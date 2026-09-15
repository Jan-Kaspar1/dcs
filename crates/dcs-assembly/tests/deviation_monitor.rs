//! Integration tests for the `deviation-monitor` kind architecture
//! decision 53 records: the checked-in fixture composes two monitors
//! over one scripted `expected`/`measured` pair — a windowed instance
//! (`window_ticks` 5, `deviation_limit` 0.1) and a window-1 instance
//! whose per-scan verdict marks the instantaneous comparison the
//! windowed one replaces.
//!
//! Scripted deterministic runs exercise the tracking window, a
//! sustained excess asserting at the window's close, recovery clearing
//! at the next window's close, the frozen-on-bad-quality rule, the
//! drawdown-shaped oscillation the window averages out while the
//! instantaneous instance trips, an under-delivery window, and
//! checkpointed-standby continuity mid-window; identical runs produce
//! identical snapshots.

use dcs_assembly::{
    AssemblyError, BuildError, ComponentRegistry, DriverRegistry, FanoutDriver, assemble,
    resolve_drivers,
};
use dcs_blocks::DeviationMonitor;
use dcs_core::{PointId, Quality, QualityReason, Sample, Tick, Value};
use dcs_model::PlantModel;
use dcs_runtime::{Component, Executor};

/// The fixture: one `sim-scripted` device replaying the commanded rate
/// and the measured consumption — tracking, a sustained excess, a
/// bad-quality stretch, the drawdown oscillation, and an under-delivery
/// run — and internal `Out` carriers for each monitor's `deviation` and
/// `deviating` (the links carry quality, which a field loopback's write
/// journal would drop).
const DEVIATION_MONITOR: &str = include_str!("../fixtures/deviation_monitor.json");

const DEVIATION_1: PointId = PointId(20);
const DEVIATING_1: PointId = PointId(21);
const DEVIATION_2: PointId = PointId(22);
const DEVIATING_2: PointId = PointId(23);

