//! Integration tests for the register-mapped `sim-bus` device kind: the
//! checked-in fixture mixing a `sim` device and a `sim-bus` device
//! resolves both through the standard driver registry into one executor
//! whose points route to the correct backend; the mixed run produces
//! outputs identical to the equivalent all-local reference; server loss
//! surfaces named [`IoError`]s rather than panics; and malformed
//! register parameters fail assembly with a named [`AssemblyError`]
//! naming the device.

use dcs_assembly::{
    AssemblyError, BuildError, ComponentRegistry, DriverRegistry, FanoutDriver, SIM_BUS_KIND,
    StepError, assemble, resolve_drivers, sim_driver,
};
use dcs_blocks::{AnalogInput, Pid};
use dcs_core::{
    DriverDiagnostics, IoDriver, IoError, LinkState, PointId, Quality, QualityReason,
    TelemetrySnapshot, Value, ValueKind,
};
use dcs_model::{DeviceId, PlantModel};
use dcs_runtime::Component;
use dcs_sim_bus::{BusDriver, BusServer, PointRegister, RegisterBank, RegisterDecl};
use std::net::{SocketAddr, TcpListener};
use std::thread;

/// The mixed-kind fixture: a local `sim` device (setpoint, valve) and a
/// `sim-bus` device whose `level-raw` channel lives at register 4 — the
/// `address` parameter is the `__BUS_ADDR__` placeholder tests
/// substitute a live server for.
const MIXED_BUS: &str = include_str!("../fixtures/mixed_bus.json");
/// The all-local reference: the same points, components, and wiring on a
/// single `sim` device.
const TANK_LOOP: &str = include_str!("../fixtures/tank_loop.json");
/// The malformed-register fixture: `level-raw` mapped to a register
/// address outside the `u16` range.
const BAD_BUS: &str = include_str!("../fixtures/invalid/bad_sim_bus_parameters.json");

const SETPOINT: PointId = PointId(10);
const LEVEL_RAW: PointId = PointId(11);
const VALVE: PointId = PointId(12);
const BUS_DEVICE: DeviceId = DeviceId(2);
/// The register address the fixture maps `level-raw` to.
const LEVEL_REGISTER: u16 = 4;

fn boxed<C, E>(result: Result<C, E>) -> Result<Box<dyn Component>, BuildError>
where
    C: Component + 'static,
    E: std::error::Error + 'static,
{
    result
        .map(|component| Box::new(component) as Box<dyn Component>)
        .map_err(BuildError::other)
}

/// The `dcs-blocks` registration for the kinds the fixtures use.
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
}

fn model(source: &str) -> PlantModel {
    PlantModel::load(source).unwrap()
}

/// The register bank the fixture's device serves: `level-raw` at
/// register 4, powered on at 0.0 like the local-sim initial.
fn device_bank() -> RegisterBank {
    RegisterBank::new([RegisterDecl {
        register: LEVEL_REGISTER,
        initial: Value::Float(0.0),
    }])
    .unwrap()
}

/// `shutdown` on drop, so a panicking test still lets the scoped serve
/// thread exit instead of hanging the scope's join.
struct ShutdownOnDrop<'s>(&'s BusServer);

impl Drop for ShutdownOnDrop<'_> {
    fn drop(&mut self) {
        self.0.shutdown();
    }
}

/// Serves `bank`'s registers on an ephemeral loopback port for the
/// duration of `test`, then shuts the server down and joins its accept
/// thread.
fn with_server<R>(bank: RegisterBank, test: impl FnOnce(&BusServer, SocketAddr) -> R) -> R {
    let server = BusServer::bind(("127.0.0.1", 0), bank).unwrap();
    let addr = server.local_addr().unwrap();
    thread::scope(|scope| {
        scope.spawn(|| server.serve());
        let _guard = ShutdownOnDrop(&server);
        test(&server, addr)
    })
}

