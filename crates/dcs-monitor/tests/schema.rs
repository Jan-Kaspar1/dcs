//! `GET /schema` and `GET /resources` — the served half of the
//! schema-driven-interface decision: every served instance's complete
//! five-category `BlockInterface`, and per-instance live resource values
//! — measurements and state with quality, current configuration, command
//! availability or its named refusal, recently emitted events — joined
//! from the published read model the snapshot serves, never under the
//! executor lock. Covers `MonitorClient`'s typed accessors and the
//! documents' serde-additive compatibility.

use dcs_blocks::{Pid, PidConfig};
use dcs_core::{
    Command, CommandArgument, CommandAvailability, CommandDecl, CommandError, CommandOutcome,
    CommandState, ComponentDescriptor, ComponentResources, Direction, DriverDiagnostics, EventDecl,
    EventField, EventFieldKind, EventRetention, IoDriver, IoError, JournalEvent, PointId,
    PortDescriptor, PortRole, ResourceView, Sample, SchemaView, TelemetrySnapshot, Tick, Value,
    ValueKind,
};
use dcs_model::{PlantModel, SignalIndex};
use dcs_monitor::{Monitor, MonitorClient};
use dcs_runtime::{
    Component, ComponentIo, ComponentIoExt, DECLARED_COMMAND_GAP, Executor, IoRequirement,
    PointMap, StepError,
};
use std::collections::{BTreeMap, BTreeSet, HashMap};
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
            snapshot_calls: AtomicU64::new(0),
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

    /// `Executor::snapshot` asks the driver for transport diagnostics
    /// exactly once per construction — counting the calls instruments
    /// snapshot materialization without touching the executor.
    fn diagnostics(&self) -> Option<DriverDiagnostics> {
        self.snapshot_calls.fetch_add(1, Ordering::Relaxed);
        None
    }
}

// The rig's points, matching the shared faceplates fixture: `SP` is the
// model-declared writable internal `In` point carrying the operator
// setpoint; `TRIPPED` the `Out` `Bool` the drive's status port writes;
// `DEBUG` a mapped `Out` point no component writes; the rest are field
// points the stub driver serves.
const PV: PointId = PointId(10);
const SP: PointId = PointId(11);
const OUT: PointId = PointId(20);
const TRIPPED: PointId = PointId(21);
const DEBUG: PointId = PointId(30);
const FRAGILE_OUT: PointId = PointId(31);
const RUN: PointId = PointId(40);

/// A stub kind exercising the surface no `dcs-blocks` kind declares yet:
/// a `Status`-roled port (`state`), a kind-declared command and event
/// (`Declared` provenance), and an unwired optional port the descriptor
/// carries without a bound point.
struct Drive;

impl Component for Drive {
    fn name(&self) -> &str {
        "drive"
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        vec![
            IoRequirement::input::<f64>("run", RUN),
            IoRequirement::output::<bool>("tripped", TRIPPED),
        ]
    }

    fn step(&mut self, io: &dyn ComponentIo, _tick: Tick) -> Result<(), StepError> {
        let run = io.read_typed::<f64>(RUN)?;
        io.write_typed(TRIPPED, run.value > 50.0)?;
        Ok(())
    }

    fn describe(&self) -> ComponentDescriptor {
        ComponentDescriptor {
            name: "drive".to_string(),
            kind: "drive".to_string(),
            label: "drive".to_string(),
            ports: vec![
                PortDescriptor {
                    name: "run".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: Some(PortRole::Setpoint),
                    point: None,
                },
                PortDescriptor {
                    name: "tripped".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Status),
                    point: None,
                },
                // Declared but never wired — no io_requirement resolves
                // it, so the served descriptor annotates it `point:
                // None`.
                PortDescriptor {
                    name: "remote".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: None,
                    point: None,
                },
            ],
            parameters: Vec::new(),
            commands: vec![CommandDecl {
                name: "stroke_test".to_string(),
                request: vec![CommandArgument {
                    name: "ticks".to_string(),
                    kind: ValueKind::Int,
                }],
                availability: CommandAvailability::KindDeclared,
            }],
            events: vec![EventDecl {
                name: "stroke_complete".to_string(),
                payload: vec![EventField {
                    name: "ticks".to_string(),
                    kind: EventFieldKind::Value(ValueKind::Int),
                    optional: false,
                }],
                retention: EventRetention::Journal,
            }],
        }
    }
}

