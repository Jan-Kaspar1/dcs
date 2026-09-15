//! The reference pumping station, composed through
//! [`dcs_build::station::pumping_station`] — issue #220's artifact.
//!
//! The checked-in documents live at `crates/dcs-demo/fixtures/`:
//! `pump_station.json` is the emitted PlantModel,
//! `pump_station_dynamics.json` the decision-44 dynamics declaration —
//! a declared inflow plus two `bool_flow` pump draws summed into the
//! level integrator, with a first-order lag producing the backup
//! measurement. These tests assert the helper re-emits the checked-in
//! document exactly, that the document validates and lints clean,
//! assembles through the standard registries, serde-roundtrips, and
//! that a scripted run over the merged dynamics shows the closed
//! station loop — and that every run is bit-for-bit deterministic.
//!
//! ## The scripted scenario
//!
//! Each iteration scans the executor, observes the image, applies the
//! script's commands/forcing, then steps the plant one second — so
//! scan `s` observes the world `step s` produced. The dynamics start
//! the level at 0.8 m with a 0.6 m/step declared inflow: the level
//! climbs past `start` (2.0) into `lag_start` (3.0) and `high` (4.0)
//! before the staged pumps' −1.0 draws win, then drains back through
//! `stop` (1.0). The script then exercises every wired contract:
//!
//! - the level drains while a pump command stands and climbs on inflow;
//! - the level alarms assert at the declared `high`/`cutoff` thresholds
//!   and their `unacknowledged` latches hold until the operator acks;
//! - a `Bad` primary level flips the failover to the backup
//!   measurement and raises its alarm;
//! - the manual-takeover path drives `p101-cmd` from the operator's
//!   `hand` request while the group no longer requests that pump;
//! - a `Disconnected` run contact proves the motor fault, drops the
//!   pump from the group, and latches the fault alarm;
//! - out-of-service blocks even a hand start through the guard;
//! - the power-fail contact drops every pump's availability.
//!
//! ## The managed-lifecycle scenario
//!
//! A second scripted run — monitor-backed, so it rides the receipted,
//! actor-attributed command path and the durable journal — exercises
//! the managed alarm surface decision 73 wires into the station: the
//! per-pump fault alarm's designed suppression on the pump's own
//! maintenance-inhibit point, the shelvable low-level alarm's bounded
//! shelve and its automatic expiry, and the never-shelvable
//! high-level alarm's `NotWritable` rejection, every transition
//! landing in the journal in `seq` order.

use dcs_assembly::{DriverRegistry, FanoutDriver, assemble, resolve_drivers};
use dcs_build::station::{
    ManagedAlarmLayout, PumpStation, PumpStationConfig, PumpStationLayout, pumping_station,
};
use dcs_build::{PointId, Value};
use dcs_core::{
    Command, CommandError, CommandOutcome, CommandReceipt, IoDriver, JournalEntry, JournalEvent,
    Quality, QualityReason, Sample, ValueKind,
};
use dcs_model::PlantModel;
use dcs_monitor::{Monitor, MonitorClient};
use dcs_sim::{Fault, ProcessElement};
use std::thread;

/// The checked-in emitted document.
const MODEL_JSON: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/pump_station.json"
);
/// The checked-in dynamics declaration merged over it.
const DYNAMICS_JSON: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/pump_station_dynamics.json"
);

/// The plant step each scan period covers, in seconds.
const DT: f64 = 1.0;
/// The scripted run length — covers every phase below plus settling.
const SCANS: u64 = 115;
/// The managed-lifecycle run's length — the shelve bound's expiry, the
/// suppression arc, and settling.
const MANAGED_SCANS: u64 = 52;
/// The actor every operator command in the managed-lifecycle run
/// carries — the attributed, receipted path decision 77 names.
const OPERATOR: &str = "operator";

/// Emits the station through the reference configuration.
fn emit() -> PumpStation {
    pumping_station(&PumpStationConfig::reference()).unwrap()
}

/// The checked-in document, loaded through `dcs-model`'s validating
/// loader.
fn fixture_model() -> PlantModel {
    PlantModel::load(&std::fs::read_to_string(MODEL_JSON).unwrap()).unwrap()
}

/// The checked-in dynamics declaration, parsed as `dcs-plant-server
/// --dynamics` parses it.
fn dynamics() -> Vec<ProcessElement> {
    serde_json::from_str(&std::fs::read_to_string(DYNAMICS_JSON).unwrap()).unwrap()
}

/// Builds the driver side of `model` through the standard
/// [`DriverRegistry`], merging the checked-in dynamics into the shared
/// sim map exactly as `dcs-plant-server --dynamics` does — each element
/// lands through `with_element` and revalidates the map.
fn build_driver(model: &PlantModel) -> FanoutDriver {
    let mut plan = resolve_drivers(model, &DriverRegistry::standard()).unwrap();
    for element in dynamics() {
        plan.sim_map = plan.sim_map.with_element(element);
        plan.sim_map.validate().unwrap();
    }
    plan.build().unwrap()
}

/// Writes `value` to `point` through the operator command path.
fn write(executor: &mut dcs_runtime::Executor<'_>, point: PointId, kind: ValueKind, value: Value) {
    let receipt = executor.submit_command(Command::WriteValue { point, kind, value });
    assert!(
        matches!(receipt.outcome, CommandOutcome::Accepted { .. }),
        "write to {point:?} rejected: {receipt:?}"
    );
}

/// Pulses the alarm's writable ack point: `true`, released next scan.
fn ack(executor: &mut dcs_runtime::Executor<'_>, alarm: &ManagedAlarmLayout) {
    write(executor, alarm.ack, ValueKind::Bool, Value::Bool(true));
}

/// Releases the alarm's writable ack point.
fn release_ack(executor: &mut dcs_runtime::Executor<'_>, alarm: &ManagedAlarmLayout) {
    write(executor, alarm.ack, ValueKind::Bool, Value::Bool(false));
}

/// One scan's observable record.
#[derive(Debug, PartialEq)]
struct Scan {
    /// The primary level the plant sees.
    level: f64,
    /// The failover-selected level the chain and alarms control on.
    selected: f64,
    /// The chain's stage-count demand.
    demand: i64,
    /// The group's duty holder, `0` while none holds it.
    duty: i64,
    /// How many pumps the group commands.
    staged: i64,
    duty_call: bool,
    lag_call: bool,
    below_cutoff: bool,
    high_level: bool,
    backup_active: bool,
    none_available: bool,
    all_faulted: bool,
    /// The group's `cmd_i` requests, per pump.
    group_cmd: [bool; 2],
    /// The field commands actually driven, per pump.
    cmd: [bool; 2],
    /// The run contacts the loopback reports, per pump.
    run: [bool; 2],
    lah_alarm: bool,
    lah_unack: bool,
    lal_alarm: bool,
    lal_unack: bool,
    ba_alarm: bool,
    ba_unack: bool,
    na_alarm: bool,
    na_unack: bool,
    pw_alarm: bool,
    pw_unack: bool,
    p1_fault_alarm: bool,
    p1_fault_unack: bool,
    p1_thermal_alarm: bool,
    p1_thermal_unack: bool,
}

