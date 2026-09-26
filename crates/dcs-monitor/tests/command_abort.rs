//! Issue #948 — the `command-abort-shows-failed-while-applied` QA
//! finding — and issue #1036's
//! `command-abort-reports-failure-after-apply` follow-up: the page's
//! `AbortSignal.timeout` on `POST /command` cancels the client's wait,
//! never the server's work — the request may already sit buffered in
//! the listen backlog — and a killed response path ends the wait the
//! same way without an answer, so an unanswered submission's outcome
//! is genuinely unknown and rendering it "command failed" asserts a
//! non-application that is not true. The page now answers the
//! indeterminate "outcome unknown" verdict for every post that ends
//! without an answered receipt — no fetch rejection proves the request
//! never left — never retries a submission whose fate is unresolved (a
//! landed invoke would apply twice), and holds the declared
//! `{command, actor, reason}` pending so the journaled
//! `command_settled` — the receipted contract's truth about a command —
//! resolves the standing notice. Only the endpoint's own answered 4xx,
//! a refusal provably delivered before admission, stays an honest
//! "command failed". These tests drive the page's `submitCommand`,
//! mirrored here, against a stub transport answering `/command` past
//! the abort bound or closing the socket unanswered, then serve the
//! applied settle the buffered request produced once the listener ran
//! again — the `docker pause`/`unpause` reproduction and the killed
//! response path simulated.

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

/// The answer the stub's `/command` listener scripts for a request
/// that provably arrived.
#[derive(Clone)]
enum Answer {
    /// 200 OK carrying the receipt body after `hold` — the
    /// docker-paused controller's late answer.
    Receipt(String),
    /// Nothing — the socket closes after `hold` without a response,
    /// the killed response path the reproduction's second variant
    /// names.
    Close,
    /// The endpoint's own answered refusal — an HTTP status plus body
    /// that provably reached the client before admission could queue
    /// the command.
    Refusal(u16, String),
}

