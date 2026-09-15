//! Shared aeration-header coordination: the bank-level contract
//! architecture decision 62 records for aeration control — the
//! `WW-CTL-005` requirement `docs/requirements/water-wastewater.md`
//! carries (`docs/research/aeration-do-control.md`). Independent
//! per-zone DO→valve loops hunt through the common discharge header;
//! the coordination remedy — constant header pressure, most-open-valve
//! pressure reset, or direct-airflow control — plus the plant-wide
//! pulse cap are per-scan run state (the walking set-point, the held
//! pulse-grant set, the most-open identity) a wiring composition
//! cannot hold, so the bank contract is a component kind.

use crate::describe;
use crate::params::{self, ParameterError, Parameters};
use dcs_core::{
    CommandError, ComponentDescriptor, ParameterRange, PointId, PortRole, Quality, Sample,
    StateError, StateMap, Tick, Value, ValueKind,
};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// How a [`HeaderCoordinator`] coordinates the bank — the `strategy`
/// parameter's `Int` code.
///
/// The model's parameter vocabulary has no string type, so the
/// `strategy` parameter carries the choice as an `Int` code — the
/// values [`code`](Self::code) reports and [`decode`](Self::decode)
/// accepts. All three codes are the decision's recorded
/// customer-validation assumptions, carried as declared data rather
/// than defaults.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CoordinationStrategy {
    /// `0` — constant header pressure: `pressure_sp` holds the
    /// declared `pressure_hold` clamped to the declared bounds while
    /// the zones throttle their valves.
    ConstantPressure,
    /// `1` — most-open-valve pressure reset: `pressure_sp` walks at
    /// the `adjust_ticks` cadence until the most-open trusted
    /// `valve_pos_i` settles inside the declared
    /// `mov_band_lo`–`mov_band_hi` band.
    MostOpenValveReset,
    /// `2` — direct-airflow control: `blower_demand` sums the zone
    /// `airflow_i` demands; `pressure_sp` emits the declared
    /// `pressure_hold` clamped to the bounds — an inert reference, not
    /// a control output, the plant's valve-positioning wiring acting
    /// on the demand directly.
    DirectAirflow,
}

impl CoordinationStrategy {
    /// The `Int` code the `strategy` parameter carries.
    pub const fn code(self) -> i64 {
        match self {
            Self::ConstantPressure => 0,
            Self::MostOpenValveReset => 1,
            Self::DirectAirflow => 2,
        }
    }

    /// The strategy the `Int` code `code` selects, or `None` when the
    /// code declares no strategy.
    pub const fn decode(code: i64) -> Option<Self> {
        match code {
            0 => Some(Self::ConstantPressure),
            1 => Some(Self::MostOpenValveReset),
            2 => Some(Self::DirectAirflow),
            _ => None,
        }
    }
}

/// One aeration zone's bound points within a [`HeaderCoordinator`],
/// declared `i` as the zone's 1-based index: `valve_pos_i` (`In`,
/// `Float`) is the zone valve's position feedback, `airflow_i` (`In`,
/// `Float`) the zone's airflow demand — the DO/airflow loop's output
/// the header must deliver, `pulsing_i` (`In`, `Bool`) the grid's
/// mixing-pulse request, and `pulse_grant_i` (`Out`, `Bool`) the
/// coordinator's pulse admission.
#[derive(Debug, Clone, Copy)]
pub struct ZoneIo {
    /// `valve_pos_i` — the zone valve's position feedback.
    pub valve_pos: PointId,
    /// `airflow_i` — the zone's airflow demand.
    pub airflow: PointId,
    /// `pulsing_i` — the grid's mixing-pulse request.
    pub pulsing: PointId,
    /// `pulse_grant_i` — the coordinator's pulse admission.
    pub pulse_grant: PointId,
}

/// The bank-level output points a [`HeaderCoordinator`] reports on:
/// `pressure_sp` (`Out`, `Float`) the header-pressure set-point the
/// blower capacity loop tracks, `blower_demand` (`Out`, `Float`) the
/// aggregate capacity demand, `most_open` (`Out`, `Int`) the 1-based
/// index of the zone whose valve is most open — `0` while none is
/// trusted — `at_bound` (`Out`, `Bool`) asserted while the emitted
/// set-point or demand rests at a declared bound, and `pulse_blocked`
/// (`Out`, `Bool`) asserted while a pulse request stands refused by
/// the declared cap.
#[derive(Debug, Clone, Copy)]
pub struct HeaderOutputs {
    /// `pressure_sp` — the header-pressure set-point.
    pub pressure_sp: PointId,
    /// `blower_demand` — the aggregate capacity demand.
    pub blower_demand: PointId,
    /// `most_open` — the most-open trusted zone's 1-based index, `0`
    /// while no `valve_pos_i` is trusted.
    pub most_open: PointId,
    /// `at_bound` — the set-point or demand resting at a declared
    /// bound: the coordinator can no longer optimize.
    pub at_bound: PointId,
    /// `pulse_blocked` — a pulse request standing refused by the
    /// declared `max_pulsing` cap.
    pub pulse_blocked: PointId,
}

/// The tuned behavior a [`HeaderCoordinator`] runs under — its
/// parameter map's nine keys as one value.
///
/// `strategy` is the [`CoordinationStrategy`] code. `pressure_hold` is
/// the set-point the constant-pressure and direct-airflow strategies
/// hold and the walking set-point's initial value under
/// most-open-valve reset. `pressure_min`/`pressure_max` bound the
/// emitted set-point; `mov_band_lo`/`mov_band_hi` declare the
/// near-open band the reset drives the most-open valve into;
/// `adjust_ticks` is the minimum interval between set-point moves;
/// `min_total_airflow` is the header-level mixing floor the emitted
/// demand cannot fall below; `max_pulsing` caps simultaneously granted
/// mixing pulses. Every value must be finite, with
/// `pressure_min <= pressure_max`, `mov_band_lo <= mov_band_hi`,
/// `adjust_ticks >= 1`, and `min_total_airflow` non-negative. All nine
/// are the decision's recorded customer-validation assumptions carried
/// as declared data — required parameters, never silently defaulted.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct HeaderCoordinatorConfig {
    /// The coordination strategy — the `strategy` code.
    pub strategy: CoordinationStrategy,
    /// `pressure_hold` — the held set-point under the constant and
    /// direct-airflow strategies, the walk's initial set-point under
    /// most-open-valve reset.
    pub pressure_hold: f64,
    /// `pressure_min` — the emitted set-point's lower bound.
    pub pressure_min: f64,
    /// `pressure_max` — the emitted set-point's upper bound.
    pub pressure_max: f64,
    /// `mov_band_lo` — the most-open band's lower edge.
    pub mov_band_lo: f64,
    /// `mov_band_hi` — the most-open band's upper edge.
    pub mov_band_hi: f64,
    /// `adjust_ticks` — the minimum interval between set-point moves.
    pub adjust_ticks: u64,
    /// `min_total_airflow` — the header-level mixing floor.
    pub min_total_airflow: f64,
    /// `max_pulsing` — the cap on simultaneously granted pulses.
    pub max_pulsing: u64,
}

