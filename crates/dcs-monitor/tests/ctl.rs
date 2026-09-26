//! Subprocess tests for the `dcs-ctl` operator tool: every subcommand
//! run against an in-process `Monitor` serving a fixture executor — the
//! read side roundtripping each contract payload, the command side
//! printing receipts and exiting nonzero on the contract's named
//! rejections, promote/demote against a standby peer rig with the named
//! `SwitchError`s, and the usage/transport failure surface — never a
//! panic.

use dcs_blocks::{Pid, PidConfig, Sequencer, SequencerStep};
use dcs_core::{
    Command, CommandError, CommandOutcome, CommandReceipt, Direction, ForcedPoint, IoDriver,
    IoError, JournalEvent, PointId, Quality, QualityReason, Role, RoleReport, Sample, SignalId,
    StandbySync, SwitchOrigin, Tick, Value, ValueKind,
};
use dcs_model::{PointSignal, SignalIndex};
use dcs_monitor::{Monitor, MonitorClient, PairFaultKind};
use dcs_runtime::{
    Component, ComponentIo, ComponentIoExt, Executor, IoRequirement, Peer, PointMap, StepError,
};
use std::collections::HashMap;
use std::net::{SocketAddr, TcpListener};
use std::process::{Command as Process, Output};
use std::sync::{Arc, Mutex};
use std::thread;

/// The tool under test, built by Cargo alongside the test harness.
const CTL: &str = env!("CARGO_BIN_EXE_dcs-ctl");

// The rig's points: `PV` is the model-declared writable field `In`
// point — the write/force target; `SP`, `COUNT`, and `FLAG` are
// writable internal `In` points covering each declared value kind;
// `OUT`/`PLAIN_OUT` are `Out` points — never legal command targets;
// `PLAIN_IN` is a field `In` point left unmarked. The `SEQ_*` points
// wire the sequencer — the rig's declared-command kind `invoke`
// exercises: `SEQ_RUN`/`SEQ_RESET` field `In` points left unmarked
// and the `SEQ_OUT`/`SEQ_STEP`/`SEQ_DONE` `Out` triple it drives.
const PV: PointId = PointId(10);
const SP: PointId = PointId(11);
const COUNT: PointId = PointId(12);
const FLAG: PointId = PointId(13);
const OUT: PointId = PointId(20);
const PLAIN_OUT: PointId = PointId(31);
const PLAIN_IN: PointId = PointId(40);
const SEQ_RUN: PointId = PointId(50);
const SEQ_RESET: PointId = PointId(51);
const SEQ_OUT: PointId = PointId(60);
const SEQ_STEP: PointId = PointId(61);
const SEQ_DONE: PointId = PointId(62);

/// In-memory driver stub; the same minimal stand-in the other monitor
/// tests use — `dcs-monitor` sees only the `IoDriver` contract.
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
        *sample = Sample::good(value, sample.tick);
        Ok(())
    }
}

/// A component with no `describe` override — the kind declaring no
/// parameters, so `set-parameter` against it answers the named
/// `unsupported_parameter` rejection.
struct Plain;

impl Component for Plain {
    fn name(&self) -> &str {
        "plain"
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        vec![
            IoRequirement::input::<f64>("in", PLAIN_IN),
            IoRequirement::output::<f64>("out", PLAIN_OUT),
        ]
    }

    fn step(&mut self, io: &dyn ComponentIo, _tick: Tick) -> Result<(), StepError> {
        let sample = io.read_typed::<f64>(PLAIN_IN)?;
        io.write_typed(PLAIN_OUT, sample.value)?;
        Ok(())
    }
}

/// One index entry; the rig's index is built inline — the tool needs
/// only the served `value_type` declarations.
fn entry(point: PointId, direction: Direction, kind: ValueKind, writable: bool) -> PointSignal {
    PointSignal {
        point,
        signal: Some(SignalId(point.0 + 100)),
        name: format!("point-{}", point.0),
        direction,
        value_type: kind,
        unit: None,
        description: None,
        group: None,
        writable,
        requires_reason: false,
    }
}

/// The rig's served signal index — one entry per mapped point, ordered
/// by id so `SignalIndex::get` resolves.
fn signal_index() -> SignalIndex {
    SignalIndex {
        points: vec![
            entry(PV, Direction::In, ValueKind::Float, true),
            entry(SP, Direction::In, ValueKind::Float, true),
            entry(COUNT, Direction::In, ValueKind::Int, true),
            entry(FLAG, Direction::In, ValueKind::Bool, true),
            entry(OUT, Direction::Out, ValueKind::Float, false),
            entry(PLAIN_OUT, Direction::Out, ValueKind::Float, false),
            entry(PLAIN_IN, Direction::In, ValueKind::Float, false),
            entry(SEQ_RUN, Direction::In, ValueKind::Bool, false),
            entry(SEQ_RESET, Direction::In, ValueKind::Bool, false),
            entry(SEQ_OUT, Direction::Out, ValueKind::Float, false),
            entry(SEQ_STEP, Direction::Out, ValueKind::Int, false),
            entry(SEQ_DONE, Direction::Out, ValueKind::Bool, false),
        ],
        components: vec![],
    }
}

fn point_map() -> PointMap {
    PointMap::new()
        .with_writable_point(PV, Direction::In, ValueKind::Float)
        .with_writable_internal(SP, Direction::In, ValueKind::Float, Value::Float(50.0))
        .with_writable_internal(COUNT, Direction::In, ValueKind::Int, Value::Int(0))
        .with_writable_internal(FLAG, Direction::In, ValueKind::Bool, Value::Bool(false))
        .with_point(OUT, Direction::Out, ValueKind::Float)
        .with_point(PLAIN_OUT, Direction::Out, ValueKind::Float)
        .with_point(PLAIN_IN, Direction::In, ValueKind::Float)
        .with_point(SEQ_RUN, Direction::In, ValueKind::Bool)
        .with_point(SEQ_RESET, Direction::In, ValueKind::Bool)
        .with_point(SEQ_OUT, Direction::Out, ValueKind::Float)
        .with_point(SEQ_STEP, Direction::Out, ValueKind::Int)
        .with_point(SEQ_DONE, Direction::Out, ValueKind::Bool)
}

fn components() -> Vec<Box<dyn Component>> {
    let pid = Pid::new(
        "level-pid",
        SP,
        PV,
        OUT,
        PidConfig {
            kp: 1.0,
            ki: 0.0,
            kd: 0.0,
            dt: 0.1,
            out_min: 0.0,
            out_max: 5.0,
        },
    )
    .unwrap();
    // The declared-command kind: `seq` declares the `advance`/`reset`
    // invoke surface — `advance` carrying the `count` Int argument and
    // `KindDeclared` availability, `reset` argument-free and `Always`.
    // A two-step table: `advance count=2` runs it to completion, where
    // the availability predicate starts refusing `advance` until
    // `reset` — the invoke tests' applied-and-refused pair.
    let sequencer = Sequencer::new(
        "seq",
        SEQ_RUN,
        SEQ_RESET,
        SEQ_OUT,
        SEQ_STEP,
        SEQ_DONE,
        vec![
            SequencerStep {
                ticks: 1,
                value: 10.0,
            },
            SequencerStep {
                ticks: 1,
                value: 20.0,
            },
        ],
    )
    .unwrap();
    vec![Box::new(pid), Box::new(Plain), Box::new(sequencer)]
}

/// The rig's field-side driver — every field `In` and `Out` point the
/// executor reads or drives. The writable internals are image state,
/// not driver points.
fn field_driver() -> StubDriver {
    StubDriver::new(&[
        (PV, Value::Float(0.0)),
        (OUT, Value::Float(0.0)),
        (PLAIN_OUT, Value::Float(0.0)),
        (PLAIN_IN, Value::Float(0.0)),
        (SEQ_RUN, Value::Bool(false)),
        (SEQ_RESET, Value::Bool(false)),
        (SEQ_OUT, Value::Float(0.0)),
        (SEQ_STEP, Value::Int(0)),
        (SEQ_DONE, Value::Bool(false)),
    ])
}

