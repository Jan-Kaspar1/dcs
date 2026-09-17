//! Integration tests for the station level-control contract
//! architecture decision 42 records: the checked-in fixture composes a
//! `failover-select` (primary/backup scripted level sources), a
//! `threshold-chain` (the ordered setpoint table driving a stage-count
//! demand), a two-pump `pump-group` consuming that demand, the
//! `bool-latching-alarm` wired onto `backup_active` — the alarmed
//! backup-mode engagement decisions 42 and 43 call for — and the
//! recorded manual-takeover composition: a writable `mode` point per
//! pump gating `cmd_i` against the operator's `hand` request through
//! `digital-input`/`bool-gate` wiring into the effective `eff_i`
//! request. Scripted deterministic runs exercise the chain in order
//! with hysteresis, the non-Good-measurement rule including the
//! source-failover interaction, manual takeover, setpoint tuning
//! through the command path, and checkpointed-standby continuity;
//! identical runs produce identical snapshots and write journals.
//!
//! Every inter-component hop crosses a one-scan link boundary — the
//! scripted `level` tick lands on the chain two scans later, the
//! chain's `demand` lands on the group one scan after that, and a
//! `mode`/`hand` write lands on `eff_i` about three scans on.

use dcs_assembly::{
    AssemblyError, BuildError, ComponentRegistry, DriverRegistry, FanoutDriver, assemble,
    resolve_drivers,
};
use dcs_blocks::{
    BoolGate, BoolLatchingAlarm, DigitalInput, FailoverSelect, GroupOutputs, PumpGroup, PumpIo,
    ThresholdChain, ThresholdOutputs,
};
use dcs_core::{
    Command, CommandError, CommandOutcome, IoDriver, PointId, Sample, Tick, Value, ValueKind,
};
use dcs_model::{DeviceId, PlantModel};
use dcs_runtime::{Component, Executor};
use dcs_sim::ScriptedDriver;

/// The fixture: one `sim-scripted` device replaying the level sweep —
/// `level` primary, `backup` secondary — plus the pump feedback set.
/// `sel-out`/`demand-out` are internal image points (the links carry
/// quality, which a field loopback's write journal would drop); the
/// per-pump `cmd_i` echoes the takeover composition consumes are field
/// loopbacks.
const STATION_LEVEL: &str = include_str!("../fixtures/station_level.json");

const SEL_OUT: PointId = PointId(20);
const MODE_1: PointId = PointId(30);
const HAND_1: PointId = PointId(31);
const MODE_2: PointId = PointId(32);
const HAND_2: PointId = PointId(33);
const SEL_BACKUP: PointId = PointId(41);
const BACKUP_ACK: PointId = PointId(48);
const SEL_ALARM: PointId = PointId(49);
const SEL_UNACK: PointId = PointId(62);
const DEMAND: PointId = PointId(23);
const DUTY_CALL: PointId = PointId(43);
const LAG_CALL: PointId = PointId(44);
const BELOW_CUTOFF: PointId = PointId(45);
const HIGH_LEVEL: PointId = PointId(46);
const CMD_1: PointId = PointId(50);
const CMD_2: PointId = PointId(51);
const STAGED: PointId = PointId(53);
const EFF_1: PointId = PointId(60);
const EFF_2: PointId = PointId(61);
const SCRIPTED_DEVICE: DeviceId = DeviceId(1);

fn boxed<C, E>(result: Result<C, E>) -> Result<Box<dyn Component>, BuildError>
where
    C: Component + 'static,
    E: std::error::Error + 'static,
{
    result
        .map(|component| Box::new(component) as Box<dyn Component>)
        .map_err(BuildError::other)
}

