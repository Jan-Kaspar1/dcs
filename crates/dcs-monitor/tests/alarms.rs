//! End-to-end tests for the monitoring page's alarm pane: the
//! descriptor-to-point wiring join plus the snapshot's live telemetry
//! yields the asserted status-role `Out` ports the pane lists; the
//! transition journal supplies the recent alarm transitions; and the
//! acknowledge affordance is an ordinary receipted `write_value` on the
//! `ack` input's bound point — offered only where the model marks that
//! point writable — all driven over TCP through the in-process
//! `MonitorClient`.

use dcs_blocks::{AlarmLimits, LatchingAlarm, Rationalization};
use dcs_core::{
    Command, CommandError, CommandOutcome, CommandReceipt, ComponentDescriptor, Direction,
    IoDriver, IoError, JournalEvent, PointId, PortDescriptor, PortRole, Quality, QualityReason,
    Sample, TelemetrySnapshot, Tick, Value, ValueKind,
};
use dcs_model::{PlantModel, SignalIndex};
use dcs_monitor::{Monitor, MonitorClient};
use dcs_runtime::{Executor, PointMap};
use std::collections::HashMap;
use std::sync::Mutex;
use std::thread;

/// In-memory driver stub; the same minimal stand-in the other monitor
/// tests use — `dcs-monitor` sees only the `IoDriver` contract. `feed`
/// stores an explicit sample so a field input can carry degraded
/// quality.
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

    /// Stores `sample` as the field side's reading of `point` — the
    /// feed a degraded sensor is simulated with.
    fn feed(&self, point: PointId, sample: Sample) {
        self.points.lock().unwrap().insert(point, sample);
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

// The rig's points. Two latching alarms: `lal-1`'s `ack` binds the
// model-declared writable internal point — the pane's ack affordance —
// while `lal-2`'s binds a field `In` point the model left unmarked, so
// no ack is ever offered for it. The outputs are the field `Out`
// points the stub driver serves.
const PV1: PointId = PointId(10);
const ACK1: PointId = PointId(11);
const PV2: PointId = PointId(12);
const ACK2: PointId = PointId(13);
const ALARM1: PointId = PointId(20);
const UNACK1: PointId = PointId(21);
const ALARM2: PointId = PointId(22);
const UNACK2: PointId = PointId(23);

/// Limits 10/90 with a 5-unit hysteresis — the shared `AlarmLimits`
/// both rig components run.
fn limits() -> AlarmLimits {
    AlarmLimits {
        low: 10.0,
        high: 90.0,
        hysteresis: 5.0,
    }
}

/// The decision-70 codes both rig components declare.
fn codes() -> Rationalization {
    Rationalization {
        priority: 1,
        class: 2,
        response_ticks: 30,
    }
}

/// The model fixture behind the monitor: its `io_points` and `signals`
/// give the rig's points names and carry the `writable` mark the ack
/// affordance gates on — `ACK1` marked, `ACK2` unmarked.
const MODEL: &str = include_str!("../fixtures/alarms.json");

fn signal_index() -> SignalIndex {
    PlantModel::load(MODEL).unwrap().signal_index()
}

/// Builds the rig and runs `body` against a serving monitor; the server
/// is shut down before the driver's borrow ends.
fn with_monitor<T>(body: impl FnOnce(&StubDriver, &MonitorClient) -> T) -> T {
    let driver = StubDriver::new(&[
        // Both process values start inside the 10/90 limits so no
        // alarm stands until a test drives one out of range.
        (PV1, Value::Float(50.0)),
        (PV2, Value::Float(50.0)),
        (ACK2, Value::Bool(false)),
        (ALARM1, Value::Bool(false)),
        (UNACK1, Value::Bool(false)),
        (ALARM2, Value::Bool(false)),
        (UNACK2, Value::Bool(false)),
    ]);
    let map = PointMap::new()
        .with_point(PV1, Direction::In, ValueKind::Float)
        // The model-declared ack surface: a channel-less internal `In`
        // point the operator's acknowledgment writes through the
        // ordinary command path, seeded at its declared initial.
        .with_writable_internal(ACK1, Direction::In, ValueKind::Bool, Value::Bool(false))
        .with_point(PV2, Direction::In, ValueKind::Float)
        .with_point(ACK2, Direction::In, ValueKind::Bool)
        .with_point(ALARM1, Direction::Out, ValueKind::Bool)
        .with_point(UNACK1, Direction::Out, ValueKind::Bool)
        .with_point(ALARM2, Direction::Out, ValueKind::Bool)
        .with_point(UNACK2, Direction::Out, ValueKind::Bool);
    let executor = Executor::new(
        &driver,
        map,
        vec![
            Box::new(
                LatchingAlarm::new("lal-1", PV1, ACK1, ALARM1, UNACK1, limits(), codes()).unwrap(),
            ),
            Box::new(
                LatchingAlarm::new("lal-2", PV2, ACK2, ALARM2, UNACK2, limits(), codes()).unwrap(),
            ),
        ],
    )
    .unwrap();
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

/// One standing alarm row's data: the owning component's name, the
/// status port, and its live sample.
#[derive(Debug)]
struct Standing<'s> {
    component: &'s str,
    port: &'s PortDescriptor,
    sample: Sample,
}

/// The join the alarm pane performs over a polled snapshot: every
/// status-role `Out` port whose bound point's live sample asserts —
/// Bool true — listed with its component.
fn asserted_status_ports(snapshot: &TelemetrySnapshot) -> Vec<Standing<'_>> {
    snapshot
        .descriptors
        .iter()
        .flat_map(|descriptor| descriptor.ports.iter().map(move |port| (descriptor, port)))
        .filter(|(_, port)| port.role == Some(PortRole::Status) && port.direction == Direction::Out)
        .filter_map(|(descriptor, port)| {
            let sample = port
                .point
                .and_then(|point| telemetry(snapshot, point).sample)?;
            (sample.value == Value::Bool(true)).then_some(Standing {
                component: &descriptor.name,
                port,
                sample,
            })
        })
        .collect()
}

