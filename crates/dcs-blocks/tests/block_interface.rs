//! Block-interface mapping tests: representative registered kinds
//! derive their five [`BlockInterface`] collections from their
//! descriptor/spec data — the decision-82 compatibility mapping.
//!
//! The all-kind sweep lives in `spec_drift.rs`, where every registered
//! `KIND`'s spec, `describe()` descriptor, and derived interface are
//! pinned together. These tests pin the *separation* the sweep checks
//! structurally: which ports become `measurements` versus `state`, the
//! parameters as `configuration`, and the adapted command and event
//! identities a consumer reads.

use dcs_blocks::{
    AlarmLimits, GroupOutputs, LatchingAlarm, Motor, PumpGroup, PumpGroupConfig, PumpIo,
    RotationPolicy, SetpointTable, ThresholdChain, ThresholdOutputs, Valve,
};
use dcs_core::{
    AdaptedCommand, AdaptedEvent, BlockInterface, CommandAvailability, ConfigCapability, Direction,
    EventEmission, EventRetention, StatePersistence, ValueKind,
};
use dcs_runtime::Component;

fn point(id: u64) -> dcs_core::PointId {
    dcs_core::PointId(id)
}

fn measurement_names(interface: &BlockInterface) -> Vec<&str> {
    interface
        .measurements
        .iter()
        .map(|entry| entry.name.as_str())
        .collect()
}

fn state_names(interface: &BlockInterface) -> Vec<&str> {
    interface
        .state
        .iter()
        .map(|entry| entry.name.as_str())
        .collect()
}

fn configuration_names(interface: &BlockInterface) -> Vec<&str> {
    interface
        .configuration
        .iter()
        .map(|entry| entry.name.as_str())
        .collect()
}

fn command_names(interface: &BlockInterface) -> Vec<&str> {
    interface
        .commands
        .iter()
        .map(|entry| entry.name.as_str())
        .collect()
}

fn event_names(interface: &BlockInterface) -> Vec<&str> {
    interface
        .events
        .iter()
        .map(|entry| entry.name.as_str())
        .collect()
}

/// `pump-group` with two pumps: the setpoint `demand` input, the
/// per-pump `cmd_N` driven outputs and `run_N`/`fault_N`/`avail_N`
/// feedbacks are measurements; the group `Status` outputs are runtime
/// state.
#[test]
fn pump_group_interface_separates_measurements_state_and_configuration() {
    let component = PumpGroup::new(
        "pg",
        point(1),
        vec![
            PumpIo {
                cmd: point(2),
                run: point(3),
                fault: point(4),
                avail: point(5),
            },
            PumpIo {
                cmd: point(6),
                run: point(7),
                fault: point(8),
                avail: point(9),
            },
        ],
        GroupOutputs {
            duty: point(10),
            staged: point(11),
            none_available: point(12),
            all_faulted: point(13),
        },
        PumpGroupConfig {
            rotation: RotationPolicy::AlternateEachCycle,
            rotation_ticks: 0,
            start_delay_ticks: 0,
            restage_delay_ticks: 0,
            min_off_ticks: 0,
        },
    )
    .unwrap();
    let interface = BlockInterface::from_descriptor(&component.describe());

    assert_eq!(interface.kind, "pump-group");
    assert_eq!(
        measurement_names(&interface),
        [
            "demand", "cmd_1", "run_1", "fault_1", "avail_1", "cmd_2", "run_2", "fault_2",
            "avail_2",
        ]
    );
    assert_eq!(
        state_names(&interface),
        ["duty", "staged", "none_available", "all_faulted"]
    );
    // `duty`/`staged` are `Int` state; the availability/fault flags are
    // `Bool` — each keeps the port's direction and kind.
    let duty = interface
        .state
        .iter()
        .find(|entry| entry.name == "duty")
        .unwrap();
    assert_eq!(duty.direction, Direction::Out);
    assert_eq!(duty.kind, ValueKind::Int);
    assert_eq!(duty.persistence, StatePersistence::BoundPoint);

    assert_eq!(
        configuration_names(&interface),
        [
            "rotation",
            "rotation_ticks",
            "start_delay_ticks",
            "restage_delay_ticks",
            "min_off_ticks",
        ]
    );
    assert!(
        interface
            .configuration
            .iter()
            .all(|entry| entry.capability == ConfigCapability::Tunable)
    );

    // The command surface: the point-command triple on every `In`
    // port — `demand` and the per-pump feedbacks — plus one
    // `set_parameter` per tunable. `Out` ports like `cmd_1` are never
    // command targets, so no `write_value:cmd_1` exists.
    let names = command_names(&interface);
    for port in [
        "demand", "run_1", "fault_1", "avail_1", "run_2", "fault_2", "avail_2",
    ] {
        for verb in ["write_value", "force_point", "unforce_point"] {
            assert!(
                names.contains(&format!("{verb}:{port}").as_str()),
                "{verb}:{port}"
            );
        }
    }
    assert!(!names.contains(&"write_value:cmd_1"));
    assert!(!names.contains(&"write_value:duty"));
    assert!(names.contains(&"set_parameter:rotation"));
    let tune = interface
        .commands
        .iter()
        .find(|command| command.name == "set_parameter:rotation")
        .unwrap();
    assert_eq!(tune.adapted, AdaptedCommand::SetParameter);
    assert_eq!(tune.request[0].kind, ValueKind::Int);

    // Every port here is `Bool`/`Int`, so each may emit the durable
    // `point_changed` transition where the model marks its bound point
    // `journaled`, beside the always-on `quality_changed`.
    let events = event_names(&interface);
    for port in [
        "demand",
        "cmd_1",
        "run_1",
        "duty",
        "none_available",
        "all_faulted",
    ] {
        assert!(
            events.contains(&format!("point_changed:{port}").as_str()),
            "point_changed:{port}"
        );
        assert!(
            events.contains(&format!("quality_changed:{port}").as_str()),
            "quality_changed:{port}"
        );
    }
    assert!(events.contains(&"command_settled"));
    assert!(events.contains(&"step_failed"));
}

