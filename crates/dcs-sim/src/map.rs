//! Declarative channel-map types describing the simulated I/O topology a
//! [`SimDriver`](crate::SimDriver) serves.
//!
//! A [`ChannelMap`] is the simulated backend's resolved view of the plant
//! model's I/O mapping: which logical points exist, which simulated device
//! channel backs each point, how written `Out` points loop back onto paired
//! `In` points, and which process elements drive field-side values. Building
//! the map from a model document is the caller's business; `dcs-sim` sees
//! only this resolved form.

use dcs_core::{PointId, Value, ValueKind};
use serde::{Deserialize, Serialize};
use std::collections::hash_map::Entry;
use std::collections::{HashMap, HashSet};
use std::fmt;

/// Data-flow direction of a simulated point.
///
/// Mirrors the plant model's channel direction: `In` carries a value from
/// the simulated field into the controller, `Out` carries a controller
/// command toward the field.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Direction {
    /// Field-to-controller; the controller reads these points.
    In,
    /// Controller-to-field; the controller writes these points.
    Out,
}

impl fmt::Display for Direction {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Direction::In => "in",
            Direction::Out => "out",
        })
    }
}

/// Identifies one channel on one simulated device.
///
/// Channel identity is bookkeeping for diagnostics; the driver is addressed
/// by [`PointId`], never by channel.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct ChannelId {
    /// The simulated device's identifier.
    pub device: u64,
    /// The channel's name on that device.
    pub name: String,
}

/// Binds a logical I/O point to a simulated device channel.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PointBinding {
    /// The logical point the driver serves.
    pub point: PointId,
    /// The channel backing the point.
    pub channel: ChannelId,
    /// Whether the controller reads (`In`) or writes (`Out`) the point.
    ///
    /// Direction is enforced where it carries meaning — [`Loopback`] ends —
    /// but not on [`IoDriver`](dcs_core::IoDriver) access: writes to an `In`
    /// point are how tests and field-side models force input values.
    pub direction: Direction,
    /// The channel's value before the first write or element step. Its
    /// [`Value`] variant is the point's declared type; writes carrying any
    /// other variant fail with
    /// [`IoError::TypeMismatch`](dcs_core::IoError::TypeMismatch).
    pub initial: Value,
}

impl PointBinding {
    /// The point's declared value kind: the variant of `initial`.
    pub fn kind(&self) -> ValueKind {
        self.initial.kind()
    }
}

/// Routes values written to `output` onto `input` at the next step.
///
/// Loopbacks are the model mapping's pairing of an `Out` channel with the
/// `In` channel observing it — the simulated equivalent of a wired-back
/// actuator. The routed sample keeps the written value and the output
/// point's effective quality, so a quality fault injected on the output
/// propagates to the input.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct Loopback {
    /// The `Out` point the controller writes.
    pub output: PointId,
    /// The `In` point observing the written value one step later.
    pub input: PointId,
}

/// A first-order lag process element: `dy/dt = (u - y) / time_constant`.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct FirstOrderLag {
    /// The point read as input `u`; must be a `Float` point.
    pub input: PointId,
    /// The point the element drives; must be a `Float` point.
    pub output: PointId,
    /// The time constant, in the same time units as `step`'s `dt`. Must be
    /// finite and positive.
    pub time_constant: f64,
    /// The output value before the first step. Must be finite.
    pub initial: f64,
}

/// An integrator process element: `dy/dt = u`.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct Integrator {
    /// The point read as input `u`; must be a `Float` point.
    pub input: PointId,
    /// The point the element drives; must be a `Float` point.
    pub output: PointId,
    /// The output value before the first step. Must be finite.
    pub initial: f64,
}

/// A transport-delay process element: `y(t) = u(t - delay)`.
///
/// The element keeps a deterministic delay line — a ring of past
/// `(time, input)` samples advanced by the caller-supplied `dt`, never a
/// wall clock — and outputs the newest sample at or before `t - delay`.
/// The ring is seeded with `initial`, so outputs read `initial` until
/// the line has filled. The realized delay is rounded up to whole steps:
/// an input first read at one step appears at the output
/// `ceil(delay / dt)` steps later, within one `dt` of `delay`.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct DeadTime {
    /// The point read as input `u`; must be a `Float` point.
    pub input: PointId,
    /// The point the element drives; must be a `Float` point.
    pub output: PointId,
    /// The transport delay, in the same time units as `step`'s `dt`.
    /// Must be finite and positive.
    pub delay: f64,
    /// The output value until the delay line has filled. Must be finite.
    pub initial: f64,
}

