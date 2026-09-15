//! Spec-versus-descriptor drift guard: every `dcs-build` kind spec must
//! declare the same interface its kind's `Component::describe` reports —
//! the same `kind` string, the same ports (name, direction, value kind)
//! in `io_requirements` order, and the same parameters (name, kind,
//! declared range) in `describe` order.
//!
//! `dcs-build` cannot depend on this crate, so the specs are data
//! mirrors kept honest here. Coverage is recorded against a checked-in
//! kind list: [`dcs_blocks::KINDS`] names every kind the standard
//! registry serves — `dcs-controller`'s registry test pins
//! `registry().kinds()` to it — and the coverage assertion at the end
//! of the table fails when a registered kind lacks a spec, not only
//! when a spec drifts. Adding a kind to `dcs-blocks` therefore means
//! adding its `KIND` to the list, its `dcs-build` spec, and a `check`
//! call below — the convention `dcs-build`'s crate docs record.

use std::collections::BTreeSet;

use dcs_blocks::describe::{FINITE_F64, NONNEGATIVE_F64, NONNEGATIVE_INT, POSITIVE_INT};
use dcs_blocks::{
    AlarmLimits, AlarmMonitor, AnalogInput, AnalogOutput, BackwashCoordinator,
    BackwashCoordinatorConfig, BlowerGroup, BlowerGroupConfig, BlowerIo, BlowerOutputs,
    BlowerRotation, BoolGate, BoolLatchingAlarm, CoordinationStrategy, CoordinatorOutputs, Counter,
    DemandFallback, DemandFallbackConfig, DemandFallbackIo, DeviationMonitor, DigitalInput,
    DigitalOutput, Edge, EdgeTrigger, FailoverSelect, FeedforwardSum, FeedforwardSumConfig,
    FeedforwardSumIo, FilterIo, FlowPacedRatio, FlowPacedRatioConfig, GateOperation, GroupOutputs,
    HeaderCoordinator, HeaderCoordinatorConfig, HeaderOutputs, Interlock, LatchingAlarm,
    ManagedAlarmConfig, ManagedAlarmIo, ManagedBoolLatchingAlarm, ManagedLatchingAlarm,
    ManualStation, MedianVoter, Motor, OverrideSelect, PermissiveInputs, PhaseMode, PhaseMonitor,
    PhaseMonitorIo, Pid, PidConfig, PumpGroup, PumpGroupConfig, PumpIo, QueuePolicy, QueuedState,
    RateLimiter, RatioOutputs, Rationalization, RotationPolicy, Scaling, Sequencer, SequencerStep,
    SetpointTable, SignalFilter, SrLatch, StagingAuthority, SurgeGuard, SurgeGuardConfig,
    SurgeGuardIo, ThresholdChain, ThresholdOutputs, Timer, Totalizer, UnitBounds, Valve, ZoneIo,
};
use dcs_build::Spec;
use dcs_build::specs::{
    AlarmMonitorSpec, AnalogInputSpec, AnalogOutputSpec, BackwashCoordinatorSpec, BlowerGroupSpec,
    BoolGateSpec, BoolLatchingAlarmSpec, CounterSpec, DemandFallbackSpec, DeviationMonitorSpec,
    DigitalInputSpec, DigitalOutputSpec, EdgeTriggerSpec, FailoverSelectSpec, FeedforwardSumSpec,
    FlowPacedRatioSpec, HeaderCoordinatorSpec, InterlockSpec, LatchingAlarmSpec,
    ManagedBoolLatchingAlarmSpec, ManagedInputs, ManagedLatchingAlarmSpec, ManualStationSpec,
    MedianVoterSpec, MotorSpec, OverrideSelectSpec, PhaseMonitorSpec, PidSpec, PumpGroupSpec,
    RateLimiterSpec, SequencerSpec, SignalFilterSpec, SrLatchSpec, SurgeGuardSpec,
    ThresholdChainSpec, TimerSpec, TotalizerSpec, ValveSpec,
};
use dcs_core::{ComponentDescriptor, ParameterRange, PointId, Value, ValueKind};
use dcs_runtime::Component;

