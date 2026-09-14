//! Deterministic checkpoint/restore: the redundant hot-swap groundwork.
//!
//! [`Executor::checkpoint`](crate::Executor::checkpoint) captures a run's
//! transferable state as a serde-serializable [`Checkpoint`];
//! [`Executor::restore`](crate::Executor::restore) rebuilds an equivalent
//! executor — fresh components wired against the same point map — with
//! that state applied. Determinism is what makes the transfer meaningful:
//! same model, same state, same inputs produce the same outputs, so a
//! restored run's subsequent scans are identical to the uninterrupted
//! run's.
//!
//! This maps to real redundancy as follows. Active and standby peers run
//! the same plant model; the active periodically ships a [`Checkpoint`]
//! to the standby, which restores it into its own executor so that on
//! switchover its next scan produces what the active would have produced.
//! The `components`, `outputs`, and `internal` sections transfer verbatim —
//! they are controller-side state: `outputs` carries the image's `Out`
//! samples and `internal` the image-carried `In` samples. The `driver`
//! section is simulation-specific:
//! on live hardware the standby's driver observes the actual process
//! through its own channels rather than reconstructing captured field
//! state, so a driver that does not implement the contract simply leaves
//! `driver` empty and the restore skips it.

use crate::executor::WiringError;
use dcs_core::{PointId, Sample, StateError, StateMap, Tick};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fmt;

/// A serializable snapshot of a run's transferable state.
///
/// Produced by [`Executor::checkpoint`](crate::Executor::checkpoint) at
/// the current tick and consumed by
/// [`Executor::restore`](crate::Executor::restore) on a standby — or by a
/// test reconstructing a run. The whole value round-trips through serde
/// like the rest of the contract types.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Checkpoint {
    /// The executor's tick at capture; the restored executor resumes
    /// numbering from here.
    pub tick: Tick,
    /// Each component's captured state, keyed by the component's
    /// [`name`](crate::Component::name) — its identity within the run.
    /// Every registered component has an entry, possibly an empty map, so
    /// the checkpoint describes the component set exactly and a
    /// mismatched restore is caught by name.
    pub components: BTreeMap<String, StateMap>,
    /// The driver's captured state, or `None` when the driver does not
    /// implement the state-capture contract. `None` leaves the standby's
    /// driver to its own channels — the live-hardware case, where the
    /// field's real values are the state.
    pub driver: Option<StateMap>,
    /// The scan image's `Out` samples at capture: the last written output
    /// values, including image-carried internal `Out` points. Restoring
    /// them means an output a component does not rewrite on the next scan
    /// keeps its last value, exactly as the uninterrupted run would.
    pub outputs: BTreeMap<PointId, Sample>,
    /// The scan image's internal `In` samples at capture: held operator
    /// values and link carriers, which field reads never refresh.
    /// Restoring them means a commanded setpoint survives a switchover
    /// instead of reverting to its declared initial. Absent from
    /// checkpoints written before internal points existed; defaults to
    /// empty.
    #[serde(default)]
    pub internal: BTreeMap<PointId, Sample>,
}

/// Why [`Executor::restore`](crate::Executor::restore) failed.
///
/// A restore either rebuilds a run equivalent to the captured one or
/// fails outright — it never produces a partially restored executor.
/// (The supplied driver may reject its state section after inspection;
/// drivers are expected to validate before applying so a failed restore
/// leaves them untouched.)
#[derive(Debug, Clone, PartialEq)]
pub enum RestoreError {
    /// The fresh components did not wire against the point map.
    Wiring(WiringError),
    /// The checkpoint names a component the executor does not register.
    UnknownComponent {
        /// The checkpoint entry with no registered component.
        component: String,
    },
    /// A registered component has no entry in the checkpoint.
    MissingComponent {
        /// The registered component the checkpoint does not describe.
        component: String,
    },
    /// A component rejected its captured state.
    Component(StateError),
    /// The driver rejected its captured state.
    Driver(StateError),
}

impl fmt::Display for RestoreError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Wiring(error) => write!(f, "wiring the fresh components failed: {error}"),
            Self::UnknownComponent { component } => write!(
                f,
                "checkpoint captures component {component:?} the executor does not register"
            ),
            Self::MissingComponent { component } => {
                write!(f, "component {component:?} has no state in the checkpoint")
            }
            Self::Component(error) => write!(f, "component state restore failed: {error}"),
            Self::Driver(error) => write!(f, "driver state restore failed: {error}"),
        }
    }
}

impl std::error::Error for RestoreError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Wiring(error) => Some(error),
            Self::Component(error) | Self::Driver(error) => Some(error),
            _ => None,
        }
    }
}

impl From<WiringError> for RestoreError {
    fn from(error: WiringError) -> Self {
        Self::Wiring(error)
    }
}
