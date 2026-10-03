//! Typed composition of the plant-side dynamics document.
//!
//! The plant model is the control contract; the dynamics document
//! beside it is the simulated world's physics — a JSON list of
//! `ProcessElement` declarations `dcs-plant-server --dynamics` merges
//! into the served channel map and `dcs-sim-bus-device --dynamics`
//! merges over its register bank. This module mirrors that serde
//! vocabulary as data — the same spec-mirror convention
//! [`specs`](crate::specs) applies to component-kind descriptors — so
//! a consumer composes the document through compile-checked builders
//! and emits exactly the grammar the merge accepts, while `dcs-build`
//! stays a model producer with no `dcs-sim` dependency.
//!
//! Element ends bind the model's points through the typed handles the
//! model composition returns — [`InPoint`](crate::InPoint) and
//! [`OutPoint`](crate::OutPoint) — narrowed to the kind each end
//! requires: a [`FloatPoint`] accepts only `Float` point handles, a
//! [`BoolPoint`] only `Bool` ones, so a wrongly-kinded reference fails
//! to compile rather than reaching the merge. What the types cannot
//! carry, [`DynamicsBuilder::emit`] checks against the emitted model
//! as named [`DynamicsError`]s: every referenced point declared and
//! channel-bound — a channel-less internal point lives in the scan
//! image, where no simulated field can serve it — every declared
//! parameter inside its element's documented bounds, and no point
//! driven by two elements.
//!
//! ```rust
//! use dcs_build::{Direction, DynamicsBuilder, PlantBuilder, PointId};
//!
//! let mut plant = PlantBuilder::new();
//! let sim = plant.device("sim").id;
//! let inflow_ch = plant.channel::<f64>(sim, "inflow", Direction::In);
//! let net_flow_ch = plant.channel::<f64>(sim, "net-flow", Direction::In);
//! let draw_ch = plant.channel::<f64>(sim, "pump-draw", Direction::In);
//! let cmd_ch = plant.channel::<bool>(sim, "pump-cmd", Direction::Out);
//!
//! let inflow = plant.field_input::<f64>(PointId(12), inflow_ch, false);
//! let net_flow = plant.field_input::<f64>(PointId(13), net_flow_ch, false);
//! let draw = plant.field_input::<f64>(PointId(20), draw_ch, false);
//! let cmd = plant.field_output::<bool>(PointId(100), cmd_ch);
//! let model = plant.build().unwrap();
//!
//! let mut dynamics = DynamicsBuilder::new();
//! dynamics
//!     .bool_flow(cmd, draw, -10.0, 0.0, 0.0)
//!     .flow_sum([inflow, draw], net_flow, 4.0, 4.0);
//! let document = dynamics.emit(&model).unwrap();
//! let json = serde_json::to_string_pretty(&document).unwrap();
//! assert!(json.contains("\"bool_flow\""));
//! ```
//!
//! The emitted document is byte-deterministic — elements serialize in
//! declaration order under the same externally tagged `snake_case`
//! spelling the merge parses — so identical compositions emit
//! identical bytes. Fault injection stays outside the document: it is
//! the plant protocol's runtime verb, not a `ProcessElement` kind the
//! grammar declares.

use crate::{InPoint, OutPoint};
use dcs_core::{PointId, ValueKind};
use dcs_model::PlantModel;
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use std::fmt;

/// A `Float` model point a dynamics element's end binds.
///
/// Obtained by conversion from the [`InPoint<f64>`](crate::InPoint) or
/// [`OutPoint<f64>`](crate::OutPoint) handle a point declaration
/// returns — never from a bare id, so a `Bool` point cannot reach a
/// `Float` end at compile time. [`emit`](DynamicsBuilder::emit) still
/// resolves the carried id against the emitted model: a handle
/// declared by a different composition is a named
/// [`DynamicsError::UnknownPoint`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FloatPoint {
    id: PointId,
}

impl FloatPoint {
    /// The referenced point's id.
    pub fn id(&self) -> PointId {
        self.id
    }
}

impl From<InPoint<f64>> for FloatPoint {
    fn from(point: InPoint<f64>) -> Self {
        Self { id: point.id() }
    }
}

impl From<OutPoint<f64>> for FloatPoint {
    fn from(point: OutPoint<f64>) -> Self {
        Self { id: point.id() }
    }
}

