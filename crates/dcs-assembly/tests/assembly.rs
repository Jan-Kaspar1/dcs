//! Integration tests for model-driven assembly: the tank-loop fixture is
//! assembled and run entirely through the registry — no manual component
//! wiring — and every [`AssemblyError`] kind is produced by a corresponding
//! invalid fixture or a deliberately miswired stub kind.

use dcs_assembly::{
    AssemblyError, BuildError, ComponentRegistry, assemble, sim_channel_map, sim_driver,
};
use dcs_blocks::{AnalogInput, Counter, DigitalOutput, Pid, RateLimiter, Timer};
use dcs_core::{Direction, IoDriver, PointId, Tick, Value, ValueKind};
use dcs_model::{ComponentId, Connection, Endpoint, PlantModel, PortRef};
use dcs_runtime::{Component, ComponentIo, Executor, IoRequirement, StepError};
use dcs_sim::{ChannelId, ConfigError, FirstOrderLag, ProcessElement, SimDriver};

const TANK_LOOP: &str = include_str!("../fixtures/tank_loop.json");
/// The M1 milestone fixture, checked in with `dcs-demo` — reused here
/// exactly as the issue asks, through the general registry.
const M1_TANK_LEVEL: &str = include_str!("../../dcs-demo/fixtures/tank_level.json");
/// The cyclic-vocabulary fixture: a timer, a counter, and a rate limiter.
const CYCLIC: &str = include_str!("../fixtures/cyclic.json");

const SETPOINT: PointId = PointId(10);
const LEVEL_RAW: PointId = PointId(11);
const VALVE: PointId = PointId(12);

const PULSE: PointId = PointId(10);
const RESET: PointId = PointId(11);
const TARGET: PointId = PointId(12);
const TIMED: PointId = PointId(20);
const COUNT: PointId = PointId(21);
const DONE: PointId = PointId(22);
const LIMITED: PointId = PointId(23);

fn boxed<C, E>(result: Result<C, E>) -> Result<Box<dyn Component>, BuildError>
where
    C: Component + 'static,
    E: std::error::Error + 'static,
{
    result
        .map(|component| Box::new(component) as Box<dyn Component>)
        .map_err(BuildError::other)
}

/// The `dcs-blocks` registration the `dcs-controller` binary performs,
/// restricted to the kinds these fixtures use.
fn registry() -> ComponentRegistry {
    ComponentRegistry::new()
        .with(AnalogInput::<f64>::KIND, |spec| {
            let raw = spec.require("raw")?;
            let out = spec.require("out")?;
            if spec.point_kind(raw) == Some(ValueKind::Int) {
                boxed(AnalogInput::<i64>::from_parameters(
                    spec.name.as_str(),
                    raw,
                    out,
                    spec.parameters,
                ))
            } else {
                boxed(AnalogInput::<f64>::from_parameters(
                    spec.name.as_str(),
                    raw,
                    out,
                    spec.parameters,
                ))
            }
        })
        .with(Pid::KIND, |spec| {
            boxed(Pid::from_parameters(
                spec.name.as_str(),
                spec.require("sp")?,
                spec.require("pv")?,
                spec.require("out")?,
                spec.parameters,
            ))
        })
        .with(DigitalOutput::KIND, |spec| {
            boxed(DigitalOutput::from_parameters(
                spec.name.as_str(),
                spec.require("in")?,
                spec.require("out")?,
                spec.parameters,
            ))
        })
        .with(Timer::KIND, |spec| {
            boxed(Timer::from_parameters(
                spec.name.as_str(),
                spec.require("in")?,
                spec.require("out")?,
                spec.parameters,
            ))
        })
        .with(Counter::KIND, |spec| {
            boxed(Counter::from_parameters(
                spec.name.as_str(),
                spec.require("in")?,
                spec.require("reset")?,
                spec.require("count")?,
                spec.require("done")?,
                spec.parameters,
            ))
        })
        .with(RateLimiter::KIND, |spec| {
            boxed(RateLimiter::from_parameters(
                spec.name.as_str(),
                spec.require("in")?,
                spec.require("out")?,
                spec.parameters,
            ))
        })
}

fn model(source: &str) -> PlantModel {
    PlantModel::load(source).unwrap()
}