/// A component whose `step` always fails — the `step_failed` event path.
struct Fragile;

impl Component for Fragile {
    fn name(&self) -> &str {
        "fragile"
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        vec![IoRequirement::output::<f64>("out", FRAGILE_OUT)]
    }

    fn step(&mut self, _io: &dyn ComponentIo, _tick: Tick) -> Result<(), StepError> {
        Err("the fragile step always fails".into())
    }

    fn describe(&self) -> ComponentDescriptor {
        ComponentDescriptor {
            name: "fragile".to_string(),
            kind: "fragile".to_string(),
            label: "fragile".to_string(),
            ports: vec![PortDescriptor {
                name: "out".to_string(),
                direction: Direction::Out,
                kind: ValueKind::Float,
                role: None,
                point: None,
            }],
            parameters: Vec::new(),
            commands: Vec::new(),
            events: Vec::new(),
        }
    }
}

/// The model fixture behind the monitor — the same one the faceplate
/// tests serve, covering every point this rig maps.
const MODEL: &str = include_str!("../fixtures/faceplates.json");

fn signal_index() -> SignalIndex {
    PlantModel::load(MODEL).unwrap().signal_index()
}

fn point_map() -> PointMap {
    PointMap::new()
        .with_point(PV, Direction::In, ValueKind::Float)
        .with_writable_internal(SP, Direction::In, ValueKind::Float, Value::Float(50.0))
        .with_point(OUT, Direction::Out, ValueKind::Float)
        .with_point(TRIPPED, Direction::Out, ValueKind::Bool)
        .with_point(DEBUG, Direction::Out, ValueKind::Float)
        .with_point(FRAGILE_OUT, Direction::Out, ValueKind::Float)
        .with_point(RUN, Direction::In, ValueKind::Float)
}

fn components() -> Vec<Box<dyn Component>> {
    vec![
        Box::new(
            Pid::new(
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
            .unwrap(),
        ),
        Box::new(Drive),
        Box::new(Fragile),
    ]
}

/// Builds the rig and runs `body` against a serving monitor; the server
/// is shut down before the driver's borrow ends — a failing assertion
/// must not deadlock the scope join.
fn with_monitor<T>(body: impl FnOnce(&StubDriver, &MonitorClient) -> T) -> T {
    let driver = StubDriver::new(&[
        (PV, Value::Float(0.0)),
        (OUT, Value::Float(0.0)),
        (TRIPPED, Value::Bool(false)),
        (DEBUG, Value::Float(0.0)),
        (FRAGILE_OUT, Value::Float(0.0)),
        (RUN, Value::Float(0.0)),
    ]);
    let executor = Executor::new(&driver, point_map(), components()).unwrap();
    let monitor = Monitor::bind("127.0.0.1:0", executor, signal_index()).unwrap();
    let client = MonitorClient::new(monitor.local_addr());
    let result = thread::scope(|scope| {
        scope.spawn(|| monitor.serve());
        let result =
            std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| body(&driver, &client)));
        monitor.shutdown();
        result
    });
    result.unwrap_or_else(|panic| std::panic::resume_unwind(panic))
}

/// Runs `body` while `monitor` serves — the paced variant of
/// `with_monitor` for the off-lock proof.
fn serving<T>(monitor: &Monitor<'_>, body: impl FnOnce() -> T) -> T {
    let result = thread::scope(|scope| {
        scope.spawn(|| monitor.serve());
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(body));
        monitor.shutdown();
        result
    });
    result.unwrap_or_else(|panic| std::panic::resume_unwind(panic))
}

