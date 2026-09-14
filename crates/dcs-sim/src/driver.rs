//! The [`SimDriver`] backend: point storage, loopback routing, process
//! element stepping, and fault injection behind the [`IoDriver`] boundary.

use crate::map::{ChannelMap, ConfigError, Loopback, ProcessElement};
use dcs_core::{IoDriver, IoError, PointId, Quality, Sample, Tick, Value, ValueKind};
use serde::{Deserialize, Serialize};
use std::cell::RefCell;
use std::collections::HashMap;

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
}

/// Everything behind the driver's `RefCell`.
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
/// The driver uses interior mutability, so typed
/// [`Input`](dcs_core::Input)/[`Output`](dcs_core::Output) handles, the
/// stepping API, and the fault API can all share one `&SimDriver`.
pub struct SimDriver {
    state: RefCell<State>,
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
            elements.push(ElementState { element, y });
        }
        Ok(Self {
            state: RefCell::new(State {
                points,
                loopbacks: map.loopbacks,
                elements,
                tick: Tick::ZERO,
            }),
        })
    }

    /// The driver's current logical tick.
    pub fn tick(&self) -> Tick {
        self.state.borrow().tick
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
    ///    input is integrated and stamps `Good`; a non-`Good` input freezes
    ///    the element's state and propagates its quality to the output
    ///    sample, mirroring the contract's quality propagation.
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
        let state = &mut *self.state.borrow_mut();
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
                element.y = element.element.advance(element.y, u, dt);
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
            .borrow_mut()
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
            .borrow_mut()
            .points
            .get_mut(&point)
            .ok_or(IoError::UnknownPoint(point))?
            .fault = None;
        Ok(())
    }
}

impl IoDriver for SimDriver {
    fn read(&self, point: PointId) -> Result<Sample, IoError> {
        let state = self.state.borrow();
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
        let state = &mut *self.state.borrow_mut();
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
    use crate::map::{ChannelId, Direction, FirstOrderLag, Integrator, PointBinding};
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
    }

    /// One scripted run over a map with a loopback, a lag, and an
    /// integrator: identical writes and steps must replay identically.
    fn scripted_run() -> Vec<Sample> {
        let map = ChannelMap::new()
            .with_point(float_point(10, Direction::In))
            .with_point(float_point(20, Direction::Out))
            .with_point(float_point(30, Direction::In))
            .with_point(float_point(40, Direction::In))
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
            }));
        let sim = SimDriver::new(map).unwrap();
        let driver: &dyn IoDriver = &sim;

        let mut samples = Vec::new();
        for command in [1.0_f64, 2.0, 2.0, -1.0, 0.0] {
            driver.write(PointId(20), Value::Float(command)).unwrap();
            sim.step(0.25);
            for point in [10_u64, 30, 40] {
                samples.push(driver.read(PointId(point)).unwrap());
            }
        }
        sim.step(0.5);
        for point in [10_u64, 30, 40] {
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
        assert_eq!(first.len(), 18);
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
            }));
        let json = serde_json::to_string(&map).unwrap();
        assert_eq!(serde_json::from_str::<ChannelMap>(&json).unwrap(), map);
    }
}
