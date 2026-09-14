//! End-to-end tests driving a real `Monitor` over TCP through the
//! in-process `MonitorClient`.

use dcs_core::{
    Command, CommandError, CommandOutcome, CommandReceipt, Direction, IoDriver, IoError,
    JournalEvent, PointId, Quality, QualityReason, Sample, Tick, Value, ValueKind,
};
use dcs_model::{PlantModel, SignalIndex};
use dcs_monitor::{Monitor, MonitorClient, PAGE};
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

/// The model fixture behind the monitor: points 10/20/30 match the rig's
/// point map, and signals give points 10 and 20 names and units.
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

/// Builds the rig and runs `body` against a serving monitor; the server is
/// shut down before the driver's borrow ends.
fn with_monitor<T>(body: impl FnOnce(&StubDriver, &MonitorClient) -> T) -> T {
    let driver = StubDriver::new(&[
        (PointId(10), Value::Float(0.0)),
        (PointId(20), Value::Float(0.0)),
        (PointId(30), Value::Float(0.0)),
    ]);
    let map = PointMap::new()
        .with_writable_point(PointId(10), Direction::In, ValueKind::Float)
        .with_point(PointId(20), Direction::Out, ValueKind::Float)
        .with_point(PointId(30), Direction::Out, ValueKind::Float);
    let executor = Executor::new(&driver, map, vec![Box::new(Scale)]).unwrap();
    let monitor = Monitor::bind("127.0.0.1:0", executor, signal_index()).unwrap();
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
                command: command.clone(),
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

        // Exactly one receipt per command: the submitted command's entry
        // now reports it applied at tick 2.
        let receipts = client.receipts().unwrap();
        assert_eq!(receipts.len(), 1);
        assert_eq!(
            receipts[0],
            CommandReceipt {
                command,
                outcome: CommandOutcome::Applied { tick: Tick(2) },
            }
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

        // An unmarked or `Out` point is not a command target.
        let receipt = client
            .command(&write_value(30, ValueKind::Float, Value::Float(9.0)))
            .unwrap();
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::NotWritable { point: PointId(30) }
            }
        );

        // Driver rejection surfaces at the scan boundary.
        driver.faults.lock().unwrap().insert(PointId(10));
        let receipt = client
            .command(&write_value(10, ValueKind::Float, Value::Float(9.0)))
            .unwrap();
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        client.advance(1).unwrap();
        assert_eq!(
            client.receipts().unwrap().last().unwrap().outcome,
            CommandOutcome::Rejected {
                reason: CommandError::DriverRejected {
                    point: PointId(10),
                    error: IoError::Disconnected(PointId(10)),
                }
            }
        );
        driver.faults.lock().unwrap().remove(&PointId(10));
        // A component-targeted command roundtrips through the same POST
        // contract and is answered by the same receipt shape.
        let command = Command::SetParameter {
            component: "scale".to_string(),
            name: "gain".to_string(),
            value: Value::Float(4.0),
        };
        let receipt = client.command(&command).unwrap();
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::UnsupportedParameter {
                    component: "scale".to_string(),
                    parameter: "gain".to_string(),
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
fn signals_endpoint_serves_the_models_metadata() {
    with_monitor(|_driver, client| {
        let index = client.signals().unwrap();
        // The served payload is exactly the loaded model's SignalIndex,
        // so it serde-roundtrips over the wire.
        assert_eq!(index, signal_index());
        assert_eq!(index.points.len(), 3);

        let input = index.get(PointId(10)).unwrap();
        assert_eq!(input.name, "reactor-temperature");
        assert_eq!(input.unit.as_deref(), Some("degC"));
        assert_eq!(
            input.description.as_deref(),
            Some("Reactor temperature measurement")
        );
        assert_eq!(input.direction, dcs_model::Direction::In);
        assert_eq!(input.value_type, ValueKind::Float);
        // The signal's display group rides the metadata endpoint.
        assert_eq!(input.group.as_deref(), Some("reactor"));

        let output = index.get(PointId(20)).unwrap();
        assert_eq!(output.name, "heater-command");
        assert_eq!(output.unit.as_deref(), Some("%"));
        assert_eq!(output.direction, dcs_model::Direction::Out);
        assert_eq!(output.group, None);

        // A point no signal sources still gets a default entry.
        let spare = index.get(PointId(30)).unwrap();
        assert_eq!(spare.signal, None);
        assert_eq!(spare.name, "point-30");
        assert_eq!(spare.unit, None);
        assert_eq!(spare.group, None);
    });
}

#[test]
fn monitoring_page_is_served() {
    with_monitor(|_driver, client| {
        assert_eq!(client.page().unwrap(), PAGE);
        for path in ["/", "/index.html"] {
            let (status, body) = client.request("GET", path, None).unwrap();
            assert_eq!(status, 200, "{path}");
            assert!(body.contains("<title>dcs-monitor</title>"), "{path}");
            // The page drives only the JSON contract endpoints.
            for endpoint in ["/signals", "/snapshot", "/history", "/journal", "/command"] {
                assert!(body.contains(endpoint), "{path} lacks {endpoint}");
            }
        }
    });
}

#[test]
fn page_json_feed_tracks_snapshots_and_commands() {
    with_monitor(|driver, client| {
        driver.write(PointId(10), Value::Float(3.0)).unwrap();
        client.advance(1).unwrap();

        // The join the page performs: metadata from /signals names and
        // units each point; /snapshot carries its live sample.
        let index = client.signals().unwrap();
        let snapshot = client.snapshot().unwrap();
        let meta = index.get(PointId(10)).unwrap();
        let telemetry = snapshot
            .points
            .iter()
            .find(|point| point.point == meta.point)
            .unwrap();
        assert_eq!(meta.name, "reactor-temperature");
        assert_eq!(telemetry.sample.unwrap().value, Value::Float(3.0));
        assert_eq!(telemetry.sample.unwrap().tick, Tick(1));

        // The exact body the page's command form serializes is a Command.
        let (status, body) = client
            .request(
                "POST",
                "/command",
                Some(r#"{"write_value":{"point":10,"kind":"Float","value":{"Float":7.5}}}"#),
            )
            .unwrap();
        assert_eq!(status, 200);
        let receipt: CommandReceipt = serde_json::from_str(&body).unwrap();
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Accepted {
                apply_tick: Tick(2)
            }
        );

        // The commanded value is what later snapshots — and so the page —
        // display for that point.
        let snapshot = client.advance(1).unwrap();
        assert_eq!(point_value(&snapshot, 10), Some(Value::Float(7.5)));
        assert_eq!(point_value(&snapshot, 20), Some(Value::Float(15.0)));
    });
}

#[test]
fn page_serves_trend_and_journal_markup() {
    with_monitor(|_driver, client| {
        let page = client.page().unwrap();
        // The trend pane: one inline-SVG figure per point, fed by /history
        // with a since-cursor — dependency-free markup, no build assets.
        for needle in ["id=\"trends\"", "<svg", "/history?since="] {
            assert!(page.contains(needle), "page lacks {needle}");
        }
        // The journal pane: a tick-ordered table fed by /journal with a
        // since-cursor.
        for needle in ["id=\"journal\"", "<th>Tick</th>", "/journal?since="] {
            assert!(page.contains(needle), "page lacks {needle}");
        }
        // The I/O-health line: the snapshot's io_health section rendered
        // as its own surface beside the per-point quality column.
        for needle in ["id=\"io-health\"", "snapshot.io_health"] {
            assert!(page.contains(needle), "page lacks {needle}");
        }
        // The page stays a single dependency-free asset.
        assert!(!page.contains("src="), "page references external assets");
    });
}

#[test]
fn page_groups_its_listing_by_the_models_signal_groups() {
    with_monitor(|_driver, client| {
        let page = client.page().unwrap();
        // The listing is organized client-side from the model's declared
        // display groups: a per-group header row and the bucketing logic.
        for needle in [
            "point-group",
            "function groupedPoints()",
            "function pointGroup(meta)",
        ] {
            assert!(page.contains(needle), "page lacks {needle}");
        }
        // The client-side default: a point whose entry carries no group —
        // like the served index's ungrouped and signal-less points — is
        // filed under the documented "ungrouped" group.
        assert!(
            page.contains("const DEFAULT_GROUP = \"ungrouped\""),
            "page lacks the documented default group"
        );
        assert!(
            page.contains("meta.group || DEFAULT_GROUP"),
            "page lacks the ungrouped fallback"
        );
        let index = client.signals().unwrap();
        assert_eq!(index.get(PointId(20)).unwrap().group, None);
        assert_eq!(index.get(PointId(30)).unwrap().group, None);
    });
}

#[test]
fn trend_and_journal_feeds_track_the_run() {
    with_monitor(|driver, client| {
        driver.write(PointId(10), Value::Float(3.0)).unwrap();
        client.advance(2).unwrap();

        // The payload the trend fetches: the point's retained samples in
        // tick order, roundtripping through the wire format.
        let history = client.history(&[PointId(10)], 0).unwrap();
        assert_eq!(history.len(), 1);
        let json = serde_json::to_string(&history).unwrap();
        let history: Vec<dcs_core::PointHistory> = serde_json::from_str(&json).unwrap();
        let samples = &history[0].samples;
        assert_eq!(
            samples
                .iter()
                .map(|entry| entry.sample.tick)
                .collect::<Vec<_>>(),
            vec![Tick(1), Tick(2)]
        );
        assert_eq!(
            samples
                .iter()
                .map(|entry| entry.sample.value)
                .collect::<Vec<_>>(),
            vec![Value::Float(3.0), Value::Float(3.0)]
        );

        // The trend grows across polls: the next since-cursor fetch
        // returns only the newer samples.
        let seen = samples.last().unwrap().seq;
        driver.write(PointId(10), Value::Float(4.0)).unwrap();
        client.advance(1).unwrap();
        let more = client.history(&[PointId(10)], seen).unwrap();
        assert_eq!(
            more[0]
                .samples
                .iter()
                .map(|entry| (entry.seq, entry.sample))
                .collect::<Vec<_>>(),
            vec![(seen + 1, Sample::good(Value::Float(4.0), Tick(3)))]
        );

        // The journal feed: an injected quality fault at tick 4 and a
        // command rejected at submission, both tick-attributed.
        driver.faults.lock().unwrap().insert(PointId(10));
        client.advance(1).unwrap();
        // The same fault counted on the snapshot's I/O-health surface —
        // named counters and attribution beside the point's bad quality.
        let health = &client.snapshot().unwrap().io_health;
        assert_eq!(health.failed_reads, 1);
        assert_eq!(
            health.last_error.map(|fault| (fault.tick, fault.point)),
            Some((Tick(4), PointId(10)))
        );
        let rejected = client
            .command(&write_value(99, ValueKind::Float, Value::Float(1.0)))
            .unwrap();
        let journal = client.journal(0).unwrap();
        let json = serde_json::to_string(&journal).unwrap();
        let journal: Vec<dcs_core::JournalEntry> = serde_json::from_str(&json).unwrap();

        let fault = journal
            .iter()
            .find(|entry| {
                matches!(
                    entry.event,
                    JournalEvent::QualityChanged {
                        to: Quality::Bad(_),
                        ..
                    }
                )
            })
            .expect("the injected fault is journaled");
        assert_eq!(fault.tick, Tick(4));
        assert_eq!(
            fault.event,
            JournalEvent::QualityChanged {
                point: PointId(10),
                from: Some(Quality::Good),
                to: Quality::Bad(QualityReason::CommunicationFault),
            }
        );

        let rejection = journal
            .iter()
            .find(|entry| matches!(entry.event, JournalEvent::CommandSettled { .. }))
            .expect("the rejected command is journaled");
        assert_eq!(rejection.tick, Tick(4));
        assert_eq!(
            rejection.event,
            JournalEvent::CommandSettled {
                receipt: CommandReceipt {
                    command: rejected.command,
                    outcome: CommandOutcome::Rejected {
                        reason: CommandError::UnknownPoint { point: PointId(99) },
                    },
                },
            }
        );
    });
}

#[test]
fn paced_monitor_scans_through_the_lock_and_refuses_post_scan() {
    // The paced binding a controller uses: the hosting loop drives
    // paced_scan, so POST /scan is refused — the wall clock owns the
    // schedule and an endpoint-driven tick would break it.
    let driver = StubDriver::new(&[
        (PointId(10), Value::Float(3.0)),
        (PointId(20), Value::Float(0.0)),
        (PointId(30), Value::Float(0.0)),
    ]);
    let map = PointMap::new()
        .with_writable_point(PointId(10), Direction::In, ValueKind::Float)
        .with_point(PointId(20), Direction::Out, ValueKind::Float)
        .with_point(PointId(30), Direction::Out, ValueKind::Float);
    let executor = Executor::new(&driver, map, vec![Box::new(Scale)]).unwrap();
    let monitor = Monitor::bind_paced("127.0.0.1:0", executor, signal_index()).unwrap();
    let client = MonitorClient::new(monitor.local_addr());
    thread::scope(|scope| {
        scope.spawn(|| monitor.serve());

        // The paced loop's entry point: the scan runs through the shared
        // lock and is recorded like an endpoint-driven one.
        assert_eq!(monitor.paced_scan(), Ok(Tick(1)));
        assert_eq!(monitor.tick(), Tick(1));
        assert_eq!(client.snapshot().unwrap().tick, Tick(1));
        assert_eq!(
            point_value(&monitor.snapshot(), 20),
            Some(Value::Float(6.0))
        );
        let history = client.history(&[PointId(10)], 0).unwrap();
        assert_eq!(history[0].samples.len(), 1);

        // Externally requested scans are refused under pacing.
        let (status, body) = client
            .request("POST", "/scan", Some(r#"{"scans":1}"#))
            .unwrap();
        assert_eq!(status, 409, "{body}");
        assert!(body.contains("paced"), "{body}");
        assert_eq!(monitor.tick(), Tick(1));

        // Commands still queue for the paced boundary and settle there.
        let receipt = client
            .command(&write_value(10, ValueKind::Float, Value::Float(7.0)))
            .unwrap();
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Accepted {
                apply_tick: Tick(2)
            }
        );
        monitor.paced_scan().unwrap();
        assert_eq!(
            client.receipts().unwrap()[0].outcome,
            CommandOutcome::Applied { tick: Tick(2) }
        );
        assert_eq!(
            point_value(&client.snapshot().unwrap(), 20),
            Some(Value::Float(14.0))
        );

        monitor.shutdown();
    });
}

#[test]
fn the_paced_loops_overrun_feed_counts_into_io_health() {
    // The paced controller loop's documented feed: a cycle that overran
    // its wall-clock period is reported through
    // `Monitor::record_scan_overrun` — under the same lock that
    // serializes scans — and the count surfaces in the snapshot the
    // endpoints serve.
    let driver = StubDriver::new(&[
        (PointId(10), Value::Float(3.0)),
        (PointId(20), Value::Float(0.0)),
        (PointId(30), Value::Float(0.0)),
    ]);
    let map = PointMap::new()
        .with_writable_point(PointId(10), Direction::In, ValueKind::Float)
        .with_point(PointId(20), Direction::Out, ValueKind::Float)
        .with_point(PointId(30), Direction::Out, ValueKind::Float);
    let executor = Executor::new(&driver, map, vec![Box::new(Scale)]).unwrap();
    let monitor = Monitor::bind_paced("127.0.0.1:0", executor, signal_index()).unwrap();

    assert_eq!(monitor.snapshot().io_health.scan_overruns, 0);
    monitor.paced_scan().unwrap();
    monitor.record_scan_overrun();
    monitor.record_scan_overrun();
    let snapshot = monitor.snapshot();
    assert_eq!(snapshot.io_health.scan_overruns, 2);
    // The JSON contract carries the counter as part of the section.
    let json = serde_json::to_string(&snapshot.io_health).unwrap();
    assert_eq!(
        serde_json::from_str::<dcs_core::IoHealth>(&json).unwrap(),
        snapshot.io_health
    );
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
