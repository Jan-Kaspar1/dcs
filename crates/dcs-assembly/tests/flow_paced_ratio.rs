//! Integration tests for the `flow-paced-ratio` kind architecture
//! decision 50 records: the checked-in fixture composes three ratios
//! over one scripted `flow`/`trim` pair — a trimmed instance stopping
//! on a bad flow (`on_bad_flow` 0, `on_bad_trim` 0), an untrimmed one
//! holding the last `Good` demand (`on_bad_flow` 1), and an untrimmed
//! one driving the declared `fallback_rate` (`on_bad_flow` 2) — with
//! the `dose` setpoint riding a writable internal `In` point per the
//! decision, so operator writes land through the journaled receipted
//! command path and the held value crosses checkpoints.
//!
//! Scripted deterministic runs exercise the ratio across the feed
//! range, the dose and rate clamps with the `clamped` flag, every
//! declared bad-flow response with `fallback_active` timing and
//! worst-of quality, the bad-trim response, dose writes through the
//! command path, and checkpointed-standby continuity mid-fallback;
//! identical runs produce identical snapshots.

use dcs_assembly::{
    AssemblyError, BuildError, ComponentRegistry, DriverRegistry, FanoutDriver, assemble,
    resolve_drivers,
};
use dcs_blocks::{FlowPacedRatio, RatioOutputs};
use dcs_core::{Command, CommandOutcome, PointId, Quality, QualityReason, Sample, Tick, Value};
use dcs_model::PlantModel;
use dcs_runtime::{Component, Executor};

/// The fixture: one `sim-scripted` device replaying the flow sweep and
/// the trim's good/bad windows, the writable internal `dose` point, and
/// internal `Out` carriers for each ratio's three outputs (the links
/// carry quality, which a field loopback's write journal would drop).
const FLOW_PACED_RATIO: &str = include_str!("../fixtures/flow_paced_ratio.json");

const DOSE: PointId = PointId(20);
const DEMAND_1: PointId = PointId(30);
const CLAMPED_1: PointId = PointId(31);
const FALLBACK_1: PointId = PointId(32);
const DEMAND_2: PointId = PointId(33);
const CLAMPED_2: PointId = PointId(34);
const FALLBACK_2: PointId = PointId(35);
const DEMAND_3: PointId = PointId(36);
const FALLBACK_3: PointId = PointId(38);

