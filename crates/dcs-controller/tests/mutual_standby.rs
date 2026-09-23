//! The QA finding `mutual-standby-tracking-deadlock`: two peers settle
//! into mutual `standby`/`tracking` — each pulling the other's
//! checkpoint cleanly — while no peer owns the field, the plant freezes,
//! and nothing reports the outage. The tracking contract's "a produced
//! checkpoint means the source serves" heartbeat cannot see it: both
//! peers serve.
//!
//! The fix stamps every served checkpoint with its run's field
//! ownership (`source_owns_field`), so a puller can tell "the tracked
//! line's serving run writes the field" from "it serves checkpoints but
//! owns nothing". The second shape moves the puller to the named
//! `orphaned` sync state — journaled as `field_orphaned`, surfaced by
//! `/role` and pair health, still promotable — instead of reporting
//! healthy `tracking` forever. A demoted ex-owner additionally probes
//! the conditional `ensure_writer` re-arm each orphan cycle: granted
//! only while the field is unclaimed or already names its token, so a
//! released claim re-arms rather than leaving the field open to a
//! foreign grab, while a standing owner is never preempted.
//!
//! Both reproduction paths the finding names are scripted here on the
//! simulated rig: the foreign `claim_writer` preemption followed by
//! `release_writer`, and the pure-ops `demote -> promote -> restart the
//! promoted peer as --standby`.

use dcs_core::{IoDriver, JournalEvent, Role, StandbySync, Value};
use dcs_monitor::MonitorClient;
use dcs_sim_net::{RemoteDriver, RemoteError};
use std::path::Path;

mod support;

use support::{
    CONTROLLER, PAIR_TOKEN, SimTcp, controller_model, image_value, kill, listening_on, spawn,
    spawn_controller, spawn_plant,
};

/// The shared plant's model — the same tank loop the hot-swap rig runs.
const PLANT_MODEL: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-plant/fixtures/tank_loop.json"
);
const PLANT_DYNAMICS: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-plant/fixtures/tank_loop_dynamics.json"
);
const MODEL_SOURCE: &str = include_str!("../../dcs-plant/fixtures/tank_loop.json");
const DT: &str = "0.1";
/// The throwaway token the pre-spawn setpoint seed claims under.
const SEED: u64 = 499_900;
/// The foreign attachment's claim token — any token neither controller
/// generated stands in for the QA reproduction's rogue claim.
const FOREIGN: u64 = 999;
const SETPOINT: dcs_core::PointId = dcs_core::PointId(11);
const VALVE: dcs_core::PointId = dcs_core::PointId(20);

/// Whether `report` is the wedge's old disguise: `standby` reporting
/// healthy `tracking`.
fn reports_healthy_tracking(report: &dcs_core::RoleReport) -> bool {
    report.role == Role::Standby && matches!(report.sync, Some(StandbySync::Tracking { .. }))
}

/// Whether `report` surfaces the missing field owner: `standby`
/// reporting the named `orphaned` sync state.
fn reports_orphaned(report: &dcs_core::RoleReport) -> bool {
    report.role == Role::Standby && matches!(report.sync, Some(StandbySync::Orphaned { .. }))
}

