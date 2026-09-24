//! End-to-end tests for the monitoring page's managed alarm pane: the
//! descriptor-to-point wiring join routes rows into the named
//! managed-state lists — `shelved`, `suppressed`, `out_of_service` —
//! by the uniform status vocabulary, while `alarm`/`unacknowledged`
//! keep reporting process truth and managed rows still count toward
//! the totals; the declared `priority`/`class`/`response_ticks` join
//! from the snapshot's parameters section; the instance's
//! rationalization block joins from the served index's components
//! section by diagnostic name; the alarm journal keeps the durable
//! journal's `seq` order so a burst's initiating cause stands first;
//! the bound `shelve`/`oos` request inputs render held-level
//! shelve/unshelve and out-of-service/return-to-service affordances
//! where the model marks the bound point writable; and the
//! protection-layer signal group is served data, all driven over TCP
//! through the in-process `MonitorClient` against a rig of the two
//! managed sibling kinds — one unbound-`shelve` and one
//! unwritable-`shelve` instance covering the never-shelvable surface —
//! with the first alarm's `shelve` point carrying the model's
//! `requires_reason` mark, the per-alarm mandatory-reason declaration
//! the shelving-reason decision records.

use dcs_blocks::{
    AlarmLimits, ManagedAlarmConfig, ManagedAlarmIo, ManagedBoolLatchingAlarm, ManagedLatchingAlarm,
};
use dcs_core::{
    Command, CommandError, CommandOutcome, ComponentDescriptor, Direction, IoDriver, IoError,
    JournalEntry, JournalEvent, PointId, PortDescriptor, PortRole, Sample, TelemetrySnapshot, Tick,
    Value, ValueKind,
};
use dcs_model::{PlantModel, Rationalization, SignalIndex};
use dcs_monitor::{Monitor, MonitorClient};
use dcs_runtime::{Executor, PointMap, PointSpec};
use std::collections::{BTreeSet, HashMap};
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

// The rig's points, matching the managed_alarms fixture: one
// `managed-latching-alarm` (Float `in`, shelving and out-of-service
// wired to writable points), one `managed-bool-latching-alarm` (Bool
// `in`, suppression and out-of-service wired, `shelve` unbound), and a
// second `managed-bool-latching-alarm` whose `shelve` binds a field
// point the model never marked writable — the unbound and unwritable
// halves of the never-shelvable surface — plus two protection-layer
// field points under the declared `protection` signal group.
const PV1: PointId = PointId(10);
const ACK1: PointId = PointId(11);
const SHELVE1: PointId = PointId(12);
const OOS1_IN: PointId = PointId(13);
const PV2: PointId = PointId(14);
const ACK2: PointId = PointId(15);
const SUPPRESS2: PointId = PointId(16);
const OOS2_IN: PointId = PointId(17);
const PV3: PointId = PointId(18);
const ACK3: PointId = PointId(19);
const ALARM1: PointId = PointId(20);
const UNACK1: PointId = PointId(21);
const SHELVED1: PointId = PointId(22);
const SUPPRESSED1: PointId = PointId(23);
const OOS1: PointId = PointId(24);
const ALARM2: PointId = PointId(25);
const UNACK2: PointId = PointId(26);
const SHELVED2: PointId = PointId(27);
const SUPPRESSED2: PointId = PointId(28);
const OOS2: PointId = PointId(29);
const SHELVE3: PointId = PointId(30);
const ALARM3: PointId = PointId(31);
const UNACK3: PointId = PointId(32);
const SHELVED3: PointId = PointId(33);
const SUPPRESSED3: PointId = PointId(34);
const OOS3: PointId = PointId(35);
const POWER_FAIL: PointId = PointId(40);
const PROT_TRIP: PointId = PointId(41);

/// The component names the fixture's component records join on — the
/// `"<kind>:<id>"` diagnostic name `ComponentInstance::name` derives.
const ALARM1_NAME: &str = "managed-latching-alarm:1";
const ALARM2_NAME: &str = "managed-bool-latching-alarm:2";
const ALARM3_NAME: &str = "managed-bool-latching-alarm:3";

/// The managed-state status outputs, journaled like the model declares
/// them: a spec whose value transitions join the durable journal as
/// `point_changed` entries.
fn journaled_out() -> PointSpec {
    PointSpec {
        direction: Direction::Out,
        kind: ValueKind::Bool,
        internal: None,
        writable: false,
        requires_reason: false,
        stale_after_ticks: None,
        journaled: true,
    }
}

/// A journaled field `In` point — the protection-layer states the
/// model declares durable.
fn journaled_in() -> PointSpec {
    PointSpec {
        direction: Direction::In,
        kind: ValueKind::Bool,
        internal: None,
        writable: false,
        requires_reason: false,
        stale_after_ticks: None,
        journaled: true,
    }
}

/// The model fixture behind the monitor: its `components` section
/// supplies the served rationalization records and its `signals` the
/// `protection` group the pane's protection section reads.
const MODEL: &str = include_str!("../fixtures/managed_alarms.json");

fn signal_index() -> SignalIndex {
    PlantModel::load(MODEL).unwrap().signal_index()
}

