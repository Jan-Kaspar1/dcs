//! The multi-kind composition: a realistic plant assembled entirely
//! through [`PlantBuilder`]'s typed spec handles — the typed-seam proof
//! the tank loop's two-component case only began.
//!
//! Recorded choice for the issue: the composition lives in this test
//! target rather than a checked-in builder example. The run's behavior
//! is asserted per kind below, and determinism is proven by rebuilding
//! and rerunning the identical scenario — an emitted [`PlantModel`]
//! equal across builds and equal across runs needs no fixture file.
//!
//! The plant is a level-regulated dosing loop over two declared device
//! kinds:
//!
//! - a `sim` device carries the operator `setpoint` channel and the
//!   `valve`/`alarm`/`unack` outputs;
//! - a `sim-scripted` device replays the `level-raw` measurement and the
//!   `trip` high-high contact, and records the `dose-rate` output the
//!   sequencer drives.
//!
//! Five spec'd kinds compose the control: `analog-input` scales the raw
//! level, `pid` regulates it toward the setpoint, `interlock` gates the
//! PID's manipulated value behind a permissive and the scripted trip,
//! `latching-alarm` watches the raw level with the operator `ack` held
//! on a writable internal point, and `sequencer` walks a two-step dose
//! phase once `run` is commanded. Every connection goes through the
//! spec's typed handles, so port existence, direction, and value kind
//! are compile-time-checked — the compile-fail evidence is the crate's
//! documented `compile_fail` doctests.
//!
//! ## The scripted scenario
//!
//! Scans read the scripted driver's previous tick: each iteration scans,
//! then steps the driver. `level-raw` holds 8.0 mA (engineering 25.0)
//! until driver tick 6, rises to 18.0 mA (engineering 87.5 — past the
//! alarm's high limit of 16.0 mA) through tick 11, and returns to 8.0
//! mA from tick 12; `trip` asserts at tick 9 and clears at tick 14 —
//! scans 10–14. Operator commands land at the next scan's head: the
//! setpoint write applies at scan 1, `run` at scan 3, `ack` at scan 15
//! and its release at scan 16.
//!
//! The documented behavior, per kind:
//!
//! - **Regulation:** the PID's output ramps from the saturation at
//!   scan 1 — 20.0, 13.75, 15.0, 16.25, 17.5, 18.75, 20.0 while the
//!   level sits below the 50.0 setpoint — and the interlocked `valve`
//!   follows it one scan later; when the level overshoots the setpoint
//!   the output drops to `out_min` 4.0.
//! - **Tripped interlock:** scans 10–14 drive the configured
//!   `safe_value` 6.0 — a value the PID never produces — and raise
//!   `tripped`; the clear at scan 15 auto-resets to pass-through.
//! - **Latched-and-acked alarm:** the raw level reaching 18.0 trips the
//!   high limit at scan 7; `alarm` holds through scan 12 and clears
//!   when the level falls below `high - hysteresis`, while
//!   `unacknowledged` latches until the commanded `ack` at scan 15.
//! - **Sequence advance:** with `run` applied at scan 3 the table's
//!   2-tick first step completes at scan 4, `step` reports 2 and `out`
//!   drives 20.0 from scan 5, and `done` asserts when the 3-tick second
//!   step completes at scan 7; the scripted device records every
//!   `dose-rate` write.

use dcs_assembly::{DriverRegistry, FanoutDriver, assemble, resolve_drivers};
use dcs_build::specs::{AnalogInputSpec, InterlockSpec, LatchingAlarmSpec, PidSpec, SequencerSpec};
use dcs_build::{DeviceId, Direction, PlantBuilder, PointId, SignalId, Value, parameters};
use dcs_core::{Command, CommandOutcome, CommandReceipt, Tick, ValueKind};
use dcs_model::PlantModel;
use dcs_sim::{RecordedWrite, ScriptedDriver};

