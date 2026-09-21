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
//! [`Command::ForcePoint`] and [`Command::UnforcePoint`] are the forcing
//! pair: a force pins a model-declared writable `In` point to an
//! operator value across scans — the scan image reports it stamped
//! [`Quality::Uncertain`](crate::Quality::Uncertain) with
//! [`QualityReason::Substituted`](crate::QualityReason::Substituted)
//! instead of the driver's read — and the release resumes live reads at
//! the same scan boundary.
//!
//! Every submitted command produces a [`CommandReceipt`]: an accepted
//! command reports the scan tick it is scheduled to apply at and later an
//! [`CommandOutcome::Applied`] receipt at that tick; a refused command is
//! [`CommandOutcome::Rejected`] with a [`CommandError`] naming the
//! reason — unknown point, not model-declared writable, type mismatch,
//! driver rejection, or a write to a force-held internal point for point
//! commands; unknown component, unknown or
//! unsupported parameter, type mismatch, or out-of-range for parameter
//! commands; a full pending-command queue refusing admission for either —
//! and carrying the offending point or component.

use crate::descriptor::ParameterRange;
use crate::io::IoError;
use crate::role::Role;
use crate::signal::{PointId, Tick, Value, ValueKind};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
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
    /// `point` must be one the plant model declares writable — the
    /// model-declared command surface is `In` points only; every `Out`
    /// point and every point whose `io_point` declaration omits or clears
    /// `writable` refuses the write at submission with
    /// [`CommandError::NotWritable`].
    ///
    /// `kind` is the operator's declaration of the point's value kind — the
    /// controller rejects the command when `value`'s variant differs from
    /// `kind`, or when `kind` differs from the kind the point map declares
    /// for `point`. Matching is strict, never coercing, mirroring the I/O
    /// contract.
    ///
    /// A write to a point a [`ForcePoint`](Self::ForcePoint) currently
    /// pins is honored only where a store exists for the release to
    /// observe: a forced *field* point's write still reaches the driver
    /// — the force overrides the image, not the field — while a forced
    /// *internal* point has no store but the image the force owns, so
    /// its write refuses with [`CommandError::PointForced`] rather than
    /// settling `applied` for an effect that could never land.
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
    /// Pins `point` — a model-declared writable `In` point — to `value`
    /// across scans.
    ///
    /// The target surface is exactly [`WriteValue`](Self::WriteValue)'s:
    /// the point must be served, must be an `In` point the plant model
    /// marks `writable`, and `kind`/`value` must match the declared kind
    /// — so a force on an unknown, unwritable, or `Out` point refuses at
    /// submission with the same named reason a write would, and a forced
    /// point stays forced until [`UnforcePoint`](Self::UnforcePoint)
    /// releases it. While the force stands the scan's input image holds
    /// `value` stamped
    /// [`Quality::Uncertain`](crate::Quality::Uncertain)`(`[`QualityReason::Substituted`](crate::QualityReason::Substituted)`)`
    /// — substitution, never false `Good` data — and the driver's read
    /// is bypassed; a force writes nothing to the field. While the
    /// force stands, a [`WriteValue`](Self::WriteValue) to a *field*
    /// point still reaches the driver for the release to observe; the
    /// same write to a force-held *internal* point refuses with
    /// [`CommandError::PointForced`].
    ForcePoint {
        /// The logical point to force.
        point: PointId,
        /// The value kind the operator declares for the point.
        kind: ValueKind,
        /// The forced value; its variant must equal `kind`.
        value: Value,
    },
    /// Releases the force on `point`.
    ///
    /// `point` names the same writable `In` surface
    /// [`ForcePoint`](Self::ForcePoint) does — an unknown, unwritable, or
    /// `Out` point refuses at submission with the named reason — and the
    /// release lands at the same scan boundary every command uses: the
    /// applying scan's input phase already reads the driver again for a
    /// field point, or resumes the held-value rule for an internal one —
    /// its held sample, the force's last stamp, re-stamped
    /// [`Quality::Good`](crate::Quality::Good) rather than left claiming
    /// substituted data.
    /// Releasing a point that is not forced applies as a no-op — the
    /// release is idempotent so an operator never needs the current force
    /// set to issue one.
    UnforcePoint {
        /// The logical point to release.
        point: PointId,
    },
    /// Invokes the kind-declared named command `command` on `component`
    /// with the typed `arguments` the command's declared
    /// [`CommandSpec::request`](crate::CommandSpec) schema checks.
    ///
    /// `component` is the name the component's descriptor and
    /// diagnostics report; `command` is the declared
    /// [`CommandSpec`](crate::CommandSpec)'s `name`; `arguments` is
    /// keyed by the declared [`CommandArgument`](crate::CommandArgument)
    /// names — each supplied value's variant must equal the argument's
    /// declared [`ValueKind`], strict and never coercing like the rest
    /// of the command surface. The named refusals are
    /// [`UnknownComponent`](CommandError::UnknownComponent),
    /// [`UnknownCommand`](CommandError::UnknownCommand),
    /// [`ArgumentTypeMismatch`](CommandError::ArgumentTypeMismatch), and
    /// [`CommandRefused`](CommandError::CommandRefused).
    ///
    /// The invoke surface is additive to — never an alias of — the
    /// writable-point commands: declared `writable` `In` points stay
    /// authoritative for [`WriteValue`](Self::WriteValue) and the force
    /// pair.
    Invoke {
        /// The addressed component instance's name.
        component: String,
        /// The declared command's identity — the `CommandSpec.name`.
        command: String,
        /// The invocation's typed arguments, keyed by declared argument
        /// name.
        arguments: BTreeMap<String, Value>,
    },
}

