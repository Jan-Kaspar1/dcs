//! Regression coverage for the monitor's two client-paced starvation
//! findings: `incomplete-request-body-pins-monitor-workers` — request
//! handlers once read bodies on the worker that accepted them, with
//! no read bound, so a handful of connections promising bodies they
//! never delivered pinned every worker — and
//! `served-surface-starvation-by-undrained-responses` — a client that
//! never reads a large response pins its serving worker inside
//! `respond` the same way, since tiny_http exposes no socket timeout
//! to bound the write. Either way `GET /role`, `GET /snapshot`, and
//! the standby heartbeat's `GET /checkpoint` starved behind traffic
//! the plant never felt — on an armed pair that read as a dead active
//! and drove a spurious failover.
//!
//! The serving path is now three lanes behind bounded queues: any
//! request that can hold a worker on a client-paced body wait — the
//! body-reading `POST /command` and `POST /scan`, plus any request
//! still carrying body bytes the client owes, whose dropped reader
//! drains the rest the same way — runs on a small submission lane
//! behind a body-size bound; the pair-liveness reads `GET /health`,
//! `GET /checkpoint`, and `GET /role` answer from a dedicated
//! heartbeat lane; and everything else serves from a pool neither wait can
//! reach. These tests hold stalled-body and never-reading connections
//! against a live monitor and assert the liveness surface — and an
//! armed standby's verdict on its active — never notices.

use dcs_core::{
    Command, CommandOutcome, Direction, IoDriver, IoError, PointId, Role, Sample, StandbySync,
    Tick, Value, ValueKind,
};
use dcs_model::{PlantModel, SignalIndex};
use dcs_monitor::{Driven, Monitor, MonitorClient, MonitorConfig};
use dcs_runtime::{
    Component, ComponentIo, ComponentIoExt, Executor, IoRequirement, Peer, PointMap, StepError,
};
use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

/// In-memory driver stub — the same minimal stand-in the other monitor
/// suites use.
struct StubDriver {
    points: Mutex<HashMap<PointId, Sample>>,
}

impl StubDriver {
    fn new(points: &[(PointId, Value)]) -> Self {
        Self {
            points: Mutex::new(
                points
                    .iter()
                    .map(|&(point, value)| (point, Sample::good(value, Tick::ZERO)))
                    .collect(),
            ),
        }
    }
}

impl IoDriver for StubDriver {
    fn read(&self, point: PointId) -> Result<Sample, IoError> {
        self.points
            .lock()
            .unwrap()
            .get(&point)
            .copied()
            .ok_or(IoError::UnknownPoint(point))
    }

    fn write(&self, point: PointId, value: Value) -> Result<(), IoError> {
        let mut points = self.points.lock().unwrap();
        let sample = points.get_mut(&point).ok_or(IoError::UnknownPoint(point))?;
        if value.kind() != sample.value.kind() {
            return Err(IoError::TypeMismatch {
                point,
                expected: sample.value.kind(),
                found: value,
            });
        }
        *sample = Sample::good(value, Tick::ZERO);
        Ok(())
    }
}

/// Reads `In` point 10 and drives `Out` point 20 at gain 2 — the same
/// minimal component the other monitor suites use.
struct Scale;

impl Component for Scale {
    fn name(&self) -> &str {
        "scale"
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        vec![
            IoRequirement::input::<f64>("in", PointId(10)),
            IoRequirement::output::<f64>("out", PointId(20)),
        ]
    }

    fn step(&mut self, io: &dyn ComponentIo, _tick: Tick) -> Result<(), StepError> {
        let sample = io.read_typed::<f64>(PointId(10))?;
        io.write_typed(PointId(20), sample.value * 2.0)?;
        Ok(())
    }
}

/// The model fixture behind the monitors: points 10/20/30 match the
/// rig's point map.
const MODEL: &str = include_str!("../fixtures/monitor.json");

