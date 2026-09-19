//! The page's generic interface surface — the schema-driven-interface
//! decision's rendering half: every faceplate carries the component's
//! five declared `BlockInterface` categories rendered from `GET
//! /schema` joined onto `GET /resources` — measurements and state as
//! labeled readouts, tunable configuration through the existing
//! receipted parameter path, named commands as invocable actions
//! honoring the served availability and refusal reason, and the
//! per-component recent-events list. Kind-specific presentation is an
//! enhancement, never the only usable control surface: these tests pin
//! the page's generic renderers and prove a kind declaring no
//! dedicated markup — no role hints, no parameters — is fully operable
//! through the generic surface alone.

use dcs_blocks::{Pid, PidConfig};
use dcs_core::{
    AdaptedEvent, Command, CommandArgument, CommandAvailability, CommandDecl, CommandError,
    CommandOutcome, CommandState, ComponentDescriptor, ComponentResources, Direction, EmittedEvent,
    EventDecl, EventField, EventFieldKind, EventRetention, EventValue, IoDriver, IoError,
    JournalEvent, PointId, PortDescriptor, Sample, SignalId, Tick, Value, ValueKind,
};
use dcs_model::{PointSignal, SignalIndex};
use dcs_monitor::{Monitor, MonitorClient};
use dcs_runtime::{
    Component, ComponentIo, ComponentIoExt, Executor, IoRequirement, PointMap, StepError,
};
use std::collections::{BTreeMap, HashMap};
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

// The rig's points: `IN` is a writable internal `In` point — the
// generic kind's writable-port command target; `SP` the PID's writable
// internal setpoint; `PV` the field process value; `LAMP` the generic
// kind's `Out` `Bool`; `PID_OUT` the PID's `Out`.
const IN: PointId = PointId(10);
const SP: PointId = PointId(11);
const PV: PointId = PointId(12);
const LAMP: PointId = PointId(20);
const PID_OUT: PointId = PointId(21);

/// A kind with no dedicated page presentation — its descriptor
/// declares no role hints and no parameters, so the page's `isGeneric`
/// rule routes it to the generic faceplate where the interface
/// disclosure is the control surface itself. It still declares the
/// surface the generic rendering exercises: unhinted ports (one bound
/// to a writable point, one bound `Out`, one declared-but-unwired), the
/// named commands `fire` and `reset`, and the kind-emitted event
/// `fired`. `fire` arms the output for the next scan — where the step
/// drives `out` and emits `fired` carrying the armed count — and
/// refuses once spent until `reset` clears it.
struct Bare {
    armed: Option<i64>,
    spent: bool,
    pending: Vec<EmittedEvent>,
}

impl Bare {
    fn new() -> Self {
        Self {
            armed: None,
            spent: false,
            pending: Vec::new(),
        }
    }
}

impl Component for Bare {
    fn name(&self) -> &str {
        "bare"
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        vec![
            IoRequirement::input::<f64>("in", IN),
            IoRequirement::output::<bool>("out", LAMP),
        ]
    }

    fn step(&mut self, io: &dyn ComponentIo, _tick: Tick) -> Result<(), StepError> {
        if let Some(count) = self.armed.take() {
            self.spent = true;
            self.pending.push(EmittedEvent {
                event: "fired".to_string(),
                // The executor stamps the producing instance's name.
                component: String::new(),
                fields: [("count".to_string(), EventValue::Value(Value::Int(count)))]
                    .into_iter()
                    .collect(),
            });
            io.write_typed(LAMP, true)?;
        } else if !self.spent {
            io.write_typed(LAMP, false)?;
        }
        Ok(())
    }

    fn describe(&self) -> ComponentDescriptor {
        ComponentDescriptor {
            name: "bare".to_string(),
            kind: "bare".to_string(),
            label: "bare".to_string(),
            ports: vec![
                PortDescriptor {
                    name: "in".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: None,
                    point: None,
                },
                // Declared but never wired — no io_requirement resolves
                // it, so the served descriptor annotates it `point:
                // None` and its adapted commands report unbound.
                PortDescriptor {
                    name: "spare".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: None,
                    point: None,
                },
                PortDescriptor {
                    name: "out".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Bool,
                    role: None,
                    point: None,
                },
            ],
            parameters: Vec::new(),
            commands: vec![
                CommandDecl {
                    name: "fire".to_string(),
                    request: vec![CommandArgument {
                        name: "count".to_string(),
                        kind: ValueKind::Int,
                    }],
                    availability: CommandAvailability::KindDeclared,
                },
                CommandDecl {
                    name: "reset".to_string(),
                    request: Vec::new(),
                    availability: CommandAvailability::Always,
                },
            ],
            events: vec![EventDecl {
                name: "fired".to_string(),
                payload: vec![EventField {
                    name: "count".to_string(),
                    kind: EventFieldKind::Value(ValueKind::Int),
                    optional: false,
                }],
                retention: EventRetention::Journal,
            }],
        }
    }

