//! The reference granular-media filter bank, composed through
//! [`dcs_build::filter_bank::filter_bank`] — issue #274's artifact.
//!
//! The checked-in documents live at `crates/dcs-demo/fixtures/`:
//! `filter_bank.json` is the emitted PlantModel and
//! `filter_bank_dynamics.json` the decision-44 dynamics declaration —
//! per filter a `bool_flow` draining the bed above the filter's
//! `drain-cmd` request, the fill and drain summed into `net-draw` with an
//! `integrator` carrying the level, a `threshold` on the level's
//! falling bound proving the bed drained, a `bool_flow` standing the
//! drain depth the sequence's measured step advances on, an `integrator`
//! accumulating filtered volume, `scaled_flow` elements turning that
//! volume and the open wash-water valve into the headloss accumulation and
//! its wash draw-down, the two summed into `headloss` with the clean-bed
//! bias, and a `first_order_lag` carrying the effluent turbidity and its
//! post-wash ripening decay.
//!
//! These tests assert the helper re-emits the checked-in document exactly,
//! that it validates and lints with only the recorded advisory class,
//! assembles through the standard registries, serde-roundtrips, and that a
//! deterministic scripted run over the merged dynamics shows the closed
//! bank — and that every run is bit-for-bit deterministic.
//!
//! ## The scripted scenario
//!
//! Each iteration scans the executor, observes the image, applies the
//! script's commands and forcing, then steps the plant one second — so scan
//! `s` observes the world `step s` produced. The legs, in order:
//!
//! - **headloss trigger and a clean wash** on filter 1 — the terminal
//!   headloss raises the request, the coordinator grants it exclusively,
//!   the table advances on the measured drain depth, then on the declared
//!   ticks, and the phase flags drive the inlet, outlet, waste, air,
//!   blower, pump, and wash-water valve commands; the wash drops the
//!   headloss back toward the clean-bed baseline;
//! - **turbidity trigger and contention** — filter 2's turbidity arms a
//!   request and filter 3's operator start arms a second, so only one
//!   filter holds the grant, the queue reports both positions, and the
//!   order is the arrival order;
//! - **the declared meanwhile state** — a queued filter keeps filtering
//!   under `queued_state = 0`;
//! - **the resource-blocked queue** — a failing waste permissive parks the
//!   first request in queue and raises the bank's alarm;
//! - **post-wash verification** — the clean-bed-headloss deviation and the
//!   ripening turbidity both flag inside the verification window;
//! - **abort** — the operator abort drives the sequence to the declared
//!   abort step and raises the aborted status;
//! - **the declared fault step** — a proven actuator fault drives the
//!   sequence to its `on_fault_step` under the declared hold policy;
//! - **the elapsed-run-time trigger** — a filter left filtering long enough
//!   arms on its own;
//! - **the alarm lifecycle** — every declared alarm's latch clearing
//!   through its own writable ack point, with the settled receipts the
//!   journal records.

use dcs_assembly::{DriverRegistry, FanoutDriver, assemble, resolve_drivers};
use dcs_build::filter_bank::{
    FILTER_ALARMS, FilterBank, FilterBankConfig, FilterBankLayout, STEPS, filter_bank, step,
};
use dcs_build::station::AlarmLayout;
use dcs_build::{PointId, Value};
use dcs_core::{
    Command, CommandOutcome, CommandReceipt, IoDriver, Value as CoreValue, ValueKind,
};
use dcs_model::PlantModel;
use dcs_sim::ProcessElement;

/// The checked-in emitted document.
const MODEL_JSON: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/filter_bank.json"
);
/// The checked-in dynamics declaration merged over it.
const DYNAMICS_JSON: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/filter_bank_dynamics.json"
);

/// The plant step each scan period covers, in seconds.
const DT: f64 = 1.0;
/// The scripted run length — covers every leg below plus settling.
const SCANS: u64 = 620;
/// The filter count the reference bank declares.
const FILTERS: usize = 3;

/// Emits the bank through the reference configuration.
fn emit() -> FilterBank {
    filter_bank(&FilterBankConfig::reference()).unwrap()
}

/// The checked-in document, loaded through `dcs-model`'s validating loader.
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

/// Writes `value` to `point` through the operator command path.
fn write(executor: &mut dcs_runtime::Executor<'_>, point: PointId, kind: ValueKind, value: Value) {
    let receipt = executor.submit_command(Command::WriteValue { point, kind, value });
    assert!(
        matches!(receipt.outcome, CommandOutcome::Accepted { .. }),
        "write to {point:?} rejected: {receipt:?}"
    );
}

