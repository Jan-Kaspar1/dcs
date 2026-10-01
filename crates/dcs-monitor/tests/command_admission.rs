//! Regression coverage for the `command-admission` finding: a command
//! flood pipelined past the served `command_queue` capacity on one
//! keep-alive connection landed faster than the submission lane's
//! queue could drain, and the overflow met the refuse worker's bare
//! `503` — an HTTP-layer fault where the bounded-ingress contract
//! owes every submission a receipted settlement or the named
//! `queue_full` rejection. `POST /command` now takes a dedicated
//! lane — one worker, so the receipt log appends in submission order,
//! and a queue deep enough to hold the contract's whole flood wave —
//! while an overflowed command still runs the real admission path
//! rather than answering a bare fault.

use dcs_core::{
    Command, CommandError, CommandOutcome, CommandReceipt, Direction, IoDriver, IoError, PointId,
    Sample, Tick, Value, ValueKind,
};
use dcs_model::{PlantModel, SignalIndex};
use dcs_monitor::{Monitor, MonitorClient};
use dcs_runtime::{
    Component, ComponentIo, ComponentIoExt, Executor, IoRequirement, PointMap, StepError,
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

/// One serving monitor on a dedicated thread — unpaced, so scans run
/// only when the test drives `POST /scan` and the flood lands entirely
/// between boundaries.
struct Rig {
    addr: SocketAddr,
    client: MonitorClient,
    monitor: Arc<Monitor<'static>>,
    thread: Option<thread::JoinHandle<()>>,
}

impl Rig {
    fn start(capacity: usize) -> Self {
        let driver: &'static StubDriver = Box::leak(Box::new(StubDriver::new(&[
            (PointId(10), Value::Float(0.0)),
            (PointId(20), Value::Float(0.0)),
            (PointId(30), Value::Float(0.0)),
        ])));
        let executor = Executor::new(driver, point_map(), vec![Box::new(Scale)])
            .unwrap()
            .with_command_queue_capacity(capacity);
        let monitor = Monitor::bind("127.0.0.1:0", executor, signal_index()).unwrap();
        let monitor = Arc::new(monitor);
        let addr = monitor.local_addr();
        let serving = Arc::clone(&monitor);
        Self {
            addr,
            client: MonitorClient::new(addr),
            monitor,
            thread: Some(thread::spawn(move || serving.serve())),
        }
    }
}

impl Drop for Rig {
    fn drop(&mut self) {
        self.monitor.shutdown();
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

/// A receipt's normalized verdict — the same vocabulary the QA leg's
/// audit compares: `accepted`, `applied`, or `rejected:<reason>`.
fn outcome_key(receipt: &CommandReceipt) -> String {
    match &receipt.outcome {
        CommandOutcome::Accepted { .. } => "accepted".to_string(),
        CommandOutcome::Applied { .. } => "applied".to_string(),
        CommandOutcome::Rejected { reason } => {
            let name = match reason {
                CommandError::QueueFull { .. } => "queue_full",
                CommandError::NotWritable { .. } => "not_writable",
                other => panic!("flood receipt refused unexpectedly: {other:?}"),
            };
            format!("rejected:{name}")
        }
    }
}

/// The QA flood channel: every body in `bodies` POSTed to `/command`
/// on one keep-alive connection, all sent back-to-back before the
/// first answer is read — the whole wave lands inside the server's
/// read buffer faster than a scan boundary can drain the pending
/// queue. Returns `(status, receipt)` per submission in submission
/// order; a missing or unparseable answer reads `(0, None)` — the
/// no-receipt case the admission contract forbids.
fn pipelined_commands(addr: SocketAddr, bodies: &[Vec<u8>]) -> Vec<(u16, Option<CommandReceipt>)> {
    let count = bodies.len();
    let mut request = Vec::new();
    for (index, body) in bodies.iter().enumerate() {
        request.extend_from_slice(
            b"POST /command HTTP/1.1\r\nHost: qa\r\nContent-Type: application/json\r\n\
              Content-Length: ",
        );
        request.extend_from_slice(body.len().to_string().as_bytes());
        request.extend_from_slice(b"\r\n");
        if index == count - 1 {
            request.extend_from_slice(b"Connection: close\r\n");
        }
        request.extend_from_slice(b"\r\n");
        request.extend_from_slice(body);
    }
    let mut stream = TcpStream::connect(addr).unwrap();
    stream.write_all(&request).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(15)))
        .unwrap();

