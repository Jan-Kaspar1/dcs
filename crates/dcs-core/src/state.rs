//! The element state-capture contract: a serializable map of named values.
//!
//! [`StateMap`] is the unit of checkpointing: components and drivers that
//! carry internal state save it as named [`Value`]s and restore it from a
//! map an equivalent element produced. Field names are the element's own
//! vocabulary; restoring a map the element did not produce — missing
//! fields, wrong value kinds, unknown fields — fails with a
//! [`StateError`] naming the element, so a mismatched restore is loud
//! rather than silently divergent.

use crate::signal::{Value, ValueKind};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fmt;

/// The serializable form of one element's internal state: named
/// [`Value`]s.
///
/// An empty map is the stateless case — elements without captured state
/// are unaffected by checkpointing. The typed `require_*`/`optional_*`
/// accessors build [`StateError`]s naming the element for the caller;
/// [`StateMap::ensure_known_fields`] is the strict half of restore,
/// rejecting fields the element never captured.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct StateMap {
    fields: BTreeMap<String, Value>,
}

/// Why a [`StateMap`] could not be restored into an element.
///
/// Every variant names the element that rejected the restore — a
/// component's name or the driver's — and, where a single field is at
/// fault, the field.
#[derive(Debug, Clone, PartialEq)]
pub enum StateError {
    /// The element captured no such field.
    UnknownField {
        /// The element rejecting the restore.
        element: String,
        /// The unrecognized field.
        field: String,
    },
    /// A field the element requires is absent.
    MissingField {
        /// The element rejecting the restore.
        element: String,
        /// The required field.
        field: String,
    },
    /// A field's value kind differs from what the element captured.
    IncompatibleField {
        /// The element rejecting the restore.
        element: String,
        /// The mismatched field.
        field: String,
        /// The kind the element stored.
        expected: ValueKind,
        /// The kind the map carries.
        found: ValueKind,
    },
    /// A field's value is of the right kind but outside the element's
    /// domain — e.g. an enum code that names no variant.
    InvalidValue {
        /// The element rejecting the restore.
        element: String,
        /// The offending field.
        field: String,
        /// The rejected value.
        value: Value,
    },
}

impl StateMap {
    /// An empty map.
    pub fn new() -> Self {
        Self::default()
    }

    /// The number of captured fields.
    pub fn len(&self) -> usize {
        self.fields.len()
    }

    /// Whether no fields are captured.
    pub fn is_empty(&self) -> bool {
        self.fields.is_empty()
    }

    /// Captures `field` as `value`, returning any value it replaces.
    pub fn insert(&mut self, field: impl Into<String>, value: Value) -> Option<Value> {
        self.fields.insert(field.into(), value)
    }

    /// The captured value of `field`, if present.
    pub fn get(&self, field: &str) -> Option<Value> {
        self.fields.get(field).copied()
    }

