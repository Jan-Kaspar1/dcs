//! The driven standby's per-scan tracking cycle: `POST /scan` runs
//! `Peer::track_once` inside the request's boundary — one checkpoint
//! pull while the peer does not own the field, heartbeat-miss
//! accounting, and the promote-on-budget sequence — and drains the
//! queues it fills into the recorder's journal, exactly as the paced
//! monitored loop's `track_cycle` does.

use dcs_core::{
    Direction, Divergence, IoDriver, IoError, JournalEvent, PointId, Role, Sample, StandbySync,
    Tick, Value, ValueKind,
};
use dcs_model::{PlantModel, SignalIndex};
use dcs_monitor::{CheckpointPuller, Driven, Monitor, MonitorClient};
use dcs_runtime::{
    Component, ComponentIo, ComponentIoExt, Executor, IoRequirement, Peer, PointMap, StepError,
};
use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

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

fn executor(driver: &(dyn IoDriver + Sync)) -> Executor<'_> {
    let map = PointMap::new()
        .with_writable_point(PointId(10), Direction::In, ValueKind::Float)
        .with_point(PointId(20), Direction::Out, ValueKind::Float)
        .with_point(PointId(30), Direction::Out, ValueKind::Float);
    Executor::new(driver, map, vec![Box::new(Scale)]).unwrap()
}

/// One serving monitor: the `Arc` shares the handle so `stop` can drop
/// the last reference — closing the listener so a later connect is
/// refused, the simulated process outage the pair tests use.
struct Serving {
    monitor: Arc<Monitor<'static>>,
    client: MonitorClient,
    thread: Option<JoinHandle<()>>,
}

impl Serving {
    fn start(monitor: Monitor<'static>) -> Self {
        let monitor = Arc::new(monitor);
        let client = MonitorClient::new(monitor.local_addr());
        let serving = Arc::clone(&monitor);
        Self {
            monitor,
            client,
            thread: Some(thread::spawn(move || serving.serve())),
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

impl Drop for Serving {
    fn drop(&mut self) {
        self.monitor.shutdown();
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

/// A driven standby whose `POST /scan` pulls checkpoints from the
/// active at `track` — the wiring `dcs-controller --driven --standby`
/// installs. Drivers are leaked `'static` so the monitors outlive any
/// borrow.
struct DrivenStandby {
    standby: Serving,
    standby_driver: &'static StubDriver,
    active_addr: SocketAddr,
}

impl DrivenStandby {
    /// `failover` arms the standby's miss budget — `None` keeps
    /// promotion manual. Returns the standby rig and the serving
    /// active it tracks.
    fn start(failover: Option<u32>) -> (Self, Serving) {
        let active_driver: &'static StubDriver = Box::leak(Box::new(StubDriver::new(&[
            (PointId(10), Value::Float(3.0)),
            (PointId(20), Value::Float(0.0)),
            (PointId(30), Value::Float(0.0)),
        ])));
        let active = Serving::start(
            Monitor::bind_peer(
                "127.0.0.1:0",
                Peer::active(executor(active_driver), None),
                signal_index(),
            )
            .unwrap(),
        );
        let active_addr = active.monitor.local_addr();

        let standby_driver: &'static StubDriver = Box::leak(Box::new(StubDriver::new(&[
            (PointId(10), Value::Float(3.0)),
            (PointId(20), Value::Float(0.0)),
            (PointId(30), Value::Float(0.0)),
        ])));
        let mut peer = Peer::standby(executor(standby_driver), None);
        if let Some(budget) = failover {
            peer = peer.with_failover(budget);
        }
        let standby = Serving::start(
            Monitor::bind_peer("127.0.0.1:0", peer, signal_index())
                .unwrap()
                .driven(Driven {
                    track: Some(active_addr),
                    after_scan: None,
                }),
        );
        (
            Self {
                standby,
                standby_driver,
                active_addr,
            },
            active,
        )
    }
}

/// The journaled role transitions, in order.
fn role_changes(client: &MonitorClient) -> Vec<JournalEvent> {
    client
        .journal(0)
        .unwrap()
        .iter()
        .filter_map(|entry| match &entry.event {
            JournalEvent::RoleChanged { .. } => Some(entry.event.clone()),
            _ => None,
        })
        .collect()
}

