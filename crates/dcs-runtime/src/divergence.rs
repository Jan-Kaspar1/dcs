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
//! and journals the evidence; a fresh clean transfer whose comparison
//! matches clears it.
//!
//! The comparison is deterministic and exact per value kind: `Bool` and
//! `Int` values must be identical; `Float` values agree within
//! [`FLOAT_TOLERANCE`]. A point whose field read fails — a communication
//! fault is not divergence evidence — and a staged image whose tick no
//! transferred checkpoint has reached yet are skipped, so the check only
//! ever fires on same-tick, same-field observations.

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

/// Compares the staged field `Out` image — what the standby's scan would
/// have written — against the driver's reads of the same points.
/// Deterministic: the result is ordered by `PointId` and depends only on
/// the staged image and the driver's answers.
///
/// A point whose field read fails is skipped: a communication fault is
/// the driver's own diagnostics' business, not divergence evidence — the
/// check only acts on a value the field demonstrably carries.
pub fn compare_staged(
    driver: &(dyn IoDriver + Sync),
    staged: &BTreeMap<PointId, Sample>,
) -> Vec<Divergence> {
    staged
        .iter()
        .filter_map(|(&point, &staged_sample)| {
            let field = driver.read(point).ok()?;
            values_diverge(staged_sample.value, field.value).then_some(Divergence {
                point,
                staged: staged_sample.value,
                field: field.value,
            })
        })
        .collect()
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
    fn compare_staged_is_point_ordered_and_skips_failed_reads() {
        let field = FieldStub {
            samples: Mutex::new(
                [
                    (PointId(1), good(Value::Bool(true))),
                    (PointId(2), good(Value::Float(4.5))),
                    // Point 3 absent: the failed read is skipped.
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
            vec![Divergence {
                point: PointId(2),
                staged: Value::Float(9.0),
                field: Value::Float(4.5),
            }]
        );
    }
}
