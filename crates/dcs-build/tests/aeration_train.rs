//! The reference diffused-air aeration train, composed through
//! [`dcs_build::aeration::aeration_train`] — issue #289's artifact.
//!
//! The checked-in documents live at `crates/dcs-demo/fixtures/`:
//! `aeration_train.json` is the emitted PlantModel and
//! `aeration_train_dynamics.json` the decision-44 dynamics declaration —
//! per zone a `scaled_flow` turning the commanded valve opening into the
//! delivered airflow, a `scaled_flow` of that airflow into the oxygen
//! transfer and a second of the declared uptake forcing into the drain, a
//! `flow_sum` per probe, and a `first_order_lag` per probe with the three
//! declared time constants the voter's spread diagnostic reads; per
//! machine a `scaled_flow` of the commanded capacity onto the scaled
//! demand, a `first_order_lag` standing the machine's response, and a
//! `scaled_flow` of that airflow onto the motor current; and bank-wide a
//! `flow_sum` over the machines' airflow feeding both the header's
//! aggregate airflow and, through a `scaled_flow` and a biasing
//! `flow_sum`, the header pressure a `first_order_lag` carries.
//!
//! These tests assert the helper re-emits the checked-in document
//! exactly, that it validates and lints with only the recorded advisory
//! class, assembles through the standard registries, serde-roundtrips,
//! and that a deterministic scripted run over the merged dynamics shows
//! the closed train — and that every run is bit-for-bit deterministic.
//!
//! ## The scripted scenario
//!
//! Each iteration scans the executor, observes the image, applies the
//! script's commands and forcing, then steps the plant one second — so
//! scan `s` observes the world `step s` produced. The legs, in order:
//!
//! - **the DO loop closing** — the paced feed-forward demand carries both
//!   zones at their declared set-point, the loop's trim holds them there,
//!   and the air-valve commands follow the demand;
//! - **staging under the declared authority** — the group flags its stage
//!   change for approval, the operator releases it, the machines join
//!   through their vent dwell, and the capacity split lands inside the
//!   declared unit bounds;
//! - **the coordinator** — the most-open-valve reset walks the set-point
//!   on the declared cadence, `blower_demand` aggregates the zone demands,
//!   the mixing floor raises the at-bound flag, and the pulse cap admits
//!   one grid while refusing the other;
//! - **the surge guard** — a high influent drives the header pressure past
//!   the machines' declared surge bound and the guard engages inside its
//!   envelope;
//! - **the declared DO-loss response** — every probe of a zone reads
//!   `Bad`, the fallback engages with the declared safe airflow and
//!   alarms, and it recovers on the first `Good` scan;
//! - **the alarm lifecycle** — every declared alarm's latch clearing
//!   through its own writable ack point, with the settled receipts the
//!   journal records;
//! - **the declared stale path** — a held operator demand freezes the
//!   header pressure, the freshness budget expires, and the coordinator
//!   holds its last emitted set-point rather than walking an untrusted
//!   reading.

use dcs_assembly::{DriverRegistry, FanoutDriver, assemble, resolve_drivers};
use dcs_build::aeration::{
    AerationTrain, AerationTrainConfig, AerationTrainLayout, BANK_ALARMS, BLOWER_ALARMS,
    REFERENCE_BLOWERS, REFERENCE_PROBES, REFERENCE_ZONES, ZONE_ALARMS, aeration_train, points,
};
use dcs_build::station::AlarmLayout;
use dcs_build::{PointId, Value, unit};
use dcs_core::{
    Command, CommandOutcome, CommandReceipt, IoDriver, Quality, QualityReason, Value as CoreValue,
    ValueKind,
};
use dcs_model::{LintRule, PlantModel};
use dcs_sim::{Fault, ProcessElement};

/// The checked-in emitted document.
const MODEL_JSON: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/aeration_train.json"
);
/// The checked-in dynamics declaration merged over it.
const DYNAMICS_JSON: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/aeration_train_dynamics.json"
);

/// The plant step each scan period covers, in seconds.
const DT: f64 = 1.0;
/// The scripted run length — covers every leg below plus settling.
const SCANS: u64 = 900;
/// The influent flow the paced feed-forward term paces against.
const INFLUENT: f64 = 20_000.0;
/// The declared oxygen uptake the DO balance carries at the nominal load.
const UPTAKE: f64 = 1.0;
/// The declared process elements the checked-in dynamics document
/// carries.
const ELEMENTS: usize = 29;
/// The declared silence the stale leg holds the simulated plant for —
/// longer than the header pressure's freshness budget, so the budget
/// expires inside the leg.
const STALE_SPAN: u64 = 40;

