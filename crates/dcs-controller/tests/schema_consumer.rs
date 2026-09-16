//! The M12 closing verification — the schema-driven-blocks and
//! disposable-UI tranche's done criteria walked end to end as one
//! scripted, tick-paced run over the driven redundant pair on the
//! shared simulated plant, composing the slices that proved them
//! piecemeal — #375's served registry and live resource state, #376's
//! consumer non-interference matrix, #377's named commands and typed
//! emitted events, #379's generic five-category faceplate, #381's pair
//! semantics, and #382's checked-in composition — the same closing
//! shape #161 gave M5 and #178 gave M8.
//!
//! Run it with:
//!
//! ```text
//! cargo test -p dcs-controller --test schema_consumer
//! ```
//!
//! ## Rig
//!
//! `full_stack`'s driven rig: two `dcs-plant-server` processes each
//! serve the showcase model merged with its dynamics document — one is
//! the pair's shared field, the other paces the uninterrupted
//! no-consumer reference run every leg compares against. The pair runs
//! `dcs-controller --driven` — an active and a tracking standby
//! attached over `sim-tcp`, each journaling to a durable file — and a
//! third driven controller runs the reference on its own plant. Every
//! scan happens inside a `POST /scan` request the script issues, so
//! nothing is wall-clock paced and consumer traffic can only ever
//! read: the driven monitor's `POST /scan` is the driver's channel, so
//! the flood carries only its malformed forms, exactly as
//! `reference-plant/ci/consumers.py` rules them.
//!
//! ## The legs, in script order
//!
//! 1. **no-ui** — no consumers attached. The decision-82 sweep runs at
//!    the leg's boundary: `GET /schema` covers every instantiated kind
//!    once per instance, each served [`BlockInterface`] carrying all
//!    five collections and equal to the descriptor's own derivation
//!    plus the serving layer's bound-point and signal-unit
//!    annotations; `GET /resources`' live values equal the snapshot's;
//!    and the page carries the generic five-category renderers — the
//!    composed plant needs no kind-specific markup.
//! 2. **polling** — a reader cycling every read surface across both
//!    peers while the declared `advance` invocation settles `Applied`
//!    at its scan boundary.
//! 3. **stalled-reader** — a `GET /snapshot` held unread on the
//!    tracking peer through the leg while the declared-unavailable
//!    `advance` on the completed table settles `Rejected` carrying the
//!    kind's named `command_refused` reason.
//! 4. **disconnect-reconnect** — connect/read/drop churn on both peers
//!    while `reset` applies and the batch program's held `run` input
//!    raises.
//! 5. **malformed-and-flood** — refused verbs, parse failures, garbage
//!    bytes, and the hot read loop against the field owner — beside a
//!    script-side command flood that fills the bounded admission
//!    queue: the next submissions answer the named `queue_full`
//!    rejection, a statically invalid write still takes its
//!    `not_writable` validation refusal, and the queued commands all
//!    settle `Applied` at the next boundary.
//! 6. **ui-restart** — a real UI consumer process (a `python3` polling
//!    loop in the `consumers.py --ui-client` shape — `signals` once,
//!    then `snapshot`/`journal`/`history` per pass, recording each
//!    observation the way the served page's poll would) killed at the
//!    leg's midpoint and respawned, rejoining on the served
//!    publication counters; the batch program's first `step_completed`
//!    lands at its producing tick on every journal and in the served
//!    recent-events view while it does.
//! 7. **promotion** — demote, then promote, with an admitted `reset`
//!    invocation still in flight: the promote boundary's final
//!    checkpoint pull carries it and it settles exactly once on the
//!    new active — as it does on the demoted peer's own queue and the
//!    reference's.
//! 8. **polling** again and a closing **no-ui** leg against the
//!    promoted peer: the restarted table's second `step_completed`
//!    keeps the emitted-event sequence monotonic across the switch.
//!
//! Every leg boundary compares the pair view's snapshot, the receipt
//! log, and the checkpoint digest — model fingerprint normalized, the
//! pair and reference models differing only in the plant address —
//! against the no-consumer reference, and every tick asserts all
//! three snapshots equal and the field carrying the owner's write.
//! The whole run executes twice; the returned outcome must be
//! identical.

use dcs_core::{
    AdaptedCommand, AdaptedEvent, BlockInterface, Command, CommandAvailability, CommandError,
    CommandOutcome, CommandReceipt, EmittedEvent, EventEmission, EventRetention, EventValue,
    INTERFACE_VERSION, JournalEntry, JournalEvent, PointId, Role, RoleReport, Sample, StandbySync,
    TelemetrySnapshot, Tick, Value, ValueKind,
};
use dcs_demo::showcase::{self, BATCH_STEP1_TICKS, points};
use dcs_monitor::{MonitorClient, PairClient, PeerStatus, read_journal_file};
use dcs_runtime::{Checkpoint, DEFAULT_COMMAND_QUEUE_CAPACITY};
use dcs_sim_net::RemoteDriver;
use std::collections::BTreeMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{Shutdown, SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStderr, Command as Process, Stdio};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

/// The controller binary under test.
const CONTROLLER: &str = env!("CARGO_BIN_EXE_dcs-controller");
/// The showcase plant model the plant servers load — the #69 fixture.
const PLANT_MODEL: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/showcase.json"
);
/// The showcase's plant-side dynamics document.
const PLANT_DYNAMICS: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/showcase_dynamics.json"
);
/// The controller-side model source — the checked-in showcase
/// composition whose devices [`controller_model`] re-points at
/// `sim-tcp`.
const MODEL_SOURCE: &str = include_str!("../../dcs-demo/fixtures/showcase.json");

/// Process time advanced per scan — the model PID's configured dt.
const DT: &str = "0.1";
/// The batch program's instance name — the showcase's `sequencer`
/// component, the checked-in composition's kind carrying the declared
/// command/event contract.
const BATCH_COMPONENT: &str = "sequencer:20";
/// The stand-in poll interval a consumer leg's scans leave between
/// ticks — concurrent consumers share the run's duration.
const CONSUMER_IDLE: Duration = Duration::from_millis(2);

/// Leg lengths, in ticks. The batch program's first step runs
/// `BATCH_STEP1_TICKS` scans: the `run` write applies at
/// [`RUN_TICK`], so `step_completed` emits at [`EVENT_TICK`]; the
/// carried `reset` applies at [`CARRIED_TICK`], so the restarted
/// table's second emission lands at [`SECOND_EVENT_TICK`].
const NO_UI_TICKS: u64 = 6;
const POLLING_TICKS: u64 = 8;
const STALLED_TICKS: u64 = 8;
const CHURN_TICKS: u64 = 8;
const FLOOD_TICKS: u64 = 11;
const UI_RESTART_TICKS: u64 = 28;
const POST_SWITCH_TICKS: u64 = 8;
const CLOSE_TICKS: u64 = 32;

