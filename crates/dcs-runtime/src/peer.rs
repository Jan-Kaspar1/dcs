//! The redundant-pair role machine: field-write ownership, promotion,
//! and demotion across a tracking standby.
//!
//! Per the switchover-semantics decision, exactly one peer of a
//! redundant pair writes the field at a time. [`Peer`] is that rule made
//! executable: it wraps an [`Executor`] assembled against a
//! [`WriteGate`]-gated driver, reports the instance's
//! [`Role`](dcs_core::Role) as the serde
//! [`RoleReport`](dcs_core::RoleReport) the monitoring surface serves,
//! and applies promotion and demotion at a scan boundary — between
//! scans, never mid-scan — by lifting or closing the gate.
//!
//! The documented boundary: [`promote`](Peer::promote) opens the gate
//! the moment the request is handled — callers serialize it against
//! scans, so it always lands between two scans and the first
//! post-promotion scan continues the checkpointed run bumplessly —
//! while [`demote`](Peer::demote) closes it the same way. The reported
//! role shows the transition (`promoting`, `demoting`) until the first
//! scan under the new mode completes, then settles to `active` or
//! `standby`; each reported transition is queued for the journal as a
//! [`RoleChange`]. The documented switchover order is demote first, then
//! promote, so no scan ever has two writers.
//!
//! Promotion is accepted only from a peer whose run is defined by the
//! active's state — [`StandbySync::Tracking`], i.e. a checkpoint has
//! applied cleanly, or [`StandbySync::Reinitialized`], the revision-armed
//! crossing of the rolling model-revision decision — and refused
//! otherwise with a named
//! [`SwitchError`](dcs_core::SwitchError): `NotConverged` before
//! convergence, `AlreadyActive` on an instance already owning the field.
//! Demotion is accepted only from a field-owning instance. A
//! field-owning peer refuses checkpoint application outright; a
//! non-owning one — standby or mid-demotion — applies each checkpoint at
//! its scan boundary and reports convergence directly as the serde
//! [`StandbySync`] wire state.
//!
//! Automatic failover, per the failover decision: the checkpoint pull is
//! also the heartbeat — a produced checkpoint is proof the active
//! serves, so [`apply`](Peer::apply) resets the consecutive-miss count
//! a failed pull (`note_transfer_failed`) increments. Armed with
//! [`with_failover`](Peer::with_failover), the peer reports
//! [`failover_due`](Peer::failover_due) when the misses reach the
//! configured budget, and [`self_promote`](Peer::self_promote) then
//! promotes at the scan boundary exactly like a manual request — but
//! only while the convergence proof still stands: the last applied
//! verdict was `Tracking` and the misses have not exceeded the budget.
//! An unconverged or over-budget peer reports its named sync state
//! instead of promoting.
//!
//! A tracking caller runs the whole cycle once per scan through
//! [`track_once`](Peer::track_once) — the owns-field gate, the pull,
//! the miss accounting, and the promote-on-budget check in one place —
//! and presents the returned [`TrackReport`]; the scan itself stays
//! caller-owned.
//!
//! The run's tick is the journal and history attribution domain, so it
//! never rewinds: a checkpoint stream that regresses — below the run's
//! last alignment, or below the run's own tick before any alignment
//! stood — while naming a generation different from the run's own is a
//! restarted or replaced source beginning a new tick generation, not a
//! continuation of the tracked line. Its state still applies — the
//! tracked source is the live one — but the run resumes it at the run's
//! own tick, carrying the offset every later checkpoint lands under,
//! and the boundary queues one [`SourceRestart`] for the journal rather
//! than silently rewinding scans the run already ran and recorded.
//! The regression heuristic alone cannot tell that boundary from the
//! peer's own tracking reset — [`demote`](Peer::demote) clears the
//! alignment, so a demoted peer's first pull on a healthy successor
//! regresses identically — so the checkpoint's `generation` stamp
//! decides: the uninterrupted successor still stamps the generation
//! the demoted run's own captures carried, and a same-generation
//! regression journals nothing while a different one is the source's
//! new tick domain. An unidentified generation on either side can
//! prove no continuation, so it keeps the conservative verdict.
//! The carried offset is re-evaluated on each apply
//! against the run's live lead over the stream — it covers at most the
//! gap that remains and clears when the stream recovers to the run's
//! tick — so a tracking apply can hold or realign the run's clock but
//! never land it ahead of both clocks, the bound that keeps two
//! mutually tracking peers' offsets from compounding into each other's
//! served ticks.
//!
//! The field-ownership claim installed by
//! [`with_field_claim`](Peer::with_field_claim) runs at every transition
//! into field ownership — a promotion, manual or automatic, and a
//! launched-active peer's [`activate`](Peer::activate) at startup: the
//! fencing arbitration that makes the shared field refuse every
//! attachment not holding it. A failed claim refuses the transition
//! with [`SwitchError::FieldClaimFailed`] — a peer that cannot take the
//! field's single-writer arbitration does not take the field. And when
//! a claim the owner held is preempted — the field's arbitration is
//! unconditional, so a rogue claim or a misordered promotion can take
//! it — the first fenced field write demotes the superseded owner in
//! place: the gate re-closes, the reported role walks `demoting` to
//! `standby`, and one [`FencingLoss`] queues for the journal beside the
//! role changes — the survivable quiesced state the demote path
//! defines, not a dead process.
//!
//! Convergence alone does not prove the standby would write the field the
//! active writes, so a tracking peer also runs the standby-divergence
//! check of [`crate::divergence`]: each non-field-owning scan's staged
//! field `Out` image — the writes it would have issued — is stashed, and
//! each applied checkpoint whose tick matches that image compares it
//! against the peer's own reads of the same points. A mismatch moves the
//! peer to [`StandbySync::Diverged`], which promotion refuses like any
//! non-tracking state; only a same-tick comparison that read the field
//! and matched returns the peer to `Tracking` — an apply that ran no
//! comparison, a comparison whose field reads failed, and a pull that
//! produced nothing all carry no such evidence, so the verdict stands.
//! Both transitions queue for the transition journal — the detection
//! with its mismatches, the resolution with the compared points it
//! stands on. A diverged peer's gate stays closed throughout — the
//! check observes, it never writes.

use crate::checkpoint::{Checkpoint, RestoreError, SUPPORTED_FORMAT_VERSIONS};
use crate::divergence::{
    DivergenceReport, ResolutionReport, compare_staged_points, values_diverge,
};
use crate::executor::Executor;
use crate::gate::WriteGate;
use crate::revision::CarryoverError;
use dcs_core::{
    CarryoverReport, Command, CommandError, CommandOutcome, CommandReceipt, Divergence, PointId,
    Role, RoleReport, Sample, StandbySync, SwitchError, TelemetrySnapshot, Tick, Value,
};
use std::collections::{BTreeMap, BTreeSet};
use std::fmt;

/// A controller instance in a redundant pair: an [`Executor`] plus the
/// role and write-gate state deciding whether its scans reach the field.
///
/// `gate` is the [`WriteGate`] the executor was assembled against — the
/// shared-field pair shape, where the gate enforces quiescence on the
/// field-facing driver. `None` suits a peer whose plant is private (a
/// standby-local simulated driver): no shared field exists to quiesce
/// against, so the role is reporting-only and the gate flip is a no-op.
#[derive(Debug)]
pub struct Peer<'d> {
    executor: Executor<'d>,
    gate: Option<&'d WriteGate<'d>>,
    /// The currently reported role.
    role: Role,
    /// The standby's reported convergence — the serde wire state
    /// [`RoleReport`] and [`SwitchError::NotConverged`] carry — meaningful
    /// while the instance does not own the field; reset to
    /// `Unsynchronized` on demotion.
    sync: StandbySync,
    /// The last applied checkpoint's tick — in the tracked stream's own
    /// tick domain — while any transfer has succeeded; kept beside
    /// `sync` so `aligned_tick` still reports it across a `Degraded` or
    /// `Diverged` state.
    aligned: Option<Tick>,
    /// The run tick's lead over the tracked stream's own tick — the
    /// generation offset [`apply`](Self::apply) lands each checkpoint
    /// at `tick + tick_offset`. Zero while the stream continues the
    /// run's generation; a regressed stream — the source restarted
    /// cold or was replaced — resets it so the apply lands at the
    /// run's current tick rather than rewinding scans the run already
    /// ran and journaled. Re-evaluated against the run's live lead on
    /// every apply — see [`stream_offset`](Self::stream_offset) — so a
    /// recovering tracked stream shrinks it to the gap that remains
    /// and clears it at the run's own tick.
    tick_offset: u64,
    /// Reported-role transitions not yet consumed for journaling.
    pending_changes: Vec<RoleChange>,
    /// The last non-field-owning scan's staged field `Out` image and its
    /// tick — the divergence check's "would have written" evidence,
    /// compared against the field when a checkpoint lands at that tick.
    staged: Option<(Tick, BTreeMap<PointId, Sample>)>,
    /// Divergence detections not yet consumed for journaling — one per
    /// transition into [`StandbySync::Diverged`].
    pending_divergences: Vec<DivergenceReport>,
    /// Divergence resolutions not yet consumed for journaling — one per
    /// `Diverged` → `Tracking` transition, each carrying the applied
    /// tick and the same-tick field comparison the clear stands on.
    pending_resolutions: Vec<ResolutionReport>,
    /// Consecutive checkpoint pulls that produced no applied checkpoint
    /// — the heartbeat miss count the failover budget compares against.
    /// A produced checkpoint resets it, whether the apply lands or is
    /// rejected: the active served, so it is alive.
    misses: u32,
    /// Whether the convergence proof a self-promotion relies on still
    /// stands: the last applied verdict was `Tracking` (not diverged,
    /// not rejected) and the miss count has not exceeded the failover
    /// budget. Kept distinct from `sync`, which degrades on the first
    /// miss — the documented rule is that a tracking peer may promote
    /// itself *within* the budget.
    converged: bool,
    /// The consecutive-miss budget arming automatic failover — `None`
    /// keeps the peer manual-promotion-only.
    failover: Option<u32>,
    /// The field-side write-ownership claim a promotion takes before
    /// the gate lifts — the fencing arbitration of the failover
    /// decision — when the driver surface can arbitrate single-writer.
    claim: Option<Claim<'d>>,
    /// The claim's demotion counterpart — forgets the recorded
    /// ownership token on the driver surface, so a peer that gave the
    /// field up does not re-assert a stale claim when a re-attach finds
    /// the field's arbitration reset.
    release: Option<Release<'d>>,
    /// Whether this peer rolls a revised model into production — armed
    /// by [`with_revision`](Peer::with_revision): a pulled checkpoint
    /// whose fingerprint differs from this run's crosses the model
    /// boundary through the documented carryover rule rather than
    /// degrading the peer on a fingerprint mismatch.
    revision: bool,
    /// Reinitializations not yet consumed for journaling — one per
    /// transition into [`StandbySync::Reinitialized`], each carrying the
    /// crossing's [`CarryoverReport`].
    pending_reinits: Vec<CarryoverReport>,
    /// Tracked-source restarts not yet consumed for journaling — one
    /// [`SourceRestart`] per regressed-stream adoption that crossed a
    /// generation boundary.
    pending_restarts: Vec<SourceRestart>,
    /// Whether the field-ownership claim this peer holds was observed
    /// lost — set when a field-owning scan's write reports
    /// [`IoError::Fenced`](dcs_core::IoError::Fenced), meaning another
    /// attachment now holds the claim and the peer demotes itself on
    /// the spot. Re-armed by each
    /// successful claim lift: the queued report is once per ownership,
    /// not once per fenced scan.
    fencing_lost: bool,
    /// Claim losses not yet consumed for journaling — one
    /// [`FencingLoss`] per observed preemption.
    pending_fencing: Vec<FencingLoss>,
    /// Pending commands the tracked line abandoned — receipts a
    /// checkpoint adoption left behind while still `Accepted`, each
    /// already settled `Rejected` carrying [`CommandError::Superseded`]
    /// and queued for the journal. Abandoned means adjudicated: the
    /// adopted window's submission high-water passed the receipt's
    /// index without carrying the command, so the line demonstrably
    /// moved on without it — a stale checkpoint that never observed
    /// the submission drops nothing, `Executor::adopt_receipts`
    /// restoring the unreached tail suspended instead. A genuinely
    /// abandoned command can never apply here — the gate quiesces the
    /// run's writes — so it settles rejected rather than vanishing
    /// unaudited or `applied` on an abandoned image.
    pending_superseded: Vec<CommandReceipt>,
    /// Force-set changes a checkpoint adoption made that no settled
    /// receipt in the merged log accounts for — each queued as a
    /// [`CommandReceipt`] whose actor names the adopting source, for
    /// the journal. A receipted change — the force pair's own durable
    /// audit — journals through the ordinary settle path instead.
    pending_adoption_receipts: Vec<CommandReceipt>,
}

/// The field-side write-ownership claim a promotion runs before the
/// gate lifts: the fencing arbitration that makes the shared field
/// refuse a superseded owner's writes. A failed claim refuses the
/// promotion — a peer that cannot take the field's single-writer
/// arbitration does not take the field.
struct Claim<'d>(Box<dyn Fn() -> Result<(), String> + Send + Sync + 'd>);

impl fmt::Debug for Claim<'_> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("field claim")
    }
}

/// The demotion counterpart of [`Claim`]: forgets this peer's recorded
/// write-ownership on the driver surface. Infallible — the release is a
/// local memory clear, not a field transaction; the field's standing
/// claim is the field's to arbitrate.
struct Release<'d>(Box<dyn Fn() + Send + Sync + 'd>);

impl fmt::Debug for Release<'_> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("field release")
    }
}

/// One reported-role transition, queued for the transition journal: the
/// tick it is attributed to and the reported roles before and after.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RoleChange {
    /// The run tick the transition is attributed to: the serving scan's
    /// tick for a settle, the run's current tick for a request.
    pub tick: Tick,
    /// The previously reported role.
    pub from: Role,
    /// The newly reported role.
    pub to: Role,
}

/// The field's single-writer claim was preempted while this peer owned
/// the field — detected on the scan whose field write the field fenced.
/// One report is queued per held claim: further fenced writes under the
/// same lost claim do not queue again, and a fresh claim re-arms the
/// report.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FencingLoss {
    /// The run's tick when the loss was observed — the fenced scan's
    /// boundary; the aborted scan itself produced no tick.
    pub tick: Tick,
    /// The point whose write the field fenced.
    pub point: PointId,
}

/// The tracked checkpoint stream regressed across a generation
/// boundary — the tick it served fell below the run's last alignment
/// (or, before any alignment stood, below the run's own tick) while
/// naming a generation the run's own stream does not carry: the
/// signature of a cold-restarted or replaced source beginning a new
/// tick generation. The run adopted the checkpoint's state without
/// rewinding its own tick — the resync this report names — so the scan
/// history stays newest-last and the journal's attribution monotonic.
/// A regression on the run's own generation is the peer's tracking
/// reset, not the source's restart — a demoted peer's first pull on its
/// uninterrupted successor is the standing case — and queues nothing.
/// One report queues per boundary crossing, like [`DivergenceReport`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SourceRestart {
    /// The run tick the resync is attributed to — the tick the
    /// regressed checkpoint's state landed at, which the run's clock
    /// never rewound across.
    pub tick: Tick,
    /// The last applied checkpoint's tick before the regression —
    /// where the previous alignment stood; `None` when the run had
    /// none (a demoted peer's first pull), the regression then being
    /// measured against the run's own tick.
    pub was_aligned: Option<Tick>,
    /// The regressed checkpoint's own tick — where the new
    /// generation's stream resumed.
    pub resumed_at: Tick,
}

/// Why [`Peer::apply`] or [`Peer::transfer`] did not consume a
/// checkpoint.
#[derive(Debug, Clone, PartialEq)]
pub enum ApplyError {
    /// The instance owns field writes — `active` or `promoting` — and
    /// checkpoints apply only to a tracking peer.
    OwnsField,
    /// The checkpoint was rejected as incompatible: the run rolled back
    /// and the peer reports [`StandbySync::Degraded`].
    Restore(RestoreError),
    /// The checkpoint crossed a revision-armed peer's model boundary but
    /// broke the documented carryover rule — a kind-retyped carried
    /// point or an unservable force. Nothing applied and the peer
    /// reports [`StandbySync::Degraded`] carrying the named error.
    Carryover(CarryoverError),
}

impl fmt::Display for ApplyError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::OwnsField => {
                f.write_str("instance owns field writes; checkpoints apply to a tracking peer")
            }
            Self::Restore(error) => write!(f, "checkpoint apply failed: {error}"),
            Self::Carryover(error) => write!(f, "model-boundary carryover failed: {error}"),
        }
    }
}

impl std::error::Error for ApplyError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::OwnsField => None,
            Self::Restore(error) => Some(error),
            Self::Carryover(error) => Some(error),
        }
    }
}

/// What one pulled checkpoint did on a tracking peer — the answer of
/// [`Peer::transfer`].
#[derive(Debug, Clone, PartialEq)]
pub enum Transfer {
    /// The checkpoint carried this run's model fingerprint and applied
    /// as ordinary convergence — the peer reports
    /// [`StandbySync::Tracking`], or [`StandbySync::Diverged`] when the
    /// staged-output comparison found a mismatch.
    Applied,
    /// The checkpoint carried a different model fingerprint and the
    /// revision-armed peer crossed the boundary under the documented
    /// carryover rule — the peer reports [`StandbySync::Reinitialized`]
    /// carrying this report. Boxed beside `Applied`, which carries
    /// nothing — the same indirection [`StandbySync::Reinitialized`]
    /// gives the report on the wire.
    Reinitialized(Box<CarryoverReport>),
}

/// What one standby tracking cycle did — the answer of
/// [`Peer::track_once`], which the scan-cycle caller presents in its
/// logs. The cycle's ordering — the owns-field gate, the pull routing
/// through [`Peer::transfer`], the heartbeat miss accounting, and the
/// promote-on-budget sequence — lives in the peer; this report carries
/// the outcome, and the journaled transitions still drain through the
/// `take_*` queues.
#[derive(Debug, Clone, PartialEq)]
pub enum TrackReport {
    /// The peer owns the field — `active` or `promoting` — so the cycle
    /// ran no pull and no failover check: tracking is a field-owner
    /// no-op.
    OwnsField,
    /// The pull produced a checkpoint [`Peer::transfer`] consumed —
    /// [`Transfer::Applied`] for ordinary convergence,
    /// [`Transfer::Reinitialized`] for the revision crossing.
    Applied(Transfer),
    /// The pull produced a checkpoint the peer refused — the named
    /// [`ApplyError`], the peer reporting `Degraded`. The active
    /// served, so no heartbeat miss was counted.
    Refused(ApplyError),
    /// The pull produced nothing — an unreachable active or a refused
    /// request — counted as one heartbeat miss and reported `Degraded`
    /// with `detail`; the failover budget was not met.
    Missed {
        /// What the failed pull reported — the `Degraded` detail.
        detail: String,
    },
    /// The miss run reached the failover budget and the still-converged
    /// standby promoted itself at this boundary — `report` carries the
    /// post-promotion role.
    Promoted {
        /// What the failed pull reported — the `Degraded` detail.
        detail: String,
        /// The role report after the promotion.
        report: RoleReport,
    },
    /// The miss run reached the failover budget but self-promotion was
    /// refused — `error` names why; the peer keeps reporting the sync
    /// state the refusal carried.
    PromotionRefused {
        /// What the failed pull reported — the `Degraded` detail.
        detail: String,
        /// The named refusal.
        error: SwitchError,
    },
}

impl<'d> Peer<'d> {
    /// An instance owning field writes: role `active`.
    ///
    /// `gate` is the [`WriteGate`] the executor's driver is gated behind
    /// for a shared-field pair, or `None` when the field is private to
    /// this process. The gate stays closed until
    /// [`activate`](Self::activate) runs the field-ownership claim and
    /// lifts it — a launched active takes the field in the same
    /// claim-then-lift order a promotion does, so a shared field is
    /// fenced for this owner from the first scan rather than left open
    /// until the first promotion.
    pub fn active(executor: Executor<'d>, gate: Option<&'d WriteGate<'d>>) -> Self {
        Self {
            executor,
            gate,
            role: Role::Active,
            sync: StandbySync::Unsynchronized,
            aligned: None,
            tick_offset: 0,
            pending_changes: Vec::new(),
            staged: None,
            pending_divergences: Vec::new(),
            pending_resolutions: Vec::new(),
            misses: 0,
            converged: false,
            failover: None,
            claim: None,
            release: None,
            revision: false,
            pending_reinits: Vec::new(),
            pending_restarts: Vec::new(),
            fencing_lost: false,
            pending_fencing: Vec::new(),
            pending_superseded: Vec::new(),
            pending_adoption_receipts: Vec::new(),
        }
    }