/// Builds the rig — the two managed sibling kinds — and runs `body`
/// against a serving monitor; the server is shut down before the
/// driver's borrow ends.
fn with_monitor<T>(body: impl FnOnce(&StubDriver, &MonitorClient) -> T) -> T {
    let driver = StubDriver::new(&[
        // All conditions start clear so no alarm stands until a test
        // drives one; the suppression condition starts released.
        (PV1, Value::Float(50.0)),
        (PV2, Value::Bool(false)),
        (SUPPRESS2, Value::Bool(false)),
        (PV3, Value::Bool(false)),
        (SHELVE3, Value::Bool(false)),
        (POWER_FAIL, Value::Bool(false)),
        (PROT_TRIP, Value::Bool(false)),
        (ALARM1, Value::Bool(false)),
        (UNACK1, Value::Bool(false)),
        (SHELVED1, Value::Bool(false)),
        (SUPPRESSED1, Value::Bool(false)),
        (OOS1, Value::Bool(false)),
        (ALARM2, Value::Bool(false)),
        (UNACK2, Value::Bool(false)),
        (SHELVED2, Value::Bool(false)),
        (SUPPRESSED2, Value::Bool(false)),
        (OOS2, Value::Bool(false)),
        (ALARM3, Value::Bool(false)),
        (UNACK3, Value::Bool(false)),
        (SHELVED3, Value::Bool(false)),
        (SUPPRESSED3, Value::Bool(false)),
        (OOS3, Value::Bool(false)),
    ]);
    let journaled_status = [
        ALARM1,
        UNACK1,
        SHELVED1,
        SUPPRESSED1,
        OOS1,
        ALARM2,
        UNACK2,
        SHELVED2,
        SUPPRESSED2,
        OOS2,
        ALARM3,
        UNACK3,
        SHELVED3,
        SUPPRESSED3,
        OOS3,
    ];
    let mut map = PointMap::new()
        .with_point(PV1, Direction::In, ValueKind::Float)
        // The managed inputs bind the model-declared writable internal
        // points — the operator's shelve/oos requests travel the
        // ordinary receipted command path — except the designed
        // suppression condition, which is declared field wiring, and
        // the third alarm's shelve request, bound to an unwritable
        // field point: the bound-but-unwritable half of the
        // never-shelvable surface.
        .with_writable_internal(ACK1, Direction::In, ValueKind::Bool, Value::Bool(false))
        // The first alarm's shelve request point carries the model's
        // `requires_reason` mark — the per-alarm mandatory-reason
        // declaration the shelving-reason decision records.
        .with_spec(
            SHELVE1,
            PointSpec {
                direction: Direction::In,
                kind: ValueKind::Bool,
                internal: Some(Value::Bool(false)),
                writable: true,
                requires_reason: true,
                stale_after_ticks: None,
                journaled: false,
            },
        )
        .with_writable_internal(OOS1_IN, Direction::In, ValueKind::Bool, Value::Bool(false))
        .with_point(PV2, Direction::In, ValueKind::Bool)
        .with_writable_internal(ACK2, Direction::In, ValueKind::Bool, Value::Bool(false))
        .with_point(SUPPRESS2, Direction::In, ValueKind::Bool)
        .with_writable_internal(OOS2_IN, Direction::In, ValueKind::Bool, Value::Bool(false))
        .with_point(PV3, Direction::In, ValueKind::Bool)
        .with_writable_internal(ACK3, Direction::In, ValueKind::Bool, Value::Bool(false))
        .with_point(SHELVE3, Direction::In, ValueKind::Bool)
        .with_spec(POWER_FAIL, journaled_in())
        .with_spec(PROT_TRIP, journaled_in());
    for point in journaled_status {
        map = map.with_spec(point, journaled_out());
    }
    let alarm1 = ManagedLatchingAlarm::new(
        ALARM1_NAME,
        ManagedAlarmIo {
            input: PV1,
            ack: ACK1,
            shelve: Some(SHELVE1),
            oos: Some(OOS1_IN),
            suppress: None,
            alarm: ALARM1,
            unacknowledged: UNACK1,
            shelved: SHELVED1,
            suppressed: SUPPRESSED1,
            out_of_service: OOS1,
        },
        AlarmLimits {
            low: 10.0,
            high: 90.0,
            hysteresis: 5.0,
        },
        ManagedAlarmConfig {
            max_shelve_ticks: 600,
            priority: 1,
            class: 2,
            response_ticks: 30,
        },
    )
    .unwrap();
    let alarm2 = ManagedBoolLatchingAlarm::new(
        ALARM2_NAME,
        ManagedAlarmIo {
            input: PV2,
            ack: ACK2,
            shelve: None,
            oos: Some(OOS2_IN),
            suppress: Some(SUPPRESS2),
            alarm: ALARM2,
            unacknowledged: UNACK2,
            shelved: SHELVED2,
            suppressed: SUPPRESSED2,
            out_of_service: OOS2,
        },
        ManagedAlarmConfig {
            max_shelve_ticks: 0,
            priority: 2,
            class: 1,
            response_ticks: 60,
        },
    );
    let alarm3 = ManagedBoolLatchingAlarm::new(
        ALARM3_NAME,
        ManagedAlarmIo {
            input: PV3,
            ack: ACK3,
            // Bound but unwritable — the model marks no command
            // surface on the field point, so the alarm is
            // never-shelvable despite the nonzero bound.
            shelve: Some(SHELVE3),
            oos: None,
            suppress: None,
            alarm: ALARM3,
            unacknowledged: UNACK3,
            shelved: SHELVED3,
            suppressed: SUPPRESSED3,
            out_of_service: OOS3,
        },
        ManagedAlarmConfig {
            max_shelve_ticks: 5,
            priority: 3,
            class: 1,
            response_ticks: 45,
        },
    );
    let executor = Executor::new(
        &driver,
        map,
        vec![Box::new(alarm1), Box::new(alarm2), Box::new(alarm3)],
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

fn telemetry(snapshot: &TelemetrySnapshot, point: PointId) -> &dcs_core::PointTelemetry {
    snapshot
        .points
        .iter()
        .find(|telemetry| telemetry.point == point)
        .unwrap_or_else(|| panic!("no telemetry for {point:?}"))
}

/// The live sample of the component's status-role `Out` port named
/// `name` — the page's uniform-vocabulary join.
fn flag_sample(
    snapshot: &TelemetrySnapshot,
    descriptor: &ComponentDescriptor,
    name: &str,
) -> Option<Sample> {
    let port = descriptor
        .ports
        .iter()
        .find(|p| p.name == name && p.direction == Direction::Out)?;
    port.point
        .and_then(|point| telemetry(snapshot, point).sample)
}

/// The descriptor's declared managed request input — `shelve` or
/// `oos`, `In`, `Bool` — the port the row's affordance joins through,
/// or `None` where the kind leaves the input unbound.
fn request_port<'s>(descriptor: &'s ComponentDescriptor, name: &str) -> Option<&'s PortDescriptor> {
    descriptor
        .ports
        .iter()
        .find(|p| p.name == name && p.direction == Direction::In && p.kind == ValueKind::Bool)
}