/// What the scripted run produced: the per-scan record, the command
/// receipts, and the final serialized snapshot.
#[derive(Debug, PartialEq)]
struct Run {
    scans: Vec<Scan>,
    receipts: Vec<CommandReceipt>,
    snapshot: String,
}

fn float(sample: dcs_core::Sample) -> f64 {
    match sample.value {
        Value::Float(value) => value,
        other => panic!("expected a Float sample, got {other:?}"),
    }
}

fn int(sample: dcs_core::Sample) -> i64 {
    match sample.value {
        Value::Int(value) => value,
        other => panic!("expected an Int sample, got {other:?}"),
    }
}

fn bool_(sample: dcs_core::Sample) -> bool {
    match sample.value {
        Value::Bool(value) => value,
        other => panic!("expected a Bool sample, got {other:?}"),
    }
}

/// The managed-lifecycle run's observable record — pump 2's designed
/// suppression, `lal`'s bounded shelving, and `lah`'s rejected shelve.
#[derive(Debug, PartialEq)]
struct ManagedScan {
    /// The primary level — the run's pacing context.
    level: f64,
    /// How many pumps the group commands.
    staged: i64,
    /// Pump 2's run contact and driven command.
    p2_run: bool,
    p2_cmd: bool,
    /// The pump's own out-of-service point — the fault alarm's bound
    /// `oos`/`suppress` source.
    p2_oos_point: bool,
    /// The proven motor-fault carrier.
    p2_fault: bool,
    // Pump 2's managed fault alarm's full status surface.
    p2_fault_alarm: bool,
    p2_fault_unack: bool,
    p2_fault_shelved: bool,
    p2_fault_suppressed: bool,
    p2_fault_oos: bool,
    // The shelvable low-level alarm's lifecycle surface.
    lal_alarm: bool,
    lal_unack: bool,
    lal_shelved: bool,
    // The never-shelvable high-level alarm's shelve status — never
    // asserts, its request point refusing the command.
    lah_shelved: bool,
}

/// What the managed-lifecycle run produced: the per-scan record, the
/// durable journal, the command receipts — including the one
/// documented rejection — and the final serialized snapshot.
#[derive(Debug, PartialEq)]
struct ManagedRun {
    scans: Vec<ManagedScan>,
    journal: Vec<JournalEntry>,
    receipts: Vec<CommandReceipt>,
    /// The `lah` shelve attempt's receipt — the documented
    /// `NotWritable` rejection.
    rejected_shelve: CommandReceipt,
    snapshot: String,
}

fn telemetry(snapshot: &dcs_core::TelemetrySnapshot, point: PointId) -> &dcs_core::PointTelemetry {
    snapshot
        .points
        .iter()
        .find(|telemetry| telemetry.point == point)
        .unwrap_or_else(|| panic!("no telemetry for {point:?}"))
}

fn monitor_sample(snapshot: &dcs_core::TelemetrySnapshot, point: PointId) -> Sample {
    telemetry(snapshot, point)
        .sample
        .unwrap_or_else(|| panic!("no sample for {point:?}"))
}

/// Writes `value` to `point` through the attributed, receipted
/// operator command path the monitor exposes.
fn operator_write(client: &MonitorClient, point: PointId, kind: ValueKind, value: Value) {
    let receipt = client
        .command_as(&Command::WriteValue { point, kind, value }, Some(OPERATOR))
        .unwrap();
    assert!(
        matches!(receipt.outcome, CommandOutcome::Accepted { .. }),
        "write to {point:?} rejected: {receipt:?}"
    );
}

/// Pulses a managed alarm's writable ack point over the monitor path.
fn operator_ack(client: &MonitorClient, alarm: &ManagedAlarmLayout) {
    operator_write(client, alarm.ack, ValueKind::Bool, Value::Bool(true));
}

/// Releases a managed alarm's writable ack point.
fn operator_release_ack(client: &MonitorClient, alarm: &ManagedAlarmLayout) {
    operator_write(client, alarm.ack, ValueKind::Bool, Value::Bool(false));
}

/// The ids the run addresses — the emitted station's layout.
fn ids() -> PumpStationLayout {
    emit().layout
}

