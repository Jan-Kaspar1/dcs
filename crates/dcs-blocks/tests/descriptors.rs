//! Snapshot-level coverage for component self-descriptors: an executor
//! holding one instance of every `dcs-blocks` kind reports each kind's
//! `describe()` result in scan order, and the extended
//! `TelemetrySnapshot` serde-roundtrips.

use dcs_blocks::{
    AlarmLimits, AlarmMonitor, AnalogInput, AnalogOutput, BackwashCoordinator,
    BackwashCoordinatorConfig, BadTermResponse, BlowerGroup, BlowerGroupConfig, BlowerIo,
    BlowerOutputs, BlowerRotation, BoolGate, BoolLatchingAlarm, CoordinationStrategy,
    CoordinatorOutputs, Counter, DemandFallback, DemandFallbackConfig, DemandFallbackIo,
    DigitalInput, DigitalOutput, Edge, EdgeTrigger, FallbackResponse, FeedforwardSum,
    FeedforwardSumConfig, FeedforwardSumIo, FilterIo, GateOperation, GroupOutputs, GuardResponse,
    HeaderCoordinator, HeaderCoordinatorConfig, HeaderOutputs, Interlock, LatchingAlarm,
    ManagedAlarmConfig, ManagedAlarmIo, ManagedBoolLatchingAlarm, ManagedLatchingAlarm,
    ManualStation, MedianVoter, Motor, OverrideSelect, PermissiveInputs, PhaseMode, PhaseMonitor,
    PhaseMonitorIo, Pid, PidConfig, PumpGroup, PumpGroupConfig, PumpIo, QueuePolicy, QueuedState,
    RateLimiter, RateOfRise, Rationalization, RotationPolicy, Scaling, Sequencer, SequencerStep,
    SignalFilter, SrLatch, StagingAuthority, SurgeGuard, SurgeGuardConfig, SurgeGuardIo, Timer,
    Totalizer, UnitBounds, Valve, ZoneIo,
};
use dcs_core::{Command, CommandVerdict, Direction, PointId, TelemetrySnapshot, Value, ValueKind};
use dcs_runtime::{Component, Executor, PointMap};
use dcs_sim::{ChannelId, ChannelMap, PointBinding, SimDriver};
use std::collections::BTreeSet;

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
                Rationalization {
                    priority: 1,
                    class: 2,
                    response_ticks: 30,
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
        Box::new(
            Sequencer::new(
                "seq",
                point(&mut specs, 190, Direction::In, ValueKind::Bool),
                point(&mut specs, 191, Direction::In, ValueKind::Bool),
                point(&mut specs, 192, Direction::Out, ValueKind::Float),
                point(&mut specs, 193, Direction::Out, ValueKind::Int),
                point(&mut specs, 194, Direction::Out, ValueKind::Bool),
                vec![
                    SequencerStep {
                        ticks: 2,
                        value: 10.0,
                    },
                    SequencerStep {
                        ticks: 1,
                        value: 20.0,
                    },
                ],
            )
            .unwrap(),
        ),
        Box::new(BoolGate::new(
            "gate",
            vec![
                point(&mut specs, 200, Direction::In, ValueKind::Bool),
                point(&mut specs, 201, Direction::In, ValueKind::Bool),
            ],
            point(&mut specs, 202, Direction::Out, ValueKind::Bool),
            GateOperation::And,
        )),
        Box::new(
            PumpGroup::new(
                "pg",
                point(&mut specs, 230, Direction::In, ValueKind::Int),
                vec![
                    PumpIo {
                        cmd: point(&mut specs, 231, Direction::Out, ValueKind::Bool),
                        run: point(&mut specs, 232, Direction::In, ValueKind::Bool),
                        fault: point(&mut specs, 233, Direction::In, ValueKind::Bool),
                        avail: point(&mut specs, 234, Direction::In, ValueKind::Bool),
                    },
                    PumpIo {
                        cmd: point(&mut specs, 235, Direction::Out, ValueKind::Bool),
                        run: point(&mut specs, 236, Direction::In, ValueKind::Bool),
                        fault: point(&mut specs, 237, Direction::In, ValueKind::Bool),
                        avail: point(&mut specs, 238, Direction::In, ValueKind::Bool),
                    },
                ],
                GroupOutputs {
                    duty: point(&mut specs, 239, Direction::Out, ValueKind::Int),
                    staged: point(&mut specs, 240, Direction::Out, ValueKind::Int),
                    none_available: point(&mut specs, 241, Direction::Out, ValueKind::Bool),
                    all_faulted: point(&mut specs, 242, Direction::Out, ValueKind::Bool),
                },
                PumpGroupConfig {
                    rotation: RotationPolicy::AlternateEachCycle,
                    rotation_ticks: 0,
                    start_delay_ticks: 1,
                    restage_delay_ticks: 2,
                    min_off_ticks: 3,
                },
            )
            .unwrap(),
        ),
        Box::new(SrLatch::new(
            "srl",
            point(&mut specs, 210, Direction::In, ValueKind::Bool),
            point(&mut specs, 211, Direction::In, ValueKind::Bool),
            point(&mut specs, 212, Direction::Out, ValueKind::Bool),
        )),
        Box::new(EdgeTrigger::new(
            "etr",
            point(&mut specs, 220, Direction::In, ValueKind::Bool),
            point(&mut specs, 221, Direction::Out, ValueKind::Bool),
            Edge::Rising,
        )),
        Box::new(BoolLatchingAlarm::new(
            "bal",
            point(&mut specs, 250, Direction::In, ValueKind::Bool),
            point(&mut specs, 251, Direction::In, ValueKind::Bool),
            point(&mut specs, 252, Direction::Out, ValueKind::Bool),
            point(&mut specs, 253, Direction::Out, ValueKind::Bool),
            Rationalization {
                priority: 1,
                class: 2,
                response_ticks: 30,
            },
        )),
        // The managed siblings, fully bound — `in`/`ack` plus the
        // `shelve`/`oos`/`suppress` inputs and the five status outputs.
        Box::new(
            ManagedLatchingAlarm::new(
                "mlal",
                ManagedAlarmIo {
                    input: point(&mut specs, 300, Direction::In, ValueKind::Float),
                    ack: point(&mut specs, 301, Direction::In, ValueKind::Bool),
                    shelve: Some(point(&mut specs, 302, Direction::In, ValueKind::Bool)),
                    oos: Some(point(&mut specs, 303, Direction::In, ValueKind::Bool)),
                    suppress: Some(point(&mut specs, 304, Direction::In, ValueKind::Bool)),
                    alarm: point(&mut specs, 305, Direction::Out, ValueKind::Bool),
                    unacknowledged: point(&mut specs, 306, Direction::Out, ValueKind::Bool),
                    shelved: point(&mut specs, 307, Direction::Out, ValueKind::Bool),
                    suppressed: point(&mut specs, 308, Direction::Out, ValueKind::Bool),
                    out_of_service: point(&mut specs, 309, Direction::Out, ValueKind::Bool),
                },
                AlarmLimits {
                    low: 10.0,
                    high: 90.0,
                    hysteresis: 5.0,
                },
                ManagedAlarmConfig {
                    max_shelve_ticks: 5,
                    priority: 1,
                    class: 2,
                    response_ticks: 30,
                },
            )
            .unwrap(),
        ),
        Box::new(ManagedBoolLatchingAlarm::new(
            "mbal",
            ManagedAlarmIo {
                input: point(&mut specs, 320, Direction::In, ValueKind::Bool),
                ack: point(&mut specs, 321, Direction::In, ValueKind::Bool),
                shelve: Some(point(&mut specs, 322, Direction::In, ValueKind::Bool)),
                oos: Some(point(&mut specs, 323, Direction::In, ValueKind::Bool)),
                suppress: Some(point(&mut specs, 324, Direction::In, ValueKind::Bool)),
                alarm: point(&mut specs, 325, Direction::Out, ValueKind::Bool),
                unacknowledged: point(&mut specs, 326, Direction::Out, ValueKind::Bool),
                shelved: point(&mut specs, 327, Direction::Out, ValueKind::Bool),
                suppressed: point(&mut specs, 328, Direction::Out, ValueKind::Bool),
                out_of_service: point(&mut specs, 329, Direction::Out, ValueKind::Bool),
            },
            ManagedAlarmConfig {
                max_shelve_ticks: 5,
                priority: 1,
                class: 2,
                response_ticks: 30,
            },
        )),
        Box::new(
            BackwashCoordinator::new(
                "bwc",
                PermissiveInputs {
                    supply_ok: point(&mut specs, 260, Direction::In, ValueKind::Bool),
                    waste_ok: point(&mut specs, 261, Direction::In, ValueKind::Bool),
                    flow_ok: point(&mut specs, 262, Direction::In, ValueKind::Bool),
                },
                Some(point(&mut specs, 263, Direction::In, ValueKind::Int)),
                vec![
                    FilterIo {
                        request: point(&mut specs, 264, Direction::In, ValueKind::Bool),
                        grant: point(&mut specs, 265, Direction::Out, ValueKind::Bool),
                        position: point(&mut specs, 266, Direction::Out, ValueKind::Int),
                    },
                    FilterIo {
                        request: point(&mut specs, 267, Direction::In, ValueKind::Bool),
                        grant: point(&mut specs, 268, Direction::Out, ValueKind::Bool),
                        position: point(&mut specs, 269, Direction::Out, ValueKind::Int),
                    },
                ],
                CoordinatorOutputs {
                    active: point(&mut specs, 270, Direction::Out, ValueKind::Int),
                    queued: point(&mut specs, 271, Direction::Out, ValueKind::Int),
                    resource_blocked: point(&mut specs, 272, Direction::Out, ValueKind::Bool),
                },
                BackwashCoordinatorConfig {
                    queue_policy: QueuePolicy::Fifo,
                    queued_state: QueuedState::KeepFiltering,
                },
            )
            .unwrap(),
        ),
        Box::new(
            HeaderCoordinator::new(
                "hdr",
                point(&mut specs, 280, Direction::In, ValueKind::Float),
                vec![
                    ZoneIo {
                        valve_pos: point(&mut specs, 281, Direction::In, ValueKind::Float),
                        airflow: point(&mut specs, 282, Direction::In, ValueKind::Float),
                        pulsing: point(&mut specs, 283, Direction::In, ValueKind::Bool),
                        pulse_grant: point(&mut specs, 284, Direction::Out, ValueKind::Bool),
                    },
                    ZoneIo {
                        valve_pos: point(&mut specs, 285, Direction::In, ValueKind::Float),
                        airflow: point(&mut specs, 286, Direction::In, ValueKind::Float),
                        pulsing: point(&mut specs, 287, Direction::In, ValueKind::Bool),
                        pulse_grant: point(&mut specs, 288, Direction::Out, ValueKind::Bool),
                    },
                ],
                HeaderOutputs {
                    pressure_sp: point(&mut specs, 289, Direction::Out, ValueKind::Float),
                    blower_demand: point(&mut specs, 290, Direction::Out, ValueKind::Float),
                    most_open: point(&mut specs, 291, Direction::Out, ValueKind::Int),
                    at_bound: point(&mut specs, 292, Direction::Out, ValueKind::Bool),
                    pulse_blocked: point(&mut specs, 293, Direction::Out, ValueKind::Bool),
                },
                HeaderCoordinatorConfig {
                    strategy: CoordinationStrategy::ConstantPressure,
                    pressure_hold: 10.0,
                    pressure_min: 4.0,
                    pressure_max: 16.0,
                    mov_band_lo: 85.0,
                    mov_band_hi: 95.0,
                    adjust_ticks: 3,
                    min_total_airflow: 1.0,
                    max_pulsing: 1,
                },
            )
            .unwrap(),
        ),
        // The blower group fully bound — `approve` wired since its
        // instance runs the operator-approval authority.
        Box::new(
            BlowerGroup::new(
                "bg",
                point(&mut specs, 340, Direction::In, ValueKind::Float),
                Some(point(&mut specs, 341, Direction::In, ValueKind::Bool)),
                vec![
                    BlowerIo {
                        cmd: point(&mut specs, 342, Direction::Out, ValueKind::Bool),
                        run: point(&mut specs, 343, Direction::In, ValueKind::Bool),
                        fault: point(&mut specs, 344, Direction::In, ValueKind::Bool),
                        avail: point(&mut specs, 345, Direction::In, ValueKind::Bool),
                        capacity: point(&mut specs, 346, Direction::Out, ValueKind::Float),
                        vent: point(&mut specs, 347, Direction::Out, ValueKind::Bool),
                    },
                    BlowerIo {
                        cmd: point(&mut specs, 348, Direction::Out, ValueKind::Bool),
                        run: point(&mut specs, 349, Direction::In, ValueKind::Bool),
                        fault: point(&mut specs, 350, Direction::In, ValueKind::Bool),
                        avail: point(&mut specs, 351, Direction::In, ValueKind::Bool),
                        capacity: point(&mut specs, 352, Direction::Out, ValueKind::Float),
                        vent: point(&mut specs, 353, Direction::Out, ValueKind::Bool),
                    },
                ],
                vec![
                    UnitBounds {
                        min_flow: 20.0,
                        max_flow: 100.0,
                        max_current: 90.0,
                    };
                    2
                ],
                BlowerOutputs {
                    staged: point(&mut specs, 354, Direction::Out, ValueKind::Int),
                    none_available: point(&mut specs, 355, Direction::Out, ValueKind::Bool),
                    all_faulted: point(&mut specs, 356, Direction::Out, ValueKind::Bool),
                    staging_pending: point(&mut specs, 357, Direction::Out, ValueKind::Bool),
                    transition: point(&mut specs, 358, Direction::Out, ValueKind::Bool),
                },
                BlowerGroupConfig {
                    staging_authority: StagingAuthority::OperatorApproval,
                    stage_up: 0.9,
                    stage_down: 0.8,
                    min_run_ticks: 0,
                    min_start_interval_ticks: 0,
                    vent_ticks: 2,
                    rotation: BlowerRotation::NoRotation,
                },
            )
            .unwrap(),
        ),
        Box::new(
            PhaseMonitor::new(
                "phm",
                PhaseMonitorIo {
                    input: point(&mut specs, 360, Direction::In, ValueKind::Float),
                    phase: point(&mut specs, 361, Direction::In, ValueKind::Bool),
                    capture: point(&mut specs, 362, Direction::In, ValueKind::Bool),
                    deviation: point(&mut specs, 363, Direction::Out, ValueKind::Float),
                    exceeded: point(&mut specs, 364, Direction::Out, ValueKind::Bool),
                    overdue: point(&mut specs, 365, Direction::Out, ValueKind::Bool),
                },
                0.5,
                5,
                PhaseMode::Absolute,
            )
            .unwrap(),
        ),
        // The surge guard fully bound — `current` wired since its
        // instance carries the minimum-amperage proxy.
        Box::new(
            SurgeGuard::new(
                "sg",
                SurgeGuardIo {
                    demand: point(&mut specs, 370, Direction::In, ValueKind::Float),
                    flow: point(&mut specs, 371, Direction::In, ValueKind::Float),
                    pressure: point(&mut specs, 372, Direction::In, ValueKind::Float),
                    current: Some(point(&mut specs, 373, Direction::In, ValueKind::Float)),
                    surge_trip: point(&mut specs, 374, Direction::In, ValueKind::Bool),
                    out: point(&mut specs, 375, Direction::Out, ValueKind::Float),
                    guarding: point(&mut specs, 376, Direction::Out, ValueKind::Bool),
                    tripped: point(&mut specs, 377, Direction::Out, ValueKind::Bool),
                },
                SurgeGuardConfig {
                    min_flow: 50.0,
                    max_pressure: 30.0,
                    min_current: 40.0,
                    on_guard: GuardResponse::Clamp,
                    trip_value: 0.0,
                },
            )
            .unwrap(),
        ),
        Box::new(
            DemandFallback::new(
                "dfb",
                DemandFallbackIo {
                    input: point(&mut specs, 380, Direction::In, ValueKind::Float),
                    pv: point(&mut specs, 381, Direction::In, ValueKind::Float),
                    out: point(&mut specs, 382, Direction::Out, ValueKind::Float),
                    fallback_active: point(&mut specs, 383, Direction::Out, ValueKind::Bool),
                },
                DemandFallbackConfig {
                    on_bad: FallbackResponse::Hold,
                    fallback_flow: 25.0,
                    safe_flow: 5.0,
                },
            )
            .unwrap(),
        ),
        Box::new(
            FeedforwardSum::new(
                "ffs",
                FeedforwardSumIo {
                    ff: point(&mut specs, 390, Direction::In, ValueKind::Float),
                    trim: point(&mut specs, 391, Direction::In, ValueKind::Float),
                    out: point(&mut specs, 392, Direction::Out, ValueKind::Float),
                    clamped: point(&mut specs, 393, Direction::Out, ValueKind::Bool),
                    fallback_active: point(&mut specs, 394, Direction::Out, ValueKind::Bool),
                },
                FeedforwardSumConfig {
                    trim_min: -10.0,
                    trim_max: 10.0,
                    min_demand: 0.0,
                    max_demand: 100.0,
                    on_bad_ff: BadTermResponse::Drop,
                    on_bad_trim: BadTermResponse::Drop,
                },
            )
            .unwrap(),
        ),
        Box::new(
            RateOfRise::new(
                "ror",
                point(&mut specs, 400, Direction::In, ValueKind::Float),
                point(&mut specs, 401, Direction::Out, ValueKind::Float),
                point(&mut specs, 402, Direction::Out, ValueKind::Bool),
                0.5,
                0.0,
            )
            .unwrap(),
        ),
    ];
    Rig { components, specs }
}

