//! Demand fallback: the declared all-measurements-bad response on the
//! DO-to-airflow demand path architecture decision 65 records for
//! `WW-CTL-005` and `WW-OPS-003`
//! (`docs/research/aeration-do-control.md`) — the terminal fallback the
//! upstream `failover-select`/`median-voter` measurement layer cannot
//! express once no DO source remains good.

use crate::describe;
use crate::params::{self, ParameterError, Parameters};
use dcs_core::{
    CommandError, ComponentDescriptor, ParameterRange, PointId, PortRole, Quality, QualityReason,
    Sample, StateError, StateMap, Tick, Value, ValueKind,
};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// What a [`DemandFallback`] serves on `out` while the selected
/// measurement is untrusted.
///
/// The model's parameter vocabulary has no string type, so the
/// `on_bad` parameter carries the choice as an `Int` code: `0` for
/// `Hold`, `1` for `FallbackFlow`, `2` for `SafeFlow` — the values
/// [`code`](Self::code) reports and [`decode`](Self::decode) accepts.
/// Which response a plant declares is customer-validation data, never
/// a kind default.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FallbackResponse {
    /// Hold the last demand the kind stamped `Good` — the demand
    /// freezes at its last trusted value for the engagement's
    /// duration.
    Hold,
    /// Drive the declared `fallback_flow` — a fixed airflow.
    FallbackFlow,
    /// Drive the declared `safe_flow` — the declared safe airflow,
    /// e.g. the permit-protecting rate for the low-DO direction.
    SafeFlow,
}

impl FallbackResponse {
    /// The `Int` code the `on_bad` parameter carries.
    pub const fn code(self) -> i64 {
        match self {
            Self::Hold => 0,
            Self::FallbackFlow => 1,
            Self::SafeFlow => 2,
        }
    }

    /// The response the `Int` code `code` selects, or `None` when the
    /// code declares no response.
    pub const fn decode(code: i64) -> Option<Self> {
        match code {
            0 => Some(Self::Hold),
            1 => Some(Self::FallbackFlow),
            2 => Some(Self::SafeFlow),
            _ => None,
        }
    }
}

/// The declared fallback response and fixed demands a
/// [`DemandFallback`] runs under — its parameter map's three keys as
/// one value.
///
/// `on_bad` names the response a non-`Good` `pv` engages;
/// `fallback_flow` and `safe_flow` are the fixed demands codes `1` and
/// `2` drive — declared engineering data, each finite but otherwise
/// unbounded and emitted verbatim rather than clamped.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct DemandFallbackConfig {
    /// The declared response to an untrusted `pv`.
    pub on_bad: FallbackResponse,
    /// The fixed demand `on_bad` code `1` emits.
    pub fallback_flow: f64,
    /// The declared safe demand `on_bad` code `2` emits.
    pub safe_flow: f64,
}

/// The points a [`DemandFallback`] binds — the demand it guards and
/// the measurement whose quality conditions it (`In`), and the served
/// demand plus the engagement report it drives (`Out`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DemandFallbackIo {
    /// `in` (`In`, `Float`): the loop's airflow demand — the value
    /// passed through while `pv` reads `Good`.
    pub input: PointId,
    /// `pv` (`In`, `Float`): the selected DO measurement whose quality
    /// drives the fallback — conventionally a `median-voter`'s or
    /// `failover-select`'s `out`, or the single probe.
    pub pv: PointId,
    /// `out` (`Out`, `Float`): the demand served downstream.
    pub out: PointId,
    /// `fallback_active` (`Out`, `Bool`): asserted for the
    /// engagement's duration — the alarmed transition surface the
    /// alarm set consumes.
    pub fallback_active: PointId,
}

/// The inclusive `Int` bound the `on_bad` parameter accepts: the
/// declared [`FallbackResponse`] codes.
const ON_BAD_RANGE: ParameterRange = ParameterRange {
    min: Value::Int(0),
    max: Value::Int(2),
};

