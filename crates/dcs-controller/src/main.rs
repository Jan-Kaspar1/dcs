//! `dcs-controller`: loads a plant model, assembles it against the
//! simulated I/O backend, and runs the deterministic scan.
//!
//! Usage: `dcs-controller <model-file> [--ticks N] [--scan-ms MS] [--dt T]
//!         [--listen ADDR]`
//!
//! `--ticks N` runs N scans deterministically and prints the final
//! telemetry snapshot; `--scan-ms MS` paces scans to wall-clock time —
//! the pacing the execution-model decision assigns to the outer driver,
//! never to components — running until stopped, or for N scans when both
//! options are given. `--dt T` sets the simulated process time advanced per
//! scan; it defaults to the scan period in seconds, or 1.0 unpaced.
//!
//! `--listen ADDR` serves the `dcs-monitor` endpoints alongside the paced
//! scan, sharing the executor behind the monitor's mutex so a request
//! never observes a half-run scan. Under pacing the wall clock owns the
//! scan schedule, so `POST /scan` is refused (`409`): externally
//! requested scans would inject ticks no schedule accounts for. Commands
//! still queue through `POST /command` and apply at the next scan
//! boundary. Monitoring requires pacing — a pure `--ticks` run stays
//! deterministic and monitor-free.
//!
//! The binary holds no control logic: the `dcs-blocks` component kinds are
//! registered with [`ComponentRegistry`], and everything inside the
//! executor remains virtual ticks. Load, validation, and assembly failures
//! exit nonzero naming the offending model element.

use dcs_assembly::{BuildError, ComponentRegistry, assemble, sim_driver};
use dcs_blocks::{
    AlarmMonitor, AnalogInput, AnalogOutput, DigitalInput, DigitalOutput, Interlock, Motor,
    OverrideSelect, Pid, Valve,
};
use dcs_core::{TelemetrySnapshot, Tick, ValueKind};
use dcs_model::PlantModel;
use dcs_monitor::Monitor;
use dcs_runtime::{Component, ScanError};
use dcs_sim::SimDriver;
use std::path::PathBuf;
use std::process::ExitCode;
use std::time::{Duration, Instant};

/// Boxes a built component or lifts its construction failure into
/// [`BuildError`].
fn boxed<C, E>(result: Result<C, E>) -> Result<Box<dyn Component>, BuildError>
where
    C: Component + 'static,
    E: std::error::Error + 'static,
{
    result
        .map(|component| Box::new(component) as Box<dyn Component>)
        .map_err(BuildError::other)
}

/// The `dcs-blocks` kinds this controller can instantiate — every kind
/// the component library ships, keyed by each type's `KIND` constant.
/// Analog kinds dispatch on the bound raw point's value kind: an `Int`
/// channel builds the `i64` variant, anything else the `f64` one (a
/// `Bool` raw point then fails wiring as a type mismatch, naming the
/// element).
fn registry() -> ComponentRegistry {
    ComponentRegistry::new()
        .with(AnalogInput::<f64>::KIND, |spec| {
            let raw = spec.require("raw")?;
            let out = spec.require("out")?;
            if spec.point_kind(raw) == Some(ValueKind::Int) {
                boxed(AnalogInput::<i64>::from_parameters(
                    spec.name.as_str(),
                    raw,
                    out,
                    spec.parameters,
                ))
            } else {
                boxed(AnalogInput::<f64>::from_parameters(
                    spec.name.as_str(),
                    raw,
                    out,
                    spec.parameters,
                ))
            }
        })
        .with(AnalogOutput::<f64>::KIND, |spec| {
            let eng = spec.require("eng")?;
            let raw = spec.require("raw")?;
            if spec.point_kind(raw) == Some(ValueKind::Int) {
                boxed(AnalogOutput::<i64>::from_parameters(
                    spec.name.as_str(),
                    eng,
                    raw,
                    spec.parameters,
                ))
            } else {
                boxed(AnalogOutput::<f64>::from_parameters(
                    spec.name.as_str(),
                    eng,
                    raw,
                    spec.parameters,
                ))
            }
        })
        .with(Pid::KIND, |spec| {
            boxed(Pid::from_parameters(
                spec.name.as_str(),
                spec.require("sp")?,
                spec.require("pv")?,
                spec.require("out")?,
                spec.parameters,
            ))
        })
        .with(DigitalInput::KIND, |spec| {
            boxed(DigitalInput::from_parameters(
                spec.name.as_str(),
                spec.require("in")?,
                spec.require("out")?,
                spec.parameters,
            ))
        })
        .with(DigitalOutput::KIND, |spec| {
            boxed(DigitalOutput::from_parameters(
                spec.name.as_str(),
                spec.require("in")?,
                spec.require("out")?,
                spec.parameters,
            ))
        })
        .with(AlarmMonitor::KIND, |spec| {
            boxed(AlarmMonitor::from_parameters(
                spec.name.as_str(),
                spec.require("in")?,
                spec.require("alarm")?,
                spec.parameters,
            ))
        })
        .with(Interlock::KIND, |spec| {
            // Trip inputs are declared `trip_1` … `trip_N`; order the
            // bound points by numeric suffix, not lexically.
            let mut trips: Vec<_> = spec
                .ports
                .iter()
                .filter_map(|(name, point)| {
                    name.strip_prefix("trip_")
                        .and_then(|suffix| suffix.parse::<usize>().ok())
                        .map(|index| (index, *point))
                })
                .collect();
            trips.sort_by_key(|(index, _)| *index);
            let trips: Vec<_> = trips.into_iter().map(|(_, point)| point).collect();
            boxed(Interlock::from_parameters(
                spec.name.as_str(),
                spec.require("in")?,
                spec.require("permissive")?,
                trips,
                spec.require("out")?,
                spec.require("tripped")?,
                spec.parameters,
            ))
        })
        .with(OverrideSelect::KIND, |spec| {
            boxed(OverrideSelect::from_parameters(
                spec.name.as_str(),
                spec.require("control")?,
                spec.require("operator")?,
                spec.require("select")?,
                spec.require("out")?,
                spec.parameters,
            ))
        })
        .with(Valve::KIND, |spec| {
            boxed(Valve::from_parameters(
                spec.name.as_str(),
                spec.require("cmd")?,
                spec.require("out")?,
                spec.require("fb")?,
                spec.require("discrepancy")?,
                spec.parameters,
            ))
        })
        .with(Motor::KIND, |spec| {
            boxed(Motor::from_parameters(
                spec.name.as_str(),
                spec.require("cmd")?,
                spec.require("out")?,
                spec.require("run")?,
                spec.require("fault")?,
                spec.parameters,
            ))
        })
}

