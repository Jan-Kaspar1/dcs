//! Restart recovery through `--state-file`: a lone controller's run
//! persists its serde `Checkpoint` to a file at the end of every scan
//! cycle, and a restarted binary resumes from it — the no-peer recovery
//! path the state-file decision records. The scripted runs below spawn
//! the binary itself, deterministic like every `--ticks` run.

use dcs_core::{Command, CommandOutcome, PointId, TelemetrySnapshot, Tick, Value, ValueKind};
use dcs_monitor::MonitorClient;
use dcs_runtime::{CHECKPOINT_FORMAT_VERSION, Checkpoint};
use std::path::{Path, PathBuf};
use std::process::Command as Process;

mod support;

use support::{Spawned, image_value, kill, listening_on, spawn};

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

/// Rewrites `value`'s canonical `snake_case` enum spellings to the
/// PascalCase spellings pre-change builds emitted — the shape a legacy
/// `--state-file` holds. Only the audited enums' names are rewritten;
/// every other key and string passes through untouched.
fn legacy_spellings(value: &mut serde_json::Value) {
    const PASCAL: &[(&str, &str)] = &[
        ("bool", "Bool"),
        ("int", "Int"),
        ("float", "Float"),
        ("good", "Good"),
        ("uncertain", "Uncertain"),
        ("bad", "Bad"),
        ("unspecified", "Unspecified"),
        ("substituted", "Substituted"),
        ("stale", "Stale"),
        ("out_of_range", "OutOfRange"),
        ("communication_fault", "CommunicationFault"),
        ("device_fault", "DeviceFault"),
        ("configuration_fault", "ConfigurationFault"),
        ("unknown_point", "UnknownPoint"),
        ("disconnected", "Disconnected"),
        ("timeout", "Timeout"),
        ("type_mismatch", "TypeMismatch"),
        ("fenced", "Fenced"),
    ];
    match value {
        serde_json::Value::String(name) => {
            if let Some((_, legacy)) = PASCAL.iter().find(|(snake, _)| snake == name) {
                *name = (*legacy).to_string();
            }
        }
        serde_json::Value::Array(items) => {
            for item in items {
                legacy_spellings(item);
            }
        }
        serde_json::Value::Object(map) => {
            // An externally tagged variant's single key — `{"float": …}`,
            // `{"uncertain": …}` — renames like the bare strings.
            if map.len() == 1 {
                let key = map.keys().next().unwrap().clone();
                if let Some((_, legacy)) = PASCAL.iter().find(|(snake, _)| *snake == key) {
                    let inner = map.remove(&key).unwrap();
                    map.insert((*legacy).to_string(), inner);
                }
            }
            for inner in map.values_mut() {
                legacy_spellings(inner);
            }
        }
        _ => {}
    }
}

