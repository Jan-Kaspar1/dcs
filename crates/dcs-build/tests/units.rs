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
//!
//! The cases below the seam's own rig are the adoption sweep: the
//! record's proof composition (the dosing skid) and the compositions
//! that followed it — the pump station, the IJmuiden scenario, the
//! showcase line, the M1 tank loop, and the consumer reference plant
//! — each carrying its measured and wired quantities' declared unit on
//! the point, the port, and the parameter, each signal inheriting its
//! point's declaration, and each doctored mismatch failing by name.

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

// ---------- the adoption sweep across the checked-in compositions ----------

/// The checked-in emitted documents the sweep reads. Each is the
/// composition's own artifact: the three `dcs-build` builder
/// compositions' emissions, the two hand-authored demo documents, and
/// the independent consumer's emitted model.
const PUMP_STATION: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/pump_station.json"
);
const DOSING_SKID: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/dosing_skid.json"
);
const IJMUIDEN: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/ijmuiden.json"
);
const SHOWCASE: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/showcase.json"
);
const TANK_LEVEL: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/tank_level.json"
);
const REFERENCE_PLANT: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../reference-plant/model/plant.json"
);

/// Every checked-in composition the sweep covers, with the label each
/// assertion names it by.
const COMPOSITIONS: [(&str, &str); 6] = [
    ("pump station", PUMP_STATION),
    ("dosing skid", DOSING_SKID),
    ("IJmuiden scenario", IJMUIDEN),
    ("showcase line", SHOWCASE),
    ("M1 tank loop", TANK_LEVEL),
    ("consumer reference plant", REFERENCE_PLANT),
];

/// Loads a checked-in composition through `dcs-model`'s validating
/// loader — the same path every consumer's document takes.
fn load(path: &str) -> PlantModel {
    PlantModel::load(&std::fs::read_to_string(path).unwrap()).unwrap()
}

/// The declared unit of `point`, or the empty string for an undeclared
/// point.
fn point_unit(model: &PlantModel, point: PointId) -> String {
    model
        .io_points
        .iter()
        .find(|declared| declared.id == point)
        .unwrap_or_else(|| panic!("{point:?} is not a declared point"))
        .unit
        .clone()
        .unwrap_or_default()
}

/// The declared unit of `component`'s `port`, or the empty string for
/// an undeclared port.
fn port_unit(model: &PlantModel, component: ComponentId, port: &str) -> String {
    model
        .components
        .iter()
        .find(|instance| instance.id == component)
        .unwrap_or_else(|| panic!("component {} is not declared", component.0))
        .ports
        .get(port)
        .unwrap_or_else(|| panic!("component {} declares no port {port:?}", component.0))
        .unit
        .clone()
        .unwrap_or_default()
}

/// The declared unit of `component`'s `parameter`, or the empty string
/// for an undeclared parameter.
fn parameter_unit(model: &PlantModel, component: ComponentId, parameter: &str) -> String {
    model
        .components
        .iter()
        .find(|instance| instance.id == component)
        .unwrap_or_else(|| panic!("component {} is not declared", component.0))
        .parameter_units
        .get(parameter)
        .cloned()
        .unwrap_or_default()
}

/// Asserts every `(point, unit)` pair the composition declares, and
/// that no other point declares one — the sweep's shape check, so a
/// later addition cannot quietly leave a measured quantity
/// undimensioned.
fn assert_points(model: &PlantModel, expected: &[(PointId, &str)]) {
    let mut declared: Vec<(u64, String)> = model
        .io_points
        .iter()
        .filter_map(|point| point.unit.as_ref().map(|unit| (point.id.0, unit.clone())))
        .collect();
    let mut wanted: Vec<(u64, String)> = expected
        .iter()
        .map(|(point, unit)| (point.0, (*unit).to_string()))
        .collect();
    declared.sort();
    wanted.sort();
    assert_eq!(declared, wanted);
}

/// Asserts every `(component, port, unit)` triple the composition
/// declares.
fn assert_ports(model: &PlantModel, expected: &[(ComponentId, &str, &str)]) {
    for (component, port, unit) in expected {
        assert_eq!(
            &port_unit(model, *component, port),
            *unit,
            "component {} port {port:?}",
            component.0
        );
    }
}

