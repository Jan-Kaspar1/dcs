//! Integration tests for the cyclic register-image `sim-cyclic` device
//! kind: the checked-in fixture resolves through the standard driver
//! registry into a cyclic-capable backend whose held input image and
//! staged output image move at the executor's exchange boundary — one
//! wire exchange per scan, never per point; malformed station or
//! threshold parameters fail assembly with a named [`AssemblyError`];
//! and an unreachable or wrongly-mapped device server fails the build
//! before any scan.

use dcs_assembly::{
    AssemblyError, BuildError, ComponentRegistry, DriverRegistry, FanoutDriver, SIM_CYCLIC_KIND,
    assemble, resolve_drivers,
};
use dcs_blocks::{AnalogInput, Pid};
use dcs_core::{CyclicIoDriver, Direction, IoDriver, PointId, Sample, Tick, Value, ValueKind};
use dcs_model::{DeviceId, PlantModel};
use dcs_runtime::Component;
use dcs_sim_bus::{BusServer, CyclicBusDriver, CyclicPoint, RegisterBank, RegisterDecl};
use std::collections::{BTreeMap, BTreeSet};
use std::net::{SocketAddr, TcpListener};
use std::thread;
use std::time::Duration;

/// The cyclic fixture: a local `sim` device (setpoint) and a
/// `sim-cyclic` device whose `level-raw` channel lives at register 4 in
/// station `inlet` and whose `valve-cmd` channel stages register 9 in
/// station `outlet` — the `address` parameter is the `__BUS_ADDR__`
/// placeholder tests substitute a live server for.
const CYCLIC_BUS: &str = include_str!("../fixtures/cyclic_bus.json");
/// The malformed fixture: a zero `exchange_miss_threshold` would
/// escalate every read at the first miss.
const BAD_CYCLIC: &str = include_str!("../fixtures/invalid/bad_sim_cyclic_parameters.json");

const SETPOINT: PointId = PointId(10);
const LEVEL_RAW: PointId = PointId(11);
const VALVE_CMD: PointId = PointId(12);
const BUS_DEVICE: DeviceId = DeviceId(2);
/// The register addresses the fixture's stations map the channels to.
const LEVEL_REGISTER: u16 = 4;
const VALVE_REGISTER: u16 = 9;

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
}

fn model(source: &str) -> PlantModel {
    PlantModel::load(source).unwrap()
}

/// The station layout the fixture declares: `inlet` holds the level
/// register, `outlet` the valve register.
fn stations() -> BTreeMap<String, BTreeSet<u16>> {
    [
        ("inlet".to_string(), BTreeSet::from([LEVEL_REGISTER])),
        ("outlet".to_string(), BTreeSet::from([VALVE_REGISTER])),
    ]
    .into_iter()
    .collect()
}

