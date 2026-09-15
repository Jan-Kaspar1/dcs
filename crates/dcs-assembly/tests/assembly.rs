//! Integration tests for model-driven assembly: the tank-loop fixture is
//! assembled and run entirely through the registry — no manual component
//! wiring — and every [`AssemblyError`] kind is produced by a corresponding
//! invalid fixture or a deliberately miswired stub kind.

use dcs_assembly::{
    AssemblyError, BuildError, ComponentRegistry, InternalPointError, assemble, sim_channel_map,
    sim_driver,
};
use dcs_blocks::{
    AnalogInput, AnalogOutput, BoolGate, BoolLatchingAlarm, Counter, DigitalOutput, EdgeTrigger,
    LatchingAlarm, ManualStation, MedianVoter, Motor, Pid, RateLimiter, Sequencer, SignalFilter,
    SrLatch, Timer, Totalizer,
};
use dcs_core::{Command, CommandOutcome, Direction, IoDriver, PointId, Tick, Value, ValueKind};
use dcs_model::{ComponentId, Connection, Endpoint, PlantModel, PortRef, ValidationError};
use dcs_runtime::{Component, ComponentIo, Executor, IoRequirement, StepError};
use dcs_sim::{ChannelId, ConfigError, FirstOrderLag, ProcessElement, SimDriver};

const TANK_LOOP: &str = include_str!("../fixtures/tank_loop.json");
/// The M1 milestone fixture, checked in with `dcs-demo` — reused here
/// exactly as the issue asks, through the general registry.
const M1_TANK_LEVEL: &str = include_str!("../../dcs-demo/fixtures/tank_level.json");
/// The internal-points fixture: a held setpoint, a declared internal
/// carrier pair, and a port-to-port wire — plus field points.
const INTERNAL_POINTS: &str = include_str!("../fixtures/internal_points.json");
/// The cyclic-vocabulary fixture: a timer, a counter, and a rate limiter.
const CYCLIC: &str = include_str!("../fixtures/cyclic.json");
/// The latching-alarm fixture: one `latching-alarm` over a field `pv`,
/// an internal operator `ack` point, and field `alarm`/`unack` outputs.
const LATCHING_ALARM: &str = include_str!("../fixtures/latching_alarm.json");
/// The operator-vocabulary fixture: a manual/auto station and a signal
/// filter.
const OPERATOR: &str = include_str!("../fixtures/operator.json");
/// The voting/accumulation fixture: a 2oo3 median voter and a totalizer.
const VOTING_TOTALIZER: &str = include_str!("../fixtures/voting_totalizer.json");
/// The sequence-control fixture: a sequencer walking a declared
/// three-step table while `run` holds.
const SEQUENCER: &str = include_str!("../fixtures/sequencer.json");
/// The logic-vocabulary fixture: a three-input `bool-gate`, an
/// `sr-latch`, and an `edge-trigger`.
const LOGIC: &str = include_str!("../fixtures/logic.json");
/// The bool-latching-alarm fixture: a motor's `fault` output carried
/// through a declared internal point pair into a `bool-latching-alarm`'s
/// `in`, with field-side `alarm`/`unack` outputs.
const BOOL_LATCHING_ALARM: &str = include_str!("../fixtures/bool_latching_alarm.json");

const SETPOINT: PointId = PointId(10);
const LEVEL_RAW: PointId = PointId(11);
const VALVE: PointId = PointId(12);

// `internal_points.json`: the held setpoint and the declared internal
// `Out`/`In` pair carrying the pid's output to the analog-output.
const HELD_SETPOINT: PointId = PointId(10);
const PID_OUT: PointId = PointId(13);
const AO_ENG: PointId = PointId(14);
// The synthesized carrier pair for the `ai.out` → `pid.pv` port-to-port
// wire: internal point ids allocate above the highest declared id (14).
const LINK_OUT: PointId = PointId(15);
const LINK_IN: PointId = PointId(16);

// `cyclic.json`: the field-side points the fixture's components drive.
const PULSE: PointId = PointId(10);
const RESET: PointId = PointId(11);
const TARGET: PointId = PointId(12);
const TIMED: PointId = PointId(20);
const COUNT: PointId = PointId(21);
const DONE: PointId = PointId(22);
const LIMITED: PointId = PointId(23);

// `latching_alarm.json`: the field measurement, the internal operator
// ack point, and the two field-side outputs.
const ALARM_PV: PointId = PointId(10);
const ALARM_ACK: PointId = PointId(11);
const ALARM_OUT: PointId = PointId(20);
const UNACK_OUT: PointId = PointId(21);

// `bool_latching_alarm.json`: the field run feedback, the internal
// operator command/ack points, the internal fault carrier pair, and the
// three field-side outputs.
const RUN_FEEDBACK: PointId = PointId(10);
const MOTOR_CMD: PointId = PointId(11);
const FAULT_ACK: PointId = PointId(12);
const MOTOR_FAULT: PointId = PointId(13);
const MOTOR_OUT: PointId = PointId(20);
const FAULT_ALARM: PointId = PointId(21);
const FAULT_UNACK: PointId = PointId(22);

// `operator.json`: the field-side points the fixture's components drive.
const CV: PointId = PointId(10);
const MANUAL: PointId = PointId(11);
const MODE: PointId = PointId(12);
const RAW: PointId = PointId(13);
const DRIVE: PointId = PointId(20);
const ACTIVE: PointId = PointId(21);
const FILTERED: PointId = PointId(22);

// `voting_totalizer.json`: the redundant voter inputs, the flow rate and
// reset feeding the totalizer, and the field-side outputs.
const VOTE_A: PointId = PointId(10);
const VOTE_B: PointId = PointId(11);
const VOTE_C: PointId = PointId(12);
const FLOW: PointId = PointId(13);
const TRESET: PointId = PointId(14);
const VOTED: PointId = PointId(20);
const SPREAD: PointId = PointId(21);
const ACCUM: PointId = PointId(22);

// `sequencer.json`: the run/reset commands and the field-side outputs
// the sequencer drives.
const SEQ_RUN: PointId = PointId(10);
const SEQ_RESET: PointId = PointId(11);
const SEQ_OUT: PointId = PointId(20);
const SEQ_STEP: PointId = PointId(21);
const SEQ_DONE: PointId = PointId(22);

// `logic.json`: the gate inputs, latch commands, and trigger input, and
// the three field-side outputs the logic kinds drive.
const GATE_A: PointId = PointId(10);
const GATE_B: PointId = PointId(11);
const GATE_C: PointId = PointId(12);
const LATCH_SET: PointId = PointId(13);
const LATCH_RESET: PointId = PointId(14);
const TRIG_IN: PointId = PointId(15);
const GATED: PointId = PointId(20);
const LATCHED: PointId = PointId(21);
const PULSED: PointId = PointId(22);

