//! Integration tests for the `feedforward-sum` kind architecture
//! decision 66 records: the checked-in fixture composes three
//! instances over one scripted `ff`/`trim` pair — a drop/drop
//! instance (`on_bad_ff` 0, `on_bad_trim` 0 — an untrusted term drops
//! out and the other serves alone), a hold/hold instance (`1`/`1` —
//! the term's last `Good` value stands in), and a mixed instance
//! (`0`/`1` — feed-forward drops, trim holds) — so one script
//! exercises every declared response and their composition.
//!
//! The scripted run demonstrates `out` summing `ff` and the
//! authority-bounded `trim` inside `min_demand`/`max_demand`, the
//! trim and demand clamps reporting on `clamped`, each `on_bad` code's
//! declared response with `fallback_active` asserted and `out`
//! carrying the merged worst-of quality, and a recovering input
//! resuming the sum the same scan; checkpointed standby and
//! determinism hold.

use dcs_assembly::{
    AssemblyError, BuildError, ComponentRegistry, DriverRegistry, FanoutDriver, assemble,
    resolve_drivers,
};
use dcs_blocks::{FeedforwardSum, FeedforwardSumIo};
use dcs_core::{PointId, Quality, QualityReason, Sample, Tick, Value};
use dcs_model::PlantModel;
use dcs_runtime::{Component, Executor};

/// The fixture: one `sim-scripted` device replaying the feed-forward
/// and trim inputs — and internal `Out` carriers for each instance's
/// `out`, `clamped`, and `fallback_active` (the links carry quality,
/// which a field loopback's write journal would drop).
const FEEDFORWARD_SUM: &str = include_str!("../fixtures/feedforward_sum.json");

const OUT_1: PointId = PointId(20);
const CLAMPED_1: PointId = PointId(21);
const FALLBACK_1: PointId = PointId(22);
const OUT_2: PointId = PointId(23);
const CLAMPED_2: PointId = PointId(24);
const FALLBACK_2: PointId = PointId(25);
const OUT_3: PointId = PointId(26);
const FALLBACK_3: PointId = PointId(28);

