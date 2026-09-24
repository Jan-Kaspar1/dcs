//! Issue #948 — the `command-abort-shows-failed-while-applied` QA
//! finding: the page's `AbortSignal.timeout` on `POST /command` cancels
//! the client's wait, never the server's work — the request may already
//! sit buffered in the listen backlog — so an abandoned submission's
//! outcome is genuinely unknown, and rendering it "command failed"
//! asserts a non-application that is not true. The page now answers the
//! indeterminate "outcome unknown" verdict, never retries a submission
//! whose fate is unresolved (a landed invoke would apply twice), and
//! holds the declared `{command, actor, reason}` pending so the
//! journaled `command_settled` — the receipted contract's truth about a
//! command — resolves the standing notice. These tests drive the page's
//! `submitCommand`, mirrored here, against a stub transport answering
//! `/command` past the abort bound, then serve the applied settle the
//! buffered request produced once the stuck listener ran again — the
//! `docker pause`/`unpause` reproduction simulated.

use dcs_core::{
    Command, CommandOutcome, CommandReceipt, JournalEntry, JournalEvent, PointId, Tick, Value,
    ValueKind,
};
use dcs_monitor::MonitorClient;
use std::io::{ErrorKind, Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver};
use std::thread;
use std::time::Duration;

/// The reproduction's transport stand-in: a listener whose `/command`
/// answer lands only after `hold` — the docker-paused controller's
/// buffered request, written to a client that already abandoned the
/// wait — and whose `/journal` serves the settled entries the buffered
/// request produced once the listener ran again. `received` reports
/// each POST whose bytes arrived, proving the abandoned request landed
/// server-side.
struct StubTransport {
    addr: SocketAddr,
    stop: Arc<AtomicBool>,
    received: Receiver<()>,
    thread: Option<thread::JoinHandle<()>>,
}

impl StubTransport {
    fn start(hold: Duration, journal: Vec<JournalEntry>, receipt: &CommandReceipt) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let addr = listener.local_addr().unwrap();
        let stop = Arc::new(AtomicBool::new(false));
        let (tx, received) = mpsc::channel();
        let journal_body = serde_json::to_string(&journal).unwrap();
        let receipt_body = serde_json::to_string(receipt).unwrap();
        let flag = Arc::clone(&stop);
        let thread = thread::spawn(move || {
            while !flag.load(Ordering::Relaxed) {
                let mut stream = match listener.accept() {
                    Ok((stream, _)) => stream,
                    Err(error) if error.kind() == ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(2));
                        continue;
                    }
                    Err(_) => break,
                };
                let tx = tx.clone();
                let journal_body = journal_body.clone();
                let receipt_body = receipt_body.clone();
                thread::spawn(move || {
                    stream
                        .set_read_timeout(Some(Duration::from_secs(5)))
                        .unwrap();
                    // The request head — the buffered bytes the QA
                    // reproduction says land while the listener is
                    // stuck.
                    let mut head = Vec::new();
                    let mut chunk = [0u8; 4096];
                    let filled = loop {
                        if head.windows(4).any(|window| window == b"\r\n\r\n") {
                            break true;
                        }
                        match stream.read(&mut chunk) {
                            Ok(0) => break false,
                            Ok(n) => head.extend_from_slice(&chunk[..n]),
                            Err(_) => break false,
                        }
                    };
                    if !filled {
                        return;
                    }
                    let request = String::from_utf8_lossy(&head);
                    let mut parts = request.lines().next().unwrap_or("").split_whitespace();
                    let (method, path) = (parts.next().unwrap_or(""), parts.next().unwrap_or(""));
                    let body = match (method, path.split('?').next().unwrap_or("")) {
                        ("POST", "/command") => {
                            // The request provably arrived — then the
                            // stuck listener holds the answer past the
                            // client's bound, exactly as the paused
                            // controller's backlog did.
                            let _ = tx.send(());
                            thread::sleep(hold);
                            receipt_body
                        }
                        ("GET", "/journal") => journal_body,
                        _ => "null".to_string(),
                    };
                    // Best effort: the abandoned client's socket is
                    // already closed when the held answer finally lands.
                    let _ = stream.write_all(
                        format!(
                            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\
                             Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
                            body.len()
                        )
                        .as_bytes(),
                    );
                });
            }
        });
        Self {
            addr,
            stop,
            received,
            thread: Some(thread),
        }
    }
}