fn write_value(point: PointId, value: f64) -> Command {
    Command::WriteValue {
        point,
        kind: ValueKind::Float,
        value: Value::Float(value),
    }
}

fn component<'a>(view: &'a ResourceView, name: &str) -> &'a ComponentResources {
    view.components
        .iter()
        .find(|entry| entry.name == name)
        .unwrap_or_else(|| panic!("no resource entry for {name}"))
}

fn command_state<'a>(component: &'a ComponentResources, name: &str) -> &'a CommandState {
    component
        .commands
        .iter()
        .find(|command| command.name == name)
        .unwrap_or_else(|| panic!("{name} missing from {}'s commands", component.name))
}

fn sample_at(snapshot: &TelemetrySnapshot, point: PointId) -> Option<Sample> {
    snapshot
        .points
        .iter()
        .find(|telemetry| telemetry.point == point)
        .and_then(|telemetry| telemetry.sample)
}

fn parameter_values<'a>(
    snapshot: &'a TelemetrySnapshot,
    name: &str,
) -> &'a BTreeMap<String, Value> {
    &snapshot
        .parameters
        .iter()
        .find(|entry| entry.name == name)
        .unwrap_or_else(|| panic!("no parameters entry for {name}"))
        .values
}

#[test]
fn schema_serves_every_instances_five_category_interface() {
    with_monitor(|_driver, client| {
        client.advance(1).unwrap();
        let snapshot = client.snapshot().unwrap();
        let schema: SchemaView = client.schema().unwrap();

        // The view derives from the same publication the snapshot
        // serves — the stamped seq and tick identify the read model.
        assert_eq!(schema.publication, snapshot.publication.unwrap().published);
        assert_eq!(schema.tick, snapshot.tick);

        // One interface per served instance, in scan order, keyed by the
        // instance name — every kind the run instantiated is covered.
        assert_eq!(schema.interfaces.len(), snapshot.descriptors.len());
        let kinds: BTreeSet<&str> = schema
            .interfaces
            .iter()
            .map(|entry| entry.interface.kind.as_str())
            .collect();
        assert_eq!(kinds, BTreeSet::from(["pid", "drive", "fragile"]));

        let signals = signal_index();
        for (entry, descriptor) in schema.interfaces.iter().zip(&snapshot.descriptors) {
            assert_eq!(entry.name, descriptor.name);
            // The served interface is the descriptor's derivation
            // completed with the serving-layer `unit` annotation the
            // signal index carries.
            let mut expected = descriptor.interface();
            for measurement in &mut expected.measurements {
                measurement.unit = measurement
                    .point
                    .and_then(|point| signals.get(point))
                    .and_then(|signal| signal.unit.clone());
            }
            assert_eq!(
                entry.interface, expected,
                "{} interface drifted",
                entry.name
            );
        }
        // Serving-layer annotation applied: `sp`'s bound point's signal
        // declares `%`; `pv`'s declares `degC`.
        let pid = schema
            .interfaces
            .iter()
            .find(|entry| entry.name == "level-pid")
            .unwrap();
        let sp = pid
            .interface
            .measurements
            .iter()
            .find(|measurement| measurement.name == "sp")
            .unwrap();
        assert_eq!(sp.point, Some(SP));
        assert_eq!(sp.unit.as_deref(), Some("%"));

        // The wire document carries the complete five-category shape on
        // every interface — the shape the drift pin checks per kind.
        let (status, body) = client.request("GET", "/schema", None).unwrap();
        assert_eq!(status, 200, "{body}");
        let document: serde_json::Value = serde_json::from_str(&body).unwrap();
        for interface in document["interfaces"].as_array().unwrap() {
            for key in [
                "version",
                "kind",
                "measurements",
                "configuration",
                "state",
                "commands",
                "events",
            ] {
                assert!(
                    interface["interface"].get(key).is_some(),
                    "served interface lacks {key:?}: {interface}"
                );
            }
        }
    });
}

