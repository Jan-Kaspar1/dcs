//! Integration tests for the managed latching alarm kinds — the
//! `managed_alarms` fixture composes three instances through the
//! model-driven registry: a `managed-latching-alarm` on the scripted
//! wet-well level and a `managed-bool-latching-alarm` on the pump fault
//! flag, both with the full `shelve`/`oos`/`suppress` surface bound to
//! writable internal points, and a never-shelvable
//! `managed-bool-latching-alarm` on station power whose bound `shelve`
//! point is *not* writable — the documented `NotWritable` rejection
//! path — and whose `suppress` port is unbound.
//!
//! Scripted runs exercise the operator commands riding the receipted
//! `WriteValue` path — shelving inside and past the declared bound, the
//! re-shelve-requires-cycle rule, acknowledgment mid-shelve, a trip
//! mid-OOS, suppression withholding the latch and releasing fresh — a
//! checkpointed standby continuing a mid-shelve countdown identically,
//! and identical runs producing identical snapshots.

use dcs_assembly::{AssemblyError, BuildError, ComponentRegistry, assemble, sim_driver};
use dcs_blocks::{ManagedAlarmIo, ManagedBoolLatchingAlarm, ManagedLatchingAlarm};
use dcs_core::{Command, CommandError, CommandOutcome, IoDriver, PointId, Value};
use dcs_model::PlantModel;
use dcs_runtime::{Component, Executor};
use dcs_sim::SimDriver;

/// The fixture: one `sim` device serving the level, fault, and power
/// channels; writable internal `In` points for the operator `ack`,
/// `shelve`, `oos`, and `suppress` commands; a bound-but-unwritable
/// `shelve` point on the never-shelvable instance; and internal `Out`
/// carriers for every status output (the links carry quality, which a
/// field loopback's write journal would drop).
const MANAGED_ALARMS: &str = include_str!("../fixtures/managed_alarms.json");

// Component 1 — `managed-latching-alarm` on the level, bound 3.
const LEVEL: PointId = PointId(10);
const LEVEL_SHELVE: PointId = PointId(12);
const LEVEL_OOS: PointId = PointId(13);
const LEVEL_SUPPRESS: PointId = PointId(14);
const LEVEL_ALARM: PointId = PointId(20);
const LEVEL_UNACK: PointId = PointId(21);
const LEVEL_SHELVED: PointId = PointId(22);
const LEVEL_SUPPRESSED: PointId = PointId(23);
const LEVEL_OOS_FLAG: PointId = PointId(24);

// Component 2 — `managed-bool-latching-alarm` on the fault, bound 3.
const FAULT: PointId = PointId(15);
const FAULT_ACK: PointId = PointId(16);
const FAULT_SHELVE: PointId = PointId(17);
const FAULT_OOS: PointId = PointId(18);
const FAULT_SUPPRESS: PointId = PointId(19);
const FAULT_ALARM: PointId = PointId(30);
const FAULT_UNACK: PointId = PointId(31);
const FAULT_SHELVED: PointId = PointId(32);
const FAULT_SUPPRESSED: PointId = PointId(33);
const FAULT_OOS_FLAG: PointId = PointId(34);

// Component 3 — `managed-bool-latching-alarm` on power, bound 0,
// `shelve` bound to a point that is not writable, `suppress` unbound.
const POWER: PointId = PointId(25);
const POWER_SHELVE: PointId = PointId(27);
const POWER_OOS: PointId = PointId(28);
const POWER_ALARM: PointId = PointId(40);
const POWER_SHELVED: PointId = PointId(42);
const POWER_SUPPRESSED: PointId = PointId(43);
const POWER_OOS_FLAG: PointId = PointId(44);

/// The `dcs-blocks` registration for the fixture's kinds, mirroring
/// the controller registry's port binding.
fn registry() -> ComponentRegistry {
    ComponentRegistry::new()
        .with(ManagedLatchingAlarm::KIND, |spec| {
            boxed(ManagedLatchingAlarm::from_parameters(
                spec.name.as_str(),
                managed_io(spec)?,
                spec.parameters,
            ))
        })
        .with(ManagedBoolLatchingAlarm::KIND, |spec| {
            boxed(ManagedBoolLatchingAlarm::from_parameters(
                spec.name.as_str(),
                managed_io(spec)?,
                spec.parameters,
            ))
        })
}