/// A simulated process element advancing one point's value from another's.
///
/// Elements are stepped in declaration order by
/// [`SimDriver::step`](crate::SimDriver::step), after loopback routing, so
/// an element observes the input value the current step produced.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProcessElement {
    /// A [`FirstOrderLag`].
    FirstOrderLag(FirstOrderLag),
    /// An [`Integrator`].
    Integrator(Integrator),
    /// A [`DeadTime`].
    DeadTime(DeadTime),
}

impl ProcessElement {
    /// The point read as the element's input.
    pub fn input(&self) -> PointId {
        match self {
            Self::FirstOrderLag(element) => element.input,
            Self::Integrator(element) => element.input,
            Self::DeadTime(element) => element.input,
        }
    }

    /// The point the element drives.
    pub fn output(&self) -> PointId {
        match self {
            Self::FirstOrderLag(element) => element.output,
            Self::Integrator(element) => element.output,
            Self::DeadTime(element) => element.output,
        }
    }

    /// The output value before the first step.
    pub fn initial(&self) -> f64 {
        match self {
            Self::FirstOrderLag(element) => element.initial,
            Self::Integrator(element) => element.initial,
            Self::DeadTime(element) => element.initial,
        }
    }
}

/// The simulated I/O topology a [`SimDriver`](crate::SimDriver) serves.
///
/// Construct with the `with_*` builders or by filling the fields directly,
/// then pass to [`SimDriver::new`](crate::SimDriver::new), which runs
/// [`ChannelMap::validate`] and reports the first [`ConfigError`].
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct ChannelMap {
    /// Point bindings; each point id and each channel at most once.
    pub points: Vec<PointBinding>,
    /// Loopback wires from `Out` points to their paired `In` points.
    pub loopbacks: Vec<Loopback>,
    /// Process elements stepped by [`SimDriver::step`](crate::SimDriver::step).
    pub elements: Vec<ProcessElement>,
}

/// Resolves a [`PointId`] to its binding, or reports it unbound.
fn binding<'m>(
    points: &'m HashMap<PointId, &PointBinding>,
    point: PointId,
) -> Result<&'m PointBinding, ConfigError> {
    points
        .get(&point)
        .copied()
        .ok_or(ConfigError::UnknownPoint(point))
}

impl ChannelMap {
    /// An empty map.
    pub fn new() -> Self {
        Self::default()
    }

    /// Adds a point binding.
    pub fn with_point(mut self, point: PointBinding) -> Self {
        self.points.push(point);
        self
    }

    /// Adds a loopback wire.
    pub fn with_loopback(mut self, loopback: Loopback) -> Self {
        self.loopbacks.push(loopback);
        self
    }

    /// Adds a process element.
    pub fn with_element(mut self, element: ProcessElement) -> Self {
        self.elements.push(element);
        self
    }

    /// Checks the map's internal consistency, returning the first
    /// [`ConfigError`] found:
    ///
    /// - point ids and channels are bound at most once;
    /// - every loopback and element names bound points only;
    /// - a loopback runs from an `Out` point to an `In` point of the same
    ///   value kind;
    /// - element ends are `Float` points, `time_constant` and `delay`
    ///   are finite and positive, and `initial` is finite;
    /// - no point is driven by more than one loopback or element.
    pub fn validate(&self) -> Result<(), ConfigError> {
        let mut points = HashMap::with_capacity(self.points.len());
        let mut channels = HashMap::with_capacity(self.points.len());
        for binding_ in &self.points {
            match points.entry(binding_.point) {
                Entry::Occupied(_) => return Err(ConfigError::DuplicatePoint(binding_.point)),
                Entry::Vacant(slot) => {
                    slot.insert(binding_);
                }
            }
            match channels.entry(&binding_.channel) {
                Entry::Occupied(_) => {
                    return Err(ConfigError::DuplicateChannel(binding_.channel.clone()));
                }
                Entry::Vacant(slot) => {
                    slot.insert(binding_.point);
                }
            }
        }

        // Points whose stored sample a loopback or element owns.
        let mut driven = HashSet::new();
        for loopback in &self.loopbacks {
            let output = binding(&points, loopback.output)?;
            let input = binding(&points, loopback.input)?;
            if output.direction != Direction::Out || input.direction != Direction::In {
                return Err(ConfigError::LoopbackDirection {
                    output: loopback.output,
                    output_direction: output.direction,
                    input: loopback.input,
                    input_direction: input.direction,
                });
            }
            if output.kind() != input.kind() {
                return Err(ConfigError::LoopbackKindMismatch {
                    output: loopback.output,
                    output_kind: output.kind(),
                    input: loopback.input,
                    input_kind: input.kind(),
                });
            }
            if !driven.insert(loopback.input) {
                return Err(ConfigError::ConflictingDriver {
                    point: loopback.input,
                });
            }
        }

        for element in &self.elements {
            for point in [element.input(), element.output()] {
                let bound = binding(&points, point)?;
                if bound.kind() != ValueKind::Float {
                    return Err(ConfigError::ElementPointKind {
                        point,
                        kind: bound.kind(),
                    });
                }
            }
            if let ProcessElement::FirstOrderLag(lag) = element
                && (!lag.time_constant.is_finite() || lag.time_constant <= 0.0)
            {
                return Err(ConfigError::InvalidTimeConstant {
                    point: lag.output,
                    value: lag.time_constant,
                });
            }
            if let ProcessElement::DeadTime(dead_time) = element
                && (!dead_time.delay.is_finite() || dead_time.delay <= 0.0)
            {
                return Err(ConfigError::InvalidDelay {
                    point: dead_time.output,
                    value: dead_time.delay,
                });
            }
            if !element.initial().is_finite() {
                return Err(ConfigError::NonFiniteInitial {
                    point: element.output(),
                    value: element.initial(),
                });
            }
            if !driven.insert(element.output()) {
                return Err(ConfigError::ConflictingDriver {
                    point: element.output(),
                });
            }
        }
        Ok(())
    }
}

