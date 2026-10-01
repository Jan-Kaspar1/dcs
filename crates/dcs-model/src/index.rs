//! Point-to-signal metadata index for monitoring consumers.
//!
//! [`SignalIndex`] is a derived view of a [`PlantModel`]: one [`PointSignal`]
//! entry per declared [`IoPoint`](crate::IoPoint), ordered by [`PointId`],
//! carrying the metadata a monitoring UI needs — signal name, engineering
//! unit, description, display group, direction, value type, and command
//! writability — plus one [`ComponentRecord`] per declared
//! [`ComponentInstance`](crate::ComponentInstance) carrying the per-instance
//! model data descriptors do not, today the decision-70 rationalization
//! block — so consumers never walk the device/channel/signal graph
//! themselves.
//! [`PlantModel::signal_index`] builds it.
//!
//! Resolution rules:
//!
//! - A point sourced by a [`Signal`](crate::Signal) copies the signal's
//!   `name`, `unit`, `description`, and `group`; `direction`, `value_type`,
//!   and `writable` always come from the point itself.
//! - A point no signal sources gets a default entry: `signal` is `None`,
//!   `name` is `"point-<id>"`, and `unit`/`description`/`group` are `None`.
//! - When several signals source one point, the lowest [`SignalId`] wins, so
//!   the index does not depend on document order.
//! - A signal whose `source` is not a declared point cannot occur in a
//!   validated model ([`PlantModel::load`] rejects it); from an unvalidated
//!   model it is ignored, since there is no point to attach it to.

use crate::model::{Direction, PlantModel, Rationalization, Signal};
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
    /// Display group declared by the signal, if any. `None` means the
    /// point is ungrouped; the monitoring UI files such points under its
    /// documented default group.
    pub group: Option<String>,
    /// Whether the point's `io_point` declaration marks it writable — the
    /// model-declared command surface. A monitoring consumer may offer
    /// operator `WriteValue` commands against these `In` points only;
    /// writes to `Out` points and unmarked points are refused with a
    /// `not_writable` rejection.
    pub writable: bool,
    /// Whether the point's `io_point` declaration marks its commands
    /// reason-carrying: a writable `In` point's reasonless command
    /// refuses at admission with a `reason_required` rejection, so a
    /// consumer may demand the operator declare one up front. Serde-
    /// defaulted like [`Signal::unit`](crate::Signal::unit): an index
    /// served before the field existed decodes it `false`.
    #[serde(default)]
    pub requires_reason: bool,
}

/// A component instance's monitoring record — the per-instance model
/// data a monitoring consumer joins against the snapshot's descriptors
/// that the descriptor contract itself does not carry: today, the
/// declared decision-70 rationalization block.
///
/// `name` is the instance's diagnostic name —
/// [`ComponentInstance::name`](crate::ComponentInstance::name) — the
/// identity the assembled component reports as
/// `ComponentDescriptor.name`, so the record joins the descriptor by
/// name without the consumer knowing the naming convention.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ComponentRecord {
    /// The instance's diagnostic name — `"<kind>:<id>"` — matching the
    /// served `ComponentDescriptor.name`.
    pub name: String,
    /// The instance's declared kind.
    pub kind: String,
    /// The instance's declared alarm rationalization block — the
    /// consequence of inaction, the required action, and the
    /// display/procedure reference — when it carries one.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub rationalization: Option<Rationalization>,
}