    fn invoke_command(
        &mut self,
        command: &str,
        arguments: &BTreeMap<String, Value>,
    ) -> Result<(), String> {
        match command {
            "fire" => {
                if self.spent || self.armed.is_some() {
                    return Err("a fire is already spent; reset clears it".to_string());
                }
                let count = match arguments.get("count") {
                    None => 1,
                    Some(Value::Int(count)) => *count,
                    Some(_) => {
                        unreachable!("submission validates the declared argument kind")
                    }
                };
                self.armed = Some(count);
                Ok(())
            }
            "reset" => {
                self.armed = None;
                self.spent = false;
                Ok(())
            }
            _ => unreachable!("submission validates the declared command name"),
        }
    }

    fn drain_events(&mut self) -> Vec<EmittedEvent> {
        std::mem::take(&mut self.pending)
    }
}

/// One index entry; the rig's index is built inline — the served
/// `writable` marks and units are what the availability and unit
/// annotations read.
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
    }
}

fn signal_index() -> SignalIndex {
    SignalIndex {
        points: vec![
            entry(IN, Direction::In, ValueKind::Float, true),
            entry(SP, Direction::In, ValueKind::Float, true),
            entry(PV, Direction::In, ValueKind::Float, false),
            entry(LAMP, Direction::Out, ValueKind::Bool, false),
            entry(PID_OUT, Direction::Out, ValueKind::Float, false),
        ],
        components: vec![],
    }
}

fn point_map() -> PointMap {
    PointMap::new()
        .with_writable_internal(IN, Direction::In, ValueKind::Float, Value::Float(0.0))
        .with_writable_internal(SP, Direction::In, ValueKind::Float, Value::Float(50.0))
        .with_point(PV, Direction::In, ValueKind::Float)
        .with_point(LAMP, Direction::Out, ValueKind::Bool)
        .with_point(PID_OUT, Direction::Out, ValueKind::Float)
}

fn components() -> Vec<Box<dyn Component>> {
    vec![
        // The kind with dedicated presentation — role hints and tunable
        // parameters — beside the generic `bare`: the interface
        // surface renders for both alike.
        Box::new(
            Pid::new(
                "level-pid",
                SP,
                PV,
                PID_OUT,
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
        Box::new(Bare::new()),
    ]
}

/// Builds the rig and runs `body` against a serving monitor; the server
/// is shut down before the driver's borrow ends — a failing assertion
/// must not deadlock the scope join.
fn with_monitor<T>(body: impl FnOnce(&MonitorClient) -> T) -> T {
    let driver = StubDriver::new(&[
        (PV, Value::Float(0.0)),
        (LAMP, Value::Bool(false)),
        (PID_OUT, Value::Float(0.0)),
    ]);
    let executor = Executor::new(&driver, point_map(), components()).unwrap();
    let monitor = Monitor::bind("127.0.0.1:0", executor, signal_index()).unwrap();
    let client = MonitorClient::new(monitor.local_addr());
    let result = thread::scope(|scope| {
        scope.spawn(|| monitor.serve());
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| body(&client)));
        monitor.shutdown();
        result
    });
    result.unwrap_or_else(|panic| std::panic::resume_unwind(panic))
}

fn component<'a>(view: &'a dcs_core::ResourceView, name: &str) -> &'a ComponentResources {
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

fn sample_at(snapshot: &dcs_core::TelemetrySnapshot, point: PointId) -> Option<Sample> {
    snapshot
        .points
        .iter()
        .find(|telemetry| telemetry.point == point)
        .and_then(|telemetry| telemetry.sample)
}

