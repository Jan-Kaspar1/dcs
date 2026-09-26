//! End-to-end tests for the monitoring page's alarm-to-equipment
//! navigation: the alarm surfaces' rows and attributable journal
//! entries link into the equipment pane by the owning component's
//! diagnostic name, and the pane composes the surfaces the operations
//! workflow reads together — the descriptor-driven faceplate, the
//! bound points' live values with quality, a trend window per bound
//! point, the component-attributed journal slice, the instance's
//! rationalization record, and the pair-health and I/O-health lines —
//! all joined from the payloads the page already polls. The tests
//! below verify the descriptor-plus-snapshot-plus-parameters join
//! yields every navigation target, that the served page asset carries
//! the routing and the managed-list/priority/rationalization/health
//! rendering, that a snapshot lacking the newer sections degrades
//! cleanly, and that identical scripted runs render identically — all
//! driven over TCP through the in-process `MonitorClient` against the
//! managed-alarm rig.

use dcs_blocks::{
    AlarmLimits, ManagedAlarmConfig, ManagedAlarmIo, ManagedBoolLatchingAlarm, ManagedLatchingAlarm,
};
use dcs_core::{
    Command, CommandOutcome, ComponentDescriptor, Direction, IoDriver, IoError, JournalEntry,
    JournalEvent, PointId, PortRole, Quality, Sample, TelemetrySnapshot, Tick, Value, ValueKind,
};
use dcs_model::{PlantModel, SignalIndex};
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

// The rig's points — the managed_alarms fixture's two managed sibling
// kinds plus a protection-layer field pair: three owning components
// the alarm surfaces navigate to.
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
/// supplies the served rationalization records.
const MODEL: &str = include_str!("../fixtures/managed_alarms.json");

fn signal_index() -> SignalIndex {
    PlantModel::load(MODEL).unwrap().signal_index()
}

