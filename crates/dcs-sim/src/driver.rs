//! The [`SimDriver`] backend: point storage, loopback routing, process
//! element stepping, and fault injection behind the [`IoDriver`] boundary.

use crate::map::{ChannelMap, ConfigError, Direction, Loopback, ProcessElement};
use crate::state::{FaultParticipation, capture_points, restore_points};
use dcs_core::{
    IoDriver, IoError, PointId, Quality, QualityReason, Sample, StateError, StateMap, Tick, Value,
    ValueKind,
};
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, VecDeque};
use std::sync::Mutex;

/// A fault injected on a simulated point for diagnostics testing.
///
/// Faults are set per point with [`SimDriver::inject_fault`] and stay active
/// until [`SimDriver::clear_fault`]. A [`Fault::Quality`] fault substitutes
/// the quality readers observe — including loopback routing and process
/// elements — while leaving the stored sample untouched; the error faults
/// make every [`IoDriver`] access fail, standing in for field-device
/// communication failures.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Fault {
    /// Reads observe this quality on the stored sample.
    Quality(Quality),
    /// Accesses fail with [`IoError::Disconnected`].
    Disconnected,
    /// Accesses fail with [`IoError::Timeout`].
    Timeout,
}

impl Fault {
    /// The error accesses return while this fault is active, if any.
    fn io_error(self, point: PointId) -> Option<IoError> {
        match self {
            Self::Disconnected => Some(IoError::Disconnected(point)),
            Self::Timeout => Some(IoError::Timeout(point)),
            Self::Quality(_) => None,
        }
    }
}

/// One bound point's description, as [`SimDriver::points`] reports it.
///
/// The listing a plant server answers point-census requests with: the
/// binding's direction, the sample readers currently observe — the stored
/// value plus any injected quality fault — and the active [`Fault`], if
/// any. A point carrying an error fault still reports its stored sample
/// here; `fault` says why [`IoDriver`] accesses fail instead.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PointInfo {
    /// The bound point.
    pub point: PointId,
    /// Whether the controller reads (`In`) or writes (`Out`) the point.
    pub direction: Direction,
    /// The sample readers currently observe.
    pub sample: Sample,
    /// The fault injected on the point, if any.
    pub fault: Option<Fault>,
}

/// One bound point's runtime state.
struct PointState {
    /// The point's declared kind, taken from the binding's initial value.
    kind: ValueKind,
    /// The binding's direction: whether the controller reads or writes
    /// the point.
    direction: Direction,
    /// The stored sample: the last write, loopback, or element update.
    sample: Sample,
    /// The active injected fault, if any.
    fault: Option<Fault>,
}

impl PointState {
    /// The sample readers and downstream elements observe: the stored
    /// sample with any injected quality fault applied. Error faults are
    /// enforced at the [`IoDriver`] boundary, not here.
    fn effective_sample(&self) -> Sample {
        match self.fault {
            Some(Fault::Quality(quality)) => Sample {
                quality,
                ..self.sample
            },
            _ => self.sample,
        }
    }
}

/// A [`ProcessElement`]'s runtime state.
struct ElementState {
    element: ProcessElement,
    /// The current output `y`; seeded from the element's `initial`.
    /// Unused by a [`ProcessElement::Threshold`], whose state is the
    /// `contact` below.
    y: f64,
    /// The output's rate of change `dy/dt`. Only a
    /// [`ProcessElement::SecondOrderLag`] advances it — the other
    /// elements keep it at its zero seed.
    v: f64,
    /// The generator state a [`ProcessElement::Noise`] draws from,
    /// seeded from the element's `seed`; the other elements hold no
    /// generator and keep it at its zero seed.
    rng: u64,
    /// The delay line a [`ProcessElement::DeadTime`] advances; `None` for
    /// the scalar elements.
    delay_line: Option<DelayLine>,
    /// The standing contact a [`ProcessElement::Threshold`] drives,
    /// seeded from the element's `initial`; the `Float`-output elements
    /// keep it at its `false` seed.
    contact: bool,
}

/// A [`ProcessElement::DeadTime`] element's delay line: a ring of past
/// `(time, input)` samples in `dt` time units, oldest first. Entries a
/// delay lookup can never reach again are dropped from the front as time
/// advances, so the ring stays bounded by the samples inside the delay
/// window.
struct DelayLine {
    /// Simulated time elapsed since the driver was built. Advances only
    /// while the element's input is `Good` — a non-`Good` input freezes
    /// the whole line.
    t: f64,
    /// Past input samples, newest last. Seeded with `(0.0, initial)` so
    /// lookups before the line has filled read `initial`.
    history: VecDeque<(f64, f64)>,
}

impl ElementState {
    /// Advances the element's state one step of `dt` given a `Good`
    /// `Float` input `u`, returning the new output — or `None` when the
    /// step's arithmetic would leave the element non-finite, in which
    /// case nothing is committed: `y`, `v`, the delay line, and the
    /// generator all keep their last finite values.
    ///
    /// Serves the `Float` single-input variants only — a `bool_flow`'s
    /// `Bool` gate, a `flow_sum`'s input list, and a `threshold`'s
    /// `Bool` contact step on `step`'s own paths. The lag uses the exact discretization
    /// `y += (1 - e^{-dt/τ})(u - y)`, stable for every non-negative `dt`;
    /// the integrator uses Euler's `y += u·dt`; the second-order lag
    /// applies the exact zero-order-hold update
    /// [`second_order_transition`] computes, stable for every
    /// non-negative `dt`; the dead-time element pushes `u` onto its
    /// delay line at the new time and outputs the newest sample at or
    /// before `t - delay`; the noise element draws once from its
    /// generator and outputs `u + amplitude · (2x − 1)` for the draw
    /// `x`, staying within `u ± amplitude`; the scaled flow stands at
    /// `gain · u` — a rate, not an increment, so `dt` does not scale
    /// it. All are pure functions of their arguments and stored state,
    /// keeping stepping deterministic.
    ///
    /// The `None` verdict is what keeps a stored sample representable:
    /// finite inputs and a finite `dt` can still overflow — an
    /// integrator wound past the `f64` range by a large `u·dt`, a
    /// scaled flow or lag difference saturating — and a committed
    /// non-finite accumulator would both serve values no JSON contract
    /// can spell and stay non-finite under every later input. Holding
    /// the last finite state instead leaves the element recoverable:
    /// the first step whose arithmetic lands finite resumes it.
    fn advance(&mut self, u: f64, dt: f64) -> Option<f64> {
        match &self.element {
            ProcessElement::FirstOrderLag(element) => {
                let y = self.y + (1.0 - (-dt / element.time_constant).exp()) * (u - self.y);
                y.is_finite().then_some(y)
            }
            ProcessElement::SecondOrderLag(element) => {
                // The held input shifts the equilibrium: the deviation
                // state (e = y - u, v = dy/dt) evolves by e^{A dt}.
                let (e, v) = second_order_transition(
                    self.y - u,
                    self.v,
                    1.0 / element.time_constant,
                    element.damping_ratio,
                    dt,
                );
                let y = u + e;
                if y.is_finite() && v.is_finite() {
                    self.v = v;
                    Some(y)
                } else {
                    None
                }
            }
            ProcessElement::Integrator(_) => {
                let y = self.y + u * dt;
                y.is_finite().then_some(y)
            }
            ProcessElement::DeadTime(element) => {
                // Constructed in `SimDriver::new` for every dead-time element.
                let line = self.delay_line.as_mut().unwrap();
                // A `dt` that overflows the line's clock is refused
                // before anything moves: a non-finite `t` would drain
                // the whole history and never recover.
                let t = line.t + dt;
                if !t.is_finite() {
                    return None;
                }
                line.t = t;
                line.history.push_back((t, u));
                // The newest sample at or before `t - delay`. The
                // tolerance absorbs float error accumulated in the
                // stored times so a sample recorded exactly on the
                // boundary is delivered on the expected step.
                let target = t - element.delay;
                let tolerance = 1e-9 * t.abs().max(1.0);
                while line.history.len() > 1 && line.history[1].0 <= target + tolerance {
                    line.history.pop_front();
                }
                Some(line.history[0].1)
            }
            ProcessElement::Noise(element) => {
                // Draw into a scratch state so a refused step leaves the
                // generator where it stood — the same freeze a non-Good
                // input applies.
                let mut rng = self.rng;
                let x = splitmix64_next(&mut rng);
                let y = u + element.amplitude * (2.0 * x - 1.0);
                if y.is_finite() {
                    self.rng = rng;
                    Some(y)
                } else {
                    None
                }
            }
            ProcessElement::ScaledFlow(element) => {
                let y = element.gain * u;
                y.is_finite().then_some(y)
            }
            ProcessElement::BoolFlow(_)
            | ProcessElement::FlowSum(_)
            | ProcessElement::Threshold(_) => {
                unreachable!(
                    "bool_flow, flow_sum, and threshold step on SimDriver::step's own paths"
                )
            }
        }
    }
}

/// The next draw from a splitmix64 generator, scaled into `[0, 1)`.
///
/// The recorded generator [`ProcessElement::Noise`] runs: the state
/// advances by the golden-ratio increment `0x9E3779B97F4A7C15`, then the
/// standard splitmix64 mix scrambles it — a pure function of the `u64`
/// state alone, so a carried state replays the identical sequence and
/// nothing reads a wall clock or OS entropy. The mix's top 53 bits scale
/// into `[0, 1)`; every IEEE-754 double in the range is a possible draw.
fn splitmix64_next(state: &mut u64) -> f64 {
    *state = state.wrapping_add(0x9E37_79B9_7F4A_7C15);
    let mut z = *state;
    z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
    z ^= z >> 31;
    (z >> 11) as f64 * (1.0 / 9_007_199_254_740_992.0)
}

/// A [`ProcessElement::SecondOrderLag`]'s exact step transition.
///
/// Evolves the deviation state `(e, v)` — output minus the held input,
/// and the output rate — over one step of `dt` by `e^{A dt}` for
/// `A = [[0, 1], [-ω², -2ζω]]`, the zero-order-hold-exact
/// discretization of `τ² y'' + 2ζτ y' + y = u` with `ω = 1/τ`. A
/// damping ratio within `1e-9` of critical (by `ζ² - 1`) steps with the
/// critically damped coefficients; the three branches are continuous
/// there, so the hand-off is invisible at tick resolution.
fn second_order_transition(e: f64, v: f64, omega: f64, zeta: f64, dt: f64) -> (f64, f64) {
    let sigma = zeta * omega;
    let decay = (-sigma * dt).exp();
    let deviation = zeta * zeta - 1.0;
    if deviation.abs() < 1e-9 {
        // Critically damped: Φ = e^{-ωdt} [[1 + ωdt, dt], [-ω²dt, 1 - ωdt]].
        let x = omega * dt;
        (
            decay * ((1.0 + x) * e + dt * v),
            decay * (-omega * x * e + (1.0 - x) * v),
        )
    } else if deviation < 0.0 {
        // Underdamped, ωd = ω√(1-ζ²): Φ = e^{-σdt}
        // [[c + (σ/ωd)s, s/ωd], [-(ω²/ωd)s, c - (σ/ωd)s]].
        let wd = omega * (-deviation).sqrt();
        let (s, c) = (wd * dt).sin_cos();
        let k = sigma / wd * s;
        (
            decay * ((c + k) * e + s / wd * v),
            decay * (-omega * omega / wd * s * e + (c - k) * v),
        )
    } else {
        // Overdamped, β = ω√(ζ²-1): the cosh/sinh analog of the
        // underdamped coefficients.
        let beta = omega * deviation.sqrt();
        let sh = (beta * dt).sinh();
        let ch = (beta * dt).cosh();
        let k = sigma / beta * sh;
        (
            decay * ((ch + k) * e + sh / beta * v),
            decay * (-omega * omega / beta * sh * e + (ch - k) * v),
        )
    }
}

/// Everything behind the driver's `Mutex` — `SimDriver` is `Sync`, so an
/// executor running on one thread can share it with a monitoring server or
/// fault injectors on another.
struct State {
    points: HashMap<PointId, PointState>,
    loopbacks: Vec<Loopback>,
    elements: Vec<ElementState>,
    tick: Tick,
}

/// The element name [`StateError`]s from the driver's state-capture
/// contract report.
const STATE_ELEMENT: &str = "sim-driver";

/// `QualityReason` as a stable `i64` code — the reason arm of the
/// `point.{id}.quality` and `point.{id}.fault` fields.
fn encode_reason(reason: QualityReason) -> i64 {
    match reason {
        QualityReason::Unspecified => 0,
        QualityReason::Substituted => 1,
        QualityReason::Stale => 2,
        QualityReason::OutOfRange => 3,
        QualityReason::CommunicationFault => 4,
        QualityReason::DeviceFault => 5,
        QualityReason::ConfigurationFault => 6,
    }
}

fn decode_reason(code: i64) -> Option<QualityReason> {
    Some(match code {
        0 => QualityReason::Unspecified,
        1 => QualityReason::Substituted,
        2 => QualityReason::Stale,
        3 => QualityReason::OutOfRange,
        4 => QualityReason::CommunicationFault,
        5 => QualityReason::DeviceFault,
        6 => QualityReason::ConfigurationFault,
        _ => return None,
    })
}

/// `Quality` as a stable `i64` code: `0` for `Good`, `10 + reason` for
/// `Uncertain`, `20 + reason` for `Bad`.
pub(crate) fn encode_quality(quality: Quality) -> i64 {
    match quality {
        Quality::Good => 0,
        Quality::Uncertain(reason) => 10 + encode_reason(reason),
        Quality::Bad(reason) => 20 + encode_reason(reason),
    }
}

pub(crate) fn decode_quality(code: i64) -> Option<Quality> {
    Some(match code {
        0 => Quality::Good,
        10..=16 => Quality::Uncertain(decode_reason(code - 10)?),
        20..=26 => Quality::Bad(decode_reason(code - 20)?),
        _ => return None,
    })
}

/// A [`Fault`] as a stable `i64` code: `0` disconnected, `1` timeout,
/// `2 + quality code` for a substituted quality.
pub(crate) fn encode_fault(fault: Fault) -> i64 {
    match fault {
        Fault::Disconnected => 0,
        Fault::Timeout => 1,
        Fault::Quality(quality) => 2 + encode_quality(quality),
    }
}

pub(crate) fn decode_fault(code: i64) -> Option<Fault> {
    Some(match code {
        0 => Fault::Disconnected,
        1 => Fault::Timeout,
        2.. => Fault::Quality(decode_quality(code - 2)?),
        _ => return None,
    })
}

/// A deterministic simulated I/O backend implementing [`IoDriver`].
///
/// `SimDriver` serves the [`PointId`]s its [`ChannelMap`] binds, routes
/// written `Out` points onto their paired `In` points one step later, and
/// advances the map's process elements by an explicit, caller-supplied
/// `dt`. Nothing in the driver reads a wall clock or a random source:
/// identical write and step sequences produce identical samples on every
/// run, which is what makes the execution model's fixed-step scan and
/// replayable tests possible.
///
/// The driver uses interior mutability behind a `Mutex`, so it is `Sync`:
/// typed [`Input`](dcs_core::Input)/[`Output`](dcs_core::Output) handles,
/// the stepping API, and the fault API can all share one `&SimDriver`,
/// including across threads when the executor lives behind the monitoring
/// server's lock.
pub struct SimDriver {
    state: Mutex<State>,
}

impl SimDriver {
    /// Builds a driver serving `map`, or reports the map's first
    /// inconsistency as a [`ConfigError`].
    ///
    /// Every bound point starts at its declared initial value with
    /// [`Quality::Good`] at [`Tick::ZERO`]; element output points are
    /// additionally seeded with their element's `initial`.
    pub fn new(map: ChannelMap) -> Result<Self, ConfigError> {
        map.validate()?;
        let mut points = HashMap::with_capacity(map.points.len());
        for binding in map.points {
            points.insert(
                binding.point,
                PointState {
                    kind: binding.kind(),
                    direction: binding.direction,
                    sample: Sample::good(binding.initial, Tick::ZERO),
                    fault: None,
                },
            );
        }
        let mut elements = Vec::with_capacity(map.elements.len());
        for element in map.elements {
            let initial = element.initial();
            // Validated: element outputs are always bound points, and
            // the initial's kind matches the output's declared kind.
            points.get_mut(&element.output()).unwrap().sample = Sample::good(initial, Tick::ZERO);
            let y = match initial {
                Value::Float(y) => y,
                _ => 0.0,
            };
            let delay_line = match &element {
                ProcessElement::DeadTime(_) => Some(DelayLine {
                    t: 0.0,
                    history: VecDeque::from([(0.0, y)]),
                }),
                _ => None,
            };
            let rng = match &element {
                ProcessElement::Noise(noise) => noise.seed,
                _ => 0,
            };
            let contact = match &element {
                ProcessElement::Threshold(threshold) => threshold.initial,
                _ => false,
            };
            elements.push(ElementState {
                element,
                y,
                v: 0.0,
                rng,
                delay_line,
                contact,
            });
        }
        Ok(Self {
            state: Mutex::new(State {
                points,
                loopbacks: map.loopbacks,
                elements,
                tick: Tick::ZERO,
            }),
        })
    }

