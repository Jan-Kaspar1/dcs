//! End-to-end tests for the monitoring page's forcing surface: a point
//! forced through the receipted command path lands in the snapshot's
//! force list — the data the page's badge consumes — stamped
//! `Uncertain(Substituted)`; a release command through the client clears
//! the badge in the following snapshot; rejected forces and releases
//! surface their named reasons through the same journaled receipt
//! surface every rejection takes; and the pair client routes the force
//! commands to the peer reporting `active` like every other command —
//! all driven over TCP through the in-process `MonitorClient`.

use dcs_core::{
    Command, CommandError, CommandOutcome, CommandReceipt, Direction, ForcedPoint, IoDriver,
    IoError, JournalEvent, PointId, Quality, QualityReason, Role, Sample, Tick, Value, ValueKind,
};
use dcs_model::{PlantModel, SignalIndex};
use dcs_monitor::{Monitor, MonitorClient, PairClient};
use dcs_runtime::{
    Component, ComponentIo, ComponentIoExt, Executor, IoRequirement, Peer, PointMap, StepError,
};
use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::{Arc, Mutex};
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

// The rig's points: `FIELD_IN` is the model-declared writable field `In`
// point — the force contract's canonical target; `UNMARKED_IN` is a
// field `In` point the model left unmarked; `OUT`/`SPARE` are `Out`
// points, never legal force targets.
const FIELD_IN: PointId = PointId(10);
const OUT: PointId = PointId(20);
const SPARE: PointId = PointId(30);
const UNMARKED_IN: PointId = PointId(40);

/// Reads `In` point 10 and drives `Out` point 20 at gain 2 — the same
/// component shape the other monitor tests use, so the served
/// descriptor's `in` port binds the force target for the
/// faceplate-badge join.
struct Scale;

impl Component for Scale {
    fn name(&self) -> &str {
        "scale"
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        vec![
            IoRequirement::input::<f64>("in", FIELD_IN),
            IoRequirement::output::<f64>("out", OUT),
        ]
    }

    fn step(&mut self, io: &dyn ComponentIo, _tick: Tick) -> Result<(), StepError> {
        let sample = io.read_typed::<f64>(FIELD_IN)?;
        io.write_typed(OUT, sample.value * 2.0)?;
        Ok(())
    }
}

/// The model fixture behind the monitor: its `io_points` declare
/// `FIELD_IN` writable — the operator force surface — and leave
/// `UNMARKED_IN` unmarked.
const MODEL: &str = include_str!("../fixtures/forcing.json");

fn signal_index() -> SignalIndex {
    PlantModel::load(MODEL).unwrap().signal_index()
}

fn point_map() -> PointMap {
    PointMap::new()
        .with_writable_point(FIELD_IN, Direction::In, ValueKind::Float)
        .with_point(OUT, Direction::Out, ValueKind::Float)
        .with_point(SPARE, Direction::Out, ValueKind::Float)
        .with_point(UNMARKED_IN, Direction::In, ValueKind::Float)
}

