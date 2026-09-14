//! End-to-end tests for the monitor's bounded point history and
//! transition journal, driven over TCP through the in-process
//! `MonitorClient`.

use dcs_core::{
    Command, CommandError, CommandOutcome, CommandReceipt, Direction, IoDriver, IoError,
    JournalEntry, JournalEvent, PointId, Quality, QualityReason, Sample, Tick, Value, ValueKind,
};
use dcs_model::{PlantModel, SignalIndex};
use dcs_monitor::{Monitor, MonitorClient, MonitorConfig};
use dcs_runtime::{
    Component, ComponentIo, ComponentIoExt, Executor, IoRequirement, PointMap, StepError,
};
use std::collections::{HashMap, HashSet};
use std::sync::Mutex;
use std::sync::atomic::{AtomicU64, Ordering};
use std::thread;

/// In-memory driver stub with injectable faults; the same minimal
/// stand-in the other monitor tests use.
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

/// Reads `In` point 10 and fails its step whenever the value goes
/// negative — a controllable step-failure source for the journal.
struct Fragile;

impl Component for Fragile {
    fn name(&self) -> &str {
        "fragile"
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        vec![IoRequirement::input::<f64>("in", PointId(10))]
    }

    fn step(&mut self, io: &dyn ComponentIo, _tick: Tick) -> Result<(), StepError> {
        let sample = io.read_typed::<f64>(PointId(10))?;
        if sample.value < 0.0 {
            return Err("input below zero".into());
        }
        Ok(())
    }
}

/// The model fixture behind the monitor.
const MODEL: &str = include_str!("../fixtures/monitor.json");

/// The signal index a controller built from [`MODEL`] would serve.
fn signal_index() -> SignalIndex {
    PlantModel::load(MODEL).unwrap().signal_index()
}

fn write_value(point: u64, kind: ValueKind, value: Value) -> Command {
    Command::WriteValue {
        point: PointId(point),
        kind,
        value,
    }
}

/// Builds the rig with default retention and runs `body` against a
/// serving monitor; the server is shut down before the driver's borrow
/// ends.
fn with_monitor<T>(body: impl FnOnce(&StubDriver, &MonitorClient) -> T) -> T {
    with_monitor_config(MonitorConfig::default(), vec![Box::new(Scale)], body)
}

