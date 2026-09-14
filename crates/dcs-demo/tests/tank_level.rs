//! Integration test for the end-to-end simulated tank level loop.

use dcs_demo::{MODEL_DOCUMENT, SETPOINT_PERCENT};
use dcs_model::PlantModel;

/// Scans per run: 40 simulated seconds at `SCAN_PERIOD` = 0.1.
const SCANS: u64 = 400;
/// Number of trailing scans the level must hold the setpoint for.
const HOLD_SCANS: usize = 100;
/// How close the level must track the setpoint, in percent of span.
const TOLERANCE_PERCENT: f64 = 0.5;

#[test]
fn model_fixture_loads_and_validates() {
    let model = PlantModel::load(MODEL_DOCUMENT).unwrap();
    assert!(model.validate().is_empty());
}

#[test]
fn loop_reaches_setpoint_and_holds() {
    let run = dcs_demo::run(MODEL_DOCUMENT, SCANS).unwrap();
    assert_eq!(run.levels.len(), SCANS as usize);

    // No component failed a step during the run.
    for component in &run.snapshot.components {
        assert_eq!(
            component.step_errors, 0,
            "{}: {:?}",
            component.name, component.last_error
        );
    }

    // The loop actually moved the level: it starts near empty.
    assert!(run.levels[0] < SETPOINT_PERCENT - TOLERANCE_PERCENT);

    // Stated hold: each of the last HOLD_SCANS scans is within
    // TOLERANCE_PERCENT of the setpoint.
    let tail = &run.levels[run.levels.len() - HOLD_SCANS..];
    for (index, level) in tail.iter().enumerate() {
        assert!(
            (level - SETPOINT_PERCENT).abs() <= TOLERANCE_PERCENT,
            "scan {}: level {level}% outside {SETPOINT_PERCENT}±{TOLERANCE_PERCENT}%",
            SCANS as usize - HOLD_SCANS + index + 1,
        );
    }
}

#[test]
fn identical_runs_produce_identical_results() {
    let first = dcs_demo::run(MODEL_DOCUMENT, SCANS).unwrap();
    let second = dcs_demo::run(MODEL_DOCUMENT, SCANS).unwrap();
    assert_eq!(first, second);
}
