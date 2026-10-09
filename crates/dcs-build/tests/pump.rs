//! The public pump composition works without a station or duty allocator.

use dcs_assembly::{DriverRegistry, assemble, resolve_drivers};
use dcs_build::pump::{PumpConfig, PumpInputs, PumpLinks, pump};
use dcs_build::{BuildError, Direction, PlantBuilder, PointId, Value, ValueKind};
use dcs_core::{Command, CommandOutcome};

#[test]
fn independent_pumps_follow_their_own_logical_demand() {
    let mut plant = PlantBuilder::new();
    let device = plant.device("sim-pumps").id;
    let power_fail = plant.internal_input::<bool>(PointId(1), false, false);
    let power_ok = plant.internal_output::<bool>(PointId(2), true);
    let dry_run = plant.internal_output::<bool>(PointId(3), false);
    let measurement = plant.internal_input::<f64>(PointId(4), 2.0, false);
    let guard_anchor = plant.internal_input::<f64>(PointId(5), 0.0, false);
    let guard_true = plant.internal_input::<bool>(PointId(6), true, false);
    let mut pumps = Vec::new();
    let mut demands = Vec::new();

    for index in 0..2 {
        let field = 100 + 100 * index;
        let tag = format!("transfer-{}", index + 1);
        let run_channel = plant.channel::<bool>(device, &format!("{tag}-feedback"), Direction::In);
        let thermal_channel =
            plant.channel::<bool>(device, &format!("{tag}-overload"), Direction::In);
        let moisture_channel =
            plant.channel::<bool>(device, &format!("{tag}-moisture"), Direction::In);
        let command_channel =
            plant.channel::<bool>(device, &format!("{tag}-output"), Direction::Out);
        let run = plant.field_input::<bool>(PointId(field), run_channel, false);
        let thermal = plant.field_input::<bool>(PointId(field + 1), thermal_channel, false);
        let moisture = plant.field_input::<bool>(PointId(field + 2), moisture_channel, false);
        let command = plant.field_output::<bool>(PointId(field + 3), command_channel);
        // Feedback simulation belongs to the plant, not to reusable equipment.
        plant.connect(run, command);
        let demand = plant.internal_input::<bool>(PointId(field + 4), false, true);
        demands.push(demand);
        let mut config = PumpConfig::new(tag, 500 + 100 * index, 1_000 + 100 * index);
        if index == 1 {
            config.fault_priority = 4;
            config.thermal_priority = 1;
            config.moisture_priority = 2;
        }
        pumps.push(
            pump(
                &mut plant,
                &config,
                PumpInputs {
                    run,
                    thermal,
                    moisture,
                    command,
                    automatic_request: demand.into(),
                    power_fail,
                    power_ok,
                    dry_run,
                    protective_measurement: measurement,
                    guard_anchor,
                    guard_true,
                    links: PumpLinks::default(),
                },
            )
            .unwrap(),
        );
    }

    let model = plant.build().unwrap();
    for (index, (equipment, pump)) in model.equipment.iter().zip(&pumps).enumerate() {
        assert!(equipment.components.contains(&pump.layout.motor));
        assert!(
            equipment
                .controls
                .iter()
                .any(|control| control.point == pump.mode.id())
        );
        let priorities = if index == 0 { [2, 2, 3] } else { [4, 1, 2] };
        for (alarm, priority) in [
            &pump.layout.fault_alarm,
            &pump.layout.thermal_alarm,
            &pump.layout.moisture_alarm,
        ]
        .into_iter()
        .zip(priorities)
        {
            let component = model
                .components
                .iter()
                .find(|component| component.id == alarm.component)
                .unwrap();
            assert_eq!(component.parameters["priority"], Value::Int(priority));
            // Configuring priority does not weaken the protection alarm policy.
            assert_eq!(component.parameters["max_shelve_ticks"], Value::Int(0));
            assert!(alarm.shelve.is_none());
        }
    }
    let driver = resolve_drivers(&model, &DriverRegistry::standard())
        .unwrap()
        .build()
        .unwrap();
    let mut executor = assemble(&model, &dcs_controller::registry(), &driver).unwrap();

    // Switching independent demand proves neither instance assumes station ids
    // or accidentally shares the other instance's control wiring.
    for expected in [[true, false], [false, true]] {
        for (demand, value) in demands.iter().zip(expected) {
            let receipt = executor.submit_command(Command::WriteValue {
                point: demand.id(),
                kind: ValueKind::Bool,
                value: Value::Bool(value),
            });
            assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
        }
        for _ in 0..30 {
            executor.scan();
            driver.step(1.0).unwrap();
        }
        for (pump, expected) in pumps.iter().zip(expected) {
            assert_eq!(
                executor.sample(pump.layout.cmd).unwrap().value,
                Value::Bool(expected)
            );
            assert_eq!(
                executor.sample(pump.layout.run).unwrap().value,
                Value::Bool(expected)
            );
            assert_eq!(
                executor.sample(pump.layout.fault).unwrap().value,
                Value::Bool(false)
            );
        }
    }
}

