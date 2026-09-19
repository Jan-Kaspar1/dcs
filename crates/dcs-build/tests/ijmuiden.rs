//! The IJmuiden-pattern consequential-annunciation scenario, composed
//! through [`dcs_build::ijmuiden::ijmuiden`] — issue #297's artifact,
//! decisions 75 and 77.
//!
//! The checked-in documents live at `crates/dcs-demo/fixtures/`:
//! `ijmuiden.json` is the emitted PlantModel, `ijmuiden_dynamics.json`
//! the dynamics declaration — the gate's confirmed position admitting
//! inflow through a `scaled_flow`, the independent layer's actuation
//! drawing through a `bool_flow`, both summed with the declared tide
//! into the level integrator. These tests assert the helper re-emits
//! the checked-in document exactly, that the document validates and
//! documents its one lint finding, assembles through the standard
//! registries, serde-roundtrips, and that a scripted monitor-backed
//! run shows the whole consequential-annunciation chain — and that
//! every run is bit-for-bit deterministic.
//!
//! ## The scripted scenario
//!
//! Each iteration runs one monitor-paced scan over the HTTP surface —
//! `POST /scan` — observes the snapshot, applies the script's commands
//! and forcing, then steps the plant one second — so scan `s`
//! observes the world `step s` produced and the scripted devices'
//! tick-`s` entries. The dynamics start the level at 3.0 m on a
//! +0.13 m/step net flow: it climbs past `start` and `lag_start` into
//! `high` until the independent layer's −0.45 draw pulls it back.
//!
//! - scan 6: the field fault switches `gate-mode` to local manual —
//!   `manual_active` and the never-shelvable mode alarm assert, and
//!   the transition lands in the durable journal;
//! - the operator's manual demand of 0 commands the gate safe while
//!   the confirmed-open `gate-fb` holds — `valve`'s `discrepancy`
//!   asserts the demanded-safe-but-confirmed-open mismatch;
//! - the rising level trips the managed high-level alarm and walks the
//!   chain's ladder, while the composed divergence detector flags the
//!   sustained rise;
//! - the remote repeater freezes (its last scripted update is the
//!   declared schedule's) — `level-remote` presents `Uncertain(Stale)`
//!   past its `stale_after_ticks` budget, and the `Bad` primary flips
//!   the failover onto it;
//! - the independent layer trips at scan 24: the scripted inflow
//!   carries the canal level across the declared high-high bound, the
//!   dynamics' `threshold` element asserts `sis-active` — no field
//!   write touches the contact — `sis-trip` reports the same scan,
//!   and the dynamics' relief acts on the process whether or not the
//!   controller scans; the discrepancy alarm's declared `suppress`
//!   holds its standing truth out of the annunciation while the layer
//!   owns the hazard;
//! - the shelving bound, the manual unshelve, and the out-of-service
//!   path all run on the high-level alarm;
//! - the operator's `sis-bypass` write rides the receipted,
//!   attributed command path onto the writable field point;
//! - every lifecycle transition lands in the journal in `seq` order.

use dcs_assembly::{DriverRegistry, FanoutDriver, assemble, resolve_drivers};
use dcs_build::ijmuiden::{
    Ijmuiden, IjmuidenConfig, IjmuidenLayout, ManagedAlarmLayout, SIS_HIGH_HIGH, SIS_RELEASE,
    ijmuiden, points, schedule,
};
use dcs_build::station::AlarmLayout;
use dcs_build::{PointId, Value};
use dcs_core::{
    Command, CommandOutcome, IoDriver, JournalEntry, JournalEvent, PointTelemetry, Quality,
    QualityReason, Sample, TelemetrySnapshot, ValueKind,
};
use dcs_model::PlantModel;
use dcs_monitor::{Monitor, MonitorClient};
use dcs_sim::{Fault, ProcessElement};
use std::thread;

/// The checked-in emitted document.
const MODEL_JSON: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/ijmuiden.json"
);
/// The checked-in dynamics declaration merged over it.
const DYNAMICS_JSON: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/ijmuiden_dynamics.json"
);

/// The plant step each scan period covers, in seconds.
const DT: f64 = 1.0;
/// The scripted run length — the second excursion's re-latch plus
/// settling.
const SCANS: u64 = 78;
/// The actor every operator command carries — the attributed, receipted
/// path decision 77 names for the bypass.
const OPERATOR: &str = "operator";

