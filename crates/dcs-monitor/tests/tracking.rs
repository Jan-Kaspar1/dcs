//! The driven standby's per-scan tracking cycle: `POST /scan` runs
//! `Peer::track_once` inside the request's boundary — one checkpoint
//! pull while the peer does not own the field, heartbeat-miss
//! accounting, and the promote-on-budget sequence — and drains the
//! queues it fills into the recorder's journal, exactly as the paced
//! monitored loop's `track_cycle` does.

use dcs_core::{
    Command, CommandAvailability, CommandDecl, CommandOutcome, ComponentDescriptor, Direction,
    Divergence, EmittedEvent, EventDecl, EventField, EventFieldKind, EventRetention, EventValue,
    IoDriver, IoError, JournalEvent, PointId, Quality, QualityReason, Role, Sample, StandbySync,
    StateMap, SwitchError, Tick, Value, ValueKind,
};
use dcs_model::{PlantModel, SignalIndex};
use dcs_monitor::{CheckpointPuller, Driven, Monitor, MonitorClient};
use dcs_runtime::{
    Checkpoint, Component, ComponentIo, ComponentIoExt, Executor, IoRequirement, Peer, PointMap,
    PointSpec, StepError, mint_generation,
};
use std::collections::{BTreeMap, HashMap};
use std::io::{Read, Write};
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr, TcpListener};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
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

/// A driver front that fences field writes on demand — the in-process
/// stand-in for the shared field's arbitration preempting this
/// attachment's claim: once `preempt` runs, every write returns
/// `IoError::Fenced`, exactly what a foreign `claim_writer` makes the
/// superseded owner's next field-owning scan observe.
struct FencingDriver {
    inner: StubDriver,
    preempted: AtomicBool,
}

impl FencingDriver {
    fn start() -> &'static Self {
        Box::leak(Box::new(Self {
            inner: StubDriver::new(&[
                (PointId(10), Value::Float(3.0)),
                (PointId(20), Value::Float(0.0)),
                (PointId(30), Value::Float(0.0)),
            ]),
            preempted: AtomicBool::new(false),
        }))
    }

    /// The foreign `claim_writer` preemption: every write fences from
    /// here on — the claim a dead token keeps standing.
    fn preempt(&self) {
        self.preempted.store(true, Ordering::Relaxed);
    }
}

impl IoDriver for FencingDriver {
    fn read(&self, point: PointId) -> Result<Sample, IoError> {
        self.inner.read(point)
    }

