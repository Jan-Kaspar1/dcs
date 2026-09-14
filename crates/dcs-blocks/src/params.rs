//! Component parameter maps and the error their constructors report.
//!
//! A plant-model `ComponentInstance` carries a `parameters` map of
//! kind-specific settings keyed by name. Each reusable component documents
//! the parameter names it reads and exposes a `from_parameters`
//! constructor over this map; every failure is a [`ParameterError`] naming
//! the component and the offending parameter. Which logical points a
//! component binds is *not* a parameter: point ids come from the model's
//! connections and are passed to the constructor separately.

use dcs_core::{CommandError, Value, ValueKind};
use std::collections::BTreeMap;
use std::fmt;

/// A component's parameter map: the `parameters` field of a plant-model
/// `ComponentInstance`.
pub type Parameters = BTreeMap<String, Value>;

/// Why a component could not be built from its [`Parameters`].
#[derive(Debug, Clone, PartialEq)]
pub enum ParameterError {
    /// A required parameter is absent from the map.
    Missing {
        /// The component being built.
        component: String,
        /// The missing parameter's name.
        parameter: String,
    },
    /// A parameter is present but unusable: the value carries the wrong
    /// [`Value`] variant, is not losslessly representable as the setting's
    /// type, is non-finite, or violates the parameter's documented
    /// constraint.
    Invalid {
        /// The component being built.
        component: String,
        /// The offending parameter's name.
        parameter: String,
        /// What the value violates.
        detail: String,
    },
}

impl fmt::Display for ParameterError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Missing {
                component,
                parameter,
            } => write!(
                f,
                "component {component:?} requires parameter {parameter:?}"
            ),
            Self::Invalid {
                component,
                parameter,
                detail,
            } => write!(
                f,
                "component {component:?} has invalid parameter {parameter:?}: {detail}"
            ),
        }
    }
}

impl std::error::Error for ParameterError {}

/// An [`ParameterError::Invalid`] for a parameter checked outside a map —
/// e.g. a `new` constructor validating its arguments.
pub(crate) fn invalid(
    component: &str,
    parameter: &str,
    detail: impl Into<String>,
) -> ParameterError {
    ParameterError::Invalid {
        component: component.to_string(),
        parameter: parameter.to_string(),
        detail: detail.into(),
    }
}

/// Reads a required finite `f64` parameter. `Float` and losslessly
/// representable `Int` values are accepted; any other kind is `Invalid`.
pub(crate) fn required_f64(
    component: &str,
    parameters: &Parameters,
    name: &str,
) -> Result<f64, ParameterError> {
    let value = parameters
        .get(name)
        .ok_or_else(|| ParameterError::Missing {
            component: component.to_string(),
            parameter: name.to_string(),
        })?;
    let numeric = match *value {
        Value::Int(_) | Value::Float(_) => f64::try_from(*value).map_err(|_| {
            invalid(
                component,
                name,
                format!("Int value {value:?} is not representable as f64"),
            )
        })?,
        _ => {
            return Err(invalid(
                component,
                name,
                format!("expected a Float or Int parameter, found {value:?}"),
            ));
        }
    };
    if !numeric.is_finite() {
        return Err(invalid(component, name, "must be finite".to_string()));
    }
    Ok(numeric)
}

/// Reads an optional finite `f64` parameter; `None` when absent.
pub(crate) fn optional_f64(
    component: &str,
    parameters: &Parameters,
    name: &str,
) -> Result<Option<f64>, ParameterError> {
    if parameters.contains_key(name) {
        required_f64(component, parameters, name).map(Some)
    } else {
        Ok(None)
    }
}

/// Reads a required `u64` parameter — [`optional_u64`] plus the
/// [`ParameterError::Missing`] case.
pub(crate) fn required_u64(
    component: &str,
    parameters: &Parameters,
    name: &str,
) -> Result<u64, ParameterError> {
    optional_u64(component, parameters, name)?.ok_or_else(|| ParameterError::Missing {
        component: component.to_string(),
        parameter: name.to_string(),
    })
}