impl Drop for StubTransport {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

/// What the bounded `/command` POST can end in — the page's
/// `isAbortError` split: the bound firing leaves the outcome
/// indeterminate; a refused connect or a failed send proves the request
/// never left.
enum PostEnd {
    /// The peer's response JSON — the command's receipt — answered in
    /// time.
    Answered(serde_json::Value),
    /// The bound fired mid-wait: the wait ended, not provably the work.
    Aborted,
    /// The transport refused the request itself.
    Refused(String),
}

/// The page's `postCommand`, mirrored: one POST under the poll's abort
/// bound — the socket's read bound standing in for
/// `AbortSignal.timeout` — answering the response's JSON, the bound,
/// or the refusal.
fn post_command(addr: SocketAddr, body: &serde_json::Value, bound: Duration) -> PostEnd {
    let mut stream = match TcpStream::connect_timeout(&addr, bound) {
        Ok(stream) => stream,
        Err(error) => return PostEnd::Refused(error.to_string()),
    };
    stream.set_read_timeout(Some(bound)).unwrap();
    stream.set_write_timeout(Some(bound)).unwrap();
    let payload = serde_json::to_vec(body).unwrap();
    let request = format!(
        "POST /command HTTP/1.1\r\nHost: {addr}\r\nContent-Type: text/plain\r\n\
         Content-Length: {}\r\nConnection: close\r\n\r\n",
        payload.len()
    );
    if stream
        .write_all(request.as_bytes())
        .and_then(|()| stream.write_all(&payload))
        .is_err()
    {
        return PostEnd::Refused("the send failed".to_string());
    }
    let mut raw = Vec::new();
    let mut chunk = [0u8; 4096];
    loop {
        match stream.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => raw.extend_from_slice(&chunk[..n]),
            Err(error) if error.kind() == ErrorKind::Interrupted => continue,
            Err(error) if matches!(error.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut) => {
                return PostEnd::Aborted;
            }
            Err(error) => return PostEnd::Refused(error.to_string()),
        }
    }
    let body_start = raw
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .map(|end| end + 4)
        .unwrap_or(raw.len());
    match serde_json::from_slice(&raw[body_start..]) {
        Ok(answer) => PostEnd::Answered(answer),
        Err(error) => PostEnd::Refused(error.to_string()),
    }
}

/// The page's `pendingCommands` record — the abandoned submission's
/// declared fields plus the notice the receipt pane stands on.
#[derive(Debug)]
struct Pending {
    command: serde_json::Value,
    actor: Option<String>,
    reason: Option<String>,
    notice: String,
}

/// The page's `submitCommand`, mirrored to its POST leg: the answered
/// receipt, the indeterminate verdict the abort bound leaves, or a
/// genuine not-sent failure.
#[derive(Debug)]
enum Submission {
    /// The peer answered the command's receipt inside the bound.
    Receipt(serde_json::Value),
    /// The bound fired: outcome indeterminate — the pending record the
    /// journaled settle resolves.
    Abandoned(Pending),
    /// The transport refused: the request provably never left, so the
    /// page's "command failed" is the honest verdict.
    Failed(String),
}

/// The page's `submitCommand` POST leg plus `abandonedSubmission`,
/// mirrored: the bound's verdict vocabulary — never "command failed"
/// for an outcome the abort left open.
fn submit_command(addr: SocketAddr, command: &serde_json::Value, bound: Duration) -> Submission {
    match post_command(addr, command, bound) {
        PostEnd::Answered(answer) => Submission::Receipt(answer),
        PostEnd::Aborted => Submission::Abandoned(Pending {
            command: command.clone(),
            actor: None,
            reason: None,
            notice: "command outcome unknown — the request outlived the abort \
                     bound and may still apply; the journaled settled receipt is \
                     the verdict — resubmitting now risks applying the command twice."
                .to_string(),
        }),
        PostEnd::Refused(detail) => Submission::Failed(format!("command failed: {detail}")),
    }
}