/// Runs the documented scenario against the checked-in document and
/// dynamics.
fn run() -> Run {
    let model = fixture_model();
    let layout = ids();
    let driver = build_driver(&model);
    let sim = driver
        .sim()
        .expect("the station's devices all serve the local sim");
    let mut executor = assemble(&model, &dcs_controller::registry(), &driver).unwrap();

    // The declared inflow, and one step so scan 1 sees the dynamics'
    // declared level rather than the binding's neutral seed.
    sim.write(layout.inflow, Value::Float(0.6)).unwrap();
    driver.step(DT).unwrap();

    let mut scans = Vec::with_capacity(SCANS as usize);
    let observe = |executor: &dcs_runtime::Executor<'_>, scans: &mut Vec<Scan>| {
        let sample = |point| executor.sample(point).unwrap();
        scans.push(Scan {
            level: float(sample(layout.level_primary)),
            selected: float(sample(layout.level_selected)),
            demand: int(sample(layout.demand)),
            duty: int(sample(layout.duty)),
            staged: int(sample(layout.staged)),
            duty_call: bool_(sample(layout.duty_call)),
            lag_call: bool_(sample(layout.lag_call)),
            below_cutoff: bool_(sample(layout.below_cutoff)),
            high_level: bool_(sample(layout.high_level)),
            backup_active: bool_(sample(layout.backup_active)),
            none_available: bool_(sample(layout.none_available)),
            all_faulted: bool_(sample(layout.all_faulted)),
            group_cmd: [
                bool_(sample(layout.pumps[0].group_cmd)),
                bool_(sample(layout.pumps[1].group_cmd)),
            ],
            cmd: [
                bool_(sample(layout.pumps[0].cmd)),
                bool_(sample(layout.pumps[1].cmd)),
            ],
            run: [
                bool_(sample(layout.pumps[0].run)),
                bool_(sample(layout.pumps[1].run)),
            ],
            lah_alarm: bool_(sample(layout.high_level_alarm.alarm)),
            lah_unack: bool_(sample(layout.high_level_alarm.unacknowledged)),
            lal_alarm: bool_(sample(layout.low_level_alarm.alarm)),
            lal_unack: bool_(sample(layout.low_level_alarm.unacknowledged)),
            ba_alarm: bool_(sample(layout.backup_active_alarm.alarm)),
            ba_unack: bool_(sample(layout.backup_active_alarm.unacknowledged)),
            na_alarm: bool_(sample(layout.none_available_alarm.alarm)),
            na_unack: bool_(sample(layout.none_available_alarm.unacknowledged)),
            pw_alarm: bool_(sample(layout.power_fail_alarm.alarm)),
            pw_unack: bool_(sample(layout.power_fail_alarm.unacknowledged)),
            p1_fault_alarm: bool_(sample(layout.pumps[0].fault_alarm.alarm)),
            p1_fault_unack: bool_(sample(layout.pumps[0].fault_alarm.unacknowledged)),
            p1_thermal_alarm: bool_(sample(layout.pumps[0].thermal_alarm.alarm)),
            p1_thermal_unack: bool_(sample(layout.pumps[0].thermal_alarm.unacknowledged)),
        });
    };

    for scan in 1..=SCANS {
        executor.scan().unwrap();
        observe(&executor, &mut scans);
        match scan {
            // Pump-down phase done — drop the inflow below zero so the
            // wet well drains past the dry-run cutoff.
            21 => sim.write(layout.inflow, Value::Float(-0.5)).unwrap(),
            26 => sim.write(layout.inflow, Value::Float(0.6)).unwrap(),
            // Acknowledge the level alarms and any startup
            // none-available latch.
            28 => {
                ack(&mut executor, &layout.high_level_alarm);
                ack(&mut executor, &layout.low_level_alarm);
                ack(&mut executor, &layout.none_available_alarm);
            }
            30 => {
                release_ack(&mut executor, &layout.high_level_alarm);
                release_ack(&mut executor, &layout.low_level_alarm);
                release_ack(&mut executor, &layout.none_available_alarm);
            }
            // The primary level transmitter goes Bad — the failover
            // switches to the backup and raises its alarm.
            38 => sim
                .inject_fault(
                    layout.level_primary,
                    Fault::Quality(Quality::Bad(QualityReason::CommunicationFault)),
                )
                .unwrap(),
            43 => ack(&mut executor, &layout.backup_active_alarm),
            45 => release_ack(&mut executor, &layout.backup_active_alarm),
            47 => sim.clear_fault(layout.level_primary).unwrap(),
            // Manual takeover on pump 1: the operator's hand request
            // drives the motor while the group's request drops.
            50 => write(
                &mut executor,
                layout.pumps[0].mode,
                ValueKind::Bool,
                Value::Bool(true),
            ),
            52 => write(
                &mut executor,
                layout.pumps[0].hand,
                ValueKind::Bool,
                Value::Bool(true),
            ),
            57 => write(
                &mut executor,
                layout.pumps[0].hand,
                ValueKind::Bool,
                Value::Bool(false),
            ),
            58 => write(
                &mut executor,
                layout.pumps[0].mode,
                ValueKind::Bool,
                Value::Bool(false),
            ),
            // Pump 1's run contact disconnects: the motor proves the
            // fault, the group excludes the pump, the alarm latches.
            62 => sim
                .inject_fault(layout.pumps[0].run, Fault::Disconnected)
                .unwrap(),
            64 => sim
                .inject_fault(layout.pumps[1].run, Fault::Disconnected)
                .unwrap(),
            70 => {
                ack(&mut executor, &layout.pumps[0].fault_alarm);
                ack(&mut executor, &layout.all_faulted_alarm);
                ack(&mut executor, &layout.none_available_alarm);
            }
            72 => {
                release_ack(&mut executor, &layout.pumps[0].fault_alarm);
                release_ack(&mut executor, &layout.all_faulted_alarm);
                release_ack(&mut executor, &layout.none_available_alarm);
            }
            74 => sim.clear_fault(layout.pumps[0].run).unwrap(),
            75 => sim.clear_fault(layout.pumps[1].run).unwrap(),
            // Out of service: even an operator's hand request cannot
            // start the pump — the guard blocks it.
            78 => write(
                &mut executor,
                layout.pumps[1].out_of_service,
                ValueKind::Bool,
                Value::Bool(true),
            ),
            79 => write(
                &mut executor,
                layout.pumps[1].mode,
                ValueKind::Bool,
                Value::Bool(true),
            ),
            80 => write(
                &mut executor,
                layout.pumps[1].hand,
                ValueKind::Bool,
                Value::Bool(true),
            ),
            83 => {
                write(
                    &mut executor,
                    layout.pumps[1].hand,
                    ValueKind::Bool,
                    Value::Bool(false),
                );
                write(
                    &mut executor,
                    layout.pumps[1].mode,
                    ValueKind::Bool,
                    Value::Bool(false),
                );
            }
            84 => write(
                &mut executor,
                layout.pumps[1].out_of_service,
                ValueKind::Bool,
                Value::Bool(false),
            ),
            // Station power fails: every pump's availability drops.
            88 => sim.write(layout.power_fail, Value::Bool(true)).unwrap(),
            93 => {
                ack(&mut executor, &layout.power_fail_alarm);
                ack(&mut executor, &layout.none_available_alarm);
            }
            95 => {
                release_ack(&mut executor, &layout.power_fail_alarm);
                release_ack(&mut executor, &layout.none_available_alarm);
            }
            96 => sim.write(layout.power_fail, Value::Bool(false)).unwrap(),
            // A per-pump field contact: pump 1's thermal overload.
            98 => sim
                .write(layout.pumps[0].thermal, Value::Bool(true))
                .unwrap(),
            102 => ack(&mut executor, &layout.pumps[0].thermal_alarm),
            104 => {
                release_ack(&mut executor, &layout.pumps[0].thermal_alarm);
                sim.write(layout.pumps[0].thermal, Value::Bool(false))
                    .unwrap();
            }
            _ => {}
        }
        driver.step(DT).unwrap();
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
        scans,
        receipts: executor.receipts().to_vec(),
        snapshot: serde_json::to_string(&executor.snapshot()).unwrap(),
    }
}

#[test]
fn helper_emits_the_checked_in_document() {
    let emitted = emit();
    assert_eq!(emitted.model, fixture_model());
    assert_eq!(
        serde_json::to_value(&emitted.model).unwrap(),
        serde_json::from_str::<serde_json::Value>(&std::fs::read_to_string(MODEL_JSON).unwrap())
            .unwrap(),
    );
}

