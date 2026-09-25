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
    Command, CommandError, CommandOutcome, CommandReceipt, Direction, FieldClaim, IoDriver,
    IoError, JournalEvent, PointId, Role, Sample, StandbySync, Tick, Value, ValueKind,
};
use dcs_model::{PlantModel, SignalIndex};
use dcs_monitor::{
    Monitor, MonitorClient, PAIR_FAULT_KINDS_VERSION, PairClient, PairError, PairFaultKind,
    PairHealth, PeerStatus,
};
use dcs_runtime::{
    Component, ComponentIo, ComponentIoExt, Executor, IoRequirement, Peer, PointMap, StepError,
};
use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

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
        Self::start_tracking(role, None)
    }

    /// [`start`](Self::start) with `source` recorded as the monitor's
    /// tracking source — the configured `--peer`/`--standby` half of
    /// the follow-peer contract a demotion tracks.
    fn start_tracking(role: Role, source: Option<SocketAddr>) -> Self {
        Self::assemble(role, source, None)
    }

    /// [`start`](Self::start) with a scripted field-claim probe: the
    /// peer's per-scan claim observation answers whatever `claim`
    /// currently holds, so a test drives `field_claim` through `held` /
    /// `unclaimed` without field-side arbitration.
    fn start_probed(role: Role, claim: Arc<Mutex<FieldClaim>>) -> Self {
        Self::assemble(role, None, Some(claim))
    }

    fn assemble(
        role: Role,
        source: Option<SocketAddr>,
        claim: Option<Arc<Mutex<FieldClaim>>>,
    ) -> Self {
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
        let peer = match claim {
            Some(claim) => peer.with_field_probe(move || Ok(*claim.lock().unwrap())),
            None => peer,
        };
        let monitor = Monitor::bind_peer("127.0.0.1:0", peer, signal_index()).unwrap();
        let monitor = match source {
            Some(source) => monitor.with_standby_source(source),
            None => monitor,
        };
        let monitor = Arc::new(monitor);
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

fn assert_fault_kinds(health: &PairHealth, expected: &[PairFaultKind]) {
    assert_eq!(health.fault_kinds_version, Some(PAIR_FAULT_KINDS_VERSION));
    assert_eq!(health.faults.len(), health.fault_kinds.len());
    assert_eq!(health.fault_kinds, expected);
}

#[test]
fn pair_health_fault_kinds_roundtrip() {
    for kind in PairFaultKind::ALL {
        let health = PairHealth {
            active: Some("127.0.0.1:5800".parse().unwrap()),
            faults: vec!["diagnostic detail".to_string()],
            fault_kinds_version: Some(PAIR_FAULT_KINDS_VERSION),
            fault_kinds: vec![kind],
        };
        let json = serde_json::to_value(&health).unwrap();
        assert_eq!(json["fault_kinds_version"], 3);
        assert_eq!(json["fault_kinds"], serde_json::json!([kind]));
        assert_eq!(serde_json::from_value::<PairHealth>(json).unwrap(), health);
    }
}

#[test]
fn legacy_pair_health_accepts_absent_fault_kind_fields() {
    for faults in [vec![], vec!["legacy diagnostic detail"]] {
        let json = serde_json::json!({
            "active": "127.0.0.1:5800",
            "faults": faults,
        });
        let health: PairHealth = serde_json::from_value(json.clone()).unwrap();
        assert_eq!(health.active, Some("127.0.0.1:5800".parse().unwrap()));
        assert_eq!(health.faults, faults);
        assert_eq!(health.fault_kinds_version, None);
        assert!(health.fault_kinds.is_empty());
        assert_eq!(serde_json::to_value(&health).unwrap(), json);
    }
}

#[test]
fn healthy_pair_health_omits_empty_fault_kinds_and_roundtrips() {
    let health = PairHealth {
        active: Some("127.0.0.1:5800".parse().unwrap()),
        faults: vec![],
        fault_kinds_version: Some(PAIR_FAULT_KINDS_VERSION),
        fault_kinds: vec![],
    };
    let json = serde_json::to_value(&health).unwrap();
    assert_eq!(json["fault_kinds_version"], 3);
    assert!(json.get("fault_kinds").is_none());
    assert_eq!(serde_json::from_value::<PairHealth>(json).unwrap(), health);
}

#[test]
fn pair_health_rejects_unknown_fault_kinds() {
    let json = serde_json::json!({
        "active": null,
        "faults": ["unknown diagnostic detail"],
        "fault_kinds_version": 1,
        "fault_kinds": ["unknown_pair_fault"],
    });
    assert!(serde_json::from_value::<PairHealth>(json).is_err());
    assert!(serde_json::from_str::<PairFaultKind>(r#""unknown_pair_fault""#).is_err());
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
            actor: None,
            reason: None,
        }]
    );
    assert!(standby.client.receipts().unwrap().is_empty());
    assert!(standby.client.journal(0).unwrap().is_empty());

    // A component-targeted command — the set_parameter the faceplate's
    // parameter edits submit — rides the same active-only routing: the
    // active's receipt log answers with the named rejection (the Scale
    // kind declares no parameters), and the standby was never sent it.
    let tune = Command::SetParameter {
        component: "scale".to_string(),
        name: "gain".to_string(),
        value: Value::Float(2.0),
    };
    let receipt = pair.command(&tune).unwrap();
    assert_eq!(
        receipt.outcome,
        CommandOutcome::Rejected {
            reason: CommandError::UnsupportedParameter {
                component: "scale".to_string(),
                parameter: "gain".to_string(),
            }
        }
    );
    assert_eq!(active.client.receipts().unwrap().len(), 2);
    assert!(standby.client.receipts().unwrap().is_empty());
}

