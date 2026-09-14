//! Rate limiter: an analog output slewing toward its input by a bounded
//! per-tick delta.

use crate::describe;
use crate::params::{self, ParameterError, Parameters};
use dcs_core::{
    CommandError, ComponentDescriptor, PointId, PortRole, Quality, QualityReason, Sample,
    StateError, StateMap, Tick, Value, ValueKind,
};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// A rate limiter: tracks the analog `in` on `out`, moving at most
/// `max_delta` per scan — the tick-domain slew bound.
///
/// The first finite input is adopted outright: initialization is not a
/// slew and the bound does not apply to it. Thereafter each scan adds
/// `clamp(in - out, -max_delta, max_delta)` to the driven value, so the
/// output approaches a stepped input in `ceil(|in - out| / max_delta)`
/// scans and an input changing faster than the bound is followed, never
/// jumped to.
///
/// **Quality rule:** the slew bound holds through degraded quality — a
/// non-`Good` input still slews toward its value by at most
/// `max_delta` per scan, and `out` carries the input's quality so the
/// tracking is visibly untrusted. A non-finite input is the exception:
/// it cannot be approached, so `out` holds its current value, merges
/// `Bad(DeviceFault)` into the quality, and — before any finite input
/// has been seen — passes the input's value through, surfacing the
/// fault rather than inventing a number.
///
/// Declared I/O: `in` (`In`, `Float`) and `out` (`Out`, `Float`).
///
/// Parameters: `max_delta` — required finite `Float` (or losslessly
/// representable `Int`), strictly positive.
#[derive(Debug)]
pub struct RateLimiter {
    name: String,
    input: PointId,
    output: PointId,
    max_delta: f64,
    /// The value `out` currently slews from; `None` until the first
    /// finite input is adopted.
    current: Option<f64>,
}

impl RateLimiter {
    /// The component-kind string the model-driven registry maps onto this
    /// type's constructor.
    pub const KIND: &'static str = "rate-limiter";

    /// Builds the component from explicit points and the per-scan bound.
    ///
    /// `max_delta` must be finite and strictly positive; violations are
    /// reported as a [`ParameterError`].
    pub fn new(
        name: impl Into<String>,
        input: PointId,
        output: PointId,
        max_delta: f64,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        if !max_delta.is_finite() {
            return Err(params::invalid(
                &name,
                "max_delta",
                "must be finite".to_string(),
            ));
        }
        if max_delta <= 0.0 {
            return Err(params::invalid(
                &name,
                "max_delta",
                "must be positive".to_string(),
            ));
        }
        Ok(Self {
            name,
            input,
            output,
            max_delta,
            current: None,
        })
    }

    /// Builds the component from a plant-model parameter map, reading the
    /// parameters listed on the type's docs.
    pub fn from_parameters(
        name: impl Into<String>,
        input: PointId,
        output: PointId,
        parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        let max_delta = params::required_f64(&name, parameters, "max_delta")?;
        Self::new(name, input, output, max_delta)
    }
}

