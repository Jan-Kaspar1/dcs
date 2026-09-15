//! Deviation monitor: the expected-versus-measured verification a
//! measured-consumption signal enables — the dose-confirmation check
//! architecture decision 53 records for `WW-CTL-003`
//! (`docs/research/chemical-dosing.md`). Where no measured-consumption
//! signal exists the model simply does not instantiate the kind —
//! commanded `totalizer` integration alone claims no dose-confirmed
//! verdict.

use crate::describe;
use crate::params::{self, ParameterError, Parameters};
use dcs_core::{
    CommandError, ComponentDescriptor, PointId, PortRole, Quality, QualityReason, Sample,
    StateError, StateMap, Tick, Value, ValueKind,
};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// A deviation monitor: reads `expected` (`In`, `Float`) — the
/// commanded chemical rate or total — and `measured` (`In`, `Float`) —
/// the measured consumption — and drives `deviation` (`Out`, `Float`),
/// the relative deviation the last completed window returned, and
/// `deviating` (`Out`, `Bool`), the dose-not-confirmed condition the
/// decision-55 alarm set consumes.
///
/// **The windowed comparison.** Each scan both inputs read `Good` with
/// finite values banks the pair: `expected` accrues onto the expected
/// sum, `measured` onto the measured sum, and the window position
/// advances. The scan that completes the declared `window_ticks` window
/// evaluates `deviation = (measured_sum − expected_sum) / expected_sum`
/// — the window's relative deviation, signed: positive is over-delivery
/// against the command, negative under-delivery — and sets `deviating`
/// while `|deviation| > deviation_limit`. The accumulators then clear
/// and the next window opens. Between evaluations both outputs hold the
/// last completed window's verdict: the comparison is a window
/// quantity, so a single scan's excursion inside an otherwise tracking
/// window never asserts `deviating`, and a sustained deviation asserts
/// at the close of the window it dominated. `deviation` reports `0.0`
/// and `deviating` `false` until the first window completes. A
/// `window_ticks` of `1` evaluates every banked scan — the degenerate
/// instantaneous case the same mechanism covers.
///
/// This windowed-accumulator form is what covers a slow-trend measured
/// signal — the tank drawdown case decision 53 records — where the two
/// wired signals are rates: per-tick readings of a slow trend carry
/// noise a static or instantaneous bound trips on, while the window's
/// accumulated quantities compare what was commanded against what was
/// delivered. The same accumulation serves a wired pair of running
/// totals, comparing the sustained totals offset across the window.
///
/// **Zero expected accumulation.** A window commanding nothing divides
/// by zero: `deviation` is `0.0` when the window's measured
/// accumulation is also zero — nothing commanded, nothing delivered —
/// and saturates at `±f64::MAX` otherwise, the largest expressible
/// relative deviation, so measured consumption against a silent command
/// trips any finite `deviation_limit`. A quotient that overflows `f64`
/// saturates the same way.
///
/// **Quality rule.** A scan whose `expected` or `measured` sample is
/// not `Good` or not finite banks nothing — neither accumulator moves
/// and the window position does not advance, the freeze rule
/// `totalizer` records — and the outputs keep the standing verdict
/// stamped with the merged worst of the two input qualities, plus
/// `Bad(DeviceFault)` for a non-finite reading the point did not
/// report. A banked sum that would overflow `f64` freezes the same way
/// flagged `Bad(DeviceFault)`. Both outputs always carry the merged
/// input quality: the verdict is only as trusted as the signals it
/// compares.
///
/// Declared I/O: `expected` (`In`, `Float`), `measured` (`In`,
/// `Float`), `deviation` (`Out`, `Float`), `deviating` (`Out`, `Bool`).
///
/// Parameters: `deviation_limit` — required finite, non-negative
/// `Float`, the relative-deviation magnitude the windowed verdict trips
/// at — and `window_ticks` — required `Int` in `1..=i64::MAX`, the
/// scans of trusted input one window accumulates. Both tune through
/// `set_parameter`; retuning `window_ticks` keeps the banked
/// accumulators and position, so a shortened window closes at the next
/// banked scan and a lengthened one accumulates the remaining balance.
#[derive(Debug)]
pub struct DeviationMonitor {
    name: String,
    expected: PointId,
    measured: PointId,
    deviation_out: PointId,
    deviating_out: PointId,
    /// The relative-deviation magnitude `deviating` trips at.
    deviation_limit: f64,
    /// The trusted scans one window accumulates before it evaluates.
    window_ticks: u64,
    /// The banked `expected` accumulation of the open window.
    expected_sum: f64,
    /// The banked `measured` accumulation of the open window.
    measured_sum: f64,
    /// The banked pairs in the open window — `0..window_ticks` at rest.
    window_position: u64,
    /// The standing verdict's relative deviation — `0.0` until the
    /// first window completes.
    deviation: f64,
    /// The standing verdict's trip state.
    deviating: bool,
}

