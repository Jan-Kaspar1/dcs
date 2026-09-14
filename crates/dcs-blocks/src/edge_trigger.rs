//! Edge trigger: a one-scan Bool pulse on the rising, falling, or both
//! edges of `in`.

use crate::describe;
use crate::params::{self, ParameterError, Parameters};
use dcs_core::{
    CommandError, ComponentDescriptor, ParameterRange, PointId, PortRole, Sample, StateError,
    StateMap, Tick, Value, ValueKind,
};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// Which edge of `in` an [`EdgeTrigger`] fires on.
///
/// The model's parameter vocabulary has no string type, so the `edge`
/// parameter carries the choice as an `Int` code: `0` for `Rising`, `1`
/// for `Falling`, `2` for `Both` — the values [`code`](Self::code)
/// reports and [`decode`](Self::decode) accepts.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Edge {
    /// `out` pulses on each `false` → `true` transition.
    Rising,
    /// `out` pulses on each `true` → `false` transition.
    Falling,
    /// `out` pulses on every level change.
    Both,
}

impl Edge {
    /// The `Int` code the `edge` parameter carries.
    pub const fn code(self) -> i64 {
        match self {
            Self::Rising => 0,
            Self::Falling => 1,
            Self::Both => 2,
        }
    }

    /// The edge the `Int` code `code` selects, or `None` when the code
    /// declares no edge.
    pub const fn decode(code: i64) -> Option<Self> {
        match code {
            0 => Some(Self::Rising),
            1 => Some(Self::Falling),
            2 => Some(Self::Both),
            _ => None,
        }
    }

    /// Whether a `previous` → `current` transition is a matching edge.
    const fn matches(self, previous: bool, current: bool) -> bool {
        match self {
            Self::Rising => !previous && current,
            Self::Falling => previous && !current,
            Self::Both => previous != current,
        }
    }
}

/// An edge trigger: asserts `out` for exactly one scan on each edge of
/// `in` the configured [`Edge`] selects.
///
/// Each scan the trigger compares `in`'s observed value with the
/// previous scan's; a matching transition asserts `out` on that scan
/// only — a held level produces no repeated pulse, and the next edge
/// needs the opposite transition first. The tracked previous value
/// starts `false`, so the first scan of a run reading `true` counts as
/// a rising edge — the same convention [`Counter`](crate::Counter)'s
/// edge detection follows; a first scan reading `false` matches no
/// edge under any selection.
///
/// **Quality rule:** the edge is a deterministic function of the values
/// observed — a level change on a degraded read still pulses for its
/// one scan — and `out` carries `in`'s quality, so a pulse computed
/// from an untrusted read is marked untrusted rather than suppressed.
///
/// Declared I/O: `in` (`In`, `Bool`), `out` (`Out`, `Bool`).
///
/// Parameters: `edge` — required `Int` (or integral `Float`) carrying an
/// [`Edge`] code: `0` rising, `1` falling, `2` both.
#[derive(Debug)]
pub struct EdgeTrigger {
    name: String,
    input: PointId,
    output: PointId,
    edge: Edge,
    /// The `in` value observed on the previous scan; `false` before the
    /// first, so a `true` first read is a rising edge.
    previous: bool,
}

/// The inclusive `Int` bound the `edge` parameter accepts: the declared
/// [`Edge`] codes.
const EDGE_RANGE: ParameterRange = ParameterRange {
    min: Value::Int(0),
    max: Value::Int(2),
};

impl EdgeTrigger {
    /// The component-kind string the model-driven registry maps onto
    /// this type's constructor.
    pub const KIND: &'static str = "edge-trigger";

    /// Builds the component from explicit points and the selected edge.
    pub fn new(name: impl Into<String>, input: PointId, output: PointId, edge: Edge) -> Self {
        Self {
            name: name.into(),
            input,
            output,
            edge,
            previous: false,
        }
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
        let code = params::required_u64(&name, parameters, "edge")?;
        let edge = Edge::decode(code as i64).ok_or_else(|| {
            params::invalid(
                &name,
                "edge",
                format!("expected 0 (rising), 1 (falling), or 2 (both), found {code}"),
            )
        })?;
        Ok(Self::new(name, input, output, edge))
    }
}

