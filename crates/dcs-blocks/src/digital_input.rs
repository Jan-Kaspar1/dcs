//! Digital input: boolean conditioning with optional inversion and
//! tick-based debounce.

use crate::params::{self, ParameterError, Parameters};
use dcs_core::{PointId, Sample, StateError, StateMap, Tick, Value};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// A digital input channel: conditions a boolean field `In` point onto a
/// boolean `Out` point, optionally inverting and debouncing it.
///
/// Each step reads `in`, applies `invert`, and drives the conditioned value
/// on `out` subject to the debounce rule: a *change* is adopted only after
/// the conditioned value has held unchanged for `debounce_ticks`
/// consecutive scans. `0` or `1` passes every change through on the scan it
/// appears; `2` swallows single-scan pulses, `3` two-scan pulses, and so
/// on. The first observed value is adopted immediately — the debounce
/// filters changes, not initialization.
///
/// The output sample carries the input's quality, so a degraded field
/// read produces a degraded conditioned value; while the debounce holds a
/// change back, the held output still carries the current input quality.
///
/// Declared I/O: `in` (`In`, `Bool`) and `out` (`Out`, `Bool`).
///
/// Parameters: `invert` — optional `Bool` (or `0`/`1`), default `false`;
/// `debounce_ticks` — optional non-negative `Int` (or integral `Float`),
/// default `0`.
#[derive(Debug)]
pub struct DigitalInput {
    name: String,
    input: PointId,
    output: PointId,
    invert: bool,
    debounce_ticks: u64,
    /// The conditioned value last observed and how many consecutive scans
    /// it has held; `None` before the first step.
    stable: Option<(bool, u64)>,
    /// The value currently driven on `out`.
    driven: bool,
}

impl DigitalInput {
    /// The component-kind string the model-driven registry maps onto this
    /// type's constructor.
    pub const KIND: &'static str = "digital-input";

    /// A non-inverting, non-debounced digital input.
    pub fn new(name: impl Into<String>, input: PointId, output: PointId) -> Self {
        Self {
            name: name.into(),
            input,
            output,
            invert: false,
            debounce_ticks: 0,
            stable: None,
            driven: false,
        }
    }

    /// Sets whether the conditioned value is the field signal's negation.
    pub fn with_invert(mut self, invert: bool) -> Self {
        self.invert = invert;
        self
    }

    /// Sets the debounce: how many consecutive scans a changed conditioned
    /// value must hold before `out` adopts it.
    pub fn with_debounce_ticks(mut self, debounce_ticks: u64) -> Self {
        self.debounce_ticks = debounce_ticks;
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
        let debounce_ticks =
            params::optional_u64(&name, parameters, "debounce_ticks")?.unwrap_or(0);
        Ok(Self::new(name, input, output)
            .with_invert(invert)
            .with_debounce_ticks(debounce_ticks))
    }
}

