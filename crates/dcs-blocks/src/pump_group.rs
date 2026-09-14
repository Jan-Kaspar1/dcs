//! Duty/standby pump group: which of N pumps holds duty, rotation of
//! that assignment by a declared policy, lag staging on unmet demand,
//! exclusion of unavailable pumps, and duty handover when a running
//! pump's feedback proves failed — the contract architecture decision
//! 41 records for the water/wastewater station.

use crate::describe;
use crate::params::{self, ParameterError, Parameters};
use dcs_core::{
    CommandError, ComponentDescriptor, ParameterRange, PointId, PortRole, Quality, Sample,
    StateError, StateMap, Tick, Value, ValueKind,
};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// How a [`PumpGroup`] re-assigns the duty pump.
///
/// The model's parameter vocabulary has no string type, so the
/// `rotation` parameter carries the choice as an `Int` code: `0` for
/// per-cycle alternation, `1` for a timed interval, `2` for
/// least-run-hours first — the values [`code`](Self::code) reports and
/// [`decode`](Self::decode) accepts.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RotationPolicy {
    /// Duty moves to the next available pump each time a demand cycle
    /// ends — the decision's recorded reference default.
    AlternateEachCycle,
    /// Duty moves to the next available pump every `rotation_ticks`
    /// scans.
    TimedInterval,
    /// The available pump with the fewest accumulated run ticks takes
    /// duty at each assignment.
    LeastRunHours,
}

impl RotationPolicy {
    /// The `Int` code the `rotation` parameter carries.
    pub const fn code(self) -> i64 {
        match self {
            Self::AlternateEachCycle => 0,
            Self::TimedInterval => 1,
            Self::LeastRunHours => 2,
        }
    }

    /// The policy the `Int` code `code` selects, or `None` when the
    /// code declares no policy.
    pub const fn decode(code: i64) -> Option<Self> {
        match code {
            0 => Some(Self::AlternateEachCycle),
            1 => Some(Self::TimedInterval),
            2 => Some(Self::LeastRunHours),
            _ => None,
        }
    }
}

/// One pump's bound points within a [`PumpGroup`], declared `i` as the
/// pump's 1-based index: `cmd_i` (`Out`, `Bool`) carries the group's
/// run request, `run_i` (`In`, `Bool`) the run feedback, `fault_i`
/// (`In`, `Bool`) the proven-failure flag, and `avail_i` (`In`, `Bool`)
/// the aggregated availability.
#[derive(Debug, Clone, Copy)]
pub struct PumpIo {
    /// `cmd_i` — the group's run request for this pump, typically wired
    /// to the pump's `motor.cmd`.
    pub cmd: PointId,
    /// `run_i` — the pump's run feedback, normally the same field point
    /// its motor verifies.
    pub run: PointId,
    /// `fault_i` — the pump's proven feedback-failure flag, normally
    /// its `motor.fault`.
    pub fault: PointId,
    /// `avail_i` — the aggregated availability: not faulted, not
    /// locally selected, not maintenance-inhibited, permissives
    /// satisfied. The aggregation itself is station wiring; the group
    /// consumes the declared result.
    pub avail: PointId,
}

/// The group-level output points a [`PumpGroup`] reports on: `duty`
/// (`Out`, `Int`) the 1-based index of the pump holding the duty
/// designation — `0` while none does — `staged` (`Out`, `Int`) how many
/// pumps the group currently commands, and the station-alarm
/// conditions `none_available` (`Out`, `Bool`) and `all_faulted`
/// (`Out`, `Bool`).
#[derive(Debug, Clone, Copy)]
pub struct GroupOutputs {
    /// `duty` — the duty holder's 1-based index, `0` while none holds it.
    pub duty: PointId,
    /// `staged` — the count of pumps the group commands.
    pub staged: PointId,
    /// `none_available` — asserts while no pump is available.
    pub none_available: PointId,
    /// `all_faulted` — asserts while every pump's `fault_i` reads failed.
    pub all_faulted: PointId,
}

/// The tuned behavior a [`PumpGroup`] runs under — its parameter map's
/// five keys as one value.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct PumpGroupConfig {
    /// The rotation policy — the `rotation` code.
    pub rotation: RotationPolicy,
    /// The timed policy's interval in scans — `rotation_ticks`; it must
    /// be at least 1 when `rotation` is [`RotationPolicy::TimedInterval`]
    /// and is inert under the other policies.
    pub rotation_ticks: u64,
    /// Minimum ticks between successive pump starts — the
    /// simultaneous-start delay, `start_delay_ticks`.
    pub start_delay_ticks: u64,
    /// Ticks a de-staged pump is held out before it may stage again —
    /// `restage_delay_ticks`.
    pub restage_delay_ticks: u64,
    /// Minimum ticks a stopped pump stays off — `min_off_ticks`, the
    /// tick-domain form of the engineered starts-per-hour bound.
    pub min_off_ticks: u64,
}

/// One pump's run state — the checkpoint carries all of it.
#[derive(Debug, Default, Clone)]
struct PumpState {
    /// Scans the run feedback proved the pump ran — the run-hours
    /// accumulator, in ticks.
    run_hours: u64,
    /// The earliest tick the pump may start; `0` means eligible.
    held_until: u64,
    /// Whether the group commanded the pump on the last scan.
    commanded: bool,
}

