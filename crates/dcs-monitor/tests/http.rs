//! End-to-end tests driving a real `Monitor` over TCP through the
//! in-process `MonitorClient`.

use dcs_core::{
    Command, CommandError, CommandOutcome, CommandReceipt, CyclicIoDriver, Direction,
    DriverDiagnostics, EmittedEvent, EventValue, ExchangeDiagnostics, ForcedPoint, IoDriver,
    IoError, IoFault, IoHealth, JournalEvent, LinkState, PointId, Quality, QualityReason, Sample,
    Tick, Value, ValueKind,
};
use dcs_model::{PlantModel, SignalIndex};
use dcs_monitor::{Monitor, MonitorClient, PAGE, PAIR_FAULT_KINDS_VERSION, PairFaultKind};
use dcs_runtime::{
    Component, ComponentIo, ComponentIoExt, Executor, IoRequirement, PointMap, StepError,
};
use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::Mutex;
use std::sync::atomic::{AtomicU64, Ordering};
use std::thread;

/// In-memory driver stub with injectable faults; the same minimal stand-in
/// the executor tests use — `dcs-monitor` sees only the `IoDriver`
/// contract.
struct StubDriver {
    points: Mutex<HashMap<PointId, Sample>>,
    faults: Mutex<HashSet<PointId>>,
    report: Mutex<Option<DriverDiagnostics>>,
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
            report: Mutex::new(None),
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

    /// The optional transport-diagnostics hook: injectable, so a test
    /// can report link degradation the way a dead plant server's driver
    /// does — named link health, distinct from per-point faults.
    fn diagnostics(&self) -> Option<DriverDiagnostics> {
        self.report.lock().unwrap().clone()
    }
}

/// What the stub's next `exchange` does — the scripted transport.
#[derive(Clone, Copy)]
enum Exchange {
    /// A clean exchange: the staged output image publishes and the
    /// input image latches at the exchange's tick.
    Complete,
    /// Complete but past its deadline: the exchange succeeds and counts
    /// a missed deadline.
    Late,
    /// Completed short of the working counter: the image still latches
    /// and the shortfall counts a mismatch.
    Short,
    /// Did not complete: nothing publishes or latches, the staged image
    /// is retained, and the miss counts once at the read boundary.
    Failed,
}

/// A minimal scripted [`CyclicIoDriver`] — the smallest carrier of the
/// exchange-diagnostics surface the I/O-health pane renders. `exchange`
/// is the only call touching the simulated field: `read` serves the
/// input image the last completed exchange latched and `write` stages
/// the pending output image, per the cyclic contract.
struct CyclicStub {
    /// Every point the process image covers, with its declared kind.
    points: HashMap<PointId, ValueKind>,
    state: Mutex<CyclicState>,
}

/// The stub's mutable bus and counters.
struct CyclicState {
    /// The simulated field — the only data `exchange` may move.
    field: HashMap<PointId, Sample>,
    /// The held input image the last completed exchange latched.
    latched: HashMap<PointId, Sample>,
    /// The pending output image `write` stages.
    staged: HashMap<PointId, Value>,
    /// The scripted exchange outcomes, consumed in order; an exhausted
    /// script completes cleanly.
    script: VecDeque<Exchange>,
    /// Consecutive uncompleted exchanges — while any stands, the link
    /// reports disconnected.
    misses: u64,
    /// The exchange counters `diagnostics` reports.
    attempted: u64,
    succeeded: u64,
    mismatches: u64,
    missed_deadlines: u64,
    last_exchange_tick: Option<Tick>,
    last_error: Option<String>,
}

impl CyclicStub {
    fn new(points: &[(u64, ValueKind)], script: &[Exchange]) -> Self {
        let points: HashMap<PointId, ValueKind> = points
            .iter()
            .map(|&(point, kind)| (PointId(point), kind))
            .collect();
        // The input image starts as the field's initial contents — as a
        // pre-run exchange would leave it.
        let field: HashMap<PointId, Sample> = points
            .keys()
            .map(|&point| (point, Sample::good(Value::Float(0.0), Tick::ZERO)))
            .collect();
        Self {
            points,
            state: Mutex::new(CyclicState {
                latched: field.clone(),
                field,
                staged: HashMap::new(),
                script: script.iter().copied().collect(),
                misses: 0,
                attempted: 0,
                succeeded: 0,
                mismatches: 0,
                missed_deadlines: 0,
                last_exchange_tick: None,
                last_error: None,
            }),
        }
    }
}

