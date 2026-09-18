//! The published read-model seam: each completed scan materializes one
//! immutable [`Publication`] into the bounded store the read endpoints
//! serve — outside the executor lock — with sequence, tick, and
//! gap/coalescing semantics for lagging consumers and bounded storage
//! with no consumers at all.

use dcs_core::{
    Direction, DriverDiagnostics, IoDriver, IoError, PointId, Sample, Tick, Value, ValueKind,
};
use dcs_model::{PlantModel, SignalIndex};
use dcs_monitor::{Monitor, MonitorClient, MonitorConfig, PublicationGap};
use dcs_runtime::{
    Component, ComponentIo, ComponentIoExt, Executor, IoRequirement, Peer, PointMap, StepError,
};
use std::collections::{HashMap, HashSet};
use std::io::{Read, Write};
use std::net::TcpStream;
use std::sync::Mutex;
use std::sync::atomic::{AtomicU64, Ordering};
use std::thread;
use std::time::{Duration, Instant};

/// In-memory driver stub; `snapshot_calls` counts `diagnostics` calls —
/// the executor's only call site for it is `Executor::snapshot`, so the
/// counter proves how often a snapshot is materialized.
struct StubDriver {
    points: Mutex<HashMap<PointId, Sample>>,
    faults: Mutex<HashSet<PointId>>,
    snapshot_calls: AtomicU64,
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
            faults: Mutex::new(HashSet::new()),
            snapshot_calls: AtomicU64::new(0),
        }
    }
}

impl IoDriver for StubDriver {
    fn read(&self, point: PointId) -> Result<Sample, IoError> {
        if self.faults.lock().unwrap().contains(&point) {
            return Err(IoError::Disconnected(point));
        }
        self.points
            .lock()
            .unwrap()
            .get(&point)
            .copied()
            .ok_or(IoError::UnknownPoint(point))
    }

    fn write(&self, point: PointId, value: Value) -> Result<(), IoError> {
        if self.faults.lock().unwrap().contains(&point) {
            return Err(IoError::Disconnected(point));
        }
        let mut points = self.points.lock().unwrap();
        let sample = points.get_mut(&point).ok_or(IoError::UnknownPoint(point))?;
        if value.kind() != sample.value.kind() {
            return Err(IoError::TypeMismatch {
                point,
                expected: sample.value.kind(),
                found: value,
            });
        }
        *sample = Sample::good(value, Tick::ZERO);
        Ok(())
    }

    /// `Executor::snapshot` asks the driver for transport diagnostics
    /// exactly once per construction — counting the calls instruments
    /// snapshot materialization without touching the executor.
    fn diagnostics(&self) -> Option<DriverDiagnostics> {
        self.snapshot_calls.fetch_add(1, Ordering::Relaxed);
        None
    }
}

/// Reads `In` point 10 and drives `Out` point 20 at gain 2; point 30 is a
/// mapped `Out` point no component writes — the same minimal component
/// the other monitor suites use.
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

/// The model fixture behind the monitor: points 10/20/30 match the rig's
/// point map, and signals give points 10 and 20 names and units.
const MODEL: &str = include_str!("../fixtures/monitor.json");

fn signal_index() -> SignalIndex {
    PlantModel::load(MODEL).unwrap().signal_index()
}

fn rig() -> (StubDriver, PointMap) {
    let driver = StubDriver::new(&[
        (PointId(10), Value::Float(0.0)),
        (PointId(20), Value::Float(0.0)),
        (PointId(30), Value::Float(0.0)),
    ]);
    let map = PointMap::new()
        .with_writable_point(PointId(10), Direction::In, ValueKind::Float)
        .with_point(PointId(20), Direction::Out, ValueKind::Float)
        .with_point(PointId(30), Direction::Out, ValueKind::Float);
    (driver, map)
}

/// Runs `body` while `monitor` serves, always shutting the server down
/// before the driver's borrow ends — a failing assertion must not
/// deadlock the scope join.
fn serving<T>(monitor: &Monitor<'_>, body: impl FnOnce() -> T) -> T {
    let result = thread::scope(|scope| {
        scope.spawn(|| monitor.serve());
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(body));
        monitor.shutdown();
        result
    });
    result.unwrap_or_else(|panic| std::panic::resume_unwind(panic))
}

