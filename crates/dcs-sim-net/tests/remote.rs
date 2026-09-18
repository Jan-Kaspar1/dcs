//! Loopback-TCP integration tests for `dcs-sim-net`: the remote driver
//! against a live `PlantServer`, the shared plant two clients observe,
//! failure mapping at the `IoDriver` boundary, and identical behavior for
//! a scripted executor run local and remote.

use dcs_core::{
    Direction, DriverDiagnostics, IoDriver, IoError, IoFault, LinkState, PointId, Quality,
    QualityReason, Role, Sample, Tick, Value, ValueKind,
};
use dcs_runtime::{
    Component, ComponentIo, ComponentIoExt, Executor, IoRequirement, Peer, PointMap, StepError,
    WriteGate,
};
use dcs_sim::{
    ChannelId, ChannelMap, Fault, FirstOrderLag, Loopback, PointBinding, ProcessElement, SimDriver,
};
use dcs_sim_net::{ClaimGrant, PlantError, PlantResponse, PlantServer, RemoteDriver, RemoteError};
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
        // Diagnostics report the link live again while retaining the
        // outage's failure record.
        let diagnostics = remote.diagnostics().unwrap();
        assert_eq!(diagnostics.link, LinkState::Connected);
        assert!(diagnostics.last_error.is_some());
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
