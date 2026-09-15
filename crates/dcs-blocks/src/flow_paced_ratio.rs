//! Flow-paced ratio: the chemical-dosing demand contract — `dose` per
//! flow unit times the measured `flow`, optionally scaled by `trim`,
//! bounded by declared dose and rate limits, with declared responses to
//! untrusted inputs. Architecture decision 50 records the contract
//! `WW-CTL-003` requires (`docs/research/chemical-dosing.md`).

use crate::describe;
use crate::params::{self, ParameterError, Parameters};
use dcs_core::{
    CommandError, ComponentDescriptor, ParameterRange, PointId, PortRole, Quality, QualityReason,
    Sample, StateError, StateMap, Tick, Value, ValueKind,
};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// The declared bounds and bad-signal responses a [`FlowPacedRatio`]
/// runs under — its parameter map's seven keys as one value.
///
/// `min_dose`/`max_dose` bound the dose term the operator's `dose`
/// setpoint is clamped to; `min_rate`/`max_rate` bound the emitted
/// `demand`. All four must be finite, with `min_dose <= max_dose` and
/// `min_rate <= max_rate`. `fallback_rate` is the fixed demand
/// `on_bad_flow` code `2` drives — finite, emitted verbatim rather
/// than re-clamped against the rate bounds. `on_bad_flow` is the `Int`
/// code naming the response to an untrusted `flow`: `0` stops —
/// `demand` falls to zero; `1` holds the last `Good`-stamped demand;
/// `2` drives `fallback_rate`. `on_bad_trim` names the response to an
/// untrusted bound `trim`: `0` paces untrimmed on flow alone; `1`
/// holds the last `Good` trim. Which `on_bad_flow` response a plant
/// defaults to is a recorded customer-validation question — the
/// decision keeps it a declared parameter, never a kind default.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct FlowPacedRatioConfig {
    /// The dose term's lower bound — the engineered feed-range floor.
    pub min_dose: f64,
    /// The dose term's upper bound.
    pub max_dose: f64,
    /// The actuator demand's lower bound — the minimum pump running
    /// speed in demand units.
    pub min_rate: f64,
    /// The actuator demand's upper bound.
    pub max_rate: f64,
    /// The response to an untrusted `flow`: `0` stop, `1` hold the last
    /// `Good` demand, `2` drive `fallback_rate`.
    pub on_bad_flow: i64,
    /// The fixed demand `on_bad_flow` code `2` emits.
    pub fallback_rate: f64,
    /// The response to an untrusted bound `trim`: `0` pace untrimmed,
    /// `1` hold the last `Good` trim.
    pub on_bad_trim: i64,
}

/// The output points a [`FlowPacedRatio`] reports on: `demand` (`Out`,
/// `Float`) — the bounded actuator demand the actuation path consumes —
/// `clamped` (`Out`, `Bool`) — asserted while either declared bound
/// engages — and `fallback_active` (`Out`, `Bool`) — asserted while a
/// declared bad-signal response runs, the engagement flag the alarm set
/// consumes.
#[derive(Debug, Clone, Copy)]
pub struct RatioOutputs {
    /// `demand` — the bounded actuator demand.
    pub demand: PointId,
    /// `clamped` — asserts while the dose or rate bound engages.
    pub clamped: PointId,
    /// `fallback_active` — asserts while a declared bad-signal response
    /// runs.
    pub fallback_active: PointId,
}

/// The inclusive `Int` bound `on_bad_flow` accepts: the declared codes
/// `0` (stop), `1` (hold), `2` (fallback rate).
const BAD_FLOW_RANGE: ParameterRange = ParameterRange {
    min: Value::Int(0),
    max: Value::Int(2),
};

/// The inclusive `Int` bound `on_bad_trim` accepts: `0` (pace
/// untrimmed) or `1` (hold the last `Good` trim).
const BAD_TRIM_RANGE: ParameterRange = ParameterRange {
    min: Value::Int(0),
    max: Value::Int(1),
};

