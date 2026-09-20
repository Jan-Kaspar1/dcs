//! Integration test for `BackwashCoordinator` under the executor: a
//! three-filter bank under FIFO ordering with the operator `reorder`
//! instruction bound to a writable internal `In` point, so reorder
//! writes ride the receipted command path. A scripted run exercises the
//! held grant through a permissive outage, the standing reorder
//! instruction, and release-on-request-drop handing the grant down the
//! queue; a checkpoint mid-queue — the held grant and the ordered
//! pending members in flight — restored into a fresh executor and
//! driver must continue identically, and repeated runs must agree
//! exactly.

use dcs_blocks::{
    BackwashCoordinator, BackwashCoordinatorConfig, CoordinatorOutputs, FilterIo, PermissiveInputs,
    QueuePolicy, QueuedState,
};
use dcs_core::{
    Command, CommandOutcome, Direction, IoDriver, PointId, Sample, Tick, Value, ValueKind,
};
use dcs_runtime::{Checkpoint, Component, Executor, PointMap};
use dcs_sim::{ChannelId, ChannelMap, PointBinding, SimDriver};

const SUPPLY: PointId = PointId(10);
const WASTE: PointId = PointId(11);
const FLOW: PointId = PointId(12);
const REQUEST_1: PointId = PointId(20);
const REQUEST_2: PointId = PointId(21);
const REQUEST_3: PointId = PointId(22);
const REORDER: PointId = PointId(30);
const GRANT_1: PointId = PointId(40);
const GRANT_2: PointId = PointId(41);
const GRANT_3: PointId = PointId(42);
const POSITION_1: PointId = PointId(50);
const POSITION_2: PointId = PointId(51);
const POSITION_3: PointId = PointId(52);
const ACTIVE: PointId = PointId(60);
const QUEUED: PointId = PointId(61);
const BLOCKED: PointId = PointId(62);

/// Simulated time each scan advances.
const DT: f64 = 0.1;

/// The checkpoint lands mid-queue: the grant held on filter 1, the
/// queue carrying the reordered members, and the standing reorder
/// instruction written to the internal point.
const CHECKPOINT_AT: u64 = 9;
const CONTINUED: u64 = 9;

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

/// The simulated plant: the coordinator's field inputs as `In` points
/// and its driven outputs as `Out` points. `REORDER` is not served —
/// it is an image-carried writable internal point, the receipted
/// command target the point map declares.
fn channel_map() -> ChannelMap {
    ChannelMap::new()
        .with_point(sim_point(SUPPLY, Direction::In, Value::Bool(true)))
        .with_point(sim_point(WASTE, Direction::In, Value::Bool(true)))
        .with_point(sim_point(FLOW, Direction::In, Value::Bool(true)))
        .with_point(sim_point(REQUEST_1, Direction::In, Value::Bool(false)))
        .with_point(sim_point(REQUEST_2, Direction::In, Value::Bool(false)))
        .with_point(sim_point(REQUEST_3, Direction::In, Value::Bool(false)))
        .with_point(sim_point(GRANT_1, Direction::Out, Value::Bool(false)))
        .with_point(sim_point(GRANT_2, Direction::Out, Value::Bool(false)))
        .with_point(sim_point(GRANT_3, Direction::Out, Value::Bool(false)))
        .with_point(sim_point(POSITION_1, Direction::Out, Value::Int(0)))
        .with_point(sim_point(POSITION_2, Direction::Out, Value::Int(0)))
        .with_point(sim_point(POSITION_3, Direction::Out, Value::Int(0)))
        .with_point(sim_point(ACTIVE, Direction::Out, Value::Int(0)))
        .with_point(sim_point(QUEUED, Direction::Out, Value::Int(0)))
        .with_point(sim_point(BLOCKED, Direction::Out, Value::Bool(false)))
}