    /// The driver's current logical tick.
    pub fn tick(&self) -> Tick {
        self.state.lock().unwrap().tick
    }

    /// Advances the simulation one tick of `dt` time units and returns the
    /// new tick.
    ///
    /// Each step, in order:
    ///
    /// 1. the driver's tick counter advances by one;
    /// 2. every [`Loopback`] copies its `Out` point's effective sample —
    ///    value plus any injected quality — onto its paired `In` point,
    ///    stamped with the new tick;
    /// 3. every [`ProcessElement`], in declaration order, reads its input
    ///    points' effective samples and updates its output point: a
    ///    `Good` input advances the element — for a dead-time element,
    ///    pushes the input onto its delay line; for a noise element,
    ///    draws the next deviation from its generator; a `bool_flow`
    ///    stands its `on_rate` or `off_rate` by its `Bool` gate; a
    ///    `flow_sum` sums its declared `Float` inputs plus `bias`; a
    ///    `scaled_flow` stands at `gain` times its `Float` input; a
    ///    `threshold` evaluates its `Float` input against the declared
    ///    `on`/`off` bounds and drives the asserted or released contact
    ///    onto its `Bool` output — and stamps `Good`; a non-`Good` input
    ///    freezes the element's state, delay-line clock, generator, and
    ///    contact included, and propagates its quality to the output
    ///    sample — a `flow_sum` propagating the worst of its inputs'
    ///    qualities — mirroring the contract's quality propagation.
    ///
    /// A `Good`-input step whose arithmetic would drive the element
    /// non-finite — an integrator's `y + u·dt` overflowing, a `flow_sum`
    /// or scaled flow saturating, a lag's difference or a dead-time
    /// clock passing the `f64` range — commits nothing for that
    /// element: its state holds the last finite values and the output
    /// reports them `Bad`/`out_of_range`. The rule keeps every stored
    /// sample representable — a non-finite `Float` has no JSON spelling
    /// and would poison any served copy of the field permanently — and
    /// keeps the element recoverable: the first later step whose
    /// arithmetic lands finite resumes it, so a finite input write
    /// repairs an overflowed element without restarting the field.
    ///
    /// `dt` must be finite and non-negative.
    ///
    /// # Panics
    ///
    /// Panics when `dt` is negative or non-finite.
    pub fn step(&self, dt: f64) -> Tick {
        assert!(
            dt.is_finite() && dt >= 0.0,
            "step dt must be finite and non-negative, got {dt}"
        );
        let state = &mut *self.state.lock().unwrap();
        state.tick = Tick(state.tick.0 + 1);
        let tick = state.tick;

        for loopback in &state.loopbacks {
            let sample = state.points[&loopback.output].effective_sample();
            state.points.get_mut(&loopback.input).unwrap().sample = Sample { tick, ..sample };
        }

        for element in &mut state.elements {
            match &element.element {
                ProcessElement::BoolFlow(flow) => {
                    let input = state.points[&flow.input].effective_sample();
                    let output = state.points.get_mut(&flow.output).unwrap();
                    if input.quality.is_good() {
                        let Value::Bool(gate) = input.value else {
                            unreachable!("validated bool_flow gates are Bool points")
                        };
                        // The gate selects a rate, not an increment:
                        // `dt` does not scale the output — a downstream
                        // integrator owns the time base.
                        element.y = if gate { flow.on_rate } else { flow.off_rate };
                        output.sample = Sample::good(Value::Float(element.y), tick);
                    } else {
                        output.sample = Sample::new(Value::Float(element.y), input.quality, tick);
                    }
                }
                ProcessElement::FlowSum(sum) => {
                    let mut total = sum.bias;
                    let mut quality = Quality::Good;
                    for &point in &sum.inputs {
                        let sample = state.points[&point].effective_sample();
                        quality = quality.merge(sample.quality);
                        let Value::Float(value) = sample.value else {
                            unreachable!("validated flow_sum inputs are Float points")
                        };
                        total += value;
                    }
                    let output = state.points.get_mut(&sum.output).unwrap();
                    if quality.is_good() && total.is_finite() {
                        element.y = total;
                        output.sample = Sample::good(Value::Float(element.y), tick);
                    } else if quality.is_good() {
                        // All-Good inputs summed past the finite range:
                        // hold the last finite total and mark the
                        // output bad — a non-finite sample would be
                        // unrepresentable on the wire, and the element
                        // recovers on the first step whose inputs sum
                        // finite.
                        output.sample = Sample::new(
                            Value::Float(element.y),
                            Quality::Bad(QualityReason::OutOfRange),
                            tick,
                        );
                    } else {
                        output.sample = Sample::new(Value::Float(element.y), quality, tick);
                    }
                }
                ProcessElement::Threshold(threshold) => {
                    let input = state.points[&threshold.input].effective_sample();
                    let output = state.points.get_mut(&threshold.output).unwrap();
                    if input.quality.is_good() {
                        let Value::Float(u) = input.value else {
                            unreachable!("validated threshold inputs are Float points")
                        };
                        // The contact's hysteresis is the element's own:
                        // assert crossing `on`, release crossing back
                        // strictly past `off`, hold between the bounds.
                        element.contact = threshold.evaluate(u, element.contact);
                        output.sample = Sample::good(Value::Bool(element.contact), tick);
                    } else {
                        output.sample =
                            Sample::new(Value::Bool(element.contact), input.quality, tick);
                    }
                }
                _ => {
                    // Every remaining variant reads exactly one `Float`
                    // input point and drives a `Float` output.
                    let Some(input_point) = element.element.input() else {
                        unreachable!("multi-input elements step on their own paths")
                    };
                    let input = state.points[&input_point].effective_sample();
                    let output = state.points.get_mut(&element.element.output()).unwrap();
                    if input.quality.is_good() {
                        let Value::Float(u) = input.value else {
                            unreachable!("validated element inputs are Float points")
                        };
                        match element.advance(u, dt) {
                            Some(y) => {
                                element.y = y;
                                output.sample = Sample::good(Value::Float(y), tick);
                            }
                            // The step's arithmetic overflowed: the
                            // element holds its last finite state and
                            // the output reports it bad — the field
                            // degrades instead of storing a value the
                            // wire cannot carry, and recovers on the
                            // first step whose inputs produce a finite
                            // result.
                            None => {
                                output.sample = Sample::new(
                                    Value::Float(element.y),
                                    Quality::Bad(QualityReason::OutOfRange),
                                    tick,
                                );
                            }
                        }
                    } else {
                        output.sample = Sample::new(Value::Float(element.y), input.quality, tick);
                    }
                }
            }
        }
        tick
    }

    /// Every bound point's current description, ordered by [`PointId`].
    ///
    /// The driver's point census — what a server sharing this plant
    /// answers a point-listing request with. Each entry reports the
    /// binding's direction, the sample readers currently observe
    /// (injected quality faults included), and the active [`Fault`].
    pub fn points(&self) -> Vec<PointInfo> {
        let state = self.state.lock().unwrap();
        let mut points: Vec<PointInfo> = state
            .points
            .iter()
            .map(|(&point, point_state)| PointInfo {
                point,
                direction: point_state.direction,
                sample: point_state.effective_sample(),
                fault: point_state.fault,
            })
            .collect();
        points.sort_by_key(|info| info.point);
        points
    }

    /// Injects `fault` on `point`, replacing any fault already active.
    ///
    /// Returns [`IoError::UnknownPoint`] when the driver serves no such
    /// point.
    pub fn inject_fault(&self, point: PointId, fault: Fault) -> Result<(), IoError> {
        self.state
            .lock()
            .unwrap()
            .points
            .get_mut(&point)
            .ok_or(IoError::UnknownPoint(point))?
            .fault = Some(fault);
        Ok(())
    }

    /// Removes any fault injected on `point`.
    ///
    /// Returns [`IoError::UnknownPoint`] when the driver serves no such
    /// point.
    pub fn clear_fault(&self, point: PointId) -> Result<(), IoError> {
        self.state
            .lock()
            .unwrap()
            .points
            .get_mut(&point)
            .ok_or(IoError::UnknownPoint(point))?
            .fault = None;
        Ok(())
    }
}

impl IoDriver for SimDriver {
    fn read(&self, point: PointId) -> Result<Sample, IoError> {
        let state = self.state.lock().unwrap();
        let point_state = state
            .points
            .get(&point)
            .ok_or(IoError::UnknownPoint(point))?;
        if let Some(error) = point_state.fault.and_then(|fault| fault.io_error(point)) {
            return Err(error);
        }
        Ok(point_state.effective_sample())
    }

    fn write(&self, point: PointId, value: Value) -> Result<(), IoError> {
        let state = &mut *self.state.lock().unwrap();
        let point_state = state
            .points
            .get_mut(&point)
            .ok_or(IoError::UnknownPoint(point))?;
        if let Some(error) = point_state.fault.and_then(|fault| fault.io_error(point)) {
            return Err(error);
        }
        if value.kind() != point_state.kind {
            return Err(IoError::TypeMismatch {
                point,
                expected: point_state.kind,
                found: value,
            });
        }
        // A `Float` point refuses a non-finite value: NaN and the
        // infinities have no JSON spelling — serde emits `null` — so
        // storing one would serve samples no contract consumer can
        // decode, corrupting the field for every attachment until the
        // process restarts. Finite `Float`s of any magnitude land;
        // element arithmetic that overflows anyway degrades the driven
        // point's quality rather than storing the result.
        if let Value::Float(v) = value
            && !v.is_finite()
        {
            return Err(IoError::InvalidValue { point });
        }
        point_state.sample = Sample::good(value, state.tick);
        Ok(())
    }

    /// Captures the simulated field state: the driver tick, every bound
    /// point's stored sample (value, quality, tick) and injected fault,
    /// and every process element's accumulator.
    ///
    /// Field names are `tick`, `point.{id}.value` / `.quality` / `.tick`
    /// / `.fault` (the last only while a fault is active), and
    /// `element.{id}` — a `Float` accumulator for every variant but a
    /// `threshold`, whose standing contact captures as a `Bool` — plus
    /// `element.{id}.v` for second-order lags, `element.{id}.rng` for
    /// noise elements' generator state, and a dead time's delay line as
    /// `element.{id}.line.t` / `.len` / `.{i}.t` / `.{i}.u` — the line's
    /// clock and its history ring, oldest first —
    /// keyed by the element's driven point id. Loopbacks and element
    /// definitions are map configuration, not state, so they are not
    /// captured. This is what transfers the simulated process to a
    /// standby; a real driver leaves the contract unimplemented and
    /// observes the actual field instead.
    fn capture_state(&self) -> Option<StateMap> {
        let state = self.state.lock().unwrap();
        let mut captured = StateMap::new();
        captured.insert("tick", Value::Int(state.tick.0 as i64));
        let mut points: Vec<(PointId, Sample)> = state
            .points
            .iter()
            .map(|(&point, point_state)| (point, point_state.sample))
            .collect();
        points.sort_by_key(|(point, _)| *point);
        capture_points(&mut captured, &points, |point| state.points[&point].fault);
        for element in &state.elements {
            let output = element.element.output().0;
            let field = format!("element.{output}");
            match &element.element {
                ProcessElement::Threshold(_) => {
                    captured.insert(field, Value::Bool(element.contact));
                }
                _ => {
                    captured.insert(field, Value::Float(element.y));
                }
            }
            if let ProcessElement::SecondOrderLag(_) = element.element {
                captured.insert(format!("element.{output}.v"), Value::Float(element.v));
            }
            if let ProcessElement::Noise(_) = element.element {
                captured.insert(
                    format!("element.{output}.rng"),
                    Value::Int(element.rng as i64),
                );
            }
            if let ProcessElement::DeadTime(_) = element.element {
                // Constructed in `new` for every dead-time element.
                let line = element.delay_line.as_ref().unwrap();
                let prefix = format!("element.{output}.line");
                captured.insert(format!("{prefix}.t"), Value::Float(line.t));
                captured.insert(
                    format!("{prefix}.len"),
                    Value::Int(line.history.len() as i64),
                );
                for (index, &(t, u)) in line.history.iter().enumerate() {
                    captured.insert(format!("{prefix}.{index}.t"), Value::Float(t));
                    captured.insert(format!("{prefix}.{index}.u"), Value::Float(u));
                }
            }
        }
        Some(captured)
    }