/// Builds the rig and runs `body` against a serving monitor, handing it
/// the driver, the bound address (the tool's `<addr>`), and an
/// in-process client for arranging the run; the server is shut down
/// before the driver's borrow ends.
fn with_monitor<T>(body: impl FnOnce(&StubDriver, SocketAddr, &MonitorClient) -> T) -> T {
    let driver = field_driver();
    let executor = Executor::new(&driver, point_map(), components()).unwrap();
    let monitor = Monitor::bind("127.0.0.1:0", executor, signal_index()).unwrap();
    let addr = monitor.local_addr();
    let client = MonitorClient::new(addr);
    let result = thread::scope(|scope| {
        scope.spawn(|| monitor.serve());
        // A failing assertion must not deadlock the scope join: catch the
        // panic so the server is always shut down before it propagates.
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            body(&driver, addr, &client)
        }));
        monitor.shutdown();
        result
    });
    result.unwrap_or_else(|panic| std::panic::resume_unwind(panic))
}

/// One peer of the pair rig: its monitor served on a dedicated thread —
/// the same shape the pair-view tests use. The driver is leaked
/// `'static` so the monitor outlives any borrow.
struct PeerRig {
    monitor: Arc<Monitor<'static>>,
    client: MonitorClient,
    addr: SocketAddr,
    thread: Option<thread::JoinHandle<()>>,
}

impl PeerRig {
    /// Assembles a peer executor over a private stub driver — `gate`
    /// `None`: this test exercises the role surface, not field
    /// quiescence — and serves its monitor on a spawned thread.
    fn start(role: Role) -> Self {
        Self::start_tracking(role, None)
    }

