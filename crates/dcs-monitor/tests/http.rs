//! End-to-end tests driving a real `Monitor` over TCP through the
//! in-process `MonitorClient`.

use dcs_core::{
    Command, CommandError, CommandOutcome, CommandReceipt, Direction, IoDriver, IoError, PointId,
    Sample, Tick, Value, ValueKind,
};
use dcs_monitor::{Monitor, MonitorClient};
use dcs_runtime::{
    Component, ComponentIo, ComponentIoExt, Executor, IoRequirement, PointMap, StepError,
};
use std::collections::{HashMap, HashSet};
use std::sync::Mutex;
use std::sync::atomic::{AtomicU64, Ordering};
use std::thread;

/// In-memory driver stub with injectable faults; the same minimal stand-in
/// the executor tests use — `dcs-monitor` sees only the `IoDriver`
/// contract.
struct StubDriver {
    points: Mutex<HashMap<PointId, Sample>>,
    faults: Mutex<HashSet<PointId>>,
    tick: AtomicU64,
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
            faults: Mutex::new(HashSet::new()),
            tick: AtomicU64::new(0),
        }
    }
}

impl IoDriver for StubDriver {
    fn read(&self, point: PointId) -> Result<Sample, IoError> {
        if self.faults.lock().unwrap().contains(&point) {
            return Err(IoError::Disconnected(point));
        }
        self.points
            .lock()
            .unwrap()
            .get(&point)
            .copied()
            .ok_or(IoError::UnknownPoint(point))
    }

    fn write(&self, point: PointId, value: Value) -> Result<(), IoError> {
        if self.faults.lock().unwrap().contains(&point) {
            return Err(IoError::Disconnected(point));
        }
        let mut points = self.points.lock().unwrap();
        let sample = points.get_mut(&point).ok_or(IoError::UnknownPoint(point))?;
        if value.kind() != sample.value.kind() {
            return Err(IoError::TypeMismatch {
                point,
                expected: sample.value.kind(),
                found: value,
            });
        }
        *sample = Sample::good(value, Tick(self.tick.load(Ordering::Relaxed)));
        Ok(())
    }
}

/// Reads `In` point 10 and drives `Out` point 20 at gain 2; point 30 is a
/// mapped `Out` point no component writes.
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

fn write_value(point: u64, kind: ValueKind, value: Value) -> Command {
    Command::WriteValue {
        point: PointId(point),
        kind,
        value,
    }
}

/// Builds the rig and runs `body` against a serving monitor; the server is
/// shut down before the driver's borrow ends.
fn with_monitor<T>(body: impl FnOnce(&StubDriver, &MonitorClient) -> T) -> T {
    let driver = StubDriver::new(&[
        (PointId(10), Value::Float(0.0)),
        (PointId(20), Value::Float(0.0)),
        (PointId(30), Value::Float(0.0)),
    ]);
    let map: PointMap = [
        (PointId(10), Direction::In, ValueKind::Float),
        (PointId(20), Direction::Out, ValueKind::Float),
        (PointId(30), Direction::Out, ValueKind::Float),
    ]
    .into_iter()
    .collect();
    let executor = Executor::new(&driver, map, vec![Box::new(Scale)]).unwrap();
    let monitor = Monitor::bind("127.0.0.1:0", executor).unwrap();
    let client = MonitorClient::new(monitor.local_addr());
    thread::scope(|scope| {
        scope.spawn(|| monitor.serve());
        let result = body(&driver, &client);
        monitor.shutdown();
        result
    })
}

fn point_value(snapshot: &dcs_core::TelemetrySnapshot, point: u64) -> Option<Value> {
    snapshot
        .points
        .iter()
        .find(|telemetry| telemetry.point == PointId(point))
        .and_then(|telemetry| telemetry.sample)
        .map(|sample| sample.value)
}