    /// Restores state produced by an equivalent driver's
    /// [`capture_state`](IoDriver::capture_state).
    ///
    /// The whole map is validated first — required fields present with
    /// the declared kinds, codes decodable, every field one this driver
    /// captured — so a rejected restore changes nothing. Field `point.{id}`
    /// entries must cover exactly the bound points and `element.{id}`
    /// entries exactly the map's elements, so a map from a different
    /// [`ChannelMap`] fails naming `sim-driver`.
    fn restore_state(&self, state: &StateMap) -> Result<(), StateError> {
        let invalid = |field: String, value: Value| StateError::InvalidValue {
            element: STATE_ELEMENT.to_string(),
            field,
            value,
        };
        let current = &mut *self.state.lock().unwrap();

        let tick = state.require_i64(STATE_ELEMENT, "tick")?;
        if tick < 0 {
            return Err(invalid("tick".to_string(), Value::Int(tick)));
        }

        // Collect every expected field name while validating, so the
        // final check rejects fields this driver never captured.
        let mut known = vec!["tick".to_string()];
        let points = restore_points(
            current
                .points
                .iter()
                .map(|(&point, point_state)| (point, point_state.kind)),
            state,
            STATE_ELEMENT,
            FaultParticipation::Participates,
            &mut known,
        )?;

        let mut ys = Vec::with_capacity(current.elements.len());
        for element in &current.elements {
            let output = element.element.output().0;
            let field = format!("element.{output}");
            // A threshold's accumulator is its standing Bool contact;
            // every other element's is a finite Float.
            let (y, contact) = match &element.element {
                ProcessElement::Threshold(_) => (0.0, state.require_bool(STATE_ELEMENT, &field)?),
                _ => {
                    let y = state.require_f64(STATE_ELEMENT, &field)?;
                    if !y.is_finite() {
                        return Err(invalid(field, Value::Float(y)));
                    }
                    (y, false)
                }
            };
            known.push(field);
            let mut v = 0.0;
            if let ProcessElement::SecondOrderLag(_) = element.element {
                let field = format!("element.{output}.v");
                v = state.require_f64(STATE_ELEMENT, &field)?;
                if !v.is_finite() {
                    return Err(invalid(field, Value::Float(v)));
                }
                known.push(field);
            }
            let mut rng = 0;
            if let ProcessElement::Noise(_) = element.element {
                let field = format!("element.{output}.rng");
                // Every bit pattern is a legal generator state, so a
                // decoded i64 restores as its u64 bits.
                rng = state.require_i64(STATE_ELEMENT, &field)? as u64;
                known.push(field);
            }
            let mut delay_line = None;
            if let ProcessElement::DeadTime(_) = element.element {
                let prefix = format!("element.{output}.line");
                let t = state.require_f64(STATE_ELEMENT, &format!("{prefix}.t"))?;
                if !t.is_finite() || t < 0.0 {
                    return Err(invalid(format!("{prefix}.t"), Value::Float(t)));
                }
                let len = state.require_i64(STATE_ELEMENT, &format!("{prefix}.len"))?;
                // The ring always holds at least its seed sample; an
                // empty restored line could never answer a lookup.
                if len < 1 {
                    return Err(invalid(format!("{prefix}.len"), Value::Int(len)));
                }
                known.extend([format!("{prefix}.t"), format!("{prefix}.len")]);
                let mut history = VecDeque::with_capacity(len as usize);
                let mut previous = 0.0;
                for index in 0..len {
                    let field = format!("{prefix}.{index}");
                    let sample_t = state.require_f64(STATE_ELEMENT, &format!("{field}.t"))?;
                    // A produced ring is oldest-first — samples at or
                    // after 0, non-decreasing — with none ahead of the
                    // line's clock.
                    if !sample_t.is_finite() || sample_t < previous || sample_t > t {
                        return Err(invalid(format!("{field}.t"), Value::Float(sample_t)));
                    }
                    let u = state.require_f64(STATE_ELEMENT, &format!("{field}.u"))?;
                    if !u.is_finite() {
                        return Err(invalid(format!("{field}.u"), Value::Float(u)));
                    }
                    known.extend([format!("{field}.t"), format!("{field}.u")]);
                    history.push_back((sample_t, u));
                    previous = sample_t;
                }
                delay_line = Some(DelayLine { t, history });
            }
            ys.push((y, v, rng, contact, delay_line));
        }

        let known_refs: Vec<&str> = known.iter().map(String::as_str).collect();
        state.ensure_known_fields(STATE_ELEMENT, &known_refs)?;

        current.tick = Tick(tick as u64);
        for (point, restored) in points {
            let point_state = current.points.get_mut(&point).unwrap();
            point_state.sample = restored.sample;
            point_state.fault = restored.fault;
        }
        for (element, (y, v, rng, contact, delay_line)) in current.elements.iter_mut().zip(ys) {
            element.y = y;
            element.v = v;
            element.rng = rng;
            element.contact = contact;
            element.delay_line = delay_line;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::map::{
        BoolFlow, ChannelId, DeadTime, Direction, FirstOrderLag, FlowSum, Integrator, Noise,
        PointBinding, ScaledFlow, SecondOrderLag, Threshold,
    };
    use dcs_core::{Input, Output, QualityReason};

    fn binding(point: u64, direction: Direction, initial: Value) -> PointBinding {
        PointBinding {
            point: PointId(point),
            channel: ChannelId {
                device: 1,
                name: format!("ch{point}"),
            },
            direction,
            initial,
        }
    }

    fn float_point(point: u64, direction: Direction) -> PointBinding {
        binding(point, direction, Value::Float(0.0))
    }

    fn loopback_map() -> ChannelMap {
        ChannelMap::new()
            .with_point(float_point(10, Direction::In))
            .with_point(float_point(20, Direction::Out))
            .with_loopback(Loopback {
                output: PointId(20),
                input: PointId(10),
            })
    }

    #[test]
    fn written_output_is_readable_on_loopback_input_after_one_tick() {
        let sim = SimDriver::new(loopback_map()).unwrap();
        let driver: &dyn IoDriver = &sim;

        driver.write(PointId(20), Value::Float(3.5)).unwrap();
        // Routing happens at the step boundary, not at write time.
        assert_eq!(driver.read(PointId(10)).unwrap().value, Value::Float(0.0));

        let tick = sim.step(0.1);
        let sample = driver.read(PointId(10)).unwrap();
        assert_eq!(sample.value, Value::Float(3.5));
        assert!(sample.quality.is_good());
        assert_eq!(sample.tick, tick);
    }

    #[test]
    fn first_order_lag_converges_toward_input_within_tolerance() {
        let map = ChannelMap::new()
            .with_point(float_point(1, Direction::In))
            .with_point(float_point(2, Direction::In))
            .with_element(ProcessElement::FirstOrderLag(FirstOrderLag {
                input: PointId(1),
                output: PointId(2),
                time_constant: 1.0,
                initial: 0.0,
            }));
        let sim = SimDriver::new(map).unwrap();
        let driver: &dyn IoDriver = &sim;
        driver.write(PointId(1), Value::Float(10.0)).unwrap();

        let (dt, ticks) = (0.1, 50); // five time constants of simulated time
        for _ in 0..ticks {
            sim.step(dt);
        }

        let Value::Float(y) = driver.read(PointId(2)).unwrap().value else {
            panic!("lag output must be Float")
        };
        // Exact discretization: y_N = u - (u - y0)·e^{-N·dt/τ} = 10(1 - e^-5).
        let expected = 10.0 * (1.0 - (-5.0_f64).exp());
        assert!((y - expected).abs() < 1e-9, "y={y} expected={expected}");
        // Stated tolerance: within 1% of the 10-unit step.
        assert!((y - 10.0).abs() < 0.1, "y={y}");
    }

    #[test]
    fn integrator_accumulates_input_times_dt() {
        let map = ChannelMap::new()
            .with_point(float_point(1, Direction::In))
            .with_point(float_point(2, Direction::In))
            .with_element(ProcessElement::Integrator(Integrator {
                input: PointId(1),
                output: PointId(2),
                initial: 0.0,
            }));
        let sim = SimDriver::new(map).unwrap();
        sim.write(PointId(1), Value::Float(2.0)).unwrap();

        // Variable caller-supplied dt: 4·0.5 + 3·1.0 = 5 time units.
        for _ in 0..4 {
            sim.step(0.5);
        }
        for _ in 0..3 {
            sim.step(1.0);
        }
        // y = 0 + 2.0 · 5.0 = 10.0, exactly representable.
        assert_eq!(sim.read(PointId(2)).unwrap().value, Value::Float(10.0));
    }

    #[test]
    fn an_overflowing_step_marks_the_output_bad_and_the_element_recovers() {
        // Finite, contract-legal inputs can still overflow the element's
        // own arithmetic — a 1e308 input stepping 1e308 lands past the
        // f64 range. The step commits nothing: the output reports the
        // last finite state `Bad`/`out_of_range` — a sample every wire
        // contract still decodes — rather than storing a non-finite
        // value that serializes `{"float":null}` and stays corrupt under
        // every later step until the field restarts.
        let map = ChannelMap::new()
            .with_point(float_point(1, Direction::In))
            .with_point(float_point(2, Direction::In))
            .with_element(ProcessElement::Integrator(Integrator {
                input: PointId(1),
                output: PointId(2),
                initial: 0.0,
            }));
        let sim = SimDriver::new(map).unwrap();
        sim.write(PointId(1), Value::Float(1e308)).unwrap();
        sim.step(1e308);
        let sample = sim.read(PointId(2)).unwrap();
        assert_eq!(sample.value, Value::Float(0.0));
        assert_eq!(sample.quality, Quality::Bad(QualityReason::OutOfRange));
        // The served sample stays decodable under the JSON contract —
        // the roundtrip is the representability invariant itself.
        let json = serde_json::to_string(&sample).unwrap();
        assert_eq!(serde_json::from_str::<Sample>(&json).unwrap(), sample);

        // A second overflowing step holds the same verdict — the
        // accumulator never left its finite state.
        sim.step(1e308);
        assert_eq!(sim.read(PointId(2)).unwrap().value, Value::Float(0.0));

        // The documented recovery: a finite input write followed by a
        // step whose arithmetic lands finite resumes the element from
        // the state it held — no field restart.
        sim.write(PointId(1), Value::Float(2.0)).unwrap();
        sim.step(1.0);
        let sample = sim.read(PointId(2)).unwrap();
        assert_eq!(sample.value, Value::Float(2.0));
        assert!(sample.quality.is_good());
    }

    fn second_order_map(time_constant: f64, damping_ratio: f64) -> ChannelMap {
        ChannelMap::new()
            .with_point(float_point(1, Direction::In))
            .with_point(float_point(2, Direction::In))
            .with_element(ProcessElement::SecondOrderLag(SecondOrderLag {
                input: PointId(1),
                output: PointId(2),
                time_constant,
                damping_ratio,
                initial: 0.0,
            }))
    }

    /// The continuous-time step response of
    /// `τ² y'' + 2ζτ y' + y = u` to input `u` from rest at `y = 0` —
    /// what the exact zero-order-hold discretization must reproduce at
    /// every tick boundary `t = k·dt`.
    fn second_order_step_response(u: f64, tau: f64, zeta: f64, t: f64) -> f64 {
        let omega = 1.0 / tau;
        let sigma = zeta * omega;
        let deviation = zeta * zeta - 1.0;
        if deviation.abs() < 1e-9 {
            u * (1.0 - (-omega * t).exp() * (1.0 + omega * t))
        } else if deviation < 0.0 {
            let wd = omega * (-deviation).sqrt();
            u * (1.0 - (-sigma * t).exp() * ((wd * t).cos() + sigma / wd * (wd * t).sin()))
        } else {
            // y = u + c1·e^{r1·t} + c2·e^{r2·t} with y(0) = y'(0) = 0.
            let beta = omega * deviation.sqrt();
            let r1 = -sigma + beta;
            let r2 = -sigma - beta;
            let c1 = u * r2 / (r1 - r2);
            let c2 = -u * r1 / (r1 - r2);
            u + c1 * (r1 * t).exp() + c2 * (r2 * t).exp()
        }
    }

    fn second_order_output(sim: &SimDriver) -> f64 {
        let Value::Float(y) = sim.read(PointId(2)).unwrap().value else {
            panic!("second-order output must be Float")
        };
        y
    }

    #[test]
    fn second_order_lag_step_response_matches_continuous_time_at_tick_boundaries() {
        // τ = 0.5, ζ = 0.3: underdamped, ~37% overshoot, peak near
        // t ≈ 1.65; 160 steps of dt = 0.05 reach t = 8 = 16τ.
        let sim = SimDriver::new(second_order_map(0.5, 0.3)).unwrap();
        sim.write(PointId(1), Value::Float(10.0)).unwrap();

        let dt = 0.05;
        let mut peak = f64::MIN;
        for tick in 1..=160_u64 {
            sim.step(dt);
            let y = second_order_output(&sim);
            peak = peak.max(y);
            let expected = second_order_step_response(10.0, 0.5, 0.3, tick as f64 * dt);
            // Stated tolerance: the exact discretization reproduces the
            // continuous-time response at tick boundaries within 1e-9.
            assert!(
                (y - expected).abs() < 1e-9,
                "tick {tick}: y={y} expected={expected}"
            );
        }
        // The underdamped response overshot its input by more than 30%...
        assert!(peak > 13.0, "peak {peak}");
        // ...then settled to it: within 1% of the 10-unit step.
        assert!((second_order_output(&sim) - 10.0).abs() < 0.1);
    }

    #[test]
    fn second_order_lag_critical_and_overdamped_approach_monotonically() {
        // ζ = 1 critically damped, ζ = 2 overdamped: no overshoot, and
        // every tick-boundary sample still matches continuous time.
        for zeta in [1.0, 2.0] {
            let sim = SimDriver::new(second_order_map(0.5, zeta)).unwrap();
            sim.write(PointId(1), Value::Float(10.0)).unwrap();
            let dt = 0.05;
            let mut previous = 0.0;
            for tick in 1..=160_u64 {
                sim.step(dt);
                let y = second_order_output(&sim);
                let expected = second_order_step_response(10.0, 0.5, zeta, tick as f64 * dt);
                assert!(
                    (y - expected).abs() < 1e-9,
                    "zeta={zeta} tick {tick}: y={y} expected={expected}"
                );
                assert!(
                    y >= previous - 1e-12 && y <= 10.0 + 1e-9,
                    "zeta={zeta} tick {tick}: non-monotone y={y}"
                );
                previous = y;
            }
            // The overdamped run is still rising where the critical run
            // has converged — the sluggish approach is visible.
            assert!(second_order_output(&sim) < 10.0 + 1e-9);
        }
    }

    #[test]
    fn non_good_second_order_input_freezes_output_and_rate_then_resumes() {
        // A clean reference run: the trajectory the faulted run must
        // rejoin once the fault clears.
        let clean = SimDriver::new(second_order_map(0.5, 0.3)).unwrap();
        clean.write(PointId(1), Value::Float(10.0)).unwrap();
        let mut reference = Vec::new();
        for _ in 0..30 {
            clean.step(0.05);
            reference.push(second_order_output(&clean));
        }

        let sim = SimDriver::new(second_order_map(0.5, 0.3)).unwrap();
        sim.write(PointId(1), Value::Float(10.0)).unwrap();
        // Freeze mid-swing, while the output rate is nonzero.
        for _ in 0..10 {
            sim.step(0.05);
        }
        let frozen = second_order_output(&sim);

        let quality = Quality::Bad(QualityReason::CommunicationFault);
        sim.inject_fault(PointId(1), Fault::Quality(quality))
            .unwrap();
        for _ in 0..3 {
            sim.step(0.05);
            let sample = sim.read(PointId(2)).unwrap();
            // The documented rule: a non-Good input freezes the
            // element's state and propagates its quality.
            assert_eq!(sample.value, Value::Float(frozen));
            assert_eq!(sample.quality, quality);
        }
        sim.clear_fault(PointId(1)).unwrap();

        // Both accumulators froze: the resumed run rejoins the
        // reference trajectory exactly three ticks late.
        for step in 14..=30_usize {
            sim.step(0.05);
            let sample = sim.read(PointId(2)).unwrap();
            assert_eq!(
                sample.value,
                Value::Float(reference[step - 4]),
                "resumed step {step}"
            );
            assert!(sample.quality.is_good());
        }
    }

    #[test]
    fn second_order_lag_rejects_invalid_constants_and_point_kinds() {
        // The time constant must be finite and positive.
        for time_constant in [0.0, -1.0, f64::NAN, f64::INFINITY] {
            assert!(matches!(
                second_order_map(time_constant, 1.0).validate().unwrap_err(),
                ConfigError::InvalidTimeConstant { point, .. } if point == PointId(2)
            ));
        }
        // The damping ratio must be finite and positive.
        for damping_ratio in [0.0, -0.5, f64::NAN, f64::INFINITY] {
            assert!(matches!(
                second_order_map(1.0, damping_ratio).validate().unwrap_err(),
                ConfigError::InvalidDamping { point, .. } if point == PointId(2)
            ));
        }
        // Element ends must be Float points.
        let map = ChannelMap::new()
            .with_point(binding(1, Direction::In, Value::Int(0)))
            .with_point(float_point(2, Direction::In))
            .with_element(ProcessElement::SecondOrderLag(SecondOrderLag {
                input: PointId(1),
                output: PointId(2),
                time_constant: 1.0,
                damping_ratio: 1.0,
                initial: 0.0,
            }));
        assert_eq!(
            map.validate().unwrap_err(),
            ConfigError::ElementPointKind {
                point: PointId(1),
                kind: ValueKind::Int,
            }
        );
        // The initial value must be finite.
        let map = ChannelMap::new()
            .with_point(float_point(1, Direction::In))
            .with_point(float_point(2, Direction::In))
            .with_element(ProcessElement::SecondOrderLag(SecondOrderLag {
                input: PointId(1),
                output: PointId(2),
                time_constant: 1.0,
                damping_ratio: 1.0,
                initial: f64::NAN,
            }));
        assert!(matches!(
            map.validate().unwrap_err(),
            ConfigError::NonFiniteInitial { point, .. } if point == PointId(2)
        ));
    }

    #[test]
    fn second_order_lag_serde_roundtrips() {
        let map = ChannelMap::new()
            .with_point(float_point(1, Direction::In))
            .with_point(float_point(2, Direction::In))
            .with_element(ProcessElement::SecondOrderLag(SecondOrderLag {
                input: PointId(1),
                output: PointId(2),
                time_constant: 0.5,
                damping_ratio: 0.3,
                initial: 1.25,
            }));
        let json = serde_json::to_string(&map).unwrap();
        assert!(json.contains("\"second_order_lag\""), "{json}");
        assert_eq!(serde_json::from_str::<ChannelMap>(&json).unwrap(), map);
    }

    #[test]
    fn captured_state_restores_second_order_rate_mid_swing() {
        let map = second_order_map(0.5, 0.3);
        let sim = SimDriver::new(map.clone()).unwrap();
        sim.write(PointId(1), Value::Float(10.0)).unwrap();
        // Capture mid-swing, while the output rate is nonzero: the rate
        // accumulator is part of the transferred field state.
        for _ in 0..8 {
            sim.step(0.05);
        }
        let state = sim.capture_state().unwrap();

        let fresh = SimDriver::new(map).unwrap();
        fresh.restore_state(&state).unwrap();
        for _ in 0..20 {
            sim.step(0.05);
            fresh.step(0.05);
            assert_eq!(
                fresh.read(PointId(2)).unwrap(),
                sim.read(PointId(2)).unwrap()
            );
        }
    }

    fn dead_time_map(delay: f64) -> ChannelMap {
        ChannelMap::new()
            .with_point(float_point(1, Direction::In))
            .with_point(float_point(2, Direction::In))
            .with_element(ProcessElement::DeadTime(DeadTime {
                input: PointId(1),
                output: PointId(2),
                delay,
                initial: -1.0,
            }))
    }

    #[test]
    fn dead_time_reproduces_step_input_at_expected_tick() {
        // delay = 0.3 = 3 steps of dt = 0.1: a value first read at step k
        // must appear at the output at step k + 3.
        let sim = SimDriver::new(dead_time_map(0.3)).unwrap();
        sim.write(PointId(1), Value::Float(5.0)).unwrap();

        // Steps 1-3: the delay line still holds only its seed.
        for step in 1..=3 {
            sim.step(0.1);
            let sample = sim.read(PointId(2)).unwrap();
            assert_eq!(sample.value, Value::Float(-1.0), "step {step}");
            assert!(sample.quality.is_good());
        }
        // Step 4: the step input written before step 1 arrives, delayed by
        // exactly the configured 0.3 time units.
        sim.step(0.1);
        assert_eq!(sim.read(PointId(2)).unwrap().value, Value::Float(5.0));

        // A second step written before step 7 arrives at step 10.
        for _ in 0..2 {
            sim.step(0.1);
        }
        sim.write(PointId(1), Value::Float(7.5)).unwrap();
        for step in 7..=9 {
            sim.step(0.1);
            assert_eq!(
                sim.read(PointId(2)).unwrap().value,
                Value::Float(5.0),
                "step {step}"
            );
        }
        sim.step(0.1);
        assert_eq!(sim.read(PointId(2)).unwrap().value, Value::Float(7.5));
    }

    #[test]
    fn dead_time_rounds_sub_step_delay_up_to_whole_ticks() {
        // delay = 0.25 is not a multiple of dt = 0.1: the realized delay is
        // ceil(0.25 / 0.1) = 3 steps, within one dt of the configured delay.
        let sim = SimDriver::new(dead_time_map(0.25)).unwrap();
        sim.write(PointId(1), Value::Float(2.0)).unwrap();
        for step in 1..=3 {
            sim.step(0.1);
            assert_eq!(
                sim.read(PointId(2)).unwrap().value,
                Value::Float(-1.0),
                "step {step}"
            );
        }
        sim.step(0.1);
        assert_eq!(sim.read(PointId(2)).unwrap().value, Value::Float(2.0));
    }

    #[test]
    fn non_good_dead_time_input_freezes_output_and_propagates_quality() {
        // delay = 0.2, dt = 0.1: inputs appear two steps after being read.
        let sim = SimDriver::new(dead_time_map(0.2)).unwrap();
        sim.write(PointId(1), Value::Float(4.0)).unwrap();
        for _ in 0..3 {
            sim.step(0.1);
        }
        let frozen = sim.read(PointId(2)).unwrap().value;
        assert_eq!(frozen, Value::Float(4.0));

        // While the input is non-Good the delay line freezes: the output
        // holds the frozen value and reports the input's quality, and a
        // write behind the fault is not consumed.
        let quality = Quality::Bad(QualityReason::CommunicationFault);
        sim.inject_fault(PointId(1), Fault::Quality(quality))
            .unwrap();
        sim.write(PointId(1), Value::Float(9.0)).unwrap();
        for _ in 0..2 {
            sim.step(0.1);
            let sample = sim.read(PointId(2)).unwrap();
            assert_eq!(sample.value, frozen);
            assert_eq!(sample.quality, quality);
        }

        // Clearing the fault resumes the delay line where it froze: the
        // 9.0 write is read at the next Good step and arrives two steps
        // later.
        sim.clear_fault(PointId(1)).unwrap();
        for step in 0..2 {
            sim.step(0.1);
            let sample = sim.read(PointId(2)).unwrap();
            assert_eq!(sample.value, frozen, "resumed step {step}");
            assert!(sample.quality.is_good());
        }
        sim.step(0.1);
        assert_eq!(sim.read(PointId(2)).unwrap().value, Value::Float(9.0));
    }

    #[test]
    fn captured_state_restores_dead_time_line_mid_flight() {
        let map = dead_time_map(0.4);
        let sim = SimDriver::new(map.clone()).unwrap();
        sim.write(PointId(1), Value::Float(5.0)).unwrap();
        // Capture mid-delay, inputs still in flight on the line: the
        // line's clock and history ring are part of the transferred
        // field state.
        for _ in 0..2 {
            sim.step(0.1);
        }
        let state = sim.capture_state().unwrap();
        assert_eq!(state.get("element.2.line.t"), Some(Value::Float(0.2)));
        assert_eq!(state.get("element.2.line.len"), Some(Value::Int(3)));
        assert_eq!(state.get("element.2.line.0.u"), Some(Value::Float(-1.0)));
        assert_eq!(state.get("element.2.line.2.u"), Some(Value::Float(5.0)));

        let fresh = SimDriver::new(map).unwrap();
        fresh.restore_state(&state).unwrap();
        // The in-flight inputs land on the same steps — no `initial`
        // replay window — and a further input step delays identically.
        sim.write(PointId(1), Value::Float(7.5)).unwrap();
        fresh.write(PointId(1), Value::Float(7.5)).unwrap();
        for _ in 0..10 {
            sim.step(0.1);
            fresh.step(0.1);
            assert_eq!(
                fresh.read(PointId(2)).unwrap(),
                sim.read(PointId(2)).unwrap()
            );
        }
    }

    #[test]
    fn captured_state_resyncs_a_standbys_drifted_delay_line() {
        let map = dead_time_map(0.4);
        let sim = SimDriver::new(map.clone()).unwrap();
        sim.write(PointId(1), Value::Float(5.0)).unwrap();
        for _ in 0..3 {
            sim.step(0.1);
        }
        let state = sim.capture_state().unwrap();

        // The tracking standby's local backend kept stepping between
        // pulls, so its line holds a divergent clock and input
        // trajectory of its own.
        let standby = SimDriver::new(map).unwrap();
        standby.write(PointId(1), Value::Float(-40.0)).unwrap();
        for _ in 0..6 {
            standby.step(0.1);
        }
        standby.restore_state(&state).unwrap();

        // The rebuild is wholesale — the restored driver's full state
        // is the captured one, the drifted line resynced, not merged.
        assert_eq!(standby.capture_state().unwrap(), state);
        for _ in 0..10 {
            sim.step(0.1);
            standby.step(0.1);
            assert_eq!(
                standby.read(PointId(2)).unwrap(),
                sim.read(PointId(2)).unwrap()
            );
        }
    }

    #[test]
    fn restore_rejects_dead_time_maps_without_their_line() {
        let sim = SimDriver::new(dead_time_map(0.3)).unwrap();
        sim.write(PointId(1), Value::Float(5.0)).unwrap();
        sim.step(0.1);
        let state = sim.capture_state().unwrap();

        // A checkpoint from a driver that produced no line section.
        let mut stripped = StateMap::new();
        for (field, value) in state.iter() {
            if !field.starts_with("element.2.line.") {
                stripped.insert(field, value);
            }
        }
        assert_eq!(
            sim.restore_state(&stripped).unwrap_err(),
            StateError::MissingField {
                element: "sim-driver".to_string(),
                field: "element.2.line.t".to_string(),
            }
        );
        // A rejected restore changes nothing.
        assert_eq!(sim.tick(), Tick(1));

        // A field inside the section the driver never captured.
        let mut foreign = state.clone();
        foreign.insert("element.2.line.99.u", Value::Float(0.0));
        assert_eq!(
            sim.restore_state(&foreign).unwrap_err(),
            StateError::UnknownField {
                element: "sim-driver".to_string(),
                field: "element.2.line.99.u".to_string(),
            }
        );
        assert_eq!(sim.tick(), Tick(1));

        // Line fields are foreign to elements of every other variant.
        let scalar = SimDriver::new(
            ChannelMap::new()
                .with_point(float_point(1, Direction::In))
                .with_point(float_point(2, Direction::In))
                .with_element(ProcessElement::FirstOrderLag(FirstOrderLag {
                    input: PointId(1),
                    output: PointId(2),
                    time_constant: 1.0,
                    initial: 0.0,
                })),
        )
        .unwrap();
        assert_eq!(
            scalar.restore_state(&state).unwrap_err(),
            StateError::UnknownField {
                element: "sim-driver".to_string(),
                field: "element.2.line.0.t".to_string(),
            }
        );
    }

    fn noise_map(amplitude: f64, seed: u64) -> ChannelMap {
        ChannelMap::new()
            .with_point(float_point(1, Direction::In))
            .with_point(float_point(2, Direction::In))
            .with_element(ProcessElement::Noise(Noise {
                input: PointId(1),
                output: PointId(2),
                amplitude,
                seed,
                initial: 0.0,
            }))
    }

    fn noise_output(sim: &SimDriver) -> f64 {
        let Value::Float(y) = sim.read(PointId(2)).unwrap().value else {
            panic!("noise output must be Float")
        };
        y
    }

    fn noise_sequence(seed: u64, ticks: usize) -> Vec<f64> {
        let sim = SimDriver::new(noise_map(0.5, seed)).unwrap();
        sim.write(PointId(1), Value::Float(10.0)).unwrap();
        (0..ticks)
            .map(|_| {
                sim.step(0.1);
                noise_output(&sim)
            })
            .collect()
    }

    #[test]
    fn identical_noise_runs_produce_identical_sequences() {
        let first = noise_sequence(42, 40);
        let second = noise_sequence(42, 40);
        assert_eq!(first, second);
        // The run produced real deviation, not a constant passthrough.
        assert!(first.iter().any(|&y| y != 10.0));
    }

    #[test]
    fn distinct_seeds_produce_distinct_noise_sequences() {
        assert_ne!(noise_sequence(1, 40), noise_sequence(2, 40));
    }

    #[test]
    fn noise_output_stays_within_amplitude_of_input() {
        let sim = SimDriver::new(noise_map(0.5, 7)).unwrap();
        // A moving input still bounds every step's output: the documented
        // contract is y within u ± amplitude.
        for (step, u) in [10.0, -3.0, 0.0, 4.25].into_iter().enumerate() {
            for _ in 0..10 {
                sim.write(PointId(1), Value::Float(u)).unwrap();
                sim.step(0.1);
                let y = noise_output(&sim);
                assert!(
                    (y - u).abs() <= 0.5,
                    "input block {step}: y={y} outside u={u} ± 0.5"
                );
            }
        }
        // Zero amplitude passes the input through unchanged.
        let quiet = SimDriver::new(noise_map(0.0, 99)).unwrap();
        quiet.write(PointId(1), Value::Float(3.0)).unwrap();
        quiet.step(0.1);
        assert_eq!(noise_output(&quiet), 3.0);
    }

    #[test]
    fn non_good_noise_input_freezes_generator_and_propagates_quality() {
        // A clean reference run: the deviation sequence the faulted run
        // must rejoin once the fault clears.
        let clean = SimDriver::new(noise_map(0.5, 5)).unwrap();
        clean.write(PointId(1), Value::Float(4.0)).unwrap();
        let mut reference = Vec::new();
        for _ in 0..20 {
            clean.step(0.1);
            reference.push(noise_output(&clean) - 4.0);
        }

        let sim = SimDriver::new(noise_map(0.5, 5)).unwrap();
        sim.write(PointId(1), Value::Float(4.0)).unwrap();
        for _ in 0..10 {
            sim.step(0.1);
        }
        let frozen = noise_output(&sim);

        let quality = Quality::Bad(QualityReason::CommunicationFault);
        sim.inject_fault(PointId(1), Fault::Quality(quality))
            .unwrap();
        // A write behind the fault is stored but not consumed.
        sim.write(PointId(1), Value::Float(9.0)).unwrap();
        for _ in 0..3 {
            sim.step(0.1);
            let sample = sim.read(PointId(2)).unwrap();
            // The documented rule: a non-Good input freezes the
            // element's state — generator included — and propagates
            // its quality.
            assert_eq!(sample.value, Value::Float(frozen));
            assert_eq!(sample.quality, quality);
        }
        sim.clear_fault(PointId(1)).unwrap();

        // The generator froze through the fault: the resumed run draws
        // the reference run's eleventh deviation onward, now around the
        // stored 9.0 input.
        for step in 14..=20_usize {
            sim.step(0.1);
            let sample = sim.read(PointId(2)).unwrap();
            assert!(sample.quality.is_good());
            // The deviation matches the reference draw within a rounding
            // slack — `9.0 + d` rounds in a coarser binade than `4.0 + d`.
            let Value::Float(y) = sample.value else {
                panic!("noise output must be Float")
            };
            assert!(
                (y - 9.0 - reference[step - 4]).abs() < 1e-12,
                "step {step}: y={y} reference deviation {}",
                reference[step - 4]
            );
        }
    }

    #[test]
    fn noise_rejects_invalid_constants_and_point_kinds() {
        // The amplitude must be finite and non-negative.
        for amplitude in [
            -1.0,
            -f64::MIN_POSITIVE,
            f64::NAN,
            f64::INFINITY,
            f64::NEG_INFINITY,
        ] {
            assert!(matches!(
                noise_map(amplitude, 0).validate().unwrap_err(),
                ConfigError::InvalidAmplitude { point, .. } if point == PointId(2)
            ));
        }
        // Element ends must be Float points.
        let map = ChannelMap::new()
            .with_point(binding(1, Direction::In, Value::Int(0)))
            .with_point(float_point(2, Direction::In))
            .with_element(ProcessElement::Noise(Noise {
                input: PointId(1),
                output: PointId(2),
                amplitude: 1.0,
                seed: 0,
                initial: 0.0,
            }));
        assert_eq!(
            map.validate().unwrap_err(),
            ConfigError::ElementPointKind {
                point: PointId(1),
                kind: ValueKind::Int,
            }
        );
        // The initial value must be finite.
        let map = ChannelMap::new()
            .with_point(float_point(1, Direction::In))
            .with_point(float_point(2, Direction::In))
            .with_element(ProcessElement::Noise(Noise {
                input: PointId(1),
                output: PointId(2),
                amplitude: 1.0,
                seed: 0,
                initial: f64::INFINITY,
            }));
        assert!(matches!(
            map.validate().unwrap_err(),
            ConfigError::NonFiniteInitial { point, .. } if point == PointId(2)
        ));
    }

    #[test]
    fn noise_serde_roundtrips() {
        let map = ChannelMap::new()
            .with_point(float_point(1, Direction::In))
            .with_point(float_point(2, Direction::In))
            .with_element(ProcessElement::Noise(Noise {
                input: PointId(1),
                output: PointId(2),
                amplitude: 0.5,
                seed: 42,
                initial: 1.25,
            }));
        let json = serde_json::to_string(&map).unwrap();
        assert!(json.contains("\"noise\""), "{json}");
        assert_eq!(serde_json::from_str::<ChannelMap>(&json).unwrap(), map);
    }

    #[test]
    fn captured_state_restores_noise_generator_mid_sequence() {
        let map = noise_map(0.5, 42);
        let sim = SimDriver::new(map.clone()).unwrap();
        sim.write(PointId(1), Value::Float(4.0)).unwrap();
        // Capture mid-sequence: the generator state is part of the
        // transferred field state.
        for _ in 0..8 {
            sim.step(0.1);
        }
        let state = sim.capture_state().unwrap();

        let fresh = SimDriver::new(map).unwrap();
        fresh.restore_state(&state).unwrap();
        for _ in 0..20 {
            sim.step(0.1);
            fresh.step(0.1);
            assert_eq!(
                fresh.read(PointId(2)).unwrap(),
                sim.read(PointId(2)).unwrap()
            );
        }
    }

    #[test]
    fn driver_is_usable_solely_through_io_driver_trait_object() {
        let sim = SimDriver::new(loopback_map()).unwrap();
        let driver: &dyn IoDriver = &sim;

        Output::<f64>::new(driver, PointId(20)).write(4.25).unwrap();
        sim.step(0.1);
        let sample = Input::<f64>::new(driver, PointId(10)).read().unwrap();
        assert_eq!(sample.value, 4.25);
        assert!(sample.quality.is_good());
        assert_eq!(sample.tick, Tick(1));
    }

    #[test]
    fn quality_fault_changes_read_quality() {
        let sim = SimDriver::new(loopback_map()).unwrap();
        sim.write(PointId(20), Value::Float(1.5)).unwrap();
        sim.step(0.1);

        let quality = Quality::Uncertain(QualityReason::Substituted);
        sim.inject_fault(PointId(10), Fault::Quality(quality))
            .unwrap();
        let sample = sim.read(PointId(10)).unwrap();
        assert_eq!(sample.quality, quality);
        // The stored value and tick are untouched.
        assert_eq!(sample.value, Value::Float(1.5));
        assert_eq!(sample.tick, Tick(1));

        sim.clear_fault(PointId(10)).unwrap();
        assert!(sim.read(PointId(10)).unwrap().quality.is_good());
    }

    #[test]
    fn error_faults_return_io_error() {
        let sim = SimDriver::new(loopback_map()).unwrap();
        let point = PointId(10);

        sim.inject_fault(point, Fault::Disconnected).unwrap();
        assert_eq!(sim.read(point), Err(IoError::Disconnected(point)));

        sim.inject_fault(point, Fault::Timeout).unwrap();
        assert_eq!(sim.read(point), Err(IoError::Timeout(point)));
        assert_eq!(sim.write(PointId(20), Value::Float(0.0)), Ok(()));
        sim.inject_fault(PointId(20), Fault::Timeout).unwrap();
        assert_eq!(
            sim.write(PointId(20), Value::Float(0.0)),
            Err(IoError::Timeout(PointId(20)))
        );

        sim.clear_fault(point).unwrap();
        assert!(sim.read(point).is_ok());
    }

    #[test]
    fn output_quality_fault_propagates_through_loopback() {
        let sim = SimDriver::new(loopback_map()).unwrap();
        sim.write(PointId(20), Value::Float(2.0)).unwrap();
        sim.inject_fault(
            PointId(20),
            Fault::Quality(Quality::Bad(QualityReason::DeviceFault)),
        )
        .unwrap();

        sim.step(0.1);
        let sample = sim.read(PointId(10)).unwrap();
        assert_eq!(sample.value, Value::Float(2.0));
        assert_eq!(sample.quality, Quality::Bad(QualityReason::DeviceFault));
    }

    #[test]
    fn non_good_element_input_freezes_output_and_propagates_quality() {
        let map = ChannelMap::new()
            .with_point(float_point(1, Direction::In))
            .with_point(float_point(2, Direction::In))
            .with_element(ProcessElement::Integrator(Integrator {
                input: PointId(1),
                output: PointId(2),
                initial: 0.0,
            }));
        let sim = SimDriver::new(map).unwrap();
        sim.write(PointId(1), Value::Float(1.0)).unwrap();
        sim.step(1.0);
        let frozen = sim.read(PointId(2)).unwrap().value;
        assert_eq!(frozen, Value::Float(1.0));

        let quality = Quality::Bad(QualityReason::CommunicationFault);
        sim.inject_fault(PointId(1), Fault::Quality(quality))
            .unwrap();
        sim.step(1.0);
        let sample = sim.read(PointId(2)).unwrap();
        // The integrator froze instead of adding u·dt = 1.0.
        assert_eq!(sample.value, frozen);
        assert_eq!(sample.quality, quality);

        // Clearing the fault resumes integration from the frozen state.
        sim.clear_fault(PointId(1)).unwrap();
        sim.step(1.0);
        let sample = sim.read(PointId(2)).unwrap();
        assert_eq!(sample.value, Value::Float(2.0));
        assert!(sample.quality.is_good());
    }

    #[test]
    fn write_of_wrong_kind_is_type_mismatch() {
        let sim = SimDriver::new(loopback_map()).unwrap();
        let error = sim.write(PointId(20), Value::Int(1)).unwrap_err();
        assert_eq!(
            error,
            IoError::TypeMismatch {
                point: PointId(20),
                expected: ValueKind::Float,
                found: Value::Int(1),
            }
        );

        let sim = SimDriver::new(ChannelMap::new().with_point(binding(
            5,
            Direction::In,
            Value::Bool(false),
        )))
        .unwrap();
        sim.write(PointId(5), Value::Bool(true)).unwrap();
        assert_eq!(sim.read(PointId(5)).unwrap().value, Value::Bool(true));
    }

    #[test]
    fn points_lists_every_binding_with_sample_direction_and_fault() {
        let map = ChannelMap::new()
            .with_point(float_point(20, Direction::Out))
            .with_point(float_point(10, Direction::In))
            .with_point(binding(5, Direction::In, Value::Bool(false)))
            .with_loopback(Loopback {
                output: PointId(20),
                input: PointId(10),
            });
        let sim = SimDriver::new(map).unwrap();
        sim.write(PointId(20), Value::Float(3.5)).unwrap();
        sim.step(0.1);
        sim.inject_fault(PointId(5), Fault::Timeout).unwrap();
        sim.inject_fault(
            PointId(10),
            Fault::Quality(Quality::Uncertain(QualityReason::Stale)),
        )
        .unwrap();

        let points = sim.points();
        // Ordered by point id regardless of binding order.
        assert_eq!(
            points.iter().map(|info| info.point).collect::<Vec<_>>(),
            vec![PointId(5), PointId(10), PointId(20)]
        );

        let bool_in = &points[0];
        assert_eq!(bool_in.direction, Direction::In);
        assert_eq!(bool_in.fault, Some(Fault::Timeout));
        // An error-faulted point still lists its stored sample.
        assert_eq!(bool_in.sample.value, Value::Bool(false));

        let looped = &points[1];
        assert_eq!(looped.direction, Direction::In);
        assert_eq!(looped.sample.value, Value::Float(3.5));
        // The quality fault is reflected in the listed sample.
        assert_eq!(
            looped.sample.quality,
            Quality::Uncertain(QualityReason::Stale)
        );
        assert_eq!(
            looped.fault,
            Some(Fault::Quality(Quality::Uncertain(QualityReason::Stale)))
        );

        let out = &points[2];
        assert_eq!(out.direction, Direction::Out);
        assert_eq!(out.sample.value, Value::Float(3.5));
        assert_eq!(out.fault, None);
    }

    #[test]
    fn unserved_points_are_unknown() {
        let sim = SimDriver::new(ChannelMap::new()).unwrap();
        let unknown = PointId(99);
        assert_eq!(sim.read(unknown), Err(IoError::UnknownPoint(unknown)));
        assert_eq!(
            sim.write(unknown, Value::Float(0.0)),
            Err(IoError::UnknownPoint(unknown))
        );
        assert_eq!(
            sim.inject_fault(unknown, Fault::Timeout),
            Err(IoError::UnknownPoint(unknown))
        );
        assert_eq!(
            sim.clear_fault(unknown),
            Err(IoError::UnknownPoint(unknown))
        );
    }

    #[test]
    fn captured_state_restores_into_a_fresh_driver() {
        // A lag element, a loopback pair, and an injected fault exercise
        // every captured section.
        let map = ChannelMap::new()
            .with_point(float_point(1, Direction::In))
            .with_point(float_point(2, Direction::In))
            .with_point(float_point(3, Direction::In))
            .with_point(float_point(4, Direction::Out))
            .with_loopback(Loopback {
                output: PointId(4),
                input: PointId(3),
            })
            .with_element(ProcessElement::FirstOrderLag(FirstOrderLag {
                input: PointId(1),
                output: PointId(2),
                time_constant: 1.0,
                initial: 0.0,
            }));
        let sim = SimDriver::new(map.clone()).unwrap();
        sim.write(PointId(1), Value::Float(4.0)).unwrap();
        sim.write(PointId(4), Value::Float(2.5)).unwrap();
        sim.step(0.1);
        sim.inject_fault(
            PointId(3),
            Fault::Quality(Quality::Uncertain(QualityReason::Stale)),
        )
        .unwrap();
        sim.step(0.1);
        let state = sim.capture_state().unwrap();
        assert!(!state.is_empty());

        // The map serde-roundtrips like the rest of the contract.
        let json = serde_json::to_string(&state).unwrap();
        let state = serde_json::from_str::<StateMap>(&json).unwrap();

        let fresh = SimDriver::new(map).unwrap();
        fresh.restore_state(&state).unwrap();
        assert_eq!(fresh.tick(), sim.tick());
        for point in [PointId(1), PointId(2), PointId(3), PointId(4)] {
            assert_eq!(
                fresh.read(point).unwrap(),
                sim.read(point).unwrap(),
                "point {point:?}"
            );
        }

        // Continued stepping stays identical: element accumulators and
        // faults carried over.
        sim.step(0.1);
        fresh.step(0.1);
        for point in [PointId(1), PointId(2), PointId(3), PointId(4)] {
            assert_eq!(fresh.read(point).unwrap(), sim.read(point).unwrap());
        }
    }

    #[test]
    fn restore_rejects_state_the_driver_did_not_produce() {
        let sim =
            SimDriver::new(ChannelMap::new().with_point(float_point(1, Direction::In))).unwrap();

        // A field for a point the map does not bind.
        let mut foreign = StateMap::new();
        foreign.insert("tick", Value::Int(0));
        foreign.insert("point.1.value", Value::Float(0.0));
        foreign.insert("point.1.quality", Value::Int(0));
        foreign.insert("point.1.tick", Value::Int(0));
        foreign.insert("point.9.value", Value::Float(0.0));
        assert_eq!(
            sim.restore_state(&foreign).unwrap_err(),
            StateError::UnknownField {
                element: "sim-driver".to_string(),
                field: "point.9.value".to_string(),
            }
        );

        // A required field missing.
        let mut incomplete = StateMap::new();
        incomplete.insert("tick", Value::Int(3));
        incomplete.insert("point.1.value", Value::Float(1.0));
        assert_eq!(
            sim.restore_state(&incomplete).unwrap_err(),
            StateError::MissingField {
                element: "sim-driver".to_string(),
                field: "point.1.quality".to_string(),
            }
        );
        // Nothing was applied.
        assert_eq!(sim.tick(), Tick::ZERO);

        // A value kind the point does not declare.
        let mut wrong_kind = StateMap::new();
        wrong_kind.insert("tick", Value::Int(0));
        wrong_kind.insert("point.1.value", Value::Bool(true));
        assert!(matches!(
            sim.restore_state(&wrong_kind).unwrap_err(),
            StateError::IncompatibleField { .. }
        ));

        // An undecodable quality code.
        let mut bad_code = StateMap::new();
        bad_code.insert("tick", Value::Int(0));
        bad_code.insert("point.1.value", Value::Float(0.0));
        bad_code.insert("point.1.quality", Value::Int(99));
        bad_code.insert("point.1.tick", Value::Int(0));
        assert_eq!(
            sim.restore_state(&bad_code).unwrap_err(),
            StateError::InvalidValue {
                element: "sim-driver".to_string(),
                field: "point.1.quality".to_string(),
                value: Value::Int(99),
            }
        );
    }

    #[test]
    fn restore_rejects_a_non_finite_point_value() {
        // A checkpoint map carrying a non-finite `Float` point value is
        // refused whole: the restored sample must stay representable
        // like every value the write boundary accepts — a JSON artifact
        // could never spell the value, so carrying it in-process is the
        // only way it can arrive, and it still changes nothing.
        let sim = SimDriver::new(loopback_map()).unwrap();
        let mut state = sim.capture_state().unwrap();
        state.insert("point.10.value", Value::Float(f64::INFINITY));
        assert_eq!(
            sim.restore_state(&state).unwrap_err(),
            StateError::InvalidValue {
                element: "sim-driver".to_string(),
                field: "point.10.value".to_string(),
                value: Value::Float(f64::INFINITY),
            }
        );
        assert_eq!(
            sim.read(PointId(10)).unwrap(),
            Sample::good(Value::Float(0.0), Tick::ZERO)
        );
    }

    #[test]
    fn inconsistent_maps_are_rejected() {
        // Two bindings on one point id.
        let map = ChannelMap::new()
            .with_point(float_point(1, Direction::In))
            .with_point(float_point(1, Direction::Out));
        assert_eq!(
            map.validate().unwrap_err(),
            ConfigError::DuplicatePoint(PointId(1))
        );

        // A `Float` point's seed sample must be finite — the same
        // representability rule the write boundary applies.
        for initial in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
            let map =
                ChannelMap::new().with_point(binding(1, Direction::In, Value::Float(initial)));
            assert!(matches!(
                map.validate().unwrap_err(),
                ConfigError::NonFinitePointInitial {
                    point: PointId(1),
                    value,
                } if !value.is_finite()
            ));
        }

        // A loopback must run Out -> In.
        let map = ChannelMap::new()
            .with_point(float_point(1, Direction::In))
            .with_point(float_point(2, Direction::In))
            .with_loopback(Loopback {
                output: PointId(1),
                input: PointId(2),
            });
        assert!(matches!(
            map.validate().unwrap_err(),
            ConfigError::LoopbackDirection { .. }
        ));

        // Loopback ends must share a value kind.
        let map = ChannelMap::new()
            .with_point(float_point(1, Direction::In))
            .with_point(binding(2, Direction::Out, Value::Bool(false)))
            .with_loopback(Loopback {
                output: PointId(2),
                input: PointId(1),
            });
        assert!(matches!(
            map.validate().unwrap_err(),
            ConfigError::LoopbackKindMismatch { .. }
        ));

        // Elements reference bound Float points only.
        let map = ChannelMap::new()
            .with_point(float_point(1, Direction::In))
            .with_element(ProcessElement::Integrator(Integrator {
                input: PointId(1),
                output: PointId(9),
                initial: 0.0,
            }));
        assert_eq!(
            map.validate().unwrap_err(),
            ConfigError::UnknownPoint(PointId(9))
        );

        let map = ChannelMap::new()
            .with_point(binding(1, Direction::In, Value::Int(0)))
            .with_point(float_point(2, Direction::In))
            .with_element(ProcessElement::Integrator(Integrator {
                input: PointId(1),
                output: PointId(2),
                initial: 0.0,
            }));
        assert_eq!(
            map.validate().unwrap_err(),
            ConfigError::ElementPointKind {
                point: PointId(1),
                kind: ValueKind::Int,
            }
        );

        // A point cannot be driven by a loopback and an element at once.
        let map = loopback_map().with_element(ProcessElement::Integrator(Integrator {
            input: PointId(10),
            output: PointId(10),
            initial: 0.0,
        }));
        assert_eq!(
            map.validate().unwrap_err(),
            ConfigError::ConflictingDriver { point: PointId(10) }
        );

        // Lag time constant must be finite and positive.
        for time_constant in [0.0, -1.0, f64::NAN, f64::INFINITY] {
            let map = ChannelMap::new()
                .with_point(float_point(1, Direction::In))
                .with_point(float_point(2, Direction::In))
                .with_element(ProcessElement::FirstOrderLag(FirstOrderLag {
                    input: PointId(1),
                    output: PointId(2),
                    time_constant,
                    initial: 0.0,
                }));
            assert!(matches!(
                map.validate().unwrap_err(),
                ConfigError::InvalidTimeConstant { .. }
            ));
        }

        // Dead-time delay must be finite and positive.
        for delay in [0.0, -1.0, f64::NAN, f64::INFINITY] {
            let map = ChannelMap::new()
                .with_point(float_point(1, Direction::In))
                .with_point(float_point(2, Direction::In))
                .with_element(ProcessElement::DeadTime(DeadTime {
                    input: PointId(1),
                    output: PointId(2),
                    delay,
                    initial: 0.0,
                }));
            assert!(matches!(
                map.validate().unwrap_err(),
                ConfigError::InvalidDelay { point, .. } if point == PointId(2)
            ));
        }

        // Dead-time ends must be Float points.
        let map = ChannelMap::new()
            .with_point(float_point(1, Direction::In))
            .with_point(binding(2, Direction::In, Value::Bool(false)))
            .with_element(ProcessElement::DeadTime(DeadTime {
                input: PointId(1),
                output: PointId(2),
                delay: 1.0,
                initial: 0.0,
            }));
        assert_eq!(
            map.validate().unwrap_err(),
            ConfigError::ElementPointKind {
                point: PointId(2),
                kind: ValueKind::Bool,
            }
        );
    }

    /// One scripted run over a map with a loopback, both lags, an
    /// integrator, and a dead time: identical writes and steps must
    /// replay identically.
    fn scripted_run() -> Vec<Sample> {
        let map = ChannelMap::new()
            .with_point(float_point(10, Direction::In))
            .with_point(float_point(20, Direction::Out))
            .with_point(float_point(30, Direction::In))
            .with_point(float_point(40, Direction::In))
            .with_point(float_point(50, Direction::In))
            .with_point(float_point(60, Direction::In))
            .with_loopback(Loopback {
                output: PointId(20),
                input: PointId(10),
            })
            .with_element(ProcessElement::FirstOrderLag(FirstOrderLag {
                input: PointId(10),
                output: PointId(30),
                time_constant: 0.5,
                initial: 0.0,
            }))
            .with_element(ProcessElement::Integrator(Integrator {
                input: PointId(10),
                output: PointId(40),
                initial: 0.0,
            }))
            .with_element(ProcessElement::DeadTime(DeadTime {
                input: PointId(10),
                output: PointId(50),
                delay: 0.5,
                initial: 0.0,
            }))
            .with_element(ProcessElement::SecondOrderLag(SecondOrderLag {
                input: PointId(10),
                output: PointId(60),
                time_constant: 0.25,
                damping_ratio: 0.4,
                initial: 0.0,
            }));
        let sim = SimDriver::new(map).unwrap();
        let driver: &dyn IoDriver = &sim;

        let mut samples = Vec::new();
        for command in [1.0_f64, 2.0, 2.0, -1.0, 0.0] {
            driver.write(PointId(20), Value::Float(command)).unwrap();
            sim.step(0.25);
            for point in [10_u64, 30, 40, 50, 60] {
                samples.push(driver.read(PointId(point)).unwrap());
            }
        }
        sim.step(0.5);
        for point in [10_u64, 30, 40, 50, 60] {
            samples.push(driver.read(PointId(point)).unwrap());
        }
        samples
    }

    #[test]
    fn identical_scripted_runs_produce_identical_samples() {
        let first = scripted_run();
        let second = scripted_run();
        assert_eq!(first, second);
        // The run actually produced distinct, non-trivial samples.
        assert!(first.iter().any(|sample| sample.value != Value::Float(0.0)));
        assert_eq!(first.len(), 30);
    }

    #[test]
    fn channel_map_serde_roundtrip() {
        let map = ChannelMap::new()
            .with_point(float_point(10, Direction::In))
            .with_point(float_point(20, Direction::Out))
            .with_point(float_point(30, Direction::In))
            .with_loopback(Loopback {
                output: PointId(20),
                input: PointId(10),
            })
            .with_element(ProcessElement::FirstOrderLag(FirstOrderLag {
                input: PointId(10),
                output: PointId(30),
                time_constant: 0.5,
                initial: 1.0,
            }))
            .with_point(float_point(40, Direction::In))
            .with_element(ProcessElement::DeadTime(DeadTime {
                input: PointId(10),
                output: PointId(40),
                delay: 0.5,
                initial: 2.0,
            }));
        let json = serde_json::to_string(&map).unwrap();
        assert_eq!(serde_json::from_str::<ChannelMap>(&json).unwrap(), map);
    }

    /// A `bool_flow` element gating an actuator command `Out` point
    /// onto a driven `Float` flow.
    fn bool_flow_map(on_rate: f64, off_rate: f64) -> ChannelMap {
        ChannelMap::new()
            .with_point(binding(1, Direction::Out, Value::Bool(false)))
            .with_point(float_point(2, Direction::In))
            .with_element(ProcessElement::BoolFlow(BoolFlow {
                input: PointId(1),
                output: PointId(2),
                on_rate,
                off_rate,
                initial: -1.0,
            }))
    }

    #[test]
    fn bool_flow_drives_the_declared_rate_while_the_gate_stands() {
        let sim = SimDriver::new(bool_flow_map(-10.0, 0.5)).unwrap();
        // Before the first step the output holds the declared initial.
        assert_eq!(sim.read(PointId(2)).unwrap().value, Value::Float(-1.0));

        // The released gate drives the off-rate — here a nonzero
        // trickle — from the first step reading it.
        sim.step(1.0);
        assert_eq!(sim.read(PointId(2)).unwrap().value, Value::Float(0.5));

        // Asserting the command stands the on-rate at the next tick
        // boundary, step after step, with no ramp and no dt scaling.
        sim.write(PointId(1), Value::Bool(true)).unwrap();
        for dt in [1.0, 0.25, 3.0] {
            sim.step(dt);
            assert_eq!(sim.read(PointId(2)).unwrap().value, Value::Float(-10.0));
        }

        // Releasing the command returns the output to the off-rate at
        // the next tick boundary.
        sim.write(PointId(1), Value::Bool(false)).unwrap();
        sim.step(1.0);
        assert_eq!(sim.read(PointId(2)).unwrap().value, Value::Float(0.5));
    }

    #[test]
    fn non_good_bool_flow_gate_freezes_output_and_propagates_quality() {
        let sim = SimDriver::new(bool_flow_map(-10.0, 0.0)).unwrap();
        sim.write(PointId(1), Value::Bool(true)).unwrap();
        sim.step(1.0);
        assert_eq!(sim.read(PointId(2)).unwrap().value, Value::Float(-10.0));

        let quality = Quality::Bad(QualityReason::CommunicationFault);
        sim.inject_fault(PointId(1), Fault::Quality(quality))
            .unwrap();
        // A write behind the fault is stored but not consumed.
        sim.write(PointId(1), Value::Bool(false)).unwrap();
        for _ in 0..2 {
            sim.step(1.0);
            let sample = sim.read(PointId(2)).unwrap();
            // The documented rule: a non-Good gate freezes the output
            // and propagates its quality.
            assert_eq!(sample.value, Value::Float(-10.0));
            assert_eq!(sample.quality, quality);
        }

        // Clearing the fault resumes the gate on the first Good step:
        // the stored `false` drives the off-rate.
        sim.clear_fault(PointId(1)).unwrap();
        sim.step(1.0);
        let sample = sim.read(PointId(2)).unwrap();
        assert_eq!(sample.value, Value::Float(0.0));
        assert!(sample.quality.is_good());
    }

    #[test]
    fn bool_flow_rejects_invalid_constants_and_point_kinds() {
        // Rates must be finite; the rejection names the element's
        // driven point and which declared rate offended.
        for (rate, value) in [
            ("on_rate", f64::NAN),
            ("on_rate", f64::INFINITY),
            ("off_rate", f64::NEG_INFINITY),
        ] {
            let mut flow = BoolFlow {
                input: PointId(1),
                output: PointId(2),
                on_rate: -10.0,
                off_rate: 0.0,
                initial: 0.0,
            };
            match rate {
                "on_rate" => flow.on_rate = value,
                _ => flow.off_rate = value,
            }
            let map = ChannelMap::new()
                .with_point(binding(1, Direction::Out, Value::Bool(false)))
                .with_point(float_point(2, Direction::In))
                .with_element(ProcessElement::BoolFlow(flow));
            assert!(matches!(
                map.validate().unwrap_err(),
                ConfigError::InvalidRate {
                    point: PointId(2),
                    rate: name,
                    value: rejected,
                } if name == rate && !rejected.is_finite()
            ));
        }

        // A negative rate is a declared draw, not an error.
        assert!(bool_flow_map(-10.0, 0.0).validate().is_ok());

        // The gate must be a Bool point.
        let map = ChannelMap::new()
            .with_point(float_point(1, Direction::Out))
            .with_point(float_point(2, Direction::In))
            .with_element(ProcessElement::BoolFlow(BoolFlow {
                input: PointId(1),
                output: PointId(2),
                on_rate: -10.0,
                off_rate: 0.0,
                initial: 0.0,
            }));
        assert_eq!(
            map.validate().unwrap_err(),
            ConfigError::ElementGateKind {
                point: PointId(1),
                kind: ValueKind::Float,
            }
        );

        // The driven point must be Float.
        let map = ChannelMap::new()
            .with_point(binding(1, Direction::Out, Value::Bool(false)))
            .with_point(binding(2, Direction::In, Value::Bool(false)))
            .with_element(ProcessElement::BoolFlow(BoolFlow {
                input: PointId(1),
                output: PointId(2),
                on_rate: -10.0,
                off_rate: 0.0,
                initial: 0.0,
            }));
        assert_eq!(
            map.validate().unwrap_err(),
            ConfigError::ElementPointKind {
                point: PointId(2),
                kind: ValueKind::Bool,
            }
        );

        // The initial value must be finite.
        let map = ChannelMap::new()
            .with_point(binding(1, Direction::Out, Value::Bool(false)))
            .with_point(float_point(2, Direction::In))
            .with_element(ProcessElement::BoolFlow(BoolFlow {
                input: PointId(1),
                output: PointId(2),
                on_rate: -10.0,
                off_rate: 0.0,
                initial: f64::NAN,
            }));
        assert!(matches!(
            map.validate().unwrap_err(),
            ConfigError::NonFiniteInitial { point, .. } if point == PointId(2)
        ));
    }

    #[test]
    fn bool_flow_serde_roundtrips() {
        let map = bool_flow_map(-10.0, 0.5);
        let json = serde_json::to_string(&map).unwrap();
        assert!(json.contains("\"bool_flow\""), "{json}");
        assert_eq!(serde_json::from_str::<ChannelMap>(&json).unwrap(), map);
    }

    #[test]
    fn captured_state_restores_bool_flow_output_mid_run() {
        // The element's only state is its standing output; a captured
        // run continues identically through a fresh driver.
        let map = bool_flow_map(-10.0, 0.5);
        let sim = SimDriver::new(map.clone()).unwrap();
        sim.write(PointId(1), Value::Bool(true)).unwrap();
        sim.step(1.0);
        let state = sim.capture_state().unwrap();

        let fresh = SimDriver::new(map).unwrap();
        fresh.restore_state(&state).unwrap();
        for _ in 0..5 {
            sim.step(1.0);
            fresh.step(1.0);
            assert_eq!(
                fresh.read(PointId(2)).unwrap(),
                sim.read(PointId(2)).unwrap()
            );
        }
    }

    /// A `flow_sum` reading `inputs` and driving point 20.
    fn flow_sum_map(inputs: &[u64], bias: f64) -> ChannelMap {
        let mut map = ChannelMap::new().with_point(float_point(20, Direction::In));
        for &point in inputs {
            map = map.with_point(float_point(point, Direction::In));
        }
        map.with_element(ProcessElement::FlowSum(FlowSum {
            inputs: inputs.iter().map(|&point| PointId(point)).collect(),
            output: PointId(20),
            bias,
            initial: -1.0,
        }))
    }

    #[test]
    fn flow_sum_sums_declared_inputs_and_bias() {
        let sim = SimDriver::new(flow_sum_map(&[1, 2, 3], 4.0)).unwrap();
        // Before the first step the output holds the declared initial.
        assert_eq!(sim.read(PointId(20)).unwrap().value, Value::Float(-1.0));

        sim.write(PointId(1), Value::Float(2.0)).unwrap();
        sim.write(PointId(2), Value::Float(-5.0)).unwrap();
        // Point 3 still holds its neutral 0.0.
        sim.step(1.0);
        assert_eq!(sim.read(PointId(20)).unwrap().value, Value::Float(1.0));

        // The sum is read at each tick boundary — a changed input lands
        // on the next step, and dt does not scale it.
        sim.write(PointId(3), Value::Float(0.25)).unwrap();
        sim.step(0.5);
        assert_eq!(sim.read(PointId(20)).unwrap().value, Value::Float(1.25));
    }

    #[test]
    fn flow_sum_with_no_inputs_outputs_its_bias() {
        // An empty list declares a constant — the bias alone.
        let sim = SimDriver::new(flow_sum_map(&[], 4.0)).unwrap();
        sim.step(1.0);
        assert_eq!(sim.read(PointId(20)).unwrap().value, Value::Float(4.0));
    }

    #[test]
    fn a_saturating_flow_sum_marks_the_output_bad_and_recovers() {
        // Two finite inputs can sum past the f64 range — and +inf −
        // −inf is the reachable NaN a saturating pair approaches. The
        // verdict matches the scalar elements': the output holds its
        // last finite total `Bad`/`out_of_range` until a step whose
        // inputs sum finite resumes it.
        let sim = SimDriver::new(flow_sum_map(&[1, 2], 0.0)).unwrap();
        sim.write(PointId(1), Value::Float(f64::MAX)).unwrap();
        sim.write(PointId(2), Value::Float(f64::MAX)).unwrap();
        sim.step(1.0);
        let sample = sim.read(PointId(20)).unwrap();
        // The declared initial stands — the overflow committed nothing.
        assert_eq!(sample.value, Value::Float(-1.0));
        assert_eq!(sample.quality, Quality::Bad(QualityReason::OutOfRange));

        sim.write(PointId(2), Value::Float(-f64::MAX)).unwrap();
        sim.step(1.0);
        let sample = sim.read(PointId(20)).unwrap();
        assert_eq!(sample.value, Value::Float(0.0));
        assert!(sample.quality.is_good());
    }

    #[test]
    fn non_good_flow_sum_input_freezes_output_and_propagates_worst_quality() {
        let sim = SimDriver::new(flow_sum_map(&[1, 2], 0.0)).unwrap();
        sim.write(PointId(1), Value::Float(2.0)).unwrap();
        sim.write(PointId(2), Value::Float(3.0)).unwrap();
        sim.step(1.0);
        assert_eq!(sim.read(PointId(20)).unwrap().value, Value::Float(5.0));

        // One degraded input freezes the sum and propagates its
        // quality; a write behind the fault is stored but not consumed.
        let uncertain = Quality::Uncertain(QualityReason::Stale);
        sim.inject_fault(PointId(1), Fault::Quality(uncertain))
            .unwrap();
        sim.write(PointId(1), Value::Float(10.0)).unwrap();
        sim.step(1.0);
        let sample = sim.read(PointId(20)).unwrap();
        assert_eq!(sample.value, Value::Float(5.0));
        assert_eq!(sample.quality, uncertain);

        // With two degraded inputs the output reports the worst of
        // their qualities.
        sim.inject_fault(
            PointId(2),
            Fault::Quality(Quality::Bad(QualityReason::DeviceFault)),
        )
        .unwrap();
        sim.step(1.0);
        let sample = sim.read(PointId(20)).unwrap();
        assert_eq!(sample.value, Value::Float(5.0));
        assert_eq!(sample.quality, Quality::Bad(QualityReason::DeviceFault));

        // Clearing one fault leaves the other propagating; clearing
        // both resumes the sum on the first all-Good step.
        sim.clear_fault(PointId(2)).unwrap();
        sim.step(1.0);
        let sample = sim.read(PointId(20)).unwrap();
        assert_eq!(sample.value, Value::Float(5.0));
        assert_eq!(sample.quality, uncertain);
        sim.clear_fault(PointId(1)).unwrap();
        sim.step(1.0);
        let sample = sim.read(PointId(20)).unwrap();
        assert_eq!(sample.value, Value::Float(13.0));
        assert!(sample.quality.is_good());
    }

    #[test]
    fn flow_sum_rejects_invalid_constants_and_point_kinds() {
        // Inputs must be bound Float points.
        let map = ChannelMap::new()
            .with_point(binding(1, Direction::In, Value::Int(0)))
            .with_point(float_point(20, Direction::In))
            .with_element(ProcessElement::FlowSum(FlowSum {
                inputs: vec![PointId(1)],
                output: PointId(20),
                bias: 0.0,
                initial: 0.0,
            }));
        assert_eq!(
            map.validate().unwrap_err(),
            ConfigError::ElementPointKind {
                point: PointId(1),
                kind: ValueKind::Int,
            }
        );

        // An unbound input is unknown, like any element end.
        let map = ChannelMap::new()
            .with_point(float_point(20, Direction::In))
            .with_element(ProcessElement::FlowSum(FlowSum {
                inputs: vec![PointId(9)],
                output: PointId(20),
                bias: 0.0,
                initial: 0.0,
            }));
        assert_eq!(
            map.validate().unwrap_err(),
            ConfigError::UnknownPoint(PointId(9))
        );

        // The driven point must be Float.
        let map = ChannelMap::new()
            .with_point(float_point(1, Direction::In))
            .with_point(binding(20, Direction::In, Value::Bool(false)))
            .with_element(ProcessElement::FlowSum(FlowSum {
                inputs: vec![PointId(1)],
                output: PointId(20),
                bias: 0.0,
                initial: 0.0,
            }));
        assert_eq!(
            map.validate().unwrap_err(),
            ConfigError::ElementPointKind {
                point: PointId(20),
                kind: ValueKind::Bool,
            }
        );

        // The bias must be finite.
        for bias in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
            assert!(matches!(
                flow_sum_map(&[1], bias).validate().unwrap_err(),
                ConfigError::NonFiniteBias {
                    point: PointId(20),
                    value,
                } if !value.is_finite()
            ));
        }

        // The initial value must be finite.
        let mut map = flow_sum_map(&[1], 0.0);
        map.elements = vec![ProcessElement::FlowSum(FlowSum {
            inputs: vec![PointId(1)],
            output: PointId(20),
            bias: 0.0,
            initial: f64::INFINITY,
        })];
        assert!(matches!(
            map.validate().unwrap_err(),
            ConfigError::NonFiniteInitial { point, .. } if point == PointId(20)
        ));
    }

