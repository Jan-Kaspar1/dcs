//! End-to-end tests for the faceplate parameter-edit surface: a
//! `set_parameter` command posted the way the page posts it applies at
//! the next scan boundary with a receipt and the tuned behavior shows in
//! later telemetry; out-of-range and mistyped submissions answer the
//! named `CommandError` rejection, journaled for the page's journal
//! pane; and the served page carries the parameter-edit markup plus the
//! declared-range warning logic — all driven over TCP through the
//! in-process `MonitorClient` against a multi-kind rig: a real
//! `dcs_blocks::Pid` beside a kind declaring no parameters.

use dcs_blocks::{Pid, PidConfig};
use dcs_core::{
    Command, CommandError, CommandOutcome, CommandReceipt, Direction, IoDriver, IoError,
    JournalEvent, ParameterRange, PointId, Sample, TelemetrySnapshot, Tick, Value, ValueKind,
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

// The rig's points, matching the shared faceplates fixture: `SP` is the
// model-declared writable internal `In` point carrying the operator
// setpoint; the rest are field points the stub driver serves.
const PV: PointId = PointId(10);
const SP: PointId = PointId(11);
const OUT: PointId = PointId(20);
const PLAIN_OUT: PointId = PointId(31);
const PLAIN_IN: PointId = PointId(40);

/// A component with no `describe` override — the second kind of the
/// multi-kind fixture, declaring no parameters and so offering no edit
/// affordance.
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

/// The model fixture behind the monitor — the same one the faceplate
/// tests serve, covering every point this rig maps.
const MODEL: &str = include_str!("../fixtures/faceplates.json");

fn signal_index() -> SignalIndex {
    PlantModel::load(MODEL).unwrap().signal_index()
}

/// Builds the rig — a `Pid` with kp = 1, no integral or derivative, and
/// 0..5 output limits, beside the parameterless `Plain` — and runs
/// `body` against a serving monitor; the server is shut down before the
/// driver's borrow ends.
fn with_monitor<T>(body: impl FnOnce(&StubDriver, &MonitorClient) -> T) -> T {
    let driver = StubDriver::new(&[
        (PV, Value::Float(0.0)),
        (OUT, Value::Float(0.0)),
        (PLAIN_OUT, Value::Float(0.0)),
        (PLAIN_IN, Value::Float(0.0)),
    ]);
    let map = PointMap::new()
        .with_point(PV, Direction::In, ValueKind::Float)
        .with_writable_internal(SP, Direction::In, ValueKind::Float, Value::Float(50.0))
        .with_point(OUT, Direction::Out, ValueKind::Float)
        .with_point(PLAIN_OUT, Direction::Out, ValueKind::Float)
        .with_point(PLAIN_IN, Direction::In, ValueKind::Float);
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
    let executor = Executor::new(&driver, map, vec![Box::new(pid), Box::new(Plain)]).unwrap();
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

fn telemetry(snapshot: &TelemetrySnapshot, point: PointId) -> &dcs_core::PointTelemetry {
    snapshot
        .points
        .iter()
        .find(|telemetry| telemetry.point == point)
        .unwrap_or_else(|| panic!("no telemetry for {point:?}"))
}

fn descriptor<'s>(
    snapshot: &'s TelemetrySnapshot,
    name: &str,
) -> &'s dcs_core::ComponentDescriptor {
    snapshot
        .descriptors
        .iter()
        .find(|descriptor| descriptor.name == name)
        .unwrap_or_else(|| panic!("no descriptor for {name}"))
}

/// Posts the exact body the page's parameter edit serializes — the
/// `set_parameter` wire shape — and decodes the receipt like the page
/// does.
fn post_set_parameter(
    client: &MonitorClient,
    component: &str,
    name: &str,
    value: Value,
) -> CommandReceipt {
    let body = format!(
        "{{\"set_parameter\":{{\"component\":{component:?},\"name\":{name:?},\"value\":{}}}}}",
        serde_json::to_string(&value).unwrap()
    );
    let (status, body) = client.request("POST", "/command", Some(&body)).unwrap();
    assert_eq!(status, 200, "{body}");
    serde_json::from_str(&body).unwrap()
}

