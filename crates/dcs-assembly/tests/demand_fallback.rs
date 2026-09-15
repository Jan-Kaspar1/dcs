//! Integration tests for the `demand-fallback` kind architecture
//! decision 65 records: the checked-in fixture composes three
//! instances over one scripted input set — one per `on_bad` code (0
//! hold the last `Good`-stamped demand, 1 drive `fallback_flow` 25, 2
//! drive `safe_flow` 5) — so one script exercises every declared
//! response.
//!
//! The scripted run demonstrates the demand passing unmodified while
//! the selected `pv` reads `Good`, each `on_bad` code answering a
//! non-`Good` `pv` with `fallback_active` asserted and `out` carrying
//! the merged worst-of quality, the held demand freezing while a
//! still-`Good` `in` moves during the engagement, and a recovering
//! `pv` resuming pass-through; checkpointed standby and determinism
//! hold.

use dcs_assembly::{
    AssemblyError, BuildError, ComponentRegistry, DriverRegistry, FanoutDriver, assemble,
    resolve_drivers,
};
use dcs_blocks::{DemandFallback, DemandFallbackIo};
use dcs_core::{PointId, Quality, QualityReason, Sample, Tick, Value};
use dcs_model::PlantModel;
use dcs_runtime::{Component, Executor};

/// The fixture: one `sim-scripted` device replaying the demand and
/// selected-measurement inputs — and internal `Out` carriers for each
/// instance's `out` and `fallback_active` (the links carry quality,
/// which a field loopback's write journal would drop).
const DEMAND_FALLBACK: &str = include_str!("../fixtures/demand_fallback.json");

const OUT_HOLD: PointId = PointId(20);
const ACTIVE_HOLD: PointId = PointId(21);
const OUT_FIXED: PointId = PointId(22);
const ACTIVE_FIXED: PointId = PointId(23);
const OUT_SAFE: PointId = PointId(24);
const ACTIVE_SAFE: PointId = PointId(25);