    /// Arms the peer's fencing hook — the field-side write-ownership
    /// claim run before the gate lifts at every transition into field
    /// ownership: a promotion's, and a launched active's
    /// [`activate`](Self::activate). `claim` is the caller's arbitration
    /// against the shared field — e.g. the plant server's single-writer
    /// claim — so a peer built without it relies on the write gate
    /// alone.
    pub fn with_field_claim(
        mut self,
        claim: impl Fn() -> Result<(), String> + Send + Sync + 'd,
    ) -> Self {
        self.claim = Some(Claim(Box::new(claim)));
        self
    }

    /// Arms the claim's demotion counterpart — run when the gate closes:
    /// `release` forgets whatever ownership token this peer's attachments
    /// recorded, so a re-attaching driver surface cannot re-assert a
    /// stale claim and race the peer that legitimately took the field.
    /// A peer built without it still quiesces at the gate; the release
    /// matters only for driver surfaces that re-arm claims on reconnect.
    pub fn with_field_release(mut self, release: impl Fn() + Send + Sync + 'd) -> Self {
        self.release = Some(Release(Box::new(release)));
        self
    }

    /// Arms automatic failover: `budget` consecutive failed checkpoint
    /// pulls — one per scan cycle, the documented heartbeat cadence —
    /// make [`failover_due`](Self::failover_due) report, and
    /// [`self_promote`](Self::self_promote) take the field at that scan
    /// boundary while the convergence proof stands. `budget` must be at
    /// least one; the default is no automatic failover.
    pub fn with_failover(mut self, budget: u32) -> Self {
        assert!(
            budget > 0,
            "a failover budget must be at least one missed pull"
        );
        self.failover = Some(budget);
        self
    }

    /// Arms the rolling model-revision path — meaningful on a standby
    /// assembled under a revised plant model whose fingerprint differs
    /// from the active's by design. A pulled checkpoint carrying a
    /// foreign fingerprint then crosses the model boundary through
    /// [`transfer`](Self::transfer)'s documented carryover rule —
    /// reporting [`StandbySync::Reinitialized`] — instead of degrading
    /// the peer on a fingerprint mismatch. Checkpoints matching this
    /// run's fingerprint apply as ordinary convergence either way, so an
    /// armed peer also converges after the revision has rolled.
    pub fn with_revision(mut self) -> Self {
        self.revision = true;
        self
    }

    /// An instance tracking an active peer: role `standby`, its gate
    /// closed — scans compute outputs but no write reaches the field.
    pub fn standby(executor: Executor<'d>, gate: Option<&'d WriteGate<'d>>) -> Self {
        if let Some(gate) = gate {
            gate.close();
        }
        Self {
            executor,
            gate,
            role: Role::Standby,
            sync: StandbySync::Unsynchronized,
            aligned: None,
            tick_offset: 0,
            pending_changes: Vec::new(),
            staged: None,
            pending_divergences: Vec::new(),
            pending_resolutions: Vec::new(),
            misses: 0,
            converged: false,
            failover: None,
            claim: None,
            release: None,
            revision: false,
            pending_reinits: Vec::new(),
            pending_restarts: Vec::new(),
            fencing_lost: false,
            pending_fencing: Vec::new(),
            pending_superseded: Vec::new(),
            pending_adoption_receipts: Vec::new(),
        }
    }

    /// Starts field ownership on a launched-active peer — the startup
    /// half of the claim contract: takes the field-ownership claim
    /// [`with_field_claim`](Self::with_field_claim) installed, then
    /// lifts the gate, in the same order a promotion runs them. The
    /// caller runs it once at startup, after the claim hook is
    /// installed and before the first scan; until it runs the gate
    /// stays closed — a launched active that cannot take the plant's
    /// single-writer claim does not run unfenced.
    ///
    /// Only a launched `active` activates — any other role is refused
    /// with [`SwitchError::NotActive`] — and a failed claim refuses the
    /// start as [`SwitchError::FieldClaimFailed`] with the gate still
    /// closed. On a peer carrying no claim hook — a private field — the
    /// gate simply lifts.
    pub fn activate(&mut self) -> Result<(), SwitchError> {
        if self.role != Role::Active {
            return Err(SwitchError::NotActive);
        }
        self.lift_gate()
    }

    /// The currently reported role.
    pub fn role(&self) -> Role {
        self.role
    }

    /// The standby's reported convergence — meaningful while the
    /// instance does not own the field.
    pub fn sync_state(&self) -> &StandbySync {
        &self.sync
    }

    /// The tick of the last applied checkpoint — how far the run is
    /// known to be aligned with the active's. Reported in the tracked
    /// stream's own tick domain: after a source restart the run's clock
    /// leads it by the generation offset the resync left.
    pub fn aligned_tick(&self) -> Option<Tick> {
        self.aligned
    }

    /// Whether the instance currently owns field writes — `active`, or
    /// `promoting` (the gate is already lifted).
    pub fn owns_field(&self) -> bool {
        matches!(self.role, Role::Active | Role::Promoting)
    }

    /// Whether the instance accepts operator commands: only a settled
    /// `active` does — a standby's writes are quiesced, and a command
    /// landing mid-transition is refused so the UI retries after
    /// re-polling roles, per the monitoring-under-redundancy decision.
    pub fn accepts_commands(&self) -> bool {
        self.role == Role::Active
    }

    /// The instance's reported role as the serde [`RoleReport`] the
    /// monitoring contract serves.
    pub fn report(&self) -> RoleReport {
        RoleReport {
            role: self.role,
            tick: self.executor.tick(),
            sync: match self.role {
                Role::Active => None,
                _ => Some(self.sync.clone()),
            },
        }
    }

    /// Promotes the instance to field owner: lifts the write gate at the
    /// request's scan boundary and reports `promoting`, settling to
    /// `active` when the first scan under the lifted gate completes.
    ///
    /// Only a converged peer may promote: [`StandbySync::Tracking`]
    /// means a checkpoint has applied cleanly and the staged-output
    /// divergence check has found the run matching the field, so the
    /// next scan writes what the run would have — bumpless by
    /// determinism. [`StandbySync::Reinitialized`] is the revision
    /// path's equivalent: the checkpoint crossed the model boundary
    /// under the documented carryover rule and the carryover report
    /// records exactly what continues — not bumpless by restored state,
    /// but honest about what the revised run starts from. A standby
    /// that has not converged — or that reports
    /// [`StandbySync::Diverged`] — is refused with
    /// [`SwitchError::NotConverged`] carrying the reported state; a
    /// field-owning instance — including a still-settling promotion —
    /// with [`SwitchError::AlreadyActive`]. A refused promotion touches
    /// nothing: the gate stays as it was.
    pub fn promote(&mut self) -> Result<(), SwitchError> {
        match self.role {
            Role::Active | Role::Promoting => return Err(SwitchError::AlreadyActive),
            Role::Standby | Role::Demoting => {}
        }
        if !matches!(
            self.sync,
            StandbySync::Tracking { .. } | StandbySync::Reinitialized { .. }
        ) {
            return Err(SwitchError::NotConverged {
                sync: self.sync.clone(),
            });
        }
        self.lift_gate()?;
        self.change(self.executor.tick(), Role::Promoting);
        Ok(())
    }

    /// The automatic-failover half of [`promote`](Self::promote): when
    /// the checkpoint-pull heartbeat's consecutive misses have reached
    /// the configured [`with_failover`](Self::with_failover) budget and
    /// the convergence proof still stands — the last applied verdict
    /// was `Tracking` or `Reinitialized` and the misses have not
    /// exceeded the budget —
    /// lifts the gate at this boundary exactly as a manual promotion
    /// would, reporting `promoting`.
    ///
    /// Anything else is the named [`SwitchError::NotConverged`]
    /// carrying the reported sync state: an unconverged standby reports
    /// rather than promotes, a single transient miss has not reached
    /// the budget, and an over-budget miss run has voided the
    /// convergence the promotion would rely on. Callers check
    /// [`failover_due`](Self::failover_due) once per scan cycle and
    /// invoke this only then — the boundary at which a self-promotion
    /// lands is the budget-th miss's scan.
    pub fn self_promote(&mut self) -> Result<(), SwitchError> {
        match self.role {
            Role::Active | Role::Promoting => return Err(SwitchError::AlreadyActive),
            Role::Standby | Role::Demoting => {}
        }
        if !(self.failover_due() && self.converged) {
            return Err(SwitchError::NotConverged {
                sync: self.sync.clone(),
            });
        }
        self.lift_gate()?;
        self.change(self.executor.tick(), Role::Promoting);
        Ok(())
    }

    /// Whether the heartbeat's consecutive failed pulls have reached the
    /// configured failover budget — the scan boundary at which a
    /// still-converged standby may [`self_promote`](Self::self_promote).
    /// `false` without a configured budget; misses beyond the budget do
    /// not re-arm — the failover window closed with the convergence
    /// proof.
    pub fn failover_due(&self) -> bool {
        self.failover.is_some_and(|budget| self.misses == budget)
    }

    /// Consecutive checkpoint pulls that produced no applied checkpoint
    /// — the heartbeat miss count the failover budget compares against.
    pub fn missed_transfers(&self) -> u32 {
        self.misses
    }

    /// Demotes the instance to a tracking peer: closes the write gate at
    /// the request's scan boundary — the next scan is already quiesced —
    /// runs the installed field-release hook so the demoted attachments
    /// forget the ownership token they would otherwise re-assert on a
    /// re-attach, and reports `demoting`, settling to `standby` when that
    /// scan completes.
    ///
    /// The same boundary reconciles the command audit the way the
    /// fenced path's [`Executor::supersede_commands`] does, but for the
    /// voluntary handoff: the run's queued commands are *suspended*
    /// ([`Executor::suspend_pending_commands`]) rather than rejected —
    /// their receipts stay `Accepted` in the log so the checkpoints
    /// this peer keeps serving still carry them for the successor's
    /// promotion-boundary pull — while the demoted run's own scans can
    /// no longer apply them: a quiesced scan's `Applied` would journal
    /// an application the gate kept off the field, erased by the next
    /// adoption. Each suspended entry resolves on the tracked line's
    /// checkpoint applies: covered by the adopted log it re-queues and
    /// settles with the run; passed by the adopted window's submission
    /// high-water without being carried, it settles `Rejected`
    /// carrying [`CommandError::Superseded`] and queues for the
    /// journal — never `applied` on the fenced image, never vanished
    /// unaudited. An adoption whose high-water has not reached an
    /// entry's index adjudicates nothing: the tracked source captured
    /// the checkpoint before it observed the submission, so the
    /// unreached tail is restored suspended — still served for the
    /// successor's carry — and the receipt keeps exactly one terminal
    /// settle ahead of it rather than a provisional `superseded` the
    /// next, fresher adoption would contradict.
    ///
    /// Only a field-owning instance demotes; anything else is refused
    /// with [`SwitchError::NotActive`]. The demoted peer's reported
    /// convergence resets to [`StandbySync::Unsynchronized`]: it
    /// reconverges through fresh checkpoints from the new active.
    pub fn demote(&mut self) -> Result<(), SwitchError> {
        match self.role {
            Role::Active | Role::Promoting => {}
            Role::Standby | Role::Demoting => return Err(SwitchError::NotActive),
        }
        if let Some(gate) = self.gate {
            gate.close();
        }
        if let Some(release) = &self.release {
            release.0();
        }
        self.executor.suspend_pending_commands();
        self.sync = StandbySync::Unsynchronized;
        self.aligned = None;
        self.tick_offset = 0;
        self.staged = None;
        self.misses = 0;
        self.converged = false;
        self.change(self.executor.tick(), Role::Demoting);
        Ok(())
    }

    /// One best-effort synchronization at the promotion boundary — a
    /// final checkpoint pull against the tracking source before the
    /// gate lifts, so a command the active admitted up to the promote
    /// request — still `Accepted` in its checkpointed receipt log —
    /// carries into the promoted run and settles at the new active's
    /// next scan boundary rather than being lost to the sub-scan gap
    /// between the last tracking pull and the promote.
    ///
    /// The pull is opportunistic, not a heartbeat cycle: it never counts
    /// a miss and never decides the transition by itself. A field-owning
    /// peer has no source to sync from; a produced-nothing pull changes
    /// nothing; and a checkpoint older than the run's tick is stale —
    /// applying it would rewind scans the peer already ran, re-applying
    /// their commands and re-emitting their events — so its state does
    /// not land. Stale is not empty, though: the receipt log inside the
    /// stale checkpoint may still carry admissions the run lacks — the
    /// driven cadence rests at `aligned + 1`, so the freshest checkpoint
    /// carrying a just-admitted command is exactly this one — and the
    /// boundary adopts that log's new tail instead
    /// ([`Executor::carry_pending_commands`]), the still-`Accepted`
    /// entries queueing on the standing state to settle at the promoted
    /// run's first scan. A checkpoint this build cannot read — an
    /// unsupported format version — is skipped whole.
    /// A landed checkpoint is an ordinary [`transfer`](Self::transfer)
    /// on the executor side — the reinitialization report a
    /// model-boundary crossing owes the journal queues as it would on
    /// any pull — but the standing the promotion check reads is
    /// restored regardless of the outcome, as below.
    ///
    /// The staged `Out` image is set aside for the transfer rather than
    /// carried into its divergence comparison: the staged evidence
    /// describes the tracking line the applied checkpoint abandons —
    /// what the quiesced scan *would* have written — and at the
    /// promotion boundary a same-tick comparison would flag the
    /// one-tick lag a field-carried command write inherently leaves on
    /// a peer that cannot issue it: the standby's own scan ran on the
    /// field's pre-command value, the checkpoint carries the post-write
    /// state, and the promoted run continues from the checkpoint, not
    /// from the staged what-if. Set aside, though — not discarded: a
    /// boundary that ends in refusal leaves the peer tracking, and the
    /// staged image stays the cadence's evidence for the next same-tick
    /// apply — on a diverged peer, the only comparison that can return
    /// it to `Tracking`. Only a transfer that itself crossed a
    /// boundary — a stream regression's new generation, a rolling
    /// revision's new model — retires the image outright.
    ///
    /// The reported standing is the cadence's, whatever the pull does:
    /// the executor side of a landed transfer — state, receipt log,
    /// pending queue, alignment, the miss reset — stays, so the carried
    /// commands settle under the promotion, while the `sync`/
    /// `converged` verdict [`promote`](Self::promote) and
    /// [`self_promote`](Self::self_promote) read restores to what the
    /// tracking cadence last established — a boundary pull neither
    /// manufactures promotability on a peer that never tracked nor
    /// revokes it on one that did.
    pub fn final_sync(&mut self, pull: impl FnOnce() -> Result<Checkpoint, String>) {
        if self.owns_field() {
            return;
        }
        let Ok(checkpoint) = pull() else {
            return;
        };
        if checkpoint.tick < self.executor.tick() {
            // Stale by the run's clock: the state cannot land without
            // rewinding scans this peer already ran, but the receipt
            // log's newer tail still carries — a command the active
            // admitted since this run's last alignment queues here and
            // settles at the promoted run's first scan rather than
            // being lost to the gap the skip used to leave.
            if SUPPORTED_FORMAT_VERSIONS.contains(&checkpoint.format_version) {
                self.executor.carry_pending_commands(&checkpoint);
            }
            return;
        }
        // The staged image is set aside for the boundary transfer, not
        // consumed: the apply must not compare against it (the one-tick
        // lag a field-carried command leaves would flag spuriously),
        // but a boundary that ends in refusal hands it back — it is the
        // cadence's own evidence, still owed to the next same-tick
        // apply, and on a diverged peer it is the only path back to
        // `Tracking`. Only the transfer's own crossings retire it: a
        // regressed stream's staged evidence belongs to the old
        // generation, a reinitialized model's to the old model.
        let staged = self.staged.take();
        let restarts = self.pending_restarts.len();
        let fingerprint = self.executor.model_fingerprint();
        let (sync, converged) = (self.sync.clone(), self.converged);
        let _ = self.transfer(&checkpoint);
        self.sync = sync;
        self.converged = converged;
        if self.pending_restarts.len() == restarts
            && self.executor.model_fingerprint() == fingerprint
        {
            // No generation or model boundary crossed, so the staged
            // image stays the cadence's evidence: the boundary apply
            // ran no comparison against it — it cannot have queued a
            // resolution — and a refused promotion leaves it owed to
            // the next same-tick apply.
            self.staged = staged;
        }
    }

    /// Applies a checkpoint received from the active, aligning the run
    /// at the checkpointed tick — the tracking half of the redundancy
    /// contract, in place on the running executor.
    ///
    /// The run's tick is its journal and history attribution domain and
    /// never rewinds across a source-generation boundary: a checkpoint
    /// whose tick fell below the run's last alignment — or below the
    /// run's own tick before any alignment stood — while naming a
    /// generation the run's own stream does not carry is not a
    /// continuation of the tracked line but the signature of a
    /// cold-restarted or replaced source beginning a new tick
    /// generation. Its state still applies — the tracked source is the
    /// live one — but the run resumes it at the run's current tick, the
    /// `tick + tick_offset` landing every later checkpoint on the new
    /// stream takes, and one [`SourceRestart`] queues for the journal:
    /// a restart that rewound nothing still changes what the stream
    /// means. A checkpoint on the tracked line itself — a repeat or a
    /// post-miss catch-up, which may still lag the run's tick — keeps
    /// the standing offset: that realignment is the line's own, not a
    /// new generation. And a regression that names the run's own
    /// generation — a demoted peer's first pull on its uninterrupted
    /// successor, the demotion having cleared the alignment the tick
    /// comparison stood on — is the peer's tracking reset rather than
    /// the source's restart: the state adopts under the same
    /// monotone-clock rule, but no boundary crossed, so nothing
    /// journals. Kept, but re-evaluated — the offset covers at
    /// most the run's live lead over the stream
    /// ([`stream_offset`](Self::stream_offset)), so an apply can hold
    /// the run's tick or realign it backward but never land it ahead
    /// of both clocks.
    ///
    /// A field-owning instance refuses with [`ApplyError::OwnsField`]: a
    /// checkpoint landing on the active would clobber the run it is
    /// writing. On a tracking peer a mismatched checkpoint is rejected
    /// with [`ApplyError::Restore`], the executor rolls back to its
    /// pre-apply state, and the peer reports
    /// [`StandbySync::Degraded`] — recoverable by the next good
    /// transfer.
    ///
    /// A successful apply also runs the standby-divergence check when
    /// the tick the checkpoint landed at equals the stashed staged
    /// image's tick —
    /// under the documented pull-per-scan cadence that pairing lands one
    /// transfer after the staged scan, the stated detection bound: the
    /// field then holds the active's write for the tick the staged image
    /// describes. Mismatches move the peer to
    /// [`StandbySync::Diverged`] and queue a
    /// [`DivergenceReport`] for the journal — one per transition, not
    /// per transfer — while a comparison on a diverged peer that read
    /// every staged point's field value and matched is the resync that
    /// returns it to [`StandbySync::Tracking`], queued for the journal
    /// as a [`ResolutionReport`] carrying every compared point's
    /// evidence. `Diverged` is the promotion-blocking "my staged
    /// outputs differ from the field" verdict and clears only on that
    /// positive evidence: an apply that ran no comparison — a staged
    /// image the checkpoint stream has not caught up to, or has
    /// overtaken — and a comparison whose field reads failed both
    /// observe nothing, and the verdict stands. A staged image the
    /// checkpoint stream has not caught up to — or has overtaken — is
    /// discarded: only a same-tick comparison is honest evidence.
    pub fn apply(&mut self, checkpoint: &Checkpoint) -> Result<(), ApplyError> {
        if self.owns_field() {
            return Err(ApplyError::OwnsField);
        }
        // The pull produced a checkpoint — the active served, so it is
        // alive: the heartbeat miss count resets whether the apply
        // lands or is rejected.
        self.misses = 0;
        // The still-`Accepted` receipts this log holds — the demoted
        // run's suspended commands and any adopted pending entries —
        // are what the adoption below either covers, passes by, or
        // leaves unrestored behind its high-water.
        let pending = self.pending_accepted();
        // The standing force set is the adoption audit's baseline: a
        // force change the merged receipt log cannot account for is the
        // adoption's own doing and journals naming the source.
        let prior_forces = self.executor.forces().clone();
        let (offset, regressed) = self.stream_offset(checkpoint);
        // A regression is a source restart only when the stream's
        // generation provably differs from the run's own: a demoted
        // peer cleared its own alignment, so its first pull on the
        // uninterrupted successor — still stamping the generation this
        // run's own captures carried — regresses in tick without the
        // source restarting, and attributing that self-caused reset to
        // the source is the journal lie the generation check exists to
        // prevent.
        let boundary = regressed
            && Self::generation_boundary(self.executor.generation(), checkpoint.generation);
        let landed = Tick(checkpoint.tick.0 + offset);
        let adopted;
        let applied = if landed == checkpoint.tick {
            self.executor.apply(checkpoint)
        } else {
            adopted = Checkpoint {
                tick: landed,
                ..checkpoint.clone()
            };
            self.executor.apply(&adopted)
        };
        match applied {
            Ok(()) => {
                self.note_abandoned_commands(pending);
                self.note_adopted_forces(&prior_forces, checkpoint, landed);
                self.tick_offset = offset;
                if regressed {
                    // The staged image pairs its run tick against the
                    // stream position it was staged on — a regressed
                    // apply moved that pairing, so the evidence cannot
                    // compare and is discarded whether or not the
                    // generation changed. Only the generation boundary
                    // journals: a same-generation regression is the
                    // peer's own tracking reset, not the source's
                    // restart.
                    self.staged = None;
                    if boundary {
                        self.pending_restarts.push(SourceRestart {
                            tick: landed,
                            was_aligned: self.aligned,
                            resumed_at: checkpoint.tick,
                        });
                    }
                }
                self.aligned = Some(checkpoint.tick);
                let was_diverged = matches!(self.sync, StandbySync::Diverged { .. });
                // The field evidence this apply carries: a comparison
                // runs only when the checkpoint landed at the stashed
                // staged image's tick — any other apply performs zero
                // reads — and `compared` answers only the reads that
                // succeeded, so `compared.len() != staged.len()` marks
                // the field partially observed. Neither is the
                // field-matching evidence a standing `Diverged` verdict
                // clears on: only a fully-read same-tick match returns
                // a diverged peer to `Tracking`.
                let comparison = match self.staged.take() {
                    Some((tick, staged)) if tick == landed => Some((
                        tick,
                        staged.len(),
                        compare_staged_points(self.executor.driver(), &staged),
                    )),
                    _ => None,
                };
                match comparison {
                    Some((tick, staged_len, compared)) => {
                        let mismatches: Vec<Divergence> = compared
                            .iter()
                            .copied()
                            .filter(|point| values_diverge(point.staged, point.field))
                            .collect();
                        if !mismatches.is_empty() {
                            if !was_diverged {
                                self.pending_divergences.push(DivergenceReport {
                                    tick,
                                    mismatches: mismatches.clone(),
                                });
                            }
                            self.sync = StandbySync::Diverged { mismatches };
                            self.converged = false;
                        } else if compared.len() == staged_len {
                            // Every staged point's field read succeeded
                            // and none mismatched — the positive
                            // evidence a diverged peer's resync stands
                            // on, journaled as its own named event.
                            if was_diverged {
                                self.pending_resolutions
                                    .push(ResolutionReport { tick, compared });
                            }
                            self.sync = StandbySync::Tracking {
                                aligned: checkpoint.tick,
                            };
                            self.converged = true;
                        } else if !was_diverged {
                            // Some field reads failed: evidence-free for
                            // a standing `Diverged` verdict, which
                            // stands — but a tracking peer is not
                            // convicted on an unobserved field either.
                            self.sync = StandbySync::Tracking {
                                aligned: checkpoint.tick,
                            };
                            self.converged = true;
                        }
                    }
                    None => {
                        // No same-tick comparison ran: the apply carries
                        // no field-matching evidence, so a standing
                        // `Diverged` verdict stands while any other
                        // state reconverges to `Tracking`.
                        if !was_diverged {
                            self.sync = StandbySync::Tracking {
                                aligned: checkpoint.tick,
                            };
                            self.converged = true;
                        }
                    }
                }
                Ok(())
            }
            Err(error) => {
                self.sync = StandbySync::Degraded {
                    detail: error.to_string(),
                };
                self.converged = false;
                Err(ApplyError::Restore(error))
            }
        }
    }

