//! The file-backed durable process-history store: monitor-local tooling
//! persisting the declared-duty sample stream across a restart — the
//! durable-history decision's store, in the journal file's exact
//! pattern. It is deliberately not a contract concern: the served
//! endpoints, event types, and payloads follow the established
//! since-cursor conventions, and the file is consumed by export
//! tooling and by the replaying monitor itself.
//!
//! With a path configured on [`MonitorConfig`](crate::MonitorConfig),
//! every [`DurableEntry`] the [`Recorder`](super::recorder) appends —
//! the declared-duty points' samples recorded at their declared
//! cadence at the existing post-scan recording point, never mid-scan
//! — is also appended here, one line-delimited [`HistoryRecord`] per
//! line. The append itself runs on the sink's own writer thread, not
//! under the monitor lock: the recording point hands each record to a
//! bounded [`Drain`](crate::drain::Drain) queue — the same isolation
//! the journal sink's decision landed — so a slow or stalled sink can
//! neither lengthen a scan nor pin the lock, while the queue's
//! declared bound keeps the failure fatal at the push: an append the
//! file cannot take fails the run naming the file rather than leaving
//! a silent gap in the record — decision 36's rule, extended to the
//! sibling file. On startup the file replays into a bounded served
//! window and `seq` numbering continues where it left off, so
//! `GET /history/durable` answers continuously across a restart and a
//! retained eviction still reads as a numbering gap — except a
//! `run_boundary` or `domain` marker entry never drops: the tail's
//! bound migrating it aside keeps every recorded lifetime and tick-
//! domain boundary served no matter the sample volume that follows.
//!
//! One [`HistoryRecord`] per line:
//!
//! - `{"run_boundary":{"run":N,"tick":T,"anchor":A}}` — a
//!   process-lifetime boundary, appended once at startup before the
//!   run's first record. `run` counts the file's lifetimes from 1;
//!   `tick` is the tick the run starts at — `0` on a cold start, the
//!   restored tick when the run resumes through `--state-file`, so the
//!   marker records the tick domain the following entries belong to.
//!   `anchor` stamps the domain's declared civil-time anchor — the
//!   wall-clock instant of the domain's origin tick the pacing layer
//!   minted — so every exported record self-describes its
//!   tick-to-civil mapping; absent when the domain is unanchored — a
//!   driven or unminted run, whose artifacts stay byte-identical under
//!   an unchanged script. A restart's marker also enters the durable
//!   stream once as a `run_boundary` entry — the marker's served
//!   form — so a `GET /history/durable` consumer attributes records
//!   to a process lifetime the same way the file's readers do.
//! - `{"entry":{...}}` — a [`DurableEntry`] verbatim, in append order.
//!   A `domain` event inside an entry is the mid-lifetime tick-domain
//!   seam — a tracking peer's checkpoint adoption moved the run onto
//!   a source's newer domain — carrying that domain's anchor the same
//!   way the boundary markers carry their run's.
//!
//! The file stays separate from the `--state-file` checkpoint on
//! purpose, as the journal file does: the checkpoint is state,
//! overwritten per save and consumed by restore; the history file is
//! the append-only record of what the run observed, consumed by
//! export. A resumed run keeps appending to the same file in the
//! restored tick domain and anchor, so the durable series spans the
//! restart the checkpoint healed. A file that cannot be replayed — a
//! torn or corrupt record — fails startup naming the file and the
//! offending record rather than silently dropping the record; a
//! missing file is a cold start.
//!
//! One path has one live writer — the same single-writer rule the
//! journal file takes, for the same reason: two writers each
//! continuing `seq` from their own replay point would interleave
//! duplicate `seq`s and leave the record un-replayable at the next
//! startup. The second opener's bind fails naming the file and the
//! live-holder conflict; the lock releases with the holder's file
//! descriptor, so an ordinary restart — including a killed
//! process's — re-acquires it immediately.
//!
//! What the file does not do: retention on disk is a deployment-
//! managed bound — the declared cadence and `retain_days` span are
//! what a deployment sizes, rotates, and archives against; the
//! multi-year regulatory term is the downstream records system's,
//! and historian/CMMS/report export consumes this file plus the
//! since-cursor endpoint — this module is the seam, not the export.

use crate::drain::Drain;
use dcs_core::{DurableEntry, DurableEvent, PointId, Tick, TickAnchor};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::collections::VecDeque;
use std::fs::{File, OpenOptions, TryLockError};
use std::io::{self, BufRead, BufReader, Write};
use std::path::{Path, PathBuf};