    /// [`start`](Self::start) with `source` recorded as the monitor's
    /// tracking source — the configured `--peer`/`--standby` half of
    /// the follow-peer contract a demotion tracks.
    fn start_tracking(role: Role, source: Option<SocketAddr>) -> Self {
        let driver: &'static StubDriver = Box::leak(Box::new(field_driver()));
        let executor = Executor::new(driver, point_map(), components()).unwrap();
        let peer = match role {
            Role::Active => Peer::active(executor, None),
            _ => Peer::standby(executor, None),
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

    /// Stops serving and drops the monitor handle.
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

/// Runs the tool against the monitor at `addr`. `DCS_ACTOR` — the
/// configured actor default — is removed so an ambient value can never
/// attribute a run meant to submit unattributed; `ctl_env` is the
/// variant for cases setting it.
fn ctl(addr: SocketAddr, args: &[&str]) -> Output {
    ctl_env(addr, args, &[])
}

/// Runs the tool against the monitor at `addr` with extra environment.
fn ctl_env(addr: SocketAddr, args: &[&str], env: &[(&str, &str)]) -> Output {
    let mut command = Process::new(CTL);
    command
        .arg(addr.to_string())
        .args(args)
        .env_remove("DCS_ACTOR");
    for &(key, value) in env {
        command.env(key, value);
    }
    command.output().expect("dcs-ctl runs")
}

/// Runs the tool with `args` verbatim — for malformed-command-line
/// cases where the address itself is absent.
fn ctl_args(args: &[&str]) -> Output {
    Process::new(CTL)
        .args(args)
        .env_remove("DCS_ACTOR")
        .output()
        .expect("dcs-ctl runs")
}

/// Runs the tool expecting success and returns the server's answer as
/// printed JSON on stdout.
fn ctl_ok(addr: SocketAddr, args: &[&str]) -> serde_json::Value {
    let output = ctl(addr, args);
    assert!(
        output.status.success(),
        "{args:?} failed: {}",
        stderr(&output)
    );
    serde_json::from_slice(&output.stdout).expect("stdout is the answer's JSON")
}

fn stdout(output: &Output) -> String {
    String::from_utf8_lossy(&output.stdout).into_owned()
}

fn stderr(output: &Output) -> String {
    String::from_utf8_lossy(&output.stderr).into_owned()
}

fn telemetry(snapshot: &dcs_core::TelemetrySnapshot, point: PointId) -> &dcs_core::PointTelemetry {
    snapshot
        .points
        .iter()
        .find(|telemetry| telemetry.point == point)
        .unwrap_or_else(|| panic!("no telemetry for {point:?}"))
}

#[test]
fn read_subcommands_roundtrip_the_served_payloads() {
    with_monitor(|driver, addr, client| {
        driver.write(PV, Value::Float(3.0)).unwrap();
        client.advance(2).unwrap();

        // `snapshot` prints the served TelemetrySnapshot.
        let snapshot: dcs_core::TelemetrySnapshot =
            serde_json::from_value(ctl_ok(addr, &["snapshot"])).unwrap();
        assert_eq!(snapshot.tick, Tick(2));
        assert_eq!(
            telemetry(&snapshot, PV).sample.unwrap().value,
            Value::Float(3.0)
        );

        // `signals` prints the served SignalIndex.
        let index: SignalIndex = serde_json::from_value(ctl_ok(addr, &["signals"])).unwrap();
        assert_eq!(index, signal_index());

        // `role` prints the RoleReport — a plain monitor reports active.
        let report: RoleReport = serde_json::from_value(ctl_ok(addr, &["role"])).unwrap();
        assert_eq!(
            report,
            RoleReport {
                role: Role::Active,
                tick: Tick(2),
                sync: None,
                field_claim: None,
                failover: None,
            }
        );

        // `receipts` prints the receipt log — empty so far.
        let receipts: Vec<CommandReceipt> =
            serde_json::from_value(ctl_ok(addr, &["receipts"])).unwrap();
        assert!(receipts.is_empty());

        // `history --point` prints that point's retained samples.
        let history: Vec<dcs_core::PointHistory> =
            serde_json::from_value(ctl_ok(addr, &["history", "--point", "10"])).unwrap();
        assert_eq!(history.len(), 1);
        assert_eq!(
            history[0]
                .samples
                .iter()
                .map(|entry| (entry.seq, entry.sample.value))
                .collect::<Vec<_>>(),
            vec![(1, Value::Float(3.0)), (2, Value::Float(3.0))]
        );
        // Multi-point selection and the since cursor compose.
        let history: Vec<dcs_core::PointHistory> = serde_json::from_value(ctl_ok(
            addr,
            &["history", "--point", "10", "--point", "20", "--since", "1"],
        ))
        .unwrap();
        assert_eq!(
            history
                .iter()
                .map(|point| (point.point, point.samples.len()))
                .collect::<Vec<_>>(),
            vec![(PV, 1), (OUT, 1)]
        );

        // `journal` prints the journal; `--since` keeps only newer
        // entries. A command settling at a scan boundary gives the
        // journal content: a rejected write is journaled at submission.
        let rejected = ctl(addr, &["write", "20", "5"]);
        assert!(!rejected.status.success());
        let journal: Vec<dcs_core::JournalEntry> =
            serde_json::from_value(ctl_ok(addr, &["journal"])).unwrap();
        assert!(!journal.is_empty());
        let last = journal.last().unwrap().seq;
        let newer: Vec<dcs_core::JournalEntry> =
            serde_json::from_value(ctl_ok(addr, &["journal", "--since", &last.to_string()]))
                .unwrap();
        assert!(newer.is_empty());
        let rest: Vec<dcs_core::JournalEntry> =
            serde_json::from_value(ctl_ok(addr, &["journal", "--since", "0"])).unwrap();
        assert_eq!(rest, journal);
    });
}

#[test]
fn schema_prints_the_served_interface_registry() {
    with_monitor(|_driver, addr, client| {
        client.advance(1).unwrap();

        // `schema` prints the served SchemaView: one interface per
        // instance, each carrying the five declared collections — the
        // `kind` keys the kind-level registry.
        let schema: dcs_core::SchemaView =
            serde_json::from_value(ctl_ok(addr, &["schema"])).unwrap();
        assert_eq!(schema.interfaces.len(), 3);
        for entry in &schema.interfaces {
            assert_eq!(entry.interface.version, dcs_core::INTERFACE_VERSION);
        }

        // The sequencer kind's declared vocabulary serves under the
        // instance: `advance`/`reset` sit beside the port- and
        // parameter-adapted generic commands, and the kind-emitted
        // events — one declared per retention class — beside the
        // adapted journal transitions, each spec's `retention` mark
        // routing the consumer: `step_completed` the bounded
        // `history` record, `sequence_completed` the durable
        // `journal`, `progress` the superseding `latest` view.
        let seq = schema
            .interfaces
            .iter()
            .find(|entry| entry.name == "seq")
            .unwrap();
        assert_eq!(seq.interface.kind, "sequencer");
        let commands: Vec<&str> = seq
            .interface
            .commands
            .iter()
            .map(|command| command.name.as_str())
            .collect();
        for name in [
            "advance",
            "reset",
            "write_value:run",
            "set_parameter:step_count",
        ] {
            assert!(commands.contains(&name), "{name} missing: {commands:?}");
        }
        let events: Vec<&str> = seq
            .interface
            .events
            .iter()
            .map(|event| event.name.as_str())
            .collect();
        assert!(events.contains(&"command_settled"), "{events:?}");
        for (name, retention) in [
            ("step_completed", dcs_core::EventRetention::History),
            ("sequence_completed", dcs_core::EventRetention::Journal),
            ("progress", dcs_core::EventRetention::Latest),
        ] {
            let spec = seq
                .interface
                .events
                .iter()
                .find(|event| event.name == name)
                .unwrap_or_else(|| panic!("{name} missing: {events:?}"));
            assert_eq!(spec.retention, retention, "{name}");
            assert_eq!(
                spec.emission,
                dcs_core::EventEmission::KindEmitted,
                "{name}"
            );
        }

        // And the port/parameter halves: `run` is a measurement, `done`
        // a Status-roled state, `step_count` a tunable configuration.
        assert!(
            seq.interface
                .measurements
                .iter()
                .any(|measurement| measurement.name == "run" && measurement.point == Some(SEQ_RUN))
        );
        assert!(
            seq.interface
                .state
                .iter()
                .any(|state| state.name == "done" && state.point == Some(SEQ_DONE))
        );
        assert!(
            seq.interface
                .configuration
                .iter()
                .any(|property| property.name == "step_count")
        );
    });
}

#[test]
fn events_print_each_components_recent_emissions() {
    with_monitor(|driver, addr, client| {
        // Before the first scan the attributed event record is empty —
        // the empty case prints an empty list for a served component.
        let events: Vec<dcs_core::ResourceEvent> =
            serde_json::from_value(ctl_ok(addr, &["events", "seq"])).unwrap();
        assert!(events.is_empty());

        // Holding `run` through a scan completes the one-tick step: the
        // kind-emitted `step_completed` lands in the event-history ring
        // attributed to `seq` — the emitted event the run produced
        // reflected in `events`, marked `history`-retained — beside the
        // standing `progress` record the `latest` mark carries.
        driver.write(SEQ_RUN, Value::Bool(true)).unwrap();
        client.advance(1).unwrap();
        let events: Vec<dcs_core::ResourceEvent> =
            serde_json::from_value(ctl_ok(addr, &["events", "seq"])).unwrap();
        let completed = events
            .iter()
            .find(|entry| {
                matches!(
                    &entry.event,
                    JournalEvent::EventEmitted { event }
                        if event.event == "step_completed"
                            && event.component == "seq"
                            && event.fields["step"] == dcs_core::EventValue::Value(Value::Int(1))
                )
            })
            .expect("the attributed record carries step_completed");
        assert_eq!(completed.retention, dcs_core::EventRetention::History);
        let progress = events
            .iter()
            .find(|entry| {
                matches!(
                    &entry.event,
                    JournalEvent::EventEmitted { event } if event.event == "progress"
                )
            })
            .expect("the attributed record carries progress");
        assert_eq!(progress.retention, dcs_core::EventRetention::Latest);
        // Neither `history` nor `latest` emission is journaled — the
        // durable record's mark appears on no routed entry beside the
        // journal-retained adapted transitions the tail also carries.
        assert!(events.iter().all(|entry| {
            !matches!(
                &entry.event,
                JournalEvent::EventEmitted { event }
                    if event.event == "step_completed" || event.event == "progress"
            ) || entry.retention != dcs_core::EventRetention::Journal
        }));

        // The all-components form keys every served instance's list by
        // name — `seq`'s carries the same tail, and `level-pid`'s the
        // first-observation transitions on its bound points.
        let all: serde_json::Value = ctl_ok(addr, &["events"]);
        for name in ["level-pid", "plain", "seq"] {
            assert!(all.get(name).is_some(), "{name} missing: {all}");
        }
        assert_eq!(
            serde_json::from_value::<Vec<dcs_core::ResourceEvent>>(all["seq"].clone()).unwrap(),
            events
        );
        assert!(all["level-pid"].as_array().unwrap().iter().any(|entry| {
            entry["event"]["quality_changed"]["point"] == serde_json::json!(PV.0)
        }));

        // A name the served registry does not carry fails the
        // invocation naming it — a lookup miss, not a rejection.
        let output = ctl(addr, &["events", "ghost"]);
        assert!(!output.status.success());
        assert!(stderr(&output).contains("ghost"), "{output:?}");
    });
}

#[test]
fn resources_print_the_served_live_resource_view() {
    with_monitor(|driver, addr, client| {
        // One scan so every bound point has a served sample: `run`
        // held completes the first one-tick step.
        driver.write(SEQ_RUN, Value::Bool(true)).unwrap();
        client.advance(1).unwrap();

        // The bare form prints the served ResourceView — the same
        // document the client's own accessor decodes, one entry per
        // served instance in scan order.
        let view: dcs_core::ResourceView =
            serde_json::from_value(ctl_ok(addr, &["resources"])).unwrap();
        assert_eq!(view, client.resources().unwrap());
        assert_eq!(
            view.components
                .iter()
                .map(|entry| entry.name.as_str())
                .collect::<Vec<_>>(),
            ["level-pid", "plain", "seq"]
        );

        // The filtered form prints the named instance's
        // ComponentResources — the same element of the bare view,
        // addressed like `events <component>`: measurements and state
        // joined to the bound points' samples, the current
        // configuration values, and each command's
        // `available`/`refusal`.
        let seq: dcs_core::ComponentResources =
            serde_json::from_value(ctl_ok(addr, &["resources", "seq"])).unwrap();
        assert_eq!(
            seq,
            *view
                .components
                .iter()
                .find(|entry| entry.name == "seq")
                .unwrap()
        );
        assert_eq!(seq.kind, "sequencer");
        let run = seq
            .measurements
            .iter()
            .find(|entry| entry.name == "run")
            .unwrap();
        assert_eq!(run.point, Some(SEQ_RUN));
        assert_eq!(run.sample, Some(Sample::good(Value::Bool(true), Tick(1))));
        let done = seq.state.iter().find(|entry| entry.name == "done").unwrap();
        assert_eq!(done.point, Some(SEQ_DONE));
        assert_eq!(done.sample, Some(Sample::good(Value::Bool(false), Tick(1))));
        assert_eq!(
            seq.configuration
                .iter()
                .find(|entry| entry.name == "step_count")
                .unwrap()
                .value,
            Some(Value::Int(2))
        );
        // `advance`/`reset` report admissible; `write_value:run`'s
        // bound point is not model-declared writable, so its served
        // refusal is the reason the receipted path would answer.
        for name in ["advance", "reset"] {
            let command = seq
                .commands
                .iter()
                .find(|command| command.name == name)
                .unwrap_or_else(|| panic!("{name} missing: {:?}", seq.commands));
            assert!(command.available, "{name}: {command:?}");
        }
        let write_run = seq
            .commands
            .iter()
            .find(|command| command.name == "write_value:run")
            .unwrap();
        assert!(!write_run.available);
        assert_eq!(
            write_run.refusal.as_deref(),
            Some("I/O point PointId(50) is not declared writable")
        );

        // The `KindDeclared` probe's published verdict joins the same
        // read: `advance` landing the table's last step completes the
        // run, and the served state turns unavailable carrying the
        // kind's standing refusal — the text a refused submission's
        // receipt settles — while `reset` stays available.
        let receipt: CommandReceipt =
            serde_json::from_value(ctl_ok(addr, &["invoke", "seq", "advance"])).unwrap();
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        ctl_ok(addr, &["scan", "1"]);
        let seq: dcs_core::ComponentResources =
            serde_json::from_value(ctl_ok(addr, &["resources", "seq"])).unwrap();
        let advance = seq
            .commands
            .iter()
            .find(|command| command.name == "advance")
            .unwrap();
        assert!(!advance.available, "{advance:?}");
        assert_eq!(
            advance.refusal.as_deref(),
            Some("the sequence has run to its end; reset restarts it")
        );
        assert!(
            seq.commands
                .iter()
                .find(|command| command.name == "reset")
                .unwrap()
                .available
        );

        // A name the served registry does not carry fails the
        // invocation naming it — the same lookup `events <component>`
        // performs, never a rejection.
        let output = ctl(addr, &["resources", "ghost"]);
        assert!(!output.status.success());
        assert!(stderr(&output).contains("ghost"), "{output:?}");
    });
}

#[test]
fn write_parses_per_the_declared_kind_and_reports_receipts() {
    with_monitor(|_driver, addr, client| {
        client.advance(1).unwrap();

        // `write <point> <value>` parses per the point's declared kind:
        // "60" on the Float internal setpoint is Float 60.0, "3" on the
        // Int point is Int 3, "true" on the Bool point is Bool true —
        // each answered by an accepted receipt printed as JSON.
        let receipt: CommandReceipt =
            serde_json::from_value(ctl_ok(addr, &["write", "11", "60"])).unwrap();
        assert_eq!(
            receipt,
            CommandReceipt {
                command: Command::WriteValue {
                    point: SP,
                    kind: ValueKind::Float,
                    value: Value::Float(60.0),
                },
                outcome: CommandOutcome::Accepted {
                    apply_tick: Tick(2)
                },
                actor: None,
                reason: None,
            }
        );
        let receipt: CommandReceipt =
            serde_json::from_value(ctl_ok(addr, &["write", "12", "3"])).unwrap();
        assert_eq!(
            receipt.command,
            Command::WriteValue {
                point: COUNT,
                kind: ValueKind::Int,
                value: Value::Int(3),
            }
        );
        let receipt: CommandReceipt =
            serde_json::from_value(ctl_ok(addr, &["write", "13", "true"])).unwrap();
        assert_eq!(
            receipt.command,
            Command::WriteValue {
                point: FLAG,
                kind: ValueKind::Bool,
                value: Value::Bool(true),
            }
        );

        // A field `In` write reaches the driver at the next scan
        // boundary — the same command path the page's write affordance
        // uses — and the Pid's output follows it: pv 48 against sp 60,
        // kp 1 → out 12 clamped to the 0..5 limits.
        let receipt: CommandReceipt =
            serde_json::from_value(ctl_ok(addr, &["write", "10", "48"])).unwrap();
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        let snapshot: dcs_core::TelemetrySnapshot =
            serde_json::from_value(ctl_ok(addr, &["scan", "1"])).unwrap();
        assert_eq!(snapshot.tick, Tick(2));
        assert_eq!(
            telemetry(&snapshot, PV).sample.unwrap().value,
            Value::Float(48.0)
        );
        assert_eq!(
            telemetry(&snapshot, OUT).sample.unwrap().value,
            Value::Float(5.0)
        );
        // The receipt log reflects the settled writes.
        let receipts: Vec<CommandReceipt> =
            serde_json::from_value(ctl_ok(addr, &["receipts"])).unwrap();
        assert_eq!(receipts.len(), 4);
        assert_eq!(
            receipts.last().unwrap().outcome,
            CommandOutcome::Applied { tick: Tick(2) }
        );
    });
}

#[test]
fn command_rejections_exit_nonzero_naming_the_command_error() {
    with_monitor(|_driver, addr, _client| {
        // A non-writable point: the rejected receipt still prints on
        // stdout, and stderr names the CommandError.
        let output = ctl(addr, &["write", "20", "5"]);
        assert!(!output.status.success());
        let receipt: CommandReceipt = serde_json::from_str(&stdout(&output)).unwrap();
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: dcs_core::CommandError::NotWritable { point: OUT }
            }
        );
        assert!(stderr(&output).contains("not_writable"), "{output:?}");

        // An unmarked field `In` point rejects the same way.
        let output = ctl(addr, &["write", "40", "5"]);
        assert!(!output.status.success());
        assert!(stderr(&output).contains("not_writable"), "{output:?}");

        // A point the index does not declare still reaches the server —
        // the literal's kind rides the command — and the contract's
        // `unknown_point` rejection answers.
        let output = ctl(addr, &["write", "99", "5"]);
        assert!(!output.status.success());
        assert!(stderr(&output).contains("unknown_point"), "{output:?}");
    });
}

#[test]
fn set_parameter_parses_per_the_descriptor_and_names_rejections() {
    with_monitor(|driver, addr, client| {
        driver.write(PV, Value::Float(48.0)).unwrap();
        client.advance(1).unwrap();

        // `set-parameter <component> <name> <value>` parses per the
        // parameter's declared kind: "0.5" on kp is Float 0.5.
        let receipt: CommandReceipt =
            serde_json::from_value(ctl_ok(addr, &["set-parameter", "level-pid", "kp", "0.5"]))
                .unwrap();
        assert_eq!(
            receipt.command,
            Command::SetParameter {
                component: "level-pid".to_string(),
                name: "kp".to_string(),
                value: Value::Float(0.5),
            }
        );
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));

        // The tuned loop runs it at the next scan: out = 0.5·(50−48) = 1.
        let snapshot = client.advance(1).unwrap();
        assert_eq!(
            telemetry(&snapshot, OUT).sample.unwrap().value,
            Value::Float(1.0)
        );

        // An out-of-range tune answers the named rejection: dt declares
        // the positive-finite range.
        let output = ctl(addr, &["set-parameter", "level-pid", "dt", "-1"]);
        assert!(!output.status.success());
        assert!(stderr(&output).contains("out_of_range"), "{output:?}");

        // Undeclared targets still reach the server and answer its named
        // rejections: unknown component, unknown parameter, and a kind
        // exposing no parameters at all.
        for (args, name) in [
            (
                ["set-parameter", "ghost", "kp", "1"].as_slice(),
                "unknown_component",
            ),
            (
                ["set-parameter", "level-pid", "bogus", "1"].as_slice(),
                "unknown_parameter",
            ),
            (
                ["set-parameter", "plain", "anything", "1"].as_slice(),
                "unsupported_parameter",
            ),
        ] {
            let output = ctl(addr, args);
            assert!(!output.status.success(), "{args:?} unexpectedly succeeded");
            assert!(
                stderr(&output).contains(name),
                "{args:?}: {}",
                stderr(&output)
            );
        }
    });
}

