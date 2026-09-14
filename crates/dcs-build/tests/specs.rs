//! The six kind specs that completed coverage — `latching-alarm`,
//! `manual-station`, `signal-filter`, `median-voter`, `totalizer`,
//! `sequencer` — each composes into a plant that builds, loads,
//! validates, and assembles through the standard registry
//! (`dcs_controller::registry()`), the same path a hand-written
//! document takes.
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
    AlarmMonitorSpec, AnalogInputSpec, AnalogOutputSpec, CounterSpec, DigitalInputSpec,
    DigitalOutputSpec, InterlockSpec, LatchingAlarmSpec, ManualStationSpec, MedianVoterSpec,
    MotorSpec, OverrideSelectSpec, PidSpec, RateLimiterSpec, SequencerSpec, SignalFilterSpec,
    TimerSpec, TotalizerSpec, ValveSpec,
};
use dcs_build::{Direction, PlantBuilder, PointId, Value, parameters};
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
    ]
    .into_iter()
    .collect();
    assert_eq!(registered, specced);
}
