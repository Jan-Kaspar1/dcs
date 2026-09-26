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
//! A continuous run — `--scan-ms` without `--ticks` — reports the run's
//! state on stdout as one telemetry-snapshot JSON line per scan. That
//! stream is a bounded consumer of the scan loop (decision 83): each
//! line is handed to a dedicated writer through a 64-line queue, so a
//! consumer that stops draining stdout — an unflushed or filled process
//! pipe — never paces the scan; once the queue saturates, further lines
//! drop under the named `stdout_snapshot_drops` counter, reported on
//! stderr at a doubling rate until the reader drains. The `--ticks`
//! run's single final snapshot keeps a direct print.
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
//! checkpoint-versioning decision — is captured at the end of every
//! completed scan cycle, after the scan and the plant step, and handed
//! to a bounded dedicated writer — a monitored run's `state_sink`, a
//! monitorless run's own [`StateSink`] — which serializes it and
//! replaces `PATH` by write-then-rename in push order, so a slow or
//! stalled disk never lengthens a scan and a crash mid-write cannot
//! leave a torn file. A second write boundary keeps the command path
//! honest: `POST /command`'s accepted admission queues the
//! just-captured checkpoint — receipt log included — and its `200`
//! answers only once that capture has replaced the file, so a restart
//! between admission and the applying scan re-queues the carried
//! `Accepted` receipt instead of losing the command with no audit
//! trace, the same guarantee the checkpoint contract gives a promoted
//! standby.
//! When `PATH` exists at startup the run resumes from it: the checkpoint is
//! applied to the freshly assembled executor before pacing begins, so
//! the next scan continues the interrupted run tick-for-tick. Resume is
//! all-or-nothing, the checkpoint-restore rule — an unreadable or
//! unparseable file, an unsupported format version, or a fingerprint or
//! structural mismatch (a state file captured under a different model)
//! exits nonzero naming the reason rather than silently starting fresh;
//! a missing file is a cold start. A `--revised` run relaxes exactly
//! the fingerprint half of that gate — the lone controller's
//! scheduled-outage roll: a checkpoint captured under a different model
//! crosses the boundary through the documented carryover rule,
//! `Executor::reinitialize` in place of `Executor::apply`, resuming at
//! the checkpointed tick with the carried setpoints, output image, and
//! force set the rule names and the crossing's carryover report printed
//! at startup and journaled when a monitor binds; a checkpoint the
//! rule cannot carry fails startup naming the `CarryoverError`, the
//! file untouched, and a matching fingerprint still resumes ordinarily.
//! The standby path is unchanged — where
//! a redundant peer exists it remains the preferred recovery story, its
//! checkpoint stream converging a standby continuously rather than at
//! the last persisted cycle.
//!
//! `--journal-file PATH` persists the transition journal the monitor
//! records — the journal-persistence decision's durable audit trail:
//! every journaled entry is appended to `PATH` as one line-delimited
//! JSON record in `seq` order, and startup replays the file into the
//! served ring with `seq` numbering continued where it left off, so
//! `GET /journal` answers continuously across a restart. The appends
//! run on a dedicated writer behind a bounded queue (the monitor's
//! `journal_drain_capacity`), so a slow or stalled sink lengthens
//! neither a scan nor the executor lock's hold — the recording point
//! hands each record off without waiting, and a queue that fills past
//! the bound is the run's fatal point, named like every append
//! failure. A sink's lag reads on the snapshot's
//! `publication.journal_sink` health — `healthy`, `lagging`, or
//! `failed` with the accepted/drained/lost accounting — and a request
//! that answers with durable state attests the drain caught up before
//! it responds. A run-boundary marker line separates process
//! lifetimes within one file; a file that cannot be replayed exits
//! nonzero naming the file and the offending record, and a missing
//! file is a cold start. The journal requires `--listen` — the
//! recorder lives in the monitor — and stays deliberately separate
//! from `--state-file`: the checkpoint is overwritten per save and
//! consumed by restore, the journal is append-only and consumed by
//! review; a `--state-file`-resumed run keeps appending to the same
//! journal file in the restored tick domain. The file is
//! single-writer: the monitor bind holds an exclusive advisory lock
//! on the path for the process lifetime, so a second live process
//! pointed at the same `--journal-file` — a misconfiguration that
//! would interleave duplicate `seq`s into an un-replayable record —
//! exits nonzero naming the file and the conflict, while a dead
//! holder's lock releases with its descriptor and a restart
//! re-acquires it.
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
//! successor lives — an announce the serving side accepts only when it
//! names the pulling connection's own source address, resolving the
//! wildcard a `--listen 0.0.0.0` peer announces to that address so the
//! recorded source is always one a demotion could dial. There,
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
//! peer for later" — else, on a `--pair-token` keyed run only, the
//! address the tracking peer announced through its pulls. The
//! announced fallback is a hint, not a proof:
//! the serving side cannot tell the puller's monitor port from any
//! other port its connection's source claims, so `POST /demote`
//! toward an announced-only source first pulls one checkpoint from it
//! and proceeds only when the answer carries the `?prove=` nonce's
//! keyed `line_proof` — the attestation only a peer holding the pair's
//! token produces — *and* that checkpoint continues this run's line in
//! a way this run's own public `/checkpoint` could not have answered —
//! a field-owning document not ahead of this run's tick is replayable,
//! not a successor — journaling the adopted source and pinning it, so
//! a later `?peer=` rewrite cannot redirect the demoted peer's pulls —
//! while a dead, unreachable, unsigned, replayed, or forged hint
//! refuses `no_tracking_source` like an absent one. The announced
//! contract is keyed-only outright: `/checkpoint` is public, so on an
//! unkeyed run every document shape an announced endpoint could serve —
//! the standby's `source_owns_field: false` included — is derivable
//! from this run's own answers and proves nothing about who serves it,
//! and an announced-only demotion refuses whatever the hint would
//! serve; the configured `--peer` remains the unkeyed switchover path.
//! The same scrutiny gates the involuntary demotion a preempted field
//! claim forces: with no `POST /demote` boundary to run it on, the
//! tracking cycle verifies the recorded hints lazily — a dead or
//! foreign announcer loses to the legitimate successor's own proof
//! inside one bounded pass, and a peer with only unproven hints pulls
//! nothing rather than following one verbatim. On a keyed run every
//! checkpoint the adopted source later serves keeps proving under
//! fresh nonces, so an endpoint that merely replays or fabricates this
//! line's checkpoints feeds the demoted peer nothing. Either way the
//! demoted instance pulls, applies, and
//! reconverges like any standby, and a later `POST /promote` fails
//! back without a restart. A field owner with neither — nothing
//! configured and no announced source it can prove — refuses
//! `POST /demote` outright (`no_tracking_source`) rather than silently
//! marooning itself.
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
//! `field_claim_lost` carrying the preempting owner token and the role
//! changes journaled — a fenced active degrades instead of exiting, so
//! a misordered promotion or a restarted superseded process cannot
//! crash-loop the pair. The demotion leaves a fencing-loss mark, and
//! a `standby` peer still carrying it probes a bound conditional
//! re-grant every scan — refused while the preemptor's claim stands,
//! granted once the field frees — so a released rogue claim ends with
//! the ex-owner holding the field again, walking `promoting` back to
//! `active` without an operator call.
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
//! The same arm extends to the no-peer half: `--revised` with
//! `--state-file` and no `--standby` rolls a revised model on a lone
//! controller through the scheduled outage the restart already is —
//! the restarted process resumes its own persisted checkpoint across
//! the model boundary under the same classify-then-apply carryover
//! rule instead of refusing on the fingerprint, so the setpoints,
//! accumulated image, and forces the continuity clause names survive
//! where a cold start would lose them.
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
//! conditional startup grant preempts the dead owner's standing token —
//! a surviving `--state-file` resumes the run at its last persisted
//! cycle, and the standby reconverges on the new active's checkpoint
//! stream where its tracking source resolves. The grant is conditional
//! precisely so the same launch cannot take a *live* incumbent's field:
//! a controller restarting into a pair cannot prove its resumed state is
//! current with the incumbent's, so a live different-owner claim refuses
//! the start — the named remedy is rejoining as `--standby`, whose
//! tracking pulls adopt the incumbent's state rather than reverting it.
//! There is deliberately no force-promote and no operator claim-release:
//! a standby that never proved it tracks the field is never a writer.
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
use dcs_core::{CarryoverReport, FieldClaim, IoDriver, PointId, TelemetrySnapshot, Tick};
use dcs_model::PlantModel;
use dcs_monitor::{
    CheckpointPuller, DEFAULT_STATE_DRAIN_CAPACITY, Driven, Monitor, MonitorConfig, StateSink,
};
use dcs_runtime::{Checkpoint, Executor, Peer, TrackReport, WriteGate, mint_generation};
use dcs_sim_net::{ClaimGrant, RemoteDriver, RemoteError};
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::atomic::Ordering;
use std::sync::{Arc, Mutex, mpsc};
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

    /// Drops this instance's field-ownership hold — the demotion
    /// counterpart of [`claim_writer`](Self::claim_writer): the
    /// attachment forgets the recorded owner so a re-attach does not
    /// re-assert a claim this peer gave up, and the claim itself stays
    /// standing, marked yielded — the field never opens an unclaimed
    /// window, while a successor's conditional claim can still tell
    /// this deliberate step-down from a live incumbent's claim.
    /// Best-effort: a dead plant drops the connection — and the hold
    /// with it — anyway.
    fn release_claim(&self) {
        match self {
            Self::Remote(remote) => {
                let _ = remote.release_writer_keep_claim();
            }
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

    /// The launched-controller counterpart of
    /// [`claim_writer`](Self::claim_writer) — run once at startup
    /// activation, and by an orphaned peer's promotion as the
    /// conditional claim the stale-island rule needs: takes the field's
    /// write-ownership under `owner` only where no live *controller*
    /// attachment holds a different owner's unyielded claim —
    /// `Ok(true)` — answering `Ok(false)` where a live incumbent
    /// stands. A restarted controller cannot prove its resumed state is
    /// current with that incumbent's — a stale `--state-file` would
    /// silently roll back commands the incumbent receipted and applied —
    /// while a claim a dead owner left standing, a deliberately
    /// yielded claim, or a field tool's hold is still preempted: the
    /// restart-as-active recovery path, the demotion hand-off, and the
    /// rogue-claim cleanup the promote recovery relies on. A purely
    /// local simulated model has no shared field to claim and answers
    /// `Ok(true)` vacuously; field kinds that cannot distinguish live
    /// holders fall back to the unconditional claim.
    fn claim_writer_unless_held(&self, owner: u64) -> Result<bool, String> {
        match self {
            Self::Remote(remote) => match remote.claim_writer_unless_held(owner) {
                Ok(ClaimGrant::Exclusive) => Ok(true),
                Ok(ClaimGrant::Shared) => {
                    eprintln!(
                        "warning: field write-ownership claim for owner token {owner} is \
                         shared with another live attachment — expected only for a \
                         deliberate same-owner attachment; a second controller pinned to \
                         the same --owner-token defeats single-writer fencing"
                    );
                    Ok(true)
                }
                Err(RemoteError::Fenced) => Ok(false),
                Err(error) => Err(format!("plant write-ownership claim failed: {error}")),
            },
            Self::Local(fanout) => fanout
                .claim_field_writer_unless_held(owner)
                .map_err(|error| format!("plant write-ownership claim failed: {error}")),
        }
    }

    /// The conditional counterpart of [`claim_writer`](Self::claim_writer)
    /// — the orphan-cycle probe a demoted ex-owner runs while the
    /// tracked line reports no field owner: keeps the released claim
    /// standing under `owner` where the field stands unclaimed or
    /// already names the token — `Ok(true)` — refusing `Ok(false)`
    /// while a different owner stands, so a released claim stays armed
    /// instead of leaving the field open to a foreign grab and no probe
    /// ever preempts. The probe is unbound: it never joins the claim's
    /// holders, so the probing ex-owner cannot read as a live incumbent
    /// to another owner's conditional claim. A purely local simulated
    /// model has no shared field to claim and answers `Ok(true)`
    /// vacuously.
    fn ensure_writer(&self, owner: u64) -> Result<bool, String> {
        match self {
            Self::Remote(remote) => match remote.ensure_writer_unbound(owner) {
                Ok(()) => Ok(true),
                Err(RemoteError::Fenced) => Ok(false),
                Err(error) => Err(format!("plant write-ownership re-arm failed: {error}")),
            },
            Self::Local(fanout) => fanout
                .ensure_field_writer(owner)
                .map_err(|error| format!("plant write-ownership re-arm failed: {error}")),
        }
    }

    /// The read-only half of the field claim — the per-scan observation
    /// the peer reports as `RoleReport::field_claim`: the verdict a
    /// mutation from this instance's attachments would meet, asked
    /// without mutating — `Held` while an owner stands, `Unclaimed`
    /// while none does. A purely local simulated model has no shared
    /// field to arbitrate; its fan-out answers `Err` and the run's last
    /// observation stands.
    fn probe_field_claim(&self) -> Result<FieldClaim, String> {
        match self {
            Self::Remote(remote) => remote
                .probe_writer()
                .map_err(|error| format!("plant write-ownership probe failed: {error}")),
            Self::Local(fanout) => fanout
                .probe_field_claim()
                .map_err(|error| format!("plant write-ownership probe failed: {error}")),
        }
    }

    /// The fencing-loss counterpart of [`ensure_writer`](Self::ensure_writer)
    /// — the *bound* conditional re-grant a fencing-demoted ex-owner
    /// probes each scan while its loss mark stands: takes the field's
    /// write-ownership under `owner` where the field stands unclaimed
    /// or already names the token — `Ok(true)` — answering `Ok(false)`
    /// while a different owner stands, so a still-held preemptor's
    /// claim keeps the field until it releases and the probe never
    /// preempts. Unlike the orphan cycle's unbound probe the grant
    /// joins this instance's attachments to the claim's holders — the
    /// gate the reclaim re-lifts must pass the arbitration it re-took.
    /// A purely local simulated model has no shared field to claim and
    /// answers `Ok(true)` vacuously; a fan-out with no reclaim-capable
    /// field backend answers `Ok(false)` — nothing probed.
    fn reclaim_writer(&self, owner: u64) -> Result<bool, String> {
        match self {
            Self::Remote(remote) => match remote.ensure_writer(owner) {
                Ok(ClaimGrant::Exclusive) => Ok(true),
                Ok(ClaimGrant::Shared) => {
                    eprintln!(
                        "warning: field write-ownership claim for owner token {owner} is \
                         shared with another live attachment — expected only for a \
                         deliberate same-owner attachment; a second controller pinned to \
                         the same --owner-token defeats single-writer fencing"
                    );
                    Ok(true)
                }
                Err(RemoteError::Fenced) => Ok(false),
                Err(error) => Err(format!("plant write-ownership reclaim failed: {error}")),
            },
            Self::Local(fanout) => fanout
                .reclaim_field_writer(owner)
                .map_err(|error| format!("plant write-ownership reclaim failed: {error}")),
        }
    }

    /// The owner token the field's standing claim named the last time
    /// it fenced a mutation from this instance's attachments — the
    /// claimant a superseded field owner's `field_claim_lost` journal
    /// record attributes the preemption to. `None` where no verdict
    /// named a claimant — a driver surface whose fencing answer carries
    /// no owner identity — and the loss then records unattributed.
    fn fencing_claimant(&self, point: PointId) -> Option<u64> {
        match self {
            Self::Remote(remote) => remote.fenced_by(),
            Self::Local(fanout) => fanout.fencing_claimant(point),
        }
    }

    /// The checkpoint endpoint the field's standing claim carried on
    /// the last verdict fencing a mutation on the claim domain `point`
    /// routes to — the address the claim's owner registered for its
    /// checkpoint monitor, attested by the field's own arbitration.
    /// The peer's `field_owner_endpoint` asks it so a fencing-demoted
    /// ex-owner can learn the successor the field named — the tracking
    /// source an unkeyed pair's announced-hint channel cannot
    /// authenticate. `None` where no verdict carried an endpoint.
    fn field_owner_endpoint(&self, point: PointId) -> Option<SocketAddr> {
        match self {
            Self::Remote(remote) => remote.fenced_endpoint(),
            Self::Local(fanout) => fanout.field_owner_endpoint(point),
        }
    }

    /// Declares this instance's checkpoint-monitor port on every claim
    /// the driver surface asserts or re-arms — wired once the monitor's
    /// bound address is known, so the field's claim record names where
    /// this owner serves checkpoints and the verdict fencing a
    /// superseded owner out can carry its successor's endpoint to it.
    fn set_claim_endpoint(&self, port: u16) {
        match self {
            Self::Remote(remote) => remote.set_claim_endpoint(port),
            Self::Local(fanout) => fanout.set_claim_endpoint(port),
        }
    }

    /// The standing-owner tokens the last refused conditional grant
    /// probe — the orphan cycle's `ensure` or the fencing-loss
    /// `reclaim` — named, one per refusing claim domain. The peer's
    /// `field_claim_observed` journal record reads them immediately
    /// after a probe answers `Ok(false)`, so the answer attributes the
    /// refusal that just landed rather than a stale or anonymous one.
    /// Empty where no refusal named a claimant.
    fn refused_claimants(&self) -> Vec<u64> {
        match self {
            Self::Remote(remote) => remote.fenced_by().into_iter().collect(),
            Self::Local(fanout) => fanout.refused_claimants(),
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
    /// This instance's model is a deliberate revision of the run's
    /// previous one: on a tracking peer a pulled checkpoint carrying a
    /// different fingerprint crosses the model boundary through the
    /// documented carryover rule instead of degrading on the mismatch;
    /// on a lone `--state-file` run the same arm routes a
    /// foreign-fingerprint checkpoint through the carryover rule
    /// instead of refusing the resume.
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
    /// The pair's shared tracking secret — both peers launch with the
    /// same token, hashed to the key the monitor's `?prove=`
    /// checkpoint answers sign and its announced-source pulls verify:
    /// an announced demotion and every checkpoint the adopted source
    /// later serves must carry the keyed line proof only a peer
    /// holding the token can produce, so an endpoint that merely
    /// replays or fabricates this line's checkpoints can neither arm
    /// the demotion nor feed the demoted peer forged state. `None`
    /// keeps the unkeyed contract: announced demotions verify on the
    /// document checks alone.
    pair_token: Option<String>,
}

const USAGE: &str = "\
Usage: dcs-controller <model-file> [--check] [--ticks N] [--scan-ms MS]
                      [--dt T] [--listen ADDR] [--standby ADDR]
                      [--peer ADDR] [--remote ADDR] [--driven]
                      [--auto-promote N] [--owner-token N] [--revised]
                      [--pair-token TOKEN]
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
                  runs until stopped, or for N scans when --ticks is given
                  too. Without --ticks each scan prints one
                  telemetry-snapshot JSON line on stdout through a bounded
                  (64-line) sink — a consumer that stops draining degrades
                  delivery under the stdout_snapshot_drops counter
                  reported on stderr, never the scan's cadence
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
  --revised       declare this instance's model a deliberate revision of
                  the run's previous one. On a --standby peer a pulled
                  checkpoint whose model fingerprint differs crosses the
                  boundary under the documented carryover rule —
                  operator-writable internal points matched by declared
                  identity carry their last values, component state
                  reinitializes — and the peer reports reinitialized,
                  promotable in place of tracking; a checkpoint breaking
                  the rule is rejected with a named error before
                  promotion. On a lone controller the flag arms the
                  --state-file resume instead: a persisted checkpoint
                  under a foreign fingerprint crosses through the same
                  rule — the scheduled-outage roll — resuming at the
                  checkpointed tick with the carryover report printed
                  and journaled, while a rule-breaking checkpoint fails
                  startup naming the CarryoverError and leaves the file
                  untouched. Requires --standby or --state-file
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
  --pair-token TOKEN
                  the pair's shared tracking secret — launch both peers
                  of a redundant pair with the same TOKEN. The monitor
                  then signs its /checkpoint answers to ?prove= pulls
                  with the keyed line proof, and an announced-source
                  demotion plus every checkpoint the adopted source
                  later serves must return the matching proof — an
                  endpoint that only replays or fabricates this line's
                  checkpoints can neither arm the demotion nor feed the
                  demoted peer forged state. Requires --listen; unset,
                  the announced-source contract is closed — /checkpoint
                  is public, so no announced endpoint can prove itself
                  and an announced-only demotion refuses
                  no_tracking_source (a configured --peer still covers
                  the switchover)
  --state-file PATH
                  persist the run's checkpoint to PATH at the end of
                  every scan cycle and at each accepted command's
                  admission boundary — atomically, by write-then-rename —
                  and resume from it at startup when it exists: a file
                  that cannot be resumed (unreadable, unparseable, an
                  unsupported format version, or a fingerprint/structural
                  mismatch with the loaded model) exits nonzero naming
                  the reason; a missing file is a cold start. With
                  --revised, a foreign-fingerprint file instead crosses
                  the model boundary under the carryover rule — the lone
                  controller's scheduled-outage roll
  --journal-file PATH
                  persist the transition journal to PATH — one
                  line-delimited JSON record per journaled entry,
                  appended by a dedicated writer behind a bounded
                  queue so a slow sink never lengthens a scan — and
                  replay it at startup, seeding the served ring and
                  continuing seq numbering across a restart; a
                  run-boundary marker separates process lifetimes, a
                  corrupt record exits nonzero naming it, and a missing
                  file is a cold start. The file is single-writer: a
                  second live process on the same PATH exits nonzero
                  naming the writer-lock conflict — never point two
                  controllers at one journal file. Requires --listen
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
        let mut pair_token = None;
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
                "--pair-token" => pair_token = Some(value("--pair-token")?),
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
                ("--pair-token", pair_token.is_some()),
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
        if revised && standby.is_none() && state_file.is_none() {
            return Err(
                "--revised requires --standby or --state-file: a tracking peer or a \
                 lone state-file resume rolls a revised model"
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
        if pair_token.is_some() && listen.is_none() {
            return Err(
                "--pair-token requires --listen: the line proofs it keys live on the monitor"
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
            pair_token,
        })
    }
}

fn fail(message: impl std::fmt::Display) -> ExitCode {
    eprintln!("error: {message}");
    ExitCode::FAILURE
}

/// Installs the pair's shared tracking secret on the monitor when the
/// deployment declared one — `--pair-token` hashed to the key the
/// monitor's `?prove=` checkpoint answers sign and its adopted-source
/// pulls verify. `None` keeps the run unkeyed: `?prove=` answers stay
/// plain and announced demotions verify on the document checks alone.
fn keyed_monitor<'d>(monitor: Monitor<'d>, options: &Options) -> Monitor<'d> {
    match &options.pair_token {
        Some(token) => monitor.with_pair_key(dcs_monitor::pair_key(token)),
        None => monitor,
    }
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

/// What a `--state-file` resume did with an existing checkpoint — the
/// answer [`resume_state_file`] reports so the caller can present and
/// record the crossing a revision-armed resume ran.
enum Resume {
    /// No state file existed — a cold start.
    Cold,
    /// The checkpoint carried this run's model fingerprint and applied
    /// under the strict restore negotiation.
    Applied,
    /// The checkpoint carried a foreign fingerprint and the
    /// revision-armed resume crossed the model boundary under the
    /// documented carryover rule — the report is the run's record of
    /// what transferred, what initialized fresh, and what was named
    /// dropped. Boxed — the report is a record of the crossing, not a
    /// per-scan payload, and keeping the enum small keeps the common
    /// `Applied`/`Cold` legs cheap to move.
    Reinitialized(Box<CarryoverReport>),
}

/// The `--state-file` resume half: when `path` names an existing file it
/// must hold a [`Checkpoint`] this run can take over — applied in place
/// to the freshly assembled `executor` before the first scan, so the run
/// continues at the checkpointed tick. A missing file is a cold start
/// ([`Resume::Cold`]); anything else that cannot resume — an unreadable
/// file, contents that are not a checkpoint, or a [`RestoreError`]
/// naming the version, fingerprint, or structural mismatch — fails the
/// start, per the checkpoint-restore decision's all-or-nothing rule:
/// never silently fresh over a state file that exists but cannot be
/// resumed.
///
/// `revised` arms the lone controller's scheduled-outage roll — the
/// rolling model-revision decision's carryover rule extended to this
/// seam: a checkpoint whose model fingerprint differs from the freshly
/// assembled run's routes through [`Executor::reinitialize`] instead of
/// [`Executor::apply`], carrying the writable internal values, the
/// output image, and the force set the rule names and answering
/// [`Resume::Reinitialized`] with the crossing's report. A checkpoint
/// the rule cannot carry fails startup naming the `CarryoverError` and
/// — like every refused resume — leaves the file untouched; a matching
/// fingerprint still resumes ordinarily through `apply`, and an
/// unarmed mismatch still refuses on `RestoreError::FingerprintMismatch`.
fn resume_state_file(
    path: &Path,
    executor: &mut Executor<'_>,
    revised: bool,
) -> Result<Resume, String> {
    let body = match std::fs::read(path) {
        Ok(body) => body,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Resume::Cold),
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
    if revised && checkpoint.model_fingerprint != executor.model_fingerprint() {
        return executor
            .reinitialize(&checkpoint)
            .map(|report| Resume::Reinitialized(Box::new(report)))
            .map_err(|error| {
                format!(
                    "cannot resume from state file {}: model-boundary carryover failed: {error}",
                    path.display()
                )
            });
    }
    executor
        .apply(&checkpoint)
        .map_err(|error| format!("cannot resume from state file {}: {error}", path.display()))?;
    Ok(Resume::Applied)
}

/// The `--state-file` persist a monitorless run owns directly: the
/// monitor carries its own [`StateSink`] when `state_file` is
/// configured, but a run serving no monitor still owes its cycle-end
/// checkpoint the same bounded dedicated-writer drain — the capture
/// rides the scan loop's own serialization (single-threaded), the
/// write drains off it. The sink's drop drains and joins the writer,
/// so a graceful exit leaves the file complete through the last push.
fn open_state_sink(options: &Options) -> Option<StateSink> {
    options
        .state_file
        .as_ref()
        .map(|path| StateSink::new(path, DEFAULT_STATE_DRAIN_CAPACITY))
}

/// The monitorless scan loop's cycle-end `--state-file` persist:
/// captures the peer's checkpoint — after the scan and the plant
/// step, where the loop invokes it — and hands it to the sink's
/// bounded queue, so a slow disk never lengthens this loop's cadence.
/// `Ok` once the capture is queued — FIFO behind every earlier one;
/// a refusal — the queue full past its bound, or a recorded write
/// failure — returns the named error the loop fails the run on.
fn persist_state_file(
    state_sink: &Option<StateSink>,
    peer: &std::cell::RefCell<Peer<'_>>,
) -> Result<(), String> {
    match state_sink {
        Some(sink) => sink.offer(peer.borrow().checkpoint()).map(|_| ()),
        None => Ok(()),
    }
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
                // A controller's field attachment claims as a
                // controller: its write-ownership claims record the
                // marker a peer's conditional takeover refuses to
                // preempt while they stand live.
                Ok(remote) => Driver::Remote(remote.as_controller()),
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
    // This process boot is a new checkpoint-stream generation: every
    // checkpoint the run serves stamps it, so a tracking peer can tell
    // the source's cold restart — a fresh mint — from its own demotion's
    // tracking reset on the uninterrupted line. A `--state-file` resume
    // below adopts the file's generation instead: the resumed run
    // continues the line it persisted.
    let mut executor = match assemble(&model, &registry(), io) {
        Ok(executor) => executor.with_generation(mint_generation()),
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
    // interrupted run at the checkpointed tick. A --revised run whose
    // file was captured under a different model crosses the boundary
    // under the documented carryover rule instead — the lone roll's
    // scheduled-outage resume — and the crossing's report is the run's
    // record of what carried: printed here, journaled into the
    // monitor's durable record below when one binds.
    let mut resumed_crossing = None;
    if let Some(path) = &options.state_file {
        match resume_state_file(path, &mut executor, options.revised) {
            Ok(Resume::Applied) => eprintln!(
                "resumed from state file {} at tick {}",
                path.display(),
                executor.tick().0
            ),
            Ok(Resume::Reinitialized(report)) => {
                eprintln!(
                    "resumed from state file {} across the model boundary: {report}",
                    path.display()
                );
                resumed_crossing = Some(report);
            }
            Ok(Resume::Cold) => {}
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
    // field's standing owner. The activation's own claim is the
    // conditional startup grant: it preempts a dead owner's standing
    // claim — the restart-as-active recovery — but refuses while a
    // *live* incumbent holds the field, so a controller restarting
    // onto a stale checkpoint cannot seize the field and silently roll
    // back commands the incumbent receipted and applied.
    let owner = options.owner_token.unwrap_or_else(owner_token);
    let peer = match &options.standby {
        Some(_) => Peer::standby(executor, gate.as_ref()),
        None => Peer::active(executor, gate.as_ref()),
    };
    let peer = peer
        .with_field_claim(|| driver.claim_writer(owner))
        .with_field_release(|| driver.release_claim())
        .with_field_ensure(|| driver.ensure_writer(owner))
        .with_field_orphan_claim(|| driver.claim_writer_unless_held(owner))
        .with_field_startup_claim(|| driver.claim_writer_unless_held(owner))
        .with_field_probe(|| driver.probe_field_claim())
        .with_field_claimant(|point| driver.fencing_claimant(point))
        .with_field_owner_endpoint(|point| driver.field_owner_endpoint(point))
        .with_field_reclaim(|| driver.reclaim_writer(owner))
        .with_claim_observer(|| driver.refused_claimants());
    let peer = match options.auto_promote {
        Some(budget) => peer.with_failover(budget),
        None => peer,
    };
    // A --revised instance declared its model a deliberate revision of
    // the run's previous one: on a tracking peer the pull path routes a
    // foreign-fingerprint checkpoint through the documented carryover
    // rule rather than degrading on the mismatch the fingerprint gate
    // would otherwise report — and on a lone --state-file run the same
    // arm already routed the resume through `Executor::reinitialize`
    // above, the flag staying set so a later demotion keeps the
    // declared intent on the pull path too.
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
    // the durable journal-file sink --journal-file names, and the
    // --state-file checkpoint sink — a monitored run persists through
    // the monitor's own drained writer, so its scan and command paths
    // never hold the lock on the file's I/O.
    let monitor_config = || MonitorConfig {
        journal_file: options.journal_file.clone(),
        state_file: options.state_file.clone(),
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
        let monitor = keyed_monitor(monitor, &options);
        // Register the bound monitor port on the field claims: every
        // claim this instance asserts or re-arms names where its owner
        // serves checkpoints, so the verdict fencing a superseded peer
        // out can carry this run's endpoint as its successor source.
        driver.set_claim_endpoint(monitor.local_addr().port());
        let monitor = monitor.driven(Driven {
            track,
            after_scan: Some(Box::new(|peer: &Peer<'_>| {
                // The scan cycle's plant step; the configured state
                // file's capture+queue follows inside the same lock
                // hold — the monitor's own persist boundary — and the
                // request's `200` attests the durable file caught up.
                driver.step(dt, peer.owns_field())
            })),
        });
        // The armed-resume crossing's report joins the durable record
        // behind the run-boundary marker the bind journaled.
        if let Some(report) = &resumed_crossing {
            monitor.note_reinitialized(report.as_ref().clone());
        }
        // A launched active owns the field from startup: activation
        // runs the claim-then-lift sequence — the conditional startup
        // grant under this instance's token first, the gate second —
        // deferred to here, after every fallible local startup step
        // (the track address resolved, the journal replayed, the
        // monitor bound), so a starter that cannot serve never lands a
        // claim on the field's standing owner. The grant preempts a
        // dead owner's claim but refuses a live incumbent's, and a
        // refused grant is a named startup failure, not an unfenced
        // run: the incumbent's receipted state is never silently
        // reverted by a stale restart.
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
                let monitor = keyed_monitor(monitor, &options);
                // As on every monitored path: the bound monitor port
                // registers on this instance's field claims, so a peer
                // this run later fences out learns where its successor
                // serves checkpoints from the field's own arbitration.
                driver.set_claim_endpoint(monitor.local_addr().port());
                // The promotion boundary runs one final pull against the
                // tracking source, so a command the active admitted up
                // to the promote request is carried.
                let monitor = monitor.with_standby_source(active_addr);
                // The armed-resume crossing's report joins the durable
                // record behind the run-boundary marker the bind
                // journaled.
                if let Some(report) = &resumed_crossing {
                    monitor.note_reinitialized(report.as_ref().clone());
                }
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
                let state_sink = open_state_sink(&options);
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
                        for orphan in peer.take_orphans() {
                            eprintln!(
                                "standby: the tracked line has no field owner — orphaned at tick {} (aligned to {})",
                                orphan.tick.0, orphan.aligned.0
                            );
                        }
                        for observation in peer.take_claim_observations() {
                            eprintln!(
                                "standby: field write-ownership claim observed standing under foreign owner token {} at tick {} (point {:?})",
                                observation.claimant, observation.tick.0, observation.point
                            );
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
                        for (_index, receipt) in peer.take_superseded_commands() {
                            eprintln!(
                                "standby: pending command superseded at tick {}: {:?}",
                                peer.tick().0,
                                receipt.command
                            );
                        }
                        for receipt in peer.take_adoption_receipts() {
                            eprintln!(
                                "standby: checkpoint adoption changed receipted state at tick {}: {:?} (actor {:?})",
                                peer.tick().0,
                                receipt.command,
                                receipt.actor
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
                            match loss.claimant {
                                Some(claimant) => eprintln!(
                                    "standby: field write-ownership claim lost at tick {}: {:?} fenced, preempted by owner token {claimant}",
                                    loss.tick.0, loss.point
                                ),
                                None => eprintln!(
                                    "standby: field write-ownership claim lost at tick {}: {:?} fenced",
                                    loss.tick.0, loss.point
                                ),
                            }
                        }
                        for observation in peer.take_claim_observations() {
                            eprintln!(
                                "standby: field write-ownership claim observed standing under foreign owner token {} at tick {} (point {:?})",
                                observation.claimant, observation.tick.0, observation.point
                            );
                        }
                        scanned
                    },
                    || peer.borrow().snapshot(),
                    || persist_state_file(&state_sink, &peer),
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
                let monitor = keyed_monitor(monitor, &options);
                // As on every monitored path: the bound monitor port
                // registers on this instance's field claims, so a peer
                // this run later fences out learns where its successor
                // serves checkpoints from the field's own arbitration.
                driver.set_claim_endpoint(monitor.local_addr().port());
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
                // The armed-resume crossing's report joins the durable
                // record behind the run-boundary marker the bind
                // journaled.
                if let Some(report) = &resumed_crossing {
                    monitor.note_reinitialized(report.as_ref().clone());
                }
                // The launched active's deferred startup activation —
                // the same conditional-grant sequence the driven path
                // runs: the claim lands only now, the journal replayed,
                // the monitor bound, and the peer address resolved, so
                // a startup that failed earlier left no stale claim
                // fencing the field's standing owner. The grant
                // preempts a dead owner's claim but refuses a live
                // incumbent's — a refused grant is a named startup
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
                // conditional-grant sequence the monitored paths defer
                // to their last startup step: nothing fallible stands
                // between here and the scan loop, so the claim runs
                // only now that startup can no longer abort — and a
                // live incumbent's claim refuses it rather than being
                // preempted by a stale restart.
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
                let state_sink = open_state_sink(&options);
                scan_loop(
                    || {
                        let mut peer = peer.borrow_mut();
                        let scanned = peer.scan();
                        for loss in peer.take_fencing_losses() {
                            match loss.claimant {
                                Some(claimant) => eprintln!(
                                    "field write-ownership claim lost at tick {}: {:?} fenced, preempted by owner token {claimant}",
                                    loss.tick.0, loss.point
                                ),
                                None => eprintln!(
                                    "field write-ownership claim lost at tick {}: {:?} fenced",
                                    loss.tick.0, loss.point
                                ),
                            }
                        }
                        for observation in peer.take_claim_observations() {
                            eprintln!(
                                "field write-ownership claim observed standing under foreign owner token {} at tick {} (point {:?})",
                                observation.claimant, observation.tick.0, observation.point
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
                    || persist_state_file(&state_sink, &peer),
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
/// the configured `--standby`/`--peer` target when set, else a proven
/// announced source — the monitor address a tracking peer announced
/// through its `?peer=` pulls once the demote verify's checks passed
/// on it — the follow-peer half that lets a demoted launched active
/// find its successor without a restart, the serving side accepting
/// the announce only as the pulling connection's own source address
/// (a wildcard `--listen 0.0.0.0` announce resolving to it, so the
/// recorded source is never an undialable bind address). An
/// involuntary demotion — the field claim's mid-run loss — runs that
/// verification lazily here the first sourceless cycle after it: each
/// recorded hint gets one bounded pull, a dead or foreign announcer
/// loses to the legitimate successor's own proof, and only unproven
/// hints leaves the peer pulling nothing rather than following one.
/// The puller follows the resolved source, respawning when it
/// changes, and announces this monitor's own address on every pull so
/// the serving peer learns where to track back. A
/// field-owning cycle's [`Monitor::track_cycle`] short-circuits before
/// the pull, so the puller's fetch thread idles until a demotion.
fn tracked_cycle(
    monitor: &Monitor<'_>,
    puller: &mut Option<(SocketAddr, CheckpointPuller)>,
) -> Tick {
    if let Some(source) = monitor.verified_tracking_source() {
        if puller.as_ref().map(|(bound, _)| *bound) != Some(source) {
            let announce = Some(monitor.local_addr());
            // A source a keyed run adopted through an announced
            // demotion must keep proving every checkpoint it serves —
            // an endpoint that only replays or fabricates this line's
            // documents feeds the demoted peer nothing. A configured
            // source — or an unkeyed run — pulls unproven, as before.
            let fresh = match monitor.pull_proof_key(source) {
                Some(key) => CheckpointPuller::with_pair_proof(source, announce, key),
                None => CheckpointPuller::new(source, announce),
            };
            *puller = Some((source, fresh));
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
            || monitor.persist_state(),
            step,
            || monitor.record_scan_overrun(),
            options,
            Some(period),
        );
        monitor.shutdown();
        result
    })
}

/// The bound on [`SnapshotSink`]'s handoff queue: at most this many
/// serialized per-scan snapshot lines may wait on the stdout writer
/// before the scan loop drops further lines under the
/// `stdout_snapshot_drops` counter. The bound is the sink's only
/// buffering — a consumer that keeps draining sees every line, while a
/// stalled one degrades delivery, never cadence.
const SNAPSHOT_SINK_BOUND: usize = 64;

/// The bound on the sink's report queue: the degradation lines
/// [`SnapshotSink`] hands to stderr ride their own bounded channel to a
/// reporter thread, because stderr is a potentially blocking write too
/// — a deployment that stalls both streams must still not reach the
/// scan cycle. A saturated report queue drops the advisory line; the
/// `stdout_snapshot_drops` count the surviving reports carry stays
/// accurate regardless.
const SNAPSHOT_REPORT_BOUND: usize = 8;

/// The bounded handoff between the scan loop and stdout, adopted from
/// review finding #545: a continuous run emits one telemetry-snapshot
/// JSON line per scan, and a consumer that stops draining stdout — an
/// unflushed or filled process pipe — must never pace the loop by
/// backpressure, the bounded-consumer rule decision 83 sets for every
/// consumer-facing delivery. A dedicated writer thread drains a
/// bounded channel into the locked stdout, so [`emit`](Self::emit)
/// only ever *offers* the line: a saturated sink drops it under the
/// `stdout_snapshot_drops` counter — reported on stderr at the
/// episode's start, at each doubling of the count, and once more when
/// the consumer drains — the named delivery gap, while scan cadence,
/// `io_health.scan_overruns` truthfulness, and the failover miss
/// budget all run untouched. A writer thread that dies on a failed
/// stdout write — a closed pipe — is reported once with its error and
/// every later line drops on the same counter; delivery loss never
/// becomes a plant shutdown condition.
struct SnapshotSink {
    sender: mpsc::SyncSender<String>,
    /// Lines handed off but not yet written — the sink's outstanding
    /// depth, decremented by the writer after each successful write.
    /// Signed because the writer can finish a queued line before the
    /// emit side's increment lands; a transient negative corrects
    /// itself on the next send.
    pending: Arc<std::sync::atomic::AtomicI64>,
    /// The writer thread's terminal stdout error, recorded before its
    /// receiver drops so the emit side can name it once.
    failure: Arc<Mutex<Option<String>>>,
    /// The degradation reports' bounded handoff to the reporter
    /// thread — stderr writes stay off the scan cycle exactly like
    /// stdout's do.
    reports: mpsc::SyncSender<String>,
    /// Total lines the sink refused — the `stdout_snapshot_drops`
    /// counter the degradation reports carry.
    dropped: u64,
    /// The next `dropped` value that logs a progress line — doubling
    /// from 1, so a permanently stalled consumer costs a bounded
    /// report trail rather than one line per scan.
    next_report: u64,
    /// Whether the sink is inside a saturated episode — cleared only
    /// when a send finds the queue drained, so a consumer hovering at
    /// the boundary does not flap the report pair every scan.
    saturated: bool,
    /// Whether the writer's loss was already reported.
    writer_lost: bool,
}

impl SnapshotSink {
    /// Starts the sink reporting on stderr: `writer` — stdout in the
    /// run loop — is drained on its own thread, so the only blocking
    /// write lives off the scan cycle's critical path.
    fn start(writer: impl std::io::Write + Send + 'static) -> Self {
        Self::reporting(writer, |line| eprintln!("{line}"))
    }

    /// The injectable form: `writer` is anything `Write + Send` and
    /// `report` receives the degradation lines — stderr in the run,
    /// anything `Send` in tests — each on its own thread, so neither
    /// stream's backpressure reaches the caller of
    /// [`emit`](Self::emit).
    fn reporting(
        writer: impl std::io::Write + Send + 'static,
        mut report: impl FnMut(&str) + Send + 'static,
    ) -> Self {
        let (sender, receiver) = mpsc::sync_channel(SNAPSHOT_SINK_BOUND);
        let (reports, report_lines) = mpsc::sync_channel::<String>(SNAPSHOT_REPORT_BOUND);
        let pending = Arc::new(std::sync::atomic::AtomicI64::new(0));
        let failure = Arc::new(Mutex::new(None));
        let writer_thread = (pending.clone(), failure.clone());
        std::thread::spawn(move || {
            let (pending, failure) = writer_thread;
            let mut writer = writer;
            while let Ok(line) = receiver.recv() {
                match writeln!(writer, "{line}") {
                    Ok(()) => {
                        pending.fetch_sub(1, Ordering::SeqCst);
                    }
                    Err(error) => {
                        *failure.lock().unwrap() = Some(error.to_string());
                        return;
                    }
                }
            }
        });
        std::thread::spawn(move || {
            while let Ok(line) = report_lines.recv() {
                report(&line);
            }
        });
        Self {
            sender,
            pending,
            failure,
            reports,
            dropped: 0,
            next_report: 1,
            saturated: false,
            writer_lost: false,
        }
    }

    /// Queues one degradation line for the reporter. Advisory only: a
    /// saturated report queue drops the line rather than blocking the
    /// scan cycle — the `stdout_snapshot_drops` count the surviving
    /// lines carry remains the accurate total.
    fn report(&self, line: String) {
        let _ = self.reports.try_send(line);
    }

    /// Offers one serialized snapshot line to the writer. Never blocks
    /// the caller: a full queue — or a writer gone after a failed
    /// write — drops the line under `stdout_snapshot_drops` instead,
    /// the named degradation replacing finding #545's stall.
    fn emit(&mut self, line: String) {
        match self.sender.try_send(line) {
            Ok(()) => {
                let outstanding = self.pending.fetch_add(1, Ordering::SeqCst) + 1;
                // Recovery counts only once the backlog actually
                // drained — our line the only one outstanding — so a
                // consumer that frees a single slot per scan does not
                // retrigger the report pair every cycle.
                if self.saturated && outstanding <= 1 {
                    self.saturated = false;
                    self.report(format!(
                        "snapshot sink: stdout drained — per-scan snapshot lines resumed \
                         (stdout_snapshot_drops={})",
                        self.dropped
                    ));
                }
            }
            Err(mpsc::TrySendError::Full(_)) => {
                self.dropped += 1;
                self.saturated = true;
                if self.dropped >= self.next_report {
                    self.report(format!(
                        "snapshot sink: stdout not draining — dropping per-scan snapshot \
                         lines (stdout_snapshot_drops={})",
                        self.dropped
                    ));
                    self.next_report = self.dropped.saturating_mul(2).max(self.dropped + 1);
                }
            }
            Err(mpsc::TrySendError::Disconnected(_)) => {
                self.dropped += 1;
                if !self.writer_lost {
                    self.writer_lost = true;
                    let detail = self
                        .failure
                        .lock()
                        .unwrap()
                        .clone()
                        .unwrap_or_else(|| "writer thread ended".to_string());
                    self.report(format!(
                        "snapshot sink: stdout write failed ({detail}) — dropping per-scan \
                         snapshot lines for the rest of the run (stdout_snapshot_drops={})",
                        self.dropped
                    ));
                }
            }
        }
    }
}

/// The scan loop every run mode shares: `scan` performs one executor
/// scan — directly, through the monitor's lock when serving, or after a
/// standby's checkpoint pull — and `snapshot` reads the resulting
/// telemetry. `step` advances the simulated plant one `dt`; the
/// `--ticks` bound, the snapshot reporting, and the wall-clock pacing
/// are identical either way.
///
/// `persist` is the cycle-end `--state-file` persist: the run's
/// transferable state is captured at the end of every completed scan
/// cycle — after the scan and the plant step, so a resumed run
/// re-enters the loop at exactly this point — and queued to the
/// sink's dedicated writer, which serializes and replaces the file
/// off this thread's path. A push refusal — the queue full past its
/// bound, a recorded write failure — fails the run like a step
/// failure does: a controller that cannot persist its recovery state
/// exits naming the file rather than running on without it. On a
/// monitored run the closure routes through
/// [`Monitor::persist_state`], so the cycle-end capture and a
/// command's admission-boundary capture serialize on the same lock —
/// and through the sink's FIFO, so the two can never interleave into
/// a stale overwrite; a monitorless run owns its own [`StateSink`]
/// behind the same shape.
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
    persist: impl Fn() -> Result<(), String>,
    step: impl Fn() -> Result<(), String>,
    mut overrun: impl FnMut(),
    options: &Options,
    period: Option<Duration>,
) -> ExitCode {
    let mut scanned = 0_u64;
    // The continuous run's per-scan snapshot stream is a bounded
    // consumer of this loop (decision 83): lines go through the sink,
    // so a stdout reader that stops draining degrades delivery under
    // `stdout_snapshot_drops` rather than pacing the scan — review
    // finding #545. A `--ticks` run prints only its final snapshot, a
    // one-shot write after the last scan, and keeps the direct print.
    let mut sink = options
        .ticks
        .is_none()
        .then(|| SnapshotSink::start(std::io::stdout()));
    loop {
        let started = Instant::now();
        scan();
        if let Err(error) = step() {
            return fail(error);
        }
        if let Err(error) = persist() {
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
            // Continuous operation: report the run's state as JSON
            // lines — through the sink, so this write can never stall
            // the cycle the `elapsed` measurement below closes.
            match serde_json::to_string(&snapshot()) {
                Ok(snapshot) => sink.as_mut().unwrap().emit(snapshot),
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

#[cfg(test)]
mod tests {
    use super::*;

    /// A writer whose every write blocks forever — the consumer that
    /// stopped draining, finding #545's stall reproduced as a type.
    struct StalledWriter;

    impl std::io::Write for StalledWriter {
        fn write(&mut self, _buf: &[u8]) -> std::io::Result<usize> {
            loop {
                std::thread::park();
            }
        }

        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }

    /// A writer that fails every write — the closed pipe the sink must
    /// name once and then drop against.
    struct FailingWriter;

    impl std::io::Write for FailingWriter {
        fn write(&mut self, _buf: &[u8]) -> std::io::Result<usize> {
            Err(std::io::Error::new(
                std::io::ErrorKind::BrokenPipe,
                "closed pipe",
            ))
        }

        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }

    /// A writer gated on `open`: closed it blocks in `write` — the
    /// stalled consumer — and opened it drains, so a test scripts the
    /// exact stall-then-recover episode the drain report covers.
    struct GatedWriter {
        open: Arc<std::sync::atomic::AtomicBool>,
    }

    impl std::io::Write for GatedWriter {
        fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
            while !self.open.load(Ordering::Relaxed) {
                std::thread::sleep(Duration::from_millis(1));
            }
            Ok(buf.len())
        }

        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }

    /// Collects the report lines a sink hands its reporter thread.
    fn collector() -> (impl FnMut(&str) + Send + 'static, Arc<Mutex<Vec<String>>>) {
        let collected = Arc::new(Mutex::new(Vec::new()));
        let sink = collected.clone();
        (
            move |line: &str| sink.lock().unwrap().push(line.to_string()),
            collected,
        )
    }

    /// Polls `condition` until it holds or `deadline` elapses — the
    /// sink's writer and reporter threads run concurrently with the
    /// test, so their effects arrive asynchronously.
    fn eventually(mut condition: impl FnMut() -> bool, deadline: Duration, what: &str) {
        let started = Instant::now();
        while !condition() {
            assert!(started.elapsed() < deadline, "timed out waiting for {what}");
            std::thread::sleep(Duration::from_millis(2));
        }
    }

    #[test]
    fn a_stalled_consumer_drops_under_the_named_counter_without_blocking() {
        let (report, collected) = collector();
        let mut sink = SnapshotSink::reporting(StalledWriter, report);

        // Far more lines than the bound: every emit returns without
        // blocking — the loop completing is the cadence proof — and
        // all but the writer-held and queued lines drop.
        let lines = (SNAPSHOT_SINK_BOUND * 4) as u64;
        for index in 0..lines {
            sink.emit(format!("line {index}"));
        }
        assert!(sink.dropped >= lines - SNAPSHOT_SINK_BOUND as u64 - 1);
        // Queued-but-unwritten plus dropped accounts for every line —
        // nothing duplicated, nothing silently lost.
        assert_eq!(
            sink.dropped as i64 + sink.pending.load(Ordering::SeqCst),
            lines as i64
        );

        // The named degradation is reported.
        eventually(
            || !collected.lock().unwrap().is_empty(),
            Duration::from_secs(5),
            "the stdout_snapshot_drops report",
        );
        let reports = collected.lock().unwrap().clone();
        assert!(
            reports
                .iter()
                .any(|line| line.contains("stdout not draining")
                    && line.contains("stdout_snapshot_drops=")),
            "{reports:?}"
        );
    }

    #[test]
    fn a_failed_writer_is_named_once_and_every_later_line_drops() {
        let (report, collected) = collector();
        let mut sink = SnapshotSink::reporting(FailingWriter, report);

        // The writer takes the first queued line and dies on it; emit
        // until the receiver's drop turns sends into the Disconnected
        // leg.
        sink.emit("first".to_string());
        eventually(
            || {
                sink.emit("probe".to_string());
                sink.writer_lost
            },
            Duration::from_secs(5),
            "the failed writer to be named",
        );
        for _ in 0..10 {
            sink.emit("more".to_string());
        }
        assert!(sink.dropped >= 10);

        eventually(
            || !collected.lock().unwrap().is_empty(),
            Duration::from_secs(5),
            "the writer-failure report",
        );
        let reports = collected.lock().unwrap().clone();
        let failures: Vec<_> = reports
            .iter()
            .filter(|line| line.contains("stdout write failed"))
            .collect();
        assert_eq!(failures.len(), 1, "{reports:?}");
        assert!(failures[0].contains("closed pipe"), "{failures:?}");
        assert!(failures[0].contains("stdout_snapshot_drops="));
    }

    #[test]
    fn a_draining_consumer_resumes_delivery_and_reports_once() {
        let (report, collected) = collector();
        let open = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let mut sink = SnapshotSink::reporting(GatedWriter { open: open.clone() }, report);

        // Stall: the queue saturates and lines drop under the counter.
        let lines = (SNAPSHOT_SINK_BOUND * 4) as u64;
        for index in 0..lines {
            sink.emit(format!("line {index}"));
        }
        assert!(sink.saturated);

        // Drain: the consumer comes back; the next emit finds the
        // backlog cleared and reports the resume.
        open.store(true, Ordering::Relaxed);
        eventually(
            || {
                sink.emit("after drain".to_string());
                collected
                    .lock()
                    .unwrap()
                    .iter()
                    .any(|line| line.contains("stdout drained"))
            },
            Duration::from_secs(5),
            "the drain report",
        );
        let reports = collected.lock().unwrap().clone();
        let drains: Vec<_> = reports
            .iter()
            .filter(|line| line.contains("stdout drained"))
            .collect();
        assert_eq!(drains.len(), 1, "{reports:?}");
        assert!(drains[0].contains("stdout_snapshot_drops="));
    }
}