/// Asserts `spec` declares the same kind string and ports `descriptor`
/// reports — the same names, directions, and value kinds in
/// `io_requirements` order — and returns the checked kind for the
/// coverage table.
fn check_interface<S: Spec>(spec: &S, descriptor: &ComponentDescriptor) -> String {
    assert_eq!(spec.kind(), descriptor.kind, "kind string drifted");

    let spec_ports: Vec<(String, _, _)> = spec
        .ports()
        .into_iter()
        .map(|decl| (decl.name, decl.direction, decl.kind))
        .collect();
    let descriptor_ports: Vec<(String, _, _)> = descriptor
        .ports
        .iter()
        .map(|port| (port.name.clone(), port.direction, port.kind))
        .collect();
    assert_eq!(spec_ports, descriptor_ports, "port vocabulary drifted");

    spec.kind().to_string()
}

/// Asserts `spec` declares the same interface `descriptor` reports:
/// kind string, ports in declared order, parameters in declared order.
/// Returns the checked kind for the coverage table.
fn check<S: Spec>(spec: &S, descriptor: &ComponentDescriptor) -> String {
    let kind = check_interface(spec, descriptor);

    let spec_parameters: Vec<(String, _, _)> = spec
        .declared_parameters()
        .unwrap_or_default()
        .iter()
        .map(|decl| (decl.name.to_string(), decl.kind, decl.range))
        .collect();
    let descriptor_parameters: Vec<(String, _, _)> = descriptor
        .parameters
        .iter()
        .map(|parameter| (parameter.name.clone(), parameter.kind, parameter.range))
        .collect();
    assert_eq!(
        spec_parameters, descriptor_parameters,
        "parameter vocabulary drifted"
    );

    kind
}

/// A placeholder point for constructing components — the descriptor is
/// kind-level, so any ids do.
fn point(id: u64) -> PointId {
    PointId(id)
}

