//! Integration test for the showcase plant: `fixtures/showcase.json`'s
//! pump-and-tank line assembled through the standard registries and run
//! through its documented command-and-fault scenario — the acceptance
//! criteria for the showcase issue.
//!
//! The run's documented behavior, phase by phase, is in
//! [`dcs_demo::showcase`]'s module docs; these tests pin the fixture's
//! validation and coverage, the standard-registry assembly, the
//! command's scan boundary, the injected fault's interlock and alarm
//! behavior, the recovery, and run-to-run determinism.

use dcs_core::{
    Command, CommandError, CommandOutcome, PointId, Quality, QualityReason, Sample, Tick, Value,
    ValueKind,
};
use dcs_demo::showcase::{
    self, FAULT_SCANS, INITIAL_SETPOINT, MOVED_SCANS, MOVED_SETPOINT, SETTLE_SCANS,
    SHOWCASE_DOCUMENT, TOTAL_SCANS, points,
};
use dcs_model::PlantModel;
use std::collections::BTreeSet;

fn sample(snapshot: &dcs_core::TelemetrySnapshot, point: PointId) -> Sample {
    showcase::sample(snapshot, point).expect("the showcase maps every named point")
}

fn float(snapshot: &dcs_core::TelemetrySnapshot, point: PointId) -> f64 {
    let Value::Float(value) = sample(snapshot, point).value else {
        panic!("point {} is a Float point", point.0)
    };
    value
}

fn bool_point(snapshot: &dcs_core::TelemetrySnapshot, point: PointId) -> bool {
    let Value::Bool(value) = sample(snapshot, point).value else {
        panic!("point {} is a Bool point", point.0)
    };
    value
}

fn int_point(snapshot: &dcs_core::TelemetrySnapshot, point: PointId) -> i64 {
    let Value::Int(value) = sample(snapshot, point).value else {
        panic!("point {} is an Int point", point.0)
    };
    value
}

#[test]
fn fixture_loads_validates_and_covers_the_component_library() {
    let model = PlantModel::load(SHOWCASE_DOCUMENT).expect("the showcase fixture loads");
    assert!(
        model.validate().is_empty(),
        "the showcase fixture passes dcs-model validation"
    );

    let kinds: BTreeSet<&str> = model
        .components
        .iter()
        .map(|component| component.kind.as_str())
        .collect();
    for kind in [
        "analog-input",
        "analog-output",
        "digital-input",
        "digital-output",
        "pid",
        "rate-limiter",
        "timer",
        "counter",
        "valve",
        "motor",
        "interlock",
        "alarm-monitor",
        "override-select",
    ] {
        assert!(kinds.contains(kind), "the showcase exercises kind {kind:?}");
    }
}

#[test]
fn assembles_through_the_standard_registry_with_no_manual_wiring() {
    let model = PlantModel::load(SHOWCASE_DOCUMENT).unwrap();
    let driver = showcase::driver(&model).expect("the standard driver registry resolves");
    let mut executor = dcs_assembly::assemble(&model, &dcs_controller::registry(), &driver)
        .expect("the standard component registry assembles the model");

    // The command surface is the model's: a point not declared writable
    // is refused at submission, naming the point.
    let receipt = executor.submit_command(Command::WriteValue {
        point: points::LEVEL_RAW,
        kind: ValueKind::Float,
        value: Value::Float(1.0),
    });
    assert_eq!(
        receipt.outcome,
        CommandOutcome::Rejected {
            reason: CommandError::NotWritable {
                point: points::LEVEL_RAW
            }
        }
    );
}

#[test]
fn loop_regulates_at_the_declared_setpoint() {
    let run = showcase::run(SHOWCASE_DOCUMENT).expect("the showcase run completes");

    // The loop settled at the declared setpoint: level and valve at 60%
    // (13.6 mA of the 4–20 mA range), no interlock, alarm, or motor
    // fault, and the start-delay trip already counted — the counter's
    // first edge, preset not yet reached.
    assert_eq!(run.settled.tick, Tick(SETTLE_SCANS));
    assert!(
        (float(&run.settled, points::LEVEL_PERCENT) - INITIAL_SETPOINT).abs() < 0.5,
        "level settled at the declared setpoint"
    );
    assert!(
        (float(&run.settled, points::VALVE_RAW) - 13.6).abs() < 0.1,
        "the valve sits at the settled command"
    );
    assert!(!bool_point(&run.settled, points::INTERLOCK_TRIPPED));
    assert!(!bool_point(&run.settled, points::LEVEL_ALARM));
    assert!(!bool_point(&run.settled, points::PUMP_FAULT));
    assert_eq!(int_point(&run.settled, points::TRIP_COUNT), 1);
    assert!(!bool_point(&run.settled, points::TRIPS_DONE));
}