#[test]
fn invoke_runs_declared_commands_and_names_refusals() {
    with_monitor(|_driver, addr, client| {
        client.advance(1).unwrap();

        // `invoke <component> <command> [<name>=<value>]` parses each
        // argument per the served schema's declared request kind —
        // `count` is `advance`'s declared Int — and submits the Invoke
        // variant through the receipted path: the printed receipt is
        // the accepted answer.
        let receipt: CommandReceipt =
            serde_json::from_value(ctl_ok(addr, &["invoke", "seq", "advance", "count=2"])).unwrap();
        assert_eq!(
            receipt.command,
            Command::Invoke {
                component: "seq".to_string(),
                command: "advance".to_string(),
                arguments: [("count".to_string(), Value::Int(2))].into_iter().collect(),
            }
        );
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));

        // The invocation applies at the next scan boundary: count 2
        // walks the two-step table to its end — `done` asserts — and
        // the settled receipt lands applied in the log.
        let snapshot: dcs_core::TelemetrySnapshot =
            serde_json::from_value(ctl_ok(addr, &["scan", "1"])).unwrap();
        assert_eq!(
            telemetry(&snapshot, SEQ_DONE).sample.unwrap().value,
            Value::Bool(true)
        );
        let receipts: Vec<CommandReceipt> =
            serde_json::from_value(ctl_ok(addr, &["receipts"])).unwrap();
        assert_eq!(
            receipts.last().unwrap().outcome,
            CommandOutcome::Applied { tick: Tick(2) }
        );

        // The same command is now declared-unavailable — the completed
        // table's KindDeclared predicate. Admission still accepts it
        // (the kind's own predicate decides), and the boundary settles
        // the named refusal: `command_refused` carrying the kind's
        // declared reason verbatim.
        let receipt: CommandReceipt =
            serde_json::from_value(ctl_ok(addr, &["invoke", "seq", "advance"])).unwrap();
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        ctl_ok(addr, &["scan", "1"]);
        let receipts: Vec<CommandReceipt> =
            serde_json::from_value(ctl_ok(addr, &["receipts"])).unwrap();
        assert_eq!(
            receipts.last().unwrap().outcome,
            CommandOutcome::Rejected {
                reason: CommandError::CommandRefused {
                    component: "seq".to_string(),
                    command: "advance".to_string(),
                    reason: "the sequence has run to its end; reset restarts it".to_string(),
                }
            }
        );

        // `reset` — the Always-available declared command — applies and
        // the next step reports the restarted table.
        let receipt: CommandReceipt =
            serde_json::from_value(ctl_ok(addr, &["invoke", "seq", "reset"])).unwrap();
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        let snapshot: dcs_core::TelemetrySnapshot =
            serde_json::from_value(ctl_ok(addr, &["scan", "1"])).unwrap();
        assert_eq!(
            telemetry(&snapshot, SEQ_DONE).sample.unwrap().value,
            Value::Bool(false)
        );

        // Admission rejections still print the rejected receipt, exit
        // nonzero, and name the CommandError: an undeclared command on
        // a known component, and an unknown component.
        for (args, name) in [
            (["invoke", "seq", "bogus"].as_slice(), "unknown_command"),
            (
                ["invoke", "ghost", "advance"].as_slice(),
                "unknown_component",
            ),
        ] {
            let output = ctl(addr, args);
            assert!(!output.status.success(), "{args:?} unexpectedly succeeded");
            let receipt: CommandReceipt = serde_json::from_str(&stdout(&output)).unwrap();
            assert!(matches!(receipt.outcome, CommandOutcome::Rejected { .. }));
            assert!(
                stderr(&output).contains(name),
                "{args:?}: {}",
                stderr(&output)
            );
        }
    });
}