#[test]
fn specs_match_registered_kinds_descriptors() {
    let scaling = Scaling {
        raw_min: 4.0,
        raw_max: 20.0,
        eng_min: 0.0,
        eng_max: 100.0,
    };
    let limits = AlarmLimits {
        low: 10.0,
        high: 90.0,
        hysteresis: 5.0,
    };
    // The decision-70 pair: the codes the components carry and the
    // prose record the specs require — the parameter check compares
    // only the declared vocabulary.
    let codes = Rationalization {
        priority: 1,
        class: 2,
        response_ticks: 30,
    };
    let record = dcs_build::Rationalization {
        consequence: "c".to_string(),
        required_action: "a".to_string(),
        reference: "r".to_string(),
    };
    let mut covered = BTreeSet::new();

    // Analog kinds are type-parameterized on the raw channel's value
    // kind: both registered variants must mirror their spec.
    covered.insert(check(
        &AnalogInputSpec::<f64>::new(Default::default()),
        &AnalogInput::<f64>::new("ai", point(1), point(2), scaling)
            .unwrap()
            .describe(),
    ));
    covered.insert(check(
        &AnalogInputSpec::<i64>::new(Default::default()),
        &AnalogInput::<i64>::new("ai", point(1), point(2), scaling)
            .unwrap()
            .describe(),
    ));
    covered.insert(check(
        &AnalogOutputSpec::<f64>::new(Default::default()),
        &AnalogOutput::<f64>::new("ao", point(1), point(2), scaling)
            .unwrap()
            .describe(),
    ));
    covered.insert(check(
        &AnalogOutputSpec::<i64>::new(Default::default()),
        &AnalogOutput::<i64>::new("ao", point(1), point(2), scaling)
            .unwrap()
            .describe(),
    ));
    covered.insert(check(
        &PidSpec::new(Default::default()),
        &Pid::new(
            "pid",
            point(1),
            point(2),
            point(3),
            PidConfig {
                kp: 1.0,
                ki: 0.0,
                kd: 0.0,
                dt: 0.1,
                out_min: 0.0,
                out_max: 5.0,
            },
        )
        .unwrap()
        .describe(),
    ));
    covered.insert(check(
        &DigitalInputSpec::new(Default::default()),
        &DigitalInput::new("di", point(1), point(2)).describe(),
    ));
    covered.insert(check(
        &DigitalOutputSpec::new(Default::default()),
        &DigitalOutput::new("do", point(1), point(2)).describe(),
    ));
    covered.insert(check(
        &AlarmMonitorSpec::new(Default::default()),
        &AlarmMonitor::new("alm", point(1), point(2), limits)
            .unwrap()
            .describe(),
    ));
    covered.insert(check(
        &LatchingAlarmSpec::new(Default::default(), record.clone()),
        &LatchingAlarm::new("lal", point(1), point(2), point(3), point(4), limits, codes)
            .unwrap()
            .describe(),
    ));
    covered.insert(check(
        &BoolLatchingAlarmSpec::new(Default::default(), record.clone()),
        &BoolLatchingAlarm::new("bal", point(1), point(2), point(3), point(4), codes).describe(),
    ));
    // The managed siblings' `shelve`/`oos`/`suppress` inputs are
    // optional — declared only where bound — so each spec is checked
    // against the fully bound and the unmanaged instance.
    let managed_config = ManagedAlarmConfig {
        max_shelve_ticks: 5,
        priority: 1,
        class: 2,
        response_ticks: 30,
    };
    let managed_io = |input: usize| ManagedAlarmIo {
        input: point(input as u64),
        ack: point(2),
        shelve: Some(point(3)),
        oos: Some(point(4)),
        suppress: Some(point(5)),
        alarm: point(6),
        unacknowledged: point(7),
        shelved: point(8),
        suppressed: point(9),
        out_of_service: point(10),
    };
    let unmanaged_io = |input: usize| ManagedAlarmIo {
        shelve: None,
        oos: None,
        suppress: None,
        ..managed_io(input)
    };
    let all_managed = ManagedInputs {
        shelve: true,
        oos: true,
        suppress: true,
    };
    covered.insert(check(
        &ManagedLatchingAlarmSpec::new(Default::default(), all_managed, record.clone()),
        &ManagedLatchingAlarm::new("mlal", managed_io(1), limits, managed_config)
            .unwrap()
            .describe(),
    ));
    covered.insert(check(
        &ManagedLatchingAlarmSpec::new(
            Default::default(),
            ManagedInputs::default(),
            record.clone(),
        ),
        &ManagedLatchingAlarm::new("mlal", unmanaged_io(1), limits, managed_config)
            .unwrap()
            .describe(),
    ));
    covered.insert(check(
        &ManagedBoolLatchingAlarmSpec::new(Default::default(), all_managed, record.clone()),
        &ManagedBoolLatchingAlarm::new("mbal", managed_io(1), managed_config).describe(),
    ));
    covered.insert(check(
        &ManagedBoolLatchingAlarmSpec::new(
            Default::default(),
            ManagedInputs::default(),
            record.clone(),
        ),
        &ManagedBoolLatchingAlarm::new("mbal", unmanaged_io(1), managed_config).describe(),
    ));
    covered.insert(check(
        &InterlockSpec::new(Default::default(), 2),
        &Interlock::new(
            "ilk",
            point(1),
            point(2),
            vec![point(3), point(4)],
            point(5),
            point(6),
            0.0,
        )
        .unwrap()
        .describe(),
    ));
    covered.insert(check(
        &OverrideSelectSpec::new(Default::default()),
        &OverrideSelect::new("ovr", point(1), point(2), point(3), point(4)).describe(),
    ));
    covered.insert(check(
        &ValveSpec::new(Default::default()),
        &Valve::new("vlv", point(1), point(2), point(3), point(4), 2.0)
            .unwrap()
            .describe(),
    ));
    covered.insert(check(
        &MotorSpec::new(Default::default()),
        &Motor::new("mtr", point(1), point(2), point(3), point(4)).describe(),
    ));
    covered.insert(check(
        &TimerSpec::new(Default::default()),
        &Timer::new("tmr", point(1), point(2), 3).unwrap().describe(),
    ));
    covered.insert(check(
        &CounterSpec::new(Default::default()),
        &Counter::new("ctr", point(1), point(2), point(3), point(4), 4)
            .unwrap()
            .describe(),
    ));
    covered.insert(check(
        &RateLimiterSpec::new(Default::default()),
        &RateLimiter::new("rl", point(1), point(2), 2.5)
            .unwrap()
            .describe(),
    ));
    covered.insert(check(
        &ManualStationSpec::new(Default::default()),
        &ManualStation::new("mas", point(1), point(2), point(3), point(4), point(5), 5.0)
            .unwrap()
            .describe(),
    ));
    covered.insert(check(
        &SignalFilterSpec::new(Default::default()),
        &SignalFilter::new("flt", point(1), point(2), 0.5)
            .unwrap()
            .describe(),
    ));
    covered.insert(check(
        &MedianVoterSpec::new(Default::default()),
        &MedianVoter::new("vot", point(1), point(2), point(3), point(4), point(5), 2.0)
            .unwrap()
            .describe(),
    ));
    covered.insert(check(
        &TotalizerSpec::new(Default::default()),
        &Totalizer::new("tot", point(1), point(2), point(3), 1.0, 0.0)
            .unwrap()
            .describe(),
    ));

    // `sequencer`'s parameter set is indexed by `step_count` — a
    // different key set per instance, so the spec's recorded treatment
    // is `declared_parameters() -> None` and `build` leaves the map
    // unchecked. The kind and ports still mirror `describe()`; the
    // descriptor's step-table parameters are pinned here, so a
    // `describe` drift — or a spec silently growing a static prefix —
    // still fails.
    let sequencer = Sequencer::new(
        "seq",
        point(1),
        point(2),
        point(3),
        point(4),
        point(5),
        vec![
            SequencerStep {
                ticks: 2,
                value: 10.0,
            },
            SequencerStep {
                ticks: 1,
                value: 20.0,
            },
        ],
    )
    .unwrap();
    let sequencer_spec = SequencerSpec::new(Default::default());
    covered.insert(check_interface(&sequencer_spec, &sequencer.describe()));
    assert!(
        sequencer_spec.declared_parameters().is_none(),
        "sequencer's parameter set is not statically enumerable"
    );
    let step_table: Vec<(String, _, _)> = sequencer
        .describe()
        .parameters
        .iter()
        .map(|parameter| (parameter.name.clone(), parameter.kind, parameter.range))
        .collect();
    assert_eq!(
        step_table,
        vec![
            ("step_count".to_string(), ValueKind::Int, Some(POSITIVE_INT)),
            (
                "step_1_ticks".to_string(),
                ValueKind::Int,
                Some(NONNEGATIVE_INT)
            ),
            ("step_1_out".to_string(), ValueKind::Float, Some(FINITE_F64)),
            (
                "step_2_ticks".to_string(),
                ValueKind::Int,
                Some(NONNEGATIVE_INT)
            ),
            ("step_2_out".to_string(), ValueKind::Float, Some(FINITE_F64)),
        ],
        "sequencer's indexed parameter vocabulary drifted"
    );

    // `bool-gate`'s `in_N` set is instance-dependent — `N` is the
    // spec's `inputs`, matching the interlock's `trips` convention.
    covered.insert(check(
        &BoolGateSpec::new(Default::default(), 3),
        &BoolGate::new(
            "gate",
            vec![point(1), point(2), point(3)],
            point(4),
            GateOperation::Or,
        )
        .describe(),
    ));
    // `pump-group`'s per-pump `cmd_i`/`run_i`/`fault_i`/`avail_i`
    // families are instance-dependent — `N` is the spec's `pumps`,
    // matching the interlock's `trips` convention.
    covered.insert(check(
        &PumpGroupSpec::new(Default::default(), 2),
        &PumpGroup::new(
            "pg",
            point(1),
            vec![
                PumpIo {
                    cmd: point(2),
                    run: point(3),
                    fault: point(4),
                    avail: point(5),
                },
                PumpIo {
                    cmd: point(6),
                    run: point(7),
                    fault: point(8),
                    avail: point(9),
                },
            ],
            GroupOutputs {
                duty: point(10),
                staged: point(11),
                none_available: point(12),
                all_faulted: point(13),
            },
            PumpGroupConfig {
                rotation: RotationPolicy::AlternateEachCycle,
                rotation_ticks: 0,
                start_delay_ticks: 0,
                restage_delay_ticks: 0,
                min_off_ticks: 0,
            },
        )
        .unwrap()
        .describe(),
    ));
    // `blower-group`'s per-blower `cmd_i`/`run_i`/`fault_i`/`avail_i`/
    // `capacity_i`/`vent_i` families are instance-dependent — `N` is
    // the spec's `blowers`, matching the pump-group convention — and
    // `approve` is the optional port, declared only where bound. Its
    // parameter set is indexed by the unit count — a different key set
    // per instance — so, like `sequencer`, the spec's recorded
    // treatment is `declared_parameters() -> None` and `build` leaves
    // the map to `from_parameters`; the descriptor's parameter
    // vocabulary is pinned here.
    let blower_io = |base: u64| BlowerIo {
        cmd: point(base),
        run: point(base + 1),
        fault: point(base + 2),
        avail: point(base + 3),
        capacity: point(base + 4),
        vent: point(base + 5),
    };
    let blower_bounds = UnitBounds {
        min_flow: 20.0,
        max_flow: 100.0,
        max_current: 90.0,
    };
    let blower_outputs = |base: u64| BlowerOutputs {
        staged: point(base),
        none_available: point(base + 1),
        all_faulted: point(base + 2),
        staging_pending: point(base + 3),
        transition: point(base + 4),
    };
    let blower_config = BlowerGroupConfig {
        staging_authority: StagingAuthority::Automatic,
        stage_up: 0.9,
        stage_down: 0.8,
        min_run_ticks: 0,
        min_start_interval_ticks: 0,
        vent_ticks: 2,
        rotation: BlowerRotation::NoRotation,
    };
    let blower_group = |approve: Option<PointId>| {
        BlowerGroup::new(
            "bg",
            point(1),
            approve,
            vec![blower_io(10), blower_io(20)],
            vec![blower_bounds; 2],
            blower_outputs(30),
            blower_config,
        )
        .unwrap()
    };
    covered.insert(check_interface(
        &BlowerGroupSpec::new(Default::default(), 2, true),
        &blower_group(Some(point(2))).describe(),
    ));
    check_interface(
        &BlowerGroupSpec::new(Default::default(), 2, false),
        &blower_group(None).describe(),
    );
    assert!(
        BlowerGroupSpec::new(Default::default(), 2, true)
            .declared_parameters()
            .is_none(),
        "blower-group's parameter set is not statically enumerable"
    );
    let blower_parameters: Vec<(String, _, _)> = blower_group(None)
        .describe()
        .parameters
        .iter()
        .map(|parameter| (parameter.name.clone(), parameter.kind, parameter.range))
        .collect();
    assert_eq!(
        blower_parameters,
        vec![
            (
                "staging_authority".to_string(),
                ValueKind::Int,
                Some(ParameterRange {
                    min: Value::Int(0),
                    max: Value::Int(2),
                })
            ),
            (
                "stage_up".to_string(),
                ValueKind::Float,
                Some(NONNEGATIVE_F64)
            ),
            (
                "stage_down".to_string(),
                ValueKind::Float,
                Some(NONNEGATIVE_F64)
            ),
            (
                "min_run_ticks".to_string(),
                ValueKind::Int,
                Some(NONNEGATIVE_INT)
            ),
            (
                "min_start_interval_ticks".to_string(),
                ValueKind::Int,
                Some(NONNEGATIVE_INT)
            ),
            (
                "unit_1_min_flow".to_string(),
                ValueKind::Float,
                Some(NONNEGATIVE_F64)
            ),
            (
                "unit_1_max_flow".to_string(),
                ValueKind::Float,
                Some(NONNEGATIVE_F64)
            ),
            (
                "unit_1_max_current".to_string(),
                ValueKind::Float,
                Some(NONNEGATIVE_F64)
            ),
            (
                "unit_2_min_flow".to_string(),
                ValueKind::Float,
                Some(NONNEGATIVE_F64)
            ),
            (
                "unit_2_max_flow".to_string(),
                ValueKind::Float,
                Some(NONNEGATIVE_F64)
            ),
            (
                "unit_2_max_current".to_string(),
                ValueKind::Float,
                Some(NONNEGATIVE_F64)
            ),
            ("vent_ticks".to_string(), ValueKind::Int, Some(POSITIVE_INT)),
            (
                "rotation".to_string(),
                ValueKind::Int,
                Some(ParameterRange {
                    min: Value::Int(0),
                    max: Value::Int(2),
                })
            ),
        ],
        "blower-group's indexed parameter vocabulary drifted"
    );

    covered.insert(check(
        &SrLatchSpec::new(Default::default()),
        &SrLatch::new("srl", point(1), point(2), point(3)).describe(),
    ));
    covered.insert(check(
        &EdgeTriggerSpec::new(Default::default()),
        &EdgeTrigger::new("etr", point(1), point(2), Edge::Rising).describe(),
    ));
    covered.insert(check(
        &ThresholdChainSpec::new(Default::default()),
        &ThresholdChain::new(
            "lch",
            point(1),
            ThresholdOutputs {
                demand: point(2),
                duty_call: point(3),
                lag_call: point(4),
                below_cutoff: point(5),
                high_level: point(6),
            },
            SetpointTable {
                cutoff: 1.0,
                stop: 2.0,
                start: 4.0,
                lag_start: 6.0,
                high: 8.0,
                on_bad_demand: 0,
            },
        )
        .unwrap()
        .describe(),
    ));
    covered.insert(check(
        &FailoverSelectSpec::new(Default::default()),
        &FailoverSelect::new("fsel", point(1), point(2), point(3), point(4)).describe(),
    ));
    // `flow-paced-ratio`'s `trim` is the optional port — declared only
    // where bound, so the spec is checked against both instances.
    let fpr_config = FlowPacedRatioConfig {
        min_dose: 0.5,
        max_dose: 4.0,
        min_rate: 0.0,
        max_rate: 50.0,
        on_bad_flow: 0,
        fallback_rate: 12.0,
        on_bad_trim: 0,
    };
    let fpr_outputs = || RatioOutputs {
        demand: point(4),
        clamped: point(5),
        fallback_active: point(6),
    };
    covered.insert(check(
        &FlowPacedRatioSpec::new(Default::default(), true),
        &FlowPacedRatio::new(
            "fpr",
            point(1),
            point(2),
            Some(point(3)),
            fpr_outputs(),
            fpr_config,
        )
        .unwrap()
        .describe(),
    ));
    covered.insert(check(
        &FlowPacedRatioSpec::new(Default::default(), false),
        &FlowPacedRatio::new("fpr", point(1), point(2), None, fpr_outputs(), fpr_config)
            .unwrap()
            .describe(),
    ));
    covered.insert(check(
        &DeviationMonitorSpec::new(Default::default()),
        &DeviationMonitor::new("dev", point(1), point(2), point(3), point(4), 0.1, 4)
            .unwrap()
            .describe(),
    ));
    covered.insert(check(
        &PhaseMonitorSpec::new(Default::default()),
        &PhaseMonitor::new(
            "phm",
            PhaseMonitorIo {
                input: point(1),
                phase: point(2),
                capture: point(3),
                deviation: point(4),
                exceeded: point(5),
                overdue: point(6),
            },
            0.5,
            4,
            PhaseMode::Absolute,
        )
        .unwrap()
        .describe(),
    ));
    // `surge-guard`'s `current` is the optional port — declared only
    // where bound, so the spec is checked against both instances.
    let guard_config = SurgeGuardConfig {
        min_flow: 50.0,
        max_pressure: 30.0,
        min_current: 40.0,
        on_guard: dcs_blocks::GuardResponse::Clamp,
        trip_value: 0.0,
    };
    let guard_io = |current: Option<PointId>| SurgeGuardIo {
        demand: point(1),
        flow: point(2),
        pressure: point(3),
        current,
        surge_trip: point(4),
        out: point(5),
        guarding: point(6),
        tripped: point(7),
    };
    covered.insert(check(
        &SurgeGuardSpec::new(Default::default(), true),
        &SurgeGuard::new("sg", guard_io(Some(point(8))), guard_config)
            .unwrap()
            .describe(),
    ));
    covered.insert(check(
        &SurgeGuardSpec::new(Default::default(), false),
        &SurgeGuard::new("sg", guard_io(None), guard_config)
            .unwrap()
            .describe(),
    ));
    covered.insert(check(
        &DemandFallbackSpec::new(Default::default()),
        &DemandFallback::new(
            "dfb",
            DemandFallbackIo {
                input: point(1),
                pv: point(2),
                out: point(3),
                fallback_active: point(4),
            },
            DemandFallbackConfig {
                on_bad: dcs_blocks::FallbackResponse::Hold,
                fallback_flow: 25.0,
                safe_flow: 5.0,
            },
        )
        .unwrap()
        .describe(),
    ));
    covered.insert(check(
        &FeedforwardSumSpec::new(Default::default()),
        &FeedforwardSum::new(
            "ffs",
            FeedforwardSumIo {
                ff: point(1),
                trim: point(2),
                out: point(3),
                clamped: point(4),
                fallback_active: point(5),
            },
            FeedforwardSumConfig {
                trim_min: -10.0,
                trim_max: 10.0,
                min_demand: 0.0,
                max_demand: 100.0,
                on_bad_ff: dcs_blocks::BadTermResponse::Drop,
                on_bad_trim: dcs_blocks::BadTermResponse::Drop,
            },
        )
        .unwrap()
        .describe(),
    ));
    // `backwash-coordinator`'s per-filter `request_i`/`grant_i`/
    // `position_i` families are instance-dependent — `N` is the spec's
    // `filters`, matching the interlock's `trips` convention — and
    // `reorder` is the optional port, declared only where bound.
    let coordinator = |reorder: Option<PointId>| {
        BackwashCoordinator::new(
            "bwc",
            PermissiveInputs {
                supply_ok: point(1),
                waste_ok: point(2),
                flow_ok: point(3),
            },
            reorder,
            vec![
                FilterIo {
                    request: point(10),
                    grant: point(11),
                    position: point(12),
                },
                FilterIo {
                    request: point(20),
                    grant: point(21),
                    position: point(22),
                },
            ],
            CoordinatorOutputs {
                active: point(30),
                queued: point(31),
                resource_blocked: point(32),
            },
            BackwashCoordinatorConfig {
                queue_policy: QueuePolicy::Fifo,
                queued_state: QueuedState::KeepFiltering,
            },
        )
        .unwrap()
    };
    covered.insert(check(
        &BackwashCoordinatorSpec::new(Default::default(), 2, true),
        &coordinator(Some(point(4))).describe(),
    ));
    covered.insert(check(
        &BackwashCoordinatorSpec::new(Default::default(), 2, false),
        &coordinator(None).describe(),
    ));
    // `header-coordinator`'s per-zone `valve_pos_i`/`airflow_i`/
    // `pulsing_i`/`pulse_grant_i` families are instance-dependent —
    // `N` is the spec's `zones`, matching the interlock's `trips`
    // convention.
    covered.insert(check(
        &HeaderCoordinatorSpec::new(Default::default(), 2),
        &HeaderCoordinator::new(
            "hdr",
            point(1),
            vec![
                ZoneIo {
                    valve_pos: point(10),
                    airflow: point(11),
                    pulsing: point(12),
                    pulse_grant: point(13),
                },
                ZoneIo {
                    valve_pos: point(20),
                    airflow: point(21),
                    pulsing: point(22),
                    pulse_grant: point(23),
                },
            ],
            HeaderOutputs {
                pressure_sp: point(30),
                blower_demand: point(31),
                most_open: point(32),
                at_bound: point(33),
                pulse_blocked: point(34),
            },
            HeaderCoordinatorConfig {
                strategy: CoordinationStrategy::ConstantPressure,
                pressure_hold: 10.0,
                pressure_min: 4.0,
                pressure_max: 16.0,
                mov_band_lo: 85.0,
                mov_band_hi: 95.0,
                adjust_ticks: 3,
                min_total_airflow: 1.0,
                max_pulsing: 1,
            },
        )
        .unwrap()
        .describe(),
    ));

    // The coverage guard: the table must pin exactly the kinds the
    // standard registry serves — the checked-in `dcs_blocks::KINDS`
    // list the `dcs-controller` registry test keeps in step. A kind
    // registered without a spec leaves a gap in `covered`; a spec for
    // a kind no longer registered leaves an extra — both fail here.
    let registered: BTreeSet<String> = dcs_blocks::KINDS
        .iter()
        .map(|kind| kind.to_string())
        .collect();
    assert_eq!(
        covered, registered,
        "the spec table must cover every registered kind"
    );
}