/// A produced checkpoint converges the standby through the driven pull;
/// once the active is unreachable the miss budget is met and the
/// self-promotion's role transitions journal — the driven path draining
/// the same queues the paced monitored path's recorder drains.
#[test]
fn driven_track_cycle_journals_the_self_promotion_role_changes() {
    let (standby, active) = DrivenStandby::start(Some(2));

    // Converge: the requested scan pulls the active's checkpoint first,
    // then scans on the aligned run.
    active.client.advance(3).unwrap();
    standby.standby.client.advance(1).unwrap();
    let report = standby.standby.client.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert_eq!(
        report.sync,
        Some(StandbySync::Tracking { aligned: Tick(3) })
    );

    // Active loss: the first produced-nothing pull is one miss under
    // budget — the peer reports degraded and changes no role.
    let active_addr = standby.active_addr;
    active.stop();
    standby.standby.client.advance(1).unwrap();
    let report = standby.standby.client.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert!(
        matches!(
            &report.sync,
            Some(StandbySync::Degraded { detail })
                if detail.starts_with(&format!("fetch from {active_addr}"))
        ),
        "the failed pull reports its detail: {report:?}"
    );
    assert!(
        role_changes(&standby.standby.client).is_empty(),
        "one miss under budget changes no role"
    );

    // The budget-th miss self-promotes at that boundary: the role
    // changes the cycle queued drain into the journal — the request's
    // promotion, then the scan settling it.
    standby.standby.client.advance(1).unwrap();
    let report = standby.standby.client.role().unwrap();
    assert_eq!(report.role, Role::Active);
    assert_eq!(
        role_changes(&standby.standby.client),
        vec![
            JournalEvent::RoleChanged {
                from: Role::Standby,
                to: Role::Promoting,
            },
            JournalEvent::RoleChanged {
                from: Role::Promoting,
                to: Role::Active,
            },
        ]
    );

    // Field-owning now: the cycle is a no-op — no pull, no further
    // degraded report even though the active stays unreachable.
    standby.standby.client.advance(1).unwrap();
    let report = standby.standby.client.role().unwrap();
    assert_eq!(report.role, Role::Active);
    assert_eq!(report.sync, None);
}

/// A produced-nothing pull on a standby whose convergence proof does
/// not stand reports the named degraded state instead of promoting —
/// the promotion refusal the driven path reports through `GET /role`.
#[test]
fn driven_track_cycle_reports_the_refused_self_promotion() {
    let (standby, active) = DrivenStandby::start(Some(1));

    // The standby never converged; active loss at budget meets the
    // failover check but self-promotion is refused — `GET /role`
    // reports the named state, and no role change journals.
    active.stop();
    standby.standby.client.advance(1).unwrap();
    let report = standby.standby.client.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert!(
        matches!(&report.sync, Some(StandbySync::Degraded { .. })),
        "the refused promotion leaves the named state: {report:?}"
    );
    assert!(
        role_changes(&standby.standby.client).is_empty(),
        "a refused promotion journals no role change"
    );
}

/// A staged image the applied checkpoint's tick matches still runs the
/// divergence check on the driven path, and the transition drains into
/// the journal at the compared tick.
#[test]
fn driven_track_cycle_journals_the_divergence_transition() {
    let (standby, active) = DrivenStandby::start(None);

    active.client.advance(3).unwrap();
    // The pull applies checkpoint@3; the scan then stages the tick-4
    // `Out` image this private-plant standby would have written.
    standby.standby.client.advance(1).unwrap();

    // Bend the standby's view of the field: its own `Out` read — the
    // divergence check's field side — no longer matches what its last
    // scan staged.
    standby
        .standby_driver
        .write(PointId(20), Value::Float(99.0))
        .unwrap();
    active.client.advance(1).unwrap();
    standby.standby.client.advance(1).unwrap();

    let report = standby.standby.client.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert_eq!(
        report.sync,
        Some(StandbySync::Diverged {
            mismatches: vec![Divergence {
                point: PointId(20),
                staged: Value::Float(6.0),
                field: Value::Float(99.0),
            }]
        })
    );
    let journal = standby.standby.client.journal(0).unwrap();
    assert!(
        journal.iter().any(|entry| matches!(
            &entry.event,
            JournalEvent::DivergenceDetected { mismatches }
                if *mismatches
                    == vec![Divergence {
                        point: PointId(20),
                        staged: Value::Float(6.0),
                        field: Value::Float(99.0),
                    }]
                && entry.tick == Tick(4)
        )),
        "the driven cycle journaled the divergence at the compared tick: {journal:?}"
    );
}

