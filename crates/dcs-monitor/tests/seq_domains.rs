//! Pinning tests for the served `seq` epoch record — the naming
//! decision recorded in `docs/architecture.md` ("The served `seq`
//! namespaces"): the sequence domains the served surface spells
//! `seq` across, and the restart scope each carries. The run-scoped
//! publication and history-ring axes open each process lifetime as a
//! new epoch; the file-scoped journal and durable-history axes
//! continue across a restart through replay and `run_boundary`
//! markers; and a `since` cursor held across a restart against a
//! run-scoped domain must reset-or-gap, never silently starve — the
//! #741 contract the record pins. These tests assert the documented
//! definitions against the serving code's actual numbering, so a
//! change that shifts a domain's scope breaks the pin rather than
//! the vocabulary.

use dcs_core::{
    Direction, DurableEvent, IoDriver, IoError, JournalEvent, PointId, Sample, Tick, Value,
    ValueKind,
};
use dcs_model::{PlantModel, SignalIndex};
use dcs_monitor::{Monitor, MonitorClient, MonitorConfig, read_history_file, read_journal_file};
use dcs_runtime::{Executor, PointMap, PointSpec};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
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
    let dir = std::env::temp_dir().join(format!(
        "dcs-monitor-seq-domains-{test}-{}",
        std::process::id()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

/// The plain rig's point map: field `In` point 10 and `Out` point 20.
fn plain_map() -> PointMap {
    PointMap::new()
        .with_point(PointId(10), Direction::In, ValueKind::Float)
        .with_point(PointId(20), Direction::Out, ValueKind::Float)
}

/// The declared-duty rig's point map: the same points, with point 10
/// carrying the per-tick durable `record` declaration.
fn declared_map() -> PointMap {
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
                record_every_ticks: Some(1),
            },
        )
        .with_point(PointId(20), Direction::Out, ValueKind::Float)
}

/// Runs `body` against a serving `monitor` and its `driver`; the
/// server is shut down before the driver's borrow ends — the same
/// helper shape the history and journal suites use. Dropping the
/// monitor drains and joins the sink writers, so a returned run's
/// files hold everything it journaled.
fn serve<T>(
    driver: &StubDriver,
    monitor: Monitor,
    body: impl FnOnce(&StubDriver, &MonitorClient) -> T,
) -> T {
    let client = MonitorClient::new(monitor.local_addr());
    let result = thread::scope(|scope| {
        scope.spawn(|| monitor.serve());
        // A failing assertion must not deadlock the scope join: catch
        // the panic so the server is always shut down before it
        // propagates.
        let result =
            std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| body(driver, &client)));
        monitor.shutdown();
        result
    });
    result.unwrap_or_else(|panic| std::panic::resume_unwind(panic))
}

/// The record itself: the architecture document must name the served
/// `seq` domains and their epoch scopes — the run-scoped publication
/// and history-ring axes beside the file-scoped journal and durable
/// history axes — plus the reset-or-gap rule a run-scoped `since`
/// cursor follows across a restart, reconciling the #847 vocabulary
/// ticket against defect #741's demonstrated injury. The pins are
/// the definitions' own vocabulary, not prose details.
#[test]
fn the_record_names_the_seq_domains_their_scopes_and_the_cursor_rule() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(2)
        .unwrap()
        .to_path_buf();
    let doc = std::fs::read_to_string(root.join("docs/architecture.md")).unwrap();
    let start = doc
        .find("The served `seq` namespaces")
        .expect("docs/architecture.md records the seq-domain naming decision");
    let section = match doc[start..].find("\n### ") {
        Some(end) => &doc[start..start + end],
        None => &doc[start..],
    };
    for definition in [
        "`Publication.seq`",
        "`HistorySample.seq`",
        "`JournalEntry.seq`",
        "`DurableEntry.seq`",
        "run-scoped",
        "file-scoped",
        "epoch",
        "since",
        "reset-or-gap",
        "#741",
        "#847",
    ] {
        assert!(
            section.contains(definition),
            "the seq-domain record must carry the definition: missing {definition}"
        );
    }
}

/// The same vocabulary in the platform's shared domain language: the
/// `CONTEXT.md` section that names each `seq` domain where crates,
/// contracts, and docs collide over the one spelling must agree with the
/// architecture record above — the run-scoped publication and
/// history-ring axes, the file-scoped journal and durable axes, and the
/// reset-or-gap rule a run-scoped cursor owes. A later change renaming
/// a domain's scope in one record and not the other breaks the pin
/// instead of leaving the two vocabularies disagreeing.
#[test]
fn the_shared_language_record_agrees_on_the_seq_domains() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(2)
        .unwrap()
        .to_path_buf();
    let doc = std::fs::read_to_string(root.join("CONTEXT.md")).unwrap();
    let start = doc
        .find("### Seq domains")
        .expect("CONTEXT.md records the seq-domain vocabulary");
    let section = match doc[start..].find("\n### ") {
        Some(end) => &doc[start..start + end],
        None => &doc[start..],
    };
    for definition in [
        "**Publication seq**",
        "**History-ring seq**",
        "**Journal seq**",
        "**durable seq**",
        "`Publication.seq`",
        "`HistorySample.seq`",
        "`JournalEntry.seq`",
        "`DurableEntry.seq`",
        "Run-scoped",
        "file-scoped",
        "reset-or-gap",
        "Cursor reset-or-gap",
    ] {
        assert!(
            section.contains(definition),
            "CONTEXT.md's seq-domain record must carry the definition: \
             missing {definition}"
        );
    }
}