/// Emits the train through the reference configuration.
fn emit() -> AerationTrain {
    aeration_train(&AerationTrainConfig::reference()).unwrap()
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
/// [`DriverRegistry`], merging the checked-in dynamics into the shared sim
/// map exactly as `dcs-plant-server --dynamics` does — each element lands
/// through `with_element` and revalidates the map.
fn build_driver(model: &PlantModel) -> FanoutDriver {
    let mut plan = resolve_drivers(model, &DriverRegistry::standard()).unwrap();
    for element in dynamics() {
        plan.sim_map = plan.sim_map.with_element(element);
        plan.sim_map.validate().unwrap();
    }
    plan.build().unwrap()
}

/// Writes `value` to `point` through the operator command path, returning
/// the settled receipt.
fn write(
    executor: &mut dcs_runtime::Executor<'_>,
    point: PointId,
    kind: ValueKind,
    value: Value,
) -> CommandReceipt {
    let receipt = executor.submit_command(Command::WriteValue { point, kind, value });
    assert!(
        matches!(receipt.outcome, CommandOutcome::Accepted { .. }),
        "write to {point:?} rejected: {receipt:?}"
    );
    receipt
}

/// The scripted run's operator writes: each one settles a receipt, which
/// the run collects so the acknowledged and held values are provable
/// through the receipted path.
#[allow(clippy::too_many_arguments)]
fn op(
    executor: &mut dcs_runtime::Executor<'_>,
    receipts: &mut Vec<CommandReceipt>,
    point: PointId,
    kind: ValueKind,
    value: Value,
) {
    receipts.push(write(executor, point, kind, value));
}

/// Every alarm the reference train declares, in layout order.
fn all_alarms(layout: &AerationTrainLayout) -> Vec<AlarmLayout> {
    let mut alarms: Vec<AlarmLayout> = vec![
        layout.pressure_alarm.clone(),
        layout.at_bound_alarm.clone(),
        layout.pulse_blocked_alarm.clone(),
        layout.staging_pending_alarm.clone(),
        layout.none_available_alarm.clone(),
        layout.all_faulted_alarm.clone(),
    ];
    for zone in &layout.zones {
        alarms.extend([
            zone.do_low_alarm.clone(),
            zone.do_high_alarm.clone(),
            zone.airflow_alarm.clone(),
            zone.do_discrepancy_alarm.clone(),
            zone.fallback_alarm.clone(),
            zone.manual_alarm.clone(),
        ]);
    }
    for blower in &layout.blowers {
        alarms.extend([blower.trip_alarm.clone(), blower.protective_alarm.clone()]);
    }
    assert_eq!(
        alarms.len(),
        (BANK_ALARMS
            + ZONE_ALARMS * REFERENCE_ZONES as u64
            + BLOWER_ALARMS * REFERENCE_BLOWERS as u64) as usize
    );
    alarms
}

fn float(sample: dcs_core::Sample) -> f64 {
    match sample.value {
        CoreValue::Float(value) => value,
        other => panic!("expected a Float sample, got {other:?}"),
    }
}

fn int(sample: dcs_core::Sample) -> i64 {
    match sample.value {
        CoreValue::Int(value) => value,
        other => panic!("expected an Int sample, got {other:?}"),
    }
}

fn bool_(sample: dcs_core::Sample) -> bool {
    match sample.value {
        CoreValue::Bool(value) => value,
        other => panic!("expected a Bool sample, got {other:?}"),
    }
}

/// One zone's observable record at a scan.
#[derive(Debug, PartialEq, Clone)]
struct ZoneScan {
    /// The voted DO measurement.
    do_selected: f64,
    /// The filtered DO measurement the loop and the fallback read.
    do_filtered: f64,
    /// The DO loop's additive trim.
    pid_trim: f64,
    /// The demand the declared DO-loss response serves.
    served: f64,
    /// The zone's commanded airflow after the operator takeover — the
    /// value the valve's engineering scaling carries and the coordinator
    /// reads.
    commanded: f64,
    /// The delivered airflow measurement.
    airflow: f64,
    /// The air-valve position command.
    valve_cmd: f64,
    /// The air-valve position feedback.
    valve_pos: f64,
    /// The totalized delivered airflow.
    zone_total: f64,
    /// The feed-forward layer's clamped flag.
    clamped: bool,
    /// The probe-spread diagnostic.
    do_discrepancy: bool,
    /// The declared DO-loss response's engagement.
    fallback_active: bool,
    /// The operator takeover's engagement.
    manual_active: bool,
    /// The air valve's proven feedback discrepancy.
    valve_discrepancy: bool,
    /// The per-grid mixing-pulse request.
    pulsing: bool,
    /// The coordinator's pulse admission.
    pulse_grant: bool,
    /// The filter quality of the zone's delivered airflow.
    airflow_quality: Quality,
}

/// One machine's observable record at a scan.
#[derive(Debug, PartialEq, Clone)]
struct BlowerScan {
    /// The measured discharge airflow.
    flow: f64,
    /// The motor current.
    current: f64,
    /// The machine's split capacity demand.
    capacity: f64,
    /// The surge-guarded demand.
    guarded: f64,
    /// The machine's capacity command.
    speed_cmd: f64,
    /// The group's run request.
    group_cmd: bool,
    /// The run feedback the simulated plant reports.
    run_fb: bool,
    /// The unloading/vent valve command.
    vent_cmd: bool,
    /// The surge region's engagement flag.
    guarding: bool,
    /// The guard's proven-trip flag.
    tripped: bool,
    /// The motor's proven fault.
    motor_fault: bool,
    /// The hardwired protective-status family's aggregate.
    protective: bool,
    /// The machine's availability as the group reads it.
    group_avail: bool,
    /// The machine's fault as the group reads it.
    group_fault: bool,
}

/// One scan's observable record.
#[derive(Debug, PartialEq, Clone)]
struct Scan {
    /// The header pressure measurement.
    pressure: f64,
    /// The header pressure's served quality — the declared stale path's
    /// observable surface.
    pressure_quality: Quality,
    /// The coordinator's emitted set-point.
    pressure_sp: f64,
    /// The slew-bounded set-point.
    pressure_sp_slew: f64,
    /// The coordinator's aggregate capacity demand.
    blower_demand: f64,
    /// The coordinator's most-open zone identity.
    most_open: i64,
    /// The coordinator's at-bound flag.
    at_bound: bool,
    /// The coordinator's pulse-cap refusal flag.
    pulse_blocked: bool,
    /// How many machines the group commands.
    staged: i64,
    /// A group transition in progress.
    transition: bool,
    /// A stage change awaiting the operator's release.
    staging_pending: bool,
    /// No machine available.
    none_available: bool,
    /// Every machine faulted.
    all_faulted: bool,
    /// The header's totalized aggregate airflow.
    header_total: f64,
    /// The per-zone records.
    zones: [ZoneScan; REFERENCE_ZONES],
    /// The per-machine records.
    blowers: [BlowerScan; REFERENCE_BLOWERS],
    /// Every declared alarm's standing state and latch.
    alarms: Vec<(bool, bool)>,
}

/// What the scripted run produced: the per-scan record and the command
/// receipts it settled.
#[derive(Debug, PartialEq)]
struct Run {
    /// Every scan's observable record.
    scans: Vec<Scan>,
    /// Every command receipt the script's writes settled.
    receipts: Vec<CommandReceipt>,
}

/// The ids the run addresses — the emitted train's layout.
fn ids() -> AerationTrainLayout {
    emit().layout
}

/// The most recent observed scan — the scripted legs read the world the
/// scan just produced rather than the executor's live image.
fn run_snapshot(scans: &[Scan]) -> &Scan {
    scans.last().expect("the run observes every scan it takes")
}

/// The scripted run over the checked-in model and dynamics document.
///
/// The legs are keyed off what the run observes rather than off fixed
/// scan numbers, so they hold whatever order the plant settles in.
fn run() -> Run {
    let model = fixture_model();
    let layout = ids();
    let driver = build_driver(&model);
    let sim = driver
        .sim()
        .expect("the train's devices all serve the local sim");
    let mut executor = assemble(&model, &dcs_controller::registry(), &driver).unwrap();

    // The declared load: the paced influent, the nominal oxygen uptake on
    // both zones, every machine available and healthy, the analog raw
    // loops' neutral seed, and the hardwired protective family clear —
    // plus one step so scan 1 sees the dynamics' declared state.
    sim.write(layout.influent_flow, CoreValue::Float(INFLUENT))
        .unwrap();
    for index in 0..REFERENCE_ZONES {
        sim.write(points::uptake(index), CoreValue::Float(UPTAKE))
            .unwrap();
    }
    for index in 0..REFERENCE_BLOWERS {
        sim.write(points::avail(index), CoreValue::Bool(true))
            .unwrap();
    }
    driver.step(DT).unwrap();

    let mut scans = Vec::with_capacity(SCANS as usize);
    let mut receipts = Vec::new();
    let observe = |executor: &dcs_runtime::Executor<'_>, scans: &mut Vec<Scan>| {
        let sample = |point| executor.sample(point).unwrap();
        let alarms = all_alarms(&layout)
            .iter()
            .map(|alarm| {
                (
                    bool_(sample(alarm.alarm)),
                    bool_(sample(alarm.unacknowledged)),
                )
            })
            .collect();
        scans.push(Scan {
            pressure: float(sample(layout.pressure)),
            pressure_quality: sample(layout.pressure).quality,
            pressure_sp: float(sample(layout.pressure_sp)),
            pressure_sp_slew: float(sample(layout.pressure_sp_slew)),
            blower_demand: float(sample(layout.blower_demand)),
            most_open: int(sample(layout.most_open)),
            at_bound: bool_(sample(layout.at_bound)),
            pulse_blocked: bool_(sample(layout.pulse_blocked)),
            staged: int(sample(layout.staged)),
            transition: bool_(sample(layout.transition)),
            staging_pending: bool_(sample(layout.staging_pending)),
            none_available: bool_(sample(layout.none_available)),
            all_faulted: bool_(sample(layout.all_faulted)),
            header_total: float(sample(layout.header_total)),
            alarms,
            zones: core::array::from_fn(|index| {
                let zone = &layout.zones[index];
                ZoneScan {
                    do_selected: float(sample(zone.do_selected)),
                    do_filtered: float(sample(zone.do_filtered)),
                    pid_trim: float(sample(zone.pid_trim)),
                    served: float(sample(zone.served)),
                    commanded: float(sample(zone.commanded)),
                    airflow: float(sample(zone.airflow)),
                    valve_cmd: float(sample(zone.valve_cmd)),
                    valve_pos: float(sample(zone.valve_pos)),
                    zone_total: float(sample(zone.zone_total)),
                    clamped: bool_(sample(zone.clamped)),
                    do_discrepancy: bool_(sample(zone.do_discrepancy)),
                    fallback_active: bool_(sample(zone.fallback_active)),
                    manual_active: bool_(sample(zone.manual_active)),
                    valve_discrepancy: bool_(sample(zone.valve_discrepancy)),
                    pulsing: bool_(sample(zone.pulsing)),
                    pulse_grant: bool_(sample(zone.pulse_grant)),
                    airflow_quality: sample(zone.airflow).quality,
                }
            }),
            blowers: core::array::from_fn(|index| {
                let blower = &layout.blowers[index];
                BlowerScan {
                    flow: float(sample(blower.flow)),
                    current: float(sample(blower.current)),
                    capacity: float(sample(blower.capacity)),
                    guarded: float(sample(blower.guarded)),
                    speed_cmd: float(sample(blower.speed_cmd)),
                    group_cmd: bool_(sample(blower.group_cmd)),
                    run_fb: bool_(sample(blower.run_fb)),
                    vent_cmd: bool_(sample(blower.vent_cmd)),
                    guarding: bool_(sample(blower.guarding)),
                    tripped: bool_(sample(blower.tripped)),
                    motor_fault: bool_(sample(blower.motor_fault)),
                    protective: bool_(sample(blower.protective)),
                    group_avail: bool_(sample(blower.group_avail)),
                    group_fault: bool_(sample(blower.group_fault)),
                }
            }),
        });
    };

    // The scripted legs' own latches. Each leg starts when the run
    // observes the condition it responds to, so the whole scenario holds
    // whatever order the plant settles in.
    let approve_held = false;
    let mut pulse_started = false;
    let mut surge_done = false;
    let mut fallback_started = false;
    let mut fallback_cleared = false;
    let mut low_do_started = false;
    let mut high_do_started = false;
    let mut manual_started = false;
    let mut discrepancy_started = false;
    let mut stale_started = false;
    let mut stale_frozen = false;
    let mut frozen_until = 0u64;
    let mut stale_seen = false;
    let mut reset_written = false;
    let mut reset_released = false;
    let mut protective_started = false;
    let mut tripped_started = false;
    let mut held_ack: Option<AlarmLayout> = None;
    let mut pending_acks: Vec<AlarmLayout> = Vec::new();
    let mut acked = vec![false; all_alarms(&layout).len()];

    // A one-scan-long Bool pulse on the shared operator release: the
    // group consumes the edge, so the request is asserted on the scan it
    // is written and released on the next.
    let mut release = |executor: &mut dcs_runtime::Executor<'_>,
                       receipts: &mut Vec<CommandReceipt>,
                       held: bool| {
        if held {
            op(
                executor,
                receipts,
                layout.approve,
                ValueKind::Bool,
                Value::Bool(false),
            );
        } else {
            op(
                executor,
                receipts,
                layout.approve,
                ValueKind::Bool,
                Value::Bool(true),
            );
        }
    };

    let mut approve_held = approve_held;
    for _scan in 1..=SCANS {
        executor.scan();
        observe(&executor, &mut scans);
        let current = run_snapshot(&scans).clone();
        let at = scans.len() as u64;

        // **Staging under the declared authority.** The group flags every
        // warranted stage change and holds it until the operator releases
        // it, one release per flag.
        if current.staging_pending {
            release(&mut executor, &mut receipts, approve_held);
            approve_held = !approve_held;
        } else if approve_held {
            op(
                &mut executor,
                &mut receipts,
                layout.approve,
                ValueKind::Bool,
                Value::Bool(false),
            );
            approve_held = false;
        }

        // **The pulse cap.** Both grids request a mixing pulse; the
        // declared cap admits the first and refuses the rest, which the
        // leg retires once the refusal has been observed.
        if !pulse_started && current.staged == REFERENCE_BLOWERS as i64 {
            pulse_started = true;
            for zone in &layout.zones {
                op(
                    &mut executor,
                    &mut receipts,
                    zone.pulse_start,
                    ValueKind::Bool,
                    Value::Bool(true),
                );
            }
        }
        if pulse_started && current.pulse_blocked {
            for zone in &layout.zones {
                op(
                    &mut executor,
                    &mut receipts,
                    zone.pulse_start,
                    ValueKind::Bool,
                    Value::Bool(false),
                );
            }
            pulse_started = false;
        }

        // **The surge guard.** A heavier influent drives both zones onto
        // their declared demand ceiling, and the summed machine airflow
        // carries the header pressure past the declared surge bound.
        if !surge_done && !pulse_started && at > 60 {
            surge_done = true;
            sim.write(layout.influent_flow, CoreValue::Float(40_000.0))
                .unwrap();
            // With the zones on their declared demand ceiling a light
            // oxygen load carries both zones' DO past the high tier.
            sim.write(points::uptake(0), CoreValue::Float(0.0)).unwrap();
            sim.write(points::uptake(1), CoreValue::Float(0.0)).unwrap();
        }
        if surge_done && at > 150 {
            sim.write(layout.influent_flow, CoreValue::Float(INFLUENT))
                .unwrap();
            sim.write(points::uptake(0), CoreValue::Float(UPTAKE))
                .unwrap();
            sim.write(points::uptake(1), CoreValue::Float(UPTAKE))
                .unwrap();
        }

        // **The declared DO-loss response.** Every probe of the second
        // zone reads `Bad`; the fallback engages with the declared safe
        // airflow and alarms, and recovers on the first `Good` scan.
        if !fallback_started && at > 180 {
            fallback_started = true;
            for zone in 0..REFERENCE_ZONES {
                for probe in 0..REFERENCE_PROBES {
                    sim.inject_fault(
                        points::do_probe(zone, probe),
                        Fault::Quality(Quality::Bad(QualityReason::DeviceFault)),
                    )
                    .unwrap();
                }
            }
        }
        if fallback_started && !fallback_cleared && at > 240 {
            fallback_cleared = true;
            for zone in 0..REFERENCE_ZONES {
                for probe in 0..REFERENCE_PROBES {
                    sim.clear_fault(points::do_probe(zone, probe)).unwrap();
                }
            }
        }

        // **The DO tiers.** A heavy oxygen load on the second zone drives
        // its DO past the low tier and stretches the redundant probes far
        // enough apart to raise the spread diagnostic; a light load on
        // the first drives its DO past the high tier.
        if !low_do_started && at > 300 {
            low_do_started = true;
            sim.write(points::uptake(1), CoreValue::Float(3.2)).unwrap();
        }
        if low_do_started && at > 380 {
            sim.write(points::uptake(1), CoreValue::Float(UPTAKE))
                .unwrap();
            low_do_started = false;
            high_do_started = true;
            sim.write(points::uptake(0), CoreValue::Float(0.2)).unwrap();
        }
        if high_do_started && at > 460 {
            sim.write(points::uptake(0), CoreValue::Float(UPTAKE))
                .unwrap();
            high_do_started = false;
        }

        // **The operator takeover and the mixing floor.** The second zone
        // is taken over below its declared mixing floor, so the airflow
        // tier and the takeover's engagement both annunciate.
        if !manual_started && at > 480 {
            manual_started = true;
            op(
                &mut executor,
                &mut receipts,
                layout.zones[1].manual_mode,
                ValueKind::Bool,
                Value::Bool(true),
            );
            op(
                &mut executor,
                &mut receipts,
                layout.zones[1].manual_rate,
                ValueKind::Float,
                Value::Float(40.0),
            );
        }
        if manual_started && at > 520 {
            // The first zone follows the second below its declared
            // mixing floor, so both zones' airflow tiers annunciate.
            op(
                &mut executor,
                &mut receipts,
                layout.zones[0].manual_mode,
                ValueKind::Bool,
                Value::Bool(true),
            );
            op(
                &mut executor,
                &mut receipts,
                layout.zones[0].manual_rate,
                ValueKind::Float,
                Value::Float(40.0),
            );
        }
        if manual_started && at > 560 {
            op(
                &mut executor,
                &mut receipts,
                layout.zones[1].manual_mode,
                ValueKind::Bool,
                Value::Bool(false),
            );
        }

        // **The air valve's proven discrepancy.** The first zone's
        // position feedback is driven untrusted, so the commanded and
        // proven positions can no longer agree; the fault is then cleared.
        if !discrepancy_started && at > 580 {
            discrepancy_started = true;
            sim.inject_fault(
                points::valve_pos(0),
                Fault::Quality(Quality::Bad(QualityReason::CommunicationFault)),
            )
            .unwrap();
        }
        if discrepancy_started && current.zones[0].valve_discrepancy && at > 620 {
            sim.clear_fault(points::valve_pos(0)).unwrap();
            discrepancy_started = false;
        }

        // **The declared stale path.** Both zones are pinned at a held
        // operator demand, the machines settle on the resulting capacity,
        // and the header pressure stops changing — the freshness budget
        // expires and the coordinator holds its last emitted set-point
        // rather than walking an untrusted reading.
        if !stale_started && at > 640 {
            stale_started = true;
            for zone in &layout.zones {
                op(
                    &mut executor,
                    &mut receipts,
                    zone.manual_mode,
                    ValueKind::Bool,
                    Value::Bool(true),
                );
                op(
                    &mut executor,
                    &mut receipts,
                    zone.manual_rate,
                    ValueKind::Float,
                    Value::Float(250.0),
                );
            }
            frozen_until = at + STALE_SPAN;
        }
        // The declared silence: for the declared budget's span the
        // simulated plant stops stepping, so its last report ages out on
        // the point's freshness budget — decision 45's contract that a
        // frozen measurement presents degraded, never as a healthy
        // last-known value.
        if stale_started && at <= frozen_until {
            stale_frozen = true;
        }
        if stale_frozen && current.pressure_quality == Quality::Uncertain(QualityReason::Stale) {
            stale_seen = true;
        }

        // **The totals' shared reset.** One declared write clears both the
        // zone and the header accounting, which then re-accumulates.
        if stale_seen && !reset_written {
            reset_written = true;
            op(
                &mut executor,
                &mut receipts,
                layout.total_reset,
                ValueKind::Bool,
                Value::Bool(true),
            );
        }
        if reset_written
            && !reset_released
            && current.header_total < 1.0
            && current.zones.iter().all(|zone| zone.zone_total < 1.0)
        {
            reset_released = true;
            op(
                &mut executor,
                &mut receipts,
                layout.total_reset,
                ValueKind::Bool,
                Value::Bool(false),
            );
        }

        // **The hardwired protective family and the proven surge trip.**
        if !protective_started && at > 780 {
            protective_started = true;
            sim.write(points::bearing(0), CoreValue::Bool(true))
                .unwrap();
        }
        if !tripped_started && at > 840 {
            tripped_started = true;
            sim.write(points::surge_trip(0), CoreValue::Bool(true))
                .unwrap();
            sim.write(points::surge_trip(1), CoreValue::Bool(true))
                .unwrap();
        }

        // Every standing unacknowledged alarm is queued for acknowledgment,
        // one per scan, so each latch clears through its own writable point
        // and each receipt settles on its own scan.
        for (index, alarm) in all_alarms(&layout).iter().enumerate() {
            if acked[index] {
                continue;
            }
            if bool_(executor.sample(alarm.unacknowledged).unwrap()) {
                acked[index] = true;
                pending_acks.push(alarm.clone());
            }
        }
        if let Some(alarm) = held_ack.take() {
            op(
                &mut executor,
                &mut receipts,
                alarm.ack,
                ValueKind::Bool,
                Value::Bool(false),
            );
        } else if !pending_acks.is_empty() {
            let alarm = pending_acks.remove(0);
            op(
                &mut executor,
                &mut receipts,
                alarm.ack,
                ValueKind::Bool,
                Value::Bool(true),
            );
            held_ack = Some(alarm);
        }
        if at > frozen_until {
            driver.step(DT).unwrap();
        }
    }
    let _ = &mut release;

    Run { scans, receipts }
}

