//! The monitoring write-side contract: operator commands and their receipts.
//!
//! A [`Command`] is an operator action against the running controller:
//! writing a [`Value`] to a [`PointId`], or tuning a named parameter of a
//! component instance. Commands travel over the monitoring transport and
//! apply inside the executor at a documented scan boundary, so the
//! contract must be serde-serializable like the rest of `dcs-core`.
//!
//! [`Command::SetParameter`] addresses the component by the name its
//! descriptor and diagnostics report; the parameter's existence,
//! expected [`ValueKind`], and optional [`ParameterRange`] are declared
//! by the component's [`ParameterDescriptor`](crate::ParameterDescriptor)s.
//!
//! Every submitted command produces a [`CommandReceipt`]: an accepted
//! command reports the scan tick it is scheduled to apply at and later an
//! [`CommandOutcome::Applied`] receipt at that tick; a refused command is
//! [`CommandOutcome::Rejected`] with a [`CommandError`] naming the
//! reason — unknown point, type mismatch, or driver rejection for point
//! commands; unknown component, unknown or unsupported parameter, type
//! mismatch, or out-of-range for parameter commands — and carrying the
//! offending point or component.

use crate::descriptor::ParameterRange;
use crate::io::IoError;
use crate::signal::{PointId, Tick, Value, ValueKind};
use serde::{Deserialize, Serialize};
use std::fmt;

/// An operator command directed at the running controller.
///
/// The variant set is deliberately small; new operator actions extend the
/// enum rather than overloading fields.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Command {
    /// Writes `value` to `point`.
    ///
    /// `kind` is the operator's declaration of the point's value kind — the
    /// controller rejects the command when `value`'s variant differs from
    /// `kind`, or when `kind` differs from the kind the point map declares
    /// for `point`. Matching is strict, never coercing, mirroring the I/O
    /// contract.
    WriteValue {
        /// The logical point to write.
        point: PointId,
        /// The value kind the operator declares for the point.
        kind: ValueKind,
        /// The value to write; its variant must equal `kind`.
        value: Value,
    },
    /// Sets `component`'s writable parameter `name` to `value`.
    ///
    /// `component` is the name the component's descriptor and diagnostics
    /// report. The component's [`ParameterDescriptor`]s are authoritative:
    /// `name` must be declared, `value`'s kind must match the declared
    /// [`ValueKind`], and a declared [`ParameterRange`] bounds the value.
    /// A tuned parameter is run state — a component that accepts one must
    /// carry it in its checkpoint vocabulary so a tracking standby
    /// inherits the runtime tuning.
    SetParameter {
        /// The addressed component instance's name.
        component: String,
        /// The declared parameter's name.
        name: String,
        /// The value to tune to; its variant must equal the declared kind.
        value: Value,
    },
}

impl Command {
    /// The point the command acts on, when it is a point command.
    pub fn point(&self) -> Option<PointId> {
        match self {
            Command::WriteValue { point, .. } => Some(*point),
            Command::SetParameter { .. } => None,
        }
    }
}

/// Why a [`Command`] was rejected. Point-command variants carry the
/// offending [`PointId`]; parameter-command variants carry the offending
/// component name. [`CommandError::point`] and [`CommandError::component`]
/// retrieve each uniformly.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CommandError {
    /// The controller's point map serves no such point.
    UnknownPoint {
        /// The offending point.
        point: PointId,
    },
    /// The command's declared kind or value does not match the point's
    /// declared kind.
    TypeMismatch {
        /// The offending point.
        point: PointId,
        /// The kind the point (or the command's declaration) requires.
        expected: ValueKind,
        /// The value actually supplied.
        found: Value,
    },
    /// The field driver refused the write at the scan boundary.
    DriverRejected {
        /// The offending point.
        point: PointId,
        /// The error the driver returned.
        error: IoError,
    },
    /// The controller hosts no component of this name.
    UnknownComponent {
        /// The offending component name.
        component: String,
    },
    /// The component's descriptor declares no parameter of this name.
    UnknownParameter {
        /// The offending component name.
        component: String,
        /// The rejected parameter name.
        parameter: String,
    },
    /// The component's kind exposes no writable parameters at all.
    UnsupportedParameter {
        /// The offending component name.
        component: String,
        /// The rejected parameter name.
        parameter: String,
    },
    /// The supplied value's kind differs from the parameter's declared
    /// [`ValueKind`].
    ParameterTypeMismatch {
        /// The offending component name.
        component: String,
        /// The rejected parameter name.
        parameter: String,
        /// The kind the parameter's descriptor declares.
        expected: ValueKind,
        /// The value actually supplied.
        found: Value,
    },
    /// The supplied value falls outside the parameter's declared
    /// [`ParameterRange`].
    OutOfRange {
        /// The offending component name.
        component: String,
        /// The rejected parameter name.
        parameter: String,
        /// The value actually supplied.
        value: Value,
        /// The descriptor-declared range the value must satisfy.
        range: ParameterRange,
    },
    /// The component refused the value at the scan boundary — the
    /// descriptor checks passed, but the value breaks an invariant the
    /// kind itself enforces (for example a PID's `out_min >= out_max`).
    InvalidParameter {
        /// The offending component name.
        component: String,
        /// The rejected parameter name.
        parameter: String,
        /// Why the component refused the value.
        detail: String,
    },
}