#[test]
fn interlock_spec_tracks_trip_count() {
    // The `trip_N` set is instance-dependent: the spec's port list must
    // follow the constructed component's.
    for trips in [0usize, 1, 5] {
        let component = Interlock::new(
            "ilk",
            point(1),
            point(2),
            (0..trips as u64).map(|n| point(10 + n)).collect(),
            point(3),
            point(4),
            0.0,
        )
        .unwrap();
        check(
            &InterlockSpec::new(Default::default(), trips),
            &component.describe(),
        );
    }
}

#[test]
fn pump_group_spec_tracks_pump_count() {
    // The `cmd_i`/`run_i`/`fault_i`/`avail_i` families are
    // instance-dependent: the spec's port list must follow the
    // constructed component's.
    let config = PumpGroupConfig {
        rotation: RotationPolicy::AlternateEachCycle,
        rotation_ticks: 0,
        start_delay_ticks: 0,
        restage_delay_ticks: 0,
        min_off_ticks: 0,
    };
    for pumps in [1usize, 2, 5] {
        let pump_io: Vec<PumpIo> = (0..pumps as u64)
            .map(|n| PumpIo {
                cmd: point(10 + n * 4),
                run: point(11 + n * 4),
                fault: point(12 + n * 4),
                avail: point(13 + n * 4),
            })
            .collect();
        let component = PumpGroup::new(
            "pg",
            point(1),
            pump_io,
            GroupOutputs {
                duty: point(2),
                staged: point(3),
                none_available: point(4),
                all_faulted: point(5),
            },
            config,
        )
        .unwrap();
        check(
            &PumpGroupSpec::new(Default::default(), pumps),
            &component.describe(),
        );
    }
}