fn signal_index() -> SignalIndex {
    PlantModel::load(MODEL).unwrap().signal_index()
}

fn point_map() -> PointMap {
    PointMap::new()
        .with_writable_point(PointId(10), Direction::In, ValueKind::Float)
        .with_point(PointId(20), Direction::Out, ValueKind::Float)
        .with_point(PointId(30), Direction::Out, ValueKind::Float)
}

fn write_value(point: u64, value: f64) -> Command {
    Command::WriteValue {
        point: PointId(point),
        kind: ValueKind::Float,
        value: Value::Float(value),
    }
}

/// The pair's shared tracking secret the keyed rigs install — the
/// deployment's `--pair-token` hashed through `pair_key`: an
/// announced-source demotion verifies the hinted endpoint against
/// the `line_proof` it signs, and the adopted source's checkpoints
/// keep proving under fresh nonces.
const PAIR_KEY: u64 = 0x517c_c1b7_2722_0a95;

/// The answer bound the reproduction held `GET /role` to — `curl -m3`.
const ANSWER_BOUND: Duration = Duration::from_secs(3);

/// The connections held open against the monitor — comfortably past
/// the workers every lane fields together, so every worker of the old
/// single pool pinned under the probe, and both submission workers
/// still pin under the trickle shape.
const PINNING_CONNECTIONS: usize = 8;

/// The serving lane's worker count and every lane's queue bound —
/// `SERVE_WORKERS` and `LANE_QUEUE_DEPTH` in the crate, restated here
/// because integration tests see only the public API: exactly
/// `SERVE_LANE` undrained pulls pin the whole serving lane, and a
/// flood past `LANE_DEPTH` queued requests is refused rather than
/// queued without bound.
const SERVE_LANE: usize = 4;
const LANE_DEPTH: usize = 64;

/// The deep-history fixture's width and depth: extra input points
/// each sampled once a scan for `HISTORY_SCANS` paced scans. At ~70
/// bytes of JSON a sample that retains enough history for
/// `/history?since=0` to answer past twenty megabytes — beyond the
/// largest socket-buffer pair a never-reading loopback client can
/// absorb (rmem/wmem autotuning maxima around 6MB/4MB) — so the
/// serving worker's `respond` write blocks for as long as the
/// connection stays open. The QA reproduction pinned the client's
/// `SO_RCVBUF` small to the same effect; the oversized answer alone
/// pins it here.
const HISTORY_POINTS: u64 = 512;
const HISTORY_SCANS: u64 = 640;

/// One serving monitor on a dedicated thread. The monitor is
/// `Arc`-shared so `stop`/drop can end it; the driver is leaked
/// `'static` so the monitor outlives any borrow.
struct Rig {
    monitor: Arc<Monitor<'static>>,
    client: MonitorClient,
    addr: SocketAddr,
    thread: Option<thread::JoinHandle<()>>,
}

impl Rig {
    /// Serves `monitor` on a spawned thread — the caller binds it
    /// (plain, peer, or driven).
    fn start(monitor: Monitor<'static>) -> Self {
        let monitor = Arc::new(monitor);
        let addr = monitor.local_addr();
        let serving = Arc::clone(&monitor);
        Self {
            monitor,
            client: MonitorClient::new(addr),
            addr,
            thread: Some(thread::spawn(move || serving.serve())),
        }
    }

