//! The monitoring write-side contract: operator commands and their receipts.
//!
//! A [`Command`] is an operator action against the running controller — for
//! now, writing a [`Value`] to a [`PointId`] while declaring the point's
//! [`ValueKind`]. Commands travel over the monitoring transport and apply
//! inside the executor at a documented scan boundary, so the contract must
//! be serde-serializable like the rest of `dcs-core`.
//!
//! Every submitted command produces a [`CommandReceipt`]: an accepted
//! command reports the scan tick it is scheduled to apply at and later an
//! [`CommandOutcome::Applied`] receipt at that tick; a refused command is
//! [`CommandOutcome::Rejected`] with a [`CommandError`] naming the reason —
//! unknown point, type mismatch, or driver rejection — and carrying the
//! offending point.

use crate::io::IoError;
use crate::signal::{PointId, Tick, Value, ValueKind};
use serde::{Deserialize, Serialize};
use std::fmt;

/// An operator command directed at the running controller.
///
/// The variant set is deliberately small; new operator actions extend the
/// enum rather than overloading fields.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
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
}

impl Command {
    /// The point the command acts on.
    pub fn point(&self) -> PointId {
        match *self {
            Command::WriteValue { point, .. } => point,
        }
    }
}

/// Why a [`Command`] was rejected. Every variant carries the offending
/// [`PointId`]; [`CommandError::point`] retrieves it uniformly.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
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
}

impl CommandError {
    /// The point the rejection is attributed to.
    pub fn point(&self) -> PointId {
        match *self {
            CommandError::UnknownPoint { point }
            | CommandError::TypeMismatch { point, .. }
            | CommandError::DriverRejected { point, .. } => point,
        }
    }
}

impl fmt::Display for CommandError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match *self {
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
        }
    }
}

impl std::error::Error for CommandError {}

/// What became of a submitted [`Command`].
///
/// `Accepted` and `Rejected` are produced at submission; `Accepted` commands
/// produce a second receipt — `Applied`, or `Rejected` when the driver
/// refuses — when they apply at the next scan boundary.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
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
        /// The named rejection reason, carrying the offending point.
        reason: CommandError,
    },
}

/// The controller's verdict on one submitted [`Command`].
///
/// Receipts are produced in queue order: submission yields `Accepted` or
/// `Rejected`, and each accepted command later yields `Applied` or
/// `Rejected` at the scan boundary. Because commands apply deterministically,
/// identical command and scan sequences produce identical receipt sequences.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
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

    #[test]
    fn command_serde_roundtrip() {
        for command in [
            write_value(),
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
        ] {
            let json = serde_json::to_string(&command).unwrap();
            assert_eq!(serde_json::from_str::<Command>(&json).unwrap(), command);
        }
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
        ] {
            let receipt = CommandReceipt {
                command: write_value(),
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
            assert_eq!(error.point(), point);
            assert!(!error.to_string().is_empty());
        }
    }

    #[test]
    fn command_names_its_point() {
        assert_eq!(write_value().point(), PointId(7));
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