impl CommandError {
    /// The point the rejection is attributed to, when the rejection is
    /// for a point command.
    pub fn point(&self) -> Option<PointId> {
        match self {
            CommandError::UnknownPoint { point }
            | CommandError::TypeMismatch { point, .. }
            | CommandError::DriverRejected { point, .. } => Some(*point),
            _ => None,
        }
    }

    /// The component the rejection is attributed to, when the rejection
    /// is for a parameter command.
    pub fn component(&self) -> Option<&str> {
        match self {
            CommandError::UnknownComponent { component }
            | CommandError::UnknownParameter { component, .. }
            | CommandError::UnsupportedParameter { component, .. }
            | CommandError::ParameterTypeMismatch { component, .. }
            | CommandError::OutOfRange { component, .. }
            | CommandError::InvalidParameter { component, .. } => Some(component),
            _ => None,
        }
    }
}

impl fmt::Display for CommandError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            CommandError::UnknownPoint { point } => {
                write!(f, "unknown I/O point {point:?}")
            }
            CommandError::TypeMismatch {
                point,
                expected,
                found,
            } => write!(
                f,
                "I/O point {point:?} expects {expected:?}, found {found:?}"
            ),
            CommandError::DriverRejected { point, error } => {
                write!(f, "driver rejected command on I/O point {point:?}: {error}")
            }
            CommandError::UnknownComponent { component } => {
                write!(f, "unknown component {component:?}")
            }
            CommandError::UnknownParameter {
                component,
                parameter,
            } => write!(
                f,
                "component {component:?} declares no parameter {parameter:?}"
            ),
            CommandError::UnsupportedParameter {
                component,
                parameter,
            } => write!(
                f,
                "component {component:?} exposes no writable parameters, refused {parameter:?}"
            ),
            CommandError::ParameterTypeMismatch {
                component,
                parameter,
                expected,
                found,
            } => write!(
                f,
                "parameter {component:?}.{parameter:?} expects {expected:?}, found {found:?}"
            ),
            CommandError::OutOfRange {
                component,
                parameter,
                value,
                range,
            } => write!(
                f,
                "parameter {component:?}.{parameter:?} value {value:?} outside declared range {range:?}"
            ),
            CommandError::InvalidParameter {
                component,
                parameter,
                detail,
            } => write!(
                f,
                "component {component:?} rejected parameter {parameter:?}: {detail}"
            ),
        }
    }
}

impl std::error::Error for CommandError {}

/// What became of a submitted [`Command`].
///
/// Each command produces exactly one receipt. At submission the outcome is
/// `Accepted` — reporting the tick the command is scheduled to apply at —
/// or `Rejected` with a named reason; at the scan boundary an accepted
/// command's receipt is updated to `Applied`, or `Rejected` when the
/// driver or component refuses it.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CommandOutcome {
    /// The command passed validation and is queued to apply at `apply_tick`
    /// — the tick the next scan runs at.
    Accepted {
        /// The scan tick the command is scheduled to apply at.
        apply_tick: Tick,
    },
    /// The command was applied at the recorded scan tick.
    Applied {
        /// The scan tick the command applied at.
        tick: Tick,
    },
    /// The command was refused; `reason` names why.
    Rejected {
        /// The named rejection reason, carrying the offending point or
        /// component.
        reason: CommandError,
    },
}

