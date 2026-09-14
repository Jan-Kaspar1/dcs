//! Runs the end-to-end simulated tank level loop.
//!
//! `dcs-demo [model.json | --showcase]` — with no argument it runs the
//! checked-in `fixtures/tank_level.json` model; with a path it runs that
//! document instead. It loads and validates the model, assembles the
//! simulated driver and the executor from it, runs
//! [`dcs_demo::DEFAULT_SCANS`] scans, and prints the final telemetry
//! snapshot as JSON. Output is deterministic: identical invocations
//! print identical bytes.
//!
//! `--showcase` runs `fixtures/showcase.json` — the pump-and-tank line
//! exercising the whole component library, assembled through the
//! standard registries — through its documented command-and-fault
//! scenario, printing each phase's outcome plus the final snapshot.

use std::process::ExitCode;

/// Runs the showcase scenario and prints its documented outcomes.
fn run_showcase() -> ExitCode {
    use dcs_demo::showcase::{self, points};
    match showcase::run(showcase::SHOWCASE_DOCUMENT) {
        Ok(run) => {
            let settled = showcase::sample(&run.settled, points::LEVEL_PERCENT);
            let moved = showcase::sample(&run.moved, points::LEVEL_PERCENT);
            let faulted = (
                showcase::sample(&run.faulted, points::INTERLOCK_TRIPPED),
                showcase::sample(&run.faulted, points::LEVEL_ALARM),
            );
            let recovered = showcase::sample(&run.recovered, points::LEVEL_PERCENT);
            println!("pump-and-tank showcase after {} scans:", run.levels.len());
            if let Some(sample) = settled {
                println!(
                    "  settled:   level {:.2}% at {:.0}% setpoint",
                    f64::try_from(sample.value).unwrap_or(f64::NAN),
                    showcase::INITIAL_SETPOINT,
                );
            }
            if let Some(sample) = moved {
                println!(
                    "  commanded: setpoint moved to {:.0}% at the phase boundary; level {:.2}%",
                    showcase::MOVED_SETPOINT,
                    f64::try_from(sample.value).unwrap_or(f64::NAN),
                );
            }
            if let (Some(tripped), Some(alarm)) = faulted {
                println!(
                    "  faulted:   p101_run disconnected — interlock tripped {}, level alarm {}",
                    bool::try_from(tripped.value).unwrap_or_default(),
                    bool::try_from(alarm.value).unwrap_or_default(),
                );
            }
            if let Some(sample) = recovered {
                println!(
                    "  recovered: fault cleared — interlock reset, level {:.2}%",
                    f64::try_from(sample.value).unwrap_or(f64::NAN),
                );
            }
            match serde_json::to_string_pretty(&run.recovered) {
                Ok(json) => {
                    println!("{json}");
                    ExitCode::SUCCESS
                }
                Err(error) => {
                    eprintln!("dcs-demo: cannot serialize telemetry snapshot: {error}");
                    ExitCode::FAILURE
                }
            }
        }
        Err(error) => {
            eprintln!("dcs-demo: {error}");
            ExitCode::FAILURE
        }
    }
}

fn main() -> ExitCode {
    if std::env::args().nth(1).as_deref() == Some("--showcase") {
        return run_showcase();
    }
    let source = match std::env::args().nth(1) {
        Some(path) => match std::fs::read_to_string(&path) {
            Ok(source) => source,
            Err(error) => {
                eprintln!("dcs-demo: cannot read {path}: {error}");
                return ExitCode::FAILURE;
            }
        },
        None => dcs_demo::MODEL_DOCUMENT.to_string(),
    };
    match dcs_demo::run(&source, dcs_demo::DEFAULT_SCANS) {
        Ok(run) => {
            println!(
                "tank level after {} scans: {:.3}% (setpoint {:.1}%)",
                run.levels.len(),
                run.levels.last().copied().unwrap_or(0.0),
                dcs_demo::SETPOINT_PERCENT,
            );
            match serde_json::to_string_pretty(&run.snapshot) {
                Ok(json) => {
                    println!("{json}");
                    ExitCode::SUCCESS
                }
                Err(error) => {
                    eprintln!("dcs-demo: cannot serialize telemetry snapshot: {error}");
                    ExitCode::FAILURE
                }
            }
        }
        Err(error) => {
            eprintln!("dcs-demo: {error}");
            ExitCode::FAILURE
        }
    }
}
