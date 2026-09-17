//! The standby divergence check: comparing the field `Out` values a
//! quiesced standby's own scan stages against the values the field
//! actually carries, per the standby-divergence decision.
//!
//! A checkpoint-converged standby computes, every scan, the writes it
//! *would* issue — the staged field `Out` image — while its
//! [`WriteGate`](crate::WriteGate) keeps them from the field. That staged
//! image is evidence: if it disagrees with the field's actual values at
//! the same tick, promoting this peer would write a different field than
//! the active produced — skewed parameters, a plant restart, a state the
//! checkpoint stream did not cover. [`Peer`](crate::Peer) therefore
//! stashes each non-field-owning scan's staged image and, when a later
//! checkpoint transfer lands at that same tick, compares it point by
//! point against the peer's own reads of the same `Out` points —
//! the field-observing path the decision allows. A mismatch moves the
//! peer to the named `Diverged` synchronization state, blocks promotion,
//! and journals the evidence; a fresh transfer whose same-tick
//! comparison *read the field and matched* clears it — and only that:
//! `Diverged` is the promotion-blocking "my staged outputs differ from
//! the field" verdict, so an apply that ran no comparison, or a
//! comparison whose field reads failed, carries no such evidence and
//! leaves the verdict standing.
//!
//! The comparison is deterministic and exact per value kind: `Bool` and
//! `Int` values must be identical; `Float` values agree within
//! [`FLOAT_TOLERANCE`]. A point whose field read fails — a communication
//! fault is neither divergence evidence nor convergence evidence —
//! reports as [`FieldComparison::unread`], and a staged image whose tick
//! no transferred checkpoint has reached yet runs no comparison at all,
//! so the check only ever acts on same-tick, same-field observations.

use dcs_core::{Divergence, IoDriver, PointId, Sample, Tick, Value};
use std::collections::BTreeMap;

/// The `Float` leg of the divergence comparison rule: two `Float`s agree
/// when their absolute difference is within `FLOAT_TOLERANCE` of the
/// larger magnitude, with an absolute floor of the same size. `Bool` and
/// `Int` require exact equality; samples of different kinds always
/// diverge. The tolerance absorbs transport round-trips that re-encode a
/// `Float` (the plant protocol's JSON) without masking real divergence.
pub const FLOAT_TOLERANCE: f64 = 1e-9;

/// Whether `staged` and `field` disagree under the per-kind rule — see
/// the module docs and [`FLOAT_TOLERANCE`].
pub fn values_diverge(staged: Value, field: Value) -> bool {
    match (staged, field) {
        (Value::Float(staged), Value::Float(field)) => {
            if staged.is_nan() || field.is_nan() {
                return staged.is_nan() != field.is_nan();
            }
            let scale = staged.abs().max(field.abs()).max(1.0);
            (staged - field).abs() > FLOAT_TOLERANCE * scale
        }
        _ => staged != field,
    }
}

/// One transfer's divergence verdict: the tick the compared staged image
/// belongs to and the mismatched field `Out` points, in point order.
#[derive(Debug, Clone, PartialEq)]
pub struct DivergenceReport {
    /// The tick both sides describe — the staged scan's tick, equal to
    /// the applied checkpoint's tick by the check's pairing rule.
    pub tick: Tick,
    /// The mismatched field `Out` points, in ascending point order.
    pub mismatches: Vec<Divergence>,
}

/// One transfer's divergence clear: the tick the compared staged image
/// belonged to and the field `Out` points the comparison verified
/// matching — the positive evidence a `Diverged` verdict clears on,
/// journaled as its own named event.
#[derive(Debug, Clone, PartialEq)]
pub struct DivergenceResolution {
    /// The tick both sides describe — the staged scan's tick, equal to
    /// the applied checkpoint's tick by the check's pairing rule.
    pub tick: Tick,
    /// The staged `Out` points whose field reads matched, in ascending
    /// point order.
    pub points: Vec<PointId>,
}

/// One same-tick comparison's verdict over a staged field `Out` image:
/// every staged point lands in exactly one of the three lists, so a
/// comparison that observed nothing is distinguishable from one that
/// observed the image agreeing with the field.
#[derive(Debug, Clone, PartialEq)]
pub struct FieldComparison {
    /// Staged `Out` points whose field reads matched, in ascending
    /// point order — the positive convergence evidence.
    pub matched: Vec<PointId>,
    /// The mismatched field `Out` points, in ascending point order —
    /// the divergence evidence itself.
    pub mismatches: Vec<Divergence>,
    /// Staged points whose field read failed — a communication fault is
    /// the driver's own diagnostics' business, not divergence evidence;
    /// the comparison neither convicts nor clears them. A comparison
    /// that cleared a `Diverged` verdict must have this list empty:
    /// only a fully-read match is the "staged outputs now track the
    /// field" evidence the clear stands on.
    pub unread: Vec<PointId>,
}

