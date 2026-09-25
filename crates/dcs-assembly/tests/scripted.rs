//! Integration tests for the scripted simulated device kind: the
//! checked-in fixture mixing a `sim` device and a `sim-scripted` device
//! resolves both through the standard driver registry into one executor;
//! the scripted `In` points replay their declared scripts in tick order —
//! scripted non-`Good` quality included — and writes to the scripted
//! device's `Out` points are recorded and inspectable; malformed scripts
//! fail assembly with a named [`AssemblyError`] naming the device.

use dcs_assembly::{
    AssemblyError, BuildError, ComponentRegistry, DriverRegistry, FanoutDriver, SIM_SCRIPTED_KIND,
    assemble, resolve_drivers,
};
use dcs_blocks::{AnalogInput, DigitalOutput, Pid};
use dcs_core::{IoDriver, IoError, PointId, Quality, QualityReason, Sample, Tick, Value};
use dcs_model::{DeviceId, PlantModel};
use dcs_runtime::{Component, Executor};
use dcs_sim::ScriptedDriver;

/// The mixed-kind fixture: a local `sim` device (setpoint, pump-enable,
/// valve) and a `sim-scripted` device replaying `level-raw` and
/// `run-hours` while recording `pump-cmd` writes.
const SCRIPTED: &str = include_str!("../fixtures/scripted_kinds.json");
/// The malformed-script fixture: a `level-raw` script whose ticks are
/// not in increasing order.
const BAD_SCRIPT: &str = include_str!("../fixtures/invalid/bad_script.json");

const SETPOINT: PointId = PointId(10);
const LEVEL_RAW: PointId = PointId(11);
const VALVE: PointId = PointId(12);
const RUN_HOURS: PointId = PointId(13);
const PUMP_CMD: PointId = PointId(14);
const PUMP_ENABLE: PointId = PointId(15);
const SCRIPTED_DEVICE: DeviceId = DeviceId(2);

fn boxed<C, E>(result: Result<C, E>) -> Result<Box<dyn Component>, BuildError>
where
    C: Component + 'static,
    E: std::error::Error + 'static,
{
    result
        .map(|component| Box::new(component) as Box<dyn Component>)
        .map_err(BuildError::other)
}

/// The `dcs-blocks` registration for the kinds the fixture uses.
fn registry() -> ComponentRegistry {
    ComponentRegistry::new()
        .with(AnalogInput::<f64>::KIND, |spec| {
            boxed(AnalogInput::<f64>::from_parameters(
                spec.name.as_str(),
                spec.require("raw")?,
                spec.require("out")?,
                spec.parameters,
            ))
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
}

fn model(source: &str) -> PlantModel {
    PlantModel::load(source).unwrap()
}

/// Resolves and builds the fixture's driver side through the standard
/// registry.
fn build_driver(model: &PlantModel) -> FanoutDriver {
    resolve_drivers(model, &DriverRegistry::standard())
        .unwrap()
        .build()
        .unwrap()
}

/// Assembles the fixture's executor against `driver`.
fn build_executor<'d>(model: &PlantModel, driver: &'d FanoutDriver) -> Executor<'d> {
    assemble(model, &registry(), driver).unwrap()
}

#[test]
fn mixed_sim_and_scripted_kinds_assemble_and_scan_through_the_registry() {
    let model = model(SCRIPTED);
    let driver = build_driver(&model);
    let mut executor = build_executor(&model, &driver);

    // Both kinds resolved: the `sim` device's points live in the shared
    // local simulated map, the scripted device's points route to a
    // device backend — `sim-scripted` did not fall through to the `sim`
    // prefix despite starting with it.
    assert!(driver.sim().is_some());
    assert!(driver.backend(SCRIPTED_DEVICE).is_some());
    assert_eq!(
        driver.sim().unwrap().read(LEVEL_RAW),
        Err(IoError::UnknownPoint(LEVEL_RAW))
    );
    assert_eq!(
        driver.backend(SCRIPTED_DEVICE).unwrap().read(SETPOINT),
        Err(IoError::UnknownPoint(SETPOINT))
    );

    // The assembled executor scans: commands land on the local sim
    // backend, scripted inputs feed the loop, and the pump command
    // reaches the scripted device's Out point.
    driver.write(SETPOINT, Value::Float(50.0)).unwrap();
    driver.write(PUMP_ENABLE, Value::Bool(true)).unwrap();
    for _ in 0..15 {
        executor.scan();
        driver.step(0.1).unwrap();
    }
    assert!(
        executor
            .snapshot()
            .components
            .iter()
            .all(|component| component.step_errors == 0)
    );
    assert_eq!(driver.read(PUMP_CMD).unwrap().value, Value::Bool(true));
    // The PID's valve command landed on the local simulated channel.
    assert!(matches!(
        driver.sim().unwrap().read(VALVE).unwrap().value,
        Value::Float(_)
    ));
}

