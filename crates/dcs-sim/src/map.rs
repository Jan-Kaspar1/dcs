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
/// The shared [`dcs_core::Direction`], re-exported so the channel map, the
/// plant model, and the [`IoDriver`](dcs_core::IoDriver) boundary name one
/// type: `In` carries a value from the simulated field into the controller,
/// `Out` carries a controller command toward the field.
pub use dcs_core::Direction;

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

/// A second-order lag process element:
/// `time_constant² · y'' + 2·damping_ratio·time_constant·y' + y = u`.
///
/// The parameterization is a time constant τ — the inverse of the
/// undamped natural frequency ωₙ — and a dimensionless damping ratio ζ.
/// ζ < 1 gives an underdamped step response that overshoots its input,
/// ζ = 1 is critically damped, and ζ > 1 gives the sluggish approach a
/// cascade of two first-order lags produces. The element starts at
/// rest: the output holds `initial` with zero rate until the first
/// step. Stepping applies the exact zero-order-hold discretization —
/// the input read at each step is held across it — so the output at
/// every tick equals the continuous-time response at `t = n·dt` to
/// within floating-point round-off.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct SecondOrderLag {
    /// The point read as input `u`; must be a `Float` point.
    pub input: PointId,
    /// The point the element drives; must be a `Float` point.
    pub output: PointId,
    /// The time constant τ = 1/ωₙ, in the same time units as `step`'s
    /// `dt`. Must be finite and positive.
    pub time_constant: f64,
    /// The damping ratio ζ: below 1 underdamped (the step response
    /// overshoots), 1 critically damped, above 1 overdamped. Must be
    /// finite and positive.
    pub damping_ratio: f64,
    /// The output value before the first step; the initial rate is
    /// zero. Must be finite.
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

/// A noise process element: `y = u + deviation`, where the deviation is
/// drawn from a seeded pseudo-random generator.
///
/// Each [`SimDriver::step`](crate::SimDriver::step) with a `Good` input
/// draws once from a splitmix64 generator — `state += golden`, then the
/// standard mix — seeded by `seed` and carried as element state, and
/// outputs `u + amplitude · (2x − 1)` for the draw `x ∈ [0, 1)`, so the
/// output stays within `u ± amplitude`. The draw is one per step and does
/// not scale with `dt`. The generator is a pure function of its carried
/// `u64` state — never a wall clock or OS entropy — so identical seeded
/// runs produce identical deviation sequences and distinct seeds produce
/// distinct ones; state capture and restore continue a run's sequence
/// exactly.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct Noise {
    /// The point read as input `u`; must be a `Float` point.
    pub input: PointId,
    /// The point the element drives; must be a `Float` point.
    pub output: PointId,
    /// The deviation bound: the output stays within `u ± amplitude`.
    /// Must be finite and non-negative; zero passes the input through
    /// unchanged.
    pub amplitude: f64,
    /// The generator's initial state. Every `u64` is a legal seed;
    /// distinct seeds produce distinct deviation sequences.
    pub seed: u64,
    /// The output value before the first step. Must be finite.
    pub initial: f64,
}

/// A Bool-gated flow source: `y = on_rate` while the gate stands,
/// `off_rate` while it is released.
///
/// The element answers an actuator's `Bool` run command with the flow
/// the running actuator produces — a pump's draw on a well, a discharge
/// flow — so a dynamics document can close a simulated station's loop:
/// the command moves the level the controller reads back. The gate is a
/// `Bool` point (typically the controller's `Out` command), the output
/// a `Float` flow: each step the gate reads `true` the output stands at
/// `on_rate`, each step it reads `false` at `off_rate`. A pump's draw
/// is simply a negative `on_rate` — rates are signed, only finiteness
/// is validated. The element holds no dynamics of its own: `dt` does
/// not scale the output — rates, not increments, are what a downstream
/// [`FlowSum`] and [`Integrator`] consume — and `initial` covers only
/// the reads before the first `Good` step.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct BoolFlow {
    /// The point read as the gate; must be a `Bool` point.
    pub input: PointId,
    /// The point the element drives; must be a `Float` point.
    pub output: PointId,
    /// The output while the gate reads `true` — a running actuator's
    /// flow, negative for a draw on a summed balance. Must be finite.
    pub on_rate: f64,
    /// The output while the gate reads `false`. Must be finite.
    pub off_rate: f64,
    /// The output value before the first step. Must be finite.
    pub initial: f64,
}

