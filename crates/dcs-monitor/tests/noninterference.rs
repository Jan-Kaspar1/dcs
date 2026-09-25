//! The consumer non-interference matrix — the load/failure slice of
//! decision 83 and the `WW-FND-004` acceptance: the controller owns
//! execution, and monitoring delivery is a bounded, disposable consumer
//! that can never delay a scan boundary, change an output, or become
//! part of plant liveness.
//!
//! Every matrix case runs one deterministic input script twice — once
//! with no consumers at all, once with the case's consumer behavior
//! overlaid — and asserts byte-identical authoritative artifacts: the
//! field-side record of every output write, the final checkpoint's
//! serialized bytes, the receipt log, and the durable journal file.
//! Reads, malformed requests, refused verbs, and dropped sockets are
//! interference; every receipted command is a run input, so commands
//! live in the deterministic script and never in the interference.
//! Run pacing is likewise script-owned: each case binds a paced
//! monitor, so consumer `POST /scan` attempts meet the named `409`
//! refusal and can never inject a tick.

use dcs_core::{
    Command, CommandError, CommandOutcome, Direction, IoDriver, IoError, PointId,
    PublicationHealth, Role, Sample, StandbySync, Tick, Value, ValueKind,
};
use dcs_model::{PlantModel, SignalIndex};
use dcs_monitor::{Driven, Monitor, MonitorClient, MonitorConfig, PublicationGap};
use dcs_runtime::{
    Component, ComponentIo, ComponentIoExt, DEFAULT_COMMAND_QUEUE_CAPACITY, Executor,
    IoRequirement, Peer, PointMap, StepError,
};
use std::collections::HashMap;
use std::io::{self, Read, Write};
use std::net::{Shutdown, SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU16, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

/// In-memory driver stub — the same minimal stand-in the other monitor
/// suites use — plus the field's write log: every value the executor
/// ever issued to it, in issue order. The log is the scan output
/// image's field-side record — the artifact byte-identity compares
/// most directly, covering every scan rather than only the final image.
struct StubDriver {
    points: Mutex<HashMap<PointId, Sample>>,
    writes: Mutex<Vec<(PointId, Value)>>,
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
            writes: Mutex::new(Vec::new()),
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
        self.writes.lock().unwrap().push((point, value));
        Ok(())
    }
}

/// Reads `In` point 10 and drives `Out` point 20 at gain 2; point 30 is a
/// mapped `Out` point no component writes — the same minimal component
/// the other monitor suites use.
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

fn components() -> Vec<Box<dyn Component>> {
    vec![Box::new(Scale)]
}

fn executor(driver: &'static StubDriver, command_capacity: usize) -> Executor<'static> {
    Executor::new(driver, point_map(), components())
        .unwrap()
        .with_command_queue_capacity(command_capacity)
}

fn write_value(point: u64, value: f64) -> Command {
    Command::WriteValue {
        point: PointId(point),
        kind: ValueKind::Float,
        value: Value::Float(value),
    }
}

fn force(point: u64, value: f64) -> Command {
    Command::ForcePoint {
        point: PointId(point),
        kind: ValueKind::Float,
        value: Value::Float(value),
    }
}

fn unforce(point: u64) -> Command {
    Command::UnforcePoint {
        point: PointId(point),
    }
}

/// A scratch directory per case arm and process — tests run in
/// parallel.
fn scratch(case: &str, arm: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "dcs-noninterference-{case}-{arm}-{}",
        std::process::id()
    ));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

/// Runs `body` while `monitor` serves, always shutting the server down
/// before the driver's borrow ends — a failing assertion must not
/// deadlock the scope join.
fn serving<T>(monitor: &Monitor<'_>, body: impl FnOnce() -> T) -> T {
    let result = thread::scope(|scope| {
        scope.spawn(|| monitor.serve());
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(body));
        monitor.shutdown();
        result
    });
    result.unwrap_or_else(|panic| std::panic::resume_unwind(panic))
}

/// One step of a run's deterministic input script. Every mutation of
/// authoritative state — scans, command submissions, the checkpointed
/// restart — is an op, sequenced by the driving thread so a run's
/// inputs are fixed before any consumer behavior is overlaid. Commands
/// submit through the receipted HTTP path between paced scans, so each
/// one's boundary is the next scan — identical in every run.
#[derive(Clone)]
enum Op {
    /// Drive that many paced scans, a short period's pause between them
    /// so concurrent consumers share the run's duration.
    Scans(u64),
    /// Submit a command through `POST /command`.
    Command(Command),
    /// Capture the checkpoint, stop the monitor, and rebind a fresh one
    /// around an executor restored from it — the checkpointed restart,
    /// the field driver and the journal file carrying on across the
    /// boundary.
    Restart,
}

/// The shared input script: accepted and refused writes, a force and
/// its release — each a receipt plus journaled transitions — and
/// enough scans to fill small bounded windows several times over.
fn base_ops() -> Vec<Op> {
    vec![
        Op::Scans(3),
        Op::Command(write_value(10, 4.0)),
        // Refused at submission: the point is not declared writable.
        Op::Command(write_value(20, 1.0)),
        Op::Scans(2),
        Op::Command(force(10, 7.5)),
        Op::Scans(2),
        Op::Command(unforce(10)),
        Op::Command(write_value(10, -1.5)),
        Op::Scans(4),
    ]
}

/// The queue-saturation script: under the case's capacity-3 bound,
/// three commands queue, the fourth meets the named `queue_full`
/// admission rejection, and a statically invalid command still takes
/// its validation rejection — validation precedes admission. The next
/// scan boundary then drains and applies the three admitted.
fn queue_ops() -> Vec<Op> {
    vec![
        Op::Scans(2),
        Op::Command(write_value(10, 1.0)),
        Op::Command(write_value(10, 2.0)),
        Op::Command(write_value(10, 3.0)),
        // The queue is at its bound: the named `queue_full` rejection.
        Op::Command(write_value(10, 4.0)),
        // Validation still precedes admission even on a full queue.
        Op::Command(write_value(20, 4.0)),
        Op::Scans(2),
        Op::Command(write_value(10, 5.0)),
        Op::Scans(2),
    ]
}