/// A flow-paced ratio: reads `flow` (`In`, `Float`) — the measured
/// process flow — `dose` (`In`, `Float`) — the operator's dose
/// setpoint per flow unit, conventionally wired to a writable internal
/// `In` point so writes ride the journaled receipted path and the held
/// value crosses checkpoints — and `trim` (`In`, `Float`) — the
/// optional analyzer correction — and drives `demand` (`Out`,
/// `Float`), `clamped` (`Out`, `Bool`), and `fallback_active` (`Out`,
/// `Bool`).
///
/// **The ratio.** While `flow` reads `Good` with a finite value the
/// kind computes `clamp(dose, min_dose, max_dose) × flow × trim` per
/// scan, clamps the result to `[min_rate, max_rate]`, and writes it on
/// `demand`. `clamped` asserts while either bound engages — a dose
/// entry outside its declared bounds saturates the dose term rather
/// than passing, and a paced result outside the rate bounds saturates
/// at the bound.
///
/// **`dose` hold rule.** A `dose` sample that is `Good` with a finite
/// value refreshes the held setpoint and paces this scan's demand; a
/// non-`Good` or non-finite `dose` holds the last such value — the
/// declared bad-setpoint response — and `fallback_active` asserts
/// while the hold runs. Before the first `Good` finite `dose` arrives
/// the kind cannot pace: `demand` falls to zero and `fallback_active`
/// asserts until one does.
///
/// **`trim`.** An unwired `trim` port means unity — the kind declares
/// the port only where the model wires one (`ComponentSpec::get`). A
/// bound `trim` reading `Good` and finite scales the demand and
/// refreshes the held trim. A non-`Good` or non-finite `trim` takes
/// the declared `on_bad_trim` response — `0` paces untrimmed on flow
/// alone (unity); `1` holds the last `Good` trim, unity when none has
/// been seen — and `fallback_active` asserts.
///
/// **Untrusted `flow`.** A `flow` that is not `Good` or not finite
/// cannot pace: the declared `on_bad_flow` response drives `demand` —
/// `0` falls to zero, `1` holds the last demand the kind stamped
/// `Good` (zero before the first), `2` drives `fallback_rate` — and
/// `fallback_active` asserts for the engagement's duration. The held
/// and fallback values emit verbatim — `fallback_rate` is declared
/// engineering data, not re-clamped — and `clamped` reports `false`
/// while a response runs: no ratio computed, no bound engaged.
///
/// **Quality.** `demand` carries the merged worst of the `flow`,
/// `dose`, and — while bound — `trim` qualities, plus
/// `Bad(DeviceFault)` for a non-finite reading, so a bad flow marks
/// the demand untrusted even under a hold or fallback response.
/// `clamped` and `fallback_active` always carry `Quality::Good`: each
/// is the kind's own computed state.
///
/// Parameters: `min_dose`, `max_dose`, `min_rate`, `max_rate`,
/// `fallback_rate` — required finite `Float`s with `min_dose <=
/// max_dose` and `min_rate <= max_rate` — `on_bad_flow` (required
/// `Int`, `0`–`2`), and `on_bad_trim` (required `Int`, `0`–`1`). All
/// seven tune through `set_parameter`; a retune breaking a bound pair
/// is refused naming the parameter.
#[derive(Debug)]
pub struct FlowPacedRatio {
    name: String,
    flow: PointId,
    dose: PointId,
    trim: Option<PointId>,
    outputs: RatioOutputs,
    config: FlowPacedRatioConfig,
    /// The held last `Good` finite dose — `None` until the first
    /// arrives; a non-`Good` `dose` paces on it.
    last_dose: Option<f64>,
    /// The held last `Good` finite trim — `None` until the first
    /// arrives or while `trim` is unwired; `on_bad_trim = 1` holds it.
    last_trim: Option<f64>,
    /// The last demand the kind stamped `Good` — `None` until the
    /// first; `on_bad_flow = 1` holds it.
    last_demand: Option<f64>,
}

impl FlowPacedRatioConfig {
    /// Validates the invariants every construction path enforces — all
    /// rates finite, each bound pair ordered, both codes in range —
    /// reporting a violation as a [`ParameterError`] naming `component`
    /// and the offending parameter.
    pub(crate) fn checked(component: &str, config: Self) -> Result<Self, ParameterError> {
        for (parameter, value) in [
            ("min_dose", config.min_dose),
            ("max_dose", config.max_dose),
            ("min_rate", config.min_rate),
            ("max_rate", config.max_rate),
            ("fallback_rate", config.fallback_rate),
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
        if !(0..=2).contains(&config.on_bad_flow) {
            return Err(params::invalid(
                component,
                "on_bad_flow",
                "must be 0, 1, or 2".to_string(),
            ));
        }
        if !(0..=1).contains(&config.on_bad_trim) {
            return Err(params::invalid(
                component,
                "on_bad_trim",
                "must be 0 or 1".to_string(),
            ));
        }
        Ok(config)
    }

    /// The bound-pair half of [`checked`](Self::checked): each upper
    /// bound must be at least its lower, and the reported parameter is
    /// the one that failed that constraint.
    fn check_order(component: &str, config: &Self) -> Result<(), ParameterError> {
        if config.min_dose > config.max_dose {
            return Err(params::invalid(
                component,
                "max_dose",
                "must be at least min_dose".to_string(),
            ));
        }
        if config.min_rate > config.max_rate {
            return Err(params::invalid(
                component,
                "max_rate",
                "must be at least min_rate".to_string(),
            ));
        }
        Ok(())
    }
}

impl FlowPacedRatio {
    /// The component-kind string the model-driven registry maps onto
    /// this type's constructor.
    pub const KIND: &'static str = "flow-paced-ratio";

    /// Builds the component from explicit points and the declared
    /// config, or reports an inconsistent one as a [`ParameterError`].
    /// `trim` is `None` for an instance the model leaves unwired.
    pub fn new(
        name: impl Into<String>,
        flow: PointId,
        dose: PointId,
        trim: Option<PointId>,
        outputs: RatioOutputs,
        config: FlowPacedRatioConfig,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        Ok(Self {
            config: FlowPacedRatioConfig::checked(&name, config)?,
            name,
            flow,
            dose,
            trim,
            outputs,
            last_dose: None,
            last_trim: None,
            last_demand: None,
        })
    }

    /// The declared config — the reported parameter set.
    pub fn config(&self) -> FlowPacedRatioConfig {
        self.config
    }

    /// Builds the component from a plant-model parameter map, reading
    /// the parameters listed on the type's docs.
    pub fn from_parameters(
        name: impl Into<String>,
        flow: PointId,
        dose: PointId,
        trim: Option<PointId>,
        outputs: RatioOutputs,
        parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        let config = FlowPacedRatioConfig {
            min_dose: params::required_f64(&name, parameters, "min_dose")?,
            max_dose: params::required_f64(&name, parameters, "max_dose")?,
            min_rate: params::required_f64(&name, parameters, "min_rate")?,
            max_rate: params::required_f64(&name, parameters, "max_rate")?,
            on_bad_flow: params::required_u64(&name, parameters, "on_bad_flow")? as i64,
            fallback_rate: params::required_f64(&name, parameters, "fallback_rate")?,
            on_bad_trim: params::required_u64(&name, parameters, "on_bad_trim")? as i64,
        };
        Self::new(name, flow, dose, trim, outputs, config)
    }