/// Compares the staged field `Out` image — what the standby's scan would
/// have written — against the driver's reads of the same points.
/// Deterministic: the result is ordered by `PointId` and depends only on
/// the staged image and the driver's answers.
///
/// A point whose field read fails reports in
/// [`FieldComparison::unread`] rather than being silently skipped: the
/// failed read observed nothing, so it can neither convict the staged
/// image nor clear a standing `Diverged` verdict — only a comparison
/// that actually read the field and matched carries the clearing
/// evidence.
pub fn compare_staged(
    driver: &(dyn IoDriver + Sync),
    staged: &BTreeMap<PointId, Sample>,
) -> FieldComparison {
    let mut comparison = FieldComparison {
        matched: Vec::new(),
        mismatches: Vec::new(),
        unread: Vec::new(),
    };
    for (&point, &staged_sample) in staged {
        match driver.read(point) {
            Ok(field) if values_diverge(staged_sample.value, field.value) => {
                comparison.mismatches.push(Divergence {
                    point,
                    staged: staged_sample.value,
                    field: field.value,
                });
            }
            Ok(_) => comparison.matched.push(point),
            Err(_) => comparison.unread.push(point),
        }
    }
    comparison
}

#[cfg(test)]
mod tests {
    use super::*;
    use dcs_core::{IoError, Quality, Sample};
    use std::collections::HashMap;
    use std::sync::Mutex;

    /// A read-only field stub for the comparison rule: each read answers
    /// `samples`'s entry, or `UnknownPoint` when absent.
    struct FieldStub {
        samples: Mutex<HashMap<PointId, Sample>>,
    }

    impl IoDriver for FieldStub {
        fn read(&self, point: PointId) -> Result<Sample, IoError> {
            self.samples
                .lock()
                .unwrap()
                .get(&point)
                .copied()
                .ok_or(IoError::UnknownPoint(point))
        }

        fn write(&self, point: PointId, _value: Value) -> Result<(), IoError> {
            Err(IoError::UnknownPoint(point))
        }
    }

    fn good(value: Value) -> Sample {
        Sample::new(value, Quality::Good, Tick::ZERO)
    }

    #[test]
    fn per_kind_rule() {
        // Exact for Bool and Int; tolerance for Float; kind mismatch diverges.
        assert!(!values_diverge(Value::Bool(true), Value::Bool(true)));
        assert!(values_diverge(Value::Bool(true), Value::Bool(false)));
        assert!(!values_diverge(Value::Int(7), Value::Int(7)));
        assert!(values_diverge(Value::Int(7), Value::Int(8)));
        assert!(!values_diverge(
            Value::Float(1.0),
            Value::Float(1.0 + 5e-10)
        ));
        assert!(values_diverge(Value::Float(1.0), Value::Float(1.1)));
        assert!(values_diverge(Value::Float(1.0), Value::Int(1)));
        assert!(values_diverge(Value::Float(f64::NAN), Value::Float(1.0)));
        assert!(!values_diverge(
            Value::Float(f64::NAN),
            Value::Float(f64::NAN)
        ));
    }

    #[test]
    fn compare_staged_is_point_ordered_and_marks_failed_reads_unread() {
        let field = FieldStub {
            samples: Mutex::new(
                [
                    (PointId(1), good(Value::Bool(true))),
                    (PointId(2), good(Value::Float(4.5))),
                    // Point 3 absent: the failed read is evidence-free —
                    // reported unread, neither convicting nor clearing.
                ]
                .into_iter()
                .collect(),
            ),
        };
        let staged: BTreeMap<PointId, Sample> = [
            (PointId(1), good(Value::Bool(true))),
            (PointId(2), good(Value::Float(9.0))),
            (PointId(3), good(Value::Int(1))),
        ]
        .into_iter()
        .collect();
        assert_eq!(
            compare_staged(&field, &staged),
            FieldComparison {
                matched: vec![PointId(1)],
                mismatches: vec![Divergence {
                    point: PointId(2),
                    staged: Value::Float(9.0),
                    field: Value::Float(4.5),
                }],
                unread: vec![PointId(3)],
            }
        );
    }
}