/// Reads an optional `u64` parameter; `None` when absent. Non-negative
/// `Int` values and finite, integral, non-negative `Float` values are
/// accepted; anything else is `Invalid`.
pub(crate) fn optional_u64(
    component: &str,
    parameters: &Parameters,
    name: &str,
) -> Result<Option<u64>, ParameterError> {
    match parameters.get(name) {
        None => Ok(None),
        Some(&Value::Int(v)) if v >= 0 => Ok(Some(v as u64)),
        Some(&Value::Float(v))
            if v.is_finite() && v.fract() == 0.0 && v >= 0.0 && v <= u64::MAX as f64 =>
        {
            Ok(Some(v as u64))
        }
        Some(value) => Err(invalid(
            component,
            name,
            format!("expected a non-negative Int or integral Float, found {value:?}"),
        )),
    }
}

/// Reads an optional `bool` parameter; `None` when absent. `Bool` and the
/// `Int` values `0`/`1` are accepted; anything else is `Invalid`.
pub(crate) fn optional_bool(
    component: &str,
    parameters: &Parameters,
    name: &str,
) -> Result<Option<bool>, ParameterError> {
    match parameters.get(name) {
        None => Ok(None),
        Some(value @ (Value::Bool(_) | Value::Int(_))) => {
            bool::try_from(*value).map(Some).map_err(|_| {
                invalid(
                    component,
                    name,
                    format!("expected a Bool or Int 0/1, found {value:?}"),
                )
            })
        }
        Some(value) => Err(invalid(
            component,
            name,
            format!("expected a Bool or Int 0/1, found {value:?}"),
        )),
    }
}

/// A [`CommandError::UnknownParameter`] naming the component — a tuning
/// hook's answer to a name its descriptor does not declare.
pub(crate) fn unknown_parameter(component: &str, parameter: &str) -> CommandError {
    CommandError::UnknownParameter {
        component: component.to_string(),
        parameter: parameter.to_string(),
    }
}

/// A [`CommandError::InvalidParameter`] naming the component — a tuning
/// hook's answer to a value that would break an invariant the kind owns
/// (for example a PID's `out_min >= out_max`).
pub(crate) fn invalid_parameter(
    component: &str,
    parameter: &str,
    detail: impl Into<String>,
) -> CommandError {
    CommandError::InvalidParameter {
        component: component.to_string(),
        parameter: parameter.to_string(),
        detail: detail.into(),
    }
}

/// A [`CommandError::ParameterTypeMismatch`] naming the component — a
/// hook's defense against a value kind the executor's descriptor check
/// did not pre-filter.
fn parameter_mismatch(
    component: &str,
    parameter: &str,
    expected: ValueKind,
    found: Value,
) -> CommandError {
    CommandError::ParameterTypeMismatch {
        component: component.to_string(),
        parameter: parameter.to_string(),
        expected,
        found,
    }
}

/// Decodes the `Float` value a
/// [`Component::apply_parameter`](dcs_runtime::Component::apply_parameter)
/// hook was handed.
pub(crate) fn tune_f64(
    component: &str,
    parameter: &str,
    value: Value,
) -> Result<f64, CommandError> {
    match value {
        Value::Float(tuned) => Ok(tuned),
        _ => Err(parameter_mismatch(
            component,
            parameter,
            ValueKind::Float,
            value,
        )),
    }
}

/// Decodes the non-negative `Int` value a
/// [`Component::apply_parameter`](dcs_runtime::Component::apply_parameter)
/// hook was handed.
pub(crate) fn tune_u64(
    component: &str,
    parameter: &str,
    value: Value,
) -> Result<u64, CommandError> {
    match value {
        Value::Int(tuned) if tuned >= 0 => Ok(tuned as u64),
        Value::Int(_) => Err(invalid_parameter(
            component,
            parameter,
            "must be non-negative",
        )),
        _ => Err(parameter_mismatch(
            component,
            parameter,
            ValueKind::Int,
            value,
        )),
    }
}

/// Decodes the `Bool` value a
/// [`Component::apply_parameter`](dcs_runtime::Component::apply_parameter)
/// hook was handed.
pub(crate) fn tune_bool(
    component: &str,
    parameter: &str,
    value: Value,
) -> Result<bool, CommandError> {
    match value {
        Value::Bool(tuned) => Ok(tuned),
        _ => Err(parameter_mismatch(
            component,
            parameter,
            ValueKind::Bool,
            value,
        )),
    }
}
