//! The nine kind specs that completed coverage — `latching-alarm`,
//! `manual-station`, `signal-filter`, `median-voter`, `totalizer`,
//! `sequencer`, `bool-gate`, `sr-latch`, `edge-trigger` — each
//! composes into a plant that builds, loads, validates, and assembles
//! through the standard registry (`dcs_controller::registry()`), the
//! same path a hand-written document takes.
//!
//! The registry-enumeration half of the coverage guard lives here too:
//! `every_registered_kind_has_a_spec` enumerates
//! `registry().kinds()` against the spec kinds this crate ships, so a
//! kind added to the registry without a `dcs-build` spec fails — the
//! spec-versus-`describe()` mirror check is `dcs-blocks`' spec-drift
//! test's business.

use std::collections::BTreeSet;

use dcs_assembly::{AssemblyError, assemble, sim_driver};
use dcs_build::specs::{
    AlarmMonitorSpec, AnalogInputSpec, AnalogOutputSpec, BoolGateSpec, CounterSpec,
    DigitalInputSpec, DigitalOutputSpec, EdgeTriggerSpec, InterlockSpec, LatchingAlarmSpec,
    ManualStationSpec, MedianVoterSpec, MotorSpec, OverrideSelectSpec, PidSpec, PumpGroupSpec,
    RateLimiterSpec, SequencerSpec, SignalFilterSpec, SrLatchSpec, TimerSpec, TotalizerSpec,
    ValveSpec,
};
use dcs_build::{BuildError, Direction, PlantBuilder, PointId, Value, parameters};
use dcs_core::IoDriver;
use dcs_model::PlantModel;

/// Builds `plant`, reloads the emitted document through `dcs-model`'s
/// loader — which validates it — and assembles it through the standard
/// registry against the simulated driver. Returns the reloaded model.
fn build_load_assemble(plant: PlantBuilder) -> PlantModel {
    let model = plant.build().unwrap();
    let json = serde_json::to_string_pretty(&model).unwrap();
    let reloaded = PlantModel::load(&json).unwrap();
    assert_eq!(reloaded, model);
    let driver = sim_driver(&reloaded).unwrap();
    assemble(&reloaded, &dcs_controller::registry(), &driver).unwrap();
    reloaded
}

#[test]
fn latching_alarm_spec_emits_an_assembling_document() {
    let mut plant = PlantBuilder::new();
    let sim = plant.device("sim").id;
    let level_raw = plant.channel::<f64>(sim, "level-raw", Direction::In);

    let pv = plant.field_input::<f64>(PointId(10), level_raw, false);
    let ack = plant.internal_input::<bool>(PointId(11), false, true);
    let alarm = plant.internal_output::<bool>(PointId(12), false);
    let unacknowledged = plant.internal_output::<bool>(PointId(13), false);

    let lal = plant.add(LatchingAlarmSpec::new(parameters([
        ("low_limit", Value::Float(10.0)),
        ("high_limit", Value::Float(90.0)),
        ("hysteresis", Value::Float(5.0)),
    ])));
    plant.connect(pv, lal.input);
    plant.connect(ack, lal.ack);
    plant.connect(&lal.alarm, alarm);
    plant.connect(&lal.unacknowledged, unacknowledged);

    let model = build_load_assemble(plant);
    assert_eq!(model.components[0].kind, LatchingAlarmSpec::KIND);
}

#[test]
fn manual_station_spec_emits_an_assembling_document() {
    let mut plant = PlantBuilder::new();
    let sim = plant.device("sim").id;
    let demand = plant.channel::<f64>(sim, "demand", Direction::In);

    let control = plant.field_input::<f64>(PointId(10), demand, false);
    let manual = plant.internal_input::<f64>(PointId(11), 50.0, true);
    let mode = plant.internal_input::<bool>(PointId(12), false, true);
    let out = plant.internal_output::<f64>(PointId(13), 0.0);
    let manual_active = plant.internal_output::<bool>(PointId(14), false);

    let mas = plant.add(ManualStationSpec::new(parameters([(
        "transfer_delta",
        Value::Float(5.0),
    )])));
    plant.connect(control, mas.control);
    plant.connect(manual, mas.manual);
    plant.connect(mode, mas.mode);
    plant.connect(&mas.out, out);
    plant.connect(&mas.manual_active, manual_active);

    let model = build_load_assemble(plant);
    assert_eq!(model.components[0].kind, ManualStationSpec::KIND);
}

