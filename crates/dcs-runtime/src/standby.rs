//! The tracking standby: a second controller instance kept converged to
//! the active's run by applied checkpoints.
//!
//! Per the redundancy decisions, an active/standby pair runs the same
//! plant model. The standby assembles an equivalent executor and driver
//! set, then applies the active's [`Checkpoint`]s as they arrive —
//! fetched over the monitoring transport or delivered by other means —
//! through [`Standby::apply`]. Applying aligns the run: the executor's
//! tick resumes from the checkpointed tick and its component, driver,
//! and output-image state becomes the captured state, so subsequent
//! scans continue the run the active produced — deterministically, per
//! the execution-model decision.
//!
//! The standby reports its synchronization as a named
//! [`StandbyState`]: `Unsynchronized` before the first transfer lands,
//! `Tracking` after a clean apply, and `Degraded` after a failed one —
//! an unreachable active or a rejected checkpoint. Degraded is
//! recoverable by construction: the executor keeps scanning its
//! last-known state and the next successful apply reconverges it.
//!
//! Bumpless switchover path, per the switchover-semantics decision:
//! while standby, field-facing writes are quiesced behind a
//! [`WriteGate`](crate::WriteGate) at the driver boundary — the executor
//! still computes outputs and reads the field, but only the active's
//! writes reach it. Promotion lifts the gate at a scan boundary and the
//! converged standby's next scan writes what the active would have —
//! the promotion action itself is the follow-up switchover ticket.

use crate::checkpoint::{Checkpoint, RestoreError};
use crate::executor::{Executor, ScanError};
use dcs_core::{TelemetrySnapshot, Tick};
use std::fmt;

/// How the last checkpoint transfer went — the standby's named,
/// monitorable synchronization state.
#[derive(Debug, Clone, PartialEq)]
pub enum StandbyState {
    /// Assembled and scanning, but no checkpoint has landed yet: the
    /// run reflects only this instance's initial state.
    Unsynchronized,
    /// The last transfer applied cleanly: the run is aligned at the
    /// checkpointed tick and tracking the active.
    Tracking,
    /// The last transfer failed — the fetch produced no checkpoint, or
    /// the checkpoint was rejected as incompatible — so the run
    /// continues on its last-known state. Recoverable: the next
    /// successful [`Standby::apply`] realigns the run.
    Degraded {
        /// What the failed transfer reported, for diagnostics.
        detail: String,
    },
}

impl fmt::Display for StandbyState {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Unsynchronized => f.write_str("unsynchronized"),
            Self::Tracking => f.write_str("tracking"),
            Self::Degraded { detail } => write!(f, "degraded: {detail}"),
        }
    }
}

/// A standby controller instance: an executor kept converged to an
/// active peer's run by applied checkpoints.
///
/// Construct it from the same plant model the active runs — the
/// equivalent executor and driver set the assembly produces — then feed
/// it checkpoints with [`apply`](Self::apply) and scan it with
/// [`scan`](Self::scan). Between transfers the executor free-runs on its
/// own driver: a standby sharing the field through a field-observing
/// driver tracks the real process continuously, while one behind a
/// local simulated driver tracks its restored private copy and
/// reconverges on each transfer.
#[derive(Debug)]
pub struct Standby<'d> {
    executor: Executor<'d>,
    state: StandbyState,
    /// The last applied checkpoint's tick — `None` while
    /// `Unsynchronized`.
    aligned: Option<Tick>,
}

impl<'d> Standby<'d> {
    /// A standby over `executor`, [`Unsynchronized`](StandbyState::Unsynchronized)
    /// until the first checkpoint lands.
    pub fn new(executor: Executor<'d>) -> Self {
        Self {
            executor,
            state: StandbyState::Unsynchronized,
            aligned: None,
        }
    }

    /// The current synchronization state.
    pub fn state(&self) -> &StandbyState {
        &self.state
    }

    /// The tick of the last applied checkpoint, when any transfer has
    /// succeeded — how far the run is known to be aligned.
    pub fn aligned_tick(&self) -> Option<Tick> {
        self.aligned
    }

    /// The executor's current tick.
    pub fn tick(&self) -> Tick {
        self.executor.tick()
    }

    /// The wrapped executor, e.g. for per-point samples and diagnostics.
    pub fn executor(&self) -> &Executor<'d> {
        &self.executor
    }

    /// Consumes the standby and returns the executor — the shape the
    /// promotion path continues from.
    pub fn into_executor(self) -> Executor<'d> {
        self.executor
    }

    /// Runs one scan on the tracked state.
    pub fn scan(&mut self) -> Result<Tick, ScanError> {
        self.executor.scan()
    }

    /// A monitoring snapshot of the tracked run.
    pub fn snapshot(&self) -> TelemetrySnapshot {
        self.executor.snapshot()
    }

    /// Applies a checkpoint received from the active, aligning the run
    /// at the checkpointed tick.
    ///
    /// On success the state becomes [`Tracking`](StandbyState::Tracking)
    /// and [`aligned_tick`](Self::aligned_tick) reports the checkpoint's
    /// tick. A checkpoint from a mismatched model or component set is
    /// rejected with the [`RestoreError`] naming the element, the
    /// executor rolls back to its pre-apply state, and the standby turns
    /// [`Degraded`](StandbyState::Degraded) — still running, still
    /// recoverable by the next good transfer.
    pub fn apply(&mut self, checkpoint: &Checkpoint) -> Result<(), RestoreError> {
        match self.executor.apply(checkpoint) {
            Ok(()) => {
                self.state = StandbyState::Tracking;
                self.aligned = Some(checkpoint.tick);
                Ok(())
            }
            Err(error) => {
                self.state = StandbyState::Degraded {
                    detail: error.to_string(),
                };
                Err(error)
            }
        }
    }

    /// Marks the standby [`Degraded`](StandbyState::Degraded) after a
    /// transfer failure that produced no checkpoint at all — an
    /// unreachable active or a refused request. The run continues on
    /// its last-known state; the next successful [`apply`](Self::apply)
    /// reconverges it.
    pub fn note_transfer_failed(&mut self, detail: impl fmt::Display) {
        self.state = StandbyState::Degraded {
            detail: detail.to_string(),
        };
    }
}
