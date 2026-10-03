//! The QA finding `demoted-peer-adopts-standby-line-into-promotable-
//! stale-island`: one active and two standbys share a simulated field.
//! Both standbys announce themselves through their checkpoint pulls;
//! demoting the active adopts the sibling standby — a source that
//! merely replays the demoted run's own line back to it — and
//! promoting the third controller used to leave the demoted pair
//! orphaned-tracking each other forever, an island whose stale image a
//! later `POST /promote` could still slam over the live incumbent's
//! applied state and energized outputs.
//!
//! The fix closes both halves. Served checkpoints propagate the line's
//! field-owner address (`line_owner`) and the orphan-resolution probe
//! follows it — plus the recorded announcers — onto the endpoint that
//! verifiably serves the line *as its owner*, so the demoted pair
//! reconverges on the real owner instead of wedging. And an orphaned
//! run's promotion claims *conditionally*: the field's arbitration
//! refuses while a live *controller* attachment holds a different
//! owner's unyielded claim, so a stale island can never preempt the
//! incumbent — the `409 field_claim_failed` this fixture drives on the
//! islanded peer before its re-resolution lands.

use dcs_core::{IoDriver, JournalEvent, Role, StandbySync, Value};
use dcs_monitor::MonitorClient;
use dcs_sim_net::RemoteDriver;
use std::path::Path;

mod support;

use support::{SimTcp, controller_model, image_value, spawn_controller, spawn_plant};

/// The shared plant's model — the same tank loop the other
/// switchover rigs run.
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
const SEED: u64 = 499_901;
const SETPOINT: dcs_core::PointId = dcs_core::PointId(11);
const VALVE: dcs_core::PointId = dcs_core::PointId(20);

/// Whether `report` surfaces the tracked line's missing field owner.
fn reports_orphaned(report: &dcs_core::RoleReport) -> bool {
    report.role == Role::Standby && matches!(report.sync, Some(StandbySync::Orphaned { .. }))
}

/// Whether `report` shows a standby reconverged on a field-owning
/// line.
fn reports_tracking(report: &dcs_core::RoleReport) -> bool {
    report.role == Role::Standby && matches!(report.sync, Some(StandbySync::Tracking { .. }))
}

