//! `dcs-controller`: loads a plant model, assembles it against the
//! simulated I/O backend, and runs the deterministic scan.
//!
//! Usage: `dcs-controller <model-file> [--ticks N] [--scan-ms MS] [--dt T]`
//!
//! `--ticks N` runs N scans deterministically and prints the final
//! telemetry snapshot; `--scan-ms MS` paces scans to wall-clock time —
//! the pacing the execution-model decision assigns to the outer driver,
//! never to components — running until stopped, or for N scans when both
//! options are given. `--dt T` sets the simulated process time advanced per
//! scan; it defaults to the scan period in seconds, or 1.0 unpaced.
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
use dcs_model::PlantModel;
use dcs_runtime::Component;
use std::path::PathBuf;
use std::process::ExitCode;
use std::time::{Duration, Instant};

/// The `dcs-blocks` kinds this controller can instantiate — every kind
/// the component library ships, keyed by each type's `KIND` constant.
fn registry() -> ComponentRegistry {
    ComponentRegistry::new()
        .with(AnalogInput::<f64>::KIND, |spec| {
            AnalogInput::<f64>::from_parameters(
                spec.name.as_str(),
                spec.require("raw")?,
                spec.require("out")?,
                spec.parameters,
            )
            .map(|block| Box::new(block) as Box<dyn Component>)
            .map_err(BuildError::other)
        })
        .with(AnalogOutput::<f64>::KIND, |spec| {
            AnalogOutput::<f64>::from_parameters(
                spec.name.as_str(),
                spec.require("eng")?,
                spec.require("raw")?,
                spec.parameters,
            )
            .map(|block| Box::new(block) as Box<dyn Component>)
            .map_err(BuildError::other)
        })
        .with(Pid::KIND, |spec| {
            Pid::from_parameters(
                spec.name.as_str(),
                spec.require("sp")?,
                spec.require("pv")?,
                spec.require("out")?,
                spec.parameters,
            )
            .map(|block| Box::new(block) as Box<dyn Component>)
            .map_err(BuildError::other)
        })
        .with(DigitalInput::KIND, |spec| {
            DigitalInput::from_parameters(
                spec.name.as_str(),
                spec.require("in")?,
                spec.require("out")?,
                spec.parameters,
            )
            .map(|block| Box::new(block) as Box<dyn Component>)
            .map_err(BuildError::other)
        })
        .with(DigitalOutput::KIND, |spec| {
            DigitalOutput::from_parameters(
                spec.name.as_str(),
                spec.require("in")?,
                spec.require("out")?,
                spec.parameters,
            )
            .map(|block| Box::new(block) as Box<dyn Component>)
            .map_err(BuildError::other)
        })
        .with(AlarmMonitor::KIND, |spec| {
            AlarmMonitor::from_parameters(
                spec.name.as_str(),
                spec.require("in")?,
                spec.require("alarm")?,
                spec.parameters,
            )
            .map(|block| Box::new(block) as Box<dyn Component>)
            .map_err(BuildError::other)
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
            Interlock::from_parameters(
                spec.name.as_str(),
                spec.require("in")?,
                spec.require("permissive")?,
                trips,
                spec.require("out")?,
                spec.require("tripped")?,
                spec.parameters,
            )
            .map(|block| Box::new(block) as Box<dyn Component>)
            .map_err(BuildError::other)
        })
        .with(OverrideSelect::KIND, |spec| {
            OverrideSelect::from_parameters(
                spec.name.as_str(),
                spec.require("control")?,
                spec.require("operator")?,
                spec.require("select")?,
                spec.require("out")?,
                spec.parameters,
            )
            .map(|block| Box::new(block) as Box<dyn Component>)
            .map_err(BuildError::other)
        })
        .with(Valve::KIND, |spec| {
            Valve::from_parameters(
                spec.name.as_str(),
                spec.require("cmd")?,
                spec.require("out")?,
                spec.require("fb")?,
                spec.require("discrepancy")?,
                spec.parameters,
            )
            .map(|block| Box::new(block) as Box<dyn Component>)
            .map_err(BuildError::other)
        })
        .with(Motor::KIND, |spec| {
            Motor::from_parameters(
                spec.name.as_str(),
                spec.require("cmd")?,
                spec.require("out")?,
                spec.require("run")?,
                spec.require("fault")?,
                spec.parameters,
            )
            .map(|block| Box::new(block) as Box<dyn Component>)
            .map_err(BuildError::other)
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
}

const USAGE: &str = "\
Usage: dcs-controller <model-file> [--ticks N] [--scan-ms MS] [--dt T]

Loads and validates the plant model, assembles it against simulated I/O,
and runs the controller scan.

  --ticks N     run N deterministic ticks, then print the telemetry snapshot
  --scan-ms MS  pace scans to a wall-clock period of MS milliseconds;
                runs until stopped, or for N scans when --ticks is given too
  --dt T        simulated process time per scan (default: scan period in
                seconds, or 1.0 when unpaced)
  -h, --help    show this text

With neither --ticks nor --scan-ms, a paced run at 100 ms is assumed.";

impl Options {
    fn parse(args: impl Iterator<Item = String>) -> Result<Self, String> {
        let mut model = None;
        let mut ticks = None;
        let mut scan_ms = None;
        let mut dt = None;
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
        Ok(Self {
            model,
            ticks,
            scan_ms,
            dt,
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
    let mut executor = match assemble(&model, &registry(), &driver) {
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

    let mut scanned = 0_u64;
    loop {
        let started = Instant::now();
        if let Err(error) = executor.scan() {
            return fail(format!("scan {} failed: {error}", executor.tick().0));
        }
        driver.step(dt);
        scanned += 1;

        if let Some(ticks) = options.ticks {
            if scanned >= ticks {
                match serde_json::to_string_pretty(&executor.snapshot()) {
                    Ok(snapshot) => println!("{snapshot}"),
                    Err(error) => return fail(format!("cannot serialize snapshot: {error}")),
                }
                return ExitCode::SUCCESS;
            }
        } else {
            // Continuous operation: report the run's state as JSON lines.
            match serde_json::to_string(&executor.snapshot()) {
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
