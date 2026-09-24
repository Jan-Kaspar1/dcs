//! Loopback-TCP tests for `sim-cyclic`'s [`CyclicBusDriver`]: the
//! cyclic process-image contract exercised over the real register
//! protocol against a live [`BusServer`], mirroring the executor's
//! `CyclicStub` matrix — one exchange per scan at the boundary, the
//! held input image aging under its freshness budget, the declared
//! `exchange_miss_threshold` escalation, station-attributed and
//! unattributable short exchanges, staged-output retention across a
//! failed exchange, and the exchange counters on the diagnostics
//! surface.

use dcs_core::{
    Command, CyclicIoDriver, Direction, ExchangeDiagnostics, IoDriver, IoError, LinkState, PointId,
    Quality, QualityReason, Sample, Tick, Value, ValueKind,
};
use dcs_runtime::{
    Component, ComponentIo, ComponentIoExt, Executor, IoRequirement, PointMap, StepError, WriteGate,
};
use dcs_sim_bus::{
    BusDriver, BusServer, CyclicBusDriver, CyclicPoint, ExchangeOutcome, PointRegister,
    RegisterBank, RegisterDecl,
};
use std::collections::{BTreeMap, BTreeSet};
use std::net::SocketAddr;
use std::thread;
use std::time::Duration;

/// The fixture register bank: two float registers on station `inlet`,
/// a bool and a float on `outlet` — enough surface for whole-station
/// and partial-station shortfalls.
fn fixture_decls() -> Vec<RegisterDecl> {
    vec![
        RegisterDecl {
            register: 4,
            initial: Value::Float(0.0),
        },
        RegisterDecl {
            register: 5,
            initial: Value::Float(0.0),
        },
        RegisterDecl {
            register: 7,
            initial: Value::Bool(false),
        },
        RegisterDecl {
            register: 9,
            initial: Value::Float(0.0),
        },
    ]
}

/// The fixture station layout: `inlet` holds registers 4 and 5 —
/// two registers, so a shortfall naming only one is unattributable —
/// and `outlet` holds 7 and 9.
fn fixture_stations() -> BTreeMap<String, BTreeSet<u16>> {
    [
        ("inlet".to_string(), BTreeSet::from([4, 5])),
        ("outlet".to_string(), BTreeSet::from([7, 9])),
    ]
    .into_iter()
    .collect()
}

/// The fixture point map: `In` points 10 and 11 read inlet registers 4
/// and 5, `In` point 12 reads outlet register 7, and `Out` point 20
/// stages outlet register 9.
fn fixture_points() -> Vec<CyclicPoint> {
    vec![
        CyclicPoint {
            point: PointId(10),
            register: 4,
            direction: Direction::In,
            kind: ValueKind::Float,
        },
        CyclicPoint {
            point: PointId(11),
            register: 5,
            direction: Direction::In,
            kind: ValueKind::Float,
        },
        CyclicPoint {
            point: PointId(12),
            register: 7,
            direction: Direction::In,
            kind: ValueKind::Bool,
        },
        CyclicPoint {
            point: PointId(20),
            register: 9,
            direction: Direction::Out,
            kind: ValueKind::Float,
        },
    ]
}

/// The declared `exchange_miss_threshold` every fixture driver runs
/// under — three consecutive misses escalate reads.
const MISS_THRESHOLD: u64 = 3;
/// A short request timeout keeps the scripted-miss path fast: the
/// failed connection drops outright rather than waiting a real read
/// timeout out.
const TIMEOUT: Duration = Duration::from_millis(500);

/// `shutdown` on drop, so a panicking test still lets the scoped serve
/// thread exit instead of hanging the scope's join.
struct ShutdownOnDrop<'s>(&'s BusServer);

impl Drop for ShutdownOnDrop<'_> {
    fn drop(&mut self) {
        self.0.shutdown();
    }
}

/// Serves `decls`'s register bank with the fixture station layout on an
/// ephemeral loopback port for the duration of `test`, then shuts the
/// server down and joins its accept thread.
fn with_server<R>(test: impl FnOnce(&BusServer, SocketAddr) -> R) -> R {
    let bank = RegisterBank::new(fixture_decls()).unwrap();
    let server = BusServer::bind_stationed(("127.0.0.1", 0), bank, fixture_stations()).unwrap();
    let addr = server.local_addr().unwrap();
    thread::scope(|scope| {
        scope.spawn(|| server.serve());
        let _guard = ShutdownOnDrop(&server);
        test(&server, addr)
    })
}