/// `latching-alarm`: the `in` process value is the only measurement;
/// the `ack` input and the `alarm`/`unacknowledged` outputs all hint
/// `Status`, so all three are runtime state — direction is preserved,
/// so a consumer still tells the operator-driven `ack` from the
/// reported flags.
#[test]
fn latching_alarm_interface_files_status_ports_as_state() {
    let component = LatchingAlarm::new(
        "lal",
        point(1),
        point(2),
        point(3),
        point(4),
        AlarmLimits {
            low: 10.0,
            high: 90.0,
            hysteresis: 5.0,
        },
        dcs_blocks::Rationalization {
            priority: 1,
            class: 2,
            response_ticks: 30,
        },
    )
    .unwrap();
    let interface = component.describe().interface();

    assert_eq!(interface.kind, "latching-alarm");
    assert_eq!(measurement_names(&interface), ["in"]);
    assert_eq!(state_names(&interface), ["ack", "alarm", "unacknowledged"]);
    let ack = interface
        .state
        .iter()
        .find(|entry| entry.name == "ack")
        .unwrap();
    assert_eq!(ack.direction, Direction::In);
    assert_eq!(ack.kind, ValueKind::Bool);

    assert_eq!(
        configuration_names(&interface),
        [
            "low_limit",
            "high_limit",
            "hysteresis",
            "priority",
            "class",
            "response_ticks",
        ]
    );

    // `ack` is the operator-facing input — the point-command surface
    // adapts onto it exactly like the measurement `in`.
    let names = command_names(&interface);
    for port in ["in", "ack"] {
        for verb in ["write_value", "force_point", "unforce_point"] {
            assert!(
                names.contains(&format!("{verb}:{port}").as_str()),
                "{verb}:{port}"
            );
        }
    }
    assert!(!names.contains(&"write_value:alarm"));

    // `in` is `Float` — the durable `point_changed` record cannot
    // carry it — while the `Bool` status flags may.
    let events = event_names(&interface);
    assert!(!events.contains(&"point_changed:in"));
    assert!(events.contains(&"quality_changed:in"));
    for port in ["ack", "alarm", "unacknowledged"] {
        assert!(
            events.contains(&format!("point_changed:{port}").as_str()),
            "point_changed:{port}"
        );
    }
}