#[test]
fn scripted_inputs_replay_the_declared_script_in_tick_order() {
    let model = model(SCRIPTED);
    let driver = build_driver(&model);

    // The tick-0 entry is already in effect; the Int channel's first
    // entry is still ahead, so it holds its neutral initial value.
    assert_eq!(
        driver.read(LEVEL_RAW).unwrap(),
        Sample::good(Value::Float(4.0), Tick::ZERO)
    );
    assert_eq!(
        driver.read(RUN_HOURS).unwrap(),
        Sample::good(Value::Int(0), Tick::ZERO)
    );

    for _ in 0..2 {
        driver.step(0.1).unwrap();
    }
    assert_eq!(
        driver.read(RUN_HOURS).unwrap(),
        Sample::good(Value::Int(100), Tick(2))
    );

    for _ in 0..3 {
        driver.step(0.1).unwrap();
    }
    assert_eq!(
        driver.read(LEVEL_RAW).unwrap(),
        Sample::good(Value::Float(12.0), Tick(5))
    );

    for _ in 0..3 {
        driver.step(0.1).unwrap();
    }
    assert_eq!(
        driver.read(LEVEL_RAW).unwrap(),
        Sample::new(
            Value::Float(14.0),
            Quality::Uncertain(QualityReason::Stale),
            Tick(8)
        )
    );
    assert_eq!(
        driver.read(RUN_HOURS).unwrap(),
        Sample::new(
            Value::Int(200),
            Quality::Uncertain(QualityReason::Substituted),
            Tick(6)
        )
    );

    for _ in 0..2 {
        driver.step(0.1).unwrap();
    }
    assert_eq!(
        driver.read(LEVEL_RAW).unwrap(),
        Sample::new(
            Value::Float(0.0),
            Quality::Bad(QualityReason::DeviceFault),
            Tick(10)
        )
    );

    for _ in 0..2 {
        driver.step(0.1).unwrap();
    }
    // The script's fault lifted: the last entry holds Good.
    assert_eq!(
        driver.read(LEVEL_RAW).unwrap(),
        Sample::good(Value::Float(8.0), Tick(12))
    );
}

#[test]
fn scripted_playback_is_deterministic_across_identical_runs() {
    let run = || {
        let model = model(SCRIPTED);
        let driver = build_driver(&model);
        let mut executor = build_executor(&model, &driver);
        driver.write(SETPOINT, Value::Float(50.0)).unwrap();
        driver.write(PUMP_ENABLE, Value::Bool(true)).unwrap();
        for _ in 0..20 {
            executor.scan();
            driver.step(0.1).unwrap();
        }
        let snapshot = serde_json::to_string(&executor.snapshot()).unwrap();
        let writes = driver
            .inspect::<ScriptedDriver>(SCRIPTED_DEVICE)
            .unwrap()
            .writes();
        (snapshot, writes)
    };
    assert_eq!(run(), run());
}

#[test]
fn scripted_quality_fault_surfaces_as_non_good_snapshot_samples() {
    let model = model(SCRIPTED);
    let driver = build_driver(&model);
    let mut executor = build_executor(&model, &driver);

    // Scan 11 reads the point while the driver sits at tick 10 — the
    // script's `bad`/`device_fault` entry.
    for _ in 0..11 {
        executor.scan();
        driver.step(0.1).unwrap();
    }
    let sample = executor
        .snapshot()
        .points
        .iter()
        .find(|telemetry| telemetry.point == LEVEL_RAW)
        .and_then(|telemetry| telemetry.sample)
        .unwrap();
    assert_eq!(
        sample.quality,
        Quality::Bad(QualityReason::DeviceFault),
        "{sample:?}"
    );
    assert_eq!(sample.value, Value::Float(0.0));
}

#[test]
fn writes_to_the_scripted_out_channel_are_recorded_and_inspectable() {
    let model = model(SCRIPTED);
    let driver = build_driver(&model);
    let mut executor = build_executor(&model, &driver);
    driver.write(PUMP_ENABLE, Value::Bool(true)).unwrap();

    // Five scans: the digital-output block writes the pump command each
    // scan, and one direct write goes through the fan-out surface.
    for _ in 0..5 {
        executor.scan();
        driver.step(0.1).unwrap();
    }
    driver.write(PUMP_CMD, Value::Bool(false)).unwrap();

    let writes = driver
        .inspect::<ScriptedDriver>(SCRIPTED_DEVICE)
        .unwrap()
        .writes();
    assert_eq!(writes.len(), 6);
    for write in &writes[..5] {
        assert_eq!(write.point, PUMP_CMD);
        assert_eq!(write.channel.name, "pump-cmd");
        assert_eq!(write.value, Value::Bool(true));
    }
    assert_eq!(writes[5].value, Value::Bool(false));
    assert_eq!(writes[5].tick, Tick(5));

    // A wrong-kind write is refused and never reaches the log.
    assert!(matches!(
        driver.write(PUMP_CMD, Value::Float(1.0)),
        Err(IoError::TypeMismatch { .. })
    ));
    assert_eq!(
        driver
            .inspect::<ScriptedDriver>(SCRIPTED_DEVICE)
            .unwrap()
            .writes()
            .len(),
        6
    );
}