/// A summing process element: `y = bias + Σ uᵢ` over the declared
/// `Float` inputs.
///
/// The element combines declared flows — an inflow, per-pump draws —
/// into the single net rate an [`Integrator`] consumes, so a document
/// expresses a station's balance without coded physics. Every input is
/// re-read each step in declaration order; `bias` is a declared
/// constant term, so a fixed inflow or outflow needs no point of its
/// own — an empty `inputs` list declares exactly a constant. Like a
/// [`BoolFlow`], the element holds no dynamics: `dt` does not scale the
/// sum and `initial` covers only the reads before the first all-`Good`
/// step.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FlowSum {
    /// The `Float` points summed into the output, in declaration order.
    pub inputs: Vec<PointId>,
    /// The point the element drives; must be a `Float` point.
    pub output: PointId,
    /// A constant term added to the sum — a declared inflow or outflow
    /// with no point of its own. Must be finite; documents may omit it,
    /// deserializing as zero.
    #[serde(default)]
    pub bias: f64,
    /// The output value before the first step. Must be finite.
    pub initial: f64,
}

/// A scaling flow source: `y = gain · u`, re-evaluated each step.
///
/// The element answers an actuator's `Float` demand with the rate the
/// running actuator produces at that demand — a metering pump's
/// discharge at its analog speed command, a chemical tank's drawdown
/// tracking the same demand — so a dynamics document can close a
/// proportionally driven loop `bool_flow`'s fixed on/off rates cannot
/// express. The input is a `Float` point (typically the controller's
/// `Out` demand), the output a `Float` rate: each step the input reads
/// `u`, the output stands at `gain · u`. Gains are signed — a negative
/// `gain` declares a draw on a summed balance — and only finiteness is
/// validated. Like a [`BoolFlow`], the element holds no dynamics: `dt`
/// does not scale the output — rates, not increments, are what a
/// downstream [`FlowSum`] and [`Integrator`] consume — and `initial`
/// covers only the reads before the first `Good` step.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct ScaledFlow {
    /// The point read as input `u`; must be a `Float` point.
    pub input: PointId,
    /// The point the element drives; must be a `Float` point.
    pub output: PointId,
    /// The proportion between input and output — a pump's rate per
    /// unit of demand, negative for a draw on a summed balance. Must
    /// be finite.
    pub gain: f64,
    /// The output value before the first step. Must be finite.
    pub initial: f64,
}

/// A threshold process element: a `Float` input driving a `Bool`
/// contact — the [`BoolFlow`]'s mirror.
///
/// The element is the dynamics vocabulary's Float-to-Bool shape: a
/// level, pressure, or temperature crossing a declared bound asserts a
/// contact, so a plant-side protective or limit function — the
/// Bool contact a [`BoolFlow`]'s emergency draw then gates on — is a
/// declared element rather than a scheduled script. The declared
/// parameterization is a pair of `on`/`off` bounds whose order carries
/// the trip direction: `on > off` declares a rising (high-side) trip —
/// the contact asserts when `u` reaches `on` and releases once `u`
/// falls strictly below `off`; `on < off` declares a falling
/// (low-side) trip — the contact asserts when `u` reaches `on` and
/// releases once `u` rises strictly above `off`. Between the bounds the
/// contact holds its state: the gap `|on - off|` is the declared
/// hysteresis band that keeps a hovering input from chattering the
/// contact. A `NaN` input satisfies no bound and also holds — the
/// comparison rule the alarm vocabulary records. Like a [`BoolFlow`],
/// the element holds no dynamics: `dt` does not scale the decision and
/// `initial` covers only the reads before the first `Good` step.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct Threshold {
    /// The point read as input `u`; must be a `Float` point.
    pub input: PointId,
    /// The point the element drives — the contact; must be a `Bool`
    /// point.
    pub output: PointId,
    /// The bound asserting the contact: `u` reaching `on` in the trip
    /// direction asserts it. Must be finite.
    pub on: f64,
    /// The bound releasing the contact: `u` crossing back strictly past
    /// `off` releases it. Must be finite and distinct from `on` —
    /// `on > off` declares a rising trip, `on < off` a falling one.
    pub off: f64,
    /// The contact state before the first `Good` step.
    pub initial: bool,
}

