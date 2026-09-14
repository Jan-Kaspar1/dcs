//! Component self-description: the metadata contract a monitoring UI
//! renders faceplates from.
//!
//! A [`ComponentDescriptor`] is a component's own account of what it is:
//! its kind and display label, its declared I/O as named
//! [`PortDescriptor`]s carrying direction and value kind plus an
//! optional [`PortRole`] hint, and its tunable [`ParameterDescriptor`]s
//! — enough for a monitoring UI to render a faceplate without per-kind
//! engineering. The runtime's component contract derives a correct
//! default from the component's name and declared I/O; component kinds
//! override it to report their model kind, role hints, and parameter
//! metadata. Like the rest of `dcs-core` the types are
//! serde-serializable, so a UI needs only the shared contracts.

use crate::io::Direction;
use crate::signal::{PointId, Value, ValueKind};
use serde::{Deserialize, Serialize};

/// What a port carries in the process, as a hint to faceplate layout.
///
/// The hint lets a UI pick the port's conventional role — the trended
/// process value, the operator setpoint, the driven output — without
/// knowing the component kind. A [`PortDescriptor`] with no role gives
/// no hint; the UI falls back to direction and value kind alone.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PortRole {
    /// The measured process variable the component acts on.
    ProcessValue,
    /// The operator or cascade target the component regulates toward.
    Setpoint,
    /// The manipulated value the component drives.
    Output,
    /// A reported state — alarm, fault, or mode — rather than a
    /// controlled process value.
    Status,
}

/// One declared I/O port of a component.
///
/// The monitoring-facing form of the runtime's `IoRequirement`: the
/// port's name, its [`Direction`], and its declared [`ValueKind`], plus
/// an optional [`PortRole`] hint. `point` is the serving layer's
/// annotation — the logical point this instance's port is bound to —
/// so a monitoring UI can join the port to live point telemetry and
/// point metadata without re-resolving the plant model's wiring.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PortDescriptor {
    /// The port's name within the component, matching the component's
    /// declared I/O and the plant model's port keys.
    pub name: String,
    /// `In` ports are read by the component; `Out` ports are written.
    pub direction: Direction,
    /// The port's declared value kind.
    pub kind: ValueKind,
    /// What the port carries in the process, when the component says.
    pub role: Option<PortRole>,
    /// The logical point the port is bound to on this instance.
    ///
    /// A bare `describe()` — or any producer describing a kind without
    /// an instance — reports `None`; the serving layer annotates the
    /// descriptor it serves, joining each port to the point its
    /// same-named declared I/O requirement resolved to, so a
    /// [`TelemetrySnapshot`](crate::TelemetrySnapshot) descriptor
    /// always carries the wiring. A port that survives with `None` is
    /// unwired and renders without a live value.
    ///
    /// Optional like `Signal::unit` in the plant model: serialized only
    /// when populated, so payloads predating the field still parse.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub point: Option<PointId>,
}

/// The inclusive bounds a tunable parameter accepts.
///
/// Bounds are [`Value`]s so they carry the parameter's [`ValueKind`]
/// exactly — an `Int` parameter is bounded by `Int`s, a `Float` by
/// `Float`s.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct ParameterRange {
    /// The lowest value the parameter accepts.
    pub min: Value,
    /// The highest value the parameter accepts.
    pub max: Value,
}

impl ParameterRange {
    /// Whether `value` satisfies the inclusive bounds.
    ///
    /// A value whose [`ValueKind`] differs from the bounds' is outside
    /// the range — callers validate the declared kind separately. For
    /// `Float` bounds, `NaN` is outside every range; the
    /// infinite-to-infinite bounds that mean "any finite" therefore
    /// still refuse it.
    pub fn contains(&self, value: Value) -> bool {
        match (self.min, self.max, value) {
            (Value::Bool(_), Value::Bool(_), Value::Bool(_)) => true,
            (Value::Int(min), Value::Int(max), Value::Int(value)) => (min..=max).contains(&value),
            (Value::Float(min), Value::Float(max), Value::Float(value)) => {
                value >= min && value <= max
            }
            _ => false,
        }
    }
}

/// Metadata for one tunable parameter of a component.
///
/// `name` matches the key the plant model's parameter map uses for the
/// parameter; `kind` is the parameter's value kind; `range`, when
/// present, is the inclusive bound the component accepts.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ParameterDescriptor {
    /// The parameter's name, matching the plant model's parameter map.
    pub name: String,
    /// The parameter's value kind.
    pub kind: ValueKind,
    /// The inclusive range the parameter accepts, when bounded.
    pub range: Option<ParameterRange>,
}