fn is_asserted(sample: Option<Sample>) -> bool {
    sample.is_some_and(|sample| match sample.value {
        Value::Bool(value) => value,
        Value::Int(value) => value != 0,
        Value::Float(value) => value != 0.0,
    })
}

/// One alarm row's data, as the pane's data model builds it: the
/// owning component, the asserted status port, and the component's
/// reported states — each asserted managed flag plus `active` while
/// `alarm` stands and `unacknowledged` while the latch stands.
#[derive(Debug)]
struct Row<'s> {
    component: &'s str,
    port: &'s str,
    states: BTreeSet<&'static str>,
}

impl Row<'_> {
    /// The row's managed states — the states subset in the uniform
    /// decision-71 vocabulary, the routing the pane's managed lists
    /// apply.
    fn managed(&self) -> BTreeSet<&'static str> {
        self.states
            .intersection(&BTreeSet::from(["shelved", "suppressed", "out_of_service"]))
            .copied()
            .collect()
    }
}

/// The pane's row model over one snapshot: every asserted status-role
/// `Out` port, each carrying the states the component reports.
fn alarm_rows(snapshot: &TelemetrySnapshot) -> Vec<Row<'_>> {
    let mut rows = Vec::new();
    for descriptor in &snapshot.descriptors {
        let mut states = BTreeSet::new();
        for name in ["shelved", "suppressed", "out_of_service"] {
            if is_asserted(flag_sample(snapshot, descriptor, name)) {
                states.insert(name);
            }
        }
        if is_asserted(flag_sample(snapshot, descriptor, "alarm")) {
            states.insert("active");
        }
        if is_asserted(flag_sample(snapshot, descriptor, "unacknowledged")) {
            states.insert("unacknowledged");
        }
        for port in &descriptor.ports {
            if port.role != Some(PortRole::Status) || port.direction != Direction::Out {
                continue;
            }
            let sample = port
                .point
                .and_then(|point| telemetry(snapshot, point).sample);
            if is_asserted(sample) {
                rows.push(Row {
                    component: descriptor.name.as_str(),
                    port: port.name.as_str(),
                    states: states.clone(),
                });
            }
        }
    }
    rows
}

/// The snapshot's parameters-section entry for one component — the map
/// the pane's priority/class/response cells read by parameter name.
fn parameter_values<'s>(
    snapshot: &'s TelemetrySnapshot,
    component: &str,
) -> &'s std::collections::BTreeMap<String, Value> {
    &snapshot
        .parameters
        .iter()
        .find(|entry| entry.name == component)
        .unwrap_or_else(|| panic!("no parameters entry for {component}"))
        .values
}

/// The alarm-surface journal filter the pane applies: value
/// transitions on status-bound journaled points, quality transitions
/// and settled commands on the same points, and the owning components'
/// step failures — `related` holding every point a status port binds.
fn alarm_journal_entries<'s>(
    journal: &'s [JournalEntry],
    related: &BTreeSet<PointId>,
    components: &BTreeSet<String>,
) -> Vec<&'s JournalEntry> {
    journal
        .iter()
        .filter(|entry| match &entry.event {
            JournalEvent::PointChanged { point, .. }
            | JournalEvent::QualityChanged { point, .. } => related.contains(point),
            JournalEvent::CommandSettled { receipt } => match &receipt.command {
                Command::SetParameter { component, .. } => components.contains(component),
                command => command
                    .point()
                    .is_some_and(|point| related.contains(&point)),
            },
            JournalEvent::StepFailed { component, .. } => components.contains(component),
            _ => false,
        })
        .collect()
}