/// The tick the completing `advance` invocation applies.
const ADVANCE_TICK: u64 = NO_UI_TICKS + 1;
/// The tick the declared-unavailable `advance` settles refused.
const REFUSED_TICK: u64 = NO_UI_TICKS + POLLING_TICKS + 1;
/// The tick the first `reset` invocation applies.
const RESET_TICK: u64 = REFUSED_TICK + STALLED_TICKS;
/// The tick the held `run` write applies — the batch program starts;
/// the write is submitted one tick before the churn leg's end.
const RUN_TICK: u64 = RESET_TICK + CHURN_TICKS - 1;
/// The tick the admission-queue flood drains — the 64 queued writes
/// all apply.
const DRAIN_TICK: u64 = RUN_TICK + 1;
/// The producing tick of the first `step_completed` — pre-promotion.
const EVENT_TICK: u64 = RUN_TICK + BATCH_STEP1_TICKS - 1;
/// The tick the carried `reset` settles on all three controllers —
/// the promoted peer's first scan.
const CARRIED_TICK: u64 = EVENT_TICK + 1;
/// The restarted table's second `step_completed` — post-promotion.
const SECOND_EVENT_TICK: u64 = CARRIED_TICK + BATCH_STEP1_TICKS - 1;
/// The run's final tick.
const END_TICK: u64 = SECOND_EVENT_TICK;

/// A `dcs-*` binary sibling of the controller binary under test in the
/// workspace target dir; workspace builds produce them.
fn workspace_binary(name: &str) -> PathBuf {
    let binary = Path::new(CONTROLLER)
        .parent()
        .unwrap()
        .join(format!("{name}{}", std::env::consts::EXE_SUFFIX));
    assert!(
        binary.is_file(),
        "{} not found — build the workspace first",
        binary.display()
    );
    binary
}

/// A spawned process: its bound address learned from the `listening on`
/// stderr line, stderr held open so a later diagnostic write never meets
/// a closed pipe, and a kill on drop so a panicking test leaves no stray
/// processes behind.
struct Spawned {
    child: Child,
    addr: SocketAddr,
    _stderr: BufReader<ChildStderr>,
}