/// The `dcs-blocks` registration for the kinds the fixture uses,
/// mirroring the controller registry's port binding.
fn registry() -> ComponentRegistry {
    ComponentRegistry::new()
        .with(FailoverSelect::KIND, |spec| {
            boxed(FailoverSelect::from_parameters(
                spec.name.as_str(),
                spec.require("primary")?,
                spec.require("backup")?,
                spec.require("out")?,
                spec.require("backup_active")?,
                spec.get("backup_unhealthy"),
                spec.parameters,
            ))
        })
        .with(ThresholdChain::KIND, |spec| {
            boxed(ThresholdChain::from_parameters(
                spec.name.as_str(),
                spec.require("level")?,
                ThresholdOutputs {
                    demand: spec.require("demand")?,
                    duty_call: spec.require("duty_call")?,
                    lag_call: spec.require("lag_call")?,
                    below_cutoff: spec.require("below_cutoff")?,
                    high_level: spec.require("high_level")?,
                },
                spec.parameters,
            ))
        })
        .with(PumpGroup::KIND, |spec| {
            let pumps = spec
                .indexed_families(["cmd_", "run_", "fault_", "avail_"])?
                .into_iter()
                .map(|[cmd, run, fault, avail]| PumpIo {
                    cmd,
                    run,
                    fault,
                    avail,
                })
                .collect();
            boxed(PumpGroup::from_parameters(
                spec.name.as_str(),
                spec.require("demand")?,
                pumps,
                GroupOutputs {
                    duty: spec.require("duty")?,
                    staged: spec.require("staged")?,
                    none_available: spec.require("none_available")?,
                    all_faulted: spec.require("all_faulted")?,
                },
                spec.parameters,
            ))
        })
        .with(BoolLatchingAlarm::KIND, |spec| {
            boxed(BoolLatchingAlarm::from_parameters(
                spec.name.as_str(),
                spec.require("in")?,
                spec.require("ack")?,
                spec.require("alarm")?,
                spec.require("unacknowledged")?,
                spec.parameters,
            ))
        })
        .with(DigitalInput::KIND, |spec| {
            boxed(DigitalInput::from_parameters(
                spec.name.as_str(),
                spec.require("in")?,
                spec.require("out")?,
                spec.parameters,
            ))
        })
        .with(BoolGate::KIND, |spec| {
            let inputs = spec.indexed("in_");
            boxed(BoolGate::from_parameters(
                spec.name.as_str(),
                inputs,
                spec.require("out")?,
                spec.parameters,
            ))
        })
}

fn model(source: &str) -> PlantModel {
    PlantModel::load(source).unwrap()
}

fn build_driver(model: &PlantModel) -> FanoutDriver {
    resolve_drivers(model, &DriverRegistry::standard())
        .unwrap()
        .build()
        .unwrap()
}

fn build_executor<'d>(model: &PlantModel, driver: &'d FanoutDriver) -> Executor<'d> {
    assemble(model, &registry(), driver).unwrap()
}

fn int(driver: &FanoutDriver, point: PointId) -> i64 {
    match driver.read(point).unwrap().value {
        Value::Int(value) => value,
        value => panic!("point {point:?}: expected Int, got {value:?}"),
    }
}

fn boolean(driver: &FanoutDriver, point: PointId) -> bool {
    match driver.read(point).unwrap().value {
        Value::Bool(value) => value,
        value => panic!("point {point:?}: expected Bool, got {value:?}"),
    }
}

/// The image sample the last scan left on `point` — how the internal
/// `sel-out`/`demand-out` links read: the driver never sees them, and
/// unlike a field loopback the internal wire carries quality.
fn sample(executor: &Executor, point: PointId) -> Sample {
    executor
        .snapshot()
        .points
        .iter()
        .find(|telemetry| telemetry.point == point)
        .and_then(|telemetry| telemetry.sample)
        .unwrap_or_else(|| panic!("point {point:?} has no sample"))
}

fn int_at(executor: &Executor, point: PointId) -> i64 {
    match sample(executor, point).value {
        Value::Int(value) => value,
        value => panic!("point {point:?}: expected Int, got {value:?}"),
    }
}

/// The image-carried Bool on `point` — how the internal `sel-backup`
/// link reads.
fn bool_at(executor: &Executor, point: PointId) -> bool {
    match sample(executor, point).value {
        Value::Bool(value) => value,
        value => panic!("point {point:?}: expected Bool, got {value:?}"),
    }
}

/// The scripted run. Each scan reads the scripted entries the driver's
/// last `step` made current: the tick-`t` entry is first read at scan
/// `t + 1`, and the chain sees the failover's selection one loopback
/// boundary after that.
fn scan(executor: &mut Executor, driver: &FanoutDriver) {
    executor.scan().unwrap();
    driver.step(0.1).unwrap();
}

