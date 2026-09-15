//! Surge guard: the machine-protection demand bound architecture
//! decision 64 records for `WW-CTL-005`
//! (`docs/research/aeration-do-control.md`) — the two-variable
//! flow-versus-pressure surge-region guard on a blower's capacity
//! demand, the algorithm-side layer between the machine's hardwired
//! protective devices and the control algorithm.

use crate::describe;
use crate::params::{self, ParameterError, Parameters};
use dcs_core::{
    CommandError, ComponentDescriptor, ParameterRange, PointId, PortRole, Quality, QualityReason,
    Sample, StateError, StateMap, Tick, Value, ValueKind,
};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// What a [`SurgeGuard`] does when the operating point crosses a
/// declared bound into the surge region.
///
/// The model's parameter vocabulary has no string type, so the
/// `on_guard` parameter carries the choice as an `Int` code: `0` for
/// `Clamp`, `1` for `Trip` — the values [`code`](Self::code) reports
/// and [`decode`](Self::decode) accepts.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GuardResponse {
    /// Clamp `out` at the crossed bound and continue — the
    /// minimum-flow maintenance the evidence prescribes: each crossed
    /// lower bound floors the demand at its declared value, a crossed
    /// pressure bound caps it.
    Clamp,
    /// Trip the machine: `out` drives the declared `trip_value` and
    /// `tripped` asserts.
    Trip,
}

impl GuardResponse {
    /// The `Int` code the `on_guard` parameter carries.
    pub const fn code(self) -> i64 {
        match self {
            Self::Clamp => 0,
            Self::Trip => 1,
        }
    }

    /// The response the `Int` code `code` selects, or `None` when the
    /// code declares no response.
    pub const fn decode(code: i64) -> Option<Self> {
        match code {
            0 => Some(Self::Clamp),
            1 => Some(Self::Trip),
            _ => None,
        }
    }
}

/// The declared surge-region bounds and guard response a
/// [`SurgeGuard`] runs under — its parameter map's five keys as one
/// value.
///
/// `min_flow`, `max_pressure`, and `min_current` are the declared
/// surge-region bounds — machine data, each finite and non-negative:
/// the operating point sits inside the region while `flow` reads
/// below `min_flow`, `pressure` reads above `max_pressure`, or a bound
/// `current` reads below `min_current` (the minimum-amperage proxy).
/// The bounds double as the clamp's demand limits under
/// [`GuardResponse::Clamp`]: a crossed `min_flow` or `min_current`
/// floors `out` at the bound, a crossed `max_pressure` caps it.
/// `on_guard` names the bound-crossing response and `trip_value` is
/// the demand a trip emits — the unload/vent direction the machine's
/// shutdown requires — finite but otherwise unbounded.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct SurgeGuardConfig {
    /// The declared minimum-flow bound — the low-flow edge of the
    /// surge region and the clamp's demand floor.
    pub min_flow: f64,
    /// The declared maximum-pressure bound — the high-pressure edge
    /// of the surge region and the clamp's demand ceiling.
    pub max_pressure: f64,
    /// The declared minimum-current bound — the minimum-amperage
    /// surge proxy, evaluated only while the `current` port binds.
    pub min_current: f64,
    /// The declared bound-crossing response: clamp the demand at the
    /// bound and continue, or trip to `trip_value`.
    pub on_guard: GuardResponse,
    /// The demand a trip emits — the unload/vent direction the
    /// machine's shutdown requires.
    pub trip_value: f64,
}

/// The points a [`SurgeGuard`] binds — the demand it guards, the
/// surge-region measurements, the proven-trip status it honors (`In`),
/// and the guarded demand plus the two condition reports it drives
/// (`Out`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SurgeGuardIo {
    /// `demand` (`In`, `Float`): the unit's capacity demand —
    /// conventionally the `blower-group`'s `capacity_i`.
    pub demand: PointId,
    /// `flow` (`In`, `Float`): the unit's discharge airflow.
    pub flow: PointId,
    /// `pressure` (`In`, `Float`): the discharge or header pressure.
    pub pressure: PointId,
    /// `current` (`In`, `Float`): the motor current — the
    /// minimum-amperage surge proxy — `None` for a unit without the
    /// signal; the port is declared only where the model wires one.
    pub current: Option<PointId>,
    /// `surge_trip` (`In`, `Bool`): the machine's own proven-surge
    /// protective-device status — hardwired equipment reporting, never
    /// re-implemented.
    pub surge_trip: PointId,
    /// `out` (`Out`, `Float`): the guarded demand feeding the
    /// machine's actuation path.
    pub out: PointId,
    /// `guarding` (`Out`, `Bool`): asserted while a declared bound is
    /// crossed — the operating point inside the surge region.
    pub guarding: PointId,
    /// `tripped` (`Out`, `Bool`): asserted while a proven surge, a
    /// declared trip response, or an untrusted demand drives
    /// `trip_value` — the output the alarm set consumes.
    pub tripped: PointId,
}

/// The inclusive `Int` bound the `on_guard` parameter accepts: the
/// declared [`GuardResponse`] codes.
const ON_GUARD_RANGE: ParameterRange = ParameterRange {
    min: Value::Int(0),
    max: Value::Int(1),
};

