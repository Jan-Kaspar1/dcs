//! Spec-versus-descriptor drift guard: every `dcs-build` kind spec must
//! declare the same interface its kind's `Component::describe` reports —
//! the same `kind` string, the same ports (name, direction, value kind)
//! in `io_requirements` order, and the same parameters (name, kind,
//! declared range) in `describe` order.
//!
//! `dcs-build` cannot depend on this crate, so the specs are data
//! mirrors kept honest here: adding a kind to `dcs-blocks` means adding
//! its `dcs-build` spec and extending the table below — the convention
//! `dcs-build`'s crate docs record.

use dcs_blocks::{
    AlarmLimits, AlarmMonitor, AnalogInput, AnalogOutput, Counter, DigitalInput, DigitalOutput,
    Interlock, Motor, OverrideSelect, Pid, PidConfig, RateLimiter, Scaling, Timer, Valve,
};
use dcs_build::Spec;
use dcs_build::specs::{
    AlarmMonitorSpec, AnalogInputSpec, AnalogOutputSpec, CounterSpec, DigitalInputSpec,
    DigitalOutputSpec, InterlockSpec, MotorSpec, OverrideSelectSpec, PidSpec, RateLimiterSpec,
    TimerSpec, ValveSpec,
};
use dcs_core::{ComponentDescriptor, PointId};
use dcs_runtime::Component;

/// Asserts `spec` declares the same interface `descriptor` reports:
/// kind string, ports in declared order, parameters in declared order.
fn check<S: Spec>(spec: &S, descriptor: &ComponentDescriptor) {
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

    // Analog kinds are type-parameterized on the raw channel's value
    // kind: both registered variants must mirror their spec.
    check(
        &AnalogInputSpec::<f64>::new(Default::default()),
        &AnalogInput::<f64>::new("ai", point(1), point(2), scaling)
            .unwrap()
            .describe(),
    );
    check(
        &AnalogInputSpec::<i64>::new(Default::default()),
        &AnalogInput::<i64>::new("ai", point(1), point(2), scaling)
            .unwrap()
            .describe(),
    );
    check(
        &AnalogOutputSpec::<f64>::new(Default::default()),
        &AnalogOutput::<f64>::new("ao", point(1), point(2), scaling)
            .unwrap()
            .describe(),
    );
    check(
        &AnalogOutputSpec::<i64>::new(Default::default()),
        &AnalogOutput::<i64>::new("ao", point(1), point(2), scaling)
            .unwrap()
            .describe(),
    );
    check(
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
    );
    check(
        &DigitalInputSpec::new(Default::default()),
        &DigitalInput::new("di", point(1), point(2)).describe(),
    );
    check(
        &DigitalOutputSpec::new(Default::default()),
        &DigitalOutput::new("do", point(1), point(2)).describe(),
    );
    check(
        &AlarmMonitorSpec::new(Default::default()),
        &AlarmMonitor::new(
            "alm",
            point(1),
            point(2),
            AlarmLimits {
                low: 10.0,
                high: 90.0,
                hysteresis: 5.0,
            },
        )
        .unwrap()
        .describe(),
    );
    check(
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
    );
    check(
        &OverrideSelectSpec::new(Default::default()),
        &OverrideSelect::new("ovr", point(1), point(2), point(3), point(4)).describe(),
    );
    check(
        &ValveSpec::new(Default::default()),
        &Valve::new("vlv", point(1), point(2), point(3), point(4), 2.0)
            .unwrap()
            .describe(),
    );
    check(
        &MotorSpec::new(Default::default()),
        &Motor::new("mtr", point(1), point(2), point(3), point(4)).describe(),
    );
    check(
        &TimerSpec::new(Default::default()),
        &Timer::new("tmr", point(1), point(2), 3).unwrap().describe(),
    );
    check(
        &CounterSpec::new(Default::default()),
        &Counter::new("ctr", point(1), point(2), point(3), point(4), 4)
            .unwrap()
            .describe(),
    );
    check(
        &RateLimiterSpec::new(Default::default()),
        &RateLimiter::new("rl", point(1), point(2), 2.5)
            .unwrap()
            .describe(),
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