    /// A plain monitor — `POST /scan` stays available for the driven
    /// standby's track pulls and the recovery assertions.
    fn plain() -> Self {
        let driver: &'static StubDriver = Box::leak(Box::new(StubDriver::new(&[
            (PointId(10), Value::Float(0.0)),
            (PointId(20), Value::Float(0.0)),
            (PointId(30), Value::Float(0.0)),
        ])));
        let executor = Executor::new(driver, point_map(), vec![Box::new(Scale)]).unwrap();
        Self::start(Monitor::bind("127.0.0.1:0", executor, signal_index()).unwrap())
    }

    /// A peer monitor with `peer`'s role — `active` or `standby` — and
    /// `driven` wiring when the standby tracks a source through its
    /// `POST /scan` boundary. Every peer rig runs keyed — the
    /// `--pair-token` deployment a real redundant pair declares.
    fn peer(peer: Peer<'static>, driven: Option<Driven<'static>>) -> Self {
        let monitor = Monitor::bind_peer("127.0.0.1:0", peer, signal_index())
            .unwrap()
            .with_pair_key(PAIR_KEY);
        let monitor = match driven {
            Some(driven) => monitor.driven(driven),
            None => monitor,
        };
        Self::start(monitor)
    }

    /// A monitor carrying a deep recorded history: `HISTORY_POINTS`
    /// extra field inputs beside the model's three, each read into the
    /// image every one of `HISTORY_SCANS` paced scans run here before
    /// returning. `/history?since=0` then answers ~20MB — the undrained
    /// large response the reproduction pinned every serving worker
    /// with.
    fn deep_history() -> Self {
        Self::deep_history_rig(
            Monitor::bind_with(
                "127.0.0.1:0",
                deep_history_executor(),
                signal_index(),
                MonitorConfig {
                    history_capacity: HISTORY_SCANS as usize,
                    ..MonitorConfig::default()
                },
            )
            .unwrap(),
        )
    }

    /// As [`deep_history`](Self::deep_history) on a field-owning peer —
    /// the wedge sitting on the very monitor an operator's switchover
    /// request targets.
    fn deep_history_active() -> Self {
        Self::deep_history_rig(
            Monitor::bind_peer_with(
                "127.0.0.1:0",
                Peer::active(deep_history_executor(), None),
                signal_index(),
                MonitorConfig {
                    history_capacity: HISTORY_SCANS as usize,
                    ..MonitorConfig::default()
                },
            )
            .unwrap()
            .with_pair_key(PAIR_KEY),
        )
    }

    /// Serves `monitor` and runs `HISTORY_SCANS` paced scans against
    /// it, so its `/history?since=0` answers ~20MB — the undrained
    /// large response the reproduction pinned every serving worker
    /// with.
    fn deep_history_rig(monitor: Monitor<'static>) -> Self {
        let rig = Self::start(monitor);
        for _ in 0..HISTORY_SCANS {
            rig.monitor.paced_scan();
        }
        rig
    }
}

/// The deep-history fixture's executor: `HISTORY_POINTS` extra field
/// inputs beside the model's three, each sampled once a scan.
fn deep_history_executor() -> Executor<'static> {
    let mut points: Vec<(PointId, Value)> = vec![
        (PointId(10), Value::Float(0.0)),
        (PointId(20), Value::Float(0.0)),
        (PointId(30), Value::Float(0.0)),
    ];
    points.extend((0..HISTORY_POINTS).map(|index| (PointId(1000 + index), Value::Float(0.0))));
    let driver: &'static StubDriver = Box::leak(Box::new(StubDriver::new(&points)));
    let mut map = point_map();
    for index in 0..HISTORY_POINTS {
        map = map.with_point(PointId(1000 + index), Direction::In, ValueKind::Float);
    }
    Executor::new(driver, map, vec![Box::new(Scale)]).unwrap()
}

impl Drop for Rig {
    fn drop(&mut self) {
        self.monitor.shutdown();
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

/// A standby executor with the armed failover budget — the
/// `--auto-promote` half of the reproduction's pair.
fn standby_peer(budget: u32) -> Peer<'static> {
    let driver: &'static StubDriver = Box::leak(Box::new(StubDriver::new(&[
        (PointId(10), Value::Float(0.0)),
        (PointId(20), Value::Float(0.0)),
        (PointId(30), Value::Float(0.0)),
    ])));
    let executor = Executor::new(driver, point_map(), vec![Box::new(Scale)]).unwrap();
    Peer::standby(executor, None).with_failover(budget)
}