    fn write(&self, point: PointId, value: Value) -> Result<(), IoError> {
        if self.preempted.load(Ordering::Relaxed) {
            return Err(IoError::Fenced(point));
        }
        self.inner.write(point, value)
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

/// The realign finding's executor: the fixture's surface with a
/// `stale_after_ticks` budget on the field `In` — the declared
/// freshness bound the tracker's own scans outrun while the frozen
/// checkpoint stream holds the field sample unchanged.
fn stale_executor(driver: &'static StubDriver) -> Executor<'static> {
    let map = PointMap::new()
        .with_spec(
            PointId(10),
            PointSpec {
                direction: Direction::In,
                kind: ValueKind::Float,
                internal: None,
                writable: true,
                stale_after_ticks: Some(STALE_BUDGET),
                journaled: false,
            },
        )
        .with_point(PointId(20), Direction::Out, ValueKind::Float)
        .with_point(PointId(30), Direction::Out, ValueKind::Float);
    Executor::new(driver, map, vec![Box::new(Scale)]).unwrap()
}

/// Point 10's declared freshness budget on the realign rig — long
/// enough to span the converging scans, short enough that a handful of
/// degraded scans crosses it.
const STALE_BUDGET: u64 = 4;

/// The `executor` fixture over a fencing front — the launched active's
/// build in the involuntary-demotion reproduction.
fn fenced_executor(driver: &'static FencingDriver) -> Executor<'static> {
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

/// The forged-internal-state rig's executor: the fixture's surface
/// plus one image-carried writable `In` — the operator-held value a
/// forged standby-shaped checkpoint plants, and the state the field
/// owner's own audit proves or convicts.
fn internal_executor(driver: &'static StubDriver) -> Executor<'static> {
    let map = PointMap::new()
        .with_writable_point(PointId(10), Direction::In, ValueKind::Float)
        .with_point(PointId(20), Direction::Out, ValueKind::Float)
        .with_point(PointId(30), Direction::Out, ValueKind::Float)
        .with_writable_internal(
            PointId(40),
            Direction::In,
            ValueKind::Bool,
            Value::Bool(false),
        );
    Executor::new(driver, map, vec![Box::new(Scale)]).unwrap()
}

/// A component declaring a `KindDeclared` command whose standing
/// predicate refuses at a checkpointed count — the served-verdict
/// parity rig: `advance` counts toward the declared `LIMIT`, where
/// probe and dispatch share the named refusal, while `reset` —
/// `Always`-available — zeroes it. The checkpointed `count` carries
/// the verdict across the tracking pull, so a standby's scans
/// re-derive the active's published answer from the adopted state.
struct Gate {
    count: i64,
}

impl Gate {
    /// The kind's declared `advance` ceiling.
    const LIMIT: i64 = 2;

    /// The standing predicate `command_refusal` publishes and
    /// `invoke_command` enforces — one code path, as the contract
    /// requires of probe and dispatch.
    fn refusal(&self) -> Option<String> {
        (self.count >= Self::LIMIT).then(|| "the gate is at its end; reset reopens it".to_string())
    }
}

impl Component for Gate {
    fn name(&self) -> &str {
        "gate"
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        Vec::new()
    }

    fn step(&mut self, _io: &dyn ComponentIo, _tick: Tick) -> Result<(), StepError> {
        Ok(())
    }

    fn describe(&self) -> ComponentDescriptor {
        ComponentDescriptor {
            name: "gate".to_string(),
            kind: "gate".to_string(),
            label: "gate".to_string(),
            ports: Vec::new(),
            parameters: Vec::new(),
            commands: vec![
                CommandDecl {
                    name: "advance".to_string(),
                    request: Vec::new(),
                    availability: CommandAvailability::KindDeclared,
                },
                CommandDecl {
                    name: "reset".to_string(),
                    request: Vec::new(),
                    availability: CommandAvailability::Always,
                },
            ],
            events: Vec::new(),
        }
    }

    fn command_refusal(&self, _command: &str) -> Option<String> {
        self.refusal()
    }

    fn invoke_command(
        &mut self,
        command: &str,
        _arguments: &BTreeMap<String, Value>,
    ) -> Result<(), String> {
        match command {
            "advance" => {
                if let Some(reason) = self.refusal() {
                    return Err(reason);
                }
                self.count += 1;
                Ok(())
            }
            "reset" => {
                self.count = 0;
                Ok(())
            }
            _ => unreachable!("submission validates the declared command name"),
        }
    }

    fn capture_state(&self) -> StateMap {
        let mut state = StateMap::new();
        state.insert("count", Value::Int(self.count));
        state
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), dcs_core::StateError> {
        state.ensure_known_fields("gate", &["count"])?;
        self.count = state.require_i64("gate", "count")?;
        Ok(())
    }
}

/// The verdict-parity rig's executor: one `Gate` and no I/O surface —
/// the tracked `count` exercises the served `KindDeclared` verdicts.
fn gate_executor(driver: &'static StubDriver) -> Executor<'static> {
    Executor::new(driver, PointMap::new(), vec![Box::new(Gate { count: 0 })]).unwrap()
}

/// The dialable form of a bound monitor address: in this in-process
/// rig a wildcard bind is reached through loopback — a deployment's
/// answer to the same `local_addr` is the peer's `host:port` name.
fn dialable(bound: SocketAddr) -> SocketAddr {
    SocketAddr::new(
        match bound.ip() {
            IpAddr::V4(ip) if ip.is_unspecified() => IpAddr::V4(Ipv4Addr::LOCALHOST),
            IpAddr::V6(ip) if ip.is_unspecified() => IpAddr::V6(Ipv6Addr::LOCALHOST),
            ip => ip,
        },
        bound.port(),
    )
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
        let client = MonitorClient::new(dialable(monitor.local_addr()));
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
        Self::start_binding(failover, build, "127.0.0.1:0", "127.0.0.1:0", None)
    }

    /// `active_bind` and `standby_bind` are the monitors' listen
    /// addresses — `0.0.0.0:0` reproduces the wildcard bind every
    /// container deployment carries, whose `local_addr` is the
    /// unroutable announce the follow-peer contract must resolve.
    /// `key` installs the pair's shared tracking secret on both
    /// monitors — the `--pair-token` deployment's shape.
    fn start_binding(
        failover: Option<u32>,
        build: fn(&'static StubDriver) -> Executor<'static>,
        active_bind: &str,
        standby_bind: &str,
        key: Option<u64>,
    ) -> (Self, Serving) {
        let active_driver: &'static StubDriver = Box::leak(Box::new(StubDriver::new(&[
            (PointId(10), Value::Float(3.0)),
            (PointId(20), Value::Float(0.0)),
            (PointId(30), Value::Float(0.0)),
        ])));
        let active = Serving::start(keyed(
            Monitor::bind_peer(
                active_bind,
                Peer::active(build(active_driver), None),
                signal_index(),
            )
            .unwrap(),
            key,
        ));
        // The standby's configured track is the deployment's
        // `--standby <host:port>` — the dialable name, never the
        // active's wildcard bind address.
        let active_addr = dialable(active.monitor.local_addr());

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
            keyed(
                Monitor::bind_peer(standby_bind, peer, signal_index()).unwrap(),
                key,
            )
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

    /// Both peers launched with the pair's tracking secret — the
    /// `--pair-token` deployment: `?prove=` answers sign and the
    /// announced-source pulls verify.
    fn start_keyed(key: u64) -> (Self, Serving) {
        Self::start_binding(None, executor, "127.0.0.1:0", "127.0.0.1:0", Some(key))
    }
}

/// `key` installs the pair's shared tracking secret on `monitor` —
/// the `--pair-token` deployment's shape; `None` leaves the unkeyed
/// contract.
fn keyed(monitor: Monitor<'static>, key: Option<u64>) -> Monitor<'static> {
    match key {
        Some(key) => monitor.with_pair_key(key),
        None => monitor,
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
/// divergence check on the driven path, and both transitions drain into
/// the journal at the compared tick — the detection with its mismatches,
/// the resync's resolution with the compared-point evidence.
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

    // The field carries what the standby stages again: the next pull's
    // same-tick compare is the resync, and the journal records the
    // resolution — the diverged → tracking transition that reopens the
    // promote gate — at the compared tick with its compared points.
    standby
        .standby_driver
        .write(PointId(20), Value::Float(6.0))
        .unwrap();
    active.client.advance(1).unwrap();
    standby.standby.client.advance(1).unwrap();

    let report = standby.standby.client.role().unwrap();
    assert_eq!(
        report.sync,
        Some(StandbySync::Tracking { aligned: Tick(5) }),
        "the clean compare resyncs: {report:?}"
    );
    let journal = standby.standby.client.journal(0).unwrap();
    assert!(
        journal.iter().any(|entry| matches!(
            &entry.event,
            JournalEvent::DivergenceResolved { compared }
                if *compared
                    == vec![Divergence {
                        point: PointId(20),
                        staged: Value::Float(6.0),
                        field: Value::Float(6.0),
                    }]
                && entry.tick == Tick(5)
        )),
        "the driven cycle journaled the resolution at the compared tick: {journal:?}"
    );
}

/// The QA finding `diverged-clears-without-valid-field-comparison` on
/// the driven path: a diverged standby whose tracking source stalls
/// keeps pulling the same stale checkpoint — an apply that matches no
/// staged image's tick runs zero field reads and must leave the verdict
/// standing, `POST /promote` staying refused — until a fresh same-tick
/// comparison reads the field and matches, clearing the verdict and
/// journaling the named `divergence_resolved` event.
#[test]
fn driven_stale_apply_leaves_the_standby_diverged() {
    let (standby, active) = DrivenStandby::start(None);

    active.client.advance(3).unwrap();
    // Converge on ckpt@3; the scan stages the tick-4 `Out` image.
    standby.standby.client.advance(1).unwrap();

    // Diverge: the private field's Out point no longer carries what the
    // staged image describes — the next same-tick compare reports it.
    standby
        .standby_driver
        .write(PointId(20), Value::Float(99.0))
        .unwrap();
    active.client.advance(1).unwrap();
    standby.standby.client.advance(1).unwrap();
    let report = standby.standby.client.role().unwrap();
    let Some(diverged @ StandbySync::Diverged { .. }) = &report.sync else {
        panic!("the same-tick compare must report diverged: {report:?}");
    };
    let diverged = diverged.clone();

    // The frozen source keeps serving its tick-4 checkpoint: the
    // re-apply matches no staged image's tick and performs zero field
    // reads — the verdict stands and promotion stays refused.
    standby.standby.client.advance(1).unwrap();
    let report = standby.standby.client.role().unwrap();
    assert_eq!(report.sync.as_ref(), Some(&diverged));
    let (status, body) = standby
        .standby
        .client
        .request("POST", "/promote", None)
        .unwrap();
    assert_eq!(status, 409, "{body}");
    assert_eq!(
        serde_json::from_str::<SwitchError>(&body).unwrap(),
        SwitchError::NotConverged {
            sync: diverged.clone()
        }
    );

    // A fresh same-tick comparison that read the field and matched is
    // the resync: the verdict clears, the journal carries the named
    // resolution at the compared tick, and the promote gate reopens.
    active.client.advance(1).unwrap();
    standby.standby.client.advance(1).unwrap();
    let report = standby.standby.client.role().unwrap();
    assert_eq!(
        report.sync,
        Some(StandbySync::Tracking { aligned: Tick(5) })
    );
    let journal = standby.standby.client.journal(0).unwrap();
    assert!(
        journal.iter().any(|entry| matches!(
            &entry.event,
            JournalEvent::DivergenceResolved { compared }
                if compared.len() == 1
                    && compared[0].point == PointId(20)
                    && compared[0].staged == compared[0].field
                    && entry.tick == Tick(6)
        )),
        "the clear must journal as divergence_resolved at the compared tick: {journal:?}"
    );
    let promoted = standby.standby.client.promote().unwrap();
    assert_eq!(promoted.role, Role::Promoting);
}

/// The QA finding `checkpoint-peer-announce-overwrites-follow-source`:
/// `GET /checkpoint?peer=` is the tracking standby announcing *its own*
/// monitor address — a claim the serving monitor accepts only when it
/// names the pulling connection's own source address. A client
/// announcing an address that is not its own — the reproduction's
/// bogus `10.255.255.1:9999` — is ignored while the checkpoint still
/// answers `200`: the read endpoint cannot plant or overwrite the
/// follow-peer tracking source, a field owner whose only "announce"
/// was spoofed still refuses `POST /demote` with `no_tracking_source`,
/// and a real tracking peer's announce keeps selecting the demotion's
/// source.
#[test]
fn a_spoofed_peer_announce_cannot_redirect_the_demotion_tracking_source() {
    const KEY: u64 = 0x517c_c1b7_2722_0a95;
    let (standby, active) = DrivenStandby::start_keyed(KEY);
    let bogus: SocketAddr = "10.255.255.1:9999".parse().unwrap();

    // The reproduction's first half: ahead of any real announce the
    // bogus foreign address does not land — the checkpoint answers,
    // the announced tracking source stays unset.
    active.client.checkpoint_announcing(bogus).unwrap();
    assert_eq!(active.monitor.tracking_source(), None);

    // A real tracking peer's announce lands: the driven standby's
    // per-scan pull names its own bound address — the pulling
    // connection's own source — which the serving monitor records.
    standby.standby.client.advance(1).unwrap();
    let standby_addr = standby.standby.monitor.local_addr();
    assert_eq!(active.monitor.tracking_source(), Some(standby_addr));

    // The spoofed announce cannot overwrite it either.
    active.client.checkpoint_announcing(bogus).unwrap();
    assert_eq!(active.monitor.tracking_source(), Some(standby_addr));

    // Demotion follows the recorded real source: the demoted peer's
    // tracking pull converges against the standby that announced
    // itself — a pull toward the planted address would have reported
    // `degraded` naming it. The successor still owns nothing — the
    // demote→promote gap is an unowned line — so the honest interim
    // verdict is `orphaned` until its promotion claims the field.
    assert_eq!(active.client.demote().unwrap().role, Role::Demoting);
    active.client.advance(1).unwrap();
    let report = active.client.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert!(
        matches!(report.sync, Some(StandbySync::Orphaned { .. })),
        "the demoted peer tracks its announced successor, not the \
         spoofed address: {report:?}"
    );
    assert_eq!(
        standby.standby.client.promote().unwrap().role,
        Role::Promoting
    );
    standby.standby.client.advance(1).unwrap();
    active.client.advance(1).unwrap();
    assert!(
        matches!(
            active.client.role().unwrap().sync,
            Some(StandbySync::Tracking { .. })
        ),
        "the demoted peer reconverges once the successor owns the field"
    );

    // A field owner whose only "announce" was the spoofed one keeps
    // refusing demotion — `no_tracking_source` rather than converging
    // against the planted address.
    let lonely = lonely_owner(Some(KEY));
    lonely.client.checkpoint_announcing(bogus).unwrap();
    let error = lonely.client.demote().unwrap_err();
    assert!(
        error.to_string().contains("no_tracking_source"),
        "a spoofed announce must not arm the demotion tracking source: {error}"
    );
    assert_eq!(lonely.monitor.tracking_source(), None);

    // The wildcard announce a `0.0.0.0`-bound puller sends — "my port
    // on every interface" — resolves to the source the connection
    // proves rather than dropping the follow-peer contract.
    lonely
        .client
        .checkpoint_announcing("0.0.0.0:12345".parse().unwrap())
        .unwrap();
    assert_eq!(
        lonely.monitor.tracking_source(),
        Some("127.0.0.1:12345".parse().unwrap())
    );
    lonely.stop();
}

/// The QA finding `demoted-peer-adopts-standby-line-into-promotable-
/// stale-island`, demote half: with two standbys announcing, an
/// announcer that merely tracks this run's line — a sibling standby
/// replaying the demoted peer's own line back to it — can never win
/// the demotion over the candidate that serves the line as its field
/// owner, even when the sibling announced last. The pair is keyed —
/// only a key-attested pull may adopt a field-owning document at all —
/// so the successor's strictly-ahead signed owner document verifies
/// where an unproven one would refuse as replayable. The demotion
/// adopts the owner and the demoted peer reconverges `tracking`,
/// never `orphaned`.
#[test]
fn an_announced_standby_line_loses_the_demote_hint_to_the_field_owner() {
    const KEY: u64 = 0x85a3_08d3_1319_8a2e;
    let (b, active) = DrivenStandby::start_keyed(KEY);
    // A second driven standby on the same active — the reproduction's
    // sibling — wired the same way `b` is, pair key included so its
    // checkpoints sign the verify pull's nonce.
    let c_driver: &'static StubDriver = Box::leak(Box::new(StubDriver::new(&[
        (PointId(10), Value::Float(3.0)),
        (PointId(20), Value::Float(0.0)),
        (PointId(30), Value::Float(0.0)),
    ])));
    let c = Serving::start(
        keyed(
            Monitor::bind_peer(
                "127.0.0.1:0",
                Peer::standby(executor(c_driver), None),
                signal_index(),
            )
            .unwrap(),
            Some(KEY),
        )
        .driven(Driven {
            track: Some(b.active_addr),
            after_scan: None,
        }),
    );

    // Both standbys track and announce; the sibling announces last,
    // so under the old last-announcer-wins slot it is the one the
    // demotion would have adopted.
    active.client.advance(3).unwrap();
    b.standby.client.advance(1).unwrap();
    c.client.advance(1).unwrap();

    // The other standby takes the field — the line's real owner,
    // strictly ahead of the active's stalled tick — and the sibling
    // announces again, keeping the newest hint the standby-line one.
    assert_eq!(c.client.promote().unwrap().role, Role::Promoting);
    c.client.advance(1).unwrap();
    b.standby.client.advance(1).unwrap();

    // Demotion verifies every announcer and adopts the owner: the
    // sibling's standby-line checkpoint continues the run's line but
    // claims no field, so it can only ever be the provisional
    // fallback — never the verified adoption.
    assert_eq!(active.client.demote().unwrap().role, Role::Demoting);
    assert_eq!(
        active.monitor.tracking_source(),
        Some(dialable(c.monitor.local_addr())),
        "the demotion adopts the field owner, not the standby-line sibling"
    );

    // The demoted peer reconverges on the owner it adopted — an
    // owning source's checkpoint is a tracking verdict, not an
    // orphan report.
    active.client.advance(1).unwrap();
    let report = active.client.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert!(
        matches!(report.sync, Some(StandbySync::Tracking { .. })),
        "the demoted peer tracks the adopted owner: {report:?}"
    );
}

/// The QA finding `follow-peer-announces-unroutable-bind-address`: a
/// tracking standby whose monitor binds the wildcard — every container
/// deployment's `--listen 0.0.0.0:<port>` — announces that bind
/// address through its `GET /checkpoint?peer=` pulls, and the serving
/// monitor resolves it to the pull connection's proven source rather
/// than recording the unroutable wildcard. Demoting the field owner
/// then follows the resolved address: the demoted peer's checkpoint
/// pull targets a routable address — the pull's source IP, loopback in
/// this in-process rig, the peer's container IP on the shipped rig —
/// and reconverges, staying promotable instead of looping back onto
/// itself forever.
#[test]
fn a_wildcard_bound_peers_announce_tracks_a_routable_source() {
    const KEY: u64 = 0x9e37_79b9_7f4a_7c15;
    let (standby, active) =
        DrivenStandby::start_binding(None, executor, "127.0.0.1:0", "0.0.0.0:0", Some(KEY));
    let standby_bound = standby.standby.monitor.local_addr();
    assert!(
        standby_bound.ip().is_unspecified(),
        "the standby binds the wildcard like the container deployment: {standby_bound}"
    );

    // The tracking pull announces `local_addr` — `0.0.0.0:<port>`, the
    // reproduction's unroutable address — and the serving monitor
    // resolves the wildcard to the pull's proven source: the recorded
    // follow-peer source is the connection's IP, never the wildcard.
    standby.standby.client.advance(1).unwrap();
    let resolved = active.monitor.tracking_source();
    assert_eq!(
        resolved,
        Some(SocketAddr::new(
            IpAddr::V4(Ipv4Addr::LOCALHOST),
            standby_bound.port()
        )),
        "the announced wildcard resolves to the connection's source"
    );

    // Demotion follows the resolved source: the pull reaches the
    // announcing peer — not the demoted peer's own loopback where
    // nothing listens on that port — so the demoted standby converges
    // and stays promotable. The resolved successor owns nothing until
    // promoted, so the pull's honest verdict is `orphaned` — which
    // promotes on the same convergence `tracking` proves.
    assert_eq!(active.client.demote().unwrap().role, Role::Demoting);
    active.client.advance(1).unwrap();
    let report = active.client.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert!(
        matches!(report.sync, Some(StandbySync::Orphaned { .. })),
        "the demoted peer reconverges on the resolved source: {report:?}"
    );
    assert_eq!(active.client.promote().unwrap().role, Role::Promoting);
}

/// The QA finding `checkpoint-announced-peer-unroutable-wildcard`'s
/// full reproduction: *both* peers' monitors bound on the wildcard the
/// documented container deployment's `--listen 0.0.0.0:<port>`
/// produces, then the documented demote-then-promote switchover order.
/// The demoted launched active's recorded tracking source is the
/// standby's announced address resolved through its pull connection's
/// proven source — never the unroutable `0.0.0.0` the pull announced —
/// so the demoted peer pulls its successor and reconverges to
/// `Tracking`. On the reported build this stranded `degraded` on
/// `fetch from 0.0.0.0:<port>` forever, and the unconditional per-pull
/// overwrite re-poisoned a manually corrected source within one pull
/// cycle — only a process restart recovered, at the price of claim
/// churn. Now each pull re-derives the recorded source from the
/// connection itself, so the overwrite cannot drift it back.
#[test]
fn a_wildcard_bound_pair_reconverges_the_demoted_peer() {
    const KEY: u64 = 0x6a09_e667_f3bc_c909;
    let (standby, active) =
        DrivenStandby::start_binding(None, executor, "0.0.0.0:0", "0.0.0.0:0", Some(KEY));
    assert!(
        active.monitor.local_addr().ip().is_unspecified()
            && standby.standby.monitor.local_addr().ip().is_unspecified(),
        "both peers bind the wildcard like the container deployment"
    );

    // Converge the standby: its tracking pull announces
    // `0.0.0.0:<port>` — the reproduction's unroutable address — and
    // the active records the connection-proven resolution.
    active.client.advance(3).unwrap();
    standby.standby.client.advance(1).unwrap();
    assert!(matches!(
        standby.standby.client.role().unwrap().sync,
        Some(StandbySync::Tracking { .. })
    ));
    assert_eq!(
        active.monitor.tracking_source(),
        Some(SocketAddr::new(
            IpAddr::V4(Ipv4Addr::LOCALHOST),
            standby.standby.monitor.local_addr().port()
        )),
        "the recorded source derives from the pull connection's remote \
         address, never the announced wildcard"
    );

    // The documented switchover order: demote the field owner first,
    // then promote the standby.
    assert_eq!(active.client.demote().unwrap().role, Role::Demoting);
    assert_eq!(
        standby.standby.client.promote().unwrap().role,
        Role::Promoting
    );
    standby.standby.client.advance(1).unwrap();
    assert_eq!(standby.standby.client.role().unwrap().role, Role::Active);

    // The demoted peer's next scan cycle pulls the resolved source and
    // reconverges — where the reported build looped on `fetch from
    // 0.0.0.0:<port>: Connection refused` forever.
    active.client.advance(1).unwrap();
    let report = active.client.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert!(
        matches!(report.sync, Some(StandbySync::Tracking { .. })),
        "the demoted peer reconverges on the resolved source: {report:?}"
    );

    // Every further pull re-derives the recorded source from the
    // connection — the overwrite cannot re-poison it — and the
    // demoted peer stays promotable, so fail-back works without a
    // restart.
    active.client.advance(1).unwrap();
    let recorded = active.monitor.tracking_source().unwrap();
    assert!(
        !recorded.ip().is_unspecified() && recorded.port() != 0,
        "the per-pull overwrite re-derives a dialable source: {recorded}"
    );
    assert_eq!(active.client.promote().unwrap().role, Role::Promoting);
}

/// A closed loopback port — nothing listens there, so a tracking pull
/// aimed at it is refused: the `closed_port` convention the
/// reference-plant peer-announce leg uses for its crafted announce.
fn closed_port() -> SocketAddr {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    drop(listener);
    addr
}

/// A hostile checkpoint server on the test network — the QA
/// reproduction's interposer: it answers every checkpoint pull with
/// whatever document it currently serves, so a test can adopt the
/// endpoint on one document and then have later pulls answer with
/// another — the reproduction's flip after the one-shot verify.
/// `serve_signed` gives the endpoint the pair's key: its answers then
/// carry the `line_proof` a keyed pull's `?prove=` nonce demands —
/// the strongest interposer shape, where every pulled document is
/// genuinely signed and only its content can convict it. Runs on its
/// own thread until dropped.
struct Hostile {
    addr: SocketAddr,
    body: Arc<Mutex<Checkpoint>>,
    hits: Arc<AtomicUsize>,
    stop: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
}

impl Hostile {
    fn serve(forged: &Checkpoint) -> Self {
        Self::listen(forged, None)
    }

    /// The key-holding variant: every answer carries the keyed
    /// `line_proof` binding the pull's `?prove=` nonce to the served
    /// document — a signed forgery the proof alone cannot refuse.
    fn serve_signed(forged: &Checkpoint, key: u64) -> Self {
        Self::listen(forged, Some(key))
    }

    fn listen(forged: &Checkpoint, key: Option<u64>) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let addr = listener.local_addr().unwrap();
        let body = Arc::new(Mutex::new(forged.clone()));
        let served = Arc::clone(&body);
        let hits = Arc::new(AtomicUsize::new(0));
        let counting = Arc::clone(&hits);
        let stop = Arc::new(AtomicBool::new(false));
        let stopping = Arc::clone(&stop);
        let thread = thread::spawn(move || {
            while !stopping.load(Ordering::Relaxed) {
                match listener.accept() {
                    Ok((mut stream, _)) => {
                        counting.fetch_add(1, Ordering::Relaxed);
                        let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
                        let mut seen = Vec::new();
                        let mut buf = [0u8; 4096];
                        loop {
                            match stream.read(&mut buf) {
                                Ok(0) => break,
                                Ok(n) => {
                                    seen.extend_from_slice(&buf[..n]);
                                    if seen.windows(4).any(|window| window == b"\r\n\r\n") {
                                        break;
                                    }
                                    if seen.len() > 65536 {
                                        break;
                                    }
                                }
                                Err(_) => break,
                            }
                        }
                        let mut document = served.lock().unwrap().clone();
                        if let (Some(key), Some(nonce)) = (key, request_prove(&seen)) {
                            document.line_proof =
                                Some(dcs_monitor::line_proof(key, nonce, &document));
                        }
                        let body = serde_json::to_string(&document).unwrap();
                        let response = format!(
                            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\
                             Content-Length: {}\r\nConnection: close\r\n\r\n{}",
                            body.len(),
                            body
                        );
                        let _ = stream.write_all(response.as_bytes());
                        let _ = stream.flush();
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(5));
                    }
                    Err(_) => break,
                }
            }
        });
        Self {
            addr,
            body,
            hits,
            stop,
            thread: Some(thread),
        }
    }