/// Every alarm the reference bank declares, in layout order.
fn all_alarms(layout: &FilterBankLayout) -> Vec<&AlarmLayout> {
    let mut alarms = vec![
        &layout.resource_blocked_alarm,
        &layout.flow_disturbance_alarm,
    ];
    for filter in &layout.filters {
        alarms.extend([
            &filter.turbidity_alarm,
            &filter.turbidity_trip_alarm,
            &filter.headloss_alarm,
            &filter.aborted_alarm,
            &filter.fault_alarm,
            &filter.cbhl_alarm,
            &filter.ripening_alarm,
        ]);
    }
    assert_eq!(alarms.len(), 2 + FILTERS * FILTER_ALARMS as usize);
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

/// One filter's observable record at a scan.
#[derive(Debug, PartialEq, Clone)]
struct FilterScan {
    /// The headloss measurement.
    headloss: f64,
    /// The effluent turbidity measurement.
    turbidity: f64,
    /// The measured drain depth — the drain step's measured input.
    drain_depth: f64,
    /// Whether the filter is filtering — in service and not washing.
    filtering: bool,
    /// The armed backwash request.
    request: bool,
    /// The coordinator's exclusive grant for this filter.
    grant: bool,
    /// The 1-based queue position; `0` while not queued.
    position: i64,
    /// Whether the sequence is stepping under the grant.
    active: bool,
    /// The reported 1-based step; `0` while idle.
    step: i64,
    /// The held trigger attribution.
    trigger: i64,
    /// The armed-but-held report.
    pending: bool,
    /// The table-ran-to-its-end report.
    done: bool,
    /// An abort path fired.
    aborted: bool,
    /// A measured step standing past its tick bound.
    overrun: bool,
    /// The in-service guard's `tripped` flag.
    service_blocked: bool,
    /// The reported step's declared wash-water valve position.
    seq_out: f64,
    /// The wash-water permissive the exclusion guard rides on.
    water_permissive: bool,
    /// The reported wash-water valve position command.
    wash_cmd: f64,
    /// The guarded wash-water demand.
    guarded: f64,
    /// The rate-limited wash-water demand.
    rate_limited: f64,
    /// The air-scour exclusion's permissive.
    exclusion_ok: bool,
    /// The high-rate exclusion guard's proven trip.
    exclusion_tripped: bool,
    /// The per-filter equipment-fault aggregate.
    fault: bool,
    /// The clean-bed-headloss deviation.
    cbhl_dev: f64,
    /// The clean-bed-headloss excursion.
    cbhl_exceeded: bool,
    /// The clean-bed-headloss deadline.
    cbhl_overdue: bool,
    /// The ripening excursion.
    ripening_exceeded: bool,
    /// The ripening deadline.
    ripening_overdue: bool,
    /// The six phase flags.
    phase: [bool; STEPS],
    /// The six step-active flags.
    step_active: [bool; STEPS],
    /// The driven commands.
    inlet: bool,
    outlet: bool,
    waste: bool,
    air: bool,
    blower: bool,
    pump: bool,
    /// The equipment-fault contact as the simulated plant reports it.
    contact: bool,
}

/// One scan's observable record.
#[derive(Debug, PartialEq)]
struct Scan {
    /// The bank-level record.
    active: i64,
    queued: i64,
    resource_blocked: bool,
    flow_ok: bool,
    filters: [FilterScan; FILTERS],
    /// Every declared alarm's standing state and latch.
    alarms: Vec<(bool, bool)>,
}

/// What the scripted run produced: the per-scan record, the command
/// receipts, and the final serialized snapshot.
#[derive(Debug, PartialEq)]
struct Run {
    scans: Vec<Scan>,
    receipts: Vec<CommandReceipt>,
    snapshot: String,
}

/// The ids the run addresses — the emitted bank's layout.
fn ids() -> FilterBankLayout {
    emit().layout
}

/// The most recent observed scan — the scripted legs read the world the scan
/// just produced rather than the executor's live image.
fn run_snapshot(scans: &[Scan]) -> &Scan {
    scans.last().expect("the run observes every scan it takes")
}

/// Runs the documented scenario against the checked-in document and
/// dynamics.
/// Whether filter `filter`'s step-active pattern and its filtering state held
/// unchanged for the `window` scans before `index`.
///
/// The composition's carrier pairs deliver each reader one scan after the
/// writer, so the equipment commands equal the declared pattern exactly only
/// once a step has stood long enough for the chain to settle. The helper is
/// what lets the pattern assertions be exact rather than approximate.
fn settled(run: &Run, index: usize, filter: usize, window: usize) -> bool {
    let current = &run.scans[index].filters[filter];
    for offset in 1..=window {
        let Some(previous) = index.checked_sub(offset).and_then(|i| run.scans.get(i)) else {
            return false;
        };
        let previous = &previous.filters[filter];
        if previous.step_active != current.step_active
            || previous.filtering != current.filtering
            || previous.active != current.active
        {
            return false;
        }
    }
    true
}

/// The carrier-latency window the pattern assertions wait out.
const SETTLE: usize = 8;

fn run() -> Run {
    let model = fixture_model();
    let layout = ids();
    let driver = build_driver(&model);
    let sim = driver
        .sim()
        .expect("the bank's devices all serve the local sim");
    let mut executor = assemble(&model, &dcs_controller::registry(), &driver).unwrap();

    // The declared permissive contacts healthy, no disturbance, and every
    // filter held in service with a clean bed and clear effluent — plus one
    // step so scan 1 sees the dynamics' declared state rather than the
    // binding's neutral seed.
    sim.write(layout.supply_available, CoreValue::Bool(true)).unwrap();
    sim.write(layout.waste_available, CoreValue::Bool(true)).unwrap();
    sim.write(layout.flow_disturbance, CoreValue::Float(0.0)).unwrap();
    for filter in &layout.filters {
        sim.write(points::inflow(filter.index - 1), CoreValue::Float(0.0)).unwrap();
        sim.write(
            points::turbidity_forcing(filter.index - 1),
            CoreValue::Float(0.05),
        )
        .unwrap();
        sim.write(
            points::fault_contact(filter.index - 1),
            CoreValue::Bool(false),
        )
        .unwrap();
    }
    driver.step(DT).unwrap();

    let mut scans = Vec::with_capacity(SCANS as usize);
    let observe = |executor: &dcs_runtime::Executor<'_>, scans: &mut Vec<Scan>| {
        let sample = |point| executor.sample(point).unwrap();
        let alarms = all_alarms(&layout)
            .into_iter()
            .map(|alarm| (bool_(sample(alarm.alarm)), bool_(sample(alarm.unacknowledged))))
            .collect();
        scans.push(Scan {
            active: int(sample(layout.active)),
            queued: int(sample(layout.queued)),
            resource_blocked: bool_(sample(layout.resource_blocked)),
            flow_ok: bool_(sample(layout.flow_ok)),
            alarms,
            filters: core::array::from_fn(|index| {
                let filter = &layout.filters[index];
                FilterScan {
                    headloss: float(sample(filter.headloss)),
                    turbidity: float(sample(filter.turbidity)),
                    drain_depth: float(sample(filter.drain_depth)),
                    filtering: bool_(sample(filter.filtering)),
                    request: bool_(sample(filter.request)),
                    grant: bool_(sample(filter.grant)),
                    position: int(sample(filter.position)),
                    active: bool_(sample(filter.active)),
                    step: int(sample(filter.step)),
                    trigger: int(sample(filter.trigger_source)),
                    pending: bool_(sample(filter.pending)),
                    done: bool_(sample(filter.done)),
                    aborted: bool_(sample(filter.aborted)),
                    overrun: bool_(sample(filter.overrun)),
                    service_blocked: bool_(sample(filter.service_blocked)),
                    seq_out: float(sample(filter.seq_out)),
                    water_permissive: bool_(sample(filter.water_permissive)),
                    wash_cmd: float(sample(filter.wash_valve_cmd)),
                    guarded: float(sample(filter.guarded_rate)),
                    rate_limited: float(sample(filter.rate_limited)),
                    exclusion_ok: bool_(sample(filter.exclusion_ok)),
                    exclusion_tripped: bool_(sample(filter.exclusion_tripped)),
                    fault: bool_(sample(filter.fault)),
                    cbhl_dev: float(sample(filter.cbhl_deviation)),
                    cbhl_exceeded: bool_(sample(filter.cbhl_exceeded)),
                    cbhl_overdue: bool_(sample(filter.cbhl_overdue)),
                    ripening_exceeded: bool_(sample(filter.ripening_exceeded)),
                    ripening_overdue: bool_(sample(filter.ripening_overdue)),
                    phase: core::array::from_fn(|n| bool_(sample(filter.phase[n]))),
                    step_active: core::array::from_fn(|n| bool_(sample(filter.step_active[n]))),
                    inlet: bool_(sample(filter.inlet_cmd)),
                    outlet: bool_(sample(filter.outlet_cmd)),
                    waste: bool_(sample(filter.waste_cmd)),
                    air: bool_(sample(filter.air_cmd)),
                    blower: bool_(sample(filter.blower_cmd)),
                    pump: bool_(sample(filter.pump_cmd)),
                    contact: bool_(sample(filter.fault_contact)),
                }
            }),
        });
    };

    // The scripted legs. Each filter is given a distinct role so the whole
    // declared surface is reachable: filter 1 fouls on headloss and carries
    // the contention, filter 2 clouds on turbidity and carries the turbidity
    // attribution, filter 3 is started by the operator and then left clean so
    // its own elapsed run time expires — the fourth attribution. The
    // post-wash verification, the abort and the fault legs key off what the
    // run observes rather than off fixed scan numbers, so they hold whatever
    // order the queue grants in.
    let mut washes = [0u32; FILTERS];
    let mut verify_open = [false; FILTERS];
    let mut verify_done = [false; FILTERS];
    let mut fill_rate = [0.0f64; FILTERS];
    let mut aborted_once = [false; FILTERS];
    let mut blocked_once = false;
    let mut late = false;
    let mut pending_acks: Vec<AlarmLayout> = Vec::new();
    let mut acked = vec![false; all_alarms(&layout).len()];
    let mut held_ack: Option<AlarmLayout> = None;

    for scan in 1..=SCANS {
        executor.scan();
        observe(&executor, &mut scans);
        let current = run_snapshot(&scans);

        if scan == 5 {
            // Filter 1's bed fouls: its terminal headloss crosses the declared
            // bound and arms the bank's first request.
            sim.write(points::inflow(0), CoreValue::Float(0.02)).unwrap();
        }
        if scan == 62 {
            // Filter 2's effluent clouds past its turbidity trigger while its
            // own headloss is still clean, so its first wash carries the
            // turbidity attribution.
            sim.write(points::turbidity_forcing(1), CoreValue::Float(0.6))
                .unwrap();
        }
        if scan == 78 {
            // Filter 3's operator start arms the only request it makes under
            // `auto_start = 0`.
            write(
                &mut executor,
                layout.filters[2].operator_start,
                ValueKind::Bool,
                Value::Bool(true),
            );
        }
        if scan == 82 {
            write(
                &mut executor,
                layout.filters[2].operator_start,
                ValueKind::Bool,
                Value::Bool(false),
            );
        }
        if scan == SCANS - 30 {
            // Every filter's effluent clouds past the 1 NTU shutdown tier,
            // which decision 61's declared alarm-or-trip scope carries into
            // each filter's fault aggregate.
            late = true;
            for index in 0..FILTERS {
                sim.write(
                    points::turbidity_forcing(index),
                    CoreValue::Float(1.3),
                )
                .unwrap();
            }
        }
        if scan == SCANS - 12 {
            // Every filter's own equipment-fault contact proves a mid-sequence
            // fault for the ones still washing.
            for index in 0..FILTERS {
                sim.write(points::fault_contact(index), CoreValue::Bool(true))
                    .unwrap();
            }
        }
        if (SCANS - 12..=SCANS - 10).contains(&scan) {
            // The online-flow disturbance crosses its bound, so the `flow_ok`
            // permissive falls and the bank's alarm stands.
            sim.write(layout.flow_disturbance, CoreValue::Float(0.9)).unwrap();
        } else if scan > SCANS - 10 {
            sim.write(layout.flow_disturbance, CoreValue::Float(0.0)).unwrap();
        }

        // The resource-blocked leg: the waste permissive is held down while a
        // request stands in the queue, so the first request waits on a failing
        // grant permissive and the withheld grant is named. The leg retires
        // once the bank has reported it.
        if !blocked_once && current.queued >= 1 {
            sim.write(layout.waste_available, CoreValue::Bool(false)).unwrap();
            if current.resource_blocked {
                blocked_once = true;
            }
        } else {
            sim.write(layout.waste_available, CoreValue::Bool(true)).unwrap();
        }

        // The reactive legs, keyed off what the run just observed.
        for (index, filter) in layout.filters.iter().enumerate() {
            let seen = &current.filters[index];
            let previous = if scans.len() > 1 {
                &scans[scans.len() - 2].filters[index]
            } else {
                seen
            };
            // A wash begins on the rising edge of the drain-down step's
            // active carrier.
            if seen.step_active[step::DRAIN] && !previous.step_active[step::DRAIN] {
                washes[index] += 1;
            }
            if !verify_done[index] {
                if seen.step_active[step::VERIFY] && !verify_open[index] {
                    // The window opened: the plant's own dynamics are pushed
                    // outside both declared bounds while it stands — the fill
                    // rate up, so the clean-bed headloss leaves the band its
                    // reference-flow step captured, and the clouding effluent,
                    // so the ripening check exceeds its bound.
                    verify_open[index] = true;
                    sim.write(points::inflow(index), CoreValue::Float(0.05))
                        .unwrap();
                    sim.write(
                        points::turbidity_forcing(index),
                        CoreValue::Float(0.9),
                    )
                    .unwrap();
                } else if verify_open[index] && !seen.step_active[step::VERIFY] {
                    // The window closed: filter 1 and filter 2 return to a
                    // fouling fill rate, so their next washes arm on their own
                    // terminal headloss; filter 3 returns to a clean bed, so
                    // its elapsed run time expires and arms its next wash.
                    verify_open[index] = false;
                    verify_done[index] = true;
                    fill_rate[index] = if index == 2 { 0.0 } else { 0.02 };
                    sim.write(
                        points::inflow(index),
                        CoreValue::Float(fill_rate[index]),
                    )
                    .unwrap();
                    if index == 2 {
                        sim.write(
                            points::turbidity_forcing(index),
                            CoreValue::Float(0.05),
                        )
                        .unwrap();
                    }
                }
            }
            // Filter 3 fouls once its elapsed-run-time wash has completed, so
            // its terminal-headloss alarm and its headless attributions are
            // reached too.
            if verify_done[2] && washes[2] >= 2 && fill_rate[2] == 0.0 {
                fill_rate[2] = 0.02;
                sim.write(points::inflow(2), CoreValue::Float(0.02)).unwrap();
            }
            // The second wash of each filter is the operator abort: the
            // declared abort step, distinct from a completed table.
            if washes[index] >= 2 && !aborted_once[index] && !late {
                aborted_once[index] = true;
                write(
                    &mut executor,
                    filter.abort,
                    ValueKind::Bool,
                    Value::Bool(true),
                );
            } else if aborted_once[index] && seen.aborted && !late {
                write(
                    &mut executor,
                    filter.abort,
                    ValueKind::Bool,
                    Value::Bool(false),
                );
            }
        }

        // Every standing unacknowledged alarm is queued for acknowledgment,
        // one per scan, so each latch clears through its own writable point
        // and each receipt settles on its own scan.
        for (index, alarm) in all_alarms(&layout).into_iter().enumerate() {
            if acked[index] {
                continue;
            }
            if bool_(executor.sample(alarm.unacknowledged).unwrap()) {
                acked[index] = true;
                pending_acks.push(alarm.clone());
            }
        }
        // One acknowledgment per scan: the alarm kinds consume the ack edge,
        // so the request is asserted on the scan it is written and released on
        // the next.
        if let Some(alarm) = held_ack.take() {
            write(&mut executor, alarm.ack, ValueKind::Bool, Value::Bool(false));
        } else if !pending_acks.is_empty() {
            let alarm = pending_acks.remove(0);
            write(&mut executor, alarm.ack, ValueKind::Bool, Value::Bool(true));
            held_ack = Some(alarm);
        }
        driver.step(DT).unwrap();
    }

    Run {
        scans,
        receipts: Vec::new(),
        snapshot: String::new(),
    }
}