/// A history file's full contents for export tooling: every recorded
/// [`DurableEntry`] in `seq` order — the durable record is never
/// truncated here, unlike the replayed window — plus the
/// run-boundary markers separating its process lifetimes.
///
/// Produced by [`read_history_file`]. The boundaries matter: each
/// run's records carry its own tick domain and anchor, so civil-time
/// aggregation spanning a restart maps each side through the anchor
/// its boundary declares, never by mixing ticks across domains.
#[derive(Debug, Clone, PartialEq)]
pub struct HistoryData {
    /// Every entry the file records, oldest first — the `domain`
    /// events among them marking mid-lifetime tick-domain changes.
    pub entries: Vec<DurableEntry>,
    /// The file's run-boundary markers in order — one per process
    /// lifetime the file records, each carrying the tick the run
    /// started at and the domain anchor the run recorded under.
    pub boundaries: Vec<HistoryBoundary>,
}

/// One run-boundary marker's export-relevant data: which lifetime
/// began, the tick its run started at, the domain's declared
/// civil-time anchor, and the `seq` the run's first entry takes —
/// the attribution an entry's run comes from.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct HistoryBoundary {
    /// Which lifetime begins — the file counts runs from 1.
    pub run: u64,
    /// The tick the run started at: `0` on a cold start, the restored
    /// tick under `--state-file`.
    pub start_tick: Tick,
    /// The domain's declared civil-time anchor — `None` on an
    /// unanchored (driven or unminted) domain.
    pub anchor: Option<TickAnchor>,
    /// The `seq` the run's first entry takes.
    pub first_seq: u64,
}

/// Reads the whole history file at `path` for export tooling — the
/// consumer side of the durable sink, returning every [`DurableEntry`]
/// the file holds and its run count.
///
/// The same strictness startup replay applies: a line that does not
/// parse as a [`HistoryRecord`] or an entry whose `seq` does not
/// continue the strictly increasing stream fails naming the file, the
/// line, and the record. A missing or unreadable file is an error here
/// — unlike the monitor's cold start, an export tool asked for a file
/// that does not exist has nothing to report on.
pub fn read_history_file(path: &Path) -> io::Result<HistoryData> {
    let file = File::open(path).map_err(|error| named(path, "cannot read history file", error))?;
    let mut data = HistoryData {
        entries: Vec::new(),
        boundaries: Vec::new(),
    };
    let mut next_seq = 1_u64;
    for (index, line) in BufReader::new(file).lines().enumerate() {
        let line = line.map_err(|error| named(path, "cannot read history file", error))?;
        let record: HistoryRecord = serde_json::from_str(&line).map_err(|error| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "history file {} cannot be read: line {} is not a history record: \
                     {line} ({error})",
                    path.display(),
                    index + 1,
                ),
            )
        })?;
        match record {
            HistoryRecord::Entry(entry) => {
                if entry.seq < next_seq {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        format!(
                            "history file {} cannot be read: line {} carries seq {} after \
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
            HistoryRecord::RunBoundary { run, tick, anchor } => {
                data.boundaries.push(HistoryBoundary {
                    run,
                    start_tick: tick,
                    anchor,
                    first_seq: next_seq,
                });
            }
        }
    }
    Ok(data)
}

/// One line of the history file — the monitor's own format wrapping
/// the contract's serialized [`DurableEntry`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(super) enum HistoryRecord {
    /// A durable-history entry, verbatim — boxed beside the small
    /// `RunBoundary` marker so the record's size stays the marker's.
    Entry(Box<DurableEntry>),
    /// The start of a new process lifetime in this file — see the
    /// module docs for the fields' meaning.
    RunBoundary {
        /// Which lifetime begins: `1` for the file's first run.
        run: u64,
        /// The tick the run starts at — `0` cold, the restored tick
        /// under `--state-file`.
        tick: Tick,
        /// The run's tick domain's declared civil-time anchor —
        /// absent when the domain is unanchored.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        anchor: Option<TickAnchor>,
    },
}

/// What a replayed file yields: the retained durable tail for the
/// served window, the `seq` the next appended entry takes, the number
/// of process lifetimes the file already records, and the file's last
/// recorded run tick per point — the cadence baselines a resumed run
/// continues recording under instead of re-recording the standing
/// census as first observations. The default is the cold start: no
/// entries, `seq` numbering from 1.
#[derive(Debug)]
pub(super) struct HistoryReplay {
    /// The file's last `capacity` entries, oldest first.
    pub entries: VecDeque<DurableEntry>,
    /// The served boundary entries — `run_boundary` and `domain`
    /// markers — the retained tail's bound evicted during replay, in
    /// `seq` order: pinned aside under the same rule the live ring
    /// applies, so a restart's served stream still exposes every
    /// recorded lifetime and domain boundary the tail alone would
    /// have dropped.
    pub boundaries: Vec<DurableEntry>,
    /// The `seq` the next durable entry takes.
    pub next_seq: u64,
    /// Run-boundary markers the file already holds.
    pub runs: u64,
    /// The last run tick each declared-duty point's records were
    /// attributed to — the cadence baseline a resumed run adopts when
    /// its own tick domain continues (a `--state-file` restore): the
    /// file is the run's own record, so a point re-observed inside its
    /// cadence across the restart records nothing until the interval
    /// the file already paced out expires. A baseline ahead of the
    /// run's start tick belongs to a different domain — the restart
    /// rewound the axis — and is not adopted.
    pub last_ticks: BTreeMap<PointId, Tick>,
    /// The domain anchor the file's tail records under — the last
    /// stamped anchor: the newest boundary marker's, or the newest
    /// `domain` event's where a mid-lifetime adoption superseded it.
    /// `None` when the file records no anchor.
    pub anchor: Option<TickAnchor>,
}

