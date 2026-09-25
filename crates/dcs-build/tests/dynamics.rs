//! The typed dynamics-composition seam: every `dcs-sim` process-element
//! kind emits through compile-checked builders, point references bind
//! through the typed handles the model composition returns, and
//! emit-time validation rejects — with named diagnostics — what the
//! `dcs-plant-server --dynamics` merge would name at server startup.
//!
//! The conformance leg emits the reference station's dynamics document
//! through the typed seam and holds it byte-identical to the checked-in
//! `reference-plant/model/dynamics.json`, then merges the emitted bytes
//! through the same `ProcessElement` parse and channel-map validation
//! the server runs.

use dcs_build::dynamics::{
    BoolFlow, DeadTime, DynamicsElement, FirstOrderLag, FlowSum, Integrator, Noise, ScaledFlow,
    SecondOrderLag, Threshold,
};
use dcs_build::{
    Direction, DynamicsBuilder, DynamicsError, InPoint, OutPoint, PlantBuilder, PointId, ValueKind,
};
use dcs_model::PlantModel;
use dcs_sim::ProcessElement;

/// The checked-in reference station dynamics document — the artifact
/// the typed composition must reproduce byte-for-byte.
const REFERENCE_DYNAMICS: &str = include_str!(concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../reference-plant/model/dynamics.json"
));

/// The typed handles the reference dynamics document's elements bind,
/// returned by the rig's point declarations.
struct StationPoints {
    level_primary: InPoint<f64>,
    level_backup: InPoint<f64>,
    inflow: InPoint<f64>,
    net_flow: InPoint<f64>,
    well_full: InPoint<bool>,
    draw0: InPoint<f64>,
    draw1: InPoint<f64>,
    cmd0: OutPoint<bool>,
    cmd1: OutPoint<bool>,
}

/// Composes the field points the reference dynamics document addresses:
/// the level pair, inflow and net flow, the high-well contact, two pump
/// draws, and two pump commands — a stand-in for the station model the
/// document merges over.
fn station_rig() -> (PlantModel, StationPoints) {
    let mut plant = PlantBuilder::new();
    let sim = plant.device("sim").id;
    let level_primary_ch = plant.channel::<f64>(sim, "level-primary", Direction::In);
    let level_backup_ch = plant.channel::<f64>(sim, "level-backup", Direction::In);
    let inflow_ch = plant.channel::<f64>(sim, "inflow", Direction::In);
    let net_flow_ch = plant.channel::<f64>(sim, "net-flow", Direction::In);
    let well_full_ch = plant.channel::<bool>(sim, "well-full", Direction::In);
    let draw0_ch = plant.channel::<f64>(sim, "p0-draw", Direction::In);
    let draw1_ch = plant.channel::<f64>(sim, "p1-draw", Direction::In);
    let cmd0_ch = plant.channel::<bool>(sim, "p0-cmd", Direction::Out);
    let cmd1_ch = plant.channel::<bool>(sim, "p1-cmd", Direction::Out);

    let points = StationPoints {
        level_primary: plant.field_input::<f64>(PointId(10), level_primary_ch, false),
        level_backup: plant.field_input::<f64>(PointId(11), level_backup_ch, false),
        inflow: plant.field_input::<f64>(PointId(12), inflow_ch, false),
        net_flow: plant.field_input::<f64>(PointId(13), net_flow_ch, false),
        well_full: plant.field_input::<bool>(PointId(15), well_full_ch, false),
        draw0: plant.field_input::<f64>(PointId(20), draw0_ch, false),
        draw1: plant.field_input::<f64>(PointId(21), draw1_ch, false),
        cmd0: plant.field_output::<bool>(PointId(100), cmd0_ch),
        cmd1: plant.field_output::<bool>(PointId(101), cmd1_ch),
    };
    (plant.build().unwrap(), points)
}

/// The reference station's dynamics, composed through the typed seam
/// exactly as the checked-in document declares it.
fn station_dynamics(points: &StationPoints) -> DynamicsBuilder {
    let mut dynamics = DynamicsBuilder::new();
    dynamics
        .bool_flow(points.well_full, points.inflow, 0.0, 0.25, 0.25)
        .bool_flow(points.cmd0, points.draw0, -0.4, 0.0, 0.0)
        .bool_flow(points.cmd1, points.draw1, -0.4, 0.0, 0.0)
        .flow_sum(
            [points.inflow, points.draw0, points.draw1],
            points.net_flow,
            0.0,
            0.25,
        )
        .integrator(points.net_flow, points.level_primary, 3.5)
        .integrator(points.net_flow, points.level_backup, 3.45)
        .threshold(points.level_backup, points.well_full, 4.9, 4.7, false);
    dynamics
}