#[test]
fn the_join_yields_the_managed_lists_priority_and_rationalization() {
    with_monitor(|driver, client| {
        // Trip both alarms, shelve the first, take both out of service,
        // and assert the second's designed-suppression condition — the
        // shelve and oos requests travel the receipted command path.
        driver.write(PV1, Value::Float(95.0)).unwrap();
        driver.write(PV2, Value::Bool(true)).unwrap();
        driver.write(SUPPRESS2, Value::Bool(true)).unwrap();
        for point in [SHELVE1, OOS1_IN, OOS2_IN] {
            let receipt = managed_write(client, point, true);
            assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        }
        let snapshot = client.advance(1).unwrap();

        // The uniform-vocabulary join across kinds: no per-kind code
        // distinguishes the two descriptors' managed flags.
        let rows = alarm_rows(&snapshot);
        let shelved: Vec<_> = rows
            .iter()
            .filter(|row| row.states.contains("shelved"))
            .collect();
        let suppressed: Vec<_> = rows
            .iter()
            .filter(|row| row.states.contains("suppressed"))
            .collect();
        let out_of_service: Vec<_> = rows
            .iter()
            .filter(|row| row.states.contains("out_of_service"))
            .collect();
        // The first alarm shelves and is out of service — its four
        // asserted status ports (alarm, unacknowledged, shelved,
        // out_of_service) route to both named lists; the second
        // suppresses and is out of service — its three asserted ports
        // (alarm, suppressed, out_of_service) route likewise, the
        // unacknowledged latch withheld under suppression.
        assert_eq!(shelved.len(), 4, "{shelved:?}");
        assert!(shelved.iter().all(|row| row.component == ALARM1_NAME));
        assert_eq!(suppressed.len(), 3, "{suppressed:?}");
        assert!(suppressed.iter().all(|row| row.component == ALARM2_NAME));
        assert_eq!(out_of_service.len(), 7, "{out_of_service:?}");
        // Each row is one asserted status port's record — the flag
        // names the lists carry through the routing.
        assert_eq!(
            shelved.iter().map(|row| row.port).collect::<BTreeSet<_>>(),
            BTreeSet::from(["alarm", "unacknowledged", "shelved", "out_of_service"])
        );
        assert_eq!(
            suppressed
                .iter()
                .map(|row| row.port)
                .collect::<BTreeSet<_>>(),
            BTreeSet::from(["alarm", "suppressed", "out_of_service"])
        );
        // Managed rows count toward the totals: every asserted status
        // port stands in the data model even while routed — a
        // suppressed or shelved alarm is never erased.
        assert_eq!(rows.len(), 7, "{rows:?}");
        // Process truth on every row: `active` from the alarm port,
        // `unacknowledged` from the latch — withheld under suppression.
        assert!(
            rows.iter()
                .filter(|row| row.component == ALARM1_NAME)
                .all(|row| row.states.contains("active") && row.states.contains("unacknowledged"))
        );
        assert!(
            rows.iter()
                .filter(|row| row.component == ALARM2_NAME)
                .all(|row| row.states.contains("active") && !row.states.contains("unacknowledged"))
        );
        // No unmanaged rows stand: the standing list renders empty
        // while the managed lists carry the routed rows.
        assert!(
            rows.iter()
                .all(|row| !row.states.is_disjoint(&BTreeSet::from([
                    "shelved",
                    "suppressed",
                    "out_of_service"
                ])))
        );

        // The declared codes join from the parameters section by
        // component name — the data the pane's priority, class, and
        // response_ticks cells render.
        let values = parameter_values(&snapshot, ALARM1_NAME);
        assert_eq!(values["priority"], Value::Int(1));
        assert_eq!(values["class"], Value::Int(2));
        assert_eq!(values["response_ticks"], Value::Int(30));
        let values = parameter_values(&snapshot, ALARM2_NAME);
        assert_eq!(values["priority"], Value::Int(2));
        assert_eq!(values["class"], Value::Int(1));
        assert_eq!(values["response_ticks"], Value::Int(60));

        // The served index's components section joins by the
        // descriptor's diagnostic name — the rationalization block the
        // row's disclosure renders.
        let index = client.signals().unwrap();
        let record = index
            .components
            .iter()
            .find(|record| record.name == ALARM1_NAME)
            .unwrap();
        assert_eq!(record.kind, "managed-latching-alarm");
        assert_eq!(
            record.rationalization,
            Some(Rationalization {
                consequence: "The wet well overtops into the collection system".to_string(),
                required_action: "Start a standby pump and confirm discharge flow".to_string(),
                reference: "WW-OPS-301 high-level response".to_string(),
            })
        );
        assert!(
            index
                .components
                .iter()
                .any(|record| record.name == ALARM2_NAME && record.rationalization.is_some())
        );
    });
}

#[test]
fn the_alarm_journal_keeps_durable_transition_order() {
    with_monitor(|driver, client| {
        // The shelve request's settled receipt is the burst's
        // initiating cause; the resulting `shelved` transition is its
        // consequence. In the durable journal's seq order the cause
        // stands first — tick order alone cannot show it: both land at
        // the applying scan's tick. SHELVE1 is the fixture's
        // `requires_reason` point, so the request declares one.
        let receipt = managed_write(client, SHELVE1, true);
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        assert_eq!(receipt.reason.as_deref(), Some("pump maintenance window"));
        driver.write(PV1, Value::Float(95.0)).unwrap();
        client.advance(1).unwrap();

        // The sets the pane's filter tests against: every point a
        // status port binds, every component declaring status ports.
        let snapshot = client.snapshot().unwrap();
        let mut related = BTreeSet::new();
        let mut components = BTreeSet::new();
        for descriptor in &snapshot.descriptors {
            for port in &descriptor.ports {
                if port.role != Some(PortRole::Status) {
                    continue;
                }
                components.insert(descriptor.name.clone());
                if let Some(point) = port.point {
                    related.insert(point);
                }
            }
        }
        let journal = client.journal(0).unwrap();
        let entries = alarm_journal_entries(&journal, &related, &components);
        // point_changed transitions joined the slice — the journaled
        // lifecycle points' durable record — alongside the settled
        // receipt.
        assert!(
            entries.iter().any(|entry| matches!(
                &entry.event,
                JournalEvent::PointChanged { point, to, .. }
                    if *point == SHELVED1 && *to == Value::Bool(true)
            )),
            "the shelved assertion's point_changed is on the alarm surface"
        );
        // Seq order is the served order — and the initiating receipt
        // stands before the transition it caused.
        let seqs: Vec<u64> = entries.iter().map(|entry| entry.seq).collect();
        let mut sorted = seqs.clone();
        sorted.sort_unstable();
        assert_eq!(seqs, sorted, "the alarm journal is in durable seq order");
        let cause = entries
            .iter()
            .find(|entry| {
                matches!(
                    &entry.event,
                    JournalEvent::CommandSettled { receipt }
                        if matches!(&receipt.command,
                            Command::WriteValue { point, .. } if *point == SHELVE1)
                            && matches!(receipt.outcome, CommandOutcome::Applied { .. })
                )
            })
            .unwrap();
        let consequence = entries
            .iter()
            .find(|entry| {
                matches!(
                    &entry.event,
                    JournalEvent::PointChanged { point, to, .. }
                        if *point == SHELVED1 && *to == Value::Bool(true)
                )
            })
            .unwrap();
        assert!(
            cause.seq < consequence.seq,
            "the receipt (seq {}) precedes the shelved transition (seq {})",
            cause.seq,
            consequence.seq
        );
        assert_eq!(cause.tick, consequence.tick);
        // And the receipt the alarm journal pairs the transition with
        // carries the declared actor and reason — the durable record
        // the shelving-reason decision prescribes.
        let JournalEvent::CommandSettled { receipt } = &cause.event else {
            unreachable!();
        };
        assert_eq!(receipt.actor.as_deref(), Some("op-1"));
        assert_eq!(receipt.reason.as_deref(), Some("pump maintenance window"));
    });
}

