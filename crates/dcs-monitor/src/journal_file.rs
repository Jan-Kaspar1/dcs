//! The file-backed journal sink: monitor-local tooling persisting the
//! transition journal across a restart — the journal-persistence
//! decision. It is deliberately not a contract concern: the served
//! endpoints, event types, and payloads are unchanged, and the file is
//! consumed by review tooling and by the replaying monitor itself.
//!
//! With a path configured on [`MonitorConfig`](crate::MonitorConfig),
//! every entry the [`Recorder`](super::recorder) appends to the
//! transition journal is also appended here — one line-delimited
//! [`JournalRecord`] per line — at the same recording point: after the
//! scan's write phase, under the monitor lock, never mid-scan. On
//! startup the file replays into the in-memory ring and `seq` numbering
//! continues where it left off, so `GET /journal` answers continuously
//! across a restart and a retained eviction still reads as a numbering
//! gap — except a `run_boundary` entry never drops: the tail's bound
//! migrating one aside keeps every recorded lifetime boundary served
//! no matter the event volume that follows.
//!
//! One [`JournalRecord`] per line:
//!
//! - `{"run_boundary":{"run":N,"tick":T}}` — a process-lifetime
//!   boundary, appended once at startup before the run's first entry.
//!   `run` counts the file's lifetimes from 1; `tick` is the tick the
//!   run starts at — `0` on a cold start, the restored tick when the
//!   run resumes through `--state-file`, so the marker records the tick
//!   domain the following entries belong to. A restart's marker also
//!   journals once as a `run_boundary` entry — the marker's served
//!   form — so a `GET /journal` consumer attributes entries to a
//!   process lifetime the same way the file's readers do.
//! - `{"entry":{...}}` — a [`JournalEntry`] verbatim, in append order.
//!
//! The file stays separate from the `--state-file` checkpoint on
//! purpose: the checkpoint is state, overwritten per save and consumed
//! by restore; the journal is the append-only record of what happened,
//! consumed by review. A resumed run keeps appending to the same file
//! in the restored tick domain, so the audit record spans the restart
//! the checkpoint healed. A file that cannot be replayed — a torn or
//! corrupt record — fails startup naming the file and the offending
//! record rather than silently dropping the audit trail; a missing
//! file is a cold start. Point history stays volatile: only the
//! journal persists.
//!
//! One path has one live writer. The open takes an exclusive advisory
//! lock on the file held for the process lifetime, so two processes
//! configured with the same `--journal-file` — a same-host deployment
//! misconfiguration — cannot both run: each recorder continues `seq`
//! numbering from its own replay point, and concurrent writers would
//! interleave duplicate `seq`s, leaving the file un-replayable for
//! *every* subsequent startup. The second opener's bind instead fails
//! naming the file and the live-holder conflict. The lock releases
//! with the holder's file descriptor, so an ordinary restart —
//! including a killed process's — re-acquires it immediately.

use dcs_core::{JournalEntry, JournalEvent, PointId, Quality, Tick, Value};
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, VecDeque};
use std::fs::{File, OpenOptions, TryLockError};
use std::io::{self, BufRead, BufReader, Write};
use std::path::{Path, PathBuf};

/// A journal file's full contents for review tooling: every journaled
/// entry in `seq` order — the durable record is never truncated here,
/// unlike the replayed ring — plus the run-boundary markers separating
/// its process lifetimes.
///
/// Produced by [`read_journal_file`]; the metrics report consumes it as
/// the restart-surviving dataset the flood-and-performance decision
/// computes over. The boundaries matter: each run's entries carry its
/// own tick domain, so durations spanning a restart compute on the
/// elapsed-scans axis the boundaries declare, never by subtracting
/// ticks across domains.
#[derive(Debug, Clone, PartialEq)]
pub struct JournalData {
    /// Every entry the file records, oldest first.
    pub entries: Vec<JournalEntry>,
    /// The file's run-boundary markers in order — one per process
    /// lifetime the file records.
    pub boundaries: Vec<RunBoundary>,
}

/// One run-boundary marker's report-relevant data: which lifetime
/// began, the tick its run started at, and the `seq` the run's first
/// entry takes — the attribution an entry's run comes from.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RunBoundary {
    /// Which lifetime begins — the file counts runs from 1.
    pub run: u64,
    /// The tick the run started at: `0` on a cold start, the restored
    /// tick under `--state-file`.
    pub start_tick: Tick,
    /// The `seq` the run's first entry takes.
    pub first_seq: u64,
}

