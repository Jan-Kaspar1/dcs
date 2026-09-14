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
//! [`StandbySync::Tracking`], i.e. a checkpoint has applied cleanly —
//! and refused otherwise with a named
//! [`SwitchError`](dcs_core::SwitchError): `NotConverged` before
//! convergence, `AlreadyActive` on an instance already owning the field.
//! Demotion is accepted only from a field-owning instance. A
//! field-owning peer refuses checkpoint application outright; a
//! non-owning one — standby or mid-demotion — applies each checkpoint at
//! its scan boundary and reports convergence directly as the serde
//! [`StandbySync`] wire state.
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
use dcs_core::{
    Command, CommandReceipt, PointId, Role, RoleReport, Sample, StandbySync, SwitchError,
    TelemetrySnapshot, Tick,
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
    /// and the peer reports [`StandbySync::Degraded`].
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
            sync: StandbySync::Unsynchronized,
            aligned: None,
            pending_changes: Vec::new(),
            staged: None,
            pending_divergences: Vec::new(),
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
            sync: StandbySync::Unsynchronized,
            aligned: None,
            pending_changes: Vec::new(),
            staged: None,
            pending_divergences: Vec::new(),
        }
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
    /// determinism. A standby that has not converged — or that reports
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
        if !matches!(self.sync, StandbySync::Tracking { .. }) {
            return Err(SwitchError::NotConverged {
                sync: self.sync.clone(),
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
        self.sync = StandbySync::Unsynchronized;
        self.aligned = None;
        self.staged = None;
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
        match self.executor.apply(checkpoint) {
            Ok(()) => {
                let was_diverged = matches!(self.sync, StandbySync::Diverged { .. });
                self.sync = StandbySync::Tracking {
                    aligned: checkpoint.tick,
                };
                self.aligned = Some(checkpoint.tick);
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
                    }
                }
                Ok(())
            }
            Err(error) => {
                self.sync = StandbySync::Degraded {
                    detail: error.to_string(),
                };
                Err(ApplyError::Restore(error))
            }
        }
    }

    /// Marks the tracking peer [`Degraded`](StandbySync::Degraded)
    /// after a transfer failure that produced no checkpoint at all — an
    /// unreachable active or a refused request.
    pub fn note_transfer_failed(&mut self, detail: impl fmt::Display) {
        self.sync = StandbySync::Degraded {
            detail: detail.to_string(),
        };
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
    pub fn scan(&mut self) -> Result<Tick, ScanError> {
        let tick = self.executor.scan()?;
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
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{Component, ComponentIo, ComponentIoExt, IoRequirement, PointMap, StepError};
    use dcs_core::{Direction, Divergence, IoDriver, IoError, PointId, Sample, Value, ValueKind};
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
        assert_eq!(peer.sync_state(), &StandbySync::Unsynchronized);

        // Demoting a non-owner is a named error.
        assert_eq!(peer.demote(), Err(SwitchError::NotActive));
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
}
