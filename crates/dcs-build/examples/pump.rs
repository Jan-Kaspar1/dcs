//! A small plant composed from the public pump equipment API.
//!
//! The control module has no device-specific logic. This example binds its
//! logical contacts and output to simulated channels; the explicit loopback
//! stands in for the motor's running contact in this demonstration.

use dcs_build::pump::{PumpConfig, PumpInputs, PumpLinks, pump};
use dcs_build::specs::DigitalInputSpec;
use dcs_build::{Direction, PlantBuilder, PointId, SignalId, Value, parameters, unit};

fn main() {
    let mut plant = PlantBuilder::new();
    let di = plant.device("sim-di").id;
    let d_o = plant.device("sim-do").id;

    let power_ch = plant.channel::<bool>(di, "power-fail", Direction::In);
    let power_fail = plant.field_input::<bool>(PointId(1), power_ch, false);
    let power_ok = plant.internal_output::<bool>(PointId(2), true);
    let dry_run = plant.internal_output::<bool>(PointId(3), false);
    let measurement = plant.internal_input::<f64>(PointId(4), 2.0, false);
    let anchor = plant.internal_input::<f64>(PointId(5), 0.0, false);
    let trusted = plant.internal_input::<bool>(PointId(6), true, false);
    plant.unit(measurement, unit::M);
    let healthy_power = plant.add(DigitalInputSpec::new(parameters([(
        "invert",
        Value::Bool(true),
    )])));
    plant.connect(power_fail, &healthy_power.input);
    plant.connect(&healthy_power.out, power_ok);

    for (index, tag) in ["p101", "p102"].into_iter().enumerate() {
        let base = 10 + index as u64 * 10;
        let run_ch = plant.channel::<bool>(di, &format!("{tag}-run"), Direction::In);
        let thermal_ch = plant.channel::<bool>(di, &format!("{tag}-thermal"), Direction::In);
        let moisture_ch = plant.channel::<bool>(di, &format!("{tag}-moisture"), Direction::In);
        let command_ch = plant.channel::<bool>(d_o, &format!("{tag}-cmd"), Direction::Out);
        let run = plant.field_input::<bool>(PointId(base), run_ch, false);
        let thermal = plant.field_input::<bool>(PointId(base + 1), thermal_ch, false);
        let moisture = plant.field_input::<bool>(PointId(base + 2), moisture_ch, false);
        let command = plant.field_output::<bool>(PointId(base + 3), command_ch);
        let automatic = plant.internal_input::<bool>(PointId(base + 4), false, true);
        plant
            .signal(
                SignalId(100 + base),
                &format!("{tag}-automatic-request"),
                automatic,
            )
            .group(&format!("pump-{tag}"))
            .description("Supervisory demand; the pump still applies modes and protections");

        let mut config = PumpConfig::new(tag, 1000 + index as u64 * 32, 5000 + index as u64 * 30);
        config.label = format!("Pump {}", index + 1);
        config.min_off_ticks = 5;
        config.motor_fault_ticks = 10;
        pump(
            &mut plant,
            &config,
            PumpInputs {
                run,
                thermal,
                moisture,
                power_fail,
                command,
                automatic_request: automatic.into(),
                power_ok,
                dry_run,
                protective_measurement: measurement,
                guard_anchor: anchor,
                guard_true: trusted,
                links: PumpLinks::default(),
            },
        )
        .expect("pump equipment composes");

        // Simulation-only feedback. Physical integrations supply their
        // independent running contact instead of this field loopback.
        plant.connect(run, command);
    }

    let model = plant
        .build()
        .expect("the pump demonstration model validates");
    println!("{}", serde_json::to_string_pretty(&model).unwrap());
}