#[test]
fn emitted_document_is_byte_identical_to_the_checked_in_dynamics() {
    let (model, points) = station_rig();
    let document = station_dynamics(&points).emit(&model).unwrap();
    let emitted = serde_json::to_string_pretty(&document).unwrap();
    assert_eq!(
        emitted, REFERENCE_DYNAMICS,
        "the typed composition must emit the checked-in dynamics document byte-for-byte"
    );
}

#[test]
fn emission_is_byte_deterministic() {
    let (model, points) = station_rig();
    let emit =
        || serde_json::to_string_pretty(&station_dynamics(&points).emit(&model).unwrap()).unwrap();
    assert_eq!(emit(), emit());
}

#[test]
fn emitted_document_merges_through_the_dynamics_path() {
    let (model, points) = station_rig();
    let document = station_dynamics(&points).emit(&model).unwrap();
    let emitted = serde_json::to_string_pretty(&document).unwrap();
    // The merge `dcs-plant-server --dynamics` runs: parse the document
    // as the `ProcessElement` list and land each element on the
    // resolved channel map, validating as it goes.
    let elements: Vec<ProcessElement> = serde_json::from_str(&emitted).unwrap();
    let mut map = dcs_assembly::sim_channel_map(&model).unwrap();
    for element in elements {
        map = map.with_element(element);
        map.validate().unwrap();
    }
}

/// A field-point rig covering every element kind: `Float` sources,
/// `Float` outputs the elements drive, a `Bool` gate, and a `Bool`
/// contact.
struct KindPoints {
    source: InPoint<f64>,
    demand: OutPoint<f64>,
    gate: OutPoint<bool>,
    contact: InPoint<bool>,
    out: [InPoint<f64>; 8],
}

fn all_kinds_rig() -> (PlantModel, KindPoints) {
    let mut plant = PlantBuilder::new();
    let sim = plant.device("sim").id;
    let source_ch = plant.channel::<f64>(sim, "source", Direction::In);
    let demand_ch = plant.channel::<f64>(sim, "demand", Direction::Out);
    let gate_ch = plant.channel::<bool>(sim, "gate", Direction::Out);
    let contact_ch = plant.channel::<bool>(sim, "contact", Direction::In);
    let out_channels: Vec<_> = (0..8)
        .map(|i| plant.channel::<f64>(sim, &format!("out-{i}"), Direction::In))
        .collect();

    let points = KindPoints {
        source: plant.field_input::<f64>(PointId(1), source_ch, false),
        demand: plant.field_output::<f64>(PointId(2), demand_ch),
        gate: plant.field_output::<bool>(PointId(20), gate_ch),
        contact: plant.field_input::<bool>(PointId(21), contact_ch, false),
        out: <[InPoint<f64>; 8]>::try_from(
            out_channels
                .into_iter()
                .enumerate()
                .map(|(i, ch)| plant.field_input::<f64>(PointId(30 + i as u64), ch, false))
                .collect::<Vec<_>>(),
        )
        .unwrap(),
    };
    (plant.build().unwrap(), points)
}

#[test]
fn every_element_kind_emits_the_dynamics_grammar() {
    let (model, points) = all_kinds_rig();
    let mut dynamics = DynamicsBuilder::new();
    dynamics
        .first_order_lag(points.source, points.out[0], 2.0, 4.0)
        .second_order_lag(points.source, points.out[1], 1.0, 0.25, 4.0)
        .integrator(points.source, points.out[2], 3.5)
        .dead_time(points.source, points.out[3], 3.0, 0.0)
        .noise(points.source, points.out[4], 0.5, 42, 4.0)
        .bool_flow(points.gate, points.out[5], -10.0, 0.0, 0.0)
        .flow_sum([points.source, points.out[5]], points.out[6], 4.0, 4.0)
        .scaled_flow(points.demand, points.out[7], 0.5, 0.0)
        .threshold(points.out[2], points.contact, 8.0, 7.5, false);
    let document = dynamics.emit(&model).unwrap();
    let emitted = serde_json::to_string_pretty(&document).unwrap();
    // Every kind parses through the document's own grammar and merges
    // onto the resolved channel map.
    let elements: Vec<ProcessElement> = serde_json::from_str(&emitted).unwrap();
    assert_eq!(elements.len(), 9);
    for (kind, element) in [
        "FirstOrderLag",
        "SecondOrderLag",
        "Integrator",
        "DeadTime",
        "Noise",
        "BoolFlow",
        "FlowSum",
        "ScaledFlow",
        "Threshold",
    ]
    .iter()
    .zip(&elements)
    {
        let name = match element {
            ProcessElement::FirstOrderLag(_) => "FirstOrderLag",
            ProcessElement::SecondOrderLag(_) => "SecondOrderLag",
            ProcessElement::Integrator(_) => "Integrator",
            ProcessElement::DeadTime(_) => "DeadTime",
            ProcessElement::Noise(_) => "Noise",
            ProcessElement::BoolFlow(_) => "BoolFlow",
            ProcessElement::FlowSum(_) => "FlowSum",
            ProcessElement::ScaledFlow(_) => "ScaledFlow",
            ProcessElement::Threshold(_) => "Threshold",
        };
        assert_eq!(*kind, name);
    }
    let mut map = dcs_assembly::sim_channel_map(&model).unwrap();
    for element in elements {
        map = map.with_element(element);
        map.validate().unwrap();
    }
}

