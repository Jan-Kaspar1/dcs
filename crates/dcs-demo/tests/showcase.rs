//! Integration test for the showcase plant: `fixtures/showcase.json`'s
//! pump-and-tank line assembled through the standard registries and run
//! through its documented command-and-fault scenario — the acceptance
//! criteria for the showcase issue.
//!
//! The run's documented behavior, phase by phase, is in
//! [`dcs_demo::showcase`]'s module docs; these tests pin the fixture's
//! validation and coverage, the dynamics document the served plant
//! shares, the standard-registry assembly, the command path's scan
//! boundary, each component kind's documented behavior — the batch
//! sequence's advance and the retune's visible overshoot, the voter's
//! deviant-leg discrepancy, the latching alarm's ordinary-path
//! acknowledgment, the manual station's bumpless transfer, the
//! totalizer's accumulation and reset — the injected fault's interlock
//! and alarm behavior, the recovery, and run-to-run determinism.

use dcs_core::{
    Command, CommandError, CommandOutcome, PointId, Quality, QualityReason, Sample, Tick, Value,
    ValueKind,
};
use dcs_demo::showcase::{
    self, ACKED_TICK, AGREED_TICK, BATCH_PRE_TUNE_SCANS, BATCH_SCANS, BATCH_STEP1_TICKS,
    BATCH_STEP2_TICKS, BATCH_STEP3_TICKS, BATCH_STEP4_TICKS, BATCHED_TICK, DEVIANT_RAW,
    DEVIATED_TICK, FAULTED_TICK, INITIAL_SETPOINT, MANUAL_ENGAGED_TICK, MANUAL_HOLD_SCANS,
    MANUAL_POSITION, MANUAL_TICK, MOVED_SETPOINT, MOVED_TICK, SETTLE_SCANS, SHOWCASE_DOCUMENT,
    SHOWCASE_DYNAMICS_DOCUMENT, TOTAL_SCANS, TRIPPED_TICK, points,
};
use dcs_model::{Direction, PlantModel};
use dcs_sim::{ProcessElement, SecondOrderLag};
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

    // Every kind the standard registry — and so the deployed
    // controller — can instantiate appears on the sheet at least once.
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
        "latching-alarm",
        "manual-station",
        "signal-filter",
        "median-voter",
        "totalizer",
        "sequencer",
    ] {
        assert!(kinds.contains(kind), "the showcase exercises kind {kind:?}");
    }
}

#[test]
fn journaled_marks_the_durable_record_points() {
    // Decision 74's sweep on the hand-authored fixture: the alarm
    // lifecycle outputs, the mode and managed-state points decision
    // 75 names, the batch record, and the protection-relevant status
    // points carry `journaled`; receipts-only writables, delivered
    // copies, pulses, and every float stay off the durable record.
    let model = PlantModel::load(SHOWCASE_DOCUMENT).unwrap();
    let journaled = |point: PointId| {
        model
            .io_points
            .iter()
            .find(|io| io.id == point)
            .unwrap_or_else(|| panic!("{point:?} is not in the fixture"))
            .journaled
    };
    for point in [
        points::PUMP_RUN,
        points::HIGH_SWITCH,
        points::VALVE_MANUAL_SELECT,
        points::BATCH_RUN,
        points::BATCH_MODE,
        points::INTERLOCK_TRIPPED,
        points::LEVEL_ALARM,
        points::PUMP_FAULT,
        points::VALVE_DISCREPANCY,
        points::TRIP_COUNT,
        points::TRIPS_DONE,
        points::VOTER_DISCREPANCY,
        points::LATCH_ALARM,
        points::LATCH_UNACKNOWLEDGED,
        points::BATCH_STEP,
        points::BATCH_DONE,
        points::STATION_MANUAL,
    ] {
        assert!(journaled(point), "{point:?} must carry `journaled`");
    }
    for point in [
        points::LEVEL_SETPOINT,
        points::PUMP_START,
        points::VALVE_MANUAL,
        points::TRIP_COUNT_RESET,
        points::BATCH_RESET,
        points::ALARM_ACK,
        points::TOTAL_RESET,
        points::PUMP_COMMAND,
        points::HORN,
        points::BEACON,
        points::PUMP_RUNNING,
        points::RUN_FOR_MOTOR,
        points::RUN_FOR_PERMISSIVE,
        points::TRIP_PULSE,
        points::HORN_COMMAND,
        points::BEACON_COMMAND,
        points::BATCH_PROGRAM,
        points::PROGRAM_FOR_SELECT,
    ] {
        assert!(!journaled(point), "{point:?} must stay off the record");
    }
    for point in &model.io_points {
        if point.journaled {
            assert!(
                matches!(point.value_type, ValueKind::Bool | ValueKind::Int),
                "a journaled float slipped in: {point:?}"
            );
        }
    }
}

