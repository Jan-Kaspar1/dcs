//! Integration tests for the `backwash-sequence` kind: the checked-in
//! fixture instantiates two sequences — component 20, a four-step table
//! under `auto_start = 0` and `on_fault_policy = 0` (fault hold), and
//! component 21, a two-step table under `auto_start = 1` and
//! `on_fault_policy = 1` (fault abort). A scripted deterministic run
//! exercises the grant handshake — the armed request dwelling on its
//! first step ungranted, the table advancing only while `grant`
//! stands, and `done`/`aborted` releasing it — timed, measured and
//! first-of advance including the measured overrun's hold with
//! `overrun` asserted, the untrusted-`meas_i` fail-safe reading,
//! `phase_<n>` exclusivity, per-source trigger attribution held for
//! the run, the `pending` latch's operator release through the
//! receipted command path, `abort` driving `abort_step`, and each
//! fault policy; identical runs produce identical snapshots and write
//! journals, and a checkpointed standby continues the run identically.

use dcs_assembly::{
    AssemblyError, BuildError, ComponentRegistry, DriverRegistry, FanoutDriver, assemble,
    resolve_drivers,
};
use dcs_blocks::{BackwashSequence, BackwashSequenceInputs, BackwashSequenceOutputs};
use dcs_core::{Command, CommandOutcome, IoDriver, PointId, Value, ValueKind};
use dcs_model::{DeviceId, PlantModel};
use dcs_runtime::{Component, Executor};
use dcs_sim::ScriptedDriver;

/// The fixture: one `sim-scripted` device replaying the trigger,
/// `grant`, `fault` and measurement scripts for both sequences;
/// `backwash-sequence` component 20 the four-step table (timed,
/// measured-hold, first-of, timed) whose `trig_operator` and `abort`
/// ports bind the writable internal points 30 and 31, and component 21
/// the two-step `pending`-latched table whose operator ports bind
/// points 32 and 33.
const BACKWASH: &str = include_str!("../fixtures/backwash_sequence.json");

const TRIG_OPERATOR_A: PointId = PointId(30);
const ABORT_A: PointId = PointId(31);
const TRIG_OPERATOR_B: PointId = PointId(32);
const REQUEST_A: PointId = PointId(40);
const ACTIVE_A: PointId = PointId(41);
const PENDING_A: PointId = PointId(42);
const DONE_A: PointId = PointId(43);
const ABORTED_A: PointId = PointId(44);
const OVERRUN_A: PointId = PointId(45);
const SOURCE_A: PointId = PointId(46);
const STEP_A: PointId = PointId(47);
const PHASE_A: [PointId; 4] = [PointId(49), PointId(50), PointId(51), PointId(52)];
const REQUEST_B: PointId = PointId(60);
const PENDING_B: PointId = PointId(62);
const DONE_B: PointId = PointId(63);
const ABORTED_B: PointId = PointId(64);
const SOURCE_B: PointId = PointId(66);
const STEP_B: PointId = PointId(67);
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

