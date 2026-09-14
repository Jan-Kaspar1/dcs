//! `dcs-controller`: loads a plant model, resolves its devices through the
//! driver registry — local `sim*` devices plus remote `sim-tcp` ones —
//! assembles the executor against the resulting fan-out driver, and runs
//! the deterministic scan — as the active instance, or as a standby
//! tracking an active peer's checkpoints.
//!
//! Usage: `dcs-controller <model-file> [--ticks N] [--scan-ms MS] [--dt T]
//!         [--listen ADDR] [--standby ADDR] [--remote ADDR] [--driven]`
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
//! `--driven` is the alternative to pacing for an externally paced run —
//! the deterministic, request-timed mode a scripted redundant pair runs
//! under: `--listen` serves the monitor and scans happen only when
//! `POST /scan` requests them, each requested scan running the scan
//! cycle's wiring inside the request — a tracking standby's checkpoint
//! pull first, then the scan, then the plant step a field-owning
//! instance owes the run — so the run is exactly as deterministic as
//! the requests driving it. `--driven` requires `--listen` and excludes
//! `--scan-ms` and `--ticks`.
//!
//! `--remote ADDR` attaches to a shared simulated plant served by
//! `dcs-sim-net`'s `PlantServer` instead of resolving the model's devices
//! through the registry — the field-observing driver mode of the
//! standby-field-observation decision.
//!
//! Redundancy, per the peer-transport and switchover-semantics
//! decisions: every instance whose driver surface reaches the shared
//! field runs behind a [`WriteGate`], and the [`Peer`] role machine
//! decides which peer's writes pass — a `--remote` attachment behind a
//! gate covering every write, a registry-resolved fan-out declaring
//! `sim-tcp` devices behind a gate covering only the field-facing
//! points so a tracking standby's local simulated backends keep
//! stepping their private plant. An active instance started with `--listen ADDR`
//! serves `GET /checkpoint`, `GET /role`, and `POST /demote`, and its
//! pace loop drives scans through the monitor's lock so a checkpoint is
//! always a between-scans capture. A standby started with
//! `--standby ADDR --listen ADDR` pulls checkpoints from the active at
//! the first address, one per scan cycle, applies each to its running
//! executor — aligning at the checkpointed tick and continuing
//! deterministically — and serves its own monitor at the second, where
//! `GET /role` reports `standby` plus its convergence and
//! `POST /promote` is the operator's switchover action: the gate lifts
//! at the request's scan boundary, the next scan writes what the
//! checkpointed run would have, and the instance starts stepping the
//! shared plant. The documented switchover order — `POST /demote` on
//! the old active first — keeps exactly one peer writing the field.
//! A standby-local `SimDriver` needs no gate: its plant is a private
//! tracking copy every checkpoint's driver section resynchronizes.
//!
//! The monitoring page presents the pair as one logical controller: open
//! it on either peer's `--listen` address and pass the other peer's
//! address as `?peer=<host:port>` — e.g.
//! `http://active:8080/?peer=standby:8081`. The page polls `GET /role`
//! on each peer, renders the active's telemetry plus pair health, and
//! submits commands only to the peer reporting `active`.
//!
//! The binary holds no control logic: the `dcs-blocks` component kinds
//! are registered with the `ComponentRegistry` [`dcs_controller::registry`]
//! builds, and everything inside the executor remains virtual ticks.
//! Load, validation, and assembly failures exit nonzero naming the
//! offending model element.

use dcs_assembly::{DriverRegistry, FanoutDriver, assemble, resolve_drivers};
use dcs_controller::registry;
use dcs_core::{IoDriver, TelemetrySnapshot, Tick};
use dcs_model::PlantModel;
use dcs_monitor::{Driven, Monitor, MonitorClient};
use dcs_runtime::{Peer, ScanError, WriteGate};
use dcs_sim_net::RemoteDriver;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::process::ExitCode;
use std::time::{Duration, Instant};

/// The field driver this instance runs: the [`FanoutDriver`] the driver
/// registry builds from the model — local `sim*` backends plus any
/// model-declared `sim-tcp` devices — or a [`RemoteDriver`] attached to a
/// shared simulated plant — the two field-observation modes of the
/// redundancy decisions.
enum Driver {
    /// The registry-resolved backends held in this process.
    Local(FanoutDriver),
    /// A client of a shared plant server.
    Remote(RemoteDriver),
}

