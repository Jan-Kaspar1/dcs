//! Loopback-TCP integration tests for `dcs-sim-net`: the remote driver
//! against a live `PlantServer`, the shared plant two clients observe,
//! failure mapping at the `IoDriver` boundary, and identical behavior for
//! a scripted executor run local and remote.

use dcs_core::{
    IoDriver, IoError, PointId, Quality, QualityReason, Sample, Tick, Value, ValueKind,
};
use dcs_runtime::{
    Component, ComponentIo, ComponentIoExt, Executor, IoRequirement, PointMap, StepError,
};
use dcs_sim::{
    ChannelId, ChannelMap, Direction, Fault, FirstOrderLag, Loopback, PointBinding, ProcessElement,
    SimDriver,
};
use dcs_sim_net::{PlantError, PlantResponse, PlantServer, RemoteDriver, RemoteError};
use std::io::{self, BufRead, BufReader, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
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
        executor.scan().unwrap();
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
fn protocol_answers_map_to_named_io_errors() {
    with_server(loopback_map(), |addr| {
        let remote = RemoteDriver::connect(addr).unwrap();
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
        // named errors — and the dead driver does not silently recover.
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
fn an_unresponsive_peer_surfaces_timeout_then_disconnects() {
    // A listener that never answers: the handshake completes out of the
    // backlog but no response ever arrives.
    let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let addr = listener.local_addr().unwrap();
    let remote = RemoteDriver::connect_with_timeout(addr, Duration::from_millis(200)).unwrap();

    assert_eq!(remote.read(PointId(1)), Err(IoError::Timeout(PointId(1))));
    // A late answer could pair with a later request, so the link is
    // dropped: the next access fails fast as Disconnected.
    assert_eq!(
        remote.read(PointId(1)),
        Err(IoError::Disconnected(PointId(1)))
    );
    drop(listener);
}

#[test]
fn connecting_to_a_dead_port_fails_cleanly() {
    // Bind once to learn a free port, then drop the listener so the
    // address refuses connections.
    let addr = TcpListener::bind(("127.0.0.1", 0))
        .unwrap()
        .local_addr()
        .unwrap();
    let error = RemoteDriver::connect(addr).unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::ConnectionRefused);
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
            scripted_run(&remote, |dt| remote.step(dt).unwrap())
        })
    };
    assert_eq!(run(), run());
}
