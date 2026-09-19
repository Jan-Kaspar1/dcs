//! Rate of rise: the one-sided derivative annunciation the IJmuiden
//! composition's contract gap records — the filtered level against a
//! slower `signal-filter` trend flags fast excursions in *either*
//! direction, so the rapidly falling level after the protective draw
//! reads as a "rise". A dedicated per-scan first-difference kind
//! asserts only while the input's rise meets the declared bound.

use crate::describe;
use crate::params::{self, ParameterError, Parameters};
use dcs_core::{
    CommandError, ComponentDescriptor, PointId, PortRole, Quality, QualityReason, Sample,
    StateError, StateMap, Tick, Value, ValueKind,
};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// A rate-of-rise monitor: reads `in` (`In`, `Float`) — the measured
/// value — and drives `rate` (`Out`, `Float`), the per-scan first
/// difference in the input's declared measured-units-per-tick
/// (components own no wall clock — a scan is the unit), and `rising`
/// (`Out`, `Bool`), the standing condition a downstream
/// `bool-latching-alarm`/`managed-bool-latching-alarm` `in` consumes.
///
/// **The first difference.** Each scan a [`Quality::Good`] finite
/// input banks as the new previous sample; once two Good samples have
/// banked, `rate` reports `in − previous` — the signed per-scan
/// change, positive while the input rises, negative while it falls —
/// and `rising` asserts while `rate` reaches `rate_limit`: the
/// comparison is one-sided and inclusive, so a rising input asserts at
/// the bound while a falling excursion — however fast — stays silent.
/// Neither output latches: `rising` releases the first evaluated scan
/// whose rate falls below the bound, and a plant needing a held
/// annunciation wires the latching alarm kind the flag feeds — the
/// alarm rationalization, priority, and lifecycle live there, not
/// here. A difference overflowing `f64` saturates at `±f64::MAX`, the
/// largest expressible rate, so a saturating rise still trips any
/// finite `rate_limit`.
///
/// **The declared initial.** Until the first `Good` sample pair
/// completes a difference there is no computed rate: `rate` reports
/// the declared `initial_rate` and `rising` evaluates it against the
/// bound, so a plant choosing the alarm-until-proven posture declares
/// an initial at or above `rate_limit` while the usual silent start
/// declares `0.0` — the flag and the reported rate never contradict.
///
/// **Quality rule.** A scan whose `in` sample is not `Good` or not
/// finite banks nothing — the previous sample holds, so the next
/// `Good` sample differences against the last trusted reading and the
/// whole excursion across the gap reports as one per-tick rate — and
/// `rate`/`rising` hold their standing values stamped with the
/// input's quality, plus `Bad(DeviceFault)` for a non-finite reading
/// the point did not report. The freeze convention the sibling kinds
/// record — holding rather than deasserting — keeps a standing
/// excursion visible but marked untrusted instead of presenting as a
/// healthy clear.
///
/// The kind is deliberately one-sided: whether a declared `direction`
/// parameter or a `rate-of-fall` sibling covers the falling case is
/// the implementing ticket's recorded parameterization — the
/// divergence monitor this kind succeeds flagged both directions,
/// which is the mis-annunciation being closed.
///
/// Declared I/O: `in` (`In`, `Float`), `rate` (`Out`, `Float`),
/// `rising` (`Out`, `Bool`).
///
/// Parameters: `rate_limit` — required finite `Float`, strictly
/// positive, the per-tick rise `rising` asserts at — and
/// `initial_rate` — required finite `Float`, the rate reported until
/// the first `Good` pair completes a difference. Both tune through
/// `set_parameter`; retuning `initial_rate` while no pair has banked
/// re-bases the reported rate, while after the first pair it tunes a
/// value the instance no longer reads.
#[derive(Debug)]
pub struct RateOfRise {
    name: String,
    input: PointId,
    rate_out: PointId,
    rising_out: PointId,
    /// The per-tick rise `rising` asserts at — strictly positive.
    rate_limit: f64,
    /// The rate reported until the first `Good` pair completes a
    /// difference.
    initial_rate: f64,
    /// The last banked `Good` finite sample; `None` until the first.
    previous: Option<f64>,
    /// The standing reported rate — `initial_rate` until the first
    /// computed difference.
    rate: f64,
    /// The standing flag — evaluated on every trusted scan, held
    /// through frozen ones.
    rising: bool,
}