impl HeaderCoordinatorConfig {
    /// Validates the invariants every construction path enforces — all
    /// floats finite, each bound pair ordered, `adjust_ticks` at least
    /// one, `min_total_airflow` non-negative — reporting a violation
    /// as a [`ParameterError`] naming `component` and the offending
    /// parameter.
    pub(crate) fn checked(component: &str, config: Self) -> Result<Self, ParameterError> {
        for (parameter, value) in [
            ("pressure_hold", config.pressure_hold),
            ("pressure_min", config.pressure_min),
            ("pressure_max", config.pressure_max),
            ("mov_band_lo", config.mov_band_lo),
            ("mov_band_hi", config.mov_band_hi),
            ("min_total_airflow", config.min_total_airflow),
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
        if config.adjust_ticks < 1 {
            return Err(params::invalid(
                component,
                "adjust_ticks",
                "must be at least 1".to_string(),
            ));
        }
        if config.min_total_airflow < 0.0 {
            return Err(params::invalid(
                component,
                "min_total_airflow",
                "must be non-negative".to_string(),
            ));
        }
        Ok(config)
    }

    /// The bound-pair half of [`checked`](Self::checked): each upper
    /// bound must be at least its lower, and the reported parameter is
    /// the one that failed that constraint.
    fn check_order(component: &str, config: &Self) -> Result<(), ParameterError> {
        if config.pressure_min > config.pressure_max {
            return Err(params::invalid(
                component,
                "pressure_max",
                "must be at least pressure_min".to_string(),
            ));
        }
        if config.mov_band_lo > config.mov_band_hi {
            return Err(params::invalid(
                component,
                "mov_band_hi",
                "must be at least mov_band_lo".to_string(),
            ));
        }
        Ok(())
    }
}

/// The inclusive `Int` bound the `strategy` parameter accepts: the
/// declared [`CoordinationStrategy`] codes.
const STRATEGY_RANGE: ParameterRange = ParameterRange {
    min: Value::Int(0),
    max: Value::Int(2),
};

/// A shared-header aeration coordinator: owns the bank-level
/// coordination the independent per-zone loops cannot express —
/// `pressure` (`In`, `Float`) the discharge-header pressure
/// transmitter plus, per zone `i` declared `valve_pos_i`/`airflow_i`/
/// `pulsing_i`/`pulse_grant_i` under the indexed-port convention —
/// the zone count discovered from the bound port names at
/// construction — and the bank outputs `pressure_sp`,
/// `blower_demand`, `most_open`, `at_bound`, `pulse_blocked`.
///
/// **The most-open selection.** `most_open` reports the 1-based index
/// of the zone whose `valve_pos_i` reads highest — the identity the
/// most-open-valve reset walks against and the bank state every
/// strategy reports. Only `Good` finite `valve_pos_i` samples count:
/// an untrusted or non-finite position cannot claim the band, so the
/// selection falls to the next-most-open trusted zone, and `most_open`
/// reports `0` while no zone's position is trusted. A tie resolves to
/// the lowest zone index.
///
/// **The set-point path.** Under [`ConstantPressure`] the emitted
/// `pressure_sp` is `pressure_hold` clamped to
/// `pressure_min`–`pressure_max`. Under [`DirectAirflow`] the same
/// clamped hold emits as an inert reference — the strategy runs no
/// pressure loop, the coordination acting through `blower_demand`.
/// Under [`MostOpenValveReset`] `pressure_sp` walks: the set-point
/// starts at the clamped `pressure_hold` and, once every
/// `adjust_ticks` scans, moves by the most-open valve's distance
/// outside the band — down by `valve_pos - mov_band_hi` while the
/// valve sits too open (more pressure than the header needs), up by
/// `mov_band_lo - valve_pos` while it sits too closed — clamped to the
/// declared bounds each move. The evaluation cadence is fixed: a scan
/// whose `pressure` is not `Good` and finite, or whose most-open
/// selection is empty, skips that interval's move rather than catching
/// up, so an untrusted pressure holds the last emitted set-point —
/// the decision's documented non-`Good` rule.
///
/// **The demand.** `blower_demand` emits the summed zone `airflow_i`
/// demands floored at `min_total_airflow` — the header-level mixing
/// floor. A `Good` finite `airflow_i` refreshes the zone's held
/// demand; an untrusted one holds the last `Good` value, a zone never
/// yet trusted contributing nothing.
///
/// **Pulse admission.** A `Good` `pulsing_i` reading `true` requests a
/// mixing pulse; the coordinator admits requests up to `max_pulsing`,
/// grants holding while their request stands — a granted pulse keeps
/// its slot while lower-indexed requests wait — and waiting requests
/// admit in ascending zone order as capacity frees. A `pulsing_i` that
/// drops or goes non-`Good` releases its grant; `pulse_blocked`
/// asserts while a requesting zone stands refused by the cap.
///
/// **Status.** `at_bound` asserts while the emitted set-point rests
/// at `pressure_min`/`pressure_max` or the demand rests at the
/// `min_total_airflow` floor — the coordinator can no longer optimize.
/// Every output carries [`Quality::Good`]: the values are the
/// coordinator's own computed decisions under these rules.
///
/// Declared I/O: `pressure` (`In`, `Float`); per zone `valve_pos_i`
/// (`In`, `Float`), `airflow_i` (`In`, `Float`), `pulsing_i` (`In`,
/// `Bool`), `pulse_grant_i` (`Out`, `Bool`); `pressure_sp` (`Out`,
/// `Float`), `blower_demand` (`Out`, `Float`), `most_open` (`Out`,
/// `Int`), `at_bound` (`Out`, `Bool`), `pulse_blocked` (`Out`,
/// `Bool`).
///
/// Parameters — required, the decision's assumption-marked declared
/// data: `strategy` (`Int`, a [`CoordinationStrategy`] code),
/// `pressure_hold`, `pressure_min`, `pressure_max`, `mov_band_lo`,
/// `mov_band_hi` (finite `Float`s, `pressure_min <= pressure_max`,
/// `mov_band_lo <= mov_band_hi`), `adjust_ticks` (`Int` ≥ 1),
/// `min_total_airflow` (finite `Float` ≥ 0), `max_pulsing` (`Int` ≥
/// 0). All nine tune through `set_parameter`; a retune breaking a
/// bound pair is refused naming the parameter, and a pressure-bound
/// retune re-clamps the held set-point.
#[derive(Debug)]
pub struct HeaderCoordinator {
    name: String,
    pressure: PointId,
    zones: Vec<ZoneIo>,
    outputs: HeaderOutputs,
    config: HeaderCoordinatorConfig,
    /// The emitted set-point — the clamped hold under the
    /// non-reset strategies, the walking set-point under
    /// most-open-valve reset.
    setpoint: f64,
    /// The emitted aggregate demand — the floored zone-demand sum.
    demand: f64,
    /// The most-open trusted zone — a 0-based index into `zones`;
    /// `most_open` reports its 1-based form, `0` while `None`.
    most_open: Option<usize>,
    /// Scans since the last evaluated set-point move — the
    /// `adjust_ticks` interval timer.
    adjust_elapsed: u64,
    /// Each zone's held last `Good` finite airflow demand — `None`
    /// until the first arrives.
    last_airflow: Vec<Option<f64>>,
    /// Each zone's held pulse grant — a grant stands while its
    /// `pulsing_i` request asserts.
    pulse_granted: Vec<bool>,
}

impl HeaderCoordinator {
    /// The component-kind string the model-driven registry maps onto
    /// this type's constructor.
    pub const KIND: &'static str = "header-coordinator";

    /// Builds the component from explicit points and the tuned
    /// configuration, or reports an inconsistent one as a
    /// [`ParameterError`].
    ///
    /// `zones` are the managed zones in declared order — `zones[i]` is
    /// zone `i + 1` — and must not be empty.
    pub fn new(
        name: impl Into<String>,
        pressure: PointId,
        zones: Vec<ZoneIo>,
        outputs: HeaderOutputs,
        config: HeaderCoordinatorConfig,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        if zones.is_empty() {
            return Err(params::invalid(
                &name,
                "zones",
                "must declare at least one zone".to_string(),
            ));
        }
        let config = HeaderCoordinatorConfig::checked(&name, config)?;
        let count = zones.len();
        Ok(Self {
            setpoint: config
                .pressure_hold
                .clamp(config.pressure_min, config.pressure_max),
            demand: config.min_total_airflow,
            name,
            pressure,
            zones,
            outputs,
            config,
            most_open: None,
            adjust_elapsed: 0,
            last_airflow: vec![None; count],
            pulse_granted: vec![false; count],
        })
    }

