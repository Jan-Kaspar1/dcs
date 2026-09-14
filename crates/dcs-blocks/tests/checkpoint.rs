//! Integration test for deterministic checkpoint/restore: a `Pid` closed
//! around a simulated first-order lag, checkpointed mid-run and restored
//! into a fresh executor and driver, must produce output samples
//! identical to the uninterrupted run — the bumpless-switchover
//! acceptance criterion.

use dcs_blocks::{OverrideSelect, Pid, PidConfig};
use dcs_core::{
    Direction, IoDriver, IoError, PointId, Sample, StateError, StateMap, Tick, Value, ValueKind,
};
use dcs_runtime::{Checkpoint, Component, Executor, PointMap, RestoreError};
use dcs_sim::{ChannelId, ChannelMap, FirstOrderLag, PointBinding, ProcessElement, SimDriver};
use std::collections::HashMap;
use std::sync::Mutex;

const SP: PointId = PointId(11);
const PV: PointId = PointId(10);
const OUT: PointId = PointId(20);

/// Simulated time each scan advances the lag — also the PID's `dt`.
const DT: f64 = 0.1;

/// The checkpoint is taken at tick N and the restored run is compared
/// over the following M ticks.
const CHECKPOINT_AT: u64 = 30;
const CONTINUED: u64 = 40;

fn sim_point(point: PointId, direction: Direction) -> PointBinding {
    PointBinding {
        point,
        channel: ChannelId {
            device: 1,
            name: format!("ch{}", point.0),
        },
        direction,
        initial: Value::Float(0.0),
    }
}

/// The simulated plant: setpoint and measurement `In` points, the
/// manipulated-variable `Out` point, and a first-order lag driving `pv`
/// from `out`.
fn channel_map() -> ChannelMap {
    ChannelMap::new()
        .with_point(sim_point(PV, Direction::In))
        .with_point(sim_point(SP, Direction::In))
        .with_point(sim_point(OUT, Direction::Out))
        .with_element(ProcessElement::FirstOrderLag(FirstOrderLag {
            input: OUT,
            output: PV,
            time_constant: 1.0,
            initial: 0.0,
        }))
}

fn point_map() -> PointMap {
    [
        (PV, Direction::In, ValueKind::Float),
        (SP, Direction::In, ValueKind::Float),
        (OUT, Direction::Out, ValueKind::Float),
    ]
    .into_iter()
    .collect()
}

fn pid(name: &str) -> Pid {
    Pid::new(
        name,
        SP,
        PV,
        OUT,
        PidConfig {
            kp: 2.0,
            ki: 1.0,
            kd: 0.5,
            dt: DT,
            out_min: 0.0,
            out_max: 15.0,
        },
    )
    .unwrap()
}

/// One loop iteration: the executor scans, the simulated process
/// advances, and the valve output the driver holds is recorded.
fn iterate(executor: &mut Executor<'_>, sim: &SimDriver, outputs: &mut Vec<Sample>) {
    executor.scan().unwrap();
    sim.step(DT);
    outputs.push(sim.read(OUT).unwrap());
}

/// The reference run: `total` uninterrupted scan/step iterations.
fn run_uninterrupted(total: u64) -> Vec<Sample> {
    let sim = SimDriver::new(channel_map()).unwrap();
    sim.write(SP, Value::Float(10.0)).unwrap();
    let mut executor = Executor::new(&sim, point_map(), vec![Box::new(pid("pid"))]).unwrap();
    let mut outputs = Vec::new();
    for _ in 0..total {
        iterate(&mut executor, &sim, &mut outputs);
    }
    outputs
}

