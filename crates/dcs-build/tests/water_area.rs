//! One connected process, composed through the supported engineering seam.
use dcs_assembly::{DriverRegistry, FanoutDriver, assemble, resolve_drivers};
use dcs_build::water_area::{WaterArea, WaterAreaConfig, water_area};
use dcs_build::{PointId, Value};
use dcs_core::{Command, CommandError, CommandOutcome, Quality, QualityReason, ValueKind};
use dcs_sim::{Fault, ProcessElement};

fn driver(area: &WaterArea) -> FanoutDriver {
    let mut plan = resolve_drivers(&area.model, &DriverRegistry::standard()).unwrap();
    let elements: Vec<ProcessElement> =
        serde_json::from_value(serde_json::to_value(&area.dynamics).unwrap()).unwrap();
    for element in elements {
        plan.sim_map = plan.sim_map.with_element(element);
    }
    plan.sim_map.validate().unwrap();
    plan.build().unwrap()
}
fn command(executor: &mut dcs_runtime::Executor<'_>, point: PointId, value: Value) {
    let receipt = executor.submit_command(Command::WriteValue {
        point,
        kind: value.kind(),
        value,
    });
    assert!(
        matches!(receipt.outcome, CommandOutcome::Accepted { .. }),
        "{receipt:?}"
    );
}
fn number(executor: &dcs_runtime::Executor<'_>, point: u64) -> f64 {
    let Value::Float(value) = executor.sample(PointId(point)).unwrap().value else {
        panic!("numeric point")
    };
    value
}
fn scans(executor: &mut dcs_runtime::Executor<'_>, driver: &FanoutDriver, count: usize) {
    for _ in 0..count {
        executor.scan();
        driver.step(0.2).unwrap();
    }
}

#[test]
fn another_pump_instance_gets_controls_alarms_and_topology_without_browser_code() {
    for count in [2, 3] {
        let mut config = WaterAreaConfig::default();
        config.station.pumps = count;
        let area = water_area(&config).unwrap();
        assert!(area.model.validate().is_empty());
        assert_eq!(
            area.model
                .equipment
                .iter()
                .filter(|e| e.kind == "pump")
                .count(),
            count
        );
        assert_eq!(
            area.model.views[0]
                .nodes
                .iter()
                .filter(|n| n.symbol == dcs_build::PlantViewSymbol::Pump)
                .count(),
            count
        );
        let field = driver(&area);
        let mut executor = assemble(&area.model, &dcs_controller::registry(), &field).unwrap();
        scans(&mut executor, &field, 100);
        assert!(
            number(&executor, 20_001) > 0.0,
            "pumps deliver into the balance vessel"
        );
    }
}

#[test]
fn hydraulic_operator_workflow_and_receipts_are_bit_deterministic() {
    fn run() -> (Vec<String>, Vec<dcs_core::CommandReceipt>) {
        let area = water_area(&WaterAreaConfig::default()).unwrap();
        let field = driver(&area);
        let mut executor = assemble(&area.model, &dcs_controller::registry(), &field).unwrap();
        let mut outputs = Vec::new();
        let mut delivery_seen = false;
        let mut discharge_seen = false;
        for tick in 0..1400 {
            match tick {
                250 => command(
                    &mut executor,
                    area.level_loop.setpoint.id(),
                    Value::Float(2.8),
                ),
                350 => command(
                    &mut executor,
                    area.isolation.request.id(),
                    Value::Bool(false),
                ),
                450 => command(
                    &mut executor,
                    area.isolation.request.id(),
                    Value::Bool(true),
                ),
                650 => {
                    command(&mut executor, area.outlet.mode.id(), Value::Bool(true));
                    command(&mut executor, area.outlet.manual.id(), Value::Float(30.0));
                }
                850 => field
                    .sim()
                    .unwrap()
                    .inject_fault(
                        PointId(20_013),
                        Fault::Quality(Quality::Bad(QualityReason::DeviceFault)),
                    )
                    .unwrap(),
                950 => field.sim().unwrap().clear_fault(PointId(20_013)).unwrap(),
                1100 => command(&mut executor, area.outlet.mode.id(), Value::Bool(false)),
                _ => {}
            }
            executor.scan();
            field.step(0.2).unwrap();
            delivery_seen |= number(&executor, 20_001) > 0.0;
            discharge_seen |= number(&executor, 20_002) > 0.0;
            if tick == 420 {
                assert_eq!(number(&executor, 20_016), 0.0);
                assert!(
                    number(&executor, 20_002).abs() < 0.001,
                    "reported flow settles after physical shutoff"
                );
                assert_eq!(number(&executor, area.outlet.applied.id().0), 0.0);
            }
            if tick == 800 {
                assert!((number(&executor, area.outlet.applied.id().0) - 30.0).abs() < 1e-8);
            }
            if tick == 930 {
                assert_eq!(
                    executor.sample(area.outlet.fault.id()).unwrap().value,
                    Value::Bool(true)
                );
            }
            if tick == 1050 {
                assert_eq!(
                    executor.sample(area.outlet.fault.id()).unwrap().value,
                    Value::Bool(false)
                );
            }
            for point in [20_000, 20_008] {
                assert!((0.0..=5.0).contains(&number(&executor, point)));
            }
            outputs.push(
                serde_json::to_string(
                    &area
                        .model
                        .io_points
                        .iter()
                        .map(|point| executor.sample(point.id))
                        .collect::<Vec<_>>(),
                )
                .unwrap(),
            );
        }
        assert!(delivery_seen && discharge_seen);
        assert!(
            executor
                .receipts()
                .iter()
                .all(|r| matches!(r.outcome, CommandOutcome::Applied { .. }))
        );
        (outputs, executor.receipts().to_vec())
    }
    assert_eq!(run(), run());
}