/// A `Bool` model point a dynamics element's gate or contact end
/// binds — [`FloatPoint`]'s mirror for the vocabulary's two `Bool`
/// ends.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BoolPoint {
    id: PointId,
}

impl BoolPoint {
    /// The referenced point's id.
    pub fn id(&self) -> PointId {
        self.id
    }
}

impl From<InPoint<bool>> for BoolPoint {
    fn from(point: InPoint<bool>) -> Self {
        Self { id: point.id() }
    }
}

impl From<OutPoint<bool>> for BoolPoint {
    fn from(point: OutPoint<bool>) -> Self {
        Self { id: point.id() }
    }
}

/// A first-order lag process element: `dy/dt = (u - y) / time_constant`.
///
/// The data mirror of `dcs_sim::FirstOrderLag` — the emitted
/// document's `first_order_lag` kind, field for field.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct FirstOrderLag {
    /// The point read as input `u`; must be a `Float` point.
    pub input: PointId,
    /// The point the element drives; must be a `Float` point.
    pub output: PointId,
    /// The time constant, in the same time units as `step`'s `dt`.
    /// Must be finite and positive.
    pub time_constant: f64,
    /// The output value before the first step. Must be finite.
    pub initial: f64,
}

/// An integrator process element: `dy/dt = u`.
///
/// The data mirror of `dcs_sim::Integrator`.
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
/// The data mirror of `dcs_sim::SecondOrderLag`; the element starts at
/// rest — the output holds `initial` with zero rate until the first
/// step.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct SecondOrderLag {
    /// The point read as input `u`; must be a `Float` point.
    pub input: PointId,
    /// The point the element drives; must be a `Float` point.
    pub output: PointId,
    /// The time constant τ = 1/ωₙ, in the same time units as `step`'s
    /// `dt`. Must be finite and positive.
    pub time_constant: f64,
    /// The damping ratio ζ: below 1 underdamped, 1 critically damped,
    /// above 1 overdamped. Must be finite and positive.
    pub damping_ratio: f64,
    /// The output value before the first step; the initial rate is
    /// zero. Must be finite.
    pub initial: f64,
}

/// A transport-delay process element: `y(t) = u(t - delay)`.
///
/// The data mirror of `dcs_sim::DeadTime`; the delay line is seeded
/// with `initial`, so outputs read `initial` until the line has
/// filled.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct DeadTime {
    /// The point read as input `u`; must be a `Float` point.
    pub input: PointId,
    /// The point the element drives; must be a `Float` point.
    pub output: PointId,
    /// The transport delay, in the same time units as `step`'s `dt`.
    /// Must be finite and positive.
    pub delay: f64,
    /// The output value until the delay line has filled. Must be
    /// finite.
    pub initial: f64,
}

/// A noise process element: `y = u + deviation`, where the deviation
/// is drawn from a seeded pseudo-random generator.
///
/// The data mirror of `dcs_sim::Noise`: each `Good`-input step draws
/// once from a splitmix64 generator seeded by `seed` and outputs
/// `u + amplitude · (2x − 1)`, so the output stays within
/// `u ± amplitude` and identical seeds produce identical deviation
/// sequences.
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
    /// The generator's initial state. Every `u64` is a legal seed.
    pub seed: u64,
    /// The output value before the first step. Must be finite.
    pub initial: f64,
}

/// A Bool-gated flow source: `y = on_rate` while the gate stands,
/// `off_rate` while it is released.
///
/// The data mirror of `dcs_sim::BoolFlow`: the gate is a `Bool` point
/// (typically the controller's `Out` command), the output a `Float`
/// flow — a negative `on_rate` declares a draw on a summed balance.
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
/// The data mirror of `dcs_sim::FlowSum`: `bias` is a declared
/// constant term — a fixed inflow or outflow needing no point of its
/// own — so an empty `inputs` list declares exactly a constant.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FlowSum {
    /// The `Float` points summed into the output, in declaration
    /// order.
    pub inputs: Vec<PointId>,
    /// The point the element drives; must be a `Float` point.
    pub output: PointId,
    /// A constant term added to the sum. Must be finite; documents may
    /// omit it, deserializing as zero.
    #[serde(default)]
    pub bias: f64,
    /// The output value before the first step. Must be finite.
    pub initial: f64,
}

