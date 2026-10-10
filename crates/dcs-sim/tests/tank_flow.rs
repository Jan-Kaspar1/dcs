//! Inventory limits are physical accounting, independent of control policy.
use dcs_core::{IoDriver, PointId, Quality, QualityReason, Value};
use dcs_sim::{
    BoundedIntegrator, ChannelId, ChannelMap, Direction, Fault, FlowSum, PointBinding,
    ProcessElement, ScaledFlow, SimDriver, TankFlow, TankFlowMode,
};

fn binding(id: u64, value: f64) -> PointBinding {
    PointBinding {
        point: PointId(id),
        channel: ChannelId {
            device: 1,
            name: format!("p{id}"),
        },
        direction: Direction::In,
        initial: Value::Float(value),
    }
}
fn flow(mode: TankFlowMode, output: u64) -> TankFlow {
    TankFlow {
        level: PointId(1),
        inflow: PointId(2),
        outlet: PointId(3),
        output: PointId(output),
        mode,
        min: 0.0,
        max: 5.0,
        flow_per_level: 3.0,
        initial: 0.0,
    }
}
fn rig(level: f64, inflow: f64, request: f64, mode: TankFlowMode) -> SimDriver {
    SimDriver::new(
        ChannelMap::new()
            .with_point(binding(1, level))
            .with_point(binding(2, inflow))
            .with_point(binding(3, request))
            .with_point(binding(4, 0.0))
            .with_element(ProcessElement::TankFlow(flow(mode, 4))),
    )
    .unwrap()
}
fn number(sim: &SimDriver, point: u64) -> f64 {
    let Value::Float(value) = sim.read(PointId(point)).unwrap().value else {
        panic!("Float point")
    };
    value
}

#[test]
fn empty_storage_cannot_supply_outlet_and_full_storage_reports_spill() {
    let empty = rig(0.0, 2.0, 100.0, TankFlowMode::Outlet);
    empty.step(0.2);
    assert_eq!(number(&empty, 4), 2.0);
    let finite = rig(1.0, 2.0, 100.0, TankFlowMode::Outlet);
    finite.step(0.5);
    assert_eq!(number(&finite, 4), 8.0);
    let full = rig(5.0, 20.0, 3.0, TankFlowMode::Overflow);
    full.step(0.5);
    assert_eq!(number(&full, 4), 17.0);
    let filling = rig(4.0, 20.0, 3.0, TankFlowMode::Overflow);
    filling.step(0.5);
    assert_eq!(number(&filling, 4), 11.0);
}

#[test]
fn signed_net_inlet_allocates_shared_inventory_without_double_withdrawal() {
    let first = rig(1.0, 2.0, 5.0, TankFlowMode::Outlet);
    first.step(0.5);
    let taken = number(&first, 4);
    assert_eq!(taken, 5.0);
    let next = rig(1.0, 2.0 - taken, 100.0, TankFlowMode::Outlet);
    next.step(0.5);
    assert_eq!(number(&next, 4), 3.0);
    assert!(next.read(PointId(4)).unwrap().quality.is_good());
    assert_eq!(taken + number(&next, 4), 2.0 + 1.0 * 3.0 / 0.5);
    for dt in [0.0, 0.5] {
        let empty = rig(0.0, -2.0, 100.0, TankFlowMode::Outlet);
        empty.step(dt);
        assert_eq!(number(&empty, 4), 0.0);
        assert!(empty.read(PointId(4)).unwrap().quality.is_good());
    }
}

