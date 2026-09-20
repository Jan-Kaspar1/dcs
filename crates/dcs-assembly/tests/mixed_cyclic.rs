//! Integration tests for a plant mixing the cyclic register-image
//! `sim-cyclic` device kind with the point-wise `sim-tcp` kind under one
//! controller — the mixed shape the hardware-independence promise
//! (WW-FND-002) requires the exchange boundary to cover: the checked-in
//! fixture assembles through the standard driver registry into one
//! [`FanoutDriver`] whose `exchange` reaches only the cyclic backend
//! once per scan while the remote plant's points keep flowing per
//! point; a missed exchange retains the cyclic output image and
//! degrades only the cyclic device's points; and a tracking standby's
//! [`WriteGate`] quiesces both backends' field-facing writes without
//! freezing the reads its tracking cycle needs.

use dcs_assembly::{
    BuildError, ComponentRegistry, DriverRegistry, FanoutDriver, assemble, resolve_drivers,
};
use dcs_blocks::{AnalogInput, Pid};
use dcs_core::{
    CyclicIoDriver, Direction, IoDriver, IoError, IoFault, LinkState, PointId, Quality,
    QualityReason, Sample, TelemetrySnapshot, Tick, Value,
};
use dcs_model::{DeviceId, PlantModel};
use dcs_runtime::{Component, WriteGate};
use dcs_sim::{ChannelId, ChannelMap, PointBinding, SimDriver};
use dcs_sim_bus::{BusServer, CyclicBusDriver, ExchangeOutcome, RegisterBank, RegisterDecl};
use dcs_sim_net::{PlantServer, RemoteDriver};
use std::collections::{BTreeMap, BTreeSet};
use std::net::SocketAddr;
use std::thread;

/// The mixed-transport fixture: a local `sim` device (setpoint), a
/// `sim-cyclic` device whose `level-raw` channel lives at register 4 in
/// station `inlet` and whose `valve-cmd` channel stages register 9 in
/// station `outlet`, and a `sim-tcp` device serving `flow-raw` and
/// `pump-cmd` per point — the `__BUS_ADDR__`/`__PLANT_ADDR__`
/// placeholders tests substitute live servers for.
const MIXED: &str = include_str!("../fixtures/mixed_cyclic.json");

const SETPOINT: PointId = PointId(10);
const LEVEL_RAW: PointId = PointId(11);
const VALVE_CMD: PointId = PointId(12);
const FLOW_RAW: PointId = PointId(13);
const PUMP_CMD: PointId = PointId(14);
const CYCLIC_DEVICE: DeviceId = DeviceId(2);
const TCP_DEVICE: DeviceId = DeviceId(3);
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

/// The register bank the fixture's `sim-cyclic` device serves.
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

/// The remote device 3's slice of the plant: the `flow-raw` and
/// `pump-cmd` points the `sim-tcp` backend must serve.
fn remote_map() -> ChannelMap {
    ChannelMap::new()
        .with_point(PointBinding {
            point: FLOW_RAW,
            channel: ChannelId {
                device: 3,
                name: "flow-raw".to_string(),
            },
            direction: Direction::In,
            initial: Value::Float(0.0),
        })
        .with_point(PointBinding {
            point: PUMP_CMD,
            channel: ChannelId {
                device: 3,
                name: "pump-cmd".to_string(),
            },
            direction: Direction::Out,
            initial: Value::Float(0.0),
        })
}

/// `shutdown` on drop for both servers, so a panicking test still lets
/// the scoped serve threads exit instead of hanging the scope's join.
struct ShutdownOnDrop<'s> {
    bus: &'s BusServer,
    plant: &'s PlantServer,
}

impl Drop for ShutdownOnDrop<'_> {
    fn drop(&mut self) {
        self.bus.shutdown();
        self.plant.shutdown();
    }
}

/// Serves the fixture's register bank and remote plant on ephemeral
/// loopback ports for the duration of `test`, then shuts both servers
/// down and joins their accept threads.
fn with_field<R>(test: impl FnOnce(&BusServer, &PlantServer, SocketAddr, SocketAddr) -> R) -> R {
    let bus = BusServer::bind_stationed(("127.0.0.1", 0), device_bank(), stations()).unwrap();
    let plant = PlantServer::bind(("127.0.0.1", 0), SimDriver::new(remote_map()).unwrap()).unwrap();
    let bus_addr = bus.local_addr().unwrap();
    let plant_addr = plant.local_addr().unwrap();
    thread::scope(|scope| {
        scope.spawn(|| bus.serve());
        scope.spawn(|| plant.serve());
        let _guard = ShutdownOnDrop {
            bus: &bus,
            plant: &plant,
        };
        test(&bus, &plant, bus_addr, plant_addr)
    })
}