#[test]
fn a_parameter_edit_applies_at_the_scan_boundary_and_telemetry_reflects_it() {
    with_monitor(|driver, client| {
        // pv 48 against setpoint 50: with kp = 1 the loop drives out = 2.
        driver.write(PV, Value::Float(48.0)).unwrap();
        let snapshot = client.advance(1).unwrap();
        assert_eq!(
            telemetry(&snapshot, OUT).sample.unwrap().value,
            Value::Float(2.0)
        );

        // The body the page's edit control posts: retune kp to 0.5.
        let receipt = post_set_parameter(client, "level-pid", "kp", Value::Float(0.5));
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Accepted {
                apply_tick: Tick(2)
            }
        );
        // Between scans nothing has changed yet.
        assert_eq!(
            telemetry(&client.snapshot().unwrap(), OUT)
                .sample
                .unwrap()
                .value,
            Value::Float(2.0)
        );

        // The next scan applies the tune at its boundary; the same scan's
        // step already sees it: out = 0.5 · (50 − 48) = 1.
        let snapshot = client.advance(1).unwrap();
        assert_eq!(snapshot.tick, Tick(2));
        assert_eq!(
            telemetry(&snapshot, OUT).sample.unwrap().value,
            Value::Float(1.0)
        );

        // Exactly one receipt, now reporting the applied tick — the
        // answer the page's status cell and journal pane display.
        let command = Command::SetParameter {
            component: "level-pid".to_string(),
            name: "kp".to_string(),
            value: Value::Float(0.5),
        };
        assert_eq!(
            client.receipts().unwrap(),
            &[CommandReceipt {
                command: command.clone(),
                outcome: CommandOutcome::Applied { tick: Tick(2) },
                actor: None,
            }]
        );
        assert!(
            client.journal(0).unwrap().iter().any(|entry| matches!(
                &entry.event,
                JournalEvent::CommandSettled { receipt }
                    if receipt.command == command
                        && receipt.outcome
                            == CommandOutcome::Applied { tick: Tick(2) }
            )),
            "the settled tune is journaled"
        );
    });
}

#[test]
fn out_of_range_and_mistyped_edits_yield_named_rejections() {
    with_monitor(|_driver, client| {
        // dt declares the positive-finite range; -1 lands outside it and
        // the receipted path answers the named rejection — the variant
        // the page's row and journal display.
        let receipt = post_set_parameter(client, "level-pid", "dt", Value::Float(-1.0));
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::OutOfRange {
                    component: "level-pid".to_string(),
                    parameter: "dt".to_string(),
                    value: Value::Float(-1.0),
                    range: ParameterRange {
                        min: Value::Float(f64::MIN_POSITIVE),
                        max: Value::Float(f64::MAX),
                    },
                }
            }
        );

        // A value of the wrong kind answers the named type mismatch.
        let receipt = post_set_parameter(client, "level-pid", "kp", Value::Bool(true));
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::ParameterTypeMismatch {
                    component: "level-pid".to_string(),
                    parameter: "kp".to_string(),
                    expected: ValueKind::Float,
                    found: Value::Bool(true),
                }
            }
        );

        // Both named rejections are journaled — the journal pane the
        // page polls shows them rather than the edits dropping silently.
        let journal = client.journal(0).unwrap();
        for parameter in ["dt", "kp"] {
            assert!(
                journal.iter().any(|entry| matches!(
                    &entry.event,
                    JournalEvent::CommandSettled { receipt }
                        if matches!(
                            &receipt.command,
                            Command::SetParameter { name, .. } if name.as_str() == parameter
                        ) && matches!(
                            receipt.outcome,
                            CommandOutcome::Rejected { .. }
                        )
                )),
                "the {parameter} rejection is journaled"
            );
        }
    });
}

#[test]
fn page_serves_parameter_edit_markup_and_declared_range_warning() {
    with_monitor(|_driver, client| {
        let page = client.page().unwrap();
        // The edit surface: a control typed to the declared kind per
        // parameter — a Bool select, a text input for Int and Float —
        // carrying the component, parameter, and kind the submit path
        // reads, plus the set button and the status cell the receipted
        // answer lands on.
        for needle in [
            "function parameterTable(",
            "function parameterControl(",
            "class=\\\"param-value\\\"",
            "<option value=\\\"true\\\">true</option>",
            "data-component",
            "data-param",
            "data-kind",
            "class=\\\"tune\\\"",
            "param-status",
            "function setParamStatus(",
        ] {
            assert!(page.contains(needle), "page lacks {needle}");
        }
        // The submission path: set_parameter built per edit, routed
        // through the pair view's active-peer command path, and the
        // descriptor-declared range check warning — advisory, sent
        // anyway — plus the settled-receipt surface in the journal feed.
        for needle in [
            "function submitParameter(",
            "{ set_parameter: {",
            "await submitCommand(command)",
            "function parameterValue(",
            "function inDeclaredRange(",
            "outside declared range",
            "not an Int value",
            "not a Float value",
            "settled.receipt.command.set_parameter",
        ] {
            assert!(page.contains(needle), "page lacks {needle}");
        }
        // The no-affordance rule for a kind declaring no parameters.
        assert!(page.contains("descriptor.parameters.length > 0"));
        assert!(!page.contains("src="), "page references external assets");

        // The descriptor data behind the markup: the Pid kind declares
        // its tunables with ranges; the parameterless kind declares
        // none, so its faceplate renders no edit affordance.
        let snapshot = client.snapshot().unwrap();
        let pid = descriptor(&snapshot, "level-pid");
        assert_eq!(pid.kind, "pid");
        assert!(!pid.parameters.is_empty());
        let dt = pid
            .parameters
            .iter()
            .find(|param| param.name == "dt")
            .unwrap();
        assert_eq!(dt.kind, ValueKind::Float);
        assert_eq!(
            dt.range,
            Some(ParameterRange {
                min: Value::Float(f64::MIN_POSITIVE),
                max: Value::Float(f64::MAX),
            })
        );
        assert!(descriptor(&snapshot, "plain").parameters.is_empty());
    });
}