/// A component's self-description for the monitoring UI.
///
/// The descriptor is static metadata — the component's identity and
/// interface — not live data: samples and diagnostics ride in the same
/// snapshot's other sections, keyed by the same `name` and listed in
/// the same scan order.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ComponentDescriptor {
    /// The component instance's name — the identity its diagnostics are
    /// keyed by in the [`TelemetrySnapshot`](crate::TelemetrySnapshot).
    pub name: String,
    /// The component's kind: the type identity a UI uses to pick a
    /// faceplate, e.g. the plant model's `kind` string.
    pub kind: String,
    /// The human-facing label to display, typically the instance name.
    pub label: String,
    /// The component's declared I/O as named ports.
    pub ports: Vec<PortDescriptor>,
    /// The component's tunable parameters.
    pub parameters: Vec<ParameterDescriptor>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn descriptor_serde_roundtrip() {
        let descriptor = ComponentDescriptor {
            name: "lic-101".to_string(),
            kind: "pid".to_string(),
            label: "Tank level".to_string(),
            ports: vec![
                PortDescriptor {
                    name: "pv".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: Some(PortRole::ProcessValue),
                    point: Some(PointId(10)),
                },
                PortDescriptor {
                    name: "sp".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: Some(PortRole::Setpoint),
                    point: Some(PointId(11)),
                },
                PortDescriptor {
                    name: "out".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Float,
                    role: Some(PortRole::Output),
                    point: Some(PointId(20)),
                },
                PortDescriptor {
                    name: "tripped".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Status),
                    point: Some(PointId(21)),
                },
                PortDescriptor {
                    name: "unhinted".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Int,
                    role: None,
                    point: None,
                },
            ],
            parameters: vec![
                ParameterDescriptor {
                    name: "kp".to_string(),
                    kind: ValueKind::Float,
                    range: Some(ParameterRange {
                        min: Value::Float(0.0),
                        max: Value::Float(10.0),
                    }),
                },
                ParameterDescriptor {
                    name: "counts".to_string(),
                    kind: ValueKind::Int,
                    range: Some(ParameterRange {
                        min: Value::Int(0),
                        max: Value::Int(100),
                    }),
                },
                ParameterDescriptor {
                    name: "enabled".to_string(),
                    kind: ValueKind::Bool,
                    range: None,
                },
            ],
        };
        let json = serde_json::to_string(&descriptor).unwrap();
        assert_eq!(
            serde_json::from_str::<ComponentDescriptor>(&json).unwrap(),
            descriptor
        );
    }

    #[test]
    fn minimal_descriptor_serde_roundtrip() {
        let descriptor = ComponentDescriptor {
            name: "bare".to_string(),
            kind: "custom".to_string(),
            label: "bare".to_string(),
            ports: Vec::new(),
            parameters: Vec::new(),
        };
        let json = serde_json::to_string(&descriptor).unwrap();
        assert_eq!(
            serde_json::from_str::<ComponentDescriptor>(&json).unwrap(),
            descriptor
        );
    }

    #[test]
    fn range_membership_is_inclusive_and_kind_checked() {
        let ints = ParameterRange {
            min: Value::Int(0),
            max: Value::Int(100),
        };
        assert!(ints.contains(Value::Int(0)));
        assert!(ints.contains(Value::Int(100)));
        assert!(!ints.contains(Value::Int(-1)));
        assert!(!ints.contains(Value::Int(101)));
        assert!(!ints.contains(Value::Float(50.0)));

        let floats = ParameterRange {
            min: Value::Float(f64::NEG_INFINITY),
            max: Value::Float(f64::INFINITY),
        };
        assert!(floats.contains(Value::Float(1e300)));
        assert!(!floats.contains(Value::Float(f64::NAN)));
    }

    #[test]
    fn role_json_uses_snake_case_names() {
        for (role, name) in [
            (PortRole::ProcessValue, "process_value"),
            (PortRole::Setpoint, "setpoint"),
            (PortRole::Output, "output"),
            (PortRole::Status, "status"),
        ] {
            assert_eq!(serde_json::to_string(&role).unwrap(), format!("\"{name}\""));
        }
    }

    #[test]
    fn port_point_is_optional_on_the_wire() {
        // Payloads predating `point` still parse, and `None` serializes
        // back without the key — the bound point is serving-layer
        // annotation, not part of the kind's static interface.
        let port: PortDescriptor = serde_json::from_str(
            r#"{"name":"pv","direction":"in","kind":"float","role":"process_value"}"#,
        )
        .unwrap();
        assert_eq!(port.point, None);
        assert!(!serde_json::to_string(&port).unwrap().contains("point"));
    }
}
