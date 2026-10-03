//! The QA finding `field-claim-preemption-diverged-lockout`: a
//! sanctioned foreign `claim_writer` preempted the field's
//! write-ownership claim while a field `Out` stood energized
//! (`p101-cmd=true`). The superseded owner's first fenced write
//! demoted it in place — the documented `field_claim_lost` path — but
//! the field froze with the output energized, fenced to the
//! preempting token: a dead token keeps fencing by design. Every
//! tracking peer then convicted the stale field: the staged-vs-field
//! comparison matched the released staged image against the frozen
//! energized read and both peers reported `standby`/`diverged`;
//! `POST /promote` answered `409 not_converged` on either peer, and
//! the only recovery was an external field rewrite or a restart.
//!
//! The contract the finding demands: the pair's documented
//! promote-based recovery must survive the wedge — a frozen stale
//! field is not genuine process divergence, and a tracking peer must
//! be able to take the field back. The shipped shape: a serving run
//! that owns no field writes stamps `source_owns_field: false` on its
//! checkpoints, and the apply re-adjudicates the line as `orphaned` —
//! promotable on the same convergence proof `tracking` stands on —
//! superseding even a standing `diverged` verdict, because no
//! staged-vs-field comparison on an ownerless stream can ever clear
//! one and the promotion the verdict blocks is exactly the write that
//! converges the field. The promotion's conditional claim then
//! preempts the foreign token — released or not: the field records a
//! tool's claim as a tool's, and only a live *controller's* unyielded
//! claim is the incumbent an orphaned promote must refuse.
//!
//! The scripted reproduction runs the QA rig's own shape: one
//! `dcs-plant-server` serving the demo pump station, two
//! `dcs-controller --driven --remote` peers, the field `Out`s
//! energized, then a foreign `claim_writer` plus the trip that moves
//! the staged image — asserted through the same field seam the lane's
//! plant tooling uses. The same-tick pairing means a transient
//! `diverged` on the last owned checkpoint is possible; what the
//! defect turned permanent — promotion refused on every peer while
//! the output stood energized — is what must never report.

use dcs_core::{IoDriver, IoError, JournalEvent, PointId, Role, StandbySync, Value};
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
/// The promoted peer's first field-owning scans the leg gives the
/// re-issued release to land and the tracker to reconverge.
const OWNER_TICKS: u64 = 4;

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
/// The foreign attachment's claim token — any token neither
/// controller generated stands in for the QA reproduction's rogue
/// claim. The claim outlives its holder by design, so no release
/// ever frees it for the pair.
const FOREIGN: u64 = 0xF0_21_61_6E;

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
/// the foreign preemption interrupts.
fn full_demand(snapshot: &dcs_core::TelemetrySnapshot) -> bool {
    matches!(image_value(snapshot, DEMAND), Value::Int(demand) if demand >= 2)
        && matches!(image_value(snapshot, STAGED), Value::Int(staged) if staged >= 2)
        && image_value(snapshot, CMD_A) == Value::Bool(true)
        && image_value(snapshot, CMD_B) == Value::Bool(true)
        && image_value(snapshot, HIGH_LEVEL) == Value::Bool(true)
}

/// The reproduction's shared launch: one plant server, the launched
/// active claiming the field under its startup token, and the
/// `--standby` peer tracking it — driven to the standing two-pump
/// demand so both field commands stand energized, exactly the
/// finding's precondition. The returned field attachment is the
/// rogue actor: its `ensure_writer` join seeds the inflow under the
/// owner's claim, then its `claim_writer` takes the field for the
/// foreign token.
struct Pair {
    _plant: support::Spawned,
    _active_process: support::Spawned,
    _standby_process: support::Spawned,
    active: MonitorClient,
    standby: MonitorClient,
    field: RemoteDriver,
}

