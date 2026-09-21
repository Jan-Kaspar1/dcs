//! Issue #686 — every adopt/promote cycle re-journaled
//! `command_settled` for already-settled receipts: the recorder dropped
//! a receipt's observation when its index left the bounded log's
//! served window, so a checkpoint adoption that re-admitted the same
//! receipt at the same index re-emitted its settle — ~1 duplicate per
//! adopt, compounding across failovers. These tests hold the
//! acceptance: each admission's terminal outcome journals exactly once
//! per peer, however the receipt window moves.

use dcs_core::{
    Command, CommandOutcome, CommandReceipt, Direction, IoDriver, IoError, JournalEvent, PointId,
    Sample, Tick, Value, ValueKind,
};
use dcs_model::{PlantModel, SignalIndex};
use dcs_monitor::{Driven, Monitor, MonitorClient};
use dcs_runtime::{Executor, Peer, PointMap};
use std::collections::HashMap;
use std::sync::Mutex;
use std::thread::{self, JoinHandle};

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

const MODEL: &str = include_str!("../fixtures/monitor.json");

fn signal_index() -> SignalIndex {
    PlantModel::load(MODEL).unwrap().signal_index()
}

/// A small receipt log, so a handful of admissions already slides the
/// bounded window's base — the eviction the defect keyed on.
const RECEIPT_CAPACITY: usize = 4;

