//! End-to-end tests for the command-path audit identity: a command
//! submitted with a declared actor settles into a receipt carrying that
//! actor and the journaled `CommandSettled` entry shows the
//! attribution; the bare-`Command` body still parses and journals
//! unattributed; the `not_active` rejection path stamps the actor
//! identically; the served page carries the actor-supply logic; and
//! identical scripted runs produce identical receipts and journal
//! entries — all driven over TCP through the in-process `MonitorClient`.

use dcs_core::{
    Command, CommandError, CommandOutcome, CommandReceipt, Direction, IoDriver, IoError,
    JournalEvent, PointId, Role, Sample, Tick, Value, ValueKind,
};
use dcs_model::{PlantModel, SignalIndex};
use dcs_monitor::{Monitor, MonitorClient, PAGE};
use dcs_runtime::{
    Component, ComponentIo, ComponentIoExt, Executor, IoRequirement, Peer, PointMap, StepError,
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

/// The model fixture behind the monitor: point 10 is the
/// model-declared writable `In` point commands target.
const MODEL: &str = include_str!("../fixtures/monitor.json");

fn signal_index() -> SignalIndex {
    PlantModel::load(MODEL).unwrap().signal_index()
}

fn write_value(point: u64, value: Value) -> Command {
    Command::WriteValue {
        point: PointId(point),
        kind: ValueKind::Float,
        value,
    }
}

fn executor(driver: &StubDriver) -> Executor<'_> {
    let map = PointMap::new()
        .with_writable_point(PointId(10), Direction::In, ValueKind::Float)
        .with_point(PointId(20), Direction::Out, ValueKind::Float)
        .with_point(PointId(30), Direction::Out, ValueKind::Float);
    Executor::new(driver, map, vec![Box::new(Scale)]).unwrap()
}

/// Runs `body` against a serving monitor; the server is shut down
/// before the driver's borrow ends.
fn serve<T>(
    monitor: &Monitor<'_>,
    driver: &StubDriver,
    body: impl FnOnce(&StubDriver, &MonitorClient) -> T,
) -> T {
    let client = MonitorClient::new(monitor.local_addr());
    let result = thread::scope(|scope| {
        scope.spawn(|| monitor.serve());
        // A failing assertion must not deadlock the scope join: catch the
        // panic so the server is always shut down before it propagates.
        let result =
            std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| body(driver, &client)));
        monitor.shutdown();
        result
    });
    result.unwrap_or_else(|panic| std::panic::resume_unwind(panic))
}

/// The active-peer rig.
fn with_monitor<T>(body: impl FnOnce(&StubDriver, &MonitorClient) -> T) -> T {
    let driver = StubDriver::new(&[
        (PointId(10), Value::Float(0.0)),
        (PointId(20), Value::Float(0.0)),
        (PointId(30), Value::Float(0.0)),
    ]);
    let monitor = Monitor::bind("127.0.0.1:0", executor(&driver), signal_index()).unwrap();
    serve(&monitor, &driver, body)
}

/// The standby-peer rig: the same executor behind a `standby` peer, so
/// `POST /command` exercises the `not_active` rejection path.
fn with_standby_monitor<T>(body: impl FnOnce(&StubDriver, &MonitorClient) -> T) -> T {
    let driver = StubDriver::new(&[
        (PointId(10), Value::Float(0.0)),
        (PointId(20), Value::Float(0.0)),
        (PointId(30), Value::Float(0.0)),
    ]);
    let peer = Peer::standby(executor(&driver), None);
    let monitor = Monitor::bind_peer("127.0.0.1:0", peer, signal_index()).unwrap();
    serve(&monitor, &driver, body)
}

/// The settled-command journal entries of the served journal, in order.
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

#[test]
fn an_attributed_command_settles_into_an_attributed_receipt_and_journal_entry() {
    with_monitor(|_driver, client| {
        client.advance(1).unwrap();
        let command = write_value(10, Value::Float(5.0));
        let receipt = client.command_as(&command, Some("operator-7")).unwrap();
        // The declared actor is stamped at submission and rides the
        // receipt through the boundary.
        assert_eq!(
            receipt,
            CommandReceipt {
                command: command.clone(),
                outcome: CommandOutcome::Accepted {
                    apply_tick: Tick(2)
                },
                actor: Some("operator-7".to_string()),
                reason: None,
            }
        );

        client.advance(1).unwrap();
        // The journaled CommandSettled echoes the settled receipt —
        // attribution included.
        assert_eq!(
            settled_receipts(client),
            vec![CommandReceipt {
                command,
                outcome: CommandOutcome::Applied { tick: Tick(2) },
                actor: Some("operator-7".to_string()),
                reason: None,
            }]
        );
        // The receipt log carries the same attribution.
        assert_eq!(client.receipts().unwrap(), settled_receipts(client));
    });
}