/// A scaling flow source: `y = gain · u`, re-evaluated each step.
///
/// The data mirror of `dcs_sim::ScaledFlow`: the input is a `Float`
/// point (typically the controller's `Out` demand), the output a
/// `Float` rate — a negative `gain` declares a draw on a summed
/// balance.
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
/// The data mirror of `dcs_sim::Threshold`: `on > off` declares a
/// rising (high-side) trip, `on < off` a falling one; between the
/// bounds the contact holds its state, the gap `|on − off|` the
/// declared hysteresis band.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct Threshold {
    /// The point read as input `u`; must be a `Float` point.
    pub input: PointId,
    /// The point the element drives — the contact; must be a `Bool`
    /// point.
    pub output: PointId,
    /// The bound asserting the contact. Must be finite.
    pub on: f64,
    /// The bound releasing the contact. Must be finite and distinct
    /// from `on`.
    pub off: f64,
    /// The contact state before the first `Good` step.
    pub initial: bool,
}

/// One process element in a composed dynamics document — the data
/// mirror of `dcs_sim::ProcessElement`, serializing under the same
/// externally tagged `snake_case` spelling the `dcs-plant-server
/// --dynamics` merge parses.
///
/// Construct through [`DynamicsBuilder`]'s typed methods rather than
/// directly: the builders bind element ends through [`FloatPoint`] /
/// [`BoolPoint`] handles so the required kind is checked at compile
/// time.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DynamicsElement {
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