    /// The generation offset `checkpoint` applies under — the run
    /// tick's lead over the tracked stream's own tick — and whether
    /// the stream regressed: a checkpoint whose tick fell below the
    /// run's last alignment, or below the run's own tick before any
    /// alignment stood, is the signature of a source-side reset — a
    /// cold-restarted or replaced source, or the peer's own demotion
    /// clearing the alignment the comparison stood on. The generation
    /// check in [`apply`](Self::apply) separates the two; the offset
    /// mechanics are the same either way. The
    /// offset a regression resets to — `run - checkpoint.tick` —
    /// lands the apply at the run's current tick, so the run's clock
    /// never rewinds scans it already ran and journaled; a
    /// same-generation pull keeps the standing offset, and a first
    /// alignment at or above the run's tick lands at the checkpoint's
    /// own.
    ///
    /// The standing offset is re-evaluated against the run's live lead
    /// on every same-generation apply rather than carried verbatim: it
    /// exists to keep the run's clock monotone across the restart that
    /// seeded it, so it may cover at most the gap that remains. A
    /// tracked stream still below the run's tick lands the apply at
    /// the run's tick — never ahead of it — and one that has recovered
    /// to or past the run's tick clears the offset entirely, so the
    /// run's clock rejoins the stream's domain. A stale offset can
    /// therefore realign the run backward onto the line or hold it in
    /// place, but it can never land the apply ahead of both clocks —
    /// the bound that keeps two mutually tracking peers' seeded
    /// offsets from feeding back into each other's served ticks.
    fn stream_offset(&self, checkpoint: &Checkpoint) -> (u64, bool) {
        let run = self.executor.tick();
        let regressed = match self.aligned {
            Some(aligned) => checkpoint.tick < aligned,
            None => checkpoint.tick < run,
        };
        if regressed {
            (run.0 - checkpoint.tick.0, true)
        } else if self.aligned.is_none() {
            (0, false)
        } else {
            (
                self.tick_offset
                    .min(run.0.saturating_sub(checkpoint.tick.0)),
                false,
            )
        }
    }

    /// Whether a regressed checkpoint stream crossed a source
    /// generation boundary — the condition [`apply`](Self::apply) and
    /// [`reinitialize`](Self::reinitialize) journal a
    /// [`SourceRestart`] on. `own` is the generation this run's
    /// executor currently stamps, `checkpoint`'s the pulled stream's:
    /// only a checkpoint whose generation positively differs proves
    /// the source began a new tick domain. A demoted peer's first pull
    /// on its uninterrupted successor regresses on the generation its
    /// own captures stamped — the tracking reset was the peer's, so
    /// nothing journals. Either side unidentified — a checkpoint a
    /// pre-generation build wrote, or a run never given a generation —
    /// can prove no continuation, so the regression journals as a
    /// restart exactly as it always did.
    fn generation_boundary(own: Option<u64>, checkpoint: Option<u64>) -> bool {
        own.is_none_or(|own| Some(own) != checkpoint)
    }

    /// The log's still-`Accepted` receipts with their absolute
    /// submission indices — the pending set a checkpoint adoption
    /// either covers or abandons.
    fn pending_accepted(&self) -> Vec<(u64, CommandReceipt)> {
        let base = self.executor.receipt_base();
        self.executor
            .receipts()
            .iter()
            .enumerate()
            .filter(|(_, receipt)| matches!(receipt.outcome, CommandOutcome::Accepted { .. }))
            .map(|(index, receipt)| (base + index as u64, receipt.clone()))
            .collect()
    }

    /// Reconciles the pending commands a successful adoption left
    /// behind: the adopted receipt log is the line's one audit, so an
    /// entry this run still held `Accepted` that the new log does not
    /// carry — at its absolute index, as the same command — can never
    /// apply here: the gate quiesces this run's writes. It settles
    /// `Rejected` carrying [`CommandError::Superseded`] and queues for
    /// the journal rather than vanishing unaudited. A covered entry's
    /// outcome is the line's own — re-queued still `Accepted`, or
    /// already settled on the tracked run — and needs nothing.
    ///
    /// Absent means adjudicated, not merely unseen: the adoption keeps
    /// every prior receipt at or beyond its window's high-water — the
    /// submissions the tracked source had not observed at capture —
    /// so the entries reaching this reconciliation as missing are
    /// exactly the ones the line's submission sequence passed without
    /// carrying. The suspended tail a stale checkpoint could not see
    /// never arrives here to settle `superseded` provisionally and be
    /// contradicted by the next, fresher adoption.
    fn note_abandoned_commands(&mut self, pending: Vec<(u64, CommandReceipt)>) {
        let base = self.executor.receipt_base();
        for (index, receipt) in pending {
            let carried = index.checked_sub(base).is_some_and(|position| {
                self.executor
                    .receipts()
                    .get(position as usize)
                    .is_some_and(|adopted| adopted.command == receipt.command)
            });
            if !carried {
                self.pending_superseded.push(CommandReceipt {
                    command: receipt.command.clone(),
                    outcome: CommandOutcome::Rejected {
                        reason: CommandError::Superseded {
                            point: receipt.command.point(),
                        },
                    },
                    actor: receipt.actor,
                });
            }
        }
    }

    /// Audits the force-set changes a successful adoption made: every
    /// point whose standing force the adoption changed — stood up,
    /// re-valued, or dropped — must trace to the merged receipt log,
    /// the pair's one command audit. `Executor::adopt_receipts`
    /// already re-asserts this run's unreached settled verdicts over
    /// the adopted image, so what remains here is the residual the
    /// receipted-command contract names: a change whose newest settled
    /// verdict for the point does not produce the adopted state has
    /// the adoption itself as its only cause, and it must not stand
    /// silently — the QA finding's quality-only resurrection. Each
    /// such change queues a [`CommandReceipt`] carrying the equivalent
    /// force or release `Applied` at the landing tick, its `actor`
    /// naming the adopting source, so the durable journal always
    /// answers "who re-stood this force". Receipted changes journal
    /// through the ordinary settle-diff path and queue nothing here.
    fn note_adopted_forces(
        &mut self,
        prior: &BTreeMap<PointId, Value>,
        checkpoint: &Checkpoint,
        landed: Tick,
    ) {
        let post = self.executor.forces();
        let changed: BTreeSet<PointId> = prior.keys().chain(post.keys()).copied().collect();
        for point in changed {
            let adopted = post.get(&point).copied();
            if prior.get(&point).copied() == adopted {
                continue;
            }
            if receipted_force_state(self.executor.receipts(), point) == Some(adopted) {
                continue;
            }
            let command = match adopted {
                Some(value) => Command::ForcePoint {
                    point,
                    kind: value.kind(),
                    value,
                },
                None => Command::UnforcePoint { point },
            };
            self.pending_adoption_receipts.push(CommandReceipt {
                command,
                outcome: CommandOutcome::Applied { tick: landed },
                actor: Some(adoption_actor(checkpoint)),
            });
        }
    }

    /// Consumes one pulled checkpoint — the standby's transfer entry
    /// point, covering both convergence and the rolling model revision.
    ///
    /// A field-owning instance refuses with [`ApplyError::OwnsField`].
    /// Otherwise the checkpoint's model fingerprint routes it: equal to
    /// this run's means ordinary convergence — [`apply`](Self::apply)
    /// and [`Transfer::Applied`] — while a different fingerprint on a
    /// revision-armed peer ([`with_revision`](Self::with_revision)) is
    /// the rolling deployment of the model-revision decision:
    /// [`reinitialize`](Self::reinitialize) crosses the boundary and
    /// [`Transfer::Reinitialized`] carries its report. A foreign
    /// fingerprint on an unarmed peer is an ordinary
    /// [`ApplyError::Restore`]-wrapped
    /// [`RestoreError::FingerprintMismatch`] — the fingerprint gate is
    /// not weakened for peers that did not opt in.
    pub fn transfer(&mut self, checkpoint: &Checkpoint) -> Result<Transfer, ApplyError> {
        if self.owns_field() {
            return Err(ApplyError::OwnsField);
        }
        if self.revision && checkpoint.model_fingerprint != self.executor.model_fingerprint() {
            return self
                .reinitialize(checkpoint)
                .map(|report| Transfer::Reinitialized(Box::new(report)));
        }
        self.apply(checkpoint).map(|()| Transfer::Applied)
    }

    /// Crosses the model boundary with a checkpoint captured under a
    /// different model — the rolling-revision half of the transfer
    /// contract, in place on the running executor assembled under the
    /// revised model.
    ///
    /// The carryover rule is documented in [`crate::revision`]: operator
    /// values and the output image carry by declared identity, forces
    /// carry all-or-nothing, component and driver state reinitialize,
    /// and the tick resumes at the checkpoint's. Success reports
    /// [`StandbySync::Reinitialized`] carrying the
    /// [`CarryoverReport`] — a promotable state: the documented
    /// switchover order (demote the old peer, then promote) moves the
    /// field writer to the revised model at a scan boundary with
    /// exactly-one-writer preserved throughout.
    ///
    /// A checkpoint breaking the rule is refused with
    /// [`ApplyError::Carryover`] naming the element and reason, nothing
    /// applies, and the peer reports [`StandbySync::Degraded`] — the
    /// failure lands before promotion, so the old active keeps the
    /// field. A field-owning instance refuses with
    /// [`ApplyError::OwnsField`]. Each successful pull refreshes the
    /// crossing — the report always describes the latest checkpoint —
    /// and the journal queue takes one entry per transition into
    /// `Reinitialized`, not per pull.
    pub fn reinitialize(&mut self, checkpoint: &Checkpoint) -> Result<CarryoverReport, ApplyError> {
        if self.owns_field() {
            return Err(ApplyError::OwnsField);
        }
        // A produced checkpoint means the active served — the heartbeat
        // miss count resets as in `apply`, whether the crossing lands.
        self.misses = 0;
        // The same monotone-clock rule as `apply`: a regressed stream's
        // crossing lands at the run's current tick, not the
        // checkpoint's own — and the resync queues for the journal
        // beside the carryover report.
        // The still-`Accepted` receipts this log holds are what the
        // crossing either carries or abandons, exactly as in `apply`.
        let pending = self.pending_accepted();
        // The force-set adoption audit runs the same diff `apply`
        // does — a carried force set answers to the merged receipt
        // log, and an unbacked change journals naming the source.
        let prior_forces = self.executor.forces().clone();
        let (offset, regressed) = self.stream_offset(checkpoint);
        // As in `apply`: the regression journals a `SourceRestart` only
        // when the stream's generation differs from the run's own — a
        // demoted peer's tracking reset regresses on the same
        // generation and crosses no source boundary.
        let boundary = regressed
            && Self::generation_boundary(self.executor.generation(), checkpoint.generation);
        let landed = Tick(checkpoint.tick.0 + offset);
        let adopted;
        let applied = if landed == checkpoint.tick {
            self.executor.reinitialize(checkpoint)
        } else {
            adopted = Checkpoint {
                tick: landed,
                ..checkpoint.clone()
            };
            self.executor.reinitialize(&adopted)
        };
        match applied {
            Ok(report) => {
                self.note_abandoned_commands(pending);
                self.note_adopted_forces(&prior_forces, checkpoint, landed);
                self.tick_offset = offset;
                if boundary {
                    self.pending_restarts.push(SourceRestart {
                        tick: landed,
                        was_aligned: self.aligned,
                        resumed_at: checkpoint.tick,
                    });
                }
                if !matches!(self.sync, StandbySync::Reinitialized { .. }) {
                    self.pending_reinits.push(report.clone());
                }
                self.sync = StandbySync::Reinitialized {
                    report: Box::new(report.clone()),
                };
                self.aligned = Some(checkpoint.tick);
                // Staged evidence belongs to the old alignment — the
                // divergence check does not pair against a crossing.
                self.staged = None;
                self.converged = true;
                Ok(report)
            }
            Err(error) => {
                self.sync = StandbySync::Degraded {
                    detail: error.to_string(),
                };
                self.converged = false;
                Err(ApplyError::Carryover(error))
            }
        }
    }

    /// Marks the tracking peer [`Degraded`](StandbySync::Degraded)
    /// after a transfer failure that produced no checkpoint at all — an
    /// unreachable active or a refused request — and counts the
    /// heartbeat miss toward the failover budget. Misses beyond the
    /// budget void the convergence proof a self-promotion would rely
    /// on: the failover window closes with it.
    ///
    /// A produced-nothing pull carries no field evidence, so a standing
    /// [`Diverged`](StandbySync::Diverged) verdict stands through the
    /// miss: the peer's promotability-blocking truth — its staged
    /// outputs differ from the field — is unresolved until a same-tick
    /// comparison reads the field and matches, while the miss still
    /// counts toward the failover budget and reports in
    /// [`TrackReport::Missed`].
    pub fn note_transfer_failed(&mut self, detail: impl fmt::Display) {
        self.misses += 1;
        if self.failover.is_some_and(|budget| self.misses > budget) {
            self.converged = false;
        }
        if !matches!(self.sync, StandbySync::Diverged { .. }) {
            self.sync = StandbySync::Degraded {
                detail: detail.to_string(),
            };
        }
    }

    /// Runs the standby's per-scan tracking cycle — the once-per-scan
    /// checkpoint pull the peer-transport decision documents — with the
    /// whole sequence consolidated beside the state it coordinates: the
    /// owns-field gate, the pull routed through
    /// [`transfer`](Self::transfer) or counted as a heartbeat miss by
    /// [`note_transfer_failed`](Self::note_transfer_failed), and the
    /// promote-on-budget sequence of [`self_promote`](Self::self_promote)
    /// when the miss run reaches the
    /// [`with_failover`](Self::with_failover) budget.
    ///
    /// `pull` fetches the active's checkpoint — over the monitoring
    /// transport in the controller, a stub in tests — answering the
    /// produced checkpoint or the `Err` detail the peer reports as
    /// [`StandbySync::Degraded`]. A produced checkpoint resets the miss
    /// count whether the apply lands or is refused; a produced-nothing
    /// pull increments it, and the budget-th miss runs the
    /// self-promotion check at this boundary. The [`TrackReport`]
    /// describes what the cycle did, for the caller's logs; transitions
    /// the cycle queued — divergences, reinitializations, role changes —
    /// still drain through the `take_*` queues for the journal.
    ///
    /// The caller's scan cycle waits on `pull`, so a pull that can
    /// block on the network must be bounded or run on a fetch worker —
    /// `dcs_monitor::CheckpointPuller` consumes a dedicated thread's
    /// completed fetch per cycle and answers a cycle whose pull is
    /// still in flight with the same `Err` a refused fetch produces,
    /// keeping the failover window at budget × scan period whatever
    /// the fetch latency.
    ///
    /// A field-owning peer performs no pull and no failover check:
    /// [`TrackReport::OwnsField`], and `pull` is never invoked. The scan
    /// itself stays caller-owned — this is the pre-scan tracking half.
    pub fn track_once(&mut self, pull: impl FnOnce() -> Result<Checkpoint, String>) -> TrackReport {
        if self.owns_field() {
            return TrackReport::OwnsField;
        }
        let detail = match pull() {
            Ok(checkpoint) => {
                return match self.transfer(&checkpoint) {
                    Ok(transfer) => TrackReport::Applied(transfer),
                    Err(error) => TrackReport::Refused(error),
                };
            }
            Err(detail) => detail,
        };
        self.note_transfer_failed(&detail);
        if !self.failover_due() {
            return TrackReport::Missed { detail };
        }
        match self.self_promote() {
            Ok(()) => TrackReport::Promoted {
                detail,
                report: self.report(),
            },
            Err(error) => TrackReport::PromotionRefused { detail, error },
        }
    }

    /// Runs one scan and settles a pending role transition: the first
    /// completed scan after a promotion or demotion ends the reported
    /// transition (`promoting` → `active`, `demoting` → `standby`).
    /// Because the gate moved at the request's boundary, this scan
    /// already runs under the new field-write mode.
    ///
    /// While the peer does not own the field the scan's staged field
    /// `Out` image is stashed for the divergence check
    /// [`apply`](Self::apply) runs; a field-owning peer stages nothing —
    /// its writes are the field's truth.
    ///
    /// A field-owning scan whose write the shared field fenced —
    /// [`IoError::Fenced`](dcs_core::IoError::Fenced), meaning the claim
    /// this peer held was preempted by another attachment — completes
    /// degraded like any field fault: the refusal counts in `io_health`
    /// and one [`FencingLoss`] queues for the journal. But the peer is
    /// superseded, and a degraded report alone would leave it still
    /// believing it owns the field — still writing into the fence: the
    /// demote path runs in place, re-closing the gate and running the
    /// release hook so a re-attaching link cannot re-assert the claim
    /// this peer just lost, the reported role moving to `demoting` and
    /// settling `standby` on the first quiesced scan that completes.
    /// A misordered promotion or a rogue claim cannot kill or
    /// split-brain a running field owner.
    ///
    /// The same demotion reconciles the command audit: commands the
    /// still-reporting-`active` peer accepted between the preemption
    /// and this detection scan applied at its head onto an image the
    /// field never saw — the superseding owner does not carry them —
    /// so [`Executor::supersede_commands`] rewrites the boundary's
    /// `Applied` settlements `Rejected` with
    /// [`CommandError::Superseded`](dcs_core::CommandError::Superseded)
    /// before the journaled `CommandSettled` would echo a phantom
    /// application.
    ///
    /// A scan that does not own the field runs quiesced
    /// ([`Executor::scan_quiesced`]): it still reads, steps, writes
    /// (dropped at the closed gate), and stages its field `Out` image
    /// for the divergence check — but it applies no commands. An
    /// adopted still-`Accepted` receipt is carried for a possible
    /// promotion, not settled here: a quiesced scan must not mint an
    /// `Applied` the line never ordered, on an image the field never
    /// sees. The carried entries settle once at the promoted run's
    /// first field-owning scan.
    pub fn scan(&mut self) -> Tick {
        let quiesced = !self.owns_field();
        let tick = if quiesced {
            self.executor.scan_quiesced()
        } else {
            self.executor.scan()
        };
        if self.owns_field()
            && let Some(point) = self.executor.fenced_write()
        {
            if !self.fencing_lost {
                self.fencing_lost = true;
                self.pending_fencing.push(FencingLoss { tick, point });
            }
            // Superseded: the field's single-writer claim belongs to
            // another attachment now. The scan completed degraded; the
            // demote path is the survivable answer — the gate
            // re-closes, the release hook forgets the recorded
            // ownership token, and the reported role walks `demoting`
            // to `standby`. The fenced scan ran under the lifted gate,
            // so it does not settle the transition — the first
            // quiesced scan does. Commands the fenced boundary applied
            // reconcile first: the image they changed is the abandoned
            // run's, so they settle superseded rather than applied.
            self.executor.supersede_commands(tick);
            self.demote().expect("a field-owning peer demotes");
            return tick;
        }
        match self.role {
            Role::Promoting => self.change(tick, Role::Active),
            Role::Demoting => self.change(tick, Role::Standby),
            _ => {}
        }
        if self.owns_field() {
            self.staged = None;
        } else {
            self.staged = Some((tick, self.executor.staged_field_outputs()));
        }
        tick
    }