/// The window's signed relative deviation, `(measured − expected) /
/// expected` — saturated to `±f64::MAX` where no finite quotient
/// exists: a zero `expected` accumulation against a nonzero `measured`
/// one, or a difference overflowing the type. Both sums are finite by
/// construction — the freeze rule refuses to bank a sum that would
/// overflow — so only the difference and the divide need the
/// saturation.
fn relative_deviation(expected: f64, measured: f64) -> f64 {
    if expected == 0.0 {
        return if measured == 0.0 {
            0.0
        } else {
            measured.signum() * f64::MAX
        };
    }
    let quotient = (measured - expected) / expected;
    if quotient.is_finite() {
        quotient
    } else {
        quotient.signum() * f64::MAX
    }
}

impl DeviationMonitor {
    /// The component-kind string the model-driven registry maps onto
    /// this type's constructor.
    pub const KIND: &'static str = "deviation-monitor";

    /// Builds the component from explicit points, the relative
    /// deviation limit, and the window length in scans.
    ///
    /// `deviation_limit` must be finite and non-negative;
    /// `window_ticks` must be at least `1` and may not exceed
    /// `i64::MAX` — the captured position is an `Int`. Violations are
    /// reported as a [`ParameterError`] naming the parameter.
    pub fn new(
        name: impl Into<String>,
        expected: PointId,
        measured: PointId,
        deviation: PointId,
        deviating: PointId,
        deviation_limit: f64,
        window_ticks: u64,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        Self::check_config(&name, deviation_limit, window_ticks)?;
        Ok(Self {
            name,
            expected,
            measured,
            deviation_out: deviation,
            deviating_out: deviating,
            deviation_limit,
            window_ticks,
            expected_sum: 0.0,
            measured_sum: 0.0,
            window_position: 0,
            deviation: 0.0,
            deviating: false,
        })
    }

    /// The parameter invariants every construction and tuning path
    /// enforces, reported as a [`ParameterError`] naming `component`
    /// and the offending parameter.
    fn check_config(
        component: &str,
        deviation_limit: f64,
        window_ticks: u64,
    ) -> Result<(), ParameterError> {
        if !deviation_limit.is_finite() {
            return Err(params::invalid(
                component,
                "deviation_limit",
                "must be finite".to_string(),
            ));
        }
        if deviation_limit < 0.0 {
            return Err(params::invalid(
                component,
                "deviation_limit",
                "must be non-negative".to_string(),
            ));
        }
        if window_ticks == 0 {
            return Err(params::invalid(
                component,
                "window_ticks",
                "must be at least 1".to_string(),
            ));
        }
        if window_ticks > i64::MAX as u64 {
            return Err(params::invalid(
                component,
                "window_ticks",
                format!("must not exceed i64::MAX, found {window_ticks}"),
            ));
        }
        Ok(())
    }

    /// Builds the component from a plant-model parameter map, reading
    /// the parameters listed on the type's docs.
    pub fn from_parameters(
        name: impl Into<String>,
        expected: PointId,
        measured: PointId,
        deviation: PointId,
        deviating: PointId,
        parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        let deviation_limit = params::required_f64(&name, parameters, "deviation_limit")?;
        let window_ticks = params::required_u64(&name, parameters, "window_ticks")?;
        Self::new(
            name,
            expected,
            measured,
            deviation,
            deviating,
            deviation_limit,
            window_ticks,
        )
    }

    /// Retunes one declared parameter, or reports a [`CommandError`]
    /// naming `component` and the offending parameter. A refused value
    /// changes nothing.
    fn tune(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        match parameter {
            "deviation_limit" => {
                let tuned = params::tune_f64(&self.name, parameter, value)?;
                if !tuned.is_finite() || tuned < 0.0 {
                    return Err(params::invalid_parameter(
                        &self.name,
                        parameter,
                        "must be finite and non-negative",
                    ));
                }
                self.deviation_limit = tuned;
            }
            "window_ticks" => {
                let tuned = params::tune_u64(&self.name, parameter, value)?;
                if tuned == 0 || tuned > i64::MAX as u64 {
                    return Err(params::invalid_parameter(
                        &self.name,
                        parameter,
                        "must be in 1..=i64::MAX",
                    ));
                }
                // The banked window keeps its accumulators and position:
                // a shortened bound closes the window at the next banked
                // scan, a lengthened one accumulates the balance.
                self.window_ticks = tuned;
            }
            _ => return Err(params::unknown_parameter(&self.name, parameter)),
        }
        Ok(())
    }
}

