//! Timer: on-delay and off-delay timing of a Boolean signal in the tick
//! domain.

use crate::describe;
use crate::params::{self, ParameterError, Parameters};
use dcs_core::{
    CommandError, ComponentDescriptor, PointId, PortRole, Sample, StateError, StateMap, Tick,
    Value, ValueKind,
};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// A delay timer: drives `out` from the Boolean `in` after a configured
/// number of ticks, in either on-delay or off-delay mode.
///
/// All timing is in scans — the executor's virtual tick — never wall
/// clock, per the execution-model decision.
///
/// **On-delay** (`off_delay` unset or `false`): while `in` reads `true`
/// the timer counts consecutive scans; `out` asserts once the count
/// reaches `delay_ticks` — so `delay_ticks` of `0` or `1` passes `in`
/// straight through. The count *resets* on the scan `in` reads `false`:
/// an input that falls before the delay elapses is never asserted, and
/// `out` follows `in` back down on that same scan.
///
/// **Off-delay** (`off_delay` `true`): `out` asserts on the first scan
/// `in` reads `true` and holds while it stays `true`. Once `in` reads
/// `false` the timer counts consecutive scans; `out` deasserts when the
/// count reaches `delay_ticks`. A `true` scan before the delay elapses
/// resets the release count, keeping `out` asserted.
///
/// The timer evaluates the input's value every scan whatever its
/// quality; `out` carries the input's quality, so a degraded read
/// produces a degraded output without stalling the count.
///
/// Declared I/O: `in` (`In`, `Bool`) and `out` (`Out`, `Bool`).
///
/// Parameters: `delay_ticks` — required non-negative `Int` (or integral
/// `Float`) no larger than `i64::MAX`; `off_delay` — optional `Bool`
/// (or `0`/`1`), default `false`.
#[derive(Debug)]
pub struct Timer {
    name: String,
    input: PointId,
    output: PointId,
    delay_ticks: u64,
    off_delay: bool,
    /// Consecutive scans `in` has held the timed state — `true` for
    /// on-delay, `false` for off-delay. Clamped at `delay_ticks`.
    elapsed: u64,
}

impl Timer {
    /// The component-kind string the model-driven registry maps onto this
    /// type's constructor.
    pub const KIND: &'static str = "timer";

    /// An on-delay timer asserting `out` after `in` has held `true` for
    /// `delay_ticks` consecutive scans.
    ///
    /// `delay_ticks` may not exceed `i64::MAX` — the captured state is an
    /// `Int` — and violations are reported as a [`ParameterError`].
    pub fn new(
        name: impl Into<String>,
        input: PointId,
        output: PointId,
        delay_ticks: u64,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        if delay_ticks > i64::MAX as u64 {
            return Err(params::invalid(
                &name,
                "delay_ticks",
                format!("must not exceed i64::MAX, found {delay_ticks}"),
            ));
        }
        Ok(Self {
            name,
            input,
            output,
            delay_ticks,
            off_delay: false,
            elapsed: 0,
        })
    }

    /// Selects off-delay mode: `out` follows `in` up immediately and
    /// deasserts only after `in` has held `false` for `delay_ticks`.
    pub fn with_off_delay(mut self, off_delay: bool) -> Self {
        self.off_delay = off_delay;
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
        let delay_ticks = params::required_u64(&name, parameters, "delay_ticks")?;
        let off_delay = params::optional_bool(&name, parameters, "off_delay")?.unwrap_or(false);
        Ok(Self::new(name, input, output, delay_ticks)?.with_off_delay(off_delay))
    }
}

