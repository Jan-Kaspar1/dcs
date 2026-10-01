//! The stalled-stdout regression for review finding #545 (managed as
//! issue #941): a paced run's per-scan snapshot lines are a bounded
//! consumer of the scan loop — a reader that stops draining the pipe
//! degrades delivery under the named `stdout_snapshot_drops` counter,
//! never cadence, and `io_health.scan_overruns` stays truthful through
//! the blocked window.

use dcs_monitor::MonitorClient;
use std::io::{BufRead, BufReader};
use std::process::{Child, Command as Process, Stdio};
use std::time::Duration;

mod support;

use support::listening_on;

const BINARY: &str = env!("CARGO_BIN_EXE_dcs-controller");
const TANK_LOOP: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-assembly/fixtures/tank_loop.json"
);

/// A spawned controller this test kills on every exit path — the
/// process outlives a failed assert otherwise.
struct KillOnDrop(Child);

impl Drop for KillOnDrop {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

#[test]
fn an_undrained_stdout_pipe_degrades_delivery_not_cadence() {
    let child = Process::new(BINARY)
        .args([TANK_LOOP, "--scan-ms", "10", "--listen", "127.0.0.1:0"])
        // The bounded pipe the finding describes: piped and never read.
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut child = KillOnDrop(child);

    // stderr drains through a thread so the script waits on named
    // lines without the child's diagnostics ever meeting a full pipe.
    let mut stderr = BufReader::new(child.0.stderr.take().unwrap());
    let (lines, stderr_lines) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let mut line = String::new();
        while stderr.read_line(&mut line).unwrap_or(0) > 0 {
            let _ = lines.send(line.trim_end().to_string());
            line.clear();
        }
    });
    let next_line = |deadline: Duration| -> String {
        stderr_lines
            .recv_timeout(deadline)
            .expect("controller stderr went quiet before the expected line")
    };

    // The monitor announces its address on stderr before the loop.
    let addr = loop {
        let line = next_line(Duration::from_secs(15));
        if let Some(addr) = listening_on(&line) {
            break addr;
        }
    };
    let client = MonitorClient::new(addr);

    // Once the sink reports drops, stdout is provably saturated: the
    // queue behind the writer only fills after the pipe did.
    loop {
        let line = next_line(Duration::from_secs(60));
        if line.contains("stdout_snapshot_drops") {
            break;
        }
    }

    // Inside the blocked window the scan keeps its declared period and
    // the overrun counter stays truthful — the stall is not charged to
    // the loop it never entered.
    let before = client.snapshot().unwrap();
    std::thread::sleep(Duration::from_millis(500));
    let after = client.snapshot().unwrap();
    let scanned = after.tick.0 - before.tick.0;
    assert!(
        scanned >= 25,
        "scan cadence collapsed behind a stalled stdout: {scanned} scans \
         in 500ms of a 10ms period"
    );
    let overruns = after.io_health.scan_overruns - before.io_health.scan_overruns;
    assert!(
        overruns * 4 <= scanned,
        "blocked output leaked into overrun accounting: {overruns} \
         overruns over {scanned} scans"
    );

    // The run is alive — degraded delivery, not a stopped plant.
    assert!(child.0.try_wait().unwrap().is_none());
}
