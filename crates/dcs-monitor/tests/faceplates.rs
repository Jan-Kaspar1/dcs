//! End-to-end tests for the descriptor-driven component faceplates the
//! monitoring page renders: the served snapshot joins each descriptor
//! port to its bound point, the page carries the faceplate markup and
//! the writable-only affordance logic, and a command on a point the
//! model did not mark writable is refused with a rejection visible on
//! the journal surface — all driven over TCP through the in-process
//! `MonitorClient`.

use dcs_core::{
    Command, CommandError, CommandOutcome, ComponentDescriptor, Direction, IoDriver, IoError,
    JournalEvent, ParameterDescriptor, ParameterRange, PointId, PortDescriptor, PortRole, Sample,
    TelemetrySnapshot, Tick, Value, ValueKind,
};
use dcs_model::{PlantModel, SignalIndex};
use dcs_monitor::{Monitor, MonitorClient};
use dcs_runtime::{
    Component, ComponentIo, ComponentIoExt, Executor, IoRequirement, PointMap, StepError,
};
use std::collections::HashMap;
use std::sync::Mutex;
use std::thread;

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

// The rig's points. `SP` is the model-declared writable point — a
// channel-less internal `In` point the operator writes through the
// command path; the rest are field points the stub driver serves.
const PV: PointId = PointId(10);
const SP: PointId = PointId(11);
const OUT: PointId = PointId(20);
const ALARM: PointId = PointId(21);
const DBG: PointId = PointId(30);
const PLAIN_OUT: PointId = PointId(31);
const PLAIN_IN: PointId = PointId(40);

/// A component with a custom descriptor — role hints on `pv`, `sp`,
/// `out`, and `alarm`; `dbg` deliberately unhinted; `aux` a descriptor
/// port no declared I/O requirement backs, exercising the unwired
/// degrade. Its step is a plain proportional law: `out = sp - pv`,
/// `alarm = pv > sp`.
struct Regulator;

impl Component for Regulator {
    fn name(&self) -> &str {
        "lic-42"
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        vec![
            IoRequirement::input::<f64>("pv", PV),
            IoRequirement::input::<f64>("sp", SP),
            IoRequirement::output::<f64>("out", OUT),
            IoRequirement::output::<bool>("alarm", ALARM),
            IoRequirement::output::<f64>("dbg", DBG),
        ]
    }

    fn step(&mut self, io: &dyn ComponentIo, _tick: Tick) -> Result<(), StepError> {
        let pv = io.read_typed::<f64>(PV)?;
        let sp = io.read_typed::<f64>(SP)?;
        io.write_typed(OUT, sp.value - pv.value)?;
        io.write_typed(ALARM, pv.value > sp.value)?;
        io.write_typed(DBG, pv.value)?;
        Ok(())
    }

    fn describe(&self) -> ComponentDescriptor {
        ComponentDescriptor {
            name: self.name().to_string(),
            kind: "regulator".to_string(),
            label: "Level control".to_string(),
            ports: vec![
                PortDescriptor {
                    name: "pv".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: Some(PortRole::ProcessValue),
                    point: None,
                },
                PortDescriptor {
                    name: "sp".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: Some(PortRole::Setpoint),
                    point: None,
                },
                PortDescriptor {
                    name: "out".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Float,
                    role: Some(PortRole::Output),
                    point: None,
                },
                PortDescriptor {
                    name: "alarm".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Status),
                    point: None,
                },
                PortDescriptor {
                    name: "dbg".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Float,
                    role: None,
                    point: None,
                },
                // No declared requirement named `aux`: the serving layer
                // finds no binding for it and the port stays unwired.
                PortDescriptor {
                    name: "aux".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: None,
                    point: None,
                },
            ],
            parameters: vec![
                ParameterDescriptor {
                    name: "gain".to_string(),
                    kind: ValueKind::Float,
                    range: Some(ParameterRange {
                        min: Value::Float(0.0),
                        max: Value::Float(10.0),
                    }),
                },
                ParameterDescriptor {
                    name: "enabled".to_string(),
                    kind: ValueKind::Bool,
                    range: None,
                },
            ],
        }
    }
}

/// A component with no `describe` override — the kind-without-a-custom-
/// descriptor case the page degrades to a generic faceplate for.
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

/// The model fixture behind the monitor: its `io_points` and `signals`
/// give the rig's points names, units, and the writable mark.
const MODEL: &str = include_str!("../fixtures/faceplates.json");

fn signal_index() -> SignalIndex {
    PlantModel::load(MODEL).unwrap().signal_index()
}

fn write_value(point: PointId, kind: ValueKind, value: Value) -> Command {
    Command::WriteValue { point, kind, value }
}

