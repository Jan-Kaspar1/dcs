//! In-process tests for the pair view: two monitor instances — one
//! reporting `active`, one `standby`, per the role contract — presented
//! as one logical controller through `PairClient`, plus the markup the
//! served page uses for the same view.
//!
//! Each peer is a `Monitor` serving a `Peer`-wrapped executor on its own
//! thread; the standby is converged through `apply_checkpoint` like the
//! promotion tests, and a "dropped" standby is a monitor shut down and
//! dropped so its port refuses connections.

use dcs_core::{
    Command, CommandError, CommandOutcome, CommandReceipt, Direction, IoDriver, IoError,
    JournalEvent, PointId, Role, Sample, StandbySync, Tick, Value, ValueKind,
};
use dcs_model::{PlantModel, SignalIndex};
use dcs_monitor::{Monitor, MonitorClient, PairClient, PairError, PeerStatus};
use dcs_runtime::{
    Component, ComponentIo, ComponentIoExt, Executor, IoRequirement, Peer, PointMap, StepError,
};
use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::sync::{Arc, Mutex};
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

/// The model fixture behind the monitors; both peers serve the same index.
const MODEL: &str = include_str!("../fixtures/monitor.json");

fn signal_index() -> SignalIndex {
    PlantModel::load(MODEL).unwrap().signal_index()
}

fn write_value(point: u64, kind: ValueKind, value: Value) -> Command {
    Command::WriteValue {
        point: PointId(point),
        kind,
        value,
    }
}

/// One peer of the rig: its monitor served on a dedicated thread. The
/// monitor is `Arc`-shared so `stop` can drop the last handle and close
/// the listener — a simulated process outage — mid-test. The driver is
/// leaked `'static` so the monitor outlives any borrow.
struct PeerRig {
    monitor: Arc<Monitor<'static>>,
    client: MonitorClient,
    addr: SocketAddr,
    thread: Option<thread::JoinHandle<()>>,
}

impl PeerRig {
    /// Assembles a peer executor over a private stub driver — `gate`
    /// `None`: pair-view tests exercise roles and routing, not field
    /// quiescence — and serves its monitor on a spawned thread.
    fn start(role: Role) -> Self {
        let driver: &'static StubDriver = Box::leak(Box::new(StubDriver::new(&[
            (PointId(10), Value::Float(0.0)),
            (PointId(20), Value::Float(0.0)),
            (PointId(30), Value::Float(0.0)),
        ])));
        let map = PointMap::new()
            .with_writable_point(PointId(10), Direction::In, ValueKind::Float)
            .with_point(PointId(20), Direction::Out, ValueKind::Float)
            .with_point(PointId(30), Direction::Out, ValueKind::Float);
        let executor = Executor::new(driver, map, vec![Box::new(Scale)]).unwrap();
        let peer = match role {
            Role::Active => Peer::active(executor, None),
            _ => Peer::standby(executor, None),
        };
        let monitor = Arc::new(Monitor::bind_peer("127.0.0.1:0", peer, signal_index()).unwrap());
        let addr = monitor.local_addr();
        let client = MonitorClient::new(addr);
        let serving = Arc::clone(&monitor);
        let thread = thread::spawn(move || serving.serve());
        Self {
            monitor,
            client,
            addr,
            thread: Some(thread),
        }
    }

    /// Stops serving and drops the monitor handle: the listener closes
    /// and later connects are refused — a simulated peer outage.
    fn stop(mut self) {
        self.monitor.shutdown();
        if let Some(thread) = self.thread.take() {
            thread.join().unwrap();
        }
    }
}