/// Asserts every `(component, parameter, unit)` triple the composition
/// declares.
fn assert_parameters(model: &PlantModel, expected: &[(ComponentId, &str, &str)]) {
    for (component, parameter, unit) in expected {
        assert_eq!(
            &parameter_unit(model, *component, parameter),
            *unit,
            "component {} parameter {parameter:?}",
            component.0
        );
    }
}

#[test]
fn the_pump_station_declares_every_quantity_it_measures_and_wires() {
    let model = load(PUMP_STATION);
    // The wet-well level path and the station flow meters, end to end.
    assert_points(
        &model,
        &[
            (PointId(10), unit::M),
            (PointId(11), unit::M),
            (PointId(12), unit::M3_PER_H),
            (PointId(13), unit::M3_PER_H),
            (PointId(20), unit::M3_PER_H),
            (PointId(21), unit::M3_PER_H),
            (PointId(200), unit::M),
            (PointId(201), unit::M),
            (PointId(202), unit::M),
            (PointId(203), unit::M),
            (PointId(204), unit::PUMPS),
            (PointId(205), unit::PUMPS),
            (PointId(211), unit::PUMPS),
        ],
    );
    assert_ports(
        &model,
        &[
            // The failover's three level ports.
            (ComponentId(1), "primary", unit::M),
            (ComponentId(1), "backup", unit::M),
            (ComponentId(1), "out", unit::M),
            // The chain's level and its stage-count demand, and the
            // group's matching stage-count surface.
            (ComponentId(2), "level", unit::M),
            (ComponentId(2), "demand", unit::PUMPS),
            (ComponentId(3), "demand", unit::PUMPS),
            (ComponentId(3), "duty", unit::PUMPS),
            (ComponentId(3), "staged", unit::PUMPS),
            // The two level alarms observe the selected level in `m`;
            // the per-pump protection interlocks gate it too. The
            // power guard and the cause guards bind held anchors and
            // stay undeclared.
            (ComponentId(7), "in", unit::M),
            (ComponentId(8), "in", unit::M),
            (ComponentId(23), "in", unit::M),
            (ComponentId(23), "out", unit::M),
            (ComponentId(42), "in", unit::M),
            (ComponentId(42), "out", unit::M),
        ],
    );
    assert_parameters(
        &model,
        &[
            // The chain's rungs are the level's metres.
            (ComponentId(2), "cutoff", unit::M),
            (ComponentId(2), "stop", unit::M),
            (ComponentId(2), "start", unit::M),
            (ComponentId(2), "lag_start", unit::M),
            (ComponentId(2), "high", unit::M),
            // The staging bounds count scans.
            (ComponentId(3), "start_delay_ticks", unit::TICKS),
            (ComponentId(3), "restage_delay_ticks", unit::TICKS),
            (ComponentId(3), "min_off_ticks", unit::TICKS),
            // The level alarms bound the level in `m` and their
            // lifecycle budgets in scans.
            (ComponentId(7), "low_limit", unit::M),
            (ComponentId(7), "high_limit", unit::M),
            (ComponentId(7), "hysteresis", unit::M),
            (ComponentId(7), "max_shelve_ticks", unit::TICKS),
            (ComponentId(7), "response_ticks", unit::TICKS),
            (ComponentId(8), "low_limit", unit::M),
            (ComponentId(8), "high_limit", unit::M),
            (ComponentId(8), "hysteresis", unit::M),
            (ComponentId(8), "max_shelve_ticks", unit::TICKS),
            (ComponentId(8), "response_ticks", unit::TICKS),
            // The protection interlocks' safe level, and the holdout
            // and motor budgets that guard the command.
            (ComponentId(23), "safe_value", unit::M),
            (ComponentId(27), "delay_ticks", unit::TICKS),
            (ComponentId(28), "fault_ticks", unit::TICKS),
            (ComponentId(46), "delay_ticks", unit::TICKS),
            (ComponentId(47), "fault_ticks", unit::TICKS),
        ],
    );
    // Every Bool alarm declares its two lifecycle budgets.
    for alarm in [9, 10, 11, 12, 13, 29, 30, 31, 48, 49, 50] {
        assert_parameters(
            &model,
            &[
                (ComponentId(alarm), "max_shelve_ticks", unit::TICKS),
                (ComponentId(alarm), "response_ticks", unit::TICKS),
            ],
        );
    }
}