impl Default for HistoryReplay {
    fn default() -> Self {
        Self {
            entries: VecDeque::new(),
            boundaries: Vec::new(),
            next_seq: 1,
            runs: 0,
            last_ticks: BTreeMap::new(),
            anchor: None,
        }
    }
}

/// The open append handle plus its path — the path rides along so
/// append errors can name the file.
pub(super) struct HistoryFile {
    path: PathBuf,
    file: File,
}

impl HistoryFile {
    /// Opens `path` as this process's history file: the append handle
    /// takes an exclusive advisory lock held until the file closes —
    /// a second live writer on the same path fails here naming the
    /// conflict, the single-writer rule the journal file's decision
    /// records. Under the lock, an existing file is replayed into a
    /// [`HistoryReplay`] first — any unreadable, torn, or corrupt
    /// record fails by name — then this run's boundary marker is
    /// appended; a missing file is a cold start, created holding
    /// `run` 1's marker. `tick` is the tick this run starts at — the
    /// restored tick for a `--state-file` resume, `0` cold — and
    /// `anchor` the tick domain's declared civil-time anchor, `None`
    /// on an unanchored domain.
    pub(super) fn open(
        path: &Path,
        capacity: usize,
        tick: Tick,
        anchor: Option<TickAnchor>,
    ) -> io::Result<(Self, HistoryReplay)> {
        let file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)
            .map_err(|error| named(path, "cannot open history file", error))?;
        match file.try_lock() {
            Ok(()) => {}
            Err(TryLockError::WouldBlock) => {
                return Err(named(
                    path,
                    "cannot open history file",
                    "a live process already holds its writer lock — two writers \
                     on one --history-file interleave duplicate seqs and leave \
                     the record un-replayable; give each process its own history \
                     file (the holder's identity is `fuser`/`lsof` on the path)",
                ));
            }
            Err(TryLockError::Error(error)) => {
                return Err(named(path, "cannot lock history file", error));
            }
        }
        let replay = match File::open(path) {
            Ok(file) => replay(file, path, capacity)?,
            Err(error) if error.kind() == io::ErrorKind::NotFound => HistoryReplay::default(),
            Err(error) => return Err(named(path, "cannot read history file", error)),
        };
        let mut sink = Self {
            path: path.to_path_buf(),
            file,
        };
        sink.write(&HistoryRecord::RunBoundary {
            run: replay.runs + 1,
            tick,
            anchor,
        })?;
        Ok((sink, replay))
    }

    /// Hands this file to a [`Drain`]'s writer thread — the append
    /// isolation the durable-history decision extends from the
    /// journal's: the recorder's push queues each record rather than
    /// writing it, so the monitor lock never waits on the file, and
    /// the queue's `capacity` bound — a push finding it full — is
    /// where a stalled sink turns fatal. The writer appends in push
    /// order on this descriptor, keeping the advisory lock and the
    /// file's `seq` continuity with one writer per path.
    pub(super) fn into_drain(self, capacity: usize) -> Drain<HistoryRecord> {
        let label = format!("history file {}", self.path.display());
        let mut file = self;
        Drain::new(label, capacity, move |record| file.write(record))
    }

    /// Serializes `record` as one line and appends it — a direct
    /// `write_all` per record, so a completed append has reached the
    /// operating system and a killed process loses nothing it
    /// recorded.
    fn write(&mut self, record: &HistoryRecord) -> io::Result<()> {
        let mut line = serde_json::to_string(record)
            .map_err(|error| named(&self.path, "cannot serialize a history record", error))?;
        line.push('\n');
        self.file
            .write_all(line.as_bytes())
            .map_err(|error| named(&self.path, "cannot append to history file", error))
    }
}

