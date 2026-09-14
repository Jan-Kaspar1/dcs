//! `dcs-controller`: loads a plant model, assembles it against its I/O
//! backend, and runs the deterministic scan — as the active instance, or
//! as a standby tracking an active peer's checkpoints.
//!
//! Usage: `dcs-controller <model-file> [--ticks N] [--scan-ms MS] [--dt T]
//!         [--listen ADDR] [--standby ADDR] [--remote ADDR]`
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
//! `--remote ADDR` attaches to a shared simulated plant served by
//! `dcs-sim-net`'s `PlantServer` instead of building a local `SimDriver`
//! — the field-observing driver mode of the standby-field-observation
//! decision.
//!
//! Redundancy, per the peer-transport decision: an active instance
//! started with `--listen ADDR` also serves `GET /checkpoint`, and its
//! pace loop drives scans through the monitor's lock so a checkpoint is
//! always a between-scans capture. A standby started with
//! `--standby ADDR` pulls those checkpoints, one per scan cycle, applies
//! each to its running executor — aligning at the checkpointed tick and
//! continuing deterministically — and reports `tracking`/`degraded` on
//! stderr. A standby sharing the field through `--remote` is
//! output-quiescent: its writes are dropped by a [`WriteGate`] at the
//! driver boundary and it never steps the shared plant. A standby-local
//! `SimDriver` instead keeps a private plant every checkpoint's driver
//! section resynchronizes. Promotion — lifting the gate — is the
//! follow-up switchover ticket.
//!
//! The binary holds no control logic: the `dcs-blocks` component kinds
//! are registered with the `ComponentRegistry` [`dcs_controller::registry`]
//! builds, and everything inside the executor remains virtual ticks.
//! Load, validation, and assembly failures exit nonzero naming the
//! offending model element.

use dcs_assembly::{assemble, sim_driver};
use dcs_controller::registry;
use dcs_core::{IoDriver, TelemetrySnapshot, Tick};
use dcs_model::PlantModel;
use dcs_monitor::{Monitor, MonitorClient};
use dcs_runtime::{ScanError, Standby, WriteGate};
use dcs_sim::SimDriver;
use dcs_sim_net::RemoteDriver;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::process::ExitCode;
use std::time::{Duration, Instant};

/// The field driver this instance runs: a local [`SimDriver`] built
/// from the model, or a [`RemoteDriver`] attached to a shared simulated
/// plant — the two field-observation modes of the redundancy decisions.
enum Driver {
    /// A private simulated plant held in this process.
    Local(SimDriver),
    /// A client of a shared plant server.
    Remote(RemoteDriver),
}

impl Driver {
    /// The driver as the executor-facing trait object.
    fn io(&self) -> &(dyn IoDriver + Sync) {
        match self {
            Self::Local(sim) => sim,
            Self::Remote(remote) => remote,
        }
    }

    /// Advances the simulated plant by one scan's `dt`. The local driver
    /// steps in place; the remote one steps the shared plant — callers
    /// skip this on a remote standby, where the plant's clock belongs to
    /// the active.
    fn step(&self, dt: f64) -> Result<(), String> {
        match self {
            Self::Local(sim) => {
                sim.step(dt);
                Ok(())
            }
            Self::Remote(remote) => remote
                .step(dt)
                .map(|_| ())
                .map_err(|error| format!("plant step failed: {error}")),
        }
    }
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
    /// Run as a standby pulling checkpoints from the active at this
    /// monitoring address.
    standby: Option<String>,
    /// Attach to the shared simulated plant at this `dcs-sim-net`
    /// address instead of building a local `SimDriver`.
    remote: Option<String>,
}

const USAGE: &str = "\
Usage: dcs-controller <model-file> [--ticks N] [--scan-ms MS] [--dt T]
                      [--listen ADDR] [--standby ADDR] [--remote ADDR]

Loads and validates the plant model, assembles it against the field
driver, and runs the controller scan.

  --ticks N       run N deterministic ticks, then print the telemetry snapshot
  --scan-ms MS    pace scans to a wall-clock period of MS milliseconds;
                  runs until stopped, or for N scans when --ticks is given too
  --dt T          simulated process time per scan (default: scan period in
                  seconds, or 1.0 when unpaced)
  --listen ADDR   serve the monitoring endpoints on ADDR while the paced
                  scan runs; requires --scan-ms. While pacing, POST /scan is
                  refused: the wall clock owns the scan schedule
  --standby ADDR  run as a standby: pull the active's checkpoints from
                  its monitoring address ADDR and apply one per scan
  --remote ADDR   attach to the shared simulated plant at ADDR instead
                  of a local simulation
  -h, --help      show this text

With neither --ticks nor --scan-ms, a paced run at 100 ms is assumed.
--standby does not combine with --listen.";

impl Options {
    fn parse(args: impl Iterator<Item = String>) -> Result<Self, String> {
        let mut model = None;
        let mut ticks = None;
        let mut scan_ms = None;
        let mut dt = None;
        let mut listen = None;
        let mut standby = None;
        let mut remote = None;
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
                "--listen" => listen = Some(value("--listen")?),
                "--standby" => standby = Some(value("--standby")?),
                "--remote" => remote = Some(value("--remote")?),
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
        if standby.is_some() && listen.is_some() {
            return Err("--standby does not combine with --listen".to_string());
        }
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
            standby,
            remote,
        })
    }
}

fn fail(message: impl std::fmt::Display) -> ExitCode {
    eprintln!("error: {message}");
    ExitCode::FAILURE
}

