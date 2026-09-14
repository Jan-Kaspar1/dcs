//! Digital output: boolean write with quality propagation.

use crate::describe;
use crate::params::{self, ParameterError, Parameters};
use dcs_core::{
    CommandError, ComponentDescriptor, PointId, PortRole, Sample, StateError, StateMap, Tick,
    Value, ValueKind,
};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// A digital output channel: copies a boolean command `In` point onto a
/// boolean field `Out` point, optionally inverting it.
///
/// The output sample carries the command's quality — a non-`Good` command
/// produces a non-`Good` output of the same severity — so downstream
/// consumers see degraded commands for what they are.
///
/// Declared I/O: `in` (`In`, `Bool`) and `out` (`Out`, `Bool`).
///
/// Parameters: `invert` — optional `Bool` (or `0`/`1`), default `false`.
/// When set, the written value is the command's logical negation.
#[derive(Debug)]
pub struct DigitalOutput {
    name: String,
    input: PointId,
    output: PointId,
    invert: bool,
}

impl DigitalOutput {
    /// The component-kind string the model-driven registry maps onto this
    /// type's constructor.
    pub const KIND: &'static str = "digital-output";

    /// A non-inverting digital output.
    pub fn new(name: impl Into<String>, input: PointId, output: PointId) -> Self {
        Self {
            name: name.into(),
            input,
            output,
            invert: false,
        }
    }

    /// Sets whether the written value is the command's negation.
    pub fn with_invert(mut self, invert: bool) -> Self {
        self.invert = invert;
        self
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
        let invert = params::optional_bool(&name, parameters, "invert")?.unwrap_or(false);
        Ok(Self::new(name, input, output).with_invert(invert))
    }
}

impl Component for DigitalOutput {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        vec![
            IoRequirement::input::<bool>("in", self.input),
            IoRequirement::output::<bool>("out", self.output),
        ]
    }

    fn step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError> {
        let command = io.read_typed::<bool>(self.input)?;
        io.write_sample(
            self.output,
            Sample::new(
                Value::Bool(command.value ^ self.invert),
                command.quality,
                tick,
            ),
        )?;
        Ok(())
    }

    /// Describes the channel: `in` is the commanded state the block
    /// echoes, `out` the field value it writes; the `invert` parameter.
    fn describe(&self) -> ComponentDescriptor {
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &[("in", PortRole::Setpoint), ("out", PortRole::Output)],
            vec![describe::parameter("invert", ValueKind::Bool, None)],
        )
    }

    /// Tunes `invert` at the scan boundary; the next step's write uses
    /// the new sense.
    fn apply_parameter(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        match parameter {
            "invert" => self.invert = params::tune_bool(&self.name, parameter, value)?,
            _ => return Err(params::unknown_parameter(&self.name, parameter)),
        }
        Ok(())
    }

    /// Captures the tuned `invert` — the block's only run state.
    fn capture_state(&self) -> StateMap {
        let mut state = StateMap::new();
        state.insert("invert", Value::Bool(self.invert));
        state
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        state.ensure_known_fields(&self.name, &["invert"])?;
        self.invert = state.require_bool(&self.name, "invert")?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::TestIo;
    use dcs_core::{Direction, ParameterDescriptor, PortDescriptor, Quality, QualityReason};
    use dcs_model::{ComponentId, ComponentInstance};
    use std::collections::BTreeMap;

    const IN: PointId = PointId(30);
    const OUT: PointId = PointId(31);

    fn io(command: Sample) -> TestIo {
        TestIo::new(&[
            (IN, Direction::In, command),
            (
                OUT,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
        ])
    }

    #[test]
    fn writes_command_and_propagates_quality() {
        let mut block = DigitalOutput::new("do", IN, OUT);
        let io = io(Sample::good(Value::Bool(true), Tick::ZERO));
        block.step(&io, Tick(1)).unwrap();
        let out = io.written(OUT).unwrap();
        assert_eq!(out.value, Value::Bool(true));
        assert_eq!(out.quality, Quality::Good);
        assert_eq!(out.tick, Tick(1));

        io.feed(
            IN,
            Sample::new(
                Value::Bool(false),
                Quality::Uncertain(QualityReason::Substituted),
                Tick(2),
            ),
        );
        block.step(&io, Tick(2)).unwrap();
        let out = io.written(OUT).unwrap();
        assert_eq!(out.value, Value::Bool(false));
        assert_eq!(out.quality, Quality::Uncertain(QualityReason::Substituted));
    }

    #[test]
    fn invert_negates_the_command() {
        let mut block = DigitalOutput::new("do", IN, OUT).with_invert(true);
        let io = io(Sample::good(Value::Bool(false), Tick::ZERO));
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(io.written(OUT).unwrap().value, Value::Bool(true));
    }

    #[test]
    fn builds_from_parameter_map() {
        let parameters: Parameters = [("invert".to_string(), Value::Bool(true))]
            .into_iter()
            .collect();
        let instance = ComponentInstance {
            id: ComponentId(3),
            kind: DigitalOutput::KIND.to_string(),
            parameters,
            ports: BTreeMap::new(),
        };
        let mut block =
            DigitalOutput::from_parameters("do", IN, OUT, &instance.parameters).unwrap();
        assert!(block.invert);
        let io = io(Sample::good(Value::Bool(true), Tick::ZERO));
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(io.written(OUT).unwrap().value, Value::Bool(false));

        // The parameter is optional; an empty map builds the plain block.
        let block = DigitalOutput::from_parameters("do", IN, OUT, &Parameters::new()).unwrap();
        assert!(!block.invert);

        let wrong: Parameters = [("invert".to_string(), Value::Float(0.5))]
            .into_iter()
            .collect();
        assert!(matches!(
            DigitalOutput::from_parameters("do", IN, OUT, &wrong).unwrap_err(),
            ParameterError::Invalid { ref parameter, .. } if parameter == "invert"
        ));
    }

    #[test]
    fn describes_itself() {
        let descriptor = DigitalOutput::new("do", IN, OUT).describe();
        assert_eq!(descriptor.name, "do");
        assert_eq!(descriptor.kind, DigitalOutput::KIND);
        assert_eq!(descriptor.label, "do");
        assert_eq!(
            descriptor.ports,
            [
                PortDescriptor {
                    name: "in".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Setpoint),
                },
                PortDescriptor {
                    name: "out".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Output),
                },
            ]
        );
        // Drift guard: the descriptor's parameter names are exactly the
        // keys `from_parameters` reads.
        assert_eq!(
            descriptor.parameters,
            [ParameterDescriptor {
                name: "invert".to_string(),
                kind: ValueKind::Bool,
                range: None,
            }]
        );
    }
}