#[test]
fn force_and_unforce_roundtrip_through_the_receipted_path() {
    with_monitor(|driver, addr, client| {
        driver.write(PV, Value::Float(1.0)).unwrap();
        client.advance(1).unwrap();

        // `force <point> <value>` parses per the declared kind like a
        // write and pins the point across scans.
        let receipt: CommandReceipt =
            serde_json::from_value(ctl_ok(addr, &["force", "10", "9"])).unwrap();
        assert_eq!(
            receipt.command,
            Command::ForcePoint {
                point: PV,
                kind: ValueKind::Float,
                value: Value::Float(9.0),
            }
        );
        let snapshot: dcs_core::TelemetrySnapshot =
            serde_json::from_value(ctl_ok(addr, &["scan", "1"])).unwrap();
        assert_eq!(
            snapshot.forces,
            vec![ForcedPoint {
                point: PV,
                value: Value::Float(9.0),
            }]
        );
        assert_eq!(
            telemetry(&snapshot, PV).sample.unwrap().quality,
            Quality::Uncertain(QualityReason::Substituted)
        );

        // `unforce <point>` releases it; the next scan reads the field
        // again.
        driver.write(PV, Value::Float(4.0)).unwrap();
        let receipt: CommandReceipt =
            serde_json::from_value(ctl_ok(addr, &["unforce", "10"])).unwrap();
        assert_eq!(receipt.command, Command::UnforcePoint { point: PV });
        let snapshot: dcs_core::TelemetrySnapshot =
            serde_json::from_value(ctl_ok(addr, &["scan", "1"])).unwrap();
        assert!(snapshot.forces.is_empty());
        assert_eq!(
            telemetry(&snapshot, PV).sample.unwrap().value,
            Value::Float(4.0)
        );

        // A force on an `Out` point and a release on an unknown point
        // answer their named rejections.
        let output = ctl(addr, &["force", "20", "5"]);
        assert!(!output.status.success());
        assert!(stderr(&output).contains("not_writable"), "{output:?}");
        let output = ctl(addr, &["unforce", "99"]);
        assert!(!output.status.success());
        assert!(stderr(&output).contains("unknown_point"), "{output:?}");
    });
}

/// The `CommandSettled` receipts of the served journal, in order.
fn settled_receipts(client: &MonitorClient) -> Vec<CommandReceipt> {
    client
        .journal(0)
        .unwrap()
        .into_iter()
        .filter_map(|entry| match entry.event {
            JournalEvent::CommandSettled { receipt } => Some(receipt),
            _ => None,
        })
        .collect()
}

/// The `Command::Invoke` an `invoke seq <command>` submission sends —
/// the shape the printed receipt echoes.
fn invoke(command: &str, arguments: &[(&str, Value)]) -> Command {
    Command::Invoke {
        component: "seq".to_string(),
        command: command.to_string(),
        arguments: arguments
            .iter()
            .map(|(name, value)| (name.to_string(), *value))
            .collect(),
    }
}

#[test]
fn invoke_submits_declared_commands_and_prints_the_receipt() {
    with_monitor(|_driver, addr, client| {
        client.advance(1).unwrap();

        // `invoke <component> <command> [<name>=<value>]` builds the
        // declared-command submission: `advance` carries its declared
        // `count` Int argument — the printed receipt echoes the exact
        // Command::Invoke and the accepted outcome.
        let receipt: CommandReceipt =
            serde_json::from_value(ctl_ok(addr, &["invoke", "seq", "advance", "count=1"])).unwrap();
        assert_eq!(
            receipt,
            CommandReceipt {
                command: invoke("advance", &[("count", Value::Int(1))]),
                outcome: CommandOutcome::Accepted {
                    apply_tick: Tick(2)
                },
                actor: None,
                reason: None,
            }
        );

        // The invocation applies at the next scan boundary — the table
        // moves to step 2 — and the settled receipt's journaled
        // CommandSettled echo reads `applied`.
        ctl_ok(addr, &["scan", "1"]);
        let snapshot: dcs_core::TelemetrySnapshot =
            serde_json::from_value(ctl_ok(addr, &["snapshot"])).unwrap();
        assert_eq!(
            telemetry(&snapshot, SEQ_STEP).sample.unwrap().value,
            Value::Int(2)
        );
        let receipts: Vec<CommandReceipt> =
            serde_json::from_value(ctl_ok(addr, &["receipts"])).unwrap();
        assert_eq!(
            receipts.last().unwrap().outcome,
            CommandOutcome::Applied { tick: Tick(2) }
        );
        assert_eq!(settled_receipts(client).last(), receipts.last());

        // `reset` is argument-free and `Always`-available: it applies
        // and the table reports step 1 again.
        let receipt: CommandReceipt =
            serde_json::from_value(ctl_ok(addr, &["invoke", "seq", "reset"])).unwrap();
        assert_eq!(receipt.command, invoke("reset", &[]));
        ctl_ok(addr, &["scan", "1"]);
        let snapshot: dcs_core::TelemetrySnapshot =
            serde_json::from_value(ctl_ok(addr, &["snapshot"])).unwrap();
        assert_eq!(
            telemetry(&snapshot, SEQ_STEP).sample.unwrap().value,
            Value::Int(1)
        );
    });
}