/// The `dcs-blocks` registration for the fixture's kind, mirroring the
/// controller registry's port binding.
fn registry() -> ComponentRegistry {
    ComponentRegistry::new().with(DemandFallback::KIND, |spec| {
        boxed(DemandFallback::from_parameters(
            spec.name.as_str(),
            DemandFallbackIo {
                input: spec.require("in")?,
                pv: spec.require("pv")?,
                out: spec.require("out")?,
                fallback_active: spec.require("fallback_active")?,
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
fn demand_fallback_fixture_assembles_through_the_registry() {
    let model = model(DEMAND_FALLBACK);
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
    assert_eq!(snapshot.components.len(), 3);
    for descriptor in &snapshot.descriptors {
        assert_eq!(descriptor.ports.len(), 4);
    }
}

/// The scripted run: pass-through while `pv` reads `Good`, each
/// `on_bad` code answering a non-`Good` `pv` — the hold freezing the
/// last `Good`-stamped demand, the fixed and safe codes driving their
/// declared flows — `fallback_active` asserted for each engagement,
/// `out` carrying the merged worst-of quality, and recovery resuming
/// pass-through.
#[test]
fn the_scripted_run_exercises_each_on_bad_code() {
    let model = model(DEMAND_FALLBACK);
    let driver = build_driver(&model);
    let mut executor = build_executor(&model, &driver);

    // `pv` Good: the 42 demand passes on all three instances.
    scan_to(&mut executor, &driver, 5);
    for out in [OUT_HOLD, OUT_FIXED, OUT_SAFE] {
        assert_eq!(float_at(&executor, out), 42.0);
        assert_eq!(sample(&executor, out).quality, Quality::Good);
    }
    for active in [ACTIVE_HOLD, ACTIVE_FIXED, ACTIVE_SAFE] {
        assert!(!bool_at(&executor, active));
    }

    // `pv` went `Bad` at script tick 10: the engagement stands —
    // hold serves the last `Good`-stamped 42, the fixed code 25, the
    // safe code 5 — each flagged, each `out` carrying the fault.
    scan_to(&mut executor, &driver, 15);
    assert_eq!(float_at(&executor, OUT_HOLD), 42.0);
    assert_eq!(float_at(&executor, OUT_FIXED), 25.0);
    assert_eq!(float_at(&executor, OUT_SAFE), 5.0);
    for active in [ACTIVE_HOLD, ACTIVE_FIXED, ACTIVE_SAFE] {
        assert!(bool_at(&executor, active));
        assert_eq!(sample(&executor, active).quality, Quality::Good);
    }
    assert_eq!(
        sample(&executor, OUT_HOLD).quality,
        Quality::Bad(QualityReason::CommunicationFault)
    );

    // The demand moves to 55 while `pv` stays `Bad`: the hold is
    // frozen — the last demand the kind stamped `Good` — while the
    // fixed codes keep their declared flows.
    scan_to(&mut executor, &driver, 25);
    assert_eq!(float_at(&executor, OUT_HOLD), 42.0);
    assert_eq!(float_at(&executor, OUT_FIXED), 25.0);
    assert_eq!(float_at(&executor, OUT_SAFE), 5.0);

    // `pv` recovers at script tick 30: pass-through resumes the same
    // scan it reads `Good` — no latch — serving the standing 55.
    scan_to(&mut executor, &driver, 35);
    for out in [OUT_HOLD, OUT_FIXED, OUT_SAFE] {
        assert_eq!(float_at(&executor, out), 55.0);
        assert_eq!(sample(&executor, out).quality, Quality::Good);
    }
    assert!(!bool_at(&executor, ACTIVE_HOLD));

    // An `Uncertain` `pv` engages the same responses: the hold now
    // serves 55 — refreshed during the recovery — and `out` carries
    // the `Uncertain` half of the merge.
    scan_to(&mut executor, &driver, 45);
    assert_eq!(float_at(&executor, OUT_HOLD), 55.0);
    assert_eq!(float_at(&executor, OUT_FIXED), 25.0);
    assert_eq!(float_at(&executor, OUT_SAFE), 5.0);
    assert!(bool_at(&executor, ACTIVE_HOLD));
    assert_eq!(
        sample(&executor, OUT_HOLD).quality,
        Quality::Uncertain(QualityReason::Stale)
    );

    // A `Bad` demand under the still-`Uncertain` `pv`: the engagement
    // stands and the worse half wins the merge.
    scan_to(&mut executor, &driver, 47);
    assert_eq!(
        sample(&executor, OUT_HOLD).quality,
        Quality::Bad(QualityReason::CommunicationFault)
    );

    // The demand reads `Good` again while `pv` stays `Uncertain`:
    // quality relaxes to the `pv` half and the hold stays put.
    scan_to(&mut executor, &driver, 50);
    assert_eq!(float_at(&executor, OUT_HOLD), 55.0);
    assert_eq!(
        sample(&executor, OUT_HOLD).quality,
        Quality::Uncertain(QualityReason::Stale)
    );

    // `pv` recovers at script tick 60 alongside the demand's move to
    // 30: every instance passes the standing demand.
    scan_to(&mut executor, &driver, 65);
    for out in [OUT_HOLD, OUT_FIXED, OUT_SAFE] {
        assert_eq!(float_at(&executor, out), 30.0);
        assert_eq!(sample(&executor, out).quality, Quality::Good);
    }
    for active in [ACTIVE_HOLD, ACTIVE_FIXED, ACTIVE_SAFE] {
        assert!(!bool_at(&executor, active));
    }
}

/// A standby assembling the same model and applying a mid-run
/// checkpoint — the hold engaged with its frozen demand while `in`
/// has moved on — continues the scripted run identically to the
/// active.
#[test]
fn checkpointed_standby_mid_fallback_continues_identically() {
    let model = model(DEMAND_FALLBACK);
    let driver_a = build_driver(&model);
    let mut active = build_executor(&model, &driver_a);

    // The checkpoint lands inside the first engagement: the hold
    // serves 42 while the demand already reads 55 — the held demand
    // is the run state a standby must inherit.
    scan_to(&mut active, &driver_a, 25);
    assert_eq!(float_at(&active, OUT_HOLD), 42.0);
    assert!(bool_at(&active, ACTIVE_HOLD));
    let checkpoint = active.checkpoint();

    let driver_b = build_driver(&model);
    let mut standby = build_executor(&model, &driver_b);
    standby.apply(&checkpoint).unwrap();

    for _ in 0..70 {
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
        let model = model(DEMAND_FALLBACK);
        let driver = build_driver(&model);
        let mut executor = build_executor(&model, &driver);
        for _ in 0..75 {
            scan(&mut executor, &driver);
        }
        serde_json::to_string(&executor.snapshot()).unwrap()
    };
    assert_eq!(run(), run());
}

/// A parameter outside the declared set — an `on_bad` naming no
/// response — fails assembly naming the offending parameter.
#[test]
fn a_malformed_on_bad_fails_assembly_naming_the_parameter() {
    let mut document: serde_json::Value = serde_json::from_str(DEMAND_FALLBACK).unwrap();
    document["components"][0]["parameters"]["on_bad"] = serde_json::json!({ "int": 4 });
    let model = model(&document.to_string());
    let driver = build_driver(&model);
    match assemble(&model, &registry(), &driver).err().unwrap() {
        AssemblyError::Component {
            component, detail, ..
        } => {
            assert_eq!(component.0, 1);
            assert!(
                detail.contains("on_bad"),
                "the failure should name the offending parameter, found {detail}"
            );
        }
        other => panic!("expected AssemblyError::Component, got {other:?}"),
    }
}
