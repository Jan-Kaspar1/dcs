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
//! What that left is the *window* itself, which the follow-up
//! finding `demote-during-release-orphan-window` recorded: bounded
//! only by operator action. #828 fixes that half as a declared bound
//! on the energized orphan window, and the two legs below script both
//! routes a promotion converges over — the requested `promote` the
//! operator issues, and the `--auto-promote` failover budget a
//! deployment arms, where every orphaned apply counts the heartbeat
//! miss that self-promotes at its budget. The second leg is the one
//! that makes the bound independent of an operator: it promotes
//! nothing itself, and asserts the takeover journals with the
//! `failover` origin so a build recovering by request could not pass
//! as one.
//!
//! The scripted reproduction runs the QA rig's own shape: one
//! `dcs-plant-server` serving the demo pump station, two
//! `dcs-controller --driven --remote` peers, and the field-side
//! `power-fail` write issued under the owner's writer claim exactly
//! as the lane's plant tooling does.

use dcs_core::{IoDriver, JournalEvent, PointId, Role, StandbySync, SwitchOrigin, Value};
use dcs_monitor::MonitorClient;
use dcs_sim_net::RemoteDriver;
use std::path::Path;

mod support;

use support::{Spawned, image_value, spawn_controller_logged, spawn_plant};

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
/// The failover budget the armed leg's standby carries — a deployment's
/// declared `failover_budget`, in driven ticks, the same flag the
/// reference plant's manifest records.
const BUDGET: &str = "3";
/// **The declared orphan-window bound** for the armed pair, in the
/// harness's own unit — one tracking peer's scan plus one field owner's:
/// the failover budget the armed standby's budget-th orphan
/// self-promotion fires at, the promotion's own settling scan, and the
/// promoted peer's first field-owning scan that writes the abandoned
/// release, with the pair's role walk inside the margin. The field's
/// energization may not outlive it. That is the bound #828's residual
/// risk names, the one that made the window "bounded by operator
/// action" while only an operator could close it: past it the field
/// would stand energized under a standing power-fail trip with no
/// controller able to take over. The rig closes the window on the
/// second pair tick, so the declared bound is deliberately loose — it
/// admits a slower scan cadence while still naming the shape the
/// contract forbids.
const ARMED_BOUND: u64 = 5;

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

/// The reproduction's launch shape, staged once for both legs below:
/// the shared plant, an active naming no peer, and a `--standby` peer
/// wired at the active's monitor — announcing its own monitor on every
/// pull so the demoted run knows where its successor lives. Both run
/// `--remote`: every field access is the shared plant's. `auto_promote`,
/// when given, additionally arms the standby's failover budget — the
/// deployment posture the automatic half of the bound rides. The pair
/// is driven to the standing two-pump demand before it is returned:
/// both commands delivered to the field, the standby converged.
struct Staged {
    active: MonitorClient,
    standby: MonitorClient,
    field: RemoteDriver,
    token: u64,
    /// The spawned processes — the shared plant and both peers — held
    /// only so their `Drop` kill never fires mid-run.
    _processes: Vec<Spawned>,
}

impl Staged {
    /// Launch the pair and drive the simulated well to the standing
    /// demand — the lane's forcing write: `inflow` driven above the
    /// declared high setpoint under the owner's writer claim, so the
    /// well reaches the high-level demand inside `DEMAND_BOUND` ticks.
    fn launch(auto_promote: Option<&str>) -> Self {
        let plant = spawn_plant(Path::new(STATION), Path::new(DYNAMICS));
        let remote = ["--remote".to_string(), plant.addr.to_string()];
        let (active_process, preamble) = spawn_controller_logged(Path::new(STATION), &remote, DT);
        let token = owner_token(&preamble);
        let mut standby_args = remote.to_vec();
        standby_args.extend(["--standby".to_string(), active_process.addr.to_string()]);
        if let Some(budget) = auto_promote {
            standby_args.extend(["--auto-promote".to_string(), budget.to_string()]);
        }
        let standby_process = spawn_controller_logged(Path::new(STATION), &standby_args, DT).0;
        let staged = Staged {
            active: MonitorClient::new(active_process.addr),
            standby: MonitorClient::new(standby_process.addr),
            field: RemoteDriver::connect(plant.addr).unwrap(),
            token,
            _processes: vec![plant, active_process, standby_process],
        };
        staged.demand();
        staged
    }

    /// Converge the standby and drive the pair to full demand, the
    /// healthy precondition the trip interrupts — asserted here, once,
    /// so neither leg's own episode can be confused with a pair that
    /// never reached the demand its window is measured against.
    fn demand(&self) {
        self.field.ensure_writer(self.token).unwrap();
        self.field.write(INFLOW, Value::Float(5.0)).unwrap();
        let mut reached = None;
        for _ in 0..DEMAND_BOUND {
            self.standby.advance(1).unwrap();
            let snapshot = self.active.advance(1).unwrap();
            if full_demand(&snapshot) {
                reached = Some(snapshot);
                break;
            }
        }
        reached.expect("the pump group never held a full demand");
        assert!(
            matches!(
                self.standby.role().unwrap().sync,
                Some(StandbySync::Tracking { .. })
            ),
            "the standby never converged"
        );
        assert_eq!(field_value(&self.field, CMD_A), Value::Bool(true));
        assert_eq!(field_value(&self.field, CMD_B), Value::Bool(true));
    }