    /// How many connections the endpoint has served — the count an
    /// unproven announcer must never see grow past its bounded verify
    /// probes.
    fn hits(&self) -> usize {
        self.hits.load(Ordering::Relaxed)
    }

    /// Swaps the document later pulls answer with — the reproduction's
    /// post-adoption flip, where the verified document and the tracked
    /// ones are not the same.
    fn set_body(&self, forged: &Checkpoint) {
        *self.body.lock().unwrap() = forged.clone();
    }
}

/// The `prove` nonce a checkpoint request's query carries — the
/// attestation demand a signing hostile answers under its key.
fn request_prove(request: &[u8]) -> Option<u64> {
    let line = std::str::from_utf8(request).ok()?.lines().next()?;
    let query = line.split_whitespace().nth(1)?.split_once('?')?.1;
    query.split('&').find_map(|pair| {
        pair.split_once('=')
            .filter(|(key, _)| *key == "prove")
            .and_then(|(_, value)| value.parse().ok())
    })
}

impl Drop for Hostile {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

/// A transparent TCP relay — the reproduction's interposer in its
/// strongest shape: rather than serving a captured document it proxies
/// every connection to the victim's real monitor, so a keyed verify
/// pull's `?prove=` nonce returns a genuinely *signed* answer — the
/// victim's own field-owning document, exactly the replayable shape
/// the demote-side document checks refuse whatever the proof says.
/// Runs on its own thread until dropped.
struct Relay {
    addr: SocketAddr,
    partitioned: Arc<AtomicBool>,
    stop: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
}

impl Relay {
    fn serve(upstream: SocketAddr) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let addr = listener.local_addr().unwrap();
        let partitioned = Arc::new(AtomicBool::new(false));
        let cutting = Arc::clone(&partitioned);
        let stop = Arc::new(AtomicBool::new(false));
        let stopping = Arc::clone(&stop);
        let thread = thread::spawn(move || {
            while !stopping.load(Ordering::Relaxed) {
                match listener.accept() {
                    Ok((client, _)) => {
                        if cutting.load(Ordering::Relaxed) {
                            drop(client);
                            continue;
                        }
                        if let Ok(server) = std::net::TcpStream::connect(upstream) {
                            thread::spawn(move || pump_relay(client, server));
                        }
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(5));
                    }
                    Err(_) => break,
                }
            }
        });
        Self {
            addr,
            partitioned,
            stop,
            thread: Some(thread),
        }
    }

    /// Cuts or restores the relayed stream — the frozen-source window
    /// the realign finding pauses: accepted connections close
    /// unanswered while cut, so the tracker's pulls fail fast like the
    /// reproduction's paused active.
    fn partition(&self, cut: bool) {
        self.partitioned.store(cut, Ordering::Relaxed);
    }
}