/// Parsed command line.
struct Options {
    /// The plant model document to load.
    model: PathBuf,
    /// How many scans to run; `None` runs until stopped.
    ticks: Option<u64>,
    /// Wall-clock scan period in milliseconds; `None` runs unpaced.
    scan_ms: Option<u64>,
    /// Simulated process time advanced per scan.
    dt: Option<f64>,
    /// Address the monitoring endpoints are served on; requires pacing.
    listen: Option<String>,
}

const USAGE: &str = "\
Usage: dcs-controller <model-file> [--ticks N] [--scan-ms MS] [--dt T]
           [--listen ADDR]

Loads and validates the plant model, assembles it against simulated I/O,
and runs the controller scan.

  --ticks N      run N deterministic ticks, then print the telemetry snapshot
  --scan-ms MS   pace scans to a wall-clock period of MS milliseconds;
                 runs until stopped, or for N scans when --ticks is given too
  --dt T         simulated process time per scan (default: scan period in
                 seconds, or 1.0 when unpaced)
  --listen ADDR  serve the monitoring endpoints on ADDR while the paced
                 scan runs; requires --scan-ms. While pacing, POST /scan is
                 refused: the wall clock owns the scan schedule
  -h, --help     show this text

With neither --ticks nor --scan-ms, a paced run at 100 ms is assumed.";

impl Options {
    fn parse(args: impl Iterator<Item = String>) -> Result<Self, String> {
        let mut model = None;
        let mut ticks = None;
        let mut scan_ms = None;
        let mut dt = None;
        let mut listen = None;
        let mut args = args;
        while let Some(arg) = args.next() {
            let mut value = |flag: &str| {
                args.next()
                    .ok_or_else(|| format!("{flag} requires a value"))
            };
            match arg.as_str() {
                "--ticks" => {
                    ticks = Some(
                        value("--ticks")?
                            .parse::<u64>()
                            .map_err(|error| format!("invalid --ticks value: {error}"))?,
                    );
                }
                "--scan-ms" => {
                    scan_ms = Some(
                        value("--scan-ms")?
                            .parse::<u64>()
                            .map_err(|error| format!("invalid --scan-ms value: {error}"))?,
                    );
                }
                "--dt" => {
                    dt = Some(
                        value("--dt")?
                            .parse::<f64>()
                            .map_err(|error| format!("invalid --dt value: {error}"))?,
                    );
                }
                "--listen" => {
                    listen = Some(value("--listen")?);
                }
                "-h" | "--help" => {
                    println!("{USAGE}");
                    std::process::exit(0);
                }
                _ if arg.starts_with('-') => {
                    return Err(format!("unknown option {arg:?}"));
                }
                _ if model.is_none() => model = Some(PathBuf::from(arg)),
                _ => return Err(format!("unexpected argument {arg:?}")),
            }
        }
        let model = model.ok_or_else(|| "missing <model-file>".to_string())?;
        if ticks.is_none() && scan_ms.is_none() {
            scan_ms = Some(100);
        }
        if let Some(period) = scan_ms
            && period == 0
        {
            return Err("--scan-ms must be positive".to_string());
        }
        if let Some(dt) = dt
            && (!dt.is_finite() || dt < 0.0)
        {
            return Err("--dt must be finite and non-negative".to_string());
        }
        if listen.is_some() && scan_ms.is_none() {
            return Err(
                "--listen requires --scan-ms: monitoring runs alongside the paced scan".to_string(),
            );
        }
        Ok(Self {
            model,
            ticks,
            scan_ms,
            dt,
            listen,
        })
    }
}

