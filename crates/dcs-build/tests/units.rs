//! The declared-unit discipline at the typed composition seam.
//!
//! `ValueKind` is a representation, not a dimension: a `m3/h` flow and
//! a `mg/L` dose bound are both `Float`, and a compile-time `f64`
//! connection check cannot tell them apart. The dimensional contract
//! is declared data — `IoPoint::unit` through
//! [`PlantBuilder::unit`], a unit-transparent port or parameter through
//! [`PlantBuilder::port_unit`]/[`PlantBuilder::param_unit`], a kind's
//! inherent unit through [`PortDecl::with_unit`]/[`ParamDecl::with_unit`]
//! — and two wired ends declaring disagreeing units fail `connect`
//! where both declarations are known and `build`/`load` where they are
//! not, as the named [`ValidationError::ConnectionUnitMismatch`].

use dcs_build::specs::{InterlockSpec, LatchingAlarmSpec, TimerSpec};
use dcs_build::{
    BuildError, ChannelRef, DeviceId, Direction, DynamicSpec, PlantBuilder, PointId, SignalId,
    Value, ValueKind, parameters, port, unit,
};
use dcs_model::{
    ComponentId, Endpoint, LoadError, PlantModel, PortRef, Rationalization, ValidationError,
};

/// A builder with one `sim` device carrying an `In` and an `Out`
/// `Float` channel — the minimal rig for point wiring.
fn rig() -> (PlantBuilder, DeviceId) {
    let mut plant = PlantBuilder::new();
    let sim = plant.device("sim").id;
    plant.channel::<f64>(sim, "in-ch", Direction::In);
    plant.channel::<f64>(sim, "out-ch", Direction::Out);
    (plant, sim)
}

/// The `in`/`out` channel references matching `rig`.
fn channel_in(sim: DeviceId) -> ChannelRef {
    ChannelRef {
        device: sim,
        name: "in-ch".to_string(),
    }
}

fn channel_out(sim: DeviceId) -> ChannelRef {
    ChannelRef {
        device: sim,
        name: "out-ch".to_string(),
    }
}

/// The minimal `interlock` instance — its `in`/`out` ports are
/// unit-transparent, so the composition declares their dimension.
fn interlock() -> InterlockSpec {
    InterlockSpec::new(parameters([("safe_value", Value::Float(0.0))]), 0)
}

#[test]
fn declared_units_agree_and_land_in_the_document() {
    let (mut plant, sim) = rig();
    let flow = plant.field_input::<f64>(PointId(1), channel_in(sim), false);
    let gated = plant.field_output::<f64>(PointId(2), channel_out(sim));
    let interlock = plant.add(interlock());
    plant.unit(flow, unit::M3_PER_H);
    plant.unit(gated, unit::M3_PER_H);
    plant.port_unit(interlock.id, "in", unit::M3_PER_H);
    plant.port_unit(interlock.id, "out", unit::M3_PER_H);
    plant.param_unit(interlock.id, "safe_value", unit::M3_PER_H);
    plant.connect(flow, interlock.input);
    plant.connect(interlock.out, gated);
    let model = plant.build().unwrap();
    assert_eq!(model.io_points[0].unit.as_deref(), Some("m3/h"));
    assert_eq!(model.io_points[1].unit.as_deref(), Some("m3/h"));
    let ports = &model.components[0].ports;
    assert_eq!(ports["in"].unit.as_deref(), Some("m3/h"));
    assert_eq!(ports["out"].unit.as_deref(), Some("m3/h"));
    assert_eq!(model.components[0].parameter_units["safe_value"], "m3/h");
}

#[test]
fn mismatched_declared_units_fail_at_build_by_name() {
    let (mut plant, sim) = rig();
    let flow = plant.field_input::<f64>(PointId(1), channel_in(sim), false);
    let interlock = plant.add(interlock());
    // `connect` runs before the second declaration lands, so the
    // disagreement reaches `build` as the named model error.
    plant.connect(flow, interlock.input);
    plant.unit(flow, unit::M3_PER_H);
    plant.port_unit(interlock.id, "in", unit::G_PER_H);
    match plant.build().unwrap_err() {
        BuildError::Invalid(errors) => assert_eq!(
            errors,
            [ValidationError::ConnectionUnitMismatch {
                connection: 0,
                from: Endpoint::Point(PointId(1)),
                from_unit: "m3/h".to_string(),
                to: Endpoint::Port(PortRef {
                    component: ComponentId(1),
                    name: "in".to_string(),
                }),
                to_unit: "g/h".to_string(),
            }]
        ),
        other => panic!("expected Invalid, got {other:?}"),
    }
}

#[test]
#[should_panic(expected = "wired ends must declare equal units")]
fn connect_panics_where_both_declarations_are_known() {
    let (mut plant, sim) = rig();
    let flow = plant.field_input::<f64>(PointId(1), channel_in(sim), false);
    let interlock = plant.add(interlock());
    plant.unit(flow, unit::M3_PER_H);
    plant.port_unit(interlock.id, "in", unit::G_PER_H);
    plant.connect(flow, interlock.input);
}

