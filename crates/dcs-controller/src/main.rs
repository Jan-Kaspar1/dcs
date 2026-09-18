//! `dcs-controller`: loads a plant model, resolves its devices through the
//! driver registry — local `sim*` devices plus remote `sim-tcp` ones —
//! assembles the executor against the resulting fan-out driver, and runs
//! the deterministic scan — as the active instance, or as a standby
//! tracking an active peer's checkpoints.
//!
//! Usage: `dcs-controller <model-file> [--check] [--ticks N]
//!         [--scan-ms MS] [--dt T] [--listen ADDR] [--standby ADDR]
//!         [--remote ADDR] [--driven] [--auto-promote N]
//!         [--owner-token N] [--state-file PATH] [--journal-file PATH]`
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
//! write-then-rename so a crash mid-write cannot leave a torn file.
//! A second write boundary keeps the command path honest: `POST
//! /command`'s accepted admission persists the just-captured
//! checkpoint — receipt log included — before the `200` answers, so a
//! restart between admission and the applying scan re-queues the
//! carried `Accepted` receipt instead of losing the command with no
//! audit trace, the same guarantee the checkpoint contract gives a
//! promoted standby.
//! When `PATH` exists at startup the run resumes from it: the checkpoint is
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
//! deterministically — and serves its own monitor at the second. The
//! fetch runs on a dedicated pull thread ([`CheckpointPuller`]), each
//! scan cycle consuming the latest completed pull non-blockingly: an
//! unreachable or wedged active stalls neither the scan cadence nor
//! the monitor's request serving, and a cycle whose pull produced no
//! checkpoint is the heartbeat miss the failover budget counts. Each
//! pull also announces the pulling monitor's own address
//! (`GET /checkpoint?peer=`), so the serving instance learns where its
//! successor lives. There,
//! `GET /role` reports `standby` plus its convergence and
//! `POST /promote` is the operator's switchover action: the gate lifts
//! at the request's scan boundary, the next scan writes what the
//! checkpointed run would have, and the instance starts stepping the
//! shared plant. The documented switchover order — `POST /demote` on
//! the old active first — keeps exactly one peer writing the field.
//! A standby-local `SimDriver` needs no gate: its plant is a private
//! tracking copy every checkpoint's driver section resynchronizes.
//!
//! Demotion is the launch asymmetry the follow-peer half of the
//! tracking contract closes: a launched active never named a peer —
//! `--standby` is the only peer address the CLI used to take — yet a
//! `POST /demote` turns it into a standby that must track *something*
//! or strand `unsynchronized` and unpromotable forever. The demoted
//! peer's checkpoint source is therefore resolved per scan cycle: the
//! configured `--peer ADDR` when given — "active now, but here is my
//! peer for later" — else the address the tracking peer announced
//! through its pulls. Either way the demoted instance pulls, applies,
//! and reconverges like any standby, and a later `POST /promote`
//! fails back without a restart. A field owner with neither — nothing
//! configured and no peer ever announced — refuses `POST /demote`
//! outright (`no_tracking_source`) rather than silently marooning
//! itself.
//!
//! The field's single-writer claim is taken at every transition into
//! field ownership — a promotion, and a launched active's startup:
//! `Peer::active` claims the shared plant's write arbitration before
//! the gate lifts, so the field is fenced for this owner from the
//! first scan rather than open to every attachment until the first
//! promotion. Because the claim preempts unconditionally and outlives
//! a dead holder, the startup activation is deliberately the run's
//! last local step — journal replay, monitor bind, and peer-address
//! resolution all run first, so a process that cannot finish starting
//! never leaves a stale claim fencing the field's standing owner. The
//! claim rides under a per-process owner token —
//! `--owner-token N` pins it when an external attachment must share the
//! owner's claim (a test harness driving plant stimuli); otherwise a
//! fresh token is generated per process. Pinning a second *controller*
//! to the same token is a misconfiguration: both instances' claims
//! succeed — the field cannot tell a same-owner attachment from a peer
//! reusing the token — but the plant server flags each shared grant
//! `claimed_shared` and this instance warns, because two controllers on
//! one token both write and step, defeating the single-writer fencing
//! promotion relies on. A claim the field refuses —
//! or a launch that cannot reach it — fails startup with the named
//! `FieldClaimFailed`. And a claim preempted mid-run — a rogue
//! `claim_writer`, or a promote posted before the old peer was demoted
//! — demotes the superseded owner at its first fenced write: the gate
//! re-closes and the reported role settles to `standby`, with
//! `field_claim_lost` and the role changes journaled — a fenced active
//! degrades instead of exiting, so a misordered promotion or a
//! restarted superseded process cannot crash-loop the pair.
//!
//! Rolling a revised plant model into production, per the rolling
//! model-revision decision: start the standby with `--revised` against
//! the revised model document. Its fingerprint differs by design, so
//! each pulled checkpoint crosses the model boundary under the
//! documented carryover rule — operator-writable internal points matched
//! by declared identity carry their last values, component state
//! reinitializes — and `GET /role` reports the named `reinitialized`
//! state carrying the carryover report: what transferred, what
//! initialized fresh, and every dropped element named. A checkpoint the
//! rule cannot carry is rejected before promotion with a named error and
//! the peer reports `degraded`; the old active keeps the field. The same
//! `POST /demote`-then-`POST /promote` order then moves the field writer
//! to the revised model at a scan boundary.
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
//! The one active-loss case the pair cannot heal itself, per the
//! dead-active recovery decision: the active dies holding the field
//! claim while its standby is not converged — `POST /promote` answers
//! `not_converged` and no checkpoint will ever arrive to change that.
//! The recorded recovery is restart-as-active: relaunch the controller
//! on the same model without `--standby`, and the launched active's
//! unconditional startup claim preempts the dead owner's token — a
//! surviving `--state-file` resumes the run at its last persisted
//! cycle, and the standby reconverges on the new active's checkpoint
//! stream where its tracking source resolves. There is deliberately no
//! force-promote and no operator claim-release: a standby that never
//! proved it tracks the field is never a writer.
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

