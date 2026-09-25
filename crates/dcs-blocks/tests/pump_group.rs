//! Integration test for `PumpGroup` under the executor: a two-pump
//! group under timed rotation whose `run_i` feedback follows `cmd_i`
//! through simulated loopbacks. A checkpoint mid-rotation — duty
//! assignment, rotation interval, accumulated run-hours, and command
//! state all in flight — restored into a fresh executor and driver must
//! produce output samples identical to the uninterrupted run, and
//! repeated runs must agree exactly.

use dcs_blocks::{GroupOutputs, PumpGroup, PumpGroupConfig, PumpIo, RotationPolicy};
use dcs_core::{Direction, IoDriver, PointId, Sample, Tick, Value, ValueKind};
use dcs_runtime::{Checkpoint, Component, Executor, PointMap};
use dcs_sim::{ChannelId, ChannelMap, Loopback, PointBinding, SimDriver};

const DEMAND: PointId = PointId(10);
const CMD_1: PointId = PointId(20);
const RUN_1: PointId = PointId(21);
const FAULT_1: PointId = PointId(22);
const AVAIL_1: PointId = PointId(23);
const CMD_2: PointId = PointId(30);
const RUN_2: PointId = PointId(31);
const FAULT_2: PointId = PointId(32);
const AVAIL_2: PointId = PointId(33);
const DUTY: PointId = PointId(40);
const STAGED: PointId = PointId(41);
const NONE_AVAILABLE: PointId = PointId(42);
const ALL_FAULTED: PointId = PointId(43);

/// Simulated time each scan advances — also the loopback delay the run
/// feedback crosses.
const DT: f64 = 0.1;

/// The checkpoint lands mid-rotation: the timed interval is part
/// elapsed, duty sits on pump 2, and the run-hours differ.
const CHECKPOINT_AT: u64 = 22;
const CONTINUED: u64 = 10;

fn sim_point(point: PointId, direction: Direction, initial: Value) -> PointBinding {
    PointBinding {
        point,
        channel: ChannelId {
            device: 1,
            name: format!("ch{}", point.0),
        },
        direction,
        initial,
    }
}

/// The simulated plant: the group's inputs as `In` points — `run_i`
/// fed by the `cmd_i` loopbacks — and its outputs as `Out` points.
fn channel_map() -> ChannelMap {
    ChannelMap::new()
        .with_point(sim_point(DEMAND, Direction::In, Value::Int(0)))
        .with_point(sim_point(CMD_1, Direction::Out, Value::Bool(false)))
        .with_point(sim_point(RUN_1, Direction::In, Value::Bool(false)))
        .with_point(sim_point(FAULT_1, Direction::In, Value::Bool(false)))
        .with_point(sim_point(AVAIL_1, Direction::In, Value::Bool(true)))
        .with_point(sim_point(CMD_2, Direction::Out, Value::Bool(false)))
        .with_point(sim_point(RUN_2, Direction::In, Value::Bool(false)))
        .with_point(sim_point(FAULT_2, Direction::In, Value::Bool(false)))
        .with_point(sim_point(AVAIL_2, Direction::In, Value::Bool(true)))
        .with_point(sim_point(DUTY, Direction::Out, Value::Int(0)))
        .with_point(sim_point(STAGED, Direction::Out, Value::Int(0)))
        .with_point(sim_point(
            NONE_AVAILABLE,
            Direction::Out,
            Value::Bool(false),
        ))
        .with_point(sim_point(ALL_FAULTED, Direction::Out, Value::Bool(false)))
        .with_loopback(Loopback {
            output: CMD_1,
            input: RUN_1,
        })
        .with_loopback(Loopback {
            output: CMD_2,
            input: RUN_2,
        })
}

fn point_map() -> PointMap {
    [
        (DEMAND, Direction::In, ValueKind::Int),
        (CMD_1, Direction::Out, ValueKind::Bool),
        (RUN_1, Direction::In, ValueKind::Bool),
        (FAULT_1, Direction::In, ValueKind::Bool),
        (AVAIL_1, Direction::In, ValueKind::Bool),
        (CMD_2, Direction::Out, ValueKind::Bool),
        (RUN_2, Direction::In, ValueKind::Bool),
        (FAULT_2, Direction::In, ValueKind::Bool),
        (AVAIL_2, Direction::In, ValueKind::Bool),
        (DUTY, Direction::Out, ValueKind::Int),
        (STAGED, Direction::Out, ValueKind::Int),
        (NONE_AVAILABLE, Direction::Out, ValueKind::Bool),
        (ALL_FAULTED, Direction::Out, ValueKind::Bool),
    ]
    .into_iter()
    .collect()
}

fn group(name: &str) -> PumpGroup {
    PumpGroup::new(
        name,
        DEMAND,
        vec![
            PumpIo {
                cmd: CMD_1,
                run: RUN_1,
                fault: FAULT_1,
                avail: AVAIL_1,
            },
            PumpIo {
                cmd: CMD_2,
                run: RUN_2,
                fault: FAULT_2,
                avail: AVAIL_2,
            },
        ],
        GroupOutputs {
            duty: DUTY,
            staged: STAGED,
            none_available: NONE_AVAILABLE,
            all_faulted: ALL_FAULTED,
        },
        PumpGroupConfig {
            rotation: RotationPolicy::TimedInterval,
            rotation_ticks: 4,
            start_delay_ticks: 1,
            restage_delay_ticks: 1,
            min_off_ticks: 1,
        },
    )
    .unwrap()
}