#[test]
fn backwash_coordinator_spec_tracks_filter_count() {
    // The `request_i`/`grant_i`/`position_i` families are
    // instance-dependent: the spec's port list must follow the
    // constructed component's — with and without the optional
    // `reorder` port.
    let config = BackwashCoordinatorConfig {
        queue_policy: QueuePolicy::Fifo,
        queued_state: QueuedState::KeepFiltering,
    };
    for filters in [1usize, 2, 5] {
        for reorder in [None, Some(point(9))] {
            let filter_io: Vec<FilterIo> = (0..filters as u64)
                .map(|n| FilterIo {
                    request: point(10 + n * 3),
                    grant: point(11 + n * 3),
                    position: point(12 + n * 3),
                })
                .collect();
            let component = BackwashCoordinator::new(
                "bwc",
                PermissiveInputs {
                    supply_ok: point(1),
                    waste_ok: point(2),
                    flow_ok: point(3),
                },
                reorder,
                filter_io,
                CoordinatorOutputs {
                    active: point(4),
                    queued: point(5),
                    resource_blocked: point(6),
                },
                config,
            )
            .unwrap();
            check(
                &BackwashCoordinatorSpec::new(Default::default(), filters, reorder.is_some()),
                &component.describe(),
            );
        }
    }
}

