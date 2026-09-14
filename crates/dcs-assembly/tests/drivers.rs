//! Integration tests for the device-kind driver registry: a checked-in
//! fixture declaring two device kinds assembles into one executor whose
//! points route to the correct backend; the mixed local-plus-remote run
//! produces outputs identical to the equivalent single-backend reference;
//! and unknown kinds and malformed device parameters fail assembly with
//! named [`AssemblyError`] variants before any scan.

use dcs_assembly::{
    AssemblyError, BuildError, ComponentRegistry, DriverRegistry, assemble, resolve_drivers,
    sim_driver,
};
use dcs_blocks::{AnalogInput, Pid};
use dcs_core::{Direction, IoDriver, LinkState, PointId, TelemetrySnapshot, Value};
use dcs_model::{DeviceId, PlantModel};
use dcs_runtime::Component;
use dcs_sim::{ChannelId, ChannelMap, PointBinding, SimDriver};
use dcs_sim_net::PlantServer;
use std::net::{SocketAddr, TcpListener};
use std::thread;

/// The two-kind fixture: a local `sim` device (setpoint, valve) and a
/// `sim-tcp` device (level-raw) whose `address` parameter is the
/// `__PLANT_ADDR__` placeholder tests substitute a live server for.
const MIXED: &str = include_str!("../fixtures/mixed_kinds.json");
/// The all-local reference: the same points, components, and wiring on a
/// single `sim` device.
const TANK_LOOP: &str = include_str!("../fixtures/tank_loop.json");

const SETPOINT: PointId = PointId(10);
const LEVEL_RAW: PointId = PointId(11);
const VALVE: PointId = PointId(12);

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

/// The remote device 2's slice of the plant: the `level-raw` point the
/// `sim-tcp` backend must serve.
fn remote_map() -> ChannelMap {
    ChannelMap::new().with_point(PointBinding {
        point: LEVEL_RAW,
        channel: ChannelId {
            device: 2,
            name: "level-raw".to_string(),
        },
        direction: Direction::In,
        initial: Value::Float(0.0),
    })
}

/// `shutdown` on drop, so a panicking test still lets the scoped serve
/// thread exit instead of hanging the scope's join.
struct ShutdownOnDrop<'s>(&'s PlantServer);

impl Drop for ShutdownOnDrop<'_> {
    fn drop(&mut self) {
        self.0.shutdown();
    }
}

/// Serves `map`'s plant on an ephemeral loopback port for the duration of
/// `test`, then shuts the server down and joins its accept thread.
fn with_server<R>(map: ChannelMap, test: impl FnOnce(&PlantServer, SocketAddr) -> R) -> R {
    let server = PlantServer::bind(("127.0.0.1", 0), SimDriver::new(map).unwrap()).unwrap();
    thread::scope(|scope| {
        scope.spawn(|| server.serve());
        let _guard = ShutdownOnDrop(&server);
        test(&server, server.local_addr().unwrap())
    })
}

/// The mixed-kind model with a live server address substituted in.
fn mixed_model(addr: SocketAddr) -> PlantModel {
    model(&MIXED.replace("__PLANT_ADDR__", &addr.to_string()))
}