#[test]
fn the_helper_re_emits_the_checked_in_document_exactly() {
    let emitted = serde_json::to_string_pretty(&emit().model).unwrap();
    let checked_in = std::fs::read_to_string(MODEL_JSON).unwrap();
    assert_eq!(
        emitted.trim_end(),
        checked_in.trim_end(),
        "the emitted document differs from crates/dcs-demo/fixtures/aeration_train.json"
    );
}

#[test]
fn identical_builder_invocations_emit_identical_documents() {
    let first = serde_json::to_string(
        &aeration_train(&AerationTrainConfig::reference())
            .unwrap()
            .model,
    )
    .unwrap();
    let second = serde_json::to_string(
        &aeration_train(&AerationTrainConfig::reference())
            .unwrap()
            .model,
    )
    .unwrap();
    assert_eq!(first, second);
}

#[test]
fn checked_in_document_validates_and_lints_clean() {
    let model = fixture_model();
    assert_eq!(model.version, dcs_model::MODEL_VERSION);
    assert!(model.validate().is_empty(), "{:?}", model.validate());
    // The train's only advisory is the declared `stale_after_ticks`
    // budget on the header pressure — freshness stays an opt-in
    // per-point declaration (decision 45); every other lint class stays
    // empty.
    let findings = model.lint();
    assert!(
        findings
            .iter()
            .all(|finding| finding.rule == LintRule::FieldInputWithoutFreshnessBudget),
        "{findings:?}"
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
fn the_dynamics_document_loads_through_the_plant_server_contract() {
    let model = fixture_model();
    let driver = build_driver(&model);
    assert!(driver.sim().is_some());
    assert_eq!(dynamics().len(), ELEMENTS);
}

#[test]
fn the_reference_train_declares_two_zones_over_two_machines() {
    let config = AerationTrainConfig::reference();
    assert_eq!(config.zones, REFERENCE_ZONES);
    assert_eq!(config.probes, REFERENCE_PROBES);
    assert_eq!(config.blowers, REFERENCE_BLOWERS);
    let train = emit();
    assert_eq!(train.layout.zones.len(), REFERENCE_ZONES);
    assert_eq!(train.layout.blowers.len(), REFERENCE_BLOWERS);
    // The reference trains declare the feed-forward layer and the
    // per-grid mixing pulses the decisions scope conditionally.
    assert!(config.feedforward);
    assert!(config.pulsed_mixing);
    assert_eq!(
        config.strategy, 1,
        "the reference resets on the most-open valve"
    );
    assert_eq!(config.max_pulsing, 1);
    assert_eq!(config.staging_authority, 1);
}

#[test]
fn the_m10_aeration_kind_families_are_the_ones_the_decisions_name() {
    let model = fixture_model();
    let kinds: std::collections::BTreeSet<&str> =
        model.components.iter().map(|c| c.kind.as_str()).collect();
    for kind in [
        // decision 62 — the shared-header coordinator and its slew bound
        "header-coordinator",
        "rate-limiter",
        // decision 63 — the blower group's staging policy
        "blower-group",
        // decision 64 — the surge and machine guard
        "surge-guard",
        // decision 65 — the declared DO-loss fallback
        "demand-fallback",
        // decision 66 — the additive feed-forward layer
        "flow-paced-ratio",
        "feedforward-sum",
        // decision 67 — the blower device contract and the DO loop
        "motor",
        "analog-output",
        "valve",
        "pid",
        "signal-filter",
        "median-voter",
        // the declared operator takeover
        "manual-station",
        // decision 68 — the measured totalization
        "totalizer",
        // decision 69 — the alarm set on the managed kinds
        "managed-latching-alarm",
        "managed-bool-latching-alarm",
        // decision 62's per-grid mixing timer
        "timer",
    ] {
        assert!(
            kinds.contains(kind),
            "the train composes no {kind} instance"
        );
    }
}

#[test]
fn the_field_points_carry_their_declared_engineering_units() {
    let model = fixture_model();
    let unit_of = |point: PointId| {
        model
            .io_points
            .iter()
            .find(|p| p.id == point)
            .and_then(|p| p.unit.clone())
    };
    assert_eq!(unit_of(layout().pressure), Some(unit::KPA.to_string()));
    assert_eq!(
        unit_of(layout().header_airflow),
        Some(unit::SM3_PER_H.to_string())
    );
    assert_eq!(
        unit_of(layout().influent_flow),
        Some(unit::SM3_PER_H.to_string())
    );
    let train = ids();
    for index in 0..REFERENCE_ZONES {
        assert_eq!(
            unit_of(points::airflow(index)),
            Some(unit::SM3_PER_H.to_string())
        );
        assert_eq!(
            unit_of(points::do_probe(index, 0)),
            Some(unit::MG_PER_L.to_string())
        );
        assert_eq!(
            unit_of(points::valve_cmd(index)),
            Some(unit::PERCENT.to_string())
        );
        assert_eq!(
            unit_of(train.zones[index].air_per_flow),
            Some(unit::SM3_PER_SM3.to_string())
        );
    }
    for index in 0..REFERENCE_BLOWERS {
        assert_eq!(
            unit_of(points::flow(index)),
            Some(unit::SM3_PER_H.to_string())
        );
        assert_eq!(
            unit_of(points::current(index)),
            Some(unit::AMPERES.to_string())
        );
        assert_eq!(
            unit_of(points::speed_cmd(index)),
            Some(unit::PERCENT.to_string())
        );
    }
}

fn layout() -> AerationTrainLayout {
    ids()
}

#[test]
fn the_journaled_adoption_covers_every_alarm_status_point() {
    let model = fixture_model();
    let journaled: std::collections::BTreeSet<PointId> = model
        .io_points
        .iter()
        .filter(|point| point.journaled)
        .map(|point| point.id)
        .collect();
    let train = ids();
    for alarm in all_alarms(&train) {
        // Every managed alarm's five status ports carry the decision-74
        // `journaled` declaration, so activation, return, the latch's
        // clear, shelving and out-of-service transitions land durably.
        for offset in 3..=7u64 {
            let point = PointId(alarm.alarm.0 - 3 + offset);
            assert!(
                journaled.contains(&point),
                "alarm status point {point:?} is not journaled"
            );
        }
    }
    // The declared protection-layer reports are journaled too, under
    // decision 77's boundary.
    for index in 0..REFERENCE_BLOWERS {
        assert!(journaled.contains(&points::bearing(index)));
        assert!(journaled.contains(&points::surge_trip(index)));
        assert!(journaled.contains(&points::avail(index)));
    }
}

#[test]
fn the_composition_panics_outside_the_declared_point_id_scheme() {
    let config = AerationTrainConfig {
        zones: 11,
        ..AerationTrainConfig::reference()
    };
    let panic = std::panic::catch_unwind(move || {
        let _ = aeration_train(&config);
    });
    assert!(panic.is_err(), "an over-wide zone count must be refused");
}

#[test]
#[should_panic(expected = "the train's point-id scheme admits 1..=8 machines")]
fn a_machine_count_outside_the_scheme_is_refused() {
    let config = AerationTrainConfig {
        blowers: 9,
        ..AerationTrainConfig::reference()
    };
    let _ = aeration_train(&config);
}

#[test]
#[should_panic(expected = "2oo3 median-voter over three probes")]
fn a_zone_without_three_probes_is_refused() {
    let config = AerationTrainConfig {
        probes: 2,
        ..AerationTrainConfig::reference()
    };
    let _ = aeration_train(&config);
}

/// The declared alarm index — the bank-level six first, then each zone's
/// six, then each machine's two.
const fn alarm_index(zone: usize, offset: usize) -> usize {
    BANK_ALARMS as usize + ZONE_ALARMS as usize * zone + offset
}

/// The first index satisfying `predicate`.
fn first_scan(scans: &[Scan], predicate: impl Fn(&Scan) -> bool) -> usize {
    scans_where(scans, predicate)
        .first()
        .copied()
        .unwrap_or_else(|| panic!("no scan satisfies the leg"))
}

/// The scans on which `predicate` holds.
fn scans_where(scans: &[Scan], predicate: impl Fn(&Scan) -> bool) -> Vec<usize> {
    scans
        .iter()
        .enumerate()
        .filter(|(_, scan)| predicate(scan))
        .map(|(index, _)| index)
        .collect()
}

/// The alarm index the named flag names on `scan`.
fn alarm_at(scan: &Scan, index: usize) -> (bool, bool) {
    scan.alarms[index]
}

/// Whether `observed` tracks `source` on every scan, allowing the
/// composition's declared carrier latency — the internal `Out`/`In`
/// carrier pairs deliver their reader one scan after the writer, and the
/// sampled values are read at different depths of that chain.
fn tracks(observed: &[f64], source: &[f64], lags: usize, tolerance: f64) -> bool {
    observed.iter().enumerate().all(|(index, value)| {
        (0..=lags.min(index)).any(|lag| (value - source[index - lag]).abs() <= tolerance)
    })
}

/// The sum of each scan's zone-level `f64` field.
fn sum_over_zones(scans: &[Scan], field: impl Fn(&ZoneScan) -> f64) -> Vec<f64> {
    scans
        .iter()
        .map(|scan| scan.zones.iter().map(&field).sum())
        .collect()
}

/// The sum of each scan's machine-level `f64` field.
fn sum_over_blowers(scans: &[Scan], field: impl Fn(&BlowerScan) -> f64) -> Vec<f64> {
    scans
        .iter()
        .map(|scan| scan.blowers.iter().map(&field).sum())
        .collect()
}

#[test]
fn do_feedback_drives_the_zone_valve_demand() {
    let run = run();
    let scans = &run.scans;
    let config = AerationTrainConfig::reference();

    // Every zone's loop closes on its declared set-point: the filtered DO
    // measurement reaches the operator's set-point and leaves it again,
    // and the air-valve command carries the zone's commanded airflow
    // through the declared engineering scaling.
    for zone in 0..REFERENCE_ZONES {
        let values: Vec<f64> = scans
            .iter()
            .map(|scan| scan.zones[zone].do_filtered)
            .collect();
        let lowest = values.iter().cloned().fold(f64::INFINITY, f64::min);
        let highest = values.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
        assert!(
            values
                .iter()
                .any(|value| (value - config.do_sp).abs() < 0.05),
            "zone {}'s DO loop never held its declared set-point: {lowest}..{highest}",
            zone + 1
        );
        assert!(
            lowest < config.do_low && highest > config.do_high,
            "zone {}'s DO never crossed both declared tiers: {lowest}..{highest}",
            zone + 1
        );
        // The 2oo3 voter stands between the probes and the loop: the
        // voted measurement the loop closes on never leaves the band its
        // probes hold.
        let selected: Vec<f64> = scans
            .iter()
            .map(|scan| scan.zones[zone].do_selected)
            .collect();
        assert!(selected.iter().all(|value| value.is_finite()));
        // The valve carries the zone's commanded airflow: the engineering
        // demand scaled onto the declared raw loop.
        let commanded: Vec<f64> = scans
            .iter()
            .map(|scan| scan.zones[zone].commanded)
            .collect();
        let expected: Vec<f64> = commanded
            .iter()
            .map(|value| value / config.max_demand * 100.0)
            .collect();
        let commands: Vec<f64> = scans
            .iter()
            .map(|scan| scan.zones[zone].valve_cmd)
            .collect();
        assert!(
            tracks(&commands, &expected, 4, 0.5),
            "zone {}'s valve command does not carry its commanded airflow",
            zone + 1
        );
        assert!(
            commands.iter().all(|value| (0.0..=100.0).contains(value)),
            "zone {}'s valve command left the declared raw loop",
            zone + 1
        );
        // The loop's trim is the additive correction on the paced term,
        // bounded by the declared authority.
        let trims: Vec<f64> = scans.iter().map(|scan| scan.zones[zone].pid_trim).collect();
        assert!(
            trims.iter().all(|value| {
                *value >= config.trim_min - 1e-6 && *value <= config.trim_max + 1e-6
            }),
            "zone {}'s loop trim left its declared authority",
            zone + 1
        );
    }
    // The DO feedback is what moves the demand: a heavier oxygen load
    // drives the trim and the valve up, and its removal drives them back.
    let heavy = first_scan(scans, |scan| scan.zones[0].do_filtered > config.do_sp + 0.5);
    let light = (heavy + 1..scans.len())
        .find(|index| scans[*index].zones[0].do_filtered < config.do_sp - 0.5)
        .unwrap_or_else(|| panic!("zone 1's DO never fell back below its set-point"));
    let mean = |from: usize, to: usize, field: fn(&ZoneScan) -> f64| {
        let to = to.min(scans.len()).max(from + 1);
        (from..to)
            .map(|index| field(&scans[index].zones[0]))
            .sum::<f64>()
            / (to - from) as f64
    };
    let under = light.min(light + 60);
    assert!(
        mean(heavy, light, |zone| zone.valve_cmd)
            > mean(under.min(scans.len() - 1), scans.len(), |zone| zone
                .valve_cmd),
        "zone 1's DO excursion did not move its valve command"
    );
}

#[test]
fn the_coordinator_resets_its_set_point_on_the_declared_cadence() {
    let run = run();
    let scans = &run.scans;
    let config = AerationTrainConfig::reference();

    // The most-open-valve reset walks the set-point: the emitted value
    // takes several distinct values and never leaves the declared bounds.
    let emitted: Vec<f64> = scans.iter().map(|scan| scan.pressure_sp).collect();
    let lowest = emitted.iter().cloned().fold(f64::INFINITY, f64::min);
    let highest = emitted.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
    assert!(
        lowest >= config.pressure_min - 1e-9 && highest <= config.pressure_max + 1e-9,
        "the emitted set-point left its declared bounds: {lowest}..{highest}"
    );
    assert!(
        (highest - lowest) > 10.0,
        "the most-open-valve reset never moved the set-point"
    );
    // The downstream rate limiter bounds the set-point's slew: the
    // bounded value never steps further than the declared per-scan delta,
    // and it converges on the emitted one.
    let slewed: Vec<f64> = scans.iter().map(|scan| scan.pressure_sp_slew).collect();
    assert!(
        slewed
            .windows(2)
            .all(|w| (w[1] - w[0]).abs() <= config.pressure_slew_kpa + 1e-9),
        "the slew limiter let the set-point step past its declared bound"
    );
    assert!(
        slewed.iter().all(|value| {
            *value >= config.pressure_min - 1e-9 && *value <= config.pressure_max + 1e-9
        }),
        "the slew-bounded set-point left its declared bounds"
    );
    assert!(
        (slewed.last().copied().unwrap() - *emitted.last().unwrap()).abs() < 1e-6,
        "the slew-bounded set-point never converged on the emitted one"
    );
    // The reset's own rule: while the most-open trusted valve sits below
    // the declared band's lower edge the set-point walks up, and while it
    // sits above the upper edge it walks down.
    let below = scans_where(scans, |scan| most_open(scan) < config.mov_band_lo);
    let above = scans_where(scans, |scan| most_open(scan) > config.mov_band_hi);
    assert!(
        !below.is_empty() && !above.is_empty(),
        "the run never sat on both sides of the declared near-open band"
    );
    assert!(
        walks_up(scans, &below) && walks_down(scans, &above),
        "the emitted set-point did not follow the most-open valve out of the band"
    );
    // And the coordinator names the most-open zone while its position is
    // trusted.
    assert!(
        !scans_where(scans, |scan| scan.most_open == 1).is_empty()
            && !scans_where(scans, |scan| scan.most_open == 2).is_empty(),
        "the most-open selection never named both zones"
    );
}

/// The most-open zone's trusted valve position at `scan`.
fn most_open(scan: &Scan) -> f64 {
    if scan.most_open < 1 {
        return f64::NAN;
    }
    scan.zones[(scan.most_open - 1) as usize].valve_pos
}

/// Whether the emitted set-point rose across the scans in `window`.
fn walks_up(scans: &[Scan], window: &[usize]) -> bool {
    window
        .iter()
        .filter(|index| **index > 0)
        .any(|index| scans[*index].pressure_sp > scans[index - 1].pressure_sp)
}

/// Whether the emitted set-point fell across the scans in `window`.
fn walks_down(scans: &[Scan], window: &[usize]) -> bool {
    window
        .iter()
        .filter(|index| **index > 0)
        .any(|index| scans[*index].pressure_sp < scans[index - 1].pressure_sp)
}

#[test]
fn the_coordinator_aggregates_the_zone_demands_over_its_mixing_floor() {
    let run = run();
    let scans = &run.scans;
    let config = AerationTrainConfig::reference();

    // The aggregate demand is the sum of the zones' commanded airflow,
    // floored at the declared mixing minimum.
    // On a scan where neither zone is taken over or in its declared
    // DO-loss response, both commanded airflows are trusted and the
    // aggregate demand is their sum over the declared floor.
    let summed = sum_over_zones(scans, |zone| zone.commanded);
    let demand: Vec<f64> = scans.iter().map(|scan| scan.blower_demand).collect();
    let trusted: Vec<f64> = scans
        .iter()
        .map(|scan| {
            if scan
                .zones
                .iter()
                .all(|zone| !zone.manual_active && !zone.fallback_active)
            {
                1.0
            } else {
                0.0
            }
        })
        .collect();
    let settled: Vec<usize> = (0..scans.len())
        .filter(|index| trusted[*index] > 0.0 && *index > 6)
        .collect();
    assert!(
        settled.len() > 50,
        "the run never held both zones on trusted demands"
    );
    let carried = settled
        .iter()
        .filter(|index| {
            let index = **index;
            (0..=12).any(|lag| {
                (demand[index] - summed[index - lag].max(config.min_total_airflow)).abs() <= 8.0
            })
        })
        .count();
    assert!(
        carried * 10 >= settled.len() * 9,
        "the aggregate demand carried the zones' summed commanded airflow on only \
         {carried} of {} settled scans",
        settled.len()
    );
    // The declared mixing floor is held whenever the sum falls below it.
    let floored = scans_where(scans, |scan| {
        scan.blower_demand <= config.min_total_airflow + 1e-6
    });
    assert!(
        !floored.is_empty(),
        "the run never drove the aggregate demand onto its declared floor"
    );
    for index in &floored {
        assert_eq!(
            scans[*index].blower_demand,
            config.min_total_airflow,
            "the declared mixing floor was not held on scan {}",
            index + 1
        );
    }
    // The floored condition is one of the coordinator's two at-bound
    // indications, and it asserts while the coordinator cannot optimize.
    assert!(
        floored.iter().all(|index| scans[*index].at_bound),
        "the coordinator did not report its at-bound condition on the floor"
    );
    assert!(!floored.is_empty());
}

#[test]
fn the_pulse_cap_admits_one_grid_and_refuses_the_rest() {
    let run = run();
    let scans = &run.scans;
    let config = AerationTrainConfig::reference();
    assert_eq!(config.max_pulsing, 1);

    // The declared cap admits exactly one grid while the rest stand
    // refused, and the refusal is the coordinator's own reported
    // condition.
    let capped = scans_where(scans, |scan| {
        scan.zones.iter().filter(|zone| zone.pulsing).count() > config.max_pulsing as usize
            && scan.pulse_blocked
    });
    assert!(
        !capped.is_empty(),
        "the declared cap never refused a request"
    );
    for index in capped {
        let granted = scans[index]
            .zones
            .iter()
            .filter(|zone| zone.pulse_grant)
            .count();
        assert!(
            granted <= config.max_pulsing as usize,
            "the declared cap admitted {granted} grids on scan {}",
            index + 1
        );
        assert!(
            (0..3).any(|lag| { index + lag < scans.len() && scans[index + lag].alarms[2].0 }),
            "the refused request did not annunciate near scan {}",
            index + 1
        );
    }
    // No grant ever stands for a grid that is not requesting.
    assert!(
        scans.iter().enumerate().all(|(index, scan)| {
            scan.zones.iter().enumerate().all(|(zone, record)| {
                !record.pulse_grant
                    || record.pulsing
                    || (1..4).any(|lag| index >= lag && scans[index - lag].zones[zone].pulsing)
            })
        }),
        "a pulse grant stood with no request"
    );
}

#[test]
fn the_group_stages_under_the_declared_operator_approval_authority() {
    let run = run();
    let scans = &run.scans;

    // The declared authority is operator approval: a warranted stage
    // change is flagged before it executes, and the group starts with
    // nothing staged.
    let pending = scans_where(scans, |scan| scan.staging_pending);
    assert!(
        !pending.is_empty(),
        "the group never flagged a stage change for the operator's release"
    );
    assert!(
        scans.iter().any(|scan| scan.staged == 0),
        "the group never started with nothing staged"
    );
    assert!(
        scans
            .iter()
            .any(|scan| scan.staged == REFERENCE_BLOWERS as i64),
        "the group never staged the whole bank"
    );
    assert!(
        scans.iter().any(|scan| scan.transition),
        "the group never reported a join or departure in progress"
    );
    assert!(
        pending.iter().all(|index| scans[*index].staging_pending),
        "the staging-pending flag flapped without a warranted change"
    );
    // Every stage change the run drove was answered by an operator
    // release on the shared writable point — the receipted path.
    let releases = run
        .receipts
        .iter()
        .filter(|receipt| {
            matches!(
                &receipt.command,
                dcs_core::Command::WriteValue { point, .. } if *point == ids().approve
            )
        })
        .count();
    assert!(
        releases >= pending.len(),
        "only {releases} releases answered {} flagged stage changes",
        pending.len()
    );
    // The vent dwell is part of the join: a machine commands its vent
    // open while it proves its start, and both machines reach the header.
    assert!(
        scans
            .iter()
            .any(|scan| scan.blowers.iter().any(|blower| blower.vent_cmd)),
        "no machine's unloading/vent valve opened while it proved its start"
    );
}

#[test]
fn the_capacity_split_stays_inside_the_declared_unit_bounds() {
    let run = run();
    let scans = &run.scans;
    let config = AerationTrainConfig::reference();

    for scan in scans {
        for (index, blower) in scan.blowers.iter().enumerate() {
            let max = config.unit_max_flow[index].min(config.unit_max_current[index]);
            if blower.group_cmd {
                assert!(
                    blower.capacity >= config.unit_min_flow[index] - 1e-9
                        && blower.capacity <= max + 1e-9,
                    "machine {}'s split capacity {} left its declared bounds [{}, {max}]",
                    index + 1,
                    blower.capacity,
                    config.unit_min_flow[index]
                );
            } else {
                assert_eq!(
                    blower.capacity, 0.0,
                    "an uncommanded machine emitted a capacity demand"
                );
            }
        }
    }
    // The equal split the group computes is the aggregate demand over the
    // joined machines, so two joined machines carry equal shares.
    let split = sum_over_blowers(scans, |blower| blower.capacity);
    let demand: Vec<f64> = scans.iter().map(|scan| scan.blower_demand).collect();
    let joined: Vec<f64> = scans.iter().map(|scan| scan.staged as f64).collect();
    let floor = config.unit_min_flow[0];
    let ceiling = config.unit_max_flow[0].min(config.unit_max_current[0]);
    for (index, scan) in scans.iter().enumerate() {
        if index < 40 || joined[index] < 1.0 || split[index] == 0.0 || scan.transition {
            continue;
        }
        // Each joined machine carries the aggregate demand's equal share
        // clamped into its own declared bounds.
        let carried = scan.blowers.iter().all(|blower| {
            if !blower.group_cmd {
                return true;
            }
            let tolerance = 2.0 + 0.03 * demand[index];
            (0..=12).any(|lag| {
                let candidate = if index >= lag {
                    Some(demand[index - lag])
                } else {
                    None
                }
                .or_else(|| (index + lag < scans.len()).then(|| demand[index + lag]));
                candidate.is_some_and(|value| {
                    (blower.capacity - (value / joined[index]).clamp(floor, ceiling)).abs()
                        <= tolerance
                })
            })
        });
        assert!(
            carried,
            "the joined machines' split {} does not carry the aggregate demand {} on scan {}",
            split[index],
            demand[index],
            index + 1
        );
    }
    let both = scans_where(scans, |scan| scan.staged == 2 && !scan.transition);
    assert!(!both.is_empty());
    for index in both {
        assert!(
            (scans[index].blowers[0].capacity - scans[index].blowers[1].capacity).abs() < 1e-6,
            "the equal split gave the two joined machines different shares on scan {}",
            index + 1
        );
    }
}

#[test]
fn the_surge_guard_guards_only_inside_its_declared_envelope() {
    let run = run();
    let scans = &run.scans;
    let config = AerationTrainConfig::reference();
    assert_eq!(
        config.surge_on_guard, 0,
        "the reference clamps rather than trips"
    );

    let guarding = scans_where(scans, |scan| {
        scan.blowers.iter().any(|blower| blower.guarding)
    });
    assert!(!guarding.is_empty(), "the surge guard never engaged");
    for index in guarding {
        let scan = &scans[index];
        for (unit, blower) in scan.blowers.iter().enumerate() {
            // Decision 64's fail-safe reading: an untrusted measurement
            // cannot vouch the machine sits outside the surge region, so
            // the stale header pressure reads as its bound crossed.
            let pressure_untrusted = scan.pressure_quality != Quality::Good;
            let outside = blower.flow < config.surge_min_flow
                || blower.current < config.surge_min_current
                || pressure_untrusted
                || scan.pressure > config.surge_max_pressure;
            assert_eq!(
                blower.guarding,
                outside,
                "machine {}'s guarding flag {} disagrees with its declared envelope at scan {} \
                 (flow {}, current {}, pressure {})",
                unit + 1,
                blower.guarding,
                index + 1,
                blower.flow,
                blower.current,
                scan.pressure
            );
        }
    }
    // The clamp holds the demand at each crossed bound's floor and never
    // above the pressure bound — decision 64's declared maintenance
    // direction.
    for scan in scans {
        let pressure_crossed =
            scan.pressure_quality != Quality::Good || scan.pressure > config.surge_max_pressure;
        for (unit, blower) in scan.blowers.iter().enumerate() {
            if blower.guarding && !blower.tripped {
                assert!(
                    blower.guarded >= config.surge_min_flow.min(config.surge_min_current) - 1e-6,
                    "machine {}'s guarded demand {} fell below the declared bound",
                    unit + 1,
                    blower.guarded
                );
            }
            if pressure_crossed {
                assert!(
                    blower.guarded <= config.surge_max_pressure + 1e-6,
                    "machine {}'s guarded demand {} rose above the declared pressure bound",
                    unit + 1,
                    blower.guarded
                );
            }
        }
    }
    // A proven surge trip drives the declared trip value and annunciates
    // whether or not a bound stands crossed.
    let tripped = scans_where(scans, |scan| scan.blowers.iter().any(|b| b.tripped));
    assert!(!tripped.is_empty(), "no machine ever proved a surge trip");
    for index in tripped {
        for (unit, blower) in scans[index].blowers.iter().enumerate() {
            if blower.tripped {
                assert!(
                    (blower.guarded - config.surge_trip_value).abs() < 1e-9,
                    "machine {}'s tripped guard did not drive the declared trip value",
                    unit + 1
                );
                assert!(
                    (0..4).any(|lag| {
                        (index >= lag
                            && scans[index - lag].alarms[alarm_index(REFERENCE_ZONES, 2 * unit)].0)
                            || (index + lag < scans.len()
                                && scans[index + lag].alarms
                                    [alarm_index(REFERENCE_ZONES, 2 * unit)]
                                .0)
                    }),
                    "machine {}'s proven trip did not annunciate near scan {}",
                    unit + 1,
                    index + 1
                );
            }
        }
    }
}

#[test]
fn the_hardwired_protective_family_aggregates_into_availability_and_fault() {
    let run = run();
    let scans = &run.scans;

    // A machine reporting a hardwired protective device loses its
    // availability and raises its fault for the group.
    let tripped = scans_where(scans, |scan| {
        scan.blowers.iter().any(|blower| blower.protective)
    });
    assert!(
        !tripped.is_empty(),
        "no hardwired protective device ever reported"
    );
    for index in tripped {
        for (unit, blower) in scans[index].blowers.iter().enumerate() {
            if blower.protective {
                assert!(
                    (0..6).any(|lag| {
                        index >= lag
                            && (scans[index - lag].blowers[unit].motor_fault
                                || scans[index - lag].blowers[unit].group_fault)
                    }) || (0..6).any(|lag| {
                        index + lag < scans.len()
                            && (scans[index + lag].blowers[unit].motor_fault
                                || scans[index + lag].blowers[unit].group_fault)
                    }),
                    "machine {}'s protective report never reached its fault aggregate",
                    unit + 1
                );
            }
        }
        assert!(
            (0..6).any(|lag| {
                (index >= lag && scans[index - lag].blowers.iter().any(|b| !b.group_avail))
                    || (index + lag < scans.len()
                        && scans[index + lag].blowers.iter().any(|b| !b.group_avail))
            }),
            "a tripped machine stayed available to the group on scan {}",
            index + 1
        );
    }
    // Every machine faulted is the group's own reported condition, and it
    // raises both named alarms.
    let all_faulted = scans_where(scans, |scan| scan.all_faulted);
    assert!(
        !all_faulted.is_empty(),
        "the group never reported every machine faulted"
    );
    for index in all_faulted {
        assert!(
            scans[index].none_available,
            "all-faulted stood with a machine available on scan {}",
            index + 1
        );
        assert!(
            (0..4).any(|lag| {
                (index >= lag && scans[index - lag].alarms[5].0)
                    || (index + lag < scans.len() && scans[index + lag].alarms[5].0)
            }),
            "the all-faulted condition did not annunciate near scan {}",
            index + 1
        );
        assert!(
            (0..4).any(|lag| {
                (index >= lag && scans[index - lag].alarms[4].0)
                    || (index + lag < scans.len() && scans[index + lag].alarms[4].0)
            }),
            "the no-machine condition did not annunciate near scan {}",
            index + 1
        );
    }
}

#[test]
fn the_demand_fallback_engages_and_alarms_on_an_all_bad_do() {
    let run = run();
    let scans = &run.scans;
    let config = AerationTrainConfig::reference();
    assert_eq!(
        config.on_bad, 2,
        "the reference declares the safe-airflow response"
    );

    let engaged = scans_where(scans, |scan| {
        scan.zones.iter().any(|zone| zone.fallback_active)
    });
    assert!(
        !engaged.is_empty(),
        "the declared DO-loss response never engaged"
    );
    for zone in 0..REFERENCE_ZONES {
        let own = scans_where(scans, |scan| scan.zones[zone].fallback_active);
        assert!(
            !own.is_empty(),
            "zone {}'s declared DO-loss response never engaged",
            zone + 1
        );
        for index in &own {
            let index = *index;
            let observed = &scans[index].zones[zone];
            assert!(
                (observed.served - config.safe_flow).abs() < 1e-6,
                "zone {}'s DO-loss response served {} instead of the declared safe airflow",
                zone + 1,
                observed.served
            );
            assert!(
                (0..4).any(|lag| {
                    index + lag < scans.len() && scans[index + lag].alarms[alarm_index(zone, 4)].0
                }),
                "zone {}'s DO-loss engagement did not annunciate",
                zone + 1
            );
        }
        // The response releases on the first scan the measurement is
        // trusted again — no latch of its own to clear by hand.
        let last = own.last().copied().unwrap();
        assert!(
            scans[last + 1..]
                .iter()
                .all(|scan| !scan.zones[zone].fallback_active),
            "zone {}'s DO-loss response did not release on recovery",
            zone + 1
        );
    }
    assert!(!engaged.is_empty());
}

#[test]
fn the_redundant_probes_and_their_tiers_annunciate() {
    let run = run();
    let scans = &run.scans;

    // The 2oo3 voter names the disagreeing probe set: an oxygen step
    // stretches the three declared probe time constants far enough apart
    // for the spread diagnostic to stand.
    let spread = scans_where(scans, |scan| {
        scan.zones.iter().any(|zone| zone.do_discrepancy)
    });
    assert!(
        !spread.is_empty(),
        "the probe spread never exceeded its tolerance"
    );
    for zone in 0..REFERENCE_ZONES {
        let own = scans_where(scans, |scan| scan.zones[zone].do_discrepancy);
        assert!(
            own.iter()
                .any(|index| scans[*index].alarms[alarm_index(zone, 3)].0),
            "zone {}'s probe spread did not annunciate",
            zone + 1
        );
        // Both DO tiers annunciate, in both directions.
        for offset in [0usize, 1] {
            assert!(
                scans
                    .iter()
                    .any(|scan| scan.alarms[alarm_index(zone, offset)].0),
                "zone {}'s DO tier {offset} never annunciated",
                zone + 1
            );
        }
    }
}

#[test]
fn the_operator_takeover_annunciates_and_its_mixing_floor_stands() {
    let run = run();
    let scans = &run.scans;
    let config = AerationTrainConfig::reference();

    for zone in 0..REFERENCE_ZONES {
        let engaged = scans_where(scans, |scan| scan.zones[zone].manual_active);
        assert!(
            !engaged.is_empty(),
            "zone {}'s operator takeover never engaged",
            zone + 1
        );
        assert!(
            engaged
                .iter()
                .any(|index| scans[*index].alarms[alarm_index(zone, 5)].0),
            "zone {}'s takeover engagement did not annunciate",
            zone + 1
        );
        // While the takeover serves, the zone delivers the operator's
        // declared rate — below the declared mixing floor, which raises
        // the airflow tier.
        let below = scans_where(scans, |scan| {
            scan.zones[zone].manual_active && scan.zones[zone].airflow < config.airflow_floor
        });
        assert!(
            !below.is_empty(),
            "zone {}'s takeover never served below its declared mixing floor",
            zone + 1
        );
        for index in below {
            assert!(
                (0..=4).any(|lag| {
                    index >= lag && scans[index - lag].alarms[alarm_index(zone, 2)].0
                }),
                "zone {}'s mixing-floor tier did not annunciate",
                zone + 1
            );
        }
        // The takeover releases without a latch of its own.
        let last = engaged.last().copied().unwrap();
        assert!(
            scans[last + 1..]
                .iter()
                .all(|scan| !scan.zones[zone].manual_active),
            "zone {}'s takeover did not release",
            zone + 1
        );
    }
}

#[test]
fn the_air_valve_proves_its_feedback_discrepancy() {
    let run = run();
    let scans = &run.scans;
    let faulted = scans_where(scans, |scan| {
        scan.zones.iter().any(|zone| zone.valve_discrepancy)
    });
    assert!(
        !faulted.is_empty(),
        "no air valve ever proved a discrepancy"
    );
    for index in &faulted {
        let index = *index;
        for zone in 0..REFERENCE_ZONES {
            if scans[index].zones[zone].valve_discrepancy {
                assert!(
                    scans[index].zones[zone].valve_cmd <= config_max_demand(),
                    "zone {}'s discrepancy stood with its command above the ceiling",
                    zone + 1
                );
            }
        }
    }
    // The valve is otherwise clean: the simulated plant's loopback keeps
    // the proven position on the commanded one, so no other scan reports
    // a divergence without the flag.
    // The flag stands exactly where the commanded and proven positions
    // disagree: on the faulted scan the command holds while the feedback
    // reports untrusted, and the flag follows its declared budget.
    let proven = faulted
        .iter()
        .any(|index| scan_flagged_while_diverged(&scans[*index]));
    assert!(
        proven,
        "the discrepancy flag never stood while the positions disagreed"
    );
}

/// The declared demand ceiling, so the assertion does not restate the
/// reference configuration inline.
fn config_max_demand() -> f64 {
    AerationTrainConfig::reference().max_demand
}

/// Whether any zone's flagged air valve disagrees with its command.
fn scan_flagged_while_diverged(scan: &Scan) -> bool {
    scan.zones
        .iter()
        .any(|zone| zone.valve_discrepancy && (zone.valve_pos - zone.valve_cmd).abs() > 1.0)
}

#[test]
fn the_declared_stale_pressure_path_holds_the_emitted_set_point() {
    let run = run();
    let scans = &run.scans;
    let config = AerationTrainConfig::reference();

    // The header pressure carries the train's single declared freshness
    // budget; the scripted silence longer than that budget presents the
    // measurement degraded, never as a healthy last-known value.
    let model = fixture_model();
    let declared: Vec<(PointId, u64)> = model
        .io_points
        .iter()
        .filter_map(|point| point.stale_after_ticks.map(|budget| (point.id, budget)))
        .collect();
    assert_eq!(
        declared,
        vec![(ids().pressure, config.pressure_stale_ticks)]
    );
    let stale = scans_where(scans, |scan| {
        scan.pressure_quality == Quality::Uncertain(QualityReason::Stale)
    });
    assert!(
        !stale.is_empty(),
        "the declared budget never expired on the run"
    );
    let first = stale[0];
    assert!(
        first >= config.pressure_stale_ticks as usize,
        "the budget expired before its declared patience"
    );
    // Under the most-open-valve reset a non-Good pressure holds the last
    // emitted set-point — the coordinator never walks an untrusted
    // reading.
    assert!(
        scans[first..]
            .windows(2)
            .all(|w| w[0].pressure_sp == w[1].pressure_sp),
        "the coordinator walked its set-point on an untrusted pressure"
    );
    // The scan keeps running: the run's later legs observe the restored
    // plant's own dynamics.
    assert!(
        scans[first..]
            .iter()
            .any(|scan| scan.pressure_quality == Quality::Good),
        "the measurement never recovered after the declared silence"
    );
}

#[test]
fn the_totalizers_accumulate_and_reset_through_the_shared_point() {
    let run = run();
    let scans = &run.scans;

    // Every zone's delivered airflow and the header's aggregate
    // accumulate.
    let last = scans.last().unwrap();
    assert!(
        last.header_total > 1000.0,
        "the header's aggregate airflow was never totalized"
    );
    for zone in 0..REFERENCE_ZONES {
        assert!(
            last.zones[zone].zone_total > 100.0,
            "zone {}'s delivered airflow was never totalized",
            zone + 1
        );
    }
    // One declared write on the shared reset point clears every total and
    // they then re-accumulate from zero.
    let first = (1..scans.len())
        .find(|index| {
            scans[*index].header_total < 1.0
                && scans[*index].zones.iter().all(|zone| zone.zone_total < 1.0)
                && scans[index - 1].header_total > 1000.0
        })
        .expect("the shared reset never cleared the accumulated totals");
    assert!(
        scans[first..].iter().any(|scan| scan.header_total > 1000.0),
        "the totals did not re-accumulate after the reset"
    );
    let resets = run
        .receipts
        .iter()
        .filter(|receipt| {
            matches!(
                &receipt.command,
                dcs_core::Command::WriteValue { point, .. } if *point == ids().total_reset
            )
        })
        .count();
    assert_eq!(
        resets, 2,
        "the reset is a request-and-release pair on one point"
    );
}

#[test]
fn every_declared_alarm_asserts_latches_and_clears_through_its_ack_point() {
    let run = run();
    let scans = &run.scans;
    let train = ids();

    for (index, alarm) in all_alarms(&train).iter().enumerate() {
        let asserted = scans_where(scans, |scan| alarm_at(scan, index).0);
        assert!(
            !asserted.is_empty(),
            "alarm {index} ({:?}) never asserted",
            alarm.component
        );
        let latched = scans_where(scans, |scan| alarm_at(scan, index).1);
        assert!(
            !latched.is_empty(),
            "alarm {index} asserted without latching until acknowledged"
        );
        // The latch clears only through its own writable ack point.
        assert!(
            scans
                .windows(2)
                .any(|w| w[0].alarms[index].1 && !w[1].alarms[index].1),
            "alarm {index}'s latch never cleared"
        );
        // The standing condition and the latch are the two-flag language:
        // the latch never asserts on a scan the condition never asserted.
        assert!(
            latched.iter().all(|scan_index| {
                asserted
                    .iter()
                    .any(|asserted_index| asserted_index <= scan_index)
            }),
            "alarm {index}'s latch asserted before its condition ever stood"
        );
        // The acknowledgment is a receipted write on the alarm's own
        // point, asserted and released.
        let acks = run
            .receipts
            .iter()
            .filter(|receipt| {
                matches!(
                    &receipt.command,
                    dcs_core::Command::WriteValue { point, .. } if *point == alarm.ack
                )
            })
            .count();
        assert!(
            acks >= 2,
            "alarm {index} cleared on {acks} acknowledgments, never through its own point"
        );
    }
}

#[test]
fn every_measured_scan_sample_is_finite() {
    let run = run();
    for (index, scan) in run.scans.iter().enumerate() {
        for (value, name) in [
            (scan.pressure, "header pressure"),
            (scan.pressure_sp, "pressure set-point"),
            (scan.pressure_sp_slew, "slew-bounded set-point"),
            (scan.blower_demand, "aggregate demand"),
            (scan.header_total, "header total"),
        ] {
            assert!(
                value.is_finite(),
                "scan {}'s {name} is not finite",
                index + 1
            );
        }
        for (zone, record) in scan.zones.iter().enumerate() {
            for (value, name) in [
                (record.do_selected, "voted DO"),
                (record.do_filtered, "filtered DO"),
                (record.pid_trim, "loop trim"),
                (record.served, "served demand"),
                (record.airflow, "delivered airflow"),
                (record.valve_cmd, "valve command"),
                (record.valve_pos, "valve position"),
                (record.zone_total, "zone total"),
            ] {
                assert!(
                    value.is_finite(),
                    "scan {}'s zone {} {name} is not finite",
                    index + 1,
                    zone + 1
                );
            }
        }
        for (unit, record) in scan.blowers.iter().enumerate() {
            for (value, name) in [
                (record.flow, "discharge airflow"),
                (record.current, "motor current"),
                (record.capacity, "capacity demand"),
                (record.guarded, "guarded demand"),
                (record.speed_cmd, "capacity command"),
            ] {
                assert!(
                    value.is_finite(),
                    "scan {}'s machine {} {name} is not finite",
                    index + 1,
                    unit + 1
                );
            }
        }
    }
}

#[test]
fn repeated_scripted_runs_produce_identical_output() {
    let first = run();
    let second = run();
    assert_eq!(first.scans, second.scans);
    assert_eq!(first.receipts, second.receipts);
}
