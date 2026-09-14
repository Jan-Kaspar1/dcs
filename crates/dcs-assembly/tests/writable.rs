//! Integration tests for the model-declared command surface: the
//! `writable_points` fixture declares writable and unmarked `io_point`s
//! of every kind, and the assembled executor's command path enforces
//! the declaration — writable `In` points apply their commands at the
//! scan boundary, everything else is refused at submission with the
//! named `not_writable` rejection carrying the point.

use dcs_assembly::{ComponentRegistry, assemble, sim_driver};
use dcs_blocks::RateLimiter;
use dcs_core::{
    Command, CommandError, CommandOutcome, CommandReceipt, IoDriver, PointId, Quality,
    QualityReason, Sample, Tick, Value,
};
use dcs_model::PlantModel;
use dcs_runtime::Component;

/// The checked-in fixture: writable and unmarked field `In` points,
/// a field `Out` point, writable and unmarked internal `In` points,
/// and an internal `Out` point.
const WRITABLE_POINTS: &str = include_str!("../fixtures/writable_points.json");

const FIELD_WRITABLE: PointId = PointId(10);
const FIELD_UNMARKED: PointId = PointId(11);
const FIELD_OUT: PointId = PointId(12);
const INTERNAL_WRITABLE: PointId = PointId(20);
const INTERNAL_UNMARKED: PointId = PointId(21);
const INTERNAL_OUT: PointId = PointId(30);

/// Only the fixture's `rate-limiter` kind is needed here.
fn registry() -> ComponentRegistry {
    ComponentRegistry::new().with(RateLimiter::KIND, |spec| {
        RateLimiter::from_parameters(
            spec.name.as_str(),
            spec.require("in")?,
            spec.require("out")?,
            spec.parameters,
        )
        .map(|component| Box::new(component) as Box<dyn Component>)
        .map_err(dcs_assembly::BuildError::other)
    })
}

fn write_value(point: PointId, value: Value) -> Command {
    Command::WriteValue {
        point,
        kind: value.kind(),
        value,
    }
}

#[test]
fn fixture_declares_writable_and_unmarked_points() {
    let model = PlantModel::load(WRITABLE_POINTS).unwrap();
    let writable_of = |id| {
        model
            .io_points
            .iter()
            .find(|point| point.id == id)
            .unwrap()
            .writable
    };
    assert!(writable_of(FIELD_WRITABLE));
    assert!(writable_of(INTERNAL_WRITABLE));
    for point in [FIELD_UNMARKED, FIELD_OUT, INTERNAL_UNMARKED, INTERNAL_OUT] {
        assert!(!writable_of(point), "{point:?}");
    }

    // The marks reach the signal index monitoring consumers read.
    let index = model.signal_index();
    assert!(index.get(FIELD_WRITABLE).unwrap().writable);
    assert!(index.get(INTERNAL_WRITABLE).unwrap().writable);
    assert!(!index.get(FIELD_UNMARKED).unwrap().writable);
    assert!(!index.get(FIELD_OUT).unwrap().writable);

    // The model serde-roundtrips: `writable` survives as declared, and
    // unmarked points serialize back without the key.
    let json = serde_json::to_string_pretty(&model).unwrap();
    let reloaded = PlantModel::load(&json).unwrap();
    assert_eq!(reloaded, model);
    assert!(json.contains("\"writable\": true"), "{json}");
}

#[test]
fn writable_field_input_applies_at_the_scan_boundary() {
    let model = PlantModel::load(WRITABLE_POINTS).unwrap();
    let driver = sim_driver(&model).unwrap();
    let mut executor = assemble(&model, &registry(), &driver).unwrap();

    // Operator substitution of the input image: the write is forwarded
    // to the driver at the scan head and the same scan's input read —
    // and the component's step — observe it.
    let receipt = executor.submit_command(write_value(FIELD_WRITABLE, Value::Float(7.0)));
    assert_eq!(
        receipt.outcome,
        CommandOutcome::Accepted {
            apply_tick: Tick(1)
        }
    );
    // Queued, not yet applied: the driver still holds the old value.
    assert_eq!(
        driver.read(FIELD_WRITABLE).unwrap().value,
        Value::Float(0.0)
    );

    executor.scan().unwrap();
    assert_eq!(
        executor.receipts()[0].outcome,
        CommandOutcome::Applied { tick: Tick(1) }
    );
    assert_eq!(
        executor.sample(FIELD_WRITABLE),
        Some(Sample::good(Value::Float(7.0), Tick(1)))
    );
    // The substituted input drove the valve output the same scan.
    assert_eq!(driver.read(FIELD_OUT).unwrap().value, Value::Float(7.0));

    // The substitution holds until the field side asserts a new value.
    executor.scan().unwrap();
    assert_eq!(
        executor.sample(FIELD_WRITABLE).unwrap().value,
        Value::Float(7.0)
    );
}

