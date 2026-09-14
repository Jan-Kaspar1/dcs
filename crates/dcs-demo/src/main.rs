//! Runs the end-to-end simulated tank level loop.
//!
//! `dcs-demo [model.json]` — with no argument it runs the checked-in
//! `fixtures/tank_level.json` model; with a path it runs that document
//! instead. It loads and validates the model, assembles the simulated
//! driver and the executor from it, runs [`dcs_demo::DEFAULT_SCANS`]
//! scans, and prints the final telemetry snapshot as JSON. Output is
//! deterministic: identical invocations print identical bytes.

use std::process::ExitCode;

fn main() -> ExitCode {
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