/// The run-scoped domains: with no durable file configured, the
/// publication seq, the per-point ring seq, and the in-memory journal
/// seq each open the process's epoch at seq 1 — and the next
/// lifetime renumbers the same way. A `since` cursor held across the
/// restart gets the reset signals the record pins — a renumbered
/// stream under the served epoch marks — rather than silently
/// starving.
#[test]
fn run_scoped_domains_open_each_lifetime_at_seq_one_and_reset_on_restart() {
    let driver = StubDriver::new(&[
        (PointId(10), Value::Float(0.0)),
        (PointId(20), Value::Float(0.0)),
    ]);
    let bind = || {
        Monitor::bind(
            "127.0.0.1:0",
            Executor::new(&driver, plain_map(), Vec::new()).unwrap(),
            signal_index(),
        )
        .unwrap()
    };

    // Lifetime 1: the bind-time seed is publication seq 1; two scans
    // stamp the ring and the journal from 1.
    let first = bind();
    assert_eq!(
        first.published().unwrap().seq,
        1,
        "the bind-time seed opens the publication epoch"
    );
    let first_max = serve(&driver, first, |_driver, client| {
        client.advance(2).unwrap();
        let snapshot = client.snapshot().unwrap();
        assert_eq!(snapshot.publication.unwrap().published, 3);
        let history = client.history(&[PointId(10)], 0).unwrap();
        assert_eq!(history[0].run, 1);
        assert_eq!(
            history[0]
                .samples
                .iter()
                .map(|sample| sample.seq)
                .collect::<Vec<_>>(),
            vec![1, 2],
            "the ring axis rides the run's tick domain from 1"
        );
        let journal = client.journal(0).unwrap();
        assert_eq!(
            journal.first().unwrap().seq,
            1,
            "an unfiled journal is one more run-scoped axis"
        );
        history[0].samples.last().unwrap().seq
    });

    // Lifetime 2 — a cold restart against the same field, no durable
    // files: every run-scoped axis renumbers from 1. With no journal
    // file the `run` mark is a constant 1, so the regressed
    // publication identity is the restart's served signal — the
    // record's reset rule stands either way: the dead cursor's page
    // still names the serving epoch instead of starving silently.
    let second = bind();
    assert_eq!(
        second.published().unwrap().seq,
        1,
        "the restart reopens the publication epoch at seq 1"
    );
    serve(&driver, second, move |_driver, client| {
        let snapshot = client.snapshot().unwrap();
        assert_eq!(snapshot.publication.unwrap().published, 1);
        assert_eq!(
            snapshot.tick,
            Tick::ZERO,
            "a cold run opens a new tick domain"
        );
        client.advance(1).unwrap();
        let history = client.history(&[PointId(10)], 0).unwrap();
        assert_eq!(
            history[0]
                .samples
                .iter()
                .map(|sample| sample.seq)
                .collect::<Vec<_>>(),
            vec![1],
            "the restarted ring renumbers under the new epoch"
        );
        let stale = client.history(&[PointId(10)], first_max).unwrap();
        assert!(
            stale[0].samples.is_empty(),
            "the dead cursor's samples filter out — the page must reset, not starve"
        );
        assert_eq!(
            stale[0].run, 1,
            "the emptied page still carries the epoch mark"
        );
        let journal = client.journal(0).unwrap();
        assert_eq!(
            journal.first().unwrap().seq,
            1,
            "the in-memory journal renumbers with the lifetime"
        );
    });
}

