//! The QA evidence-capture reproduction (issue #342): `GET /receipts`
//! and `GET /journal` return records covering the run so far — on
//! whichever peer of the pair serves them.
//!
//! The finding's defect was `receipts returned 0 entries` on the peer
//! queried after the switchover: the command audit lived only in the
//! accepting executor's memory, so a peer that never saw the submission
//! served an empty log. The checkpoint now carries the receipt log —
//! the pair's one command audit — so a tracking standby converges to
//! the accepting peer's receipts, journals their settlements as its own
//! record of the run, and the peer it promotes into keeps serving them.
//!
//! Two `Peer`s assemble the same model against `RemoteDriver`s on one
//! `PlantServer`, each behind a `WriteGate` and each serving a driven
//! monitor — the QA rig's `--standby`/`--scan-ms` wiring shrunk to
//! request-driven ticks: the standby pulls `GET /checkpoint` from the
//! active's monitor inside each `POST /scan`, exactly as the paced
//! controller does once per scan cycle.

use dcs_assembly::{assemble, sim_channel_map};
use dcs_controller::registry;
use dcs_core::{Command, CommandOutcome, CommandReceipt, JournalEvent, PointId, Value, ValueKind};
use dcs_model::PlantModel;
use dcs_monitor::{Driven, Monitor, MonitorClient};
use dcs_runtime::{Peer, WriteGate};
use dcs_sim::SimDriver;
use dcs_sim_net::{PlantServer, RemoteDriver};
use std::thread;

const TANK_LOOP: &str = include_str!("../../dcs-assembly/fixtures/tank_loop.json");

/// The process time the shared plant advances per scan — the fixture's
/// PID is parameterized for dt 0.1.
const DT: f64 = 0.1;
/// Ticks the pair runs before the command — enough for the standby to
/// have converged and journaled its own tracking record.
const N: u64 = 8;
/// The fixture's writable setpoint input — the command target.
const SETPOINT: PointId = PointId(10);
/// The pair's shared tracking secret both monitors key with — the
/// `--pair-token` deployment the announced-demotion contract requires:
/// the active's demote below verifies the standby's announced hint
/// against the keyed `line_proof` and tracks it under proof.
const PAIR_KEY: u64 = 0x517c_c1b7_2722_0a95;

/// `shutdown` on drop, so a panicking test still lets the scoped serve
/// threads exit instead of hanging the scope's join — the same pattern
/// `dcs-sim-net`'s and the standby tests use.
trait Stoppable {
    fn stop(&self);
}

impl Stoppable for PlantServer {
    fn stop(&self) {
        self.shutdown();
    }
}

impl Stoppable for Monitor<'_> {
    fn stop(&self) {
        self.shutdown();
    }
}

struct ShutdownOnDrop<'s, T: Stoppable>(&'s T);

impl<T: Stoppable> Drop for ShutdownOnDrop<'_, T> {
    fn drop(&mut self) {
        self.0.stop();
    }
}

/// The `command_settled` journal entries `client` serves whose receipt
/// answers `command` — the run's audit covering the submission.
fn settlements(client: &MonitorClient, command: &Command) -> Vec<CommandReceipt> {
    client
        .journal(0)
        .unwrap()
        .iter()
        .filter_map(|entry| match &entry.event {
            JournalEvent::CommandSettled { receipt } if receipt.command == *command => {
                Some(receipt.clone())
            }
            _ => None,
        })
        .collect()
}

