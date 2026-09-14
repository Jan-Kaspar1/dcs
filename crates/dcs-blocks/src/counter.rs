//! Up-counter: rising-edge counting with a preset-reached flag and a
//! reset input.

use crate::describe;
use crate::params::{self, ParameterError, Parameters};
use dcs_core::{
    CommandError, ComponentDescriptor, PointId, PortRole, Sample, StateError, StateMap, Tick,
    Value, ValueKind,
};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// An up-counter: counts rising edges on `in`, reports the total on
/// `count`, and asserts `done` once the count reaches `preset`.
///
/// Each scan the counter observes `in`; a scan reading `true` whose
/// predecessor read `false` — or the first scan of a run reading `true`
/// — is one rising edge and adds one to `count`, saturating at
/// `i64::MAX`. A held level counts once: `in` must fall and rise again
/// for the next count.
///
/// **Reset rule:** while `reset` reads `true` the count is forced to
/// `0` and no edges are counted — reset dominates a simultaneous rising
/// edge. The input is still tracked across a reset, so a level held
/// high through it produces no edge when `reset` releases; counting
/// resumes on the next genuine rising edge.
///
/// `done` asserts whenever `count >= preset` — a `preset` of `0` asserts
/// unconditionally — and clears only through `reset`.
///
/// Both outputs carry the worst of the `in` and `reset` qualities: a
/// degraded count or reset input marks the outputs, while counting
/// itself stays a deterministic function of the values read.
///
/// Declared I/O: `in` (`In`, `Bool`), `reset` (`In`, `Bool`), `count`
/// (`Out`, `Int`), `done` (`Out`, `Bool`).
///
/// Parameters: `preset` — required non-negative `Int` (or integral
/// `Float`) no larger than `i64::MAX`.
#[derive(Debug)]
pub struct Counter {
    name: String,
    input: PointId,
    reset: PointId,
    count_out: PointId,
    done: PointId,
    preset: u64,
    /// Edges counted so far.
    count: u64,
    /// The `in` value seen on the previous scan; `false` before the
    /// first, so a `true` first read counts as an edge.
    previous: bool,
}

impl Counter {
    /// The component-kind string the model-driven registry maps onto this
    /// type's constructor.
    pub const KIND: &'static str = "counter";

    /// Builds the component from explicit points and the count `done`
    /// asserts at.
    ///
    /// `preset` may not exceed `i64::MAX` — the count is an `Int` — and
    /// violations are reported as a [`ParameterError`].
    pub fn new(
        name: impl Into<String>,
        input: PointId,
        reset: PointId,
        count_out: PointId,
        done: PointId,
        preset: u64,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        if preset > i64::MAX as u64 {
            return Err(params::invalid(
                &name,
                "preset",
                format!("must not exceed i64::MAX, found {preset}"),
            ));
        }
        Ok(Self {
            name,
            input,
            reset,
            count_out,
            done,
            preset,
            count: 0,
            previous: false,
        })
    }

    /// Builds the component from a plant-model parameter map, reading the
    /// parameters listed on the type's docs.
    pub fn from_parameters(
        name: impl Into<String>,
        input: PointId,
        reset: PointId,
        count_out: PointId,
        done: PointId,
        parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        let preset = params::required_u64(&name, parameters, "preset")?;
        Self::new(name, input, reset, count_out, done, preset)
    }
}