impl DynamicsElement {
    /// The point the element drives.
    fn output(&self) -> PointId {
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

    /// Every point the element reads or drives, paired with the kind
    /// the end requires: `Bool` for a `bool_flow`'s gate and a
    /// `threshold`'s contact, `Float` for every other end — the rule
    /// the merge's channel-map validation applies.
    fn ends(&self) -> Vec<(PointId, ValueKind)> {
        let float = |point| (point, ValueKind::Float);
        let boolean = |point| (point, ValueKind::Bool);
        match self {
            Self::FirstOrderLag(e) => vec![float(e.input), float(e.output)],
            Self::SecondOrderLag(e) => vec![float(e.input), float(e.output)],
            Self::Integrator(e) => vec![float(e.input), float(e.output)],
            Self::DeadTime(e) => vec![float(e.input), float(e.output)],
            Self::Noise(e) => vec![float(e.input), float(e.output)],
            Self::BoolFlow(e) => vec![boolean(e.input), float(e.output)],
            Self::FlowSum(e) => e
                .inputs
                .iter()
                .map(|&input| float(input))
                .chain([float(e.output)])
                .collect(),
            Self::ScaledFlow(e) => vec![float(e.input), float(e.output)],
            Self::Threshold(e) => vec![float(e.input), boolean(e.output)],
        }
    }

    /// Checks the declared parameters the merge's channel-map
    /// validation enforces: the finite/positive bounds each variant's
    /// fields document, a `threshold`'s distinct `on`/`off` bounds,
    /// and a finite `initial` everywhere it is a `Float`.
    fn check(&self, element: usize) -> Result<(), DynamicsError> {
        let invalid = |parameter: &'static str, rule: &'static str, value: f64| {
            DynamicsError::InvalidParameter {
                element,
                point: self.output(),
                parameter,
                rule,
                value,
            }
        };
        let positive = |value: f64| value.is_finite() && value > 0.0;
        match self {
            Self::FirstOrderLag(e) => {
                if !positive(e.time_constant) {
                    return Err(invalid(
                        "time_constant",
                        "finite and positive",
                        e.time_constant,
                    ));
                }
            }
            Self::SecondOrderLag(e) => {
                if !positive(e.time_constant) {
                    return Err(invalid(
                        "time_constant",
                        "finite and positive",
                        e.time_constant,
                    ));
                }
                if !positive(e.damping_ratio) {
                    return Err(invalid(
                        "damping_ratio",
                        "finite and positive",
                        e.damping_ratio,
                    ));
                }
            }
            Self::DeadTime(e) => {
                if !positive(e.delay) {
                    return Err(invalid("delay", "finite and positive", e.delay));
                }
            }
            Self::Noise(e) => {
                if !e.amplitude.is_finite() || e.amplitude < 0.0 {
                    return Err(invalid("amplitude", "finite and non-negative", e.amplitude));
                }
            }
            Self::BoolFlow(e) => {
                for (parameter, value) in [("on_rate", e.on_rate), ("off_rate", e.off_rate)] {
                    if !value.is_finite() {
                        return Err(invalid(parameter, "finite", value));
                    }
                }
            }
            Self::FlowSum(e) => {
                if !e.bias.is_finite() {
                    return Err(invalid("bias", "finite", e.bias));
                }
            }
            Self::ScaledFlow(e) => {
                if !e.gain.is_finite() {
                    return Err(invalid("gain", "finite", e.gain));
                }
            }
            Self::Threshold(e) => {
                for (parameter, value) in [("on", e.on), ("off", e.off)] {
                    if !value.is_finite() {
                        return Err(invalid(parameter, "finite", value));
                    }
                }
                if e.on == e.off {
                    return Err(DynamicsError::DegenerateThreshold {
                        element,
                        point: e.output,
                        on: e.on,
                        off: e.off,
                    });
                }
            }
            Self::Integrator(_) => {}
        }
        if let Self::Threshold(_) = self {
            // A `Bool` initial is always valid.
        } else {
            let initial = match self {
                Self::FirstOrderLag(e) => e.initial,
                Self::SecondOrderLag(e) => e.initial,
                Self::Integrator(e) => e.initial,
                Self::DeadTime(e) => e.initial,
                Self::Noise(e) => e.initial,
                Self::BoolFlow(e) => e.initial,
                Self::FlowSum(e) => e.initial,
                Self::ScaledFlow(e) => e.initial,
                Self::Threshold(_) => unreachable!(),
            };
            if !initial.is_finite() {
                return Err(invalid("initial", "finite", initial));
            }
        }
        Ok(())
    }
}

/// Why [`DynamicsBuilder::emit`] could not emit a valid document.
///
/// Every variant names the offending element by its position in the
/// composed list — the same way `dcs-plant-server --dynamics` names a
/// rejected element at merge, but raised where the document is
/// authored.
#[derive(Debug, Clone, PartialEq)]
pub enum DynamicsError {
    /// An element references a point the model does not declare — a
    /// stale or foreign handle, or a point id the composition dropped.
    UnknownPoint {
        /// The element's position in the composed list.
        element: usize,
        /// The referenced point.
        point: PointId,
    },
    /// An element references a channel-less internal point. Internal
    /// points live in the controller's scan image, so the simulated
    /// field has no binding an element could read or drive.
    InternalPoint {
        /// The element's position in the composed list.
        element: usize,
        /// The referenced internal point.
        point: PointId,
    },
    /// An element end's required kind differs from the referenced
    /// point's declared kind — reachable only through a handle the
    /// model's own declaration drifted from.
    PointKind {
        /// The element's position in the composed list.
        element: usize,
        /// The referenced point.
        point: PointId,
        /// The kind the end requires.
        expected: ValueKind,
        /// The kind the point declares.
        found: ValueKind,
    },
    /// A declared parameter falls outside the bounds the element's
    /// semantics require — non-finite, or violating the rule `rule`
    /// records.
    InvalidParameter {
        /// The element's position in the composed list.
        element: usize,
        /// The point the element drives.
        point: PointId,
        /// The offending parameter's name.
        parameter: &'static str,
        /// The bound the parameter must satisfy, spelled out.
        rule: &'static str,
        /// The offending value.
        value: f64,
    },
    /// A `threshold` element's `on` and `off` bounds are equal — the
    /// contact would have no hysteresis band, so its release would
    /// chatter on the assert bound.
    DegenerateThreshold {
        /// The element's position in the composed list.
        element: usize,
        /// The point the element drives.
        point: PointId,
        /// The declared `on` bound.
        on: f64,
        /// The declared `off` bound — equal to `on`.
        off: f64,
    },
    /// Two elements drive the same point — the merge's conflicting
    /// driver, caught while the document is authored.
    ConflictingDriver {
        /// The position of the later element in the composed list.
        element: usize,
        /// The contested point.
        point: PointId,
    },
}

impl fmt::Display for DynamicsError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::UnknownPoint { element, point } => write!(
                f,
                "dynamics element {element} references point {}, which the model does not declare",
                point.0
            ),
            Self::InternalPoint { element, point } => write!(
                f,
                "dynamics element {element} references internal point {} — channel-less points live in the scan image, where the simulated field cannot serve them",
                point.0
            ),
            Self::PointKind {
                element,
                point,
                expected,
                found,
            } => write!(
                f,
                "dynamics element {element} requires a {expected:?} point, but point {} is {found:?}",
                point.0
            ),
            Self::InvalidParameter {
                element,
                point,
                parameter,
                rule,
                value,
            } => write!(
                f,
                "dynamics element {element} driving point {} declares {parameter} {value}, which must be {rule}",
                point.0
            ),
            Self::DegenerateThreshold {
                element,
                point,
                on,
                off,
            } => write!(
                f,
                "dynamics element {element} driving point {} declares no hysteresis band: on {on} equals off {off}",
                point.0
            ),
            Self::ConflictingDriver { element, point } => write!(
                f,
                "dynamics element {element} drives point {}, which another element already drives",
                point.0
            ),
        }
    }
}

