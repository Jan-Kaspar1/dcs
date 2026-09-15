//! Boolean gate: an N-input `and`/`or`/`xor` truth function over
//! `in_1` … `in_N`, producing one Bool `out`.

use crate::describe;
use crate::params::{self, ParameterError, Parameters};
use dcs_core::{
    CommandError, ComponentDescriptor, ParameterRange, PointId, PortRole, Quality, Sample,
    StateError, StateMap, Tick, Value, ValueKind,
};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// The truth function a [`BoolGate`] applies across its declared inputs.
///
/// The model's parameter vocabulary has no string type, so the
/// `operation` parameter carries the choice as an `Int` code: `0` for
/// `And`, `1` for `Or`, `2` for `Xor` — the values [`code`](Self::code)
/// reports and [`decode`](Self::decode) accepts.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GateOperation {
    /// `out` asserts while every declared input reads `true`.
    And,
    /// `out` asserts while at least one declared input reads `true`.
    Or,
    /// `out` asserts while an odd number of declared inputs read `true`.
    Xor,
}

impl GateOperation {
    /// The `Int` code the `operation` parameter carries.
    pub const fn code(self) -> i64 {
        match self {
            Self::And => 0,
            Self::Or => 1,
            Self::Xor => 2,
        }
    }

    /// The operation the `Int` code `code` selects, or `None` when the
    /// code declares no operation.
    pub const fn decode(code: i64) -> Option<Self> {
        match code {
            0 => Some(Self::And),
            1 => Some(Self::Or),
            2 => Some(Self::Xor),
            _ => None,
        }
    }

    /// The fold's seed: the operation's identity element.
    const fn identity(self) -> bool {
        match self {
            Self::And => true,
            Self::Or | Self::Xor => false,
        }
    }

    /// Folds one more input into the running value.
    const fn apply(self, folded: bool, input: bool) -> bool {
        match self {
            Self::And => folded && input,
            Self::Or => folded || input,
            Self::Xor => folded != input,
        }
    }
}

/// A boolean gate: folds the declared `in_1` … `in_N` inputs through the
/// configured [`GateOperation`] and drives the result on `out`.
///
/// **Input set:** the input set is variable-arity, following the
/// `interlock` kind's `trip_N` convention — the registry binds every
/// wired `in_N` port ordered by numeric suffix, so the model declares
/// exactly the input count it needs. The fold runs from the operation's
/// identity element, so an empty input set — an instance wired with no
/// `in_N` ports — produces the identity every scan: `and` reads `true`,
/// `or` and `xor` read `false`. A single input passes through unchanged
/// under every operation.
///
/// **Quality rule:** `out` carries the worst of the declared inputs'
/// qualities — the merge folds every input, including ones whose value
/// the truth function ignores — so a degraded input marks the output
/// while the truth function itself stays a deterministic function of the
/// values read. An empty input set reports [`Quality::Good`]: the
/// identity is a known constant.
///
/// Declared I/O: `in_1` … `in_N` (`In`, `Bool`), `out` (`Out`, `Bool`).
///
/// Parameters: `operation` — required `Int` (or integral `Float`)
/// carrying a [`GateOperation`] code: `0` `and`, `1` `or`, `2` `xor`.
#[derive(Debug)]
pub struct BoolGate {
    name: String,
    inputs: Vec<PointId>,
    output: PointId,
    operation: GateOperation,
}

/// The inclusive `Int` bound the `operation` parameter accepts: the
/// declared [`GateOperation`] codes.
const OPERATION_RANGE: ParameterRange = ParameterRange {
    min: Value::Int(0),
    max: Value::Int(2),
};

impl BoolGate {
    /// The component-kind string the model-driven registry maps onto
    /// this type's constructor.
    pub const KIND: &'static str = "bool-gate";

    /// Builds the component from explicit points and the selected
    /// operation.
    ///
    /// `inputs` are the Boolean gate inputs, declared as `in_1` … `in_N`
    /// in the given order; an empty `Vec` declares the documented
    /// constant-identity gate.
    pub fn new(
        name: impl Into<String>,
        inputs: Vec<PointId>,
        output: PointId,
        operation: GateOperation,
    ) -> Self {
        Self {
            name: name.into(),
            inputs,
            output,
            operation,
        }
    }

    /// Builds the component from a plant-model parameter map, reading the
    /// parameters listed on the type's docs.
    pub fn from_parameters(
        name: impl Into<String>,
        inputs: Vec<PointId>,
        output: PointId,
        parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        let code = params::required_u64(&name, parameters, "operation")?;
        let operation = GateOperation::decode(code as i64).ok_or_else(|| {
            params::invalid(
                &name,
                "operation",
                format!("expected 0 (and), 1 (or), or 2 (xor), found {code}"),
            )
        })?;
        Ok(Self::new(name, inputs, output, operation))
    }
}