/// Connects the fixture driver to `addr`.
fn driver(addr: SocketAddr) -> CyclicBusDriver {
    CyclicBusDriver::connect_with_timeout(
        addr,
        TIMEOUT,
        &fixture_points(),
        &fixture_stations(),
        MISS_THRESHOLD,
    )
    .unwrap()
}

/// The driver's cyclic surface — the boundary the contract lives at.
fn cyclic(driver: &CyclicBusDriver) -> &(dyn CyclicIoDriver + Sync) {
    driver.cyclic().unwrap()
}

/// A raw point-wise attachment — the rig's observer window on the
/// bank and its claim-taker.
fn observer(addr: SocketAddr, points: &[PointRegister]) -> BusDriver {
    BusDriver::connect_with_timeout(addr, TIMEOUT, points).unwrap()
}

#[test]
fn point_access_serves_the_images_and_never_touches_the_wire() {
    with_server(|server, addr| {
        let driver = driver(addr);
        // The connect-time census seeded the held image at Tick::ZERO.
        assert_eq!(
            driver.read(PointId(10)),
            Ok(Sample::good(Value::Float(0.0), Tick::ZERO))
        );

        // A write stages the pending output image — the field register
        // does not move until the next exchange publishes it.
        driver.write(PointId(20), Value::Float(5.0)).unwrap();
        assert_eq!(
            server.bank().read(9).unwrap().value,
            Value::Float(0.0),
            "a staged write must not reach the field before an exchange"
        );
        // …and the Out point's read serves the held image — the field's
        // last-published value — never the pending staged one.
        assert_eq!(
            driver.read(PointId(20)),
            Ok(Sample::good(Value::Float(0.0), Tick::ZERO))
        );

        // A field-side register change is equally invisible to reads:
        // the held input image serves until an exchange latches again.
        server.bank().write(4, Value::Float(7.0)).unwrap();
        assert_eq!(
            driver.read(PointId(10)),
            Ok(Sample::good(Value::Float(0.0), Tick::ZERO)),
            "a read must serve the held image, not the field"
        );

        // One exchange: the staged output publishes and the census
        // latches the field — both at the exchange's tick.
        cyclic(&driver).exchange(Tick(1)).unwrap();
        assert_eq!(server.bank().read(9).unwrap().value, Value::Float(5.0));
        assert_eq!(
            driver.read(PointId(10)),
            Ok(Sample::good(Value::Float(7.0), Tick(1)))
        );
        // The completed exchange's census echoes the published output
        // back into the held image — the Out read now reports the
        // field's value.
        assert_eq!(
            driver.read(PointId(20)),
            Ok(Sample::good(Value::Float(5.0), Tick(1)))
        );
    });
}

