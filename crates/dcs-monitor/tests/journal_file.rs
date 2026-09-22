//! The durable journal file's single-writer contract at the monitor
//! bind: two monitors configured with the same `journal_file` path —
//! the shared-sink misconfiguration — cannot both run, because each
//! recorder would continue `seq` numbering from its own replay point
//! and interleave duplicate `seq`s into a record no later startup can
//! replay. The second bind fails naming the file and the live-holder
//! conflict; once the holder is gone the file replays for the next
//! writer, `seq` numbering continuing across the run boundary.

use dcs_core::{
    Command, Direction, IoDriver, IoError, JournalEvent, PointId, Sample, Tick, Value, ValueKind,
};
use dcs_model::{PlantModel, SignalIndex};
use dcs_monitor::{Monitor, MonitorClient, MonitorConfig, read_journal_file};
use dcs_runtime::{Executor, Peer, PointMap};
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Mutex;
use std::thread;

/// The same minimal in-memory driver the other monitor tests use.
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
        *sample = Sample::good(value, Tick::ZERO);
        Ok(())
    }
}

/// The model fixture behind the monitor — the same one the other
/// monitor tests serve.
const MODEL: &str = include_str!("../fixtures/monitor.json");

fn signal_index() -> SignalIndex {
    PlantModel::load(MODEL).unwrap().signal_index()
}

/// A scratch directory per test and process — tests run in parallel.
fn scratch(test: &str) -> PathBuf {
    let dir =
        std::env::temp_dir().join(format!("dcs-monitor-journal-{test}-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

#[test]
fn a_second_monitor_on_the_same_journal_file_fails_its_bind_naming_the_conflict() {
    let dir = scratch("shared-writer");
    let path = dir.join("journal.jsonl");
    let driver = StubDriver::new(&[(PointId(10), Value::Float(0.0))]);
    let bind = || {
        let map: PointMap = [(PointId(10), Direction::In, ValueKind::Float)]
            .into_iter()
            .collect();
        let executor = Executor::new(&driver, map, Vec::new()).unwrap();
        Monitor::bind_with(
            "127.0.0.1:0",
            executor,
            signal_index(),
            MonitorConfig {
                journal_file: Some(path.clone()),
                ..MonitorConfig::default()
            },
        )
    };

    // The first writer binds and scans — one first-observation
    // transition journals. A second monitor on the same path must not
    // start: its recorder's own replay point would interleave
    // duplicate seqs into the durable record, so the bind fails
    // naming the file and the live-holder conflict.
    let first = bind().unwrap();
    first.paced_scan();
    let error = match bind() {
        Ok(_) => panic!("two monitors on one journal file must not both bind"),
        Err(error) => error,
    };
    let message = error.to_string();
    assert!(message.contains(path.to_str().unwrap()), "{message}");
    assert!(message.contains("writer lock"), "{message}");

    // The failed bind left the holder undisturbed — it still scans and
    // appends. Once the holder drops — the process-lifetime lock
    // releasing with its descriptor — the next writer replays the
    // single-writer file and continues the seq domain across the run
    // boundary: the restart the misconfiguration would have corrupted.
    first.paced_scan();
    drop(first);
    let second = bind().unwrap();
    second.paced_scan();
    let data = read_journal_file(&path).unwrap();
    assert_eq!(
        data.entries
            .iter()
            .map(|entry| entry.seq)
            .collect::<Vec<_>>(),
        (1..=data.entries.len() as u64).collect::<Vec<_>>(),
        "a single-writer file replays with contiguous seqs"
    );
    assert_eq!(data.boundaries.len(), 2);
    let _ = std::fs::remove_dir_all(&dir);
}

/// #623's regression: `run_boundary` entries are the served markers a
/// `GET /journal` consumer attributes entries to a process lifetime
/// with — a bounded tail that evicts them under ordinary event volume
/// merges two lifetimes invisibly, exactly what the marker exists to
/// prevent. A journal file holding two lifetimes, a flood of journaled
/// commands past the tail's bound, and a restart — whose replay must
/// recover the marker its own retained tail already lost — and
/// `GET /journal` still answers every served lifetime's boundary.
#[test]
fn served_run_boundaries_survive_a_flood_past_the_tail_bound() {
    let dir = scratch("pinned-boundary");
    let path = dir.join("journal.jsonl");
    let driver = StubDriver::new(&[(PointId(10), Value::Float(0.0))]);
    let bind = || {
        let map = PointMap::new().with_writable_point(PointId(10), Direction::In, ValueKind::Float);
        let executor = Executor::new(&driver, map, Vec::new()).unwrap();
        Monitor::bind_with(
            "127.0.0.1:0",
            executor,
            signal_index(),
            MonitorConfig {
                journal_file: Some(path.clone()),
                journal_capacity: 8,
                ..MonitorConfig::default()
            },
        )
        .unwrap()
    };
    let flood = |monitor: &Monitor| {
        thread::scope(|scope| {
            scope.spawn(|| monitor.serve());
            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                let client = MonitorClient::new(monitor.local_addr());
                // More journaled commands than the ring retains — the
                // reproduction's flood.
                for value in 0..16_u64 {
                    client
                        .command(&Command::WriteValue {
                            point: PointId(10),
                            kind: ValueKind::Float,
                            value: Value::Float(value as f64),
                        })
                        .unwrap();
                    client.advance(1).unwrap();
                }
                client
            }));
            monitor.shutdown();
            result.unwrap_or_else(|panic| std::panic::resume_unwind(panic))
        })
    };

    // Two earlier lifetimes on the file: run 1 journals its census;
    // run 2 journals its boundary and floods past the tail bound, so
    // its own marker already lies outside the file's retained replay
    // tail.
    let first = bind();
    first.paced_scan();
    drop(first);
    let second = bind();
    flood(&second);
    drop(second);

    // The third lifetime replays the file — run 2's boundary already
    // outside the retained tail — then floods again. `GET /journal`
    // must still answer both served lifetimes' markers.
    let third = bind();
    thread::scope(|scope| {
        scope.spawn(|| third.serve());
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let client = MonitorClient::new(third.local_addr());
            for value in 0..16_u64 {
                client
                    .command(&Command::WriteValue {
                        point: PointId(10),
                        kind: ValueKind::Float,
                        value: Value::Float(value as f64),
                    })
                    .unwrap();
                client.advance(1).unwrap();
            }
            let journal = client.journal(0).unwrap();
            let runs = journal
                .iter()
                .filter_map(|entry| match &entry.event {
                    JournalEvent::RunBoundary { run } => Some((entry.seq, *run)),
                    _ => None,
                })
                .collect::<Vec<_>>();
            assert_eq!(
                runs.iter().map(|&(_, run)| run).collect::<Vec<_>>(),
                vec![2, 3],
                "event volume must not evict the served run-boundary markers: {journal:?}"
            );
            assert!(
                journal.windows(2).all(|pair| pair[0].seq < pair[1].seq),
                "the served journal stays in seq order: {journal:?}"
            );
            // The tail is still bounded: the pinned markers answer
            // ahead of the retained window and the ordinary eviction
            // between them reads as the usual seq gap.
            let tail = &journal[runs.len()..];
            assert_eq!(tail.len(), 8);
            assert!(tail[0].seq > runs[1].0 + 1);
            // The `since` cursor still filters on seq — a consumer
            // after the last boundary sees only the retained tail.
            let since_boundary = client.journal(runs[1].0).unwrap();
            assert_eq!(
                since_boundary
                    .iter()
                    .map(|entry| entry.seq)
                    .collect::<Vec<_>>(),
                tail.iter().map(|entry| entry.seq).collect::<Vec<_>>()
            );
        }));
        third.shutdown();
        result.unwrap_or_else(|panic| std::panic::resume_unwind(panic));
    });
    drop(third);

    // The file itself was never in doubt: all three markers endure.
    let data = read_journal_file(&path).unwrap();
    assert_eq!(
        data.boundaries
            .iter()
            .map(|boundary| boundary.run)
            .collect::<Vec<_>>(),
        vec![1, 2, 3]
    );
    let _ = std::fs::remove_dir_all(&dir);
}