/// Builds the rig and runs `body` against a serving monitor; the server
/// is shut down before the driver's borrow ends.
fn with_monitor<T>(body: impl FnOnce(&StubDriver, &MonitorClient) -> T) -> T {
    let driver = StubDriver::new(&[
        (FIELD_IN, Value::Float(0.0)),
        (OUT, Value::Float(0.0)),
        (SPARE, Value::Float(0.0)),
        (UNMARKED_IN, Value::Float(0.0)),
    ]);
    let executor = Executor::new(&driver, point_map(), vec![Box::new(Scale)]).unwrap();
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

fn telemetry(snapshot: &dcs_core::TelemetrySnapshot, point: PointId) -> &dcs_core::PointTelemetry {
    snapshot
        .points
        .iter()
        .find(|telemetry| telemetry.point == point)
        .unwrap_or_else(|| panic!("no telemetry for {point:?}"))
}

#[test]
fn a_forced_point_lands_in_the_snapshot_force_list_the_page_consumes() {
    with_monitor(|driver, client| {
        driver.write(FIELD_IN, Value::Float(1.0)).unwrap();
        client.advance(1).unwrap();

        // The exact body the page's force affordance serializes.
        let (status, body) = client
            .request(
                "POST",
                "/command",
                Some(r#"{"force_point":{"point":10,"kind":"float","value":{"float":9.0}}}"#),
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

        // The applying scan's snapshot carries the force list the page
        // badges from…
        let snapshot = client.advance(1).unwrap();
        assert_eq!(
            snapshot.forces,
            vec![ForcedPoint {
                point: FIELD_IN,
                value: Value::Float(9.0),
            }]
        );
        // …and the point's sample reports the Substituted quality the
        // page draws distinctly from ordinary Uncertain.
        let sample = telemetry(&snapshot, FIELD_IN).sample.unwrap();
        assert_eq!(
            sample,
            Sample::new(
                Value::Float(9.0),
                Quality::Uncertain(QualityReason::Substituted),
                Tick(2),
            )
        );

        // The faceplate-badge join the page performs: the component's
        // `in` port is annotated with the forced point, so the element
        // the port renders carries the badge too.
        let scale = snapshot
            .descriptors
            .iter()
            .find(|descriptor| descriptor.name == "scale")
            .unwrap();
        assert_eq!(
            scale
                .ports
                .iter()
                .find(|port| port.name == "in")
                .unwrap()
                .point,
            Some(FIELD_IN)
        );
    });
}

#[test]
fn page_serves_the_badge_and_writable_only_force_affordances() {
    with_monitor(|_driver, client| {
        let page = client.page().unwrap();
        // The badge: the page's data model marks each point in the
        // snapshot's force set, rendered beside the value in the point
        // listing and on the faceplate element bound to it.
        for needle in [
            "snapshot.forces",
            "function forcedBadge(",
            "class=\\\"forced\\\"",
            "forcedBadge(meta.point, forced)",
            "forcedBadge(port.point, forced)",
        ] {
            assert!(page.contains(needle), "page lacks {needle}");
        }
        // The force affordance appears only on model-declared writable
        // In points — the force contract's legal targets — and the
        // release only on a forced point; both submit through the
        // receipted, active-peer-routed command path.
        for needle in [
            "meta.writable && meta.direction === \"in\"",
            "class=\\\"force\\\"",
            "class=\\\"unforce\\\"",
            "function submitForce(",
            "function submitUnforce(",
            "{ force_point: {",
            "{ unforce_point: {",
            "await submitCommand(command)",
            "id=\"command-force\"",
        ] {
            assert!(page.contains(needle), "page lacks {needle}");
        }
        // Forced values draw their Substituted quality distinctly from
        // ordinary Uncertain.
        for needle in [
            "quality.uncertain === \"substituted\"",
            "\"substituted\"",
            ".substituted",
        ] {
            assert!(page.contains(needle), "page lacks {needle}");
        }
        // A rejected force or release surfaces its named reason through
        // the same journal and receipt surface as every rejection.
        for needle in ["force_point", "unforce_point", "not_writable"] {
            assert!(page.contains(needle), "page lacks {needle}");
        }
        assert!(!page.contains("src="), "page references external assets");

        // The metadata the affordance gates on: the model marks the
        // field input writable; the unmarked input and the outputs are
        // display-only.
        let index = client.signals().unwrap();
        assert!(index.get(FIELD_IN).unwrap().writable);
        assert_eq!(index.get(FIELD_IN).unwrap().direction, Direction::In);
        assert!(!index.get(UNMARKED_IN).unwrap().writable);
        assert!(!index.get(OUT).unwrap().writable);
    });
}

#[test]
fn a_release_command_clears_the_badge_in_the_following_snapshot() {
    with_monitor(|driver, client| {
        driver.write(FIELD_IN, Value::Float(1.0)).unwrap();
        client.advance(1).unwrap();
        client
            .command(&Command::ForcePoint {
                point: FIELD_IN,
                kind: ValueKind::Float,
                value: Value::Float(9.0),
            })
            .unwrap();
        assert_eq!(client.advance(1).unwrap().forces.len(), 1);

        // The release the badge's affordance posts: the exact wire
        // shape, answered by the same receipt contract.
        let (status, body) = client
            .request(
                "POST",
                "/command",
                Some(r#"{"unforce_point":{"point":10}}"#),
            )
            .unwrap();
        assert_eq!(status, 200, "{body}");
        let receipt: CommandReceipt = serde_json::from_str(&body).unwrap();
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Accepted {
                apply_tick: Tick(3)
            }
        );
        // Between scans the badge still stands…
        assert_eq!(client.snapshot().unwrap().forces.len(), 1);

        // …and clears in the following snapshot, the live field read
        // resumed at the release's scan boundary.
        driver.write(FIELD_IN, Value::Float(4.0)).unwrap();
        let snapshot = client.advance(1).unwrap();
        assert!(snapshot.forces.is_empty());
        let sample = telemetry(&snapshot, FIELD_IN).sample.unwrap();
        assert_eq!(sample, Sample::good(Value::Float(4.0), Tick(3)));
    });
}

#[test]
fn rejected_forces_and_releases_surface_named_reasons() {
    with_monitor(|_driver, client| {
        // An `Out` point is never a legal force target.
        let receipt = client
            .command(&Command::ForcePoint {
                point: OUT,
                kind: ValueKind::Float,
                value: Value::Float(9.0),
            })
            .unwrap();
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::NotWritable { point: OUT }
            }
        );

        // A field `In` point the model left unmarked refuses the same
        // way — the writable surface bounds forces exactly like writes.
        let receipt = client
            .command(&Command::ForcePoint {
                point: UNMARKED_IN,
                kind: ValueKind::Float,
                value: Value::Float(9.0),
            })
            .unwrap();
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::NotWritable { point: UNMARKED_IN }
            }
        );

        // A release on a point outside the writable surface refuses at
        // submission too.
        let receipt = client
            .command(&Command::UnforcePoint { point: SPARE })
            .unwrap();
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::NotWritable { point: SPARE }
            }
        );

        // An unknown point and a mistyped force value carry their own
        // named reasons.
        let receipt = client
            .command(&Command::ForcePoint {
                point: PointId(99),
                kind: ValueKind::Float,
                value: Value::Float(9.0),
            })
            .unwrap();
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::UnknownPoint { point: PointId(99) }
            }
        );
        let receipt = client
            .command(&Command::ForcePoint {
                point: FIELD_IN,
                kind: ValueKind::Int,
                value: Value::Int(9),
            })
            .unwrap();
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::TypeMismatch {
                    point: FIELD_IN,
                    expected: ValueKind::Float,
                    found: Value::Int(9),
                }
            }
        );

        // Every refusal lands on the journal surface the page polls —
        // each as a settled command carrying its named reason.
        let journal = client.journal(0).unwrap();
        let rejections: Vec<&CommandReceipt> = journal
            .iter()
            .filter_map(|entry| match &entry.event {
                JournalEvent::CommandSettled { receipt } => Some(receipt),
                _ => None,
            })
            .filter(|receipt| matches!(receipt.outcome, CommandOutcome::Rejected { .. }))
            .collect();
        assert_eq!(rejections.len(), 5, "{rejections:?}");
        assert!(
            journal.iter().any(|entry| matches!(
                &entry.event,
                JournalEvent::CommandSettled { receipt }
                    if matches!(receipt.command, Command::ForcePoint { point, .. } if point == OUT)
                        && matches!(receipt.outcome, CommandOutcome::Rejected { .. })
            )),
            "the Out-point force rejection is journaled"
        );
        assert!(
            journal.iter().any(|entry| matches!(
                &entry.event,
                JournalEvent::CommandSettled { receipt }
                    if matches!(receipt.command, Command::UnforcePoint { point } if point == SPARE)
                        && matches!(receipt.outcome, CommandOutcome::Rejected { .. })
            )),
            "the release rejection is journaled"
        );
    });
}