impl Component for RateLimiter {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        vec![
            IoRequirement::input::<f64>("in", self.input),
            IoRequirement::output::<f64>("out", self.output),
        ]
    }

    fn step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError> {
        let sample = io.read_typed::<f64>(self.input)?;
        let mut quality = sample.quality;
        let next = if sample.value.is_finite() {
            Some(match self.current {
                None => sample.value,
                Some(current) => {
                    current + (sample.value - current).clamp(-self.max_delta, self.max_delta)
                }
            })
        } else {
            // A non-finite input cannot be approached: hold (or pass it
            // through before the first finite input).
            quality = quality.merge(Quality::Bad(QualityReason::DeviceFault));
            self.current
        };
        if next.is_some() {
            self.current = next;
        }
        io.write_sample(
            self.output,
            Sample::new(Value::Float(next.unwrap_or(sample.value)), quality, tick),
        )?;
        Ok(())
    }

    /// Describes the limiter: `in` is the process value being followed,
    /// `out` the slew-bounded result; the `max_delta` parameter
    /// `from_parameters` reads.
    fn describe(&self) -> ComponentDescriptor {
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &[("in", PortRole::ProcessValue), ("out", PortRole::Output)],
            vec![describe::parameter(
                "max_delta",
                ValueKind::Float,
                Some(describe::POSITIVE_F64),
            )],
        )
    }

    /// Tunes `max_delta` at the scan boundary; the new bound applies to
    /// the next slew step.
    fn apply_parameter(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        match parameter {
            "max_delta" => {
                let tuned = params::tune_f64(&self.name, parameter, value)?;
                if !tuned.is_finite() || tuned <= 0.0 {
                    return Err(params::invalid_parameter(
                        &self.name,
                        parameter,
                        "must be finite and positive",
                    ));
                }
                self.max_delta = tuned;
            }
            _ => return Err(params::unknown_parameter(&self.name, parameter)),
        }
        Ok(())
    }

    /// Captures the value `out` is slewing from — absent before the
    /// first finite input — plus the tuned `max_delta`, so a
    /// checkpointed limiter continues mid-slew under the same bound.
    fn capture_state(&self) -> StateMap {
        let mut state = StateMap::new();
        if let Some(current) = self.current {
            state.insert("current", Value::Float(current));
        }
        state.insert("max_delta", Value::Float(self.max_delta));
        state
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        state.ensure_known_fields(&self.name, &["current", "max_delta"])?;
        let current = match state.optional_f64(&self.name, "current")? {
            Some(current) if current.is_finite() => Some(current),
            Some(current) => {
                return Err(StateError::InvalidValue {
                    element: self.name.clone(),
                    field: "current".to_string(),
                    value: Value::Float(current),
                });
            }
            None => None,
        };
        let max_delta = state.require_f64(&self.name, "max_delta")?;
        if !max_delta.is_finite() || max_delta <= 0.0 {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "max_delta".to_string(),
                value: Value::Float(max_delta),
            });
        }
        self.current = current;
        self.max_delta = max_delta;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::TestIo;
    use dcs_core::{Direction, ParameterDescriptor, PortDescriptor};
    use dcs_model::{ComponentId, ComponentInstance};
    use std::collections::BTreeMap;

    const IN: PointId = PointId(130);
    const OUT: PointId = PointId(131);

    /// A limiter slewing at most 2.5 units per scan.
    fn component() -> RateLimiter {
        RateLimiter::new("rl", IN, OUT, 2.5).unwrap()
    }

    fn io(field: Sample) -> TestIo {
        TestIo::new(&[
            (IN, Direction::In, field),
            (
                OUT,
                Direction::Out,
                Sample::good(Value::Float(0.0), Tick::ZERO),
            ),
        ])
    }

    fn feed(io: &TestIo, value: f64, tick: u64) {
        io.feed(IN, Sample::good(Value::Float(value), Tick(tick)));
    }

    fn driven(io: &TestIo) -> f64 {
        match io.written(OUT).unwrap().value {
            Value::Float(value) => value,
            other => panic!("out must be Float, found {other:?}"),
        }
    }

    #[test]
    fn first_finite_input_is_adopted() {
        let mut block = component();
        let io = io(Sample::good(Value::Float(10.0), Tick::ZERO));
        block.step(&io, Tick(1)).unwrap();
        let out = io.written(OUT).unwrap();
        assert_eq!(out.value, Value::Float(10.0));
        assert_eq!(out.quality, Quality::Good);
        assert_eq!(out.tick, Tick(1));
    }

    #[test]
    fn slews_toward_the_input_bounded_per_tick() {
        let mut block = component();
        let io = io(Sample::good(Value::Float(0.0), Tick::ZERO));
        block.step(&io, Tick(1)).unwrap();

        // A step to 10.0: the output climbs by exactly max_delta until
        // the remainder is inside the bound.
        feed(&io, 10.0, 2);
        block.step(&io, Tick(2)).unwrap();
        assert_eq!(driven(&io), 2.5);
        feed(&io, 10.0, 3);
        block.step(&io, Tick(3)).unwrap();
        assert_eq!(driven(&io), 5.0);
        feed(&io, 10.0, 4);
        block.step(&io, Tick(4)).unwrap();
        assert_eq!(driven(&io), 7.5);
        feed(&io, 10.0, 5);
        block.step(&io, Tick(5)).unwrap();
        assert_eq!(driven(&io), 10.0);
        // Arrived: tracks exactly.
        feed(&io, 10.0, 6);
        block.step(&io, Tick(6)).unwrap();
        assert_eq!(driven(&io), 10.0);

        // The bound applies downward too.
        feed(&io, 4.0, 7);
        block.step(&io, Tick(7)).unwrap();
        assert_eq!(driven(&io), 7.5);
    }

    #[test]
    fn a_moving_input_is_followed_never_jumped_to() {
        let mut block = component();
        let io = io(Sample::good(Value::Float(0.0), Tick::ZERO));
        block.step(&io, Tick(1)).unwrap();

        // The input retreats while the output chases: each step still
        // moves at most max_delta.
        for (tick, target, expected) in
            [(2, 10.0, 2.5), (3, 5.0, 5.0), (4, 0.0, 2.5), (5, 0.0, 0.0)]
        {
            feed(&io, target, tick);
            block.step(&io, Tick(tick)).unwrap();
            assert_eq!(driven(&io), expected, "tick={tick}");
        }
    }

    #[test]
    fn bad_quality_input_still_slews_bounded_and_propagates_quality() {
        let mut block = component();
        let io = io(Sample::good(Value::Float(0.0), Tick::ZERO));
        block.step(&io, Tick(1)).unwrap();

        // A Bad step to 10.0: the output slews by exactly max_delta,
        // flagged with the input's quality — the bound holds through
        // bad quality.
        io.feed(
            IN,
            Sample::new(
                Value::Float(10.0),
                Quality::Bad(QualityReason::CommunicationFault),
                Tick(2),
            ),
        );
        block.step(&io, Tick(2)).unwrap();
        let out = io.written(OUT).unwrap();
        assert_eq!(out.value, Value::Float(2.5));
        assert_eq!(out.quality, Quality::Bad(QualityReason::CommunicationFault));

        io.feed(
            IN,
            Sample::new(
                Value::Float(10.0),
                Quality::Uncertain(QualityReason::Stale),
                Tick(3),
            ),
        );
        block.step(&io, Tick(3)).unwrap();
        let out = io.written(OUT).unwrap();
        assert_eq!(out.value, Value::Float(5.0));
        assert_eq!(out.quality, Quality::Uncertain(QualityReason::Stale));
    }

    #[test]
    fn non_finite_input_holds_and_marks_bad() {
        let mut block = component();
        let io = io(Sample::good(Value::Float(4.0), Tick::ZERO));
        block.step(&io, Tick(1)).unwrap();

        feed(&io, f64::NAN, 2);
        block.step(&io, Tick(2)).unwrap();
        let out = io.written(OUT).unwrap();
        assert_eq!(out.value, Value::Float(4.0));
        assert_eq!(out.quality, Quality::Bad(QualityReason::DeviceFault));

        // Recovery resumes slewing from the held value.
        feed(&io, 9.0, 3);
        block.step(&io, Tick(3)).unwrap();
        assert_eq!(driven(&io), 6.5);
    }

    #[test]
    fn non_finite_first_input_passes_through_flagged() {
        let mut block = component();
        let io = io(Sample::good(Value::Float(f64::INFINITY), Tick::ZERO));
        block.step(&io, Tick(1)).unwrap();
        let out = io.written(OUT).unwrap();
        assert_eq!(out.value, Value::Float(f64::INFINITY));
        assert_eq!(out.quality, Quality::Bad(QualityReason::DeviceFault));
        // Nothing was adopted: the first finite input still initializes.
        feed(&io, 10.0, 2);
        block.step(&io, Tick(2)).unwrap();
        assert_eq!(driven(&io), 10.0);
    }

    #[test]
    fn restored_limiter_continues_mid_slew() {
        let mut block = component();
        let io = io(Sample::good(Value::Float(0.0), Tick::ZERO));
        block.step(&io, Tick(1)).unwrap();
        feed(&io, 10.0, 2);
        block.step(&io, Tick(2)).unwrap();

        // Checkpoint mid-slew at 2.5 of 10.0.
        let state = block.capture_state();
        assert_eq!(state.get("current"), Some(Value::Float(2.5)));

        let mut standby = component();
        standby.restore_state(&state).unwrap();
        assert_eq!(standby.capture_state(), state);
        feed(&io, 10.0, 3);
        standby.step(&io, Tick(3)).unwrap();
        assert_eq!(driven(&io), 5.0);

        // An uninitialized capture restores an uninitialized limiter:
        // `current` is absent while the tuned bound rides along.
        let state = component().capture_state();
        assert_eq!(state.get("current"), None);
        let mut fresh = component();
        fresh.restore_state(&state).unwrap();
        let fresh_io = TestIo::new(&[
            (
                IN,
                Direction::In,
                Sample::good(Value::Float(7.0), Tick::ZERO),
            ),
            (
                OUT,
                Direction::Out,
                Sample::good(Value::Float(0.0), Tick::ZERO),
            ),
        ]);
        fresh.step(&fresh_io, Tick(1)).unwrap();
        assert_eq!(driven(&fresh_io), 7.0);
    }

    #[test]
    fn incompatible_state_is_a_named_error() {
        let mut block = component();
        let mut state = StateMap::new();
        state.insert("current", Value::Float(f64::NAN));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::InvalidValue { ref field, .. }) if field == "current"
        ));
        let mut state = StateMap::new();
        state.insert("current", Value::Int(3));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::IncompatibleField { ref field, .. }) if field == "current"
        ));
        let mut state = StateMap::new();
        state.insert("other", Value::Float(1.0));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::UnknownField { ref field, .. }) if field == "other"
        ));
    }

    #[test]
    fn builds_from_parameter_map() {
        let parameters: Parameters = [("max_delta".to_string(), Value::Float(2.5))]
            .into_iter()
            .collect();
        let instance = ComponentInstance {
            id: ComponentId(11),
            kind: RateLimiter::KIND.to_string(),
            parameters,
            ports: BTreeMap::new(),
        };
        let block = RateLimiter::from_parameters("rl", IN, OUT, &instance.parameters).unwrap();
        assert_eq!(block.max_delta, 2.5);

        assert!(matches!(
            RateLimiter::from_parameters("rl", IN, OUT, &Parameters::new()).unwrap_err(),
            ParameterError::Missing { ref parameter, .. } if parameter == "max_delta"
        ));
        for bad in [
            Value::Float(0.0),
            Value::Float(-1.0),
            Value::Float(f64::INFINITY),
        ] {
            let parameters: Parameters = [("max_delta".to_string(), bad)].into_iter().collect();
            assert!(
                matches!(
                    RateLimiter::from_parameters("rl", IN, OUT, &parameters).unwrap_err(),
                    ParameterError::Invalid { ref parameter, .. } if parameter == "max_delta"
                ),
                "max_delta={bad:?}"
            );
        }
        // A losslessly representable Int is accepted.
        let parameters: Parameters = [("max_delta".to_string(), Value::Int(2))]
            .into_iter()
            .collect();
        let block = RateLimiter::from_parameters("rl", IN, OUT, &parameters).unwrap();
        assert_eq!(block.max_delta, 2.0);
    }

    #[test]
    fn describes_itself() {
        let descriptor = component().describe();
        assert_eq!(descriptor.name, "rl");
        assert_eq!(descriptor.kind, RateLimiter::KIND);
        assert_eq!(descriptor.label, "rl");
        assert_eq!(
            descriptor.ports,
            [
                PortDescriptor {
                    name: "in".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: Some(PortRole::ProcessValue),
                    point: None,
                },
                PortDescriptor {
                    name: "out".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Float,
                    role: Some(PortRole::Output),
                    point: None,
                },
            ]
        );
        // Drift guard: the descriptor's parameter names are exactly the
        // keys `from_parameters` reads.
        assert_eq!(
            descriptor.parameters,
            [ParameterDescriptor {
                name: "max_delta".to_string(),
                kind: ValueKind::Float,
                range: Some(describe::POSITIVE_F64),
            }]
        );
    }
}