/// The reproduction: `a` active with `b` and `c` both `--standby a` on
/// the shared remote plant. `POST a/demote` adopts the last-announcing
/// sibling; `POST b/promote` takes the field. The demoted pair used to
/// orphaned-track each other forever while `b` owned — and an island
/// member's `POST /promote` returned 200, silently reverting `b`'s
/// applied state and cutting its energized outputs.
///
/// Now the island breaks two ways, both asserted: an islanded
/// `POST /promote` is refused `field_claim_failed` — the conditional
/// claim sees `b`'s live unyielded claim and never preempts — and the
/// demoted pair follows the line's propagated owner name onto `b`,
/// reconverging `tracking` rather than looping orphaned forever.
#[test]
fn demoted_pair_reconverges_and_an_islanded_promote_is_refused() {
    let dir = std::env::temp_dir().join(format!("dcs-stale-island-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();

    let plant = spawn_plant(Path::new(PLANT_MODEL), Path::new(PLANT_DYNAMICS));
    let model = controller_model(
        &dir,
        "triple.json",
        MODEL_SOURCE,
        plant.addr,
        SimTcp::PerDevice,
    )
    .0;

    let field = RemoteDriver::connect(plant.addr).unwrap();
    field.ensure_writer(SEED).unwrap();
    field.write(SETPOINT, Value::Float(50.0)).unwrap();
    field.release_writer().unwrap();

    // The reproduction's topology: `a` launched active naming no peer;
    // `b` and `c` each `--standby a` — every tracking pull announces
    // its own monitor on `a`'s checkpoint endpoint.
    let a_process = spawn_controller(&model, &[], DT);
    let b_process = spawn_controller(
        &model,
        &["--standby".to_string(), a_process.addr.to_string()],
        DT,
    );
    let c_process = spawn_controller(
        &model,
        &["--standby".to_string(), a_process.addr.to_string()],
        DT,
    );
    let a = MonitorClient::new(a_process.addr);
    let b = MonitorClient::new(b_process.addr);
    let c = MonitorClient::new(c_process.addr);

    // Converge both standbys and let `a` scan alongside — each pull
    // announces its puller, so `a` holds both announcers as demotion
    // candidates.
    for _ in 0..4 {
        b.advance(1).unwrap();
        c.advance(1).unwrap();
        a.advance(1).unwrap();
    }
    assert!(
        reports_tracking(&b.role().unwrap()) && reports_tracking(&c.role().unwrap()),
        "both standbys must converge before the switchover"
    );

    // `POST a/demote`: every announcer is verified — both serve this
    // line as mere trackers today — so the demotion adopts the newest
    // standby-line candidate and journals the adoption.
    assert_eq!(a.demote().unwrap().role, Role::Demoting);
    let journal = a.journal(0).unwrap();
    assert!(
        journal
            .iter()
            .any(|entry| matches!(entry.event, JournalEvent::TrackingSourceAdopted { .. })),
        "the announced-source adoption must journal: {journal:?}"
    );

    // `POST b/promote` takes the field — the unconditional claim a
    // converged standby's deliberate takeover runs — and `b` settles
    // the line's owner.
    assert_eq!(b.promote().unwrap().role, Role::Promoting);
    b.advance(1).unwrap();
    assert_eq!(b.role().unwrap().role, Role::Active);

    // The island forms: `c` pulls `a`'s demoted checkpoints — the
    // tracked run owns nothing — and its probe finds no owner yet:
    // `a` has not propagated a `line_owner`, `c`'s only announcer is
    // `a` itself. `c` reports orphaned.
    c.advance(1).unwrap();
    assert!(
        reports_orphaned(&c.role().unwrap()),
        "the adopted sibling must surface the island before it can resolve"
    );

    // The defect's stomp, refused: `POST c/promote` while `c` is still
    // orphaned runs the conditional claim — `b`'s live attachment
    // holds its unyielded claim, so the field's own arbitration says
    // no. `c` stays a gated standby; `b`'s applied state and outputs
    // are untouched.
    let error = c.promote().unwrap_err();
    assert!(
        error.to_string().contains("field_claim_failed"),
        "an islanded promote must be refused, got: {error}"
    );
    assert_eq!(b.role().unwrap().role, Role::Active);

    // Re-resolution: `a`'s orphan cycle probes the announced set,
    // finds `b` serving this line as its owner, and re-targets; `c`
    // follows the `line_owner` `a`'s served checkpoints now propagate.
    // A handful of cycles converges the whole pair onto `b`.
    for _ in 0..6 {
        a.advance(1).unwrap();
        c.advance(1).unwrap();
        b.advance(1).unwrap();
    }
    let a_report = a.role().unwrap();
    let c_report = c.role().unwrap();
    assert!(
        reports_tracking(&a_report),
        "the demoted peer must reconverge on the real owner, got {a_report:?}"
    );
    assert!(
        reports_tracking(&c_report),
        "the adopted sibling must reconverge on the real owner, got {c_report:?}"
    );
    assert_eq!(b.role().unwrap().role, Role::Active);

    // Nothing reverted: the incumbent's writes still reach the field
    // and no island member's stale image ever overwrote them.
    let snapshot = b.advance(1).unwrap();
    assert_eq!(
        field.read(VALVE).unwrap().value,
        image_value(&snapshot, VALVE),
        "the incumbent's write must still reach the field"
    );
    // A fresh islanded-promote attempt now meets a converged standby:
    // the deliberate takeover path is legal again — the live-owner
    // refusal only binds while the run's own evidence is orphaned.
    // `c` may still adopt commands through `b`'s checkpoints.
    assert!(
        reports_tracking(&c.role().unwrap()),
        "the pair stays converged on the owner"
    );

    let _ = std::fs::remove_dir_all(&dir);
}