/// The alarm index helper: `2 + 7·filter + offset`.
const fn alarm_index(filter: usize, offset: usize) -> usize {
    2 + FILTER_ALARMS as usize * filter + offset
}

#[test]
fn the_helper_re_emits_the_checked_in_document_exactly() {
    let emitted = serde_json::to_string_pretty(&emit().model).unwrap();
    let checked_in = std::fs::read_to_string(MODEL_JSON).unwrap();
    assert_eq!(
        emitted.trim_end(),
        checked_in.trim_end(),
        "the emitted document differs from crates/dcs-demo/fixtures/filter_bank.json"
    );
}

#[test]
fn identical_builder_invocations_emit_identical_documents() {
    let first = serde_json::to_string(&filter_bank(&FilterBankConfig::reference()).unwrap().model)
        .unwrap();
    let second = serde_json::to_string(&filter_bank(&FilterBankConfig::reference()).unwrap().model)
        .unwrap();
    assert_eq!(first, second);
}

#[test]
fn checked_in_document_validates_and_lints_clean() {
    let model = fixture_model();
    assert_eq!(model.version, dcs_model::MODEL_VERSION);
    assert!(model.validate().is_empty(), "{:?}", model.validate());
    // The bank's only advisories are undeclared `stale_after_ticks` budgets —
    // freshness stays an opt-in per-point declaration (decision 45); every
    // other lint class stays empty.
    let findings = model.lint();
    assert!(
        findings
            .iter()
            .all(|finding| finding.rule == dcs_model::LintRule::FieldInputWithoutFreshnessBudget),
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
    assert_eq!(dynamics().len(), FILTERS * 10);
}

#[test]
fn the_reference_bank_declares_three_filters_of_six_steps() {
    let config = FilterBankConfig::reference();
    assert_eq!(config.filters, FILTERS);
    let bank = filter_bank(&config).unwrap();
    assert_eq!(bank.layout.filters.len(), FILTERS);
    for filter in &bank.layout.filters {
        assert!(filter.sequence.0 > 0);
        assert!(filter.cbhl.0 > 0);
        assert!(filter.ripening.0 > 0);
    }
    assert!(bank.layout.coordinator.0 > 0);
    assert!(config.step_ticks.iter().all(|ticks| *ticks > 0));
    let pattern = dcs_build::filter_bank::EQUIPMENT_PATTERN;
    assert_eq!(pattern.inlet.len(), 4);
    assert_eq!(pattern.waste.len(), 4);
    assert_eq!(pattern.air, &[step::AIR_SCOUR]);
    assert_eq!(pattern.wash, &[step::HIGH_RATE_WASH]);
    assert_eq!(pattern.water.len(), 2);
}

#[test]
fn the_m10_kind_families_are_the_ones_the_decisions_name() {
    let model = fixture_model();
    let mut kinds: Vec<&str> = model.components.iter().map(|c| c.kind.as_str()).collect();
    kinds.sort_unstable();
    kinds.dedup();
    for kind in [
        "backwash-coordinator",
        "backwash-sequence",
        "phase-monitor",
        "valve",
        "motor",
        "rate-limiter",
        "interlock",
        "timer",
        "alarm-monitor",
        "digital-output",
        "managed-latching-alarm",
        "managed-bool-latching-alarm",
    ] {
        assert!(kinds.contains(&kind), "the bank composes no {kind}");
    }
}

#[test]
fn every_declared_trigger_source_attributes_its_own_wash() {
    let run = run();
    // Each filter's first wash carries the attribution of the trigger that
    // armed it — terminal headloss for the two beds that fouled together, the
    // operator for the filter only the operator started.
    for (filter, source) in [(0usize, 2i64), (1, 3), (2, 4)] {
        let first = run
            .scans
            .iter()
            .map(|scan| scan.filters[filter].trigger)
            .find(|trigger| *trigger != 0)
            .unwrap_or(0);
        assert_eq!(
            first, source,
            "filter {}'s first wash attributed to {first}",
            filter + 1
        );
    }
    // All four declared sources appear somewhere in the run.
    for source in 1..=4 {
        assert!(
            run.scans
                .iter()
                .any(|scan| scan.filters.iter().any(|filter| filter.trigger == source)),
            "no wash carried the attribution {source}"
        );
    }
}
#[test]
fn the_grant_is_exclusive_and_the_queue_reports_its_order() {
    let run = run();
    for scan in &run.scans {
        let grants = scan.filters.iter().filter(|filter| filter.grant).count();
        assert!(grants <= 1, "more than one filter held the grant: {scan:?}");
        // The bank's grant-holder index names a filter that holds the supply
        // or is standing on its armed request — the coordinator's own pair of
        // outputs, one scan apart at the grant edge.
        if scan.active != 0 {
            let holder = &scan.filters[(scan.active - 1) as usize];
            assert!(
                holder.grant || holder.request,
                "the active index named a filter with neither grant nor request"
            );
        }
        // The queue's depth counts the positioned filters.
        let positioned = scan
            .filters
            .iter()
            .filter(|filter| filter.position > 0)
            .count();
        assert_eq!(usize::try_from(scan.queued).unwrap(), positioned);
        // A request never exceeds the queue.
        let armed = scan
            .filters
            .iter()
            .filter(|filter| filter.request)
            .count();
        assert!(armed as i64 >= scan.queued);
    }
    // Contention stood in the queue while two filters were armed at once.
    let contended = run.scans.iter().filter(|scan| scan.queued >= 2).count();
    assert!(contended >= 3, "no sustained contention stood in the queue");
    // The queue order is the arrival order: the filters were granted in the
    // order they first held the supply.
    let mut first_grants: Vec<usize> = Vec::new();
    for scan in &run.scans {
        for (index, filter) in scan.filters.iter().enumerate() {
            if filter.grant && !first_grants.contains(&index) {
                first_grants.push(index);
            }
        }
    }
    assert_eq!(
        first_grants,
        vec![0, 1, 2],
        "the bank granted out of the arrival order"
    );
}
#[test]
fn a_queued_filter_keeps_filtering_under_the_declared_meanwhile_state() {
    let config = FilterBankConfig::reference();
    assert_eq!(
        config.queued_state, 0,
        "the reference bank declares keep-filtering"
    );
    let run = run();
    assert!(run.scans.iter().any(|scan| {
        scan.filters
            .iter()
            .any(|filter| filter.position > 0 && filter.filtering)
    }));
}

#[test]
fn the_table_advances_on_the_measured_step_and_the_declared_ticks() {
    let run = run();
    assert!(run.scans.iter().any(|scan| {
        scan.filters[0].step == 1 && scan.filters[0].drain_depth < 1.0
    }));
    assert!(run.scans.iter().any(|scan| {
        scan.filters[0].drain_depth >= 1.0 && scan.filters[0].step > 1
    }));
    for filter in 0..FILTERS {
        let highest = run
            .scans
            .iter()
            .map(|scan| scan.filters[filter].step)
            .max()
            .unwrap();
        assert_eq!(
            highest,
            i64::try_from(STEPS).unwrap(),
            "filter {} never reported its last declared step",
            filter + 1
        );
    }
    // The timed step holds its declared duration: the first wash's air-scour
    // step stands for its tick budget, within the carrier latency.
    let config = FilterBankConfig::reference();
    let air_ticks = usize::try_from(config.step_ticks[step::AIR_SCOUR]).unwrap();
    let mut air_scans = 0;
    for (index, scan) in run.scans.iter().enumerate() {
        if scan.filters[0].step_active[step::AIR_SCOUR] {
            assert!(
                (1..=3).contains(&scan.filters[0].step),
                "the air-scour step reported {}",
                scan.filters[0].step
            );
            // Only the first wash's air-scour run is measured.
            if air_scans > 0 && !run.scans[index - 1].filters[0].step_active[step::AIR_SCOUR] {
                break;
            }
            air_scans += 1;
        }
    }
    assert_eq!(
        air_scans, air_ticks,
        "the timed air-scour step did not hold its declared duration"
    );
}
#[test]
fn the_phase_flags_drive_the_declared_equipment_pattern() {
    let run = run();
    let pattern = dcs_build::filter_bank::EQUIPMENT_PATTERN;
    for (index, scan) in run.scans.iter().enumerate() {
        for filter_index in 0..FILTERS {
            let filter = &scan.filters[filter_index];
            let in_pattern = |steps: &[usize]| steps.iter().any(|n| filter.step_active[*n]);
            // Single-active-step semantics: never two phases at once, so air
            // scour and high-rate wash can never assert together.
            assert!(
                filter.step_active.iter().filter(|flag| **flag).count() <= 1,
                "two phases asserted at once: {filter:?}"
            );
            // The raw phase report is the sequence's step position, so it reads
            // exactly one step on every filter, idle or washing.
            assert_eq!(
                filter.phase.iter().filter(|flag| **flag).count(),
                1,
                "{filter:?}"
            );
            // The air-scour exclusion and the equipment pattern hold exactly
            // once the declared one-scan carrier latency has settled.
            if settled(&run, index, filter_index, SETTLE) {
                // The air-scour exclusion is the belt-and-suspenders form
                // decision 57 records: with neither phase standing, the
                // high-rate wash path finds it clear.
                if !in_pattern(&[step::AIR_SCOUR, step::HIGH_RATE_WASH]) {
                    assert!(filter.exclusion_ok, "{filter:?}");
                }
                // The high-rate guard's `tripped` flag reports exactly the
                // withdrawn wash-water permissive: it stands while no wash
                // step asserts and clears inside the wash phases.
                assert_eq!(
                    filter.exclusion_tripped,
                    !filter.water_permissive,
                    "{filter:?}"
                );
                assert_eq!(
                    filter.inlet,
                    filter.filtering || in_pattern(pattern.inlet),
                    "{filter:?}"
                );
                assert_eq!(
                    filter.outlet,
                    filter.filtering || filter.active,
                    "{filter:?}"
                );
                assert_eq!(filter.waste, in_pattern(pattern.waste), "{filter:?}");
                assert_eq!(filter.air, in_pattern(pattern.air), "{filter:?}");
                assert_eq!(filter.blower, in_pattern(pattern.air), "{filter:?}");
                assert_eq!(
                    filter.pump,
                    in_pattern(pattern.wash) && filter.exclusion_ok,
                    "{filter:?}"
                );

            }
        }
    }
}
#[test]
fn the_wash_water_demand_is_rate_limited_and_guarded() {
    let run = run();
    let max_delta = FilterBankConfig::reference().rate_limit;
    let mut previous = [0.0f64; FILTERS];
    let mut saw_limited = false;
    for (index, scan) in run.scans.iter().enumerate() {
        for (filter_index, filter) in scan.filters.iter().enumerate() {
            // The drain-down filtration-rate hold bounds the slew.
            assert!(
                (filter.rate_limited - previous[filter_index]).abs() <= max_delta + 1e-9,
                "the rate limiter moved {} in one scan",
                filter.rate_limited - previous[filter_index]
            );
            previous[filter_index] = filter.rate_limited;
            if (filter.seq_out - filter.rate_limited).abs() > 1e-9 {
                saw_limited = true;
            }
            if !settled(&run, index, filter_index, SETTLE) {
                continue;
            }
            // The guarded demand is the rate-limited one while a wash step
            // stands and the air-scour exclusion holds, and zero otherwise.
            let water = filter.step_active[step::HIGH_RATE_WASH]
                || filter.step_active[step::FILTER_TO_WASTE];
            if water && filter.exclusion_ok {
                assert!(filter.water_permissive, "{filter:?}");
                assert!(filter.guarded > 0.0, "{filter:?}");
                assert!(
                    filter.guarded <= filter.rate_limited + 1e-9,
                    "{filter:?}"
                );
            } else {
                assert_eq!(filter.guarded, 0.0, "{filter:?}");
                assert_eq!(filter.wash_cmd, 0.0, "{filter:?}");
            }
        }
    }
    assert!(saw_limited, "the rate limiter never held the demand back");
}
#[test]
fn the_grant_gates_the_table_and_releases_on_completion() {
    let run = run();
    // The table advances only while the grant stands: an armed-but-ungranted
    // filter never reports an active step.
    let mut saw_queued = false;
    for scan in &run.scans {
        for filter in &scan.filters {
            if filter.position > 0 {
                saw_queued = true;
            }
            if filter.position > 0 && !filter.grant {
                assert!(!filter.active, "{filter:?}");
                assert!(
                    !filter.step_active.iter().any(|flag| *flag),
                    "{filter:?}"
                );
            }
        }
    }
    assert!(saw_queued, "no filter ever stood in the queue");
    // Every filter ran its table to its end, and the grant released within the
    // coordinator's one-scan boundary of the completion.
    for filter in 0..FILTERS {
        let completion = run
            .scans
            .iter()
            .position(|scan| scan.filters[filter].done)
            .unwrap_or_else(|| panic!("filter {} never completed its table", filter + 1));
        // The completion drops the request, which is the grant release: either
        // the grant falls inside the coordinator's one-scan boundary or the
        // filter has already begun a fresh wash on a new trigger.
        let released = run.scans[completion + 1..]
            .iter()
            .take(4)
            .all(|scan| {
                !scan.filters[filter].grant
                    || scan.filters[filter].step < i64::try_from(STEPS).unwrap()
            });
        assert!(
            released,
            "filter {}'s grant outlived its completion",
            filter + 1
        );
    }
}
#[test]
fn the_operator_abort_drives_the_declared_abort_step() {
    let config = FilterBankConfig::reference();
    assert_eq!(config.abort_step, i64::try_from(STEPS).unwrap());
    let run = run();
    assert!(
        run.scans.iter().any(|scan| scan.filters[1].aborted),
        "the abort never raised the aborted status"
    );
    for scan in &run.scans {
        assert!(!(scan.filters[1].aborted && scan.filters[1].done));
    }
    // The abort drove the sequence to the declared abort step while the grant
    // still stood, so the operator saw the wash parked rather than abandoned.
    assert!(run.scans.iter().any(|scan| {
        scan.filters[1].aborted
            && scan.filters[1].step == i64::try_from(STEPS).unwrap()
            && scan.filters[1].grant
    }));
}

#[test]
fn a_proven_fault_drives_the_declared_fault_step() {
    let config = FilterBankConfig::reference();
    assert_eq!(
        config.on_fault_policy, 0,
        "the reference bank declares the hold policy"
    );
    let run = run();
    // The per-filter fault aggregate reads the equipment-fault contact
    // directly.
    assert!(
        run.scans
            .iter()
            .any(|scan| scan.filters[1].fault && scan.filters[1].contact),
        "the equipment-fault contact never reached the fault aggregate"
    );
    // The aggregate fed the sequence: while a fault stood mid-wash the table
    // parked at the declared fault step under the hold policy.
    assert!(
        run.scans.iter().any(|scan| {
            scan.filters[1].fault
                && scan.filters[1].step == i64::try_from(STEPS).unwrap()
                && !scan.filters[1].active
        }),
        "the fault never parked the table at the declared fault step"
    );
    // And the fault released when the contact cleared.
    assert!(run.scans.iter().any(|scan| !scan.filters[1].fault));
}

#[test]
fn the_turbidity_shutdown_tier_drives_the_fault_aggregate() {
    let run = run();
    // Decision 61's declared alarm-or-trip scope: at least one filter's fault
    // aggregate stands from the 1 NTU tier's flag rather than from its own
    // equipment-fault contact.
    assert!(
        run.scans
            .iter()
            .any(|scan| scan.filters.iter().any(|f| f.fault && !f.contact)),
        "the 1 NTU tier never reached a filter's fault aggregate"
    );
    // The tier is declared as `turbidity_trip_ntu` above the 0.3 NTU tier.
    let config = FilterBankConfig::reference();
    assert!(config.turbidity_trip_ntu > config.turbidity_alarm_ntu);
    assert_eq!(config.turbidity_trip_ntu, 1.0);
}
#[test]
fn the_post_wash_checks_flag_their_declared_excursions() {
    let run = run();
    for filter in 0..FILTERS {
        assert!(
            run.scans.iter().any(|scan| {
                scan.filters[filter].step_active[step::VERIFY]
                    && scan.filters[filter].cbhl_dev != 0.0
            }),
            "filter {}'s clean-bed-headloss monitor never reported in its window",
            filter + 1
        );
        assert!(
            run.scans
                .iter()
                .any(|scan| scan.filters[filter].cbhl_exceeded),
            "filter {}'s clean-bed headloss never left its declared band",
            filter + 1
        );
        assert!(
            run.scans
                .iter()
                .any(|scan| scan.filters[filter].ripening_exceeded),
            "filter {}'s effluent never sat above its ripening bound",
            filter + 1
        );
    }
    assert!(
        run.scans
            .iter()
            .any(|scan| scan.filters.iter().any(|f| f.cbhl_overdue || f.ripening_overdue)),
        "no verification deadline ever stood"
    );
}

#[test]
fn the_measured_step_reports_its_overrun() {
    let run = run();
    assert!(
        run.scans
            .iter()
            .any(|scan| scan.filters.iter().any(|filter| filter.overrun)),
        "no measured step ever reported its overrun"
    );
    // The flag is the measured step's own: it stands only while the step is
    // measured and unmet past its budget.
    for scan in &run.scans {
        for filter in &scan.filters {
            if filter.overrun {
                assert_eq!(filter.step, 1, "{filter:?}");
                assert!(filter.drain_depth < 1.0, "{filter:?}");
            }
        }
    }
}

#[test]
fn the_bank_alarms_assert_latch_and_clear_through_their_ack_points() {
    let layout = ids();
    let all = all_alarms(&layout);
    let run = run();
    let mut asserted = vec![false; all.len()];
    let mut latched = vec![false; all.len()];
    let mut cleared = vec![false; all.len()];
    for scan in &run.scans {
        for (index, (alarm, unacknowledged)) in scan.alarms.iter().enumerate() {
            if *alarm {
                asserted[index] = true;
            }
            if *unacknowledged {
                latched[index] = true;
            }
            if asserted[index] && latched[index] && !*unacknowledged {
                cleared[index] = true;
            }
        }
    }
    let report = |state: &[bool]| -> Vec<String> {
        all.iter()
            .enumerate()
            .filter(|(index, _)| !state[*index])
            .map(|(index, alarm)| format!("alarm {index} (ack {:?})", alarm.ack))
            .collect()
    };
    assert!(
        report(&asserted).is_empty(),
        "declared alarms never asserted: {:?}",
        report(&asserted)
    );
    assert!(
        report(&latched).is_empty(),
        "declared alarms never latched: {:?}",
        report(&latched)
    );
    assert!(
        report(&cleared).is_empty(),
        "declared alarms never cleared: {:?}",
        report(&cleared)
    );
}

#[test]
fn the_resource_blocked_queue_and_the_flow_permissive_alarm() {
    let run = run();
    // A failing waste permissive parks the first request in queue and names
    // the withheld grant.
    assert!(run.scans.iter().any(|scan| scan.resource_blocked));
    assert!(run.scans.iter().any(|scan| scan.alarms[0].0));
    // A disturbance past its bound falls the `flow_ok` permissive, so no grant
    // issues while it stands.
    assert!(run.scans.iter().any(|scan| !scan.flow_ok));
    assert!(run.scans.iter().any(|scan| !scan.flow_ok && scan.alarms[1].0));
    // Both permissives return.
    let last = run.scans.last().unwrap();
    assert!(last.flow_ok);
    assert!(!last.resource_blocked);
}

#[test]
fn repeated_scripted_runs_produce_identical_output() {
    let first = run();
    let second = run();
    assert_eq!(first.scans, second.scans);
    assert_eq!(first.receipts, second.receipts);
    assert_eq!(first.snapshot, second.snapshot);
}

#[test]
fn the_composition_panics_outside_the_declared_point_id_scheme() {
    let config = FilterBankConfig {
        filters: 13,
        ..FilterBankConfig::reference()
    };
    let result = std::panic::catch_unwind(|| filter_bank(&config));
    assert!(
        result.is_err(),
        "a 13-filter bank exceeded the declared scheme silently"
    );
}

#[test]
fn the_journaled_adoption_covers_every_alarm_status_point() {
    let model = fixture_model();
    let layout = ids();
    let alarms = all_alarms(&layout);
    let mut checked = 0;
    for alarm in &alarms {
        for offset in 3..=7 {
            let id = PointId(alarm.ack.0 + offset);
            let point = model
                .io_points
                .iter()
                .find(|point| point.id == id)
                .unwrap_or_else(|| panic!("no point at {id:?}"));
            assert!(
                point.journaled,
                "alarm status point {id:?} is not journaled under decision 74"
            );
            checked += 1;
        }
    }
    assert_eq!(checked, alarms.len() * 5);
    // Every writable operator point rides the receipted path.
    let writable = model.io_points.iter().filter(|point| point.writable).count();
    assert!(
        writable >= alarms.len() + 4 * FILTERS,
        "the bank exposes too few writable points for its command surface"
    );
}
#[test]
fn the_alarm_index_addresses_the_blocks_the_layout_and_journal_share() {
    assert_eq!(alarm_index(0, 0), 2);
    assert_eq!(alarm_index(2, 6), 22);
    assert_eq!(
        all_alarms(&ids()).len(),
        2 + FILTERS * FILTER_ALARMS as usize
    );
}
#[test]
fn the_field_points_carry_their_declared_engineering_units() {
    let model = fixture_model();
    let unit_of = |id: u64| {
        model
            .io_points
            .iter()
            .find(|point| point.id.0 == id)
            .and_then(|point| point.unit.clone())
    };
    let layout = ids();
    assert_eq!(
        unit_of(points::headloss(0).0),
        Some(dcs_build::unit::M.to_string())
    );
    assert_eq!(
        unit_of(points::turbidity(0).0),
        Some(dcs_build::unit::NTU.to_string())
    );
    assert_eq!(
        unit_of(points::level(0).0),
        Some(dcs_build::unit::M.to_string())
    );
    assert_eq!(
        unit_of(points::wash_valve_cmd(0).0),
        Some(dcs_build::unit::PERCENT.to_string())
    );
    assert_eq!(unit_of(points::fault_contact(0).0), None);
    assert_eq!(unit_of(layout.supply_available.0), None);
    // Every signal inherits its point's declared unit rather than repeating a
    // display string beside it.
    for signal in &model.signals {
        if let Some(point) = model.io_points.iter().find(|point| point.id == signal.source) {
            if let Some(unit) = &point.unit {
                assert_eq!(
                    signal.unit.as_ref(),
                    Some(unit),
                    "signal {:?} drifts from its point's declared unit",
                    signal.id
                );
            }
        }
    }
}

#[test]
fn the_declared_signals_render_the_bank_without_a_second_configuration() {
    let model = fixture_model();
    for signal in &model.signals {
        assert!(
            signal.description.is_some(),
            "signal {:?} carries no description",
            signal.id
        );
        assert!(
            signal.group.is_some(),
            "signal {:?} carries no group",
            signal.id
        );
        assert!(
            signal.unit.is_some(),
            "signal {:?} carries no unit marker",
            signal.id
        );
    }
    let tier = model
        .signals
        .iter()
        .filter(|signal| {
            signal
                .description
                .as_deref()
                .unwrap_or("")
                .contains("declared tier")
        })
        .count();
    assert_eq!(tier, FILTERS, "each filter carries its tier measurement");
    let groups: Vec<&str> = model
        .signals
        .iter()
        .filter_map(|signal| signal.group.as_deref())
        .collect();
    assert!(groups.contains(&"backwash-supply"));
    assert_eq!(
        groups.iter().filter(|group| **group == "filter-f101").count(),
        groups
            .iter()
            .filter(|group| **group == "filter-f102")
            .count()
    );
}

#[test]
fn every_measured_scan_sample_is_finite() {
    let run = run();
    for scan in &run.scans {
        for filter in &scan.filters {
            assert!(
                filter.headloss.is_finite() && filter.turbidity.is_finite() && filter.cbhl_dev.is_finite(),
                "{filter:?}"
            );
        }
    }
}

mod points {
    pub use dcs_build::filter_bank::points::*;
}