#[test]
fn the_dosing_skid_still_declares_its_recorded_dimensions() {
    // The record's proof composition is the regression guard for the
    // sweep: adopting units elsewhere must not move what it emits.
    let model = load(DOSING_SKID);
    assert_points(
        &model,
        &[
            // The process flow and the paced demand chain in `g/h`.
            (PointId(10), unit::M3_PER_H),
            (PointId(11), unit::M3_PER_H),
            (PointId(13), unit::G_PER_H),
            (PointId(16), unit::G_PER_H),
            (PointId(34), unit::G_PER_H),
            (PointId(35), unit::G_PER_H),
            (PointId(200), unit::G_PER_H),
            (PointId(201), unit::G_PER_H),
            (PointId(222), unit::G_PER_H),
            (PointId(223), unit::G_PER_H),
            (PointId(225), unit::G_PER_H),
            (PointId(226), unit::G_PER_H),
            (PointId(227), unit::G_PER_H),
            (PointId(228), unit::G_PER_H),
            (PointId(242), unit::G_PER_H),
            (PointId(315), unit::G_PER_H),
            (PointId(316), unit::G_PER_H),
            (PointId(317), unit::G_PER_H),
            (PointId(347), unit::G_PER_H),
            (PointId(348), unit::G_PER_H),
            (PointId(349), unit::G_PER_H),
            // The dose bound and the tank's levels and throughput.
            (PointId(240), unit::MG_PER_L),
            (PointId(12), unit::L),
            (PointId(14), unit::L_PER_SCAN),
            (PointId(15), unit::L_PER_SCAN),
            (PointId(30), unit::L_PER_SCAN),
            (PointId(31), unit::L_PER_SCAN),
            // The pump counts, the totalized mass, the deviation ratio,
            // and the speed outputs.
            (PointId(230), unit::PUMPS),
            (PointId(231), unit::PUMPS),
            (PointId(232), unit::PUMPS),
            (PointId(233), unit::PUMPS),
            (PointId(244), unit::G),
            (PointId(245), unit::FRACTION),
            (PointId(319), unit::STROKES),
            (PointId(351), unit::STROKES),
            (PointId(110), unit::PERCENT),
            (PointId(111), unit::PERCENT),
        ],
    );
    assert_ports(
        &model,
        &[
            (ComponentId(1), "flow", unit::M3_PER_H),
            (ComponentId(1), "dose", unit::MG_PER_L),
            (ComponentId(1), "demand", unit::G_PER_H),
            (ComponentId(8), "in", unit::G_PER_H),
            (ComponentId(8), "out", unit::G_PER_H),
            (ComponentId(10), "demand", unit::PUMPS),
            (ComponentId(11), "demand", unit::PUMPS),
            (ComponentId(11), "duty", unit::PUMPS),
            (ComponentId(11), "staged", unit::PUMPS),
            (ComponentId(12), "rate", unit::G_PER_H),
            (ComponentId(12), "total", unit::G),
            (ComponentId(13), "deviation", unit::FRACTION),
            (ComponentId(26), "eng", unit::G_PER_H),
            (ComponentId(26), "raw", unit::PERCENT),
        ],
    );
    assert_parameters(
        &model,
        &[
            (ComponentId(1), "min_dose", unit::MG_PER_L),
            (ComponentId(1), "max_dose", unit::MG_PER_L),
            (ComponentId(1), "fallback_rate", unit::G_PER_H),
            (ComponentId(10), "cutoff", unit::G_PER_H),
            (ComponentId(11), "min_off_ticks", unit::TICKS),
            (ComponentId(14), "low_limit", unit::L),
            (ComponentId(14), "response_ticks", unit::TICKS),
            (ComponentId(24), "fault_ticks", unit::TICKS),
            (ComponentId(26), "eng_min", unit::G_PER_H),
            (ComponentId(26), "raw_min", unit::PERCENT),
        ],
    );
}