impl Component for DeviationMonitor {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        vec![
            IoRequirement::input::<f64>("expected", self.expected),
            IoRequirement::input::<f64>("measured", self.measured),
            IoRequirement::output::<f64>("deviation", self.deviation_out),
            IoRequirement::output::<bool>("deviating", self.deviating_out),
        ]
    }

    fn step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError> {
        let expected = io.read_typed::<f64>(self.expected)?;
        let measured = io.read_typed::<f64>(self.measured)?;

        // Both outputs carry the worst of the two inputs, and a
        // non-finite reading is a device fault the point did not
        // report — the verdict is only as trusted as its signals.
        let mut quality = expected.quality.merge(measured.quality);
        if !expected.value.is_finite() || !measured.value.is_finite() {
            quality = quality.merge(Quality::Bad(QualityReason::DeviceFault));
        }

        if expected.quality.is_good()
            && measured.quality.is_good()
            && expected.value.is_finite()
            && measured.value.is_finite()
        {
            let expected_sum = self.expected_sum + expected.value;
            let measured_sum = self.measured_sum + measured.value;
            if expected_sum.is_finite() && measured_sum.is_finite() {
                self.expected_sum = expected_sum;
                self.measured_sum = measured_sum;
                self.window_position += 1;
                if self.window_position >= self.window_ticks {
                    self.deviation = relative_deviation(self.expected_sum, self.measured_sum);
                    self.deviating = self.deviation.abs() > self.deviation_limit;
                    self.expected_sum = 0.0;
                    self.measured_sum = 0.0;
                    self.window_position = 0;
                }
            } else {
                // A banked sum that would overflow the type cannot be
                // banked: the scan freezes like an untrusted one.
                quality = quality.merge(Quality::Bad(QualityReason::DeviceFault));
            }
        }

        io.write_sample(
            self.deviation_out,
            Sample::new(Value::Float(self.deviation), quality, tick),
        )?;
        io.write_sample(
            self.deviating_out,
            Sample::new(Value::Bool(self.deviating), quality, tick),
        )?;
        Ok(())
    }

    /// Describes the monitor: `expected` and `measured` are the process
    /// values it compares, `deviation` the reported window deviation,
    /// `deviating` the standing dose-not-confirmed condition; the
    /// declared parameter set is the two keys `from_parameters` reads.
    fn describe(&self) -> ComponentDescriptor {
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &[
                ("expected", PortRole::ProcessValue),
                ("measured", PortRole::ProcessValue),
                ("deviation", PortRole::Output),
                ("deviating", PortRole::Status),
            ],
            vec![
                describe::parameter(
                    "deviation_limit",
                    ValueKind::Float,
                    Some(describe::NONNEGATIVE_F64),
                ),
                describe::parameter("window_ticks", ValueKind::Int, Some(describe::POSITIVE_INT)),
            ],
        )
    }

    /// Tunes the declared parameters at the scan boundary — the
    /// recorded mechanism for operator-adjustable limits. Retuning
    /// `window_ticks` keeps the open window's accumulators and
    /// position; a refused value changes nothing.
    fn apply_parameter(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        self.tune(parameter, value)
    }

    /// Reports the tuned `deviation_limit`/`window_ticks` — the same
    /// fields [`capture_state`](Self::capture_state) checkpoints, so
    /// the faceplate and a tracking standby read one vocabulary.
    fn report_parameters(&self) -> StateMap {
        let mut parameters = StateMap::new();
        parameters.insert("deviation_limit", Value::Float(self.deviation_limit));
        parameters.insert("window_ticks", Value::Int(self.window_ticks as i64));
        parameters
    }

    /// Captures the tuned parameters, the open window's accumulators
    /// and position, and the standing verdict — the state the windowed
    /// comparison needs — so a checkpointed standby continues a
    /// half-filled window identically.
    fn capture_state(&self) -> StateMap {
        let mut state = self.report_parameters();
        state.insert("expected_sum", Value::Float(self.expected_sum));
        state.insert("measured_sum", Value::Float(self.measured_sum));
        state.insert("window_position", Value::Int(self.window_position as i64));
        state.insert("deviation", Value::Float(self.deviation));
        state.insert("deviating", Value::Bool(self.deviating));
        state
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        state.ensure_known_fields(
            &self.name,
            &[
                "deviation_limit",
                "window_ticks",
                "expected_sum",
                "measured_sum",
                "window_position",
                "deviation",
                "deviating",
            ],
        )?;

        // The whole map is validated before any state mutates, so a
        // rejected checkpoint leaves the component on its last-good
        // state rather than half-applied.
        let deviation_limit = state.require_f64(&self.name, "deviation_limit")?;
        let window_ticks = state.require_i64(&self.name, "window_ticks")?;
        let expected_sum = state.require_f64(&self.name, "expected_sum")?;
        let measured_sum = state.require_f64(&self.name, "measured_sum")?;
        let window_position = state.require_i64(&self.name, "window_position")?;
        let deviation = state.require_f64(&self.name, "deviation")?;
        let deviating = state.require_bool(&self.name, "deviating")?;

        // The same invariants `new` and `apply_parameter` enforce.
        if !deviation_limit.is_finite() || deviation_limit < 0.0 {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "deviation_limit".to_string(),
                value: Value::Float(deviation_limit),
            });
        }
        if !(1..=i64::MAX).contains(&window_ticks) {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "window_ticks".to_string(),
                value: Value::Int(window_ticks),
            });
        }
        for (field, value) in [
            ("expected_sum", expected_sum),
            ("measured_sum", measured_sum),
            ("deviation", deviation),
        ] {
            if !value.is_finite() {
                return Err(StateError::InvalidValue {
                    element: self.name.clone(),
                    field: field.to_string(),
                    value: Value::Float(value),
                });
            }
        }
        // The position counts banked pairs of the open window —
        // `0..window_ticks` is the only range a capture produces, the
        // evaluation resetting it in the scan that reaches the bound.
        if !(0..window_ticks).contains(&window_position) {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "window_position".to_string(),
                value: Value::Int(window_position),
            });
        }
        // A position of zero marks a just-closed or never-filled
        // window: its accumulators are zero in every state this kind
        // captures.
        if window_position == 0 && (expected_sum != 0.0 || measured_sum != 0.0) {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "expected_sum".to_string(),
                value: Value::Float(expected_sum),
            });
        }

        self.deviation_limit = deviation_limit;
        self.window_ticks = window_ticks as u64;
        self.expected_sum = expected_sum;
        self.measured_sum = measured_sum;
        self.window_position = window_position as u64;
        self.deviation = deviation;
        self.deviating = deviating;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::TestIo;
    use dcs_core::{Direction, ParameterDescriptor, PortDescriptor};

    const EXPECTED: PointId = PointId(40);
    const MEASURED: PointId = PointId(41);
    const DEVIATION: PointId = PointId(50);
    const DEVIATING: PointId = PointId(51);

    /// Limit 0.1 over a four-scan window.
    fn component() -> DeviationMonitor {
        DeviationMonitor::new("dev", EXPECTED, MEASURED, DEVIATION, DEVIATING, 0.1, 4).unwrap()
    }

    fn io() -> TestIo {
        TestIo::new(&[
            (
                EXPECTED,
                Direction::In,
                Sample::good(Value::Float(0.0), Tick::ZERO),
            ),
            (
                MEASURED,
                Direction::In,
                Sample::good(Value::Float(0.0), Tick::ZERO),
            ),
            (
                DEVIATION,
                Direction::Out,
                Sample::good(Value::Float(0.0), Tick::ZERO),
            ),
            (
                DEVIATING,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
        ])
    }

    /// Feeds both inputs `Good` and steps once.
    fn step(block: &mut DeviationMonitor, io: &TestIo, expected: f64, measured: f64, tick: u64) {
        io.feed(EXPECTED, Sample::good(Value::Float(expected), Tick(tick)));
        io.feed(MEASURED, Sample::good(Value::Float(measured), Tick(tick)));
        block.step(io, Tick(tick)).unwrap();
    }

    fn deviation(io: &TestIo) -> Sample {
        io.written(DEVIATION).unwrap()
    }

    fn deviating(io: &TestIo) -> Sample {
        io.written(DEVIATING).unwrap()
    }

    fn deviating_value(io: &TestIo) -> bool {
        match deviating(io).value {
            Value::Bool(deviating) => deviating,
            value => panic!("deviating must be Bool, got {value:?}"),
        }
    }

    #[test]
    fn a_tracking_measured_signal_keeps_deviating_clear() {
        let mut block = component();
        let io = io();
        // Two full windows of measured tracking expected exactly.
        for tick in 1..=8 {
            step(&mut block, &io, 10.0, 10.0, tick);
            assert!(!deviating_value(&io), "tick {tick}");
        }
        assert_eq!(deviation(&io).value, Value::Float(0.0));
        // A window inside the limit reports its signed deviation.
        for tick in 9..=12 {
            step(&mut block, &io, 10.0, 10.5, tick);
        }
        assert_eq!(deviation(&io).value, Value::Float(0.05));
        assert!(!deviating_value(&io));
        // Exactly at the limit is not deviating.
        for tick in 13..=16 {
            step(&mut block, &io, 10.0, 11.0, tick);
        }
        assert_eq!(deviation(&io).value, Value::Float(0.1));
        assert!(!deviating_value(&io));
    }

    #[test]
    fn a_sustained_deviation_asserts_at_the_windows_close() {
        let mut block = component();
        let io = io();
        // Measured runs 20% over: inside the window the standing
        // verdict holds — the first three excess scans assert nothing.
        for tick in 1..=3 {
            step(&mut block, &io, 10.0, 12.0, tick);
            assert_eq!(deviation(&io).value, Value::Float(0.0), "tick {tick}");
            assert!(!deviating_value(&io), "tick {tick}");
        }
        // The fourth banked scan closes the window: deviation 0.2
        // exceeds the 0.1 limit and the verdict asserts.
        step(&mut block, &io, 10.0, 12.0, 4);
        assert_eq!(deviation(&io).value, Value::Float(0.2));
        assert!(deviating_value(&io));
        // The verdict stands while the next window accumulates.
        step(&mut block, &io, 10.0, 12.0, 5);
        assert!(deviating_value(&io));
    }

    #[test]
    fn recovery_clears_at_the_close_of_a_within_limit_window() {
        let mut block = component();
        let io = io();
        for tick in 1..=4 {
            step(&mut block, &io, 10.0, 5.0, tick);
        }
        // Under-delivery: deviation −0.5 asserts the same way.
        assert_eq!(deviation(&io).value, Value::Float(-0.5));
        assert!(deviating_value(&io));

        // The recovered window still holds the standing verdict until
        // it completes — recovery clears at the window's close.
        for tick in 5..=7 {
            step(&mut block, &io, 10.0, 10.0, tick);
            assert!(deviating_value(&io), "tick {tick}");
        }
        step(&mut block, &io, 10.0, 10.0, 8);
        assert_eq!(deviation(&io).value, Value::Float(0.0));
        assert!(!deviating_value(&io));
    }

    #[test]
    fn a_slow_trend_is_compared_over_the_window_not_instantaneously() {
        let mut block = component();
        let io = io();
        // The drawdown shape: a measured rate wandering ±20% around the
        // command. Each scan's instantaneous deviation would trip the
        // limit; the window's accumulated quantities nearly cancel and
        // the verdict stays clear.
        for (tick, measured) in [(1, 8.0), (2, 12.0), (3, 8.0), (4, 12.0)] {
            step(&mut block, &io, 10.0, measured, tick);
            assert!(!deviating_value(&io), "tick {tick}");
        }
        // Sum 40 against 40: the window's deviation is zero.
        assert_eq!(deviation(&io).value, Value::Float(0.0));

        // A window of one scan is the instantaneous case the same
        // mechanism covers: each excess scan asserts on the spot.
        let mut instant =
            DeviationMonitor::new("dev", EXPECTED, MEASURED, DEVIATION, DEVIATING, 0.1, 1).unwrap();
        step(&mut instant, &io, 10.0, 8.0, 5);
        assert!(deviating_value(&io));
        assert_eq!(deviation(&io).value, Value::Float(-0.2));
        step(&mut instant, &io, 10.0, 10.0, 6);
        assert!(!deviating_value(&io));
    }

    #[test]
    fn a_lone_excursion_inside_a_tracking_window_never_asserts() {
        let mut block = component();
        let io = io();
        // One scan's thirty-percent shortfall inside an otherwise
        // tracking window dilutes below the limit — the comparison is
        // the window's, not the scan's.
        for (tick, measured) in [(1, 10.0), (2, 7.0), (3, 10.0), (4, 10.0)] {
            step(&mut block, &io, 10.0, measured, tick);
            assert!(!deviating_value(&io), "tick {tick}");
        }
        assert_eq!(deviation(&io).value, Value::Float(-0.075));
    }

    #[test]
    fn a_non_good_input_banks_nothing_and_holds_the_verdict() {
        for point in [EXPECTED, MEASURED] {
            let mut block = component();
            let io = io();
            step(&mut block, &io, 10.0, 12.0, 1);
            step(&mut block, &io, 10.0, 12.0, 2);

            // Two bad scans: the window does not advance, the standing
            // verdict holds stamped with the input's quality.
            for tick in 3..=4 {
                io.feed(
                    point,
                    Sample::new(
                        Value::Float(99.0),
                        Quality::Bad(QualityReason::CommunicationFault),
                        Tick(tick),
                    ),
                );
                block.step(&io, Tick(tick)).unwrap();
                assert_eq!(deviation(&io).value, Value::Float(0.0));
                assert!(!deviating_value(&io));
                assert_eq!(
                    deviating(&io).quality,
                    Quality::Bad(QualityReason::CommunicationFault)
                );
            }

            // The window resumes where it froze: the excess completes
            // on the second recovered scan — two banked plus two more.
            step(&mut block, &io, 10.0, 12.0, 5);
            step(&mut block, &io, 10.0, 12.0, 6);
            assert!(deviating_value(&io), "frozen point {point:?}");
            assert_eq!(deviation(&io).quality, Quality::Good);
        }
    }

    #[test]
    fn an_uncertain_input_freezes_and_stamps_the_verdict() {
        let mut block = component();
        let io = io();
        step(&mut block, &io, 10.0, 10.0, 1);
        io.feed(
            EXPECTED,
            Sample::new(
                Value::Float(10.0),
                Quality::Uncertain(QualityReason::Stale),
                Tick(2),
            ),
        );
        block.step(&io, Tick(2)).unwrap();
        assert_eq!(
            deviation(&io).quality,
            Quality::Uncertain(QualityReason::Stale)
        );
        // Recovery resumes the open window.
        for tick in 3..=5 {
            step(&mut block, &io, 10.0, 10.0, tick);
        }
        assert_eq!(deviation(&io).value, Value::Float(0.0));
        assert_eq!(deviation(&io).quality, Quality::Good);
    }

    #[test]
    fn a_non_finite_input_freezes_flagged_device_fault() {
        let mut block = component();
        let io = io();
        step(&mut block, &io, 10.0, 10.0, 1);
        io.feed(MEASURED, Sample::good(Value::Float(f64::NAN), Tick(2)));
        block.step(&io, Tick(2)).unwrap();
        assert_eq!(
            deviation(&io).quality,
            Quality::Bad(QualityReason::DeviceFault)
        );
        // A `Good`-stamped NaN banks nothing.
        step(&mut block, &io, 10.0, 10.0, 3);
        step(&mut block, &io, 10.0, 10.0, 4);
        step(&mut block, &io, 10.0, 10.0, 5);
        assert_eq!(deviation(&io).value, Value::Float(0.0));
        assert!(!deviating_value(&io));
    }

    #[test]
    fn an_unbankable_sum_freezes_flagged() {
        let mut block = component();
        let io = io();
        step(&mut block, &io, f64::MAX, f64::MAX, 1);
        step(&mut block, &io, f64::MAX, f64::MAX, 2);
        // Both sums overflowed on the second scan — the window froze at
        // one banked pair rather than banking an infinite sum.
        assert_eq!(
            deviation(&io).quality,
            Quality::Bad(QualityReason::DeviceFault)
        );
        assert!(!deviating_value(&io));
    }

    #[test]
    fn a_window_commanding_nothing_compares_against_zero() {
        let mut block = component();
        let io = io();
        // Nothing commanded, nothing delivered: agreement.
        for tick in 1..=4 {
            step(&mut block, &io, 0.0, 0.0, tick);
        }
        assert_eq!(deviation(&io).value, Value::Float(0.0));
        assert!(!deviating_value(&io));

        // Measured consumption against a silent command is an
        // unbounded relative deviation — the verdict trips on the
        // saturated value.
        for tick in 5..=8 {
            step(&mut block, &io, 0.0, 2.0, tick);
        }
        assert_eq!(deviation(&io).value, Value::Float(f64::MAX));
        assert!(deviating_value(&io));
    }

    #[test]
    fn the_verdict_waits_for_trusted_samples_to_fill_the_window() {
        let mut block = component();
        let io = io();
        // Alternating trusted and bad scans: only the trusted ones
        // bank — after three banked pairs the window is still open.
        for tick in 1..=6 {
            if tick % 2 == 0 {
                io.feed(
                    EXPECTED,
                    Sample::new(
                        Value::Float(10.0),
                        Quality::Bad(QualityReason::DeviceFault),
                        Tick(tick),
                    ),
                );
                io.feed(MEASURED, Sample::good(Value::Float(15.0), Tick(tick)));
                block.step(&io, Tick(tick)).unwrap();
            } else {
                step(&mut block, &io, 10.0, 15.0, tick);
            }
            assert!(!deviating_value(&io), "tick {tick}");
        }
        // The fourth trusted scan closes the window: four banked pairs
        // of 10 against 15 give deviation 0.5, and the verdict holds
        // through the following bad scan.
        step(&mut block, &io, 10.0, 15.0, 7);
        assert!(deviating_value(&io));
        assert_eq!(deviation(&io).value, Value::Float(0.5));
        io.feed(
            EXPECTED,
            Sample::new(
                Value::Float(10.0),
                Quality::Bad(QualityReason::DeviceFault),
                Tick(8),
            ),
        );
        block.step(&io, Tick(8)).unwrap();
        assert!(deviating_value(&io));
        assert_eq!(
            deviating(&io).quality,
            Quality::Bad(QualityReason::DeviceFault)
        );
    }

    #[test]
    fn capture_restore_mid_window_continues_identically() {
        let mut block = component();
        let io_a = io();
        let io_b = io();

        // Bank two pairs, then tune the limit so the tuned parameter
        // rides the checkpoint too.
        step(&mut block, &io_a, 10.0, 12.0, 1);
        step(&mut block, &io_a, 10.0, 12.0, 2);
        block
            .apply_parameter("deviation_limit", Value::Float(0.05))
            .unwrap();
        let state = block.capture_state();
        assert_eq!(state.get("expected_sum"), Some(Value::Float(20.0)));
        assert_eq!(state.get("measured_sum"), Some(Value::Float(24.0)));
        assert_eq!(state.get("window_position"), Some(Value::Int(2)));
        assert_eq!(state.get("deviation_limit"), Some(Value::Float(0.05)));

        let mut standby =
            DeviationMonitor::new("dev", EXPECTED, MEASURED, DEVIATION, DEVIATING, 0.1, 4).unwrap();
        standby.restore_state(&state).unwrap();

        // Both finish the window identically — the tuned 0.05 limit
        // trips on deviation 0.2 — and keep running in step through
        // the recovered window's clear.
        for tick in 3..=8 {
            let measured = if tick <= 4 { 12.0 } else { 10.0 };
            step(&mut block, &io_a, 10.0, measured, tick);
            step(&mut standby, &io_b, 10.0, measured, tick);
            assert_eq!(deviation(&io_a), deviation(&io_b), "tick {tick}");
            assert_eq!(deviating(&io_a), deviating(&io_b), "tick {tick}");
        }
        // The checkpointed window closed on the excess at tick 4; the
        // recovered one cleared both sides at tick 8.
        assert!(!deviating_value(&io_a));
        assert!(!deviating_value(&io_b));
        assert_eq!(standby.capture_state(), block.capture_state());
    }

    #[test]
    fn restore_rejects_an_incompatible_state_map() {
        let mut block = component();

        let mut foreign = block.capture_state();
        foreign.insert("surprise", Value::Int(1));
        assert!(matches!(
            block.restore_state(&foreign),
            Err(StateError::UnknownField { .. })
        ));
        assert!(matches!(
            block.restore_state(&StateMap::new()),
            Err(StateError::MissingField { .. })
        ));

        // A window position outside the accumulator range is refused,
        // as is one that disagrees with the banked sums.
        let mut bad = block.capture_state();
        bad.insert("window_position", Value::Int(4));
        assert_eq!(
            block.restore_state(&bad),
            Err(StateError::InvalidValue {
                element: "dev".to_string(),
                field: "window_position".to_string(),
                value: Value::Int(4),
            })
        );
        let mut bad = block.capture_state();
        bad.insert("expected_sum", Value::Float(3.0));
        assert_eq!(
            block.restore_state(&bad),
            Err(StateError::InvalidValue {
                element: "dev".to_string(),
                field: "expected_sum".to_string(),
                value: Value::Float(3.0),
            })
        );

        // Parameter invariants apply to the restored vocabulary too.
        let mut bad = block.capture_state();
        bad.insert("deviation_limit", Value::Float(-0.5));
        assert_eq!(
            block.restore_state(&bad),
            Err(StateError::InvalidValue {
                element: "dev".to_string(),
                field: "deviation_limit".to_string(),
                value: Value::Float(-0.5),
            })
        );
        let mut bad = block.capture_state();
        bad.insert("window_ticks", Value::Int(0));
        assert!(matches!(
            block.restore_state(&bad),
            Err(StateError::InvalidValue { ref field, .. }) if field == "window_ticks"
        ));
        let mut bad = block.capture_state();
        bad.insert("deviation", Value::Float(f64::NAN));
        assert!(matches!(
            block.restore_state(&bad),
            Err(StateError::InvalidValue { ref field, .. }) if field == "deviation"
        ));
    }

    #[test]
    fn malformed_parameters_fail_construction_naming_the_parameter() {
        for (limit, ticks, parameter) in [
            (f64::NAN, 4, "deviation_limit"),
            (-0.1, 4, "deviation_limit"),
            (0.1, 0, "window_ticks"),
            (0.1, i64::MAX as u64 + 1, "window_ticks"),
        ] {
            match DeviationMonitor::new(
                "dev", EXPECTED, MEASURED, DEVIATION, DEVIATING, limit, ticks,
            ) {
                Err(ParameterError::Invalid {
                    parameter: found, ..
                }) => assert_eq!(found, parameter),
                other => panic!("{parameter}: expected Invalid, got {other:?}"),
            }
        }

        // Through the parameter map: a missing key is Missing, a
        // mistyped one Invalid.
        let mut parameters = Parameters::new();
        parameters.insert("deviation_limit".to_string(), Value::Float(0.1));
        match DeviationMonitor::from_parameters(
            "dev",
            EXPECTED,
            MEASURED,
            DEVIATION,
            DEVIATING,
            &parameters,
        ) {
            Err(ParameterError::Missing { parameter, .. }) => {
                assert_eq!(parameter, "window_ticks")
            }
            other => panic!("expected Missing window_ticks, got {other:?}"),
        }
        parameters.insert("window_ticks".to_string(), Value::Int(4));
        DeviationMonitor::from_parameters(
            "dev",
            EXPECTED,
            MEASURED,
            DEVIATION,
            DEVIATING,
            &parameters,
        )
        .unwrap();
        parameters.insert("window_ticks".to_string(), Value::Int(0));
        match DeviationMonitor::from_parameters(
            "dev",
            EXPECTED,
            MEASURED,
            DEVIATION,
            DEVIATING,
            &parameters,
        ) {
            Err(ParameterError::Invalid { parameter, .. }) => {
                assert_eq!(parameter, "window_ticks")
            }
            other => panic!("expected Invalid window_ticks, got {other:?}"),
        }
        parameters.insert("deviation_limit".to_string(), Value::Bool(true));
        match DeviationMonitor::from_parameters(
            "dev",
            EXPECTED,
            MEASURED,
            DEVIATION,
            DEVIATING,
            &parameters,
        ) {
            Err(ParameterError::Invalid { parameter, .. }) => {
                assert_eq!(parameter, "deviation_limit")
            }
            other => panic!("expected Invalid deviation_limit, got {other:?}"),
        }
    }

    #[test]
    fn tuning_retunes_the_declared_parameters() {
        let mut block = component();
        block
            .apply_parameter("deviation_limit", Value::Float(0.25))
            .unwrap();
        block
            .apply_parameter("window_ticks", Value::Int(2))
            .unwrap();
        let reported = block.report_parameters();
        assert_eq!(reported.get("deviation_limit"), Some(Value::Float(0.25)));
        assert_eq!(reported.get("window_ticks"), Some(Value::Int(2)));

        // The retuned window closes on the second banked scan.
        let first_io = io();
        step(&mut block, &first_io, 10.0, 13.0, 1);
        step(&mut block, &first_io, 10.0, 13.0, 2);
        assert!(deviating_value(&first_io));
        assert_eq!(deviation(&first_io).value, Value::Float(0.3));

        // A shortened window below the open position closes at the
        // next banked scan.
        let mut block = component();
        let tuned_io = io();
        step(&mut block, &tuned_io, 10.0, 12.0, 1);
        step(&mut block, &tuned_io, 10.0, 12.0, 2);
        step(&mut block, &tuned_io, 10.0, 12.0, 3);
        block
            .apply_parameter("window_ticks", Value::Int(1))
            .unwrap();
        step(&mut block, &tuned_io, 10.0, 12.0, 4);
        assert!(deviating_value(&tuned_io));
        assert_eq!(deviation(&tuned_io).value, Value::Float(0.2));

        // Refusals name the tuned parameter and change nothing.
        assert!(matches!(
            block
                .apply_parameter("deviation_limit", Value::Float(-1.0))
                .unwrap_err(),
            CommandError::InvalidParameter { ref parameter, .. } if parameter == "deviation_limit"
        ));
        assert!(matches!(
            block.apply_parameter("window_ticks", Value::Int(0)).unwrap_err(),
            CommandError::InvalidParameter { ref parameter, .. } if parameter == "window_ticks"
        ));
        assert!(matches!(
            block.apply_parameter("gain", Value::Float(1.0)).unwrap_err(),
            CommandError::UnknownParameter { ref parameter, .. } if parameter == "gain"
        ));
        assert!(matches!(
            block
                .apply_parameter("deviation_limit", Value::Bool(true))
                .unwrap_err(),
            CommandError::ParameterTypeMismatch { ref parameter, .. } if parameter == "deviation_limit"
        ));
    }

    #[test]
    fn identical_runs_produce_identical_outputs() {
        let run = || {
            let mut block = component();
            let io = io();
            let mut trace = Vec::new();
            for tick in 1..=10u64 {
                match tick {
                    5 | 6 => {
                        io.feed(
                            EXPECTED,
                            Sample::new(
                                Value::Float(10.0),
                                Quality::Bad(QualityReason::Stale),
                                Tick(tick),
                            ),
                        );
                        io.feed(MEASURED, Sample::good(Value::Float(9.0), Tick(tick)));
                        block.step(&io, Tick(tick)).unwrap();
                    }
                    _ => step(
                        &mut block,
                        &io,
                        10.0,
                        if tick < 9 { 10.0 } else { 15.0 },
                        tick,
                    ),
                }
                trace.push((deviation(&io), deviating(&io)));
            }
            trace
        };
        assert_eq!(run(), run());
    }

    #[test]
    fn describes_itself() {
        let descriptor = component().describe();
        assert_eq!(descriptor.name, "dev");
        assert_eq!(descriptor.kind, DeviationMonitor::KIND);
        assert_eq!(descriptor.label, "dev");
        assert_eq!(
            descriptor.ports,
            [
                PortDescriptor {
                    name: "expected".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: Some(PortRole::ProcessValue),
                    point: None,
                },
                PortDescriptor {
                    name: "measured".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: Some(PortRole::ProcessValue),
                    point: None,
                },
                PortDescriptor {
                    name: "deviation".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Float,
                    role: Some(PortRole::Output),
                    point: None,
                },
                PortDescriptor {
                    name: "deviating".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Status),
                    point: None,
                },
            ]
        );
        // The declared parameter set is exactly the key set
        // `from_parameters` reads, in the documented order.
        assert_eq!(
            descriptor.parameters,
            [
                ParameterDescriptor {
                    name: "deviation_limit".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::NONNEGATIVE_F64),
                },
                ParameterDescriptor {
                    name: "window_ticks".to_string(),
                    kind: ValueKind::Int,
                    range: Some(describe::POSITIVE_INT),
                },
            ]
        );
    }
}