/// The paced-standby reproduction of the QA finding
/// `monitor-requests-blocked-by-dead-peer-pull`: while the tracking
/// pull stalls on a dead active, `GET /role` and `GET /snapshot` must
/// stay far below the fetch's own wait — the fetch runs on the pull
/// worker's thread, and the per-scan `track_cycle` consumes it
/// non-blockingly outside the request-serving lock — and the paced
/// scan cadence the failover miss budget counts in must not inflate
/// toward the fetch's stall.
fn assert_dead_peer_pull_stays_off_the_request_path(dead: SocketAddr) {
    let standby_driver: &'static StubDriver = Box::leak(Box::new(StubDriver::new(&[
        (PointId(10), Value::Float(3.0)),
        (PointId(20), Value::Float(0.0)),
        (PointId(30), Value::Float(0.0)),
    ])));
    let standby = Serving::start(
        Monitor::bind_paced_peer(
            "127.0.0.1:0",
            Peer::standby(executor(standby_driver), None),
            signal_index(),
        )
        .unwrap(),
    );

    // The paced loop, shaped like dcs-controller's: one tracking cycle
    // consuming the fetch worker's latest pull, then one paced scan.
    let mut puller = CheckpointPuller::new(dead);
    let pacing = Arc::clone(&standby.monitor);
    let stop = Arc::new(AtomicBool::new(false));
    let stopping = Arc::clone(&stop);
    let pacer = thread::spawn(move || {
        while !stopping.load(Ordering::Relaxed) {
            pacing.track_cycle(|| puller.poll());
            let _ = pacing.paced_scan();
            thread::sleep(Duration::from_millis(10));
        }
    });

    // Every monitor read stays far under the pull's own stall, and the
    // degraded heartbeat reports — the dead peer is exactly when the
    // operator needs the endpoints.
    for _ in 0..20 {
        let started = Instant::now();
        standby.client.role().unwrap();
        let role_elapsed = started.elapsed();
        let started = Instant::now();
        standby.client.snapshot().unwrap();
        let snapshot_elapsed = started.elapsed();
        assert!(
            role_elapsed < Duration::from_millis(500)
                && snapshot_elapsed < Duration::from_millis(500),
            "monitor requests serialized behind the dead peer's pull: \
             /role took {role_elapsed:?}, /snapshot took {snapshot_elapsed:?}"
        );
        thread::sleep(Duration::from_millis(10));
    }
    let report = standby.client.role().unwrap();
    assert!(
        matches!(&report.sync, Some(StandbySync::Degraded { .. })),
        "the failed pulls report the degraded heartbeat: {report:?}"
    );
    let first = report.tick;
    thread::sleep(Duration::from_millis(400));
    let later = standby.client.role().unwrap().tick;
    assert!(
        later.0 - first.0 >= 10,
        "the paced scan cadence held while the pull stalled: {first:?} -> {later:?}"
    );

    stop.store(true, Ordering::Relaxed);
    pacer.join().unwrap();
}

/// The reproduction's literal case: the active's address is unroutable
/// (TEST-NET-1, RFC 5737), so each pull stalls on the connect until
/// the dedicated pull bound.
#[test]
fn unroutable_active_pull_keeps_the_monitor_responsive() {
    assert_dead_peer_pull_stays_off_the_request_path("192.0.2.1:8080".parse().unwrap());
}

/// The guaranteed-stalled case: a listener that accepts connections
/// but never answers, so every pull sits in flight until the pull
/// bound — requests must not wait on it.
#[test]
fn silent_active_pull_keeps_the_monitor_responsive() {
    let silent = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    assert_dead_peer_pull_stays_off_the_request_path(silent.local_addr().unwrap());
}

/// The lock property behind the reproduction's fix, without any
/// network timing: `track_cycle` invokes its `pull` outside the
/// request-serving lock, so even a deliberately slow pull cannot make
/// a request wait on it.
#[test]
fn track_cycles_pull_does_not_hold_the_request_serving_lock() {
    let standby_driver: &'static StubDriver = Box::leak(Box::new(StubDriver::new(&[
        (PointId(10), Value::Float(3.0)),
        (PointId(20), Value::Float(0.0)),
        (PointId(30), Value::Float(0.0)),
    ])));
    let standby = Serving::start(
        Monitor::bind_paced_peer(
            "127.0.0.1:0",
            Peer::standby(executor(standby_driver), None),
            signal_index(),
        )
        .unwrap(),
    );

    let pacing = Arc::clone(&standby.monitor);
    let stop = Arc::new(AtomicBool::new(false));
    let stopping = Arc::clone(&stop);
    let pacer = thread::spawn(move || {
        while !stopping.load(Ordering::Relaxed) {
            // A pull that stalls well past the asserted request bound.
            pacing.track_cycle(|| {
                thread::sleep(Duration::from_millis(400));
                Err("fetch from 192.0.2.1:8080: stalled".to_string())
            });
            let _ = pacing.paced_scan();
        }
    });
    thread::sleep(Duration::from_millis(50));

    let started = Instant::now();
    standby.client.role().unwrap();
    let elapsed = started.elapsed();
    assert!(
        elapsed < Duration::from_millis(200),
        "GET /role waited on the in-flight pull: {elapsed:?}"
    );

    stop.store(true, Ordering::Relaxed);
    pacer.join().unwrap();
}