impl Component for Counter {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        vec![
            IoRequirement::input::<bool>("in", self.input),
            IoRequirement::input::<bool>("reset", self.reset),
            IoRequirement::output::<i64>("count", self.count_out),
            IoRequirement::output::<bool>("done", self.done),
        ]
    }

    fn step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError> {
        let input = io.read_typed::<bool>(self.input)?;
        let reset = io.read_typed::<bool>(self.reset)?;
        if reset.value {
            self.count = 0;
        } else if input.value && !self.previous {
            self.count = (self.count + 1).min(i64::MAX as u64);
        }
        self.previous = input.value;
        let quality = input.quality.merge(reset.quality);
        io.write_sample(
            self.count_out,
            Sample::new(Value::Int(self.count as i64), quality, tick),
        )?;
        io.write_sample(
            self.done,
            Sample::new(Value::Bool(self.count >= self.preset), quality, tick),
        )?;
        Ok(())
    }

    /// Describes the counter: `in` is the counted signal, `reset` the
    /// clearing condition, `count` the accumulated total, `done` the
    /// preset-reached flag; the `preset` parameter.
    fn describe(&self) -> ComponentDescriptor {
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &[
                ("in", PortRole::ProcessValue),
                ("reset", PortRole::Status),
                ("count", PortRole::Output),
                ("done", PortRole::Status),
            ],
            vec![describe::parameter(
                "preset",
                ValueKind::Int,
                Some(describe::NONNEGATIVE_INT),
            )],
        )
    }

    /// Tunes the `preset` at the scan boundary; retuning below the
    /// banked `count` asserts `done` on the next scan.
    fn apply_parameter(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        match parameter {
            "preset" => self.preset = params::tune_u64(&self.name, parameter, value)?,
            _ => return Err(params::unknown_parameter(&self.name, parameter)),
        }
        Ok(())
    }

    /// Captures the accumulated count, the previous `in` reading — the
    /// state a standby needs to continue mid-count without recounting a
    /// held-high input's edge — and the tuned `preset`.
    fn capture_state(&self) -> StateMap {
        let mut state = StateMap::new();
        state.insert("count", Value::Int(self.count as i64));
        state.insert("previous", Value::Bool(self.previous));
        state.insert("preset", Value::Int(self.preset as i64));
        state
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        state.ensure_known_fields(&self.name, &["count", "previous", "preset"])?;
        let count = state.require_i64(&self.name, "count")?;
        if count < 0 {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "count".to_string(),
                value: Value::Int(count),
            });
        }
        let preset = state.require_i64(&self.name, "preset")?;
        if preset < 0 {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "preset".to_string(),
                value: Value::Int(preset),
            });
        }
        self.count = count as u64;
        self.previous = state.require_bool(&self.name, "previous")?;
        self.preset = preset as u64;
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

    const IN: PointId = PointId(120);
    const RESET: PointId = PointId(121);
    const COUNT: PointId = PointId(122);
    const DONE: PointId = PointId(123);

    /// A counter asserting `done` at the third edge.
    fn component() -> Counter {
        Counter::new("ctr", IN, RESET, COUNT, DONE, 3).unwrap()
    }

    fn io() -> TestIo {
        TestIo::new(&[
            (
                IN,
                Direction::In,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
            (
                RESET,
                Direction::In,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
            (
                COUNT,
                Direction::Out,
                Sample::good(Value::Int(0), Tick::ZERO),
            ),
            (
                DONE,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
        ])
    }

    /// Feeds `in`/`reset` and steps once.
    fn step(block: &mut Counter, io: &TestIo, input: bool, reset: bool, tick: u64) {
        io.feed(IN, Sample::good(Value::Bool(input), Tick(tick)));
        io.feed(RESET, Sample::good(Value::Bool(reset), Tick(tick)));
        block.step(io, Tick(tick)).unwrap();
    }

    fn count(io: &TestIo) -> i64 {
        match io.written(COUNT).unwrap().value {
            Value::Int(count) => count,
            other => panic!("count must be Int, found {other:?}"),
        }
    }

    fn done(io: &TestIo) -> bool {
        io.written(DONE).unwrap().value == Value::Bool(true)
    }

    #[test]
    fn counts_rising_edges_and_asserts_at_the_preset() {
        let mut block = component();
        let io = io();

        // First scan reading true is an edge.
        step(&mut block, &io, true, false, 1);
        assert_eq!(count(&io), 1);
        assert!(!done(&io));

        // A held level counts once.
        step(&mut block, &io, true, false, 2);
        assert_eq!(count(&io), 1);

        // Each further rising edge adds one; the third asserts `done`.
        step(&mut block, &io, false, false, 3);
        step(&mut block, &io, true, false, 4);
        assert_eq!(count(&io), 2);
        assert!(!done(&io));
        step(&mut block, &io, false, false, 5);
        step(&mut block, &io, true, false, 6);
        assert_eq!(count(&io), 3);
        assert!(done(&io));

        // Counting continues past the preset; `done` stays asserted.
        step(&mut block, &io, false, false, 7);
        step(&mut block, &io, true, false, 8);
        assert_eq!(count(&io), 4);
        assert!(done(&io));
    }

    #[test]
    fn reset_clears_the_count_and_dominates_edges() {
        let mut block = component();
        let io = io();

        step(&mut block, &io, true, false, 1);
        step(&mut block, &io, false, false, 2);
        step(&mut block, &io, true, false, 3);
        assert_eq!(count(&io), 2);

        // Reset on a rising edge: the edge is not counted.
        step(&mut block, &io, false, false, 4);
        step(&mut block, &io, true, true, 5);
        assert_eq!(count(&io), 0);
        assert!(!done(&io));

        // The level held high through the reset is not an edge on
        // release; the next genuine rising edge counts.
        step(&mut block, &io, true, false, 6);
        assert_eq!(count(&io), 0);
        step(&mut block, &io, false, false, 7);
        step(&mut block, &io, true, false, 8);
        assert_eq!(count(&io), 1);
    }

    #[test]
    fn zero_preset_asserts_done_unconditionally() {
        let mut block = Counter::new("ctr", IN, RESET, COUNT, DONE, 0).unwrap();
        let io = io();
        step(&mut block, &io, false, false, 1);
        assert_eq!(count(&io), 0);
        assert!(done(&io));
    }

    #[test]
    fn outputs_carry_worst_of_input_qualities() {
        let mut block = component();
        let io = io();
        io.feed(
            IN,
            Sample::new(
                Value::Bool(true),
                Quality::Uncertain(QualityReason::Stale),
                Tick(1),
            ),
        );
        io.feed(
            RESET,
            Sample::new(
                Value::Bool(false),
                Quality::Bad(QualityReason::CommunicationFault),
                Tick(1),
            ),
        );
        block.step(&io, Tick(1)).unwrap();
        // Reset's value was false, so the edge counted — but both
        // outputs carry the merged Bad quality.
        assert_eq!(count(&io), 1);
        assert_eq!(
            io.written(COUNT).unwrap().quality,
            Quality::Bad(QualityReason::CommunicationFault)
        );
        assert_eq!(
            io.written(DONE).unwrap().quality,
            Quality::Bad(QualityReason::CommunicationFault)
        );
    }

    #[test]
    fn restored_counter_continues_mid_count() {
        let mut block = component();
        let io = io();

        // Two edges banked, `in` currently high — checkpoint.
        step(&mut block, &io, true, false, 1);
        step(&mut block, &io, false, false, 2);
        step(&mut block, &io, true, false, 3);
        let state = block.capture_state();
        assert_eq!(state.get("count"), Some(Value::Int(2)));
        assert_eq!(state.get("previous"), Some(Value::Bool(true)));

        // The standby continues identically: the held-high input is no
        // new edge, and the next rising edge completes the preset.
        let mut standby = component();
        standby.restore_state(&state).unwrap();
        assert_eq!(standby.capture_state(), state);
        step(&mut standby, &io, true, false, 4);
        assert_eq!(count(&io), 2);
        step(&mut standby, &io, false, false, 5);
        step(&mut standby, &io, true, false, 6);
        assert_eq!(count(&io), 3);
        assert!(done(&io));
    }

    #[test]
    fn incompatible_state_is_a_named_error() {
        let mut block = component();
        let mut state = StateMap::new();
        state.insert("count", Value::Int(-1));
        state.insert("previous", Value::Bool(false));
        state.insert("preset", Value::Int(3));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::InvalidValue { ref field, .. }) if field == "count"
        ));
        let mut state = StateMap::new();
        state.insert("count", Value::Int(1));
        state.insert("preset", Value::Int(3));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::MissingField { ref field, .. }) if field == "previous"
        ));
    }

    #[test]
    fn builds_from_parameter_map() {
        let parameters: Parameters = [("preset".to_string(), Value::Int(3))]
            .into_iter()
            .collect();
        let instance = ComponentInstance {
            id: ComponentId(10),
            kind: Counter::KIND.to_string(),
            parameters,
            ports: BTreeMap::new(),
        };
        let mut block =
            Counter::from_parameters("ctr", IN, RESET, COUNT, DONE, &instance.parameters).unwrap();
        assert_eq!(block.preset, 3);
        let io = io();
        step(&mut block, &io, true, false, 1);
        assert_eq!(count(&io), 1);

        assert!(matches!(
            Counter::from_parameters("ctr", IN, RESET, COUNT, DONE, &Parameters::new())
                .unwrap_err(),
            ParameterError::Missing { ref parameter, .. } if parameter == "preset"
        ));
        let negative: Parameters = [("preset".to_string(), Value::Int(-2))]
            .into_iter()
            .collect();
        assert!(matches!(
            Counter::from_parameters("ctr", IN, RESET, COUNT, DONE, &negative).unwrap_err(),
            ParameterError::Invalid { ref parameter, .. } if parameter == "preset"
        ));
        assert!(matches!(
            Counter::new("ctr", IN, RESET, COUNT, DONE, i64::MAX as u64 + 1),
            Err(ParameterError::Invalid { ref parameter, .. }) if parameter == "preset"
        ));
    }

    #[test]
    fn describes_itself() {
        let descriptor = component().describe();
        assert_eq!(descriptor.name, "ctr");
        assert_eq!(descriptor.kind, Counter::KIND);
        assert_eq!(descriptor.label, "ctr");
        assert_eq!(
            descriptor.ports,
            [
                PortDescriptor {
                    name: "in".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::ProcessValue),
                },
                PortDescriptor {
                    name: "reset".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Status),
                },
                PortDescriptor {
                    name: "count".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Int,
                    role: Some(PortRole::Output),
                },
                PortDescriptor {
                    name: "done".to_string(),
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
                name: "preset".to_string(),
                kind: ValueKind::Int,
                range: Some(describe::NONNEGATIVE_INT),
            }]
        );
    }
}