fn active_peer() -> Peer<'static> {
    let driver: &'static StubDriver = Box::leak(Box::new(StubDriver::new(&[
        (PointId(10), Value::Float(0.0)),
        (PointId(20), Value::Float(0.0)),
        (PointId(30), Value::Float(0.0)),
    ])));
    let executor = Executor::new(driver, point_map(), vec![Box::new(Scale)]).unwrap();
    Peer::active(executor, None)
}

/// The undrained-response probe shape: `SERVE_LANE` connections each
/// request the full recorded history — an answer far past the socket
/// buffers — then never read a byte of it, so every serving worker
/// blocks inside `respond` for as long as its connection stays open.
/// Dropping a stream ends the pin: the next write fails.
fn starve_on_undrained_history(addr: SocketAddr) -> Vec<TcpStream> {
    (0..SERVE_LANE)
        .map(|_| {
            let mut stream = TcpStream::connect(addr).unwrap();
            stream
                .write_all(b"GET /history?since=0 HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
                .unwrap();
            stream
        })
        .collect()
}

/// The reproduction's probe shape: `POST /command` headers declaring a
/// body far past any legitimate one, never sent. Each returned stream
/// stays open — dropping it ends the pin.
fn starve_on_declared_body(addr: SocketAddr) -> Vec<TcpStream> {
    (0..PINNING_CONNECTIONS)
        .map(|_| {
            let mut stream = TcpStream::connect(addr).unwrap();
            stream
                .write_all(
                    b"POST /command HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n\
                      Content-Length: 1000000\r\n\r\n",
                )
                .unwrap();
            stream
        })
        .collect()
}

/// The trickle shape: headers whose bodies the handlers must read off
/// the live socket — `Expect: 100-continue` requests get a live reader
/// regardless of the declared size, chunked ones carry no declared
/// length at all. The body never follows, so a worker that reads it
/// blocks for as long as the connection stays open: the starvation a
/// body cap cannot reach, which only the lane split covers.
fn starve_on_trickled_body(addr: SocketAddr) -> Vec<TcpStream> {
    (0..PINNING_CONNECTIONS)
        .map(|index| {
            let mut stream = TcpStream::connect(addr).unwrap();
            if index % 2 == 0 {
                stream
                    .write_all(
                        b"POST /command HTTP/1.1\r\nHost: x\r\nContent-Length: 16\r\n\
                          Expect: 100-continue\r\n\r\n",
                    )
                    .unwrap();
            } else {
                stream
                    .write_all(
                        b"POST /scan HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n\
                          10\r\n",
                    )
                    .unwrap();
            }
            stream
        })
        .collect()
}

/// Reads one response head off a pinning connection — the status code
/// the server answered, or `None` when the worker never answered
/// (blocked mid-body or starved behind one that is).
fn response_status(stream: &TcpStream, timeout: Duration) -> Option<u16> {
    stream.set_read_timeout(Some(timeout)).unwrap();
    let mut stream = stream;
    let mut buf = [0u8; 512];
    let mut head = Vec::new();
    while !head.windows(4).any(|window| window == b"\r\n\r\n") {
        match stream.read(&mut buf) {
            Ok(0) | Err(_) => return None,
            Ok(n) => head.extend_from_slice(&buf[..n]),
        }
    }
    String::from_utf8_lossy(&head)
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|code| code.parse::<u16>().ok())
}

/// Every served surface the reproduction starved answers within the
/// bound while `streams` still hold their stalled bodies open — the
/// container health check's `GET /health` probe rides the heartbeat
/// lane beside the pair-liveness reads.
fn assert_served_surface_survives(addr: SocketAddr) {
    let client = MonitorClient::with_timeout(addr, ANSWER_BOUND);
    client
        .role()
        .expect("GET /role starved behind stalled bodies");
    client
        .health()
        .expect("GET /health starved behind stalled bodies");
    client
        .checkpoint()
        .expect("GET /checkpoint starved behind stalled bodies");
    client
        .snapshot()
        .expect("GET /snapshot starved behind stalled bodies");
}

