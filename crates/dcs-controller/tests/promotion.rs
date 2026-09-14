//! Promotion of a tracking standby to active over the shared simulated
//! plant — the issue's acceptance test.
//!
//! Two `Peer` instances assemble the same model against `RemoteDriver`s
//! on one `PlantServer`, each behind a `WriteGate` and each serving a
//! monitor. The standby scans gate-closed — quiesced at the field —
//! while pulling checkpoints through `GET /checkpoint`; `POST /demote`
//! on the active then `POST /promote` on the converged standby moves
//! field-write ownership at the requests' scan boundary, and the
//! promoted peer's subsequent snapshots equal the uninterrupted run the
//! demoted peer still computes. `GET /role` reports each stage of the
//! switch, the journal carries the transitions, and premature or
//! repeated promotion answers the named `SwitchError`s.

use dcs_assembly::{assemble, sim_channel_map};
use dcs_controller::registry;
use dcs_core::{
    Command, CommandError, CommandOutcome, IoDriver, JournalEvent, PointId, Role, StandbySync,
    SwitchError, Tick, Value, ValueKind,
};
use dcs_model::PlantModel;
use dcs_monitor::{Monitor, MonitorClient};
use dcs_runtime::{Peer, WriteGate};
use dcs_sim::SimDriver;
use dcs_sim_net::{PlantServer, RemoteDriver};
use std::thread;

const TANK_LOOP: &str = include_str!("../../dcs-assembly/fixtures/tank_loop.json");

/// The process time the shared plant advances per scan — the fixture's
/// PID is parameterized for dt 0.1.
const DT: f64 = 0.1;
/// Ticks the active runs before the first checkpoint transfer.
const N: u64 = 30;
/// Tracking ticks between convergence and the switchover.
const K: u64 = 10;
/// Post-promotion ticks compared against the uninterrupted run.
const M: u64 = 20;
/// The fixture's valve output point — the field write under test.
const VALVE: PointId = PointId(12);

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

/// The valve value the field currently carries, read through `driver`.
fn field_valve(driver: &RemoteDriver) -> Value {
    driver.read(VALVE).unwrap().value
}

/// The valve value `snapshot`'s output image reports.
fn image_valve(snapshot: &dcs_core::TelemetrySnapshot) -> Value {
    snapshot
        .points
        .iter()
        .find(|point| point.point == VALVE)
        .and_then(|point| point.sample)
        .unwrap()
        .value
}