/// One peer of the pair rig: its monitor served on a dedicated thread —
/// the same shape the pair-view tests use.
struct PeerRig {
    monitor: Arc<Monitor<'static>>,
    client: MonitorClient,
    addr: SocketAddr,
    thread: Option<thread::JoinHandle<()>>,
}

impl PeerRig {
    /// Assembles a peer executor over a private stub driver — `gate`
    /// `None`: this test exercises command routing, not field
    /// quiescence — and serves its monitor on a spawned thread.
    fn start(role: Role) -> Self {
        let driver: &'static StubDriver = Box::leak(Box::new(StubDriver::new(&[
            (FIELD_IN, Value::Float(0.0)),
            (OUT, Value::Float(0.0)),
            (SPARE, Value::Float(0.0)),
            (UNMARKED_IN, Value::Float(0.0)),
        ])));
        let executor = Executor::new(driver, point_map(), vec![Box::new(Scale)]).unwrap();
        let peer = match role {
            Role::Active => Peer::active(executor, None),
            _ => Peer::standby(executor, None),
        };
        let monitor = Arc::new(Monitor::bind_peer("127.0.0.1:0", peer, signal_index()).unwrap());
        let addr = monitor.local_addr();
        let client = MonitorClient::new(addr);
        let serving = Arc::clone(&monitor);
        let thread = thread::spawn(move || serving.serve());
        Self {
            monitor,
            client,
            addr,
            thread: Some(thread),
        }
    }

    /// Stops serving and drops the monitor handle.
    fn stop(mut self) {
        self.monitor.shutdown();
        if let Some(thread) = self.thread.take() {
            thread.join().unwrap();
        }
    }
}

impl Drop for PeerRig {
    fn drop(&mut self) {
        self.monitor.shutdown();
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

#[test]
fn force_and_release_route_only_to_the_peer_reporting_active() {
    let active = PeerRig::start(Role::Active);
    let standby = PeerRig::start(Role::Standby);
    let mut pair = PairClient::new([standby.addr, active.addr]);
    pair.poll_roles();
    assert_eq!(pair.source(), Some(active.addr));

    active.client.advance(1).unwrap();
    // The force command the page's affordance submits rides the pair
    // view's active-only routing like every other command.
    let force = Command::ForcePoint {
        point: FIELD_IN,
        kind: ValueKind::Float,
        value: Value::Float(9.0),
    };
    let receipt = pair.command(&force).unwrap();
    assert_eq!(
        receipt.outcome,
        CommandOutcome::Accepted {
            apply_tick: Tick(2)
        }
    );
    assert_eq!(active.client.receipts().unwrap().len(), 1);
    assert!(standby.client.receipts().unwrap().is_empty());

    // The badge's release routes the same way: the active's log grows,
    // the standby was never sent either command.
    let release = Command::UnforcePoint { point: FIELD_IN };
    let receipt = pair.command(&release).unwrap();
    assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
    assert_eq!(active.client.receipts().unwrap().len(), 2);
    assert!(standby.client.receipts().unwrap().is_empty());
    assert!(standby.client.journal(0).unwrap().is_empty());

    active.stop();
    standby.stop();
}
