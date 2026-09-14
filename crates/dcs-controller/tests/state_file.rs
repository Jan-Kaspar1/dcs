//! Restart recovery through `--state-file`: a lone controller's run
//! persists its serde `Checkpoint` to a file at the end of every scan
//! cycle, and a restarted binary resumes from it — the no-peer recovery
//! path the state-file decision records. The scripted runs below spawn
//! the binary itself, deterministic like every `--ticks` run.

use dcs_core::{TelemetrySnapshot, Tick};
use dcs_monitor::MonitorClient;
use dcs_runtime::{CHECKPOINT_FORMAT_VERSION, Checkpoint};
use std::io::{BufRead, BufReader};
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStderr, Command as Process, Stdio};

const BINARY: &str = env!("CARGO_BIN_EXE_dcs-controller");
/// The shared tank loop: a PID integrating a setpoint error against a
/// looped-back level — run state the checkpoint must carry for a resumed
/// run to equal the uninterrupted one.
const TANK_LOOP: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-assembly/fixtures/tank_loop.json"
);
const TANK_LOOP_SOURCE: &str = include_str!("../../dcs-assembly/fixtures/tank_loop.json");

/// Ticks the first half of an interrupted run executes before the
/// restart; the resumed run then executes that many again.
const HALF: u64 = 20;
const WHOLE: u64 = 2 * HALF;

fn run(args: &[&str]) -> std::process::Output {
    Process::new(BINARY).args(args).output().unwrap()
}

