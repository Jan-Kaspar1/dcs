//! The model-revision carryover contract: what crosses the model boundary
//! when a revised plant model rolls into production through the redundant
//! pair.
//!
//! Per the rolling model-revision decision, a revised model fingerprints
//! differently by design, so its standby cannot converge by ordinary
//! checkpoint restore — the fingerprint gate refuses it. Instead the
//! revision-armed standby consumes the active's checkpoint under the
//! documented carryover rule and reports
//! [`StandbySync::Reinitialized`](crate::StandbySync), whose
//! [`CarryoverReport`] is the audit record of the crossing: the model
//! fingerprints on each side, the tick the run resumes from, and every
//! element classified — what carried its last value, what the revision
//! initializes fresh, and what had no continuation and is named rather
//! than silently dropped.
//!
//! The report is serde-serializable like the rest of the contract: the
//! monitoring surface serves it inside [`RoleReport`](crate::RoleReport)
//! and journals the transition, and a deterministic run reports identical
//! carryover on every execution.

use crate::fingerprint::ModelFingerprint;
use crate::signal::{PointId, Tick, Value};
use crate::telemetry::ForcedPoint;
use serde::{Deserialize, Serialize};
use std::fmt;

/// One point whose last value crossed the model boundary — a held
/// operator value or an output image sample the revision continues from.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct CarriedPoint {
    /// The point's declared identity — its `io_point` id, unchanged
    /// across the revision.
    pub point: PointId,
    /// The value the old model's run last held, now the revision's.
    pub value: Value,
}

/// One checkpoint element with no continuation under the revised model —
/// named in the [`CarryoverReport`] rather than silently dropped.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DroppedElement {
    /// A checkpointed internal `In` sample whose point the revision does
    /// not serve as a writable internal `In` point — the point was
    /// removed, field-bound, re-aimed as an output, or made read-only.
    InternalPoint {
        /// The dropped point.
        point: PointId,
    },
    /// A checkpointed `Out` sample whose point the revision does not
    /// serve as an `Out` point — the point was removed or its direction
    /// changed.
    OutputPoint {
        /// The dropped point.
        point: PointId,
    },
    /// A checkpointed component state entry naming a component the
    /// revision does not register — its state has nothing to carry into.
    Component {
        /// The dropped component's name — its identity within the run.
        name: String,
    },
    /// The checkpoint's captured driver state — never carried: the
    /// revision's driver observes the real field, and its own driver may
    /// be a different surface entirely.
    DriverState,
}

impl fmt::Display for DroppedElement {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InternalPoint { point } => {
                write!(f, "internal in point {}", point.0)
            }
            Self::OutputPoint { point } => write!(f, "out point {}", point.0),
            Self::Component { name } => write!(f, "component {name:?}"),
            Self::DriverState => f.write_str("driver state"),
        }
    }
}

/// One descriptor-declared parameter whose checkpointed value the
/// crossing reverted to the revision's declared default — the itemized
/// record of tuning a reinitialized component lost.
///
/// Component state reinitializes across the boundary by rule, so a
/// receipted `set_parameter` tune does not carry; the report names what
/// reverted instead, so the witnessed record of the in-service change
/// says exactly which declared parameters lost their tuned values —
/// the value the old run's checkpoint held beside the revision's
/// declared default now standing.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RevertedParameter {
    /// The component owning the parameter — the same name the report's
    /// `reinitialized` list carries.
    pub component: String,
    /// The parameter's declared name — the descriptor's key, matching
    /// the plant model's parameter map and the `set_parameter` target.
    pub parameter: String,
    /// The value the old run's checkpoint held — the tuning that was
    /// lost.
    pub checkpointed: Value,
    /// The revision's declared default the parameter reverted to — the
    /// value the reinitialized component reports.
    pub declared: Value,
}