/// Scans until `executor`'s tick reaches `target`.
fn scan_to(executor: &mut Executor, driver: &FanoutDriver, target: u64) {
    while executor.snapshot().tick < Tick(target) {
        scan(executor, driver);
    }
}

#[test]
fn station_level_fixture_assembles_through_the_registry() {
    let model = model(STATION_LEVEL);
    let driver = build_driver(&model);
    let mut executor = build_executor(&model, &driver);
    scan(&mut executor, &driver);
    let snapshot = executor.snapshot();
    assert!(
        snapshot
            .components
            .iter()
            .all(|component| component.step_errors == 0)
    );
    // The recorded composition assembled: failover, chain, group, the
    // backup-mode latching alarm, and the per-pump takeover gates —
    // two digital-inputs and six bool-gates — twelve components in
    // all.
    assert_eq!(snapshot.components.len(), 12);
}

/// The scripted sweep drives the declared setpoint chain in order: the
/// level lands on the chain two scans after its script tick, and the
/// calls assert and release exactly at the declared thresholds.
#[test]
fn scripted_level_sweep_drives_the_chain_in_order() {
    let model = model(STATION_LEVEL);
    let driver = build_driver(&model);
    let mut executor = build_executor(&model, &driver);

    // Scans 1-6 read the below-cutoff level: every call stays off and
    // the cut-off condition asserts.
    for _ in 0..6 {
        scan(&mut executor, &driver);
        assert_eq!(int_at(&executor, DEMAND), 0);
        assert!(boolean(&driver, BELOW_CUTOFF));
        assert!(!boolean(&driver, DUTY_CALL));
    }

    // Scan 7 reads 3.0 through the failover link — inside the
    // stop/start band the station stays stopped and the flag clears.
    scan(&mut executor, &driver);
    assert!(!boolean(&driver, BELOW_CUTOFF));
    assert_eq!(int_at(&executor, DEMAND), 0);

    // Scan 10 reads 4.5 — at or above `start` (4.0): the duty call
    // asserts. Scan 13 reads 5.5: still inside the start/lag band.
    scan_to(&mut executor, &driver, 10);
    assert_eq!(int_at(&executor, DEMAND), 1);
    assert!(boolean(&driver, DUTY_CALL));
    assert!(!boolean(&driver, LAG_CALL));
    scan_to(&mut executor, &driver, 13);
    assert_eq!(int_at(&executor, DEMAND), 1);

    // Scan 15 reads 6.5 — at or above `lag_start` (6.0): the lag call
    // joins; scan 18 reads 8.5 — at or above `high` (8.0): the alarm
    // condition asserts while both calls hold.
    scan_to(&mut executor, &driver, 15);
    assert_eq!(int_at(&executor, DEMAND), 2);
    assert!(boolean(&driver, LAG_CALL));
    scan_to(&mut executor, &driver, 18);
    assert!(boolean(&driver, HIGH_LEVEL));

    // Falling through 6.5 and 5.0 (scans 21, 24): the lag's own
    // hysteresis — `start`, not `lag_start` — holds demand at 2.
    for target in [21, 24] {
        scan_to(&mut executor, &driver, target);
        assert_eq!(int_at(&executor, DEMAND), 2);
    }
    assert!(!boolean(&driver, HIGH_LEVEL));

    // Scan 27 reads 3.5 — at or below `start`: the lag releases first
    // while the duty call holds its band down to `stop`.
    scan_to(&mut executor, &driver, 27);
    assert_eq!(int_at(&executor, DEMAND), 1);
    assert!(!boolean(&driver, LAG_CALL));
    assert!(boolean(&driver, DUTY_CALL));

    // Scan 30 reads 2.5 — above `stop` (2.0): the duty keeps running;
    // scan 33 reads 1.5 — at or below `stop`: every call releases.
    scan_to(&mut executor, &driver, 30);
    assert_eq!(int_at(&executor, DEMAND), 1);
    scan_to(&mut executor, &driver, 33);
    assert_eq!(int_at(&executor, DEMAND), 0);
    assert!(!boolean(&driver, DUTY_CALL));

    // Scan 36 reads 0.8 — the cut-off floor again; scan 39 reads 3.0
    // back inside the band.
    scan_to(&mut executor, &driver, 36);
    assert!(boolean(&driver, BELOW_CUTOFF));
    scan_to(&mut executor, &driver, 39);
    assert!(!boolean(&driver, BELOW_CUTOFF));
    assert_eq!(int_at(&executor, DEMAND), 0);
}