impl IoDriver for CyclicStub {
    /// Serves the held input image — never the transport.
    fn read(&self, point: PointId) -> Result<Sample, IoError> {
        self.state
            .lock()
            .unwrap()
            .latched
            .get(&point)
            .copied()
            .ok_or(IoError::UnknownPoint(point))
    }

    /// Stages the pending output image — never the transport; the
    /// `UnknownPoint`/`TypeMismatch` semantics are unchanged.
    fn write(&self, point: PointId, value: Value) -> Result<(), IoError> {
        let kind = *self
            .points
            .get(&point)
            .ok_or(IoError::UnknownPoint(point))?;
        if value.kind() != kind {
            return Err(IoError::TypeMismatch {
                point,
                expected: kind,
                found: value,
            });
        }
        self.state.lock().unwrap().staged.insert(point, value);
        Ok(())
    }

    /// The cyclic half of the I/O-health surface: the exchange counters
    /// and the link state — disconnected while a miss stands, with the
    /// last shortfall or failure's description.
    fn diagnostics(&self) -> Option<DriverDiagnostics> {
        let state = self.state.lock().unwrap();
        Some(DriverDiagnostics {
            link: if state.misses > 0 {
                LinkState::Disconnected
            } else {
                LinkState::Connected
            },
            last_error: state.last_error.clone(),
            exchange: Some(ExchangeDiagnostics {
                attempted: state.attempted,
                succeeded: state.succeeded,
                working_counter_mismatches: state.mismatches,
                last_exchange_tick: state.last_exchange_tick,
                missed_deadlines: state.missed_deadlines,
            }),
        })
    }

    /// The stub carries the cyclic surface.
    fn cyclic(&self) -> Option<&(dyn CyclicIoDriver + Sync)> {
        Some(self)
    }
}

