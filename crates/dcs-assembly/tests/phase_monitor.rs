//! Integration tests for the `phase-monitor` kind architecture
//! decision 61 records: the checked-in fixture composes the two
//! post-wash verification checks over scripted channels — a mode-0
//! ripening instance (`bound` 0.5, `limit_ticks` 5) on the turbidity
//! channel and post-wash window, and a mode-1 CBHL instance (`bound`
//! 0.25, `limit_ticks` 5) on the headloss channel, reference-flow
//! window, and baseline-capture request.
//!
//! Scripted deterministic runs exercise phase-window gating, the
//! absolute-bound excursion and the met-latch holding `overdue` clear
//! on a re-excursion, the deadline asserting on an unmet window and
//! clearing on a late meet, the capture-then-deviate sequence with a
//! held baseline, the unverifiable no-capture window flagging
//! `overdue`, the held-scan rule on each untrusted input, and
//! checkpointed-standby continuity mid-window; identical runs produce
//! identical snapshots.

use dcs_assembly::{
    AssemblyError, BuildError, ComponentRegistry, DriverRegistry, FanoutDriver, assemble,
    resolve_drivers,
};
use dcs_blocks::{PhaseMonitor, PhaseMonitorIo};
use dcs_core::{PointId, Quality, QualityReason, Sample, Tick, Value};
use dcs_model::PlantModel;
use dcs_runtime::{Component, Executor};

/// The fixture: one `sim-scripted` device replaying the turbidity and
/// headloss measurements, the two condition windows, and the
/// reference-flow capture request — and internal `Out` carriers for
/// each monitor's `deviation`, `exceeded`, and `overdue` (the links
/// carry quality, which a field loopback's write journal would drop).
const PHASE_MONITOR: &str = include_str!("../fixtures/phase_monitor.json");

const DEVIATION_1: PointId = PointId(20);
const EXCEEDED_1: PointId = PointId(21);
const OVERDUE_1: PointId = PointId(22);
const DEVIATION_2: PointId = PointId(23);
const EXCEEDED_2: PointId = PointId(24);
const OVERDUE_2: PointId = PointId(25);

/// The `dcs-blocks` registration for the fixture's kind, mirroring the
/// controller registry's port binding.
fn registry() -> ComponentRegistry {
    ComponentRegistry::new().with(PhaseMonitor::KIND, |spec| {
        boxed(PhaseMonitor::from_parameters(
            spec.name.as_str(),
            PhaseMonitorIo {
                input: spec.require("in")?,
                phase: spec.require("phase")?,
                capture: spec.require("capture")?,
                deviation: spec.require("deviation")?,
                exceeded: spec.require("exceeded")?,
                overdue: spec.require("overdue")?,
            },
            spec.parameters,
        ))
    })
}

