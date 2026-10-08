//! A small plant composed from the public pump equipment API.
//!
//! The control module has no device-specific logic. This example binds its
//! logical contacts and output to simulated channels; the explicit loopback
//! stands in for the motor's running contact in this demonstration.

use dcs_build::pump::{PumpConfig, PumpInputs, PumpLinks, pump};
use dcs_build::specs::{DigitalInputSpec, ManagedInputs, ManagedLatchingAlarmSpec};
use dcs_build::{
    ComponentId, Direction, InPoint, PlantBuilder, PlantView, PlantViewBinding, PlantViewNode,
    PlantViewPipe, PlantViewPort, PlantViewSymbol, PointId, SignalId, Value, parameters, unit,
};
use dcs_model::Rationalization;

// This example's process-warning policy is engineered here, independently of
// the reusable pump's protection contacts. Lower priority numbers rank first
// in this demo. The temperature warning has no connection to the command path.
const TEMPERATURE_UNIT: &str = "°C";
const BEARING_WARNING_LIMIT: f64 = 60.0;
const BEARING_WARNING_PRIORITY: i64 = 3;
const BEARING_WARNING_MAX_SHELVE_TICKS: i64 = 300;

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
    plant
        .signal(SignalId(4), "protective-measurement", measurement)
        .unit(unit::M)
        .description("Simulated protective measurement");
    let healthy_power = plant.add(DigitalInputSpec::new(parameters([(
        "invert",
        Value::Bool(true),
    )])));
    plant.connect(power_fail, &healthy_power.input);
    plant.connect(&healthy_power.out, power_ok);

    let mut warnings = Vec::new();
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
        config.thermal_priority = 1 + index as i64;
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

        let temperature = plant.internal_input::<f64>(PointId(base + 5), 20.0, true);
        plant.unit(temperature, TEMPERATURE_UNIT);
        plant
            .signal(
                SignalId(100 + base + 5),
                &format!("{tag}-bearing-temperature"),
                temperature,
            )
            .unit(TEMPERATURE_UNIT)
            .group(&config.group)
            .description("Simulator temperature input; physical plants bind this to field I/O");
        let (component, points) =
            bearing_warning(&mut plant, tag, temperature, 6000 + index as u64 * 10);
        warnings.push((tag, component, points));
    }

    // The operator schematic is configured by this plant's Rust composition.
    // Symbols bind to declared equipment and signals; geometry does not wire
    // control logic. The two independent test loops are shown explicitly.
    plant.view(PlantView {
        id: "overview".into(),
        label: "Pump test rig".into(),
        parent: None,
        nodes: vec![
            symbol(
                "feedback-1",
                PlantViewSymbol::Label,
                "Running contact",
                100,
                220,
                None,
            ),
            symbol(
                "pump-1",
                PlantViewSymbol::Pump,
                "Pump 1",
                520,
                220,
                Some(PlantViewBinding::Equipment("p101".into())),
            ),
            symbol(
                "output-1",
                PlantViewSymbol::Label,
                "Motor output",
                850,
                220,
                None,
            ),
            symbol(
                "feedback-2",
                PlantViewSymbol::Label,
                "Running contact",
                100,
                450,
                None,
            ),
            symbol(
                "pump-2",
                PlantViewSymbol::Pump,
                "Pump 2",
                520,
                450,
                Some(PlantViewBinding::Equipment("p102".into())),
            ),
            symbol(
                "output-2",
                PlantViewSymbol::Label,
                "Motor output",
                850,
                450,
                None,
            ),
            symbol(
                "protection-input",
                PlantViewSymbol::Measurement,
                "Protection input",
                505,
                65,
                Some(PlantViewBinding::Point(measurement.id())),
            ),
        ],
        pipes: vec![
            pipe("feedback-1", "pump-1"),
            pipe("pump-1", "output-1"),
            pipe("feedback-2", "pump-2"),
            pipe("pump-2", "output-2"),
        ],
    });
    for (index, tag) in ["p101", "p102"].into_iter().enumerate() {
        plant.view(PlantView {
            id: tag.into(),
            label: format!("Pump {}", index + 1),
            parent: Some("overview".into()),
            nodes: vec![
                symbol(
                    "pump",
                    PlantViewSymbol::Pump,
                    &format!("Pump {}", index + 1),
                    520,
                    300,
                    Some(PlantViewBinding::Equipment(tag.into())),
                ),
                symbol(
                    "bearing-temperature",
                    PlantViewSymbol::Measurement,
                    "Bearing temperature",
                    505,
                    140,
                    Some(PlantViewBinding::Point(PointId(15 + index as u64 * 10))),
                ),
            ],
            pipes: vec![],
        });
    }

    let mut model = plant
        .build()
        .expect("the pump demonstration model validates");
    // Consumer code can extend an equipment assembly with additional existing
    // blocks. Register ownership in the same model, then validate that final
    // document; the generic monitor discovers the warning through membership.
    for (tag, component, points) in warnings {
        let equipment = model
            .equipment
            .iter_mut()
            .find(|equipment| equipment.id == tag)
            .expect("the pump owns its additional process warning");
        equipment.components.push(component);
        equipment.points.extend(points);
    }
    let errors = model.validate();
    assert!(
        errors.is_empty(),
        "extended equipment model is invalid: {errors:?}"
    );
    println!("{}", serde_json::to_string_pretty(&model).unwrap());
}

