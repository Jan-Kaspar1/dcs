//! The `point.{id}` section of the simulated drivers' capture/restore
//! contract.
//!
//! [`SimDriver`](crate::SimDriver) and
//! [`ScriptedDriver`](crate::ScriptedDriver) spell one bound point's
//! checkpointed state identically — `point.{id}.value` / `.quality` /
//! `.tick` for the stored or observed sample — and decode it under the
//! same rules on restore: each field required with the bound point's
//! declared kind, quality codes decodable, sample ticks non-negative.
//! `SimDriver` additionally participates in `point.{id}.fault`, the
//! injected-[`Fault`] encoding; `ScriptedDriver` does not, so the field
//! stays foreign to its strictness check. Single-sourcing the section
//! here — the shared half of the decision-11 checkpoint wire contract —
//! keeps a third capturing backend from forking the spelling. Each
//! driver keeps its own sections (`tick`, `element.*`, `writes.*`), its
//! own apply semantics, and the single trailing `ensure_known_fields`
//! check over the union [`restore_points`] helped build.

use crate::driver::{Fault, decode_fault, decode_quality, encode_fault, encode_quality};
use dcs_core::{PointId, Sample, StateError, StateMap, Tick, Value, ValueKind};
use std::collections::HashMap;

/// Whether a driver participates in the `point.{id}.fault` field.
///
/// [`SimDriver`](crate::SimDriver) declares
/// [`Participates`](Self::Participates): the field is emitted while a
/// fault is active, listed as known for every bound point, and restored
/// when carried — an absent field restores `None`, never a
/// missing-field error. [`ScriptedDriver`](crate::ScriptedDriver)
/// declares [`Foreign`](Self::Foreign): the field is never emitted nor
/// listed, so a map carrying `point.{id}.fault` fails its strictness
/// check as a foreign field.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum FaultParticipation {
    /// `point.{id}.fault` is an optional known field on every bound
    /// point.
    Participates,
    /// `point.{id}.fault` is outside the driver's captured vocabulary.
    Foreign,
}

/// One bound point's decoded state, as [`restore_points`] validated it.
pub(crate) struct RestoredPoint {
    /// The decoded `point.{id}.value` / `.quality` / `.tick` sample.
    pub sample: Sample,
    /// The decoded `point.{id}.fault` — always `None` under
    /// [`FaultParticipation::Foreign`].
    pub fault: Option<Fault>,
}

/// Emits the `point.{id}` section into `captured`: `.value`, `.quality`,
/// and `.tick` for every entry of `points` — the driver's bound points
/// sorted by [`PointId`] — plus `.fault` where `fault` reports an
/// injected fault on the point. A driver without fault participation
/// passes an accessor returning `None`.
pub(crate) fn capture_points(
    captured: &mut StateMap,
    points: &[(PointId, Sample)],
    fault: impl Fn(PointId) -> Option<Fault>,
) {
    for &(point, sample) in points {
        let prefix = format!("point.{}", point.0);
        captured.insert(format!("{prefix}.value"), sample.value);
        captured.insert(
            format!("{prefix}.quality"),
            Value::Int(encode_quality(sample.quality)),
        );
        captured.insert(format!("{prefix}.tick"), Value::Int(sample.tick.0 as i64));
        if let Some(fault) = fault(point) {
            captured.insert(format!("{prefix}.fault"), Value::Int(encode_fault(fault)));
        }
    }
}

/// Validates and decodes the `point.{id}` section for `points` — the
/// driver's bound points with their declared kinds — returning each
/// point's [`RestoredPoint`] and appending the section's field names to
/// `known`, so the caller's single `ensure_known_fields` check still
/// covers it alongside the driver's own sections. Errors name `element`,
/// the driver's own `STATE_ELEMENT`; `faults` declares the driver's
/// [`FaultParticipation`].
pub(crate) fn restore_points(
    points: impl IntoIterator<Item = (PointId, ValueKind)>,
    state: &StateMap,
    element: &str,
    faults: FaultParticipation,
    known: &mut Vec<String>,
) -> Result<HashMap<PointId, RestoredPoint>, StateError> {
    let invalid = |field: String, value: Value| StateError::InvalidValue {
        element: element.to_string(),
        field,
        value,
    };
    let points = points.into_iter();
    let (capacity, _) = points.size_hint();
    let mut restored = HashMap::with_capacity(capacity);
    for (point, kind) in points {
        let prefix = format!("point.{}", point.0);
        let value = state.require_kind(element, &format!("{prefix}.value"), kind)?;
        // A stored `Float` must be finite like every value the point
        // can hold live: a checkpoint carrying NaN or an infinity would
        // restore a sample no wire contract can spell back.
        if let Value::Float(v) = value
            && !v.is_finite()
        {
            return Err(invalid(format!("{prefix}.value"), value));
        }
        let quality_code = state.require_i64(element, &format!("{prefix}.quality"))?;
        let quality = decode_quality(quality_code)
            .ok_or_else(|| invalid(format!("{prefix}.quality"), Value::Int(quality_code)))?;
        let sample_tick = state.require_i64(element, &format!("{prefix}.tick"))?;
        if sample_tick < 0 {
            return Err(invalid(format!("{prefix}.tick"), Value::Int(sample_tick)));
        }
        let fault = match faults {
            FaultParticipation::Participates => match state.get(&format!("{prefix}.fault")) {
                None => None,
                Some(Value::Int(code)) => Some(
                    decode_fault(code)
                        .ok_or_else(|| invalid(format!("{prefix}.fault"), Value::Int(code)))?,
                ),
                Some(found) => {
                    return Err(StateError::IncompatibleField {
                        element: element.to_string(),
                        field: format!("{prefix}.fault"),
                        expected: ValueKind::Int,
                        found: found.kind(),
                    });
                }
            },
            FaultParticipation::Foreign => None,
        };
        known.push(format!("{prefix}.value"));
        known.push(format!("{prefix}.quality"));
        known.push(format!("{prefix}.tick"));
        if faults == FaultParticipation::Participates {
            known.push(format!("{prefix}.fault"));
        }
        restored.insert(
            point,
            RestoredPoint {
                sample: Sample::new(value, quality, Tick(sample_tick as u64)),
                fault,
            },
        );
    }
    Ok(restored)
}
