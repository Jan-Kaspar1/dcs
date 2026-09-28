//! The durable process-history store's driven-run contract — the
//! durable-history decision's sibling file exercised end to end
//! through the monitor: declared-`record` points' post-scan samples
//! landing in the append-only history file at their declared cadence,
//! replayed across a monitor restart with `seq` numbering continuing
//! and run-boundary markers attributing each process lifetime, the
//! bounded served window evicting under the numbering-gap convention
//! with the markers pinned, and two identical driven runs producing
//! byte-identical store artifacts. The blocked-sink half of the
//! non-interference contract — a stalled or failing writer never
//! lengthening a scan, a full queue's push failing fatally — is
//! covered deterministically inside `src/history_file.rs`'s unit
//! tests through the drain-injection seam; here the end-to-end check
//! is that the store's presence never perturbs the run's
//! authoritative artifacts.

use dcs_core::{
    Direction, DurableEntry, DurableEvent, HistorySinkState, IoDriver, IoError, PointId, Sample,
    Tick, TickAnchor, Value, ValueKind,
};
use dcs_model::{PlantModel, SignalIndex};
use dcs_monitor::{Monitor, MonitorClient, MonitorConfig, read_history_file};
use dcs_runtime::{
    Component, ComponentIo, ComponentIoExt, Executor, IoRequirement, PointMap, PointSpec, StepError,
};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::thread;
use std::time::Duration;

/// The same minimal in-memory driver the other monitor tests use —
/// driver-served points whose values the test steps — plus the
/// field's write log the non-interference comparison reads.
struct StubDriver {
    points: Mutex<HashMap<PointId, Sample>>,
    writes: Mutex<Vec<(PointId, Value)>>,
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
            writes: Mutex::new(Vec::new()),
        }
    }

    fn set(&self, point: PointId, value: Value) {
        self.points
            .lock()
            .unwrap()
            .insert(point, Sample::good(value, Tick::ZERO));
    }

    /// Every value the executor ever issued to the field, in issue
    /// order — the scan output image's field-side record the
    /// byte-identity comparison reads most directly.
    fn writes(&self) -> Vec<(PointId, Value)> {
        self.writes.lock().unwrap().clone()
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
        self.writes.lock().unwrap().push((point, value));
        Ok(())
    }
}

/// Reads `In` point 10 and drives `Out` point 20 at gain 2 — the
/// minimal component the non-interference comparison's field-write
/// log needs, the same shape the other monitor suites use.
struct Scale;

impl Component for Scale {
    fn name(&self) -> &str {
        "scale"
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        vec![
            IoRequirement::input::<f64>("in", PointId(10)),
            IoRequirement::output::<f64>("out", PointId(20)),
        ]
    }

    fn step(&mut self, io: &dyn ComponentIo, _tick: Tick) -> Result<(), StepError> {
        let sample = io.read_typed::<f64>(PointId(10))?;
        io.write_typed(PointId(20), sample.value * 2.0)?;
        Ok(())
    }
}