/// The shared port binding both managed kinds' constructors take.
fn managed_io(spec: &dcs_assembly::ComponentSpec<'_>) -> Result<ManagedAlarmIo, BuildError> {
    Ok(ManagedAlarmIo {
        input: spec.require("in")?,
        ack: spec.require("ack")?,
        shelve: spec.get("shelve"),
        oos: spec.get("oos"),
        suppress: spec.get("suppress"),
        alarm: spec.require("alarm")?,
        unacknowledged: spec.require("unacknowledged")?,
        shelved: spec.require("shelved")?,
        suppressed: spec.require("suppressed")?,
        out_of_service: spec.require("out_of_service")?,
    })
}

fn boxed<C, E>(result: Result<C, E>) -> Result<Box<dyn Component>, BuildError>
where
    C: Component + 'static,
    E: std::error::Error + 'static,
{
    result
        .map(|component| Box::new(component) as Box<dyn Component>)
        .map_err(BuildError::other)
}

fn model() -> PlantModel {
    PlantModel::load(MANAGED_ALARMS).unwrap()
}

fn build_executor<'d>(model: &PlantModel, driver: &'d SimDriver) -> Executor<'d> {
    assemble(model, &registry(), driver).unwrap()
}

/// The image flag the last scan left on `point`.
fn flag(executor: &Executor, point: PointId) -> bool {
    executor.sample(point).map(|sample| sample.value) == Some(Value::Bool(true))
}

/// An operator `WriteValue` on a writable internal point — the
/// receipted path decisions 49/72 prescribe.
fn write_value(point: PointId, value: Value) -> Command {
    Command::WriteValue {
        point,
        kind: value.kind(),
        value,
    }
}

#[test]
fn managed_alarms_fixture_assembles_through_the_registry() {
    let model = model();
    let driver = sim_driver(&model).unwrap();
    let mut executor = build_executor(&model, &driver);
    executor.scan().unwrap();

    let snapshot = executor.snapshot();
    assert!(
        snapshot
            .components
            .iter()
            .all(|component| component.step_errors == 0)
    );
    let kinds: Vec<&str> = snapshot
        .descriptors
        .iter()
        .map(|descriptor| descriptor.kind.as_str())
        .collect();
    assert_eq!(
        kinds,
        [
            ManagedLatchingAlarm::KIND,
            ManagedBoolLatchingAlarm::KIND,
            ManagedBoolLatchingAlarm::KIND,
        ]
    );
    // The unmanaged instance's descriptor exposes no `suppress` port —
    // the unbound surface declares nothing.
    let power = &snapshot.descriptors[2];
    assert!(power.ports.iter().all(|port| port.name != "suppress"));
    assert!(power.ports.iter().any(|port| port.name == "shelve"));
    // And every status port reports the Status role the pane joins.
    for descriptor in &snapshot.descriptors {
        for name in ["shelved", "suppressed", "out_of_service"] {
            assert_eq!(
                descriptor
                    .ports
                    .iter()
                    .find(|port| port.name == name)
                    .and_then(|port| port.role),
                Some(dcs_core::PortRole::Status),
                "{}.{name}",
                descriptor.name
            );
        }
    }
}