#[test]
fn consecutive_scans_publish_distinct_immutable_read_models() {
    let (driver, map) = rig();
    let executor = Executor::new(&driver, map, vec![Box::new(Scale)]).unwrap();
    let monitor = Monitor::bind("127.0.0.1:0", executor, signal_index()).unwrap();
    let client = MonitorClient::new(monitor.local_addr());
    serving(&monitor, || {
        // The bind-time seed is publication 1; each completed scan
        // publishes the next seq at the scan's tick.
        let seed = monitor.published().unwrap();
        assert_eq!(seed.seq, 1);
        assert_eq!(seed.tick, Tick::ZERO);

        client.advance(1).unwrap();
        let first = monitor.published().unwrap();
        client.advance(1).unwrap();
        let second = monitor.published().unwrap();

        // Two scans, two distinct immutable publications.
        assert_eq!((first.seq, first.tick), (2, Tick(1)));
        assert_eq!((second.seq, second.tick), (3, Tick(2)));
        assert!(!std::sync::Arc::ptr_eq(&first, &second));
        assert_ne!(first.snapshot, second.snapshot);

        // Each publication carries the appended deltas: every scan adds
        // a fresh sample per point to the history delta, and the
        // first-observation quality transitions journal at the scan
        // that recorded them.
        assert!(!first.journal.is_empty());
        assert!(
            second
                .history
                .iter()
                .any(|history| history.point == PointId(10) && !history.samples.is_empty())
        );

        // The retained earlier publication is the same allocation,
        // unchanged by the later scan — immutability by construction.
        let page = monitor.publications_since(1);
        assert!(page.gap.is_none());
        assert_eq!(page.publications.len(), 2);
        assert!(std::sync::Arc::ptr_eq(&page.publications[0], &first));
        assert!(std::sync::Arc::ptr_eq(&page.publications[1], &second));
        assert_eq!(first.tick, Tick(1));
        assert_eq!(
            monitor.published().unwrap().seq,
            3,
            "the retained window reads never re-publish"
        );
    });
}

#[test]
fn snapshot_reads_do_not_reconstruct_the_snapshot() {
    let (driver, map) = rig();
    let executor = Executor::new(&driver, map, vec![Box::new(Scale)]).unwrap();
    let monitor = Monitor::bind("127.0.0.1:0", executor, signal_index()).unwrap();
    let client = MonitorClient::new(monitor.local_addr());
    serving(&monitor, || {
        // Bind materialized exactly one snapshot — the seed read model.
        assert_eq!(driver.snapshot_calls.load(Ordering::Relaxed), 1);

        // Repeated reads serve the published copy: no construction.
        for _ in 0..8 {
            client.snapshot().unwrap();
        }
        assert_eq!(driver.snapshot_calls.load(Ordering::Relaxed), 1);

        // Two scans build exactly two more — one per completed scan,
        // regardless of request count around them.
        client.advance(1).unwrap();
        for _ in 0..4 {
            client.snapshot().unwrap();
        }
        client.advance(1).unwrap();
        for _ in 0..4 {
            client.snapshot().unwrap();
        }
        assert_eq!(driver.snapshot_calls.load(Ordering::Relaxed), 3);

        // And the POST /scan answer is the same published copy the
        // next GET serves.
        let advanced = client.advance(1).unwrap();
        let served = client.snapshot().unwrap();
        assert_eq!(advanced, served);
        assert_eq!(served.publication.unwrap().published, 4);
    });
}

#[test]
fn a_lagging_cursor_observes_the_named_gap() {
    let (driver, map) = rig();
    let executor = Executor::new(&driver, map, vec![Box::new(Scale)]).unwrap();
    let monitor = Monitor::bind_with(
        "127.0.0.1:0",
        executor,
        signal_index(),
        MonitorConfig {
            publication_capacity: 3,
            ..MonitorConfig::default()
        },
    )
    .unwrap();
    let client = MonitorClient::new(monitor.local_addr());
    serving(&monitor, || {
        // Seed (seq 1) plus six scans: publications 2..=7; the
        // capacity-3 window retains 5..=7.
        client.advance(6).unwrap();

        // A consumer that read publication 2 and comes back now finds
        // its successors 3..=4 evicted: the named gap reports the lost
        // stretch and the page coalesces onto the retained tail.
        let page = monitor.publications_since(2);
        assert_eq!(page.gap, Some(PublicationGap { through: 4 }));
        assert_eq!(
            page.publications
                .iter()
                .map(|publication| publication.seq)
                .collect::<Vec<_>>(),
            vec![5, 6, 7]
        );

        // Inside the window there is no gap; at the head there is
        // nothing newer.
        assert_eq!(
            monitor.publications_since(6).gap,
            None,
            "a cursor inside the window pages without a gap"
        );
        let head = monitor.publications_since(7);
        assert!(head.gap.is_none() && head.publications.is_empty());

        // The same accounting rides the served snapshot's publication
        // section — the documented diagnostics placement.
        let health = client.snapshot().unwrap().publication.unwrap();
        assert_eq!(health.published, 7);
        assert_eq!(health.coalesced, 4);
        assert_eq!(health.depth, 3);
        assert_eq!(health.window, 3);
    });
}