#[test]
fn the_raw_envelope_and_bare_bodies_parse_side_by_side() {
    with_monitor(|_driver, client| {
        // The envelope wire shape the page serializes.
        let (status, body) = client
            .request(
                "POST",
                "/command",
                Some(
                    r#"{"command":{"write_value":{"point":10,"kind":"float","value":{"float":7.5}}},"actor":"console-a"}"#,
                ),
            )
            .unwrap();
        assert_eq!(status, 200, "{body}");
        let receipt: CommandReceipt = serde_json::from_str(&body).unwrap();
        assert_eq!(receipt.actor.as_deref(), Some("console-a"));
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));

        // The bare-Command body a pre-attribution client sends still
        // parses, and journals unattributed — never a rejection.
        let (status, body) = client
            .request(
                "POST",
                "/command",
                Some(r#"{"write_value":{"point":10,"kind":"float","value":{"float":1.0}}}"#),
            )
            .unwrap();
        assert_eq!(status, 200, "{body}");
        let receipt: CommandReceipt = serde_json::from_str(&body).unwrap();
        assert_eq!(receipt.actor, None);
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        // An unattributed receipt serializes without the actor key.
        assert!(!body.contains("\"actor\""), "{body}");

        // The envelope without an actor submits unattributed the same.
        let (status, body) = client
            .request(
                "POST",
                "/command",
                Some(r#"{"command":{"unforce_point":{"point":10}}}"#),
            )
            .unwrap();
        assert_eq!(status, 200, "{body}");
        let receipt: CommandReceipt = serde_json::from_str(&body).unwrap();
        assert_eq!(receipt.actor, None);

        client.advance(1).unwrap();
        let settled = settled_receipts(client);
        assert_eq!(settled.len(), 3);
        assert_eq!(settled[0].actor.as_deref(), Some("console-a"));
        assert_eq!(settled[1].actor, None);
        assert_eq!(settled[2].actor, None);
        // The unattributed journal entry carries no actor key either.
        let journal_json = serde_json::to_string(&client.journal(0).unwrap()).unwrap();
        assert_eq!(
            journal_json.matches("\"actor\"").count(),
            1,
            "{journal_json}"
        );
    });
}

#[test]
fn a_stray_actor_beside_a_bare_command_is_refused() {
    with_monitor(|_driver, client| {
        // A top-level "actor" without the envelope's "command" wrapper
        // would silently lose its attribution — the endpoint refuses it
        // instead.
        let (status, _body) = client
            .request(
                "POST",
                "/command",
                Some(
                    r#"{"write_value":{"point":10,"kind":"float","value":{"float":7.5}},"actor":"console-a"}"#,
                ),
            )
            .unwrap();
        assert_eq!(status, 400);
        assert!(client.receipts().unwrap().is_empty());
    });
}

#[test]
fn a_reasoned_command_settles_into_a_reasoned_receipt_and_journal_entry() {
    with_monitor(|_driver, client| {
        client.advance(1).unwrap();
        let command = write_value(10, Value::Float(5.0));
        // The fully attributed envelope: actor and reason both declared,
        // both stamped onto the receipt and both journaled.
        let receipt = client
            .command_attributed(
                &command,
                Some("operator-7"),
                Some("nuisance trips during pump work"),
            )
            .unwrap();
        assert_eq!(
            receipt,
            CommandReceipt {
                command: command.clone(),
                outcome: CommandOutcome::Accepted {
                    apply_tick: Tick(2)
                },
                actor: Some("operator-7".to_string()),
                reason: Some("nuisance trips during pump work".to_string()),
            }
        );
        client.advance(1).unwrap();
        assert_eq!(
            settled_receipts(client),
            vec![CommandReceipt {
                command: command.clone(),
                outcome: CommandOutcome::Applied { tick: Tick(2) },
                actor: Some("operator-7".to_string()),
                reason: Some("nuisance trips during pump work".to_string()),
            }]
        );

        // The reason is independent submission metadata: declared
        // without an actor it still rides the envelope and journals.
        let receipt = client
            .command_attributed(&command, None, Some("declared alone"))
            .unwrap();
        assert_eq!(receipt.actor, None);
        assert_eq!(receipt.reason.as_deref(), Some("declared alone"));
        client.advance(1).unwrap();
        let settled = settled_receipts(client);
        assert_eq!(settled.len(), 2);
        assert_eq!(settled[1].reason.as_deref(), Some("declared alone"));
        assert_eq!(settled[1].actor, None);
    });
}