/// The switchover run: N iterations, checkpoint, then fresh driver and
/// component instances restored from it run the remaining M.
fn run_with_restore() -> (Vec<Sample>, Checkpoint) {
    let sim = SimDriver::new(channel_map()).unwrap();
    sim.write(SP, Value::Float(10.0)).unwrap();
    let mut executor = Executor::new(&sim, point_map(), vec![Box::new(pid("pid"))]).unwrap();
    let mut outputs = Vec::new();
    for _ in 0..CHECKPOINT_AT {
        iterate(&mut executor, &sim, &mut outputs);
    }
    let checkpoint = executor.checkpoint();

    // A standby rebuilds from the same plant model — fresh driver and
    // components — and applies the checkpoint.
    let standby_sim = SimDriver::new(channel_map()).unwrap();
    let mut restored = Executor::restore(
        &standby_sim,
        point_map(),
        vec![Box::new(pid("pid"))],
        &checkpoint,
        None,
    )
    .unwrap();
    assert_eq!(restored.tick(), Tick(CHECKPOINT_AT));
    for _ in 0..CONTINUED {
        iterate(&mut restored, &standby_sim, &mut outputs);
    }
    (outputs, checkpoint)
}

#[test]
fn restored_run_produces_identical_outputs() {
    let (restored_outputs, checkpoint) = run_with_restore();
    let expected = run_uninterrupted(CHECKPOINT_AT + CONTINUED);

    // The checkpoint actually landed mid-run: the PID had accumulated
    // integral state worth transferring.
    let pid_state = &checkpoint.components["pid"];
    let Some(Value::Float(integrator)) = pid_state.get("integrator") else {
        panic!("pid state must carry a Float integrator")
    };
    assert!(integrator != 0.0);

    assert_eq!(restored_outputs.len(), (CHECKPOINT_AT + CONTINUED) as usize);
    assert_eq!(restored_outputs, expected);
}

#[test]
fn checkpoint_serde_roundtrips() {
    let (_, checkpoint) = run_with_restore();
    let json = serde_json::to_string(&checkpoint).unwrap();
    assert_eq!(
        serde_json::from_str::<Checkpoint>(&json).unwrap(),
        checkpoint
    );
}

#[test]
fn pid_integrator_continues_across_restore() {
    // Capture mid-run state out of a live executor, then restore it into
    // a freshly constructed Pid and verify the accumulated state
    // transfers directly.
    let sim = SimDriver::new(channel_map()).unwrap();
    sim.write(SP, Value::Float(10.0)).unwrap();
    let mut executor = Executor::new(&sim, point_map(), vec![Box::new(pid("pid"))]).unwrap();
    let mut outputs = Vec::new();
    for _ in 0..CHECKPOINT_AT {
        iterate(&mut executor, &sim, &mut outputs);
    }
    let state = executor.checkpoint().components["pid"].clone();

    let mut standby = pid("pid");
    standby.restore_state(&state).unwrap();

    let Some(Value::Float(integrator)) = state.get("integrator") else {
        panic!("integrator must be a Float")
    };
    assert!(integrator > 0.0, "the run must have integrated by now");
    assert_eq!(standby.integral(), integrator);
    // The whole transferred state round-trips through the component.
    assert_eq!(standby.capture_state(), state);
}

#[test]
fn incompatible_component_state_is_a_named_error() {
    let mut standby = pid("pid");
    // A state map the Pid did not produce: the integrator as an Int.
    let mut foreign = StateMap::new();
    foreign.insert("integrator", Value::Int(3));
    foreign.insert("last_output", Value::Float(1.0));
    let error = standby.restore_state(&foreign).unwrap_err();
    assert_eq!(
        error,
        StateError::IncompatibleField {
            element: "pid".to_string(),
            field: "integrator".to_string(),
            expected: ValueKind::Float,
            found: ValueKind::Int,
        }
    );
    // Required fields are missing entirely.
    assert_eq!(
        standby.restore_state(&StateMap::new()).unwrap_err(),
        StateError::MissingField {
            element: "pid".to_string(),
            field: "integrator".to_string(),
        }
    );
}

#[test]
fn mismatched_component_set_is_a_named_error() {
    let (_, checkpoint) = run_with_restore();
    let sim = SimDriver::new(channel_map()).unwrap();

    // The checkpoint's "pid" has no counterpart in the fresh executor.
    let error = Executor::restore(
        &sim,
        point_map(),
        vec![Box::new(pid("other"))],
        &checkpoint,
        None,
    )
    .unwrap_err();
    assert_eq!(
        error,
        RestoreError::UnknownComponent {
            component: "pid".to_string()
        }
    );
    assert!(error.to_string().contains("\"pid\""), "{error}");

    // A registered component the checkpoint does not describe.
    let error = Executor::restore(
        &sim,
        point_map(),
        vec![Box::new(pid("pid")), Box::new(pid("pid-2"))],
        &checkpoint,
        None,
    )
    .unwrap_err();
    assert_eq!(
        error,
        RestoreError::MissingComponent {
            component: "pid-2".to_string()
        }
    );
}