/// The mixed-kind model with a live server address substituted in.
fn mixed_model(addr: SocketAddr) -> PlantModel {
    model(&MIXED_BUS.replace("__BUS_ADDR__", &addr.to_string()))
}

/// Resolves and builds the fixture's driver side through the standard
/// registry.
fn build_driver(model: &PlantModel) -> FanoutDriver {
    resolve_drivers(model, &DriverRegistry::standard())
        .unwrap()
        .build()
        .unwrap()
}

#[test]
fn mixed_sim_and_bus_kinds_assemble_scan_and_route() {
    with_server(device_bank(), |server, addr| {
        let model = mixed_model(addr);
        let driver = build_driver(&model);
        let mut executor = assemble(&model, &registry(), &driver).unwrap();

        // Both kinds resolved: the `sim` device's points live in the
        // shared local simulated map, the bus device's point routes to a
        // device backend — `sim-bus` did not fall through to the `sim`
        // prefix despite starting with it.
        assert!(driver.sim().is_some());
        assert!(driver.backend(BUS_DEVICE).is_some());
        assert_eq!(
            driver.sim().unwrap().read(LEVEL_RAW),
            Err(IoError::UnknownPoint(LEVEL_RAW))
        );
        assert_eq!(
            driver.backend(BUS_DEVICE).unwrap().read(VALVE),
            Err(IoError::UnknownPoint(VALVE))
        );
        // The register-mapped kind is field-facing — a redundant pair's
        // write gate treats its points as the shared field.
        assert!(driver.is_field_point(LEVEL_RAW));
        assert!(!driver.is_field_point(SETPOINT));

        // The point space is one surface: the setpoint write lands on
        // the local simulated backend, a level write lands on the
        // device register, and the level read comes back over the
        // register protocol.
        driver.write(SETPOINT, Value::Float(50.0)).unwrap();
        assert_eq!(
            driver.sim().unwrap().read(SETPOINT).unwrap().value,
            Value::Float(50.0)
        );
        driver.write(LEVEL_RAW, Value::Float(8.5)).unwrap();
        assert_eq!(
            server.bank().read(LEVEL_REGISTER).unwrap().value,
            Value::Float(8.5)
        );
        server
            .bank()
            .write(LEVEL_REGISTER, Value::Float(6.5))
            .unwrap();
        assert_eq!(driver.read(LEVEL_RAW).unwrap().value, Value::Float(6.5));

        // The cross-backend field wire carries the valve command onto
        // the remote level register at each step, so the loop closes
        // over the register protocol.
        for _ in 0..400 {
            executor.scan().unwrap();
            driver.step(0.1).unwrap();
        }
        let Value::Float(valve) = driver.read(VALVE).unwrap().value else {
            panic!("valve must be Float")
        };
        assert!((valve - 12.0).abs() < 0.5, "valve={valve}");
        let Value::Float(level) = driver.read(LEVEL_RAW).unwrap().value else {
            panic!("level raw must be Float")
        };
        assert!((level - 12.0).abs() < 0.5, "level={level}");
        assert!(
            executor
                .snapshot()
                .components
                .iter()
                .all(|component| component.step_errors == 0)
        );
    });
}

#[test]
fn the_bus_backend_arbitrates_the_single_writer_claim() {
    with_server(device_bank(), |_, addr| {
        let model = mixed_model(addr);
        let driver = build_driver(&model);

        // The register-mapped kind arbitrates through the device
        // server's claim: no unfenceable field-facing device remains,
        // so a model built on it may arm automatic failover — while the
        // manual promotion path, which runs the same claim before the
        // gate lifts, is unaffected.
        assert!(driver.is_field_point(LEVEL_RAW));
        assert!(driver.unfenced_field_devices().is_empty());

        // The promotion path's claim lands on the device server: the
        // claimed fan-out keeps writing while a second attachment's
        // register writes are refused with the named fenced error.
        driver.claim_field_writer(7).unwrap();
        driver.write(LEVEL_RAW, Value::Float(8.5)).unwrap();
        let fenced = BusDriver::connect(
            addr,
            &[PointRegister {
                point: PointId(11),
                register: LEVEL_REGISTER,
                kind: ValueKind::Float,
            }],
        )
        .unwrap();
        assert_eq!(
            fenced.write(PointId(11), Value::Float(0.0)),
            Err(IoError::Fenced(PointId(11)))
        );
        // Reads stay open to the fenced attachment.
        assert_eq!(fenced.read(PointId(11)).unwrap().value, Value::Float(8.5));
    });
}