/// #726's regression: a tracking standby restarted onto a checkpoint
/// whose receipt window no longer covers a settlement its journal file
/// already recorded — the missing `--state-file` cold start —
/// re-journaled the adopted `command_settled` across the run boundary.
/// The durable file is the pair's one command audit trail — one
/// `command_settled` per settlement — so the replayed file's settled
/// fold must mark the journaled receipt already recorded, while a
/// settlement the file never saw still journals as this run's news.
#[test]
fn a_cold_restarted_standby_does_not_rejournal_the_adopted_settlement() {
    let dir = scratch("adopted-settled");
    let journal = dir.join("standby.jsonl");
    let driver = StubDriver::new(&[(PointId(10), Value::Float(0.0))]);
    let active_driver = StubDriver::new(&[(PointId(10), Value::Float(0.0))]);
    let map = || PointMap::new().with_writable_point(PointId(10), Direction::In, ValueKind::Float);
    let bind_standby = || {
        let executor = Executor::new(&driver, map(), Vec::new()).unwrap();
        Monitor::bind_peer_with(
            "127.0.0.1:0",
            Peer::standby(executor, None),
            signal_index(),
            MonitorConfig {
                journal_file: Some(journal.clone()),
                ..MonitorConfig::default()
            },
        )
        .unwrap()
    };
    let settlements = || {
        read_journal_file(&journal)
            .unwrap()
            .entries
            .iter()
            .filter(|entry| matches!(entry.event, JournalEvent::CommandSettled { .. }))
            .count()
    };

    // The tracked run: a write submitted to the field owner applies at
    // its next boundary — the receipt the pair's audit carries.
    let mut active = Peer::active(
        Executor::new(&active_driver, map(), Vec::new()).unwrap(),
        None,
    );
    active.scan();
    active.submit_command(Command::WriteValue {
        point: PointId(10),
        kind: ValueKind::Float,
        value: Value::Float(2.5),
    });
    active.scan();
    let checkpoint = active.checkpoint();

    // First lifetime: the standby converges on the checkpoint and its
    // first scan journals the adopted settlement — the file's one
    // record of it.
    let first = bind_standby();
    first.apply_checkpoint(&checkpoint).unwrap();
    first.paced_scan();
    assert_eq!(settlements(), 1);
    drop(first);

    // The restart onto a missing `--state-file`: a cold executor
    // behind the replayed journal file. The first post-restart
    // adoption carries the same settled receipt — first observed by
    // the empty receipt baseline, it must not journal a second time.
    let second = bind_standby();
    second.apply_checkpoint(&checkpoint).unwrap();
    second.paced_scan();
    assert_eq!(
        settlements(),
        1,
        "the journaled settlement must not re-record across the run boundary"
    );
    assert_eq!(read_journal_file(&journal).unwrap().boundaries.len(), 2);

    // A settlement the file never saw still journals: the next adopted
    // command is this run's news, not the replay fold's.
    active.submit_command(Command::WriteValue {
        point: PointId(10),
        kind: ValueKind::Float,
        value: Value::Float(7.5),
    });
    active.scan();
    second.apply_checkpoint(&active.checkpoint()).unwrap();
    second.paced_scan();
    assert_eq!(settlements(), 2);
    drop(second);

    let _ = std::fs::remove_dir_all(&dir);
}