impl Component for Timer {
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
        // `elapsed` counts consecutive scans in the timed state — high
        // for on-delay, low for off-delay — clamped at the configured
        // delay so captured state stays bounded.
        let out = if self.off_delay {
            if sample.value {
                self.elapsed = 0;
            } else {
                self.elapsed = (self.elapsed + 1).min(self.delay_ticks);
            }
            sample.value || self.elapsed < self.delay_ticks
        } else {
            if sample.value {
                self.elapsed = (self.elapsed + 1).min(self.delay_ticks);
            } else {
                self.elapsed = 0;
            }
            sample.value && self.elapsed >= self.delay_ticks
        };
        io.write_sample(
            self.output,
            Sample::new(Value::Bool(out), sample.quality, tick),
        )?;
        Ok(())
    }

    /// Describes the timer: `in` is the signal being timed, `out` the
    /// delayed result; the `delay_ticks` and `off_delay` parameters
    /// `from_parameters` reads.
    fn describe(&self) -> ComponentDescriptor {
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &[("in", PortRole::ProcessValue), ("out", PortRole::Output)],
            vec![
                describe::parameter(
                    "delay_ticks",
                    ValueKind::Int,
                    Some(describe::NONNEGATIVE_INT),
                ),
                describe::parameter("off_delay", ValueKind::Bool, None),
            ],
        )
    }

    /// Tunes a declared parameter at the scan boundary.
    ///
    /// Shrinking `delay_ticks` below the banked `elapsed` clamps the
    /// count at the new bound — an on-delay timer already satisfied
    /// asserts on the next scan. Switching `off_delay` mid-run resets the
    /// count: banked scans of one mode mean nothing in the other, so the
    /// switch starts a clean count rather than reinterpreting them.
    fn apply_parameter(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        match parameter {
            "delay_ticks" => {
                self.delay_ticks = params::tune_u64(&self.name, parameter, value)?;
                self.elapsed = self.elapsed.min(self.delay_ticks);
            }
            "off_delay" => {
                self.off_delay = params::tune_bool(&self.name, parameter, value)?;
                self.elapsed = 0;
            }
            _ => return Err(params::unknown_parameter(&self.name, parameter)),
        }
        Ok(())
    }

    /// Captures the in-progress count and the tuned delay so a
    /// checkpointed timer continues mid-count under the same tuning: a
    /// restored timer with `elapsed` scans banked needs exactly
    /// `delay_ticks - elapsed` further scans of the timed state.
    fn capture_state(&self) -> StateMap {
        let mut state = StateMap::new();
        state.insert("elapsed", Value::Int(self.elapsed as i64));
        state.insert("delay_ticks", Value::Int(self.delay_ticks as i64));
        state.insert("off_delay", Value::Bool(self.off_delay));
        state
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        state.ensure_known_fields(&self.name, &["elapsed", "delay_ticks", "off_delay"])?;
        let elapsed = state.require_i64(&self.name, "elapsed")?;
        let delay_ticks = state.require_i64(&self.name, "delay_ticks")?;
        if delay_ticks < 0 {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "delay_ticks".to_string(),
                value: Value::Int(delay_ticks),
            });
        }
        if elapsed < 0 || elapsed > delay_ticks {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "elapsed".to_string(),
                value: Value::Int(elapsed),
            });
        }
        self.elapsed = elapsed as u64;
        self.delay_ticks = delay_ticks as u64;
        self.off_delay = state.require_bool(&self.name, "off_delay")?;
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

    const IN: PointId = PointId(110);
    const OUT: PointId = PointId(111);

    /// An on-delay timer asserting on the third consecutive `true` scan.
    fn component() -> Timer {
        Timer::new("tmr", IN, OUT, 3).unwrap()
    }

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

    fn driven(io: &TestIo) -> bool {
        io.written(OUT).unwrap().value == Value::Bool(true)
    }

    #[test]
    fn on_delay_asserts_exactly_at_the_configured_tick() {
        let mut block = component();
        let io = io(Sample::good(Value::Bool(false), Tick::ZERO));

        // Ticks 1-2: held true but below the delay — not yet asserted.
        for tick in 1..=2 {
            feed(&io, true, tick);
            block.step(&io, Tick(tick)).unwrap();
            assert!(!driven(&io), "tick={tick}");
        }
        // Tick 3: the third consecutive true scan asserts.
        feed(&io, true, 3);
        block.step(&io, Tick(3)).unwrap();
        assert!(driven(&io));
        // Held beyond the delay: stays asserted.
        feed(&io, true, 4);
        block.step(&io, Tick(4)).unwrap();
        assert!(driven(&io));
    }

    #[test]
    fn on_delay_resets_on_the_first_false_scan() {
        let mut block = component();
        let io = io(Sample::good(Value::Bool(false), Tick::ZERO));

        // Assert, then drop the input: `out` follows it down on the
        // same scan and the count resets.
        for tick in 1..=3 {
            feed(&io, true, tick);
            block.step(&io, Tick(tick)).unwrap();
        }
        assert!(driven(&io));
        feed(&io, false, 4);
        block.step(&io, Tick(4)).unwrap();
        assert!(!driven(&io));

        // An interrupted run-up counts nothing: two true scans, a false
        // scan, then two more trues — still below the delay.
        for (tick, value) in [(5, true), (6, true), (7, false), (8, true), (9, true)] {
            feed(&io, value, tick);
            block.step(&io, Tick(tick)).unwrap();
            assert!(!driven(&io), "tick={tick}");
        }
        feed(&io, true, 10);
        block.step(&io, Tick(10)).unwrap();
        assert!(driven(&io));
    }

    #[test]
    fn off_delay_holds_then_deasserts_at_the_configured_tick() {
        let mut block = Timer::new("tmr", IN, OUT, 3).unwrap().with_off_delay(true);
        let io = io(Sample::good(Value::Bool(false), Tick::ZERO));

        // The first true scan asserts immediately.
        feed(&io, true, 1);
        block.step(&io, Tick(1)).unwrap();
        assert!(driven(&io));

        // Held through the first two false scans; deasserts on the third.
        for tick in 2..=3 {
            feed(&io, false, tick);
            block.step(&io, Tick(tick)).unwrap();
            assert!(driven(&io), "tick={tick}");
        }
        feed(&io, false, 4);
        block.step(&io, Tick(4)).unwrap();
        assert!(!driven(&io));
    }

    #[test]
    fn off_delay_reassertion_resets_the_release_count() {
        let mut block = Timer::new("tmr", IN, OUT, 3).unwrap().with_off_delay(true);
        let io = io(Sample::good(Value::Bool(false), Tick::ZERO));

        feed(&io, true, 1);
        block.step(&io, Tick(1)).unwrap();
        // Two false scans, then a true: the release count resets and
        // `out` never dropped.
        for (tick, value) in [(2, false), (3, false), (4, true)] {
            feed(&io, value, tick);
            block.step(&io, Tick(tick)).unwrap();
            assert!(driven(&io), "tick={tick}");
        }
        // A fresh release needs the full delay again.
        for tick in 5..=6 {
            feed(&io, false, tick);
            block.step(&io, Tick(tick)).unwrap();
            assert!(driven(&io), "tick={tick}");
        }
        feed(&io, false, 7);
        block.step(&io, Tick(7)).unwrap();
        assert!(!driven(&io));
    }

    #[test]
    fn zero_delay_passes_the_input_through() {
        for off_delay in [false, true] {
            let mut block = Timer::new("tmr", IN, OUT, 0)
                .unwrap()
                .with_off_delay(off_delay);
            let io = io(Sample::good(Value::Bool(false), Tick::ZERO));
            feed(&io, true, 1);
            block.step(&io, Tick(1)).unwrap();
            assert!(driven(&io), "off_delay={off_delay}");
            feed(&io, false, 2);
            block.step(&io, Tick(2)).unwrap();
            assert!(!driven(&io), "off_delay={off_delay}");
        }
    }

    #[test]
    fn output_carries_input_quality() {
        let mut block = component();
        let io = io(Sample::good(Value::Bool(false), Tick::ZERO));
        io.feed(
            IN,
            Sample::new(
                Value::Bool(true),
                Quality::Bad(QualityReason::CommunicationFault),
                Tick(1),
            ),
        );
        block.step(&io, Tick(1)).unwrap();
        let out = io.written(OUT).unwrap();
        assert_eq!(out.value, Value::Bool(false));
        assert_eq!(out.quality, Quality::Bad(QualityReason::CommunicationFault));
    }

    #[test]
    fn restored_timer_continues_mid_count() {
        let mut block = component();
        let on_io = io(Sample::good(Value::Bool(false), Tick::ZERO));

        // Two of the three required scans, then checkpoint.
        for tick in 1..=2 {
            feed(&on_io, true, tick);
            block.step(&on_io, Tick(tick)).unwrap();
        }
        let state = block.capture_state();
        assert_eq!(state.get("elapsed"), Some(Value::Int(2)));

        // A standby restores and finishes the count identically.
        let mut standby = component();
        standby.restore_state(&state).unwrap();
        assert_eq!(standby.capture_state(), state);
        feed(&on_io, true, 3);
        standby.step(&on_io, Tick(3)).unwrap();
        assert!(driven(&on_io));

        // A restore mid-release behaves the same for off-delay.
        let mut block = Timer::new("tmr", IN, OUT, 3).unwrap().with_off_delay(true);
        let off_io = io(Sample::good(Value::Bool(true), Tick::ZERO));
        block.step(&off_io, Tick(1)).unwrap();
        for tick in 2..=3 {
            feed(&off_io, false, tick);
            block.step(&off_io, Tick(tick)).unwrap();
        }
        let state = block.capture_state();
        assert_eq!(state.get("elapsed"), Some(Value::Int(2)));
        let mut standby = Timer::new("tmr", IN, OUT, 3).unwrap().with_off_delay(true);
        standby.restore_state(&state).unwrap();
        feed(&off_io, false, 4);
        standby.step(&off_io, Tick(4)).unwrap();
        assert!(!driven(&off_io));
    }

    #[test]
    fn incompatible_state_is_a_named_error() {
        let mut block = component();
        let mut state = StateMap::new();
        state.insert("elapsed", Value::Int(7)); // beyond delay_ticks
        state.insert("delay_ticks", Value::Int(3));
        state.insert("off_delay", Value::Bool(false));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::InvalidValue { ref field, .. }) if field == "elapsed"
        ));
        let mut state = StateMap::new();
        state.insert("elapsed", Value::Bool(true));
        state.insert("delay_ticks", Value::Int(3));
        state.insert("off_delay", Value::Bool(false));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::IncompatibleField { ref field, .. }) if field == "elapsed"
        ));
        assert!(matches!(
            block.restore_state(&StateMap::new()),
            Err(StateError::MissingField { ref field, .. }) if field == "elapsed"
        ));
    }

    #[test]
    fn builds_from_parameter_map() {
        let parameters: Parameters = [
            ("delay_ticks".to_string(), Value::Int(3)),
            ("off_delay".to_string(), Value::Bool(true)),
        ]
        .into_iter()
        .collect();
        let instance = ComponentInstance {
            id: ComponentId(9),
            kind: Timer::KIND.to_string(),
            parameters,
            ports: BTreeMap::new(),
        };
        let block = Timer::from_parameters("tmr", IN, OUT, &instance.parameters).unwrap();
        assert_eq!(block.delay_ticks, 3);
        assert!(block.off_delay);

        // `off_delay` is optional; `delay_ticks` is not.
        let required: Parameters = [("delay_ticks".to_string(), Value::Int(0))]
            .into_iter()
            .collect();
        let block = Timer::from_parameters("tmr", IN, OUT, &required).unwrap();
        assert!(!block.off_delay);
        assert!(matches!(
            Timer::from_parameters("tmr", IN, OUT, &Parameters::new()).unwrap_err(),
            ParameterError::Missing { ref parameter, .. } if parameter == "delay_ticks"
        ));

        let negative: Parameters = [("delay_ticks".to_string(), Value::Int(-1))]
            .into_iter()
            .collect();
        assert!(matches!(
            Timer::from_parameters("tmr", IN, OUT, &negative).unwrap_err(),
            ParameterError::Invalid { ref parameter, .. } if parameter == "delay_ticks"
        ));
        let wrong_kind: Parameters = [("off_delay".to_string(), Value::Float(1.5))]
            .into_iter()
            .chain(required.iter().map(|(k, v)| (k.clone(), *v)))
            .collect();
        assert!(matches!(
            Timer::from_parameters("tmr", IN, OUT, &wrong_kind).unwrap_err(),
            ParameterError::Invalid { ref parameter, .. } if parameter == "off_delay"
        ));
        assert!(matches!(
            Timer::new("tmr", IN, OUT, i64::MAX as u64 + 1),
            Err(ParameterError::Invalid { ref parameter, .. }) if parameter == "delay_ticks"
        ));
    }

    #[test]
    fn describes_itself() {
        let descriptor = component().describe();
        assert_eq!(descriptor.name, "tmr");
        assert_eq!(descriptor.kind, Timer::KIND);
        assert_eq!(descriptor.label, "tmr");
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
            [
                ParameterDescriptor {
                    name: "delay_ticks".to_string(),
                    kind: ValueKind::Int,
                    range: Some(describe::NONNEGATIVE_INT),
                },
                ParameterDescriptor {
                    name: "off_delay".to_string(),
                    kind: ValueKind::Bool,
                    range: None,
                },
            ]
        );
    }
}