    /// Retunes one declared parameter, returning the updated config or
    /// a [`CommandError`] naming `component`. The bound-pair invariant
    /// is re-checked after a bound change; a refused value changes
    /// nothing.
    fn tune(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        let mut config = self.config;
        match parameter {
            "min_dose" | "max_dose" | "min_rate" | "max_rate" | "fallback_rate" => {
                let tuned = params::tune_f64(&self.name, parameter, value)?;
                if !tuned.is_finite() {
                    return Err(params::invalid_parameter(
                        &self.name,
                        parameter,
                        "must be finite",
                    ));
                }
                match parameter {
                    "min_dose" => config.min_dose = tuned,
                    "max_dose" => config.max_dose = tuned,
                    "min_rate" => config.min_rate = tuned,
                    "max_rate" => config.max_rate = tuned,
                    _ => config.fallback_rate = tuned,
                }
                FlowPacedRatioConfig::check_order(&self.name, &config).map_err(|_| {
                    params::invalid_parameter(
                        &self.name,
                        parameter,
                        "bounds must keep min_dose <= max_dose and min_rate <= max_rate",
                    )
                })?;
            }
            "on_bad_flow" => {
                let tuned = params::tune_u64(&self.name, parameter, value)?;
                if tuned > 2 {
                    return Err(params::invalid_parameter(
                        &self.name,
                        parameter,
                        "must be 0, 1, or 2",
                    ));
                }
                config.on_bad_flow = tuned as i64;
            }
            "on_bad_trim" => {
                let tuned = params::tune_u64(&self.name, parameter, value)?;
                if tuned > 1 {
                    return Err(params::invalid_parameter(
                        &self.name,
                        parameter,
                        "must be 0 or 1",
                    ));
                }
                config.on_bad_trim = tuned as i64;
            }
            _ => return Err(params::unknown_parameter(&self.name, parameter)),
        }
        self.config = config;
        Ok(())
    }
}

impl Component for FlowPacedRatio {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        let mut requirements = vec![
            IoRequirement::input::<f64>("flow", self.flow),
            IoRequirement::input::<f64>("dose", self.dose),
        ];
        // `trim` is the optional port: declared only where the model
        // wired one, so an unwired instance's descriptor and I/O scope
        // carry no `trim`.
        if let Some(trim) = self.trim {
            requirements.push(IoRequirement::input::<f64>("trim", trim));
        }
        requirements.extend([
            IoRequirement::output::<f64>("demand", self.outputs.demand),
            IoRequirement::output::<bool>("clamped", self.outputs.clamped),
            IoRequirement::output::<bool>("fallback_active", self.outputs.fallback_active),
        ]);
        requirements
    }

    fn step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError> {
        let flow = io.read_typed::<f64>(self.flow)?;
        let dose = io.read_typed::<f64>(self.dose)?;
        let trim = match self.trim {
            Some(point) => Some(io.read_typed::<f64>(point)?),
            None => None,
        };

        // `demand` carries the worst of every contributing input, and a
        // non-finite reading is a device fault the point did not
        // report — so an untrusted flow marks the demand even under a
        // hold or fallback response.
        let mut quality = flow.quality.merge(dose.quality);
        if let Some(trim) = trim {
            quality = quality.merge(trim.quality);
        }
        for sample in [flow, dose].into_iter().chain(trim) {
            if !sample.value.is_finite() {
                quality = quality.merge(Quality::Bad(QualityReason::DeviceFault));
            }
        }

        // The dose term: a `Good` finite sample refreshes the held
        // setpoint; anything else holds the last one — the declared
        // bad-setpoint response.
        let dose_ok = dose.quality.is_good() && dose.value.is_finite();
        if dose_ok {
            self.last_dose = Some(dose.value);
        }
        let dose_served = if dose_ok {
            Some(dose.value)
        } else {
            self.last_dose
        };

        // The trim factor: unity while unwired; a bound non-`Good` or
        // non-finite sample takes the declared `on_bad_trim` response.
        let mut trim_engaged = false;
        let trim_factor = match trim {
            None => 1.0,
            Some(sample) if sample.quality.is_good() && sample.value.is_finite() => {
                self.last_trim = Some(sample.value);
                sample.value
            }
            Some(_) => {
                trim_engaged = true;
                match self.config.on_bad_trim {
                    1 => self.last_trim.unwrap_or(1.0),
                    _ => 1.0,
                }
            }
        };

        let flow_trusted = flow.quality.is_good() && flow.value.is_finite();
        let (demand, clamped, fallback_active) = if !flow_trusted {
            // The declared response to an untrusted pacing signal —
            // the held value is the last `Good`-stamped demand, zero
            // before the first.
            let response = match self.config.on_bad_flow {
                1 => self.last_demand.unwrap_or(0.0),
                2 => self.config.fallback_rate,
                _ => 0.0,
            };
            (response, false, true)
        } else if let Some(dose) = dose_served {
            let bounded_dose = dose.clamp(self.config.min_dose, self.config.max_dose);
            let raw = bounded_dose * flow.value * trim_factor;
            let demand = raw.clamp(self.config.min_rate, self.config.max_rate);
            if quality.is_good() {
                self.last_demand = Some(demand);
            }
            (
                demand,
                bounded_dose != dose || demand != raw,
                trim_engaged || !dose_ok,
            )
        } else {
            // No trusted dose has ever arrived: the kind cannot pace.
            (0.0, false, true)
        };

        io.write_sample(
            self.outputs.demand,
            Sample::new(Value::Float(demand), quality, tick),
        )?;
        io.write_sample(
            self.outputs.clamped,
            Sample::good(Value::Bool(clamped), tick),
        )?;
        io.write_sample(
            self.outputs.fallback_active,
            Sample::good(Value::Bool(fallback_active), tick),
        )?;
        Ok(())
    }