/// Reads the whole journal file at `path` for review tooling — the
/// consumer side of the durable sink, returning every [`JournalEntry`]
/// the file holds and its run count.
///
/// The same strictness startup replay applies: a line that does not
/// parse as a [`JournalRecord`] or an entry whose `seq` does not
/// continue the strictly increasing stream fails naming the file, the
/// line, and the record. A missing or unreadable file is an error here
/// — unlike the monitor's cold start, a review tool asked for a file
/// that does not exist has nothing to report on.
pub fn read_journal_file(path: &Path) -> io::Result<JournalData> {
    let file = File::open(path).map_err(|error| named(path, "cannot read journal file", error))?;
    let mut data = JournalData {
        entries: Vec::new(),
        boundaries: Vec::new(),
    };
    let mut next_seq = 1_u64;
    for (index, line) in BufReader::new(file).lines().enumerate() {
        let line = line.map_err(|error| named(path, "cannot read journal file", error))?;
        let record: JournalRecord = serde_json::from_str(&line).map_err(|error| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "journal file {} cannot be read: line {} is not a journal record: \
                     {line} ({error})",
                    path.display(),
                    index + 1,
                ),
            )
        })?;
        match record {
            JournalRecord::Entry(entry) => {
                if entry.seq < next_seq {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        format!(
                            "journal file {} cannot be read: line {} carries seq {} after \
                             seq {}: {line}",
                            path.display(),
                            index + 1,
                            entry.seq,
                            next_seq - 1,
                        ),
                    ));
                }
                next_seq = entry.seq + 1;
                data.entries.push(*entry);
            }
            JournalRecord::RunBoundary { run, tick } => {
                data.boundaries.push(RunBoundary {
                    run,
                    start_tick: tick,
                    first_seq: next_seq,
                });
            }
        }
    }
    Ok(data)
}

/// One line of the journal file — the monitor's own format wrapping
/// the contract's serialized [`JournalEntry`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(super) enum JournalRecord {
    /// A journaled entry, verbatim — boxed beside the small
    /// `RunBoundary` marker so the record's size stays the marker's.
    Entry(Box<JournalEntry>),
    /// The start of a new process lifetime in this file — see the
    /// module docs for the fields' meaning.
    RunBoundary {
        /// Which lifetime begins: `1` for the file's first run.
        run: u64,
        /// The tick the run starts at — `0` cold, the restored tick
        /// under `--state-file`.
        tick: Tick,
    },
}

/// What a replayed file yields: the retained journal tail for the
/// in-memory ring, the `seq` the next appended entry takes, the number
/// of process lifetimes the file already records, and the file's last
/// recorded observation per point — the whole record's fold, not just
/// the retained tail's, so a resumed run's recorder can diff its first
/// scan against the state the journal itself last carried instead of
/// re-recording the standing census as first observations. The default
/// is the cold start: no entries, `seq` numbering from 1.
#[derive(Debug)]
pub(super) struct Replay {
    /// The file's last `capacity` entries, oldest first.
    pub entries: VecDeque<JournalEntry>,
    /// The served `run_boundary` entries the retained tail's bound
    /// evicted during replay, in `seq` order — pinned aside under the
    /// same rule the live ring applies, so a restart's served journal
    /// still exposes every recorded lifetime boundary the tail alone
    /// would have dropped.
    pub boundaries: Vec<JournalEntry>,
    /// The `seq` the next journaled entry takes.
    pub next_seq: u64,
    /// Run-boundary markers the file already holds.
    pub runs: u64,
    /// The last quality each point's journaled transitions recorded.
    pub qualities: HashMap<PointId, Quality>,
    /// The last value each journaled point's transitions recorded.
    pub values: HashMap<PointId, Value>,
}

impl Default for Replay {
    fn default() -> Self {
        Self {
            entries: VecDeque::new(),
            boundaries: Vec::new(),
            next_seq: 1,
            runs: 0,
            qualities: HashMap::new(),
            values: HashMap::new(),
        }
    }
}

/// The open append handle plus its path — the path rides along so
/// append errors can name the file.
pub(super) struct JournalFile {
    path: PathBuf,
    file: File,
}