/// The register bank the fixture's device serves.
fn device_bank() -> RegisterBank {
    RegisterBank::new([
        RegisterDecl {
            register: LEVEL_REGISTER,
            initial: Value::Float(0.0),
        },
        RegisterDecl {
            register: VALVE_REGISTER,
            initial: Value::Float(0.0),
        },
    ])
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

/// Serves the fixture's register bank with its declared station layout
/// on an ephemeral loopback port for the duration of `test`, then
/// shuts the server down and joins its accept thread.
fn with_server<R>(test: impl FnOnce(&BusServer, SocketAddr) -> R) -> R {
    let server = BusServer::bind_stationed(("127.0.0.1", 0), device_bank(), stations()).unwrap();
    let addr = server.local_addr().unwrap();
    thread::scope(|scope| {
        scope.spawn(|| server.serve());
        let _guard = ShutdownOnDrop(&server);
        test(&server, addr)
    })
}

/// The fixture model with a live server address substituted in.
fn cyclic_model(addr: SocketAddr) -> PlantModel {
    model(&CYCLIC_BUS.replace("__BUS_ADDR__", &addr.to_string()))
}

/// Resolves and builds the fixture's driver side through the standard
/// registry.
fn build_driver(model: &PlantModel) -> FanoutDriver {
    resolve_drivers(model, &DriverRegistry::standard())
        .unwrap()
        .build()
        .unwrap()
}

/// The fan-out's cyclic surface — `Some` while any backend answers
/// `cyclic()`.
fn cyclic(driver: &FanoutDriver) -> &(dyn CyclicIoDriver + Sync) {
    driver.cyclic().unwrap()
}

#[test]
fn the_sim_cyclic_kind_resolves_and_exchanges_once_per_scan() {
    with_server(|server, addr| {
        let model = cyclic_model(addr);
        let driver = build_driver(&model);

        // The kind resolved through the standard registry — `sim` did
        // not claim it by prefix — and the backend carries the cyclic
        // surface, so the fan-out answers `cyclic()` itself.
        assert!(driver.sim().is_some());
        let backend = driver.backend(BUS_DEVICE).unwrap();
        assert!(backend.cyclic().is_some());
        assert!(driver.cyclic().is_some());
        assert!(driver.inspect::<CyclicBusDriver>(BUS_DEVICE).is_some());
        // The register-image kind is field-facing — a redundant pair's
        // write gate treats its points as the shared field — while the
        // local `sim` point is not.
        assert!(driver.is_field_point(LEVEL_RAW));
        assert!(!driver.is_field_point(SETPOINT));

        // Reads serve the connect-time census (Tick::ZERO); a write
        // stages the output image — the field register does not move
        // until an exchange publishes it. One exchange per scan, never
        // per point.
        server
            .bank()
            .write(LEVEL_REGISTER, Value::Float(7.0))
            .unwrap();
        assert_eq!(driver.read(LEVEL_RAW).unwrap().value, Value::Float(0.0));
        driver.write(VALVE_CMD, Value::Float(8.5)).unwrap();
        assert_eq!(
            server.bank().read(VALVE_REGISTER).unwrap().value,
            Value::Float(0.0),
            "a staged write must not reach the field before an exchange"
        );
        cyclic(&driver).exchange(Tick(1)).unwrap();
        assert_eq!(
            server.bank().read(VALVE_REGISTER).unwrap().value,
            Value::Float(8.5)
        );
        assert_eq!(
            driver.read(LEVEL_RAW).unwrap(),
            Sample::good(Value::Float(7.0), Tick(1))
        );

        // The assembled executor exchanges once per scan at the input
        // boundary: the PID's staged output publishes through the next
        // scan's exchange and the counters accumulate one attempt per
        // boundary.
        let mut executor = assemble(&model, &registry(), &driver).unwrap();
        for _ in 0..10 {
            executor.scan().unwrap();
        }
        // The manual exchange above plus the ten scans' exchanges.
        let exchange = executor
            .snapshot()
            .io_health
            .driver
            .unwrap()
            .exchange
            .unwrap();
        assert_eq!(exchange.attempted, 11);
        assert_eq!(exchange.succeeded, 11);
        assert_eq!(exchange.last_exchange_tick, Some(Tick(10)));
        // The valve command converged onto the field register through
        // the exchanges — the loop drives it toward its clamp.
        let Value::Float(valve) = server.bank().read(VALVE_REGISTER).unwrap().value else {
            panic!("valve register must be Float")
        };
        assert!(valve > 0.0, "valve={valve}");
    });
}

#[test]
fn the_cyclic_backend_arbitrates_the_writer_claim() {
    with_server(|server, addr| {
        let model = cyclic_model(addr);
        let driver = build_driver(&model);

        // The register-image kind arbitrates through the device
        // server's claim: no unfenceable field-facing device remains,
        // so a model built on it may arm automatic failover.
        assert!(driver.unfenced_field_devices().is_empty());

        driver.claim_field_writer(7).unwrap();
        // A second attachment's exchange carrying staged outputs is
        // fenced; its census-only exchange — the tracking standby's —
        // stays open.
        let tracking = CyclicBusDriver::connect_with_timeout(
            addr,
            Duration::from_millis(500),
            &[CyclicPoint {
                point: LEVEL_RAW,
                register: LEVEL_REGISTER,
                direction: Direction::In,
                kind: ValueKind::Float,
            }],
            &stations(),
            3,
        )
        .unwrap();
        server
            .bank()
            .write(LEVEL_REGISTER, Value::Float(6.0))
            .unwrap();
        tracking.cyclic().unwrap().exchange(Tick(1)).unwrap();
        assert_eq!(
            tracking.read(LEVEL_RAW).unwrap().value,
            Value::Float(6.0),
            "the tracking attachment's census latches fresh inputs under the claim"
        );

        // The claimed fan-out's own staged output publishes.
        driver.write(VALVE_CMD, Value::Float(4.0)).unwrap();
        cyclic(&driver).exchange(Tick(2)).unwrap();
        assert_eq!(
            server.bank().read(VALVE_REGISTER).unwrap().value,
            Value::Float(4.0)
        );
    });
}

#[test]
fn malformed_station_map_fixture_fails_assembly_naming_the_device() {
    let model = model(BAD_CYCLIC);
    let error = resolve_drivers(&model, &DriverRegistry::standard())
        .err()
        .unwrap();
    assert!(error.to_string().contains("device 1"));
    assert!(error.to_string().contains("\"sim-cyclic\""));
    match error {
        AssemblyError::InvalidDeviceParameters {
            device,
            kind,
            detail,
        } => {
            assert_eq!(device, DeviceId(1));
            assert_eq!(kind, SIM_CYCLIC_KIND);
            assert!(detail.contains("exchange_miss_threshold"), "{detail}");
        }
        other => panic!("expected InvalidDeviceParameters, got {other:?}"),
    }
}

/// Mutates the fixture's `sim-cyclic` device, asserts assembly fails
/// with [`AssemblyError::InvalidDeviceParameters`] naming the device
/// and kind, and returns the failure's detail.
fn expect_bad_parameters(mutate: impl FnOnce(&mut serde_json::Value)) -> String {
    let mut document: serde_json::Value = serde_json::from_str(CYCLIC_BUS).unwrap();
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
            assert_eq!(kind, SIM_CYCLIC_KIND);
            detail
        }
        other => panic!("expected InvalidDeviceParameters, got {other:?}"),
    }
}