fn executor(driver: &'static StubDriver) -> Executor<'static> {
    Executor::new(
        driver,
        PointMap::new().with_writable_internal(
            PointId(40),
            Direction::In,
            ValueKind::Bool,
            Value::Bool(false),
        ),
        Vec::new(),
    )
    .unwrap()
    .with_receipt_log_capacity(RECEIPT_CAPACITY)
}

struct Serving {
    monitor: std::sync::Arc<Monitor<'static>>,
    client: MonitorClient,
    thread: Option<JoinHandle<()>>,
}

impl Serving {
    fn start(monitor: Monitor<'static>) -> Self {
        let monitor = std::sync::Arc::new(monitor);
        let client = MonitorClient::new(monitor.local_addr());
        let serving = std::sync::Arc::clone(&monitor);
        Self {
            monitor,
            client,
            thread: Some(thread::spawn(move || serving.serve())),
        }
    }
}

impl Drop for Serving {
    fn drop(&mut self) {
        self.monitor.shutdown();
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

/// Every `command_settled` receipt the peer's journal carries.
fn settles(client: &MonitorClient) -> Vec<CommandReceipt> {
    client
        .journal(0)
        .unwrap()
        .iter()
        .filter_map(|entry| match &entry.event {
            JournalEvent::CommandSettled { receipt } => Some(receipt.clone()),
            _ => None,
        })
        .collect()
}

fn submit(client: &MonitorClient, value: bool) -> CommandReceipt {
    client
        .command(&Command::WriteValue {
            point: PointId(40),
            kind: ValueKind::Bool,
            value: Value::Bool(value),
        })
        .unwrap()
}

/// No two journaled settles may be identical — a receipt journaled
/// twice is the defect itself, whatever produced it.
fn assert_unique(settles: &[CommandReceipt]) {
    for (position, receipt) in settles.iter().enumerate() {
        assert!(
            !settles[..position].iter().any(|earlier| earlier == receipt),
            "settle journaled twice: {receipt:?} in {settles:?}"
        );
    }
}

/// The window-regression the QA run's failovers produced, distilled:
/// the active runs its bounded log's base past the standby's whole
/// window, then the standby promotes without ever having seen the
/// tail. The demoted peer's tracking pull adopts a window whose base
/// sits back below receipts it already journaled and evicted — every
/// re-admitted settle must not journal again.
#[test]
fn checkpoint_adoption_does_not_rejournal_evicted_settles() {
    let driver_a: &'static StubDriver = Box::leak(Box::new(StubDriver::new(&[])));
    let a = Serving::start(
        Monitor::bind_peer(
            "127.0.0.1:0",
            Peer::active(executor(driver_a), None),
            signal_index(),
        )
        .unwrap(),
    );
    let driver_b: &'static StubDriver = Box::leak(Box::new(StubDriver::new(&[])));
    let b = Serving::start(
        Monitor::bind_peer(
            "127.0.0.1:0",
            Peer::standby(executor(driver_b), None),
            signal_index(),
        )
        .unwrap(),
    );

    let sync = |from: &Serving, to: &Serving| {
        let checkpoint = from.client.checkpoint().unwrap();
        to.monitor.apply_checkpoint(&checkpoint).unwrap();
    };

    // Converge B on A and announce B so A can demote and later track it.
    a.client.advance(1).unwrap();
    sync(&a, &b);
    a.client
        .checkpoint_announcing(b.monitor.local_addr())
        .unwrap();

    // Three settled commands — both peers journal each once.
    for i in 0..3 {
        submit(&a.client, i % 2 == 0);
        a.client.advance(1).unwrap();
    }
    sync(&a, &b);
    b.client.advance(1).unwrap();
    assert_eq!(settles(&a.client).len(), 3);
    assert_eq!(settles(&b.client).len(), 3);

    // A runs its log's base past B's entire window — eight more
    // settled admissions B never observes.
    for _ in 0..8 {
        submit(&a.client, true);
        a.client.advance(1).unwrap();
    }
    assert_eq!(settles(&a.client).len(), 11);

    // One pending admission suspends at the demote; B promotes without
    // it (or the tail), so the line adjudicates it superseded on A's
    // next tracking apply.
    submit(&a.client, true);
    a.client.demote().unwrap();
    b.client.promote().unwrap();
    b.client.advance(1).unwrap();

    // A tracks back: each pull adopts B's window — whose base sits
    // below receipts A already journaled and whose span re-admits them
    // at their original indices — and the un-seen tail restores behind
    // it. The re-admitted settles were already in A's journal; nothing
    // may re-emit, however many pulls adopt the same window.
    for _ in 0..3 {
        a.client.advance(1).unwrap();
    }

    let a_settles = settles(&a.client);
    // Twelve admissions: eleven applied, the suspended one superseded
    // — each exactly once.
    assert_eq!(a_settles.len(), 12, "{a_settles:?}");
    let superseded = a_settles
        .iter()
        .filter(|receipt| matches!(receipt.outcome, CommandOutcome::Rejected { .. }))
        .count();
    assert_eq!(superseded, 1, "{a_settles:?}");
    assert_unique(&a_settles);

    // B journaled only the three receipts it ever observed — the
    // promotion re-emitted none of the adopted settles.
    let b_settles = settles(&b.client);
    assert_eq!(b_settles.len(), 3, "{b_settles:?}");
    assert_unique(&b_settles);
}

/// The required integration test's shape: commands raced against N
/// demote/promote transitions on a tracking pair — the QA
/// reproduction's "submit a command, then demote+promote the active
/// repeatedly". Every admission journals exactly one settle on each
/// peer; the tracking side never duplicates a settle adopted through
/// a checkpoint.
#[test]
fn demote_promote_cycles_journal_each_admission_once() {
    let driver_a: &'static StubDriver = Box::leak(Box::new(StubDriver::new(&[])));
    let a = Serving::start(
        Monitor::bind_peer(
            "127.0.0.1:0",
            Peer::active(executor(driver_a), None),
            signal_index(),
        )
        .unwrap(),
    );
    let driver_b: &'static StubDriver = Box::leak(Box::new(StubDriver::new(&[])));
    let b = Serving::start(
        Monitor::bind_peer(
            "127.0.0.1:0",
            Peer::standby(executor(driver_b), None),
            signal_index(),
        )
        .unwrap()
        .driven(Driven {
            track: Some(a.monitor.local_addr()),
            after_scan: None,
        }),
    );

    // Converge B on A and announce B so the active's demote has a
    // tracking source to leave behind.
    a.client.advance(1).unwrap();
    a.client
        .checkpoint_announcing(b.monitor.local_addr())
        .unwrap();
    b.client.advance(1).unwrap();

    let mut admissions = 0;
    for cycle in 0..6 {
        let (owner, other) = if cycle % 2 == 0 { (&a, &b) } else { (&b, &a) };
        // Race the admission against the demote — accepted pending,
        // suspended at the boundary, carried to the successor's
        // promote-sync and settled there.
        submit(&owner.client, true);
        admissions += 1;
        owner.client.demote().unwrap();
        other.client.promote().unwrap();
        // The promoted peer applies the carried command; the demoted
        // peer tracks back and adopts the settled receipt — the
        // adoption that re-journaled per cycle pre-fix.
        for _ in 0..3 {
            other.client.advance(1).unwrap();
            owner.client.advance(1).unwrap();
        }
    }

    for client in [&a.client, &b.client] {
        let settled = settles(client);
        assert_eq!(settled.len(), admissions, "{settled:?}");
        assert_unique(&settled);
        assert!(
            settled
                .iter()
                .all(|receipt| matches!(receipt.outcome, CommandOutcome::Applied { .. })),
            "every raced admission carries to the successor: {settled:?}"
        );
    }
}