/// The reproduction's recovery evidence — socket close frees the
/// workers, and submissions settle normally again.
fn assert_recovery(addr: SocketAddr) {
    let client = MonitorClient::with_timeout(addr, ANSWER_BOUND);
    let receipt = client
        .command(&write_value(10, 1.0))
        .expect("POST /command never recovered");
    assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
    client.advance(1).expect("POST /scan never recovered");
}

#[test]
fn declared_bodies_never_sent_leave_the_serving_path_up() {
    let rig = Rig::plain();
    // The reproduction itself: headers promising a large body, then
    // silence. On the unfixed pool each request blocked its worker in
    // the body read and the probe's four connections starved the whole
    // served surface. Now each routes to the submission lane, where a
    // worker refuses the declared body 413 before reading a byte —
    // then waits out the owed-body drain on its own lane while the
    // served surface answers on.
    let mut streams = starve_on_declared_body(rig.addr);
    let refused = streams
        .iter()
        .filter(|stream| response_status(stream, ANSWER_BOUND) == Some(413))
        .count();
    assert!(
        refused >= 1,
        "a declared body past the bound should be refused 413, not awaited"
    );
    assert_served_surface_survives(rig.addr);
    streams.clear();
    assert_recovery(rig.addr);
}

#[test]
fn stalled_bodies_pin_only_the_submission_lane() {
    let rig = Rig::plain();
    // Bodies the server must actually read, never delivered: on the
    // unfixed pool every worker blocked in the read and the served
    // surface died; now both submission workers block and the serving
    // lane answers on.
    let mut streams = starve_on_trickled_body(rig.addr);
    // Give the dispatcher and workers a moment to reach their reads —
    // the assertion below is meaningless if the pin has not landed.
    thread::sleep(Duration::from_millis(250));
    assert_served_surface_survives(rig.addr);
    streams.clear();
    assert_recovery(rig.addr);
}

#[test]
fn bodies_owed_on_any_route_stay_off_the_serving_path() {
    let rig = Rig::plain();
    // The drain hole beneath the reproduction: a request whose handler
    // never touches the body still carries it — a declared
    // `Content-Length` leaves the owed remainder on a live reader, and
    // dropping the request drains it on whatever worker holds it. A
    // `GET /role` carrying a promised body is therefore as pinning as
    // a `POST /command` one, so it routes to the submission lane too:
    // answered there before its drain, while the serving lane answers
    // on behind it.
    let mut streams: Vec<TcpStream> = (0..PINNING_CONNECTIONS)
        .map(|_| {
            let mut stream = TcpStream::connect(rig.addr).unwrap();
            stream
                .write_all(b"GET /role HTTP/1.1\r\nHost: x\r\nContent-Length: 1000000\r\n\r\n")
                .unwrap();
            stream
        })
        .collect();
    let answered = streams
        .iter()
        .filter(|stream| response_status(stream, ANSWER_BOUND) == Some(200))
        .count();
    assert!(
        answered >= 1,
        "a body-owing request should still be answered before its drain"
    );
    assert_served_surface_survives(rig.addr);
    streams.clear();
    assert_recovery(rig.addr);
}