impl Drop for Relay {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

/// One proxied connection: two copy loops, one per direction, each
/// ending by half-closing the other side so the request/response pair
/// completes and the sockets close cleanly.
fn pump_relay(client: std::net::TcpStream, server: std::net::TcpStream) {
    use std::net::Shutdown;
    let Ok(client_reader) = client.try_clone() else {
        return;
    };
    let Ok(server_reader) = server.try_clone() else {
        return;
    };
    let writer = thread::spawn(move || {
        let mut from = client_reader;
        let mut to = server;
        let _ = std::io::copy(&mut from, &mut to);
        let _ = to.shutdown(Shutdown::Write);
    });
    let mut from = server_reader;
    let mut to = client;
    let _ = std::io::copy(&mut from, &mut to);
    let _ = to.shutdown(Shutdown::Write);
    let _ = writer.join();
}

/// The QA finding `checkpoint-peer-hint-fabricates-tracking-source`'s
/// first half: on a lone field owner with no configured source, a
/// same-source `?peer=` announce naming a closed port lands as the
/// hint — the serving side cannot tell the puller's monitor port from
/// any other same-IP port — but the hint is unverified, so it must
/// not arm the demotion: `POST /demote` still refuses
/// `no_tracking_source` and the instance stays the field owner
/// instead of stranding `degraded` on a pull that can never land.
#[test]
fn announced_hint_to_a_dead_port_cannot_unblock_no_tracking_source() {
    const KEY: u64 = 0x517c_c1b7_2722_0a95;
    let lonely = lonely_owner(Some(KEY));
    // No hint yet: the guard refuses.
    let error = lonely.client.demote().unwrap_err();
    assert!(
        error.to_string().contains("no_tracking_source"),
        "a sourceless owner refuses demotion: {error}"
    );

    // The same-source announce lands as the recorded hint — the
    // checkpoint read still answers `200`.
    let dead = closed_port();
    lonely.client.checkpoint_announcing(dead).unwrap();
    assert_eq!(lonely.monitor.tracking_source(), Some(dead));

    // But the dead hint cannot unblock the guard: demotion still
    // refuses, the instance stays the field owner, and no adoption is
    // journaled.
    let error = lonely.client.demote().unwrap_err();
    assert!(
        error.to_string().contains("no_tracking_source"),
        "a dead announced hint must not arm the demotion: {error}"
    );
    assert_eq!(lonely.client.role().unwrap().role, Role::Active);
    assert!(
        lonely
            .client
            .journal(0)
            .unwrap()
            .iter()
            .all(|entry| !matches!(entry.event, JournalEvent::TrackingSourceAdopted { .. })),
        "a refused demotion adopts no tracking source"
    );
}

/// The finding's second half: the same-source announce names a live
/// hostile endpoint serving a forged checkpoint far ahead of the
/// run's tick. The hint lands — like any same-IP port claim — but the
/// keyed demotion verifies it first: the unsigned answer proves
/// nothing about the endpoint, so `POST /demote` refuses
/// `no_tracking_source`, the run keeps its tick, and nothing journals
/// an adoption.
#[test]
fn announced_hint_to_a_hostile_checkpoint_server_cannot_unblock_no_tracking_source() {
    const KEY: u64 = 0x9e37_79b9_7f4a_7c15;
    let lonely = lonely_owner(Some(KEY));
    lonely.client.advance(3).unwrap();
    let tick = lonely.client.role().unwrap().tick;

    // The forged document: the run's own checkpoint with the tick
    // rewritten far ahead — the reproduction's `tick 793 -> 99999`
    // jump — served by the interposer.
    let mut forged = lonely.client.checkpoint().unwrap();
    forged.tick = Tick(99999);
    let hostile = Hostile::serve(&forged);

    lonely.client.checkpoint_announcing(hostile.addr).unwrap();
    assert_eq!(lonely.monitor.tracking_source(), Some(hostile.addr));

    let error = lonely.client.demote().unwrap_err();
    assert!(
        error.to_string().contains("no_tracking_source"),
        "a forged announced stream must not arm the demotion: {error}"
    );
    // The run is undisturbed: still the owner, same tick, and the
    // journal names no adoption — the forgery was never adopted,
    // silently or otherwise.
    let report = lonely.client.role().unwrap();
    assert_eq!(report.role, Role::Active);
    assert_eq!(report.tick, tick);
    assert!(
        lonely
            .client
            .journal(0)
            .unwrap()
            .iter()
            .all(|entry| !matches!(
                entry.event,
                JournalEvent::TrackingSourceAdopted { .. } | JournalEvent::RoleChanged { .. }
            )),
        "a refused demotion journals neither an adoption nor a role change"
    );
}

/// The finding's audit half on the honest path: a field owner with no
/// configured source demotes toward the address its tracking peer
/// announced — the follow-peer contract — and the demotion journals
/// the adopted source naming it, so the run's move onto the announced
/// endpoint is visible. The demoted peer then reconverges on that
/// successor and stays promotable.
#[test]
fn demote_toward_a_verified_announced_source_journals_the_adopted_source() {
    const KEY: u64 = 0x517c_c1b7_2722_0a95;
    let (standby, active) = DrivenStandby::start_keyed(KEY);

    // Converge: the standby's pull announces its own address, which
    // the owner records as its demotion fallback.
    active.client.advance(3).unwrap();
    standby.standby.client.advance(1).unwrap();
    let successor = active.monitor.tracking_source().unwrap();

    assert_eq!(active.client.demote().unwrap().role, Role::Demoting);
    // The adoption is journaled at the demotion boundary, naming the
    // verified source — the audit record of where the run moved.
    assert!(
        active
            .client
            .journal(0)
            .unwrap()
            .iter()
            .any(|entry| matches!(
                entry.event,
                JournalEvent::TrackingSourceAdopted { source } if source == successor
            )),
        "the demotion must journal its adopted tracking source: {:?}",
        active.client.journal(0).unwrap()
    );

    // And the demoted peer reconverges on that successor instead of
    // stranding, staying promotable for fail-back. The adopted source
    // owns nothing until promoted, so the honest verdict while the
    // field stands unclaimed is `orphaned` — promotable on the same
    // convergence `tracking` proves.
    active.client.advance(1).unwrap();
    let report = active.client.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert!(
        matches!(report.sync, Some(StandbySync::Orphaned { .. })),
        "the demoted peer reconverges on the adopted source: {report:?}"
    );
    assert_eq!(active.client.promote().unwrap().role, Role::Promoting);
}

/// The QA finding `announced-verify-standby-shape-forgery` (#872) at
/// the verify boundary: the reproduction's forge is a same-source
/// endpoint serving the victim's own checkpoint with the field claim
/// stripped — the standby's `source_owns_field: false` shape the
/// unkeyed document checks used to accept. An unkeyed run can prove
/// nothing about the endpoint serving that document — the public
/// `/checkpoint` hands the forge every shape — so the demotion must
/// refuse `no_tracking_source` whatever the document claims: nothing
/// is adopted or journaled, the hint never becomes a tracking source,
/// and the run stays the field owner at its own tick.
#[test]
fn an_unkeyed_announced_standby_shaped_document_cannot_arm_the_demotion() {
    let lonely = lonely_owner(None);
    lonely.client.advance(3).unwrap();
    let tick = lonely.client.role().unwrap().tick;

    // The reproduction's forge verbatim: the victim's served document
    // with `source_owns_field` flipped true -> false — the standby
    // shape the own-document refusal never covered — at a tick inside
    // the honest successor skew, carrying a planted output image.
    let mut forged = lonely.client.checkpoint().unwrap();
    forged.source_owns_field = Some(false);
    forged.tick = Tick(forged.tick.0 + 1);
    forged
        .outputs
        .insert(PointId(20), Sample::good(Value::Float(1234.0), forged.tick));
    let hostile = Hostile::serve(&forged);

    // The same-source announce lands — but on an unkeyed run a bare
    // hint is never a tracking source: nothing can prove the endpoint.
    lonely.client.checkpoint_announcing(hostile.addr).unwrap();
    assert_eq!(lonely.monitor.tracking_source(), None);

    let error = lonely.client.demote().unwrap_err();
    assert!(
        error.to_string().contains("no_tracking_source"),
        "an unproven standby-shaped document must not arm the demotion: {error}"
    );
    let report = lonely.client.role().unwrap();
    assert_eq!(report.role, Role::Active);
    assert_eq!(report.tick, tick);
    assert!(
        lonely
            .client
            .journal(0)
            .unwrap()
            .iter()
            .all(|entry| !matches!(
                entry.event,
                JournalEvent::TrackingSourceAdopted { .. } | JournalEvent::RoleChanged { .. }
            )),
        "a refused demotion journals neither an adoption nor a role change"
    );
}

/// The finding's redirect half on the standing tracking path: the
/// source a verified announced demotion adopts is also the one the
/// demoted peer keeps pulling — pinned — so a later `?peer=` rewrite,
/// the same unauthenticated mutation that planted the hint, cannot
/// move the tracking onto an endpoint the demotion never proved.
#[test]
fn a_reannounce_cannot_redirect_the_demoted_peers_tracking() {
    const KEY: u64 = 0x243f_6a88_85a3_08d3;
    let (standby, active) = DrivenStandby::start_keyed(KEY);
    active.client.advance(3).unwrap();
    standby.standby.client.advance(1).unwrap();
    let successor = active.monitor.tracking_source().unwrap();

    assert_eq!(active.client.demote().unwrap().role, Role::Demoting);

    // The same-source re-announce still lands — the read endpoint
    // cannot refuse a dialable self-claim — but the demoted peer's
    // pulls stay pinned to the verified adoption rather than the
    // rewrite's hostile endpoint.
    let mut forged = active.client.checkpoint().unwrap();
    forged.tick = Tick(99999);
    let hostile = Hostile::serve(&forged);
    active.client.checkpoint_announcing(hostile.addr).unwrap();
    assert_eq!(active.monitor.tracking_source(), Some(successor));

    // The demoted peer keeps tracking the adopted successor: it
    // converges on the real stream instead of adopting the hostile
    // port's forged tick domain — the alignment, not just the
    // convergence, proves which endpoint the pulls reached. The pinned
    // successor owns nothing, so the verdict is `orphaned`; the forged
    // stream — cloned from the then-owner's checkpoint — would have
    // read `tracking` at the forged tick.
    standby.standby.client.advance(1).unwrap();
    active.client.advance(1).unwrap();
    let report = active.client.role().unwrap();
    assert!(
        matches!(report.sync, Some(StandbySync::Orphaned { aligned }) if aligned != forged.tick),
        "the demoted peer tracks the pinned adoption, not the rewrite: {report:?}"
    );
    assert!(
        report.tick.0 < forged.tick.0,
        "the run's clock stayed off the forged domain: {report:?}"
    );
    assert_eq!(active.client.promote().unwrap().role, Role::Promoting);
}

/// The QA finding `involuntary-demote-unverified-announced-hint`
/// (#873): an active launched without `--peer` records every `?peer=`
/// announce as an unproven hint — the serving side cannot tell the
/// puller's monitor port from any other same-IP claim. `POST /demote`
/// proves each hint before following it; the involuntary path — a
/// foreign `claim_writer` preempting the field, `field_claim_lost`
/// demoting the owner mid-scan with no demote boundary ever running
/// the verify — must apply the same scrutiny: the demoted peer's
/// first sourceless cycle probes the recorded hints under the demote
/// verify's checks, the dead and foreign endpoints lose to the
/// legitimate successor's own proof inside one bounded pass, and the
/// run adopts, journals, and pulls that one — never the unproven
/// announcer the defect stranded it on.
#[test]
fn an_involuntary_demote_verifies_the_announced_hints_before_tracking() {
    // The announced contract is keyed-only — the reproduction's pair
    // carries the deployment's `--pair-token`; the foreign probe does
    // not hold it, so its answers can never attest.
    const KEY: u64 = 0x243f_6a88_85a3_08d3;
    let fencing = FencingDriver::start();
    let active = Serving::start(
        Monitor::bind_peer(
            "127.0.0.1:0",
            Peer::active(fenced_executor(fencing), None),
            signal_index(),
        )
        .unwrap()
        .with_pair_key(KEY),
    );

    // The legitimate successor: a driven standby tracking the active
    // and announcing its own address on every pull — the `--standby`
    // half of the reproduction's pair.
    let standby_driver: &'static StubDriver = Box::leak(Box::new(StubDriver::new(&[
        (PointId(10), Value::Float(3.0)),
        (PointId(20), Value::Float(0.0)),
        (PointId(30), Value::Float(0.0)),
    ])));
    let standby = Serving::start(
        Monitor::bind_peer(
            "127.0.0.1:0",
            Peer::standby(executor(standby_driver), None),
            signal_index(),
        )
        .unwrap()
        .with_pair_key(KEY)
        .driven(Driven {
            track: Some(dialable(active.monitor.local_addr())),
            after_scan: None,
        }),
    );
    let successor = standby.monitor.local_addr();

    active.client.advance(3).unwrap();
    standby.client.advance(1).unwrap();
    assert!(
        matches!(
            standby.client.role().unwrap().sync,
            Some(StandbySync::Tracking { .. })
        ),
        "the standby converged and announced itself to the active"
    );
    assert_eq!(active.monitor.tracking_source(), Some(successor));

    // The reproduction's interloper: a foreign monitor announcing
    // itself through a routine `?peer=` pull — served here by a
    // hostile endpoint answering a foreign-generation document, the
    // probe the QA run watched the demoted peer strand on — and a
    // dead address recorded the same way. Both land as recorded
    // hints, dead last so it heads the set, and both are unproven.
    let mut foreign = active.client.checkpoint().unwrap();
    foreign.generation = Some(mint_generation());
    let probe = Hostile::serve(&foreign);
    let dead = closed_port();
    active.client.checkpoint_announcing(probe.addr).unwrap();
    active.client.checkpoint_announcing(dead).unwrap();
    assert_eq!(active.monitor.tracking_source(), Some(dead));

    // Preempt the field claim: the superseded owner's next field
    // write fences and the documented `field_claim_lost` path demotes
    // it in place — no `POST /demote` ever runs.
    fencing.preempt();
    active.client.advance(1).unwrap();
    assert_eq!(active.client.role().unwrap().role, Role::Demoting);
    assert!(
        active
            .client
            .journal(0)
            .unwrap()
            .iter()
            .any(|entry| matches!(entry.event, JournalEvent::FieldClaimLost { .. })),
        "the fencing loss must journal on the superseded owner"
    );

    // The demoted peer's first tracking cycle runs the demote verify
    // lazily over the recorded set: the dead hint's pull fails inside
    // its bound and falls back, the foreign stream refuses the
    // continuation checks, and the standby's own document proves the
    // line — so the run adopts the legitimate successor, journals it,
    // and pulls it, reporting the unowned line `orphaned` rather than
    // stranding on the foreign endpoint.
    active.client.advance(1).unwrap();
    let report = active.client.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert!(
        matches!(report.sync, Some(StandbySync::Orphaned { .. })),
        "the demoted peer tracks the announced successor — owning \
         nothing until promoted — not the foreign probe: {report:?}"
    );
    assert_eq!(active.monitor.tracking_source(), Some(successor));
    assert!(
        active
            .client
            .journal(0)
            .unwrap()
            .iter()
            .any(|entry| matches!(
                entry.event,
                JournalEvent::TrackingSourceAdopted { source } if source == successor
            )),
        "the lazy verification journals the adopted source: {:?}",
        active.client.journal(0).unwrap()
    );
    assert!(
        probe.hits() <= 2,
        "the foreign endpoint saw at most the verify probe and the \
         orphan-resolution probe — never a tracking pull: {}",
        probe.hits()
    );

    // And the pair reconverges the way the reproduction needed the
    // operator to force: the successor promotes, the demoted peer's
    // next pull converges `tracking` on it, and fail-back works.
    assert_eq!(standby.client.promote().unwrap().role, Role::Promoting);
    standby.client.advance(1).unwrap();
    active.client.advance(1).unwrap();
    let report = active.client.role().unwrap();
    assert!(
        matches!(report.sync, Some(StandbySync::Tracking { .. })),
        "the demoted peer reconverges on the promoted successor: {report:?}"
    );
    assert_eq!(active.client.promote().unwrap().role, Role::Promoting);
}

