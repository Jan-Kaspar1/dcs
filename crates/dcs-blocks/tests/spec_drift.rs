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

use dcs_blocks::describe::{FINITE_F64, NONNEGATIVE_INT, POSITIVE_INT};
use dcs_blocks::{
    AlarmLimits, AlarmMonitor, AnalogInput, AnalogOutput, Counter, DigitalInput, DigitalOutput,
    Interlock, LatchingAlarm, ManualStation, MedianVoter, Motor, OverrideSelect, Pid, PidConfig,
    RateLimiter, Scaling, Sequencer, SequencerStep, SignalFilter, Timer, Totalizer, Valve,
};
use dcs_build::Spec;
use dcs_build::specs::{
    AlarmMonitorSpec, AnalogInputSpec, AnalogOutputSpec, CounterSpec, DigitalInputSpec,
    DigitalOutputSpec, InterlockSpec, LatchingAlarmSpec, ManualStationSpec, MedianVoterSpec,
    MotorSpec, OverrideSelectSpec, PidSpec, RateLimiterSpec, SequencerSpec, SignalFilterSpec,
    TimerSpec, TotalizerSpec, ValveSpec,
};
use dcs_core::{ComponentDescriptor, PointId, ValueKind};
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
        &LatchingAlarmSpec::new(Default::default()),
        &LatchingAlarm::new("lal", point(1), point(2), point(3), point(4), limits)
            .unwrap()
            .describe(),
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