#[test]
fn signal_filter_spec_emits_an_assembling_document() {
    let mut plant = PlantBuilder::new();
    let sim = plant.device("sim").id;
    let noisy = plant.channel::<f64>(sim, "noisy", Direction::In);

    let input = plant.field_input::<f64>(PointId(10), noisy, false);
    let filtered = plant.internal_output::<f64>(PointId(11), 0.0);

    let filter = plant.add(SignalFilterSpec::new(parameters([(
        "alpha",
        Value::Float(0.5),
    )])));
    plant.connect(input, filter.input);
    plant.connect(&filter.out, filtered);

    let model = build_load_assemble(plant);
    assert_eq!(model.components[0].kind, SignalFilterSpec::KIND);
}

#[test]
fn median_voter_spec_emits_an_assembling_document() {
    let mut plant = PlantBuilder::new();
    let sim = plant.device("sim").id;
    let first = plant.channel::<f64>(sim, "first", Direction::In);
    let second = plant.channel::<f64>(sim, "second", Direction::In);
    let third = plant.channel::<f64>(sim, "third", Direction::In);

    let in_1 = plant.field_input::<f64>(PointId(10), first, false);
    let in_2 = plant.field_input::<f64>(PointId(11), second, false);
    let in_3 = plant.field_input::<f64>(PointId(12), third, false);
    let voted = plant.internal_output::<f64>(PointId(13), 0.0);
    let discrepancy = plant.internal_output::<bool>(PointId(14), false);

    let voter = plant.add(MedianVoterSpec::new(parameters([(
        "tolerance",
        Value::Float(2.0),
    )])));
    plant.connect(in_1, voter.in_1);
    plant.connect(in_2, voter.in_2);
    plant.connect(in_3, voter.in_3);
    plant.connect(&voter.out, voted);
    plant.connect(&voter.discrepancy, discrepancy);

    let model = build_load_assemble(plant);
    assert_eq!(model.components[0].kind, MedianVoterSpec::KIND);
}

#[test]
fn totalizer_spec_emits_an_assembling_document() {
    let mut plant = PlantBuilder::new();
    let sim = plant.device("sim").id;
    let flow = plant.channel::<f64>(sim, "flow", Direction::In);

    let rate = plant.field_input::<f64>(PointId(10), flow, false);
    let reset = plant.internal_input::<bool>(PointId(11), false, true);
    let total = plant.internal_output::<f64>(PointId(12), 0.0);

    let tot = plant.add(TotalizerSpec::new(parameters([
        ("rate_unit", Value::Float(1.0)),
        ("rollover", Value::Float(1000.0)),
    ])));
    plant.connect(rate, tot.rate);
    plant.connect(reset, tot.reset);
    plant.connect(&tot.total, total);

    let model = build_load_assemble(plant);
    assert_eq!(model.components[0].kind, TotalizerSpec::KIND);
}

/// A two-step sequencer plant — the step table the emitted document
/// carries is `step_count` plus the per-step `step_<n>_ticks` /
/// `step_<n>_out` entries.
fn sequencer_plant(parameters_map: dcs_build::Parameters) -> PlantBuilder {
    let mut plant = PlantBuilder::new();
    let run = plant.internal_input::<bool>(PointId(10), false, true);
    let reset = plant.internal_input::<bool>(PointId(11), false, true);
    let out = plant.internal_output::<f64>(PointId(12), 0.0);
    let step = plant.internal_output::<i64>(PointId(13), 0);
    let done = plant.internal_output::<bool>(PointId(14), false);

    let seq = plant.add(SequencerSpec::new(parameters_map));
    plant.connect(run, seq.run);
    plant.connect(reset, seq.reset);
    plant.connect(&seq.out, out);
    plant.connect(&seq.step, step);
    plant.connect(&seq.done, done);
    plant
}

#[test]
fn sequencer_spec_emits_an_assembling_document() {
    let plant = sequencer_plant(parameters([
        ("step_count", Value::Int(2)),
        ("step_1_ticks", Value::Int(2)),
        ("step_1_out", Value::Float(10.0)),
        ("step_2_ticks", Value::Int(1)),
        ("step_2_out", Value::Float(20.0)),
    ]));

    let model = build_load_assemble(plant);
    assert_eq!(model.components[0].kind, SequencerSpec::KIND);
}

#[test]
fn sequencer_step_table_is_checked_at_assembly() {
    // `SequencerSpec` declares `declared_parameters() -> None` — the
    // `step_<n>_*` keys are indexed by `step_count`, so no static set
    // exists — and `build` accepts the map unchecked. The kind's
    // `from_parameters` is the authority instead: a missing
    // `step_2_out` surfaces as a named component failure when the
    // emitted document assembles.
    let model = sequencer_plant(parameters([
        ("step_count", Value::Int(2)),
        ("step_1_ticks", Value::Int(2)),
        ("step_1_out", Value::Float(10.0)),
        ("step_2_ticks", Value::Int(1)),
    ]))
    .build()
    .unwrap();
    let driver = sim_driver(&model).unwrap();
    let error = assemble(&model, &dcs_controller::registry(), &driver).unwrap_err();
    match &error {
        AssemblyError::Component { detail, .. } => assert!(
            detail.contains("step_2_out"),
            "the failure should name the missing entry, found {detail}"
        ),
        _ => panic!("expected a component construction failure, found {error:?}"),
    }
}