    let mut raw = Vec::new();
    let mut replies: Vec<(u16, Option<CommandReceipt>)> = Vec::new();
    let deadline = std::time::Instant::now() + Duration::from_secs(15);
    let mut buf = [0u8; 65536];
    while replies.len() < count && std::time::Instant::now() < deadline {
        match stream.read(&mut buf) {
            Ok(0) | Err(_) => break,
            Ok(n) => raw.extend_from_slice(&buf[..n]),
        }
        // Split `raw` into as many complete HTTP responses as it
        // holds; a truncated or unframed answer stays for the next
        // chunk.
        while let Some(head_end) = raw.windows(4).position(|window| window == b"\r\n\r\n") {
            let head = String::from_utf8_lossy(&raw[..head_end]).to_string();
            let status = head
                .lines()
                .next()
                .and_then(|line| line.split_whitespace().nth(1))
                .and_then(|code| code.parse::<u16>().ok())
                .unwrap_or(0);
            let length = head.lines().find_map(|line| {
                line.split_once(':').and_then(|(name, value)| {
                    (name.trim().eq_ignore_ascii_case("content-length"))
                        .then(|| value.trim().parse::<usize>().ok())
                        .flatten()
                })
            });
            let Some(length) = length else { break };
            let frame = head_end + 4 + length;
            if raw.len() < frame {
                break;
            }
            let payload = raw[head_end + 4..frame].to_vec();
            raw.drain(..frame);
            replies.push((status, serde_json::from_slice(&payload).ok()));
        }
    }
    replies.resize(count, (0, None));
    replies
}