impl Drop for Spawned {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// Spawns `binary`, reads its `listening on <addr>` line, and returns
/// the running process.
fn spawn(binary: &Path, args: &[String]) -> Spawned {
    let mut child = Process::new(binary)
        .args(args)
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap_or_else(|error| panic!("cannot spawn {}: {error}", binary.display()));
    let mut stderr = BufReader::new(child.stderr.take().unwrap());
    let mut line = String::new();
    if stderr.read_line(&mut line).unwrap() == 0 {
        panic!("{} exited before reporting its address", binary.display());
    }
    let addr = line
        .trim()
        .strip_prefix("listening on ")
        .unwrap_or_else(|| {
            panic!(
                "expected a `listening on` line from {}, found {line:?}",
                binary.display()
            )
        })
        .parse()
        .unwrap();
    Spawned {
        child,
        addr,
        _stderr: stderr,
    }
}

/// A plant-server process serving the showcase plant — model plus
/// dynamics document — on an ephemeral port.
fn spawn_plant() -> Spawned {
    spawn(
        &workspace_binary("dcs-plant-server"),
        &[
            PLANT_MODEL.to_string(),
            "--dynamics".to_string(),
            PLANT_DYNAMICS.to_string(),
            "--listen".to_string(),
            "127.0.0.1:0".to_string(),
        ],
    )
}

/// A `--driven` controller process on `model`: the monitor serves on an
/// ephemeral port and scans run only when `POST /scan` requests them.
fn spawn_controller(model: &Path, extra: &[String]) -> Spawned {
    let mut args = vec![model.to_str().unwrap().to_string()];
    args.extend(extra.iter().cloned());
    for arg in ["--listen", "127.0.0.1:0", "--driven", "--dt", DT] {
        args.push(arg.to_string());
    }
    spawn(Path::new(CONTROLLER), &args)
}

/// Writes the controller-side model for a plant server at `plant`: the
/// shared showcase model with every declared channel merged onto one
/// `sim-tcp` device carrying the plant's address — the remote-sim path
/// through the assembly driver registry. One remote backend steps the
/// plant once per scan, keeping the dynamics document's dt pacing.
fn controller_model(dir: &Path, name: &str, plant: SocketAddr) -> PathBuf {
    let mut document: serde_json::Value = serde_json::from_str(MODEL_SOURCE).unwrap();
    let mut channels = serde_json::Map::new();
    for device in document["devices"].as_array().unwrap() {
        for (channel, declaration) in device["channels"].as_object().unwrap() {
            channels.insert(channel.clone(), declaration.clone());
        }
    }
    document["devices"] = serde_json::json!([{
        "id": 1,
        "kind": "sim-tcp",
        "parameters": { "address": plant.to_string() },
        "channels": channels,
    }]);
    for point in document["io_points"].as_array_mut().unwrap() {
        if let Some(channel) = point.get_mut("channel") {
            channel["device"] = serde_json::json!(1);
        }
    }
    let path = dir.join(name);
    std::fs::write(&path, serde_json::to_string_pretty(&document).unwrap()).unwrap();
    path
}

/// The sample `snapshot`'s image reports for `point`.
fn image_sample(snapshot: &TelemetrySnapshot, point: PointId) -> Sample {
    showcase::sample(snapshot, point).expect("the showcase maps every named point")
}

/// A `Command::Invoke` against the batch program — the declared-command
/// submission the bounded, receipted command path carries.
fn invoke(command: &str, arguments: &[(&str, Value)]) -> Command {
    Command::Invoke {
        component: BATCH_COMPONENT.to_string(),
        command: command.to_string(),
        arguments: BTreeMap::from_iter(
            arguments
                .iter()
                .map(|(name, value)| (name.to_string(), *value)),
        ),
    }
}

/// The batch program's held `run` input — the ordinary writable-point
/// write the declared commands do not alias.
fn batch_run(running: bool) -> Command {
    Command::WriteValue {
        point: points::BATCH_RUN,
        kind: ValueKind::Bool,
        value: Value::Bool(running),
    }
}

/// The `step_completed` event the batch program emits on a step's
/// completing scan — the kind-declared emission the journal and the
/// served recent-events view carry.
fn step_completed(step: i64) -> JournalEvent {
    JournalEvent::EventEmitted {
        event: EmittedEvent {
            event: "step_completed".to_string(),
            component: BATCH_COMPONENT.to_string(),
            fields: [("step".to_string(), EventValue::Value(Value::Int(step)))]
                .into_iter()
                .collect(),
        },
    }
}

/// The journal's emitted-event stream — `(tick, event)` per
/// `EventEmitted` entry: the comparable shape the identical-streams
/// assertions read.
fn emitted(client: &MonitorClient) -> Vec<(u64, EmittedEvent)> {
    client
        .journal(0)
        .unwrap()
        .iter()
        .filter_map(|entry| match &entry.event {
            JournalEvent::EventEmitted { event } => Some((entry.tick.0, event.clone())),
            _ => None,
        })
        .collect()
}

/// The journaled settlements of one exact command — `(tick, outcome)`
/// per `CommandSettled` entry whose receipt carries `command`, however
/// it settled — the exactly-once count for a carried invocation.
fn settlements_of(client: &MonitorClient, command: &Command) -> Vec<(u64, CommandOutcome)> {
    client
        .journal(0)
        .unwrap()
        .iter()
        .filter_map(|entry| match &entry.event {
            JournalEvent::CommandSettled { receipt } if &receipt.command == command => {
                Some((entry.tick.0, receipt.outcome.clone()))
            }
            _ => None,
        })
        .collect()
}

/// The checkpoint a client serves with its model fingerprint
/// normalized out: the pair and reference models differ only in the
/// plant address, so the rest of the transferable state — tick,
/// component states, output and internal images, forces, receipts,
/// admission counters — must serialize identically.
fn checkpoint_digest(client: &MonitorClient) -> Vec<u8> {
    let mut checkpoint: Checkpoint = client.checkpoint().unwrap();
    checkpoint.model_fingerprint = None;
    serde_json::to_vec(&checkpoint).unwrap()
}

/// What one tick left on the pair's shared plant — the field the
/// run-to-run digest compares.
#[derive(Debug, Clone, PartialEq)]
struct FieldRow {
    /// The run tick the observation followed.
    tick: Tick,
    /// Raw level input `lt101_raw`.
    level_raw: Sample,
    /// Raw valve command output `lv101_raw`.
    valve_raw: Sample,
    /// Pump run feedback `p101_run`.
    run_feedback: Sample,
    /// Pump starter command `p101_cmd`.
    pump_command: Sample,
    /// Alarm horn `la101_horn`.
    horn: Sample,
}

/// The field state after the field owner's scan of `tick`.
fn observe(field: &RemoteDriver, tick: Tick) -> FieldRow {
    use dcs_core::IoDriver;
    FieldRow {
        tick,
        level_raw: field.read(points::LEVEL_RAW).unwrap(),
        valve_raw: field.read(points::VALVE_RAW).unwrap(),
        run_feedback: field.read(points::PUMP_RUN).unwrap(),
        pump_command: field.read(points::PUMP_COMMAND).unwrap(),
        horn: field.read(points::HORN).unwrap(),
    }
}

/// One scripted tick of the run: the quiesced peer scans first — pulling
/// the field owner's checkpoint while it tracks — then the owner and
/// the uninterrupted reference scan and step their plants. The three
/// snapshots must be equal and the field must carry the owner's write.
/// Returns the field owner's snapshot.
fn tick(
    quiesced: &MonitorClient,
    owner: &MonitorClient,
    reference: &MonitorClient,
    field: &RemoteDriver,
    trace: &mut Vec<FieldRow>,
) -> TelemetrySnapshot {
    let tracking = quiesced.advance(1).unwrap();
    let owner_image = owner.advance(1).unwrap();
    let alone = reference.advance(1).unwrap();
    let n = owner_image.tick;
    assert_eq!(
        tracking, owner_image,
        "quiesced peer diverged at tick {n:?}"
    );
    assert_eq!(
        owner_image, alone,
        "pair diverged from the no-consumer reference at tick {n:?}"
    );
    let row = observe(field, n);
    assert_eq!(
        row.valve_raw.value,
        image_sample(&owner_image, points::VALVE_RAW).value,
        "the field did not carry the field owner's write at tick {n:?}"
    );
    trace.push(row);
    owner_image
}

/// Runs `scans` scripted ticks; `consumers` selects the per-tick pause
/// a leg's consumer traffic overlaps. Returns the field owner's
/// snapshot at the leg's end.
fn phase(
    quiesced: &MonitorClient,
    owner: &MonitorClient,
    reference: &MonitorClient,
    field: &RemoteDriver,
    trace: &mut Vec<FieldRow>,
    scans: u64,
    consumers: bool,
) -> TelemetrySnapshot {
    let mut image = None;
    for _ in 0..scans {
        image = Some(tick(quiesced, owner, reference, field, trace));
        if consumers {
            thread::sleep(CONSUMER_IDLE);
        }
    }
    image.unwrap()
}

/// The leg-boundary proof a leg's consumer traffic never reached the
/// run: the pair view sources the field owner's snapshot, and the
/// owner's receipt log and checkpoint digest equal the no-consumer
/// reference's.
fn leg_boundary(
    name: &str,
    pair: &mut PairClient,
    owner: &MonitorClient,
    reference: &MonitorClient,
    stages: &mut Vec<TelemetrySnapshot>,
    checkpoints: &mut Vec<Vec<u8>>,
) {
    let view = pair.snapshot().unwrap();
    let own = owner.snapshot().unwrap();
    assert_eq!(
        view, own,
        "{name}: the pair view must source the field owner"
    );
    stages.push(view);
    let owner_checkpoint = checkpoint_digest(owner);
    assert_eq!(
        owner_checkpoint,
        checkpoint_digest(reference),
        "{name}: consumer traffic changed the checkpoint"
    );
    assert_eq!(
        owner.receipts().unwrap(),
        reference.receipts().unwrap(),
        "{name}: consumer traffic changed the receipt log"
    );
    checkpoints.push(owner_checkpoint);
}

/// The pair view's role poll: every peer must be reporting; returns the
/// reports in configured-peer order.
fn poll(pair: &mut PairClient) -> Vec<RoleReport> {
    pair.poll_roles();
    pair.peers()
        .iter()
        .map(|peer| match peer.status() {
            PeerStatus::Reporting(report) => report.clone(),
            status => panic!("peer {} is not reporting: {status:?}", peer.addr()),
        })
        .collect()
}

/// Submits `command` through the pair view — routed to the settled
/// active — and to the reference, both accepted for `apply_tick`.
fn submit(pair: &mut PairClient, reference: &MonitorClient, command: &Command, apply_tick: Tick) {
    assert_eq!(
        pair.command(command).unwrap().outcome,
        CommandOutcome::Accepted { apply_tick },
        "the pair view routed {command:?} to the settled active"
    );
    assert_eq!(
        reference.command(command).unwrap().outcome,
        CommandOutcome::Accepted { apply_tick }
    );
}

// --------------------------------------------------------------------
// The consumer schedule — the thread behaviors the legs overlay on the
// pair's monitors, after `dcs-monitor`'s non-interference matrix and
// `reference-plant/ci/consumers.py`.
// --------------------------------------------------------------------

/// What the consumers observed — the evidence that a leg's
/// interference was real rather than vacuous.
#[derive(Default)]
struct ConsumerLog {
    /// Every HTTP status a consumer read back.
    statuses: Vec<u16>,
    /// Transport failures — connections refused or dropped before an
    /// answer. No monitor restarts in this run: none are expected.
    errors: Vec<String>,
    /// Raw probes pushed at the socket level — garbage bytes and
    /// half-sent requests that never became a request.
    probes: u64,
    /// The stalled reader's held response, read back at leg end.
    stalled: Option<(u16, String)>,
}

/// The read surfaces a polling consumer cycles — every read endpoint
/// the contract serves, `/schema` and `/resources` inside the matrix
/// per decision 83.
const READ_SURFACES: &[&str] = &[
    "/snapshot",
    "/receipts",
    "/history?point=60&since=0",
    "/journal?since=0",
    "/schema",
    "/resources",
    "/checkpoint",
    "/role",
    "/signals",
    "/",
];

fn record(log: &Mutex<ConsumerLog>, result: std::io::Result<(u16, String)>) {
    let mut log = log.lock().unwrap();
    match result {
        Ok((status, _)) => log.statuses.push(status),
        Err(error) => log.errors.push(error.to_string()),
    }
}

/// A normally polling reader: one fresh request per read surface,
/// round-robin across `targets`, until the leg ends.
fn polling(
    targets: &[SocketAddr],
    stop: &AtomicBool,
    started: &AtomicUsize,
    log: &Mutex<ConsumerLog>,
) {
    let mut index = 0;
    let mut contacted = false;
    while !stop.load(Ordering::Relaxed) {
        let client = MonitorClient::new(targets[index % targets.len()]);
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

/// A reader that issues `GET /snapshot` and then holds the connection
/// without reading a byte of the response until the leg ends — the
/// held-response case the publication split exists for.
fn stalled(
    targets: &[SocketAddr],
    stop: &AtomicBool,
    started: &AtomicUsize,
    log: &Mutex<ConsumerLog>,
) {
    let mut stream = match TcpStream::connect(targets[0]) {
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
fn churn(
    targets: &[SocketAddr],
    stop: &AtomicBool,
    started: &AtomicUsize,
    log: &Mutex<ConsumerLog>,
) {
    let mut index = 0;
    let mut contacted = false;
    while !stop.load(Ordering::Relaxed) {
        if !contacted {
            contacted = true;
            started.fetch_add(1, Ordering::Relaxed);
        }
        let target = targets[index % targets.len()];
        let path = READ_SURFACES[index % READ_SURFACES.len()];
        index += 1;
        match TcpStream::connect(target) {
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

/// Malformed traffic and read flooding within the declared bounds:
/// garbage bytes, unparsable bodies, bad queries, refused verbs —
/// none of it may reach authoritative state — interleaved with a hot
/// read loop over every surface. `targets` names the field owner's
/// monitor only: `POST /promote` lands the named `already_active`
/// refusal there, while on a tracking standby it would be a real
/// switch request — and no well-formed `POST /scan` appears, the
/// driven monitor's drive channel staying the driver's alone.
fn flood(
    targets: &[SocketAddr],
    stop: &AtomicBool,
    started: &AtomicUsize,
    log: &Mutex<ConsumerLog>,
) {
    /// `(method, path, body)` — every entry either fails to parse or is
    /// refused before touching the run.
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
        ("POST", "/promote", None),
    ];
    let mut contacted = false;
    while !stop.load(Ordering::Relaxed) {
        let addr = targets[0];
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

/// The consumer behavior a leg overlays on the pair's monitors.
#[derive(Clone, Copy)]
enum Interference {
    /// A reader polling every read surface in a loop.
    Polling,
    /// A reader holding `GET /snapshot` unanswered-read until leg end.
    Stalled,
    /// Connect, one read, disconnect — repeated, sometimes dropped
    /// mid-response.
    Churn,
    /// Malformed requests of every kind plus a read flood.
    Flood,
}

impl Interference {
    /// How many threads the behavior spawns — the count the leg waits
    /// to see make contact before its ticks start, so the interference
    /// provably overlapped them.
    fn threads(self) -> usize {
        match self {
            Interference::Polling | Interference::Stalled => 1,
            Interference::Churn | Interference::Flood => 2,
        }
    }

    /// The thread body.
    fn body(self) -> fn(&[SocketAddr], &AtomicBool, &AtomicUsize, &Mutex<ConsumerLog>) {
        match self {
            Interference::Polling => polling,
            Interference::Stalled => stalled,
            Interference::Churn => churn,
            Interference::Flood => flood,
        }
    }
}

/// One leg's running consumer threads, their shared stop and contact
/// counters, and the observation log.
struct Consumers {
    stop: Arc<AtomicBool>,
    log: Arc<Mutex<ConsumerLog>>,
    handles: Vec<JoinHandle<()>>,
}

impl Consumers {
    /// Spawns `interference`'s threads against `targets` and waits
    /// until each has made contact — the leg's interference provably
    /// overlaps its ticks.
    fn start(interference: Interference, targets: &[SocketAddr]) -> Self {
        let stop = Arc::new(AtomicBool::new(false));
        let started = Arc::new(AtomicUsize::new(0));
        let log = Arc::new(Mutex::new(ConsumerLog::default()));
        let targets = Arc::new(targets.to_vec());
        let body = interference.body();
        let handles = (0..interference.threads())
            .map(|_| {
                let (targets, stop, started, log) =
                    (targets.clone(), stop.clone(), started.clone(), log.clone());
                thread::spawn(move || body(&targets, &stop, &started, &log))
            })
            .collect::<Vec<_>>();
        let deadline = Instant::now();
        while started.load(Ordering::Relaxed) < handles.len() {
            assert!(
                deadline.elapsed() < Duration::from_secs(10),
                "consumers never made contact"
            );
            thread::yield_now();
        }
        Self { stop, log, handles }
    }

    /// Stops the leg's threads and asserts the interference actually
    /// happened and saw the right surface — a leg whose consumers never
    /// ran would prove nothing. No monitor ever restarts in this run,
    /// so no consumer may meet a server fault or a refused connection.
    fn finish(self, name: &str, interference: Interference) {
        self.stop.store(true, Ordering::Relaxed);
        for handle in self.handles {
            handle.join().unwrap();
        }
        let log = self.log.lock().unwrap();
        match interference {
            Interference::Stalled => {
                let (status, body) = log
                    .stalled
                    .as_ref()
                    .expect("the stalled reader's held response");
                assert_eq!(*status, 200, "{name}: {body}");
                assert!(body.contains("\"tick\""), "{name}: {body}");
            }
            Interference::Flood => {
                assert!(log.probes > 0, "{name}: no raw probes reached the socket");
                assert!(
                    log.statuses
                        .iter()
                        .any(|&status| (400..500).contains(&status)),
                    "{name}: malformed traffic was never refused with a 4xx"
                );
            }
            Interference::Polling | Interference::Churn => {
                assert!(!log.statuses.is_empty(), "{name}: consumers never ran");
            }
        }
        assert!(
            log.statuses.iter().all(|&status| status < 500),
            "{name}: a consumer saw a server fault: {:?}",
            log.statuses
        );
        assert!(
            log.errors.is_empty(),
            "{name}: consumer transport errors: {:?}",
            log.errors
        );
    }
}

// --------------------------------------------------------------------
// The disposable UI consumer process — a `python3` polling loop in
// `reference-plant/ci/consumers.py`'s `--ui-client` shape: `signals`
// once, then `snapshot`/`journal`/`history` per pass the way the
// served page polls, each pass's observation — polls, faults, tick,
// and the publication counters — recorded at a seen file the driver
// compares across the mid-leg restart.
// --------------------------------------------------------------------

/// The poll loop the disposable UI process runs.
const UI_CLIENT_SCRIPT: &str = r#"
import json, os, sys, time, urllib.request
monitor, seen, point = sys.argv[1], sys.argv[2], sys.argv[3]
tmp = seen + ".tmp"
polls = faults = 0
since = 0
def fetch(path):
    return json.load(urllib.request.urlopen(monitor + path, timeout=10))
while True:
    try:
        if polls == 0:
            fetch("/signals")
        snap = fetch("/snapshot")
        journal = fetch("/journal?since=%d" % since)
        fetch("/history?point=%s&since=0" % point)
    except Exception:
        faults += 1
        time.sleep(0.05)
        continue
    polls += 1
    if journal:
        since = journal[-1]["seq"]
    pub = snap.get("publication") or {}
    with open(tmp, "w") as handle:
        json.dump({"polls": polls, "faults": faults, "tick": snap["tick"],
                   "published": pub.get("published"),
                   "coalesced": pub.get("coalesced")}, handle)
    os.replace(tmp, seen)
"#;

/// A running UI consumer process, killed on drop so a panicking test
/// leaves none behind.
struct UiProcess {
    child: Child,
}

impl Drop for UiProcess {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// Spawns the disposable UI consumer against `monitor`, recording each
/// poll's observation at `seen`.
fn spawn_ui_process(monitor: SocketAddr, seen: &Path) -> UiProcess {
    let child = Process::new("python3")
        .arg("-c")
        .arg(UI_CLIENT_SCRIPT)
        .arg(format!("http://{monitor}"))
        .arg(seen)
        .arg(points::LEVEL_PERCENT.0.to_string())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("cannot spawn the UI consumer process");
    UiProcess { child }
}

/// The UI process's last recorded observation, or `None` while it has
/// not written one.
fn read_ui_seen(path: &Path) -> Option<serde_json::Value> {
    serde_json::from_str(&std::fs::read_to_string(path).ok()?).ok()
}

/// Waits until the UI process's seen file records its first completed
/// poll — the spawned process's first-contact signal.
fn wait_ui_poll(path: &Path) {
    let deadline = Instant::now();
    while read_ui_seen(path)
        .and_then(|seen| seen["polls"].as_u64())
        .unwrap_or(0)
        == 0
    {
        assert!(
            deadline.elapsed() < Duration::from_secs(10),
            "the UI process never completed a poll"
        );
        thread::sleep(Duration::from_millis(10));
    }
}

// --------------------------------------------------------------------
// The decision-82 sweep: the served registry, the live resource view,
// and the generic page.
// --------------------------------------------------------------------

/// The served-registry sweep: `GET /schema` covers every instantiated
/// kind once per instance — each served interface at the declared
/// version, equal to the descriptor's own
/// [`BlockInterface::from_descriptor`] derivation plus the serving
/// layer's bound-point annotations and the bound point's signal `unit`.
fn assert_schema(client: &MonitorClient, snapshot: &TelemetrySnapshot) {
    let schema = client.schema().unwrap();
    assert_eq!(schema.tick, snapshot.tick);
    assert_eq!(
        schema.publication,
        snapshot.publication.unwrap().published,
        "the schema stamps the publication it was derived from"
    );
    assert_eq!(
        schema.interfaces.len(),
        snapshot.descriptors.len(),
        "every instantiated component serves an interface"
    );
    let signals = client.signals().unwrap();
    for (entry, descriptor) in schema.interfaces.iter().zip(&snapshot.descriptors) {
        assert_eq!(entry.name, descriptor.name);
        assert_eq!(entry.interface.kind, descriptor.kind);
        assert_eq!(entry.interface.version, INTERFACE_VERSION);
        let mut expected = BlockInterface::from_descriptor(descriptor);
        for (served, want) in entry
            .interface
            .measurements
            .iter()
            .zip(expected.measurements.iter_mut())
        {
            let unit = served
                .point
                .and_then(|point| signals.get(point))
                .and_then(|signal| signal.unit.clone());
            assert_eq!(
                served.unit, unit,
                "{}.{}: the served unit is not the bound point's signal unit",
                entry.name, served.name
            );
            want.unit = unit;
        }
        assert_eq!(
            entry.interface, expected,
            "{}: the served interface is not the descriptor's derivation",
            entry.name
        );
    }
}

/// The live-resource sweep: `GET /resources` joins the same
/// publication, and every per-instance live collection is parallel to
/// its interface's — measurement and state readings equal to the
/// snapshot's samples, configuration equal to the snapshot's
/// parameters, unavailable commands always carrying the named refusal
/// the generic command table renders.
fn assert_resources(client: &MonitorClient, snapshot: &TelemetrySnapshot) {
    let resources = client.resources().unwrap();
    assert_eq!(resources.tick, snapshot.tick);
    assert_eq!(
        resources.publication,
        snapshot.publication.unwrap().published
    );
    assert_eq!(resources.components.len(), snapshot.descriptors.len());
    for (live, descriptor) in resources.components.iter().zip(&snapshot.descriptors) {
        assert_eq!(live.name, descriptor.name);
        assert_eq!(live.kind, descriptor.kind);
        for reading in live.measurements.iter().chain(&live.state) {
            match reading.point {
                Some(point) => {
                    let telemetry = snapshot
                        .points
                        .iter()
                        .find(|entry| entry.point == point)
                        .unwrap_or_else(|| panic!("{point:?} is not in the snapshot"));
                    assert_eq!(
                        reading.sample, telemetry.sample,
                        "{}.{}: the live reading is not the snapshot's",
                        live.name, reading.name
                    );
                }
                None => assert!(
                    reading.sample.is_none(),
                    "{}.{}: an unbound resource reports a sample",
                    live.name,
                    reading.name
                ),
            }
        }
        let parameters = snapshot
            .parameters
            .iter()
            .find(|entry| entry.name == live.name)
            .unwrap_or_else(|| panic!("{} has no parameters section", live.name));
        for config in &live.configuration {
            assert_eq!(
                config.value,
                parameters.values.get(&config.name).copied(),
                "{}.{}: the live configuration is not the snapshot's",
                live.name,
                config.name
            );
        }
        for state in &live.commands {
            assert!(
                state.available || state.refusal.is_some(),
                "{}.{} reports unavailable without a named refusal",
                live.name,
                state.name
            );
        }
    }
}

/// The generic-page sweep: the served page fetches `/schema` and
/// `/resources` and renders every instance through the five-category
/// disclosure — the wire documents carrying all five collections on
/// every entry, so no kind needs dedicated markup to be operable.
fn assert_generic_page(client: &MonitorClient) {
    let page = client.page().unwrap();
    for needle in [
        "fetch(base + \"/schema\")",
        "fetch(base + \"/resources\")",
        "interfaceMarkup(descriptor.name, generic)",
        "interfaceOpen.get(name) : generic",
        "resourceTable(\"measurements\", iface.measurements",
        "configTable(name, iface.configuration",
        "resourceTable(\"state\", iface.state",
        "commandTable(name, iface.commands",
        "eventList(resources && resources.events)",
        "data-category=\\\"configuration\\\"",
        "data-category=\\\"commands\\\"",
        "data-category=\\\"events\\\"",
        "class=\\\"invoke\\\"",
        "{ invoke: {",
        "state.available",
        "state.refusal",
    ] {
        assert!(page.contains(needle), "page lacks {needle}");
    }
    for (path, key) in [("/schema", "interfaces"), ("/resources", "components")] {
        let (status, body) = client.request("GET", path, None).unwrap();
        assert_eq!(status, 200, "{body}");
        let document: serde_json::Value = serde_json::from_str(&body).unwrap();
        for served in document[key].as_array().unwrap() {
            let carrier = if key == "interfaces" {
                &served["interface"]
            } else {
                served
            };
            for category in [
                "measurements",
                "configuration",
                "state",
                "commands",
                "events",
            ] {
                assert!(
                    carrier.get(category).is_some(),
                    "{path} entry lacks {category:?}: {served}"
                );
            }
        }
    }
}

/// No component stepped in error anywhere in the snapshot.
fn assert_clean(snapshot: &TelemetrySnapshot) {
    assert!(
        snapshot
            .components
            .iter()
            .all(|component| component.step_errors == 0),
        "every component stepped clean: {:?}",
        snapshot.components
    );
}

/// What one scripted run produces — the outcome a repeated run must
/// reproduce identically: the per-tick field trace, the pair-view
/// snapshot and field-owner checkpoint digest at every leg boundary,
/// the final receipt log, the promoted peer's emitted-event stream,
/// and every peer's served journal plus the durable journal files'
/// entries.
#[derive(Debug, PartialEq)]
struct Outcome {
    /// The pair plant's observed field state after each owner tick.
    field: Vec<FieldRow>,
    /// The pair view's snapshot at each leg boundary, in leg order.
    stages: Vec<TelemetrySnapshot>,
    /// The field owner's fingerprint-normalized checkpoint bytes at
    /// each leg boundary — each asserted equal to the no-consumer
    /// reference's at capture.
    checkpoints: Vec<Vec<u8>>,
    /// The promoted peer's receipt log at the run's end — asserted
    /// equal to the reference's.
    receipts: Vec<CommandReceipt>,
    /// The promoted peer's emitted-event stream — asserted equal to
    /// the reference's, the monotonic-continuation proof.
    emitted: Vec<(u64, EmittedEvent)>,
    /// Each controller's served journal at run end, then each durable
    /// journal file's entries: active, standby, reference, then the
    /// three files in the same order.
    journals: Vec<Vec<JournalEntry>>,
}

/// One scripted run of the verification documented in the module
/// header. Returns the run's [`Outcome`].
fn run_verification(tag: &str) -> Outcome {
    let dir =
        std::env::temp_dir().join(format!("dcs-schema-consumer-{}-{tag}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();

    // Two shared plants: the pair's and the no-consumer reference
    // run's — identical model and dynamics, identical request
    // sequences, identical runs.
    let pair_plant = spawn_plant();
    let reference_plant = spawn_plant();
    let pair_model = controller_model(&dir, "pair.json", pair_plant.addr);
    let reference_model = controller_model(&dir, "reference.json", reference_plant.addr);

    let active_journal = dir.join("active.journal.jsonl");
    let standby_journal = dir.join("standby.journal.jsonl");
    let reference_journal = dir.join("reference.journal.jsonl");
    let active_process = spawn_controller(
        &pair_model,
        &[
            "--journal-file".to_string(),
            active_journal.to_str().unwrap().to_string(),
        ],
    );
    let standby_process = spawn_controller(
        &pair_model,
        &[
            "--standby".to_string(),
            active_process.addr.to_string(),
            "--journal-file".to_string(),
            standby_journal.to_str().unwrap().to_string(),
        ],
    );
    let reference_process = spawn_controller(
        &reference_model,
        &[
            "--journal-file".to_string(),
            reference_journal.to_str().unwrap().to_string(),
        ],
    );
    let active = MonitorClient::new(active_process.addr);
    let standby = MonitorClient::new(standby_process.addr);
    let reference = MonitorClient::new(reference_process.addr);

    // An observer client on the pair's plant — the field's own view of
    // what each tick carried.
    let field = RemoteDriver::connect(pair_plant.addr).unwrap();

    // The pair view over both monitor surfaces.
    let mut pair = PairClient::new([active_process.addr, standby_process.addr]);

    assert_eq!(active.role().unwrap().role, Role::Active);
    let report = standby.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert_eq!(report.sync, Some(StandbySync::Unsynchronized));
    assert_eq!(reference.role().unwrap().role, Role::Active);
    let reports = poll(&mut pair);
    assert_eq!(reports[0].role, Role::Active);
    assert_eq!(reports[1].role, Role::Standby);
    assert_eq!(pair.source(), Some(active_process.addr));

    let mut trace = Vec::new();
    let mut stages = Vec::new();
    let mut checkpoints = Vec::new();

    // --- Leg 1: no-ui — no consumers attached ---
    let image = phase(
        &standby,
        &active,
        &reference,
        &field,
        &mut trace,
        NO_UI_TICKS,
        false,
    );
    leg_boundary(
        "no-ui",
        &mut pair,
        &active,
        &reference,
        &mut stages,
        &mut checkpoints,
    );
    assert_clean(&image);

    // The decision-82 sweep at the no-consumer baseline: the served
    // registry covers every instantiated kind with all five
    // collections; the tracking peer serves the identical registry —
    // the pair's one logical controller; and the live resource values
    // equal the snapshot's.
    assert_schema(&active, &image);
    assert_eq!(
        active.schema().unwrap().interfaces,
        standby.schema().unwrap().interfaces,
        "the tracking peer serves the pair's identical registry"
    );
    assert_resources(&active, &image);
    assert_generic_page(&active);

    // The batch program is the composition's declared-contract kind:
    // `advance`/`reset` under the `Declared` provenance and the
    // kind-emitted `step_completed` beside the adapted entries.
    let schema = active.schema().unwrap();
    let batch = &schema
        .interfaces
        .iter()
        .find(|entry| entry.name == BATCH_COMPONENT)
        .expect("the served schema covers the batch program")
        .interface;
    for (command, availability) in [
        ("advance", CommandAvailability::KindDeclared),
        ("reset", CommandAvailability::Always),
    ] {
        let spec = batch
            .commands
            .iter()
            .find(|spec| spec.name == command)
            .unwrap_or_else(|| panic!("the schema declares {command}"));
        assert_eq!(spec.adapted, AdaptedCommand::Declared, "{command}");
        assert_eq!(spec.availability, availability, "{command}");
    }
    let declared = batch
        .events
        .iter()
        .find(|spec| spec.name == "step_completed")
        .expect("the schema declares step_completed");
    assert_eq!(declared.adapted, AdaptedEvent::Declared);
    assert_eq!(declared.emission, EventEmission::KindEmitted);
    assert_eq!(declared.retention, EventRetention::Journal);

    // --- Leg 2: polling — a declared command settles Applied ---
    // `advance {count: 9}` lands past the table's last step: admitted
    // through the bounded receipted path, applied at the boundary, the
    // table completes.
    let advance = invoke("advance", &[("count", Value::Int(9))]);
    submit(&mut pair, &reference, &advance, Tick(ADVANCE_TICK));
    let consumers = Consumers::start(
        Interference::Polling,
        &[active_process.addr, standby_process.addr],
    );
    phase(
        &standby,
        &active,
        &reference,
        &field,
        &mut trace,
        POLLING_TICKS,
        true,
    );
    consumers.finish("polling", Interference::Polling);
    leg_boundary(
        "polling",
        &mut pair,
        &active,
        &reference,
        &mut stages,
        &mut checkpoints,
    );
    for client in [&active, &standby, &reference] {
        assert_eq!(
            settlements_of(client, &advance),
            vec![(
                ADVANCE_TICK,
                CommandOutcome::Applied {
                    tick: Tick(ADVANCE_TICK)
                }
            )],
            "the completing advance settled once, applied at its boundary"
        );
    }

    // --- Leg 3: stalled-reader — the declared-unavailable refusal ---
    // The table is complete: `advance` is `KindDeclared`-unavailable
    // and settles Rejected carrying the kind's named reason, while a
    // reader holds its snapshot request unanswered through the leg.
    let unavailable = invoke("advance", &[("count", Value::Int(1))]);
    submit(&mut pair, &reference, &unavailable, Tick(REFUSED_TICK));
    let consumers = Consumers::start(Interference::Stalled, &[standby_process.addr]);
    phase(
        &standby,
        &active,
        &reference,
        &field,
        &mut trace,
        STALLED_TICKS,
        true,
    );
    consumers.finish("stalled-reader", Interference::Stalled);
    leg_boundary(
        "stalled-reader",
        &mut pair,
        &active,
        &reference,
        &mut stages,
        &mut checkpoints,
    );
    let refused = CommandOutcome::Rejected {
        reason: CommandError::CommandRefused {
            component: BATCH_COMPONENT.to_string(),
            command: "advance".to_string(),
            reason: "the sequence has run to its end; reset restarts it".to_string(),
        },
    };
    for client in [&active, &standby, &reference] {
        assert_eq!(
            settlements_of(client, &unavailable),
            vec![(REFUSED_TICK, refused.clone())],
            "the declared-unavailable advance settled once, refused by name"
        );
    }

    // --- Leg 4: disconnect-reconnect — reset, then the run starts ---
    let reset = invoke("reset", &[]);
    submit(&mut pair, &reference, &reset, Tick(RESET_TICK));
    let consumers = Consumers::start(
        Interference::Churn,
        &[active_process.addr, standby_process.addr],
    );
    phase(
        &standby,
        &active,
        &reference,
        &field,
        &mut trace,
        CHURN_TICKS - 1,
        true,
    );
    // The held `run` write applies on the leg's last tick — the batch
    // program starts stepping under the churn.
    submit(&mut pair, &reference, &batch_run(true), Tick(RUN_TICK));
    phase(&standby, &active, &reference, &field, &mut trace, 1, true);
    consumers.finish("disconnect-reconnect", Interference::Churn);
    leg_boundary(
        "disconnect-reconnect",
        &mut pair,
        &active,
        &reference,
        &mut stages,
        &mut checkpoints,
    );
    for client in [&active, &standby, &reference] {
        assert_eq!(
            settlements_of(client, &reset),
            vec![(
                RESET_TICK,
                CommandOutcome::Applied {
                    tick: Tick(RESET_TICK)
                }
            )],
        );
    }

    // --- Leg 5: malformed-and-flood — bounded admission under load ---
    // The flood's refused verbs and parse failures run against the
    // field owner while the script floods the admission queue itself:
    // `DEFAULT_COMMAND_QUEUE_CAPACITY` held-`run` writes queue, the
    // next two meet the named `queue_full` rejection, and a statically
    // invalid write still takes its `not_writable` validation refusal —
    // validation precedes admission.
    let consumers = Consumers::start(Interference::Flood, &[active_process.addr]);
    for client in [&active, &reference] {
        for _ in 0..DEFAULT_COMMAND_QUEUE_CAPACITY {
            assert!(
                matches!(
                    client.command(&batch_run(true)).unwrap().outcome,
                    CommandOutcome::Accepted {
                        apply_tick: Tick(DRAIN_TICK)
                    }
                ),
                "the queued flood write must admit"
            );
        }
        for _ in 0..2 {
            assert_eq!(
                client.command(&batch_run(true)).unwrap().outcome,
                CommandOutcome::Rejected {
                    reason: CommandError::QueueFull {
                        point: Some(points::BATCH_RUN),
                        capacity: DEFAULT_COMMAND_QUEUE_CAPACITY,
                    }
                },
                "the queue must refuse by name at its bound"
            );
        }
        assert_eq!(
            client
                .command(&Command::WriteValue {
                    point: points::LEVEL_FOR_PID,
                    kind: ValueKind::Float,
                    value: Value::Float(1.0),
                })
                .unwrap()
                .outcome,
            CommandOutcome::Rejected {
                reason: CommandError::NotWritable {
                    point: points::LEVEL_FOR_PID,
                }
            },
            "an invalid write still meets validation, full queue or not"
        );
    }
    phase(
        &standby,
        &active,
        &reference,
        &field,
        &mut trace,
        FLOOD_TICKS,
        true,
    );
    consumers.finish("malformed-and-flood", Interference::Flood);
    leg_boundary(
        "malformed-and-flood",
        &mut pair,
        &active,
        &reference,
        &mut stages,
        &mut checkpoints,
    );
    // All 64 admitted writes settled Applied at the drain boundary —
    // the earlier admissions were not lost to the full-queue
    // rejections — and the admission counters report the flood.
    for client in [&active, &standby, &reference] {
        let settlements = settlements_of(client, &batch_run(true));
        let applied: Vec<_> = settlements
            .iter()
            .filter(|(_, outcome)| matches!(outcome, CommandOutcome::Applied { .. }))
            .collect();
        assert_eq!(
            applied.len(),
            1 + DEFAULT_COMMAND_QUEUE_CAPACITY,
            "every admitted flood write settled applied"
        );
        assert!(
            applied
                .iter()
                .all(|(tick, _)| *tick == RUN_TICK || *tick == DRAIN_TICK),
            "flood writes settled at their apply boundaries: {settlements:?}"
        );
        let full = settlements
            .iter()
            .filter(|(_, outcome)| {
                matches!(
                    outcome,
                    CommandOutcome::Rejected {
                        reason: CommandError::QueueFull { .. }
                    }
                )
            })
            .count();
        assert_eq!(full, 2, "both over-bound submissions journaled queue_full");
    }
    let queue = active.snapshot().unwrap().command_queue;
    assert_eq!(queue.capacity, DEFAULT_COMMAND_QUEUE_CAPACITY);
    assert_eq!(queue.full_rejections, 2);
    assert_eq!(queue.high_water, DEFAULT_COMMAND_QUEUE_CAPACITY);

    // --- Leg 6: ui-restart — the disposable UI consumer process ---
    // A `python3` polling process in the `--ui-client` shape runs
    // against the field owner, is killed at the leg's midpoint, and
    // respawns; the first `step_completed` lands at the leg's last
    // tick on every journal while it does.
    let ui_seen_a = dir.join("ui-a-seen.json");
    let ui_seen_b = dir.join("ui-b-seen.json");
    let mut ui = spawn_ui_process(active_process.addr, &ui_seen_a);
    wait_ui_poll(&ui_seen_a);
    phase(
        &standby,
        &active,
        &reference,
        &field,
        &mut trace,
        UI_RESTART_TICKS / 2,
        true,
    );
    let _ = ui.child.kill();
    let _ = ui.child.wait();
    let before = read_ui_seen(&ui_seen_a).expect("the UI process never polled before its restart");
    ui = spawn_ui_process(active_process.addr, &ui_seen_b);
    wait_ui_poll(&ui_seen_b);
    phase(
        &standby,
        &active,
        &reference,
        &field,
        &mut trace,
        UI_RESTART_TICKS / 2,
        true,
    );
    let after = read_ui_seen(&ui_seen_b).expect("the restarted UI process never rejoined");
    drop(ui);
    // The restart's evidence — the `ui_evidence_failures` shape:
    // neither incarnation met a fault, the restarted process read the
    // freshness metadata moved past what the first saw, and the
    // missed stretch reported the bounded window's named coalescing.
    assert_eq!(
        before["faults"].as_u64().unwrap_or(1),
        0,
        "the UI process met faults before its restart: {before}"
    );
    assert_eq!(
        after["faults"].as_u64().unwrap_or(1),
        0,
        "the restarted UI process met faults: {after}"
    );
    assert!(
        after["published"].as_u64() > before["published"].as_u64(),
        "the restarted UI saw no freshness advance: {before} -> {after}"
    );
    assert!(
        after["coalesced"].as_u64().unwrap_or(0) > 0,
        "the missed stretch never reported coalescing: {after}"
    );
    leg_boundary(
        "ui-restart",
        &mut pair,
        &active,
        &reference,
        &mut stages,
        &mut checkpoints,
    );

    // The kind-emitted event landed at the producing tick on every
    // controller's journal — the tracking peer's included — and in the
    // durable journal files; the served recent-events view attributes
    // it to the instance.
    let event_tick = Tick(EVENT_TICK);
    assert_eq!(reference.snapshot().unwrap().tick, event_tick);
    for (peer, journal) in [
        ("the field owner", active.journal(0).unwrap()),
        ("the tracking peer", standby.journal(0).unwrap()),
        ("the reference", reference.journal(0).unwrap()),
    ] {
        assert!(
            journal
                .iter()
                .any(|entry| entry.tick == event_tick && entry.event == step_completed(1)),
            "{peer} journaled the emission at the producing tick"
        );
    }
    for path in [&active_journal, &standby_journal, &reference_journal] {
        assert!(
            read_journal_file(path)
                .unwrap()
                .entries
                .iter()
                .any(|entry| entry.tick == event_tick && entry.event == step_completed(1)),
            "the durable journal {} carries the emission",
            path.display()
        );
    }
    let resources = active.resources().unwrap();
    let batch_resources = resources
        .components
        .iter()
        .find(|component| component.name == BATCH_COMPONENT)
        .expect("the resource view covers the batch program");
    assert!(
        batch_resources
            .events
            .iter()
            .any(|entry| entry.tick == event_tick && entry.event == step_completed(1)),
        "the served events tail carries the emission at the producing tick"
    );

    // --- Promotion mid-schedule: an admitted command in flight ---
    // `reset` is admitted on the pair's active and the reference and
    // stays `Accepted` through the switchover requests — the promote
    // boundary's final checkpoint pull is its carrier onto the standby.
    let carried = invoke("reset", &[]);
    submit(&mut pair, &reference, &carried, Tick(CARRIED_TICK));
    assert_eq!(active.demote().unwrap().role, Role::Demoting);
    assert_eq!(standby.promote().unwrap().role, Role::Promoting);
    let reports = poll(&mut pair);
    assert_eq!(reports[0].role, Role::Demoting);
    assert_eq!(reports[1].role, Role::Promoting);
    assert_eq!(pair.source(), Some(standby_process.addr));

    // --- Leg 7: polling again — against the promoted peer ---
    // The carried invocation settles on all three at the first
    // post-switch boundary: the promoted peer's adopted queue, the
    // demoted peer's own, the reference's.
    let consumers = Consumers::start(
        Interference::Polling,
        &[standby_process.addr, active_process.addr],
    );
    phase(
        &active,
        &standby,
        &reference,
        &field,
        &mut trace,
        POST_SWITCH_TICKS,
        true,
    );
    consumers.finish("polling-post-switch", Interference::Polling);
    leg_boundary(
        "polling-post-switch",
        &mut pair,
        &standby,
        &reference,
        &mut stages,
        &mut checkpoints,
    );
    for client in [&standby, &active, &reference] {
        assert_eq!(
            settlements_of(client, &carried),
            vec![
                (
                    RESET_TICK,
                    CommandOutcome::Applied {
                        tick: Tick(RESET_TICK)
                    }
                ),
                (
                    CARRIED_TICK,
                    CommandOutcome::Applied {
                        tick: Tick(CARRIED_TICK)
                    }
                ),
            ],
            "each admitted reset settled exactly once — the carried one at the switch"
        );
    }
    assert_eq!(standby.role().unwrap().role, Role::Active);
    assert_eq!(active.role().unwrap().role, Role::Standby);

    // --- Leg 8: no-ui close — the second emission, post-switch ---
    let closed = phase(
        &active,
        &standby,
        &reference,
        &field,
        &mut trace,
        CLOSE_TICKS,
        false,
    );
    leg_boundary(
        "no-ui-close",
        &mut pair,
        &standby,
        &reference,
        &mut stages,
        &mut checkpoints,
    );
    assert_clean(&closed);
    assert_eq!(closed.tick, Tick(END_TICK));

    // The pinned record: the promoted peer's emitted-event stream is
    // the uninterrupted reference's — `step_completed` at both
    // producing ticks, the sequence continuing monotonically across
    // the switch — its receipt log is identical, and every admitted
    // command settled exactly once: one journaled settlement per
    // receipt on every peer.
    assert_eq!(emitted(&standby), emitted(&reference));
    let stream = emitted(&standby);
    assert_eq!(stream.len(), 2, "{stream:?}");
    assert_eq!(stream[0], (EVENT_TICK, emitted_event(1)));
    assert_eq!(stream[1], (SECOND_EVENT_TICK, emitted_event(1)));
    assert!(
        stream[0].0 < stream[1].0,
        "the emitted-event sequence continues monotonically across the promotion"
    );
    assert_eq!(standby.receipts().unwrap(), reference.receipts().unwrap());
    for client in [&active, &standby, &reference] {
        let journal = client.journal(0).unwrap();
        let settled = journal
            .iter()
            .filter(|entry| matches!(entry.event, JournalEvent::CommandSettled { .. }))
            .count();
        assert_eq!(
            settled,
            client.receipts().unwrap().len(),
            "every receipt settled into exactly one journaled entry"
        );
    }

    Outcome {
        field: trace,
        stages,
        checkpoints,
        receipts: standby.receipts().unwrap(),
        emitted: emitted(&standby),
        journals: [
            active.journal(0).unwrap(),
            standby.journal(0).unwrap(),
            reference.journal(0).unwrap(),
            read_journal_file(&active_journal).unwrap().entries,
            read_journal_file(&standby_journal).unwrap().entries,
            read_journal_file(&reference_journal).unwrap().entries,
        ]
        .into(),
    }
}

/// The emitted-event payload the stream assertions compare against.
fn emitted_event(step: i64) -> EmittedEvent {
    EmittedEvent {
        event: "step_completed".to_string(),
        component: BATCH_COMPONENT.to_string(),
        fields: [("step".to_string(), EventValue::Value(Value::Int(step)))]
            .into_iter()
            .collect(),
    }
}

/// The whole scripted verification runs twice; every asserted monitor
/// payload, the per-tick field trace, the leg-boundary checkpoints,
/// the receipts, the emitted-event streams, and the journals must be
/// identical across runs.
#[test]
fn schema_driven_blocks_and_disposable_ui_verify_end_to_end() {
    let first = run_verification("first");
    let second = run_verification("second");
    assert_eq!(first, second, "repeated runs produce identical outcomes");
}