#[test]
fn bodies_past_the_bound_are_refused_413() {
    let rig = Rig::plain();
    // Declared-past-the-bound: refused before a byte is read, on both
    // body-reading endpoints.
    for path in ["/command", "/scan"] {
        let mut stream = TcpStream::connect(rig.addr).unwrap();
        stream
            .write_all(
                format!(
                    "POST {path} HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n\
                     Content-Length: 70000\r\n\r\n"
                )
                .as_bytes(),
            )
            .unwrap();
        assert_eq!(response_status(&stream, ANSWER_BOUND), Some(413));
    }
    // Delivered-past-the-bound: a chunked body gets no declared-length
    // pre-check, so the `take` bound is what trips — 413 the moment the
    // decoded bytes cross the cap, without waiting for the rest of the
    // framing.
    let mut stream = TcpStream::connect(rig.addr).unwrap();
    stream
        .write_all(
            b"POST /command HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n\
              20000\r\n",
        )
        .unwrap();
    stream.write_all(&[b' '; 0x20000]).unwrap();
    stream.write_all(b"\r\n0\r\n\r\n").unwrap();
    assert_eq!(response_status(&stream, ANSWER_BOUND), Some(413));

    // Legitimate submissions are untouched.
    assert_recovery(rig.addr);
}

/// The pair-level half of the reproduction: an armed standby tracking
/// a paced active over `GET /checkpoint`. While the active's monitor
/// is starved of workers by stalled bodies, the standby's heartbeat
/// pulls once failed like a dead peer — misses reached the armed
/// budget and the standby self-promoted against a live active. With
/// the serving split the heartbeat rides the lane the bodies can never
/// reach, so the starvation is invisible to the failover measure.
#[test]
fn an_armed_standby_holds_role_through_a_starved_active_monitor() {
    let active = Rig::peer(active_peer(), None);
    let standby = Rig::peer(
        standby_peer(3),
        Some(Driven {
            track: Some(active.addr),
            after_scan: None,
        }),
    );

    // The active paces its own scans in-process — the wall-clock loop
    // the reproduction's controller ran, immune to its own monitor's
    // starvation by construction.
    for _ in 0..3 {
        active.monitor.paced_scan();
    }
    // The standby converges: its `POST /scan` boundary pulls the
    // active's checkpoint and applies it — `Tracking`.
    standby.client.advance(1).unwrap();
    match standby.client.role().unwrap() {
        report
            if report.role == Role::Standby
                && matches!(report.sync, Some(StandbySync::Tracking { .. })) => {}
        report => panic!("the standby never converged: {report:?}"),
    }

    // Starve the active's monitor — the trickle shape that holds
    // workers indefinitely, not the declared body a cap refuses.
    let mut streams = starve_on_trickled_body(active.addr);
    thread::sleep(Duration::from_millis(250));

    // Well past the armed budget of three misses: each standby scan
    // pulls the active's checkpoint. The pulls keep landing — the
    // serving lane answers through the pin — so no miss ever accrues
    // and the standby's verdict on its active stays alive. On the
    // unfixed pool every pull hit the client timeout, three scans
    // armed the budget, and the standby self-promoted.
    for _ in 0..6 {
        active.monitor.paced_scan();
        standby.client.advance(1).unwrap();
    }
    let report = standby.client.role().unwrap();
    assert_eq!(
        report.role,
        Role::Standby,
        "the standby moved through the starvation: {report:?}"
    );
    assert!(
        matches!(report.sync, Some(StandbySync::Tracking { .. })),
        "the standby's convergence degraded against a live active: {report:?}"
    );

    // And the starved active itself still reports — the served surface
    // the heartbeat rides was never down.
    let active_report = MonitorClient::with_timeout(active.addr, ANSWER_BOUND)
        .role()
        .expect("the active's /role starved behind stalled bodies");
    assert_eq!(active_report.role, Role::Active);

    streams.clear();
    assert_recovery(active.addr);
}