/// Builds the rig and runs `body` against a serving monitor; the server
/// is shut down before the driver's borrow ends.
fn with_monitor<T>(body: impl FnOnce(&StubDriver, &MonitorClient) -> T) -> T {
    let driver = StubDriver::new(&[
        (PV, Value::Float(0.0)),
        (OUT, Value::Float(0.0)),
        (ALARM, Value::Bool(false)),
        (DBG, Value::Float(0.0)),
        (PLAIN_OUT, Value::Float(0.0)),
        (PLAIN_IN, Value::Float(0.0)),
    ]);
    let map = PointMap::new()
        .with_point(PV, Direction::In, ValueKind::Float)
        // The model-declared command surface: the internal `In` point
        // carrying the operator setpoint, seeded at its declared initial.
        .with_writable_internal(SP, Direction::In, ValueKind::Float, Value::Float(50.0))
        .with_point(OUT, Direction::Out, ValueKind::Float)
        .with_point(ALARM, Direction::Out, ValueKind::Bool)
        .with_point(DBG, Direction::Out, ValueKind::Float)
        .with_point(PLAIN_OUT, Direction::Out, ValueKind::Float)
        .with_point(PLAIN_IN, Direction::In, ValueKind::Float);
    let executor = Executor::new(&driver, map, vec![Box::new(Regulator), Box::new(Plain)]).unwrap();
    let monitor = Monitor::bind("127.0.0.1:0", executor, signal_index()).unwrap();
    let client = MonitorClient::new(monitor.local_addr());
    let result = thread::scope(|scope| {
        scope.spawn(|| monitor.serve());
        // A failing assertion must not deadlock the scope join: catch the
        // panic so the server is always shut down before it propagates.
        let result =
            std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| body(&driver, &client)));
        monitor.shutdown();
        result
    });
    result.unwrap_or_else(|panic| std::panic::resume_unwind(panic))
}

fn descriptor<'s>(snapshot: &'s TelemetrySnapshot, name: &str) -> &'s ComponentDescriptor {
    snapshot
        .descriptors
        .iter()
        .find(|descriptor| descriptor.name == name)
        .unwrap_or_else(|| panic!("no descriptor for {name}"))
}

fn bound_point(descriptor: &ComponentDescriptor, port: &str) -> Option<PointId> {
    descriptor
        .ports
        .iter()
        .find(|p| p.name == port)
        .unwrap_or_else(|| panic!("{} has no port {port}", descriptor.name))
        .point
}

fn telemetry(snapshot: &TelemetrySnapshot, point: PointId) -> &dcs_core::PointTelemetry {
    snapshot
        .points
        .iter()
        .find(|telemetry| telemetry.point == point)
        .unwrap_or_else(|| panic!("no telemetry for {point:?}"))
}

#[test]
fn descriptor_ports_join_to_their_bound_points() {
    with_monitor(|_driver, client| {
        let snapshot = client.snapshot().unwrap();
        let regulator = descriptor(&snapshot, "lic-42");
        // The wiring metadata the page consumes: every descriptor port
        // carries the point its declared I/O bound.
        assert_eq!(bound_point(regulator, "pv"), Some(PV));
        assert_eq!(bound_point(regulator, "sp"), Some(SP));
        assert_eq!(bound_point(regulator, "out"), Some(OUT));
        assert_eq!(bound_point(regulator, "alarm"), Some(ALARM));
        assert_eq!(bound_point(regulator, "dbg"), Some(DBG));
        // A descriptor port no declared requirement backs stays unwired.
        assert_eq!(bound_point(regulator, "aux"), None);

        // The default-derived descriptor of the kind without a custom
        // `describe` is wired the same way.
        let plain = descriptor(&snapshot, "plain");
        assert_eq!(bound_point(plain, "in"), Some(PLAIN_IN));
        assert_eq!(bound_point(plain, "out"), Some(PLAIN_OUT));

        // The served payload serde-roundtrips with the bound points.
        let json = serde_json::to_string(&snapshot).unwrap();
        assert_eq!(
            serde_json::from_str::<TelemetrySnapshot>(&json).unwrap(),
            snapshot
        );
    });
}

#[test]
fn live_values_land_on_the_role_hinted_ports() {
    with_monitor(|driver, client| {
        driver.write(PV, Value::Float(3.5)).unwrap();
        driver.write(PLAIN_IN, Value::Float(7.0)).unwrap();
        let snapshot = client.advance(1).unwrap();

        // The join the page performs: descriptor port → bound point →
        // the snapshot's live sample for that point.
        let regulator = descriptor(&snapshot, "lic-42");
        let live = |role| {
            let port = regulator
                .ports
                .iter()
                .find(|port| port.role == Some(role))
                .unwrap();
            telemetry(&snapshot, port.point.unwrap()).sample.unwrap()
        };
        assert_eq!(live(PortRole::ProcessValue).value, Value::Float(3.5));
        // The setpoint port reads the internal point's declared initial.
        assert_eq!(live(PortRole::Setpoint).value, Value::Float(50.0));
        // The driven output carries what the component wrote.
        assert_eq!(live(PortRole::Output).value, Value::Float(46.5));
        assert_eq!(live(PortRole::Status).value, Value::Bool(false));

        // The generic component's wired ports show their values too.
        let plain = descriptor(&snapshot, "plain");
        let input = bound_point(plain, "in").unwrap();
        let output = bound_point(plain, "out").unwrap();
        assert_eq!(
            telemetry(&snapshot, input).sample.unwrap().value,
            Value::Float(7.0)
        );
        assert_eq!(
            telemetry(&snapshot, output).sample.unwrap().value,
            Value::Float(7.0)
        );
    });
}

