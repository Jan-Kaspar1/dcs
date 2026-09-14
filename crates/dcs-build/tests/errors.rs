//! Every named [`BuildError`] variant, produced by a corresponding
//! invalid composition — plus the dynamic path, whose unresolvable ids
//! and mismatched ends surface as `ValidationError`s at `build`.

use dcs_build::specs::{InterlockSpec, OverrideSelectSpec, PidSpec, TimerSpec};
use dcs_build::{
    BuildError, ChannelRef, DeviceId, Direction, Dynamic, DynamicSpec, PlantBuilder, PointId, Sink,
    Source, Value, ValueKind, parameters, port,
};
use dcs_model::{ComponentId, Endpoint, PortRef, ValidationError};

/// A builder with one `sim` device carrying an `In` and an `Out`
/// `Float` channel — the minimal rig for point wiring.
fn rig() -> (PlantBuilder, DeviceId) {
    let mut plant = PlantBuilder::new();
    let sim = plant.device("sim").id;
    plant.channel::<f64>(sim, "in-ch", Direction::In);
    plant.channel::<f64>(sim, "out-ch", Direction::Out);
    (plant, sim)
}

/// The minimal `pid` instance; `pid` requires `kp`, `dt`, `out_min`,
/// `out_max`.
fn pid_spec() -> PidSpec {
    PidSpec::new(parameters([
        ("kp", Value::Float(0.5)),
        ("dt", Value::Float(0.1)),
        ("out_min", Value::Float(0.0)),
        ("out_max", Value::Float(5.0)),
    ]))
}

#[test]
fn missing_required_parameter_names_the_component() {
    let mut plant = PlantBuilder::new();
    // `pid` without `kp`.
    plant.add(PidSpec::new(parameters([
        ("dt", Value::Float(0.1)),
        ("out_min", Value::Float(0.0)),
        ("out_max", Value::Float(5.0)),
    ])));
    assert_eq!(
        plant.build().unwrap_err(),
        BuildError::MissingParameter {
            component: ComponentId(1),
            parameter: "kp".to_string(),
        }
    );
}

#[test]
fn undeclared_parameter_names_the_component() {
    let mut plant = PlantBuilder::new();
    // `override-select` declares no parameters at all.
    plant.add(OverrideSelectSpec::new(parameters([(
        "gain",
        Value::Float(1.0),
    )])));
    assert_eq!(
        plant.build().unwrap_err(),
        BuildError::UnknownParameter {
            component: ComponentId(1),
            parameter: "gain".to_string(),
        }
    );
}

#[test]
fn parameter_kind_mismatch_names_expected_and_found() {
    let mut plant = PlantBuilder::new();
    // `timer`'s `delay_ticks` is a declared `Int`; a `Float` is a
    // kind mismatch, not a coercion opportunity.
    plant.add(TimerSpec::new(parameters([(
        "delay_ticks",
        Value::Float(3.0),
    )])));
    assert_eq!(
        plant.build().unwrap_err(),
        BuildError::ParameterKindMismatch {
            component: ComponentId(1),
            parameter: "delay_ticks".to_string(),
            expected: ValueKind::Int,
            found: ValueKind::Float,
        }
    );
}

#[test]
fn out_of_range_parameter_names_the_element() {
    let mut plant = PlantBuilder::new();
    // `pid`'s `dt` declares a strictly positive range.
    let mut spec = pid_spec();
    spec.parameters.insert("dt".to_string(), Value::Float(0.0));
    plant.add(spec);
    assert_eq!(
        plant.build().unwrap_err(),
        BuildError::ParameterOutOfRange {
            component: ComponentId(1),
            parameter: "dt".to_string(),
            value: Value::Float(0.0),
        }
    );
}

#[test]
fn unknown_channel_reference_fails_at_build() {
    let (mut plant, sim) = rig();
    // The channel name does not exist on the device.
    let dangling = ChannelRef {
        device: sim,
        name: "nope".to_string(),
    };
    plant.field_input::<f64>(PointId(1), dangling, false);
    match plant.build().unwrap_err() {
        BuildError::Invalid(errors) => assert_eq!(
            errors,
            [ValidationError::UnknownChannel {
                point: PointId(1),
                device: sim,
                channel: "nope".to_string(),
            }]
        ),
        other => panic!("expected validation errors, got {other:?}"),
    }
}

#[test]
fn channel_on_unknown_device_fails_at_build() {
    let (mut plant, _sim) = rig();
    plant.field_input::<f64>(
        PointId(1),
        ChannelRef {
            device: DeviceId(99),
            name: "in-ch".to_string(),
        },
        false,
    );
    match plant.build().unwrap_err() {
        BuildError::Invalid(errors) => assert_eq!(
            errors,
            [ValidationError::UnknownDevice {
                point: PointId(1),
                device: DeviceId(99),
            }]
        ),
        other => panic!("expected validation errors, got {other:?}"),
    }
}

