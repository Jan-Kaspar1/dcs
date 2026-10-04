//! The claim-basis bound on the promotion claim — QA finding
//! `skewed-claim-preempts-live-incumbent-unbounded` (run
//! `qax-20261002-007`, rig `lenovo`, scenario leg
//! `2490_claim_skew_bound`) as a durable product regression.
//!
//! The finding's subject is the claim path: a standby whose own run tick
//! accrues several for every one the field owner's does — the QA lane
//! stages it through the born launcher's per-container `--scan-ms`
//! lever, 25 ms against a 100 ms holder — converges `tracking` on the
//! live incumbent and asks for the field. The field's arbitration
//! resolves a claim on no basis at all, so the claim landed, the
//! incumbent's next write met the fence, and it demoted itself in place:
//! a takeover decided on a pair of stamps `CONTEXT.md`'s same-domain
//! comparability rule forbids ordering, with the pacing skew as the only
//! thing that separated the two runs.
//!
//! The bound rides the claim's ask, not the field's grant
//! (`dcs_runtime::Peer::check_claim_basis`): the field sees an owner
//! token and a monitor and cannot translate one run's stamp into
//! another's, so the run that holds both — the claimant, with its own
//! run tick and the tracked line's last served stamp — is the one to
//! refuse. It refuses only where something stands to preempt: an
//! `unclaimed` probe and a tracked line that stopped serving this run
//! both mean nothing demonstrably stands in the way, and every
//! conditional claim (the startup grant, the orphaned promotion, the
//! re-arm, the reclaim) is untouched.
//!
//! These tests drive the same pair over the shared simulated plant the
//! promotion test drives, at the two cadences the rig uses: the skewed
//! claimant's `POST /promote` is answered `field_claim_failed` with the
//! bound named, the incumbent keeps writing the field and journals
//! neither a claim loss nor a fenced demotion, and the in-bound arm's
//! switchover — the documented takeover the bound must not wedge — lands
//! exactly as `promotion.rs` proves it does without it.

use dcs_assembly::{assemble, sim_channel_map};
use dcs_controller::registry;
use dcs_core::{
    FieldClaim, IoDriver, JournalEvent, Role, StandbySync, SwitchError, SwitchOrigin, Value,
};
use dcs_model::PlantModel;
use dcs_monitor::{Monitor, MonitorClient};
use dcs_runtime::{MAX_CLAIM_LEAD, Peer, WriteGate};
use dcs_sim::SimDriver;
use dcs_sim_net::{PlantServer, RemoteDriver, RemoteError};
use std::thread;

const TANK_LOOP: &str = include_str!("../../dcs-assembly/fixtures/tank_loop.json");

/// The process time the shared plant advances per scan — the fixture's
/// PID is parameterized for dt 0.1.
const DT: f64 = 0.1;
/// The field owner's owner token — the live incumbent every arm's claim
/// would preempt.
const HOLDER: u64 = 1001;
/// The promoting standby's owner token.
const CLAIMANT: u64 = 2002;
/// The fixture's valve output point — the field write under test.
const VALVE: dcs_core::PointId = dcs_core::PointId(12);
/// The holder's scans before the claimant's arms start, so the claimant
/// lands its first transfer behind the line as a freshly launched
/// tracker does.
const LEAD_IN: u64 = 4;

/// `shutdown` on drop, so a panicking test still lets the scoped serve
/// threads exit instead of hanging the scope's join.
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

/// The skew the finding's rig staged: the claimant's run tick accrues
/// this many for every one the holder's does, so its lead over the
/// tracked line grows on every transfer.
const PACES: (u64, u64) = (4, 1);