#[test]
fn checked_in_document_validates_and_lints_clean() {
    let model = fixture_model();
    assert_eq!(model.version, dcs_model::MODEL_VERSION);
    assert!(model.validate().is_empty(), "{:?}", model.validate());
    assert!(model.lint().is_empty(), "{:?}", model.lint());
}

#[test]
fn document_assembles_through_the_standard_registry() {
    let model = fixture_model();
    let driver = build_driver(&model);
    assemble(&model, &dcs_controller::registry(), &driver).unwrap();
}

#[test]
fn document_serde_roundtrips() {
    let emitted = emit().model;
    let json = serde_json::to_string_pretty(&emitted).unwrap();
    assert_eq!(PlantModel::load(&json).unwrap(), emitted);
}

#[test]
fn journaled_marks_the_durable_record_points() {
    // Decision 74's sweep: every managed alarm's full status set —
    // `alarm`/`unacknowledged`/`shelved`/`suppressed`/
    // `out_of_service` — plus the declared `shelve`/`oos` request
    // points, the mode and managed-state flags decision 75 names,
    // and the protection-relevant status points the composition
    // carries are declared `journaled` — their value transitions join
    // the durable journal as `point_changed` entries. The flag stays
    // opt-in: receipts, demand copies, and the float measurements
    // keep their own paths.
    let emitted = emit();
    let layout = &emitted.layout;
    let journaled = |point: PointId| {
        emitted
            .model
            .io_points
            .iter()
            .find(|io| io.id == point)
            .unwrap_or_else(|| panic!("{point:?} is not in the emitted model"))
            .journaled
    };

    let mut record = vec![
        layout.power_fail,
        layout.below_cutoff,
        layout.high_level,
        layout.backup_active,
        layout.none_available,
        layout.all_faulted,
    ];
    let mut off_record = vec![
        layout.level_primary,
        layout.level_backup,
        layout.level_selected,
        layout.inflow,
        layout.net_flow,
        layout.demand,
        layout.duty,
        layout.staged,
        layout.duty_call,
        layout.lag_call,
    ];
    let alarms = [
        &layout.high_level_alarm,
        &layout.low_level_alarm,
        &layout.backup_active_alarm,
        &layout.none_available_alarm,
        &layout.all_faulted_alarm,
        &layout.power_fail_alarm,
    ];
    for alarm in alarms {
        record.extend([
            alarm.alarm,
            alarm.unacknowledged,
            alarm.shelved,
            alarm.suppressed,
            alarm.out_of_service,
        ]);
        record.extend(alarm.shelve);
        record.extend(alarm.oos);
        // The ack point's writes are already the attributed,
        // receipted record — it stays off the journaled set.
        off_record.push(alarm.ack);
    }
    for pump in &layout.pumps {
        record.extend([
            pump.run,
            pump.thermal,
            pump.moisture,
            pump.mode,
            pump.out_of_service,
            pump.fault,
            pump.avail,
        ]);
        off_record.extend([pump.cmd, pump.draw, pump.hand, pump.group_cmd]);
        for alarm in [&pump.fault_alarm, &pump.thermal_alarm, &pump.moisture_alarm] {
            record.extend([
                alarm.alarm,
                alarm.unacknowledged,
                alarm.shelved,
                alarm.suppressed,
                alarm.out_of_service,
            ]);
            record.extend(alarm.shelve);
            record.extend(alarm.oos);
            off_record.push(alarm.ack);
        }
    }
    for point in &record {
        assert!(journaled(*point), "{point:?} must carry `journaled`");
    }
    for point in &off_record {
        assert!(!journaled(*point), "{point:?} must stay off the record");
    }

    // The durable record is bool/int-only: no float point may carry
    // the flag — validation reports `JournaledFloat`.
    for point in &emitted.model.io_points {
        if point.journaled {
            assert!(
                matches!(point.value_type, ValueKind::Bool | ValueKind::Int),
                "a journaled float slipped in: {point:?}"
            );
        }
    }
}

#[test]
fn identical_builds_emit_identical_documents() {
    let first = emit().model;
    let second = emit().model;
    assert_eq!(first, second);
    assert_eq!(
        serde_json::to_string_pretty(&first).unwrap(),
        serde_json::to_string_pretty(&second).unwrap()
    );
}

#[test]
fn dynamics_document_loads_through_the_dynamics_merge() {
    // `build_driver` merges each element through `with_element` and
    // revalidates — the same path `dcs-plant-server --dynamics` takes.
    let model = fixture_model();
    build_driver(&model);
}