// Field inputs.
const SETPOINT: PointId = PointId(10);
const LEVEL_RAW: PointId = PointId(11);
const TRIP: PointId = PointId(12);
// Held operator values — the writable internal points.
const PERMISSIVE: PointId = PointId(13);
const ACK: PointId = PointId(14);
const SEQ_RUN: PointId = PointId(15);
const SEQ_RESET: PointId = PointId(16);
// Field outputs.
const VALVE: PointId = PointId(20);
const ALARM: PointId = PointId(21);
const UNACK: PointId = PointId(22);
const DOSE_RATE: PointId = PointId(23);
// Internal observation outputs.
const TRIPPED: PointId = PointId(31);
const SEQ_STEP: PointId = PointId(32);
const SEQ_DONE: PointId = PointId(33);

const SCRIPTED: DeviceId = DeviceId(2);
/// The scripted run length — covers every documented event above plus
/// settling scans after them.
const SCANS: u64 = 20;
/// The interlock's safe value, chosen distinct from every value the PID
/// produces so a tripped `valve` is unambiguous.
const SAFE_VALUE: f64 = 6.0;

/// The plant, composed: every component through its typed spec, every
/// connection through typed handles.
fn composed_plant() -> PlantBuilder {
    let mut plant = PlantBuilder::new();

    // The local simulated device: the operator setpoint and the
    // loop's field outputs.
    let sim = plant.device("sim").id;
    let setpoint_ch = plant.channel::<f64>(sim, "setpoint", Direction::In);
    let valve_ch = plant.channel::<f64>(sim, "valve", Direction::Out);
    let alarm_ch = plant.channel::<bool>(sim, "alarm", Direction::Out);
    let unack_ch = plant.channel::<bool>(sim, "unack", Direction::Out);

    // The scripted device: the measurement and trip scripts replay in
    // driver-tick order; `dose-rate` records what the sequencer drives.
    let scripted = {
        let device = plant.device("sim-scripted");
        device.parameters.insert(
            "script".to_string(),
            serde_json::json!({
                "level-raw": [
                    { "tick": 0, "value": 8.0 },
                    { "tick": 6, "value": 18.0 },
                    { "tick": 12, "value": 8.0 }
                ],
                "trip": [
                    { "tick": 9, "value": true },
                    { "tick": 14, "value": false }
                ]
            }),
        );
        device.id
    };
    let level_raw_ch = plant.channel::<f64>(scripted, "level-raw", Direction::In);
    let trip_ch = plant.channel::<bool>(scripted, "trip", Direction::In);
    let dose_rate_ch = plant.channel::<f64>(scripted, "dose-rate", Direction::Out);

    // Logical I/O: field points bound to channels, writable internal
    // points holding the operator's permissive, ack, and run/reset.
    let sp = plant.field_input::<f64>(SETPOINT, setpoint_ch, true);
    let level = plant.field_input::<f64>(LEVEL_RAW, level_raw_ch, false);
    let trip = plant.field_input::<bool>(TRIP, trip_ch, false);
    let permissive = plant.internal_input::<bool>(PERMISSIVE, true, true);
    let ack = plant.internal_input::<bool>(ACK, false, true);
    let run = plant.internal_input::<bool>(SEQ_RUN, false, true);
    let reset = plant.internal_input::<bool>(SEQ_RESET, false, true);

    let valve = plant.field_output::<f64>(VALVE, valve_ch);
    let alarm = plant.field_output::<bool>(ALARM, alarm_ch);
    let unack = plant.field_output::<bool>(UNACK, unack_ch);
    let dose = plant.field_output::<f64>(DOSE_RATE, dose_rate_ch);

    let tripped = plant.internal_output::<bool>(TRIPPED, false);
    let step = plant.internal_output::<i64>(SEQ_STEP, 0);
    let done = plant.internal_output::<bool>(SEQ_DONE, false);

    // The monitoring surface.
    plant
        .signal(SignalId(100), "level-setpoint", sp)
        .unit("m")
        .description("Tank level setpoint");
    plant
        .signal(SignalId(101), "level-raw", level)
        .unit("mA")
        .description("Tank level raw measurement, replayed by the scripted device");
    plant
        .signal(SignalId(102), "high-high-trip", trip)
        .description("High-high contact, replayed by the scripted device");
    plant
        .signal(SignalId(103), "valve-command", valve)
        .unit("%")
        .description("Interlocked inlet valve command");
    plant
        .signal(SignalId(104), "level-alarm", alarm)
        .description("Raw-level limit state");
    plant
        .signal(SignalId(105), "level-alarm-unacknowledged", unack)
        .description("Latched until the operator acks");
    plant
        .signal(SignalId(106), "dose-rate", dose)
        .unit("%")
        .description("Rate the active sequence step drives");
    plant
        .signal(SignalId(107), "dose-step", step)
        .description("Active dose step, 1-based");
    plant
        .signal(SignalId(108), "dose-done", done)
        .description("Dose sequence hold-at-end flag");

    // The components, each through its kind's spec.
    let ai = plant.add(AnalogInputSpec::<f64>::new(parameters([
        ("raw_min", Value::Float(4.0)),
        ("raw_max", Value::Float(20.0)),
        ("eng_min", Value::Float(0.0)),
        ("eng_max", Value::Float(100.0)),
    ])));
    let pid = plant.add(PidSpec::new(parameters([
        ("kp", Value::Float(0.5)),
        ("ki", Value::Float(0.5)),
        ("kd", Value::Float(0.0)),
        ("dt", Value::Float(0.1)),
        ("out_min", Value::Float(4.0)),
        ("out_max", Value::Float(20.0)),
    ])));
    let ilk = plant.add(InterlockSpec::new(
        parameters([("safe_value", Value::Float(SAFE_VALUE))]),
        1,
    ));
    let lal = plant.add(LatchingAlarmSpec::new(
        parameters([
            ("low_limit", Value::Float(6.0)),
            ("high_limit", Value::Float(16.0)),
            ("hysteresis", Value::Float(1.0)),
            ("priority", Value::Int(1)),
            ("class", Value::Int(2)),
            ("response_ticks", Value::Int(30)),
        ]),
        dcs_build::Rationalization {
            consequence: "c".to_string(),
            required_action: "a".to_string(),
            reference: "r".to_string(),
        },
    ));
    let seq = plant.add(SequencerSpec::new(parameters([
        ("step_count", Value::Int(2)),
        ("step_1_ticks", Value::Int(2)),
        ("step_1_out", Value::Float(10.0)),
        ("step_2_ticks", Value::Int(3)),
        ("step_2_out", Value::Float(20.0)),
    ])));

    // The wiring. Port-to-port links cross the one-scan boundary; point
    // bindings are same-scan.
    plant.connect(sp, pid.sp);
    plant.connect(level, ai.raw);
    plant.connect(&ai.out, pid.pv);
    plant.connect(&pid.out, &ilk.input);
    plant.connect(permissive, &ilk.permissive);
    plant.connect(trip, ilk.trip(1));
    plant.connect(&ilk.out, valve);
    plant.connect(&ilk.tripped, tripped);
    plant.connect(level, lal.input);
    plant.connect(ack, lal.ack);
    plant.connect(&lal.alarm, alarm);
    plant.connect(&lal.unacknowledged, unack);
    plant.connect(run, seq.run);
    plant.connect(reset, seq.reset);
    plant.connect(&seq.out, dose);
    plant.connect(&seq.step, step);
    plant.connect(&seq.done, done);

    plant
}