/// The scripted managed lifecycle on the Bool instance: shelve inside
/// and past the bound, an ack mid-shelve, a trip mid-OOS, and a
/// suppression window releasing fresh — every command a receipted
/// `WriteValue` on a writable internal point.
#[test]
fn the_operator_commands_drive_the_managed_lifecycle() {
    let model = model();
    let driver = sim_driver(&model).unwrap();
    let mut executor = build_executor(&model, &driver);

    // Trip the fault alarm and acknowledge nothing: the record stands.
    driver.write(FAULT, Value::Bool(true)).unwrap();
    executor.scan().unwrap();
    assert!(flag(&executor, FAULT_ALARM));
    assert!(flag(&executor, FAULT_UNACK));

    // Shelve it: the command is receipted, applied at the next scan,
    // and the flag asserts while the record stands untouched.
    let receipt = executor.submit_command(write_value(FAULT_SHELVE, Value::Bool(true)));
    assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
    executor.scan().unwrap();
    assert!(flag(&executor, FAULT_SHELVED));
    assert!(flag(&executor, FAULT_ALARM));
    assert!(flag(&executor, FAULT_UNACK));

    // An ack mid-shelve clears the latch normally.
    executor.submit_command(write_value(FAULT_ACK, Value::Bool(true)));
    executor.scan().unwrap();
    assert!(flag(&executor, FAULT_SHELVED));
    assert!(flag(&executor, FAULT_ALARM));
    assert!(!flag(&executor, FAULT_UNACK));
    executor.submit_command(write_value(FAULT_ACK, Value::Bool(false)));

    // The bound is three scans — the request's asserting scan counts
    // as the first: one more shelved scan, then expiry drops the flag
    // while the request still stands.
    executor.scan().unwrap();
    assert!(flag(&executor, FAULT_SHELVED), "scan 3 of the bound");
    executor.scan().unwrap();
    assert!(!flag(&executor, FAULT_SHELVED), "bound 3 expired");
    executor.scan().unwrap();
    assert!(
        !flag(&executor, FAULT_SHELVED),
        "the standing request cannot re-arm"
    );

    // Cycling the request through false re-arms a fresh bound.
    executor.submit_command(write_value(FAULT_SHELVE, Value::Bool(false)));
    executor.scan().unwrap();
    executor.submit_command(write_value(FAULT_SHELVE, Value::Bool(true)));
    executor.scan().unwrap();
    assert!(flag(&executor, FAULT_SHELVED), "cycled request re-shelves");
    executor.submit_command(write_value(FAULT_SHELVE, Value::Bool(false)));
    executor.scan().unwrap();
    assert!(!flag(&executor, FAULT_SHELVED));

    // Out of service: the flag follows the command level, manual in
    // both directions, and a trip arriving mid-OOS evaluates and
    // latches normally.
    driver.write(FAULT, Value::Bool(false)).unwrap();
    executor.scan().unwrap();
    assert!(!flag(&executor, FAULT_ALARM));
    executor.submit_command(write_value(FAULT_OOS, Value::Bool(true)));
    executor.scan().unwrap();
    assert!(flag(&executor, FAULT_OOS_FLAG));
    driver.write(FAULT, Value::Bool(true)).unwrap();
    executor.scan().unwrap();
    assert!(flag(&executor, FAULT_OOS_FLAG));
    assert!(flag(&executor, FAULT_ALARM), "the mid-OOS trip reports");
    assert!(flag(&executor, FAULT_UNACK), "the mid-OOS trip latches");
    executor.scan().unwrap();
    assert!(flag(&executor, FAULT_OOS_FLAG), "no automatic return");
    executor.submit_command(write_value(FAULT_OOS, Value::Bool(false)));
    executor.submit_command(write_value(FAULT_ACK, Value::Bool(true)));
    executor.scan().unwrap();
    assert!(!flag(&executor, FAULT_OOS_FLAG));
    assert!(!flag(&executor, FAULT_UNACK));

    // Suppression: clear the trip first, then suppress and trip under
    // it — `alarm` reports the truth while the latch is withheld.
    executor.submit_command(write_value(FAULT_ACK, Value::Bool(false)));
    driver.write(FAULT, Value::Bool(false)).unwrap();
    executor.scan().unwrap();
    executor.submit_command(write_value(FAULT_SUPPRESS, Value::Bool(true)));
    executor.scan().unwrap();
    assert!(flag(&executor, FAULT_SUPPRESSED));
    driver.write(FAULT, Value::Bool(true)).unwrap();
    executor.scan().unwrap();
    assert!(flag(&executor, FAULT_ALARM));
    assert!(!flag(&executor, FAULT_UNACK), "annunciation withheld");

    // Release: the outlasted condition arrives as a new transition.
    executor.submit_command(write_value(FAULT_SUPPRESS, Value::Bool(false)));
    executor.scan().unwrap();
    assert!(!flag(&executor, FAULT_SUPPRESSED));
    assert!(flag(&executor, FAULT_ALARM));
    assert!(flag(&executor, FAULT_UNACK), "released fresh — latched");
}

