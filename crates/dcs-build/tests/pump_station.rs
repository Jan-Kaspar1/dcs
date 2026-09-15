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

use dcs_assembly::{DriverRegistry, FanoutDriver, assemble, resolve_drivers};
use dcs_build::station::{
    AlarmLayout, PumpStation, PumpStationConfig, PumpStationLayout, pumping_station,
};
use dcs_build::{PointId, Value};
use dcs_core::{
    Command, CommandOutcome, CommandReceipt, IoDriver, Quality, QualityReason, ValueKind,
};
use dcs_model::PlantModel;
use dcs_sim::{Fault, ProcessElement};

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
fn ack(executor: &mut dcs_runtime::Executor<'_>, alarm: &AlarmLayout) {
    write(executor, alarm.ack, ValueKind::Bool, Value::Bool(true));
}

/// Releases the alarm's writable ack point.
fn release_ack(executor: &mut dcs_runtime::Executor<'_>, alarm: &AlarmLayout) {
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
}