/// The page's `sameJson`, mirrored: structural equality with numeric
/// equivalence — a receipt's echoed command needn't match the submitted
/// literal's number spelling (the `float: 1` vs `float: 1.0` case).
fn same_json(a: &serde_json::Value, b: &serde_json::Value) -> bool {
    use serde_json::Value;
    match (a, b) {
        (Value::Object(a), Value::Object(b)) => {
            a.len() == b.len()
                && a.iter()
                    .all(|(key, value)| b.get(key).is_some_and(|other| same_json(value, other)))
        }
        (Value::Array(a), Value::Array(b)) => {
            a.len() == b.len() && a.iter().zip(b).all(|(x, y)| same_json(x, y))
        }
        (Value::Number(a), Value::Number(b)) => a.as_f64() == b.as_f64(),
        _ => a == b,
    }
}

/// The page's `sameSubmission`, mirrored: the settled receipt answers
/// the abandoned submission when the command matches structurally and
/// the declared actor and reason the receipt echoes agree.
fn same_submission(receipt: &CommandReceipt, pending: &Pending) -> bool {
    serde_json::to_value(&receipt.command).is_ok_and(|command| {
        same_json(&command, &pending.command)
            && receipt.actor == pending.actor
            && receipt.reason == pending.reason
    })
}

/// The page's `describeOutcome`, mirrored for the outcomes these tests
/// exercise.
fn describe_outcome(outcome: &CommandOutcome) -> String {
    match outcome {
        CommandOutcome::Applied { tick } => format!("applied at tick {}", tick.0),
        CommandOutcome::Accepted { apply_tick } => {
            format!("accepted for tick {}", apply_tick.0)
        }
        CommandOutcome::Rejected { .. } => "rejected".to_string(),
    }
}

/// The page's pending-resolution fold in `refreshJournal`, mirrored:
/// the first journaled `command_settled` whose receipt matches the
/// abandoned submission resolves the indeterminate notice with the
/// authority's verdict.
fn resolve_pending(pending: &Pending, entries: &[JournalEntry]) -> Option<String> {
    entries.iter().find_map(|entry| match &entry.event {
        JournalEvent::CommandSettled { receipt } if same_submission(receipt, pending) => {
            Some(format!(
                "the abandoned submission settled — {}",
                describe_outcome(&receipt.outcome)
            ))
        }
        _ => None,
    })
}

/// The reproduction: a `/command` listener stuck past the page's abort
/// bound with the request bytes already buffered, then unpaused — the
/// buffered write applies and the journal settles it. The mirrored
/// `submitCommand` must answer the indeterminate verdict, and the
/// journaled settle must resolve it to `applied` — where the page once
/// printed `command failed: AbortError` beside the landed write.
#[test]
fn an_abandoned_command_reports_indeterminate_then_the_journaled_settle() {
    let command = serde_json::json!({
        "write_value": {"point": 10, "kind": "float", "value": {"float": 1.0}}
    });
    let submitted = Command::WriteValue {
        point: PointId(10),
        kind: ValueKind::Float,
        value: Value::Float(1.0),
    };
    // The receipt the stuck listener eventually answers — to a client
    // already gone — and the settle the buffered write journaled: the
    // QA evidence's seq 369, applied at tick 8019.
    let late_receipt = CommandReceipt {
        command: submitted.clone(),
        outcome: CommandOutcome::Accepted {
            apply_tick: Tick(8020),
        },
        actor: None,
        reason: None,
    };
    let settle = JournalEntry {
        seq: 369,
        tick: Tick(8019),
        event: JournalEvent::CommandSettled {
            receipt: CommandReceipt {
                command: submitted,
                outcome: CommandOutcome::Applied { tick: Tick(8019) },
                actor: None,
                reason: None,
            },
        },
    };
    let transport = StubTransport::start(Duration::from_millis(600), vec![settle], &late_receipt);

    // Drive the mirrored submitCommand past the abort bound — a
    // shortened stand-in for the page's POLL_MS; the defect is
    // bound-agnostic, the bound only needs to precede the answer.
    let submission = submit_command(transport.addr, &command, Duration::from_millis(150));
    let Submission::Abandoned(pending) = submission else {
        panic!("an aborted post must answer the indeterminate verdict, got {submission:?}");
    };
    assert!(
        pending.notice.contains("outcome unknown"),
        "the abandoned submission must report an indeterminate outcome: {}",
        pending.notice
    );
    assert!(
        !pending.notice.contains("command failed"),
        "the defect's verdict must not stand: {}",
        pending.notice
    );

    // The request provably landed — the stub read the buffered bytes —
    // so "outcome unknown" was the only honest verdict: a retry here is
    // the double-apply hazard the finding names.
    transport
        .received
        .recv_timeout(Duration::from_secs(5))
        .expect("the abandoned POST's bytes never reached the listener");

    // The buffered request applied once the listener ran again: the
    // journal serves the applied settle, and the pending submission
    // resolves to the authority's verdict rather than the failure the
    // page once claimed.
    let entries = MonitorClient::new(transport.addr).journal(0).unwrap();
    let resolved = resolve_pending(&pending, &entries)
        .expect("the journaled settle must answer the abandoned submission");
    assert!(
        resolved.contains("applied at tick 8019"),
        "the settle must resolve the notice to the applied verdict: {resolved}"
    );
}

