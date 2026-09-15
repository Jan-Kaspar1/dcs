//! Integration tests for the `surge-guard` kind architecture decision
//! 64 records: the checked-in fixture composes two guards over one
//! scripted input set — a clamp instance (`on_guard` 0, `min_flow` 50,
//! `max_pressure` 30, `min_current` 40, `trip_value` 0) with the
//! motor-current proxy bound, and a trip instance (`on_guard` 1) with
//! `current` left unbound, so one script exercises both declared
//! responses and the optional port's bound and unbound paths.
//!
//! The scripted run demonstrates demand passing unmodified inside the
//! region, each bound crossing clamping or tripping per `on_guard`
//! with `guarding`/`tripped` reporting which fired, a proven
//! `surge_trip` driving `trip_value` whatever the demand, the
//! unwired-`current` instance ignoring the amperage proxy, the
//! declared fail-safe on each measurement input, and the strict
//! boundary comparisons; checkpointed standby and determinism hold.

use dcs_assembly::{
    AssemblyError, BuildError, ComponentRegistry, DriverRegistry, FanoutDriver, assemble,
    resolve_drivers,
};
use dcs_blocks::{SurgeGuard, SurgeGuardIo};
use dcs_core::{PointId, Quality, QualityReason, Sample, Tick, Value};
use dcs_model::PlantModel;
use dcs_runtime::{Component, Executor};

/// The fixture: one `sim-scripted` device replaying the demand, flow,
/// pressure, current, and proven-trip inputs — and internal `Out`
/// carriers for each guard's `out`, `guarding`, and `tripped` (the
/// links carry quality, which a field loopback's write journal would
/// drop).
const SURGE_GUARD: &str = include_str!("../fixtures/surge_guard.json");

const OUT_1: PointId = PointId(20);
const GUARDING_1: PointId = PointId(21);
const TRIPPED_1: PointId = PointId(22);
const OUT_2: PointId = PointId(23);
const GUARDING_2: PointId = PointId(24);
const TRIPPED_2: PointId = PointId(25);

