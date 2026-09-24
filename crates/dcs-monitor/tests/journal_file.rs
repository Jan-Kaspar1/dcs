//! The durable journal file's single-writer contract at the monitor
//! bind: two monitors configured with the same `journal_file` path —
//! the shared-sink misconfiguration — cannot both run, because each
//! recorder would continue `seq` numbering from its own replay point
//! and interleave duplicate `seq`s into a record no later startup can
//! replay. The second bind fails naming the file and the live-holder
//! conflict; once the holder is gone the file replays for the next
//! writer, `seq` numbering continuing across the run boundary.

use dcs_core::{
    Command, Direction, IoDriver, IoError, JournalEntry, JournalEvent, PointHistory, PointId,
    Sample, Tick, Value, ValueKind,
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

/// Reads `point`'s served history envelopes — the whole ring plus the
/// `since`-filtered page a cursor consumer polls — and the served
/// journal while `monitor` serves. One serving session per call: a
/// monitor's `serve` runs once, so both reads share it.
fn served(
    monitor: &Monitor<'_>,
    point: PointId,
    since: u64,
) -> (PointHistory, PointHistory, Vec<JournalEntry>) {
    thread::scope(|scope| {
        scope.spawn(|| monitor.serve());
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let client = MonitorClient::new(monitor.local_addr());
            let history = |since: u64| {
                client
                    .history(&[point], since)
                    .unwrap()
                    .into_iter()
                    .find(|history| history.point == point)
                    .expect("the point's history envelope answers")
            };
            (history(0), history(since), client.journal(0).unwrap())
        }));
        monitor.shutdown();
        result.unwrap_or_else(|panic| std::panic::resume_unwind(panic))
    })
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

/// #884's regression, the tick-domain-continuing half: `/history` seqs
/// rode a per-process append count, so a restart onto the run's
/// continuing tick domain — the checkpoint-adopted standby — renumbered
/// samples from 1 and a `since` cursor read the restart as phantom idle
/// before stitching two process lifetimes contiguously. The seq axis
/// now rides the tick domain itself: the restarted run's samples
/// continue numbering from it, the never-served stretch answering as
/// the numbering gap it is, and the envelope's `run` marker — the
/// journal file's run count — advances with the process lifetime.
#[test]
fn a_restart_continuing_the_tick_domain_keeps_the_history_seq_axis() {
    let dir = scratch("history-seq-continues");
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

    // The tracked line the standby follows.
    let mut active = Peer::active(
        Executor::new(&active_driver, map(), Vec::new()).unwrap(),
        None,
    );
    for _ in 0..3 {
        active.scan();
    }

    // First lifetime: adopt the line's checkpoint — the tick domain the
    // ring's seqs ride — then scan on it. Seqs and ticks share the
    // adopted domain: the served tail ends at the cursor the restart
    // read must honor.
    let first = bind_standby();
    first.apply_checkpoint(&active.checkpoint()).unwrap();
    for _ in 0..3 {
        first.paced_scan();
    }
    let (before, _, _) = served(&first, PointId(10), 0);
    assert_eq!(before.run, 1);
    let cursor = before.samples.last().unwrap().seq;
    assert_eq!(
        before
            .samples
            .iter()
            .map(|sample| (sample.seq, sample.sample.tick))
            .collect::<Vec<_>>(),
        vec![(4, Tick(4)), (5, Tick(5)), (6, Tick(6))],
        "the first lifetime's seqs ride the adopted tick domain"
    );
    drop(first);

    // The line advances while the standby is down — the stretch the
    // restarted run never serves.
    for _ in 0..4 {
        active.scan();
    }

    // Second lifetime on the same journal file: the adoption resumes
    // the tick domain at the line's current mark, so the restarted
    // ring's seqs continue past the pre-restart cursor rather than
    // renumbering from 1.
    let second = bind_standby();
    second.apply_checkpoint(&active.checkpoint()).unwrap();
    for _ in 0..3 {
        second.paced_scan();
    }
    // The defect's `since` read alongside the whole ring: the
    // pre-restart cursor answers the new run's samples — the unserved
    // stretch surfacing as the numbering gap it is — rather than the
    // empty, idle-looking page that let two lifetimes stitch
    // contiguously.
    let (after, increment, journal) = served(&second, PointId(10), cursor);
    assert_eq!(after.run, 2);
    let seqs: Vec<u64> = after.samples.iter().map(|sample| sample.seq).collect();
    assert!(
        seqs.iter().all(|&seq| seq > cursor),
        "the restarted run's seqs continue the tick domain past the \
         pre-restart cursor {cursor}: {seqs:?}"
    );
    assert_eq!(increment.run, 2);
    let first_seq = increment.samples.first().unwrap().seq;
    assert_eq!(first_seq, seqs[0]);
    assert!(
        first_seq > cursor + 1,
        "seqs {cursor}..{first_seq} were never served — the cursor read \
         must surface the gap, not empty-then-resume"
    );
    // The journal's own attribution of the same seam: run 2's boundary
    // is served — the marker the page's restart observation reads.
    assert!(
        journal
            .iter()
            .any(|entry| matches!(entry.event, JournalEvent::RunBoundary { run: 2 })),
        "the served journal carries run 2's boundary: {journal:?}"
    );
    drop(second);

    let _ = std::fs::remove_dir_all(&dir);
}