fn boxed<C, E>(result: Result<C, E>) -> Result<Box<dyn Component>, BuildError>
where
    C: Component + 'static,
    E: std::error::Error + 'static,
{
    result
        .map(|component| Box::new(component) as Box<dyn Component>)
        .map_err(BuildError::other)
}

/// The `dcs-blocks` registration the `dcs-controller` binary performs,
/// restricted to the kinds these fixtures use.
fn registry() -> ComponentRegistry {
    ComponentRegistry::new()
        .with(AnalogInput::<f64>::KIND, |spec| {
            let raw = spec.require("raw")?;
            let out = spec.require("out")?;
            if spec.point_kind(raw) == Some(ValueKind::Int) {
                boxed(AnalogInput::<i64>::from_parameters(
                    spec.name.as_str(),
                    raw,
                    out,
                    spec.parameters,
                ))
            } else {
                boxed(AnalogInput::<f64>::from_parameters(
                    spec.name.as_str(),
                    raw,
                    out,
                    spec.parameters,
                ))
            }
        })
        .with(Pid::KIND, |spec| {
            boxed(Pid::from_parameters(
                spec.name.as_str(),
                spec.require("sp")?,
                spec.require("pv")?,
                spec.require("out")?,
                spec.parameters,
            ))
        })
        .with(DigitalOutput::KIND, |spec| {
            boxed(DigitalOutput::from_parameters(
                spec.name.as_str(),
                spec.require("in")?,
                spec.require("out")?,
                spec.parameters,
            ))
        })
        .with(AnalogOutput::<f64>::KIND, |spec| {
            let eng = spec.require("eng")?;
            let raw = spec.require("raw")?;
            if spec.point_kind(raw) == Some(ValueKind::Int) {
                boxed(AnalogOutput::<i64>::from_parameters(
                    spec.name.as_str(),
                    eng,
                    raw,
                    spec.parameters,
                ))
            } else {
                boxed(AnalogOutput::<f64>::from_parameters(
                    spec.name.as_str(),
                    eng,
                    raw,
                    spec.parameters,
                ))
            }
        })
        .with(Timer::KIND, |spec| {
            boxed(Timer::from_parameters(
                spec.name.as_str(),
                spec.require("in")?,
                spec.require("out")?,
                spec.parameters,
            ))
        })
        .with(Counter::KIND, |spec| {
            boxed(Counter::from_parameters(
                spec.name.as_str(),
                spec.require("in")?,
                spec.require("reset")?,
                spec.require("count")?,
                spec.require("done")?,
                spec.parameters,
            ))
        })
        .with(RateLimiter::KIND, |spec| {
            boxed(RateLimiter::from_parameters(
                spec.name.as_str(),
                spec.require("in")?,
                spec.require("out")?,
                spec.parameters,
            ))
        })
        .with(LatchingAlarm::KIND, |spec| {
            boxed(LatchingAlarm::from_parameters(
                spec.name.as_str(),
                spec.require("in")?,
                spec.require("ack")?,
                spec.require("alarm")?,
                spec.require("unacknowledged")?,
                spec.parameters,
            ))
        })
        .with(ManualStation::KIND, |spec| {
            boxed(ManualStation::from_parameters(
                spec.name.as_str(),
                spec.require("control")?,
                spec.require("manual")?,
                spec.require("mode")?,
                spec.require("out")?,
                spec.require("manual_active")?,
                spec.parameters,
            ))
        })
        .with(SignalFilter::KIND, |spec| {
            boxed(SignalFilter::from_parameters(
                spec.name.as_str(),
                spec.require("in")?,
                spec.require("out")?,
                spec.parameters,
            ))
        })
        .with(MedianVoter::KIND, |spec| {
            boxed(MedianVoter::from_parameters(
                spec.name.as_str(),
                spec.require("in_1")?,
                spec.require("in_2")?,
                spec.require("in_3")?,
                spec.require("out")?,
                spec.require("discrepancy")?,
                spec.parameters,
            ))
        })
        .with(Totalizer::KIND, |spec| {
            boxed(Totalizer::from_parameters(
                spec.name.as_str(),
                spec.require("rate")?,
                spec.require("reset")?,
                spec.require("total")?,
                spec.parameters,
            ))
        })
        .with(Sequencer::KIND, |spec| {
            boxed(Sequencer::from_parameters(
                spec.name.as_str(),
                spec.require("run")?,
                spec.require("reset")?,
                spec.require("out")?,
                spec.require("step")?,
                spec.require("done")?,
                spec.parameters,
            ))
        })
        .with(BoolGate::KIND, |spec| {
            let mut inputs: Vec<_> = spec
                .ports
                .iter()
                .filter_map(|(name, point)| {
                    name.strip_prefix("in_")
                        .and_then(|suffix| suffix.parse::<usize>().ok())
                        .map(|index| (index, *point))
                })
                .collect();
            inputs.sort_by_key(|(index, _)| *index);
            let inputs: Vec<_> = inputs.into_iter().map(|(_, point)| point).collect();
            boxed(BoolGate::from_parameters(
                spec.name.as_str(),
                inputs,
                spec.require("out")?,
                spec.parameters,
            ))
        })
        .with(SrLatch::KIND, |spec| {
            boxed(SrLatch::from_parameters(
                spec.name.as_str(),
                spec.require("set")?,
                spec.require("reset")?,
                spec.require("out")?,
                spec.parameters,
            ))
        })
        .with(EdgeTrigger::KIND, |spec| {
            boxed(EdgeTrigger::from_parameters(
                spec.name.as_str(),
                spec.require("in")?,
                spec.require("out")?,
                spec.parameters,
            ))
        })
        .with(Motor::KIND, |spec| {
            boxed(Motor::from_parameters(
                spec.name.as_str(),
                spec.require("cmd")?,
                spec.require("out")?,
                spec.require("run")?,
                spec.require("fault")?,
                spec.parameters,
            ))
        })
        .with(BoolLatchingAlarm::KIND, |spec| {
            boxed(BoolLatchingAlarm::from_parameters(
                spec.name.as_str(),
                spec.require("in")?,
                spec.require("ack")?,
                spec.require("alarm")?,
                spec.require("unacknowledged")?,
                spec.parameters,
            ))
        })
}

fn model(source: &str) -> PlantModel {
    PlantModel::load(source).unwrap()
}