/// The refuse leg of the same finding: with only unproven announced
/// hints — here a foreign endpoint is the lone announcer — the
/// involuntarily demoted peer pulls nothing, the same answer
/// `POST /demote` gives an unproven hint. The failed set is
/// remembered rather than re-probed every cycle, so the unproven
/// endpoint sees one bounded verification pass — the demote
/// verify's pull and the owner-resolution probe's — and no
/// tracking pull ever follows it.
#[test]
fn an_involuntary_demote_with_only_unproven_hints_pulls_nothing() {
    // Keyed, like the verified leg — the announced contract resolves
    // only under `--pair-token`; the foreign announcer holds no key.
    const KEY: u64 = 0x6a09_e667_f3bc_c909;
    let fencing = FencingDriver::start();
    let active = Serving::start(
        Monitor::bind_peer(
            "127.0.0.1:0",
            Peer::active(fenced_executor(fencing), None),
            signal_index(),
        )
        .unwrap()
        .with_pair_key(KEY),
    );
    active.client.advance(3).unwrap();

    // The foreign monitor's routine `?peer=` announce — recorded as
    // the only hint, unproven like every announce.
    let mut foreign = active.client.checkpoint().unwrap();
    foreign.generation = Some(mint_generation());
    let probe = Hostile::serve(&foreign);
    active.client.checkpoint_announcing(probe.addr).unwrap();
    assert_eq!(active.monitor.tracking_source(), Some(probe.addr));

    fencing.preempt();
    active.client.advance(1).unwrap();
    assert_eq!(active.client.role().unwrap().role, Role::Demoting);
    assert!(
        active
            .client
            .journal(0)
            .unwrap()
            .iter()
            .any(|entry| matches!(entry.event, JournalEvent::FieldClaimLost { .. })),
        "the fencing loss must journal on the superseded owner"
    );

    // The demoted peer's cycles refuse the hint the way the demote
    // path would: one verification pass — the demote verify's pull
    // and the owner-resolution probe's — proves the stream foreign,
    // nothing adopts, and no tracking pull ever targets it — the run
    // reports standby/unsynchronized instead of stranding degraded on
    // the foreign endpoint.
    active.client.advance(3).unwrap();
    let report = active.client.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert_eq!(report.sync, Some(StandbySync::Unsynchronized));
    assert!(
        active
            .client
            .journal(0)
            .unwrap()
            .iter()
            .all(|entry| !matches!(entry.event, JournalEvent::TrackingSourceAdopted { .. })),
        "an unproven hint must never journal an adoption"
    );
    assert!(
        probe.hits() <= 2,
        "the unproven hint earned one bounded verify pass — the \
         demote-verify pull and the owner-resolution probe — and \
         nothing else: {}",
        probe.hits()
    );
    // The recorded hint stays the recorded answer, but the proven
    // resolution stays empty: no pull ever follows it.
    assert_eq!(active.monitor.tracking_source(), Some(probe.addr));
    assert_eq!(active.monitor.verified_tracking_source(), None);
}

/// A lone field owner fixture for the announced-demotion tests —
/// the reproduction's unconfigured instance: no `--standby`, no
/// `--peer`, and `key` installing the `--pair-token` secret when set.
fn lonely_owner(key: Option<u64>) -> Serving {
    let lonely_driver: &'static StubDriver = Box::leak(Box::new(StubDriver::new(&[
        (PointId(10), Value::Float(3.0)),
        (PointId(20), Value::Float(0.0)),
        (PointId(30), Value::Float(0.0)),
    ])));
    Serving::start(keyed(
        Monitor::bind_peer(
            "127.0.0.1:0",
            Peer::active(executor(lonely_driver), None),
            signal_index(),
        )
        .unwrap(),
        key,
    ))
}

/// The QA finding `demote-verify-replayable-redirects-tracking` (#807):
/// the announced-hint verify proved only that the hinted endpoint
/// serves one currently-valid continuation checkpoint — and
/// `/checkpoint` is public, so an interposer that replays the victim's
/// own document verbatim passed every check, got adopted and pinned,
/// and then served forged state the demoted peer applied. The verify
/// must refuse the document shape this run's own `/checkpoint`
/// answers — a field-owning source not ahead of this run's tick — so
/// a replay can never arm the demotion.
#[test]
fn an_announced_replay_of_the_victims_own_checkpoint_cannot_unblock_no_tracking_source() {
    const KEY: u64 = 0x517c_c1b7_2722_0a95;
    let lonely = lonely_owner(Some(KEY));
    lonely.client.advance(3).unwrap();
    let tick = lonely.client.role().unwrap().tick;

    // The reproduction's interposer: a same-source endpoint serving
    // the victim's own checkpoint document verbatim — every byte the
    // public read returns, unsigned.
    let replayed = lonely.client.checkpoint().unwrap();
    let hostile = Hostile::serve(&replayed);

    // The announce lands — the interposer's port is a same-source
    // claim like any other — and the hint is recorded.
    lonely.client.checkpoint_announcing(hostile.addr).unwrap();
    assert_eq!(lonely.monitor.tracking_source(), Some(hostile.addr));

    // But the replayed document cannot arm the demotion: it carries
    // no keyed `line_proof` for the verify pull's nonce — and even
    // signed it is the shape this run's own `/checkpoint` answers,
    // which proves nothing about the endpoint serving it — the guard
    // refuses, the run
    // stays the field owner at its own tick, and nothing journals an
    // adoption or a role change.
    let error = lonely.client.demote().unwrap_err();
    assert!(
        error.to_string().contains("no_tracking_source"),
        "a replayed own-document must not arm the demotion: {error}"
    );
    let report = lonely.client.role().unwrap();
    assert_eq!(report.role, Role::Active);
    assert_eq!(report.tick, tick);
    assert!(
        lonely
            .client
            .journal(0)
            .unwrap()
            .iter()
            .all(|entry| !matches!(
                entry.event,
                JournalEvent::TrackingSourceAdopted { .. } | JournalEvent::RoleChanged { .. }
            )),
        "a refused demotion journals neither an adoption nor a role change"
    );
}

/// A stale replay is the same refusal: the interposer captured the
/// victim's document earlier and the run has scanned past it — the
/// replayed tick is behind now, still stamped field-owning, still the
/// refused own-document shape.
#[test]
fn an_announced_stale_replay_cannot_unblock_no_tracking_source() {
    const KEY: u64 = 0x6a09_e667_f3bc_c909;
    let lonely = lonely_owner(Some(KEY));

    // The interposer captures the victim's document, then the run
    // moves past it — the replayed tick is stale when the demotion
    // verifies.
    let stale = lonely.client.checkpoint().unwrap();
    lonely.client.advance(3).unwrap();
    let tick = lonely.client.role().unwrap().tick;
    let hostile = Hostile::serve(&stale);
    lonely.client.checkpoint_announcing(hostile.addr).unwrap();

    let error = lonely.client.demote().unwrap_err();
    assert!(
        error.to_string().contains("no_tracking_source"),
        "a stale replayed document must not arm the demotion: {error}"
    );
    let report = lonely.client.role().unwrap();
    assert_eq!(report.role, Role::Active);
    assert_eq!(report.tick, tick);
    assert!(
        lonely
            .client
            .journal(0)
            .unwrap()
            .iter()
            .all(|entry| !matches!(
                entry.event,
                JournalEvent::TrackingSourceAdopted { .. } | JournalEvent::RoleChanged { .. }
            )),
        "a refused demotion journals neither an adoption nor a role change"
    );
}

/// The QA finding `demote-verify-own-document-check-bypassed-by-tick-
/// bump` (#832): the own-document refusal covered only the verbatim
/// and stale replay shapes — `source_owns_field: true` at or behind
/// the run's tick — so the interposer serving the victim's live
/// checkpoint with its tick bumped into the honest skew window took
/// the accepted "successor strictly ahead" shape and armed the
/// demotion. Under the keyed-only announced contract the unsigned
/// bump fails the verify pull's `line_proof` demand outright, and
/// even a signed owner document must run strictly ahead of the run
/// it replaces: the demotion
/// answers `no_tracking_source`, the run keeps its tick, and nothing
/// journals an adoption or a role change.
#[test]
fn an_announced_tick_bumped_replay_cannot_unblock_no_tracking_source() {
    const KEY: u64 = 0x85a3_08d3_1319_8a2e;
    let lonely = lonely_owner(Some(KEY));
    lonely.client.advance(3).unwrap();
    let tick = lonely.client.role().unwrap().tick;

    // The reproduction's interposer: the victim's own live document
    // replayed with its tick bumped +10 — inside the announced-ahead
    // skew the successor shape covers — and forged state planted in
    // it, served from the announced same-source endpoint.
    let mut replayed = lonely.client.checkpoint().unwrap();
    replayed.tick = Tick(replayed.tick.0 + 10);
    replayed
        .internal
        .insert(PointId(10), Sample::good(Value::Float(99.0), replayed.tick));
    let hostile = Hostile::serve(&replayed);

    lonely.client.checkpoint_announcing(hostile.addr).unwrap();
    assert_eq!(lonely.monitor.tracking_source(), Some(hostile.addr));

    let error = lonely.client.demote().unwrap_err();
    assert!(
        error.to_string().contains("no_tracking_source"),
        "a tick-bumped replay must not arm the demotion: {error}"
    );
    let report = lonely.client.role().unwrap();
    assert_eq!(report.role, Role::Active);
    assert_eq!(report.tick, tick);
    assert!(
        lonely
            .client
            .journal(0)
            .unwrap()
            .iter()
            .all(|entry| !matches!(
                entry.event,
                JournalEvent::TrackingSourceAdopted { .. } | JournalEvent::RoleChanged { .. }
            )),
        "a refused demotion journals neither an adoption nor a role change"
    );
}