#[test]
fn zero_duration_exposes_instantaneous_rates_without_dividing_by_zero() {
    for (level, mode, expected) in [
        (0.0, TankFlowMode::Outlet, 2.0),
        (1.0, TankFlowMode::Outlet, 100.0),
        (5.0, TankFlowMode::Overflow, 0.0),
    ] {
        let sim = rig(level, 2.0, 100.0, mode);
        sim.step(0.0);
        assert_eq!(number(&sim, 4), expected);
        assert!(sim.read(PointId(4)).unwrap().quality.is_good());
    }
    let full = rig(5.0, 20.0, 3.0, TankFlowMode::Overflow);
    full.step(0.0);
    assert_eq!(number(&full, 4), 17.0);
    let not_full = rig(4.0, 20.0, 3.0, TankFlowMode::Overflow);
    not_full.step(0.0);
    assert_eq!(number(&not_full, 4), 0.0);
}

#[test]
fn level_uses_prior_inventory_and_flows_use_current_declared_order() {
    for level_first in [false, true] {
        let mut map = ChannelMap::new();
        for (id, initial) in [(1, 2.0), (2, 0.0), (3, 100.0), (4, 0.0), (5, 0.0)] {
            map = map.with_point(binding(id, initial));
        }
        let update = ProcessElement::BoundedIntegrator(BoundedIntegrator {
            input: PointId(5),
            output: PointId(1),
            initial: 2.0,
            min: 0.0,
            max: 5.0,
        });
        map = map.with_element(ProcessElement::FlowSum(FlowSum {
            inputs: vec![],
            output: PointId(2),
            bias: 4.0,
            initial: 0.0,
        }));
        if level_first {
            map = map.with_element(update.clone());
        }
        map = map.with_element(ProcessElement::TankFlow(flow(TankFlowMode::Outlet, 4)));
        if !level_first {
            map = map.with_element(update);
        }
        let sim = SimDriver::new(map).unwrap();
        sim.write(PointId(5), Value::Float(-2.0)).unwrap();
        sim.step(0.5);
        assert_eq!(number(&sim, 1), 1.0);
        assert_eq!(
            number(&sim, 4),
            16.0,
            "budget uses initial level2 and same-step inflow4"
        );
    }
}

#[test]
fn degraded_invalid_and_overflowing_inputs_hold_finite_rates_then_recover() {
    let sim = rig(1.0, 2.0, 100.0, TankFlowMode::Outlet);
    sim.step(0.5);
    assert_eq!(number(&sim, 4), 8.0);
    sim.inject_fault(
        PointId(2),
        Fault::Quality(Quality::Uncertain(QualityReason::Stale)),
    )
    .unwrap();
    sim.step(0.5);
    assert_eq!(number(&sim, 4), 8.0);
    assert_eq!(
        sim.read(PointId(4)).unwrap().quality,
        Quality::Uncertain(QualityReason::Stale)
    );
    sim.clear_fault(PointId(2)).unwrap();
    for (point, invalid, restored) in [(3, -1.0, 100.0), (1, 6.0, 1.0)] {
        sim.write(PointId(point), Value::Float(invalid)).unwrap();
        sim.step(0.5);
        assert_eq!(number(&sim, 4), 8.0);
        assert_eq!(
            sim.read(PointId(4)).unwrap().quality,
            Quality::Bad(QualityReason::OutOfRange)
        );
        sim.write(PointId(point), Value::Float(restored)).unwrap();
    }
    sim.step(f64::MIN_POSITIVE);
    assert_eq!(number(&sim, 4), 8.0);
    assert_eq!(
        sim.read(PointId(4)).unwrap().quality,
        Quality::Bad(QualityReason::OutOfRange)
    );
    sim.step(0.5);
    assert!(sim.read(PointId(4)).unwrap().quality.is_good());
}

