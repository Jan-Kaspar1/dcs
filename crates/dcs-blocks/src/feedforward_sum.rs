//! Feedforward sum: the additive trim contract architecture decision
//! 66 records for `WW-CTL-005`
//! (`docs/research/aeration-do-control.md`) — the bounded `ff` + `trim`
//! sum on a zone's aeration demand path, the sibling of the landed
//! `flow-paced-ratio` whose `trim` scales *multiplicatively* where this
//! kind's adds: a computed feed-forward demand summed with the feedback
//! loop's correction.

use crate::describe;
use crate::params::{self, ParameterError, Parameters};
use dcs_core::{
    CommandError, ComponentDescriptor, ParameterRange, PointId, PortRole, Quality, QualityReason,
    Sample, StateError, StateMap, Tick, Value, ValueKind,
};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// What an untrusted input term contributes to the sum.
///
/// The model's parameter vocabulary has no string type, so the
/// `on_bad_ff`/`on_bad_trim` parameters carry the choice as an `Int`
/// code: `0` for [`Drop`](Self::Drop), `1` for [`Hold`](Self::Hold) —
/// the values [`code`](Self::code) reports and
/// [`decode`](Self::decode) accepts. The alternatives are the
/// decision's hold-versus-serve-other-term pair; which response a
/// plant declares for each input is customer-validation data, never a
/// kind default.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BadTermResponse {
    /// The untrusted term drops out — contributes `0.0`, the additive
    /// identity, so the other term serves the demand alone. For `ff`
    /// that is feedback-only service; for `trim` it is untrimmed
    /// feed-forward service.
    Drop,
    /// The term's last `Good` finite value stands in — `0.0` before
    /// the first one arrives.
    Hold,
}

impl BadTermResponse {
    /// The `Int` code the `on_bad_ff`/`on_bad_trim` parameters carry.
    pub const fn code(self) -> i64 {
        match self {
            Self::Drop => 0,
            Self::Hold => 1,
        }
    }

    /// The response the `Int` code `code` selects, or `None` when the
    /// code declares no response.
    pub const fn decode(code: i64) -> Option<Self> {
        match code {
            0 => Some(Self::Drop),
            1 => Some(Self::Hold),
            _ => None,
        }
    }
}

/// The declared bounds and bad-signal responses a [`FeedforwardSum`]
/// runs under — its parameter map's six keys as one value.
///
/// `trim_min`/`trim_max` bound the trim's authority — the feedback
/// portion trims, never owns, the demand; `min_demand`/`max_demand`
/// bound the emitted `out`. All four must be finite, with
/// `trim_min <= trim_max` and `min_demand <= max_demand`. `on_bad_ff`
/// and `on_bad_trim` name each input's declared response to a
/// non-`Good` or non-finite sample.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct FeedforwardSumConfig {
    /// The trim term's lower bound — the most the feedback correction
    /// may subtract from the feed-forward demand.
    pub trim_min: f64,
    /// The trim term's upper bound.
    pub trim_max: f64,
    /// The emitted demand's lower bound.
    pub min_demand: f64,
    /// The emitted demand's upper bound.
    pub max_demand: f64,
    /// The response to an untrusted `ff`: `0` the feed-forward drops
    /// out and the trim serves alone, `1` hold the last `Good` finite
    /// `ff`.
    pub on_bad_ff: BadTermResponse,
    /// The response to an untrusted `trim`: `0` the trim drops out and
    /// `ff` serves untrimmed, `1` hold the last `Good` finite `trim`.
    pub on_bad_trim: BadTermResponse,
}

/// The points a [`FeedforwardSum`] binds — the two demand terms it
/// sums (`In`), and the bounded demand plus the bound and engagement
/// reports it drives (`Out`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FeedforwardSumIo {
    /// `ff` (`In`, `Float`): the computed feed-forward demand —
    /// conventionally a `flow-paced-ratio` with `trim` unwired where
    /// the pacing law is proportional, or any declared load estimate.
    pub ff: PointId,
    /// `trim` (`In`, `Float`): the feedback loop's additive
    /// correction — typically the zone DO `pid`'s `out`.
    pub trim: PointId,
    /// `out` (`Out`, `Float`): the summed, bounded demand feeding the
    /// zone's demand path.
    pub out: PointId,
    /// `clamped` (`Out`, `Bool`): asserted while either declared bound
    /// engages — the trim saturating at its authority or the sum at
    /// the demand bounds.
    pub clamped: PointId,
    /// `fallback_active` (`Out`, `Bool`): asserted while a declared
    /// bad-signal response runs — the engagement flag the alarm set
    /// consumes.
    pub fallback_active: PointId,
}

/// The inclusive `Int` bound the `on_bad_ff`/`on_bad_trim` parameters
/// accept: the declared [`BadTermResponse`] codes `0` (drop) and `1`
/// (hold).
const BAD_TERM_RANGE: ParameterRange = ParameterRange {
    min: Value::Int(0),
    max: Value::Int(1),
};

