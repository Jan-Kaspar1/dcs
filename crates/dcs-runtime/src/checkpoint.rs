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
use dcs_core::{ModelFingerprint, PointId, Sample, StateError, StateMap, Tick, Value, ValueKind};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fmt;

/// The checkpoint format version this build writes.
///
/// The version negotiates checkpoint compatibility explicitly, the way
/// `MODEL_VERSION` does for the model document: a bump marks a breaking
/// format change, while additive changes — a new optional section or a
/// new optional field inside an element's [`StateMap`] vocabulary — ride
/// the existing version. Restore accepts every version in
/// [`SUPPORTED_FORMAT_VERSIONS`]; anything else is
/// [`RestoreError::UnsupportedVersion`].
pub const CHECKPOINT_FORMAT_VERSION: u32 = 1;

/// The checkpoint format versions a restore accepts.
///
/// Version 0 is the shape checkpoints had before the format was
/// versioned — serde reads an absent `format_version` as 0 — so a
/// checkpoint captured by a pre-versioning build still restores where
/// its fingerprint also matches, i.e. onto an executor assembled without
/// one. A compatible version may differ from
/// [`CHECKPOINT_FORMAT_VERSION`] only in what serde tolerates on this
/// schema: sections or fields it does not name may be absent (they
/// default) or extra (they are ignored), and an element's [`StateMap`]
/// may carry optional fields its `optional_*` accessors read. Everything
/// else remains strict: component-set equality, each element's required
/// fields and known-field check, and the point/kind checks on the
/// `outputs` and `internal` sections all still apply after the version
/// is accepted.
pub const SUPPORTED_FORMAT_VERSIONS: &[u32] = &[0, CHECKPOINT_FORMAT_VERSION];

