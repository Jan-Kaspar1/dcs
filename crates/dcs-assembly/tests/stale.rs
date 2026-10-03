//! Integration tests for the declared freshness contract: the
//! `stale_input` fixture marks field `io_point` inputs with
//! `stale_after_ticks`, assembly carries each budget into the executor's
//! point map, and a field source that stops stepping — the sim driver,
//! or the bank behind the bus device — leaves its served report frozen,
//! so the point lands in the image as `Uncertain(Stale)` once the lag
//! exceeds the budget, recovering to `Good` on the first fresh read.
//! While the field keeps stepping, a bare channel re-stamps every step
//! like a scanned input card and stays fresh. Points without the field
//! are never stale-stamped.

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
fn sim_input_stays_fresh_while_the_field_steps_and_stales_once_it_stops() {
    with_server(device_bank(), |_, addr| {
        let model = stale_model(addr);
        let driver = build_driver(&model);
        let mut executor = assemble(&model, &ComponentRegistry::new(), &driver).unwrap();

        // The device refreshed both inputs at its tick 0.
        driver.write(MEASUREMENT, Value::Float(7.0)).unwrap();
        driver.write(AMBIENT, Value::Float(20.0)).unwrap();

        // While the plant steps, the bare channel re-stamps every step
        // like a scanned input card: each scan observes a changed
        // report, so a held value stays fresh indefinitely.
        executor.scan();
        for _ in 0..2 {
            driver.step(0.1).unwrap();
            executor.scan();
        }
        assert_eq!(
            image_sample(&executor, MEASUREMENT),
            Sample::good(Value::Float(7.0), Tick(3))
        );

        // The plant stops stepping — the report freezes and the run's
        // own lag accrues. Scans 4 and 5 lag by one and two ticks —
        // inside the budget of 2 — and the image stamps the scan tick,
        // never the driver's.
        executor.run(2);
        assert_eq!(
            image_sample(&executor, MEASUREMENT),
            Sample::good(Value::Float(7.0), Tick(5))
        );

        // Scan 6's lag of 3 exceeds the budget: the held value lands as
        // Uncertain(Stale) at the scan tick.
        executor.scan();
        assert_eq!(
            image_sample(&executor, MEASUREMENT),
            Sample::new(
                Value::Float(7.0),
                Quality::Uncertain(QualityReason::Stale),
                Tick(6)
            )
        );

        // The unbudgeted input on the same device froze identically —
        // with no declared budget it stays Good regardless of the lag.
        assert_eq!(
            image_sample(&executor, AMBIENT),
            Sample::good(Value::Float(20.0), Tick(6))
        );

        // A resumed step's re-stamp is a changed report — the first
        // fresh read returns the driver's own Good quality.
        driver.step(0.1).unwrap();
        executor.scan();
        assert_eq!(
            image_sample(&executor, MEASUREMENT),
            Sample::good(Value::Float(7.0), Tick(7))
        );
    });
}

#[test]
fn bus_register_stays_fresh_while_the_bank_steps_and_stales_once_it_stops() {
    with_server(device_bank(), |server, addr| {
        let model = stale_model(addr);
        let driver = build_driver(&model);
        let mut executor = assemble(&model, &ComponentRegistry::new(), &driver).unwrap();

        // The device wrote the register at bank tick 0 — and the field
        // keeps stepping: each fan-out step re-stamps the held sample,
        // so every scan's report changes and the budgeted point stays
        // Good.
        server
            .bank()
            .write(LEVEL_REGISTER, Value::Float(6.5))
            .unwrap();
        for _ in 0..3 {
            driver.step(0.1).unwrap();
            executor.scan();
        }
        assert_eq!(
            image_sample(&executor, LEVEL_RAW),
            Sample::good(Value::Float(6.5), Tick(3))
        );

        // The bank stops stepping — the held report freezes and the
        // run's lag accrues. Scans 4 through 6 lag by one, two, and
        // three ticks — inside the budget of 3.
        executor.run(3);
        assert_eq!(
            image_sample(&executor, LEVEL_RAW),
            Sample::good(Value::Float(6.5), Tick(6))
        );

        // Scan 7's lag of 4 exceeds the budget — the register-held
        // value lands as Uncertain(Stale), stamped at the scan tick.
        executor.scan();
        assert_eq!(
            image_sample(&executor, LEVEL_RAW),
            Sample::new(
                Value::Float(6.5),
                Quality::Uncertain(QualityReason::Stale),
                Tick(7)
            )
        );

        // A fresh device write while the bank stands still is a changed
        // report — Good on the first read.
        server
            .bank()
            .write(LEVEL_REGISTER, Value::Float(7.5))
            .unwrap();
        executor.scan();
        assert_eq!(
            image_sample(&executor, LEVEL_RAW),
            Sample::good(Value::Float(7.5), Tick(8))
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
                executor.scan();
                driver.step(0.1).unwrap();
            }
            serde_json::to_string(&executor.snapshot()).unwrap()
        })
    };
    assert_eq!(run(), run());
}