#[test]
fn page_serves_faceplate_markup_and_writable_only_affordances() {
    with_monitor(|_driver, client| {
        let page = client.page().unwrap();
        // One faceplate section per component instance, rendered
        // generically from the snapshot's descriptors.
        for needle in [
            "id=\"faceplates\"",
            "class=\\\"faceplate",
            "function faceplateMarkup(",
            "function isGeneric(",
            // Role-hinted elements and the conventional order.
            "process_value",
            "setpoint:",
            "output:",
            "port.role === \"status\"",
            // Ports join to live points through the served binding.
            "port.point",
            // The generic degrade: name + diagnostics + wired points.
            "\"faceplate\" + (generic ? \" generic\" : \"\")",
            "function portTable(",
            "function diagnosticsMarkup(",
            "function parameterTable(",
        ] {
            assert!(page.contains(needle), "page lacks {needle}");
        }
        // Command affordances exist only behind the metadata's writable
        // mark — the gate and the fallback are both in the page.
        for needle in [
            "function commandAffordance(",
            "!meta.writable",
            "class=\\\"command\\\"",
            "input.command-value",
            "write_value",
        ] {
            assert!(page.contains(needle), "page lacks {needle}");
        }
        assert!(!page.contains("src="), "page references external assets");
    });
}

#[test]
fn writable_point_offers_affordance_unmarked_point_does_not() {
    with_monitor(|_driver, client| {
        // The metadata the page gates on: the model marks the setpoint
        // point writable; the field input and the outputs are unmarked.
        let index = client.signals().unwrap();
        assert!(index.get(SP).unwrap().writable);
        assert!(!index.get(PV).unwrap().writable);
        assert!(!index.get(OUT).unwrap().writable);
        // A bound point no signal sources — the generic component's
        // output — still indexes, unmarked and display-only.
        let spare = index.get(PLAIN_OUT).unwrap();
        assert_eq!(spare.signal, None);
        assert!(!spare.writable);
    });
}

#[test]
fn writable_command_applies_unwritable_rejection_is_visible() {
    with_monitor(|driver, client| {
        driver.write(PV, Value::Float(3.5)).unwrap();
        client.advance(1).unwrap();

        // A command on the writable setpoint point is accepted and
        // applies at the next scan's boundary — the path the page's
        // affordance takes.
        let command = write_value(SP, ValueKind::Float, Value::Float(55.0));
        let receipt = client.command(&command).unwrap();
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Accepted {
                apply_tick: Tick(2)
            }
        );
        let snapshot = client.advance(1).unwrap();
        assert_eq!(
            telemetry(&snapshot, SP).sample.unwrap().value,
            Value::Float(55.0)
        );
        // And the commanded setpoint drives the role-hinted output port.
        let regulator = descriptor(&snapshot, "lic-42");
        let output = regulator
            .ports
            .iter()
            .find(|port| port.role == Some(PortRole::Output))
            .unwrap();
        assert_eq!(
            telemetry(&snapshot, output.point.unwrap())
                .sample
                .unwrap()
                .value,
            Value::Float(51.5)
        );

        // A command on a point the model did not mark writable is
        // refused at submission — never silently accepted — and the
        // rejection lands on the journal surface the page polls.
        let rejection = client
            .command(&write_value(PV, ValueKind::Float, Value::Float(9.0)))
            .unwrap();
        assert_eq!(
            rejection.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::NotWritable { point: PV }
            }
        );
        let journal = client.journal(0).unwrap();
        assert!(
            journal.iter().any(|entry| matches!(
                &entry.event,
                JournalEvent::CommandSettled { receipt }
                    if receipt.outcome
                        == CommandOutcome::Rejected {
                            reason: CommandError::NotWritable { point: PV }
                        }
            )),
            "the not_writable rejection is journaled"
        );

        // An `Out` point — the role-hinted driven output — is never a
        // command target either.
        let rejection = client
            .command(&write_value(OUT, ValueKind::Float, Value::Float(9.0)))
            .unwrap();
        assert_eq!(
            rejection.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::NotWritable { point: OUT }
            }
        );
    });
}

#[test]
fn added_payloads_serde_roundtrip() {
    // The added field: a port descriptor carrying its bound point.
    let port = PortDescriptor {
        name: "sp".to_string(),
        direction: Direction::In,
        kind: ValueKind::Float,
        role: Some(PortRole::Setpoint),
        point: Some(SP),
    };
    let json = serde_json::to_string(&port).unwrap();
    assert!(json.contains("\"point\":11"), "{json}");
    assert_eq!(serde_json::from_str::<PortDescriptor>(&json).unwrap(), port);
    // Payloads predating the field still parse as unwired.
    let bare: PortDescriptor =
        serde_json::from_str(r#"{"name":"sp","direction":"in","kind":"float","role":"setpoint"}"#)
            .unwrap();
    assert_eq!(bare.point, None);
}
