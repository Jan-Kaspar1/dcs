//! Scratch: trace the dynamics trajectory with the threshold element.

use dcs_assembly::{DriverRegistry, resolve_drivers};
use dcs_build::ijmuiden::{IjmuidenConfig, ijmuiden, points};
use dcs_build::Value;
use dcs_core::IoDriver;
use dcs_model::PlantModel;
use dcs_sim::{Fault, ProcessElement};
use dcs_core::Quality;

const MODEL_JSON: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/ijmuiden.json"
);
const DYNAMICS_JSON: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/ijmuiden_dynamics.json"
);

#[test]
fn trace() {
    let model = PlantModel::load(&std::fs::read_to_string(MODEL_JSON).unwrap()).unwrap();
    let _ = ijmuiden(&IjmuidenConfig::reference()).unwrap();
    let elements: Vec<ProcessElement> =
        serde_json::from_str(&std::fs::read_to_string(DYNAMICS_JSON).unwrap()).unwrap();
    let mut plan = resolve_drivers(&model, &DriverRegistry::standard()).unwrap();
    for element in elements {
        plan.sim_map = plan.sim_map.with_element(element);
        plan.sim_map.validate().unwrap();
    }
    let driver = plan.build().unwrap();
    let sim = driver.sim().unwrap();
    sim.write(points::INFLOW, Value::Float(0.08)).unwrap();
    sim.write(points::GATE_FB, Value::Float(1.0)).unwrap();
    driver.step(1.0).unwrap();
    let f = |p: dcs_build::PointId| match sim.read(p) {
        Ok(s) => format!("{:.2}q{:?}", match s.value {
            Value::Float(v) => v,
            other => panic!("{other:?}"),
        }, s.quality),
        Err(e) => format!("ERR({e:?})"),
    };
    let b = |p: dcs_build::PointId| match sim.read(p) {
        Ok(s) => format!("{}q{:?}", match s.value {
            Value::Bool(v) => v,
            other => panic!("{other:?}"),
        }, s.quality),
        Err(e) => format!("ERR({e:?})"),
    };
    for tick in 2..=60u64 {
        if tick == 22 {
            sim.inject_fault(points::LEVEL, Fault::Disconnected).unwrap();
        }
        if tick == 27 {
            sim.clear_fault(points::LEVEL).unwrap();
        }
        driver.step(1.0).unwrap();
        println!(
            "tick {tick}: level={} sis_active={} sis_draw={} net={}",
            f(points::LEVEL),
            b(points::SIS_ACTIVE),
            f(points::SIS_DRAW),
            f(points::NET_FLOW),
        );
        let _ = Quality::Good;
    }
}