#[test]
fn sim_bus_run_matches_the_all_local_reference() {
    // The single-backend reference: the all-`sim` tank-loop model
    // through `sim_driver`, exactly as before the registry existed.
    let reference = {
        let model = model(TANK_LOOP);
        let driver = sim_driver(&model).unwrap();
        let mut executor = assemble(&model, &registry(), &driver).unwrap();
        driver.write(SETPOINT, Value::Float(50.0)).unwrap();
        for _ in 0..200 {
            executor.scan().unwrap();
            driver.step(0.1);
        }
        serde_json::to_string(&executor.snapshot()).unwrap()
    };

    // The same loop split across the local `sim` backend and the
    // register-mapped device; the fan-out routes each point to its
    // owner and the cross-backend wire carries the valve command into
    // the remote level register.
    let mixed = with_server(device_bank(), |_, addr| {
        let model = mixed_model(addr);
        let driver = build_driver(&model);
        let mut executor = assemble(&model, &registry(), &driver).unwrap();
        driver.write(SETPOINT, Value::Float(50.0)).unwrap();
        for _ in 0..200 {
            executor.scan().unwrap();
            driver.step(0.1).unwrap();
        }
        executor.snapshot()
    });

    // The mixed run's bus backend reports its link — the live
    // connection the reference's all-local driver has no transport to
    // report on. That is the one legitimate difference between the two
    // snapshots' health sections; every other field, including the
    // executor-collected counters, is identical.
    assert_eq!(
        mixed.io_health.driver,
        Some(DriverDiagnostics {
            link: LinkState::Connected,
            last_error: None,
            exchange: None,
        })
    );
    let mut normalized = mixed;
    normalized.io_health.driver = None;
    assert_eq!(
        serde_json::from_str::<TelemetrySnapshot>(&reference).unwrap(),
        normalized
    );
}

#[test]
fn two_identical_sim_bus_runs_produce_identical_snapshots() {
    let run = || {
        with_server(device_bank(), |_, addr| {
            let model = mixed_model(addr);
            let driver = build_driver(&model);
            let mut executor = assemble(&model, &registry(), &driver).unwrap();
            driver.write(SETPOINT, Value::Float(50.0)).unwrap();
            for _ in 0..50 {
                executor.scan().unwrap();
                driver.step(0.1).unwrap();
            }
            serde_json::to_string(&executor.snapshot()).unwrap()
        })
    };
    assert_eq!(run(), run());
}