/// A surge guard: bounds a blower's capacity `demand` (`In`, `Float`)
/// against the declared two-variable surge region — `flow` below
/// `min_flow`, `pressure` above `max_pressure`, a bound `current`
/// below `min_current` — honoring the machine's proven `surge_trip`
/// (`In`, `Bool`), and drives `out` (`Out`, `Float`), `guarding`
/// (`Out`, `Bool`), and `tripped` (`Out`, `Bool`).
///
/// **The region.** `flow`, `pressure`, and — where the model wires it
/// — `current` are the surge-region measurements: the operating point
/// sits inside the declared region while `flow < min_flow`,
/// `pressure > max_pressure`, or `current < min_current`. `guarding`
/// asserts while any bound is crossed, whatever the response.
///
/// **The response.** While the point sits outside the region the
/// `demand` passes to `out` unmodified. A crossing engages the
/// declared `on_guard` response: under code `0` (`Clamp`) each crossed
/// bound clamps `out` at its declared value — `min_flow` and
/// `min_current` floor the demand, `max_pressure` caps it — and the
/// machine keeps running at the bound; under code `1` (`Trip`) `out`
/// drives `trip_value` and `tripped` asserts. A `surge_trip` reading
/// `true` with `Good` quality is the hardwired protective device's
/// proven trip: `out` drives `trip_value` and `tripped` asserts
/// unconditionally, whatever the demand or the region. Neither state
/// latches — the kind auto-clears with the inputs, and a plant needing
/// a held lockout wires an `sr-latch` downstream per the decision.
///
/// **Fail-safe rules.** An input that is not `Good` — or a `Float`
/// input whose value is not finite — cannot vouch the machine sits
/// outside the region, so each measurement reads fail-safe: an
/// untrusted `flow` reads below `min_flow`, an untrusted `pressure`
/// above `max_pressure`, an untrusted bound `current` below
/// `min_current`, and a non-`Good` `surge_trip` reads as proven. An
/// untrusted `demand` can neither pass nor be bounded — the guard
/// drives `trip_value` and asserts `tripped`, the safe-demand answer
/// the interlock convention records.
///
/// **Quality.** `out` carries the merged worst of every bound input's
/// quality — `demand`, `flow`, `pressure`, `surge_trip`, and `current`
/// while bound — plus `Bad(DeviceFault)` for a non-finite reading the
/// point did not report, so a fail-safe response stays marked
/// untrusted. `guarding` and `tripped` always carry `Quality::Good`:
/// each is the kind's own computed verdict.
///
/// Declared I/O: `demand`, `flow`, `pressure` (`In`, `Float`),
/// `current` (`In`, `Float` — optional, declared only where bound),
/// `surge_trip` (`In`, `Bool`); `out` (`Out`, `Float`), `guarding`,
/// `tripped` (`Out`, `Bool`).
///
/// Parameters: `min_flow`, `max_pressure`, `min_current` — required
/// non-negative finite `Float`s, the declared surge-region bounds;
/// `on_guard` — required `Int` carrying a [`GuardResponse`] code, `0`
/// clamp or `1` trip; `trip_value` — required finite `Float`, the
/// demand a trip emits. All five tune through `set_parameter`.
#[derive(Debug)]
pub struct SurgeGuard {
    name: String,
    demand: PointId,
    flow: PointId,
    pressure: PointId,
    current: Option<PointId>,
    surge_trip: PointId,
    out: PointId,
    guarding: PointId,
    tripped: PointId,
    config: SurgeGuardConfig,
}

impl SurgeGuardConfig {
    /// Validates the invariants every construction and tuning path
    /// enforces — the bounds finite and non-negative, `trip_value`
    /// finite — reporting a violation as a [`ParameterError`] naming
    /// `component` and the offending parameter.
    pub(crate) fn checked(component: &str, config: Self) -> Result<Self, ParameterError> {
        for (parameter, value) in [
            ("min_flow", config.min_flow),
            ("max_pressure", config.max_pressure),
            ("min_current", config.min_current),
        ] {
            if !value.is_finite() {
                return Err(params::invalid(
                    component,
                    parameter,
                    "must be finite".to_string(),
                ));
            }
            if value < 0.0 {
                return Err(params::invalid(
                    component,
                    parameter,
                    "must be non-negative".to_string(),
                ));
            }
        }
        if !config.trip_value.is_finite() {
            return Err(params::invalid(
                component,
                "trip_value",
                "must be finite".to_string(),
            ));
        }
        Ok(config)
    }
}

impl SurgeGuard {
    /// The component-kind string the model-driven registry maps onto
    /// this type's constructor.
    pub const KIND: &'static str = "surge-guard";

    /// Builds the component from explicit points and the declared
    /// config, or reports an inconsistent one as a [`ParameterError`].
    /// `io.current` is `None` for an instance the model leaves
    /// unwired.
    pub fn new(
        name: impl Into<String>,
        io: SurgeGuardIo,
        config: SurgeGuardConfig,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        Ok(Self {
            config: SurgeGuardConfig::checked(&name, config)?,
            name,
            demand: io.demand,
            flow: io.flow,
            pressure: io.pressure,
            current: io.current,
            surge_trip: io.surge_trip,
            out: io.out,
            guarding: io.guarding,
            tripped: io.tripped,
        })
    }

    /// The declared config — the reported parameter set.
    pub fn config(&self) -> SurgeGuardConfig {
        self.config
    }