    #[test]
    fn flow_sum_serde_roundtrips() {
        let map = flow_sum_map(&[1, 2, 3], 4.0);
        let json = serde_json::to_string(&map).unwrap();
        assert!(json.contains("\"flow_sum\""), "{json}");
        assert_eq!(serde_json::from_str::<ChannelMap>(&json).unwrap(), map);

        // `bias` is optional in a document, deserializing as zero.
        let element: ProcessElement =
            serde_json::from_str(r#"{"flow_sum":{"inputs":[1],"output":20,"initial":0.0}}"#)
                .unwrap();
        let ProcessElement::FlowSum(sum) = element else {
            panic!("the declaration is a flow_sum")
        };
        assert_eq!(sum.bias, 0.0);
    }

    #[test]
    fn captured_state_restores_flow_sum_output_mid_run() {
        // The element's only state is its standing output; a captured
        // run continues identically through a fresh driver.
        let map = flow_sum_map(&[1, 2], 0.5);
        let sim = SimDriver::new(map.clone()).unwrap();
        sim.write(PointId(1), Value::Float(2.0)).unwrap();
        sim.write(PointId(2), Value::Float(3.0)).unwrap();
        sim.step(1.0);
        let state = sim.capture_state().unwrap();

        let fresh = SimDriver::new(map).unwrap();
        fresh.restore_state(&state).unwrap();
        for _ in 0..5 {
            sim.step(1.0);
            fresh.step(1.0);
            assert_eq!(
                fresh.read(PointId(20)).unwrap(),
                sim.read(PointId(20)).unwrap()
            );
        }
    }

    /// The station loop this vocabulary exists for: a declared inflow
    /// and two Bool-gated pump draws summed into an integrator driving
    /// the level point — the same declaration
    /// `fixtures/pump_station_dynamics.json` carries.
    fn station_map() -> ChannelMap {
        ChannelMap::new()
            .with_point(float_point(10, Direction::In)) // well level
            .with_point(float_point(11, Direction::In)) // inflow
            .with_point(float_point(12, Direction::In)) // pump-1 draw
            .with_point(float_point(13, Direction::In)) // pump-2 draw
            .with_point(float_point(14, Direction::In)) // net flow
            .with_point(binding(20, Direction::Out, Value::Bool(false))) // pump-1 command
            .with_point(binding(21, Direction::Out, Value::Bool(false))) // pump-2 command
            .with_element(ProcessElement::BoolFlow(BoolFlow {
                input: PointId(20),
                output: PointId(12),
                on_rate: -10.0,
                off_rate: 0.0,
                initial: 0.0,
            }))
            .with_element(ProcessElement::BoolFlow(BoolFlow {
                input: PointId(21),
                output: PointId(13),
                on_rate: -10.0,
                off_rate: 0.0,
                initial: 0.0,
            }))
            .with_element(ProcessElement::FlowSum(FlowSum {
                inputs: vec![PointId(11), PointId(12), PointId(13)],
                output: PointId(14),
                bias: 4.0,
                initial: 4.0,
            }))
            .with_element(ProcessElement::Integrator(Integrator {
                input: PointId(14),
                output: PointId(10),
                initial: 50.0,
            }))
    }

    fn level(sim: &SimDriver) -> f64 {
        let Value::Float(level) = sim.read(PointId(10)).unwrap().value else {
            panic!("the level point is Float")
        };
        level
    }

    #[test]
    fn asserted_pump_commands_drain_the_integrator_while_they_stand() {
        let sim = SimDriver::new(station_map()).unwrap();
        // The integrator seeds the level at its declared initial.
        assert_eq!(level(&sim), 50.0);

        // Idle station: the declared inflow alone — net +4 per time
        // unit — climbs the integrator.
        sim.step(1.0);
        assert_eq!(level(&sim), 54.0);

        // Asserting pump 1's command drains at the declared draw each
        // step it stands: net 4 - 10 = -6 per time unit.
        sim.write(PointId(20), Value::Bool(true)).unwrap();
        for expected in [48.0, 42.0, 36.0] {
            sim.step(1.0);
            assert_eq!(level(&sim), expected);
        }

        // A second running pump doubles the draw: net 4 - 20 = -16.
        sim.write(PointId(21), Value::Bool(true)).unwrap();
        sim.step(1.0);
        assert_eq!(level(&sim), 20.0);

        // Releasing both commands stops the draw at the next tick
        // boundary; the level climbs on inflow alone again.
        sim.write(PointId(20), Value::Bool(false)).unwrap();
        sim.write(PointId(21), Value::Bool(false)).unwrap();
        sim.step(1.0);
        assert_eq!(level(&sim), 24.0);

        // A written inflow joins the same balance: net 8 + 4 = +12.
        sim.write(PointId(11), Value::Float(8.0)).unwrap();
        sim.step(1.0);
        assert_eq!(level(&sim), 36.0);
    }

    /// One scripted run over the station loop, returning the level
    /// sequence — the output identical runs and restored runs must
    /// reproduce.
    fn station_run(sim: &SimDriver) -> Vec<Sample> {
        let mut samples = Vec::new();
        for (pump_one, pump_two) in [(false, false), (true, false), (true, true), (false, true)] {
            sim.write(PointId(20), Value::Bool(pump_one)).unwrap();
            sim.write(PointId(21), Value::Bool(pump_two)).unwrap();
            for _ in 0..3 {
                sim.step(0.5);
                samples.push(sim.read(PointId(10)).unwrap());
            }
        }
        samples
    }

    #[test]
    fn identical_station_runs_produce_identical_samples() {
        let first = station_run(&SimDriver::new(station_map()).unwrap());
        let second = station_run(&SimDriver::new(station_map()).unwrap());
        assert_eq!(first, second);
        // The run actually moved the level in both directions.
        let levels: Vec<f64> = first
            .iter()
            .map(|sample| match sample.value {
                Value::Float(y) => y,
                _ => panic!("the level point is Float"),
            })
            .collect();
        let start = levels[0];
        assert!(levels.iter().any(|&y| y < start), "{levels:?}");
        assert!(levels.iter().any(|&y| y > start), "{levels:?}");
    }

    #[test]
    fn captured_state_restores_the_station_loop_mid_run() {
        let map = station_map();
        let sim = SimDriver::new(map.clone()).unwrap();
        // Capture mid-run, a pump standing: every element's state —
        // the integrator's level included — crosses to the fresh
        // driver, which then continues the sequence identically.
        sim.write(PointId(20), Value::Bool(true)).unwrap();
        for _ in 0..3 {
            sim.step(0.5);
        }
        let state = sim.capture_state().unwrap();

        let fresh = SimDriver::new(map).unwrap();
        fresh.restore_state(&state).unwrap();
        assert_eq!(level(&fresh), level(&sim));
        let continued = station_run(&sim);
        let restored = station_run(&fresh);
        assert_eq!(restored, continued);
    }

    /// The checked-in dynamics document this issue adds: the station
    /// loop as declared data, the same file `dcs-plant-server
    /// --dynamics` merges.
    const STATION_DYNAMICS: &str = include_str!("../fixtures/pump_station_dynamics.json");

    #[test]
    fn the_station_dynamics_document_declares_the_loop() {
        let elements: Vec<ProcessElement> = serde_json::from_str(STATION_DYNAMICS)
            .expect("the document parses as process-element declarations");
        let [
            ProcessElement::BoolFlow(first),
            ProcessElement::BoolFlow(second),
            ProcessElement::FlowSum(net),
            ProcessElement::Integrator(well),
        ] = elements.as_slice()
        else {
            panic!("the document is two bool_flows, a flow_sum, and an integrator")
        };
        assert_eq!((first.input, first.output), (PointId(20), PointId(12)));
        assert_eq!((second.input, second.output), (PointId(21), PointId(13)));
        assert_eq!(
            (net.inputs.as_slice(), net.output),
            (&[PointId(11), PointId(12), PointId(13)][..], PointId(14))
        );
        assert_eq!((well.input, well.output), (PointId(14), PointId(10)));

        // Merged onto the station's bound points, the document builds
        // the identical map the coded constructor does.
        let mut map = ChannelMap::new()
            .with_point(float_point(10, Direction::In))
            .with_point(float_point(11, Direction::In))
            .with_point(float_point(12, Direction::In))
            .with_point(float_point(13, Direction::In))
            .with_point(float_point(14, Direction::In))
            .with_point(binding(20, Direction::Out, Value::Bool(false)))
            .with_point(binding(21, Direction::Out, Value::Bool(false)));
        for element in elements {
            map = map.with_element(element);
        }
        assert_eq!(map, station_map());

        // ...and the merged map runs the loop.
        let sim = SimDriver::new(map).unwrap();
        sim.write(PointId(20), Value::Bool(true)).unwrap();
        sim.step(1.0);
        assert_eq!(level(&sim), 44.0);
    }

    /// A `scaled_flow` element scaling an analog demand `Out` point
    /// onto a driven `Float` rate.
    fn scaled_flow_map(gain: f64) -> ChannelMap {
        ChannelMap::new()
            .with_point(float_point(1, Direction::Out))
            .with_point(float_point(2, Direction::In))
            .with_element(ProcessElement::ScaledFlow(ScaledFlow {
                input: PointId(1),
                output: PointId(2),
                gain,
                initial: -1.0,
            }))
    }

    #[test]
    fn scaled_flow_drives_gain_times_the_input_at_tick_boundaries() {
        let sim = SimDriver::new(scaled_flow_map(0.5)).unwrap();
        // Before the first step the output holds the declared initial.
        assert_eq!(sim.read(PointId(2)).unwrap().value, Value::Float(-1.0));

        // The output stands at gain × the standing demand from the
        // first step reading it — a rate, so `dt` does not scale it.
        sim.write(PointId(1), Value::Float(50.0)).unwrap();
        for dt in [1.0, 0.25, 3.0] {
            sim.step(dt);
            assert_eq!(sim.read(PointId(2)).unwrap().value, Value::Float(25.0));
        }

        // A changed demand lands at the next tick boundary.
        sim.write(PointId(1), Value::Float(20.0)).unwrap();
        sim.step(1.0);
        assert_eq!(sim.read(PointId(2)).unwrap().value, Value::Float(10.0));

        // A negative gain declares a draw: the output is the signed
        // rate while the demand stands.
        let draw = SimDriver::new(scaled_flow_map(-0.5)).unwrap();
        draw.write(PointId(1), Value::Float(50.0)).unwrap();
        draw.step(1.0);
        assert_eq!(draw.read(PointId(2)).unwrap().value, Value::Float(-25.0));
    }

    #[test]
    fn non_good_scaled_flow_input_freezes_output_and_propagates_quality() {
        let sim = SimDriver::new(scaled_flow_map(0.5)).unwrap();
        sim.write(PointId(1), Value::Float(50.0)).unwrap();
        sim.step(1.0);
        assert_eq!(sim.read(PointId(2)).unwrap().value, Value::Float(25.0));

        let quality = Quality::Bad(QualityReason::CommunicationFault);
        sim.inject_fault(PointId(1), Fault::Quality(quality))
            .unwrap();
        // A write behind the fault is stored but not consumed.
        sim.write(PointId(1), Value::Float(80.0)).unwrap();
        for _ in 0..2 {
            sim.step(1.0);
            let sample = sim.read(PointId(2)).unwrap();
            // The documented rule: a non-Good input freezes the
            // output and propagates its quality.
            assert_eq!(sample.value, Value::Float(25.0));
            assert_eq!(sample.quality, quality);
        }

        // Clearing the fault resumes the scaling on the first Good
        // step: the stored 80.0 drives gain × 80.
        sim.clear_fault(PointId(1)).unwrap();
        sim.step(1.0);
        let sample = sim.read(PointId(2)).unwrap();
        assert_eq!(sample.value, Value::Float(40.0));
        assert!(sample.quality.is_good());
    }

    #[test]
    fn scaled_flow_rejects_invalid_gain_and_point_kinds() {
        // The gain must be finite; the rejection names the element's
        // driven point.
        for gain in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
            assert!(matches!(
                scaled_flow_map(gain).validate().unwrap_err(),
                ConfigError::InvalidGain {
                    point: PointId(2),
                    value,
                } if !value.is_finite()
            ));
        }

        // A negative gain is a declared draw and a zero gain a
        // stopped actuator — signed rates, not errors.
        assert!(scaled_flow_map(-10.0).validate().is_ok());
        assert!(scaled_flow_map(0.0).validate().is_ok());

        // The input must be a Float point.
        let map = ChannelMap::new()
            .with_point(binding(1, Direction::Out, Value::Bool(false)))
            .with_point(float_point(2, Direction::In))
            .with_element(ProcessElement::ScaledFlow(ScaledFlow {
                input: PointId(1),
                output: PointId(2),
                gain: 0.5,
                initial: 0.0,
            }));
        assert_eq!(
            map.validate().unwrap_err(),
            ConfigError::ElementPointKind {
                point: PointId(1),
                kind: ValueKind::Bool,
            }
        );

        // The driven point must be Float.
        let map = ChannelMap::new()
            .with_point(float_point(1, Direction::Out))
            .with_point(binding(2, Direction::In, Value::Bool(false)))
            .with_element(ProcessElement::ScaledFlow(ScaledFlow {
                input: PointId(1),
                output: PointId(2),
                gain: 0.5,
                initial: 0.0,
            }));
        assert_eq!(
            map.validate().unwrap_err(),
            ConfigError::ElementPointKind {
                point: PointId(2),
                kind: ValueKind::Bool,
            }
        );

        // An unbound end is unknown, like any element end.
        let map = ChannelMap::new()
            .with_point(float_point(2, Direction::In))
            .with_element(ProcessElement::ScaledFlow(ScaledFlow {
                input: PointId(9),
                output: PointId(2),
                gain: 0.5,
                initial: 0.0,
            }));
        assert_eq!(
            map.validate().unwrap_err(),
            ConfigError::UnknownPoint(PointId(9))
        );

        // The initial value must be finite.
        let mut map = scaled_flow_map(0.5);
        map.elements = vec![ProcessElement::ScaledFlow(ScaledFlow {
            input: PointId(1),
            output: PointId(2),
            gain: 0.5,
            initial: f64::INFINITY,
        })];
        assert!(matches!(
            map.validate().unwrap_err(),
            ConfigError::NonFiniteInitial { point, .. } if point == PointId(2)
        ));
    }