/// The Float sibling takes the same lifecycle on the level input.
#[test]
fn the_float_sibling_shelves_and_suppresses_identically() {
    let model = model();
    let driver = sim_driver(&model).unwrap();
    let mut executor = build_executor(&model, &driver);

    // Trip high at 90+ and shelve the standing alarm.
    driver.write(LEVEL, Value::Float(95.0)).unwrap();
    executor.scan().unwrap();
    assert!(flag(&executor, LEVEL_ALARM));
    assert!(flag(&executor, LEVEL_UNACK));
    executor.submit_command(write_value(LEVEL_SHELVE, Value::Bool(true)));
    executor.scan().unwrap();
    assert!(flag(&executor, LEVEL_SHELVED));

    // Expiry at the bound — three scans, the asserting one counted —
    // while the request stands.
    executor.scan().unwrap();
    assert!(flag(&executor, LEVEL_SHELVED));
    executor.scan().unwrap();
    assert!(flag(&executor, LEVEL_SHELVED), "last scan of the bound");
    executor.scan().unwrap();
    assert!(!flag(&executor, LEVEL_SHELVED), "bound 3 expired");
    assert!(flag(&executor, LEVEL_ALARM), "the record never moved");
    assert!(flag(&executor, LEVEL_UNACK));

    // Suppress, clear the field trip, and release: nothing latches for
    // a condition that ended under suppression.
    executor.submit_command(write_value(LEVEL_SUPPRESS, Value::Bool(true)));
    driver.write(LEVEL, Value::Float(50.0)).unwrap();
    executor.scan().unwrap();
    assert!(flag(&executor, LEVEL_SUPPRESSED));
    assert!(!flag(&executor, LEVEL_UNACK), "the withheld latch cleared");
    executor.submit_command(write_value(LEVEL_SUPPRESS, Value::Bool(false)));
    executor.scan().unwrap();
    assert!(!flag(&executor, LEVEL_ALARM));
    assert!(!flag(&executor, LEVEL_UNACK), "a cleared trip stays silent");

    // Re-trip after release: a fresh transition latches.
    driver.write(LEVEL, Value::Float(95.0)).unwrap();
    executor.scan().unwrap();
    assert!(flag(&executor, LEVEL_ALARM));
    assert!(flag(&executor, LEVEL_UNACK));

    // OOS entry and manual return on the Float sibling too.
    executor.submit_command(write_value(LEVEL_OOS, Value::Bool(true)));
    executor.scan().unwrap();
    assert!(flag(&executor, LEVEL_OOS_FLAG));
    executor.submit_command(write_value(LEVEL_OOS, Value::Bool(false)));
    executor.scan().unwrap();
    assert!(!flag(&executor, LEVEL_OOS_FLAG));
}

/// The never-shelvable instance: `max_shelve_ticks` is 0 and its
/// `shelve` port binds a point the model does not mark writable — the
/// command path's `NotWritable` at submission is the stronger
/// rejection, and the flag never asserts.
#[test]
fn an_unwritable_shelve_point_rejects_not_writable_at_submission() {
    let model = model();
    let driver = sim_driver(&model).unwrap();
    let mut executor = build_executor(&model, &driver);

    let receipt = executor.submit_command(write_value(POWER_SHELVE, Value::Bool(true)));
    assert_eq!(
        receipt.outcome,
        CommandOutcome::Rejected {
            reason: CommandError::NotWritable {
                point: POWER_SHELVE
            }
        }
    );

    // The instance still runs the two-flag lifecycle — a power failure
    // trips and latches, OOS works, and `shelved`/`suppressed` report
    // the absent surfaces as standing-clear.
    driver.write(POWER, Value::Bool(true)).unwrap();
    executor.scan().unwrap();
    assert!(flag(&executor, POWER_ALARM));
    assert!(!flag(&executor, POWER_SHELVED));
    assert!(!flag(&executor, POWER_SUPPRESSED));
    executor.submit_command(write_value(POWER_OOS, Value::Bool(true)));
    executor.scan().unwrap();
    assert!(flag(&executor, POWER_OOS_FLAG));
}