#[test]
fn resources_join_the_same_publication_the_snapshot_serves() {
    with_monitor(|_driver, client| {
        client.advance(1).unwrap();
        // An accepted command settles at the next scan's boundary; a
        // statically invalid one is refused and journaled at once —
        // between scans, ahead of the stamped publication.
        let receipt = client.command(&write_value(SP, 42.0)).unwrap();
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        client.advance(1).unwrap();
        let refused = client.command(&write_value(RUN, 1.0)).unwrap();
        assert_eq!(
            refused.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::NotWritable { point: RUN }
            }
        );

        let snapshot = client.snapshot().unwrap();
        let view: ResourceView = client.resources().unwrap();
        assert_eq!(
            view.publication,
            snapshot.publication.unwrap().published,
            "resource view and snapshot must name one publication"
        );
        assert_eq!(view.tick, snapshot.tick);
        assert_eq!(view.components.len(), snapshot.descriptors.len());
        for (resources, descriptor) in view.components.iter().zip(&snapshot.descriptors) {
            assert_eq!(resources.name, descriptor.name);
            assert_eq!(resources.kind, descriptor.kind);

            // Collections are parallel to the instance's interface:
            // names in order, values from the same publication's
            // points and parameters sections.
            let interface = descriptor.interface();
            for (reading, measurement) in resources.measurements.iter().zip(&interface.measurements)
            {
                assert_eq!(reading.name, measurement.name);
                assert_eq!(reading.point, measurement.point);
                assert_eq!(
                    reading.sample,
                    measurement
                        .point
                        .and_then(|point| sample_at(&snapshot, point)),
                    "{}.{} sample drifted",
                    resources.name,
                    reading.name
                );
            }
            for (reading, property) in resources.state.iter().zip(&interface.state) {
                assert_eq!(reading.name, property.name);
                assert_eq!(reading.point, property.point);
                assert_eq!(
                    reading.sample,
                    property.point.and_then(|point| sample_at(&snapshot, point))
                );
            }
            for (entry, property) in resources.configuration.iter().zip(&interface.configuration) {
                assert_eq!(entry.name, property.name);
                assert_eq!(
                    entry.value,
                    parameter_values(&snapshot, &resources.name)
                        .get(&property.name)
                        .copied()
                );
            }
            for (state, spec) in resources.commands.iter().zip(&interface.commands) {
                assert_eq!(state.name, spec.name);
                assert_eq!(state.point, spec.point);
            }
        }

        // Command availability reads the availability rule live. The
        // writable setpoint admits writes; the field-served `pv` and
        // `run` refuse `not_writable`; the declared command reports the
        // runtime's dispatch gap; the unwired `remote` port's commands
        // refuse as unbound.
        let pid = component(&view, "level-pid");
        assert!(command_state(pid, "write_value:sp").available);
        assert!(command_state(pid, "set_parameter:kp").available);
        let pv_write = command_state(pid, "write_value:pv");
        assert!(!pv_write.available);
        assert_eq!(
            pv_write.refusal.as_deref(),
            Some(CommandError::NotWritable { point: PV }.to_string().as_str())
        );

        let drive = component(&view, "drive");
        assert!(!command_state(drive, "write_value:run").available);
        assert_eq!(
            command_state(drive, "write_value:run").refusal.as_deref(),
            Some(
                CommandError::NotWritable { point: RUN }
                    .to_string()
                    .as_str()
            )
        );
        let remote = command_state(drive, "write_value:remote");
        assert!(!remote.available);
        assert_eq!(remote.point, None);
        assert!(
            remote
                .refusal
                .as_deref()
                .is_some_and(|reason| reason.contains("unbound")),
            "{remote:?}"
        );
        let declared = command_state(drive, "stroke_test");
        assert!(!declared.available);
        let dispatch_gap = CommandError::CommandRefused {
            component: "drive".to_string(),
            command: "stroke_test".to_string(),
            reason: DECLARED_COMMAND_GAP.to_string(),
        }
        .to_string();
        assert_eq!(declared.refusal.as_deref(), Some(dispatch_gap.as_str()));

        // The live readings: the applied setpoint write shows in `sp`'s
        // sample; `tripped`'s status reads the scan's `Bool` write with
        // quality; the unwired port and the never-written `Out` points
        // report `sample: None`.
        let sp = pid
            .measurements
            .iter()
            .find(|reading| reading.name == "sp")
            .unwrap();
        assert_eq!(
            sp.sample.unwrap().value,
            Value::Float(42.0),
            "the applied write rides the bound point's sample"
        );
        assert_eq!(
            drive
                .state
                .iter()
                .find(|reading| reading.name == "tripped")
                .unwrap()
                .sample
                .unwrap()
                .value,
            Value::Bool(false)
        );
        let remote_reading = drive
            .measurements
            .iter()
            .find(|reading| reading.name == "remote")
            .unwrap();
        assert_eq!(remote_reading.point, None);
        assert_eq!(remote_reading.sample, None);
        assert_eq!(
            component(&view, "fragile")
                .measurements
                .iter()
                .find(|reading| reading.name == "out")
                .unwrap()
                .sample,
            None
        );

        // Recently emitted events: the applied setpoint write's settled
        // receipt attributes to the instance through its bound point;
        // the between-scans refusal attributes to `drive` the moment it
        // journaled; `fragile`'s step failures attribute by name; the
        // first-observation quality transitions attribute per bound
        // point.
        assert!(pid.events.iter().any(|entry| matches!(
            &entry.event,
            JournalEvent::CommandSettled { receipt }
                if receipt.command == write_value(SP, 42.0)
                    && matches!(receipt.outcome, CommandOutcome::Applied { .. })
        )));
        assert!(drive.events.iter().any(|entry| matches!(
            &entry.event,
            JournalEvent::CommandSettled { receipt }
                if receipt.command == write_value(RUN, 1.0)
        )));
        assert!(
            component(&view, "fragile")
                .events
                .iter()
                .any(|entry| matches!(
                    &entry.event,
                    JournalEvent::StepFailed { component, .. } if component == "fragile"
                ))
        );
        assert!(drive.events.iter().any(|entry| matches!(
            &entry.event,
            JournalEvent::QualityChanged { point, .. } if *point == TRIPPED
        )));

        // An entry attributed to no instance — a command refused on an
        // unbound point — reaches the journal but no component's events.
        let receipt = client.command(&write_value(PointId(99), 0.0)).unwrap();
        assert!(matches!(receipt.outcome, CommandOutcome::Rejected { .. }));
        let view = client.resources().unwrap();
        assert!(client.journal(0).unwrap().iter().any(|entry| matches!(
            &entry.event,
            JournalEvent::CommandSettled { receipt } if receipt.command == write_value(PointId(99), 0.0)
        )));
        for entry in &view.components {
            assert!(!entry.events.iter().any(|entry| matches!(
                &entry.event,
                JournalEvent::CommandSettled { receipt } if receipt.command == write_value(PointId(99), 0.0)
            )));
        }
    });
}