/// Why a [`ChannelMap`] cannot back a [`SimDriver`](crate::SimDriver).
#[derive(Debug, Clone, PartialEq)]
pub enum ConfigError {
    /// Two bindings declare the same point id.
    DuplicatePoint(PointId),
    /// Two bindings declare the same channel.
    DuplicateChannel(ChannelId),
    /// A loopback or element names a point the map does not bind.
    UnknownPoint(PointId),
    /// A loopback's ends are not an `Out` point feeding an `In` point.
    LoopbackDirection {
        /// The point the loopback sources.
        output: PointId,
        /// The direction `output` actually declares.
        output_direction: Direction,
        /// The point the loopback feeds.
        input: PointId,
        /// The direction `input` actually declares.
        input_direction: Direction,
    },
    /// A loopback's ends carry different value kinds.
    LoopbackKindMismatch {
        /// The point the loopback sources.
        output: PointId,
        /// `output`'s declared kind.
        output_kind: ValueKind,
        /// The point the loopback feeds.
        input: PointId,
        /// `input`'s declared kind.
        input_kind: ValueKind,
    },
    /// An element end is bound to a non-`Float` point.
    ElementPointKind {
        /// The offending point.
        point: PointId,
        /// The kind the point declares.
        kind: ValueKind,
    },
    /// A lag's `time_constant` is not finite and positive.
    InvalidTimeConstant {
        /// The lag's output point.
        point: PointId,
        /// The offending value.
        value: f64,
    },
    /// A dead-time element's `delay` is not finite and positive.
    InvalidDelay {
        /// The element's output point.
        point: PointId,
        /// The offending value.
        value: f64,
    },
    /// An element's `initial` is not finite.
    NonFiniteInitial {
        /// The element's output point.
        point: PointId,
        /// The offending value.
        value: f64,
    },
    /// A point's value is driven by more than one loopback or element.
    ConflictingDriver {
        /// The contested point.
        point: PointId,
    },
}

impl fmt::Display for ConfigError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::DuplicatePoint(point) => {
                write!(f, "point {} is bound more than once", point.0)
            }
            Self::DuplicateChannel(channel) => write!(
                f,
                "channel {:?} on device {} is bound more than once",
                channel.name, channel.device
            ),
            Self::UnknownPoint(point) => {
                write!(f, "point {} is not bound to a channel", point.0)
            }
            Self::LoopbackDirection {
                output,
                output_direction,
                input,
                input_direction,
            } => write!(
                f,
                "loopback {} -> {} requires an out point feeding an in point, found {output_direction} -> {input_direction}",
                output.0, input.0
            ),
            Self::LoopbackKindMismatch {
                output,
                output_kind,
                input,
                input_kind,
            } => write!(
                f,
                "loopback {} -> {} carries {output_kind:?} into {input_kind:?}",
                output.0, input.0
            ),
            Self::ElementPointKind { point, kind } => write!(
                f,
                "process elements drive Float points, but point {} is {kind:?}",
                point.0
            ),
            Self::InvalidTimeConstant { point, value } => write!(
                f,
                "lag driving point {} has non-positive or non-finite time constant {value}",
                point.0
            ),
            Self::InvalidDelay { point, value } => write!(
                f,
                "dead-time element driving point {} has non-positive or non-finite delay {value}",
                point.0
            ),
            Self::NonFiniteInitial { point, value } => write!(
                f,
                "element driving point {} has non-finite initial value {value}",
                point.0
            ),
            Self::ConflictingDriver { point } => write!(
                f,
                "point {} is driven by more than one loopback or element",
                point.0
            ),
        }
    }
}

impl std::error::Error for ConfigError {}