#[test]
fn invoke_rejections_print_the_receipt_and_name_the_command_error() {
    with_monitor(|_driver, addr, client| {
        client.advance(1).unwrap();

        // Each named submission rejection prints the server's rejected
        // receipt on stdout and exits nonzero naming the CommandError:
        // unknown component and undeclared command — including a kind
        // declaring none — and an argument name the declared `request`
        // schema does not carry. An argument whose text is not its
        // declared request kind's value never reaches the server: the
        // schema-driven parse fails it as usage (see
        // `value_parse_errors_print_usage_against_a_live_monitor`).
        for (args, name) in [
            (
                ["invoke", "ghost", "advance"].as_slice(),
                "unknown_component",
            ),
            (["invoke", "seq", "spin"].as_slice(), "unknown_command"),
            (
                ["invoke", "plain", "anything"].as_slice(),
                "unknown_command",
            ),
            (
                ["invoke", "seq", "advance", "stride=2"].as_slice(),
                "unknown_argument",
            ),
        ] {
            let output = ctl(addr, args);
            assert!(!output.status.success(), "{args:?} unexpectedly succeeded");
            let receipt: CommandReceipt = serde_json::from_str(&stdout(&output)).unwrap();
            assert!(
                matches!(receipt.outcome, CommandOutcome::Rejected { .. }),
                "{args:?}: {}",
                stdout(&output)
            );
            assert!(
                stderr(&output).contains(name),
                "{args:?}: {}",
                stderr(&output)
            );
        }

        // The unavailable command: `advance` on a completed table is
        // the kind's `KindDeclared` refusal — the submission still
        // validates, so the printed receipt reads `accepted` and the
        // refusal settles at the boundary onto the journaled
        // `command_refused` receipt.
        let receipt: CommandReceipt =
            serde_json::from_value(ctl_ok(addr, &["invoke", "seq", "advance", "count=2"])).unwrap();
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        ctl_ok(addr, &["scan", "1"]); // lands past the last step: done
        let receipt: CommandReceipt =
            serde_json::from_value(ctl_ok(addr, &["invoke", "seq", "advance"])).unwrap();
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        ctl_ok(addr, &["scan", "1"]);
        let receipts: Vec<CommandReceipt> =
            serde_json::from_value(ctl_ok(addr, &["receipts"])).unwrap();
        assert_eq!(
            receipts.last().unwrap().outcome,
            CommandOutcome::Rejected {
                reason: dcs_core::CommandError::CommandRefused {
                    component: "seq".to_string(),
                    command: "advance".to_string(),
                    reason: "the sequence has run to its end; reset restarts it".to_string(),
                }
            }
        );
    });
}

#[test]
fn command_submissions_carry_the_declared_actor() {
    with_monitor(|_driver, addr, client| {
        client.advance(1).unwrap();

        // `--actor <name>` on a write declares the identity: the
        // printed receipt carries it, and the journaled CommandSettled
        // entry — the settled receipt's echo — shows the attribution.
        let receipt: CommandReceipt =
            serde_json::from_value(ctl_ok(addr, &["write", "11", "60", "--actor", "console-7"]))
                .unwrap();
        assert_eq!(receipt.actor.as_deref(), Some("console-7"));
        ctl_ok(addr, &["scan", "1"]);
        let settled = settled_receipts(client);
        assert_eq!(settled.len(), 1);
        assert_eq!(settled[0].actor.as_deref(), Some("console-7"));
        assert_eq!(
            settled[0].outcome,
            CommandOutcome::Applied { tick: Tick(2) }
        );

        // Every receipted subcommand takes the flag — the flag may sit
        // anywhere in the argument list, and the attribution lands on
        // each journaled CommandSettled identically, invoke included.
        for args in [
            [
                "set-parameter",
                "level-pid",
                "kp",
                "0.5",
                "--actor",
                "console-7",
            ]
            .as_slice(),
            ["force", "--actor", "console-7", "10", "9"].as_slice(),
            ["unforce", "10", "--actor", "console-7"].as_slice(),
            [
                "invoke",
                "seq",
                "advance",
                "count=1",
                "--actor",
                "console-7",
            ]
            .as_slice(),
        ] {
            let receipt: CommandReceipt = serde_json::from_value(ctl_ok(addr, args)).unwrap();
            assert_eq!(receipt.actor.as_deref(), Some("console-7"), "{args:?}");
        }
        ctl_ok(addr, &["scan", "1"]);
        for settled in settled_receipts(client) {
            assert_eq!(settled.actor.as_deref(), Some("console-7"));
        }

        // A rejected command still prints its receipt — attribution
        // included — and exits nonzero naming the CommandError.
        let output = ctl(addr, &["write", "20", "5", "--actor", "console-7"]);
        assert!(!output.status.success());
        let receipt: CommandReceipt = serde_json::from_str(&stdout(&output)).unwrap();
        assert_eq!(receipt.actor.as_deref(), Some("console-7"));
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: dcs_core::CommandError::NotWritable { point: OUT }
            }
        );
        assert!(stderr(&output).contains("not_writable"), "{output:?}");
    });
}

#[test]
fn dcs_actor_is_the_configured_default_the_flag_overrides() {
    with_monitor(|_driver, addr, _client| {
        // The environment variable attributes invocations that carry no
        // flag — a shell or service configures the identity once.
        let output = ctl_env(addr, &["write", "11", "60"], &[("DCS_ACTOR", "ops-cli")]);
        assert!(output.status.success(), "{}", stderr(&output));
        let receipt: CommandReceipt = serde_json::from_str(&stdout(&output)).unwrap();
        assert_eq!(receipt.actor.as_deref(), Some("ops-cli"));

        // The flag wins over the environment.
        let output = ctl_env(
            addr,
            &["write", "11", "61", "--actor", "console-7"],
            &[("DCS_ACTOR", "ops-cli")],
        );
        assert!(output.status.success(), "{}", stderr(&output));
        let receipt: CommandReceipt = serde_json::from_str(&stdout(&output)).unwrap();
        assert_eq!(receipt.actor.as_deref(), Some("console-7"));

        // An empty configured default is no declaration.
        let output = ctl_env(addr, &["write", "11", "62"], &[("DCS_ACTOR", "")]);
        assert!(output.status.success(), "{}", stderr(&output));
        let receipt: CommandReceipt = serde_json::from_str(&stdout(&output)).unwrap();
        assert_eq!(receipt.actor, None);
    });
}

#[test]
fn an_unattributed_submission_journals_unattributed() {
    with_monitor(|_driver, addr, client| {
        client.advance(1).unwrap();
        // No flag and no configured default: the bare-Command body
        // submits as before — the receipt and its journaled echo carry
        // no actor, never a rejection.
        let receipt: CommandReceipt =
            serde_json::from_value(ctl_ok(addr, &["write", "11", "60"])).unwrap();
        assert_eq!(receipt.actor, None);
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        ctl_ok(addr, &["scan", "1"]);
        let settled = settled_receipts(client);
        assert_eq!(settled.len(), 1);
        assert_eq!(settled[0].actor, None);
        let journal_json = serde_json::to_string(&client.journal(0).unwrap()).unwrap();
        assert!(!journal_json.contains("\"actor\""), "{journal_json}");
    });
}

#[test]
fn pair_health_prints_healthy_json_for_all_resolved_peers() {
    let active = PeerRig::start(Role::Active);
    let standby = PeerRig::start(Role::Standby);
    let other_standby = PeerRig::start(Role::Standby);
    active.client.advance(1).unwrap();
    let checkpoint = active.client.checkpoint().unwrap();
    standby.monitor.apply_checkpoint(&checkpoint).unwrap();
    other_standby.monitor.apply_checkpoint(&checkpoint).unwrap();

    for (first, second) in [(active.addr, standby.addr), (standby.addr, active.addr)] {
        let output = ctl(
            first,
            &[
                "pair-health",
                &second.to_string(),
                &other_standby.addr.to_string(),
            ],
        );
        assert!(output.status.success(), "{}", stderr(&output));
        assert!(stderr(&output).is_empty());
        let health: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(health["active"], active.addr.to_string());
        assert_eq!(health["faults"], serde_json::json!([]));
        assert!(health.get("fault_kinds").is_none());
        let decoded: dcs_monitor::PairHealth = serde_json::from_value(health.clone()).unwrap();
        assert!(decoded.fault_kinds.is_empty());
        assert!(health["fault_kinds_version"].is_u64());
    }
}

#[test]
fn pair_health_faults_exit_nonzero_with_serialized_named_kinds() {
    for (role, kind) in [
        (Role::Active, PairFaultKind::DualActive),
        (Role::Standby, PairFaultKind::NoActivePeer),
    ] {
        let first = PeerRig::start(role);
        let second = PeerRig::start(role);
        let output = ctl(first.addr, &["pair-health", &second.addr.to_string()]);
        assert!(!output.status.success(), "{output:?}");
        let health: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
        let kinds = vec![kind];
        assert_eq!(health["active"], serde_json::Value::Null);
        assert_eq!(health["fault_kinds"], serde_json::to_value(&kinds).unwrap());
        assert_eq!(health["faults"].as_array().unwrap().len(), kinds.len());
        assert!(health["fault_kinds_version"].is_u64());
        assert_eq!(
            stderr(&output),
            format!(
                "dcs-ctl: {}: pair-health faults: {}\n",
                first.addr,
                serde_json::to_string_pretty(&kinds).unwrap()
            )
        );
        for peer in [&first, &second] {
            let report: RoleReport = serde_json::from_value(ctl_ok(peer.addr, &["role"])).unwrap();
            assert_eq!(report, peer.client.role().unwrap());
            assert_eq!(report.role, role);
        }
    }
}