#[test]
fn the_ijmuiden_scenario_declares_every_quantity_it_measures_and_wires() {
    let model = load(IJMUIDEN);
    // The canal level path in metres, the tide and discharge flows in
    // metres per second, the gate position path as a fraction of
    // travel, and the chain's annunciation rung count.
    assert_points(
        &model,
        &[
            (PointId(10), unit::M),
            (PointId(11), unit::M),
            (PointId(12), unit::M_PER_S),
            (PointId(13), unit::M_PER_S),
            (PointId(14), unit::M_PER_S),
            (PointId(15), unit::M_PER_S),
            (PointId(16), unit::FRACTION),
            (PointId(20), unit::FRACTION),
            (PointId(200), unit::M),
            (PointId(201), unit::M),
            (PointId(203), unit::M),
            (PointId(204), unit::M),
            (PointId(205), unit::M),
            (PointId(206), unit::M),
            (PointId(207), unit::M_PER_SCAN),
            (PointId(209), unit::M_PER_SCAN),
            (PointId(220), unit::FRACTION),
            (PointId(221), unit::FRACTION),
            (PointId(230), unit::STAGES),
            (PointId(240), unit::FRACTION),
            (PointId(241), unit::FRACTION),
        ],
    );
    assert_ports(
        &model,
        &[
            (ComponentId(1), "primary", unit::M),
            (ComponentId(1), "backup", unit::M),
            (ComponentId(1), "out", unit::M),
            // The filter smooths the level, not a fixed dimension of its
            // own.
            (ComponentId(2), "in", unit::M),
            (ComponentId(2), "out", unit::M),
            (ComponentId(3), "level", unit::M),
            // The annunciation rungs a discharge gate declares, not
            // pump stages.
            (ComponentId(3), "demand", unit::STAGES),
            // The dedicated rate-of-rise detector reads the filtered
            // level and reports its per-scan first difference.
            (ComponentId(4), "in", unit::M),
            (ComponentId(4), "rate", unit::M_PER_SCAN),
            (ComponentId(5), "control", unit::FRACTION),
            (ComponentId(5), "manual", unit::FRACTION),
            (ComponentId(5), "out", unit::FRACTION),
            (ComponentId(6), "cmd", unit::FRACTION),
            (ComponentId(6), "out", unit::FRACTION),
            (ComponentId(6), "fb", unit::FRACTION),
            (ComponentId(7), "in", unit::M),
        ],
    );
    assert_parameters(
        &model,
        &[
            (ComponentId(3), "cutoff", unit::M),
            (ComponentId(3), "high", unit::M),
            (ComponentId(4), "rate_limit", unit::M_PER_SCAN),
            (ComponentId(4), "initial_rate", unit::M_PER_SCAN),
            (ComponentId(5), "transfer_delta", unit::FRACTION),
            (ComponentId(6), "tolerance", unit::FRACTION),
            (ComponentId(6), "discrepancy_ticks", unit::TICKS),
            (ComponentId(7), "low_limit", unit::M),
            (ComponentId(7), "high_limit", unit::M),
            (ComponentId(7), "hysteresis", unit::M),
            (ComponentId(7), "max_shelve_ticks", unit::TICKS),
            (ComponentId(7), "response_ticks", unit::TICKS),
        ],
    );
    // Every other alarm declares its decision-70 response budget.
    for alarm in [8, 9, 10, 11, 12, 13, 14, 15] {
        assert_parameters(
            &model,
            &[(ComponentId(alarm), "response_ticks", unit::TICKS)],
        );
    }
}

