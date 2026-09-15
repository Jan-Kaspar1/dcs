//! The kind specs that completed coverage — `latching-alarm`,
//! `manual-station`, `signal-filter`, `median-voter`, `totalizer`,
//! `sequencer`, `bool-gate`, `sr-latch`, `edge-trigger`,
//! `bool-latching-alarm` — each
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
    AlarmMonitorSpec, AnalogInputSpec, AnalogOutputSpec, BackwashCoordinatorSpec, BoolGateSpec,
    BoolLatchingAlarmSpec, CounterSpec, DigitalInputSpec, DigitalOutputSpec, EdgeTriggerSpec,
    FailoverSelectSpec, FlowPacedRatioSpec, InterlockSpec, LatchingAlarmSpec, ManualStationSpec,
    MedianVoterSpec, MotorSpec, OverrideSelectSpec, PidSpec, PumpGroupSpec, RateLimiterSpec,
    SequencerSpec, SignalFilterSpec, SrLatchSpec, ThresholdChainSpec, TimerSpec, TotalizerSpec,
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
fn bool_latching_alarm_spec_emits_an_assembling_document() {
    let mut plant = PlantBuilder::new();
    let sim = plant.device("sim").id;
    let power_fail = plant.channel::<bool>(sim, "power-fail", Direction::In);

    let input = plant.field_input::<bool>(PointId(10), power_fail, false);
    let ack = plant.internal_input::<bool>(PointId(11), false, true);
    let alarm = plant.internal_output::<bool>(PointId(12), false);
    let unacknowledged = plant.internal_output::<bool>(PointId(13), false);

    let bal = plant.add(BoolLatchingAlarmSpec::new(Default::default()));
    plant.connect(input, bal.input);
    plant.connect(ack, bal.ack);
    plant.connect(&bal.alarm, alarm);
    plant.connect(&bal.unacknowledged, unacknowledged);

    let model = build_load_assemble(plant);
    assert_eq!(model.components[0].kind, BoolLatchingAlarmSpec::KIND);
    assert!(model.components[0].parameters.is_empty());
}