#[test]
fn the_marked_shelve_point_refuses_reasonless_and_journals_the_reason() {
    with_monitor(|driver, client| {
        // The served index carries the mark: the page's early-explain
        // gate reads `requires_reason` off the same PointSignal the
        // admission check enforces.
        let index = client.signals().unwrap();
        assert!(index.get(SHELVE1).unwrap().requires_reason);
        assert!(!index.get(OOS1_IN).unwrap().requires_reason);

        driver.write(PV1, Value::Float(95.0)).unwrap();
        client.advance(1).unwrap();

        // A reasonless shelve request on the marked point refuses at
        // admission with the named rejection — and the refused attempt
        // journals like every refused command. A blank declaration is
        // no reason.
        for reason in [None, Some("   ")] {
            let receipt = client
                .command_attributed(
                    &Command::WriteValue {
                        point: SHELVE1,
                        kind: ValueKind::Bool,
                        value: Value::Bool(true),
                    },
                    Some("op-1"),
                    reason,
                )
                .unwrap();
            assert_eq!(
                receipt.outcome,
                CommandOutcome::Rejected {
                    reason: CommandError::ReasonRequired { point: SHELVE1 }
                }
            );
        }
        let snapshot = client.advance(1).unwrap();
        assert_eq!(
            telemetry(&snapshot, SHELVED1).sample.unwrap().value,
            Value::Bool(false),
            "the refused requests never shelved the alarm"
        );

        // The declared reason admits the shelve and journals beside the
        // lifecycle record — the receipted `CommandSettled` carries it
        // and the `shelved` `point_changed` it drove follows at the same
        // tick.
        let receipt = managed_write(client, SHELVE1, true);
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        let snapshot = client.advance(1).unwrap();
        assert_eq!(
            telemetry(&snapshot, SHELVED1).sample.unwrap().value,
            Value::Bool(true)
        );

        let journal = client.journal(0).unwrap();
        assert!(
            journal.iter().any(|entry| matches!(
                &entry.event,
                JournalEvent::CommandSettled { receipt }
                    if receipt.outcome
                        == CommandOutcome::Rejected {
                            reason: CommandError::ReasonRequired { point: SHELVE1 }
                        }
            )),
            "the refused reasonless requests are journaled"
        );
        assert!(
            journal.iter().any(|entry| matches!(
                &entry.event,
                JournalEvent::CommandSettled { receipt }
                    if matches!(
                        &receipt.command,
                        Command::WriteValue { point, .. } if *point == SHELVE1
                    )
                        && matches!(receipt.outcome, CommandOutcome::Applied { .. })
                        && receipt.actor.as_deref() == Some("op-1")
                        && receipt.reason.as_deref() == Some("pump maintenance window")
            )),
            "the reasoned shelve's applied receipt is journaled with the reason"
        );
        // The served receipt log is the page's other managed-state
        // surface — the same attribution reaches it.
        let receipts = client.receipts().unwrap();
        let applied = receipts
            .iter()
            .find(|receipt| matches!(receipt.outcome, CommandOutcome::Applied { .. }))
            .unwrap();
        assert_eq!(applied.reason.as_deref(), Some("pump maintenance window"));
    });
}

#[test]
fn the_protection_group_is_served_data_distinct_from_the_alarm_surface() {
    with_monitor(|driver, client| {
        // The protection layer's reported field points assert under the
        // declared `protection` group — served SignalIndex data — and
        // never join the alarm surface: no component binds them, so the
        // pane's descriptor join cannot route them into an alarm row.
        driver.write(POWER_FAIL, Value::Bool(true)).unwrap();
        let snapshot = client.advance(1).unwrap();

        let index = client.signals().unwrap();
        for point in [POWER_FAIL, PROT_TRIP] {
            assert_eq!(
                index.get(point).unwrap().group.as_deref(),
                Some("protection")
            );
        }
        // The alarm join's related set — status-bound points — excludes
        // them: their transitions can never masquerade as alarm rows.
        let related: BTreeSet<PointId> = snapshot
            .descriptors
            .iter()
            .flat_map(|descriptor| &descriptor.ports)
            .filter(|port| port.role == Some(PortRole::Status))
            .filter_map(|port| port.point)
            .collect();
        assert!(!related.contains(&POWER_FAIL));
        assert!(!related.contains(&PROT_TRIP));
        assert!(alarm_rows(&snapshot).is_empty());
    });
}

#[test]
fn absent_sections_decode_empty() {
    with_monitor(|_driver, client| {
        // The serde-default shape from before the sections existed:
        // strip `components` from the served index and it still decodes
        // — the page's `index.components || []` guard joins nothing.
        let (status, body) = client.request("GET", "/signals", None).unwrap();
        assert_eq!(status, 200, "{body}");
        let mut document: serde_json::Value = serde_json::from_str(&body).unwrap();
        document.as_object_mut().unwrap().remove("components");
        let legacy: SignalIndex = serde_json::from_value(document).unwrap();
        assert!(legacy.components.is_empty());

        // The parameters section the priority join reads degrades the
        // same way — a snapshot lacking it decodes to the empty join.
        let (status, body) = client.request("GET", "/snapshot", None).unwrap();
        assert_eq!(status, 200, "{body}");
        let mut document: serde_json::Value = serde_json::from_str(&body).unwrap();
        document.as_object_mut().unwrap().remove("parameters");
        let legacy: TelemetrySnapshot = serde_json::from_value(document).unwrap();
        assert!(legacy.parameters.is_empty());
    });
}