#[test]
fn the_showcase_line_declares_its_dimensional_contract() {
    let model = load(SHOWCASE);
    // The transmitter and valve field channels carry `mA`; the level,
    // valve, batch, and flow signals the plant engineers in `%`.
    assert_points(
        &model,
        &[
            (PointId(10), unit::MA),
            (PointId(11), unit::MA),
            (PointId(12), unit::MA),
            (PointId(13), unit::MA),
            (PointId(14), unit::MA),
            (PointId(20), unit::MA),
            (PointId(50), unit::PERCENT),
            (PointId(52), unit::PERCENT),
            (PointId(60), unit::PERCENT),
            (PointId(61), unit::PERCENT),
            (PointId(62), unit::PERCENT),
            (PointId(75), unit::PERCENT),
            (PointId(76), unit::PERCENT),
            (PointId(77), unit::PERCENT),
            (PointId(81), unit::PERCENT),
            (PointId(82), unit::PERCENT),
            (PointId(86), unit::PERCENT),
            (PointId(87), unit::PERCENT),
        ],
    );
    assert_ports(
        &model,
        &[
            // The four transmitter conditioners read `mA` and deliver
            // the engineering `%`; the fifth reads the flow meter.
            (ComponentId(5), "raw", unit::MA),
            (ComponentId(5), "out", unit::PERCENT),
            (ComponentId(11), "eng", unit::PERCENT),
            (ComponentId(11), "raw", unit::MA),
            (ComponentId(12), "raw", unit::MA),
            (ComponentId(12), "out", unit::PERCENT),
            (ComponentId(16), "raw", unit::MA),
            (ComponentId(16), "out", unit::PERCENT),
            (ComponentId(17), "raw", unit::MA),
            (ComponentId(17), "out", unit::PERCENT),
            (ComponentId(22), "raw", unit::MA),
            (ComponentId(22), "out", unit::PERCENT),
            // The level loop, the valve station, and the batch
            // program's demand path are all `%` of span.
            (ComponentId(6), "in", unit::PERCENT),
            (ComponentId(6), "out", unit::PERCENT),
            (ComponentId(7), "sp", unit::PERCENT),
            (ComponentId(7), "pv", unit::PERCENT),
            (ComponentId(7), "out", unit::PERCENT),
            (ComponentId(8), "in", unit::PERCENT),
            (ComponentId(8), "out", unit::PERCENT),
            (ComponentId(9), "control", unit::PERCENT),
            (ComponentId(9), "operator", unit::PERCENT),
            (ComponentId(9), "out", unit::PERCENT),
            (ComponentId(10), "cmd", unit::PERCENT),
            (ComponentId(10), "out", unit::PERCENT),
            (ComponentId(10), "fb", unit::PERCENT),
            (ComponentId(18), "in_1", unit::PERCENT),
            (ComponentId(18), "in_2", unit::PERCENT),
            (ComponentId(18), "in_3", unit::PERCENT),
            (ComponentId(18), "out", unit::PERCENT),
            (ComponentId(19), "in", unit::PERCENT),
            (ComponentId(19), "out", unit::PERCENT),
            (ComponentId(20), "out", unit::PERCENT),
            (ComponentId(21), "control", unit::PERCENT),
            (ComponentId(21), "manual", unit::PERCENT),
            (ComponentId(21), "out", unit::PERCENT),
            // The totalizer accumulates the flow signal.
            (ComponentId(23), "rate", unit::PERCENT),
            (ComponentId(13), "in", unit::PERCENT),
            (ComponentId(24), "in", unit::PERCENT),
        ],
    );
    assert_parameters(
        &model,
        &[
            (ComponentId(5), "raw_min", unit::MA),
            (ComponentId(5), "raw_max", unit::MA),
            (ComponentId(5), "eng_min", unit::PERCENT),
            (ComponentId(5), "eng_max", unit::PERCENT),
            (ComponentId(6), "max_delta", unit::PERCENT),
            (ComponentId(7), "out_min", unit::PERCENT),
            (ComponentId(7), "out_max", unit::PERCENT),
            (ComponentId(8), "safe_value", unit::PERCENT),
            (ComponentId(10), "tolerance", unit::PERCENT),
            (ComponentId(10), "discrepancy_ticks", unit::TICKS),
            (ComponentId(13), "low_limit", unit::PERCENT),
            (ComponentId(13), "high_limit", unit::PERCENT),
            (ComponentId(13), "hysteresis", unit::PERCENT),
            (ComponentId(18), "tolerance", unit::PERCENT),
            (ComponentId(21), "transfer_delta", unit::PERCENT),
            (ComponentId(24), "low_limit", unit::PERCENT),
            (ComponentId(24), "high_limit", unit::PERCENT),
            (ComponentId(24), "hysteresis", unit::PERCENT),
            (ComponentId(24), "response_ticks", unit::TICKS),
        ],
    );
    // Every scan interval the line declares counts ticks.
    for (component, parameter) in [
        (1, "debounce_ticks"),
        (2, "debounce_ticks"),
        (3, "delay_ticks"),
        (4, "fault_ticks"),
        (20, "step_1_ticks"),
        (20, "step_2_ticks"),
        (20, "step_3_ticks"),
        (20, "step_4_ticks"),
        (20, "step_5_ticks"),
    ] {
        assert_parameters(&model, &[(ComponentId(component), parameter, unit::TICKS)]);
    }
    // The batch table's driven values are the program's `%` demands.
    for step in 1..=5 {
        let name = format!("step_{step}_out");
        assert_parameters(&model, &[(ComponentId(20), name.as_str(), unit::PERCENT)]);
    }
}