/// A feedforward sum: reads `ff` (`In`, `Float`) — the computed
/// feed-forward demand — and `trim` (`In`, `Float`) — the feedback
/// loop's additive correction — and drives `out` (`Out`, `Float`),
/// `clamped` (`Out`, `Bool`), and `fallback_active` (`Out`, `Bool`).
///
/// **The sum.** Each scan the kind computes `ff + clamp(trim,
/// trim_min, trim_max)` and clamps the result to `[min_demand,
/// max_demand]` — the trim is bounded *before* the sum, so the
/// feedback portion's authority is its declared range. `clamped`
/// asserts while either bound engages — a trim beyond its authority
/// saturates rather than passing, and a sum outside the demand bounds
/// saturates at the bound; a term resting exactly on its bound is not
/// a clamp.
///
/// **Untrusted inputs.** A sample that is not `Good` — or not finite,
/// the crate's `NaN`-as-device-fault convention — cannot serve: the
/// term takes its declared [`BadTermResponse`] and `fallback_active`
/// asserts for the engagement's duration. [`Drop`](BadTermResponse::Drop)
/// serves the other term alone — an untrusted `ff` leaves the bounded
/// trim as the whole demand, an untrusted `trim` leaves `ff`
/// untrimmed. [`Hold`](BadTermResponse::Hold) stands the term's last
/// `Good` finite value in — `0.0` before the first. The two inputs
/// respond independently: both bad composes both responses — `ff`
/// held beside `trim` held, or `ff` dropped beside `trim` dropped
/// serving `min_demand`-bounded zero. Neither state latches: the
/// first scan an input reads `Good` and finite it serves and banks
/// again. The served terms still pass through the same bounds, so
/// `clamped` reports honestly while a response runs.
///
/// **Quality.** `out` carries the merged worst of the `ff` and `trim`
/// qualities plus `Bad(DeviceFault)` for a non-finite reading the
/// point did not report, so a demand served under a response stays
/// marked untrusted. `clamped` and `fallback_active` always carry
/// `Quality::Good`: each is the kind's own computed state.
///
/// Declared I/O: `ff`, `trim` (`In`, `Float`); `out` (`Out`,
/// `Float`), `clamped`, `fallback_active` (`Out`, `Bool`).
///
/// Parameters: `trim_min`, `trim_max`, `min_demand`, `max_demand` —
/// required finite `Float`s with `trim_min <= trim_max` and
/// `min_demand <= max_demand` — and `on_bad_ff`, `on_bad_trim` —
/// required `Int`s carrying [`BadTermResponse`] codes. All six tune
/// through `set_parameter`; a retune breaking a bound pair is refused
/// naming the parameter.
#[derive(Debug)]
pub struct FeedforwardSum {
    name: String,
    io: FeedforwardSumIo,
    config: FeedforwardSumConfig,
    /// The held last `Good` finite `ff` — `None` until the first
    /// arrives; `on_bad_ff` code `1` stands it in.
    last_ff: Option<f64>,
    /// The held last `Good` finite `trim` — `None` until the first
    /// arrives; `on_bad_trim` code `1` stands it in.
    last_trim: Option<f64>,
}

impl FeedforwardSumConfig {
    /// Validates the invariants every construction path enforces — all
    /// bounds finite, each bound pair ordered — reporting a violation
    /// as a [`ParameterError`] naming `component` and the offending
    /// parameter.
    pub(crate) fn checked(component: &str, config: Self) -> Result<Self, ParameterError> {
        for (parameter, value) in [
            ("trim_min", config.trim_min),
            ("trim_max", config.trim_max),
            ("min_demand", config.min_demand),
            ("max_demand", config.max_demand),
        ] {
            if !value.is_finite() {
                return Err(params::invalid(
                    component,
                    parameter,
                    "must be finite".to_string(),
                ));
            }
        }
        Self::check_order(component, &config)?;
        Ok(config)
    }

    /// The bound-pair half of [`checked`](Self::checked): each upper
    /// bound must be at least its lower, and the reported parameter is
    /// the one that failed that constraint.
    fn check_order(component: &str, config: &Self) -> Result<(), ParameterError> {
        if config.trim_min > config.trim_max {
            return Err(params::invalid(
                component,
                "trim_max",
                "must be at least trim_min".to_string(),
            ));
        }
        if config.min_demand > config.max_demand {
            return Err(params::invalid(
                component,
                "max_demand",
                "must be at least min_demand".to_string(),
            ));
        }
        Ok(())
    }
}

impl FeedforwardSum {
    /// The component-kind string the model-driven registry maps onto
    /// this type's constructor.
    pub const KIND: &'static str = "feedforward-sum";

    /// Builds the component from explicit points and the declared
    /// config, or reports an inconsistent one as a [`ParameterError`].
    pub fn new(
        name: impl Into<String>,
        io: FeedforwardSumIo,
        config: FeedforwardSumConfig,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        Ok(Self {
            config: FeedforwardSumConfig::checked(&name, config)?,
            name,
            io,
            last_ff: None,
            last_trim: None,
        })
    }

    /// The declared config — the reported parameter set.
    pub fn config(&self) -> FeedforwardSumConfig {
        self.config
    }

    /// Builds the component from a plant-model parameter map, reading
    /// the parameters listed on the type's docs.
    pub fn from_parameters(
        name: impl Into<String>,
        io: FeedforwardSumIo,
        parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        let config = FeedforwardSumConfig {
            trim_min: params::required_f64(&name, parameters, "trim_min")?,
            trim_max: params::required_f64(&name, parameters, "trim_max")?,
            min_demand: params::required_f64(&name, parameters, "min_demand")?,
            max_demand: params::required_f64(&name, parameters, "max_demand")?,
            on_bad_ff: {
                let code = params::required_u64(&name, parameters, "on_bad_ff")? as i64;
                BadTermResponse::decode(code).ok_or_else(|| {
                    params::invalid(
                        &name,
                        "on_bad_ff",
                        format!("expected 0 (drop) or 1 (hold), found {code}"),
                    )
                })?
            },
            on_bad_trim: {
                let code = params::required_u64(&name, parameters, "on_bad_trim")? as i64;
                BadTermResponse::decode(code).ok_or_else(|| {
                    params::invalid(
                        &name,
                        "on_bad_trim",
                        format!("expected 0 (drop) or 1 (hold), found {code}"),
                    )
                })?
            },
        };
        Self::new(name, io, config)
    }

