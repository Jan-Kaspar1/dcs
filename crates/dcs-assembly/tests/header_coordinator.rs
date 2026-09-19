//! Integration tests for the `header-coordinator` kind: the checked-in
//! fixture instantiates three aeration banks — one per declared
//! strategy — fed by one scripted input set. A scripted deterministic
//! run exercises constant-pressure holding, the most-open-valve
//! set-point walk to the declared band, direct-airflow demand
//! summation behind the declared floor, the bounded set-point
//! reporting through `at_bound`, the capped pulse admission reporting
//! `pulse_blocked`, and the documented non-`Good` rules; identical
//! runs produce identical snapshots and write journals, and a
//! checkpointed standby continues the run identically.

use dcs_assembly::{
    AssemblyError, BuildError, ComponentRegistry, DriverRegistry, FanoutDriver, assemble,
    resolve_drivers,
};
use dcs_blocks::{HeaderCoordinator, HeaderOutputs, ZoneIo};
use dcs_core::{IoDriver, PointId, Value};
use dcs_model::{DeviceId, PlantModel};
use dcs_runtime::{Component, Executor};
use dcs_sim::ScriptedDriver;

/// The fixture: one `sim-scripted` device replaying the bank inputs —
/// per-zone valve positions, airflow demands, and pulse requests plus
/// the header pressure — and three `header-coordinator` instances:
/// `header-coordinator:10` holding constant pressure over two zones,
/// `header-coordinator:11` walking the set-point on the most-open
/// valve of three, and `header-coordinator:12` summing two zones'
/// direct-airflow demands.
const HEADER_COORDINATOR: &str = include_str!("../fixtures/header_coordinator.json");

const GRANT_A1: PointId = PointId(40);
const GRANT_A2: PointId = PointId(41);
const SP_A: PointId = PointId(42);
const DEMAND_A: PointId = PointId(43);
const MOST_A: PointId = PointId(44);
const BOUND_A: PointId = PointId(45);
const PBLOCKED_A: PointId = PointId(46);
const SP_B: PointId = PointId(53);
const DEMAND_B: PointId = PointId(54);
const MOST_B: PointId = PointId(55);
const BOUND_B: PointId = PointId(56);
const GRANT_C1: PointId = PointId(60);
const GRANT_C2: PointId = PointId(61);
const SP_C: PointId = PointId(62);
const DEMAND_C: PointId = PointId(63);
const MOST_C: PointId = PointId(64);
const BOUND_C: PointId = PointId(65);
const PBLOCKED_C: PointId = PointId(66);
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

