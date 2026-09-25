//! The `--state-file` checkpoint sink's durability contract at the
//! monitor (#982): the write drains on the sink's own bounded writer
//! off the executor lock, while the answers whose contract promises
//! the file caught up — an accepted `POST /command`'s `200`, a driven
//! `POST /scan` batch's — wait their pushed ordinal out on the
//! request's worker before answering, so a restart after the answer
//! resumes through every effect it covered. The drain's own stalled-,
//! failing-, and ordering-writer coverage lives beside it in
//! `state_file.rs`'s unit tests; these pin the serving layer's end of
//! the seam and the publication's `state_sink` health stamp.

use dcs_core::{
    Command, CommandOutcome, Direction, IoDriver, IoError, PointId, Sample, StateSinkState, Tick,
    Value, ValueKind,
};
use dcs_model::{PlantModel, SignalIndex};
use dcs_monitor::{Driven, Monitor, MonitorClient, MonitorConfig};
use dcs_runtime::{Checkpoint, Executor, PointMap};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::thread;
use std::time::Duration;

/// The same minimal in-memory driver the other monitor tests use.
struct StubDriver {
    points: Mutex<HashMap<PointId, Sample>>,
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
    let dir = std::env::temp_dir().join(format!("dcs-monitor-state-{test}-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

/// The persisted checkpoint the resume path would read — the serde
/// document the file has always carried.
fn persisted(path: &Path) -> Checkpoint {
    serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap()
}

/// Runs `body` against a client while `monitor` serves, then stops the
/// serving loop — one serving session per call.
fn served<T>(monitor: &Monitor<'_>, body: impl FnOnce(&MonitorClient) -> T) -> T {
    thread::scope(|scope| {
        scope.spawn(|| monitor.serve());
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            body(&MonitorClient::new(monitor.local_addr()))
        }));
        monitor.shutdown();
        result.unwrap_or_else(|panic| std::panic::resume_unwind(panic))
    })
}

/// #982's admission-durability ordering at the new seam: an accepted
/// `POST /command`'s `200` answers only after the checkpoint carrying
/// its receipt has atomically replaced the state file — the
/// attest-through-ordinal wait on the request worker, the lock long
/// released — so the file the request's answer attests already
/// re-queues the carried receipt across a restart.
#[test]
fn an_accepted_command_is_durable_before_its_receipt_answers() {
    let dir = scratch("admission");
    let path = dir.join("state.json");
    let driver = StubDriver {
        points: Mutex::new(
            [(PointId(10), Sample::good(Value::Float(0.0), Tick::ZERO))]
                .into_iter()
                .collect(),
        ),
    };
    let map = PointMap::new().with_writable_point(PointId(10), Direction::In, ValueKind::Float);
    let executor = Executor::new(&driver, map, Vec::new()).unwrap();
    let monitor = Monitor::bind_with(
        "127.0.0.1:0",
        executor,
        signal_index(),
        MonitorConfig {
            state_file: Some(path.clone()),
            ..MonitorConfig::default()
        },
    )
    .unwrap();

    let command = Command::WriteValue {
        point: PointId(10),
        kind: ValueKind::Float,
        value: Value::Float(3.5),
    };
    served(&monitor, |client| {
        let receipt = client.command(&command).unwrap();
        assert!(
            matches!(receipt.outcome, CommandOutcome::Accepted { .. }),
            "the command must be accepted for its durability to be owed: {receipt:?}"
        );
        // The `200` already answered — the file must hold the
        // checkpoint carrying this receipt, the restart path's record
        // of the admission it promised.
        let checkpoint = persisted(&path);
        assert!(
            checkpoint.receipts.iter().any(|entry| entry == &receipt),
            "the durable checkpoint does not carry the answered receipt: {checkpoint:?}"
        );
        assert_eq!(
            checkpoint.format_version,
            dcs_runtime::CHECKPOINT_FORMAT_VERSION,
            "the checkpoint format is unchanged"
        );
    });
    let _ = std::fs::remove_dir_all(&dir);
}

