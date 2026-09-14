//! `dcs-controller`: loads a plant model, resolves its devices through the
//! driver registry — local `sim*` devices plus remote `sim-tcp` ones —
//! assembles the executor against the resulting fan-out driver, and runs
//! the deterministic scan — as the active instance, or as a standby
//! tracking an active peer's checkpoints.
//!
//! Usage: `dcs-controller <model-file> [--check] [--ticks N]
//!         [--scan-ms MS] [--dt T] [--listen ADDR] [--standby ADDR]
//!         [--remote ADDR] [--driven] [--auto-promote N]
//!         [--state-file PATH] [--journal-file PATH]`
//!
//! `--check` is the engineering compile-check: the model is loaded,
//! validated, and assembled through the standard registries — device
//! kinds resolved, channels mapped, ports bound, parameters
//! constructed — and a summary of what assembled prints, answering
//! "does this model assemble" without starting a run. No scan executes
//! and no listener binds; a load, validation, or assembly failure exits
//! nonzero naming the element exactly as a run would. Run-mode options
//! do not apply and are rejected as usage errors.
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
//! `--state-file PATH` persists the run for restart recovery — the
//! no-peer half of the checkpoint machinery: the serde `Checkpoint`
//! `GET /checkpoint` serves — versioned and fingerprinted per the
//! checkpoint-versioning decision — is written to `PATH` at the end of
//! every completed scan cycle, after the scan and the plant step, by
//! write-then-rename so a crash mid-write cannot leave a torn file. When
//! `PATH` exists at startup the run resumes from it: the checkpoint is
//! applied to the freshly assembled executor before pacing begins, so
//! the next scan continues the interrupted run tick-for-tick. Resume is
//! all-or-nothing, the checkpoint-restore rule — an unreadable or
//! unparseable file, an unsupported format version, or a fingerprint or
//! structural mismatch (a state file captured under a different model)
//! exits nonzero naming the reason rather than silently starting fresh;
//! a missing file is a cold start. The standby path is unchanged — where
//! a redundant peer exists it remains the preferred recovery story, its
//! checkpoint stream converging a standby continuously rather than at
//! the last persisted cycle.
//!
//! `--journal-file PATH` persists the transition journal the monitor
//! records — the journal-persistence decision's durable audit trail:
//! every journaled entry is appended to `PATH` as one line-delimited
//! JSON record at the same recording point, and startup replays the
//! file into the served ring with `seq` numbering continued where it
//! left off, so `GET /journal` answers continuously across a restart.
//! A run-boundary marker line separates process lifetimes within one
//! file; a file that cannot be replayed exits nonzero naming the file
//! and the offending record, and a missing file is a cold start. The
//! journal requires `--listen` — the recorder lives in the monitor —
//! and stays deliberately separate from `--state-file`: the checkpoint
//! is overwritten per save and consumed by restore, the journal is
//! append-only and consumed by review; a `--state-file`-resumed run
//! keeps appending to the same journal file in the restored tick
//! domain.
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
//! Automatic failover, per the failover decision: a standby armed with
//! `--auto-promote N` treats the checkpoint pull as the heartbeat —
//! `N` consecutive failed pulls is active loss, and the peer
//! self-promotes at that scan boundary provided it still holds its
//! convergence proof. Promotion — manual or automatic — first takes the
//! shared plant's write-ownership claim on this instance's owner token,
//! so a still-alive old peer's writes are refused by the field itself
//! (`IoError::Fenced`), and a promoted standby continues writing. A
//! model whose field-facing devices cannot arbitrate a single writer
//! refuses `--auto-promote` at startup; manual promotion still works.
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
use dcs_monitor::{Driven, Monitor, MonitorClient, MonitorConfig};
use dcs_runtime::{Checkpoint, Executor, Peer, ScanError, WriteGate};
use dcs_sim_net::RemoteDriver;
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
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

    /// Takes the shared field's write-ownership under `owner` — the
    /// fencing claim every promotion runs before the gate lifts, so the
    /// field itself refuses a superseded owner's writes. A purely local
    /// simulated model has no shared field to claim and answers `Ok`.
    fn claim_writer(&self, owner: u64) -> Result<(), String> {
        match self {
            Self::Remote(remote) => remote
                .claim_writer(owner)
                .map_err(|error| format!("plant write-ownership claim failed: {error}")),
            Self::Local(fanout) => fanout
                .claim_field_writer(owner)
                .map_err(|error| format!("plant write-ownership claim failed: {error}")),
        }
    }

    /// The field-facing devices that cannot arbitrate a single writer —
    /// automatic failover is honest only when this is empty: a fenced
    /// old peer's writes must actually stop at the field. A `--remote`
    /// attachment always arbitrates through the plant server's claim.
    fn unfenced_field_devices(&self) -> Vec<String> {
        match self {
            Self::Remote(_) => Vec::new(),
            Self::Local(fanout) => fanout
                .unfenced_field_devices()
                .iter()
                .map(|device| device.0.to_string())
                .collect(),
        }
    }
}