/// Trigger A of the QA reproduction: a protocol-legal foreign
/// `claim_writer` preempts the active's claim; the first fenced write
/// demotes the ex-owner in place; the foreign claim is then released —
/// and the old code left the demoted ex-owner tracking its standby
/// forever, the field unclaimed, the plant frozen, both peers reporting
/// healthy `tracking`.
///
/// Now the demoted ex-owner's checkpoint pulls land `orphaned` — the
/// tracked line's serving run owns nothing — and each orphan cycle
/// probes the conditional claim re-arm: refused while the foreign token
/// stands, granted once the release leaves the field unclaimed, so the
/// claim re-arms for the ex-owner's token rather than staying open to
/// the next foreign grab. Both peers report the named state throughout,
/// and the journaled `field_orphaned` carries the outage.
#[test]
fn foreign_claim_release_lets_the_demoted_ex_owner_rearm() {
    let dir = std::env::temp_dir().join(format!("dcs-mutual-standby-claim-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();

    let plant = spawn_plant(Path::new(PLANT_MODEL), Path::new(PLANT_DYNAMICS));
    let model = controller_model(
        &dir,
        "pair.json",
        MODEL_SOURCE,
        plant.addr,
        SimTcp::PerDevice,
    )
    .0;

    let field = RemoteDriver::connect(plant.addr).unwrap();
    field.ensure_writer(SEED).unwrap();
    field.write(SETPOINT, Value::Float(50.0)).unwrap();
    field.release_writer().unwrap();

    // The reproduction's launch shape: the active names no peer; the
    // standby tracks it by --standby, announcing its own monitor on
    // every pull so the demoted run knows where its successor lives.
    let active_process = spawn_controller(&model, &[], DT);
    let standby_process = spawn_controller(
        &model,
        &["--standby".to_string(), active_process.addr.to_string()],
        DT,
    );
    let active = MonitorClient::new(active_process.addr);
    let standby = MonitorClient::new(standby_process.addr);

    // Converge the standby and let the plant step a few ticks.
    for _ in 0..4 {
        standby.advance(1).unwrap();
        active.advance(1).unwrap();
    }
    assert!(
        matches!(
            standby.role().unwrap().sync,
            Some(StandbySync::Tracking { .. })
        ),
        "the standby never converged"
    );

    // The trigger: a foreign attachment claims the field. The ex-owner's
    // next field write is fenced and it demotes itself in place — the
    // documented field_claim_lost path.
    field.claim_writer(FOREIGN).unwrap();
    active.advance(1).unwrap(); // the fenced scan: demoting
    active.advance(1).unwrap(); // settles standby; the first pull orphans
    let report = active.role().unwrap();
    assert_eq!(report.role, Role::Standby, "{report:?}");

    // The wedge: the demoted ex-owner tracks the still-standby peer and
    // the standby tracks it back — every pull applies cleanly while no
    // peer owns the field. The old contract reported both as healthy
    // `tracking`; now each reports the named `orphaned` state.
    standby.advance(1).unwrap();
    for _ in 0..2 {
        active.advance(1).unwrap();
        standby.advance(1).unwrap();
    }
    let active_report = active.role().unwrap();
    let standby_report = standby.role().unwrap();
    assert!(
        reports_orphaned(&active_report),
        "the demoted ex-owner must surface the unowned line, got {active_report:?}"
    );
    assert!(
        reports_orphaned(&standby_report),
        "the standby must surface the unowned line, got {standby_report:?}"
    );
    assert!(
        !reports_healthy_tracking(&active_report) && !reports_healthy_tracking(&standby_report),
        "the wedge must never present as mutual healthy tracking"
    );

    // While the foreign claim stands, the ex-owner's conditional re-arm
    // probes every orphan cycle but never preempts: a foreign mutation
    // attempt is the standing claim's fence, not the probe's.
    field
        .write(SETPOINT, Value::Float(60.0))
        .expect("a claim holder writes");
    assert_eq!(field.read(SETPOINT).unwrap().value, Value::Float(60.0));

    // The journaled outage: the demoted run's fencing loss and the
    // orphan transition are both durable — nothing silent.
    let journal = active.journal(0).unwrap();
    assert!(
        journal
            .iter()
            .any(|entry| matches!(entry.event, JournalEvent::FieldClaimLost { .. })),
        "the fencing loss must journal: {journal:?}"
    );
    assert!(
        journal
            .iter()
            .any(|entry| matches!(entry.event, JournalEvent::FieldOrphaned { .. })),
        "the orphan transition must journal: {journal:?}"
    );

    // The reproduction's release: the foreign claim hands the field
    // back. The ex-owner's next orphan cycle re-arms its released claim
    // through ensure_writer — conditional, so it lands only because no
    // owner stands — and the field is closed to foreign grabs again:
    // a fresh foreign ensure is refused, a foreign write is fenced.
    field.release_writer().unwrap();
    active.advance(1).unwrap();
    assert!(
        matches!(field.ensure_writer(FOREIGN), Err(RemoteError::Fenced)),
        "the ex-owner's re-armed claim must fence the foreign attachment"
    );
    assert!(
        reports_orphaned(&active.role().unwrap()),
        "re-arming the claim is not ownership: the peer still surfaces orphaned"
    );

    // The escape: a promotion takes the field for the re-armed token
    // and the pair converges to exactly one active — the standby
    // reconverges on the owner's checkpoints.
    assert_eq!(active.promote().unwrap().role, Role::Promoting);
    active.advance(1).unwrap();
    standby.advance(1).unwrap();
    assert_eq!(active.role().unwrap().role, Role::Active);
    assert!(
        matches!(
            standby.role().unwrap().sync,
            Some(StandbySync::Tracking { .. })
        ),
        "the standby must reconverge on the restored owner"
    );
    // And the plant steps again: the owner's write reaches the field.
    let snapshot = active.advance(1).unwrap();
    assert_eq!(
        field.read(VALVE).unwrap().value,
        image_value(&snapshot, VALVE),
        "the promoted ex-owner's write must reach the field"
    );

    let _ = std::fs::remove_dir_all(&dir);
}

/// Trigger B of the QA reproduction — the pure-ops path: `POST /demote`
/// on the active, `POST /promote` on the standby, then a container
/// restart of the newly promoted peer, which relaunches `--standby` and
/// tracks the demoted peer while the demoted peer still tracks it. The
/// field stays fenced to the dead owner's token — the claim outlives
/// the connection by design — and both peers used to report healthy
/// `tracking` while the plant froze.
///
/// Now each peer's pulls land checkpoints stamped "the serving run owns
/// nothing", both report `orphaned`, the transition journals
/// `field_orphaned`, and a `POST /promote` on the orphaned ex-owner —
/// promotable on the same convergence `tracking` proves — escapes the
/// wedge with exactly one active.
#[test]
fn demote_promote_restart_reports_orphaned_not_healthy_tracking() {
    let dir =
        std::env::temp_dir().join(format!("dcs-mutual-standby-restart-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();

    let plant = spawn_plant(Path::new(PLANT_MODEL), Path::new(PLANT_DYNAMICS));
    let model = controller_model(
        &dir,
        "pair.json",
        MODEL_SOURCE,
        plant.addr,
        SimTcp::PerDevice,
    )
    .0;

    let field = RemoteDriver::connect(plant.addr).unwrap();
    field.ensure_writer(SEED).unwrap();
    field.write(SETPOINT, Value::Float(50.0)).unwrap();
    field.release_writer().unwrap();

    let active_process = spawn_controller(&model, &[], DT);
    let mut standby_process = spawn_controller(
        &model,
        &["--standby".to_string(), active_process.addr.to_string()],
        DT,
    );
    let active = MonitorClient::new(active_process.addr);
    let standby_addr = standby_process.addr;

    for _ in 0..4 {
        MonitorClient::new(standby_addr).advance(1).unwrap();
        active.advance(1).unwrap();
    }

    // The reproduction's ops sequence: demote the active, promote the
    // standby, let both settle — then restart the promoted peer back
    // into `--standby`, the launch flag the rig carries.
    assert_eq!(active.demote().unwrap().role, Role::Demoting);
    let standby = MonitorClient::new(standby_addr);
    assert_eq!(standby.promote().unwrap().role, Role::Promoting);
    active.advance(1).unwrap();
    standby.advance(1).unwrap();
    assert_eq!(standby.role().unwrap().role, Role::Active);

    // The container restart: the same monitor address comes back as a
    // tracking standby of the demoted peer — the mutual loop the wedge
    // lives on. The demoted peer's announced tracking source still names
    // this address, so its pulls now land on a run that owns nothing.
    kill(&mut standby_process);
    let restarted = spawn(
        Path::new(CONTROLLER),
        &[
            model.to_str().unwrap().to_string(),
            "--standby".to_string(),
            active_process.addr.to_string(),
            "--listen".to_string(),
            standby_addr.to_string(),
            "--driven".to_string(),
            "--dt".to_string(),
            DT.to_string(),
            "--pair-token".to_string(),
            PAIR_TOKEN.to_string(),
        ],
        listening_on,
    );
    let restarted = MonitorClient::new(restarted.addr);

    // Drive the wedge: every pull applies cleanly in both directions —
    // the old contract's "healthy tracking" — but no peer owns the
    // field. Within the bound both must report the named `orphaned`
    // state instead.
    for _ in 0..4 {
        active.advance(1).unwrap();
        restarted.advance(1).unwrap();
    }
    let active_report = active.role().unwrap();
    let restarted_report = restarted.role().unwrap();
    assert!(
        reports_orphaned(&active_report),
        "the demoted ex-owner must surface the unowned line, got {active_report:?}"
    );
    assert!(
        reports_orphaned(&restarted_report),
        "the restarted successor must surface the unowned line, got {restarted_report:?}"
    );

    // The field stays fenced to the dead owner's token: the claim
    // outlives the connection by design, and the ex-owner's conditional
    // re-arm — refused while a different owner stands — must not
    // preempt it. A foreign attachment finds the claim still standing.
    assert!(
        matches!(field.ensure_writer(FOREIGN), Err(RemoteError::Fenced)),
        "the dead owner's claim still stands: no probe may preempt it"
    );

    // The outage is durable: the orphan transition journaled on the
    // ex-owner, attributed beside the generation restart it followed.
    let journal = active.journal(0).unwrap();
    assert!(
        journal
            .iter()
            .any(|entry| matches!(entry.event, JournalEvent::FieldOrphaned { .. })),
        "the orphan transition must journal: {journal:?}"
    );

    // The escape: promoting the orphaned ex-owner takes the field — the
    // unconditional claim preempts the dead token — and the pair
    // converges to exactly one active, the restarted peer tracking it.
    assert_eq!(active.promote().unwrap().role, Role::Promoting);
    active.advance(1).unwrap();
    assert_eq!(active.role().unwrap().role, Role::Active);
    restarted.advance(1).unwrap();
    let restarted_report = restarted.role().unwrap();
    assert_eq!(restarted_report.role, Role::Standby, "{restarted_report:?}");
    assert!(
        matches!(restarted_report.sync, Some(StandbySync::Tracking { .. })),
        "the restarted peer must reconverge on the restored owner: {restarted_report:?}"
    );

    // And the plant steps again under the restored owner.
    let snapshot = active.advance(1).unwrap();
    assert_eq!(
        field.read(VALVE).unwrap().value,
        image_value(&snapshot, VALVE),
        "the promoted ex-owner's write must reach the field"
    );

    let _ = std::fs::remove_dir_all(&dir);
}