#[test]
fn a_missed_exchange_holds_the_image_and_escalates_at_the_threshold() {
    with_server(|server, addr| {
        let driver = driver(addr);
        server.bank().write(4, Value::Float(7.0)).unwrap();
        cyclic(&driver).exchange(Tick(1)).unwrap();
        assert_eq!(driver.read(PointId(10)).unwrap().value, Value::Float(7.0));

        // The field moves on — the held image cannot see it until an
        // exchange latches again.
        server.bank().write(4, Value::Float(9.0)).unwrap();

        driver
            .script_exchange(&[
                ExchangeOutcome::Miss,
                ExchangeOutcome::Miss,
                ExchangeOutcome::Miss,
            ])
            .unwrap();

        // The first two misses: the exchange reports the boundary
        // failure once each — attributed to the lowest served point —
        // the held image still serves the last latched value, and the
        // link reports its degradation.
        for tick in [2, 3] {
            assert_eq!(
                cyclic(&driver).exchange(Tick(tick)),
                Err(IoError::Disconnected(PointId(10))),
                "tick {tick}: the failed exchange must name its boundary"
            );
            assert_eq!(
                driver.read(PointId(10)),
                Ok(Sample::good(Value::Float(7.0), Tick(1))),
                "tick {tick}: the held image still serves"
            );
        }
        assert!(!driver.connected(), "a scripted miss drops the link");
        let diagnostics = driver.diagnostics().unwrap();
        assert_eq!(diagnostics.link, LinkState::Disconnected);
        assert!(diagnostics.last_error.is_some());
        assert_eq!(
            diagnostics.exchange.unwrap(),
            ExchangeDiagnostics {
                attempted: 3,
                succeeded: 1,
                working_counter_mismatches: 0,
                last_exchange_tick: Some(Tick(1)),
                missed_deadlines: 0,
            }
        );

        // The third miss reaches the declared threshold: every point's
        // read escalates to `Disconnected` — the whole image, both
        // directions, every station.
        assert_eq!(
            cyclic(&driver).exchange(Tick(4)),
            Err(IoError::Disconnected(PointId(10)))
        );
        for point in [10, 11, 12, 20] {
            assert_eq!(
                driver.read(PointId(point)),
                Err(IoError::Disconnected(PointId(point))),
                "point {point} must escalate at the miss threshold"
            );
        }

        // The script exhausted, the next exchange reconnects lazily and
        // completes: misses reset, the field's asserted value latches
        // fresh, the link recovers.
        cyclic(&driver).exchange(Tick(5)).unwrap();
        assert_eq!(
            driver.read(PointId(10)),
            Ok(Sample::good(Value::Float(9.0), Tick(5)))
        );
        assert_eq!(driver.diagnostics().unwrap().link, LinkState::Connected);
    });
}

#[test]
fn the_staged_output_image_is_retained_across_a_failed_exchange() {
    with_server(|server, addr| {
        let driver = driver(addr);
        driver.write(PointId(20), Value::Float(9.0)).unwrap();
        driver.script_exchange(&[ExchangeOutcome::Miss]).unwrap();

        // The failed exchange publishes nothing; the staged image is
        // retained — the next completed exchange still carries it.
        assert!(cyclic(&driver).exchange(Tick(1)).is_err());
        assert_eq!(
            server.bank().read(9).unwrap().value,
            Value::Float(0.0),
            "a failed exchange must not publish the staged image"
        );

        cyclic(&driver).exchange(Tick(2)).unwrap();
        assert_eq!(
            server.bank().read(9).unwrap().value,
            Value::Float(9.0),
            "the retained staged image publishes on the next completed exchange"
        );
    });
}

#[test]
fn a_station_short_exchange_degrades_only_that_station() {
    with_server(|server, addr| {
        let driver = driver(addr);
        server.bank().write(4, Value::Float(7.0)).unwrap();
        server.bank().write(7, Value::Bool(true)).unwrap();
        cyclic(&driver).exchange(Tick(1)).unwrap();

        // A staged output on an inlet register plus one on the outlet:
        // the shortfall must withhold only the named station's.
        driver.write(PointId(11), Value::Float(3.5)).unwrap();
        driver.write(PointId(20), Value::Float(8.0)).unwrap();
        driver
            .script_exchange(&[ExchangeOutcome::ShortStation {
                station: "inlet".to_string(),
            }])
            .unwrap();

        // The exchange completes — the boundary counts no failure —
        // but the withheld census attributes to `inlet`: its points
        // escalate while the outlet's serve freshly latched data.
        cyclic(&driver).exchange(Tick(2)).unwrap();
        for point in [10, 11] {
            assert_eq!(
                driver.read(PointId(point)),
                Err(IoError::Disconnected(PointId(point))),
                "point {point}: station inlet must escalate"
            );
        }
        assert_eq!(
            driver.read(PointId(12)),
            Ok(Sample::good(Value::Bool(true), Tick(2))),
            "the answered station latches fresh"
        );
        // The outlet's staged output published; the withheld inlet
        // register's did not — its register keeps the bank's value and
        // stays dirty for the next exchange.
        assert_eq!(server.bank().read(9).unwrap().value, Value::Float(8.0));
        assert_eq!(server.bank().read(5).unwrap().value, Value::Float(0.0));

        let diagnostics = driver.diagnostics().unwrap();
        // The link stayed up — the completed exchange named its
        // shortfall — and the exchange section counts the mismatch.
        assert_eq!(diagnostics.link, LinkState::Connected);
        assert_eq!(
            diagnostics.last_error.as_deref(),
            Some("working counter shortfall attributed to station \"inlet\"")
        );
        let exchange = diagnostics.exchange.unwrap();
        assert_eq!(exchange.working_counter_mismatches, 1);
        assert_eq!(exchange.last_exchange_tick, Some(Tick(2)));

        // A clean exchange clears the shortfall: inlet's points serve
        // fresh data and the retained staged output finally publishes.
        cyclic(&driver).exchange(Tick(3)).unwrap();
        assert_eq!(
            driver.read(PointId(10)),
            Ok(Sample::good(Value::Float(7.0), Tick(3)))
        );
        assert_eq!(server.bank().read(5).unwrap().value, Value::Float(3.5));
    });
}

