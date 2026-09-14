//! Valve: an analog actuator that echoes a command to the field and
//! flags a discrepancy when the position feedback stops tracking it.

use crate::describe;
use crate::params::{self, ParameterError, Parameters};
use dcs_core::{
    CommandError, ComponentDescriptor, PointId, PortRole, Quality, QualityReason, Sample,
    StateError, StateMap, Tick, Value, ValueKind,
};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// A valve: drives the analog `cmd` request onto the field `out` point and
/// verifies the `fb` position feedback follows it.
///
/// Each step copies `cmd` onto `out` — the actuator's field command —
/// then compares `fb` against the commanded value. The feedback *agrees*
/// only when both inputs carry [`Quality::Good`] and
/// `|fb - cmd| <= tolerance`; every other state — values out of
/// tolerance, a `NaN` on either side, or a non-`Good` input that cannot
/// prove agreement — counts as deviating, the fail-safe reading.
/// `discrepancy` asserts once the count of consecutive deviating scans
/// reaches `discrepancy_ticks` (`0` or `1` flags the first deviating
/// scan) and clears on the first scan the feedback agrees again; the
/// comparison is stateless apart from that count, so a recovering
/// feedback needs no reset.
///
/// `out` carries `cmd`'s quality — a degraded command produces a degraded
/// field write. `discrepancy` carries the worst of the `cmd` and `fb`
/// qualities, so a `Bad` feedback both counts as deviating and marks the
/// diagnostic itself untrusted. A `NaN` on either input additionally
/// merges `Bad(DeviceFault)` into the output it feeds — `out` for `cmd`,
/// `discrepancy` for `fb` — matching the crate's non-finite-input
/// convention.
///
/// Declared I/O: `cmd` (`In`, `Float`), `out` (`Out`, `Float`), `fb`
/// (`In`, `Float`), `discrepancy` (`Out`, `Bool`).
///
/// Parameters: `tolerance` — required finite `Float` or losslessly
/// representable `Int`, non-negative, the largest `|fb - cmd|` that still
/// counts as agreement; `discrepancy_ticks` — optional non-negative `Int`
/// (or integral `Float`), default `0`, the consecutive deviating scans
/// the flag waits for.
#[derive(Debug)]
pub struct Valve {
    name: String,
    command: PointId,
    output: PointId,
    feedback: PointId,
    discrepancy: PointId,
    tolerance: f64,
    discrepancy_ticks: u64,
    /// Consecutive deviating scans observed so far.
    deviating: u64,
}

impl Valve {
    /// The component-kind string the model-driven registry maps onto this
    /// type's constructor.
    pub const KIND: &'static str = "valve";

    /// Builds the component from explicit points and a feedback
    /// tolerance, or reports a non-finite or negative tolerance as a
    /// [`ParameterError`].
    pub fn new(
        name: impl Into<String>,
        command: PointId,
        output: PointId,
        feedback: PointId,
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
            command,
            output,
            feedback,
            discrepancy,
            tolerance,
            discrepancy_ticks: 0,
            deviating: 0,
        })
    }

    /// Sets how many consecutive deviating scans `discrepancy` waits for
    /// before asserting; `0` or `1` flags the first deviating scan.
    pub fn with_discrepancy_ticks(mut self, discrepancy_ticks: u64) -> Self {
        self.discrepancy_ticks = discrepancy_ticks;
        self
    }

    /// Builds the component from a plant-model parameter map, reading the
    /// parameters listed on the type's docs.
    pub fn from_parameters(
        name: impl Into<String>,
        command: PointId,
        output: PointId,
        feedback: PointId,
        discrepancy: PointId,
        parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        let tolerance = params::required_f64(&name, parameters, "tolerance")?;
        let discrepancy_ticks =
            params::optional_u64(&name, parameters, "discrepancy_ticks")?.unwrap_or(0);
        Ok(
            Self::new(name, command, output, feedback, discrepancy, tolerance)?
                .with_discrepancy_ticks(discrepancy_ticks),
        )
    }
}

