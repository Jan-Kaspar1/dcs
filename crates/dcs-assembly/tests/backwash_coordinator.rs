//! Integration tests for the `backwash-coordinator` kind: the
//! checked-in fixture instantiates two coordinators — component 10, a
//! three-filter bank under FIFO ordering with no `reorder` surface, and
//! component 11, a two-filter bank under operator-managed ordering with
//! `reorder` bound to the writable internal `In` point the receipted
//! command path serves. A scripted deterministic run exercises the
//! exclusive grant, queue order and `position_i`/`queued`/`active`
//! reporting, permissive gating including the non-`Good` fail-safe
//! reading, `resource_blocked` timing, release-on-request-drop handing
//! the grant down the queue, and the standing reorder instruction
//! selecting the managed bank's grant; identical runs produce identical
//! snapshots and write journals, and a checkpointed standby continues
//! the run identically.

use dcs_assembly::{
    AssemblyError, BuildError, ComponentRegistry, DriverRegistry, FanoutDriver, assemble,
    resolve_drivers,
};
use dcs_blocks::{BackwashCoordinator, CoordinatorOutputs, FilterIo, PermissiveInputs};
use dcs_core::{Command, CommandOutcome, IoDriver, PointId, Value, ValueKind};
use dcs_model::{DeviceId, PlantModel};
use dcs_runtime::{Component, Executor};
use dcs_sim::ScriptedDriver;

/// The fixture: one `sim-scripted` device replaying the request and
/// permissive scripts for both banks; `backwash-coordinator` component
/// 10 the three-filter FIFO bank (queued filters keep filtering) and
/// component 11 the two-filter operator-managed bank (queued filters
/// offline with standby cover) whose `reorder` port binds the writable
/// internal point 30.
const BACKWASH: &str = include_str!("../fixtures/backwash_coordinator.json");