/// As [`with_monitor`] with explicit retention bounds and component set.
fn with_monitor_config<T>(
    config: MonitorConfig,
    components: Vec<Box<dyn Component>>,
    body: impl FnOnce(&StubDriver, &MonitorClient) -> T,
) -> T {
    let driver = StubDriver::new(&[
        (PointId(10), Value::Float(0.0)),
        (PointId(20), Value::Float(0.0)),
        (PointId(30), Value::Float(0.0)),
    ]);
    let map = PointMap::new()
        .with_writable_point(PointId(10), Direction::In, ValueKind::Float)
        .with_point(PointId(20), Direction::Out, ValueKind::Float)
        .with_point(PointId(30), Direction::Out, ValueKind::Float);
    let executor = Executor::new(&driver, map, components).unwrap();
    let monitor = Monitor::bind_with("127.0.0.1:0", executor, signal_index(), config).unwrap();
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

#[test]
fn history_ring_respects_capacity_and_tick_order() {
    let config = MonitorConfig {
        history_capacity: 4,
        journal_capacity: 64,
        ..MonitorConfig::default()
    };
    with_monitor_config(config, vec![Box::new(Scale)], |_driver, client| {
        // Two scans against a capacity-4 ring retain both.
        client.advance(2).unwrap();
        let history = client.history(&[PointId(10)], 0).unwrap();
        assert_eq!(
            history[0]
                .samples
                .iter()
                .map(|sample| sample.sample.tick)
                .collect::<Vec<_>>(),
            vec![Tick(1), Tick(2)]
        );

        // Seven scans total retain the last min(7, 4) — the ring drops
        // the oldest rather than growing.
        client.advance(5).unwrap();
        let history = client.history(&[], 0).unwrap();
        assert_eq!(
            history.iter().map(|h| h.point).collect::<Vec<_>>(),
            vec![PointId(10), PointId(20), PointId(30)]
        );
        for point in &history[..2] {
            let ticks: Vec<Tick> = point
                .samples
                .iter()
                .map(|sample| sample.sample.tick)
                .collect();
            assert_eq!(ticks, vec![Tick(4), Tick(5), Tick(6), Tick(7)]);
            let seqs: Vec<u64> = point.samples.iter().map(|sample| sample.seq).collect();
            assert_eq!(seqs, vec![4, 5, 6, 7]);
        }
        // Point 30 is mapped but never written: listed, yet empty.
        assert!(history[2].samples.is_empty());

        // `since` keeps only newer samples; eviction shows up as a
        // numbering gap — seq 3 is gone, not silently present.
        let recent = client.history(&[PointId(10)], 5).unwrap();
        assert_eq!(
            recent[0]
                .samples
                .iter()
                .map(|sample| sample.seq)
                .collect::<Vec<_>>(),
            vec![6, 7]
        );
        let gapped = client.history(&[PointId(10)], 2).unwrap();
        assert_eq!(gapped[0].samples.first().unwrap().seq, 4);
    });
}

#[test]
fn quality_fault_is_journaled_at_the_fault_tick() {
    with_monitor(|driver, client| {
        client.advance(2).unwrap();
        // Each point's first observed sample journals its initial quality.
        assert_eq!(
            client.journal(0).unwrap(),
            vec![
                JournalEntry {
                    seq: 1,
                    tick: Tick(1),
                    event: JournalEvent::QualityChanged {
                        point: PointId(10),
                        from: None,
                        to: Quality::Good,
                    },
                },
                JournalEntry {
                    seq: 2,
                    tick: Tick(1),
                    event: JournalEvent::QualityChanged {
                        point: PointId(20),
                        from: None,
                        to: Quality::Good,
                    },
                },
            ]
        );

        driver.faults.lock().unwrap().insert(PointId(10));
        client.advance(1).unwrap();
        assert_eq!(
            client.journal(2).unwrap(),
            vec![JournalEntry {
                seq: 3,
                tick: Tick(3),
                event: JournalEvent::QualityChanged {
                    point: PointId(10),
                    from: Some(Quality::Good),
                    to: Quality::Bad(QualityReason::CommunicationFault),
                },
            }]
        );

        driver.faults.lock().unwrap().remove(&PointId(10));
        client.advance(1).unwrap();
        assert_eq!(
            client.journal(3).unwrap(),
            vec![JournalEntry {
                seq: 4,
                tick: Tick(4),
                event: JournalEvent::QualityChanged {
                    point: PointId(10),
                    from: Some(Quality::Bad(QualityReason::CommunicationFault)),
                    to: Quality::Good,
                },
            }]
        );
    });
}

#[test]
fn commands_are_journaled_with_their_final_outcomes() {
    with_monitor(|driver, client| {
        client.advance(2).unwrap();
        let base_seq = client.journal(0).unwrap().last().unwrap().seq;

        // Rejected at submission: journaled immediately at the run's
        // current tick, with the named reason inside the receipt.
        let rejected = client
            .command(&write_value(99, ValueKind::Float, Value::Float(1.0)))
            .unwrap();
        assert_eq!(
            client.journal(base_seq).unwrap(),
            vec![JournalEntry {
                seq: base_seq + 1,
                tick: Tick(2),
                event: JournalEvent::CommandSettled {
                    receipt: rejected.clone()
                },
            }]
        );
        assert_eq!(
            rejected.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::UnknownPoint { point: PointId(99) }
            }
        );

        // Accepted: nothing is journaled until a scan settles it, then the
        // entry carries the applied receipt at the applying tick.
        let accepted = client
            .command(&write_value(10, ValueKind::Float, Value::Float(5.0)))
            .unwrap();
        assert!(client.journal(base_seq + 1).unwrap().is_empty());
        client.advance(1).unwrap();
        assert_eq!(
            client.journal(base_seq + 1).unwrap(),
            vec![JournalEntry {
                seq: base_seq + 2,
                tick: Tick(3),
                event: JournalEvent::CommandSettled {
                    receipt: CommandReceipt {
                        command: accepted.command,
                        outcome: CommandOutcome::Applied { tick: Tick(3) },
                        actor: None,
                    },
                },
            }]
        );

        // Refused by the driver at the scan boundary: journaled with the
        // named driver reason at that scan's tick, ahead of the quality
        // transition the faulted input read produces in the same scan.
        driver.faults.lock().unwrap().insert(PointId(10));
        let receipt = client
            .command(&write_value(10, ValueKind::Float, Value::Float(9.0)))
            .unwrap();
        client.advance(1).unwrap();
        let settled = client
            .journal(base_seq + 2)
            .unwrap()
            .into_iter()
            .find(|entry| matches!(entry.event, JournalEvent::CommandSettled { .. }))
            .expect("the boundary rejection is journaled");
        assert_eq!(settled.tick, Tick(4));
        assert_eq!(
            settled.event,
            JournalEvent::CommandSettled {
                receipt: CommandReceipt {
                    command: receipt.command,
                    outcome: CommandOutcome::Rejected {
                        reason: CommandError::DriverRejected {
                            point: PointId(10),
                            error: IoError::Disconnected(PointId(10)),
                        },
                    },
                    actor: None,
                },
            }
        );
    });
}