    /// The coordinator's tuned configuration — the reported parameter
    /// set.
    pub fn config(&self) -> HeaderCoordinatorConfig {
        self.config
    }

    /// Builds the component from a plant-model parameter map, reading
    /// the parameters listed on the type's docs.
    pub fn from_parameters(
        name: impl Into<String>,
        pressure: PointId,
        zones: Vec<ZoneIo>,
        outputs: HeaderOutputs,
        parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        let strategy_code = params::required_u64(&name, parameters, "strategy")?;
        let strategy = CoordinationStrategy::decode(strategy_code as i64).ok_or_else(|| {
            params::invalid(
                &name,
                "strategy",
                format!(
                    "expected 0 (constant header pressure), 1 (most-open-valve reset), or 2 (direct airflow), found {strategy_code}"
                ),
            )
        })?;
        let config = HeaderCoordinatorConfig {
            strategy,
            pressure_hold: params::required_f64(&name, parameters, "pressure_hold")?,
            pressure_min: params::required_f64(&name, parameters, "pressure_min")?,
            pressure_max: params::required_f64(&name, parameters, "pressure_max")?,
            mov_band_lo: params::required_f64(&name, parameters, "mov_band_lo")?,
            mov_band_hi: params::required_f64(&name, parameters, "mov_band_hi")?,
            adjust_ticks: params::required_u64(&name, parameters, "adjust_ticks")?,
            min_total_airflow: params::required_f64(&name, parameters, "min_total_airflow")?,
            max_pulsing: params::required_u64(&name, parameters, "max_pulsing")?,
        };
        Self::new(name, pressure, zones, outputs, config)
    }

    /// Retunes one declared parameter, returning a [`CommandError`]
    /// naming `parameter` on an unknown name, a wrong kind, or a value
    /// that would break an invariant — a refused value changes
    /// nothing.
    fn tune(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        let mut config = self.config;
        match parameter {
            "strategy" => {
                let code = params::tune_u64(&self.name, parameter, value)?;
                config.strategy =
                    CoordinationStrategy::decode(code as i64).ok_or_else(|| {
                        params::invalid_parameter(
                            &self.name,
                            parameter,
                            "expected 0 (constant header pressure), 1 (most-open-valve reset), or 2 (direct airflow)",
                        )
                    })?;
            }
            "pressure_hold" => {
                let tuned = params::tune_f64(&self.name, parameter, value)?;
                if !tuned.is_finite() {
                    return Err(params::invalid_parameter(
                        &self.name,
                        parameter,
                        "must be finite",
                    ));
                }
                config.pressure_hold = tuned;
            }
            "pressure_min" | "pressure_max" | "mov_band_lo" | "mov_band_hi" => {
                let tuned = params::tune_f64(&self.name, parameter, value)?;
                if !tuned.is_finite() {
                    return Err(params::invalid_parameter(
                        &self.name,
                        parameter,
                        "must be finite",
                    ));
                }
                match parameter {
                    "pressure_min" => config.pressure_min = tuned,
                    "pressure_max" => config.pressure_max = tuned,
                    "mov_band_lo" => config.mov_band_lo = tuned,
                    _ => config.mov_band_hi = tuned,
                }
                HeaderCoordinatorConfig::check_order(&self.name, &config).map_err(|_| {
                    params::invalid_parameter(
                        &self.name,
                        parameter,
                        "bounds must keep pressure_min <= pressure_max and mov_band_lo <= mov_band_hi",
                    )
                })?;
            }
            "adjust_ticks" => {
                let tuned = params::tune_u64(&self.name, parameter, value)?;
                if tuned < 1 {
                    return Err(params::invalid_parameter(
                        &self.name,
                        parameter,
                        "must be at least 1",
                    ));
                }
                config.adjust_ticks = tuned;
            }
            "min_total_airflow" => {
                let tuned = params::tune_f64(&self.name, parameter, value)?;
                if !tuned.is_finite() || tuned < 0.0 {
                    return Err(params::invalid_parameter(
                        &self.name,
                        parameter,
                        "must be a non-negative finite value",
                    ));
                }
                config.min_total_airflow = tuned;
            }
            "max_pulsing" => {
                config.max_pulsing = params::tune_u64(&self.name, parameter, value)?;
            }
            _ => return Err(params::unknown_parameter(&self.name, parameter)),
        }
        self.config = config;
        // A bound retune re-clamps the held set-point: the emitted
        // set-point never sits outside the declared bounds.
        self.setpoint = self
            .setpoint
            .clamp(self.config.pressure_min, self.config.pressure_max);
        Ok(())
    }
}

impl Component for HeaderCoordinator {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        let mut requirements = Vec::with_capacity(self.zones.len() * 4 + 6);
        requirements.push(IoRequirement::input::<f64>("pressure", self.pressure));
        for (index, zone) in self.zones.iter().enumerate() {
            let index = index + 1;
            requirements.push(IoRequirement::input::<f64>(
                format!("valve_pos_{index}"),
                zone.valve_pos,
            ));
            requirements.push(IoRequirement::input::<f64>(
                format!("airflow_{index}"),
                zone.airflow,
            ));
            requirements.push(IoRequirement::input::<bool>(
                format!("pulsing_{index}"),
                zone.pulsing,
            ));
            requirements.push(IoRequirement::output::<bool>(
                format!("pulse_grant_{index}"),
                zone.pulse_grant,
            ));
        }
        requirements.push(IoRequirement::output::<f64>(
            "pressure_sp",
            self.outputs.pressure_sp,
        ));
        requirements.push(IoRequirement::output::<f64>(
            "blower_demand",
            self.outputs.blower_demand,
        ));
        requirements.push(IoRequirement::output::<i64>(
            "most_open",
            self.outputs.most_open,
        ));
        requirements.push(IoRequirement::output::<bool>(
            "at_bound",
            self.outputs.at_bound,
        ));
        requirements.push(IoRequirement::output::<bool>(
            "pulse_blocked",
            self.outputs.pulse_blocked,
        ));
        requirements
    }