    /// Builds the component from a plant-model parameter map, reading
    /// the parameters listed on the type's docs.
    pub fn from_parameters(
        name: impl Into<String>,
        io: SurgeGuardIo,
        parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        let config = SurgeGuardConfig {
            min_flow: params::required_f64(&name, parameters, "min_flow")?,
            max_pressure: params::required_f64(&name, parameters, "max_pressure")?,
            min_current: params::required_f64(&name, parameters, "min_current")?,
            on_guard: {
                let code = params::required_u64(&name, parameters, "on_guard")?;
                GuardResponse::decode(code as i64).ok_or_else(|| {
                    params::invalid(
                        &name,
                        "on_guard",
                        format!("expected 0 (clamp) or 1 (trip), found {code}"),
                    )
                })?
            },
            trip_value: params::required_f64(&name, parameters, "trip_value")?,
        };
        Self::new(name, io, config)
    }

    /// Retunes one declared parameter, or reports a [`CommandError`]
    /// naming `component` and the offending parameter. A refused value
    /// changes nothing.
    fn tune(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        match parameter {
            "min_flow" | "max_pressure" | "min_current" => {
                let tuned = params::tune_f64(&self.name, parameter, value)?;
                if !tuned.is_finite() || tuned < 0.0 {
                    return Err(params::invalid_parameter(
                        &self.name,
                        parameter,
                        "must be finite and non-negative",
                    ));
                }
                match parameter {
                    "min_flow" => self.config.min_flow = tuned,
                    "max_pressure" => self.config.max_pressure = tuned,
                    _ => self.config.min_current = tuned,
                }
            }
            "on_guard" => {
                let tuned = params::tune_u64(&self.name, parameter, value)?;
                self.config.on_guard = GuardResponse::decode(tuned as i64).ok_or_else(|| {
                    params::invalid_parameter(
                        &self.name,
                        parameter,
                        "expected 0 (clamp) or 1 (trip)",
                    )
                })?;
            }
            "trip_value" => {
                let tuned = params::tune_f64(&self.name, parameter, value)?;
                if !tuned.is_finite() {
                    return Err(params::invalid_parameter(
                        &self.name,
                        parameter,
                        "must be finite",
                    ));
                }
                self.config.trip_value = tuned;
            }
            _ => return Err(params::unknown_parameter(&self.name, parameter)),
        }
        Ok(())
    }
}