#[test]
fn channel_direction_and_kind_mismatches_fail_at_build() {
    let (mut plant, sim) = rig();
    // `out-ch` is declared `Out`; an `In` point cannot bind it.
    let out_ch = ChannelRef {
        device: sim,
        name: "out-ch".to_string(),
    };
    plant.field_input::<f64>(PointId(1), out_ch, false);
    // `in-ch` is `Float`; an `i64` point cannot bind it.
    plant.field_input::<i64>(
        PointId(2),
        ChannelRef {
            device: sim,
            name: "in-ch".to_string(),
        },
        false,
    );
    match plant.build().unwrap_err() {
        BuildError::Invalid(errors) => {
            assert!(errors.contains(&ValidationError::ChannelDirectionMismatch {
                point: PointId(1),
                device: sim,
                channel: "out-ch".to_string(),
                point_direction: dcs_model::Direction::In,
                channel_direction: dcs_model::Direction::Out,
            }));
            assert!(errors.contains(&ValidationError::ChannelTypeMismatch {
                point: PointId(2),
                device: sim,
                channel: "in-ch".to_string(),
                point_type: ValueKind::Int,
                channel_type: ValueKind::Float,
            }));
        }
        other => panic!("expected validation errors, got {other:?}"),
    }
}

#[test]
fn dynamic_connection_wrong_kind_fails_at_build() {
    let (mut plant, sim) = rig();
    let input = plant.field_input::<f64>(
        PointId(1),
        ChannelRef {
            device: sim,
            name: "in-ch".to_string(),
        },
        false,
    );
    // A `counter` consumes `Bool` on `in`; feeding it a `Float` point
    // through the dynamic path fails at build.
    let counter = plant.add(DynamicSpec::new(
        "counter",
        vec![port("in", Direction::In, ValueKind::Bool)],
    ));
    plant.connect(input.erase(), counter.sink("in"));
    match plant.build().unwrap_err() {
        BuildError::Invalid(errors) => assert_eq!(
            errors,
            [ValidationError::ConnectionTypeMismatch {
                connection: 0,
                from: ValueKind::Float,
                to: ValueKind::Bool,
            }]
        ),
        other => panic!("expected validation errors, got {other:?}"),
    }
}

#[test]
fn dynamic_connection_wrong_direction_fails_at_build() {
    let mut plant = PlantBuilder::new();
    plant.add(pid_spec());
    // `sp` is an `In` port — it consumes; it cannot be a `from` end.
    plant.connect(
        Source::<Dynamic>::dynamic(Endpoint::Port(PortRef {
            component: ComponentId(1),
            name: "sp".to_string(),
        })),
        Sink::<Dynamic>::dynamic(Endpoint::Point(PointId(9))),
    );
    match plant.build().unwrap_err() {
        BuildError::Invalid(errors) => {
            assert!(
                errors.contains(&ValidationError::ConnectionDirectionMismatch {
                    connection: 0,
                    end: dcs_model::End::From,
                    endpoint: Endpoint::Port(PortRef {
                        component: ComponentId(1),
                        name: "sp".to_string(),
                    }),
                    direction: dcs_model::Direction::In,
                })
            );
            // The `to` end also dangles: point 9 was never declared.
            assert!(errors.contains(&ValidationError::UnknownPoint {
                connection: 0,
                end: dcs_model::End::To,
                point: PointId(9),
            }));
        }
        other => panic!("expected validation errors, got {other:?}"),
    }
}

#[test]
fn dynamic_port_name_and_component_resolve_at_build() {
    let mut plant = PlantBuilder::new();
    let pid = plant.add(pid_spec());
    // A port the instance does not declare…
    let bogus_port = Source::<Dynamic>::dynamic(Endpoint::Port(PortRef {
        component: pid.id,
        name: "nope".to_string(),
    }));
    // …and a component the model does not declare.
    let bogus_component = Source::<Dynamic>::dynamic(Endpoint::Port(PortRef {
        component: ComponentId(99),
        name: "out".to_string(),
    }));
    let sink = Sink::<Dynamic>::dynamic(Endpoint::Port(PortRef {
        component: pid.id,
        name: "pv".to_string(),
    }));
    plant.connect(bogus_port, sink.clone());
    plant.connect(bogus_component, sink);
    match plant.build().unwrap_err() {
        BuildError::Invalid(errors) => {
            assert!(errors.contains(&ValidationError::UnknownPort {
                connection: 0,
                end: dcs_model::End::From,
                component: pid.id,
                port: "nope".to_string(),
            }));
            assert!(errors.contains(&ValidationError::UnknownComponent {
                connection: 1,
                end: dcs_model::End::From,
                component: ComponentId(99),
            }));
        }
        other => panic!("expected validation errors, got {other:?}"),
    }
}

#[test]
fn interlock_trip_beyond_declared_count_fails_at_build() {
    let mut plant = PlantBuilder::new();
    let sim = plant.device("sim").id;
    let ch = plant.channel::<bool>(sim, "sig", Direction::In);
    let sig = plant.field_input::<bool>(PointId(1), ch, false);
    let interlock = plant.add(InterlockSpec::new(
        parameters([("safe_value", Value::Float(0.0))]),
        1,
    ));
    // The instance declares `trip_1` only — `trip(2)` names a port that
    // does not exist.
    plant.connect(sig, interlock.trip(2));
    match plant.build().unwrap_err() {
        BuildError::Invalid(errors) => assert_eq!(
            errors,
            [ValidationError::UnknownPort {
                connection: 0,
                end: dcs_model::End::To,
                component: interlock.id,
                port: "trip_2".to_string(),
            }]
        ),
        other => panic!("expected validation errors, got {other:?}"),
    }
}
