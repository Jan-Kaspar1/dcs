//! The [`SimDriver`] backend: point storage, loopback routing, process
//! element stepping, and fault injection behind the [`IoDriver`] boundary.

use crate::map::{ChannelMap, ConfigError, Loopback, ProcessElement};
use dcs_core::{IoDriver, IoError, PointId, Quality, Sample, Tick, Value, ValueKind};
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

/// One bound point's runtime state.
struct PointState {
    /// The point's declared kind, taken from the binding's initial value.
    kind: ValueKind,
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
    y: f64,
    /// The delay line a [`ProcessElement::DeadTime`] advances; `None` for
    /// the scalar elements.
    delay_line: Option<DelayLine>,
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
    /// Advances the element's state one step of `dt` given a `Good` input
    /// `u`, returning the new output.
    ///
    /// The lag uses the exact discretization `y += (1 - e^{-dt/τ})(u - y)`,
    /// stable for every non-negative `dt`; the integrator uses Euler's
    /// `y += u·dt`; the dead-time element pushes `u` onto its delay line
    /// at the new time and outputs the newest sample at or before
    /// `t - delay`. All are pure functions of their arguments and stored
    /// state, keeping stepping deterministic.
    fn advance(&mut self, u: f64, dt: f64) -> f64 {
        match self.element {
            ProcessElement::FirstOrderLag(element) => {
                self.y + (1.0 - (-dt / element.time_constant).exp()) * (u - self.y)
            }
            ProcessElement::Integrator(_) => self.y + u * dt,
            ProcessElement::DeadTime(element) => {
                // Constructed in `SimDriver::new` for every dead-time element.
                let line = self.delay_line.as_mut().unwrap();
                line.t += dt;
                line.history.push_back((line.t, u));
                // The newest sample at or before `t - delay`. The
                // tolerance absorbs float error accumulated in the
                // stored times so a sample recorded exactly on the
                // boundary is delivered on the expected step.
                let target = line.t - element.delay;
                let tolerance = 1e-9 * line.t.abs().max(1.0);
                while line.history.len() > 1 && line.history[1].0 <= target + tolerance {
                    line.history.pop_front();
                }
                line.history[0].1
            }
        }
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
                    sample: Sample::good(binding.initial, Tick::ZERO),
                    fault: None,
                },
            );
        }
        let mut elements = Vec::with_capacity(map.elements.len());
        for element in map.elements {
            let y = element.initial();
            // Validated: element outputs are always bound points.
            points.get_mut(&element.output()).unwrap().sample =
                Sample::good(Value::Float(y), Tick::ZERO);
            let delay_line = match element {
                ProcessElement::DeadTime(_) => Some(DelayLine {
                    t: 0.0,
                    history: VecDeque::from([(0.0, y)]),
                }),
                _ => None,
            };
            elements.push(ElementState {
                element,
                y,
                delay_line,
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
    ///    point's effective sample and updates its output point: a `Good`
    ///    input advances the element — for a dead-time element, pushes
    ///    the input onto its delay line — and stamps `Good`; a
    ///    non-`Good` input freezes the element's state, delay-line clock
    ///    included, and propagates its quality to the output sample,
    ///    mirroring the contract's quality propagation.
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
            let input = state.points[&element.element.input()].effective_sample();
            let output = state.points.get_mut(&element.element.output()).unwrap();
            if input.quality.is_good() {
                let Value::Float(u) = input.value else {
                    unreachable!("validated element inputs are Float points")
                };
                element.y = element.advance(u, dt);
                output.sample = Sample::good(Value::Float(element.y), tick);
            } else {
                output.sample = Sample::new(Value::Float(element.y), input.quality, tick);
            }
        }
        tick
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
        point_state.sample = Sample::good(value, state.tick);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::map::{ChannelId, DeadTime, Direction, FirstOrderLag, Integrator, PointBinding};
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
    fn inconsistent_maps_are_rejected() {
        // Two bindings on one point id.
        let map = ChannelMap::new()
            .with_point(float_point(1, Direction::In))
            .with_point(float_point(1, Direction::Out));
        assert_eq!(
            map.validate().unwrap_err(),
            ConfigError::DuplicatePoint(PointId(1))
        );

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

    /// One scripted run over a map with a loopback, a lag, an integrator,
    /// and a dead time: identical writes and steps must replay
    /// identically.
    fn scripted_run() -> Vec<Sample> {
        let map = ChannelMap::new()
            .with_point(float_point(10, Direction::In))
            .with_point(float_point(20, Direction::Out))
            .with_point(float_point(30, Direction::In))
            .with_point(float_point(40, Direction::In))
            .with_point(float_point(50, Direction::In))
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
            }));
        let sim = SimDriver::new(map).unwrap();
        let driver: &dyn IoDriver = &sim;

        let mut samples = Vec::new();
        for command in [1.0_f64, 2.0, 2.0, -1.0, 0.0] {
            driver.write(PointId(20), Value::Float(command)).unwrap();
            sim.step(0.25);
            for point in [10_u64, 30, 40, 50] {
                samples.push(driver.read(PointId(point)).unwrap());
            }
        }
        sim.step(0.5);
        for point in [10_u64, 30, 40, 50] {
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
        assert_eq!(first.len(), 24);
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
}