/// One holder scan against `claimant_scans` claimant scans, each
/// claimant scan pulling the holder's latest checkpoint — the pull-per-
/// scan cadence the controller drives, at the rig's two cadences. The
/// holder's stamp stands while the claimant runs, so the claimant's own
/// tick is the only clock moving: the declared lead is the staged skew,
/// and `tick` is what the pair's served `/role` reports at the end.
fn pace(
    holder: &MonitorClient,
    holder_driver: &RemoteDriver,
    claimant: &MonitorClient,
    claimant_monitor: &Monitor,
    claimant_scans: u64,
) {
    let served = holder.checkpoint().unwrap();
    for _ in 0..claimant_scans {
        claimant.advance(1).unwrap();
        claimant_monitor.apply_checkpoint(&served).unwrap();
    }
    holder.advance(1).unwrap();
    holder_driver.step(DT).unwrap();
}

/// The measured skew the leg reads off the rig: the claimant's served
/// run tick minus the line's last served stamp, which is the holder's
/// own served tick at the moment the claimant last pulled.
fn separation(claimant: &MonitorClient) -> i64 {
    let report = claimant.role().unwrap();
    let aligned = match report.sync {
        Some(StandbySync::Tracking { aligned }) => aligned,
        other => panic!("the claimant must stay tracking, got {other:?}"),
    };
    report.tick.0 as i64 - aligned.0 as i64
}

/// The refused promote's named verdict — the answer a refusal must
/// carry, parsed off the 409 the monitor serves.
fn refused_promote(claimant: &MonitorClient) -> SwitchError {
    let (status, body) = claimant.request("POST", "/promote", None).unwrap();
    assert_eq!(status, 409, "a refused claim answers 409: {body}");
    serde_json::from_str::<SwitchError>(&body).unwrap()
}

/// The role transitions a peer's journal carries — the takeover's
/// evidence, and the absence of which is the leg's incumbent clause.
fn role_walk(client: &MonitorClient) -> Vec<(Role, Role, Option<SwitchOrigin>)> {
    client
        .journal(0)
        .unwrap()
        .iter()
        .filter_map(|entry| match &entry.event {
            JournalEvent::RoleChanged {
                from, to, origin, ..
            } => Some((*from, *to, *origin)),
            _ => None,
        })
        .collect()
}

/// The owner tokens the peer's journal attributes a lost field claim to
/// — the `field_claim_lost` record the fencing path queues.
fn claim_losses(client: &MonitorClient) -> Vec<Option<u64>> {
    client
        .journal(0)
        .unwrap()
        .iter()
        .filter_map(|entry| match &entry.event {
            JournalEvent::FieldClaimLost { claimant, .. } => Some(*claimant),
            _ => None,
        })
        .collect()
}

