//! `dcs-model`: the engineering-side command-line consumer of the plant
//! model contract shared by the controller and the monitoring UI.
//!
//! Subcommands:
//!
//! - `dcs-model validate <file>` parses and validates a model document,
//!   printing every validation error and exiting nonzero on failure.
//! - `dcs-model summary <file>` prints counts of devices, io_points,
//!   signals, components, and connections, plus the number of distinct
//!   signal display groups declared.
//! - `dcs-model signal-index <file>` emits the point-to-signal
//!   [`SignalIndex`](dcs_model::SignalIndex) as JSON.
//!
//! Malformed input — unreadable files, broken JSON, unsupported document
//! versions, invalid models — produces error output naming the problem and
//! a nonzero exit, never a panic.

use dcs_model::{LoadError, PlantModel};
use std::collections::BTreeSet;
use std::fmt::Write as _;
use std::process::ExitCode;

const USAGE: &str = "\
usage: dcs-model <command> <file>

commands:
  validate <file>      validate a plant model document, listing every error
  summary <file>       print model element counts
  signal-index <file>  print the point-to-signal index as JSON";

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match run(&args) {
        Ok(output) => {
            println!("{output}");
            ExitCode::SUCCESS
        }
        Err(message) => {
            eprintln!("{message}");
            ExitCode::FAILURE
        }
    }
}

/// Runs one command line, returning the text for stdout or the error
/// message for stderr.
fn run(args: &[String]) -> Result<String, String> {
    let [command, path] = args else {
        return Err(format!("expected a command and a model file\n{USAGE}"));
    };
    match command.as_str() {
        "validate" => validate(path),
        "summary" => summary(path),
        "signal-index" => signal_index(path),
        _ => Err(format!("unknown command {command:?}\n{USAGE}")),
    }
}

/// Reads and fully validates a model document. A validation failure
/// reports every error found, one per line.
fn load(path: &str) -> Result<PlantModel, String> {
    let source = std::fs::read_to_string(path)
        .map_err(|error| format!("{path}: cannot read file: {error}"))?;
    PlantModel::load(&source).map_err(|error| match error {
        LoadError::Invalid(errors) => {
            let mut message = format!("{path}: {} validation error(s):", errors.len());
            for error in errors {
                let _ = writeln!(message, "  {error}");
            }
            message
        }
        error => format!("{path}: {error}"),
    })
}

fn validate(path: &str) -> Result<String, String> {
    load(path).map(|_| format!("{path}: valid"))
}

fn summary(path: &str) -> Result<String, String> {
    let model = load(path)?;
    // Signal groups are display metadata: report how many distinct groups
    // the model declares; ungrouped signals do not add to the count.
    let groups: BTreeSet<&str> = model
        .signals
        .iter()
        .filter_map(|signal| signal.group.as_deref())
        .collect();
    Ok(format!(
        "\
devices: {}\n\
io_points: {}\n\
signals: {}\n\
signal_groups: {}\n\
components: {}\n\
connections: {}",
        model.devices.len(),
        model.io_points.len(),
        model.signals.len(),
        groups.len(),
        model.components.len(),
        model.connections.len()
    ))
}

fn signal_index(path: &str) -> Result<String, String> {
    let model = load(path)?;
    serde_json::to_string_pretty(&model.signal_index())
        .map_err(|error| format!("{path}: cannot serialize the signal index: {error}"))
}