    /// Describes the ratio: `flow` and `trim` are the measured values
    /// it acts on, `dose` the operator's setpoint, `demand` the driven
    /// value, `clamped` and `fallback_active` the reported conditions;
    /// `trim` joins the descriptor only where bound. The declared
    /// parameter set is the seven keys `from_parameters` reads.
    fn describe(&self) -> ComponentDescriptor {
        let mut roles: Vec<(&str, PortRole)> = vec![
            ("flow", PortRole::ProcessValue),
            ("dose", PortRole::Setpoint),
        ];
        if self.trim.is_some() {
            roles.push(("trim", PortRole::ProcessValue));
        }
        roles.extend([
            ("demand", PortRole::Output),
            ("clamped", PortRole::Status),
            ("fallback_active", PortRole::Status),
        ]);
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &roles,
            vec![
                describe::parameter("min_dose", ValueKind::Float, Some(describe::FINITE_F64)),
                describe::parameter("max_dose", ValueKind::Float, Some(describe::FINITE_F64)),
                describe::parameter("min_rate", ValueKind::Float, Some(describe::FINITE_F64)),
                describe::parameter("max_rate", ValueKind::Float, Some(describe::FINITE_F64)),
                describe::parameter("on_bad_flow", ValueKind::Int, Some(BAD_FLOW_RANGE)),
                describe::parameter(
                    "fallback_rate",
                    ValueKind::Float,
                    Some(describe::FINITE_F64),
                ),
                describe::parameter("on_bad_trim", ValueKind::Int, Some(BAD_TRIM_RANGE)),
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
        parameters.insert("min_dose", Value::Float(self.config.min_dose));
        parameters.insert("max_dose", Value::Float(self.config.max_dose));
        parameters.insert("min_rate", Value::Float(self.config.min_rate));
        parameters.insert("max_rate", Value::Float(self.config.max_rate));
        parameters.insert("on_bad_flow", Value::Int(self.config.on_bad_flow));
        parameters.insert("fallback_rate", Value::Float(self.config.fallback_rate));
        parameters.insert("on_bad_trim", Value::Int(self.config.on_bad_trim));
        parameters
    }

    /// Captures the tuned config and the held last-good dose, trim,
    /// and demand — the state the hold responses need — so a
    /// checkpointed standby continues an engaged fallback identically.
    /// A held value never yet established captures as absent.
    fn capture_state(&self) -> StateMap {
        let mut state = self.report_parameters();
        if let Some(dose) = self.last_dose {
            state.insert("last_dose", Value::Float(dose));
        }
        if let Some(trim) = self.last_trim {
            state.insert("last_trim", Value::Float(trim));
        }
        if let Some(demand) = self.last_demand {
            state.insert("last_demand", Value::Float(demand));
        }
        state
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        state.ensure_known_fields(
            &self.name,
            &[
                "min_dose",
                "max_dose",
                "min_rate",
                "max_rate",
                "on_bad_flow",
                "fallback_rate",
                "on_bad_trim",
                "last_dose",
                "last_trim",
                "last_demand",
            ],
        )?;

        // The whole map is validated before any state mutates, so a
        // rejected checkpoint leaves the component on its last-good
        // state rather than half-applied.
        let config = FlowPacedRatioConfig {
            min_dose: state.require_f64(&self.name, "min_dose")?,
            max_dose: state.require_f64(&self.name, "max_dose")?,
            min_rate: state.require_f64(&self.name, "min_rate")?,
            max_rate: state.require_f64(&self.name, "max_rate")?,
            on_bad_flow: state.require_i64(&self.name, "on_bad_flow")?,
            fallback_rate: state.require_f64(&self.name, "fallback_rate")?,
            on_bad_trim: state.require_i64(&self.name, "on_bad_trim")?,
        };
        let last_dose = state.optional_f64(&self.name, "last_dose")?;
        let last_trim = state.optional_f64(&self.name, "last_trim")?;
        let last_demand = state.optional_f64(&self.name, "last_demand")?;

        for (field, value) in [
            ("min_dose", config.min_dose),
            ("max_dose", config.max_dose),
            ("min_rate", config.min_rate),
            ("max_rate", config.max_rate),
            ("fallback_rate", config.fallback_rate),
        ] {
            if !value.is_finite() {
                return Err(StateError::InvalidValue {
                    element: self.name.clone(),
                    field: field.to_string(),
                    value: Value::Float(value),
                });
            }
        }
        for (field, value) in [
            ("last_dose", last_dose),
            ("last_trim", last_trim),
            ("last_demand", last_demand),
        ] {
            match value {
                Some(held) if !held.is_finite() => {
                    return Err(StateError::InvalidValue {
                        element: self.name.clone(),
                        field: field.to_string(),
                        value: Value::Float(held),
                    });
                }
                _ => {}
            }
        }
        if let Err(ParameterError::Invalid { parameter, .. }) =
            FlowPacedRatioConfig::check_order(&self.name, &config)
        {
            let value = match parameter.as_str() {
                "max_dose" => config.max_dose,
                _ => config.max_rate,
            };
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: parameter,
                value: Value::Float(value),
            });
        }
        for (field, code, bound) in [
            ("on_bad_flow", config.on_bad_flow, 2),
            ("on_bad_trim", config.on_bad_trim, 1),
        ] {
            if !(0..=bound).contains(&code) {
                return Err(StateError::InvalidValue {
                    element: self.name.clone(),
                    field: field.to_string(),
                    value: Value::Int(code),
                });
            }
        }