/// Builds the rig — the two managed sibling kinds — and runs `body`
/// against a serving monitor; the server is shut down before the
/// driver's borrow ends.
fn with_monitor<T>(body: impl FnOnce(&StubDriver, &MonitorClient) -> T) -> T {
    let driver = StubDriver::new(&[
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
        .with_writable_internal(ACK1, Direction::In, ValueKind::Bool, Value::Bool(false))
        .with_writable_internal(SHELVE1, Direction::In, ValueKind::Bool, Value::Bool(false))
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

fn is_asserted(sample: Option<Sample>) -> bool {
    sample.is_some_and(|sample| match sample.value {
        Value::Bool(value) => value,
        Value::Int(value) => value != 0,
        Value::Float(value) => value != 0.0,
    })
}

/// One alarm row's data, as the pane's data model builds it: the
/// owning component — the navigation target's name — the asserted
/// status port, and the component's reported states.
#[derive(Debug)]
struct Row<'s> {
    component: &'s str,
    port: &'s str,
    states: BTreeSet<&'static str>,
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

/// The component's bound points, deduplicated — the pane's
/// live-points table, trend windows, and journal-attribution set all
/// draw from this join.
fn bound_points(descriptor: &ComponentDescriptor) -> BTreeSet<PointId> {
    descriptor
        .ports
        .iter()
        .filter_map(|port| port.point)
        .collect()
}

/// The serving layer's component-attribution rule, mirrored page-side
/// in `equipmentJournalEntries`: transitions on the instance's bound
/// points, command receipts its commands settle (addressed by name or
/// by bound point), its step failures, and its kind-emitted events.
fn attributed(entry: &JournalEntry, name: &str, points: &BTreeSet<PointId>) -> bool {
    match &entry.event {
        JournalEvent::QualityChanged { point, .. }
        | JournalEvent::PointChanged { point, .. }
        | JournalEvent::FieldClaimLost { point, .. }
        | JournalEvent::FieldClaimObserved { point, .. } => points.contains(point),
        JournalEvent::CommandSettled { receipt } => {
            receipt.command.component() == Some(name)
                || receipt
                    .command
                    .point()
                    .is_some_and(|point| points.contains(&point))
        }
        JournalEvent::StepFailed { component, .. } => component == name,
        JournalEvent::EventEmitted { event } => event.component == name,
        _ => false,
    }
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

#[test]
fn the_join_yields_every_navigation_target() {
    with_monitor(|driver, client| {
        // Trip the first alarm through its process input, shelve it
        // through the receipted path, and let the transitions settle —
        // the standing row plus a journaled burst to navigate from.
        driver.write(PV1, Value::Float(95.0)).unwrap();
        client.advance(1).unwrap();
        let receipt = client
            .command(&Command::WriteValue {
                point: SHELVE1,
                kind: ValueKind::Bool,
                value: Value::Bool(true),
            })
            .unwrap();
        assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        let snapshot = client.advance(1).unwrap();

        // Every row's component label is a navigation target — the
        // descriptor join resolves the diagnostic name to the
        // faceplate's self-description.
        let rows = alarm_rows(&snapshot);
        assert!(!rows.is_empty());
        for row in &rows {
            let target = descriptor(&snapshot, row.component);
            // The row's port belongs to the navigated component — the
            // link label and its target join on the same descriptor.
            assert!(
                target
                    .ports
                    .iter()
                    .any(|port| port.name == row.port && port.role == Some(PortRole::Status)),
                "{}'s row names no status port on the target",
                row.component
            );
        }
        assert!(rows.iter().any(|row| row.component == ALARM1_NAME));

        // The equipment surface's sections join for the selected
        // component — each over served payloads alone.
        let target = descriptor(&snapshot, ALARM1_NAME);
        let bound = bound_points(target);
        assert!(bound.contains(&PV1));
        assert!(bound.contains(&ALARM1));

        // Live points with quality: every bound point reports a good
        // telemetry sample the pane's live-points table reads.
        for point in &bound {
            let sample = telemetry(&snapshot, *point).sample.unwrap();
            assert_eq!(sample.quality, Quality::Good);
        }

        // Trend/history windows: every bound point's retained series
        // is served through the history join the pane draws from.
        let histories = client
            .history(&bound.iter().copied().collect::<Vec<_>>(), 0)
            .unwrap();
        for history in &histories {
            assert!(
                bound.contains(&history.point),
                "history served for unbound point {:?}",
                history.point
            );
        }
        assert!(
            histories
                .iter()
                .any(|history| history.point == ALARM1 && !history.samples.is_empty()),
            "the alarm flag's trend window has retained samples"
        );

        // The component-attributed journal slice: the shelve write's
        // settled receipt and the status transitions it caused all
        // attribute to the owning component.
        let journal = client.journal(0).unwrap();
        let entries: Vec<_> = journal
            .iter()
            .filter(|entry| attributed(entry, ALARM1_NAME, &bound))
            .collect();
        assert!(
            entries.iter().any(|entry| matches!(
                &entry.event,
                JournalEvent::PointChanged { point, .. } if *point == SHELVED1
            )),
            "the shelved transition attributes to {ALARM1_NAME}"
        );
        assert!(
            entries.iter().any(|entry| matches!(
                &entry.event,
                JournalEvent::CommandSettled { receipt }
                    if matches!(&receipt.command,
                        Command::WriteValue { point, .. } if *point == SHELVE1)
            )),
            "the settled shelve receipt attributes to {ALARM1_NAME}"
        );
        // Component-addressed commands attribute by name — the
        // SetParameter path the faceplate's tuning controls issue.
        assert_eq!(
            Command::SetParameter {
                component: ALARM1_NAME.to_string(),
                name: "priority".to_string(),
                value: Value::Int(1),
            }
            .component(),
            Some(ALARM1_NAME)
        );
        // The slice keeps the durable record's seq order.
        let seqs: Vec<u64> = entries.iter().map(|entry| entry.seq).collect();
        let mut sorted = seqs.clone();
        sorted.sort_unstable();
        assert_eq!(seqs, sorted, "the attributed slice keeps seq order");

        // The parameters join — the row's priority cell and the
        // faceplate's tuning surface read the same section.
        let values = parameter_values(&snapshot, ALARM1_NAME);
        assert_eq!(values["priority"], Value::Int(1));
        assert_eq!(values["class"], Value::Int(2));
        assert_eq!(values["response_ticks"], Value::Int(30));

        // The rationalization record joins from the served index's
        // components section by the same diagnostic name the
        // navigation targets.
        let index = client.signals().unwrap();
        let record = index
            .components
            .iter()
            .find(|record| record.name == ALARM1_NAME)
            .unwrap();
        assert!(record.rationalization.is_some());

        // The health lines' source sections: the snapshot's io_health
        // reports, and the pair line reads the served role reports —
        // absent in this single-controller rig, where the pane's
        // standalone-controller line stands.
        assert_eq!(snapshot.io_health.failed_reads, 0);
    });
}

#[test]
fn managed_rows_navigate_to_the_same_targets() {
    with_monitor(|driver, client| {
        // Trip both alarms and drive every managed state — each routed
        // row's component label and priority cell target the same
        // equipment join as a standing row's.
        driver.write(PV1, Value::Float(95.0)).unwrap();
        driver.write(PV2, Value::Bool(true)).unwrap();
        driver.write(SUPPRESS2, Value::Bool(true)).unwrap();
        for point in [SHELVE1, OOS1_IN, OOS2_IN] {
            client
                .command(&Command::WriteValue {
                    point,
                    kind: ValueKind::Bool,
                    value: Value::Bool(true),
                })
                .unwrap();
        }
        let snapshot = client.advance(1).unwrap();

        let rows = alarm_rows(&snapshot);
        assert_eq!(rows.len(), 7, "{rows:?}");
        // Every row is routed to a managed list — the standing list
        // renders empty — yet each keeps its navigation target.
        assert!(
            rows.iter()
                .all(|row| !row.states.is_disjoint(&BTreeSet::from([
                    "shelved",
                    "suppressed",
                    "out_of_service"
                ]))),
            "{rows:?}"
        );
        // Every routed row — shelved, suppressed, out-of-service —
        // resolves to a descriptor whose parameters and index record
        // supply the row's priority cell and the pane's
        // rationalization block.
        let index = client.signals().unwrap();
        for row in &rows {
            let target = descriptor(&snapshot, row.component);
            assert!(
                !bound_points(target).is_empty(),
                "{} has no bound points to navigate to",
                row.component
            );
            assert!(
                parameter_values(&snapshot, row.component).contains_key("priority"),
                "{} lacks the priority cell's join",
                row.component
            );
            assert!(
                index
                    .components
                    .iter()
                    .any(|record| record.name == row.component && record.rationalization.is_some()),
                "{} lacks the rationalization record",
                row.component
            );
        }
    });
}

#[test]
fn page_serves_the_equipment_navigation_markup() {
    with_monitor(|_driver, client| {
        let page = client.page().unwrap();
        // The pane and its sections: faceplate, live points, trend
        // windows, journal slice, rationalization, and the health
        // lines.
        for needle in [
            "id=\"equipment\"",
            "id=\"equipment-name\"",
            "id=\"equipment-faceplate\"",
            "id=\"equipment-points\"",
            "id=\"equipment-trends\"",
            "id=\"equipment-journal\"",
            "id=\"equipment-rationalization\"",
            "id=\"equipment-pair\"",
            "id=\"equipment-io\"",
            "id=\"equipment-close\"",
            "function renderEquipment(",
            "function equipmentPoints(",
            "function equipmentJournalEntries(",
        ] {
            assert!(page.contains(needle), "page lacks {needle}");
        }
        // The alarm-to-faceplate routing: the nav link every row's
        // component and priority cells and each attributable journal
        // entry carries, the delegated handler selecting the owning
        // component, and the immediate re-render from the cached
        // poll inputs.
        for needle in [
            "function equipmentLink(",
            "nav-equipment",
            "data-component",
            "function alarmEntryComponent(",
            "equipmentSelection = nav.dataset.component",
            "renderEquipment(rendered.snapshot",
            "href=\\\"#equipment\\\"",
        ] {
            assert!(page.contains(needle), "page lacks {needle}");
        }
        // The pane's duplicated faceplate operable through the same
        // delegated receipted paths — the identical handlers, not a
        // divergent command path.
        for needle in [
            "getElementById(\"equipment\").addEventListener(\"click\", faceplateClick)",
            "getElementById(\"equipment\").addEventListener(\"keydown\", faceplateKeydown)",
            "faceplateMarkup(descriptor",
        ] {
            assert!(page.contains(needle), "page lacks {needle}");
        }
        // Managed-list counts and the named-state routing preserved.
        for needle in [
            "id=\"managed-lists\"",
            "managed-count",
            "MANAGED_STATES",
            "function managedListMarkup(",
        ] {
            assert!(page.contains(needle), "page lacks {needle}");
        }
        // Priority redundant coding — colour class plus textual level.
        for needle in [
            "PRIORITY_VOCABULARY",
            "function priorityMarkup(",
            "priority.p1",
        ] {
            assert!(page.contains(needle), "page lacks {needle}");
        }
        // The rationalization record rendered on the equipment
        // surface through the same components-section join.
        for needle in [
            "function rationalizationMarkup(",
            "block.consequence",
            "block.required_action",
            "block.reference",
        ] {
            assert!(page.contains(needle), "page lacks {needle}");
        }
        // The health lines: the shared I/O-health summary and the
        // pair view's own health helpers — not a second policy.
        for needle in [
            "function ioHealthSummary(",
            "ioHealthSummary(snapshot.io_health)",
            "pairHealth(peers, peerState)",
            "pairSummary(health)",
        ] {
            assert!(page.contains(needle), "page lacks {needle}");
        }
        // First-out ordering on both journal slices.
        assert!(page.contains("a.seq - b.seq"), "page lacks seq ordering");
        assert!(!page.contains("src="), "page references external assets");
    });
}

#[test]
fn absent_sections_decode_empty() {
    with_monitor(|_driver, client| {
        // The serde-default shape from before the sections existed:
        // strip the sections the equipment pane joins and the
        // document still decodes — the page's `|| []`/`|| {}` guards
        // render the empty join rather than failing the poll.
        let (status, body) = client.request("GET", "/snapshot", None).unwrap();
        assert_eq!(status, 200, "{body}");
        let mut document: serde_json::Value = serde_json::from_str(&body).unwrap();
        document.as_object_mut().unwrap().remove("parameters");
        document.as_object_mut().unwrap().remove("forces");
        let legacy: TelemetrySnapshot = serde_json::from_value(document).unwrap();
        assert!(legacy.parameters.is_empty());
        assert!(legacy.forces.is_empty());
        // The descriptor and telemetry joins survive the stripped
        // sections — the navigation targets still resolve.
        assert!(!legacy.descriptors.is_empty());
        assert!(!legacy.points.is_empty());

        // The served index's components section — the rationalization
        // join — degrades the same way.
        let (status, body) = client.request("GET", "/signals", None).unwrap();
        assert_eq!(status, 200, "{body}");
        let mut document: serde_json::Value = serde_json::from_str(&body).unwrap();
        document.as_object_mut().unwrap().remove("components");
        let legacy: SignalIndex = serde_json::from_value(document).unwrap();
        assert!(legacy.components.is_empty());

        // A history window with no retained samples serves an empty
        // list — the pane's trend draws empty rather than failing.
        let histories = client.history(&[PROT_TRIP], 0).unwrap();
        assert!(
            histories
                .iter()
                .find(|history| history.point == PROT_TRIP)
                .is_none_or(|history| history.samples.is_empty()),
            "an untrended point serves no fabricated samples"
        );
    });
}

/// One rendered equipment-pane join: the selected component's
/// bound-point samples, its attributed journal seqs, and the priority
/// cell's value.
type EquipmentRender = (Vec<(PointId, Option<Sample>)>, Vec<u64>, Value);

/// The scripted sequence the determinism check replays: trip the
/// first alarm, shelve it, retune the bound so expiry lands, release
/// the request — answering the equipment pane's navigation model
/// after each step.
fn scripted_run() -> Vec<EquipmentRender> {
    with_monitor(|driver, client| {
        let collect = |snapshot: &TelemetrySnapshot| {
            let target = descriptor(snapshot, ALARM1_NAME);
            let bound = bound_points(target);
            let points = bound
                .iter()
                .map(|point| (*point, telemetry(snapshot, *point).sample))
                .collect::<Vec<_>>();
            let journal = client.journal(0).unwrap();
            let seqs = journal
                .iter()
                .filter(|entry| attributed(entry, ALARM1_NAME, &bound))
                .map(|entry| entry.seq)
                .collect::<Vec<_>>();
            let priority = parameter_values(snapshot, ALARM1_NAME)["priority"];
            (points, seqs, priority)
        };
        let mut renders = Vec::new();
        driver.write(PV1, Value::Float(95.0)).unwrap();
        renders.push(collect(&client.advance(1).unwrap()));
        client
            .command(&Command::WriteValue {
                point: SHELVE1,
                kind: ValueKind::Bool,
                value: Value::Bool(true),
            })
            .unwrap();
        renders.push(collect(&client.advance(1).unwrap()));
        client
            .command(&Command::SetParameter {
                component: ALARM1_NAME.to_string(),
                name: "max_shelve_ticks".to_string(),
                value: Value::Int(1),
            })
            .unwrap();
        renders.push(collect(&client.advance(1).unwrap()));
        renders
    })
}

#[test]
fn identical_scripted_runs_render_identically() {
    // No wall-clock or scheduling input reaches the navigation model:
    // two rigs running the same command sequence produce the same
    // bound-point samples, attributed journal seqs, and priority join.
    assert_eq!(scripted_run(), scripted_run());
}