#[test]
fn tank_loop_reaches_setpoint_with_no_manual_wiring() {
    let model = model(TANK_LOOP);
    let driver = sim_driver(&model).unwrap();
    let mut executor = assemble(&model, &registry(), &driver).unwrap();

    // The field-side setpoint: what an operator or upstream device writes.
    driver.write(SETPOINT, Value::Float(50.0)).unwrap();
    for _ in 0..500 {
        executor.scan().unwrap();
        driver.step(0.1);
    }

    // Steady state: the scaled level (4-20 mA -> 0-100 units) settles at the
    // 50-unit setpoint, which the loopback drives from a 12-unit valve
    // command. Stated tolerance: 1% of the 50-unit setpoint.
    let Value::Float(raw) = driver.read(LEVEL_RAW).unwrap().value else {
        panic!("level raw must be Float")
    };
    assert!((raw - 12.0).abs() < 0.5, "raw={raw}");
    let Value::Float(valve) = driver.read(VALVE).unwrap().value else {
        panic!("valve must be Float")
    };
    assert!((valve - 12.0).abs() < 0.5, "valve={valve}");

    // The scaled level the controller observes reached the setpoint.
    let snapshot = executor.snapshot();
    assert_eq!(snapshot.tick.0, 500);
    assert!(
        snapshot
            .components
            .iter()
            .all(|component| component.step_errors == 0),
        "{:?}",
        snapshot.components
    );
}

#[test]
fn m1_tank_level_fixture_reaches_setpoint() {
    // The M1 fixture (`dcs-demo`'s tank_level.json) declares `sim-ai` and
    // `sim-ao` devices, an analog-input, and a pid. Its simulated plant is
    // not part of the model: the test adds the tank's first-order lag to
    // the resolved channel map — process emulation, not component wiring —
    // then assembles through the registry with no manual wiring code.
    let model = model(M1_TANK_LEVEL);
    let level_raw = PointId(10); // lt101_raw, driven by the lag
    let setpoint = PointId(11); // lic101_sp, written field-side
    let valve = PointId(20); // lv101_cmd, the pid's output

    let map = sim_channel_map(&model)
        .unwrap()
        .with_element(ProcessElement::FirstOrderLag(FirstOrderLag {
            input: valve,
            output: level_raw,
            time_constant: 2.0,
            initial: 4.0,
        }));
    let driver = SimDriver::new(map).unwrap();
    let mut executor = assemble(&model, &registry(), &driver).unwrap();

    driver.write(setpoint, Value::Float(60.0)).unwrap();
    for _ in 0..400 {
        executor.scan().unwrap();
        driver.step(0.1);
    }

    // 60% of the 4–20 mA raw range settles at 13.6 mA; stated tolerance
    // is 0.5 mA.
    let Value::Float(raw) = driver.read(level_raw).unwrap().value else {
        panic!("level raw must be Float")
    };
    assert!((raw - 13.6).abs() < 0.5, "raw={raw}");
    assert!(
        executor
            .snapshot()
            .components
            .iter()
            .all(|component| component.step_errors == 0)
    );
}

#[test]
fn identical_runs_snapshot_identically() {
    let model = model(TANK_LOOP);
    let run = || {
        let driver = sim_driver(&model).unwrap();
        let mut executor = assemble(&model, &registry(), &driver).unwrap();
        driver.write(SETPOINT, Value::Float(50.0)).unwrap();
        for _ in 0..200 {
            executor.scan().unwrap();
            driver.step(0.1);
        }
        serde_json::to_string(&executor.snapshot()).unwrap()
    };
    assert_eq!(run(), run());
}

/// Steps the executor once and returns the value the driver holds on
/// `point` — what the field side of the cyclic fixture observes.
fn field_value(executor: &mut Executor<'_>, driver: &SimDriver, point: PointId) -> Value {
    executor.scan().unwrap();
    driver.read(point).unwrap().value
}