impl Drop for PeerRig {
    fn drop(&mut self) {
        self.monitor.shutdown();
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

/// The status `pair` last recorded for `addr`.
fn status_of(pair: &PairClient, addr: SocketAddr) -> &PeerStatus {
    pair.peers()
        .iter()
        .find(|peer| peer.addr() == addr)
        .unwrap()
        .status()
}

#[test]
fn role_endpoints_are_polled_and_the_active_sources_the_view() {
    let active = PeerRig::start(Role::Active);
    let standby = PeerRig::start(Role::Standby);
    let mut pair = PairClient::new([active.addr, standby.addr]);

    // Before the first poll nothing is known and no source is selected.
    assert!(matches!(pair.peers()[0].status(), PeerStatus::Unknown));
    assert_eq!(pair.source(), None);
    assert!(matches!(
        pair.snapshot().unwrap_err(),
        PairError::NoSourcePeer
    ));

    pair.poll_roles();
    // Both peers' /role answers are recorded — the proof the endpoints
    // were polled — and the settled-active peer sources the view.
    match status_of(&pair, active.addr) {
        PeerStatus::Reporting(report) => {
            assert_eq!(report.role, Role::Active);
            assert_eq!(report.sync, None);
        }
        other => panic!("expected the active's report, got {other:?}"),
    }
    match status_of(&pair, standby.addr) {
        PeerStatus::Reporting(report) => {
            assert_eq!(report.role, Role::Standby);
            assert_eq!(report.sync, Some(StandbySync::Unsynchronized));
        }
        other => panic!("expected the standby's report, got {other:?}"),
    }
    assert_eq!(pair.source(), Some(active.addr));

    // The logical controller's data is the active peer's: snapshots
    // advance with its scans, and both peers serve the same signal index.
    active.client.advance(2).unwrap();
    standby.client.advance(1).unwrap();
    assert_eq!(pair.snapshot().unwrap().tick, Tick(2));
    assert_eq!(pair.signals().unwrap(), signal_index());
}

#[test]
fn commands_route_only_to_the_peer_reporting_active() {
    let active = PeerRig::start(Role::Active);
    let standby = PeerRig::start(Role::Standby);
    let mut pair = PairClient::new([standby.addr, active.addr]);
    pair.poll_roles();
    assert_eq!(pair.source(), Some(active.addr));

    active.client.advance(1).unwrap();
    let command = write_value(10, ValueKind::Float, Value::Float(5.0));
    let receipt = pair.command(&command).unwrap();
    assert_eq!(
        receipt.outcome,
        CommandOutcome::Accepted {
            apply_tick: Tick(2)
        }
    );

    // The command entered the active's receipt log; the standby was never
    // sent it — nothing queued, nothing journaled.
    assert_eq!(
        active.client.receipts().unwrap(),
        &[CommandReceipt {
            command,
            outcome: CommandOutcome::Accepted {
                apply_tick: Tick(2)
            },
        }]
    );
    assert!(standby.client.receipts().unwrap().is_empty());
    assert!(standby.client.journal(0).unwrap().is_empty());
}

#[test]
fn dropped_standby_is_a_named_redundancy_fault_while_the_active_view_runs() {
    let active = PeerRig::start(Role::Active);
    let standby = PeerRig::start(Role::Standby);
    let standby_addr = standby.addr;
    let mut pair = PairClient::new([active.addr, standby_addr]);
    pair.poll_roles();
    assert_eq!(pair.source(), Some(active.addr));

    // The standby process drops: serving stops and the listener closes.
    standby.stop();
    pair.poll_roles();

    // The named redundancy fault — a peer outage is pair health, not a
    // plant fault.
    match status_of(&pair, standby_addr) {
        PeerStatus::Unreachable { detail } => assert!(!detail.is_empty()),
        other => panic!("expected the unreachable redundancy fault, got {other:?}"),
    }
    assert!(matches!(
        status_of(&pair, active.addr),
        PeerStatus::Reporting(report) if report.role == Role::Active
    ));

    // The active view keeps updating through the standby's outage:
    // snapshots still flow and commands still land on the active.
    active.client.advance(1).unwrap();
    assert_eq!(pair.snapshot().unwrap().tick, Tick(1));
    active.client.advance(1).unwrap();
    assert_eq!(pair.snapshot().unwrap().tick, Tick(2));

    let receipt = pair
        .command(&write_value(10, ValueKind::Float, Value::Float(3.0)))
        .unwrap();
    assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
    assert_eq!(active.client.receipts().unwrap().len(), 1);

    active.stop();
}

#[test]
fn role_flip_moves_the_command_target_and_the_tick_domain_continues() {
    let peer_a = PeerRig::start(Role::Active);
    let peer_b = PeerRig::start(Role::Standby);
    let mut pair = PairClient::new([peer_a.addr, peer_b.addr]);

    // A runs three ticks; B applies the checkpoint — converging to
    // Tracking — and tracks one more tick alongside A.
    peer_a.client.advance(3).unwrap();
    peer_b
        .monitor
        .apply_checkpoint(&peer_a.client.checkpoint().unwrap())
        .unwrap();
    peer_a.client.advance(1).unwrap();
    peer_b.client.advance(1).unwrap();

    pair.poll_roles();
    assert_eq!(pair.source(), Some(peer_a.addr));
    // The view's history from A ends at tick 4.
    let history_a = pair.history(&[PointId(10)], 0).unwrap();
    let last_a = history_a[0].samples.last().unwrap().sample.tick;
    assert_eq!(last_a, Tick(4));

    // The command target is the settled-active peer.
    let first = write_value(10, ValueKind::Float, Value::Float(5.0));
    let receipt = pair.command(&first).unwrap();
    assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
    assert_eq!(peer_a.client.receipts().unwrap().len(), 1);
    assert!(peer_b.client.receipts().unwrap().is_empty());

    // Switchover without telling the pair: A demotes, the converged B
    // promotes and settles to active on its next scan.
    assert_eq!(peer_a.client.demote().unwrap().role, Role::Demoting);
    assert_eq!(peer_b.client.promote().unwrap().role, Role::Promoting);
    peer_b.client.advance(1).unwrap();
    assert_eq!(peer_b.client.role().unwrap().role, Role::Active);

    // The pair's stored status still names A the active: its command
    // lands on the demoting A, is rejected not_active, and the client
    // re-polls both roles and retries once — against the new active B.
    let second = write_value(10, ValueKind::Float, Value::Float(7.0));
    let receipt = pair.command(&second).unwrap();
    assert!(
        matches!(receipt.outcome, CommandOutcome::Accepted { .. }),
        "{receipt:?}"
    );
    assert_eq!(pair.source(), Some(peer_b.addr));

    // The re-poll saw A mid-transition, and A journaled the refused
    // command rather than queueing it — the receipt log proves only the
    // pre-flip command reached A's executor.
    assert!(matches!(
        status_of(&pair, peer_a.addr),
        PeerStatus::Reporting(report) if report.role == Role::Demoting
    ));
    assert_eq!(peer_a.client.receipts().unwrap().len(), 1);
    assert_eq!(peer_b.client.receipts().unwrap().len(), 1);
    assert!(
        peer_a
            .client
            .journal(0)
            .unwrap()
            .iter()
            .any(|entry| matches!(
                &entry.event,
                JournalEvent::CommandSettled { receipt }
                    if receipt.command == second
                        && matches!(
                            receipt.outcome,
                            CommandOutcome::Rejected {
                                reason: CommandError::NotActive { .. }
                            }
                        )
            )),
        "A journaled the mid-transition refusal"
    );

    // Tick continuity: the new source's history overlaps the old one's
    // tail — B's tracking scan recorded tick 4 — and continues it, so a
    // pane merging by tick draws one unbroken run.
    let history_b = pair.history(&[PointId(10)], 0).unwrap();
    let ticks_b: Vec<Tick> = history_b[0]
        .samples
        .iter()
        .map(|entry| entry.sample.tick)
        .collect();
    assert_eq!(ticks_b, vec![last_a, Tick(last_a.0 + 1)]);

    // The journal likewise continues on the new source: B's role
    // transitions are journaled at ticks continuing the run.
    let journal = pair.journal(0).unwrap();
    assert!(
        journal.iter().any(|entry| matches!(
            entry.event,
            JournalEvent::RoleChanged {
                from: Role::Promoting,
                to: Role::Active,
            }
        )),
        "the promoted peer's settle is journaled"
    );

    peer_a.stop();
    peer_b.stop();
}

#[test]
fn a_command_is_never_sent_to_a_standby_role_peer() {
    // No peer reporting active — both standby, as during a transition
    // window: the command is not sent at all.
    let a = PeerRig::start(Role::Standby);
    let b = PeerRig::start(Role::Standby);
    let mut pair = PairClient::new([a.addr, b.addr]);
    pair.poll_roles();

    let error = pair
        .command(&write_value(10, ValueKind::Float, Value::Float(1.0)))
        .unwrap_err();
    assert!(matches!(error, PairError::NoActivePeer), "{error:?}");
    // Nothing reached either peer: no queued receipt, no journaled
    // refusal.
    assert!(a.client.receipts().unwrap().is_empty());
    assert!(b.client.receipts().unwrap().is_empty());
    assert!(a.client.journal(0).unwrap().is_empty());
    assert!(b.client.journal(0).unwrap().is_empty());

    a.stop();
    b.stop();
}

#[test]
fn page_carries_the_pair_view_and_answers_cross_origin_role_reads() {
    let active = PeerRig::start(Role::Active);

    // The served page contains the pair-view machinery: ?peer=
    // configuration, /role polling, the pair-health section, and the
    // active-only command path with its mid-transition retry.
    let page = dcs_monitor::PAGE;
    for needle in [
        "getAll(\"peer\")",
        "\"/role\"",
        "id=\"pair\"",
        "pair-peers",
        "pair-summary",
        "redundancy fault",
        "unreachable",
        "function pollRoles()",
        "function selectSource()",
        "function switchSource(next)",
        "function submitCommand(command)",
        "not_active",
        "role_changed",
    ] {
        assert!(page.contains(needle), "page lacks {needle}");
    }

    // The contract endpoints answer cross-origin reads so the page can
    // poll a peer on another origin.
    let mut stream = TcpStream::connect(active.addr).unwrap();
    stream
        .write_all(b"GET /role HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        .unwrap();
    let mut raw = String::new();
    stream.read_to_string(&mut raw).unwrap();
    assert!(
        raw.to_lowercase()
            .contains("access-control-allow-origin: *"),
        "role response lacks the cross-origin header: {raw}"
    );

    active.stop();
}
