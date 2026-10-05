//! The graceful-shutdown contract (#820, WW-LCM-001 continuity): a
//! SIGTERM/SIGINT to `dcs-controller` stops the paced scan loop at a
//! scan boundary within the documented bound, flushes the latest
//! checkpoint through `--state-file` where configured, releases the
//! held plant write claim on the way out, and exits 0 — nonzero,
//! naming the reason, only on failure. A second signal forces prompt
//! exit, skipping the flush.
//!
//! The rig: a `dcs-plant-server` process owns the shared tank-loop
//! plant for the claim leg; the paced controller runs `--remote` on
//! the same model — the field-observing driver mode — with `--listen`
//! serving the monitor the script polls. Signals go through the
//! `kill` tool against the child's pid; `Spawned::wait_exit` bounds
//! every stop.
//!
//! The legs, each its own test so a failure names the clause:
//!
//! 1. **Flush and resume** — a paced active holding the field claim
//!    and persisting `--state-file` stops on SIGTERM with exit 0
//!    inside the bound, its stderr naming the graceful shutdown, the
//!    state file holding the stopped run's tick, and a relaunched
//!    process resuming at that tick and advancing — the resumed run
//!    continuing the persisted tick domain rather than cold-starting.
//! 2. **Claim handover** — the relaunched active above binds its
//!    monitor *after* the shutdown: its conditional startup claim was
//!    granted, so no dead claim fenced the successor. (A live
//!    incumbent's claim would refuse the start naming
//!    `FieldClaimFailed`; the graceful release marks the deliberate
//!    step-down instead.)
//! 3. **No-flags run** — a paced controller without `--state-file`
//!    stops on SIGTERM with exit 0 unchanged, naming the graceful
//!    shutdown and the absent state file.
//! 4. **Second signal** — a paced controller whose `--state-file`
//!    mount is a reader-less FIFO (the drain writer parked inside its
//!    write, so the graceful flush waits) stays alive after the first
//!    SIGTERM and exits promptly with status 143 on the second — the
//!    prompt-exit path a stalled sink must not close.

mod support;

use dcs_monitor::MonitorClient;
use dcs_runtime::Checkpoint;
use std::path::{Path, PathBuf};
use std::process::Command as Process;
use std::time::{Duration, Instant};

use support::{CONTROLLER, kill, listening_on, spawn_logged, spawn_plant, workspace_binary};

/// The shared plant's model — the dcs-plant tank loop.
const PLANT_MODEL: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-plant/fixtures/tank_loop.json"
);
/// The plant-side physics: the raw level lags the valve with τ = 2 s.
const PLANT_DYNAMICS: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-plant/fixtures/tank_loop_dynamics.json"
);

/// The documented shutdown bound: the loop stops within ten seconds
/// of the signal; the legs assert well inside it.
const SHUTDOWN_BOUND: Duration = Duration::from_secs(10);
/// The second signal's prompt-exit bound — far below the five-second
/// flush wait the stalled FIFO holds the first shutdown in.
const PROMPT_BOUND: Duration = Duration::from_secs(5);

