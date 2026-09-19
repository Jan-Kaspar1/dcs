//! The page's feed-state surface — the consumer-side honesty the
//! bounded-publication decision requires of the page as a consumer: a
//! `since`-cursor poll stepping over an evicted stretch observes the
//! served numbering gap, and a snapshot re-serving the same
//! publication's seq/tick observes a stall. The page's detection rule
//! is mirrored here poll-for-poll against the same Monitor rig the
//! other page tests use — the stub driver is the stubbed feed — driving
//! a real eviction through small retained bounds and a real stall by
//! simply not scanning, then asserting the named state renders and
//! clears.

use dcs_core::{Direction, IoDriver, IoError, PointId, Sample, Tick, Value, ValueKind};
use dcs_model::{PlantModel, SignalIndex};
use dcs_monitor::{Monitor, MonitorClient, MonitorConfig};
use dcs_runtime::{
    Component, ComponentIo, ComponentIoExt, Executor, IoRequirement, PointMap, StepError,
};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::Mutex;
use std::thread;

/// The same minimal in-memory driver the other monitor tests use, with
/// injectable faults so a poll can land non-Good process data.
struct StubDriver {
    points: Mutex<HashMap<PointId, Sample>>,
    faults: Mutex<HashSet<PointId>>,
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
}

/// Reads `In` point 10 and drives `Out` point 20 at gain 2 — the same
/// component shape the other monitor tests use.
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

/// The model fixture behind the monitor; points 10/20/30 match the
/// rig's point map.
const MODEL: &str = include_str!("../fixtures/monitor.json");

fn signal_index() -> SignalIndex {
    PlantModel::load(MODEL).unwrap().signal_index()
}

/// Runs `body` while `monitor` serves, always shutting the server down
/// before the driver's borrow ends — the same helper the publication
/// tests use.
fn serving<T>(monitor: &Monitor<'_>, body: impl FnOnce() -> T) -> T {
    let result = thread::scope(|scope| {
        scope.spawn(|| monitor.serve());
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(body));
        monitor.shutdown();
        result
    });
    result.unwrap_or_else(|panic| std::panic::resume_unwind(panic))
}

/// What the page's `#feed-state` line renders after a poll — the named
/// degraded states the view owes instead of last-known values standing
/// in for current ones.
#[derive(Debug, PartialEq)]
enum FeedState {
    /// The poll landed a fresh publication and every since-cursor read
    /// came back in sequence: the indicator is hidden.
    Fresh,
    /// A since-cursor read answered entries numbered `through` while
    /// the cursor stood before `from` — the stretch between evicted
    /// from the bounded served stream before the page read it. The
    /// page's "publication gap".
    Gap { from: u64, through: u64 },
    /// The snapshot re-served the publication identity the last poll
    /// already rendered — a stall. The page's "stale publication".
    Stale { published: Option<u64>, tick: Tick },
}

/// The page's feed-state rule, mirrored poll-for-poll: one snapshot
/// read against the last rendered publication identity — the
/// `publication` section's `published` seq beside the snapshot tick —
/// then the since-cursor history and journal increments, whose first
/// returned seq stepping over the cursor's successor is the served
/// numbering gap. Cursors live per point like the page's per-trend
/// `lastSeq`, and the common history `since` follows the page's rule:
/// the smallest seen seq once every point has seen one, else a whole
/// refetch.
struct PageFeed {
    /// The last rendered publication identity — `(published, tick)` —
    /// `published` `None` for a peer serving snapshots without the
    /// publication section, which then compares on tick alone.
    publication: Option<(Option<u64>, Tick)>,
    /// Per-point history cursors — the page's per-trend `lastSeq`,
    /// seeded for every index point like `buildTrends`.
    history_since: BTreeMap<PointId, u64>,
    /// The journal cursor — the page's `journalSince`.
    journal_since: u64,
}

impl PageFeed {
    fn new(client: &MonitorClient) -> Self {
        Self {
            publication: None,
            history_since: client
                .signals()
                .unwrap()
                .points
                .iter()
                .map(|meta| (meta.point, 0))
                .collect(),
            journal_since: 0,
        }
    }