/// A duty/standby pump group: manages `N` pumps declared `cmd_1` …
/// `cmd_N`, `run_1` … `run_N`, `fault_1` … `fault_N`, and `avail_1` …
/// `avail_N` under the `trip_N` indexed-port convention — the pump
/// count is discovered from the bound port names at construction.
///
/// **Duty assignment:** exactly one pump holds the duty designation —
/// `duty` reports its 1-based index every scan, `0` while none holds
/// it. The designation persists across demand cycles: a pump keeps
/// duty while stopped until a rotation event or an exclusion reassigns
/// it. The first assignment — and any re-assignment after a scan where
/// nothing was available — selects the rotation policy's choice.
///
/// **Rotation policies:** `rotation` selects when the designation
/// advances.
///
/// - `0` ([`RotationPolicy::AlternateEachCycle`], the recorded
///   default): the scan the effective demand falls from ≥ 1 to `0` —
///   a completed pump-down cycle — duty advances to the next available
///   pump in rotation order after the last holder.
/// - `1` ([`RotationPolicy::TimedInterval`]): every `rotation_ticks`
///   scans, duty advances to the next available pump in rotation order
///   — mid-cycle or idle alike. A duty re-assignment of any kind starts
///   a fresh interval.
/// - `2` ([`RotationPolicy::LeastRunHours`]): the available pump with
///   the fewest accumulated run ticks takes duty at each cycle end and
///   each assignment; ties break to the lowest pump index. The policy
///   does not re-evaluate mid-cycle — a running pump out-accumulating
///   the standby takes over at the next assignment event, never
///   mid-run.
///
/// **Exclusion and handover:** a pump is *available* only while its
/// `avail_i` reads `true` with `Good` quality and its `fault_i` reads
/// `false` with `Good` quality. The running duty holder whose `fault_i`
/// asserts or whose `avail_i` drops hands duty to the policy's next
/// choice at the same scan — the decision's recorded automatic
/// handover; when no pump is available `duty` reports `0` and the
/// designation stands unassigned until one is. Non-duty pumps are
/// simply never staged: an unavailable pump is skipped wherever it
/// sits in the rotation order. Latched lockout-until-reset is not
/// group machinery — it composes in station wiring as an `sr-latch`
/// into `avail_i`.
///
/// **Staging:** the stage order is the duty pump first, then the
/// remaining pumps in rotation order after it. An effective demand of
/// `d` fills the first `d` slots from pumps that are available and
/// either already commanded or past their holdout — an unavailable or
/// held-out pump yields its slot to the next eligible one, and a
/// held-out duty takes its slot back once the holdout clears. A pump
/// not already commanded needs a *start*: it issues only once at least
/// `start_delay_ticks` scans have passed since the last issued start,
/// so at most one new pump starts per scan while the delay stands and
/// all eligible starts issue together when it is `0`. On falling
/// demand the tail of the stage order de-stages — the most recently
/// staged lag stops first. `staged` reports how many pumps the group
/// currently commands.
///
/// **Stop holdouts:** a pump that stops banks a holdout the next start
/// must wait out. A still-available pump dropped from the target set
/// while demand stands — the de-staged lag, or a pump a rotation moved
/// past — holds for `max(min_off_ticks, restage_delay_ticks)`; any
/// other stop — an excluded pump, or a stop at zero demand — holds for
/// `min_off_ticks` alone.
///
/// **Run-hours:** each pump accumulates one tick per scan its `run_i`
/// reads `true` with `Good` quality — proven-running time, commanded
/// or not — feeding the least-run ranking deterministically.
///
/// **Status outputs:** `none_available` asserts while no pump is
/// available and `all_faulted` while every pump's `fault_i` reads
/// failed — the group-raised station conditions the alarm set consumes
/// (`latching-alarm` instances or the Bool sibling the station decision
/// records). `none_available` is implied by `all_faulted` since a
/// faulted pump is never available.
///
/// **Quality rule — every input's non-`Good` reading is fail-safe:**
/// `avail_i` non-`Good` reads unavailable, `fault_i` non-`Good` reads
/// failed, `run_i` non-`Good` cannot prove running and accumulates
/// nothing that scan, and a non-`Good` `demand` holds the last `Good`
/// stage request — the group neither stages up nor rotates on a demand
/// it cannot trust. Every output carries [`Quality::Good`]: the values
/// are the group's own computed decision under these rules.
///
/// A `demand` value below `0` clamps to `0` and above `N` clamps to
/// `N`, so an over-request simply stages every eligible pump.
///
/// Declared I/O: `demand` (`In`, `Int`); per pump `cmd_i` (`Out`,
/// `Bool`), `run_i` (`In`, `Bool`), `fault_i` (`In`, `Bool`), `avail_i`
/// (`In`, `Bool`); `duty` (`Out`, `Int`), `staged` (`Out`, `Int`),
/// `none_available` (`Out`, `Bool`), `all_faulted` (`Out`, `Bool`).
///
/// Parameters: `rotation` — required `Int` carrying a
/// [`RotationPolicy`] code (`0` is the recorded reference default);
/// `rotation_ticks` — optional non-negative `Int` that must be at
/// least `1` when supplied, and is required when `rotation` is `1`;
/// `start_delay_ticks`, `restage_delay_ticks`, `min_off_ticks` —
/// optional non-negative `Int`s, default `0`.
#[derive(Debug)]
pub struct PumpGroup {
    name: String,
    demand: PointId,
    pumps: Vec<PumpIo>,
    outputs: GroupOutputs,
    config: PumpGroupConfig,
    /// The pump holding duty — a 0-based index into `pumps`; `duty`
    /// reports it 1-based and `0` while none holds the designation.
    duty_index: Option<usize>,
    /// Where rotation resumes: the index the next rotation-order pick
    /// starts from — the slot after the last duty holder's.
    rotation_cursor: usize,
    /// Scans banked on the timed interval; a duty re-assignment resets it.
    rotation_elapsed: u64,
    /// The held effective demand — already clamped to `0..=N` — so a
    /// non-`Good` `demand` keeps the last `Good` stage request.
    last_demand: i64,
    /// The tick the most recent commanded start issued at — the
    /// inter-pump start delay's reference.
    last_start: Option<u64>,
    state: Vec<PumpState>,
}

/// The inclusive `Int` bound the `rotation` parameter accepts: the
/// declared [`RotationPolicy`] codes.
const ROTATION_RANGE: ParameterRange = ParameterRange {
    min: Value::Int(0),
    max: Value::Int(2),
};

impl PumpGroup {
    /// The component-kind string the model-driven registry maps onto
    /// this type's constructor.
    pub const KIND: &'static str = "pump-group";

    /// Builds the component from explicit points and the tuned
    /// configuration, or reports an inconsistent one as a
    /// [`ParameterError`].
    ///
    /// `pumps` are the managed pumps in declared order — `pumps[i]` is
    /// pump `i + 1` — and must not be empty. `config.rotation_ticks`
    /// must be at least `1` for [`RotationPolicy::TimedInterval`].
    pub fn new(
        name: impl Into<String>,
        demand: PointId,
        pumps: Vec<PumpIo>,
        outputs: GroupOutputs,
        config: PumpGroupConfig,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        if pumps.is_empty() {
            return Err(params::invalid(
                &name,
                "pumps",
                "must declare at least one pump".to_string(),
            ));
        }
        if config.rotation == RotationPolicy::TimedInterval && config.rotation_ticks == 0 {
            return Err(params::invalid(
                &name,
                "rotation_ticks",
                "timed rotation requires an interval of at least 1".to_string(),
            ));
        }
        Ok(Self {
            name,
            demand,
            outputs,
            config,
            state: vec![PumpState::default(); pumps.len()],
            duty_index: None,
            rotation_cursor: 0,
            rotation_elapsed: 0,
            last_demand: 0,
            last_start: None,
            pumps,
        })
    }

    /// The group's tuned configuration — the reported parameter set.
    pub fn config(&self) -> PumpGroupConfig {
        self.config
    }

    /// Builds the component from a plant-model parameter map, reading
    /// the parameters listed on the type's docs.
    pub fn from_parameters(
        name: impl Into<String>,
        demand: PointId,
        pumps: Vec<PumpIo>,
        outputs: GroupOutputs,
        parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        let code = params::required_u64(&name, parameters, "rotation")?;
        let rotation = RotationPolicy::decode(code as i64).ok_or_else(|| {
            params::invalid(
                &name,
                "rotation",
                format!(
                    "expected 0 (alternate each cycle), 1 (timed interval), or 2 (least run hours), found {code}"
                ),
            )
        })?;
        let rotation_ticks = match params::optional_u64(&name, parameters, "rotation_ticks")? {
            Some(0) => {
                return Err(params::invalid(
                    &name,
                    "rotation_ticks",
                    "must be at least 1".to_string(),
                ));
            }
            Some(ticks) => ticks,
            None if rotation == RotationPolicy::TimedInterval => {
                return Err(ParameterError::Missing {
                    component: name,
                    parameter: "rotation_ticks".to_string(),
                });
            }
            None => 0,
        };
        let config = PumpGroupConfig {
            rotation,
            rotation_ticks,
            start_delay_ticks: params::optional_u64(&name, parameters, "start_delay_ticks")?
                .unwrap_or(0),
            restage_delay_ticks: params::optional_u64(&name, parameters, "restage_delay_ticks")?
                .unwrap_or(0),
            min_off_ticks: params::optional_u64(&name, parameters, "min_off_ticks")?.unwrap_or(0),
        };
        Self::new(name, demand, pumps, outputs, config)
    }

    /// Re-selects the duty holder per the rotation policy and advances
    /// the rotation cursor past the pick; `duty_index` clears when
    /// nothing is available.
    fn assign(&mut self, available: &[bool]) {
        let count = self.pumps.len();
        self.duty_index = match self.config.rotation {
            // The next available pump in rotation order, wrapping.
            RotationPolicy::AlternateEachCycle | RotationPolicy::TimedInterval => (0..count)
                .map(|offset| (self.rotation_cursor + offset) % count)
                .find(|&index| available[index]),
            RotationPolicy::LeastRunHours => (0..count)
                .filter(|&index| available[index])
                .min_by_key(|&index| (self.state[index].run_hours, index)),
        };
        if let Some(index) = self.duty_index {
            self.rotation_cursor = (index + 1) % count;
        }
    }
}

