//! Point-to-signal metadata index for monitoring consumers.
//!
//! [`SignalIndex`] is a derived view of a [`PlantModel`]: one [`PointSignal`]
//! entry per declared [`IoPoint`](crate::IoPoint), ordered by [`PointId`],
//! carrying the metadata a monitoring UI needs — signal name, engineering
//! unit, description, direction, and value type — so consumers never walk
//! the device/channel/signal graph themselves. [`PlantModel::signal_index`]
//! builds it.
//!
//! Resolution rules:
//!
//! - A point sourced by a [`Signal`](crate::Signal) copies the signal's
//!   `name`, `unit`, and `description`; `direction` and `value_type` always
//!   come from the point itself.
//! - A point no signal sources gets a default entry: `signal` is `None`,
//!   `name` is `"point-<id>"`, and `unit`/`description` are `None`.
//! - When several signals source one point, the lowest [`SignalId`] wins, so
//!   the index does not depend on document order.
//! - A signal whose `source` is not a declared point cannot occur in a
//!   validated model ([`PlantModel::load`] rejects it); from an unvalidated
//!   model it is ignored, since there is no point to attach it to.

use crate::model::{Direction, PlantModel, Signal};
use dcs_core::{PointId, SignalId, ValueKind};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

/// A point's monitoring metadata: one entry of a [`SignalIndex`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PointSignal {
    /// The point this entry describes.
    pub point: PointId,
    /// The signal sourcing the point, or `None` for a signal-less point.
    pub signal: Option<SignalId>,
    /// Display name: the signal's name, or `"point-<id>"` when no signal
    /// sources the point.
    pub name: String,
    /// Whether the point is read from (`In`) or written to (`Out`) the field.
    pub direction: Direction,
    /// The point's value type.
    pub value_type: ValueKind,
    /// Engineering unit declared by the signal, if any.
    pub unit: Option<String>,
    /// Human-facing description declared by the signal, if any.
    pub description: Option<String>,
}

/// A derived view resolving every declared I/O point to its monitoring
/// metadata; see the [module documentation](self) for the resolution rules.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SignalIndex {
    /// One entry per declared point, ordered by [`PointId`].
    pub points: Vec<PointSignal>,
}

impl SignalIndex {
    /// The entry for `point`, if the model declares it. Entries are ordered
    /// by [`PointId`], so lookup is a binary search.
    pub fn get(&self, point: PointId) -> Option<&PointSignal> {
        self.points
            .binary_search_by_key(&point, |entry| entry.point)
            .ok()
            .map(|index| &self.points[index])
    }
}

impl PlantModel {
    /// Builds the point-to-signal [`SignalIndex`] for monitoring consumers.
    ///
    /// The model is expected to be validated — via [`PlantModel::load`] or
    /// [`PlantModel::validate`] — before indexing; the resolution rules in
    /// the [module documentation](self) define the outcome either way.
    pub fn signal_index(&self) -> SignalIndex {
        let mut signals_by_point: BTreeMap<u64, &Signal> = BTreeMap::new();
        for signal in &self.signals {
            signals_by_point
                .entry(signal.source.0)
                .and_modify(|current| {
                    if signal.id < current.id {
                        *current = signal;
                    }
                })
                .or_insert(signal);
        }

        let mut points: Vec<_> = self.io_points.iter().collect();
        points.sort_by_key(|point| point.id);
        let points = points
            .into_iter()
            .map(|point| match signals_by_point.get(&point.id.0) {
                Some(signal) => PointSignal {
                    point: point.id,
                    signal: Some(signal.id),
                    name: signal.name.clone(),
                    direction: point.direction,
                    value_type: point.value_type,
                    unit: signal.unit.clone(),
                    description: signal.description.clone(),
                },
                None => PointSignal {
                    point: point.id,
                    signal: None,
                    name: format!("point-{}", point.id.0),
                    direction: point.direction,
                    value_type: point.value_type,
                    unit: None,
                    description: None,
                },
            })
            .collect();
        SignalIndex { points }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const MINIMAL: &str = include_str!("../fixtures/minimal.json");
    const SIGNAL_INDEX: &str = include_str!("../fixtures/signal_index.json");
    const EXPECTED_INDEX: &str = include_str!("../fixtures/signal_index.index.json");

    #[test]
    fn fixture_index_resolves_signal_metadata() {
        let model = PlantModel::load(SIGNAL_INDEX).unwrap();
        let index = model.signal_index();
        assert_eq!(index.points.len(), 3);

        let input = index.get(PointId(10)).unwrap();
        assert_eq!(input.signal, Some(SignalId(100)));
        assert_eq!(input.name, "reactor-temperature");
        assert_eq!(input.direction, Direction::In);
        assert_eq!(input.value_type, ValueKind::Float);
        assert_eq!(input.unit.as_deref(), Some("degC"));
        assert_eq!(
            input.description.as_deref(),
            Some("Reactor core temperature")
        );

        let output = index.get(PointId(12)).unwrap();
        assert_eq!(output.signal, Some(SignalId(101)));
        assert_eq!(output.name, "valve-command");
        assert_eq!(output.direction, Direction::Out);
        assert_eq!(output.value_type, ValueKind::Float);
        assert_eq!(output.unit.as_deref(), Some("%"));
        assert_eq!(
            output.description.as_deref(),
            Some("Control valve position command")
        );
    }

    #[test]
    fn fixture_index_matches_checked_in_output() {
        let model = PlantModel::load(SIGNAL_INDEX).unwrap();
        let json = serde_json::to_string_pretty(&model.signal_index()).unwrap();
        assert_eq!(json, EXPECTED_INDEX.trim_end());
    }

    #[test]
    fn index_serde_roundtrips() {
        let model = PlantModel::load(SIGNAL_INDEX).unwrap();
        let index = model.signal_index();
        let json = serde_json::to_string(&index).unwrap();
        assert_eq!(serde_json::from_str::<SignalIndex>(&json).unwrap(), index);
    }

    #[test]
    fn signal_less_points_index_with_default_entries() {
        let mut model: PlantModel = serde_json::from_str(MINIMAL).unwrap();
        model.signals.clear();
        let index = model.signal_index();
        assert_eq!(index.points.len(), model.io_points.len());
        for entry in &index.points {
            assert_eq!(entry.signal, None);
            assert_eq!(entry.name, format!("point-{}", entry.point.0));
            assert_eq!(entry.unit, None);
            assert_eq!(entry.description, None);
        }
        // Direction and value type still come from the point.
        let entry = index.get(PointId(11)).unwrap();
        assert_eq!(entry.direction, Direction::Out);
        assert_eq!(entry.value_type, ValueKind::Float);
    }

    #[test]
    fn lowest_signal_id_wins_for_shared_point() {
        let mut model: PlantModel = serde_json::from_str(MINIMAL).unwrap();
        model.signals.push(Signal {
            id: SignalId(50),
            name: "alias".to_string(),
            source: PointId(10),
            unit: None,
            description: None,
        });
        let index = model.signal_index();
        let entry = index.get(PointId(10)).unwrap();
        assert_eq!(entry.signal, Some(SignalId(50)));
        assert_eq!(entry.name, "alias");
    }

    #[test]
    fn signal_without_point_is_ignored() {
        // Parse without validating: a signal whose source is not a declared
        // point has no entry to attach to and is ignored.
        let mut model: PlantModel = serde_json::from_str(MINIMAL).unwrap();
        model.signals[0].source = PointId(99);
        let index = model.signal_index();
        assert_eq!(index.get(PointId(10)).unwrap().signal, None);
        assert_eq!(index.get(PointId(99)), None);
    }
}