impl Command {
    /// The point the command acts on, when it is a point command.
    pub fn point(&self) -> Option<PointId> {
        match self {
            Command::WriteValue { point, .. }
            | Command::ForcePoint { point, .. }
            | Command::UnforcePoint { point } => Some(*point),
            Command::SetParameter { .. } | Command::Invoke { .. } => None,
        }
    }

    /// The component the command acts on, when it is a component
    /// command.
    pub fn component(&self) -> Option<&str> {
        match self {
            Command::SetParameter { component, .. } | Command::Invoke { component, .. } => {
                Some(component)
            }
            _ => None,
        }
    }
}

/// Why a [`Command`] was rejected. Point-command variants carry the
/// offending [`PointId`]; parameter-command variants carry the offending
/// component name; [`CommandError::NotActive`] and
/// [`CommandError::QueueFull`] carry the command's target point when it
/// has one. [`CommandError::point`] and [`CommandError::component`]
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
    /// The point is not a declared command target: the plant model marks
    /// which `In` points accept operator writes, and this point's
    /// `io_point` declaration does not — or the point is an `Out` point,
    /// which the command path refuses outright.
    NotWritable {
        /// The offending point.
        point: PointId,
    },
    /// The field driver refused the write at the scan boundary.
    DriverRejected {
        /// The offending point.
        point: PointId,
        /// The error the driver returned.
        error: IoError,
    },
    /// The write targets an image-held (internal) `In` point while a
    /// force stands on it. The force owns the point's image until
    /// release — the input phase re-stamps the forced value every scan —
    /// and the executor keeps no second store for the release to
    /// observe, so nothing the write staged could ever land: settling
    /// `Applied` would journal an effect that never happened. Release
    /// the force, then write. Forced *field* points refuse nothing
    /// here: their write still reaches the driver, which holds it for
    /// the release to observe.
    PointForced {
        /// The offending point.
        point: PointId,
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
    /// The component's kind declares no named command of this identity —
    /// the [`Invoke`](crate::Command::Invoke) surface's
    /// [`UnknownParameter`](Self::UnknownParameter) analogue: the
    /// descriptor's `commands` declare the invoke vocabulary, so a
    /// submission naming an undeclared command is refused at
    /// submission.
    UnknownCommand {
        /// The offending component name.
        component: String,
        /// The rejected command identity.
        command: String,
    },
    /// An [`Invoke`](crate::Command::Invoke) argument's kind differs
    /// from the kind the declared command's
    /// [`request`](crate::CommandSpec) schema declares — strict, never
    /// coercing, like every command-path kind check.
    ArgumentTypeMismatch {
        /// The offending component name.
        component: String,
        /// The invoked command's identity.
        command: String,
        /// The offending argument's declared name.
        argument: String,
        /// The kind the argument's declaration requires.
        expected: ValueKind,
        /// The kind the supplied value carried.
        found: ValueKind,
    },
    /// The component refused the invocation: the declared command
    /// exists and its argument schema validated, but the kind's
    /// availability predicate or the invocation itself reported the
    /// declared refusal `reason` — the outcome
    /// [`KindDeclared`](crate::CommandAvailability::KindDeclared)
    /// availability reports.
    CommandRefused {
        /// The offending component name.
        component: String,
        /// The invoked command's identity.
        command: String,
        /// The kind's declared refusal reason.
        reason: String,
    },
    /// The instance refused the command because of its reported role:
    /// per the monitoring-under-redundancy decision only the peer
    /// reporting [`Role::Active`] accepts commands — a standby or a
    /// mid-transition instance cannot let the write reach the field.
    /// `point` is the command's target, not a point at fault; a command
    /// targeting a component rather than a point carries `None`.
    NotActive {
        /// The point the command targeted, when it targeted a point.
        point: Option<PointId>,
        /// The role the instance reported.
        role: Role,
    },
    /// The pending-command queue was already at its declared capacity
    /// when the command was submitted — the admission refusal of the
    /// bounded command-ingress decision. The command had passed
    /// submission validation, but nothing was queued; it may be
    /// resubmitted once a scan boundary drains pending entries below the
    /// bound. `point` is the command's target, not a point at fault; a
    /// command targeting a component rather than a point carries `None`.
    QueueFull {
        /// The point the command targeted, when it targeted a point.
        point: Option<PointId>,
        /// The pending-command queue's declared capacity.
        capacity: usize,
    },
    /// The run the command applied onto was superseded out of the field:
    /// the shared field's single-writer claim already belonged to another
    /// attachment when the superseded peer's scan boundary settled the
    /// command, so the change reached an image the field never saw and
    /// the demotion reconciles the settlement rather than reporting an
    /// `applied` the surviving owner does not carry. `point` is the
    /// command's target, not a point at fault; a command targeting a
    /// component rather than a point carries `None`. Resubmit to the peer
    /// now owning the field.
    Superseded {
        /// The point the command targeted, when it targeted a point.
        point: Option<PointId>,
    },
}