#[test]
fn server_loss_surfaces_named_io_errors_and_degrades_the_scan() {
    let server = BusServer::bind(("127.0.0.1", 0), device_bank()).unwrap();
    let addr = server.local_addr().unwrap();
    thread::scope(|scope| {
        scope.spawn(|| server.serve());
        let model = mixed_model(addr);
        let driver = build_driver(&model);
        let mut executor = assemble(&model, &registry(), &driver).unwrap();
        executor.scan().unwrap();
        assert_eq!(
            executor
                .snapshot()
                .points
                .iter()
                .find(|telemetry| telemetry.point == LEVEL_RAW)
                .and_then(|telemetry| telemetry.sample)
                .unwrap()
                .quality,
            Quality::Good
        );
        // While the device server is live the fan-out's aggregate
        // reports the bus backend's link as connected, no last error.
        assert_eq!(
            executor.snapshot().io_health.driver,
            Some(DriverDiagnostics {
                link: LinkState::Connected,
                last_error: None,
                exchange: None,
            })
        );

        server.shutdown();
        // The lost device surfaces named IoErrors through the fan-out —
        // at the point the caller addressed — and the link-health hook
        // on the inspectable driver reports the failure.
        assert_eq!(
            driver.read(LEVEL_RAW),
            Err(IoError::Disconnected(LEVEL_RAW))
        );
        assert_eq!(
            driver.write(LEVEL_RAW, Value::Float(1.0)),
            Err(IoError::Disconnected(LEVEL_RAW))
        );
        let bus = driver.inspect::<BusDriver>(BUS_DEVICE).unwrap();
        assert!(!bus.connected());
        assert!(bus.last_failure().is_some());
        // The fan-out's aggregate names the dead backend's last failure
        // by the device it serves.
        assert_eq!(
            driver.diagnostics(),
            Some(DriverDiagnostics {
                link: LinkState::Disconnected,
                last_error: Some("device 2: no live connection to the device server".to_string()),
                exchange: None,
            })
        );
        // Stepping fails on the cross-backend wire first: the route's
        // write to the dead register surfaces the same named IoError.
        assert_eq!(
            driver.step(0.1),
            Err(StepError::Route(IoError::Disconnected(LEVEL_RAW)))
        );

        // The local simulated backend is unaffected, and the scan
        // degrades the remote input to Bad rather than panicking.
        driver.write(SETPOINT, Value::Float(40.0)).unwrap();
        executor.scan().unwrap();
        let snapshot = executor.snapshot();
        let sample = snapshot
            .points
            .iter()
            .find(|telemetry| telemetry.point == LEVEL_RAW)
            .and_then(|telemetry| telemetry.sample)
            .unwrap();
        assert_eq!(
            sample.quality,
            Quality::Bad(QualityReason::CommunicationFault)
        );
        // The same dead link reaches the snapshot's I/O-health driver
        // section through the aggregate — the link-level report beside
        // the per-point quality the event left on the input sample.
        assert_eq!(
            snapshot.io_health.driver,
            Some(DriverDiagnostics {
                link: LinkState::Disconnected,
                last_error: Some("device 2: no live connection to the device server".to_string()),
                exchange: None,
            })
        );
    });
}

#[test]
fn malformed_register_map_fixture_fails_assembly_naming_the_device() {
    let model = model(BAD_BUS);
    let error = resolve_drivers(&model, &DriverRegistry::standard())
        .err()
        .unwrap();
    assert!(error.to_string().contains("device 2"));
    assert!(error.to_string().contains("\"sim-bus\""));
    match error {
        AssemblyError::InvalidDeviceParameters {
            device,
            kind,
            detail,
        } => {
            assert_eq!(device, BUS_DEVICE);
            assert_eq!(kind, SIM_BUS_KIND);
            assert!(detail.contains("level-raw"), "{detail}");
        }
        other => panic!("expected InvalidDeviceParameters, got {other:?}"),
    }
}

/// Mutates the fixture's `sim-bus` device, asserts assembly fails with
/// [`AssemblyError::InvalidDeviceParameters`] naming the device and
/// kind, and returns the failure's detail.
fn expect_bad_parameters(mutate: impl FnOnce(&mut serde_json::Value)) -> String {
    let mut document: serde_json::Value = serde_json::from_str(MIXED_BUS).unwrap();
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
            assert_eq!(device, BUS_DEVICE);
            assert_eq!(kind, SIM_BUS_KIND);
            detail
        }
        other => panic!("expected InvalidDeviceParameters, got {other:?}"),
    }
}

