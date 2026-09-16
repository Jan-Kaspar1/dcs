//! The reference chemical dosing skid, composed through
//! [`dcs_build::dosing::dosing_skid`] — issue #258's artifact.
//!
//! The checked-in documents live at `crates/dcs-demo/fixtures/`:
//! `dosing_skid.json` is the emitted PlantModel,
//! `dosing_skid_dynamics.json` the decision-44 dynamics declaration —
//! a declared process-flow source summed into `flow`, a `scaled_flow`
//! per pump turning its speed demand into a metered rate summed into
//! `injection-rate`, a `dead_time` carrying the transport delay from
//! the injection point to the downstream `discharge-rate` measurement,
//! a `bool_flow` per pump drawing the chemical tank down while its run
//! command stands, the refill line and draws summed into `net-draw`,
//! and an integrator carrying the tank level. These tests assert the
//! helper re-emits the checked-in document exactly, that the document
//! validates and lints clean,
//! assembles through the standard registries, serde-roundtrips, and
//! that a scripted run over the merged dynamics shows the closed
//! dosing loop — and that every run is bit-for-bit deterministic.
//!
//! ## The scripted scenario
//!
//! Each iteration scans the executor, observes the image, applies the
//! script's commands/forcing, then steps the plant one second — so
//! scan `s` observes the world `step s` produced. The dynamics start
//! the tank at 400 L with the refill line holding it while pumps run;
//! the script drains it through the declared `tank-low` (150 L) and
//! `tank-empty` (60 L) thresholds, then exercises every wired
//! contract:
//!
//! - the dose-proportional demand across a flow sweep, `clamped`
//!   asserting at the declared `min_rate`/`max_rate` bounds and on an
//!   out-of-range operator dose;
//! - permissive loss (flow not proven) dropping the demand to
//!   `safe_value` with restart inhibited until permissives return;
//! - a `Bad` pacing flow engaging the declared `on_bad_flow = 2`
//!   fallback — `fallback_active` asserted, the fallback demand marked
//!   untrusted, and the interlock refusing it;
//! - a disconnected run contact proving the motor fault, handing duty
//!   to the standby pump, and the `deviation-monitor` flagging the
//!   sustained measured-consumption gap;
//! - the pump's own fault / no-discharge contacts dropping
//!   `avail_i` and raising their alarms;
//! - bund flood and external inhibit each tripping the chain;
//! - manual takeover through `manual-station`;
//! - every declared alarm asserting and clearing its latch through
//!   the writable `ack` points, journaled as settled receipts.

use dcs_assembly::{DriverRegistry, FanoutDriver, assemble, resolve_drivers};
use dcs_build::dosing::{DosingSkid, DosingSkidConfig, DosingSkidLayout, dosing_skid};
use dcs_build::station::AlarmLayout;
use dcs_build::{PointId, Value};
use dcs_core::{
    Command, CommandOutcome, CommandReceipt, IoDriver, Quality, QualityReason, ValueKind,
};
use dcs_model::PlantModel;
use dcs_sim::{Fault, ProcessElement};

/// The checked-in emitted document.
const MODEL_JSON: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/dosing_skid.json"
);
/// The checked-in dynamics declaration merged over it.
const DYNAMICS_JSON: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/dosing_skid_dynamics.json"
);

/// The plant step each scan period covers, in seconds.
const DT: f64 = 1.0;
/// The scripted run length — covers every phase below plus settling.
const SCANS: u64 = 235;