impl std::error::Error for DynamicsError {}

/// Composes a dynamics document from typed handles — the list of
/// [`DynamicsElement`]s `dcs-plant-server --dynamics` merges.
///
/// Each method declares one element of its kind and returns `&mut
/// Self`, so elements chain in declaration order — the order the
/// served `SimDriver` steps them. Point arguments take the typed
/// handles the model composition returns: `Float` ends take
/// [`FloatPoint`]-convertible handles (`InPoint<f64>` /
/// `OutPoint<f64>`), the `Bool` gate and contact ends take
/// [`BoolPoint`]-convertible ones.
#[derive(Debug, Default)]
pub struct DynamicsBuilder {
    elements: Vec<DynamicsElement>,
}

impl DynamicsBuilder {
    /// An empty composition.
    pub fn new() -> Self {
        Self::default()
    }

    /// Declares a `first_order_lag` element reading `input` and
    /// driving `output`.
    pub fn first_order_lag(
        &mut self,
        input: impl Into<FloatPoint>,
        output: impl Into<FloatPoint>,
        time_constant: f64,
        initial: f64,
    ) -> &mut Self {
        self.elements
            .push(DynamicsElement::FirstOrderLag(FirstOrderLag {
                input: input.into().id,
                output: output.into().id,
                time_constant,
                initial,
            }));
        self
    }

    /// Declares a `second_order_lag` element reading `input` and
    /// driving `output` with the declared time constant and damping
    /// ratio.
    pub fn second_order_lag(
        &mut self,
        input: impl Into<FloatPoint>,
        output: impl Into<FloatPoint>,
        time_constant: f64,
        damping_ratio: f64,
        initial: f64,
    ) -> &mut Self {
        self.elements
            .push(DynamicsElement::SecondOrderLag(SecondOrderLag {
                input: input.into().id,
                output: output.into().id,
                time_constant,
                damping_ratio,
                initial,
            }));
        self
    }

    /// Declares an `integrator` element reading `input` and driving
    /// `output`.
    pub fn integrator(
        &mut self,
        input: impl Into<FloatPoint>,
        output: impl Into<FloatPoint>,
        initial: f64,
    ) -> &mut Self {
        self.elements.push(DynamicsElement::Integrator(Integrator {
            input: input.into().id,
            output: output.into().id,
            initial,
        }));
        self
    }

    /// Declares a `dead_time` element reading `input` and driving
    /// `output` with the declared transport delay.
    pub fn dead_time(
        &mut self,
        input: impl Into<FloatPoint>,
        output: impl Into<FloatPoint>,
        delay: f64,
        initial: f64,
    ) -> &mut Self {
        self.elements.push(DynamicsElement::DeadTime(DeadTime {
            input: input.into().id,
            output: output.into().id,
            delay,
            initial,
        }));
        self
    }

    /// Declares a `noise` element reading `input` and driving `output`
    /// with the declared deviation bound and generator seed.
    pub fn noise(
        &mut self,
        input: impl Into<FloatPoint>,
        output: impl Into<FloatPoint>,
        amplitude: f64,
        seed: u64,
        initial: f64,
    ) -> &mut Self {
        self.elements.push(DynamicsElement::Noise(Noise {
            input: input.into().id,
            output: output.into().id,
            amplitude,
            seed,
            initial,
        }));
        self
    }

