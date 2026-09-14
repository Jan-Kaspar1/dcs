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
//! gap.
//!
//! One [`JournalRecord`] per line:
//!
//! - `{"run_boundary":{"run":N,"tick":T}}` — a process-lifetime
//!   boundary, appended once at startup before the run's first entry.
//!   `run` counts the file's lifetimes from 1; `tick` is the tick the
//!   run starts at — `0` on a cold start, the restored tick when the
//!   run resumes through `--state-file`, so the marker records the tick
//!   domain the following entries belong to.
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

use dcs_core::{JournalEntry, Tick};
use serde::{Deserialize, Serialize};
use std::collections::VecDeque;
use std::fs::{File, OpenOptions};
use std::io::{self, BufRead, BufReader, Write};
use std::path::{Path, PathBuf};

/// One line of the journal file — the monitor's own format wrapping
/// the contract's serialized [`JournalEntry`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(super) enum JournalRecord {
    /// A journaled entry, verbatim.
    Entry(JournalEntry),
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
/// in-memory ring, the `seq` the next appended entry takes, and the
/// number of process lifetimes the file already records. The default
/// is the cold start: no entries, `seq` numbering from 1.
#[derive(Debug)]
pub(super) struct Replay {
    /// The file's last `capacity` entries, oldest first.
    pub entries: VecDeque<JournalEntry>,
    /// The `seq` the next journaled entry takes.
    pub next_seq: u64,
    /// Run-boundary markers the file already holds.
    pub runs: u64,
}

impl Default for Replay {
    fn default() -> Self {
        Self {
            entries: VecDeque::new(),
            next_seq: 1,
            runs: 0,
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
    /// Opens `path` as this process's journal file: an existing file is
    /// replayed into a [`Replay`] first — any unreadable, torn, or
    /// corrupt record fails by name — then this run's boundary marker
    /// is appended; a missing file is a cold start, created holding
    /// `run` 1's marker. `tick` is the tick this run starts at — the
    /// restored tick for a `--state-file` resume, `0` cold.
    pub(super) fn open(path: &Path, capacity: usize, tick: Tick) -> io::Result<(Self, Replay)> {
        let replay = match File::open(path) {
            Ok(file) => replay(file, path, capacity)?,
            Err(error) if error.kind() == io::ErrorKind::NotFound => Replay::default(),
            Err(error) => return Err(named(path, "cannot read journal file", error)),
        };
        let file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)
            .map_err(|error| named(path, "cannot open journal file", error))?;
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
        self.write(&JournalRecord::Entry(entry.clone()))
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
                replayed.entries.push_back(entry);
                while replayed.entries.len() > capacity {
                    replayed.entries.pop_front();
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
        recorder.note_settled(receipt(10, 1), Tick(1));
        recorder.note_settled(receipt(11, 2), Tick(2));
        assert_eq!(
            records(&path),
            vec![
                JournalRecord::RunBoundary {
                    run: 1,
                    tick: Tick::ZERO
                },
                JournalRecord::Entry(JournalEntry {
                    seq: 1,
                    tick: Tick(1),
                    event: dcs_core::JournalEvent::CommandSettled {
                        receipt: receipt(10, 1)
                    },
                }),
                JournalRecord::Entry(JournalEntry {
                    seq: 2,
                    tick: Tick(2),
                    event: dcs_core::JournalEvent::CommandSettled {
                        receipt: receipt(11, 2)
                    },
                }),
            ]
        );
        drop(recorder);

        // Second lifetime: the replayed entries are served with their
        // seqs, the boundary marker separates the runs in the file, and
        // new entries continue the numbering.
        let mut recorder = crate::recorder::Recorder::new(config(&path, 8), Tick::ZERO).unwrap();
        assert_eq!(
            recorder
                .journal(0)
                .iter()
                .map(|entry| entry.seq)
                .collect::<Vec<_>>(),
            vec![1, 2]
        );
        recorder.note_settled(receipt(12, 1), Tick(1));
        let entries = recorder.journal(0);
        assert_eq!(entries.last().unwrap().seq, 3);

        let file = records(&path);
        assert_eq!(file.len(), 5);
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
            vec![3]
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn replay_keeps_the_retained_tail_and_a_resumed_tick_lands_in_the_marker() {
        let dir = scratch("tail");
        let path = dir.join("journal.jsonl");

        let mut recorder = crate::recorder::Recorder::new(config(&path, 2), Tick::ZERO).unwrap();
        for index in 0..3_u64 {
            recorder.note_settled(receipt(10, index + 1), Tick(index + 1));
        }
        drop(recorder);

        // Capacity 2 retains only the tail; the seq numbering still
        // continues from the file's last entry, and the run's restored
        // tick is recorded in the boundary marker — the --state-file
        // resume case.
        let mut recorder = crate::recorder::Recorder::new(config(&path, 2), Tick(40)).unwrap();
        assert_eq!(
            recorder
                .journal(0)
                .iter()
                .map(|entry| entry.seq)
                .collect::<Vec<_>>(),
            vec![2, 3]
        );
        recorder.note_settled(receipt(10, 41), Tick(41));
        assert_eq!(recorder.journal(3).last().unwrap().seq, 4);
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
        recorder.note_settled(receipt(10, 1), Tick(1));
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
        recorder.note_settled(receipt(10, 1), Tick(1));
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
            vec![JournalEntry {
                seq: 1,
                tick: Tick(1),
                event: dcs_core::JournalEvent::QualityChanged {
                    point: PointId(10),
                    from: None,
                    to: Quality::Good,
                },
            }]
        );
        let _ = std::fs::remove_dir_all(&dir);
    }
}
