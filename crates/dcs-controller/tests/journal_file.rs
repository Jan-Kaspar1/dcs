//! The `--journal-file` durable transition journal: a configured path
//! receives every journaled entry as a line-delimited JSON record, a
//! restarted process replays the file so `GET /journal` answers
//! continuously across the restart with `seq` numbering continued, and
//! a `--state-file`-resumed run keeps appending in the restored tick
//! domain — the journal-persistence decision. The scripted runs below
//! spawn the binary itself under `--driven`, deterministic like every
//! request-paced run.

use dcs_core::{Command, JournalEntry, PointId, Tick, Value, ValueKind};
use dcs_monitor::MonitorClient;
use std::path::{Path, PathBuf};
use std::process::Command as Process;

mod support;

use support::{Spawned, kill, listening_on, spawn};

const BINARY: &str = env!("CARGO_BIN_EXE_dcs-controller");
/// The shared tank loop: point 10 is a model-declared writable `In`
/// point, so a `write_value` command settles through the receipted
/// path into the journal.
const TANK_LOOP: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-assembly/fixtures/tank_loop.json"
);

/// Scans the first half of an interrupted run requests before the
/// restart; the resumed run then requests that many again.
const HALF: u64 = 6;

fn run(args: &[&str]) -> std::process::Output {
    Process::new(BINARY).args(args).output().unwrap()
}