#[test]
fn dropped_standby_is_a_named_redundancy_fault_while_the_active_view_runs() {
    let active = PeerRig::start(Role::Active);
    let standby = PeerRig::start(Role::Standby);
    let standby_addr = standby.addr;
    let mut pair = PairClient::new([active.addr, standby_addr]);
    pair.poll_roles();
    assert_eq!(pair.source(), Some(active.addr));
    assert_fault_kinds(&pair.health(), &[]);

    // The standby process drops: serving stops and the listener closes.
    standby.stop();
    pair.poll_roles();
    let health = pair.health();
    assert_eq!(health.active, Some(active.addr));
    assert_fault_kinds(&health, &[PairFaultKind::PeerUnreachable]);
    assert_eq!(health.faults[0], format!("{standby_addr} unreachable"));

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

    let active_addr = active.addr;
    active.stop();
    pair.poll_roles();
    let health = pair.health();
    assert_eq!(health.active, None);
    assert_fault_kinds(
        &health,
        &[
            PairFaultKind::PeerUnreachable,
            PairFaultKind::PeerUnreachable,
            PairFaultKind::NoActivePeer,
        ],
    );
    assert_eq!(
        health.faults,
        [
            format!("{active_addr} unreachable"),
            format!("{standby_addr} unreachable"),
            "no peer reports role active".to_string(),
        ]
    );
}