    /// Retunes one declared parameter, or reports a [`CommandError`]
    /// naming `component` and the offending parameter. The bound-pair
    /// invariant is re-checked after a bound change; a refused value
    /// changes nothing.
    fn tune(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        let mut config = self.config;
        match parameter {
            "trim_min" | "trim_max" | "min_demand" | "max_demand" => {
                let tuned = params::tune_f64(&self.name, parameter, value)?;
                if !tuned.is_finite() {
                    return Err(params::invalid_parameter(
                        &self.name,
                        parameter,
                        "must be finite",
                    ));
                }
                match parameter {
                    "trim_min" => config.trim_min = tuned,
                    "trim_max" => config.trim_max = tuned,
                    "min_demand" => config.min_demand = tuned,
                    _ => config.max_demand = tuned,
                }
                FeedforwardSumConfig::check_order(&self.name, &config).map_err(|_| {
                    params::invalid_parameter(
                        &self.name,
                        parameter,
                        "bounds must keep trim_min <= trim_max and min_demand <= max_demand",
                    )
                })?;
            }
            "on_bad_ff" | "on_bad_trim" => {
                let tuned = params::tune_u64(&self.name, parameter, value)?;
                let response = BadTermResponse::decode(tuned as i64).ok_or_else(|| {
                    params::invalid_parameter(
                        &self.name,
                        parameter,
                        "expected 0 (drop) or 1 (hold)",
                    )
                })?;
                match parameter {
                    "on_bad_ff" => config.on_bad_ff = response,
                    _ => config.on_bad_trim = response,
                }
            }
            _ => return Err(params::unknown_parameter(&self.name, parameter)),
        }
        self.config = config;
        Ok(())
    }
}