/// The failover path the decision records: the `sel-out`/`sel-backup`
/// pair selects the primary while it reads `Good`; a failed primary
/// switches the chain onto the backup and the `bool-latching-alarm`
/// wired onto `backup_active` captures the transition — the alarmed
/// backup-mode engagement the decision requires. When every source
/// fails the chain's declared `on_bad_demand` — `0` here — answers
/// instead of silently controlling on bad data, and the recorded
/// return rule re-selects a recovered `Good` primary the same scan.
/// The alarm latch outlives the return until the operator's `ack`.
#[test]
fn a_non_good_level_follows_the_declared_fallback() {
    let model = model(STATION_LEVEL);
    let driver = build_driver(&model);
    let mut executor = build_executor(&model, &driver);

    // Scan 40: the primary is still the 3.0 of tick 37 — demand holds
    // 0, the backup flag is clear, and the alarm is quiet.
    scan_to(&mut executor, &driver, 40);
    assert!(!bool_at(&executor, SEL_BACKUP));
    assert!(!boolean(&driver, SEL_ALARM));
    assert!(!boolean(&driver, SEL_UNACK));
    assert_eq!(int_at(&executor, DEMAND), 0);

    // Scan 41 the failover reads the primary's Bad entry and selects
    // the backup: `backup_active` asserts on the internal point the
    // same scan — the alarmed transition's source.
    scan_to(&mut executor, &driver, 41);
    assert!(bool_at(&executor, SEL_BACKUP));

    // Scan 42 the internal link lands the asserted flag on the alarm's
    // `in`: the latching alarm captures the transition — `alarm`
    // follows the condition and `unacknowledged` latches the fresh
    // trip — while the chain reads the backup's Good 5.0 and calls the
    // duty pump on the backup measurement. `sel-out` carries the
    // backup value with Good quality.
    scan_to(&mut executor, &driver, 42);
    assert!(boolean(&driver, SEL_ALARM));
    assert!(boolean(&driver, SEL_UNACK));
    assert_eq!(int_at(&executor, DEMAND), 1);
    assert!(boolean(&driver, DUTY_CALL));
    assert_eq!(sample(&executor, SEL_OUT).value, Value::Float(5.0));

    // Scan 44 the backup itself goes Bad: `out` carries that failed
    // sample — every source is down — while the alarm's condition and
    // latch both stand.
    scan_to(&mut executor, &driver, 44);
    assert!(!sample(&executor, SEL_OUT).quality.is_good());
    assert!(boolean(&driver, SEL_ALARM));
    assert!(boolean(&driver, SEL_UNACK));
    // Scan 45 the chain reads the non-Good level: the declared fallback
    // emits, the calls drop, and no condition flag can assert on a
    // measurement the chain cannot read.
    scan_to(&mut executor, &driver, 45);
    assert_eq!(int_at(&executor, DEMAND), 0);
    assert!(!boolean(&driver, DUTY_CALL));
    assert!(!boolean(&driver, LAG_CALL));
    assert!(!boolean(&driver, BELOW_CUTOFF));
    assert!(!boolean(&driver, HIGH_LEVEL));

    // Scan 47 the recovered primary re-selects — `backup_active`
    // clears, the recorded immediate-return rule — and scan 48 the
    // chain reads the primary's 3.0 again while the link carries the
    // cleared flag to the alarm: `alarm` follows it down but the latch
    // stands until the operator acknowledges.
    scan_to(&mut executor, &driver, 48);
    assert!(!bool_at(&executor, SEL_BACKUP));
    assert!(!boolean(&driver, SEL_ALARM));
    assert!(boolean(&driver, SEL_UNACK));
    assert_eq!(int_at(&executor, DEMAND), 0);

    // The operator's `ack` clears the latch at the next scan boundary;
    // releasing the point leaves the alarm ready for a fresh trip.
    let receipt = executor.submit_command(Command::WriteValue {
        point: BACKUP_ACK,
        kind: ValueKind::Bool,
        value: Value::Bool(true),
    });
    assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
    scan(&mut executor, &driver);
    assert!(!boolean(&driver, SEL_UNACK));
    executor.submit_command(Command::WriteValue {
        point: BACKUP_ACK,
        kind: ValueKind::Bool,
        value: Value::Bool(false),
    });
    scan(&mut executor, &driver);
    assert!(!boolean(&driver, SEL_ALARM));
    assert!(!boolean(&driver, SEL_UNACK));
}