    /// Drains reported-role transitions queued since the last call — for
    /// the transition journal the monitoring layer records them into.
    pub fn take_role_changes(&mut self) -> Vec<RoleChange> {
        std::mem::take(&mut self.pending_changes)
    }

    /// Drains divergence detections queued since the last call — one
    /// [`DivergenceReport`] per transition into
    /// [`StandbySync::Diverged`], each carrying the tick the staged
    /// image belonged to — for the transition journal the monitoring
    /// layer records them into.
    pub fn take_divergences(&mut self) -> Vec<DivergenceReport> {
        std::mem::take(&mut self.pending_divergences)
    }

    /// Drains divergence resolutions queued since the last call — one
    /// [`ResolutionReport`] per `Diverged` → [`StandbySync::Tracking`]
    /// transition, each carrying the applied tick and the same-tick
    /// field comparison the clear stands on — for the transition
    /// journal the monitoring layer records them into.
    pub fn take_resolutions(&mut self) -> Vec<ResolutionReport> {
        std::mem::take(&mut self.pending_resolutions)
    }

    /// Drains reinitializations queued since the last call — one
    /// [`CarryoverReport`] per transition into
    /// [`StandbySync::Reinitialized`] — for the transition journal the
    /// monitoring layer records them into.
    pub fn take_reinitializations(&mut self) -> Vec<CarryoverReport> {
        std::mem::take(&mut self.pending_reinits)
    }

    /// Drains field-claim losses queued since the last call — one
    /// [`FencingLoss`] per observed preemption of the claim this peer
    /// held — for the transition journal the monitoring layer records
    /// them into.
    pub fn take_fencing_losses(&mut self) -> Vec<FencingLoss> {
        std::mem::take(&mut self.pending_fencing)
    }

    /// Drains tracked-source restarts queued since the last call — one
    /// [`SourceRestart`] per regressed-stream adoption that crossed a
    /// generation boundary — for the transition journal the monitoring
    /// layer records them into.
    pub fn take_source_restarts(&mut self) -> Vec<SourceRestart> {
        std::mem::take(&mut self.pending_restarts)
    }

    /// Drains pending-command settlements queued since the last call —
    /// one [`CommandReceipt`] rewritten to `Rejected` carrying
    /// [`CommandError::Superseded`] per still-`Accepted` entry a
    /// checkpoint adoption abandoned — for the settle journal the
    /// monitoring layer records them into through
    /// `Recorder::note_settled`.
    pub fn take_superseded_commands(&mut self) -> Vec<CommandReceipt> {
        std::mem::take(&mut self.pending_superseded)
    }

    /// Drains the adoption-audit receipts queued since the last call —
    /// one [`CommandReceipt`] per force-set change a checkpoint
    /// adoption made that no settled receipt accounts for, each
    /// `Applied` at the landing tick with `actor` naming the adopting
    /// checkpoint — for the settle journal the monitoring layer
    /// records them into through `Recorder::note_settled`, beside the
    /// superseded settlements.
    pub fn take_adoption_receipts(&mut self) -> Vec<CommandReceipt> {
        std::mem::take(&mut self.pending_adoption_receipts)
    }

    /// Queues `command` for application at the next scan boundary —
    /// forwarded to the executor; [`accepts_commands`](Self::accepts_commands)
    /// is the role check callers apply first.
    pub fn submit_command(&mut self, command: Command) -> CommandReceipt {
        self.executor.submit_command(command)
    }

    /// The attributed variant of [`submit_command`](Self::submit_command):
    /// the receipt — and the journaled `CommandSettled` echoing it —
    /// carries the submitter's declared actor identity.
    pub fn submit_command_as(&mut self, command: Command, actor: Option<String>) -> CommandReceipt {
        self.executor.submit_command_as(command, actor)
    }

    /// The wrapped executor, e.g. for snapshots, receipts, and
    /// checkpoints.
    pub fn executor(&self) -> &Executor<'d> {
        &self.executor
    }

    /// The executor's current tick.
    pub fn tick(&self) -> Tick {
        self.executor.tick()
    }

    /// A monitoring snapshot of the run.
    pub fn snapshot(&self) -> TelemetrySnapshot {
        self.executor.snapshot()
    }

    /// The executor's receipt log — its retained, bounded tail.
    pub fn receipts(&self) -> &[CommandReceipt] {
        self.executor.receipts()
    }

    /// The absolute submission index of `receipts()[0]` — the count of
    /// settled receipts already evicted; see
    /// [`Executor::receipt_base`].
    pub fn receipt_base(&self) -> u64 {
        self.executor.receipt_base()
    }

    /// Records one scan cycle that overran its wall-clock period —
    /// forwarded to the executor; the pacing shell calls this through
    /// whichever wrapper it scans through.
    pub fn record_scan_overrun(&mut self) {
        self.executor.record_scan_overrun();
    }

    /// The executor's current transferable state.
    pub fn checkpoint(&self) -> Checkpoint {
        self.executor.checkpoint()
    }

    /// Consumes the peer and returns the executor.
    pub fn into_executor(self) -> Executor<'d> {
        self.executor
    }

    /// Takes the field's write-ownership claim when one is installed —
    /// the fencing arbitration a promotion relies on — then lifts the
    /// gate. A failed claim refuses the promotion as
    /// [`SwitchError::FieldClaimFailed`]: the peer that cannot take the
    /// field's single-writer arbitration does not take the field, and
    /// the gate stays closed.
    fn lift_gate(&mut self) -> Result<(), SwitchError> {
        if let Some(claim) = &self.claim {
            claim.0().map_err(|detail| SwitchError::FieldClaimFailed { detail })?;
        }
        if let Some(gate) = self.gate {
            gate.open();
        }
        // A fresh claim re-arms the loss report — a fenced write under
        // this ownership is a new event, not a repeat of a prior one.
        self.fencing_lost = false;
        Ok(())
    }

    /// The reported-role change bookkeeping: `from` is the previously
    /// reported role, recorded at `tick`, and the journal entry follows.
    fn change(&mut self, tick: Tick, to: Role) {
        let from = std::mem::replace(&mut self.role, to);
        self.pending_changes.push(RoleChange { tick, from, to });
    }
}

/// The force state `receipts`' newest *settled* verdict for `point`
/// accounts for: `Some(Some(value))` an applied force standing,
/// `Some(None)` an applied release, `None` no settled verdict at all.
/// `Accepted` and `Rejected` entries move no force set and back
/// nothing — the newest `Applied` verdict is the receipted truth a
/// post-adoption force set must match to be considered commanded
/// rather than resurrected.
fn receipted_force_state(receipts: &[CommandReceipt], point: PointId) -> Option<Option<Value>> {
    receipts.iter().rev().find_map(|receipt| {
        if !matches!(receipt.outcome, CommandOutcome::Applied { .. }) {
            return None;
        }
        match &receipt.command {
            Command::ForcePoint {
                point: p, value, ..
            } if *p == point => Some(Some(*value)),
            Command::UnforcePoint { point: p } if *p == point => Some(None),
            _ => None,
        }
    })
}

