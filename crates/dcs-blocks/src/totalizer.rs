//! Totalizer: a rate input accumulated into a running total, with a
//! reset input and a configurable rollover.

use crate::describe;
use crate::params::{self, ParameterError, Parameters};
use dcs_core::{
    CommandError, ComponentDescriptor, PointId, PortRole, Quality, QualityReason, Sample,
    StateError, StateMap, Tick, Value, ValueKind,
};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// A totalizer: accumulates the analog `rate` input into the running
/// `total`, adding `rate * rate_unit` each scan.
///
/// `rate_unit` scales the per-tick increment — the tick-domain rate
/// unit: a `rate` expressed per hour scanned once per second integrates
/// with `rate_unit = 1/3600`. The default `1.0` treats `rate` as a
/// per-scan increment. Negative rates count the total down.
///
/// **Rollover rule:** a positive `rollover` bounds the total to
/// `[0, rollover)`: each scan's increment is taken modulo `rollover`
/// and the banked total wraps, so reaching the range continues from
/// `0` keeping the overshoot and a negative step wraps up from the top
/// — an odometer dial. `rollover` of `0` — or the parameter absent —
/// accumulates without bound, saturating the total at `±f64::MAX` when
/// a sum would overflow the type.
///
/// **Reset rule:** while `reset` reads `true` the total is forced to
/// `0` and no increment accrues — reset dominates a simultaneous valid
/// rate. Accumulation resumes on the scan after `reset` releases.
///
/// **Quality rule:** a non-`Good` or non-finite `rate` freezes the
/// accumulation — `total` holds its banked value — while `reset` still
/// acts on its own value. `total` is stamped with the worst of the
/// `rate` and `reset` qualities, merged with `Bad(DeviceFault)` when
/// `rate` is non-finite; a `rate * rate_unit` increment that overflows
/// `f64` cannot be banked either, and freezes the same way.
///
/// Declared I/O: `rate` (`In`, `Float`), `reset` (`In`, `Bool`),
/// `total` (`Out`, `Float`).
///
/// Parameters: `rate_unit` — optional finite `Float` (or losslessly
/// representable `Int`), strictly positive, default `1.0`; `rollover`
/// — optional finite `Float`, non-negative, default `0.0` (unbounded).
#[derive(Debug)]
pub struct Totalizer {
    name: String,
    rate: PointId,
    reset: PointId,
    total_out: PointId,
    rate_unit: f64,
    /// The wrap bound; `0.0` accumulates unbounded.
    rollover: f64,
    /// The banked total `total` reports.
    total: f64,
}

impl Totalizer {
    /// The component-kind string the model-driven registry maps onto
    /// this type's constructor.
    pub const KIND: &'static str = "totalizer";

    /// Builds the component from explicit points, the per-tick rate
    /// unit, and the rollover bound (`0.0` for unbounded accumulation).
    ///
    /// `rate_unit` must be finite and strictly positive; `rollover`
    /// must be finite and non-negative. Violations are reported as a
    /// [`ParameterError`].
    pub fn new(
        name: impl Into<String>,
        rate: PointId,
        reset: PointId,
        total_out: PointId,
        rate_unit: f64,
        rollover: f64,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        if !rate_unit.is_finite() {
            return Err(params::invalid(
                &name,
                "rate_unit",
                "must be finite".to_string(),
            ));
        }
        if rate_unit <= 0.0 {
            return Err(params::invalid(
                &name,
                "rate_unit",
                "must be positive".to_string(),
            ));
        }
        if !rollover.is_finite() {
            return Err(params::invalid(
                &name,
                "rollover",
                "must be finite".to_string(),
            ));
        }
        if rollover < 0.0 {
            return Err(params::invalid(
                &name,
                "rollover",
                "must be non-negative".to_string(),
            ));
        }
        Ok(Self {
            name,
            rate,
            reset,
            total_out,
            rate_unit,
            rollover,
            total: 0.0,
        })
    }

    /// Builds the component from a plant-model parameter map, reading
    /// the parameters listed on the type's docs.
    pub fn from_parameters(
        name: impl Into<String>,
        rate: PointId,
        reset: PointId,
        total_out: PointId,
        parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        let rate_unit = params::optional_f64(&name, parameters, "rate_unit")?.unwrap_or(1.0);
        let rollover = params::optional_f64(&name, parameters, "rollover")?.unwrap_or(0.0);
        Self::new(name, rate, reset, total_out, rate_unit, rollover)
    }