#[test]
fn snapshot_roundtrips_over_http() {
    with_monitor(|driver, client| {
        driver.write(PointId(10), Value::Float(3.0)).unwrap();
        let advanced = client.advance(1).unwrap();
        assert_eq!(advanced.tick, Tick(1));
        assert_eq!(point_value(&advanced, 20), Some(Value::Float(6.0)));

        let snapshot = client.snapshot().unwrap();
        assert_eq!(snapshot, advanced);
        // Points ordered by id, directions reported.
        let ids: Vec<u64> = snapshot.points.iter().map(|p| p.point.0).collect();
        assert_eq!(ids, vec![10, 20, 30]);
        assert_eq!(snapshot.points[0].direction, Direction::In);
        assert_eq!(snapshot.points[2].direction, Direction::Out);
        assert_eq!(snapshot.components[0].name, "scale");
        assert_eq!(snapshot.components[0].last_tick, Some(Tick(1)));
    });
}

#[test]
fn setpoint_command_changes_output_at_the_tick_boundary() {
    with_monitor(|_driver, client| {
        client.advance(1).unwrap();
        let command = write_value(10, ValueKind::Float, Value::Float(5.0));
        let receipt = client.command(&command).unwrap();
        // Accepted at submission, scheduled for the next scan's tick.
        assert_eq!(
            receipt,
            CommandReceipt {
                command,
                outcome: CommandOutcome::Accepted {
                    apply_tick: Tick(2)
                },
            }
        );
        // Between scans nothing has changed yet.
        assert_eq!(
            point_value(&client.snapshot().unwrap(), 20),
            Some(Value::Float(0.0))
        );

        // The next scan applies the command before reading inputs, so the
        // component already sees the new setpoint.
        let snapshot = client.advance(1).unwrap();
        assert_eq!(snapshot.tick, Tick(2));
        assert_eq!(point_value(&snapshot, 10), Some(Value::Float(5.0)));
        assert_eq!(point_value(&snapshot, 20), Some(Value::Float(10.0)));

        let receipts = client.receipts().unwrap();
        assert_eq!(receipts.len(), 2);
        assert_eq!(
            receipts[1].outcome,
            CommandOutcome::Applied { tick: Tick(2) }
        );
    });
}

#[test]
fn rejected_commands_return_named_reasons() {
    with_monitor(|driver, client| {
        // Unknown point.
        let receipt = client
            .command(&write_value(99, ValueKind::Float, Value::Float(1.0)))
            .unwrap();
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::UnknownPoint { point: PointId(99) }
            }
        );

        // Type mismatch against the point's declared kind.
        let receipt = client
            .command(&write_value(10, ValueKind::Int, Value::Int(1)))
            .unwrap();
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::TypeMismatch {
                    point: PointId(10),
                    expected: ValueKind::Float,
                    found: Value::Int(1),
                }
            }
        );

        // Driver rejection surfaces at the scan boundary.
        driver.faults.lock().unwrap().insert(PointId(30));
        let receipt = client
            .command(&write_value(30, ValueKind::Float, Value::Float(9.0)))
            .unwrap();
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        client.advance(1).unwrap();
        assert_eq!(
            client.receipts().unwrap().last().unwrap().outcome,
            CommandOutcome::Rejected {
                reason: CommandError::DriverRejected {
                    point: PointId(30),
                    error: IoError::Disconnected(PointId(30)),
                }
            }
        );
    });
}

#[test]
fn identical_scripted_runs_produce_identical_receipts() {
    let run = || {
        with_monitor(|_driver, client| {
            client.advance(1).unwrap();
            client
                .command(&write_value(10, ValueKind::Float, Value::Float(5.0)))
                .unwrap();
            client
                .command(&write_value(99, ValueKind::Float, Value::Float(0.0)))
                .unwrap();
            client
                .command(&write_value(30, ValueKind::Float, Value::Float(7.0)))
                .unwrap();
            client.advance(2).unwrap();
            client
                .command(&write_value(10, ValueKind::Float, Value::Float(8.0)))
                .unwrap();
            client.advance(1).unwrap();
            (
                serde_json::to_string(&client.receipts().unwrap()).unwrap(),
                serde_json::to_string(&client.snapshot().unwrap()).unwrap(),
            )
        })
    };

    assert_eq!(run(), run());
}

#[test]
fn malformed_bodies_and_unknown_paths_are_http_errors() {
    with_monitor(|_driver, client| {
        // Garbage JSON is a 400, not a panic or a silent rejection.
        let (status, _body) = client
            .request("POST", "/command", Some("{not json"))
            .unwrap();
        assert_eq!(status, 400);
        let (status, _body) = client.request("GET", "/nope", None).unwrap();
        assert_eq!(status, 404);
    });
}