/// The QA finding's own sequence, on the shared simulated plant: the
/// holder is a born-active holding the field under its conditional
/// startup grant, and the claimant is a `--standby` peer converged
/// `tracking` on it at four times its cadence. The skew is read back off
/// the pair's served ticks before the claim goes out — an attempt
/// computed inside the bound would prove nothing — and the claim must
/// not take the field.
#[test]
fn a_skewed_promote_cannot_preempt_a_live_incumbent() {
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
    let serving = thread::spawn({
        let plant = std::sync::Arc::clone(&plant);
        move || plant.serve()
    });

    // The live incumbent: a launched-active taking the field's
    // write-ownership claim, scanning and stepping the shared plant. The
    // `as_controller` marker is the one the launched controller stamps
    // on its own attachment — only a controller's claim is the live
    // incumbent a conditional grant refuses to preempt.
    let holder_driver = RemoteDriver::connect(plant_addr).unwrap().as_controller();
    let holder_gate = WriteGate::closed(&holder_driver);
    let mut holder = Peer::active(
        assemble(&model, &registry, &holder_gate).unwrap(),
        Some(&holder_gate),
    )
    .with_field_claim(|| {
        holder_driver
            .claim_writer(HOLDER)
            .map(|_| ())
            .map_err(|error| error.to_string())
    })
    .with_field_probe(|| {
        holder_driver
            .probe_writer()
            .map_err(|error| error.to_string())
    })
    .with_field_release(|| holder_driver.release_claim());
    holder.activate().unwrap();
    let holder_monitor =
        Monitor::bind_peer(("127.0.0.1", 0), holder, model.signal_index()).unwrap();
    let holder_client = MonitorClient::new(holder_monitor.local_addr());

    // The claimant: the same launch shape as a `--standby` peer, its
    // own monitor serving the role and promote surfaces.
    let claimant_driver = RemoteDriver::connect(plant_addr).unwrap().as_controller();
    let claimant_gate = WriteGate::closed(&claimant_driver);
    let claimant = Peer::standby(
        assemble(&model, &registry, &claimant_gate).unwrap(),
        Some(&claimant_gate),
    )
    .with_field_claim(|| {
        claimant_driver
            .claim_writer(CLAIMANT)
            .map(|_| ())
            .map_err(|error| error.to_string())
    })
    .with_field_probe(|| {
        claimant_driver
            .probe_writer()
            .map_err(|error| error.to_string())
    })
    .with_field_release(|| claimant_driver.release_claim());
    let claimant_monitor =
        Monitor::bind_peer(("127.0.0.1", 0), claimant, model.signal_index()).unwrap();
    let claimant_client = MonitorClient::new(claimant_monitor.local_addr());

    thread::scope(|scope| {
        scope.spawn(|| holder_monitor.serve());
        let _holder_monitor = ShutdownOnDrop(&holder_monitor);
        scope.spawn(|| claimant_monitor.serve());
        let _claimant_monitor = ShutdownOnDrop(&claimant_monitor);

        // The holder's first scan makes the claim posture observable:
        // the per-scan probe answers the field's own arbitration.
        holder_client.advance(1).unwrap();
        holder_driver.step(DT).unwrap();
        assert_eq!(
            holder_client.role().unwrap().field_claim,
            Some(FieldClaim::Held),
            "the holder's claim is what the claimant's claim would preempt"
        );
        assert!(holder_gate.is_open());

        // The staged skew: the claimant tracks the holder while running
        // four scans for the holder's one, until the measured separation
        // passes the recorded bound.
        for _ in 0..LEAD_IN {
            pace(
                &holder_client,
                &holder_driver,
                &claimant_client,
                &claimant_monitor,
                PACES.0,
            );
        }
        while separation(&claimant_client) <= MAX_CLAIM_LEAD {
            pace(
                &holder_client,
                &holder_driver,
                &claimant_client,
                &claimant_monitor,
                PACES.0,
            );
        }
        let skew = separation(&claimant_client);
        assert!(
            skew > MAX_CLAIM_LEAD,
            "the staging must sit past the bound, got {skew}"
        );

        // The claim: refused, by name, naming the bound the basis
        // exceeded. Not `not_converged` — the claimant is converged, and
        // the answer has to be the arbitration's own verdict.
        let error = refused_promote(&claimant_client);
        let detail = match &error {
            SwitchError::FieldClaimFailed { detail } => detail.clone(),
            other => panic!("the bound must refuse the claim by name, got {other:?}"),
        };
        assert!(detail.contains("recorded skew bound"), "{detail}");
        assert!(
            detail.contains("--scan-ms"),
            "the refusal names the remedy an operator acts on: {detail}"
        );
        // The refusal is auditable with its own evidence: both stamps
        // the basis was measured from, so an operator reading the 409
        // sees which line the claim outran rather than a bare verdict.
        let report = claimant_client.role().unwrap();
        assert!(
            detail.contains(&format!("run tick {}", report.tick.0)),
            "{detail}"
        );
        assert!(
            detail.contains(&format!(
                "stamp {}",
                report
                    .sync
                    .and_then(|sync| match sync {
                        StandbySync::Tracking { aligned } => Some(aligned.0),
                        _ => None,
                    })
                    .expect("a converged claimant")
            )),
            "{detail}"
        );

        // The incumbent across the attempt: still the field's writer,
        // still active, its scan advancing, and a different owner's
        // conditional claim refused — the field's own answer that the
        // claim never moved.
        assert_eq!(holder_client.role().unwrap().role, Role::Active);
        assert_eq!(
            holder_client.role().unwrap().field_claim,
            Some(FieldClaim::Held)
        );
        assert!(holder_gate.is_open());
        let third = claimant_driver.claim_writer_unless_held(0xf00d);
        assert!(
            matches!(third, Err(RemoteError::Fenced)),
            "the holder's claim still stands against a third attachment, got {third:?}"
        );
        let before = holder_client.role().unwrap().tick;
        holder_client.advance(1).unwrap();
        holder_driver.step(DT).unwrap();
        assert!(
            holder_client.role().unwrap().tick > before,
            "the incumbent keeps scanning through the refused claim"
        );

        // The claimant kept its role, its closed gate, and a fenced
        // field: the promotion touched nothing.
        assert_eq!(claimant_client.role().unwrap().role, Role::Standby);
        assert!(!claimant_gate.is_open());
        let held = holder_driver.read(VALVE).unwrap().value;
        claimant_gate.write(VALVE, Value::Float(-1.0)).unwrap();
        assert_eq!(
            holder_driver.read(VALVE).unwrap().value,
            held,
            "the refused claimant's closed gate never reaches the field"
        );

        // The incumbent's journal carries no claim loss and no fenced
        // demotion — the takeover the finding recorded is gone, and the
        // claimant journaled none either.
        assert!(
            role_walk(&holder_client).is_empty(),
            "the incumbent never transitions: a refused claim takes nothing"
        );
        assert!(
            claim_losses(&holder_client).is_empty(),
            "and never loses the claim it holds"
        );
        assert!(
            role_walk(&claimant_client).is_empty(),
            "a refused promotion transitions nothing"
        );

        // The escape the bound must leave open: with nothing standing to
        // preempt, the same claimant takes the free field. The field's
        // claim is released here — the tool-side hand-back the forced
        // hand-off rides — so the claimant's own next probe reads
        // `unclaimed` and the claim is unopposed.
        holder_driver.release_writer().unwrap();
        claimant_client.advance(1).unwrap();
        assert_eq!(
            claimant_client.role().unwrap().field_claim,
            Some(FieldClaim::Unclaimed),
            "the claimant's own fresh probe is the observation the escape reads"
        );
        let promoted = claimant_client.promote().unwrap();
        assert_eq!(promoted.role, Role::Promoting);
        assert!(claimant_gate.is_open());
        assert!(
            claimant_driver.step(DT).is_ok(),
            "the promoted claim now owns the field's write-ownership"
        );
    });
    drop(_plant);
    serving.join().unwrap();
}