impl JournalFile {
    /// Opens `path` as this process's journal file: the append handle
    /// takes an exclusive advisory lock held until the file closes —
    /// a second live writer on the same path fails here naming the
    /// conflict, because two writers each continuing `seq` from their
    /// own replay point would interleave duplicate `seq`s and leave
    /// the record un-replayable at the next startup. Under the lock, an
    /// existing file is replayed into a [`Replay`] first — any
    /// unreadable, torn, or corrupt record fails by name — then this
    /// run's boundary marker is appended; a missing file is a cold
    /// start, created holding `run` 1's marker. `tick` is the tick this
    /// run starts at — the restored tick for a `--state-file` resume,
    /// `0` cold.
    pub(super) fn open(path: &Path, capacity: usize, tick: Tick) -> io::Result<(Self, Replay)> {
        let file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)
            .map_err(|error| named(path, "cannot open journal file", error))?;
        match file.try_lock() {
            Ok(()) => {}
            Err(TryLockError::WouldBlock) => {
                return Err(named(
                    path,
                    "cannot open journal file",
                    "a live process already holds its writer lock — two writers \
                     on one --journal-file interleave duplicate seqs and leave \
                     the record un-replayable; give each process its own journal \
                     file (the holder's identity is `fuser`/`lsof` on the path)",
                ));
            }
            Err(TryLockError::Error(error)) => {
                return Err(named(path, "cannot lock journal file", error));
            }
        }
        let replay = match File::open(path) {
            Ok(file) => replay(file, path, capacity)?,
            Err(error) if error.kind() == io::ErrorKind::NotFound => Replay::default(),
            Err(error) => return Err(named(path, "cannot read journal file", error)),
        };
        let mut sink = Self {
            path: path.to_path_buf(),
            file,
        };
        sink.write(&JournalRecord::RunBoundary {
            run: replay.runs + 1,
            tick,
        })?;
        Ok((sink, replay))
    }

    /// Appends `entry` as one line — the write every journaled entry
    /// takes at the recording point.
    pub(super) fn append(&mut self, entry: &JournalEntry) -> io::Result<()> {
        self.write(&JournalRecord::Entry(Box::new(entry.clone())))
    }

    /// Serializes `record` as one line and appends it — a direct
    /// `write_all` per record, so a completed append has reached the
    /// operating system and a killed process loses nothing it
    /// journaled.
    fn write(&mut self, record: &JournalRecord) -> io::Result<()> {
        let mut line = serde_json::to_string(record)
            .map_err(|error| named(&self.path, "cannot serialize a journal record", error))?;
        line.push('\n');
        self.file
            .write_all(line.as_bytes())
            .map_err(|error| named(&self.path, "cannot append to journal file", error))
    }
}

/// Replays `file` — already opened from `path` — into a [`Replay`].
/// Every line must parse as a [`JournalRecord`], and `Entry` records
/// must carry strictly increasing `seq`s: anything else is a corrupt
/// record, and the error names the file, the line number, and the
/// offending record rather than dropping the audit trail.
fn replay(file: File, path: &Path, capacity: usize) -> io::Result<Replay> {
    let mut replayed = Replay::default();
    for (index, line) in BufReader::new(file).lines().enumerate() {
        let line = line.map_err(|error| named(path, "cannot read journal file", error))?;
        let record: JournalRecord = serde_json::from_str(&line).map_err(|error| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "journal file {} cannot be replayed: line {} is not a journal record: \
                     {line} ({error})",
                    path.display(),
                    index + 1,
                ),
            )
        })?;
        match record {
            JournalRecord::Entry(entry) => {
                if entry.seq < replayed.next_seq {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        format!(
                            "journal file {} cannot be replayed: line {} carries seq {} after \
                             seq {}: {line}",
                            path.display(),
                            index + 1,
                            entry.seq,
                            replayed.next_seq - 1,
                        ),
                    ));
                }
                replayed.next_seq = entry.seq + 1;
                // The last-observed fold runs over every record, not
                // just the retained tail: an evicted entry still said
                // what the point's recorded state became, and a later
                // entry simply overwrites it.
                match &entry.event {
                    JournalEvent::QualityChanged { point, to, .. } => {
                        replayed.qualities.insert(*point, *to);
                    }
                    JournalEvent::PointChanged { point, to, .. } => {
                        replayed.values.insert(*point, *to);
                    }
                    _ => {}
                }
                replayed.entries.push_back(*entry);
                while replayed.entries.len() > capacity {
                    // The pinning rule the served ring applies live: an
                    // evicted run-boundary entry is set aside rather
                    // than dropped — the marker is the record's only
                    // sign that a new process lifetime began.
                    let evicted = replayed.entries.pop_front().unwrap();
                    if matches!(evicted.event, JournalEvent::RunBoundary { .. }) {
                        replayed.boundaries.push(evicted);
                    }
                }
            }
            JournalRecord::RunBoundary { .. } => replayed.runs += 1,
        }
    }
    Ok(replayed)
}