#[test]
fn mixed_kind_fixture_assembles_scans_and_routes() {
    with_server(remote_map(), |server, addr| {
        let model = mixed_model(addr);
        let driver = resolve_drivers(&model, &DriverRegistry::standard())
            .unwrap()
            .build()
            .unwrap();
        let mut executor = assemble(&model, &registry(), &driver).unwrap();

        // The point space is one surface: the setpoint write lands on the
        // local simulated backend, the level read comes from the remote
        // plant.
        driver.write(SETPOINT, Value::Float(50.0)).unwrap();
        assert_eq!(
            driver.sim().unwrap().read(SETPOINT).unwrap().value,
            Value::Float(50.0)
        );
        server.driver().write(LEVEL_RAW, Value::Float(8.5)).unwrap();
        assert_eq!(driver.read(LEVEL_RAW).unwrap().value, Value::Float(8.5));
        // The local backend does not serve the remote device's point, and
        // the remote backend does not serve local points.
        assert_eq!(
            driver.sim().unwrap().read(LEVEL_RAW),
            Err(dcs_core::IoError::UnknownPoint(LEVEL_RAW))
        );
        assert_eq!(
            driver.backend(DeviceId(2)).unwrap().read(VALVE),
            Err(dcs_core::IoError::UnknownPoint(VALVE))
        );

        // The cross-backend field wire carries the valve command onto the
        // remote level point at each step, so the loop closes over TCP.
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
fn two_backend_run_matches_single_backend_reference() {
    // The single-backend reference: the all-`sim` tank-loop model through
    // `sim_driver`, exactly as before the registry existed.
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

    // The same loop split across the local `sim` backend and the remote
    // `sim-tcp` plant; the fan-out routes each point to its owner and the
    // cross-backend wire carries the valve command into the remote level.
    let mixed = with_server(remote_map(), |_, addr| {
        let model = mixed_model(addr);
        let driver = resolve_drivers(&model, &DriverRegistry::standard())
            .unwrap()
            .build()
            .unwrap();
        let mut executor = assemble(&model, &registry(), &driver).unwrap();
        driver.write(SETPOINT, Value::Float(50.0)).unwrap();
        for _ in 0..200 {
            executor.scan().unwrap();
            driver.step(0.1).unwrap();
        }
        executor.snapshot()
    });

    // The mixed run's remote backend reports its link — the live
    // connection the reference's all-local driver has no transport to
    // report on. That is the one legitimate difference between the two
    // snapshots' health sections; every other field, including the
    // executor-collected counters, is identical.
    assert_eq!(
        mixed.io_health.driver.as_ref().map(|driver| driver.link),
        Some(LinkState::Connected)
    );
    let mut normalized = mixed;
    normalized.io_health.driver = None;
    assert_eq!(
        serde_json::from_str::<TelemetrySnapshot>(&reference).unwrap(),
        normalized
    );
}

#[test]
fn unknown_device_kind_fails_assembly_naming_device_and_kind() {
    let model = model(include_str!("../fixtures/invalid/unknown_device_kind.json"));
    let error = resolve_drivers(&model, &DriverRegistry::standard())
        .err()
        .unwrap();
    assert_eq!(
        error,
        AssemblyError::UnknownDeviceKind {
            device: DeviceId(1),
            kind: "ethercat-8ai".to_string(),
        }
    );
    assert!(error.to_string().contains("device 1"));
    assert!(error.to_string().contains("\"ethercat-8ai\""));
}

#[test]
fn missing_sim_tcp_address_is_invalid_device_parameters() {
    let model = model(include_str!(
        "../fixtures/invalid/bad_device_parameters.json"
    ));
    let error = resolve_drivers(&model, &DriverRegistry::standard())
        .err()
        .unwrap();
    match error {
        AssemblyError::InvalidDeviceParameters {
            device,
            kind,
            detail,
        } => {
            assert_eq!(device, DeviceId(1));
            assert_eq!(kind, "sim-tcp");
            assert!(detail.contains("\"address\""), "{detail}");
        }
        other => panic!("expected InvalidDeviceParameters, got {other:?}"),
    }
}

#[test]
fn malformed_sim_tcp_address_is_invalid_device_parameters() {
    let model = model(&MIXED.replace("__PLANT_ADDR__", "not a socket address"));
    let error = resolve_drivers(&model, &DriverRegistry::standard())
        .err()
        .unwrap();
    match error {
        AssemblyError::InvalidDeviceParameters { device, kind, .. } => {
            assert_eq!(device, DeviceId(2));
            assert_eq!(kind, "sim-tcp");
        }
        other => panic!("expected InvalidDeviceParameters, got {other:?}"),
    }
}

#[test]
fn unknown_sim_tcp_parameter_is_invalid_device_parameters() {
    let mut document: serde_json::Value = serde_json::from_str(MIXED).unwrap();
    document["devices"][1]["parameters"]["bogus"] = serde_json::json!(1);
    let model = model(&document.to_string());
    let error = resolve_drivers(&model, &DriverRegistry::standard())
        .err()
        .unwrap();
    match error {
        AssemblyError::InvalidDeviceParameters {
            device,
            kind,
            detail,
        } => {
            assert_eq!(device, DeviceId(2));
            assert_eq!(kind, "sim-tcp");
            assert!(detail.contains("\"bogus\""), "{detail}");
        }
        other => panic!("expected InvalidDeviceParameters, got {other:?}"),
    }
}

#[test]
fn unreachable_sim_tcp_backend_fails_before_any_scan() {
    // A guaranteed-dead address: bind once to learn a free port, then drop
    // the listener so the connect is refused.
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
            assert_eq!(device, DeviceId(2));
            assert_eq!(kind, "sim-tcp");
        }
        other => panic!("expected DeviceBackend, got {other:?}"),
    }
}

#[test]
fn sim_only_model_still_builds_through_the_registry() {
    // The standard registry serves the plain `sim` family too: the
    // single-backend tank loop assembles through `resolve_drivers` and
    // scans identically.
    let model = model(TANK_LOOP);
    let driver = resolve_drivers(&model, &DriverRegistry::standard())
        .unwrap()
        .build()
        .unwrap();
    assert!(driver.sim().is_some());
    let mut executor = assemble(&model, &registry(), &driver).unwrap();
    driver.write(SETPOINT, Value::Float(50.0)).unwrap();
    for _ in 0..200 {
        executor.scan().unwrap();
        driver.step(0.1).unwrap();
    }
    let Value::Float(level) = driver.read(LEVEL_RAW).unwrap().value else {
        panic!("level raw must be Float")
    };
    assert!((level - 12.0).abs() < 0.5, "level={level}");
}

#[test]
fn a_remote_device_serving_wrong_points_fails_assembly() {
    // The server binds the wrong point id: the `sim-tcp` factory's probe
    // catches it at assembly.
    let wrong_map = ChannelMap::new().with_point(PointBinding {
        point: PointId(99),
        channel: ChannelId {
            device: 2,
            name: "level-raw".to_string(),
        },
        direction: Direction::In,
        initial: Value::Float(0.0),
    });
    with_server(wrong_map, |_, addr| {
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
                assert_eq!(device, DeviceId(2));
                assert_eq!(kind, "sim-tcp");
                assert!(detail.contains("11"), "{detail}");
            }
            other => panic!("expected DeviceBackend, got {other:?}"),
        }
    });
}

#[test]
fn executor_snapshot_reports_the_remote_point() {
    with_server(remote_map(), |server, addr| {
        let model = mixed_model(addr);
        let driver = resolve_drivers(&model, &DriverRegistry::standard())
            .unwrap()
            .build()
            .unwrap();
        let mut executor = assemble(&model, &registry(), &driver).unwrap();
        server.driver().write(LEVEL_RAW, Value::Float(9.0)).unwrap();
        executor.scan().unwrap();
        let snapshot = executor.snapshot();
        let point = snapshot
            .points
            .iter()
            .find(|telemetry| telemetry.point == LEVEL_RAW)
            .unwrap();
        assert_eq!(point.sample.unwrap().value, Value::Float(9.0));
    });
}