/// One scan's observable outputs — the command pair, the duty and
/// staged reports, and the two group conditions.
type Outputs = [Sample; 6];

fn iterate(executor: &mut Executor<'_>, sim: &SimDriver, outputs: &mut Vec<Outputs>) {
    executor.scan();
    sim.step(DT);
    outputs.push([
        sim.read(CMD_1).unwrap(),
        sim.read(CMD_2).unwrap(),
        sim.read(DUTY).unwrap(),
        sim.read(STAGED).unwrap(),
        sim.read(NONE_AVAILABLE).unwrap(),
        sim.read(ALL_FAULTED).unwrap(),
    ]);
}

/// The demand profile: one pump for the first eight scans, both pumps
/// while demand stands at 2, then the lag de-staged again — so the
/// checkpoint lands with unequal accumulated run-hours.
fn drive(sim: &SimDriver, scan: u64) {
    if scan == 9 {
        sim.write(DEMAND, Value::Int(2)).unwrap();
    } else if scan == 16 {
        sim.write(DEMAND, Value::Int(1)).unwrap();
    }
}

fn run_uninterrupted(total: u64) -> Vec<Outputs> {
    let sim = SimDriver::new(channel_map()).unwrap();
    sim.write(DEMAND, Value::Int(1)).unwrap();
    let mut executor = Executor::new(&sim, point_map(), vec![Box::new(group("pg"))]).unwrap();
    let mut outputs = Vec::new();
    for scan in 1..=total {
        drive(&sim, scan);
        iterate(&mut executor, &sim, &mut outputs);
    }
    outputs
}

/// The switchover run: N iterations, checkpoint, then fresh driver and
/// component instances restored from it run the remaining M.
fn run_with_restore() -> (Vec<Outputs>, Checkpoint) {
    let sim = SimDriver::new(channel_map()).unwrap();
    sim.write(DEMAND, Value::Int(1)).unwrap();
    let mut executor = Executor::new(&sim, point_map(), vec![Box::new(group("pg"))]).unwrap();
    let mut outputs = Vec::new();
    for scan in 1..=CHECKPOINT_AT {
        drive(&sim, scan);
        iterate(&mut executor, &sim, &mut outputs);
    }
    let checkpoint = executor.checkpoint();

    // A standby rebuilds from the same plant model — fresh driver and
    // component — and applies the checkpoint.
    let standby_sim = SimDriver::new(channel_map()).unwrap();
    let mut restored = Executor::restore(
        &standby_sim,
        point_map(),
        vec![Box::new(group("pg"))],
        &checkpoint,
        None,
    )
    .unwrap();
    assert_eq!(restored.tick(), Tick(CHECKPOINT_AT));
    for scan in (CHECKPOINT_AT + 1)..=(CHECKPOINT_AT + CONTINUED) {
        drive(&standby_sim, scan);
        iterate(&mut restored, &standby_sim, &mut outputs);
    }
    (outputs, checkpoint)
}

#[test]
fn restored_run_produces_identical_outputs() {
    let (restored_outputs, checkpoint) = run_with_restore();
    let expected = run_uninterrupted(CHECKPOINT_AT + CONTINUED);

    // The checkpoint landed mid-rotation: duty on pump 2 and unequal
    // accumulated run-hours carried in state.
    let state = &checkpoint.components["pg"];
    let Some(Value::Int(hours_1)) = state.get("run_hours_1") else {
        panic!("pg state must carry Int run_hours_1")
    };
    let Some(Value::Int(hours_2)) = state.get("run_hours_2") else {
        panic!("pg state must carry Int run_hours_2")
    };
    assert!(hours_1 > 0 && hours_2 > 0 && hours_1 != hours_2);
    assert_eq!(
        state.get("duty"),
        Some(Value::Int(2)),
        "a duty assignment must be in flight"
    );

    assert_eq!(restored_outputs.len(), (CHECKPOINT_AT + CONTINUED) as usize);
    assert_eq!(restored_outputs, expected);
}

#[test]
fn repeated_runs_produce_identical_outputs() {
    assert_eq!(
        run_uninterrupted(CHECKPOINT_AT + CONTINUED),
        run_uninterrupted(CHECKPOINT_AT + CONTINUED)
    );
}

#[test]
fn mid_rotation_state_round_trips_through_the_component() {
    // Capture mid-rotation state out of the live executor and restore
    // it into a freshly constructed group: rotation position, run
    // hours, and command state all transfer, and the standby's own
    // capture reproduces the map.
    let sim = SimDriver::new(channel_map()).unwrap();
    sim.write(DEMAND, Value::Int(1)).unwrap();
    let mut executor = Executor::new(&sim, point_map(), vec![Box::new(group("pg"))]).unwrap();
    let mut outputs = Vec::new();
    for scan in 1..=CHECKPOINT_AT {
        drive(&sim, scan);
        iterate(&mut executor, &sim, &mut outputs);
    }
    let state = executor.checkpoint().components["pg"].clone();

    let mut standby = group("pg");
    standby.restore_state(&state).unwrap();
    assert_eq!(standby.capture_state(), state);
}
