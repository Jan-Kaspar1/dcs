//! Snapshot-level coverage for component self-descriptors: an executor
//! holding one instance of every `dcs-blocks` kind reports each kind's
//! `describe()` result in scan order, and the extended
//! `TelemetrySnapshot` serde-roundtrips.

use dcs_blocks::{
    AlarmLimits, AlarmMonitor, AnalogInput, AnalogOutput, Counter, DigitalInput, DigitalOutput,
    Interlock, LatchingAlarm, ManualStation, MedianVoter, Motor, OverrideSelect, Pid, PidConfig,
    RateLimiter, Scaling, SignalFilter, Timer, Totalizer, Valve,
};
use dcs_core::{Direction, PointId, TelemetrySnapshot, Value, ValueKind};
use dcs_runtime::{Component, Executor, PointMap};
use dcs_sim::{ChannelId, ChannelMap, PointBinding, SimDriver};

/// Records `id` in `specs` and returns it as a [`PointId`], so a rig's
/// point declarations and the point map built from them cannot drift.
fn point(
    specs: &mut Vec<(PointId, Direction, ValueKind)>,
    id: u64,
    direction: Direction,
    kind: ValueKind,
) -> PointId {
    specs.push((PointId(id), direction, kind));
    PointId(id)
}

/// One instance of every `dcs-blocks` kind plus the `(point, direction,
/// kind)` specs their declared I/O resolves against.
struct Rig {
    components: Vec<Box<dyn Component>>,
    specs: Vec<(PointId, Direction, ValueKind)>,
}

