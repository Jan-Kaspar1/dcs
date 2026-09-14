//! Signal filter: a first-order per-tick smoothing of an analog signal.

use crate::describe;
use crate::params::{self, ParameterError, Parameters};
use dcs_core::{
    CommandError, ComponentDescriptor, PointId, PortRole, Quality, QualityReason, Sample,
    StateError, StateMap, Tick, Value, ValueKind,
};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// A first-order signal filter: smooths the analog `in` onto `out` by
/// the per-tick smoothing constant `alpha`.
///
/// Each scan a [`Quality::Good`] finite input advances the estimate by
/// the documented recurrence
///
/// ```text
/// out += alpha * (in - out)        0 < alpha <= 1
/// ```
///
/// — `alpha` near `1.0` tracks the input closely, `alpha` near `0.0`
/// smooths heavily, and `alpha` of exactly `1.0` passes the input
/// through. The first `Good` finite input is adopted outright:
/// initialization is not a filter step and the constant does not apply
/// to it.
///
/// **Quality rule:** a non-`Good` input freezes the filter — the
/// estimate does not advance and `out` holds the last estimate, stamped
/// with the input's quality so the freeze shows on the signal. A
/// non-finite input additionally merges `Bad(DeviceFault)`, matching
/// the crate's non-finite-input convention. Before the first `Good`
/// finite input there is no estimate to hold: `out` passes the input's
/// value through stamped with its quality, surfacing whatever arrives
/// rather than inventing a number.
///
/// Declared I/O: `in` (`In`, `Float`) and `out` (`Out`, `Float`).
///
/// Parameters: `alpha` — required finite `Float` (or losslessly
/// representable `Int`) in `(0, 1]`: the fraction of the remaining
/// input–estimate gap closed per scan.
#[derive(Debug)]
pub struct SignalFilter {
    name: String,
    input: PointId,
    output: PointId,
    alpha: f64,
    /// The running estimate `out` carries; `None` until the first
    /// `Good` finite input is adopted.
    estimate: Option<f64>,
}

impl SignalFilter {
    /// The component-kind string the model-driven registry maps onto
    /// this type's constructor.
    pub const KIND: &'static str = "signal-filter";

    /// Builds the component from explicit points and the smoothing
    /// constant.
    ///
    /// `alpha` must be finite and inside `(0, 1]`; violations are
    /// reported as a [`ParameterError`].
    pub fn new(
        name: impl Into<String>,
        input: PointId,
        output: PointId,
        alpha: f64,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        if !alpha.is_finite() {
            return Err(params::invalid(
                &name,
                "alpha",
                "must be finite".to_string(),
            ));
        }
        if !(0.0..=1.0).contains(&alpha) || alpha == 0.0 {
            return Err(params::invalid(
                &name,
                "alpha",
                "must be in (0, 1]".to_string(),
            ));
        }
        Ok(Self {
            name,
            input,
            output,
            alpha,
            estimate: None,
        })
    }

    /// Builds the component from a plant-model parameter map, reading
    /// the parameters listed on the type's docs.
    pub fn from_parameters(
        name: impl Into<String>,
        input: PointId,
        output: PointId,
        parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        let alpha = params::required_f64(&name, parameters, "alpha")?;
        Self::new(name, input, output, alpha)
    }
}

