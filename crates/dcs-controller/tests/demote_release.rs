//! The QA finding `demote-during-interlock-release-leaves-field-
//! energized-and-wedges-pair`: the pump station's power-fail release
//! propagates over several driven scans (power-fail -> power-ok ->
//! availability -> gate -> the `p10x-cmd` field write), and a legal
//! `POST /demote` landing inside that window stepped the field owner
//! down before the release write reached the plant. The field kept
//! the last energized outputs while both peers' staged images
//! computed the released ones: the staged-vs-field comparison flagged
//! both `diverged`, `POST /promote` answered `409 not_converged`
//! forever, and the pair stood with no active able to write the
//! release — healed only by writing the field by hand.
//!
//! The contract the finding demands: a demote during a tripped
//! interlock must either flush the pending released outputs before
//! stepping down or let the promoted peer converge and write them —
//! the field must never stand energized under a standing power-fail
//! trip with no controller able to take over. The shipped shape is
//! the second half: the demoted owner's checkpoints stamp
//! `source_owns_field: false`, so both tracking pulls land the named
//! `orphaned` sync state — promotable on the same convergence proof
//! `tracking` stands on — instead of the staged-vs-field divergence
//! the abandoned write used to produce. The promoted successor's
//! first field-owning scan then writes the released commands the
//! demoted run could not.
//!
//! The scripted reproduction runs the QA rig's own shape: one
//! `dcs-plant-server` serving the demo pump station, two
//! `dcs-controller --driven --remote` peers, and the field-side
//! `power-fail` write issued under the owner's writer claim exactly
//! as the lane's plant tooling does.

use dcs_core::{IoDriver, JournalEvent, PointId, Role, StandbySync, Value};
use dcs_monitor::MonitorClient;
use dcs_sim_net::RemoteDriver;
use std::path::Path;

mod support;

use support::{image_value, spawn_controller_logged, spawn_plant};

/// The shared plant's model — the QA rig's own pump station: the same
/// checked-in document the lane's plant server and both controllers
/// run. Its `p10x-cmd` outputs are the only field `Out` points, so
/// the staged-output divergence check covers exactly the commands the
/// finding watched freeze.
const STATION: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/pump_station.json"
);
/// The plant-side physics the lane runs — the well refilling toward
/// the standing two-pump demand the trip interrupts.
const DYNAMICS: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../qa_lane/fixtures/pump_station_dynamics.json"
);
const DT: &str = "0.1";
/// The driven-tick bound on reaching full demand — the lane's rig
/// reaches it in about ten.
const DEMAND_BOUND: u64 = 48;
/// The driven ticks the ownerless window gets to surface the named
/// state — longer than the release propagation itself.
const ORPHAN_TICKS: u64 = 6;
/// The promoted successor's first field-owning scans the leg gives
/// the re-issued release to land.
const OWNER_TICKS: u64 = 3;

const POWER_FAIL: PointId = PointId(120);
const CMD_A: PointId = PointId(100);
const CMD_B: PointId = PointId(101);
const DEMAND: PointId = PointId(204);
const STAGED: PointId = PointId(211);
const HIGH_LEVEL: PointId = PointId(215);
/// The dynamics' declared forcing input — the field point the QA
/// lane drives high to fill the well to the standing demand inside
/// the demand bound, written through the same field seam the trip's
/// `power-fail` write uses.
const INFLOW: PointId = PointId(12);

/// The owner token the launched active's startup claim reported —
/// parsed from the spawn preamble's `owner token <n>` line, the same
/// claim the QA leg's field writes join through `ensure_writer`.
fn owner_token(preamble: &[String]) -> u64 {
    preamble
        .iter()
        .find_map(|line| {
            line.split("owner token ")
                .nth(1)
                .and_then(|rest| rest.split_whitespace().next()?.parse().ok())
        })
        .expect("the launched active reports its writer-claim owner token")
}