/// A demand fallback: reads `in` (`In`, `Float`) — the loop's airflow
/// demand — and `pv` (`In`, `Float`) — the selected DO measurement —
/// and drives `out` (`Out`, `Float`) and `fallback_active` (`Out`,
/// `Bool`).
///
/// **Pass-through.** While `pv` reads `Good` with a finite value the
/// `in` sample passes to `out` unmodified and `fallback_active`
/// reports `false` — a non-finite `pv` cannot be controlled on and
/// counts as an untrusted measurement, the crate's
/// `NaN`-as-device-fault convention.
///
/// **The fallback.** A `pv` that is not `Good` — or not finite —
/// engages the declared `on_bad` response and asserts
/// `fallback_active` for the engagement's duration: code `0` holds the
/// last demand the kind stamped `Good` — zero before the first; code
/// `1` drives the declared `fallback_flow`; code `2` drives the
/// declared `safe_flow`. Neither state latches: the first scan `pv`
/// reads `Good` and finite resumes pass-through, and the transition
/// each way is the alarmed event `fallback_active` carries.
///
/// **The held demand.** While passing through, a `Good` finite `in`
/// refreshes the held demand — the last demand the kind stamped
/// `Good`. While the fallback stands it is frozen, so a still-`Good`
/// `in` moving during the engagement does not follow it. `in` never
/// gates the response: an untrusted demand under a `Good` `pv` passes
/// verbatim, marked by the merged quality.
///
/// **Quality.** `out` carries the merged worst of the `in` and `pv`
/// qualities plus `Bad(DeviceFault)` for a non-finite reading the
/// point did not report, so a held or fallback demand stays marked
/// untrusted. `fallback_active` always carries `Quality::Good`: it is
/// the kind's own computed verdict.
///
/// Declared I/O: `in`, `pv` (`In`, `Float`); `out` (`Out`, `Float`),
/// `fallback_active` (`Out`, `Bool`).
///
/// Parameters: `on_bad` — required `Int` carrying a
/// [`FallbackResponse`] code, `0` hold, `1` `fallback_flow`, `2`
/// `safe_flow`; `fallback_flow` and `safe_flow` — required finite
/// `Float`s, the fixed demands the codes emit. All three tune through
/// `set_parameter`.
#[derive(Debug)]
pub struct DemandFallback {
    name: String,
    input: PointId,
    pv: PointId,
    out: PointId,
    fallback_active: PointId,
    config: DemandFallbackConfig,
    /// The last demand the kind stamped `Good` — `None` until the
    /// first; `on_bad` code `0` holds it.
    held_demand: Option<f64>,
}

impl DemandFallbackConfig {
    /// Validates the invariants every construction and tuning path
    /// enforces — both fixed demands finite — reporting a violation as
    /// a [`ParameterError`] naming `component` and the offending
    /// parameter.
    pub(crate) fn checked(component: &str, config: Self) -> Result<Self, ParameterError> {
        for (parameter, value) in [
            ("fallback_flow", config.fallback_flow),
            ("safe_flow", config.safe_flow),
        ] {
            if !value.is_finite() {
                return Err(params::invalid(
                    component,
                    parameter,
                    "must be finite".to_string(),
                ));
            }
        }
        Ok(config)
    }
}

impl DemandFallback {
    /// The component-kind string the model-driven registry maps onto
    /// this type's constructor.
    pub const KIND: &'static str = "demand-fallback";

    /// Builds the component from explicit points and the declared
    /// config, or reports an inconsistent one as a [`ParameterError`].
    pub fn new(
        name: impl Into<String>,
        io: DemandFallbackIo,
        config: DemandFallbackConfig,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        Ok(Self {
            config: DemandFallbackConfig::checked(&name, config)?,
            name,
            input: io.input,
            pv: io.pv,
            out: io.out,
            fallback_active: io.fallback_active,
            held_demand: None,
        })
    }

    /// The declared config — the reported parameter set.
    pub fn config(&self) -> DemandFallbackConfig {
        self.config
    }

    /// Builds the component from a plant-model parameter map, reading
    /// the parameters listed on the type's docs.
    pub fn from_parameters(
        name: impl Into<String>,
        io: DemandFallbackIo,
        parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        let config = DemandFallbackConfig {
            on_bad: {
                let code = params::required_u64(&name, parameters, "on_bad")?;
                FallbackResponse::decode(code as i64).ok_or_else(|| {
                    params::invalid(
                        &name,
                        "on_bad",
                        format!(
                            "expected 0 (hold), 1 (fallback_flow), or 2 (safe_flow), found {code}"
                        ),
                    )
                })?
            },
            fallback_flow: params::required_f64(&name, parameters, "fallback_flow")?,
            safe_flow: params::required_f64(&name, parameters, "safe_flow")?,
        };
        Self::new(name, io, config)
    }