impl Threshold {
    /// The contact state after reading `u`: asserted when `u` crosses
    /// `on` in the trip direction, released when `u` crosses back
    /// strictly past `off`, `contact` otherwise — inside the
    /// hysteresis band and on a `NaN` input alike.
    ///
    /// `on > off` is the rising trip: `u >= on` asserts, `u < off`
    /// releases. `on < off` is the falling trip: `u <= on` asserts,
    /// `u > off` releases. Validation rejects `on == off`, so the two
    /// arms are exhaustive.
    pub fn evaluate(&self, u: f64, contact: bool) -> bool {
        if self.on > self.off {
            if u >= self.on {
                true
            } else if u < self.off {
                false
            } else {
                contact
            }
        } else if u <= self.on {
            true
        } else if u > self.off {
            false
        } else {
            contact
        }
    }
}

/// A simulated process element advancing one point's value from other
/// points'.
///
/// Elements are stepped in declaration order by
/// [`SimDriver::step`](crate::SimDriver::step), after loopback routing, so
/// an element observes the input values the current step produced. The
/// single-input variants read one point — [`input`](Self::input)
/// reports it; a [`FlowSum`] reads a declared list, the vocabulary's
/// one multi-input shape, and [`inputs`](Self::inputs) covers every
/// variant. Every variant drives a `Float` point except a
/// [`Threshold`], whose contact output is a `Bool` point.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProcessElement {
    /// A [`FirstOrderLag`].
    FirstOrderLag(FirstOrderLag),
    /// A [`SecondOrderLag`].
    SecondOrderLag(SecondOrderLag),
    /// An [`Integrator`].
    Integrator(Integrator),
    /// A [`DeadTime`].
    DeadTime(DeadTime),
    /// A [`Noise`].
    Noise(Noise),
    /// A [`BoolFlow`].
    BoolFlow(BoolFlow),
    /// A [`FlowSum`].
    FlowSum(FlowSum),
    /// A [`ScaledFlow`].
    ScaledFlow(ScaledFlow),
    /// A [`Threshold`].
    Threshold(Threshold),
}

impl ProcessElement {
    /// The point read as the element's input — `Some` for the
    /// single-input variants. A [`FlowSum`] reads a declared list and
    /// has no single input, so it reports `None` here;
    /// [`inputs`](Self::inputs) enumerates every variant's input
    /// points.
    pub fn input(&self) -> Option<PointId> {
        match self {
            Self::FirstOrderLag(element) => Some(element.input),
            Self::SecondOrderLag(element) => Some(element.input),
            Self::Integrator(element) => Some(element.input),
            Self::DeadTime(element) => Some(element.input),
            Self::Noise(element) => Some(element.input),
            Self::BoolFlow(element) => Some(element.input),
            Self::FlowSum(_) => None,
            Self::ScaledFlow(element) => Some(element.input),
            Self::Threshold(element) => Some(element.input),
        }
    }