/// The `dcs-blocks` registration for the fixture's kind, mirroring the
/// controller registry's port binding.
fn registry() -> ComponentRegistry {
    ComponentRegistry::new().with(FeedforwardSum::KIND, |spec| {
        boxed(FeedforwardSum::from_parameters(
            spec.name.as_str(),
            FeedforwardSumIo {
                ff: spec.require("ff")?,
                trim: spec.require("trim")?,
                out: spec.require("out")?,
                clamped: spec.require("clamped")?,
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
fn feedforward_sum_fixture_assembles_through_the_registry() {
    let model = model(FEEDFORWARD_SUM);
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
        assert_eq!(descriptor.ports.len(), 5);
    }
}

/// The scripted run: the plain sum at ticks 0–9, the trim authority
/// clamp at 10–19, the demand bound at 20–24, the clean window at
/// 25–29 banking the hold terms, the bad-`ff` responses at 30–34, both
/// inputs bad at 35–39, the `ff` recovery beside the still-bad `trim`
/// at 40–44, and full recovery at 45.
#[test]
fn the_scripted_run_exercises_each_declared_response() {
    let model = model(FEEDFORWARD_SUM);
    let driver = build_driver(&model);
    let mut executor = build_executor(&model, &driver);

    // ff 60, trim 5 — every instance sums 65 inside the bounds.
    scan_to(&mut executor, &driver, 5);
    for out in [OUT_1, OUT_2, OUT_3] {
        assert_eq!(float_at(&executor, out), 65.0);
        assert_eq!(sample(&executor, out).quality, Quality::Good);
    }
    for flag in [CLAMPED_1, CLAMPED_2, FALLBACK_1, FALLBACK_2, FALLBACK_3] {
        assert!(!bool_at(&executor, flag));
    }

    // Trim 15 exceeds its ±10 authority: the bound trim serves, so
    // 60 + 10 = 70 with `clamped` reporting the bound.
    scan_to(&mut executor, &driver, 12);
    for out in [OUT_1, OUT_2, OUT_3] {
        assert_eq!(float_at(&executor, out), 70.0);
    }
    for clamped in [CLAMPED_1, CLAMPED_2] {
        assert!(bool_at(&executor, clamped));
    }

    // Trim -15 floors the same way: 60 - 10 = 50.
    scan_to(&mut executor, &driver, 17);
    for out in [OUT_1, OUT_2, OUT_3] {
        assert_eq!(float_at(&executor, out), 50.0);
        assert_eq!(sample(&executor, out).quality, Quality::Good);
    }

    // ff 95 plus the bound trim 10 = 105 saturates at `max_demand`
    // 100 — the demand bound's `clamped` report.
    scan_to(&mut executor, &driver, 22);
    for out in [OUT_1, OUT_2, OUT_3] {
        assert_eq!(float_at(&executor, out), 100.0);
    }
    assert!(bool_at(&executor, CLAMPED_1));

    // The clean window: ff 30, trim 5 sums 35 and banks both hold
    // terms for the responses ahead.
    scan_to(&mut executor, &driver, 27);
    for out in [OUT_1, OUT_2, OUT_3] {
        assert_eq!(float_at(&executor, out), 35.0);
    }
    for flag in [CLAMPED_1, CLAMPED_2, FALLBACK_1, FALLBACK_2, FALLBACK_3] {
        assert!(!bool_at(&executor, flag));
    }

    // `ff` turns bad: the drop codes serve the trim alone — 5 — while
    // the hold code stands the banked 30 beside it — 35 — each
    // `fallback_active` asserting and `out` carrying the fault.
    scan_to(&mut executor, &driver, 32);
    assert_eq!(float_at(&executor, OUT_1), 5.0);
    assert_eq!(float_at(&executor, OUT_2), 35.0);
    assert_eq!(float_at(&executor, OUT_3), 5.0);
    for active in [FALLBACK_1, FALLBACK_2, FALLBACK_3] {
        assert!(bool_at(&executor, active));
        assert_eq!(sample(&executor, active).quality, Quality::Good);
    }
    for out in [OUT_1, OUT_2, OUT_3] {
        assert_eq!(
            sample(&executor, out).quality,
            Quality::Bad(QualityReason::CommunicationFault)
        );
    }

    // The `trim` turns bad too: the responses compose — drop/drop
    // serves `min_demand`-bounded zero, hold/hold the banked 30 + 5,
    // the mixed instance the dropped `ff` beside the held trim.
    scan_to(&mut executor, &driver, 37);
    assert_eq!(float_at(&executor, OUT_1), 0.0);
    assert_eq!(float_at(&executor, OUT_2), 35.0);
    assert_eq!(float_at(&executor, OUT_3), 5.0);
    for active in [FALLBACK_1, FALLBACK_2, FALLBACK_3] {
        assert!(bool_at(&executor, active));
    }
    assert_eq!(
        sample(&executor, OUT_1).quality,
        Quality::Bad(QualityReason::DeviceFault)
    );

    // `ff` recovers to 40 while `trim` stays bad: the served sums
    // track the recovered term — 40 + 0 for the drops, 40 + held 5
    // for the holds — the trim's response still engaged.
    scan_to(&mut executor, &driver, 43);
    assert_eq!(float_at(&executor, OUT_1), 40.0);
    assert_eq!(float_at(&executor, OUT_2), 45.0);
    assert_eq!(float_at(&executor, OUT_3), 45.0);
    for active in [FALLBACK_1, FALLBACK_2, FALLBACK_3] {
        assert!(bool_at(&executor, active));
    }

    // `trim` recovers to -8 at script tick 45 (scan 46): every
    // instance resumes the plain sum — 40 - 8 = 32 — flags cleared.
    scan_to(&mut executor, &driver, 46);
    for out in [OUT_1, OUT_2, OUT_3] {
        assert_eq!(float_at(&executor, out), 32.0);
        assert_eq!(sample(&executor, out).quality, Quality::Good);
    }
    for flag in [CLAMPED_1, CLAMPED_2, FALLBACK_1, FALLBACK_2, FALLBACK_3] {
        assert!(!bool_at(&executor, flag));
    }
}

/// A standby assembling the same model and applying a mid-fallback
/// checkpoint — both inputs bad with the hold terms banked —
/// continues the scripted run identically to the active.
#[test]
fn checkpointed_standby_mid_fallback_continues_identically() {
    let model = model(FEEDFORWARD_SUM);
    let driver_a = build_driver(&model);
    let mut active = build_executor(&model, &driver_a);

    // Scan 37 sits inside the both-bad window: the hold instance
    // serves its banked terms — the run state the checkpoint must
    // carry.
    scan_to(&mut active, &driver_a, 37);
    assert_eq!(float_at(&active, OUT_2), 35.0);
    assert!(bool_at(&active, FALLBACK_2));
    let checkpoint = active.checkpoint();

    let driver_b = build_driver(&model);
    let mut standby = build_executor(&model, &driver_b);
    standby.apply(&checkpoint).unwrap();
    assert_eq!(float_at(&standby, OUT_2), 35.0);
    assert!(bool_at(&standby, FALLBACK_2));

    // Through the `ff` recovery and the trim's return both run
    // identically.
    for _ in 0..15 {
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
        let model = model(FEEDFORWARD_SUM);
        let driver = build_driver(&model);
        let mut executor = build_executor(&model, &driver);
        for _ in 0..50 {
            scan(&mut executor, &driver);
        }
        serde_json::to_string(&executor.snapshot()).unwrap()
    };
    assert_eq!(run(), run());
}

/// A parameter outside the declared set — a bad `on_bad_ff` code —
/// fails assembly naming the offending parameter.
#[test]
fn a_malformed_response_code_fails_assembly_naming_the_parameter() {
    let mut document: serde_json::Value = serde_json::from_str(FEEDFORWARD_SUM).unwrap();
    document["components"][0]["parameters"]["on_bad_ff"] = serde_json::json!({ "int": 7 });
    let model = model(&document.to_string());
    let driver = build_driver(&model);
    match assemble(&model, &registry(), &driver).err().unwrap() {
        AssemblyError::Component {
            component, detail, ..
        } => {
            assert_eq!(component.0, 1);
            assert!(
                detail.contains("on_bad_ff"),
                "the failure should name the offending code, found {detail}"
            );
        }
        other => panic!("expected AssemblyError::Component, got {other:?}"),
    }
}