impl CyclicIoDriver for CyclicStub {
    /// One scripted exchange for `tick`: a completed outcome publishes
    /// the staged output image and latches the field into the input
    /// image stamped at `tick`; `Failed` moves nothing and counts the
    /// miss the executor records once at the boundary.
    fn exchange(&self, tick: Tick) -> Result<(), IoError> {
        let mut state = self.state.lock().unwrap();
        state.attempted += 1;
        match state.script.pop_front().unwrap_or(Exchange::Complete) {
            Exchange::Failed => {
                state.misses += 1;
                state.last_error = Some("the exchange did not complete".to_string());
                Err(IoError::Disconnected(
                    *self.points.keys().min().expect("a nonempty image"),
                ))
            }
            outcome => {
                let staged = std::mem::take(&mut state.staged);
                for (point, value) in staged {
                    state.field.insert(point, Sample::good(value, tick));
                }
                state.latched = state.field.clone();
                state.misses = 0;
                state.succeeded += 1;
                state.last_exchange_tick = Some(tick);
                match outcome {
                    Exchange::Late => state.missed_deadlines += 1,
                    Exchange::Short => {
                        state.mismatches += 1;
                        state.last_error =
                            Some("answered short of its working counter".to_string());
                    }
                    _ => {}
                }
                Ok(())
            }
        }
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

/// The same rig over a scripted cyclic driver — the fixture whose
/// served snapshot carries the `driver.exchange` diagnostics the
/// I/O-health pane renders.
fn with_cyclic_monitor<T>(script: &[Exchange], body: impl FnOnce(&MonitorClient) -> T) -> T {
    let driver = CyclicStub::new(
        &[
            (10, ValueKind::Float),
            (20, ValueKind::Float),
            (30, ValueKind::Float),
        ],
        script,
    );
    let map = PointMap::new()
        .with_writable_point(PointId(10), Direction::In, ValueKind::Float)
        .with_point(PointId(20), Direction::Out, ValueKind::Float)
        .with_point(PointId(30), Direction::Out, ValueKind::Float);
    let executor = Executor::new(&driver, map, vec![Box::new(Scale)]).unwrap();
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

/// The pane's rows and verdict, mirroring the page's `renderIoHealth`
/// over a landed snapshot's `io_health`: the driver's link and — for a
/// cyclic driver — exchange counters beside the executor's boundary
/// counters, the attributed last fault, and the overrun count; troubled
/// on link degradation, any boundary failure, exchange mismatches or
/// missed deadlines, an attributed fault, or overruns. The fault row's
/// text stands in for the page's describeIoError/pointName rendering —
/// the row's presence, not its exact wording, is the contract here.
fn io_health_pane(health: &IoHealth) -> (Vec<(String, String)>, bool) {
    let mut rows: Vec<(String, String)> = Vec::new();
    let mut troubled = false;
    if let Some(driver) = &health.driver {
        rows.push((
            "link".to_string(),
            serde_json::to_value(driver.link)
                .unwrap()
                .as_str()
                .unwrap()
                .to_string(),
        ));
        if driver.link != LinkState::Connected {
            troubled = true;
        }
        if let Some(error) = &driver.last_error {
            rows.push(("last link failure".to_string(), error.clone()));
        }
        if let Some(exchange) = &driver.exchange {
            rows.push((
                "exchanges attempted/succeeded".to_string(),
                format!("{}/{}", exchange.attempted, exchange.succeeded),
            ));
            rows.push((
                "working-counter mismatches".to_string(),
                exchange.working_counter_mismatches.to_string(),
            ));
            rows.push((
                "missed exchange deadlines".to_string(),
                exchange.missed_deadlines.to_string(),
            ));
            rows.push((
                "last exchange tick".to_string(),
                exchange
                    .last_exchange_tick
                    .map(|tick| tick.0.to_string())
                    .unwrap_or_else(|| "none".to_string()),
            ));
            if exchange.working_counter_mismatches > 0 || exchange.missed_deadlines > 0 {
                troubled = true;
            }
        }
    }
    rows.push(("failed reads".to_string(), health.failed_reads.to_string()));
    rows.push((
        "failed writes".to_string(),
        health.failed_writes.to_string(),
    ));
    let exchange_reported = health
        .driver
        .as_ref()
        .is_some_and(|driver| driver.exchange.is_some());
    if health.failed_exchanges > 0 || exchange_reported {
        rows.push((
            "failed exchanges".to_string(),
            health.failed_exchanges.to_string(),
        ));
        if health.failed_exchanges > 0 {
            troubled = true;
        }
    }
    rows.push((
        "consecutive failures".to_string(),
        health.consecutive_failures.to_string(),
    ));
    if health.failed_reads > 0 || health.failed_writes > 0 {
        troubled = true;
    }
    if let Some(fault) = &health.last_error {
        rows.push((
            "last I/O fault".to_string(),
            format!(
                "{} — {} boundary, point {}, tick {}",
                fault.error,
                if fault.direction == Direction::In {
                    "read"
                } else {
                    "write"
                },
                fault.point.0,
                fault.tick.0
            ),
        ));
        troubled = true;
    }
    if health.scan_overruns > 0 {
        rows.push((
            "scan overruns".to_string(),
            health.scan_overruns.to_string(),
        ));
        troubled = true;
    }
    (rows, troubled)
}

/// The pair card's I/O-health line for a landed snapshot, mirroring the
/// page's `ioHealthLine`: the pane's troubled rule reduced to one
/// line's worth of named troubles.
fn io_health_card_line(health: &IoHealth) -> (String, bool) {
    let mut troubles = Vec::new();
    if let Some(driver) = &health.driver
        && driver.link != LinkState::Connected
    {
        troubles.push(format!(
            "link {}",
            serde_json::to_value(driver.link).unwrap().as_str().unwrap()
        ));
    }
    if health.failed_reads > 0 || health.failed_writes > 0 {
        troubles.push(format!(
            "{} failed read(s), {} failed write(s)",
            health.failed_reads, health.failed_writes
        ));
    }
    if health.failed_exchanges > 0 {
        troubles.push(format!("{} failed exchange(s)", health.failed_exchanges));
    }
    if let Some(exchange) = health
        .driver
        .as_ref()
        .and_then(|driver| driver.exchange.as_ref())
    {
        if exchange.working_counter_mismatches > 0 {
            troubles.push(format!(
                "{} working-counter mismatch(es)",
                exchange.working_counter_mismatches
            ));
        }
        if exchange.missed_deadlines > 0 {
            troubles.push(format!(
                "{} missed exchange deadline(s)",
                exchange.missed_deadlines
            ));
        }
    }
    if let Some(fault) = &health.last_error {
        troubles.push(format!(
            "last fault {} at tick {}",
            fault.error, fault.tick.0
        ));
    }
    if health.scan_overruns > 0 {
        troubles.push(format!("{} scan overrun(s)", health.scan_overruns));
    }
    if troubles.is_empty() {
        ("I/O healthy".to_string(), false)
    } else {
        (format!("I/O degraded: {}", troubles.join("; ")), true)
    }
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
                actor: None,
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
                actor: None,
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
        assert_eq!(input.direction, Direction::In);
        assert_eq!(input.value_type, ValueKind::Float);
        // The signal's display group rides the metadata endpoint.
        assert_eq!(input.group.as_deref(), Some("reactor"));

        let output = index.get(PointId(20)).unwrap();
        assert_eq!(output.name, "heater-command");
        assert_eq!(output.unit.as_deref(), Some("%"));
        assert_eq!(output.direction, Direction::Out);
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

/// The wire spelling serde emits for a unit or externally tagged enum
/// variant: the bare string for a unit variant, the sole object key for
/// a tagged one.
fn emitted_spelling(value: &impl serde::Serialize) -> String {
    match serde_json::to_value(value).unwrap() {
        serde_json::Value::String(name) => name,
        serde_json::Value::Object(map) => map.keys().next().unwrap().clone(),
        other => panic!(
            "an externally tagged variant serializes as a string or single-key object: {other}"
        ),
    }
}

#[test]
fn the_pages_hardcoded_spellings_are_the_emitted_contract() {
    with_monitor(|_driver, client| {
        let page = client.page().unwrap();
        // Every name the page pattern-matches or constructs is the
        // audited enum's canonical snake_case spelling — derived from
        // serde itself so the page and the rename rules cannot fork.
        for spelling in [
            emitted_spelling(&Value::Bool(true)),
            emitted_spelling(&Value::Int(0)),
            emitted_spelling(&Value::Float(0.0)),
            emitted_spelling(&ValueKind::Bool),
            emitted_spelling(&ValueKind::Int),
            emitted_spelling(&ValueKind::Float),
            emitted_spelling(&Quality::Good),
            emitted_spelling(&Quality::Uncertain(QualityReason::Substituted)),
            emitted_spelling(&Quality::Bad(QualityReason::Substituted)),
            emitted_spelling(&QualityReason::Substituted),
            emitted_spelling(&IoError::UnknownPoint(PointId(0))),
            emitted_spelling(&IoError::Disconnected(PointId(0))),
            emitted_spelling(&IoError::Timeout(PointId(0))),
            emitted_spelling(&IoError::Fenced(PointId(0))),
            emitted_spelling(&IoError::TypeMismatch {
                point: PointId(0),
                expected: ValueKind::Bool,
                found: Value::Bool(false),
            }),
            // The declared-command/emitted-event vocabulary the journal
            // pane renders.
            emitted_spelling(&Command::Invoke {
                component: String::new(),
                command: String::new(),
                arguments: Default::default(),
            }),
            emitted_spelling(&JournalEvent::EventEmitted {
                event: EmittedEvent {
                    event: String::new(),
                    component: String::new(),
                    fields: Default::default(),
                },
            }),
            emitted_spelling(&EventValue::Value(Value::Int(0))),
            emitted_spelling(&EventValue::Quality(Quality::Good)),
            emitted_spelling(&EventValue::Text(String::new())),
            emitted_spelling(&CommandError::UnknownCommand {
                component: String::new(),
                command: String::new(),
            }),
            emitted_spelling(&CommandError::ArgumentTypeMismatch {
                component: String::new(),
                command: String::new(),
                argument: String::new(),
                expected: ValueKind::Int,
                found: ValueKind::Bool,
            }),
            emitted_spelling(&CommandError::CommandRefused {
                component: String::new(),
                command: String::new(),
                reason: String::new(),
            }),
        ] {
            assert!(
                page.contains(&spelling),
                "page lacks the emitted spelling {spelling}"
            );
        }
        // The legacy PascalCase spellings the read aliases keep
        // deserializable survive nowhere as wire operands — only as
        // display output the `pascal` helper computes at render time.
        for legacy in [
            "Bool",
            "Int",
            "Float",
            "Good",
            "Uncertain",
            "Bad",
            "Unspecified",
            "Substituted",
            "Stale",
            "OutOfRange",
            "CommunicationFault",
            "DeviceFault",
            "ConfigurationFault",
            "UnknownPoint",
            "Disconnected",
            "Timeout",
            "TypeMismatch",
            "Fenced",
        ] {
            for operand in [
                format!("=== \"{legacy}\""),
                format!("=== '{legacy}'"),
                format!("\"{legacy}\" in "),
                format!(" {{ {legacy}: "),
            ] {
                assert!(
                    !page.contains(&operand),
                    "page still matches the legacy spelling: {operand}"
                );
            }
        }
    });
}

#[test]
fn the_pages_pair_fault_kinds_match_the_versioned_contract() {
    let mut spellings: Vec<_> = PairFaultKind::ALL.iter().map(emitted_spelling).collect();
    spellings.sort();
    assert_eq!(
        spellings,
        [
            "dual_active",
            "field_unclaimed",
            "no_active_peer",
            "peer_unreachable",
            "standby_degraded",
            "standby_diverged",
            "standby_orphaned",
            "standby_unsynchronized_past_grace",
        ]
    );
    assert_eq!(PAIR_FAULT_KINDS_VERSION, 3);

    with_monitor(|_driver, client| {
        let page = client.page().unwrap();
        let compact: String = page.chars().filter(|c| !c.is_whitespace()).collect();
        assert!(
            compact.contains("constPAIR_FAULT_KINDS_VERSION=3;"),
            "page lacks the version constant"
        );
        assert!(
            compact.contains("fault_kinds_version:PAIR_FAULT_KINDS_VERSION"),
            "page lacks the versioned fault_kinds field"
        );
        for spelling in spellings {
            assert!(
                page.contains(&format!("\"{spelling}\"")),
                "page lacks the emitted pair fault spelling {spelling}"
            );
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
                Some(r#"{"write_value":{"point":10,"kind":"float","value":{"float":7.5}}}"#),
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
        // The I/O-health pane: the snapshot's io_health section rendered
        // as its own surface beside the pair reporting — and the
        // link-degradation display rule that shows a dead transport as
        // link health, distinct from the per-point quality column. The
        // cyclic contract's half: the pane's exchange rows and boundary
        // count, and the pair card's named exchange troubles.
        for needle in [
            "id=\"health\"",
            "id=\"io-health\"",
            "id=\"io-health-rows\"",
            "snapshot.io_health",
            "health.driver.link !== \"connected\"",
            "scan overruns",
            "health.driver.exchange",
            "health.failed_exchanges",
            "exchanges attempted/succeeded",
            "working-counter mismatches",
            "missed exchange deadlines",
            "last exchange tick",
            "failed exchanges",
            " failed exchange(s)",
            "working-counter mismatch(es)",
            "missed exchange deadline(s)",
        ] {
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
                    actor: None,
                },
            }
        );
    });
}

#[test]
fn the_health_panes_fields_ride_the_served_snapshot() {
    with_monitor(|driver, client| {
        // Injected driver faults at both boundaries, plus a link-level
        // report through the driver's optional diagnostics hook — the
        // dead plant server's signature: named link degradation beside
        // the per-point faults the executor counts.
        *driver.report.lock().unwrap() = Some(DriverDiagnostics {
            link: LinkState::Disconnected,
            last_error: Some("no live connection to the plant server".to_string()),
            exchange: None,
        });
        driver.faults.lock().unwrap().insert(PointId(10));
        driver.faults.lock().unwrap().insert(PointId(20));
        // The failed output write degrades the scan — the health
        // section counted and attributed it while the run continues.
        client.advance(1).unwrap();

        // Every field the pane reads is present in the served snapshot.
        let health = &client.snapshot().unwrap().io_health;
        assert_eq!(health.failed_reads, 1);
        assert_eq!(health.failed_writes, 1);
        assert_eq!(health.consecutive_failures, 2);
        assert_eq!(
            health.last_error,
            Some(IoFault {
                tick: Tick(1),
                point: PointId(20),
                direction: Direction::Out,
                error: IoError::Disconnected(PointId(20)),
            })
        );
        assert_eq!(
            health.driver,
            Some(DriverDiagnostics {
                link: LinkState::Disconnected,
                last_error: Some("no live connection to the plant server".to_string()),
                exchange: None,
            })
        );
        assert_eq!(health.scan_overruns, 0);

        // The payloads serde-roundtrip per the #83 contract.
        let json = serde_json::to_string(health).unwrap();
        assert_eq!(
            serde_json::from_str::<dcs_core::IoHealth>(&json).unwrap(),
            health.clone()
        );
    });
}

#[test]
fn the_pane_renders_the_cyclic_exchange_surface() {
    // A driver carrying the cyclic contract: the script's one failed
    // exchange counts once at the executor's boundary while the late
    // frame counts a missed deadline under succeeded — both halves of
    // the surface land in the served snapshot.
    with_cyclic_monitor(
        &[
            Exchange::Complete,
            Exchange::Failed,
            Exchange::Late,
            Exchange::Complete,
        ],
        |client| {
            client.advance(4).unwrap();
            let health = client.snapshot().unwrap().io_health;

            // The executor's boundary count and the driver's exchange
            // counters — the fields the pane's new rows read.
            assert_eq!(health.failed_exchanges, 1);
            let exchange = health
                .driver
                .as_ref()
                .and_then(|driver| driver.exchange.as_ref())
                .expect("the cyclic driver reports its exchange counters");
            assert_eq!(
                *exchange,
                ExchangeDiagnostics {
                    attempted: 4,
                    succeeded: 3,
                    working_counter_mismatches: 0,
                    last_exchange_tick: Some(Tick(4)),
                    missed_deadlines: 1,
                }
            );

            // The pane renders the exchange rows beside the boundary
            // counters and reads degraded.
            let (rows, troubled) = io_health_pane(&health);
            let rendered: HashMap<&str, &str> = rows
                .iter()
                .map(|(label, value)| (label.as_str(), value.as_str()))
                .collect();
            assert_eq!(rendered["exchanges attempted/succeeded"], "4/3");
            assert_eq!(rendered["working-counter mismatches"], "0");
            assert_eq!(rendered["missed exchange deadlines"], "1");
            assert_eq!(rendered["last exchange tick"], "4");
            assert_eq!(rendered["failed exchanges"], "1");
            assert!(troubled, "the pane reads degraded: {rows:?}");

            // The pair card's line names the troubles.
            let (line, bad) = io_health_card_line(&health);
            assert!(bad);
            assert!(line.contains("1 failed exchange(s)"), "{line}");
            assert!(line.contains("1 missed exchange deadline(s)"), "{line}");

            // Identical scripted snapshots render identically — a serde
            // roundtrip produces the same rows and verdict.
            let again: IoHealth =
                serde_json::from_str(&serde_json::to_string(&health).unwrap()).unwrap();
            assert_eq!(io_health_pane(&again), io_health_pane(&health));
        },
    );
}

#[test]
fn the_troubled_rule_reads_the_exchange_counters() {
    // A clean run — every attempted exchange succeeded with no
    // shortfall and no missed deadline — stays healthy even though the
    // exchange rows render.
    with_cyclic_monitor(&[Exchange::Complete, Exchange::Complete], |client| {
        client.advance(2).unwrap();
        let health = client.snapshot().unwrap().io_health;
        let exchange = health
            .driver
            .as_ref()
            .and_then(|driver| driver.exchange.as_ref())
            .unwrap();
        assert_eq!(exchange.succeeded, exchange.attempted);
        let (rows, troubled) = io_health_pane(&health);
        assert!(
            rows.iter()
                .any(|(label, _)| label == "exchanges attempted/succeeded"),
            "the exchange surface renders even while healthy: {rows:?}"
        );
        assert!(!troubled, "clean exchanges read healthy: {rows:?}");
        let (line, bad) = io_health_card_line(&health);
        assert_eq!((line.as_str(), bad), ("I/O healthy", false));
    });

    // A completed exchange can still degrade the field: the late frame
    // counted under succeeded — succeeded == attempted — yet its missed
    // deadline trips the rule.
    with_cyclic_monitor(&[Exchange::Complete, Exchange::Late], |client| {
        client.advance(2).unwrap();
        let health = client.snapshot().unwrap().io_health;
        let exchange = health
            .driver
            .as_ref()
            .and_then(|driver| driver.exchange.as_ref())
            .unwrap();
        assert_eq!(exchange.succeeded, exchange.attempted);
        assert_eq!(exchange.missed_deadlines, 1);
        assert_eq!(health.failed_exchanges, 0);
        let (_, troubled) = io_health_pane(&health);
        assert!(troubled, "a missed deadline reads degraded");
        let (line, bad) = io_health_card_line(&health);
        assert!(bad);
        assert!(line.contains("missed exchange deadline"), "{line}");
    });

    // The working-counter shortfall likewise: the exchange completed
    // and counted under succeeded, but the named shortfall trips the
    // rule.
    with_cyclic_monitor(&[Exchange::Complete, Exchange::Short], |client| {
        client.advance(2).unwrap();
        let health = client.snapshot().unwrap().io_health;
        let exchange = health
            .driver
            .as_ref()
            .and_then(|driver| driver.exchange.as_ref())
            .unwrap();
        assert_eq!(exchange.succeeded, exchange.attempted);
        assert_eq!(exchange.working_counter_mismatches, 1);
        assert_eq!(health.failed_exchanges, 0);
        let (_, troubled) = io_health_pane(&health);
        assert!(troubled, "a working-counter shortfall reads degraded");
        let (line, bad) = io_health_card_line(&health);
        assert!(bad);
        assert!(line.contains("working-counter mismatch"), "{line}");
    });
}

#[test]
fn a_point_wise_driver_renders_no_exchange_rows() {
    // The absent-field convention: a driver without the cyclic surface
    // serializes no `exchange` field and the pane renders exactly as
    // before — no exchange rows, no failed-exchanges counter, and the
    // verdict untouched.
    with_monitor(|_driver, client| {
        client.advance(1).unwrap();
        let health = client.snapshot().unwrap().io_health;
        assert_eq!(health.failed_exchanges, 0);
        assert!(
            health
                .driver
                .as_ref()
                .is_none_or(|driver| driver.exchange.is_none()),
            "a point-wise driver reports no exchange section"
        );
        let (rows, troubled) = io_health_pane(&health);
        assert!(
            !rows.iter().any(|(label, _)| label.contains("exchange")),
            "no exchange row may render for a non-cyclic driver: {rows:?}"
        );
        assert!(!troubled);
        let (line, bad) = io_health_card_line(&health);
        assert_eq!((line.as_str(), bad), ("I/O healthy", false));
    });
}

#[test]
fn force_and_release_are_journaled_and_badged_in_the_snapshot() {
    with_monitor(|driver, client| {
        driver.write(PointId(10), Value::Float(1.0)).unwrap();
        client.advance(1).unwrap();

        // The force submits over the same `/command` surface as a write.
        let force = Command::ForcePoint {
            point: PointId(10),
            kind: ValueKind::Float,
            value: Value::Float(9.0),
        };
        let receipt = client.command(&force).unwrap();
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Accepted {
                apply_tick: Tick(2)
            }
        );

        // At the applying scan the snapshot badges the point: its force
        // entry and the Substituted-stamped sample it reports.
        client.advance(1).unwrap();
        let snapshot = client.snapshot().unwrap();
        assert_eq!(
            snapshot.forces,
            vec![ForcedPoint {
                point: PointId(10),
                value: Value::Float(9.0),
            }]
        );
        let sample = snapshot
            .points
            .iter()
            .find(|telemetry| telemetry.point == PointId(10))
            .and_then(|telemetry| telemetry.sample)
            .unwrap();
        assert_eq!(
            sample,
            Sample::new(
                Value::Float(9.0),
                Quality::Uncertain(QualityReason::Substituted),
                Tick(2),
            )
        );

        // The field moving under the force changes nothing the monitor
        // reports; the release resumes the live read at its boundary.
        driver.write(PointId(10), Value::Float(4.0)).unwrap();
        client.advance(1).unwrap();
        assert_eq!(
            point_value(&client.snapshot().unwrap(), 10),
            Some(Value::Float(9.0))
        );

        let unforce = Command::UnforcePoint { point: PointId(10) };
        let receipt = client.command(&unforce).unwrap();
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Accepted {
                apply_tick: Tick(4)
            }
        );
        client.advance(1).unwrap();
        assert_eq!(
            point_value(&client.snapshot().unwrap(), 10),
            Some(Value::Float(4.0))
        );
        assert!(client.snapshot().unwrap().forces.is_empty());

        // Both halves are journaled as settled commands at their ticks —
        // the force's application carrying the quality transition to
        // Substituted beside it.
        let journal = client.journal(0).unwrap();
        let json = serde_json::to_string(&journal).unwrap();
        let journal: Vec<dcs_core::JournalEntry> = serde_json::from_str(&json).unwrap();

        let settled: Vec<CommandReceipt> = journal
            .iter()
            .filter_map(|entry| match &entry.event {
                JournalEvent::CommandSettled { receipt } => Some(receipt.clone()),
                _ => None,
            })
            .collect();
        assert_eq!(
            settled,
            vec![
                CommandReceipt {
                    command: force,
                    outcome: CommandOutcome::Applied { tick: Tick(2) },
                    actor: None,
                },
                CommandReceipt {
                    command: unforce,
                    outcome: CommandOutcome::Applied { tick: Tick(4) },
                    actor: None,
                },
            ]
        );
        assert!(journal.iter().any(|entry| {
            entry.event
                == JournalEvent::QualityChanged {
                    point: PointId(10),
                    from: Some(Quality::Good),
                    to: Quality::Uncertain(QualityReason::Substituted),
                }
        }));
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
        assert_eq!(monitor.paced_scan(), Tick(1));
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
        monitor.paced_scan();
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
fn the_served_receipt_log_stays_bounded_while_the_journal_keeps_the_audit() {
    // The QA finding's surface end to end: `GET /receipts` and the
    // checkpoint the standby pulls flatten at the declared bound once
    // settled receipts outnumber it — the journal keeps every
    // settlement, and pending entries are never evicted.
    let driver = StubDriver::new(&[
        (PointId(10), Value::Float(0.0)),
        (PointId(20), Value::Float(0.0)),
        (PointId(30), Value::Float(0.0)),
    ]);
    let map = PointMap::new()
        .with_writable_point(PointId(10), Direction::In, ValueKind::Float)
        .with_point(PointId(20), Direction::Out, ValueKind::Float)
        .with_point(PointId(30), Direction::Out, ValueKind::Float);
    let executor = Executor::new(&driver, map, vec![Box::new(Scale)])
        .unwrap()
        .with_receipt_log_capacity(4);
    let monitor = Monitor::bind("127.0.0.1:0", executor, signal_index()).unwrap();
    let client = MonitorClient::new(monitor.local_addr());
    thread::scope(|scope| {
        scope.spawn(|| monitor.serve());

        // Six settled commands against a bound of four: the served log
        // and the checkpoint carry the newest four only, the eviction
        // gap reading through `attempts`.
        for value in 0..6 {
            let receipt = client
                .command(&write_value(
                    10,
                    ValueKind::Float,
                    Value::Float(value as f64),
                ))
                .unwrap();
            assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
            client.advance(1).unwrap();
        }
        assert_eq!(client.receipts().unwrap().len(), 4);
        let checkpoint = client.checkpoint().unwrap();
        assert_eq!(checkpoint.receipts.len(), 4);
        assert_eq!(checkpoint.receipt_base(), 2);
        assert_eq!(client.snapshot().unwrap().command_queue.attempts, 6);

        // Two more pending submissions: the settled prefix evicts to
        // the bound while both `Accepted` entries stay served.
        for value in 6..8 {
            client
                .command(&write_value(
                    10,
                    ValueKind::Float,
                    Value::Float(value as f64),
                ))
                .unwrap();
        }
        let receipts = client.receipts().unwrap();
        assert_eq!(receipts.len(), 4);
        assert!(matches!(
            receipts[2].outcome,
            CommandOutcome::Accepted { .. }
        ));
        assert!(matches!(
            receipts[3].outcome,
            CommandOutcome::Accepted { .. }
        ));
        client.advance(1).unwrap();
        assert!(
            client
                .receipts()
                .unwrap()
                .iter()
                .all(|receipt| matches!(receipt.outcome, CommandOutcome::Applied { .. }))
        );

        // Eviction dropped no audit: every one of the eight submissions
        // journaled its settlement exactly once.
        let journal = client.journal(0).unwrap();
        let settlements = journal
            .iter()
            .filter(|entry| matches!(entry.event, JournalEvent::CommandSettled { .. }))
            .count();
        assert_eq!(settlements, 8);

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
    monitor.paced_scan();
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