        self.config = config;
        self.last_dose = last_dose;
        self.last_trim = last_trim;
        self.last_demand = last_demand;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::TestIo;
    use dcs_core::{Direction, ParameterDescriptor, PortDescriptor, StateError, StateMap};

    const FLOW: PointId = PointId(10);
    const DOSE: PointId = PointId(11);
    const TRIM: PointId = PointId(12);
    const DEMAND: PointId = PointId(20);
    const CLAMPED: PointId = PointId(21);
    const FALLBACK: PointId = PointId(22);

    fn outputs() -> RatioOutputs {
        RatioOutputs {
            demand: DEMAND,
            clamped: CLAMPED,
            fallback_active: FALLBACK,
        }
    }

    fn config() -> FlowPacedRatioConfig {
        FlowPacedRatioConfig {
            min_dose: 0.5,
            max_dose: 4.0,
            min_rate: 0.0,
            max_rate: 50.0,
            on_bad_flow: 0,
            fallback_rate: 12.0,
            on_bad_trim: 0,
        }
    }

    fn ratio() -> FlowPacedRatio {
        FlowPacedRatio::new("fpr", FLOW, DOSE, Some(TRIM), outputs(), config()).unwrap()
    }

    fn io() -> TestIo {
        TestIo::new(&[
            (
                FLOW,
                Direction::In,
                Sample::good(Value::Float(10.0), Tick::ZERO),
            ),
            (
                DOSE,
                Direction::In,
                Sample::good(Value::Float(2.0), Tick::ZERO),
            ),
            (
                TRIM,
                Direction::In,
                Sample::good(Value::Float(1.0), Tick::ZERO),
            ),
            (
                DEMAND,
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

    fn demand(io: &TestIo) -> Sample {
        io.written(DEMAND).unwrap()
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
    fn the_ratio_paces_demand_across_the_flow_range() {
        let mut block = ratio();
        let io = io();
        // Dose 2.0 across the feed's range: demand tracks `dose × flow`
        // scan by scan until the rate bound takes over.
        for (tick, flow, expected) in [
            (1, 0.0, 0.0),
            (2, 1.0, 2.0),
            (3, 5.0, 10.0),
            (4, 10.0, 20.0),
            (5, 25.0, 50.0),
        ] {
            feed(&io, FLOW, flow, Quality::Good);
            block.step(&io, Tick(tick)).unwrap();
            assert_eq!(
                demand(&io),
                Sample::good(Value::Float(expected), Tick(tick)),
                "flow {flow}"
            );
            // Exactly at the bound is not clamping.
            assert!(!clamped(&io), "flow {flow}");
            assert!(!fallback_active(&io));
        }
    }

    #[test]
    fn the_dose_bounds_clamp_the_dose_term() {
        let mut block = ratio();
        let io = io();

        // An operator dose above `max_dose` saturates at the bound.
        feed(&io, DOSE, 6.0, Quality::Good);
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(demand(&io).value, Value::Float(40.0));
        assert!(clamped(&io));

        // Below `min_dose` saturates the same way.
        feed(&io, DOSE, 0.1, Quality::Good);
        block.step(&io, Tick(2)).unwrap();
        assert_eq!(demand(&io).value, Value::Float(5.0));
        assert!(clamped(&io));

        // A dose exactly at the bound is not a clamp.
        feed(&io, DOSE, 4.0, Quality::Good);
        block.step(&io, Tick(3)).unwrap();
        assert_eq!(demand(&io).value, Value::Float(40.0));
        assert!(!clamped(&io));
    }

    #[test]
    fn the_rate_bounds_clamp_the_demand() {
        let mut block = ratio();
        let io = io();

        // 2.0 × 30 = 60 exceeds `max_rate` 50.
        feed(&io, FLOW, 30.0, Quality::Good);
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(demand(&io).value, Value::Float(50.0));
        assert!(clamped(&io));

        // A negative flow drives the raw demand below `min_rate` 0.
        feed(&io, FLOW, -5.0, Quality::Good);
        block.step(&io, Tick(2)).unwrap();
        assert_eq!(demand(&io).value, Value::Float(0.0));
        assert!(clamped(&io));

        // A raised `min_rate` floor-clamps a low flow the same way.
        let mut config = config();
        config.min_rate = 5.0;
        let mut block =
            FlowPacedRatio::new("fpr", FLOW, DOSE, Some(TRIM), outputs(), config).unwrap();
        feed(&io, FLOW, 1.0, Quality::Good);
        block.step(&io, Tick(3)).unwrap();
        assert_eq!(demand(&io).value, Value::Float(5.0));
        assert!(clamped(&io));
    }

    #[test]
    fn trim_scales_the_paced_demand_while_bound_and_good() {
        let mut block = ratio();
        let io = io();
        feed(&io, TRIM, 1.5, Quality::Good);
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(demand(&io).value, Value::Float(30.0));
    }

    #[test]
    fn an_unwired_trim_means_unity() {
        let mut block = FlowPacedRatio::new("fpr", FLOW, DOSE, None, outputs(), config()).unwrap();
        let io = io();
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(demand(&io).value, Value::Float(20.0));
        assert_eq!(demand(&io).quality, Quality::Good);
    }

    #[test]
    fn a_non_good_flow_takes_the_declared_response() {
        for (code, expected) in [(0, 0.0), (1, 10.0), (2, 12.0)] {
            let mut config = config();
            config.on_bad_flow = code;
            let mut block =
                FlowPacedRatio::new("fpr", FLOW, DOSE, Some(TRIM), outputs(), config).unwrap();
            let io = io();

            // A trusted scan first: dose 2 × flow 5 = 10 — the demand
            // code 1 holds.
            feed(&io, FLOW, 5.0, Quality::Good);
            block.step(&io, Tick(1)).unwrap();
            assert_eq!(demand(&io).value, Value::Float(10.0));
            assert!(!fallback_active(&io));

            // The flow turns bad: the declared response drives the
            // demand and `fallback_active` asserts for the engagement.
            feed(
                &io,
                FLOW,
                5.0,
                Quality::Bad(QualityReason::CommunicationFault),
            );
            block.step(&io, Tick(2)).unwrap();
            assert_eq!(
                demand(&io).value,
                Value::Float(expected),
                "on_bad_flow = {code}"
            );
            assert!(fallback_active(&io), "on_bad_flow = {code}");
            assert_eq!(
                demand(&io).quality,
                Quality::Bad(QualityReason::CommunicationFault)
            );

            // The response runs for the engagement's whole duration.
            block.step(&io, Tick(3)).unwrap();
            assert_eq!(demand(&io).value, Value::Float(expected));
            assert!(fallback_active(&io));

            // Recovery paces again — and clears the flag on that scan.
            feed(&io, FLOW, 5.0, Quality::Good);
            block.step(&io, Tick(4)).unwrap();
            assert_eq!(demand(&io).value, Value::Float(10.0));
            assert_eq!(demand(&io).quality, Quality::Good);
            assert!(!fallback_active(&io));
        }
    }

    #[test]
    fn a_non_finite_flow_takes_the_response_flagged_device_fault() {
        let mut block = ratio();
        let io = io();
        feed(&io, FLOW, f64::NAN, Quality::Good);
        block.step(&io, Tick(1)).unwrap();
        // `on_bad_flow` 0 stops the demand.
        assert_eq!(demand(&io).value, Value::Float(0.0));
        // A `Good`-stamped NaN is a device fault the point did not
        // report — the demand carries it.
        assert_eq!(
            demand(&io).quality,
            Quality::Bad(QualityReason::DeviceFault)
        );
        assert!(fallback_active(&io));
    }

    #[test]
    fn the_hold_response_has_no_demand_to_hold_until_the_first_good_one() {
        let mut config = config();
        config.on_bad_flow = 1;
        let mut block =
            FlowPacedRatio::new("fpr", FLOW, DOSE, Some(TRIM), outputs(), config).unwrap();
        let io = io();
        feed(&io, FLOW, 5.0, Quality::Bad(QualityReason::DeviceFault));
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(demand(&io).value, Value::Float(0.0));
        assert!(fallback_active(&io));
    }

    #[test]
    fn a_non_good_trim_takes_the_declared_response() {
        for code in [0, 1] {
            let mut config = config();
            config.on_bad_trim = code;
            let mut block =
                FlowPacedRatio::new("fpr", FLOW, DOSE, Some(TRIM), outputs(), config).unwrap();
            let io = io();

            // Bank a good trim of 1.5 first.
            feed(&io, TRIM, 1.5, Quality::Good);
            block.step(&io, Tick(1)).unwrap();
            assert_eq!(demand(&io).value, Value::Float(30.0));

            feed(&io, TRIM, 0.0, Quality::Bad(QualityReason::DeviceFault));
            block.step(&io, Tick(2)).unwrap();
            // Code 0 paces untrimmed — unity; code 1 holds the banked
            // 1.5. Either way `fallback_active` asserts and `demand`
            // carries the trim's quality.
            let expected = if code == 0 { 20.0 } else { 30.0 };
            assert_eq!(demand(&io).value, Value::Float(expected), "code {code}");
            assert!(fallback_active(&io), "code {code}");
            assert_eq!(
                demand(&io).quality,
                Quality::Bad(QualityReason::DeviceFault)
            );
        }
    }

    #[test]
    fn the_trim_hold_has_no_trim_to_hold_until_the_first_good_one() {
        let mut config = config();
        config.on_bad_trim = 1;
        let mut block =
            FlowPacedRatio::new("fpr", FLOW, DOSE, Some(TRIM), outputs(), config).unwrap();
        let io = io();
        feed(&io, TRIM, 1.5, Quality::Bad(QualityReason::DeviceFault));
        block.step(&io, Tick(1)).unwrap();
        // Unity stands in for a trim never seen good.
        assert_eq!(demand(&io).value, Value::Float(20.0));
        assert!(fallback_active(&io));
    }

    #[test]
    fn a_non_good_dose_holds_the_last_good_dose() {
        let mut block = ratio();
        let io = io();
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(demand(&io).value, Value::Float(20.0));

        // The setpoint point degrades: the ratio keeps pacing on the
        // held 2.0 while `fallback_active` reports the hold and
        // `demand` carries the setpoint's quality.
        feed(&io, DOSE, 9.0, Quality::Uncertain(QualityReason::Stale));
        block.step(&io, Tick(2)).unwrap();
        assert_eq!(demand(&io).value, Value::Float(20.0));
        assert_eq!(
            demand(&io).quality,
            Quality::Uncertain(QualityReason::Stale)
        );
        assert!(fallback_active(&io));

        feed(&io, DOSE, 3.0, Quality::Good);
        block.step(&io, Tick(3)).unwrap();
        assert_eq!(demand(&io).value, Value::Float(30.0));
        assert!(!fallback_active(&io));
    }

    #[test]
    fn before_the_first_good_dose_the_kind_cannot_pace() {
        let mut block = ratio();
        let io = io();
        feed(&io, DOSE, 2.0, Quality::Bad(QualityReason::DeviceFault));
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(demand(&io).value, Value::Float(0.0));
        assert_eq!(
            demand(&io).quality,
            Quality::Bad(QualityReason::DeviceFault)
        );
        assert!(fallback_active(&io));
    }

    #[test]
    fn demand_carries_the_worst_of_every_input() {
        for (point, quality) in [
            (FLOW, Quality::Bad(QualityReason::CommunicationFault)),
            (DOSE, Quality::Uncertain(QualityReason::OutOfRange)),
            (TRIM, Quality::Uncertain(QualityReason::Substituted)),
        ] {
            let mut config = config();
            config.on_bad_trim = 0;
            let mut block =
                FlowPacedRatio::new("fpr", FLOW, DOSE, Some(TRIM), outputs(), config).unwrap();
            let io = io();
            feed(&io, point, 1.0, quality);
            block.step(&io, Tick(1)).unwrap();
            assert_eq!(demand(&io).quality, quality, "degrading {point:?}");
        }

        // An unwired trim contributes nothing — its point is not read.
        let mut block = FlowPacedRatio::new("fpr", FLOW, DOSE, None, outputs(), config()).unwrap();
        let io = io();
        feed(
            &io,
            TRIM,
            f64::NAN,
            Quality::Bad(QualityReason::DeviceFault),
        );
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(demand(&io).quality, Quality::Good);
    }

    #[test]
    fn capture_restore_mid_fallback_continues_the_run_identically() {
        let mut config = config();
        config.on_bad_flow = 1;
        config.on_bad_trim = 1;
        let mut block =
            FlowPacedRatio::new("fpr", FLOW, DOSE, Some(TRIM), outputs(), config).unwrap();
        let io_a = io();
        let io_b = io();

        // Bank good demand (dose 2 × flow 5 = 10), trim 1.5, dose 3.
        feed(&io_a, FLOW, 5.0, Quality::Good);
        feed(&io_a, DOSE, 3.0, Quality::Good);
        feed(&io_a, TRIM, 1.5, Quality::Good);
        block.step(&io_a, Tick(1)).unwrap();
        block
            .apply_parameter("fallback_rate", Value::Float(15.0))
            .unwrap();

        // Mid-fallback: flow and trim both bad, the hold engaged.
        feed(&io_a, FLOW, 5.0, Quality::Bad(QualityReason::DeviceFault));
        feed(&io_a, TRIM, 0.0, Quality::Bad(QualityReason::DeviceFault));
        feed(&io_a, DOSE, 9.0, Quality::Bad(QualityReason::DeviceFault));
        block.step(&io_a, Tick(2)).unwrap();
        assert!(fallback_active(&io_a));
        let state = block.capture_state();
        assert_eq!(state.get("last_demand"), Some(Value::Float(22.5)));
        assert_eq!(state.get("last_dose"), Some(Value::Float(3.0)));
        assert_eq!(state.get("last_trim"), Some(Value::Float(1.5)));
        assert_eq!(state.get("fallback_rate"), Some(Value::Float(15.0)));

        // A standby restores the held values and the tuned config, and
        // both continue the same inputs identically — hold through the
        // bad scans, recover pacing together.
        let mut standby =
            FlowPacedRatio::new("fpr", FLOW, DOSE, Some(TRIM), outputs(), config).unwrap();
        standby.restore_state(&state).unwrap();
        for tick in 3..8 {
            // The pacing signal stays bad through tick 5 — both sides
            // hold the banked 22.5 — then flow, trim, and the dose
            // setpoint recover together at tick 6.
            let quality = if tick < 6 {
                Quality::Bad(QualityReason::DeviceFault)
            } else {
                Quality::Good
            };
            for io in [&io_a, &io_b] {
                feed(io, FLOW, 5.0, quality);
                feed(io, TRIM, 1.5, quality);
                feed(io, DOSE, 3.0, quality);
            }
            block.step(&io_a, Tick(tick)).unwrap();
            standby.step(&io_b, Tick(tick)).unwrap();
            for point in [DEMAND, CLAMPED, FALLBACK] {
                assert_eq!(io_a.written(point), io_b.written(point), "tick {tick}");
            }
        }
        assert_eq!(standby.capture_state(), block.capture_state());
    }

    #[test]
    fn restore_rejects_an_incompatible_state_map() {
        let mut block = ratio();

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
        bad.insert("max_rate", Value::Float(-1.0));
        assert_eq!(
            block.restore_state(&bad),
            Err(StateError::InvalidValue {
                element: "fpr".to_string(),
                field: "max_rate".to_string(),
                value: Value::Float(-1.0),
            })
        );

        // A held value that is not finite is refused too.
        let mut bad = block.capture_state();
        bad.insert("last_demand", Value::Float(f64::NAN));
        assert!(matches!(
            block.restore_state(&bad),
            Err(StateError::InvalidValue { ref field, .. }) if field == "last_demand"
        ));
    }

    #[test]
    fn malformed_parameters_fail_construction_naming_the_parameter() {
        // The constructor's invariant checks name the bound that
        // failed.
        for (parameter, config) in [
            (
                "max_dose",
                FlowPacedRatioConfig {
                    max_dose: 0.4,
                    ..config()
                },
            ),
            (
                "max_rate",
                FlowPacedRatioConfig {
                    min_rate: 5.0,
                    max_rate: 5.0 - 10.0,
                    ..config()
                },
            ),
            (
                "on_bad_flow",
                FlowPacedRatioConfig {
                    on_bad_flow: 3,
                    ..config()
                },
            ),
            (
                "on_bad_trim",
                FlowPacedRatioConfig {
                    on_bad_trim: 2,
                    ..config()
                },
            ),
            (
                "fallback_rate",
                FlowPacedRatioConfig {
                    fallback_rate: f64::NAN,
                    ..config()
                },
            ),
        ] {
            match FlowPacedRatio::new("fpr", FLOW, DOSE, None, outputs(), config) {
                Err(ParameterError::Invalid {
                    parameter: found, ..
                }) => assert_eq!(found, parameter),
                other => panic!("{parameter}: expected Invalid, got {other:?}"),
            }
        }

        // Through the parameter map: a missing key is Missing, a
        // mistyped one Invalid.
        let mut parameters = Parameters::new();
        for (name, value) in [
            ("min_dose", 0.5),
            ("max_dose", 4.0),
            ("min_rate", 0.0),
            ("max_rate", 50.0),
            ("fallback_rate", 12.0),
        ] {
            parameters.insert(name.to_string(), Value::Float(value));
        }
        parameters.insert("on_bad_flow".to_string(), Value::Int(0));
        match FlowPacedRatio::from_parameters("fpr", FLOW, DOSE, None, outputs(), &parameters) {
            Err(ParameterError::Missing { parameter, .. }) => {
                assert_eq!(parameter, "on_bad_trim")
            }
            other => panic!("expected Missing on_bad_trim, got {other:?}"),
        }
        parameters.insert("on_bad_trim".to_string(), Value::Int(1));
        FlowPacedRatio::from_parameters("fpr", FLOW, DOSE, None, outputs(), &parameters).unwrap();
        parameters.insert("min_dose".to_string(), Value::Float(9.0));
        match FlowPacedRatio::from_parameters("fpr", FLOW, DOSE, None, outputs(), &parameters) {
            Err(ParameterError::Invalid { parameter, .. }) => assert_eq!(parameter, "max_dose"),
            other => panic!("expected Invalid max_dose, got {other:?}"),
        }
    }

    #[test]
    fn tuning_retunes_the_declared_parameters() {
        let mut block = ratio();
        block
            .apply_parameter("max_dose", Value::Float(3.0))
            .unwrap();
        block.apply_parameter("on_bad_flow", Value::Int(2)).unwrap();
        block
            .apply_parameter("fallback_rate", Value::Float(15.0))
            .unwrap();
        assert_eq!(block.config().max_dose, 3.0);
        assert_eq!(block.config().on_bad_flow, 2);
        assert_eq!(block.config().fallback_rate, 15.0);
        // The tuned values report through the checkpoint vocabulary.
        let reported = block.report_parameters();
        assert_eq!(reported.get("on_bad_flow"), Some(Value::Int(2)));

        // A bound inversion refuses naming the tuned parameter and
        // changes nothing.
        assert!(matches!(
            block.apply_parameter("min_rate", Value::Float(60.0)).unwrap_err(),
            CommandError::InvalidParameter { ref parameter, .. } if parameter == "min_rate"
        ));
        assert_eq!(block.config().min_rate, 0.0);

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
            block.apply_parameter("max_rate", Value::Bool(true)).unwrap_err(),
            CommandError::ParameterTypeMismatch { ref parameter, .. } if parameter == "max_rate"
        ));
    }

    #[test]
    fn identical_inputs_produce_identical_outputs() {
        let run = || {
            let mut config = config();
            config.on_bad_flow = 1;
            config.on_bad_trim = 1;
            let mut block =
                FlowPacedRatio::new("fpr", FLOW, DOSE, Some(TRIM), outputs(), config).unwrap();
            let io = io();
            let mut trace = Vec::new();
            for (tick, flow, quality) in [
                (1, 5.0, Quality::Good),
                (2, 30.0, Quality::Good),
                (3, 30.0, Quality::Bad(QualityReason::DeviceFault)),
                (4, 30.0, Quality::Bad(QualityReason::DeviceFault)),
                (5, 10.0, Quality::Good),
            ] {
                feed(&io, FLOW, flow, quality);
                feed(&io, TRIM, 1.5, quality);
                block.step(&io, Tick(tick)).unwrap();
                trace.push((
                    demand(&io),
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
        let descriptor = ratio().describe();
        assert_eq!(descriptor.name, "fpr");
        assert_eq!(descriptor.kind, FlowPacedRatio::KIND);
        assert_eq!(descriptor.label, "fpr");
        assert_eq!(
            descriptor.ports,
            [
                PortDescriptor {
                    name: "flow".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: Some(PortRole::ProcessValue),
                    point: None,
                },
                PortDescriptor {
                    name: "dose".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: Some(PortRole::Setpoint),
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
                    name: "demand".to_string(),
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
                    name: "min_dose".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::FINITE_F64),
                },
                ParameterDescriptor {
                    name: "max_dose".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::FINITE_F64),
                },
                ParameterDescriptor {
                    name: "min_rate".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::FINITE_F64),
                },
                ParameterDescriptor {
                    name: "max_rate".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::FINITE_F64),
                },
                ParameterDescriptor {
                    name: "on_bad_flow".to_string(),
                    kind: ValueKind::Int,
                    range: Some(BAD_FLOW_RANGE),
                },
                ParameterDescriptor {
                    name: "fallback_rate".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::FINITE_F64),
                },
                ParameterDescriptor {
                    name: "on_bad_trim".to_string(),
                    kind: ValueKind::Int,
                    range: Some(BAD_TRIM_RANGE),
                },
            ]
        );

        // Unwired, `trim` is absent from the descriptor entirely.
        let descriptor = FlowPacedRatio::new("fpr", FLOW, DOSE, None, outputs(), config())
            .unwrap()
            .describe();
        let ports: Vec<&str> = descriptor
            .ports
            .iter()
            .map(|port| port.name.as_str())
            .collect();
        assert_eq!(
            ports,
            ["flow", "dose", "demand", "clamped", "fallback_active"]
        );
    }
}