/// Emits the document, reloads it through `dcs-model`'s validating
/// loader — the serde roundtrip — and returns the reloaded model.
fn emit_and_reload() -> PlantModel {
    let model = composed_plant().build().unwrap();
    let json = serde_json::to_string_pretty(&model).unwrap();
    let reloaded = PlantModel::load(&json).unwrap();
    assert_eq!(reloaded, model);
    reloaded
}

/// Builds the driver side of `model` through the standard
/// [`DriverRegistry`]. The caller owns the returned driver and assembles
/// the executor against it — the executor borrows the driver.
fn build_driver(model: &PlantModel) -> FanoutDriver {
    resolve_drivers(model, &DriverRegistry::standard())
        .unwrap()
        .build()
        .unwrap()
}

/// One scan's observable record: the image value each watched point
/// carried after the scan's write phase.
#[derive(Debug, Default, PartialEq)]
struct Trace {
    valve: Vec<Value>,
    tripped: Vec<Value>,
    alarm: Vec<Value>,
    unack: Vec<Value>,
    seq_step: Vec<Value>,
    seq_done: Vec<Value>,
}

/// What the scripted run produced: the per-scan trace, the scripted
/// device's recorded `dose-rate` writes, the command receipts, and the
/// final serialized snapshot.
#[derive(Debug, PartialEq)]
struct Run {
    trace: Trace,
    dose_writes: Vec<RecordedWrite>,
    receipts: Vec<CommandReceipt>,
    snapshot: String,
}