impl Component for EdgeTrigger {
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
        let sample = io.read_typed::<bool>(self.input)?;
        let fired = self.edge.matches(self.previous, sample.value);
        self.previous = sample.value;
        io.write_sample(
            self.output,
            Sample::new(Value::Bool(fired), sample.quality, tick),
        )?;
        Ok(())
    }

    /// Describes the trigger: `in` is the observed signal, `out` the
    /// one-scan pulse; the `edge` parameter with its declared code range.
    fn describe(&self) -> ComponentDescriptor {
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &[("in", PortRole::ProcessValue), ("out", PortRole::Output)],
            vec![describe::parameter(
                "edge",
                ValueKind::Int,
                Some(EDGE_RANGE),
            )],
        )
    }

    /// Tunes `edge` at the scan boundary. The declared `EDGE_RANGE`
    /// bound already bars codes outside `0..=2`; the hook re-checks so
    /// a caller bypassing the executor cannot install a code the
    /// trigger cannot evaluate. Retuning keeps the tracked `previous`
    /// level: the next scan compares under the new selection.
    fn apply_parameter(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        match parameter {
            "edge" => {
                let code = params::tune_u64(&self.name, parameter, value)?;
                self.edge = Edge::decode(code as i64).ok_or_else(|| {
                    params::invalid_parameter(
                        &self.name,
                        parameter,
                        "expected 0 (rising), 1 (falling), or 2 (both)",
                    )
                })?;
            }
            _ => return Err(params::unknown_parameter(&self.name, parameter)),
        }
        Ok(())
    }

    /// Reports the declared `edge` — the same field
    /// [`capture_state`](Self::capture_state) checkpoints, so the
    /// faceplate and a tracking standby read one vocabulary.
    fn report_parameters(&self) -> StateMap {
        let mut parameters = StateMap::new();
        parameters.insert("edge", Value::Int(self.edge.code()));
        parameters
    }

    /// Captures the previous-scan input level — the state a standby
    /// needs so a held-high `in` does not re-pulse on its first scan —
    /// and the tuned `edge`.
    fn capture_state(&self) -> StateMap {
        let mut state = self.report_parameters();
        state.insert("previous", Value::Bool(self.previous));
        state
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        state.ensure_known_fields(&self.name, &["edge", "previous"])?;
        let code = state.require_i64(&self.name, "edge")?;
        self.edge = Edge::decode(code).ok_or_else(|| StateError::InvalidValue {
            element: self.name.clone(),
            field: "edge".to_string(),
            value: Value::Int(code),
        })?;
        self.previous = state.require_bool(&self.name, "previous")?;
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

    const IN: PointId = PointId(220);
    const OUT: PointId = PointId(221);

    /// A rising-edge trigger.
    fn component() -> EdgeTrigger {
        EdgeTrigger::new("etr", IN, OUT, Edge::Rising)
    }

    fn io() -> TestIo {
        TestIo::new(&[
            (
                IN,
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

    /// Feeds `in` and steps once.
    fn step(block: &mut EdgeTrigger, io: &TestIo, value: bool, tick: u64) {
        io.feed(IN, Sample::good(Value::Bool(value), Tick(tick)));
        block.step(io, Tick(tick)).unwrap();
    }

    fn out(io: &TestIo) -> Sample {
        io.written(OUT).unwrap()
    }

    fn pulsed(io: &TestIo) -> bool {
        out(io).value == Value::Bool(true)
    }

    #[test]
    fn rising_edge_pulses_for_exactly_one_scan() {
        let mut block = component();
        let io = io();

        // The rising scan pulses; the held level does not re-pulse.
        step(&mut block, &io, true, 1);
        assert!(pulsed(&io));
        for tick in 2..=3 {
            step(&mut block, &io, true, tick);
            assert!(!pulsed(&io), "tick={tick}");
        }

        // The falling scan is quiet; the next rising edge pulses again.
        step(&mut block, &io, false, 4);
        assert!(!pulsed(&io));
        step(&mut block, &io, true, 5);
        assert!(pulsed(&io));
        step(&mut block, &io, true, 6);
        assert!(!pulsed(&io));
    }

    #[test]
    fn falling_edge_pulses_for_exactly_one_scan() {
        let mut block = EdgeTrigger::new("etr", IN, OUT, Edge::Falling);
        let io = io();

        // Rising scans stay quiet under the falling selection.
        step(&mut block, &io, true, 1);
        assert!(!pulsed(&io));

        // The falling scan pulses; the held-low level does not.
        step(&mut block, &io, false, 2);
        assert!(pulsed(&io));
        step(&mut block, &io, false, 3);
        assert!(!pulsed(&io));
    }

    #[test]
    fn both_edges_pulse_for_exactly_one_scan_each() {
        let mut block = EdgeTrigger::new("etr", IN, OUT, Edge::Both);
        let io = io();

        step(&mut block, &io, true, 1);
        assert!(pulsed(&io));
        step(&mut block, &io, true, 2);
        assert!(!pulsed(&io));
        step(&mut block, &io, false, 3);
        assert!(pulsed(&io));
        step(&mut block, &io, false, 4);
        assert!(!pulsed(&io));
    }

    #[test]
    fn a_first_scan_high_is_a_rising_edge() {
        // The tracked level starts false, so a `true` first read fires
        // the rising selection — Counter's edge convention.
        let mut block = component();
        let high = TestIo::new(&[
            (
                IN,
                Direction::In,
                Sample::good(Value::Bool(true), Tick::ZERO),
            ),
            (
                OUT,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
        ]);
        block.step(&high, Tick(1)).unwrap();
        assert!(pulsed(&high));

        // A `false` first read matches no edge under any selection.
        for edge in [Edge::Rising, Edge::Falling, Edge::Both] {
            let mut block = EdgeTrigger::new("etr", IN, OUT, edge);
            let low = io();
            block.step(&low, Tick(1)).unwrap();
            assert!(!pulsed(&low), "{edge:?}");
        }
    }

    #[test]
    fn a_degraded_edge_still_pulses_marked_with_its_quality() {
        let mut block = component();
        let io = io();

        // The edge is a function of the values observed: a Bad read's
        // transition pulses for its one scan, carrying the Bad quality.
        io.feed(
            IN,
            Sample::new(
                Value::Bool(true),
                Quality::Bad(QualityReason::DeviceFault),
                Tick(1),
            ),
        );
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(
            out(&io),
            Sample::new(
                Value::Bool(true),
                Quality::Bad(QualityReason::DeviceFault),
                Tick(1)
            )
        );

        // The degraded level was still tracked: a return to Good at the
        // same level is no new edge.
        io.feed(IN, Sample::good(Value::Bool(true), Tick(2)));
        block.step(&io, Tick(2)).unwrap();
        assert_eq!(out(&io), Sample::good(Value::Bool(false), Tick(2)));
    }

    #[test]
    fn restored_trigger_continues_without_re_pulsing() {
        let mut block = component();
        let io = io();

        // `in` held high after its edge — checkpoint mid-level.
        step(&mut block, &io, true, 1);
        step(&mut block, &io, true, 2);
        let state = block.capture_state();
        assert_eq!(state.get("previous"), Some(Value::Bool(true)));

        // The standby's first scan sees the held level, not a fresh
        // edge: no re-pulse; the next falling→rising pair pulses once
        // more, identically to the uninterrupted run.
        let mut standby = component();
        standby.restore_state(&state).unwrap();
        assert_eq!(standby.capture_state(), state);
        step(&mut standby, &io, true, 3);
        assert!(!pulsed(&io));
        step(&mut standby, &io, false, 4);
        assert!(!pulsed(&io));
        step(&mut standby, &io, true, 5);
        assert!(pulsed(&io));
    }

    #[test]
    fn tuning_and_restore_reject_undeclared_or_unknown_codes() {
        let mut block = component();
        block
            .apply_parameter("edge", Value::Int(Edge::Both.code()))
            .unwrap();
        let state = block.capture_state();
        assert_eq!(state.get("edge"), Some(Value::Int(Edge::Both.code())));

        assert!(matches!(
            block.apply_parameter("edge", Value::Int(9)),
            Err(CommandError::InvalidParameter { ref parameter, .. }) if parameter == "edge"
        ));
        assert!(matches!(
            block.apply_parameter("edge", Value::Bool(true)),
            Err(CommandError::ParameterTypeMismatch { ref parameter, .. }) if parameter == "edge"
        ));
        assert!(matches!(
            block.apply_parameter("nope", Value::Int(0)),
            Err(CommandError::UnknownParameter { ref parameter, .. }) if parameter == "nope"
        ));
        // The refused tunes changed nothing: `both` still stands.
        assert_eq!(block.capture_state(), state);

        let mut state = StateMap::new();
        state.insert("edge", Value::Int(9));
        state.insert("previous", Value::Bool(false));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::InvalidValue { ref field, .. }) if field == "edge"
        ));
        let mut state = StateMap::new();
        state.insert("edge", Value::Int(0));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::MissingField { ref field, .. }) if field == "previous"
        ));
    }

    #[test]
    fn builds_from_parameter_map() {
        let parameters: Parameters = [("edge".to_string(), Value::Int(2))].into_iter().collect();
        let instance = ComponentInstance {
            id: ComponentId(32),
            kind: EdgeTrigger::KIND.to_string(),
            parameters,
            ports: BTreeMap::new(),
        };
        let mut block = EdgeTrigger::from_parameters("etr", IN, OUT, &instance.parameters).unwrap();
        assert_eq!(block.edge, Edge::Both);
        let io = io();
        step(&mut block, &io, true, 1);
        assert!(pulsed(&io));
        step(&mut block, &io, false, 2);
        assert!(pulsed(&io));

        // `edge` is required and must carry a declared code.
        assert!(matches!(
            EdgeTrigger::from_parameters("etr", IN, OUT, &Parameters::new()).unwrap_err(),
            ParameterError::Missing { ref parameter, .. } if parameter == "edge"
        ));
        for value in [
            Value::Int(3),
            Value::Int(-1),
            Value::Float(0.5),
            Value::Bool(true),
        ] {
            let bad: Parameters = [("edge".to_string(), value)].into_iter().collect();
            assert!(
                matches!(
                    EdgeTrigger::from_parameters("etr", IN, OUT, &bad).unwrap_err(),
                    ParameterError::Invalid { ref parameter, .. } if parameter == "edge"
                ),
                "value={value:?}"
            );
        }
    }

    #[test]
    fn describes_itself() {
        let descriptor = component().describe();
        assert_eq!(descriptor.name, "etr");
        assert_eq!(descriptor.kind, EdgeTrigger::KIND);
        assert_eq!(descriptor.label, "etr");
        assert_eq!(
            descriptor.ports,
            [
                PortDescriptor {
                    name: "in".to_string(),
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
                name: "edge".to_string(),
                kind: ValueKind::Int,
                range: Some(EDGE_RANGE),
            }]
        );
    }
}