/// `threshold-chain`: the `level` process value and the `demand`
/// `Output` it drives are measurements; the four call/flag outputs are
/// runtime state.
#[test]
fn threshold_chain_interface_separates_demand_from_flags() {
    let component = ThresholdChain::new(
        "lch",
        point(1),
        ThresholdOutputs {
            demand: point(2),
            duty_call: point(3),
            lag_call: point(4),
            below_cutoff: point(5),
            high_level: point(6),
        },
        SetpointTable {
            cutoff: 1.0,
            stop: 2.0,
            start: 4.0,
            lag_start: 6.0,
            high: 8.0,
            on_bad_demand: 0,
        },
    )
    .unwrap();
    let interface = component.describe().interface();

    assert_eq!(interface.kind, "threshold-chain");
    assert_eq!(measurement_names(&interface), ["level", "demand"]);
    assert_eq!(
        state_names(&interface),
        ["duty_call", "lag_call", "below_cutoff", "high_level"]
    );

    // The only `In` port is `level`, so the point-command surface is
    // its triple alone; tuning adapts onto the declared parameters.
    let names = command_names(&interface);
    for verb in ["write_value", "force_point", "unforce_point"] {
        assert!(
            names.contains(&format!("{verb}:level").as_str()),
            "{verb}:level"
        );
    }
    for verb in ["write_value", "force_point", "unforce_point"] {
        // `demand` is an `Out` port — never a command target.
        assert!(
            !names.contains(&format!("{verb}:demand").as_str()),
            "{verb}:demand"
        );
    }
    assert!(names.contains(&"set_parameter:on_bad_demand"));
    let tune = interface
        .commands
        .iter()
        .find(|command| command.name == "set_parameter:high")
        .unwrap();
    assert_eq!(tune.adapted, AdaptedCommand::SetParameter);
    assert_eq!(tune.availability, CommandAvailability::Always);

    // `level` and `demand` are `Float`/`Int`: `point_changed` adapts
    // onto the `Int` demand but not the `Float` level.
    let events = event_names(&interface);
    assert!(!events.contains(&"point_changed:level"));
    assert!(events.contains(&"point_changed:demand"));
    let demand_changed = interface
        .events
        .iter()
        .find(|event| event.name == "point_changed:demand")
        .unwrap();
    assert_eq!(demand_changed.adapted, AdaptedEvent::PointChanged);
    assert_eq!(demand_changed.retention, EventRetention::Journal);
    assert_eq!(demand_changed.emission, EventEmission::WhenJournaled);
}

/// `valve` and `motor` — the command-with-feedback pattern: the
/// operator `cmd` setpoint input, the driven `out`, and the `fb`/`run`
/// feedback are measurements; the `discrepancy`/`fault` flag is state.
#[test]
fn valve_and_motor_interfaces_share_the_command_feedback_shape() {
    let valve = Valve::new("vlv", point(1), point(2), point(3), point(4), 2.0).unwrap();
    let interface = valve.describe().interface();
    assert_eq!(interface.kind, "valve");
    assert_eq!(measurement_names(&interface), ["cmd", "out", "fb"]);
    assert_eq!(state_names(&interface), ["discrepancy"]);
    assert_eq!(
        configuration_names(&interface),
        ["tolerance", "discrepancy_ticks"]
    );
    // `cmd`/`fb` are `In` — command targets; `out`/`discrepancy` are
    // `Out` — never writable.
    let names = command_names(&interface);
    for port in ["cmd", "fb"] {
        for verb in ["write_value", "force_point", "unforce_point"] {
            assert!(
                names.contains(&format!("{verb}:{port}").as_str()),
                "{verb}:{port}"
            );
        }
    }
    assert!(
        !names
            .iter()
            .any(|name| { name.ends_with(":out") || name.ends_with(":discrepancy") })
    );
    let write = interface
        .commands
        .iter()
        .find(|command| command.name == "write_value:cmd")
        .unwrap();
    assert_eq!(write.adapted, AdaptedCommand::WriteValue);
    assert_eq!(write.availability, CommandAvailability::BoundPointWritable);
    assert_eq!(write.request[0].kind, ValueKind::Float);

    let motor = Motor::new("mtr", point(1), point(2), point(3), point(4));
    let interface = motor.describe().interface();
    assert_eq!(interface.kind, "motor");
    assert_eq!(measurement_names(&interface), ["cmd", "out", "run"]);
    assert_eq!(state_names(&interface), ["fault"]);
    // Every motor port is `Bool`, so the full `point_changed` surface
    // is journaled-eligible.
    let events = event_names(&interface);
    for port in ["cmd", "out", "run", "fault"] {
        assert!(
            events.contains(&format!("point_changed:{port}").as_str()),
            "point_changed:{port}"
        );
    }
}