/// The file-scoped domains: a configured journal file and history
/// file carry their `seq` axes across the restart — replay keeps the
/// file's numbering and the new lifetime's `run_boundary` takes the
/// next file seq — while the same restart's run-scoped axes open a
/// fresh epoch beside them, the two scopes coexisting on one surface.
#[test]
fn file_scoped_domains_continue_while_run_scoped_domains_re_epoch() {
    let dir = scratch("epoch-scope");
    let journal_path = dir.join("journal.jsonl");
    let history_path = dir.join("history.jsonl");
    let driver = StubDriver::new(&[
        (PointId(10), Value::Float(0.0)),
        (PointId(20), Value::Float(0.0)),
    ]);
    let bind = || {
        Monitor::bind_with(
            "127.0.0.1:0",
            Executor::new(&driver, declared_map(), Vec::new()).unwrap(),
            signal_index(),
            MonitorConfig {
                journal_file: Some(journal_path.clone()),
                history_file: Some(history_path.clone()),
                ..MonitorConfig::default()
            },
        )
        .unwrap()
    };

    // Lifetime 1: three scans — the journaled census plus cadence-1
    // durable records. The files' tail seqs are the restart's
    // continuation point; the ring's tail seq is the cursor a
    // consumer would hold across.
    let first = bind();
    assert_eq!(first.published().unwrap().seq, 1);
    let (journal_tail, durable_tail, ring_tail) = serve(&driver, first, |_driver, client| {
        client.advance(3).unwrap();
        let journal = client.journal(0).unwrap();
        assert_eq!(
            journal.first().unwrap().seq,
            1,
            "the journal file opens its numbering at 1"
        );
        let durable = client.durable_history(&[], 0).unwrap();
        assert_eq!(
            durable.iter().map(|entry| entry.seq).collect::<Vec<_>>(),
            vec![1, 2, 3],
            "the declared-duty stream numbers the file from 1"
        );
        let history = client.history(&[PointId(10)], 0).unwrap();
        assert_eq!(history[0].run, 1);
        (
            journal.last().unwrap().seq,
            durable.last().unwrap().seq,
            history[0].samples.last().unwrap().seq,
        )
    });

    // Lifetime 2 on the same files: the file axes continue — the
    // replayed tail keeps its seqs and each lifetime boundary takes
    // the next one — while the publication and ring axes renumber.
    let second = bind();
    assert_eq!(
        second.published().unwrap().seq,
        1,
        "the publication epoch is per lifetime"
    );
    serve(&driver, second, move |_driver, client| {
        // The journal's file axis: the replayed tail, then the
        // lifetime boundary continuing the file's `seq`.
        let journal = client.journal(0).unwrap();
        let boundary = journal
            .iter()
            .find(|entry| matches!(entry.event, JournalEvent::RunBoundary { run: 2 }))
            .expect("the restart journals its lifetime boundary");
        assert_eq!(
            boundary.seq,
            journal_tail + 1,
            "the boundary takes the file's next seq"
        );
        let durable = client.durable_history(&[], 0).unwrap();
        let marker = durable
            .iter()
            .find(|entry| matches!(entry.event, DurableEvent::RunBoundary { run: 2, .. }))
            .expect("the restart records its lifetime boundary");
        assert_eq!(
            marker.seq,
            durable_tail + 1,
            "the durable axis continues the file's numbering"
        );

        client.advance(1).unwrap();

        // File-scoped cursors read straight across the seam: the held
        // `since` values still mean what they meant before the
        // restart — the boundary markers and new records answer.
        let journal = client.journal(journal_tail).unwrap();
        assert!(
            journal.iter().all(|entry| entry.seq > journal_tail),
            "a file-scoped cursor keeps reading after the restart"
        );
        assert!(
            journal
                .iter()
                .any(|entry| matches!(entry.event, JournalEvent::RunBoundary { run: 2 })),
            "the boundary the cursor steps over names the new lifetime"
        );
        let durable = client.durable_history(&[], durable_tail).unwrap();
        assert_eq!(
            durable.iter().map(|entry| entry.seq).collect::<Vec<_>>(),
            vec![durable_tail + 1, durable_tail + 2],
            "the durable tail answers the held cursor: boundary, then the new record"
        );

        // The same restart's run-scoped axes: the ring renumbered
        // under the new epoch, and a ring cursor held across gets the
        // reset signal — the changed `run` mark on an emptied page —
        // rather than silently starving.
        let history = client.history(&[PointId(10)], 0).unwrap();
        assert_eq!(history[0].run, 2, "the envelope names the new epoch");
        assert_eq!(
            history[0]
                .samples
                .iter()
                .map(|sample| sample.seq)
                .collect::<Vec<_>>(),
            vec![1],
            "the run-scoped ring renumbers while the file axes continue"
        );
        let stale = client.history(&[PointId(10)], ring_tail).unwrap();
        assert!(
            stale[0].samples.is_empty() && stale[0].run == 2,
            "the held cursor's answer names the new epoch instead of starving"
        );
        assert_eq!(
            client.snapshot().unwrap().publication.unwrap().published,
            2,
            "the publication axis counts the new epoch alone"
        );
    });

    // The files themselves: both lifetimes' records under one
    // gap-free numbering each, the boundary markers segmenting the
    // runs.
    let journal_data = read_journal_file(&journal_path).unwrap();
    assert_eq!(journal_data.boundaries.len(), 2);
    assert_eq!(journal_data.boundaries[1].run, 2);
    assert_eq!(
        journal_data
            .entries
            .iter()
            .map(|entry| entry.seq)
            .collect::<Vec<_>>(),
        (1..=journal_data.entries.last().unwrap().seq).collect::<Vec<_>>(),
        "the journal file's record stays gap-free across the restart"
    );
    let history_data = read_history_file(&history_path).unwrap();
    assert_eq!(history_data.boundaries.len(), 2);
    assert_eq!(history_data.boundaries[1].run, 2);
    assert_eq!(
        history_data
            .entries
            .iter()
            .map(|entry| entry.seq)
            .collect::<Vec<_>>(),
        (1..=history_data.entries.last().unwrap().seq).collect::<Vec<_>>(),
        "the history file's record stays gap-free across the restart"
    );
    let _ = std::fs::remove_dir_all(&dir);
}