/// Replays `file` — already opened from `path` — into a
/// [`HistoryReplay`]. Every line must parse as a [`HistoryRecord`],
/// and `Entry` records must carry strictly increasing `seq`s:
/// anything else is a corrupt record, and the error names the file,
/// the line number, and the offending record rather than dropping
/// the durable record.
fn replay(file: File, path: &Path, capacity: usize) -> io::Result<HistoryReplay> {
    let mut replayed = HistoryReplay::default();
    for (index, line) in BufReader::new(file).lines().enumerate() {
        let line = line.map_err(|error| named(path, "cannot read history file", error))?;
        let record: HistoryRecord = serde_json::from_str(&line).map_err(|error| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "history file {} cannot be replayed: line {} is not a history record: \
                     {line} ({error})",
                    path.display(),
                    index + 1,
                ),
            )
        })?;
        match record {
            HistoryRecord::Entry(entry) => {
                if entry.seq < replayed.next_seq {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        format!(
                            "history file {} cannot be replayed: line {} carries seq {} after \
                             seq {}: {line}",
                            path.display(),
                            index + 1,
                            entry.seq,
                            replayed.next_seq - 1,
                        ),
                    ));
                }
                replayed.next_seq = entry.seq + 1;
                // The last-tick and anchor folds run over every
                // record, not just the retained tail: an evicted
                // entry still said what the point's cadence baseline
                // became and which domain the tail records under, and
                // a later entry simply overwrites them.
                match &entry.event {
                    DurableEvent::Sampled { point, .. } => {
                        replayed.last_ticks.insert(*point, entry.tick);
                    }
                    DurableEvent::Domain { anchor } => {
                        replayed.anchor = *anchor;
                    }
                    _ => {}
                }
                replayed.entries.push_back(*entry);
                while replayed.entries.len() > capacity {
                    // The pinning rule the served ring applies live:
                    // an evicted boundary entry is set aside rather
                    // than dropped — the markers are the record's only
                    // sign that a new process lifetime or tick domain
                    // began.
                    let evicted = replayed.entries.pop_front().unwrap();
                    if matches!(
                        evicted.event,
                        DurableEvent::RunBoundary { .. } | DurableEvent::Domain { .. }
                    ) {
                        replayed.boundaries.push(evicted);
                    }
                }
            }
            HistoryRecord::RunBoundary { anchor, .. } => {
                replayed.runs += 1;
                replayed.anchor = anchor;
            }
        }
    }
    Ok(replayed)
}