/// The actor identity an adoption-audit receipt declares — the
/// adopting source the receipted-command contract names: the pulled
/// checkpoint's stream generation and capture tick, so the journal
/// attributes a receiptless force change to the image it came from
/// rather than to a command nobody issued.
fn adoption_actor(checkpoint: &Checkpoint) -> String {
    match checkpoint.generation {
        Some(generation) => format!("checkpoint:{generation:x}@{}", checkpoint.tick.0),
        None => format!("checkpoint@{}", checkpoint.tick.0),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{Component, ComponentIo, ComponentIoExt, IoRequirement, PointMap, StepError};
    use dcs_core::{
        CommandArgument, CommandAvailability, CommandDecl, CommandError, CommandOutcome,
        ComponentDescriptor, Direction, Divergence, EmittedEvent, EventDecl, EventField,
        EventFieldKind, EventRetention, EventValue, IoDriver, IoError, IoFault, PointId, Sample,
        StateMap, Value, ValueKind,
    };
    use std::collections::HashMap;
    use std::sync::Mutex;
    use std::sync::atomic::{AtomicBool, Ordering};

    /// Minimal in-memory driver for role-machine tests: points, reads,
    /// writes — the gate semantics under test live in `WriteGate`.
    struct StubDriver {
        points: Mutex<HashMap<PointId, Sample>>,
    }

    impl StubDriver {
        fn new(point: PointId, value: Value) -> Self {
            Self::field(&[(point, value)])
        }

        /// A multi-point field for divergence tests.
        fn field(points: &[(PointId, Value)]) -> Self {
            Self {
                points: Mutex::new(
                    points
                        .iter()
                        .map(|&(point, value)| (point, Sample::good(value, Tick::ZERO)))
                        .collect(),
                ),
            }
        }

        fn value(&self, point: PointId) -> Value {
            self.points.lock().unwrap()[&point].value
        }
    }

    impl IoDriver for StubDriver {
        fn read(&self, point: PointId) -> Result<Sample, IoError> {
            self.points
                .lock()
                .unwrap()
                .get(&point)
                .copied()
                .ok_or(IoError::UnknownPoint(point))
        }

        fn write(&self, point: PointId, value: Value) -> Result<(), IoError> {
            let mut points = self.points.lock().unwrap();
            let stored = points.get_mut(&point).ok_or(IoError::UnknownPoint(point))?;
            *stored = Sample::good(value, stored.tick);
            Ok(())
        }
    }

    /// A componentless executor over `driver` — checkpoints and scans
    /// still work; convergence reduces to the tick.
    fn executor<'d>(driver: &'d (dyn IoDriver + Sync)) -> Executor<'d> {
        Executor::new(driver, crate::PointMap::new(), Vec::new()).unwrap()
    }

    #[test]
    fn promotion_requires_a_converged_standby() {
        let driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let gate = WriteGate::closed(&driver);
        let mut peer = Peer::standby(executor(&gate), Some(&gate));

        // Premature promotion is a named error, not a take-over.
        assert_eq!(
            peer.promote(),
            Err(SwitchError::NotConverged {
                sync: StandbySync::Unsynchronized
            })
        );
        assert_eq!(peer.role(), Role::Standby);
        assert!(!gate.is_open());
    }

    #[test]
    fn promotion_lifts_the_gate_and_settles_on_the_next_scan() {
        let driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let gate = WriteGate::closed(&driver);
        let mut peer = Peer::standby(executor(&gate), Some(&gate));

        // A checkpoint from an equivalent run converges the standby.
        let source_driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let mut source = executor(&source_driver);
        source.run(7);
        peer.apply(&source.checkpoint()).unwrap();
        assert_eq!(
            peer.sync_state(),
            &StandbySync::Tracking { aligned: Tick(7) }
        );
        assert_eq!(peer.aligned_tick(), Some(Tick(7)));

        peer.promote().unwrap();
        assert_eq!(peer.role(), Role::Promoting);
        assert!(gate.is_open());
        assert!(peer.owns_field());
        assert!(!peer.accepts_commands());
        assert_eq!(
            peer.report(),
            RoleReport {
                role: Role::Promoting,
                tick: Tick(7),
                sync: Some(StandbySync::Tracking { aligned: Tick(7) }),
            }
        );

        // The first post-promotion scan settles the role.
        peer.scan();
        assert_eq!(peer.role(), Role::Active);
        assert_eq!(peer.report().sync, None);
        assert!(peer.accepts_commands());

        // The queued transitions journal both halves of the switch.
        assert_eq!(
            peer.take_role_changes(),
            vec![
                RoleChange {
                    tick: Tick(7),
                    from: Role::Standby,
                    to: Role::Promoting,
                },
                RoleChange {
                    tick: Tick(8),
                    from: Role::Promoting,
                    to: Role::Active,
                },
            ]
        );
    }

    #[test]
    fn repeated_promotion_and_apply_on_the_owner_are_named_errors() {
        let driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let gate = WriteGate::closed(&driver);
        let mut peer = Peer::standby(executor(&gate), Some(&gate));

        let source_driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let mut source = executor(&source_driver);
        source.run(3);
        let checkpoint = source.checkpoint();
        peer.apply(&checkpoint).unwrap();

        peer.promote().unwrap();
        assert_eq!(peer.promote(), Err(SwitchError::AlreadyActive));

        // A checkpoint offered to a field-owning peer is refused.
        assert_eq!(peer.apply(&checkpoint), Err(ApplyError::OwnsField));

        peer.scan();
        assert_eq!(peer.promote(), Err(SwitchError::AlreadyActive));
        assert_eq!(peer.apply(&checkpoint), Err(ApplyError::OwnsField));
    }

    /// The revision-side executor: a writable internal `In` point is the
    /// carried operator value's landing.
    fn revision_executor<'d>(driver: &'d (dyn IoDriver + Sync)) -> Executor<'d> {
        Executor::new(
            driver,
            PointMap::new().with_writable_internal(
                PointId(10),
                Direction::In,
                ValueKind::Float,
                Value::Float(0.0),
            ),
            Vec::new(),
        )
        .unwrap()
        .with_model_fingerprint(dcs_core::ModelFingerprint::of(b"model-b"))
    }

    /// A checkpoint captured under `model-a`: internal 10 carries the
    /// old run's held operator value.
    fn foreign_checkpoint(tick: u64) -> Checkpoint {
        let source_driver = StubDriver::field(&[]);
        let mut source = Executor::new(
            &source_driver,
            PointMap::new().with_writable_internal(
                PointId(10),
                Direction::In,
                ValueKind::Float,
                Value::Float(0.0),
            ),
            Vec::new(),
        )
        .unwrap()
        .with_model_fingerprint(dcs_core::ModelFingerprint::of(b"model-a"));
        source.submit_command(Command::WriteValue {
            point: PointId(10),
            kind: ValueKind::Float,
            value: Value::Float(7.0),
        });
        source.run(tick);
        source.checkpoint()
    }

    #[test]
    fn revision_transfer_reinitializes_instead_of_converging() {
        let driver = StubDriver::field(&[]);
        let gate = WriteGate::closed(&driver);
        let mut peer = Peer::standby(revision_executor(&gate), Some(&gate)).with_revision();

        // An unarmed standby degrades on the foreign fingerprint — the
        // gate is opt-in, not weakened.
        let checkpoint = foreign_checkpoint(5);
        let mut plain = Peer::standby(executor(&gate), Some(&gate));
        assert!(matches!(
            plain.transfer(&checkpoint),
            Err(ApplyError::Restore(
                RestoreError::FingerprintMismatch { .. }
            ))
        ));
        assert!(matches!(plain.sync_state(), StandbySync::Degraded { .. }));

        // The armed peer crosses the boundary: the operator value
        // carried, the component set reinitialized, and the report rides
        // the named sync state.
        let transfer = peer.transfer(&checkpoint).unwrap();
        let report = match &transfer {
            Transfer::Reinitialized(report) => (**report).clone(),
            Transfer::Applied => panic!("a foreign fingerprint reinitializes"),
        };
        assert_eq!(report.resumed_at, Tick(5));
        assert_eq!(
            report.carried,
            vec![dcs_core::CarriedPoint {
                point: PointId(10),
                value: Value::Float(7.0),
            }]
        );
        assert_eq!(
            peer.sync_state(),
            &StandbySync::Reinitialized {
                report: Box::new(report.clone())
            }
        );
        assert_eq!(peer.take_reinitializations(), vec![report]);

        // Refresh pulls keep the crossing current without re-journaling
        // the transition.
        let _ = peer.transfer(&foreign_checkpoint(6)).unwrap();
        assert!(matches!(
            peer.sync_state(),
            StandbySync::Reinitialized { .. }
        ));
        assert!(peer.take_reinitializations().is_empty());
    }

    #[test]
    fn reinitialized_peer_promotes_at_the_boundary() {
        let driver = StubDriver::field(&[]);
        let gate = WriteGate::closed(&driver);
        let mut peer = Peer::standby(revision_executor(&gate), Some(&gate)).with_revision();
        peer.transfer(&foreign_checkpoint(5)).unwrap();

        peer.promote().unwrap();
        assert_eq!(peer.role(), Role::Promoting);
        assert!(gate.is_open());
        peer.scan();
        assert_eq!(peer.role(), Role::Active);
        // The carried operator value survived the switch.
        assert_eq!(
            peer.snapshot()
                .points
                .iter()
                .find(|telemetry| telemetry.point == PointId(10))
                .and_then(|telemetry| telemetry.sample)
                .map(|sample| sample.value),
            Some(Value::Float(7.0))
        );
    }

    #[test]
    fn broken_carryover_fails_before_promotion() {
        let driver = StubDriver::field(&[]);
        let gate = WriteGate::closed(&driver);
        let mut peer = Peer::standby(revision_executor(&gate), Some(&gate)).with_revision();

        // A force on a point the revision does not serve breaks the
        // all-or-nothing force rule.
        let mut checkpoint = foreign_checkpoint(5);
        checkpoint.forces.insert(PointId(9), Value::Float(1.0));
        assert_eq!(
            peer.transfer(&checkpoint),
            Err(ApplyError::Carryover(CarryoverError::ForceNotServed {
                point: PointId(9)
            }))
        );
        assert!(matches!(peer.sync_state(), StandbySync::Degraded { .. }));

        // The refused crossing leaves the peer unpromotable — the named
        // sync state is the refusal's evidence.
        assert!(matches!(
            peer.promote(),
            Err(SwitchError::NotConverged {
                sync: StandbySync::Degraded { .. }
            })
        ));
        assert!(!gate.is_open());

        // A clean later pull still crosses: the failure is per-transfer,
        // not a latch.
        assert!(peer.transfer(&foreign_checkpoint(6)).is_ok());
        assert!(matches!(
            peer.sync_state(),
            StandbySync::Reinitialized { .. }
        ));
    }

    #[test]
    fn demotion_requiesces_the_old_active() {
        let point = PointId(1);
        let driver = StubDriver::new(point, Value::Float(0.0));
        let gate = WriteGate::closed(&driver);
        let released = AtomicBool::new(false);
        let mut peer = Peer::active(executor(&gate), Some(&gate)).with_field_release(|| {
            released.store(true, Ordering::Relaxed);
        });
        peer.activate().unwrap();
        peer.scan();
        assert!(gate.is_open());

        peer.demote().unwrap();
        assert_eq!(peer.role(), Role::Demoting);
        assert!(!gate.is_open());
        assert!(!peer.owns_field());
        // The release ran with the gate's close — the demoted
        // attachment's recorded claim cannot ride a later re-attach back
        // onto the field its new owner claimed.
        assert!(released.load(Ordering::Relaxed));

        // The gate is already closed: writes stop before the settle.
        gate.write(point, Value::Float(9.0)).unwrap();
        assert_eq!(driver.value(point), Value::Float(0.0));

        peer.scan();
        assert_eq!(peer.role(), Role::Standby);
        assert_eq!(peer.sync_state(), &StandbySync::Unsynchronized);

        // Demoting a non-owner is a named error.
        assert_eq!(peer.demote(), Err(SwitchError::NotActive));
    }

    /// A launched active takes the same field-ownership claim a
    /// promotion does — at startup, before the gate lifts — so the
    /// shared field is fenced for this owner from the first scan.
    #[test]
    fn the_launched_active_claims_the_field_then_lifts_the_gate() {
        let driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let gate = WriteGate::closed(&driver);
        let claimed = AtomicBool::new(false);
        let mut peer = Peer::active(executor(&gate), Some(&gate)).with_field_claim(|| {
            claimed.store(true, Ordering::Relaxed);
            Ok(())
        });

        // The gate waits on the claim: construction alone opens nothing.
        assert!(!claimed.load(Ordering::Relaxed));
        assert!(!gate.is_open());

        peer.activate().unwrap();
        assert!(claimed.load(Ordering::Relaxed));
        assert!(gate.is_open());
    }

    /// A startup claim the field refuses fails the activation named —
    /// `FieldClaimFailed` — with the gate still closed: a launched
    /// active that cannot take the single-writer arbitration does not
    /// run unfenced.
    #[test]
    fn a_refused_startup_claim_keeps_the_gate_closed() {
        let driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let gate = WriteGate::closed(&driver);
        let mut peer = Peer::active(executor(&gate), Some(&gate))
            .with_field_claim(|| Err("claim refused".to_string()));

        assert_eq!(
            peer.activate(),
            Err(SwitchError::FieldClaimFailed {
                detail: "claim refused".to_string()
            })
        );
        assert!(!gate.is_open());
        // Activation is a launched-active transition only.
        let mut standby = Peer::standby(executor(&gate), None);
        assert_eq!(standby.activate(), Err(SwitchError::NotActive));
    }

    /// A write the shared field fenced — its answer to a preempted
    /// claim — demotes the owning peer in place rather than failing its
    /// scan: the gate re-closes, the reported role walks `demoting` to
    /// `standby`, and one `FencingLoss` queues for the journal.
    struct FencingDriver<'d> {
        inner: &'d (dyn IoDriver + Sync),
        armed: AtomicBool,
    }

    impl IoDriver for FencingDriver<'_> {
        fn read(&self, point: PointId) -> Result<Sample, IoError> {
            self.inner.read(point)
        }

        fn write(&self, point: PointId, value: Value) -> Result<(), IoError> {
            if self.armed.load(Ordering::Relaxed) {
                return Err(IoError::Fenced(point));
            }
            self.inner.write(point, value)
        }
    }

    #[test]
    fn a_preempted_claim_demotes_the_owner_and_journals_one_loss_per_ownership() {
        let field = StubDriver::field(&[(INPUT, Value::Float(1.0)), (OUTPUT, Value::Float(0.0))]);
        let fenced = FencingDriver {
            inner: &field,
            armed: AtomicBool::new(false),
        };
        let gate = WriteGate::closed(&fenced);
        let mut peer = Peer::active(
            Executor::new(&gate, loop_map(), vec![Box::new(PassThrough)]).unwrap(),
            Some(&gate),
        );
        peer.activate().unwrap();
        peer.scan();
        assert_eq!(field.value(OUTPUT), Value::Float(1.0));
        assert!(peer.take_fencing_losses().is_empty());

        // Another attachment took the claim: the next write is fenced.
        // The scan completes degraded — the boundary counted the fenced
        // fault into `io_health` — and the peer adopts the demote
        // path's survivable state on the spot: gate re-closed, role
        // reporting `demoting`, the claim loss queued once.
        fenced.armed.store(true, Ordering::Relaxed);
        assert_eq!(peer.scan(), Tick(2));
        assert_eq!(
            peer.snapshot().io_health.last_error,
            Some(IoFault {
                tick: Tick(2),
                point: OUTPUT,
                direction: Direction::Out,
                error: IoError::Fenced(OUTPUT),
            })
        );
        assert_eq!(
            peer.take_fencing_losses(),
            vec![FencingLoss {
                tick: Tick(2),
                point: OUTPUT
            }]
        );
        assert_eq!(peer.role(), Role::Demoting);
        assert_eq!(peer.sync_state(), &StandbySync::Unsynchronized);
        assert!(!gate.is_open());
        assert!(!peer.owns_field());
        assert_eq!(
            peer.take_role_changes(),
            vec![RoleChange {
                tick: Tick(2),
                from: Role::Active,
                to: Role::Demoting
            }]
        );

        // The next scan's write is quiesced at the closed gate — it
        // never reaches the field — and the completed scan settles the
        // demotion. The loss stays one event per held claim: the
        // quiesced scans queue nothing further.
        assert_eq!(peer.scan(), Tick(3));
        assert_eq!(peer.role(), Role::Standby);
        assert_eq!(
            field.value(OUTPUT),
            Value::Float(1.0),
            "a demoted peer's writes must not reach the field"
        );
        assert!(peer.take_fencing_losses().is_empty());
        assert_eq!(
            peer.take_role_changes(),
            vec![RoleChange {
                tick: Tick(3),
                from: Role::Demoting,
                to: Role::Standby
            }]
        );

        // The loss reports once per held claim: re-converged and
        // re-promoted, a second preemption queues a second loss and
        // walks the same demotion again.
        let checkpoint = peer.checkpoint();
        peer.apply(&checkpoint).unwrap();
        peer.promote().unwrap();
        assert!(gate.is_open());
        assert_eq!(peer.scan(), Tick(4));
        assert_eq!(
            peer.take_fencing_losses(),
            vec![FencingLoss {
                tick: Tick(4),
                point: OUTPUT
            }]
        );
        assert_eq!(peer.scan(), Tick(5));
        assert_eq!(peer.role(), Role::Standby);
        assert_eq!(
            peer.take_role_changes(),
            vec![
                RoleChange {
                    tick: Tick(3),
                    from: Role::Standby,
                    to: Role::Promoting
                },
                RoleChange {
                    tick: Tick(4),
                    from: Role::Promoting,
                    to: Role::Demoting
                },
                RoleChange {
                    tick: Tick(5),
                    from: Role::Demoting,
                    to: Role::Standby
                },
            ]
        );
    }

    /// A peer whose claim was preempted between scans still reports
    /// `active` and accepts commands until the detection scan — the
    /// fencing demotion must reconcile what that boundary settled onto
    /// the abandoned image: `Rejected`/`Superseded`, never `Applied`
    /// for an effect the field never saw.
    #[test]
    fn a_fenced_owner_supersedes_the_commands_its_detection_scan_applied() {
        const HELD: PointId = PointId(30);
        let field = StubDriver::field(&[(INPUT, Value::Float(1.0)), (OUTPUT, Value::Float(0.0))]);
        let fenced = FencingDriver {
            inner: &field,
            armed: AtomicBool::new(false),
        };
        let gate = WriteGate::closed(&fenced);
        let map = loop_map()
            .with_writable_point(INPUT, Direction::In, ValueKind::Float)
            .with_writable_internal(HELD, Direction::In, ValueKind::Float, Value::Float(0.0));
        let mut peer = Peer::active(
            Executor::new(&gate, map, vec![Box::new(PassThrough)]).unwrap(),
            Some(&gate),
        );
        peer.activate().unwrap();

        // A command applied while the peer still owned the field keeps
        // its applied settlement — only the superseded boundary
        // reconciles.
        peer.submit_command(Command::WriteValue {
            point: HELD,
            kind: ValueKind::Float,
            value: Value::Float(2.0),
        });
        assert_eq!(peer.scan(), Tick(1));
        assert_eq!(
            peer.receipts()[0].outcome,
            CommandOutcome::Applied { tick: Tick(1) }
        );

        // The claim is preempted between scans: the still-`active` peer
        // accepts the window's commands — they queue for its next
        // boundary.
        fenced.armed.store(true, Ordering::Relaxed);
        assert!(peer.accepts_commands());
        let internal = peer.submit_command(Command::WriteValue {
            point: HELD,
            kind: ValueKind::Float,
            value: Value::Float(9.9),
        });
        assert_eq!(
            internal.outcome,
            CommandOutcome::Accepted {
                apply_tick: Tick(2)
            }
        );
        let field_write = peer.submit_command(Command::WriteValue {
            point: INPUT,
            kind: ValueKind::Float,
            value: Value::Float(5.0),
        });
        assert!(matches!(
            field_write.outcome,
            CommandOutcome::Accepted { .. }
        ));

        // The detection scan: the boundary applies the queued commands
        // onto the image, then the field write meets the fence and the
        // demotion runs. The reconciled settlements name the
        // supersession; the field write's own fenced refusal stands as
        // the named driver rejection; nothing reached the field.
        assert_eq!(peer.scan(), Tick(2));
        assert_eq!(peer.role(), Role::Demoting);
        assert!(!gate.is_open());
        assert_eq!(field.value(INPUT), Value::Float(1.0));
        assert_eq!(
            peer.receipts()[1].outcome,
            CommandOutcome::Rejected {
                reason: CommandError::Superseded { point: Some(HELD) }
            }
        );
        assert_eq!(
            peer.receipts()[2].outcome,
            CommandOutcome::Rejected {
                reason: CommandError::DriverRejected {
                    point: INPUT,
                    error: IoError::Fenced(INPUT),
                }
            }
        );
        // The earlier boundary's settlement stands: it applied while
        // the peer owned the field.
        assert_eq!(
            peer.receipts()[0].outcome,
            CommandOutcome::Applied { tick: Tick(1) }
        );

        // The demoted peer's next quiesced boundary settles no strays —
        // the pending queue was drained by the reconciliation.
        assert_eq!(peer.scan(), Tick(3));
        assert_eq!(peer.role(), Role::Standby);
        assert_eq!(peer.receipts().len(), 3);
    }

    /// A read-biasing driver wrapper: adds `offset` to `Float` reads of
    /// `point` while `armed` — the skewed view of the field a diverging
    /// standby computes from.
    struct BiasedDriver<'d> {
        inner: &'d (dyn IoDriver + Sync),
        point: PointId,
        offset: f64,
        armed: AtomicBool,
    }

    impl IoDriver for BiasedDriver<'_> {
        fn read(&self, point: PointId) -> Result<Sample, IoError> {
            let mut sample = self.inner.read(point)?;
            if point == self.point
                && self.armed.load(Ordering::Relaxed)
                && let Value::Float(value) = sample.value
            {
                sample.value = Value::Float(value + self.offset);
            }
            Ok(sample)
        }

        fn write(&self, point: PointId, value: Value) -> Result<(), IoError> {
            self.inner.write(point, value)
        }
    }

    /// A read-faulting driver wrapper: reads of `point` fail
    /// `Disconnected` while `armed` — the transient channel fault the
    /// QA finding injects on the diverging point.
    struct FailingDriver<'d> {
        inner: &'d (dyn IoDriver + Sync),
        point: PointId,
        armed: AtomicBool,
    }

    impl IoDriver for FailingDriver<'_> {
        fn read(&self, point: PointId) -> Result<Sample, IoError> {
            if point == self.point && self.armed.load(Ordering::Relaxed) {
                return Err(IoError::Disconnected(point));
            }
            self.inner.read(point)
        }

        fn write(&self, point: PointId, value: Value) -> Result<(), IoError> {
            self.inner.write(point, value)
        }
    }

    /// Copies its input to its output each scan — the minimal staged
    /// field output a skewed input read can bend.
    struct PassThrough;

    impl Component for PassThrough {
        fn name(&self) -> &str {
            "pass"
        }

        fn io_requirements(&self) -> Vec<IoRequirement> {
            vec![
                IoRequirement::input::<f64>("in", INPUT),
                IoRequirement::output::<f64>("out", OUTPUT),
            ]
        }

        fn step(&mut self, io: &dyn ComponentIo, _tick: Tick) -> Result<(), StepError> {
            let input = io.read_typed::<f64>(INPUT)?;
            io.write_typed(OUTPUT, input.value)?;
            Ok(())
        }
    }

    const INPUT: PointId = PointId(10);
    const OUTPUT: PointId = PointId(20);

    /// The point map both peers' executors share.
    fn loop_map() -> PointMap {
        PointMap::new()
            .with_point(INPUT, Direction::In, ValueKind::Float)
            .with_point(OUTPUT, Direction::Out, ValueKind::Float)
    }

    /// One transfer-and-scan cycle of the shared-field pair: the active
    /// scans — the field then carries its `Out` write for that tick —
    /// the standby applies the checkpoint, which runs the divergence
    /// check on the staged image of the matching tick, then scans.
    fn cycle(active: &mut Peer<'_>, standby: &mut Peer<'_>) {
        active.scan();
        standby.apply(&active.checkpoint()).unwrap();
        standby.scan();
    }

    #[test]
    fn diverged_standby_is_refused_promotion_until_resync() {
        // The shared field: the active's writes land; the quiesced
        // standby's do not.
        let field = StubDriver::field(&[(INPUT, Value::Float(2.0)), (OUTPUT, Value::Float(0.0))]);
        let biased = BiasedDriver {
            inner: &field,
            point: INPUT,
            offset: 5.0,
            armed: AtomicBool::new(false),
        };
        let gate = WriteGate::closed(&biased);
        let mut standby = Peer::standby(
            Executor::new(&gate, loop_map(), vec![Box::new(PassThrough)]).unwrap(),
            Some(&gate),
        );
        let mut active = Peer::active(
            Executor::new(&field, loop_map(), vec![Box::new(PassThrough)]).unwrap(),
            None,
        );

        // Converge: the first transfer aligns ticks; tracking cycles
        // keep the staged image and the field's values equal.
        active.scan();
        standby.apply(&active.checkpoint()).unwrap();
        standby.scan();
        for _ in 0..3 {
            cycle(&mut active, &mut standby);
        }
        assert_eq!(
            standby.sync_state(),
            &StandbySync::Tracking { aligned: Tick(4) }
        );
        assert_eq!(standby.take_divergences(), vec![]);
        assert!(matches!(
            standby.report().sync,
            Some(StandbySync::Tracking { .. })
        ));

        // Skew the standby's view of the field: its staged output bends
        // away from the field's. The check pairs the skewed staged image
        // with the first checkpoint of its tick — the detection bound of
        // one transfer after the staged scan.
        biased.armed.store(true, Ordering::Relaxed);
        cycle(&mut active, &mut standby);
        assert_eq!(
            standby.sync_state(),
            &StandbySync::Tracking { aligned: Tick(5) }
        );
        cycle(&mut active, &mut standby);

        let diverged = StandbySync::Diverged {
            mismatches: vec![Divergence {
                point: OUTPUT,
                staged: Value::Float(7.0),
                field: Value::Float(2.0),
            }],
        };
        assert_eq!(standby.report().sync, Some(diverged.clone()));
        assert_eq!(
            standby.take_divergences(),
            vec![DivergenceReport {
                tick: Tick(6),
                mismatches: vec![Divergence {
                    point: OUTPUT,
                    staged: Value::Float(7.0),
                    field: Value::Float(2.0),
                }],
            }]
        );

        // The diverged standby cannot promote: the named refusal carries
        // the named state, the gate never lifted, and its writes stayed
        // quiesced throughout.
        assert_eq!(
            standby.promote(),
            Err(SwitchError::NotConverged { sync: diverged })
        );
        assert!(!gate.is_open());
        assert_eq!(field.value(OUTPUT), Value::Float(2.0));

        // Relieved of the skew, a same-tick comparison that matches
        // resynchronizes the peer — promotion is accepted again. The
        // staged image of the still-skewed scan keeps it diverged one
        // more cycle.
        biased.armed.store(false, Ordering::Relaxed);
        cycle(&mut active, &mut standby);
        assert!(matches!(standby.sync_state(), StandbySync::Diverged { .. }));
        assert_eq!(
            standby.take_resolutions(),
            vec![],
            "still diverged — nothing resolved yet"
        );
        cycle(&mut active, &mut standby);
        assert_eq!(
            standby.sync_state(),
            &StandbySync::Tracking { aligned: Tick(8) }
        );
        // The diverged → tracking transition queues once, attributed
        // to the compared tick and carrying the compared-point evidence
        // — both sides' values now agreeing.
        assert_eq!(
            standby.take_resolutions(),
            vec![ResolutionReport {
                tick: Tick(8),
                compared: vec![Divergence {
                    point: OUTPUT,
                    staged: Value::Float(2.0),
                    field: Value::Float(2.0),
                }],
            }]
        );
        standby.promote().unwrap();
        assert!(gate.is_open());
    }

    /// The QA finding `diverged-clears-without-valid-field-comparison`:
    /// `Diverged` is the promotion-blocking "staged outputs differ from
    /// the field" verdict and clears only on a same-tick comparison
    /// that actually read the field and matched. A stale checkpoint
    /// apply runs zero reads, and a same-tick compare whose field reads
    /// fail observes nothing — neither reopens the promote gate while
    /// the divergence evidence stands unresolved.
    #[test]
    fn diverged_standby_clears_only_on_a_fully_read_same_tick_compare() {
        // The shared field; the standby observes it through a
        // fault-injecting wrapper on the point under test.
        let field = StubDriver::field(&[(INPUT, Value::Float(2.0)), (OUTPUT, Value::Float(0.0))]);
        let failing = FailingDriver {
            inner: &field,
            point: OUTPUT,
            armed: AtomicBool::new(false),
        };
        let gate = WriteGate::closed(&failing);
        let mut standby = Peer::standby(
            Executor::new(&gate, loop_map(), vec![Box::new(PassThrough)]).unwrap(),
            Some(&gate),
        );
        let mut active = Peer::active(
            Executor::new(&field, loop_map(), vec![Box::new(PassThrough)]).unwrap(),
            None,
        );

        // Converge and track clean: each same-tick comparison matches.
        active.scan();
        standby.apply(&active.checkpoint()).unwrap();
        standby.scan();
        for _ in 0..3 {
            cycle(&mut active, &mut standby);
        }
        assert_eq!(
            standby.sync_state(),
            &StandbySync::Tracking { aligned: Tick(4) }
        );

        // Diverge: the staged image describes the tick-5 output; the
        // active's write lands, then a rogue writer moves the same
        // field point before the same-tick compare reads it.
        active.scan();
        field.write(OUTPUT, Value::Float(9.9)).unwrap();
        standby.apply(&active.checkpoint()).unwrap();
        standby.scan();
        let diverged = StandbySync::Diverged {
            mismatches: vec![Divergence {
                point: OUTPUT,
                staged: Value::Float(2.0),
                field: Value::Float(9.9),
            }],
        };
        assert_eq!(standby.sync_state(), &diverged);
        assert_eq!(
            standby.take_divergences(),
            vec![DivergenceReport {
                tick: Tick(5),
                mismatches: vec![Divergence {
                    point: OUTPUT,
                    staged: Value::Float(2.0),
                    field: Value::Float(9.9),
                }],
            }]
        );
        assert!(standby.take_resolutions().is_empty());
        assert_eq!(
            standby.promote(),
            Err(SwitchError::NotConverged {
                sync: diverged.clone()
            })
        );
        assert!(!gate.is_open());

        // (1) The stalled-source repro: the active stopped advancing,
        // so the pulled checkpoint repeats the last alignment and its
        // tick matches no staged image — the apply performs zero field
        // reads. The verdict stands and the gate stays shut.
        standby.apply(&active.checkpoint()).unwrap();
        assert_eq!(standby.sync_state(), &diverged);
        assert_eq!(
            standby.promote(),
            Err(SwitchError::NotConverged {
                sync: diverged.clone()
            })
        );
        assert!(!gate.is_open());
        // A produced-nothing pull carries no field evidence either —
        // the miss counts toward failover, the verdict still stands.
        standby.note_transfer_failed("fetch from active: refused");
        assert_eq!(standby.sync_state(), &diverged);
        assert!(standby.take_resolutions().is_empty());

        // (2) The faulted-reads repro: a fresh same-tick checkpoint
        // lands, so the comparison runs — but the field read of the
        // diverging point fails. A comparison that read nothing observed
        // nothing; the verdict stands.
        failing.armed.store(true, Ordering::Relaxed);
        standby.scan();
        active.scan();
        standby.apply(&active.checkpoint()).unwrap();
        assert_eq!(standby.sync_state(), &diverged);
        assert_eq!(
            standby.promote(),
            Err(SwitchError::NotConverged {
                sync: diverged.clone()
            })
        );
        assert!(!gate.is_open());
        assert!(standby.take_resolutions().is_empty());

        // (3) Only a fresh same-tick comparison whose field reads all
        // succeeded and matched clears the verdict — (4) journaled as
        // the named resolution carrying every compared point's
        // evidence — and the promote gate reopens.
        failing.armed.store(false, Ordering::Relaxed);
        standby.scan();
        active.scan();
        standby.apply(&active.checkpoint()).unwrap();
        assert_eq!(
            standby.sync_state(),
            &StandbySync::Tracking { aligned: Tick(7) }
        );
        assert_eq!(
            standby.take_resolutions(),
            vec![ResolutionReport {
                tick: Tick(7),
                compared: vec![Divergence {
                    point: OUTPUT,
                    staged: Value::Float(2.0),
                    field: Value::Float(2.0),
                }],
            }]
        );
        standby.promote().unwrap();
        assert!(gate.is_open());
    }

    /// The promotion boundary's `final_sync` sets the staged image
    /// aside for its own transfer — a same-tick comparison there would
    /// flag the one-tick command lag spuriously — but a promote the
    /// standing verdict refuses must hand it back: on a diverged peer
    /// it is the only evidence the next same-tick apply can clear the
    /// verdict on.
    #[test]
    fn a_refused_promotions_boundary_sync_keeps_the_divergence_evidence() {
        let field = StubDriver::field(&[(INPUT, Value::Float(2.0)), (OUTPUT, Value::Float(0.0))]);
        let gate = WriteGate::closed(&field);
        let mut standby = Peer::standby(
            Executor::new(&gate, loop_map(), vec![Box::new(PassThrough)]).unwrap(),
            Some(&gate),
        );
        let mut active = Peer::active(
            Executor::new(&field, loop_map(), vec![Box::new(PassThrough)]).unwrap(),
            None,
        );

        // Converge and track clean.
        active.scan();
        standby.apply(&active.checkpoint()).unwrap();
        standby.scan();
        for _ in 0..3 {
            cycle(&mut active, &mut standby);
        }
        assert_eq!(
            standby.sync_state(),
            &StandbySync::Tracking { aligned: Tick(4) }
        );

        // Diverge: the staged image describes the tick-5 output; a rogue
        // writer moves the field point before the same-tick compare
        // reads it.
        active.scan();
        field.write(OUTPUT, Value::Float(9.9)).unwrap();
        standby.apply(&active.checkpoint()).unwrap();
        standby.scan();
        let diverged = StandbySync::Diverged {
            mismatches: vec![Divergence {
                point: OUTPUT,
                staged: Value::Float(2.0),
                field: Value::Float(9.9),
            }],
        };
        assert_eq!(standby.sync_state(), &diverged);
        standby.take_divergences();

        // The active's next scan overwrote the skew — the field carries
        // its write again — and the diverged peer's promote runs its
        // boundary pull on the fresh tick-6 checkpoint: refused by the
        // standing verdict, which the boundary neither manufactures nor
        // revokes.
        active.scan();
        standby.final_sync(|| Ok(active.checkpoint()));
        assert_eq!(standby.sync_state(), &diverged);
        assert_eq!(
            standby.promote(),
            Err(SwitchError::NotConverged {
                sync: diverged.clone()
            })
        );

        // The cadence's next pull lands the same checkpoint again —
        // paired this time with the handed-back staged image: the
        // fully-read same-tick match clears the verdict and journals
        // the resolution.
        standby.apply(&active.checkpoint()).unwrap();
        assert_eq!(
            standby.sync_state(),
            &StandbySync::Tracking { aligned: Tick(6) }
        );
        assert_eq!(
            standby.take_resolutions(),
            vec![ResolutionReport {
                tick: Tick(6),
                compared: vec![Divergence {
                    point: OUTPUT,
                    staged: Value::Float(2.0),
                    field: Value::Float(2.0),
                }],
            }]
        );
        standby.promote().unwrap();
        assert!(gate.is_open());
    }

    /// A diverged peer whose next apply runs no same-tick field
    /// comparison — the staged image's tick never matched the applied
    /// checkpoint's — stays diverged: the apply carried no
    /// field-matching evidence, so the promotion-blocking verdict
    /// stands and no resolution queues.
    #[test]
    fn diverged_verdict_stands_on_an_apply_that_ran_no_compare() {
        let field = StubDriver::field(&[(INPUT, Value::Float(2.0)), (OUTPUT, Value::Float(0.0))]);
        let biased = BiasedDriver {
            inner: &field,
            point: INPUT,
            offset: 5.0,
            armed: AtomicBool::new(true),
        };
        let gate = WriteGate::closed(&biased);
        let mut standby = Peer::standby(
            Executor::new(&gate, loop_map(), vec![Box::new(PassThrough)]).unwrap(),
            Some(&gate),
        );
        let mut active = Peer::active(
            Executor::new(&field, loop_map(), vec![Box::new(PassThrough)]).unwrap(),
            None,
        );

        // Converge, then diverge on the first same-tick compare — the
        // skew is armed from the start, so the tick-2 staged image
        // mismatches the field the active wrote.
        active.scan();
        standby.apply(&active.checkpoint()).unwrap();
        standby.scan();
        cycle(&mut active, &mut standby);
        assert!(matches!(standby.sync_state(), StandbySync::Diverged { .. }));
        assert_eq!(standby.take_divergences().len(), 1);

        // The detecting apply consumed the staged image; two active
        // scans before the next apply leave no staged image whose tick
        // the checkpoint@4 matches — the apply runs no field
        // comparison, so the verdict stands and the gate stays shut.
        active.scan();
        active.scan();
        standby.apply(&active.checkpoint()).unwrap();
        assert!(matches!(standby.sync_state(), StandbySync::Diverged { .. }));
        assert!(standby.take_resolutions().is_empty());
        assert!(matches!(
            standby.promote(),
            Err(SwitchError::NotConverged {
                sync: StandbySync::Diverged { .. }
            })
        ));
    }

    /// A `final_sync` pull on a diverged peer runs the transfer's apply
    /// but restores the reported verdict — the peer never observably
    /// leaves `diverged`, so the resolution the apply queued must not
    /// reach the journal.
    #[test]
    fn final_sync_on_a_diverged_peer_queues_no_phantom_resolution() {
        let field = StubDriver::field(&[(INPUT, Value::Float(2.0)), (OUTPUT, Value::Float(0.0))]);
        let biased = BiasedDriver {
            inner: &field,
            point: INPUT,
            offset: 5.0,
            armed: AtomicBool::new(true),
        };
        let gate = WriteGate::closed(&biased);
        let mut standby = Peer::standby(
            Executor::new(&gate, loop_map(), vec![Box::new(PassThrough)]).unwrap(),
            Some(&gate),
        );
        let mut active = Peer::active(
            Executor::new(&field, loop_map(), vec![Box::new(PassThrough)]).unwrap(),
            None,
        );

        active.scan();
        standby.apply(&active.checkpoint()).unwrap();
        standby.scan();
        cycle(&mut active, &mut standby);
        assert!(matches!(standby.sync_state(), StandbySync::Diverged { .. }));
        standby.take_divergences();

        // The promote path's boundary pull: its apply would clear the
        // diverged state evidence-free, but the restored verdict keeps
        // the report — and no resolution queues. The active scans once
        // first so its checkpoint is fresh enough to land.
        active.scan();
        standby.final_sync(|| Ok(active.checkpoint()));
        assert!(matches!(standby.sync_state(), StandbySync::Diverged { .. }));
        assert_eq!(standby.take_resolutions(), vec![]);
        assert!(matches!(
            standby.promote(),
            Err(SwitchError::NotConverged {
                sync: StandbySync::Diverged { .. }
            })
        ));
    }

    /// A converged standby with a failover budget of two: one transient
    /// miss neither reports due nor promotes; the second does, taking
    /// the claim and the gate at that scan boundary.
    #[test]
    fn failover_promotes_at_the_budget_boundary_only() {
        let driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let gate = WriteGate::closed(&driver);
        let claimed = AtomicBool::new(false);
        let mut peer = Peer::standby(executor(&gate), Some(&gate))
            .with_failover(2)
            .with_field_claim(|| {
                claimed.store(true, Ordering::Relaxed);
                Ok(())
            });

        let source_driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let mut source = executor(&source_driver);
        source.run(3);
        peer.apply(&source.checkpoint()).unwrap();
        assert_eq!(
            peer.sync_state(),
            &StandbySync::Tracking { aligned: Tick(3) }
        );

        // One transient miss: the budget is not met and self-promotion
        // is refused with the named degraded state — the peer reports
        // rather than promotes.
        peer.note_transfer_failed("connection refused");
        assert!(!peer.failover_due());
        assert_eq!(
            peer.self_promote(),
            Err(SwitchError::NotConverged {
                sync: StandbySync::Degraded {
                    detail: "connection refused".into(),
                },
            })
        );
        assert_eq!(peer.role(), Role::Standby);
        assert!(!gate.is_open());

        // A produced checkpoint resets the run — the active served.
        source.run(1);
        peer.apply(&source.checkpoint()).unwrap();
        assert_eq!(peer.missed_transfers(), 0);

        // Two consecutive misses reach the budget: the scan boundary
        // promotes, the claim runs before the gate lifts.
        peer.note_transfer_failed("a");
        peer.note_transfer_failed("b");
        assert!(peer.failover_due());
        assert_eq!(peer.missed_transfers(), 2);
        peer.self_promote().unwrap();
        assert!(claimed.load(Ordering::Relaxed));
        assert_eq!(peer.role(), Role::Promoting);
        assert!(gate.is_open());
        peer.scan();
        assert_eq!(peer.role(), Role::Active);
    }

    /// Self-promotion refuses while the convergence proof does not
    /// stand: never-converged, diverged, or past the budget — each is
    /// the named `NotConverged` carrying the reported state.
    #[test]
    fn self_promotion_requires_standing_convergence() {
        let driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let gate = WriteGate::closed(&driver);
        let mut peer = Peer::standby(executor(&gate), Some(&gate)).with_failover(2);

        // Never converged: misses or not, the named state reports — the
        // failed pulls degrade it rather than promote it.
        peer.note_transfer_failed("a");
        peer.note_transfer_failed("b");
        assert!(peer.failover_due());
        assert_eq!(
            peer.self_promote(),
            Err(SwitchError::NotConverged {
                sync: StandbySync::Degraded { detail: "b".into() }
            })
        );

        // Converge, then run past the budget: the failover window
        // closed with the proof, and the peer reports `degraded`.
        let source_driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let mut source = executor(&source_driver);
        source.run(3);
        peer.apply(&source.checkpoint()).unwrap();
        peer.note_transfer_failed("a");
        peer.note_transfer_failed("b");
        peer.note_transfer_failed("c");
        assert!(!peer.failover_due());
        assert!(matches!(
            peer.self_promote(),
            Err(SwitchError::NotConverged {
                sync: StandbySync::Degraded { .. }
            })
        ));
        assert_eq!(peer.role(), Role::Standby);
        assert!(!gate.is_open());
    }

    /// A failed field claim refuses the promotion — the gate stays
    /// closed and the reported role does not move.
    #[test]
    fn a_failed_claim_refuses_promotion() {
        let driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let gate = WriteGate::closed(&driver);
        let mut peer = Peer::standby(executor(&gate), Some(&gate))
            .with_field_claim(|| Err("plant unreachable".to_string()));

        let source_driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let mut source = executor(&source_driver);
        source.run(3);
        peer.apply(&source.checkpoint()).unwrap();

        assert_eq!(
            peer.promote(),
            Err(SwitchError::FieldClaimFailed {
                detail: "plant unreachable".into()
            })
        );
        assert_eq!(peer.role(), Role::Standby);
        assert!(!gate.is_open());
    }

    /// Without a failover budget the miss count still reports but never
    /// arms — manual promotion carries the pair alone.
    #[test]
    fn without_a_budget_failover_never_arms() {
        let driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let mut peer = Peer::standby(executor(&driver), None);
        peer.note_transfer_failed("a");
        peer.note_transfer_failed("b");
        assert_eq!(peer.missed_transfers(), 2);
        assert!(!peer.failover_due());
        assert_eq!(
            peer.self_promote(),
            Err(SwitchError::NotConverged {
                sync: StandbySync::Degraded { detail: "b".into() }
            })
        );
    }

    /// A produced checkpoint applies through `transfer` — the report
    /// carries the `Transfer` and the peer reports `tracking`.
    #[test]
    fn track_once_applies_a_produced_checkpoint() {
        let driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let gate = WriteGate::closed(&driver);
        let mut peer = Peer::standby(executor(&gate), Some(&gate));

        let source_driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let mut source = executor(&source_driver);
        source.run(4);
        let checkpoint = source.checkpoint();

        let report = peer.track_once(|| Ok(checkpoint));
        assert_eq!(report, TrackReport::Applied(Transfer::Applied));
        assert_eq!(
            peer.sync_state(),
            &StandbySync::Tracking { aligned: Tick(4) }
        );
        assert_eq!(peer.missed_transfers(), 0);
    }

    /// A produced-nothing pull is the heartbeat miss — counted toward
    /// the budget and reported degraded — while a produced-but-refused
    /// checkpoint is not a miss at all: the active served.
    #[test]
    fn track_once_counts_the_miss_and_reports_the_refusal() {
        let driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let gate = WriteGate::closed(&driver);
        let mut peer = Peer::standby(executor(&gate), Some(&gate)).with_failover(3);

        let report = peer.track_once(|| Err("fetch from active: refused".to_string()));
        assert_eq!(
            report,
            TrackReport::Missed {
                detail: "fetch from active: refused".into()
            }
        );
        assert_eq!(peer.missed_transfers(), 1);
        assert_eq!(
            peer.sync_state(),
            &StandbySync::Degraded {
                detail: "fetch from active: refused".into()
            }
        );

        // A checkpoint the apply rejects still proves the active alive:
        // the miss count resets and the refusal is the named report.
        let mut foreign = executor(&driver).checkpoint();
        foreign
            .components
            .insert("ghost".to_string(), Default::default());
        let report = peer.track_once(|| Ok(foreign));
        assert!(matches!(
            report,
            TrackReport::Refused(ApplyError::Restore(RestoreError::UnknownComponent { .. }))
        ));
        assert_eq!(peer.missed_transfers(), 0);
        assert!(matches!(peer.sync_state(), StandbySync::Degraded { .. }));
        assert!(!peer.failover_due());
    }

    /// The budget-th miss promotes a still-converged standby at that
    /// boundary — the report carries the post-change role, and the
    /// field owner the peer became tracks nothing further.
    #[test]
    fn track_once_promotes_on_the_budget_miss() {
        let driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let gate = WriteGate::closed(&driver);
        let mut peer = Peer::standby(executor(&gate), Some(&gate)).with_failover(2);

        let source_driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let mut source = executor(&source_driver);
        source.run(3);
        let checkpoint = source.checkpoint();
        assert_eq!(
            peer.track_once(|| Ok(checkpoint)),
            TrackReport::Applied(Transfer::Applied)
        );

        assert!(matches!(
            peer.track_once(|| Err("a".to_string())),
            TrackReport::Missed { .. }
        ));
        let report = peer.track_once(|| Err("b".to_string()));
        match report {
            TrackReport::Promoted { detail, report } => {
                assert_eq!(detail, "b");
                assert_eq!(report.role, Role::Promoting);
            }
            other => panic!("the budget-th miss promotes, got {other:?}"),
        }
        assert!(gate.is_open());
        assert_eq!(
            peer.take_role_changes(),
            vec![RoleChange {
                tick: Tick(3),
                from: Role::Standby,
                to: Role::Promoting,
            }]
        );

        // Now field-owning, the cycle is a no-op: no pull, no failover
        // check — a pull that would fail never runs.
        let report = peer.track_once(|| panic!("a field owner pulls nothing"));
        assert_eq!(report, TrackReport::OwnsField);
    }

    /// The budget-th miss on a peer whose convergence proof does not
    /// stand reports the named refusal instead of promoting.
    #[test]
    fn track_once_reports_the_refused_self_promotion() {
        let driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let gate = WriteGate::closed(&driver);
        let mut peer = Peer::standby(executor(&gate), Some(&gate)).with_failover(1);

        let report = peer.track_once(|| Err("active gone".to_string()));
        match report {
            TrackReport::PromotionRefused { detail, error } => {
                assert_eq!(detail, "active gone");
                assert_eq!(
                    error,
                    SwitchError::NotConverged {
                        sync: StandbySync::Degraded {
                            detail: "active gone".into()
                        }
                    }
                );
            }
            other => panic!("an unconverged standby reports, got {other:?}"),
        }
        assert_eq!(peer.role(), Role::Standby);
        assert!(!gate.is_open());
        assert!(peer.take_role_changes().is_empty());
    }

    /// A field-owning peer runs no pull and no failover check — the
    /// cycle is the reported no-op.
    #[test]
    fn track_once_is_a_no_op_for_the_field_owner() {
        let driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let gate = WriteGate::closed(&driver);
        let mut peer = Peer::active(executor(&gate), Some(&gate));
        peer.activate().unwrap();

        let report = peer.track_once(|| panic!("a field owner pulls nothing"));
        assert_eq!(report, TrackReport::OwnsField);
        assert_eq!(peer.missed_transfers(), 0);
        assert_eq!(peer.sync_state(), &StandbySync::Unsynchronized);
    }

    /// The declared-command emitter the command/event redundancy tests
    /// prove on: `bump {by}` adds to the checkpointed `count` — a
    /// repeated application would show in the total — and each scan
    /// emits `beat` carrying the checkpointed `scans` counter, the
    /// emitted sequence the continuity assertions read.
    struct Clocked {
        name: &'static str,
        scans: i64,
        count: i64,
    }

    impl Clocked {
        /// The rig's executor — no I/O surface, just the declared
        /// command and the per-scan event.
        fn executor<'d>(driver: &'d (dyn IoDriver + Sync)) -> Executor<'d> {
            Executor::new(
                driver,
                PointMap::new(),
                vec![Box::new(Self {
                    name: "clk",
                    scans: 0,
                    count: 0,
                })],
            )
            .unwrap()
        }

        /// The `bump` invocation `by` steps.
        fn bump(by: i64) -> Command {
            Command::Invoke {
                component: "clk".to_string(),
                command: "bump".to_string(),
                arguments: [("by".to_string(), Value::Int(by))].into_iter().collect(),
            }
        }

        /// `count` as the checkpoint reports it — the application's
        /// once-witness.
        fn count(checkpoint: &Checkpoint) -> Value {
            checkpoint.components["clk"].get("count").unwrap()
        }
    }

    impl Component for Clocked {
        fn name(&self) -> &str {
            self.name
        }

        fn io_requirements(&self) -> Vec<IoRequirement> {
            Vec::new()
        }

        fn step(&mut self, _io: &dyn ComponentIo, _tick: Tick) -> Result<(), StepError> {
            self.scans += 1;
            Ok(())
        }

        fn describe(&self) -> ComponentDescriptor {
            ComponentDescriptor {
                name: self.name.to_string(),
                kind: "clocked".to_string(),
                label: self.name.to_string(),
                ports: Vec::new(),
                parameters: Vec::new(),
                commands: vec![CommandDecl {
                    name: "bump".to_string(),
                    request: vec![CommandArgument {
                        name: "by".to_string(),
                        kind: ValueKind::Int,
                    }],
                    availability: CommandAvailability::Always,
                }],
                events: vec![EventDecl {
                    name: "beat".to_string(),
                    payload: vec![EventField {
                        name: "n".to_string(),
                        kind: EventFieldKind::Value(ValueKind::Int),
                        optional: false,
                    }],
                    retention: EventRetention::Journal,
                }],
            }
        }

        fn invoke_command(
            &mut self,
            command: &str,
            arguments: &BTreeMap<String, Value>,
        ) -> Result<(), String> {
            match (command, arguments.get("by")) {
                ("bump", Some(Value::Int(by))) => {
                    self.count += by;
                    Ok(())
                }
                _ => unreachable!("submission validates the declared surface"),
            }
        }

        fn drain_events(&mut self) -> Vec<EmittedEvent> {
            vec![EmittedEvent {
                event: "beat".to_string(),
                component: String::new(),
                fields: [("n".to_string(), EventValue::Value(Value::Int(self.scans)))]
                    .into_iter()
                    .collect(),
            }]
        }

        fn capture_state(&self) -> StateMap {
            let mut state = StateMap::new();
            state.insert("scans", Value::Int(self.scans));
            state.insert("count", Value::Int(self.count));
            state
        }

        fn restore_state(&mut self, state: &StateMap) -> Result<(), dcs_core::StateError> {
            state.ensure_known_fields(self.name, &["scans", "count"])?;
            self.scans = state.require_i64(self.name, "scans")?;
            self.count = state.require_i64(self.name, "count")?;
            Ok(())
        }
    }

    /// The emitted `beat`'s payload counter — the sequence the
    /// continuity assertions compare.
    fn beat_n(executor: &Executor<'_>) -> i64 {
        match executor.emitted_events() {
            [event] => match event.fields["n"] {
                EventValue::Value(Value::Int(n)) => n,
                ref other => panic!("the beat's n is an Int, got {other:?}"),
            },
            other => panic!("one beat per scan, got {other:?}"),
        }
    }

    #[test]
    fn a_pending_invoke_carried_at_promotion_settles_exactly_once() {
        // The strict takeover case: the invoke is admitted on the
        // active and still `Accepted` in the receipt log the promotion
        // boundary's final pull carries — unsettled when the peer
        // promotes, settling once at the new active's first scan, never
        // lost, never applied again. Its `bump` lands 7 exactly once.
        let field = StubDriver::field(&[]);
        let gate = WriteGate::closed(&field);
        let mut standby = Peer::standby(Clocked::executor(&gate), Some(&gate));

        let source_driver = StubDriver::field(&[]);
        let mut source = Clocked::executor(&source_driver);
        source.run(2);
        standby.apply(&source.checkpoint()).unwrap();

        // The admission lands after the last tracking pull — the
        // promotion boundary's `final_sync` is its carrier.
        source.submit_command(Clocked::bump(7));
        standby.final_sync(|| Ok(source.checkpoint()));
        assert_eq!(standby.receipts().len(), 1);
        assert!(matches!(
            standby.receipts()[0].outcome,
            CommandOutcome::Accepted { .. }
        ));

        standby.promote().unwrap();
        assert_eq!(standby.role(), Role::Promoting);
        standby.scan();
        assert_eq!(standby.role(), Role::Active);
        assert_eq!(
            standby.receipts()[0].outcome,
            CommandOutcome::Applied { tick: Tick(3) }
        );
        assert_eq!(Clocked::count(&standby.checkpoint()), Value::Int(7));

        // Never again: the settled outcome stays the log's one entry.
        standby.scan();
        assert_eq!(standby.receipts().len(), 1);
        assert_eq!(Clocked::count(&standby.checkpoint()), Value::Int(7));
    }

    #[test]
    fn a_promoted_peers_emitted_events_continue_the_sequence() {
        // Identical per-peer streams, then the promoted run's own
        // continuation: the emitted `beat` count matches an
        // uninterrupted reference scan for scan across the promotion —
        // no reset, no replay.
        let field = StubDriver::field(&[]);
        let gate = WriteGate::closed(&field);
        let mut standby = Peer::standby(Clocked::executor(&gate), Some(&gate));
        let reference_driver = StubDriver::field(&[]);
        let mut reference = Clocked::executor(&reference_driver);

        for _ in 0..3 {
            standby.apply(&reference.checkpoint()).unwrap();
            standby.scan();
            reference.scan();
            assert_eq!(
                standby.executor().emitted_events(),
                reference.emitted_events()
            );
        }

        standby.promote().unwrap();
        for tick in 4..7 {
            standby.scan();
            reference.scan();
            assert_eq!(
                standby.executor().emitted_events(),
                reference.emitted_events()
            );
            // The sequence continues — the promoted scan's `beat` counts
            // the adopted run's scans, not a restarted line's.
            assert_eq!(beat_n(standby.executor()), tick);
        }
        assert_eq!(standby.role(), Role::Active);
    }

    #[test]
    fn final_sync_is_a_no_op_for_a_failed_or_refused_pull() {
        let field = StubDriver::field(&[]);
        let gate = WriteGate::closed(&field);
        let mut standby = Peer::standby(Clocked::executor(&gate), Some(&gate));
        let source_driver = StubDriver::field(&[]);
        let mut source = Clocked::executor(&source_driver);
        source.run(2);
        standby.apply(&source.checkpoint()).unwrap();
        assert_eq!(
            standby.sync_state(),
            &StandbySync::Tracking { aligned: Tick(2) }
        );

        // A produced-nothing pull is no miss and no state change — the
        // standing convergence decides the promotion that follows.
        standby.final_sync(|| Err("fetch from active: refused".to_string()));
        assert_eq!(standby.missed_transfers(), 0);
        assert_eq!(
            standby.sync_state(),
            &StandbySync::Tracking { aligned: Tick(2) }
        );

        // A refused transfer — here a checkpoint whose component set
        // the run cannot restore — restores the standing report too.
        let mut corrupt = source.checkpoint();
        corrupt
            .components
            .insert("ghost".to_string(), Default::default());
        standby.final_sync(|| Ok(corrupt));
        assert_eq!(
            standby.sync_state(),
            &StandbySync::Tracking { aligned: Tick(2) }
        );
        assert!(standby.receipts().is_empty());

        // The standing proof still promotes.
        standby.promote().unwrap();
        assert_eq!(standby.role(), Role::Promoting);
    }

    #[test]
    fn final_sync_carries_a_stale_checkpoints_pending_commands() {
        // The driven pair's resting shape is the QA finding's: the
        // standby's last pull applied ckpt@2 and its scan advanced the
        // run to 3, so the freshest checkpoint the active can serve —
        // @2, carrying the invoke admitted since that pull — is stale
        // by the run's clock. The stale state cannot land without
        // rewinding the standby's own scan, but the receipt log's new
        // tail still carries: the pending invoke queues on the
        // standing state and settles once at the promoted run's first
        // scan.
        let field = StubDriver::field(&[]);
        let gate = WriteGate::closed(&field);
        let mut standby = Peer::standby(Clocked::executor(&gate), Some(&gate));
        let source_driver = StubDriver::field(&[]);
        let mut source = Clocked::executor(&source_driver);
        source.run(2);
        standby.apply(&source.checkpoint()).unwrap();
        standby.scan();
        assert_eq!(standby.tick(), Tick(3));

        source.submit_command(Clocked::bump(7));
        let checkpoint = source.checkpoint();
        assert_eq!(checkpoint.tick, Tick(2));
        standby.final_sync(|| Ok(checkpoint.clone()));

        // Carried, not rewound: the pending receipt joined the log and
        // queued, while the run's tick, its component state, and the
        // cadence's convergence verdict all stand.
        assert_eq!(standby.tick(), Tick(3));
        assert_eq!(standby.receipts().len(), 1);
        assert!(matches!(
            standby.receipts()[0].outcome,
            CommandOutcome::Accepted { .. }
        ));
        assert_eq!(Clocked::count(&standby.checkpoint()), Value::Int(0));
        assert_eq!(
            standby.sync_state(),
            &StandbySync::Tracking { aligned: Tick(2) }
        );

        // The same stale pull a second time carries nothing twice —
        // the log's overlap is already this run's.
        standby.final_sync(|| Ok(checkpoint));
        assert_eq!(standby.receipts().len(), 1);

        standby.promote().unwrap();
        standby.scan();
        assert_eq!(standby.role(), Role::Active);
        assert_eq!(
            standby.receipts()[0].outcome,
            CommandOutcome::Applied { tick: Tick(4) }
        );
        assert_eq!(Clocked::count(&standby.checkpoint()), Value::Int(7));

        // Never again: the settled outcome stays the log's one entry.
        standby.scan();
        assert_eq!(standby.receipts().len(), 1);
        assert_eq!(Clocked::count(&standby.checkpoint()), Value::Int(7));
    }

    #[test]
    fn final_sync_never_requeues_a_command_the_run_already_settled() {
        // The stale checkpoint's still-`Accepted` view of a receipt
        // this run carries is the tracked line lagging, not a new
        // admission: the suffix rule adopts only entries past the
        // log's own length, so a carried command never queues twice —
        // and the quiesced scan never settles it either. It stays
        // `Accepted` until the promotion boundary applies it once.
        let field = StubDriver::field(&[]);
        let gate = WriteGate::closed(&field);
        let mut standby = Peer::standby(Clocked::executor(&gate), Some(&gate));
        let source_driver = StubDriver::field(&[]);
        let mut source = Clocked::executor(&source_driver);
        source.run(2);
        source.submit_command(Clocked::bump(7));

        // The adopted `Accepted` invoke is carried, not settled: the
        // quiesced scan leaves the receipt `Accepted` and the run's
        // count at zero — no phantom `Applied` on an image the field
        // never saw — while the active, stalled at tick 2, still
        // serves it `Accepted`.
        standby.apply(&source.checkpoint()).unwrap();
        standby.scan();
        assert!(matches!(
            standby.receipts()[0].outcome,
            CommandOutcome::Accepted { .. }
        ));
        assert_eq!(Clocked::count(&standby.checkpoint()), Value::Int(0));

        standby.final_sync(|| Ok(source.checkpoint()));
        assert_eq!(standby.receipts().len(), 1);
        assert!(matches!(
            standby.receipts()[0].outcome,
            CommandOutcome::Accepted { .. }
        ));

        standby.promote().unwrap();
        standby.scan();
        // Settled once, at the promotion boundary — never re-applied:
        // the count holds at one bump.
        assert_eq!(
            standby.receipts()[0].outcome,
            CommandOutcome::Applied { tick: Tick(4) }
        );
        assert_eq!(Clocked::count(&standby.checkpoint()), Value::Int(7));
    }

    #[test]
    fn final_sync_carries_state_without_manufacturing_convergence() {
        // The boundary pull lands the checkpoint's state — the pending
        // invoke included — but the promotability proof stays the
        // tracking cadence's: a peer that never tracked reports
        // `Unsynchronized` still and is refused promotion even aligned
        // to the freshest state.
        let field = StubDriver::field(&[]);
        let gate = WriteGate::closed(&field);
        let mut standby = Peer::standby(Clocked::executor(&gate), Some(&gate));
        let source_driver = StubDriver::field(&[]);
        let mut source = Clocked::executor(&source_driver);
        source.run(5);
        source.submit_command(Clocked::bump(7));

        standby.final_sync(|| Ok(source.checkpoint()));
        assert_eq!(standby.executor().tick(), Tick(5));
        assert_eq!(standby.receipts().len(), 1);
        assert_eq!(standby.sync_state(), &StandbySync::Unsynchronized);
        assert_eq!(
            standby.promote(),
            Err(SwitchError::NotConverged {
                sync: StandbySync::Unsynchronized
            })
        );

        // The cadence's own proof still earns it: one applied tracking
        // pull on the same state, and the carried invoke promotes and
        // settles with the run.
        standby.apply(&source.checkpoint()).unwrap();
        standby.promote().unwrap();
        standby.scan();
        assert_eq!(standby.role(), Role::Active);
        assert_eq!(beat_n(standby.executor()), 6);
        assert_eq!(
            standby.receipts()[0].outcome,
            CommandOutcome::Applied { tick: Tick(6) }
        );
    }

    /// The QA finding `tracking-apply-rewinds-run-tick-on-source-restart`:
    /// a tracking peer whose source cold-restarts — its checkpoint
    /// stream regressing below the run's alignment — must adopt the new
    /// generation's state without rewinding its own tick, and the
    /// boundary must queue the named restart report the journal
    /// records.
    #[test]
    fn a_regressed_checkpoint_stream_resyncs_without_rewinding_the_run() {
        let driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let gate = WriteGate::closed(&driver);
        let mut peer = Peer::standby(executor(&gate), Some(&gate));

        // Converged at the source's tick 7, then two of the peer's own
        // scans — the standing tracking shape.
        let source_driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let mut source = executor(&source_driver);
        source.run(7);
        peer.apply(&source.checkpoint()).unwrap();
        peer.scan();
        peer.scan();
        assert_eq!(peer.tick(), Tick(9));

        // The source cold-restarts: a fresh run serves a tick-1
        // checkpoint. The peer adopts the restarted state — converged
        // and aligned to the new generation — but its own clock stands.
        let restarted_driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let mut restarted = executor(&restarted_driver);
        restarted.run(1);
        peer.apply(&restarted.checkpoint()).unwrap();
        assert_eq!(
            peer.tick(),
            Tick(9),
            "a regressed stream must not rewind the run's tick"
        );
        assert_eq!(peer.aligned_tick(), Some(Tick(1)));
        assert_eq!(
            peer.sync_state(),
            &StandbySync::Tracking { aligned: Tick(1) }
        );
        assert_eq!(
            peer.take_source_restarts(),
            vec![SourceRestart {
                tick: Tick(9),
                was_aligned: Some(Tick(7)),
                resumed_at: Tick(1),
            }]
        );

        // The new generation's stream lands at tick + offset — but the
        // offset covers only the run's live lead over the stream: the
        // apply holds the run's clock in place rather than injecting
        // the stream's delta, and the stored offset shrinks to the gap
        // that remains.
        restarted.run(1);
        peer.apply(&restarted.checkpoint()).unwrap();
        assert_eq!(peer.tick(), Tick(9));
        assert_eq!(peer.aligned_tick(), Some(Tick(2)));
        peer.scan();
        assert_eq!(peer.tick(), Tick(10));
        assert!(peer.take_source_restarts().is_empty());

        // A second cold start regresses the stream again: another
        // named report, and the apply lands at the run's tick again.
        let again_driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let mut again = executor(&again_driver);
        again.run(1);
        peer.apply(&again.checkpoint()).unwrap();
        assert_eq!(peer.tick(), Tick(10));
        assert_eq!(
            peer.take_source_restarts(),
            vec![SourceRestart {
                tick: Tick(10),
                was_aligned: Some(Tick(2)),
                resumed_at: Tick(1),
            }]
        );

        // Once the tracked stream recovers to the run's tick the
        // seeded offset clears entirely: the apply lands at the
        // stream's own tick — no residual lead — and tracking
        // continues in the stream's domain from there.
        again.run(11);
        peer.apply(&again.checkpoint()).unwrap();
        assert_eq!(peer.tick(), Tick(12));
        assert_eq!(peer.aligned_tick(), Some(Tick(12)));
        again.run(1);
        peer.apply(&again.checkpoint()).unwrap();
        assert_eq!(peer.tick(), Tick(13));
        assert!(peer.take_source_restarts().is_empty());
    }

    /// The demoted-peer half of the finding — the driven-failover
    /// shape: a fenced peer demotes, clearing its alignment, then pulls
    /// the cold-restarted successor's low-tick stream. With no
    /// alignment standing the regression measures against the run's own
    /// tick — the resync still lands monotone.
    #[test]
    fn a_demoted_peers_first_pull_on_a_restarted_source_stays_monotone() {
        let driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let gate = WriteGate::closed(&driver);
        let mut peer = Peer::standby(executor(&gate), Some(&gate));

        let source_driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let mut source = executor(&source_driver);
        source.run(4);
        peer.apply(&source.checkpoint()).unwrap();
        peer.promote().unwrap();
        peer.scan();
        peer.demote().unwrap();
        assert_eq!(peer.aligned_tick(), None);
        peer.scan();
        assert_eq!(peer.tick(), Tick(6));

        let restarted_driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let mut restarted = executor(&restarted_driver);
        restarted.run(1);
        peer.apply(&restarted.checkpoint()).unwrap();
        assert_eq!(peer.tick(), Tick(6));
        assert_eq!(peer.aligned_tick(), Some(Tick(1)));
        assert_eq!(
            peer.take_source_restarts(),
            vec![SourceRestart {
                tick: Tick(6),
                was_aligned: None,
                resumed_at: Tick(1),
            }]
        );
    }

    /// The QA finding `source-restarted-journaled-on-demote-track`: the
    /// same demote-then-pull shape on the *uninterrupted* successor. The
    /// demotion cleared the alignment, so the successor's served tick
    /// regresses against the run's own — but the stream still names the
    /// generation this run's own captures stamped, because the
    /// successor's executor adopted it tracking this peer. The reset was
    /// the peer's tracking state, not the source's restart: the state
    /// still adopts at the run's tick, and nothing journals.
    #[test]
    fn a_demoted_peers_reset_on_the_same_generation_journals_no_restart() {
        let driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let gate = WriteGate::closed(&driver);
        let mut peer = Peer::active(executor(&gate).with_generation(7), Some(&gate));
        peer.activate().unwrap();
        peer.scan();
        peer.scan();

        // The promoted successor's executor carries this line's
        // generation — adopted tracking this peer's stream, exactly as
        // the pair's pull path does — while its served tick still trails
        // the demoted run's own.
        let source_driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let mut source = executor(&source_driver);
        source.apply(&peer.checkpoint()).unwrap();

        peer.demote().unwrap();
        assert_eq!(peer.aligned_tick(), None);
        peer.scan();
        assert_eq!(peer.tick(), Tick(3));

        // ckpt{tick: 2} against run tick 3 with no alignment standing:
        // the regression the heuristic used to journal.
        peer.apply(&source.checkpoint()).unwrap();
        assert_eq!(peer.tick(), Tick(3));
        assert_eq!(peer.aligned_tick(), Some(Tick(2)));
        assert!(peer.take_source_restarts().is_empty());
    }

    /// The generation check's other half on the same shape: the
    /// demoted peer's first pull on a genuinely restarted source —
    /// its checkpoints name a generation this run's stream never
    /// carried — still journals the `SourceRestart` with no prior
    /// alignment.
    #[test]
    fn a_demoted_peers_reset_on_a_new_generation_journals_the_restart() {
        let driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let gate = WriteGate::closed(&driver);
        let mut peer = Peer::active(executor(&gate).with_generation(7), Some(&gate));
        peer.activate().unwrap();
        peer.scan();
        peer.scan();
        peer.demote().unwrap();
        peer.scan();
        assert_eq!(peer.tick(), Tick(3));

        // The cold-restarted source minted its own generation — a new
        // tick domain the demoted run's captures never stamped.
        let restarted_driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let mut restarted = executor(&restarted_driver).with_generation(9);
        restarted.run(1);
        peer.apply(&restarted.checkpoint()).unwrap();
        assert_eq!(peer.tick(), Tick(3));
        assert_eq!(peer.aligned_tick(), Some(Tick(1)));
        assert_eq!(
            peer.take_source_restarts(),
            vec![SourceRestart {
                tick: Tick(3),
                was_aligned: None,
                resumed_at: Tick(1),
            }]
        );
    }

    /// The contrast case the offset must not break: a post-miss
    /// catch-up checkpoint lags the run's tick but continues the same
    /// generation — the apply still realigns the run to the stream's
    /// own tick, the rewind the freshness budgets are measured
    /// against.
    #[test]
    fn a_same_generation_catch_up_still_realigns_the_run_tick() {
        let driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let gate = WriteGate::closed(&driver);
        let mut peer = Peer::standby(executor(&gate), Some(&gate));

        let source_driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let mut source = executor(&source_driver);
        source.run(4);
        peer.apply(&source.checkpoint()).unwrap();

        // Misses while the peer keeps scanning — the pull produced
        // nothing, the run ticked on past the alignment.
        peer.note_transfer_failed("active gone");
        peer.note_transfer_failed("active gone");
        peer.scan();
        peer.scan();
        assert_eq!(peer.tick(), Tick(6));

        // The source's resumed stream — warm-restarted or merely
        // lagging — still realigns the run at its own tick: same
        // generation, not a restart, so the apply does move the clock
        // back to the stream's line.
        source.run(1);
        peer.apply(&source.checkpoint()).unwrap();
        assert_eq!(peer.tick(), Tick(5));
        assert_eq!(peer.aligned_tick(), Some(Tick(5)));
        assert!(peer.take_source_restarts().is_empty());
    }

    /// QA finding `mutual-tracking-regressed-offset-ratchets-run-tick`:
    /// under mutual tracking — two standby peers applying each other's
    /// checkpoints, the transient every demote creates — a regressed
    /// apply that seeds a nonzero `tick_offset` must never feed back:
    /// each apply lands the run at the tracked tick or holds it at its
    /// own, never ahead of both, so alternating applies keep both run
    /// ticks bounded by the scan count instead of compounding each
    /// peer's served tick into the other's.
    #[test]
    fn a_seeded_offset_cannot_ratchet_mutually_tracking_peers() {
        let driver_a = StubDriver::new(PointId(1), Value::Float(0.0));
        let gate_a = WriteGate::closed(&driver_a);
        let mut a = Peer::standby(executor(&gate_a), Some(&gate_a));
        let driver_b = StubDriver::new(PointId(1), Value::Float(0.0));
        let gate_b = WriteGate::closed(&driver_b);
        let mut b = Peer::standby(executor(&gate_b), Some(&gate_b));

        // Mutual tracking at cadence: one scan and one apply of the
        // peer's checkpoint per cycle on each side — the dual-standby
        // shape a demote creates while the standing standby still
        // tracks. Both run ticks stay on the shared line.
        for _ in 0..5 {
            a.scan();
            b.scan();
            a.apply(&b.checkpoint()).unwrap();
            b.apply(&a.checkpoint()).unwrap();
        }
        assert_eq!(a.tick(), b.tick());

        // A regressed apply on each peer — the resumed pull landing a
        // source tick below the alignment — seeds each generation
        // offset; both runs hold their own ticks rather than rewinding.
        let cold_driver_a = StubDriver::new(PointId(1), Value::Float(0.0));
        let mut cold_a = executor(&cold_driver_a);
        cold_a.run(1);
        a.apply(&cold_a.checkpoint()).unwrap();
        assert_eq!(a.take_source_restarts().len(), 1);
        let cold_driver_b = StubDriver::new(PointId(1), Value::Float(0.0));
        let mut cold_b = executor(&cold_driver_b);
        cold_b.run(1);
        b.apply(&cold_b.checkpoint()).unwrap();
        assert_eq!(b.take_source_restarts().len(), 1);

        // The mutual cycle resumes. Without the offset's re-evaluation
        // each apply lands at the peer's served tick plus the standing
        // offset and serves the sum back — the positive-feedback
        // ratchet the finding measured at ~1e3 ticks/s against a ~10/s
        // cadence. Bounded: each peer's run tick stays within the
        // pair's maximum plus the scans that actually ran.
        let start = a.tick().0.max(b.tick().0);
        const CYCLES: u64 = 50;
        for _ in 0..CYCLES {
            a.scan();
            b.scan();
            a.apply(&b.checkpoint()).unwrap();
            b.apply(&a.checkpoint()).unwrap();
        }
        assert!(
            a.tick().0 <= start + CYCLES && b.tick().0 <= start + CYCLES,
            "run ticks must stay bounded by the scan count, not compound: a={} b={} bound={}",
            a.tick().0,
            b.tick().0,
            start + CYCLES
        );
        assert!(
            (a.tick().0 as i64 - b.tick().0 as i64).abs() <= 1,
            "mutual peers track within scan cadence of each other: a={} b={}",
            a.tick().0,
            b.tick().0
        );
    }

    /// QA finding `demote-boundary-pending-command-lost-or-phantom-applied`,
    /// the quiesced-scan-wins half: a command admitted on the active
    /// between its last served checkpoint and the demote must not
    /// apply on the demoted run's first quiesced scan — the gate
    /// already closed, so an `Applied` there would journal an
    /// application the field never saw, erased by the next adoption.
    /// Demotion suspends the pending queue instead of settling it:
    /// the receipt stays `Accepted` so the checkpoints this peer keeps
    /// serving still carry the command for the successor's
    /// final-sync pull.
    #[test]
    fn a_demotion_suspends_pending_commands_without_settling_them() {
        let driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let gate = WriteGate::closed(&driver);
        let mut peer = Peer::active(Clocked::executor(&gate), Some(&gate));
        peer.activate().unwrap();
        peer.scan();
        peer.submit_command(Clocked::bump(7));
        assert!(matches!(
            peer.receipts()[0].outcome,
            CommandOutcome::Accepted { .. }
        ));

        peer.demote().unwrap();
        // Suspended, not settled: the receipt stays `Accepted`, and the
        // checkpoint this peer still serves carries it for the
        // successor's promotion-boundary pull.
        assert!(matches!(
            peer.receipts()[0].outcome,
            CommandOutcome::Accepted { .. }
        ));
        assert!(matches!(
            peer.checkpoint().receipts[0].outcome,
            CommandOutcome::Accepted { .. }
        ));

        // The quiesced scans cannot apply it: the suspended queue no
        // longer feeds `apply_commands`, so no phantom `Applied`
        // settles on the abandoned image.
        peer.scan();
        peer.scan();
        assert_eq!(peer.role(), Role::Standby);
        assert!(matches!(
            peer.receipts()[0].outcome,
            CommandOutcome::Accepted { .. }
        ));
        assert_eq!(Clocked::count(&peer.checkpoint()), Value::Int(0));
    }

    /// The apply-wins half of the finding: a pending command the
    /// tracked line demonstrably passed by settles `Rejected` carrying
    /// `Superseded` rather than vanishing unaudited. Passed by means
    /// adjudicated, not merely absent: the adopted receipt window's
    /// submission high-water covers the orphan's index, and the entry
    /// the line holds there is a different command — the successor's
    /// history moved on with admissions of its own, never carrying
    /// this one. (The stale-checkpoint case — a window that never
    /// reached the index — is the next test's: it settles nothing.)
    #[test]
    fn an_adoption_orphaned_pending_command_settles_superseded() {
        let driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let gate = WriteGate::closed(&driver);
        let mut peer = Peer::active(Clocked::executor(&gate), Some(&gate));
        peer.activate().unwrap();
        peer.scan();

        // The tracked line — a successor that started from this peer's
        // pre-admission checkpoint but moved on with an admission of
        // its own: its receipt window covers index 0 with a different
        // command, so the orphan's absence is adjudicated.
        let source_driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let source_gate = WriteGate::closed(&source_driver);
        let mut source = Peer::standby(Clocked::executor(&source_gate), Some(&source_gate));
        source.apply(&peer.checkpoint()).unwrap();
        // The successor settles its own admission on the live line: it
        // promotes first — a quiesced standby scan must not settle it —
        // then its field-owning scan applies the bump.
        source.promote().unwrap();
        source.submit_command(Clocked::bump(3));
        source.scan();

        // The admission lands after the line's last pull; the demotion
        // suspends it. The demoted peer's first tracking apply then
        // adopts a receipt log whose high-water passed the entry's
        // index carrying a different command: orphaned, it settles
        // superseded and queues for the journal rather than vanishing
        // unaudited.
        peer.submit_command(Clocked::bump(7));
        peer.demote().unwrap();
        peer.apply(&source.checkpoint()).unwrap();
        assert_eq!(peer.receipts(), source.receipts());
        let superseded = peer.take_superseded_commands();
        assert_eq!(superseded.len(), 1, "{superseded:?}");
        assert_eq!(superseded[0].command, Clocked::bump(7));
        assert_eq!(
            superseded[0].outcome,
            CommandOutcome::Rejected {
                reason: CommandError::Superseded { point: None }
            }
        );
        // The drain empties — one settlement per orphan, journaled once.
        assert!(peer.take_superseded_commands().is_empty());
        // No strays: the superseded command never re-queues, so the
        // demoted run's next scan applies nothing for it — the count
        // stays the adopted line's own bump, never the orphan's — and
        // no later adoption or scan can mint an `applied` beside the
        // settled `superseded`.
        peer.scan();
        assert_eq!(Clocked::count(&peer.checkpoint()), Value::Int(3));
        peer.apply(&source.checkpoint()).unwrap();
        peer.scan();
        assert!(peer.take_superseded_commands().is_empty());
        assert_eq!(peer.receipts(), source.receipts());
        assert_eq!(Clocked::count(&peer.checkpoint()), Value::Int(3));
    }

    /// QA finding `superseded-command-still-settles-applied`: the
    /// demote boundary's stale-checkpoint collision. A tracking pull
    /// can land a checkpoint the source captured before it ever
    /// observed the admission — the receipt window's submission
    /// high-water never reached the suspended entry's index, so its
    /// absence there is the capture's staleness, not the line's
    /// verdict. The adoption restores the unreached tail suspended:
    /// no `superseded` journals, the checkpoint this peer keeps
    /// serving still offers the command to the successor's
    /// final-sync carry, and the covering adoption that follows
    /// settles it exactly once — never a provisional `superseded`
    /// beside the `applied`.
    #[test]
    fn a_stale_adoption_cannot_supersede_a_suspended_command() {
        let driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let gate = WriteGate::closed(&driver);
        let mut peer = Peer::active(Clocked::executor(&gate), Some(&gate));
        peer.activate().unwrap();
        peer.scan();

        // The tracked line — the QA reproduction's successor, whose
        // last pull predates the admission: its served checkpoint's
        // receipt window never reached the command's index.
        let source_driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let source_gate = WriteGate::closed(&source_driver);
        let mut source = Peer::standby(Clocked::executor(&source_gate), Some(&source_gate));
        source.apply(&peer.checkpoint()).unwrap();
        source.scan();

        // The raced admission and demote: the pending command suspends
        // at submission index 0.
        peer.submit_command(Clocked::bump(7));
        peer.demote().unwrap();

        // The first tracking pull lands the stale checkpoint: the
        // suspended entry is restored behind the adopted window, not
        // settled `superseded` — nothing journals, and the checkpoint
        // this peer still serves keeps offering it.
        peer.apply(&source.checkpoint()).unwrap();
        assert!(peer.take_superseded_commands().is_empty());
        assert_eq!(peer.receipt_base(), 0);
        assert_eq!(peer.receipts().len(), 1);
        assert!(matches!(
            peer.receipts()[0].outcome,
            CommandOutcome::Accepted { .. }
        ));
        assert!(matches!(
            peer.checkpoint().receipts[0].outcome,
            CommandOutcome::Accepted { .. }
        ));
        // Restored entries stay suspended — the demoted run's quiesced
        // scan applies nothing for them.
        peer.scan();
        assert_eq!(Clocked::count(&peer.checkpoint()), Value::Int(0));

        // The successor's promote-boundary pull still finds the
        // command in the served checkpoint, carries it, and settles it
        // on the live line.
        source.final_sync(|| Ok(peer.checkpoint()));
        source.promote().unwrap();
        source.scan();
        assert_eq!(
            source.receipts()[0].outcome,
            CommandOutcome::Applied { tick: Tick(4) }
        );

        // The covering adoption converges the log on the line's
        // verdict: the receipt settles `applied` — the one terminal
        // outcome, with no `superseded` ever journaled beside it.
        peer.apply(&source.checkpoint()).unwrap();
        assert!(peer.take_superseded_commands().is_empty());
        assert_eq!(peer.receipts(), source.receipts());
        assert_eq!(
            peer.receipts()[0].outcome,
            CommandOutcome::Applied { tick: Tick(4) }
        );
        peer.scan();
        assert_eq!(Clocked::count(&peer.checkpoint()), Value::Int(7));
        assert_eq!(peer.receipts().len(), 1);
    }

    /// The carried half of the fix: a suspended command the tracked
    /// line did pick up — the successor's final-sync carry in the
    /// documented demote-then-promote order — is *covered* by the
    /// adopted receipt log, not abandoned: it re-queues at the
    /// adoption and settles with the run, never superseded, never
    /// double-settled.
    #[test]
    fn a_carried_pending_command_is_not_superseded_by_adoption() {
        let driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let gate = WriteGate::closed(&driver);
        let mut peer = Peer::active(Clocked::executor(&gate), Some(&gate));
        peer.activate().unwrap();
        peer.scan();

        let source_driver = StubDriver::new(PointId(1), Value::Float(0.0));
        let source_gate = WriteGate::closed(&source_driver);
        let mut source = Peer::standby(Clocked::executor(&source_gate), Some(&source_gate));
        source.apply(&peer.checkpoint()).unwrap();
        source.scan();

        // The admission lands after the line's last pull; the demotion
        // suspends it; the source's promote-boundary `final_sync`
        // carries it from the checkpoint this peer still serves —
        // the documented demote-then-promote order.
        peer.submit_command(Clocked::bump(7));
        peer.demote().unwrap();
        source.final_sync(|| Ok(peer.checkpoint()));
        assert!(matches!(
            source.receipts()[0].outcome,
            CommandOutcome::Accepted { .. }
        ));

        // The demoted peer's next tracking apply adopts the successor's
        // checkpoint: the carried entry is the same command at the same
        // submission index — covered — so nothing supersedes. The
        // adopted pending entry is carried, not settled: the quiesced
        // scan leaves it `Accepted` — a gated peer never mints an
        // `Applied` the line never ordered — and it settles once at
        // the promoted run's first field-owning scan.
        peer.apply(&source.checkpoint()).unwrap();
        assert!(peer.take_superseded_commands().is_empty());
        assert_eq!(peer.receipts(), source.receipts());
        assert!(matches!(
            peer.receipts()[0].outcome,
            CommandOutcome::Accepted { .. }
        ));
        peer.scan();
        assert!(matches!(
            peer.receipts()[0].outcome,
            CommandOutcome::Accepted { .. }
        ));
        assert_eq!(Clocked::count(&peer.checkpoint()), Value::Int(0));
        assert!(peer.take_superseded_commands().is_empty());

        peer.promote().unwrap();
        peer.scan();
        assert_eq!(
            peer.receipts()[0].outcome,
            CommandOutcome::Applied { tick: Tick(4) }
        );
        assert_eq!(Clocked::count(&peer.checkpoint()), Value::Int(7));
    }

    /// QA finding `quiesced-standby-scan-settles-adopted-pending-commands`
    /// (#689): a standby's quiesced scan must not apply an adopted
    /// still-`Accepted` internal-point command. Seed the receipt log via
    /// a checkpoint apply, run one quiesced scan, assert the receipt
    /// stays `Accepted` and the staged image is untouched; then the
    /// promoted peer applies the carried command itself — a single
    /// settle at a tick on or past the promotion boundary.
    #[test]
    fn a_quiesced_scan_carries_adopted_pending_commands_for_promotion() {
        use dcs_core::Command;
        let point = PointId(10);
        let map = || {
            PointMap::new().with_writable_internal(
                point,
                Direction::In,
                ValueKind::Float,
                Value::Float(0.0),
            )
        };
        let field = StubDriver::field(&[]);
        let gate = WriteGate::closed(&field);
        let mut standby = Peer::standby(
            Executor::new(&gate, map(), Vec::new()).unwrap(),
            Some(&gate),
        );

        // The active admits an internal-point write and serves it still
        // `Accepted` in its checkpoint.
        let source_driver = StubDriver::field(&[]);
        let mut source = Executor::new(&source_driver, map(), Vec::new()).unwrap();
        source.run(2);
        source.submit_command(Command::WriteValue {
            point,
            kind: ValueKind::Float,
            value: Value::Float(7.0),
        });
        let checkpoint = source.checkpoint();
        assert!(matches!(
            checkpoint.receipts[0].outcome,
            CommandOutcome::Accepted { .. }
        ));

        // Adopt: the standby carries the pending command.
        standby.apply(&checkpoint).unwrap();
        assert!(matches!(
            standby.receipts()[0].outcome,
            CommandOutcome::Accepted { .. }
        ));

        // One quiesced scan: no settle, no image mutation — the receipt
        // stays `Accepted` and nothing queues for the settle journal.
        standby.scan();
        assert!(matches!(
            standby.receipts()[0].outcome,
            CommandOutcome::Accepted { .. }
        ));
        assert!(standby.take_superseded_commands().is_empty());
        assert_eq!(
            standby.executor().sample(point).map(|sample| sample.value),
            Some(Value::Float(0.0))
        );

        // Promotion applies the carried command itself: a single settle
        // at or past the promotion boundary.
        let boundary = standby.tick();
        standby.promote().unwrap();
        standby.scan();
        assert_eq!(standby.role(), Role::Active);
        match standby.receipts()[0].outcome {
            CommandOutcome::Applied { tick } => assert!(tick >= Tick(boundary.0 + 1)),
            ref other => panic!("carried command must settle Applied once, got {other:?}"),
        }
        assert_eq!(
            standby.executor().sample(point).map(|sample| sample.value),
            Some(Value::Float(7.0))
        );
        // Never again: the settled outcome stays the log's one entry.
        standby.scan();
        assert_eq!(standby.receipts().len(), 1);
        assert!(matches!(
            standby.receipts()[0].outcome,
            CommandOutcome::Applied { .. }
        ));
    }

    /// QA finding `stale-checkpoint-resurrects-receipted-unforce`
    /// (#639): a standby restarting onto a staler peer's checkpoint
    /// must not re-stand a force its own journal already receipted as
    /// released. The applied `unforce` receipt is durable truth — the
    /// adoption replays it over the adopted force set, the scan
    /// reports the field's live value at `Good` quality, and no
    /// adoption-audit receipt queues because the merged log already
    /// accounts for the outcome.
    #[test]
    fn a_receipted_unforce_survives_adopting_a_stale_checkpoint() {
        const POINT: PointId = PointId(10);
        let map = || PointMap::new().with_writable_point(POINT, Direction::In, ValueKind::Float);
        let force = || Command::ForcePoint {
            point: POINT,
            kind: ValueKind::Float,
            value: Value::Float(5.0),
        };

        // The tracked peer forces the point; its served checkpoint
        // freezes there — captured before the release could ever
        // reach its journal.
        let a_driver = StubDriver::new(POINT, Value::Float(1.0));
        let a_gate = WriteGate::closed(&a_driver);
        let mut a = Peer::active(
            Executor::new(&a_gate, map(), Vec::new()).unwrap(),
            Some(&a_gate),
        );
        a.activate().unwrap();
        a.submit_command(force());
        a.scan();
        let stale = a.checkpoint();

        // The standby adopts the force, promotes, and releases it —
        // applied and journaled on its own run.
        let b_driver = StubDriver::new(POINT, Value::Float(1.0));
        let b_gate = WriteGate::closed(&b_driver);
        let mut b = Peer::standby(
            Executor::new(&b_gate, map(), Vec::new()).unwrap(),
            Some(&b_gate),
        );
        b.apply(&stale).unwrap();
        b.promote().unwrap();
        b.scan();
        b.submit_command(Command::UnforcePoint { point: POINT });
        b.scan();
        assert!(b.executor().forces().is_empty());
        assert!(matches!(
            b.receipts().last().unwrap().outcome,
            CommandOutcome::Applied { .. }
        ));

        // The restart: a fresh peer resumes from the run's own state
        // — the receipt log and the released force set carry — then
        // tracks the stale image.
        let resumed = Executor::restore(&b_gate, map(), Vec::new(), &b.checkpoint(), None).unwrap();
        let mut restarted = Peer::standby(resumed, Some(&b_gate));
        restarted.apply(&stale).unwrap();

        // The adopted image's force set is already reverted by the
        // journaled release: the resurrection never happens, so the
        // audit queues nothing.
        assert!(restarted.executor().forces().is_empty());
        assert!(restarted.take_adoption_receipts().is_empty());

        // And the scan reports the field's live value at `Good`
        // quality — the silent `Substituted` return is the defect.
        restarted.scan();
        assert_eq!(
            restarted.executor().sample(POINT),
            Some(Sample::good(Value::Float(1.0), Tick(4)))
        );
        assert!(restarted.executor().snapshot().forces.is_empty());
    }

    /// The audit's positive half: a checkpoint whose force set stands
    /// a force the merged receipt log cannot account for journals the
    /// adoption itself — one `Applied` receipt whose `actor` names the
    /// adopting checkpoint — rather than letting the change stand
    /// silently.
    #[test]
    fn an_unbacked_adopted_force_journals_a_receipt_naming_the_source() {
        use crate::checkpoint::CommandAdmissionCounts;
        const POINT: PointId = PointId(10);
        let map = || PointMap::new().with_writable_point(POINT, Direction::In, ValueKind::Float);

        let a_driver = StubDriver::new(POINT, Value::Float(1.0));
        let a_gate = WriteGate::closed(&a_driver);
        let mut a = Peer::active(
            Executor::new(&a_gate, map(), Vec::new()).unwrap(),
            Some(&a_gate),
        );
        a.activate().unwrap();
        a.submit_command(Command::ForcePoint {
            point: POINT,
            kind: ValueKind::Float,
            value: Value::Float(5.0),
        });
        a.scan();
        let mut checkpoint = a.checkpoint();
        // The carried force loses its receipt: the merged log cannot
        // account for the adopted state.
        checkpoint.receipts.clear();
        checkpoint.command_admission = CommandAdmissionCounts::default();

        let b_driver = StubDriver::new(POINT, Value::Float(1.0));
        let b_gate = WriteGate::closed(&b_driver);
        let mut b = Peer::standby(
            Executor::new(&b_gate, map(), Vec::new()).unwrap(),
            Some(&b_gate),
        );
        b.apply(&checkpoint).unwrap();

        assert_eq!(b.executor().forces()[&POINT], Value::Float(5.0));
        assert_eq!(
            b.take_adoption_receipts(),
            vec![CommandReceipt {
                command: Command::ForcePoint {
                    point: POINT,
                    kind: ValueKind::Float,
                    value: Value::Float(5.0),
                },
                outcome: CommandOutcome::Applied { tick: Tick(1) },
                actor: Some("checkpoint@1".to_string()),
            }]
        );
        // The drain empties — one audit receipt per unbacked change.
        assert!(b.take_adoption_receipts().is_empty());
    }

    /// The mirror image: an adoption that *drops* a standing force the
    /// merged log still shows receipted journals the release the same
    /// way — while a drop the adopted log's own `unforce` verdict
    /// explains queues nothing.
    #[test]
    fn an_unbacked_dropped_force_journals_a_release_naming_the_source() {
        const POINT: PointId = PointId(10);
        let map = || PointMap::new().with_writable_point(POINT, Direction::In, ValueKind::Float);

        let a_driver = StubDriver::new(POINT, Value::Float(1.0));
        let a_gate = WriteGate::closed(&a_driver);
        let mut a = Peer::active(
            Executor::new(&a_gate, map(), Vec::new()).unwrap(),
            Some(&a_gate),
        );
        a.activate().unwrap();
        a.submit_command(Command::ForcePoint {
            point: POINT,
            kind: ValueKind::Float,
            value: Value::Float(5.0),
        });
        a.scan();
        let forced = a.checkpoint();

        let b_driver = StubDriver::new(POINT, Value::Float(1.0));
        let b_gate = WriteGate::closed(&b_driver);
        let mut b = Peer::standby(
            Executor::new(&b_gate, map(), Vec::new()).unwrap(),
            Some(&b_gate),
        );
        // The backed adoption: the checkpoint's own force receipt
        // explains the stood-up force — nothing queues.
        b.apply(&forced).unwrap();
        assert!(b.take_adoption_receipts().is_empty());

        // The receipted release: the fresher checkpoint's `unforce`
        // verdict explains the empty force set — nothing queues.
        a.submit_command(Command::UnforcePoint { point: POINT });
        a.scan();
        b.apply(&a.checkpoint()).unwrap();
        assert!(b.executor().forces().is_empty());
        assert!(b.take_adoption_receipts().is_empty());

        // The unbacked drop: a peer whose covered log still shows the
        // force standing adopts a checkpoint whose force set drops it
        // — the change has the adoption as its only cause, so it
        // journals.
        let c_driver = StubDriver::new(POINT, Value::Float(1.0));
        let c_gate = WriteGate::closed(&c_driver);
        let mut c = Peer::standby(
            Executor::new(&c_gate, map(), Vec::new()).unwrap(),
            Some(&c_gate),
        );
        c.apply(&forced).unwrap();
        assert!(c.take_adoption_receipts().is_empty());
        let mut dropped = forced.clone();
        dropped.tick = Tick(2);
        dropped.forces.clear();
        c.apply(&dropped).unwrap();
        assert!(c.executor().forces().is_empty());
        assert_eq!(
            c.take_adoption_receipts(),
            vec![CommandReceipt {
                command: Command::UnforcePoint { point: POINT },
                outcome: CommandOutcome::Applied { tick: Tick(2) },
                actor: Some("checkpoint@2".to_string()),
            }]
        );
    }
}
