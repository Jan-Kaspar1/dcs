//! The worked example: the tank-loop plant composed in Rust through the
//! typed seam, emitting byte-for-byte the checked-in reference fixture
//! `dcs-assembly` assembles.
//!
//! The fixture is the model the general-registry assembly tests run; an
//! emitted [`PlantModel`] equal to the loaded fixture loads, validates,
//! and assembles identically to it, because assembly is a pure function
//! of the document.

use dcs_build::specs::{AnalogInputSpec, PidSpec};
use dcs_build::{Direction, PlantBuilder, PointId, SignalId, Value, parameters};
use dcs_model::PlantModel;

/// The reference document `dcs-assembly`'s tests assemble.
const TANK_LOOP: &str = include_str!("../../dcs-assembly/fixtures/tank_loop.json");

/// The tank loop, composed: a writable setpoint and a raw level feed a
/// scaling block and a PID; the PID drives the inlet valve, and the raw
/// level also feeds the valve point directly.
fn tank_loop() -> PlantBuilder {
    let mut plant = PlantBuilder::new();

    // The simulated I/O device and its three channels.
    let sim = plant.device("sim").id;
    let setpoint = plant.channel::<f64>(sim, "setpoint", Direction::In);
    let level_raw = plant.channel::<f64>(sim, "level-raw", Direction::In);
    let valve = plant.channel::<f64>(sim, "valve", Direction::Out);

    // Logical I/O points bound to the channels. The setpoint is
    // operator-writable; the valve is the loop's field output.
    let sp = plant.field_input::<f64>(PointId(10), setpoint, true);
    let level = plant.field_input::<f64>(PointId(11), level_raw, false);
    let cmd = plant.field_output::<f64>(PointId(12), valve);

    // The monitoring names for the three points.
    plant
        .signal(SignalId(100), "level-setpoint", sp)
        .unit("m")
        .description("Tank level setpoint");
    plant
        .signal(SignalId(101), "tank-level-raw", level)
        .unit("mA")
        .description("Tank level raw measurement");
    plant
        .signal(SignalId(102), "valve-command", cmd)
        .unit("%")
        .description("Inlet valve command");

    // The control components, from their kind specs.
    let ai = plant.add(AnalogInputSpec::<f64>::new(parameters([
        ("raw_min", Value::Float(4.0)),
        ("raw_max", Value::Float(20.0)),
        ("eng_min", Value::Float(0.0)),
        ("eng_max", Value::Float(100.0)),
    ])));
    let pid = plant.add(PidSpec::new(parameters([
        ("kp", Value::Float(0.1)),
        ("ki", Value::Float(0.1)),
        ("kd", Value::Float(0.0)),
        ("dt", Value::Float(0.1)),
        ("out_min", Value::Float(4.0)),
        ("out_max", Value::Float(20.0)),
    ])));

    // The wiring: setpoint and scaled level into the PID, PID output to
    // the valve, and the raw level directly to the valve point.
    plant.connect(sp, pid.sp);
    plant.connect(level, ai.raw);
    plant.connect(ai.out, pid.pv);
    plant.connect(&pid.out, cmd);
    plant.connect(level, cmd);

    plant
}

#[test]
fn composition_emits_the_reference_document() {
    let built = tank_loop().build().unwrap();
    let reference = PlantModel::load(TANK_LOOP).unwrap();

    // The same document: same devices, points, signals, components,
    // connections — so a consumer cannot tell the composed model from
    // the hand-written one, and `dcs-assembly` resolves them identically.
    assert_eq!(built, reference);

    // The serialized documents are identical too, not merely equivalent.
    let emitted = serde_json::to_value(&built).unwrap();
    let checked_in = serde_json::from_str::<serde_json::Value>(TANK_LOOP).unwrap();
    assert_eq!(emitted, checked_in);
}

#[test]
fn emitted_document_serde_roundtrips_through_dcs_model() {
    let model = tank_loop().build().unwrap();
    let json = serde_json::to_string_pretty(&model).unwrap();
    let reloaded = PlantModel::load(&json).unwrap();
    assert_eq!(reloaded, model);
    assert_eq!(serde_json::to_string_pretty(&reloaded).unwrap(), json);
}