#[test]
fn scripted_run_shows_the_closed_station_loop() {
    let run = run();
    let scans = &run.scans;
    let at = |scan: u64| &scans[scan as usize - 1];

    // The level climbs on the declared inflow from the dynamics'
    // 0.8 m start until the staged pumps' draws win.
    assert!(
        (at(1).level - 1.4).abs() < 1e-9,
        "scan 1: {:?}",
        at(1).level
    );
    for window in scans.windows(2).take(6) {
        assert!(
            window[1].level > window[0].level,
            "the level must climb on inflow: {:?} -> {:?}",
            window[0].level,
            window[1].level
        );
    }

    // The chain calls for duty then lag as the level crosses the
    // declared setpoints, and the group stages real pumps.
    assert!(scans.iter().any(|scan| scan.duty_call));
    assert!(scans.iter().any(|scan| scan.lag_call));
    assert!(scans.iter().any(|scan| scan.demand == 2));
    assert!(scans.iter().any(|scan| scan.staged == 2));
    assert!(scans.iter().any(|scan| scan.cmd[0] && scan.run[0]));
    assert!(scans.iter().any(|scan| scan.cmd[1] && scan.run[1]));

    // The level drains while a pump command stands: after the staged
    // peak the trace falls through several consecutive scans.
    let peak = scans
        .iter()
        .map(|scan| scan.level)
        .fold(f64::NEG_INFINITY, f64::max);
    assert!(
        peak >= 4.0,
        "the level must reach the high setpoint: {peak}"
    );
    let peak_at = scans.iter().position(|scan| scan.level == peak).unwrap();
    assert!(
        scans[peak_at..]
            .iter()
            .any(|scan| scan.cmd.iter().any(|&c| c))
    );
    let mut drained = false;
    for window in scans[peak_at..].windows(4) {
        if window[0].level > window[1].level
            && window[1].level > window[2].level
            && window[2].level > window[3].level
            && window.iter().any(|scan| scan.cmd.iter().any(|&c| c))
        {
            drained = true;
        }
    }
    assert!(drained, "the level must drain while a pump command stands");

    // The high-level alarm trips at the declared `high` (4.0), latches
    // unacknowledged, clears under the scan-28 ack — and re-latches on
    // the level's next excursion past 4.0, the documented trip-until-
    // acknowledged lifecycle.
    let lah = scans.iter().position(|scan| scan.lah_alarm).unwrap();
    assert!(
        at(lah as u64 + 1).selected >= 4.0 - 1e-9,
        "LAH tripped below the declared high: {:?}",
        at(lah as u64 + 1).selected
    );
    assert!(scans[..29].iter().any(|scan| scan.lah_unack));
    assert!(
        scans[29..48].iter().all(|scan| !scan.lah_unack),
        "the ack must hold the latch clear until a fresh trip"
    );
    assert!(
        scans[48..]
            .iter()
            .any(|scan| scan.lah_alarm && scan.lah_unack),
        "a fresh violation must re-latch the alarm"
    );

    // The low-level alarm trips at the declared `cutoff` (0.5) while
    // the chain reports `below_cutoff`, and clears under the same ack.
    let lal = scans.iter().position(|scan| scan.lal_alarm).unwrap();
    assert!(
        at(lal as u64 + 1).selected <= 0.5 + 1e-9,
        "LAL tripped above the declared cutoff: {:?}",
        at(lal as u64 + 1).selected
    );
    assert!(scans.iter().any(|scan| scan.below_cutoff));
    assert!(scans[..29].iter().any(|scan| scan.lal_unack));
    assert!(
        scans[29..62].iter().all(|scan| !scan.lal_unack),
        "the ack must hold the latch clear until a fresh trip"
    );

    // The Bad primary flips the failover onto the backup measurement:
    // `backup_active` stands and its alarm latches until the scan-43
    // ack.
    assert!(scans[38..47].iter().all(|scan| scan.backup_active));
    assert!(
        scans[39..47]
            .iter()
            .any(|scan| scan.ba_alarm && scan.ba_unack)
    );
    assert!(scans[43..].iter().all(|scan| !scan.ba_unack));

    // Manual takeover: while `mode` stands the group drops the pump's
    // request, and the operator's `hand` drives `p101-cmd` alone.
    assert!(
        scans[55..59]
            .iter()
            .all(|scan| scan.cmd[0] && !scan.group_cmd[0])
    );

    // The disconnected run contact proves the motor fault, latches the
    // alarm until the scan-70 ack, and — with pump 2 faulted too —
    // raises `all_faulted` and `none_available`.
    assert!(
        scans[64..70]
            .iter()
            .any(|scan| scan.p1_fault_alarm && scan.p1_fault_unack)
    );
    assert!(scans[65..75].iter().any(|scan| scan.all_faulted));
    assert!(
        scans[65..75]
            .iter()
            .any(|scan| scan.none_available && scan.na_unack)
    );
    assert!(scans[70..].iter().all(|scan| !scan.p1_fault_unack));

    // Out of service blocks even a hand start: `p102-cmd` never stands
    // while `oos` holds — the command guard wins.
    assert!(scans[80..85].iter().all(|scan| !scan.cmd[1]));

    // The power-fail contact drops both pumps' availability:
    // `none_available` and the power alarm stand until restored.
    assert!(
        scans[90..96]
            .iter()
            .any(|scan| scan.none_available && scan.na_alarm)
    );
    assert!(
        scans[88..94]
            .iter()
            .any(|scan| scan.pw_alarm && scan.pw_unack)
    );

    // Pump 1's thermal contact trips its field-point alarm and clears
    // under the scan-102 ack.
    assert!(
        scans[98..103]
            .iter()
            .any(|scan| scan.p1_thermal_alarm && scan.p1_thermal_unack)
    );
    assert!(scans[103..].iter().all(|scan| !scan.p1_thermal_unack));

    // Duty rotates between cycles: the first pump-down's duty holder is
    // not the later one's.
    let first_duty = scans
        .iter()
        .find_map(|scan| (scan.duty > 0).then_some(scan.duty));
    let duties: Vec<i64> = scans.iter().map(|scan| scan.duty).collect();
    assert_eq!(first_duty, Some(1));
    assert!(duties.contains(&2), "duty never rotated: {duties:?}");

    // Every command landed.
    assert!(
        run.receipts.iter().all(|receipt| matches!(
            receipt.outcome,
            CommandOutcome::Applied { .. } | CommandOutcome::Accepted { .. }
        )),
        "a command did not apply: {:?}",
        run.receipts
    );
}

#[test]
fn repeated_builds_and_runs_are_deterministic() {
    assert_eq!(run(), run());
    assert_eq!(managed_run(), managed_run());
}