#[test]
fn the_raw_reason_envelope_parses_and_a_stray_reason_is_refused() {
    with_monitor(|_driver, client| {
        // The reason-only envelope — `reason` alone selects the
        // envelope shape; the reasonless half journals unattributed.
        let (status, body) = client
            .request(
                "POST",
                "/command",
                Some(
                    r#"{"command":{"write_value":{"point":10,"kind":"float","value":{"float":7.5}}},"reason":"shelved for the washdown"}"#,
                ),
            )
            .unwrap();
        assert_eq!(status, 200, "{body}");
        let receipt: CommandReceipt = serde_json::from_str(&body).unwrap();
        assert_eq!(receipt.actor, None);
        assert_eq!(
            receipt.reason.as_deref(),
            Some("shelved for the washdown")
        );

        // An envelope key the contract does not declare refuses —
        // strict fields, never a silently dropped attribution.
        let (status, _body) = client
            .request(
                "POST",
                "/command",
                Some(
                    r#"{"command":{"write_value":{"point":10,"kind":"float","value":{"float":7.5}}},"operator":"console-a"}"#,
                ),
            )
            .unwrap();
        assert_eq!(status, 400);

        // A stray top-level "reason" beside a bare command is the same
        // refusal the stray "actor" takes.
        let (status, _body) = client
            .request(
                "POST",
                "/command",
                Some(
                    r#"{"write_value":{"point":10,"kind":"float","value":{"float":7.5}},"reason":"x"}"#,
                ),
            )
            .unwrap();
        assert_eq!(status, 400);
        assert!(client.receipts().unwrap().is_empty());
    });
}

#[test]
fn a_not_active_rejection_stamps_the_actor_identically() {
    with_standby_monitor(|_driver, client| {
        let command = write_value(10, Value::Float(5.0));
        let receipt = client
            .command_attributed(&command, Some("operator-7"), Some("retry on the peer"))
            .unwrap();
        assert_eq!(
            receipt,
            CommandReceipt {
                command: command.clone(),
                outcome: CommandOutcome::Rejected {
                    reason: CommandError::NotActive {
                        point: Some(PointId(10)),
                        role: Role::Standby,
                    },
                },
                actor: Some("operator-7".to_string()),
                reason: Some("retry on the peer".to_string()),
            }
        );
        // The refusal never entered the executor's receipt log, but the
        // journaled CommandSettled carries the attribution identically.
        assert!(client.receipts().unwrap().is_empty());
        assert_eq!(settled_receipts(client), vec![receipt]);
    });
}

#[test]
fn the_served_page_supplies_the_configured_operator_identity() {
    with_monitor(|_driver, client| {
        let page = client.page().unwrap();
        assert_eq!(page, PAGE);
        // The ?operator= URL parameter is the configured identity…
        assert!(page.contains("urlParams.get(\"operator\")"), "{page}");
        // …stamped on every submission through the attributed envelope…
        assert!(page.contains("actor: operator"), "{page}");
        // …and stated beside the command form.
        assert!(page.contains("command-actor"), "{page}");
    });
}

#[test]
fn identical_attributed_runs_produce_identical_receipts_and_journal_entries() {
    // One scripted run over the endpoint: an accepted attributed write,
    // a rejected attributed write (the Out point is not a command
    // target), and an unattributed bare write — then the scans settling
    // them.
    fn script(client: &MonitorClient) -> (String, String) {
        client.advance(1).unwrap();
        client
            .command_as(&write_value(10, Value::Float(5.0)), Some("operator-7"))
            .unwrap();
        client
            .command_as(&write_value(30, Value::Float(9.0)), Some("operator-7"))
            .unwrap();
        client.command(&write_value(10, Value::Float(1.0))).unwrap();
        client.advance(1).unwrap();
        (
            serde_json::to_string(&client.receipts().unwrap()).unwrap(),
            serde_json::to_string(&client.journal(0).unwrap()).unwrap(),
        )
    }
    let first = with_monitor(|_driver, client| script(client));
    let second = with_monitor(|_driver, client| script(client));
    assert_eq!(first, second);
}
