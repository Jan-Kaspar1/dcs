//! Integration tests for the capacity-staging `blower-group` kind: the
//! checked-in fixture instantiates five groups on one scripted device —
//! `blower-group:10` a three-unit automatic no-rotation group exercising
//! the thresholds, the capacity split and clamps, the vent-based join
//! and departure choreography, the prove timeout, the start interval,
//! faulted-unit exclusion with standby handover, and the starvation
//! window; `blower-group:11` equalize-runtime rotation with `transition`
//! asserted across the handover; `blower-group:12` fixed order with a
//! warranted departure held behind `min_run_ticks`; `blower-group:13`
//! operator approval through the writable `approve` point; and
//! `blower-group:14` flag-only. Identical runs produce identical
//! snapshots and write journals, and a checkpointed standby resumes a
//! mid-join run identically.

use dcs_assembly::{
    AssemblyError, BuildError, ComponentRegistry, DriverRegistry, FanoutDriver, assemble,
    resolve_drivers,
};
use dcs_blocks::{BlowerGroup, BlowerIo, BlowerOutputs};
use dcs_core::{Command, CommandOutcome, IoDriver, PointId, Value, ValueKind};
use dcs_model::{DeviceId, PlantModel};
use dcs_runtime::{Component, Executor};
use dcs_sim::ScriptedDriver;

/// The fixture: one `sim-scripted` device replaying per-group demand,
/// run, fault, and availability scripts; five `blower-group` instances
/// covering every `staging_authority` and `rotation` code; and the
/// writable internal point 70 feeding component 13's `approve` port.
const BLOWER_GROUP: &str = include_str!("../fixtures/blower_group.json");

const APPROVE: PointId = PointId(70);

const CMD_A1: PointId = PointId(100);
const CAP_A1: PointId = PointId(103);
const CAP_A2: PointId = PointId(104);
const CAP_A3: PointId = PointId(105);
const VENT_A1: PointId = PointId(106);
const VENT_A2: PointId = PointId(107);
const VENT_A3: PointId = PointId(108);
const STAGED_A: PointId = PointId(109);
const NONE_A: PointId = PointId(110);
const FAULTED_A: PointId = PointId(111);
const TRANSITION_A: PointId = PointId(113);

const CMD_B1: PointId = PointId(120);
const CMD_B2: PointId = PointId(121);
const CAP_B1: PointId = PointId(122);
const CAP_B2: PointId = PointId(123);
const VENT_B1: PointId = PointId(124);
const VENT_B2: PointId = PointId(125);
const STAGED_B: PointId = PointId(126);
const TRANSITION_B: PointId = PointId(130);

const CMD_C1: PointId = PointId(140);
const CAP_C2: PointId = PointId(143);
const VENT_C1: PointId = PointId(144);
const STAGED_C: PointId = PointId(146);
const TRANSITION_C: PointId = PointId(150);

const CMD_D1: PointId = PointId(160);
const CMD_D2: PointId = PointId(161);
const CAP_D1: PointId = PointId(162);
const CAP_D2: PointId = PointId(163);
const VENT_D1: PointId = PointId(164);
const VENT_D2: PointId = PointId(165);
const STAGED_D: PointId = PointId(166);
const PENDING_D: PointId = PointId(169);
const TRANSITION_D: PointId = PointId(170);

const CMD_E1: PointId = PointId(180);
const CMD_E2: PointId = PointId(181);
const STAGED_E: PointId = PointId(186);
const PENDING_E: PointId = PointId(189);

const SCRIPTED_DEVICE: DeviceId = DeviceId(1);

fn boxed<C, E>(result: Result<C, E>) -> Result<Box<dyn Component>, BuildError>
where
    C: Component + 'static,
    E: std::error::Error + 'static,
{
    result
        .map(|component| Box::new(component) as Box<dyn Component>)
        .map_err(BuildError::other)
}