/// The kinds' registered kind strings in the rig's scan order.
const EXPECTED_KINDS: [&str; 34] = [
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
    Sequencer::KIND,
    BoolGate::KIND,
    PumpGroup::KIND,
    SrLatch::KIND,
    EdgeTrigger::KIND,
    BoolLatchingAlarm::KIND,
    ManagedLatchingAlarm::KIND,
    ManagedBoolLatchingAlarm::KIND,
    BackwashCoordinator::KIND,
    HeaderCoordinator::KIND,
    BlowerGroup::KIND,
    PhaseMonitor::KIND,
    SurgeGuard::KIND,
    DemandFallback::KIND,
    FeedforwardSum::KIND,
    RateOfRise::KIND,
];

/// The rig wired for an executor: the simulated driver serving every
/// declared point, the resolved point map, and the component set.
fn wired() -> (SimDriver, PointMap, Vec<Box<dyn Component>>) {
    let Rig { components, specs } = rig();
    let channel_map = specs
        .iter()
        .map(|&(point, direction, kind)| PointBinding {
            point,
            channel: ChannelId {
                device: 1,
                name: format!("ch{}", point.0),
            },
            direction,
            initial: match kind {
                ValueKind::Bool => Value::Bool(false),
                ValueKind::Int => Value::Int(0),
                ValueKind::Float => Value::Float(0.0),
            },
        })
        .fold(ChannelMap::new(), ChannelMap::with_point);
    let sim = SimDriver::new(channel_map).unwrap();
    let point_map: PointMap = specs.into_iter().collect();
    (sim, point_map, components)
}