#[test]
fn the_descriptor_snapshot_join_yields_the_asserted_status_ports() {
    with_monitor(|driver, client| {
        // Trip lal-1's high alarm; lal-2 stays inside its limits.
        driver.write(PV1, Value::Float(95.0)).unwrap();
        let snapshot = client.advance(1).unwrap();

        // The wiring the pane resolves through: every status-role port
        // — the `ack` inputs included — carries its bound point, so the
        // standing list joins straight into telemetry and the ack
        // affordance reads its target's writable mark.
        let lal1 = descriptor(&snapshot, "lal-1");
        assert_eq!(bound_point(lal1, "in"), Some(PV1));
        assert_eq!(bound_point(lal1, "ack"), Some(ACK1));
        assert_eq!(bound_point(lal1, "alarm"), Some(ALARM1));
        assert_eq!(bound_point(lal1, "unacknowledged"), Some(UNACK1));
        let lal2 = descriptor(&snapshot, "lal-2");
        assert_eq!(bound_point(lal2, "ack"), Some(ACK2));
        assert_eq!(bound_point(lal2, "alarm"), Some(ALARM2));

        // The standing list: both of lal-1's status-role Out flags
        // assert — the standing limit state and the fresh latch — each
        // carrying its component, value, and the tick it last changed.
        let standing = asserted_status_ports(&snapshot);
        assert_eq!(standing.len(), 2, "{standing:?}");
        for row in &standing {
            assert_eq!(row.component, "lal-1");
            assert_eq!(row.sample.value, Value::Bool(true));
            assert_eq!(row.sample.tick, Tick(1));
        }
        let ports: Vec<&str> = standing.iter().map(|row| row.port.name.as_str()).collect();
        assert!(ports.contains(&"alarm"), "{ports:?}");
        assert!(ports.contains(&"unacknowledged"), "{ports:?}");

        // Clearing removes the standing flag on the next snapshot; the
        // unacknowledged latch stands — the two-flag model's point.
        driver.write(PV1, Value::Float(50.0)).unwrap();
        let snapshot = client.advance(1).unwrap();
        let standing = asserted_status_ports(&snapshot);
        assert_eq!(standing.len(), 1, "{standing:?}");
        assert_eq!(standing[0].component, "lal-1");
        assert_eq!(standing[0].port.name, "unacknowledged");
        assert_eq!(standing[0].sample.tick, Tick(2));
    });
}

#[test]
fn page_serves_alarm_pane_markup_and_the_writable_only_ack_rule() {
    with_monitor(|_driver, client| {
        let page = client.page().unwrap();
        // The pane: a standing list of the asserted status-role Out
        // ports plus the alarm journal slice of the merged journal.
        for needle in [
            "id=\"alarm-pane\"",
            "id=\"alarms\"",
            "id=\"alarm-summary\"",
            "id=\"alarm-journal\"",
            "function renderAlarms(",
            "function alarmJoin(",
            "port.role !== \"status\"",
            "port.direction === \"out\"",
            "function isAsserted(",
            "function lastChangedTick(",
            "function alarmJournalEntry(",
            "journalEntries",
        ] {
            assert!(page.contains(needle), "page lacks {needle}");
        }
        // The ack affordance exists only where a declared `ack` input
        // binds a point the model marked writable — the same metadata
        // gate every command affordance follows — and issues an
        // ordinary receipted write_value, no alarm protocol.
        for needle in [
            "function ackAffordance(",
            "p.name === \"ack\" && p.direction === \"in\"",
            "!meta || !meta.writable",
            "class=\\\"ack\\\"",
            "function submitAck(",
            "write_value",
            "{ bool: true }",
            "await submitCommand(",
            "not_writable",
        ] {
            assert!(page.contains(needle), "page lacks {needle}");
        }
        // A degraded alarm value draws the row distinctly — the quality
        // class marks the row rather than reporting a clean assertion.
        for needle in ["tr.alarm", "qualityClass(sample.quality)"] {
            assert!(page.contains(needle), "page lacks {needle}");
        }
        assert!(!page.contains("src="), "page references external assets");

        // The metadata the rule gates on: lal-1's ack point is the
        // model-declared writable internal point; lal-2's is an
        // unmarked field input — never an affordance.
        let index = client.signals().unwrap();
        assert!(index.get(ACK1).unwrap().writable);
        assert_eq!(index.get(ACK1).unwrap().direction, Direction::In);
        assert!(!index.get(ACK2).unwrap().writable);
        assert!(!index.get(ALARM1).unwrap().writable);
    });
}