#[test]
fn a_hand_written_document_fails_load_by_name() {
    // The same check a `Dynamic` end faces: the document itself carries
    // the declarations, so `load` reports the disagreement without the
    // builder — a hand-authored or data-driven composition cannot
    // bypass the seam.
    let document = r#"{
        "version": 1,
        "devices": [],
        "io_points": [
            {
                "id": 1,
                "direction": "in",
                "value_type": "float",
                "initial": { "float": 0.0 },
                "unit": "m3/h"
            }
        ],
        "signals": [],
        "components": [
            {
                "id": 1,
                "kind": "interlock",
                "parameters": { "safe_value": { "float": 0.0 } },
                "ports": {
                    "in": {
                        "direction": "in",
                        "value_type": "float",
                        "unit": "g/h"
                    }
                }
            }
        ],
        "connections": [
            { "from": { "point": 1 }, "to": { "port": { "component": 1, "name": "in" } } }
        ]
    }"#;
    match PlantModel::load(document).unwrap_err() {
        LoadError::Invalid(errors) => assert_eq!(
            errors,
            [ValidationError::ConnectionUnitMismatch {
                connection: 0,
                from: Endpoint::Point(PointId(1)),
                from_unit: "m3/h".to_string(),
                to: Endpoint::Port(PortRef {
                    component: ComponentId(1),
                    name: "in".to_string(),
                }),
                to_unit: "g/h".to_string(),
            }]
        ),
        other => panic!("expected Invalid, got {other:?}"),
    }
}

#[test]
fn a_unit_transparent_end_stays_uncheckable() {
    // The gradual-adoption half of the discipline: a port declaring no
    // unit — the unit-transparent kind the composition did not
    // dimension — connects freely to a declared point.
    let (mut plant, sim) = rig();
    let flow = plant.field_input::<f64>(PointId(1), channel_in(sim), false);
    let interlock = plant.add(interlock());
    plant.unit(flow, unit::M3_PER_H);
    plant.connect(flow, interlock.input);
    assert!(plant.build().is_ok());
}

#[test]
fn a_declared_dimensionless_end_rejects_a_dimensioned_one() {
    // `""` is a checked declaration — "this value has no unit" — not an
    // absent one: a dimensionless flag into a `g/h` port disagrees.
    // `connect` runs before the declarations, so the disagreement lands
    // as the named model error at `build`.
    let (mut plant, sim) = rig();
    let flow = plant.field_input::<f64>(PointId(1), channel_in(sim), false);
    let interlock = plant.add(interlock());
    plant.connect(flow, interlock.input);
    plant.unit(flow, unit::DIMENSIONLESS);
    plant.port_unit(interlock.id, "in", unit::G_PER_H);
    match plant.build().unwrap_err() {
        BuildError::Invalid(errors) => assert_eq!(
            errors,
            [ValidationError::ConnectionUnitMismatch {
                connection: 0,
                from: Endpoint::Point(PointId(1)),
                from_unit: String::new(),
                to: Endpoint::Port(PortRef {
                    component: ComponentId(1),
                    name: "in".to_string(),
                }),
                to_unit: "g/h".to_string(),
            }]
        ),
        other => panic!("expected Invalid, got {other:?}"),
    }
}

#[test]
#[should_panic(expected = "wired ends must declare equal units")]
fn a_dynamic_end_faces_the_same_check() {
    let (mut plant, sim) = rig();
    let flow = plant.field_input::<f64>(PointId(1), channel_in(sim), false);
    let dynamic = plant.add(DynamicSpec::new(
        "interlock",
        vec![port("in", Direction::In, ValueKind::Float).with_unit("g/h")],
    ));
    plant.unit(flow, unit::M3_PER_H);
    // The erased end's dynamic port already carries its spec-declared
    // `g/h` in the builder's document, so `connect` resolves both
    // declarations and reports the disagreement immediately — a
    // dynamic end cannot smuggle a unit mismatch past the seam.
    plant.connect(flow.erase(), dynamic.sink("in"));
}

#[test]
fn a_spec_declared_port_unit_is_fixed_by_the_kind() {
    // The spec-declared unit stays spec-level: it is not emitted into
    // the document (an unchanged composition emits identical bytes
    // across a compatible crossing), so the port records no unit until
    // the composition declares one.
    let mut plant = PlantBuilder::new();
    plant.add(DynamicSpec::new(
        "flow-source",
        vec![port("out", Direction::Out, ValueKind::Float).with_unit("m3/h")],
    ));
    assert_eq!(plant.build().unwrap().components[0].ports["out"].unit, None);
    // The composition's agreeing declaration is recorded; a
    // disagreeing one is a programming error.
    let mut plant = PlantBuilder::new();
    let dynamic = plant.add(DynamicSpec::new(
        "flow-source",
        vec![port("out", Direction::Out, ValueKind::Float).with_unit("m3/h")],
    ));
    plant.port_unit(dynamic.id(), "out", unit::M3_PER_H);
    assert_eq!(
        plant.build().unwrap().components[0].ports["out"]
            .unit
            .as_deref(),
        Some("m3/h")
    );
    let mut plant = PlantBuilder::new();
    let dynamic = plant.add(DynamicSpec::new(
        "flow-source",
        vec![port("out", Direction::Out, ValueKind::Float).with_unit("m3/h")],
    ));
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        plant.port_unit(dynamic.id(), "out", unit::G_PER_H);
    }));
    assert!(result.is_err());
}