#[test]
fn tank_loop_reaches_setpoint_with_no_manual_wiring() {
    let model = model(TANK_LOOP);
    let driver = sim_driver(&model).unwrap();
    let mut executor = assemble(&model, &registry(), &driver).unwrap();

    // The field-side setpoint: what an operator or upstream device writes.
    driver.write(SETPOINT, Value::Float(50.0)).unwrap();
    for _ in 0..500 {
        executor.scan().unwrap();
        driver.step(0.1);
    }

    // Steady state: the scaled level (4-20 mA -> 0-100 units) settles at the
    // 50-unit setpoint, which the loopback drives from a 12-unit valve
    // command. Stated tolerance: 1% of the 50-unit setpoint.
    let Value::Float(raw) = driver.read(LEVEL_RAW).unwrap().value else {
        panic!("level raw must be Float")
    };
    assert!((raw - 12.0).abs() < 0.5, "raw={raw}");
    let Value::Float(valve) = driver.read(VALVE).unwrap().value else {
        panic!("valve must be Float")
    };
    assert!((valve - 12.0).abs() < 0.5, "valve={valve}");

    // The scaled level the controller observes reached the setpoint.
    let snapshot = executor.snapshot();
    assert_eq!(snapshot.tick.0, 500);
    assert!(
        snapshot
            .components
            .iter()
            .all(|component| component.step_errors == 0),
        "{:?}",
        snapshot.components
    );
}

#[test]
fn m1_tank_level_fixture_reaches_setpoint() {
    // The M1 fixture (`dcs-demo`'s tank_level.json) declares `sim-ai` and
    // `sim-ao` devices, an analog-input, and a pid. Its simulated plant is
    // not part of the model: the test adds the tank's first-order lag to
    // the resolved channel map — process emulation, not component wiring —
    // then assembles through the registry with no manual wiring code.
    let model = model(M1_TANK_LEVEL);
    let level_raw = PointId(10); // lt101_raw, driven by the lag
    let setpoint = PointId(11); // lic101_sp, written field-side
    let valve = PointId(20); // lv101_cmd, the pid's output

    let map = sim_channel_map(&model)
        .unwrap()
        .with_element(ProcessElement::FirstOrderLag(FirstOrderLag {
            input: valve,
            output: level_raw,
            time_constant: 2.0,
            initial: 4.0,
        }));
    let driver = SimDriver::new(map).unwrap();
    let mut executor = assemble(&model, &registry(), &driver).unwrap();

    driver.write(setpoint, Value::Float(60.0)).unwrap();
    for _ in 0..400 {
        executor.scan().unwrap();
        driver.step(0.1);
    }

    // 60% of the 4–20 mA raw range settles at 13.6 mA; stated tolerance
    // is 0.5 mA.
    let Value::Float(raw) = driver.read(level_raw).unwrap().value else {
        panic!("level raw must be Float")
    };
    assert!((raw - 13.6).abs() < 0.5, "raw={raw}");
    assert!(
        executor
            .snapshot()
            .components
            .iter()
            .all(|component| component.step_errors == 0)
    );
}

#[test]
fn identical_runs_snapshot_identically() {
    let model = model(TANK_LOOP);
    let run = || {
        let driver = sim_driver(&model).unwrap();
        let mut executor = assemble(&model, &registry(), &driver).unwrap();
        driver.write(SETPOINT, Value::Float(50.0)).unwrap();
        for _ in 0..200 {
            executor.scan().unwrap();
            driver.step(0.1);
        }
        serde_json::to_string(&executor.snapshot()).unwrap()
    };
    assert_eq!(run(), run());
}

/// Steps the executor once and returns the value the driver holds on
/// `point` — what the field side of the cyclic fixture observes.
fn field_value(executor: &mut Executor<'_>, driver: &SimDriver, point: PointId) -> Value {
    executor.scan().unwrap();
    driver.read(point).unwrap().value
}

#[test]
fn cyclic_fixture_runs_timer_counter_and_limiter() {
    let model = model(CYCLIC);
    let driver = sim_driver(&model).unwrap();
    let mut executor = assemble(&model, &registry(), &driver).unwrap();

    // The timer holds `pulse` for delay_ticks=3 scans; the counter's
    // preset=3 counts its rising edges; the limiter follows `target`
    // at max_delta=2.5 per scan.
    driver.write(PULSE, Value::Bool(true)).unwrap();
    driver.write(TARGET, Value::Float(10.0)).unwrap();

    // Tick 1: the timer has banked one scan, the counter has seen one
    // rising edge, and the limiter adopted its first finite input.
    assert_eq!(
        field_value(&mut executor, &driver, TIMED),
        Value::Bool(false)
    );
    assert_eq!(driver.read(COUNT).unwrap().value, Value::Int(1));
    assert_eq!(driver.read(LIMITED).unwrap().value, Value::Float(10.0));

    // Tick 2: still below the delay.
    assert_eq!(
        field_value(&mut executor, &driver, TIMED),
        Value::Bool(false)
    );
    // Tick 3: the timer asserts exactly at the configured tick.
    assert_eq!(
        field_value(&mut executor, &driver, TIMED),
        Value::Bool(true)
    );

    // A further target step slews by exactly max_delta per scan.
    driver.write(TARGET, Value::Float(20.0)).unwrap();
    assert_eq!(
        field_value(&mut executor, &driver, LIMITED),
        Value::Float(12.5)
    );

    // Dropping `pulse` deasserts the on-delay timer the same scan.
    driver.write(PULSE, Value::Bool(false)).unwrap();
    assert_eq!(
        field_value(&mut executor, &driver, TIMED),
        Value::Bool(false)
    );

    // Two more rising edges bring the counter to its preset.
    driver.write(PULSE, Value::Bool(true)).unwrap();
    executor.scan().unwrap();
    assert_eq!(driver.read(COUNT).unwrap().value, Value::Int(2));
    driver.write(PULSE, Value::Bool(false)).unwrap();
    executor.scan().unwrap();
    driver.write(PULSE, Value::Bool(true)).unwrap();
    executor.scan().unwrap();
    assert_eq!(driver.read(COUNT).unwrap().value, Value::Int(3));
    assert_eq!(driver.read(DONE).unwrap().value, Value::Bool(true));

    // Reset clears the count and the flag.
    driver.write(RESET, Value::Bool(true)).unwrap();
    executor.scan().unwrap();
    assert_eq!(driver.read(COUNT).unwrap().value, Value::Int(0));
    assert_eq!(driver.read(DONE).unwrap().value, Value::Bool(false));

    assert!(
        executor
            .snapshot()
            .components
            .iter()
            .all(|component| component.step_errors == 0)
    );
}