#[test]
fn monitor_client_serves_the_typed_schema_and_resource_views() {
    with_monitor(|_driver, client| {
        client.advance(1).unwrap();
        // The typed accessors decode the served documents; serializing
        // the decoded views back re-reads identically — the wire
        // round-trip both directions.
        let schema = client.schema().unwrap();
        assert_eq!(
            serde_json::from_str::<SchemaView>(&serde_json::to_string(&schema).unwrap()).unwrap(),
            schema
        );
        let resources = client.resources().unwrap();
        assert_eq!(
            serde_json::from_str::<ResourceView>(&serde_json::to_string(&resources).unwrap())
                .unwrap(),
            resources
        );
    });
}

#[test]
fn served_documents_tolerate_new_categories_and_older_readers() {
    with_monitor(|_driver, client| {
        client.advance(1).unwrap();

        // A newer document carrying categories this version does not
        // know still parses — unknown keys at every level are ignored.
        for (path, document) in ["/schema", "/resources"].map(|path| {
            let (status, body) = client.request("GET", path, None).unwrap();
            assert_eq!(status, 200, "{body}");
            (
                path,
                serde_json::from_str::<serde_json::Value>(&body).unwrap(),
            )
        }) {
            let mut document = document;
            let root = document.as_object_mut().unwrap();
            root.insert(
                "future_section".to_string(),
                serde_json::json!({"anything": true}),
            );
            for entry in root
                .values_mut()
                .flat_map(|value| value.as_array_mut().into_iter().flatten())
            {
                entry
                    .as_object_mut()
                    .unwrap()
                    .insert("future_category".to_string(), serde_json::json!([]));
            }
            match path {
                "/schema" => {
                    let view: SchemaView = serde_json::from_value(document).unwrap();
                    assert_eq!(view.interfaces.len(), 3);
                }
                "/resources" => {
                    let view: ResourceView = serde_json::from_value(document).unwrap();
                    assert_eq!(view.components.len(), 3);
                }
                _ => unreachable!(),
            }
        }

        // And a document predating a category still parses — a missing
        // collection reads empty.
        let (status, body) = client.request("GET", "/resources", None).unwrap();
        assert_eq!(status, 200, "{body}");
        let mut document: serde_json::Value = serde_json::from_str(&body).unwrap();
        for entry in document["components"].as_array_mut().unwrap() {
            entry.as_object_mut().unwrap().remove("events");
        }
        let view: ResourceView = serde_json::from_value(document).unwrap();
        for entry in &view.components {
            assert!(entry.events.is_empty());
        }
    });
}

