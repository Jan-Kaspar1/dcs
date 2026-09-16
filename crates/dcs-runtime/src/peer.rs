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
//! The field-ownership claim installed by
//! [`with_field_claim`](Peer::with_field_claim) runs at every transition
//! into field ownership — a promotion, manual or automatic, and a
//! launched-active peer's [`activate`](Peer::activate) at startup: the
//! fencing arbitration that makes the shared field refuse every
//! attachment not holding it. A failed claim refuses the transition
//! with [`SwitchError::FieldClaimFailed`] — a peer that cannot take the
//! field's single-writer arbitration does not take the field. And when
//! a claim the owner held is preempted — the field's arbitration is
//! unconditional, so a rogue claim can take it — the first fenced field
//! write queues a [`FencingLoss`] for the journal beside the scan
//! failure: the loss of the field's single-writer claim is a recorded
//! run event, not only an exit cause.
//!
//! Convergence alone does not prove the standby would write the field the
//! active writes, so a tracking peer also runs the standby-divergence
//! check of [`crate::divergence`]: each non-field-owning scan's staged
//! field `Out` image — the writes it would have issued — is stashed, and
//! each applied checkpoint whose tick matches that image compares it
//! against the peer's own reads of the same points. A mismatch moves the
//! peer to [`StandbySync::Diverged`], which promotion refuses like any
//! non-tracking state; the next clean transfer whose comparison matches
//! returns the peer to `Tracking`. A diverged peer's gate stays closed
//! throughout — the check observes, it never writes.