#[test]
fn writable_internal_point_updates_the_image_at_the_boundary() {
    let model = PlantModel::load(WRITABLE_POINTS).unwrap();
    let driver = sim_driver(&model).unwrap();
    let mut executor = assemble(&model, &registry(), &driver).unwrap();

    // The declared initial serves the point before any command, and a
    // first scan seeds the limiter's tracked current at it.
    assert_eq!(
        executor.sample(INTERNAL_WRITABLE),
        Some(Sample::good(Value::Float(2.0), Tick::ZERO))
    );
    executor.scan().unwrap();

    let receipt = executor.submit_command(write_value(INTERNAL_WRITABLE, Value::Float(9.0)));
    assert_eq!(
        receipt.outcome,
        CommandOutcome::Accepted {
            apply_tick: Tick(2)
        }
    );
    executor.scan().unwrap();
    assert_eq!(
        executor.receipts()[0].outcome,
        CommandOutcome::Applied { tick: Tick(2) }
    );
    assert_eq!(
        executor.sample(INTERNAL_WRITABLE),
        Some(Sample::good(Value::Float(9.0), Tick(2)))
    );
    // The rate limiter observed the commanded value that same scan,
    // slewing its recorded output one `max_delta` toward it.
    assert_eq!(
        executor.sample(INTERNAL_OUT).unwrap().value,
        Value::Float(7.0)
    );

    // The held value survives until the next command.
    executor.scan().unwrap();
    assert_eq!(
        executor.sample(INTERNAL_WRITABLE).unwrap().value,
        Value::Float(9.0)
    );
}

#[test]
fn forced_field_input_holds_across_scans_until_released() {
    let model = PlantModel::load(WRITABLE_POINTS).unwrap();
    let driver = sim_driver(&model).unwrap();
    let mut executor = assemble(&model, &registry(), &driver).unwrap();
    executor.scan().unwrap();

    let receipt = executor.submit_command(Command::ForcePoint {
        point: FIELD_WRITABLE,
        kind: dcs_core::ValueKind::Float,
        value: Value::Float(9.0),
    });
    assert_eq!(
        receipt.outcome,
        CommandOutcome::Accepted {
            apply_tick: Tick(2)
        }
    );

    // The field reasserting a different value changes nothing: every
    // scan's image reports the forced value at Substituted quality, and
    // the limiter slews its valve output toward the forced input.
    driver.write(FIELD_WRITABLE, Value::Float(0.5)).unwrap();
    for tick in 2..=3u64 {
        executor.scan().unwrap();
        assert_eq!(
            executor.sample(FIELD_WRITABLE),
            Some(Sample::new(
                Value::Float(9.0),
                Quality::Uncertain(QualityReason::Substituted),
                Tick(tick),
            )),
            "tick {tick}"
        );
    }
    assert_eq!(
        executor.snapshot().forces,
        vec![dcs_core::ForcedPoint {
            point: FIELD_WRITABLE,
            value: Value::Float(9.0),
        }]
    );

    // Release resumes the live read at the next scan boundary.
    executor.submit_command(Command::UnforcePoint {
        point: FIELD_WRITABLE,
    });
    executor.scan().unwrap();
    assert_eq!(
        executor.sample(FIELD_WRITABLE),
        Some(Sample::good(Value::Float(0.5), Tick(4)))
    );
    assert!(executor.snapshot().forces.is_empty());
}

#[test]
fn forcing_unmarked_or_out_points_rejects_with_not_writable() {
    let model = PlantModel::load(WRITABLE_POINTS).unwrap();
    let driver = sim_driver(&model).unwrap();
    let mut executor = assemble(&model, &registry(), &driver).unwrap();

    for point in [FIELD_UNMARKED, INTERNAL_UNMARKED, FIELD_OUT, INTERNAL_OUT] {
        let receipt = executor.submit_command(Command::ForcePoint {
            point,
            kind: dcs_core::ValueKind::Float,
            value: Value::Float(5.0),
        });
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::NotWritable { point }
            },
            "{point:?}"
        );
    }
}

#[test]
fn unmarked_points_reject_with_not_writable_naming_the_point() {
    let model = PlantModel::load(WRITABLE_POINTS).unwrap();
    let driver = sim_driver(&model).unwrap();
    let mut executor = assemble(&model, &registry(), &driver).unwrap();

    for point in [FIELD_UNMARKED, INTERNAL_UNMARKED] {
        let receipt = executor.submit_command(write_value(point, Value::Float(5.0)));
        assert_eq!(
            receipt,
            CommandReceipt {
                command: write_value(point, Value::Float(5.0)),
                outcome: CommandOutcome::Rejected {
                    reason: CommandError::NotWritable { point }
                },
            },
            "{point:?}"
        );
    }

    // Refused at submission: nothing queues for the scan, so no
    // application receipts appear and the held initial stands.
    executor.scan().unwrap();
    assert_eq!(executor.receipts().len(), 2);
    assert_eq!(
        executor.sample(INTERNAL_UNMARKED).unwrap().value,
        Value::Float(1.0)
    );
    assert_eq!(
        executor.sample(FIELD_UNMARKED).unwrap().value,
        Value::Float(0.0)
    );
}

#[test]
fn out_point_commands_are_rejected_per_the_documented_rule() {
    // The recorded rule: `Out` points are never command targets — the
    // command path refuses them outright, so a valid model cannot mark
    // one writable either.
    let model = PlantModel::load(WRITABLE_POINTS).unwrap();
    let driver = sim_driver(&model).unwrap();
    let mut executor = assemble(&model, &registry(), &driver).unwrap();

    for point in [FIELD_OUT, INTERNAL_OUT] {
        let receipt = executor.submit_command(write_value(point, Value::Float(5.0)));
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::NotWritable { point }
            },
            "{point:?}"
        );
    }
    executor.scan().unwrap();
    assert_eq!(driver.read(FIELD_OUT).unwrap().value, Value::Float(0.0));
}

#[test]
fn not_writable_serializes_in_the_command_contract() {
    // The wire name the monitoring UI's rejection display matches on.
    let error = CommandError::NotWritable { point: PointId(11) };
    let json = serde_json::to_string(&error).unwrap();
    assert_eq!(json, r#"{"not_writable":{"point":11}}"#);
    assert_eq!(serde_json::from_str::<CommandError>(&json).unwrap(), error);
    assert_eq!(error.point(), Some(PointId(11)));
}