    /// Every point the element reads, in declaration order: the single
    /// input of the single-input variants, or a [`FlowSum`]'s declared
    /// list.
    pub fn inputs(&self) -> &[PointId] {
        match self {
            Self::FirstOrderLag(element) => std::slice::from_ref(&element.input),
            Self::SecondOrderLag(element) => std::slice::from_ref(&element.input),
            Self::Integrator(element) => std::slice::from_ref(&element.input),
            Self::DeadTime(element) => std::slice::from_ref(&element.input),
            Self::Noise(element) => std::slice::from_ref(&element.input),
            Self::BoolFlow(element) => std::slice::from_ref(&element.input),
            Self::FlowSum(element) => &element.inputs,
            Self::ScaledFlow(element) => std::slice::from_ref(&element.input),
            Self::Threshold(element) => std::slice::from_ref(&element.input),
        }
    }

    /// The point the element drives.
    pub fn output(&self) -> PointId {
        match self {
            Self::FirstOrderLag(element) => element.output,
            Self::SecondOrderLag(element) => element.output,
            Self::Integrator(element) => element.output,
            Self::DeadTime(element) => element.output,
            Self::Noise(element) => element.output,
            Self::BoolFlow(element) => element.output,
            Self::FlowSum(element) => element.output,
            Self::ScaledFlow(element) => element.output,
            Self::Threshold(element) => element.output,
        }
    }