const REORDER: PointId = PointId(30);
const GRANT_1: PointId = PointId(40);
const GRANT_2: PointId = PointId(41);
const GRANT_3: PointId = PointId(42);
const POSITION_1: PointId = PointId(50);
const POSITION_2: PointId = PointId(51);
const POSITION_3: PointId = PointId(52);
const ACTIVE_A: PointId = PointId(53);
const QUEUED_A: PointId = PointId(54);
const BLOCKED_A: PointId = PointId(55);
const GRANT_B1: PointId = PointId(60);
const GRANT_B2: PointId = PointId(61);
const POSITION_B1: PointId = PointId(70);
const POSITION_B2: PointId = PointId(71);
const ACTIVE_B: PointId = PointId(72);
const QUEUED_B: PointId = PointId(73);
const BLOCKED_B: PointId = PointId(74);
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
/// mirroring the controller registry's indexed-port discovery.
fn registry() -> ComponentRegistry {
    ComponentRegistry::new().with(BackwashCoordinator::KIND, |spec| {
        let mut indices = std::collections::BTreeSet::new();
        for prefix in ["request_", "grant_", "position_"] {
            indices.extend(spec.ports.keys().filter_map(|name| {
                name.strip_prefix(prefix)
                    .and_then(|suffix| suffix.parse::<usize>().ok())
            }));
        }
        let count = indices.iter().next_back().copied().unwrap_or(0);
        let mut filters = Vec::with_capacity(count);
        for index in 1..=count {
            filters.push(FilterIo {
                request: spec.require(&format!("request_{index}"))?,
                grant: spec.require(&format!("grant_{index}"))?,
                position: spec.require(&format!("position_{index}"))?,
            });
        }
        boxed(BackwashCoordinator::from_parameters(
            spec.name.as_str(),
            PermissiveInputs {
                supply_ok: spec.require("supply_ok")?,
                waste_ok: spec.require("waste_ok")?,
                flow_ok: spec.require("flow_ok")?,
            },
            spec.get("reorder"),
            filters,
            CoordinatorOutputs {
                active: spec.require("active")?,
                queued: spec.require("queued")?,
                resource_blocked: spec.require("resource_blocked")?,
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

fn boolean(driver: &FanoutDriver, point: PointId) -> bool {
    match driver.read(point).unwrap().value {
        Value::Bool(value) => value,
        value => panic!("point {point:?}: expected Bool, got {value:?}"),
    }
}

/// The scripted run. Each scan reads the scripted entries the driver's
/// last `step` made current: the tick-0 values are already in effect at
/// scan 1.
fn scan(executor: &mut Executor, driver: &FanoutDriver) {
    executor.scan().unwrap();
    driver.step(0.1).unwrap();
}

/// A receipted reorder instruction on the writable internal point.
fn reorder(executor: &mut Executor, value: i64) {
    let receipt = executor.submit_command(Command::WriteValue {
        point: REORDER,
        kind: ValueKind::Int,
        value: Value::Int(value),
    });
    assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
}

#[test]
fn backwash_coordinator_fixture_assembles_through_the_registry() {
    let model = model(BACKWASH);
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

/// The FIFO bank's grant path: filter 1 takes the grant and holds it
/// through the supply outage — the `false` entry and the non-`Good`
/// entry reading identically as not-OK — while `resource_blocked`
/// asserts over the standing queue; its request drop releases the grant
/// to the queue's head the same scan.
#[test]
fn scripted_run_demonstrates_fifo_arbitration_gating_and_handover() {
    let model = model(BACKWASH);
    let driver = build_driver(&model);
    let mut executor = build_executor(&model, &driver);

    // Scan 1: filter 1's tick-0 request takes the grant and reports no
    // queue slot — the holder has left the queue.
    scan(&mut executor, &driver);
    assert!(boolean(&driver, GRANT_1));
    assert!(!boolean(&driver, GRANT_2));
    assert!(!boolean(&driver, GRANT_3));
    assert_eq!(int(&driver, ACTIVE_A), 1);
    assert_eq!(int(&driver, QUEUED_A), 0);
    assert_eq!(int(&driver, POSITION_1), 0);
    assert!(!boolean(&driver, BLOCKED_A));

    // Scan 3: filter 3's tick-2 request queues behind the held grant.
    scan(&mut executor, &driver);
    scan(&mut executor, &driver);
    assert_eq!(int(&driver, POSITION_3), 1);
    assert_eq!(int(&driver, QUEUED_A), 1);
    // Scan 4: filter 2's tick-3 request queues behind filter 3 — FIFO
    // arrival order.
    scan(&mut executor, &driver);
    assert_eq!(int(&driver, POSITION_2), 2);
    assert_eq!(int(&driver, QUEUED_A), 2);

    // Scans 6-11: the supply permissive is out — `false` from tick 5,
    // `Uncertain` from tick 8 — and reads as not-OK throughout: the
    // grant output falls while the holding stands (`active` still
    // reports 1) and `resource_blocked` asserts over the non-empty
    // queue.
    scan(&mut executor, &driver);
    for scan_number in 6..=11 {
        scan(&mut executor, &driver);
        assert!(
            !boolean(&driver, GRANT_1),
            "scan {scan_number}: grant must stay low while a permissive fails"
        );
        assert_eq!(int(&driver, ACTIVE_A), 1);
        assert!(boolean(&driver, BLOCKED_A));
    }

    // Scan 12: the permissive's tick-11 Good restore re-asserts the
    // held grant without re-queuing, and `resource_blocked` clears.
    scan(&mut executor, &driver);
    assert!(boolean(&driver, GRANT_1));
    assert!(!boolean(&driver, BLOCKED_A));

    // Scan 14: filter 1's tick-13 request drop releases the grant and
    // hands it to the queue's head — filter 3, queued ahead of filter
    // 2 — the same scan.
    scan(&mut executor, &driver);
    scan(&mut executor, &driver);
    assert!(!boolean(&driver, GRANT_1));
    assert!(boolean(&driver, GRANT_3));
    assert!(!boolean(&driver, GRANT_2));
    assert_eq!(int(&driver, ACTIVE_A), 3);
    assert_eq!(int(&driver, QUEUED_A), 1);
    assert_eq!(int(&driver, POSITION_3), 0);
    assert_eq!(int(&driver, POSITION_2), 1);

    // Scan 17: filter 2's tick-16 request drop empties the queue while
    // filter 3's grant holds.
    for _ in 0..3 {
        scan(&mut executor, &driver);
    }
    assert!(boolean(&driver, GRANT_3));
    assert_eq!(int(&driver, QUEUED_A), 0);
    assert_eq!(int(&driver, POSITION_2), 0);

    // Scan 20: filter 3's tick-19 request drop releases the last grant
    // — the bank idles.
    for _ in 0..3 {
        scan(&mut executor, &driver);
    }
    assert!(!boolean(&driver, GRANT_3));
    assert_eq!(int(&driver, ACTIVE_A), 0);
}

/// The operator-managed bank: requests queue in assertion order but no
/// grant issues until the standing `reorder` instruction — written
/// through the receipted command path — names a queued member; the
/// selection holds while the granted request stands, and clearing the
/// instruction does not release it.
#[test]
fn operator_managed_bank_grants_only_the_receipted_selection() {
    let model = model(BACKWASH);
    let driver = build_driver(&model);
    let mut executor = build_executor(&model, &driver);

    // Scans 1-4: both requests stand queued — filter b1 at tick 0,
    // filter b2 at tick 1 — and the idle instruction grants nothing.
    for _ in 0..4 {
        scan(&mut executor, &driver);
    }
    assert!(!boolean(&driver, GRANT_B1));
    assert!(!boolean(&driver, GRANT_B2));
    assert_eq!(int(&driver, ACTIVE_B), 0);
    assert_eq!(int(&driver, QUEUED_B), 2);
    assert_eq!(int(&driver, POSITION_B1), 1);
    assert_eq!(int(&driver, POSITION_B2), 2);
    assert!(!boolean(&driver, BLOCKED_B));

    // The receipted write selects filter b2: at the next scan it has
    // moved to the head and taken the grant.
    reorder(&mut executor, 2);
    scan(&mut executor, &driver);
    assert!(boolean(&driver, GRANT_B2));
    assert!(!boolean(&driver, GRANT_B1));
    assert_eq!(int(&driver, ACTIVE_B), 2);
    assert_eq!(int(&driver, POSITION_B2), 0);
    assert_eq!(int(&driver, POSITION_B1), 1);
    assert_eq!(int(&driver, QUEUED_B), 1);

    // Clearing the instruction leaves the held grant standing — the
    // holder's request still asserts.
    reorder(&mut executor, 0);
    for _ in 0..9 {
        scan(&mut executor, &driver);
        assert!(boolean(&driver, GRANT_B2));
        assert_eq!(int(&driver, ACTIVE_B), 2);
    }

    // Scan 16: filter b1's tick-15 request drop drains the queue.
    for _ in 0..2 {
        scan(&mut executor, &driver);
    }
    assert_eq!(int(&driver, QUEUED_B), 0);
    assert!(boolean(&driver, GRANT_B2));

    // Scan 19: filter b2's tick-18 request drop releases the grant —
    // the managed bank idles.
    for _ in 0..3 {
        scan(&mut executor, &driver);
    }
    assert!(!boolean(&driver, GRANT_B2));
    assert_eq!(int(&driver, ACTIVE_B), 0);
}

#[test]
fn identical_scripted_runs_produce_identical_snapshots_and_journals() {
    let run = || {
        let model = model(BACKWASH);
        let driver = build_driver(&model);
        let mut executor = build_executor(&model, &driver);
        for scan_number in 1..=24 {
            if scan_number == 5 {
                reorder(&mut executor, 2);
            } else if scan_number == 7 {
                reorder(&mut executor, 0);
            }
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
/// checkpoint — the held grant, the ordered queue, and the standing
/// reorder instruction included — continues the scripted run
/// identically to the active.
#[test]
fn checkpointed_standby_continues_the_run_identically() {
    let model = model(BACKWASH);
    let driver_a = build_driver(&model);
    let mut active = build_executor(&model, &driver_a);
    for scan_number in 1..=10 {
        if scan_number == 5 {
            reorder(&mut active, 2);
        }
        scan(&mut active, &driver_a);
    }
    let checkpoint = active.checkpoint();

    let driver_b = build_driver(&model);
    let mut standby = build_executor(&model, &driver_b);
    standby.apply(&checkpoint).unwrap();

    for _ in 0..12 {
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

/// An indexed filter family missing a member fails assembly with
/// [`AssemblyError::UnboundPort`] naming the port.
#[test]
fn a_gap_in_the_indexed_filter_family_fails_assembly_naming_the_port() {
    let mut document: serde_json::Value = serde_json::from_str(BACKWASH).unwrap();
    let connections = document["connections"].as_array_mut().unwrap();
    connections.retain(|connection| {
        connection["to"]["port"]["name"] != "request_2"
            || connection["to"]["port"]["component"] != 10
    });
    let model = model(&document.to_string());
    let driver = build_driver(&model);
    match assemble(&model, &registry(), &driver).err().unwrap() {
        AssemblyError::UnboundPort { component, port } => {
            assert_eq!(component.0, 10);
            assert_eq!(port, "request_2");
        }
        other => panic!("expected UnboundPort, got {other:?}"),
    }
}
