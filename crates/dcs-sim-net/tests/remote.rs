//! Loopback-TCP integration tests for `dcs-sim-net`: the remote driver
//! against a live `PlantServer`, the shared plant two clients observe,
//! failure mapping at the `IoDriver` boundary, and identical behavior for
//! a scripted executor run local and remote.

use dcs_core::{
    Direction, DriverDiagnostics, FieldClaim, IoDriver, IoError, IoFault, LinkState, PointId,
    Quality, QualityReason, Role, Sample, StandbySync, SwitchError, Tick, Value, ValueKind,
};
use dcs_runtime::{
    Component, ComponentIo, ComponentIoExt, Executor, IoRequirement, Peer, PointMap, PointSpec,
    StepError, WriteGate,
};
use dcs_sim::{
    BoolFlow, ChannelId, ChannelMap, Fault, FirstOrderLag, FlowSum, Integrator, Loopback,
    PointBinding, ProcessElement, SimDriver,
};
use dcs_sim_net::{ClaimGrant, PlantError, PlantResponse, PlantServer, RemoteDriver, RemoteError};
use std::io::{self, BufRead, BufReader, Write};
use std::net::{IpAddr, Ipv4Addr, SocketAddr, TcpListener, TcpStream};
use std::thread;
use std::time::Duration;

fn binding(point: u64, direction: Direction, initial: Value) -> PointBinding {
    PointBinding {
        point: PointId(point),
        channel: ChannelId {
            device: 1,
            name: format!("ch{point}"),
        },
        direction,
        initial,
    }
}

/// A simple plant: the `Out` point 20 loops back onto the `In` point 10
/// at the next step.
fn loopback_map() -> ChannelMap {
    ChannelMap::new()
        .with_point(binding(10, Direction::In, Value::Float(0.0)))
        .with_point(binding(20, Direction::Out, Value::Float(0.0)))
        .with_loopback(Loopback {
            output: PointId(20),
            input: PointId(10),
        })
}

/// A plant whose `In` point is a bare channel — no loopback routes onto
/// it and no element output owns it: the scanned-input-card shape the
/// driver's step re-stamps every tick. Point 20 stays bound so the
/// `Accumulator` component's output write has a field point to land on.
fn bare_map() -> ChannelMap {
    ChannelMap::new()
        .with_point(binding(10, Direction::In, Value::Float(0.0)))
        .with_point(binding(20, Direction::Out, Value::Float(0.0)))
}

/// The scripted executor scenario: a stateful component reads the
/// lag-driven `In` point 1 and drives the `Out` point 2 feeding it —
/// a closed loop through the simulated field.
fn scenario_map() -> ChannelMap {
    ChannelMap::new()
        .with_point(binding(1, Direction::In, Value::Float(0.0)))
        .with_point(binding(2, Direction::Out, Value::Float(0.0)))
        .with_element(ProcessElement::FirstOrderLag(FirstOrderLag {
            input: PointId(2),
            output: PointId(1),
            time_constant: 1.0,
            initial: 2.0,
        }))
}

/// A stateful test component: integrates its input and writes the running
/// total, so a transport difference would show up in the trace.
struct Accumulator {
    input: PointId,
    output: PointId,
    total: f64,
}

impl Component for Accumulator {
    fn name(&self) -> &str {
        "accumulator"
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        vec![
            IoRequirement::input::<f64>("in", self.input),
            IoRequirement::output::<f64>("out", self.output),
        ]
    }

    fn step(&mut self, io: &dyn ComponentIo, _tick: Tick) -> Result<(), StepError> {
        let input = io.read_typed::<f64>(self.input)?;
        if input.quality.is_good() {
            self.total += input.value;
        }
        io.write_typed::<f64>(self.output, self.total)?;
        Ok(())
    }
}