/// #884's regression, the cold-restart half: a restart onto a fresh
/// tick domain — no checkpoint, no `--state-file` — legitimately
/// renumbers the history seq axis with the domain's own restart. What
/// makes the seam detectable is the envelope: every served
/// `PointHistory` stamps the run's lifetime ordinal, so the
/// `since`-cursor read the restarted axis still filters to empty
/// answers with `run` advanced — attributable on the empty page itself
/// rather than indistinguishable from idle.
#[test]
fn a_cold_restart_stamps_the_new_run_on_every_served_history_envelope() {
    let dir = scratch("history-run-marker");
    let journal = dir.join("monitor.jsonl");
    let driver = StubDriver::new(&[(PointId(10), Value::Float(0.0))]);
    let bind = || {
        let map = PointMap::new().with_writable_point(PointId(10), Direction::In, ValueKind::Float);
        let executor = Executor::new(&driver, map, Vec::new()).unwrap();
        Monitor::bind_with(
            "127.0.0.1:0",
            executor,
            signal_index(),
            MonitorConfig {
                journal_file: Some(journal.clone()),
                ..MonitorConfig::default()
            },
        )
        .unwrap()
    };

    // First lifetime: three scans of ring history behind run 1's
    // envelopes.
    let first = bind();
    for _ in 0..3 {
        first.paced_scan();
    }
    let (before, _, _) = served(&first, PointId(10), 0);
    assert_eq!(before.run, 1);
    let cursor = before.samples.last().unwrap().seq;
    drop(first);

    // The cold restart onto the same journal file: a fresh tick domain,
    // so the new run's ring legitimately renumbers with its own ticks —
    // but every envelope carries the advanced `run`, the marker a
    // cursor consumer compares across polls.
    let second = bind();
    for _ in 0..2 {
        second.paced_scan();
    }
    let (after, increment, journal) = served(&second, PointId(10), cursor);
    assert_eq!(after.run, 2);
    assert!(
        after.samples.iter().all(|sample| sample.seq <= cursor),
        "the restarted tick domain renumbers the axis: {:?}",
        after
            .samples
            .iter()
            .map(|sample| sample.seq)
            .collect::<Vec<_>>()
    );

    // The defect's phantom-idle read: the pre-restart cursor still
    // filters every restarted sample out — but the empty answer now
    // carries the advanced run marker, so the seam is attributable on
    // the empty page rather than silently resuming in sequence later.
    assert!(increment.samples.is_empty());
    assert_eq!(increment.run, 2);
    assert!(
        journal
            .iter()
            .any(|entry| matches!(entry.event, JournalEvent::RunBoundary { run: 2 })),
        "the served journal carries the same run-2 boundary the \
         envelope marker counts: {journal:?}"
    );
    drop(second);

    let _ = std::fs::remove_dir_all(&dir);
}