/// A serializable snapshot of a run's transferable state.
///
/// Produced by [`Executor::checkpoint`](crate::Executor::checkpoint) at
/// the current tick and consumed by
/// [`Executor::restore`](crate::Executor::restore) on a standby — or by a
/// test reconstructing a run. The whole value round-trips through serde
/// like the rest of the contract types.
///
/// Restore enforces a two-part negotiation before any state applies: the
/// checkpoint's [`format_version`](Self::format_version) must be one of
/// [`SUPPORTED_FORMAT_VERSIONS`] and its
/// [`model_fingerprint`](Self::model_fingerprint) must equal the
/// fingerprint the restoring executor was assembled with — mismatches
/// are the named [`RestoreError::UnsupportedVersion`] and
/// [`RestoreError::FingerprintMismatch`]. Only then do the structural
/// checks of decision 11's strictness run.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Checkpoint {
    /// The wire format's version. Checkpoints serialized before the
    /// format was versioned carry no field and deserialize as 0; this
    /// build writes [`CHECKPOINT_FORMAT_VERSION`].
    #[serde(default)]
    pub format_version: u32,
    /// The fingerprint of the model the capturing run was assembled
    /// from, supplied by the assembling layer — the executor is
    /// model-agnostic and never derives it. `None` when the run was
    /// assembled without one (and on every checkpoint a pre-versioning
    /// build wrote); a fingerprinted executor accepts only a checkpoint
    /// carrying the same value.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub model_fingerprint: Option<ModelFingerprint>,
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
    /// The operator force set at capture: each forced point and the
    /// value the scan image substitutes for its input read. Restoring
    /// them means a standby keeps forcing the same points through a
    /// switchover instead of reverting to live reads. Absent from
    /// checkpoints written before forces existed; defaults to empty.
    #[serde(default)]
    pub forces: BTreeMap<PointId, Value>,
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
    /// The checkpoint's format version is not one this build accepts —
    /// checked before anything else, so a checkpoint from a newer or
    /// unknown format never reaches the structural checks.
    UnsupportedVersion {
        /// The version the checkpoint declares — 0 when the field is
        /// absent, as on checkpoints a pre-versioning build wrote.
        found: u32,
        /// Every format version this build accepts.
        supported: &'static [u32],
    },
    /// The checkpoint's model fingerprint differs from the fingerprint
    /// the executor was assembled with — a checkpoint captured under a
    /// different model, including one whose component set happens to
    /// match structurally.
    FingerprintMismatch {
        /// The fingerprint the checkpoint carries — `None` when it was
        /// captured by a run assembled without one.
        found: Option<ModelFingerprint>,
        /// The fingerprint the restoring executor was assembled with.
        expected: Option<ModelFingerprint>,
    },
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
    /// The checkpoint's output image names a point the executor's map
    /// does not serve as `Out` — a checkpoint from a different I/O
    /// mapping.
    UnknownOutput {
        /// The unserved or misdirected point.
        point: PointId,
    },
    /// A checkpointed output's value kind differs from the kind the
    /// point map declares for it — again a different mapping's artifact.
    IncompatibleOutput {
        /// The mismatched point.
        point: PointId,
        /// The kind the point map declares.
        expected: ValueKind,
        /// The kind the checkpoint carries.
        found: Value,
    },
    /// The checkpoint's internal section names a point the executor's
    /// map does not serve as an internal `In` — a checkpoint from a
    /// different I/O mapping.
    UnknownInternal {
        /// The unserved or misdirected point.
        point: PointId,
    },
    /// A checkpointed internal sample's value kind differs from the kind
    /// the point map declares for it — again a different mapping's
    /// artifact.
    IncompatibleInternal {
        /// The mismatched point.
        point: PointId,
        /// The kind the point map declares.
        expected: ValueKind,
        /// The kind the checkpoint carries.
        found: Value,
    },
    /// The checkpoint's force section names a point the executor's map
    /// does not serve as a writable `In` point — a checkpoint from a
    /// different I/O mapping.
    UnknownForce {
        /// The unserved or unforceable point.
        point: PointId,
    },
    /// A checkpointed force's value kind differs from the kind the
    /// point map declares for it — again a different mapping's
    /// artifact.
    IncompatibleForce {
        /// The mismatched point.
        point: PointId,
        /// The kind the point map declares.
        expected: ValueKind,
        /// The kind the checkpoint carries.
        found: Value,
    },
}

impl fmt::Display for RestoreError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::UnsupportedVersion { found, supported } => write!(
                f,
                "unsupported checkpoint format version {found} (this build accepts {supported:?})"
            ),
            Self::FingerprintMismatch { found, expected } => write!(
                f,
                "checkpoint model fingerprint {} does not match this run's {}",
                found
                    .map(|fingerprint| fingerprint.to_string())
                    .unwrap_or_else(|| "<none>".to_string()),
                expected
                    .map(|fingerprint| fingerprint.to_string())
                    .unwrap_or_else(|| "<none>".to_string()),
            ),
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
            Self::UnknownOutput { point } => write!(
                f,
                "checkpoint carries output point {} the point map does not serve as out",
                point.0
            ),
            Self::IncompatibleOutput {
                point,
                expected,
                found,
            } => write!(
                f,
                "checkpoint output point {} expects {expected:?}, found {found:?}",
                point.0
            ),
            Self::UnknownInternal { point } => write!(
                f,
                "checkpoint carries internal point {} the point map does not serve as an internal in point",
                point.0
            ),
            Self::IncompatibleInternal {
                point,
                expected,
                found,
            } => write!(
                f,
                "checkpoint internal point {} expects {expected:?}, found {found:?}",
                point.0
            ),
            Self::UnknownForce { point } => write!(
                f,
                "checkpoint carries forced point {} the point map does not serve as a writable in point",
                point.0
            ),
            Self::IncompatibleForce {
                point,
                expected,
                found,
            } => write!(
                f,
                "checkpoint forced point {} expects {expected:?}, found {found:?}",
                point.0
            ),
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