    /// Drive the field-side `power-fail` contact inside the owner's
    /// standing writer claim — the trip the release propagates from.
    /// The field commands still read energized when the write lands:
    /// the release lands on the driven scan sequence, never on the
    /// write itself, which is exactly what leaves the demote below a
    /// release that has not reached the plant yet.
    fn trip(&self) {
        self.field.ensure_writer(self.token).unwrap();
        self.field.write(POWER_FAIL, Value::Bool(true)).unwrap();
        assert_eq!(field_value(&self.field, CMD_A), Value::Bool(true));
        assert_eq!(field_value(&self.field, CMD_B), Value::Bool(true));
    }

    /// One driven pair tick — the tracking peer first so its pull
    /// applies the other's latest checkpoint, then the field owner —
    /// with the energization read back off the field afterwards.
    fn tick(&self) -> bool {
        self.standby.advance(1).unwrap();
        self.active.advance(1).unwrap();
        field_value(&self.field, CMD_A) == Value::Bool(true)
            || field_value(&self.field, CMD_B) == Value::Bool(true)
    }
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
    let pair = Staged::launch(None);
    let (active, standby, field) = (&pair.active, &pair.standby, &pair.field);

    // The trip, and the defect's trigger: a legal operator demote
    // inside the release-propagation window — the owner's gate closes
    // before its released-command write ever reached the plant.
    pair.trip();
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
            field_value(field, CMD_A),
            Value::Bool(true),
            "tick {tick}: no standby may write the field"
        );
        assert_eq!(field_value(field, CMD_B), Value::Bool(true));
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
        field_value(field, CMD_A),
        Value::Bool(false),
        "the promoted successor must write the pending release"
    );
    assert_eq!(
        field_value(field, CMD_B),
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
}

/// The bound itself, with no operator in the loop: the same staged
/// demote, but the standby carries the deployment's declared failover
/// budget — the flag the reference plant's manifest records as
/// `--auto-promote` beside its `failover_budget`. The residual risk
/// #828's reproduction left is the window being bounded by operator
/// action alone: with the budget armed, the orphan verdict counts the
/// cycle's miss (decision 87's accounting), so the budget-th orphan
/// self-promotes at that cycle's boundary and the promoted peer's first
/// field-owning scan writes the abandoned release. The field's
/// energization therefore never outlives the declared bound, and no
/// operator promotes anything.
#[test]
fn demote_during_power_fail_release_bounds_the_window_with_the_failover_budget() {
    let pair = Staged::launch(Some(BUDGET));
    let (active, standby, field) = (&pair.active, &pair.standby, &pair.field);

    // The same trigger: the trip with both outputs still energized on
    // the field, then the demote inside the release window — the
    // abandoned write, with no promote anywhere in the run.
    pair.trip();
    assert_eq!(active.demote().unwrap().role, Role::Demoting);

    // The window's own precondition: the energization is real, not a
    // release that happened to land before the demotion. The first
    // tick reads it still standing — no peer owns the field, so the
    // single-writer rule forbids either of them writing it.
    assert!(
        pair.tick(),
        "the demote must land inside the release window — the field \
         read released before any tick, which is the contract's other \
         half and measures no window"
    );

    // The bound: the energization must not outlive it. The tick that
    // closes the window is counted from the demote, so a build that
    // left the field energized orphaned with no controller able to
    // take over past it fails here rather than passing on the shape of
    // a window the operator would eventually have cleared.
    let mut ticks = 1u64;
    while ticks < ARMED_BOUND && pair.tick() {
        ticks += 1;
    }
    assert!(
        ticks < ARMED_BOUND,
        "the field outputs stood energized orphaned for {ticks} driven \
         ticks past the declared {ARMED_BOUND}-tick bound — the \
         armed standby never self-promoted to write the abandoned \
         release: {:?}",
        standby.role().unwrap()
    );
    assert_eq!(
        field_value(field, CMD_A),
        Value::Bool(false),
        "the self-promoted successor must write the pending release"
    );
    assert_eq!(
        field_value(field, CMD_B),
        Value::Bool(false),
        "the self-promoted successor must write the pending release"
    );

    // The recovery was the runtime's own, not an operator's: the
    // standby took the field through the failover path — journaled
    // with the `failover` origin, never a request's — and the pair
    // reconverged on it.
    assert!(
        standby.journal(0).unwrap().iter().any(|entry| matches!(
            entry.event,
            JournalEvent::RoleChanged {
                to: Role::Promoting,
                origin: Some(SwitchOrigin::Failover),
                ..
            }
        )),
        "the takeover must journal on the peer that took the field, \
         with the automatic origin: {:?}",
        standby.journal(0).unwrap()
    );
    assert_eq!(standby.role().unwrap().role, Role::Active);
    let report = active.role().unwrap();
    assert_eq!(report.role, Role::Standby, "{report:?}");
    assert!(
        matches!(report.sync, Some(StandbySync::Tracking { .. })),
        "the demoted peer must reconverge on the new owner: {report:?}"
    );
}