#[test]
fn header_coordinator_spec_tracks_zone_count() {
    // The `valve_pos_i`/`airflow_i`/`pulsing_i`/`pulse_grant_i`
    // families are instance-dependent: the spec's port list must
    // follow the constructed component's.
    let config = HeaderCoordinatorConfig {
        strategy: CoordinationStrategy::ConstantPressure,
        pressure_hold: 10.0,
        pressure_min: 4.0,
        pressure_max: 16.0,
        mov_band_lo: 85.0,
        mov_band_hi: 95.0,
        adjust_ticks: 3,
        min_total_airflow: 1.0,
        max_pulsing: 1,
    };
    for zones in [1usize, 2, 5] {
        let zone_io: Vec<ZoneIo> = (0..zones as u64)
            .map(|n| ZoneIo {
                valve_pos: point(10 + n * 4),
                airflow: point(11 + n * 4),
                pulsing: point(12 + n * 4),
                pulse_grant: point(13 + n * 4),
            })
            .collect();
        let component = HeaderCoordinator::new(
            "hdr",
            point(1),
            zone_io,
            HeaderOutputs {
                pressure_sp: point(2),
                blower_demand: point(3),
                most_open: point(4),
                at_bound: point(5),
                pulse_blocked: point(6),
            },
            config,
        )
        .unwrap();
        check(
            &HeaderCoordinatorSpec::new(Default::default(), zones),
            &component.describe(),
        );
    }
}