/// The restart script: a command left pending across the boundary —
/// the checkpoint carries it `Accepted` and the restored run applies
/// it — plus a force standing through the restart.
fn restart_ops() -> Vec<Op> {
    vec![
        Op::Scans(3),
        Op::Command(write_value(10, 4.0)),
        Op::Scans(2),
        Op::Command(force(10, 7.5)),
        Op::Scans(2),
        // Still `Accepted` at capture: re-queued by the restore.
        Op::Command(write_value(10, 6.0)),
        Op::Restart,
        Op::Scans(3),
        Op::Command(unforce(10)),
        Op::Command(write_value(10, 8.0)),
        Op::Scans(3),
    ]
}

/// One serving monitor: the `Arc` shares the handle so `stop` can drop
/// the last reference — closing the listener so a later connect is
/// refused, the simulated monitor outage the restart cases use.
struct Serving {
    monitor: Arc<Monitor<'static>>,
    addr: SocketAddr,
    thread: Option<JoinHandle<()>>,
}

impl Serving {
    /// Serves `monitor` on a dedicated thread — the caller binds it
    /// (paced, driven, or plain).
    fn start(monitor: Monitor<'static>) -> Self {
        let monitor = Arc::new(monitor);
        let addr = monitor.local_addr();
        let serving = Arc::clone(&monitor);
        Self {
            monitor,
            addr,
            thread: Some(thread::spawn(move || serving.serve())),
        }
    }

    /// Captures this run's checkpoint, stops the monitor, and rebinds a
    /// fresh paced monitor around an executor restored from it — same
    /// field driver, same journal file, new ephemeral port the
    /// consumers re-resolve through `port`.
    fn restart(self, driver: &'static StubDriver, case: &Case, journal_path: &Path) -> Self {
        let checkpoint = self.monitor.checkpoint();
        self.stop();
        // The outage is held long enough that a consumer in a poll loop
        // reliably meets the refused connect — the restart is the
        // interference under test, not a race.
        thread::sleep(Duration::from_millis(50));
        let executor = Executor::restore(driver, point_map(), components(), &checkpoint, None)
            .unwrap()
            // The queue bound is construction configuration — a
            // checkpoint does not carry it, so the restored run
            // re-declares it.
            .with_command_queue_capacity(case.command_capacity);
        Self::start(
            Monitor::bind_paced_peer_with(
                "127.0.0.1:0",
                Peer::active(executor, None),
                signal_index(),
                case.config(journal_path),
            )
            .unwrap(),
        )
    }

    /// Stops serving and drops the monitor handle: the listener closes
    /// and later connects are refused.
    fn stop(mut self) {
        self.monitor.shutdown();
        if let Some(thread) = self.thread.take() {
            thread.join().unwrap();
        }
    }
}