    /// Banks `delta` onto the total under the rollover rule; the caller
    /// has already proven `delta` finite.
    fn accumulate(&mut self, delta: f64) {
        self.total = if self.rollover > 0.0 {
            let increment = delta.rem_euclid(self.rollover);
            let sum = self.total + increment;
            if sum.is_finite() {
                // Both terms sit in `[0, rollover)`, so one
                // conditional wrap keeps the total inside the range.
                if sum >= self.rollover {
                    sum - self.rollover
                } else {
                    sum
                }
            } else {
                // A rollover bound above `f64::MAX / 2` can overflow
                // the sum itself; the wrapped `sum - rollover` still
                // computes without overflow.
                self.total - (self.rollover - increment)
            }
        } else {
            (self.total + delta).clamp(-f64::MAX, f64::MAX)
        };
    }
}

impl Component for Totalizer {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        vec![
            IoRequirement::input::<f64>("rate", self.rate),
            IoRequirement::input::<bool>("reset", self.reset),
            IoRequirement::output::<f64>("total", self.total_out),
        ]
    }

    fn step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError> {
        let rate = io.read_typed::<f64>(self.rate)?;
        let reset = io.read_typed::<bool>(self.reset)?;
        let mut quality = rate.quality.merge(reset.quality);
        if !rate.value.is_finite() {
            quality = quality.merge(Quality::Bad(QualityReason::DeviceFault));
        }
        if reset.value {
            self.total = 0.0;
        } else if rate.quality.is_good() && rate.value.is_finite() {
            let delta = rate.value * self.rate_unit;
            if delta.is_finite() {
                self.accumulate(delta);
            } else {
                // The increment itself overflowed the type: nothing
                // finite to bank, so the total freezes flagged.
                quality = quality.merge(Quality::Bad(QualityReason::DeviceFault));
            }
        }
        io.write_sample(
            self.total_out,
            Sample::new(Value::Float(self.total), quality, tick),
        )?;
        Ok(())
    }

    /// Describes the totalizer: `rate` is the process rate being
    /// accumulated, `reset` the clearing condition, `total` the running
    /// total; the `rate_unit` and `rollover` parameters
    /// `from_parameters` reads.
    fn describe(&self) -> ComponentDescriptor {
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &[
                ("rate", PortRole::ProcessValue),
                ("reset", PortRole::Status),
                ("total", PortRole::Output),
            ],
            vec![
                describe::parameter("rate_unit", ValueKind::Float, Some(describe::POSITIVE_F64)),
                describe::parameter(
                    "rollover",
                    ValueKind::Float,
                    Some(describe::NONNEGATIVE_F64),
                ),
            ],
        )
    }

    /// Tunes a declared parameter at the scan boundary.
    ///
    /// The new `rate_unit` applies to the next scan's increment.
    /// Retuning `rollover` re-wraps the banked total into the new range
    /// immediately — a count banked under one dial lands on its residue
    /// under the retuned one — and tuning it to `0` releases the bound.
    fn apply_parameter(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        match parameter {
            "rate_unit" => {
                let tuned = params::tune_f64(&self.name, parameter, value)?;
                if !tuned.is_finite() || tuned <= 0.0 {
                    return Err(params::invalid_parameter(
                        &self.name,
                        parameter,
                        "must be finite and positive",
                    ));
                }
                self.rate_unit = tuned;
            }
            "rollover" => {
                let tuned = params::tune_f64(&self.name, parameter, value)?;
                if !tuned.is_finite() || tuned < 0.0 {
                    return Err(params::invalid_parameter(
                        &self.name,
                        parameter,
                        "must be finite and non-negative",
                    ));
                }
                self.rollover = tuned;
                if self.rollover > 0.0 {
                    self.total = self.total.rem_euclid(self.rollover);
                }
            }
            _ => return Err(params::unknown_parameter(&self.name, parameter)),
        }
        Ok(())
    }

    /// Reports the declared `rate_unit`/`rollover` tuning — the same
    /// fields [`capture_state`](Self::capture_state) checkpoints, so the
    /// faceplate and a tracking standby read one vocabulary.
    fn report_parameters(&self) -> StateMap {
        let mut parameters = StateMap::new();
        parameters.insert("rate_unit", Value::Float(self.rate_unit));
        parameters.insert("rollover", Value::Float(self.rollover));
        parameters
    }

    /// Captures the banked total and the tuned `rate_unit`/`rollover`,
    /// so a checkpointed totalizer continues accumulating from the
    /// transferred count under the same tuning.
    fn capture_state(&self) -> StateMap {
        let mut state = self.report_parameters();
        state.insert("total", Value::Float(self.total));
        state
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        state.ensure_known_fields(&self.name, &["total", "rate_unit", "rollover"])?;
        let total = state.require_f64(&self.name, "total")?;
        let rate_unit = state.require_f64(&self.name, "rate_unit")?;
        let rollover = state.require_f64(&self.name, "rollover")?;
        for (field, value) in [
            ("total", total),
            ("rate_unit", rate_unit),
            ("rollover", rollover),
        ] {
            if !value.is_finite() {
                return Err(StateError::InvalidValue {
                    element: self.name.clone(),
                    field: field.to_string(),
                    value: Value::Float(value),
                });
            }
        }
        if rate_unit <= 0.0 {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "rate_unit".to_string(),
                value: Value::Float(rate_unit),
            });
        }
        if rollover < 0.0 {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "rollover".to_string(),
                value: Value::Float(rollover),
            });
        }
        if rollover > 0.0 && !(0.0..rollover).contains(&total) {
            // A bounded total must already sit inside its range —
            // anything else is not a state this kind captured.
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "total".to_string(),
                value: Value::Float(total),
            });
        }
        self.total = total;
        self.rate_unit = rate_unit;
        self.rollover = rollover;
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

    const RATE: PointId = PointId(180);
    const RESET: PointId = PointId(181);
    const TOTAL: PointId = PointId(182);

    /// An unbounded totalizer counting `rate` once per scan.
    fn component() -> Totalizer {
        Totalizer::new("tot", RATE, RESET, TOTAL, 1.0, 0.0).unwrap()
    }

    fn io() -> TestIo {
        TestIo::new(&[
            (
                RATE,
                Direction::In,
                Sample::good(Value::Float(0.0), Tick::ZERO),
            ),
            (
                RESET,
                Direction::In,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
            (
                TOTAL,
                Direction::Out,
                Sample::good(Value::Float(0.0), Tick::ZERO),
            ),
        ])
    }

    /// Feeds `rate`/`reset` and steps once.
    fn step(block: &mut Totalizer, io: &TestIo, rate: f64, reset: bool, tick: u64) {
        io.feed(RATE, Sample::good(Value::Float(rate), Tick(tick)));
        io.feed(RESET, Sample::good(Value::Bool(reset), Tick(tick)));
        block.step(io, Tick(tick)).unwrap();
    }

    fn total(io: &TestIo) -> Sample {
        io.written(TOTAL).unwrap()
    }

    #[test]
    fn accumulates_the_rate_per_scan() {
        let mut block = component();
        let io = io();
        step(&mut block, &io, 4.0, false, 1);
        assert_eq!(total(&io).value, Value::Float(4.0));
        assert_eq!(total(&io).quality, Quality::Good);
        assert_eq!(total(&io).tick, Tick(1));

        step(&mut block, &io, 4.0, false, 2);
        step(&mut block, &io, 4.0, false, 3);
        assert_eq!(total(&io).value, Value::Float(12.0));

        // A negative rate counts the total down.
        step(&mut block, &io, -2.0, false, 4);
        assert_eq!(total(&io).value, Value::Float(10.0));
    }

    #[test]
    fn rate_unit_scales_the_increment() {
        // A per-hour rate scanned once per second: rate_unit 1/3600.
        let mut block = Totalizer::new("tot", RATE, RESET, TOTAL, 1.0 / 3600.0, 0.0).unwrap();
        let io = io();
        step(&mut block, &io, 720.0, false, 1);
        assert_eq!(total(&io).value, Value::Float(0.2));
        step(&mut block, &io, 720.0, false, 2);
        assert_eq!(total(&io).value, Value::Float(0.4));
    }

    #[test]
    fn reset_clears_and_dominates_the_increment() {
        let mut block = component();
        let io = io();
        step(&mut block, &io, 4.0, false, 1);
        step(&mut block, &io, 4.0, false, 2);
        assert_eq!(total(&io).value, Value::Float(8.0));

        // Reset with a simultaneous valid rate: nothing accrues.
        step(&mut block, &io, 4.0, true, 3);
        assert_eq!(total(&io).value, Value::Float(0.0));

        // Release resumes accumulation from zero.
        step(&mut block, &io, 4.0, false, 4);
        assert_eq!(total(&io).value, Value::Float(4.0));
    }

    #[test]
    fn rollover_wraps_the_total_keeping_the_overshoot() {
        let mut block = Totalizer::new("tot", RATE, RESET, TOTAL, 1.0, 10.0).unwrap();
        let io = io();
        step(&mut block, &io, 9.0, false, 1);
        assert_eq!(total(&io).value, Value::Float(9.0));
        // 9 + 3 wraps to 2 — the overshoot past 10 is kept.
        step(&mut block, &io, 3.0, false, 2);
        assert_eq!(total(&io).value, Value::Float(2.0));
        // An increment wider than the range banks only its residue.
        step(&mut block, &io, 25.0, false, 3);
        assert_eq!(total(&io).value, Value::Float(7.0));
        // A negative step wraps down from the top.
        step(&mut block, &io, -9.0, false, 4);
        assert_eq!(total(&io).value, Value::Float(8.0));
    }

    #[test]
    fn unbounded_accumulation_saturates_at_the_type_limit() {
        let mut block = component();
        let io = io();
        step(&mut block, &io, f64::MAX, false, 1);
        step(&mut block, &io, f64::MAX, false, 2);
        // The sum overflowed: the total saturates, never goes infinite.
        assert_eq!(total(&io).value, Value::Float(f64::MAX));
        step(&mut block, &io, -f64::MAX, false, 3);
        assert_eq!(total(&io).value, Value::Float(0.0));
    }

    #[test]
    fn non_good_rate_freezes_the_total_and_stamps_the_quality() {
        let mut block = component();
        let io = io();
        step(&mut block, &io, 4.0, false, 1);
        assert_eq!(total(&io).value, Value::Float(4.0));

        // A Bad rate banks nothing; the output reports its quality.
        io.feed(
            RATE,
            Sample::new(
                Value::Float(99.0),
                Quality::Bad(QualityReason::CommunicationFault),
                Tick(2),
            ),
        );
        io.feed(RESET, Sample::good(Value::Bool(false), Tick(2)));
        block.step(&io, Tick(2)).unwrap();
        let out = total(&io);
        assert_eq!(out.value, Value::Float(4.0));
        assert_eq!(out.quality, Quality::Bad(QualityReason::CommunicationFault));

        // Uncertain freezes identically.
        io.feed(
            RATE,
            Sample::new(
                Value::Float(99.0),
                Quality::Uncertain(QualityReason::Stale),
                Tick(3),
            ),
        );
        block.step(&io, Tick(3)).unwrap();
        let out = total(&io);
        assert_eq!(out.value, Value::Float(4.0));
        assert_eq!(out.quality, Quality::Uncertain(QualityReason::Stale));

        // Recovery resumes where the total held.
        step(&mut block, &io, 4.0, false, 4);
        assert_eq!(total(&io).value, Value::Float(8.0));
    }

    #[test]
    fn non_finite_rate_freezes_and_marks_bad() {
        let mut block = component();
        let io = io();
        step(&mut block, &io, 4.0, false, 1);

        io.feed(RATE, Sample::good(Value::Float(f64::NAN), Tick(2)));
        block.step(&io, Tick(2)).unwrap();
        let out = total(&io);
        assert_eq!(out.value, Value::Float(4.0));
        assert_eq!(out.quality, Quality::Bad(QualityReason::DeviceFault));

        // An increment that overflows f64 cannot be banked either.
        let mut block = Totalizer::new("tot", RATE, RESET, TOTAL, f64::MAX, 0.0).unwrap();
        step(&mut block, &io, f64::MAX, false, 3);
        let out = total(&io);
        assert_eq!(out.value, Value::Float(0.0));
        assert_eq!(out.quality, Quality::Bad(QualityReason::DeviceFault));
    }

    #[test]
    fn reset_acts_despite_a_bad_rate() {
        let mut block = component();
        let io = io();
        step(&mut block, &io, 4.0, false, 1);
        assert_eq!(total(&io).value, Value::Float(4.0));

        io.feed(
            RATE,
            Sample::new(
                Value::Float(99.0),
                Quality::Bad(QualityReason::CommunicationFault),
                Tick(2),
            ),
        );
        io.feed(RESET, Sample::good(Value::Bool(true), Tick(2)));
        block.step(&io, Tick(2)).unwrap();
        let out = total(&io);
        assert_eq!(out.value, Value::Float(0.0));
        assert_eq!(out.quality, Quality::Bad(QualityReason::CommunicationFault));
    }

    #[test]
    fn restored_totalizer_continues_mid_accumulation() {
        let mut block = Totalizer::new("tot", RATE, RESET, TOTAL, 0.5, 100.0).unwrap();
        let io = io();
        step(&mut block, &io, 10.0, false, 1);
        step(&mut block, &io, 10.0, false, 2);
        step(&mut block, &io, 10.0, false, 3);
        assert_eq!(total(&io).value, Value::Float(15.0));

        let state = block.capture_state();
        assert_eq!(state.get("total"), Some(Value::Float(15.0)));
        assert_eq!(state.get("rate_unit"), Some(Value::Float(0.5)));
        assert_eq!(state.get("rollover"), Some(Value::Float(100.0)));

        let mut standby = Totalizer::new("tot", RATE, RESET, TOTAL, 1.0, 0.0).unwrap();
        standby.restore_state(&state).unwrap();
        assert_eq!(standby.capture_state(), state);

        // The standby banks the same increments — and wraps under the
        // transferred rollover.
        for tick in 4..24 {
            step(&mut standby, &io, 10.0, false, tick);
        }
        assert_eq!(total(&io).value, Value::Float(15.0));
    }

    #[test]
    fn incompatible_state_is_a_named_error() {
        let mut block = component();
        let mut state = StateMap::new();
        state.insert("total", Value::Float(f64::INFINITY));
        state.insert("rate_unit", Value::Float(1.0));
        state.insert("rollover", Value::Float(0.0));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::InvalidValue { ref field, .. }) if field == "total"
        ));
        let mut state = StateMap::new();
        state.insert("total", Value::Float(1.0));
        state.insert("rate_unit", Value::Float(0.0));
        state.insert("rollover", Value::Float(0.0));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::InvalidValue { ref field, .. }) if field == "rate_unit"
        ));
        let mut state = StateMap::new();
        state.insert("total", Value::Float(1.0));
        state.insert("rate_unit", Value::Float(1.0));
        state.insert("rollover", Value::Float(-10.0));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::InvalidValue { ref field, .. }) if field == "rollover"
        ));
        // A bounded total outside its range is not a captured state.
        let mut state = StateMap::new();
        state.insert("total", Value::Float(150.0));
        state.insert("rate_unit", Value::Float(1.0));
        state.insert("rollover", Value::Float(100.0));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::InvalidValue { ref field, .. }) if field == "total"
        ));
        let mut state = StateMap::new();
        state.insert("total", Value::Int(4));
        state.insert("rate_unit", Value::Float(1.0));
        state.insert("rollover", Value::Float(0.0));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::IncompatibleField { ref field, .. }) if field == "total"
        ));
        assert!(matches!(
            block.restore_state(&StateMap::new()),
            Err(StateError::MissingField { ref field, .. }) if field == "total"
        ));
        let mut state = StateMap::new();
        state.insert("total", Value::Float(1.0));
        state.insert("rate_unit", Value::Float(1.0));
        state.insert("rollover", Value::Float(0.0));
        state.insert("other", Value::Float(1.0));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::UnknownField { ref field, .. }) if field == "other"
        ));
    }

    #[test]
    fn apply_parameter_retunes_the_unit_and_rollover() {
        let mut block = component();
        let io = io();
        step(&mut block, &io, 10.0, false, 1);
        assert_eq!(total(&io).value, Value::Float(10.0));

        block
            .apply_parameter("rate_unit", Value::Float(0.5))
            .unwrap();
        step(&mut block, &io, 10.0, false, 2);
        assert_eq!(total(&io).value, Value::Float(15.0));

        // Retuning the rollover re-wraps the banked total immediately.
        block
            .apply_parameter("rollover", Value::Float(10.0))
            .unwrap();
        assert_eq!(block.total, 5.0);
        step(&mut block, &io, 10.0, false, 3);
        assert_eq!(total(&io).value, Value::Float(0.0));
        // Tuning back to 0 releases the bound.
        block
            .apply_parameter("rollover", Value::Float(0.0))
            .unwrap();
        step(&mut block, &io, 10.0, false, 4);
        assert_eq!(total(&io).value, Value::Float(5.0));

        assert_eq!(
            block
                .apply_parameter("other", Value::Float(1.0))
                .unwrap_err(),
            CommandError::UnknownParameter {
                component: "tot".to_string(),
                parameter: "other".to_string(),
            }
        );
        for bad in [0.0, -0.5, f64::NAN] {
            assert!(
                matches!(
                    block.apply_parameter("rate_unit", Value::Float(bad)).unwrap_err(),
                    CommandError::InvalidParameter { ref parameter, .. } if parameter == "rate_unit"
                ),
                "rate_unit={bad}"
            );
        }
        for bad in [-1.0, f64::INFINITY] {
            assert!(
                matches!(
                    block.apply_parameter("rollover", Value::Float(bad)).unwrap_err(),
                    CommandError::InvalidParameter { ref parameter, .. } if parameter == "rollover"
                ),
                "rollover={bad}"
            );
        }
        assert_eq!(
            block.capture_state().get("rate_unit"),
            Some(Value::Float(0.5))
        );
    }

    #[test]
    fn builds_from_parameter_map() {
        // Defaults: rate_unit 1.0, rollover 0.0 (unbounded).
        let block =
            Totalizer::from_parameters("tot", RATE, RESET, TOTAL, &Parameters::new()).unwrap();
        assert_eq!(block.rate_unit, 1.0);
        assert_eq!(block.rollover, 0.0);

        let instance = ComponentInstance {
            id: ComponentId(18),
            kind: Totalizer::KIND.to_string(),
            parameters: [
                ("rate_unit".to_string(), Value::Float(0.5)),
                ("rollover".to_string(), Value::Int(1000)),
            ]
            .into_iter()
            .collect(),
            ports: BTreeMap::new(),
        };
        let block =
            Totalizer::from_parameters("tot", RATE, RESET, TOTAL, &instance.parameters).unwrap();
        assert_eq!(block.rate_unit, 0.5);
        assert_eq!(block.rollover, 1000.0);

        for (parameter, bad) in [
            ("rate_unit", Value::Float(0.0)),
            ("rate_unit", Value::Float(-1.0)),
            ("rate_unit", Value::Float(f64::NAN)),
            ("rate_unit", Value::Bool(true)),
            ("rollover", Value::Float(-1.0)),
            ("rollover", Value::Float(f64::INFINITY)),
            ("rollover", Value::Bool(false)),
        ] {
            let parameters: Parameters = [(parameter.to_string(), bad)].into_iter().collect();
            assert!(
                matches!(
                    Totalizer::from_parameters("tot", RATE, RESET, TOTAL, &parameters)
                        .unwrap_err(),
                    ParameterError::Invalid { parameter: ref p, .. } if p == parameter
                ),
                "{parameter}={bad:?}"
            );
        }
    }

    #[test]
    fn describes_itself() {
        let descriptor = component().describe();
        assert_eq!(descriptor.name, "tot");
        assert_eq!(descriptor.kind, Totalizer::KIND);
        assert_eq!(descriptor.label, "tot");
        assert_eq!(
            descriptor.ports,
            [
                PortDescriptor {
                    name: "rate".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: Some(PortRole::ProcessValue),
                    point: None,
                },
                PortDescriptor {
                    name: "reset".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Status),
                    point: None,
                },
                PortDescriptor {
                    name: "total".to_string(),
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
            [
                ParameterDescriptor {
                    name: "rate_unit".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::POSITIVE_F64),
                },
                ParameterDescriptor {
                    name: "rollover".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::NONNEGATIVE_F64),
                },
            ]
        );
    }
}