#[test]
fn setpoint_command_applies_at_the_documented_boundary() {
    let run = showcase::run(SHOWCASE_DOCUMENT).unwrap();

    // The submission receipt names the scan the write applies at: the
    // first tick after the settle phase.
    assert_eq!(
        run.receipts[0].outcome,
        CommandOutcome::Accepted {
            apply_tick: Tick(SETTLE_SCANS + 1)
        }
    );

    // Before the boundary the setpoint point still held the declared
    // initial; the moved-phase snapshot shows the applied value and the
    // loop re-settled at it.
    assert_eq!(
        float(&run.settled, points::LEVEL_SETPOINT),
        INITIAL_SETPOINT
    );
    assert_eq!(float(&run.moved, points::LEVEL_SETPOINT), MOVED_SETPOINT);
    assert_eq!(run.moved.tick, Tick(SETTLE_SCANS + MOVED_SCANS));
    assert!(
        (float(&run.moved, points::LEVEL_PERCENT) - MOVED_SETPOINT).abs() < 0.5,
        "the loop re-settled at the commanded setpoint"
    );
}

#[test]
fn injected_input_fault_trips_interlock_and_asserts_alarm() {
    let run = showcase::run(SHOWCASE_DOCUMENT).unwrap();

    // The disconnected run feedback reads Bad(CommunicationFault): the
    // conditioned run status goes bad, the interlock trips on the bad
    // permissive and drives the valve to its safe value, and the level
    // drains to the low alarm — alarm asserted, horn driven, motor
    // fault flagged, and the second trip edge latching the counter's
    // preset.
    assert_eq!(
        run.faulted.tick,
        Tick(SETTLE_SCANS + MOVED_SCANS + FAULT_SCANS)
    );
    assert_eq!(
        sample(&run.faulted, points::PUMP_RUN).quality,
        Quality::Bad(QualityReason::CommunicationFault)
    );
    assert!(bool_point(&run.faulted, points::INTERLOCK_TRIPPED));
    assert!(bool_point(&run.faulted, points::LEVEL_ALARM));
    assert!(bool_point(&run.faulted, points::HORN));
    assert!(bool_point(&run.faulted, points::PUMP_FAULT));
    assert_eq!(int_point(&run.faulted, points::TRIP_COUNT), 2);
    assert!(bool_point(&run.faulted, points::TRIPS_DONE));
    assert_eq!(float(&run.faulted, points::VALVE_RAW), 4.0);
    assert!(
        float(&run.faulted, points::LEVEL_PERCENT) < 10.0,
        "the level drained through the low alarm limit"
    );
}

#[test]
fn clearing_the_fault_auto_resets_and_the_operator_reset_rearms() {
    let run = showcase::run(SHOWCASE_DOCUMENT).unwrap();

    // Recovery: the permissive proves Good again so the interlock
    // auto-resets — it holds no latch — the motor fault clears on the
    // first agreeing scan, the level climbs back through the alarm
    // deadband to the moved setpoint, and the operator's reset command
    // cleared and rearmed the trip counter.
    assert_eq!(run.recovered.tick, Tick(TOTAL_SCANS));
    assert!(!bool_point(&run.recovered, points::INTERLOCK_TRIPPED));
    assert!(!bool_point(&run.recovered, points::LEVEL_ALARM));
    assert!(!bool_point(&run.recovered, points::HORN));
    assert!(!bool_point(&run.recovered, points::PUMP_FAULT));
    assert!(
        (float(&run.recovered, points::LEVEL_PERCENT) - MOVED_SETPOINT).abs() < 0.5,
        "the level recovered to the moved setpoint"
    );
    assert_eq!(int_point(&run.recovered, points::TRIP_COUNT), 0);
    assert!(!bool_point(&run.recovered, points::TRIPS_DONE));

    // The reset's assert and release were accepted for the last two
    // scan boundaries.
    assert_eq!(
        run.receipts[1].outcome,
        CommandOutcome::Accepted {
            apply_tick: Tick(TOTAL_SCANS - 1)
        }
    );
    assert_eq!(
        run.receipts[2].outcome,
        CommandOutcome::Accepted {
            apply_tick: Tick(TOTAL_SCANS)
        }
    );
}

#[test]
fn identical_runs_produce_identical_snapshots() {
    let first = showcase::run(SHOWCASE_DOCUMENT).unwrap();
    let second = showcase::run(SHOWCASE_DOCUMENT).unwrap();
    assert_eq!(first, second, "the showcase run is fully deterministic");

    // No component stepped in error anywhere in the scenario.
    for snapshot in [
        &first.settled,
        &first.moved,
        &first.faulted,
        &first.recovered,
    ] {
        assert!(
            snapshot
                .components
                .iter()
                .all(|component| component.step_errors == 0),
            "every component stepped clean"
        );
    }
}