#[test]
fn invalid_pump_allocations_fail_before_mutating_the_composition() {
    let config = PumpConfig::new("transfer", 100, 1_000);
    let mut cases = Vec::new();
    let mut negative_holdout = config.clone();
    negative_holdout.min_off_ticks = -1;
    cases.push((negative_holdout, "min_off_ticks"));
    let mut negative_feedback = config.clone();
    negative_feedback.motor_fault_ticks = -1;
    cases.push((negative_feedback, "motor_fault_ticks"));
    let mut negative_fault_priority = config.clone();
    negative_fault_priority.fault_priority = -1;
    cases.push((negative_fault_priority, "fault_priority"));
    let mut negative_thermal_priority = config.clone();
    negative_thermal_priority.thermal_priority = -1;
    cases.push((negative_thermal_priority, "thermal_priority"));
    let mut negative_moisture_priority = config.clone();
    negative_moisture_priority.moisture_priority = -1;
    cases.push((negative_moisture_priority, "moisture_priority"));
    let mut control_overflow = config.clone();
    control_overflow.point_base = u64::MAX - 10;
    cases.push((control_overflow, "point_base"));
    let mut alarm_overflow = config.clone();
    alarm_overflow.alarm_base = u64::MAX - 10;
    cases.push((alarm_overflow, "alarm_base"));
    let mut signal_overflow = config;
    signal_overflow.signal_base = u64::MAX - 10;
    cases.push((signal_overflow, "signal_base"));

    for (config, expected_field) in cases {
        let mut plant = PlantBuilder::new();
        let inputs = PumpInputs {
            run: plant.internal_input::<bool>(PointId(1), false, false),
            thermal: plant.internal_input::<bool>(PointId(2), false, false),
            moisture: plant.internal_input::<bool>(PointId(3), false, false),
            command: plant.internal_output::<bool>(PointId(4), false),
            automatic_request: plant
                .internal_input::<bool>(PointId(5), false, false)
                .into(),
            power_fail: plant.internal_input::<bool>(PointId(6), false, false),
            power_ok: plant.internal_output::<bool>(PointId(7), true),
            dry_run: plant.internal_output::<bool>(PointId(8), false),
            protective_measurement: plant.internal_input::<f64>(PointId(9), 2.0, false),
            guard_anchor: plant.internal_input::<f64>(PointId(10), 0.0, false),
            guard_true: plant.internal_input::<bool>(PointId(11), true, false),
            links: PumpLinks::default(),
        };
        assert!(matches!(
            pump(&mut plant, &config, inputs),
            Err(BuildError::InvalidConfiguration { field, .. }) if field == expected_field
        ));
        let model = plant.build().unwrap();
        assert!(model.components.is_empty());
        assert!(model.signals.is_empty());
        assert!(model.equipment.is_empty());
    }
}