fn fail(message: impl std::fmt::Display) -> ExitCode {
    eprintln!("error: {message}");
    ExitCode::FAILURE
}

fn main() -> ExitCode {
    let options = match Options::parse(std::env::args().skip(1)) {
        Ok(options) => options,
        Err(error) => {
            eprintln!("error: {error}\n\n{USAGE}");
            return ExitCode::from(2);
        }
    };

    let source = match std::fs::read_to_string(&options.model) {
        Ok(source) => source,
        Err(error) => {
            return fail(format!(
                "cannot read model file {}: {error}",
                options.model.display()
            ));
        }
    };
    let model = match PlantModel::load(&source) {
        Ok(model) => model,
        Err(error) => return fail(error),
    };
    let driver = match sim_driver(&model) {
        Ok(driver) => driver,
        Err(error) => return fail(error),
    };
    let executor = match assemble(&model, &registry(), &driver) {
        Ok(executor) => executor,
        Err(error) => return fail(error),
    };

    // The simulated process time per scan: explicit --dt, else the
    // wall-clock period in seconds, else one unit per unpaced tick.
    let dt = options
        .dt
        .or_else(|| options.scan_ms.map(|ms| ms as f64 / 1000.0))
        .unwrap_or(1.0);
    let period = options.scan_ms.map(Duration::from_millis);

    match &options.listen {
        Some(addr) => {
            let monitor = match Monitor::bind_paced(addr.as_str(), executor, model.signal_index()) {
                Ok(monitor) => monitor,
                Err(error) => {
                    return fail(format!("cannot bind monitor on {addr}: {error}"));
                }
            };
            // Announce the bound address — with a port of 0 this is the
            // only way to learn where the monitor listens. Stderr keeps
            // stdout a pure snapshot stream.
            eprintln!("listening on {}", monitor.local_addr());
            run_monitored(&monitor, &driver, &options, period.unwrap(), dt)
        }
        None => {
            // The RefCell lets the two loop closures share the executor;
            // the loop is single-threaded, so the borrows never overlap.
            let executor = std::cell::RefCell::new(executor);
            scan_loop(
                || executor.borrow_mut().scan(),
                || executor.borrow().snapshot(),
                &driver,
                &options,
                period,
                dt,
            )
        }
    }
}

/// Serves `monitor` on a scoped thread while the main thread paces scans
/// through [`Monitor::paced_scan`]: the executor stays behind the
/// monitor's one mutex, so a request never observes a half-run scan and a
/// queued command applies at the next scan boundary. [`Monitor::shutdown`]
/// stops the serve loop when the run ends and the scope join completes
/// the graceful close.
fn run_monitored(
    monitor: &Monitor<'_>,
    driver: &SimDriver,
    options: &Options,
    period: Duration,
    dt: f64,
) -> ExitCode {
    std::thread::scope(|scope| {
        scope.spawn(|| monitor.serve());
        let result = scan_loop(
            || monitor.paced_scan(),
            || monitor.snapshot(),
            driver,
            options,
            Some(period),
            dt,
        );
        monitor.shutdown();
        result
    })
}

/// The scan loop both run modes share: `scan` performs one executor scan
/// — directly, or through the monitor's lock when serving — and
/// `snapshot` reads the resulting telemetry. The `--ticks` bound, the
/// snapshot reporting, and the wall-clock pacing are identical either
/// way.
fn scan_loop(
    mut scan: impl FnMut() -> Result<Tick, ScanError>,
    snapshot: impl Fn() -> TelemetrySnapshot,
    driver: &SimDriver,
    options: &Options,
    period: Option<Duration>,
    dt: f64,
) -> ExitCode {
    let mut scanned = 0_u64;
    loop {
        let started = Instant::now();
        if let Err(error) = scan() {
            return fail(format!("scan {} failed: {error}", snapshot().tick.0));
        }
        driver.step(dt);
        scanned += 1;

        if let Some(ticks) = options.ticks {
            if scanned >= ticks {
                return match serde_json::to_string_pretty(&snapshot()) {
                    Ok(snapshot) => {
                        println!("{snapshot}");
                        ExitCode::SUCCESS
                    }
                    Err(error) => fail(format!("cannot serialize snapshot: {error}")),
                };
            }
        } else {
            // Continuous operation: report the run's state as JSON lines.
            match serde_json::to_string(&snapshot()) {
                Ok(snapshot) => println!("{snapshot}"),
                Err(error) => return fail(format!("cannot serialize snapshot: {error}")),
            }
        }

        if let Some(period) = period {
            let elapsed = started.elapsed();
            if elapsed < period {
                std::thread::sleep(period - elapsed);
            }
        }
    }
}