    fn step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError> {
        let count = self.zones.len();
        let pressure = io.read_typed::<f64>(self.pressure)?;

        // Fail-safe input readings: an untrusted or non-finite valve
        // position cannot claim the band, an untrusted airflow demand
        // holds the zone's last trusted value, and an untrusted pulse
        // request reads as not requesting.
        let mut trusted_pos: Vec<Option<f64>> = Vec::with_capacity(count);
        let mut requesting = vec![false; count];
        for (index, zone) in self.zones.iter().enumerate() {
            let position = io.read_typed::<f64>(zone.valve_pos)?;
            trusted_pos.push(
                (position.quality.is_good() && position.value.is_finite())
                    .then_some(position.value),
            );
            let airflow = io.read_typed::<f64>(zone.airflow)?;
            if airflow.quality.is_good() && airflow.value.is_finite() {
                self.last_airflow[index] = Some(airflow.value);
            }
            let pulsing = io.read_typed::<bool>(zone.pulsing)?;
            requesting[index] = pulsing.quality.is_good() && pulsing.value;
        }

        // The most-open selection: the highest trusted position, a tie
        // resolving to the lowest zone index.
        self.most_open = trusted_pos
            .iter()
            .enumerate()
            .filter_map(|(index, position)| position.map(|position| (index, position)))
            .fold(None, |best, current| match best {
                Some((_, best_pos)) if current.1 <= best_pos => best,
                _ => Some(current),
            })
            .map(|(index, _)| index);

        // The aggregate demand: the held per-zone demands summed and
        // floored at the declared mixing minimum.
        let total: f64 = self.last_airflow.iter().flatten().copied().sum();
        let floored = total < self.config.min_total_airflow;
        self.demand = if floored {
            self.config.min_total_airflow
        } else {
            total
        };

        // The set-point path. Constant-pressure and direct-airflow
        // emit the declared hold clamped to the bounds; the
        // most-open-valve reset walks the held set-point at the
        // declared cadence — an interval whose `pressure` or most-open
        // selection cannot be trusted skips its move, the cadence
        // rolling on, so the untrusted scan holds the last emitted
        // set-point.
        match self.config.strategy {
            CoordinationStrategy::ConstantPressure | CoordinationStrategy::DirectAirflow => {
                self.setpoint = self
                    .config
                    .pressure_hold
                    .clamp(self.config.pressure_min, self.config.pressure_max);
            }
            CoordinationStrategy::MostOpenValveReset => {
                self.adjust_elapsed += 1;
                if self.adjust_elapsed >= self.config.adjust_ticks {
                    self.adjust_elapsed = 0;
                    if pressure.quality.is_good()
                        && pressure.value.is_finite()
                        && let Some(position) = self.most_open.and_then(|index| trusted_pos[index])
                    {
                        let moved = if position > self.config.mov_band_hi {
                            self.setpoint - (position - self.config.mov_band_hi)
                        } else if position < self.config.mov_band_lo {
                            self.setpoint + (self.config.mov_band_lo - position)
                        } else {
                            self.setpoint
                        };
                        self.setpoint =
                            moved.clamp(self.config.pressure_min, self.config.pressure_max);
                    }
                }
            }
        }
        let at_bound = self.setpoint <= self.config.pressure_min
            || self.setpoint >= self.config.pressure_max
            || floored;

        // Pulse admission: a grant holds while its request stands;
        // waiting requests admit in ascending zone order while the
        // declared cap leaves room, the rest standing refused on
        // `pulse_blocked`.
        for (index, granted) in self.pulse_granted.iter_mut().enumerate() {
            if *granted && !requesting[index] {
                *granted = false;
            }
        }
        let mut held = self
            .pulse_granted
            .iter()
            .filter(|granted| **granted)
            .count();
        let mut blocked = false;
        for (index, granted) in self.pulse_granted.iter_mut().enumerate() {
            if requesting[index] && !*granted {
                if held < self.config.max_pulsing as usize {
                    *granted = true;
                    held += 1;
                } else {
                    blocked = true;
                }
            }
        }

        for (index, zone) in self.zones.iter().enumerate() {
            io.write_sample(
                zone.pulse_grant,
                Sample::new(Value::Bool(self.pulse_granted[index]), Quality::Good, tick),
            )?;
        }
        io.write_sample(
            self.outputs.pressure_sp,
            Sample::new(Value::Float(self.setpoint), Quality::Good, tick),
        )?;
        io.write_sample(
            self.outputs.blower_demand,
            Sample::new(Value::Float(self.demand), Quality::Good, tick),
        )?;
        io.write_sample(
            self.outputs.most_open,
            Sample::new(
                Value::Int(self.most_open.map_or(0, |index| index as i64 + 1)),
                Quality::Good,
                tick,
            ),
        )?;
        io.write_sample(
            self.outputs.at_bound,
            Sample::new(Value::Bool(at_bound), Quality::Good, tick),
        )?;
        io.write_sample(
            self.outputs.pulse_blocked,
            Sample::new(Value::Bool(blocked), Quality::Good, tick),
        )?;
        Ok(())
    }

