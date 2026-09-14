//! Structural diffing between two validated
//! [`PlantModel`](crate::PlantModel) revisions.
//!
//! [`PlantModel::diff`](crate::PlantModel::diff) compares an old document
//! against a revised one and reports the elements the revision adds,
//! removes, or changes, grouped by element class and ordered
//! deterministically — the engineering-review surface for a rolling model
//! revision. Devices, io_points, signals, and components are matched by
//! their model id; connections have no id and are matched as a multiset on
//! their endpoints, so duplicate wires diff correctly.
//!
//! A `changed` entry carries field-level [`FieldChange`]s computed over the
//! element's serialized form: scalar fields report old and new values while
//! nested maps recurse to dotted paths, so `parameters.k`,
//! `ports.out.value_type`, and `channels.ch1.direction` name the parameter,
//! port, or channel that moved. Because the comparison runs over the
//! serialized schema rather than a hand-maintained field list, optional
//! fields added later — a declared `writable` flag on points, say — are
//! diffed automatically once they exist.

use crate::model::{ComponentInstance, Connection, Device, Endpoint, IoPoint, PlantModel, Signal};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::fmt;

/// How one element differs between the old and revised documents.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ChangeKind {
    /// The revision declares an element the old document does not.
    Added,
    /// The old document declares an element the revision drops.
    Removed,
    /// Both documents declare the element, but its fields differ.
    Changed,
}

impl fmt::Display for ChangeKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            ChangeKind::Added => "added",
            ChangeKind::Removed => "removed",
            ChangeKind::Changed => "changed",
        })
    }
}

/// One element-level difference, naming the element it applies to.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ElementChange {
    /// Whether the element was added, removed, or changed.
    pub change: ChangeKind,
    /// A human-readable name for the element: `device 3`, `io_point 12`,
    /// `signal 100 "reactor-temp"`, `component 1 "pid"`, or a connection's
    /// `from -> to` endpoints.
    pub element: String,
    /// The element's model id where its class has one; connections carry
    /// none.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub id: Option<u64>,
    /// Field-level differences for `changed` entries — e.g. `direction`,
    /// `channel.name`, `parameters.k` — empty for added and removed
    /// entries.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub fields: Vec<FieldChange>,
}

/// One field-level difference inside a changed element.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FieldChange {
    /// The field's dotted path within the element's serialized form, e.g.
    /// `direction`, `channel.name`, or `ports.out.value_type`.
    pub field: String,
    /// The old document's value; absent when the revision newly declares
    /// the field.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub old: Option<serde_json::Value>,
    /// The revision's value; absent when the revision drops the field.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub new: Option<serde_json::Value>,
}

/// The differences between two validated plant model documents, grouped by
/// element class. Each class lists its entries in a deterministic order —
/// by element id, or by endpoint description for connections.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct ModelDiff {
    /// Added, removed, and changed devices.
    pub devices: Vec<ElementChange>,
    /// Added, removed, and changed io_points.
    pub io_points: Vec<ElementChange>,
    /// Added, removed, and changed signals.
    pub signals: Vec<ElementChange>,
    /// Added, removed, and changed components.
    pub components: Vec<ElementChange>,
    /// Added and removed connections; a connection is its endpoints, so it
    /// cannot change in place — rewiring removes one and adds another.
    pub connections: Vec<ElementChange>,
}

impl ModelDiff {
    /// Whether the two documents declare identical content.
    pub fn is_empty(&self) -> bool {
        self.devices.is_empty()
            && self.io_points.is_empty()
            && self.signals.is_empty()
            && self.components.is_empty()
            && self.connections.is_empty()
    }
}