/// Runs the managed-lifecycle scenario against the checked-in document
/// over the monitor's paced-scan surface — the same command and journal
/// paths the alarm pane uses. The scenario:
///
/// - pump 2 goes out of service early (scan-4 write lands at scan 5);
///   its fault alarm's bound `oos` asserts `out_of_service` and the
///   pass-through copy asserts `suppressed` a scan later;
/// - pump 2's run contact disconnects while the pump stands
///   deliberately offline: the non-Good feedback counts as disagreeing
///   even uncommanded, so the motor proves the fault and the alarm's
///   `alarm` reports it — countable — while `suppressed` holds the
///   `unacknowledged` latch clear, the designed suppression;
/// - the operator's shelve request on the never-shelvable high-level
///   alarm answers `NotWritable` at submission — the documented
///   rejection path;
/// - the low-level alarm takes a bounded shelve — `shelved` asserts
///   inside `lal_max_shelve_ticks` and expires while the request still
///   stands;
/// - releasing the pump's out-of-service lifts the suppression onto a
///   still-standing fault, re-annunciating it as a fresh latch; the
///   operator acks it and the contact recovers;
/// - every lifecycle transition lands in the durable journal in `seq`
///   order.
fn managed_run() -> ManagedRun {
    let model = fixture_model();
    let layout = ids();
    let driver = build_driver(&model);
    let sim = driver
        .sim()
        .expect("the station's devices all serve the local sim");
    let executor = assemble(&model, &dcs_controller::registry(), &driver).unwrap();
    let monitor = Monitor::bind("127.0.0.1:0", executor, model.signal_index()).unwrap();
    let client = MonitorClient::new(monitor.local_addr());

    // The declared inflow, and one step so scan 1 sees the dynamics'
    // declared level rather than the binding's neutral seed.
    sim.write(layout.inflow, Value::Float(0.6)).unwrap();
    driver.step(DT).unwrap();

    let result = thread::scope(|scope| {
        scope.spawn(|| monitor.serve());
        // A failing assertion must not deadlock the scope join: catch
        // the panic so the server is always shut down first.
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let pump = &layout.pumps[1];
            let mut scans = Vec::with_capacity(MANAGED_SCANS as usize);
            let mut rejected_shelve = None;
            for scan in 1..=MANAGED_SCANS {
                let snapshot = client.advance(1).unwrap();
                scans.push(ManagedScan {
                    level: float(monitor_sample(&snapshot, layout.level_primary)),
                    staged: int(monitor_sample(&snapshot, layout.staged)),
                    p2_run: bool_(monitor_sample(&snapshot, pump.run)),
                    p2_cmd: bool_(monitor_sample(&snapshot, pump.cmd)),
                    p2_oos_point: bool_(monitor_sample(&snapshot, pump.out_of_service)),
                    p2_fault: bool_(monitor_sample(&snapshot, pump.fault)),
                    p2_fault_alarm: bool_(monitor_sample(&snapshot, pump.fault_alarm.alarm)),
                    p2_fault_unack: bool_(monitor_sample(
                        &snapshot,
                        pump.fault_alarm.unacknowledged,
                    )),
                    p2_fault_shelved: bool_(monitor_sample(&snapshot, pump.fault_alarm.shelved)),
                    p2_fault_suppressed: bool_(monitor_sample(
                        &snapshot,
                        pump.fault_alarm.suppressed,
                    )),
                    p2_fault_oos: bool_(monitor_sample(&snapshot, pump.fault_alarm.out_of_service)),
                    lal_alarm: bool_(monitor_sample(&snapshot, layout.low_level_alarm.alarm)),
                    lal_unack: bool_(monitor_sample(
                        &snapshot,
                        layout.low_level_alarm.unacknowledged,
                    )),
                    lal_shelved: bool_(monitor_sample(&snapshot, layout.low_level_alarm.shelved)),
                    lah_shelved: bool_(monitor_sample(&snapshot, layout.high_level_alarm.shelved)),
                });
                match scan {
                    // Pump 2 out of service: the maintenance-inhibit
                    // point lands at scan 5 — `out_of_service` asserts
                    // on the bound `oos`, `suppressed` a scan later on
                    // the delivered copy.
                    4 => operator_write(
                        &client,
                        pump.out_of_service,
                        ValueKind::Bool,
                        Value::Bool(true),
                    ),
                    // The offline pump's run contact disconnects — the
                    // non-Good feedback disagrees even uncommanded, so
                    // the motor proves the fault inside two scans.
                    6 => sim.inject_fault(pump.run, Fault::Disconnected).unwrap(),
                    // The never-shelvable rejection path: the standing
                    // high-level alarm's shelve point is read-only, so
                    // the request answers `NotWritable` at submission.
                    12 => {
                        let receipt = client
                            .command_as(
                                &Command::WriteValue {
                                    point: layout.high_level_alarm.shelve.unwrap(),
                                    kind: ValueKind::Bool,
                                    value: Value::Bool(true),
                                },
                                Some(OPERATOR),
                            )
                            .unwrap();
                        rejected_shelve = Some(receipt);
                    }
                    // The bounded shelve on the nuisance alarm: the
                    // request lands at scan 15 and holds the flag for
                    // `lal_max_shelve_ticks` — expiring at scan 23 while
                    // the request still stands.
                    14 => operator_write(
                        &client,
                        layout.low_level_alarm.shelve.unwrap(),
                        ValueKind::Bool,
                        Value::Bool(true),
                    ),
                    26 => operator_write(
                        &client,
                        layout.low_level_alarm.shelve.unwrap(),
                        ValueKind::Bool,
                        Value::Bool(false),
                    ),
                    // Back in service: the suppression lifts onto the
                    // still-standing fault, re-annunciating it as a
                    // fresh latch the operator then acknowledges.
                    30 => operator_write(
                        &client,
                        pump.out_of_service,
                        ValueKind::Bool,
                        Value::Bool(false),
                    ),
                    34 => operator_ack(&client, &pump.fault_alarm),
                    36 => operator_release_ack(&client, &pump.fault_alarm),
                    // The contact recovers: the feedback agrees again
                    // and the fault clears.
                    38 => sim.clear_fault(pump.run).unwrap(),
                    _ => {}
                }
                driver.step(DT).unwrap();
            }
            ManagedRun {
                scans,
                journal: client.journal(0).unwrap(),
                receipts: client.receipts().unwrap(),
                rejected_shelve: rejected_shelve.unwrap(),
                snapshot: serde_json::to_string(&client.snapshot().unwrap()).unwrap(),
            }
        }));
        monitor.shutdown();
        result
    });
    let run = result.unwrap_or_else(|panic| std::panic::resume_unwind(panic));
    assert!(
        serde_json::from_str::<serde_json::Value>(&run.snapshot).unwrap()["components"]
            .as_array()
            .unwrap()
            .iter()
            .all(|component| component["step_errors"] == 0),
        "a component failed to step"
    );
    run
}

/// The point bound to `component`'s `port` — `None` where the model
/// leaves the port unbound.
fn bound_point(
    model: &PlantModel,
    component: dcs_model::ComponentId,
    port: &str,
) -> Option<PointId> {
    model
        .connections
        .iter()
        .find_map(|connection| match (&connection.from, &connection.to) {
            (dcs_model::Endpoint::Point(point), dcs_model::Endpoint::Port(reference))
                if reference.component == component && reference.name == port =>
            {
                Some(*point)
            }
            _ => None,
        })
}

/// The point's declared `writable` mark.
fn writable(model: &PlantModel, point: PointId) -> bool {
    model
        .io_points
        .iter()
        .find(|io| io.id == point)
        .unwrap_or_else(|| panic!("{point:?} is not in the emitted model"))
        .writable
}