impl Component for BoolGate {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        let mut requirements = Vec::with_capacity(self.inputs.len() + 1);
        requirements.extend(self.inputs.iter().enumerate().map(|(index, point)| {
            IoRequirement::input::<bool>(format!("in_{}", index + 1), *point)
        }));
        requirements.push(IoRequirement::output::<bool>("out", self.output));
        requirements
    }

    fn step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError> {
        let mut folded = self.operation.identity();
        let mut quality = Quality::Good;
        for input in &self.inputs {
            let sample = io.read_typed::<bool>(*input)?;
            quality = quality.merge(sample.quality);
            folded = self.operation.apply(folded, sample.value);
        }
        io.write_sample(self.output, Sample::new(Value::Bool(folded), quality, tick))?;
        Ok(())
    }

    /// Describes the gate: the `in_N` set is the combined inputs, `out`
    /// the driven result; the `operation` parameter with its declared
    /// code range.
    fn describe(&self) -> ComponentDescriptor {
        let roles: Vec<(String, PortRole)> = (1..=self.inputs.len())
            .map(|index| (format!("in_{index}"), PortRole::ProcessValue))
            .chain([("out".to_string(), PortRole::Output)])
            .collect();
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &roles,
            vec![describe::parameter(
                "operation",
                ValueKind::Int,
                Some(OPERATION_RANGE),
            )],
        )
    }

    /// Tunes `operation` at the scan boundary. The declared
    /// `OPERATION_RANGE` bound already bars codes outside `0..=2`; the
    /// hook re-checks so a caller bypassing the executor cannot install
    /// a code the gate cannot evaluate.
    fn apply_parameter(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        match parameter {
            "operation" => {
                let code = params::tune_u64(&self.name, parameter, value)?;
                self.operation = GateOperation::decode(code as i64).ok_or_else(|| {
                    params::invalid_parameter(
                        &self.name,
                        parameter,
                        "expected 0 (and), 1 (or), or 2 (xor)",
                    )
                })?;
            }
            _ => return Err(params::unknown_parameter(&self.name, parameter)),
        }
        Ok(())
    }

    /// Reports the declared `operation` — the same field
    /// [`capture_state`](Self::capture_state) checkpoints, so the
    /// faceplate and a tracking standby read one vocabulary.
    fn report_parameters(&self) -> StateMap {
        let mut parameters = StateMap::new();
        parameters.insert("operation", Value::Int(self.operation.code()));
        parameters
    }

    /// Captures the tuned `operation` — the gate is otherwise stateless,
    /// but runtime tuning is run state a standby must inherit.
    fn capture_state(&self) -> StateMap {
        self.report_parameters()
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        state.ensure_known_fields(&self.name, &["operation"])?;
        let code = state.require_i64(&self.name, "operation")?;
        self.operation = GateOperation::decode(code).ok_or_else(|| StateError::InvalidValue {
            element: self.name.clone(),
            field: "operation".to_string(),
            value: Value::Int(code),
        })?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::TestIo;
    use dcs_core::{Direction, ParameterDescriptor, PortDescriptor, QualityReason};
    use dcs_model::{ComponentId, ComponentInstance};
    use std::collections::BTreeMap;

    const IN_1: PointId = PointId(200);
    const IN_2: PointId = PointId(201);
    const IN_3: PointId = PointId(202);
    const OUT: PointId = PointId(203);

    /// A three-input gate running `operation`.
    fn component(operation: GateOperation) -> BoolGate {
        BoolGate::new("gate", vec![IN_1, IN_2, IN_3], OUT, operation)
    }

    fn io() -> TestIo {
        TestIo::new(&[
            (
                IN_1,
                Direction::In,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
            (
                IN_2,
                Direction::In,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
            (
                IN_3,
                Direction::In,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
            (
                OUT,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
        ])
    }

    /// Feeds the three inputs and steps once.
    fn step(block: &mut BoolGate, io: &TestIo, inputs: [bool; 3], tick: u64) {
        for (point, value) in [IN_1, IN_2, IN_3].into_iter().zip(inputs) {
            io.feed(point, Sample::good(Value::Bool(value), Tick(tick)));
        }
        block.step(io, Tick(tick)).unwrap();
    }

    fn out(io: &TestIo) -> Sample {
        io.written(OUT).unwrap()
    }

    fn driven(io: &TestIo) -> bool {
        out(io).value == Value::Bool(true)
    }

    #[test]
    fn and_asserts_only_when_every_input_holds() {
        let mut block = component(GateOperation::And);
        let io = io();

        // Partial assertion does not satisfy `and`.
        for (tick, inputs) in [
            (1, [true, true, false]),
            (2, [true, false, true]),
            (3, [false, false, false]),
        ] {
            step(&mut block, &io, inputs, tick);
            assert!(!driven(&io), "tick={tick}");
        }

        // All three asserted: the gate opens; dropping one closes it on
        // the same scan — the fold is stateless.
        step(&mut block, &io, [true, true, true], 4);
        assert!(driven(&io));
        step(&mut block, &io, [true, false, true], 5);
        assert!(!driven(&io));
    }

    #[test]
    fn or_asserts_when_any_input_holds() {
        let mut block = component(GateOperation::Or);
        let io = io();

        step(&mut block, &io, [false, false, false], 1);
        assert!(!driven(&io));
        for (tick, inputs) in [
            (2, [true, false, false]),
            (3, [false, false, true]),
            (4, [true, true, true]),
        ] {
            step(&mut block, &io, inputs, tick);
            assert!(driven(&io), "tick={tick}");
        }
        step(&mut block, &io, [false, false, false], 5);
        assert!(!driven(&io));
    }

    #[test]
    fn xor_asserts_on_odd_parity() {
        let mut block = component(GateOperation::Xor);
        let io = io();

        // Odd counts assert; even counts — including all-asserted — do
        // not.
        for (tick, inputs, expected) in [
            (1, [false, false, false], false),
            (2, [true, false, false], true),
            (3, [true, true, false], false),
            (4, [true, true, true], true),
            (5, [false, true, true], false),
        ] {
            step(&mut block, &io, inputs, tick);
            assert_eq!(driven(&io), expected, "tick={tick}");
        }
    }

    #[test]
    fn empty_and_single_inputs_follow_the_documented_identity_rule() {
        // No `in_N` ports: the fold yields the operation's identity —
        // `and` reads true, `or` and `xor` read false — at Good quality.
        for (operation, expected) in [
            (GateOperation::And, true),
            (GateOperation::Or, false),
            (GateOperation::Xor, false),
        ] {
            let mut block = BoolGate::new("gate", Vec::new(), OUT, operation);
            let io = TestIo::new(&[(
                OUT,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            )]);
            block.step(&io, Tick(1)).unwrap();
            assert_eq!(
                out(&io),
                Sample::new(Value::Bool(expected), Quality::Good, Tick(1)),
                "{operation:?}"
            );
        }

        // One input passes through under every operation.
        for operation in [GateOperation::And, GateOperation::Or, GateOperation::Xor] {
            let mut block = BoolGate::new("gate", vec![IN_1], OUT, operation);
            let io = TestIo::new(&[
                (
                    IN_1,
                    Direction::In,
                    Sample::good(Value::Bool(false), Tick::ZERO),
                ),
                (
                    OUT,
                    Direction::Out,
                    Sample::good(Value::Bool(false), Tick::ZERO),
                ),
            ]);
            block.step(&io, Tick(1)).unwrap();
            assert!(!driven(&io), "{operation:?}");
            io.feed(IN_1, Sample::good(Value::Bool(true), Tick(2)));
            block.step(&io, Tick(2)).unwrap();
            assert!(driven(&io), "{operation:?}");
        }
    }

    #[test]
    fn output_carries_the_worst_input_quality() {
        let mut block = component(GateOperation::And);
        let io = io();
        step(&mut block, &io, [true, true, true], 1);
        assert_eq!(out(&io).quality, Quality::Good);

        // A non-Good input marks the output even where the truth
        // function's result is already decided by other inputs.
        io.feed(
            IN_3,
            Sample::new(
                Value::Bool(true),
                Quality::Uncertain(QualityReason::Stale),
                Tick(2),
            ),
        );
        block.step(&io, Tick(2)).unwrap();
        assert_eq!(out(&io).value, Value::Bool(true));
        assert_eq!(out(&io).quality, Quality::Uncertain(QualityReason::Stale));

        // A Bad input whose false value decides `and` still drives the
        // folded result, marked with its quality.
        io.feed(
            IN_2,
            Sample::new(
                Value::Bool(false),
                Quality::Bad(QualityReason::DeviceFault),
                Tick(3),
            ),
        );
        block.step(&io, Tick(3)).unwrap();
        assert_eq!(out(&io).value, Value::Bool(false));
        assert_eq!(out(&io).quality, Quality::Bad(QualityReason::DeviceFault));
    }

    #[test]
    fn tuned_operation_continues_across_restore() {
        // The gate is stateless apart from its tuning: a runtime-tuned
        // `operation` is captured run state the standby inherits.
        let mut block = component(GateOperation::And);
        block
            .apply_parameter("operation", Value::Int(GateOperation::Or.code()))
            .unwrap();
        let state = block.capture_state();
        assert_eq!(
            state.get("operation"),
            Some(Value::Int(GateOperation::Or.code()))
        );

        let mut standby = component(GateOperation::And);
        standby.restore_state(&state).unwrap();
        assert_eq!(standby.capture_state(), state);

        // The restored gate runs the tuned operation.
        let io = io();
        step(&mut standby, &io, [true, false, false], 1);
        assert!(driven(&io));
    }

    #[test]
    fn tuning_and_restore_reject_undeclared_or_unknown_codes() {
        let mut block = component(GateOperation::And);
        assert!(matches!(
            block.apply_parameter("operation", Value::Int(7)),
            Err(CommandError::InvalidParameter { ref parameter, .. }) if parameter == "operation"
        ));
        assert!(matches!(
            block.apply_parameter("nope", Value::Int(0)),
            Err(CommandError::UnknownParameter { ref parameter, .. }) if parameter == "nope"
        ));
        // A refused tune changes nothing.
        let io = io();
        step(&mut block, &io, [true, false, false], 1);
        assert!(!driven(&io));

        let mut state = StateMap::new();
        state.insert("operation", Value::Int(7));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::InvalidValue { ref field, .. }) if field == "operation"
        ));
        let mut state = StateMap::new();
        state.insert("operation", Value::Bool(true));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::IncompatibleField { ref field, .. }) if field == "operation"
        ));
        assert!(matches!(
            block.restore_state(&StateMap::new()),
            Err(StateError::MissingField { ref field, .. }) if field == "operation"
        ));
    }

    #[test]
    fn builds_from_parameter_map() {
        let parameters: Parameters = [("operation".to_string(), Value::Int(1))]
            .into_iter()
            .collect();
        let instance = ComponentInstance {
            id: ComponentId(30),
            kind: BoolGate::KIND.to_string(),
            parameters,
            rationalization: None,
            ports: BTreeMap::new(),
        };
        let mut block =
            BoolGate::from_parameters("gate", vec![IN_1, IN_2], OUT, &instance.parameters).unwrap();
        assert_eq!(block.operation, GateOperation::Or);
        let io = TestIo::new(&[
            (
                IN_1,
                Direction::In,
                Sample::good(Value::Bool(true), Tick::ZERO),
            ),
            (
                IN_2,
                Direction::In,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
            (
                OUT,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
        ]);
        block.step(&io, Tick(1)).unwrap();
        assert!(driven(&io));

        // `operation` is required and must carry a declared code.
        assert!(matches!(
            BoolGate::from_parameters("gate", vec![IN_1], OUT, &Parameters::new()).unwrap_err(),
            ParameterError::Missing { ref parameter, .. } if parameter == "operation"
        ));
        for value in [
            Value::Int(3),
            Value::Int(-1),
            Value::Float(1.5),
            Value::Bool(true),
        ] {
            let bad: Parameters = [("operation".to_string(), value)].into_iter().collect();
            assert!(
                matches!(
                    BoolGate::from_parameters("gate", vec![IN_1], OUT, &bad).unwrap_err(),
                    ParameterError::Invalid { ref parameter, .. } if parameter == "operation"
                ),
                "value={value:?}"
            );
        }
    }

    #[test]
    fn describes_itself() {
        let descriptor = component(GateOperation::Xor).describe();
        assert_eq!(descriptor.name, "gate");
        assert_eq!(descriptor.kind, BoolGate::KIND);
        assert_eq!(descriptor.label, "gate");
        // Ports follow the declared-I/O order: the in_N set, then out.
        assert_eq!(
            descriptor.ports,
            [
                PortDescriptor {
                    name: "in_1".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::ProcessValue),
                    point: None,
                },
                PortDescriptor {
                    name: "in_2".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::ProcessValue),
                    point: None,
                },
                PortDescriptor {
                    name: "in_3".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::ProcessValue),
                    point: None,
                },
                PortDescriptor {
                    name: "out".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Bool,
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
                name: "operation".to_string(),
                kind: ValueKind::Int,
                range: Some(OPERATION_RANGE),
            }]
        );
    }
}