/// #982's driven half: a `POST /scan` batch answers only after the
/// file caught up through the batch's last pushed checkpoint — every
/// scan the batch ran resumable across a restart that follows the
/// answer, the FIFO making the last ordinal cover them all.
#[test]
fn a_driven_scan_batch_is_durable_before_its_answer() {
    let dir = scratch("driven-batch");
    let path = dir.join("state.json");
    let driver = StubDriver {
        points: Mutex::new(
            [(PointId(10), Sample::good(Value::Float(0.0), Tick::ZERO))]
                .into_iter()
                .collect(),
        ),
    };
    let map = PointMap::new().with_writable_point(PointId(10), Direction::In, ValueKind::Float);
    let executor = Executor::new(&driver, map, Vec::new()).unwrap();
    let monitor = Monitor::bind_with(
        "127.0.0.1:0",
        executor,
        signal_index(),
        MonitorConfig {
            state_file: Some(path.clone()),
            ..MonitorConfig::default()
        },
    )
    .unwrap()
    .driven(Driven {
        track: None,
        after_scan: None,
    });

    served(&monitor, |client| {
        client.advance(4).unwrap();
        // The batch's answer already attested the durable file:
        // `200` means the last capture's replace landed.
        assert_eq!(persisted(&path).tick, Tick(4));
        // The publication carries the sink's health section — the
        // stamp is the publish-instant read (the queue legitimately
        // lagging mid-batch), while the live health after the
        // attested answer reports the drain caught up.
        let snapshot = client.snapshot().unwrap();
        assert!(
            snapshot
                .publication
                .expect("the monitor stamps its publication health")
                .state_sink
                .is_some(),
            "a state-file run stamps the sink's health into its publications"
        );
    });
    let health = monitor.state_sink_health().unwrap();
    assert_eq!(health.state, StateSinkState::Healthy);
    assert_eq!(health.drained, health.accepted);
    assert_eq!(health.lost, 0);
    let _ = std::fs::remove_dir_all(&dir);
}

/// The paced path's end: `persist_state` only queues the capture — the
/// caller's flush attests the file caught up — while a monitor with no
/// `state_file` serves snapshots carrying no `state_sink` section.
#[test]
fn the_paced_persist_queues_and_the_health_reports_by_name() {
    let dir = scratch("paced");
    let path = dir.join("state.json");
    let driver = StubDriver {
        points: Mutex::new(
            [(PointId(10), Sample::good(Value::Float(0.0), Tick::ZERO))]
                .into_iter()
                .collect(),
        ),
    };
    let map = || PointMap::new().with_point(PointId(10), Direction::In, ValueKind::Float);
    let executor = Executor::new(&driver, map(), Vec::new()).unwrap();
    let monitor = Monitor::bind_with(
        "127.0.0.1:0",
        executor,
        signal_index(),
        MonitorConfig {
            state_file: Some(path.clone()),
            ..MonitorConfig::default()
        },
    )
    .unwrap();

    monitor.paced_scan();
    monitor.paced_scan();
    monitor.persist_state().unwrap();
    monitor.flush_state_sink(Duration::from_secs(10));
    assert_eq!(persisted(&path).tick, monitor.tick());
    let health = monitor.state_sink_health().unwrap();
    assert_eq!(health.state, StateSinkState::Healthy);
    assert_eq!(health.drained, health.accepted);
    drop(monitor);

    // No state file configured: no sink, no stamped section, the
    // persist a no-op.
    let executor = Executor::new(&driver, map(), Vec::new()).unwrap();
    let monitor = Monitor::bind_with(
        "127.0.0.1:0",
        executor,
        signal_index(),
        MonitorConfig::default(),
    )
    .unwrap();
    monitor.paced_scan();
    monitor.persist_state().unwrap();
    assert!(monitor.state_sink_health().is_none());
    assert!(served(&monitor, |client| client
        .snapshot()
        .unwrap()
        .publication
        .unwrap()
        .state_sink
        .is_none()));
    let _ = std::fs::remove_dir_all(&dir);
}
