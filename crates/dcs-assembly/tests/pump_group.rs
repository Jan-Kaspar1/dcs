//! Integration tests for the duty/standby `pump-group` kind: the
//! checked-in fixture instantiates three two-pump groups — one per
//! declared rotation policy — fed by one scripted input set, plus the
//! two `sr-latch` instances the station alarm set composes from the
//! group's `none_available`/`all_faulted` status outputs. A scripted
//! deterministic run exercises duty rotation under each policy,
//! availability exclusion, failed-feedback handover, lag staging behind
//! the inter-pump start delay, de-staging, and the group conditions;
//! identical runs produce identical snapshots and write journals, and a
//! checkpointed standby continues the run identically.

use dcs_assembly::{
    AssemblyError, BuildError, ComponentRegistry, DriverRegistry, FanoutDriver, assemble,
    resolve_drivers,
};
use dcs_blocks::{GroupOutputs, PumpGroup, PumpIo, SrLatch};
use dcs_core::{Command, CommandOutcome, IoDriver, PointId, Value, ValueKind};
use dcs_model::{DeviceId, PlantModel};
use dcs_runtime::{Component, Executor};
use dcs_sim::ScriptedDriver;

/// The fixture: one `sim-scripted` device replaying demand, run
/// feedback, fault, and availability scripts for two pumps; three
/// `pump-group` instances — `pump-group:10` alternating per cycle,
/// `pump-group:11` rotating on a six-tick interval, `pump-group:12`
/// picking least-run-hours first — and `sr-latch` instances 30/31
/// latching the alternating group's status outputs behind the writable
/// `ack` point.
const PUMP_GROUP: &str = include_str!("../fixtures/pump_group.json");