impl Drop for Serving {
    fn drop(&mut self) {
        self.monitor.shutdown();
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

/// The consumer behavior a case overlays on the run.
#[derive(Clone, Copy)]
enum Interference {
    /// No consumers — the control arm; also proves the harness itself
    /// is deterministic.
    None,
    /// A reader polling every read surface in a loop.
    Polling,
    /// A reader that issues `GET /snapshot` and holds the connection
    /// without reading a byte of the response until the run ends.
    Stalled,
    /// Connect, one read, disconnect — repeated, sometimes dropped
    /// mid-response.
    Churn,
    /// A consumer that reads, goes silent through the run's middle, and
    /// rejoins — the consumer-side restart.
    ConsumerRestart,
    /// Malformed requests of every kind plus a read flood.
    Flood,
}

/// What the consumers observed — the evidence that the interference
/// was real rather than vacuous.
#[derive(Default)]
struct ConsumerLog {
    /// Every HTTP status a consumer read back.
    statuses: Vec<u16>,
    /// Transport failures — connections refused or dropped before an
    /// answer. Expected only across a monitor restart, where the outage
    /// is the point.
    errors: Vec<String>,
    /// Raw probes answered or dropped at the socket level — garbage
    /// bytes and half-sent requests that never became a request.
    probes: u64,
    /// The stalled reader's held response, read back at run end.
    stalled: Option<(u16, String)>,
    /// The rejoining consumer's freshness evidence: the store's
    /// overload counters before it went silent and on its return.
    rejoin: Option<(PublicationHealth, PublicationHealth)>,
}

/// The read surfaces a polling consumer cycles — every read endpoint
/// the contract serves.
const READ_SURFACES: &[&str] = &[
    "/snapshot",
    "/receipts",
    "/history?point=10&since=0",
    "/journal?since=0",
    "/schema",
    "/resources",
    "/checkpoint",
    "/role",
    "/signals",
    "/",
];

/// The monitor address the consumers currently target — re-resolved
/// per request so a monitor restart moves them onto the fresh binding.
fn live_addr(port: &AtomicU16) -> SocketAddr {
    SocketAddr::from(([127, 0, 0, 1], port.load(Ordering::Relaxed)))
}

fn record(log: &Mutex<ConsumerLog>, result: io::Result<(u16, String)>) {
    let mut log = log.lock().unwrap();
    match result {
        Ok((status, _)) => log.statuses.push(status),
        Err(error) => log.errors.push(error.to_string()),
    }
}

/// A normally polling reader: one fresh request per read surface,
/// round-robin, until the run ends.
fn polling(port: &AtomicU16, stop: &AtomicBool, started: &AtomicUsize, log: &Mutex<ConsumerLog>) {
    let mut index = 0;
    let mut contacted = false;
    while !stop.load(Ordering::Relaxed) {
        let client = MonitorClient::new(live_addr(port));
        record(
            log,
            client.request("GET", READ_SURFACES[index % READ_SURFACES.len()], None),
        );
        index += 1;
        if !contacted {
            contacted = true;
            started.fetch_add(1, Ordering::Relaxed);
        }
    }
}

/// A reader that issues its request and then holds the connection
/// without reading a byte of the response until the run ends — the
/// held-response case the publication split exists for. Contact is
/// reported only once the request is issued, so the held connection
/// provably spans the whole script.
fn stalled(port: &AtomicU16, stop: &AtomicBool, started: &AtomicUsize, log: &Mutex<ConsumerLog>) {
    let mut stream = match TcpStream::connect(live_addr(port)) {
        Ok(stream) => stream,
        Err(error) => {
            log.lock().unwrap().errors.push(error.to_string());
            started.fetch_add(1, Ordering::Relaxed);
            return;
        }
    };
    stream
        .write_all(b"GET /snapshot HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        .unwrap();
    started.fetch_add(1, Ordering::Relaxed);
    while !stop.load(Ordering::Relaxed) {
        thread::sleep(Duration::from_millis(5));
    }
    // The answer waited on the wire the whole time: complete, correct,
    // and never holding the scan loop.
    let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
    let mut buf = Vec::new();
    match stream.read_to_end(&mut buf) {
        Ok(_) => {
            let text = String::from_utf8_lossy(&buf).into_owned();
            let status = text
                .lines()
                .next()
                .and_then(|line| line.split_whitespace().nth(1))
                .and_then(|code| code.parse::<u16>().ok())
                .unwrap_or(0);
            log.lock().unwrap().stalled = Some((status, text));
        }
        Err(error) => log.lock().unwrap().errors.push(error.to_string()),
    }
}

/// Disconnect/reconnect churn: each cycle opens a connection, issues a
/// read, reads the response's first bytes — or none — and drops,
/// sometimes mid-response.
fn churn(port: &AtomicU16, stop: &AtomicBool, started: &AtomicUsize, log: &Mutex<ConsumerLog>) {
    let mut index = 0;
    let mut contacted = false;
    while !stop.load(Ordering::Relaxed) {
        if !contacted {
            contacted = true;
            started.fetch_add(1, Ordering::Relaxed);
        }
        let path = READ_SURFACES[index % READ_SURFACES.len()];
        index += 1;
        match TcpStream::connect(live_addr(port)) {
            Ok(mut stream) => {
                let _ = stream.write_all(
                    format!("GET {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
                        .as_bytes(),
                );
                let _ = stream.set_read_timeout(Some(Duration::from_millis(250)));
                let mut buf = [0u8; 512];
                if let Ok(n) = stream.read(&mut buf)
                    && n > 0
                {
                    let head = String::from_utf8_lossy(&buf[..n]).into_owned();
                    if let Some(code) = head
                        .split_whitespace()
                        .nth(1)
                        .and_then(|code| code.parse::<u16>().ok())
                    {
                        log.lock().unwrap().statuses.push(code);
                    }
                }
                // Drop without draining — the disconnect mid-response.
            }
            Err(error) => log.lock().unwrap().errors.push(error.to_string()),
        }
    }
}

/// A consumer that reads, goes silent through the run's middle — the
/// consumer-side restart — then rejoins. The counters it finds on
/// return are the freshness metadata telling it how much it missed.
fn consumer_restart(
    port: &AtomicU16,
    stop: &AtomicBool,
    started: &AtomicUsize,
    log: &Mutex<ConsumerLog>,
) {
    let before = MonitorClient::new(live_addr(port))
        .snapshot()
        .ok()
        .and_then(|snapshot| snapshot.publication);
    started.fetch_add(1, Ordering::Relaxed);
    // The silent stretch — long enough that the bounded windows move
    // well past what it saw.
    let quiet = Instant::now();
    while quiet.elapsed() < Duration::from_millis(300) && !stop.load(Ordering::Relaxed) {
        thread::sleep(Duration::from_millis(10));
    }
    // Rejoin: the overload counters on the served snapshot report the
    // publications produced and evicted while it was away — the named
    // gap, exposed as metadata rather than silent loss.
    let after = MonitorClient::new(live_addr(port))
        .snapshot()
        .ok()
        .and_then(|snapshot| snapshot.publication);
    if let (Some(before), Some(after)) = (before, after) {
        log.lock().unwrap().rejoin = Some((before, after));
    }
    while !stop.load(Ordering::Relaxed) {
        let client = MonitorClient::new(live_addr(port));
        record(log, client.request("GET", READ_SURFACES[0], None));
    }
}

/// Malformed traffic and read flooding within the declared bounds:
/// garbage bytes, unparsable bodies, bad queries, refused verbs —
/// none of it may reach authoritative state — interleaved with a hot
/// read loop over every surface.
fn flood(port: &AtomicU16, stop: &AtomicBool, started: &AtomicUsize, log: &Mutex<ConsumerLog>) {
    /// `(method, path, body)` — every entry either fails to parse or is
    /// refused before touching the run: the valid `POST /scan` is the
    /// paced monitor's `409` proving a consumer cannot inject a tick,
    /// and `POST /promote` on the settled active is the named
    /// `already_active` refusal. No well-formed command appears here —
    /// a receipted command is a run input, not interference.
    const MALFORMED: &[(&str, &str, Option<&str>)] = &[
        ("GET", "/nonexistent", None),
        ("POST", "/snapshot", None),
        ("DELETE", "/receipts", None),
        ("PUT", "/scan", None),
        ("GET", "/history?point=abc", None),
        ("GET", "/history?since=-1", None),
        ("GET", "/journal?since=soon", None),
        ("POST", "/command", Some("{")),
        ("POST", "/command", Some(r#"{"command":{"bogus":1}}"#)),
        ("POST", "/command", Some(r#"{"actor":3}"#)),
        (
            "POST",
            "/command",
            Some(r#"{"write_value":{"point":10,"kind":"float","value":"high"}}"#),
        ),
        ("POST", "/scan", Some("{")),
        ("POST", "/scan", Some(r#"{"scans":-1}"#)),
        ("POST", "/scan", Some(r#"{"scans":1}"#)),
        ("POST", "/promote", None),
    ];
    let mut contacted = false;
    while !stop.load(Ordering::Relaxed) {
        let addr = live_addr(port);
        let client = MonitorClient::new(addr);
        for path in READ_SURFACES {
            record(log, client.request("GET", path, None));
        }
        for &(method, path, body) in MALFORMED {
            record(log, client.request(method, path, body));
        }
        // Raw garbage on the socket — never a request at all.
        if let Ok(mut stream) = TcpStream::connect(addr) {
            let _ = stream.write_all(b"\x89not-an-http-request\x90\r\n\r\n");
            let _ = stream.shutdown(Shutdown::Write);
            let _ = stream.set_read_timeout(Some(Duration::from_millis(250)));
            let mut buf = Vec::new();
            let _ = stream.read_to_end(&mut buf);
            log.lock().unwrap().probes += 1;
        }
        // A request abandoned half-sent.
        if let Ok(mut stream) = TcpStream::connect(addr) {
            let _ = stream.write_all(b"GET /snapshot HTT");
            let _ = stream.shutdown(Shutdown::Both);
            log.lock().unwrap().probes += 1;
        }
        if !contacted {
            contacted = true;
            started.fetch_add(1, Ordering::Relaxed);
        }
    }
}

impl Interference {
    /// How many threads the behavior spawns — the count `run` waits to
    /// see make contact before the script starts, so the interference
    /// provably overlapped the run.
    fn threads(self) -> usize {
        match self {
            Interference::None => 0,
            Interference::Stalled | Interference::ConsumerRestart | Interference::Polling => 1,
            Interference::Churn => 2,
            Interference::Flood => 3,
        }
    }

    /// Spawns the case's consumer behavior; the threads stop on `stop`,
    /// report first contact through `started`, and record into `log`.
    /// `port` always names the live monitor — across a restart the
    /// consumers re-resolve it, exercising their reconnect path.
    fn spawn(
        self,
        port: &Arc<AtomicU16>,
        stop: &Arc<AtomicBool>,
        started: &Arc<AtomicUsize>,
        log: &Arc<Mutex<ConsumerLog>>,
    ) -> Vec<JoinHandle<()>> {
        let body: fn(&AtomicU16, &AtomicBool, &AtomicUsize, &Mutex<ConsumerLog>) = match self {
            Interference::None => return Vec::new(),
            Interference::Polling => polling,
            Interference::Stalled => stalled,
            Interference::Churn => churn,
            Interference::ConsumerRestart => consumer_restart,
            Interference::Flood => flood,
        };
        (0..self.threads())
            .map(|_| {
                let (port, stop, started, log) =
                    (port.clone(), stop.clone(), started.clone(), log.clone());
                thread::spawn(move || body(&port, &stop, &started, &log))
            })
            .collect()
    }
}

/// One matrix case: a deterministic input script plus the consumer
/// behavior overlaid on it.
struct Case {
    name: &'static str,
    ops: Vec<Op>,
    interference: Interference,
    command_capacity: usize,
    history_capacity: usize,
    journal_capacity: usize,
    publication_capacity: usize,
}

impl Case {
    fn new(name: &'static str, ops: Vec<Op>, interference: Interference) -> Self {
        Self {
            name,
            ops,
            interference,
            command_capacity: DEFAULT_COMMAND_QUEUE_CAPACITY,
            history_capacity: 1024,
            journal_capacity: 1024,
            publication_capacity: 64,
        }
    }

    fn queue_capacity(mut self, capacity: usize) -> Self {
        self.command_capacity = capacity;
        self
    }

    fn store_bounds(mut self, history: usize, journal: usize, window: usize) -> Self {
        self.history_capacity = history;
        self.journal_capacity = journal;
        self.publication_capacity = window;
        self
    }

    fn config(&self, journal_path: &Path) -> MonitorConfig {
        MonitorConfig {
            journal_file: Some(journal_path.to_path_buf()),
            history_capacity: self.history_capacity,
            journal_capacity: self.journal_capacity,
            event_history_capacity: self.journal_capacity,
            publication_capacity: self.publication_capacity,
            ..MonitorConfig::default()
        }
    }
}

/// The failure matrix: every case's script runs identically with and
/// without its consumer behavior.
fn matrix() -> Vec<Case> {
    vec![
        // The control arm — the harness itself must be deterministic.
        Case::new("zero-clients", base_ops(), Interference::None),
        Case::new("normal-polling", base_ops(), Interference::Polling),
        Case::new("stalled-reader", base_ops(), Interference::Stalled),
        Case::new("disconnect-reconnect", base_ops(), Interference::Churn),
        // Small bounds so the rejoining consumer's stale view meets a
        // real eviction stretch.
        Case::new(
            "consumer-restart",
            base_ops(),
            Interference::ConsumerRestart,
        )
        .store_bounds(4, 4, 4),
        Case::new("malformed-and-flood", base_ops(), Interference::Flood),
        Case::new("queue-full", queue_ops(), Interference::Polling).queue_capacity(3),
        Case::new("monitor-restart", restart_ops(), Interference::Polling),
        Case::new(
            "monitor-restart-under-flood",
            restart_ops(),
            Interference::Flood,
        ),
    ]
}

/// A run's authoritative artifacts, byte-serialized for the
/// byte-identical comparison against the no-consumer reference.
#[derive(Debug)]
struct Artifacts {
    /// Every write the executor issued to the field, in order — the
    /// scan output image's whole history, not just its final state.
    field_writes: Vec<u8>,
    /// The final checkpoint's serialized bytes — tick, component
    /// states, output and internal images, forces, receipts, and the
    /// command-admission counters.
    checkpoint: Vec<u8>,
    /// The receipt log alone — the run's command audit.
    receipts: Vec<u8>,
    /// The durable journal file's raw bytes — every journaled entry
    /// plus the run-boundary markers separating process lifetimes.
    journal: Vec<u8>,
}

/// Executes one run of `case`: binds the paced monitor with its journal
/// file, spawns the case's consumer behavior when `with_consumers`,
/// then plays the script. Returns the run's authoritative artifacts,
/// the consumers' observation log, and the slowest scan's duration —
/// the scan-boundary delay a consumer could have added.
fn run(case: &Case, with_consumers: bool) -> (Artifacts, ConsumerLog, Duration) {
    let journal_path = scratch(
        case.name,
        if with_consumers {
            "observed"
        } else {
            "reference"
        },
    )
    .join("journal.jsonl");
    // The simulated field — leaked so the monitor threads outlive the
    // borrow, and shared across a restart: the field persists while the
    // controller process comes and goes.
    let driver: &'static StubDriver = Box::leak(Box::new(StubDriver::new(&[
        (PointId(10), Value::Float(0.0)),
        (PointId(20), Value::Float(0.0)),
        (PointId(30), Value::Float(0.0)),
    ])));
    let port = Arc::new(AtomicU16::new(0));
    let stop = Arc::new(AtomicBool::new(false));
    let started = Arc::new(AtomicUsize::new(0));
    let log = Arc::new(Mutex::new(ConsumerLog::default()));

    // Paced binding: the run's wall-clock shell owns the schedule, so
    // consumer traffic can never inject a tick.
    let mut monitor = Serving::start(
        Monitor::bind_paced_peer_with(
            "127.0.0.1:0",
            Peer::active(executor(driver, case.command_capacity), None),
            signal_index(),
            case.config(&journal_path),
        )
        .unwrap(),
    );
    port.store(monitor.addr.port(), Ordering::Relaxed);
    let consumers = if with_consumers {
        case.interference.spawn(&port, &stop, &started, &log)
    } else {
        Vec::new()
    };
    // Wait until every consumer thread has made contact before the
    // script starts — the interference provably overlapped the run.
    let contact = Instant::now();
    while started.load(Ordering::Relaxed) < consumers.len() {
        assert!(
            contact.elapsed() < Duration::from_secs(10),
            "{}: consumers never made contact",
            case.name
        );
        thread::yield_now();
    }

    let mut slowest = Duration::ZERO;
    for op in &case.ops {
        match op {
            Op::Scans(scans) => {
                for _ in 0..*scans {
                    let start = Instant::now();
                    monitor.monitor.paced_scan();
                    slowest = slowest.max(start.elapsed());
                    // The paced period's stand-in: gives concurrent
                    // consumers a real share of the run's duration.
                    thread::sleep(Duration::from_millis(2));
                }
            }
            Op::Command(command) => {
                MonitorClient::new(monitor.addr).command(command).unwrap();
            }
            Op::Restart => {
                monitor = monitor.restart(driver, case, &journal_path);
                port.store(monitor.addr.port(), Ordering::Relaxed);
            }
        }
    }

    stop.store(true, Ordering::Relaxed);
    for consumer in consumers {
        consumer.join().unwrap();
    }

    let checkpoint = monitor.monitor.checkpoint();
    let mut artifacts = Artifacts {
        field_writes: serde_json::to_vec(&*driver.writes.lock().unwrap()).unwrap(),
        checkpoint: serde_json::to_vec(&checkpoint).unwrap(),
        receipts: serde_json::to_vec(&checkpoint.receipts).unwrap(),
        journal: Vec::new(),
    };
    monitor.stop();
    artifacts.journal = std::fs::read(&journal_path).unwrap();
    (
        artifacts,
        std::mem::take(&mut *log.lock().unwrap()),
        slowest,
    )
}

/// Asserts the interference actually happened and saw the right
/// surface — a case whose consumers never ran would prove nothing.
fn check_consumer_log(case: &Case, log: &ConsumerLog) {
    match case.interference {
        Interference::None => {
            assert!(log.statuses.is_empty(), "{}", case.name);
        }
        Interference::Stalled => {
            let (status, body) = log
                .stalled
                .as_ref()
                .expect("the stalled reader's held response");
            assert_eq!(*status, 200, "{}: {body}", case.name);
            assert!(body.contains("\"tick\""), "{}: {body}", case.name);
        }
        Interference::ConsumerRestart => {
            let (before, after) = log.rejoin.expect("the consumer rejoined");
            assert!(
                after.published > before.published,
                "{}: the rejoining consumer found no freshness advance",
                case.name
            );
            assert!(
                after.coalesced > before.coalesced || after.coalesced > 0,
                "{}: bounded eviction while the consumer was away never reported",
                case.name
            );
        }
        Interference::Polling | Interference::Churn | Interference::Flood => {
            assert!(
                !log.statuses.is_empty(),
                "{}: consumers never ran",
                case.name
            );
        }
    }
    // The monitor-restart outage is the only case where a consumer may
    // see anything but a clean answer: a connect refused while the
    // listener is down, or — a tiny_http shutdown artifact — a 500 for
    // a request the dying monitor had accepted but not yet dispatched
    // (the request object drops unresponded, which the library answers
    // as an internal error). Both are the consumer observing the
    // outage; neither touches the run. Everywhere else no consumer may
    // ever meet a server fault.
    let expects_outage = case.ops.iter().any(|op| matches!(op, Op::Restart));
    if expects_outage {
        assert!(
            !log.errors.is_empty() || log.statuses.iter().any(|&status| status >= 500),
            "{}: consumers never noticed the monitor restart",
            case.name
        );
    } else {
        assert!(
            log.statuses.iter().all(|&status| status < 500),
            "{}: a consumer saw a server fault: {:?}",
            case.name,
            log.statuses
        );
        match case.interference {
            Interference::Polling | Interference::ConsumerRestart | Interference::Flood => {
                assert!(
                    log.errors.is_empty(),
                    "{}: consumer errors outside a restart: {:?}",
                    case.name,
                    log.errors
                );
            }
            _ => {}
        }
    }
}

/// The parameterized non-interference proof: every matrix case runs its
/// deterministic script twice — once with no consumers at all, once
/// under the case's consumer behavior — and the runs must produce
/// byte-identical authoritative artifacts.
#[test]
fn consumer_failures_never_change_authoritative_artifacts() {
    for case in matrix() {
        let (reference, reference_log, _) = run(&case, false);
        let (observed, log, slowest) = run(&case, true);

        assert!(
            reference_log.statuses.is_empty(),
            "{}: the no-consumer reference served consumers",
            case.name
        );
        assert_eq!(
            reference.field_writes, observed.field_writes,
            "{}: consumer interference changed the scan output image",
            case.name
        );
        assert_eq!(
            reference.checkpoint, observed.checkpoint,
            "{}: consumer interference changed the checkpoint",
            case.name
        );
        assert_eq!(
            reference.receipts, observed.receipts,
            "{}: consumer interference changed the receipt log",
            case.name
        );
        assert_eq!(
            reference.journal, observed.journal,
            "{}: consumer interference changed the journal",
            case.name
        );
        assert!(
            slowest < Duration::from_secs(5),
            "{}: a scan boundary waited {slowest:?} behind consumer load",
            case.name
        );
        check_consumer_log(&case, &log);
    }
}

/// The held-response-connection case standalone: the reader holds its
/// socket across the whole paced batch; every scan completes on its own
/// boundary and the buffered answer is still delivered complete.
#[test]
fn a_stalled_reader_adds_no_scan_boundary_delay_and_no_liveness_dependency() {
    let driver = StubDriver::new(&[
        (PointId(10), Value::Float(0.0)),
        (PointId(20), Value::Float(0.0)),
        (PointId(30), Value::Float(0.0)),
    ]);
    let executor = Executor::new(&driver, point_map(), components()).unwrap();
    let monitor = Monitor::bind_paced("127.0.0.1:0", executor, signal_index()).unwrap();
    let addr = monitor.local_addr();
    serving(&monitor, || {
        let mut stalled = TcpStream::connect(addr).unwrap();
        stalled
            .write_all(b"GET /snapshot HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            .unwrap();
        // The held response connection spans the batch: the served
        // copy was materialized and serialized off the executor
        // lock, so the paced loop's scans complete on schedule.
        let deadline = Instant::now() + Duration::from_secs(10);
        for _ in 0..20 {
            monitor.paced_scan();
        }
        assert!(
            Instant::now() < deadline,
            "the paced loop stalled behind a silent reader"
        );
        // And the plant kept scanning whether or not the answer was
        // ever read — no liveness dependency on the consumer.
        stalled
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        let mut buf = Vec::new();
        stalled.read_to_end(&mut buf).unwrap();
        let response = String::from_utf8_lossy(&buf).into_owned();
        assert!(
            response
                .lines()
                .next()
                .is_some_and(|line| line.contains("200")),
            "{response}"
        );
        assert!(response.contains("\"tick\""), "{response}");
        assert_eq!(monitor.tick(), Tick(20));
    });
}

/// The bounded ingress's full-queue case standalone: a validated
/// command past the bound takes the named `queue_full` rejection
/// receipt — never queueing, never fire-and-forget — while admitted
/// commands settle as ordinary receipts at the scan boundary.
#[test]
fn a_full_admission_queue_answers_queue_full_and_stays_bounded() {
    let driver = StubDriver::new(&[
        (PointId(10), Value::Float(0.0)),
        (PointId(20), Value::Float(0.0)),
        (PointId(30), Value::Float(0.0)),
    ]);
    let executor = Executor::new(&driver, point_map(), components())
        .unwrap()
        .with_command_queue_capacity(2);
    let monitor = Monitor::bind("127.0.0.1:0", executor, signal_index()).unwrap();
    let client = MonitorClient::new(monitor.local_addr());
    serving(&monitor, || {
        client.advance(1).unwrap();

        // Two admissions fill the bound.
        for value in [1.0, 2.0] {
            let receipt = client.command(&write_value(10, value)).unwrap();
            assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        }
        // The third validated command meets the named rejection —
        // receipted, never queued.
        let receipt = client.command(&write_value(10, 3.0)).unwrap();
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::QueueFull {
                    point: Some(PointId(10)),
                    capacity: 2,
                }
            }
        );
        // Validation still precedes admission: a statically invalid
        // command takes its own named rejection even on a full queue.
        let receipt = client.command(&write_value(20, 3.0)).unwrap();
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::NotWritable { point: PointId(20) }
            }
        );
        // The admission metrics ride the snapshot's command_queue
        // section — depth bounded by capacity throughout.
        let queue = monitor.snapshot().command_queue;
        assert_eq!(queue.attempts, 4);
        assert_eq!(queue.full_rejections, 1);
        assert_eq!(queue.capacity, 2);
        assert_eq!(queue.depth, 2);
        assert_eq!(queue.high_water, 2);

        // The boundary drains the queue: admitted commands settle as
        // ordinary applied receipts in submission order, the
        // rejections already final.
        client.advance(1).unwrap();
        let receipts = client.receipts().unwrap();
        assert_eq!(receipts.len(), 4);
        assert_eq!(
            receipts[0].outcome,
            CommandOutcome::Applied { tick: Tick(2) }
        );
        assert_eq!(
            receipts[1].outcome,
            CommandOutcome::Applied { tick: Tick(2) }
        );
        assert!(matches!(
            receipts[2].outcome,
            CommandOutcome::Rejected {
                reason: CommandError::QueueFull { .. }
            }
        ));
        assert_eq!(monitor.snapshot().command_queue.depth, 0);
    });
}

/// Bounded eviction and coalescing are exposed, never silent: the
/// lagging consumer sees the named [`PublicationGap`] on the seq-cursor
/// read, numbering gaps on the history and journal streams, and the
/// `coalesced` counter on the served snapshot's `publication` section —
/// the freshness metadata a consumer reads to learn how far behind it
/// has fallen.
#[test]
fn bounded_eviction_exposes_named_gaps_never_silent_loss() {
    let driver = StubDriver::new(&[
        (PointId(10), Value::Float(0.0)),
        (PointId(20), Value::Float(0.0)),
        (PointId(30), Value::Float(0.0)),
    ]);
    let executor = Executor::new(&driver, point_map(), components()).unwrap();
    let monitor = Monitor::bind_with(
        "127.0.0.1:0",
        executor,
        signal_index(),
        MonitorConfig {
            history_capacity: 3,
            journal_capacity: 4,
            event_history_capacity: 4,
            publication_capacity: 3,
            journal_file: None,
            ..MonitorConfig::default()
        },
    )
    .unwrap();
    let client = MonitorClient::new(monitor.local_addr());
    serving(&monitor, || {
        // Enough scans and commands to evict well past every bound.
        client.advance(4).unwrap();
        for value in [1.0, 2.0, 3.0] {
            client.command(&write_value(10, value)).unwrap();
            client.advance(1).unwrap();
        }
        client.advance(3).unwrap();
        // Seed + 10 scans = 11 publications; the window holds 3.
        let health = client.snapshot().unwrap().publication.unwrap();
        assert_eq!(health.published, 11);
        assert_eq!(health.depth, 3);
        assert_eq!(health.window, 3);
        assert_eq!(health.coalesced, 8);

        // A lagging seq-cursor read names the evicted stretch.
        let page = monitor.publications_since(2);
        assert_eq!(page.gap, Some(PublicationGap { through: 8 }));
        assert_eq!(
            page.publications
                .iter()
                .map(|publication| publication.seq)
                .collect::<Vec<_>>(),
            vec![9, 10, 11]
        );
        // The retained tail still reports its own position — the
        // freshest publication's seq and tick are the freshness
        // metadata a consumer compares against its last-seen.
        let latest = monitor.published().unwrap();
        assert_eq!(latest.seq, 11);
        assert_eq!(latest.tick, Tick(10));

        // The journal stream: a stale `since` cursor returns the
        // retained tail whose first seq tells the consumer the
        // stretch it lost — a numbering gap, never silent loss.
        let journal = client.journal(0).unwrap();
        assert_eq!(journal.len(), 4);
        let first = journal[0].seq;
        assert!(first > 1, "evicted journal seqs left no trace");
        let tail = client.journal(first - 1).unwrap();
        assert_eq!(tail, journal);
        assert!(
            tail[0].seq > first - 1,
            "the gap between cursor and tail is detectable"
        );

        // The history stream behaves identically: retained samples
        // start above the evicted seqs.
        let history = client.history(&[PointId(10)], 0).unwrap();
        assert_eq!(history[0].samples.len(), 3);
        assert!(history[0].samples[0].seq > 1);
        let stale = client.history(&[PointId(10)], 1).unwrap();
        assert_eq!(stale, history);
        assert!(stale[0].samples[0].seq > 2);
    });
}

/// Every read surface serves the published read model under every
/// execution mode — paced scans, unpaced `POST /scan`, standby
/// tracking, and across a checkpointed restart. The `publication`
/// section is stamped only by the publication store, so its presence
/// on the served snapshot proves the copy came off the store rather
/// than the executor's own view.
#[test]
fn published_reads_cover_every_execution_mode() {
    /// Asserts every read surface answers the published copy.
    fn assert_published_surfaces(monitor: &Monitor, client: &MonitorClient, tick: Tick) {
        let published = monitor
            .published()
            .expect("a bound monitor publishes a seed read model");
        let snapshot = client.snapshot().unwrap();
        assert_eq!(snapshot, published.snapshot);
        assert_eq!(snapshot.tick, tick);
        assert!(
            snapshot.publication.is_some(),
            "the served snapshot is the store's stamped copy"
        );
        // The receipt mirror answers the authoritative log as it
        // stands — visible between scans.
        assert_eq!(client.receipts().unwrap(), monitor.checkpoint().receipts);
        // The executor's between-scans capture — the standby's pull
        // target — is consistent with the in-process view, modulo
        // `line_owner`: the serving monitor stamps its own address on
        // the wire form as owner-propagation decoration, never part of
        // the captured run state.
        let mut served = client.checkpoint().unwrap();
        served.line_owner = None;
        assert_eq!(served, monitor.checkpoint());
        // The bounded streams answer from the store.
        let history = client.history(&[PointId(10), PointId(20)], 0).unwrap();
        assert!(history.iter().all(|history| !history.samples.is_empty()));
        let journal = client.journal(0).unwrap();
        assert!(
            journal.windows(2).all(|pair| pair[0].seq < pair[1].seq),
            "journal seqs strictly increase"
        );
        assert_eq!(client.signals().unwrap(), signal_index());
        assert_eq!(client.role().unwrap().tick, monitor.tick());
        assert!(client.page().unwrap().contains("<title>"));
    }

    let driver_points = || {
        StubDriver::new(&[
            (PointId(10), Value::Float(0.0)),
            (PointId(20), Value::Float(0.0)),
            (PointId(30), Value::Float(0.0)),
        ])
    };

    // ---- Paced operation.
    {
        let driver = driver_points();
        let executor = Executor::new(&driver, point_map(), components()).unwrap();
        let monitor = Monitor::bind_paced("127.0.0.1:0", executor, signal_index()).unwrap();
        let client = MonitorClient::new(monitor.local_addr());
        serving(&monitor, || {
            for _ in 0..3 {
                monitor.paced_scan();
            }
            assert_published_surfaces(&monitor, &client, Tick(3));
        });
    }

    // ---- Unpaced `POST /scan` operation.
    {
        let driver = driver_points();
        let executor = Executor::new(&driver, point_map(), components()).unwrap();
        let monitor = Monitor::bind("127.0.0.1:0", executor, signal_index()).unwrap();
        let client = MonitorClient::new(monitor.local_addr());
        serving(&monitor, || {
            // The request's answer is the same published copy the
            // next `GET /snapshot` serves.
            let advanced = client.advance(3).unwrap();
            assert!(advanced.publication.is_some());
            assert_eq!(client.snapshot().unwrap(), advanced);
            assert_published_surfaces(&monitor, &client, Tick(3));
        });
    }

    // ---- Standby tracking: the driven standby pulls the active's
    // checkpoint per requested scan and serves the same published
    // surfaces from its own store.
    {
        let active_driver = driver_points();
        let standby_driver = driver_points();
        let active_executor = Executor::new(&active_driver, point_map(), components()).unwrap();
        let standby_executor = Executor::new(&standby_driver, point_map(), components()).unwrap();
        let active = Monitor::bind("127.0.0.1:0", active_executor, signal_index()).unwrap();
        let active_addr = active.local_addr();
        let standby = Monitor::bind_peer(
            "127.0.0.1:0",
            Peer::standby(standby_executor, None),
            signal_index(),
        )
        .unwrap()
        .driven(Driven {
            track: Some(active_addr),
            after_scan: None,
        });
        let active_client = MonitorClient::new(active_addr);
        let standby_client = MonitorClient::new(standby.local_addr());
        let result = thread::scope(|scope| {
            scope.spawn(|| active.serve());
            scope.spawn(|| standby.serve());
            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                active_client.advance(4).unwrap();
                // Each requested scan pulls the active's checkpoint
                // first — the driven tracking cycle.
                standby_client.advance(2).unwrap();
                let report = standby_client.role().unwrap();
                assert_eq!(report.role, Role::Standby);
                assert_eq!(
                    report.sync,
                    Some(StandbySync::Tracking { aligned: Tick(4) })
                );
                // Tick 6, not 5: the second pull repeats the tick-4
                // checkpoint and a tracking apply never rewinds the
                // run's clock — it lands at the run's own tick, so the
                // second requested scan produces tick 6 rather than
                // re-recording tick 5.
                assert_published_surfaces(&standby, &standby_client, Tick(6));
                // The role gate holds: a command on the tracking peer
                // takes the named `not_active` rejection receipt, never
                // a phantom application.
                let receipt = standby_client.command(&write_value(10, 1.0)).unwrap();
                assert_eq!(
                    receipt.outcome,
                    CommandOutcome::Rejected {
                        reason: CommandError::NotActive {
                            point: Some(PointId(10)),
                            role: Role::Standby,
                        }
                    }
                );
            }));
            active.shutdown();
            standby.shutdown();
            result
        });
        result.unwrap_or_else(|panic| std::panic::resume_unwind(panic));
    }

    // ---- Across a checkpointed restart: the restored run resumes the
    // tick, the journal file continues the served `seq` domain, and
    // every surface keeps answering published copies.
    {
        let dir = scratch("mode-restart", "run");
        let journal_path = dir.join("journal.jsonl");
        let driver: &'static StubDriver = Box::leak(Box::new(driver_points()));
        let executor = Executor::new(driver, point_map(), components()).unwrap();
        let config = || MonitorConfig {
            journal_file: Some(journal_path.clone()),
            ..MonitorConfig::default()
        };
        let monitor = Monitor::bind_paced_peer_with(
            "127.0.0.1:0",
            Peer::active(executor, None),
            signal_index(),
            config(),
        )
        .unwrap();
        let first_client = MonitorClient::new(monitor.local_addr());
        let checkpoint = serving(&monitor, || {
            for _ in 0..4 {
                monitor.paced_scan();
            }
            first_client.command(&write_value(10, 3.0)).unwrap();
            monitor.paced_scan();
            assert_published_surfaces(&monitor, &first_client, Tick(5));
            monitor.checkpoint()
        });

        // The restart: the old monitor's process lifetime ends — its
        // drop releases the journal file's writer lock — then a fresh
        // executor restored from the checkpoint and a fresh monitor
        // replaying the same journal file take over.
        drop(monitor);
        let restored =
            Executor::restore(driver, point_map(), components(), &checkpoint, None).unwrap();
        let monitor = Monitor::bind_paced_peer_with(
            "127.0.0.1:0",
            Peer::active(restored, None),
            signal_index(),
            config(),
        )
        .unwrap();
        let client = MonitorClient::new(monitor.local_addr());
        serving(&monitor, || {
            monitor.paced_scan();
            client.command(&write_value(10, 5.0)).unwrap();
            monitor.paced_scan();
            assert_published_surfaces(&monitor, &client, Tick(7));
            // The served journal is continuous across the restart:
            // the replayed pre-restart entries stand beside the
            // post-restart ones in one `seq` domain — the restart's
            // served boundary entry separates the lifetimes, and the
            // post-restart command settles at tick 7.
            let journal = client.journal(0).unwrap();
            assert_eq!(journal[0].seq, 1);
            assert!(
                journal.iter().any(|entry| entry.tick == Tick(7)),
                "the post-restart entries continue the same stream"
            );
        });
    }
}