/// Emits the skid through the reference configuration.
fn emit() -> DosingSkid {
    dosing_skid(&DosingSkidConfig::reference()).unwrap()
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
    /// The measured process flow the ratio paces on.
    flow: f64,
    /// Whether the flow sample reads `Good`.
    flow_good: bool,
    /// The chemical tank level.
    tank_level: f64,
    /// The measured discharge rate — the measured-consumption signal.
    discharge: f64,
    /// The injected rate at the injection point — upstream of the
    /// transport delay to the discharge measurement.
    injection: f64,
    /// The ratio kind's raw paced demand.
    ratio_demand: f64,
    /// Whether the paced demand sample reads `Good`.
    ratio_good: bool,
    /// The permissive-gated demand.
    gated: f64,
    /// The commanded chemical rate.
    demand: f64,
    /// The chain's stage-count demand.
    stage: i64,
    /// The group's duty holder, `0` while none holds it.
    duty: i64,
    /// How many pumps the group commands.
    staged: i64,
    /// Demand saturating at a declared bound.
    clamped: bool,
    /// The declared bad-flow fallback engaged.
    fallback: bool,
    /// The aggregated dosing-permitted condition.
    permitted: bool,
    /// The demand interlock's trip flag.
    tripped: bool,
    /// The manual station's selection report.
    manual_active: bool,
    /// The group's no-pump-available flag.
    none_available: bool,
    /// The group's every-pump-faulted flag.
    all_faulted: bool,
    /// The commanded chemical total.
    total: f64,
    /// The windowed relative deviation.
    deviation: f64,
    /// The dose-not-confirmed condition.
    deviating: bool,
    /// The field run commands, per pump.
    cmd: [bool; 2],
    /// The run contacts the loopback reports, per pump.
    run: [bool; 2],
    /// The field speed demands, per pump.
    speed: [f64; 2],
    /// The accumulated stroke counts, per pump.
    strokes: [i64; 2],
    /// The proven motor faults, per pump.
    motor_fault: [bool; 2],
    low_alarm: bool,
    low_unack: bool,
    empty_alarm: bool,
    empty_unack: bool,
    pacing_alarm: bool,
    pacing_unack: bool,
    bund_alarm: bool,
    bund_unack: bool,
    ext_alarm: bool,
    ext_unack: bool,
    dev_alarm: bool,
    dev_unack: bool,
    p1_fault_alarm: bool,
    p1_fault_unack: bool,
    p1_pfault_alarm: bool,
    p1_pfault_unack: bool,
    p2_fault_alarm: bool,
    p2_fault_unack: bool,
    p2_pfault_alarm: bool,
    p2_pfault_unack: bool,
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

/// The ids the run addresses — the emitted skid's layout.
fn ids() -> DosingSkidLayout {
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
        .expect("the skid's devices all serve the local sim");
    let mut executor = assemble(&model, &dcs_controller::registry(), &driver).unwrap();

    // The declared process flow, the flow-proven contact, and the
    // refill line holding the tank while a pump runs — plus one step
    // so scan 1 sees the dynamics' declared state rather than the
    // binding's neutral seed.
    sim.write(layout.flow_source, Value::Float(20.0)).unwrap();
    sim.write(layout.flow_proven, Value::Bool(true)).unwrap();
    sim.write(layout.tank_refill, Value::Float(10.0)).unwrap();
    driver.step(DT).unwrap();

    let mut scans = Vec::with_capacity(SCANS as usize);
    let observe = |executor: &dcs_runtime::Executor<'_>, scans: &mut Vec<Scan>| {
        let sample = |point| executor.sample(point).unwrap();
        scans.push(Scan {
            flow: float(sample(layout.flow)),
            flow_good: sample(layout.flow).quality.is_good(),
            tank_level: float(sample(layout.tank_level)),
            discharge: float(sample(layout.discharge_rate)),
            injection: float(sample(layout.injection_rate)),
            ratio_demand: float(sample(layout.ratio_demand)),
            ratio_good: sample(layout.ratio_demand).quality.is_good(),
            gated: float(sample(layout.gated_demand)),
            demand: float(sample(layout.demand)),
            stage: int(sample(layout.stage_demand)),
            duty: int(sample(layout.duty)),
            staged: int(sample(layout.staged)),
            clamped: bool_(sample(layout.clamped)),
            fallback: bool_(sample(layout.fallback_active)),
            permitted: bool_(sample(layout.dosing_permitted)),
            tripped: bool_(sample(layout.interlock_tripped)),
            manual_active: bool_(sample(layout.manual_active)),
            none_available: bool_(sample(layout.none_available)),
            all_faulted: bool_(sample(layout.all_faulted)),
            total: float(sample(layout.dose_total)),
            deviation: float(sample(layout.deviation)),
            deviating: bool_(sample(layout.deviating)),
            cmd: [
                bool_(sample(layout.pumps[0].cmd)),
                bool_(sample(layout.pumps[1].cmd)),
            ],
            run: [
                bool_(sample(layout.pumps[0].run)),
                bool_(sample(layout.pumps[1].run)),
            ],
            speed: [
                float(sample(layout.pumps[0].speed)),
                float(sample(layout.pumps[1].speed)),
            ],
            strokes: [
                int(sample(layout.pumps[0].strokes)),
                int(sample(layout.pumps[1].strokes)),
            ],
            motor_fault: [
                bool_(sample(layout.pumps[0].motor_fault)),
                bool_(sample(layout.pumps[1].motor_fault)),
            ],
            low_alarm: bool_(sample(layout.tank_low_alarm.alarm)),
            low_unack: bool_(sample(layout.tank_low_alarm.unacknowledged)),
            empty_alarm: bool_(sample(layout.tank_empty_alarm.alarm)),
            empty_unack: bool_(sample(layout.tank_empty_alarm.unacknowledged)),
            pacing_alarm: bool_(sample(layout.pacing_alarm.alarm)),
            pacing_unack: bool_(sample(layout.pacing_alarm.unacknowledged)),
            bund_alarm: bool_(sample(layout.bund_alarm.alarm)),
            bund_unack: bool_(sample(layout.bund_alarm.unacknowledged)),
            ext_alarm: bool_(sample(layout.external_alarm.alarm)),
            ext_unack: bool_(sample(layout.external_alarm.unacknowledged)),
            dev_alarm: bool_(sample(layout.deviation_alarm.alarm)),
            dev_unack: bool_(sample(layout.deviation_alarm.unacknowledged)),
            p1_fault_alarm: bool_(sample(layout.pumps[0].fault_alarm.alarm)),
            p1_fault_unack: bool_(sample(layout.pumps[0].fault_alarm.unacknowledged)),
            p1_pfault_alarm: bool_(sample(layout.pumps[0].pump_fault_alarm.alarm)),
            p1_pfault_unack: bool_(sample(layout.pumps[0].pump_fault_alarm.unacknowledged)),
            p2_fault_alarm: bool_(sample(layout.pumps[1].fault_alarm.alarm)),
            p2_fault_unack: bool_(sample(layout.pumps[1].fault_alarm.unacknowledged)),
            p2_pfault_alarm: bool_(sample(layout.pumps[1].pump_fault_alarm.alarm)),
            p2_pfault_unack: bool_(sample(layout.pumps[1].pump_fault_alarm.unacknowledged)),
        });
    };

    for scan in 1..=SCANS {
        executor.scan().unwrap();
        observe(&executor, &mut scans);
        match scan {
            // Pump 1's stroke pulse — five rising edges the counter
            // accumulates.
            20..=29 if scan % 2 == 0 => sim
                .write(layout.pumps[0].stroke, Value::Bool(true))
                .unwrap(),
            20..=29 => sim
                .write(layout.pumps[0].stroke, Value::Bool(false))
                .unwrap(),
            // Drain the tank through the declared low and empty
            // thresholds — the pump's draw alone carries it.
            18 => sim.write(layout.tank_refill, Value::Float(0.0)).unwrap(),
            // The low and empty alarms latched — acknowledge both.
            60 => {
                ack(&mut executor, &layout.tank_low_alarm);
                ack(&mut executor, &layout.tank_empty_alarm);
            }
            62 => {
                release_ack(&mut executor, &layout.tank_low_alarm);
                release_ack(&mut executor, &layout.tank_empty_alarm);
                // The delivery arrives: the refill line lifts the tank
                // back above the empty inhibit, then holds it.
                sim.write(layout.tank_refill, Value::Float(50.0)).unwrap();
            }
            68 => sim.write(layout.tank_refill, Value::Float(10.0)).unwrap(),
            // The flow sweep: dose-proportional demand through and past
            // the declared rate bounds.
            76 => sim.write(layout.flow_source, Value::Float(15.0)).unwrap(),
            82 => sim.write(layout.flow_source, Value::Float(60.0)).unwrap(),
            88 => sim.write(layout.flow_source, Value::Float(3.0)).unwrap(),
            94 => sim.write(layout.flow_source, Value::Float(20.0)).unwrap(),
            // An out-of-range operator dose saturates at `max_dose` —
            // `clamped` asserts on the dose bound.
            100 => write(
                &mut executor,
                layout.dose,
                ValueKind::Float,
                Value::Float(5.0),
            ),
            106 => write(
                &mut executor,
                layout.dose,
                ValueKind::Float,
                Value::Float(2.0),
            ),
            // Flow not proven: the permissive drops and the demand
            // falls to `safe_value` until it returns.
            112 => sim.write(layout.flow_proven, Value::Bool(false)).unwrap(),
            120 => sim.write(layout.flow_proven, Value::Bool(true)).unwrap(),
            // The pacing flow goes Bad: the declared `on_bad_flow = 2`
            // response drives `fallback_rate` marked untrusted, and
            // `fallback_active` raises its alarm.
            128 => sim
                .inject_fault(
                    layout.flow_source,
                    Fault::Quality(Quality::Bad(QualityReason::CommunicationFault)),
                )
                .unwrap(),
            136 => ack(&mut executor, &layout.pacing_alarm),
            138 => {
                release_ack(&mut executor, &layout.pacing_alarm);
                sim.clear_fault(layout.flow_source).unwrap();
            }
            // Pump 1's run contact disconnects: the motor proves the
            // fault, the group hands duty to the standby, and the
            // delivery gap trips the deviation monitor.
            146 => sim
                .inject_fault(layout.pumps[0].run, Fault::Disconnected)
                .unwrap(),
            152 => {
                ack(&mut executor, &layout.pumps[0].fault_alarm);
                ack(&mut executor, &layout.deviation_alarm);
            }
            154 => {
                release_ack(&mut executor, &layout.pumps[0].fault_alarm);
                release_ack(&mut executor, &layout.deviation_alarm);
            }
            // Pump 1's own no-discharge contact — the availability leg
            // drops and the pump-fault alarm latches.
            158 => sim
                .write(layout.pumps[0].pump_fault, Value::Bool(true))
                .unwrap(),
            162 => ack(&mut executor, &layout.pumps[0].pump_fault_alarm),
            164 => {
                release_ack(&mut executor, &layout.pumps[0].pump_fault_alarm);
                // Pump 1's run contact recovers; the pump-fault contact
                // still stands, holding it out of service.
                sim.clear_fault(layout.pumps[0].run).unwrap();
            }
            // Pump 2's run contact disconnects while its own fault
            // contact also reports — with pump 1 held out, every pump
            // is unavailable and the chain trips on `none_available`.
            168 => {
                sim.inject_fault(layout.pumps[1].run, Fault::Disconnected)
                    .unwrap();
                sim.write(layout.pumps[1].pump_fault, Value::Bool(true))
                    .unwrap();
            }
            174 => {
                ack(&mut executor, &layout.pumps[1].fault_alarm);
                ack(&mut executor, &layout.pumps[1].pump_fault_alarm);
            }
            176 => {
                release_ack(&mut executor, &layout.pumps[1].fault_alarm);
                release_ack(&mut executor, &layout.pumps[1].pump_fault_alarm);
                sim.clear_fault(layout.pumps[1].run).unwrap();
                sim.write(layout.pumps[0].pump_fault, Value::Bool(false))
                    .unwrap();
                sim.write(layout.pumps[1].pump_fault, Value::Bool(false))
                    .unwrap();
            }
            // Bund flood: the contact alarms and trips the chain.
            184 => sim.write(layout.bund_flood, Value::Bool(true)).unwrap(),
            190 => ack(&mut executor, &layout.bund_alarm),
            192 => {
                release_ack(&mut executor, &layout.bund_alarm);
                sim.write(layout.bund_flood, Value::Bool(false)).unwrap();
            }
            // External (wet-weather) inhibit: same shape.
            198 => sim
                .write(layout.external_inhibit, Value::Bool(true))
                .unwrap(),
            204 => ack(&mut executor, &layout.external_alarm),
            206 => {
                release_ack(&mut executor, &layout.external_alarm);
                sim.write(layout.external_inhibit, Value::Bool(false))
                    .unwrap();
            }
            // Manual takeover: the operator's manual rate drives the
            // demand while `manual_active` reports the selection.
            214 => {
                write(
                    &mut executor,
                    layout.manual_mode,
                    ValueKind::Bool,
                    Value::Bool(true),
                );
                write(
                    &mut executor,
                    layout.manual_rate,
                    ValueKind::Float,
                    Value::Float(55.0),
                );
            }
            222 => write(
                &mut executor,
                layout.manual_mode,
                ValueKind::Bool,
                Value::Bool(false),
            ),
            // The deviation latch re-arms on every delivery transient;
            // a final ack leaves the journal with every latch cleared.
            226 => ack(&mut executor, &layout.deviation_alarm),
            228 => release_ack(&mut executor, &layout.deviation_alarm),
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
    // Decision 74's sweep: every alarm's `alarm`/`unacknowledged`
    // status points, the mode and managed-state flags decision 75
    // names, and the protection-relevant status points the
    // composition carries are declared `journaled` — their value
    // transitions join the durable journal as `point_changed`
    // entries. The flag stays opt-in: receipts, demand copies, the
    // stroke churn, and the float measurements and setpoints keep
    // their own paths.
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
        layout.flow_proven,
        layout.bund_flood,
        layout.external_inhibit,
        layout.manual_mode,
        layout.fallback_active,
        layout.none_available,
        layout.dosing_permitted,
        layout.interlock_tripped,
        layout.manual_active,
        layout.all_faulted,
        layout.deviating,
    ];
    let mut off_record = vec![
        layout.flow,
        layout.flow_source,
        layout.tank_level,
        layout.discharge_rate,
        layout.net_draw,
        layout.tank_refill,
        layout.ratio_demand,
        layout.gated_demand,
        layout.demand,
        layout.stage_demand,
        layout.duty,
        layout.staged,
        layout.clamped,
        layout.dose_total,
        layout.deviation,
        layout.dose,
        layout.manual_rate,
        layout.totalizer_reset,
    ];
    for alarm in [
        &layout.tank_low_alarm,
        &layout.tank_empty_alarm,
        &layout.pacing_alarm,
        &layout.bund_alarm,
        &layout.external_alarm,
        &layout.deviation_alarm,
    ] {
        record.extend([alarm.alarm, alarm.unacknowledged]);
        // The ack point's writes are already the attributed,
        // receipted record — it stays off the journaled set.
        off_record.push(alarm.ack);
    }
    for pump in &layout.pumps {
        record.extend([
            pump.run,
            pump.local,
            pump.pump_fault,
            pump.out_of_service,
            pump.avail,
            pump.motor_fault,
            pump.speed_gated,
        ]);
        off_record.extend([
            pump.cmd,
            pump.speed,
            pump.draw,
            pump.rate,
            pump.stroke,
            pump.stroke_reset,
            pump.group_cmd,
            pump.speed_eng,
            pump.strokes,
            pump.strokes_done,
        ]);
        for alarm in [&pump.fault_alarm, &pump.pump_fault_alarm] {
            record.extend([alarm.alarm, alarm.unacknowledged]);
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
fn scripted_run_shows_the_closed_dosing_loop() {
    let run = run();
    let scans = &run.scans;
    let at = |scan: u64| &scans[scan as usize - 1];

    // The dose-proportional demand: at the declared 2 mg/L dose a 20
    // m3/h flow paces 40 g/h, within the 10–100 g/h bounds, and the
    // loop closes — the duty pump runs and the metered discharge reads
    // the commanded rate.
    assert!(at(12).demand == 40.0, "scan 12: {:?}", at(12));
    assert!(at(12).ratio_good && at(12).flow_good);
    assert!(!at(12).clamped && !at(12).tripped && at(12).permitted);
    assert!(at(12).duty == 1 && at(12).run[0] && at(12).cmd[0]);
    assert!(
        at(14).discharge == 40.0,
        "the metered discharge must track the command: {:?}",
        at(14)
    );
    assert!(at(14).speed[0] > 0.0 && at(14).speed[1] == 0.0);

    // The transport delay: the downstream measurement replays the
    // injected rate exactly `delay` scans later — every injected step
    // lands on `discharge` three scans after `injection` reads it.
    for needle in [40.0, 80.0] {
        let injected = scans
            .iter()
            .position(|scan| scan.injection == needle)
            .unwrap_or_else(|| panic!("injection never read {needle}"));
        let measured = scans
            .iter()
            .position(|scan| scan.discharge == needle)
            .unwrap_or_else(|| panic!("discharge never read {needle}"));
        assert_eq!(
            measured - injected,
            3,
            "the injected rate must reach the measurement `delay` scans later: \
             injection@{injected} discharge@{measured}"
        );
    }

    // The flow sweep: demand follows dose × flow through the declared
    // bounds — 15 m3/h paces 30 g/h, 60 m3/h saturates at `max_rate`
    // with `clamped`, 3 m3/h saturates at `min_rate`, and 20 m3/h
    // returns mid-range with the flag clear.
    assert!(at(81).demand == 30.0, "scan 81: {:?}", at(81).demand);
    assert!(
        at(87).demand == 100.0 && at(87).clamped,
        "the upper rate bound must clamp: {:?}",
        at(87)
    );
    assert!(
        at(93).demand == 10.0 && at(93).clamped,
        "the lower rate bound must clamp: {:?}",
        at(93)
    );
    assert!(at(99).demand == 40.0 && !at(99).clamped, "{:?}", at(99));

    // The operator dose write rides the journaled path: 5 mg/L exceeds
    // `max_dose` (4.0), so the dose term clamps and the demand reads
    // 80 g/h with `clamped` asserted.
    assert!(
        at(105).demand == 80.0 && at(105).clamped,
        "the dose bound must clamp: {:?}",
        at(105)
    );
    assert!(at(111).demand == 40.0 && !at(111).clamped, "{:?}", at(111));

    // The commanded total accumulates while the demand stands.
    assert!(
        at(110).total > at(20).total,
        "the totalizer must integrate the demand: {:?} -> {:?}",
        at(20).total,
        at(110).total
    );
    // The stroke counter accumulated pump 1's pulses.
    assert!(
        scans.iter().any(|scan| scan.strokes[0] >= 5),
        "the stroke counter must count the pulse train: {:?}",
        scans.iter().map(|scan| scan.strokes[0]).collect::<Vec<_>>()
    );

    // The tank drew down through the declared thresholds: the low
    // alarm tripped at 150 L and the empty alarm at 60 L, each
    // latching until its scan-60 ack — and the empty condition dropped
    // the permissive, driving the demand to `safe_value` while the
    // pump stopped.
    let low = scans.iter().position(|scan| scan.low_alarm).unwrap();
    assert!(
        at(low as u64 + 1).tank_level <= 150.0 + 1e-9,
        "tank-low tripped above its limit: {:?}",
        at(low as u64 + 1)
    );
    let empty = scans.iter().position(|scan| scan.empty_alarm).unwrap();
    assert!(
        at(empty as u64 + 1).tank_level <= 60.0 + 1e-9,
        "tank-empty tripped above its limit: {:?}",
        at(empty as u64 + 1)
    );
    assert!(
        scans
            .iter()
            .any(|scan| scan.empty_alarm && !scan.permitted && scan.demand == 0.0),
        "the empty condition must drop the permissive and the demand"
    );
    assert!(
        scans[..62]
            .iter()
            .any(|scan| scan.low_unack && scan.empty_unack)
    );
    // The refill lifted the tank back above the empty inhibit and the
    // skid restarted — restart inhibited only until permissives
    // returned.
    assert!(
        scans[62..76]
            .iter()
            .any(|scan| scan.permitted && scan.demand == 40.0 && scan.run.iter().any(|&r| r)),
        "the skid must resume once the tank refills"
    );

    // Flow not proven: the permissive drops, the interlock drives
    // `safe_value`, and the restart stays inhibited until the contact
    // returns.
    assert!(
        scans[116..120]
            .iter()
            .all(|scan| !scan.permitted && scan.tripped && scan.demand == 0.0),
        "loss of flow-proven must hold the demand at the safe value"
    );
    assert!(
        scans[117..125]
            .iter()
            .all(|scan| !scan.cmd[0] && !scan.cmd[1]),
        "no pump may be commanded while flow is unproven"
    );
    assert!(
        scans[118..126]
            .iter()
            .all(|scan| !scan.run[0] && !scan.run[1]),
        "no pump may run while flow is unproven"
    );
    assert!(
        scans[124..128].iter().all(|scan| scan.demand == 40.0),
        "the skid must resume when flow-proven returns"
    );

    // The Bad pacing flow engages the declared `on_bad_flow = 2`
    // response: `ratio-demand` stands at `fallback_rate` (40 g/h)
    // marked untrusted, `fallback_active` asserts, its alarm latches —
    // and the interlock refuses the untrusted demand, so the skid
    // stops dosing until the signal recovers.
    assert!(
        scans[130..138]
            .iter()
            .any(|scan| scan.fallback && !scan.flow_good && !scan.ratio_good),
        "the declared fallback must engage on a Bad pacing flow"
    );
    assert!(
        scans[130..138].iter().any(|scan| scan.ratio_demand == 40.0),
        "the fallback rate must stand at the ratio's output"
    );
    assert!(
        scans[130..138]
            .iter()
            .any(|scan| scan.pacing_alarm && scan.pacing_unack),
        "the pacing-loss alarm must assert and latch"
    );
    assert!(
        scans[130..138].iter().all(|scan| scan.demand == 0.0),
        "the interlock must refuse an untrusted demand"
    );
    assert!(scans[138..].iter().all(|scan| !scan.pacing_unack));

    // The disconnected run contact proves the motor fault: the alarm
    // latches until the scan-152 ack, the group hands duty to pump 2,
    // and delivery resumes on the standby.
    assert!(
        scans[146..152]
            .iter()
            .any(|scan| scan.p1_fault_alarm && scan.p1_fault_unack),
        "the motor fault must latch its alarm"
    );
    assert!(scans[152..].iter().all(|scan| !scan.p1_fault_unack));
    assert!(
        scans[148..158]
            .iter()
            .any(|scan| scan.duty == 2 && scan.run[1]),
        "the standby pump must take duty on the proven fault"
    );
    assert!(
        scans[152..168].iter().any(|scan| scan.discharge == 40.0),
        "delivery must resume on the standby pump"
    );

    // The sustained measured-consumption gap tripped the deviation
    // window: `deviating` asserts and the dose-not-confirmed alarm
    // latches until its scan-152 ack.
    assert!(
        scans[146..160]
            .iter()
            .any(|scan| scan.deviating && scan.deviation < -0.2),
        "the measured-consumption gap must trip the deviation window"
    );
    assert!(
        scans[146..160]
            .iter()
            .any(|scan| scan.dev_alarm && scan.dev_unack),
        "the dose-not-confirmed alarm must assert and latch"
    );
    // The latch clears on the ack and re-arms only on a fresh
    // divergence — the next delivery transient re-latches it.
    assert!(scans[154..173].iter().all(|scan| !scan.dev_unack));
    // The final ack (scan 226) clears the manual-step latch; nothing
    // diverges again, so the journal ends with the latch clear.
    assert!(scans[230..].iter().all(|scan| !scan.dev_unack));

    // The pump-fault contacts drop availability: pump 1's contact holds
    // it out while pump 2's fault proves — `none_available` trips the
    // chain and every pump stops until both contacts clear.
    assert!(
        scans[160..166]
            .iter()
            .any(|scan| scan.p1_pfault_alarm && scan.p1_pfault_unack),
        "the pump-fault contact must latch its alarm"
    );
    assert!(
        scans[170..176]
            .iter()
            .any(|scan| scan.p2_fault_alarm && scan.p2_fault_unack),
        "pump 2's proven fault must latch its alarm"
    );
    assert!(
        scans[170..176].iter().any(|scan| scan.p2_pfault_alarm),
        "pump 2's fault contact must raise its alarm"
    );
    assert!(
        scans[170..180]
            .iter()
            .any(|scan| scan.none_available && scan.demand == 0.0),
        "no pump available must drop the demand"
    );
    assert!(
        scans[176..]
            .iter()
            .all(|scan| !scan.p2_fault_unack && !scan.p2_pfault_unack)
    );

    // Bund flood: the field contact alarms and trips the chain until
    // cleared; the ack releases its latch.
    assert!(
        scans[186..192]
            .iter()
            .any(|scan| scan.bund_alarm && scan.bund_unack),
        "the bund-flood alarm must assert and latch"
    );
    assert!(
        scans[185..194]
            .iter()
            .all(|scan| scan.tripped && scan.demand == 0.0),
        "bund flood must hold the demand at the safe value"
    );
    assert!(scans[192..].iter().all(|scan| !scan.bund_unack));

    // External inhibit: the same shape — alarm, trip, safe demand.
    assert!(
        scans[200..206]
            .iter()
            .any(|scan| scan.ext_alarm && scan.ext_unack),
        "the external-inhibit alarm must assert and latch"
    );
    assert!(
        scans[200..209].iter().all(|scan| scan.demand == 0.0),
        "the external inhibit must hold the demand at the safe value"
    );
    assert!(scans[206..].iter().all(|scan| !scan.ext_unack));

    // Manual takeover: `manual_active` reports the selection and the
    // operator's 55 g/h manual rate drives the demand.
    assert!(
        scans[216..222]
            .iter()
            .any(|scan| scan.manual_active && scan.demand == 55.0),
        "manual mode must drive the operator's rate"
    );
    assert!(
        scans[224..235]
            .iter()
            .any(|scan| !scan.manual_active && scan.demand == 40.0),
        "releasing manual must return to the paced demand"
    );

    // Every declared alarm asserted and cleared its latch through the
    // writable ack points — and every command landed, journaled as a
    // settled receipt.
    for (alarm, unack, name) in [
        (
            scans.iter().any(|s| s.low_alarm),
            scans.iter().any(|s| s.low_unack),
            "tank-low",
        ),
        (
            scans.iter().any(|s| s.empty_alarm),
            scans.iter().any(|s| s.empty_unack),
            "tank-empty",
        ),
        (
            scans.iter().any(|s| s.pacing_alarm),
            scans.iter().any(|s| s.pacing_unack),
            "pacing-lost",
        ),
        (
            scans.iter().any(|s| s.bund_alarm),
            scans.iter().any(|s| s.bund_unack),
            "bund-flood",
        ),
        (
            scans.iter().any(|s| s.ext_alarm),
            scans.iter().any(|s| s.ext_unack),
            "external-inhibit",
        ),
        (
            scans.iter().any(|s| s.dev_alarm),
            scans.iter().any(|s| s.dev_unack),
            "dose-not-confirmed",
        ),
        (
            scans.iter().any(|s| s.p1_fault_alarm),
            scans.iter().any(|s| s.p1_fault_unack),
            "p201 motor fault",
        ),
        (
            scans.iter().any(|s| s.p1_pfault_alarm),
            scans.iter().any(|s| s.p1_pfault_unack),
            "p201 pump fault",
        ),
        (
            scans.iter().any(|s| s.p2_fault_alarm),
            scans.iter().any(|s| s.p2_fault_unack),
            "p202 motor fault",
        ),
        (
            scans.iter().any(|s| s.p2_pfault_alarm),
            scans.iter().any(|s| s.p2_pfault_unack),
            "p202 pump fault",
        ),
    ] {
        assert!(alarm, "the {name} alarm never asserted");
        assert!(unack, "the {name} alarm never latched unacknowledged");
    }
    assert!(
        at(SCANS).total > at(150).total,
        "the commanded total must keep accumulating"
    );
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
