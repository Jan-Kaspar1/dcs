//! Wire compatibility checks for optional snapshot and signal-index sections.

use dcs_blocks::{
    AlarmLimits, ManagedAlarmConfig, ManagedAlarmIo, ManagedBoolLatchingAlarm, ManagedLatchingAlarm,
};
use dcs_core::{
    Direction, IoDriver, IoError, PointId, Sample, TelemetrySnapshot, Tick, Value, ValueKind,
};
use dcs_model::{PlantModel, SignalIndex};
use dcs_monitor::{Monitor, MonitorClient};
use dcs_runtime::{Executor, PointMap, PointSpec};
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
        record_every_ticks: None,
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
        record_every_ticks: None,
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