#[test]
fn an_unattributable_short_exchange_degrades_the_whole_image() {
    with_server(|_server, addr| {
        let driver = driver(addr);
        cyclic(&driver).exchange(Tick(1)).unwrap();

        // Withholding register 4 alone cannot name a station — `inlet`
        // holds 4 and 5 — so the shortfall degrades the whole device.
        driver
            .script_exchange(&[ExchangeOutcome::ShortRegisters { registers: vec![4] }])
            .unwrap();
        cyclic(&driver).exchange(Tick(2)).unwrap();

        for point in [10, 11, 12, 20] {
            assert_eq!(
                driver.read(PointId(point)),
                Err(IoError::Disconnected(PointId(point))),
                "point {point}: an unattributable shortfall must degrade the whole image"
            );
        }
        let diagnostics = driver.diagnostics().unwrap();
        assert_eq!(diagnostics.link, LinkState::Connected);
        assert_eq!(
            diagnostics.last_error.as_deref(),
            Some("unattributable working counter shortfall")
        );
        assert_eq!(diagnostics.exchange.unwrap().working_counter_mismatches, 1);

        cyclic(&driver).exchange(Tick(3)).unwrap();
        assert_eq!(driver.read(PointId(12)).unwrap().quality, Quality::Good);
    });
}

#[test]
fn a_late_exchange_completes_and_counts_the_missed_deadline() {
    with_server(|server, addr| {
        let driver = driver(addr);
        server.bank().write(4, Value::Float(6.0)).unwrap();
        driver.script_exchange(&[ExchangeOutcome::Late]).unwrap();

        cyclic(&driver).exchange(Tick(1)).unwrap();
        // The late answer still latches — and reports its deadline.
        assert_eq!(
            driver.read(PointId(10)),
            Ok(Sample::good(Value::Float(6.0), Tick(1)))
        );
        assert_eq!(
            driver
                .diagnostics()
                .unwrap()
                .exchange
                .unwrap()
                .missed_deadlines,
            1
        );
    });
}

#[test]
fn a_fenced_exchange_completes_nothing_but_the_census_stays_open() {
    with_server(|server, addr| {
        let driver = driver(addr);
        let tracking = CyclicBusDriver::connect_with_timeout(
            addr,
            TIMEOUT,
            &fixture_points(),
            &fixture_stations(),
            MISS_THRESHOLD,
        )
        .unwrap();
        // Another attachment owns the field's write claim.
        let owner = observer(
            addr,
            &[PointRegister {
                point: PointId(10),
                register: 4,
                kind: ValueKind::Float,
            }],
        );
        owner.claim_writer(7).unwrap();

        // A staged write makes this attachment's exchange a field-
        // mutating one: the fenced verdict completes nothing — a named
        // miss, the staged image retained.
        driver.write(PointId(20), Value::Float(5.0)).unwrap();
        assert_eq!(
            cyclic(&driver).exchange(Tick(1)),
            Err(IoError::Fenced(PointId(10)))
        );
        assert_eq!(server.bank().read(9).unwrap().value, Value::Float(0.0));

        // The tracking peer's census-only exchange — the standby's,
        // which stages nothing — stays open under the claim: it
        // latches fresh inputs while never publishing.
        server.bank().write(4, Value::Float(8.0)).unwrap();
        cyclic(&tracking).exchange(Tick(1)).unwrap();
        assert_eq!(
            tracking.read(PointId(10)),
            Ok(Sample::good(Value::Float(8.0), Tick(1)))
        );

        // Releasing the claim reopens the field: the retained staged
        // image publishes on the next exchange.
        owner.release_writer().unwrap();
        cyclic(&driver).exchange(Tick(2)).unwrap();
        assert_eq!(server.bank().read(9).unwrap().value, Value::Float(5.0));
    });
}