#[test]
fn the_ack_affordance_issues_an_ordinary_receipted_write() {
    with_monitor(|driver, client| {
        driver.write(PV1, Value::Float(95.0)).unwrap();
        client.advance(1).unwrap();

        // The exact body the pane's ack button posts: an ordinary
        // write_value of true on the ack port's bound point, answered
        // by the same receipt contract every operator command gets.
        let (status, body) = client
            .request(
                "POST",
                "/command",
                Some(r#"{"write_value":{"point":11,"kind":"bool","value":{"bool":true}}}"#),
            )
            .unwrap();
        assert_eq!(status, 200, "{body}");
        let receipt: CommandReceipt = serde_json::from_str(&body).unwrap();
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Accepted {
                apply_tick: Tick(2)
            }
        );

        // The applying scan observes the ack's rising edge: the latch
        // clears while the standing limit state still reports alarmed.
        let snapshot = client.advance(1).unwrap();
        assert_eq!(
            telemetry(&snapshot, UNACK1).sample.unwrap().value,
            Value::Bool(false)
        );
        assert_eq!(
            telemetry(&snapshot, ALARM1).sample.unwrap().value,
            Value::Bool(true)
        );

        // The pane's pulse writes the point back false once the ack
        // applied — an ordinary receipted command too — re-arming the
        // latch for a fresh trip.
        let receipt = client
            .command(&Command::WriteValue {
                point: ACK1,
                kind: ValueKind::Bool,
                value: Value::Bool(false),
            })
            .unwrap();
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        driver.write(PV1, Value::Float(5.0)).unwrap();
        let snapshot = client.advance(2).unwrap();
        // The release scanned past: the fresh low trip latches again.
        assert_eq!(
            telemetry(&snapshot, UNACK1).sample.unwrap().value,
            Value::Bool(true)
        );

        // lal-2's ack binds a point the model never marked writable —
        // the affordance's gate exists because this submission rejects.
        let rejection = client
            .command(&Command::WriteValue {
                point: ACK2,
                kind: ValueKind::Bool,
                value: Value::Bool(true),
            })
            .unwrap();
        assert_eq!(
            rejection.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::NotWritable { point: ACK2 }
            }
        );
        // Both outcomes journal — the settled ack write and the refused
        // one — on the transition surface the alarm pane filters.
        let journal = client.journal(0).unwrap();
        assert!(
            journal.iter().any(|entry| matches!(
                &entry.event,
                JournalEvent::CommandSettled { receipt }
                    if matches!(receipt.command, Command::WriteValue { point, .. } if point == ACK1)
                        && matches!(receipt.outcome, CommandOutcome::Applied { .. })
            )),
            "the settled ack write is journaled"
        );
        assert!(
            journal.iter().any(|entry| matches!(
                &entry.event,
                JournalEvent::CommandSettled { receipt }
                    if receipt.outcome
                        == CommandOutcome::Rejected {
                            reason: CommandError::NotWritable { point: ACK2 }
                        }
            )),
            "the refused write on the unmarked ack point is journaled"
        );
    });
}

#[test]
fn a_bad_quality_alarm_is_served_marked_and_journaled() {
    with_monitor(|driver, client| {
        // A degraded sensor: the field read of lal-1's input carries
        // Bad quality while asserting the trip.
        driver.feed(
            PV1,
            Sample::new(
                Value::Float(95.0),
                Quality::Bad(QualityReason::CommunicationFault),
                Tick(1),
            ),
        );
        let snapshot = client.advance(1).unwrap();

        // The join still yields the standing flags — the alarm asserts —
        // but both samples carry the input's degraded quality: the pane
        // draws that distinctly rather than as a clean trip.
        let standing = asserted_status_ports(&snapshot);
        assert_eq!(standing.len(), 2, "{standing:?}");
        for row in &standing {
            assert_eq!(row.component, "lal-1");
            assert_eq!(
                row.sample.quality,
                Quality::Bad(QualityReason::CommunicationFault)
            );
        }

        // The quality transition lands in the journal — the event the
        // pane's alarm journal lists for the status-bound point.
        let journal = client.journal(0).unwrap();
        assert!(
            journal.iter().any(|entry| matches!(
                &entry.event,
                JournalEvent::QualityChanged { point, to, .. }
                    if *point == ALARM1
                        && *to == Quality::Bad(QualityReason::CommunicationFault)
            )),
            "the alarm point's degradation is journaled"
        );
    });
}
