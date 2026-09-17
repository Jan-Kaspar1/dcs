//! The rolling model-revision carryover rule: how a standby assembled
//! under a revised plant model consumes the old model's checkpoint.
//!
//! Per the rolling model-revision decision, a revised model's fingerprint
//! differs by design, so the strict restore negotiation — equal
//! fingerprints or refuse — cannot converge it. The revision-armed peer
//! instead runs [`Executor::reinitialize`](crate::Executor::reinitialize)
//! against each pulled checkpoint, applying this documented rule:
//!
//! - **Operator-writable internal `In` points** matched by declared
//!   identity — the `io_point` id — carry their last sample, value kind
//!   permitting: a commanded setpoint holds across the boundary. One the
//!   revision does not still serve as a writable internal `In` point is
//!   named dropped; one whose declared kind changed is a
//!   [`CarryoverError`] — a retype must rename the point, not silently
//!   reinterpret a live value.
//! - **`Out` image samples** matched by declared identity carry their
//!   last values into the revision's image: the first post-promotion scan
//!   continues from the field's last written state, so an output a
//!   component does not immediately rewrite holds its value rather than
//!   a hole. An unserved or kind-changed `Out` point is named dropped —
//!   a dropped output informs but never blocks.
//! - **Operator forces** carry only as a set: a forced point the revision
//!   still serves as a writable `In` of the same kind keeps its force;
//!   any other forced point fails the crossing with
//!   [`CarryoverError::ForceNotServed`] — an active force must never be
//!   released silently.
//! - **Component state reinitializes** — every component of the revision
//!   starts from construction, named in the report's `reinitialized`
//!   list. There is no implicit field-by-field state mapping across a
//!   model boundary; a checkpointed component the revision does not
//!   register is named dropped. Selected per-kind compatibility rules
//!   are a future extension of this contract; none exist yet.
//!   Descriptor-declared parameters reinitialize with the rest of the
//!   component state — the recorded tuning-carry decision: the
//!   checkpoint's component section cannot distinguish a tuned value
//!   from a constructed default, so carrying by (component name,
//!   parameter name, value kind) would silently defeat the revising
//!   engineer's deliberate re-default. The report's `reverted_tuning`
//!   itemizes what the rule reverted instead — per component, each
//!   declared parameter whose checkpointed value differed from the
//!   revision's declared default, with both values — so the witnessed
//!   record names the tuning that was lost, and deliberate re-tuning
//!   re-enters through the receipted `set_parameter` path. A
//!   checkpointed field whose kind the revision retyped itemizes like
//!   any differing value: nothing is reinterpreted, so the
//!   named-refusal convention that binds the carried sections does not
//!   apply.
//! - **Driver state never carries** — the revision's driver observes the
//!   real field through its own channels; the checkpoint's captured
//!   section is named dropped.
//! - **The tick resumes** at the checkpoint's tick, so the promoted
//!   revision continues the run's numbering.
//!
//! The rule classifies first and applies second: every named failure is
//! detected before any state moves, so a revision that breaks the rule
//! fails before promotion — the peer reports
//! [`StandbySync::Degraded`](dcs_core::StandbySync) carrying the named
//! error, the old active keeps the field, and nothing half-applies.

use dcs_core::{PointId, Value, ValueKind};
use std::fmt;

/// Why [`Executor::reinitialize`](crate::Executor::reinitialize) refused
/// a checkpoint — a break of the module's documented carryover rule.
///
/// Every variant names the offending element and the reason, so a failed
/// revision reports exactly what could not cross rather than a bare
/// refusal. A refusal applies nothing: the peer keeps its last-defined
/// state and reports it, and promotion stays refused.
#[derive(Debug, Clone, PartialEq)]
pub enum CarryoverError {
    /// The checkpoint's format version is not one this build accepts —
    /// checked before anything else, exactly like the restore
    /// negotiation.
    UnsupportedVersion {
        /// The version the checkpoint declares.
        found: u32,
        /// Every format version this build accepts.
        supported: &'static [u32],
    },
    /// A checkpointed internal `In` sample's value kind differs from the
    /// kind the revision declares under the same point identity. A live
    /// operator value is never reinterpreted — a retype must rename the
    /// point.
    InternalKindMismatch {
        /// The mismatched point.
        point: PointId,
        /// The kind the revision declares.
        expected: ValueKind,
        /// The kind the checkpoint carries.
        found: Value,
    },
    /// A checkpointed `Out` sample's value kind differs from the kind
    /// the revision declares under the same point identity.
    OutputKindMismatch {
        /// The mismatched point.
        point: PointId,
        /// The kind the revision declares.
        expected: ValueKind,
        /// The kind the checkpoint carries.
        found: Value,
    },
    /// A checkpointed force names a point the revision does not serve as
    /// a writable `In` point — the point was removed, became an `Out`
    /// point, or lost `writable`. An active force is never released
    /// silently, so the crossing fails here.
    ForceNotServed {
        /// The forced point the revision cannot serve.
        point: PointId,
    },
    /// A checkpointed force's value kind differs from the kind the
    /// revision declares under the same point identity.
    ForceKindMismatch {
        /// The mismatched point.
        point: PointId,
        /// The kind the revision declares.
        expected: ValueKind,
        /// The kind the checkpoint carries.
        found: Value,
    },
}

impl fmt::Display for CarryoverError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::UnsupportedVersion { found, supported } => write!(
                f,
                "unsupported checkpoint format version {found} (this build accepts {supported:?})"
            ),
            Self::InternalKindMismatch {
                point,
                expected,
                found,
            } => write!(
                f,
                "checkpoint internal point {} carries {found:?} but the revision declares {expected:?} — a retype must rename the point",
                point.0
            ),
            Self::OutputKindMismatch {
                point,
                expected,
                found,
            } => write!(
                f,
                "checkpoint output point {} carries {found:?} but the revision declares {expected:?}",
                point.0
            ),
            Self::ForceNotServed { point } => write!(
                f,
                "checkpoint carries a force on point {} the revision does not serve as a writable in point — an active force is never released silently",
                point.0
            ),
            Self::ForceKindMismatch {
                point,
                expected,
                found,
            } => write!(
                f,
                "checkpoint forced point {} carries {found:?} but the revision declares {expected:?}",
                point.0
            ),
        }
    }
}

impl std::error::Error for CarryoverError {}