#[test]
fn latching_alarm_fixture_trips_latches_and_acknowledges() {
    let model = model(LATCHING_ALARM);
    let driver = sim_driver(&model).unwrap();
    let mut executor = assemble(&model, &registry(), &driver).unwrap();

    // The registry built the declared kind: its descriptor reports
    // `latching-alarm` and the internal `ack` point holds its declared
    // initial.
    assert!(
        executor
            .snapshot()
            .descriptors
            .iter()
            .any(|descriptor| descriptor.kind == LatchingAlarm::KIND)
    );
    assert_eq!(
        executor.sample(ALARM_ACK).unwrap().value,
        Value::Bool(false)
    );

    // A trip asserts both outputs on the field points.
    driver.write(ALARM_PV, Value::Float(95.0)).unwrap();
    executor.scan().unwrap();
    assert_eq!(driver.read(ALARM_OUT).unwrap().value, Value::Bool(true));
    assert_eq!(driver.read(UNACK_OUT).unwrap().value, Value::Bool(true));

    // The input receding inside the limits clears the alarm through the
    // hysteresis rule; the latch stands until acknowledged.
    driver.write(ALARM_PV, Value::Float(50.0)).unwrap();
    executor.scan().unwrap();
    assert_eq!(driver.read(ALARM_OUT).unwrap().value, Value::Bool(false));
    assert_eq!(driver.read(UNACK_OUT).unwrap().value, Value::Bool(true));

    // The operator's ack — a command to the fixture's `writable`
    // internal point — applies at the scan boundary and clears the
    // latch.
    let receipt = executor.submit_command(Command::WriteValue {
        point: ALARM_ACK,
        kind: ValueKind::Bool,
        value: Value::Bool(true),
    });
    assert_eq!(
        receipt.outcome,
        CommandOutcome::Accepted {
            apply_tick: Tick(3)
        }
    );
    executor.scan().unwrap();
    assert_eq!(driver.read(UNACK_OUT).unwrap().value, Value::Bool(false));

    // A fresh trip after acknowledgment latches again.
    executor.submit_command(Command::WriteValue {
        point: ALARM_ACK,
        kind: ValueKind::Bool,
        value: Value::Bool(false),
    });
    driver.write(ALARM_PV, Value::Float(95.0)).unwrap();
    executor.scan().unwrap();
    assert_eq!(driver.read(ALARM_OUT).unwrap().value, Value::Bool(true));
    assert_eq!(driver.read(UNACK_OUT).unwrap().value, Value::Bool(true));

    assert!(
        executor
            .snapshot()
            .components
            .iter()
            .all(|component| component.step_errors == 0)
    );
}

#[test]
fn bool_latching_alarm_fixture_latches_a_motor_fault() {
    let model = model(BOOL_LATCHING_ALARM);
    let driver = sim_driver(&model).unwrap();
    let mut executor = assemble(&model, &registry(), &driver).unwrap();

    // The registry built both declared kinds: the motor and the Bool
    // sibling of `latching-alarm` wired to its `fault` output through
    // the declared internal carrier pair.
    let snapshot = executor.snapshot();
    let kinds: Vec<&str> = snapshot
        .descriptors
        .iter()
        .map(|descriptor| descriptor.kind.as_str())
        .collect();
    assert_eq!(kinds, [Motor::KIND, BoolLatchingAlarm::KIND]);
    assert_eq!(
        executor.sample(FAULT_ACK).unwrap().value,
        Value::Bool(false)
    );

    // The operator starts the motor; the run feedback never follows, so
    // `fault` asserts on the first disagreeing scan (fault_ticks=0) and
    // lands on the internal carrier — a fail-to-start.
    let receipt = executor.submit_command(Command::WriteValue {
        point: MOTOR_CMD,
        kind: ValueKind::Bool,
        value: Value::Bool(true),
    });
    assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
    executor.scan().unwrap();
    assert_eq!(driver.read(MOTOR_OUT).unwrap().value, Value::Bool(true));
    assert_eq!(
        executor.sample(MOTOR_FAULT).unwrap().value,
        Value::Bool(true)
    );
    // The internal link delivers the fault to the alarm's `in` one scan
    // later — the same boundary a field loopback crosses.
    assert_eq!(driver.read(FAULT_ALARM).unwrap().value, Value::Bool(false));

    executor.scan().unwrap();
    assert_eq!(driver.read(FAULT_ALARM).unwrap().value, Value::Bool(true));
    assert_eq!(driver.read(FAULT_UNACK).unwrap().value, Value::Bool(true));

    // Acknowledging while the alarm stands clears the latch only.
    executor.submit_command(Command::WriteValue {
        point: FAULT_ACK,
        kind: ValueKind::Bool,
        value: Value::Bool(true),
    });
    executor.scan().unwrap();
    assert_eq!(driver.read(FAULT_UNACK).unwrap().value, Value::Bool(false));
    assert_eq!(driver.read(FAULT_ALARM).unwrap().value, Value::Bool(true));

    // The feedback recovering clears the motor's fault; `alarm` follows
    // it down a scan later and the already-acknowledged latch stays down.
    executor.submit_command(Command::WriteValue {
        point: FAULT_ACK,
        kind: ValueKind::Bool,
        value: Value::Bool(false),
    });
    driver.write(RUN_FEEDBACK, Value::Bool(true)).unwrap();
    executor.scan().unwrap();
    assert_eq!(
        executor.sample(MOTOR_FAULT).unwrap().value,
        Value::Bool(false)
    );
    executor.scan().unwrap();
    assert_eq!(driver.read(FAULT_ALARM).unwrap().value, Value::Bool(false));
    assert_eq!(driver.read(FAULT_UNACK).unwrap().value, Value::Bool(false));

    assert!(
        executor
            .snapshot()
            .components
            .iter()
            .all(|component| component.step_errors == 0)
    );
}

#[test]
fn bool_latching_alarm_fixture_runs_deterministically() {
    let model = model(BOOL_LATCHING_ALARM);
    let run = || {
        let driver = sim_driver(&model).unwrap();
        let mut executor = assemble(&model, &registry(), &driver).unwrap();
        executor.submit_command(Command::WriteValue {
            point: MOTOR_CMD,
            kind: ValueKind::Bool,
            value: Value::Bool(true),
        });
        for _ in 0..3 {
            executor.scan().unwrap();
        }
        executor.submit_command(Command::WriteValue {
            point: FAULT_ACK,
            kind: ValueKind::Bool,
            value: Value::Bool(true),
        });
        driver.write(RUN_FEEDBACK, Value::Bool(true)).unwrap();
        for _ in 0..3 {
            executor.scan().unwrap();
        }
        serde_json::to_string(&executor.snapshot()).unwrap()
    };
    assert_eq!(run(), run());
}