/// The `dcs-blocks` registration for the kind the fixture uses,
/// mirroring the controller registry's indexed-port discovery.
fn registry() -> ComponentRegistry {
    ComponentRegistry::new().with(BackwashSequence::KIND, |spec| {
        let meas = spec
            .indexed_families(["meas_"])?
            .into_iter()
            .map(|[meas]| meas)
            .collect();
        let phases = spec
            .indexed_families(["phase_"])?
            .into_iter()
            .map(|[phase]| phase)
            .collect();
        boxed(BackwashSequence::from_parameters(
            spec.name.as_str(),
            BackwashSequenceInputs {
                trig_time: spec.require("trig_time")?,
                trig_headloss: spec.require("trig_headloss")?,
                trig_turbidity: spec.require("trig_turbidity")?,
                trig_operator: spec.require("trig_operator")?,
                grant: spec.require("grant")?,
                abort: spec.require("abort")?,
                fault: spec.require("fault")?,
                meas,
            },
            BackwashSequenceOutputs {
                request: spec.require("request")?,
                active: spec.require("active")?,
                pending: spec.require("pending")?,
                done: spec.require("done")?,
                aborted: spec.require("aborted")?,
                overrun: spec.require("overrun")?,
                trigger_source: spec.require("trigger_source")?,
                step: spec.require("step")?,
                out: spec.require("out")?,
                phases,
            },
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
/// last `step` made current: the tick-0 values are already in effect at
/// scan 1.
fn scan(executor: &mut Executor, driver: &FanoutDriver) {
    executor.scan();
    driver.step(0.1).unwrap();
}

/// A receipted write on a writable internal `In` point — the operator
/// surface `trig_operator` and `abort` ride.
fn write(executor: &mut Executor, point: PointId, value: bool) {
    let receipt = executor.submit_command(Command::WriteValue {
        point,
        kind: ValueKind::Bool,
        value: Value::Bool(value),
    });
    assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
}

/// One scan of the scripted run, applying the operator writes the
/// script calls for at that boundary and asserting `phase_<n>`
/// exclusivity on sequence A — exactly the reported step's flag
/// stands.
fn run_scan(executor: &mut Executor, driver: &FanoutDriver, scan_number: u64) {
    match scan_number {
        18 | 28 => write(executor, TRIG_OPERATOR_A, true),
        19 | 29 => write(executor, TRIG_OPERATOR_A, false),
        20 => write(executor, ABORT_A, true),
        21 => write(executor, ABORT_A, false),
        4 | 11 => write(executor, TRIG_OPERATOR_B, true),
        5 | 12 => write(executor, TRIG_OPERATOR_B, false),
        _ => {}
    }
    scan(executor, driver);
    let step = int(driver, STEP_A);
    for (index, phase) in PHASE_A.iter().enumerate() {
        assert_eq!(
            boolean(driver, *phase),
            index as i64 + 1 == step,
            "scan {scan_number}: phase_{} must track the reported step {step}",
            index + 1
        );
    }
}

#[test]
fn backwash_sequence_fixture_assembles_through_the_registry() {
    let model = model(BACKWASH);
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

/// Sequence A's first run: the tick-0 `trig_time` arms the request,
/// which dwells on step 1 while `grant` stands low; once granted the
/// timed step advances on its tick bound, the measured step stands
/// past its overrun timeout — `overrun` asserting while the
/// `Uncertain` reading cannot satisfy the bound — until a `Good`
/// reading crosses it, the first-of step leaves on its bound, and the
/// final timed step completes with `done` and the request drop.
#[test]
fn scripted_run_demonstrates_the_grant_gated_step_table() {
    let model = model(BACKWASH);
    let driver = build_driver(&model);
    let mut executor = build_executor(&model, &driver);

    // Scans 1-3: the time trigger arms `request` on scan 1 — the
    // trigger source attributed and held — but `grant` stands low
    // until tick 3: the armed run dwells on step 1 without advancing.
    for scan_number in 1..=3 {
        run_scan(&mut executor, &driver, scan_number);
        assert!(boolean(&driver, REQUEST_A));
        assert!(!boolean(&driver, ACTIVE_A));
        assert!(!boolean(&driver, PENDING_A));
        assert_eq!(int(&driver, SOURCE_A), 1);
        assert_eq!(int(&driver, STEP_A), 1);
    }

    // Scans 4-5: granted, the timed step 1 banks its two ticks and
    // reports itself until the advancing scan hands over.
    run_scan(&mut executor, &driver, 4);
    assert!(boolean(&driver, ACTIVE_A));
    assert_eq!(int(&driver, STEP_A), 1);
    run_scan(&mut executor, &driver, 5);
    assert_eq!(int(&driver, STEP_A), 1);
    run_scan(&mut executor, &driver, 6);
    assert_eq!(int(&driver, STEP_A), 2);

    // Scans 8-10: step 2's three-tick overrun timeout stands without
    // the bound satisfied — the `Uncertain` 60.0 from tick 8 cannot
    // satisfy it — so `overrun` asserts while the hold policy parks
    // the banked count.
    for scan_number in 7..=10 {
        run_scan(&mut executor, &driver, scan_number);
        assert_eq!(int(&driver, STEP_A), 2);
        assert_eq!(
            boolean(&driver, OVERRUN_A),
            scan_number >= 8,
            "scan {scan_number}: overrun asserts at the measured timeout"
        );
    }

    // Scan 11: the tick-10 `Good` 60.0 satisfies the bound — the step
    // advances and `overrun` clears.
    run_scan(&mut executor, &driver, 11);
    assert!(!boolean(&driver, OVERRUN_A));
    assert_eq!(int(&driver, STEP_A), 2);

    // Scans 12-13: the first-of step 3 leaves on its `meas_2` bound
    // crossing ahead of its three-tick bound.
    run_scan(&mut executor, &driver, 12);
    assert_eq!(int(&driver, STEP_A), 3);
    run_scan(&mut executor, &driver, 13);
    assert_eq!(int(&driver, STEP_A), 3);
    run_scan(&mut executor, &driver, 14);
    assert_eq!(int(&driver, STEP_A), 4);

    // Scan 14 completed the one-tick step 4: `done` stands and
    // `request` dropped — the coordinator grant released — while the
    // attributed source still reports the run's trigger. `active`
    // stands this scan: the completing scan did step.
    assert!(boolean(&driver, DONE_A));
    assert!(!boolean(&driver, REQUEST_A));
    assert!(boolean(&driver, ACTIVE_A));
    assert_eq!(int(&driver, SOURCE_A), 1);
}

/// The operator paths on sequence A: the receipted `trig_operator`
/// edge arms a run attributed to source 4, the receipted `abort`
/// drives `abort_step` with `aborted` raised and `request` dropped;
/// the re-armed run's proven `fault` parks it at `on_fault_step`
/// holding the grant until the operator edge resumes it into `done`.
#[test]
fn scripted_run_demonstrates_abort_and_fault_hold_through_the_command_path() {
    let model = model(BACKWASH);
    let driver = build_driver(&model);
    let mut executor = build_executor(&model, &driver);

    // Scans 1-17: the first run completes as in the step-table test.
    for scan_number in 1..=17 {
        run_scan(&mut executor, &driver, scan_number);
    }
    assert!(boolean(&driver, DONE_A));

    // Scan 18: the receipted `trig_operator` write arms a fresh run —
    // source 4 — stepping under the standing grant.
    run_scan(&mut executor, &driver, 18);
    assert!(boolean(&driver, REQUEST_A));
    assert!(boolean(&driver, ACTIVE_A));
    assert!(!boolean(&driver, DONE_A));
    assert_eq!(int(&driver, SOURCE_A), 4);
    assert_eq!(int(&driver, STEP_A), 1);
    run_scan(&mut executor, &driver, 19);
    assert_eq!(int(&driver, STEP_A), 1);

    // Scan 20: the receipted `abort` drives `abort_step` — step 4 —
    // raising `aborted` and dropping `request`; `done` stays clear.
    run_scan(&mut executor, &driver, 20);
    assert!(boolean(&driver, ABORTED_A));
    assert!(!boolean(&driver, DONE_A));
    assert!(!boolean(&driver, REQUEST_A));
    assert!(!boolean(&driver, ACTIVE_A));
    assert_eq!(int(&driver, STEP_A), 4);
    run_scan(&mut executor, &driver, 21);
    assert!(boolean(&driver, ABORTED_A));
    run_scan(&mut executor, &driver, 22);
    assert!(boolean(&driver, ABORTED_A));

    // Scan 23: the scripted `trig_turbidity` edge arms source 3 and
    // steps into the table under the standing grant.
    run_scan(&mut executor, &driver, 23);
    assert!(boolean(&driver, REQUEST_A));
    assert!(!boolean(&driver, ABORTED_A));
    assert_eq!(int(&driver, SOURCE_A), 3);
    assert_eq!(int(&driver, STEP_A), 1);
    run_scan(&mut executor, &driver, 24);
    assert_eq!(int(&driver, STEP_A), 1);

    // Scans 25-27: the proven `fault` parks the run at `on_fault_step`
    // — step 4 — under the hold policy: `request` stays armed so the
    // grant holds, `active` falls, and neither the fault's release
    // alone nor dwell resumes it.
    for scan_number in 25..=27 {
        run_scan(&mut executor, &driver, scan_number);
        assert!(boolean(&driver, REQUEST_A));
        assert!(!boolean(&driver, ACTIVE_A));
        assert!(!boolean(&driver, DONE_A));
        assert_eq!(int(&driver, STEP_A), 4);
    }

    // Scan 28: the receipted `trig_operator` edge with the fault
    // cleared releases the hold — the held one-tick step banks and
    // completes: `done` stands, `request` releases.
    run_scan(&mut executor, &driver, 28);
    assert!(boolean(&driver, DONE_A));
    assert!(!boolean(&driver, ABORTED_A));
    assert!(!boolean(&driver, REQUEST_A));
    assert_eq!(int(&driver, STEP_A), 4);
    run_scan(&mut executor, &driver, 29);
    assert!(boolean(&driver, DONE_A));
    run_scan(&mut executor, &driver, 30);
    assert_eq!(int(&driver, SOURCE_A), 3);
}

/// Sequence B under `auto_start = 1`: the automatic `trig_turbidity`
/// edge latches `pending` instead of arming — `request` stays low —
/// until the receipted `trig_operator` releases the run attributed to
/// the captured source; the same latch serves the second run, whose
/// proven `fault` takes the `on_fault_policy = 1` abort path:
/// `abort_step` reporting with `aborted` raised and `done` never
/// standing.
#[test]
fn scripted_run_demonstrates_pending_latch_and_fault_abort() {
    let model = model(BACKWASH);
    let driver = build_driver(&model);
    let mut executor = build_executor(&model, &driver);

    // Scans 1-3: the tick-0 `trig_turbidity` edge latches `pending`
    // with the source captured; `request` stays low through the latch.
    for scan_number in 1..=3 {
        run_scan(&mut executor, &driver, scan_number);
        assert!(boolean(&driver, PENDING_B));
        assert!(!boolean(&driver, REQUEST_B));
    }

    // Scan 4: the receipted `trig_operator` edge releases the latch —
    // the run arms attributed to the captured turbidity source and
    // steps under the standing grant.
    run_scan(&mut executor, &driver, 4);
    assert!(!boolean(&driver, PENDING_B));
    assert!(boolean(&driver, REQUEST_B));
    assert_eq!(int(&driver, SOURCE_B), 3);
    assert_eq!(int(&driver, STEP_B), 1);
    run_scan(&mut executor, &driver, 5);
    assert_eq!(int(&driver, STEP_B), 1);
    run_scan(&mut executor, &driver, 6);
    assert_eq!(int(&driver, STEP_B), 2);

    // Scan 7: the second timed step completes — `done` stands and
    // `request` drops; the source still reports the held attribution.
    run_scan(&mut executor, &driver, 7);
    assert!(boolean(&driver, DONE_B));
    assert!(!boolean(&driver, REQUEST_B));
    assert_eq!(int(&driver, SOURCE_B), 3);
    run_scan(&mut executor, &driver, 8);
    run_scan(&mut executor, &driver, 9);
    assert!(boolean(&driver, DONE_B));

    // Scan 10: the scripted `trig_time` edge latches `pending` again;
    // scan 11's receipted operator edge arms it attributed to source 1.
    run_scan(&mut executor, &driver, 10);
    assert!(boolean(&driver, PENDING_B));
    assert!(!boolean(&driver, REQUEST_B));
    run_scan(&mut executor, &driver, 11);
    assert!(!boolean(&driver, PENDING_B));
    assert!(boolean(&driver, REQUEST_B));
    assert!(!boolean(&driver, DONE_B));
    assert_eq!(int(&driver, SOURCE_B), 1);
    run_scan(&mut executor, &driver, 12);
    run_scan(&mut executor, &driver, 13);
    assert_eq!(int(&driver, STEP_B), 2);

    // Scan 14: the proven `fault` takes the declared abort path —
    // `abort_step` reporting, `aborted` raised, `request` dropped and
    // `done` never standing.
    run_scan(&mut executor, &driver, 14);
    assert!(boolean(&driver, ABORTED_B));
    assert!(!boolean(&driver, DONE_B));
    assert!(!boolean(&driver, REQUEST_B));
    assert_eq!(int(&driver, STEP_B), 2);
    for scan_number in 15..=17 {
        run_scan(&mut executor, &driver, scan_number);
        assert!(boolean(&driver, ABORTED_B));
        assert!(!boolean(&driver, DONE_B));
    }
}

#[test]
fn identical_scripted_runs_produce_identical_snapshots_and_journals() {
    let run = || {
        let model = model(BACKWASH);
        let driver = build_driver(&model);
        let mut executor = build_executor(&model, &driver);
        for scan_number in 1..=30 {
            run_scan(&mut executor, &driver, scan_number);
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

/// A standby assembling the same model and applying a mid-run
/// checkpoint — the armed request, the held trigger source, the
/// banked step count, and the latched `pending` on the second
/// sequence included — continues the scripted run identically to the
/// active.
#[test]
fn checkpointed_standby_continues_the_run_identically() {
    let model = model(BACKWASH);
    let driver_a = build_driver(&model);
    let mut active = build_executor(&model, &driver_a);
    for scan_number in 1..=10 {
        run_scan(&mut active, &driver_a, scan_number);
    }
    let checkpoint = active.checkpoint();

    let driver_b = build_driver(&model);
    let mut standby = build_executor(&model, &driver_b);
    standby.apply(&checkpoint).unwrap();

    for scan_number in 11..=30 {
        run_scan(&mut active, &driver_a, scan_number);
        run_scan(&mut standby, &driver_b, scan_number);
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

/// An indexed `meas` family missing a member fails assembly with
/// [`AssemblyError::UnboundPort`] naming the port.
#[test]
fn a_gap_in_the_indexed_meas_family_fails_assembly_naming_the_port() {
    let mut document: serde_json::Value = serde_json::from_str(BACKWASH).unwrap();
    let connections = document["connections"].as_array_mut().unwrap();
    connections.retain(|connection| {
        connection["to"]["port"]["name"] != "meas_1" || connection["to"]["port"]["component"] != 20
    });
    let model = model(&document.to_string());
    let driver = build_driver(&model);
    match assemble(&model, &registry(), &driver).err().unwrap() {
        AssemblyError::UnboundPort { component, port } => {
            assert_eq!(component.0, 20);
            assert_eq!(port, "meas_1");
        }
        other => panic!("expected UnboundPort, got {other:?}"),
    }
}