/// The manual-takeover composition the decision records: the writable
/// `mode` point per pump selects between the group's `cmd_i` and the
/// operator's `hand` request through the gate wiring — the hand request
/// wins regardless of what the level chain calls for, and returning to
/// automatic restores `cmd_i`.
#[test]
fn manual_takeover_selects_the_hand_request() {
    let model = model(STATION_LEVEL);
    let driver = build_driver(&model);
    let mut executor = build_executor(&model, &driver);

    // Through the sweep's end: the station idles, the effective
    // requests follow the (released) pump calls.
    scan_to(&mut executor, &driver, 66);
    assert!(!boolean(&driver, CMD_1));
    assert!(!boolean(&driver, EFF_1));
    assert!(!boolean(&driver, EFF_2));

    // Pump 1 to manual with the hand request on: `eff_1` follows the
    // hand request — the chain's released call does not pass.
    let receipt = executor.submit_command(Command::WriteValue {
        point: MODE_1,
        kind: ValueKind::Bool,
        value: Value::Bool(true),
    });
    assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
    let receipt = executor.submit_command(Command::WriteValue {
        point: HAND_1,
        kind: ValueKind::Bool,
        value: Value::Bool(true),
    });
    assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
    scan_to(&mut executor, &driver, 70);
    assert!(!boolean(&driver, CMD_1));
    assert!(boolean(&driver, EFF_1));
    assert!(!boolean(&driver, EFF_2));

    // The hand request off: `eff_1` releases while the mode point
    // still holds manual.
    executor.submit_command(Command::WriteValue {
        point: HAND_1,
        kind: ValueKind::Bool,
        value: Value::Bool(false),
    });
    scan_to(&mut executor, &driver, 74);
    assert!(!boolean(&driver, EFF_1));

    // The scripted 6.5 rise lands: the chain calls for both pumps
    // (demand 2) and the group asserts `cmd_1`/`cmd_2` — but pump 1 is
    // in manual, so `eff_1` stays with the operator's released hand
    // request while the automatic pump 2 runs its call.
    scan_to(&mut executor, &driver, 80);
    assert_eq!(int_at(&executor, DEMAND), 2);
    assert_eq!(int(&driver, STAGED), 2);
    assert!(boolean(&driver, CMD_1));
    assert!(boolean(&driver, CMD_2));
    assert!(!boolean(&driver, EFF_1));
    assert!(boolean(&driver, EFF_2));

    // Back to automatic: `eff_1` returns to `cmd_1`.
    executor.submit_command(Command::WriteValue {
        point: MODE_1,
        kind: ValueKind::Bool,
        value: Value::Bool(false),
    });
    scan_to(&mut executor, &driver, 85);
    assert!(boolean(&driver, EFF_1));
    assert!(boolean(&driver, EFF_2));

    // Pump 2's own mode point works independently.
    executor.submit_command(Command::WriteValue {
        point: MODE_2,
        kind: ValueKind::Bool,
        value: Value::Bool(true),
    });
    executor.submit_command(Command::WriteValue {
        point: HAND_2,
        kind: ValueKind::Bool,
        value: Value::Bool(false),
    });
    scan_to(&mut executor, &driver, 90);
    assert!(boolean(&driver, CMD_2));
    assert!(!boolean(&driver, EFF_2));
    assert!(boolean(&driver, EFF_1));
}