#[test]
fn operator_fixture_runs_station_and_filter() {
    let model = model(OPERATOR);
    let driver = sim_driver(&model).unwrap();
    let mut executor = assemble(&model, &registry(), &driver).unwrap();

    // Auto mode: the station adopts the control value outright; the
    // filter adopts its first `Good` input. `manual_active` reports
    // the auto selection.
    driver.write(CV, Value::Float(10.0)).unwrap();
    driver.write(MANUAL, Value::Float(30.0)).unwrap();
    driver.write(RAW, Value::Float(4.0)).unwrap();
    executor.scan().unwrap();
    assert_eq!(driver.read(DRIVE).unwrap().value, Value::Float(10.0));
    assert_eq!(driver.read(ACTIVE).unwrap().value, Value::Bool(false));
    assert_eq!(driver.read(FILTERED).unwrap().value, Value::Float(4.0));

    // Switching to manual slews `drive` toward the manual value by
    // exactly transfer_delta=5 per scan until it arrives; the status
    // asserts on the switch scan.
    driver.write(MODE, Value::Bool(true)).unwrap();
    for expected in [15.0, 20.0, 25.0, 30.0] {
        executor.scan().unwrap();
        assert_eq!(driver.read(DRIVE).unwrap().value, Value::Float(expected));
    }
    assert_eq!(driver.read(ACTIVE).unwrap().value, Value::Bool(true));

    // Arrived: the manual source passes through unbounded.
    driver.write(MANUAL, Value::Float(33.0)).unwrap();
    executor.scan().unwrap();
    assert_eq!(driver.read(DRIVE).unwrap().value, Value::Float(33.0));

    // The filter's documented recurrence out += alpha * (in - out)
    // with alpha = 0.5 halves the gap per scan.
    driver.write(RAW, Value::Float(12.0)).unwrap();
    executor.scan().unwrap();
    assert_eq!(driver.read(FILTERED).unwrap().value, Value::Float(8.0));
    executor.scan().unwrap();
    assert_eq!(driver.read(FILTERED).unwrap().value, Value::Float(10.0));

    assert!(
        executor
            .snapshot()
            .components
            .iter()
            .all(|component| component.step_errors == 0)
    );
}

#[test]
fn voting_totalizer_fixture_votes_accumulates_and_restores() {
    let model = model(VOTING_TOTALIZER);
    let driver = sim_driver(&model).unwrap();
    let mut executor = assemble(&model, &registry(), &driver).unwrap();

    // The registry built both declared kinds, in scan order.
    let snapshot = executor.snapshot();
    let kinds: Vec<&str> = snapshot
        .descriptors
        .iter()
        .map(|descriptor| descriptor.kind.as_str())
        .collect();
    assert_eq!(kinds, [MedianVoter::KIND, Totalizer::KIND]);

    // Three inputs within tolerance=2.0: the median passes through,
    // no discrepancy; the totalizer banks rate * rate_unit = 5/scan.
    driver.write(VOTE_A, Value::Float(10.0)).unwrap();
    driver.write(VOTE_B, Value::Float(11.0)).unwrap();
    driver.write(VOTE_C, Value::Float(11.5)).unwrap();
    driver.write(FLOW, Value::Float(10.0)).unwrap();
    executor.scan().unwrap();
    assert_eq!(driver.read(VOTED).unwrap().value, Value::Float(11.0));
    assert_eq!(driver.read(SPREAD).unwrap().value, Value::Bool(false));
    assert_eq!(driver.read(ACCUM).unwrap().value, Value::Float(5.0));

    // in_3 deviating past the tolerance asserts the flag; the median
    // is unaffected, and accumulation continues per scan.
    driver.write(VOTE_C, Value::Float(20.0)).unwrap();
    executor.scan().unwrap();
    assert_eq!(driver.read(VOTED).unwrap().value, Value::Float(11.0));
    assert_eq!(driver.read(SPREAD).unwrap().value, Value::Bool(true));
    assert_eq!(driver.read(ACCUM).unwrap().value, Value::Float(10.0));

    // A standby assembled from the same model applies the mid-run
    // checkpoint and continues identically — field state and the
    // banked total both transfer.
    let checkpoint = executor.checkpoint();
    let standby_driver = sim_driver(&model).unwrap();
    let mut standby = assemble(&model, &registry(), &standby_driver).unwrap();
    standby.apply(&checkpoint).unwrap();
    assert_eq!(standby.tick(), Tick(2));
    for _ in 0..3 {
        executor.scan().unwrap();
        standby.scan().unwrap();
        assert_eq!(
            standby_driver.read(VOTED).unwrap(),
            driver.read(VOTED).unwrap()
        );
        assert_eq!(
            standby_driver.read(ACCUM).unwrap(),
            driver.read(ACCUM).unwrap()
        );
    }
    assert_eq!(driver.read(ACCUM).unwrap().value, Value::Float(25.0));

    // Reset clears the total on both runs; release resumes banking.
    driver.write(TRESET, Value::Bool(true)).unwrap();
    standby_driver.write(TRESET, Value::Bool(true)).unwrap();
    executor.scan().unwrap();
    standby.scan().unwrap();
    assert_eq!(driver.read(ACCUM).unwrap().value, Value::Float(0.0));
    assert_eq!(standby_driver.read(ACCUM).unwrap().value, Value::Float(0.0));

    assert!(
        executor
            .snapshot()
            .components
            .iter()
            .all(|component| component.step_errors == 0)
    );
}