#[test]
fn a_closed_gate_exchanges_but_never_stages() {
    with_server(|server, addr| {
        let driver = driver(addr);
        let gate = WriteGate::closed(&driver);
        server.bank().write(4, Value::Float(6.0)).unwrap();

        // The gate passes the cyclic surface through but drops the
        // write before it can stage: the exchange is census-only, so
        // the standby latches fresh inputs and publishes nothing.
        gate.write(PointId(20), Value::Float(9.0)).unwrap();
        gate.cyclic().unwrap().exchange(Tick(1)).unwrap();
        assert_eq!(
            gate.read(PointId(10)),
            Ok(Sample::good(Value::Float(6.0), Tick(1)))
        );
        assert_eq!(
            server.bank().read(9).unwrap().value,
            Value::Float(0.0),
            "a gated write must never stage, so never publish"
        );

        // Opening the gate lets the next write stage — and the next
        // exchange publish.
        gate.open();
        gate.write(PointId(20), Value::Float(9.0)).unwrap();
        gate.cyclic().unwrap().exchange(Tick(2)).unwrap();
        assert_eq!(server.bank().read(9).unwrap().value, Value::Float(9.0));
    });
}

#[test]
fn the_connect_census_probes_the_declared_image() {
    // A server missing a declared register.
    let bank = RegisterBank::new([RegisterDecl {
        register: 5,
        initial: Value::Float(0.0),
    }])
    .unwrap();
    let server = BusServer::bind_stationed(("127.0.0.1", 0), bank, fixture_stations()).unwrap();
    let addr = server.local_addr().unwrap();
    thread::scope(|scope| {
        scope.spawn(|| server.serve());
        let _guard = ShutdownOnDrop(&server);
        let error = CyclicBusDriver::connect_with_timeout(
            addr,
            TIMEOUT,
            &fixture_points(),
            &fixture_stations(),
            MISS_THRESHOLD,
        )
        .unwrap_err();
        assert!(
            error.to_string().contains("register 4"),
            "{error} must name the missing register"
        );
    });

    // A register whose kind disagrees with the declaration.
    let bank = RegisterBank::new([
        RegisterDecl {
            register: 4,
            initial: Value::Bool(false),
        },
        RegisterDecl {
            register: 5,
            initial: Value::Float(0.0),
        },
        RegisterDecl {
            register: 7,
            initial: Value::Bool(false),
        },
        RegisterDecl {
            register: 9,
            initial: Value::Float(0.0),
        },
    ])
    .unwrap();
    let server = BusServer::bind_stationed(("127.0.0.1", 0), bank, fixture_stations()).unwrap();
    let addr = server.local_addr().unwrap();
    thread::scope(|scope| {
        scope.spawn(|| server.serve());
        let _guard = ShutdownOnDrop(&server);
        let error = CyclicBusDriver::connect_with_timeout(
            addr,
            TIMEOUT,
            &fixture_points(),
            &fixture_stations(),
            MISS_THRESHOLD,
        )
        .unwrap_err();
        assert!(
            error.to_string().contains("register 4"),
            "{error} must name the kind-mismatched register"
        );
    });

    // A threshold of zero escalates every read — rejected up front.
    with_server(|_, addr| {
        assert!(
            CyclicBusDriver::connect_with_timeout(
                addr,
                TIMEOUT,
                &fixture_points(),
                &fixture_stations(),
                0,
            )
            .is_err()
        );
    });
}

#[test]
fn point_semantics_match_any_driver() {
    with_server(|_, addr| {
        let driver = driver(addr);
        assert_eq!(
            driver.read(PointId(99)),
            Err(IoError::UnknownPoint(PointId(99)))
        );
        assert_eq!(
            driver.write(PointId(99), Value::Float(1.0)),
            Err(IoError::UnknownPoint(PointId(99)))
        );
        assert_eq!(
            driver.write(PointId(10), Value::Bool(true)),
            Err(IoError::TypeMismatch {
                point: PointId(10),
                expected: ValueKind::Float,
                found: Value::Bool(true),
            })
        );
    });
}