    #[test]
    fn scaled_flow_serde_roundtrips() {
        let map = scaled_flow_map(-0.5);
        let json = serde_json::to_string(&map).unwrap();
        assert!(json.contains("\"scaled_flow\""), "{json}");
        assert_eq!(serde_json::from_str::<ChannelMap>(&json).unwrap(), map);
    }

    #[test]
    fn captured_state_restores_scaled_flow_output_mid_run() {
        // The element's only state is its standing output; a captured
        // run continues identically through a fresh driver.
        let map = scaled_flow_map(0.5);
        let sim = SimDriver::new(map.clone()).unwrap();
        sim.write(PointId(1), Value::Float(50.0)).unwrap();
        sim.step(1.0);
        let state = sim.capture_state().unwrap();

        let fresh = SimDriver::new(map).unwrap();
        fresh.restore_state(&state).unwrap();
        for _ in 0..5 {
            sim.step(1.0);
            fresh.step(1.0);
            assert_eq!(
                fresh.read(PointId(2)).unwrap(),
                sim.read(PointId(2)).unwrap()
            );
        }
    }

    /// A `threshold` element: the Float input on point 1 driving the
    /// Bool contact on point 2.
    fn threshold_map(on: f64, off: f64, initial: bool) -> ChannelMap {
        ChannelMap::new()
            .with_point(float_point(1, Direction::In))
            .with_point(binding(2, Direction::In, Value::Bool(false)))
            .with_element(ProcessElement::Threshold(Threshold {
                input: PointId(1),
                output: PointId(2),
                on,
                off,
                initial,
            }))
    }