/// The QA findings `announced-source-verify-adopts-standby-shaped-
/// checkpoint` (#850) and `announced-verify-standby-shape-forgery`
/// (#872): the owner-document refusals cover the replayed
/// `source_owns_field: true` shapes, but the standby document shape —
/// the victim's own checkpoint with the stamp flipped `false` and the
/// tick bumped inside the announced-ahead window — is exactly what a
/// real tracking peer serves, so an unkeyed verify cannot distinguish
/// the shape itself — nor can it prove the endpoint, since the public
/// `/checkpoint` hands the forge every shape. The announced-source
/// contract is therefore keyed-only: on an unkeyed run a bare hint
/// never becomes a tracking source and the demotion refuses whatever
/// the document claims — while a keyed demotion's attested pulls
/// still answer this run's command audit, so a document whose receipt
/// window forks the settled log or whose internal `In` samples plant
/// a value no settled verdict produced is forged rather than a
/// continuation. Across the whole `+1..=32` tick window the unkeyed
/// demotion refuses `no_tracking_source`, the run stays the field
/// owner, and nothing adopts.
#[test]
fn an_announced_standby_shaped_forgery_cannot_unblock_no_tracking_source() {
    let lonely_driver: &'static StubDriver = Box::leak(Box::new(StubDriver::new(&[
        (PointId(10), Value::Float(3.0)),
        (PointId(20), Value::Float(0.0)),
        (PointId(30), Value::Float(0.0)),
    ])));
    let lonely = Serving::start(
        Monitor::bind_peer(
            "127.0.0.1:0",
            Peer::active(internal_executor(lonely_driver), None),
            signal_index(),
        )
        .unwrap(),
    );
    lonely.client.advance(3).unwrap();
    let tick = lonely.client.role().unwrap().tick;

    // The reproduction's interposer: the victim's own captured
    // document, re-stamped `source_owns_field: false` — the shape the
    // unkeyed document checks used to accept — with a planted internal
    // value the run never held and no command produced, served at
    // every tick offset the announced-ahead window covers.
    let captured = lonely.client.checkpoint().unwrap();
    assert_eq!(
        captured
            .internal
            .get(&PointId(40))
            .map(|sample| sample.value),
        Some(Value::Bool(false)),
        "the rig's operator-held value starts unwritten: {captured:?}"
    );
    let hostile = Hostile::serve(&captured);
    lonely.client.checkpoint_announcing(hostile.addr).unwrap();
    // The hint records — but unkeyed it can never resolve: nothing
    // proves the endpoint behind it.
    assert_eq!(lonely.monitor.tracking_source(), None);

    for ahead in 1..=32u64 {
        let mut forged = captured.clone();
        forged.source_owns_field = Some(false);
        forged.tick = Tick(tick.0 + ahead);
        forged
            .internal
            .insert(PointId(40), Sample::good(Value::Bool(true), forged.tick));
        hostile.set_body(&forged);

        let error = lonely.client.demote().unwrap_err();
        assert!(
            error.to_string().contains("no_tracking_source"),
            "tick +{ahead}: a forged standby-shaped document must not arm \
             the demotion: {error}"
        );
        let report = lonely.client.role().unwrap();
        assert_eq!(report.role, Role::Active);
        assert_eq!(report.tick, tick);
    }
    assert!(
        lonely
            .client
            .journal(0)
            .unwrap()
            .iter()
            .all(|entry| !matches!(
                entry.event,
                JournalEvent::TrackingSourceAdopted { .. } | JournalEvent::RoleChanged { .. }
            )),
        "a refused demotion journals neither an adoption nor a role change"
    );
    // And the planted value never landed: the run's own document
    // still carries the value its commands produced.
    let served = lonely.client.checkpoint().unwrap();
    assert_eq!(
        served.internal.get(&PointId(40)).map(|sample| sample.value),
        Some(Value::Bool(false))
    );
}

/// The finding's post-adoption half, in its strongest shape under the
/// keyed contract: a key-holding endpoint — every answer genuinely
/// signed, so the `line_proof` convicts nothing — adopts on a
/// standby-shaped document and then flips. The reproduction's second
/// move: once adopted, the interposer flips `source_owns_field` back
/// to `true` to clear the orphan verdict the `false` stamp would
/// raise, and keeps serving the planted internal value on the
/// standing pulls. The adoption binds the endpoint, not its contents:
/// the demoted peer's own audit still vets every pulled document, so
/// the signed forgery refuses like any rejected checkpoint — the peer
/// reports `degraded` rather than adopting the planted state, and
/// cannot promote onto it.
#[test]
fn an_adopted_announced_source_cannot_land_forged_commanded_state() {
    const KEY: u64 = 0x9e37_79b9_7f4a_7c15;
    let lonely_driver: &'static StubDriver = Box::leak(Box::new(StubDriver::new(&[
        (PointId(10), Value::Float(3.0)),
        (PointId(20), Value::Float(0.0)),
        (PointId(30), Value::Float(0.0)),
    ])));
    let lonely = Serving::start(
        Monitor::bind_peer(
            "127.0.0.1:0",
            Peer::active(internal_executor(lonely_driver), None),
            signal_index(),
        )
        .unwrap()
        .with_pair_key(KEY),
    );
    lonely.client.advance(3).unwrap();

    // The verification document: standby-shaped and otherwise the
    // run's own line, signed under the pair's key — the endpoint
    // adopts on it, the journaled adoption the announced-source
    // contract requires.
    let mut clean = lonely.client.checkpoint().unwrap();
    clean.source_owns_field = Some(false);
    clean.tick = Tick(clean.tick.0 + 1);
    let hostile = Hostile::serve_signed(&clean, KEY);
    lonely.client.checkpoint_announcing(hostile.addr).unwrap();
    assert_eq!(lonely.client.demote().unwrap().role, Role::Demoting);
    assert!(
        lonely
            .client
            .journal(0)
            .unwrap()
            .iter()
            .any(|entry| matches!(
                entry.event,
                JournalEvent::TrackingSourceAdopted { source } if source == hostile.addr
            )),
        "the verify-clean document adopts and journals its source: {:?}",
        lonely.client.journal(0).unwrap()
    );

    // The reproduction's post-verify flip: the tracked documents now
    // carry the planted internal value under an owner stamp — the
    // orphan dodge — while the peer's tracking pulls keep coming.
    let mut forged = clean.clone();
    forged.source_owns_field = Some(true);
    forged.tick = Tick(forged.tick.0 + 1);
    forged
        .internal
        .insert(PointId(40), Sample::good(Value::Bool(true), forged.tick));
    hostile.set_body(&forged);

    // The pull refuses the forged document like any rejected
    // checkpoint: the peer reports the named degraded state, applies
    // nothing of it, and stays un promotable — the converge gate the
    // promotion checks never opened on forged state.
    lonely.client.advance(1).unwrap();
    let report = lonely.client.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert!(
        matches!(report.sync, Some(StandbySync::Degraded { .. })),
        "a forged tracked document degrades the pull: {report:?}"
    );
    let error = lonely.client.promote().unwrap_err();
    assert!(
        error.to_string().contains("not_converged"),
        "a peer that never converged on real state cannot promote: {error}"
    );
    let served = lonely.client.checkpoint().unwrap();
    assert_eq!(
        served.internal.get(&PointId(40)).map(|sample| sample.value),
        Some(Value::Bool(false)),
        "the planted internal value never applied: {served:?}"
    );
    assert!(
        lonely
            .client
            .journal(0)
            .unwrap()
            .iter()
            .all(|entry| !matches!(
                &entry.event,
                JournalEvent::RoleChanged {
                    to: Role::Promoting | Role::Active,
                    ..
                }
            )),
        "no promotion journaled on forged state: {:?}",
        lonely.client.journal(0).unwrap()
    );
}

/// The replay in its strongest shape on a keyed run: the interposer
/// does not serve a *captured* document — it relays every connection
/// to the victim's own monitor live, so even the verify pull's
/// `?prove=` nonce comes back genuinely signed. What the relayed
/// answer cannot change is its content: the victim's checkpoint is a
/// field-owning document not ahead of the run's tick — the replayable
/// shape the document checks refuse whatever the proof says — so the
/// demotion refuses `no_tracking_source`, no adoption journals, and
/// the field owner is undisturbed. A forged same-generation document
/// the interposer could serve afterward never gets the chance:
/// nothing was adopted, so nothing follows it.
#[test]
fn an_announced_relay_of_the_victims_monitor_cannot_arm_a_keyed_demote() {
    const KEY: u64 = 0x517c_c1b7_2722_0a95;
    let lonely = lonely_owner(Some(KEY));
    lonely.client.advance(3).unwrap();
    let tick = lonely.client.role().unwrap().tick;

    // The relaying interposer: every connection — including the
    // verify pull's `?prove=` nonce — is proxied to the victim's real
    // monitor, which signs its own document under the pull's nonce.
    let relay = Relay::serve(lonely.monitor.local_addr());
    lonely.client.checkpoint_announcing(relay.addr).unwrap();
    assert_eq!(lonely.monitor.tracking_source(), Some(relay.addr));

    // The signed answer is the victim's own document — field-owning
    // at the run's own tick — the refused replayable shape: the proof
    // attests content, and the content proves nothing about the
    // endpoint being a successor.
    let error = lonely.client.demote().unwrap_err();
    assert!(
        error.to_string().contains("no_tracking_source"),
        "a signed replay of this run's own document must not arm the \
         demotion: {error}"
    );
    let report = lonely.client.role().unwrap();
    assert_eq!(report.role, Role::Active);
    assert_eq!(report.tick, tick);
    assert!(
        lonely
            .client
            .journal(0)
            .unwrap()
            .iter()
            .all(|entry| !matches!(
                entry.event,
                JournalEvent::TrackingSourceAdopted { .. } | JournalEvent::RoleChanged { .. }
            )),
        "a refused demotion journals neither an adoption nor a role change"
    );
}

/// The keyed half on an unproven endpoint: a `--pair-token` monitor
/// demands the pulled checkpoint carry the keyed `line_proof` bound
/// to the verify pull's fresh nonce — only a peer holding the token
/// produces it — so even a fabricated document crafted to pass every
/// continuation check cannot arm the demotion.
#[test]
fn a_fabricated_announced_checkpoint_cannot_unblock_a_keyed_demote() {
    const KEY: u64 = 0x9e37_79b9_7f4a_7c15;
    let lonely = lonely_owner(Some(KEY));
    lonely.client.advance(3).unwrap();
    let tick = lonely.client.role().unwrap().tick;

    // The fabricated document: the victim's checkpoint rewritten past
    // every document check — claiming no field ownership, one tick
    // ahead — the exact shape the unkeyed verify accepts.
    let mut fabricated = lonely.client.checkpoint().unwrap();
    fabricated.source_owns_field = Some(false);
    fabricated.tick = Tick(fabricated.tick.0 + 1);
    let hostile = Hostile::serve(&fabricated);
    lonely.client.checkpoint_announcing(hostile.addr).unwrap();
    let error = lonely.client.demote().unwrap_err();
    assert!(
        error.to_string().contains("no_tracking_source"),
        "an unproven fabricated document must not arm a keyed demotion: {error}"
    );

    // A fabricated document carrying a guessed proof fails the same
    // way — the proof binds the pull's nonce to the document under
    // the pair's key, which the fabricator does not hold.
    let mut forged_proof = fabricated.clone();
    forged_proof.line_proof = Some(0xdead_beef_cafe);
    let hostile = Hostile::serve(&forged_proof);
    lonely.client.checkpoint_announcing(hostile.addr).unwrap();
    let error = lonely.client.demote().unwrap_err();
    assert!(
        error.to_string().contains("no_tracking_source"),
        "a guessed line proof must not arm a keyed demotion: {error}"
    );

    let report = lonely.client.role().unwrap();
    assert_eq!(report.role, Role::Active);
    assert_eq!(report.tick, tick);
    assert!(
        lonely
            .client
            .journal(0)
            .unwrap()
            .iter()
            .all(|entry| !matches!(
                entry.event,
                JournalEvent::TrackingSourceAdopted { .. } | JournalEvent::RoleChanged { .. }
            )),
        "a refused demotion journals neither an adoption nor a role change"
    );
}

/// The keyed contract's honest path: both peers launched with the
/// pair's token — the announced demotion still verifies, adopts, and
/// journals the real standby's endpoint, and the demoted peer's
/// standing pulls keep proving each checkpoint against it.
#[test]
fn a_keyed_demote_toward_the_real_announced_peer_verifies_and_keeps_proving() {
    const KEY: u64 = 0x517c_c1b7_2722_0a95;
    let (standby, active) = DrivenStandby::start_keyed(KEY);

    // Converge: the standby's pull announces its own address, which
    // the owner records as its demotion fallback.
    active.client.advance(3).unwrap();
    standby.standby.client.advance(1).unwrap();
    let successor = active.monitor.tracking_source().unwrap();

    // The verify pull's `?prove=` nonce is answered by the keyed
    // standby's signed document — the demotion adopts and journals it.
    assert_eq!(active.client.demote().unwrap().role, Role::Demoting);
    assert!(
        active
            .client
            .journal(0)
            .unwrap()
            .iter()
            .any(|entry| matches!(
                entry.event,
                JournalEvent::TrackingSourceAdopted { source } if source == successor
            )),
        "the keyed demotion journals its adopted tracking source: {:?}",
        active.client.journal(0).unwrap()
    );

    // The demoted peer's standing pulls toward the adopted endpoint
    // carry fresh nonces the keyed successor keeps answering — the
    // line reconverges and the peer stays promotable.
    active.client.advance(1).unwrap();
    let report = active.client.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert!(
        matches!(report.sync, Some(StandbySync::Orphaned { .. })),
        "the keyed demoted peer reconverges on the proved source: {report:?}"
    );
    assert_eq!(active.client.promote().unwrap().role, Role::Promoting);
}