/// The per-scan first difference `in − previous`, saturated to
/// `±f64::MAX` where the finite operands overflow the type — the same
/// saturation `deviation-monitor` records for a quotient with no
/// finite value.
fn difference(input: f64, previous: f64) -> f64 {
    let difference = input - previous;
    if difference.is_finite() {
        difference
    } else {
        difference.signum() * f64::MAX
    }
}

impl RateOfRise {
    /// The component-kind string the model-driven registry maps onto
    /// this type's constructor.
    pub const KIND: &'static str = "rate-of-rise";

    /// Builds the component from explicit points, the asserted rate
    /// bound, and the declared initial rate.
    ///
    /// `rate_limit` must be finite and strictly positive;
    /// `initial_rate` must be finite. Violations are reported as a
    /// [`ParameterError`] naming the parameter.
    pub fn new(
        name: impl Into<String>,
        input: PointId,
        rate: PointId,
        rising: PointId,
        rate_limit: f64,
        initial_rate: f64,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        Self::check_config(&name, rate_limit, initial_rate)?;
        Ok(Self {
            name,
            input,
            rate_out: rate,
            rising_out: rising,
            rate_limit,
            initial_rate,
            previous: None,
            rate: initial_rate,
            rising: initial_rate >= rate_limit,
        })
    }

    /// The parameter invariants every construction and tuning path
    /// enforces, reported as a [`ParameterError`] naming `component`
    /// and the offending parameter.
    fn check_config(
        component: &str,
        rate_limit: f64,
        initial_rate: f64,
    ) -> Result<(), ParameterError> {
        if !rate_limit.is_finite() {
            return Err(params::invalid(
                component,
                "rate_limit",
                "must be finite".to_string(),
            ));
        }
        if rate_limit <= 0.0 {
            return Err(params::invalid(
                component,
                "rate_limit",
                "must be positive".to_string(),
            ));
        }
        if !initial_rate.is_finite() {
            return Err(params::invalid(
                component,
                "initial_rate",
                "must be finite".to_string(),
            ));
        }
        Ok(())
    }

    /// Builds the component from a plant-model parameter map, reading
    /// the parameters listed on the type's docs.
    pub fn from_parameters(
        name: impl Into<String>,
        input: PointId,
        rate: PointId,
        rising: PointId,
        parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        let rate_limit = params::required_f64(&name, parameters, "rate_limit")?;
        let initial_rate = params::required_f64(&name, parameters, "initial_rate")?;
        Self::new(name, input, rate, rising, rate_limit, initial_rate)
    }

    /// Retunes one declared parameter, or reports a [`CommandError`]
    /// naming `component` and the offending parameter. A refused value
    /// changes nothing. Retuning `initial_rate` while no `Good` pair
    /// has banked re-bases the reported `rate` — the standing rate is
    /// the initial in that phase; once a pair has banked the tuned
    /// initial is declared data the instance no longer reads.
    fn tune(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        match parameter {
            "rate_limit" => {
                let tuned = params::tune_f64(&self.name, parameter, value)?;
                if !tuned.is_finite() || tuned <= 0.0 {
                    return Err(params::invalid_parameter(
                        &self.name,
                        parameter,
                        "must be finite and positive",
                    ));
                }
                self.rate_limit = tuned;
            }
            "initial_rate" => {
                let tuned = params::tune_f64(&self.name, parameter, value)?;
                if !tuned.is_finite() {
                    return Err(params::invalid_parameter(
                        &self.name,
                        parameter,
                        "must be finite",
                    ));
                }
                self.initial_rate = tuned;
                if self.previous.is_none() {
                    self.rate = tuned;
                }
            }
            _ => return Err(params::unknown_parameter(&self.name, parameter)),
        }
        Ok(())
    }
}