/// The drift guard pinning the mirror: each emitted element serializes
/// identically to the `dcs-sim` vocabulary item it mirrors, so a
/// vocabulary change lands as a diff here, not at a consumer's merge.
#[test]
fn emitted_elements_match_the_sim_vocabulary() {
    let cases: Vec<(DynamicsElement, ProcessElement)> = vec![
        (
            DynamicsElement::FirstOrderLag(FirstOrderLag {
                input: PointId(1),
                output: PointId(2),
                time_constant: 2.0,
                initial: 4.0,
            }),
            ProcessElement::FirstOrderLag(dcs_sim::FirstOrderLag {
                input: PointId(1),
                output: PointId(2),
                time_constant: 2.0,
                initial: 4.0,
            }),
        ),
        (
            DynamicsElement::SecondOrderLag(SecondOrderLag {
                input: PointId(1),
                output: PointId(2),
                time_constant: 1.5,
                damping_ratio: 0.5,
                initial: 13.6,
            }),
            ProcessElement::SecondOrderLag(dcs_sim::SecondOrderLag {
                input: PointId(1),
                output: PointId(2),
                time_constant: 1.5,
                damping_ratio: 0.5,
                initial: 13.6,
            }),
        ),
        (
            DynamicsElement::Integrator(Integrator {
                input: PointId(1),
                output: PointId(2),
                initial: 50.0,
            }),
            ProcessElement::Integrator(dcs_sim::Integrator {
                input: PointId(1),
                output: PointId(2),
                initial: 50.0,
            }),
        ),
        (
            DynamicsElement::DeadTime(DeadTime {
                input: PointId(1),
                output: PointId(2),
                delay: 3.0,
                initial: 0.0,
            }),
            ProcessElement::DeadTime(dcs_sim::DeadTime {
                input: PointId(1),
                output: PointId(2),
                delay: 3.0,
                initial: 0.0,
            }),
        ),
        (
            DynamicsElement::Noise(Noise {
                input: PointId(1),
                output: PointId(2),
                amplitude: 0.5,
                seed: 42,
                initial: 4.0,
            }),
            ProcessElement::Noise(dcs_sim::Noise {
                input: PointId(1),
                output: PointId(2),
                amplitude: 0.5,
                seed: 42,
                initial: 4.0,
            }),
        ),
        (
            DynamicsElement::BoolFlow(BoolFlow {
                input: PointId(20),
                output: PointId(12),
                on_rate: -10.0,
                off_rate: 0.0,
                initial: 0.0,
            }),
            ProcessElement::BoolFlow(dcs_sim::BoolFlow {
                input: PointId(20),
                output: PointId(12),
                on_rate: -10.0,
                off_rate: 0.0,
                initial: 0.0,
            }),
        ),
        (
            DynamicsElement::FlowSum(FlowSum {
                inputs: vec![PointId(11), PointId(12), PointId(13)],
                output: PointId(14),
                bias: 4.0,
                initial: 4.0,
            }),
            ProcessElement::FlowSum(dcs_sim::FlowSum {
                inputs: vec![PointId(11), PointId(12), PointId(13)],
                output: PointId(14),
                bias: 4.0,
                initial: 4.0,
            }),
        ),
        (
            DynamicsElement::ScaledFlow(ScaledFlow {
                input: PointId(20),
                output: PointId(11),
                gain: 0.5,
                initial: 0.0,
            }),
            ProcessElement::ScaledFlow(dcs_sim::ScaledFlow {
                input: PointId(20),
                output: PointId(11),
                gain: 0.5,
                initial: 0.0,
            }),
        ),
        (
            DynamicsElement::Threshold(Threshold {
                input: PointId(10),
                output: PointId(30),
                on: 8.0,
                off: 7.5,
                initial: false,
            }),
            ProcessElement::Threshold(dcs_sim::Threshold {
                input: PointId(10),
                output: PointId(30),
                on: 8.0,
                off: 7.5,
                initial: false,
            }),
        ),
    ];
    for (emitted, sim) in cases {
        assert_eq!(
            serde_json::to_value(&emitted).unwrap(),
            serde_json::to_value(&sim).unwrap(),
            "{emitted:?} must serialize identically to the dcs-sim vocabulary item it mirrors"
        );
    }
}