#[test]
fn the_dynamics_document_loads_with_the_second_order_tank() {
    // The same document `dcs-plant-server --dynamics` merges: a JSON
    // list of process-element declarations. The tank is a
    // second_order_lag — underdamped, so the mid-batch retune's
    // overshoot change is visible; the redundant transmitter legs and
    // the flow channel get first-order lags.
    let elements: Vec<ProcessElement> = serde_json::from_str(SHOWCASE_DYNAMICS_DOCUMENT)
        .expect("the dynamics document parses as process-element declarations");

    let [ProcessElement::SecondOrderLag(tank), rest @ ..] = elements.as_slice() else {
        panic!("the tank is the document's first element, a second_order_lag")
    };
    let SecondOrderLag {
        input,
        output,
        damping_ratio,
        ..
    } = *tank;
    assert_eq!((input, output), (points::VALVE_RAW, points::LEVEL_RAW));
    assert!(
        damping_ratio < 1.0,
        "the tank is underdamped — the retune visibly changes the overshoot"
    );

    let legs = [
        (points::LEVEL_RAW, points::LEVEL_B_RAW),
        (points::LEVEL_RAW, points::LEVEL_C_RAW),
        (points::VALVE_FEEDBACK_RAW, points::FLOW_RAW),
    ];
    assert_eq!(rest.len(), legs.len());
    for (element, endpoints) in rest.iter().zip(legs) {
        let ProcessElement::FirstOrderLag(lag) = element else {
            panic!("the transmitter legs and the flow are first_order_lag elements")
        };
        assert_eq!((lag.input, lag.output), endpoints);
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
    // fault, the voter's legs agreeing, the latch quiet, and the
    // start-delay trip already counted — the counter's first edge,
    // preset not yet reached — while the totalizer accumulates the
    // inlet flow.
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
    assert!(!bool_point(&run.settled, points::VOTER_DISCREPANCY));
    assert!(!bool_point(&run.settled, points::LATCH_ALARM));
    assert!(!bool_point(&run.settled, points::LATCH_UNACKNOWLEDGED));
    assert!(!bool_point(&run.settled, points::BEACON));
    assert_eq!(int_point(&run.settled, points::TRIP_COUNT), 1);
    assert!(!bool_point(&run.settled, points::TRIPS_DONE));
    assert!(
        float(&run.settled, points::FLOW_TOTAL) > 0.0,
        "the totalizer is accumulating the inlet flow"
    );
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
    assert_eq!(run.moved.tick, Tick(MOVED_TICK));
    assert!(
        (float(&run.moved, points::LEVEL_PERCENT) - MOVED_SETPOINT).abs() < 0.5,
        "the loop re-settled at the commanded setpoint"
    );
}

#[test]
fn the_batch_sequence_advances_and_the_retune_changes_the_overshoot() {
    let run = showcase::run(SHOWCASE_DOCUMENT).unwrap();

    // The operator's batch-mode and run writes apply at the boundary
    // after the moved phase; the mid-table `SetParameter` retune lands
    // inside the step-3 hold.
    for receipt in &run.receipts[1..=2] {
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Accepted {
                apply_tick: Tick(MOVED_TICK + 1)
            }
        );
    }
    assert_eq!(
        run.receipts[3].outcome,
        CommandOutcome::Accepted {
            apply_tick: Tick(MOVED_TICK + BATCH_PRE_TUNE_SCANS + 1)
        }
    );

    // The step trace walks the declared table in order — 40+80+120+80+80
    // scans — advancing on each step's tick boundary.
    assert_eq!(run.batch_steps.len(), BATCH_SCANS as usize);
    for (step, boundary) in [
        0,
        BATCH_STEP1_TICKS,
        BATCH_STEP1_TICKS + BATCH_STEP2_TICKS,
        BATCH_STEP1_TICKS + BATCH_STEP2_TICKS + BATCH_STEP3_TICKS,
        BATCH_STEP1_TICKS + BATCH_STEP2_TICKS + BATCH_STEP3_TICKS + BATCH_STEP4_TICKS,
    ]
    .iter()
    .enumerate()
    {
        assert_eq!(
            run.batch_steps[*boundary as usize],
            step as i64 + 1,
            "the sequencer reports step {} from batch scan {boundary}",
            step + 1
        );
    }
    assert!(
        run.batch_steps.windows(2).all(|pair| pair[1] >= pair[0]),
        "the step trace advances monotonically through the table"
    );

    // The table completes on its final scan: step 5 with `done`
    // asserted and the program carrier driving step 5's setpoint.
    assert_eq!(run.batched.tick, Tick(BATCHED_TICK));
    assert_eq!(int_point(&run.batched, points::BATCH_STEP), 5);
    assert!(bool_point(&run.batched, points::BATCH_DONE));
    assert_eq!(float(&run.batched, points::BATCH_PROGRAM), MOVED_SETPOINT);

    // The operator's exit commands drop `run` and pulse `reset`: the
    // sequencer returns to its first step — program back on step 1's
    // setpoint, `done` cleared — and the pid's setpoint returns to the
    // operator's value through the override select.
    assert_eq!(int_point(&run.deviated, points::BATCH_STEP), 1);
    assert_eq!(float(&run.deviated, points::BATCH_PROGRAM), MOVED_SETPOINT);
    assert!(!bool_point(&run.deviated, points::BATCH_DONE));

    // The tuning demonstration: the recipe's two identical up-steps
    // bracket the `kp` retune — on the underdamped tank the post-tune
    // peak overshoots visibly higher than the pre-tune one.
    let batch = MOVED_TICK as usize;
    let step2 = &run.levels[batch + BATCH_STEP1_TICKS as usize
        ..batch + (BATCH_STEP1_TICKS + BATCH_STEP2_TICKS) as usize];
    let step4 = &run.levels[batch
        + (BATCH_STEP1_TICKS + BATCH_STEP2_TICKS + BATCH_STEP3_TICKS) as usize
        ..batch
            + (BATCH_STEP1_TICKS + BATCH_STEP2_TICKS + BATCH_STEP3_TICKS + BATCH_STEP4_TICKS)
                as usize];
    let peak = |window: &[f64]| window.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
    assert!(
        peak(step4) > peak(step2) + 3.0,
        "the retuned loop overshoots visibly more on the second fill: {:.2} vs {:.2}",
        peak(step2),
        peak(step4)
    );
}