#[test]
fn promotion_is_bumpless_and_exactly_one_peer_writes_the_field() {
    let model = PlantModel::load(TANK_LOOP).unwrap();
    let registry = registry();
    let plant = PlantServer::bind(
        ("127.0.0.1", 0),
        SimDriver::new(sim_channel_map(&model).unwrap()).unwrap(),
    )
    .unwrap();
    let plant_addr = plant.local_addr().unwrap();

    // The active: a remote driver behind a gate `Peer::active` opens —
    // the gate a later demotion re-closes — serving the monitor the
    // standby pulls checkpoints from.
    let active_driver = RemoteDriver::connect(plant_addr).unwrap();
    let active_gate = WriteGate::closed(&active_driver);
    let active = Peer::active(
        assemble(&model, &registry, &active_gate).unwrap(),
        Some(&active_gate),
    );
    let active_monitor =
        Monitor::bind_peer(("127.0.0.1", 0), active, model.signal_index()).unwrap();
    let active_client = MonitorClient::new(active_monitor.local_addr());

    // The standby: a second remote attachment behind a closed gate, its
    // own monitor serving `/role` and `/promote`.
    let standby_driver = RemoteDriver::connect(plant_addr).unwrap();
    let standby_gate = WriteGate::closed(&standby_driver);
    let standby = Peer::standby(
        assemble(&model, &registry, &standby_gate).unwrap(),
        Some(&standby_gate),
    );
    let standby_monitor =
        Monitor::bind_peer(("127.0.0.1", 0), standby, model.signal_index()).unwrap();
    let standby_client = MonitorClient::new(standby_monitor.local_addr());

    thread::scope(|scope| {
        scope.spawn(|| plant.serve());
        let _plant = ShutdownOnDrop(&plant);
        scope.spawn(|| active_monitor.serve());
        let _active_monitor = ShutdownOnDrop(&active_monitor);
        scope.spawn(|| standby_monitor.serve());
        let _standby_monitor = ShutdownOnDrop(&standby_monitor);

        // Roles are visible before any transfer: the pair reports
        // `active` and an `unsynchronized` standby.
        assert_eq!(active_client.role().unwrap().role, Role::Active);
        let report = standby_client.role().unwrap();
        assert_eq!(report.role, Role::Standby);
        assert_eq!(report.sync, Some(StandbySync::Unsynchronized));

        // Premature promotion is the named refusal, not a take-over.
        let (status, body) = standby_client.request("POST", "/promote", None).unwrap();
        assert_eq!(status, 409, "{body}");
        assert_eq!(
            serde_json::from_str::<SwitchError>(&body).unwrap(),
            SwitchError::NotConverged {
                sync: StandbySync::Unsynchronized
            }
        );
        assert_eq!(standby_client.role().unwrap().role, Role::Standby);
        assert!(!standby_gate.is_open());

        // Phase 1: the active runs N ticks against the shared plant while
        // the standby has not yet converged.
        for _ in 0..N {
            active_client.advance(1).unwrap();
            active_driver.step(DT).unwrap();
        }
        assert_eq!(active_client.role().unwrap().tick, Tick(N));

        // The checkpoint aligns the standby at tick N — the convergence
        // promotion requires.
        standby_monitor
            .apply_checkpoint(&active_client.checkpoint().unwrap())
            .unwrap();
        let report = standby_client.role().unwrap();
        assert_eq!(report.role, Role::Standby);
        assert_eq!(
            report.sync,
            Some(StandbySync::Tracking { aligned: Tick(N) })
        );

        // Phase 2: K tracking ticks. The standby scans the same field
        // and its snapshots equal the active's, while its closed gate
        // keeps every write from the plant.
        for tick in (N + 1)..=(N + K) {
            let reference = active_client.advance(1).unwrap();
            let tracked = standby_client.advance(1).unwrap();
            active_driver.step(DT).unwrap();
            assert_eq!(tracked, reference, "tick {tick}");
            assert_eq!(field_valve(&standby_driver), image_valve(&reference));
        }

        // Quiescence at the plant: a command is refused at the role
        // boundary rather than phantom-applied, and a write through the
        // standby's gate — the channel every executor write traverses —
        // never lands.
        let receipt = standby_client
            .command(&Command::WriteValue {
                point: PointId(10),
                kind: ValueKind::Float,
                value: Value::Float(42.0),
            })
            .unwrap();
        assert!(
            matches!(
                receipt.outcome,
                CommandOutcome::Rejected {
                    reason: CommandError::NotActive {
                        role: Role::Standby,
                        ..
                    }
                }
            ),
            "{receipt:?}"
        );
        let held = field_valve(&standby_driver);
        standby_gate.write(VALVE, Value::Float(-42.0)).unwrap();
        assert_eq!(field_valve(&standby_driver), held);

        // The documented switchover order at the scan boundary between
        // ticks N+K and N+K+1: demote the active first — its gate closes
        // with the request — then promote the converged standby, whose
        // gate lifts at the same boundary.
        let demoted = active_client.demote().unwrap();
        assert_eq!(demoted.role, Role::Demoting);
        assert!(!active_gate.is_open());
        let promoted = standby_client.promote().unwrap();
        assert_eq!(promoted.role, Role::Promoting);
        assert!(standby_gate.is_open());

        // Repeating the promotion mid-transition is the named error too.
        let (status, body) = standby_client.request("POST", "/promote", None).unwrap();
        assert_eq!(status, 409, "{body}");
        assert_eq!(
            serde_json::from_str::<SwitchError>(&body).unwrap(),
            SwitchError::AlreadyActive
        );

        // Phase 3: M post-switch ticks. Each tick the demoted peer scans
        // quiesced — still computing the uninterrupted reference run —
        // then the promoted peer's scan writes the field and the plant
        // steps once. The snapshots are equal tick for tick: the
        // takeover is bumpless, and the field carries the promoted
        // peer's write.
        for tick in (N + K + 1)..=(N + K + M) {
            let reference = active_client.advance(1).unwrap();
            let continued = standby_client.advance(1).unwrap();
            standby_driver.step(DT).unwrap();
            assert_eq!(continued, reference, "tick {tick}");
            assert_eq!(field_valve(&standby_driver), image_valve(&continued));
        }
        assert_eq!(standby_client.role().unwrap().role, Role::Active);
        assert_eq!(standby_client.role().unwrap().sync, None);
        assert_eq!(active_client.role().unwrap().role, Role::Standby);

        // The demoted active stays quiesced: a write through its gate
        // drops like the standby's did, and its commands are refused.
        let held = field_valve(&standby_driver);
        active_gate.write(VALVE, Value::Float(-42.0)).unwrap();
        assert_eq!(field_valve(&standby_driver), held);
        let receipt = active_client
            .command(&Command::WriteValue {
                point: PointId(10),
                kind: ValueKind::Float,
                value: Value::Float(1.0),
            })
            .unwrap();
        assert!(matches!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::NotActive {
                    role: Role::Standby,
                    ..
                }
            }
        ));

        // A settled active still refuses promotion; a standby refuses
        // demotion — both named.
        let (status, body) = standby_client.request("POST", "/promote", None).unwrap();
        assert_eq!(status, 409, "{body}");
        assert_eq!(
            serde_json::from_str::<SwitchError>(&body).unwrap(),
            SwitchError::AlreadyActive
        );
        let (status, body) = active_client.request("POST", "/demote", None).unwrap();
        assert_eq!(status, 409, "{body}");
        assert_eq!(
            serde_json::from_str::<SwitchError>(&body).unwrap(),
            SwitchError::NotActive
        );

        // The transition journal carries the reported-role changes of
        // the switch: standby → promoting → active on the promoted
        // peer, active → demoting → standby on the demoted one.
        let role_changes = |client: &MonitorClient| -> Vec<(Role, Role)> {
            client
                .journal(0)
                .unwrap()
                .iter()
                .filter_map(|entry| match entry.event {
                    JournalEvent::RoleChanged { from, to } => Some((from, to)),
                    _ => None,
                })
                .collect()
        };
        assert_eq!(
            role_changes(&standby_client),
            vec![
                (Role::Standby, Role::Promoting),
                (Role::Promoting, Role::Active)
            ]
        );
        assert_eq!(
            role_changes(&active_client),
            vec![
                (Role::Active, Role::Demoting),
                (Role::Demoting, Role::Standby)
            ]
        );
    });
}
