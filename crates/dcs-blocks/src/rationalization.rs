//! The decision-70 rationalization parameter set: `priority`, `class`,
//! and `response_ticks` — the numeric half of the alarm record every
//! alarm kind declares.
//!
//! Carried as descriptor-declared parameters, the codes reach the
//! monitoring surface through the snapshot's parameter section and are
//! tunable and checkpointed like any declared parameter; a missing key
//! is the alarm kind's construction rejection. The managed kinds carry
//! the same three fields inside [`ManagedAlarmConfig`] beside
//! `max_shelve_ticks`; [`Rationalization`] is the unmanaged kinds' copy
//! — the set minus the shelving bound.
//!
//! The record's prose half — the consequence, the required action, and
//! the display/procedure reference — is the model's optional
//! `rationalization` block, which the kinds never see: enforcement lands
//! where kind and instance meet, the registry's
//! `ComponentSpec::require_rationalization`.

use crate::describe;
use crate::params::{self, ParameterError, Parameters};
use dcs_core::{CommandError, ParameterDescriptor, StateError, StateMap, Value, ValueKind};

/// An alarm instance's declared rationalization codes: the decision-70
/// `priority`/`class`/`response_ticks` parameters — required
/// non-negative `Int` data, the same set every alarm kind carries.
///
/// The site vocabulary the codes name — priority levels, class rules —
/// is decision 70's open customer assumption; the model and the
/// component carry the values, not their meaning.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Rationalization {
    /// `priority` — the site priority code.
    pub priority: u64,
    /// `class` — the administrative class code.
    pub class: u64,
    /// `response_ticks` — the allowable operator response time in scans.
    pub response_ticks: u64,
}

impl Rationalization {
    /// The declared parameter set — the descriptors `describe` reports,
    /// in this order, and the order the `dcs-build` specs mirror.
    pub(crate) fn parameters() -> Vec<ParameterDescriptor> {
        vec![
            describe::parameter("priority", ValueKind::Int, Some(describe::NONNEGATIVE_INT)),
            describe::parameter("class", ValueKind::Int, Some(describe::NONNEGATIVE_INT)),
            describe::parameter(
                "response_ticks",
                ValueKind::Int,
                Some(describe::NONNEGATIVE_INT),
            ),
        ]
    }

    /// Reads the three required non-negative `Int` parameters, a missing
    /// or invalid key failing as a [`ParameterError`] naming it.
    pub(crate) fn from_parameters(
        component: &str,
        parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        Ok(Self {
            priority: params::required_u64(component, parameters, "priority")?,
            class: params::required_u64(component, parameters, "class")?,
            response_ticks: params::required_u64(component, parameters, "response_ticks")?,
        })
    }

    /// Retunes one declared parameter, returning the updated record or a
    /// [`CommandError`] naming `component`; an undeclared name is
    /// `UnknownParameter`.
    pub(crate) fn tune(
        self,
        component: &str,
        parameter: &str,
        value: Value,
    ) -> Result<Self, CommandError> {
        let tuned = params::tune_u64(component, parameter, value)?;
        let mut record = self;
        match parameter {
            "priority" => record.priority = tuned,
            "class" => record.class = tuned,
            "response_ticks" => record.response_ticks = tuned,
            _ => return Err(params::unknown_parameter(component, parameter)),
        }
        Ok(record)
    }

    /// Reports the three parameters into `map` — report and checkpoint
    /// share the one vocabulary.
    pub(crate) fn report(&self, map: &mut StateMap) {
        map.insert("priority", Value::Int(self.priority as i64));
        map.insert("class", Value::Int(self.class as i64));
        map.insert("response_ticks", Value::Int(self.response_ticks as i64));
    }

    /// The restore-path mirror of
    /// [`from_parameters`](Self::from_parameters): reads the three `Int`
    /// fields, a negative value failing `InvalidValue` so a rejected
    /// checkpoint changes nothing.
    pub(crate) fn restore(component: &str, state: &StateMap) -> Result<Self, StateError> {
        let read = |field: &str| -> Result<u64, StateError> {
            let value = state.require_i64(component, field)?;
            if value < 0 {
                return Err(StateError::InvalidValue {
                    element: component.to_string(),
                    field: field.to_string(),
                    value: Value::Int(value),
                });
            }
            Ok(value as u64)
        };
        Ok(Self {
            priority: read("priority")?,
            class: read("class")?,
            response_ticks: read("response_ticks")?,
        })
    }
}