/// The reproduction: a flood of twice the served `command_queue`
/// capacity pipelined on one connection must answer every submission
/// with a receipt — `accepted` or the named `queue_full` rejection —
/// never an HTTP-layer fault, a silent drop, or a hang; the admitted
/// commands then settle `applied` at the next scan boundary, and the
/// receipt log's order matches the submission order the audit
/// correlates by.
#[test]
fn a_command_flood_past_the_admission_bound_is_receipted() {
    const CAPACITY: usize = 64;
    let rig = Rig::start(CAPACITY);
    let attempts0 = rig.client.snapshot().unwrap().command_queue.attempts;

    let body = serde_json::to_vec(&serde_json::json!({
        "command": {"write_value": {"point": 10, "kind": "float",
            "value": {"float": 1.0}}},
        "actor": "qa-lane",
    }))
    .unwrap();
    let replies = pipelined_commands(rig.addr, &vec![body; 2 * CAPACITY]);

    // Every submission answered 200 with a parseable receipt — the
    // reproduction's 503s and no-receipt entries are the defect.
    for (index, (status, receipt)) in replies.iter().enumerate() {
        assert_eq!(
            *status, 200,
            "submission {index} met an HTTP-layer error, not a receipt"
        );
        assert!(
            receipt.is_some(),
            "submission {index} answered without a receipt"
        );
    }
    let outcomes: Vec<String> = replies
        .iter()
        .map(|(_, receipt)| outcome_key(receipt.as_ref().unwrap()))
        .collect();
    let accepted = outcomes.iter().filter(|key| *key == "accepted").count();
    let queue_full = outcomes
        .iter()
        .filter(|key| *key == "rejected:queue_full")
        .count();
    assert_eq!(
        accepted + queue_full,
        2 * CAPACITY,
        "a flood receipt answered outside the admission vocabulary: {outcomes:?}"
    );
    // The unpaced run drains nothing between submissions: the queue
    // bound is met exactly once — the first `capacity` submissions
    // admit, the rest take the named rejection, in order.
    assert_eq!(accepted, CAPACITY, "the flood never filled the queue");
    assert_eq!(
        queue_full, CAPACITY,
        "the named queue_full rejection never appeared"
    );
    assert!(
        outcomes[..CAPACITY].iter().all(|key| key == "accepted"),
        "an early submission was refused while the queue had room"
    );
    assert!(
        outcomes[CAPACITY..]
            .iter()
            .all(|key| key == "rejected:queue_full"),
        "a submission past the bound was answered out of order"
    );

    // One logged receipt per submission, in submission order — the
    // high-water pairing the bounded-log audit correlates by. The
    // receipt mirror refreshes at each admission, so the log is
    // already live; the served snapshot's `command_queue` section is
    // published per scan and catches up at the boundary below.
    let receipts = rig.client.receipts().unwrap();
    assert_eq!(receipts.len(), 2 * CAPACITY);
    for (index, receipt) in receipts.iter().enumerate() {
        assert_eq!(
            outcome_key(receipt),
            outcomes[index],
            "receipt {index} is not the submission's own verdict — the \
             log fell out of submission order"
        );
    }

    // The admitted half settles applied at the next scan boundary, and
    // the served queue metrics now count the whole flood — every
    // submission an attempt, the over-bound half named rejections.
    let queue = rig.client.advance(1).unwrap().command_queue;
    assert_eq!(queue.attempts - attempts0, 2 * CAPACITY as u64);
    assert_eq!(queue.full_rejections, CAPACITY as u64);
    let receipts = rig.client.receipts().unwrap();
    for (index, receipt) in receipts.iter().enumerate() {
        let expected = if index < CAPACITY {
            "applied"
        } else {
            "rejected:queue_full"
        };
        assert_eq!(outcome_key(receipt), expected, "receipt {index}");
    }

    // The served surface stayed up throughout the flood — a read and a
    // fresh command answer normally once it drains.
    rig.client.snapshot().unwrap();
    let receipt = rig
        .client
        .command(&Command::WriteValue {
            point: PointId(10),
            kind: ValueKind::Float,
            value: Value::Float(2.0),
        })
        .unwrap();
    assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
}