/// One response write, best effort: the abandoned client's socket is
/// already closed when a held answer finally lands.
fn reply(stream: &mut TcpStream, status: &str, body: &str) {
    let _ = stream.write_all(
        format!(
            "HTTP/1.1 {status}\r\nContent-Type: application/json\r\n\
             Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        )
        .as_bytes(),
    );
}

/// The reproduction's transport stand-in: a listener whose `/command`
/// answer scripts through `Answer` — the late receipt, the silent
/// close the killed response path stands in for, or the pre-admission
/// refusal — and whose `/journal` serves the settled entries the
/// buffered request produced once the listener ran again. `received`
/// reports each POST whose bytes arrived, proving the abandoned
/// request landed server-side.
struct StubTransport {
    addr: SocketAddr,
    stop: Arc<AtomicBool>,
    received: Receiver<()>,
    thread: Option<thread::JoinHandle<()>>,
}

impl StubTransport {
    fn start(hold: Duration, journal: Vec<JournalEntry>, receipt: &CommandReceipt) -> Self {
        Self::start_with(
            hold,
            journal,
            Answer::Receipt(serde_json::to_string(receipt).unwrap()),
        )
    }

    /// The killed response path: the listener reads the request, then
    /// closes the socket unanswered.
    fn start_closing(hold: Duration, journal: Vec<JournalEntry>) -> Self {
        Self::start_with(hold, journal, Answer::Close)
    }

    /// The endpoint's own answered refusal: the listener answers
    /// `status` — a provable pre-admission rejection.
    fn start_refusing(hold: Duration, journal: Vec<JournalEntry>, status: u16) -> Self {
        Self::start_with(
            hold,
            journal,
            Answer::Refusal(status, "the request never reached admission".to_string()),
        )
    }

    fn start_with(hold: Duration, journal: Vec<JournalEntry>, answer: Answer) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let addr = listener.local_addr().unwrap();
        let stop = Arc::new(AtomicBool::new(false));
        let (tx, received) = mpsc::channel();
        let journal_body = serde_json::to_string(&journal).unwrap();
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
                let answer = answer.clone();
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
                    match (method, path.split('?').next().unwrap_or("")) {
                        ("POST", "/command") => {
                            // The request provably arrived — then the
                            // scripted answer, held past the client's
                            // bound exactly as the paused controller's
                            // backlog held it.
                            let _ = tx.send(());
                            thread::sleep(hold);
                            match &answer {
                                Answer::Receipt(body) => reply(&mut stream, "200 OK", body),
                                // The killed response path: the socket
                                // drops without a status line.
                                Answer::Close => {}
                                Answer::Refusal(status, body) => {
                                    reply(&mut stream, &format!("{status} Refused"), body)
                                }
                            }
                        }
                        ("GET", "/journal") => reply(&mut stream, "200 OK", &journal_body),
                        _ => reply(&mut stream, "200 OK", "null"),
                    }
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
/// answered-refusal split: every end without an answered receipt
/// leaves the outcome indeterminate — the bound firing, a dead
/// response path, a refused connect the fetch surface cannot prove
/// non-transmission for — while an answered 4xx is the one refusal
/// that proves the command never queued.
#[derive(Debug)]
enum PostEnd {
    /// The peer's response JSON — the command's receipt — answered in
    /// time.
    Answered(serde_json::Value),
    /// No answered response: the wait ended — the bound, a killed
    /// response path, a dead listener — not provably the server's work.
    Unanswered,
    /// The endpoint's own answered refusal: a 4xx response arrived, so
    /// the request provably never queued.
    Refused { status: u16, body: String },
}

/// The page's `postCommand`, mirrored: one POST under the poll's abort
/// bound — the socket's read bound standing in for
/// `AbortSignal.timeout` — answering the response's JSON, any
/// unanswered end indistinguishably, or the answered refusal.
fn post_command(addr: SocketAddr, body: &serde_json::Value, bound: Duration) -> PostEnd {
    let mut stream = match TcpStream::connect_timeout(&addr, bound) {
        Ok(stream) => stream,
        // A refused connect throws the same indistinguishable rejection
        // at the fetch surface — the page cannot prove the request
        // never left, so it is unanswered, not failed.
        Err(_) => return PostEnd::Unanswered,
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
        return PostEnd::Unanswered;
    }
    let mut raw = Vec::new();
    let mut chunk = [0u8; 4096];
    loop {
        match stream.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => raw.extend_from_slice(&chunk[..n]),
            Err(error) if error.kind() == ErrorKind::Interrupted => continue,
            // The bound firing and a mid-response reset alike end the
            // wait unanswered — the read failure's kind never reaches
            // the page either way.
            Err(_) => return PostEnd::Unanswered,
        }
    }
    let Some(body_start) = raw
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .map(|end| end + 4)
    else {
        return PostEnd::Unanswered;
    };
    let status = String::from_utf8_lossy(&raw[..body_start])
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|code| code.parse::<u16>().ok());
    match status {
        Some(status) if (400..500).contains(&status) => PostEnd::Refused {
            status,
            body: String::from_utf8_lossy(&raw[body_start..]).into_owned(),
        },
        Some(200..=299) => match serde_json::from_slice(&raw[body_start..]) {
            Ok(answer) => PostEnd::Answered(answer),
            Err(_) => PostEnd::Unanswered,
        },
        _ => PostEnd::Unanswered,
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
/// receipt, the indeterminate verdict every unanswered post leaves, or
/// the answered refusal's provable failure.
#[derive(Debug)]
enum Submission {
    /// The peer answered the command's receipt inside the bound.
    Receipt(serde_json::Value),
    /// The post ended unanswered: outcome indeterminate — the pending
    /// record the journaled settle resolves.
    Abandoned(Pending),
    /// The endpoint's own answered refusal: the request provably never
    /// queued, so the page's "command failed" is the honest verdict.
    Failed(String),
}

/// The page's `submitCommand` POST leg plus `abandonedSubmission`,
/// mirrored: the verdict vocabulary — never "command failed" for an
/// outcome an unanswered post left open.
fn submit_command(addr: SocketAddr, command: &serde_json::Value, bound: Duration) -> Submission {
    match post_command(addr, command, bound) {
        PostEnd::Answered(answer) => Submission::Receipt(answer),
        PostEnd::Unanswered => Submission::Abandoned(Pending {
            command: command.clone(),
            actor: None,
            reason: None,
            notice: "command outcome unknown — no receipt answered the submission \
                     and it may still apply; the journaled settled receipt is the \
                     verdict — resubmitting now risks applying the command twice."
                .to_string(),
        }),
        PostEnd::Refused { status, body } => Submission::Failed(format!(
            "command failed: the monitor refused the request: HTTP {status}: {body}"
        )),
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

/// The finding's killed-path variant: the listener reads the request —
/// the write lands server-side — then closes the socket unanswered.
/// The fetch surface cannot prove the request never left, so the
/// mirrored `submitCommand` must answer the same indeterminate verdict
/// the abort bound produces — never "command failed" — and the
/// journaled settle resolves it.
#[test]
fn a_killed_response_path_reports_indeterminate_then_the_journaled_settle() {
    let command = serde_json::json!({
        "write_value": {"point": 10, "kind": "float", "value": {"float": 1.0}}
    });
    let submitted = Command::WriteValue {
        point: PointId(10),
        kind: ValueKind::Float,
        value: Value::Float(1.0),
    };
    let settle = JournalEntry {
        seq: 309,
        tick: Tick(7864),
        event: JournalEvent::CommandSettled {
            receipt: CommandReceipt {
                command: submitted,
                outcome: CommandOutcome::Applied { tick: Tick(7864) },
                actor: None,
                reason: None,
            },
        },
    };
    // The socket dies once the request head has been read — the QA
    // evidence's "socket closed before reading the response", journaled
    // applied@tick 7864.
    let transport = StubTransport::start_closing(Duration::ZERO, vec![settle]);

    let submission = submit_command(transport.addr, &command, Duration::from_millis(500));
    let Submission::Abandoned(pending) = submission else {
        panic!("a killed response path must answer the indeterminate verdict, got {submission:?}");
    };
    assert!(
        pending.notice.contains("outcome unknown"),
        "the unanswered submission must report an indeterminate outcome: {}",
        pending.notice
    );
    assert!(
        !pending.notice.contains("command failed"),
        "the defect's verdict must not stand: {}",
        pending.notice
    );

    // The request provably landed — the stub read its bytes — so
    // "outcome unknown" was the only honest verdict.
    transport
        .received
        .recv_timeout(Duration::from_secs(5))
        .expect("the abandoned POST's bytes never reached the listener");

    let entries = MonitorClient::new(transport.addr).journal(0).unwrap();
    let resolved = resolve_pending(&pending, &entries)
        .expect("the journaled settle must answer the abandoned submission");
    assert!(
        resolved.contains("applied at tick 7864"),
        "the settle must resolve the notice to the applied verdict: {resolved}"
    );
}

/// The split's dead-listener end: a refused connect throws the same
/// indistinguishable rejection at the fetch surface — the page cannot
/// prove the request never left, so it answers the indeterminate
/// verdict too; the journal's silence is the verdict a "command
/// failed" claim would have falsified.
#[test]
fn a_dead_listener_reports_indeterminate_too() {
    // Bind then drop, so nothing listens on the address.
    let addr = TcpListener::bind("127.0.0.1:0")
        .map(|listener| listener.local_addr().unwrap())
        .unwrap();
    let command = serde_json::json!({
        "write_value": {"point": 10, "kind": "float", "value": {"float": 1.0}}
    });
    match submit_command(addr, &command, Duration::from_millis(200)) {
        Submission::Abandoned(pending) => {
            assert!(
                pending.notice.contains("outcome unknown"),
                "an unprovable non-delivery must not assert failure: {}",
                pending.notice
            );
            assert!(
                !pending.notice.contains("command failed"),
                "the defect's verdict must not stand: {}",
                pending.notice
            );
        }
        other => panic!("a dead listener is indeterminate, not failed: {other:?}"),
    }
}

/// The one provable failure: the endpoint's own answered 4xx — the
/// documented pre-admission refusal — reached the client, so the
/// command provably never queued and "command failed" stays honest.
#[test]
fn an_answered_refusal_reports_failure() {
    let command = serde_json::json!({
        "write_value": {"point": 10, "kind": "float", "value": {"float": 1.0}}
    });
    let transport = StubTransport::start_refusing(Duration::ZERO, vec![], 400);
    match submit_command(transport.addr, &command, Duration::from_millis(500)) {
        Submission::Failed(text) => {
            assert!(text.contains("command failed"), "{text}");
            assert!(text.contains("HTTP 400"), "{text}");
        }
        other => panic!("an answered refusal is a provable failure: {other:?}"),
    }
}

/// The page itself must carry the indeterminate-outcome machinery:
/// the answered-refusal split, the pending record, the journaled
/// settle's resolution — and the unanswered post must answer before
/// the not_active retry can resend an unresolved command.
#[test]
fn the_page_carries_the_indeterminate_outcome_path() {
    let page = dcs_monitor::PAGE;
    for needle in [
        "function answeredRefusal(status, detail)",
        "error.answered = true",
        "throw answeredRefusal(response.status,",
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
    // The indeterminate verdict precedes the not_active re-poll-and-
    // retry: a submission whose fate is unresolved is never resent — a
    // landed invoke would apply twice.
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
