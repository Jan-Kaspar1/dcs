//! The unclaimed-field signal's driven-pair contract: an external
//! `claim_writer` preempts the shared plant's write-ownership claim and
//! a `release_writer` hands it back, leaving the field `unclaimed` —
//! the fail-closed window where no attachment may write or step. The
//! tracking peer's per-scan claim probe must surface the contracted
//! `field_claim: "unclaimed"` on `GET /role` within five scans while
//! the peer stays `standby`/`tracking`: the observation seizes no
//! claim, opens no gate, fires no promotion, and journals nothing, and
//! `POST /promote` remains the documented recovery.
//!
//! The active is never scanned past the preemption here: its frozen
//! run keeps serving checkpoints stamped field-owning, so the standby's
//! tracked line still reports `tracking` while the field's own
//! arbitration answers "no owner stands" — the divergence this signal
//! exists to name, which the checkpoint contract cannot see.

use dcs_core::{FieldClaim, IoDriver, JournalEntry, JournalEvent, Role, StandbySync, Value};
use dcs_monitor::MonitorClient;
use dcs_sim_net::RemoteDriver;
use std::path::Path;

mod support;

use support::{SimTcp, controller_model, image_value, spawn_controller, spawn_plant};

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
/// The contract's bound: the unclaimed field must report within five
/// scans of the release.
const REPORT_BOUND: u64 = 5;

/// What one scripted run leaves for the determinism check — the
/// standby's last published snapshot and its full journal. Two
/// identical runs must produce identical records.
struct RunRecord {
    snapshot: serde_json::Value,
    journal: Vec<JournalEntry>,
}

/// One full scripted run: spawn the pair on a shared plant, preempt and
/// release the field claim through a foreign attachment, drive only the
/// tracking peer through the report bound, then recover by `POST
/// /promote`. Returns the standby's closing snapshot and journal.
fn run_once(tag: &str) -> RunRecord {
    let dir =
        std::env::temp_dir().join(format!("dcs-unclaimed-field-{}-{tag}", std::process::id()));
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
    let standby_process = spawn_controller(
        &model,
        &["--standby".to_string(), active_process.addr.to_string()],
        DT,
    );
    let active = MonitorClient::new(active_process.addr);
    let standby = MonitorClient::new(standby_process.addr);

    // Converge the standby on the claimed field: the probe answers
    // `held` while an owner stands.
    for _ in 0..4 {
        standby.advance(1).unwrap();
        active.advance(1).unwrap();
    }
    let report = standby.role().unwrap();
    assert!(
        matches!(report.sync, Some(StandbySync::Tracking { .. })),
        "the standby never converged: {report:?}"
    );
    assert_eq!(
        report.field_claim,
        Some(FieldClaim::Held),
        "the owned field must report held: {report:?}"
    );

    // The trigger: a foreign attachment preempts the active's claim and
    // hands the field back unclaimed. The active is never driven past
    // the preemption — it still reports `active` and its checkpoints
    // still stamp a field owner, so the tracked line converges while
    // the field's own arbitration stands unclaimed.
    field.claim_writer(FOREIGN).unwrap();
    field.release_writer().unwrap();

    // Within five driven scans the probe must report the contracted
    // signal — the role stays `standby`, the tracked line stays
    // `tracking`, and nothing promotes.
    let mut reported = None;
    for _ in 0..REPORT_BOUND {
        standby.advance(1).unwrap();
        let report = standby.role().unwrap();
        assert_eq!(
            report.role,
            Role::Standby,
            "the observation must never move the role: {report:?}"
        );
        assert!(
            matches!(report.sync, Some(StandbySync::Tracking { .. })),
            "the frozen owner keeps stamping its claim: {report:?}"
        );
        if report.field_claim == Some(FieldClaim::Unclaimed) {
            reported = Some(report);
        }
    }
    assert!(
        reported.is_some(),
        "the unclaimed field must report within {REPORT_BOUND} scans"
    );
    let report = standby.role().unwrap();
    assert_eq!(
        report.field_claim,
        Some(FieldClaim::Unclaimed),
        "the signal must stand once observed: {report:?}"
    );

    // The probe seized nothing: a fresh attachment's observation still
    // answers unclaimed — the field stays fail-closed — and the write
    // gate never opened: a standby's staged writes drop at the closed
    // gate without reaching the driver, so one failed write would prove
    // it opened.
    assert_eq!(
        field.probe_writer().unwrap(),
        FieldClaim::Unclaimed,
        "reporting an unclaimed field must not seize it"
    );
    let snapshot = standby.advance(1).unwrap();
    assert_eq!(
        snapshot.io_health.failed_writes, 0,
        "the closed gate dropped every staged write: {snapshot:?}"
    );

    // The observation journaled nothing: the claim surface was asked,
    // never told — no fencing loss, no orphan transition, no role
    // change may appear on the tracking peer's durable record.
    let journal = standby.journal(0).unwrap();
    assert!(
        !journal.iter().any(|entry| matches!(
            entry.event,
            JournalEvent::FieldClaimLost { .. }
                | JournalEvent::FieldOrphaned { .. }
                | JournalEvent::RoleChanged { .. }
        )),
        "the probe must journal no claim event: {journal:?}"
    );

    // The recovery is the documented one: `POST /promote` claims the
    // unclaimed field unconditionally and lifts the gate — the probe's
    // report never did either.
    assert_eq!(standby.promote().unwrap().role, Role::Promoting);
    let snapshot = standby.advance(1).unwrap();
    let report = standby.role().unwrap();
    assert_eq!(report.role, Role::Active, "{report:?}");
    assert_eq!(
        report.field_claim,
        Some(FieldClaim::Held),
        "the promoted peer's own claim reports held: {report:?}"
    );
    assert_eq!(
        field.probe_writer().unwrap(),
        FieldClaim::Held,
        "the promotion's claim must stand at the field"
    );
    assert_eq!(
        field.read(VALVE).unwrap().value,
        image_value(&snapshot, VALVE),
        "the promoted peer's write must reach the field"
    );

    let record = RunRecord {
        snapshot: serde_json::to_value(&snapshot).unwrap(),
        journal: standby.journal(0).unwrap(),
    };
    let _ = std::fs::remove_dir_all(&dir);
    record
}

/// The acceptance scenario: preempt + release leaves the field
/// unclaimed, the tracking peer reports the contracted signal inside
/// the bound while staying `standby`/`tracking`, no claim is seized and
/// no journal event lands, and `POST /promote` recovers the pair. The
/// run executes twice — identical scripts must produce identical
/// snapshots and journals.
#[test]
fn the_tracking_peer_reports_the_unclaimed_field_without_seizing_it() {
    let first = run_once("a");
    let second = run_once("b");
    assert_eq!(
        first.snapshot, second.snapshot,
        "identical runs must produce identical snapshots"
    );
    assert_eq!(
        first.journal, second.journal,
        "identical runs must produce identical journals"
    );
}