fn components() -> Vec<Box<dyn Component>> {
    vec![Box::new(Scale)]
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
        std::env::temp_dir().join(format!("dcs-monitor-history-{test}-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

/// The point map: driver `In` point 10 carrying the declared durable
/// recording duty `cadence` (`None` leaves it to the volatile ring),
/// and driver `Out` point 20 the `scale` component writes.
fn declared_map(cadence: Option<u64>) -> PointMap {
    PointMap::new()
        .with_spec(
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
        )
        .with_point(PointId(20), Direction::Out, ValueKind::Float)
}

/// Runs `body` while `monitor` serves, always shutting the server
/// down before the driver's borrow ends — the same helper the
/// publication and feed-state tests use. A monitor serves once, so
/// every read a check needs happens inside `body` against one
/// session's `MonitorClient`.
fn serving<T>(monitor: &Monitor<'_>, body: impl FnOnce() -> T) -> T {
    let result = thread::scope(|scope| {
        scope.spawn(|| monitor.serve());
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(body));
        monitor.shutdown();
        result
    });
    result.unwrap_or_else(|panic| std::panic::resume_unwind(panic))
}

/// The served stream's `seq`s — the shape the gap and pinning
/// assertions read.
fn seqs(entries: &[DurableEntry]) -> Vec<u64> {
    entries.iter().map(|entry| entry.seq).collect()
}

/// The recorded-datasets contract: declared-duty samples persist
/// across a controller restart and re-serve with the decision's
/// attribution marks — the run-boundary entry carrying the new
/// lifetime — while `seq` numbering continues the file's axis and a
/// cold restart re-censuses under its new tick domain.
#[test]
fn recorded_samples_persist_and_re_serve_across_a_restart() {
    let dir = scratch("restart-continuity");
    let path = dir.join("history.jsonl");
    let driver = StubDriver::new(&[
        (PointId(10), Value::Float(0.0)),
        (PointId(20), Value::Float(0.0)),
    ]);
    let bind = || {
        let executor = Executor::new(&driver, declared_map(Some(2)), Vec::new()).unwrap();
        Monitor::bind_with(
            "127.0.0.1:0",
            executor,
            signal_index(),
            MonitorConfig {
                history_file: Some(path.clone()),
                ..MonitorConfig::default()
            },
        )
        .unwrap()
    };

    // First lifetime: cadence-2 recording over five driven scans —
    // the standing census at tick 1, then every second scan. The
    // served endpoint attests the drain caught up before answering.
    let first = bind();
    first.paced_scan();
    for n in 1..5_u64 {
        driver.set(PointId(10), Value::Float(n as f64));
        first.paced_scan();
    }
    serving(&first, || {
        let client = MonitorClient::new(first.local_addr());
        assert_eq!(
            seqs(&client.durable_history(&[], 0).unwrap()),
            vec![1, 2, 3],
            "the first lifetime records the census then every cadence"
        );
    });
    let health = first
        .flush_history_sink(Duration::from_secs(10))
        .expect("a history-file monitor reports the sink's drain");
    assert_eq!(health.state, HistorySinkState::Healthy);
    assert_eq!(health.drained, health.accepted);
    drop(first);

    // The restart's lifetime boundary is the served marker attributing
    // the new run — the replayed entries answering ahead of it, `seq`
    // continuing the file's axis. A cold restart's new tick domain
    // re-censuses: the old domain's cadence baselines mean nothing
    // under it, so the first scan of run 2 records again at its own
    // tick 1.
    let second = bind();
    serving(&second, || {
        let client = MonitorClient::new(second.local_addr());
        let entries = client.durable_history(&[], 0).unwrap();
        assert_eq!(seqs(&entries), vec![1, 2, 3, 4]);
        assert_eq!(
            entries[3].event,
            DurableEvent::RunBoundary {
                run: 2,
                anchor: None,
            },
            "the unanchored restart's boundary entry attributes the lifetime"
        );
        second.paced_scan();
        let entries = client.durable_history(&[], 0).unwrap();
        assert_eq!(seqs(&entries), vec![1, 2, 3, 4, 5]);
        assert_eq!(entries[4].tick, Tick(1));
    });
    drop(second);

    // The file itself: two run-boundary markers — the served
    // markers' file form — so each lifetime's tick domain stays
    // attributable, the unanchored domains recording no anchor.
    let data = read_history_file(&path).unwrap();
    assert_eq!(data.boundaries.len(), 2);
    assert_eq!(data.boundaries[1].run, 2);
    assert_eq!(data.boundaries[1].anchor, None);
    let _ = std::fs::remove_dir_all(&dir);
}

/// The declared tick-to-civil anchor, applied: a run whose executor
/// carries the tick domain's anchor stamps it into the file's
/// boundary marker and the served `run_boundary` entry — every
/// exported record self-describing its tick-to-civil mapping.
#[test]
fn the_anchor_stamps_the_boundary_markers_and_the_served_entries() {
    let dir = scratch("anchored");
    let path = dir.join("history.jsonl");
    let anchor = TickAnchor {
        epoch_ms: 7_654_321,
    };
    let driver = StubDriver::new(&[
        (PointId(10), Value::Float(0.0)),
        (PointId(20), Value::Float(0.0)),
    ]);
    let bind = || {
        let executor = Executor::new(&driver, declared_map(Some(1)), Vec::new())
            .unwrap()
            .with_anchor(anchor);
        Monitor::bind_with(
            "127.0.0.1:0",
            executor,
            signal_index(),
            MonitorConfig {
                history_file: Some(path.clone()),
                ..MonitorConfig::default()
            },
        )
        .unwrap()
    };

    bind().paced_scan();
    // A second lifetime on the same anchored domain: the restart's
    // served boundary entry and the file's marker both carry the
    // domain's anchor.
    let second = bind();
    serving(&second, || {
        let client = MonitorClient::new(second.local_addr());
        let entries = client.durable_history(&[], 0).unwrap();
        assert_eq!(
            entries.last().unwrap().event,
            DurableEvent::RunBoundary {
                run: 2,
                anchor: Some(anchor),
            },
            "the served restart marker carries the domain's anchor"
        );
    });
    drop(second);
    let data = read_history_file(&path).unwrap();
    assert_eq!(data.boundaries[0].anchor, Some(anchor));
    assert_eq!(data.boundaries[1].anchor, Some(anchor));
    let _ = std::fs::remove_dir_all(&dir);
}

/// Retention eviction is bounded and honest: sample volume past the
/// served window's bound drops the oldest entries, the evicted
/// stretch reading to a since-cursor consumer as a numbering gap —
/// while the restart's `run_boundary` marker, evicted by the same
/// flood, stays pinned ahead of the retained tail so the lifetime
/// attribution survives.
#[test]
fn retention_eviction_reads_as_seq_gaps_and_the_marker_stays_pinned() {
    let dir = scratch("gap-honesty");
    let path = dir.join("history.jsonl");
    let driver = StubDriver::new(&[
        (PointId(10), Value::Float(0.0)),
        (PointId(20), Value::Float(0.0)),
    ]);
    let bind = || {
        let executor = Executor::new(&driver, declared_map(Some(1)), Vec::new()).unwrap();
        Monitor::bind_with(
            "127.0.0.1:0",
            executor,
            signal_index(),
            MonitorConfig {
                history_file: Some(path.clone()),
                durable_capacity: 4,
                ..MonitorConfig::default()
            },
        )
        .unwrap()
    };

    // Run 1 floods eight cadence-1 records into a 4-entry window.
    let first = bind();
    for _ in 0..8 {
        first.paced_scan();
    }
    serving(&first, || {
        let client = MonitorClient::new(first.local_addr());
        assert_eq!(
            seqs(&client.durable_history(&[], 0).unwrap()),
            vec![5, 6, 7, 8],
            "the bounded tail evicts oldest-first, seqs never reused"
        );
    });
    drop(first);

    // Run 2: the restart's boundary entry takes seq 9, then the next
    // flood evicts it into the pinned stream — the served answer
    // keeps the lifetime marker ahead of the retained tail with the
    // evicted stretch reading as the numbering gap it is.
    let second = bind();
    for _ in 0..8 {
        second.paced_scan();
    }
    serving(&second, || {
        let client = MonitorClient::new(second.local_addr());
        let entries = client.durable_history(&[], 0).unwrap();
        assert_eq!(seqs(&entries), vec![9, 14, 15, 16, 17]);
        assert_eq!(
            entries[0].event,
            DurableEvent::RunBoundary {
                run: 2,
                anchor: None,
            },
            "the pinned marker answers ahead of the tail"
        );
        // The since cursor still filters on seq: a consumer past the
        // marker sees only the retained tail; `point` filtering
        // narrows the sampled records without dropping the marker.
        assert_eq!(
            seqs(&client.durable_history(&[], 9).unwrap()),
            vec![14, 15, 16, 17]
        );
        assert_eq!(
            seqs(&client.durable_history(&[PointId(10)], 0).unwrap()),
            vec![9, 14, 15, 16, 17]
        );
    });
    drop(second);

    // The file never lost any of it: the durable artifact holds every
    // recorded entry contiguously — eviction bounds the served
    // window, never the record.
    let data = read_history_file(&path).unwrap();
    assert_eq!(
        data.entries
            .iter()
            .map(|entry| entry.seq)
            .collect::<Vec<_>>(),
        (1..=17).collect::<Vec<_>>(),
        "the file's record stays gap-free under served-window eviction"
    );
    let _ = std::fs::remove_dir_all(&dir);
}

/// A `--state-file`-style resume: the run's checkpointed tick domain
/// and anchor continue through `Executor::restore`, the restarted
/// monitor replays the same file, and the cadence interval the file
/// already paced out carries across the restart rather than
/// re-recording the standing census.
#[test]
fn a_state_resume_continues_the_cadence_the_file_paced_out() {
    let dir = scratch("cadence-resume");
    let path = dir.join("history.jsonl");
    let anchor = TickAnchor { epoch_ms: 9_000 };
    let driver = StubDriver::new(&[
        (PointId(10), Value::Float(0.0)),
        (PointId(20), Value::Float(0.0)),
    ]);

    // Run 1 on the anchored domain: the standing census records at
    // tick 1 of an 8-cadence, the run scans to tick 6, and the
    // checkpoint captures the resumable state.
    let executor = Executor::new(&driver, declared_map(Some(8)), Vec::new())
        .unwrap()
        .with_anchor(anchor);
    let first = Monitor::bind_with(
        "127.0.0.1:0",
        executor,
        signal_index(),
        MonitorConfig {
            history_file: Some(path.clone()),
            ..MonitorConfig::default()
        },
    )
    .unwrap();
    for _ in 0..6 {
        first.paced_scan();
    }
    let persisted = first.checkpoint();
    first.flush_history_sink(Duration::from_secs(10));
    assert_eq!(read_history_file(&path).unwrap().entries.len(), 1);
    drop(first);

    // The resume: the restored executor re-enters the domain at tick
    // 6 under the same anchor. Scans 7 and 8 record nothing — the
    // interval the file paced out expires at tick 9 — while the
    // restart's boundary entry stands between the lifetimes.
    let restored =
        Executor::restore(&driver, declared_map(Some(8)), Vec::new(), &persisted, None).unwrap();
    let second = Monitor::bind_with(
        "127.0.0.1:0",
        restored,
        signal_index(),
        MonitorConfig {
            history_file: Some(path.clone()),
            ..MonitorConfig::default()
        },
    )
    .unwrap();
    serving(&second, || {
        let client = MonitorClient::new(second.local_addr());
        second.paced_scan();
        second.paced_scan();
        assert_eq!(
            seqs(&client.durable_history(&[], 1).unwrap()),
            vec![2],
            "only the restart's boundary entry lands inside the interval"
        );
        second.paced_scan();
        let entries = client.durable_history(&[], 2).unwrap();
        assert_eq!(entries.last().unwrap().tick, Tick(9));
        assert_eq!(
            entries.last().unwrap().event,
            DurableEvent::Sampled {
                point: PointId(10),
                sample: Sample::good(Value::Float(0.0), Tick(9)),
            },
            "the resumed run records when the file-paced interval expires"
        );
    });
    drop(second);
    let _ = std::fs::remove_dir_all(&dir);
}

/// Scan-path non-interference at the driven-run level: the same
/// input script run twice — once with the durable store configured,
/// once without — produces byte-identical authoritative artifacts:
/// the field's write log and the final checkpoint's serialized
/// bytes. The store records off the scan's results and never reaches
/// back into them.
#[test]
fn the_store_never_perturbs_the_runs_authoritative_artifacts() {
    let artifacts = |scratch_dir: &Path, with_store: bool| {
        let driver = StubDriver::new(&[
            (PointId(10), Value::Float(0.0)),
            (PointId(20), Value::Float(0.0)),
        ]);
        let executor = Executor::new(&driver, declared_map(Some(1)), components()).unwrap();
        let monitor = Monitor::bind_with(
            "127.0.0.1:0",
            executor,
            signal_index(),
            MonitorConfig {
                history_file: with_store.then(|| scratch_dir.join("history.jsonl")),
                ..MonitorConfig::default()
            },
        )
        .unwrap();
        // The deterministic script: a standing value, a stepped
        // value, scans between — every scan recording under the
        // store's arm.
        monitor.paced_scan();
        driver.set(PointId(10), Value::Float(4.0));
        monitor.paced_scan();
        monitor.paced_scan();
        let checkpoint = serde_json::to_string(&monitor.checkpoint()).unwrap();
        if with_store {
            monitor.flush_history_sink(Duration::from_secs(10));
        }
        let writes = driver.writes();
        drop(monitor);
        (checkpoint, writes)
    };

    let dir = scratch("noninterference");
    let with_store = artifacts(&dir, true);
    let without_store = artifacts(&dir, false);
    assert_eq!(
        with_store, without_store,
        "the durable store's presence must not touch the run's artifacts"
    );
    let _ = std::fs::remove_dir_all(&dir);
}

/// Two identical driven runs produce byte-identical store artifacts:
/// the same script under the same tick domain lands the same records
/// — boundary markers, seqs, ticks, and samples — so the artifact
/// the export seam later consumes is a function of the script alone.
#[test]
fn two_identical_driven_runs_produce_byte_identical_store_artifacts() {
    let run = |dir: &Path| {
        let driver = StubDriver::new(&[
            (PointId(10), Value::Float(0.0)),
            (PointId(20), Value::Float(0.0)),
        ]);
        let path = dir.join("history.jsonl");
        let bind = || {
            let executor = Executor::new(&driver, declared_map(Some(2)), Vec::new()).unwrap();
            Monitor::bind_with(
                "127.0.0.1:0",
                executor,
                signal_index(),
                MonitorConfig {
                    history_file: Some(path.clone()),
                    ..MonitorConfig::default()
                },
            )
            .unwrap()
        };
        let first = bind();
        for n in 0..6_u64 {
            driver.set(PointId(10), Value::Float(n as f64));
            first.paced_scan();
        }
        drop(first);
        // A restart mid-script, so the artifact comparison covers
        // boundary markers and the replay-continued seq axis too.
        let second = bind();
        for n in 6..10_u64 {
            driver.set(PointId(10), Value::Float(n as f64));
            second.paced_scan();
        }
        second.flush_history_sink(Duration::from_secs(10));
        drop(second);
        path
    };

    let dir_a = scratch("determinism-a");
    let dir_b = scratch("determinism-b");
    let path_a = run(&dir_a);
    let path_b = run(&dir_b);
    assert_eq!(
        std::fs::read(&path_a).unwrap(),
        std::fs::read(&path_b).unwrap(),
        "identical driven runs must produce byte-identical history files"
    );
    let _ = std::fs::remove_dir_all(&dir_a);
    let _ = std::fs::remove_dir_all(&dir_b);
}