/// The `dcs-blocks` registration for the fixture's kind, mirroring the
/// controller registry's port binding — `current` through `get`, the
/// optional port.
fn registry() -> ComponentRegistry {
    ComponentRegistry::new().with(SurgeGuard::KIND, |spec| {
        boxed(SurgeGuard::from_parameters(
            spec.name.as_str(),
            SurgeGuardIo {
                demand: spec.require("demand")?,
                flow: spec.require("flow")?,
                pressure: spec.require("pressure")?,
                current: spec.get("current"),
                surge_trip: spec.require("surge_trip")?,
                out: spec.require("out")?,
                guarding: spec.require("guarding")?,
                tripped: spec.require("tripped")?,
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
fn surge_guard_fixture_assembles_through_the_registry() {
    let model = model(SURGE_GUARD);
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
    // The clamp instance binds `current`; the trip instance leaves the
    // optional port undeclared.
    assert_eq!(snapshot.descriptors[0].ports.len(), 8);
    assert_eq!(snapshot.descriptors[1].ports.len(), 7);
}

/// The scripted run: pass-through inside the region, each bound
/// crossing under both `on_guard` codes, the bound/unbound `current`
/// difference, the unconditional proven trip, the strict boundary
/// comparisons, and every measurement's declared fail-safe.
#[test]
fn the_scripted_run_exercises_both_guard_responses() {
    let model = model(SURGE_GUARD);
    let driver = build_driver(&model);
    let mut executor = build_executor(&model, &driver);

    // Inside the region the demand passes unmodified on both
    // instances.
    scan_to(&mut executor, &driver, 5);
    assert_eq!(float_at(&executor, OUT_1), 70.0);
    assert_eq!(float_at(&executor, OUT_2), 70.0);
    assert!(!bool_at(&executor, GUARDING_1));
    assert!(!bool_at(&executor, GUARDING_2));

    // The flow bound crosses (40 < 50): the clamp instance floors the
    // demand at `min_flow` — already above it, so 70 passes — while
    // the trip instance drives `trip_value`.
    scan_to(&mut executor, &driver, 8);
    assert_eq!(float_at(&executor, OUT_1), 70.0);
    assert!(bool_at(&executor, GUARDING_1));
    assert!(!bool_at(&executor, TRIPPED_1));
    assert_eq!(float_at(&executor, OUT_2), 0.0);
    assert!(bool_at(&executor, GUARDING_2));
    assert!(bool_at(&executor, TRIPPED_2));

    // Clear again — neither state latches.
    scan_to(&mut executor, &driver, 11);
    assert_eq!(float_at(&executor, OUT_1), 70.0);
    assert_eq!(float_at(&executor, OUT_2), 70.0);
    assert!(!bool_at(&executor, GUARDING_2));

    // The pressure bound crosses (35 > 30): the clamp caps `out` at
    // `max_pressure`; the trip instance trips.
    scan_to(&mut executor, &driver, 14);
    assert_eq!(float_at(&executor, OUT_1), 30.0);
    assert!(bool_at(&executor, GUARDING_1));
    assert_eq!(float_at(&executor, OUT_2), 0.0);
    assert!(bool_at(&executor, TRIPPED_2));

    scan_to(&mut executor, &driver, 17);
    assert_eq!(float_at(&executor, OUT_1), 70.0);
    assert_eq!(float_at(&executor, OUT_2), 70.0);

    // The current bound crosses (30 < 40): only the bound instance
    // sees it — `guarding` stands on the clamp instance (the 70 demand
    // sits above the 40 floor) while the unwired instance passes
    // clear of it.
    scan_to(&mut executor, &driver, 20);
    assert_eq!(float_at(&executor, OUT_1), 70.0);
    assert!(bool_at(&executor, GUARDING_1));
    assert_eq!(float_at(&executor, OUT_2), 70.0);
    assert!(!bool_at(&executor, GUARDING_2));

    // The proven surge trip: `surge_trip` true drives `trip_value`
    // unconditionally — every bound clear, the demand still asking.
    scan_to(&mut executor, &driver, 26);
    assert_eq!(float_at(&executor, OUT_1), 0.0);
    assert_eq!(float_at(&executor, OUT_2), 0.0);
    assert!(!bool_at(&executor, GUARDING_1));
    assert!(bool_at(&executor, TRIPPED_1));
    assert!(bool_at(&executor, TRIPPED_2));

    // The device reports clear and the guard auto-resets.
    scan_to(&mut executor, &driver, 29);
    assert_eq!(float_at(&executor, OUT_1), 70.0);
    assert!(!bool_at(&executor, TRIPPED_1));
    assert!(!bool_at(&executor, TRIPPED_2));

    // An untrusted demand can neither pass nor be bounded: both
    // instances drive `trip_value` and report `tripped`, `out`
    // carrying the input's quality.
    scan_to(&mut executor, &driver, 32);
    assert_eq!(float_at(&executor, OUT_1), 0.0);
    assert_eq!(float_at(&executor, OUT_2), 0.0);
    assert!(bool_at(&executor, TRIPPED_1));
    assert_eq!(
        sample(&executor, OUT_1).quality,
        Quality::Bad(QualityReason::CommunicationFault)
    );

    scan_to(&mut executor, &driver, 35);
    assert_eq!(float_at(&executor, OUT_1), 70.0);
    assert!(!bool_at(&executor, TRIPPED_1));

    // An untrusted flow reads as its bound crossed: the clamp
    // instance guards (demand above the floor), the trip instance
    // trips, and `out` carries the fault.
    scan_to(&mut executor, &driver, 38);
    assert_eq!(float_at(&executor, OUT_1), 70.0);
    assert!(bool_at(&executor, GUARDING_1));
    assert_eq!(
        sample(&executor, OUT_1).quality,
        Quality::Bad(QualityReason::CommunicationFault)
    );
    assert_eq!(float_at(&executor, OUT_2), 0.0);
    assert!(bool_at(&executor, TRIPPED_2));

    scan_to(&mut executor, &driver, 41);
    assert_eq!(float_at(&executor, OUT_2), 70.0);
    assert!(!bool_at(&executor, GUARDING_1));

    // An untrusted current reads below its bound — the clamp instance
    // alone guards; the unwired instance's `out` keeps its inputs'
    // `Good` quality.
    scan_to(&mut executor, &driver, 44);
    assert!(bool_at(&executor, GUARDING_1));
    assert_eq!(
        sample(&executor, OUT_1).quality,
        Quality::Bad(QualityReason::CommunicationFault)
    );
    assert_eq!(float_at(&executor, OUT_2), 70.0);
    assert_eq!(sample(&executor, OUT_2).quality, Quality::Good);

    // An untrusted `surge_trip` cannot prove the device stands down:
    // it reads as proven and both instances trip.
    scan_to(&mut executor, &driver, 50);
    assert_eq!(float_at(&executor, OUT_1), 0.0);
    assert_eq!(float_at(&executor, OUT_2), 0.0);
    assert!(bool_at(&executor, TRIPPED_1));
    assert!(bool_at(&executor, TRIPPED_2));

    scan_to(&mut executor, &driver, 53);
    assert_eq!(float_at(&executor, OUT_1), 70.0);
    assert!(!bool_at(&executor, TRIPPED_1));

    // An untrusted pressure reads as its bound crossed: the clamp
    // caps at `max_pressure`, the trip instance trips.
    scan_to(&mut executor, &driver, 56);
    assert_eq!(float_at(&executor, OUT_1), 30.0);
    assert!(bool_at(&executor, GUARDING_1));
    assert_eq!(float_at(&executor, OUT_2), 0.0);
    assert!(bool_at(&executor, TRIPPED_2));

    // A demand below the floor with flow crossed: the clamp actually
    // lifts `out` to `min_flow` — the minimum-flow maintenance
    // direction.
    scan_to(&mut executor, &driver, 62);
    assert_eq!(float_at(&executor, OUT_1), 50.0);
    assert!(bool_at(&executor, GUARDING_1));
    assert_eq!(float_at(&executor, OUT_2), 0.0);
    assert!(bool_at(&executor, TRIPPED_2));

    // Every bound met exactly — flow at `min_flow`, pressure at
    // `max_pressure`, current at `min_current` — is outside the
    // region: the strict comparisons pass the demand through.
    scan_to(&mut executor, &driver, 65);
    assert_eq!(float_at(&executor, OUT_1), 45.0);
    assert_eq!(float_at(&executor, OUT_2), 45.0);
    assert!(!bool_at(&executor, GUARDING_1));
    assert!(!bool_at(&executor, GUARDING_2));

    scan_to(&mut executor, &driver, 68);
    assert_eq!(float_at(&executor, OUT_1), 70.0);
    assert_eq!(float_at(&executor, OUT_2), 70.0);
}

/// A standby assembling the same model and applying a mid-run
/// checkpoint — the flow bound crossed, the clamp standing and the
/// trip asserted — continues the scripted run identically to the
/// active.
#[test]
fn checkpointed_standby_mid_guard_continues_identically() {
    let model = model(SURGE_GUARD);
    let driver_a = build_driver(&model);
    let mut active = build_executor(&model, &driver_a);

    scan_to(&mut active, &driver_a, 8);
    assert!(bool_at(&active, GUARDING_1));
    assert!(bool_at(&active, TRIPPED_2));
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
        let model = model(SURGE_GUARD);
        let driver = build_driver(&model);
        let mut executor = build_executor(&model, &driver);
        for _ in 0..75 {
            scan(&mut executor, &driver);
        }
        serde_json::to_string(&executor.snapshot()).unwrap()
    };
    assert_eq!(run(), run());
}

/// A parameter outside the declared set — an `on_guard` naming no
/// response — fails assembly naming the offending parameter.
#[test]
fn a_malformed_on_guard_fails_assembly_naming_the_parameter() {
    let mut document: serde_json::Value = serde_json::from_str(SURGE_GUARD).unwrap();
    document["components"][0]["parameters"]["on_guard"] = serde_json::json!({ "int": 4 });
    let model = model(&document.to_string());
    let driver = build_driver(&model);
    match assemble(&model, &registry(), &driver).err().unwrap() {
        AssemblyError::Component {
            component, detail, ..
        } => {
            assert_eq!(component.0, 1);
            assert!(
                detail.contains("on_guard"),
                "the failure should name the offending parameter, found {detail}"
            );
        }
        other => panic!("expected AssemblyError::Component, got {other:?}"),
    }
}