/// This process's field-ownership token — the identity its promotions
/// claim the shared plant's single-writer arbitration under. One token
/// per process: every attachment this instance owns claims it, so all
/// its field connections keep writing, while a peer's takeover claims
/// its own fresh token and fences this one out.
fn owner_token() -> u64 {
    use std::hash::{BuildHasher, Hasher};
    let mut hasher = std::collections::hash_map::RandomState::new().build_hasher();
    hasher.write_u32(std::process::id());
    if let Ok(since) = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH) {
        hasher.write_u128(since.as_nanos());
    }
    hasher.finish()
}

/// Parsed command line.
struct Options {
    /// The plant model document to load.
    model: PathBuf,
    /// Assemble and report without running a scan — the engineering
    /// compile-check mode.
    check: bool,
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
    /// The consecutive checkpoint-pull misses after which a tracking
    /// standby self-promotes — `None` keeps promotion manual-only.
    auto_promote: Option<u32>,
    /// Persist the run's checkpoint to this file at the end of every
    /// scan cycle, and resume from it at startup when it exists — the
    /// restart-recovery path for a controller with no redundant peer.
    state_file: Option<PathBuf>,
    /// Persist the transition journal to this append-only file and
    /// replay it at startup — the run's audit record surviving a
    /// restart. Requires `--listen`: the journal's recorder lives in
    /// the monitor.
    journal_file: Option<PathBuf>,
}

const USAGE: &str = "\
Usage: dcs-controller <model-file> [--check] [--ticks N] [--scan-ms MS]
                      [--dt T] [--listen ADDR] [--standby ADDR]
                      [--remote ADDR] [--driven] [--auto-promote N]
                      [--state-file PATH] [--journal-file PATH]

Loads and validates the plant model, resolves its devices through the
driver registry (local `sim*` and remote `sim-tcp` kinds), and runs the
controller scan.

  --check         assemble the model without running it: load, validate,
                  resolve devices, and construct components through the
                  standard registries, then print what assembled and
                  exit; no scan runs and no listener binds. Run-mode
                  options do not apply
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
  --auto-promote N
                  arm automatic failover on a tracking standby: N
                  consecutive failed checkpoint pulls self-promote the
                  standby at that scan boundary. Requires the model's
                  field-facing devices to arbitrate a single writer —
                  sim-tcp does through the plant server's claim
  --state-file PATH
                  persist the run's checkpoint to PATH at the end of
                  every scan cycle — atomically, by write-then-rename —
                  and resume from it at startup when it exists: a file
                  that cannot be resumed (unreadable, unparseable, an
                  unsupported format version, or a fingerprint/structural
                  mismatch with the loaded model) exits nonzero naming
                  the reason; a missing file is a cold start
  --journal-file PATH
                  persist the transition journal to PATH — one
                  line-delimited JSON record per journaled entry — and
                  replay it at startup, seeding the served ring and
                  continuing seq numbering across a restart; a
                  run-boundary marker separates process lifetimes, a
                  corrupt record exits nonzero naming it, and a missing
                  file is a cold start. Requires --listen
  -h, --help      show this text

With neither --ticks nor --scan-ms, a paced run at 100 ms is assumed.
A remote-attached standby is output-quiescent behind a write gate until
POST /promote lifts it; a demoted remote active is re-quiesced the same
way, so exactly one peer writes the shared plant.";