/// The keyed contract's promoted-successor half: the announced peer
/// already owns the field when the demotion verifies — the
/// misordered switchover a failover or an operator can produce — so
/// its checkpoint stamps `source_owns_field: true`, the shape an
/// unproven pull must refuse as replayable. The keyed `line_proof`
/// separates the real owner document from the replayed bump: the
/// demotion verifies, adopts, and the demoted peer reconverges on
/// its field-owning successor.
#[test]
fn a_keyed_demote_toward_the_promoted_announced_peer_verifies() {
    const KEY: u64 = 0x6a09_e667_f3bc_c909;
    let (standby, active) = DrivenStandby::start_keyed(KEY);

    // Converge, then promote the standby while the active still owns
    // the field — the announced source's checkpoints now carry the
    // owner stamp a replay endpoint would also serve.
    active.client.advance(3).unwrap();
    standby.standby.client.advance(1).unwrap();
    let successor = active.monitor.tracking_source().unwrap();
    assert_eq!(
        standby.standby.client.promote().unwrap().role,
        Role::Promoting
    );
    standby.standby.client.advance(1).unwrap();
    assert_eq!(standby.standby.client.role().unwrap().role, Role::Active);
    let served = standby.standby.client.checkpoint().unwrap();
    assert_eq!(served.source_owns_field, Some(true));
    assert!(
        served.tick > active.client.role().unwrap().tick,
        "the promoted successor's document must run strictly ahead: {served:?}"
    );

    // The verify pull's `?prove=` nonce is answered by the keyed
    // successor's signed owner document — the one field-owning
    // checkpoint a pull may accept — and the demotion adopts and
    // journals the endpoint.
    assert_eq!(active.client.demote().unwrap().role, Role::Demoting);
    assert!(
        active
            .client
            .journal(0)
            .unwrap()
            .iter()
            .any(|entry| matches!(
                entry.event,
                JournalEvent::TrackingSourceAdopted { source } if source == successor
            )),
        "the keyed demotion journals its adopted tracking source: {:?}",
        active.client.journal(0).unwrap()
    );

    // The demoted peer reconverges on the proved owner — a `tracking`
    // verdict this time, the served run genuinely owning the field.
    active.client.advance(1).unwrap();
    let report = active.client.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert!(
        matches!(report.sync, Some(StandbySync::Tracking { .. })),
        "the keyed demoted peer reconverges on its field-owning successor: {report:?}"
    );
}