#[test]
fn the_served_rationalization_record_serde_roundtrips() {
    with_monitor(|_driver, client| {
        // The served index decodes whole, and each record's
        // rationalization block roundtrips — the monitoring consumer
        // reads exactly the block the model declared.
        let (status, body) = client.request("GET", "/signals", None).unwrap();
        assert_eq!(status, 200, "{body}");
        let index: SignalIndex = serde_json::from_str(&body).unwrap();
        assert_eq!(index.components.len(), 3);
        for record in &index.components {
            let block = record.rationalization.as_ref().unwrap();
            let json = serde_json::to_string(block).unwrap();
            assert_eq!(
                serde_json::from_str::<Rationalization>(&json).unwrap(),
                *block
            );
        }
        // And the served record names match the descriptors' diagnostic
        // names — the join the page performs.
        let snapshot = client.snapshot().unwrap();
        for name in [ALARM1_NAME, ALARM2_NAME, ALARM3_NAME] {
            descriptor(&snapshot, name);
            assert!(index.components.iter().any(|record| record.name == name));
        }
    });
}

#[test]
fn page_serves_the_managed_pane_markup() {
    with_monitor(|_driver, client| {
        let page = client.page().unwrap();
        // The managed-state lists: the uniform decision-71 vocabulary,
        // the per-row state join, and the named-list routing.
        for needle in [
            "MANAGED_STATES",
            "[\"shelved\", \"suppressed\", \"out_of_service\"]",
            "function flagSample(",
            "function alarmRowStates(",
            "function managedListMarkup(",
            "id=\"managed-lists\"",
            "managed-count",
            "row.managed.length === 0",
        ] {
            assert!(page.contains(needle), "page lacks {needle}");
        }
        // The two-flag truth every routed row still reports.
        for needle in ["function truthFlags(", "\"unacknowledged\""] {
            assert!(page.contains(needle), "page lacks {needle}");
        }
        // Redundant priority coding: the declared vocabulary maps the
        // code to colour class plus textual level; class and
        // response_ticks render beside it.
        for needle in [
            "PRIORITY_VOCABULARY",
            "function priorityMarkup(",
            "<th>Priority</th>",
            "priority.p1",
            "\"response_ticks\"",
            "snapshot.parameters || []",
        ] {
            assert!(page.contains(needle), "page lacks {needle}");
        }
        // The rationalization disclosure joined from the served
        // components section.
        for needle in [
            "function rationalizationMarkup(",
            "componentsByName",
            "index.components || []",
            "block.consequence",
            "block.required_action",
            "block.reference",
        ] {
            assert!(page.contains(needle), "page lacks {needle}");
        }
        // First-out ordering: the alarm journal slices in the durable
        // journal's seq order, point_changed entries included.
        for needle in ["a.seq - b.seq", "\"point_changed\" in event"] {
            assert!(page.contains(needle), "page lacks {needle}");
        }
        // The protection boundary's distinct section, declared through
        // the ?protection= group parameter.
        for needle in [
            "id=\"protection-pane\"",
            "id=\"protection-points\"",
            "protectionGroups",
            "getAll(\"protection\")",
            "function renderProtection(",
        ] {
            assert!(page.contains(needle), "page lacks {needle}");
        }
        // The state filter — active, unacknowledged, and each managed
        // state — the pane's navigation.
        for needle in [
            "id=\"alarm-filter\"",
            "value=\"active\"",
            "value=\"unacknowledged\"",
            "value=\"shelved\"",
            "value=\"suppressed\"",
            "value=\"out_of_service\"",
        ] {
            assert!(page.contains(needle), "page lacks {needle}");
        }
        assert!(!page.contains("src="), "page references external assets");
    });
}

/// The affordance's post: an attributed `write_value` of `level` on the
/// managed request point — the receipted path the pane's managed
/// buttons issue, under the actor the page's `?operator=` declares and
/// carrying the declared reason the managed cell's reason field holds —
/// mandatory on the `requires_reason`-marked SHELVE1, voluntary on the
/// rest.
fn managed_write(client: &MonitorClient, point: PointId, level: bool) -> dcs_core::CommandReceipt {
    client
        .command_attributed(
            &Command::WriteValue {
                point,
                kind: ValueKind::Bool,
                value: Value::Bool(level),
            },
            Some("op-1"),
            Some("pump maintenance window"),
        )
        .unwrap()
}

#[test]
fn the_join_yields_the_bound_shelve_and_oos_targets_and_their_marks() {
    with_monitor(|_driver, client| {
        let snapshot = client.snapshot().unwrap();
        let index = client.signals().unwrap();

        // The first alarm's `shelve` and `oos` ports join through the
        // descriptor's bound-point annotation to model-writable
        // internal points — the shelve/unshelve and
        // out-of-service/return-to-service affordances' targets.
        let alarm1 = descriptor(&snapshot, ALARM1_NAME);
        let shelve = request_port(alarm1, "shelve").unwrap();
        assert_eq!(shelve.point, Some(SHELVE1));
        assert_eq!(shelve.role, Some(PortRole::Status));
        assert_eq!(request_port(alarm1, "oos").unwrap().point, Some(OOS1_IN));
        assert!(index.get(SHELVE1).unwrap().writable);
        assert!(index.get(OOS1_IN).unwrap().writable);

        // The second leaves `shelve` unbound — the descriptor declares
        // no port, so no affordance can join — while `oos` binds a
        // writable point.
        let alarm2 = descriptor(&snapshot, ALARM2_NAME);
        assert!(request_port(alarm2, "shelve").is_none());
        assert_eq!(request_port(alarm2, "oos").unwrap().point, Some(OOS2_IN));
        assert!(index.get(OOS2_IN).unwrap().writable);

        // The third binds `shelve` to a field point the model never
        // marked writable — the bound-but-unwritable half of the
        // never-shelvable surface — and leaves `oos` unbound.
        let alarm3 = descriptor(&snapshot, ALARM3_NAME);
        assert_eq!(request_port(alarm3, "shelve").unwrap().point, Some(SHELVE3));
        assert!(!index.get(SHELVE3).unwrap().writable);
        assert!(request_port(alarm3, "oos").is_none());
    });
}