impl PlantModel {
    /// Diffs this document against a revised one, reporting added, removed,
    /// and changed elements in every class, each entry naming its element.
    ///
    /// Both models must already be valid — load them through
    /// [`PlantModel::load`], which guarantees the unique ids this matching
    /// relies on. The result is deterministic: entries are ordered by
    /// element id (connections by endpoint description) and field changes
    /// by field path.
    pub fn diff(&self, revised: &PlantModel) -> ModelDiff {
        ModelDiff {
            devices: diff_elements(
                &self.devices,
                &revised.devices,
                |device| device.id.0,
                describe_device,
            ),
            io_points: diff_elements(
                &self.io_points,
                &revised.io_points,
                |point| point.id.0,
                describe_point,
            ),
            signals: diff_elements(
                &self.signals,
                &revised.signals,
                |signal| signal.id.0,
                describe_signal,
            ),
            components: diff_elements(
                &self.components,
                &revised.components,
                |component| component.id.0,
                describe_component,
            ),
            connections: diff_connections(&self.connections, &revised.connections),
        }
    }
}

/// Diffs one id-keyed element class: elements present in only one document
/// are added or removed; elements present in both but serializing
/// differently are changed, with [`field_changes`] naming the fields.
fn diff_elements<T: Serialize>(
    old: &[T],
    new: &[T],
    id: impl Fn(&T) -> u64,
    describe: impl Fn(&T) -> String,
) -> Vec<ElementChange> {
    let old_by_id: BTreeMap<u64, &T> = old.iter().map(|element| (id(element), element)).collect();
    let new_by_id: BTreeMap<u64, &T> = new.iter().map(|element| (id(element), element)).collect();
    let ids: BTreeSet<u64> = old_by_id.keys().chain(new_by_id.keys()).copied().collect();
    let mut changes = Vec::new();
    for key in ids {
        match (old_by_id.get(&key), new_by_id.get(&key)) {
            (Some(old), None) => changes.push(ElementChange {
                change: ChangeKind::Removed,
                element: describe(old),
                id: Some(key),
                fields: Vec::new(),
            }),
            (None, Some(new)) => changes.push(ElementChange {
                change: ChangeKind::Added,
                element: describe(new),
                id: Some(key),
                fields: Vec::new(),
            }),
            (Some(old), Some(new)) => {
                let fields = field_changes(old, new);
                if !fields.is_empty() {
                    changes.push(ElementChange {
                        change: ChangeKind::Changed,
                        element: describe(new),
                        id: Some(key),
                        fields,
                    });
                }
            }
            (None, None) => {}
        }
    }
    changes
}

/// Diffs two connections lists as a multiset keyed on each connection's
/// `from -> to` description: copies present only in the old document are
/// removed, copies present only in the revision added.
fn diff_connections(old: &[Connection], new: &[Connection]) -> Vec<ElementChange> {
    fn counts(connections: &[Connection]) -> BTreeMap<String, usize> {
        let mut counts = BTreeMap::new();
        for connection in connections {
            *counts.entry(describe_connection(connection)).or_default() += 1;
        }
        counts
    }
    let old = counts(old);
    let new = counts(new);
    let keys: BTreeSet<&String> = old.keys().chain(new.keys()).collect();
    let mut changes = Vec::new();
    for key in keys {
        let old_count = old.get(key).copied().unwrap_or(0);
        let new_count = new.get(key).copied().unwrap_or(0);
        for _ in 0..old_count.saturating_sub(new_count) {
            changes.push(ElementChange {
                change: ChangeKind::Removed,
                element: key.clone(),
                id: None,
                fields: Vec::new(),
            });
        }
        for _ in 0..new_count.saturating_sub(old_count) {
            changes.push(ElementChange {
                change: ChangeKind::Added,
                element: key.clone(),
                id: None,
                fields: Vec::new(),
            });
        }
    }
    changes
}

/// Computes the field-level differences between two serializable elements
/// by recursively comparing their serialized object forms.
fn field_changes<T: Serialize>(old: &T, new: &T) -> Vec<FieldChange> {
    let old = serde_json::to_value(old).expect("model elements serialize to JSON");
    let new = serde_json::to_value(new).expect("model elements serialize to JSON");
    let mut fields = Vec::new();
    diff_values("", &old, &new, &mut fields);
    fields
}