/// The operator-adjustable setpoints tune through `SetParameter` — the
/// recorded tuning path — landing at the scan boundary, reporting the
/// new value, and refusing a retune that would break the declared
/// ordering with a named `InvalidParameter`.
#[test]
fn operator_setpoints_tune_through_the_command_path() {
    let model = model(STATION_LEVEL);
    let driver = build_driver(&model);
    let mut executor = build_executor(&model, &driver);
    scan(&mut executor, &driver);

    // A mistyped value is refused at submission — the declared kind is
    // `Float`.
    let receipt = executor.submit_command(Command::SetParameter {
        component: "threshold-chain:2".to_string(),
        name: "start".to_string(),
        value: Value::Int(5),
    });
    assert!(matches!(
        receipt.outcome,
        CommandOutcome::Rejected {
            reason: CommandError::ParameterTypeMismatch { .. }
        }
    ));

    // Retune `start` from 4.0 to 5.0: applied at the next scan's
    // boundary and reported back through the descriptor surface.
    let receipt = executor.submit_command(Command::SetParameter {
        component: "threshold-chain:2".to_string(),
        name: "start".to_string(),
        value: Value::Float(5.0),
    });
    assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
    scan(&mut executor, &driver);
    let settled = executor.receipts().last().unwrap();
    assert_eq!(settled.outcome, CommandOutcome::Applied { tick: Tick(2) });
    let reported = executor.snapshot();
    let chain = reported
        .parameters
        .iter()
        .find(|component| component.name == "threshold-chain:2")
        .unwrap();
    assert_eq!(chain.values["start"], Value::Float(5.0));

    // The tuned threshold rules the chain's rising edge: the scripted
    // 4.5 (tick 8, seen at scan 10) sits below the new `start` — the
    // duty call stays off where the untuned table called it.
    scan_to(&mut executor, &driver, 10);
    assert_eq!(int_at(&executor, DEMAND), 0);
    assert!(!boolean(&driver, DUTY_CALL));

    // The 5.5 at tick 11 still crosses the tuned start.
    scan_to(&mut executor, &driver, 13);
    assert_eq!(int_at(&executor, DEMAND), 1);
    assert!(boolean(&driver, DUTY_CALL));

    // The tuned `start` is the lag call's release level as well: the
    // held demand-2 releases at 5.0 (tick 22, seen at scan 24) where
    // the untuned table held it down to 4.0 — hysteresis moves with
    // the retune, it does not release retroactively.
    scan_to(&mut executor, &driver, 24);
    assert_eq!(int_at(&executor, DEMAND), 1);

    // The second pass through 4.5 (tick 49, seen at scan 51) stays
    // below the tuned start too, and the 6.5 at tick 70 still crosses
    // `lag_start`.
    scan_to(&mut executor, &driver, 51);
    assert_eq!(int_at(&executor, DEMAND), 0);
    scan_to(&mut executor, &driver, 72);
    assert_eq!(int_at(&executor, DEMAND), 2);

    // A retune breaking the ordering — `start` at `lag_start` — is
    // refused naming the parameter, and the standing table is
    // untouched.
    executor.submit_command(Command::SetParameter {
        component: "threshold-chain:2".to_string(),
        name: "start".to_string(),
        value: Value::Float(6.0),
    });
    scan(&mut executor, &driver);
    let settled = executor.receipts().last().unwrap();
    assert_eq!(
        settled.outcome,
        CommandOutcome::Rejected {
            reason: CommandError::InvalidParameter {
                component: "threshold-chain:2".to_string(),
                parameter: "start".to_string(),
                detail: "setpoints must stay strictly increasing: \
                         cutoff < stop < start < lag_start < high"
                    .to_string(),
            }
        }
    );
    let reported = executor.snapshot();
    let chain = reported
        .parameters
        .iter()
        .find(|component| component.name == "threshold-chain:2")
        .unwrap();
    assert_eq!(chain.values["start"], Value::Float(5.0));
}