#[test]
fn step_failures_are_journaled_in_scan_order() {
    with_monitor_config(
        MonitorConfig::default(),
        vec![Box::new(Scale), Box::new(Fragile)],
        |driver, client| {
            client.advance(1).unwrap();
            driver.write(PointId(10), Value::Float(-1.0)).unwrap();
            client.advance(1).unwrap();
            let failure = client
                .journal(0)
                .unwrap()
                .into_iter()
                .find(|entry| matches!(entry.event, JournalEvent::StepFailed { .. }))
                .expect("a step failure is journaled");
            assert_eq!(failure.tick, Tick(2));
            assert_eq!(
                failure.event,
                JournalEvent::StepFailed {
                    component: "fragile".to_string(),
                    error: "input below zero".to_string(),
                }
            );
        },
    );
}

#[test]
fn journal_is_bounded_with_visible_eviction() {
    let config = MonitorConfig {
        history_capacity: 8,
        journal_capacity: 3,
        ..MonitorConfig::default()
    };
    with_monitor_config(config, vec![Box::new(Scale)], |driver, client| {
        client.advance(1).unwrap();
        driver.faults.lock().unwrap().insert(PointId(10));
        client.advance(1).unwrap();
        driver.faults.lock().unwrap().remove(&PointId(10));
        client.advance(1).unwrap();
        let journal = client.journal(0).unwrap();
        assert_eq!(
            journal.iter().map(|entry| entry.seq).collect::<Vec<_>>(),
            vec![2, 3, 4]
        );
        // A stale cursor simply sees the evicted stretch as a gap.
        assert_eq!(client.journal(1).unwrap().len(), 3);
    });
}

#[test]
fn identical_scripted_runs_produce_identical_history_and_journal() {
    let run = || {
        with_monitor(|driver, client| {
            driver.write(PointId(10), Value::Float(2.0)).unwrap();
            client.advance(2).unwrap();
            client
                .command(&write_value(10, ValueKind::Float, Value::Float(5.0)))
                .unwrap();
            client
                .command(&write_value(99, ValueKind::Float, Value::Float(0.0)))
                .unwrap();
            client.advance(1).unwrap();
            driver.faults.lock().unwrap().insert(PointId(10));
            client.advance(1).unwrap();
            driver.faults.lock().unwrap().remove(&PointId(10));
            client.advance(1).unwrap();
            (
                serde_json::to_string(&client.history(&[], 0).unwrap()).unwrap(),
                serde_json::to_string(&client.journal(0).unwrap()).unwrap(),
            )
        })
    };

    assert_eq!(run(), run());
}