fn snapshot() -> TelemetrySnapshot {
    let (sim, point_map, components) = wired();
    let mut executor = Executor::new(&sim, point_map, components).unwrap();
    executor.scan();
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
    // OverrideSelect and SrLatch are the rig's parameterless kinds.
    assert_eq!(with_parameters.len(), EXPECTED_KINDS.len() - 2);
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

#[test]
fn reported_parameters_match_each_kinds_declared_set() {
    let snapshot = snapshot();

    // One parameter section per component, aligned by name with the
    // diagnostics and descriptors in scan order.
    assert_eq!(snapshot.parameters.len(), snapshot.components.len());
    for ((parameters, descriptor), diagnostics) in snapshot
        .parameters
        .iter()
        .zip(&snapshot.descriptors)
        .zip(&snapshot.components)
    {
        assert_eq!(parameters.name, diagnostics.name);
        assert_eq!(parameters.name, descriptor.name);
        // Drift guard: the reported names are exactly the kind's
        // declared `ParameterDescriptor` set — every tunable reports,
        // and nothing undeclared leaks.
        let declared: BTreeSet<&str> = descriptor
            .parameters
            .iter()
            .map(|parameter| parameter.name.as_str())
            .collect();
        let reported: BTreeSet<&str> = parameters.values.keys().map(String::as_str).collect();
        assert_eq!(reported, declared, "{}", descriptor.name);
    }
    // OverrideSelect and SrLatch declare no parameters and report an
    // empty set; BoolLatchingAlarm reports the decision-70 codes.
    let ovr = &snapshot.parameters[7];
    assert_eq!(ovr.name, "ovr");
    assert!(ovr.values.is_empty());
    let srl = &snapshot.parameters[21];
    assert_eq!(srl.name, "srl");
    assert!(srl.values.is_empty());
    let bal = &snapshot.parameters[23];
    assert_eq!(bal.name, "bal");
    assert_eq!(bal.values["priority"], Value::Int(1));
    assert_eq!(bal.values["class"], Value::Int(2));
    assert_eq!(bal.values["response_ticks"], Value::Int(30));
}

#[test]
fn reported_values_equal_the_checkpointed_fields() {
    let (sim, point_map, components) = wired();
    let mut executor = Executor::new(&sim, point_map, components).unwrap();
    executor.scan();

    // Every reported value is what the same run's checkpoint persists
    // for the component — the standby and the faceplate read one
    // vocabulary.
    let checkpoint = executor.checkpoint();
    for parameters in &executor.snapshot().parameters {
        let captured = &checkpoint.components[&parameters.name];
        for (name, value) in &parameters.values {
            assert_eq!(
                captured.get(name),
                Some(*value),
                "{}.{name}",
                parameters.name
            );
        }
    }
}

#[test]
fn a_tuned_pid_parameter_reports_and_restores_identically() {
    let (sim, point_map, components) = wired();
    let mut executor = Executor::new(&sim, point_map, components).unwrap();
    executor.scan();

    // A receipted tune on the `pid` instance reports its new value in
    // the next snapshot under the declared name.
    executor.submit_command(Command::SetParameter {
        component: "pid".to_string(),
        name: "kp".to_string(),
        value: Value::Float(3.5),
    });
    executor.scan();
    let pid = &executor.snapshot().parameters[2];
    assert_eq!(pid.name, "pid");
    assert_eq!(pid.values["kp"], Value::Float(3.5));

    // A checkpoint/restore mid-tune reports identically from the fresh
    // executor.
    let checkpoint = executor.checkpoint();
    let (sim, point_map, components) = wired();
    let mut restored = Executor::restore(&sim, point_map, components, &checkpoint, None).unwrap();
    restored.scan();
    assert_eq!(
        restored.snapshot().parameters,
        executor.snapshot().parameters
    );
}

/// Reads `command`'s published verdict on `component` out of the
/// snapshot's `command_verdicts` section — `None` when the component or
/// command carries none.
fn verdict<'a>(
    snapshot: &'a TelemetrySnapshot,
    component: &str,
    command: &str,
) -> Option<&'a CommandVerdict> {
    snapshot
        .command_verdicts
        .iter()
        .find(|entry| entry.name == component)
        .and_then(|entry| entry.verdicts.iter().find(|v| v.name == command))
}