    /// The output value before the first step — a `Float` for every
    /// variant but a [`Threshold`], whose declared initial contact is
    /// a `Bool`.
    pub fn initial(&self) -> Value {
        match self {
            Self::FirstOrderLag(element) => Value::Float(element.initial),
            Self::SecondOrderLag(element) => Value::Float(element.initial),
            Self::Integrator(element) => Value::Float(element.initial),
            Self::DeadTime(element) => Value::Float(element.initial),
            Self::Noise(element) => Value::Float(element.initial),
            Self::BoolFlow(element) => Value::Float(element.initial),
            Self::FlowSum(element) => Value::Float(element.initial),
            Self::ScaledFlow(element) => Value::Float(element.initial),
            Self::Threshold(element) => Value::Bool(element.initial),
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
    /// - point ids and channels are bound at most once, and a `Float`
    ///   point's `initial` is finite — the seed sample must be
    ///   representable like every value the point can later hold;
    /// - every loopback and element names bound points only;
    /// - a loopback runs from an `Out` point to an `In` point of the same
    ///   value kind;
    /// - element ends are `Float` points — except a `bool_flow`'s gate
    ///   input and a `threshold`'s contact output, which must be `Bool`
    ///   points, the requirement attaching to each leg's role so an
    ///   element reading and driving the same point faces both legs'
    ///   checks — `time_constant`, `delay`, and `damping_ratio` are
    ///   finite and positive, `amplitude` is finite and non-negative,
    ///   `on_rate`, `off_rate`, `gain`, and `bias` are finite, a
    ///   `threshold`'s `on`/`off` bounds are finite and distinct —
    ///   their separation the declared hysteresis band — and `initial`
    ///   is finite;
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
            if let Value::Float(initial) = binding_.initial
                && !initial.is_finite()
            {
                return Err(ConfigError::NonFinitePointInitial {
                    point: binding_.point,
                    value: initial,
                });
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
            // Every end must name a bound point of the kind its role
            // requires: Float throughout, except a bool_flow's gate
            // input and a threshold's contact output, which must be
            // Bool points. The requirement attaches to the leg, not to
            // the point's identity, so an element whose input and
            // output name the same point still faces both legs' checks.
            for point in element.inputs().iter().copied() {
                let bound = binding(&points, point)?;
                if matches!(element, ProcessElement::BoolFlow(_)) {
                    if bound.kind() != ValueKind::Bool {
                        return Err(ConfigError::ElementGateKind {
                            point,
                            kind: bound.kind(),
                        });
                    }
                } else if bound.kind() != ValueKind::Float {
                    return Err(ConfigError::ElementPointKind {
                        point,
                        kind: bound.kind(),
                    });
                }
            }
            let point = element.output();
            let bound = binding(&points, point)?;
            if matches!(element, ProcessElement::Threshold(_)) {
                if bound.kind() != ValueKind::Bool {
                    return Err(ConfigError::ElementContactKind {
                        point,
                        kind: bound.kind(),
                    });
                }
            } else if bound.kind() != ValueKind::Float {
                return Err(ConfigError::ElementPointKind {
                    point,
                    kind: bound.kind(),
                });
            }
            if let ProcessElement::BoolFlow(flow) = element {
                for (rate, value) in [("on_rate", flow.on_rate), ("off_rate", flow.off_rate)] {
                    if !value.is_finite() {
                        return Err(ConfigError::InvalidRate {
                            point: flow.output,
                            rate,
                            value,
                        });
                    }
                }
            }
            if let ProcessElement::FlowSum(sum) = element
                && !sum.bias.is_finite()
            {
                return Err(ConfigError::NonFiniteBias {
                    point: sum.output,
                    value: sum.bias,
                });
            }
            if let ProcessElement::ScaledFlow(flow) = element
                && !flow.gain.is_finite()
            {
                return Err(ConfigError::InvalidGain {
                    point: flow.output,
                    value: flow.gain,
                });
            }
            if let ProcessElement::FirstOrderLag(lag) = element
                && (!lag.time_constant.is_finite() || lag.time_constant <= 0.0)
            {
                return Err(ConfigError::InvalidTimeConstant {
                    point: lag.output,
                    value: lag.time_constant,
                });
            }
            if let ProcessElement::SecondOrderLag(lag) = element {
                if !lag.time_constant.is_finite() || lag.time_constant <= 0.0 {
                    return Err(ConfigError::InvalidTimeConstant {
                        point: lag.output,
                        value: lag.time_constant,
                    });
                }
                if !lag.damping_ratio.is_finite() || lag.damping_ratio <= 0.0 {
                    return Err(ConfigError::InvalidDamping {
                        point: lag.output,
                        value: lag.damping_ratio,
                    });
                }
            }
            if let ProcessElement::DeadTime(dead_time) = element
                && (!dead_time.delay.is_finite() || dead_time.delay <= 0.0)
            {
                return Err(ConfigError::InvalidDelay {
                    point: dead_time.output,
                    value: dead_time.delay,
                });
            }
            if let ProcessElement::Noise(noise) = element
                && (!noise.amplitude.is_finite() || noise.amplitude < 0.0)
            {
                return Err(ConfigError::InvalidAmplitude {
                    point: noise.output,
                    value: noise.amplitude,
                });
            }
            if let ProcessElement::Threshold(threshold) = element {
                for (bound, value) in [("on", threshold.on), ("off", threshold.off)] {
                    if !value.is_finite() {
                        return Err(ConfigError::InvalidBound {
                            point: threshold.output,
                            bound,
                            value,
                        });
                    }
                }
                if threshold.on == threshold.off {
                    return Err(ConfigError::NonPositiveBand {
                        point: threshold.output,
                        on: threshold.on,
                        off: threshold.off,
                    });
                }
            }
            // A threshold's Bool initial is always valid; every other
            // variant's is the `Float` the finiteness check covers.
            if let Value::Float(initial) = element.initial()
                && !initial.is_finite()
            {
                return Err(ConfigError::NonFiniteInitial {
                    point: element.output(),
                    value: initial,
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
    /// A `bool_flow` element's gate input is bound to a non-`Bool`
    /// point.
    ElementGateKind {
        /// The offending point.
        point: PointId,
        /// The kind the point declares.
        kind: ValueKind,
    },
    /// A `threshold` element's contact output is bound to a non-`Bool`
    /// point.
    ElementContactKind {
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
    /// A second-order lag's `damping_ratio` is not finite and positive.
    InvalidDamping {
        /// The element's output point.
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
    /// A noise element's `amplitude` is negative or not finite.
    InvalidAmplitude {
        /// The element's output point.
        point: PointId,
        /// The offending value.
        value: f64,
    },
    /// A `bool_flow` element's `on_rate` or `off_rate` is not finite.
    InvalidRate {
        /// The element's output point.
        point: PointId,
        /// Which declared rate is invalid: `"on_rate"` or `"off_rate"`.
        rate: &'static str,
        /// The offending value.
        value: f64,
    },
    /// A `flow_sum` element's `bias` is not finite.
    NonFiniteBias {
        /// The element's output point.
        point: PointId,
        /// The offending value.
        value: f64,
    },
    /// A `scaled_flow` element's `gain` is not finite.
    InvalidGain {
        /// The element's output point.
        point: PointId,
        /// The offending value.
        value: f64,
    },
    /// A `threshold` element's `on` or `off` bound is not finite.
    InvalidBound {
        /// The element's output point.
        point: PointId,
        /// Which declared bound is invalid: `"on"` or `"off"`.
        bound: &'static str,
        /// The offending value.
        value: f64,
    },
    /// A `threshold` element's `on` and `off` bounds are equal — the
    /// contact would have no hysteresis band, so its release would
    /// chatter on the assert bound.
    NonPositiveBand {
        /// The element's output point.
        point: PointId,
        /// The declared `on` bound.
        on: f64,
        /// The declared `off` bound — equal to `on`.
        off: f64,
    },
    /// An element's `initial` is not finite.
    NonFiniteInitial {
        /// The element's output point.
        point: PointId,
        /// The offending value.
        value: f64,
    },
    /// A point binding's `initial` is a non-finite `Float` — the seed
    /// sample must be representable like every value the point can
    /// later hold.
    NonFinitePointInitial {
        /// The bound point.
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
                "process element ends are Float points, but point {} is {kind:?}",
                point.0
            ),
            Self::ElementGateKind { point, kind } => write!(
                f,
                "a bool_flow element's gate must read a Bool point, but point {} is {kind:?}",
                point.0
            ),
            Self::ElementContactKind { point, kind } => write!(
                f,
                "a threshold element's contact must drive a Bool point, but point {} is {kind:?}",
                point.0
            ),
            Self::InvalidTimeConstant { point, value } => write!(
                f,
                "lag driving point {} has non-positive or non-finite time constant {value}",
                point.0
            ),
            Self::InvalidDamping { point, value } => write!(
                f,
                "second-order lag driving point {} has non-positive or non-finite damping ratio {value}",
                point.0
            ),
            Self::InvalidDelay { point, value } => write!(
                f,
                "dead-time element driving point {} has non-positive or non-finite delay {value}",
                point.0
            ),
            Self::InvalidAmplitude { point, value } => write!(
                f,
                "noise element driving point {} has negative or non-finite amplitude {value}",
                point.0
            ),
            Self::InvalidRate { point, rate, value } => write!(
                f,
                "bool_flow element driving point {} has non-finite {rate} {value}",
                point.0
            ),
            Self::NonFiniteBias { point, value } => write!(
                f,
                "flow_sum element driving point {} has non-finite bias {value}",
                point.0
            ),
            Self::InvalidGain { point, value } => write!(
                f,
                "scaled_flow element driving point {} has non-finite gain {value}",
                point.0
            ),
            Self::InvalidBound {
                point,
                bound,
                value,
            } => write!(
                f,
                "threshold element driving point {} has non-finite {bound} bound {value}",
                point.0
            ),
            Self::NonPositiveBand { point, on, off } => write!(
                f,
                "threshold element driving point {} declares no hysteresis band: on {on} equals off {off}",
                point.0
            ),
            Self::NonFiniteInitial { point, value } => write!(
                f,
                "element driving point {} has non-finite initial value {value}",
                point.0
            ),
            Self::NonFinitePointInitial { point, value } => {
                write!(f, "point {} has non-finite initial value {value}", point.0)
            }
            Self::ConflictingDriver { point } => write!(
                f,
                "point {} is driven by more than one loopback or element",
                point.0
            ),
        }
    }
}

impl std::error::Error for ConfigError {}