    /// One page refresh's feed reads — `/snapshot` then the `/history`
    /// and `/journal` since-polls — answering the state the indicator
    /// renders. A poll observing both degradations answers the gap:
    /// the streams provably advanced past the cursor while the
    /// publication identity repeated cannot happen — a gap needs new
    /// entries, which a repeated publication never carries.
    fn poll(&mut self, client: &MonitorClient) -> FeedState {
        let snapshot = client.snapshot().unwrap();
        let publication = (
            snapshot.publication.map(|health| health.published),
            snapshot.tick,
        );
        let mut state = match self.publication {
            Some(last) if last == publication => FeedState::Stale {
                published: publication.0,
                tick: publication.1,
            },
            _ => FeedState::Fresh,
        };
        self.publication = Some(publication);

        let since =
            if !self.history_since.is_empty() && self.history_since.values().all(|seq| *seq > 0) {
                self.history_since.values().copied().min().unwrap()
            } else {
                0
            };
        for history in client.history(&[], since).unwrap() {
            let last = self.history_since.get(&history.point).copied().unwrap_or(0);
            if last > 0
                && let Some(first) = history.samples.iter().find(|entry| entry.seq > last)
                && first.seq > last + 1
            {
                state = FeedState::Gap {
                    from: last + 1,
                    through: first.seq - 1,
                };
            }
            for entry in &history.samples {
                if entry.seq > last {
                    self.history_since.insert(history.point, entry.seq);
                }
            }
        }

        let entries = client.journal(self.journal_since).unwrap();
        if self.journal_since > 0
            && let Some(first) = entries.first()
            && first.seq > self.journal_since + 1
        {
            state = FeedState::Gap {
                from: self.journal_since + 1,
                through: first.seq - 1,
            };
        }
        for entry in &entries {
            self.journal_since = self.journal_since.max(entry.seq);
        }
        state
    }
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

#[test]
fn page_carries_the_feed_state_indicator() {
    let page = dcs_monitor::PAGE;
    // The line beside the view the marks render on, and the named
    // states themselves — distinct names from the pair section's
    // unreachable-peer fault and from the quality vocabulary.
    for needle in ["id=\"feed-state\"", "publication gap", "stale publication"] {
        assert!(page.contains(needle), "page lacks {needle}");
    }
    // The detection machinery: the publication identity the freshness
    // check compares, the since-cursor gap reads, and the renderer.
    for needle in [
        "function notePublication(snapshot)",
        "function noteFeedGap(stream, from, through)",
        "function renderFeed()",
        "feed.publication",
        "feed.gap",
        "feed.stale",
        "snapshot.publication",
        "health.published",
        "noteFeedGap(\"history\"",
        "noteFeedGap(\"journal\"",
    ] {
        assert!(page.contains(needle), "page lacks {needle}");
    }
    // Recovery: a source switch resets the bookkeeping, and the marks
    // recompute per poll — the next in-sequence, fresh publication
    // hides the line.
    for needle in [
        "feed.publication = null",
        "feed.gap = null",
        "feed.stale = null",
        "line.hidden = notices.length === 0",
    ] {
        assert!(page.contains(needle), "page lacks {needle}");
    }
    // The page stays a single dependency-free asset over the existing
    // endpoints: no new wire types or endpoints were consumed.
    assert!(!page.contains("src="), "page references external assets");
}

#[test]
fn a_poll_through_eviction_and_stall_names_the_state_then_clears() {
    let (driver, map) = rig();
    let executor = Executor::new(&driver, map, vec![Box::new(Scale)]).unwrap();
    // Small bounds so a handful of scans out-publishes the retained
    // rings — the poll then steps over the evicted stretch.
    let monitor = Monitor::bind_with(
        "127.0.0.1:0",
        executor,
        signal_index(),
        MonitorConfig {
            history_capacity: 3,
            journal_capacity: 4,
            publication_capacity: 3,
            ..MonitorConfig::default()
        },
    )
    .unwrap();
    let client = MonitorClient::new(monitor.local_addr());
    serving(&monitor, || {
        let mut feed = PageFeed::new(&client);

        // The first poll lands the bind-time seed publication: fresh.
        assert_eq!(feed.poll(&client), FeedState::Fresh);

        // A stall: the same publication's seq and tick re-serve, so the
        // re-rendered values are last-known — the named stale mark.
        assert_eq!(
            feed.poll(&client),
            FeedState::Stale {
                published: Some(1),
                tick: Tick(0),
            }
        );

        // The next fresh publication clears it.
        client.advance(2).unwrap();
        assert_eq!(feed.poll(&client), FeedState::Fresh);

        // Eviction: scans out-publish the bounded rings between polls,
        // so the next since-read answers past the cursor's successor —
        // the named gap, carrying the evicted stretch.
        client.advance(5).unwrap();
        match feed.poll(&client) {
            FeedState::Gap { from, through } => {
                assert!(from > 1 && through >= from, "gap {from}..{through}");
            }
            other => panic!("expected the named gap, got {other:?}"),
        }

        // Recovery on the next in-sequence publication: the cursor now
        // sits at the retained tail, so the following increment lands in
        // sequence and the mark clears.
        client.advance(1).unwrap();
        assert_eq!(feed.poll(&client), FeedState::Fresh);

        // And the feed stalls again the moment publications stop
        // advancing — stale is freshness state, not a latched fault.
        assert_eq!(
            feed.poll(&client),
            FeedState::Stale {
                published: Some(9),
                tick: Tick(8),
            }
        );
    });
}

#[test]
fn non_good_process_data_is_not_a_feed_state() {
    let (driver, map) = rig();
    let executor = Executor::new(&driver, map, vec![Box::new(Scale)]).unwrap();
    let monitor = Monitor::bind("127.0.0.1:0", executor, signal_index()).unwrap();
    let client = MonitorClient::new(monitor.local_addr());
    serving(&monitor, || {
        let mut feed = PageFeed::new(&client);
        assert_eq!(feed.poll(&client), FeedState::Fresh);

        // A field read fails: the point's sample lands non-Good while
        // the feed itself still publishes fresh, in-sequence read
        // models — process-data degradation is the quality column's,
        // not the feed line's.
        driver.faults.lock().unwrap().insert(PointId(10));
        client.advance(1).unwrap();
        assert_eq!(feed.poll(&client), FeedState::Fresh);
        let snapshot = client.snapshot().unwrap();
        let telemetry = snapshot
            .points
            .iter()
            .find(|telemetry| telemetry.point == PointId(10))
            .unwrap();
        assert!(
            telemetry
                .sample
                .is_some_and(|sample| !matches!(sample.quality, dcs_core::Quality::Good)),
            "the faulted read landed non-Good process data: {:?}",
            telemetry.sample
        );
    });
}