impl Driver {
    /// The driver as the executor-facing trait object.
    fn io(&self) -> &(dyn IoDriver + Sync) {
        match self {
            Self::Local(fanout) => fanout,
            Self::Remote(remote) => remote,
        }
    }

    /// Advances the simulated plant by one scan's `dt`. A field-owning
    /// instance steps everything — local backends plus the shared
    /// plant's remote clock; a tracking instance steps only its local
    /// backends — the shared field's clock belongs to the owner, so a
    /// remote-attached standby steps nothing and a fan-out standby
    /// leaves `sim-tcp` backends to the active.
    fn step(&self, dt: f64, owns_field: bool) -> Result<(), String> {
        match (self, owns_field) {
            (Self::Local(fanout), true) => fanout
                .step(dt)
                .map_err(|error| format!("plant step failed: {error}")),
            (Self::Local(fanout), false) => fanout
                .step_local(dt)
                .map_err(|error| format!("plant step failed: {error}")),
            (Self::Remote(remote), true) => remote
                .step(dt)
                .map(|_| ())
                .map_err(|error| format!("plant step failed: {error}")),
            (Self::Remote(_), false) => Ok(()),
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
    /// Serve the monitor without pacing: scans run only when
    /// `POST /scan` requests them, each request carrying the scan
    /// cycle's checkpoint pull and plant step with it. Requires
    /// `--listen`; the deterministic mode a scripted redundant pair
    /// runs under.
    driven: bool,
}

const USAGE: &str = "\
Usage: dcs-controller <model-file> [--ticks N] [--scan-ms MS] [--dt T]
                      [--listen ADDR] [--standby ADDR] [--remote ADDR]
                      [--driven]

Loads and validates the plant model, resolves its devices through the
driver registry (local `sim*` and remote `sim-tcp` kinds), and runs the
controller scan.

  --ticks N       run N deterministic ticks, then print the telemetry snapshot
  --scan-ms MS    pace scans to a wall-clock period of MS milliseconds;
                  runs until stopped, or for N scans when --ticks is given too
  --dt T          simulated process time per scan (default: scan period in
                  seconds, or 1.0 when unpaced)
  --listen ADDR   serve the monitoring endpoints on ADDR while the paced
                  scan runs; requires --scan-ms. While pacing, POST /scan is
                  refused: the wall clock owns the scan schedule
  --standby ADDR  run as a standby: pull the active's checkpoints from
                  its monitoring address ADDR and apply one per scan;
                  combines with --listen, whose POST /promote is the
                  switchover action
  --remote ADDR   attach to the shared simulated plant at ADDR instead
                  of a local simulation
  --driven        serve the monitor without pacing: scans run only when
                  POST /scan requests them, each request also running a
                  tracking standby's checkpoint pull and the plant step a
                  field-owning run paces to its ticks; requires --listen
                  and excludes --scan-ms and --ticks
  -h, --help      show this text

With neither --ticks nor --scan-ms, a paced run at 100 ms is assumed.
A remote-attached standby is output-quiescent behind a write gate until
POST /promote lifts it; a demoted remote active is re-quiesced the same
way, so exactly one peer writes the shared plant.";

impl Options {
    fn parse(args: impl Iterator<Item = String>) -> Result<Self, String> {
        let mut model = None;
        let mut ticks = None;
        let mut scan_ms = None;
        let mut dt = None;
        let mut listen = None;
        let mut standby = None;
        let mut remote = None;
        let mut driven = false;
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
                "--driven" => driven = true,
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
        if !driven && ticks.is_none() && scan_ms.is_none() {
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
        if driven {
            if listen.is_none() {
                return Err(
                    "--driven requires --listen: scans arrive through POST /scan".to_string(),
                );
            }
            if ticks.is_some() || scan_ms.is_some() {
                return Err(
                    "--driven paces scans through POST /scan; --ticks and --scan-ms do not apply"
                        .to_string(),
                );
            }
        } else if listen.is_some() && scan_ms.is_none() {
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
            driven,
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
    // The field driver: the registry-resolved fan-out — local simulated
    // backends plus any `sim-tcp` devices the model declares — or the
    // shared simulated plant a redundant pair observes together.
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
        None => {
            match resolve_drivers(&model, &DriverRegistry::standard()).and_then(|plan| plan.build())
            {
                Ok(fanout) => Driver::Local(fanout),
                Err(error) => return fail(error),
            }
        }
    };

    // Every instance whose driver surface reaches the shared field runs
    // behind the write gate: on a standby it quiesces field writes until
    // promotion lifts it, and on an active it is what demotion re-closes
    // — the single-writer invariant of the switchover-semantics
    // decision. A `--remote` attachment gates every write; a
    // registry-resolved fan-out declaring `sim-tcp` devices gates only
    // the field-facing points, so a tracking standby's local simulated
    // backends still see their writes and their private plant keeps
    // tracking.
    let gate = match &driver {
        Driver::Remote(remote) => Some(WriteGate::closed(remote)),
        Driver::Local(fanout) if fanout.has_field_backend() => {
            Some(WriteGate::closed_covering(fanout, |point| {
                fanout.is_field_point(point)
            }))
        }
        Driver::Local(_) => None,
    };
    let io: &(dyn IoDriver + Sync) = match &gate {
        Some(gate) => gate,
        None => driver.io(),
    };
    let executor = match assemble(&model, &registry(), io) {
        Ok(executor) => executor,
        Err(error) => return fail(error),
    };

    // The role machine: a --standby instance tracks its active's
    // checkpoints gate-closed until promoted; anything else owns the
    // field from the start.
    let peer = match &options.standby {
        Some(_) => Peer::standby(executor, gate.as_ref()),
        None => Peer::active(executor, gate.as_ref()),
    };

    // The simulated process time per scan: explicit --dt, else the
    // wall-clock period in seconds, else one unit per unpaced tick.
    let dt = options
        .dt
        .or_else(|| options.scan_ms.map(|ms| ms as f64 / 1000.0))
        .unwrap_or(1.0);
    let period = options.scan_ms.map(Duration::from_millis);

    // The externally paced mode: serve the monitor unpaced and let
    // `POST /scan` requests drive the run — each requested scan carries
    // a tracking standby's checkpoint pull and the plant step the
    // peer's field ownership calls for. A scripted redundant pair runs
    // its whole scenario — converge, promote, demote — through these
    // requests, tick by tick, without a wall clock.
    if options.driven {
        let addr = options.listen.as_deref().unwrap();
        let track = match &options.standby {
            Some(active) => match resolve(active) {
                Ok(active) => Some(active),
                Err(error) => return fail(error),
            },
            None => None,
        };
        let monitor = match Monitor::bind_peer(addr, peer, model.signal_index()) {
            Ok(monitor) => monitor,
            Err(error) => {
                return fail(format!("cannot bind monitor on {addr}: {error}"));
            }
        };
        let monitor = monitor.driven(Driven {
            track,
            after_scan: Some(Box::new(|owns_field| driver.step(dt, owns_field))),
        });
        eprintln!("listening on {}", monitor.local_addr());
        monitor.serve();
        return ExitCode::SUCCESS;
    }

    if let Some(active_addr) = &options.standby {
        // Standby operation: one checkpoint pull per scan cycle while the
        // peer does not own the field. A failed fetch or a rejected
        // checkpoint degrades the standby — named and recoverable —
        // while the next good transfer reconverges it.
        let active_addr = match resolve(active_addr) {
            Ok(active_addr) => active_addr,
            Err(error) => return fail(error),
        };
        let client = MonitorClient::new(active_addr);
        match &options.listen {
            Some(addr) => {
                let monitor =
                    match Monitor::bind_paced_peer(addr.as_str(), peer, model.signal_index()) {
                        Ok(monitor) => monitor,
                        Err(error) => {
                            return fail(format!("cannot bind monitor on {addr}: {error}"));
                        }
                    };
                eprintln!("listening on {}", monitor.local_addr());
                let step = || driver.step(dt, monitor.owns_field());
                run_monitored(
                    &monitor,
                    || {
                        if !monitor.owns_field() {
                            match client.checkpoint() {
                                Ok(checkpoint) => {
                                    if let Err(error) = monitor.apply_checkpoint(&checkpoint) {
                                        eprintln!(
                                            "standby: rejected checkpoint from {active_addr}: {error}"
                                        );
                                    }
                                }
                                Err(error) => {
                                    monitor.note_transfer_failed(format!(
                                        "fetch from {active_addr}: {error}"
                                    ));
                                    eprintln!("standby: fetch from {active_addr} failed: {error}");
                                }
                            }
                        }
                        monitor.paced_scan()
                    },
                    step,
                    &options,
                    period.unwrap(),
                )
            }
            None => {
                // Without a monitor nothing can promote this standby;
                // the RefCell lets the two loop closures share the peer.
                let peer = std::cell::RefCell::new(peer);
                let step = || driver.step(dt, false);
                scan_loop(
                    || {
                        let mut peer = peer.borrow_mut();
                        match client.checkpoint() {
                            Ok(checkpoint) => {
                                if let Err(error) = peer.apply(&checkpoint) {
                                    eprintln!(
                                        "standby: rejected checkpoint from {active_addr}: {error}"
                                    );
                                }
                            }
                            Err(error) => {
                                peer.note_transfer_failed(format!(
                                    "fetch from {active_addr}: {error}"
                                ));
                                eprintln!("standby: fetch from {active_addr} failed: {error}");
                            }
                        }
                        peer.scan()
                    },
                    || peer.borrow().snapshot(),
                    step,
                    || peer.borrow_mut().record_scan_overrun(),
                    &options,
                    period,
                )
            }
        }
    } else {
        match &options.listen {
            Some(addr) => {
                let monitor =
                    match Monitor::bind_paced_peer(addr.as_str(), peer, model.signal_index()) {
                        Ok(monitor) => monitor,
                        Err(error) => {
                            return fail(format!("cannot bind monitor on {addr}: {error}"));
                        }
                    };
                // Announce the bound address — with a port of 0 this is the
                // only way to learn where the monitor listens. Stderr keeps
                // stdout a pure snapshot stream.
                eprintln!("listening on {}", monitor.local_addr());
                // Demotion may re-quiesce this instance mid-run, so the
                // plant step consults the role each scan.
                let step = || driver.step(dt, monitor.owns_field());
                run_monitored(
                    &monitor,
                    || monitor.paced_scan(),
                    step,
                    &options,
                    period.unwrap(),
                )
            }
            None => {
                // The RefCell lets the two loop closures share the peer;
                // the loop is single-threaded, so the borrows never
                // overlap. No monitor means no demotion path, so the
                // field ownership below never changes.
                let peer = std::cell::RefCell::new(peer);
                scan_loop(
                    || peer.borrow_mut().scan(),
                    || peer.borrow().snapshot(),
                    || driver.step(dt, true),
                    || peer.borrow_mut().record_scan_overrun(),
                    &options,
                    period,
                )
            }
        }
    }
}

/// Serves `monitor` on a scoped thread while the main thread paces
/// scans: the peer stays behind the monitor's one mutex, so a request
/// never observes a half-run scan, a queued command applies at the next
/// scan boundary, a served `GET /checkpoint` is always a between-scans
/// capture, and a `POST /promote`/`POST /demote` lands at the same
/// boundary. `scan` is one scan cycle — a plain
/// [`Monitor::paced_scan`] for an active, a checkpoint pull plus paced
/// scan for a standby. [`Monitor::shutdown`] stops the serve loop when
/// the run ends and the scope join completes the graceful close.
fn run_monitored(
    monitor: &Monitor<'_>,
    scan: impl FnMut() -> Result<Tick, ScanError>,
    step: impl Fn() -> Result<(), String>,
    options: &Options,
    period: Duration,
) -> ExitCode {
    std::thread::scope(|scope| {
        scope.spawn(|| monitor.serve());
        let result = scan_loop(
            scan,
            || monitor.snapshot(),
            step,
            || monitor.record_scan_overrun(),
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
///
/// `overrun` is the paced loop's feed for the snapshot's
/// `io_health.scan_overruns`: a cycle whose wall-clock elapsed reaches
/// its period is reported through it once. The counter itself lives in
/// the executor — callers wire this closure to
/// `record_scan_overrun` on whichever wrapper they scan through — so
/// wall-clock overrun detection stays out here in the shell and only a
/// count, not a timestamp, enters the tick domain.
fn scan_loop(
    mut scan: impl FnMut() -> Result<Tick, ScanError>,
    snapshot: impl Fn() -> TelemetrySnapshot,
    step: impl Fn() -> Result<(), String>,
    mut overrun: impl FnMut(),
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
            } else {
                // The cycle overran its period — there is nothing left
                // to sleep off, so report it into io_health.scan_overruns.
                overrun();
            }
        }
    }
}