/// Runs the scripted scenario against `driver`: 20 scans of an
/// `Accumulator` wired to the scenario's point map, each scan followed by
/// one plant step through `step`. Returns the executor-image sample
/// sequence — the component-visible behavior the driver produced.
fn scripted_run(driver: &(dyn IoDriver + Sync), mut step: impl FnMut(f64) -> Tick) -> Vec<Sample> {
    let point_map: PointMap = [
        (PointId(1), dcs_core::Direction::In, ValueKind::Float),
        (PointId(2), dcs_core::Direction::Out, ValueKind::Float),
    ]
    .into_iter()
    .collect();
    let mut executor = Executor::new(
        driver,
        point_map,
        vec![Box::new(Accumulator {
            input: PointId(1),
            output: PointId(2),
            total: 0.0,
        })],
    )
    .unwrap();
    let mut trace = Vec::new();
    for _ in 0..20 {
        executor.scan();
        step(0.1);
        trace.push(executor.sample(PointId(1)).unwrap());
        trace.push(executor.sample(PointId(2)).unwrap());
    }
    trace
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
fn with_server<R>(map: ChannelMap, test: impl FnOnce(SocketAddr) -> R) -> R {
    let server = PlantServer::bind(("127.0.0.1", 0), SimDriver::new(map).unwrap()).unwrap();
    let addr = server.local_addr().unwrap();
    thread::scope(|scope| {
        scope.spawn(|| server.serve());
        let _guard = ShutdownOnDrop(&server);
        test(addr)
    })
}

#[test]
fn write_then_read_through_the_remote_driver_roundtrips_a_value() {
    with_server(loopback_map(), |addr| {
        let remote = RemoteDriver::connect(addr).unwrap();
        // The field fails closed while unclaimed: the test attachment
        // owns the claim its mutations ride under.
        remote.claim_writer(1).unwrap();
        let driver: &dyn IoDriver = &remote;

        driver.write(PointId(20), Value::Float(3.5)).unwrap();
        let sample = driver.read(PointId(20)).unwrap();
        assert_eq!(sample.value, Value::Float(3.5));
        assert!(sample.quality.is_good());

        // The typed handles over the remote driver decode identically.
        assert_eq!(
            dcs_core::Input::<f64>::new(driver, PointId(20))
                .read()
                .unwrap()
                .value,
            3.5
        );
        dcs_core::Output::<f64>::new(driver, PointId(20))
            .write(4.5)
            .unwrap();
        assert_eq!(
            dcs_core::Input::<f64>::new(driver, PointId(20))
                .read()
                .unwrap()
                .value,
            4.5
        );
    });
}

#[test]
fn a_second_client_observes_the_same_stepped_point_values() {
    with_server(loopback_map(), |addr| {
        let active = RemoteDriver::connect(addr).unwrap();
        let standby = RemoteDriver::connect(addr).unwrap();
        active.claim_writer(1).unwrap();

        active.write(PointId(20), Value::Float(3.5)).unwrap();
        let tick = active.step(0.1).unwrap();

        // The step routed 20's write onto 10; both clients read the same
        // stored sample — the standby's view of the field is the active's.
        let seen_by_active = active.read(PointId(10)).unwrap();
        let seen_by_standby = standby.read(PointId(10)).unwrap();
        assert_eq!(seen_by_active, seen_by_standby);
        assert_eq!(seen_by_active.value, Value::Float(3.5));
        assert_eq!(seen_by_active.tick, tick);

        // An injected fault is shared field state too.
        active
            .inject_fault(
                PointId(10),
                Fault::Quality(Quality::Bad(QualityReason::DeviceFault)),
            )
            .unwrap();
        assert_eq!(
            standby.read(PointId(10)).unwrap().quality,
            Quality::Bad(QualityReason::DeviceFault)
        );
    });
}

#[test]
fn ping_reports_the_plants_tick_on_every_attachment() {
    with_server(loopback_map(), |addr| {
        // The container health contract's probe: the server answers
        // its plant tick to an attachment holding no claim — the
        // liveness half is the answer at all, the tick the freshness
        // half.
        let probe = RemoteDriver::connect(addr).unwrap();
        assert_eq!(probe.ping().unwrap(), Tick::ZERO);

        // A stepping plant's tick advances between probes — and a
        // non-owning attachment's probe reads the same freshness.
        let owner = RemoteDriver::connect(addr).unwrap();
        owner.claim_writer(1).unwrap();
        owner.step(0.5).unwrap();
        assert_eq!(owner.ping().unwrap(), Tick(1));
        assert_eq!(probe.ping().unwrap(), Tick(1));
    });
}

#[test]
fn protocol_answers_map_to_named_io_errors() {
    with_server(loopback_map(), |addr| {
        let remote = RemoteDriver::connect(addr).unwrap();
        // Claimed before any mutation — the fail-closed field refuses a
        // claim-less attachment before the kind check could run.
        remote.claim_writer(1).unwrap();
        let driver: &dyn IoDriver = &remote;

        // The server's `IoError`s arrive unchanged.
        assert_eq!(
            driver.read(PointId(99)),
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
        assert_eq!(
            remote.inject_fault(PointId(99), Fault::Timeout),
            Err(RemoteError::Io(IoError::UnknownPoint(PointId(99))))
        );

        // Faults injected through the protocol surface as their `IoError`.
        remote
            .inject_fault(PointId(10), Fault::Disconnected)
            .unwrap();
        assert_eq!(
            driver.read(PointId(10)),
            Err(IoError::Disconnected(PointId(10)))
        );
        remote.inject_fault(PointId(10), Fault::Timeout).unwrap();
        assert_eq!(
            driver.write(PointId(10), Value::Float(1.0)),
            Err(IoError::Timeout(PointId(10)))
        );
        remote.clear_fault(PointId(10)).unwrap();
        assert_eq!(driver.read(PointId(10)).unwrap().quality, Quality::Good);

        // An invalid step is a named refusal — the server never panics —
        // and the connection stays usable.
        assert!(matches!(
            remote.step(-0.1),
            Err(RemoteError::InvalidRequest(_))
        ));
        assert!(matches!(
            remote.step(f64::NAN),
            Err(RemoteError::InvalidRequest(_))
        ));
        assert_eq!(remote.step(0.1), Ok(Tick(1)));
    });
}

/// An integrator plant: point 1 is the writable inflow, point 10 the
/// level it integrates — the shape the QA reproduction drives.
fn integrator_map() -> ChannelMap {
    ChannelMap::new()
        .with_point(binding(1, Direction::In, Value::Float(0.0)))
        .with_point(binding(10, Direction::In, Value::Float(0.0)))
        .with_element(ProcessElement::Integrator(Integrator {
            input: PointId(1),
            output: PointId(10),
            initial: 0.0,
        }))
}

#[test]
fn a_legal_step_that_overflows_an_element_degrades_the_sample_and_recovers() {
    // QA `sim-net-nonfinite-plant-state-poisons-wire-permanently`:
    // claim -> write 1e308 -> step dt=1e308 drove the integrator's
    // accumulator past the f64 range; the stored non-finite sample
    // serialized `{"float":null}` — a frame this protocol's own client
    // must reject — and the element stayed corrupt under every later
    // step until the server restarted. The field now holds the last
    // finite state instead: the served sample decodes, marked
    // `Bad`/`out_of_range`, and a finite input write plus a finite
    // step recovers the element in place.
    with_server(integrator_map(), |addr| {
        let remote = RemoteDriver::connect(addr).unwrap();
        remote.claim_writer(1).unwrap();
        let driver: &dyn IoDriver = &remote;

        driver.write(PointId(1), Value::Float(1e308)).unwrap();
        remote.step(1e308).unwrap();
        remote.step(1e308).unwrap();

        // The frame decodes — the defect's Disconnected is gone — and
        // the overflowed point reports its held finite value bad
        // instead of an unrepresentable one.
        let sample = driver.read(PointId(10)).unwrap();
        assert_eq!(sample.value, Value::Float(0.0));
        assert_eq!(sample.quality, Quality::Bad(QualityReason::OutOfRange));

        // The census decodes wholesale — one bad point no longer
        // poisons the whole response.
        assert_eq!(remote.list_points().unwrap().len(), 2);

        // A second attachment reads the same decodable degradation.
        let standby = RemoteDriver::connect(addr).unwrap();
        assert_eq!(standby.read(PointId(10)).unwrap(), sample);

        // The documented recovery path: a finite input write followed
        // by a finite step resumes the element from the state it held —
        // no plant restart.
        driver.write(PointId(1), Value::Float(2.0)).unwrap();
        remote.step(1.0).unwrap();
        let recovered = driver.read(PointId(10)).unwrap();
        assert_eq!(recovered.value, Value::Float(2.0));
        assert!(recovered.quality.is_good());
    });
}

#[test]
fn a_non_finite_write_payload_is_refused_at_the_field_boundary() {
    with_server(loopback_map(), |addr| {
        // The wire is symmetric: JSON cannot spell a non-finite f64,
        // and serde_json refuses an out-of-range literal at parse.
        // A `{"float": 1e999}` write dies as InvalidRequest before any
        // driver call — the stored sample is untouched and the
        // connection stays up.
        let mut stream = BufReader::new(TcpStream::connect(addr).unwrap());
        stream
            .get_mut()
            .write_all(b"{\"op\":\"claim_writer\",\"owner\":1}\n")
            .unwrap();
        let mut line = String::new();
        stream.read_line(&mut line).unwrap();
        assert!(matches!(
            serde_json::from_str::<PlantResponse>(&line).unwrap(),
            PlantResponse::Done
        ));
        stream
            .get_mut()
            .write_all(b"{\"op\":\"write\",\"point\":10,\"value\":{\"float\":1e999}}\n")
            .unwrap();
        line.clear();
        stream.read_line(&mut line).unwrap();
        assert!(matches!(
            serde_json::from_str::<PlantResponse>(&line).unwrap(),
            PlantResponse::Error {
                error: PlantError::InvalidRequest { .. }
            }
        ));

        let remote = RemoteDriver::connect(addr).unwrap();
        assert_eq!(remote.read(PointId(10)).unwrap().value, Value::Float(0.0));
    });
}

/// The pump-station dynamics chain the QA lane deploys: the gate
/// points 100/101 drive the pump draws 20/21, the sum of the draws
/// with the writable inflow 12 stands on net flow 13, the level
/// integrator 10 accumulates it, and the backup lag 11 follows — the
/// `pump_station_dynamics` fixture's shape, points included.
fn pump_dynamics_map() -> ChannelMap {
    ChannelMap::new()
        .with_point(binding(10, Direction::In, Value::Float(0.8)))
        .with_point(binding(11, Direction::In, Value::Float(0.8)))
        .with_point(binding(12, Direction::In, Value::Float(0.0)))
        .with_point(binding(13, Direction::In, Value::Float(0.2)))
        .with_point(binding(20, Direction::In, Value::Float(0.0)))
        .with_point(binding(21, Direction::In, Value::Float(0.0)))
        .with_point(binding(100, Direction::In, Value::Bool(false)))
        .with_point(binding(101, Direction::In, Value::Bool(false)))
        .with_element(ProcessElement::BoolFlow(BoolFlow {
            input: PointId(100),
            output: PointId(20),
            on_rate: -1.0,
            off_rate: 0.0,
            initial: 0.0,
        }))
        .with_element(ProcessElement::BoolFlow(BoolFlow {
            input: PointId(101),
            output: PointId(21),
            on_rate: -1.0,
            off_rate: 0.0,
            initial: 0.0,
        }))
        .with_element(ProcessElement::FlowSum(FlowSum {
            inputs: vec![PointId(12), PointId(20), PointId(21)],
            output: PointId(13),
            bias: 0.2,
            initial: 0.2,
        }))
        .with_element(ProcessElement::Integrator(Integrator {
            input: PointId(13),
            output: PointId(10),
            initial: 0.8,
        }))
        .with_element(ProcessElement::FirstOrderLag(FirstOrderLag {
            input: PointId(10),
            output: PointId(11),
            time_constant: 0.5,
            initial: 0.8,
        }))
}

#[test]
fn an_overflowed_level_chain_stays_finite_and_decodable_for_fresh_clients() {
    // QA `sim-net-nonfinite-write-poisons-plant-permanently` (verif #4
    // replay): claim -> write inflow 12 = 1e308 -> step until the level
    // integrator overflows -> release -> a fresh client reading 10/11
    // got `{"float":null}` forever — a frame this protocol cannot spell
    // — until the plant restarted. The chain now degrades in place:
    // every stored value stays finite, the overflowed elements report
    // their held state `Bad`/`out_of_range`, and the served frames
    // decode for every later attachment.
    with_server(pump_dynamics_map(), |addr| {
        let remote = RemoteDriver::connect(addr).unwrap();
        remote.claim_writer(1).unwrap();
        let driver: &dyn IoDriver = &remote;

        driver.write(PointId(12), Value::Float(1e308)).unwrap();
        // The first step still sums finite — 13 stands at 1e308 and the
        // integrator commits it — so the second step is the one whose
        // accumulation would pass the f64 range; the element refuses
        // the commit and holds its last finite state instead.
        remote.step(1.0).unwrap();
        remote.step(1.0).unwrap();
        remote.release_writer().unwrap();

        // The overflowed integrator reports the finite level it held,
        // degraded — never an unrepresentable sample.
        let level = driver.read(PointId(10)).unwrap();
        assert_eq!(level.quality, Quality::Bad(QualityReason::OutOfRange));
        let Value::Float(held) = level.value else {
            panic!("a level sample is always a Float")
        };
        assert!(held.is_finite());

        // The downstream lag propagates the input's quality onto its
        // own held finite value — the whole chain degrades, nothing
        // stores non-finite.
        let backup = driver.read(PointId(11)).unwrap();
        assert_eq!(backup.quality, Quality::Bad(QualityReason::OutOfRange));
        let Value::Float(followed) = backup.value else {
            panic!("a level sample is always a Float")
        };
        assert!(followed.is_finite());

        // The defect's verdict: a fresh client connecting after the
        // writer released reads real samples — `{"float":null}` never
        // reaches the wire, on the census either.
        let fresh = RemoteDriver::connect(addr).unwrap();
        assert_eq!(fresh.read(PointId(10)).unwrap(), level);
        assert_eq!(fresh.read(PointId(11)).unwrap(), backup);
        assert!(
            fresh
                .list_points()
                .unwrap()
                .iter()
                .all(|info| match info.sample.value {
                    Value::Float(v) => v.is_finite(),
                    _ => true,
                })
        );

        // A finite inflow write plus a finite step resumes the chain in
        // place — no plant restart.
        remote.claim_writer(2).unwrap();
        driver.write(PointId(12), Value::Float(0.0)).unwrap();
        remote.step(1.0).unwrap();
        assert!(driver.read(PointId(10)).unwrap().quality.is_good());
    });
}

#[test]
fn stopping_the_server_surfaces_disconnected_not_panics() {
    let server =
        PlantServer::bind(("127.0.0.1", 0), SimDriver::new(loopback_map()).unwrap()).unwrap();
    let addr = server.local_addr().unwrap();
    thread::scope(|scope| {
        scope.spawn(|| server.serve());
        let remote = RemoteDriver::connect(addr).unwrap();
        assert!(remote.read(PointId(10)).is_ok());

        server.shutdown();
        // Every access now fails with Disconnected — at the boundary, as
        // named errors — while the dead endpoint's refused re-attach
        // attempts keep the driver down until the plant returns.
        assert_eq!(
            remote.read(PointId(10)),
            Err(IoError::Disconnected(PointId(10)))
        );
        assert_eq!(
            remote.write(PointId(20), Value::Float(1.0)),
            Err(IoError::Disconnected(PointId(20)))
        );
        assert_eq!(remote.step(0.1), Err(RemoteError::Disconnected));
        assert!(!remote.connected());
        assert_eq!(
            remote.read(PointId(10)),
            Err(IoError::Disconnected(PointId(10)))
        );
    });
}

#[test]
fn a_killed_server_reports_link_disconnected_health_in_the_snapshot() {
    let server =
        PlantServer::bind(("127.0.0.1", 0), SimDriver::new(loopback_map()).unwrap()).unwrap();
    let addr = server.local_addr().unwrap();
    thread::scope(|scope| {
        scope.spawn(|| server.serve());
        let remote = RemoteDriver::connect(addr).unwrap();
        // The executor's output writes ride this attachment's claim.
        remote.claim_writer(1).unwrap();
        let point_map: PointMap = [
            (PointId(10), dcs_core::Direction::In, ValueKind::Float),
            (PointId(20), dcs_core::Direction::Out, ValueKind::Float),
        ]
        .into_iter()
        .collect();
        let mut executor = Executor::new(
            &remote,
            point_map,
            vec![Box::new(Accumulator {
                input: PointId(10),
                output: PointId(20),
                total: 0.0,
            })],
        )
        .unwrap();

        executor.scan();
        // A live link reports connected with no failure history — the
        // driver's own diagnostics surface, beside the counters.
        assert_eq!(
            executor.snapshot().io_health.driver,
            Some(DriverDiagnostics {
                link: LinkState::Connected,
                last_error: None,
                exchange: None,
            })
        );

        server.shutdown();
        // The dead link fails the input read — degraded to a Bad sample
        // — and the output write; both degrade into io_health while the
        // scan completes: a field outage does not stop the controller.
        assert_eq!(executor.scan(), Tick(2));

        let snapshot = executor.snapshot();
        let health = &snapshot.io_health;
        // Both boundaries counted, attributed to the point and tick.
        assert_eq!(health.failed_reads, 1);
        assert_eq!(health.failed_writes, 1);
        assert_eq!(health.consecutive_failures, 2);
        assert_eq!(
            health.last_error,
            Some(IoFault {
                tick: Tick(2),
                point: PointId(20),
                direction: dcs_core::Direction::Out,
                error: IoError::Disconnected(PointId(20)),
            })
        );
        // And the link itself is named degradation on the driver's own
        // surface — visibly separate from the per-point quality the same
        // event left on the input sample.
        let driver = health.driver.as_ref().unwrap();
        assert_eq!(driver.link, LinkState::Disconnected);
        assert_eq!(
            driver.last_error.as_deref(),
            Some("no live connection to the plant server")
        );
        assert!(!remote.connected());
        assert_eq!(
            snapshot.points[0].sample.unwrap().quality,
            Quality::Bad(QualityReason::CommunicationFault)
        );
    });
}

#[test]
fn a_recovered_link_clears_the_standing_failure_from_diagnostics() {
    let server =
        PlantServer::bind(("127.0.0.1", 0), SimDriver::new(loopback_map()).unwrap()).unwrap();
    let addr = server.local_addr().unwrap();
    let remote = thread::scope(|scope| {
        scope.spawn(|| server.serve());
        let remote = RemoteDriver::connect(addr).unwrap();
        assert!(remote.read(PointId(10)).is_ok());
        server.shutdown();
        // The severed link records the failure it stands under.
        assert_eq!(
            remote.read(PointId(10)),
            Err(IoError::Disconnected(PointId(10)))
        );
        let diagnostics = remote.diagnostics().unwrap();
        assert_eq!(diagnostics.link, LinkState::Disconnected);
        assert_eq!(
            diagnostics.last_error.as_deref(),
            Some("no live connection to the plant server")
        );
        remote
    });
    // The plant returns on the same port: the first exchange that
    // answers — here the re-attach probe's own read — clears the
    // standing record, so the health surface reports the link as it is.
    let restarted = PlantServer::bind(addr, SimDriver::new(loopback_map()).unwrap()).unwrap();
    thread::scope(|scope| {
        scope.spawn(|| restarted.serve());
        let _guard = ShutdownOnDrop(&restarted);

        wait_for_reattach(&remote, Duration::from_secs(10));
        assert_eq!(
            remote.diagnostics().unwrap(),
            DriverDiagnostics {
                link: LinkState::Connected,
                last_error: None,
                exchange: None,
            }
        );

        // A request failing again re-records, and continued failures
        // keep the newest error: the fresh field's unclaimed refusal
        // stands until the next failure replaces it.
        assert_eq!(remote.step(0.1), Err(RemoteError::Unclaimed));
        assert_eq!(
            remote.diagnostics().unwrap().last_error.as_deref(),
            Some("field mutation refused: no attachment owns field writes")
        );
        restarted.shutdown();
        assert_eq!(
            remote.read(PointId(10)),
            Err(IoError::Disconnected(PointId(10)))
        );
        assert_eq!(
            remote.diagnostics().unwrap().last_error.as_deref(),
            Some("no live connection to the plant server")
        );
    });
}

#[test]
fn an_unresponsive_peer_surfaces_timeout_on_every_access() {
    // A listener that never answers: the handshake completes out of the
    // backlog but no response ever arrives.
    let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let addr = listener.local_addr().unwrap();
    let remote = RemoteDriver::connect_with_timeout(addr, Duration::from_millis(200)).unwrap();

    assert_eq!(remote.read(PointId(1)), Err(IoError::Timeout(PointId(1))));
    // A late answer could pair with a later request, so the link is
    // dropped — but the peer is still listening, so the lazy re-attach
    // lands a fresh stream and the next access waits out its own
    // timeout rather than failing fast.
    assert_eq!(remote.read(PointId(1)), Err(IoError::Timeout(PointId(1))));
    drop(listener);
    // With the listener gone the re-attach itself is refused — the
    // endpoint now reports not-answerable at connect time.
    assert_eq!(
        remote.read(PointId(1)),
        Err(IoError::Disconnected(PointId(1)))
    );
}

#[test]
fn connecting_to_a_dead_port_fails_cleanly() {
    // Bind once to learn a free port, then drop the listener so the
    // address refuses connections. A concurrently bound fixture can
    // legitimately take the freed port — the connect then succeeds —
    // so retry until a probed port stays refused.
    for _ in 0..20 {
        let addr = TcpListener::bind(("127.0.0.1", 0))
            .unwrap()
            .local_addr()
            .unwrap();
        match RemoteDriver::connect(addr) {
            Err(error) => {
                assert_eq!(error.kind(), io::ErrorKind::ConnectionRefused);
                return;
            }
            Ok(_) => continue,
        }
    }
    panic!("twenty dead ports each connected — a concurrent listener keeps taking the freed port");
}

#[test]
fn a_malformed_line_is_refused_without_killing_the_connection() {
    with_server(loopback_map(), |addr| {
        let mut stream = BufReader::new(TcpStream::connect(addr).unwrap());
        stream.get_mut().write_all(b"this is not json\n").unwrap();
        let mut line = String::new();
        stream.read_line(&mut line).unwrap();
        let response: PlantResponse = serde_json::from_str(&line).unwrap();
        assert!(matches!(
            response,
            PlantResponse::Error {
                error: PlantError::InvalidRequest { .. }
            }
        ));

        // The connection still serves well-formed requests.
        stream
            .get_mut()
            .write_all(b"{\"op\":\"read\",\"point\":10}\n")
            .unwrap();
        line.clear();
        stream.read_line(&mut line).unwrap();
        let response: PlantResponse = serde_json::from_str(&line).unwrap();
        assert!(matches!(response, PlantResponse::Sample { .. }));
    });
}

#[test]
fn executor_runs_unchanged_with_identical_behavior_local_and_remote() {
    let local = SimDriver::new(scenario_map()).unwrap();
    let local_trace = scripted_run(&local, |dt| local.step(dt));
    let remote_trace = with_server(scenario_map(), |addr| {
        let remote = RemoteDriver::connect(addr).unwrap();
        remote.claim_writer(1).unwrap();
        scripted_run(&remote, |dt| remote.step(dt).unwrap())
    });
    // Same components, same script, same point map — only the transport
    // differs, and the observed samples are identical.
    assert_eq!(local_trace, remote_trace);
}

#[test]
fn two_identical_scripted_runs_produce_identical_sample_sequences() {
    let run = || {
        with_server(scenario_map(), |addr| {
            let remote = RemoteDriver::connect(addr).unwrap();
            remote.claim_writer(1).unwrap();
            scripted_run(&remote, |dt| remote.step(dt).unwrap())
        })
    };
    assert_eq!(run(), run());
}

/// A point map declaring a `stale_after_ticks` freshness budget on the
/// `In` point — the shape a budgeted `io_point` resolves into.
fn budgeted_map(point: u64, budget: u64) -> PointMap {
    PointMap::new()
        .with_spec(
            PointId(point),
            PointSpec {
                direction: Direction::In,
                kind: ValueKind::Float,
                internal: None,
                writable: false,
                requires_reason: false,
                stale_after_ticks: Some(budget),
                journaled: false,
            },
        )
        .with_point(PointId(20), Direction::Out, ValueKind::Float)
}

#[test]
fn a_resumed_field_owner_clears_the_stale_an_unstepped_window_left() {
    with_server(loopback_map(), |addr| {
        let remote = RemoteDriver::connect(addr).unwrap();
        remote.claim_writer(1).unwrap();
        let mut executor = Executor::new(
            &remote,
            budgeted_map(10, 2),
            vec![Box::new(Accumulator {
                input: PointId(10),
                output: PointId(20),
                total: 0.0,
            })],
        )
        .unwrap();

        // The field owner steps: the plant's own tick domain advances
        // with the run's and every read lands fresh.
        for _ in 0..3 {
            remote.step(0.1).unwrap();
            executor.scan();
            assert_eq!(executor.sample(PointId(10)).unwrap().quality, Quality::Good);
        }

        // The QA reproduction: the owner is paused (or demoted) and the
        // plant stops stepping while the run keeps ticking — the plant
        // domain freezes, the report stops changing, and the held
        // sample ages to stale past its budget. The field still serves
        // the last-known value good: stale is the run's verdict on a
        // frozen report, not the driver's.
        executor.run(4);
        assert_eq!(remote.read(PointId(10)).unwrap().quality, Quality::Good);
        assert_eq!(
            executor.sample(PointId(10)).unwrap().quality,
            Quality::Uncertain(QualityReason::Stale)
        );

        // The owner resumes stepping — the plant domain resumes behind
        // the run's by the window's length, permanently. A lag measured
        // across the two domains reads past the budget forever and
        // latches stale; judged on the report's own change, the first
        // fresh read restores the driver's quality.
        remote.step(0.1).unwrap();
        executor.scan();
        assert_eq!(executor.sample(PointId(10)).unwrap().quality, Quality::Good);
        assert!(
            remote.read(PointId(10)).unwrap().tick < executor.snapshot().tick,
            "the resumed plant domain still lags the run's — the offset a \
             cross-domain lag would read as permanently stale"
        );

        // And stays fresh while the resumed domain keeps advancing —
        // the permanent offset is not the freshness the budget measures.
        for _ in 0..3 {
            remote.step(0.1).unwrap();
            executor.scan();
            assert_eq!(executor.sample(PointId(10)).unwrap().quality, Quality::Good);
        }
    });
}

#[test]
fn a_bare_field_input_reads_fresh_while_the_plant_steps_and_stales_when_it_stops() {
    // The QA finding's shared-plant half: a `sim-tcp` input no
    // loopback or element drives re-stamps every step the field owner
    // calls, so a budgeted bare point stays fresh while the plant
    // steps — and the held report ages out on the declared budget once
    // stepping stops.
    with_server(bare_map(), |addr| {
        let remote = RemoteDriver::connect(addr).unwrap();
        remote.claim_writer(1).unwrap();
        let mut executor = Executor::new(
            &remote,
            budgeted_map(10, 2),
            vec![Box::new(Accumulator {
                input: PointId(10),
                output: PointId(20),
                total: 0.0,
            })],
        )
        .unwrap();

        // Each plant step re-stamps the bare channel's held sample —
        // a changed report every scan, fresh forever under the budget.
        for _ in 0..5 {
            remote.step(0.1).unwrap();
            executor.scan();
            assert_eq!(executor.sample(PointId(10)).unwrap().quality, Quality::Good);
        }

        // The owner stops stepping: the report freezes and the run's
        // own lag accrues — past the budget the point presents stale.
        executor.run(3);
        assert_eq!(
            executor.sample(PointId(10)).unwrap().quality,
            Quality::Uncertain(QualityReason::Stale)
        );

        // Resumed stepping re-stamps on the next step; the changed
        // report restores the driver's own quality on the first read.
        remote.step(0.1).unwrap();
        executor.scan();
        assert_eq!(executor.sample(PointId(10)).unwrap().quality, Quality::Good);
    });
}

#[test]
fn a_run_resumed_behind_the_plant_domain_still_marks_stale() {
    with_server(loopback_map(), |addr| {
        let remote = RemoteDriver::connect(addr).unwrap();
        remote.claim_writer(1).unwrap();

        // The resume shape the QA finding's symmetric edge names: the
        // plant has already advanced well past the run's first tick —
        // a checkpoint restart lands behind the driver's stamp domain.
        for _ in 0..20 {
            remote.step(0.1).unwrap();
        }
        let mut executor = Executor::new(
            &remote,
            budgeted_map(10, 2),
            vec![Box::new(Accumulator {
                input: PointId(10),
                output: PointId(20),
                total: 0.0,
            })],
        )
        .unwrap();

        // While the field owner keeps stepping the report keeps
        // changing: fresh every scan even from behind.
        for _ in 0..3 {
            remote.step(0.1).unwrap();
            executor.scan();
            assert_eq!(executor.sample(PointId(10)).unwrap().quality, Quality::Good);
        }

        // The owner stalls: the report stops changing and the run's own
        // lag accrues — the verdict a stamp-domain subtraction could
        // never reach from behind, since the lag saturates at zero.
        executor.run(3);
        assert_eq!(
            executor.sample(PointId(10)).unwrap().quality,
            Quality::Uncertain(QualityReason::Stale)
        );
    });
}

#[test]
fn the_writer_claim_fences_every_attachment_not_holding_it() {
    with_server(loopback_map(), |addr| {
        // Two attachments per side — the multi-connection shape one
        // controller presents — plus a reader that never claims.
        let old_a = RemoteDriver::connect(addr).unwrap();
        let old_b = RemoteDriver::connect(addr).unwrap();
        let new_a = RemoteDriver::connect(addr).unwrap();
        let new_b = RemoteDriver::connect(addr).unwrap();
        let observer = RemoteDriver::connect(addr).unwrap();

        // Unclaimed the field fails closed — the named `unclaimed`
        // refusal on step, the point's `IoError::Fenced` on write —
        // while reads stay open. No attachment mutates before a claim.
        assert_eq!(old_a.step(0.1), Err(RemoteError::Unclaimed));
        assert_eq!(
            old_b.write(PointId(20), Value::Float(1.0)),
            Err(IoError::Fenced(PointId(20)))
        );
        assert_eq!(observer.read(PointId(20)).unwrap().value, Value::Float(0.0));

        // The old owner's several attachments claim the same token; all
        // of them keep writing.
        old_a.claim_writer(1).unwrap();
        old_b.claim_writer(1).unwrap();
        old_a.write(PointId(20), Value::Float(2.0)).unwrap();
        old_b.step(0.1).unwrap();

        // The takeover claim preempts unconditionally — after it, the
        // old owner's writes and steps are refused at the field, not
        // merely quiesced at its own gate.
        new_a.claim_writer(2).unwrap();
        assert_eq!(
            old_a.write(PointId(20), Value::Float(9.0)),
            Err(IoError::Fenced(PointId(20)))
        );
        assert_eq!(old_b.step(0.1), Err(RemoteError::Fenced));
        // The claim covers the whole owner token: the takeover side's
        // second attachment claims the same token and writes.
        new_b.claim_writer(2).unwrap();
        new_b.write(PointId(20), Value::Float(3.0)).unwrap();
        new_a.step(0.1).unwrap();
        assert_eq!(observer.read(PointId(20)).unwrap().value, Value::Float(3.0));

        // Reads and plant tooling stay open to a fenced attachment.
        assert_eq!(old_a.read(PointId(20)).unwrap().value, Value::Float(3.0));
        old_a.inject_fault(PointId(10), Fault::Timeout).unwrap();
        assert_eq!(new_a.read(PointId(10)), Err(IoError::Timeout(PointId(10))));

        // A fenced attachment reclaims the field only by claiming again
        // — the documented switchback, not an automatic reopen.
        old_a.claim_writer(3).unwrap();
        old_a.write(PointId(20), Value::Float(4.0)).unwrap();
        assert_eq!(
            new_a.write(PointId(20), Value::Float(5.0)),
            Err(IoError::Fenced(PointId(20)))
        );
    });
}

#[test]
fn a_second_attachment_claiming_a_held_token_is_flagged_shared() {
    with_server(loopback_map(), |addr| {
        let first = RemoteDriver::connect(addr).unwrap();
        let second = RemoteDriver::connect(addr).unwrap();
        let takeover = RemoteDriver::connect(addr).unwrap();

        // The first claim of a token is the sole-owner grant.
        assert_eq!(first.claim_writer(7).unwrap(), ClaimGrant::Exclusive);
        // A second connection claiming the held token is still granted —
        // one owner's several attachments share a token by design — but
        // flagged: the token cannot tell that attachment from a second
        // field-owning process pinned to it, which would defeat the
        // fencing silently.
        assert_eq!(second.claim_writer(7).unwrap(), ClaimGrant::Shared);
        // The shared grant writes: the flag reports the sharing, it does
        // not refuse it.
        first.write(PointId(20), Value::Float(1.0)).unwrap();
        second.step(0.1).unwrap();

        // `ensure_writer` joining a held token is flagged the same way —
        // the raw protocol answer, as a re-attaching owner sees it.
        let mut raw = BufReader::new(TcpStream::connect(addr).unwrap());
        raw.get_mut()
            .write_all(b"{\"op\":\"ensure_writer\",\"owner\":7}\n")
            .unwrap();
        let mut line = String::new();
        raw.read_line(&mut line).unwrap();
        let response: PlantResponse = serde_json::from_str(&line).unwrap();
        assert_eq!(response, PlantResponse::ClaimedShared { owner: 7 });
        raw.get_mut()
            .write_all(b"{\"op\":\"step\",\"dt\":0.1}\n")
            .unwrap();
        line.clear();
        raw.read_line(&mut line).unwrap();
        let response: PlantResponse = serde_json::from_str(&line).unwrap();
        assert!(matches!(response, PlantResponse::Stepped { .. }));

        // A different token preempts unconditionally and unflagged — a
        // takeover is the claim's ordinary shape, not a shared owner.
        assert_eq!(takeover.claim_writer(8).unwrap(), ClaimGrant::Exclusive);
        assert_eq!(
            first.write(PointId(20), Value::Float(9.0)),
            Err(IoError::Fenced(PointId(20)))
        );
    });
}

#[test]
fn the_shared_flag_tracks_live_holders_not_the_standing_claim() {
    with_server(loopback_map(), |addr| {
        let first = RemoteDriver::connect(addr).unwrap();
        let second = RemoteDriver::connect(addr).unwrap();
        assert_eq!(first.claim_writer(7).unwrap(), ClaimGrant::Exclusive);
        assert_eq!(second.claim_writer(7).unwrap(), ClaimGrant::Shared);

        // The second holder's disconnect releases only its hold — the
        // claim itself keeps fencing claim-less attachments — and a
        // re-claim of the token is exclusive again rather than flagged
        // against a dead connection.
        drop(second);
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        loop {
            match first.claim_writer(7).unwrap() {
                ClaimGrant::Exclusive => break,
                ClaimGrant::Shared => {
                    assert!(
                        std::time::Instant::now() < deadline,
                        "the dropped holder was not reaped within {deadline:?}"
                    );
                    thread::sleep(Duration::from_millis(50));
                }
            }
        }
        // The claim stands through it all: a claim-less attachment is
        // still fenced out of field mutations.
        let probe = RemoteDriver::connect(addr).unwrap();
        assert_eq!(probe.step(0.1), Err(RemoteError::Fenced));
    });
}

#[test]
fn the_conditional_startup_claim_refuses_a_live_incumbent_only() {
    with_server(loopback_map(), |addr| {
        // The incumbent claims as a controller — only a live
        // *controller's* unyielded claim is the incumbent the
        // conditional grant refuses; a tool's claim never blocks it.
        let incumbent = RemoteDriver::connect(addr).unwrap().as_controller();
        let restart = RemoteDriver::connect(addr).unwrap();
        let same_owner = RemoteDriver::connect(addr).unwrap();
        let tool = RemoteDriver::connect(addr).unwrap();

        // The incumbent's unconditional claim stands with a live holder.
        assert_eq!(incumbent.claim_writer(7).unwrap(), ClaimGrant::Exclusive);

        // A different token's conditional startup grant is refused — the
        // stale-checkpoint takeover the grant exists to prevent. The
        // refusal changed nothing: the incumbent keeps writing and
        // stepping, and the refused attachment holds no claim.
        assert_eq!(
            restart.claim_writer_unless_held(9),
            Err(RemoteError::Fenced)
        );
        assert_eq!(
            restart.write(PointId(20), Value::Float(9.0)),
            Err(IoError::Fenced(PointId(20)))
        );
        incumbent.write(PointId(20), Value::Float(1.0)).unwrap();
        incumbent.step(0.1).unwrap();

        // The incumbent's own token still joins — a second live holder
        // of the same owner is the shared grant, never a refusal.
        assert_eq!(
            same_owner.claim_writer_unless_held(7).unwrap(),
            ClaimGrant::Shared
        );

        // Once the incumbent's last holder drops, the standing claim's
        // holder set empties — the dead-owner state — and the
        // conditional grant preempts it legitimately: the
        // restart-as-active recovery of a crashed owner keeps working.
        drop(incumbent);
        drop(same_owner);
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        loop {
            match restart.claim_writer_unless_held(9) {
                Ok(ClaimGrant::Exclusive) => break,
                Err(RemoteError::Fenced) => {
                    assert!(
                        std::time::Instant::now() < deadline,
                        "the dead owner's claim was never reaped"
                    );
                    thread::sleep(Duration::from_millis(50));
                }
                other => panic!("a dead owner's claim must preempt: {other:?}"),
            }
        }
        // The granted restart owns the field outright.
        restart.write(PointId(20), Value::Float(3.0)).unwrap();
        restart.step(0.1).unwrap();
        let observer = RemoteDriver::connect(addr).unwrap();
        assert_eq!(observer.read(PointId(20)).unwrap().value, Value::Float(3.0));

        // The other half of the verdict: a field tool's claim is never
        // an incumbent — its live hold does not refuse the conditional
        // grant, so a rogue `claim_writer` can never wedge a peer's
        // documented promote recovery the way the unconditional
        // live-holder refusal did.
        tool.claim_writer(0xF0_21_61_6E).unwrap();
        let peer = RemoteDriver::connect(addr).unwrap();
        assert_eq!(
            peer.claim_writer_unless_held(9).unwrap(),
            ClaimGrant::Exclusive
        );
        assert_eq!(tool.step(0.1), Err(RemoteError::Fenced));
    });
}

/// Polls `remote`'s link until the re-attach lands or `deadline`
/// expires — the returned-plant half of every restart test.
fn wait_for_reattach(remote: &RemoteDriver, deadline: Duration) {
    let start = std::time::Instant::now();
    while start.elapsed() < deadline {
        if remote.read(PointId(10)).is_ok() {
            return;
        }
        thread::sleep(Duration::from_millis(100));
    }
    panic!("the remote driver did not re-attach within {deadline:?}");
}

#[test]
fn a_restarted_server_is_re_served_by_the_same_attachment() {
    let server =
        PlantServer::bind(("127.0.0.1", 0), SimDriver::new(loopback_map()).unwrap()).unwrap();
    let addr = server.local_addr().unwrap();
    let remote = thread::scope(|scope| {
        scope.spawn(|| server.serve());
        let remote = RemoteDriver::connect(addr).unwrap();
        remote.claim_writer(7).unwrap();
        remote.write(PointId(20), Value::Float(1.0)).unwrap();
        remote.step(0.1).unwrap();
        // The field owner holds its claim; a claim-less attachment is
        // already fenced out of mutations.
        let probe = RemoteDriver::connect(addr).unwrap();
        assert_eq!(probe.step(0.1), Err(RemoteError::Fenced));

        server.shutdown();
        assert_eq!(
            remote.read(PointId(10)),
            Err(IoError::Disconnected(PointId(10)))
        );
        remote
    });
    // The accept thread is joined and the listener dropped, so the
    // restarted plant rebinds the same port — the `docker stop`/`start`
    // shape the QA reproduction produces.
    let restarted = PlantServer::bind(addr, SimDriver::new(loopback_map()).unwrap()).unwrap();
    thread::scope(|scope| {
        scope.spawn(|| restarted.serve());
        let _guard = ShutdownOnDrop(&restarted);

        // The restart is a new claim lifetime, and the unclaimed field
        // fails closed: before the owner re-arms, a fresh attachment's
        // mutation is refused `unclaimed` — the named, closed window the
        // restart left — while reads stay open.
        let probe = RemoteDriver::connect(addr).unwrap();
        assert_eq!(probe.step(0.1), Err(RemoteError::Unclaimed));
        assert_eq!(
            probe.write(PointId(20), Value::Float(9.0)),
            Err(IoError::Fenced(PointId(20)))
        );
        assert_eq!(probe.read(PointId(20)).unwrap().value, Value::Float(0.0));

        wait_for_reattach(&remote, Duration::from_secs(10));
        assert!(remote.connected());
        // The recorded claim re-armed itself on the re-attach: the fresh
        // plant's empty arbitration now names owner 7 again, and the
        // same claim-less attachment's mutation answers `fenced` — a
        // distinguishable verdict from `unclaimed`, so a probe can tell
        // the owner's re-arm landed.
        assert_eq!(probe.step(0.1), Err(RemoteError::Fenced));
        assert_eq!(
            probe.write(PointId(20), Value::Float(9.0)),
            Err(IoError::Fenced(PointId(20)))
        );
        // The owner's own mutations pass through on the re-armed claim.
        remote.write(PointId(20), Value::Float(2.0)).unwrap();
        remote.step(0.1).unwrap();
        assert_eq!(remote.read(PointId(20)).unwrap().value, Value::Float(2.0));
        // Diagnostics report the link live again with the outage's
        // failure record cleared — the first successful exchange after
        // recovery drops it, so the surface describes current health.
        let diagnostics = remote.diagnostics().unwrap();
        assert_eq!(diagnostics.link, LinkState::Connected);
        assert_eq!(diagnostics.last_error, None);
    });
}

#[test]
fn a_reconnecting_attachment_cannot_preempt_a_standing_claim() {
    let server =
        PlantServer::bind(("127.0.0.1", 0), SimDriver::new(loopback_map()).unwrap()).unwrap();
    let addr = server.local_addr().unwrap();
    let remote = thread::scope(|scope| {
        scope.spawn(|| server.serve());
        let remote = RemoteDriver::connect(addr).unwrap();
        remote.claim_writer(7).unwrap();
        remote.write(PointId(20), Value::Float(1.0)).unwrap();
        server.shutdown();
        remote
    });
    // The plant came back under a different owner — the peer that
    // promoted during the outage claimed it unconditionally.
    let restarted = PlantServer::bind(addr, SimDriver::new(loopback_map()).unwrap()).unwrap();
    thread::scope(|scope| {
        scope.spawn(|| restarted.serve());
        let _guard = ShutdownOnDrop(&restarted);
        let new_owner = RemoteDriver::connect(addr).unwrap();
        // The restart window is fail-closed: the fresh field refuses an
        // unclaimed mutation rather than admitting whatever attached
        // first — the claim has to land explicitly.
        assert_eq!(new_owner.step(0.1), Err(RemoteError::Unclaimed));
        new_owner.claim_writer(9).unwrap();

        // The superseded attachment re-attaches, but `ensure_writer`
        // cannot preempt owner 9 — the recorded claim is dropped and
        // the attachment's mutations stay fenced at the field.
        wait_for_reattach(&remote, Duration::from_secs(10));
        assert_eq!(
            remote.write(PointId(20), Value::Float(3.0)),
            Err(IoError::Fenced(PointId(20)))
        );
        assert_eq!(remote.step(0.1), Err(RemoteError::Fenced));
        // The standing owner is undisturbed.
        new_owner.write(PointId(20), Value::Float(4.0)).unwrap();
        assert_eq!(remote.read(PointId(20)).unwrap().value, Value::Float(4.0));
    });
}

#[test]
fn release_claim_keeps_a_demoted_attachment_from_re_arm() {
    let server =
        PlantServer::bind(("127.0.0.1", 0), SimDriver::new(loopback_map()).unwrap()).unwrap();
    let addr = server.local_addr().unwrap();
    let remote = thread::scope(|scope| {
        scope.spawn(|| server.serve());
        let remote = RemoteDriver::connect(addr).unwrap();
        remote.claim_writer(7).unwrap();
        // Demotion: the gate closes and the recorded claim is forgotten,
        // so the restarted plant's empty arbitration is not this
        // attachment's to re-arm.
        remote.release_claim();
        server.shutdown();
        remote
    });

    let restarted = PlantServer::bind(addr, SimDriver::new(loopback_map()).unwrap()).unwrap();
    thread::scope(|scope| {
        scope.spawn(|| restarted.serve());
        let _guard = ShutdownOnDrop(&restarted);
        wait_for_reattach(&remote, Duration::from_secs(10));
        // Had the demoted attachment re-armed owner 7, a claim-less
        // attachment's mutation would answer `fenced`; it answers the
        // named `unclaimed` refusal instead — the field is closed but
        // still free for whichever peer legitimately claims it.
        let probe = RemoteDriver::connect(addr).unwrap();
        assert_eq!(probe.step(0.1), Err(RemoteError::Unclaimed));
        assert_eq!(
            probe.write(PointId(20), Value::Float(5.0)),
            Err(IoError::Fenced(PointId(20)))
        );
        probe.claim_writer(9).unwrap();
        probe.step(0.1).unwrap();
        probe.write(PointId(20), Value::Float(5.0)).unwrap();
    });
}

#[test]
fn a_released_claim_returns_the_field_to_unclaimed_not_open() {
    with_server(loopback_map(), |addr| {
        let owner = RemoteDriver::connect(addr).unwrap();
        let probe = RemoteDriver::connect(addr).unwrap();

        // The tool's shape: a conditional claim, the mutation, then the
        // deliberate hand-back — `ensure_writer`, never `claim_writer`,
        // so tooling can never preempt a live field owner.
        owner.ensure_writer(5).unwrap();
        owner.write(PointId(20), Value::Float(1.0)).unwrap();
        owner.step(0.1).unwrap();
        owner.release_writer().unwrap();

        // The last hold out released the claim itself: the field is
        // `unclaimed` again — still closed, not the old free-for-all —
        // and claimable by whoever legitimately owns it next.
        assert_eq!(probe.step(0.1), Err(RemoteError::Unclaimed));
        assert_eq!(
            probe.write(PointId(20), Value::Float(9.0)),
            Err(IoError::Fenced(PointId(20)))
        );
        probe.claim_writer(9).unwrap();
        probe.step(0.1).unwrap();

        // A partial release keeps the claim standing: one holder
        // releasing while another still holds the token leaves the
        // field owned.
        let second = RemoteDriver::connect(addr).unwrap();
        second.ensure_writer(9).unwrap();
        probe.release_writer().unwrap();
        assert_eq!(owner.step(0.1), Err(RemoteError::Fenced));
        second.write(PointId(20), Value::Float(4.0)).unwrap();
    });
}

/// Connects an attachment that ensures `owner` on `addr` and returns it
/// once it holds the claim alone — a same-token ensure answers
/// `ClaimedShared` while a dropped holder's corpse still counts,
/// `Done` once every prior holder has been reaped server-side.
fn sole_holder(addr: SocketAddr, owner: u64) -> RemoteDriver {
    let probe = RemoteDriver::connect(addr).unwrap();
    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    loop {
        match probe.ensure_writer(owner).unwrap() {
            ClaimGrant::Exclusive => return probe,
            ClaimGrant::Shared => {
                assert!(
                    std::time::Instant::now() < deadline,
                    "a dropped holder was not reaped within {deadline:?}"
                );
                thread::sleep(Duration::from_millis(50));
            }
        }
    }
}

#[test]
fn a_non_holder_release_cannot_strip_a_dead_owners_claim() {
    with_server(loopback_map(), |addr| {
        let owner = RemoteDriver::connect(addr).unwrap();
        owner.claim_writer(5).unwrap();
        // The owner's link drops: the server releases only its hold,
        // leaving the dead-owner claim fencing the field for its
        // token — the failure shape the never-released rule exists to
        // preserve.
        drop(owner);

        // The empty holder set is unobservable — a same-token probe
        // that could report it joins the set — so the reap is chased
        // indirectly: this probe becomes the sole holder only once the
        // owner's corpse is gone, and its own drop leaves exactly one
        // corpse whose reap is then in flight.
        drop(sole_holder(addr, 5));

        // The defect's chain, watched across that last reap: an
        // attachment holding nothing sends `release_writer`, then a
        // foreign token ensures. A non-holder's release is a no-op —
        // the dead owner's claim keeps the foreign token fenced — and
        // the release stripping the claim is what let the ensure land.
        let thief = RemoteDriver::connect(addr).unwrap();
        let stranger = RemoteDriver::connect(addr).unwrap();
        let deadline = std::time::Instant::now() + Duration::from_secs(2);
        loop {
            thief.release_writer().unwrap();
            assert_eq!(
                stranger.ensure_writer(9),
                Err(RemoteError::Fenced),
                "a non-holder's release stripped the dead owner's claim"
            );
            if std::time::Instant::now() >= deadline {
                break;
            }
            thread::sleep(Duration::from_millis(10));
        }

        // The same-token re-arm the fencing protects: the dead owner's
        // re-attach is granted and its writes land — and its own
        // release is the real hand-back, returning the field to
        // `unclaimed`.
        let rearmed = RemoteDriver::connect(addr).unwrap();
        rearmed.ensure_writer(5).unwrap();
        rearmed.write(PointId(20), Value::Float(7.0)).unwrap();
        assert_eq!(rearmed.read(PointId(20)).unwrap().value, Value::Float(7.0));
        rearmed.release_writer().unwrap();
        assert_eq!(thief.step(0.1), Err(RemoteError::Unclaimed));
    });
}

#[test]
fn a_non_holder_release_cannot_dissolve_a_dead_owners_claim() {
    with_server(loopback_map(), |addr| {
        let owner = RemoteDriver::connect(addr).unwrap();
        let stray = RemoteDriver::connect(addr).unwrap();

        // The field owner claims and writes, then dies still holding
        // the claim: the empty-holder claim it leaves is the fence a
        // dead owner's silence keeps standing.
        owner.claim_writer(1).unwrap();
        owner.write(PointId(20), Value::Float(1.0)).unwrap();
        assert_eq!(stray.ensure_writer(2), Err(RemoteError::Fenced));
        drop(owner);

        // The disconnect reaps the owner's hold on the server's
        // schedule — no request can observe the empty set without
        // joining it — so the stray release is repeated across the
        // reaping window. A `release_writer` from an attachment holding
        // nothing must never dissolve the claim: before the reap it
        // removes nothing from a set still naming the corpse, and after
        // it the empty set is the dead-owner state the claim exists to
        // fence rather than the last holder's hand-back.
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        loop {
            stray.release_writer().unwrap();
            match stray.ensure_writer(2) {
                Err(RemoteError::Fenced) => {
                    if std::time::Instant::now() >= deadline {
                        break;
                    }
                    thread::sleep(Duration::from_millis(50));
                }
                grant => panic!("a non-holder release dissolved the dead owner's claim: {grant:?}"),
            }
        }

        // The stray's mutations stay fenced at the field itself — only
        // a fresh preempting claim moves the ownership.
        assert_eq!(
            stray.write(PointId(20), Value::Float(9.0)),
            Err(IoError::Fenced(PointId(20)))
        );
        assert_eq!(stray.step(0.1), Err(RemoteError::Fenced));
        stray.claim_writer(2).unwrap();
        stray.write(PointId(20), Value::Float(4.0)).unwrap();
        assert_eq!(stray.read(PointId(20)).unwrap().value, Value::Float(4.0));
    });
}

#[test]
fn an_unclaimed_field_write_re_arms_the_recorded_owner_in_place() {
    with_server(loopback_map(), |addr| {
        let owner = RemoteDriver::connect(addr).unwrap();
        owner.claim_writer(777).unwrap();
        owner.write(PointId(20), Value::Float(1.0)).unwrap();
        owner.step(0.1).unwrap();

        // A second connection preempts the claim and hands it straight
        // back — the soft-DoS shape from the issue: the field sits
        // unclaimed while the owner's connection stays alive.
        let interposer = RemoteDriver::connect(addr).unwrap();
        interposer.claim_writer(777_777).unwrap();
        interposer.release_writer().unwrap();
        assert_eq!(
            RemoteDriver::connect(addr).unwrap().step(0.1),
            Err(RemoteError::Unclaimed)
        );

        // The recorded owner re-arms conditionally and the mutation
        // lands instead of surfacing a fencing verdict.
        owner.write(PointId(20), Value::Float(2.0)).unwrap();
        owner.step(0.1).unwrap();
        assert_eq!(owner.read(PointId(20)).unwrap().value, Value::Float(2.0));

        // A genuinely stolen field still fences: the re-arm refuses and
        // the write answers the point's `Fenced`.
        interposer.claim_writer(888_888).unwrap();
        assert_eq!(
            owner.write(PointId(20), Value::Float(3.0)),
            Err(IoError::Fenced(PointId(20)))
        );
        assert_eq!(owner.step(0.1), Err(RemoteError::Fenced));
        // The standing owner is undisturbed.
        interposer.write(PointId(20), Value::Float(4.0)).unwrap();
    });
}

#[test]
fn an_unclaimed_window_does_not_demote_the_standing_owner() {
    with_server(loopback_map(), |addr| {
        const OWNER: u64 = 777;
        const FOREIGN: u64 = 777_777;

        // The driven-active shape: the executor scans behind a closed
        // write gate over the remote attachment, activated under the
        // owner's claim.
        let active = RemoteDriver::connect(addr).unwrap();
        let gate = WriteGate::closed(&active);
        let point_map: PointMap = [
            (PointId(10), Direction::In, ValueKind::Float),
            (PointId(20), Direction::Out, ValueKind::Float),
        ]
        .into_iter()
        .collect();
        let mut peer = Peer::active(
            Executor::new(
                &gate,
                point_map,
                vec![Box::new(Accumulator {
                    input: PointId(10),
                    output: PointId(20),
                    total: 0.0,
                })],
            )
            .unwrap(),
            Some(&gate),
        )
        .with_field_claim(|| {
            active
                .claim_writer(OWNER)
                .map(|_| ())
                .map_err(|error| error.to_string())
        })
        .with_field_release(|| active.release_claim());
        peer.activate().unwrap();
        peer.scan();
        assert_eq!(peer.role(), Role::Active);

        // The issue's reproduction: a second connection claims the plant
        // socket under a foreign owner then releases, leaving the field
        // unclaimed while the controller connection stays alive.
        let interposer = RemoteDriver::connect(addr).unwrap();
        interposer.claim_writer(FOREIGN).unwrap();
        interposer.release_writer().unwrap();

        // One active scan: the write re-arms the recorded owner and
        // lands — no `field_claim_lost`, no demotion, the field owned
        // again under the standing token.
        peer.scan();
        assert_eq!(peer.role(), Role::Active);
        assert!(peer.take_fencing_losses().is_empty());
        assert!(peer.take_role_changes().is_empty());

        // The write landed on the shared field under the re-armed claim.
        let probe = RemoteDriver::connect(addr).unwrap();
        assert_eq!(probe.step(0.1), Err(RemoteError::Fenced));
    });
}

/// The QA finding `demoted-ex-owner-orphan-probe-reseizes-field-claim`
/// (#835): a foreign `claim_writer` preempts the active's claim, the
/// first fenced write demotes the ex-owner in place, and the foreign
/// claim's release leaves the field unclaimed — the mutual-standby
/// wedge where the demoted ex-owner's orphan cycle re-arms its released
/// token through `ensure_writer`. The defect had the probe *binding*
/// the ex-owner's still-live connection into the claim's holder set, so
/// the re-armed claim read as a live incumbent and refused the
/// restart-as-active grant `claim_writer_unless_held` — foreclosing the
/// documented recovery for any new token. The probe must stay unbound:
/// the re-armed claim stands with an empty holder set, fencing every
/// attachment — the ex-owner's own included — while staying preemptable
/// exactly like a dead owner's standing claim.
///
/// Two `Peer`s over `RemoteDriver` attachments play the redundant pair,
/// wired the way `dcs-controller` wires the claim hooks; the foreign
/// attachment's claim-and-release drives the fencing-loss demote.
#[test]
fn an_orphaned_ex_owners_rearm_holds_no_live_holder_and_stays_preemptable() {
    with_server(loopback_map(), |addr| {
        const OWNER_A: u64 = 7;
        const OWNER_B: u64 = 8;
        const FOREIGN: u64 = 999;
        const RESTARTED: u64 = 4242;

        let point_map = || -> PointMap {
            [
                (PointId(10), Direction::In, ValueKind::Float),
                (PointId(20), Direction::Out, ValueKind::Float),
            ]
            .into_iter()
            .collect()
        };
        let component = || -> Box<dyn Component> {
            Box::new(Accumulator {
                input: PointId(10),
                output: PointId(20),
                total: 0.0,
            })
        };
        // The claim hooks as `dcs-controller` wires them: the orphan
        // probe is the *unbound* ensure — it re-arms the released claim
        // for the recorded token without the probing attachment ever
        // joining the holder set.
        let conditional =
            |remote: &RemoteDriver, owner: u64| match remote.claim_writer_unless_held(owner) {
                Ok(_) => Ok(true),
                Err(RemoteError::Fenced) => Ok(false),
                Err(error) => Err(error.to_string()),
            };

        let a = RemoteDriver::connect(addr).unwrap().as_controller();
        let a_gate = WriteGate::closed(&a);
        let mut a_peer = Peer::active(
            Executor::new(&a_gate, point_map(), vec![component()]).unwrap(),
            Some(&a_gate),
        )
        .with_field_claim(|| {
            a.claim_writer(OWNER_A)
                .map(|_| ())
                .map_err(|error| error.to_string())
        })
        .with_field_release(|| {
            let _ = a.release_writer_keep_claim();
        })
        .with_field_ensure(|| match a.ensure_writer_unbound(OWNER_A) {
            Ok(()) => Ok(true),
            Err(RemoteError::Fenced) => Ok(false),
            Err(error) => Err(error.to_string()),
        })
        .with_field_orphan_claim(|| conditional(&a, OWNER_A))
        .with_field_startup_claim(|| conditional(&a, OWNER_A));
        a_peer.activate().unwrap();
        a_peer.scan();
        assert_eq!(a_peer.role(), Role::Active);

        let b = RemoteDriver::connect(addr).unwrap().as_controller();
        let b_gate = WriteGate::closed(&b);
        let mut b_peer = Peer::standby(
            Executor::new(&b_gate, point_map(), vec![component()]).unwrap(),
            Some(&b_gate),
        )
        .with_field_claim(|| {
            b.claim_writer(OWNER_B)
                .map(|_| ())
                .map_err(|error| error.to_string())
        })
        .with_field_release(|| {
            let _ = b.release_writer_keep_claim();
        })
        .with_field_ensure(|| match b.ensure_writer_unbound(OWNER_B) {
            Ok(()) => Ok(true),
            Err(RemoteError::Fenced) => Ok(false),
            Err(error) => Err(error.to_string()),
        })
        .with_field_orphan_claim(|| conditional(&b, OWNER_B))
        .with_field_startup_claim(|| conditional(&b, OWNER_B));
        // Converge the standby on the owner's line.
        b_peer.scan();
        b_peer.track_once(|| Ok(a_peer.checkpoint()));
        assert!(matches!(b_peer.sync_state(), StandbySync::Tracking { .. }));

        // The reproduction's trigger: a foreign attachment claims the
        // field, holds it across the owner's fenced write — which
        // demotes the superseded owner in place — then releases,
        // leaving the field unclaimed.
        let interposer = RemoteDriver::connect(addr).unwrap();
        interposer.claim_writer(FOREIGN).unwrap();
        a_peer.scan();
        assert_eq!(a_peer.role(), Role::Demoting);
        a_peer.scan();
        assert_eq!(a_peer.role(), Role::Standby);
        assert_eq!(a_peer.take_fencing_losses().len(), 1);
        interposer.release_writer().unwrap();

        let probe = RemoteDriver::connect(addr).unwrap();
        assert_eq!(
            probe.probe_writer().unwrap(),
            FieldClaim::Unclaimed,
            "the released foreign claim leaves the field unclaimed"
        );

        // The wedge: both peers settle standby, each pulling the
        // other's ownerless checkpoints. The never-owner's orphaned
        // apply probes nothing — it has no claim to re-arm — so the
        // field stays unclaimed through its pull alone.
        b_peer.scan();
        b_peer.track_once(|| Ok(a_peer.checkpoint()));
        assert!(matches!(b_peer.sync_state(), StandbySync::Orphaned { .. }));
        assert_eq!(
            probe.probe_writer().unwrap(),
            FieldClaim::Unclaimed,
            "a peer that never owned has no claim to re-arm"
        );

        // The ex-owner's orphaned apply runs the unbound ensure probe:
        // the claim stands again for its token — the field closed to a
        // foreign grab — but with no live holder.
        a_peer.track_once(|| Ok(b_peer.checkpoint()));
        assert!(matches!(a_peer.sync_state(), StandbySync::Orphaned { .. }));
        assert_eq!(
            probe.probe_writer().unwrap(),
            FieldClaim::Held,
            "the re-armed claim must stand"
        );
        assert_eq!(probe.step(0.1), Err(RemoteError::Fenced));
        assert_eq!(
            probe.write(PointId(20), Value::Float(9.0)),
            Err(IoError::Fenced(PointId(20)))
        );
        // The wire's holder verdict, directly: the ex-owner's own
        // still-live attachment is fenced like every other non-holder
        // — on the defect build the bound probe had made it a live
        // holder and this step answered `Stepped`.
        assert_eq!(
            a.step(0.1),
            Err(RemoteError::Fenced),
            "the probing ex-owner must not read as a live holder"
        );

        // The defect's foreclosure, asserted clear: the conditional
        // startup grant a restarted controller's activation runs
        // preempts the holderless claim — as it does a dead owner's —
        // instead of refusing a live incumbent.
        let restart = RemoteDriver::connect(addr).unwrap().as_controller();
        assert_eq!(
            restart.claim_writer_unless_held(RESTARTED).unwrap(),
            ClaimGrant::Exclusive
        );
        restart.write(PointId(20), Value::Float(4.0)).unwrap();
        restart.step(0.1).unwrap();
        assert_eq!(restart.read(PointId(20)).unwrap().value, Value::Float(4.0));

        // And the probe's other half still stands: a granted
        // `claim_writer_unless_held` is itself a live controller claim,
        // so the ex-owner's next conditional re-arm — and its orphaned
        // promote, which runs the same grant — refuse the live
        // incumbent rather than preempting it.
        assert_eq!(a.ensure_writer_unbound(OWNER_A), Err(RemoteError::Fenced));
        assert!(matches!(
            a_peer.promote(),
            Err(SwitchError::FieldClaimFailed { .. })
        ));
        assert!(matches!(a_peer.sync_state(), StandbySync::Orphaned { .. }));

        // Once the restart hands the field back the documented escape
        // returns: the orphaned ex-owner's promote takes the holderless
        // field and the pair converges on one active.
        restart.release_writer().unwrap();
        a_peer.promote().unwrap();
        a_peer.scan();
        assert_eq!(a_peer.role(), Role::Active);
        b_peer.track_once(|| Ok(a_peer.checkpoint()));
        assert!(matches!(b_peer.sync_state(), StandbySync::Tracking { .. }));
    });
}

/// The claimant-attribution the fencing-loss journal rides on: a
/// write fenced by a standing foreign claim records the claim's owner
/// token as `fenced_by`, and the record holds until a verdict names
/// another owner or the field reports unclaimed.
#[test]
fn a_fenced_write_records_the_standing_claims_owner_as_fenced_by() {
    with_server(loopback_map(), |addr| {
        let owner = RemoteDriver::connect(addr).unwrap();
        owner.claim_writer(1).unwrap();
        assert_eq!(owner.fenced_by(), None);

        let rogue = RemoteDriver::connect(addr).unwrap();
        rogue.claim_writer(2).unwrap();

        // The superseded owner's next write meets the point's fenced
        // verdict — carrying the rogue claim's owner token, which the
        // attachment records as the claimant its audit names.
        assert_eq!(
            owner.write(PointId(20), Value::Float(1.0)),
            Err(IoError::Fenced(PointId(20)))
        );
        assert_eq!(owner.fenced_by(), Some(2));

        // A step fenced by the same claim attributes identically; a
        // probe of the standing claim does not clear the record.
        assert_eq!(owner.step(0.1), Err(RemoteError::Fenced));
        assert_eq!(owner.fenced_by(), Some(2));
        assert_eq!(owner.probe_writer().unwrap(), FieldClaim::Held);
        assert_eq!(owner.fenced_by(), Some(2));

        // The release's unclaimed verdict clears the record — the field
        // names no claimant once no claim stands.
        rogue.release_writer().unwrap();
        assert_eq!(owner.probe_writer().unwrap(), FieldClaim::Unclaimed);
        assert_eq!(owner.fenced_by(), None);
    });
}

/// The monitor half of the fencing verdict: a claimant that declares
/// its monitor endpoint on the claim arms the field's arbitration to
/// name that endpoint to every attachment it fences — the
/// field-arbitrated successor a fencing-demoted peer verifies and
/// tracks on the unkeyed pair (#1045). A claim declared without a
/// monitor carries `None`, exactly like `fenced_by` on an
/// unattributed verdict.
#[test]
fn a_fenced_write_carries_the_standing_claims_declared_monitor() {
    with_server(loopback_map(), |addr| {
        let owner = RemoteDriver::connect(addr).unwrap().as_controller();
        let declared = SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), 7741);
        owner.set_claim_monitor(declared);
        owner.claim_writer(1).unwrap();

        let intruder = RemoteDriver::connect(addr).unwrap();
        assert_eq!(intruder.claimed_monitor(), None);

        // The intruder's write meets the point's fenced verdict —
        // carrying the standing claim's declared monitor alongside its
        // owner token.
        assert_eq!(
            intruder.write(PointId(20), Value::Float(1.0)),
            Err(IoError::Fenced(PointId(20)))
        );
        assert_eq!(intruder.fenced_by(), Some(1));
        assert_eq!(intruder.claimed_monitor(), Some(declared));

        // The conditional grant's live-incumbent refusal and the
        // claim probe carry the same endpoint — every fencing verdict
        // reports what the standing claim declared.
        assert_eq!(
            intruder.claim_writer_unless_held(9),
            Err(RemoteError::Fenced)
        );
        assert_eq!(intruder.claimed_monitor(), Some(declared));
        assert_eq!(intruder.probe_writer().unwrap(), FieldClaim::Held);
        assert_eq!(intruder.claimed_monitor(), Some(declared));

        // A claim declared without a monitor carries no endpoint: the
        // foreign takeover's verdicts report `None` exactly where
        // `fenced_by` reports the token.
        let rogue = RemoteDriver::connect(addr).unwrap();
        rogue.claim_writer(2).unwrap();
        assert_eq!(
            owner.write(PointId(20), Value::Float(1.0)),
            Err(IoError::Fenced(PointId(20)))
        );
        assert_eq!(owner.fenced_by(), Some(2));
        assert_eq!(owner.claimed_monitor(), None);

        // The unclaimed verdict clears both records — the field names
        // neither claimant nor monitor once no claim stands.
        rogue.release_writer().unwrap();
        assert_eq!(owner.probe_writer().unwrap(), FieldClaim::Unclaimed);
        assert_eq!(owner.claimed_monitor(), None);
    });
}