fn storage_rig() -> SimDriver {
    let mut map = ChannelMap::new();
    for id in 1..=9 {
        map = map.with_point(binding(id, 0.0));
    }
    let mut spill = flow(TankFlowMode::Overflow, 5);
    spill.outlet = PointId(4);
    map = map
        .with_element(ProcessElement::TankFlow(flow(TankFlowMode::Outlet, 4)))
        .with_element(ProcessElement::TankFlow(spill))
        .with_element(ProcessElement::ScaledFlow(ScaledFlow {
            input: PointId(4),
            output: PointId(6),
            gain: -1.0,
            initial: 0.0,
        }))
        .with_element(ProcessElement::ScaledFlow(ScaledFlow {
            input: PointId(5),
            output: PointId(7),
            gain: -1.0,
            initial: 0.0,
        }))
        .with_element(ProcessElement::FlowSum(FlowSum {
            inputs: vec![PointId(2), PointId(6), PointId(7)],
            output: PointId(8),
            bias: 0.0,
            initial: 0.0,
        }))
        .with_element(ProcessElement::ScaledFlow(ScaledFlow {
            input: PointId(8),
            output: PointId(9),
            gain: 1.0 / 3.0,
            initial: 0.0,
        }))
        .with_element(ProcessElement::BoundedIntegrator(BoundedIntegrator {
            input: PointId(9),
            output: PointId(1),
            initial: 2.0,
            min: 0.0,
            max: 5.0,
        }));
    SimDriver::new(map).unwrap()
}

#[test]
fn inventory_and_spill_conserve_volume_through_boundaries_and_checkpoint() {
    let sim = storage_rig();
    let restored = storage_rig();
    let mut discharged = 0.0;
    let mut spilled = 0.0;
    let mut entered = 0.0;
    let mut empty_seen = false;
    let mut full_seen = false;
    for tick in 0..200 {
        let (inflow, request) = if tick < 50 {
            (4.0, 17.0)
        } else if tick < 150 {
            (22.0, 0.0)
        } else {
            (0.0, 17.0)
        };
        sim.write(PointId(2), Value::Float(inflow)).unwrap();
        sim.write(PointId(3), Value::Float(request)).unwrap();
        sim.step(0.2);
        entered += inflow * 0.2;
        discharged += number(&sim, 4) * 0.2;
        spilled += number(&sim, 5) * 0.2;
        let level = number(&sim, 1);
        empty_seen |= level < 1e-12;
        full_seen |= level == 5.0;
        assert!(
            (level * 3.0 + discharged + spilled - 6.0 - entered).abs() < 1e-10,
            "volume balance at tick{tick}"
        );
        assert!((0.0..=5.0).contains(&level));
        if tick == 73 {
            restored
                .restore_state(&sim.capture_state().unwrap())
                .unwrap();
        }
        if tick > 73 {
            restored.write(PointId(2), Value::Float(inflow)).unwrap();
            restored.write(PointId(3), Value::Float(request)).unwrap();
            restored.step(0.2);
            assert_eq!(restored.capture_state(), sim.capture_state());
        }
    }
    assert!(empty_seen && full_seen && spilled > 0.0);
    let before = sim.capture_state().unwrap();
    let mut invalid = before.clone();
    invalid.insert("element.4", Value::Float(-1.0));
    assert!(sim.restore_state(&invalid).is_err());
    assert_eq!(sim.capture_state().unwrap(), before);
}

#[test]
fn map_rejects_invalid_configuration_and_missing_tank_dependencies() {
    let base = ChannelMap::new()
        .with_point(binding(1, 1.0))
        .with_point(binding(2, 0.0))
        .with_point(binding(3, 0.0))
        .with_point(binding(4, 0.0));
    let valid = flow(TankFlowMode::Outlet, 4);
    for invalid in [
        TankFlow { min: 5.0, ..valid },
        TankFlow {
            max: f64::NAN,
            ..valid
        },
        TankFlow {
            flow_per_level: 0.0,
            ..valid
        },
        TankFlow {
            initial: -1.0,
            ..valid
        },
        TankFlow {
            level: PointId(99),
            ..valid
        },
    ] {
        assert!(
            base.clone()
                .with_element(ProcessElement::TankFlow(invalid))
                .validate()
                .is_err()
        );
    }
    let dependencies: Vec<_> = ProcessElement::TankFlow(valid).read_points().collect();
    assert_eq!(dependencies, vec![PointId(1), PointId(2), PointId(3)]);
}
