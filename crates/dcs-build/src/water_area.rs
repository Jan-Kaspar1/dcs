//! Connected extension of the existing duty/standby water station.
//! All field values, dynamics, equipment and operator geometry are engineered
//! here. See docs/milestones/connected-water.md for equations and site policy.

use crate::dynamics::{TankFlowConfig, TankFlowMode};
use crate::equipment::{self, ControlLoop, EquipmentConfig, ModulatingValve, OnOffValve};
use crate::station::{PumpStationConfig, PumpStationLayout, pumping_station};
use crate::{
    BuildError, Direction, DynamicsBuilder, DynamicsElement, InPoint, MeasurementDisplay,
    PlantBuilder, PlantView, PlantViewBinding, PlantViewNode, PlantViewPipe, PlantViewPipeEnd,
    PlantViewPort, PlantViewSymbol, PointId, SignalId,
};
use dcs_model::PlantModel;

/// Engineered process parameters; dimensions use metres, m3/h, seconds and percent.
#[derive(Debug, Clone)]
pub struct WaterAreaConfig {
    /// Existing station thresholds, allocation and protection policy.
    pub station: PumpStationConfig,
    /// Controller/simulator period in seconds.
    pub dt: f64,
    /// Incoming flow in m3/h.
    pub inflow: f64,
    /// Per-pump rated flow in m3/h.
    pub pump_flow: f64,
    /// Horizontal tank cross-section in m2, equal for both vessels.
    pub tank_area: f64,
}

impl Default for WaterAreaConfig {
    fn default() -> Self {
        let mut station = PumpStationConfig::reference();
        station.motor_fault_ticks = 15;
        Self {
            station,
            dt: 0.2,
            inflow: 36.0,
            pump_flow: 72.0,
            tank_area: 0.2,
        }
    }
}

/// One emitted area and its deterministic field simulation.
pub struct WaterArea {
    /// Generic runtime input; also the monitor's plant contract.
    pub model: PlantModel,
    /// Field process equations, in evaluation order.
    pub dynamics: Vec<DynamicsElement>,
    /// Existing station's identity map, retained without renumbering.
    pub station: PumpStationLayout,
    /// Measured receiving-tank level.
    pub level: InPoint<f64>,
    /// Physical storage independent of reported level faults.
    pub physical_balance_level: InPoint<f64>,
    /// Physical wet-well storage.
    pub physical_wet_level: InPoint<f64>,
    /// Conserved spill to the declared wet-well overflow route.
    pub wet_overflow: InPoint<f64>,
    /// Conserved spill to the declared balance-vessel overflow route.
    pub balance_overflow: InPoint<f64>,
    /// Standard balance-level measurement, including warning and delivery.
    pub balance_measurement: equipment::Measurement,
    /// Standard discharge measurement, including warning and delivery.
    pub flow_measurement: equipment::Measurement,
    /// Outlet isolation valve.
    pub isolation: OnOffValve,
    /// Protected modulating outlet.
    pub outlet: ModulatingValve,
    /// Automatic receiving-tank level loop.
    pub level_loop: ControlLoop,
}

fn field(
    plant: &mut PlantBuilder,
    device: crate::DeviceId,
    id: u64,
    name: &str,
    unit: &str,
) -> InPoint<f64> {
    let channel = plant.channel::<f64>(device, name, Direction::In);
    let point = plant.field_input_stale_after::<f64>(PointId(id), channel, false, 5);
    plant.unit(point, unit);
    plant
        .signal(SignalId(id), name, point)
        .group("water-area")
        .description(name);
    point
}

fn node(
    id: &str,
    symbol: PlantViewSymbol,
    label: &str,
    x: u32,
    y: u32,
    binding: Option<PlantViewBinding>,
) -> PlantViewNode {
    let mut node = PlantViewNode::new(id, symbol, label, x, y);
    node.binding = binding;
    node
}