fn bearing_warning(
    plant: &mut PlantBuilder,
    tag: &str,
    temperature: InPoint<f64>,
    base: u64,
) -> (ComponentId, Vec<PointId>) {
    let warning = plant.add(ManagedLatchingAlarmSpec::new(
        parameters([
            ("low_limit", Value::Float(-1.0e12)),
            ("high_limit", Value::Float(BEARING_WARNING_LIMIT)),
            ("hysteresis", Value::Float(2.0)),
            ("max_shelve_ticks", Value::Int(BEARING_WARNING_MAX_SHELVE_TICKS)),
            ("priority", Value::Int(BEARING_WARNING_PRIORITY)),
            ("class", Value::Int(2)),
            ("response_ticks", Value::Int(300)),
        ]),
        ManagedInputs {
            shelve: true,
            oos: true,
            ..ManagedInputs::default()
        },
        Rationalization {
            consequence: "Persistently elevated bearing temperature can damage the pump".into(),
            required_action: "Inspect bearing lubrication and cooling; stop the pump if the temperature continues to rise".into(),
            reference: format!("{tag}-bearing-temperature-alarm"),
        },
    ));
    plant.connect(temperature, &warning.input);
    plant.port_unit(warning.id, "in", TEMPERATURE_UNIT);
    for parameter in ["low_limit", "high_limit", "hysteresis"] {
        plant.param_unit(warning.id, parameter, TEMPERATURE_UNIT);
    }
    for parameter in ["max_shelve_ticks", "response_ticks"] {
        plant.param_unit(warning.id, parameter, unit::TICKS);
    }

    let group = format!("pump-{tag}");
    let prefix = format!("{tag}-bearing-temperature");
    let ack = plant.internal_input::<bool>(PointId(base), false, true);
    let shelve = plant.internal_input::<bool>(PointId(base + 1), false, true);
    let oos = plant.internal_input::<bool>(PointId(base + 2), false, true);
    plant.requires_reason(shelve);
    plant.connect(ack, &warning.ack);
    plant.connect(shelve, warning.managed.shelve.as_ref().unwrap());
    plant.connect(oos, warning.managed.oos.as_ref().unwrap());
    for (point, suffix, description) in [
        (ack, "ack", "Acknowledge the temperature warning"),
        (
            shelve,
            "shelve",
            "Bounded shelving request; an operator reason is required",
        ),
        (
            oos,
            "oos",
            "Place only the temperature warning out of service for maintenance",
        ),
    ] {
        plant.unit(point, unit::DIMENSIONLESS);
        plant
            .signal(
                SignalId(10_000 + point.id().0),
                &format!("{prefix}-{suffix}"),
                point,
            )
            .group(&group)
            .description(description);
    }
    let mut points = vec![temperature.id(), ack.id(), shelve.id(), oos.id()];
    for (offset, suffix, output) in [
        (3, "alarm", &warning.alarm),
        (4, "unacknowledged", &warning.unacknowledged),
        (5, "shelved", &warning.managed.shelved),
        (6, "suppressed", &warning.managed.suppressed),
        (7, "out-of-service", &warning.managed.out_of_service),
    ] {
        let point = plant.internal_output::<bool>(PointId(base + offset), false);
        plant.unit(point, unit::DIMENSIONLESS);
        plant.journaled(point);
        plant.connect(output, point);
        plant
            .signal(
                SignalId(10_000 + point.id().0),
                &format!("{prefix}-{suffix}"),
                point,
            )
            .group(&group)
            .description("Managed bearing-temperature warning lifecycle");
        points.push(point.id());
    }
    (warning.id, points)
}

fn symbol(
    id: &str,
    symbol: PlantViewSymbol,
    label: &str,
    x: u32,
    y: u32,
    binding: Option<PlantViewBinding>,
) -> PlantViewNode {
    PlantViewNode {
        id: id.into(),
        symbol,
        label: label.into(),
        x,
        y,
        binding,
    }
}

fn pipe(from: &str, to: &str) -> PlantViewPipe {
    // Ports and paths describe only the drawing. The loopbacks above are the
    // actual simulated field wiring.
    PlantViewPipe {
        from: dcs_build::PlantViewPipeEnd {
            node: from.into(),
            port: PlantViewPort::E,
        },
        to: dcs_build::PlantViewPipeEnd {
            node: to.into(),
            port: PlantViewPort::W,
        },
    }
}