#[test]
fn bool_latching_alarm_rejects_an_undeclared_parameter() {
    // The kind declares no parameters: a stray key — a `latching-alarm`
    // tunable carried onto the Bool sibling, say — is `UnknownParameter`
    // naming the key at `build`, before the document exists.
    let mut plant = PlantBuilder::new();
    let input = plant.internal_input::<bool>(PointId(10), false, true);
    let ack = plant.internal_input::<bool>(PointId(11), false, true);
    let alarm = plant.internal_output::<bool>(PointId(12), false);
    let unacknowledged = plant.internal_output::<bool>(PointId(13), false);

    let bal = plant.add(BoolLatchingAlarmSpec::new(parameters([(
        "hysteresis",
        Value::Float(0.5),
    )])));
    plant.connect(input, bal.input);
    plant.connect(ack, bal.ack);
    plant.connect(&bal.alarm, alarm);
    plant.connect(&bal.unacknowledged, unacknowledged);

    assert!(matches!(
        plant.build(),
        Err(BuildError::UnknownParameter { ref parameter, .. }) if parameter == "hysteresis"
    ));
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
fn threshold_chain_spec_emits_an_assembling_document() {
    let mut plant = PlantBuilder::new();
    let sim = plant.device("sim").id;
    let level_raw = plant.channel::<f64>(sim, "level", Direction::In);

    let level = plant.field_input::<f64>(PointId(10), level_raw, false);
    let demand = plant.internal_output::<i64>(PointId(20), 0);
    let duty_call = plant.internal_output::<bool>(PointId(21), false);
    let lag_call = plant.internal_output::<bool>(PointId(22), false);
    let below_cutoff = plant.internal_output::<bool>(PointId(23), false);
    let high_level = plant.internal_output::<bool>(PointId(24), false);

    let chain = plant.add(ThresholdChainSpec::new(parameters([
        ("cutoff", Value::Float(1.0)),
        ("stop", Value::Float(2.0)),
        ("start", Value::Float(4.0)),
        ("lag_start", Value::Float(6.0)),
        ("high", Value::Float(8.0)),
        ("on_bad_demand", Value::Int(0)),
    ])));
    plant.connect(level, chain.level);
    plant.connect(&chain.demand, demand);
    plant.connect(&chain.duty_call, duty_call);
    plant.connect(&chain.lag_call, lag_call);
    plant.connect(&chain.below_cutoff, below_cutoff);
    plant.connect(&chain.high_level, high_level);

    let model = build_load_assemble(plant);
    assert_eq!(model.components[0].kind, ThresholdChainSpec::KIND);
}

#[test]
fn threshold_chain_rejects_an_unordered_setpoint_table() {
    // The strictly-increasing ordering is a cross-parameter invariant
    // no spec can express: `build` accepts the map, and the kind's
    // `from_parameters` reports the offending setpoint at assembly.
    let mut plant = PlantBuilder::new();
    let level = plant.internal_input::<f64>(PointId(10), 0.0, true);
    let demand = plant.internal_output::<i64>(PointId(20), 0);
    let duty_call = plant.internal_output::<bool>(PointId(21), false);
    let lag_call = plant.internal_output::<bool>(PointId(22), false);
    let below_cutoff = plant.internal_output::<bool>(PointId(23), false);
    let high_level = plant.internal_output::<bool>(PointId(24), false);

    let chain = plant.add(ThresholdChainSpec::new(parameters([
        ("cutoff", Value::Float(1.0)),
        ("stop", Value::Float(2.0)),
        ("start", Value::Float(4.0)),
        ("lag_start", Value::Float(3.0)),
        ("high", Value::Float(8.0)),
        ("on_bad_demand", Value::Int(0)),
    ])));
    plant.connect(level, chain.level);
    plant.connect(&chain.demand, demand);
    plant.connect(&chain.duty_call, duty_call);
    plant.connect(&chain.lag_call, lag_call);
    plant.connect(&chain.below_cutoff, below_cutoff);
    plant.connect(&chain.high_level, high_level);

    let model = plant.build().unwrap();
    let driver = sim_driver(&model).unwrap();
    let error = assemble(&model, &dcs_controller::registry(), &driver).unwrap_err();
    match &error {
        AssemblyError::Component { detail, .. } => assert!(
            detail.contains("lag_start"),
            "the failure should name the offending setpoint, found {detail}"
        ),
        _ => panic!("expected a component construction failure, found {error:?}"),
    }
}

#[test]
fn failover_select_spec_emits_an_assembling_document() {
    let mut plant = PlantBuilder::new();
    let sim = plant.device("sim").id;
    let primary_raw = plant.channel::<f64>(sim, "level", Direction::In);
    let backup_raw = plant.channel::<f64>(sim, "backup", Direction::In);

    let primary = plant.field_input::<f64>(PointId(10), primary_raw, false);
    let backup = plant.field_input::<f64>(PointId(11), backup_raw, false);
    let out = plant.internal_output::<f64>(PointId(20), 0.0);
    let backup_active = plant.internal_output::<bool>(PointId(21), false);

    let select = plant.add(FailoverSelectSpec::new(Default::default()));
    plant.connect(primary, select.primary);
    plant.connect(backup, select.backup);
    plant.connect(&select.out, out);
    plant.connect(&select.backup_active, backup_active);

    let model = build_load_assemble(plant);
    assert_eq!(model.components[0].kind, FailoverSelectSpec::KIND);
}

#[test]
fn failover_select_rejects_an_undeclared_parameter() {
    // The kind declares no parameters: a stray key — a `latch` flag
    // carried over from a remembered return rule, say — is
    // `UnknownParameter` naming the key at `build`, before the
    // document exists.
    let mut plant = PlantBuilder::new();
    let sim = plant.device("sim").id;
    let primary_raw = plant.channel::<f64>(sim, "level", Direction::In);
    let backup_raw = plant.channel::<f64>(sim, "backup", Direction::In);

    let primary = plant.field_input::<f64>(PointId(10), primary_raw, false);
    let backup = plant.field_input::<f64>(PointId(11), backup_raw, false);
    let out = plant.internal_output::<f64>(PointId(20), 0.0);
    let backup_active = plant.internal_output::<bool>(PointId(21), false);

    let select = plant.add(FailoverSelectSpec::new(parameters([(
        "latch",
        Value::Bool(true),
    )])));
    plant.connect(primary, select.primary);
    plant.connect(backup, select.backup);
    plant.connect(&select.out, out);
    plant.connect(&select.backup_active, backup_active);

    assert!(matches!(
        plant.build(),
        Err(BuildError::UnknownParameter { ref parameter, .. }) if parameter == "latch"
    ));
}

/// The `flow-paced-ratio` plant both tests wire: a scripted flow and
/// trim on the device, a writable internal `dose` setpoint per decision
/// 50, and internal carriers for the three outputs.
fn flow_paced_ratio_plant(parameters_map: dcs_build::Parameters, trim: bool) -> PlantBuilder {
    let mut plant = PlantBuilder::new();
    let sim = plant.device("sim").id;
    let flow_raw = plant.channel::<f64>(sim, "flow", Direction::In);
    let trim_raw = plant.channel::<f64>(sim, "trim", Direction::In);

    let flow = plant.field_input::<f64>(PointId(10), flow_raw, false);
    let trim_in = plant.field_input::<f64>(PointId(11), trim_raw, false);
    // The operator dose setpoint — the writable internal `In` point
    // decision 50 prescribes, so writes ride the journaled receipted
    // path.
    let dose = plant.internal_input::<f64>(PointId(12), 2.0, true);
    let demand = plant.internal_output::<f64>(PointId(20), 0.0);
    let clamped = plant.internal_output::<bool>(PointId(21), false);
    let fallback_active = plant.internal_output::<bool>(PointId(22), false);

    let ratio = plant.add(FlowPacedRatioSpec::new(parameters_map, trim));
    plant.connect(flow, ratio.flow);
    plant.connect(dose, ratio.dose);
    if let Some(trim_port) = ratio.trim {
        plant.connect(trim_in, trim_port);
    }
    plant.connect(&ratio.demand, demand);
    plant.connect(&ratio.clamped, clamped);
    plant.connect(&ratio.fallback_active, fallback_active);
    plant
}

fn ratio_parameters() -> dcs_build::Parameters {
    parameters([
        ("min_dose", Value::Float(0.5)),
        ("max_dose", Value::Float(4.0)),
        ("min_rate", Value::Float(0.0)),
        ("max_rate", Value::Float(50.0)),
        ("on_bad_flow", Value::Int(0)),
        ("fallback_rate", Value::Float(12.0)),
        ("on_bad_trim", Value::Int(0)),
    ])
}

#[test]
fn flow_paced_ratio_spec_emits_an_assembling_document() {
    // Both forms assemble: `trim` bound, and the unwired port omitted —
    // the "unwired means unity" half the registry's `get("trim")`
    // serves.
    for trim in [true, false] {
        let model = build_load_assemble(flow_paced_ratio_plant(ratio_parameters(), trim));
        assert_eq!(model.components[0].kind, FlowPacedRatioSpec::KIND);
        assert_eq!(
            model.components[0].ports.contains_key("trim"),
            trim,
            "the optional port follows the spec flag"
        );
    }
}

#[test]
fn flow_paced_ratio_rejects_an_out_of_range_response_code() {
    // `on_bad_flow` declares the code range `0..=2`; a code outside it
    // is `ParameterOutOfRange` at `build`, before the document exists.
    let mut parameters_map = ratio_parameters();
    parameters_map.insert("on_bad_flow".to_string(), Value::Int(9));
    assert!(matches!(
        flow_paced_ratio_plant(parameters_map, false).build(),
        Err(BuildError::ParameterOutOfRange { ref parameter, .. }) if parameter == "on_bad_flow"
    ));
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

/// The `backwash-coordinator` plant both reorder cases wire: three
/// scripted requests and the three permissives on the device, the
/// writable internal `reorder` instruction point per decision 56, and
/// internal carriers for the grant/position/bank-state outputs.
fn backwash_plant(parameters_map: dcs_build::Parameters, reorder: bool) -> PlantBuilder {
    let mut plant = PlantBuilder::new();
    let sim = plant.device("sim").id;
    let req_1_raw = plant.channel::<bool>(sim, "request-1", Direction::In);
    let req_2_raw = plant.channel::<bool>(sim, "request-2", Direction::In);
    let req_3_raw = plant.channel::<bool>(sim, "request-3", Direction::In);
    let supply_raw = plant.channel::<bool>(sim, "supply-ok", Direction::In);
    let waste_raw = plant.channel::<bool>(sim, "waste-ok", Direction::In);
    let flow_raw = plant.channel::<bool>(sim, "flow-ok", Direction::In);

    let request_1 = plant.field_input::<bool>(PointId(10), req_1_raw, false);
    let request_2 = plant.field_input::<bool>(PointId(11), req_2_raw, false);
    let request_3 = plant.field_input::<bool>(PointId(12), req_3_raw, false);
    let supply_ok = plant.field_input::<bool>(PointId(13), supply_raw, false);
    let waste_ok = plant.field_input::<bool>(PointId(14), waste_raw, false);
    let flow_ok = plant.field_input::<bool>(PointId(15), flow_raw, false);
    // The operator reorder instruction — the writable internal `In`
    // point decision 56 prescribes, so writes ride the journaled
    // receipted path.
    let reorder_in = plant.internal_input::<i64>(PointId(16), 0, true);

    let bwc = plant.add(BackwashCoordinatorSpec::new(parameters_map, 3, reorder));
    // `grant`/`position` borrow `bwc` — bind them before the `Sink`
    // field moves below partially move the instance.
    let grants = [bwc.grant(1), bwc.grant(2), bwc.grant(3)];
    let positions = [bwc.position(1), bwc.position(2), bwc.position(3)];
    plant.connect(request_1, bwc.request(1));
    plant.connect(request_2, bwc.request(2));
    plant.connect(request_3, bwc.request(3));
    plant.connect(supply_ok, bwc.supply_ok);
    plant.connect(waste_ok, bwc.waste_ok);
    plant.connect(flow_ok, bwc.flow_ok);
    if let Some(reorder_port) = bwc.reorder {
        plant.connect(reorder_in, reorder_port);
    }
    for (index, grant) in [20u64, 21, 22].into_iter().zip(grants) {
        let carrier = plant.internal_output::<bool>(PointId(index), false);
        plant.connect(&grant, carrier);
    }
    for (index, position) in [23u64, 24, 25].into_iter().zip(positions) {
        let carrier = plant.internal_output::<i64>(PointId(index), 0);
        plant.connect(&position, carrier);
    }
    let active = plant.internal_output::<i64>(PointId(26), 0);
    plant.connect(&bwc.active, active);
    let queued = plant.internal_output::<i64>(PointId(27), 0);
    plant.connect(&bwc.queued, queued);
    let blocked = plant.internal_output::<bool>(PointId(28), false);
    plant.connect(&bwc.resource_blocked, blocked);
    plant
}

fn backwash_parameters() -> dcs_build::Parameters {
    parameters([
        ("queue_policy", Value::Int(0)),
        ("queued_state", Value::Int(0)),
    ])
}

#[test]
fn backwash_coordinator_spec_emits_an_assembling_document() {
    // Both forms assemble: `reorder` bound, and the unwired port
    // omitted — the "a plant not exposing reorder leaves the point
    // unbound" half the registry's `get("reorder")` serves.
    for reorder in [true, false] {
        let model = build_load_assemble(backwash_plant(backwash_parameters(), reorder));
        assert_eq!(model.components[0].kind, BackwashCoordinatorSpec::KIND);
        assert_eq!(
            model.components[0].ports.contains_key("reorder"),
            reorder,
            "the optional port follows the spec flag"
        );
    }
}

#[test]
fn backwash_coordinator_rejects_a_missing_declared_parameter() {
    // Both parameters are required declared data: a map missing
    // `queued_state` is `MissingParameter` naming the key at `build`,
    // before the document exists.
    let mut parameters_map = backwash_parameters();
    parameters_map.remove("queued_state");
    assert!(matches!(
        backwash_plant(parameters_map, false).build(),
        Err(BuildError::MissingParameter { ref parameter, .. }) if parameter == "queued_state"
    ));
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
        BoolLatchingAlarmSpec::KIND,
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
        ThresholdChainSpec::KIND,
        FailoverSelectSpec::KIND,
        FlowPacedRatioSpec::KIND,
        BackwashCoordinatorSpec::KIND,
    ]
    .into_iter()
    .collect();
    assert_eq!(registered, specced);
}