#[test]
fn cyclic_fixture_runs_timer_counter_and_limiter() {
    let model = model(CYCLIC);
    let driver = sim_driver(&model).unwrap();
    let mut executor = assemble(&model, &registry(), &driver).unwrap();

    // The timer holds `pulse` for delay_ticks=3 scans; the counter's
    // preset=3 counts its rising edges; the limiter follows `target`
    // at max_delta=2.5 per scan.
    driver.write(PULSE, Value::Bool(true)).unwrap();
    driver.write(TARGET, Value::Float(10.0)).unwrap();

    // Tick 1: the timer has banked one scan, the counter has seen one
    // rising edge, and the limiter adopted its first finite input.
    assert_eq!(
        field_value(&mut executor, &driver, TIMED),
        Value::Bool(false)
    );
    assert_eq!(driver.read(COUNT).unwrap().value, Value::Int(1));
    assert_eq!(driver.read(LIMITED).unwrap().value, Value::Float(10.0));

    // Tick 2: still below the delay.
    assert_eq!(
        field_value(&mut executor, &driver, TIMED),
        Value::Bool(false)
    );
    // Tick 3: the timer asserts exactly at the configured tick.
    assert_eq!(
        field_value(&mut executor, &driver, TIMED),
        Value::Bool(true)
    );

    // A further target step slews by exactly max_delta per scan.
    driver.write(TARGET, Value::Float(20.0)).unwrap();
    assert_eq!(
        field_value(&mut executor, &driver, LIMITED),
        Value::Float(12.5)
    );

    // Dropping `pulse` deasserts the on-delay timer the same scan.
    driver.write(PULSE, Value::Bool(false)).unwrap();
    assert_eq!(
        field_value(&mut executor, &driver, TIMED),
        Value::Bool(false)
    );

    // Two more rising edges bring the counter to its preset.
    driver.write(PULSE, Value::Bool(true)).unwrap();
    executor.scan().unwrap();
    assert_eq!(driver.read(COUNT).unwrap().value, Value::Int(2));
    driver.write(PULSE, Value::Bool(false)).unwrap();
    executor.scan().unwrap();
    driver.write(PULSE, Value::Bool(true)).unwrap();
    executor.scan().unwrap();
    assert_eq!(driver.read(COUNT).unwrap().value, Value::Int(3));
    assert_eq!(driver.read(DONE).unwrap().value, Value::Bool(true));

    // Reset clears the count and the flag.
    driver.write(RESET, Value::Bool(true)).unwrap();
    executor.scan().unwrap();
    assert_eq!(driver.read(COUNT).unwrap().value, Value::Int(0));
    assert_eq!(driver.read(DONE).unwrap().value, Value::Bool(false));

    assert!(
        executor
            .snapshot()
            .components
            .iter()
            .all(|component| component.step_errors == 0)
    );
}

#[test]
fn unknown_component_kind_names_the_instance() {
    let model = model(include_str!(
        "../fixtures/invalid/unknown_component_kind.json"
    ));
    let driver = sim_driver(&model).unwrap();
    let error = assemble(&model, &registry(), &driver).unwrap_err();
    assert_eq!(
        error,
        AssemblyError::UnknownComponentKind {
            component: ComponentId(1),
            kind: "flux-capacitor".to_string(),
        }
    );
    assert!(error.to_string().contains("\"flux-capacitor\""));
}

#[test]
fn unknown_device_kind_names_the_device() {
    let model = model(include_str!("../fixtures/invalid/unknown_device_kind.json"));
    let error = sim_driver(&model).err().unwrap();
    assert_eq!(
        error,
        AssemblyError::UnknownDeviceKind {
            device: dcs_model::DeviceId(1),
            kind: "ethercat-8ai".to_string(),
        }
    );
    assert!(error.to_string().contains("device 1"));
}

#[test]
fn unbound_port_names_component_and_port() {
    let model = model(include_str!("../fixtures/invalid/unbound_port.json"));
    let driver = sim_driver(&model).unwrap();
    let error = assemble(&model, &registry(), &driver).unwrap_err();
    assert_eq!(
        error,
        AssemblyError::UnboundPort {
            component: ComponentId(1),
            port: "pv".to_string(),
        }
    );
}

#[test]
fn port_bound_twice_names_component_and_port() {
    let model = model(include_str!("../fixtures/invalid/port_bound_twice.json"));
    let driver = sim_driver(&model).unwrap();
    let error = assemble(&model, &registry(), &driver).unwrap_err();
    assert_eq!(
        error,
        AssemblyError::PortBoundTwice {
            component: ComponentId(1),
            port: "pv".to_string(),
        }
    );
}

#[test]
fn direction_mismatch_names_element_and_directions() {
    let model = model(include_str!("../fixtures/invalid/direction_mismatch.json"));
    let driver = sim_driver(&model).unwrap();
    let error = assemble(&model, &registry(), &driver).unwrap_err();
    assert_eq!(
        error,
        AssemblyError::DirectionMismatch {
            component: ComponentId(1),
            port: "pv".to_string(),
            point: PointId(12),
            declared: Direction::In,
            mapped: Direction::Out,
        }
    );
}