fn point_map() -> PointMap {
    PointMap::new()
        .with_point(SUPPLY, Direction::In, ValueKind::Bool)
        .with_point(WASTE, Direction::In, ValueKind::Bool)
        .with_point(FLOW, Direction::In, ValueKind::Bool)
        .with_point(REQUEST_1, Direction::In, ValueKind::Bool)
        .with_point(REQUEST_2, Direction::In, ValueKind::Bool)
        .with_point(REQUEST_3, Direction::In, ValueKind::Bool)
        .with_writable_internal(REORDER, Direction::In, ValueKind::Int, Value::Int(0))
        .with_point(GRANT_1, Direction::Out, ValueKind::Bool)
        .with_point(GRANT_2, Direction::Out, ValueKind::Bool)
        .with_point(GRANT_3, Direction::Out, ValueKind::Bool)
        .with_point(POSITION_1, Direction::Out, ValueKind::Int)
        .with_point(POSITION_2, Direction::Out, ValueKind::Int)
        .with_point(POSITION_3, Direction::Out, ValueKind::Int)
        .with_point(ACTIVE, Direction::Out, ValueKind::Int)
        .with_point(QUEUED, Direction::Out, ValueKind::Int)
        .with_point(BLOCKED, Direction::Out, ValueKind::Bool)
}

fn coordinator(name: &str) -> BackwashCoordinator {
    BackwashCoordinator::new(
        name,
        PermissiveInputs {
            supply_ok: SUPPLY,
            waste_ok: WASTE,
            flow_ok: FLOW,
        },
        Some(REORDER),
        vec![
            FilterIo {
                request: REQUEST_1,
                grant: GRANT_1,
                position: POSITION_1,
            },
            FilterIo {
                request: REQUEST_2,
                grant: GRANT_2,
                position: POSITION_2,
            },
            FilterIo {
                request: REQUEST_3,
                grant: GRANT_3,
                position: POSITION_3,
            },
        ],
        CoordinatorOutputs {
            active: ACTIVE,
            queued: QUEUED,
            resource_blocked: BLOCKED,
        },
        BackwashCoordinatorConfig {
            queue_policy: QueuePolicy::Fifo,
            queued_state: QueuedState::KeepFiltering,
        },
    )
    .unwrap()
}

/// One scan's observable outputs: the three grants, the three queue
/// positions, and the bank-level reports.
type Outputs = [Sample; 9];

fn iterate(executor: &mut Executor<'_>, sim: &SimDriver, outputs: &mut Vec<Outputs>) {
    executor.scan();
    sim.step(DT);
    outputs.push([
        sim.read(GRANT_1).unwrap(),
        sim.read(GRANT_2).unwrap(),
        sim.read(GRANT_3).unwrap(),
        sim.read(POSITION_1).unwrap(),
        sim.read(POSITION_2).unwrap(),
        sim.read(POSITION_3).unwrap(),
        sim.read(ACTIVE).unwrap(),
        sim.read(QUEUED).unwrap(),
        sim.read(BLOCKED).unwrap(),
    ]);
}

/// The field-side schedule: filter 1 requests first and takes the
/// grant; filters 3 then 2 queue behind it; the supply permissive drops
/// and restores while the grant holds; each request then releases in
/// turn so the grant walks the queue.
fn drive(sim: &SimDriver, scan: u64) {
    match scan {
        2 => sim.write(REQUEST_1, Value::Bool(true)).unwrap(),
        4 => sim.write(REQUEST_3, Value::Bool(true)).unwrap(),
        5 => sim.write(REQUEST_2, Value::Bool(true)).unwrap(),
        7 => sim.write(SUPPLY, Value::Bool(false)).unwrap(),
        9 => sim.write(SUPPLY, Value::Bool(true)).unwrap(),
        11 => sim.write(REQUEST_1, Value::Bool(false)).unwrap(),
        13 => sim.write(REQUEST_2, Value::Bool(false)).unwrap(),
        15 => sim.write(REQUEST_3, Value::Bool(false)).unwrap(),
        _ => {}
    }
}