    fn contact(sim: &SimDriver) -> Sample {
        sim.read(PointId(2)).unwrap()
    }

    #[test]
    fn threshold_asserts_at_the_on_bound_and_releases_below_the_off_bound() {
        // on > off declares a high trip: assert at `u >= on`, release
        // strictly below `off`, hold inside the band.
        let sim = SimDriver::new(threshold_map(8.0, 7.5, false)).unwrap();
        // Before the first step the contact holds the declared initial.
        let sample = contact(&sim);
        assert_eq!(sample.value, Value::Bool(false));
        assert_eq!(sample.tick, Tick::ZERO);

        sim.write(PointId(1), Value::Float(7.9)).unwrap();
        sim.step(1.0);
        assert_eq!(contact(&sim).value, Value::Bool(false));

        // Reaching the bound exactly asserts at that tick boundary.
        sim.write(PointId(1), Value::Float(8.0)).unwrap();
        sim.step(1.0);
        assert_eq!(contact(&sim).value, Value::Bool(true));

        // Inside the band the contact holds — no chatter.
        for u in [7.6, 7.5] {
            sim.write(PointId(1), Value::Float(u)).unwrap();
            sim.step(1.0);
            assert_eq!(contact(&sim).value, Value::Bool(true));
        }

        // Releasing is strictly below `off`: 7.5 held, 7.4 releases.
        sim.write(PointId(1), Value::Float(7.4)).unwrap();
        sim.step(1.0);
        assert_eq!(contact(&sim).value, Value::Bool(false));

        // The released contact stays released through the band until
        // the input reaches `on` again.
        sim.write(PointId(1), Value::Float(7.9)).unwrap();
        sim.step(1.0);
        assert_eq!(contact(&sim).value, Value::Bool(false));
        sim.write(PointId(1), Value::Float(8.0)).unwrap();
        sim.step(1.0);
        assert_eq!(contact(&sim).value, Value::Bool(true));
    }