#[test]
fn a_legacy_spelling_state_file_resumes_through_the_aliases() {
    // The uninterrupted reference: one run of WHOLE ticks.
    let reference = run(&[TANK_LOOP, "--ticks", &WHOLE.to_string()]);
    assert!(reference.status.success());

    // Capture a canonical checkpoint at HALF, then rewrite it to the
    // PascalCase spellings a pre-snake_case build persisted — the file
    // the same run would have produced under the old contract.
    let dir = scratch("legacy");
    let state = dir.join("state.json");
    let first = run(&[
        TANK_LOOP,
        "--ticks",
        &HALF.to_string(),
        "--state-file",
        state.to_str().unwrap(),
    ]);
    assert!(first.status.success());
    let mut document: serde_json::Value =
        serde_json::from_slice(&std::fs::read(&state).unwrap()).unwrap();
    legacy_spellings(&mut document);
    let legacy = serde_json::to_string(&document).unwrap();
    assert!(legacy.contains("\"Float\""), "{legacy}");
    std::fs::write(&state, legacy).unwrap();

    // The aliases read it back: the resumed run produces the identical
    // continuation the canonical file would have.
    let resumed = run(&[
        TANK_LOOP,
        "--ticks",
        &HALF.to_string(),
        "--state-file",
        state.to_str().unwrap(),
    ]);
    assert!(resumed.status.success());
    let stderr = String::from_utf8(resumed.stderr).unwrap();
    assert!(stderr.contains("resumed from state file"), "{stderr}");
    assert_eq!(resumed.stdout, reference.stdout);
    assert_eq!(persisted(&state).tick, Tick(WHOLE));

    let _ = std::fs::remove_dir_all(&dir);
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

/// Spawns the controller and reads stderr until its `listening on`
/// line — a resumed process reports the resume first.
fn spawn_driven(args: &[String]) -> Spawned {
    spawn(Path::new(BINARY), args, listening_on)
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
    kill(&mut first);
    assert_eq!(persisted(&state).tick, Tick(HALF));

    let resumed = spawn_driven(&args(true));
    let continued = MonitorClient::new(resumed.addr).advance(HALF).unwrap();
    assert_eq!(continued, reference);

    let _ = std::fs::remove_dir_all(&dir);
}

/// The restart-durability regression: a command accepted between the
/// last cycle-end state-file write and the restart used to evaporate —
/// `/receipts` empty after resume, the value never applied, the journal
/// silent. The admission boundary now persists the checkpoint, so the
/// carried `Accepted` receipt re-queues on the resumed run and settles
/// applied at the promised tick.
#[test]
fn an_accepted_command_survives_a_restart_before_its_apply_tick() {
    let dir = scratch("admitted");
    let journal = dir.join("journal.jsonl");
    let state = dir.join("state.json");
    let args = || {
        vec![
            TANK_LOOP.to_string(),
            "--listen".to_string(),
            "127.0.0.1:0".to_string(),
            "--driven".to_string(),
            "--state-file".to_string(),
            state.to_str().unwrap().to_string(),
            "--journal-file".to_string(),
            journal.to_str().unwrap().to_string(),
        ]
    };
    let command = Command::WriteValue {
        point: PointId(10),
        kind: ValueKind::Float,
        value: Value::Float(2.5),
    };

    // Three requested scans, then the write — accepted, promised
    // apply_tick 4 — and the process dies before that scan ever runs.
    let mut first = spawn_driven(&args());
    let client = MonitorClient::new(first.addr);
    client.advance(3).unwrap();
    let receipt = client.command(&command).unwrap();
    assert_eq!(
        receipt.outcome,
        CommandOutcome::Accepted {
            apply_tick: Tick(4)
        }
    );
    kill(&mut first);

    // The admission persisted at its boundary: the state file holds the
    // still-Accepted receipt at the tick the run reached.
    let checkpoint = persisted(&state);
    assert_eq!(checkpoint.tick, Tick(3));
    assert_eq!(
        checkpoint.receipts.last().map(|receipt| &receipt.outcome),
        Some(&CommandOutcome::Accepted {
            apply_tick: Tick(4)
        }),
        "{checkpoint:?}"
    );

    // The resumed run re-queues the carried receipt — `/receipts`
    // answers it before any scan — and the next scan applies it at the
    // promised tick: the point takes the written value, the receipt
    // settles applied, and the durable journal records the settlement
    // across the restart.
    let second = spawn_driven(&args());
    let client = MonitorClient::new(second.addr);
    let receipts = client.receipts().unwrap();
    assert_eq!(
        receipts.last().map(|receipt| &receipt.outcome),
        Some(&CommandOutcome::Accepted {
            apply_tick: Tick(4)
        }),
        "{receipts:?}"
    );
    let snapshot = client.advance(1).unwrap();
    assert_eq!(snapshot.tick, Tick(4));
    assert_eq!(image_value(&snapshot, PointId(10)), Value::Float(2.5));
    let receipts = client.receipts().unwrap();
    assert_eq!(
        receipts.last().map(|receipt| &receipt.outcome),
        Some(&CommandOutcome::Applied { tick: Tick(4) }),
        "{receipts:?}"
    );
    assert!(
        client.journal(0).unwrap().iter().any(|entry| matches!(
            &entry.event,
            dcs_core::JournalEvent::CommandSettled { receipt }
                if receipt.command == command
                    && receipt.outcome == CommandOutcome::Applied { tick: Tick(4) }
        )),
        "the settled command must be journaled across the restart"
    );
    let file = std::fs::read_to_string(&journal).unwrap();
    assert!(file.contains("command_settled"), "{file}");

    let _ = std::fs::remove_dir_all(&dir);
}