#[test]
fn page_serves_the_managed_affordance_markup_and_the_held_level_rule() {
    with_monitor(|_driver, client| {
        let page = client.page().unwrap();
        // The affordances join the descriptor's bound-point annotation
        // like ack — port name, `In`, `Bool` — gated on the bound
        // point's writable mark; the label tracks the request point's
        // live level: shelve/unshelve and out-of-service/return-to-
        // service by whether the request stands.
        for needle in [
            "MANAGED_ACTIONS",
            "function managedAffordance(",
            "p.name === action.port && p.direction === \"in\"",
            "!meta || !meta.writable",
            "isAsserted(portSample(port, telemetry))",
            "class=\\\"managed\\\"",
            "data-level",
            "\"shelve\"",
            "\"unshelve\"",
            "\"out of service\"",
            "\"return to service\"",
            "function submitManaged(",
            "button.managed",
        ] {
            assert!(page.contains(needle), "page lacks {needle}");
        }
        // The declared-reason affordance: one input beside the managed
        // buttons, its text riding the attributed envelope, and the
        // `requires_reason` mark gating a blank submission up front.
        for needle in [
            "input.managed-reason",
            "meta.requires_reason",
            "declare a reason",
            "managedTransitionAttribution",
        ] {
            assert!(page.contains(needle), "page lacks {needle}");
        }
        let start = page.find("async function submitManaged(").unwrap();
        let end = start + page[start..].find("\n}\n").unwrap();
        let managed_body = &page[start..end];
        assert!(managed_body.contains("reason"), "{managed_body}");
        // The held-level rule: the submission issues the single
        // receipted write_value of the affordance's target level — no
        // `ackReleases` arming, no pulse-back write; the request stands
        // until the opposite command.
        let start = page.find("async function submitManaged(").unwrap();
        let end = start + page[start..].find("\n}\n").unwrap();
        let body = &page[start..end];
        assert!(body.contains("write_value"), "{body}");
        assert!(body.contains("submitCommand"), "{body}");
        assert!(body.contains("button.dataset.level"), "{body}");
        assert!(
            !body.contains("ackReleases"),
            "managed writes are held level-observed, never pulsed: {body}"
        );
        assert!(!page.contains("src="), "page references external assets");
    });
}

#[test]
fn the_managed_actions_settle_through_the_receipted_attributed_path() {
    with_monitor(|driver, client| {
        // Trip the first alarm so its row stands, then issue the
        // shelve affordance's post — an attributed write_value of true
        // on the bound shelve point.
        driver.write(PV1, Value::Float(95.0)).unwrap();
        let snapshot = client.advance(1).unwrap();
        let rows = alarm_rows(&snapshot);
        assert_eq!(rows.len(), 2, "{rows:?}");
        assert!(
            rows.iter()
                .all(|row| row.component == ALARM1_NAME && row.managed().is_empty())
        );

        let receipt = managed_write(client, SHELVE1, true);
        assert_eq!(receipt.actor.as_deref(), Some("op-1"));
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        let snapshot = client.advance(1).unwrap();
        // `shelved` asserts on the request's first scan and the row
        // routes to the managed list — while the request point stays
        // asserted: the write is held level-observed, not pulsed back.
        assert_eq!(
            telemetry(&snapshot, SHELVED1).sample.unwrap().value,
            Value::Bool(true)
        );
        assert_eq!(
            telemetry(&snapshot, SHELVE1).sample.unwrap().value,
            Value::Bool(true)
        );
        let rows = alarm_rows(&snapshot);
        assert_eq!(rows.len(), 3, "{rows:?}");
        assert!(
            rows.iter()
                .all(|row| row.component == ALARM1_NAME && row.states.contains("shelved"))
        );

        // Expiry at max_shelve_ticks: retuning the bound to 1 through
        // the receipted parameter path drops the flag on the request's
        // second scan even though the request still stands.
        let receipt = client
            .command_as(
                &Command::SetParameter {
                    component: ALARM1_NAME.to_string(),
                    name: "max_shelve_ticks".to_string(),
                    value: Value::Int(1),
                },
                Some("op-1"),
            )
            .unwrap();
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        let snapshot = client.advance(1).unwrap();
        assert_eq!(
            telemetry(&snapshot, SHELVED1).sample.unwrap().value,
            Value::Bool(false)
        );
        assert_eq!(
            telemetry(&snapshot, SHELVE1).sample.unwrap().value,
            Value::Bool(true),
            "the held request stands past the flag's expiry"
        );
        let rows = alarm_rows(&snapshot);
        assert!(
            rows.iter()
                .all(|row| row.component == ALARM1_NAME && row.managed().is_empty())
        );
        // The standing request never re-arms — the documented
        // re-shelve-requires-cycle rule.
        let snapshot = client.advance(1).unwrap();
        assert_eq!(
            telemetry(&snapshot, SHELVED1).sample.unwrap().value,
            Value::Bool(false)
        );

        // Cycling the request through false re-arms the bound: the
        // unshelve write, then a fresh shelve request asserts the flag
        // again within the retuned bound.
        let receipt = managed_write(client, SHELVE1, false);
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        let snapshot = client.advance(1).unwrap();
        assert_eq!(
            telemetry(&snapshot, SHELVE1).sample.unwrap().value,
            Value::Bool(false)
        );
        let receipt = managed_write(client, SHELVE1, true);
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        let snapshot = client.advance(1).unwrap();
        assert_eq!(
            telemetry(&snapshot, SHELVED1).sample.unwrap().value,
            Value::Bool(true)
        );

        // The manual unshelve returns the row to the standing list per
        // the kind's documented rule — the flag follows the released
        // request.
        let receipt = managed_write(client, SHELVE1, false);
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        let snapshot = client.advance(1).unwrap();
        assert_eq!(
            telemetry(&snapshot, SHELVED1).sample.unwrap().value,
            Value::Bool(false)
        );
        let rows = alarm_rows(&snapshot);
        assert!(
            rows.iter()
                .all(|row| row.component == ALARM1_NAME && row.managed().is_empty())
        );

        // The out-of-service affordance asserts `out_of_service` and
        // holds it across scans — manual in both directions, no
        // automatic return — until the return command.
        let receipt = managed_write(client, OOS1_IN, true);
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        let snapshot = client.advance(1).unwrap();
        assert_eq!(
            telemetry(&snapshot, OOS1).sample.unwrap().value,
            Value::Bool(true)
        );
        assert!(
            alarm_rows(&snapshot)
                .iter()
                .all(|row| row.states.contains("out_of_service"))
        );
        let snapshot = client.advance(1).unwrap();
        assert_eq!(
            telemetry(&snapshot, OOS1).sample.unwrap().value,
            Value::Bool(true),
            "out of service stands without a return command"
        );
        let receipt = managed_write(client, OOS1_IN, false);
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        let snapshot = client.advance(1).unwrap();
        assert_eq!(
            telemetry(&snapshot, OOS1).sample.unwrap().value,
            Value::Bool(false)
        );

        // Every action lands in the journal as an attributed settled
        // receipt carrying the declared reason — the lifecycle audit
        // the shelving-reason decision prescribes.
        let journal = client.journal(0).unwrap();
        for point in [SHELVE1, OOS1_IN] {
            assert!(
                journal.iter().any(|entry| matches!(
                    &entry.event,
                    JournalEvent::CommandSettled { receipt }
                        if matches!(
                            &receipt.command,
                            Command::WriteValue { point: p, .. } if *p == point
                        )
                            && matches!(receipt.outcome, CommandOutcome::Applied { .. })
                            && receipt.actor.as_deref() == Some("op-1")
                            && receipt.reason.as_deref() == Some("pump maintenance window")
                )),
                "no applied attributed receipt journaled for {point:?}"
            );
        }
        assert!(
            journal.iter().any(|entry| matches!(
                &entry.event,
                JournalEvent::CommandSettled { receipt }
                    if matches!(
                        &receipt.command,
                        Command::SetParameter { component, name, .. }
                            if component == ALARM1_NAME && name == "max_shelve_ticks"
                    )
                        && receipt.actor.as_deref() == Some("op-1")
            )),
            "the expiry tune's attributed receipt is journaled"
        );
    });
}