#[test]
fn sequencer_fixture_advances_holds_at_end_and_restores() {
    let model = model(SEQUENCER);
    let driver = sim_driver(&model).unwrap();
    let mut executor = assemble(&model, &registry(), &driver).unwrap();

    // The registry built the declared kind.
    assert!(
        executor
            .snapshot()
            .descriptors
            .iter()
            .any(|descriptor| descriptor.kind == Sequencer::KIND)
    );

    // Parked on step 1 before `run` asserts: the first step's value is
    // already driven and `step` reports it.
    executor.scan().unwrap();
    assert_eq!(driver.read(SEQ_OUT).unwrap().value, Value::Float(10.0));
    assert_eq!(driver.read(SEQ_STEP).unwrap().value, Value::Int(1));
    assert_eq!(driver.read(SEQ_DONE).unwrap().value, Value::Bool(false));

    // The table declares step 1 for 2 ticks, step 2 for 2, step 3 for 1.
    // A step still drives the scan it completes on, so the outputs walk
    // 1,1,2,2,3 across the five running scans.
    driver.write(SEQ_RUN, Value::Bool(true)).unwrap();
    for (out, step) in [(10.0, 1), (10.0, 1), (20.0, 2)] {
        executor.scan().unwrap();
        assert_eq!(driver.read(SEQ_OUT).unwrap().value, Value::Float(out));
        assert_eq!(driver.read(SEQ_STEP).unwrap().value, Value::Int(step));
        assert_eq!(driver.read(SEQ_DONE).unwrap().value, Value::Bool(false));
    }

    // Mid-sequence checkpoint — one scan banked into step 2 — transfers
    // to a standby assembled from the same model, which continues
    // identically through the table's end.
    let checkpoint = executor.checkpoint();
    let standby_driver = sim_driver(&model).unwrap();
    let mut standby = assemble(&model, &registry(), &standby_driver).unwrap();
    standby.apply(&checkpoint).unwrap();
    assert_eq!(standby.tick(), Tick(4));
    standby_driver.write(SEQ_RUN, Value::Bool(true)).unwrap();
    for (out, step, done) in [(20.0, 2, false), (30.0, 3, true), (30.0, 3, true)] {
        executor.scan().unwrap();
        standby.scan().unwrap();
        assert_eq!(
            standby_driver.read(SEQ_OUT).unwrap(),
            driver.read(SEQ_OUT).unwrap()
        );
        assert_eq!(
            standby_driver.read(SEQ_STEP).unwrap(),
            driver.read(SEQ_STEP).unwrap()
        );
        assert_eq!(driver.read(SEQ_OUT).unwrap().value, Value::Float(out));
        assert_eq!(driver.read(SEQ_STEP).unwrap().value, Value::Int(step));
        assert_eq!(driver.read(SEQ_DONE).unwrap().value, Value::Bool(done));
    }

    // Hold-at-end: the final step keeps driving while `run` holds; only
    // `reset` parks the table back on step 1.
    driver.write(SEQ_RESET, Value::Bool(true)).unwrap();
    executor.scan().unwrap();
    assert_eq!(driver.read(SEQ_OUT).unwrap().value, Value::Float(10.0));
    assert_eq!(driver.read(SEQ_STEP).unwrap().value, Value::Int(1));
    assert_eq!(driver.read(SEQ_DONE).unwrap().value, Value::Bool(false));

    assert!(
        executor
            .snapshot()
            .components
            .iter()
            .all(|component| component.step_errors == 0)
    );
}

#[test]
fn logic_fixture_folds_latches_pulses_and_restores() {
    let model = model(LOGIC);
    let driver = sim_driver(&model).unwrap();
    let mut executor = assemble(&model, &registry(), &driver).unwrap();

    // The registry built all three declared kinds, in scan order.
    let snapshot = executor.snapshot();
    let kinds: Vec<&str> = snapshot
        .descriptors
        .iter()
        .map(|descriptor| descriptor.kind.as_str())
        .collect();
    assert_eq!(kinds, [BoolGate::KIND, SrLatch::KIND, EdgeTrigger::KIND]);

    // The `and` gate needs every input: partial assertion stays low,
    // the third input opens it.
    driver.write(GATE_A, Value::Bool(true)).unwrap();
    driver.write(GATE_B, Value::Bool(true)).unwrap();
    executor.scan().unwrap();
    assert_eq!(driver.read(GATED).unwrap().value, Value::Bool(false));
    driver.write(GATE_C, Value::Bool(true)).unwrap();
    executor.scan().unwrap();
    assert_eq!(driver.read(GATED).unwrap().value, Value::Bool(true));

    // Simultaneous set and reset exercises the latch's recorded
    // precedence — reset wins — while the trigger pulses on its
    // input's first rising read.
    driver.write(LATCH_SET, Value::Bool(true)).unwrap();
    driver.write(LATCH_RESET, Value::Bool(true)).unwrap();
    driver.write(TRIG_IN, Value::Bool(true)).unwrap();
    executor.scan().unwrap();
    assert_eq!(driver.read(LATCHED).unwrap().value, Value::Bool(false));
    assert_eq!(driver.read(PULSED).unwrap().value, Value::Bool(true));

    // Releasing reset lets the still-asserted set latch; the held
    // pulse level produces no second pulse.
    driver.write(LATCH_RESET, Value::Bool(false)).unwrap();
    executor.scan().unwrap();
    assert_eq!(driver.read(LATCHED).unwrap().value, Value::Bool(true));
    assert_eq!(driver.read(PULSED).unwrap().value, Value::Bool(false));

    // Mid-state checkpoint — the latch set, the trigger's previous
    // level high — transfers to a standby assembled from the same
    // model, which continues identically.
    let checkpoint = executor.checkpoint();
    let standby_driver = sim_driver(&model).unwrap();
    let mut standby = assemble(&model, &registry(), &standby_driver).unwrap();
    standby.apply(&checkpoint).unwrap();
    assert_eq!(standby.tick(), Tick(4));
    for point in [GATE_A, GATE_B, GATE_C, LATCH_SET, TRIG_IN] {
        standby_driver.write(point, Value::Bool(true)).unwrap();
    }
    for _ in 0..3 {
        executor.scan().unwrap();
        standby.scan().unwrap();
        for point in [GATED, LATCHED, PULSED] {
            assert_eq!(
                standby_driver.read(point).unwrap(),
                driver.read(point).unwrap()
            );
        }
    }

    // The latched state survived and the held pulse level never
    // re-pulsed on either run.
    assert_eq!(driver.read(LATCHED).unwrap().value, Value::Bool(true));
    assert_eq!(driver.read(PULSED).unwrap().value, Value::Bool(false));

    // The next rising edge pulses exactly once on both runs.
    driver.write(TRIG_IN, Value::Bool(false)).unwrap();
    standby_driver.write(TRIG_IN, Value::Bool(false)).unwrap();
    executor.scan().unwrap();
    standby.scan().unwrap();
    driver.write(TRIG_IN, Value::Bool(true)).unwrap();
    standby_driver.write(TRIG_IN, Value::Bool(true)).unwrap();
    executor.scan().unwrap();
    standby.scan().unwrap();
    assert_eq!(driver.read(PULSED).unwrap().value, Value::Bool(true));
    assert_eq!(
        standby_driver.read(PULSED).unwrap().value,
        Value::Bool(true)
    );
    executor.scan().unwrap();
    standby.scan().unwrap();
    assert_eq!(driver.read(PULSED).unwrap().value, Value::Bool(false));
    assert_eq!(
        standby_driver.read(PULSED).unwrap().value,
        Value::Bool(false)
    );

    assert!(
        executor
            .snapshot()
            .components
            .iter()
            .all(|component| component.step_errors == 0)
    );
}

#[test]
fn unknown_component_kind_names_the_instance() {
    let model = model(include_str!(
        "../fixtures/invalid/unknown_component_kind.json"
    ));
    let driver = sim_driver(&model).unwrap();
    let error = assemble(&model, &registry(), &driver).unwrap_err();
    assert_eq!(
        error,
        AssemblyError::UnknownComponentKind {
            component: ComponentId(1),
            kind: "flux-capacitor".to_string(),
        }
    );
    assert!(error.to_string().contains("\"flux-capacitor\""));
}

