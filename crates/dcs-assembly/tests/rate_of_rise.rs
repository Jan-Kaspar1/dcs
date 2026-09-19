//! Integration tests for the `rate-of-rise` kind — the one-sided
//! derivative annunciation the IJmuiden composition's recorded
//! contract gap names: the checked-in fixture composes two monitors
//! over one scripted `level` channel — a silent-initial instance
//! (`rate_limit` 0.5, `initial_rate` 0.0) and a declared-initial
//! instance whose `initial_rate` 0.75 covers the reads before the
//! first `Good` sample pair.
//!
//! Scripted deterministic runs exercise the quiet tracking, a rising
//! stretch asserting and releasing at the bound, the fast fall that
//! stays silent — the divergence the composed
//! `deviation-monitor`-versus-`signal-filter` detector cannot express
//! — the frozen-on-bad-quality rule holding the asserted flag, the
//! gap-spanning recovery difference, and checkpointed-standby
//! continuity mid-freeze; identical runs produce identical snapshots.

use dcs_assembly::{
    AssemblyError, BuildError, ComponentRegistry, DriverRegistry, FanoutDriver, assemble,
    resolve_drivers,
};
use dcs_blocks::RateOfRise;
use dcs_core::{PointId, Quality, QualityReason, Sample, Tick, Value};
use dcs_model::PlantModel;
use dcs_runtime::{Component, Executor};

/// The fixture: one `sim-scripted` device replaying the level —
/// quiet, a fast rise, a release below the bound, a fast fall, a
/// bad-quality stretch mid-rise, the gap-spanning recovery, and a
/// stale stretch — and internal `Out` carriers for each monitor's
/// `rate` and `rising` (the links carry quality, which a field
/// loopback's write journal would drop).
const RATE_OF_RISE: &str = include_str!("../fixtures/rate_of_rise.json");

const RATE_1: PointId = PointId(20);
const RISING_1: PointId = PointId(21);
const RATE_2: PointId = PointId(22);
const RISING_2: PointId = PointId(23);