impl Component for Valve {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        vec![
            IoRequirement::input::<f64>("cmd", self.command),
            IoRequirement::output::<f64>("out", self.output),
            IoRequirement::input::<f64>("fb", self.feedback),
            IoRequirement::output::<bool>("discrepancy", self.discrepancy),
        ]
    }

    fn step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError> {
        let command = io.read_typed::<f64>(self.command)?;
        let feedback = io.read_typed::<f64>(self.feedback)?;

        let command_quality = if command.value.is_nan() {
            command
                .quality
                .merge(Quality::Bad(QualityReason::DeviceFault))
        } else {
            command.quality
        };
        io.write_sample(
            self.output,
            Sample::new(Value::Float(command.value), command_quality, tick),
        )?;

        // Agreement must be proven: both inputs Good and the feedback
        // within tolerance of the command. A NaN fails the comparison and
        // a non-Good input cannot vouch for agreement, so both count as
        // deviating — the fail-safe reading.
        let agrees = command.quality.is_good()
            && feedback.quality.is_good()
            && (feedback.value - command.value).abs() <= self.tolerance;
        if agrees {
            self.deviating = 0;
        } else {
            self.deviating = self.deviating.saturating_add(1);
        }
        let discrepancy = self.deviating >= self.discrepancy_ticks.max(1);
        let mut flag_quality = command.quality.merge(feedback.quality);
        if command.value.is_nan() || feedback.value.is_nan() {
            flag_quality = flag_quality.merge(Quality::Bad(QualityReason::DeviceFault));
        }
        io.write_sample(
            self.discrepancy,
            Sample::new(Value::Bool(discrepancy), flag_quality, tick),
        )?;
        Ok(())
    }

    /// Describes the actuator: `cmd` is the position request the block is
    /// driven toward, `out` the field command, `fb` the measured position
    /// feedback it verifies, `discrepancy` the reported diagnostic; the
    /// `tolerance` and `discrepancy_ticks` parameters.
    fn describe(&self) -> ComponentDescriptor {
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &[
                ("cmd", PortRole::Setpoint),
                ("out", PortRole::Output),
                ("fb", PortRole::ProcessValue),
                ("discrepancy", PortRole::Status),
            ],
            vec![
                describe::parameter(
                    "tolerance",
                    ValueKind::Float,
                    Some(describe::NONNEGATIVE_F64),
                ),
                describe::parameter(
                    "discrepancy_ticks",
                    ValueKind::Int,
                    Some(describe::NONNEGATIVE_INT),
                ),
            ],
        )
    }

    /// Tunes a declared parameter at the scan boundary.
    ///
    /// The executor pre-checks the descriptor — declared name, kind,
    /// and the declared range — so this hook re-states only the
    /// constructor's finiteness rule on `tolerance`: the declared
    /// `NONNEGATIVE_F64` bound already covers it, but the hook does not
    /// rely on the executor's pre-check alone.
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
            "discrepancy_ticks" => {
                self.discrepancy_ticks = params::tune_u64(&self.name, parameter, value)?;
            }
            _ => return Err(params::unknown_parameter(&self.name, parameter)),
        }
        Ok(())
    }

    /// Reports the declared `tolerance`/`discrepancy_ticks` tuning —
    /// the same fields [`capture_state`](Self::capture_state)
    /// checkpoints, so the faceplate and a tracking standby read one
    /// vocabulary.
    fn report_parameters(&self) -> StateMap {
        let mut parameters = StateMap::new();
        parameters.insert("tolerance", Value::Float(self.tolerance));
        parameters.insert(
            "discrepancy_ticks",
            Value::Int(self.discrepancy_ticks as i64),
        );
        parameters
    }

    /// Captures the banked deviating-scan count and the tuned
    /// `tolerance`/`discrepancy_ticks` so a tracking standby continues
    /// the discrepancy count under the same tuning.
    fn capture_state(&self) -> StateMap {
        let mut state = self.report_parameters();
        state.insert("deviating", Value::Int(self.deviating as i64));
        state
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        state.ensure_known_fields(&self.name, &["deviating", "tolerance", "discrepancy_ticks"])?;
        let deviating = state.require_i64(&self.name, "deviating")?;
        if deviating < 0 {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "deviating".to_string(),
                value: Value::Int(deviating),
            });
        }
        let tolerance = state.require_f64(&self.name, "tolerance")?;
        if !tolerance.is_finite() || tolerance < 0.0 {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "tolerance".to_string(),
                value: Value::Float(tolerance),
            });
        }
        let discrepancy_ticks = state.require_i64(&self.name, "discrepancy_ticks")?;
        if discrepancy_ticks < 0 {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "discrepancy_ticks".to_string(),
                value: Value::Int(discrepancy_ticks),
            });
        }
        self.deviating = deviating as u64;
        self.tolerance = tolerance;
        self.discrepancy_ticks = discrepancy_ticks as u64;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::TestIo;
    use dcs_core::{Direction, ParameterDescriptor, PortDescriptor, Quality};
    use dcs_model::{ComponentId, ComponentInstance};
    use std::collections::BTreeMap;

    const CMD: PointId = PointId(90);
    const OUT: PointId = PointId(91);
    const FB: PointId = PointId(92);
    const DISCREPANCY: PointId = PointId(93);

    /// A valve with tolerance 2 and the command and feedback both at 50.
    fn component() -> Valve {
        Valve::new("vlv", CMD, OUT, FB, DISCREPANCY, 2.0).unwrap()
    }

    fn io() -> TestIo {
        TestIo::new(&[
            (
                CMD,
                Direction::In,
                Sample::good(Value::Float(50.0), Tick::ZERO),
            ),
            (
                OUT,
                Direction::Out,
                Sample::good(Value::Float(0.0), Tick::ZERO),
            ),
            (
                FB,
                Direction::In,
                Sample::good(Value::Float(50.0), Tick::ZERO),
            ),
            (
                DISCREPANCY,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
        ])
    }

    fn feed_fb(io: &TestIo, value: f64, tick: u64) {
        io.feed(FB, Sample::good(Value::Float(value), Tick(tick)));
    }

    fn discrepancy(io: &TestIo) -> Sample {
        io.written(DISCREPANCY).unwrap()
    }

    #[test]
    fn echoes_command_to_the_field_and_tracks_matching_feedback() {
        let mut valve = component();
        let io = io();
        valve.step(&io, Tick(1)).unwrap();
        let out = io.written(OUT).unwrap();
        assert_eq!(out.value, Value::Float(50.0));
        assert_eq!(out.quality, Quality::Good);
        assert_eq!(out.tick, Tick(1));
        let flag = discrepancy(&io);
        assert_eq!(flag.value, Value::Bool(false));
        assert_eq!(flag.quality, Quality::Good);

        // A new command the feedback follows stays clear.
        io.feed(CMD, Sample::good(Value::Float(80.0), Tick(2)));
        feed_fb(&io, 79.0, 2);
        valve.step(&io, Tick(2)).unwrap();
        assert_eq!(io.written(OUT).unwrap().value, Value::Float(80.0));
        assert_eq!(discrepancy(&io).value, Value::Bool(false));
    }

    #[test]
    fn flags_discrepancy_only_after_the_configured_tick_count() {
        let mut valve = component().with_discrepancy_ticks(3);
        let io = io();
        // Feedback stuck at 40 against a command of 50 deviates by 10.
        for tick in 1..=2 {
            feed_fb(&io, 40.0, tick);
            valve.step(&io, Tick(tick)).unwrap();
            assert_eq!(discrepancy(&io).value, Value::Bool(false), "tick={tick}");
        }
        // The third consecutive deviating scan asserts the flag.
        feed_fb(&io, 40.0, 3);
        valve.step(&io, Tick(3)).unwrap();
        assert_eq!(discrepancy(&io).value, Value::Bool(true));

        // The count resets when the feedback agrees again: a single new
        // deviation does not re-flag.
        feed_fb(&io, 50.0, 4);
        valve.step(&io, Tick(4)).unwrap();
        assert_eq!(discrepancy(&io).value, Value::Bool(false));
        feed_fb(&io, 40.0, 5);
        valve.step(&io, Tick(5)).unwrap();
        assert_eq!(discrepancy(&io).value, Value::Bool(false));
    }

    #[test]
    fn flags_the_first_deviating_scan_with_zero_or_one_ticks() {
        for ticks in [0, 1] {
            let mut valve = component().with_discrepancy_ticks(ticks);
            let io = io();
            feed_fb(&io, 40.0, 1);
            valve.step(&io, Tick(1)).unwrap();
            assert_eq!(discrepancy(&io).value, Value::Bool(true), "ticks={ticks}");
        }
    }

    #[test]
    fn feedback_at_the_tolerance_boundary_does_not_deviate() {
        let mut valve = component();
        let io = io();
        // |fb - cmd| == tolerance still counts as agreement; only a
        // strictly larger deviation deviates.
        for (tick, fb) in [(1, 48.0), (2, 52.0), (3, 52.0000001)] {
            feed_fb(&io, fb, tick);
            valve.step(&io, Tick(tick)).unwrap();
            let expected = fb - 50.0 > 2.0;
            assert_eq!(discrepancy(&io).value, Value::Bool(expected), "fb={fb}");
        }
    }

    #[test]
    fn bad_feedback_counts_as_deviating_and_marks_the_flag() {
        let mut valve = component().with_discrepancy_ticks(2);
        let io = io();
        // A Bad-quality feedback cannot prove agreement even while its
        // value sits within tolerance.
        io.feed(
            FB,
            Sample::new(
                Value::Float(50.0),
                Quality::Bad(QualityReason::CommunicationFault),
                Tick(1),
            ),
        );
        valve.step(&io, Tick(1)).unwrap();
        let flag = discrepancy(&io);
        assert_eq!(flag.value, Value::Bool(false));
        assert_eq!(
            flag.quality,
            Quality::Bad(QualityReason::CommunicationFault)
        );

        valve.step(&io, Tick(2)).unwrap();
        let flag = discrepancy(&io);
        assert_eq!(flag.value, Value::Bool(true));
        assert_eq!(
            flag.quality,
            Quality::Bad(QualityReason::CommunicationFault)
        );
    }

    #[test]
    fn command_quality_propagates_to_the_field_write_and_flag() {
        let mut valve = component();
        let io = io();
        io.feed(
            CMD,
            Sample::new(
                Value::Float(50.0),
                Quality::Uncertain(QualityReason::Substituted),
                Tick(1),
            ),
        );
        valve.step(&io, Tick(1)).unwrap();
        assert_eq!(
            io.written(OUT).unwrap().quality,
            Quality::Uncertain(QualityReason::Substituted)
        );
        assert_eq!(
            discrepancy(&io).quality,
            Quality::Uncertain(QualityReason::Substituted)
        );
    }

    #[test]
    fn nan_input_marks_outputs_bad() {
        let mut valve = component();
        let io = io();
        io.feed(CMD, Sample::good(Value::Float(f64::NAN), Tick(1)));
        valve.step(&io, Tick(1)).unwrap();
        assert_eq!(
            io.written(OUT).unwrap().quality,
            Quality::Bad(QualityReason::DeviceFault)
        );
        let flag = discrepancy(&io);
        assert_eq!(flag.value, Value::Bool(true));
        assert_eq!(flag.quality, Quality::Bad(QualityReason::DeviceFault));
    }

    #[test]
    fn builds_from_parameter_map() {
        let parameters: Parameters = [
            ("tolerance".to_string(), Value::Float(1.5)),
            ("discrepancy_ticks".to_string(), Value::Int(2)),
        ]
        .into_iter()
        .collect();
        let instance = ComponentInstance {
            id: ComponentId(9),
            kind: Valve::KIND.to_string(),
            parameters,
            ports: BTreeMap::new(),
        };
        let mut valve =
            Valve::from_parameters("vlv", CMD, OUT, FB, DISCREPANCY, &instance.parameters).unwrap();
        assert_eq!(valve.tolerance, 1.5);
        assert_eq!(valve.discrepancy_ticks, 2);

        // Two deviating scans assert the flag under the built config.
        let io = io();
        feed_fb(&io, 40.0, 1);
        valve.step(&io, Tick(1)).unwrap();
        assert_eq!(discrepancy(&io).value, Value::Bool(false));
        valve.step(&io, Tick(2)).unwrap();
        assert_eq!(discrepancy(&io).value, Value::Bool(true));

        // tolerance is required; discrepancy_ticks is optional.
        assert!(matches!(
            Valve::from_parameters("vlv", CMD, OUT, FB, DISCREPANCY, &Parameters::new())
                .unwrap_err(),
            ParameterError::Missing { ref parameter, .. } if parameter == "tolerance"
        ));
        let wrong: Parameters = [("tolerance".to_string(), Value::Float(-1.0))]
            .into_iter()
            .collect();
        assert!(matches!(
            Valve::from_parameters("vlv", CMD, OUT, FB, DISCREPANCY, &wrong).unwrap_err(),
            ParameterError::Invalid { ref parameter, .. } if parameter == "tolerance"
        ));
        let bad_ticks: Parameters = [
            ("tolerance".to_string(), Value::Float(1.0)),
            ("discrepancy_ticks".to_string(), Value::Float(0.5)),
        ]
        .into_iter()
        .collect();
        assert!(matches!(
            Valve::from_parameters("vlv", CMD, OUT, FB, DISCREPANCY, &bad_ticks).unwrap_err(),
            ParameterError::Invalid { ref parameter, .. } if parameter == "discrepancy_ticks"
        ));
    }

    #[test]
    fn describes_itself() {
        let descriptor = component().describe();
        assert_eq!(descriptor.name, "vlv");
        assert_eq!(descriptor.kind, Valve::KIND);
        assert_eq!(descriptor.label, "vlv");
        assert_eq!(
            descriptor.ports,
            [
                PortDescriptor {
                    name: "cmd".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: Some(PortRole::Setpoint),
                    point: None,
                },
                PortDescriptor {
                    name: "out".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Float,
                    role: Some(PortRole::Output),
                    point: None,
                },
                PortDescriptor {
                    name: "fb".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: Some(PortRole::ProcessValue),
                    point: None,
                },
                PortDescriptor {
                    name: "discrepancy".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Status),
                    point: None,
                },
            ]
        );
        // Drift guard: the descriptor's parameter names are exactly the
        // keys `from_parameters` reads.
        assert_eq!(
            descriptor.parameters,
            [
                ParameterDescriptor {
                    name: "tolerance".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::NONNEGATIVE_F64),
                },
                ParameterDescriptor {
                    name: "discrepancy_ticks".to_string(),
                    kind: ValueKind::Int,
                    range: Some(describe::NONNEGATIVE_INT),
                },
            ]
        );
    }
}