/// The controller's verdict on one submitted [`Command`].
///
/// Receipts are produced in submission order and live in the executor's
/// receipt log: a queued command's entry reads
/// [`CommandOutcome::Accepted`] until the scan boundary rewrites it
/// `Applied` or `Rejected`. Because commands apply deterministically,
/// identical command and scan sequences produce identical receipt
/// sequences.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CommandReceipt {
    /// The command this receipt answers.
    pub command: Command,
    /// The verdict.
    pub outcome: CommandOutcome,
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::signal::Quality;

    fn write_value() -> Command {
        Command::WriteValue {
            point: PointId(7),
            kind: ValueKind::Float,
            value: Value::Float(2.5),
        }
    }

    fn set_parameter() -> Command {
        Command::SetParameter {
            component: "level-pid".to_string(),
            name: "kp".to_string(),
            value: Value::Float(3.5),
        }
    }

    #[test]
    fn command_serde_roundtrip() {
        for command in [
            write_value(),
            set_parameter(),
            Command::WriteValue {
                point: PointId(1),
                kind: ValueKind::Bool,
                value: Value::Bool(true),
            },
            Command::WriteValue {
                point: PointId(2),
                kind: ValueKind::Int,
                value: Value::Int(-3),
            },
            Command::SetParameter {
                component: "timer".to_string(),
                name: "delay_ticks".to_string(),
                value: Value::Int(40),
            },
        ] {
            let json = serde_json::to_string(&command).unwrap();
            assert_eq!(serde_json::from_str::<Command>(&json).unwrap(), command);
        }
    }

    #[test]
    fn set_parameter_uses_the_documented_wire_shape() {
        let json = serde_json::to_string(&set_parameter()).unwrap();
        assert_eq!(
            json,
            r#"{"set_parameter":{"component":"level-pid","name":"kp","value":{"Float":3.5}}}"#
        );
    }

    #[test]
    fn receipt_serde_roundtrip() {
        for outcome in [
            CommandOutcome::Accepted {
                apply_tick: Tick(4),
            },
            CommandOutcome::Applied { tick: Tick(4) },
            CommandOutcome::Rejected {
                reason: CommandError::UnknownPoint { point: PointId(7) },
            },
            CommandOutcome::Rejected {
                reason: CommandError::TypeMismatch {
                    point: PointId(7),
                    expected: ValueKind::Float,
                    found: Value::Bool(true),
                },
            },
            CommandOutcome::Rejected {
                reason: CommandError::DriverRejected {
                    point: PointId(7),
                    error: IoError::Timeout(PointId(7)),
                },
            },
            CommandOutcome::Rejected {
                reason: CommandError::UnknownComponent {
                    component: "ghost".to_string(),
                },
            },
            CommandOutcome::Rejected {
                reason: CommandError::OutOfRange {
                    component: "level-pid".to_string(),
                    parameter: "kp".to_string(),
                    value: Value::Float(11.0),
                    range: ParameterRange {
                        min: Value::Float(0.0),
                        max: Value::Float(10.0),
                    },
                },
            },
            CommandOutcome::Rejected {
                reason: CommandError::InvalidParameter {
                    component: "level-pid".to_string(),
                    parameter: "out_min".to_string(),
                    detail: "output limits require out_min < out_max".to_string(),
                },
            },
        ] {
            let receipt = CommandReceipt {
                command: set_parameter(),
                outcome,
            };
            let json = serde_json::to_string(&receipt).unwrap();
            assert_eq!(
                serde_json::from_str::<CommandReceipt>(&json).unwrap(),
                receipt
            );
        }
    }

    #[test]
    fn error_carries_offending_point() {
        let point = PointId(9);
        for error in [
            CommandError::UnknownPoint { point },
            CommandError::TypeMismatch {
                point,
                expected: ValueKind::Int,
                found: Value::Float(0.5),
            },
            CommandError::DriverRejected {
                point,
                error: IoError::Disconnected(point),
            },
        ] {
            assert_eq!(error.point(), Some(point));
            assert_eq!(error.component(), None);
            assert!(!error.to_string().is_empty());
        }
    }

    #[test]
    fn error_carries_offending_component() {
        for error in [
            CommandError::UnknownComponent {
                component: "ghost".to_string(),
            },
            CommandError::UnknownParameter {
                component: "level-pid".to_string(),
                parameter: "nope".to_string(),
            },
            CommandError::UnsupportedParameter {
                component: "mux".to_string(),
                parameter: "gain".to_string(),
            },
            CommandError::ParameterTypeMismatch {
                component: "level-pid".to_string(),
                parameter: "kp".to_string(),
                expected: ValueKind::Float,
                found: Value::Bool(true),
            },
            CommandError::OutOfRange {
                component: "level-pid".to_string(),
                parameter: "dt".to_string(),
                value: Value::Float(-1.0),
                range: ParameterRange {
                    min: Value::Float(f64::EPSILON),
                    max: Value::Float(f64::INFINITY),
                },
            },
            CommandError::InvalidParameter {
                component: "level-pid".to_string(),
                parameter: "out_min".to_string(),
                detail: "out_min must be below out_max".to_string(),
            },
        ] {
            assert_eq!(error.point(), None);
            assert!(error.component().is_some());
            assert!(!error.to_string().is_empty());
        }
    }

    #[test]
    fn command_names_its_point() {
        assert_eq!(write_value().point(), Some(PointId(7)));
        assert_eq!(set_parameter().point(), None);
    }

    #[test]
    fn outcome_json_uses_snake_case_names() {
        let receipt = CommandReceipt {
            command: write_value(),
            outcome: CommandOutcome::Rejected {
                reason: CommandError::UnknownPoint { point: PointId(7) },
            },
        };
        let json = serde_json::to_string(&receipt).unwrap();
        assert!(json.contains("\"rejected\""), "{json}");
        assert!(json.contains("\"unknown_point\""), "{json}");
        assert!(json.contains("\"write_value\""), "{json}");
        // `Quality` and other signal types stay untouched by the contract.
        let _ = Quality::Good;
    }
}