#[test]
fn the_page_carries_the_generic_five_category_renderers() {
    with_monitor(|client| {
        let page = client.page().unwrap();

        // The fetch half: the schema and resource views ride the same
        // poll as the snapshot, tolerating a peer that predates the
        // endpoints — the absent-section convention.
        for needle in [
            "fetch(base + \"/schema\")",
            "fetch(base + \"/resources\")",
            "(r.ok ? r.json() : null)",
            "interfaceByName",
            "resourcesByName",
        ] {
            assert!(page.contains(needle), "page lacks {needle}");
        }

        // Every faceplate carries the interface disclosure — the call
        // is unconditional beside the dedicated presentation, opened
        // by default for a generic kind and remembered across polls.
        for needle in [
            "interfaceMarkup(descriptor.name, generic)",
            "details class=\\\"interface\\\"",
            "interfaceOpen.get(name) : generic",
            "interfaceOpen.set(details.dataset.component",
        ] {
            assert!(page.contains(needle), "page lacks {needle}");
        }

        // The five categories each render — measurements and state
        // through the labeled readout table, configuration with the
        // parameter path's own controls, commands as invocable rows,
        // events as the attributed journal tail.
        for needle in [
            "data-category=\\\"",
            "resourceTable(\"measurements\", iface.measurements",
            "configTable(name, iface.configuration",
            "resourceTable(\"state\", iface.state",
            "commandTable(name, iface.commands",
            "eventList(resources && resources.events)",
            "data-category=\\\"configuration\\\"",
            "data-category=\\\"commands\\\"",
            "data-category=\\\"events\\\"",
            "none declared",
            "no recent events",
            "entry.retention",
            "unwired",
            "no sample",
            "EVENT_LIMIT",
        ] {
            assert!(page.contains(needle), "page lacks {needle}");
        }

        // Configuration edits reuse the existing parameter machinery —
        // the .param-value/.tune markup submits through
        // `submitParameter`'s receipted set_parameter path unchanged —
        // while `engineering_fixed` properties render read-only.
        for needle in [
            "parameterControl(component, property)",
            "property.capability === \"tunable\"",
            "class=\\\"tune\\\"",
            "class=\\\"param-value\\\"",
        ] {
            assert!(page.contains(needle), "page lacks {needle}");
        }

        // Commands submit through the receipted, active-peer-routed
        // path: the spec's adapted variant reconstructs its generic
        // Command and a declared command goes as the Invoke variant;
        // the row honors the served availability and carries the
        // refusal text; a settled receipt updates the row from the
        // journal.
        for needle in [
            "specCommand(component, spec, args)",
            "submitInterfaceCommand",
            "submitCommand(specCommand(component, spec, args))",
            "{ invoke: {",
            "{ write_value: {",
            "{ force_point: {",
            "{ unforce_point: {",
            "{ set_parameter: {",
            "class=\\\"invoke\\\"",
            "class=\\\"command-arg\\\"",
            "class=\\\"command-status",
            "state.available",
            "state.refusal",
            "settledCommandKey(settled.receipt.command)",
            "setCommandStatus",
            "button.invoke",
            ".command-arg",
        ] {
            assert!(page.contains(needle), "page lacks {needle}");
        }

        // The journal vocabulary the event list and status cells read:
        // the emitted-event record, the invoke command, and the named
        // rejections an invocation can meet.
        for needle in [
            "\"event_emitted\" in event",
            "\"invoke\" in command",
            "\"unknown_command\" in reason",
            "\"argument_type_mismatch\" in reason",
            "\"command_refused\" in reason",
        ] {
            assert!(page.contains(needle), "page lacks {needle}");
        }

        assert!(!page.contains("src="), "page references external assets");
    });
}