impl Component for PumpGroup {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        let mut requirements = Vec::with_capacity(self.pumps.len() * 4 + 5);
        requirements.push(IoRequirement::input::<i64>("demand", self.demand));
        for (index, pump) in self.pumps.iter().enumerate() {
            let index = index + 1;
            requirements.push(IoRequirement::output::<bool>(
                format!("cmd_{index}"),
                pump.cmd,
            ));
            requirements.push(IoRequirement::input::<bool>(
                format!("run_{index}"),
                pump.run,
            ));
            requirements.push(IoRequirement::input::<bool>(
                format!("fault_{index}"),
                pump.fault,
            ));
            requirements.push(IoRequirement::input::<bool>(
                format!("avail_{index}"),
                pump.avail,
            ));
        }
        requirements.push(IoRequirement::output::<i64>("duty", self.outputs.duty));
        requirements.push(IoRequirement::output::<i64>("staged", self.outputs.staged));
        requirements.push(IoRequirement::output::<bool>(
            "none_available",
            self.outputs.none_available,
        ));
        requirements.push(IoRequirement::output::<bool>(
            "all_faulted",
            self.outputs.all_faulted,
        ));
        requirements
    }

    fn step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError> {
        let count = self.pumps.len();

        // The stage request: a Good demand clamps into 0..=N; a
        // non-Good one holds the last Good request.
        let demand = io.read_typed::<i64>(self.demand)?;
        let demand_effective = if demand.quality.is_good() {
            demand.value.clamp(0, count as i64)
        } else {
            self.last_demand
        };

        // Fail-safe per-pump readings: a non-Good input cannot vouch
        // for its condition, so it reads as the failed side — untrusted
        // availability is unavailable, an untrusted fault flag is
        // failed, and untrusted feedback cannot prove running.
        let mut faulted = vec![false; count];
        let mut available = vec![false; count];
        for (index, pump) in self.pumps.iter().enumerate() {
            let run = io.read_typed::<bool>(pump.run)?;
            let fault = io.read_typed::<bool>(pump.fault)?;
            let avail = io.read_typed::<bool>(pump.avail)?;
            faulted[index] = fault.value || !fault.quality.is_good();
            available[index] = avail.quality.is_good() && avail.value && !faulted[index];
            if run.quality.is_good() && run.value {
                self.state[index].run_hours = self.state[index].run_hours.saturating_add(1);
            }
        }

        // Duty evaluation. Exclusion hands duty over at this scan;
        // otherwise the policy's rotation event may advance it, and an
        // unassigned designation fills as soon as a pump is available.
        if self.config.rotation == RotationPolicy::TimedInterval {
            self.rotation_elapsed = self.rotation_elapsed.saturating_add(1);
        }
        if self.duty_index.is_some_and(|index| !available[index]) {
            self.assign(&available);
            self.rotation_elapsed = 0;
        } else {
            let cycle_ended = self.last_demand >= 1 && demand_effective == 0;
            let rotate = match self.config.rotation {
                RotationPolicy::AlternateEachCycle | RotationPolicy::LeastRunHours => cycle_ended,
                RotationPolicy::TimedInterval => {
                    self.rotation_elapsed >= self.config.rotation_ticks
                }
            };
            if rotate {
                self.assign(&available);
                self.rotation_elapsed = 0;
            }
        }
        if self.duty_index.is_none() {
            self.assign(&available);
            self.rotation_elapsed = 0;
        }

        // The stage order is duty first, then rotation order after it.
        // A slot goes to an available pump that is already commanded or
        // past its holdout; an unavailable or held-out pump yields its
        // slot to the next eligible one.
        let lead = self.duty_index.unwrap_or(self.rotation_cursor);
        let mut targets = Vec::new();
        for offset in 0..count {
            if targets.len() as i64 >= demand_effective {
                break;
            }
            let index = (lead + offset) % count;
            if available[index]
                && (self.state[index].commanded || tick.0 >= self.state[index].held_until)
            {
                targets.push(index);
            }
        }

        // A target not already commanded needs a start, which issues
        // only once the inter-pump delay since the last start has
        // passed — so at most one new start per scan while the delay
        // stands.
        let mut commanded = vec![false; count];
        for &index in &targets {
            if self.state[index].commanded {
                commanded[index] = true;
            } else if self.last_start.is_none_or(|started| {
                tick.0 >= started.saturating_add(self.config.start_delay_ticks)
            }) {
                commanded[index] = true;
                self.last_start = Some(tick.0);
            }
        }

        // Stops bank the pump's holdout: the de-staged lag waits the
        // restage delay beside the minimum-off time; any other stop
        // waits the minimum-off time alone.
        for index in 0..count {
            if self.state[index].commanded && !commanded[index] {
                let hold = if available[index] && demand_effective > 0 {
                    self.config
                        .min_off_ticks
                        .max(self.config.restage_delay_ticks)
                } else {
                    self.config.min_off_ticks
                };
                self.state[index].held_until = tick.0.saturating_add(hold);
            }
            self.state[index].commanded = commanded[index];
        }
        self.last_demand = demand_effective;

        for (index, pump) in self.pumps.iter().enumerate() {
            io.write_sample(
                pump.cmd,
                Sample::new(Value::Bool(commanded[index]), Quality::Good, tick),
            )?;
        }
        io.write_sample(
            self.outputs.duty,
            Sample::new(
                Value::Int(self.duty_index.map_or(0, |index| index as i64 + 1)),
                Quality::Good,
                tick,
            ),
        )?;
        io.write_sample(
            self.outputs.staged,
            Sample::new(
                Value::Int(commanded.iter().filter(|&&on| on).count() as i64),
                Quality::Good,
                tick,
            ),
        )?;
        io.write_sample(
            self.outputs.none_available,
            Sample::new(
                Value::Bool(!available.iter().any(|&on| on)),
                Quality::Good,
                tick,
            ),
        )?;
        io.write_sample(
            self.outputs.all_faulted,
            Sample::new(
                Value::Bool(faulted.iter().all(|&failed| failed)),
                Quality::Good,
                tick,
            ),
        )?;
        Ok(())
    }

    /// Describes the group: `demand` is the stage request the group is
    /// driven toward, each `cmd_i` a driven run request, each `run_i` /
    /// `fault_i` / `avail_i` the measured equipment state it acts on,
    /// and `duty` / `staged` / `none_available` / `all_faulted` the
    /// reported conditions; the five declared parameters with their
    /// ranges.
    fn describe(&self) -> ComponentDescriptor {
        let mut roles: Vec<(String, PortRole)> = vec![("demand".to_string(), PortRole::Setpoint)];
        for index in 1..=self.pumps.len() {
            roles.push((format!("cmd_{index}"), PortRole::Output));
            roles.push((format!("run_{index}"), PortRole::ProcessValue));
            roles.push((format!("fault_{index}"), PortRole::ProcessValue));
            roles.push((format!("avail_{index}"), PortRole::ProcessValue));
        }
        roles.extend([
            ("duty".to_string(), PortRole::Status),
            ("staged".to_string(), PortRole::Status),
            ("none_available".to_string(), PortRole::Status),
            ("all_faulted".to_string(), PortRole::Status),
        ]);
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &roles,
            vec![
                describe::parameter("rotation", ValueKind::Int, Some(ROTATION_RANGE)),
                describe::parameter(
                    "rotation_ticks",
                    ValueKind::Int,
                    Some(describe::POSITIVE_INT),
                ),
                describe::parameter(
                    "start_delay_ticks",
                    ValueKind::Int,
                    Some(describe::NONNEGATIVE_INT),
                ),
                describe::parameter(
                    "restage_delay_ticks",
                    ValueKind::Int,
                    Some(describe::NONNEGATIVE_INT),
                ),
                describe::parameter(
                    "min_off_ticks",
                    ValueKind::Int,
                    Some(describe::NONNEGATIVE_INT),
                ),
            ],
        )
    }

    /// Tunes the declared parameters at the scan boundary. Retuning
    /// `rotation` resets the banked interval so the new policy starts a
    /// fresh one, and selecting the timed policy while `rotation_ticks`
    /// is `0` is refused — set the interval first. `rotation_ticks`
    /// must be at least `1` whenever set.
    fn apply_parameter(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        match parameter {
            "rotation" => {
                let code = params::tune_u64(&self.name, parameter, value)?;
                let policy = RotationPolicy::decode(code as i64).ok_or_else(|| {
                    params::invalid_parameter(
                        &self.name,
                        parameter,
                        "expected 0 (alternate each cycle), 1 (timed interval), or 2 (least run hours)",
                    )
                })?;
                if policy == RotationPolicy::TimedInterval && self.config.rotation_ticks == 0 {
                    return Err(params::invalid_parameter(
                        &self.name,
                        parameter,
                        "timed rotation requires rotation_ticks of at least 1",
                    ));
                }
                self.config.rotation = policy;
                self.rotation_elapsed = 0;
            }
            "rotation_ticks" => {
                let tuned = params::tune_u64(&self.name, parameter, value)?;
                if tuned == 0 {
                    return Err(params::invalid_parameter(
                        &self.name,
                        parameter,
                        "must be at least 1",
                    ));
                }
                self.config.rotation_ticks = tuned;
            }
            "start_delay_ticks" => {
                self.config.start_delay_ticks = params::tune_u64(&self.name, parameter, value)?;
            }
            "restage_delay_ticks" => {
                self.config.restage_delay_ticks = params::tune_u64(&self.name, parameter, value)?;
            }
            "min_off_ticks" => {
                self.config.min_off_ticks = params::tune_u64(&self.name, parameter, value)?;
            }
            _ => return Err(params::unknown_parameter(&self.name, parameter)),
        }
        Ok(())
    }

    /// Reports the tuned parameters — the same fields
    /// [`capture_state`](Self::capture_state) checkpoints, so the
    /// faceplate and a tracking standby read one vocabulary.
    fn report_parameters(&self) -> StateMap {
        let mut parameters = StateMap::new();
        parameters.insert("rotation", Value::Int(self.config.rotation.code()));
        parameters.insert(
            "rotation_ticks",
            Value::Int(self.config.rotation_ticks as i64),
        );
        parameters.insert(
            "start_delay_ticks",
            Value::Int(self.config.start_delay_ticks as i64),
        );
        parameters.insert(
            "restage_delay_ticks",
            Value::Int(self.config.restage_delay_ticks as i64),
        );
        parameters.insert(
            "min_off_ticks",
            Value::Int(self.config.min_off_ticks as i64),
        );
        parameters
    }

    /// Captures the duty assignment and rotation position, the banked
    /// interval, the held demand, the in-flight start/holdout timers,
    /// the per-pump run-hours accumulators and commanded flags, and the
    /// tuned parameters — so a checkpointed standby inherits the duty
    /// assignment and continues the run identically.
    fn capture_state(&self) -> StateMap {
        let mut state = self.report_parameters();
        state.insert(
            "duty",
            Value::Int(self.duty_index.map_or(0, |index| index as i64 + 1)),
        );
        state.insert(
            "rotation_cursor",
            Value::Int(self.rotation_cursor as i64 + 1),
        );
        state.insert("rotation_elapsed", Value::Int(self.rotation_elapsed as i64));
        state.insert("last_demand", Value::Int(self.last_demand));
        state.insert(
            "last_start",
            Value::Int(self.last_start.map_or(0, |started| started as i64)),
        );
        for (index, pump) in self.state.iter().enumerate() {
            state.insert(
                format!("run_hours_{}", index + 1),
                Value::Int(pump.run_hours as i64),
            );
            state.insert(
                format!("held_until_{}", index + 1),
                Value::Int(pump.held_until as i64),
            );
            state.insert(
                format!("commanded_{}", index + 1),
                Value::Bool(pump.commanded),
            );
        }
        state
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        let count = self.pumps.len();
        let mut known: Vec<String> = [
            "rotation",
            "rotation_ticks",
            "start_delay_ticks",
            "restage_delay_ticks",
            "min_off_ticks",
            "duty",
            "rotation_cursor",
            "rotation_elapsed",
            "last_demand",
            "last_start",
        ]
        .into_iter()
        .map(str::to_string)
        .collect();
        for index in 1..=count {
            known.push(format!("run_hours_{index}"));
            known.push(format!("held_until_{index}"));
            known.push(format!("commanded_{index}"));
        }
        let known_refs: Vec<&str> = known.iter().map(String::as_str).collect();
        state.ensure_known_fields(&self.name, &known_refs)?;

        // The whole map is validated before any state mutates, so a
        // rejected checkpoint leaves the component on its last-good
        // state rather than half-applied.
        let non_negative = |field: &str| -> Result<u64, StateError> {
            let value = state.require_i64(&self.name, field)?;
            if value < 0 {
                return Err(StateError::InvalidValue {
                    element: self.name.clone(),
                    field: field.to_string(),
                    value: Value::Int(value),
                });
            }
            Ok(value as u64)
        };

        let rotation = {
            let code = state.require_i64(&self.name, "rotation")?;
            RotationPolicy::decode(code).ok_or_else(|| StateError::InvalidValue {
                element: self.name.clone(),
                field: "rotation".to_string(),
                value: Value::Int(code),
            })?
        };
        let rotation_ticks = non_negative("rotation_ticks")?;
        if rotation == RotationPolicy::TimedInterval && rotation_ticks == 0 {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "rotation_ticks".to_string(),
                value: Value::Int(0),
            });
        }
        let start_delay_ticks = non_negative("start_delay_ticks")?;
        let restage_delay_ticks = non_negative("restage_delay_ticks")?;
        let min_off_ticks = non_negative("min_off_ticks")?;

        let duty = state.require_i64(&self.name, "duty")?;
        if !(0..=count as i64).contains(&duty) {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "duty".to_string(),
                value: Value::Int(duty),
            });
        }
        let rotation_cursor = state.require_i64(&self.name, "rotation_cursor")?;
        // The cursor is the 1-based index rotation resumes at; a held
        // duty assignment always leaves it on the slot after the
        // holder's.
        if !(1..=count as i64).contains(&rotation_cursor)
            || (duty > 0 && rotation_cursor != duty % count as i64 + 1)
        {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "rotation_cursor".to_string(),
                value: Value::Int(rotation_cursor),
            });
        }
        let rotation_elapsed = non_negative("rotation_elapsed")?;
        let last_start = non_negative("last_start")?;
        let last_demand = state.require_i64(&self.name, "last_demand")?;
        if !(0..=count as i64).contains(&last_demand) {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "last_demand".to_string(),
                value: Value::Int(last_demand),
            });
        }

        let mut pumps = Vec::with_capacity(count);
        for index in 1..=count {
            pumps.push(PumpState {
                run_hours: non_negative(&format!("run_hours_{index}"))?,
                held_until: non_negative(&format!("held_until_{index}"))?,
                commanded: state.require_bool(&self.name, &format!("commanded_{index}"))?,
            });
        }

        self.config = PumpGroupConfig {
            rotation,
            rotation_ticks,
            start_delay_ticks,
            restage_delay_ticks,
            min_off_ticks,
        };
        self.duty_index = if duty == 0 {
            None
        } else {
            Some(duty as usize - 1)
        };
        self.rotation_cursor = rotation_cursor as usize - 1;
        self.rotation_elapsed = rotation_elapsed;
        self.last_demand = last_demand;
        self.last_start = if last_start == 0 {
            None
        } else {
            Some(last_start)
        };
        self.state = pumps;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::TestIo;
    use dcs_core::{Direction, ParameterDescriptor, PortDescriptor, QualityReason};

    const DEMAND: PointId = PointId(10);
    const DUTY: PointId = PointId(11);
    const STAGED: PointId = PointId(12);
    const NONE_AVAIL: PointId = PointId(13);
    const ALL_FAULTED: PointId = PointId(14);

    const fn cmd(index: usize) -> PointId {
        PointId(100 + index as u64)
    }
    const fn run(index: usize) -> PointId {
        PointId(200 + index as u64)
    }
    const fn fault(index: usize) -> PointId {
        PointId(300 + index as u64)
    }
    const fn avail(index: usize) -> PointId {
        PointId(400 + index as u64)
    }

    fn pumps(count: usize) -> Vec<PumpIo> {
        (1..=count)
            .map(|index| PumpIo {
                cmd: cmd(index),
                run: run(index),
                fault: fault(index),
                avail: avail(index),
            })
            .collect()
    }

    fn config() -> PumpGroupConfig {
        PumpGroupConfig {
            rotation: RotationPolicy::AlternateEachCycle,
            rotation_ticks: 0,
            start_delay_ticks: 0,
            restage_delay_ticks: 0,
            min_off_ticks: 0,
        }
    }

    /// A two-pump group under the given config, all inputs healthy and idle.
    fn component(config: PumpGroupConfig) -> PumpGroup {
        component_of(2, config)
    }

    const OUTPUTS: GroupOutputs = GroupOutputs {
        duty: DUTY,
        staged: STAGED,
        none_available: NONE_AVAIL,
        all_faulted: ALL_FAULTED,
    };

    fn component_of(count: usize, config: PumpGroupConfig) -> PumpGroup {
        PumpGroup::new("pg", DEMAND, pumps(count), OUTPUTS, config).unwrap()
    }

    fn io(count: usize) -> TestIo {
        let mut points = vec![
            (
                DEMAND,
                Direction::In,
                Sample::good(Value::Int(0), Tick::ZERO),
            ),
            (
                DUTY,
                Direction::Out,
                Sample::good(Value::Int(0), Tick::ZERO),
            ),
            (
                STAGED,
                Direction::Out,
                Sample::good(Value::Int(0), Tick::ZERO),
            ),
            (
                NONE_AVAIL,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
            (
                ALL_FAULTED,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
        ];
        for index in 1..=count {
            points.extend([
                (
                    cmd(index),
                    Direction::Out,
                    Sample::good(Value::Bool(false), Tick::ZERO),
                ),
                (
                    run(index),
                    Direction::In,
                    Sample::good(Value::Bool(false), Tick::ZERO),
                ),
                (
                    fault(index),
                    Direction::In,
                    Sample::good(Value::Bool(false), Tick::ZERO),
                ),
                (
                    avail(index),
                    Direction::In,
                    Sample::good(Value::Bool(true), Tick::ZERO),
                ),
            ]);
        }
        TestIo::new(&points)
    }

    fn feed_demand(io: &TestIo, value: i64, tick: u64) {
        io.feed(DEMAND, Sample::good(Value::Int(value), Tick(tick)));
    }

    fn feed_run(io: &TestIo, index: usize, value: bool, tick: u64) {
        io.feed(run(index), Sample::good(Value::Bool(value), Tick(tick)));
    }

    fn feed_fault(io: &TestIo, index: usize, value: bool, tick: u64) {
        io.feed(fault(index), Sample::good(Value::Bool(value), Tick(tick)));
    }

    fn feed_avail(io: &TestIo, index: usize, value: bool, tick: u64) {
        io.feed(avail(index), Sample::good(Value::Bool(value), Tick(tick)));
    }

    fn commanding(io: &TestIo, index: usize) -> bool {
        matches!(io.written(cmd(index)), Some(sample) if sample.value == Value::Bool(true))
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

    /// Steps the group with the demand and both pumps healthy/available.
    fn scan(group: &mut PumpGroup, io: &TestIo, demand: i64, tick: u64) {
        feed_demand(io, demand, tick);
        group.step(io, Tick(tick)).unwrap();
    }

    #[test]
    fn duty_stands_assigned_while_idle_and_stages_on_demand() {
        let mut group = component(config());
        let io = io(2);
        // With everything available, the first pump takes duty at the
        // first scan even before demand rises.
        scan(&mut group, &io, 0, 1);
        assert_eq!(output_int(&io, DUTY), 1);
        assert_eq!(output_int(&io, STAGED), 0);
        assert!(!commanding(&io, 1));
        assert!(!output_bool(&io, NONE_AVAIL));
        assert!(!output_bool(&io, ALL_FAULTED));

        scan(&mut group, &io, 1, 2);
        assert!(commanding(&io, 1));
        assert!(!commanding(&io, 2));
        assert_eq!(output_int(&io, STAGED), 1);
        assert_eq!(io.written(cmd(1)).unwrap().quality, Quality::Good);
        assert_eq!(io.written(cmd(1)).unwrap().tick, Tick(2));

        // An over-count demand clamps to the declared pumps.
        scan(&mut group, &io, 9, 3);
        assert_eq!(output_int(&io, STAGED), 2);
        assert!(commanding(&io, 1));
        assert!(commanding(&io, 2));
    }

    #[test]
    fn alternates_duty_each_pump_down_cycle() {
        let mut group = component(config());
        let io = io(2);
        // Cycle one runs pump 1; the falling edge of demand advances
        // duty to pump 2 while the designation persists through the
        // idle scans.
        scan(&mut group, &io, 1, 1);
        scan(&mut group, &io, 1, 2);
        assert_eq!(output_int(&io, DUTY), 1);
        scan(&mut group, &io, 0, 3);
        assert_eq!(output_int(&io, DUTY), 2);
        assert!(!commanding(&io, 1));
        scan(&mut group, &io, 0, 4);
        assert_eq!(output_int(&io, DUTY), 2);

        // Cycle two runs pump 2, then hands duty back to pump 1.
        scan(&mut group, &io, 1, 5);
        assert!(commanding(&io, 2));
        assert!(!commanding(&io, 1));
        scan(&mut group, &io, 0, 6);
        assert_eq!(output_int(&io, DUTY), 1);
    }

    #[test]
    fn timed_rotation_advances_duty_on_the_interval() {
        let mut group = component(PumpGroupConfig {
            rotation: RotationPolicy::TimedInterval,
            rotation_ticks: 3,
            ..config()
        });
        let io = io(2);
        scan(&mut group, &io, 1, 1);
        assert_eq!(output_int(&io, DUTY), 1);
        // The interval expires three scans after the assignment —
        // mid-cycle — and duty hands over while the running pump's
        // command moves with it.
        scan(&mut group, &io, 1, 2);
        scan(&mut group, &io, 1, 3);
        assert_eq!(output_int(&io, DUTY), 1);
        scan(&mut group, &io, 1, 4);
        assert_eq!(output_int(&io, DUTY), 2);
        assert!(commanding(&io, 2));
        assert!(!commanding(&io, 1));
        // The handover restarted the interval: the next rotation lands
        // three scans later.
        scan(&mut group, &io, 1, 5);
        scan(&mut group, &io, 1, 6);
        assert_eq!(output_int(&io, DUTY), 2);
        scan(&mut group, &io, 1, 7);
        assert_eq!(output_int(&io, DUTY), 1);
        assert!(commanding(&io, 1));
    }

    #[test]
    fn least_run_hours_assigns_the_least_run_available_pump() {
        let mut group = component(PumpGroupConfig {
            rotation: RotationPolicy::LeastRunHours,
            ..config()
        });
        let io = io(2);
        // Pump 1 holds duty first (the all-zero tie breaks to the
        // lowest index) and accumulates run ticks over its cycle.
        for tick in 1..=3 {
            feed_run(&io, 1, true, tick);
            scan(&mut group, &io, 1, tick);
        }
        assert_eq!(
            group.capture_state().get("run_hours_1"),
            Some(Value::Int(3))
        );
        // The cycle end re-evaluates: pump 2 has fewer run hours and
        // takes duty.
        feed_run(&io, 1, false, 4);
        scan(&mut group, &io, 0, 4);
        assert_eq!(output_int(&io, DUTY), 2);
        // Pump 2 runs its cycle, still short of pump 1's total.
        for tick in 5..=6 {
            feed_run(&io, 2, true, tick);
            scan(&mut group, &io, 1, tick);
        }
        feed_run(&io, 2, false, 7);
        scan(&mut group, &io, 0, 7);
        assert_eq!(output_int(&io, DUTY), 2);
        // Pump 2 out-accumulates pump 1; the next cycle end hands duty
        // back — mid-cycle the running pump is never swapped.
        for tick in 8..=10 {
            feed_run(&io, 2, true, tick);
            scan(&mut group, &io, 1, tick);
            assert_eq!(output_int(&io, DUTY), 2, "tick={tick}");
        }
        feed_run(&io, 2, false, 11);
        scan(&mut group, &io, 0, 11);
        assert_eq!(output_int(&io, DUTY), 1);
    }

    #[test]
    fn an_unavailable_duty_pump_is_skipped_in_favor_of_the_standby() {
        let mut group = component(config());
        let io = io(2);
        feed_avail(&io, 1, false, 1);
        scan(&mut group, &io, 0, 1);
        assert_eq!(output_int(&io, DUTY), 2);
        scan(&mut group, &io, 1, 2);
        assert!(commanding(&io, 2));
        assert!(!commanding(&io, 1));

        // While pump 1 is out, a demand of two can only stage the one
        // available pump.
        scan(&mut group, &io, 2, 3);
        assert_eq!(output_int(&io, STAGED), 1);
        // Its recovery makes it stageable again — in its slot after the
        // duty holder.
        feed_avail(&io, 1, true, 4);
        scan(&mut group, &io, 2, 4);
        assert!(commanding(&io, 1));
        assert_eq!(output_int(&io, STAGED), 2);
    }

    #[test]
    fn a_running_duty_pumps_failure_hands_duty_to_the_standby() {
        let mut group = component(config());
        let io = io(2);
        scan(&mut group, &io, 1, 1);
        assert!(commanding(&io, 1));
        // The fault flag asserts on the running duty pump: the standby
        // takes duty and its command asserts at the same scan —
        // automatic handover, not alarm-and-wait.
        feed_fault(&io, 1, true, 2);
        scan(&mut group, &io, 1, 2);
        assert_eq!(output_int(&io, DUTY), 2);
        assert!(commanding(&io, 2));
        assert!(!commanding(&io, 1));
        assert_eq!(output_int(&io, STAGED), 1);
        assert!(!output_bool(&io, NONE_AVAIL));
        // A recovered fault flag does not hand duty back mid-cycle;
        // the next cycle end picks by the rotation policy.
        feed_fault(&io, 1, false, 3);
        scan(&mut group, &io, 1, 3);
        assert_eq!(output_int(&io, DUTY), 2);
        scan(&mut group, &io, 0, 4);
        assert_eq!(output_int(&io, DUTY), 1);
    }

    #[test]
    fn lag_stages_at_demand_two_and_de_stages_first_on_falling_demand() {
        let mut group = component(config());
        let io = io(2);
        scan(&mut group, &io, 2, 1);
        assert!(commanding(&io, 1));
        assert!(commanding(&io, 2));
        assert_eq!(output_int(&io, STAGED), 2);
        // Falling demand drops the lag — the tail of the stage order —
        // while the duty pump keeps its command.
        scan(&mut group, &io, 1, 2);
        assert!(commanding(&io, 1));
        assert!(!commanding(&io, 2));
        assert_eq!(output_int(&io, STAGED), 1);
        scan(&mut group, &io, 2, 3);
        assert_eq!(output_int(&io, STAGED), 2);
        // Falling all the way to zero stops both.
        scan(&mut group, &io, 0, 4);
        assert_eq!(output_int(&io, STAGED), 0);
    }

    #[test]
    fn start_delay_gates_successive_starts() {
        let mut group = component(PumpGroupConfig {
            start_delay_ticks: 2,
            ..config()
        });
        let io = io(2);
        // The first start issues at once; the second waits out the
        // inter-pump delay counted from it.
        scan(&mut group, &io, 2, 1);
        assert!(commanding(&io, 1));
        assert!(!commanding(&io, 2));
        assert_eq!(output_int(&io, STAGED), 1);
        scan(&mut group, &io, 2, 2);
        assert!(!commanding(&io, 2));
        scan(&mut group, &io, 2, 3);
        assert!(commanding(&io, 2));
        assert_eq!(output_int(&io, STAGED), 2);
    }

    #[test]
    fn restage_delay_holds_a_de_staged_lag_out() {
        let mut group = component(PumpGroupConfig {
            restage_delay_ticks: 3,
            ..config()
        });
        let io = io(2);
        scan(&mut group, &io, 2, 1);
        scan(&mut group, &io, 1, 2);
        assert!(!commanding(&io, 2));
        // Demand rises again inside the restage window: the de-staged
        // lag cannot restart yet.
        scan(&mut group, &io, 2, 3);
        assert!(!commanding(&io, 2));
        assert_eq!(output_int(&io, STAGED), 1);
        scan(&mut group, &io, 2, 4);
        assert!(!commanding(&io, 2));
        // The hold clears three ticks after the stop at tick 2.
        scan(&mut group, &io, 2, 5);
        assert!(commanding(&io, 2));
        assert_eq!(output_int(&io, STAGED), 2);
    }

    #[test]
    fn min_off_holds_a_stopped_pump_after_zero_demand() {
        let mut group = component_of(
            1,
            PumpGroupConfig {
                min_off_ticks: 2,
                ..config()
            },
        );
        let io = io(1);
        scan(&mut group, &io, 1, 1);
        assert!(commanding(&io, 1));
        scan(&mut group, &io, 0, 2);
        // The minimum off-time blocks the restart the rising demand asks
        // for until two ticks after the stop.
        scan(&mut group, &io, 1, 3);
        assert!(!commanding(&io, 1));
        assert_eq!(output_int(&io, STAGED), 0);
        scan(&mut group, &io, 1, 4);
        assert!(commanding(&io, 1));
    }

    #[test]
    fn a_faulted_pump_is_excluded_from_staging_and_duty() {
        let mut group = component(config());
        let io = io(2);
        feed_fault(&io, 2, true, 1);
        scan(&mut group, &io, 0, 1);
        assert_eq!(output_int(&io, DUTY), 1);
        // Demand for two can only stage the unfaulted pump.
        scan(&mut group, &io, 2, 2);
        assert_eq!(output_int(&io, STAGED), 1);
        assert!(!commanding(&io, 2));
        assert!(!output_bool(&io, NONE_AVAIL));
        assert!(!output_bool(&io, ALL_FAULTED));
    }

    #[test]
    fn none_available_and_all_faulted_report_the_group_conditions() {
        let mut group = component(config());
        let io = io(2);
        // No pump available — duty reads 0 and the status asserts.
        feed_avail(&io, 1, false, 1);
        feed_avail(&io, 2, false, 1);
        scan(&mut group, &io, 1, 1);
        assert_eq!(output_int(&io, DUTY), 0);
        assert_eq!(output_int(&io, STAGED), 0);
        assert!(output_bool(&io, NONE_AVAIL));
        assert!(!output_bool(&io, ALL_FAULTED));

        // Every pump faulted reads as all-faulted — which also means
        // none available.
        feed_avail(&io, 1, true, 2);
        feed_avail(&io, 2, true, 2);
        feed_fault(&io, 1, true, 2);
        feed_fault(&io, 2, true, 2);
        scan(&mut group, &io, 1, 2);
        assert_eq!(output_int(&io, DUTY), 0);
        assert!(output_bool(&io, NONE_AVAIL));
        assert!(output_bool(&io, ALL_FAULTED));
    }

    #[test]
    fn non_good_inputs_fail_safe() {
        let mut group = component(config());
        let io = io(2);
        scan(&mut group, &io, 1, 1);
        assert_eq!(output_int(&io, DUTY), 1);

        // A non-Good avail_i cannot vouch for availability: the duty
        // pump is skipped to the standby at the same scan.
        io.feed(
            avail(1),
            Sample::new(
                Value::Bool(true),
                Quality::Bad(QualityReason::CommunicationFault),
                Tick(2),
            ),
        );
        scan(&mut group, &io, 1, 2);
        assert_eq!(output_int(&io, DUTY), 2);
        assert!(commanding(&io, 2));

        // A non-Good fault_i cannot vouch for health: pump 2 hands
        // duty back to the (now good-quality) pump 1.
        feed_avail(&io, 1, true, 3);
        io.feed(
            fault(2),
            Sample::new(
                Value::Bool(false),
                Quality::Uncertain(QualityReason::Substituted),
                Tick(3),
            ),
        );
        scan(&mut group, &io, 1, 3);
        assert_eq!(output_int(&io, DUTY), 1);
        assert!(commanding(&io, 1));

        // A non-Good demand holds the last Good stage request instead
        // of acting on a value it cannot trust.
        io.feed(
            DEMAND,
            Sample::new(
                Value::Int(0),
                Quality::Bad(QualityReason::CommunicationFault),
                Tick(4),
            ),
        );
        group.step(&io, Tick(4)).unwrap();
        assert!(commanding(&io, 1));
        assert_eq!(output_int(&io, STAGED), 1);
        // And it still holds through the following scan.
        group.step(&io, Tick(5)).unwrap();
        assert!(commanding(&io, 1));
    }

    #[test]
    fn non_good_run_feedback_accumulates_no_run_hours() {
        let mut group = component(PumpGroupConfig {
            rotation: RotationPolicy::LeastRunHours,
            ..config()
        });
        let io = io(2);
        feed_run(&io, 1, true, 1);
        scan(&mut group, &io, 1, 1);
        feed_run(&io, 1, true, 2);
        scan(&mut group, &io, 1, 2);
        // A scan of non-Good feedback proves nothing — the accumulator
        // stays put.
        io.feed(
            run(1),
            Sample::new(
                Value::Bool(true),
                Quality::Bad(QualityReason::CommunicationFault),
                Tick(3),
            ),
        );
        group.step(&io, Tick(3)).unwrap();
        assert_eq!(
            group.capture_state().get("run_hours_1"),
            Some(Value::Int(2))
        );
        feed_run(&io, 1, true, 4);
        scan(&mut group, &io, 1, 4);
        assert_eq!(
            group.capture_state().get("run_hours_1"),
            Some(Value::Int(3))
        );
    }

    #[test]
    fn capture_and_restore_continues_the_run_identically() {
        let timed = PumpGroupConfig {
            rotation: RotationPolicy::TimedInterval,
            rotation_ticks: 4,
            start_delay_ticks: 1,
            restage_delay_ticks: 2,
            min_off_ticks: 1,
        };
        let mut first = component(timed);
        let io = io(2);
        for tick in 1..=5 {
            feed_run(&io, 1, true, tick);
            feed_run(&io, 2, true, tick);
            scan(&mut first, &io, 2, tick);
        }
        let captured = first.capture_state();
        // The timed interval expired at tick 5, mid-capture: duty has
        // already handed to pump 2 and the banked interval restarted.
        assert_eq!(captured.get("duty"), Some(Value::Int(2)));
        assert_eq!(captured.get("run_hours_1"), Some(Value::Int(5)));
        assert_eq!(captured.get("run_hours_2"), Some(Value::Int(5)));
        assert_eq!(captured.get("rotation_elapsed"), Some(Value::Int(0)));
        assert_eq!(captured.get("commanded_1"), Some(Value::Bool(true)));

        let mut second = component(timed);
        second.restore_state(&captured).unwrap();
        // From here the two groups step identically — the restored one
        // inherited the duty assignment, the banked interval, the
        // run-hours, and the timers.
        for tick in 6..=10 {
            feed_run(&io, 1, true, tick);
            feed_run(&io, 2, true, tick);
            feed_demand(&io, 2, tick);
            first.step(&io, Tick(tick)).unwrap();
            let expected: Vec<Sample> = [cmd(1), cmd(2), DUTY, STAGED, NONE_AVAIL, ALL_FAULTED]
                .into_iter()
                .map(|point| io.written(point).unwrap())
                .collect();
            second.step(&io, Tick(tick)).unwrap();
            for (point, sample) in [cmd(1), cmd(2), DUTY, STAGED, NONE_AVAIL, ALL_FAULTED]
                .into_iter()
                .zip(expected)
            {
                assert_eq!(
                    io.written(point).unwrap(),
                    sample,
                    "point={point:?} tick={tick}"
                );
            }
        }
        // The timed rotation still lands on its schedule: four scans
        // after the last assignment.
        assert_eq!(
            second.capture_state().get("duty"),
            first.capture_state().get("duty")
        );
        assert_eq!(
            second.capture_state().get("run_hours_1"),
            first.capture_state().get("run_hours_1")
        );
    }

    #[test]
    fn restores_state_and_rejects_malformed_maps() {
        let mut group = component(config());
        // Round-trip a healthy map first.
        scan(&mut group, &io(2), 1, 1);
        let captured = group.capture_state();
        let mut restored = component(config());
        restored.restore_state(&captured).unwrap();

        // An unknown field rejects rather than silently dropping.
        let mut unknown = captured.clone();
        unknown.insert("mystery", Value::Int(0));
        assert!(matches!(
            group.restore_state(&unknown),
            Err(StateError::UnknownField { ref field, .. }) if field == "mystery"
        ));

        // An out-of-range rotation code names its field.
        let mut bad = captured.clone();
        bad.insert("rotation", Value::Int(9));
        assert!(matches!(
            group.restore_state(&bad),
            Err(StateError::InvalidValue { ref field, .. }) if field == "rotation"
        ));

        // A duty index beyond the declared pumps names its field.
        let mut bad = captured.clone();
        bad.insert("duty", Value::Int(3));
        assert!(matches!(
            group.restore_state(&bad),
            Err(StateError::InvalidValue { ref field, .. }) if field == "duty"
        ));

        // A rotation cursor inconsistent with the held duty names its
        // field: duty on pump 1 always leaves the cursor on pump 2's
        // slot.
        let mut bad = captured.clone();
        bad.insert("rotation_cursor", Value::Int(1));
        assert!(matches!(
            group.restore_state(&bad),
            Err(StateError::InvalidValue { ref field, .. }) if field == "rotation_cursor"
        ));

        // A negative run-hours accumulator names its field.
        let mut bad = captured.clone();
        bad.insert("run_hours_1", Value::Int(-1));
        assert!(matches!(
            group.restore_state(&bad),
            Err(StateError::InvalidValue { ref field, .. }) if field == "run_hours_1"
        ));

        // A missing field names the field it needed — an empty map's
        // first required read is `rotation`.
        assert!(matches!(
            group.restore_state(&StateMap::new()),
            Err(StateError::MissingField { ref field, .. }) if field == "rotation"
        ));
    }

    #[test]
    fn reports_and_retunes_parameters() {
        let mut group = component(PumpGroupConfig {
            rotation: RotationPolicy::AlternateEachCycle,
            rotation_ticks: 0,
            start_delay_ticks: 1,
            restage_delay_ticks: 2,
            min_off_ticks: 3,
        });
        let reported = group.report_parameters();
        assert_eq!(reported.get("rotation"), Some(Value::Int(0)));
        assert_eq!(reported.get("start_delay_ticks"), Some(Value::Int(1)));
        assert_eq!(reported.get("restage_delay_ticks"), Some(Value::Int(2)));
        assert_eq!(reported.get("min_off_ticks"), Some(Value::Int(3)));

        group
            .apply_parameter("min_off_ticks", Value::Int(6))
            .unwrap();
        assert_eq!(group.config.min_off_ticks, 6);
        group.apply_parameter("rotation", Value::Int(2)).unwrap();
        assert_eq!(group.config.rotation, RotationPolicy::LeastRunHours);

        // Selecting timed rotation while no interval is set is refused;
        // setting the interval first tunes through.
        assert!(matches!(
            group.apply_parameter("rotation", Value::Int(1)),
            Err(CommandError::InvalidParameter { ref parameter, .. }) if parameter == "rotation"
        ));
        group
            .apply_parameter("rotation_ticks", Value::Int(10))
            .unwrap();
        group.apply_parameter("rotation", Value::Int(1)).unwrap();
        assert_eq!(group.config.rotation, RotationPolicy::TimedInterval);
        assert!(matches!(
            group.apply_parameter("rotation_ticks", Value::Int(0)),
            Err(CommandError::InvalidParameter { ref parameter, .. }) if parameter == "rotation_ticks"
        ));
        assert!(matches!(
            group.apply_parameter("rotation", Value::Int(7)),
            Err(CommandError::InvalidParameter { ref parameter, .. }) if parameter == "rotation"
        ));
        assert!(matches!(
            group.apply_parameter("unlisted", Value::Int(1)),
            Err(CommandError::UnknownParameter { ref parameter, .. }) if parameter == "unlisted"
        ));
    }

    #[test]
    fn builds_from_parameters_and_names_offenders() {
        let parameters: Parameters = [
            ("rotation".to_string(), Value::Int(1)),
            ("rotation_ticks".to_string(), Value::Int(5)),
            ("start_delay_ticks".to_string(), Value::Int(2)),
            ("restage_delay_ticks".to_string(), Value::Int(3)),
            ("min_off_ticks".to_string(), Value::Int(4)),
        ]
        .into_iter()
        .collect();
        let group =
            PumpGroup::from_parameters("pg", DEMAND, pumps(2), OUTPUTS, &parameters).unwrap();
        assert_eq!(
            group.config,
            PumpGroupConfig {
                rotation: RotationPolicy::TimedInterval,
                rotation_ticks: 5,
                start_delay_ticks: 2,
                restage_delay_ticks: 3,
                min_off_ticks: 4,
            }
        );

        // `rotation` is required; a map without it names the parameter.
        assert!(matches!(
            PumpGroup::from_parameters("pg", DEMAND, pumps(2), OUTPUTS, &Parameters::new())
            .unwrap_err(),
            ParameterError::Missing { ref parameter, .. } if parameter == "rotation"
        ));

        // Timed rotation without its interval names the missing one.
        let timed: Parameters = [("rotation".to_string(), Value::Int(1))]
            .into_iter()
            .collect();
        assert!(matches!(
            PumpGroup::from_parameters("pg", DEMAND, pumps(2), OUTPUTS, &timed)
            .unwrap_err(),
            ParameterError::Missing { ref parameter, .. } if parameter == "rotation_ticks"
        ));

        // A zero interval, an unknown rotation code, a wrong-typed
        // parameter, and a negative delay each name their parameter.
        for (parameters, parameter) in [
            (
                [
                    ("rotation".to_string(), Value::Int(1)),
                    ("rotation_ticks".to_string(), Value::Int(0)),
                ]
                .into_iter()
                .collect(),
                "rotation_ticks",
            ),
            (
                [("rotation".to_string(), Value::Int(9))]
                    .into_iter()
                    .collect(),
                "rotation",
            ),
            (
                [("rotation".to_string(), Value::Bool(true))]
                    .into_iter()
                    .collect(),
                "rotation",
            ),
            (
                [
                    ("rotation".to_string(), Value::Int(0)),
                    ("min_off_ticks".to_string(), Value::Int(-2)),
                ]
                .into_iter()
                .collect(),
                "min_off_ticks",
            ),
        ] {
            let parameters: Parameters = parameters;
            assert!(
                matches!(
                    PumpGroup::from_parameters("pg", DEMAND, pumps(2), OUTPUTS, &parameters)
                    .unwrap_err(),
                    ParameterError::Invalid { parameter: found, .. } if found == parameter
                ),
                "parameters={parameters:?}"
            );
        }

        // An empty pump set is invalid, and `new` refuses a timed
        // policy without an interval.
        assert!(matches!(
            PumpGroup::new("pg", DEMAND, vec![], OUTPUTS, config())
                .unwrap_err(),
            ParameterError::Invalid { ref parameter, .. } if parameter == "pumps"
        ));
        assert!(matches!(
            PumpGroup::new(
                "pg",
                DEMAND,
                pumps(2),
                OUTPUTS,
                PumpGroupConfig {
                    rotation: RotationPolicy::TimedInterval,
                    ..config()
                },
            )
            .unwrap_err(),
            ParameterError::Invalid { ref parameter, .. } if parameter == "rotation_ticks"
        ));
    }

    #[test]
    fn describes_itself() {
        let descriptor = component(config()).describe();
        assert_eq!(descriptor.name, "pg");
        assert_eq!(descriptor.kind, PumpGroup::KIND);
        assert_eq!(
            descriptor.ports,
            [
                PortDescriptor {
                    name: "demand".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Int,
                    role: Some(PortRole::Setpoint),
                    point: None,
                },
                PortDescriptor {
                    name: "cmd_1".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Output),
                    point: None,
                },
                PortDescriptor {
                    name: "run_1".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::ProcessValue),
                    point: None,
                },
                PortDescriptor {
                    name: "fault_1".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::ProcessValue),
                    point: None,
                },
                PortDescriptor {
                    name: "avail_1".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::ProcessValue),
                    point: None,
                },
                PortDescriptor {
                    name: "cmd_2".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Output),
                    point: None,
                },
                PortDescriptor {
                    name: "run_2".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::ProcessValue),
                    point: None,
                },
                PortDescriptor {
                    name: "fault_2".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::ProcessValue),
                    point: None,
                },
                PortDescriptor {
                    name: "avail_2".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::ProcessValue),
                    point: None,
                },
                PortDescriptor {
                    name: "duty".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Int,
                    role: Some(PortRole::Status),
                    point: None,
                },
                PortDescriptor {
                    name: "staged".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Int,
                    role: Some(PortRole::Status),
                    point: None,
                },
                PortDescriptor {
                    name: "none_available".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Status),
                    point: None,
                },
                PortDescriptor {
                    name: "all_faulted".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Status),
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
                    name: "rotation".to_string(),
                    kind: ValueKind::Int,
                    range: Some(ROTATION_RANGE),
                },
                ParameterDescriptor {
                    name: "rotation_ticks".to_string(),
                    kind: ValueKind::Int,
                    range: Some(describe::POSITIVE_INT),
                },
                ParameterDescriptor {
                    name: "start_delay_ticks".to_string(),
                    kind: ValueKind::Int,
                    range: Some(describe::NONNEGATIVE_INT),
                },
                ParameterDescriptor {
                    name: "restage_delay_ticks".to_string(),
                    kind: ValueKind::Int,
                    range: Some(describe::NONNEGATIVE_INT),
                },
                ParameterDescriptor {
                    name: "min_off_ticks".to_string(),
                    kind: ValueKind::Int,
                    range: Some(describe::NONNEGATIVE_INT),
                },
            ]
        );
    }
}