/// Builds the rig. Registration order is deliberately not the kinds'
/// declaration order in the crate, so the snapshot proves descriptors
/// follow scan order.
fn rig() -> Rig {
    let mut specs = Vec::new();
    let components: Vec<Box<dyn Component>> = vec![
        Box::new(Motor::new(
            "mtr",
            point(&mut specs, 100, Direction::In, ValueKind::Bool),
            point(&mut specs, 101, Direction::Out, ValueKind::Bool),
            point(&mut specs, 102, Direction::In, ValueKind::Bool),
            point(&mut specs, 103, Direction::Out, ValueKind::Bool),
        )),
        Box::new(
            AnalogInput::<f64>::new(
                "ai",
                point(&mut specs, 10, Direction::In, ValueKind::Float),
                point(&mut specs, 11, Direction::Out, ValueKind::Float),
                Scaling {
                    raw_min: 4.0,
                    raw_max: 20.0,
                    eng_min: 0.0,
                    eng_max: 100.0,
                },
            )
            .unwrap(),
        ),
        Box::new(
            Pid::new(
                "pid",
                point(&mut specs, 50, Direction::In, ValueKind::Float),
                point(&mut specs, 51, Direction::In, ValueKind::Float),
                point(&mut specs, 52, Direction::Out, ValueKind::Float),
                PidConfig {
                    kp: 2.0,
                    ki: 1.0,
                    kd: 0.5,
                    dt: 0.1,
                    out_min: 0.0,
                    out_max: 15.0,
                },
            )
            .unwrap(),
        ),
        Box::new(
            Valve::new(
                "vlv",
                point(&mut specs, 90, Direction::In, ValueKind::Float),
                point(&mut specs, 91, Direction::Out, ValueKind::Float),
                point(&mut specs, 92, Direction::In, ValueKind::Float),
                point(&mut specs, 93, Direction::Out, ValueKind::Bool),
                2.0,
            )
            .unwrap(),
        ),
        Box::new(DigitalInput::new(
            "di",
            point(&mut specs, 20, Direction::In, ValueKind::Bool),
            point(&mut specs, 21, Direction::Out, ValueKind::Bool),
        )),
        Box::new(
            Interlock::new(
                "ilk",
                point(&mut specs, 70, Direction::In, ValueKind::Float),
                point(&mut specs, 71, Direction::In, ValueKind::Bool),
                vec![point(&mut specs, 72, Direction::In, ValueKind::Bool)],
                point(&mut specs, 73, Direction::Out, ValueKind::Float),
                point(&mut specs, 74, Direction::Out, ValueKind::Bool),
                0.0,
            )
            .unwrap(),
        ),
        Box::new(
            AnalogOutput::<f64>::new(
                "ao",
                point(&mut specs, 30, Direction::In, ValueKind::Float),
                point(&mut specs, 31, Direction::Out, ValueKind::Float),
                Scaling {
                    raw_min: 4.0,
                    raw_max: 20.0,
                    eng_min: 0.0,
                    eng_max: 100.0,
                },
            )
            .unwrap(),
        ),
        Box::new(OverrideSelect::new(
            "ovr",
            point(&mut specs, 80, Direction::In, ValueKind::Float),
            point(&mut specs, 81, Direction::In, ValueKind::Float),
            point(&mut specs, 82, Direction::In, ValueKind::Bool),
            point(&mut specs, 83, Direction::Out, ValueKind::Float),
        )),
        Box::new(
            AlarmMonitor::new(
                "alm",
                point(&mut specs, 60, Direction::In, ValueKind::Float),
                point(&mut specs, 61, Direction::Out, ValueKind::Bool),
                AlarmLimits {
                    low: 10.0,
                    high: 90.0,
                    hysteresis: 5.0,
                },
            )
            .unwrap(),
        ),
        Box::new(DigitalOutput::new(
            "do",
            point(&mut specs, 40, Direction::In, ValueKind::Bool),
            point(&mut specs, 41, Direction::Out, ValueKind::Bool),
        )),
        Box::new(
            Timer::new(
                "tmr",
                point(&mut specs, 110, Direction::In, ValueKind::Bool),
                point(&mut specs, 111, Direction::Out, ValueKind::Bool),
                3,
            )
            .unwrap(),
        ),
        Box::new(
            Counter::new(
                "ctr",
                point(&mut specs, 120, Direction::In, ValueKind::Bool),
                point(&mut specs, 121, Direction::In, ValueKind::Bool),
                point(&mut specs, 122, Direction::Out, ValueKind::Int),
                point(&mut specs, 123, Direction::Out, ValueKind::Bool),
                4,
            )
            .unwrap(),
        ),
        Box::new(
            RateLimiter::new(
                "rl",
                point(&mut specs, 130, Direction::In, ValueKind::Float),
                point(&mut specs, 131, Direction::Out, ValueKind::Float),
                2.5,
            )
            .unwrap(),
        ),
        Box::new(
            LatchingAlarm::new(
                "lal",
                point(&mut specs, 160, Direction::In, ValueKind::Float),
                point(&mut specs, 161, Direction::In, ValueKind::Bool),
                point(&mut specs, 162, Direction::Out, ValueKind::Bool),
                point(&mut specs, 163, Direction::Out, ValueKind::Bool),
                AlarmLimits {
                    low: 10.0,
                    high: 90.0,
                    hysteresis: 5.0,
                },
            )
            .unwrap(),
        ),
        Box::new(
            ManualStation::new(
                "mas",
                point(&mut specs, 140, Direction::In, ValueKind::Float),
                point(&mut specs, 141, Direction::In, ValueKind::Float),
                point(&mut specs, 142, Direction::In, ValueKind::Bool),
                point(&mut specs, 143, Direction::Out, ValueKind::Float),
                point(&mut specs, 144, Direction::Out, ValueKind::Bool),
                5.0,
            )
            .unwrap(),
        ),
        Box::new(
            SignalFilter::new(
                "filt",
                point(&mut specs, 150, Direction::In, ValueKind::Float),
                point(&mut specs, 151, Direction::Out, ValueKind::Float),
                0.5,
            )
            .unwrap(),
        ),
        Box::new(
            MedianVoter::new(
                "vot",
                point(&mut specs, 170, Direction::In, ValueKind::Float),
                point(&mut specs, 171, Direction::In, ValueKind::Float),
                point(&mut specs, 172, Direction::In, ValueKind::Float),
                point(&mut specs, 173, Direction::Out, ValueKind::Float),
                point(&mut specs, 174, Direction::Out, ValueKind::Bool),
                2.0,
            )
            .unwrap(),
        ),
        Box::new(
            Totalizer::new(
                "tot",
                point(&mut specs, 180, Direction::In, ValueKind::Float),
                point(&mut specs, 181, Direction::In, ValueKind::Bool),
                point(&mut specs, 182, Direction::Out, ValueKind::Float),
                1.0,
                0.0,
            )
            .unwrap(),
        ),
    ];
    Rig { components, specs }
}