#[test]
fn role_flip_moves_the_command_target_and_the_tick_domain_continues() {
    let peer_b = PeerRig::start(Role::Standby);
    // A launched active naming its peer — the configured tracking
    // source its demotion follows.
    let peer_a = PeerRig::start_tracking(Role::Active, Some(peer_b.addr));
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
                ..
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
    let health = pair.health();
    assert_eq!(health.active, None);
    assert_fault_kinds(&health, &[PairFaultKind::NoActivePeer]);
    assert_eq!(health.faults[0], "no peer reports role active");

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
fn dual_active_is_a_named_redundancy_fault_and_commands_have_no_target() {
    // The fenced-active split-brain: two peers both reporting settled
    // active — the impossible state the one-logical-controller contract
    // cannot represent. The pair view must name it, not render
    // "redundant pair healthy". `fenced` names `promoted` as its
    // tracking source — the configured `--peer` — so the demotion below
    // has somewhere to track.
    let promoted = PeerRig::start(Role::Active);
    let fenced = PeerRig::start_tracking(Role::Active, Some(promoted.addr));
    let mut pair = PairClient::new([fenced.addr, promoted.addr]);
    pair.poll_roles();

    // The summary verdict: both peers' reports are recorded, the
    // dual-active fault names both claimants, and no unique active
    // stands.
    match status_of(&pair, fenced.addr) {
        PeerStatus::Reporting(report) => assert_eq!(report.role, Role::Active),
        other => panic!("expected the fenced peer's report, got {other:?}"),
    }
    let health = pair.health();
    assert_eq!(health.active, None);
    assert_fault_kinds(&health, &[PairFaultKind::DualActive]);
    assert!(
        health
            .faults
            .iter()
            .any(|fault| fault.contains("dual-active")
                && fault.contains(&fenced.addr.to_string())
                && fault.contains(&promoted.addr.to_string())),
        "expected the dual-active fault naming both peers, got {:?}",
        health.faults
    );

    // Reads are held, not interrupted: the view keeps its source and
    // data keeps flowing while the fault names the ambiguity.
    assert_eq!(pair.source(), Some(fenced.addr));
    fenced.client.advance(1).unwrap();
    assert_eq!(pair.snapshot().unwrap().tick, Tick(1));

    // Command routing has no legitimate target: nothing is sent to
    // either claimant — no queued receipt, no journaled command.
    let error = pair
        .command(&write_value(10, ValueKind::Float, Value::Float(1.0)))
        .unwrap_err();
    assert!(matches!(error, PairError::AmbiguousActive), "{error:?}");
    assert!(fenced.client.receipts().unwrap().is_empty());
    assert!(promoted.client.receipts().unwrap().is_empty());
    for rig in [&fenced, &promoted] {
        assert!(
            rig.client
                .journal(0)
                .unwrap()
                .iter()
                .all(|entry| !matches!(entry.event, JournalEvent::CommandSettled { .. })),
            "no command reached the claimant"
        );
    }

    // The fault is poll-driven, not sticky: once the fenced claimant
    // demotes and settles to standby the pair is healthy again and
    // commands route to the surviving unique active.
    assert_eq!(fenced.client.demote().unwrap().role, Role::Demoting);
    fenced.client.advance(1).unwrap();
    pair.poll_roles();
    let health = pair.health();
    assert_eq!(health.active, Some(promoted.addr));
    assert!(health.faults.is_empty(), "{:?}", health.faults);
    assert_fault_kinds(&health, &[]);
    let receipt = pair
        .command(&write_value(10, ValueKind::Float, Value::Float(1.0)))
        .unwrap();
    assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
    assert_eq!(promoted.client.receipts().unwrap().len(), 1);
    assert!(fenced.client.receipts().unwrap().is_empty());

    fenced.stop();
    promoted.stop();
}

#[test]
fn an_unsynchronized_standby_past_the_convergence_grace_is_a_named_fault() {
    let active = PeerRig::start(Role::Active);
    let standby = PeerRig::start(Role::Standby);

    // Inside the grace the report is the legitimate transient: a fresh
    // standby whose first tracking pulls have not landed yet is not a
    // redundancy fault — the pair view must not flap "redundancy fault"
    // over every launch or demotion.
    let mut pair = PairClient::new([active.addr, standby.addr]);
    pair.poll_roles();
    let health = pair.health();
    assert_eq!(health.active, Some(active.addr));
    assert!(health.faults.is_empty(), "{:?}", health.faults);
    assert_fault_kinds(&health, &[]);

    // Past the grace — here a zero grace, so the first observed report
    // already exceeds it — a peer still reporting unsynchronized cannot
    // demonstrate convergence: the permanent state of a demoted peer
    // with no checkpoint source, which no promotion can recover
    // (not_converged) and no pull can reach. The pair has zero failover
    // coverage, so the verdict names it rather than rendering a
    // healthy pair.
    let mut lapsed =
        PairClient::new([active.addr, standby.addr]).with_convergence_grace(Duration::ZERO);
    lapsed.poll_roles();
    let health = lapsed.health();
    assert_eq!(health.active, Some(active.addr));
    assert_fault_kinds(&health, &[PairFaultKind::StandbyUnsynchronizedPastGrace]);
    assert!(
        health
            .faults
            .iter()
            .any(|fault| fault.contains(&standby.addr.to_string())
                && fault.contains("unsynchronized")),
        "expected the unconverged standby named as a redundancy fault, got {:?}",
        health.faults
    );

    // The verdict is not sticky: a checkpoint apply converges the
    // standby to tracking and the fault clears — the pair is healthy
    // again even under the zero grace.
    active.client.advance(1).unwrap();
    standby
        .monitor
        .apply_checkpoint(&active.client.checkpoint().unwrap())
        .unwrap();
    lapsed.poll_roles();
    let health = lapsed.health();
    assert_eq!(health.active, Some(active.addr));
    assert!(health.faults.is_empty(), "{:?}", health.faults);
    assert_fault_kinds(&health, &[]);

    // The page carries the same verdict: its pairHealth faults a peer
    // still reporting "unsynchronized" past the same grace — over the
    // reproduction's permanently dead standby the page can no longer
    // return zero faults.
    let page = dcs_monitor::PAGE;
    for needle in [
        "CONVERGENCE_GRACE_MS",
        "noteSyncAge",
        "past the convergence grace",
        "\"unsynchronized\"",
    ] {
        assert!(page.contains(needle), "page lacks {needle}");
    }

    active.stop();
    standby.stop();
}

/// The mutual-standby wedge as pair health: a standby whose tracked
/// line has no field owner reports `orphaned`, and the pair verdict
/// names it `standby_orphaned` rather than rendering a healthy pair —
/// the `tracking` report that used to hide the outage.
#[test]
fn an_orphaned_standby_is_a_named_fault() {
    let active = PeerRig::start(Role::Active);
    let standby = PeerRig::start(Role::Standby);
    let mut pair = PairClient::new([active.addr, standby.addr]);

    // The wedge's signature: a checkpoint that applies cleanly but
    // whose serving run owns no field writes.
    active.client.advance(1).unwrap();
    let mut orphaned = active.client.checkpoint().unwrap();
    orphaned.source_owns_field = Some(false);
    standby.monitor.apply_checkpoint(&orphaned).unwrap();

    pair.poll_roles();
    match status_of(&pair, standby.addr) {
        PeerStatus::Reporting(report) => assert!(
            matches!(report.sync, Some(StandbySync::Orphaned { .. })),
            "the orphaned standby must not report healthy tracking: {report:?}"
        ),
        other => panic!("expected the standby's report, got {other:?}"),
    }
    let health = pair.health();
    assert_eq!(health.active, Some(active.addr));
    assert_fault_kinds(&health, &[PairFaultKind::StandbyOrphaned]);
    assert!(
        health
            .faults
            .iter()
            .any(|fault| fault.contains(&standby.addr.to_string())
                && fault.contains("no field owner")),
        "expected the orphaned standby named as a redundancy fault, got {:?}",
        health.faults
    );

    // A checkpoint from a field-owning source ends the fault — the
    // verdict is poll-driven, not sticky.
    active.client.advance(1).unwrap();
    standby
        .monitor
        .apply_checkpoint(&active.client.checkpoint().unwrap())
        .unwrap();
    pair.poll_roles();
    let health = pair.health();
    assert_eq!(health.active, Some(active.addr));
    assert!(health.faults.is_empty(), "{:?}", health.faults);
    assert_fault_kinds(&health, &[]);

    active.stop();
    standby.stop();
}

/// The unclaimed-field verdict as pair health: a reporting peer whose
/// served `field_claim` stands `unclaimed` — the field's own
/// arbitration answering "no owner stands" — is named the
/// `field_unclaimed` redundancy fault, distinctly from the
/// orphaned/degraded sync verdicts, and clears on the first poll after
/// a holder claims. `POST /promote` on a converged peer is the
/// documented remedy the fault names.
#[test]
fn an_unclaimed_field_report_is_a_named_fault_until_a_holder_claims() {
    let active = PeerRig::start(Role::Active);
    let claim = Arc::new(Mutex::new(FieldClaim::Held));
    let standby = PeerRig::start_probed(Role::Standby, Arc::clone(&claim));
    let mut pair = PairClient::new([active.addr, standby.addr]);

    // A held claim is no fault: one scan lands the probe's `held`
    // answer in the served report and the pair reads healthy — the
    // unprobed active serves no `field_claim` at all, rendering
    // exactly as before.
    standby.client.advance(1).unwrap();
    pair.poll_roles();
    match status_of(&pair, standby.addr) {
        PeerStatus::Reporting(report) => {
            assert_eq!(report.field_claim, Some(FieldClaim::Held))
        }
        other => panic!("expected the standby's report, got {other:?}"),
    }
    match status_of(&pair, active.addr) {
        PeerStatus::Reporting(report) => assert_eq!(report.field_claim, None),
        other => panic!("expected the active's report, got {other:?}"),
    }
    let health = pair.health();
    assert_eq!(health.active, Some(active.addr));
    assert!(health.faults.is_empty(), "{:?}", health.faults);
    assert_fault_kinds(&health, &[]);

    // The claim released — no field owner stands: the next polled
    // report names the unclaimed field as its own redundancy fault.
    *claim.lock().unwrap() = FieldClaim::Unclaimed;
    standby.client.advance(1).unwrap();
    pair.poll_roles();
    match status_of(&pair, standby.addr) {
        PeerStatus::Reporting(report) => {
            assert_eq!(report.field_claim, Some(FieldClaim::Unclaimed))
        }
        other => panic!("expected the standby's report, got {other:?}"),
    }
    let health = pair.health();
    assert_eq!(health.active, Some(active.addr));
    assert_fault_kinds(&health, &[PairFaultKind::FieldUnclaimed]);
    assert!(
        health
            .faults
            .iter()
            .any(|fault| fault.contains(&standby.addr.to_string()) && fault.contains("unclaimed")),
        "expected the unclaimed field named as a redundancy fault, got {:?}",
        health.faults
    );

    // The first poll after a holder claims clears the fault —
    // poll-driven, not sticky.
    *claim.lock().unwrap() = FieldClaim::Held;
    standby.client.advance(1).unwrap();
    pair.poll_roles();
    let health = pair.health();
    assert_eq!(health.active, Some(active.addr));
    assert!(health.faults.is_empty(), "{:?}", health.faults);
    assert_fault_kinds(&health, &[]);

    // The page's pair-health section names the same verdict
    // distinctly from the orphaned and degraded sync faults: the
    // versioned kind, the report field it reads, and the named
    // remedy.
    let page = dcs_monitor::PAGE;
    for needle in [
        "\"field_unclaimed\"",
        "field_claim === \"unclaimed\"",
        "the field unclaimed",
        "promote is the documented remedy",
    ] {
        assert!(page.contains(needle), "page lacks {needle}");
    }

    active.stop();
    standby.stop();
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
        "async function submitCommand(command, reason)",
        "async function postSwitch(peer, verb)",
        "class=\\\"switch\\\"",
        "data-verb=\\\"promote\\\"",
        "data-verb=\\\"demote\\\"",
        "not_active",
        "role_changed",
    ] {
        assert!(page.contains(needle), "page lacks {needle}");
    }
    // The dual-active defense: the summary counts actives and names the
    // split-brain fault, and the command path requires a unique
    // settled-active target rather than picking the first claimant.
    for needle in [
        "function pairHealth(pairPeers, states)",
        "dual-active",
        "function activePeers()",
        "actives.length === 1",
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