/// An `io::Error` whose message names the journal file — the shape
/// every file-side failure takes so startup errors identify it.
fn named(path: &Path, what: &str, error: impl std::fmt::Display) -> io::Error {
    io::Error::other(format!("{what} {}: {error}", path.display()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::recorder::MonitorConfig;
    use dcs_core::{Command, CommandOutcome, CommandReceipt, PointId, Quality, Value, ValueKind};

    /// A scratch directory per test and process — tests run in
    /// parallel.
    fn scratch(test: &str) -> PathBuf {
        let dir =
            std::env::temp_dir().join(format!("dcs-journal-file-{test}-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    /// The file's lines parsed back into records.
    fn records(path: &Path) -> Vec<JournalRecord> {
        std::fs::read_to_string(path)
            .unwrap()
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect()
    }

    fn receipt(point: u64, tick: u64) -> CommandReceipt {
        CommandReceipt {
            command: Command::WriteValue {
                point: PointId(point),
                kind: ValueKind::Float,
                value: Value::Float(1.0),
            },
            outcome: CommandOutcome::Applied { tick: Tick(tick) },
            actor: None,
        }
    }

    fn config(path: &Path, journal_capacity: usize) -> MonitorConfig {
        MonitorConfig {
            journal_file: Some(path.to_path_buf()),
            journal_capacity,
            ..MonitorConfig::default()
        }
    }

    #[test]
    fn entries_append_in_order_and_replay_with_continuing_seqs_across_a_restart() {
        let dir = scratch("roundtrip");
        let path = dir.join("journal.jsonl");

        // First lifetime: two journaled entries land in the file behind
        // the run-1 boundary marker.
        let mut recorder = crate::recorder::Recorder::new(config(&path, 8), Tick::ZERO).unwrap();
        recorder.note_settled(None, receipt(10, 1), Tick(1));
        recorder.note_settled(None, receipt(11, 2), Tick(2));
        assert_eq!(
            records(&path),
            vec![
                JournalRecord::RunBoundary {
                    run: 1,
                    tick: Tick::ZERO
                },
                JournalRecord::Entry(Box::new(JournalEntry {
                    seq: 1,
                    tick: Tick(1),
                    event: dcs_core::JournalEvent::CommandSettled {
                        receipt: receipt(10, 1)
                    },
                })),
                JournalRecord::Entry(Box::new(JournalEntry {
                    seq: 2,
                    tick: Tick(2),
                    event: dcs_core::JournalEvent::CommandSettled {
                        receipt: receipt(11, 2)
                    },
                })),
            ]
        );
        drop(recorder);

        // Second lifetime: the replayed entries are served with their
        // seqs, the boundary marker separates the runs in the file —
        // and journals once as the restart's served boundary entry —
        // and new entries continue the numbering.
        let mut recorder = crate::recorder::Recorder::new(config(&path, 8), Tick::ZERO).unwrap();
        assert_eq!(
            recorder
                .journal(0)
                .iter()
                .map(|entry| (entry.seq, entry.tick, entry.event.clone()))
                .collect::<Vec<_>>(),
            vec![
                (
                    1,
                    Tick(1),
                    dcs_core::JournalEvent::CommandSettled {
                        receipt: receipt(10, 1),
                    }
                ),
                (
                    2,
                    Tick(2),
                    dcs_core::JournalEvent::CommandSettled {
                        receipt: receipt(11, 2),
                    }
                ),
                (
                    3,
                    Tick::ZERO,
                    dcs_core::JournalEvent::RunBoundary { run: 2 }
                ),
            ]
        );
        recorder.note_settled(None, receipt(12, 1), Tick(1));
        let entries = recorder.journal(0);
        assert_eq!(entries.last().unwrap().seq, 4);

        let file = records(&path);
        assert_eq!(file.len(), 6);
        assert_eq!(
            file[3],
            JournalRecord::RunBoundary {
                run: 2,
                tick: Tick::ZERO
            }
        );
        assert_eq!(
            file[4..]
                .iter()
                .map(|record| match record {
                    JournalRecord::Entry(entry) => entry.seq,
                    JournalRecord::RunBoundary { .. } => unreachable!(),
                })
                .collect::<Vec<_>>(),
            vec![3, 4]
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn point_changed_entries_append_and_replay_with_continuing_seqs_across_a_restart() {
        let dir = scratch("point-changed");
        let path = dir.join("journal.jsonl");

        // First lifetime: a first-observation transition and a follow-up
        // land in the file as ordinary entries — the variant needs no
        // special file handling.
        let mut recorder = crate::recorder::Recorder::new(config(&path, 8), Tick::ZERO).unwrap();
        recorder.push(
            Tick(1),
            dcs_core::JournalEvent::PointChanged {
                point: PointId(10),
                from: None,
                to: Value::Bool(true),
            },
        );
        recorder.push(
            Tick(2),
            dcs_core::JournalEvent::PointChanged {
                point: PointId(10),
                from: Some(Value::Bool(true)),
                to: Value::Bool(false),
            },
        );
        drop(recorder);

        // Second lifetime: the `point_changed` entries replay with
        // their seqs behind the restart's served boundary entry, and
        // the next entry continues the numbering.
        let mut recorder = crate::recorder::Recorder::new(config(&path, 8), Tick::ZERO).unwrap();
        assert_eq!(
            recorder
                .journal(0)
                .iter()
                .map(|entry| entry.seq)
                .collect::<Vec<_>>(),
            vec![1, 2, 3]
        );
        recorder.push(
            Tick(3),
            dcs_core::JournalEvent::PointChanged {
                point: PointId(10),
                from: Some(Value::Bool(false)),
                to: Value::Bool(true),
            },
        );
        assert_eq!(recorder.journal(0).last().unwrap().seq, 4);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn replay_keeps_the_retained_tail_and_a_resumed_tick_lands_in_the_marker() {
        let dir = scratch("tail");
        let path = dir.join("journal.jsonl");

        let mut recorder = crate::recorder::Recorder::new(config(&path, 2), Tick::ZERO).unwrap();
        for index in 0..3_u64 {
            recorder.note_settled(None, receipt(10, index + 1), Tick(index + 1));
        }
        drop(recorder);

        // Capacity 2 retains only the tail; the seq numbering still
        // continues from the file's last entry, and the run's restored
        // tick is recorded in the boundary marker — the --state-file
        // resume case. The restart's served boundary entry takes the
        // next `seq` and evicts the oldest retained entry.
        let mut recorder = crate::recorder::Recorder::new(config(&path, 2), Tick(40)).unwrap();
        assert_eq!(
            recorder
                .journal(0)
                .iter()
                .map(|entry| entry.seq)
                .collect::<Vec<_>>(),
            vec![3, 4]
        );
        recorder.note_settled(None, receipt(10, 41), Tick(41));
        assert_eq!(recorder.journal(3).last().unwrap().seq, 5);
        assert_eq!(
            records(&path)[4],
            JournalRecord::RunBoundary {
                run: 2,
                tick: Tick(40)
            }
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_corrupt_record_fails_replay_naming_the_file_and_the_record() {
        let dir = scratch("corrupt");
        let path = dir.join("journal.jsonl");

        let mut recorder = crate::recorder::Recorder::new(config(&path, 8), Tick::ZERO).unwrap();
        recorder.note_settled(None, receipt(10, 1), Tick(1));
        drop(recorder);

        // A torn trailing record — the crash-mid-write shape — and an
        // out-of-order seq both refuse startup by name.
        for bad in [
            "{\"entry\":{\"seq\":2,",
            "{\"entry\":{\"seq\":1,\"tick\":0,\"event\":{\"step_failed\":{\"component\":\"c\",\"error\":\"e\"}}}}",
        ] {
            let mut body = std::fs::read_to_string(&path).unwrap();
            body.push_str(bad);
            body.push('\n');
            std::fs::write(&path, body).unwrap();
            let error = match crate::recorder::Recorder::new(config(&path, 8), Tick::ZERO) {
                Ok(_) => panic!("a corrupt journal file must fail startup"),
                Err(error) => error,
            };
            let message = error.to_string();
            assert!(message.contains(path.to_str().unwrap()), "{message}");
            assert!(message.contains(bad.trim()), "{message}");
            // Repair the file for the next case: drop the bad line.
            let body = std::fs::read_to_string(&path).unwrap();
            let repaired: Vec<&str> = body.lines().collect();
            std::fs::write(&path, repaired[..repaired.len() - 1].join("\n") + "\n").unwrap();
        }

        // The repaired file replays fine — the failures named the
        // record, not the file itself.
        crate::recorder::Recorder::new(config(&path, 8), Tick::ZERO).unwrap();
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_second_live_writer_on_the_same_path_fails_naming_the_conflict() {
        let dir = scratch("shared");
        let path = dir.join("journal.jsonl");

        // The first writer binds and journals, holding the file's
        // writer lock for its lifetime.
        let mut first = crate::recorder::Recorder::new(config(&path, 8), Tick::ZERO).unwrap();
        first.note_settled(None, receipt(10, 1), Tick(1));

        // The misconfiguration — a second writer on the same path —
        // fails its bind naming the file and the live-holder conflict
        // rather than interleaving duplicate seqs from its own replay
        // point into the record.
        let error = match crate::recorder::Recorder::new(config(&path, 8), Tick::ZERO) {
            Ok(_) => panic!("a second live writer on one journal file must fail its bind"),
            Err(error) => error,
        };
        let message = error.to_string();
        assert!(message.contains(path.to_str().unwrap()), "{message}");
        assert!(message.contains("writer lock"), "{message}");

        // The holder keeps appending undisturbed, and once it dies —
        // the lock releasing with its descriptor — the next opener
        // replays the single-writer file and continues the seq domain
        // across the run boundary.
        first.note_settled(None, receipt(11, 2), Tick(2));
        drop(first);
        let mut second = crate::recorder::Recorder::new(config(&path, 8), Tick::ZERO).unwrap();
        second.note_settled(None, receipt(12, 3), Tick(3));
        assert_eq!(second.journal(0).last().unwrap().seq, 4);
        let data = read_journal_file(&path).unwrap();
        assert_eq!(
            data.entries
                .iter()
                .map(|entry| entry.seq)
                .collect::<Vec<_>>(),
            vec![1, 2, 3, 4]
        );
        assert_eq!(data.boundaries.len(), 2);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_missing_file_is_a_cold_start() {
        let dir = scratch("cold");
        let path = dir.join("journal.jsonl");
        let recorder = crate::recorder::Recorder::new(config(&path, 8), Tick::ZERO).unwrap();
        assert!(recorder.journal(0).is_empty());
        assert_eq!(
            records(&path),
            vec![JournalRecord::RunBoundary {
                run: 1,
                tick: Tick::ZERO
            }]
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_run_without_the_sink_behaves_as_before() {
        let dir = scratch("unsunk");
        let path = dir.join("journal.jsonl");
        let mut recorder =
            crate::recorder::Recorder::new(MonitorConfig::default(), Tick::ZERO).unwrap();
        recorder.note_settled(None, receipt(10, 1), Tick(1));
        assert_eq!(recorder.journal(0).len(), 1);
        assert!(!path.exists());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn first_observed_quality_entries_roundtrip() {
        let dir = scratch("quality");
        let path = dir.join("journal.jsonl");
        let mut recorder = crate::recorder::Recorder::new(config(&path, 8), Tick::ZERO).unwrap();
        recorder.push(
            Tick(1),
            dcs_core::JournalEvent::QualityChanged {
                point: PointId(10),
                from: None,
                to: Quality::Good,
            },
        );
        drop(recorder);
        let recorder = crate::recorder::Recorder::new(config(&path, 8), Tick::ZERO).unwrap();
        assert_eq!(
            recorder.journal(0),
            vec![
                JournalEntry {
                    seq: 1,
                    tick: Tick(1),
                    event: dcs_core::JournalEvent::QualityChanged {
                        point: PointId(10),
                        from: None,
                        to: Quality::Good,
                    },
                },
                JournalEntry {
                    seq: 2,
                    tick: Tick::ZERO,
                    event: dcs_core::JournalEvent::RunBoundary { run: 2 },
                },
            ]
        );
        let _ = std::fs::remove_dir_all(&dir);
    }
}