#[test]
fn malformed_sim_cyclic_parameters_fail_assembly_with_named_errors() {
    // A missing station map.
    let detail = expect_bad_parameters(|device| {
        device["parameters"] = serde_json::json!({
            "address": "127.0.0.1:1",
            "exchange_miss_threshold": 3,
        });
    });
    assert!(detail.contains("\"stations\""), "{detail}");
    // A missing miss threshold.
    let detail = expect_bad_parameters(|device| {
        device["parameters"] = serde_json::json!({
            "address": "127.0.0.1:1",
            "stations": { "inlet": { "level-raw": 4 }, "outlet": { "valve-cmd": 9 } },
        });
    });
    assert!(detail.contains("exchange_miss_threshold"), "{detail}");
    // A threshold that is not a positive integer.
    expect_bad_parameters(|device| {
        device["parameters"]["exchange_miss_threshold"] = serde_json::json!(-1);
    });
    expect_bad_parameters(|device| {
        device["parameters"]["exchange_miss_threshold"] = serde_json::json!("three");
    });
    // A station map that is not an object.
    expect_bad_parameters(|device| {
        device["parameters"]["stations"] = serde_json::json!(["inlet"]);
    });
    // A station naming a channel the device does not declare.
    let detail = expect_bad_parameters(|device| {
        device["parameters"]["stations"]["inlet"]["no-such-channel"] = serde_json::json!(5);
    });
    assert!(detail.contains("no-such-channel"), "{detail}");
    // A station left holding no channels.
    let detail = expect_bad_parameters(|device| {
        device["parameters"]["stations"]["outlet"]
            .as_object_mut()
            .unwrap()
            .remove("valve-cmd");
    });
    assert!(detail.contains("no channels"), "{detail}");
    // A declared channel no station covers.
    let detail = expect_bad_parameters(|device| {
        device["parameters"]["stations"]
            .as_object_mut()
            .unwrap()
            .remove("outlet");
    });
    assert!(detail.contains("valve-cmd"), "{detail}");
    // A channel claimed by two stations.
    let detail = expect_bad_parameters(|device| {
        device["parameters"]["stations"]["outlet"]["level-raw"] = serde_json::json!(5);
    });
    assert!(detail.contains("level-raw"), "{detail}");
    // Two channels sharing one register across stations.
    let detail = expect_bad_parameters(|device| {
        device["parameters"]["stations"]["outlet"]["valve-cmd"] = serde_json::json!(4);
    });
    assert!(detail.contains("register 4"), "{detail}");
    // A register address outside the u16 range.
    let detail = expect_bad_parameters(|device| {
        device["parameters"]["stations"]["inlet"]["level-raw"] = serde_json::json!(70000);
    });
    assert!(detail.contains("65535"), "{detail}");
    // An initial whose kind disagrees with the channel.
    let detail = expect_bad_parameters(|device| {
        device["parameters"]["stations"]["inlet"]["level-raw"] =
            serde_json::json!({ "register": 4, "initial": { "bool": true } });
    });
    assert!(detail.contains("Float"), "{detail}");
    // An unknown parameter key.
    let detail = expect_bad_parameters(|device| {
        device["parameters"]["bogus"] = serde_json::json!(1);
    });
    assert!(detail.contains("\"bogus\""), "{detail}");
    // A negative timeout.
    expect_bad_parameters(|device| {
        device["parameters"]["timeout_ms"] = serde_json::json!(-5);
    });
    // An address that does not resolve.
    expect_bad_parameters(|device| {
        device["parameters"]["address"] = serde_json::json!("not a socket address");
    });
}

