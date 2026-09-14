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
//! - `dcs-model diff <old> <new>` reports what a revision changes —
//!   added, removed, and changed devices, io_points, signals, components,
//!   and connections, each entry naming its element. `--json` emits the
//!   [`ModelDiff`](dcs_model::ModelDiff) as JSON instead of the human
//!   listing.
//! - `dcs-model lint <file>` reports advisory engineering-quality findings
//!   — valid but probably unfinished declarations, like an io_point no
//!   signal sources or a device channel nothing binds — one `rule element:
//!   message` line each in model order. Findings are advisory and exit
//!   zero; `--strict` exits nonzero when any are found.
//!
//! Malformed input — unreadable files, broken JSON, unsupported document
//! versions, invalid models — produces error output naming the problem and
//! a nonzero exit, never a panic.

use dcs_model::{FieldChange, LoadError, ModelDiff, PlantModel};
use std::collections::BTreeSet;
use std::fmt::Write as _;
use std::process::ExitCode;

const USAGE: &str = "\
usage: dcs-model <command> <file>...

commands:
  validate <file>          validate a plant model document, listing every error
  summary <file>           print model element counts
  signal-index <file>      print the point-to-signal index as JSON
  diff <old> <new>         report what a model revision changes; --json emits it as JSON
  lint <file>              report advisory engineering-quality findings; --strict exits nonzero on them";

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
    let [command, rest @ ..] = args else {
        return Err(format!("expected a command and a model file\n{USAGE}"));
    };
    match command.as_str() {
        "validate" => validate(one_file(rest)?),
        "summary" => summary(one_file(rest)?),
        "signal-index" => signal_index(one_file(rest)?),
        "diff" => diff(rest),
        "lint" => lint(rest),
        _ => Err(format!("unknown command {command:?}\n{USAGE}")),
    }
}

/// The single model-file argument every one-file command takes.
fn one_file(args: &[String]) -> Result<&str, String> {
    let [path] = args else {
        return Err(format!("expected a command and a model file\n{USAGE}"));
    };
    Ok(path)
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

/// `diff <old> <new> [--json]` reports what a revision declares differently.
/// Both documents must validate; one that fails is reported with its
/// validation errors rather than diffed.
fn diff(args: &[String]) -> Result<String, String> {
    let mut json = false;
    let mut paths = Vec::new();
    for arg in args {
        if arg == "--json" {
            json = true;
        } else if arg.starts_with("--") {
            return Err(format!("unknown option {arg:?}\n{USAGE}"));
        } else {
            paths.push(arg.as_str());
        }
    }
    if paths.len() != 2 {
        return Err(format!("diff takes two model files\n{USAGE}"));
    }
    let old = load(paths[0])?;
    let new = load(paths[1])?;
    let diff = old.diff(&new);
    if json {
        serde_json::to_string_pretty(&diff)
            .map_err(|error| format!("cannot serialize the model diff: {error}"))
    } else {
        Ok(format_diff(&diff))
    }
}

/// Renders a [`ModelDiff`] as a human-readable listing: one section per
/// element class, one line per element prefixed by its change kind, and
/// field-level changes indented beneath the changed element they belong
/// to. An empty diff reports `no changes`.
fn format_diff(diff: &ModelDiff) -> String {
    if diff.is_empty() {
        return "no changes".to_owned();
    }
    let mut output = String::new();
    for (class, entries) in [
        ("devices", &diff.devices),
        ("io_points", &diff.io_points),
        ("signals", &diff.signals),
        ("components", &diff.components),
        ("connections", &diff.connections),
    ] {
        if entries.is_empty() {
            continue;
        }
        let _ = writeln!(output, "{class}:");
        for entry in entries {
            let _ = writeln!(output, "  {} {}", entry.change, entry.element);
            for field in &entry.fields {
                let _ = writeln!(output, "    {}", format_field_change(field));
            }
        }
    }
    output.trim_end().to_owned()
}

/// `lint <file> [--strict]` prints the model's advisory
/// [`LintFinding`](dcs_model::LintFinding)s — one `rule element: message`
/// line each — or `no findings`. Lint is advisory, so findings exit zero
/// unless `--strict` is given, which fails the command with the listing.
/// A document failing validation reports its validation errors rather
/// than being linted.
fn lint(args: &[String]) -> Result<String, String> {
    let mut strict = false;
    let mut paths = Vec::new();
    for arg in args {
        if arg == "--strict" {
            strict = true;
        } else if arg.starts_with("--") {
            return Err(format!("unknown option {arg:?}\n{USAGE}"));
        } else {
            paths.push(arg.as_str());
        }
    }
    let [path] = paths.as_slice() else {
        return Err(format!("lint takes one model file\n{USAGE}"));
    };
    let model = load(path)?;
    let findings = model.lint();
    if findings.is_empty() {
        return Ok("no findings".to_owned());
    }
    let mut listing = String::new();
    for finding in &findings {
        let _ = writeln!(listing, "{finding}");
    }
    if strict {
        Err(format!(
            "{path}: {} lint finding(s) under --strict:\n{}",
            findings.len(),
            listing.trim_end()
        ))
    } else {
        Ok(listing.trim_end().to_owned())
    }
}

/// Renders one field-level change: `field: old -> new` when both sides
/// carry a value, `field: added <new>` or `field: removed <old>` when the
/// field appears or disappears. Values print as their JSON serialization.
fn format_field_change(field: &FieldChange) -> String {
    match (&field.old, &field.new) {
        (Some(old), Some(new)) => format!("{}: {old} -> {new}", field.field),
        (None, Some(new)) => format!("{}: added {new}", field.field),
        (Some(old), None) => format!("{}: removed {old}", field.field),
        (None, None) => field.field.clone(),
    }
}