#[test]
fn blower_group_spec_tracks_blower_count() {
    // The `cmd_i`/`run_i`/`fault_i`/`avail_i`/`capacity_i`/`vent_i`
    // families are instance-dependent: the spec's port list must
    // follow the constructed component's — with and without the
    // optional `approve` port.
    let config = BlowerGroupConfig {
        staging_authority: StagingAuthority::Automatic,
        stage_up: 0.9,
        stage_down: 0.8,
        min_run_ticks: 0,
        min_start_interval_ticks: 0,
        vent_ticks: 2,
        rotation: BlowerRotation::NoRotation,
    };
    let bounds = UnitBounds {
        min_flow: 20.0,
        max_flow: 100.0,
        max_current: 90.0,
    };
    for blowers in [1usize, 2, 5] {
        for approve in [None, Some(point(9))] {
            let blower_io: Vec<BlowerIo> = (0..blowers as u64)
                .map(|n| BlowerIo {
                    cmd: point(10 + n * 6),
                    run: point(11 + n * 6),
                    fault: point(12 + n * 6),
                    avail: point(13 + n * 6),
                    capacity: point(14 + n * 6),
                    vent: point(15 + n * 6),
                })
                .collect();
            let component = BlowerGroup::new(
                "bg",
                point(1),
                approve,
                blower_io,
                vec![bounds; blowers],
                BlowerOutputs {
                    staged: point(2),
                    none_available: point(3),
                    all_faulted: point(4),
                    staging_pending: point(5),
                    transition: point(6),
                },
                config,
            )
            .unwrap();
            check_interface(
                &BlowerGroupSpec::new(Default::default(), blowers, approve.is_some()),
                &component.describe(),
            );
        }
    }
}

#[test]
fn bool_gate_spec_tracks_input_count() {
    // The `in_N` set is instance-dependent, like the interlock's
    // `trip_N` set: the spec's port list must follow the constructed
    // component's — including the empty gate, whose descriptor declares
    // `out` alone.
    for inputs in [0usize, 1, 5] {
        let component = BoolGate::new(
            "gate",
            (0..inputs as u64).map(|n| point(10 + n)).collect(),
            point(1),
            GateOperation::Or,
        );
        check(
            &BoolGateSpec::new(Default::default(), inputs),
            &component.describe(),
        );
    }
}