#[test]
fn alarms_emit_the_managed_kinds_and_wiring() {
    // Decision 73's station wiring: every alarm is the managed sibling
    // kind, carrying its declared lifecycle surface — `lah` never-
    // shelvable with a read-only `shelve` point, `lal` shelvable with a
    // writable one, and each per-pump fault alarm's `oos`/`suppress`
    // bound to the pump's maintenance-inhibit state.
    let emitted = emit();
    let model = &emitted.model;
    let layout = &emitted.layout;
    let kind = |component: dcs_model::ComponentId| {
        model
            .components
            .iter()
            .find(|instance| instance.id == component)
            .unwrap()
            .kind
            .as_str()
    };
    let parameter = |component: dcs_model::ComponentId, name: &str| {
        model
            .components
            .iter()
            .find(|instance| instance.id == component)
            .unwrap()
            .parameters
            .get(name)
            .copied()
    };

    assert_eq!(
        kind(layout.high_level_alarm.component),
        "managed-latching-alarm"
    );
    assert_eq!(
        kind(layout.low_level_alarm.component),
        "managed-latching-alarm"
    );
    for alarm in [
        &layout.backup_active_alarm,
        &layout.none_available_alarm,
        &layout.all_faulted_alarm,
        &layout.power_fail_alarm,
    ] {
        assert_eq!(kind(alarm.component), "managed-bool-latching-alarm");
    }
    for pump in &layout.pumps {
        for alarm in [&pump.fault_alarm, &pump.thermal_alarm, &pump.moisture_alarm] {
            assert_eq!(kind(alarm.component), "managed-bool-latching-alarm");
        }
    }

    // Every managed instance carries its decision-70 rationalization
    // record and parameters, and its `max_shelve_ticks` bound — `lah`
    // never-shelvable, `lal` under the declared bound.
    for alarm in [
        &layout.high_level_alarm,
        &layout.low_level_alarm,
        &layout.backup_active_alarm,
        &layout.none_available_alarm,
        &layout.all_faulted_alarm,
        &layout.power_fail_alarm,
    ]
    .into_iter()
    .chain(
        layout
            .pumps
            .iter()
            .flat_map(|pump| [&pump.fault_alarm, &pump.thermal_alarm, &pump.moisture_alarm]),
    ) {
        let instance = model
            .components
            .iter()
            .find(|instance| instance.id == alarm.component)
            .unwrap();
        let record = instance
            .rationalization
            .as_ref()
            .unwrap_or_else(|| panic!("{:?} carries no rationalization record", alarm.component));
        for field in [
            &record.consequence,
            &record.required_action,
            &record.reference,
        ] {
            assert!(
                !field.is_empty(),
                "{:?} rationalization is incomplete",
                alarm.component
            );
        }
        for name in ["max_shelve_ticks", "priority", "class", "response_ticks"] {
            assert!(
                matches!(parameter(alarm.component, name), Some(Value::Int(_))),
                "{:?} lacks parameter {name}",
                alarm.component
            );
        }
    }
    assert_eq!(
        parameter(layout.high_level_alarm.component, "max_shelve_ticks"),
        Some(Value::Int(0))
    );
    assert_eq!(
        parameter(layout.low_level_alarm.component, "max_shelve_ticks"),
        Some(Value::Int(8))
    );

    // The declared `shelve` surfaces: `lah`'s bound read-only — the
    // never-shelvable rejection path — `lal`'s bound writable — the
    // receipted operator request — and no other alarm declares the
    // port.
    assert_eq!(
        bound_point(model, layout.high_level_alarm.component, "shelve"),
        layout.high_level_alarm.shelve
    );
    assert!(!writable(model, layout.high_level_alarm.shelve.unwrap()));
    assert_eq!(
        bound_point(model, layout.low_level_alarm.component, "shelve"),
        layout.low_level_alarm.shelve
    );
    assert!(writable(model, layout.low_level_alarm.shelve.unwrap()));
    for alarm in [
        &layout.backup_active_alarm,
        &layout.none_available_alarm,
        &layout.all_faulted_alarm,
        &layout.power_fail_alarm,
    ] {
        assert!(alarm.shelve.is_none());
        assert!(alarm.oos.is_none());
        assert_eq!(bound_point(model, alarm.component, "shelve"), None);
        assert_eq!(bound_point(model, alarm.component, "oos"), None);
        assert_eq!(bound_point(model, alarm.component, "suppress"), None);
    }

    // The designed suppression: each pump's fault alarm binds `oos` to
    // the pump's own writable maintenance-inhibit point and `suppress`
    // to its delivered copy — while the contact alarms declare no
    // lifecycle inputs.
    for pump in &layout.pumps {
        let fault = &pump.fault_alarm;
        assert_eq!(fault.oos, Some(pump.out_of_service));
        assert_eq!(
            bound_point(model, fault.component, "oos"),
            Some(pump.out_of_service)
        );
        let suppress = bound_point(model, fault.component, "suppress")
            .expect("the fault alarm's suppress port is bound");
        assert_ne!(suppress, pump.out_of_service);
        assert!(!writable(model, suppress));
        for alarm in [&pump.thermal_alarm, &pump.moisture_alarm] {
            assert!(alarm.shelve.is_none());
            assert!(alarm.oos.is_none());
            assert_eq!(bound_point(model, alarm.component, "shelve"), None);
            assert_eq!(bound_point(model, alarm.component, "oos"), None);
            assert_eq!(bound_point(model, alarm.component, "suppress"), None);
        }
    }
}

#[test]
fn scripted_run_exercises_the_managed_alarm_lifecycle() {
    let run = managed_run();
    let layout = ids();
    let scans = &run.scans;

    // The pump's out-of-service point lands at scan 5: the bound `oos`
    // asserts `out_of_service` the same scan, and the pass-through
    // copy asserts `suppressed` a scan later — both holding while the
    // point stands and releasing in the same order at scans 31/32.
    assert!(scans[4..30].iter().all(|scan| scan.p2_oos_point));
    assert!(scans[31..].iter().all(|scan| !scan.p2_oos_point));
    assert!(scans[4..30].iter().all(|scan| scan.p2_fault_oos));
    assert!(scans[5..31].iter().all(|scan| scan.p2_fault_suppressed));

    // The offline pump's disconnected run contact proves the motor
    // fault at scan 8 — the `alarm` reports the standing condition
    // from scan 9, countable — while `suppressed` withholds the
    // `unacknowledged` latch: the designed suppression, named but
    // never annunciating.
    assert!(scans[7..38].iter().all(|scan| scan.p2_fault));
    assert!(
        scans[8..31]
            .iter()
            .all(|scan| scan.p2_fault_alarm && scan.p2_fault_suppressed),
        "the fault alarm must report process truth under suppression"
    );
    assert!(
        scans[..31]
            .iter()
            .all(|scan| !(scan.p2_fault_suppressed && scan.p2_fault_unack)),
        "the suppressed alarm must never annunciate"
    );

    // The never-shelvable rejection path: the shelve request on the
    // high-level alarm's read-only point answers `NotWritable` at
    // submission, and `shelved` never asserts.
    assert_eq!(
        run.rejected_shelve.outcome,
        CommandOutcome::Rejected {
            reason: CommandError::NotWritable {
                point: layout.high_level_alarm.shelve.unwrap()
            }
        }
    );
    assert!(
        scans.iter().all(|scan| !scan.lah_shelved),
        "the never-shelvable alarm must never report shelved"
    );

    // The bounded shelve: the request landing at scan 15 holds
    // `shelved` through `lal_max_shelve_ticks` (8) and expires at scan
    // 23 while the request still stands — the request releases at 27.
    assert!(scans[14..22].iter().all(|scan| scan.lal_shelved));
    assert!(
        scans[22..].iter().all(|scan| !scan.lal_shelved),
        "the bound must expire the shelve while the request still stands"
    );

    // Releasing the pump's out-of-service lifts the suppression onto
    // the still-standing fault: `unacknowledged` latches fresh at scan
    // 32 — the re-annunciation — until the operator's ack at 35.
    assert!(scans[31..34].iter().all(|scan| scan.p2_fault_unack));
    assert!(scans[35..].iter().all(|scan| !scan.p2_fault_unack));

    // The recovered contact agrees again: the fault drops at scan 39
    // and its alarm at 40.
    assert!(scans[38..].iter().all(|scan| !scan.p2_fault));
    assert!(scans[39..].iter().all(|scan| !scan.p2_fault_alarm));

    // Every submitted command settled — all applied but the one
    // documented rejection.
    assert!(
        run.receipts.iter().all(|receipt| matches!(
            receipt.outcome,
            CommandOutcome::Applied { .. }
                | CommandOutcome::Accepted { .. }
                | CommandOutcome::Rejected {
                    reason: CommandError::NotWritable { .. }
                }
        )),
        "an unexpected command outcome: {:?}",
        run.receipts
    );
}