const ACK: PointId = PointId(30);
const CMD_A1: PointId = PointId(20);
const CMD_A2: PointId = PointId(21);
const DUTY_A: PointId = PointId(22);
const STAGED_A: PointId = PointId(23);
const NONE_A: PointId = PointId(24);
const FAULTED_A: PointId = PointId(25);
const LATCH_NONE: PointId = PointId(31);
const LATCH_FAULTED: PointId = PointId(32);
const DUTY_T: PointId = PointId(42);
const STAGED_T: PointId = PointId(43);
const DUTY_L: PointId = PointId(52);
const STAGED_L: PointId = PointId(53);
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
/// mirroring the controller registry's indexed-port discovery.
fn registry() -> ComponentRegistry {
    ComponentRegistry::new()
        .with(PumpGroup::KIND, |spec| {
            let mut indices = std::collections::BTreeSet::new();
            for prefix in ["cmd_", "run_", "fault_", "avail_"] {
                indices.extend(spec.ports.keys().filter_map(|name| {
                    name.strip_prefix(prefix)
                        .and_then(|suffix| suffix.parse::<usize>().ok())
                }));
            }
            let count = indices.iter().next_back().copied().unwrap_or(0);
            let mut pumps = Vec::with_capacity(count);
            for index in 1..=count {
                pumps.push(PumpIo {
                    cmd: spec.require(&format!("cmd_{index}"))?,
                    run: spec.require(&format!("run_{index}"))?,
                    fault: spec.require(&format!("fault_{index}"))?,
                    avail: spec.require(&format!("avail_{index}"))?,
                });
            }
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
        .with(SrLatch::KIND, |spec| {
            boxed(SrLatch::from_parameters(
                spec.name.as_str(),
                spec.require("set")?,
                spec.require("reset")?,
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

/// The scripted run. Each scan reads the scripted entries the driver's
/// last `step` made current: the tick-0 demand is already in effect at
/// scan 1.
fn scan(executor: &mut Executor, driver: &FanoutDriver) {
    executor.scan().unwrap();
    driver.step(0.1).unwrap();
}

#[test]
fn pump_group_fixture_assembles_through_the_registry() {
    let model = model(PUMP_GROUP);
    let driver = build_driver(&model);
    let mut executor = build_executor(&model, &driver);
    scan(&mut executor, &driver);
    assert!(
        executor
            .snapshot()
            .components
            .iter()
            .all(|component| component.step_errors == 0)
    );
}

/// The alternating group's duty path: pump 1 takes the first demand,
/// pump 2 takes over on pump 1's scripted fault, the assignment
/// alternates back at each pump-down cycle end, the lag stages behind
/// `start_delay_ticks`, availability loss excludes pump 1 mid-demand,
/// and the no-pump-available / all-pumps-faulted conditions assert
/// through the starvation window.
#[test]
fn scripted_run_demonstrates_rotation_handover_and_staging() {
    let model = model(PUMP_GROUP);
    let driver = build_driver(&model);
    let mut executor = build_executor(&model, &driver);

    // Scans 1-4, demand 1: pump 1 is duty everywhere — the groups agree
    // on a fresh start (the alternation cursor, the timed interval, and
    // the run-hours tie all resolve to the lowest available index).
    for _ in 0..4 {
        scan(&mut executor, &driver);
        assert_eq!(int(&driver, DUTY_A), 1);
        assert_eq!(int(&driver, STAGED_A), 1);
        assert!(boolean(&driver, CMD_A1));
        assert!(!boolean(&driver, CMD_A2));
        assert_eq!(int(&driver, DUTY_T), 1);
        assert_eq!(int(&driver, DUTY_L), 1);
    }

    // Scan 5 reads fault-1's true entry: the running duty pump's proven
    // failure hands duty to the standby automatically at the same scan.
    scan(&mut executor, &driver);
    assert_eq!(int(&driver, DUTY_A), 2);
    assert!(boolean(&driver, CMD_A2));
    assert_eq!(int(&driver, DUTY_T), 2);
    assert_eq!(int(&driver, DUTY_L), 2);
    for _ in 0..3 {
        scan(&mut executor, &driver);
    }

    // Scan 9 reads demand 0: the pump-down cycle ends and the policies
    // diverge — alternation hands duty back to pump 1 while the timed
    // interval has not elapsed and least-run keeps pump 2 (four
    // accumulated ticks against pump 1's six).
    scan(&mut executor, &driver);
    assert_eq!(int(&driver, DUTY_A), 1);
    assert_eq!(int(&driver, DUTY_T), 2);
    assert_eq!(int(&driver, DUTY_L), 2);
    scan(&mut executor, &driver);

    // Scan 11 reads demand 1: the timed group's six-tick interval
    // elapsed and rotates its duty to pump 1; the others hold.
    scan(&mut executor, &driver);
    assert_eq!(int(&driver, DUTY_A), 1);
    assert_eq!(int(&driver, DUTY_T), 1);
    assert_eq!(int(&driver, DUTY_L), 2);

    // Scan 12 reads demand 2: every group's lag is eligible but the
    // inter-pump start delay holds it one more scan.
    scan(&mut executor, &driver);
    assert_eq!(int(&driver, STAGED_A), 1);
    assert_eq!(int(&driver, STAGED_T), 1);
    assert_eq!(int(&driver, STAGED_L), 1);
    // Scans 13-15: the delay elapsed and the lag stages.
    for _ in 0..3 {
        scan(&mut executor, &driver);
        assert_eq!(int(&driver, STAGED_A), 2);
        assert_eq!(int(&driver, STAGED_T), 2);
        assert_eq!(int(&driver, STAGED_L), 2);
    }

    // Scan 16 reads avail-1's false entry mid-demand: pump 1 drops out
    // of every target list and the alternating/timed groups reassign
    // duty to pump 2 — staged falls to 1 though demand still wants 2.
    scan(&mut executor, &driver);
    assert_eq!(int(&driver, DUTY_A), 2);
    assert_eq!(int(&driver, DUTY_T), 2);
    assert_eq!(int(&driver, DUTY_L), 2);
    for _ in 0..3 {
        scan(&mut executor, &driver);
        assert_eq!(int(&driver, STAGED_A), 1);
        assert_eq!(int(&driver, STAGED_T), 1);
        assert_eq!(int(&driver, STAGED_L), 1);
    }

    // Scans 20-21 read demand 1: the lag — already de-staged by the
    // availability loss — stays out; duty holds on pump 2.
    for _ in 0..2 {
        scan(&mut executor, &driver);
        assert_eq!(int(&driver, STAGED_A), 1);
    }
    // Scan 22: the timed interval elapsed again and rotates duty back
    // to pump 1 (its availability returned at scan 21).
    scan(&mut executor, &driver);
    assert_eq!(int(&driver, DUTY_T), 1);
    assert_eq!(int(&driver, DUTY_A), 2);
    assert_eq!(int(&driver, DUTY_L), 2);

    // Scan 23 reads demand 0: another cycle ends. Alternation hands
    // duty back to pump 1; least-run keeps pump 2 — pump 2 still holds
    // fewer accumulated ticks than pump 1.
    scan(&mut executor, &driver);
    assert_eq!(int(&driver, DUTY_A), 1);
    assert_eq!(int(&driver, DUTY_L), 2);
    assert_eq!(int(&driver, STAGED_A), 0);
    for _ in 0..4 {
        scan(&mut executor, &driver);
    }
    // Scan 28: the timed interval elapses while the group idles —
    // rotation still advances the recorded assignment to pump 2.
    scan(&mut executor, &driver);
    assert_eq!(int(&driver, DUTY_T), 2);

    // Scans 29-33 read the starvation window: both pumps faulted and
    // unavailable. Duty drops to none and both group conditions assert.
    for _ in 0..5 {
        scan(&mut executor, &driver);
        assert_eq!(int(&driver, DUTY_A), 0);
        assert_eq!(int(&driver, STAGED_A), 0);
        assert!(boolean(&driver, NONE_A));
        assert!(boolean(&driver, FAULTED_A));
    }
    // The status outputs fed the sr-latch instances one scan behind —
    // the point-to-point echo channels' loopback boundary.
    assert!(boolean(&driver, LATCH_NONE));
    assert!(boolean(&driver, LATCH_FAULTED));

    // Scan 34 reads the recovery: demand 1 with every pump back. The
    // alternating cursor still sits on pump 2 from the last assignment;
    // the timed group reassigns to pump 1 as the first available; the
    // least-run tie resolves to pump 1.
    scan(&mut executor, &driver);
    assert_eq!(int(&driver, DUTY_A), 2);
    assert_eq!(int(&driver, DUTY_T), 1);
    assert_eq!(int(&driver, DUTY_L), 1);
    assert_eq!(int(&driver, STAGED_A), 1);
    assert!(boolean(&driver, CMD_A2));
    // The standing conditions cleared; the latches still hold.
    assert!(!boolean(&driver, NONE_A));
    assert!(!boolean(&driver, FAULTED_A));
    assert!(boolean(&driver, LATCH_NONE));
    assert!(boolean(&driver, LATCH_FAULTED));

    // Scan 35 reads demand 0: the final cycle end alternates duty back
    // to pump 1 while the other policies hold theirs.
    scan(&mut executor, &driver);
    assert_eq!(int(&driver, DUTY_A), 1);
    assert_eq!(int(&driver, DUTY_T), 1);
    assert_eq!(int(&driver, DUTY_L), 1);
}

/// The group status outputs drive `sr-latch` instances composed exactly
/// as the station decision prescribes: the latches set while the
/// condition stands, hold after it clears, and a `WriteValue` on the
/// writable `ack` point resets them.
#[test]
fn status_outputs_drive_the_latching_instances() {
    let model = model(PUMP_GROUP);
    let driver = build_driver(&model);
    let mut executor = build_executor(&model, &driver);

    // Through the starvation window (scan 33) and into recovery (scan
    // 36): the standing outputs clear but the latches hold.
    for _ in 0..36 {
        scan(&mut executor, &driver);
    }
    assert!(!boolean(&driver, NONE_A));
    assert!(!boolean(&driver, FAULTED_A));
    assert!(boolean(&driver, LATCH_NONE));
    assert!(boolean(&driver, LATCH_FAULTED));

    let receipt = executor.submit_command(Command::WriteValue {
        point: ACK,
        kind: ValueKind::Bool,
        value: Value::Bool(true),
    });
    assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
    scan(&mut executor, &driver);
    assert!(!boolean(&driver, LATCH_NONE));
    assert!(!boolean(&driver, LATCH_FAULTED));
}

#[test]
fn identical_scripted_runs_produce_identical_snapshots_and_journals() {
    let run = || {
        let model = model(PUMP_GROUP);
        let driver = build_driver(&model);
        let mut executor = build_executor(&model, &driver);
        for _ in 0..40 {
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

/// A standby assembling the same model and applying a mid-run checkpoint
/// — rotation position, accumulated run hours, and held-off timers
/// included — continues the scripted run identically to the active.
#[test]
fn checkpointed_standby_continues_the_run_identically() {
    let model = model(PUMP_GROUP);
    let driver_a = build_driver(&model);
    let mut active = build_executor(&model, &driver_a);
    for _ in 0..20 {
        scan(&mut active, &driver_a);
    }
    let checkpoint = active.checkpoint();

    let driver_b = build_driver(&model);
    let mut standby = build_executor(&model, &driver_b);
    standby.apply(&checkpoint).unwrap();

    for _ in 0..20 {
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

/// An indexed pump family missing a member fails assembly with
/// [`AssemblyError::UnboundPort`] naming the port.
#[test]
fn a_gap_in_the_indexed_pump_family_fails_assembly_naming_the_port() {
    let mut document: serde_json::Value = serde_json::from_str(PUMP_GROUP).unwrap();
    let connections = document["connections"].as_array_mut().unwrap();
    connections.retain(|connection| {
        connection["to"]["port"]["name"] != "run_2" || connection["to"]["port"]["component"] != 10
    });
    let model = model(&document.to_string());
    let driver = build_driver(&model);
    match assemble(&model, &registry(), &driver).err().unwrap() {
        AssemblyError::UnboundPort { component, port } => {
            assert_eq!(component.0, 10);
            assert_eq!(port, "run_2");
        }
        other => panic!("expected UnboundPort, got {other:?}"),
    }
}