#[test]
fn receipts_and_journal_cover_the_run_on_every_peer_the_audit_reached() {
    let model = PlantModel::load(TANK_LOOP).unwrap();
    let registry = registry();
    let plant = std::sync::Arc::new(
        PlantServer::bind(
            ("127.0.0.1", 0),
            SimDriver::new(sim_channel_map(&model).unwrap()).unwrap(),
        )
        .unwrap(),
    );
    let _plant = ShutdownOnDrop(&*plant);
    let plant_addr = plant.local_addr().unwrap();

    // The plant serves before the peers construct: a launched active's
    // startup claim needs the server answering, and a bound-but-unserved
    // listener lets a connect through while the claim request waits for
    // nobody.
    let serving = thread::spawn({
        let plant = std::sync::Arc::clone(&plant);
        move || plant.serve()
    });

    // The active: field-owning from the start, its driven monitor
    // stepping the shared plant inside each requested scan.
    let active_driver = RemoteDriver::connect(plant_addr).unwrap();
    let active_gate = WriteGate::closed(&active_driver);
    let mut active = Peer::active(
        assemble(&model, &registry, &active_gate).unwrap(),
        Some(&active_gate),
    )
    .with_field_claim(|| {
        active_driver
            .claim_writer(1)
            .map(|_| ())
            .map_err(|error| error.to_string())
    });
    active.activate().unwrap();
    let active_step = &active_driver;
    let active_monitor = Monitor::bind_peer(("127.0.0.1", 0), active, model.signal_index())
        .unwrap()
        .with_pair_key(PAIR_KEY)
        .driven(Driven {
            track: None,
            after_scan: Some(Box::new(move |peer: &Peer<'_>| {
                if peer.owns_field() {
                    active_step
                        .step(DT)
                        .map(|_| ())
                        .map_err(|error| format!("plant step failed: {error}"))
                } else {
                    Ok(())
                }
            })),
        });
    let active_client = MonitorClient::new(active_monitor.local_addr());

    // The standby: tracking through its driven pull — the checkpoint
    // fetch the paced controller runs once per scan cycle.
    let standby_driver = RemoteDriver::connect(plant_addr).unwrap();
    let standby_gate = WriteGate::closed(&standby_driver);
    let standby = Peer::standby(
        assemble(&model, &registry, &standby_gate).unwrap(),
        Some(&standby_gate),
    )
    .with_field_claim(|| {
        standby_driver
            .claim_writer(2)
            .map(|_| ())
            .map_err(|error| error.to_string())
    });
    let standby_step = &standby_driver;
    let standby_monitor = Monitor::bind_peer(("127.0.0.1", 0), standby, model.signal_index())
        .unwrap()
        .with_pair_key(PAIR_KEY)
        .driven(Driven {
            track: Some(active_monitor.local_addr()),
            after_scan: Some(Box::new(move |peer: &Peer<'_>| {
                if peer.owns_field() {
                    standby_step
                        .step(DT)
                        .map(|_| ())
                        .map_err(|error| format!("plant step failed: {error}"))
                } else {
                    Ok(())
                }
            })),
        });
    let standby_client = MonitorClient::new(standby_monitor.local_addr());

    thread::scope(|scope| {
        scope.spawn(|| active_monitor.serve());
        let _active_monitor = ShutdownOnDrop(&active_monitor);
        scope.spawn(|| standby_monitor.serve());
        let _standby_monitor = ShutdownOnDrop(&standby_monitor);

        // The tracking cadence: the standby's scan first pulls the
        // active's last checkpoint, so advancing it before the active
        // keeps the pair tick-for-tick.
        for _ in 0..N {
            standby_client.advance(1).unwrap();
            active_client.advance(1).unwrap();
        }
        // The run's own command, attributed like the QA lane's — the
        // receipt the defect reported missing.
        let command = Command::WriteValue {
            point: SETPOINT,
            kind: ValueKind::Float,
            value: Value::Float(40.0),
        };
        let receipt = active_client.command_as(&command, Some("qa-lane")).unwrap();
        assert!(
            matches!(receipt.outcome, CommandOutcome::Accepted { .. }),
            "{receipt:?}"
        );

        // The standby's next scan pulls the checkpoint carrying the
        // still-`Accepted` receipt — the re-queue keeps the command
        // alive across the pair — then the active's own boundary
        // settles it at the same tick. Per #689 the tracker's quiesced
        // scan carries the adopted receipt rather than settling it, so
        // one further pull adopts the applied checkpoint and the two
        // logs converge on the single settlement.
        standby_client.advance(1).unwrap();
        active_client.advance(1).unwrap();
        standby_client.advance(1).unwrap();
        // #689's one-cycle lag, healed: the tracker's quiesced scan
        // carried the adopted receipt without settling it, so the
        // staged outputs it computed lagged the field by the command's
        // effect and the adopting pull named divergence. The next
        // cycle reads the commanded field and stages matching outputs,
        // so the following pull's same-tick comparison resyncs to
        // tracking — bounded here, deterministically one more cycle.
        for _ in 0..5 {
            if matches!(
                standby_client.role().unwrap().sync,
                Some(dcs_core::StandbySync::Tracking { .. })
            ) {
                break;
            }
            active_client.advance(1).unwrap();
            standby_client.advance(1).unwrap();
        }

        // The finding: the peer that never saw the submission serves an
        // empty receipt log. Now the checkpoint carries the audit — the
        // tracking peer's `GET /receipts` converges to the run's, not
        // to an empty list.
        let active_receipts = active_client.receipts().unwrap();
        assert_eq!(
            standby_client.receipts().unwrap(),
            active_receipts,
            "the tracking peer must serve the run's receipts"
        );
        assert!(
            active_receipts.iter().any(|entry| entry.command == command
                && entry.actor.as_deref() == Some("qa-lane")
                && matches!(entry.outcome, CommandOutcome::Applied { .. })),
            "the submitted command must appear settled in the log: {active_receipts:?}"
        );

        // `GET /journal` covers the run on both peers: each recorded
        // the command's settlement — the tracking peer journals the
        // adopted receipt's outcome exactly like a local submission's.
        for client in [&active_client, &standby_client] {
            assert!(
                !client.journal(0).unwrap().is_empty(),
                "the journal must hold the run's records"
            );
            assert!(
                !settlements(client, &command).is_empty(),
                "the journal must record the command's settlement"
            );
        }

        // The switchover the original run performed before reading the
        // evidence: demote the active, promote the converged standby.
        active_client.demote().unwrap();
        standby_client.promote().unwrap();
        standby_client.advance(1).unwrap();
        active_client.advance(1).unwrap();

        // The promoted peer — the endpoint the reproduction read —
        // still serves the run's retained receipt log, its journal
        // carrying the command's settlement and the role changes of the
        // switch.
        assert_eq!(
            standby_client.receipts().unwrap(),
            active_client.receipts().unwrap()
        );
        assert!(
            standby_client
                .receipts()
                .unwrap()
                .iter()
                .any(|entry| entry.command == command),
            "the promoted peer must keep serving the run's receipts"
        );
        assert!(!settlements(&standby_client, &command).is_empty());
        let role_changes = standby_client
            .journal(0)
            .unwrap()
            .iter()
            .filter(|entry| matches!(entry.event, JournalEvent::RoleChanged { .. }))
            .count();
        assert!(
            role_changes >= 2,
            "the journal must carry the switch's role changes"
        );

        // The demoted peer keeps its own record too — like the rig's
        // ctrl-a, it tracks no new partner but retains the audit its
        // participation produced.
        assert!(
            active_client
                .receipts()
                .unwrap()
                .iter()
                .any(|entry| entry.command == command)
        );
        assert!(!settlements(&active_client, &command).is_empty());
    });
    drop(_plant);
    serving.join().unwrap();
}