/// One field `Out` read through the plant protocol — the standing
/// value the last field-owning scan wrote; nothing else moves it.
fn field_value(field: &RemoteDriver, point: PointId) -> Value {
    field.read(point).unwrap().value
}

/// Whether the pair's served images still carry the standing
/// two-pump demand: `demand` and `staged` at two, both field commands
/// energized, the high-level condition up — the healthy precondition
/// the power-fail trip interrupts.
fn full_demand(snapshot: &dcs_core::TelemetrySnapshot) -> bool {
    matches!(image_value(snapshot, DEMAND), Value::Int(demand) if demand >= 2)
        && matches!(image_value(snapshot, STAGED), Value::Int(staged) if staged >= 2)
        && image_value(snapshot, CMD_A) == Value::Bool(true)
        && image_value(snapshot, CMD_B) == Value::Bool(true)
        && image_value(snapshot, HIGH_LEVEL) == Value::Bool(true)
}

/// The reproduction: full demand, the field-side `power-fail` write,
/// then `POST /demote` inside the release-propagation window — before
/// any driven scan could write the released commands. The demoted
/// owner abandons the in-flight write: the field keeps the energized
/// outputs while no peer owns them — the defect's precondition,
/// asserted rather than assumed — but the line reports `orphaned`,
/// never `diverged`, so `POST /promote` on the peer takes the field
/// and its first owning scans write the release through. The demoted
/// peer then reconverges on the restored owner.
#[test]
fn demote_during_power_fail_release_lets_the_successor_write_it() {
    let dir = std::env::temp_dir().join(format!("dcs-demote-release-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();

    let plant = spawn_plant(Path::new(STATION), Path::new(DYNAMICS));

    // The reproduction's launch shape: the active names no peer; the
    // standby tracks it by `--standby`, announcing its own monitor on
    // every pull so the demoted run knows where its successor lives.
    // Both run `--remote`: every field access is the shared plant's.
    let remote = ["--remote".to_string(), plant.addr.to_string()];
    let (active_process, preamble) = spawn_controller_logged(Path::new(STATION), &remote, DT);
    let token = owner_token(&preamble);
    let mut standby_args = remote.to_vec();
    standby_args.extend(["--standby".to_string(), active_process.addr.to_string()]);
    let standby_process = spawn_controller_logged(Path::new(STATION), &standby_args, DT).0;
    let active = MonitorClient::new(active_process.addr);
    let standby = MonitorClient::new(standby_process.addr);
    let field = RemoteDriver::connect(plant.addr).unwrap();

    // Converge the standby, then drive the simulated well to the
    // standing two-pump demand — both commands delivered to the field.
    // The lane's forcing write: `inflow` driven above the declared
    // high setpoint under the owner's writer claim, so the well
    // reaches the high-level demand inside the bound.
    field.ensure_writer(token).unwrap();
    field.write(INFLOW, Value::Float(5.0)).unwrap();
    let mut owner = None;
    for _ in 0..DEMAND_BOUND {
        standby.advance(1).unwrap();
        let snapshot = active.advance(1).unwrap();
        if full_demand(&snapshot) {
            owner = Some(snapshot);
            break;
        }
    }
    let owner = owner.expect("the pump group never held a full demand");
    assert!(
        matches!(
            standby.role().unwrap().sync,
            Some(StandbySync::Tracking { .. })
        ),
        "the standby never converged"
    );
    assert_eq!(field_value(&field, CMD_A), Value::Bool(true));
    assert_eq!(field_value(&field, CMD_B), Value::Bool(true));

    // The trip: the power-fail contact lands through the plant
    // protocol inside the owner's standing writer claim. The field
    // commands still read energized — the release lands on the driven
    // scan sequence, never on the write itself.
    field.ensure_writer(token).unwrap();
    field.write(POWER_FAIL, Value::Bool(true)).unwrap();
    assert_eq!(field_value(&field, CMD_A), Value::Bool(true));
    assert_eq!(field_value(&field, CMD_B), Value::Bool(true));

    // The defect's trigger: a legal operator demote inside the
    // release-propagation window — the owner's gate closes before its
    // released-command write ever reached the plant.
    assert_eq!(active.demote().unwrap().role, Role::Demoting);

    // The ownerless window: the release propagates through both
    // peers' quiesced staged images while the field stays frozen —
    // the single-writer rule forbids either standby from writing.
    // Where the old build compared staged-released against
    // field-energized and wedged both peers `diverged`, every pull now
    // lands the named `orphaned` state — promotable, journaled. A
    // tracking pull can still be in flight or applying a pre-demotion
    // fetch on the first ownerless cycle, so the assertion is what
    // the defect violated: the promote-blocking `diverged` never
    // reports, and `orphaned` stands inside the window.
    let mut staged = None;
    for tick in 1..=ORPHAN_TICKS {
        staged = Some(standby.advance(1).unwrap());
        active.advance(1).unwrap();
        assert_eq!(
            field_value(&field, CMD_A),
            Value::Bool(true),
            "tick {tick}: no standby may write the field"
        );
        assert_eq!(field_value(&field, CMD_B), Value::Bool(true));
        for (peer, name) in [(&active, "demoted"), (&standby, "standby")] {
            let report = peer.role().unwrap();
            assert!(
                !matches!(report.sync, Some(StandbySync::Diverged { .. })),
                "tick {tick}: the {name} peer reported the \
                 promote-blocking diverged the defect produced: \
                 {report:?}"
            );
        }
    }
    // The defect's evidence, asserted rather than assumed: the
    // quiesced peers' staged images did compute the released commands
    // while the field stood energized — the staged-vs-field mismatch
    // the old build's divergence gate wedged on.
    let staged = staged.unwrap();
    assert_eq!(image_value(&staged, CMD_A), Value::Bool(false));
    assert_eq!(image_value(&staged, CMD_B), Value::Bool(false));
    for (peer, name) in [(&active, "demoted"), (&standby, "standby")] {
        let report = peer.role().unwrap();
        assert!(
            matches!(report.sync, Some(StandbySync::Orphaned { .. })),
            "the {name} peer must surface the unowned line as \
             orphaned — never the promote-blocking diverged the \
             defect produced: {report:?}"
        );
    }
    assert!(
        standby
            .journal(0)
            .unwrap()
            .iter()
            .any(|entry| matches!(entry.event, JournalEvent::FieldOrphaned { .. })),
        "the orphan transition must journal on the tracking peer"
    );

    // The promotion the defect refused: `POST /promote` on the peer
    // answers `promoting`, never `409 not_converged`.
    let (status, body) = standby.request("POST", "/promote", None).unwrap();
    assert_eq!(status, 200, "the promote must not be refused: {body}");
    assert_eq!(
        serde_json::from_str::<dcs_core::RoleReport>(&body)
            .unwrap()
            .role,
        Role::Promoting
    );

    // The successor's first field-owning scans re-issue the release
    // the demoted owner abandoned: the field commands read released
    // under the standing power-fail trip.
    for _ in 0..OWNER_TICKS {
        active.advance(1).unwrap();
        standby.advance(1).unwrap();
    }
    assert_eq!(
        field_value(&field, CMD_A),
        Value::Bool(false),
        "the promoted successor must write the pending release"
    );
    assert_eq!(
        field_value(&field, CMD_B),
        Value::Bool(false),
        "the promoted successor must write the pending release"
    );

    // The pair closes on exactly one active: the promoted peer
    // settles, the demoted peer reconverges `tracking` on its
    // checkpoints — no peer left diverged, no field write abandoned.
    assert_eq!(standby.role().unwrap().role, Role::Active);
    let report = active.role().unwrap();
    assert_eq!(report.role, Role::Standby, "{report:?}");
    assert!(
        matches!(report.sync, Some(StandbySync::Tracking { .. })),
        "the demoted peer must reconverge on the new owner: {report:?}"
    );

    let _ = std::fs::remove_dir_all(&dir);
    drop(owner);
}
