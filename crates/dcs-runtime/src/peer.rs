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
//! Promotion is accepted only from a converged peer —
//! [`StandbyState::Tracking`], i.e. a checkpoint has applied cleanly —
//! and refused otherwise with a named
//! [`SwitchError`](dcs_core::SwitchError): `NotConverged` before
//! convergence, `AlreadyActive` on an instance already owning the field.
//! Demotion is accepted only from a field-owning instance. A
//! field-owning peer refuses checkpoint application outright; a
//! non-owning one — standby or mid-demotion — applies each checkpoint at
//! its scan boundary and tracks convergence like
//! [`Standby`](crate::Standby) does.

use crate::checkpoint::{Checkpoint, RestoreError};
use crate::executor::{Executor, ScanError};
use crate::gate::WriteGate;
use crate::standby::StandbyState;
use dcs_core::{
    Command, CommandReceipt, Role, RoleReport, StandbySync, SwitchError, TelemetrySnapshot, Tick,
};
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
    /// Standby sync bookkeeping, meaningful while the instance does not
    /// own the field; reset to `Unsynchronized` on demotion.
    sync: StandbyState,
    /// The last applied checkpoint's tick, while `sync` is `Tracking`.
    aligned: Option<Tick>,
    /// Reported-role transitions not yet consumed for journaling.
    pending_changes: Vec<RoleChange>,
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

/// Why [`Peer::apply`] did not apply a checkpoint.
#[derive(Debug, Clone, PartialEq)]
pub enum ApplyError {
    /// The instance owns field writes — `active` or `promoting` — and
    /// checkpoints apply only to a tracking peer.
    OwnsField,
    /// The checkpoint was rejected as incompatible: the run rolled back
    /// and the peer reports [`StandbyState::Degraded`].
    Restore(RestoreError),
}

impl fmt::Display for ApplyError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::OwnsField => {
                f.write_str("instance owns field writes; checkpoints apply to a tracking peer")
            }
            Self::Restore(error) => write!(f, "checkpoint apply failed: {error}"),
        }
    }
}

impl std::error::Error for ApplyError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::OwnsField => None,
            Self::Restore(error) => Some(error),
        }
    }
}