/// #726's sibling path: a standby restarted with its `--state-file`
/// intact — the restored checkpoint's receipt window covering the
/// journaled settlement — must not re-journal either: the restored
/// receipts are marked observed at bind, and the fold's accounting
/// leaves a settlement the file never saw free to journal.
#[test]
fn a_state_restored_standby_still_journals_only_new_settlements() {
    let dir = scratch("restored-settled");
    let journal = dir.join("standby.jsonl");
    let driver = StubDriver::new(&[(PointId(10), Value::Float(0.0))]);
    let active_driver = StubDriver::new(&[(PointId(10), Value::Float(0.0))]);
    let map = || PointMap::new().with_writable_point(PointId(10), Direction::In, ValueKind::Float);
    let settlements = || {
        read_journal_file(&journal)
            .unwrap()
            .entries
            .iter()
            .filter(|entry| matches!(entry.event, JournalEvent::CommandSettled { .. }))
            .count()
    };

    // The tracked run settles the write the pair audits.
    let mut active = Peer::active(
        Executor::new(&active_driver, map(), Vec::new()).unwrap(),
        None,
    );
    active.scan();
    active.submit_command(Command::WriteValue {
        point: PointId(10),
        kind: ValueKind::Float,
        value: Value::Float(2.5),
    });
    active.scan();

    // First lifetime: adopt, journal, and persist — the standby's own
    // checkpoint is the `--state-file` its restart resumes from.
    let first = {
        let executor = Executor::new(&driver, map(), Vec::new()).unwrap();
        Monitor::bind_peer_with(
            "127.0.0.1:0",
            Peer::standby(executor, None),
            signal_index(),
            MonitorConfig {
                journal_file: Some(journal.clone()),
                ..MonitorConfig::default()
            },
        )
        .unwrap()
    };
    first.apply_checkpoint(&active.checkpoint()).unwrap();
    first.paced_scan();
    assert_eq!(settlements(), 1);
    let persisted = first.checkpoint();
    drop(first);

    // The restart resumes the persisted checkpoint — the restored
    // receipt log is marked observed at bind — and reconverges: the
    // journaled settlement must not re-record.
    let restored = {
        let mut executor = Executor::new(&driver, map(), Vec::new()).unwrap();
        executor.apply(&persisted).unwrap();
        Monitor::bind_peer_with(
            "127.0.0.1:0",
            Peer::standby(executor, None),
            signal_index(),
            MonitorConfig {
                journal_file: Some(journal.clone()),
                ..MonitorConfig::default()
            },
        )
        .unwrap()
    };
    restored.apply_checkpoint(&active.checkpoint()).unwrap();
    restored.paced_scan();
    assert_eq!(settlements(), 1);

    // The next settlement the file never saw still journals.
    active.submit_command(Command::WriteValue {
        point: PointId(10),
        kind: ValueKind::Float,
        value: Value::Float(7.5),
    });
    active.scan();
    restored.apply_checkpoint(&active.checkpoint()).unwrap();
    restored.paced_scan();
    assert_eq!(settlements(), 2);
    drop(restored);

    let _ = std::fs::remove_dir_all(&dir);
}