/// A minimal driver that does not implement the state-capture contract —
/// the live-hardware shape, where the standby observes the actual process
/// through its own channels rather than restoring captured field state.
struct FlatDriver {
    points: Mutex<HashMap<PointId, Sample>>,
}

impl FlatDriver {
    fn new(points: &[(PointId, Value)]) -> Self {
        Self {
            points: Mutex::new(
                points
                    .iter()
                    .map(|&(point, value)| (point, Sample::good(value, Tick::ZERO)))
                    .collect(),
            ),
        }
    }
}

impl IoDriver for FlatDriver {
    fn read(&self, point: PointId) -> Result<Sample, IoError> {
        self.points
            .lock()
            .unwrap()
            .get(&point)
            .copied()
            .ok_or(IoError::UnknownPoint(point))
    }

    fn write(&self, point: PointId, value: Value) -> Result<(), IoError> {
        let mut points = self.points.lock().unwrap();
        let stored = points.get_mut(&point).ok_or(IoError::UnknownPoint(point))?;
        if value.kind() != stored.value.kind() {
            return Err(IoError::TypeMismatch {
                point,
                expected: stored.value.kind(),
                found: value,
            });
        }
        *stored = Sample::good(value, Tick::ZERO);
        Ok(())
    }
}

#[test]
fn stateless_components_and_drivers_are_unaffected() {
    // A stateless, parameterless component (OverrideSelect) behind a
    // driver that does not capture state: the checkpoint carries an
    // empty component map and no driver section, and restore still works.
    const CONTROL: PointId = PointId(30);
    const OPERATOR: PointId = PointId(31);
    const SELECT: PointId = PointId(32);
    const FIELD: PointId = PointId(33);
    let map: PointMap = [
        (CONTROL, Direction::In, ValueKind::Float),
        (OPERATOR, Direction::In, ValueKind::Float),
        (SELECT, Direction::In, ValueKind::Bool),
        (FIELD, Direction::Out, ValueKind::Float),
    ]
    .into_iter()
    .collect();
    let block = || OverrideSelect::new("ovr", CONTROL, OPERATOR, SELECT, FIELD);

    let driver = FlatDriver::new(&[
        (CONTROL, Value::Float(0.0)),
        (OPERATOR, Value::Float(0.0)),
        (SELECT, Value::Bool(false)),
        (FIELD, Value::Float(0.0)),
    ]);
    driver.write(CONTROL, Value::Float(7.0)).unwrap();
    driver.write(OPERATOR, Value::Float(9.0)).unwrap();
    driver.write(SELECT, Value::Bool(true)).unwrap();
    let mut executor = Executor::new(&driver, map.clone(), vec![Box::new(block())]).unwrap();
    executor.scan().unwrap();
    let checkpoint = executor.checkpoint();

    assert!(checkpoint.components["ovr"].is_empty());
    assert_eq!(checkpoint.driver, None);
    assert_eq!(
        checkpoint.outputs[&FIELD],
        Sample::good(Value::Float(9.0), Tick(1))
    );

    // The standby driver observes the field itself: it holds the live
    // input values rather than restored ones.
    let standby_driver = FlatDriver::new(&[
        (CONTROL, Value::Float(7.0)),
        (OPERATOR, Value::Float(9.0)),
        (SELECT, Value::Bool(true)),
        (FIELD, Value::Float(0.0)),
    ]);
    let mut restored = Executor::restore(
        &standby_driver,
        map,
        vec![Box::new(block())],
        &checkpoint,
        None,
    )
    .unwrap();
    restored.scan().unwrap();
    assert_eq!(restored.tick(), Tick(2));
    assert_eq!(standby_driver.read(FIELD).unwrap().value, Value::Float(9.0));
}