    #[test]
    fn threshold_falling_trip_asserts_at_on_and_releases_above_off() {
        // on < off declares a low trip: assert at `u <= on`, release
        // strictly above `off`, hold inside the band.
        let sim = SimDriver::new(threshold_map(2.0, 2.5, false)).unwrap();

        sim.write(PointId(1), Value::Float(2.1)).unwrap();
        sim.step(1.0);
        assert_eq!(contact(&sim).value, Value::Bool(false));

        sim.write(PointId(1), Value::Float(2.0)).unwrap();
        sim.step(1.0);
        assert_eq!(contact(&sim).value, Value::Bool(true));

        // 2.5 holds: release is strictly above `off`.
        for u in [2.4, 2.5] {
            sim.write(PointId(1), Value::Float(u)).unwrap();
            sim.step(1.0);
            assert_eq!(contact(&sim).value, Value::Bool(true));
        }

        sim.write(PointId(1), Value::Float(2.6)).unwrap();
        sim.step(1.0);
        assert_eq!(contact(&sim).value, Value::Bool(false));

        // `dt` scales nothing here — the contact is a pure function of
        // the standing input at each tick boundary.
        sim.write(PointId(1), Value::Float(2.1)).unwrap();
        sim.step(0.25);
        assert_eq!(contact(&sim).value, Value::Bool(false));
        sim.step(3.0);
        assert_eq!(contact(&sim).value, Value::Bool(false));
    }