#[test]
fn the_m1_tank_loop_declares_its_dimensional_contract() {
    let model = load(TANK_LEVEL);
    assert_points(
        &model,
        &[
            (PointId(10), unit::MA),
            (PointId(11), unit::PERCENT),
            // The M1 sheet has no `analog-output` scaling stage: the
            // controller's manipulated variable is written straight to
            // the command channel, so the output is the channel's.
            (PointId(20), unit::MA),
        ],
    );
    assert_ports(
        &model,
        &[
            (ComponentId(1), "raw", unit::MA),
            (ComponentId(1), "out", unit::PERCENT),
            (ComponentId(2), "sp", unit::PERCENT),
            (ComponentId(2), "pv", unit::PERCENT),
            (ComponentId(2), "out", unit::MA),
        ],
    );
    assert_parameters(
        &model,
        &[
            (ComponentId(1), "raw_min", unit::MA),
            (ComponentId(1), "raw_max", unit::MA),
            (ComponentId(1), "eng_min", unit::PERCENT),
            (ComponentId(1), "eng_max", unit::PERCENT),
            (ComponentId(2), "out_min", unit::MA),
            (ComponentId(2), "out_max", unit::MA),
        ],
    );
}

#[test]
fn the_consumer_reference_plant_adopts_the_same_convention() {
    // The customer-boundary evidence: an independent composition
    // depending only on the released `dcs-build` surface declares its
    // dimensions the same way, and its emitted artifact records them.
    let model = load(REFERENCE_PLANT);
    assert_points(
        &model,
        &[
            (PointId(10), unit::M),
            (PointId(11), unit::M),
            (PointId(12), unit::M3_PER_H),
            (PointId(13), unit::M3_PER_H),
            (PointId(20), unit::M3_PER_H),
            (PointId(21), unit::M3_PER_H),
            (PointId(200), unit::M),
            (PointId(201), unit::M),
            (PointId(202), unit::M),
            (PointId(203), unit::M),
            (PointId(204), unit::PUMPS),
            (PointId(205), unit::PUMPS),
            (PointId(210), unit::PUMPS),
            (PointId(211), unit::PUMPS),
        ],
    );
    assert_ports(
        &model,
        &[
            (ComponentId(1), "primary", unit::M),
            (ComponentId(1), "backup", unit::M),
            (ComponentId(1), "out", unit::M),
            (ComponentId(2), "level", unit::M),
            (ComponentId(2), "demand", unit::PUMPS),
            (ComponentId(3), "demand", unit::PUMPS),
            (ComponentId(3), "duty", unit::PUMPS),
            (ComponentId(3), "staged", unit::PUMPS),
            (ComponentId(7), "in", unit::M),
            (ComponentId(8), "in", unit::M),
            (ComponentId(22), "in", unit::M),
            (ComponentId(22), "out", unit::M),
            (ComponentId(41), "in", unit::M),
            (ComponentId(41), "out", unit::M),
        ],
    );
    assert_parameters(
        &model,
        &[
            (ComponentId(2), "cutoff", unit::M),
            (ComponentId(2), "high", unit::M),
            (ComponentId(3), "start_delay_ticks", unit::TICKS),
            (ComponentId(3), "restage_delay_ticks", unit::TICKS),
            (ComponentId(3), "min_off_ticks", unit::TICKS),
            (ComponentId(7), "low_limit", unit::M),
            (ComponentId(7), "hysteresis", unit::M),
            (ComponentId(7), "response_ticks", unit::TICKS),
            (ComponentId(8), "high_limit", unit::M),
            (ComponentId(8), "max_shelve_ticks", unit::TICKS),
            (ComponentId(22), "safe_value", unit::M),
            (ComponentId(26), "delay_ticks", unit::TICKS),
            (ComponentId(27), "fault_ticks", unit::TICKS),
            // The exercise table's step intervals count scans.
            (ComponentId(51), "step_1_ticks", unit::TICKS),
            (ComponentId(51), "step_2_ticks", unit::TICKS),
        ],
    );
}