#[test]
fn numeric_commands_refuse_outside_limits_and_limits_survive_checkpoint_restore() {
    let area = water_area(&WaterAreaConfig::default()).unwrap();
    let field = driver(&area);
    let mut executor = assemble(&area.model, &dcs_controller::registry(), &field).unwrap();
    for (point, value) in [
        (area.level_loop.setpoint.id(), 4.0),
        (area.outlet.manual.id(), -1.0),
        (area.outlet.manual.id(), 101.0),
    ] {
        for force in [false, true] {
            let cmd = if force {
                Command::ForcePoint {
                    point,
                    kind: ValueKind::Float,
                    value: Value::Float(value),
                }
            } else {
                Command::WriteValue {
                    point,
                    kind: ValueKind::Float,
                    value: Value::Float(value),
                }
            };
            let receipt = executor.submit_command(cmd);
            assert!(
                matches!(
                    receipt.outcome,
                    CommandOutcome::Rejected {
                        reason: CommandError::PointOutOfRange { .. },
                        ..
                    }
                ),
                "{receipt:?}"
            );
        }
    }
    command(
        &mut executor,
        area.level_loop.setpoint.id(),
        Value::Float(2.8),
    );
    scans(&mut executor, &field, 300);
    let checkpoint = executor.checkpoint();
    let restored_field = driver(&area);
    let mut restored = assemble(&area.model, &dcs_controller::registry(), &restored_field).unwrap();
    restored.apply(&checkpoint).unwrap();
    let rejected = restored.submit_command(Command::WriteValue {
        point: area.level_loop.setpoint.id(),
        kind: ValueKind::Float,
        value: Value::Float(4.0),
    });
    assert!(matches!(
        rejected.outcome,
        CommandOutcome::Rejected {
            reason: CommandError::PointOutOfRange { .. }
        }
    ));
    assert_eq!(
        restored.sample(area.level_loop.setpoint.id()),
        executor.sample(area.level_loop.setpoint.id())
    );
    for _ in 0..50 {
        executor.scan();
        field.step(0.2).unwrap();
        restored.scan();
        restored_field.step(0.2).unwrap();
        assert_eq!(
            restored.sample(area.outlet.applied.id()),
            executor.sample(area.outlet.applied.id())
        );
        assert_eq!(
            restored.sample(area.level.id()),
            executor.sample(area.level.id())
        );
    }
}

#[test]
fn sensor_loss_closes_protected_outlet_and_exposes_uncertain_process_data() {
    let area = water_area(&WaterAreaConfig::default()).unwrap();
    let field = driver(&area);
    let mut executor = assemble(&area.model, &dcs_controller::registry(), &field).unwrap();
    scans(&mut executor, &field, 200);
    field
        .sim()
        .unwrap()
        .inject_fault(
            area.level.id(),
            Fault::Quality(Quality::Uncertain(QualityReason::Stale)),
        )
        .unwrap();
    scans(&mut executor, &field, 30);
    assert!(
        number(&executor, 20_002) < 0.5,
        "safe applied output closes the physical discharge after its lag"
    );
    assert_eq!(
        executor.sample(area.outlet.applied.id()).unwrap().value,
        Value::Float(0.0)
    );
    assert_eq!(
        executor.sample(area.outlet.tripped.id()).unwrap().value,
        Value::Bool(true)
    );
    assert!(!executor.sample(PointId(22_000)).unwrap().quality.is_good());
    field.sim().unwrap().clear_fault(area.level.id()).unwrap();
    scans(&mut executor, &field, 100);
    assert_eq!(
        executor.sample(area.outlet.tripped.id()).unwrap().value,
        Value::Bool(false)
    );
}