#[test]
fn managed_lifecycle_lands_in_the_durable_record_in_seq_order() {
    let run = managed_run();
    let journal = &run.journal;
    let layout = ids();
    let pump = &layout.pumps[1];

    // The durable stream is ordered: `seq` counts from 1, one per
    // entry, never reused.
    for (index, entry) in journal.iter().enumerate() {
        assert_eq!(entry.seq, index as u64 + 1, "journal seq must be ordered");
    }

    let transitions_to = |point: PointId, value: Value| -> Vec<u64> {
        journal
            .iter()
            .filter_map(|entry| match &entry.event {
                JournalEvent::PointChanged { point: p, to, .. } if *p == point && *to == value => {
                    Some(entry.seq)
                }
                _ => None,
            })
            .collect()
    };
    let changes_to = |point: PointId, value: Value| -> Vec<u64> {
        journal
            .iter()
            .filter_map(|entry| match &entry.event {
                JournalEvent::PointChanged {
                    point: p,
                    from: Some(_),
                    to,
                } if *p == point && *to == value => Some(entry.seq),
                _ => None,
            })
            .collect()
    };
    let first = |point: PointId, value: Value| -> u64 {
        *transitions_to(point, value)
            .first()
            .unwrap_or_else(|| panic!("no journaled transition of {point:?} to {value:?}"))
    };
    let released = |point: PointId| -> u64 {
        *changes_to(point, Value::Bool(false))
            .first()
            .unwrap_or_else(|| panic!("{point:?} never journaled its release"))
    };
    let tick_of = |point: PointId, value: Value| -> u64 {
        journal
            .iter()
            .find_map(|entry| match &entry.event {
                JournalEvent::PointChanged { point: p, to, .. } if *p == point && *to == value => {
                    Some(entry.tick.0)
                }
                _ => None,
            })
            .unwrap_or_else(|| panic!("no journaled transition of {point:?} to {value:?}"))
    };
    let yes = Value::Bool(true);

    // The designed-suppression arc: the pump's maintenance-inhibit
    // point asserts, the fault alarm's `out_of_service` and
    // `suppressed` follow it in order, and its release journals the
    // reverse — the standing fault's fresh `unacknowledged` landing
    // with the suppression's release.
    assert!(first(pump.out_of_service, yes) < released(pump.out_of_service));
    assert!(tick_of(pump.out_of_service, yes) <= tick_of(pump.fault_alarm.out_of_service, yes));
    assert!(
        tick_of(pump.fault_alarm.out_of_service, yes) <= tick_of(pump.fault_alarm.suppressed, yes)
    );
    assert!(first(pump.fault_alarm.suppressed, yes) < released(pump.fault_alarm.suppressed));
    // The fault's own transitions are on record — alarm asserting
    // under suppression, clearing after the contact recovers.
    assert!(first(pump.fault, yes) < released(pump.fault));
    assert!(first(pump.fault_alarm.alarm, yes) < released(pump.fault_alarm.alarm));
    // `unacknowledged` latches only on the release's fresh evaluation.
    let unack_asserts: Vec<u64> = journal
        .iter()
        .filter_map(|entry| match &entry.event {
            JournalEvent::PointChanged { point: p, to, .. }
                if *p == pump.fault_alarm.unacknowledged && *to == yes =>
            {
                Some(entry.tick.0)
            }
            _ => None,
        })
        .collect();
    assert_eq!(
        unack_asserts.len(),
        1,
        "the suppressed fault must latch once, on release"
    );

    // The bounded shelve: the request point, the flag's assertion, and
    // its expiry all journal.
    assert!(
        tick_of(layout.low_level_alarm.shelve.unwrap(), yes)
            <= tick_of(layout.low_level_alarm.shelved, yes)
    );
    assert!(first(layout.low_level_alarm.shelved, yes) < released(layout.low_level_alarm.shelved));

    // The never-shelvable alarm's rejected request settles on record —
    // the `NotWritable` receipt journaled beside the attributed
    // commands, and no `shelved` transition ever lands.
    assert!(transitions_to(layout.high_level_alarm.shelved, yes).is_empty());
    let settled: Vec<&CommandReceipt> = journal
        .iter()
        .filter_map(|entry| match &entry.event {
            JournalEvent::CommandSettled { receipt } => Some(receipt),
            _ => None,
        })
        .collect();
    assert!(!settled.is_empty());
    assert!(
        settled
            .iter()
            .any(|receipt| receipt.actor.as_deref() == Some(OPERATOR)),
        "the operator's commands must carry their attribution"
    );
    assert!(
        settled.iter().any(|receipt| matches!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::NotWritable { point }
            } if point == layout.high_level_alarm.shelve.unwrap()
        )),
        "the rejected shelve must settle on record"
    );
    assert!(
        settled.iter().all(|receipt| matches!(
            receipt.outcome,
            CommandOutcome::Applied { .. }
                | CommandOutcome::Accepted { .. }
                | CommandOutcome::Rejected { .. }
        )),
        "a command went unrecorded: {settled:?}"
    );
}

#[test]
fn repeated_managed_lifecycle_runs_are_deterministic() {
    assert_eq!(managed_run(), managed_run());
}