/// An `io::Error` whose message names the history file — the shape
/// every file-side failure takes so startup errors identify it.
fn named(path: &Path, what: &str, error: impl std::fmt::Display) -> io::Error {
    io::Error::other(format!("{what} {}: {error}", path.display()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::drain::Drain;
    use crate::recorder::{MonitorConfig, Recorder};
    use dcs_core::{
        Direction, IoDriver, IoError, Sample, Value, ValueKind,
    };
    use dcs_runtime::{Executor, PointMap, PointSpec};
    use std::collections::HashMap;
    use std::sync::Mutex;

    /// The same minimal in-memory driver the other monitor fixtures
    /// use — one driver-served `In` point whose value the test steps.
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

        fn set(&self, point: PointId, value: Value) {
            self.points.lock().unwrap().insert(
                point,
                Sample::good(value, Tick::ZERO),
            );
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

    /// A scratch directory per test and process — tests run in
    /// parallel.
    fn scratch(test: &str) -> PathBuf {
        let dir =
            std::env::temp_dir().join(format!("dcs-history-{test}-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    /// The file's raw records — what a startup replay consumes.
    fn records(path: &Path) -> Vec<HistoryRecord> {
        std::fs::read_to_string(path)
            .unwrap()
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect()
    }

    fn config(path: &Path, durable_capacity: usize) -> MonitorConfig {
        MonitorConfig {
            history_file: Some(path.to_path_buf()),
            durable_capacity,
            ..MonitorConfig::default()
        }
    }

    /// An executor serving point 10 as a driver `In` with `cadence`
    /// as its declared durable recording duty — `None` leaves the
    /// point to the volatile ring.
    fn declared_executor<'d>(driver: &'d StubDriver, cadence: Option<u64>) -> Executor<'d> {
        let map = PointMap::new().with_spec(
            PointId(10),
            PointSpec {
                direction: Direction::In,
                kind: ValueKind::Float,
                internal: None,
                writable: false,
                requires_reason: false,
                stale_after_ticks: None,
                journaled: false,
                record_every_ticks: cadence,
            },
        );
        Executor::new(driver, map, Vec::new()).unwrap()
    }

    /// One full recording cycle: the scan then the post-scan record —
    /// the same point the monitor records at.
    fn scan(recorder: &mut Recorder, executor: &mut Executor<'_>) -> Tick {
        let tick = executor.scan();
        recorder.record_scan(executor, tick);
        tick
    }

    /// The served stream's `(seq, event)` pairs — the shape the gap
    /// and pinning assertions read.
    fn served(recorder: &Recorder, since: u64) -> Vec<(u64, DurableEvent)> {
        recorder
            .durable(since)
            .iter()
            .map(|entry| (entry.seq, entry.event.clone()))
            .collect()
    }

    #[test]
    fn declared_samples_append_at_cadence_and_replay_across_a_restart() {
        let dir = scratch("roundtrip");
        let path = dir.join("history.jsonl");
        let anchor = TickAnchor { epoch_ms: 42_000 };
        let driver = StubDriver::new(&[(PointId(10), Value::Float(1.0))]);

        // First lifetime: cadence-2 recording over five scans — the
        // first observation lands the standing census, then every
        // second scan. The executor carries the domain's anchor — the
        // recorder's bind anchor — so no `domain` seam fires.
        let mut recorder =
            Recorder::new(config(&path, 8), Tick::ZERO, Some(anchor)).unwrap();
        let mut executor = declared_executor(&driver, Some(2)).with_anchor(anchor);
        for n in 0..5_u64 {
            driver.set(PointId(10), Value::Float(n as f64));
            scan(&mut recorder, &mut executor);
        }
        recorder.flush_history_sink();
        assert_eq!(
            records(&path),
            vec![
                HistoryRecord::RunBoundary {
                    run: 1,
                    tick: Tick::ZERO,
                    anchor: Some(anchor),
                },
                HistoryRecord::Entry(Box::new(DurableEntry {
                    seq: 1,
                    tick: Tick(1),
                    event: DurableEvent::Sampled {
                        point: PointId(10),
                        sample: Sample::good(Value::Float(0.0), Tick(1)),
                    },
                })),
                HistoryRecord::Entry(Box::new(DurableEntry {
                    seq: 2,
                    tick: Tick(3),
                    event: DurableEvent::Sampled {
                        point: PointId(10),
                        sample: Sample::good(Value::Float(2.0), Tick(3)),
                    },
                })),
                HistoryRecord::Entry(Box::new(DurableEntry {
                    seq: 3,
                    tick: Tick(5),
                    event: DurableEvent::Sampled {
                        point: PointId(10),
                        sample: Sample::good(Value::Float(4.0), Tick(5)),
                    },
                })),
            ]
        );
        drop(recorder);

        // Second lifetime: the file's boundary marker — the restart's
        // served run_boundary entry taking the next seq — then the
        // replayed entries serve ahead of the new run's, numbering
        // continuing across the restart.
        let mut recorder =
            Recorder::new(config(&path, 8), Tick::ZERO, Some(anchor)).unwrap();
        // The restart's cold domain re-censuses: baselines belong to
        // the old tick domain, so the first scan records again.
        let mut executor = declared_executor(&driver, Some(2)).with_anchor(anchor);
        scan(&mut recorder, &mut executor);
        recorder.flush_history_sink();
        assert_eq!(
            served(&recorder, 0)
                .iter()
                .map(|(seq, _)| *seq)
                .collect::<Vec<_>>(),
            vec![1, 2, 3, 4, 5],
            "the served stream continues the file's seq axis"
        );
        assert_eq!(
            records(&path)[4],
            HistoryRecord::RunBoundary {
                run: 2,
                tick: Tick::ZERO,
                anchor: Some(anchor),
            },
            "the restart's boundary marker carries the domain's anchor"
        );
        assert_eq!(
            served(&recorder, 0)[3],
            (
                4,
                DurableEvent::RunBoundary {
                    run: 2,
                    anchor: Some(anchor),
                },
            ),
            "the marker's served entry attributes the lifetime"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn evictions_read_as_seq_gaps_and_markers_stay_pinned() {
        let dir = scratch("gap-honesty");
        let path = dir.join("history.jsonl");
        let driver = StubDriver::new(&[(PointId(10), Value::Float(0.0))]);

        // Run 1 floods ten cadence-1 records into an 8-entry window —
        // the served tail retains the last eight, the evicted stretch
        // reading as a numbering gap.
        let mut recorder = Recorder::new(config(&path, 8), Tick::ZERO, None).unwrap();
        let mut executor = declared_executor(&driver, Some(1));
        for _ in 0..10 {
            scan(&mut recorder, &mut executor);
        }
        assert_eq!(
            served(&recorder, 0)
                .iter()
                .map(|(seq, _)| *seq)
                .collect::<Vec<_>>(),
            vec![3, 4, 5, 6, 7, 8, 9, 10],
            "the bounded tail evicts oldest-first, seqs never reused"
        );
        drop(recorder);

        // Run 2: the restart's boundary entry takes seq 11, then the
        // flood evicts it — and every later marker — into the pinned
        // stream rather than dropping the attribution record.
        let mut recorder = Recorder::new(config(&path, 8), Tick::ZERO, None).unwrap();
        let mut executor = declared_executor(&driver, Some(1));
        for _ in 0..10 {
            scan(&mut recorder, &mut executor);
        }
        let entries = served(&recorder, 0);
        assert_eq!(
            entries[0],
            (11, DurableEvent::RunBoundary { run: 2, anchor: None }),
            "the pinned marker answers ahead of the retained tail"
        );
        assert_eq!(
            entries.iter().map(|(seq, _)| *seq).collect::<Vec<_>>(),
            vec![11, 14, 15, 16, 17, 18, 19, 20, 21],
            "the gap between the marker and the tail is the honest eviction"
        );
        // The since cursor still filters on seq — a consumer past the
        // marker sees only the retained tail.
        assert_eq!(
            recorder
                .durable(11)
                .iter()
                .map(|entry| entry.seq)
                .collect::<Vec<_>>(),
            vec![14, 15, 16, 17, 18, 19, 20, 21]
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_domain_adoption_seams_the_record_and_recensuses() {
        let dir = scratch("domain-seam");
        let path = dir.join("history.jsonl");
        let tracked = TickAnchor { epoch_ms: 7_000 };
        let driver = StubDriver::new(&[(PointId(10), Value::Float(0.0))]);

        // The tracking run binds unanchored, censuses, then adopts the
        // tracked line's domain — the next scan records the `domain`
        // seam carrying the adopted anchor before the first samples
        // attributed to it, and the cadence baselines reset so the
        // adoption's scan re-records the census.
        let mut recorder = Recorder::new(config(&path, 8), Tick::ZERO, None).unwrap();
        let mut executor = declared_executor(&driver, Some(4));
        scan(&mut recorder, &mut executor);
        executor = executor.with_anchor(tracked);
        scan(&mut recorder, &mut executor);
        let entries = served(&recorder, 0);
        assert_eq!(
            entries.iter().map(|(seq, _)| *seq).collect::<Vec<_>>(),
            vec![1, 2, 3]
        );
        assert_eq!(
            entries[1],
            (
                2,
                DurableEvent::Domain {
                    anchor: Some(tracked),
                },
            ),
            "the domain seam lands before the new domain's first sample"
        );
        assert_eq!(
            entries[2].1,
            DurableEvent::Sampled {
                point: PointId(10),
                sample: Sample::good(Value::Float(0.0), Tick(2)),
            },
            "the adoption's scan re-records the standing census"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_state_resume_continues_the_cadence_the_file_paced_out() {
        let dir = scratch("cadence-resume");
        let path = dir.join("history.jsonl");
        let anchor = TickAnchor { epoch_ms: 9_000 };
        let driver = StubDriver::new(&[(PointId(10), Value::Float(0.0))]);

        // Run 1 records at tick 1 of an 8-cadence — the standing
        // census — then runs to tick 6: the file's last attribution
        // is 1, the next record due at 9.
        let mut recorder =
            Recorder::new(config(&path, 8), Tick::ZERO, Some(anchor)).unwrap();
        let mut executor = declared_executor(&driver, Some(8)).with_anchor(anchor);
        for _ in 0..6 {
            scan(&mut recorder, &mut executor);
        }
        recorder.flush_history_sink();
        assert_eq!(
            records(&path)
                .iter()
                .filter(|record| matches!(record, HistoryRecord::Entry(_)))
                .count(),
            1
        );
        let persisted = executor.checkpoint();
        drop(recorder);

        // The --state-file resume: the restored checkpoint re-enters
        // the domain at tick 6 under the same anchor — the file's
        // cadence baseline (tick 1) is adopted, so ticks 7 and 8
        // record nothing and the interval the file already paced out
        // expires at tick 9.
        let mut recorder = Recorder::new(config(&path, 8), Tick(6), Some(anchor)).unwrap();
        let mut executor = declared_executor(&driver, Some(8));
        executor.apply(&persisted).unwrap();
        scan(&mut recorder, &mut executor);
        scan(&mut recorder, &mut executor);
        assert_eq!(
            served(&recorder, 1)
                .iter()
                .map(|(seq, _)| *seq)
                .collect::<Vec<_>>(),
            vec![2],
            "only the restart's boundary entry lands inside the interval"
        );
        scan(&mut recorder, &mut executor);
        assert_eq!(
            served(&recorder, 2).last().unwrap().1,
            DurableEvent::Sampled {
                point: PointId(10),
                sample: Sample::good(Value::Float(0.0), Tick(9)),
            },
            "the resumed run continues the interval the file paced out"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_corrupt_record_fails_replay_naming_the_file_and_the_record() {
        let dir = scratch("corrupt");
        let path = dir.join("history.jsonl");
        let driver = StubDriver::new(&[(PointId(10), Value::Float(0.0))]);

        let mut recorder = Recorder::new(config(&path, 8), Tick::ZERO, None).unwrap();
        let mut executor = declared_executor(&driver, Some(1));
        scan(&mut recorder, &mut executor);
        drop(recorder);

        // A torn line — the crash's partial record — fails the next
        // startup's replay naming the file and the offending content
        // rather than silently dropping the durable record.
        std::fs::write(
            &path,
            std::fs::read_to_string(&path).unwrap() + "{\"entry\":{\"seq\":\"x\"}}\n",
        )
        .unwrap();
        let error = Recorder::new(config(&path, 8), Tick::ZERO, None)
            .err()
            .expect("a corrupt record must fail the replay");
        let message = error.to_string();
        assert!(message.contains(path.to_str().unwrap()), "{message}");
        assert!(message.contains("line 3"), "{message}");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_seq_rewind_in_the_file_fails_replay() {
        let dir = scratch("seq-rewind");
        let path = dir.join("history.jsonl");
        let driver = StubDriver::new(&[(PointId(10), Value::Float(0.0))]);

        let mut recorder = Recorder::new(config(&path, 8), Tick::ZERO, None).unwrap();
        let mut executor = declared_executor(&driver, Some(1));
        scan(&mut recorder, &mut executor);
        drop(recorder);

        // A hand-edited or double-written file — a seq that rewinds —
        // is a corrupt record, not a gap: replay refuses it by name.
        let entry = DurableEntry {
            seq: 1,
            tick: Tick(2),
            event: DurableEvent::Sampled {
                point: PointId(10),
                sample: Sample::good(Value::Float(9.0), Tick(2)),
            },
        };
        let line = serde_json::to_string(&HistoryRecord::Entry(Box::new(entry))).unwrap();
        std::fs::write(&path, std::fs::read_to_string(&path).unwrap() + &line + "\n").unwrap();
        let error = Recorder::new(config(&path, 8), Tick::ZERO, None)
            .err()
            .expect("a rewound seq must fail the replay");
        assert!(error.to_string().contains("seq 1"), "{}", error);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_second_live_writer_on_the_path_fails_the_bind() {
        let dir = scratch("shared-writer");
        let path = dir.join("history.jsonl");

        let first = Recorder::new(config(&path, 8), Tick::ZERO, None).unwrap();
        let error = Recorder::new(config(&path, 8), Tick::ZERO, None)
            .err()
            .expect("two writers on one history file must not both bind");
        let message = error.to_string();
        assert!(message.contains(path.to_str().unwrap()), "{message}");
        assert!(message.contains("writer lock"), "{message}");
        drop(first);
        // The lock releases with the holder: an ordinary restart —
        // including a killed process's — re-acquires it immediately.
        Recorder::new(config(&path, 8), Tick::ZERO, None).unwrap();
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// #942's stalled sink, on the durable store's own drain: the
    /// recording point never waits on the writer — pushes return
    /// while the queue holds records, the standing health reads the
    /// named `lagging` state, and the push meeting the full queue is
    /// the run's fatal point rather than a lengthened scan.
    #[test]
    fn a_stalled_sink_reports_lagging_and_refuses_fatally_without_waiting() {
        use std::sync::Arc;
        use std::sync::atomic::{AtomicBool, Ordering};
        use std::sync::Condvar;
        use std::time::{Duration, Instant};

        let dir = scratch("stalled-sink");
        let path = dir.join("history.jsonl");
        let driver = StubDriver::new(&[(PointId(10), Value::Float(0.0))]);
        let (mut file, _replay) = HistoryFile::open(&path, 8, Tick::ZERO, None).unwrap();

        // The writer parks inside its first append behind the test's
        // gate — the stalled disk stand-in — while the bounded queue
        // fills behind it.
        let gate = Arc::new((Mutex::new(false), Condvar::new()));
        let entered = Arc::new(AtomicBool::new(false));
        let drain = Drain::new(format!("history file {}", path.display()), 2, {
            let gate = Arc::clone(&gate);
            let entered = Arc::clone(&entered);
            move |record| {
                entered.store(true, Ordering::SeqCst);
                let (lock, cvar) = &*gate;
                let mut open = lock.lock().unwrap();
                while !*open {
                    open = cvar.wait(open).unwrap();
                }
                drop(open);
                file.write(record)
            }
        });
        let mut recorder =
            Recorder::new(MonitorConfig::default(), Tick::ZERO, None).unwrap();
        recorder.with_history_sink(drain);
        let mut executor = declared_executor(&driver, Some(1));

        // The first record reaches the parked writer; the next two
        // scans' pushes still return — nothing in the recording path
        // waits on the sink.
        scan(&mut recorder, &mut executor);
        let deadline = Instant::now() + Duration::from_secs(10);
        while !entered.load(Ordering::SeqCst) {
            assert!(
                Instant::now() < deadline,
                "the drain writer never took the first record"
            );
            std::thread::sleep(Duration::from_millis(1));
        }
        let pushed = Instant::now();
        scan(&mut recorder, &mut executor);
        scan(&mut recorder, &mut executor);
        assert_eq!(
            recorder
                .store()
                .history_sink_health()
                .unwrap()
                .state,
            dcs_core::HistorySinkState::Lagging,
            "the sink's lag is the named health state while the queue holds records"
        );

        // The next recorded entry meets the full queue: the push is
        // refused and fatal at the recording point, naming the file
        // and the refused seq — the scan never waits.
        let panic = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            scan(&mut recorder, &mut executor);
        }));
        assert!(
            pushed.elapsed() < Duration::from_secs(1),
            "the pushes waited on the sink: the scan would stretch with it"
        );
        let message = panic_message(panic.expect_err("a full drain queue must refuse the push"));
        assert!(message.contains("history file"), "{message}");
        assert!(message.contains(path.to_str().unwrap()), "{message}");
        assert!(message.contains("refused durable history seq 4"), "{message}");

        // Releasing the sink drains the standing queue in order: the
        // file holds the three recorded entries behind the boundary
        // marker — no torn record, no gap.
        let (lock, cvar) = &*gate;
        *lock.lock().unwrap() = true;
        cvar.notify_all();
        recorder.flush_history_sink();
        let data = read_history_file(&path).unwrap();
        assert_eq!(data.boundaries.len(), 1);
        assert_eq!(
            data.entries
                .iter()
                .map(|entry| entry.seq)
                .collect::<Vec<_>>(),
            vec![1, 2, 3],
            "the durable record stays gap-free through the refused push"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// #942's failing sink, on the durable store: a write error on the
    /// writer fails the run at the next recorded append — refused
    /// naming the file — while the records the queue already took are
    /// counted `lost` and the file ends at the last durably appended
    /// record.
    #[test]
    fn a_failing_sink_is_fatal_at_the_recorded_point_without_a_torn_record() {
        use std::sync::Arc;
        use std::sync::atomic::{AtomicU64, Ordering};
        use std::time::{Duration, Instant};

        let dir = scratch("failing-sink");
        let path = dir.join("history.jsonl");
        let driver = StubDriver::new(&[(PointId(10), Value::Float(0.0))]);
        let (mut file, _replay) = HistoryFile::open(&path, 8, Tick::ZERO, None).unwrap();
        // The sink's second write fails — the error before the byte,
        // so no torn record is possible.
        let writes = Arc::new(AtomicU64::new(0));
        let fail_path = path.clone();
        let drain = Drain::new(
            format!("history file {}", path.display()),
            8,
            move |record| {
                if writes.fetch_add(1, Ordering::SeqCst) >= 1 {
                    return Err(named(
                        &fail_path,
                        "cannot append to history file",
                        "the simulated sink refuses",
                    ));
                }
                file.write(record)
            },
        );
        let mut recorder =
            Recorder::new(MonitorConfig::default(), Tick::ZERO, None).unwrap();
        recorder.with_history_sink(drain);
        let mut executor = declared_executor(&driver, Some(1));

        // Two recorded entries: the first appends, the second fails on
        // the writer. The third's push is the fatal refusal — the run
        // dies at the recorded point, never waiting on the sink.
        scan(&mut recorder, &mut executor);
        scan(&mut recorder, &mut executor);
        let deadline = Instant::now() + Duration::from_secs(10);
        while recorder
            .store()
            .history_sink_health()
            .map(|health| health.drained + health.lost < 2)
            .unwrap_or(true)
        {
            assert!(
                Instant::now() < deadline,
                "the writer never processed the queue"
            );
            std::thread::sleep(Duration::from_millis(1));
        }
        let pushed = Instant::now();
        let panic = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            scan(&mut recorder, &mut executor);
        }));
        assert!(
            pushed.elapsed() < Duration::from_secs(1),
            "the push waited on the failed sink"
        );
        let message =
            panic_message(panic.expect_err("a failed sink must refuse the next recorded append"));
        assert!(message.contains(path.to_str().unwrap()), "{message}");
        assert!(message.contains("refused durable history seq 3"), "{message}");

        // The failed record counts as lost, not silently dropped; the
        // file ends at the last durably appended entry.
        let health = recorder.store().history_sink_health().unwrap();
        assert_eq!(health.state, dcs_core::HistorySinkState::Failed);
        assert_eq!(health.lost, 1);
        let data = read_history_file(&path).unwrap();
        assert_eq!(
            data.entries
                .iter()
                .map(|entry| entry.seq)
                .collect::<Vec<_>>(),
            vec![1],
            "the file ends at the last durably appended record"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// Extracts the message a panicked push carried — the fatal
    /// refusal's text the assertions name.
    fn panic_message(payload: Box<dyn std::any::Any + Send>) -> String {
        payload
            .downcast::<String>()
            .map(|message| *message)
            .or_else(|payload| payload.downcast::<&'static str>().map(|s| s.to_string()))
            .unwrap_or_default()
    }
}