/// The `dcs-blocks` registration for the fixture's kind, mirroring the
/// controller registry's port binding.
fn registry() -> ComponentRegistry {
    ComponentRegistry::new().with(RateOfRise::KIND, |spec| {
        boxed(RateOfRise::from_parameters(
            spec.name.as_str(),
            spec.require("in")?,
            spec.require("rate")?,
            spec.require("rising")?,
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
fn rate_of_rise_fixture_assembles_through_the_registry() {
    let model = model(RATE_OF_RISE);
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

/// The scripted run walks both instances through the one-sided
/// verdict: the declared-initial instance asserts from the first scan
/// until the first `Good` pair completes its difference; the quiet
/// tracking to scan 5; the +1.0/tick rise asserting at scans 6–7 and
/// releasing at scan 8; the fast fall at scans 11–12 staying silent;
/// the second rise asserting at 16–17; the bad-quality freeze holding
/// the flag at 18–19; the gap-spanning +5.0 recovery at 20; release at
/// 21; the stale freeze at 23–24; and the final flat.
#[test]
fn the_scripted_run_exercises_the_one_sided_flag() {
    let model = model(RATE_OF_RISE);
    let driver = build_driver(&model);
    let mut executor = build_executor(&model, &driver);

    // Scan 1: no completed pair yet — the declared initials cover the
    // reads: instance 1 reports 0.0 silent, instance 2 reports its
    // declared 0.75 and asserts.
    scan_to(&mut executor, &driver, 1);
    assert_eq!(float_at(&executor, RATE_1), 0.0);
    assert!(!bool_at(&executor, RISING_1));
    assert_eq!(float_at(&executor, RATE_2), 0.75);
    assert!(bool_at(&executor, RISING_2));

    // The first completed pair replaces the declared cover — both
    // instances read the flat level's 0.0 rate from scan 2 on.
    scan_to(&mut executor, &driver, 5);
    assert_eq!(float_at(&executor, RATE_1), 0.0);
    assert_eq!(float_at(&executor, RATE_2), 0.0);
    assert!(!bool_at(&executor, RISING_1));
    assert!(!bool_at(&executor, RISING_2));

    // The +1.0/tick rise (5.0 → 6.0 at script tick 5, → 7.0 at 6)
    // asserts the flag on both instances.
    scan_to(&mut executor, &driver, 6);
    assert_eq!(float_at(&executor, RATE_1), 1.0);
    assert!(bool_at(&executor, RISING_1));
    assert!(bool_at(&executor, RISING_2));

    // The rise slows to +0.25 at script tick 7: below the bound, the
    // flag releases — no latch; the alarm kind owns that.
    scan_to(&mut executor, &driver, 8);
    assert_eq!(float_at(&executor, RATE_1), 0.25);
    assert!(!bool_at(&executor, RISING_1));

    // The fast fall — the protective-draw shape the composed
    // divergence monitor misreads as a "rise": −2.25 then −3.0 per
    // tick report their signed rates and never assert.
    scan_to(&mut executor, &driver, 12);
    assert_eq!(float_at(&executor, RATE_1), -3.0);
    assert!(!bool_at(&executor, RISING_1));
    assert!(!bool_at(&executor, RISING_2));

    // The second rise (+1.0/tick from script tick 15) asserts again.
    scan_to(&mut executor, &driver, 17);
    assert_eq!(float_at(&executor, RATE_1), 1.0);
    assert!(bool_at(&executor, RISING_1));

    // `level` turns bad mid-rise at script tick 17: the previous
    // sample holds, the standing rate and flag hold stamped with the
    // merged quality — the standing excursion stays visible, marked
    // untrusted, rather than releasing as a healthy clear.
    scan_to(&mut executor, &driver, 19);
    assert_eq!(float_at(&executor, RATE_1), 1.0);
    assert!(bool_at(&executor, RISING_1));
    assert_eq!(
        sample(&executor, RATE_1).quality,
        Quality::Bad(QualityReason::CommunicationFault)
    );
    assert_eq!(
        sample(&executor, RISING_2).quality,
        Quality::Bad(QualityReason::CommunicationFault)
    );

    // Recovery differences against the last banked sample: 9.0 − 4.0
    // reports the whole excursion across the gap as one per-tick
    // rate — the flag asserts, quality Good.
    scan_to(&mut executor, &driver, 20);
    assert_eq!(float_at(&executor, RATE_1), 5.0);
    assert!(bool_at(&executor, RISING_1));
    assert_eq!(sample(&executor, RATE_1).quality, Quality::Good);

    // The flat 9.0 releases the flag on the next evaluated scan.
    scan_to(&mut executor, &driver, 21);
    assert_eq!(float_at(&executor, RATE_1), 0.0);
    assert!(!bool_at(&executor, RISING_1));

    // The stale stretch freezes the cleared verdict, quality-stamped.
    scan_to(&mut executor, &driver, 24);
    assert!(!bool_at(&executor, RISING_1));
    assert_eq!(
        sample(&executor, RISING_1).quality,
        Quality::Uncertain(QualityReason::Stale)
    );

    // Final recovery: the flat level reports 0.0 Good and stays
    // silent.
    scan_to(&mut executor, &driver, 28);
    assert_eq!(float_at(&executor, RATE_1), 0.0);
    assert!(!bool_at(&executor, RISING_1));
    assert_eq!(sample(&executor, RATE_1).quality, Quality::Good);
}

/// A standby assembling the same model and applying a mid-freeze
/// checkpoint — the flag asserted and frozen at scan 19, the banked
/// previous sample carried — continues the scripted run identically
/// to the active: the restored `previous` differences the recovery
/// sample with no spurious edge.
#[test]
fn checkpointed_standby_mid_freeze_continues_identically() {
    let model = model(RATE_OF_RISE);
    let driver_a = build_driver(&model);
    let mut active = build_executor(&model, &driver_a);

    // Scan 19 sits inside the bad-quality freeze: the flag asserted
    // and held, `previous` banking 4.0 — the state the checkpoint must
    // carry.
    scan_to(&mut active, &driver_a, 19);
    assert!(bool_at(&active, RISING_1));
    let checkpoint = active.checkpoint();

    let driver_b = build_driver(&model);
    let mut standby = build_executor(&model, &driver_b);
    standby.apply(&checkpoint).unwrap();
    assert!(bool_at(&standby, RISING_1));

    // Through the gap-spanning recovery, the release, and the stale
    // freeze both run identically.
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
        let model = model(RATE_OF_RISE);
        let driver = build_driver(&model);
        let mut executor = build_executor(&model, &driver);
        for _ in 0..40 {
            scan(&mut executor, &driver);
        }
        serde_json::to_string(&executor.snapshot()).unwrap()
    };
    assert_eq!(run(), run());
}

/// A parameter outside the declared set — a zero `rate_limit` —
/// fails assembly naming the offending parameter.
#[test]
fn a_malformed_bound_fails_assembly_naming_the_parameter() {
    let mut document: serde_json::Value = serde_json::from_str(RATE_OF_RISE).unwrap();
    document["components"][0]["parameters"]["rate_limit"] = serde_json::json!({ "float": 0.0 });
    let model = model(&document.to_string());
    let driver = build_driver(&model);
    match assemble(&model, &registry(), &driver).err().unwrap() {
        AssemblyError::Component {
            component, detail, ..
        } => {
            assert_eq!(component.0, 1);
            assert!(
                detail.contains("rate_limit"),
                "the failure should name the offending parameter, found {detail}"
            );
        }
        other => panic!("expected AssemblyError::Component, got {other:?}"),
    }
}