/// The `command-lane-overflow-receipt-order-inversion` reproduction:
/// a flood landing while the lane's queue is full used to split the
/// receipted work across two workers — the overflowed submission ran
/// the real admission handler on the refuse worker, which raced the
/// command worker's drain on the shared lock and minted its receipt
/// ahead of earlier submissions still queued in the lane. Deferred
/// submissions now re-enter through the lane's own tail in dispatch
/// order, so the log never inverts, flooded lane or not.
///
/// The lane's queue is filled deterministically: the first submission
/// arrives with its body only partly sent, so the single command
/// worker blocks inside the body read while the rest of the wave —
/// more than the lane's `LANE_QUEUE_DEPTH + 2*capacity` bound — piles
/// into the lane and its overflow deck behind it. Completing the body
/// then unblocks the drain; every minted receipt must carry its own
/// submission's index.
#[test]
fn a_flood_past_the_lane_bound_keeps_receipts_in_submission_order() {
    const CAPACITY: usize = 64;
    // The reproduction's wave: `LANE_QUEUE_DEPTH` (64) plus twice the
    // served capacity (128) bounds the lane's queue at 192 — the
    // stalled submission plus these 240 land 49 deep in the overflow
    // the dual-consumer admission broke.
    const FLOOD: usize = 240;
    let rig = Rig::start(CAPACITY);

    let body_for = |index: usize| {
        serde_json::to_vec(&serde_json::json!({
            "command": {"write_value": {"point": 10, "kind": "float",
                "value": {"float": 1.0}}},
            "actor": format!("qa-lane-{index}"),
        }))
        .unwrap()
    };
    // Submission 0 rides its own connection and arrives with only the
    // first half of its body sent: the command worker pops it and
    // waits inside the body read, pinning the lane while the flood
    // piles in behind it. The padded reason pushes the declared
    // `Content-Length` past tiny_http's small eager buffer, so the
    // half-sent body stays on the socket — a body at or under that
    // buffer would be drained by the connection's reader before the
    // request ever reached a worker, pinning nothing.
    let stalled = serde_json::to_vec(&serde_json::json!({
        "command": {"write_value": {"point": 10, "kind": "float",
            "value": {"float": 1.0}}},
        "actor": "qa-lane-0",
        "reason": "x".repeat(2048),
    }))
    .unwrap();
    let split = stalled.len() / 2;
    let mut stalled_stream = TcpStream::connect(rig.addr).unwrap();
    stalled_stream
        .write_all(
            format!(
                "POST /command HTTP/1.1\r\nHost: qa\r\nContent-Type: application/json\r\n\
                 Content-Length: {}\r\nConnection: close\r\n\r\n",
                stalled.len()
            )
            .as_bytes(),
        )
        .unwrap();
    stalled_stream.write_all(&stalled[..split]).unwrap();
    // Let the dispatcher route the stalled head before the flood
    // arrives, so submission 0 is the drain's front.
    thread::sleep(Duration::from_millis(100));

    let bodies: Vec<Vec<u8>> = (1..=FLOOD).map(body_for).collect();
    let flood_thread = thread::spawn(move || pipelined_commands(rig.addr, &bodies));
    // Let the wave pile into the lane and overflow deck before the
    // stalled body completes and the drain begins.
    thread::sleep(Duration::from_millis(300));
    stalled_stream.write_all(&stalled[split..]).unwrap();
    stalled_stream
        .set_read_timeout(Some(Duration::from_secs(15)))
        .unwrap();
    let mut answer = Vec::new();
    let mut buf = [0u8; 8192];
    while !answer.windows(4).any(|window| window == b"\r\n\r\n") {
        let n = stalled_stream.read(&mut buf).unwrap();
        if n == 0 {
            break;
        }
        answer.extend_from_slice(&buf[..n]);
    }
    assert!(
        String::from_utf8_lossy(&answer)
            .lines()
            .next()
            .is_some_and(|line| line.contains(" 200")),
        "the stalled submission answered: {}",
        String::from_utf8_lossy(&answer)
    );
    let replies = flood_thread.join().unwrap();

    // Every submission answered 200 with its own parseable receipt —
    // a bare fault, a missing answer, or another submission's receipt
    // is the defect.
    for (offset, (status, receipt)) in replies.iter().enumerate() {
        let index = offset + 1;
        assert_eq!(
            *status, 200,
            "submission {index} met an HTTP-layer error, not a receipt"
        );
        let receipt = receipt
            .as_ref()
            .unwrap_or_else(|| panic!("submission {index} answered without a receipt"));
        assert_eq!(
            receipt.actor.as_deref(),
            Some(format!("qa-lane-{index}").as_str()),
            "submission {index} answered with another submission's receipt"
        );
    }

    // The executor's log carries one receipt per submission in
    // submission order — the first `capacity` admit, the rest take
    // the named `queue_full` rejection, and no overflowed submission
    // mints ahead of an earlier one still queued in the lane.
    let receipts = rig.client.receipts().unwrap();
    assert_eq!(receipts.len(), FLOOD + 1);
    for (index, receipt) in receipts.iter().enumerate() {
        assert_eq!(
            receipt.actor.as_deref(),
            Some(format!("qa-lane-{index}").as_str()),
            "receipt {index} is not the submission's own — the log fell \
             out of submission order"
        );
        let expected = if index < CAPACITY {
            "accepted"
        } else {
            "rejected:queue_full"
        };
        assert_eq!(outcome_key(receipt), expected, "receipt {index}");
    }
}