use crate::checkpoint::{Checkpoint, RestoreError};
use crate::divergence::{DivergenceReport, compare_staged};
use crate::executor::{Executor, ScanError};
use crate::gate::WriteGate;
use crate::revision::CarryoverError;
use dcs_core::{
    CarryoverReport, Command, CommandReceipt, IoError, PointId, Role, RoleReport, Sample,
    StandbySync, SwitchError, TelemetrySnapshot, Tick,
};
use std::collections::BTreeMap;
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
    /// The last applied checkpoint's tick, while any transfer has
    /// succeeded — kept beside `sync` so `aligned_tick` still reports it
    /// across a `Degraded` or `Diverged` state.
    aligned: Option<Tick>,
    /// Reported-role transitions not yet consumed for journaling.
    pending_changes: Vec<RoleChange>,
    /// The last non-field-owning scan's staged field `Out` image and its
    /// tick — the divergence check's "would have written" evidence,
    /// compared against the field when a checkpoint lands at that tick.
    staged: Option<(Tick, BTreeMap<PointId, Sample>)>,
    /// Divergence detections not yet consumed for journaling — one per
    /// transition into [`StandbySync::Diverged`].
    pending_divergences: Vec<DivergenceReport>,
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
    /// Whether the field-ownership claim this peer holds was observed
    /// lost — set when a field-owning scan's write reports
    /// [`IoError::Fenced`], meaning another attachment now holds the
    /// claim. Re-armed by each successful claim lift: the queued report
    /// is once per ownership, not once per fenced scan.
    fencing_lost: bool,
    /// Claim losses not yet consumed for journaling — one
    /// [`FencingLoss`] per observed preemption.
    pending_fencing: Vec<FencingLoss>,
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
    /// carrying this report.
    Reinitialized(CarryoverReport),
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
            pending_changes: Vec::new(),
            staged: None,
            pending_divergences: Vec::new(),
            misses: 0,
            converged: false,
            failover: None,
            claim: None,
            release: None,
            revision: false,
            pending_reinits: Vec::new(),
            fencing_lost: false,
            pending_fencing: Vec::new(),
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
            pending_changes: Vec::new(),
            staged: None,
            pending_divergences: Vec::new(),
            misses: 0,
            converged: false,
            failover: None,
            claim: None,
            release: None,
            revision: false,
            pending_reinits: Vec::new(),
            fencing_lost: false,
            pending_fencing: Vec::new(),
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
    /// known to be aligned with the active's.
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
        self.sync = StandbySync::Unsynchronized;
        self.aligned = None;
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
    /// their commands and re-emitting their events — so it is skipped.
    /// A landed checkpoint is an ordinary [`transfer`](Self::transfer)
    /// on the executor side — the reinitialization report a
    /// model-boundary crossing owes the journal queues as it would on
    /// any pull — but the standing the promotion check reads is
    /// restored regardless of the outcome, as below.
    ///
    /// The staged `Out` image is discarded first rather than carried
    /// into the transfer's divergence comparison: the staged evidence
    /// describes the tracking line the applied checkpoint abandons —
    /// what the quiesced scan *would* have written — and at the
    /// promotion boundary a same-tick comparison would flag the
    /// one-tick lag a field-carried command write inherently leaves on
    /// a peer that cannot issue it: the standby's own scan ran on the
    /// field's pre-command value, the checkpoint carries the post-write
    /// state, and the promoted run continues from the checkpoint, not
    /// from the staged what-if.
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
            return;
        }
        self.staged = None;
        let (sync, converged) = (self.sync.clone(), self.converged);
        let _ = self.transfer(&checkpoint);
        self.sync = sync;
        self.converged = converged;
    }

    /// Applies a checkpoint received from the active, aligning the run
    /// at the checkpointed tick — the tracking half of the redundancy
    /// contract, in place on the running executor.
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
    /// the checkpoint's tick equals the stashed staged image's tick —
    /// under the documented pull-per-scan cadence that pairing lands one
    /// transfer after the staged scan, the stated detection bound: the
    /// field then holds the active's write for the tick the staged image
    /// describes. Mismatches move the peer to
    /// [`StandbySync::Diverged`] and queue a
    /// [`DivergenceReport`] for the journal — one per transition, not
    /// per transfer — while a matching comparison on a diverged peer is
    /// the resync that returns it to [`StandbySync::Tracking`]. A
    /// staged image the checkpoint stream has not caught up to — or has
    /// overtaken — is discarded: only a same-tick comparison is honest
    /// evidence.
    pub fn apply(&mut self, checkpoint: &Checkpoint) -> Result<(), ApplyError> {
        if self.owns_field() {
            return Err(ApplyError::OwnsField);
        }
        // The pull produced a checkpoint — the active served, so it is
        // alive: the heartbeat miss count resets whether the apply
        // lands or is rejected.
        self.misses = 0;
        match self.executor.apply(checkpoint) {
            Ok(()) => {
                let was_diverged = matches!(self.sync, StandbySync::Diverged { .. });
                self.sync = StandbySync::Tracking {
                    aligned: checkpoint.tick,
                };
                self.aligned = Some(checkpoint.tick);
                self.converged = true;
                if let Some((tick, staged)) = self.staged.take()
                    && tick == checkpoint.tick
                {
                    let mismatches = compare_staged(self.executor.driver(), &staged);
                    if !mismatches.is_empty() {
                        if !was_diverged {
                            self.pending_divergences.push(DivergenceReport {
                                tick,
                                mismatches: mismatches.clone(),
                            });
                        }
                        self.sync = StandbySync::Diverged { mismatches };
                        self.converged = false;
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
            return self.reinitialize(checkpoint).map(Transfer::Reinitialized);
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
        match self.executor.reinitialize(checkpoint) {
            Ok(report) => {
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
    pub fn note_transfer_failed(&mut self, detail: impl fmt::Display) {
        self.misses += 1;
        if self.failover.is_some_and(|budget| self.misses > budget) {
            self.converged = false;
        }
        self.sync = StandbySync::Degraded {
            detail: detail.to_string(),
        };
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
    /// [`IoError::Fenced`], meaning the claim this peer held was
    /// preempted — completes degraded like any field fault, and queues
    /// one [`FencingLoss`] for the journal: the loss of the field's
    /// single-writer claim is a recorded run event, not only a counter
    /// in `io_health`.
    pub fn scan(&mut self) -> Result<Tick, ScanError> {
        let tick = match self.executor.scan() {
            Ok(tick) => tick,
            Err(error) => {
                if self.owns_field()
                    && !self.fencing_lost
                    && let ScanError::Io(IoError::Fenced(point)) = &error
                {
                    self.fencing_lost = true;
                    self.pending_fencing.push(FencingLoss {
                        tick: self.executor.tick(),
                        point: *point,
                    });
                }
                return Err(error);
            }
        };
        if self.owns_field()
            && !self.fencing_lost
            && let Some(point) = self.executor.fenced_write()
        {
            self.fencing_lost = true;
            self.pending_fencing.push(FencingLoss { tick, point });
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
        Ok(tick)
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

    /// The executor's receipt log.
    pub fn receipts(&self) -> &[CommandReceipt] {
        self.executor.receipts()
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{Component, ComponentIo, ComponentIoExt, IoRequirement, PointMap, StepError};
    use dcs_core::{
        CommandArgument, CommandAvailability, CommandDecl, CommandOutcome, ComponentDescriptor,
        Direction, Divergence, EmittedEvent, EventDecl, EventField, EventFieldKind, EventRetention,
        EventValue, IoDriver, IoError, IoFault, PointId, Sample, StateMap, Value, ValueKind,
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
        source.run(7).unwrap();
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
        peer.scan().unwrap();
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
        source.run(3).unwrap();
        let checkpoint = source.checkpoint();
        peer.apply(&checkpoint).unwrap();

        peer.promote().unwrap();
        assert_eq!(peer.promote(), Err(SwitchError::AlreadyActive));

        // A checkpoint offered to a field-owning peer is refused.
        assert_eq!(peer.apply(&checkpoint), Err(ApplyError::OwnsField));

        peer.scan().unwrap();
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
        source.run(tick).unwrap();
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
            Transfer::Reinitialized(report) => report.clone(),
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
        peer.scan().unwrap();
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
        peer.scan().unwrap();
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

        peer.scan().unwrap();
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
    /// claim — fails the owning peer's scan as before and queues one
    /// `FencingLoss` for the journal: the claim loss is a recorded run
    /// event, not only the caller's exit cause.
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
    fn a_preempted_claim_journals_one_loss_per_ownership() {
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
        peer.scan().unwrap();
        assert!(peer.take_fencing_losses().is_empty());

        // Another attachment took the claim: the next write is fenced.
        // The scan completes degraded — the boundary counted the fenced
        // fault into `io_health` — and the claim loss queues once.
        fenced.armed.store(true, Ordering::Relaxed);
        peer.scan().unwrap();
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

        // The loss is one event per held claim — repeated fenced scans
        // do not queue again.
        peer.scan().unwrap();
        assert!(peer.take_fencing_losses().is_empty());
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
        active.scan().unwrap();
        standby.apply(&active.checkpoint()).unwrap();
        standby.scan().unwrap();
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
        active.scan().unwrap();
        standby.apply(&active.checkpoint()).unwrap();
        standby.scan().unwrap();
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
        cycle(&mut active, &mut standby);
        assert_eq!(
            standby.sync_state(),
            &StandbySync::Tracking { aligned: Tick(8) }
        );
        standby.promote().unwrap();
        assert!(gate.is_open());
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
        source.run(3).unwrap();
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
        source.run(1).unwrap();
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
        peer.scan().unwrap();
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
        source.run(3).unwrap();
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
        source.run(3).unwrap();
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
        source.run(4).unwrap();
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
        source.run(3).unwrap();
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
        source.run(2).unwrap();
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
        standby.scan().unwrap();
        assert_eq!(standby.role(), Role::Active);
        assert_eq!(
            standby.receipts()[0].outcome,
            CommandOutcome::Applied { tick: Tick(3) }
        );
        assert_eq!(Clocked::count(&standby.checkpoint()), Value::Int(7));

        // Never again: the settled outcome stays the log's one entry.
        standby.scan().unwrap();
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
            standby.scan().unwrap();
            reference.scan().unwrap();
            assert_eq!(
                standby.executor().emitted_events(),
                reference.emitted_events()
            );
        }

        standby.promote().unwrap();
        for tick in 4..7 {
            standby.scan().unwrap();
            reference.scan().unwrap();
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
    fn final_sync_is_a_no_op_for_a_failed_or_stale_pull() {
        let field = StubDriver::field(&[]);
        let gate = WriteGate::closed(&field);
        let mut standby = Peer::standby(Clocked::executor(&gate), Some(&gate));
        let source_driver = StubDriver::field(&[]);
        let mut source = Clocked::executor(&source_driver);
        source.run(2).unwrap();
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

        // And a checkpoint older than the run's position is skipped:
        // the standby has scanned past tick 2, so re-applying it would
        // replay that scan's commands and events.
        standby.scan().unwrap();
        source.submit_command(Clocked::bump(7));
        standby.final_sync(|| Ok(source.checkpoint()));
        assert!(
            standby.receipts().is_empty(),
            "the stale checkpoint's pending invoke is not adopted"
        );
        assert_eq!(Clocked::count(&standby.checkpoint()), Value::Int(0));

        // The standing proof still promotes.
        standby.promote().unwrap();
        assert_eq!(standby.role(), Role::Promoting);
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
        source.run(5).unwrap();
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
        standby.scan().unwrap();
        assert_eq!(standby.role(), Role::Active);
        assert_eq!(beat_n(standby.executor()), 6);
        assert_eq!(
            standby.receipts()[0].outcome,
            CommandOutcome::Applied { tick: Tick(6) }
        );
    }
}