/// A `Float` handle from a composition `model` does not contain — the
/// stale reference emit must name.
fn foreign_f64(id: PointId) -> InPoint<f64> {
    let mut other = PlantBuilder::new();
    let sim = other.device("sim").id;
    let ch = other.channel::<f64>(sim, "foreign", Direction::In);
    other.field_input::<f64>(id, ch, false)
}

#[test]
fn element_referencing_an_undeclared_point_is_a_named_rejection() {
    let (model, points) = station_rig();
    let mut dynamics = DynamicsBuilder::new();
    dynamics
        .integrator(points.net_flow, points.level_primary, 3.5)
        // Point 999 is declared nowhere in `model`.
        .integrator(points.net_flow, foreign_f64(PointId(999)), 0.0);
    let error = dynamics.emit(&model).unwrap_err();
    assert_eq!(
        error,
        DynamicsError::UnknownPoint {
            element: 1,
            point: PointId(999),
        }
    );
    let message = error.to_string();
    assert!(message.contains("element 1"), "{message}");
    assert!(message.contains("point 999"), "{message}");
}

#[test]
fn element_referencing_an_internal_point_is_a_named_rejection() {
    let mut plant = PlantBuilder::new();
    let sim = plant.device("sim").id;
    let ch = plant.channel::<f64>(sim, "level", Direction::In);
    let level = plant.field_input::<f64>(PointId(10), ch, false);
    // An image-carried internal point: declared, but the simulated
    // field has no channel binding for it.
    let carrier = plant.internal_output::<f64>(PointId(200), 0.0);
    let model = plant.build().unwrap();

    let mut dynamics = DynamicsBuilder::new();
    dynamics.integrator(carrier, level, 0.0);
    let error = dynamics.emit(&model).unwrap_err();
    assert_eq!(
        error,
        DynamicsError::InternalPoint {
            element: 0,
            point: PointId(200),
        }
    );
    let message = error.to_string();
    assert!(message.contains("element 0"), "{message}");
    assert!(message.contains("internal point 200"), "{message}");
}

#[test]
fn element_end_of_the_wrong_kind_is_a_named_rejection() {
    let (model, points) = station_rig();
    // A `Float` handle naming the model's `Bool` well-full point — the
    // ids collide but the declared kinds do not.
    let mut dynamics = DynamicsBuilder::new();
    dynamics.integrator(foreign_f64(PointId(15)), points.level_primary, 0.0);
    let error = dynamics.emit(&model).unwrap_err();
    assert_eq!(
        error,
        DynamicsError::PointKind {
            element: 0,
            point: PointId(15),
            expected: ValueKind::Float,
            found: ValueKind::Bool,
        }
    );
    let message = error.to_string();
    assert!(message.contains("element 0"), "{message}");
    assert!(message.contains("point 15"), "{message}");
}

#[test]
fn declared_parameters_are_checked_at_emit() {
    let (model, points) = station_rig();
    let mut dynamics = DynamicsBuilder::new();
    dynamics.first_order_lag(points.net_flow, points.level_primary, 0.0, 3.5);
    let error = dynamics.emit(&model).unwrap_err();
    assert_eq!(
        error,
        DynamicsError::InvalidParameter {
            element: 0,
            point: PointId(10),
            parameter: "time_constant",
            rule: "finite and positive",
            value: 0.0,
        }
    );

    let mut dynamics = DynamicsBuilder::new();
    dynamics.threshold(points.level_backup, points.well_full, 4.8, 4.8, false);
    assert_eq!(
        dynamics.emit(&model).unwrap_err(),
        DynamicsError::DegenerateThreshold {
            element: 0,
            point: PointId(15),
            on: 4.8,
            off: 4.8,
        }
    );
}

#[test]
fn a_point_driven_twice_is_a_named_rejection() {
    let (model, points) = station_rig();
    let mut dynamics = DynamicsBuilder::new();
    dynamics
        .integrator(points.net_flow, points.level_primary, 3.5)
        .first_order_lag(points.net_flow, points.level_primary, 1.0, 0.0);
    assert_eq!(
        dynamics.emit(&model).unwrap_err(),
        DynamicsError::ConflictingDriver {
            element: 1,
            point: PointId(10),
        }
    );
}