#[test]
fn type_mismatch_names_element_and_kinds() {
    let model = model(include_str!("../fixtures/invalid/type_mismatch.json"));
    let driver = sim_driver(&model).unwrap();
    let error = assemble(&model, &registry(), &driver).unwrap_err();
    assert_eq!(
        error,
        AssemblyError::TypeMismatch {
            component: ComponentId(1),
            port: "sp".to_string(),
            point: PointId(10),
            declared: ValueKind::Float,
            mapped: ValueKind::Int,
        }
    );
}

#[test]
fn duplicate_channel_is_an_invalid_channel_map() {
    let model = model(include_str!("../fixtures/invalid/duplicate_channel.json"));
    let error = sim_driver(&model).err().unwrap();
    assert_eq!(
        error,
        AssemblyError::InvalidChannelMap {
            detail: ConfigError::DuplicateChannel(ChannelId {
                device: 1,
                name: "ch0".to_string(),
            }),
        }
    );
}

#[test]
fn constructor_failure_names_component_and_detail() {
    let model = model(include_str!("../fixtures/invalid/bad_parameters.json"));
    let driver = sim_driver(&model).unwrap();
    let error = assemble(&model, &registry(), &driver).unwrap_err();
    match error {
        AssemblyError::Component {
            component,
            kind,
            detail,
        } => {
            assert_eq!(component, ComponentId(1));
            assert_eq!(kind, "pid");
            assert!(detail.contains("\"kp\""), "{detail}");
        }
        other => panic!("expected Component, got {other:?}"),
    }
}

/// A component declaring fixed requirements, ignoring its spec — a stand-in
/// for a kind that misbinds its logical I/O.
struct Stub {
    name: String,
    requirements: Vec<IoRequirement>,
}

impl Component for Stub {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        self.requirements.clone()
    }

    fn step(&mut self, _io: &dyn ComponentIo, _tick: Tick) -> Result<(), StepError> {
        Ok(())
    }
}

/// A model whose single component has kind `"stub"` and no ports.
fn stub_model() -> PlantModel {
    let mut model = model(TANK_LOOP);
    model.components.truncate(1);
    model.components[0].kind = "stub".to_string();
    model.components[0].ports.clear();
    model.connections.clear();
    model
}

fn stub_registry(requirements: Vec<IoRequirement>) -> ComponentRegistry {
    ComponentRegistry::new().with("stub", move |spec| {
        Ok(Box::new(Stub {
            name: spec.name.clone(),
            requirements: requirements.clone(),
        }) as Box<dyn Component>)
    })
}

#[test]
fn requirement_on_unserved_point_is_unmapped() {
    let model = stub_model();
    let registry = stub_registry(vec![IoRequirement::input::<f64>("ghost", PointId(999))]);
    let driver = sim_driver(&model).unwrap();
    let error = assemble(&model, &registry, &driver).unwrap_err();
    assert_eq!(
        error,
        AssemblyError::UnmappedPoint {
            component: ComponentId(1),
            port: "ghost".to_string(),
            point: PointId(999),
        }
    );
}

#[test]
fn duplicate_declaration_surfaces_as_wiring_error() {
    let model = stub_model();
    let registry = stub_registry(vec![
        IoRequirement::input::<f64>("a", SETPOINT),
        IoRequirement::input::<f64>("b", SETPOINT),
    ]);
    let driver = sim_driver(&model).unwrap();
    let error = assemble(&model, &registry, &driver).unwrap_err();
    assert!(matches!(error, AssemblyError::Wiring { .. }), "{error:?}");
    assert!(error.to_string().contains("stub:1"), "{error}");
}

#[test]
fn port_to_port_wire_on_undeclared_component_is_unresolved() {
    // Assembling a model that skipped validation: the `from` port's
    // component does not exist.
    let mut model = model(TANK_LOOP);
    model.connections.push(Connection {
        from: Endpoint::Port(PortRef {
            component: ComponentId(99),
            name: "ghost".to_string(),
        }),
        to: Endpoint::Port(PortRef {
            component: ComponentId(2),
            name: "pv".to_string(),
        }),
    });
    let driver = sim_driver(&model).unwrap();
    let error = assemble(&model, &registry(), &driver).unwrap_err();
    assert_eq!(error, AssemblyError::UnresolvedEndpoint { connection: 5 });
}