#[test]
fn promote_and_demote_print_role_reports_and_named_refusals() {
    let active = PeerRig::start(Role::Active);
    // The standby names its tracking source — the configured peer its
    // later demotion follows back to the active.
    let standby = PeerRig::start_tracking(Role::Standby, Some(active.addr));

    // `role` on the standby reports its convergence.
    let report: RoleReport = serde_json::from_value(ctl_ok(standby.addr, &["role"])).unwrap();
    assert_eq!(report.role, Role::Standby);
    assert_eq!(report.sync, Some(StandbySync::Unsynchronized));

    // Refusals name the SwitchError: promoting an unconverged standby,
    // promoting the settled active, demoting a standby.
    let output = ctl(standby.addr, &["promote"]);
    assert!(!output.status.success());
    assert!(stderr(&output).contains("not_converged"), "{output:?}");
    let output = ctl(active.addr, &["promote"]);
    assert!(!output.status.success());
    assert!(stderr(&output).contains("already_active"), "{output:?}");
    let output = ctl(standby.addr, &["demote"]);
    assert!(!output.status.success());
    assert!(stderr(&output).contains("not_active"), "{output:?}");
    // And demoting a field owner with no checkpoint source — nothing
    // configured, no peer that ever announced itself — is refused up
    // front rather than stranding the peer permanently unsynchronized.
    // A rig of its own: this pair's standby already announced its
    // address through the premature `promote`'s final-sync pull, so
    // `active` legitimately holds a tracking source.
    let lonely = PeerRig::start(Role::Active);
    let output = ctl(lonely.addr, &["demote"]);
    assert!(!output.status.success());
    assert!(stderr(&output).contains("no_tracking_source"), "{output:?}");
    lonely.stop();

    // A command on a non-active peer answers the not_active rejection
    // through the ordinary receipted path — invoke included.
    let output = ctl(standby.addr, &["write", "11", "5"]);
    assert!(!output.status.success());
    assert!(stderr(&output).contains("not_active"), "{output:?}");
    let output = ctl(standby.addr, &["invoke", "seq", "advance"]);
    assert!(!output.status.success());
    let receipt: CommandReceipt = serde_json::from_str(&stdout(&output)).unwrap();
    assert!(matches!(receipt.outcome, CommandOutcome::Rejected { .. }));
    assert!(stderr(&output).contains("not_active"), "{output:?}");

    // Converge the standby through the checkpoint path, then `promote`
    // prints the resulting RoleReport — promoting at the request's
    // boundary — and `scan` settles it to active.
    active.client.advance(3).unwrap();
    standby
        .monitor
        .apply_checkpoint(&active.client.checkpoint().unwrap())
        .unwrap();
    let report: RoleReport = serde_json::from_value(ctl_ok(standby.addr, &["promote"])).unwrap();
    assert_eq!(report.role, Role::Promoting);
    let report: RoleReport = serde_json::from_value(ctl_ok(standby.addr, &["role"])).unwrap();
    assert_eq!(report.role, Role::Promoting);
    ctl_ok(standby.addr, &["scan", "1"]);
    let report: RoleReport = serde_json::from_value(ctl_ok(standby.addr, &["role"])).unwrap();
    assert_eq!(report.role, Role::Active);

    // `demote` on the field owner prints its RoleReport; the next scan
    // settles it to standby.
    let report: RoleReport = serde_json::from_value(ctl_ok(standby.addr, &["demote"])).unwrap();
    assert_eq!(report.role, Role::Demoting);

    active.stop();
    standby.stop();
}

/// The journaled `RoleChanged` events of a peer's served journal: the
/// transition beside the switch's attribution — `origin` marking a
/// requested switch, `actor` the declared identity it carried.
fn role_changes(client: &MonitorClient) -> Vec<(Role, Role, Option<SwitchOrigin>, Option<String>)> {
    client
        .journal(0)
        .unwrap()
        .iter()
        .filter_map(|entry| match &entry.event {
            JournalEvent::RoleChanged {
                from,
                to,
                origin,
                actor,
            } => Some((*from, *to, *origin, actor.clone())),
            _ => None,
        })
        .collect()
}

/// Converges `standby` against `active`'s current checkpoint — the
/// promote contract's prerequisite.
fn converge(active: &PeerRig, standby: &PeerRig) {
    active.client.advance(3).unwrap();
    standby
        .monitor
        .apply_checkpoint(&active.client.checkpoint().unwrap())
        .unwrap();
}

#[test]
fn promote_and_demote_carry_the_declared_actor() {
    let active = PeerRig::start(Role::Active);
    // The standby names its tracking source — the configured peer its
    // demotions follow back to the active.
    let standby = PeerRig::start_tracking(Role::Standby, Some(active.addr));
    converge(&active, &standby);

    // `promote --actor` declares the identity the switch request
    // carries: both journaled RoleChanged entries — the request's
    // transition and the settle's — stamp it beside `origin: request`.
    let report: RoleReport =
        serde_json::from_value(ctl_ok(standby.addr, &["promote", "--actor", "console-7"])).unwrap();
    assert_eq!(report.role, Role::Promoting);
    ctl_ok(standby.addr, &["scan", "1"]);
    assert_eq!(
        role_changes(&standby.client),
        vec![
            (
                Role::Standby,
                Role::Promoting,
                Some(SwitchOrigin::Request),
                Some("console-7".to_string()),
            ),
            (
                Role::Promoting,
                Role::Active,
                Some(SwitchOrigin::Request),
                Some("console-7".to_string()),
            ),
        ]
    );

    // `DCS_ACTOR` is the configured default a flagless `demote`
    // declares; the flag overrides it — the same convention the
    // command subcommands honor.
    let output = ctl_env(standby.addr, &["demote"], &[("DCS_ACTOR", "ops-cli")]);
    assert!(output.status.success(), "{}", stderr(&output));
    let report: RoleReport = serde_json::from_str(&stdout(&output)).unwrap();
    assert_eq!(report.role, Role::Demoting);
    ctl_ok(standby.addr, &["scan", "1"]);
    converge(&active, &standby);
    let output = ctl_env(
        standby.addr,
        &["promote", "--actor", "console-7"],
        &[("DCS_ACTOR", "ops-cli")],
    );
    assert!(output.status.success(), "{}", stderr(&output));
    ctl_ok(standby.addr, &["scan", "1"]);
    // An invocation declaring neither — the env removed, no flag —
    // submits the bare request and journals unattributed.
    ctl_ok(standby.addr, &["demote"]);
    ctl_ok(standby.addr, &["scan", "1"]);

    let actors: Vec<Option<String>> = role_changes(&standby.client)
        .iter()
        .map(|(_, _, origin, actor)| {
            assert_eq!(*origin, Some(SwitchOrigin::Request));
            actor.clone()
        })
        .collect();
    assert_eq!(
        actors,
        vec![
            Some("console-7".to_string()),
            Some("console-7".to_string()),
            Some("ops-cli".to_string()),
            Some("ops-cli".to_string()),
            Some("console-7".to_string()),
            Some("console-7".to_string()),
            None,
            None,
        ]
    );

    active.stop();
    standby.stop();
}

#[test]
fn scan_runs_on_an_unpaced_monitor_and_is_refused_on_a_paced_one() {
    with_monitor(|_driver, addr, _client| {
        // `scan <n>` prints the snapshot after the last requested scan.
        let snapshot: dcs_core::TelemetrySnapshot =
            serde_json::from_value(ctl_ok(addr, &["scan", "2"])).unwrap();
        assert_eq!(snapshot.tick, Tick(2));
    });

    // A paced monitor owns its own scan schedule: `POST /scan` is
    // refused and the tool exits nonzero with the refusal.
    let driver = field_driver();
    let executor = Executor::new(&driver, point_map(), components()).unwrap();
    let monitor = Monitor::bind_paced("127.0.0.1:0", executor, signal_index()).unwrap();
    let addr = monitor.local_addr();
    thread::scope(|scope| {
        scope.spawn(|| monitor.serve());
        let output = ctl(addr, &["scan", "1"]);
        assert!(!output.status.success());
        assert!(stderr(&output).contains("refused"), "{output:?}");
        monitor.shutdown();
    });
}