fn converged_pair() -> Pair {
    let plant = spawn_plant(Path::new(STATION), Path::new(DYNAMICS));
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
    // standing two-pump demand — both commands delivered to the
    // field. The lane's forcing write: `inflow` driven above the
    // declared high setpoint under the owner's writer claim.
    field.ensure_writer(token).unwrap();
    field.write(INFLOW, Value::Float(5.0)).unwrap();
    let mut reached = false;
    for _ in 0..DEMAND_BOUND {
        standby.advance(1).unwrap();
        if full_demand(&active.advance(1).unwrap()) {
            reached = true;
            break;
        }
    }
    assert!(reached, "the pump group never held a full demand");
    assert!(
        matches!(
            standby.role().unwrap().sync,
            Some(StandbySync::Tracking { .. })
        ),
        "the standby never converged"
    );
    // The defect's precondition, asserted rather than assumed: both
    // field commands stand energized on the plant under the launched
    // active's claim.
    assert_eq!(field_value(&field, CMD_A), Value::Bool(true));
    assert_eq!(field_value(&field, CMD_B), Value::Bool(true));

    Pair {
        _plant: plant,
        _active_process: active_process,
        _standby_process: standby_process,
        active,
        standby,
        field,
    }
}

/// The defect's trigger, shared by both recovery legs: the foreign
/// attachment preempts the field claim and drops the power-fail
/// contact under it — the moved image the QA run's staged peers
/// convicted against the frozen field — then the ex-owner's first
/// fenced write demotes it in place. Asserts the documented
/// `field_claim_lost` transition journaled on the ex-owner, and
/// returns with the field frozen energized under a claim no peer
/// holds.
fn preempt_and_fence(pair: &Pair) {
    // The sanctioned preemption path: a `claim_writer` under a token
    // neither controller generated takes the field's write
    // arbitration unconditionally — the claim the QA lane's rogue
    // attachment asserted, and the one a dead token keeps standing.
    pair.field.claim_writer(FOREIGN).unwrap();
    // The trip lands under the foreign hold — the image-moves half of
    // the reproduction: both peers' staged images release while the
    // field keeps the last energized commands, the stale-vs-staged
    // mismatch the old build wedged on.
    pair.field.write(POWER_FAIL, Value::Bool(true)).unwrap();

    // The superseded owner's next field write is fenced: the
    // documented `field_claim_lost` path demotes it in place — never
    // an exit, never a second writer.
    pair.active.advance(1).unwrap();
    assert_eq!(
        pair.active.role().unwrap().role,
        Role::Demoting,
        "a fenced write must demote the superseded owner in place"
    );
    assert!(
        pair.active
            .journal(0)
            .unwrap()
            .iter()
            .any(|entry| matches!(entry.event, JournalEvent::FieldClaimLost { .. })),
        "the fencing loss must journal on the superseded owner"
    );
    // The field froze energized — the plant-side hazard the finding
    // names: outputs standing with no controller owning them.
    assert_eq!(field_value(&pair.field, CMD_A), Value::Bool(true));
    assert_eq!(field_value(&pair.field, CMD_B), Value::Bool(true));
}