    #[test]
    fn non_finite_writes_are_refused_and_the_stored_sample_stands() {
        // NaN and the infinities have no JSON spelling — serde emits
        // `null` — so a `Float` point refuses them at the boundary
        // rather than storing a sample no wire contract can carry back.
        // The refusal is `IoError::InvalidValue` and the stored sample
        // is untouched, so a NaN threshold input — which would satisfy
        // no comparison and hold the contact — can never be staged.
        let sim = SimDriver::new(threshold_map(8.0, 7.5, true)).unwrap();
        // Seat the input inside the hysteresis band: the sample the
        // refused writes must leave standing.
        sim.write(PointId(1), Value::Float(7.6)).unwrap();
        for value in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
            assert_eq!(
                sim.write(PointId(1), Value::Float(value)),
                Err(IoError::InvalidValue { point: PointId(1) })
            );
        }
        sim.step(1.0);
        // The held 7.6 — never replaced — is what the element
        // evaluated: inside the band the contact holds its initial.
        let sample = contact(&sim);
        assert_eq!(sample.value, Value::Bool(true));
        assert!(sample.quality.is_good());
        assert_eq!(
            sim.read(PointId(1)).unwrap(),
            Sample::good(Value::Float(7.6), Tick::ZERO)
        );
    }

    #[test]
    fn non_good_threshold_input_holds_the_contact_and_propagates_quality() {
        let sim = SimDriver::new(threshold_map(8.0, 7.5, false)).unwrap();
        sim.write(PointId(1), Value::Float(9.0)).unwrap();
        sim.step(1.0);
        assert_eq!(contact(&sim).value, Value::Bool(true));

        let quality = Quality::Bad(QualityReason::CommunicationFault);
        sim.inject_fault(PointId(1), Fault::Quality(quality))
            .unwrap();
        // A write behind the fault is stored but not consumed — the
        // releasing value must not move the frozen contact.
        sim.write(PointId(1), Value::Float(0.0)).unwrap();
        for _ in 0..2 {
            sim.step(1.0);
            let sample = contact(&sim);
            // The documented rule: a non-Good input freezes the
            // contact state and propagates its quality to the
            // driven Bool point.
            assert_eq!(sample.value, Value::Bool(true));
            assert_eq!(sample.quality, quality);
        }

        // Clearing the fault resumes evaluation on the first Good
        // step: the stored 0.0 releases the contact.
        sim.clear_fault(PointId(1)).unwrap();
        sim.step(1.0);
        let sample = contact(&sim);
        assert_eq!(sample.value, Value::Bool(false));
        assert!(sample.quality.is_good());
    }

    #[test]
    fn threshold_rejects_invalid_bounds_and_point_kinds() {
        // Bounds must be finite; the rejection names the element's
        // driven point and which declared bound offended.
        for (bound, value) in [
            ("on", f64::NAN),
            ("on", f64::INFINITY),
            ("off", f64::NEG_INFINITY),
        ] {
            let mut threshold = Threshold {
                input: PointId(1),
                output: PointId(2),
                on: 8.0,
                off: 7.5,
                initial: false,
            };
            match bound {
                "on" => threshold.on = value,
                _ => threshold.off = value,
            }
            let map = ChannelMap::new()
                .with_point(float_point(1, Direction::In))
                .with_point(binding(2, Direction::In, Value::Bool(false)))
                .with_element(ProcessElement::Threshold(threshold));
            assert!(matches!(
                map.validate().unwrap_err(),
                ConfigError::InvalidBound {
                    point: PointId(2),
                    bound: name,
                    value: rejected,
                } if name == bound && !rejected.is_finite()
            ));
        }

        // Equal bounds declare no hysteresis band.
        let map = ChannelMap::new()
            .with_point(float_point(1, Direction::In))
            .with_point(binding(2, Direction::In, Value::Bool(false)))
            .with_element(ProcessElement::Threshold(Threshold {
                input: PointId(1),
                output: PointId(2),
                on: 7.5,
                off: 7.5,
                initial: false,
            }));
        assert_eq!(
            map.validate().unwrap_err(),
            ConfigError::NonPositiveBand {
                point: PointId(2),
                on: 7.5,
                off: 7.5,
            }
        );

        // The input must be a Float point.
        let map = ChannelMap::new()
            .with_point(binding(1, Direction::In, Value::Bool(false)))
            .with_point(binding(2, Direction::In, Value::Bool(false)))
            .with_element(ProcessElement::Threshold(Threshold {
                input: PointId(1),
                output: PointId(2),
                on: 8.0,
                off: 7.5,
                initial: false,
            }));
        assert_eq!(
            map.validate().unwrap_err(),
            ConfigError::ElementPointKind {
                point: PointId(1),
                kind: ValueKind::Bool,
            }
        );

        // The driven contact must be a Bool point.
        let map = ChannelMap::new()
            .with_point(float_point(1, Direction::In))
            .with_point(float_point(2, Direction::In))
            .with_element(ProcessElement::Threshold(Threshold {
                input: PointId(1),
                output: PointId(2),
                on: 8.0,
                off: 7.5,
                initial: false,
            }));
        assert_eq!(
            map.validate().unwrap_err(),
            ConfigError::ElementContactKind {
                point: PointId(2),
                kind: ValueKind::Float,
            }
        );
    }

    #[test]
    fn threshold_accessors_cover_the_variant() {
        let element = ProcessElement::Threshold(Threshold {
            input: PointId(1),
            output: PointId(2),
            on: 8.0,
            off: 7.5,
            initial: true,
        });
        assert_eq!(element.input(), Some(PointId(1)));
        assert_eq!(element.inputs(), &[PointId(1)]);
        assert_eq!(element.output(), PointId(2));
        assert_eq!(element.initial(), Value::Bool(true));
    }

    #[test]
    fn threshold_serde_roundtrips() {
        let map = threshold_map(8.0, 7.5, true);
        let json = serde_json::to_string(&map).unwrap();
        assert!(json.contains("\"threshold\""), "{json}");
        assert_eq!(serde_json::from_str::<ChannelMap>(&json).unwrap(), map);
    }

    #[test]
    fn captured_state_restores_threshold_contact_mid_run() {
        // The element's only state is its standing contact; a captured
        // run continues identically through a fresh driver.
        let map = threshold_map(8.0, 7.5, false);
        let sim = SimDriver::new(map.clone()).unwrap();
        sim.write(PointId(1), Value::Float(9.0)).unwrap();
        sim.step(1.0);
        let state = sim.capture_state().unwrap();
        assert_eq!(state.get("element.2"), Some(Value::Bool(true)));

        let fresh = SimDriver::new(map).unwrap();
        fresh.restore_state(&state).unwrap();
        // The restored driver holds the asserted contact inside the
        // band exactly as the original does.
        sim.write(PointId(1), Value::Float(7.9)).unwrap();
        fresh.write(PointId(1), Value::Float(7.9)).unwrap();
        for _ in 0..5 {
            sim.step(1.0);
            fresh.step(1.0);
            assert_eq!(
                fresh.read(PointId(2)).unwrap(),
                sim.read(PointId(2)).unwrap()
            );
        }
    }

    /// The protection loop this element exists for: a `threshold`
    /// watches the well level and asserts the `sis-active` Bool
    /// contact, which gates a `bool_flow` emergency draw; a
    /// `flow_sum` and `integrator` close the level process. The same
    /// declaration `fixtures/protection_dynamics.json` carries.
    fn protection_map() -> ChannelMap {
        ChannelMap::new()
            .with_point(float_point(10, Direction::In)) // well level
            .with_point(float_point(11, Direction::In)) // inflow
            .with_point(float_point(12, Direction::In)) // emergency draw
            .with_point(float_point(13, Direction::In)) // net flow
            .with_point(binding(30, Direction::In, Value::Bool(false))) // sis-active
            .with_element(ProcessElement::Threshold(Threshold {
                input: PointId(10),
                output: PointId(30),
                on: 8.0,
                off: 7.5,
                initial: false,
            }))
            .with_element(ProcessElement::BoolFlow(BoolFlow {
                input: PointId(30),
                output: PointId(12),
                on_rate: -20.0,
                off_rate: 0.0,
                initial: 0.0,
            }))
            .with_element(ProcessElement::FlowSum(FlowSum {
                inputs: vec![PointId(11), PointId(12)],
                output: PointId(13),
                bias: 4.0,
                initial: 4.0,
            }))
            .with_element(ProcessElement::Integrator(Integrator {
                input: PointId(13),
                output: PointId(10),
                initial: 6.0,
            }))
    }

    fn sis_active(sim: &SimDriver) -> bool {
        let Value::Bool(contact) = sim.read(PointId(30)).unwrap().value else {
            panic!("the contact point is Bool")
        };
        contact
    }

    #[test]
    fn the_level_crossing_drives_the_emergency_draw_without_a_script() {
        let sim = SimDriver::new(protection_map()).unwrap();
        assert_eq!(level(&sim), 6.0);
        assert!(!sis_active(&sim));

        // The level climbs on the declared inflow alone; the crossing
        // asserts the contact at the tick boundary after it, engaging
        // the draw on the same step.
        sim.step(1.0);
        assert_eq!(level(&sim), 10.0);
        assert!(!sis_active(&sim));
        sim.step(1.0);
        assert!(sis_active(&sim));
        assert_eq!(level(&sim), -6.0);

        // The draw pulls the level back below `off`; the contact
        // releases cleanly at the next boundary — no chatter, no
        // scheduled script asserted or cleared it.
        sim.step(1.0);
        assert!(!sis_active(&sim));
        assert_eq!(level(&sim), -2.0);
    }

    /// One scripted run over the protection loop, returning the
    /// (level, contact) sequence — the output identical runs and
    /// restored runs must reproduce.
    fn protection_run(sim: &SimDriver) -> Vec<(Sample, Sample)> {
        let mut samples = Vec::new();
        for inflow in [0.0, 2.0, 0.0, -1.0] {
            sim.write(PointId(11), Value::Float(inflow)).unwrap();
            for _ in 0..4 {
                sim.step(0.5);
                samples.push((
                    sim.read(PointId(10)).unwrap(),
                    sim.read(PointId(30)).unwrap(),
                ));
            }
        }
        samples
    }

    #[test]
    fn identical_protection_runs_produce_identical_samples() {
        let first = protection_run(&SimDriver::new(protection_map()).unwrap());
        let second = protection_run(&SimDriver::new(protection_map()).unwrap());
        assert_eq!(first, second);
        // The run actually asserted and released the contact.
        assert!(
            first
                .iter()
                .any(|(_, contact)| contact.value == Value::Bool(true))
        );
        assert!(
            first
                .iter()
                .any(|(_, contact)| contact.value == Value::Bool(false))
        );
    }

    /// The checked-in dynamics document this issue adds: the
    /// protection loop as declared data, the same file
    /// `dcs-plant-server --dynamics` and `dcs-sim-bus-device
    /// --dynamics` merge.
    const PROTECTION_DYNAMICS: &str = include_str!("../fixtures/protection_dynamics.json");

    #[test]
    fn the_protection_dynamics_document_declares_the_loop() {
        let elements: Vec<ProcessElement> = serde_json::from_str(PROTECTION_DYNAMICS)
            .expect("the document parses as process-element declarations");
        let [
            ProcessElement::Threshold(trip),
            ProcessElement::BoolFlow(draw),
            ProcessElement::FlowSum(net),
            ProcessElement::Integrator(well),
        ] = elements.as_slice()
        else {
            panic!("the document is a threshold, a bool_flow, a flow_sum, and an integrator")
        };
        assert_eq!((trip.input, trip.output), (PointId(10), PointId(30)));
        assert_eq!((trip.on, trip.off, trip.initial), (8.0, 7.5, false));
        assert_eq!((draw.input, draw.output), (PointId(30), PointId(12)));
        assert_eq!(
            (net.inputs.as_slice(), net.output),
            (&[PointId(11), PointId(12)][..], PointId(13))
        );
        assert_eq!((well.input, well.output), (PointId(13), PointId(10)));

        // Merged onto the loop's bound points, the document builds the
        // identical map the coded constructor does.
        let mut map = ChannelMap::new()
            .with_point(float_point(10, Direction::In))
            .with_point(float_point(11, Direction::In))
            .with_point(float_point(12, Direction::In))
            .with_point(float_point(13, Direction::In))
            .with_point(binding(30, Direction::In, Value::Bool(false)));
        for element in elements {
            map = map.with_element(element);
        }
        assert_eq!(map, protection_map());

        // ...and the merged map runs the loop.
        let sim = SimDriver::new(map).unwrap();
        for _ in 0..2 {
            sim.step(1.0);
        }
        assert!(sis_active(&sim));
    }

    /// The dosing loop this element exists for: the metering pump's
    /// analog speed demand scaled into the measured discharge rate
    /// and — with a negative gain — into the chemical tank's drawdown
    /// rate, integrated into the tank level. The same declaration
    /// `fixtures/dosing_skid_dynamics.json` carries.
    fn dosing_map() -> ChannelMap {
        ChannelMap::new()
            .with_point(float_point(10, Direction::In)) // tank level
            .with_point(float_point(11, Direction::In)) // measured discharge rate
            .with_point(float_point(12, Direction::In)) // tank draw
            .with_point(float_point(20, Direction::Out)) // speed demand
            .with_element(ProcessElement::ScaledFlow(ScaledFlow {
                input: PointId(20),
                output: PointId(11),
                gain: 0.5,
                initial: 0.0,
            }))
            .with_element(ProcessElement::ScaledFlow(ScaledFlow {
                input: PointId(20),
                output: PointId(12),
                gain: -0.5,
                initial: 0.0,
            }))
            .with_element(ProcessElement::Integrator(Integrator {
                input: PointId(12),
                output: PointId(10),
                initial: 100.0,
            }))
    }

    #[test]
    fn an_analog_demand_drains_the_tank_proportionally_while_it_stands() {
        let sim = SimDriver::new(dosing_map()).unwrap();
        // The integrator seeds the tank at its declared initial level
        // and the rates hold their declared initials.
        assert_eq!(level(&sim), 100.0);
        assert_eq!(sim.read(PointId(11)).unwrap().value, Value::Float(0.0));

        // The zeroed demand drives zero rates; the tank holds.
        sim.step(1.0);
        assert_eq!(level(&sim), 100.0);

        // The demand standing at 50, the discharge reads 0.5 × 50 and
        // the tank draws down the same 25 per time unit, step after
        // step.
        sim.write(PointId(20), Value::Float(50.0)).unwrap();
        sim.step(1.0);
        assert_eq!(sim.read(PointId(11)).unwrap().value, Value::Float(25.0));
        assert_eq!(sim.read(PointId(12)).unwrap().value, Value::Float(-25.0));
        assert_eq!(level(&sim), 75.0);
        sim.step(1.0);
        assert_eq!(level(&sim), 50.0);

        // A changed demand re-scales the draw at the next tick
        // boundary: 0.5 × 20 = 10 per time unit.
        sim.write(PointId(20), Value::Float(20.0)).unwrap();
        sim.step(1.0);
        assert_eq!(sim.read(PointId(11)).unwrap().value, Value::Float(10.0));
        assert_eq!(level(&sim), 40.0);

        // Zeroing the demand stops the draw; the level holds.
        sim.write(PointId(20), Value::Float(0.0)).unwrap();
        sim.step(1.0);
        assert_eq!(sim.read(PointId(11)).unwrap().value, Value::Float(0.0));
        assert_eq!(level(&sim), 40.0);
    }

    /// One scripted run over the dosing loop, returning the level and
    /// discharge sequence identical runs and restored runs must
    /// reproduce.
    fn dosing_run(sim: &SimDriver) -> Vec<Sample> {
        let mut samples = Vec::new();
        for demand in [0.0, 50.0, 20.0, 0.0] {
            sim.write(PointId(20), Value::Float(demand)).unwrap();
            for _ in 0..3 {
                sim.step(0.5);
                samples.push(sim.read(PointId(10)).unwrap());
                samples.push(sim.read(PointId(11)).unwrap());
            }
        }
        samples
    }

    #[test]
    fn identical_dosing_runs_produce_identical_samples() {
        let first = dosing_run(&SimDriver::new(dosing_map()).unwrap());
        let second = dosing_run(&SimDriver::new(dosing_map()).unwrap());
        assert_eq!(first, second);
        // The run actually drew the tank down.
        let levels: Vec<f64> = first
            .iter()
            .step_by(2)
            .map(|sample| match sample.value {
                Value::Float(y) => y,
                _ => panic!("the level point is Float"),
            })
            .collect();
        assert!(levels.iter().any(|&y| y < levels[0]), "{levels:?}");
    }

    #[test]
    fn captured_state_restores_the_dosing_loop_mid_run() {
        let map = dosing_map();
        let sim = SimDriver::new(map.clone()).unwrap();
        // Capture mid-run, the demand standing: every element's state
        // — the integrator's level included — crosses to the fresh
        // driver, which then continues the sequence identically.
        sim.write(PointId(20), Value::Float(50.0)).unwrap();
        for _ in 0..3 {
            sim.step(0.5);
        }
        let state = sim.capture_state().unwrap();

        let fresh = SimDriver::new(map).unwrap();
        fresh.restore_state(&state).unwrap();
        assert_eq!(level(&fresh), level(&sim));
        let continued = dosing_run(&sim);
        let restored = dosing_run(&fresh);
        assert_eq!(restored, continued);
    }

    /// The checked-in dynamics document this issue adds: the dosing
    /// loop as declared data, the same file `dcs-plant-server
    /// --dynamics` and `dcs-sim-bus-device --dynamics` merge.
    const DOSING_DYNAMICS: &str = include_str!("../fixtures/dosing_skid_dynamics.json");

    #[test]
    fn the_dosing_dynamics_document_declares_the_loop() {
        let elements: Vec<ProcessElement> = serde_json::from_str(DOSING_DYNAMICS)
            .expect("the document parses as process-element declarations");
        let [
            ProcessElement::ScaledFlow(discharge),
            ProcessElement::ScaledFlow(draw),
            ProcessElement::Integrator(tank),
        ] = elements.as_slice()
        else {
            panic!("the document is two scaled_flows and an integrator")
        };
        assert_eq!(
            (discharge.input, discharge.output, discharge.gain),
            (PointId(20), PointId(11), 0.5)
        );
        assert_eq!(
            (draw.input, draw.output, draw.gain),
            (PointId(20), PointId(12), -0.5)
        );
        assert_eq!((tank.input, tank.output), (PointId(12), PointId(10)));

        // Merged onto the skid's bound points, the document builds
        // the identical map the coded constructor does.
        let mut map = ChannelMap::new()
            .with_point(float_point(10, Direction::In))
            .with_point(float_point(11, Direction::In))
            .with_point(float_point(12, Direction::In))
            .with_point(float_point(20, Direction::Out));
        for element in elements {
            map = map.with_element(element);
        }
        assert_eq!(map, dosing_map());

        // ...and the merged map runs the loop.
        let sim = SimDriver::new(map).unwrap();
        sim.write(PointId(20), Value::Float(50.0)).unwrap();
        sim.step(1.0);
        assert_eq!(level(&sim), 75.0);
    }
}