#[test]
fn bool_gate_spec_emits_an_assembling_document() {
    let mut plant = PlantBuilder::new();
    let sim = plant.device("sim").id;
    let first = plant.channel::<bool>(sim, "first", Direction::In);
    let second = plant.channel::<bool>(sim, "second", Direction::In);
    let third = plant.channel::<bool>(sim, "third", Direction::In);

    let in_1 = plant.field_input::<bool>(PointId(10), first, false);
    let in_2 = plant.field_input::<bool>(PointId(11), second, false);
    let in_3 = plant.field_input::<bool>(PointId(12), third, false);
    let out = plant.internal_output::<bool>(PointId(13), false);

    // `or` — code 1 — over a three-wide `in_N` set.
    let gate = plant.add(BoolGateSpec::new(
        parameters([("operation", Value::Int(1))]),
        3,
    ));
    plant.connect(in_1, gate.input(1));
    plant.connect(in_2, gate.input(2));
    plant.connect(in_3, gate.input(3));
    plant.connect(&gate.out, out);

    let model = build_load_assemble(plant);
    assert_eq!(model.components[0].kind, BoolGateSpec::KIND);
}

#[test]
fn bool_gate_rejects_an_undeclared_operation_code() {
    // `operation` declares the `GateOperation` code range `0..=2`; a
    // code outside it is `ParameterOutOfRange` at `build`, before the
    // document exists.
    let mut plant = PlantBuilder::new();
    let in_1 = plant.internal_input::<bool>(PointId(10), false, true);
    let out = plant.internal_output::<bool>(PointId(11), false);

    let gate = plant.add(BoolGateSpec::new(
        parameters([("operation", Value::Int(7))]),
        1,
    ));
    plant.connect(in_1, gate.input(1));
    plant.connect(&gate.out, out);

    assert!(matches!(
        plant.build(),
        Err(BuildError::ParameterOutOfRange { ref parameter, .. }) if parameter == "operation"
    ));
}

#[test]
fn pump_group_spec_emits_an_assembling_document() {
    let mut plant = PlantBuilder::new();

    let demand = plant.internal_input::<i64>(PointId(10), 0, true);
    let run_1 = plant.internal_input::<bool>(PointId(11), false, true);
    let run_2 = plant.internal_input::<bool>(PointId(12), false, true);
    let fault_1 = plant.internal_input::<bool>(PointId(13), false, true);
    let fault_2 = plant.internal_input::<bool>(PointId(14), false, true);
    let avail_1 = plant.internal_input::<bool>(PointId(15), true, true);
    let avail_2 = plant.internal_input::<bool>(PointId(16), true, true);
    let cmd_1 = plant.internal_output::<bool>(PointId(17), false);
    let cmd_2 = plant.internal_output::<bool>(PointId(18), false);
    let duty = plant.internal_output::<i64>(PointId(19), 0);
    let staged = plant.internal_output::<i64>(PointId(20), 0);
    let none_available = plant.internal_output::<bool>(PointId(21), false);
    let all_faulted = plant.internal_output::<bool>(PointId(22), false);

    // A two-pump group on the recorded default policy — per-cycle
    // alternation — with a two-tick inter-pump start delay.
    let group = plant.add(PumpGroupSpec::new(
        parameters([
            ("rotation", Value::Int(0)),
            ("start_delay_ticks", Value::Int(2)),
        ]),
        2,
    ));
    // The indexed pump handles resolve through the instance first;
    // the `demand` field moves into `connect`, so it is wired last.
    plant.connect(run_1, group.run(1));
    plant.connect(run_2, group.run(2));
    plant.connect(fault_1, group.fault(1));
    plant.connect(fault_2, group.fault(2));
    plant.connect(avail_1, group.avail(1));
    plant.connect(avail_2, group.avail(2));
    plant.connect(group.cmd(1), cmd_1);
    plant.connect(group.cmd(2), cmd_2);
    plant.connect(demand, group.demand);
    plant.connect(&group.duty, duty);
    plant.connect(&group.staged, staged);
    plant.connect(&group.none_available, none_available);
    plant.connect(&group.all_faulted, all_faulted);

    let model = build_load_assemble(plant);
    assert_eq!(model.components[0].kind, PumpGroupSpec::KIND);
}

