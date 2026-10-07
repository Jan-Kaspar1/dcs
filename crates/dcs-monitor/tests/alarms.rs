//! Alarm command, quality, metadata and journal contracts exercised over TCP.

use dcs_blocks::{AlarmLimits, LatchingAlarm, Rationalization};
use dcs_core::{
    Command, CommandError, CommandOutcome, CommandReceipt, Direction, IoDriver, IoError,
    JournalEvent, PointId, Quality, QualityReason, Sample, TelemetrySnapshot, Tick, Value,
    ValueKind,
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

fn telemetry(snapshot: &TelemetrySnapshot, point: PointId) -> &dcs_core::PointTelemetry {
    snapshot
        .points
        .iter()
        .find(|telemetry| telemetry.point == point)
        .unwrap_or_else(|| panic!("no telemetry for {point:?}"))
}

#[test]
fn ack_targets_have_declared_writable_metadata() {
    with_monitor(|_driver, client| {
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

        for point in [ALARM1, UNACK1] {
            let sample = telemetry(&snapshot, point).sample.unwrap();
            assert_eq!(sample.value, Value::Bool(true));
            assert_eq!(
                sample.quality,
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
