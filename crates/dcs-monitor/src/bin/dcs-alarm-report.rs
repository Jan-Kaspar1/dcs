//! `dcs-alarm-report`: the alarm flood and performance report — the
//! flood-and-performance decision's computing surface, tooling-side
//! like `dcs-ctl`: it consumes the existing monitoring surface and the
//! durable journal file and prints the declared metric set as JSON,
//! adding no served endpoint, no in-controller computation, and no new
//! wire type — the plant-overview decision's aggregation-stays-out
//! precedent applied to `WW-ALM-004`.
//!
//! The invocation names one monitor by its `host:port` address and
//! where the journal comes from:
//!
//! - `dcs-alarm-report <addr>` computes over the served journal —
//!   `GET /journal` — the live, bounded view;
//! - `dcs-alarm-report <addr> --journal-file <path>` computes over the
//!   monitor's durable journal file — the restart-surviving dataset,
//!   including lifetimes the bounded in-memory ring has evicted.
//!
//! Either way `GET /snapshot` and `GET /signals` supply the alarm
//! surface — the descriptor-to-point join naming each instance's
//! `alarm`/`unacknowledged`/`ack`/`shelved`/`suppressed`/
//! `out_of_service` points and the `priority`/`response_ticks`
//! parameters — and `GET /history` the retained samples that date a
//! standing alarm the journal no longer holds.
//!
//! `--config <path>` declares the report thresholds — the flood window
//! and bound (conventional default: more than 10 alarms in 10 minutes),
//! the stale bound, the chattering and fleeting bounds, and the
//! `ticks_per_minute` mapping journal ticks carry — as a JSON
//! [`ReportConfig`](dcs_monitor::alarm_report::ReportConfig) document;
//! an absent flag computes against the defaults. The report echoes the
//! resolved thresholds it ran with.
//!
//! Output is the [`AlarmReport`](dcs_monitor::alarm_report::AlarmReport)
//! pretty-encoded on stdout — deterministic for an identical record
//! and config. Failures exit nonzero with stderr naming what refused:
//! an unreachable monitor, an unreadable journal file, or a malformed
//! config. The tool never panics.

use dcs_monitor::alarm_report::{AlarmReport, ReportConfig, alarm_instances, compute_report};
use dcs_monitor::{JournalData, MonitorClient, RunBoundary, read_journal_file};
use std::net::{SocketAddr, ToSocketAddrs};
use std::path::PathBuf;
use std::process::ExitCode;

const USAGE: &str = "\
usage: dcs-alarm-report <addr> [--journal-file <path>] [--config <path>]

Computes the alarm flood and performance report — the declared metric
set of alarm rate and peak rate, flood periods against the declared
threshold, standing and stale alarms, chattering and fleeting alarms,
most-frequent alarms, shelving and suppression accounting,
acknowledgment response times, and the annunciated-priority
distribution — over the controller's durable alarm record, and prints
it as JSON on stdout.

  <addr>                  the monitor's host:port; the snapshot,
                          signals, and history endpoints supply the
                          alarm surface
  --journal-file <path>   read the monitor's durable journal file
                          instead of GET /journal — the dataset that
                          survives restart
  --config <path>         the declared report thresholds as a JSON
                          ReportConfig document; absent, the
                          conventional defaults compute

example: dcs-alarm-report 127.0.0.1:8080 --journal-file journal.jsonl";

fn main() -> ExitCode {
    match run(std::env::args().skip(1).collect()) {
        Ok(report) => {
            println!("{report}");
            ExitCode::SUCCESS
        }
        Err(message) => {
            eprintln!("{message}");
            ExitCode::FAILURE
        }
    }
}

/// One invocation: parse, fetch, compute, encode.
fn run(args: Vec<String>) -> Result<String, String> {
    let (addr, journal_file, config) = parse(&args)?;
    let client = MonitorClient::new(addr);
    let snapshot = client
        .snapshot()
        .map_err(|error| format!("dcs-alarm-report: {addr}: snapshot: {error}"))?;
    let index = client
        .signals()
        .map_err(|error| format!("dcs-alarm-report: {addr}: signals: {error}"))?;
    let history = client
        .history(&[], 0)
        .map_err(|error| format!("dcs-alarm-report: {addr}: history: {error}"))?;
    // The journal source: the durable file when declared — its
    // run-boundary markers reaching the report so durations spanning a
    // restart compute on the elapsed-scans axis — else the served,
    // bounded view.
    let (entries, boundaries): (Vec<dcs_core::JournalEntry>, Vec<RunBoundary>) = match &journal_file
    {
        Some(path) => {
            let JournalData {
                entries,
                boundaries,
            } = read_journal_file(path).map_err(|error| error.to_string())?;
            (entries, boundaries)
        }
        None => (
            client
                .journal(0)
                .map_err(|error| format!("dcs-alarm-report: {addr}: journal: {error}"))?,
            Vec::new(),
        ),
    };
    let alarms = alarm_instances(&snapshot, &index);
    let report: AlarmReport = compute_report(
        &alarms,
        &entries,
        &boundaries,
        &history,
        snapshot.tick,
        &config,
    );
    serde_json::to_string_pretty(&report)
        .map_err(|error| format!("dcs-alarm-report: cannot encode the report: {error}"))
}

/// The parsed command line: the monitor address, the journal source,
/// and the declared report configuration.
fn parse(args: &[String]) -> Result<(SocketAddr, Option<PathBuf>, ReportConfig), String> {
    let mut addr = None;
    let mut journal_file = None;
    let mut config_path = None;
    let mut args = args.iter();
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--journal-file" => {
                let path = args
                    .next()
                    .ok_or_else(|| "--journal-file expects a path".to_string())?;
                if journal_file.replace(PathBuf::from(path)).is_some() {
                    return Err("--journal-file takes a single path".to_string());
                }
            }
            "--config" => {
                let path = args
                    .next()
                    .ok_or_else(|| "--config expects a path".to_string())?;
                if config_path.replace(PathBuf::from(path)).is_some() {
                    return Err("--config takes a single path".to_string());
                }
            }
            flag if flag.starts_with("--") => {
                return Err(format!("unknown flag {flag:?}\n{USAGE}"));
            }
            positional => {
                if addr.replace(positional.to_string()).is_some() {
                    return Err(format!("unexpected argument {positional:?}\n{USAGE}"));
                }
            }
        }
    }
    let Some(addr) = addr else {
        return Err(format!("expected a monitor address\n{USAGE}"));
    };
    let addr = addr
        .to_socket_addrs()
        .map_err(|error| format!("invalid monitor address {addr:?}: {error}"))?
        .next()
        .ok_or_else(|| format!("invalid monitor address {addr:?}: resolves to nothing"))?;
    let config = match config_path {
        Some(path) => {
            let text = std::fs::read_to_string(&path).map_err(|error| {
                format!(
                    "dcs-alarm-report: cannot read config {}: {error}",
                    path.display()
                )
            })?;
            serde_json::from_str(&text).map_err(|error| {
                format!(
                    "dcs-alarm-report: config {} is not a report config: {error}",
                    path.display()
                )
            })?
        }
        None => ReportConfig::default(),
    };
    Ok((addr, journal_file, config))
}