    /// Describes the coordinator: `pressure`, `valve_pos_i`,
    /// `airflow_i`, and `pulsing_i` are the measured conditions it
    /// acts on, `pressure_sp`/`blower_demand`/`pulse_grant_i` the
    /// driven values, and `most_open`/`at_bound`/`pulse_blocked` the
    /// reported bank state; the nine declared parameters with their
    /// ranges.
    fn describe(&self) -> ComponentDescriptor {
        let mut roles: Vec<(String, PortRole)> =
            vec![("pressure".to_string(), PortRole::ProcessValue)];
        for index in 1..=self.zones.len() {
            roles.push((format!("valve_pos_{index}"), PortRole::ProcessValue));
            roles.push((format!("airflow_{index}"), PortRole::ProcessValue));
            roles.push((format!("pulsing_{index}"), PortRole::ProcessValue));
            roles.push((format!("pulse_grant_{index}"), PortRole::Output));
        }
        roles.extend([
            ("pressure_sp".to_string(), PortRole::Output),
            ("blower_demand".to_string(), PortRole::Output),
            ("most_open".to_string(), PortRole::Status),
            ("at_bound".to_string(), PortRole::Status),
            ("pulse_blocked".to_string(), PortRole::Status),
        ]);
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &roles,
            vec![
                describe::parameter("strategy", ValueKind::Int, Some(STRATEGY_RANGE)),
                describe::parameter(
                    "pressure_hold",
                    ValueKind::Float,
                    Some(describe::FINITE_F64),
                ),
                describe::parameter("pressure_min", ValueKind::Float, Some(describe::FINITE_F64)),
                describe::parameter("pressure_max", ValueKind::Float, Some(describe::FINITE_F64)),
                describe::parameter("mov_band_lo", ValueKind::Float, Some(describe::FINITE_F64)),
                describe::parameter("mov_band_hi", ValueKind::Float, Some(describe::FINITE_F64)),
                describe::parameter("adjust_ticks", ValueKind::Int, Some(describe::POSITIVE_INT)),
                describe::parameter(
                    "min_total_airflow",
                    ValueKind::Float,
                    Some(describe::NONNEGATIVE_F64),
                ),
                describe::parameter(
                    "max_pulsing",
                    ValueKind::Int,
                    Some(describe::NONNEGATIVE_INT),
                ),
            ],
        )
    }

    /// Tunes the declared parameters at the scan boundary — the
    /// recorded mechanism for operator-adjusted strategy, bounds,
    /// band, cadence, floor, and pulse cap. A bound retune re-clamps
    /// the held set-point; a refused value names the parameter and
    /// changes nothing.
    fn apply_parameter(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        self.tune(parameter, value)
    }

    /// Reports the tuned parameters — the same fields
    /// [`capture_state`](Self::capture_state) checkpoints, so the
    /// faceplate and a tracking standby read one vocabulary.
    fn report_parameters(&self) -> StateMap {
        let mut parameters = StateMap::new();
        parameters.insert("strategy", Value::Int(self.config.strategy.code()));
        parameters.insert("pressure_hold", Value::Float(self.config.pressure_hold));
        parameters.insert("pressure_min", Value::Float(self.config.pressure_min));
        parameters.insert("pressure_max", Value::Float(self.config.pressure_max));
        parameters.insert("mov_band_lo", Value::Float(self.config.mov_band_lo));
        parameters.insert("mov_band_hi", Value::Float(self.config.mov_band_hi));
        parameters.insert("adjust_ticks", Value::Int(self.config.adjust_ticks as i64));
        parameters.insert(
            "min_total_airflow",
            Value::Float(self.config.min_total_airflow),
        );
        parameters.insert("max_pulsing", Value::Int(self.config.max_pulsing as i64));
        parameters
    }

    /// Captures the emitted set-point and demand, the most-open
    /// identity, the adjustment timer, the held pulse-grant set, the
    /// held per-zone airflow demands, and the tuned parameters — the
    /// whole of the per-scan run state, so a checkpointed standby
    /// resumes the walk mid-adjustment without stepping the header
    /// pressure. A held airflow never yet established captures as
    /// absent.
    fn capture_state(&self) -> StateMap {
        let mut state = self.report_parameters();
        state.insert("setpoint", Value::Float(self.setpoint));
        state.insert("demand", Value::Float(self.demand));
        state.insert(
            "most_open",
            Value::Int(self.most_open.map_or(0, |index| index as i64 + 1)),
        );
        state.insert("adjust_elapsed", Value::Int(self.adjust_elapsed as i64));
        for (index, granted) in self.pulse_granted.iter().enumerate() {
            state.insert(format!("pulse_grant_{}", index + 1), Value::Bool(*granted));
        }
        for (index, held) in self.last_airflow.iter().enumerate() {
            if let Some(held) = held {
                state.insert(format!("last_airflow_{}", index + 1), Value::Float(*held));
            }
        }
        state
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        let count = self.zones.len() as i64;
        let mut known: Vec<String> = [
            "strategy",
            "pressure_hold",
            "pressure_min",
            "pressure_max",
            "mov_band_lo",
            "mov_band_hi",
            "adjust_ticks",
            "min_total_airflow",
            "max_pulsing",
            "setpoint",
            "demand",
            "most_open",
            "adjust_elapsed",
        ]
        .into_iter()
        .map(str::to_string)
        .collect();
        for index in 1..=count {
            known.push(format!("pulse_grant_{index}"));
            known.push(format!("last_airflow_{index}"));
        }
        let known_refs: Vec<&str> = known.iter().map(String::as_str).collect();
        state.ensure_known_fields(&self.name, &known_refs)?;

        // The whole map is validated before any state mutates, so a
        // rejected checkpoint leaves the component on its last-good
        // state rather than half-applied.
        let invalid = |field: &str, value: Value| StateError::InvalidValue {
            element: self.name.clone(),
            field: field.to_string(),
            value,
        };
        let strategy_code = state.require_i64(&self.name, "strategy")?;
        let strategy = CoordinationStrategy::decode(strategy_code)
            .ok_or_else(|| invalid("strategy", Value::Int(strategy_code)))?;
        let pressure_hold = state.require_f64(&self.name, "pressure_hold")?;
        let pressure_min = state.require_f64(&self.name, "pressure_min")?;
        let pressure_max = state.require_f64(&self.name, "pressure_max")?;
        let mov_band_lo = state.require_f64(&self.name, "mov_band_lo")?;
        let mov_band_hi = state.require_f64(&self.name, "mov_band_hi")?;
        let adjust_ticks = state.require_i64(&self.name, "adjust_ticks")?;
        let min_total_airflow = state.require_f64(&self.name, "min_total_airflow")?;
        let max_pulsing = state.require_i64(&self.name, "max_pulsing")?;
        for (field, value) in [
            ("pressure_hold", pressure_hold),
            ("pressure_min", pressure_min),
            ("pressure_max", pressure_max),
            ("mov_band_lo", mov_band_lo),
            ("mov_band_hi", mov_band_hi),
            ("min_total_airflow", min_total_airflow),
        ] {
            if !value.is_finite() {
                return Err(invalid(field, Value::Float(value)));
            }
        }
        if pressure_min > pressure_max {
            return Err(invalid("pressure_max", Value::Float(pressure_max)));
        }
        if mov_band_lo > mov_band_hi {
            return Err(invalid("mov_band_hi", Value::Float(mov_band_hi)));
        }
        if adjust_ticks < 1 {
            return Err(invalid("adjust_ticks", Value::Int(adjust_ticks)));
        }
        if min_total_airflow < 0.0 {
            return Err(invalid(
                "min_total_airflow",
                Value::Float(min_total_airflow),
            ));
        }
        if max_pulsing < 0 {
            return Err(invalid("max_pulsing", Value::Int(max_pulsing)));
        }

        let setpoint = state.require_f64(&self.name, "setpoint")?;
        if !setpoint.is_finite() || setpoint < pressure_min || setpoint > pressure_max {
            return Err(invalid("setpoint", Value::Float(setpoint)));
        }
        let demand = state.require_f64(&self.name, "demand")?;
        if !demand.is_finite() || demand < min_total_airflow {
            return Err(invalid("demand", Value::Float(demand)));
        }
        let most_open = state.require_i64(&self.name, "most_open")?;
        if !(0..=count).contains(&most_open) {
            return Err(invalid("most_open", Value::Int(most_open)));
        }
        let adjust_elapsed = state.require_i64(&self.name, "adjust_elapsed")?;
        if !(0..adjust_ticks).contains(&adjust_elapsed) {
            return Err(invalid("adjust_elapsed", Value::Int(adjust_elapsed)));
        }

        let mut pulse_granted = Vec::with_capacity(count as usize);
        let mut grants: i64 = 0;
        for index in 1..=count {
            let field = format!("pulse_grant_{index}");
            let granted = state.require_bool(&self.name, &field)?;
            if granted {
                grants += 1;
                if grants > max_pulsing {
                    // More held grants than the declared cap is not a
                    // map this component captures.
                    return Err(invalid(&field, Value::Bool(granted)));
                }
            }
            pulse_granted.push(granted);
        }
        let mut last_airflow = Vec::with_capacity(count as usize);
        for index in 1..=count {
            let field = format!("last_airflow_{index}");
            let held = state.optional_f64(&self.name, &field)?;
            if let Some(held) = held.filter(|held| !held.is_finite()) {
                return Err(invalid(&field, Value::Float(held)));
            }
            last_airflow.push(held);
        }

        self.config = HeaderCoordinatorConfig {
            strategy,
            pressure_hold,
            pressure_min,
            pressure_max,
            mov_band_lo,
            mov_band_hi,
            adjust_ticks: adjust_ticks as u64,
            min_total_airflow,
            max_pulsing: max_pulsing as u64,
        };
        self.setpoint = setpoint;
        self.demand = demand;
        self.most_open = if most_open == 0 {
            None
        } else {
            Some(most_open as usize - 1)
        };
        self.adjust_elapsed = adjust_elapsed as u64;
        self.pulse_granted = pulse_granted;
        self.last_airflow = last_airflow;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::TestIo;
    use dcs_core::{Direction, QualityReason};

    const PRESSURE: PointId = PointId(1);
    const SP: PointId = PointId(2);
    const DEMAND: PointId = PointId(3);
    const MOST: PointId = PointId(4);
    const BOUND: PointId = PointId(5);
    const PBLOCKED: PointId = PointId(6);

    const fn valve_pos(index: usize) -> PointId {
        PointId(100 + index as u64)
    }
    const fn airflow(index: usize) -> PointId {
        PointId(200 + index as u64)
    }
    const fn pulsing(index: usize) -> PointId {
        PointId(300 + index as u64)
    }
    const fn pulse_grant(index: usize) -> PointId {
        PointId(400 + index as u64)
    }

    const OUTPUTS: HeaderOutputs = HeaderOutputs {
        pressure_sp: SP,
        blower_demand: DEMAND,
        most_open: MOST,
        at_bound: BOUND,
        pulse_blocked: PBLOCKED,
    };

    fn zones(count: usize) -> Vec<ZoneIo> {
        (1..=count)
            .map(|index| ZoneIo {
                valve_pos: valve_pos(index),
                airflow: airflow(index),
                pulsing: pulsing(index),
                pulse_grant: pulse_grant(index),
            })
            .collect()
    }

    /// The default test config: constant pressure holding 10 inside
    /// 4..=16, the 85..=95 most-open band, a two-tick adjustment
    /// interval, a 1.0 airflow floor, and a one-pulse cap.
    fn config() -> HeaderCoordinatorConfig {
        HeaderCoordinatorConfig {
            strategy: CoordinationStrategy::ConstantPressure,
            pressure_hold: 10.0,
            pressure_min: 4.0,
            pressure_max: 16.0,
            mov_band_lo: 85.0,
            mov_band_hi: 95.0,
            adjust_ticks: 2,
            min_total_airflow: 1.0,
            max_pulsing: 1,
        }
    }

    fn component_of(count: usize, config: HeaderCoordinatorConfig) -> HeaderCoordinator {
        HeaderCoordinator::new("hdr", PRESSURE, zones(count), OUTPUTS, config).unwrap()
    }

    /// The test rig: every declared point, all inputs `Good` — the
    /// header pressure at 9.0, every valve half-open, no airflow
    /// demand, no pulse requests.
    fn io(count: usize) -> TestIo {
        let mut points = vec![
            (
                PRESSURE,
                Direction::In,
                Sample::good(Value::Float(9.0), Tick::ZERO),
            ),
            (
                SP,
                Direction::Out,
                Sample::good(Value::Float(0.0), Tick::ZERO),
            ),
            (
                DEMAND,
                Direction::Out,
                Sample::good(Value::Float(0.0), Tick::ZERO),
            ),
            (
                MOST,
                Direction::Out,
                Sample::good(Value::Int(0), Tick::ZERO),
            ),
            (
                BOUND,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
            (
                PBLOCKED,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
        ];
        for index in 1..=count {
            points.extend([
                (
                    valve_pos(index),
                    Direction::In,
                    Sample::good(Value::Float(50.0), Tick::ZERO),
                ),
                (
                    airflow(index),
                    Direction::In,
                    Sample::good(Value::Float(0.0), Tick::ZERO),
                ),
                (
                    pulsing(index),
                    Direction::In,
                    Sample::good(Value::Bool(false), Tick::ZERO),
                ),
                (
                    pulse_grant(index),
                    Direction::Out,
                    Sample::good(Value::Bool(false), Tick::ZERO),
                ),
            ]);
        }
        TestIo::new(&points)
    }

    fn feed_float(io: &TestIo, point: PointId, value: f64, tick: u64) {
        io.feed(point, Sample::good(Value::Float(value), Tick(tick)));
    }

    fn feed_float_bad(io: &TestIo, point: PointId, value: f64, tick: u64) {
        io.feed(
            point,
            Sample::new(
                Value::Float(value),
                Quality::Bad(QualityReason::Unspecified),
                Tick(tick),
            ),
        );
    }

    fn feed_pulsing(io: &TestIo, index: usize, value: bool, tick: u64) {
        io.feed(pulsing(index), Sample::good(Value::Bool(value), Tick(tick)));
    }

    fn feed_pulsing_bad(io: &TestIo, index: usize, tick: u64) {
        io.feed(
            pulsing(index),
            Sample::new(
                Value::Bool(true),
                Quality::Bad(QualityReason::Unspecified),
                Tick(tick),
            ),
        );
    }

    fn output_float(io: &TestIo, point: PointId) -> f64 {
        match io.written(point).unwrap().value {
            Value::Float(value) => value,
            value => panic!("expected Float, found {value:?}"),
        }
    }

    fn output_int(io: &TestIo, point: PointId) -> i64 {
        match io.written(point).unwrap().value {
            Value::Int(value) => value,
            value => panic!("expected Int, found {value:?}"),
        }
    }

    fn output_bool(io: &TestIo, point: PointId) -> bool {
        match io.written(point).unwrap().value {
            Value::Bool(value) => value,
            value => panic!("expected Bool, found {value:?}"),
        }
    }

    fn granted_to(io: &TestIo, index: usize) -> bool {
        matches!(
            io.written(pulse_grant(index)),
            Some(sample) if sample.value == Value::Bool(true)
        )
    }

    fn scan(coordinator: &mut HeaderCoordinator, io: &TestIo, tick: u64) {
        coordinator.step(io, Tick(tick)).unwrap();
    }

    #[test]
    fn constant_pressure_holds_the_clamped_setpoint() {
        let mut coordinator = component_of(2, config());
        let io = io(2);
        for tick in 1..=4 {
            feed_float(&io, airflow(1), 2.0, tick);
            feed_float(&io, airflow(2), 4.0, tick);
            feed_float(&io, valve_pos(1), 60.0, tick);
            feed_float(&io, valve_pos(2), 70.0, tick);
            scan(&mut coordinator, &io, tick);
            assert_eq!(output_float(&io, SP), 10.0);
            assert_eq!(output_float(&io, DEMAND), 6.0);
            assert_eq!(output_int(&io, MOST), 2);
            assert!(!output_bool(&io, BOUND));
        }
    }

    #[test]
    fn constant_pressure_clamps_at_the_declared_bounds() {
        for (hold, expected) in [(20.0, 16.0), (2.0, 4.0)] {
            let mut coordinator = component_of(
                1,
                HeaderCoordinatorConfig {
                    pressure_hold: hold,
                    ..config()
                },
            );
            let io = io(1);
            feed_float(&io, airflow(1), 5.0, 1);
            scan(&mut coordinator, &io, 1);
            assert_eq!(output_float(&io, SP), expected);
            assert!(output_bool(&io, BOUND));
        }
    }

    #[test]
    fn the_airflow_floor_bounds_the_emitted_demand() {
        let mut coordinator = component_of(2, config());
        let io = io(2);
        // The summed demands 0.5 sit below the 1.0 floor: the emitted
        // demand holds at the floor and `at_bound` reports it.
        feed_float(&io, airflow(1), 0.2, 1);
        feed_float(&io, airflow(2), 0.3, 1);
        scan(&mut coordinator, &io, 1);
        assert_eq!(output_float(&io, DEMAND), 1.0);
        assert!(output_bool(&io, BOUND));
    }

    #[test]
    fn most_open_valve_reset_walks_the_setpoint_to_the_band() {
        let mut coordinator = component_of(
            3,
            HeaderCoordinatorConfig {
                strategy: CoordinationStrategy::MostOpenValveReset,
                ..config()
            },
        );
        let io = io(3);
        // Zone 3 sits most-open at 97 — two points above the band's
        // upper edge: each two-scan interval steps the set-point down
        // by the distance.
        feed_float(&io, valve_pos(1), 50.0, 1);
        feed_float(&io, valve_pos(2), 60.0, 1);
        feed_float(&io, valve_pos(3), 97.0, 1);
        feed_float(&io, airflow(3), 5.0, 1);

        scan(&mut coordinator, &io, 1);
        assert_eq!(output_float(&io, SP), 10.0);
        assert_eq!(output_int(&io, MOST), 3);
        scan(&mut coordinator, &io, 2);
        assert_eq!(output_float(&io, SP), 8.0);
        scan(&mut coordinator, &io, 3);
        assert_eq!(output_float(&io, SP), 8.0);
        scan(&mut coordinator, &io, 4);
        assert_eq!(output_float(&io, SP), 6.0);
        scan(&mut coordinator, &io, 5);
        scan(&mut coordinator, &io, 6);
        assert_eq!(output_float(&io, SP), 4.0);
        assert!(output_bool(&io, BOUND));

        // Inside the band the walk stops — the set-point rests at the
        // bound it already reached.
        feed_float(&io, valve_pos(3), 88.0, 7);
        scan(&mut coordinator, &io, 7);
        scan(&mut coordinator, &io, 8);
        assert_eq!(output_float(&io, SP), 4.0);

        // Below the band the walk climbs by the distance to the lower
        // edge — 70 sits fifteen under 85 — and clamps at the max.
        feed_float(&io, valve_pos(3), 70.0, 9);
        scan(&mut coordinator, &io, 9);
        scan(&mut coordinator, &io, 10);
        assert_eq!(output_float(&io, SP), 16.0);
        scan(&mut coordinator, &io, 11);
        scan(&mut coordinator, &io, 12);
        assert_eq!(output_float(&io, SP), 16.0);
        assert!(output_bool(&io, BOUND));
    }

    #[test]
    fn most_open_ignores_untrusted_positions_and_breaks_ties_low() {
        let mut coordinator = component_of(3, config());
        let io = io(3);
        feed_float(&io, valve_pos(1), 80.0, 1);
        feed_float(&io, valve_pos(2), 80.0, 1);
        feed_float(&io, valve_pos(3), 40.0, 1);
        scan(&mut coordinator, &io, 1);
        // The tie between zones 1 and 2 resolves to the lower index.
        assert_eq!(output_int(&io, MOST), 1);

        // Zone 1's position goes untrusted: the selection falls to
        // zone 2 without ever claiming the band for the bad reading.
        feed_float_bad(&io, valve_pos(1), 95.0, 2);
        scan(&mut coordinator, &io, 2);
        assert_eq!(output_int(&io, MOST), 2);

        // No trusted position at all reports 0.
        feed_float_bad(&io, valve_pos(2), 80.0, 3);
        feed_float_bad(&io, valve_pos(3), 40.0, 3);
        scan(&mut coordinator, &io, 3);
        assert_eq!(output_int(&io, MOST), 0);
    }

    #[test]
    fn an_untrusted_pressure_holds_the_emitted_setpoint() {
        let mut coordinator = component_of(
            1,
            HeaderCoordinatorConfig {
                strategy: CoordinationStrategy::MostOpenValveReset,
                ..config()
            },
        );
        let io = io(1);
        feed_float(&io, valve_pos(1), 97.0, 1);
        scan(&mut coordinator, &io, 1);
        scan(&mut coordinator, &io, 2);
        assert_eq!(output_float(&io, SP), 8.0);

        // The pressure read goes untrusted mid-walk: the next
        // interval's evaluation is skipped, the emitted set-point
        // holds, and the cadence rolls on rather than catching up.
        feed_float_bad(&io, PRESSURE, 9.0, 3);
        scan(&mut coordinator, &io, 3);
        scan(&mut coordinator, &io, 4);
        assert_eq!(output_float(&io, SP), 8.0);
        scan(&mut coordinator, &io, 5);
        feed_float(&io, PRESSURE, 9.0, 5);
        scan(&mut coordinator, &io, 6);
        assert_eq!(output_float(&io, SP), 6.0);
    }

    #[test]
    fn direct_airflow_sums_the_zone_demands() {
        let mut coordinator = component_of(
            2,
            HeaderCoordinatorConfig {
                strategy: CoordinationStrategy::DirectAirflow,
                min_total_airflow: 3.0,
                ..config()
            },
        );
        let io = io(2);
        feed_float(&io, airflow(1), 1.2, 1);
        feed_float(&io, airflow(2), 1.5, 1);
        scan(&mut coordinator, &io, 1);
        // The 2.7 sum sits below the declared 3.0 floor.
        assert_eq!(output_float(&io, DEMAND), 3.0);
        assert!(output_bool(&io, BOUND));
        assert_eq!(output_float(&io, SP), 10.0);

        feed_float(&io, airflow(1), 4.0, 2);
        feed_float(&io, airflow(2), 5.0, 2);
        scan(&mut coordinator, &io, 2);
        assert_eq!(output_float(&io, DEMAND), 9.0);
        assert!(!output_bool(&io, BOUND));
    }

    #[test]
    fn an_untrusted_airflow_demand_holds_its_last_value() {
        let mut coordinator = component_of(
            3,
            HeaderCoordinatorConfig {
                strategy: CoordinationStrategy::DirectAirflow,
                ..config()
            },
        );
        let io = io(3);
        feed_float(&io, airflow(1), 2.0, 1);
        feed_float(&io, airflow(2), 3.0, 1);
        scan(&mut coordinator, &io, 1);
        // Zone 3 has never reported a trusted demand — it contributes
        // nothing.
        assert_eq!(output_float(&io, DEMAND), 5.0);

        feed_float_bad(&io, airflow(2), 99.0, 2);
        scan(&mut coordinator, &io, 2);
        assert_eq!(output_float(&io, DEMAND), 5.0);
    }

    #[test]
    fn pulse_admission_caps_grants_and_reports_refusals() {
        let mut coordinator = component_of(3, config());
        let io = io(3);

        // Zone 2 pulses first and takes the single slot; zone 1's
        // later request stands refused — a held grant does not yield
        // to a lower-indexed requester.
        feed_pulsing(&io, 2, true, 1);
        scan(&mut coordinator, &io, 1);
        assert!(granted_to(&io, 2));
        assert!(!granted_to(&io, 1));
        assert!(!output_bool(&io, PBLOCKED));

        feed_pulsing(&io, 1, true, 2);
        scan(&mut coordinator, &io, 2);
        assert!(granted_to(&io, 2));
        assert!(!granted_to(&io, 1));
        assert!(output_bool(&io, PBLOCKED));

        // Zone 2's request drops; zone 1's standing request admits at
        // the same scan.
        feed_pulsing(&io, 2, false, 3);
        scan(&mut coordinator, &io, 3);
        assert!(granted_to(&io, 1));
        assert!(!granted_to(&io, 2));
        assert!(!output_bool(&io, PBLOCKED));
    }

    #[test]
    fn an_untrusted_pulse_request_releases_the_grant() {
        let mut coordinator = component_of(2, config());
        let io = io(2);
        feed_pulsing(&io, 1, true, 1);
        feed_pulsing(&io, 2, true, 1);
        scan(&mut coordinator, &io, 1);
        assert!(granted_to(&io, 1));
        assert!(output_bool(&io, PBLOCKED));

        // Zone 1's request goes untrusted: the grant releases and the
        // waiting zone admits the same scan.
        feed_pulsing_bad(&io, 1, 2);
        scan(&mut coordinator, &io, 2);
        assert!(!granted_to(&io, 1));
        assert!(granted_to(&io, 2));
        assert!(!output_bool(&io, PBLOCKED));
    }

    #[test]
    fn construction_rejects_bad_parameters_naming_them() {
        let mut parameters = Parameters::from([
            ("strategy".to_string(), Value::Int(1)),
            ("pressure_hold".to_string(), Value::Float(10.0)),
            ("pressure_min".to_string(), Value::Float(4.0)),
            ("pressure_max".to_string(), Value::Float(16.0)),
            ("mov_band_lo".to_string(), Value::Float(85.0)),
            ("mov_band_hi".to_string(), Value::Float(95.0)),
            ("adjust_ticks".to_string(), Value::Int(2)),
            ("min_total_airflow".to_string(), Value::Float(1.0)),
            ("max_pulsing".to_string(), Value::Int(1)),
        ]);
        let build = |parameters: &Parameters| {
            HeaderCoordinator::from_parameters("hdr", PRESSURE, zones(2), OUTPUTS, parameters)
        };
        build(&parameters).unwrap();

        parameters.insert("strategy".to_string(), Value::Int(3));
        match build(&parameters).err().unwrap() {
            ParameterError::Invalid { parameter, .. } => assert_eq!(parameter, "strategy"),
            other => panic!("expected Invalid, got {other:?}"),
        }
        parameters.insert("strategy".to_string(), Value::Int(1));

        parameters.remove("pressure_min");
        match build(&parameters).err().unwrap() {
            ParameterError::Missing { parameter, .. } => {
                assert_eq!(parameter, "pressure_min")
            }
            other => panic!("expected Missing, got {other:?}"),
        }
        parameters.insert("pressure_min".to_string(), Value::Float(17.0));
        match build(&parameters).err().unwrap() {
            ParameterError::Invalid { parameter, .. } => {
                assert_eq!(parameter, "pressure_max")
            }
            other => panic!("expected Invalid, got {other:?}"),
        }
        parameters.insert("pressure_min".to_string(), Value::Float(4.0));

        parameters.insert("mov_band_hi".to_string(), Value::Float(80.0));
        match build(&parameters).err().unwrap() {
            ParameterError::Invalid { parameter, .. } => {
                assert_eq!(parameter, "mov_band_hi")
            }
            other => panic!("expected Invalid, got {other:?}"),
        }
        parameters.insert("mov_band_hi".to_string(), Value::Float(95.0));

        parameters.insert("adjust_ticks".to_string(), Value::Int(0));
        match build(&parameters).err().unwrap() {
            ParameterError::Invalid { parameter, .. } => {
                assert_eq!(parameter, "adjust_ticks")
            }
            other => panic!("expected Invalid, got {other:?}"),
        }
        parameters.insert("adjust_ticks".to_string(), Value::Int(2));

        parameters.insert("min_total_airflow".to_string(), Value::Float(-1.0));
        match build(&parameters).err().unwrap() {
            ParameterError::Invalid { parameter, .. } => {
                assert_eq!(parameter, "min_total_airflow")
            }
            other => panic!("expected Invalid, got {other:?}"),
        }
        parameters.insert("min_total_airflow".to_string(), Value::Float(1.0));

        parameters.insert("pressure_hold".to_string(), Value::Float(f64::NAN));
        match build(&parameters).err().unwrap() {
            ParameterError::Invalid { parameter, .. } => {
                assert_eq!(parameter, "pressure_hold")
            }
            other => panic!("expected Invalid, got {other:?}"),
        }
        parameters.insert("pressure_hold".to_string(), Value::Float(10.0));

        match HeaderCoordinator::new("hdr", PRESSURE, Vec::new(), OUTPUTS, config())
            .err()
            .unwrap()
        {
            ParameterError::Invalid { parameter, .. } => assert_eq!(parameter, "zones"),
            other => panic!("expected Invalid, got {other:?}"),
        }
    }

    #[test]
    fn checkpoint_restore_resumes_the_run_identically() {
        let running = HeaderCoordinatorConfig {
            strategy: CoordinationStrategy::MostOpenValveReset,
            max_pulsing: 2,
            ..config()
        };
        let mut active = component_of(3, running);
        let active_io = io(3);
        feed_float(&active_io, valve_pos(3), 97.0, 1);
        feed_float(&active_io, airflow(1), 2.0, 1);
        feed_pulsing(&active_io, 1, true, 1);
        feed_pulsing(&active_io, 2, true, 1);
        for tick in 1..=3 {
            scan(&mut active, &active_io, tick);
        }
        // Mid-interval, mid-walk: the set-point moved once, the second
        // move is due next scan, and both pulse slots are held.
        assert_eq!(output_float(&active_io, SP), 8.0);
        assert!(granted_to(&active_io, 1));
        assert!(granted_to(&active_io, 2));

        let checkpoint = active.capture_state();
        let mut standby = component_of(3, running);
        standby.restore_state(&checkpoint).unwrap();

        // A promotion mid-adjustment must not step the header
        // pressure: the standby emits the captured set-point through
        // the same cadence as the active.
        let standby_io = io(3);
        feed_float(&standby_io, valve_pos(3), 97.0, 4);
        feed_float(&standby_io, airflow(1), 2.0, 4);
        feed_pulsing(&standby_io, 1, true, 4);
        feed_pulsing(&standby_io, 2, true, 4);
        for tick in 4..=8 {
            scan(&mut active, &active_io, tick);
            scan(&mut standby, &standby_io, tick);
            assert_eq!(active.capture_state(), standby.capture_state());
        }
        assert_eq!(output_float(&active_io, SP), output_float(&standby_io, SP));
    }

    #[test]
    fn restore_rejects_malformed_maps() {
        let mut coordinator = component_of(2, config());
        let io = io(2);
        scan(&mut coordinator, &io, 1);
        let valid = coordinator.capture_state();

        // Unknown, missing, and wrongly-kinded fields all fail.
        let mut unknown = valid.clone();
        unknown.insert("bogus", Value::Int(0));
        assert!(matches!(
            coordinator.restore_state(&unknown),
            Err(StateError::UnknownField { .. })
        ));
        let mut reduced = StateMap::new();
        for (field, value) in valid.iter() {
            if field != "setpoint" {
                reduced.insert(field, value);
            }
        }
        assert!(matches!(
            coordinator.restore_state(&reduced),
            Err(StateError::MissingField { .. })
        ));
        let mut wrong_kind = valid.clone();
        wrong_kind.insert("setpoint", Value::Bool(true));
        assert!(matches!(
            coordinator.restore_state(&wrong_kind),
            Err(StateError::IncompatibleField { .. })
        ));

        // Domain violations: the set-point outside the declared
        // bounds, a demand under the floor, an out-of-range most-open,
        // an elapsed counter past the interval, and more held grants
        // than the cap.
        for (field, value) in [
            ("setpoint", Value::Float(20.0)),
            ("demand", Value::Float(0.5)),
            ("most_open", Value::Int(3)),
            ("adjust_elapsed", Value::Int(2)),
        ] {
            let mut map = valid.clone();
            map.insert(field, value);
            assert!(
                matches!(
                    coordinator.restore_state(&map),
                    Err(StateError::InvalidValue { field: f, .. }) if f == field
                ),
                "expected InvalidValue naming {field}"
            );
        }
        let mut over_granted = valid.clone();
        over_granted.insert("pulse_grant_1", Value::Bool(true));
        over_granted.insert("pulse_grant_2", Value::Bool(true));
        assert!(matches!(
            coordinator.restore_state(&over_granted),
            Err(StateError::InvalidValue { field, .. }) if field == "pulse_grant_2"
        ));
    }

    #[test]
    fn apply_parameter_tunes_and_refuses() {
        let mut coordinator = component_of(2, config());
        coordinator
            .apply_parameter("pressure_hold", Value::Float(12.0))
            .unwrap();
        coordinator
            .apply_parameter("strategy", Value::Int(1))
            .unwrap();
        coordinator
            .apply_parameter("max_pulsing", Value::Int(3))
            .unwrap();
        assert_eq!(coordinator.config().pressure_hold, 12.0);
        assert_eq!(
            coordinator.config().strategy,
            CoordinationStrategy::MostOpenValveReset
        );
        assert_eq!(coordinator.config().max_pulsing, 3);

        // A bound retune breaking the pair names the parameter and
        // changes nothing.
        assert!(matches!(
            coordinator.apply_parameter("pressure_max", Value::Float(3.0)),
            Err(CommandError::InvalidParameter { parameter, .. }) if parameter == "pressure_max"
        ));
        assert_eq!(coordinator.config().pressure_max, 16.0);
        assert!(matches!(
            coordinator.apply_parameter("adjust_ticks", Value::Int(0)),
            Err(CommandError::InvalidParameter { parameter, .. }) if parameter == "adjust_ticks"
        ));
        assert!(matches!(
            coordinator.apply_parameter("strategy", Value::Int(4)),
            Err(CommandError::InvalidParameter { parameter, .. }) if parameter == "strategy"
        ));
        assert!(matches!(
            coordinator.apply_parameter("nope", Value::Int(0)),
            Err(CommandError::UnknownParameter { .. })
        ));
        assert!(matches!(
            coordinator.apply_parameter("pressure_hold", Value::Bool(true)),
            Err(CommandError::ParameterTypeMismatch { .. })
        ));
    }

    #[test]
    fn describe_reports_roles_and_parameter_ranges() {
        let coordinator = component_of(2, config());
        let descriptor = coordinator.describe();
        assert_eq!(descriptor.kind, HeaderCoordinator::KIND);
        let role_of = |name: &str| {
            descriptor
                .ports
                .iter()
                .find(|port| port.name == name)
                .and_then(|port| port.role)
        };
        assert_eq!(role_of("pressure"), Some(PortRole::ProcessValue));
        assert_eq!(role_of("valve_pos_1"), Some(PortRole::ProcessValue));
        assert_eq!(role_of("airflow_2"), Some(PortRole::ProcessValue));
        assert_eq!(role_of("pulsing_2"), Some(PortRole::ProcessValue));
        assert_eq!(role_of("pulse_grant_1"), Some(PortRole::Output));
        assert_eq!(role_of("pressure_sp"), Some(PortRole::Output));
        assert_eq!(role_of("blower_demand"), Some(PortRole::Output));
        assert_eq!(role_of("most_open"), Some(PortRole::Status));
        assert_eq!(role_of("at_bound"), Some(PortRole::Status));
        assert_eq!(role_of("pulse_blocked"), Some(PortRole::Status));

        let parameter_names: Vec<&str> = descriptor
            .parameters
            .iter()
            .map(|parameter| parameter.name.as_str())
            .collect();
        assert_eq!(
            parameter_names,
            [
                "strategy",
                "pressure_hold",
                "pressure_min",
                "pressure_max",
                "mov_band_lo",
                "mov_band_hi",
                "adjust_ticks",
                "min_total_airflow",
                "max_pulsing",
            ]
        );
    }
}
