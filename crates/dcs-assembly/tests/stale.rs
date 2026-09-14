//! Integration tests for the declared freshness contract: the
//! `stale_input` fixture marks field `io_point` inputs with
//! `stale_after_ticks`, assembly carries each budget into the executor's
//! point map, and a field source that stops refreshing — a sim point
//! left unstepped or a bus register holding the tick of its last write —
//! lands in the image as `Uncertain(Stale)` once the lag exceeds the
//! budget, recovering to `Good` on the first fresh read. Points without
//! the field are never stale-stamped.

use dcs_assembly::{ComponentRegistry, DriverRegistry, FanoutDriver, assemble, resolve_drivers};
use dcs_core::{IoDriver, PointId, Quality, QualityReason, Sample, Tick, Value};
use dcs_model::PlantModel;
use dcs_runtime::Executor;
use dcs_sim_bus::{BusServer, RegisterBank, RegisterDecl};
use std::net::SocketAddr;
use std::thread;

/// The checked-in fixture: a `sim` device serving a budgeted
/// measurement and an unbudgeted ambient input, and a `sim-bus` device
/// whose `level-raw` channel — the `__BUS_ADDR__` placeholder tests
/// substitute a live server for — carries the larger budget.
const STALE_INPUT: &str = include_str!("../fixtures/stale_input.json");

const MEASUREMENT: PointId = PointId(10);
const LEVEL_RAW: PointId = PointId(11);
const AMBIENT: PointId = PointId(12);
/// The register address the fixture maps `level-raw` to.
const LEVEL_REGISTER: u16 = 4;

fn model(source: &str) -> PlantModel {
    PlantModel::load(source).unwrap()
}

/// The register bank the fixture's bus device serves: `level-raw` at
/// register 4, stamped at the bank's tick 0.
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

/// The fixture with a live server address substituted in.
fn stale_model(addr: SocketAddr) -> PlantModel {
    model(&STALE_INPUT.replace("__BUS_ADDR__", &addr.to_string()))
}

/// Resolves and builds the fixture's driver side through the standard
/// registry.
fn build_driver(model: &PlantModel) -> FanoutDriver {
    resolve_drivers(model, &DriverRegistry::standard())
        .unwrap()
        .build()
        .unwrap()
}

/// The assembled executor's latest image sample for `point`.
fn image_sample(executor: &Executor<'_>, point: PointId) -> Sample {
    executor
        .snapshot()
        .points
        .iter()
        .find(|telemetry| telemetry.point == point)
        .and_then(|telemetry| telemetry.sample)
        .unwrap()
}

#[test]
fn fixture_declares_freshness_budgets() {
    let model = model(&STALE_INPUT.replace("__BUS_ADDR__", "127.0.0.1:1"));
    let budget_of = |id| {
        model
            .io_points
            .iter()
            .find(|point| point.id == id)
            .unwrap()
            .stale_after_ticks
    };
    assert_eq!(budget_of(MEASUREMENT), Some(2));
    assert_eq!(budget_of(LEVEL_RAW), Some(3));
    assert_eq!(budget_of(AMBIENT), None);

    // The model serde-roundtrips: declared budgets survive, and the
    // unbudgeted point serializes back without the key.
    let json = serde_json::to_string_pretty(&model).unwrap();
    let reloaded = PlantModel::load(&json).unwrap();
    assert_eq!(reloaded, model);
    assert!(json.contains("\"stale_after_ticks\": 2"), "{json}");
    assert!(!json.contains("\"stale_after_ticks\": null"), "{json}");
}

#[test]
fn sim_input_holding_an_old_tick_turns_stale() {
    with_server(device_bank(), |_, addr| {
        let model = stale_model(addr);
        let driver = build_driver(&model);
        let mut executor = assemble(&model, &ComponentRegistry::new(), &driver).unwrap();

        // The device refreshed the measurement at its tick 0 — then
        // stopped.
        driver.write(MEASUREMENT, Value::Float(7.0)).unwrap();
        driver.write(AMBIENT, Value::Float(20.0)).unwrap();

        // Scans 1 and 2 lag by one and two ticks — inside the budget of
        // 2 — and the image stamps the scan tick, never the driver's.
        executor.scan().unwrap();
        driver.step(0.1).unwrap();
        executor.scan().unwrap();
        assert_eq!(
            image_sample(&executor, MEASUREMENT),
            Sample::good(Value::Float(7.0), Tick(2))
        );

        // Scan 3's lag of 3 exceeds the budget: the held value lands as
        // Uncertain(Stale) at the scan tick.
        driver.step(0.1).unwrap();
        executor.scan().unwrap();
        assert_eq!(
            image_sample(&executor, MEASUREMENT),
            Sample::new(
                Value::Float(7.0),
                Quality::Uncertain(QualityReason::Stale),
                Tick(3)
            )
        );

        // The unbudgeted input on the same device stays Good regardless
        // of the lag — its driver sample carries the same held tick.
        assert_eq!(
            image_sample(&executor, AMBIENT),
            Sample::good(Value::Float(20.0), Tick(3))
        );

        // The first sample the device refreshes inside the budget
        // returns the driver's own Good quality.
        driver.write(MEASUREMENT, Value::Float(9.0)).unwrap();
        executor.scan().unwrap();
        assert_eq!(
            image_sample(&executor, MEASUREMENT),
            Sample::good(Value::Float(9.0), Tick(4))
        );
    });
}

#[test]
fn bus_register_holding_an_old_tick_turns_stale() {
    with_server(device_bank(), |server, addr| {
        let model = stale_model(addr);
        let driver = build_driver(&model);
        let mut executor = assemble(&model, &ComponentRegistry::new(), &driver).unwrap();

        // The device last wrote the register at its bank tick 0; each
        // fan-out step advances the bank's tick without touching it —
        // the held sample's stamp falls further behind every scan.
        server
            .bank()
            .write(LEVEL_REGISTER, Value::Float(6.5))
            .unwrap();
        driver.step(0.1).unwrap();
        for _ in 0..3 {
            executor.scan().unwrap();
            driver.step(0.1).unwrap();
        }
        // Three lags inside the budget of 3 stay Good.
        assert_eq!(
            image_sample(&executor, LEVEL_RAW),
            Sample::good(Value::Float(6.5), Tick(3))
        );

        // The fourth scan's lag exceeds the budget — the register-held
        // value lands as Uncertain(Stale), stamped at the scan tick.
        executor.scan().unwrap();
        assert_eq!(
            image_sample(&executor, LEVEL_RAW),
            Sample::new(
                Value::Float(6.5),
                Quality::Uncertain(QualityReason::Stale),
                Tick(4)
            )
        );

        // A fresh device write inside the budget returns Good.
        server
            .bank()
            .write(LEVEL_REGISTER, Value::Float(7.5))
            .unwrap();
        executor.scan().unwrap();
        assert_eq!(
            image_sample(&executor, LEVEL_RAW),
            Sample::good(Value::Float(7.5), Tick(5))
        );
    });
}

#[test]
fn identical_stale_runs_produce_identical_snapshots() {
    let run = || {
        with_server(device_bank(), |_, addr| {
            let model = stale_model(addr);
            let driver = build_driver(&model);
            let mut executor = assemble(&model, &ComponentRegistry::new(), &driver).unwrap();
            driver.write(MEASUREMENT, Value::Float(7.0)).unwrap();
            for _ in 0..5 {
                executor.scan().unwrap();
                driver.step(0.1).unwrap();
            }
            serde_json::to_string(&executor.snapshot()).unwrap()
        })
    };
    assert_eq!(run(), run());
}