#[test]
fn non_monotonic_script_fixture_fails_assembly_naming_the_device() {
    let model = model(BAD_SCRIPT);
    let error = resolve_drivers(&model, &DriverRegistry::standard())
        .err()
        .unwrap();
    assert!(error.to_string().contains("device 2"));
    assert!(error.to_string().contains("\"sim-scripted\""));
    match error {
        AssemblyError::InvalidDeviceParameters {
            device,
            kind,
            detail,
        } => {
            assert_eq!(device, SCRIPTED_DEVICE);
            assert_eq!(kind, SIM_SCRIPTED_KIND);
            assert!(detail.contains("\"level-raw\""), "{detail}");
        }
        other => panic!("expected InvalidDeviceParameters, got {other:?}"),
    }
}

/// Mutates the fixture's `sim-scripted` device, asserts assembly fails
/// with [`AssemblyError::InvalidDeviceParameters`] naming the device and
/// kind, and returns the failure's detail.
fn expect_bad_script(mutate: impl FnOnce(&mut serde_json::Value)) -> String {
    let mut document: serde_json::Value = serde_json::from_str(SCRIPTED).unwrap();
    mutate(&mut document["devices"][1]);
    let model = model(&document.to_string());
    match resolve_drivers(&model, &DriverRegistry::standard())
        .err()
        .unwrap()
    {
        AssemblyError::InvalidDeviceParameters {
            device,
            kind,
            detail,
        } => {
            assert_eq!(device, SCRIPTED_DEVICE);
            assert_eq!(kind, SIM_SCRIPTED_KIND);
            detail
        }
        other => panic!("expected InvalidDeviceParameters, got {other:?}"),
    }
}

#[test]
fn malformed_scripts_fail_assembly_with_named_errors() {
    // An entry that is not an object.
    let detail = expect_bad_script(|device| {
        device["parameters"]["script"]["level-raw"] = serde_json::json!([4.0]);
    });
    assert!(detail.contains("level-raw"), "{detail}");
    // An entry missing its tick.
    let detail = expect_bad_script(|device| {
        device["parameters"]["script"]["level-raw"] = serde_json::json!([{ "value": 4.0 }]);
    });
    assert!(detail.contains("\"tick\""), "{detail}");
    // A non-integer tick.
    expect_bad_script(|device| {
        device["parameters"]["script"]["level-raw"] =
            serde_json::json!([{ "tick": 1.5, "value": 4.0 }]);
    });
    // A value kind mismatching its channel.
    let detail = expect_bad_script(|device| {
        device["parameters"]["script"]["level-raw"] =
            serde_json::json!([{ "tick": 0, "value": true }]);
    });
    assert!(detail.contains("Float"), "{detail}");
    // A script on an `out` channel.
    let detail = expect_bad_script(|device| {
        device["parameters"]["script"]["pump-cmd"] =
            serde_json::json!([{ "tick": 0, "value": true }]);
    });
    assert!(detail.contains("pump-cmd"), "{detail}");
    // A script naming a channel the device does not declare.
    expect_bad_script(|device| {
        device["parameters"]["script"]["no-such-channel"] =
            serde_json::json!([{ "tick": 0, "value": 1.0 }]);
    });
    // A `reason` on a `good` entry.
    expect_bad_script(|device| {
        device["parameters"]["script"]["level-raw"] =
            serde_json::json!([{ "tick": 0, "value": 4.0, "reason": "stale" }]);
    });
    // An unknown quality name.
    expect_bad_script(|device| {
        device["parameters"]["script"]["level-raw"] =
            serde_json::json!([{ "tick": 0, "value": 4.0, "quality": "lousy" }]);
    });
    // A missing script parameter.
    let detail = expect_bad_script(|device| {
        device["parameters"] = serde_json::json!({});
    });
    assert!(detail.contains("\"script\""), "{detail}");
    // An unknown parameter key.
    let detail = expect_bad_script(|device| {
        device["parameters"]["bogus"] = serde_json::json!(1);
    });
    assert!(detail.contains("\"bogus\""), "{detail}");
}