#[test]
fn zero_client_paced_runs_publish_into_bounded_storage() {
    let (driver, map) = rig();
    let executor = Executor::new(&driver, map, vec![Box::new(Scale)]).unwrap();
    let monitor = Monitor::bind_paced("127.0.0.1:0", executor, signal_index()).unwrap();

    // No server, no readers: the paced loop still publishes per scan —
    // the store's counters advance while its depth stays at the bound.
    for _ in 0..40 {
        monitor.paced_scan();
    }
    let health = monitor.publication_health();
    let window = MonitorConfig::default().publication_capacity as u64;
    assert_eq!(health.published, 41);
    assert_eq!(health.depth, window);
    assert_eq!(health.window, window);
    assert_eq!(health.coalesced, 41 - window);
    // Only the window is retained for a cursor read.
    let page = monitor.publications_since(0);
    assert_eq!(page.publications.len() as u64, window);
    assert_eq!(
        page.gap,
        Some(PublicationGap {
            through: 41 - window
        })
    );
}

#[test]
fn a_stalled_reader_cannot_hold_the_executor_lock() {
    let (driver, map) = rig();
    let executor = Executor::new(&driver, map, vec![Box::new(Scale)]).unwrap();
    let monitor = Monitor::bind_paced_peer_with(
        "127.0.0.1:0",
        Peer::active(executor, None),
        signal_index(),
        MonitorConfig {
            publication_capacity: 3,
            ..MonitorConfig::default()
        },
    )
    .unwrap();
    let addr = monitor.local_addr();
    serving(&monitor, || {
        // A reader that connects, issues its request, and then goes
        // silent without reading the response: the served copy was
        // fetched and serialized off-lock, so the paced loop's scans
        // complete on schedule while it stalls.
        let mut stalled = TcpStream::connect(addr).unwrap();
        stalled
            .write_all(b"GET /snapshot HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            .unwrap();
        let deadline = Instant::now() + Duration::from_secs(5);
        for _ in 0..12 {
            monitor.paced_scan();
        }
        assert!(
            Instant::now() < deadline,
            "the paced loop stalled behind a silent reader"
        );

        // The overload counters kept accounting — publications
        // published, the window's overruns coalesced — while the
        // reader never consumed a byte.
        let health = monitor.publication_health();
        assert_eq!(health.published, 13);
        assert_eq!(health.depth, 3);
        assert_eq!(health.coalesced, 10);

        // Its response waited on the wire, complete and correct.
        stalled
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        let mut buf = Vec::new();
        stalled.read_to_end(&mut buf).unwrap();
        let response = String::from_utf8_lossy(&buf);
        assert!(
            response
                .lines()
                .next()
                .is_some_and(|line| line.contains("200")),
            "{response}"
        );
        assert!(response.contains("\"tick\""), "{response}");
    });
}

#[test]
fn receipts_stay_current_between_scans_and_publication_carry_them() {
    let (driver, map) = rig();
    let executor = Executor::new(&driver, map, vec![Box::new(Scale)]).unwrap();
    let monitor = Monitor::bind("127.0.0.1:0", executor, signal_index()).unwrap();
    let client = MonitorClient::new(monitor.local_addr());
    serving(&monitor, || {
        client.advance(1).unwrap();
        // A submission between scans is visible to readers at once —
        // the mirror refreshes under the same control-plane lock that
        // accepts the command; it does not wait for the next publish.
        let receipt = client
            .command(&dcs_core::Command::WriteValue {
                point: PointId(10),
                kind: ValueKind::Float,
                value: Value::Float(7.0),
            })
            .unwrap();
        assert!(matches!(
            receipt.outcome,
            dcs_core::CommandOutcome::Accepted { .. }
        ));
        assert_eq!(client.receipts().unwrap().len(), 1);

        // The scan that settles it publishes the applied log.
        client.advance(1).unwrap();
        let publication = monitor.published().unwrap();
        assert_eq!(publication.receipts.len(), 1);
        assert!(matches!(
            publication.receipts[0].outcome,
            dcs_core::CommandOutcome::Applied { .. }
        ));
    });
}