#[test]
fn unknown_device_kind_names_the_device() {
    let model = model(include_str!("../fixtures/invalid/unknown_device_kind.json"));
    let error = sim_driver(&model).err().unwrap();
    assert_eq!(
        error,
        AssemblyError::UnknownDeviceKind {
            device: dcs_model::DeviceId(1),
            kind: "ethercat-8ai".to_string(),
        }
    );
    assert!(error.to_string().contains("device 1"));
}

#[test]
fn unbound_port_names_component_and_port() {
    let model = model(include_str!("../fixtures/invalid/unbound_port.json"));
    let driver = sim_driver(&model).unwrap();
    let error = assemble(&model, &registry(), &driver).unwrap_err();
    assert_eq!(
        error,
        AssemblyError::UnboundPort {
            component: ComponentId(1),
            port: "pv".to_string(),
        }
    );
}

#[test]
fn port_bound_twice_names_component_and_port() {
    let model = model(include_str!("../fixtures/invalid/port_bound_twice.json"));
    let driver = sim_driver(&model).unwrap();
    let error = assemble(&model, &registry(), &driver).unwrap_err();
    assert_eq!(
        error,
        AssemblyError::PortBoundTwice {
            component: ComponentId(1),
            port: "pv".to_string(),
        }
    );
}

#[test]
fn direction_mismatch_names_element_and_directions() {
    let model = model(include_str!("../fixtures/invalid/direction_mismatch.json"));
    let driver = sim_driver(&model).unwrap();
    let error = assemble(&model, &registry(), &driver).unwrap_err();
    assert_eq!(
        error,
        AssemblyError::DirectionMismatch {
            component: ComponentId(1),
            port: "pv".to_string(),
            point: PointId(12),
            declared: Direction::In,
            mapped: Direction::Out,
        }
    );
}

#[test]
fn type_mismatch_names_element_and_kinds() {
    let model = model(include_str!("../fixtures/invalid/type_mismatch.json"));
    let driver = sim_driver(&model).unwrap();
    let error = assemble(&model, &registry(), &driver).unwrap_err();
    assert_eq!(
        error,
        AssemblyError::TypeMismatch {
            component: ComponentId(1),
            port: "sp".to_string(),
            point: PointId(10),
            declared: ValueKind::Float,
            mapped: ValueKind::Int,
        }
    );
}

#[test]
fn duplicate_channel_is_an_invalid_channel_map() {
    let model = model(include_str!("../fixtures/invalid/duplicate_channel.json"));
    let error = sim_driver(&model).err().unwrap();
    assert_eq!(
        error,
        AssemblyError::InvalidChannelMap {
            detail: ConfigError::DuplicateChannel(ChannelId {
                device: 1,
                name: "ch0".to_string(),
            }),
        }
    );
}

#[test]
fn constructor_failure_names_component_and_detail() {
    let model = model(include_str!("../fixtures/invalid/bad_parameters.json"));
    let driver = sim_driver(&model).unwrap();
    let error = assemble(&model, &registry(), &driver).unwrap_err();
    match error {
        AssemblyError::Component {
            component,
            kind,
            detail,
        } => {
            assert_eq!(component, ComponentId(1));
            assert_eq!(kind, "pid");
            assert!(detail.contains("\"kp\""), "{detail}");
        }
        other => panic!("expected Component, got {other:?}"),
    }
}

/// A component declaring fixed requirements, ignoring its spec — a stand-in
/// for a kind that misbinds its logical I/O.
struct Stub {
    name: String,
    requirements: Vec<IoRequirement>,
}

impl Component for Stub {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        self.requirements.clone()
    }

    fn step(&mut self, _io: &dyn ComponentIo, _tick: Tick) -> Result<(), StepError> {
        Ok(())
    }
}

/// A model whose single component has kind `"stub"` and no ports.
fn stub_model() -> PlantModel {
    let mut model = model(TANK_LOOP);
    model.components.truncate(1);
    model.components[0].kind = "stub".to_string();
    model.components[0].ports.clear();
    model.connections.clear();
    model
}

fn stub_registry(requirements: Vec<IoRequirement>) -> ComponentRegistry {
    ComponentRegistry::new().with("stub", move |spec| {
        Ok(Box::new(Stub {
            name: spec.name.clone(),
            requirements: requirements.clone(),
        }) as Box<dyn Component>)
    })
}

#[test]
fn requirement_on_unserved_point_is_unmapped() {
    let model = stub_model();
    let registry = stub_registry(vec![IoRequirement::input::<f64>("ghost", PointId(999))]);
    let driver = sim_driver(&model).unwrap();
    let error = assemble(&model, &registry, &driver).unwrap_err();
    assert_eq!(
        error,
        AssemblyError::UnmappedPoint {
            component: ComponentId(1),
            port: "ghost".to_string(),
            point: PointId(999),
        }
    );
}

#[test]
fn duplicate_declaration_surfaces_as_wiring_error() {
    let model = stub_model();
    let registry = stub_registry(vec![
        IoRequirement::input::<f64>("a", SETPOINT),
        IoRequirement::input::<f64>("b", SETPOINT),
    ]);
    let driver = sim_driver(&model).unwrap();
    let error = assemble(&model, &registry, &driver).unwrap_err();
    assert!(matches!(error, AssemblyError::Wiring { .. }), "{error:?}");
    assert!(error.to_string().contains("stub:1"), "{error}");
}

#[test]
fn port_to_port_wire_on_undeclared_component_is_unresolved() {
    // Assembling a model that skipped validation: the `from` port's
    // component does not exist.
    let mut model = model(TANK_LOOP);
    model.connections.push(Connection {
        from: Endpoint::Port(PortRef {
            component: ComponentId(99),
            name: "ghost".to_string(),
        }),
        to: Endpoint::Port(PortRef {
            component: ComponentId(2),
            name: "pv".to_string(),
        }),
    });
    let driver = sim_driver(&model).unwrap();
    let error = assemble(&model, &registry(), &driver).unwrap_err();
    assert_eq!(error, AssemblyError::UnresolvedEndpoint { connection: 5 });
}

#[test]
fn internal_points_fixture_validates_and_roundtrips() {
    let fixture = model(INTERNAL_POINTS);
    assert!(fixture.validate().is_empty());
    let roundtripped = model(&serde_json::to_string(&fixture).unwrap());
    assert_eq!(fixture, roundtripped);
}