/// Writes `value` to `point` through the operator command path.
fn write(executor: &mut dcs_runtime::Executor<'_>, point: PointId, kind: ValueKind, value: Value) {
    let receipt = executor.submit_command(Command::WriteValue { point, kind, value });
    assert!(
        matches!(receipt.outcome, CommandOutcome::Accepted { .. }),
        "write to {point:?} rejected: {receipt:?}"
    );
}

/// Builds the plant fresh, assembles it, and runs the documented
/// scenario: `SCANS` scans, each followed by a driver step, with the
/// operator commands landing at their scripted boundaries.
fn run() -> Run {
    let model = emit_and_reload();
    let driver = build_driver(&model);
    let mut executor = assemble(&model, &dcs_controller::registry(), &driver).unwrap();

    // The setpoint command applies at scan 1's head, before the first
    // input read.
    write(
        &mut executor,
        SETPOINT,
        ValueKind::Float,
        Value::Float(50.0),
    );

    let mut trace = Trace::default();
    let mut observe = |executor: &dcs_runtime::Executor<'_>| {
        let sample = |point| executor.sample(point).unwrap().value;
        trace.valve.push(sample(VALVE));
        trace.tripped.push(sample(TRIPPED));
        trace.alarm.push(sample(ALARM));
        trace.unack.push(sample(UNACK));
        trace.seq_step.push(sample(SEQ_STEP));
        trace.seq_done.push(sample(SEQ_DONE));
    };

    for scan in 1..=SCANS {
        executor.scan();
        driver.step(0.1).unwrap();
        observe(&executor);
        match scan {
            // `run` applies at scan 3's head.
            2 => write(&mut executor, SEQ_RUN, ValueKind::Bool, Value::Bool(true)),
            // The ack pulse: `true` at scan 15, released at scan 16.
            14 => write(&mut executor, ACK, ValueKind::Bool, Value::Bool(true)),
            15 => write(&mut executor, ACK, ValueKind::Bool, Value::Bool(false)),
            _ => {}
        }
    }

    assert!(
        executor
            .snapshot()
            .components
            .iter()
            .all(|component| component.step_errors == 0),
        "a component failed to step: {:?}",
        executor.snapshot().components
    );

    Run {
        trace,
        dose_writes: driver.inspect::<ScriptedDriver>(SCRIPTED).unwrap().writes(),
        receipts: executor.receipts().to_vec(),
        snapshot: serde_json::to_string(&executor.snapshot()).unwrap(),
    }
}

#[test]
fn composed_plant_loads_validates_and_assembles() {
    let model = emit_and_reload();
    assert_eq!(model.version, dcs_model::MODEL_VERSION);
    assert_eq!(
        model
            .components
            .iter()
            .map(|component| component.kind.as_str())
            .collect::<Vec<_>>(),
        [
            AnalogInputSpec::<f64>::KIND,
            PidSpec::KIND,
            InterlockSpec::KIND,
            LatchingAlarmSpec::KIND,
            SequencerSpec::KIND,
        ]
    );
    assert_eq!(
        model
            .devices
            .iter()
            .map(|device| device.kind.as_str())
            .collect::<Vec<_>>(),
        ["sim", "sim-scripted"]
    );

    // The emitted document assembles through the standard registries.
    let driver = build_driver(&model);
    assemble(&model, &dcs_controller::registry(), &driver).unwrap();
}