/// A standby assembling the same model and applying a mid-run
/// checkpoint — the held demand stage and tuned table included —
/// continues the scripted run identically to the active.
#[test]
fn checkpointed_standby_continues_the_run_identically() {
    let model = model(STATION_LEVEL);
    let driver_a = build_driver(&model);
    let mut active = build_executor(&model, &driver_a);
    // Checkpoint mid-sequence: demand 1 standing (the failover window
    // is behind the chain).
    scan_to(&mut active, &driver_a, 52);
    assert_eq!(int_at(&active, DEMAND), 1);
    // The backup-mode latch tripped at scan 42 still stands — the mode
    // the checkpoint must carry.
    assert!(boolean(&driver_a, SEL_UNACK));
    let checkpoint = active.checkpoint();

    let driver_b = build_driver(&model);
    let mut standby = build_executor(&model, &driver_b);
    standby.apply(&checkpoint).unwrap();

    for _ in 0..30 {
        scan(&mut active, &driver_a);
        scan(&mut standby, &driver_b);
    }

    assert_eq!(
        serde_json::to_string(&active.snapshot()).unwrap(),
        serde_json::to_string(&standby.snapshot()).unwrap()
    );
    let writes_a = driver_a
        .inspect::<ScriptedDriver>(SCRIPTED_DEVICE)
        .unwrap()
        .writes();
    let writes_b = driver_b
        .inspect::<ScriptedDriver>(SCRIPTED_DEVICE)
        .unwrap()
        .writes();
    assert_eq!(writes_a[writes_a.len() - writes_b.len()..], writes_b[..]);
}

/// The engaged backup mode is run state: a standby applying a
/// checkpoint taken while the backup serves — `backup_active`
/// asserted, the latching alarm tripped — continues the same run:
/// the failover window's tail (the all-bad fallback, the primary's
/// recovery, the standing latch) replays identically instead of
/// restarting on the primary.
#[test]
fn checkpointed_standby_mid_backup_keeps_the_engaged_mode() {
    let model = model(STATION_LEVEL);
    let driver_a = build_driver(&model);
    let mut active = build_executor(&model, &driver_a);

    // Scan 43: the backup is serving and the alarm's latch stands.
    scan_to(&mut active, &driver_a, 43);
    assert!(bool_at(&active, SEL_BACKUP));
    assert!(boolean(&driver_a, SEL_UNACK));
    let checkpoint = active.checkpoint();

    let driver_b = build_driver(&model);
    let mut standby = build_executor(&model, &driver_b);
    standby.apply(&checkpoint).unwrap();
    // The restored image carries the asserted flag and the held
    // internal link — the standby does not see a cleared mode.
    assert!(bool_at(&standby, SEL_BACKUP));

    for _ in 0..8 {
        scan(&mut active, &driver_a);
        scan(&mut standby, &driver_b);
    }

    assert_eq!(
        serde_json::to_string(&active.snapshot()).unwrap(),
        serde_json::to_string(&standby.snapshot()).unwrap()
    );
    // Through the failover window's tail the latch never cleared on
    // either side.
    assert!(boolean(&driver_b, SEL_UNACK));
    let writes_a = driver_a
        .inspect::<ScriptedDriver>(SCRIPTED_DEVICE)
        .unwrap()
        .writes();
    let writes_b = driver_b
        .inspect::<ScriptedDriver>(SCRIPTED_DEVICE)
        .unwrap()
        .writes();
    assert_eq!(writes_a[writes_a.len() - writes_b.len()..], writes_b[..]);
}

#[test]
fn identical_scripted_runs_produce_identical_snapshots_and_journals() {
    let run = || {
        let model = model(STATION_LEVEL);
        let driver = build_driver(&model);
        let mut executor = build_executor(&model, &driver);
        for _ in 0..95 {
            scan(&mut executor, &driver);
        }
        let snapshot = serde_json::to_string(&executor.snapshot()).unwrap();
        let writes = driver
            .inspect::<ScriptedDriver>(SCRIPTED_DEVICE)
            .unwrap()
            .writes();
        (snapshot, writes)
    };
    assert_eq!(run(), run());
}

/// A setpoint table violating the declared ordering fails assembly
/// naming the offending parameter — the constructor's answer to a
/// malformed table.
#[test]
fn a_malformed_setpoint_table_fails_assembly_naming_the_parameter() {
    let mut document: serde_json::Value = serde_json::from_str(STATION_LEVEL).unwrap();
    document["components"][1]["parameters"]["lag_start"] = serde_json::json!({ "float": 3.0 });
    let model = model(&document.to_string());
    let driver = build_driver(&model);
    match assemble(&model, &registry(), &driver).err().unwrap() {
        AssemblyError::Component {
            component, detail, ..
        } => {
            assert_eq!(component.0, 2);
            assert!(
                detail.contains("lag_start"),
                "the failure should name the offending setpoint, found {detail}"
            );
        }
        other => panic!("expected AssemblyError::Component, got {other:?}"),
    }
}
