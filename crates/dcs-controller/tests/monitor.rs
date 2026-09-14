//! End-to-end tests for `dcs-controller --listen`: the `dcs-monitor`
//! endpoints serve while the wall-clock-paced scan runs, commands apply
//! at the documented scan boundary, and `POST /scan` is refused under
//! pacing.

use dcs_core::{Command, CommandOutcome, PointId, TelemetrySnapshot, Value, ValueKind};
use dcs_monitor::MonitorClient;
use std::io::{BufRead, BufReader, Read};
use std::net::SocketAddr;
use std::process::{Child, Command as Process, Stdio};
use std::time::{Duration, Instant};

const BINARY: &str = env!("CARGO_BIN_EXE_dcs-controller");
const TANK_LOOP: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-assembly/fixtures/tank_loop.json"
);

/// Spawns the controller on `TANK_LOOP` with `--listen 127.0.0.1:0` plus
/// `args`, parses the bound address the process announces on stderr, and
/// returns the child and a ready client. `capture_stdout` pipes stdout
/// for runs expected to print a final snapshot; unbounded runs null it
/// so the per-scan JSON lines cannot fill the pipe and stall the scan.
fn spawn_monitored(args: &[&str], capture_stdout: bool) -> (Child, MonitorClient) {
    let mut command = Process::new(BINARY);
    command
        .arg(TANK_LOOP)
        .args(args)
        .arg("--listen")
        .arg("127.0.0.1:0")
        .stderr(Stdio::piped())
        .stdout(if capture_stdout {
            Stdio::piped()
        } else {
            Stdio::null()
        });
    let mut child = command.spawn().unwrap();
    let mut stderr = BufReader::new(child.stderr.take().unwrap());
    let addr: SocketAddr = loop {
        let mut line = String::new();
        if stderr.read_line(&mut line).unwrap() == 0 {
            panic!(
                "controller exited before announcing its listener: {:?}",
                child.wait().unwrap()
            );
        }
        if let Some(addr) = line.trim().strip_prefix("listening on ") {
            break addr.parse().unwrap();
        }
    };
    (child, MonitorClient::new(addr))
}

/// Polls `GET /snapshot` until `until` holds, returning that snapshot.
fn wait_for(
    client: &MonitorClient,
    until: impl Fn(&TelemetrySnapshot) -> bool,
) -> TelemetrySnapshot {
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        let snapshot = client.snapshot().unwrap();
        if until(&snapshot) {
            return snapshot;
        }
        assert!(
            Instant::now() < deadline,
            "timed out waiting for snapshot condition"
        );
        std::thread::sleep(Duration::from_millis(5));
    }
}

fn point_value(snapshot: &TelemetrySnapshot, point: u64) -> Option<Value> {
    snapshot
        .points
        .iter()
        .find(|telemetry| telemetry.point == PointId(point))
        .and_then(|telemetry| telemetry.sample)
        .map(|sample| sample.value)
}

#[test]
fn paced_run_serves_snapshots_whose_tick_advances() {
    let (mut child, client) = spawn_monitored(&["--scan-ms", "20"], false);

    let first = client.snapshot().unwrap();
    let later = wait_for(&client, |s| s.tick > first.tick);
    assert!(later.tick > first.tick);
    // The model's point set is served under pacing.
    assert_eq!(later.points.len(), 5);

    child.kill().unwrap();
    child.wait().unwrap();
}

#[test]
fn command_roundtrips_and_changes_later_output_at_the_scan_boundary() {
    let (mut child, client) = spawn_monitored(&["--scan-ms", "20"], false);
    // Let the paced run get going.
    let before = wait_for(&client, |s| s.tick.0 >= 3);

    // Raise the level setpoint (point 10): the PID's valve output at
    // point 12 must leave its floor for the new error.
    let command = Command::WriteValue {
        point: PointId(10),
        kind: ValueKind::Float,
        value: Value::Float(50.0),
    };
    let receipt = client.command(&command).unwrap();
    let apply_tick = match receipt.outcome {
        CommandOutcome::Accepted { apply_tick } => apply_tick,
        outcome => panic!("command not accepted: {outcome:?}"),
    };
    // The documented boundary: the head of the next scan after submission.
    assert!(apply_tick > before.tick);

    // At the apply tick the staged write lands: point 10's input read
    // observes it and the PID drives the valve output off its floor.
    wait_for(&client, |s| {
        s.tick >= apply_tick && point_value(s, 10) == Some(Value::Float(50.0))
    });
    wait_for(
        &client,
        |s| matches!(point_value(s, 12), Some(Value::Float(v)) if v > 8.0),
    );

    // Exactly one receipt, now reporting the applied tick.
    let receipts = client.receipts().unwrap();
    assert_eq!(receipts.len(), 1);
    assert_eq!(
        receipts[0].outcome,
        CommandOutcome::Applied { tick: apply_tick }
    );

    child.kill().unwrap();
    child.wait().unwrap();
}

#[test]
fn scan_requests_are_refused_while_pacing() {
    let (mut child, client) = spawn_monitored(&["--scan-ms", "20"], false);

    // The documented interleaving: the wall clock owns the scan schedule,
    // so an externally requested scan is refused, not interleaved.
    let (status, body) = client
        .request("POST", "/scan", Some(r#"{"scans":1}"#))
        .unwrap();
    assert_eq!(status, 409, "{body}");
    assert!(body.contains("paced"), "{body}");

    // Pacing still owns the tick count.
    let before = client.snapshot().unwrap().tick;
    wait_for(&client, |s| s.tick > before);

    child.kill().unwrap();
    child.wait().unwrap();
}

#[test]
fn bounded_monitored_run_shuts_down_gracefully() {
    let (mut child, client) = spawn_monitored(&["--ticks", "5", "--scan-ms", "20"], true);

    // The monitor answers while the run is live.
    client.snapshot().unwrap();

    // The run completes: exit 0, final snapshot on stdout, and the
    // listener gone — a graceful monitor shutdown, not a killed thread.
    let deadline = Instant::now() + Duration::from_secs(10);
    let status = loop {
        if let Some(status) = child.try_wait().unwrap() {
            break status;
        }
        assert!(
            Instant::now() < deadline,
            "bounded monitored run did not exit"
        );
        std::thread::sleep(Duration::from_millis(10));
    };
    assert!(status.success());
    let mut stdout = String::new();
    child
        .stdout
        .take()
        .unwrap()
        .read_to_string(&mut stdout)
        .unwrap();
    let snapshot: serde_json::Value = serde_json::from_str(&stdout).unwrap();
    assert_eq!(snapshot["tick"], 5);

    let deadline = Instant::now() + Duration::from_secs(5);
    while client.snapshot().is_ok() {
        assert!(
            Instant::now() < deadline,
            "monitor still serving after the run ended"
        );
        std::thread::sleep(Duration::from_millis(5));
    }
}

#[test]
fn bind_failure_exits_nonzero_naming_the_address() {
    // Holding the port makes the controller's bind fail.
    let blocker = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = blocker.local_addr().unwrap();
    let output = Process::new(BINARY)
        .args([TANK_LOOP, "--scan-ms", "20", "--listen", &addr.to_string()])
        .output()
        .unwrap();
    assert!(!output.status.success());
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(stderr.contains(&addr.to_string()), "{stderr}");
}

#[test]
fn listen_without_pacing_is_a_usage_error() {
    // Pure --ticks runs stay deterministic and monitor-free.
    let output = Process::new(BINARY)
        .args([TANK_LOOP, "--ticks", "5", "--listen", "127.0.0.1:0"])
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(stderr.contains("--listen requires --scan-ms"), "{stderr}");
}