#[test]
fn spec_declared_parameter_units_constrain_the_declaration() {
    // The kind's inherent dimension — `*_ticks` intervals are the
    // scan-counted `ticks` — is spec-level data: `param_unit` must
    // agree with it, and the emitted `parameter_units` records the
    // composition's declaration.
    let mut plant = PlantBuilder::new();
    plant.add(TimerSpec::new(parameters([("delay_ticks", Value::Int(3))])));
    plant.add(LatchingAlarmSpec::new(
        parameters([
            ("low_limit", Value::Float(0.0)),
            ("high_limit", Value::Float(10.0)),
            ("priority", Value::Int(1)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(30)),
        ]),
        Rationalization {
            consequence: "the tank overfills".to_string(),
            required_action: "respond within the declared interval".to_string(),
            reference: "procedure".to_string(),
        },
    ));
    // Nothing emitted until the composition declares it.
    assert!(
        plant.build().unwrap().components[0]
            .parameter_units
            .is_empty()
    );
    let mut plant = PlantBuilder::new();
    let timer = plant.add(TimerSpec::new(parameters([("delay_ticks", Value::Int(3))])));
    plant.param_unit(timer.id, "delay_ticks", unit::TICKS);
    assert_eq!(
        plant.build().unwrap().components[0].parameter_units["delay_ticks"],
        "ticks"
    );
}

#[test]
fn param_unit_names_a_declared_and_supplied_parameter() {
    let mut plant = PlantBuilder::new();
    let interlock = plant.add(interlock());
    // `interlock` declares only `safe_value` — `limit` is no parameter
    // of the kind, so declaring its unit is a programming error.
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        plant.param_unit(interlock.id, "limit", unit::L);
    }));
    assert!(result.is_err());
}

#[test]
#[should_panic(expected = "spec declares unit")]
fn param_unit_cannot_overrule_the_spec() {
    let mut plant = PlantBuilder::new();
    let timer = plant.add(TimerSpec::new(parameters([("delay_ticks", Value::Int(3))])));
    // `delay_ticks` is the kind's inherent `ticks`; a composition
    // declaring `s` disagrees with the spec — a programming error.
    plant.param_unit(timer.id, "delay_ticks", "s");
}

#[test]
fn a_signal_inherits_its_point_unit() {
    let (mut plant, sim) = rig();
    let flow = plant.field_input::<f64>(PointId(1), channel_in(sim), false);
    plant.unit(flow, unit::M3_PER_H);
    plant
        .signal(SignalId(100), "flow", flow)
        .description("Measured process flow");
    let model = plant.build().unwrap();
    assert_eq!(model.signals[0].unit.as_deref(), Some("m3/h"));
}

#[test]
fn a_signal_disagreeing_with_its_point_fails_by_name() {
    let (mut plant, sim) = rig();
    let flow = plant.field_input::<f64>(PointId(1), channel_in(sim), false);
    plant.unit(flow, unit::M3_PER_H);
    plant.signal(SignalId(100), "flow", flow).unit("L/s");
    match plant.build().unwrap_err() {
        BuildError::Invalid(errors) => assert_eq!(
            errors,
            [ValidationError::SignalUnitMismatch {
                signal: SignalId(100),
                point: PointId(1),
                signal_unit: "L/s".to_string(),
                point_unit: "m3/h".to_string(),
            }]
        ),
        other => panic!("expected Invalid, got {other:?}"),
    }
}

#[test]
fn a_parameter_unit_must_hang_on_a_carried_value() {
    // A hand-written document declaring a unit for a parameter the
    // instance does not carry fails `load` — the dead-declaration half
    // of the parameter-units rule.
    let document = r#"{
        "version": 1,
        "devices": [],
        "io_points": [],
        "signals": [],
        "components": [
            {
                "id": 1,
                "kind": "interlock",
                "parameters": { "safe_value": { "float": 0.0 } },
                "parameter_units": { "gone": "m3/h" },
                "ports": {}
            }
        ],
        "connections": []
    }"#;
    match PlantModel::load(document).unwrap_err() {
        LoadError::Invalid(errors) => assert_eq!(
            errors,
            [ValidationError::UnknownParameter {
                component: ComponentId(1),
                parameter: "gone".to_string(),
            }]
        ),
        other => panic!("expected Invalid, got {other:?}"),
    }
}
