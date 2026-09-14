//! Median voter: 2oo3 voting over three redundant analog inputs with a
//! spread discrepancy diagnostic.

use crate::describe;
use crate::params::{self, ParameterError, Parameters};
use dcs_core::{
    CommandError, ComponentDescriptor, PointId, PortRole, Quality, QualityReason, Sample,
    StateError, StateMap, Tick, Value, ValueKind,
};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// A median voter: reads the redundant measurements `in_1`, `in_2`,
/// `in_3`, publishes the voted value on `out`, and asserts
/// `discrepancy` while the participating inputs disagree by more than
/// `tolerance`.
///
/// Each scan the inputs that read [`Quality::Good`] with a finite value
/// participate in the vote; with all three participating, `out` is the
/// median — the middle value, so a single deviating sensor cannot move
/// it.
///
/// **Degradation rule:** a non-`Good` or non-finite input drops out of
/// the vote. Two remaining participants vote their midpoint — the
/// finite mean, computed without overflow. One participant votes its
/// own value. With no participants there is nothing to vote: `out`
/// holds the last voted value, and before the first vote it reports
/// `NaN`, surfacing the loss of every input rather than inventing a
/// number. Whatever the vote, `out` and `discrepancy` are stamped with
/// the worst of all three inputs' qualities — a dropped input still
/// marks the outputs it failed to join — merged with
/// `Bad(DeviceFault)` when any input is non-finite.
///
/// `discrepancy` asserts when the participating inputs' spread — the
/// largest minus the smallest voted value — exceeds `tolerance`; a
/// spread equal to the tolerance does not assert. Fewer than two
/// participants cannot disagree, so the flag deasserts: the merged
/// quality carries the degradation report instead.
///
/// Declared I/O: `in_1`, `in_2`, `in_3` (`In`, `Float`), `out` (`Out`,
/// `Float`), `discrepancy` (`Out`, `Bool`).
///
/// Parameters: `tolerance` — required finite `Float` (or losslessly
/// representable `Int`), non-negative: the spread the discrepancy flag
/// tolerates.
#[derive(Debug)]
pub struct MedianVoter {
    name: String,
    inputs: [PointId; 3],
    output: PointId,
    discrepancy: PointId,
    tolerance: f64,
    /// The last voted value `out` carries while no input participates;
    /// `None` before the first vote.
    held: Option<f64>,
}

impl MedianVoter {
    /// The component-kind string the model-driven registry maps onto
    /// this type's constructor.
    pub const KIND: &'static str = "median-voter";

    /// Builds the component from explicit points and the tolerated
    /// input spread.
    ///
    /// `tolerance` must be finite and non-negative; violations are
    /// reported as a [`ParameterError`].
    pub fn new(
        name: impl Into<String>,
        in_1: PointId,
        in_2: PointId,
        in_3: PointId,
        output: PointId,
        discrepancy: PointId,
        tolerance: f64,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        if !tolerance.is_finite() {
            return Err(params::invalid(
                &name,
                "tolerance",
                "must be finite".to_string(),
            ));
        }
        if tolerance < 0.0 {
            return Err(params::invalid(
                &name,
                "tolerance",
                "must be non-negative".to_string(),
            ));
        }
        Ok(Self {
            name,
            inputs: [in_1, in_2, in_3],
            output,
            discrepancy,
            tolerance,
            held: None,
        })
    }

    /// Builds the component from a plant-model parameter map, reading
    /// the parameters listed on the type's docs.
    pub fn from_parameters(
        name: impl Into<String>,
        in_1: PointId,
        in_2: PointId,
        in_3: PointId,
        output: PointId,
        discrepancy: PointId,
        parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        let tolerance = params::required_f64(&name, parameters, "tolerance")?;
        Self::new(name, in_1, in_2, in_3, output, discrepancy, tolerance)
    }
}