impl Component for DigitalInput {
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
        let conditioned = sample.value ^ self.invert;
        let held = match self.stable {
            Some((value, count)) if value == conditioned => count.saturating_add(1),
            Some(_) => 1,
            // The first observation initializes the output; it is not a
            // change the debounce should delay.
            None => {
                self.driven = conditioned;
                1
            }
        };
        self.stable = Some((conditioned, held));
        if held >= self.debounce_ticks.max(1) {
            self.driven = conditioned;
        }
        io.write_sample(
            self.output,
            Sample::new(Value::Bool(self.driven), sample.quality, tick),
        )?;
        Ok(())
    }

    /// Captures the driven output value and the debounce's in-progress
    /// observation (`stable_value`/`stable_count`, absent before the
    /// first step).
    fn capture_state(&self) -> StateMap {
        let mut state = StateMap::new();
        state.insert("driven", Value::Bool(self.driven));
        if let Some((value, held)) = self.stable {
            state.insert("stable_value", Value::Bool(value));
            state.insert("stable_count", Value::Int(held as i64));
        }
        state
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        state.ensure_known_fields(&self.name, &["driven", "stable_value", "stable_count"])?;
        let driven = state.require_bool(&self.name, "driven")?;
        let stable = match (
            state.optional_bool(&self.name, "stable_value")?,
            state.optional_i64(&self.name, "stable_count")?,
        ) {
            (Some(value), Some(count)) if count >= 0 => Some((value, count as u64)),
            (Some(_), Some(count)) => {
                return Err(StateError::InvalidValue {
                    element: self.name.clone(),
                    field: "stable_count".to_string(),
                    value: Value::Int(count),
                });
            }
            (None, None) => None,
            (Some(_), None) => {
                return Err(StateError::MissingField {
                    element: self.name.clone(),
                    field: "stable_count".to_string(),
                });
            }
            (None, Some(_)) => {
                return Err(StateError::MissingField {
                    element: self.name.clone(),
                    field: "stable_value".to_string(),
                });
            }
        };
        self.driven = driven;
        self.stable = stable;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::TestIo;
    use dcs_core::{Direction, Quality, QualityReason};
    use dcs_model::{ComponentId, ComponentInstance};
    use std::collections::BTreeMap;

    const IN: PointId = PointId(40);
    const OUT: PointId = PointId(41);

    fn io(field: Sample) -> TestIo {
        TestIo::new(&[
            (IN, Direction::In, field),
            (
                OUT,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
        ])
    }

    fn feed(io: &TestIo, value: bool, tick: u64) {
        io.feed(IN, Sample::good(Value::Bool(value), Tick(tick)));
    }

    #[test]
    fn passes_field_value_and_quality_through() {
        let mut block = DigitalInput::new("di", IN, OUT);
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
                Quality::Bad(QualityReason::CommunicationFault),
                Tick(2),
            ),
        );
        block.step(&io, Tick(2)).unwrap();
        let out = io.written(OUT).unwrap();
        assert_eq!(out.value, Value::Bool(false));
        assert_eq!(out.quality, Quality::Bad(QualityReason::CommunicationFault));
    }

    #[test]
    fn invert_negates_the_field_value() {
        let mut block = DigitalInput::new("di", IN, OUT).with_invert(true);
        let io = io(Sample::good(Value::Bool(false), Tick::ZERO));
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(io.written(OUT).unwrap().value, Value::Bool(true));
    }

    #[test]
    fn debounce_adopts_only_sustained_changes() {
        // The conditioned value must hold for 3 consecutive scans before
        // `out` follows.
        let mut block = DigitalInput::new("di", IN, OUT).with_debounce_ticks(3);
        let io = io(Sample::good(Value::Bool(false), Tick::ZERO));

        // First scan initializes the output without a debounce delay.
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(io.written(OUT).unwrap().value, Value::Bool(false));

        // Ticks 2-3: the change to true is pending but not yet adopted.
        for tick in 2..=3 {
            feed(&io, true, tick);
            block.step(&io, Tick(tick)).unwrap();
            assert_eq!(
                io.written(OUT).unwrap().value,
                Value::Bool(false),
                "tick={tick}"
            );
        }
        // Tick 4: the third consecutive true scan adopts the change.
        feed(&io, true, 4);
        block.step(&io, Tick(4)).unwrap();
        assert_eq!(io.written(OUT).unwrap().value, Value::Bool(true));

        // A two-scan low pulse is swallowed: the output stays true.
        for tick in 5..=6 {
            feed(&io, false, tick);
            block.step(&io, Tick(tick)).unwrap();
            assert_eq!(
                io.written(OUT).unwrap().value,
                Value::Bool(true),
                "tick={tick}"
            );
        }
        // The pending change resets when the input returns: a later
        // change must again sustain for the full count.
        feed(&io, true, 7);
        block.step(&io, Tick(7)).unwrap();
        for tick in 8..=9 {
            feed(&io, false, tick);
            block.step(&io, Tick(tick)).unwrap();
            assert_eq!(
                io.written(OUT).unwrap().value,
                Value::Bool(true),
                "tick={tick}"
            );
        }
        feed(&io, false, 10);
        block.step(&io, Tick(10)).unwrap();
        assert_eq!(io.written(OUT).unwrap().value, Value::Bool(false));
    }

    #[test]
    fn held_output_carries_current_input_quality() {
        let mut block = DigitalInput::new("di", IN, OUT).with_debounce_ticks(3);
        let io = io(Sample::good(Value::Bool(false), Tick::ZERO));
        block.step(&io, Tick(1)).unwrap();

        // A pending change with degraded quality keeps the old value but
        // marks the output with the input's quality.
        io.feed(
            IN,
            Sample::new(
                Value::Bool(true),
                Quality::Uncertain(QualityReason::Stale),
                Tick(2),
            ),
        );
        block.step(&io, Tick(2)).unwrap();
        let out = io.written(OUT).unwrap();
        assert_eq!(out.value, Value::Bool(false));
        assert_eq!(out.quality, Quality::Uncertain(QualityReason::Stale));
    }

    #[test]
    fn builds_from_parameter_map() {
        let parameters: Parameters = [
            ("invert".to_string(), Value::Bool(true)),
            ("debounce_ticks".to_string(), Value::Int(2)),
        ]
        .into_iter()
        .collect();
        let instance = ComponentInstance {
            id: ComponentId(4),
            kind: DigitalInput::KIND.to_string(),
            parameters,
            ports: BTreeMap::new(),
        };
        let mut block = DigitalInput::from_parameters("di", IN, OUT, &instance.parameters).unwrap();
        assert!(block.invert);
        assert_eq!(block.debounce_ticks, 2);

        // inverted input true -> conditioned false, held for tick 1;
        // adopted on the second consecutive scan.
        let io = io(Sample::good(Value::Bool(true), Tick::ZERO));
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(io.written(OUT).unwrap().value, Value::Bool(false));
        feed(&io, false, 2);
        block.step(&io, Tick(2)).unwrap();
        assert_eq!(io.written(OUT).unwrap().value, Value::Bool(false));
        block.step(&io, Tick(3)).unwrap();
        assert_eq!(io.written(OUT).unwrap().value, Value::Bool(true));

        // Both parameters are optional; an empty map builds the plain
        // pass-through block.
        let block = DigitalInput::from_parameters("di", IN, OUT, &Parameters::new()).unwrap();
        assert!(!block.invert);
        assert_eq!(block.debounce_ticks, 0);

        let wrong: Parameters = [("debounce_ticks".to_string(), Value::Int(-1))]
            .into_iter()
            .collect();
        assert!(matches!(
            DigitalInput::from_parameters("di", IN, OUT, &wrong).unwrap_err(),
            ParameterError::Invalid { ref parameter, .. } if parameter == "debounce_ticks"
        ));
    }
}