#[test]
fn the_voter_reports_the_deviant_transmitter_and_holds_the_good_pair() {
    let run = showcase::run(SHOWCASE_DOCUMENT).unwrap();

    // LT-101C held at full scale — the stuck-transmitter presentation:
    // the raw point reads the deviant value and the voter's
    // `discrepancy` asserts on the spread, while the median keeps the
    // voted level on the two good transmitters — the loop never sees
    // the deviant leg.
    assert_eq!(run.deviated.tick, Tick(DEVIATED_TICK));
    assert_eq!(float(&run.deviated, points::LEVEL_C_RAW), DEVIANT_RAW);
    assert!(bool_point(&run.deviated, points::VOTER_DISCREPANCY));
    assert!(
        (float(&run.deviated, points::LEVEL_PERCENT) - MOVED_SETPOINT).abs() < 3.0,
        "the median keeps the voted level on the good pair"
    );

    // Released, the lagged leg reconverges and the discrepancy clears.
    assert!(!bool_point(&run.manual_engaged, points::VOTER_DISCREPANCY));
}

#[test]
fn the_manual_station_slews_the_valve_command_and_returns_to_auto() {
    let run = showcase::run(SHOWCASE_DOCUMENT).unwrap();

    // The operator's manual-position and mode-select writes apply at
    // the boundary after the reconverged window.
    for receipt in &run.receipts[9..=10] {
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Accepted {
                apply_tick: Tick(AGREED_TICK + 1)
            }
        );
    }

    // The mid-slew snapshot: `manual_active` asserted while the station
    // is still walking the valve command from the controller's output
    // toward the manual position's raw value — strictly between them.
    assert_eq!(run.manual_engaged.tick, Tick(MANUAL_ENGAGED_TICK));
    assert!(bool_point(&run.manual_engaged, points::STATION_MANUAL));
    let manual_raw = 4.0 + 0.16 * MANUAL_POSITION;
    let slewed = float(&run.manual_engaged, points::VALVE_RAW);
    assert!(
        slewed > 10.5 && slewed < manual_raw - 0.05,
        "the valve command is mid-slew between the control value and the manual position: {slewed:.2}"
    );

    // The manual position opens the valve past the regulating command,
    // so the excursion lifts the level while the station holds.
    let excursion = &run.levels
        [MANUAL_ENGAGED_TICK as usize..MANUAL_ENGAGED_TICK as usize + MANUAL_HOLD_SCANS as usize];
    assert!(
        excursion.iter().cloned().fold(f64::NEG_INFINITY, f64::max) > MOVED_SETPOINT + 8.0,
        "the manual excursion visibly lifts the level"
    );

    // Dropping the mode slews back to the controller's output — the
    // bumpless return — and the loop re-settles at the operator
    // setpoint.
    assert_eq!(
        run.receipts[11].outcome,
        CommandOutcome::Accepted {
            apply_tick: Tick(MANUAL_ENGAGED_TICK + MANUAL_HOLD_SCANS + 1)
        }
    );
    assert_eq!(run.manual.tick, Tick(MANUAL_TICK));
    assert!(!bool_point(&run.manual, points::STATION_MANUAL));
    assert!(
        (float(&run.manual, points::LEVEL_PERCENT) - MOVED_SETPOINT).abs() < 0.5,
        "the loop re-settled after the station returned to auto"
    );
}