/// Emits the scenario through the reference configuration.
fn emit() -> Ijmuiden {
    ijmuiden(&IjmuidenConfig::reference()).unwrap()
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

/// The ids the run addresses — the emitted scenario's layout.
fn ids() -> IjmuidenLayout {
    emit().layout
}

/// One scan's observable record.
#[derive(Debug, PartialEq)]
struct Scan {
    /// The canal level the dynamics' integrator produces.
    level: f64,
    /// The remote repeater's served sample — frozen through its
    /// scripted silence.
    remote: f64,
    /// The remote repeater's served quality — `Uncertain(Stale)`
    /// while it is frozen.
    remote_quality: Quality,
    /// The failover-selected level the alarm path reads.
    selected: f64,
    /// The filtered level the chain and alarms control on.
    filtered: f64,
    /// The gate demand the station drives.
    gate_demand: f64,
    /// The gate's confirmed position.
    gate_fb: f64,
    /// The gate command the field carries.
    gate_cmd: f64,
    /// The protection layer's emergency draw.
    sis_draw: f64,
    gate_mode: bool,
    manual_active: bool,
    discrepancy: bool,
    backup_active: bool,
    backup_unhealthy: bool,
    deviating: bool,
    duty_call: bool,
    lag_call: bool,
    below_cutoff: bool,
    high_level: bool,
    /// The chain's stage-count demand.
    chain_demand: i64,
    sis_active: bool,
    sis_available: bool,
    sis_fault: bool,
    sis_trip: bool,
    sis_proof_test: bool,
    sis_bypass: bool,
    // The managed high-level alarm's full status surface.
    lah_alarm: bool,
    lah_unack: bool,
    lah_shelved: bool,
    lah_suppressed: bool,
    lah_oos: bool,
    // The never-shelvable mode alarm.
    mode_alarm: bool,
    mode_unack: bool,
    mode_shelved: bool,
    // The designed-suppression discrepancy alarm.
    disc_alarm: bool,
    disc_unack: bool,
    disc_suppressed: bool,
    // The unmanaged annunciations.
    ror_alarm: bool,
    ror_unack: bool,
    backup_alarm: bool,
    backup_unack: bool,
    buh_alarm: bool,
    buh_unack: bool,
    trip_alarm: bool,
    trip_unack: bool,
    bypass_alarm: bool,
    bypass_unack: bool,
    fault_alarm: bool,
    fault_unack: bool,
}

/// What the scripted run produced: the per-scan record, the durable
/// journal, the command receipts, and the final serialized snapshot.
#[derive(Debug, PartialEq)]
struct Run {
    scans: Vec<Scan>,
    journal: Vec<JournalEntry>,
    receipts: Vec<dcs_core::CommandReceipt>,
    snapshot: String,
}

fn float(sample: Sample) -> f64 {
    match sample.value {
        Value::Float(value) => value,
        other => panic!("expected a Float sample, got {other:?}"),
    }
}

fn int(sample: Sample) -> i64 {
    match sample.value {
        Value::Int(value) => value,
        other => panic!("expected an Int sample, got {other:?}"),
    }
}

fn bool_(sample: Sample) -> bool {
    match sample.value {
        Value::Bool(value) => value,
        other => panic!("expected a Bool sample, got {other:?}"),
    }
}

fn telemetry(snapshot: &TelemetrySnapshot, point: PointId) -> &PointTelemetry {
    snapshot
        .points
        .iter()
        .find(|telemetry| telemetry.point == point)
        .unwrap_or_else(|| panic!("no telemetry for {point:?}"))
}

fn sample(snapshot: &TelemetrySnapshot, point: PointId) -> Sample {
    telemetry(snapshot, point)
        .sample
        .unwrap_or_else(|| panic!("no sample for {point:?}"))
}

/// Writes `value` to `point` through the attributed, receipted
/// operator command path.
fn write(client: &MonitorClient, point: PointId, kind: ValueKind, value: Value) {
    let receipt = client
        .command_as(&Command::WriteValue { point, kind, value }, Some(OPERATOR))
        .unwrap();
    assert!(
        matches!(receipt.outcome, CommandOutcome::Accepted { .. }),
        "write to {point:?} rejected: {receipt:?}"
    );
}

/// Pulses a managed alarm's writable ack point: `true`, released next
/// scan.
fn ack(client: &MonitorClient, alarm: &ManagedAlarmLayout) {
    write(client, alarm.ack, ValueKind::Bool, Value::Bool(true));
}

/// Releases a managed alarm's writable ack point.
fn release_ack(client: &MonitorClient, alarm: &ManagedAlarmLayout) {
    write(client, alarm.ack, ValueKind::Bool, Value::Bool(false));
}

/// Pulses an unmanaged alarm's writable ack point.
fn ack_unmanaged(client: &MonitorClient, alarm: &AlarmLayout) {
    write(client, alarm.ack, ValueKind::Bool, Value::Bool(true));
}

/// Releases an unmanaged alarm's writable ack point.
fn release_unmanaged(client: &MonitorClient, alarm: &AlarmLayout) {
    write(client, alarm.ack, ValueKind::Bool, Value::Bool(false));
}

/// Runs the documented scenario against the checked-in document and
/// dynamics over the monitor's paced-scan surface — the same command
/// and journal paths the pane uses.
fn run() -> Run {
    let model = fixture_model();
    let layout = ids();
    let driver = build_driver(&model);
    let sim = driver
        .sim()
        .expect("the scenario's analog and digital devices all serve the local sim");
    let executor = assemble(&model, &dcs_controller::registry(), &driver).unwrap();
    let monitor = Monitor::bind("127.0.0.1:0", executor, model.signal_index()).unwrap();
    let client = MonitorClient::new(monitor.local_addr());

    // The declared tide forcing, the gate confirmed open, and one step
    // so scan 1 sees the dynamics' declared level rather than the
    // binding's neutral seed.
    sim.write(points::INFLOW, Value::Float(0.08)).unwrap();
    sim.write(points::GATE_FB, Value::Float(1.0)).unwrap();
    driver.step(DT).unwrap();

    let result = thread::scope(|scope| {
        scope.spawn(|| monitor.serve());
        // A failing assertion must not deadlock the scope join: catch
        // the panic so the server is always shut down first.
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let mut scans = Vec::with_capacity(SCANS as usize);
            for scan in 1..=SCANS {
                let snapshot = client.advance(1).unwrap();
                scans.push(Scan {
                    level: float(sample(&snapshot, points::LEVEL)),
                    remote: float(sample(&snapshot, points::LEVEL_REMOTE)),
                    remote_quality: sample(&snapshot, points::LEVEL_REMOTE).quality,
                    selected: float(sample(&snapshot, layout.level_selected)),
                    filtered: float(sample(&snapshot, layout.level_filtered)),
                    gate_demand: float(sample(&snapshot, layout.gate_demand)),
                    gate_fb: float(sample(&snapshot, points::GATE_FB)),
                    gate_cmd: float(sample(&snapshot, points::GATE_CMD)),
                    sis_draw: float(sample(&snapshot, points::SIS_DRAW)),
                    gate_mode: bool_(sample(&snapshot, points::GATE_MODE)),
                    manual_active: bool_(sample(&snapshot, layout.manual_active)),
                    discrepancy: bool_(sample(&snapshot, layout.discrepancy)),
                    backup_active: bool_(sample(&snapshot, layout.backup_active)),
                    backup_unhealthy: bool_(sample(&snapshot, layout.backup_unhealthy)),
                    deviating: bool_(sample(&snapshot, layout.deviating)),
                    duty_call: bool_(sample(&snapshot, layout.duty_call)),
                    lag_call: bool_(sample(&snapshot, layout.lag_call)),
                    below_cutoff: bool_(sample(&snapshot, layout.below_cutoff)),
                    high_level: bool_(sample(&snapshot, layout.high_level)),
                    chain_demand: int(sample(&snapshot, layout.chain_demand)),
                    sis_active: bool_(sample(&snapshot, points::SIS_ACTIVE)),
                    sis_available: bool_(sample(&snapshot, points::SIS_AVAILABLE)),
                    sis_fault: bool_(sample(&snapshot, points::SIS_FAULT)),
                    sis_trip: bool_(sample(&snapshot, points::SIS_TRIP)),
                    sis_proof_test: bool_(sample(&snapshot, points::SIS_PROOF_TEST)),
                    sis_bypass: bool_(sample(&snapshot, points::SIS_BYPASS)),
                    lah_alarm: bool_(sample(&snapshot, layout.high_level_alarm.alarm)),
                    lah_unack: bool_(sample(&snapshot, layout.high_level_alarm.unacknowledged)),
                    lah_shelved: bool_(sample(&snapshot, layout.high_level_alarm.shelved)),
                    lah_suppressed: bool_(sample(&snapshot, layout.high_level_alarm.suppressed)),
                    lah_oos: bool_(sample(&snapshot, layout.high_level_alarm.out_of_service)),
                    mode_alarm: bool_(sample(&snapshot, layout.mode_alarm.alarm)),
                    mode_unack: bool_(sample(&snapshot, layout.mode_alarm.unacknowledged)),
                    mode_shelved: bool_(sample(&snapshot, layout.mode_alarm.shelved)),
                    disc_alarm: bool_(sample(&snapshot, layout.discrepancy_alarm.alarm)),
                    disc_unack: bool_(sample(&snapshot, layout.discrepancy_alarm.unacknowledged)),
                    disc_suppressed: bool_(sample(&snapshot, layout.discrepancy_alarm.suppressed)),
                    ror_alarm: bool_(sample(&snapshot, layout.rate_of_rise_alarm.alarm)),
                    ror_unack: bool_(sample(&snapshot, layout.rate_of_rise_alarm.unacknowledged)),
                    backup_alarm: bool_(sample(&snapshot, layout.backup_active_alarm.alarm)),
                    backup_unack: bool_(sample(
                        &snapshot,
                        layout.backup_active_alarm.unacknowledged,
                    )),
                    buh_alarm: bool_(sample(&snapshot, layout.backup_unhealthy_alarm.alarm)),
                    buh_unack: bool_(sample(
                        &snapshot,
                        layout.backup_unhealthy_alarm.unacknowledged,
                    )),
                    trip_alarm: bool_(sample(&snapshot, layout.sis_trip_alarm.alarm)),
                    trip_unack: bool_(sample(&snapshot, layout.sis_trip_alarm.unacknowledged)),
                    bypass_alarm: bool_(sample(&snapshot, layout.sis_bypass_alarm.alarm)),
                    bypass_unack: bool_(sample(&snapshot, layout.sis_bypass_alarm.unacknowledged)),
                    fault_alarm: bool_(sample(&snapshot, layout.sis_fault_alarm.alarm)),
                    fault_unack: bool_(sample(&snapshot, layout.sis_fault_alarm.unacknowledged)),
                });
                let lah = &layout.high_level_alarm;
                match scan {
                    // Acknowledge the mode annunciation — the incident's
                    // alarmed transition.
                    8 => ack(&client, &layout.mode_alarm),
                    9 => release_ack(&client, &layout.mode_alarm),
                    // The operator demands the gate safe — the field
                    // fault holds it confirmed open.
                    13 => write(
                        &client,
                        layout.gate_manual_demand,
                        ValueKind::Float,
                        Value::Float(0.0),
                    ),
                    // The primary level transmitter drops off the DCS's
                    // I/O — the failover switches to the frozen remote
                    // repeater — and the high-level trip is
                    // acknowledged. The fault is the channel's
                    // disconnect: the controller's read lands
                    // `Bad(CommunicationFault)` while the physical
                    // level — and the independent layer's own view of
                    // it — keeps moving.
                    21 => {
                        sim.inject_fault(points::LEVEL, Fault::Disconnected)
                            .unwrap();
                        ack(&client, lah);
                    }
                    22 => release_ack(&client, lah),
                    // The independent high-high layer trips: the
                    // scripted inflow carried the level across the
                    // declared bound, the dynamics' `threshold` asserted
                    // the actuation contact on its own, and the
                    // reported `sis-trip` plays back this scan — the
                    // layer reports what it did, decision 77's boundary.
                    schedule::SIS_TRIP => {
                        ack_unmanaged(&client, &layout.sis_trip_alarm);
                        ack_unmanaged(&client, &layout.rate_of_rise_alarm);
                    }
                    25 => {
                        release_unmanaged(&client, &layout.sis_trip_alarm);
                        release_unmanaged(&client, &layout.rate_of_rise_alarm);
                    }
                    // The primary recovers.
                    26 => sim.clear_fault(points::LEVEL).unwrap(),
                    // The backup-serving annunciation is acknowledged —
                    // and the standby-health annunciation the frozen
                    // repeater already raised.
                    28 => {
                        ack_unmanaged(&client, &layout.backup_active_alarm);
                        ack_unmanaged(&client, &layout.backup_unhealthy_alarm);
                    }
                    29 => {
                        release_unmanaged(&client, &layout.backup_active_alarm);
                        release_unmanaged(&client, &layout.backup_unhealthy_alarm);
                    }
                    // Shelving: the request stands past the declared
                    // bound — `shelved` asserts inside it and expires
                    // while the request still stands.
                    31 => write(
                        &client,
                        lah.shelve.unwrap(),
                        ValueKind::Bool,
                        Value::Bool(true),
                    ),
                    40 => write(
                        &client,
                        lah.shelve.unwrap(),
                        ValueKind::Bool,
                        Value::Bool(false),
                    ),
                    // The trip's consequence has passed: the draw has
                    // pulled the level back through the hysteresis
                    // release, and the operator bypasses the layer for
                    // its proof-test window — the receipted
                    // writable-point path.
                    41 => {
                        write(
                            &client,
                            points::SIS_BYPASS,
                            ValueKind::Bool,
                            Value::Bool(true),
                        );
                        write(
                            &client,
                            lah.oos.unwrap(),
                            ValueKind::Bool,
                            Value::Bool(true),
                        );
                    }
                    // Acknowledge the re-annunciated discrepancy and the
                    // bypass.
                    43 => {
                        ack(&client, &layout.discrepancy_alarm);
                        ack_unmanaged(&client, &layout.sis_bypass_alarm);
                    }
                    44 => {
                        release_ack(&client, &layout.discrepancy_alarm);
                        release_unmanaged(&client, &layout.sis_bypass_alarm);
                        write(
                            &client,
                            lah.oos.unwrap(),
                            ValueKind::Bool,
                            Value::Bool(false),
                        );
                    }
                    // The proof test complete, the bypass returns.
                    47 => write(
                        &client,
                        points::SIS_BYPASS,
                        ValueKind::Bool,
                        Value::Bool(false),
                    ),
                    // Shelving again — a manual release inside the bound
                    // this time.
                    50 => write(
                        &client,
                        lah.shelve.unwrap(),
                        ValueKind::Bool,
                        Value::Bool(true),
                    ),
                    54 => write(
                        &client,
                        lah.shelve.unwrap(),
                        ValueKind::Bool,
                        Value::Bool(false),
                    ),
                    // The fault annunciation clears by its schedule; ack
                    // its latch.
                    39 => ack_unmanaged(&client, &layout.sis_fault_alarm),
                    _ => {}
                }
                driver.step(DT).unwrap();
            }
            Run {
                scans,
                journal: client.journal(0).unwrap(),
                receipts: client.receipts().unwrap(),
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

/// Regenerates the checked-in document — run with
/// `DCS_EMIT_FIXTURES=1` when the composition intentionally changes.
#[test]
fn emit_fixture() {
    if std::env::var("DCS_EMIT_FIXTURES").is_err() {
        return;
    }
    let mut document = serde_json::to_string_pretty(&emit().model).unwrap();
    document.push('\n');
    std::fs::write(MODEL_JSON, document).unwrap();
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
fn checked_in_document_validates_and_documents_its_lint() {
    let model = fixture_model();
    assert_eq!(model.version, dcs_model::MODEL_VERSION);
    assert!(model.validate().is_empty(), "{:?}", model.validate());
    // The document's one finding: the protection layer's bypass is a
    // writable field point — decision 77's declared operator path,
    // deliberately a finding rather than an internal command point.
    let lint = model.lint();
    assert_eq!(lint.len(), 1, "{lint:?}");
    let finding = &lint[0];
    assert_eq!(finding.rule, dcs_model::LintRule::WritableFieldPoint);
    assert!(
        finding
            .element
            .contains(&format!("{}", points::SIS_BYPASS.0)),
        "the one documented finding is the bypass point's writability: {finding:?}"
    );
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
fn every_alarm_carries_the_rationalization_record() {
    // Decision 70's sweep: every alarm instance carries its full
    // rationalization block beside the `priority`/`class`/
    // `response_ticks` parameters.
    let emitted = emit();
    let alarm_components = [
        emitted.layout.high_level_alarm.component,
        emitted.layout.mode_alarm.component,
        emitted.layout.discrepancy_alarm.component,
        emitted.layout.rate_of_rise_alarm.component,
        emitted.layout.backup_active_alarm.component,
        emitted.layout.backup_unhealthy_alarm.component,
        emitted.layout.sis_trip_alarm.component,
        emitted.layout.sis_bypass_alarm.component,
        emitted.layout.sis_fault_alarm.component,
    ];
    for component in alarm_components {
        let instance = emitted
            .model
            .components
            .iter()
            .find(|instance| instance.id == component)
            .unwrap();
        let record = instance
            .rationalization
            .as_ref()
            .unwrap_or_else(|| panic!("{component:?} carries no rationalization record"));
        for field in [
            &record.consequence,
            &record.required_action,
            &record.reference,
        ] {
            assert!(
                !field.is_empty(),
                "{component:?} rationalization is incomplete"
            );
        }
        for parameter in ["priority", "class", "response_ticks"] {
            assert!(
                instance.parameters.contains_key(parameter),
                "{component:?} lacks parameter {parameter}"
            );
        }
    }
}

#[test]
fn journaled_marks_the_durable_record_points() {
    // Decision 74's sweep: the mode/status transitions the
    // consequential-annunciation record names — the field mode point,
    // the manual-active and discrepancy flags, the chain's ladder, the
    // divergence and backup-serving flags, every managed status point,
    // and every protection-layer state — are declared `journaled`. The
    // flag stays opt-in: float measurements, demands, and the
    // receipted `ack` points keep their own paths.
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
        layout.sis_active,
        layout.gate_mode,
        layout.sis_available,
        layout.sis_fault,
        layout.sis_trip,
        layout.sis_proof_test,
        layout.sis_bypass,
        layout.manual_active,
        layout.discrepancy,
        layout.deviating,
        layout.backup_active,
        layout.backup_unhealthy,
        layout.duty_call,
        layout.lag_call,
        layout.below_cutoff,
        layout.high_level,
    ];
    let mut off_record = vec![
        layout.level,
        layout.level_remote,
        layout.inflow,
        layout.net_flow,
        layout.gate_flow,
        layout.sis_draw,
        layout.gate_fb,
        layout.gate_cmd,
        layout.level_selected,
        layout.level_filtered,
        layout.level_trend,
        layout.deviation,
        layout.gate_demand,
        layout.chain_demand,
        layout.gate_auto_demand,
        layout.gate_manual_demand,
    ];
    for alarm in [
        &layout.high_level_alarm,
        &layout.mode_alarm,
        &layout.discrepancy_alarm,
    ] {
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
    for alarm in [
        &layout.rate_of_rise_alarm,
        &layout.backup_active_alarm,
        &layout.backup_unhealthy_alarm,
        &layout.sis_trip_alarm,
        &layout.sis_bypass_alarm,
        &layout.sis_fault_alarm,
    ] {
        record.extend([alarm.alarm, alarm.unacknowledged]);
        off_record.push(alarm.ack);
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
fn dynamics_document_declares_the_level_threshold() {
    // The issue-#320 wiring: the checked-in dynamics declaration's
    // `threshold` element reads the canal `level` and drives
    // `sis-active` — `on` at the declared high-high bound, `off` at
    // the declared hysteresis release, `initial` deasserted — beside
    // the unchanged four flow elements the document already carried.
    let elements = dynamics();
    let threshold = elements
        .iter()
        .find_map(|element| match element {
            ProcessElement::Threshold(threshold) => Some(threshold),
            _ => None,
        })
        .expect("the dynamics document declares no threshold element");
    assert_eq!(threshold.input, points::LEVEL);
    assert_eq!(threshold.output, points::SIS_ACTIVE);
    assert_eq!(threshold.on, SIS_HIGH_HIGH);
    assert_eq!(threshold.off, SIS_RELEASE);
    assert!(!threshold.initial);
    let others: Vec<&ProcessElement> = elements
        .iter()
        .filter(|element| !matches!(element, ProcessElement::Threshold(_)))
        .collect();
    let [
        ProcessElement::ScaledFlow(scaled),
        ProcessElement::BoolFlow(flow),
        ProcessElement::FlowSum(sum),
        ProcessElement::Integrator(integrator),
    ] = others.as_slice()
    else {
        panic!("the dynamics document's other content changed: {others:?}")
    };
    assert_eq!(scaled.input, points::GATE_FB);
    assert_eq!(scaled.output, points::GATE_FLOW);
    // The emergency draw still gates on `sis-active` unchanged — the
    // element replaces only the decision.
    assert_eq!(flow.input, points::SIS_ACTIVE);
    assert_eq!(flow.output, points::SIS_DRAW);
    assert_eq!(sum.output, points::NET_FLOW);
    assert_eq!(integrator.input, points::NET_FLOW);
    assert_eq!(integrator.output, points::LEVEL);
}

#[test]
fn the_protection_layer_acts_without_a_scan() {
    // Decision 77's boundary made observable: with no executor at all,
    // the scripted inflow carrying the level across the declared bound
    // asserts the actuation contact — the `threshold` element's own
    // decision, no field write to the contact — the relief draw gates
    // on, and the level integrator falls. The protective function is
    // the dynamics', never the controller's.
    let model = fixture_model();
    let driver = build_driver(&model);
    let sim = driver.sim().unwrap();
    sim.write(points::INFLOW, Value::Float(0.08)).unwrap();
    sim.write(points::GATE_FB, Value::Float(1.0)).unwrap();
    driver.step(DT).unwrap();
    let before = sim.read(points::LEVEL).unwrap();
    let rising = float(before) - 3.0;
    assert!(rising > 0.0, "the tide forcing must push the level up");

    let mut tripped_at = None;
    for _ in 0..schedule::SIS_CLEAR {
        driver.step(DT).unwrap();
        if bool_(sim.read(points::SIS_ACTIVE).unwrap()) {
            tripped_at = Some(float(sim.read(points::LEVEL).unwrap()));
            break;
        }
    }
    let tripped_at = tripped_at.expect("the level crossing must assert the contact on its own");
    assert!(
        tripped_at >= SIS_HIGH_HIGH,
        "the contact asserted before the declared bound: {tripped_at}"
    );
    // The draw lags the contact by one step — `bool_flow` evaluates
    // ahead of `threshold` in the declaration order — so the next
    // plant step is the first the relief shows on.
    driver.step(DT).unwrap();
    let draw = float(sim.read(points::SIS_DRAW).unwrap());
    assert!(draw < 0.0, "the relief draw must gate on the contact");
    let after = float(sim.read(points::LEVEL).unwrap());
    assert!(
        after < tripped_at,
        "the layer's action must draw the level down with no scan run: {tripped_at} -> {after}"
    );

    // The hysteresis release is the element's own too: the draw pulls
    // the level back through the declared release bound and the
    // contact drops — no chatter, no field write.
    let mut released_at = None;
    for _ in 0..schedule::SIS_CLEAR {
        driver.step(DT).unwrap();
        if !bool_(sim.read(points::SIS_ACTIVE).unwrap()) {
            released_at = Some(float(sim.read(points::LEVEL).unwrap()));
            break;
        }
    }
    let released_at =
        released_at.expect("the draw's pull-back must release the contact on its own");
    assert!(
        released_at < SIS_RELEASE,
        "the contact released outside the hysteresis band: {released_at}"
    );
    driver.step(DT).unwrap();
    assert_eq!(
        float(sim.read(points::SIS_DRAW).unwrap()),
        0.0,
        "the draw must drop with the contact"
    );
}

#[test]
fn scripted_run_shows_the_consequential_annunciation() {
    let run = run();
    let scans = &run.scans;
    let at = |scan: u64| &scans[scan as usize - 1];

    // The level climbs on the declared tide plus the confirmed-open
    // gate's admitted flow until the independent layer's draw wins.
    assert!(
        (at(1).level - 3.13).abs() < 1e-9,
        "scan 1: {:?}",
        at(1).level
    );
    for window in scans.windows(2).take(6) {
        assert!(
            window[1].level > window[0].level,
            "the level must climb on the tide: {:?} -> {:?}",
            window[0].level,
            window[1].level
        );
    }

    // The automatic-to-manual transition (decision 75's first
    // annunciation): the scripted mode point asserts at its tick, the
    // station reports manual, and the never-shelvable alarm latches
    // until the scan-8 ack.
    let manual_at = scans.iter().position(|scan| scan.manual_active).unwrap() as u64 + 1;
    assert_eq!(manual_at, schedule::MODE_TO_MANUAL);
    assert!(at(schedule::MODE_TO_MANUAL).gate_mode);
    assert!(scans.iter().any(|scan| scan.mode_alarm && scan.mode_unack));
    assert!(scans[9..].iter().all(|scan| !scan.mode_unack));
    // Never shelvable: the request surface was never declared and the
    // status never asserts.
    assert!(scans.iter().all(|scan| !scan.mode_shelved));
    // The return to automatic annunciates as the transition's other
    // edge.
    assert!(!at(schedule::MODE_TO_AUTO).gate_mode);
    assert!(!at(schedule::MODE_TO_AUTO).manual_active);
    assert!(
        scans[schedule::MODE_TO_AUTO as usize..]
            .iter()
            .all(|scan| !scan.mode_alarm)
    );

    // The confirmed-state/demand discrepancy: the operator's close
    // demand slews the station's output to safe while `gate-fb` holds
    // confirmed open — the valve's `discrepancy` asserts after its
    // declared consecutive-deviating-scans budget and its alarm
    // latches.
    assert!(scans[14..].iter().any(|scan| scan.gate_demand < 0.05));
    let discrepancy_at = scans.iter().position(|scan| scan.discrepancy).unwrap() as u64 + 1;
    assert!(
        (at(discrepancy_at).gate_cmd - at(discrepancy_at).gate_fb).abs() > 0.05,
        "discrepancy asserted without a mismatch: {discrepancy_at}"
    );
    assert!(scans.iter().any(|scan| scan.disc_alarm && scan.disc_unack));

    // The worsening process condition: the filtered level walks the
    // chain's ladder and the managed high-level alarm trips at the
    // declared `high`, latching until the scan-21 ack.
    assert!(scans.iter().any(|scan| scan.duty_call));
    assert!(scans.iter().any(|scan| scan.lag_call));
    assert!(scans.iter().any(|scan| scan.high_level));
    assert!(scans.iter().any(|scan| scan.chain_demand >= 1));
    let lah_at = scans.iter().position(|scan| scan.lah_alarm).unwrap() as u64 + 1;
    assert!(
        at(lah_at).filtered >= 5.0 - 1e-9,
        "LAH tripped below the declared high: {lah_at} -> {:?}",
        at(lah_at).filtered
    );
    assert!(scans[..22].iter().any(|scan| scan.lah_unack));
    // The composed rate-of-rise annunciation: the divergence detector
    // flags the sustained rise and its alarm latches until the
    // scan-24 ack.
    assert!(scans[..24].iter().any(|scan| scan.deviating));
    assert!(scans[..25].iter().any(|scan| scan.ror_unack));

    // Bad and stale data: the repeater's last scripted update is the
    // schedule's — past the `stale_after_ticks` budget the point
    // presents `Uncertain(Stale)`, never a healthy last-known value.
    assert_eq!(
        at(schedule::REMOTE_LAST_UPDATE).remote_quality,
        Quality::Good
    );
    assert!(
        scans[schedule::REMOTE_LAST_UPDATE as usize + 3..schedule::REMOTE_RECOVERY as usize - 1]
            .iter()
            .all(|scan| scan.remote_quality == Quality::Uncertain(QualityReason::Stale)),
        "the frozen repeater must present stale, not a healthy last-known value"
    );
    assert_eq!(at(schedule::REMOTE_RECOVERY).remote_quality, Quality::Good);
    // The issue-#502 annunciation: the repeater's stale sample makes
    // the standby leg unhealthy from the first stale presentation —
    // before the primary ever fails — and its alarm latches
    // unacknowledged until the scan-28 ack, clearing on the repeater's
    // recovery.
    assert!(
        scans[..schedule::REMOTE_LAST_UPDATE as usize + 3]
            .iter()
            .all(|scan| !scan.backup_unhealthy),
        "a healthy standby must not annunciate"
    );
    assert!(
        scans[schedule::REMOTE_LAST_UPDATE as usize + 3..schedule::REMOTE_RECOVERY as usize - 1]
            .iter()
            .all(|scan| scan.backup_unhealthy),
        "the standby leg must annunciate while the repeater's own sample is untrusted"
    );
    assert!(
        scans[schedule::REMOTE_RECOVERY as usize - 1..SCANS as usize - 1]
            .iter()
            .all(|scan| !scan.backup_unhealthy),
        "the repeater's recovery must clear the indication"
    );
    // The playback's last remote update then ages past
    // `stale_after_ticks` once more — the run's final scan re-asserts
    // the carrier, tracking the standby leg's real freshness rather
    // than the incident timeline. The alarm's input is an internal
    // link, so its re-latch would land one scan past the run's end.
    assert!(scans.last().unwrap().backup_unhealthy);
    assert!(scans.iter().any(|scan| scan.buh_alarm && scan.buh_unack));
    assert!(scans[28..].iter().all(|scan| !scan.buh_unack));
    // The `Bad` primary flips the failover onto that stale repeater —
    // `backup_active` stands and its alarm latches until the scan-28
    // ack; the selected level carries the degraded quality through.
    // Both flags stand together while the untrusted backup serves.
    assert!(scans[22..26].iter().any(|scan| scan.backup_active));
    assert!(
        scans[22..26]
            .iter()
            .any(|scan| scan.backup_active && scan.backup_unhealthy)
    );
    assert!(
        scans
            .iter()
            .any(|scan| scan.backup_alarm && scan.backup_unack)
    );

    // The independent high-high layer (decision 77): the dynamics'
    // `threshold` asserts the actuation contact off the canal `level`
    // itself — the scripted inflow carries it across the declared
    // high-high bound at SIS_TRIP with no field write to the contact,
    // and the scripted `sis-trip` report plays back the same scan:
    // the layer reports what its own decision did, independently of
    // anything the controller computed.
    assert!(!at(schedule::SIS_TRIP - 1).sis_active);
    assert!(at(schedule::SIS_TRIP).sis_active);
    assert!(at(schedule::SIS_TRIP).sis_trip);
    assert!(scans.iter().any(|scan| scan.trip_alarm && scan.trip_unack));
    assert!(
        scans[schedule::SIS_TRIP as usize..]
            .iter()
            .all(|scan| scan.sis_trip)
    );
    // The draw gates on the computed contact — it stands while the
    // contact stands — and the hysteresis release drops it once the
    // draw has pulled the level back below the declared release bound.
    assert!(at(schedule::SIS_CLEAR - 1).sis_active);
    assert!(!at(schedule::SIS_CLEAR).sis_active);
    assert!(
        at(schedule::SIS_CLEAR).level < SIS_RELEASE,
        "the contact released before the level left the hysteresis band: {:?}",
        at(schedule::SIS_CLEAR)
    );
    assert!(
        scans[schedule::SIS_TRIP as usize..schedule::SIS_CLEAR as usize]
            .iter()
            .all(|scan| scan.sis_draw < 0.0),
        "the draw must follow the computed contact"
    );
    // The layer keeps its own hold on the hazard: every later bound
    // crossing re-asserts the contact and every release lands below
    // the declared release — the element's own decision each time.
    // (The first assert's image reads are legitimately frozen by the
    // transmitter fault window, so its bound check is the reconciled
    // `SIS_TRIP` tick itself.)
    let mut stood = false;
    for (index, scan) in scans.iter().enumerate() {
        let tick = index as u64 + 1;
        match (stood, scan.sis_active) {
            (false, true) if tick != schedule::SIS_TRIP => assert!(
                scan.level >= SIS_HIGH_HIGH - 1e-9,
                "the contact asserted off the declared bound at {tick}: {scan:?}"
            ),
            (true, false) => assert!(
                scan.level < SIS_RELEASE,
                "the contact released inside the hysteresis band at {tick}: {scan:?}"
            ),
            _ => {}
        }
        stood = scan.sis_active;
    }
    assert!(
        scans[schedule::SIS_CLEAR as usize..]
            .iter()
            .any(|scan| scan.sis_active),
        "the next bound crossing must re-assert the contact"
    );
    // Its reported fault and proof-test windows and the operator's
    // bypass all land on their alarmed and journaled surfaces.
    assert!(
        scans[schedule::SIS_FAULT_ON as usize - 1..schedule::SIS_FAULT_OFF as usize - 1]
            .iter()
            .all(|scan| scan.sis_fault)
    );
    assert!(
        scans
            .iter()
            .any(|scan| scan.fault_alarm && scan.fault_unack)
    );
    assert!(
        scans[schedule::PROOF_TEST_ON as usize - 1..schedule::PROOF_TEST_OFF as usize - 1]
            .iter()
            .all(|scan| scan.sis_proof_test)
    );
    assert!(scans[42..].iter().any(|scan| scan.sis_bypass));
    assert!(
        scans[42..]
            .iter()
            .any(|scan| scan.bypass_alarm && scan.bypass_unack)
    );

    // The designed suppression (decisions 73/76) rides the computed
    // contact now: while the layer's actuation stands, the standing
    // discrepancy is the trip's consequence — `suppressed` asserts
    // with the contact and `unacknowledged` holds clear — while
    // `alarm` keeps reporting the mismatch's truth. Each hysteresis
    // release re-annunciates the standing condition as a fresh latch;
    // the later crossings suppress it again until the scan-43 ack.
    assert!(
        scans[23..27].iter().all(|scan| scan.disc_suppressed),
        "the suppression must stand while the layer acts"
    );
    assert!(!at(28).disc_suppressed);
    assert!(scans[36..40].iter().all(|scan| scan.disc_suppressed));
    assert!(scans[23..27].iter().all(|scan| !scan.disc_unack));
    assert!(scans[25..40].iter().any(|scan| scan.disc_alarm));
    assert!(scans[27..36].iter().all(|scan| scan.disc_unack));
    assert!(scans[40..43].iter().all(|scan| scan.disc_unack));
    assert!(scans[44..].iter().all(|scan| !scan.disc_unack));

    // The shelve bound and its expiry: the request standing past
    // `max_shelve_ticks` asserts `shelved` inside the bound and the
    // kind expires it while the request still stands; the later
    // request released by hand clears it without the bound.
    let shelve_from = scans.iter().position(|scan| scan.lah_shelved).unwrap() as u64 + 1;
    assert_eq!(shelve_from, 32, "the request applies at the next scan");
    assert!(scans[31..37].iter().all(|scan| scan.lah_shelved));
    assert!(
        scans[37..50].iter().all(|scan| !scan.lah_shelved),
        "the bound must expire the shelve while the request still stands"
    );
    assert!(scans[50..54].iter().all(|scan| scan.lah_shelved));
    assert!(
        scans[54..].iter().all(|scan| !scan.lah_shelved),
        "the manual release must clear the shelve"
    );

    // The out-of-service path: the command asserts the status while it
    // stands and clears on its release.
    assert!(scans[41..44].iter().any(|scan| scan.lah_oos));
    assert!(scans[45..].iter().all(|scan| !scan.lah_oos));

    // The recovery and the second excursion: mode returns to
    // automatic, the demand slews back to open, the confirmed state
    // agrees again, and the discrepancy clears — while the recovering
    // level re-trips the high-level alarm, re-latching `unacknowledged`
    // — the durable record's full arc.
    assert!(scans[45..].iter().any(|scan| !scan.discrepancy));
    let second_lah = scans[50..]
        .iter()
        .position(|scan| scan.lah_alarm)
        .map(|index| index as u64 + 51);
    assert!(
        second_lah.is_some(),
        "the recovering level must re-trip the high-level alarm"
    );
    assert!(
        scans[second_lah.unwrap() as usize..]
            .iter()
            .any(|scan| scan.lah_unack),
        "a fresh violation must re-latch the alarm"
    );

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
fn every_lifecycle_transition_lands_in_the_durable_record_in_seq_order() {
    let run = run();
    let journal = &run.journal;

    // The durable stream is ordered: `seq` counts from 1, one per
    // entry, never reused.
    for (index, entry) in journal.iter().enumerate() {
        assert_eq!(entry.seq, index as u64 + 1, "journal seq must be ordered");
    }

    let layout = ids();
    // The `point_changed` entries naming `point`'s transition `to`
    // `value`, in journal order.
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
    // Real changes — `from` present — excluding the `from: None`
    // seed entry every journaled point's first observation records.
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
    // The tick a transition is attributed to — the causal-order key;
    // `seq` orders same-tick entries only by the recorder's own
    // iteration, so pairwise causality compares ticks.
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
    let release_tick = |point: PointId| -> u64 {
        journal
            .iter()
            .find_map(|entry| match &entry.event {
                JournalEvent::PointChanged {
                    point: p,
                    from: Some(_),
                    to,
                } if *p == point && *to == Value::Bool(false) => Some(entry.tick.0),
                _ => None,
            })
            .unwrap_or_else(|| panic!("{point:?} never journaled its release"))
    };
    let yes = Value::Bool(true);
    let no = Value::Bool(false);

    // The automatic-to-manual transition: the field mode point, the
    // station's status, and the alarm's two flags all land — the
    // field's report no later than the annunciation it drove, the
    // alarm before its latch, and the acknowledged release strictly
    // after.
    assert!(tick_of(points::GATE_MODE, yes) <= tick_of(layout.manual_active, yes));
    assert!(tick_of(layout.manual_active, yes) <= tick_of(layout.mode_alarm.alarm, yes));
    assert!(
        tick_of(layout.mode_alarm.alarm, yes) <= tick_of(layout.mode_alarm.unacknowledged, yes)
    );
    assert!(
        first(layout.mode_alarm.unacknowledged, yes) < released(layout.mode_alarm.unacknowledged)
    );
    // The return to automatic journals too.
    assert!(first(points::GATE_MODE, yes) < released(points::GATE_MODE));

    // The discrepancy: the flag's assertion and its alarm's latch land
    // before the suppression's assert and release.
    assert!(
        tick_of(layout.discrepancy, yes) <= tick_of(layout.discrepancy_alarm.unacknowledged, yes)
    );
    assert!(
        first(layout.discrepancy_alarm.unacknowledged, yes)
            < first(layout.discrepancy_alarm.suppressed, yes)
    );
    assert!(
        first(layout.discrepancy_alarm.suppressed, yes)
            < released(layout.discrepancy_alarm.suppressed)
    );
    // The released standing condition re-annunciates: the latch
    // re-asserts when the suppression clears.
    let disc_unack_asserts: Vec<u64> = journal
        .iter()
        .filter_map(|entry| match &entry.event {
            JournalEvent::PointChanged { point: p, to, .. }
                if *p == layout.discrepancy_alarm.unacknowledged && *to == yes =>
            {
                Some(entry.tick.0)
            }
            _ => None,
        })
        .collect();
    assert!(
        disc_unack_asserts.len() >= 2
            && *disc_unack_asserts.last().unwrap()
                >= release_tick(layout.discrepancy_alarm.suppressed),
        "the standing discrepancy must re-latch when the suppression releases"
    );

    // The managed lifecycle: shelve assert, expiry, the second
    // request's manual release, out-of-service entry and return —
    // every transition on record.
    assert!(
        tick_of(layout.high_level_alarm.shelve.unwrap(), yes)
            <= tick_of(layout.high_level_alarm.shelved, yes)
    );
    assert!(changes_to(layout.high_level_alarm.shelved, no).len() >= 2);
    assert!(
        tick_of(layout.high_level_alarm.oos.unwrap(), yes)
            <= tick_of(layout.high_level_alarm.out_of_service, yes)
    );
    assert!(
        first(layout.high_level_alarm.out_of_service, yes)
            < released(layout.high_level_alarm.out_of_service)
    );

    // The protection layer's states all land — availability was
    // declared standing, the rest transition on schedule, and the
    // operator's bypass is on record beside its attributed receipt.
    for point in [
        layout.sis_active,
        layout.sis_trip,
        layout.sis_fault,
        layout.sis_proof_test,
        layout.sis_bypass,
    ] {
        assert!(
            !transitions_to(point, yes).is_empty(),
            "{point:?} never journaled its assertion"
        );
    }
    assert!(first(layout.sis_bypass, yes) < released(layout.sis_bypass));

    // The stale transition lands as the quality record — the frozen
    // repeater's degradation is durable too — and the standby-health
    // carrier and its alarm latch journal their own arc.
    assert!(journal.iter().any(|entry| matches!(
        &entry.event,
        JournalEvent::QualityChanged { point, to, .. }
            if *point == layout.level_remote && *to == Quality::Uncertain(QualityReason::Stale)
    )));
    assert!(
        tick_of(layout.backup_unhealthy, yes)
            <= tick_of(layout.backup_unhealthy_alarm.unacknowledged, yes)
    );
    assert!(first(layout.backup_unhealthy, yes) < released(layout.backup_unhealthy));

    // Every submitted command settled on record — attributed to the
    // operator — and none were refused.
    let settled: Vec<&dcs_core::CommandReceipt> = journal
        .iter()
        .filter_map(|entry| match &entry.event {
            JournalEvent::CommandSettled { receipt } => Some(receipt),
            _ => None,
        })
        .collect();
    assert!(!settled.is_empty());
    assert!(settled.iter().all(|receipt| matches!(
        receipt.outcome,
        CommandOutcome::Applied { .. } | CommandOutcome::Accepted { .. }
    )));
    assert!(
        settled
            .iter()
            .any(|receipt| receipt.actor.as_deref() == Some(OPERATOR)),
        "the operator's commands must carry their attribution"
    );
}

#[test]
fn repeated_builds_and_runs_are_deterministic() {
    assert_eq!(run(), run());
}