/// A standby applying a mid-shelve checkpoint — the fault alarm
/// tripped, acknowledged, and shelved with one bound-scan left —
/// continues the countdown identically to the active.
#[test]
fn checkpointed_standby_mid_shelve_continues_identically() {
    let model = model();
    let driver_a = sim_driver(&model).unwrap();
    let mut active = build_executor(&model, &driver_a);

    driver_a.write(FAULT, Value::Bool(true)).unwrap();
    active.scan().unwrap();
    active.submit_command(write_value(FAULT_ACK, Value::Bool(true)));
    active.scan().unwrap();
    active.submit_command(write_value(FAULT_ACK, Value::Bool(false)));
    active.submit_command(write_value(FAULT_SHELVE, Value::Bool(true)));
    active.submit_command(write_value(FAULT_OOS, Value::Bool(true)));
    // Two of three bound scans elapse across the next two scans.
    active.scan().unwrap();
    active.scan().unwrap();
    assert!(flag(&active, FAULT_SHELVED));
    assert!(flag(&active, FAULT_OOS_FLAG));

    let checkpoint = active.checkpoint();
    let driver_b = sim_driver(&model).unwrap();
    let mut standby = build_executor(&model, &driver_b);
    standby.apply(&checkpoint).unwrap();
    assert!(flag(&standby, FAULT_SHELVED), "the timer state rode along");
    assert!(flag(&standby, FAULT_OOS_FLAG));

    // Through expiry and the manual returns, both run identically.
    driver_b.write(FAULT, Value::Bool(true)).unwrap();
    for _ in 0..4 {
        active.scan().unwrap();
        standby.scan().unwrap();
    }
    standby.submit_command(write_value(FAULT_SHELVE, Value::Bool(false)));
    standby.submit_command(write_value(FAULT_OOS, Value::Bool(false)));
    active.submit_command(write_value(FAULT_SHELVE, Value::Bool(false)));
    active.submit_command(write_value(FAULT_OOS, Value::Bool(false)));
    active.scan().unwrap();
    standby.scan().unwrap();
    assert_eq!(
        serde_json::to_string(&active.snapshot()).unwrap(),
        serde_json::to_string(&standby.snapshot()).unwrap()
    );
}

#[test]
fn identical_scripted_runs_produce_identical_snapshots() {
    let run = || {
        let model = model();
        let driver = sim_driver(&model).unwrap();
        let mut executor = build_executor(&model, &driver);
        driver.write(FAULT, Value::Bool(true)).unwrap();
        executor.scan().unwrap();
        executor.submit_command(write_value(FAULT_SHELVE, Value::Bool(true)));
        for _ in 0..6 {
            executor.scan().unwrap();
        }
        serde_json::to_string(&executor.snapshot()).unwrap()
    };
    assert_eq!(run(), run());
}

/// A managed kind missing `max_shelve_ticks` fails assembly naming the
/// parameter — the declared-set enforcement reaching the fixture path.
#[test]
fn a_missing_managed_parameter_fails_assembly_naming_it() {
    let mut document: serde_json::Value = serde_json::from_str(MANAGED_ALARMS).unwrap();
    document["components"][0]["parameters"]
        .as_object_mut()
        .unwrap()
        .remove("max_shelve_ticks");
    let model = PlantModel::load(&document.to_string()).unwrap();
    let driver = sim_driver(&model).unwrap();
    match assemble(&model, &registry(), &driver).err().unwrap() {
        AssemblyError::Component {
            component, detail, ..
        } => {
            assert_eq!(component.0, 1);
            assert!(
                detail.contains("max_shelve_ticks"),
                "the failure should name the missing parameter, found {detail}"
            );
        }
        other => panic!("expected AssemblyError::Component, got {other:?}"),
    }
}