impl Component for MedianVoter {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        self.inputs
            .iter()
            .enumerate()
            .map(|(index, point)| IoRequirement::input::<f64>(format!("in_{}", index + 1), *point))
            .chain([
                IoRequirement::output::<f64>("out", self.output),
                IoRequirement::output::<bool>("discrepancy", self.discrepancy),
            ])
            .collect()
    }

    fn step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError> {
        let samples = [
            io.read_typed::<f64>(self.inputs[0])?,
            io.read_typed::<f64>(self.inputs[1])?,
            io.read_typed::<f64>(self.inputs[2])?,
        ];
        let mut quality = samples
            .iter()
            .fold(Quality::Good, |merged, sample| merged.merge(sample.quality));
        if samples.iter().any(|sample| !sample.value.is_finite()) {
            quality = quality.merge(Quality::Bad(QualityReason::DeviceFault));
        }

        // The vote: `Good` finite inputs sorted into order.
        let mut voting: Vec<f64> = samples
            .iter()
            .filter(|sample| sample.quality.is_good() && sample.value.is_finite())
            .map(|sample| sample.value)
            .collect();
        voting.sort_by(f64::total_cmp);
        let voted = match voting.as_slice() {
            [_, b, _] => *b,
            [a, b] => a.midpoint(*b),
            [a] => *a,
            _ => self.held.unwrap_or(f64::NAN),
        };
        if !voting.is_empty() {
            self.held = Some(voted);
        }
        let discrepancy =
            voting.len() >= 2 && (voting[voting.len() - 1] - voting[0]) > self.tolerance;

        io.write_sample(self.output, Sample::new(Value::Float(voted), quality, tick))?;
        io.write_sample(
            self.discrepancy,
            Sample::new(Value::Bool(discrepancy), quality, tick),
        )?;
        Ok(())
    }

    /// Describes the voter: `in_1`/`in_2`/`in_3` are the redundant
    /// process values, `out` the voted value, `discrepancy` the
    /// spread-exceeded diagnostic; the `tolerance` parameter
    /// `from_parameters` reads.
    fn describe(&self) -> ComponentDescriptor {
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &[
                ("in_1", PortRole::ProcessValue),
                ("in_2", PortRole::ProcessValue),
                ("in_3", PortRole::ProcessValue),
                ("out", PortRole::Output),
                ("discrepancy", PortRole::Status),
            ],
            vec![describe::parameter(
                "tolerance",
                ValueKind::Float,
                Some(describe::NONNEGATIVE_F64),
            )],
        )
    }

    /// Tunes `tolerance` at the scan boundary; the new bound applies to
    /// the next scan's spread check.
    fn apply_parameter(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        match parameter {
            "tolerance" => {
                let tuned = params::tune_f64(&self.name, parameter, value)?;
                if !tuned.is_finite() || tuned < 0.0 {
                    return Err(params::invalid_parameter(
                        &self.name,
                        parameter,
                        "must be finite and non-negative",
                    ));
                }
                self.tolerance = tuned;
            }
            _ => return Err(params::unknown_parameter(&self.name, parameter)),
        }
        Ok(())
    }

    /// Captures the held vote — absent before the first participating
    /// scan — plus the tuned `tolerance`, so a checkpointed voter holds
    /// the same value through a full input loss under the same bound.
    fn capture_state(&self) -> StateMap {
        let mut state = StateMap::new();
        if let Some(held) = self.held {
            state.insert("held", Value::Float(held));
        }
        state.insert("tolerance", Value::Float(self.tolerance));
        state
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        state.ensure_known_fields(&self.name, &["held", "tolerance"])?;
        let held = match state.optional_f64(&self.name, "held")? {
            Some(held) if held.is_finite() => Some(held),
            Some(held) => {
                return Err(StateError::InvalidValue {
                    element: self.name.clone(),
                    field: "held".to_string(),
                    value: Value::Float(held),
                });
            }
            None => None,
        };
        let tolerance = state.require_f64(&self.name, "tolerance")?;
        if !tolerance.is_finite() || tolerance < 0.0 {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "tolerance".to_string(),
                value: Value::Float(tolerance),
            });
        }
        self.held = held;
        self.tolerance = tolerance;
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

    const IN_1: PointId = PointId(170);
    const IN_2: PointId = PointId(171);
    const IN_3: PointId = PointId(172);
    const OUT: PointId = PointId(173);
    const DISCREPANCY: PointId = PointId(174);

    /// A voter tolerating an input spread of 2.0.
    fn component() -> MedianVoter {
        MedianVoter::new("vot", IN_1, IN_2, IN_3, OUT, DISCREPANCY, 2.0).unwrap()
    }

    fn io() -> TestIo {
        TestIo::new(&[
            (
                IN_1,
                Direction::In,
                Sample::good(Value::Float(0.0), Tick::ZERO),
            ),
            (
                IN_2,
                Direction::In,
                Sample::good(Value::Float(0.0), Tick::ZERO),
            ),
            (
                IN_3,
                Direction::In,
                Sample::good(Value::Float(0.0), Tick::ZERO),
            ),
            (
                OUT,
                Direction::Out,
                Sample::good(Value::Float(0.0), Tick::ZERO),
            ),
            (
                DISCREPANCY,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
        ])
    }

    /// Feeds the three inputs and steps once.
    fn step(block: &mut MedianVoter, io: &TestIo, inputs: [f64; 3], tick: u64) {
        for (point, value) in [(IN_1, inputs[0]), (IN_2, inputs[1]), (IN_3, inputs[2])] {
            io.feed(point, Sample::good(Value::Float(value), Tick(tick)));
        }
        block.step(io, Tick(tick)).unwrap();
    }

    fn voted(io: &TestIo) -> Sample {
        io.written(OUT).unwrap()
    }

    fn discrepancy(io: &TestIo) -> bool {
        io.written(DISCREPANCY).unwrap().value == Value::Bool(true)
    }

    #[test]
    fn the_median_is_the_middle_value_in_any_order() {
        // Every permutation of (4, 9, 20) votes 9.
        for inputs in [
            [4.0, 9.0, 20.0],
            [4.0, 20.0, 9.0],
            [9.0, 4.0, 20.0],
            [9.0, 20.0, 4.0],
            [20.0, 4.0, 9.0],
            [20.0, 9.0, 4.0],
        ] {
            let mut block = component();
            let io = io();
            step(&mut block, &io, inputs, 1);
            assert_eq!(voted(&io).value, Value::Float(9.0), "{inputs:?}");
            assert_eq!(voted(&io).quality, Quality::Good);
            assert_eq!(voted(&io).tick, Tick(1));
            // The spread of 16 exceeds the 2.0 tolerance on every order.
            assert!(discrepancy(&io));
        }
    }

    #[test]
    fn tied_inputs_vote_the_tied_value() {
        let mut block = component();
        let io = io();
        // Two agreeing inputs dominate a third wherever it sits.
        for (inputs, expected) in [
            ([5.0, 5.0, 9.0], 5.0),
            ([9.0, 5.0, 5.0], 5.0),
            ([5.0, 9.0, 5.0], 5.0),
            ([5.0, 5.0, 5.0], 5.0),
        ] {
            step(&mut block, &io, inputs, 1);
            assert_eq!(voted(&io).value, Value::Float(expected), "{inputs:?}");
        }
    }

    #[test]
    fn discrepancy_asserts_past_the_tolerance() {
        let mut block = component();
        let io = io();
        // Spread inside the tolerance: no flag.
        step(&mut block, &io, [10.0, 11.0, 11.5], 1);
        assert_eq!(voted(&io).value, Value::Float(11.0));
        assert!(!discrepancy(&io));

        // Spread at the tolerance boundary: still no flag.
        step(&mut block, &io, [10.0, 11.0, 12.0], 2);
        assert!(!discrepancy(&io));

        // Spread past the tolerance: asserted, median unaffected.
        step(&mut block, &io, [10.0, 11.0, 12.5], 3);
        assert_eq!(voted(&io).value, Value::Float(11.0));
        assert!(discrepancy(&io));
    }

    #[test]
    fn a_dropped_input_votes_the_midpoint_of_the_remaining_two() {
        let mut block = component();
        let io = io();
        step(&mut block, &io, [10.0, 20.0, 12.0], 1);

        // in_2 goes Bad: the vote is the midpoint of in_1 and in_3,
        // stamped with the merged worst-of quality.
        io.feed(
            IN_2,
            Sample::new(
                Value::Float(99.0),
                Quality::Bad(QualityReason::CommunicationFault),
                Tick(2),
            ),
        );
        io.feed(IN_1, Sample::good(Value::Float(10.5), Tick(2)));
        io.feed(IN_3, Sample::good(Value::Float(11.5), Tick(2)));
        block.step(&io, Tick(2)).unwrap();
        let out = voted(&io);
        assert_eq!(out.value, Value::Float(11.0));
        assert_eq!(out.quality, Quality::Bad(QualityReason::CommunicationFault));
        // The dropped channel's value does not enter the spread: the
        // remaining pair agrees within tolerance.
        assert!(!discrepancy(&io));
        assert_eq!(
            io.written(DISCREPANCY).unwrap().quality,
            Quality::Bad(QualityReason::CommunicationFault)
        );
    }

    #[test]
    fn a_single_participant_votes_itself() {
        let mut block = component();
        let io = io();
        step(&mut block, &io, [10.0, 11.0, 12.0], 1);

        for point in [IN_1, IN_2] {
            io.feed(
                point,
                Sample::new(
                    Value::Float(0.0),
                    Quality::Uncertain(QualityReason::Stale),
                    Tick(2),
                ),
            );
        }
        io.feed(IN_3, Sample::good(Value::Float(12.5), Tick(2)));
        block.step(&io, Tick(2)).unwrap();
        let out = voted(&io);
        assert_eq!(out.value, Value::Float(12.5));
        assert_eq!(out.quality, Quality::Uncertain(QualityReason::Stale));
        assert!(!discrepancy(&io));
    }

    #[test]
    fn no_participants_holds_the_last_vote_then_reports_nan() {
        let mut block = component();
        let io = io();
        // All three Bad on the first scan: nothing voted, `out` is NaN
        // stamped Bad rather than an invented number.
        for point in [IN_1, IN_2, IN_3] {
            io.feed(
                point,
                Sample::new(
                    Value::Float(7.0),
                    Quality::Bad(QualityReason::DeviceFault),
                    Tick(1),
                ),
            );
        }
        block.step(&io, Tick(1)).unwrap();
        let out = voted(&io);
        assert!(matches!(out.value, Value::Float(value) if value.is_nan()));
        assert_eq!(out.quality, Quality::Bad(QualityReason::DeviceFault));
        assert!(!discrepancy(&io));

        // A real vote, then a full loss: the last vote holds.
        step(&mut block, &io, [10.0, 11.0, 12.0], 2);
        assert_eq!(voted(&io).value, Value::Float(11.0));
        for point in [IN_1, IN_2, IN_3] {
            io.feed(
                point,
                Sample::new(
                    Value::Float(50.0),
                    Quality::Bad(QualityReason::DeviceFault),
                    Tick(3),
                ),
            );
        }
        block.step(&io, Tick(3)).unwrap();
        assert_eq!(voted(&io).value, Value::Float(11.0));
        assert_eq!(voted(&io).quality, Quality::Bad(QualityReason::DeviceFault));
    }

    #[test]
    fn a_non_finite_good_input_drops_out_and_marks_bad() {
        let mut block = component();
        let io = io();
        io.feed(IN_2, Sample::good(Value::Float(f64::NAN), Tick(1)));
        io.feed(IN_1, Sample::good(Value::Float(10.0), Tick(1)));
        io.feed(IN_3, Sample::good(Value::Float(14.0), Tick(1)));
        block.step(&io, Tick(1)).unwrap();
        let out = voted(&io);
        assert_eq!(out.value, Value::Float(12.0));
        assert_eq!(out.quality, Quality::Bad(QualityReason::DeviceFault));
        // The remaining pair's spread exceeds the tolerance.
        assert!(discrepancy(&io));
    }

    #[test]
    fn restored_voter_holds_the_same_vote() {
        let mut block = component();
        let io = io();
        step(&mut block, &io, [10.0, 11.0, 12.0], 1);

        let state = block.capture_state();
        assert_eq!(state.get("held"), Some(Value::Float(11.0)));
        assert_eq!(state.get("tolerance"), Some(Value::Float(2.0)));

        let mut standby = component();
        standby.restore_state(&state).unwrap();
        assert_eq!(standby.capture_state(), state);

        // A full input loss on the standby holds the same vote.
        for point in [IN_1, IN_2, IN_3] {
            io.feed(
                point,
                Sample::new(
                    Value::Float(0.0),
                    Quality::Bad(QualityReason::DeviceFault),
                    Tick(2),
                ),
            );
        }
        standby.step(&io, Tick(2)).unwrap();
        assert_eq!(voted(&io).value, Value::Float(11.0));

        // An uninitialized capture carries no held value: a restored
        // voter is back at the never-voted initial state.
        let state = component().capture_state();
        assert_eq!(state.get("held"), None);
        let mut fresh = component();
        fresh.restore_state(&state).unwrap();
        assert_eq!(fresh.capture_state(), state);
    }

    #[test]
    fn incompatible_state_is_a_named_error() {
        let mut block = component();
        let mut state = StateMap::new();
        state.insert("held", Value::Float(f64::NAN));
        state.insert("tolerance", Value::Float(2.0));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::InvalidValue { ref field, .. }) if field == "held"
        ));
        let mut state = StateMap::new();
        state.insert("tolerance", Value::Float(-1.0));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::InvalidValue { ref field, .. }) if field == "tolerance"
        ));
        let mut state = StateMap::new();
        state.insert("held", Value::Int(3));
        state.insert("tolerance", Value::Float(2.0));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::IncompatibleField { ref field, .. }) if field == "held"
        ));
        assert!(matches!(
            block.restore_state(&StateMap::new()),
            Err(StateError::MissingField { ref field, .. }) if field == "tolerance"
        ));
        let mut state = StateMap::new();
        state.insert("tolerance", Value::Float(2.0));
        state.insert("other", Value::Float(1.0));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::UnknownField { ref field, .. }) if field == "other"
        ));
    }

    #[test]
    fn apply_parameter_retunes_the_tolerance() {
        let mut block = component();
        let io = io();
        block
            .apply_parameter("tolerance", Value::Float(20.0))
            .unwrap();
        step(&mut block, &io, [10.0, 11.0, 25.0], 1);
        assert!(!discrepancy(&io));
        assert_eq!(
            block.capture_state().get("tolerance"),
            Some(Value::Float(20.0))
        );

        assert_eq!(
            block
                .apply_parameter("other", Value::Float(1.0))
                .unwrap_err(),
            CommandError::UnknownParameter {
                component: "vot".to_string(),
                parameter: "other".to_string(),
            }
        );
        assert!(matches!(
            block.apply_parameter("tolerance", Value::Bool(true)).unwrap_err(),
            CommandError::ParameterTypeMismatch { ref parameter, .. } if parameter == "tolerance"
        ));
        for bad in [-0.5, f64::NAN, f64::INFINITY] {
            assert!(
                matches!(
                    block.apply_parameter("tolerance", Value::Float(bad)).unwrap_err(),
                    CommandError::InvalidParameter { ref parameter, .. } if parameter == "tolerance"
                ),
                "tolerance={bad}"
            );
        }
        assert_eq!(block.tolerance, 20.0);
    }

    #[test]
    fn builds_from_parameter_map() {
        let parameters: Parameters = [("tolerance".to_string(), Value::Float(2.0))]
            .into_iter()
            .collect();
        let instance = ComponentInstance {
            id: ComponentId(17),
            kind: MedianVoter::KIND.to_string(),
            parameters,
            ports: BTreeMap::new(),
        };
        let block = MedianVoter::from_parameters(
            "vot",
            IN_1,
            IN_2,
            IN_3,
            OUT,
            DISCREPANCY,
            &instance.parameters,
        )
        .unwrap();
        assert_eq!(block.tolerance, 2.0);

        assert!(matches!(
            MedianVoter::from_parameters(
                "vot", IN_1, IN_2, IN_3, OUT, DISCREPANCY, &Parameters::new()
            )
            .unwrap_err(),
            ParameterError::Missing { ref parameter, .. } if parameter == "tolerance"
        ));
        for bad in [
            Value::Float(-1.0),
            Value::Float(f64::NAN),
            Value::Bool(true),
        ] {
            let parameters: Parameters = [("tolerance".to_string(), bad)].into_iter().collect();
            assert!(
                matches!(
                    MedianVoter::from_parameters(
                        "vot", IN_1, IN_2, IN_3, OUT, DISCREPANCY, &parameters
                    )
                    .unwrap_err(),
                    ParameterError::Invalid { ref parameter, .. } if parameter == "tolerance"
                ),
                "tolerance={bad:?}"
            );
        }
        // A losslessly representable Int is accepted.
        let parameters: Parameters = [("tolerance".to_string(), Value::Int(2))]
            .into_iter()
            .collect();
        let block =
            MedianVoter::from_parameters("vot", IN_1, IN_2, IN_3, OUT, DISCREPANCY, &parameters)
                .unwrap();
        assert_eq!(block.tolerance, 2.0);
    }

    #[test]
    fn describes_itself() {
        let descriptor = component().describe();
        assert_eq!(descriptor.name, "vot");
        assert_eq!(descriptor.kind, MedianVoter::KIND);
        assert_eq!(descriptor.label, "vot");
        assert_eq!(
            descriptor.ports,
            [
                PortDescriptor {
                    name: "in_1".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: Some(PortRole::ProcessValue),
                },
                PortDescriptor {
                    name: "in_2".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: Some(PortRole::ProcessValue),
                },
                PortDescriptor {
                    name: "in_3".to_string(),
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
                PortDescriptor {
                    name: "discrepancy".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Status),
                },
            ]
        );
        // Drift guard: the descriptor's parameter names are exactly the
        // keys `from_parameters` reads.
        assert_eq!(
            descriptor.parameters,
            [ParameterDescriptor {
                name: "tolerance".to_string(),
                kind: ValueKind::Float,
                range: Some(describe::NONNEGATIVE_F64),
            }]
        );
    }
}