/// The ownerless window, shared by both legs: the release propagates
/// through both peers' quiesced staged images while the field stays
/// frozen — the single-writer rule forbids either standby from
/// writing. Where the old build's staged-vs-field comparison wedged
/// every peer `diverged` forever, the ownerless stream lands the
/// named `orphaned` state — promotable, journaled. `diverged` must
/// never report at all here: the only owned checkpoints this stream
/// can still carry predate the trip, where staged and field still
/// agree — every later apply is the ownerless kind the staged
/// comparison cannot honestly pair, so the verdict has no honest
/// `field` side to convict on.
fn ownerless_window(pair: &Pair) {
    let mut staged = None;
    for tick in 1..=ORPHAN_TICKS {
        staged = Some(pair.standby.advance(1).unwrap());
        pair.active.advance(1).unwrap();
        assert_eq!(
            field_value(&pair.field, CMD_A),
            Value::Bool(true),
            "tick {tick}: no standby may write the field"
        );
        assert_eq!(field_value(&pair.field, CMD_B), Value::Bool(true));
        for (peer, name) in [(&pair.active, "demoted"), (&pair.standby, "standby")] {
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
    for (peer, name) in [(&pair.active, "demoted"), (&pair.standby, "standby")] {
        let report = peer.role().unwrap();
        assert!(
            matches!(report.sync, Some(StandbySync::Orphaned { .. })),
            "the {name} peer must surface the unowned line as \
             orphaned — never the promote-blocking diverged the \
             defect produced: {report:?}"
        );
        assert!(
            peer.journal(0)
                .unwrap()
                .iter()
                .any(|entry| matches!(entry.event, JournalEvent::FieldOrphaned { .. })),
            "the orphan transition must journal on the {name} peer"
        );
    }
}

/// The promoted peer's first field-owning scans re-issue the release
/// the preempted owner abandoned: the field commands read released
/// under the standing power-fail trip, the foreign attachment finds
/// its writes fenced under the new claim, and the other peer
/// reconverges `tracking` on the restored owner.
fn assert_released_takeover(pair: &Pair, owner: &MonitorClient, tracker: &MonitorClient) {
    for _ in 0..OWNER_TICKS {
        tracker.advance(1).unwrap();
        owner.advance(1).unwrap();
    }
    assert_eq!(
        field_value(&pair.field, CMD_A),
        Value::Bool(false),
        "the promoted peer must write the pending release"
    );
    assert_eq!(
        field_value(&pair.field, CMD_B),
        Value::Bool(false),
        "the promoted peer must write the pending release"
    );
    // The claim moved: the foreign attachment that fenced the pair is
    // itself fenced now — the promotion's unconditional claim
    // preempted its token.
    assert!(
        matches!(
            pair.field.write(CMD_A, Value::Bool(true)),
            Err(IoError::Fenced(point)) if point == CMD_A
        ),
        "the foreign claim must be superseded by the promotion's own"
    );
    // The pair closes on exactly one active: the promoted peer
    // settles, the other peer reconverges `tracking` on its
    // checkpoints — no peer left diverged, no field write abandoned.
    assert_eq!(owner.role().unwrap().role, Role::Active);
    let report = tracker.role().unwrap();
    assert_eq!(report.role, Role::Standby, "{report:?}");
    assert!(
        matches!(report.sync, Some(StandbySync::Tracking { .. })),
        "the tracking peer must reconverge on the new owner: {report:?}"
    );
}

/// The finding's own reproduction: the field `Out`s stand energized,
/// a foreign `claim_writer` preempts the arbitration, the staged
/// images move while the field freezes — and `POST /promote` on the
/// tracking peer, refused `409 not_converged` on the QA run, now
/// answers `promoting`. Its unconditional claim preempts the foreign
/// token and its first field-owning scans write the released
/// commands through.
#[test]
fn preempted_claim_with_energized_out_recovers_through_the_tracking_peers_promotion() {
    let pair = converged_pair();

    preempt_and_fence(&pair);
    ownerless_window(&pair);

    // The promotion the defect refused: `POST /promote` on the
    // tracking peer answers `promoting`, never `409 not_converged`.
    let (status, body) = pair.standby.request("POST", "/promote", None).unwrap();
    assert_eq!(status, 200, "the promote must not be refused: {body}");
    assert_eq!(
        serde_json::from_str::<dcs_core::RoleReport>(&body)
            .unwrap()
            .role,
        Role::Promoting
    );

    assert_released_takeover(&pair, &pair.standby, &pair.active);
}

/// The symmetric leg the QA run also tried: `POST /promote` on the
/// *ex-owner* — the superseded peer that `field_claim_lost` demoted —
/// refused `409` on the defect build. Its orphan state is promotable
/// on the same convergence proof, and its claim preempts the foreign
/// token exactly as the tracking peer's does: the documented recovery
/// does not depend on which peer the operator asks.
#[test]
fn preempted_claim_also_recovers_through_the_demoted_ex_owners_promotion() {
    let pair = converged_pair();

    preempt_and_fence(&pair);
    ownerless_window(&pair);

    // The ex-owner probes its released claim every orphan cycle —
    // refused `fenced` while the foreign token stands, so it never
    // preempts — then `POST /promote` runs the unconditional claim
    // the recovery needs.
    let (status, body) = pair.active.request("POST", "/promote", None).unwrap();
    assert_eq!(status, 200, "the promote must not be refused: {body}");
    assert_eq!(
        serde_json::from_str::<dcs_core::RoleReport>(&body)
            .unwrap()
            .role,
        Role::Promoting
    );

    assert_released_takeover(&pair, &pair.active, &pair.standby);
}