/// Resolves `addr` — `host:port` — for [`MonitorClient`], which wants a
/// concrete [`SocketAddr`].
fn resolve(addr: &str) -> Result<SocketAddr, String> {
    use std::net::ToSocketAddrs;
    addr.to_socket_addrs()
        .map_err(|error| format!("cannot resolve {addr:?}: {error}"))?
        .next()
        .ok_or_else(|| format!("{addr:?} resolves to no address"))
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

    // The field driver: a local simulation, or the shared simulated
    // plant a redundant pair observes together.
    let driver = match &options.remote {
        Some(addr) => {
            let addr = match resolve(addr) {
                Ok(addr) => addr,
                Err(error) => return fail(error),
            };
            match RemoteDriver::connect(addr) {
                Ok(remote) => Driver::Remote(remote),
                Err(error) => {
                    return fail(format!("cannot connect to plant at {addr}: {error}"));
                }
            }
        }
        None => match sim_driver(&model) {
            Ok(sim) => Driver::Local(sim),
            Err(error) => return fail(error),
        },
    };

    // A standby sharing the field through a remote driver is
    // output-quiescent: its writes are gated at the driver boundary so
    // exactly the active writes the shared plant. A standby-local
    // SimDriver needs no gate — its plant is a private tracking copy
    // every checkpoint's driver section resynchronizes.
    let gate = match &driver {
        Driver::Remote(remote) if options.standby.is_some() => Some(WriteGate::closed(remote)),
        _ => None,
    };
    let io: &(dyn IoDriver + Sync) = match &gate {
        Some(gate) => gate,
        None => driver.io(),
    };
    let executor = match assemble(&model, &registry(), io) {
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

    // A remote standby never steps: the shared plant's clock belongs to
    // the active. Every other mode advances the plant one dt per scan.
    let steps_plant = !(options.standby.is_some() && matches!(driver, Driver::Remote(_)));
    let step = || {
        if steps_plant { driver.step(dt) } else { Ok(()) }
    };

    if let Some(peer) = &options.standby {
        // Standby operation: one checkpoint pull per scan cycle. A
        // failed fetch or a rejected checkpoint degrades the standby —
        // named and recoverable — while the next good transfer
        // reconverges it.
        let peer = match resolve(peer) {
            Ok(peer) => peer,
            Err(error) => return fail(error),
        };
        let client = MonitorClient::new(peer);
        // The RefCell lets the two loop closures share the standby; the
        // loop is single-threaded, so the borrows never overlap.
        let standby = std::cell::RefCell::new(Standby::new(executor));
        scan_loop(
            || {
                let mut standby = standby.borrow_mut();
                match client.checkpoint() {
                    Ok(checkpoint) => {
                        if let Err(error) = standby.apply(&checkpoint) {
                            eprintln!(
                                "standby {}: rejected checkpoint from {peer}: {error}",
                                standby.state()
                            );
                        }
                    }
                    Err(error) => {
                        standby.note_transfer_failed(format!("fetch from {peer}: {error}"));
                        eprintln!("standby {}", standby.state());
                    }
                }
                standby.scan()
            },
            || standby.borrow().snapshot(),
            step,
            &options,
            period,
        )
    } else {
        match &options.listen {
            Some(addr) => {
                let monitor =
                    match Monitor::bind_paced(addr.as_str(), executor, model.signal_index()) {
                        Ok(monitor) => monitor,
                        Err(error) => {
                            return fail(format!("cannot bind monitor on {addr}: {error}"));
                        }
                    };
                // Announce the bound address — with a port of 0 this is the
                // only way to learn where the monitor listens. Stderr keeps
                // stdout a pure snapshot stream.
                eprintln!("listening on {}", monitor.local_addr());
                run_monitored(&monitor, step, &options, period.unwrap())
            }
            None => {
                // The RefCell lets the two loop closures share the executor;
                // the loop is single-threaded, so the borrows never overlap.
                let executor = std::cell::RefCell::new(executor);
                scan_loop(
                    || executor.borrow_mut().scan(),
                    || executor.borrow().snapshot(),
                    step,
                    &options,
                    period,
                )
            }
        }
    }
}

/// Serves `monitor` on a scoped thread while the main thread paces scans
/// through [`Monitor::paced_scan`]: the executor stays behind the
/// monitor's one mutex, so a request never observes a half-run scan and a
/// queued command applies at the next scan boundary — and a served
/// `GET /checkpoint` is always a between-scans capture.
/// [`Monitor::shutdown`] stops the serve loop when the run ends and the
/// scope join completes the graceful close.
fn run_monitored(
    monitor: &Monitor<'_>,
    step: impl Fn() -> Result<(), String>,
    options: &Options,
    period: Duration,
) -> ExitCode {
    std::thread::scope(|scope| {
        scope.spawn(|| monitor.serve());
        let result = scan_loop(
            || monitor.paced_scan(),
            || monitor.snapshot(),
            step,
            options,
            Some(period),
        );
        monitor.shutdown();
        result
    })
}

/// The scan loop every run mode shares: `scan` performs one executor
/// scan — directly, through the monitor's lock when serving, or after a
/// standby's checkpoint pull — and `snapshot` reads the resulting
/// telemetry. `step` advances the simulated plant one `dt`; the
/// `--ticks` bound, the snapshot reporting, and the wall-clock pacing
/// are identical either way.
fn scan_loop(
    mut scan: impl FnMut() -> Result<Tick, ScanError>,
    snapshot: impl Fn() -> TelemetrySnapshot,
    step: impl Fn() -> Result<(), String>,
    options: &Options,
    period: Option<Duration>,
) -> ExitCode {
    let mut scanned = 0_u64;
    loop {
        let started = Instant::now();
        if let Err(error) = scan() {
            return fail(format!("scan {} failed: {error}", snapshot().tick.0));
        }
        if let Err(error) = step() {
            return fail(error);
        }
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