#[test]
fn every_served_component_renders_all_five_categories() {
    with_monitor(|client| {
        client.advance(1).unwrap();
        let snapshot = client.snapshot().unwrap();
        let schema = client.schema().unwrap();
        let resources = client.resources().unwrap();

        // One interface and one resources entry per served instance,
        // joined by the component name the page joins on — every kind
        // the run instantiated is covered, dedicated presentation or
        // not.
        assert_eq!(schema.interfaces.len(), snapshot.descriptors.len());
        assert_eq!(resources.components.len(), snapshot.descriptors.len());
        for descriptor in &snapshot.descriptors {
            let interface = schema
                .interfaces
                .iter()
                .find(|entry| entry.name == descriptor.name)
                .unwrap_or_else(|| panic!("no served interface for {}", descriptor.name));
            assert_eq!(interface.interface.kind, descriptor.kind);
            let live = component(&resources, &descriptor.name);
            assert_eq!(live.kind, descriptor.kind);

            // The rendered collections are parallel to the interface's
            // — the generic renderer's join by resource name always
            // resolves.
            for reading in &live.measurements {
                assert!(
                    interface
                        .interface
                        .measurements
                        .iter()
                        .any(|spec| spec.name == reading.name)
                );
            }
            for reading in &live.state {
                assert!(
                    interface
                        .interface
                        .state
                        .iter()
                        .any(|spec| spec.name == reading.name)
                );
            }
            for entry in &live.configuration {
                assert!(
                    interface
                        .interface
                        .configuration
                        .iter()
                        .any(|spec| spec.name == entry.name)
                );
            }
            for state in &live.commands {
                assert!(
                    interface
                        .interface
                        .commands
                        .iter()
                        .any(|spec| spec.name == state.name)
                );
            }
        }

        // The wire document carries the complete five-category shape on
        // every interface and every resource entry — the shape the
        // page's renderers read.
        let (status, body) = client.request("GET", "/schema", None).unwrap();
        assert_eq!(status, 200, "{body}");
        let document: serde_json::Value = serde_json::from_str(&body).unwrap();
        for interface in document["interfaces"].as_array().unwrap() {
            for key in [
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
        let (status, body) = client.request("GET", "/resources", None).unwrap();
        assert_eq!(status, 200, "{body}");
        let document: serde_json::Value = serde_json::from_str(&body).unwrap();
        for entry in document["components"].as_array().unwrap() {
            for key in [
                "measurements",
                "configuration",
                "state",
                "commands",
                "events",
            ] {
                assert!(
                    entry.get(key).is_some(),
                    "served resources lack {key:?}: {entry}"
                );
            }
        }
    });
}

#[test]
fn a_kind_with_no_dedicated_markup_is_fully_operable_generically() {
    with_monitor(|client| {
        client.advance(1).unwrap();
        let schema = client.schema().unwrap();
        let bare = &schema
            .interfaces
            .iter()
            .find(|entry| entry.name == "bare")
            .unwrap()
            .interface;

        // `bare` is the generic-rendered kind: its descriptor declared
        // no role hints and no parameters, so measurements carry the
        // ports, state and configuration are the declared-empty
        // categories, and the command/event surface is what the kind
        // and the adapted generic commands declare.
        assert!(bare.measurements.iter().all(|m| m.role.is_none()));
        assert_eq!(
            bare.measurements
                .iter()
                .map(|m| m.name.as_str())
                .collect::<Vec<_>>(),
            ["in", "spare", "out"]
        );
        assert!(bare.configuration.is_empty());
        assert!(bare.state.is_empty());
        assert_eq!(
            bare.commands
                .iter()
                .map(|c| c.name.as_str())
                .collect::<Vec<_>>(),
            [
                "write_value:in",
                "force_point:in",
                "unforce_point:in",
                "write_value:spare",
                "force_point:spare",
                "unforce_point:spare",
                "fire",
                "reset",
            ]
        );
        assert!(
            bare.events
                .iter()
                .any(|spec| spec.name == "fired" && spec.adapted == AdaptedEvent::Declared)
        );

        // The live resource state the command rows render: the bound
        // writable `in` port's commands are available, the unwired
        // `spare`'s refuse as unbound, the declared `fire` and `reset`
        // are admissible.
        let view = client.resources().unwrap();
        let live = component(&view, "bare");
        assert!(command_state(live, "write_value:in").available);
        let spare = command_state(live, "write_value:spare");
        assert!(!spare.available);
        assert!(
            spare
                .refusal
                .as_deref()
                .is_some_and(|reason| reason.contains("unbound"))
        );
        assert!(command_state(live, "fire").available);
        assert!(command_state(live, "reset").available);

        // The measurement readout joins the bound point's sample; the
        // unwired port reads no sample rather than failing.
        let reading = live.measurements.iter().find(|r| r.name == "in").unwrap();
        assert_eq!(reading.point, Some(IN));
        assert_eq!(reading.sample.unwrap().value, Value::Float(0.0));
        assert_eq!(
            live.measurements
                .iter()
                .find(|r| r.name == "spare")
                .unwrap()
                .sample,
            None
        );

        // The adapted write the command row offers: the receipted
        // `write_value` against the bound point applies, and the
        // measurement and the events list reflect it.
        let write = Command::WriteValue {
            point: IN,
            kind: ValueKind::Float,
            value: Value::Float(42.5),
        };
        let receipt = client.command(&write).unwrap();
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        client.advance(1).unwrap();
        let view = client.resources().unwrap();
        let live = component(&view, "bare");
        assert_eq!(
            live.measurements
                .iter()
                .find(|r| r.name == "in")
                .unwrap()
                .sample
                .unwrap()
                .value,
            Value::Float(42.5)
        );
        assert!(live.events.iter().any(|entry| matches!(
            &entry.event,
            JournalEvent::CommandSettled { receipt }
                if receipt.command == write
                    && matches!(receipt.outcome, CommandOutcome::Applied { .. })
        )));

        // The declared command the row offers as `invoke`: `fire`
        // applies at the boundary, the step emits `fired` carrying the
        // armed count, `out` asserts — command, emission, and readout
        // all through the generic surface's data.
        let fire = Command::Invoke {
            component: "bare".to_string(),
            command: "fire".to_string(),
            arguments: [("count".to_string(), Value::Int(2))].into_iter().collect(),
        };
        let receipt = client.command(&fire).unwrap();
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        client.advance(1).unwrap();
        let view = client.resources().unwrap();
        let live = component(&view, "bare");
        assert_eq!(
            live.measurements
                .iter()
                .find(|r| r.name == "out")
                .unwrap()
                .sample
                .unwrap()
                .value,
            Value::Bool(true)
        );
        assert!(live.events.iter().any(|entry| matches!(
            &entry.event,
            JournalEvent::EventEmitted { event }
                if event.component == "bare"
                    && event.event == "fired"
                    && event.fields.get("count")
                        == Some(&EventValue::Value(Value::Int(2)))
        )));

        // A second `fire` meets the kind's declared unavailability: the
        // receipted path settles the named refusal, which the events
        // list carries — the reason the command row surfaces.
        let receipt = client.command(&fire).unwrap();
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        client.advance(1).unwrap();
        let view = client.resources().unwrap();
        let live = component(&view, "bare");
        assert!(live.events.iter().any(|entry| matches!(
            &entry.event,
            JournalEvent::CommandSettled { receipt }
                if receipt.command == fire
                    && receipt.outcome
                        == (CommandOutcome::Rejected {
                            reason: CommandError::CommandRefused {
                                component: "bare".to_string(),
                                command: "fire".to_string(),
                                reason: "a fire is already spent; reset clears it".to_string(),
                            }
                        })
        )));

        // `reset` — the always-available declared command — applies and
        // the next step reports the cleared state.
        let reset = Command::Invoke {
            component: "bare".to_string(),
            command: "reset".to_string(),
            arguments: BTreeMap::new(),
        };
        let receipt = client.command(&reset).unwrap();
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        client.advance(1).unwrap();
        let view = client.resources().unwrap();
        let live = component(&view, "bare");
        assert_eq!(
            live.measurements
                .iter()
                .find(|r| r.name == "out")
                .unwrap()
                .sample
                .unwrap()
                .value,
            Value::Bool(false)
        );

        // The dedicated-markup kind beside it is covered identically:
        // the tunable `kp`'s `set_parameter:kp` spec is an available
        // command row whose submission is the unchanged receipted
        // parameter path.
        let pid = component(&view, "level-pid");
        let tuning = command_state(pid, "set_parameter:kp");
        assert!(tuning.available);
        assert_eq!(tuning.point, None);
        let tune = Command::SetParameter {
            component: "level-pid".to_string(),
            name: "kp".to_string(),
            value: Value::Float(0.5),
        };
        let receipt = client.command(&tune).unwrap();
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        client.advance(1).unwrap();
        let view = client.resources().unwrap();
        let pid = component(&view, "level-pid");
        assert_eq!(
            pid.configuration
                .iter()
                .find(|entry| entry.name == "kp")
                .unwrap()
                .value,
            Some(Value::Float(0.5))
        );
        assert!(pid.events.iter().any(|entry| matches!(
            &entry.event,
            JournalEvent::CommandSettled { receipt }
                if receipt.command == tune
                    && matches!(receipt.outcome, CommandOutcome::Applied { .. })
        )));

        // The bound point's current sample is what the snapshot serves
        // — the resource view never invents a read path.
        let snapshot = client.snapshot().unwrap();
        assert_eq!(
            sample_at(&snapshot, LAMP).unwrap().value,
            Value::Bool(false)
        );
    });
}