/// The finding itself: four connections that request the full history
/// and never read a byte of the answer pin every serving worker inside
/// `respond` — the client-paced wait the body-lane quarantine cannot
/// reach, on the same pool `GET /checkpoint` once rode. The heartbeat
/// lane keeps the pair-liveness reads answering anyway, while a
/// serving-lane read starves as proof the pin landed.
#[test]
fn undrained_responses_never_starve_the_pair_liveness_reads() {
    let rig = Rig::deep_history();
    let mut streams = starve_on_undrained_history(rig.addr);
    // Let the pins land — every serving worker claimed by an undrained
    // answer, serializing or blocked mid-write.
    thread::sleep(Duration::from_millis(250));

    // The finding's timeouts: `GET /health`, `GET /role`, and
    // `GET /checkpoint` now answer from the heartbeat lane the pinned
    // writes can never reach — well inside the bound the reproduction
    // blew through.
    let client = MonitorClient::with_timeout(rig.addr, ANSWER_BOUND);
    client
        .role()
        .expect("GET /role starved behind undrained responses");
    client
        .health()
        .expect("GET /health starved behind undrained responses");
    client
        .checkpoint()
        .expect("GET /checkpoint starved behind undrained responses");

    // The pin is real: a serving-lane read still starves behind the
    // undrained writes, so the liveness answers above rode the split.
    assert!(
        MonitorClient::with_timeout(rig.addr, Duration::from_millis(750))
            .snapshot()
            .is_err(),
        "GET /snapshot answered — the serving lane was never pinned"
    );

    streams.clear();
    assert_recovery(rig.addr);
}

/// The finding's actuation half: the same four undrained answers that
/// pin the serving lane once held the switchover endpoints on it —
/// `POST /promote` and `POST /demote` queued behind the pinned workers
/// and an operator's role change waited out the wedge, silently. The
/// control lane keeps them answering through it: the refusals below
/// are the reproduction's own baseline answers — a lone field owner
/// demotes to `no_tracking_source` and promotes to `already_active` —
/// served inside the bound the wedge blew through.
#[test]
fn undrained_responses_never_starve_role_actuation() {
    let rig = Rig::deep_history();
    let mut streams = starve_on_undrained_history(rig.addr);
    thread::sleep(Duration::from_millis(250));

    let client = MonitorClient::with_timeout(rig.addr, ANSWER_BOUND);
    for (path, refusal) in [
        ("/demote", "no_tracking_source"),
        ("/promote", "already_active"),
    ] {
        let (status, body) = client.request("POST", path, None).unwrap_or_else(|error| {
            panic!("POST {path} starved behind undrained responses: {error}")
        });
        assert_eq!(status, 409, "POST {path} answered {status}: {body}");
        assert!(
            body.contains(refusal),
            "POST {path} refused with an unexpected body: {body}"
        );
    }

    // The pin is real: a serving-lane read still starves behind the
    // undrained writes, so the actuation answers above rode the split.
    assert!(
        MonitorClient::with_timeout(rig.addr, Duration::from_millis(750))
            .snapshot()
            .is_err(),
        "GET /snapshot answered — the serving lane was never pinned"
    );

    streams.clear();
    assert_recovery(rig.addr);
}