#[test]
fn an_unreachable_monitor_exits_nonzero_naming_the_address() {
    // Bind once to learn a free port, then drop the listener so the
    // address refuses connections. A concurrently bound fixture can
    // legitimately take the freed port — a probe then answers — so
    // retry until a probed port stays refused.
    for _ in 0..20 {
        let addr = TcpListener::bind(("127.0.0.1", 0))
            .unwrap()
            .local_addr()
            .unwrap();
        let mut rebound = false;
        for args in [
            ["snapshot"].as_slice(),
            ["write", "11", "5"].as_slice(),
            ["invoke", "seq", "advance"].as_slice(),
            ["promote"].as_slice(),
        ] {
            let output = ctl(addr, args);
            if output.status.success() {
                rebound = true;
                break;
            }
            let stderr = stderr(&output);
            assert!(stderr.contains(&addr.to_string()), "{args:?}: {stderr}");
        }
        if !rebound {
            return;
        }
    }
    panic!(
        "twenty dead ports each answered — a concurrent listener keeps taking the freed port or the tool accepts a refused address"
    );
}

#[test]
fn malformed_arguments_fail_with_usage_never_a_panic() {
    // The address is dead on purpose: argument validation must precede
    // any connection.
    let dead = "127.0.0.1:9";
    let cases: Vec<Vec<&str>> = vec![
        vec![],
        vec![dead],
        vec![dead, "bogus"],
        vec![dead, "snapshot", "extra"],
        vec![dead, "role", "extra"],
        vec![dead, "pair-health"],
        vec![dead, "pair-health", "--actor", "op"],
        vec![dead, "pair-health", dead, "--bogus"],
        vec![dead, "pair-health", "not-an-address"],
        vec![dead, "pair-health", dead, "not-an-address"],
        vec![dead, "journal", "--since"],
        vec![dead, "journal", "--since", "abc"],
        vec![dead, "journal", "--bogus", "1"],
        vec![dead, "history"],
        vec![dead, "history", "10"],
        vec![dead, "history", "--point"],
        vec![dead, "history", "--point", "abc"],
        vec![dead, "history", "--point", "10", "--since", "x"],
        // The interface-surface reads' malformed shapes: `schema`
        // takes nothing, `events` at most one component name — a
        // flag-looking name is never a lookup.
        vec![dead, "schema", "extra"],
        vec![dead, "schema", "--actor", "op"],
        vec![dead, "events", "a", "b"],
        vec![dead, "events", "comp", "--actor", "op"],
        vec![dead, "events", "--bogus"],
        vec![dead, "resources", "a", "b"],
        vec![dead, "resources", "comp", "--actor", "op"],
        vec![dead, "resources", "--bogus"],
        vec![dead, "write"],
        vec![dead, "write", "10"],
        vec![dead, "write", "abc", "1"],
        vec![dead, "write", "10", "1", "extra"],
        // The actor flag's malformed shapes: a missing name, a repeated
        // flag, an unknown flag — all usage, never a submission.
        vec![dead, "write", "10", "1", "--actor"],
        vec![dead, "write", "10", "1", "--actor", "a", "--actor", "b"],
        vec![dead, "write", "10", "1", "--bogus", "x"],
        vec![dead, "set-parameter", "comp"],
        vec![dead, "set-parameter", "comp", "name"],
        vec![dead, "set-parameter", "comp", "name", "1", "extra"],
        vec![dead, "set-parameter", "comp", "name", "1", "--actor"],
        vec![dead, "force", "abc", "1"],
        vec![dead, "force", "10", "1", "--actor"],
        vec![dead, "unforce"],
        vec![dead, "unforce", "abc"],
        vec![dead, "unforce", "10", "--actor"],
        // invoke's malformed shapes: missing positionals, a bare or
        // `=`-less argument, an empty or repeated argument name, the
        // actor flag's missing name, an unknown flag.
        vec![dead, "invoke"],
        vec![dead, "invoke", "comp"],
        vec![dead, "invoke", "comp", "cmd", "noequals"],
        vec![dead, "invoke", "comp", "cmd", "=1"],
        vec![dead, "invoke", "seq", "advance", "count=1", "count=2"],
        vec![dead, "invoke", "comp", "cmd", "x=1", "--actor"],
        vec![dead, "invoke", "comp", "cmd", "--bogus"],
        // promote/demote's malformed shapes: a stray positional, the
        // actor flag's missing name or a repeat, an unknown flag.
        vec![dead, "promote", "extra"],
        vec![dead, "promote", "--actor"],
        vec![dead, "promote", "--actor", "a", "--actor", "b"],
        vec![dead, "promote", "--bogus"],
        vec![dead, "demote", "extra"],
        vec![dead, "demote", "--actor"],
        vec![dead, "demote", "--bogus", "x"],
        vec![dead, "snapshot", "--actor", "op"],
        vec![dead, "scan"],
        vec![dead, "scan", "abc"],
        vec![dead, "scan", "-1"],
        // An address that does not parse is malformed input too.
        vec!["not-an-address", "snapshot"],
    ];
    for args in &cases {
        let output = ctl_args(args);
        assert!(!output.status.success(), "{args:?} unexpectedly succeeded");
        let stderr = stderr(&output);
        assert!(stderr.contains("usage:"), "{args:?}: {stderr}");
        assert!(!stderr.contains("panic"), "{args:?}: {stderr}");
    }
}

#[test]
fn value_parse_errors_print_usage_against_a_live_monitor() {
    with_monitor(|_driver, addr, _client| {
        // Declared-kind parse failures are malformed input — usage, not
        // a submission: a Float point takes no "abc", a Bool point only
        // true|false, and a non-finite literal has no wire form.
        for args in [
            ["write", "10", "abc"].as_slice(),
            ["write", "13", "yes"].as_slice(),
            ["write", "10", "nan"].as_slice(),
            ["force", "10", "inf"].as_slice(),
            ["set-parameter", "level-pid", "kp", "abc"].as_slice(),
            // A declared Int argument takes no "abc" — the served
            // schema's request kinds rule the invoke parse.
            ["invoke", "seq", "advance", "count=abc"].as_slice(),
        ] {
            let output = ctl(addr, args);
            assert!(!output.status.success(), "{args:?} unexpectedly succeeded");
            let stderr = stderr(&output);
            assert!(stderr.contains("usage:"), "{args:?}: {stderr}");
            assert!(!stderr.contains("panic"), "{args:?}: {stderr}");
        }
    });
}

#[test]
fn identical_invocations_produce_identical_output() {
    // The same scripted session against two fresh rigs prints the same
    // answers — output is a function of the run, nothing else.
    let script = |addr: SocketAddr| {
        let mut outputs = Vec::new();
        for args in [
            ["snapshot"].as_slice(),
            ["scan", "2"].as_slice(),
            ["write", "11", "60"].as_slice(),
            ["write", "20", "5"].as_slice(),
            ["invoke", "seq", "advance", "count=1"].as_slice(),
            ["invoke", "seq", "advance", "count=1"].as_slice(),
            ["invoke", "seq", "bogus"].as_slice(),
            ["invoke", "seq", "reset", "--actor", "console-7"].as_slice(),
            ["scan", "1"].as_slice(),
            ["receipts"].as_slice(),
            ["journal"].as_slice(),
            ["history", "--point", "10"].as_slice(),
            ["schema"].as_slice(),
            ["events"].as_slice(),
            ["events", "seq"].as_slice(),
        ] {
            let output = ctl(addr, args);
            // Failure messages name the monitor address — an ephemeral
            // port that differs between rigs — so redact it for the
            // cross-run comparison.
            outputs.push((
                output.status.code(),
                stdout(&output),
                stderr(&output).replace(&addr.to_string(), "ADDR"),
            ));
        }
        outputs
    };
    let first = with_monitor(|_driver, addr, _client| script(addr));
    let second = with_monitor(|_driver, addr, _client| script(addr));
    assert_eq!(first, second);
}
