//! The shared convention for a component kind's self-description.
//!
//! Decision 16 gives every registered kind a [`ComponentDescriptor`] the
//! monitoring UI renders a faceplate from. Each `dcs-blocks` kind
//! declares its descriptor beside its `KIND` constant and
//! `from_parameters` constructor by overriding
//! [`Component::describe`](dcs_runtime::Component::describe) with one
//! call to [`component`]: the kind's declared
//! [`IoRequirement`](dcs_runtime::IoRequirement)s become
//! [`PortDescriptor`]s with [`PortRole`] hints attached by name, and
//! [`parameter`] plus the shared [`ParameterRange`] constants describe
//! the key set `from_parameters` reads. Component kinds outside this
//! crate can follow the same convention — the helpers are part of the
//! library, not of any one kind.

use dcs_core::{
    ComponentDescriptor, ParameterDescriptor, ParameterRange, PortDescriptor, PortRole, Value,
    ValueKind,
};
use dcs_runtime::IoRequirement;

/// The inclusive bound a required-finite `Float` parameter accepts:
/// every finite double.
pub const FINITE_F64: ParameterRange = ParameterRange {
    min: Value::Float(-f64::MAX),
    max: Value::Float(f64::MAX),
};

/// The inclusive bound a strictly positive finite `Float` parameter
/// accepts. The lower bound is the smallest positive normal double —
/// the tightest inclusive bound on `x > 0` the type can express.
pub const POSITIVE_F64: ParameterRange = ParameterRange {
    min: Value::Float(f64::MIN_POSITIVE),
    max: Value::Float(f64::MAX),
};

/// The inclusive bound a non-negative finite `Float` parameter accepts.
pub const NONNEGATIVE_F64: ParameterRange = ParameterRange {
    min: Value::Float(0.0),
    max: Value::Float(f64::MAX),
};

/// The inclusive bound a non-negative `Int` parameter accepts.
pub const NONNEGATIVE_INT: ParameterRange = ParameterRange {
    min: Value::Int(0),
    max: Value::Int(i64::MAX),
};

/// The inclusive bound a `Float` parameter in `(0, 1]` accepts — a
/// positive fraction such as a per-tick smoothing constant. The lower
/// bound is the smallest positive normal double — the tightest
/// inclusive bound on `x > 0` the type can express.
pub const FRACTION_F64: ParameterRange = ParameterRange {
    min: Value::Float(f64::MIN_POSITIVE),
    max: Value::Float(1.0),
};

/// A [`ParameterDescriptor`] for one key a kind's `from_parameters`
/// accepts: the parameter's name in the model's parameter map, its value
/// kind, and the inclusive bound it accepts, when bounded.
pub fn parameter(
    name: &str,
    kind: ValueKind,
    range: Option<ParameterRange>,
) -> ParameterDescriptor {
    ParameterDescriptor {
        name: name.to_string(),
        kind,
        range,
    }
}

/// Builds a kind's [`ComponentDescriptor`] from what the component
/// already declares.
///
/// `requirements` is the component's
/// [`io_requirements`](dcs_runtime::Component::io_requirements) result —
/// port names, directions, and value kinds come straight from the wired
/// I/O declaration so the descriptor cannot drift from it. `roles`
/// attaches [`PortRole`] hints by port name; a hint naming a port the
/// component did not declare is a programming error, caught by a debug
/// assertion in tests. `name` is the component instance name — also the
/// faceplate label — and `kind` is the model kind string the registry
/// maps onto `from_parameters`, conventionally the component's `KIND`
/// constant.
pub fn component<S: AsRef<str>>(
    name: &str,
    kind: &str,
    requirements: &[IoRequirement],
    roles: &[(S, PortRole)],
    parameters: Vec<ParameterDescriptor>,
) -> ComponentDescriptor {
    for (port, _) in roles {
        debug_assert!(
            requirements
                .iter()
                .any(|requirement| requirement.name == port.as_ref()),
            "role hint {:?} names no declared port",
            port.as_ref()
        );
    }
    ComponentDescriptor {
        name: name.to_string(),
        kind: kind.to_string(),
        label: name.to_string(),
        ports: requirements
            .iter()
            .map(|requirement| PortDescriptor {
                name: requirement.name.clone(),
                direction: requirement.direction,
                kind: requirement.kind,
                role: roles
                    .iter()
                    .find(|(port, _)| port.as_ref() == requirement.name)
                    .map(|(_, role)| *role),
                // The bound point is the serving layer's annotation:
                // the executor joins it in when the descriptor is
                // served in a snapshot.
                point: None,
            })
            .collect(),
        parameters,
    }
}

/// The parameter list [`AnalogInput`](crate::AnalogInput) and
/// [`AnalogOutput`](crate::AnalogOutput) share: the four required finite
/// scaling bounds `from_parameters` reads.
pub(crate) fn scaling_parameters() -> Vec<ParameterDescriptor> {
    ["raw_min", "raw_max", "eng_min", "eng_max"]
        .into_iter()
        .map(|name| parameter(name, ValueKind::Float, Some(FINITE_F64)))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use dcs_core::{Direction, PointId};

    fn requirements() -> Vec<IoRequirement> {
        vec![
            IoRequirement::input::<f64>("pv", PointId(1)),
            IoRequirement::output::<f64>("out", PointId(2)),
        ]
    }

    #[test]
    fn builds_ports_from_requirements_with_named_roles() {
        let descriptor = component(
            "lic-101",
            "pid",
            &requirements(),
            &[("pv", PortRole::ProcessValue), ("out", PortRole::Output)],
            vec![parameter("kp", ValueKind::Float, Some(FINITE_F64))],
        );
        assert_eq!(descriptor.name, "lic-101");
        assert_eq!(descriptor.kind, "pid");
        assert_eq!(descriptor.label, "lic-101");
        assert_eq!(
            descriptor.ports,
            [
                PortDescriptor {
                    name: "pv".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: Some(PortRole::ProcessValue),
                    point: None,
                },
                PortDescriptor {
                    name: "out".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Float,
                    role: Some(PortRole::Output),
                    point: None,
                },
            ]
        );
        assert_eq!(
            descriptor.parameters,
            [parameter("kp", ValueKind::Float, Some(FINITE_F64))]
        );
    }

    #[test]
    #[cfg_attr(debug_assertions, should_panic)]
    fn undeclared_role_hint_is_a_debug_assertion() {
        component(
            "x",
            "kind",
            &requirements(),
            &[("nope", PortRole::Status)],
            Vec::new(),
        );
    }
}