use dcs_assembly::{DriverRegistry, FanoutDriver, StepError, assemble, resolve_drivers};
use dcs_controller::registry;
use dcs_core::{IoDriver, TelemetrySnapshot, Tick};
use dcs_model::PlantModel;
use dcs_monitor::{CheckpointPuller, CommandPersist, Driven, Monitor, MonitorConfig};
use dcs_runtime::{Checkpoint, Executor, Peer, TrackReport, WriteGate};
use dcs_sim_net::{ClaimGrant, RemoteDriver, RemoteError};
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
    ///
    /// Field trouble is not a run failure: a dead or fenced attachment
    /// degrades the cycle — the scan's io_health already counted its
    /// boundary failures — and the run continues so the monitor keeps
    /// serving the outage. Only a refused step — a malformed `dt` — is
    /// a run defect and stays fatal.
    fn step(&self, dt: f64, owns_field: bool) -> Result<(), String> {
        match (self, owns_field) {
            (Self::Local(fanout), true) => degrade_step(fanout.step(dt)),
            (Self::Local(fanout), false) => degrade_step(fanout.step_local(dt)),
            (Self::Remote(remote), true) => match remote.step(dt) {
                Ok(_) => Ok(()),
                Err(RemoteError::InvalidRequest(detail)) => {
                    Err(format!("plant step failed: {detail}"))
                }
                Err(error) => {
                    eprintln!("plant step degraded: {error}");
                    Ok(())
                }
            },
            (Self::Remote(_), false) => Ok(()),
        }
    }

    /// Forgets this instance's recorded field-ownership claim — the
    /// demotion counterpart of [`claim_writer`](Self::claim_writer):
    /// after it, a re-attaching field driver does not re-arm a claim
    /// this peer gave up, so a restarted plant's empty arbitration stays
    /// free for the peer that legitimately owns the field.
    fn release_claim(&self) {
        match self {
            Self::Remote(remote) => remote.release_claim(),
            Self::Local(fanout) => fanout.release_field_claims(),
        }
    }

    /// Whether this driver's surface reaches the shared field — a
    /// remote attachment or a fan-out declaring field-facing devices —
    /// so its write-ownership claim means something.
    fn has_shared_field(&self) -> bool {
        match self {
            Self::Remote(_) => true,
            Self::Local(fanout) => fanout.has_field_backend(),
        }
    }

    /// Takes the shared field's write-ownership under `owner` — the
    /// fencing claim every promotion runs before the gate lifts, so the
    /// field itself refuses a superseded owner's writes. A purely local
    /// simulated model has no shared field to claim and answers `Ok`.
    ///
    /// A grant the field flags `Shared` still holds — the token cannot
    /// tell this instance's own second attachment from a peer process
    /// pinned to the same token — but the sharing is warned about: two
    /// controllers on one `--owner-token` both write and step, silently
    /// defeating the arbitration this claim exists to provide.
    fn claim_writer(&self, owner: u64) -> Result<(), String> {
        match self {
            Self::Remote(remote) => match remote.claim_writer(owner) {
                Ok(ClaimGrant::Exclusive) => Ok(()),
                Ok(ClaimGrant::Shared) => {
                    eprintln!(
                        "warning: field write-ownership claim for owner token {owner} is \
                         shared with another live attachment — expected only for a \
                         deliberate same-owner attachment; a second controller pinned to \
                         the same --owner-token defeats single-writer fencing"
                    );
                    Ok(())
                }
                Err(error) => Err(format!("plant write-ownership claim failed: {error}")),
            },
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

/// A local backend step result under the same rule [`Driver::step`]
/// applies to the remote attachment: a field failure — the backend
/// answered [`StepError::Backend`], or a cross-backend wire's I/O fault
/// [`StepError::Route`] — degrades the cycle instead of failing the run;
/// anything else is a refusal and stays fatal.
fn degrade_step(stepped: Result<(), StepError>) -> Result<(), String> {
    match stepped {
        Err(StepError::Backend { backend, detail }) => {
            eprintln!("plant step degraded on {backend}: {detail}");
            Ok(())
        }
        Err(StepError::Route(error)) => {
            eprintln!("plant step degraded on a cross-backend wire: {error}");
            Ok(())
        }
        Err(error) => Err(format!("plant step failed: {error}")),
        Ok(()) => Ok(()),
    }
}

/// Reports the field-ownership claim a launched active's deferred
/// startup activation just took — the line every field-owning startup
/// logs once the claim holds.
fn report_claim(driver: &Driver, owner: u64) {
    if driver.has_shared_field() {
        eprintln!("field write-ownership claim held under owner token {owner}");
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
    /// Run as the active, but name the peer this instance tracks if it
    /// is later demoted — "active now, but here is my peer for later".
    /// Mutually exclusive with `--standby`.
    peer: Option<String>,
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
    /// This standby's model is a deliberate revision: a pulled
    /// checkpoint carrying a different fingerprint crosses the model
    /// boundary through the documented carryover rule instead of
    /// degrading on the mismatch.
    revised: bool,
    /// Persist the run's checkpoint to this file at the end of every
    /// scan cycle and at each accepted command's admission boundary,
    /// and resume from it at startup when it exists — the
    /// restart-recovery path for a controller with no redundant peer.
    state_file: Option<PathBuf>,
    /// Persist the transition journal to this append-only file and
    /// replay it at startup — the run's audit record surviving a
    /// restart. Requires `--listen`: the journal's recorder lives in
    /// the monitor.
    journal_file: Option<PathBuf>,
    /// Pin this instance's field-ownership token instead of generating
    /// a fresh per-process one — so an external attachment can claim
    /// under the same token and share the owner's field access (a test
    /// harness driving plant stimuli through its own sim-net
    /// connection). Pinning a second controller to the same token is a
    /// misconfiguration the plant server flags `claimed_shared` and
    /// this instance warns about.
    owner_token: Option<u64>,
}

const USAGE: &str = "\
Usage: dcs-controller <model-file> [--check] [--ticks N] [--scan-ms MS]
                      [--dt T] [--listen ADDR] [--standby ADDR]
                      [--peer ADDR] [--remote ADDR] [--driven]
                      [--auto-promote N] [--owner-token N] [--revised]
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
  --peer ADDR     run as the active, but name the peer's monitoring
                  address this instance tracks if it is later demoted
                  — so a demoted active reconverges and stays
                  promotable. Mutually exclusive with --standby;
                  requires --listen
  --revised       declare this standby's model a deliberate revision of
                  the active's: a pulled checkpoint whose model
                  fingerprint differs crosses the boundary under the
                  documented carryover rule — operator-writable internal
                  points matched by declared identity carry their last
                  values, component state reinitializes — and the peer
                  reports reinitialized, promotable in place of tracking;
                  a checkpoint breaking the rule is rejected with a named
                  error before promotion. Requires --standby
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
                  sim-tcp does through the plant server's claim, sim-bus
                  through the device server's
  --owner-token N
                  pin this instance's field-ownership token to N instead
                  of generating a fresh per-process one — so an external
                  attachment claiming under the same token shares the
                  owner's field access (a test harness driving plant
                  stimuli through its own sim-net connection). Never pin
                  two controllers to the same token: both would write and
                  step the shared plant, defeating single-writer fencing;
                  the plant server flags such duplicate-owner claims and
                  this instance warns on a shared grant
  --state-file PATH
                  persist the run's checkpoint to PATH at the end of
                  every scan cycle and at each accepted command's
                  admission boundary — atomically, by write-then-rename —
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
        let mut peer = None;
        let mut remote = None;
        let mut driven = false;
        let mut auto_promote = None;
        let mut revised = false;
        let mut state_file = None;
        let mut journal_file = None;
        let mut owner_token = None;
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
                "--peer" => peer = Some(value("--peer")?),
                "--remote" => remote = Some(value("--remote")?),
                "--driven" => driven = true,
                "--revised" => revised = true,
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
                "--owner-token" => {
                    owner_token = Some(
                        value("--owner-token")?
                            .parse::<u64>()
                            .map_err(|error| format!("invalid --owner-token value: {error}"))?,
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
                ("--peer", peer.is_some()),
                ("--remote", remote.is_some()),
                ("--driven", driven),
                ("--auto-promote", auto_promote.is_some()),
                ("--revised", revised),
                ("--state-file", state_file.is_some()),
                ("--journal-file", journal_file.is_some()),
                ("--owner-token", owner_token.is_some()),
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
        if revised && standby.is_none() {
            return Err(
                "--revised requires --standby: only a tracking peer rolls a revised model"
                    .to_string(),
            );
        }
        if peer.is_some() {
            if standby.is_some() {
                return Err(
                    "--peer names the tracking peer of a launched active; it does not \
                     combine with --standby, which already runs as the tracking peer"
                        .to_string(),
                );
            }
            if listen.is_none() {
                return Err(
                    "--peer requires --listen: the demotion it answers and the tracking \
                     announcements live on the monitor"
                        .to_string(),
                );
            }
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
            peer,
            remote,
            driven,
            auto_promote,
            revised,
            state_file,
            journal_file,
            owner_token,
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

/// The `--state-file` persist hooked onto command admission: an
/// accepted command is durable run state before its `200` receipt
/// answers — the checkpoint's receipt log carries the admission, so a
/// restart between admission and the applying scan re-queues it rather
/// than losing it unaudited. This is the same write the cycle end
/// performs, fired at the admission boundary; every write rides the
/// monitor's shared lock so the two can never interleave into a stale
/// overwrite.
fn command_persist(options: &Options) -> Option<CommandPersist> {
    let path = options.state_file.clone()?;
    Some(Box::new(move |checkpoint| {
        write_state_file(&path, checkpoint)
    }))
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
    // field from the start. The field's write-ownership claim is taken
    // under this instance's token at every transition into field
    // ownership — a launched active's startup activation, and every
    // promotion — so the shared plant itself refuses every attachment
    // not holding the claim. The startup activation is deliberately
    // deferred to the run's last local step: the claim preempts
    // unconditionally and outlives a dead holder, so it runs only after
    // every fallible startup step — journal replay, monitor bind,
    // peer-address resolution — has proven this process can serve; a
    // starter that fails earlier leaves no stale claim fencing the
    // field's standing owner.
    let owner = options.owner_token.unwrap_or_else(owner_token);
    let peer = match &options.standby {
        Some(_) => Peer::standby(executor, gate.as_ref()),
        None => Peer::active(executor, gate.as_ref()),
    };
    let peer = peer
        .with_field_claim(|| driver.claim_writer(owner))
        .with_field_release(|| driver.release_claim());
    let peer = match options.auto_promote {
        Some(budget) => peer.with_failover(budget),
        None => peer,
    };
    // A --revised standby declared its model a deliberate revision of
    // the active's: the pull path routes a foreign-fingerprint
    // checkpoint through the documented carryover rule rather than
    // degrading on the mismatch the fingerprint gate would otherwise
    // report.
    let mut peer = match options.revised {
        true => peer.with_revision(),
        false => peer,
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
        let track = match options.standby.as_deref().or(options.peer.as_deref()) {
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
        let monitor = match command_persist(&options) {
            Some(persist) => monitor.with_command_persist(persist),
            None => monitor,
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
        // A launched active owns the field from startup: activation
        // runs the same claim-then-lift sequence a promotion does —
        // the plant's single-writer claim under this instance's token
        // first, the gate second — deferred to here, after every
        // fallible local startup step (the track address resolved, the
        // journal replayed, the monitor bound), so a starter that
        // cannot serve never lands the preemptive claim on the field's
        // standing owner. A claim the field refuses is a named startup
        // failure, not an unfenced run.
        if options.standby.is_none() {
            if let Err(error) = monitor.activate() {
                return fail(format!("{error}"));
            }
            report_claim(&driver, owner);
        }
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
                let monitor = match command_persist(&options) {
                    Some(persist) => monitor.with_command_persist(persist),
                    None => monitor,
                };
                // The promotion boundary runs one final pull against the
                // tracking source, so a command the active admitted up
                // to the promote request is carried.
                let monitor = monitor.with_standby_source(active_addr);
                eprintln!("listening on {}", monitor.local_addr());
                let step = || driver.step(dt, monitor.owns_field());
                let mut puller = None;
                run_monitored(
                    &monitor,
                    || tracked_cycle(&monitor, &mut puller),
                    step,
                    &options,
                    period.unwrap(),
                )
            }
            None => {
                // Without a monitor nothing external can promote this
                // standby — only the armed failover path can — and the
                // RefCell lets the two loop closures share the peer.
                // There is also no monitor address to announce on the
                // pulls — the serving peer could not track this one
                // back anyway, since a monitorless standby serves no
                // checkpoint endpoint.
                let mut puller = CheckpointPuller::new(active_addr, None);
                let peer = std::cell::RefCell::new(peer);
                let step = || driver.step(dt, peer.borrow().owns_field());
                scan_loop(
                    || {
                        let mut peer = peer.borrow_mut();
                        // The same tracking cycle the monitored loop
                        // runs through `track_cycle`, here directly on
                        // the peer; without a recorder the transition
                        // queues drain into the log instead. The pull
                        // consumes the fetch worker's latest result —
                        // the network wait itself runs off the scan
                        // cycle's critical path.
                        let report = peer.track_once(|| puller.poll());
                        report_tracking(&report, active_addr);
                        for divergence in peer.take_divergences() {
                            eprintln!(
                                "standby: staged outputs diverged from the field at tick {}: {:?}",
                                divergence.tick.0, divergence.mismatches
                            );
                        }
                        for resolution in peer.take_resolutions() {
                            eprintln!(
                                "standby: divergence resolved at tick {} — compared {:?}",
                                resolution.tick.0, resolution.compared
                            );
                        }
                        for reinitialized in peer.take_reinitializations() {
                            eprintln!("standby: {reinitialized}");
                        }
                        for restart in peer.take_source_restarts() {
                            eprintln!(
                                "standby: checkpoint stream regressed at tick {} — the source restarted or was replaced; resumed from its tick {} (was aligned to {:?})",
                                restart.tick.0,
                                restart.resumed_at.0,
                                restart.was_aligned.map(|tick| tick.0)
                            );
                        }
                        for change in peer.take_role_changes() {
                            eprintln!(
                                "standby: role {} -> {} at tick {}",
                                change.from, change.to, change.tick.0
                            );
                        }
                        let scanned = peer.scan();
                        // Transitions the scan itself produced — a
                        // fenced write's claim loss and the demotion it
                        // drove — log at the boundary they happened,
                        // not a cycle late.
                        for change in peer.take_role_changes() {
                            eprintln!(
                                "standby: role {} -> {} at tick {}",
                                change.from, change.to, change.tick.0
                            );
                        }
                        for loss in peer.take_fencing_losses() {
                            eprintln!(
                                "standby: field write-ownership claim lost at tick {}: {:?} fenced",
                                loss.tick.0, loss.point
                            );
                        }
                        scanned
                    },
                    || peer.borrow().snapshot(),
                    |path| write_state_file(path, &peer.borrow().checkpoint()),
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
                let monitor = match command_persist(&options) {
                    Some(persist) => monitor.with_command_persist(persist),
                    None => monitor,
                };
                // A --peer launched active names its tracking source up
                // front — where this instance pulls checkpoints if it is
                // demoted — ahead of anything a tracking peer announces
                // through its pulls.
                let monitor = match &options.peer {
                    Some(peer) => match resolve(peer) {
                        Ok(peer) => monitor.with_standby_source(peer),
                        Err(error) => return fail(error),
                    },
                    None => monitor,
                };
                // The launched active's deferred startup activation —
                // the same claim-then-lift sequence the driven path
                // runs: the preemptive field claim lands only now, the
                // journal replayed, the monitor bound, and the peer
                // address resolved, so a startup that failed earlier
                // left no stale claim fencing the field's standing
                // owner. A claim the field refuses is a named startup
                // failure, not an unfenced run.
                if let Err(error) = monitor.activate() {
                    return fail(format!("{error}"));
                }
                report_claim(&driver, owner);
                // Announce the bound address — with a port of 0 this is the
                // only way to learn where the monitor listens. Stderr keeps
                // stdout a pure snapshot stream.
                eprintln!("listening on {}", monitor.local_addr());
                // Demotion may re-quiesce this instance mid-run, so the
                // plant step consults the role each scan — and the scan
                // cycle itself tracks a checkpoint source once demoted:
                // the configured --peer, or the address the tracking peer
                // announced through its pulls.
                let step = || driver.step(dt, monitor.owns_field());
                let mut puller = None;
                run_monitored(
                    &monitor,
                    || tracked_cycle(&monitor, &mut puller),
                    step,
                    &options,
                    period.unwrap(),
                )
            }
            None => {
                // The launched active's startup activation — the same
                // claim-then-lift sequence the monitored paths defer to
                // their last startup step: nothing fallible stands
                // between here and the scan loop, so the preemptive
                // claim runs only now that startup can no longer abort.
                if let Err(error) = peer.activate() {
                    return fail(format!("{error}"));
                }
                report_claim(&driver, owner);
                // The RefCell lets the two loop closures share the peer;
                // the loop is single-threaded, so the borrows never
                // overlap. No monitor means no *operator* demotion
                // path, but a write the field fenced still demotes this
                // peer mid-run — the scan closure logs the claim loss
                // and the transition it drove, and the step consults
                // the role each cycle so a demoted peer stops stepping
                // a shared plant it no longer owns.
                let peer = std::cell::RefCell::new(peer);
                scan_loop(
                    || {
                        let mut peer = peer.borrow_mut();
                        let scanned = peer.scan();
                        for loss in peer.take_fencing_losses() {
                            eprintln!(
                                "field write-ownership claim lost at tick {}: {:?} fenced",
                                loss.tick.0, loss.point
                            );
                        }
                        for change in peer.take_role_changes() {
                            eprintln!(
                                "role {} -> {} at tick {}",
                                change.from, change.to, change.tick.0
                            );
                        }
                        scanned
                    },
                    || peer.borrow().snapshot(),
                    |path| write_state_file(path, &peer.borrow().checkpoint()),
                    || driver.step(dt, peer.borrow().owns_field()),
                    || peer.borrow_mut().record_scan_overrun(),
                    &options,
                    period,
                )
            }
        }
    }
}

/// One paced scan cycle behind the monitor: the tracking pull first —
/// while the peer does not own the field and a checkpoint source exists
/// — then the scan itself. The source is re-resolved every cycle:
/// the configured `--standby`/`--peer` target when set, else the monitor
/// address a tracking peer announced through its `?peer=` pulls — the
/// follow-peer half that lets a demoted launched active find its
/// successor without a restart. The puller follows the resolved source,
/// respawning when it changes, and announces this monitor's own address
/// on every pull so the serving peer learns where to track back. A
/// field-owning cycle's [`Monitor::track_cycle`] short-circuits before
/// the pull, so the puller's fetch thread idles until a demotion.
fn tracked_cycle(
    monitor: &Monitor<'_>,
    puller: &mut Option<(SocketAddr, CheckpointPuller)>,
) -> Tick {
    if let Some(source) = monitor.tracking_source() {
        if puller.as_ref().map(|(bound, _)| *bound) != Some(source) {
            *puller = Some((
                source,
                CheckpointPuller::new(source, Some(monitor.local_addr())),
            ));
        }
        let report = monitor.track_cycle(|| puller.as_mut().unwrap().1.poll());
        report_tracking(&report, source);
    }
    monitor.paced_scan()
}

/// The standby loop's presentation half of a tracking cycle: logs what
/// the [`TrackReport`] `Peer::track_once` returned describes — a refused
/// checkpoint, a produced-nothing pull counted as a heartbeat miss, or
/// the failover self-promotion the miss budget triggered (and its named
/// refusal). `active` is the pulled peer's monitoring address.
fn report_tracking(report: &TrackReport, active: SocketAddr) {
    match report {
        TrackReport::OwnsField | TrackReport::Applied(_) => {}
        TrackReport::Refused(error) => {
            eprintln!("standby: rejected checkpoint from {active}: {error}");
        }
        TrackReport::Missed { detail } => eprintln!("standby: {detail}"),
        TrackReport::Promoted { detail, report } => {
            eprintln!("standby: {detail}");
            eprintln!(
                "standby: {active} unreachable; self-promoted (role {})",
                report.role
            );
        }
        TrackReport::PromotionRefused { detail, error } => {
            eprintln!("standby: {detail}");
            eprintln!("standby: failover due but self-promotion refused: {error}");
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
    scan: impl FnMut() -> Tick,
    step: impl Fn() -> Result<(), String>,
    options: &Options,
    period: Duration,
) -> ExitCode {
    std::thread::scope(|scope| {
        scope.spawn(|| monitor.serve());
        let result = scan_loop(
            scan,
            || monitor.snapshot(),
            |path| monitor.persist_state(|checkpoint| write_state_file(path, checkpoint)),
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
/// `persist` feeds `--state-file`: the run's transferable state is
/// persisted at the end of every completed scan cycle — after the scan
/// and the plant step, so a resumed run re-enters the loop at exactly
/// this point — and a write failure fails the run like a step failure
/// does: a controller that cannot persist its recovery state exits
/// naming the file rather than running on without it. On a
/// monitored run the closure routes through
/// [`Monitor::persist_state`], so the cycle-end write and a command's
/// admission-boundary write serialize on the same lock and can never
/// interleave into a stale overwrite.
///
/// `overrun` is the paced loop's feed for the snapshot's
/// `io_health.scan_overruns`: a cycle whose wall-clock elapsed reaches
/// its period is reported through it once. The counter itself lives in
/// the executor — callers wire this closure to
/// `record_scan_overrun` on whichever wrapper they scan through — so
/// wall-clock overrun detection stays out here in the shell and only a
/// count, not a timestamp, enters the tick domain.
fn scan_loop(
    mut scan: impl FnMut() -> Tick,
    snapshot: impl Fn() -> TelemetrySnapshot,
    persist: impl Fn(&Path) -> Result<(), String>,
    step: impl Fn() -> Result<(), String>,
    mut overrun: impl FnMut(),
    options: &Options,
    period: Option<Duration>,
) -> ExitCode {
    let mut scanned = 0_u64;
    loop {
        let started = Instant::now();
        scan();
        if let Err(error) = step() {
            return fail(error);
        }
        if let Some(path) = &options.state_file
            && let Err(error) = persist(path)
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