/// Recursively compares two serialized values, appending a [`FieldChange`]
/// per differing leaf. Objects recurse into the sorted union of their keys
/// so nested maps — channels, parameters, ports — produce dotted paths;
/// anything else differing reports the path's two values directly. A
/// serialized [`Value`](dcs_core::Value) counts as a leaf even though it is
/// an object: `{"Float": 5.0} -> {"Int": 8}` is one kind change, not a
/// removed `Float` field plus an added `Int` field.
fn diff_values(
    path: &str,
    old: &serde_json::Value,
    new: &serde_json::Value,
    out: &mut Vec<FieldChange>,
) {
    match (old.as_object(), new.as_object()) {
        (Some(old_map), Some(new_map))
            if !is_signal_value(old) && !is_signal_value(new) =>
        {
            let keys: BTreeSet<&String> = old_map.keys().chain(new_map.keys()).collect();
            for key in keys {
                let field = if path.is_empty() {
                    key.clone()
                } else {
                    format!("{path}.{key}")
                };
                match (old_map.get(key), new_map.get(key)) {
                    (Some(old), Some(new)) => diff_values(&field, old, new, out),
                    (old, new) => out.push(FieldChange {
                        field,
                        old: old.cloned(),
                        new: new.cloned(),
                    }),
                }
            }
        }
        _ if old != new => out.push(FieldChange {
            field: path.to_owned(),
            old: Some(old.clone()),
            new: Some(new.clone()),
        }),
        _ => {}
    }
}

/// Whether a serialized value is a [`Value`](dcs_core::Value): an
/// externally-tagged enum, so a single-key object whose key names a variant.
fn is_signal_value(value: &serde_json::Value) -> bool {
    match value.as_object() {
        Some(map) if map.len() == 1 => {
            matches!(
                map.keys().next().map(String::as_str),
                Some("Bool" | "Int" | "Float")
            )
        }
        _ => false,
    }
}

fn describe_device(device: &Device) -> String {
    format!("device {}", device.id.0)
}

fn describe_point(point: &IoPoint) -> String {
    format!("io_point {}", point.id.0)
}

fn describe_signal(signal: &Signal) -> String {
    format!("signal {} {:?}", signal.id.0, signal.name)
}

fn describe_component(component: &ComponentInstance) -> String {
    format!("component {} {:?}", component.id.0, component.kind)
}

fn describe_connection(connection: &Connection) -> String {
    format!(
        "{} -> {}",
        describe_endpoint(&connection.from),
        describe_endpoint(&connection.to)
    )
}

fn describe_endpoint(endpoint: &Endpoint) -> String {
    match endpoint {
        Endpoint::Point(id) => format!("point {}", id.0),
        Endpoint::Port(reference) => {
            format!("port {:?} on component {}", reference.name, reference.component.0)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const BASE: &str = include_str!("../fixtures/diff/base.json");
    const REVISED: &str = include_str!("../fixtures/diff/revised.json");

    // Both fixtures must validate: the diff contract only covers valid
    // documents — an invalid one is reported with its errors, never diffed.
    fn base() -> PlantModel {
        PlantModel::load(BASE).unwrap()
    }

    fn revised() -> PlantModel {
        PlantModel::load(REVISED).unwrap()
    }

    #[test]
    fn identical_models_produce_an_empty_diff() {
        let model = base();
        assert!(model.diff(&model).is_empty());
    }

    #[test]
    fn diff_is_deterministic() {
        assert_eq!(base().diff(&revised()), base().diff(&revised()));
    }

    #[test]
    fn diff_reports_each_class() {
        let diff = base().diff(&revised());
        for (class, entries) in [
            ("devices", &diff.devices),
            ("io_points", &diff.io_points),
            ("signals", &diff.signals),
            ("components", &diff.components),
            ("connections", &diff.connections),
        ] {
            for kind in [ChangeKind::Added, ChangeKind::Removed] {
                assert!(
                    entries.iter().any(|entry| entry.change == kind),
                    "{class} reports no {kind} entry: {entries:?}"
                );
            }
        }
    }
}