/// The fixture model with live server addresses substituted in.
fn mixed_model(bus_addr: SocketAddr, plant_addr: SocketAddr) -> PlantModel {
    model(
        &MIXED
            .replace("__BUS_ADDR__", &bus_addr.to_string())
            .replace("__PLANT_ADDR__", &plant_addr.to_string()),
    )
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

/// The assembled cyclic backend — scripting exchange outcomes is
/// development tooling, reached through the inspect handle.
fn cyclic_backend(driver: &FanoutDriver) -> &CyclicBusDriver {
    driver.inspect::<CyclicBusDriver>(CYCLIC_DEVICE).unwrap()
}

/// The point's image sample in `snapshot`, when it carries one.
fn image_sample(snapshot: &TelemetrySnapshot, point: PointId) -> Sample {
    snapshot
        .points
        .iter()
        .find(|telemetry| telemetry.point == point)
        .and_then(|telemetry| telemetry.sample)
        .unwrap()
}

#[test]
fn mixed_cyclic_and_pointwise_kinds_assemble_and_scan() {
    with_field(|bus, plant, bus_addr, plant_addr| {
        let model = mixed_model(bus_addr, plant_addr);
        let driver = build_driver(&model);
        // The field-facing backends claim the shared field's write
        // ownership — the fail-closed plant refuses a claim-less
        // mutation.
        driver.claim_field_writer(1).unwrap();

        // All three kinds resolved: the local `sim` device joined the
        // shared simulated map, the cyclic device carries the
        // CyclicIoDriver surface, and the point-wise remote does not —
        // the fan-out answers `cyclic()` because exactly one backend
        // does.
        assert!(driver.sim().is_some());
        assert!(driver.backend(CYCLIC_DEVICE).unwrap().cyclic().is_some());
        assert!(driver.backend(TCP_DEVICE).unwrap().cyclic().is_none());
        assert!(driver.cyclic().is_some());
        assert!(driver.inspect::<RemoteDriver>(TCP_DEVICE).is_some());
        // Both field kinds are field-facing — a redundant pair's write
        // gate treats their points as the shared field — while the
        // local `sim` point is not.
        assert!(driver.is_field_point(LEVEL_RAW));
        assert!(driver.is_field_point(FLOW_RAW));
        assert!(driver.is_field_point(PUMP_CMD));
        assert!(!driver.is_field_point(SETPOINT));

        // The exchange boundary covers exactly the cyclic device's
        // image: the `sim-tcp` points move per point with no exchange
        // at all, while the cyclic device's reads serve the held image
        // and its writes stage until an exchange publishes them.
        plant.driver().write(FLOW_RAW, Value::Float(6.5)).unwrap();
        assert_eq!(driver.read(FLOW_RAW).unwrap().value, Value::Float(6.5));
        driver.write(PUMP_CMD, Value::Float(3.0)).unwrap();
        assert_eq!(
            plant.driver().read(PUMP_CMD).unwrap().value,
            Value::Float(3.0),
            "a point-wise write lands on the remote plant without an exchange"
        );
        bus.bank().write(LEVEL_REGISTER, Value::Float(7.0)).unwrap();
        assert_eq!(
            driver.read(LEVEL_RAW).unwrap().value,
            Value::Float(0.0),
            "the held input image answers until an exchange relatches it"
        );
        driver.write(VALVE_CMD, Value::Float(8.5)).unwrap();
        assert_eq!(
            bus.bank().read(VALVE_REGISTER).unwrap().value,
            Value::Float(0.0),
            "a staged cyclic write must not reach the field before an exchange"
        );
        cyclic(&driver).exchange(Tick(1)).unwrap();
        assert_eq!(
            bus.bank().read(VALVE_REGISTER).unwrap().value,
            Value::Float(8.5)
        );
        assert_eq!(driver.read(LEVEL_RAW).unwrap().value, Value::Float(7.0));

        // The assembled executor exchanges once per scan on the cyclic
        // backend only: the aggregate counters are that backend's own —
        // the point-wise backend has no exchange to contribute — while
        // every scan's per-point reads and writes keep flowing.
        let mut executor = assemble(&model, &registry(), &driver).unwrap();
        for _ in 0..5 {
            executor.scan();
        }
        let exchange = executor
            .snapshot()
            .io_health
            .driver
            .unwrap()
            .exchange
            .unwrap();
        assert_eq!(exchange.attempted, 6); // the manual exchange plus five scans'
        assert_eq!(exchange.succeeded, 6);
        assert_eq!(exchange.last_exchange_tick, Some(Tick(5)));
        assert_eq!(
            cyclic_backend(&driver)
                .diagnostics()
                .unwrap()
                .exchange
                .unwrap()
                .attempted,
            6,
            "the aggregate exchange count is the cyclic backend's alone"
        );
        let snapshot = executor.snapshot();
        assert_eq!(image_sample(&snapshot, LEVEL_RAW).quality, Quality::Good);
        assert_eq!(image_sample(&snapshot, FLOW_RAW).quality, Quality::Good);
        assert_eq!(snapshot.io_health.failed_reads, 0);
        assert_eq!(snapshot.io_health.failed_writes, 0);
    });
}

#[test]
fn a_missed_cyclic_exchange_degrades_only_the_cyclic_backend() {
    with_field(|bus, plant, bus_addr, plant_addr| {
        let model = mixed_model(bus_addr, plant_addr);
        let driver = build_driver(&model);
        driver.claim_field_writer(1).unwrap();

        // A failed exchange completes nothing: the staged output image
        // is retained — unpublished while the exchange misses, carried
        // onto the field by the first completed exchange after.
        driver.write(VALVE_CMD, Value::Float(8.5)).unwrap();
        cyclic_backend(&driver)
            .script_exchange(&[ExchangeOutcome::Miss])
            .unwrap();
        assert_eq!(
            cyclic(&driver).exchange(Tick(1)),
            Err(IoError::Disconnected(LEVEL_RAW))
        );
        assert_eq!(
            bus.bank().read(VALVE_REGISTER).unwrap().value,
            Value::Float(0.0),
            "a missed exchange publishes nothing and drops nothing"
        );
        // The point-wise backend sits outside the cyclic boundary: its
        // reads and writes flow while the cyclic link misses.
        driver.write(PUMP_CMD, Value::Float(3.5)).unwrap();
        assert_eq!(
            plant.driver().read(PUMP_CMD).unwrap().value,
            Value::Float(3.5)
        );
        cyclic(&driver).exchange(Tick(2)).unwrap();
        assert_eq!(
            bus.bank().read(VALVE_REGISTER).unwrap().value,
            Value::Float(8.5),
            "the retained output image publishes on the next completed exchange"
        );

        // Under the assembled executor, script misses up to the device's
        // declared threshold: the held image keeps serving under it and
        // escalates to Disconnected at it — degrading only the cyclic
        // device's points while `sim-tcp` points stay Good.
        let mut executor = assemble(&model, &registry(), &driver).unwrap();
        executor.scan();
        cyclic_backend(&driver)
            .script_exchange(&[ExchangeOutcome::Miss, ExchangeOutcome::Miss])
            .unwrap();
        executor.scan();
        // One miss under the threshold: the boundary failure counts
        // once, and the held image still serves the cyclic read.
        let snapshot = executor.snapshot();
        assert_eq!(snapshot.io_health.failed_exchanges, 1);
        assert_eq!(image_sample(&snapshot, LEVEL_RAW).quality, Quality::Good);
        assert_eq!(image_sample(&snapshot, FLOW_RAW).quality, Quality::Good);
        executor.scan();
        // The second miss reaches the declared threshold: the cyclic
        // input escalates to Bad(CommunicationFault) — the per-point
        // failure a held image becomes — while the remote plant's
        // point-wise points never saw the outage.
        let snapshot = executor.snapshot();
        assert_eq!(snapshot.io_health.failed_exchanges, 2);
        assert_eq!(snapshot.io_health.failed_reads, 1);
        assert_eq!(
            image_sample(&snapshot, LEVEL_RAW).quality,
            Quality::Bad(QualityReason::CommunicationFault)
        );
        assert_eq!(image_sample(&snapshot, FLOW_RAW).quality, Quality::Good);
        assert_eq!(
            snapshot.io_health.last_error,
            Some(IoFault {
                tick: Tick(3),
                point: LEVEL_RAW,
                direction: Direction::In,
                error: IoError::Disconnected(LEVEL_RAW),
            })
        );
        // The aggregate driver diagnostics name only the cyclic device:
        // its link reports the misses while the point-wise backend's
        // link stayed up.
        let diagnostics = snapshot.io_health.driver.unwrap();
        assert_eq!(diagnostics.link, LinkState::Disconnected);
        let last_error = diagnostics.last_error.unwrap();
        assert!(last_error.contains("device 2"), "{last_error}");
        assert!(!last_error.contains("device 3"), "{last_error}");

        // Recovery: the script queue drained, the next exchange
        // completes — the miss count resets, the retained staged image
        // publishes, and the held image relatches Good.
        executor.scan();
        let snapshot = executor.snapshot();
        assert_eq!(snapshot.io_health.failed_exchanges, 2);
        assert_eq!(image_sample(&snapshot, LEVEL_RAW).quality, Quality::Good);
        assert_eq!(
            bus.bank().read(VALVE_REGISTER).unwrap().value,
            driver.read(VALVE_CMD).unwrap().value,
            "the published register and the relatched image agree"
        );
    });
}

#[test]
fn a_tracking_standby_gate_quiesces_both_backends_field_writes() {
    with_field(|bus, plant, bus_addr, plant_addr| {
        let model = mixed_model(bus_addr, plant_addr);
        let driver = build_driver(&model);
        driver.claim_field_writer(1).unwrap();
        // The tracking standby's posture: a closed gate covering the
        // field-facing points — both field backends' writes quiesce,
        // the local simulated backend's pass through.
        let gate = WriteGate::closed_covering(&driver, |point| driver.is_field_point(point));
        let mut standby = assemble(&model, &registry(), &gate).unwrap();

        // Preload the field so a quiesced write is observable as the
        // value that does not move.
        bus.bank().write(LEVEL_REGISTER, Value::Float(5.0)).unwrap();
        plant.driver().write(FLOW_RAW, Value::Float(12.0)).unwrap();
        plant.driver().write(PUMP_CMD, Value::Float(7.0)).unwrap();

        for _ in 0..3 {
            standby.scan();
        }
        let snapshot = standby.snapshot();

        // The reads the tracking cycle needs are unfrozen: the gate
        // forwards the exchange — the cyclic input image latched fresh
        // field values every scan — and the per-point reads beside it.
        assert_eq!(
            image_sample(&snapshot, LEVEL_RAW),
            Sample::good(Value::Float(5.0), Tick(3))
        );
        assert_eq!(
            image_sample(&snapshot, FLOW_RAW),
            Sample::good(Value::Float(12.0), Tick(3))
        );
        assert_eq!(
            cyclic_backend(&driver)
                .diagnostics()
                .unwrap()
                .exchange
                .unwrap()
                .attempted,
            3,
            "the closed gate still exchanges once per scan"
        );
        // Both backends' field-facing writes are quiesced: the cyclic
        // device's register never published and the remote plant's
        // point was never written.
        assert_eq!(
            bus.bank().read(VALVE_REGISTER).unwrap().value,
            Value::Float(0.0),
            "the gated scan's writes never reached the cyclic image"
        );
        assert_eq!(
            plant.driver().read(PUMP_CMD).unwrap().value,
            Value::Float(7.0),
            "the gated scan's writes never reached the remote plant"
        );
        // The uncovered local point's writes still land — the standby's
        // local simulated backend keeps tracking beside the gate.
        gate.write(SETPOINT, Value::Float(50.0)).unwrap();
        assert_eq!(
            driver.sim().unwrap().read(SETPOINT).unwrap().value,
            Value::Float(50.0)
        );

        // Promotion lifts the gate: the point-wise write lands at its
        // scan's write boundary, the cyclic staged image publishes at
        // the following scan's exchange — the contract's one-scan
        // actuation delay.
        gate.open();
        standby.scan();
        assert_eq!(
            plant.driver().read(PUMP_CMD).unwrap().value,
            Value::Float(50.0),
            "the remote output lands the scan the gate opens — flow 12.0 raw scales to 50.0"
        );
        standby.scan();
        assert_eq!(
            bus.bank().read(VALVE_REGISTER).unwrap().value,
            driver.read(VALVE_CMD).unwrap().value,
            "the staged cyclic image publishes at the next exchange"
        );
        assert_ne!(
            bus.bank().read(VALVE_REGISTER).unwrap().value,
            Value::Float(0.0),
            "the PID's output reached the field register"
        );
    });
}