/// The `dcs-blocks` registration for the kinds the fixture uses,
/// mirroring the controller registry's six-family indexed-port
/// discovery and its optional `approve` binding.
fn registry() -> ComponentRegistry {
    ComponentRegistry::new().with(BlowerGroup::KIND, |spec| {
        let blowers = spec
            .indexed_families(["cmd_", "run_", "fault_", "avail_", "capacity_", "vent_"])?
            .into_iter()
            .map(|[cmd, run, fault, avail, capacity, vent]| BlowerIo {
                cmd,
                run,
                fault,
                avail,
                capacity,
                vent,
            })
            .collect();
        boxed(BlowerGroup::from_parameters(
            spec.name.as_str(),
            spec.require("demand")?,
            spec.get("approve"),
            blowers,
            BlowerOutputs {
                staged: spec.require("staged")?,
                none_available: spec.require("none_available")?,
                all_faulted: spec.require("all_faulted")?,
                staging_pending: spec.require("staging_pending")?,
                transition: spec.require("transition")?,
            },
            spec.parameters,
        ))
    })
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

fn int(driver: &FanoutDriver, point: PointId) -> i64 {
    match driver.read(point).unwrap().value {
        Value::Int(value) => value,
        value => panic!("point {point:?}: expected Int, got {value:?}"),
    }
}

fn float(driver: &FanoutDriver, point: PointId) -> f64 {
    match driver.read(point).unwrap().value {
        Value::Float(value) => value,
        value => panic!("point {point:?}: expected Float, got {value:?}"),
    }
}

fn boolean(driver: &FanoutDriver, point: PointId) -> bool {
    match driver.read(point).unwrap().value {
        Value::Bool(value) => value,
        value => panic!("point {point:?}: expected Bool, got {value:?}"),
    }
}

/// The scripted run. Each scan reads the scripted entries the driver's
/// last `step` made current: a script entry at tick `t` takes effect at
/// scan `t + 1`.
fn scan(executor: &mut Executor, driver: &FanoutDriver) {
    executor.scan().unwrap();
    driver.step(0.1).unwrap();
}

#[test]
fn blower_group_fixture_assembles_through_the_registry() {
    let model = model(BLOWER_GROUP);
    let driver = build_driver(&model);
    let mut executor = build_executor(&model, &driver);
    scan(&mut executor, &driver);
    assert!(
        executor
            .snapshot()
            .components
            .iter()
            .all(|component| component.step_errors == 0)
    );
}

/// The automatic no-rotation group: stage-up and stage-down on the
/// declared demand thresholds, the equal split clamped to the tighter
/// `unit_1_max_current` ceiling, the vent-based join and departure
/// sequences, the unproven-join timeout behind `min_start_interval`,
/// fault exclusion with the standby handed the demand, the starved
/// `none_available`/`all_faulted` window, and a bad demand sample
/// holding the last good value.
#[test]
fn scripted_run_demonstrates_thresholds_split_and_choreography() {
    let model = model(BLOWER_GROUP);
    let driver = build_driver(&model);
    let mut executor = build_executor(&model, &driver);

    // Scans 1-2, demand 0: nothing stages.
    for _ in 0..2 {
        scan(&mut executor, &driver);
        assert_eq!(int(&driver, STAGED_A), 0);
        assert!(!boolean(&driver, CMD_A1));
    }

    // Scan 3 reads demand 50: the join choreography starts unit 1 —
    // vent open and command asserted, the unit still off the header
    // and reporting its declared minimum.
    scan(&mut executor, &driver);
    assert_eq!(int(&driver, STAGED_A), 1);
    assert!(boolean(&driver, CMD_A1));
    assert!(boolean(&driver, VENT_A1));
    assert_eq!(float(&driver, CAP_A1), 20.0);
    assert!(boolean(&driver, TRANSITION_A));

    // Scan 4 reads run-a1: proven within `vent_ticks`, the vent closes
    // and the unit joins carrying the whole demand.
    scan(&mut executor, &driver);
    assert!(!boolean(&driver, VENT_A1));
    assert_eq!(float(&driver, CAP_A1), 50.0);
    assert!(!boolean(&driver, TRANSITION_A));
    scan(&mut executor, &driver);

    // Scan 6 reads demand 130: 130 > 0.9 × unit 1's 60 ceiling stages
    // unit 2 in; the joined unit already clamps at its ceiling.
    scan(&mut executor, &driver);
    assert_eq!(int(&driver, STAGED_A), 2);
    assert!(boolean(&driver, VENT_A2));
    assert_eq!(float(&driver, CAP_A1), 60.0);
    assert_eq!(float(&driver, CAP_A2), 20.0);

    // Scan 7: unit 2 proven and joined — the 130 split is 65 a side;
    // unit 1's 60 ceiling (its `max_current` bound) takes the clamp.
    scan(&mut executor, &driver);
    assert!(!boolean(&driver, VENT_A2));
    assert_eq!(float(&driver, CAP_A1), 60.0);
    assert_eq!(float(&driver, CAP_A2), 65.0);
    assert_eq!(int(&driver, STAGED_A), 2);
    for _ in 0..2 {
        scan(&mut executor, &driver);
    }

    // Scan 10 reads fault-a1: the joined unit's proven failure vents
    // and drops it mid-scan — the demand it carried lands on unit 2's
    // ceiling while the stage-up request waits behind the in-flight
    // departure.
    scan(&mut executor, &driver);
    assert!(!boolean(&driver, CMD_A1));
    assert!(boolean(&driver, VENT_A1));
    assert_eq!(float(&driver, CAP_A1), 0.0);
    assert_eq!(float(&driver, CAP_A2), 90.0);
    assert_eq!(int(&driver, STAGED_A), 1);
    assert!(boolean(&driver, TRANSITION_A));

    // Scan 11 reads run-a1 false: the proven stop resolves the
    // departure and the same scan hands the staged demand to the
    // standby — unit 3 vents and starts.
    scan(&mut executor, &driver);
    assert!(boolean(&driver, VENT_A3));
    assert_eq!(float(&driver, CAP_A3), 20.0);
    assert_eq!(int(&driver, STAGED_A), 2);

    // Scan 12 reads run-a3: the standby joins; the pair splits 130.
    scan(&mut executor, &driver);
    assert!(!boolean(&driver, VENT_A3));
    assert_eq!(float(&driver, CAP_A2), 65.0);
    assert_eq!(float(&driver, CAP_A3), 65.0);
    assert_eq!(int(&driver, STAGED_A), 2);
    assert!(!boolean(&driver, TRANSITION_A));
    for _ in 0..4 {
        scan(&mut executor, &driver);
    }

    // Scan 17 reads demand 200: over 0.9 × the committed 180, unit 1 —
    // fault cleared and past its start interval — rejoins.
    scan(&mut executor, &driver);
    assert_eq!(int(&driver, STAGED_A), 3);
    assert!(boolean(&driver, VENT_A1));
    assert_eq!(float(&driver, CAP_A2), 90.0);

    // Scans 18-19: unit 1's run feedback never arrives — the join
    // proves nothing within `vent_ticks` and aborts back offline.
    scan(&mut executor, &driver);
    assert!(boolean(&driver, VENT_A1));
    scan(&mut executor, &driver);
    assert!(!boolean(&driver, CMD_A1));
    assert!(!boolean(&driver, VENT_A1));
    assert_eq!(int(&driver, STAGED_A), 2);
    assert!(!boolean(&driver, TRANSITION_A));

    // Scans 20-21: the stage-up stands warranted but the aborted unit
    // is inside `min_start_interval_ticks` — nothing moves.
    for _ in 0..2 {
        scan(&mut executor, &driver);
        assert_eq!(int(&driver, STAGED_A), 2);
    }

    // Scans 22-24: the interval clears, the retry starts — and times
    // out again, banking another start interval.
    scan(&mut executor, &driver);
    assert!(boolean(&driver, VENT_A1));
    assert_eq!(int(&driver, STAGED_A), 3);
    for _ in 0..2 {
        scan(&mut executor, &driver);
    }
    assert_eq!(int(&driver, STAGED_A), 2);
    for _ in 0..2 {
        scan(&mut executor, &driver);
        assert_eq!(int(&driver, STAGED_A), 2);
    }

    // Scans 27-28: the third start proves — run-a1 arrived — and the
    // three-way 200 split clamps unit 1 at its 60 ceiling while the
    // others carry 66.67.
    scan(&mut executor, &driver);
    assert!(boolean(&driver, VENT_A1));
    scan(&mut executor, &driver);
    assert!(!boolean(&driver, VENT_A1));
    assert_eq!(int(&driver, STAGED_A), 3);
    assert_eq!(float(&driver, CAP_A1), 60.0);
    assert_eq!(float(&driver, CAP_A2), 200.0 / 3.0);
    assert_eq!(float(&driver, CAP_A3), 200.0 / 3.0);
    for _ in 0..2 {
        scan(&mut executor, &driver);
    }

    // Scan 31 reads demand 100: below 0.8 × the 180 remaining without
    // unit 1 — the most recently joined unit departs behind its vent.
    scan(&mut executor, &driver);
    assert!(!boolean(&driver, CMD_A1));
    assert!(boolean(&driver, VENT_A1));
    assert_eq!(int(&driver, STAGED_A), 2);
    assert!(boolean(&driver, TRANSITION_A));

    // Scan 32 reads the proven stop; the pair left splits 100 evenly.
    scan(&mut executor, &driver);
    assert_eq!(int(&driver, STAGED_A), 2);
    assert_eq!(float(&driver, CAP_A2), 50.0);
    assert_eq!(float(&driver, CAP_A3), 50.0);
    for _ in 0..8 {
        scan(&mut executor, &driver);
    }

    // Scan 41 reads every fault asserted: both joined units depart,
    // nothing is available, and both group conditions stand.
    scan(&mut executor, &driver);
    assert_eq!(int(&driver, STAGED_A), 0);
    assert!(boolean(&driver, VENT_A2));
    assert!(boolean(&driver, VENT_A3));
    assert!(boolean(&driver, NONE_A));
    assert!(boolean(&driver, FAULTED_A));
    for _ in 0..3 {
        scan(&mut executor, &driver);
        assert_eq!(int(&driver, STAGED_A), 0);
        assert!(boolean(&driver, NONE_A));
        assert!(boolean(&driver, FAULTED_A));
    }

    // Scan 45 reads the faults cleared: unit 1 joins the recovery.
    scan(&mut executor, &driver);
    assert!(boolean(&driver, CMD_A1));
    assert!(boolean(&driver, VENT_A1));
    assert!(!boolean(&driver, NONE_A));
    assert!(!boolean(&driver, FAULTED_A));

    // Scan 46: unit 1 joins — and the whole demand over its 60
    // ceiling already stages unit 2 in behind it.
    scan(&mut executor, &driver);
    assert_eq!(int(&driver, STAGED_A), 2);
    assert_eq!(float(&driver, CAP_A1), 60.0);
    assert!(boolean(&driver, VENT_A2));

    // Scan 47 reads a bad-quality demand: the held last-good value
    // changes nothing while unit 2's unproven join runs its window.
    scan(&mut executor, &driver);
    assert_eq!(int(&driver, STAGED_A), 2);
    assert_eq!(float(&driver, CAP_A1), 60.0);
    assert!(boolean(&driver, VENT_A2));

    // Scan 48: the window lapses — unit 2 aborts offline — and the
    // warranted stage-up immediately falls to unit 3, the next
    // eligible standby past its start interval.
    scan(&mut executor, &driver);
    assert_eq!(int(&driver, STAGED_A), 2);
    assert!(!boolean(&driver, VENT_A2));
    assert!(boolean(&driver, VENT_A3));

    // Scans 49-50: unit 3's join proves nothing either and aborts;
    // both standbys are now inside fresh start intervals.
    scan(&mut executor, &driver);
    assert!(boolean(&driver, VENT_A3));
    scan(&mut executor, &driver);
    assert!(!boolean(&driver, VENT_A3));
    assert_eq!(int(&driver, STAGED_A), 1);

    // Scan 51 reads demand 0: unit 1 vents and departs, its proven
    // stop resolving a scan later.
    scan(&mut executor, &driver);
    assert_eq!(int(&driver, STAGED_A), 0);
    assert!(boolean(&driver, VENT_A1));
    scan(&mut executor, &driver);
    assert!(!boolean(&driver, VENT_A1));
    assert!(!boolean(&driver, TRANSITION_A));
}

/// The equalize-runtime group: the standby rotates in through the same
/// join choreography once the run-hours spread reaches the declared
/// margin, `transition` asserted across both legs, the banked
/// departure following only the proven join.
#[test]
fn equalize_runtime_rotates_through_the_join_choreography() {
    let model = model(BLOWER_GROUP);
    let driver = build_driver(&model);
    let mut executor = build_executor(&model, &driver);

    // Scans 2-3, demand 60: the run-hours tie joins the lowest index.
    for _ in 0..2 {
        scan(&mut executor, &driver);
    }
    assert!(boolean(&driver, CMD_B1));
    assert!(boolean(&driver, VENT_B1));
    assert_eq!(int(&driver, STAGED_B), 1);
    scan(&mut executor, &driver);
    assert!(!boolean(&driver, VENT_B1));
    assert_eq!(float(&driver, CAP_B1), 60.0);

    // Scan 4: unit 1's two proven ticks reach the margin over unit 2's
    // zero — the rotation's first leg joins the standby with
    // `transition` standing.
    scan(&mut executor, &driver);
    assert!(boolean(&driver, CMD_B2));
    assert!(boolean(&driver, VENT_B2));
    assert_eq!(int(&driver, STAGED_B), 2);
    assert!(boolean(&driver, TRANSITION_B));

    // Scan 5 reads run-b2: the proven join releases the banked leg —
    // unit 1 vents and departs in the same scan.
    scan(&mut executor, &driver);
    assert!(!boolean(&driver, VENT_B2));
    assert!(!boolean(&driver, CMD_B1));
    assert!(boolean(&driver, VENT_B1));
    assert_eq!(int(&driver, STAGED_B), 1);
    assert!(boolean(&driver, TRANSITION_B));

    // Scan 6 reads run-b1 false: the handover completes — unit 2
    // carries the demand alone.
    scan(&mut executor, &driver);
    assert!(!boolean(&driver, VENT_B1));
    assert_eq!(float(&driver, CAP_B2), 60.0);
    assert!(!boolean(&driver, TRANSITION_B));

    // Scans 9-10 read demand 150: unit 1 — past its start interval —
    // rejoins and the pair splits evenly.
    for _ in 0..3 {
        scan(&mut executor, &driver);
    }
    assert!(boolean(&driver, VENT_B1));
    assert_eq!(int(&driver, STAGED_B), 2);
    scan(&mut executor, &driver);
    assert!(!boolean(&driver, VENT_B1));
    assert_eq!(float(&driver, CAP_B1), 75.0);
    assert_eq!(float(&driver, CAP_B2), 75.0);
    scan(&mut executor, &driver);

    // Scan 12 reads demand 60: equalize departs the most-run joined
    // unit — unit 2's eight proven ticks over unit 1's six.
    scan(&mut executor, &driver);
    assert!(!boolean(&driver, CMD_B2));
    assert!(boolean(&driver, VENT_B2));
    assert_eq!(int(&driver, STAGED_B), 1);
    scan(&mut executor, &driver);
    assert_eq!(float(&driver, CAP_B1), 60.0);

    // Scans 21-22 read demand 0: the last unit departs and resolves.
    for _ in 0..7 {
        scan(&mut executor, &driver);
    }
    scan(&mut executor, &driver);
    assert_eq!(int(&driver, STAGED_B), 0);
    assert!(boolean(&driver, VENT_B1));
    scan(&mut executor, &driver);
    assert!(!boolean(&driver, VENT_B1));
    assert!(!boolean(&driver, TRANSITION_B));
}

/// The fixed-order group: `min_run_ticks` holds a warranted departure
/// until the first unit in the fixed order is old enough to leave.
#[test]
fn fixed_order_holds_departure_behind_min_run() {
    let model = model(BLOWER_GROUP);
    let driver = build_driver(&model);
    let mut executor = build_executor(&model, &driver);

    // Scans 2-3, demand 150: unit 1 joins lowest-indexed, proves, and
    // the still-warranted stage-up starts unit 2.
    for _ in 0..2 {
        scan(&mut executor, &driver);
    }
    assert!(boolean(&driver, CMD_C1));
    scan(&mut executor, &driver);
    assert_eq!(int(&driver, STAGED_C), 2);

    // Scan 4 reads demand 60 — below the stage-down threshold — but
    // both units are inside `min_run_ticks`: the warranted departure
    // waits, no unit moves.
    scan(&mut executor, &driver);
    assert_eq!(int(&driver, STAGED_C), 2);
    assert!(!boolean(&driver, TRANSITION_C));
    for _ in 0..2 {
        scan(&mut executor, &driver);
        assert_eq!(int(&driver, STAGED_C), 2);
    }

    // Scan 7: unit 1 reaches its minimum run and departs behind its
    // vent — the fixed order's preferred unit 2 is still held out.
    scan(&mut executor, &driver);
    assert!(!boolean(&driver, CMD_C1));
    assert!(boolean(&driver, VENT_C1));
    assert_eq!(int(&driver, STAGED_C), 1);
    assert!(boolean(&driver, TRANSITION_C));
    scan(&mut executor, &driver);
    scan(&mut executor, &driver);
    assert_eq!(float(&driver, CAP_C2), 60.0);
    assert!(!boolean(&driver, TRANSITION_C));
}

/// The operator-approval group: a warranted stage change holds on
/// `staging_pending` until a `Good` true on the writable `approve`
/// point grants it — one assertion grants one change, and the point
/// must clear before the next grant.
#[test]
fn operator_approval_holds_and_grants_stage_changes() {
    let model = model(BLOWER_GROUP);
    let driver = build_driver(&model);
    let mut executor = build_executor(&model, &driver);

    let approve = |executor: &mut Executor, value: bool| {
        let receipt = executor.submit_command(Command::WriteValue {
            point: APPROVE,
            kind: ValueKind::Bool,
            value: Value::Bool(value),
        });
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
    };

    // Scans 1-2 read demand 60: the stage-up request flags on
    // `staging_pending` and nothing moves without the grant.
    for _ in 0..2 {
        scan(&mut executor, &driver);
    }
    assert!(boolean(&driver, PENDING_D));
    assert_eq!(int(&driver, STAGED_D), 0);
    assert!(!boolean(&driver, CMD_D1));

    // The grant releases the pending join through the choreography.
    approve(&mut executor, true);
    scan(&mut executor, &driver);
    assert!(boolean(&driver, CMD_D1));
    assert!(boolean(&driver, VENT_D1));
    assert!(!boolean(&driver, PENDING_D));
    assert_eq!(int(&driver, STAGED_D), 1);
    scan(&mut executor, &driver);
    assert!(!boolean(&driver, VENT_D1));
    assert_eq!(float(&driver, CAP_D1), 60.0);
    scan(&mut executor, &driver);

    // Scan 6 reads demand 150: the consumed grant cannot release the
    // next change — the request holds pending.
    scan(&mut executor, &driver);
    assert!(boolean(&driver, PENDING_D));
    assert_eq!(int(&driver, STAGED_D), 1);

    // The point must clear before it grants again: a false scan
    // re-arms, the next true releases unit 2's join.
    approve(&mut executor, false);
    scan(&mut executor, &driver);
    assert!(boolean(&driver, PENDING_D));
    assert_eq!(int(&driver, STAGED_D), 1);
    approve(&mut executor, true);
    scan(&mut executor, &driver);
    assert!(boolean(&driver, CMD_D2));
    assert!(boolean(&driver, VENT_D2));
    assert_eq!(int(&driver, STAGED_D), 2);
    scan(&mut executor, &driver);
    assert!(!boolean(&driver, VENT_D2));
    assert_eq!(float(&driver, CAP_D1), 75.0);
    assert_eq!(float(&driver, CAP_D2), 75.0);
    scan(&mut executor, &driver);

    // Scan 11 reads demand 0: the departure holds pending the same
    // way — the consumed grant cannot release it until the point
    // clears and asserts again.
    scan(&mut executor, &driver);
    assert!(boolean(&driver, PENDING_D));
    approve(&mut executor, false);
    scan(&mut executor, &driver);
    assert!(boolean(&driver, PENDING_D));
    approve(&mut executor, true);
    scan(&mut executor, &driver);
    assert!(!boolean(&driver, CMD_D2));
    assert!(boolean(&driver, VENT_D2));
    assert_eq!(int(&driver, STAGED_D), 1);
    scan(&mut executor, &driver);
    assert!(boolean(&driver, PENDING_D));
    approve(&mut executor, false);
    scan(&mut executor, &driver);
    approve(&mut executor, true);
    scan(&mut executor, &driver);
    assert_eq!(int(&driver, STAGED_D), 0);
    assert!(boolean(&driver, VENT_D1));
    scan(&mut executor, &driver);
    assert!(!boolean(&driver, VENT_D1));
    assert!(!boolean(&driver, TRANSITION_D));
}

/// The flag-only group: a warranted stage change raises
/// `staging_pending` as the operator's recommendation and commands
/// nothing — the outputs stay off until the demand clears.
#[test]
fn flag_only_flags_recommendations_without_commanding() {
    let model = model(BLOWER_GROUP);
    let driver = build_driver(&model);
    let mut executor = build_executor(&model, &driver);

    for _ in 0..2 {
        scan(&mut executor, &driver);
    }
    assert!(boolean(&driver, PENDING_E));
    assert_eq!(int(&driver, STAGED_E), 0);
    for _ in 0..6 {
        scan(&mut executor, &driver);
        assert!(boolean(&driver, PENDING_E));
        assert!(!boolean(&driver, CMD_E1));
        assert!(!boolean(&driver, CMD_E2));
        assert_eq!(int(&driver, STAGED_E), 0);
    }
    // Scan 9 reads demand 0: the recommendation clears with the demand.
    scan(&mut executor, &driver);
    assert!(!boolean(&driver, PENDING_E));
}

#[test]
fn identical_scripted_runs_produce_identical_snapshots_and_journals() {
    let run = || {
        let model = model(BLOWER_GROUP);
        let driver = build_driver(&model);
        let mut executor = build_executor(&model, &driver);
        for _ in 0..55 {
            scan(&mut executor, &driver);
        }
        let snapshot = serde_json::to_string(&executor.snapshot()).unwrap();
        let writes = driver
            .inspect::<ScriptedDriver>(SCRIPTED_DEVICE)
            .unwrap()
            .writes();
        (snapshot, writes)
    };
    assert_eq!(run(), run());
}

/// A standby assembling the same model and applying a checkpoint taken
/// mid-join — group A's first retry still proving inside `vent_ticks`,
/// group B mid-rotation — continues the scripted run identically to
/// the active: staging positions, timers, and run-hours included.
#[test]
fn checkpointed_standby_continues_the_run_identically() {
    let model = model(BLOWER_GROUP);
    let driver_a = build_driver(&model);
    let mut active = build_executor(&model, &driver_a);
    for _ in 0..18 {
        scan(&mut active, &driver_a);
    }
    // The checkpoint lands while group A's unit 1 sits in its unproven
    // join window.
    assert!(boolean(&driver_a, VENT_A1));
    assert!(boolean(&driver_a, TRANSITION_A));
    let checkpoint = active.checkpoint();

    let driver_b = build_driver(&model);
    let mut standby = build_executor(&model, &driver_b);
    standby.apply(&checkpoint).unwrap();

    for _ in 0..37 {
        scan(&mut active, &driver_a);
        scan(&mut standby, &driver_b);
    }

    assert_eq!(
        serde_json::to_string(&active.snapshot()).unwrap(),
        serde_json::to_string(&standby.snapshot()).unwrap()
    );
    let writes_a = driver_a
        .inspect::<ScriptedDriver>(SCRIPTED_DEVICE)
        .unwrap()
        .writes();
    let writes_b = driver_b
        .inspect::<ScriptedDriver>(SCRIPTED_DEVICE)
        .unwrap()
        .writes();
    assert_eq!(writes_a[writes_a.len() - writes_b.len()..], writes_b[..]);
}

/// An indexed blower family missing a member fails assembly with
/// [`AssemblyError::UnboundPort`] naming the port.
#[test]
fn a_gap_in_the_indexed_blower_family_fails_assembly_naming_the_port() {
    let mut document: serde_json::Value = serde_json::from_str(BLOWER_GROUP).unwrap();
    let connections = document["connections"].as_array_mut().unwrap();
    connections.retain(|connection| {
        connection["from"]["port"]["name"] != "vent_3"
            || connection["from"]["port"]["component"] != 10
    });
    let model = model(&document.to_string());
    let driver = build_driver(&model);
    match assemble(&model, &registry(), &driver).err().unwrap() {
        AssemblyError::UnboundPort { component, port } => {
            assert_eq!(component.0, 10);
            assert_eq!(port, "vent_3");
        }
        other => panic!("expected UnboundPort, got {other:?}"),
    }
}