impl CommandError {
    /// The point the rejection is attributed to, when the rejection is
    /// for a point command or names a point target.
    pub fn point(&self) -> Option<PointId> {
        match self {
            CommandError::UnknownPoint { point }
            | CommandError::NotWritable { point }
            | CommandError::TypeMismatch { point, .. }
            | CommandError::DriverRejected { point, .. }
            | CommandError::PointForced { point } => Some(*point),
            CommandError::NotActive { point, .. }
            | CommandError::QueueFull { point, .. }
            | CommandError::Superseded { point } => *point,
            _ => None,
        }
    }

    /// The component the rejection is attributed to, when the rejection
    /// is for a component-addressed command.
    pub fn component(&self) -> Option<&str> {
        match self {
            CommandError::UnknownComponent { component }
            | CommandError::UnknownParameter { component, .. }
            | CommandError::UnsupportedParameter { component, .. }
            | CommandError::ParameterTypeMismatch { component, .. }
            | CommandError::OutOfRange { component, .. }
            | CommandError::InvalidParameter { component, .. }
            | CommandError::UnknownCommand { component, .. }
            | CommandError::ArgumentTypeMismatch { component, .. }
            | CommandError::CommandRefused { component, .. } => Some(component),
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
            CommandError::NotWritable { point } => {
                write!(f, "I/O point {point:?} is not declared writable")
            }
            CommandError::DriverRejected { point, error } => {
                write!(f, "driver rejected command on I/O point {point:?}: {error}")
            }
            CommandError::PointForced { point } => write!(
                f,
                "I/O point {point:?} is forced: the force owns the image-held \
                 point until release, so the write cannot land; release the \
                 force, then write"
            ),
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
            CommandError::UnknownCommand { component, command } => {
                write!(f, "component {component:?} declares no command {command:?}")
            }
            CommandError::ArgumentTypeMismatch {
                component,
                command,
                argument,
                expected,
                found,
            } => write!(
                f,
                "command {component:?}.{command:?} argument {argument:?} expects {expected:?}, found {found:?}"
            ),
            CommandError::CommandRefused {
                component,
                command,
                reason,
            } => write!(
                f,
                "component {component:?} refused command {command:?}: {reason}"
            ),
            CommandError::NotActive { point, role } => match point {
                Some(point) => write!(
                    f,
                    "command on I/O point {point:?} refused: instance reports role {role}; \
                     commands apply only on the active peer"
                ),
                None => write!(
                    f,
                    "command refused: instance reports role {role}; \
                     commands apply only on the active peer"
                ),
            },
            CommandError::QueueFull { point, capacity } => match point {
                Some(point) => write!(
                    f,
                    "command on I/O point {point:?} refused: the pending-command queue is full \
                     (capacity {capacity}); resubmit once a scan drains it"
                ),
                None => write!(
                    f,
                    "command refused: the pending-command queue is full \
                     (capacity {capacity}); resubmit once a scan drains it"
                ),
            },
            CommandError::Superseded { point } => match point {
                Some(point) => write!(
                    f,
                    "command on I/O point {point:?} superseded: the peer lost the field's \
                     single-writer claim before the command took effect; resubmit to the \
                     peer now owning the field"
                ),
                None => write!(
                    f,
                    "command superseded: the peer lost the field's single-writer claim \
                     before the command took effect; resubmit to the peer now owning \
                     the field"
                ),
            },
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
    /// The actor identity the submitter declared, when it declared one.
    ///
    /// This is the audit-attribution field of the command-path
    /// audit-identity decision: the monitoring submit path stamps it
    /// from the submission, the receipt carries it unchanged through
    /// the scan boundary, and the journaled
    /// [`CommandSettled`](crate::JournalEvent::CommandSettled) entry —
    /// which echoes this receipt — gains the attribution for free. An
    /// absent actor journals as unattributed, never a rejection.
    ///
    /// The identity is *declared*, not authenticated: the receipt proves
    /// what the submitter claimed, not who sent it. Verifying an actor —
    /// e.g. a fronting proxy filling the field from authenticated
    /// context — is deployment machinery the contract deliberately does
    /// not prescribe. Serde-optional: receipts and journaled entries
    /// predating the field deserialize with `None`, and an unattributed
    /// receipt serializes without the key.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub actor: Option<String>,
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

    fn force_point() -> Command {
        Command::ForcePoint {
            point: PointId(7),
            kind: ValueKind::Float,
            value: Value::Float(42.0),
        }
    }

    fn invoke() -> Command {
        Command::Invoke {
            component: "vlv:1".to_string(),
            command: "stroke_test".to_string(),
            arguments: [("ticks".to_string(), Value::Int(30))]
                .into_iter()
                .collect(),
        }
    }

    #[test]
    fn command_serde_roundtrip() {
        for command in [
            write_value(),
            set_parameter(),
            force_point(),
            invoke(),
            Command::Invoke {
                component: "vlv:1".to_string(),
                command: "home".to_string(),
                arguments: BTreeMap::new(),
            },
            Command::UnforcePoint { point: PointId(7) },
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
            r#"{"set_parameter":{"component":"level-pid","name":"kp","value":{"float":3.5}}}"#
        );
    }

    #[test]
    fn force_commands_use_the_documented_wire_shape() {
        let json = serde_json::to_string(&force_point()).unwrap();
        assert_eq!(
            json,
            r#"{"force_point":{"point":7,"kind":"float","value":{"float":42.0}}}"#
        );
        let json = serde_json::to_string(&Command::UnforcePoint { point: PointId(7) }).unwrap();
        assert_eq!(json, r#"{"unforce_point":{"point":7}}"#);
    }

    #[test]
    fn invoke_uses_the_documented_wire_shape() {
        // The invoke variant addresses the component instance by name,
        // the declared command by identity, and carries the typed
        // name→`Value` argument map the `CommandSpec.request` schema
        // checks — snake_case like every variant. The `BTreeMap` keeps
        // the argument order deterministic on the wire.
        let json = serde_json::to_string(&invoke()).unwrap();
        assert_eq!(
            json,
            r#"{"invoke":{"component":"vlv:1","command":"stroke_test","arguments":{"ticks":{"int":30}}}}"#
        );
        let json = serde_json::to_string(&Command::Invoke {
            component: "vlv:1".to_string(),
            command: "home".to_string(),
            arguments: BTreeMap::new(),
        })
        .unwrap();
        assert_eq!(
            json,
            r#"{"invoke":{"component":"vlv:1","command":"home","arguments":{}}}"#
        );
    }

    #[test]
    fn named_refusals_use_the_documented_wire_shapes() {
        for (error, expected) in [
            (
                CommandError::UnknownCommand {
                    component: "vlv:1".to_string(),
                    command: "stroke_test".to_string(),
                },
                r#"{"unknown_command":{"component":"vlv:1","command":"stroke_test"}}"#,
            ),
            (
                CommandError::ArgumentTypeMismatch {
                    component: "vlv:1".to_string(),
                    command: "stroke_test".to_string(),
                    argument: "ticks".to_string(),
                    expected: ValueKind::Int,
                    found: ValueKind::Float,
                },
                r#"{"argument_type_mismatch":{"component":"vlv:1","command":"stroke_test","argument":"ticks","expected":"int","found":"float"}}"#,
            ),
            (
                CommandError::CommandRefused {
                    component: "vlv:1".to_string(),
                    command: "stroke_test".to_string(),
                    reason: "drive not in service".to_string(),
                },
                r#"{"command_refused":{"component":"vlv:1","command":"stroke_test","reason":"drive not in service"}}"#,
            ),
        ] {
            let json = serde_json::to_string(&error).unwrap();
            assert_eq!(json, expected);
            assert_eq!(serde_json::from_str::<CommandError>(&json).unwrap(), error);
        }
    }

    #[test]
    fn payloads_predating_the_new_variants_deserialize() {
        // Command and receipt documents recorded before the invoke
        // surface and its refusals existed carry none of the new
        // variants; they deserialize unchanged.
        assert_eq!(
            serde_json::from_str::<Command>(
                r#"{"write_value":{"point":7,"kind":"float","value":{"float":42.0}}}"#
            )
            .unwrap(),
            Command::WriteValue {
                point: PointId(7),
                kind: ValueKind::Float,
                value: Value::Float(42.0),
            }
        );
        let receipt: CommandReceipt = serde_json::from_str(
            r#"{"command":{"set_parameter":{"component":"level-pid","name":"kp","value":{"float":3.5}}},"outcome":{"rejected":{"reason":{"not_active":{"point":null,"role":"standby"}}}}}"#,
        )
        .unwrap();
        assert_eq!(receipt.command, set_parameter());
    }

    #[test]
    fn invoke_names_its_component() {
        // The invoke variant is component-addressed: `point` is `None`
        // and `component` names the instance, like `SetParameter`.
        let command = invoke();
        assert_eq!(command.point(), None);
        assert_eq!(command.component(), Some("vlv:1"));
        assert_eq!(set_parameter().component(), Some("level-pid"));
        assert_eq!(write_value().component(), None);
    }

    #[test]
    fn named_refusals_carry_the_addressed_component() {
        for error in [
            CommandError::UnknownCommand {
                component: "vlv:1".to_string(),
                command: "stroke_test".to_string(),
            },
            CommandError::ArgumentTypeMismatch {
                component: "vlv:1".to_string(),
                command: "stroke_test".to_string(),
                argument: "ticks".to_string(),
                expected: ValueKind::Int,
                found: ValueKind::Bool,
            },
            CommandError::CommandRefused {
                component: "vlv:1".to_string(),
                command: "stroke_test".to_string(),
                reason: "drive not in service".to_string(),
            },
        ] {
            assert_eq!(error.component(), Some("vlv:1"));
            assert_eq!(error.point(), None);
            assert!(!error.to_string().is_empty());
        }
    }

    #[test]
    fn commands_read_legacy_pascal_case_value_spellings() {
        // A command body written before the value-kind spellings
        // normalized still deserializes through the variant aliases.
        assert_eq!(
            serde_json::from_str::<Command>(
                r#"{"force_point":{"point":7,"kind":"Float","value":{"Float":42.0}}}"#
            )
            .unwrap(),
            force_point()
        );
        assert_eq!(
            serde_json::from_str::<Command>(
                r#"{"set_parameter":{"component":"level-pid","name":"kp","value":{"Float":3.5}}}"#
            )
            .unwrap(),
            set_parameter()
        );
        assert_eq!(
            serde_json::from_str::<Command>(
                r#"{"write_value":{"point":1,"kind":"Bool","value":{"Bool":true}}}"#
            )
            .unwrap(),
            Command::WriteValue {
                point: PointId(1),
                kind: ValueKind::Bool,
                value: Value::Bool(true),
            }
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
                reason: CommandError::NotWritable { point: PointId(7) },
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
                reason: CommandError::PointForced { point: PointId(7) },
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
            CommandOutcome::Rejected {
                reason: CommandError::QueueFull {
                    point: Some(PointId(7)),
                    capacity: 64,
                },
            },
            CommandOutcome::Rejected {
                reason: CommandError::QueueFull {
                    point: None,
                    capacity: 64,
                },
            },
            CommandOutcome::Rejected {
                reason: CommandError::Superseded {
                    point: Some(PointId(7)),
                },
            },
            CommandOutcome::Rejected {
                reason: CommandError::Superseded { point: None },
            },
        ] {
            let receipt = CommandReceipt {
                command: set_parameter(),
                outcome,
                actor: None,
            };
            let json = serde_json::to_string(&receipt).unwrap();
            assert_eq!(
                serde_json::from_str::<CommandReceipt>(&json).unwrap(),
                receipt
            );
        }
    }

    #[test]
    fn receipt_actor_is_serde_optional() {
        let attributed = CommandReceipt {
            command: write_value(),
            outcome: CommandOutcome::Applied { tick: Tick(4) },
            actor: Some("operator-7".to_string()),
        };
        let json = serde_json::to_string(&attributed).unwrap();
        assert!(json.contains("\"actor\":\"operator-7\""), "{json}");
        assert_eq!(
            serde_json::from_str::<CommandReceipt>(&json).unwrap(),
            attributed
        );

        // An unattributed receipt carries no actor key on the wire, and
        // a receipt predating the field still deserializes.
        let bare = CommandReceipt {
            actor: None,
            ..attributed.clone()
        };
        let json = serde_json::to_string(&bare).unwrap();
        assert!(!json.contains("actor"), "{json}");
        assert_eq!(serde_json::from_str::<CommandReceipt>(&json).unwrap(), bare);
        assert_eq!(
            serde_json::from_str::<CommandReceipt>(&json).unwrap().actor,
            None
        );
    }

    #[test]
    fn error_carries_offending_point() {
        let point = PointId(9);
        for error in [
            CommandError::UnknownPoint { point },
            CommandError::NotWritable { point },
            CommandError::TypeMismatch {
                point,
                expected: ValueKind::Int,
                found: Value::Float(0.5),
            },
            CommandError::DriverRejected {
                point,
                error: IoError::Disconnected(point),
            },
            CommandError::PointForced { point },
            CommandError::QueueFull {
                point: Some(point),
                capacity: 4,
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
        assert_eq!(force_point().point(), Some(PointId(7)));
        assert_eq!(
            Command::UnforcePoint { point: PointId(7) }.point(),
            Some(PointId(7))
        );
        assert_eq!(set_parameter().point(), None);
    }

    #[test]
    fn outcome_json_uses_snake_case_names() {
        let receipt = CommandReceipt {
            command: write_value(),
            outcome: CommandOutcome::Rejected {
                reason: CommandError::UnknownPoint { point: PointId(7) },
            },
            actor: None,
        };
        let json = serde_json::to_string(&receipt).unwrap();
        assert!(json.contains("\"rejected\""), "{json}");
        assert!(json.contains("\"unknown_point\""), "{json}");
        assert!(json.contains("\"write_value\""), "{json}");
        // `Quality` and other signal types stay untouched by the contract.
        let _ = Quality::Good;
    }
}