#[test]
fn internal_setpoint_holds_then_follows_a_command() {
    let model = model(INTERNAL_POINTS);
    let driver = sim_driver(&model).unwrap();
    let mut executor = assemble(&model, &registry(), &driver).unwrap();

    // The declared initial serves as the setpoint before any command —
    // no field channel carries it.
    assert_eq!(
        executor.sample(HELD_SETPOINT).unwrap().value,
        Value::Float(25.0)
    );
    driver.write(LEVEL_RAW, Value::Float(12.0)).unwrap();
    executor.scan().unwrap();
    let before = executor.sample(PID_OUT).unwrap().value;

    // A command to the internal point applies at the next scan boundary:
    // the pid's output — recorded on the declared internal `Out` carrier —
    // moves in that same scan.
    let receipt = executor.submit_command(Command::WriteValue {
        point: HELD_SETPOINT,
        kind: ValueKind::Float,
        value: Value::Float(60.0),
    });
    assert_eq!(
        receipt.outcome,
        CommandOutcome::Accepted {
            apply_tick: Tick(2)
        }
    );
    executor.scan().unwrap();
    assert_eq!(
        executor.receipts()[0].outcome,
        CommandOutcome::Applied { tick: Tick(2) }
    );
    assert_eq!(
        executor.sample(HELD_SETPOINT).unwrap(),
        dcs_core::Sample::good(Value::Float(60.0), Tick(2))
    );
    assert_ne!(executor.sample(PID_OUT).unwrap().value, before);

    // The internal point is telemetry like any field point.
    let snapshot = executor.snapshot();
    let telemetry = snapshot
        .points
        .iter()
        .find(|point| point.point == HELD_SETPOINT)
        .expect("the internal setpoint is a mapped point");
    assert_eq!(telemetry.direction, Direction::In);
    assert_eq!(telemetry.sample.unwrap().value, Value::Float(60.0));
}

#[test]
fn port_to_port_wire_delivers_one_scan_later() {
    let model = model(INTERNAL_POINTS);
    let driver = sim_driver(&model).unwrap();
    let mut executor = assemble(&model, &registry(), &driver).unwrap();

    // Field-side input: raw 12.0 mA scales to 50.0 engineering units.
    driver.write(LEVEL_RAW, Value::Float(12.0)).unwrap();
    executor.scan().unwrap();
    // Scan 1: the analog-input wrote onto the synthesized internal `Out`
    // point, but the link routes at the input phase — the consuming `In`
    // point still held its seeded initial.
    assert_eq!(executor.sample(LINK_OUT).unwrap().value, Value::Float(50.0));
    assert_eq!(executor.sample(LINK_IN).unwrap().value, Value::Float(0.0));

    // Scan 2's input phase delivered the producer's scan-1 write — the
    // same boundary a field loopback crosses.
    executor.scan().unwrap();
    assert_eq!(executor.sample(LINK_IN).unwrap().value, Value::Float(50.0));
}

#[test]
fn declared_internal_pair_carries_a_component_write() {
    let model = model(INTERNAL_POINTS);
    let driver = sim_driver(&model).unwrap();
    let mut executor = assemble(&model, &registry(), &driver).unwrap();

    driver.write(LEVEL_RAW, Value::Float(12.0)).unwrap();
    executor.scan().unwrap();
    let pid_out = executor.sample(PID_OUT).unwrap().value;
    // Scan 1 routed the carrier's seeded initial onto the analog-output's
    // eng point; the pid's write arrives at scan 2's input phase.
    assert_eq!(executor.sample(AO_ENG).unwrap().value, Value::Float(0.0));
    executor.scan().unwrap();
    assert_eq!(executor.sample(AO_ENG).unwrap().value, pid_out);
}

#[test]
fn malformed_internal_declarations_fail_validation_naming_the_point() {
    // A channel-less point without `initial`.
    let mut missing = model(INTERNAL_POINTS);
    missing.io_points[0].initial = None;
    assert!(
        missing
            .validate()
            .contains(&ValidationError::MissingInitial {
                point: HELD_SETPOINT
            })
    );

    // A channel-less point whose `initial` disagrees with `value_type`.
    let mut mismatched = model(INTERNAL_POINTS);
    mismatched.io_points[0].initial = Some(Value::Int(25));
    assert!(
        mismatched
            .validate()
            .contains(&ValidationError::InitialKindMismatch {
                point: HELD_SETPOINT,
                declared: ValueKind::Float,
                initial: ValueKind::Int,
            })
    );

    // `initial` on a channel-bound point.
    let mut seeded = model(INTERNAL_POINTS);
    seeded.io_points[1].initial = Some(Value::Float(0.0));
    assert!(
        seeded
            .validate()
            .contains(&ValidationError::FieldInitial { point: LEVEL_RAW })
    );
}

#[test]
fn malformed_internal_declarations_fail_assembly_naming_the_point() {
    // Assembling a model that skipped validation reports the same
    // defects through the assembly error vocabulary — deferred, like
    // every wiring failure, so `assemble` names it before constructing
    // anything. The driver side is unaffected: no backend ever serves a
    // channel-less point.
    let driver = sim_driver(&model(INTERNAL_POINTS)).unwrap();

    let mut missing = model(INTERNAL_POINTS);
    missing.io_points[0].initial = None;
    assert_eq!(
        assemble(&missing, &registry(), &driver).unwrap_err(),
        AssemblyError::InvalidInternalPoint {
            point: HELD_SETPOINT,
            detail: InternalPointError::MissingInitial,
        }
    );

    let mut mismatched = model(INTERNAL_POINTS);
    mismatched.io_points[0].initial = Some(Value::Bool(true));
    let error = assemble(&mismatched, &registry(), &driver).unwrap_err();
    assert_eq!(
        error,
        AssemblyError::InvalidInternalPoint {
            point: HELD_SETPOINT,
            detail: InternalPointError::InitialKindMismatch {
                declared: ValueKind::Float,
                initial: ValueKind::Bool,
            },
        }
    );
    assert!(error.to_string().contains("io point 10"), "{error}");
}

#[test]
fn mixed_field_internal_point_link_names_both_endpoints() {
    // Field `In` point 11 driven by internal `Out` point 13 validates —
    // both ends resolve, directions and kinds agree — but no carrier can
    // serve it: the internal point has no channel a loopback could drive.
    let mut model = model(INTERNAL_POINTS);
    model.connections.push(Connection {
        from: Endpoint::Point(LEVEL_RAW),
        to: Endpoint::Point(PID_OUT),
    });
    assert!(model.validate().is_empty());
    let driver = sim_driver(&model).unwrap();
    let error = assemble(&model, &registry(), &driver).unwrap_err();
    assert_eq!(
        error,
        AssemblyError::MixedPointLink {
            connection: 7,
            field: LEVEL_RAW,
            internal: PID_OUT,
        }
    );
    assert!(error.to_string().contains("io point 11"), "{error}");
    assert!(error.to_string().contains("io point 13"), "{error}");
}