#[test]
fn schema_and_resource_reads_never_hold_the_executor_lock() {
    let driver = StubDriver::new(&[
        (PV, Value::Float(0.0)),
        (OUT, Value::Float(0.0)),
        (TRIPPED, Value::Bool(false)),
        (DEBUG, Value::Float(0.0)),
        (FRAGILE_OUT, Value::Float(0.0)),
        (RUN, Value::Float(0.0)),
    ]);
    let executor = Executor::new(&driver, point_map(), components()).unwrap();
    let monitor = Monitor::bind_paced("127.0.0.1:0", executor, signal_index()).unwrap();
    let addr = monitor.local_addr();
    serving(&monitor, || {
        // Bind materialized exactly one snapshot — the seed publication.
        assert_eq!(driver.snapshot_calls.load(Ordering::Relaxed), 1);

        // Two readers that connect, issue their requests, and go silent
        // without reading a byte of the response.
        let mut stalled = TcpStream::connect(addr).unwrap();
        stalled
            .write_all(b"GET /schema HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            .unwrap();
        let mut stalled2 = TcpStream::connect(addr).unwrap();
        stalled2
            .write_all(b"GET /resources HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            .unwrap();

        // The paced loop's scans complete on schedule while they stall —
        // no part of serving either view joins the executor lock.
        let deadline = Instant::now() + Duration::from_secs(5);
        for _ in 0..12 {
            monitor.paced_scan().unwrap();
        }
        assert!(
            Instant::now() < deadline,
            "the paced loop stalled behind a silent reader"
        );

        // Reads around them still materialized no snapshot: one per
        // completed scan, never one per request — the served copies are
        // the published read models.
        let client = MonitorClient::new(addr);
        for _ in 0..8 {
            client.schema().unwrap();
            client.resources().unwrap();
        }
        assert_eq!(
            driver.snapshot_calls.load(Ordering::Relaxed),
            13,
            "schema/resource reads must serve published copies"
        );

        // The stalled responses waited on the wire, complete and correct.
        for socket in [&mut stalled, &mut stalled2] {
            socket
                .set_read_timeout(Some(Duration::from_secs(5)))
                .unwrap();
            let mut buf = Vec::new();
            socket.read_to_end(&mut buf).unwrap();
            let response = String::from_utf8_lossy(&buf);
            assert!(
                response
                    .lines()
                    .next()
                    .is_some_and(|line| line.contains("200")),
                "{response}"
            );
        }
    });
}