/// A scratch directory per test and process — tests run in parallel.
fn scratch(test: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("dcs-state-file-{test}-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

/// The checkpoint `path` currently holds — every written state file
/// must parse as the serde `Checkpoint` the monitor serves.
fn persisted(path: &Path) -> Checkpoint {
    serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap()
}

/// One interrupted run: `HALF` ticks, exit, then a restart resuming
/// from the state file for `HALF` more. Returns the resumed run's
/// stdout — the final snapshot at tick `WHOLE`.
fn interrupted(dir: &Path) -> Vec<u8> {
    let state = dir.join("state.json");
    let state_arg = state.to_str().unwrap().to_string();
    let first = run(&[
        TANK_LOOP,
        "--ticks",
        &HALF.to_string(),
        "--state-file",
        &state_arg,
    ]);
    assert!(first.status.success());
    let checkpoint = persisted(&state);
    assert_eq!(checkpoint.tick, Tick(HALF));
    assert_eq!(checkpoint.format_version, CHECKPOINT_FORMAT_VERSION);
    assert!(
        checkpoint.model_fingerprint.is_some(),
        "the persisted checkpoint names the model it was captured under"
    );

    let resumed = run(&[
        TANK_LOOP,
        "--ticks",
        &HALF.to_string(),
        "--state-file",
        &state_arg,
    ]);
    assert!(resumed.status.success());
    let stderr = String::from_utf8(resumed.stderr).unwrap();
    assert!(stderr.contains("resumed from state file"), "{stderr}");
    resumed.stdout
}

#[test]
fn a_restarted_run_resuming_the_state_file_matches_the_uninterrupted_run() {
    // The uninterrupted reference: one run of WHOLE ticks.
    let reference = run(&[TANK_LOOP, "--ticks", &WHOLE.to_string()]);
    assert!(reference.status.success());

    // The interrupted run must produce the identical continuation —
    // the resumed process's snapshot at tick WHOLE equals the
    // reference's.
    let dir = scratch("resume");
    let resumed = interrupted(&dir);
    assert_eq!(resumed, reference.stdout);
    let snapshot: TelemetrySnapshot = serde_json::from_slice(&resumed).unwrap();
    assert_eq!(snapshot.tick, Tick(WHOLE));

    // And the identical scripted run repeats identically — the file,
    // the resume, and the continuation are all deterministic.
    let again = scratch("resume-again");
    assert_eq!(interrupted(&again), resumed);

    let _ = std::fs::remove_dir_all(&dir);
    let _ = std::fs::remove_dir_all(&again);
}

#[test]
fn a_state_file_from_a_different_model_fails_resume_naming_the_fingerprint() {
    let dir = scratch("fingerprint");
    let state = dir.join("state.json");
    let first = run(&[
        TANK_LOOP,
        "--ticks",
        "10",
        "--state-file",
        state.to_str().unwrap(),
    ]);
    assert!(first.status.success());

    // A different model — same shape with a renamed signal — carries a
    // different fingerprint, so resuming its run under this model must
    // refuse, naming the fingerprint mismatch rather than starting fresh.
    let mut document: serde_json::Value = serde_json::from_str(TANK_LOOP_SOURCE).unwrap();
    document["signals"][0]["name"] = "renamed-signal".into();
    let other = dir.join("other-model.json");
    std::fs::write(&other, serde_json::to_string(&document).unwrap()).unwrap();

    let resumed = run(&[
        other.to_str().unwrap(),
        "--ticks",
        "10",
        "--state-file",
        state.to_str().unwrap(),
    ]);
    assert!(!resumed.status.success());
    let stderr = String::from_utf8(resumed.stderr).unwrap();
    assert!(stderr.contains("state file"), "{stderr}");
    assert!(stderr.contains("fingerprint"), "{stderr}");
    assert!(resumed.stdout.is_empty(), "no partial run's output");

    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_corrupt_state_file_fails_resume_without_partial_state() {
    let dir = scratch("corrupt");
    let state = dir.join("state.json");

    // Both torn and empty files are the same verdict: nonzero, naming
    // the file and why it cannot resume — never a silent fresh start.
    for contents in [b"{ not a checkpoint".as_slice(), b""] {
        std::fs::write(&state, contents).unwrap();
        let output = run(&[
            TANK_LOOP,
            "--ticks",
            "10",
            "--state-file",
            state.to_str().unwrap(),
        ]);
        assert!(!output.status.success());
        let stderr = String::from_utf8(output.stderr).unwrap();
        assert!(stderr.contains("state file"), "{stderr}");
        assert!(stderr.contains(state.to_str().unwrap()), "{stderr}");
        assert!(output.stdout.is_empty(), "no partial run's output");
    }

    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_missing_state_file_is_a_cold_start_and_without_the_flag_nothing_changes() {
    let dir = scratch("cold-start");
    let state = dir.join("state.json");

    // No file: the run starts cold and still checkpoints into it.
    let reference = run(&[TANK_LOOP, "--ticks", "10"]);
    let first = run(&[
        TANK_LOOP,
        "--ticks",
        "10",
        "--state-file",
        state.to_str().unwrap(),
    ]);
    assert!(first.status.success());
    assert_eq!(first.stdout, reference.stdout);
    assert_eq!(persisted(&state).tick, Tick(10));

    let _ = std::fs::remove_dir_all(&dir);
}

/// A spawned `--driven` controller: scans run only when `POST /scan`
/// requests them — a restart mid-run leaves the process dead until the
/// test spawns its replacement, exactly the restart the state file
/// exists for. Killed on drop so a panicking test leaves nothing behind.
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

/// Spawns the controller and reads stderr until its `listening on`
/// line — a resumed process reports the resume first.
fn spawn_driven(args: &[String]) -> Spawned {
    let mut child = Process::new(BINARY)
        .args(args)
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stderr = BufReader::new(child.stderr.take().unwrap());
    let addr = loop {
        let mut line = String::new();
        if stderr.read_line(&mut line).unwrap() == 0 {
            panic!("controller exited before reporting its address");
        }
        if let Some(addr) = line.trim().strip_prefix("listening on ") {
            break addr.parse().unwrap();
        }
    };
    Spawned {
        child,
        addr,
        _stderr: stderr,
    }
}

#[test]
fn a_driven_run_resumes_from_its_state_file() {
    let dir = scratch("driven");
    let state = dir.join("state.json");
    let state_arg = state.to_str().unwrap().to_string();
    let args = |state_file: bool| {
        let mut args = vec![
            TANK_LOOP.to_string(),
            "--listen".to_string(),
            "127.0.0.1:0".to_string(),
            "--driven".to_string(),
        ];
        if state_file {
            args.extend(["--state-file".to_string(), state_arg.clone()]);
        }
        args
    };

    // The uninterrupted driven reference: 2 * HALF requested scans.
    let reference_process = spawn_driven(&args(false));
    let reference = MonitorClient::new(reference_process.addr)
        .advance(WHOLE)
        .unwrap();

    // The interrupted driven run: HALF requested scans, then the
    // process dies; its replacement resumes from the file and runs the
    // remaining HALF — the externally paced half of the same boundary.
    let mut first = spawn_driven(&args(true));
    MonitorClient::new(first.addr).advance(HALF).unwrap();
    first.child.kill().unwrap();
    first.child.wait().unwrap();
    assert_eq!(persisted(&state).tick, Tick(HALF));

    let resumed = spawn_driven(&args(true));
    let continued = MonitorClient::new(resumed.addr).advance(HALF).unwrap();
    assert_eq!(continued, reference);

    let _ = std::fs::remove_dir_all(&dir);
}