/// The `dcs-blocks` registration for the fixture's kind, mirroring the
/// controller registry's port binding.
fn registry() -> ComponentRegistry {
    ComponentRegistry::new().with(DeviationMonitor::KIND, |spec| {
        boxed(DeviationMonitor::from_parameters(
            spec.name.as_str(),
            spec.require("expected")?,
            spec.require("measured")?,
            spec.require("deviation")?,
            spec.require("deviating")?,
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
fn deviation_monitor_fixture_assembles_through_the_registry() {
    let model = model(DEVIATION_MONITOR);
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

/// The scripted run walks both instances through the windowed verdict:
/// tracking at scans 1–10, the sustained +20% excess at 11–20, recovery
/// at 21–25, the `expected` bad-quality freeze at 26–30, the
/// drawdown-shaped ±20% oscillation at 31–40, the −40% under-delivery
/// at 41–45, the stale freeze at 46–50, and the final recovery at
/// 51–55.
#[test]
fn the_scripted_run_exercises_the_windowed_verdict() {
    let model = model(DEVIATION_MONITOR);
    let driver = build_driver(&model);
    let mut executor = build_executor(&model, &driver);

    // Tracking: both monitors stay clear; the first window closes at
    // scan 5 reporting deviation 0.
    scan_to(&mut executor, &driver, 5);
    assert_eq!(float_at(&executor, DEVIATION_1), 0.0);
    assert!(!bool_at(&executor, DEVIATING_1));
    assert!(!bool_at(&executor, DEVIATING_2));

    // The excess has run four scans by scan 14: the instantaneous
    // instance is already tripped while the windowed one still waits
    // for its window — the slow-trend gap the kind exists for.
    scan_to(&mut executor, &driver, 14);
    assert!(bool_at(&executor, DEVIATING_2));
    assert!(!bool_at(&executor, DEVIATING_1));
    // The fifth excess scan closes the window: deviation +0.2 asserts.
    // The instantaneous instance has reported the same +0.2 each scan.
    scan_to(&mut executor, &driver, 15);
    assert_eq!(float_at(&executor, DEVIATION_1), 0.2);
    assert!(bool_at(&executor, DEVIATING_1));
    assert_eq!(float_at(&executor, DEVIATION_2), 0.2);

    // Measured recovers at script tick 20: the instantaneous verdict
    // clears at scan 21 while the windowed one holds until its window
    // completes at scan 25.
    scan_to(&mut executor, &driver, 21);
    assert!(!bool_at(&executor, DEVIATING_2));
    assert!(bool_at(&executor, DEVIATING_1));
    scan_to(&mut executor, &driver, 25);
    assert_eq!(float_at(&executor, DEVIATION_1), 0.0);
    assert!(!bool_at(&executor, DEVIATING_1));

    // `expected` turns bad at script tick 25: the window freezes, the
    // standing verdict holds, and both outputs carry the merged
    // quality.
    scan_to(&mut executor, &driver, 27);
    assert!(!bool_at(&executor, DEVIATING_1));
    assert_eq!(
        sample(&executor, DEVIATION_1).quality,
        Quality::Bad(QualityReason::CommunicationFault)
    );
    assert_eq!(
        sample(&executor, DEVIATING_2).quality,
        Quality::Bad(QualityReason::CommunicationFault)
    );

    // The drawdown shape — measured oscillating ±20% around the
    // command — trips the instantaneous instance on every off-nominal
    // scan while each five-scan window's accumulated quantities
    // cancel: the windowed verdict stays clear.
    scan_to(&mut executor, &driver, 34);
    assert!(bool_at(&executor, DEVIATING_2));
    assert!(!bool_at(&executor, DEVIATING_1));
    scan_to(&mut executor, &driver, 35);
    assert!(!bool_at(&executor, DEVIATING_2));
    assert_eq!(float_at(&executor, DEVIATION_1), 0.0);

    // Under-delivery — measured 6 against commanded 10 — trips the
    // fifth banked scan of the window at −0.4.
    scan_to(&mut executor, &driver, 45);
    assert_eq!(float_at(&executor, DEVIATION_1), -0.4);
    assert!(bool_at(&executor, DEVIATING_1));
    assert!(bool_at(&executor, DEVIATING_2));

    // The stale stretch freezes the asserted verdict mid-window,
    // quality-stamped.
    scan_to(&mut executor, &driver, 47);
    assert!(bool_at(&executor, DEVIATING_1));
    assert_eq!(
        sample(&executor, DEVIATING_1).quality,
        Quality::Uncertain(QualityReason::Stale)
    );

    // Recovery: the instantaneous instance clears on the first trusted
    // scan; the windowed one needs its five banked scans and clears at
    // the close, scan 55.
    scan_to(&mut executor, &driver, 51);
    assert!(!bool_at(&executor, DEVIATING_2));
    assert!(bool_at(&executor, DEVIATING_1));
    scan_to(&mut executor, &driver, 55);
    assert!(!bool_at(&executor, DEVIATING_1));
    assert_eq!(float_at(&executor, DEVIATION_1), 0.0);
}

/// A standby assembling the same model and applying a mid-window
/// checkpoint — the under-delivery window half accumulated, the
/// instantaneous instance asserted — continues the scripted run
/// identically to the active.
#[test]
fn checkpointed_standby_mid_window_continues_identically() {
    let model = model(DEVIATION_MONITOR);
    let driver_a = build_driver(&model);
    let mut active = build_executor(&model, &driver_a);

    // Scan 43 sits inside the under-delivery window: three of five
    // pairs banked — the state the checkpoint must carry.
    scan_to(&mut active, &driver_a, 43);
    assert!(!bool_at(&active, DEVIATING_1));
    assert!(bool_at(&active, DEVIATING_2));
    let checkpoint = active.checkpoint();

    let driver_b = build_driver(&model);
    let mut standby = build_executor(&model, &driver_b);
    standby.apply(&checkpoint).unwrap();
    assert!(!bool_at(&standby, DEVIATING_1));
    assert!(bool_at(&standby, DEVIATING_2));

    // Through the window's close, the stale freeze, and the final
    // recovery both run identically.
    for _ in 0..17 {
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
        let model = model(DEVIATION_MONITOR);
        let driver = build_driver(&model);
        let mut executor = build_executor(&model, &driver);
        for _ in 0..60 {
            scan(&mut executor, &driver);
        }
        serde_json::to_string(&executor.snapshot()).unwrap()
    };
    assert_eq!(run(), run());
}

/// A parameter outside the declared set — a zero `window_ticks` —
/// fails assembly naming the offending parameter.
#[test]
fn a_malformed_window_fails_assembly_naming_the_parameter() {
    let mut document: serde_json::Value = serde_json::from_str(DEVIATION_MONITOR).unwrap();
    document["components"][0]["parameters"]["window_ticks"] = serde_json::json!({ "int": 0 });
    let model = model(&document.to_string());
    let driver = build_driver(&model);
    match assemble(&model, &registry(), &driver).err().unwrap() {
        AssemblyError::Component {
            component, detail, ..
        } => {
            assert_eq!(component.0, 1);
            assert!(
                detail.contains("window_ticks"),
                "the failure should name the offending parameter, found {detail}"
            );
        }
        other => panic!("expected AssemblyError::Component, got {other:?}"),
    }
}