#[test]
fn an_unreachable_sim_cyclic_backend_fails_before_any_scan() {
    // A guaranteed-dead address: bind once to learn a free port, then
    // drop the listener so the connect is refused.
    let dead = TcpListener::bind(("127.0.0.1", 0))
        .unwrap()
        .local_addr()
        .unwrap();
    let model = cyclic_model(dead);
    let error = resolve_drivers(&model, &DriverRegistry::standard())
        .err()
        .unwrap();
    match error {
        AssemblyError::DeviceBackend { device, kind, .. } => {
            assert_eq!(device, BUS_DEVICE);
            assert_eq!(kind, SIM_CYCLIC_KIND);
        }
        other => panic!("expected DeviceBackend, got {other:?}"),
    }
}

#[test]
fn a_cyclic_device_serving_the_wrong_register_map_fails_assembly() {
    // The server holds no register 4: the factory's connect-time census
    // probe catches it before any scan, naming the register the device
    // cannot serve.
    let bank = RegisterBank::new([RegisterDecl {
        register: VALVE_REGISTER,
        initial: Value::Float(0.0),
    }])
    .unwrap();
    let server = BusServer::bind_stationed(("127.0.0.1", 0), bank, stations()).unwrap();
    let addr = server.local_addr().unwrap();
    thread::scope(|scope| {
        scope.spawn(|| server.serve());
        let _guard = ShutdownOnDrop(&server);
        let model = cyclic_model(addr);
        let error = resolve_drivers(&model, &DriverRegistry::standard())
            .err()
            .unwrap();
        let message = error.to_string();
        match error {
            AssemblyError::DeviceBackend { device, kind, .. } => {
                assert_eq!(device, BUS_DEVICE);
                assert_eq!(kind, SIM_CYCLIC_KIND);
            }
            other => panic!("expected DeviceBackend, got {other:?}"),
        }
        assert!(message.contains("register 4"), "{message}");
    });
}