/// The receipted operator instructions, keyed to the same scans the
/// uninterrupted run sees them: at scan 8 — while the supply
/// permissive is out — the standing reorder instruction promotes
/// queued filter 2 to the head; at scan 10 the instruction clears.
fn drive_commands(executor: &mut Executor<'_>, scan: u64) {
    let reorder = |value| Command::WriteValue {
        point: REORDER,
        kind: ValueKind::Int,
        value: Value::Int(value),
    };
    match scan {
        8 | 10 => {
            let value = if scan == 8 { 2 } else { 0 };
            let receipt = executor.submit_command(reorder(value));
            assert!(
                matches!(receipt.outcome, CommandOutcome::Accepted { .. }),
                "reorder write at scan {scan} must be accepted"
            );
        }
        _ => {}
    }
}

fn run_uninterrupted(total: u64) -> Vec<Outputs> {
    let sim = SimDriver::new(channel_map()).unwrap();
    let mut executor =
        Executor::new(&sim, point_map(), vec![Box::new(coordinator("bwc"))]).unwrap();
    let mut outputs = Vec::new();
    for scan in 1..=total {
        drive(&sim, scan);
        drive_commands(&mut executor, scan);
        iterate(&mut executor, &sim, &mut outputs);
    }
    outputs
}

/// The switchover run: N iterations, checkpoint, then fresh driver and
/// component instances restored from it run the remaining M — the
/// standing reorder value transferring in the checkpoint's internal
/// section.
fn run_with_restore() -> (Vec<Outputs>, Checkpoint) {
    let sim = SimDriver::new(channel_map()).unwrap();
    let mut executor =
        Executor::new(&sim, point_map(), vec![Box::new(coordinator("bwc"))]).unwrap();
    let mut outputs = Vec::new();
    for scan in 1..=CHECKPOINT_AT {
        drive(&sim, scan);
        drive_commands(&mut executor, scan);
        iterate(&mut executor, &sim, &mut outputs);
    }
    let checkpoint = executor.checkpoint();

    // A standby rebuilds from the same plant model — fresh driver and
    // component — and applies the checkpoint.
    let standby_sim = SimDriver::new(channel_map()).unwrap();
    let mut restored = Executor::restore(
        &standby_sim,
        point_map(),
        vec![Box::new(coordinator("bwc"))],
        &checkpoint,
        None,
    )
    .unwrap();
    assert_eq!(restored.tick(), Tick(CHECKPOINT_AT));
    for scan in (CHECKPOINT_AT + 1)..=(CHECKPOINT_AT + CONTINUED) {
        drive(&standby_sim, scan);
        drive_commands(&mut restored, scan);
        iterate(&mut restored, &standby_sim, &mut outputs);
    }
    (outputs, checkpoint)
}