    /// Retunes one declared parameter, or reports a [`CommandError`]
    /// naming `component` and the offending parameter. A refused value
    /// changes nothing.
    fn tune(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        match parameter {
            "on_bad" => {
                let tuned = params::tune_u64(&self.name, parameter, value)?;
                self.config.on_bad = FallbackResponse::decode(tuned as i64).ok_or_else(|| {
                    params::invalid_parameter(
                        &self.name,
                        parameter,
                        "expected 0 (hold), 1 (fallback_flow), or 2 (safe_flow)",
                    )
                })?;
            }
            "fallback_flow" | "safe_flow" => {
                let tuned = params::tune_f64(&self.name, parameter, value)?;
                if !tuned.is_finite() {
                    return Err(params::invalid_parameter(
                        &self.name,
                        parameter,
                        "must be finite",
                    ));
                }
                match parameter {
                    "fallback_flow" => self.config.fallback_flow = tuned,
                    _ => self.config.safe_flow = tuned,
                }
            }
            _ => return Err(params::unknown_parameter(&self.name, parameter)),
        }
        Ok(())
    }
}

impl Component for DemandFallback {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        vec![
            IoRequirement::input::<f64>("in", self.input),
            IoRequirement::input::<f64>("pv", self.pv),
            IoRequirement::output::<f64>("out", self.out),
            IoRequirement::output::<bool>("fallback_active", self.fallback_active),
        ]
    }

    fn step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError> {
        let input = io.read_typed::<f64>(self.input)?;
        let pv = io.read_typed::<f64>(self.pv)?;

        // `out` carries the worst of both inputs, and a non-finite
        // reading is a device fault the point did not report — so a
        // held or fallback demand stays marked untrusted.
        let mut quality = input.quality.merge(pv.quality);
        for sample in [input, pv] {
            if !sample.value.is_finite() {
                quality = quality.merge(Quality::Bad(QualityReason::DeviceFault));
            }
        }

        // A `pv` that is not `Good` and finite cannot vouch the
        // measurement stands: the declared response engages. The held
        // demand is the last `in` the kind stamped `Good` — refreshed
        // only while passing through, so it freezes at the last
        // trusted demand for the engagement's duration.
        let pv_trusted = pv.quality.is_good() && pv.value.is_finite();
        let (out, fallback_active) = if pv_trusted {
            if input.quality.is_good() && input.value.is_finite() {
                self.held_demand = Some(input.value);
            }
            (input.value, false)
        } else {
            let served = match self.config.on_bad {
                FallbackResponse::Hold => self.held_demand.unwrap_or(0.0),
                FallbackResponse::FallbackFlow => self.config.fallback_flow,
                FallbackResponse::SafeFlow => self.config.safe_flow,
            };
            (served, true)
        };

        io.write_sample(self.out, Sample::new(Value::Float(out), quality, tick))?;
        io.write_sample(
            self.fallback_active,
            Sample::good(Value::Bool(fallback_active), tick),
        )?;
        Ok(())
    }

    /// Describes the fallback: `in` is the demand it guards, `pv` the
    /// measurement whose quality drives the response, `out` the served
    /// demand, `fallback_active` the reported engagement. The declared
    /// parameter set is the three keys `from_parameters` reads.
    fn describe(&self) -> ComponentDescriptor {
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &[
                ("in", PortRole::ProcessValue),
                ("pv", PortRole::ProcessValue),
                ("out", PortRole::Output),
                ("fallback_active", PortRole::Status),
            ],
            vec![
                describe::parameter("on_bad", ValueKind::Int, Some(ON_BAD_RANGE)),
                describe::parameter(
                    "fallback_flow",
                    ValueKind::Float,
                    Some(describe::FINITE_F64),
                ),
                describe::parameter("safe_flow", ValueKind::Float, Some(describe::FINITE_F64)),
            ],
        )
    }

    /// Tunes the declared parameters at the scan boundary — the
    /// recorded mechanism for an operator-selectable `on_bad` code and
    /// the declared fixed demands. A refused value changes nothing.
    fn apply_parameter(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        self.tune(parameter, value)
    }

    /// Reports the tuned config — the same fields
    /// [`capture_state`](Self::capture_state) checkpoints, so the
    /// faceplate and a tracking standby read one vocabulary.
    fn report_parameters(&self) -> StateMap {
        let mut parameters = StateMap::new();
        parameters.insert("on_bad", Value::Int(self.config.on_bad.code()));
        parameters.insert("fallback_flow", Value::Float(self.config.fallback_flow));
        parameters.insert("safe_flow", Value::Float(self.config.safe_flow));
        parameters
    }

    /// Captures the tuned config and the held last-good demand — the
    /// state the hold response needs — so a checkpointed standby
    /// continues an engaged fallback identically. A held value never
    /// yet established captures as absent.
    fn capture_state(&self) -> StateMap {
        let mut state = self.report_parameters();
        if let Some(held) = self.held_demand {
            state.insert("held_demand", Value::Float(held));
        }
        state
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        state.ensure_known_fields(
            &self.name,
            &["on_bad", "fallback_flow", "safe_flow", "held_demand"],
        )?;

        // The whole map is validated before any state mutates, so a
        // rejected checkpoint leaves the component on its last-good
        // state rather than half-applied.
        let config = DemandFallbackConfig {
            on_bad: {
                let code = state.require_i64(&self.name, "on_bad")?;
                FallbackResponse::decode(code).ok_or_else(|| StateError::InvalidValue {
                    element: self.name.clone(),
                    field: "on_bad".to_string(),
                    value: Value::Int(code),
                })?
            },
            fallback_flow: state.require_f64(&self.name, "fallback_flow")?,
            safe_flow: state.require_f64(&self.name, "safe_flow")?,
        };
        let held_demand = state.optional_f64(&self.name, "held_demand")?;

        // The same invariants `new` and `apply_parameter` enforce.
        if let Err(ParameterError::Invalid { parameter, .. }) =
            DemandFallbackConfig::checked(&self.name, config)
        {
            let value = match parameter.as_str() {
                "fallback_flow" => config.fallback_flow,
                _ => config.safe_flow,
            };
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: parameter,
                value: Value::Float(value),
            });
        }
        if let Some(held) = held_demand
            && !held.is_finite()
        {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "held_demand".to_string(),
                value: Value::Float(held),
            });
        }

        self.config = config;
        self.held_demand = held_demand;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::TestIo;
    use dcs_core::{Direction, ParameterDescriptor, PortDescriptor};

    const IN: PointId = PointId(10);
    const PV: PointId = PointId(11);
    const OUT: PointId = PointId(20);
    const ACTIVE: PointId = PointId(21);

    fn points() -> DemandFallbackIo {
        DemandFallbackIo {
            input: IN,
            pv: PV,
            out: OUT,
            fallback_active: ACTIVE,
        }
    }

    fn config(on_bad: FallbackResponse) -> DemandFallbackConfig {
        DemandFallbackConfig {
            on_bad,
            fallback_flow: 25.0,
            safe_flow: 5.0,
        }
    }

    /// A holding fallback — the stateful response.
    fn component() -> DemandFallback {
        DemandFallback::new("dfb", points(), config(FallbackResponse::Hold)).unwrap()
    }

    fn io() -> TestIo {
        TestIo::new(&[
            (
                IN,
                Direction::In,
                Sample::good(Value::Float(42.0), Tick::ZERO),
            ),
            (
                PV,
                Direction::In,
                Sample::good(Value::Float(2.0), Tick::ZERO),
            ),
            (
                OUT,
                Direction::Out,
                Sample::good(Value::Float(0.0), Tick::ZERO),
            ),
            (
                ACTIVE,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
        ])
    }

    fn feed(io: &TestIo, point: PointId, value: f64, quality: Quality, tick: u64) {
        io.feed(point, Sample::new(Value::Float(value), quality, Tick(tick)));
    }

    fn out(io: &TestIo) -> Sample {
        io.written(OUT).unwrap()
    }

    fn active(io: &TestIo) -> bool {
        io.written(ACTIVE).unwrap().value == Value::Bool(true)
    }

    #[test]
    fn a_good_pv_passes_the_demand_through() {
        let mut block = component();
        let io = io();
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(out(&io).value, Value::Float(42.0));
        assert_eq!(out(&io).quality, Quality::Good);
        assert!(!active(&io));
    }

    #[test]
    fn each_on_bad_code_answers_a_non_good_pv() {
        for (on_bad, served) in [
            (FallbackResponse::Hold, 0.0),
            (FallbackResponse::FallbackFlow, 25.0),
            (FallbackResponse::SafeFlow, 5.0),
        ] {
            let mut block = DemandFallback::new("dfb", points(), config(on_bad)).unwrap();
            let io = io();
            // No `Good` demand has been stamped yet: the hold serves
            // zero, the fixed codes serve their declared values, and
            // `out` carries the measurement's quality.
            feed(
                &io,
                PV,
                2.0,
                Quality::Bad(QualityReason::CommunicationFault),
                1,
            );
            block.step(&io, Tick(1)).unwrap();
            assert_eq!(out(&io).value, Value::Float(served), "{on_bad:?}");
            assert_eq!(
                out(&io).quality,
                Quality::Bad(QualityReason::CommunicationFault)
            );
            assert!(active(&io));
            assert_eq!(io.written(ACTIVE).unwrap().quality, Quality::Good);
        }
    }

    #[test]
    fn the_hold_freezes_the_last_good_stamped_demand() {
        let mut block = component();
        let io = io();
        block.step(&io, Tick(1)).unwrap();

        // The measurement fails: the held demand serves and the flag
        // asserts — and a still-`Good` demand moving during the
        // engagement does not follow it.
        feed(&io, PV, 2.0, Quality::Bad(QualityReason::DeviceFault), 2);
        block.step(&io, Tick(2)).unwrap();
        assert_eq!(out(&io).value, Value::Float(42.0));
        assert!(active(&io));

        feed(&io, IN, 55.0, Quality::Good, 3);
        block.step(&io, Tick(3)).unwrap();
        assert_eq!(out(&io).value, Value::Float(42.0));
        assert!(active(&io));
    }

    #[test]
    fn out_carries_the_worst_of_its_inputs() {
        let mut block = component();
        let io = io();

        // A `Good` pv under an untrusted demand passes the value
        // verbatim, marked by the merged quality.
        feed(&io, IN, 42.0, Quality::Uncertain(QualityReason::Stale), 1);
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(out(&io).value, Value::Float(42.0));
        assert_eq!(out(&io).quality, Quality::Uncertain(QualityReason::Stale));
        assert!(!active(&io));

        // An `Uncertain` pv under a `Bad` demand: the engagement
        // stands and the `Bad` half wins the merge.
        feed(&io, PV, 2.0, Quality::Uncertain(QualityReason::Stale), 2);
        feed(
            &io,
            IN,
            42.0,
            Quality::Bad(QualityReason::CommunicationFault),
            2,
        );
        block.step(&io, Tick(2)).unwrap();
        assert_eq!(
            out(&io).quality,
            Quality::Bad(QualityReason::CommunicationFault)
        );
        assert!(active(&io));
    }

    #[test]
    fn a_non_finite_pv_counts_as_untrusted() {
        let mut block = component();
        let io = io();
        feed(&io, PV, f64::NAN, Quality::Good, 1);
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(out(&io).value, Value::Float(0.0));
        assert_eq!(out(&io).quality, Quality::Bad(QualityReason::DeviceFault));
        assert!(active(&io));
    }

    #[test]
    fn a_non_finite_demand_is_marked_without_engaging() {
        let mut block = component();
        let io = io();
        feed(&io, IN, f64::NAN, Quality::Good, 1);
        block.step(&io, Tick(1)).unwrap();
        assert!(matches!(out(&io).value, Value::Float(v) if v.is_nan()));
        assert_eq!(out(&io).quality, Quality::Bad(QualityReason::DeviceFault));
        assert!(!active(&io), "the measurement still reads Good");
    }

    #[test]
    fn a_recovered_pv_resumes_pass_through() {
        let mut block = component();
        let io = io();
        block.step(&io, Tick(1)).unwrap();
        feed(&io, PV, 2.0, Quality::Bad(QualityReason::DeviceFault), 2);
        block.step(&io, Tick(2)).unwrap();
        assert!(active(&io));

        // The first `Good` scan resumes pass-through and clears the
        // flag — no latch, no soak.
        feed(&io, PV, 2.0, Quality::Good, 3);
        block.step(&io, Tick(3)).unwrap();
        assert_eq!(out(&io).value, Value::Float(42.0));
        assert_eq!(out(&io).quality, Quality::Good);
        assert!(!active(&io));
    }

    #[test]
    fn a_restored_fallback_resumes_the_held_demand_identically() {
        let mut block = component();
        let io = io();
        block.step(&io, Tick(1)).unwrap();

        // Capture mid-engagement: the held demand and the tuned config
        // are the run state a standby inherits.
        feed(&io, PV, 2.0, Quality::Bad(QualityReason::DeviceFault), 2);
        block.step(&io, Tick(2)).unwrap();
        let state = block.capture_state();
        assert_eq!(state.get("held_demand"), Some(Value::Float(42.0)));

        let mut standby = component();
        standby.restore_state(&state).unwrap();
        assert_eq!(standby.capture_state(), state);

        // The standby holds the captured demand; recovery resumes
        // pass-through identically.
        feed(&io, PV, 2.0, Quality::Bad(QualityReason::DeviceFault), 3);
        standby.step(&io, Tick(3)).unwrap();
        assert_eq!(out(&io).value, Value::Float(42.0));
        assert!(active(&io));
        feed(&io, PV, 2.0, Quality::Good, 4);
        standby.step(&io, Tick(4)).unwrap();
        assert_eq!(out(&io).value, Value::Float(42.0));
        assert!(!active(&io));
    }

    #[test]
    fn restore_validates_the_captured_vocabulary() {
        let mut block = component();
        let valid = block.capture_state();
        assert_eq!(valid.get("held_demand"), None);

        // Foreign and missing fields are named errors.
        let mut foreign = valid.clone();
        foreign.insert("foreign", Value::Int(1));
        assert!(matches!(
            block.restore_state(&foreign),
            Err(StateError::UnknownField { ref field, .. }) if field == "foreign"
        ));
        assert!(matches!(
            block.restore_state(&StateMap::new()),
            Err(StateError::MissingField { ref field, .. }) if field == "on_bad"
        ));

        // Mistyped and out-of-range fields are named errors, and a
        // rejected map changes nothing.
        let mut mistyped = valid.clone();
        mistyped.insert("fallback_flow", Value::Int(1));
        assert!(matches!(
            block.restore_state(&mistyped),
            Err(StateError::IncompatibleField { ref field, .. }) if field == "fallback_flow"
        ));
        let mut invalid = valid.clone();
        invalid.insert("safe_flow", Value::Float(f64::NAN));
        assert!(matches!(
            block.restore_state(&invalid),
            Err(StateError::InvalidValue { ref field, .. }) if field == "safe_flow"
        ));
        let mut bad_code = valid.clone();
        bad_code.insert("on_bad", Value::Int(4));
        assert!(matches!(
            block.restore_state(&bad_code),
            Err(StateError::InvalidValue { ref field, .. }) if field == "on_bad"
        ));
        let mut bad_held = valid.clone();
        bad_held.insert("held_demand", Value::Float(f64::INFINITY));
        assert!(matches!(
            block.restore_state(&bad_held),
            Err(StateError::InvalidValue { ref field, .. }) if field == "held_demand"
        ));
        assert_eq!(block.capture_state(), valid);
    }

    #[test]
    fn builds_from_parameter_map() {
        let parameters: Parameters = [
            ("on_bad".to_string(), Value::Int(2)),
            ("fallback_flow".to_string(), Value::Float(25.0)),
            ("safe_flow".to_string(), Value::Float(5.0)),
        ]
        .into_iter()
        .collect();
        let block = DemandFallback::from_parameters("dfb", points(), &parameters).unwrap();
        assert_eq!(block.config.on_bad, FallbackResponse::SafeFlow);
        assert_eq!(block.config.fallback_flow, 25.0);
        assert_eq!(block.config.safe_flow, 5.0);

        for (parameters, parameter) in [
            (Parameters::new(), "on_bad"),
            (
                [("on_bad".to_string(), Value::Int(3))]
                    .into_iter()
                    .chain([
                        ("fallback_flow".to_string(), Value::Float(25.0)),
                        ("safe_flow".to_string(), Value::Float(5.0)),
                    ])
                    .collect(),
                "on_bad",
            ),
            (
                [
                    ("on_bad".to_string(), Value::Int(0)),
                    ("fallback_flow".to_string(), Value::Float(f64::NAN)),
                    ("safe_flow".to_string(), Value::Float(5.0)),
                ]
                .into_iter()
                .collect(),
                "fallback_flow",
            ),
            (
                [
                    ("on_bad".to_string(), Value::Int(0)),
                    ("fallback_flow".to_string(), Value::Float(25.0)),
                ]
                .into_iter()
                .collect(),
                "safe_flow",
            ),
        ] {
            let parameters: Parameters = parameters;
            assert!(matches!(
                DemandFallback::from_parameters("dfb", points(), &parameters).unwrap_err(),
                ParameterError::Missing { parameter: ref p, .. }
                | ParameterError::Invalid { parameter: ref p, .. } if p == parameter
            ));
        }
    }

    #[test]
    fn tunes_declared_parameters_at_the_scan_boundary() {
        let mut block = component();
        let io = io();

        block.apply_parameter("on_bad", Value::Int(1)).unwrap();
        block
            .apply_parameter("fallback_flow", Value::Float(30.0))
            .unwrap();
        block
            .apply_parameter("safe_flow", Value::Float(2.0))
            .unwrap();
        assert_eq!(block.config.on_bad, FallbackResponse::FallbackFlow);
        assert_eq!(block.config.fallback_flow, 30.0);
        assert_eq!(block.config.safe_flow, 2.0);

        // A refused value changes nothing, naming the parameter.
        assert!(matches!(
            block.apply_parameter("on_bad", Value::Int(4)),
            Err(CommandError::InvalidParameter { ref parameter, .. }) if parameter == "on_bad"
        ));
        assert!(matches!(
            block.apply_parameter("safe_flow", Value::Float(f64::NAN)),
            Err(CommandError::InvalidParameter { ref parameter, .. }) if parameter == "safe_flow"
        ));
        assert!(matches!(
            block.apply_parameter("nope", Value::Int(0)),
            Err(CommandError::UnknownParameter { ref parameter, .. }) if parameter == "nope"
        ));
        assert_eq!(block.config.on_bad, FallbackResponse::FallbackFlow);

        // The tuned response engages on the next scan.
        feed(&io, PV, 2.0, Quality::Bad(QualityReason::DeviceFault), 1);
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(out(&io).value, Value::Float(30.0));
        assert!(active(&io));
    }

    #[test]
    fn describes_itself() {
        let descriptor = component().describe();
        assert_eq!(descriptor.name, "dfb");
        assert_eq!(descriptor.kind, DemandFallback::KIND);
        assert_eq!(descriptor.label, "dfb");
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
                    name: "pv".to_string(),
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
                    name: "fallback_active".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Status),
                    point: None,
                },
            ]
        );
        assert_eq!(
            descriptor.parameters,
            [
                ParameterDescriptor {
                    name: "on_bad".to_string(),
                    kind: ValueKind::Int,
                    range: Some(ON_BAD_RANGE),
                },
                ParameterDescriptor {
                    name: "fallback_flow".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::FINITE_F64),
                },
                ParameterDescriptor {
                    name: "safe_flow".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::FINITE_F64),
                },
            ]
        );

        // The descriptor's names are exactly the `from_parameters`
        // key set.
        let mut keyless = Parameters::new();
        for parameter in &descriptor.parameters {
            let value = match parameter.name.as_str() {
                "on_bad" => Value::Int(0),
                "fallback_flow" | "safe_flow" => Value::Float(1.0),
                other => panic!("undocumented parameter {other}"),
            };
            keyless.insert(parameter.name.clone(), value);
        }
        DemandFallback::from_parameters("dfb", points(), &keyless).unwrap();
    }

    #[test]
    fn identical_input_sequences_produce_identical_state() {
        let run = || {
            let mut block = component();
            let io = io();
            let mut written = Vec::new();
            for (tick, demand, quality) in [
                (1, 42.0, Quality::Good),
                (2, 55.0, Quality::Bad(QualityReason::DeviceFault)),
                (3, 55.0, Quality::Bad(QualityReason::DeviceFault)),
                (4, 55.0, Quality::Good),
            ] {
                feed(&io, IN, demand, Quality::Good, tick);
                feed(&io, PV, 2.0, quality, tick);
                block.step(&io, Tick(tick)).unwrap();
                written.push((out(&io), io.written(ACTIVE).unwrap()));
            }
            (block.capture_state(), written)
        };
        assert_eq!(run(), run());
    }
}