    /// Declares a `bool_flow` element gated on `input` — the `Bool`
    /// point, typically the controller's `Out` command — and driving
    /// `output` at `on_rate` while the gate stands, `off_rate` while
    /// released.
    pub fn bool_flow(
        &mut self,
        input: impl Into<BoolPoint>,
        output: impl Into<FloatPoint>,
        on_rate: f64,
        off_rate: f64,
        initial: f64,
    ) -> &mut Self {
        self.elements.push(DynamicsElement::BoolFlow(BoolFlow {
            input: input.into().id,
            output: output.into().id,
            on_rate,
            off_rate,
            initial,
        }));
        self
    }

    /// Declares a `flow_sum` element summing `inputs` — the declared
    /// `Float` points in declaration order — plus `bias` into
    /// `output`.
    pub fn flow_sum(
        &mut self,
        inputs: impl IntoIterator<Item = impl Into<FloatPoint>>,
        output: impl Into<FloatPoint>,
        bias: f64,
        initial: f64,
    ) -> &mut Self {
        self.elements.push(DynamicsElement::FlowSum(FlowSum {
            inputs: inputs.into_iter().map(|input| input.into().id).collect(),
            output: output.into().id,
            bias,
            initial,
        }));
        self
    }

    /// Declares a `scaled_flow` element reading `input` and driving
    /// `output` at `gain · u`.
    pub fn scaled_flow(
        &mut self,
        input: impl Into<FloatPoint>,
        output: impl Into<FloatPoint>,
        gain: f64,
        initial: f64,
    ) -> &mut Self {
        self.elements.push(DynamicsElement::ScaledFlow(ScaledFlow {
            input: input.into().id,
            output: output.into().id,
            gain,
            initial,
        }));
        self
    }

    /// Declares a `threshold` element reading `input` and driving the
    /// `Bool` contact `output`: `on > off` a rising trip, `on < off` a
    /// falling one, the gap between them the hysteresis band.
    pub fn threshold(
        &mut self,
        input: impl Into<FloatPoint>,
        output: impl Into<BoolPoint>,
        on: f64,
        off: f64,
        initial: bool,
    ) -> &mut Self {
        self.elements.push(DynamicsElement::Threshold(Threshold {
            input: input.into().id,
            output: output.into().id,
            on,
            off,
            initial,
        }));
        self
    }

    /// Emits the composed dynamics document — the element list the
    /// `--dynamics` merge accepts — checked against `model`.
    ///
    /// Every end a builder recorded is resolved against the model's
    /// declared `io_points`: an undeclared point is a named
    /// [`DynamicsError::UnknownPoint`], a channel-less internal point
    /// an [`InternalPoint`](DynamicsError::InternalPoint) — the scan
    /// image carries it, so the simulated field has no binding — and a
    /// wrongly-kinded end a [`PointKind`](DynamicsError::PointKind).
    /// Each element's declared parameters are checked against the
    /// bounds the merge's channel-map validation enforces, and a point
    /// driven by two elements reports
    /// [`ConflictingDriver`](DynamicsError::ConflictingDriver). The
    /// returned list serializes under the document's grammar —
    /// `serde_json::to_string_pretty` produces the canonical bytes.
    pub fn emit(&self, model: &PlantModel) -> Result<Vec<DynamicsElement>, DynamicsError> {
        let points: HashMap<PointId, _> = model
            .io_points
            .iter()
            .map(|point| (point.id, point))
            .collect();
        let mut driven = HashSet::new();
        for (element, declaration) in self.elements.iter().enumerate() {
            for (point, expected) in declaration.ends() {
                match points.get(&point) {
                    None => return Err(DynamicsError::UnknownPoint { element, point }),
                    Some(declared) => {
                        if declared.channel.is_none() {
                            return Err(DynamicsError::InternalPoint { element, point });
                        }
                        if declared.value_type != expected {
                            return Err(DynamicsError::PointKind {
                                element,
                                point,
                                expected,
                                found: declared.value_type,
                            });
                        }
                    }
                }
            }
            declaration.check(element)?;
            if !driven.insert(declaration.output()) {
                return Err(DynamicsError::ConflictingDriver {
                    element,
                    point: declaration.output(),
                });
            }
        }
        Ok(self.elements.clone())
    }
}