// ── The executor boundary over the real transport ───────────────────

/// A component writing `value` to `output` once — on its first step
/// only, so a later publish proves the staged image survived, not a
/// restage.
struct WriteOnce {
    output: PointId,
    value: f64,
    done: bool,
}

impl Component for WriteOnce {
    fn name(&self) -> &str {
        "once"
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        vec![IoRequirement::output::<f64>("out", self.output)]
    }

    fn step(&mut self, io: &dyn ComponentIo, _tick: Tick) -> Result<(), StepError> {
        if !self.done {
            self.done = true;
            io.write_typed(self.output, self.value)?;
        }
        Ok(())
    }
}

/// The point map the executor tests run: the fixture points, with a
/// freshness budget on point 10 and `writable` on point 11 for the
/// command path.
fn point_map() -> PointMap {
    use dcs_runtime::PointSpec;
    PointMap::new()
        .with_spec(
            PointId(10),
            PointSpec {
                direction: Direction::In,
                kind: ValueKind::Float,
                internal: None,
                writable: false,
                requires_reason: false,
                stale_after_ticks: Some(1),
                journaled: false,
            },
        )
        .with_writable_point(PointId(11), Direction::In, ValueKind::Float)
        .with_point(PointId(12), Direction::In, ValueKind::Bool)
        .with_point(PointId(20), Direction::Out, ValueKind::Float)
}

#[test]
fn the_executor_runs_the_exchange_at_the_boundary_over_the_wire() {
    with_server(|server, addr| {
        let driver = driver(addr);
        server.bank().write(4, Value::Float(7.0)).unwrap();
        let mut executor = Executor::new(
            &driver,
            point_map(),
            vec![Box::new(WriteOnce {
                output: PointId(20),
                value: 9.0,
                done: false,
            })],
        )
        .unwrap();

        // A command submitted between scans applies at the next scan's
        // boundary — its driver write stages into the output image
        // before the exchange runs, so it publishes in that same
        // exchange: the command path's one-boundary actuation.
        executor.submit_command(Command::WriteValue {
            point: PointId(11),
            kind: ValueKind::Float,
            value: Value::Float(3.0),
        });
        executor.scan();
        assert_eq!(server.bank().read(5).unwrap().value, Value::Float(3.0));
        // The input image latched the census at the scan's tick.
        assert_eq!(
            executor.sample(PointId(10)),
            Some(Sample::good(Value::Float(7.0), Tick(1)))
        );

        // Scan 1's write phase staged point 20's output — the field
        // carries the initial value until scan 2's exchange publishes
        // it: the documented one-scan actuation delay.
        assert_eq!(server.bank().read(9).unwrap().value, Value::Float(0.0));
        executor.scan();
        assert_eq!(server.bank().read(9).unwrap().value, Value::Float(9.0));

        // A scripted miss: the exchange counts once at the boundary,
        // the held image still serves — the acquisition stamp lags by
        // one, inside the declared freshness budget.
        driver
            .script_exchange(&[ExchangeOutcome::Miss, ExchangeOutcome::Miss])
            .unwrap();
        executor.scan();
        let snapshot = executor.snapshot();
        assert_eq!(snapshot.io_health.failed_exchanges, 1);
        assert_eq!(snapshot.io_health.failed_reads, 0);
        assert_eq!(
            executor.sample(PointId(10)).unwrap(),
            Sample::good(Value::Float(7.0), Tick(3))
        );

        // The second miss ages the held sample past its budget — the
        // landed quality merges Uncertain(Stale).
        executor.scan();
        let sample = executor.sample(PointId(10)).unwrap();
        assert_eq!(sample.value, Value::Float(7.0));
        assert_eq!(sample.quality, Quality::Uncertain(QualityReason::Stale));

        // The exchange section reaches the served diagnostics surface:
        // five attempted, four completed, the shortfall counter clean.
        let exchange = executor
            .snapshot()
            .io_health
            .driver
            .unwrap()
            .exchange
            .unwrap();
        assert_eq!(exchange.attempted, 4);
        assert_eq!(exchange.succeeded, 2);
        assert_eq!(exchange.last_exchange_tick, Some(Tick(2)));
    });
}