fn reading(
    id: &str,
    label: &str,
    x: u32,
    y: u32,
    point: PointId,
    [min, max]: [f64; 2],
    normal: Option<[f64; 2]>,
) -> PlantViewNode {
    let mut node = node(
        id,
        PlantViewSymbol::Measurement,
        label,
        x,
        y,
        Some(PlantViewBinding::Point(point)),
    );
    node.display = Some(MeasurementDisplay { min, max, normal });
    node
}

fn pipe(from: &str, a: PlantViewPort, to: &str, b: PlantViewPort) -> PlantViewPipe {
    PlantViewPipe {
        from: PlantViewPipeEnd {
            node: from.into(),
            port: a,
        },
        to: PlantViewPipeEnd {
            node: to.into(),
            port: b,
        },
    }
}

/// Extends the station through the public typed composition API.
/// The independent customer example calls this same API. Another pump is added
/// by changing `config.station.pumps`, which creates its standard equipment and
/// drawing automatically; no customer browser code is required.
pub fn water_area(config: &WaterAreaConfig) -> Result<WaterArea, BuildError> {
    for (field, value) in [
        ("dt", config.dt),
        ("pump_flow", config.pump_flow),
        ("tank_area", config.tank_area),
    ] {
        if !value.is_finite() || value <= 0.0 {
            return Err(BuildError::InvalidConfiguration {
                field: field.into(),
                reason: "must be finite and positive".into(),
            });
        }
    }
    if !config.inflow.is_finite() || config.inflow < 0.0 || !(2..=3).contains(&config.station.pumps)
    {
        return Err(BuildError::InvalidConfiguration {
            field: "inflow/pumps".into(),
            reason:
                "inflow must be finite and nonnegative; this drawing supports two or three pumps"
                    .into(),
        });
    }
    let mut station = pumping_station(&config.station)?;
    let layout = station.layout;
    // Upgrade presentation metadata using the station's typed identities,
    // retaining its historical emitter and external artifact bytes.
    for equipment in &mut station.model.equipment {
        for control in &mut equipment.controls {
            for pump in &layout.pumps {
                if control.point == pump.mode {
                    control.role = Some(dcs_model::EquipmentControlRole::Mode);
                } else if control.point == pump.hand {
                    control.role = Some(dcs_model::EquipmentControlRole::Request);
                } else if control.point == pump.out_of_service {
                    control.role = Some(dcs_model::EquipmentControlRole::OutOfService);
                }
            }
        }
    }
    let pump_members: std::collections::BTreeSet<_> = station
        .model
        .equipment
        .iter()
        .flat_map(|e| e.components.iter().copied())
        .collect();
    let station_members = station
        .model
        .components
        .iter()
        .filter(|c| !pump_members.contains(&c.id))
        .map(|c| c.id)
        .collect();
    let mut plant = PlantBuilder::from_model(station.model)?;
    let mut station_points = vec![
        layout.level_selected,
        layout.level_primary,
        layout.level_backup,
        layout.demand,
        layout.duty,
        layout.staged,
        layout.backup_active,
        layout.backup_unhealthy,
        layout.none_available,
        layout.all_faulted,
        layout.power_tripped,
    ];
    let mut station_controls = Vec::new();
    for alarm in [
        &layout.high_level_alarm,
        &layout.low_level_alarm,
        &layout.backup_active_alarm,
        &layout.backup_unhealthy_alarm,
        &layout.none_available_alarm,
        &layout.all_faulted_alarm,
        &layout.power_fail_alarm,
    ] {
        station_points.extend([alarm.ack, alarm.alarm, alarm.unacknowledged]);
        station_controls.push(dcs_model::EquipmentControl {
            point: alarm.ack,
            label: format!("Acknowledge alarm {}", alarm.component.0),
            role: Some(dcs_model::EquipmentControlRole::Acknowledge),
            limits: None,
            false_label: Some("Release acknowledgment".into()),
            true_label: Some("Acknowledge".into()),
        });
    }
    plant.equipment(dcs_model::Equipment {
        id: "station".into(),
        label: "Duty/standby transfer station".into(),
        kind: "pumping-station".into(),
        components: station_members,
        points: station_points,
        controls: station_controls,
    });
    let sim = plant.device("sim-water-area").id;
    // Sim devices are recognized by their sim prefix, using the standard registry.
    let level = field(&mut plant, sim, 20_000, "balance-level", "m");
    let inlet = field(&mut plant, sim, 20_001, "balance-inflow", "m3/h");
    let outlet_flow = field(&mut plant, sim, 20_002, "outlet-flow", "m3/h");
    let draw = field(&mut plant, sim, 20_003, "balance-draw", "m3/h");
    let net = field(&mut plant, sim, 20_004, "balance-net", "m3/h");
    let level_rate = field(&mut plant, sim, 20_005, "balance-level-rate", "m/s");
    let wet_rate = field(&mut plant, sim, 20_006, "wet-level-rate", "m/s");
    let combined_draw = field(&mut plant, sim, 20_007, "pump-draw-total", "m3/h");
    let wet_actual = field(&mut plant, sim, 20_008, "wet-actual-level", "m");
    let healthy = plant.internal_input::<bool>(PointId(20_010), true, false);
    plant.unit(healthy, "");
    let isolation_fb_ch = plant.channel::<bool>(sim, "xv201-open", Direction::In);
    let isolation_fb =
        plant.field_input_stale_after::<bool>(PointId(20_011), isolation_fb_ch, false, 5);
    let isolation_cmd_ch = plant.channel::<bool>(sim, "xv201-output", Direction::Out);
    let isolation_cmd = plant.field_output::<bool>(PointId(20_012), isolation_cmd_ch);
    let isolation = equipment::on_off_valve(
        &mut plant,
        &EquipmentConfig {
            label: "XV-201 · Outlet isolation".into(),
            ..EquipmentConfig::new("xv201", 21_000)
        },
        isolation_fb,
        isolation_cmd,
        healthy,
        15,
    )?;
    plant.connect(isolation_fb, isolation_cmd); // simulation-only motorized end contact
    let instrument = equipment::measurement(
        &mut plant,
        &EquipmentConfig {
            label: "LT-201 · Balance level".into(),
            ..EquipmentConfig::new("lt201", 22_000)
        },
        level,
        "m",
        [0.0, 5.0],
        [0.4, 4.2],
    )?;
    let level_loop = equipment::level_loop(
        &mut plant,
        &EquipmentConfig {
            label: "LIC-201 · Balance level".into(),
            ..EquipmentConfig::new("lic201", 23_000)
        },
        instrument.delivered,
        2.0,
        config.dt,
    )?;
    let position = field(&mut plant, sim, 20_013, "lv201-actual-position", "%");
    let actual_position = field(&mut plant, sim, 20_015, "lv201-physical-position", "%");
    let command_ch = plant.channel::<f64>(sim, "lv201-command", Direction::Out);
    let command = plant.field_output::<f64>(PointId(20_014), command_ch);
    plant.unit(command, "%");
    let outlet = equipment::modulating_valve(
        &mut plant,
        &EquipmentConfig {
            label: "LV-201 · Discharge control".into(),
            ..EquipmentConfig::new("lv201", 24_000)
        },
        level_loop.demand.into(),
        position,
        command,
        isolation_fb,
        equipment::ModulatingValveConfig {
            transfer_delta: 5.0,
            tolerance: 8.0,
            // Full 100% transfer takes 4 s; 1 s actuator settling, sensor
            // lag and scan delivery fit within this 12 s diagnostic budget.
            discrepancy_ticks: (12.0 / config.dt).ceil() as u64,
        },
    )?;

    let flow_measurement = equipment::measurement(
        &mut plant,
        &EquipmentConfig {
            label: "FT-201 · Discharge flow".into(),
            ..EquipmentConfig::new("ft201", 25_000)
        },
        outlet_flow,
        "m3/h",
        [0.0, 144.0],
        [0.0, 130.0],
    )?;

    // One-second training history cadence, declared in the model rather than UI.
    for point in [
        layout.level_selected,
        instrument.value.id(),
        level_loop.setpoint.id(),
        outlet.requested.id(),
        outlet.applied.id(),
        position.id(),
        flow_measurement.value.id(),
    ] {
        plant.record(point, 5);
    }
    // Physical flow carriers are distinct from reported flow sensors.
    // Sensor quality faults affect control only through actual control wiring.
    let actual_outlet = field(&mut plant, sim, 20_016, "physical-outlet-flow", "m3/h");
    let actual_inlet = field(&mut plant, sim, 20_017, "physical-balance-inflow", "m3/h");
    let actual_influent = field(&mut plant, sim, 20_018, "physical-influent", "m3/h");
    let balance_actual = field(&mut plant, sim, 20_019, "physical-balance-level", "m");
    let wet_overflow = field(&mut plant, sim, 20_020, "wet-overflow", "m3/h");
    let balance_overflow = field(&mut plant, sim, 20_021, "balance-overflow", "m3/h");
    let outlet_capacity = field(&mut plant, sim, 20_022, "outlet-capacity", "m3/h");
    let wet_spill_draw = field(&mut plant, sim, 20_023, "wet-overflow-draw", "m3/h");
    let balance_spill_draw = field(&mut plant, sim, 20_024, "balance-overflow-draw", "m3/h");
    for point in [wet_overflow.id(), balance_overflow.id()] {
        plant.record(point, 5);
    }
    let tank_flow = |mode| TankFlowConfig {
        mode,
        bounds: [0.0, 5.0],
        flow_per_level: 3600.0 * config.tank_area,
        initial: 0.0,
    };
    let mut dynamics = DynamicsBuilder::new();
    let inflow = plant.input::<f64>(layout.inflow)?;
    let net_wet = plant.input::<f64>(layout.net_flow)?;
    let primary = plant.input::<f64>(layout.level_primary)?;
    let backup = plant.input::<f64>(layout.level_backup)?;
    dynamics.flow_sum(
        std::iter::empty::<InPoint<f64>>(),
        actual_influent,
        config.inflow,
        config.inflow,
    );
    dynamics.first_order_lag(actual_influent, inflow, 0.2, config.inflow);
    let mut pump_draws = Vec::new();
    for (index, pump) in layout.pumps.iter().enumerate() {
        let applied = plant.output::<bool>(pump.cmd)?;
        let flow = plant.input::<f64>(pump.draw)?;
        let request = field(
            &mut plant,
            sim,
            20_100 + index as u64,
            &format!("pump-{}-capacity", index + 1),
            "m3/h",
        );
        let actual = field(
            &mut plant,
            sim,
            20_120 + index as u64,
            &format!("pump-{}-physical-flow", index + 1),
            "m3/h",
        );
        // Sequential allocation conserves the shared pre-step inventory even
        // if several pumps run or one field step spans the dry-run boundary.
        let remaining_inlet = if pump_draws.is_empty() {
            actual_influent
        } else {
            let residual = field(
                &mut plant,
                sim,
                20_110 + index as u64,
                &format!("pump-{}-remaining-supply", index + 1),
                "m3/h",
            );
            dynamics.flow_sum(
                pump_draws.iter().copied().chain([actual_influent]),
                residual,
                0.0,
                config.inflow,
            );
            residual
        };
        dynamics
            .bool_flow(applied, request, config.pump_flow, 0.0, 0.0)
            .tank_flow(
                wet_actual,
                remaining_inlet,
                request,
                actual,
                tank_flow(TankFlowMode::Outlet),
            )
            .scaled_flow(actual, flow, -1.0, 0.0);
        pump_draws.push(flow);
    }
    let gain = 1.0 / (3600.0 * config.tank_area);
    dynamics
        .flow_sum(pump_draws.clone(), combined_draw, 0.0, 0.0)
        .scaled_flow(combined_draw, actual_inlet, -1.0, 0.0)
        .tank_flow(
            wet_actual,
            actual_influent,
            actual_inlet,
            wet_overflow,
            tank_flow(TankFlowMode::Overflow),
        )
        .scaled_flow(wet_overflow, wet_spill_draw, -1.0, 0.0)
        .flow_sum(
            pump_draws
                .into_iter()
                .chain([actual_influent, wet_spill_draw]),
            net_wet,
            0.0,
            config.inflow,
        )
        .scaled_flow(net_wet, wet_rate, gain, 0.0)
        .bounded_integrator(wet_rate, wet_actual, 2.5, 0.0, 5.0)
        .first_order_lag(wet_actual, primary, 0.2, 2.5)
        .first_order_lag(wet_actual, backup, 0.4, 2.5)
        .first_order_lag(actual_inlet, inlet, 0.2, 0.0)
        .first_order_lag(command, actual_position, 1.0, 0.0)
        .first_order_lag(actual_position, position, 0.2, 0.0)
        .gated_flow(actual_position, isolation_cmd, outlet_capacity, 1.44, 0.0)
        .tank_flow(
            balance_actual,
            actual_inlet,
            outlet_capacity,
            actual_outlet,
            tank_flow(TankFlowMode::Outlet),
        )
        .tank_flow(
            balance_actual,
            actual_inlet,
            actual_outlet,
            balance_overflow,
            tank_flow(TankFlowMode::Overflow),
        )
        .first_order_lag(actual_outlet, outlet_flow, 0.2, 0.0)
        .scaled_flow(actual_outlet, draw, -1.0, 0.0)
        .scaled_flow(balance_overflow, balance_spill_draw, -1.0, 0.0)
        .flow_sum([actual_inlet, draw, balance_spill_draw], net, 0.0, 0.0)
        .scaled_flow(net, level_rate, gain, 0.0)
        .bounded_integrator(level_rate, balance_actual, 2.0, 0.0, 5.0)
        .first_order_lag(balance_actual, level, 0.2, 2.0);
    let mut view = PlantView::new("water-area", "Water station · balance and discharge");
    view.nodes = vec![
        node(
            "inlet",
            PlantViewSymbol::Label,
            &format!("Influent · {} m3/h", config.inflow),
            10,
            220,
            None,
        ),
        node(
            "wet-well",
            PlantViewSymbol::Tank,
            "TK-101 · Wet well",
            170,
            280,
            Some(PlantViewBinding::Equipment("station".into())),
        ),
        reading(
            "wet-level",
            "LT-101 · Wet well",
            150,
            110,
            layout.level_selected,
            [0.0, 5.0],
            Some([1.0, 3.0]),
        ),
        node(
            "balance",
            PlantViewSymbol::Tank,
            "TK-201 · Balance",
            620,
            280,
            Some(PlantViewBinding::Equipment("lt201".into())),
        ),
        reading(
            "balance-reading",
            "LT-201 · Balance",
            600,
            110,
            instrument.value.id(),
            [0.0, 5.0],
            Some([0.8, 3.8]),
        ),
        node(
            "isolation",
            PlantViewSymbol::Valve,
            "XV-201",
            830,
            260,
            Some(PlantViewBinding::Equipment("xv201".into())),
        ),
        node(
            "modulating",
            PlantViewSymbol::Valve,
            "LV-201",
            830,
            440,
            Some(PlantViewBinding::Equipment("lv201".into())),
        ),
        reading(
            "discharge",
            "FT-201 · Discharge",
            820,
            590,
            flow_measurement.value.id(),
            [0.0, 144.0],
            Some([0.0, 100.0]),
        ),
        reading(
            "setpoint",
            "LIC-201 · Setpoint",
            1000,
            110,
            level_loop.setpoint.id(),
            [0.0, 5.0],
            Some([0.8, 3.8]),
        ),
        reading(
            "requested-opening",
            "LV-201 · Requested",
            1000,
            330,
            outlet.requested.id(),
            [0.0, 100.0],
            None,
        ),
        reading(
            "applied-opening",
            "LV-201 · Applied",
            1000,
            440,
            outlet.applied.id(),
            [0.0, 100.0],
            None,
        ),
        reading(
            "actual-opening",
            "LV-201 · Feedback",
            1000,
            550,
            position.id(),
            [0.0, 100.0],
            None,
        ),
        reading(
            "wet-spill",
            "TK-101 · Overflow",
            70,
            540,
            wet_overflow.id(),
            [0.0, config.inflow.max(1.0)],
            Some([0.0, 0.0]),
        ),
        reading(
            "balance-spill",
            "TK-201 · Overflow",
            600,
            630,
            balance_overflow.id(),
            [0.0, config.pump_flow * config.station.pumps as f64],
            Some([0.0, 0.0]),
        ),
        node(
            "wet-spill-route",
            PlantViewSymbol::Label,
            "Wet-well overflow route",
            20,
            700,
            None,
        ),
        node(
            "balance-spill-route",
            PlantViewSymbol::Label,
            "Balance overflow route",
            560,
            710,
            None,
        ),
        node(
            "downstream",
            PlantViewSymbol::Label,
            "To receiving water",
            870,
            710,
            None,
        ),
    ];
    use PlantViewPort::{E, N, S, W};
    view.pipes = vec![
        pipe("inlet", E, "wet-well", W),
        pipe("balance", E, "isolation", W),
        pipe("isolation", S, "modulating", N),
        pipe("modulating", S, "discharge", N),
        pipe("discharge", S, "downstream", N),
        pipe("wet-well", S, "wet-spill", N),
        pipe("wet-spill", S, "wet-spill-route", N),
        pipe("balance", S, "balance-spill", N),
        pipe("balance-spill", S, "balance-spill-route", N),
    ];
    for (index, _) in layout.pumps.iter().enumerate() {
        let id = format!("pump-{}", index + 1);
        let tag = format!("p{}", 101 + index);
        let y = if layout.pumps.len() == 2 {
            240 + index as u32 * 240
        } else {
            160 + index as u32 * 180
        };
        view.nodes.push(node(
            &id,
            PlantViewSymbol::Pump,
            &format!("P-{}", 101 + index),
            400,
            y,
            Some(PlantViewBinding::Equipment(tag)),
        ));
        view.pipes
            .extend([pipe("wet-well", E, &id, W), pipe(&id, E, "balance", W)]);
    }
    for node in &view.nodes {
        if let (Some(PlantViewBinding::Point(point)), Some(display)) = (&node.binding, node.display)
        {
            plant.display(*point, display);
        }
    }
    for point in [layout.level_primary, layout.level_backup] {
        plant.display(
            point,
            MeasurementDisplay {
                min: 0.0,
                max: 5.0,
                normal: Some([1.0, 3.0]),
            },
        );
    }
    plant.display(
        layout.inflow,
        MeasurementDisplay {
            min: 0.0,
            max: config.inflow.max(1.0),
            normal: None,
        },
    );
    plant.display(
        layout.net_flow,
        MeasurementDisplay {
            min: -config.pump_flow * config.station.pumps as f64,
            max: config.inflow.max(1.0),
            normal: None,
        },
    );
    plant.view(view);
    let mut model = plant.build()?;
    for point in [layout.level_primary, layout.level_backup] {
        model
            .io_points
            .iter_mut()
            .find(|p| p.id == point)
            .unwrap()
            .stale_after_ticks = Some(5);
    }
    model.views[0]
        .nodes
        .iter_mut()
        .find(|n| n.id == "inlet")
        .unwrap()
        .label = format!("Influent · {} m3/h", config.inflow);
    let dynamics = dynamics
        .emit(&model)
        .map_err(|error| BuildError::InvalidConfiguration {
            field: "water dynamics".into(),
            reason: error.to_string(),
        })?;
    Ok(WaterArea {
        model,
        dynamics,
        station: layout,
        level,
        physical_balance_level: balance_actual,
        physical_wet_level: wet_actual,
        wet_overflow,
        balance_overflow,
        balance_measurement: instrument,
        flow_measurement,
        isolation,
        outlet,
        level_loop,
    })
}