#[test]
fn sr_latch_spec_emits_an_assembling_document() {
    let mut plant = PlantBuilder::new();
    let sim = plant.device("sim").id;
    let trip = plant.channel::<bool>(sim, "trip", Direction::In);

    let set = plant.field_input::<bool>(PointId(10), trip, false);
    let reset = plant.internal_input::<bool>(PointId(11), false, true);
    let out = plant.internal_output::<bool>(PointId(12), false);

    let latch = plant.add(SrLatchSpec::new(Default::default()));
    plant.connect(set, latch.set);
    plant.connect(reset, latch.reset);
    plant.connect(&latch.out, out);

    let model = build_load_assemble(plant);
    assert_eq!(model.components[0].kind, SrLatchSpec::KIND);
}

#[test]
fn edge_trigger_spec_emits_an_assembling_document() {
    let mut plant = PlantBuilder::new();
    let sim = plant.device("sim").id;
    let signal = plant.channel::<bool>(sim, "signal", Direction::In);

    let input = plant.field_input::<bool>(PointId(10), signal, false);
    let pulsed = plant.internal_output::<bool>(PointId(11), false);

    // `rising` — code 0.
    let trigger = plant.add(EdgeTriggerSpec::new(parameters([("edge", Value::Int(0))])));
    plant.connect(input, trigger.input);
    plant.connect(&trigger.out, pulsed);

    let model = build_load_assemble(plant);
    assert_eq!(model.components[0].kind, EdgeTriggerSpec::KIND);
}

#[test]
fn field_input_stale_after_emits_and_enforces_the_budget() {
    let mut plant = PlantBuilder::new();
    let sim = plant.device("sim").id;
    let level_raw = plant.channel::<f64>(sim, "level-raw", Direction::In);

    let pv = plant.field_input_stale_after::<f64>(PointId(10), level_raw, false, 2);

    // The emitted document carries the declared budget, and reloads
    // through `dcs-model`'s validating loader unchanged.
    let model = plant.build().unwrap();
    assert_eq!(model.io_points[0].stale_after_ticks, Some(2));
    let json = serde_json::to_string_pretty(&model).unwrap();
    let reloaded = PlantModel::load(&json).unwrap();
    assert_eq!(reloaded, model);

    // Assembled, the budget reaches the executor: the field sample the
    // driver stamped at its tick 0 turns Uncertain(Stale) once the lag
    // exceeds two scans, while the image stamp stays the scan tick.
    let driver = sim_driver(&reloaded).unwrap();
    let mut executor = assemble(&reloaded, &dcs_controller::registry(), &driver).unwrap();
    driver.write(pv.into(), Value::Float(7.0)).unwrap();
    executor.run(3).unwrap();
    let sample = executor
        .snapshot()
        .points
        .iter()
        .find(|point| point.point == pv.into())
        .and_then(|point| point.sample)
        .unwrap();
    assert_eq!(
        sample.quality,
        dcs_core::Quality::Uncertain(dcs_core::QualityReason::Stale)
    );
    assert_eq!(sample.tick, dcs_core::Tick(3));
}

/// The registry-enumeration coverage check: every kind the standard
/// registry serves has a `dcs-build` spec, and no spec names a kind the
/// registry does not serve. A kind registered without a spec fails
/// here — the mirror check (spec versus `describe()`) is
/// `dcs-blocks`' spec-drift test.
#[test]
fn every_registered_kind_has_a_spec() {
    let registry = dcs_controller::registry();
    let registered: BTreeSet<&str> = registry.kinds().collect();
    let specced: BTreeSet<&str> = [
        AnalogInputSpec::<f64>::KIND,
        AnalogOutputSpec::<f64>::KIND,
        PidSpec::KIND,
        DigitalInputSpec::KIND,
        DigitalOutputSpec::KIND,
        AlarmMonitorSpec::KIND,
        LatchingAlarmSpec::KIND,
        InterlockSpec::KIND,
        OverrideSelectSpec::KIND,
        ValveSpec::KIND,
        MotorSpec::KIND,
        TimerSpec::KIND,
        CounterSpec::KIND,
        RateLimiterSpec::KIND,
        ManualStationSpec::KIND,
        SignalFilterSpec::KIND,
        MedianVoterSpec::KIND,
        TotalizerSpec::KIND,
        SequencerSpec::KIND,
        BoolGateSpec::KIND,
        PumpGroupSpec::KIND,
        SrLatchSpec::KIND,
        EdgeTriggerSpec::KIND,
    ]
    .into_iter()
    .collect();
    assert_eq!(registered, specced);
}