#[test]
fn every_signal_inherits_its_points_declared_unit() {
    // The display string cannot drift from the wiring: a signal
    // sourcing a declared point renders that point's unit, and no
    // checked-in composition declares a signal unit of its own.
    for (label, path) in COMPOSITIONS {
        let model = load(path);
        for signal in &model.signals {
            let declared = model
                .io_points
                .iter()
                .find(|point| point.id == signal.source)
                .and_then(|point| point.unit.as_deref());
            if let Some(declared) = declared {
                assert_eq!(
                    signal.unit.as_deref(),
                    Some(declared),
                    "{label}: signal {:?} declares {:?} beside its point's {declared}",
                    signal.id,
                    signal.unit
                );
            }
        }
        // And the document validates: every wired end pair agrees.
        assert_eq!(model.validate(), Vec::new(), "{label}");
    }
}

#[test]
fn every_declared_unit_hangs_on_a_carried_value() {
    // The dead-declaration half: a `parameter_units` key the instance
    // does not carry, and a `unit` on a point no signal renders, would
    // both be a declaration nothing backs. The document's own
    // validation reports the first; the second is this check.
    for (label, path) in COMPOSITIONS {
        let model = load(path);
        for component in &model.components {
            for parameter in component.parameter_units.keys() {
                assert!(
                    component.parameters.contains_key(parameter),
                    "{label}: component {} declares a unit for absent parameter {parameter:?}",
                    component.id.0
                );
            }
        }
        assert!(
            model.io_points.iter().any(|point| point.unit.is_some()),
            "{label}: no point declares a unit"
        );
    }
}

#[test]
fn a_doctored_unit_mismatch_in_a_composition_fails_by_name() {
    // Each composition is doctored at one wired end — a point whose
    // counterpart port declares the unit it was engineered in — and
    // the document refuses to load with the named diagnostic rather
    // than passing every structural check.
    for (label, path, point, original, doctored) in [
        (
            "pump station",
            PUMP_STATION,
            PointId(10),
            unit::M,
            unit::M3_PER_H,
        ),
        (
            "dosing skid",
            DOSING_SKID,
            PointId(10),
            unit::M3_PER_H,
            unit::G_PER_H,
        ),
        (
            "IJmuiden scenario",
            IJMUIDEN,
            PointId(10),
            unit::M,
            unit::M_PER_S,
        ),
        (
            "showcase line",
            SHOWCASE,
            PointId(60),
            unit::PERCENT,
            unit::MA,
        ),
        (
            "M1 tank loop",
            TANK_LEVEL,
            PointId(11),
            unit::PERCENT,
            unit::MA,
        ),
        (
            "consumer reference plant",
            REFERENCE_PLANT,
            PointId(10),
            unit::M,
            unit::M3_PER_H,
        ),
    ] {
        // The pristine composition loads; the doctored copy is the same
        // document with one wired end's declaration replaced.
        assert_eq!(&point_unit(&load(path), point), original, "{label}");
        let mut document: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
        let declared = document["io_points"]
            .as_array_mut()
            .unwrap()
            .iter_mut()
            .find(|entry| entry["id"] == point.0)
            .unwrap_or_else(|| panic!("{label}: {point:?} is not a declared point"));
        declared["unit"] = serde_json::Value::String(doctored.to_string());
        let LoadError::Invalid(errors) = PlantModel::load(&document.to_string()).unwrap_err()
        else {
            panic!("{label}: the doctored document loaded");
        };
        assert!(
            errors
                .iter()
                .any(|error| matches!(error, ValidationError::ConnectionUnitMismatch { .. })),
            "{label}: expected ConnectionUnitMismatch, got {errors:?}"
        );
    }
}

#[test]
fn every_checked_in_composition_round_trips_its_declared_units() {
    // Byte-stability for the additive fields: re-serializing a loaded
    // composition and loading it again preserves every declaration,
    // and re-emitting the document is deterministic.
    for (label, path) in COMPOSITIONS {
        let model = load(path);
        let reserialized = serde_json::to_string_pretty(&model).unwrap();
        assert_eq!(PlantModel::load(&reserialized).unwrap(), model, "{label}");
        // Loading the same artifact twice yields the same bytes.
        assert_eq!(
            serde_json::to_string_pretty(&load(path)).unwrap(),
            reserialized,
            "{label}"
        );
    }
}
