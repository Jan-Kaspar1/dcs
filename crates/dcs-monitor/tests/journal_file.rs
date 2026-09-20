//! The durable journal file's single-writer contract at the monitor
//! bind: two monitors configured with the same `journal_file` path —
//! the shared-sink misconfiguration — cannot both run, because each
//! recorder would continue `seq` numbering from its own replay point
//! and interleave duplicate `seq`s into a record no later startup can
//! replay. The second bind fails naming the file and the live-holder
//! conflict; once the holder is gone the file replays for the next
//! writer, `seq` numbering continuing across the run boundary.

use dcs_core::{Direction, IoDriver, IoError, PointId, Sample, Tick, Value, ValueKind};
use dcs_model::{PlantModel, SignalIndex};
use dcs_monitor::{Monitor, MonitorConfig, read_journal_file};
use dcs_runtime::{Executor, PointMap};
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Mutex;

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