/// A scratch directory per test and process — tests run in parallel.
fn scratch(test: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "dcs-graceful-shutdown-{test}-{}",
        std::process::id()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

/// Sends `signal` (`-TERM`, `-INT`) to `pid` through the `kill` tool —
/// the test-side form of the container runtime's `docker kill
/// --signal=` action.
fn signal(pid: u32, flag: &str) {
    let status = Process::new("kill")
        .arg(flag)
        .arg(pid.to_string())
        .status()
        .unwrap();
    assert!(status.success(), "kill {flag} {pid} failed: {status:?}");
}

/// The controller's served tick — polled until the run proves it
/// scans, so the signal lands on a live loop rather than a starter.
fn wait_tick(client: &MonitorClient, at_least: u64, what: &str) -> u64 {
    let deadline = Instant::now() + Duration::from_secs(30);
    loop {
        let tick = client.snapshot().unwrap().tick.0;
        if tick >= at_least {
            return tick;
        }
        assert!(
            Instant::now() < deadline,
            "{what} never reached tick {at_least}"
        );
        std::thread::sleep(Duration::from_millis(20));
    }
}

/// Spawns a paced `--remote` controller on `model` against `plant`,
/// returning the process plus its startup preamble (the state-file
/// resume report lives there). `state_file` is `None` for the
/// no-flags leg.
fn spawn_paced(
    model: &Path,
    plant: std::net::SocketAddr,
    state_file: Option<&Path>,
    scan_ms: u64,
) -> (support::Spawned, Vec<String>) {
    let mut args = vec![
        model.to_str().unwrap().to_string(),
        "--remote".to_string(),
        plant.to_string(),
        "--listen".to_string(),
        "127.0.0.1:0".to_string(),
        "--scan-ms".to_string(),
        scan_ms.to_string(),
        "--dt".to_string(),
        "0.1".to_string(),
    ];
    if let Some(path) = state_file {
        args.push("--state-file".to_string());
        args.push(path.to_str().unwrap().to_string());
    }
    spawn_logged(Path::new(CONTROLLER), &args, listening_on)
}

/// The checkpoint the state file holds — the resume path reads the
/// same document.
fn read_checkpoint(path: &Path) -> Checkpoint {
    serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap()
}

/// Legs 1+2: SIGTERM stops the paced field owner inside the bound with
/// exit 0, flushes the stopped tick to `--state-file`, and hands the
/// field to a relaunched successor that resumes the persisted run.
#[test]
fn sigterm_flushes_state_file_and_hands_the_field_to_the_resumed_run() {
    let dir = scratch("flush-resume");
    let plant = spawn_plant(Path::new(PLANT_MODEL), Path::new(PLANT_DYNAMICS));
    let state_path = dir.join("state.json");

    let (mut active, _) = spawn_paced(Path::new(PLANT_MODEL), plant.addr, Some(&state_path), 50);
    let client = MonitorClient::new(active.addr);
    let served = wait_tick(&client, 5, "the paced active");

    // The graceful stop: SIGTERM exits 0 inside the bound — never the
    // signal status an unhandled SIGTERM reports — naming the
    // graceful shutdown on stderr.
    let started = Instant::now();
    signal(active.child.id(), "-TERM");
    let status = active.wait_exit(SHUTDOWN_BOUND).unwrap_or_else(|| {
        panic!("the paced active did not stop within {SHUTDOWN_BOUND:?} of SIGTERM")
    });
    assert!(
        started.elapsed() < SHUTDOWN_BOUND,
        "the stop overran its documented bound"
    );
    assert!(
        status.success(),
        "the graceful stop must exit 0, not {status:?}"
    );
    let stderr = active.stderr_tail();
    assert!(
        stderr.contains("graceful shutdown on SIGTERM"),
        "the stop must name the graceful shutdown: {stderr:?}"
    );

    // The flushed checkpoint is the stopped run's: its tick reaches
    // the last served one — cycles between the last poll and the
    // signal only move it forward — under the run's fingerprint.
    let persisted = read_checkpoint(&state_path);
    assert!(
        persisted.tick.0 >= served,
        "the state file holds tick {}, the run served {served} before the signal",
        persisted.tick.0
    );

    // The handover: a relaunched active binds its monitor at once — its
    // conditional startup claim granted, so no dead claim fenced the
    // successor — and resumes the persisted tick instead of
    // cold-starting, then keeps scanning past it.
    let (mut resumed, preamble) =
        spawn_paced(Path::new(PLANT_MODEL), plant.addr, Some(&state_path), 50);
    assert!(
        preamble
            .iter()
            .any(|line| line.contains("resumed from state file")
                && line.contains(&format!("at tick {}", persisted.tick.0))),
        "the relaunch must report the resume at the persisted tick: {preamble:?}"
    );
    let successor = MonitorClient::new(resumed.addr);
    assert_eq!(
        successor.snapshot().unwrap().tick,
        persisted.tick,
        "the resumed run continues the persisted tick domain"
    );
    wait_tick(&successor, persisted.tick.0 + 3, "the resumed run");
    kill(&mut resumed);
    let _ = std::fs::remove_dir_all(&dir);
}

/// Leg 3: behavior without the flags is unchanged — a paced run with
/// no `--state-file` still stops on SIGTERM with exit 0, naming the
/// graceful shutdown and the absent state file.
#[test]
fn sigterm_without_a_state_file_still_exits_zero() {
    let dir = scratch("no-flags");
    let plant = spawn_plant(Path::new(PLANT_MODEL), Path::new(PLANT_DYNAMICS));

    let (mut active, _) = spawn_paced(Path::new(PLANT_MODEL), plant.addr, None, 50);
    let client = MonitorClient::new(active.addr);
    wait_tick(&client, 3, "the flagless paced run");

    signal(active.child.id(), "-TERM");
    let status = active.wait_exit(SHUTDOWN_BOUND).unwrap_or_else(|| {
        panic!("the flagless run did not stop within {SHUTDOWN_BOUND:?} of SIGTERM")
    });
    assert!(
        status.success(),
        "the graceful stop must exit 0, not {status:?}"
    );
    let stderr = active.stderr_tail();
    assert!(
        stderr.contains("graceful shutdown on SIGTERM")
            && stderr.contains("no state file configured"),
        "the stop must name the graceful shutdown without flags: {stderr:?}"
    );
    let _ = std::fs::remove_dir_all(&dir);
}

/// Leg 4: the second signal forces prompt exit — the escape hatch a
/// stalled sink's flush wait must not close. After the first scans
/// land, a reader-less FIFO is staged at the sink's write-then-rename
/// temporary sibling — the drain writer's next open() blocks inside
/// the mount while captures queue — so the first SIGTERM's flush
/// waits instead of finishing.
#[cfg(unix)]
#[test]
fn a_second_signal_forces_prompt_exit_past_a_stalled_flush() {
    let dir = scratch("double-signal");
    let plant = spawn_plant(Path::new(PLANT_MODEL), Path::new(PLANT_DYNAMICS));
    let state_path = dir.join("state.json");

    let (mut active, _) = spawn_paced(Path::new(PLANT_MODEL), plant.addr, Some(&state_path), 50);
    let client = MonitorClient::new(active.addr);
    wait_tick(&client, 3, "the paced run before the stall");

    // The stalled mount, staged once scans flow — never at the state
    // path itself, whose startup resume read would park on it.
    impede_tmp(&state_path);

    // The first SIGTERM is honored gracefully — the process stays up
    // flushing — and the second forces prompt exit with the
    // conventional 128+SIGTERM status, far inside the flush wait.
    signal(active.child.id(), "-TERM");
    std::thread::sleep(Duration::from_millis(500));
    assert!(
        active.child.try_wait().unwrap().is_none(),
        "the first SIGTERM must park in the stalled flush, not exit"
    );
    let started = Instant::now();
    signal(active.child.id(), "-TERM");
    let status = active.wait_exit(PROMPT_BOUND).unwrap_or_else(|| {
        panic!("the second signal did not force prompt exit within {PROMPT_BOUND:?}")
    });
    assert!(
        started.elapsed() < PROMPT_BOUND,
        "the prompt exit overran its bound"
    );
    assert_eq!(
        status.code(),
        Some(143),
        "the second SIGTERM must exit 143 (128+SIGTERM), not {status:?}"
    );
    drop(active);
    let _ = std::fs::remove_dir_all(&dir);
}

/// Stages a reader-less FIFO at the sink's write-then-rename temporary
/// sibling of `state_path` — the stall the QA lane stages on the
/// rig's bind mount: the drain writer's next open() blocks inside it
/// while captures queue. A capture's regular temporary in flight is
/// retried until its rename clears the path; an already-staged FIFO
/// is the idempotent hit.
#[cfg(unix)]
fn impede_tmp(state_path: &Path) {
    use std::os::unix::fs::FileTypeExt;
    let mut tmp = state_path.as_os_str().to_os_string();
    tmp.push(".tmp");
    let tmp = PathBuf::from(tmp);
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        if !tmp.exists() {
            nix_mkfifo(&tmp);
            return;
        }
        if std::fs::metadata(&tmp).is_ok_and(|meta| meta.file_type().is_fifo()) {
            return;
        }
        // A capture's regular temporary — its rename clears the path.
        assert!(
            Instant::now() < deadline,
            "a regular {} never cleared for the stall",
            tmp.display()
        );
        std::thread::sleep(Duration::from_millis(20));
    }
}

/// `mkfifo` through libc so the test needs no extra dependency.
#[cfg(unix)]
fn nix_mkfifo(path: &Path) {
    use std::ffi::CString;
    use std::os::unix::ffi::OsStrExt;
    let raw = CString::new(path.as_os_str().as_bytes()).unwrap();
    let created = unsafe { libc_mkfifo(raw.as_ptr(), 0o666) };
    assert_eq!(created, 0, "mkfifo {} failed", path.display());
}

#[cfg(unix)]
unsafe extern "C" {
    #[link_name = "mkfifo"]
    fn libc_mkfifo(path: *const std::os::raw::c_char, mode: u32) -> i32;
}

/// The `dcs-plant-ctl`-free evidence the legs leave behind: the
/// workspace binaries the spawns resolve.
#[test]
fn the_workspace_ships_the_binaries_the_legs_spawn() {
    assert!(workspace_binary("dcs-plant-server").is_file());
    assert!(Path::new(CONTROLLER).is_file());
}