/// The finding end to end: the wedge sits on the very monitor the
/// operator switches over — the active's. Its standby keeps pulling
/// checkpoints through the heartbeat lane, the demote's hint
/// verification makes the same pull, and both role changes execute
/// inside the bound the serving lane could not meet.
#[test]
fn a_wedged_active_still_demotes_and_its_standby_promotes() {
    let active = Rig::deep_history_active();
    let standby = Rig::peer(
        standby_peer(3),
        Some(Driven {
            track: Some(active.addr),
            after_scan: None,
        }),
    );
    for _ in 0..3 {
        active.monitor.paced_scan();
    }
    // Converged and announced: the standby's tracking pull both
    // converges it and records its monitor address on the active —
    // the demotion's announced tracking source.
    standby.client.advance(1).unwrap();
    match standby.client.role().unwrap() {
        report
            if report.role == Role::Standby
                && matches!(report.sync, Some(StandbySync::Tracking { .. })) => {}
        report => panic!("the standby never converged: {report:?}"),
    }

    // The reproduction's wedge, on the active's own monitor.
    let mut streams = starve_on_undrained_history(active.addr);
    thread::sleep(Duration::from_millis(250));

    // The operator's demote executes through it: the hint
    // verification's checkpoint pull against the standby is answered
    // on its own lane, the gate closes at the request's boundary, and
    // the report returns inside the bound — where on the serving lane
    // the request queued behind dead readers indefinitely.
    let active_client = MonitorClient::with_timeout(active.addr, ANSWER_BOUND);
    let report = active_client
        .demote()
        .expect("POST /demote starved behind undrained responses");
    assert_eq!(report.role, Role::Demoting);
    // The standby's promote completes the switchover: its final-sync
    // pull against the wedged active rides the heartbeat lane the
    // same wedge cannot reach.
    let report = MonitorClient::with_timeout(standby.addr, ANSWER_BOUND)
        .promote()
        .expect("POST /promote starved behind undrained responses");
    assert_eq!(report.role, Role::Promoting);

    // The pin is real: a bulk read on the active's monitor still
    // starves behind the undrained writes, so the role changes above
    // rode the control lane.
    assert!(
        MonitorClient::with_timeout(active.addr, Duration::from_millis(750))
            .snapshot()
            .is_err(),
        "GET /snapshot answered — the serving lane was never pinned"
    );

    streams.clear();

    // The switchover settles: the promoted peer's next scan reports
    // settled active, and the demoted peer tracks its successor — the
    // adopted source the demotion pinned — reconverging on its next
    // driven scan.
    standby.client.advance(1).unwrap();
    assert_eq!(standby.client.role().unwrap().role, Role::Active);
    active_client.advance(1).unwrap();
    let report = active_client.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert!(
        matches!(report.sync, Some(StandbySync::Tracking { .. })),
        "the demoted peer never reconverged on its successor: {report:?}"
    );
}

/// The flood half of the finding: while the serving workers sit pinned
/// the requests behind them queue — bounded by the lane depth. A
/// request arriving past the bound gets the refuse worker's `503`
/// instead of queueing without limit, and the heartbeat lane is
/// untouched throughout.
#[test]
fn floods_past_the_lane_queue_are_refused_not_queued() {
    let rig = Rig::deep_history();
    let mut pins = starve_on_undrained_history(rig.addr);
    thread::sleep(Duration::from_millis(250));

    const FLOOD: usize = LANE_DEPTH + 8;
    let flood: Vec<TcpStream> = (0..FLOOD)
        .map(|_| {
            let mut stream = TcpStream::connect(rig.addr).unwrap();
            stream
                .write_all(b"GET /snapshot HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
                .unwrap();
            stream
        })
        .collect();

    // The serving lane's queue holds LANE_DEPTH requests against the
    // pinned workers; the FLOOD - LANE_DEPTH past the bound overflow
    // to the refuse worker's 503 instead of queueing without limit.
    // Which connections land on which side of the bound is a dispatch
    // race — tiny_http parses connections off the accept order — so
    // the refused set is counted, not assumed: every flood connection
    // reads its answer on its own thread, and exactly the overflow
    // share answers 503 while the queued rest answer nothing at all.
    let readers: Vec<_> = flood
        .into_iter()
        .map(|stream| thread::spawn(move || response_status(&stream, ANSWER_BOUND)))
        .collect();
    let (mut refused, mut queued) = (0usize, 0usize);
    for reader in readers {
        match reader.join().unwrap() {
            Some(503) => refused += 1,
            None => queued += 1,
            other => panic!("a flood request answered unexpectedly: {other:?}"),
        }
    }
    assert_eq!(
        refused,
        FLOOD - LANE_DEPTH,
        "requests past the lane bound should be refused 503, not queued"
    );
    assert_eq!(
        queued, LANE_DEPTH,
        "requests inside the lane bound wait on the pinned workers"
    );

    // The heartbeat answers on its own lane the whole time.
    MonitorClient::with_timeout(rig.addr, ANSWER_BOUND)
        .role()
        .expect("GET /role starved behind the flood");

    pins.clear();
    assert_recovery(rig.addr);
}