/// The bound's ordinary end: a listener answering inside it lands the
/// peer's receipt, and the page renders it — the indeterminate verdict
/// is the abort's alone, not every submission's.
#[test]
fn an_answered_receipt_reports_normally() {
    let command = serde_json::json!({
        "write_value": {"point": 10, "kind": "float", "value": {"float": 1.0}}
    });
    let receipt = CommandReceipt {
        command: Command::WriteValue {
            point: PointId(10),
            kind: ValueKind::Float,
            value: Value::Float(1.0),
        },
        outcome: CommandOutcome::Accepted {
            apply_tick: Tick(1),
        },
        actor: None,
        reason: None,
    };
    let transport = StubTransport::start(Duration::ZERO, vec![], &receipt);
    match submit_command(transport.addr, &command, Duration::from_millis(500)) {
        Submission::Receipt(answer) => {
            assert_eq!(
                serde_json::from_value::<CommandReceipt>(answer).unwrap(),
                receipt,
                "the served receipt must reach the page verbatim"
            );
        }
        other => panic!("a prompt answer must land the receipt: {other:?}"),
    }
}

/// The `isAbortError` split the other way: a refused connect proves the
/// request never left — "command failed" stays the honest verdict for a
/// genuine not-sent submission.
#[test]
fn a_refused_submission_still_reports_failure() {
    // Bind then drop, so nothing listens on the address.
    let addr = TcpListener::bind("127.0.0.1:0")
        .map(|listener| listener.local_addr().unwrap())
        .unwrap();
    let command = serde_json::json!({
        "write_value": {"point": 10, "kind": "float", "value": {"float": 1.0}}
    });
    match submit_command(addr, &command, Duration::from_millis(200)) {
        Submission::Failed(text) => {
            assert!(text.contains("command failed"), "{text}");
        }
        other => panic!("a refused connect is a genuine failure, not indeterminate: {other:?}"),
    }
}

/// The page itself must carry the indeterminate-outcome machinery:
/// the abort split, the pending record, the journaled settle's
/// resolution — and the aborted post must answer before the not_active
/// retry can resend an unresolved command.
#[test]
fn the_page_carries_the_indeterminate_outcome_path() {
    let page = dcs_monitor::PAGE;
    for needle in [
        "function isAbortError(error)",
        "\"AbortError\"",
        "\"TimeoutError\"",
        "function abandonedSubmission(command, reason, error)",
        "return abandonedSubmission(command, reason, error)",
        "pendingCommands",
        "function sameSubmission(receipt, pending)",
        "\"indeterminate\" in outcome",
        "outcome unknown",
        "the abandoned submission settled",
    ] {
        assert!(page.contains(needle), "page lacks {needle}");
    }
    // The abort verdict precedes the not_active re-poll-and-retry: a
    // submission whose fate is unresolved is never resent — a landed
    // invoke would apply twice.
    let submit = page
        .split("async function submitCommand(command, reason)")
        .nth(1)
        .expect("page lacks submitCommand");
    let abandoned = submit
        .find("abandonedSubmission(command, reason, error)")
        .expect("submitCommand never answers the abandoned verdict");
    let retry = submit
        .find("isNotActive(answer)")
        .expect("submitCommand lost the not_active retry");
    assert!(
        abandoned < retry,
        "the indeterminate answer must precede the not_active retry"
    );
}