/// The kinds' registered kind strings in the rig's scan order.
const EXPECTED_KINDS: [&str; 18] = [
    Motor::KIND,
    AnalogInput::<f64>::KIND,
    Pid::KIND,
    Valve::KIND,
    DigitalInput::KIND,
    Interlock::KIND,
    AnalogOutput::<f64>::KIND,
    OverrideSelect::KIND,
    AlarmMonitor::KIND,
    DigitalOutput::KIND,
    Timer::KIND,
    Counter::KIND,
    RateLimiter::KIND,
    LatchingAlarm::KIND,
    ManualStation::KIND,
    SignalFilter::KIND,
    MedianVoter::KIND,
    Totalizer::KIND,
];

fn snapshot() -> TelemetrySnapshot {
    let Rig { components, specs } = rig();
    let channel_map = specs
        .iter()
        .map(|&(point, direction, kind)| PointBinding {
            point,
            channel: ChannelId {
                device: 1,
                name: format!("ch{}", point.0),
            },
            direction: match direction {
                Direction::In => dcs_sim::Direction::In,
                Direction::Out => dcs_sim::Direction::Out,
            },
            initial: match kind {
                ValueKind::Bool => Value::Bool(false),
                ValueKind::Int => Value::Int(0),
                ValueKind::Float => Value::Float(0.0),
            },
        })
        .fold(ChannelMap::new(), ChannelMap::with_point);
    let sim = SimDriver::new(channel_map).unwrap();
    let point_map: PointMap = specs.into_iter().collect();
    let mut executor = Executor::new(&sim, point_map, components).unwrap();
    executor.scan().unwrap();
    executor.snapshot()
}

#[test]
fn snapshot_reports_every_kind_descriptor_in_scan_order() {
    let snapshot = snapshot();
    let kinds: Vec<&str> = snapshot
        .descriptors
        .iter()
        .map(|descriptor| descriptor.kind.as_str())
        .collect();
    assert_eq!(kinds, EXPECTED_KINDS);

    // Descriptors align 1:1 with the diagnostics, in scan order.
    assert_eq!(snapshot.descriptors.len(), snapshot.components.len());
    for (descriptor, diagnostics) in snapshot.descriptors.iter().zip(snapshot.components.iter()) {
        assert_eq!(descriptor.name, diagnostics.name);
    }

    // Every kind role-hints every port and names its parameter keys.
    for descriptor in &snapshot.descriptors {
        assert!(
            descriptor.ports.iter().all(|port| port.role.is_some()),
            "{} has an unhinted port",
            descriptor.name
        );
    }
    let with_parameters: Vec<&str> = snapshot
        .descriptors
        .iter()
        .filter(|descriptor| !descriptor.parameters.is_empty())
        .map(|descriptor| descriptor.name.as_str())
        .collect();
    // OverrideSelect is the only parameterless kind.
    assert_eq!(with_parameters.len(), EXPECTED_KINDS.len() - 1);
}

#[test]
fn descriptor_snapshot_serde_roundtrips() {
    let snapshot = snapshot();
    let json = serde_json::to_string(&snapshot).unwrap();
    assert_eq!(
        serde_json::from_str::<TelemetrySnapshot>(&json).unwrap(),
        snapshot
    );
}