/// The `dcs-blocks` registration for the kind the fixture uses,
/// mirroring the controller registry's indexed-family discovery.
fn registry() -> ComponentRegistry {
    ComponentRegistry::new().with(HeaderCoordinator::KIND, |spec| {
        let zones = spec
            .indexed_families(["valve_pos_", "airflow_", "pulsing_", "pulse_grant_"])?
            .into_iter()
            .map(|[valve_pos, airflow, pulsing, pulse_grant]| ZoneIo {
                valve_pos,
                airflow,
                pulsing,
                pulse_grant,
            })
            .collect();
        boxed(HeaderCoordinator::from_parameters(
            spec.name.as_str(),
            spec.require("pressure")?,
            zones,
            HeaderOutputs {
                pressure_sp: spec.require("pressure_sp")?,
                blower_demand: spec.require("blower_demand")?,
                most_open: spec.require("most_open")?,
                at_bound: spec.require("at_bound")?,
                pulse_blocked: spec.require("pulse_blocked")?,
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

fn float(driver: &FanoutDriver, point: PointId) -> f64 {
    match driver.read(point).unwrap().value {
        Value::Float(value) => value,
        value => panic!("point {point:?}: expected Float, got {value:?}"),
    }
}

fn int(driver: &FanoutDriver, point: PointId) -> i64 {
    match driver.read(point).unwrap().value {
        Value::Int(value) => value,
        value => panic!("point {point:?}: expected Int, got {value:?}"),
    }
}

fn boolean(driver: &FanoutDriver, point: PointId) -> bool {
    match driver.read(point).unwrap().value {
        Value::Bool(value) => value,
        value => panic!("point {point:?}: expected Bool, got {value:?}"),
    }
}

/// The scripted run. Each scan reads the scripted entries the driver's
/// last `step` made current: the tick-0 values are already in effect
/// at scan 1, and a tick-`t` entry first reaches the scan at `t + 1`.
fn scan(executor: &mut Executor, driver: &FanoutDriver) {
    executor.scan();
    driver.step(0.1).unwrap();
}

#[test]
fn header_coordinator_fixture_assembles_through_the_registry() {
    let model = model(HEADER_COORDINATOR);
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

/// The scripted run's per-bank behavior: bank A holds the declared
/// constant pressure while its zone demands ride above and then below
/// the declared floor, and its pulse cap refuses the second requester
/// until the holder's request drops; bank B walks its set-point on the
/// most-open valve — down by the distance above the band at each
/// three-scan interval until the pressure floor, holding the emitted
/// set-point while the pressure read is untrusted, climbing to the
/// declared maximum while the most-open valve sits under the band, and
/// re-selecting the most-open zone when the leader's position goes
/// untrusted; bank C sums its zone demands behind the floor and the
/// last-good hold, and its cap refuses the second pulse request.
#[test]
fn scripted_run_demonstrates_the_three_strategies() {
    let model = model(HEADER_COORDINATOR);
    let driver = build_driver(&model);
    let mut executor = build_executor(&model, &driver);

    // Scans 1-4: bank A holds the declared 12.0 set-point over the
    // 5.5 summed demand; bank B starts its walk at the 10.0 hold —
    // the first move is due at the third scan — with zone 3 most-open
    // at 97; bank C's 2.7 summed demand floors at the declared 3.0.
    for _ in 0..4 {
        scan(&mut executor, &driver);
        assert_eq!(float(&driver, SP_A), 12.0);
        assert_eq!(float(&driver, DEMAND_A), 5.5);
        assert_eq!(int(&driver, MOST_A), 2);
        assert!(!boolean(&driver, BOUND_A));
        assert_eq!(float(&driver, DEMAND_B), 4.5);
        assert_eq!(int(&driver, MOST_B), 3);
        assert_eq!(float(&driver, SP_C), 8.0);
        assert_eq!(float(&driver, DEMAND_C), 3.0);
        assert!(boolean(&driver, BOUND_C));
        assert_eq!(int(&driver, MOST_C), 2);
    }
    assert_eq!(float(&driver, SP_B), 8.0);

    // Scan 5 reads pulsing-a1's true entry: bank A's single pulse
    // slot grants to zone 1.
    scan(&mut executor, &driver);
    assert!(boolean(&driver, GRANT_A1));
    assert!(!boolean(&driver, GRANT_A2));
    assert!(!boolean(&driver, PBLOCKED_A));
    assert!(boolean(&driver, GRANT_C1));
    assert!(!boolean(&driver, PBLOCKED_C));

    // Scan 6: bank B's second interval moves the set-point to 6.0.
    scan(&mut executor, &driver);
    assert_eq!(float(&driver, SP_B), 6.0);

    // Scans 7-8: bank A's second requester stands refused — the held
    // grant does not yield to the lower-indexed request — and bank
    // C's second requester refuses the same way.
    for _ in 0..2 {
        scan(&mut executor, &driver);
        assert!(!boolean(&driver, GRANT_A2));
        assert!(boolean(&driver, PBLOCKED_A));
        assert!(!boolean(&driver, GRANT_C2));
        assert!(boolean(&driver, PBLOCKED_C));
    }

    // Scan 9: bank B's third interval clamps the walk at the 4.0
    // floor — `at_bound` reports the set-point resting at the bound —
    // and bank C's summed demand rises over the floor to 9.0.
    scan(&mut executor, &driver);
    assert_eq!(float(&driver, SP_B), 4.0);
    assert!(boolean(&driver, BOUND_B));
    assert_eq!(float(&driver, DEMAND_C), 9.0);
    assert!(!boolean(&driver, BOUND_C));

    // Scan 11 reads pulsing-a1's drop: the held grant releases and
    // zone 2's standing request admits the same scan.
    for _ in 0..2 {
        scan(&mut executor, &driver);
    }
    assert!(!boolean(&driver, GRANT_A1));
    assert!(boolean(&driver, GRANT_A2));
    assert!(!boolean(&driver, PBLOCKED_A));

    // Scan 15: bank A's summed demand falls to 3.0 — under the 4.0
    // floor, so the emitted demand holds at the floor and `at_bound`
    // reports it. Bank B's most-open valve entered the band at scan
    // 13, so the walk holds the floored set-point.
    for _ in 0..4 {
        scan(&mut executor, &driver);
    }
    assert_eq!(float(&driver, DEMAND_A), 4.0);
    assert!(boolean(&driver, BOUND_A));
    assert_eq!(float(&driver, SP_B), 4.0);

    // Scan 16: bank C's first requester drops — its grant releases
    // and the waiting second request admits.
    scan(&mut executor, &driver);
    assert!(!boolean(&driver, GRANT_C1));
    assert!(boolean(&driver, GRANT_C2));
    assert!(!boolean(&driver, PBLOCKED_C));

    // Scans 17-18: bank B reads the valve below the band, but the
    // scan-18 evaluation lands inside the untrusted-pressure window —
    // the emitted set-point holds rather than stepping.
    for _ in 0..2 {
        scan(&mut executor, &driver);
        assert_eq!(float(&driver, SP_B), 4.0);
    }
    // Bank C's untrusted zone-2 demand holds its last trusted 5.0 —
    // the sum still reads 9.0 — then recovers to 6.0.
    assert_eq!(float(&driver, DEMAND_C), 10.0);

    // Scan 21: the pressure read is trusted again and the most-open
    // valve sits fifteen under the band's lower edge — the walk steps
    // the set-point up and clamps at the 16.0 maximum.
    for _ in 0..3 {
        scan(&mut executor, &driver);
    }
    assert_eq!(float(&driver, SP_B), 16.0);

    // Scan 25: zone 3's position goes untrusted — the most-open
    // identity falls to zone 2, still below the band, and the walk
    // holds at the declared maximum.
    for _ in 0..4 {
        scan(&mut executor, &driver);
    }
    assert_eq!(int(&driver, MOST_B), 2);
    assert_eq!(float(&driver, SP_B), 16.0);
    assert!(boolean(&driver, BOUND_B));

    // Scan 31: zone 3's position returns trusted inside the band and
    // reclaims the most-open identity; the walk holds at the bound.
    for _ in 0..6 {
        scan(&mut executor, &driver);
    }
    assert_eq!(int(&driver, MOST_B), 3);
    assert_eq!(float(&driver, SP_B), 16.0);
}

#[test]
fn identical_scripted_runs_produce_identical_snapshots_and_journals() {
    let run = || {
        let model = model(HEADER_COORDINATOR);
        let driver = build_driver(&model);
        let mut executor = build_executor(&model, &driver);
        for _ in 0..35 {
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

/// A standby assembling the same model and applying a mid-run
/// checkpoint — the walking set-point, the adjustment timer, the held
/// pulse grants, and the most-open identity included — continues the
/// scripted run identically to the active.
#[test]
fn checkpointed_standby_continues_the_run_identically() {
    let model = model(HEADER_COORDINATOR);
    let driver_a = build_driver(&model);
    let mut active = build_executor(&model, &driver_a);
    for _ in 0..12 {
        scan(&mut active, &driver_a);
    }
    let checkpoint = active.checkpoint();

    let driver_b = build_driver(&model);
    let mut standby = build_executor(&model, &driver_b);
    standby.apply(&checkpoint).unwrap();

    for _ in 0..23 {
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

/// An indexed zone family missing a member fails assembly with
/// [`AssemblyError::UnboundPort`] naming the port.
#[test]
fn a_gap_in_the_indexed_zone_family_fails_assembly_naming_the_port() {
    let mut document: serde_json::Value = serde_json::from_str(HEADER_COORDINATOR).unwrap();
    let connections = document["connections"].as_array_mut().unwrap();
    connections.retain(|connection| {
        connection["to"]["port"]["name"] != "airflow_2"
            || connection["to"]["port"]["component"] != 10
    });
    let model = model(&document.to_string());
    let driver = build_driver(&model);
    match assemble(&model, &registry(), &driver).err().unwrap() {
        AssemblyError::UnboundPort { component, port } => {
            assert_eq!(component.0, 10);
            assert_eq!(port, "airflow_2");
        }
        other => panic!("expected UnboundPort, got {other:?}"),
    }
}