#[test]
fn scripted_run_demonstrates_exclusive_grant_queue_and_gating() {
    let outputs = run_uninterrupted(CHECKPOINT_AT + CONTINUED);
    let at = |scan: u64| &outputs[(scan - 1) as usize];
    let grant = |scan: u64, index: usize| at(scan)[index - 1].value == Value::Bool(true);
    let grants = |scan: u64| [grant(scan, 1), grant(scan, 2), grant(scan, 3)];
    let position = |scan: u64, index: usize| at(scan)[2 + index].value;
    let active = |scan: u64| at(scan)[6].value;
    let queued = |scan: u64| at(scan)[7].value;
    let blocked = |scan: u64| at(scan)[8].value;

    // Scan 1: nothing requested — the bank idles.
    assert_eq!(grants(1), [false, false, false]);
    assert_eq!(active(1), Value::Int(0));
    assert_eq!(queued(1), Value::Int(0));

    // Scans 2-3: filter 1's request takes the grant and reports no
    // queue slot — the holder has left the queue.
    for scan in 2..=3 {
        assert_eq!(grants(scan), [true, false, false]);
        assert_eq!(active(scan), Value::Int(1));
        assert_eq!(position(scan, 1), Value::Int(0));
    }

    // Scan 4: filter 3 queues behind the held grant.
    assert_eq!(queued(4), Value::Int(1));
    assert_eq!(position(4, 3), Value::Int(1));
    // Scan 5: filter 2 queues behind filter 3 — FIFO arrival order.
    assert_eq!(queued(5), Value::Int(2));
    assert_eq!(position(5, 3), Value::Int(1));
    assert_eq!(position(5, 2), Value::Int(2));

    // Scans 7-8: the supply permissive drops while the grant holds —
    // the grant output falls but the holding stands (`active` still
    // reports 1) and `resource_blocked` asserts over the non-empty
    // queue.
    for scan in 7..=8 {
        assert_eq!(grants(scan), [false, false, false]);
        assert_eq!(active(scan), Value::Int(1));
        assert_eq!(blocked(scan), Value::Bool(true));
    }
    // Scan 8 also lands the receipted reorder: filter 2 takes the
    // queue's head while the grant is still gated.
    assert_eq!(position(8, 2), Value::Int(1));
    assert_eq!(position(8, 3), Value::Int(2));

    // Scan 9: the permissive restores — the held grant re-asserts
    // without re-queuing, and `resource_blocked` clears.
    assert_eq!(grants(9), [true, false, false]);
    assert_eq!(active(9), Value::Int(1));
    assert_eq!(blocked(9), Value::Bool(false));

    // Scan 11: filter 1's request drops — the grant releases and
    // passes to the queue's head, the reordered filter 2, the same
    // scan.
    assert_eq!(grants(11), [false, true, false]);
    assert_eq!(active(11), Value::Int(2));
    assert_eq!(position(11, 2), Value::Int(0));
    assert_eq!(position(11, 3), Value::Int(1));
    assert_eq!(queued(11), Value::Int(1));

    // Scan 13: filter 2's request drops — the grant passes to filter
    // 3, the last queued member.
    assert_eq!(grants(13), [false, false, true]);
    assert_eq!(active(13), Value::Int(3));
    assert_eq!(queued(13), Value::Int(0));

    // Scan 15: the last request drops — the bank idles.
    assert_eq!(grants(15), [false, false, false]);
    assert_eq!(active(15), Value::Int(0));

    // At no scan does more than one grant assert.
    for (index, scan_outputs) in outputs.iter().enumerate() {
        let asserted = scan_outputs[..3]
            .iter()
            .filter(|sample| sample.value == Value::Bool(true))
            .count();
        assert!(
            asserted <= 1,
            "scan {} asserts {asserted} grants",
            index + 1
        );
    }
}

#[test]
fn restored_run_produces_identical_outputs() {
    let (restored_outputs, checkpoint) = run_with_restore();
    let expected = run_uninterrupted(CHECKPOINT_AT + CONTINUED);

    // The checkpoint landed mid-queue: the held grant and the two
    // queued members — reordered by the standing instruction — are in
    // flight in the component's state.
    let state = &checkpoint.components["bwc"];
    assert_eq!(state.get("granted"), Some(Value::Int(1)));
    assert_eq!(state.get("queued_count"), Some(Value::Int(2)));
    assert_eq!(state.get("queue_1"), Some(Value::Int(2)));
    assert_eq!(state.get("queue_2"), Some(Value::Int(3)));
    // And the standing reorder instruction transfers in the
    // checkpoint's internal section.
    assert_eq!(
        checkpoint.internal.get(&REORDER).map(|sample| sample.value),
        Some(Value::Int(2))
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
fn mid_queue_state_round_trips_through_the_component() {
    // Capture mid-queue state out of the live executor and restore it
    // into a freshly constructed coordinator: the held grant, the
    // ordered queue, and the tuned parameters all transfer, and the
    // standby's own capture reproduces the map.
    let sim = SimDriver::new(channel_map()).unwrap();
    let mut executor =
        Executor::new(&sim, point_map(), vec![Box::new(coordinator("bwc"))]).unwrap();
    let mut outputs = Vec::new();
    for scan in 1..=CHECKPOINT_AT {
        drive(&sim, scan);
        drive_commands(&mut executor, scan);
        iterate(&mut executor, &sim, &mut outputs);
    }
    let state = executor.checkpoint().components["bwc"].clone();

    let mut standby = coordinator("bwc");
    standby.restore_state(&state).unwrap();
    assert_eq!(standby.capture_state(), state);
}