/// The `dcs-blocks` registration for the fixture's kind, mirroring the
/// controller registry's port binding — `trim` bound through
/// `ComponentSpec::get` where the model wires it.
fn registry() -> ComponentRegistry {
    ComponentRegistry::new().with(FlowPacedRatio::KIND, |spec| {
        boxed(FlowPacedRatio::from_parameters(
            spec.name.as_str(),
            spec.require("flow")?,
            spec.require("dose")?,
            spec.get("trim"),
            RatioOutputs {
                demand: spec.require("demand")?,
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
fn flow_paced_ratio_fixture_assembles_through_the_registry() {
    let model = model(FLOW_PACED_RATIO);
    // The declared dose setpoint is the writable internal `In` point
    // decision 50 prescribes.
    let dose = model
        .io_points
        .iter()
        .find(|point| point.id == DOSE)
        .unwrap();
    assert!(dose.writable);
    assert!(dose.channel.is_none());
    // The untrimmed instances declare no `trim` port — the optional
    // port's absence is the "unwired means unity" rule.
    assert!(model.components[0].ports.contains_key("trim"));
    assert!(!model.components[1].ports.contains_key("trim"));
    assert!(!model.components[2].ports.contains_key("trim"));

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
}

/// The scripted sweep paces each declared instance through the feed
/// range: the ratio at ticks 0–4, the rate clamp at 5–9, the trim's
/// scaling at 10–14, every bad-flow response at 15–19, recovery at
/// 20–24, the bad-trim window at 25–29, and the trim's return at 30.
#[test]
fn scripted_flow_sweep_paces_each_declared_response() {
    let model = model(FLOW_PACED_RATIO);
    let driver = build_driver(&model);
    let mut executor = build_executor(&model, &driver);

    // Dose 2.0, flow 10, trim 1.0 — the untrimmed ratio reads 20.
    scan(&mut executor, &driver);
    assert_eq!(float_at(&executor, DEMAND_1), 20.0);
    assert_eq!(float_at(&executor, DEMAND_2), 20.0);
    assert_eq!(float_at(&executor, DEMAND_3), 20.0);
    assert!(!bool_at(&executor, CLAMPED_1));
    assert!(!bool_at(&executor, FALLBACK_1));

    // Flow 30 drives 2×30 = 60: every demand saturates at `max_rate`
    // 50 and `clamped` asserts.
    scan_to(&mut executor, &driver, 6);
    for point in [DEMAND_1, DEMAND_2, DEMAND_3] {
        assert_eq!(float_at(&executor, point), 50.0);
    }
    for point in [CLAMPED_1, CLAMPED_2] {
        assert!(bool_at(&executor, point));
    }

    // Flow 4 with the trim at 1.5: the bound instance paces
    // 2×4×1.5 = 12; the unwired ones run untrimmed — 8.
    scan_to(&mut executor, &driver, 11);
    assert_eq!(float_at(&executor, DEMAND_1), 12.0);
    assert_eq!(float_at(&executor, DEMAND_2), 8.0);
    assert_eq!(float_at(&executor, DEMAND_3), 8.0);
    assert!(!bool_at(&executor, CLAMPED_1));

    // The flow turns bad: stop, hold-last-Good (the banked 8), and the
    // declared fallback rate — `fallback_active` asserting for the
    // engagement and `demand` stamped worst-of.
    scan_to(&mut executor, &driver, 16);
    assert_eq!(float_at(&executor, DEMAND_1), 0.0);
    assert_eq!(float_at(&executor, DEMAND_2), 8.0);
    assert_eq!(float_at(&executor, DEMAND_3), 12.0);
    for point in [FALLBACK_1, FALLBACK_2, FALLBACK_3] {
        assert!(bool_at(&executor, point));
    }
    for point in [DEMAND_1, DEMAND_2, DEMAND_3] {
        assert_eq!(
            sample(&executor, point).quality,
            Quality::Bad(QualityReason::CommunicationFault)
        );
    }
    // The responses run for the engagement's whole duration.
    scan(&mut executor, &driver);
    assert_eq!(float_at(&executor, DEMAND_1), 0.0);
    assert_eq!(float_at(&executor, DEMAND_2), 8.0);
    assert_eq!(float_at(&executor, DEMAND_3), 12.0);
    assert!(bool_at(&executor, FALLBACK_1));

    // Recovery at script tick 20 (scan 21): flow 10, trim 1.5 — the
    // trimmed ratio paces 30, the untrimmed ones 20, flags cleared.
    scan_to(&mut executor, &driver, 21);
    assert_eq!(float_at(&executor, DEMAND_1), 30.0);
    assert_eq!(float_at(&executor, DEMAND_2), 20.0);
    assert_eq!(float_at(&executor, DEMAND_3), 20.0);
    for point in [FALLBACK_1, FALLBACK_2, FALLBACK_3] {
        assert!(!bool_at(&executor, point));
    }

    // The trim turns bad while flow stays good: `on_bad_trim` 0 paces
    // the bound instance untrimmed — 20 — and reports the engagement;
    // the unwired instances never declared the port and see nothing.
    scan_to(&mut executor, &driver, 26);
    assert_eq!(float_at(&executor, DEMAND_1), 20.0);
    assert_eq!(
        sample(&executor, DEMAND_1).quality,
        Quality::Bad(QualityReason::DeviceFault)
    );
    assert!(bool_at(&executor, FALLBACK_1));
    assert!(!bool_at(&executor, FALLBACK_2));
    assert_eq!(sample(&executor, DEMAND_2).quality, Quality::Good);

    // The trim recovers at script tick 30 (scan 31): the bound
    // instance returns to 30 and the flag clears.
    scan_to(&mut executor, &driver, 31);
    assert_eq!(float_at(&executor, DEMAND_1), 30.0);
    assert!(!bool_at(&executor, FALLBACK_1));
}

/// The operator's dose setpoint lands through the journaled command
/// path — applied at the scan boundary — and the declared dose bounds
/// clamp an out-of-range entry with `clamped` asserting.
#[test]
fn the_dose_setpoint_writes_through_the_journaled_path() {
    let model = model(FLOW_PACED_RATIO);
    let driver = build_driver(&model);
    let mut executor = build_executor(&model, &driver);
    scan_to(&mut executor, &driver, 3);
    assert_eq!(float_at(&executor, DEMAND_2), 20.0);

    // Write 6.0 — above `max_dose` 4.0: applied at scan 4's boundary
    // and clamped, so every ratio paces on 4.0.
    let receipt = executor.submit_command(Command::WriteValue {
        point: DOSE,
        kind: dcs_core::ValueKind::Float,
        value: Value::Float(6.0),
    });
    assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
    scan(&mut executor, &driver);
    assert_eq!(float_at(&executor, DEMAND_1), 40.0);
    assert_eq!(float_at(&executor, DEMAND_2), 40.0);
    assert!(bool_at(&executor, CLAMPED_1));
    assert!(bool_at(&executor, CLAMPED_2));

    // Back inside the bounds, the flag releases.
    executor.submit_command(Command::WriteValue {
        point: DOSE,
        kind: dcs_core::ValueKind::Float,
        value: Value::Float(3.0),
    });
    scan(&mut executor, &driver);
    assert_eq!(float_at(&executor, DEMAND_1), 30.0);
    assert_eq!(float_at(&executor, DEMAND_2), 30.0);
    assert!(!bool_at(&executor, CLAMPED_1));
}

/// A standby assembling the same model and applying a mid-fallback
/// checkpoint — every instance's declared response engaged, the hold
/// and the tuned set included — continues the scripted run identically
/// to the active.
#[test]
fn checkpointed_standby_mid_fallback_continues_identically() {
    let model = model(FLOW_PACED_RATIO);
    let driver_a = build_driver(&model);
    let mut active = build_executor(&model, &driver_a);

    // Scan 17 sits inside the bad-flow window: stop, hold, and the
    // fallback rate all engaged — the state the checkpoint must carry.
    scan_to(&mut active, &driver_a, 17);
    assert_eq!(float_at(&active, DEMAND_2), 8.0);
    assert!(bool_at(&active, FALLBACK_1));
    let checkpoint = active.checkpoint();

    let driver_b = build_driver(&model);
    let mut standby = build_executor(&model, &driver_b);
    standby.apply(&checkpoint).unwrap();
    assert_eq!(float_at(&standby, DEMAND_2), 8.0);
    assert!(bool_at(&standby, FALLBACK_2));

    // Through the flow's recovery and the bad-trim window both run
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
        let model = model(FLOW_PACED_RATIO);
        let driver = build_driver(&model);
        let mut executor = build_executor(&model, &driver);
        for _ in 0..35 {
            scan(&mut executor, &driver);
        }
        serde_json::to_string(&executor.snapshot()).unwrap()
    };
    assert_eq!(run(), run());
}

/// A parameter outside the declared set — a bad `on_bad_flow` code —
/// fails assembly naming the offending parameter.
#[test]
fn a_malformed_response_code_fails_assembly_naming_the_parameter() {
    let mut document: serde_json::Value = serde_json::from_str(FLOW_PACED_RATIO).unwrap();
    document["components"][0]["parameters"]["on_bad_flow"] = serde_json::json!({ "int": 7 });
    let model = model(&document.to_string());
    let driver = build_driver(&model);
    match assemble(&model, &registry(), &driver).err().unwrap() {
        AssemblyError::Component {
            component, detail, ..
        } => {
            assert_eq!(component.0, 1);
            assert!(
                detail.contains("on_bad_flow"),
                "the failure should name the offending code, found {detail}"
            );
        }
        other => panic!("expected AssemblyError::Component, got {other:?}"),
    }
}