/// The other half of the contract: a claim whose basis sits inside the
/// bound is unaffected. At the holder's own cadence the claimant's lead
/// never grows, and the documented switchover lands — the takeover, the
/// fenced-origin demotion walk, and the attributed `field_claim_lost`
/// the promotion test asserts without the bound in place.
#[test]
fn an_in_bound_promote_still_takes_the_field() {
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
    let serving = thread::spawn({
        let plant = std::sync::Arc::clone(&plant);
        move || plant.serve()
    });

    // The `as_controller` marker the launched controller stamps on its
    // own attachment: only a controller's claim is the live incumbent a
    // conditional grant refuses to preempt.
    let holder_driver = RemoteDriver::connect(plant_addr).unwrap().as_controller();
    let holder_gate = WriteGate::closed(&holder_driver);
    let mut holder = Peer::active(
        assemble(&model, &registry, &holder_gate).unwrap(),
        Some(&holder_gate),
    )
    .with_field_claim(|| {
        holder_driver
            .claim_writer(HOLDER)
            .map(|_| ())
            .map_err(|error| error.to_string())
    })
    .with_field_probe(|| {
        holder_driver
            .probe_writer()
            .map_err(|error| error.to_string())
    })
    .with_field_release(|| holder_driver.release_claim())
    .with_field_claimant(|_| holder_driver.fenced_by());
    holder.activate().unwrap();
    let holder_monitor =
        Monitor::bind_peer(("127.0.0.1", 0), holder, model.signal_index()).unwrap();
    let holder_client = MonitorClient::new(holder_monitor.local_addr());

    let claimant_driver = RemoteDriver::connect(plant_addr).unwrap().as_controller();
    let claimant_gate = WriteGate::closed(&claimant_driver);
    let claimant = Peer::standby(
        assemble(&model, &registry, &claimant_gate).unwrap(),
        Some(&claimant_gate),
    )
    .with_field_claim(|| {
        claimant_driver
            .claim_writer(CLAIMANT)
            .map(|_| ())
            .map_err(|error| error.to_string())
    })
    .with_field_probe(|| {
        claimant_driver
            .probe_writer()
            .map_err(|error| error.to_string())
    })
    .with_field_release(|| claimant_driver.release_claim());
    let claimant_monitor =
        Monitor::bind_peer(("127.0.0.1", 0), claimant, model.signal_index()).unwrap();
    let claimant_client = MonitorClient::new(claimant_monitor.local_addr());

    thread::scope(|scope| {
        scope.spawn(|| holder_monitor.serve());
        let _holder_monitor = ShutdownOnDrop(&holder_monitor);
        scope.spawn(|| claimant_monitor.serve());
        let _claimant_monitor = ShutdownOnDrop(&claimant_monitor);

        // Many more aligned transfers than the bound's window: the
        // alignment is long-lived and the basis still sits inside it.
        for _ in 0..2 * MAX_CLAIM_LEAD {
            pace(
                &holder_client,
                &holder_driver,
                &claimant_client,
                &claimant_monitor,
                PACES.1,
            );
        }
        let skew = separation(&claimant_client);
        assert!(
            skew <= MAX_CLAIM_LEAD,
            "an aligned pair's lead never grows past the bound, got {skew}"
        );
        assert!(matches!(
            holder_client.role().unwrap().sync,
            None | Some(StandbySync::Tracking { .. })
        ));

        // The documented switchover: the claim lands, the gate lifts,
        // and the incumbent demotes in place under the fence.
        let promoted = claimant_client.promote().unwrap();
        assert_eq!(promoted.role, Role::Promoting);
        assert!(claimant_gate.is_open());
        claimant_client.advance(1).unwrap();
        claimant_driver.step(DT).unwrap();
        holder_client.advance(1).unwrap();
        for _ in 0..2 {
            holder_client.advance(1).unwrap();
        }
        assert_eq!(claimant_client.role().unwrap().role, Role::Active);
        assert_eq!(
            holder_client.role().unwrap().role,
            Role::Standby,
            "the demoted owner settles back on tracking"
        );
        assert_eq!(
            role_walk(&holder_client),
            vec![
                (Role::Active, Role::Demoting, Some(SwitchOrigin::Fenced)),
                (Role::Demoting, Role::Standby, Some(SwitchOrigin::Fenced)),
            ],
            "the fenced-origin demotion walk, exactly as before the bound"
        );
        assert_eq!(
            claim_losses(&holder_client),
            vec![Some(CLAIMANT)],
            "the claim loss is attributed to the promoted peer's own token"
        );
        assert_eq!(
            role_walk(&claimant_client),
            vec![
                (Role::Standby, Role::Promoting, Some(SwitchOrigin::Request)),
                (Role::Promoting, Role::Active, Some(SwitchOrigin::Request)),
            ],
            "the requested switchover journals as an operator request"
        );
    });
    drop(_plant);
    serving.join().unwrap();
}