#[test]
fn the_latching_alarm_acknowledges_through_the_writable_internal_point() {
    let model = PlantModel::load(SHOWCASE_DOCUMENT).unwrap();
    let run = showcase::run(SHOWCASE_DOCUMENT).unwrap();

    // Decision 33's shape: the ack point is a writable internal `In`
    // point — the ordinary command path's surface, not a component
    // backdoor.
    let ack = model
        .io_points
        .iter()
        .find(|point| point.id == points::ALARM_ACK)
        .expect("the model declares the alarm's ack point");
    assert_eq!(ack.direction, Direction::In);
    assert!(ack.is_internal(), "the ack point is channel-less");
    assert!(ack.writable, "the ack point accepts WriteValue commands");

    // Mid-fault the latch is standing: alarm asserted, unacknowledged
    // asserted, the beacon lamp lit.
    assert_eq!(run.tripped.tick, Tick(TRIPPED_TICK));
    assert!(bool_point(&run.tripped, points::LATCH_ALARM));
    assert!(bool_point(&run.tripped, points::LATCH_UNACKNOWLEDGED));
    assert!(bool_point(&run.tripped, points::BEACON));

    // The ack pulse's two `WriteValue` submissions are receipted like
    // any other write — accepted for the next two scan boundaries.
    assert_eq!(
        run.receipts[12].outcome,
        CommandOutcome::Accepted {
            apply_tick: Tick(TRIPPED_TICK + 1)
        }
    );
    assert_eq!(
        run.receipts[13].outcome,
        CommandOutcome::Accepted {
            apply_tick: Tick(TRIPPED_TICK + 2)
        }
    );

    // Acknowledged: the unacknowledged flag and the beacon clear while
    // the standing alarm remains asserted — the level is still below
    // the low limit — and stays cleared for the rest of the fault.
    assert_eq!(run.acked.tick, Tick(ACKED_TICK));
    assert!(bool_point(&run.acked, points::LATCH_ALARM));
    assert!(!bool_point(&run.acked, points::LATCH_UNACKNOWLEDGED));
    assert!(!bool_point(&run.acked, points::BEACON));
    assert!(bool_point(&run.faulted, points::LATCH_ALARM));
    assert!(!bool_point(&run.faulted, points::LATCH_UNACKNOWLEDGED));
    assert!(!bool_point(&run.faulted, points::BEACON));
}

#[test]
fn the_totalizer_accumulates_flow_and_the_reset_restarts_it() {
    let run = showcase::run(SHOWCASE_DOCUMENT).unwrap();

    // The total grows while the inlet flows — through regulation, the
    // batch's fills, and the manual excursion.
    let settled = float(&run.settled, points::FLOW_TOTAL);
    let moved = float(&run.moved, points::FLOW_TOTAL);
    let faulted = float(&run.faulted, points::FLOW_TOTAL);
    assert!(settled > 0.0, "the totalizer is accumulating at settle");
    assert!(moved > settled, "the total keeps growing through the run");
    assert!(faulted > moved, "the total keeps growing under the fault");

    // The writable reset pulse drops the accumulated total to zero; the
    // recovered total is only what the final scans re-accumulated.
    for (index, apply_tick) in [(14, TOTAL_SCANS - 3), (15, TOTAL_SCANS - 2)] {
        assert_eq!(
            run.receipts[index].outcome,
            CommandOutcome::Accepted {
                apply_tick: Tick(apply_tick)
            }
        );
    }
    assert!(
        float(&run.recovered, points::FLOW_TOTAL) < 20.0,
        "the reset totalizer re-accumulates from zero"
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
    assert_eq!(run.faulted.tick, Tick(FAULTED_TICK));
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
    assert!(!bool_point(&run.recovered, points::LATCH_ALARM));
    assert!(!bool_point(&run.recovered, points::LATCH_UNACKNOWLEDGED));
    assert!(!bool_point(&run.recovered, points::BEACON));
    assert!(
        (float(&run.recovered, points::LEVEL_PERCENT) - MOVED_SETPOINT).abs() < 0.5,
        "the level recovered to the moved setpoint"
    );
    assert_eq!(int_point(&run.recovered, points::TRIP_COUNT), 0);
    assert!(!bool_point(&run.recovered, points::TRIPS_DONE));

    // The reset's assert and release were accepted for the last two
    // scan boundaries.
    assert_eq!(
        run.receipts[16].outcome,
        CommandOutcome::Accepted {
            apply_tick: Tick(TOTAL_SCANS - 1)
        }
    );
    assert_eq!(
        run.receipts[17].outcome,
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
        &first.batched,
        &first.deviated,
        &first.manual_engaged,
        &first.manual,
        &first.tripped,
        &first.acked,
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