impl Options {
    fn parse(args: impl Iterator<Item = String>) -> Result<Self, String> {
        let mut model = None;
        let mut check = false;
        let mut ticks = None;
        let mut scan_ms = None;
        let mut dt = None;
        let mut listen = None;
        let mut standby = None;
        let mut remote = None;
        let mut driven = false;
        let mut auto_promote = None;
        let mut state_file = None;
        let mut journal_file = None;
        let mut args = args;
        while let Some(arg) = args.next() {
            let mut value = |flag: &str| {
                args.next()
                    .ok_or_else(|| format!("{flag} requires a value"))
            };
            match arg.as_str() {
                "--check" => check = true,
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
                "--auto-promote" => {
                    auto_promote = Some(
                        value("--auto-promote")?
                            .parse::<u32>()
                            .map_err(|error| format!("invalid --auto-promote value: {error}"))?,
                    );
                }
                "--state-file" => state_file = Some(PathBuf::from(value("--state-file")?)),
                "--journal-file" => {
                    journal_file = Some(PathBuf::from(value("--journal-file")?));
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
        if check {
            // Check mode assembles and reports; it runs no scan and
            // binds no listener, so the run-mode options have no meaning
            // and are rejected rather than silently ignored.
            let mut rejected = Vec::new();
            for (flag, present) in [
                ("--ticks", ticks.is_some()),
                ("--scan-ms", scan_ms.is_some()),
                ("--dt", dt.is_some()),
                ("--listen", listen.is_some()),
                ("--standby", standby.is_some()),
                ("--remote", remote.is_some()),
                ("--driven", driven),
                ("--auto-promote", auto_promote.is_some()),
                ("--state-file", state_file.is_some()),
                ("--journal-file", journal_file.is_some()),
            ] {
                if present {
                    rejected.push(flag);
                }
            }
            if !rejected.is_empty() {
                return Err(format!(
                    "--check assembles the model without scanning or serving; {} do not apply",
                    rejected.join(", ")
                ));
            }
        }
        if !check && !driven && ticks.is_none() && scan_ms.is_none() {
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
        if auto_promote == Some(0) {
            return Err("--auto-promote must be at least one missed pull".to_string());
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
        if journal_file.is_some() && listen.is_none() {
            return Err(
                "--journal-file requires --listen: the transition journal lives in the monitor"
                    .to_string(),
            );
        }
        Ok(Self {
            model,
            check,
            ticks,
            scan_ms,
            dt,
            listen,
            standby,
            remote,
            driven,
            auto_promote,
            state_file,
            journal_file,
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

/// The `--state-file` resume half: when `path` names an existing file it
/// must hold a [`Checkpoint`] this run can take over — applied in place
/// to the freshly assembled `executor` before the first scan, so the run
/// continues at the checkpointed tick. A missing file is a cold start
/// (`Ok(false)`); anything else that cannot resume — an unreadable file,
/// contents that are not a checkpoint, or a [`RestoreError`] naming the
/// version, fingerprint, or structural mismatch — fails the start, per
/// the checkpoint-restore decision's all-or-nothing rule: never
/// silently fresh over a state file that exists but cannot be resumed.
fn resume_state_file(path: &Path, executor: &mut Executor<'_>) -> Result<bool, String> {
    let body = match std::fs::read(path) {
        Ok(body) => body,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(false),
        Err(error) => {
            return Err(format!(
                "cannot read state file {}: {error}",
                path.display()
            ));
        }
    };
    let checkpoint: Checkpoint = serde_json::from_slice(&body).map_err(|error| {
        format!(
            "state file {} does not hold a checkpoint: {error}",
            path.display()
        )
    })?;
    executor
        .apply(&checkpoint)
        .map_err(|error| format!("cannot resume from state file {}: {error}", path.display()))?;
    Ok(true)
}

/// Persists `checkpoint` as `path`'s new contents: write to a sibling
/// temporary file, then rename over `path` — atomic on one filesystem,
/// so a crash mid-write never leaves a torn file the next resume would
/// have to reject.
fn write_state_file(path: &Path, checkpoint: &Checkpoint) -> Result<(), String> {
    let body = serde_json::to_vec(checkpoint)
        .map_err(|error| format!("cannot serialize checkpoint: {error}"))?;
    let mut temporary = path.as_os_str().to_os_string();
    temporary.push(".tmp");
    let temporary = PathBuf::from(temporary);
    std::fs::write(&temporary, body)
        .and_then(|()| std::fs::rename(&temporary, path))
        .map_err(|error| format!("cannot write state file {}: {error}", path.display()))
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

    // The compile-check mode: driver resolution and assembly run exactly
    // as they do below — the same standard registries, the same named
    // failures — then the run stops at the summary. No gate, no peer, no
    // scan, no listener.
    if options.check {
        return match dcs_controller::check(&model) {
            Ok(report) => {
                println!("check ok: {}", options.model.display());
                print!("{report}");
                ExitCode::SUCCESS
            }
            Err(error) => fail(error),
        };
    }

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
    let mut executor = match assemble(&model, &registry(), io) {
        Ok(executor) => executor,
        Err(error) => return fail(error),
    };

    // Automatic failover needs field devices that can fence a superseded
    // owner — without single-writer arbitration a self-promoted standby
    // could not keep exactly one writer, so the flag is refused up
    // front. Manual promotion stays available either way.
    if options.auto_promote.is_some() {
        let unfenced = driver.unfenced_field_devices();
        if !unfenced.is_empty() {
            return fail(format!(
                "--auto-promote requires every field-facing device to arbitrate a single \
                 writer; these devices cannot: {}",
                unfenced.join(", ")
            ));
        }
    }

    // The --state-file resume half: an existing file holds the run's
    // last persisted checkpoint, applied to the fresh executor before
    // the first scan — the restarted process then continues the
    // interrupted run at the checkpointed tick.
    if let Some(path) = &options.state_file {
        match resume_state_file(path, &mut executor) {
            Ok(true) => eprintln!(
                "resumed from state file {} at tick {}",
                path.display(),
                executor.tick().0
            ),
            Ok(false) => {}
            Err(error) => return fail(error),
        }
    }

    // The role machine: a --standby instance tracks its active's
    // checkpoints gate-closed until promoted; anything else owns the
    // field from the start. Every promotion — manual or the
    // `--auto-promote` failover — first takes the field's
    // write-ownership claim under this instance's token, so the shared
    // plant itself refuses a superseded peer's writes.
    let owner = owner_token();
    let peer = match &options.standby {
        Some(_) => Peer::standby(executor, gate.as_ref()),
        None => Peer::active(executor, gate.as_ref()),
    };
    let peer = peer.with_field_claim(|| driver.claim_writer(owner));
    let peer = match options.auto_promote {
        Some(budget) => peer.with_failover(budget),
        None => peer,
    };

    // The simulated process time per scan: explicit --dt, else the
    // wall-clock period in seconds, else one unit per unpaced tick.
    let dt = options
        .dt
        .or_else(|| options.scan_ms.map(|ms| ms as f64 / 1000.0))
        .unwrap_or(1.0);
    let period = options.scan_ms.map(Duration::from_millis);

    // The monitor's recorder configuration: default retention bounds,
    // plus the durable journal-file sink --journal-file names.
    let monitor_config = || MonitorConfig {
        journal_file: options.journal_file.clone(),
        ..MonitorConfig::default()
    };

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
        let monitor =
            match Monitor::bind_peer_with(addr, peer, model.signal_index(), monitor_config()) {
                Ok(monitor) => monitor,
                Err(error) => {
                    return fail(format!("cannot bind monitor on {addr}: {error}"));
                }
            };
        let monitor = monitor.driven(Driven {
            track,
            after_scan: Some(Box::new(|peer: &Peer<'_>| {
                // The scan cycle's plant step, then the state-file
                // write at the same end-of-cycle boundary the paced
                // loop persists at.
                driver.step(dt, peer.owns_field())?;
                if let Some(path) = &options.state_file {
                    write_state_file(path, &peer.checkpoint())?;
                }
                Ok(())
            })),
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
                let monitor = match Monitor::bind_paced_peer_with(
                    addr.as_str(),
                    peer,
                    model.signal_index(),
                    monitor_config(),
                ) {
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
                            // The heartbeat miss reached the configured
                            // budget: a still-converged standby promotes
                            // itself at this boundary; a refusal leaves
                            // the named convergence state reporting on
                            // GET /role.
                            if monitor.failover_due() {
                                match monitor.self_promote() {
                                    Ok(report) => eprintln!(
                                        "standby: {active_addr} unreachable; self-promoted (role {})",
                                        report.role
                                    ),
                                    Err(error) => eprintln!(
                                        "standby: failover due but self-promotion refused: {error}"
                                    ),
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
                // Without a monitor nothing external can promote this
                // standby — only the armed failover path can — and the
                // RefCell lets the two loop closures share the peer.
                let peer = std::cell::RefCell::new(peer);
                let step = || driver.step(dt, peer.borrow().owns_field());
                scan_loop(
                    || {
                        let mut peer = peer.borrow_mut();
                        if !peer.owns_field() {
                            match client.checkpoint() {
                                Ok(checkpoint) => {
                                    if let Err(error) = peer.apply(&checkpoint) {
                                        eprintln!(
                                            "standby: rejected checkpoint from {active_addr}: {error}"
                                        );
                                    }
                                    for report in peer.take_divergences() {
                                        eprintln!(
                                            "standby: staged outputs diverged from the field at tick {}: {:?}",
                                            report.tick.0, report.mismatches
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
                            if peer.failover_due() {
                                match peer.self_promote() {
                                    Ok(()) => eprintln!(
                                        "standby: {active_addr} unreachable; self-promoted"
                                    ),
                                    Err(error) => eprintln!(
                                        "standby: failover due but self-promotion refused: {error}"
                                    ),
                                }
                            }
                        }
                        peer.scan()
                    },
                    || peer.borrow().snapshot(),
                    || peer.borrow().checkpoint(),
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
                let monitor = match Monitor::bind_paced_peer_with(
                    addr.as_str(),
                    peer,
                    model.signal_index(),
                    monitor_config(),
                ) {
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
                    || peer.borrow().checkpoint(),
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
            || monitor.checkpoint(),
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
/// `checkpoint` feeds `--state-file`: the run's transferable state is
/// persisted at the end of every completed scan cycle — after the scan
/// and the plant step, so a resumed run re-enters the loop at exactly
/// this point — and a write failure fails the run like a scan or step
/// failure does: a controller that cannot persist its recovery state
/// exits naming the file rather than running on without it.
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
    checkpoint: impl Fn() -> Checkpoint,
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
        if let Some(path) = &options.state_file
            && let Err(error) = write_state_file(path, &checkpoint())
        {
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