#[test]
fn identical_builds_emit_identical_documents() {
    let first = composed_plant().build().unwrap();
    let second = composed_plant().build().unwrap();
    assert_eq!(first, second);
    assert_eq!(
        serde_json::to_string_pretty(&first).unwrap(),
        serde_json::to_string_pretty(&second).unwrap()
    );
}

#[test]
fn scripted_run_shows_the_documented_per_kind_behavior() {
    let run = run();
    let float = |value: f64| Value::Float(value);
    let bool_ = |value: bool| Value::Bool(value);
    let int = |value: i64| Value::Int(value);

    // Regulation plus gating on one channel: while the interlock passes
    // through, `valve` carries the PID's output one scan later — the
    // ramp out of the initial saturation while the level sits below the
    // setpoint (20.0, 13.75, 15.0, 16.25, 17.5, 18.75, 20.0), the drop
    // to `out_min` 4.0 when the level overshoots — and the five tripped
    // scans drive the safe value instead; the clear auto-resets to the
    // saturated demand.
    assert_eq!(
        run.trace.valve,
        [
            0.0, 20.0, 13.75, 15.0, 16.25, 17.5, 18.75, 20.0, 4.0, SAFE_VALUE, SAFE_VALUE,
            SAFE_VALUE, SAFE_VALUE, SAFE_VALUE, 20.0, 20.0, 20.0, 20.0, 20.0, 20.0,
        ]
        .map(float)
    );
    assert_eq!(
        run.trace.tripped,
        [
            false, false, false, false, false, false, false, false, false, true, true, true, true,
            true, false, false, false, false, false, false,
        ]
        .map(bool_)
    );

    // The latching alarm: tripped scans 7–12, latched scans 7–14,
    // cleared by the commanded ack at scan 15.
    assert_eq!(
        run.trace.alarm,
        [
            false, false, false, false, false, false, true, true, true, true, true, true, false,
            false, false, false, false, false, false, false,
        ]
        .map(bool_)
    );
    assert_eq!(
        run.trace.unack,
        [
            false, false, false, false, false, false, true, true, true, true, true, true, true,
            true, false, false, false, false, false, false,
        ]
        .map(bool_)
    );

    // The sequencer: step 1 through scan 4, step 2 from scan 5, `done`
    // holding from scan 7.
    assert_eq!(
        run.trace.seq_step,
        [1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2,].map(int)
    );
    assert_eq!(
        run.trace.seq_done,
        [
            false, false, false, false, false, false, true, true, true, true, true, true, true,
            true, true, true, true, true, true, true,
        ]
        .map(bool_)
    );

    // Every scan wrote the sequencer's `dose-rate` to the scripted
    // device: 10.0 while step 1 stands, 20.0 from scan 5 — recorded at
    // the driver tick the scan ran against.
    assert_eq!(run.dose_writes.len(), SCANS as usize);
    for (index, write) in run.dose_writes.iter().enumerate() {
        assert_eq!(write.point, DOSE_RATE);
        assert_eq!(write.tick, Tick(index as u64));
        assert_eq!(write.value, float(if index < 4 { 10.0 } else { 20.0 }));
    }

    // All four operator commands applied at their documented ticks.
    assert_eq!(
        run.receipts
            .iter()
            .map(|receipt| receipt.outcome.clone())
            .collect::<Vec<_>>(),
        [1, 3, 15, 16]
            .into_iter()
            .map(|tick| CommandOutcome::Applied { tick: Tick(tick) })
            .collect::<Vec<_>>()
    );
}

#[test]
fn repeated_builds_and_runs_are_deterministic() {
    assert_eq!(run(), run());
}