/// A scratch directory per test and process — tests run in parallel.
fn scratch(test: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("dcs-journal-file-{test}-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

/// The file's `entry` records, in file order, decoded back into the
/// contract's `JournalEntry` — marker lines are skipped.
fn file_entries(path: &Path) -> Vec<JournalEntry> {
    std::fs::read_to_string(path)
        .unwrap()
        .lines()
        .filter_map(|line| {
            serde_json::from_str::<serde_json::Value>(line)
                .unwrap()
                .get("entry")
                .map(|entry| serde_json::from_value(entry.clone()).unwrap())
        })
        .collect()
}

/// The file's `run_boundary` marker records as `(run, tick)` pairs.
fn file_boundaries(path: &Path) -> Vec<(u64, u64)> {
    std::fs::read_to_string(path)
        .unwrap()
        .lines()
        .filter_map(|line| {
            let record: serde_json::Value = serde_json::from_str(line).unwrap();
            record.get("run_boundary").map(|marker| {
                (
                    marker["run"].as_u64().unwrap(),
                    marker["tick"].as_u64().unwrap(),
                )
            })
        })
        .collect()
}

/// Spawns the controller and reads stderr until its `listening on`
/// line — a resumed process reports the resume first.
fn spawn_driven(args: &[String]) -> Spawned {
    spawn(Path::new(BINARY), args, listening_on)
}

/// The journaled-points rig: point 10 is a writable journaled `In`
/// point — the receipted-write shape — and point 20 a journaled
/// internal `Out` status point a `digital-output` component mirrors
/// from the scripted, unjournaled point 11 — the component-driven
/// shape.
const JOURNALED_POINTS: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-assembly/fixtures/journaled_points.json"
);

/// The driven-mode arguments: `--journal-file` always, `--state-file`
/// when `state` names a checkpoint path.
fn driven_args(journal: &Path, state: Option<&Path>) -> Vec<String> {
    driven_args_for(TANK_LOOP, journal, state)
}

/// As [`driven_args`] for an explicit model — the journaled rig shares
/// the same spawning harness.
fn driven_args_for(model: &str, journal: &Path, state: Option<&Path>) -> Vec<String> {
    let mut args = vec![
        model.to_string(),
        "--listen".to_string(),
        "127.0.0.1:0".to_string(),
        "--driven".to_string(),
        "--journal-file".to_string(),
        journal.to_str().unwrap().to_string(),
    ];
    if let Some(state) = state {
        args.extend([
            "--state-file".to_string(),
            state.to_str().unwrap().to_string(),
        ]);
    }
    args
}

/// The scripted run's operator action: a setpoint write to the
/// model-declared writable point 10, settled at the next scan boundary.
fn setpoint_write() -> Command {
    Command::WriteValue {
        point: PointId(10),
        kind: ValueKind::Float,
        value: Value::Float(2.5),
    }
}

#[test]
fn journaled_entries_land_in_the_file_and_replay_across_a_restart() {
    let dir = scratch("restart");
    let journal = dir.join("journal.jsonl");

    // First lifetime: three scans of quality transitions, then a
    // command settled by a fourth.
    let mut first = spawn_driven(&driven_args(&journal, None));
    let client = MonitorClient::new(first.addr);
    client.advance(HALF / 2).unwrap();
    client.command(&setpoint_write()).unwrap();
    client.advance(1).unwrap();
    let before_restart = client.journal(0).unwrap();
    assert!(before_restart.len() >= 4, "{before_restart:?}");
    kill(&mut first);

    // Every served entry landed in the file in order, behind the
    // run-1 boundary marker.
    let entries = file_entries(&journal);
    assert_eq!(entries, before_restart);
    assert_eq!(file_boundaries(&journal), vec![(1, 0)]);

    // The restarted process replays the file: the same entries answer
    // GET /journal with their seqs, the run-2 marker separates the
    // lifetimes in the file, and new entries continue the numbering.
    let second = spawn_driven(&driven_args(&journal, None));
    let client = MonitorClient::new(second.addr);
    assert_eq!(client.journal(0).unwrap(), before_restart);
    client.advance(HALF / 2).unwrap();
    let after_restart = client.journal(0).unwrap();
    assert!(after_restart.len() > before_restart.len());
    assert_eq!(&after_restart[..before_restart.len()], &before_restart[..]);
    let last_seq = before_restart.last().unwrap().seq;
    assert_eq!(after_restart[before_restart.len()].seq, last_seq + 1);

    let entries = file_entries(&journal);
    assert_eq!(entries, after_restart);
    assert_eq!(file_boundaries(&journal), vec![(1, 0), (2, 0)]);

    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_state_file_resumed_run_appends_in_the_restored_tick_domain() {
    let dir = scratch("resumed");
    let journal = dir.join("journal.jsonl");
    let state = dir.join("state.json");

    // The interrupted run: HALF requested scans, then the process dies.
    let mut first = spawn_driven(&driven_args(&journal, Some(&state)));
    let client = MonitorClient::new(first.addr);
    client.advance(HALF).unwrap();
    let before_restart = client.journal(0).unwrap();
    assert!(
        before_restart.iter().all(|entry| entry.tick <= Tick(HALF)),
        "{before_restart:?}"
    );
    kill(&mut first);

    // The state file resumes the run at tick HALF; the journal file
    // keeps appending — the boundary marker records the restored tick,
    // and post-restart entries live in the restored tick domain with
    // continuing seqs.
    let second = spawn_driven(&driven_args(&journal, Some(&state)));
    let client = MonitorClient::new(second.addr);
    assert_eq!(client.journal(0).unwrap(), before_restart);
    assert_eq!(client.snapshot().unwrap().tick, Tick(HALF));
    client.advance(HALF).unwrap();

    let after_restart = client.journal(0).unwrap();
    assert!(after_restart.len() > before_restart.len());
    assert_eq!(
        after_restart[before_restart.len()].seq,
        before_restart.last().unwrap().seq + 1
    );
    assert!(
        after_restart[before_restart.len()..]
            .iter()
            .all(|entry| entry.tick > Tick(HALF)),
        "{after_restart:?}"
    );
    assert_eq!(file_boundaries(&journal), vec![(1, 0), (2, HALF)]);
    assert_eq!(file_entries(&journal), after_restart);

    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_legacy_spelling_journal_file_replays_through_the_aliases() {
    let dir = scratch("legacy");
    let journal = dir.join("journal.jsonl");

    // The file a pre-snake_case build wrote verbatim: entries carrying
    // Quality, QualityReason, ValueKind, and Value in their PascalCase
    // spellings — a quality transition and a settled write.
    let lines = [
        r#"{"run_boundary":{"run":1,"tick":0}}"#,
        r#"{"entry":{"seq":1,"tick":1,"event":{"quality_changed":{"point":10,"from":null,"to":{"Uncertain":"Substituted"}}}}}"#,
        r#"{"entry":{"seq":2,"tick":2,"event":{"quality_changed":{"point":10,"from":{"Uncertain":"Substituted"},"to":"Good"}}}}"#,
        r#"{"entry":{"seq":3,"tick":3,"event":{"command_settled":{"receipt":{"command":{"write_value":{"point":10,"kind":"Float","value":{"Float":2.5}}},"outcome":{"applied":{"tick":3}}}}}}}"#,
    ];
    std::fs::write(&journal, lines.join("\n") + "\n").unwrap();

    // Replay accepts every legacy spelling through the aliases: the
    // entries answer GET /journal identically to their canonical form,
    // and new entries continue the numbering behind a run-2 marker.
    let spawned = spawn_driven(&driven_args(&journal, None));
    let client = MonitorClient::new(spawned.addr);
    let replayed = client.journal(0).unwrap();
    assert_eq!(
        replayed
            .iter()
            .map(|entry| (entry.seq, &entry.event))
            .collect::<Vec<_>>(),
        vec![
            (
                1,
                &dcs_core::JournalEvent::QualityChanged {
                    point: PointId(10),
                    from: None,
                    to: dcs_core::Quality::Uncertain(dcs_core::QualityReason::Substituted),
                }
            ),
            (
                2,
                &dcs_core::JournalEvent::QualityChanged {
                    point: PointId(10),
                    from: Some(dcs_core::Quality::Uncertain(
                        dcs_core::QualityReason::Substituted
                    )),
                    to: dcs_core::Quality::Good,
                }
            ),
            (
                3,
                &dcs_core::JournalEvent::CommandSettled {
                    receipt: dcs_core::CommandReceipt {
                        command: Command::WriteValue {
                            point: PointId(10),
                            kind: ValueKind::Float,
                            value: Value::Float(2.5),
                        },
                        outcome: dcs_core::CommandOutcome::Applied { tick: Tick(3) },
                        actor: None,
                    },
                }
            ),
        ]
    );
    client.advance(1).unwrap();
    let after = client.journal(0).unwrap();
    assert!(after.len() > replayed.len(), "{after:?}");
    assert_eq!(&after[..replayed.len()], &replayed[..]);
    assert_eq!(after[replayed.len()].seq, 4);
    assert_eq!(file_boundaries(&journal), vec![(1, 0), (2, 0)]);

    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_corrupt_journal_file_fails_startup_naming_the_file_and_record() {
    let dir = scratch("corrupt");
    let journal = dir.join("journal.jsonl");

    // One intact run gives the file a valid marker and entries.
    let mut first = spawn_driven(&driven_args(&journal, None));
    MonitorClient::new(first.addr).advance(2).unwrap();
    kill(&mut first);

    // A torn trailing record — the crash-mid-write shape — refuses the
    // restart, naming the file and the offending record.
    let torn = "{\"entry\":{\"seq\":4,\"tick\":2,";
    let mut body = std::fs::read_to_string(&journal).unwrap();
    body.push_str(torn);
    body.push('\n');
    std::fs::write(&journal, body).unwrap();
    let args = driven_args(&journal, None);
    let args: Vec<&str> = args.iter().map(String::as_str).collect();
    let output = run(&args);
    assert!(!output.status.success());
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(stderr.contains(journal.to_str().unwrap()), "{stderr}");
    assert!(stderr.contains(torn), "{stderr}");

    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_receipted_write_and_a_component_transition_journal_side_by_side() {
    let dir = scratch("journaled");
    let journal = dir.join("journal.jsonl");

    // First lifetime — the first scan journals the declared points'
    // first observed samples (`from: None` on the two journaled
    // points). The receipted write to journaled writable point 10 then
    // lands its `CommandSettled` and the resulting `PointChanged` at
    // the applying scan — the action's attribution and the transition
    // coexisting in `seq` order.
    let mut first = spawn_driven(&driven_args_for(JOURNALED_POINTS, &journal, None));
    let client = MonitorClient::new(first.addr);
    client.advance(1).unwrap();
    let receipt = client
        .command(&Command::WriteValue {
            point: PointId(10),
            kind: ValueKind::Bool,
            value: Value::Bool(true),
        })
        .unwrap();
    client.advance(1).unwrap();

    // Scan 3 flips scripted point 11 — unjournaled, so no value entry —
    // and the digital-output component mirrors it onto journaled status
    // point 20: a component-driven transition with no command and so
    // no receipt.
    client.advance(1).unwrap();

    let before_restart = client.journal(0).unwrap();
    let settled = before_restart
        .iter()
        .position(|entry| matches!(entry.event, dcs_core::JournalEvent::CommandSettled { .. }))
        .expect("the write settled");
    assert_eq!(
        before_restart[settled].event,
        dcs_core::JournalEvent::CommandSettled {
            receipt: dcs_core::CommandReceipt {
                command: receipt.command,
                outcome: dcs_core::CommandOutcome::Applied { tick: Tick(2) },
                actor: None,
            },
        }
    );
    assert_eq!(
        before_restart[settled + 1].event,
        dcs_core::JournalEvent::PointChanged {
            point: PointId(10),
            from: Some(Value::Bool(false)),
            to: Value::Bool(true),
        }
    );
    assert_eq!(before_restart[settled + 1].tick, Tick(2));
    assert!(
        before_restart
            .iter()
            .filter(|entry| {
                matches!(entry.event, dcs_core::JournalEvent::CommandSettled { .. })
            })
            .count()
            == 1,
        "{before_restart:?}"
    );
    let status_transition = before_restart
        .iter()
        .find(|entry| {
            matches!(
                entry.event,
                dcs_core::JournalEvent::PointChanged {
                    point,
                    to: Value::Bool(true),
                    ..
                } if point == PointId(20)
            )
        })
        .expect("the component-driven status transition is journaled");
    assert_eq!(status_transition.tick, Tick(3));
    assert!(
        before_restart.iter().all(|entry| !matches!(
            entry.event,
            dcs_core::JournalEvent::PointChanged { point, .. } if point == PointId(11)
        )),
        "{before_restart:?}"
    );
    kill(&mut first);

    // Every served entry — receipts and `point_changed` transitions
    // alike — landed in the file in order behind the run-1 marker, and
    // a restarted process replays them with `seq` numbering continued.
    assert_eq!(file_entries(&journal), before_restart);
    let second = spawn_driven(&driven_args_for(JOURNALED_POINTS, &journal, None));
    let client = MonitorClient::new(second.addr);
    assert_eq!(client.journal(0).unwrap(), before_restart);
    client.advance(1).unwrap();
    let after_restart = client.journal(0).unwrap();
    assert_eq!(&after_restart[..before_restart.len()], &before_restart[..]);
    assert_eq!(
        after_restart[before_restart.len()].seq,
        before_restart.last().unwrap().seq + 1
    );
    assert_eq!(file_entries(&journal), after_restart);
    assert_eq!(file_boundaries(&journal), vec![(1, 0), (2, 0)]);

    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn identical_scripted_runs_produce_identical_files() {
    let scripted = |dir: &Path| {
        let journal = dir.join("journal.jsonl");
        let spawned = spawn_driven(&driven_args(&journal, None));
        let client = MonitorClient::new(spawned.addr);
        client.advance(3).unwrap();
        client.command(&setpoint_write()).unwrap();
        client.advance(2).unwrap();
        client.command(&setpoint_write()).unwrap();
        client.advance(1).unwrap();
        let served = client.journal(0).unwrap();
        let file = std::fs::read(&journal).unwrap();
        (file, served)
    };

    let first_dir = scratch("deterministic-a");
    let second_dir = scratch("deterministic-b");
    let (first_file, first_served) = scripted(&first_dir);
    let (second_file, second_served) = scripted(&second_dir);
    assert_eq!(first_file, second_file);
    assert_eq!(first_served, second_served);

    let _ = std::fs::remove_dir_all(&first_dir);
    let _ = std::fs::remove_dir_all(&second_dir);
}