#[test]
fn the_never_shelvable_surfaces_offer_nothing_and_reject() {
    with_monitor(|driver, client| {
        // Trip the third alarm so its row stands; its `shelve` binds a
        // field point the model never marked writable — the pane
        // offers no shelve affordance, and a write attempted anyway
        // answers the named rejection.
        driver.write(PV3, Value::Bool(true)).unwrap();
        let snapshot = client.advance(1).unwrap();
        assert!(
            alarm_rows(&snapshot)
                .iter()
                .all(|row| row.component == ALARM3_NAME)
        );

        let receipt = managed_write(client, SHELVE3, true);
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::NotWritable { point: SHELVE3 }
            }
        );
        // The named rejection lands in the journal like every refused
        // command — the receipt pane's named reason.
        let journal = client.journal(0).unwrap();
        assert!(
            journal.iter().any(|entry| matches!(
                &entry.event,
                JournalEvent::CommandSettled { receipt }
                    if receipt.outcome
                        == CommandOutcome::Rejected {
                            reason: CommandError::NotWritable { point: SHELVE3 }
                        }
            )),
            "the refused shelve write is journaled"
        );

        // The unbound `shelve` is the stronger never-shelvable form —
        // no port to join an affordance through at all — while the
        // bound-but-unwritable point still functions as declared field
        // wiring: the field's own request shelves within the bound.
        let alarm2 = descriptor(&snapshot, ALARM2_NAME);
        assert!(request_port(alarm2, "shelve").is_none());
        driver.write(SHELVE3, Value::Bool(true)).unwrap();
        let snapshot = client.advance(1).unwrap();
        assert_eq!(
            telemetry(&snapshot, SHELVED3).sample.unwrap().value,
            Value::Bool(true)
        );
        assert!(
            alarm_rows(&snapshot)
                .iter()
                .filter(|row| row.component == ALARM3_NAME)
                .all(|row| row.states.contains("shelved"))
        );
    });
}

/// The scripted sequence the determinism check replays: trip the first
/// alarm, shelve it through the receipted path, retune the bound so
/// expiry lands, then release the request — answering the pane's row
/// model after each step as `(component, port, states)`.
fn scripted_run() -> Vec<Vec<(String, String, Vec<String>)>> {
    with_monitor(|driver, client| {
        let collect = |snapshot: &TelemetrySnapshot| {
            alarm_rows(snapshot)
                .iter()
                .map(|row| {
                    (
                        row.component.to_string(),
                        row.port.to_string(),
                        row.states.iter().map(|state| state.to_string()).collect(),
                    )
                })
                .collect::<Vec<_>>()
        };
        let mut renders = Vec::new();
        driver.write(PV1, Value::Float(95.0)).unwrap();
        renders.push(collect(&client.advance(1).unwrap()));
        managed_write(client, SHELVE1, true);
        renders.push(collect(&client.advance(1).unwrap()));
        client
            .command(&Command::SetParameter {
                component: ALARM1_NAME.to_string(),
                name: "max_shelve_ticks".to_string(),
                value: Value::Int(1),
            })
            .unwrap();
        renders.push(collect(&client.advance(1).unwrap()));
        managed_write(client, SHELVE1, false);
        renders.push(collect(&client.advance(1).unwrap()));
        renders
    })
}

#[test]
fn identical_scripted_runs_render_identically() {
    // No wall-clock or scheduling input reaches the row model: two
    // rigs running the same command sequence produce the same renders.
    assert_eq!(scripted_run(), scripted_run());
}