    /// Iterates the captured fields in name order.
    pub fn iter(&self) -> impl Iterator<Item = (&str, Value)> + '_ {
        self.fields
            .iter()
            .map(|(name, &value)| (name.as_str(), value))
    }

    /// A required field's value: [`StateError::MissingField`] naming
    /// `element` when absent.
    pub fn require(&self, element: &str, field: &str) -> Result<Value, StateError> {
        self.get(field).ok_or_else(|| StateError::MissingField {
            element: element.to_string(),
            field: field.to_string(),
        })
    }

    /// A required field of kind `kind`: absent fields are
    /// [`StateError::MissingField`], other kinds
    /// [`StateError::IncompatibleField`], each naming `element`.
    pub fn require_kind(
        &self,
        element: &str,
        field: &str,
        kind: ValueKind,
    ) -> Result<Value, StateError> {
        let value = self.require(element, field)?;
        if value.kind() != kind {
            return Err(StateError::IncompatibleField {
                element: element.to_string(),
                field: field.to_string(),
                expected: kind,
                found: value.kind(),
            });
        }
        Ok(value)
    }

    /// An optional field of kind `kind`: `Ok(None)` when absent, a named
    /// [`StateError::IncompatibleField`] when present with another kind.
    pub fn optional_kind(
        &self,
        element: &str,
        field: &str,
        kind: ValueKind,
    ) -> Result<Option<Value>, StateError> {
        match self.get(field) {
            None => Ok(None),
            Some(value) if value.kind() == kind => Ok(Some(value)),
            Some(found) => Err(StateError::IncompatibleField {
                element: element.to_string(),
                field: field.to_string(),
                expected: kind,
                found: found.kind(),
            }),
        }
    }

    /// `require_kind` decoded as `f64`.
    pub fn require_f64(&self, element: &str, field: &str) -> Result<f64, StateError> {
        match self.require_kind(element, field, ValueKind::Float)? {
            Value::Float(value) => Ok(value),
            _ => unreachable!("require_kind checked the kind"),
        }
    }

    /// `require_kind` decoded as `i64`.
    pub fn require_i64(&self, element: &str, field: &str) -> Result<i64, StateError> {
        match self.require_kind(element, field, ValueKind::Int)? {
            Value::Int(value) => Ok(value),
            _ => unreachable!("require_kind checked the kind"),
        }
    }

    /// `require_kind` decoded as `bool`.
    pub fn require_bool(&self, element: &str, field: &str) -> Result<bool, StateError> {
        match self.require_kind(element, field, ValueKind::Bool)? {
            Value::Bool(value) => Ok(value),
            _ => unreachable!("require_kind checked the kind"),
        }
    }

    /// `optional_kind` decoded as `f64`.
    pub fn optional_f64(&self, element: &str, field: &str) -> Result<Option<f64>, StateError> {
        match self.optional_kind(element, field, ValueKind::Float)? {
            Some(Value::Float(value)) => Ok(Some(value)),
            None => Ok(None),
            _ => unreachable!("optional_kind checked the kind"),
        }
    }

    /// `optional_kind` decoded as `i64`.
    pub fn optional_i64(&self, element: &str, field: &str) -> Result<Option<i64>, StateError> {
        match self.optional_kind(element, field, ValueKind::Int)? {
            Some(Value::Int(value)) => Ok(Some(value)),
            None => Ok(None),
            _ => unreachable!("optional_kind checked the kind"),
        }
    }

    /// `optional_kind` decoded as `bool`.
    pub fn optional_bool(&self, element: &str, field: &str) -> Result<Option<bool>, StateError> {
        match self.optional_kind(element, field, ValueKind::Bool)? {
            Some(Value::Bool(value)) => Ok(Some(value)),
            None => Ok(None),
            _ => unreachable!("optional_kind checked the kind"),
        }
    }

    /// Errors with [`StateError::UnknownField`] naming `element` when any
    /// field outside `known` is present — the strict half of restore, run
    /// before applying state so a rejected map changes nothing.
    pub fn ensure_known_fields(&self, element: &str, known: &[&str]) -> Result<(), StateError> {
        for field in self.fields.keys() {
            if !known.contains(&field.as_str()) {
                return Err(StateError::UnknownField {
                    element: element.to_string(),
                    field: field.clone(),
                });
            }
        }
        Ok(())
    }

    /// Errors with [`StateError::UnknownField`] naming `element` when any
    /// field is present — the default restore for stateless elements.
    pub fn ensure_empty(&self, element: &str) -> Result<(), StateError> {
        self.ensure_known_fields(element, &[])
    }
}

impl fmt::Display for StateError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::UnknownField { element, field } => {
                write!(f, "element {element:?} captures no field {field:?}")
            }
            Self::MissingField { element, field } => {
                write!(f, "element {element:?} requires field {field:?}")
            }
            Self::IncompatibleField {
                element,
                field,
                expected,
                found,
            } => write!(
                f,
                "element {element:?} field {field:?} expects {expected:?}, found {found:?}"
            ),
            Self::InvalidValue {
                element,
                field,
                value,
            } => write!(
                f,
                "element {element:?} field {field:?} rejects value {value:?}"
            ),
        }
    }
}

impl std::error::Error for StateError {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn state_map_serde_roundtrip() {
        let mut map = StateMap::new();
        map.insert("integrator", Value::Float(1.25));
        map.insert("count", Value::Int(7));
        map.insert("enabled", Value::Bool(true));

        let json = serde_json::to_string(&map).unwrap();
        assert_eq!(serde_json::from_str::<StateMap>(&json).unwrap(), map);
    }

    #[test]
    fn accessors_name_element_and_field() {
        let mut map = StateMap::new();
        map.insert("i", Value::Int(3));

        assert_eq!(map.require_i64("elem", "i").unwrap(), 3);
        assert_eq!(
            map.require_i64("elem", "missing").unwrap_err(),
            StateError::MissingField {
                element: "elem".to_string(),
                field: "missing".to_string(),
            }
        );
        assert_eq!(
            map.require_f64("elem", "i").unwrap_err(),
            StateError::IncompatibleField {
                element: "elem".to_string(),
                field: "i".to_string(),
                expected: ValueKind::Float,
                found: ValueKind::Int,
            }
        );
        assert_eq!(map.optional_f64("elem", "opt").unwrap(), None);
        assert!(matches!(
            map.optional_bool("elem", "i").unwrap_err(),
            StateError::IncompatibleField { .. }
        ));
    }

    #[test]
    fn ensure_known_fields_rejects_unknown() {
        let mut map = StateMap::new();
        map.insert("known", Value::Float(0.0));
        map.ensure_known_fields("elem", &["known", "other"])
            .unwrap();
        assert!(map.ensure_empty("elem").is_err());
        assert_eq!(
            map.ensure_known_fields("elem", &["other"]).unwrap_err(),
            StateError::UnknownField {
                element: "elem".to_string(),
                field: "known".to_string(),
            }
        );
    }
}