impl Component for RateOfRise {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        vec![
            IoRequirement::input::<f64>("in", self.input),
            IoRequirement::output::<f64>("rate", self.rate_out),
            IoRequirement::output::<bool>("rising", self.rising_out),
        ]
    }

    fn step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError> {
        let input = io.read_typed::<f64>(self.input)?;

        // Both outputs carry the input's quality, and a non-finite
        // reading is a device fault the point did not report — the
        // rate is only as trusted as the signal it differences.
        let mut quality = input.quality;
        if !input.value.is_finite() {
            quality = quality.merge(Quality::Bad(QualityReason::DeviceFault));
        }

        if input.quality.is_good() && input.value.is_finite() {
            if let Some(previous) = self.previous {
                self.rate = difference(input.value, previous);
            }
            self.previous = Some(input.value);
            self.rising = self.rate >= self.rate_limit;
        }
        // An untrusted scan freezes: the previous sample, the standing
        // rate, and the flag all hold, stamped with the input's
        // quality — the recovery scan differences against the last
        // banked sample, so the excursion across the gap reports as
        // one per-tick rate.

        io.write_sample(
            self.rate_out,
            Sample::new(Value::Float(self.rate), quality, tick),
        )?;
        io.write_sample(
            self.rising_out,
            Sample::new(Value::Bool(self.rising), quality, tick),
        )?;
        Ok(())
    }

    /// Describes the monitor: `in` is the process value it
    /// differences, `rate` the reported per-scan change, `rising` the
    /// standing condition the alarm set consumes; the declared
    /// parameter set is the two keys `from_parameters` reads.
    fn describe(&self) -> ComponentDescriptor {
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &[
                ("in", PortRole::ProcessValue),
                ("rate", PortRole::Output),
                ("rising", PortRole::Status),
            ],
            vec![
                describe::parameter("rate_limit", ValueKind::Float, Some(describe::POSITIVE_F64)),
                describe::parameter("initial_rate", ValueKind::Float, Some(describe::FINITE_F64)),
            ],
        )
    }

    /// Tunes the declared parameters at the scan boundary — the
    /// recorded mechanism for operator-adjustable limits. Retuning
    /// `rate_limit` applies to the next evaluated scan; retuning
    /// `initial_rate` re-bases the reported rate while no pair has
    /// banked. A refused value changes nothing.
    fn apply_parameter(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        self.tune(parameter, value)
    }

    /// Reports the tuned `rate_limit`/`initial_rate` — the same fields
    /// [`capture_state`](Self::capture_state) checkpoints, so the
    /// faceplate and a tracking standby read one vocabulary.
    fn report_parameters(&self) -> StateMap {
        let mut parameters = StateMap::new();
        parameters.insert("rate_limit", Value::Float(self.rate_limit));
        parameters.insert("initial_rate", Value::Float(self.initial_rate));
        parameters
    }

    /// Captures the tuned parameters, the banked previous sample, and
    /// the standing rate and flag — the state the first difference
    /// needs — so a checkpointed standby continues without a spurious
    /// edge: the restored `previous` differences the next sample
    /// exactly as the active's would.
    fn capture_state(&self) -> StateMap {
        let mut state = self.report_parameters();
        if let Some(previous) = self.previous {
            state.insert("previous", Value::Float(previous));
        }
        state.insert("rate", Value::Float(self.rate));
        state.insert("rising", Value::Bool(self.rising));
        state
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        state.ensure_known_fields(
            &self.name,
            &["rate_limit", "initial_rate", "previous", "rate", "rising"],
        )?;

        // The whole map is validated before any state mutates, so a
        // rejected checkpoint leaves the component on its last-good
        // state rather than half-applied.
        let rate_limit = state.require_f64(&self.name, "rate_limit")?;
        let initial_rate = state.require_f64(&self.name, "initial_rate")?;
        let previous = state.optional_f64(&self.name, "previous")?;
        let rate = state.require_f64(&self.name, "rate")?;
        let rising = state.require_bool(&self.name, "rising")?;

        // The same invariants `new` and `apply_parameter` enforce.
        if !rate_limit.is_finite() || rate_limit <= 0.0 {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "rate_limit".to_string(),
                value: Value::Float(rate_limit),
            });
        }
        if !initial_rate.is_finite() {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "initial_rate".to_string(),
                value: Value::Float(initial_rate),
            });
        }
        for (field, value) in [("previous", previous), ("rate", Some(rate))] {
            if let Some(value) = value
                && !value.is_finite()
            {
                return Err(StateError::InvalidValue {
                    element: self.name.clone(),
                    field: field.to_string(),
                    value: Value::Float(value),
                });
            }
        }
        // An unbanked previous marks the pre-first-pair phase: its
        // reported rate is the declared initial in every state this
        // kind captures — a retuned `initial_rate` re-bases it — so a
        // map carrying another rate cannot have come from the kind.
        if previous.is_none() && rate != initial_rate {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "rate".to_string(),
                value: Value::Float(rate),
            });
        }

        self.rate_limit = rate_limit;
        self.initial_rate = initial_rate;
        self.previous = previous;
        self.rate = rate;
        self.rising = rising;
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

    const IN: PointId = PointId(150);
    const RATE: PointId = PointId(151);
    const RISING: PointId = PointId(152);

    /// Limit 0.5 per tick, silent initial.
    fn component() -> RateOfRise {
        RateOfRise::new("ror", IN, RATE, RISING, 0.5, 0.0).unwrap()
    }

    fn io(field: Sample) -> TestIo {
        TestIo::new(&[
            (IN, Direction::In, field),
            (
                RATE,
                Direction::Out,
                Sample::good(Value::Float(0.0), Tick::ZERO),
            ),
            (
                RISING,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
        ])
    }

    fn feed(io: &TestIo, value: f64, tick: u64) {
        io.feed(IN, Sample::good(Value::Float(value), Tick(tick)));
    }

    /// Feeds `in` `Good` and steps once.
    fn step(block: &mut RateOfRise, io: &TestIo, value: f64, tick: u64) {
        feed(io, value, tick);
        block.step(io, Tick(tick)).unwrap();
    }

    fn rate(io: &TestIo) -> Sample {
        io.written(RATE).unwrap()
    }

    fn rising(io: &TestIo) -> Sample {
        io.written(RISING).unwrap()
    }

    fn rising_value(io: &TestIo) -> bool {
        match rising(io).value {
            Value::Bool(rising) => rising,
            value => panic!("rising must be Bool, got {value:?}"),
        }
    }

    #[test]
    fn the_first_good_pair_computes_the_per_scan_difference() {
        let mut block = component();
        let io = io(Sample::good(Value::Float(5.0), Tick::ZERO));

        // The first Good sample banks with no pair to difference:
        // rate reports the declared initial and the flag evaluates it.
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(rate(&io).value, Value::Float(0.0));
        assert!(!rising_value(&io));

        // Thereafter each scan reports `in − previous`: +0.25 per tick.
        for (tick, input, expected) in [
            (2, 5.25, 0.25),
            (3, 5.5, 0.25),
            (4, 5.0, -0.5),
            (5, 5.0, 0.0),
        ] {
            step(&mut block, &io, input, tick);
            assert_eq!(rate(&io).value, Value::Float(expected), "tick={tick}");
        }
    }

    #[test]
    fn the_flag_asserts_at_the_bound_and_releases_below_it() {
        let mut block = component();
        let io = io(Sample::good(Value::Float(5.0), Tick::ZERO));
        block.step(&io, Tick(1)).unwrap();

        // +0.75 per tick meets the 0.5 bound — the flag asserts.
        step(&mut block, &io, 5.75, 2);
        assert_eq!(rate(&io).value, Value::Float(0.75));
        assert!(rising_value(&io));

        // +0.25 releases the first evaluated scan below the bound — no
        // latch, the alarm kind owns that.
        step(&mut block, &io, 6.0, 3);
        assert!(!rising_value(&io));

        // A rate resting exactly on the bound meets it — the
        // comparison is inclusive.
        step(&mut block, &io, 6.5, 4);
        assert_eq!(rate(&io).value, Value::Float(0.5));
        assert!(rising_value(&io));
    }

    #[test]
    fn falling_excursions_never_assert() {
        let mut block = component();
        let io = io(Sample::good(Value::Float(9.0), Tick::ZERO));
        block.step(&io, Tick(1)).unwrap();

        // The one-sided gap the composed divergence monitor cannot
        // express: a fall of any speed reports its signed rate and
        // stays silent.
        for (tick, input, expected) in [(2, 6.0, -3.0), (3, 1.0, -5.0), (4, -100.0, -101.0)] {
            step(&mut block, &io, input, tick);
            assert_eq!(rate(&io).value, Value::Float(expected), "tick={tick}");
            assert!(!rising_value(&io), "tick={tick}");
        }
    }

    #[test]
    fn the_declared_initial_covers_reads_before_the_first_pair() {
        // An initial at the bound asserts from the first scan — the
        // alarm-until-proven posture the declaration chooses.
        let mut block = RateOfRise::new("ror", IN, RATE, RISING, 0.5, 2.0).unwrap();
        let good_io = io(Sample::good(Value::Float(5.0), Tick::ZERO));
        block.step(&good_io, Tick(1)).unwrap();
        assert_eq!(rate(&good_io).value, Value::Float(2.0));
        assert!(rising_value(&good_io));
        // The first completed pair replaces the declared cover.
        step(&mut block, &good_io, 5.0, 2);
        assert_eq!(rate(&good_io).value, Value::Float(0.0));
        assert!(!rising_value(&good_io));

        // Before any Good sample an untrusted input still surfaces the
        // declared initial stamped with its quality.
        let mut block = RateOfRise::new("ror", IN, RATE, RISING, 0.5, 1.0).unwrap();
        let bad_io = io(Sample::new(
            Value::Float(9.0),
            Quality::Bad(QualityReason::DeviceFault),
            Tick::ZERO,
        ));
        block.step(&bad_io, Tick(1)).unwrap();
        assert_eq!(rate(&bad_io).value, Value::Float(1.0));
        assert!(rising_value(&bad_io));
        assert_eq!(
            rate(&bad_io).quality,
            Quality::Bad(QualityReason::DeviceFault)
        );
        // The untrusted sample banked nothing: the first Good pair
        // still computes the first difference.
        step(&mut block, &bad_io, 9.0, 2);
        assert_eq!(rate(&bad_io).value, Value::Float(1.0));
        step(&mut block, &bad_io, 9.5, 3);
        assert_eq!(rate(&bad_io).value, Value::Float(0.5));
    }

    #[test]
    fn a_non_good_input_freezes_and_the_recovery_spans_the_gap() {
        let mut block = component();
        let io = io(Sample::good(Value::Float(5.0), Tick::ZERO));
        step(&mut block, &io, 5.5, 1);
        step(&mut block, &io, 6.0, 2);
        assert!(rising_value(&io));

        // Bad and Uncertain scans hold the standing rate and flag
        // stamped with the input's quality — a standing excursion
        // stays visible, marked untrusted, rather than reading as a
        // healthy clear.
        io.feed(
            IN,
            Sample::new(
                Value::Float(20.0),
                Quality::Bad(QualityReason::CommunicationFault),
                Tick(3),
            ),
        );
        block.step(&io, Tick(3)).unwrap();
        assert_eq!(rate(&io).value, Value::Float(0.5));
        assert!(rising_value(&io));
        assert_eq!(
            rising(&io).quality,
            Quality::Bad(QualityReason::CommunicationFault)
        );

        io.feed(
            IN,
            Sample::new(
                Value::Float(20.0),
                Quality::Uncertain(QualityReason::Stale),
                Tick(4),
            ),
        );
        block.step(&io, Tick(4)).unwrap();
        assert_eq!(rate(&io).value, Value::Float(0.5));
        assert_eq!(rate(&io).quality, Quality::Uncertain(QualityReason::Stale));

        // Recovery differences against the last banked sample: the
        // excursion across the frozen gap reports as one per-tick
        // rate — 6.75 − 6.0, not a per-gap average.
        step(&mut block, &io, 6.75, 5);
        assert_eq!(rate(&io).value, Value::Float(0.75));
        assert!(rising_value(&io));
        assert_eq!(rate(&io).quality, Quality::Good);
    }

    #[test]
    fn a_non_finite_input_freezes_flagged_device_fault() {
        let mut block = component();
        let io = io(Sample::good(Value::Float(5.0), Tick::ZERO));
        step(&mut block, &io, 5.0, 1);
        step(&mut block, &io, 5.5, 2);
        assert_eq!(rate(&io).value, Value::Float(0.5));
        io.feed(IN, Sample::good(Value::Float(f64::NAN), Tick(3)));
        block.step(&io, Tick(3)).unwrap();
        assert_eq!(rate(&io).value, Value::Float(0.5));
        assert_eq!(rate(&io).quality, Quality::Bad(QualityReason::DeviceFault));
        // A `Good`-stamped NaN banks nothing: the next finite sample
        // still differences against 5.5.
        step(&mut block, &io, 6.5, 4);
        assert_eq!(rate(&io).value, Value::Float(1.0));
    }

    #[test]
    fn a_difference_overflowing_the_type_saturates() {
        let mut block = component();
        let io = io(Sample::good(Value::Float(-f64::MAX), Tick::ZERO));
        block.step(&io, Tick(1)).unwrap();
        // MAX − (−MAX) overflows: the saturated rate still trips any
        // finite bound; the saturating fall stays silent.
        step(&mut block, &io, f64::MAX, 2);
        assert_eq!(rate(&io).value, Value::Float(f64::MAX));
        assert!(rising_value(&io));
        step(&mut block, &io, -f64::MAX, 3);
        assert_eq!(rate(&io).value, Value::Float(-f64::MAX));
        assert!(!rising_value(&io));
    }

    #[test]
    fn capture_restore_mid_run_continues_identically() {
        let mut block = component();
        let io_a = io(Sample::good(Value::Float(5.0), Tick::ZERO));
        let io_b = io(Sample::good(Value::Float(5.0), Tick::ZERO));

        // Bank a pair and assert, then tune the bound so the tuned
        // parameter rides the checkpoint too.
        step(&mut block, &io_a, 5.0, 1);
        step(&mut block, &io_a, 5.75, 2);
        block
            .apply_parameter("rate_limit", Value::Float(0.3))
            .unwrap();
        let state = block.capture_state();
        assert_eq!(state.get("previous"), Some(Value::Float(5.75)));
        assert_eq!(state.get("rate"), Some(Value::Float(0.75)));
        assert_eq!(state.get("rising"), Some(Value::Bool(true)));
        assert_eq!(state.get("rate_limit"), Some(Value::Float(0.3)));

        let mut standby = RateOfRise::new("ror", IN, RATE, RISING, 0.5, 0.0).unwrap();
        standby.restore_state(&state).unwrap();

        // Both continue identically — the restored previous differences
        // the next sample with no spurious edge — through the release.
        for (tick, input) in [(3, 6.0), (4, 6.0), (5, 4.0)] {
            step(&mut block, &io_a, input, tick);
            step(&mut standby, &io_b, input, tick);
            assert_eq!(rate(&io_a), rate(&io_b), "tick {tick}");
            assert_eq!(rising(&io_a), rising(&io_b), "tick {tick}");
        }
        assert!(!rising_value(&io_a));
        assert!(!rising_value(&io_b));
        assert_eq!(standby.capture_state(), block.capture_state());

        // An unbanked capture restores an unbanked monitor: `previous`
        // is absent while the tuned parameters and declared initial
        // ride along.
        let state = component().capture_state();
        assert_eq!(state.get("previous"), None);
        assert_eq!(state.get("rate"), Some(Value::Float(0.0)));
        let mut fresh = component();
        fresh.restore_state(&state).unwrap();
        let fresh_io = io(Sample::good(Value::Float(7.0), Tick::ZERO));
        fresh.step(&fresh_io, Tick(1)).unwrap();
        assert_eq!(rate(&fresh_io).value, Value::Float(0.0));
        step(&mut fresh, &fresh_io, 7.875, 2);
        assert_eq!(rate(&fresh_io).value, Value::Float(0.875));
    }

    #[test]
    fn the_state_map_serde_roundtrips() {
        let mut block = component();
        let io = io(Sample::good(Value::Float(5.0), Tick::ZERO));
        step(&mut block, &io, 5.0, 1);
        step(&mut block, &io, 5.75, 2);
        let state = block.capture_state();
        let json = serde_json::to_string(&state).unwrap();
        let restored: StateMap = serde_json::from_str(&json).unwrap();
        assert_eq!(restored, state);
        let mut standby = component();
        standby.restore_state(&restored).unwrap();
        assert_eq!(standby.capture_state(), state);
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

        // Parameter invariants apply to the restored vocabulary too.
        let mut bad = block.capture_state();
        bad.insert("rate_limit", Value::Float(0.0));
        assert_eq!(
            block.restore_state(&bad),
            Err(StateError::InvalidValue {
                element: "ror".to_string(),
                field: "rate_limit".to_string(),
                value: Value::Float(0.0),
            })
        );
        let mut bad = block.capture_state();
        bad.insert("initial_rate", Value::Float(f64::INFINITY));
        assert!(matches!(
            block.restore_state(&bad),
            Err(StateError::InvalidValue { ref field, .. }) if field == "initial_rate"
        ));
        let mut bad = block.capture_state();
        bad.insert("rate", Value::Float(f64::NAN));
        assert!(matches!(
            block.restore_state(&bad),
            Err(StateError::InvalidValue { ref field, .. }) if field == "rate"
        ));
        let mut bad = block.capture_state();
        bad.insert("previous", Value::Float(f64::NAN));
        assert!(matches!(
            block.restore_state(&bad),
            Err(StateError::InvalidValue { ref field, .. }) if field == "previous"
        ));
        // A pre-first-pair map whose rate is not the declared initial
        // cannot have come from the kind.
        let mut bad = block.capture_state();
        bad.insert("rate", Value::Float(1.0));
        assert_eq!(
            block.restore_state(&bad),
            Err(StateError::InvalidValue {
                element: "ror".to_string(),
                field: "rate".to_string(),
                value: Value::Float(1.0),
            })
        );
        let mut bad = block.capture_state();
        bad.insert("rising", Value::Int(1));
        assert!(matches!(
            block.restore_state(&bad),
            Err(StateError::IncompatibleField { ref field, .. }) if field == "rising"
        ));
    }

    #[test]
    fn malformed_parameters_fail_construction_naming_the_parameter() {
        for (limit, initial, parameter) in [
            (f64::NAN, 0.0, "rate_limit"),
            (f64::INFINITY, 0.0, "rate_limit"),
            (0.0, 0.0, "rate_limit"),
            (-0.1, 0.0, "rate_limit"),
            (0.5, f64::NAN, "initial_rate"),
            (0.5, -f64::INFINITY, "initial_rate"),
        ] {
            match RateOfRise::new("ror", IN, RATE, RISING, limit, initial) {
                Err(ParameterError::Invalid {
                    parameter: found, ..
                }) => assert_eq!(found, parameter),
                other => panic!("{parameter}: expected Invalid, got {other:?}"),
            }
        }

        // Through the parameter map: a missing key is Missing, a
        // mistyped one Invalid.
        let mut parameters = Parameters::new();
        parameters.insert("rate_limit".to_string(), Value::Float(0.5));
        match RateOfRise::from_parameters("ror", IN, RATE, RISING, &parameters) {
            Err(ParameterError::Missing { parameter, .. }) => {
                assert_eq!(parameter, "initial_rate")
            }
            other => panic!("expected Missing initial_rate, got {other:?}"),
        }
        parameters.insert("initial_rate".to_string(), Value::Float(0.0));
        RateOfRise::from_parameters("ror", IN, RATE, RISING, &parameters).unwrap();
        parameters.insert("rate_limit".to_string(), Value::Int(0));
        match RateOfRise::from_parameters("ror", IN, RATE, RISING, &parameters) {
            Err(ParameterError::Invalid { parameter, .. }) => {
                assert_eq!(parameter, "rate_limit")
            }
            other => panic!("expected Invalid rate_limit, got {other:?}"),
        }
        parameters.insert("rate_limit".to_string(), Value::Float(0.5));
        parameters.insert("initial_rate".to_string(), Value::Bool(true));
        match RateOfRise::from_parameters("ror", IN, RATE, RISING, &parameters) {
            Err(ParameterError::Invalid { parameter, .. }) => {
                assert_eq!(parameter, "initial_rate")
            }
            other => panic!("expected Invalid initial_rate, got {other:?}"),
        }
    }

    #[test]
    fn builds_from_parameter_map() {
        let parameters: Parameters = [
            ("rate_limit".to_string(), Value::Float(0.5)),
            ("initial_rate".to_string(), Value::Float(0.0)),
        ]
        .into_iter()
        .collect();
        let instance = ComponentInstance {
            id: ComponentId(13),
            kind: RateOfRise::KIND.to_string(),
            parameters,
            rationalization: None,
            ports: BTreeMap::new(),
        };
        let block =
            RateOfRise::from_parameters("ror", IN, RATE, RISING, &instance.parameters).unwrap();
        assert_eq!(block.rate_limit, 0.5);
        assert_eq!(block.initial_rate, 0.0);
        // A losslessly representable Int is accepted.
        let parameters: Parameters = [
            ("rate_limit".to_string(), Value::Int(1)),
            ("initial_rate".to_string(), Value::Int(0)),
        ]
        .into_iter()
        .collect();
        let block = RateOfRise::from_parameters("ror", IN, RATE, RISING, &parameters).unwrap();
        assert_eq!(block.rate_limit, 1.0);
    }

    #[test]
    fn tuning_retunes_the_declared_parameters() {
        let mut block = component();
        block
            .apply_parameter("rate_limit", Value::Float(0.25))
            .unwrap();
        block
            .apply_parameter("initial_rate", Value::Float(0.1))
            .unwrap();
        let reported = block.report_parameters();
        assert_eq!(reported.get("rate_limit"), Some(Value::Float(0.25)));
        assert_eq!(reported.get("initial_rate"), Some(Value::Float(0.1)));

        // Before the first pair the retuned initial re-bases the
        // reported rate; the flag evaluates it on the next scan.
        let first_io = io(Sample::good(Value::Float(5.0), Tick::ZERO));
        block.step(&first_io, Tick(1)).unwrap();
        assert_eq!(rate(&first_io).value, Value::Float(0.1));
        assert!(!rising_value(&first_io));

        // After the first pair the tuned initial no longer feeds the
        // reported rate.
        step(&mut block, &first_io, 5.5, 2);
        block
            .apply_parameter("initial_rate", Value::Float(9.0))
            .unwrap();
        step(&mut block, &first_io, 6.0, 3);
        assert_eq!(rate(&first_io).value, Value::Float(0.5));

        // Refusals name the tuned parameter and change nothing.
        assert!(matches!(
            block
                .apply_parameter("rate_limit", Value::Float(-1.0))
                .unwrap_err(),
            CommandError::InvalidParameter { ref parameter, .. } if parameter == "rate_limit"
        ));
        assert!(matches!(
            block
                .apply_parameter("rate_limit", Value::Float(0.0))
                .unwrap_err(),
            CommandError::InvalidParameter { ref parameter, .. } if parameter == "rate_limit"
        ));
        assert!(matches!(
            block
                .apply_parameter("initial_rate", Value::Float(f64::NAN))
                .unwrap_err(),
            CommandError::InvalidParameter { ref parameter, .. } if parameter == "initial_rate"
        ));
        assert!(matches!(
            block.apply_parameter("gain", Value::Float(1.0)).unwrap_err(),
            CommandError::UnknownParameter { ref parameter, .. } if parameter == "gain"
        ));
        assert!(matches!(
            block
                .apply_parameter("rate_limit", Value::Bool(true))
                .unwrap_err(),
            CommandError::ParameterTypeMismatch { ref parameter, .. } if parameter == "rate_limit"
        ));
    }

    #[test]
    fn identical_runs_produce_identical_outputs() {
        let run = || {
            let mut block = component();
            let io = io(Sample::good(Value::Float(5.0), Tick::ZERO));
            let mut trace = Vec::new();
            for tick in 1..=10u64 {
                match tick {
                    5 | 6 => {
                        io.feed(
                            IN,
                            Sample::new(
                                Value::Float(6.0),
                                Quality::Bad(QualityReason::Stale),
                                Tick(tick),
                            ),
                        );
                        block.step(&io, Tick(tick)).unwrap();
                    }
                    _ => step(
                        &mut block,
                        &io,
                        match tick {
                            3 | 4 => 6.0,
                            8 => 1.0,
                            _ => 5.0,
                        },
                        tick,
                    ),
                }
                trace.push((rate(&io), rising(&io)));
            }
            trace
        };
        assert_eq!(run(), run());
    }

    #[test]
    fn describes_itself() {
        let descriptor = component().describe();
        assert_eq!(descriptor.name, "ror");
        assert_eq!(descriptor.kind, RateOfRise::KIND);
        assert_eq!(descriptor.label, "ror");
        assert_eq!(
            descriptor.ports,
            [
                PortDescriptor {
                    name: "in".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: Some(PortRole::ProcessValue),
                    point: None,
                },
                PortDescriptor {
                    name: "rate".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Float,
                    role: Some(PortRole::Output),
                    point: None,
                },
                PortDescriptor {
                    name: "rising".to_string(),
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
                    name: "rate_limit".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::POSITIVE_F64),
                },
                ParameterDescriptor {
                    name: "initial_rate".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::FINITE_F64),
                },
            ]
        );
    }
}