fn invoke(component: &str, command: &str, arguments: &[(&str, Value)]) -> Command {
    Command::Invoke {
        component: component.to_string(),
        command: command.to_string(),
        arguments: arguments
            .iter()
            .map(|(name, value)| (name.to_string(), *value))
            .collect(),
    }
}

#[test]
fn a_completed_sequencer_publishes_advances_standing_refusal() {
    // `seq` is the proving kind: `advance` is `KindDeclared`-available,
    // `reset` `Always`. The post-scan probe publishes the verdicts on
    // the snapshot — exactly the declared `KindDeclared` commands, so
    // `reset` takes no verdict — and the completed table's `advance`
    // verdict carries the same refusal the receipted path settles.
    let (sim, point_map, components) = wired();
    let mut executor = Executor::new(&sim, point_map, components).unwrap();
    executor.scan();

    let snapshot = executor.snapshot();
    let seq = snapshot
        .command_verdicts
        .iter()
        .find(|entry| entry.name == "seq")
        .expect("the probe covers every registered component");
    assert_eq!(
        seq.verdicts,
        [CommandVerdict {
            name: "advance".to_string(),
            available: true,
            refusal: None,
        }]
    );

    // `advance {count: 9}` lands past the table's last step: the run
    // completes and the same scan's probe reports the standing refusal.
    executor.submit_command(invoke("seq", "advance", &[("count", Value::Int(9))]));
    executor.scan();
    let snapshot = executor.snapshot();
    assert_eq!(
        verdict(&snapshot, "seq", "advance"),
        Some(&CommandVerdict {
            name: "advance".to_string(),
            available: false,
            refusal: Some("the sequence has run to its end; reset restarts it".to_string()),
        })
    );
    // `reset` is `Always`-declared: admissible by construction, so it
    // takes no verdict — the section covers exactly the declared
    // `KindDeclared` commands.
    assert_eq!(verdict(&snapshot, "seq", "reset"), None);

    // `reset` re-opens `advance`: the next scan's probe reports it
    // invocable again — the verdicts track checkpointed run state.
    executor.submit_command(invoke("seq", "reset", &[]));
    executor.scan();
    assert_eq!(
        verdict(&executor.snapshot(), "seq", "advance"),
        Some(&CommandVerdict {
            name: "advance".to_string(),
            available: true,
            refusal: None,
        })
    );
}