#[test]
fn malformed_sim_bus_parameters_fail_assembly_with_named_errors() {
    // A missing register map.
    let detail = expect_bad_parameters(|device| {
        device["parameters"] = serde_json::json!({ "address": "127.0.0.1:1" });
    });
    assert!(detail.contains("\"registers\""), "{detail}");
    // A register map that is not an object.
    expect_bad_parameters(|device| {
        device["parameters"]["registers"] = serde_json::json!([4]);
    });
    // A register map not covering a declared channel.
    let detail = expect_bad_parameters(|device| {
        device["parameters"]["registers"] = serde_json::json!({});
    });
    assert!(detail.contains("level-raw"), "{detail}");
    // A register map naming a channel the device does not declare.
    let detail = expect_bad_parameters(|device| {
        device["parameters"]["registers"] =
            serde_json::json!({ "level-raw": 4, "no-such-channel": 5 });
    });
    assert!(detail.contains("no-such-channel"), "{detail}");
    // Two channels sharing one register.
    let detail = expect_bad_parameters(|device| {
        device["channels"]["extra"] =
            serde_json::json!({ "direction": "in", "value_type": "float" });
        device["parameters"]["registers"] = serde_json::json!({ "level-raw": 4, "extra": 4 });
    });
    assert!(detail.contains("share register 4"), "{detail}");
    // A register address outside the u16 range.
    let detail = expect_bad_parameters(|device| {
        device["parameters"]["registers"] = serde_json::json!({ "level-raw": 70000 });
    });
    assert!(detail.contains("65535"), "{detail}");
    // A non-integer register.
    expect_bad_parameters(|device| {
        device["parameters"]["registers"] = serde_json::json!({ "level-raw": "four" });
    });
    // An initial whose kind disagrees with the channel.
    let detail = expect_bad_parameters(|device| {
        device["parameters"]["registers"] =
            serde_json::json!({ "level-raw": { "register": 4, "initial": { "bool": true } } });
    });
    assert!(detail.contains("Float"), "{detail}");
    // An unknown parameter key.
    let detail = expect_bad_parameters(|device| {
        device["parameters"]["bogus"] = serde_json::json!(1);
    });
    assert!(detail.contains("\"bogus\""), "{detail}");
    // An address that does not resolve.
    expect_bad_parameters(|device| {
        device["parameters"]["address"] = serde_json::json!("not a socket address");
    });
}

#[test]
fn an_unreachable_sim_bus_backend_fails_before_any_scan() {
    // A guaranteed-dead address: bind once to learn a free port, then
    // drop the listener so the connect is refused.
    let dead = TcpListener::bind(("127.0.0.1", 0))
        .unwrap()
        .local_addr()
        .unwrap();
    let model = mixed_model(dead);
    let error = resolve_drivers(&model, &DriverRegistry::standard())
        .err()
        .unwrap();
    match error {
        AssemblyError::DeviceBackend { device, kind, .. } => {
            assert_eq!(device, BUS_DEVICE);
            assert_eq!(kind, SIM_BUS_KIND);
        }
        other => panic!("expected DeviceBackend, got {other:?}"),
    }
}

#[test]
fn a_bus_device_serving_the_wrong_register_map_fails_assembly() {
    // The server holds no register 4: the `sim-bus` factory's probe
    // catches it at assembly, naming the point the device cannot serve.
    let wrong_bank = RegisterBank::new([RegisterDecl {
        register: 40,
        initial: Value::Float(0.0),
    }])
    .unwrap();
    with_server(wrong_bank, |_, addr| {
        let model = mixed_model(addr);
        let error = resolve_drivers(&model, &DriverRegistry::standard())
            .err()
            .unwrap();
        match error {
            AssemblyError::DeviceBackend {
                device,
                kind,
                detail,
            } => {
                assert_eq!(device, BUS_DEVICE);
                assert_eq!(kind, SIM_BUS_KIND);
                assert!(detail.contains("11"), "{detail}");
            }
            other => panic!("expected DeviceBackend, got {other:?}"),
        }
    });
}