fn boxed<C, E>(result: Result<C, E>) -> Result<Box<dyn Component>, BuildError>
where
    C: Component + 'static,
    E: std::error::Error + 'static,
{
    result
        .map(|component| Box::new(component) as Box<dyn Component>)
        .map_err(BuildError::other)
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

/// The image sample the last scan left on `point` — the internal
/// carriers' read path.
fn sample(executor: &Executor, point: PointId) -> Sample {
    executor
        .snapshot()
        .points
        .iter()
        .find(|telemetry| telemetry.point == point)
        .and_then(|telemetry| telemetry.sample)
        .unwrap_or_else(|| panic!("point {point:?} has no sample"))
}

fn float_at(executor: &Executor, point: PointId) -> f64 {
    match sample(executor, point).value {
        Value::Float(value) => value,
        value => panic!("point {point:?}: expected Float, got {value:?}"),
    }
}

fn bool_at(executor: &Executor, point: PointId) -> bool {
    match sample(executor, point).value {
        Value::Bool(value) => value,
        value => panic!("point {point:?}: expected Bool, got {value:?}"),
    }
}

/// The scripted run. Each scan reads the scripted entries the driver's
/// last `step` made current: the tick-`t` entry is first read at scan
/// `t + 1`.
fn scan(executor: &mut Executor, driver: &FanoutDriver) {
    executor.scan();
    driver.step(0.1).unwrap();
}

/// Scans until `executor`'s tick reaches `target`.
fn scan_to(executor: &mut Executor, driver: &FanoutDriver, target: u64) {
    while executor.snapshot().tick < Tick(target) {
        scan(executor, driver);
    }
}

#[test]
fn phase_monitor_fixture_assembles_through_the_registry() {
    let model = model(PHASE_MONITOR);
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
    assert_eq!(snapshot.components.len(), 2);
}

/// The scripted run walks the ripening instance through quiescence, a
/// met window with a re-excursion, a window unmet to its deadline and
/// met late, and the held-scan freeze; the CBHL instance through the
/// capture-then-deviate sequence, a stale stretch, and a window whose
/// `capture` never runs.
#[test]
fn the_scripted_run_exercises_both_verification_modes() {
    let model = model(PHASE_MONITOR);
    let driver = build_driver(&model);
    let mut executor = build_executor(&model, &driver);

    // Quiescent: turbidity reads 2.0 — already past the 0.5 bound —
    // but no window stands, so nothing reports. The absolute-mode
    // instance never captures, so its `deviation` reports 0
    // throughout.
    scan_to(&mut executor, &driver, 5);
    assert!(!bool_at(&executor, EXCEEDED_1));
    assert!(!bool_at(&executor, OVERDUE_1));
    assert_eq!(float_at(&executor, DEVIATION_1), 0.0);
    assert!(!bool_at(&executor, EXCEEDED_2));

    // Both windows open at scan 6. The ripening instance trips at
    // once — in above bound — while the CBHL instance captures: its
    // baseline tracks the 1.0 headloss and `deviation` reports 0.
    scan_to(&mut executor, &driver, 8);
    assert!(bool_at(&executor, EXCEEDED_1));
    assert!(!bool_at(&executor, OVERDUE_1));
    assert_eq!(float_at(&executor, DEVIATION_2), 0.0);
    assert!(!bool_at(&executor, EXCEEDED_2));

    // Scan 9: turbidity meets the bound — `exceeded` clears and `met`
    // latches. The CBHL capture has released; the baseline holds 1.0.
    scan_to(&mut executor, &driver, 10);
    assert!(!bool_at(&executor, EXCEEDED_1));
    assert_eq!(float_at(&executor, DEVIATION_2), 0.0);

    // Scan 11: headloss 1.5 against baseline 1.0 — deviation 0.5 past
    // the 0.25 bound trips `exceeded`.
    scan_to(&mut executor, &driver, 12);
    assert_eq!(float_at(&executor, DEVIATION_2), 0.5);
    assert!(bool_at(&executor, EXCEEDED_2));

    // Scan 12+: turbidity re-exceeds at 0.7 — `exceeded` re-asserts —
    // but the latched `met` keeps `overdue` clear even once the window
    // has stood past its five-scan deadline.
    scan_to(&mut executor, &driver, 14);
    assert!(bool_at(&executor, EXCEEDED_1));
    assert!(!bool_at(&executor, OVERDUE_1));
    // Headloss back at 1.125: deviation 0.125 within bound — clear.
    assert_eq!(float_at(&executor, DEVIATION_2), 0.125);
    assert!(!bool_at(&executor, EXCEEDED_2));

    // Both first windows close (ripening at scan 15, CBHL at 17): the
    // CBHL monitor returns to quiescent, its baseline dropped — while
    // the second ripening window opens at scan 18 with turbidity
    // still past bound, `exceeded` standing from the opening scan.
    scan_to(&mut executor, &driver, 19);
    assert!(bool_at(&executor, EXCEEDED_1));
    assert!(!bool_at(&executor, EXCEEDED_2));
    assert_eq!(float_at(&executor, DEVIATION_2), 0.0);

    // The deadline counts through the open window — scan 22 is
    // elapsed 5 of 5, still in grace.
    scan_to(&mut executor, &driver, 22);
    assert!(bool_at(&executor, EXCEEDED_1));
    assert!(!bool_at(&executor, OVERDUE_1));

    // Scan 23 is the first scan past the deadline unmet: `overdue`
    // asserts alongside `exceeded`.
    scan_to(&mut executor, &driver, 23);
    assert!(bool_at(&executor, OVERDUE_1));
    assert!(bool_at(&executor, EXCEEDED_1));

    // Turbidity meets the bound late at scan 25 (0.4): both flags
    // clear — the deadline's answer is the alarm set's record, not a
    // latch here.
    scan_to(&mut executor, &driver, 26);
    assert!(!bool_at(&executor, OVERDUE_1));
    assert!(!bool_at(&executor, EXCEEDED_1));

    // Meanwhile the second CBHL window opened at scan 21, captured
    // the 2.0 headloss at scan 22, and released the request at 24 —
    // and from scan 25 the 2.5 headloss reports the held baseline's
    // 0.5 deviation, `exceeded` standing.
    assert_eq!(float_at(&executor, DEVIATION_2), 0.5);
    assert!(bool_at(&executor, EXCEEDED_2));

    // The held scans: `phase` reports Bad at script tick 26 and `in`
    // follows at 27 — the window neither advances nor closes, the
    // standing verdicts hold stamped with the merged quality.
    scan_to(&mut executor, &driver, 28);
    assert_eq!(
        sample(&executor, OVERDUE_1).quality,
        Quality::Bad(QualityReason::CommunicationFault)
    );
    assert!(!bool_at(&executor, OVERDUE_1));

    // Trusted scans resume at 31: the window still stands, `met`
    // still latched — and the headloss back at 2.125 reports the held
    // baseline's 0.125 deviation until its stale stretch holds the
    // scan.
    scan_to(&mut executor, &driver, 31);
    assert_eq!(sample(&executor, EXCEEDED_1).quality, Quality::Good);
    assert!(!bool_at(&executor, EXCEEDED_1));
    assert_eq!(float_at(&executor, DEVIATION_2), 0.125);
    assert!(!bool_at(&executor, EXCEEDED_2));

    scan_to(&mut executor, &driver, 33);
    assert_eq!(
        sample(&executor, DEVIATION_2).quality,
        Quality::Uncertain(QualityReason::Stale)
    );

    // Both windows close (ripening at 33, CBHL at 35); the third CBHL
    // window opens at scan 41 with no capture request — nothing can
    // meet the bound, so the deadline flags the unverified window at
    // elapsed 6, scan 46, and `deviation` reports the uncaptured 0.
    scan_to(&mut executor, &driver, 36);
    assert!(!bool_at(&executor, EXCEEDED_2));
    assert_eq!(float_at(&executor, DEVIATION_2), 0.0);

    scan_to(&mut executor, &driver, 45);
    assert!(!bool_at(&executor, OVERDUE_2));
    assert!(!bool_at(&executor, EXCEEDED_2));
    scan_to(&mut executor, &driver, 47);
    assert!(bool_at(&executor, OVERDUE_2));
    assert_eq!(float_at(&executor, DEVIATION_2), 0.0);

    // The last window closes at scan 49: quiescent again.
    scan_to(&mut executor, &driver, 50);
    assert!(!bool_at(&executor, OVERDUE_2));
}

/// A standby assembling the same model and applying a mid-window
/// checkpoint — the ripening deadline already elapsed unmet and the
/// CBHL baseline just released — continues the scripted run
/// identically to the active.
#[test]
fn checkpointed_standby_mid_window_continues_identically() {
    let model = model(PHASE_MONITOR);
    let driver_a = build_driver(&model);
    let mut active = build_executor(&model, &driver_a);

    // Scan 24 sits inside both windows: the ripening instance stands
    // `overdue` with seven banked scans, the CBHL instance holds its
    // 2.0 baseline at four — the state the checkpoint must carry.
    scan_to(&mut active, &driver_a, 24);
    assert!(bool_at(&active, OVERDUE_1));
    assert!(bool_at(&active, EXCEEDED_1));
    let checkpoint = active.checkpoint();

    let driver_b = build_driver(&model);
    let mut standby = build_executor(&model, &driver_b);
    standby.apply(&checkpoint).unwrap();
    assert!(bool_at(&standby, OVERDUE_1));
    assert!(bool_at(&standby, EXCEEDED_1));

    // Through the late meet, the held scans, the CBHL excursions, the
    // unverifiable third window, and every close both run identically.
    for _ in 0..30 {
        scan(&mut active, &driver_a);
        scan(&mut standby, &driver_b);
    }
    assert_eq!(
        serde_json::to_string(&active.snapshot()).unwrap(),
        serde_json::to_string(&standby.snapshot()).unwrap()
    );
}

#[test]
fn identical_scripted_runs_produce_identical_snapshots() {
    let run = || {
        let model = model(PHASE_MONITOR);
        let driver = build_driver(&model);
        let mut executor = build_executor(&model, &driver);
        for _ in 0..55 {
            scan(&mut executor, &driver);
        }
        serde_json::to_string(&executor.snapshot()).unwrap()
    };
    assert_eq!(run(), run());
}

/// A parameter outside the declared set — a `mode` naming no check —
/// fails assembly naming the offending parameter.
#[test]
fn a_malformed_mode_fails_assembly_naming_the_parameter() {
    let mut document: serde_json::Value = serde_json::from_str(PHASE_MONITOR).unwrap();
    document["components"][0]["parameters"]["mode"] = serde_json::json!({ "int": 4 });
    let model = model(&document.to_string());
    let driver = build_driver(&model);
    match assemble(&model, &registry(), &driver).err().unwrap() {
        AssemblyError::Component {
            component, detail, ..
        } => {
            assert_eq!(component.0, 1);
            assert!(
                detail.contains("mode"),
                "the failure should name the offending parameter, found {detail}"
            );
        }
        other => panic!("expected AssemblyError::Component, got {other:?}"),
    }
}