impl<'d> Peer<'d> {
    /// An instance owning field writes: role `active`, its gate lifted.
    ///
    /// `gate` is the [`WriteGate`] the executor's driver is gated behind
    /// for a shared-field pair, or `None` when the field is private to
    /// this process. Passing the gate — opened here — is what lets a
    /// later [`demote`](Self::demote) re-quiesce the instance.
    pub fn active(executor: Executor<'d>, gate: Option<&'d WriteGate<'d>>) -> Self {
        if let Some(gate) = gate {
            gate.open();
        }
        Self {
            executor,
            gate,
            role: Role::Active,
            sync: StandbyState::Unsynchronized,
            aligned: None,
            pending_changes: Vec::new(),
        }
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
            sync: StandbyState::Unsynchronized,
            aligned: None,
            pending_changes: Vec::new(),
        }
    }

    /// The currently reported role.
    pub fn role(&self) -> Role {
        self.role
    }

    /// The standby's synchronization bookkeeping — meaningful while the
    /// instance does not own the field.
    pub fn sync_state(&self) -> &StandbyState {
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
                _ => Some(self.sync_wire()),
            },
        }
    }

    /// Promotes the instance to field owner: lifts the write gate at the
    /// request's scan boundary and reports `promoting`, settling to
    /// `active` when the first scan under the lifted gate completes.
    ///
    /// Only a converged peer may promote: [`StandbyState::Tracking`]
    /// means a checkpoint has applied cleanly, so the next scan writes
    /// what the run would have — bumpless by determinism. A standby that
    /// has not converged is refused with
    /// [`SwitchError::NotConverged`]; a field-owning instance —
    /// including a still-settling promotion — with
    /// [`SwitchError::AlreadyActive`].
    pub fn promote(&mut self) -> Result<(), SwitchError> {
        match self.role {
            Role::Active | Role::Promoting => return Err(SwitchError::AlreadyActive),
            Role::Standby | Role::Demoting => {}
        }
        if self.sync != StandbyState::Tracking {
            return Err(SwitchError::NotConverged {
                sync: self.sync_wire(),
            });
        }
        if let Some(gate) = self.gate {
            gate.open();
        }
        self.change(self.executor.tick(), Role::Promoting);
        Ok(())
    }

    /// Demotes the instance to a tracking peer: closes the write gate at
    /// the request's scan boundary — the next scan is already quiesced —
    /// and reports `demoting`, settling to `standby` when that scan
    /// completes.
    ///
    /// Only a field-owning instance demotes; anything else is refused
    /// with [`SwitchError::NotActive`]. The demoted peer's sync
    /// bookkeeping resets to [`StandbyState::Unsynchronized`]: it
    /// reconverges through fresh checkpoints from the new active.
    pub fn demote(&mut self) -> Result<(), SwitchError> {
        match self.role {
            Role::Active | Role::Promoting => {}
            Role::Standby | Role::Demoting => return Err(SwitchError::NotActive),
        }
        if let Some(gate) = self.gate {
            gate.close();
        }
        self.sync = StandbyState::Unsynchronized;
        self.aligned = None;
        self.change(self.executor.tick(), Role::Demoting);
        Ok(())
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
    /// [`StandbyState::Degraded`] — recoverable by the next good
    /// transfer.
    pub fn apply(&mut self, checkpoint: &Checkpoint) -> Result<(), ApplyError> {
        if self.owns_field() {
            return Err(ApplyError::OwnsField);
        }
        match self.executor.apply(checkpoint) {
            Ok(()) => {
                self.sync = StandbyState::Tracking;
                self.aligned = Some(checkpoint.tick);
                Ok(())
            }
            Err(error) => {
                self.sync = StandbyState::Degraded {
                    detail: error.to_string(),
                };
                Err(ApplyError::Restore(error))
            }
        }
    }

    /// Marks the tracking peer [`Degraded`](StandbyState::Degraded)
    /// after a transfer failure that produced no checkpoint at all — an
    /// unreachable active or a refused request.
    pub fn note_transfer_failed(&mut self, detail: impl fmt::Display) {
        self.sync = StandbyState::Degraded {
            detail: detail.to_string(),
        };
    }

    /// Runs one scan and settles a pending role transition: the first
    /// completed scan after a promotion or demotion ends the reported
    /// transition (`promoting` → `active`, `demoting` → `standby`).
    /// Because the gate moved at the request's boundary, this scan
    /// already runs under the new field-write mode.
    pub fn scan(&mut self) -> Result<Tick, ScanError> {
        let tick = self.executor.scan()?;
        match self.role {
            Role::Promoting => self.change(tick, Role::Active),
            Role::Demoting => self.change(tick, Role::Standby),
            _ => {}
        }
        Ok(tick)
    }

    /// Drains reported-role transitions queued since the last call — for
    /// the transition journal the monitoring layer records them into.
    pub fn take_role_changes(&mut self) -> Vec<RoleChange> {
        std::mem::take(&mut self.pending_changes)
    }

    /// Queues `command` for application at the next scan boundary —
    /// forwarded to the executor; [`accepts_commands`](Self::accepts_commands)
    /// is the role check callers apply first.
    pub fn submit_command(&mut self, command: Command) -> CommandReceipt {
        self.executor.submit_command(command)
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

    /// The reported-role change bookkeeping: `from` is the previously
    /// reported role, recorded at `tick`, and the journal entry follows.
    fn change(&mut self, tick: Tick, to: Role) {
        let from = std::mem::replace(&mut self.role, to);
        self.pending_changes.push(RoleChange { tick, from, to });
    }

    /// The wire form of the standby sync bookkeeping.
    fn sync_wire(&self) -> StandbySync {
        match &self.sync {
            StandbyState::Unsynchronized => StandbySync::Unsynchronized,
            StandbyState::Tracking => StandbySync::Tracking {
                aligned: self.aligned.unwrap_or(Tick::ZERO),
            },
            StandbyState::Degraded { detail } => StandbySync::Degraded {
                detail: detail.clone(),
            },
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use dcs_core::{IoDriver, IoError, PointId, Sample, Value};
    use std::collections::HashMap;
    use std::sync::Mutex;

    /// Minimal in-memory driver for role-machine tests: points, reads,
    /// writes — the gate semantics under test live in `WriteGate`.
    struct StubDriver {
        points: Mutex<HashMap<PointId, Sample>>,
    }

    impl StubDriver {
        fn new(point: PointId, value: Value) -> Self {
            Self {
                points: Mutex::new(HashMap::from([(point, Sample::good(value, Tick::ZERO))])),
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
        assert_eq!(peer.sync_state(), &StandbyState::Tracking);
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

    #[test]
    fn demotion_requiesces_the_old_active() {
        let point = PointId(1);
        let driver = StubDriver::new(point, Value::Float(0.0));
        let gate = WriteGate::closed(&driver);
        let mut peer = Peer::active(executor(&gate), Some(&gate));
        peer.scan().unwrap();
        assert!(gate.is_open());

        peer.demote().unwrap();
        assert_eq!(peer.role(), Role::Demoting);
        assert!(!gate.is_open());
        assert!(!peer.owns_field());

        // The gate is already closed: writes stop before the settle.
        gate.write(point, Value::Float(9.0)).unwrap();
        assert_eq!(driver.value(point), Value::Float(0.0));

        peer.scan().unwrap();
        assert_eq!(peer.role(), Role::Standby);
        assert_eq!(peer.sync_state(), &StandbyState::Unsynchronized);

        // Demoting a non-owner is a named error.
        assert_eq!(peer.demote(), Err(SwitchError::NotActive));
    }
}