impl Component for SignalFilter {
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
        let input = io.read_typed::<f64>(self.input)?;
        let mut quality = input.quality;
        if !input.value.is_finite() {
            quality = quality.merge(Quality::Bad(QualityReason::DeviceFault));
        }
        let out = if input.quality.is_good() && input.value.is_finite() {
            let estimate = match self.estimate {
                Some(estimate) => {
                    let gap = input.value - estimate;
                    // A gap that overflowed the type cannot be
                    // approached by a fraction of itself: adopt the
                    // finite input outright.
                    if gap.is_finite() {
                        estimate + self.alpha * gap
                    } else {
                        input.value
                    }
                }
                None => input.value,
            };
            self.estimate = Some(estimate);
            estimate
        } else {
            // Frozen — the last estimate stands — or, before the first
            // `Good` input, the input surfaces with its own quality.
            self.estimate.unwrap_or(input.value)
        };
        io.write_sample(self.output, Sample::new(Value::Float(out), quality, tick))?;
        Ok(())
    }

    /// Describes the filter: `in` is the process value being smoothed,
    /// `out` the filtered estimate; the `alpha` parameter
    /// `from_parameters` reads.
    fn describe(&self) -> ComponentDescriptor {
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &[("in", PortRole::ProcessValue), ("out", PortRole::Output)],
            vec![describe::parameter(
                "alpha",
                ValueKind::Float,
                Some(describe::FRACTION_F64),
            )],
        )
    }

    /// Tunes `alpha` at the scan boundary; the new constant applies to
    /// the next recurrence step.
    fn apply_parameter(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        match parameter {
            "alpha" => {
                let tuned = params::tune_f64(&self.name, parameter, value)?;
                if !tuned.is_finite() || tuned <= 0.0 || tuned > 1.0 {
                    return Err(params::invalid_parameter(
                        &self.name,
                        parameter,
                        "must be finite and in (0, 1]",
                    ));
                }
                self.alpha = tuned;
            }
            _ => return Err(params::unknown_parameter(&self.name, parameter)),
        }
        Ok(())
    }

    /// Captures the estimate — absent before the first `Good` input —
    /// plus the tuned `alpha`, so a checkpointed filter continues the
    /// recurrence under the same constant.
    fn capture_state(&self) -> StateMap {
        let mut state = StateMap::new();
        if let Some(estimate) = self.estimate {
            state.insert("estimate", Value::Float(estimate));
        }
        state.insert("alpha", Value::Float(self.alpha));
        state
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        state.ensure_known_fields(&self.name, &["estimate", "alpha"])?;
        let estimate = match state.optional_f64(&self.name, "estimate")? {
            Some(estimate) if estimate.is_finite() => Some(estimate),
            Some(estimate) => {
                return Err(StateError::InvalidValue {
                    element: self.name.clone(),
                    field: "estimate".to_string(),
                    value: Value::Float(estimate),
                });
            }
            None => None,
        };
        let alpha = state.require_f64(&self.name, "alpha")?;
        if !alpha.is_finite() || alpha <= 0.0 || alpha > 1.0 {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "alpha".to_string(),
                value: Value::Float(alpha),
            });
        }
        self.estimate = estimate;
        self.alpha = alpha;
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

    const IN: PointId = PointId(150);
    const OUT: PointId = PointId(151);

    /// A filter closing half the remaining gap per scan.
    fn component() -> SignalFilter {
        SignalFilter::new("filt", IN, OUT, 0.5).unwrap()
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
    fn first_good_input_initializes_the_estimate() {
        let mut block = component();
        let io = io(Sample::good(Value::Float(8.0), Tick::ZERO));
        block.step(&io, Tick(1)).unwrap();
        let out = io.written(OUT).unwrap();
        assert_eq!(out.value, Value::Float(8.0));
        assert_eq!(out.quality, Quality::Good);
        assert_eq!(out.tick, Tick(1));
    }

    #[test]
    fn follows_the_recurrence_over_scripted_inputs() {
        let mut block = component();
        let io = io(Sample::good(Value::Float(0.0), Tick::ZERO));
        block.step(&io, Tick(1)).unwrap();

        // out += 0.5 * (in - out): a step to 8 halves the gap per scan.
        for (tick, input, expected) in [
            (2, 8.0, 4.0),
            (3, 8.0, 6.0),
            (4, 8.0, 7.0),
            (5, 8.0, 7.5),
            (6, 0.0, 3.75),
            (7, 0.0, 1.875),
        ] {
            feed(&io, input, tick);
            block.step(&io, Tick(tick)).unwrap();
            assert_eq!(driven(&io), expected, "tick={tick}");
        }
    }

    #[test]
    fn alpha_of_one_passes_the_input_through() {
        let mut block = SignalFilter::new("filt", IN, OUT, 1.0).unwrap();
        let io = io(Sample::good(Value::Float(3.0), Tick::ZERO));
        block.step(&io, Tick(1)).unwrap();
        feed(&io, 9.0, 2);
        block.step(&io, Tick(2)).unwrap();
        assert_eq!(driven(&io), 9.0);
    }

    #[test]
    fn non_good_input_freezes_the_estimate_and_stamps_its_quality() {
        let mut block = component();
        let io = io(Sample::good(Value::Float(4.0), Tick::ZERO));
        block.step(&io, Tick(1)).unwrap();
        feed(&io, 8.0, 2);
        block.step(&io, Tick(2)).unwrap();
        assert_eq!(driven(&io), 6.0);

        // A Bad input holds the estimate; the output reports the
        // input's quality.
        io.feed(
            IN,
            Sample::new(
                Value::Float(20.0),
                Quality::Bad(QualityReason::CommunicationFault),
                Tick(3),
            ),
        );
        block.step(&io, Tick(3)).unwrap();
        let out = io.written(OUT).unwrap();
        assert_eq!(out.value, Value::Float(6.0));
        assert_eq!(out.quality, Quality::Bad(QualityReason::CommunicationFault));

        // Uncertain freezes identically.
        io.feed(
            IN,
            Sample::new(
                Value::Float(20.0),
                Quality::Uncertain(QualityReason::Stale),
                Tick(4),
            ),
        );
        block.step(&io, Tick(4)).unwrap();
        let out = io.written(OUT).unwrap();
        assert_eq!(out.value, Value::Float(6.0));
        assert_eq!(out.quality, Quality::Uncertain(QualityReason::Stale));

        // Recovery resumes the recurrence from the held estimate.
        feed(&io, 20.0, 5);
        block.step(&io, Tick(5)).unwrap();
        assert_eq!(driven(&io), 13.0);
    }

    #[test]
    fn non_finite_input_freezes_and_marks_bad() {
        let mut block = component();
        let io = io(Sample::good(Value::Float(4.0), Tick::ZERO));
        block.step(&io, Tick(1)).unwrap();

        feed(&io, f64::NAN, 2);
        block.step(&io, Tick(2)).unwrap();
        let out = io.written(OUT).unwrap();
        assert_eq!(out.value, Value::Float(4.0));
        assert_eq!(out.quality, Quality::Bad(QualityReason::DeviceFault));
    }

    #[test]
    fn before_the_first_good_input_the_input_passes_through() {
        let mut block = component();
        // A Bad first input: nothing to freeze to, so the value
        // surfaces stamped with its quality — and seeds no estimate.
        let io = io(Sample::new(
            Value::Float(7.0),
            Quality::Bad(QualityReason::DeviceFault),
            Tick::ZERO,
        ));
        block.step(&io, Tick(1)).unwrap();
        let out = io.written(OUT).unwrap();
        assert_eq!(out.value, Value::Float(7.0));
        assert_eq!(out.quality, Quality::Bad(QualityReason::DeviceFault));

        feed(&io, 10.0, 2);
        block.step(&io, Tick(2)).unwrap();
        assert_eq!(driven(&io), 10.0);
    }

    #[test]
    fn restored_filter_continues_the_recurrence() {
        let mut block = component();
        let io = io(Sample::good(Value::Float(0.0), Tick::ZERO));
        block.step(&io, Tick(1)).unwrap();
        feed(&io, 8.0, 2);
        block.step(&io, Tick(2)).unwrap();

        // Checkpoint mid-approach: estimate 4.0 of 8.0.
        let state = block.capture_state();
        assert_eq!(state.get("estimate"), Some(Value::Float(4.0)));

        let mut standby = component();
        standby.restore_state(&state).unwrap();
        assert_eq!(standby.capture_state(), state);
        feed(&io, 8.0, 3);
        standby.step(&io, Tick(3)).unwrap();
        assert_eq!(driven(&io), 6.0);

        // An uninitialized capture restores an uninitialized filter:
        // `estimate` is absent while the tuned constant rides along.
        let state = component().capture_state();
        assert_eq!(state.get("estimate"), None);
        assert_eq!(state.get("alpha"), Some(Value::Float(0.5)));
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
        state.insert("estimate", Value::Float(f64::INFINITY));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::InvalidValue { ref field, .. }) if field == "estimate"
        ));
        let mut state = StateMap::new();
        state.insert("alpha", Value::Float(1.5));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::InvalidValue { ref field, .. }) if field == "alpha"
        ));
        let mut state = StateMap::new();
        state.insert("estimate", Value::Int(3));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::IncompatibleField { ref field, .. }) if field == "estimate"
        ));
        assert!(matches!(
            block.restore_state(&StateMap::new()),
            Err(StateError::MissingField { ref field, .. }) if field == "alpha"
        ));
        let mut state = StateMap::new();
        state.insert("alpha", Value::Float(0.5));
        state.insert("other", Value::Float(1.0));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::UnknownField { ref field, .. }) if field == "other"
        ));
    }

    #[test]
    fn apply_parameter_retunes_the_constant() {
        let mut block = component();
        block.apply_parameter("alpha", Value::Float(0.25)).unwrap();
        assert_eq!(block.alpha, 0.25);
        assert_eq!(block.capture_state().get("alpha"), Some(Value::Float(0.25)));

        assert_eq!(
            block
                .apply_parameter("other", Value::Float(1.0))
                .unwrap_err(),
            CommandError::UnknownParameter {
                component: "filt".to_string(),
                parameter: "other".to_string(),
            }
        );
        assert!(matches!(
            block.apply_parameter("alpha", Value::Bool(true)).unwrap_err(),
            CommandError::ParameterTypeMismatch { ref parameter, .. } if parameter == "alpha"
        ));
        for bad in [0.0, -0.5, 1.5, f64::INFINITY] {
            assert!(
                matches!(
                    block.apply_parameter("alpha", Value::Float(bad)).unwrap_err(),
                    CommandError::InvalidParameter { ref parameter, .. } if parameter == "alpha"
                ),
                "alpha={bad}"
            );
        }
        assert_eq!(block.alpha, 0.25);
    }

    #[test]
    fn builds_from_parameter_map() {
        let parameters: Parameters = [("alpha".to_string(), Value::Float(0.5))]
            .into_iter()
            .collect();
        let instance = ComponentInstance {
            id: ComponentId(13),
            kind: SignalFilter::KIND.to_string(),
            parameters,
            ports: BTreeMap::new(),
        };
        let block = SignalFilter::from_parameters("filt", IN, OUT, &instance.parameters).unwrap();
        assert_eq!(block.alpha, 0.5);

        assert!(matches!(
            SignalFilter::from_parameters("filt", IN, OUT, &Parameters::new()).unwrap_err(),
            ParameterError::Missing { ref parameter, .. } if parameter == "alpha"
        ));
        for bad in [
            Value::Float(0.0),
            Value::Float(-0.5),
            Value::Float(1.5),
            Value::Float(f64::NAN),
            Value::Bool(true),
        ] {
            let parameters: Parameters = [("alpha".to_string(), bad)].into_iter().collect();
            assert!(
                matches!(
                    SignalFilter::from_parameters("filt", IN, OUT, &parameters).unwrap_err(),
                    ParameterError::Invalid { ref parameter, .. } if parameter == "alpha"
                ),
                "alpha={bad:?}"
            );
        }
        // A losslessly representable Int is accepted.
        let parameters: Parameters = [("alpha".to_string(), Value::Int(1))].into_iter().collect();
        let block = SignalFilter::from_parameters("filt", IN, OUT, &parameters).unwrap();
        assert_eq!(block.alpha, 1.0);
    }

    #[test]
    fn describes_itself() {
        let descriptor = component().describe();
        assert_eq!(descriptor.name, "filt");
        assert_eq!(descriptor.kind, SignalFilter::KIND);
        assert_eq!(descriptor.label, "filt");
        assert_eq!(
            descriptor.ports,
            [
                PortDescriptor {
                    name: "in".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: Some(PortRole::ProcessValue),
                },
                PortDescriptor {
                    name: "out".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Float,
                    role: Some(PortRole::Output),
                },
            ]
        );
        // Drift guard: the descriptor's parameter names are exactly the
        // keys `from_parameters` reads.
        assert_eq!(
            descriptor.parameters,
            [ParameterDescriptor {
                name: "alpha".to_string(),
                kind: ValueKind::Float,
                range: Some(describe::FRACTION_F64),
            }]
        );
    }
}