/// A derived view resolving every declared I/O point to its monitoring
/// metadata; see the [module documentation](self) for the resolution rules.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SignalIndex {
    /// One entry per declared point, ordered by [`PointId`].
    pub points: Vec<PointSignal>,
    /// One record per declared component instance, in the model's
    /// `components` order — the declared scan order.
    ///
    /// Serde-defaulted like [`Signal::unit`]: an index serialized before
    /// the section existed decodes it empty, and a consumer simply joins
    /// nothing — the model keeps evolving without a version bump.
    #[serde(default)]
    pub components: Vec<ComponentRecord>,
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

        let components = self
            .components
            .iter()
            .map(|instance| ComponentRecord {
                name: instance.name(),
                kind: instance.kind.clone(),
                rationalization: instance.rationalization.clone(),
            })
            .collect();

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
                    group: signal.group.clone(),
                    writable: point.writable,
                    requires_reason: point.requires_reason,
                },
                None => PointSignal {
                    point: point.id,
                    signal: None,
                    name: format!("point-{}", point.id.0),
                    direction: point.direction,
                    value_type: point.value_type,
                    unit: None,
                    description: None,
                    group: None,
                    writable: point.writable,
                    requires_reason: point.requires_reason,
                },
            })
            .collect();
        SignalIndex { points, components }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const MINIMAL: &str = include_str!("../fixtures/minimal.json");
    const SIGNAL_INDEX: &str = include_str!("../fixtures/signal_index.json");
    const SIGNAL_GROUPS: &str = include_str!("../fixtures/signal_groups.json");
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

        // The fixture predates the optional group field: every entry is
        // ungrouped, including the signal-less point 11.
        for entry in &index.points {
            assert_eq!(entry.group, None, "{}", entry.name);
        }
    }

    #[test]
    fn index_carries_each_signals_display_group() {
        let model = PlantModel::load(SIGNAL_GROUPS).unwrap();
        let index = model.signal_index();
        assert_eq!(index.points.len(), 4);

        // Grouped signals pass their group through to their point's entry.
        assert_eq!(
            index.get(PointId(10)).unwrap().group.as_deref(),
            Some("reactor")
        );
        assert_eq!(
            index.get(PointId(11)).unwrap().group.as_deref(),
            Some("reactor")
        );
        assert_eq!(
            index.get(PointId(13)).unwrap().group.as_deref(),
            Some("utilities")
        );
        // An ungrouped signal leaves its point's entry ungrouped.
        assert_eq!(index.get(PointId(12)).unwrap().group, None);
    }

    #[test]
    fn fixture_index_matches_checked_in_output() {
        let model = PlantModel::load(SIGNAL_INDEX).unwrap();
        let json = serde_json::to_string_pretty(&model.signal_index()).unwrap();
        assert_eq!(json, EXPECTED_INDEX.trim_end());
    }

    #[test]
    fn index_carries_each_instances_component_record() {
        let model = PlantModel::load(SIGNAL_INDEX).unwrap();
        let index = model.signal_index();
        // One record per declared instance, named by the same
        // `"<kind>:<id>"` convention the assembled component reports as
        // its descriptor name — the join key the record documents.
        assert_eq!(index.components.len(), model.components.len());
        let record = &index.components[0];
        assert_eq!(record.name, "gain:1");
        assert_eq!(record.name, model.components[0].name());
        assert_eq!(record.kind, "gain");
        // The fixture's instance declares no rationalization block.
        assert_eq!(record.rationalization, None);
    }

    #[test]
    fn component_record_carries_the_rationalization_block() {
        let mut model: PlantModel = serde_json::from_str(MINIMAL).unwrap();
        model.components[0].rationalization = Some(Rationalization {
            consequence: "the vessel overfills".to_string(),
            required_action: "close the inlet valve".to_string(),
            reference: "ALM-101 procedure".to_string(),
        });
        let index = model.signal_index();
        let record = index
            .components
            .iter()
            .find(|record| record.name == model.components[0].name())
            .unwrap();
        assert_eq!(record.rationalization, model.components[0].rationalization);
        // The served record roundtrips — the monitoring consumer decodes
        // exactly the block the model declared.
        let json = serde_json::to_string(&index).unwrap();
        assert_eq!(serde_json::from_str::<SignalIndex>(&json).unwrap(), index);
        // An index serialized before the section existed decodes it empty.
        let legacy: SignalIndex = serde_json::from_str("{\"points\": []}").unwrap();
        assert!(legacy.components.is_empty());
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
            assert_eq!(entry.group, None);
        }
        // Direction and value type still come from the point.
        let entry = index.get(PointId(11)).unwrap();
        assert_eq!(entry.direction, Direction::Out);
        assert_eq!(entry.value_type, ValueKind::Float);
    }

    #[test]
    fn index_carries_the_points_writable_mark() {
        let mut model: PlantModel = serde_json::from_str(MINIMAL).unwrap();
        model.io_points[0].writable = true;
        let index = model.signal_index();
        assert!(index.get(PointId(10)).unwrap().writable);
        assert!(!index.get(PointId(11)).unwrap().writable);
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
            group: Some("aliases".to_string()),
        });
        let index = model.signal_index();
        let entry = index.get(PointId(10)).unwrap();
        assert_eq!(entry.signal, Some(SignalId(50)));
        assert_eq!(entry.name, "alias");
        assert_eq!(entry.group.as_deref(), Some("aliases"));
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