impl Component for FeedforwardSum {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        vec![
            IoRequirement::input::<f64>("ff", self.io.ff),
            IoRequirement::input::<f64>("trim", self.io.trim),
            IoRequirement::output::<f64>("out", self.io.out),
            IoRequirement::output::<bool>("clamped", self.io.clamped),
            IoRequirement::output::<bool>("fallback_active", self.io.fallback_active),
        ]
    }

    fn step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError> {
        let ff = io.read_typed::<f64>(self.io.ff)?;
        let trim = io.read_typed::<f64>(self.io.trim)?;

        // `out` carries the worst of both inputs, and a non-finite
        // reading is a device fault the point did not report — so a
        // demand served under a response stays marked untrusted.
        let mut quality = ff.quality.merge(trim.quality);
        for sample in [ff, trim] {
            if !sample.value.is_finite() {
                quality = quality.merge(Quality::Bad(QualityReason::DeviceFault));
            }
        }

        // Each term serves while `Good` and finite — banking its value
        // for the hold response — and takes its declared response
        // otherwise: `0` drops the term to the additive identity, `1`
        // stands the last `Good` value in.
        let ff_ok = ff.quality.is_good() && ff.value.is_finite();
        if ff_ok {
            self.last_ff = Some(ff.value);
        }
        let served_ff = if ff_ok {
            ff.value
        } else {
            match self.config.on_bad_ff {
                BadTermResponse::Hold => self.last_ff.unwrap_or(0.0),
                BadTermResponse::Drop => 0.0,
            }
        };
        let trim_ok = trim.quality.is_good() && trim.value.is_finite();
        if trim_ok {
            self.last_trim = Some(trim.value);
        }
        let served_trim = if trim_ok {
            trim.value
        } else {
            match self.config.on_bad_trim {
                BadTermResponse::Hold => self.last_trim.unwrap_or(0.0),
                BadTermResponse::Drop => 0.0,
            }
        };

        // The trim is bounded before the sum — the feedback portion's
        // authority is its declared range — and the summed demand is
        // bounded after. `clamped` reports either bound engaging,
        // including while a response runs: the served terms still pass
        // through the same bounds.
        let bounded_trim = served_trim.clamp(self.config.trim_min, self.config.trim_max);
        let raw = served_ff + bounded_trim;
        let demand = raw.clamp(self.config.min_demand, self.config.max_demand);
        let clamped = bounded_trim != served_trim || demand != raw;

        io.write_sample(
            self.io.out,
            Sample::new(Value::Float(demand), quality, tick),
        )?;
        io.write_sample(self.io.clamped, Sample::good(Value::Bool(clamped), tick))?;
        io.write_sample(
            self.io.fallback_active,
            Sample::good(Value::Bool(!(ff_ok && trim_ok)), tick),
        )?;
        Ok(())
    }

    /// Describes the sum: `ff` and `trim` are the demand terms it acts
    /// on, `out` the driven demand, `clamped` and `fallback_active`
    /// the reported conditions. The declared parameter set is the six
    /// keys `from_parameters` reads.
    fn describe(&self) -> ComponentDescriptor {
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &[
                ("ff", PortRole::ProcessValue),
                ("trim", PortRole::ProcessValue),
                ("out", PortRole::Output),
                ("clamped", PortRole::Status),
                ("fallback_active", PortRole::Status),
            ],
            vec![
                describe::parameter("trim_min", ValueKind::Float, Some(describe::FINITE_F64)),
                describe::parameter("trim_max", ValueKind::Float, Some(describe::FINITE_F64)),
                describe::parameter("min_demand", ValueKind::Float, Some(describe::FINITE_F64)),
                describe::parameter("max_demand", ValueKind::Float, Some(describe::FINITE_F64)),
                describe::parameter("on_bad_ff", ValueKind::Int, Some(BAD_TERM_RANGE)),
                describe::parameter("on_bad_trim", ValueKind::Int, Some(BAD_TERM_RANGE)),
            ],
        )
    }

    /// Tunes the declared parameters at the scan boundary — the
    /// recorded mechanism for operator-adjustable bounds and
    /// bad-signal responses. A retune breaking a bound pair is refused
    /// naming the tuned parameter; the code parameters accept only
    /// their declared codes.
    fn apply_parameter(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        self.tune(parameter, value)
    }

    /// Reports the tuned config — the same fields
    /// [`capture_state`](Self::capture_state) checkpoints, so the
    /// faceplate and a tracking standby read one vocabulary.
    fn report_parameters(&self) -> StateMap {
        let mut parameters = StateMap::new();
        parameters.insert("trim_min", Value::Float(self.config.trim_min));
        parameters.insert("trim_max", Value::Float(self.config.trim_max));
        parameters.insert("min_demand", Value::Float(self.config.min_demand));
        parameters.insert("max_demand", Value::Float(self.config.max_demand));
        parameters.insert("on_bad_ff", Value::Int(self.config.on_bad_ff.code()));
        parameters.insert("on_bad_trim", Value::Int(self.config.on_bad_trim.code()));
        parameters
    }

    /// Captures the tuned config and the held last-good `ff` and
    /// `trim` — the state the hold responses need — so a checkpointed
    /// standby continues an engaged response identically. A held value
    /// never yet established captures as absent.
    fn capture_state(&self) -> StateMap {
        let mut state = self.report_parameters();
        if let Some(ff) = self.last_ff {
            state.insert("last_ff", Value::Float(ff));
        }
        if let Some(trim) = self.last_trim {
            state.insert("last_trim", Value::Float(trim));
        }
        state
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        state.ensure_known_fields(
            &self.name,
            &[
                "trim_min",
                "trim_max",
                "min_demand",
                "max_demand",
                "on_bad_ff",
                "on_bad_trim",
                "last_ff",
                "last_trim",
            ],
        )?;

        // The whole map is validated before any state mutates, so a
        // rejected checkpoint leaves the component on its last-good
        // state rather than half-applied.
        let config = FeedforwardSumConfig {
            trim_min: state.require_f64(&self.name, "trim_min")?,
            trim_max: state.require_f64(&self.name, "trim_max")?,
            min_demand: state.require_f64(&self.name, "min_demand")?,
            max_demand: state.require_f64(&self.name, "max_demand")?,
            on_bad_ff: {
                let code = state.require_i64(&self.name, "on_bad_ff")?;
                BadTermResponse::decode(code).ok_or_else(|| StateError::InvalidValue {
                    element: self.name.clone(),
                    field: "on_bad_ff".to_string(),
                    value: Value::Int(code),
                })?
            },
            on_bad_trim: {
                let code = state.require_i64(&self.name, "on_bad_trim")?;
                BadTermResponse::decode(code).ok_or_else(|| StateError::InvalidValue {
                    element: self.name.clone(),
                    field: "on_bad_trim".to_string(),
                    value: Value::Int(code),
                })?
            },
        };
        let last_ff = state.optional_f64(&self.name, "last_ff")?;
        let last_trim = state.optional_f64(&self.name, "last_trim")?;

        // The same invariants `new` and `apply_parameter` enforce.
        if let Err(ParameterError::Invalid { parameter, .. }) =
            FeedforwardSumConfig::checked(&self.name, config)
        {
            let value = match parameter.as_str() {
                "trim_min" => config.trim_min,
                "trim_max" => config.trim_max,
                "min_demand" => config.min_demand,
                _ => config.max_demand,
            };
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: parameter,
                value: Value::Float(value),
            });
        }
        for (field, value) in [("last_ff", last_ff), ("last_trim", last_trim)] {
            if let Some(held) = value
                && !held.is_finite()
            {
                return Err(StateError::InvalidValue {
                    element: self.name.clone(),
                    field: field.to_string(),
                    value: Value::Float(held),
                });
            }
        }

        self.config = config;
        self.last_ff = last_ff;
        self.last_trim = last_trim;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::TestIo;
    use dcs_core::{Direction, ParameterDescriptor, PortDescriptor};

    const FF: PointId = PointId(10);
    const TRIM: PointId = PointId(11);
    const OUT: PointId = PointId(20);
    const CLAMPED: PointId = PointId(21);
    const FALLBACK: PointId = PointId(22);

    fn points() -> FeedforwardSumIo {
        FeedforwardSumIo {
            ff: FF,
            trim: TRIM,
            out: OUT,
            clamped: CLAMPED,
            fallback_active: FALLBACK,
        }
    }

    fn config() -> FeedforwardSumConfig {
        FeedforwardSumConfig {
            trim_min: -10.0,
            trim_max: 10.0,
            min_demand: 0.0,
            max_demand: 100.0,
            on_bad_ff: BadTermResponse::Drop,
            on_bad_trim: BadTermResponse::Drop,
        }
    }

    fn component() -> FeedforwardSum {
        FeedforwardSum::new("ffs", points(), config()).unwrap()
    }

    fn io() -> TestIo {
        TestIo::new(&[
            (
                FF,
                Direction::In,
                Sample::good(Value::Float(60.0), Tick::ZERO),
            ),
            (
                TRIM,
                Direction::In,
                Sample::good(Value::Float(5.0), Tick::ZERO),
            ),
            (
                OUT,
                Direction::Out,
                Sample::good(Value::Float(0.0), Tick::ZERO),
            ),
            (
                CLAMPED,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
            (
                FALLBACK,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
        ])
    }

    fn feed(io: &TestIo, point: PointId, value: f64, quality: Quality) {
        io.feed(point, Sample::new(Value::Float(value), quality, Tick::ZERO));
    }

    fn out(io: &TestIo) -> Sample {
        io.written(OUT).unwrap()
    }

    fn clamped(io: &TestIo) -> bool {
        match io.written(CLAMPED).unwrap().value {
            Value::Bool(clamped) => clamped,
            value => panic!("clamped must be Bool, got {value:?}"),
        }
    }

    fn fallback_active(io: &TestIo) -> bool {
        match io.written(FALLBACK).unwrap().value {
            Value::Bool(active) => active,
            value => panic!("fallback_active must be Bool, got {value:?}"),
        }
    }

    #[test]
    fn the_sum_tracks_ff_plus_trim_inside_the_bounds() {
        let mut block = component();
        let io = io();
        // ff 60 across the trim's in-bounds range: out is the plain
        // sum scan by scan.
        for (tick, trim, expected) in [
            (1, -10.0, 50.0),
            (2, -5.0, 55.0),
            (3, 0.0, 60.0),
            (4, 5.0, 65.0),
            (5, 10.0, 70.0),
        ] {
            feed(&io, TRIM, trim, Quality::Good);
            block.step(&io, Tick(tick)).unwrap();
            assert_eq!(
                out(&io),
                Sample::good(Value::Float(expected), Tick(tick)),
                "trim {trim}"
            );
            // Exactly at the bound is not clamping.
            assert!(!clamped(&io), "trim {trim}");
            assert!(!fallback_active(&io));
        }
    }

    #[test]
    fn the_trim_authority_clamps_the_trim_term() {
        let mut block = component();
        let io = io();

        // A trim above `trim_max` saturates at the bound before the
        // sum: 60 + 10 = 70.
        feed(&io, TRIM, 25.0, Quality::Good);
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(out(&io).value, Value::Float(70.0));
        assert!(clamped(&io));

        // Below `trim_min` saturates the same way: 60 - 10 = 50.
        feed(&io, TRIM, -25.0, Quality::Good);
        block.step(&io, Tick(2)).unwrap();
        assert_eq!(out(&io).value, Value::Float(50.0));
        assert!(clamped(&io));
    }

    #[test]
    fn the_demand_bounds_clamp_the_sum() {
        let mut block = component();
        let io = io();

        // 95 + 10 = 105 exceeds `max_demand` 100.
        feed(&io, FF, 95.0, Quality::Good);
        feed(&io, TRIM, 10.0, Quality::Good);
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(out(&io).value, Value::Float(100.0));
        assert!(clamped(&io));

        // 30 - 40 clamps the trim to -10, then 30 - 10 = 20 sits inside
        // the demand bounds — only the trim bound engaged.
        feed(&io, FF, 30.0, Quality::Good);
        feed(&io, TRIM, -40.0, Quality::Good);
        block.step(&io, Tick(2)).unwrap();
        assert_eq!(out(&io).value, Value::Float(20.0));
        assert!(clamped(&io));

        // 10 - 10 = 0 sits exactly on `min_demand` — not a clamp.
        feed(&io, FF, 10.0, Quality::Good);
        feed(&io, TRIM, -10.0, Quality::Good);
        block.step(&io, Tick(3)).unwrap();
        assert_eq!(out(&io).value, Value::Float(0.0));
        assert!(!clamped(&io));

        // 5 - 10 = -5 floors at `min_demand` 0.
        feed(&io, FF, 5.0, Quality::Good);
        block.step(&io, Tick(4)).unwrap();
        assert_eq!(out(&io).value, Value::Float(0.0));
        assert!(clamped(&io));
    }

    #[test]
    fn each_on_bad_ff_code_answers_an_untrusted_feedforward() {
        for (response, expected) in [(BadTermResponse::Drop, 5.0), (BadTermResponse::Hold, 65.0)] {
            let mut config = config();
            config.on_bad_ff = response;
            let mut block = FeedforwardSum::new("ffs", points(), config).unwrap();
            let io = io();

            // A trusted scan first: 60 + 5 = 65 — the ff the hold
            // response banks.
            block.step(&io, Tick(1)).unwrap();
            assert_eq!(out(&io).value, Value::Float(65.0));
            assert!(!fallback_active(&io));

            // The feed-forward turns bad: the declared response serves
            // — the trim alone for `Drop`, the banked 60 plus the trim
            // for `Hold` — `fallback_active` asserting and `out`
            // stamped worst-of.
            feed(
                &io,
                FF,
                60.0,
                Quality::Bad(QualityReason::CommunicationFault),
            );
            block.step(&io, Tick(2)).unwrap();
            assert_eq!(out(&io).value, Value::Float(expected));
            assert!(fallback_active(&io));
            assert_eq!(
                out(&io).quality,
                Quality::Bad(QualityReason::CommunicationFault)
            );

            // A still-`Good` trim moves under the held term: `Hold`
            // serves the banked ff plus the *current* trim — the hold
            // is the term's, not the demand's.
            feed(&io, TRIM, 8.0, Quality::Good);
            block.step(&io, Tick(3)).unwrap();
            let moved = if response == BadTermResponse::Hold {
                68.0
            } else {
                8.0
            };
            assert_eq!(out(&io).value, Value::Float(moved));
            assert!(fallback_active(&io));

            // Recovery serves and banks again, flags clearing on that
            // scan.
            feed(&io, FF, 40.0, Quality::Good);
            block.step(&io, Tick(4)).unwrap();
            assert_eq!(out(&io).value, Value::Float(48.0));
            assert_eq!(out(&io).quality, Quality::Good);
            assert!(!fallback_active(&io));
        }
    }

    #[test]
    fn each_on_bad_trim_code_answers_an_untrusted_trim() {
        for (response, expected) in [(BadTermResponse::Drop, 60.0), (BadTermResponse::Hold, 65.0)] {
            let mut config = config();
            config.on_bad_trim = response;
            let mut block = FeedforwardSum::new("ffs", points(), config).unwrap();
            let io = io();
            block.step(&io, Tick(1)).unwrap();
            assert_eq!(out(&io).value, Value::Float(65.0));

            feed(&io, TRIM, 5.0, Quality::Bad(QualityReason::DeviceFault));
            block.step(&io, Tick(2)).unwrap();
            // `Drop` serves `ff` untrimmed; `Hold` stands the banked 5
            // in. Either way the flag asserts and `out` carries the
            // trim's quality.
            assert_eq!(out(&io).value, Value::Float(expected));
            assert!(fallback_active(&io));
            assert_eq!(out(&io).quality, Quality::Bad(QualityReason::DeviceFault));
        }
    }

    #[test]
    fn the_hold_response_has_no_term_to_hold_until_the_first_good_one() {
        for (parameter, point) in [("on_bad_ff", FF), ("on_bad_trim", TRIM)] {
            let mut config = config();
            match parameter {
                "on_bad_ff" => config.on_bad_ff = BadTermResponse::Hold,
                _ => config.on_bad_trim = BadTermResponse::Hold,
            }
            let mut block = FeedforwardSum::new("ffs", points(), config).unwrap();
            let io = io();
            feed(&io, point, 5.0, Quality::Bad(QualityReason::DeviceFault));
            block.step(&io, Tick(1)).unwrap();
            // The additive identity stands in for a term never seen
            // good — the other term serves alone.
            let expected = if point == FF { 5.0 } else { 60.0 };
            assert_eq!(out(&io).value, Value::Float(expected), "{parameter}");
            assert!(fallback_active(&io));
        }
    }

    #[test]
    fn both_inputs_bad_compose_their_declared_responses() {
        let mut config = config();
        config.on_bad_ff = BadTermResponse::Hold;
        config.on_bad_trim = BadTermResponse::Hold;
        let mut block = FeedforwardSum::new("ffs", points(), config).unwrap();
        let io = io();

        // Bank ff 60, trim 5 — then both turn bad together: hold +
        // hold serves the last sum, flagged and marked.
        block.step(&io, Tick(1)).unwrap();
        feed(
            &io,
            FF,
            60.0,
            Quality::Bad(QualityReason::CommunicationFault),
        );
        feed(&io, TRIM, 5.0, Quality::Bad(QualityReason::DeviceFault));
        block.step(&io, Tick(2)).unwrap();
        assert_eq!(out(&io).value, Value::Float(65.0));
        assert_eq!(out(&io).quality, Quality::Bad(QualityReason::DeviceFault));
        assert!(fallback_active(&io));

        // A drop/drop instance serves zero — bounded to `min_demand`.
        let mut block = component();
        feed(
            &io,
            FF,
            60.0,
            Quality::Bad(QualityReason::CommunicationFault),
        );
        block.step(&io, Tick(3)).unwrap();
        assert_eq!(out(&io).value, Value::Float(0.0));
        assert!(fallback_active(&io));
    }

    #[test]
    fn a_non_finite_input_takes_the_response_flagged_device_fault() {
        let mut block = component();
        let io = io();
        feed(&io, FF, f64::NAN, Quality::Good);
        block.step(&io, Tick(1)).unwrap();
        // `on_bad_ff` 0 drops the term — the trim serves alone.
        assert_eq!(out(&io).value, Value::Float(5.0));
        // A `Good`-stamped NaN is a device fault the point did not
        // report — the demand carries it.
        assert_eq!(out(&io).quality, Quality::Bad(QualityReason::DeviceFault));
        assert!(fallback_active(&io));
    }

    #[test]
    fn a_served_term_still_passes_through_the_bounds() {
        let mut config = config();
        config.on_bad_trim = BadTermResponse::Hold;
        let mut block = FeedforwardSum::new("ffs", points(), config).unwrap();
        let io = io();

        // Bank a trim of 25 — clamped to 10 while banking, and the
        // held raw value still meets the bound when the response runs.
        feed(&io, TRIM, 25.0, Quality::Good);
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(out(&io).value, Value::Float(70.0));
        assert!(clamped(&io));

        feed(&io, TRIM, 0.0, Quality::Bad(QualityReason::DeviceFault));
        block.step(&io, Tick(2)).unwrap();
        // The held 25 re-clamps at `trim_max` — `clamped` reports
        // honestly under the response.
        assert_eq!(out(&io).value, Value::Float(70.0));
        assert!(clamped(&io));
        assert!(fallback_active(&io));
    }

    #[test]
    fn out_carries_the_worst_of_both_inputs() {
        for (point, quality) in [
            (FF, Quality::Bad(QualityReason::CommunicationFault)),
            (TRIM, Quality::Uncertain(QualityReason::OutOfRange)),
        ] {
            let mut block = component();
            let io = io();
            feed(&io, point, 1.0, quality);
            block.step(&io, Tick(1)).unwrap();
            assert_eq!(out(&io).quality, quality, "degrading {point:?}");
        }
    }

    #[test]
    fn capture_restore_mid_fallback_continues_the_run_identically() {
        let mut config = config();
        config.on_bad_ff = BadTermResponse::Hold;
        config.on_bad_trim = BadTermResponse::Hold;
        let mut block = FeedforwardSum::new("ffs", points(), config).unwrap();
        let io_a = io();
        let io_b = io();

        // Bank ff 60, trim 5; tune a bound mid-run so the checkpoint
        // carries the tuned set too.
        block.step(&io_a, Tick(1)).unwrap();
        block
            .apply_parameter("trim_max", Value::Float(8.0))
            .unwrap();

        // Mid-fallback: both inputs bad, both holds engaged.
        feed(&io_a, FF, 60.0, Quality::Bad(QualityReason::DeviceFault));
        feed(&io_a, TRIM, 5.0, Quality::Bad(QualityReason::DeviceFault));
        block.step(&io_a, Tick(2)).unwrap();
        assert!(fallback_active(&io_a));
        let state = block.capture_state();
        assert_eq!(state.get("last_ff"), Some(Value::Float(60.0)));
        assert_eq!(state.get("last_trim"), Some(Value::Float(5.0)));
        assert_eq!(state.get("trim_max"), Some(Value::Float(8.0)));

        // A standby restores the held terms and the tuned config, and
        // both continue the same inputs identically — hold through the
        // bad scans, recover summing together.
        let mut standby = FeedforwardSum::new("ffs", points(), config).unwrap();
        standby.restore_state(&state).unwrap();
        for tick in 3..8 {
            // The inputs stay bad through tick 5 — both sides hold
            // 60 + 5 re-bounded under the tuned trim authority — then
            // recover together at tick 6.
            let quality = if tick < 6 {
                Quality::Bad(QualityReason::DeviceFault)
            } else {
                Quality::Good
            };
            for io in [&io_a, &io_b] {
                feed(io, FF, 60.0, quality);
                feed(io, TRIM, 5.0, quality);
            }
            block.step(&io_a, Tick(tick)).unwrap();
            standby.step(&io_b, Tick(tick)).unwrap();
            for point in [OUT, CLAMPED, FALLBACK] {
                assert_eq!(io_a.written(point), io_b.written(point), "tick {tick}");
            }
        }
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

        // An inverted bound pair fails the same invariant construction
        // does, naming the field that broke it.
        let mut bad = block.capture_state();
        bad.insert("max_demand", Value::Float(-1.0));
        assert_eq!(
            block.restore_state(&bad),
            Err(StateError::InvalidValue {
                element: "ffs".to_string(),
                field: "max_demand".to_string(),
                value: Value::Float(-1.0),
            })
        );

        // A code naming no response is refused the same way.
        let mut bad = block.capture_state();
        bad.insert("on_bad_trim", Value::Int(4));
        assert_eq!(
            block.restore_state(&bad),
            Err(StateError::InvalidValue {
                element: "ffs".to_string(),
                field: "on_bad_trim".to_string(),
                value: Value::Int(4),
            })
        );

        // A held value that is not finite is refused too.
        let mut bad = block.capture_state();
        bad.insert("last_ff", Value::Float(f64::NAN));
        assert!(matches!(
            block.restore_state(&bad),
            Err(StateError::InvalidValue { ref field, .. }) if field == "last_ff"
        ));
    }

    #[test]
    fn malformed_parameters_fail_construction_naming_the_parameter() {
        // The constructor's invariant checks name the bound that
        // failed.
        for (parameter, config) in [
            (
                "trim_max",
                FeedforwardSumConfig {
                    trim_max: -11.0,
                    ..config()
                },
            ),
            (
                "max_demand",
                FeedforwardSumConfig {
                    min_demand: 5.0,
                    max_demand: -5.0,
                    ..config()
                },
            ),
            (
                "trim_min",
                FeedforwardSumConfig {
                    trim_min: f64::NAN,
                    ..config()
                },
            ),
        ] {
            match FeedforwardSum::new("ffs", points(), config) {
                Err(ParameterError::Invalid {
                    parameter: found, ..
                }) => assert_eq!(found, parameter),
                other => panic!("{parameter}: expected Invalid, got {other:?}"),
            }
        }

        // Through the parameter map: a missing key is Missing, a code
        // naming no response and a bound inversion are Invalid.
        let mut parameters = Parameters::new();
        for (name, value) in [
            ("trim_min", -10.0),
            ("trim_max", 10.0),
            ("min_demand", 0.0),
            ("max_demand", 100.0),
        ] {
            parameters.insert(name.to_string(), Value::Float(value));
        }
        parameters.insert("on_bad_ff".to_string(), Value::Int(0));
        match FeedforwardSum::from_parameters("ffs", points(), &parameters) {
            Err(ParameterError::Missing { parameter, .. }) => {
                assert_eq!(parameter, "on_bad_trim")
            }
            other => panic!("expected Missing on_bad_trim, got {other:?}"),
        }
        parameters.insert("on_bad_trim".to_string(), Value::Int(1));
        FeedforwardSum::from_parameters("ffs", points(), &parameters).unwrap();
        parameters.insert("on_bad_ff".to_string(), Value::Int(3));
        match FeedforwardSum::from_parameters("ffs", points(), &parameters) {
            Err(ParameterError::Invalid { parameter, .. }) => assert_eq!(parameter, "on_bad_ff"),
            other => panic!("expected Invalid on_bad_ff, got {other:?}"),
        }
        parameters.insert("on_bad_ff".to_string(), Value::Int(0));
        parameters.insert("trim_min".to_string(), Value::Float(20.0));
        match FeedforwardSum::from_parameters("ffs", points(), &parameters) {
            Err(ParameterError::Invalid { parameter, .. }) => assert_eq!(parameter, "trim_max"),
            other => panic!("expected Invalid trim_max, got {other:?}"),
        }
    }

    #[test]
    fn tuning_retunes_the_declared_parameters() {
        let mut block = component();
        block
            .apply_parameter("trim_max", Value::Float(15.0))
            .unwrap();
        block.apply_parameter("on_bad_ff", Value::Int(1)).unwrap();
        assert_eq!(block.config().trim_max, 15.0);
        assert_eq!(block.config().on_bad_ff, BadTermResponse::Hold);
        // The tuned values report through the checkpoint vocabulary.
        let reported = block.report_parameters();
        assert_eq!(reported.get("on_bad_ff"), Some(Value::Int(1)));

        // A bound inversion refuses naming the tuned parameter and
        // changes nothing.
        assert!(matches!(
            block
                .apply_parameter("min_demand", Value::Float(200.0))
                .unwrap_err(),
            CommandError::InvalidParameter { ref parameter, .. } if parameter == "min_demand"
        ));
        assert_eq!(block.config().min_demand, 0.0);

        // An out-of-range code refuses the same way.
        assert!(matches!(
            block.apply_parameter("on_bad_trim", Value::Int(4)).unwrap_err(),
            CommandError::InvalidParameter { ref parameter, .. } if parameter == "on_bad_trim"
        ));

        // Undeclared names and wrong-kind values are named rejections.
        assert!(matches!(
            block.apply_parameter("gain", Value::Float(1.0)).unwrap_err(),
            CommandError::UnknownParameter { ref parameter, .. } if parameter == "gain"
        ));
        assert!(matches!(
            block
                .apply_parameter("max_demand", Value::Bool(true))
                .unwrap_err(),
            CommandError::ParameterTypeMismatch { ref parameter, .. } if parameter == "max_demand"
        ));
    }

    #[test]
    fn identical_inputs_produce_identical_outputs() {
        let run = || {
            let mut config = config();
            config.on_bad_ff = BadTermResponse::Hold;
            config.on_bad_trim = BadTermResponse::Hold;
            let mut block = FeedforwardSum::new("ffs", points(), config).unwrap();
            let io = io();
            let mut trace = Vec::new();
            for (tick, quality) in [
                (1, Quality::Good),
                (2, Quality::Good),
                (3, Quality::Bad(QualityReason::DeviceFault)),
                (4, Quality::Bad(QualityReason::DeviceFault)),
                (5, Quality::Good),
            ] {
                feed(&io, FF, 60.0, quality);
                feed(&io, TRIM, 25.0, quality);
                block.step(&io, Tick(tick)).unwrap();
                trace.push((
                    out(&io),
                    io.written(CLAMPED).unwrap(),
                    io.written(FALLBACK).unwrap(),
                ));
            }
            trace
        };
        assert_eq!(run(), run());
    }

    #[test]
    fn describes_itself() {
        let descriptor = component().describe();
        assert_eq!(descriptor.name, "ffs");
        assert_eq!(descriptor.kind, FeedforwardSum::KIND);
        assert_eq!(descriptor.label, "ffs");
        assert_eq!(
            descriptor.ports,
            [
                PortDescriptor {
                    name: "ff".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: Some(PortRole::ProcessValue),
                    point: None,
                },
                PortDescriptor {
                    name: "trim".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: Some(PortRole::ProcessValue),
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
                    name: "clamped".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Status),
                    point: None,
                },
                PortDescriptor {
                    name: "fallback_active".to_string(),
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
                    name: "trim_min".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::FINITE_F64),
                },
                ParameterDescriptor {
                    name: "trim_max".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::FINITE_F64),
                },
                ParameterDescriptor {
                    name: "min_demand".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::FINITE_F64),
                },
                ParameterDescriptor {
                    name: "max_demand".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::FINITE_F64),
                },
                ParameterDescriptor {
                    name: "on_bad_ff".to_string(),
                    kind: ValueKind::Int,
                    range: Some(BAD_TERM_RANGE),
                },
                ParameterDescriptor {
                    name: "on_bad_trim".to_string(),
                    kind: ValueKind::Int,
                    range: Some(BAD_TERM_RANGE),
                },
            ]
        );
    }
}