/// The standing-pull half at the fetch level: a keyed puller requires
/// every fetched document to carry the keyed `line_proof` bound to
/// the pull's nonce — a replayed or fabricated document fails the
/// fetch like a refused pull, while the real keyed peer's signed
/// answers land.
#[test]
fn a_keyed_pull_refuses_documents_without_the_pairs_line_proof() {
    const KEY: u64 = 0x243f_6a88_85a3_08d3;
    let lonely = lonely_owner(Some(KEY));
    lonely.client.advance(3).unwrap();

    // A fabricated continuation document — this line's generation at
    // a plausible tick, no field claim — without the proof.
    let mut fabricated = lonely.client.checkpoint().unwrap();
    fabricated.source_owns_field = Some(false);
    fabricated.tick = Tick(fabricated.tick.0 + 1);
    let hostile = Hostile::serve(&fabricated);

    let deadline = Instant::now() + Duration::from_secs(5);
    let mut puller = CheckpointPuller::with_pair_proof(hostile.addr, None, KEY);
    loop {
        match puller.poll() {
            Err(detail) if detail.contains("line proof") => break,
            _ if Instant::now() < deadline => thread::sleep(Duration::from_millis(5)),
            other => panic!("an unproven fetch must fail on the line proof: {other:?}"),
        }
    }

    // And the keyed pull accepts the real keyed peer's answers — the
    // monitor signs its served document under the pull's nonce.
    let deadline = Instant::now() + Duration::from_secs(5);
    let mut puller =
        CheckpointPuller::with_pair_proof(dialable(lonely.monitor.local_addr()), None, KEY);
    loop {
        match puller.poll() {
            Ok(checkpoint) => {
                assert!(checkpoint.line_proof.is_some());
                break;
            }
            _ if Instant::now() < deadline => thread::sleep(Duration::from_millis(5)),
            Err(detail) => panic!("the keyed peer's proved fetch must land: {detail}"),
        }
    }
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
    let mut puller = CheckpointPuller::new(dead, None);
    let pacing = Arc::clone(&standby.monitor);
    let stop = Arc::new(AtomicBool::new(false));
    let stopping = Arc::clone(&stop);
    let pacer = thread::spawn(move || {
        while !stopping.load(Ordering::Relaxed) {
            pacing.track_cycle(|| puller.poll());
            pacing.paced_scan();
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

/// The driven-surface half of the dead-source starvation finding
/// (`driven-scan-batch-pins-control-plane`): a `POST /scan` batch
/// whose every per-scan pull waits out the fetch bound on an
/// unreachable tracking source must not starve the other endpoints —
/// each pull runs outside the request-serving lock on the batch's own
/// worker, so `GET /role`, `GET /snapshot`, and `POST /command` answer
/// while the batch is still walking its scans. The batch itself stays
/// slow — its scans each owe the pull — but its slowness no longer
/// reaches the lock or the request queue.
#[test]
fn driven_scan_batch_on_a_dead_source_does_not_starve_the_endpoints() {
    // A listener that accepts but never answers: every pull sits in
    // flight until the dedicated pull bound — a deterministic stall,
    // unlike an unroutable address whose connect may fail fast.
    let silent = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let dead = silent.local_addr().unwrap();

    let standby_driver: &'static StubDriver = Box::leak(Box::new(StubDriver::new(&[
        (PointId(10), Value::Float(3.0)),
        (PointId(20), Value::Float(0.0)),
        (PointId(30), Value::Float(0.0)),
    ])));
    let standby = Serving::start(
        Monitor::bind_peer(
            "127.0.0.1:0",
            Peer::standby(executor(standby_driver), None),
            signal_index(),
        )
        .unwrap()
        .driven(Driven {
            track: Some(dead),
            after_scan: None,
        }),
    );

    // The batch runs on its own connection from a second thread: six
    // stalled pulls at the one-second fetch bound keep it in flight
    // far past the request bound asserted below.
    let batch_addr = standby.monitor.local_addr();
    let batch = thread::spawn(move || MonitorClient::new(batch_addr).advance(6));

    // While the batch walks its stalled pulls, the read and
    // control-plane endpoints answer promptly — on a standby the
    // command refuses with a receipt, which is still an answer.
    for _ in 0..10 {
        let started = Instant::now();
        standby.client.role().unwrap();
        let role_elapsed = started.elapsed();
        let started = Instant::now();
        standby.client.snapshot().unwrap();
        let snapshot_elapsed = started.elapsed();
        let started = Instant::now();
        standby
            .client
            .command(&dcs_core::Command::WriteValue {
                point: PointId(10),
                kind: ValueKind::Float,
                value: Value::Float(1.0),
            })
            .unwrap();
        let command_elapsed = started.elapsed();
        assert!(
            role_elapsed < Duration::from_millis(500)
                && snapshot_elapsed < Duration::from_millis(500)
                && command_elapsed < Duration::from_millis(500),
            "monitor requests serialized behind the scan batch's stalled \
             pulls: /role took {role_elapsed:?}, /snapshot took \
             {snapshot_elapsed:?}, /command took {command_elapsed:?}"
        );
    }
    // The batch still completed its requested scans — the tracking
    // misses are the named degraded state, not a request failure.
    batch.join().unwrap().unwrap();
    let report = standby.client.role().unwrap();
    assert!(
        matches!(&report.sync, Some(StandbySync::Degraded { .. })),
        "the stalled pulls report the degraded heartbeat: {report:?}"
    );
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
            pacing.paced_scan();
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

/// Verdict parity at the served surface: a tracking peer's post-scan
/// probe re-derives the `KindDeclared` verdicts from the adopted
/// state, so `GET /resources` on either peer answers the same
/// `commands` rows — `advance` invocable below the limit, then the
/// kind's named refusal once the receipted submissions complete it,
/// and `reset` `Always`-available throughout.
#[test]
fn a_tracking_standbys_resources_answer_the_same_command_verdicts() {
    let (standby, active) = DrivenStandby::start_with(None, gate_executor);

    // Converge in lockstep — each requested standby scan pulls the
    // active's checkpoint first — so both peers publish the gate's
    // mid-table verdicts: `advance` invocable.
    for _ in 0..2 {
        standby.standby.client.advance(1).unwrap();
        active.client.advance(1).unwrap();
    }
    assert_eq!(standby.standby.client.role().unwrap().role, Role::Standby);
    let commands = |client: &MonitorClient| {
        client
            .resources()
            .unwrap()
            .components
            .into_iter()
            .find(|entry| entry.name == "gate")
            .expect("the gate is served")
            .commands
    };
    let expected = commands(&active.client);
    let advance = expected
        .iter()
        .find(|command| command.name == "advance")
        .unwrap();
    assert!(advance.available);
    assert_eq!(advance.refusal, None);
    // The invocable direction's parity: the standby's served rows are
    // the active's, name for name.
    assert_eq!(commands(&standby.standby.client), expected);

    // Drive the active's count to the limit through the receipted
    // path — each `advance` settles `applied` at its own boundary —
    // and the covering pull adopts the completed state: the standby's
    // served rows carry the kind's named refusal, identical to the
    // field owner's, while the submission path it guards stays the
    // receipted one alone.
    for _ in 0..Gate::LIMIT {
        let receipt = active
            .client
            .command(&Command::Invoke {
                component: "gate".to_string(),
                command: "advance".to_string(),
                arguments: BTreeMap::new(),
            })
            .unwrap();
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        active.client.advance(1).unwrap();
        standby.standby.client.advance(1).unwrap();
    }
    let expected = commands(&active.client);
    let advance = expected
        .iter()
        .find(|command| command.name == "advance")
        .unwrap();
    assert!(!advance.available);
    assert_eq!(
        advance.refusal.as_deref(),
        Some("the gate is at its end; reset reopens it")
    );
    assert_eq!(commands(&standby.standby.client), expected);
}

/// The settle-audit rig's executor: one writable internal `In` point —
/// the held operator value the raced admission writes; the QA model's
/// command targets are all internal, and an image-carried point keeps
/// the exercise off the field.
fn held_executor(driver: &'static StubDriver) -> Executor<'static> {
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
}

/// The journal's `command_settled` entries for `command`, as
/// `(entry tick, outcome)` pairs in seq order — the per-admission
/// settle list the uniqueness contract counts.
fn settles(client: &MonitorClient, command: &dcs_core::Command) -> Vec<(u64, CommandOutcome)> {
    client
        .journal(0)
        .unwrap()
        .iter()
        .filter_map(|entry| match &entry.event {
            JournalEvent::CommandSettled { receipt } if &receipt.command == command => {
                Some((entry.tick.0, receipt.outcome.clone()))
            }
            _ => None,
        })
        .collect()
}

/// QA finding `tracking-peer-double-journals-command-settle` (#688):
/// a checkpoint pulled while the admission is still `Accepted`
/// re-queues the receipt on the tracking peer, whose quiesced scan
/// must carry it — never mint a local `Applied` the covering adoption
/// then overwrites with the line's verdict. On the reported build the
/// pair of settles journaled local tick first, line tick second —
/// two `command_settled` entries for one admission, the journal's
/// ticks going backwards. Replayed end to end on the driven pair:
/// the command submits between the active's scans, the standby's next
/// cycle adopts it pending, the active settles it on the line, and
/// the covering pull must journal the single settle — one per peer.
#[test]
fn a_tracking_peer_journals_one_settle_per_admission() {
    use dcs_core::Command;
    let (standby, active) = DrivenStandby::start_with(None, held_executor);

    // Converge the standby on the line, then race the admission: the
    // write submits between the active's scans, so the standby's next
    // pull adopts the receipt still `Accepted`.
    active.client.advance(3).unwrap();
    standby.standby.client.advance(1).unwrap();
    assert!(matches!(
        standby.standby.client.role().unwrap().sync,
        Some(StandbySync::Tracking { .. })
    ));
    let command = Command::WriteValue {
        point: PointId(40),
        kind: ValueKind::Bool,
        value: Value::Bool(true),
    };
    let receipt = active.client.command(&command).unwrap();
    assert_eq!(
        receipt.outcome,
        CommandOutcome::Accepted {
            apply_tick: Tick(4)
        }
    );

    // The raced pull: the standby's cycle adopts the pending receipt
    // and the quiesced scan carries it — on the reported build this
    // scan minted `Applied{local}` and journaled the phantom first
    // settle.
    standby.standby.client.advance(1).unwrap();
    assert!(
        matches!(
            standby.standby.client.receipts().unwrap()[0].outcome,
            CommandOutcome::Accepted { .. }
        ),
        "the carried admission stays pending on the tracking peer"
    );
    assert_eq!(settles(&standby.standby.client, &command), vec![]);

    // The line settles the admission; the covering pull adopts the
    // verdict — the one terminal outcome the admission ever gets.
    active.client.advance(1).unwrap();
    standby.standby.client.advance(1).unwrap();
    assert_eq!(
        settles(&standby.standby.client, &command),
        vec![(4, CommandOutcome::Applied { tick: Tick(4) })],
        "one admission journals one settle on the tracking peer"
    );
    assert_eq!(
        settles(&active.client, &command),
        vec![(4, CommandOutcome::Applied { tick: Tick(4) })],
        "one admission journals one settle on the field owner"
    );

    // The journal's attribution never walks backwards — the phantom
    // pair's local-then-line ordering regressed the tick.
    let journal = standby.standby.client.journal(0).unwrap();
    let ticks: Vec<u64> = journal.iter().map(|entry| entry.tick.0).collect();
    assert!(
        ticks.windows(2).all(|pair| pair[0] <= pair[1]),
        "the journal's ticks must be non-decreasing: {ticks:?}"
    );
}

/// The finding's demoted-peer half: the same raced admission observed
/// from the peer that *was* the field owner. The demote suspends its
/// pending receipt, the promote boundary's final sync carries it onto
/// the successor still `Accepted`, and the demoted peer's tracking
/// pull lands that checkpoint before the successor's applying scan —
/// the window in which the reported build minted a local `Applied`
/// the covering adoption then re-settled at the line's tick.
#[test]
fn a_demoted_peer_journals_one_settle_per_carried_admission() {
    use dcs_core::Command;
    const KEY: u64 = 0x85a3_08d3_1319_8a2e;
    // The announced-source contract is keyed-only — the demotion below
    // follows the standby's `?peer=` hint, so the pair carries the
    // `--pair-token` secret.
    let (standby, active) =
        DrivenStandby::start_binding(None, held_executor, "127.0.0.1:0", "127.0.0.1:0", Some(KEY));

    active.client.advance(3).unwrap();
    standby.standby.client.advance(1).unwrap();
    assert!(matches!(
        standby.standby.client.role().unwrap().sync,
        Some(StandbySync::Tracking { .. })
    ));

    // The raced admission: accepted on the field owner, still pending
    // when the documented demote-then-promote runs.
    let command = Command::WriteValue {
        point: PointId(40),
        kind: ValueKind::Bool,
        value: Value::Bool(true),
    };
    let receipt = active.client.command(&command).unwrap();
    assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
    assert_eq!(active.client.demote().unwrap().role, Role::Demoting);
    assert_eq!(
        standby.standby.client.promote().unwrap().role,
        Role::Promoting
    );

    // The demoted peer's first tracking pull lands the successor's
    // checkpoint while the carried admission is still `Accepted` —
    // suspended entries re-queue under the adoption, but the quiesced
    // scan must carry them, never mint a local `Applied`.
    active.client.advance(1).unwrap();
    assert_eq!(active.client.role().unwrap().role, Role::Standby);
    assert!(
        matches!(
            active.client.receipts().unwrap()[0].outcome,
            CommandOutcome::Accepted { .. }
        ),
        "the carried admission stays pending on the demoted peer"
    );
    assert_eq!(settles(&active.client, &command), vec![]);

    // The promoted peer's first field-owning scan settles the carried
    // admission on the line; the demoted peer's covering pull adopts
    // the verdict — one terminal outcome, journaled once per peer.
    standby.standby.client.advance(1).unwrap();
    active.client.advance(1).unwrap();
    let line = settles(&standby.standby.client, &command);
    assert_eq!(line.len(), 1, "{line:?}");
    assert!(
        matches!(line[0].1, CommandOutcome::Applied { .. }),
        "{line:?}"
    );
    assert_eq!(
        settles(&active.client, &command),
        line,
        "one admission journals one settle on the demoted peer too"
    );

    // Tick monotonicity on the demoted peer's journal as well.
    let journal = active.client.journal(0).unwrap();
    let ticks: Vec<u64> = journal.iter().map(|entry| entry.tick.0).collect();
    assert!(
        ticks.windows(2).all(|pair| pair[0] <= pair[1]),
        "the journal's ticks must be non-decreasing: {ticks:?}"
    );
}

/// QA finding `tracker-realign-regresses-journal-tick-axis` (#830): the
/// reported run paused the active for seconds while the standby kept
/// scanning; the tracker ran its own tick past the point's
/// `stale_after_ticks` budget and journaled the `good → stale` mark at
/// that run tick — then the realigning apply rewound the executor's
/// clock to the stream's tick, so the recovery's `stale → good`
/// transition journaled below the assertion's tick and tick-order
/// consumers read the clearance first.
///
/// Replayed on the driven pair with a partitionable relay freezing the
/// checkpoint stream the way `docker pause` froze the source: the
/// tracker's degraded scans cross the freshness budget and journal the
/// stale mark at the run's held clock; the restored pull's apply lands
/// at the run's own tick — `landed`, never the stream's — so the
/// recovery and the adopted settlement the covering checkpoint carries
/// attribute at or after the mark they follow. The receipt's own
/// `Applied{tick}` still names the line's apply tick — the one audit
/// the pair shares — but the journal's seq axis never rewinds.
#[test]
fn a_realign_after_a_degraded_window_never_rewinds_the_journal_axis() {
    let active_driver: &'static StubDriver = Box::leak(Box::new(StubDriver::new(&[
        (PointId(10), Value::Float(3.0)),
        (PointId(20), Value::Float(0.0)),
        (PointId(30), Value::Float(0.0)),
    ])));
    let active = Serving::start(
        Monitor::bind_peer(
            "127.0.0.1:0",
            Peer::active(stale_executor(active_driver), None),
            signal_index(),
        )
        .unwrap(),
    );
    // The standby tracks the active through the relay, so the frozen
    // window is a cut stream — unanswered pulls — not a stopped client.
    let relay = Relay::serve(dialable(active.monitor.local_addr()));
    let standby_driver: &'static StubDriver = Box::leak(Box::new(StubDriver::new(&[
        (PointId(10), Value::Float(3.0)),
        (PointId(20), Value::Float(0.0)),
        (PointId(30), Value::Float(0.0)),
    ])));
    let standby = Serving::start(
        Monitor::bind_peer(
            "127.0.0.1:0",
            Peer::standby(stale_executor(standby_driver), None),
            signal_index(),
        )
        .unwrap()
        .driven(Driven {
            track: Some(relay.addr),
            after_scan: None,
        }),
    );

    // Converge the standby on the line, then land the command on the
    // field owner between scans — the pending receipt the covering
    // checkpoint later adopts.
    active.client.advance(3).unwrap();
    standby.client.advance(1).unwrap();
    assert_eq!(
        standby.client.role().unwrap().sync,
        Some(StandbySync::Tracking { aligned: Tick(3) })
    );
    let command = Command::WriteValue {
        point: PointId(10),
        kind: ValueKind::Float,
        value: Value::Float(9.0),
    };
    let receipt = active.client.command(&command).unwrap();
    assert_eq!(
        receipt.outcome,
        CommandOutcome::Accepted {
            apply_tick: Tick(4)
        }
    );

    // The frozen window: the stream cuts while the standby keeps
    // scanning — each cycle's pull misses and the run's own tick
    // outruns the stale budget the field sample can no longer refresh
    // inside.
    relay.partition(true);
    standby.client.advance(4).unwrap();
    assert!(
        matches!(
            standby.client.role().unwrap().sync,
            Some(StandbySync::Degraded { .. })
        ),
        "the cut stream degrades the tracker: {:?}",
        standby.client.role().unwrap()
    );
    let stale = standby
        .client
        .journal(0)
        .unwrap()
        .into_iter()
        .find(|entry| {
            matches!(
                &entry.event,
                JournalEvent::QualityChanged { point: PointId(10), to, .. }
                    if *to == Quality::Uncertain(QualityReason::Stale)
            )
        })
        .expect("the degraded window journals the stale mark");
    assert!(
        stale.tick.0 > 4,
        "the mark stamps the run's held clock, ahead of the stream: {stale:?}"
    );

    // The stream restores: the line's covering checkpoint carries the
    // settled receipt and lands at the run's tick — the held clock
    // never rewinds to the stream's — and the journaled axis holds.
    relay.partition(false);
    active.client.advance(1).unwrap();
    standby.client.advance(1).unwrap();
    assert!(
        matches!(
            standby.client.role().unwrap().sync,
            Some(StandbySync::Tracking { aligned: Tick(4) })
        ),
        "the restored pull realigns the tracker: {:?}",
        standby.client.role().unwrap()
    );

    // The recovery: the standby's own driver finally reports a changed
    // field sample — the freshness evidence the frozen window held —
    // and the stale mark clears at the scanning run's tick.
    standby_driver
        .write(PointId(10), Value::Float(9.0))
        .unwrap();
    standby.client.advance(1).unwrap();

    let journal = standby.client.journal(0).unwrap();
    let ticks: Vec<u64> = journal.iter().map(|entry| entry.tick.0).collect();
    assert!(
        ticks.windows(2).all(|pair| pair[0] <= pair[1]),
        "the journal's ticks must be non-decreasing across the realign: \
         {ticks:?} — {journal:?}"
    );

    // The adopted settle journals once, the receipt still naming the
    // line's apply tick — the pair's one command audit — while its
    // entry attribution rides the run's non-rewinding axis.
    let outcomes = |client: &MonitorClient| {
        settles(client, &command)
            .into_iter()
            .map(|(_, outcome)| outcome)
            .collect::<Vec<_>>()
    };
    assert_eq!(
        outcomes(&standby.client),
        outcomes(&active.client),
        "the adopted settle's outcome matches the line's"
    );
    let settle = settles(&standby.client, &command);
    assert_eq!(settle.len(), 1, "{settle:?}");
    assert_eq!(
        settle[0].1,
        CommandOutcome::Applied { tick: Tick(4) },
        "the receipt carries the line's apply tick: {settle:?}"
    );
    assert!(
        settle[0].0 >= stale.tick.0,
        "the adopted settle attributes at the standing axis, not the \
         lagging stream tick: {settle:?} vs stale mark {stale:?}"
    );

    // The recovery clears the mark in order — the clearance's tick
    // never reads before the assertion's.
    let cleared = journal
        .iter()
        .find(|entry| {
            matches!(
                &entry.event,
                JournalEvent::QualityChanged { point: PointId(10), from, to }
                    if *from == Some(Quality::Uncertain(QualityReason::Stale))
                        && *to == Quality::Good
            )
        })
        .expect("the recovery journals the stale mark's clearance");
    assert!(
        cleared.tick.0 >= stale.tick.0 && cleared.seq > stale.seq,
        "the clearance follows the assertion in seq and tick order: \
         {stale:?} then {cleared:?}"
    );
}