impl Component for SurgeGuard {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        let mut requirements = vec![
            IoRequirement::input::<f64>("demand", self.demand),
            IoRequirement::input::<f64>("flow", self.flow),
            IoRequirement::input::<f64>("pressure", self.pressure),
        ];
        // `current` is the optional port: declared only where the
        // model wired one, so an unwired instance's descriptor and I/O
        // scope carry no `current`.
        if let Some(current) = self.current {
            requirements.push(IoRequirement::input::<f64>("current", current));
        }
        requirements.extend([
            IoRequirement::input::<bool>("surge_trip", self.surge_trip),
            IoRequirement::output::<f64>("out", self.out),
            IoRequirement::output::<bool>("guarding", self.guarding),
            IoRequirement::output::<bool>("tripped", self.tripped),
        ]);
        requirements
    }

    fn step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError> {
        let demand = io.read_typed::<f64>(self.demand)?;
        let flow = io.read_typed::<f64>(self.flow)?;
        let pressure = io.read_typed::<f64>(self.pressure)?;
        let current = match self.current {
            Some(point) => Some(io.read_typed::<f64>(point)?),
            None => None,
        };
        let surge_trip = io.read_typed::<bool>(self.surge_trip)?;

        // `out` carries the worst of every contributing input, and a
        // non-finite reading is a device fault the point did not
        // report — so a fail-safe response stays marked untrusted.
        let mut quality = demand
            .quality
            .merge(flow.quality)
            .merge(pressure.quality)
            .merge(surge_trip.quality);
        if let Some(current) = current {
            quality = quality.merge(current.quality);
        }
        for sample in [demand, flow, pressure].into_iter().chain(current) {
            if !sample.value.is_finite() {
                quality = quality.merge(Quality::Bad(QualityReason::DeviceFault));
            }
        }

        // The fail-safe readings: an untrusted measurement cannot
        // vouch the machine sits outside the region, so each reads as
        // its bound crossed — flow low, pressure high, current low.
        let flow_low = !(flow.quality.is_good() && flow.value.is_finite())
            || flow.value < self.config.min_flow;
        let pressure_high = !(pressure.quality.is_good() && pressure.value.is_finite())
            || pressure.value > self.config.max_pressure;
        let current_low = match current {
            Some(sample) => {
                !(sample.quality.is_good() && sample.value.is_finite())
                    || sample.value < self.config.min_current
            }
            None => false,
        };
        let guarding = flow_low || pressure_high || current_low;

        // The proven trip: a `Good` true is the hardwired device's
        // report; a non-`Good` status cannot prove the device stands
        // down and reads as proven — the interlock trip convention.
        let proven_trip = surge_trip.value || !surge_trip.quality.is_good();
        // An untrusted demand can neither pass nor be bounded: the
        // declared safe demand drives.
        let demand_trusted = demand.quality.is_good() && demand.value.is_finite();

        let (out, tripped) = if proven_trip || !demand_trusted {
            (self.config.trip_value, true)
        } else if guarding {
            match self.config.on_guard {
                GuardResponse::Trip => (self.config.trip_value, true),
                GuardResponse::Clamp => {
                    // Clamp `out` at each crossed bound: the lower
                    // bounds floor the demand, the pressure bound caps
                    // it — the minimum-flow maintenance direction.
                    let mut out = demand.value;
                    if flow_low {
                        out = out.max(self.config.min_flow);
                    }
                    if current_low {
                        out = out.max(self.config.min_current);
                    }
                    if pressure_high {
                        out = out.min(self.config.max_pressure);
                    }
                    (out, false)
                }
            }
        } else {
            (demand.value, false)
        };

        io.write_sample(self.out, Sample::new(Value::Float(out), quality, tick))?;
        io.write_sample(self.guarding, Sample::good(Value::Bool(guarding), tick))?;
        io.write_sample(self.tripped, Sample::good(Value::Bool(tripped), tick))?;
        Ok(())
    }

    /// Describes the guard: `demand` is the guarded value, `flow`,
    /// `pressure`, and a bound `current` are the surge-region
    /// measurements, `surge_trip` the reported protective-device
    /// status, `out` the driven demand, `guarding` and `tripped` the
    /// reported conditions; `current` joins the descriptor only where
    /// bound. The declared parameter set is the five keys
    /// `from_parameters` reads.
    fn describe(&self) -> ComponentDescriptor {
        let mut roles: Vec<(&str, PortRole)> = vec![
            ("demand", PortRole::ProcessValue),
            ("flow", PortRole::ProcessValue),
            ("pressure", PortRole::ProcessValue),
        ];
        if self.current.is_some() {
            roles.push(("current", PortRole::ProcessValue));
        }
        roles.extend([
            ("surge_trip", PortRole::Status),
            ("out", PortRole::Output),
            ("guarding", PortRole::Status),
            ("tripped", PortRole::Status),
        ]);
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &roles,
            vec![
                describe::parameter(
                    "min_flow",
                    ValueKind::Float,
                    Some(describe::NONNEGATIVE_F64),
                ),
                describe::parameter(
                    "max_pressure",
                    ValueKind::Float,
                    Some(describe::NONNEGATIVE_F64),
                ),
                describe::parameter(
                    "min_current",
                    ValueKind::Float,
                    Some(describe::NONNEGATIVE_F64),
                ),
                describe::parameter("on_guard", ValueKind::Int, Some(ON_GUARD_RANGE)),
                describe::parameter("trip_value", ValueKind::Float, Some(describe::FINITE_F64)),
            ],
        )
    }

    /// Tunes the declared parameters at the scan boundary — the
    /// recorded mechanism for operator-adjustable bounds and guard
    /// response. A refused value changes nothing.
    fn apply_parameter(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        self.tune(parameter, value)
    }

    /// Reports the tuned config — the same fields
    /// [`capture_state`](Self::capture_state) checkpoints, so the
    /// faceplate and a tracking standby read one vocabulary.
    fn report_parameters(&self) -> StateMap {
        let mut parameters = StateMap::new();
        parameters.insert("min_flow", Value::Float(self.config.min_flow));
        parameters.insert("max_pressure", Value::Float(self.config.max_pressure));
        parameters.insert("min_current", Value::Float(self.config.min_current));
        parameters.insert("on_guard", Value::Int(self.config.on_guard.code()));
        parameters.insert("trip_value", Value::Float(self.config.trip_value));
        parameters
    }

    /// Captures the tuned parameters — the guard is otherwise
    /// stateless, every output a pure function of the current inputs,
    /// but runtime tuning is run state a standby must inherit.
    fn capture_state(&self) -> StateMap {
        self.report_parameters()
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        state.ensure_known_fields(
            &self.name,
            &[
                "min_flow",
                "max_pressure",
                "min_current",
                "on_guard",
                "trip_value",
            ],
        )?;

        // The whole map is validated before any state mutates, so a
        // rejected checkpoint leaves the component on its last-good
        // state rather than half-applied.
        let config = SurgeGuardConfig {
            min_flow: state.require_f64(&self.name, "min_flow")?,
            max_pressure: state.require_f64(&self.name, "max_pressure")?,
            min_current: state.require_f64(&self.name, "min_current")?,
            on_guard: {
                let code = state.require_i64(&self.name, "on_guard")?;
                GuardResponse::decode(code).ok_or_else(|| StateError::InvalidValue {
                    element: self.name.clone(),
                    field: "on_guard".to_string(),
                    value: Value::Int(code),
                })?
            },
            trip_value: state.require_f64(&self.name, "trip_value")?,
        };

        // The same invariants `new` and `apply_parameter` enforce.
        if let Err(ParameterError::Invalid { parameter, .. }) =
            SurgeGuardConfig::checked(&self.name, config)
        {
            let value = match parameter.as_str() {
                "trip_value" => config.trip_value,
                "min_flow" => config.min_flow,
                "max_pressure" => config.max_pressure,
                _ => config.min_current,
            };
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: parameter,
                value: Value::Float(value),
            });
        }

        self.config = config;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::TestIo;
    use dcs_core::{Direction, ParameterDescriptor, PortDescriptor};

    const DEMAND: PointId = PointId(60);
    const FLOW: PointId = PointId(61);
    const PRESSURE: PointId = PointId(62);
    const CURRENT: PointId = PointId(63);
    const SURGE_TRIP: PointId = PointId(64);
    const OUT: PointId = PointId(70);
    const GUARDING: PointId = PointId(71);
    const TRIPPED: PointId = PointId(72);

    fn points(current: Option<PointId>) -> SurgeGuardIo {
        SurgeGuardIo {
            demand: DEMAND,
            flow: FLOW,
            pressure: PRESSURE,
            current,
            surge_trip: SURGE_TRIP,
            out: OUT,
            guarding: GUARDING,
            tripped: TRIPPED,
        }
    }

    fn config(on_guard: GuardResponse) -> SurgeGuardConfig {
        SurgeGuardConfig {
            min_flow: 50.0,
            max_pressure: 30.0,
            min_current: 40.0,
            on_guard,
            trip_value: 0.0,
        }
    }

    /// A clamping guard with `current` bound — the richest instance.
    fn component() -> SurgeGuard {
        SurgeGuard::new("sg", points(Some(CURRENT)), config(GuardResponse::Clamp)).unwrap()
    }

    fn io(current: bool) -> TestIo {
        let mut points = vec![
            (
                DEMAND,
                Direction::In,
                Sample::good(Value::Float(70.0), Tick::ZERO),
            ),
            (
                FLOW,
                Direction::In,
                Sample::good(Value::Float(100.0), Tick::ZERO),
            ),
            (
                PRESSURE,
                Direction::In,
                Sample::good(Value::Float(20.0), Tick::ZERO),
            ),
            (
                SURGE_TRIP,
                Direction::In,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
            (
                OUT,
                Direction::Out,
                Sample::good(Value::Float(0.0), Tick::ZERO),
            ),
            (
                GUARDING,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
            (
                TRIPPED,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
        ];
        if current {
            points.insert(
                3,
                (
                    CURRENT,
                    Direction::In,
                    Sample::good(Value::Float(60.0), Tick::ZERO),
                ),
            );
        }
        TestIo::new(&points)
    }

    /// Feeds the standing trusted inside-region-clear readings.
    fn feed(io: &TestIo, demand: f64, flow: f64, pressure: f64, trip: bool, tick: u64) {
        io.feed(DEMAND, Sample::good(Value::Float(demand), Tick(tick)));
        io.feed(FLOW, Sample::good(Value::Float(flow), Tick(tick)));
        io.feed(PRESSURE, Sample::good(Value::Float(pressure), Tick(tick)));
        io.feed(SURGE_TRIP, Sample::good(Value::Bool(trip), Tick(tick)));
    }

    fn out(io: &TestIo) -> Sample {
        io.written(OUT).unwrap()
    }

    fn guarding(io: &TestIo) -> bool {
        io.written(GUARDING).unwrap().value == Value::Bool(true)
    }

    fn tripped(io: &TestIo) -> bool {
        io.written(TRIPPED).unwrap().value == Value::Bool(true)
    }

    #[test]
    fn demand_passes_unmodified_inside_the_region() {
        let mut block = component();
        let io = io(true);
        feed(&io, 70.0, 100.0, 20.0, false, 1);
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(out(&io).value, Value::Float(70.0));
        assert_eq!(out(&io).quality, Quality::Good);
        assert!(!guarding(&io));
        assert!(!tripped(&io));

        // Boundary readings are outside the region: the bounds are
        // strict inequalities.
        feed(&io, 45.0, 50.0, 30.0, false, 2);
        block.step(&io, Tick(2)).unwrap();
        assert_eq!(out(&io).value, Value::Float(45.0));
        assert!(!guarding(&io));
    }

    #[test]
    fn each_bound_crossing_clamps_under_the_clamp_response() {
        let mut block = component();
        let io = io(true);

        // Low flow floors the demand at `min_flow` — the minimum-flow
        // maintenance direction; a demand already above the floor
        // passes while the bound still reports engaged.
        feed(&io, 30.0, 40.0, 20.0, false, 1);
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(out(&io).value, Value::Float(50.0));
        assert!(guarding(&io));
        assert!(!tripped(&io));
        feed(&io, 80.0, 40.0, 20.0, false, 2);
        block.step(&io, Tick(2)).unwrap();
        assert_eq!(out(&io).value, Value::Float(80.0));
        assert!(guarding(&io));

        // High pressure caps the demand at `max_pressure`; with the
        // low flow still standing the floor applies first and the cap
        // follows it.
        feed(&io, 80.0, 40.0, 35.0, false, 3);
        block.step(&io, Tick(3)).unwrap();
        assert_eq!(out(&io).value, Value::Float(30.0));
        assert!(guarding(&io));

        // Low current floors at `min_current` once the other bounds
        // clear.
        feed(&io, 20.0, 100.0, 20.0, false, 4);
        io.feed(CURRENT, Sample::good(Value::Float(30.0), Tick(4)));
        block.step(&io, Tick(4)).unwrap();
        assert_eq!(out(&io).value, Value::Float(40.0));
        assert!(guarding(&io));
        assert!(!tripped(&io));

        // Every bound clear: pass-through resumes the same scan — no
        // latch.
        io.feed(CURRENT, Sample::good(Value::Float(60.0), Tick(5)));
        feed(&io, 45.0, 100.0, 20.0, false, 5);
        block.step(&io, Tick(5)).unwrap();
        assert_eq!(out(&io).value, Value::Float(45.0));
        assert!(!guarding(&io));
    }

    #[test]
    fn each_bound_crossing_trips_under_the_trip_response() {
        let mut block =
            SurgeGuard::new("sg", points(Some(CURRENT)), config(GuardResponse::Trip)).unwrap();
        let io = io(true);

        for (tick, demand, flow, pressure, current) in [
            (1, 30.0, 40.0, 20.0, 60.0),  // flow bound
            (2, 30.0, 100.0, 35.0, 60.0), // pressure bound
            (3, 30.0, 100.0, 20.0, 30.0), // current bound
        ] {
            feed(&io, demand, flow, pressure, false, tick);
            io.feed(CURRENT, Sample::good(Value::Float(current), Tick(tick)));
            block.step(&io, Tick(tick)).unwrap();
            assert_eq!(out(&io).value, Value::Float(0.0), "tick={tick}");
            assert!(guarding(&io), "tick={tick}");
            assert!(tripped(&io), "tick={tick}");
        }

        // Clear of every bound the guard auto-resets: pass-through
        // resumes, both reports clear.
        feed(&io, 45.0, 100.0, 20.0, false, 4);
        io.feed(CURRENT, Sample::good(Value::Float(60.0), Tick(4)));
        block.step(&io, Tick(4)).unwrap();
        assert_eq!(out(&io).value, Value::Float(45.0));
        assert!(!guarding(&io));
        assert!(!tripped(&io));
    }

    #[test]
    fn a_proven_surge_trip_drives_trip_value_unconditionally() {
        for response in [GuardResponse::Clamp, GuardResponse::Trip] {
            let mut block = SurgeGuard::new("sg", points(Some(CURRENT)), config(response)).unwrap();
            let io = io(true);

            // Inside the region or out, whatever the demand reads, a
            // proven trip overrides.
            feed(&io, 70.0, 100.0, 20.0, true, 1);
            block.step(&io, Tick(1)).unwrap();
            assert_eq!(out(&io).value, Value::Float(0.0));
            assert!(!guarding(&io), "the region is clear — no bound engaged");
            assert!(tripped(&io));

            feed(&io, 30.0, 40.0, 20.0, true, 2);
            block.step(&io, Tick(2)).unwrap();
            assert_eq!(out(&io).value, Value::Float(0.0));
            assert!(guarding(&io), "the flow bound still reports engaged");
            assert!(tripped(&io));

            // The status auto-clears with the device.
            feed(&io, 45.0, 100.0, 20.0, false, 3);
            block.step(&io, Tick(3)).unwrap();
            assert_eq!(out(&io).value, Value::Float(45.0));
            assert!(!tripped(&io));
        }
    }

    #[test]
    fn an_unwired_current_port_evaluates_flow_and_pressure_only() {
        let mut block = SurgeGuard::new("sg", points(None), config(GuardResponse::Clamp)).unwrap();
        let io = io(false);

        // The `min_current` bound cannot fire without the port: a
        // demand below it passes while flow and pressure sit clear.
        feed(&io, 20.0, 100.0, 20.0, false, 1);
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(out(&io).value, Value::Float(20.0));
        assert!(!guarding(&io));

        // The wired bounds still guard.
        feed(&io, 20.0, 40.0, 20.0, false, 2);
        block.step(&io, Tick(2)).unwrap();
        assert_eq!(out(&io).value, Value::Float(50.0));
        assert!(guarding(&io));
    }

    #[test]
    fn untrusted_measurements_read_as_their_bound_crossed() {
        let mut block = component();
        let io = io(true);

        // A non-`Good` flow cannot vouch the point sits outside the
        // region — the clamp engages and `out` carries the quality.
        io.feed(
            FLOW,
            Sample::new(
                Value::Float(100.0),
                Quality::Bad(QualityReason::CommunicationFault),
                Tick(1),
            ),
        );
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(out(&io).value, Value::Float(70.0));
        assert_eq!(
            out(&io).quality,
            Quality::Bad(QualityReason::CommunicationFault)
        );
        assert!(guarding(&io));
        assert!(!tripped(&io));

        // A non-finite pressure reads as its bound crossed, faulted on
        // top of the reported quality.
        io.feed(FLOW, Sample::good(Value::Float(100.0), Tick(2)));
        io.feed(
            PRESSURE,
            Sample::new(Value::Float(f64::NAN), Quality::Good, Tick(2)),
        );
        block.step(&io, Tick(2)).unwrap();
        assert_eq!(out(&io).value, Value::Float(30.0));
        assert_eq!(out(&io).quality, Quality::Bad(QualityReason::DeviceFault));
        assert!(guarding(&io));

        // A bound current that goes Uncertain reads below its bound.
        io.feed(PRESSURE, Sample::good(Value::Float(20.0), Tick(3)));
        feed(&io, 45.0, 100.0, 20.0, false, 3);
        io.feed(
            CURRENT,
            Sample::new(
                Value::Float(60.0),
                Quality::Uncertain(QualityReason::Stale),
                Tick(3),
            ),
        );
        block.step(&io, Tick(3)).unwrap();
        assert_eq!(out(&io).value, Value::Float(45.0));
        assert_eq!(out(&io).quality, Quality::Uncertain(QualityReason::Stale));
        assert!(guarding(&io));
    }

    #[test]
    fn an_untrusted_demand_drives_trip_value_whatever_the_response() {
        for response in [GuardResponse::Clamp, GuardResponse::Trip] {
            let mut block = SurgeGuard::new("sg", points(Some(CURRENT)), config(response)).unwrap();
            let io = io(true);
            io.feed(
                DEMAND,
                Sample::new(
                    Value::Float(70.0),
                    Quality::Bad(QualityReason::CommunicationFault),
                    Tick(1),
                ),
            );
            block.step(&io, Tick(1)).unwrap();
            assert_eq!(out(&io).value, Value::Float(0.0));
            assert_eq!(
                out(&io).quality,
                Quality::Bad(QualityReason::CommunicationFault)
            );
            assert!(tripped(&io));
            assert!(!guarding(&io), "no measurement bound is crossed");
        }
    }

    #[test]
    fn an_untrusted_surge_trip_reads_as_proven() {
        let mut block = component();
        let io = io(true);
        io.feed(
            SURGE_TRIP,
            Sample::new(
                Value::Bool(false),
                Quality::Uncertain(QualityReason::Stale),
                Tick(1),
            ),
        );
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(out(&io).value, Value::Float(0.0));
        assert!(tripped(&io));
    }

    #[test]
    fn a_restored_guard_resumes_identically() {
        let mut block = component();
        let io = io(true);

        // Retune, then capture mid-engagement: the standby resumes
        // under the tuned bounds.
        block
            .apply_parameter("min_flow", Value::Float(60.0))
            .unwrap();
        feed(&io, 55.0, 55.0, 20.0, false, 1);
        block.step(&io, Tick(1)).unwrap();
        let state = block.capture_state();
        assert_eq!(state.get("min_flow"), Some(Value::Float(60.0)));

        let mut standby = component();
        standby.restore_state(&state).unwrap();
        assert_eq!(standby.capture_state(), state);

        feed(&io, 55.0, 55.0, 20.0, false, 2);
        standby.step(&io, Tick(2)).unwrap();
        assert_eq!(out(&io).value, Value::Float(60.0));
        assert!(guarding(&io));
    }

    #[test]
    fn restore_validates_the_captured_vocabulary() {
        let mut block = component();
        let valid = block.capture_state();

        // Foreign and missing fields are named errors.
        let mut foreign = valid.clone();
        foreign.insert("foreign", Value::Int(1));
        assert!(matches!(
            block.restore_state(&foreign),
            Err(StateError::UnknownField { ref field, .. }) if field == "foreign"
        ));
        assert!(matches!(
            block.restore_state(&StateMap::new()),
            Err(StateError::MissingField { ref field, .. }) if field == "min_flow"
        ));

        // Mistyped and out-of-range fields are named errors, and a
        // rejected map changes nothing.
        let mut mistyped = valid.clone();
        mistyped.insert("min_flow", Value::Int(1));
        assert!(matches!(
            block.restore_state(&mistyped),
            Err(StateError::IncompatibleField { ref field, .. }) if field == "min_flow"
        ));
        let mut invalid = valid.clone();
        invalid.insert("min_flow", Value::Float(-1.0));
        assert!(matches!(
            block.restore_state(&invalid),
            Err(StateError::InvalidValue { ref field, .. }) if field == "min_flow"
        ));
        let mut bad_code = valid.clone();
        bad_code.insert("on_guard", Value::Int(4));
        assert!(matches!(
            block.restore_state(&bad_code),
            Err(StateError::InvalidValue { ref field, .. }) if field == "on_guard"
        ));
        assert_eq!(block.capture_state(), valid);
    }

    #[test]
    fn builds_from_parameter_map() {
        let parameters: Parameters = [
            ("min_flow".to_string(), Value::Float(50.0)),
            ("max_pressure".to_string(), Value::Float(30.0)),
            ("min_current".to_string(), Value::Float(40.0)),
            ("on_guard".to_string(), Value::Int(1)),
            ("trip_value".to_string(), Value::Float(0.0)),
        ]
        .into_iter()
        .collect();
        let block = SurgeGuard::from_parameters("sg", points(Some(CURRENT)), &parameters).unwrap();
        assert_eq!(block.config.min_flow, 50.0);
        assert_eq!(block.config.max_pressure, 30.0);
        assert_eq!(block.config.min_current, 40.0);
        assert_eq!(block.config.on_guard, GuardResponse::Trip);
        assert_eq!(block.config.trip_value, 0.0);

        for (parameters, parameter) in [
            (Parameters::new(), "min_flow"),
            (
                [
                    ("min_flow".to_string(), Value::Float(-1.0)),
                    ("max_pressure".to_string(), Value::Float(30.0)),
                    ("min_current".to_string(), Value::Float(40.0)),
                    ("on_guard".to_string(), Value::Int(0)),
                    ("trip_value".to_string(), Value::Float(0.0)),
                ]
                .into_iter()
                .collect(),
                "min_flow",
            ),
            (
                [
                    ("min_flow".to_string(), Value::Float(50.0)),
                    ("max_pressure".to_string(), Value::Float(f64::NAN)),
                    ("min_current".to_string(), Value::Float(40.0)),
                    ("on_guard".to_string(), Value::Int(0)),
                    ("trip_value".to_string(), Value::Float(0.0)),
                ]
                .into_iter()
                .collect(),
                "max_pressure",
            ),
            (
                [
                    ("min_flow".to_string(), Value::Float(50.0)),
                    ("max_pressure".to_string(), Value::Float(30.0)),
                    ("min_current".to_string(), Value::Float(40.0)),
                    ("on_guard".to_string(), Value::Int(3)),
                    ("trip_value".to_string(), Value::Float(0.0)),
                ]
                .into_iter()
                .collect(),
                "on_guard",
            ),
            (
                [
                    ("min_flow".to_string(), Value::Float(50.0)),
                    ("max_pressure".to_string(), Value::Float(30.0)),
                    ("min_current".to_string(), Value::Float(40.0)),
                    ("on_guard".to_string(), Value::Int(0)),
                    ("trip_value".to_string(), Value::Float(f64::INFINITY)),
                ]
                .into_iter()
                .collect(),
                "trip_value",
            ),
        ] {
            assert!(matches!(
                SurgeGuard::from_parameters("sg", points(Some(CURRENT)), &parameters)
                    .unwrap_err(),
                ParameterError::Missing { parameter: ref p, .. }
                | ParameterError::Invalid { parameter: ref p, .. } if p == parameter
            ));
        }
    }

    #[test]
    fn tunes_declared_parameters_at_the_scan_boundary() {
        let mut block = component();
        let io = io(true);

        block
            .apply_parameter("min_flow", Value::Float(60.0))
            .unwrap();
        block
            .apply_parameter("max_pressure", Value::Float(25.0))
            .unwrap();
        block
            .apply_parameter("min_current", Value::Float(45.0))
            .unwrap();
        block.apply_parameter("on_guard", Value::Int(1)).unwrap();
        block
            .apply_parameter("trip_value", Value::Float(-5.0))
            .unwrap();
        assert_eq!(block.config.min_flow, 60.0);
        assert_eq!(block.config.max_pressure, 25.0);
        assert_eq!(block.config.min_current, 45.0);
        assert_eq!(block.config.on_guard, GuardResponse::Trip);
        assert_eq!(block.config.trip_value, -5.0);

        // A refused value changes nothing, naming the parameter.
        assert!(matches!(
            block.apply_parameter("min_flow", Value::Float(-1.0)),
            Err(CommandError::InvalidParameter { ref parameter, .. }) if parameter == "min_flow"
        ));
        assert!(matches!(
            block.apply_parameter("on_guard", Value::Int(4)),
            Err(CommandError::InvalidParameter { ref parameter, .. }) if parameter == "on_guard"
        ));
        assert!(matches!(
            block.apply_parameter("nope", Value::Int(0)),
            Err(CommandError::UnknownParameter { ref parameter, .. }) if parameter == "nope"
        ));
        assert_eq!(block.config.min_flow, 60.0);

        // The tuned trip response engages on the next scan.
        feed(&io, 30.0, 40.0, 20.0, false, 1);
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(out(&io).value, Value::Float(-5.0));
        assert!(tripped(&io));
    }

    #[test]
    fn describes_itself() {
        let descriptor = component().describe();
        assert_eq!(descriptor.name, "sg");
        assert_eq!(descriptor.kind, SurgeGuard::KIND);
        assert_eq!(descriptor.label, "sg");
        assert_eq!(
            descriptor.ports,
            [
                PortDescriptor {
                    name: "demand".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: Some(PortRole::ProcessValue),
                    point: None,
                },
                PortDescriptor {
                    name: "flow".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: Some(PortRole::ProcessValue),
                    point: None,
                },
                PortDescriptor {
                    name: "pressure".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: Some(PortRole::ProcessValue),
                    point: None,
                },
                PortDescriptor {
                    name: "current".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: Some(PortRole::ProcessValue),
                    point: None,
                },
                PortDescriptor {
                    name: "surge_trip".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Status),
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
                    name: "guarding".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Status),
                    point: None,
                },
                PortDescriptor {
                    name: "tripped".to_string(),
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
                    name: "min_flow".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::NONNEGATIVE_F64),
                },
                ParameterDescriptor {
                    name: "max_pressure".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::NONNEGATIVE_F64),
                },
                ParameterDescriptor {
                    name: "min_current".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::NONNEGATIVE_F64),
                },
                ParameterDescriptor {
                    name: "on_guard".to_string(),
                    kind: ValueKind::Int,
                    range: Some(ON_GUARD_RANGE),
                },
                ParameterDescriptor {
                    name: "trip_value".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::FINITE_F64),
                },
            ]
        );

        // The unwired instance declares no `current` port.
        let unbound = SurgeGuard::new("sg", points(None), config(GuardResponse::Clamp))
            .unwrap()
            .describe();
        assert!(
            unbound.ports.iter().all(|port| port.name != "current"),
            "an unbound current declares no port"
        );

        // The descriptor's names are exactly the `from_parameters`
        // key set.
        let mut keyless = Parameters::new();
        for parameter in &descriptor.parameters {
            let value = match parameter.name.as_str() {
                "min_flow" | "max_pressure" | "min_current" | "trip_value" => Value::Float(1.0),
                "on_guard" => Value::Int(0),
                other => panic!("undocumented parameter {other}"),
            };
            keyless.insert(parameter.name.clone(), value);
        }
        SurgeGuard::from_parameters("sg", points(Some(CURRENT)), &keyless).unwrap();
    }

    #[test]
    fn identical_input_sequences_produce_identical_state() {
        let run = || {
            let mut block = component();
            let io = io(true);
            let mut written = Vec::new();
            for (tick, demand, flow, pressure, trip) in [
                (1, 70.0, 100.0, 20.0, false),
                (2, 30.0, 40.0, 20.0, false),
                (3, 30.0, 40.0, 35.0, false),
                (4, 30.0, 100.0, 20.0, true),
                (5, 45.0, 100.0, 20.0, false),
            ] {
                feed(&io, demand, flow, pressure, trip, tick);
                block.step(&io, Tick(tick)).unwrap();
                written.push(out(&io));
            }
            (block.capture_state(), written)
        };
        assert_eq!(run(), run());
    }
}