/// The independently materialized customer uses only immutable release inputs.
/// Kept in the nested proof lane so ordinary workspace feedback stays bounded.
#[test]
fn clean_connected_area_consumer_uses_immutable_release_artifacts() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let status = std::process::Command::new("python3")
        .arg(root.join("scripts/connected_water_proof.py"))
        .arg("--cargo")
        .arg(env!("CARGO"))
        .current_dir(root)
        .status()
        .unwrap();
    assert!(
        status.success(),
        "immutable connected-area customer proof failed"
    );
}

#[test]
fn engineering_rejects_invalid_limits_display_and_overflowing_allocations() {
    use dcs_build::equipment::{EquipmentConfig, level_loop};
    let mut plant = dcs_build::PlantBuilder::new();
    let pv = plant.internal_input::<f64>(PointId(1), 2.0, false);
    assert!(
        level_loop(
            &mut plant,
            &EquipmentConfig::new("overflow", u64::MAX),
            pv,
            2.0,
            0.2
        )
        .is_err()
    );
    let area = water_area(&WaterAreaConfig::default()).unwrap();
    for range in [[3.0, 2.0], [f64::NAN, 5.0]] {
        let mut model = area.model.clone();
        let control = model
            .equipment
            .iter_mut()
            .find(|e| e.kind == "control-loop")
            .unwrap()
            .controls
            .first_mut()
            .unwrap();
        control.limits = Some(dcs_core::ParameterRange {
            min: Value::Float(range[0]),
            max: Value::Float(range[1]),
        });
        assert!(!model.validate().is_empty());
    }
    assert_eq!(area.model.version, dcs_model::CONTROL_LIMITS_MODEL_VERSION);
    let text = serde_json::to_string(&area.model).unwrap();
    assert!(dcs_model::PlantModel::load(&text).is_ok());
    let mut legacy = area.model.clone();
    legacy.version = dcs_model::MODEL_VERSION;
    assert!(!legacy.validate().is_empty());
    assert!(dcs_model::PlantModel::load(&serde_json::to_string(&legacy).unwrap()).is_err());
    let mut model = area.model.clone();
    model.views[0]
        .nodes
        .iter_mut()
        .find(|n| n.display.is_some())
        .unwrap()
        .display
        .as_mut()
        .unwrap()
        .normal = Some([-1.0, 6.0]);
    assert!(!model.validate().is_empty());
    let mut model = area.model.clone();
    model
        .equipment
        .iter_mut()
        .find(|e| e.kind == "control-loop")
        .unwrap()
        .controls[0]
        .role = Some(dcs_model::EquipmentControlRole::Mode);
    assert!(!model.validate().is_empty());
    let builder = dcs_build::PlantBuilder::from_model(area.model).unwrap();
    assert!(builder.input::<bool>(area.level.id()).is_err());
    assert!(builder.output::<f64>(area.level.id()).is_err());
}

#[test]
fn feedback_and_flow_sensor_faults_do_not_freeze_the_physical_hydraulics() {
    let area = water_area(&WaterAreaConfig::default()).unwrap();
    let field = driver(&area);
    let mut executor = assemble(&area.model, &dcs_controller::registry(), &field).unwrap();
    scans(&mut executor, &field, 200);
    command(&mut executor, area.outlet.mode.id(), Value::Bool(true));
    command(&mut executor, area.outlet.manual.id(), Value::Float(30.0));
    scans(&mut executor, &field, 80);
    for point in [area.station.pumps[0].run, PointId(20_002)] {
        field
            .sim()
            .unwrap()
            .inject_fault(
                point,
                Fault::Quality(Quality::Bad(QualityReason::DeviceFault)),
            )
            .unwrap();
    }
    let before = number(&executor, 20_000);
    scans(&mut executor, &field, 5);
    assert_ne!(number(&executor, 20_000), before);
    assert!(executor.sample(PointId(20_000)).unwrap().quality.is_good());
    assert!(executor.sample(PointId(20_005)).unwrap().quality.is_good());
    assert!(!executor.sample(PointId(20_002)).unwrap().quality.is_good());
}