/// The audit record of one model-boundary crossing: what a revised-model
/// standby took from the old model's checkpoint, what it initialized
/// fresh, and what it named as dropped.
///
/// Lists are deterministic — points in ascending id order, components in
/// scan order — so two runs of the same revision report identically.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CarryoverReport {
    /// The model fingerprint the checkpoint was captured under — the old
    /// model — or `None` when the capturing run was assembled without one.
    pub from: Option<ModelFingerprint>,
    /// The model fingerprint this run was assembled under — the revised
    /// model.
    pub to: Option<ModelFingerprint>,
    /// The tick the run resumes from — the checkpointed tick, so tick
    /// numbering continues across the revision.
    pub resumed_at: Tick,
    /// The held operator values that carried: checkpointed internal `In`
    /// samples whose declared identity the revision still serves as a
    /// writable internal `In` point of the same value kind — a commanded
    /// setpoint keeps its last value across the boundary.
    pub carried: Vec<CarriedPoint>,
    /// The checkpointed `Out` samples that carried into the revision's
    /// image: the last written field values the first post-promotion scan
    /// continues from, so a component that does not immediately rewrite
    /// an output holds the old run's last value rather than a hole.
    pub carried_outputs: Vec<CarriedPoint>,
    /// The operator forces that carried: every checkpointed force whose
    /// point the revision still serves as a writable `In` point of the
    /// same kind. The force set is all-or-nothing — a force the revision
    /// cannot serve fails the crossing rather than silently releasing.
    pub carried_forces: Vec<ForcedPoint>,
    /// The checkpoint elements with no continuation under the revision,
    /// each named by identity — see [`DroppedElement`].
    pub dropped: Vec<DroppedElement>,
    /// Every component of the revision, in scan order: component state
    /// reinitializes across the boundary by rule — there is no implicit
    /// field-by-field state mapping, so a same-named component under a
    /// revised model starts fresh. Selected per-kind state compat rules
    /// are a future extension; none exist yet.
    pub reinitialized: Vec<String>,
    /// The descriptor-declared parameters whose checkpointed values the
    /// crossing reverted — the itemized half of the reinitialize rule:
    /// per component, every declared parameter whose checkpointed value
    /// differed from the revision's declared default, in component scan
    /// order then parameter declaration order — so the record says
    /// which tuning was lost, not just which components restarted. See
    /// [`RevertedParameter`].
    ///
    /// Serde-optional like the contract's other additive sections: a
    /// report a pre-itemization build journaled carries no field and
    /// deserializes with an empty list.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub reverted_tuning: Vec<RevertedParameter>,
    /// The revision's writable internal `In` points no checkpoint value
    /// carried into — each stands at its declared `initial` value.
    pub initialized: Vec<PointId>,
}

impl fmt::Display for CarryoverReport {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "reinitialized under model {} (from {}) at tick {}: {} carried, {} initialized, {} dropped, {} reverted",
            self.to
                .map(|fingerprint| fingerprint.to_string())
                .unwrap_or_else(|| "<none>".to_string()),
            self.from
                .map(|fingerprint| fingerprint.to_string())
                .unwrap_or_else(|| "<none>".to_string()),
            self.resumed_at.0,
            self.carried.len(),
            self.initialized.len(),
            self.dropped.len(),
            self.reverted_tuning.len(),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn report() -> CarryoverReport {
        CarryoverReport {
            from: Some(ModelFingerprint(7)),
            to: Some(ModelFingerprint(9)),
            resumed_at: Tick(41),
            carried: vec![CarriedPoint {
                point: PointId(10),
                value: Value::Float(3.5),
            }],
            carried_outputs: vec![],
            carried_forces: vec![ForcedPoint {
                point: PointId(10),
                value: Value::Float(1.0),
            }],
            dropped: vec![
                DroppedElement::InternalPoint { point: PointId(11) },
                DroppedElement::Component {
                    name: "gone".to_string(),
                },
                DroppedElement::DriverState,
            ],
            reinitialized: vec!["loop".to_string()],
            reverted_tuning: vec![
                RevertedParameter {
                    component: "loop".to_string(),
                    parameter: "gain".to_string(),
                    checkpointed: Value::Float(3.0),
                    declared: Value::Float(2.0),
                },
                RevertedParameter {
                    component: "loop".to_string(),
                    parameter: "bias".to_string(),
                    checkpointed: Value::Int(4),
                    declared: Value::Float(0.0),
                },
            ],
            initialized: vec![PointId(30)],
        }
    }

    #[test]
    fn carryover_report_serde_roundtrip() {
        let report = report();
        let json = serde_json::to_string(&report).unwrap();
        assert_eq!(
            serde_json::from_str::<CarryoverReport>(&json).unwrap(),
            report
        );
    }

    #[test]
    fn empty_reverted_tuning_omits_the_key() {
        // The serde-optional convention: a report with nothing reverted
        // serializes without the section, keeping the wire shape a
        // pre-itemization consumer expects.
        let mut report = report();
        report.reverted_tuning = Vec::new();
        let json = serde_json::to_string(&report).unwrap();
        assert!(!json.contains("reverted_tuning"), "{json}");
        assert_eq!(
            serde_json::from_str::<CarryoverReport>(&json).unwrap(),
            report
        );
    }

    #[test]
    fn reports_predating_reverted_tuning_still_parse() {
        // The durable journal replays entries across restarts — a
        // report an older build journaled carries no `reverted_tuning`
        // and must deserialize with an empty list rather than fail the
        // replay.
        let mut document = serde_json::to_value(report()).unwrap();
        document.as_object_mut().unwrap().remove("reverted_tuning");
        let parsed: CarryoverReport = serde_json::from_value(document).unwrap();
        assert_eq!(parsed.reverted_tuning, Vec::new());
    }

    #[test]
    fn reverted_parameter_serde_roundtrip() {
        let parameter = RevertedParameter {
            component: "loop".to_string(),
            parameter: "gain".to_string(),
            checkpointed: Value::Float(3.0),
            declared: Value::Float(2.0),
        };
        let json = serde_json::to_string(&parameter).unwrap();
        assert_eq!(
            serde_json::from_str::<RevertedParameter>(&json).unwrap(),
            parameter
        );
    }
}
