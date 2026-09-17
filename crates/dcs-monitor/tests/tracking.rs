//! The driven standby's per-scan tracking cycle: `POST /scan` runs
//! `Peer::track_once` inside the request's boundary — one checkpoint
//! pull while the peer does not own the field, heartbeat-miss
//! accounting, and the promote-on-budget sequence — and drains the
//! queues it fills into the recorder's journal, exactly as the paced
//! monitored loop's `track_cycle` does.

use dcs_core::{
    ComponentDescriptor, Direction, Divergence, EmittedEvent, EventDecl, EventField,
    EventFieldKind, EventRetention, EventValue, IoDriver, IoError, JournalEvent, PointId, Role,
    Sample, StandbySync, StateMap, Tick, Value, ValueKind,
};
use dcs_model::{PlantModel, SignalIndex};
use dcs_monitor::{Driven, Monitor, MonitorClient};
use dcs_runtime::{
    Component, ComponentIo, ComponentIoExt, Executor, IoRequirement, Peer, PointMap, StepError,
};
use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};

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

/// A component emitting declared events during `step` — `fired`
/// (`Journal`-retained), `shift` (`History`), `beat` (`Latest`) per
/// scan, the payload's `n` counting emissions — the routed classes'
/// parity rig: the checkpointed `n` carries the emission sequence
/// across the tracking pull, so a standby's first tracked scan already
/// emits what the active's does.
struct Emitter {
    n: i64,
}

impl Emitter {
    fn event(event: &str, n: i64) -> EmittedEvent {
        EmittedEvent {
            event: event.to_string(),
            component: String::new(),
            fields: [("n".to_string(), EventValue::Value(Value::Int(n)))]
                .into_iter()
                .collect(),
        }
    }
}

impl Component for Emitter {
    fn name(&self) -> &str {
        "em"
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        Vec::new()
    }

    fn step(&mut self, _io: &dyn ComponentIo, _tick: Tick) -> Result<(), StepError> {
        self.n += 1;
        Ok(())
    }

    fn describe(&self) -> ComponentDescriptor {
        let event = |name: &str, retention: EventRetention| EventDecl {
            name: name.to_string(),
            payload: vec![EventField {
                name: "n".to_string(),
                kind: EventFieldKind::Value(ValueKind::Int),
                optional: false,
            }],
            retention,
        };
        ComponentDescriptor {
            name: "em".to_string(),
            kind: "emitter".to_string(),
            label: "em".to_string(),
            ports: Vec::new(),
            parameters: Vec::new(),
            commands: Vec::new(),
            events: vec![
                event("fired", EventRetention::Journal),
                event("shift", EventRetention::History),
                event("beat", EventRetention::Latest),
            ],
        }
    }

    fn drain_events(&mut self) -> Vec<EmittedEvent> {
        vec![
            Self::event("fired", self.n),
            Self::event("shift", self.n),
            Self::event("beat", self.n),
        ]
    }

    fn capture_state(&self) -> StateMap {
        let mut state = StateMap::new();
        state.insert("n", Value::Int(self.n));
        state
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), dcs_core::StateError> {
        state.ensure_known_fields("em", &["n"])?;
        self.n = state.require_i64("em", "n")?;
        Ok(())
    }
}

/// The model fixture behind the monitors; both peers serve the same index.
const MODEL: &str = include_str!("../fixtures/monitor.json");

fn signal_index() -> SignalIndex {
    PlantModel::load(MODEL).unwrap().signal_index()
}

fn executor(driver: &'static StubDriver) -> Executor<'static> {
    let map = PointMap::new()
        .with_writable_point(PointId(10), Direction::In, ValueKind::Float)
        .with_point(PointId(20), Direction::Out, ValueKind::Float)
        .with_point(PointId(30), Direction::Out, ValueKind::Float);
    Executor::new(driver, map, vec![Box::new(Scale)]).unwrap()
}

/// The parity rig's executor: one `Emitter` and no I/O surface — the
/// tracked emissions exercise every routed store.
fn emitter_executor(driver: &'static StubDriver) -> Executor<'static> {
    Executor::new(driver, PointMap::new(), vec![Box::new(Emitter { n: 0 })]).unwrap()
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
        Self::start_with(failover, executor)
    }

    /// `build` constructs each peer's executor over its private driver —
    /// the event-parity rig's emitter rides the same driven tracking
    /// cycle.
    fn start_with(
        failover: Option<u32>,
        build: fn(&'static StubDriver) -> Executor<'static>,
    ) -> (Self, Serving) {
        let active_driver: &'static StubDriver = Box::leak(Box::new(StubDriver::new(&[
            (PointId(10), Value::Float(3.0)),
            (PointId(20), Value::Float(0.0)),
            (PointId(30), Value::Float(0.0)),
        ])));
        let active = Serving::start(
            Monitor::bind_peer(
                "127.0.0.1:0",
                Peer::active(build(active_driver), None),
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
        let mut peer = Peer::standby(build(standby_driver), None);
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

/// Emit-identical parity at the served surface: the tracking peer
/// emits the same declared events on the adopted run state, its
/// recorder routes them by the same declared retentions, and
/// `GET /resources` on either peer answers the same routed events —
/// the journal tail's `fired` records beside the event-history ring's
/// `shift` records and the latest view's standing `beat`.
#[test]
fn a_tracking_standbys_resources_answer_the_same_routed_events() {
    let (standby, active) = DrivenStandby::start_with(None, emitter_executor);

    // Lockstep from before the first emission: each requested standby
    // scan pulls the active's checkpoint first — the tracked adopt —
    // then scans, so the peers emit and route identical streams scan
    // by scan.
    for _ in 0..4 {
        standby.standby.client.advance(1).unwrap();
        active.client.advance(1).unwrap();
    }
    assert_eq!(standby.standby.client.role().unwrap().role, Role::Standby);

    let events = |client: &MonitorClient| {
        client
            .resources()
            .unwrap()
            .components
            .into_iter()
            .find(|entry| entry.name == "em")
            .expect("the emitter is served")
            .events
    };
    let expected = events(&active.client);
    let named = |name: &str| {
        expected
            .iter()
            .filter(|entry| {
                matches!(
                    &entry.event,
                    JournalEvent::EventEmitted { event } if event.event == name
                )
            })
            .count()
    };
    // Every declared class landed: `fired` journaled per scan, `shift`
    // in the event-history ring, `beat` standing as the newest
    // `Latest` record — each entry's `retention` marking its store.
    assert_eq!(named("fired"), 4);
    assert_eq!(named("shift"), 4);
    assert_eq!(named("beat"), 1);
    for retention in [
        EventRetention::Journal,
        EventRetention::History,
        EventRetention::Latest,
    ] {
        assert!(expected.iter().any(|entry| entry.retention == retention));
    }
    // The parity itself: the standby's `events` is the same record —
    // same seqs, ticks, payloads, and retention marks.
    assert_eq!(events(&standby.standby.client), expected);
}
