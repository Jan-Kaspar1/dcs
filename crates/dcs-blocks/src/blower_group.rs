//! Blower-group capacity staging: which of N blowers runs to meet a
//! continuous capacity demand, the per-unit capacity split clamped to
//! declared machine bounds, the vent-based join/departure
//! choreography, and the declared rotation and staging-authority
//! policies — the contract architecture decision 63 records for
//! `WW-CTL-005`.

use crate::describe;
use crate::params::{self, ParameterError, Parameters};
use dcs_core::{
    CommandError, ComponentDescriptor, ParameterRange, PointId, PortRole, Quality, Sample,
    StateError, StateMap, Tick, Value, ValueKind,
};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// Who may move the staged set — the `staging_authority` parameter's
/// `Int` code.
///
/// The model's parameter vocabulary has no string type, so
/// `staging_authority` carries the choice as an `Int` code — the
/// values [`code`](Self::code) reports and [`decode`](Self::decode)
/// accepts.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StagingAuthority {
    /// `0` — the group stages up and down and rotates on its own
    /// evaluation.
    Automatic,
    /// `1` — a computed stage change holds on `staging_pending` until a
    /// `Good` `true` on the bound `approve` point grants it. One
    /// assertion grants one change; a standing `approve` is consumed
    /// by the first change it releases and must clear before the next
    /// grant. The authority requires a bound `approve` port — without
    /// the release path nothing could ever stage.
    OperatorApproval,
    /// `2` — the group commands nothing: `staging_pending` flags a
    /// warranted change while `cmd_i`/`vent_i`/`capacity_i` stay off —
    /// the flag-only advisory mode.
    FlagOnly,
}

impl StagingAuthority {
    /// The `Int` code the `staging_authority` parameter carries.
    pub const fn code(self) -> i64 {
        match self {
            Self::Automatic => 0,
            Self::OperatorApproval => 1,
            Self::FlagOnly => 2,
        }
    }

    /// The authority the `Int` code `code` selects, or `None` when the
    /// code declares none.
    pub const fn decode(code: i64) -> Option<Self> {
        match code {
            0 => Some(Self::Automatic),
            1 => Some(Self::OperatorApproval),
            2 => Some(Self::FlagOnly),
            _ => None,
        }
    }
}

/// How a [`BlowerGroup`] picks the unit a stage change or rotation
/// touches — the `rotation` parameter's `Int` code.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BlowerRotation {
    /// `0` — no maintenance rotation: joins pick the lowest-indexed
    /// eligible unit and departures take the most recently joined.
    NoRotation,
    /// `1` — equalize runtime: joins pick the eligible unit with the
    /// fewest accumulated run-hours, departures take the most-run
    /// joined unit, and while demand is met the group swaps the
    /// most-run joined unit for the least-run standby once the spread
    /// reaches `max(1, min_run_ticks)` — through the same join-then-
    /// depart choreography, `transition` asserted across the handover.
    EqualizeRuntime,
    /// `2` — fixed order: joins pick the lowest-indexed eligible unit
    /// and departures take the highest-indexed joined unit; no
    /// maintenance rotation runs.
    FixedOrder,
}

impl BlowerRotation {
    /// The `Int` code the `rotation` parameter carries.
    pub const fn code(self) -> i64 {
        match self {
            Self::NoRotation => 0,
            Self::EqualizeRuntime => 1,
            Self::FixedOrder => 2,
        }
    }

    /// The policy the `Int` code `code` selects, or `None` when the
    /// code declares no policy.
    pub const fn decode(code: i64) -> Option<Self> {
        match code {
            0 => Some(Self::NoRotation),
            1 => Some(Self::EqualizeRuntime),
            2 => Some(Self::FixedOrder),
            _ => None,
        }
    }
}

/// One blower's bound points within a [`BlowerGroup`], declared `i` as
/// the unit's 1-based index: `cmd_i` (`Out`, `Bool`) carries the
/// group's run request, `run_i`/`fault_i`/`avail_i` (`In`, `Bool`) are
/// the pump-group equipment family, `capacity_i` (`Out`, `Float`) the
/// unit's share of the demand under the declared split, and `vent_i`
/// (`Out`, `Bool`) the unloading/vent valve command — `true` vents the
/// unit off the header.
#[derive(Debug, Clone, Copy)]
pub struct BlowerIo {
    /// `cmd_i` — the group's run request for this unit, typically wired
    /// to the unit's `motor.cmd`.
    pub cmd: PointId,
    /// `run_i` — the unit's run feedback, normally the same field point
    /// its motor verifies; proves the join and the departure.
    pub run: PointId,
    /// `fault_i` — the unit's proven feedback-failure flag.
    pub fault: PointId,
    /// `avail_i` — the aggregated availability: not faulted, not
    /// locally selected, not maintenance-inhibited, permissives
    /// satisfied. The aggregation itself is station wiring; the group
    /// consumes the declared result.
    pub avail: PointId,
    /// `capacity_i` — the unit's share of `demand` under the declared
    /// equal split, clamped to its declared bounds — feeding the
    /// speed/vane demand path through the decision-64 guard.
    pub capacity: PointId,
    /// `vent_i` — the unloading/vent valve command: `true` holds the
    /// unit off the header while it starts or stops.
    pub vent: PointId,
}

/// The declared machine bounds one blower's capacity demand is clamped
/// to — the per-unit `unit_<i>_min_flow`/`unit_<i>_max_flow`/
/// `unit_<i>_max_current` parameters as one value.
///
/// `capacity_i` emits in `demand`'s coordinate; `max_current` is the
/// unit's current bound expressed in that coordinate — the effective
/// ceiling is `min(max_flow, max_current)`. All three are the
/// decision's assumption-marked machine data.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct UnitBounds {
    /// `unit_<i>_min_flow` — the floor `capacity_i` cannot fall below
    /// while the unit is on the header, and the share the unit emits
    /// while it proves its start.
    pub min_flow: f64,
    /// `unit_<i>_max_flow` — the unit's declared flow ceiling.
    pub max_flow: f64,
    /// `unit_<i>_max_current` — the unit's declared current bound in
    /// the demand coordinate — a second ceiling on `capacity_i`.
    pub max_current: f64,
}

impl UnitBounds {
    /// The ceiling `capacity_i` clamps to — the tighter of the flow
    /// and current bounds.
    fn effective_max(&self) -> f64 {
        self.max_flow.min(self.max_current)
    }

    /// Validates the declared bounds for unit `index` (1-based),
    /// reporting a violation as a [`ParameterError`] naming the
    /// offending `unit_<index>_*` parameter.
    fn checked(component: &str, index: usize, bounds: Self) -> Result<Self, ParameterError> {
        let names = (
            format!("unit_{index}_min_flow"),
            format!("unit_{index}_max_flow"),
            format!("unit_{index}_max_current"),
        );
        for (parameter, value) in [
            (names.0.as_str(), bounds.min_flow),
            (names.1.as_str(), bounds.max_flow),
            (names.2.as_str(), bounds.max_current),
        ] {
            if !value.is_finite() || value < 0.0 {
                return Err(params::invalid(
                    component,
                    parameter,
                    "must be a non-negative finite value".to_string(),
                ));
            }
        }
        if bounds.min_flow > bounds.max_flow {
            return Err(params::invalid(
                component,
                &names.1,
                format!("must be at least {}", names.0),
            ));
        }
        if bounds.min_flow > bounds.max_current {
            return Err(params::invalid(
                component,
                &names.2,
                format!("must be at least {}", names.0),
            ));
        }
        Ok(bounds)
    }
}

/// The group-level output points a [`BlowerGroup`] reports on:
/// `staged` (`Out`, `Int`) how many units the group currently
/// commands, `none_available`/`all_faulted` (`Out`, `Bool`) the
/// pump-group station conditions, `staging_pending` (`Out`, `Bool`) a
/// stage change held for approval or flagged as the operator's
/// recommendation, and `transition` (`Out`, `Bool`) a start, stop, or
/// rotation transition in progress — the surface the valve-freeze
/// wiring consumes.
#[derive(Debug, Clone, Copy)]
pub struct BlowerOutputs {
    /// `staged` — the count of units the group commands.
    pub staged: PointId,
    /// `none_available` — asserts while no unit is available.
    pub none_available: PointId,
    /// `all_faulted` — asserts while every unit's `fault_i` reads
    /// failed.
    pub all_faulted: PointId,
    /// `staging_pending` — a stage change standing unexecuted under a
    /// non-automatic authority: held for operator approval, or the
    /// flag-only recommendation.
    pub staging_pending: PointId,
    /// `transition` — a unit's join or departure in progress, or a
    /// rotation's pending second leg — the freeze surface the
    /// most-open-valve feedback wiring consumes.
    pub transition: PointId,
}

/// The tuned behavior a [`BlowerGroup`] runs under — its parameter
/// map's shared keys as one value.
///
/// `staging_authority` is the [`StagingAuthority`] code governing who
/// may move the staged set; `stage_up`/`stage_down` are the declared
/// demand thresholds — fractions of the committed capacity; a
/// stage-up computes while `demand > stage_up × committed`, a
/// stage-down while dropping one unit still leaves `demand <
/// stage_down × (committed − that unit's ceiling)`, and a non-positive
/// demand de-stages everything. `min_run_ticks` is the minimum run
/// time a joined unit must serve before it may depart;
/// `min_start_interval_ticks` is the time a stopped unit must sit out
/// before it may restart; `vent_ticks` is the prove window both legs
/// of the choreography get. `rotation` is the [`BlowerRotation`] code.
/// Every value is required declared data — the decision's recorded
/// customer-validation assumptions, never silently defaulted.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct BlowerGroupConfig {
    /// The staging authority — the `staging_authority` code.
    pub staging_authority: StagingAuthority,
    /// `stage_up` — the committed-capacity fraction whose exceedance
    /// warrants another unit.
    pub stage_up: f64,
    /// `stage_down` — the remaining-capacity fraction below which a
    /// unit may depart.
    pub stage_down: f64,
    /// `min_run_ticks` — scans a joined unit must serve before it may
    /// depart.
    pub min_run_ticks: u64,
    /// `min_start_interval_ticks` — scans a stopped unit must sit out
    /// before it may start again.
    pub min_start_interval_ticks: u64,
    /// `vent_ticks` — the scans a join has to prove `run_i` and a
    /// departure has to prove the stop before the choreography
    /// resolves the unit on its own.
    pub vent_ticks: u64,
    /// The rotation policy — the `rotation` code.
    pub rotation: BlowerRotation,
}

impl BlowerGroupConfig {
    /// Validates the invariants every construction path enforces —
    /// both thresholds non-negative and finite, `vent_ticks` at least
    /// one — reporting a violation as a [`ParameterError`] naming
    /// `component` and the offending parameter.
    fn checked(component: &str, config: Self) -> Result<Self, ParameterError> {
        for (parameter, value) in [
            ("stage_up", config.stage_up),
            ("stage_down", config.stage_down),
        ] {
            if !value.is_finite() || value < 0.0 {
                return Err(params::invalid(
                    component,
                    parameter,
                    "must be a non-negative finite value".to_string(),
                ));
            }
        }
        if config.vent_ticks < 1 {
            return Err(params::invalid(
                component,
                "vent_ticks",
                "must be at least 1".to_string(),
            ));
        }
        Ok(config)
    }
}

/// Where one unit sits in the join/departure choreography — the
/// checkpoint carries it as the `phase_<i>` code.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
enum Phase {
    /// `0` — uncommanded: `cmd_i`/`vent_i` off, `capacity_i` `0`.
    #[default]
    Offline,
    /// `1` — starting off the header: `cmd_i` and `vent_i` on,
    /// `capacity_i` at the declared minimum while `run_i` proves.
    Joining,
    /// `2` — on the header: `cmd_i` on, `vent_i` off, `capacity_i`
    /// carrying its share.
    Joined,
    /// `3` — stopping off the header: `cmd_i` off, `vent_i` on, while
    /// `run_i` proves the stop.
    Departing,
}

impl Phase {
    /// The `Int` code the `phase_<i>` state field carries.
    fn code(self) -> i64 {
        match self {
            Self::Offline => 0,
            Self::Joining => 1,
            Self::Joined => 2,
            Self::Departing => 3,
        }
    }

    /// The phase `code` selects, or `None` when it names no phase.
    fn decode(code: i64) -> Option<Self> {
        match code {
            0 => Some(Self::Offline),
            1 => Some(Self::Joining),
            2 => Some(Self::Joined),
            3 => Some(Self::Departing),
            _ => None,
        }
    }
}

/// One unit's run state — the checkpoint carries all of it.
#[derive(Debug, Default, Clone)]
struct BlowerState {
    /// Where the unit sits in the join/departure choreography.
    phase: Phase,
    /// Scans the current `Joining`/`Departing` leg has run unproven —
    /// the `vent_ticks` window's elapsed count; `0` in a settled phase.
    phase_ticks: u64,
    /// Scans the run feedback proved the unit ran — the run-hours
    /// accumulator, in ticks.
    run_hours: u64,
    /// The tick the group's last run request for this unit issued;
    /// `0` means never — the `min_run_ticks` reference.
    started_at: u64,
    /// The tick the unit last returned to `Offline` after being
    /// commanded; `0` means never — the `min_start_interval_ticks`
    /// reference.
    stopped_at: u64,
}

/// A stage change the group computed — the joiner, the departure, or
/// the rotation pair.
#[derive(Debug, Clone, Copy)]
enum Request {
    /// Stage up: join the unit the rotation policy picks.
    StageUp(usize),
    /// Stage down: depart the unit the rotation policy picks.
    StageDown(usize),
    /// Equalize-runtime rotation: join the standby, then depart the
    /// joined unit once the join completes.
    Rotate(usize, usize),
}

/// A staged-blower group: manages `N` blowers declared `cmd_i`,
/// `run_i`, `fault_i`, `avail_i`, `capacity_i`, `vent_i` for `i` in
/// `1..=N` under the indexed-port convention — the unit count is
/// discovered from the bound port names at construction — plus the
/// continuous `demand` (`In`, `Float`), the optional `approve` (`In`,
/// `Bool`) operator release, and the status outputs `staged`,
/// `none_available`, `all_faulted`, `staging_pending`, `transition`.
///
/// **Staging:** `demand` is the aggregate capacity the header needs —
/// the header-coordinator's `blower_demand`. The *committed* capacity
/// is the sum of the effective ceilings `min(unit_<i>_max_flow,
/// unit_<i>_max_current)` over the units the group commands (joining
/// plus joined). `demand > stage_up × committed` warrants a stage-up
/// while an offline unit remains; `demand <= 0`, or `demand <
/// stage_down × (committed − the departing unit's ceiling)`, warrants
/// a stage-down while a unit sits joined. Elective stage changes
/// serialize — at most one unit's join or departure runs at a time,
/// so a second warranted change waits for the in-flight leg.
///
/// **The capacity split:** `capacity_i` emits `demand` shared equally
/// across the joined units, clamped to the unit's declared bounds
/// `unit_<i>_min_flow`…`min(unit_<i>_max_flow, unit_<i>_max_current)` —
/// a saturated unit simply holds its bound, the split does not
/// redistribute the shortfall. A unit proving its start emits its
/// `min_flow`; an offline or departing unit emits `0`.
///
/// **The join choreography:** joining a unit runs the decision's
/// offline-start sequence — `vent_i` opens and `cmd_i` asserts with
/// the unit held off the header, `run_i` must prove within
/// `vent_ticks` scans, then `vent_i` closes and the unit joins. A
/// start unproven after `vent_ticks`, or a unit that faults or loses
/// availability mid-join, aborts back to `Offline` — the field failure
/// reports through `fault_i`/`avail_i`. Departure runs the sequence in
/// reverse: `vent_i` opens and `cmd_i` drops together, `run_i` must
/// prove the stop within `vent_ticks`, then `vent_i` closes. A stop
/// unproven after `vent_ticks` resolves the unit `Offline` anyway —
/// the still-running machine keeps accumulating run-hours and reports
/// through `fault_i`. A joined unit whose `fault_i` asserts or whose
/// `avail_i` drops departs at the same scan — the demand it carried
/// hands to the next eligible standby the following scan.
///
/// **Machine protections:** a joined unit may not depart until it has
/// served `min_run_ticks` since its command asserted; a stopped unit
/// may not restart until `min_start_interval_ticks` have passed since
/// it returned to `Offline` — a start aborted mid-join banks the
/// interval too. A warranted change whose unit is held out waits: the
/// request stands until the timer clears.
///
/// **Rotation:** `rotation` selects the join/departure pick and the
/// maintenance swap. `0` ([`BlowerRotation::NoRotation`]) joins the
/// lowest-indexed eligible unit and departs the most recently joined
/// (LIFO). `2` ([`BlowerRotation::FixedOrder`]) joins lowest-indexed
/// and departs highest-indexed. `1`
/// ([`BlowerRotation::EqualizeRuntime`]) joins the least-run eligible
/// unit and departs the most-run joined one; and while no stage
/// request stands, a least-run available standby whose run-hours lag
/// the most-run depart-eligible unit's by `max(1, min_run_ticks)` or
/// more swaps in — the join runs first, the departure follows it
/// through the same choreography, `transition` asserted across the
/// whole handover so the valve-freeze wiring sees one transition.
///
/// **Staging authority:** `0` ([`StagingAuthority::Automatic`])
/// executes a warranted change the first scan it is eligible. `1`
/// ([`StagingAuthority::OperatorApproval`]) holds it on
/// `staging_pending` until a `Good` `true` on the bound `approve`
/// point grants it — one assertion grants one change, and a standing
/// `approve` must clear before the next grant; the authority requires
/// the bound port. `2` ([`StagingAuthority::FlagOnly`]) never commands:
/// the request flags on `staging_pending` as the operator's
/// recommendation while the outputs stay off.
///
/// **Run-hours:** each unit accumulates one tick per scan its `run_i`
/// reads `true` with `Good` quality — proven-running time, commanded
/// or not — feeding the equalize ranking deterministically.
///
/// **Quality rule — every input's non-`Good` reading is fail-safe:**
/// `avail_i` non-`Good` reads unavailable, `fault_i` non-`Good` reads
/// failed, `run_i` non-`Good` proves neither the join nor the stop and
/// accumulates nothing, a non-`Good` or non-finite `demand` holds the
/// last `Good` value, and a non-`Good` `approve` grants nothing.
/// Every output carries [`Quality::Good`]: the values are the group's
/// own computed decisions under these rules.
///
/// Declared I/O: `demand` (`In`, `Float`); `approve` (`In`, `Bool`,
/// declared only where bound); per unit `cmd_i` (`Out`, `Bool`),
/// `run_i` (`In`, `Bool`), `fault_i` (`In`, `Bool`), `avail_i` (`In`,
/// `Bool`), `capacity_i` (`Out`, `Float`), `vent_i` (`Out`, `Bool`);
/// `staged` (`Out`, `Int`), `none_available` (`Out`, `Bool`),
/// `all_faulted` (`Out`, `Bool`), `staging_pending` (`Out`, `Bool`),
/// `transition` (`Out`, `Bool`).
///
/// Parameters — required, the decision's assumption-marked declared
/// data: `staging_authority` (`Int`, a [`StagingAuthority`] code),
/// `stage_up`, `stage_down` (non-negative finite `Float`s),
/// `min_run_ticks`, `min_start_interval_ticks` (non-negative `Int`s),
/// per unit `unit_<i>_min_flow`/`unit_<i>_max_flow`/
/// `unit_<i>_max_current` (non-negative finite `Float`s, the declared
/// bounds ordered `min_flow <= max_flow` and `min_flow <=
/// max_current`), `vent_ticks` (`Int` ≥ 1), `rotation` (`Int`, a
/// [`BlowerRotation`] code). All tune through `set_parameter`; a
/// retune breaking a bound ordering or the codes is refused naming the
/// parameter.
#[derive(Debug)]
pub struct BlowerGroup {
    name: String,
    demand: PointId,
    approve: Option<PointId>,
    units: Vec<BlowerIo>,
    bounds: Vec<UnitBounds>,
    outputs: BlowerOutputs,
    config: BlowerGroupConfig,
    state: Vec<BlowerState>,
    /// The currently joined units in the order they joined — the
    /// no-rotation policy's LIFO departure order. 0-based indices.
    joined_order: Vec<usize>,
    /// The rotation handover's banked legs — `(joining, departing)`
    /// 0-based indices: while the join proves, the paired departure
    /// waits; the join completing releases it, the join aborting
    /// cancels it.
    rotation_pair: Option<(usize, usize)>,
    /// Whether the standing `approve` assertion has already granted a
    /// change — one assertion, one grant.
    approve_used: bool,
    /// The held last `Good` finite demand — a non-`Good` demand cannot
    /// move the group.
    last_demand: f64,
}

/// The inclusive `Int` bound the code parameters accept: the declared
/// [`StagingAuthority`] and [`BlowerRotation`] codes.
const CODE_RANGE: ParameterRange = ParameterRange {
    min: Value::Int(0),
    max: Value::Int(2),
};

impl BlowerGroup {
    /// The component-kind string the model-driven registry maps onto
    /// this type's constructor.
    pub const KIND: &'static str = "blower-group";

    /// Builds the component from explicit points, declared bounds, and
    /// the tuned configuration, or reports an inconsistent one as a
    /// [`ParameterError`].
    ///
    /// `units` are the managed blowers in declared order — `units[i]`
    /// is unit `i + 1` — and must not be empty. `bounds` carries one
    /// [`UnitBounds`] per unit in the same order. `approve` is the
    /// optional operator release point —
    /// [`StagingAuthority::OperatorApproval`] requires it bound.
    pub fn new(
        name: impl Into<String>,
        demand: PointId,
        approve: Option<PointId>,
        units: Vec<BlowerIo>,
        bounds: Vec<UnitBounds>,
        outputs: BlowerOutputs,
        config: BlowerGroupConfig,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        if units.is_empty() {
            return Err(params::invalid(
                &name,
                "units",
                "must declare at least one unit".to_string(),
            ));
        }
        if bounds.len() != units.len() {
            return Err(params::invalid(
                &name,
                "bounds",
                "must carry one entry per declared unit".to_string(),
            ));
        }
        let config = BlowerGroupConfig::checked(&name, config)?;
        for (index, bounds) in bounds.iter().enumerate() {
            UnitBounds::checked(&name, index + 1, *bounds)?;
        }
        if config.staging_authority == StagingAuthority::OperatorApproval && approve.is_none() {
            return Err(params::invalid(
                &name,
                "approve",
                "operator-approval staging requires a bound approve point".to_string(),
            ));
        }
        let count = units.len();
        Ok(Self {
            name,
            demand,
            approve,
            units,
            bounds,
            outputs,
            config,
            state: vec![BlowerState::default(); count],
            joined_order: Vec::new(),
            rotation_pair: None,
            approve_used: false,
            last_demand: 0.0,
        })
    }

    /// The group's tuned configuration — the reported parameter set.
    pub fn config(&self) -> BlowerGroupConfig {
        self.config
    }

    /// Builds the component from a plant-model parameter map, reading
    /// the parameters listed on the type's docs — the shared keys plus
    /// `unit_<i>_min_flow`/`unit_<i>_max_flow`/`unit_<i>_max_current`
    /// for each declared unit.
    pub fn from_parameters(
        name: impl Into<String>,
        demand: PointId,
        approve: Option<PointId>,
        units: Vec<BlowerIo>,
        outputs: BlowerOutputs,
        parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        let authority_code = params::required_u64(&name, parameters, "staging_authority")?;
        let staging_authority = StagingAuthority::decode(authority_code as i64).ok_or_else(|| {
            params::invalid(
                &name,
                "staging_authority",
                format!(
                    "expected 0 (automatic), 1 (operator approval), or 2 (flag-only), found {authority_code}"
                ),
            )
        })?;
        let stage_up = params::required_f64(&name, parameters, "stage_up")?;
        let stage_down = params::required_f64(&name, parameters, "stage_down")?;
        let min_run_ticks = params::required_u64(&name, parameters, "min_run_ticks")?;
        let min_start_interval_ticks =
            params::required_u64(&name, parameters, "min_start_interval_ticks")?;
        let bounds: Vec<UnitBounds> = (1..=units.len())
            .map(|index| {
                Ok(UnitBounds {
                    min_flow: params::required_f64(
                        &name,
                        parameters,
                        &format!("unit_{index}_min_flow"),
                    )?,
                    max_flow: params::required_f64(
                        &name,
                        parameters,
                        &format!("unit_{index}_max_flow"),
                    )?,
                    max_current: params::required_f64(
                        &name,
                        parameters,
                        &format!("unit_{index}_max_current"),
                    )?,
                })
            })
            .collect::<Result<_, ParameterError>>()?;
        let vent_ticks = params::required_u64(&name, parameters, "vent_ticks")?;
        let rotation_code = params::required_u64(&name, parameters, "rotation")?;
        let rotation = BlowerRotation::decode(rotation_code as i64).ok_or_else(|| {
            params::invalid(
                &name,
                "rotation",
                format!(
                    "expected 0 (none), 1 (equalize runtime), or 2 (fixed order), found {rotation_code}"
                ),
            )
        })?;
        let config = BlowerGroupConfig {
            staging_authority,
            stage_up,
            stage_down,
            min_run_ticks,
            min_start_interval_ticks,
            vent_ticks,
            rotation,
        };
        Self::new(name, demand, approve, units, bounds, outputs, config)
    }

    /// Retunes one declared parameter, returning a [`CommandError`]
    /// naming `parameter` on an unknown name, a wrong kind, or a value
    /// that would break an invariant — a refused value changes
    /// nothing.
    fn tune(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        let mut config = self.config;
        let mut bounds = self.bounds.clone();
        match parameter {
            "staging_authority" => {
                let code = params::tune_u64(&self.name, parameter, value)?;
                let authority = StagingAuthority::decode(code as i64).ok_or_else(|| {
                    params::invalid_parameter(
                        &self.name,
                        parameter,
                        "expected 0 (automatic), 1 (operator approval), or 2 (flag-only)",
                    )
                })?;
                if authority == StagingAuthority::OperatorApproval && self.approve.is_none() {
                    return Err(params::invalid_parameter(
                        &self.name,
                        parameter,
                        "operator-approval staging requires a bound approve point",
                    ));
                }
                config.staging_authority = authority;
            }
            "stage_up" | "stage_down" => {
                let tuned = params::tune_f64(&self.name, parameter, value)?;
                if !tuned.is_finite() || tuned < 0.0 {
                    return Err(params::invalid_parameter(
                        &self.name,
                        parameter,
                        "must be a non-negative finite value",
                    ));
                }
                match parameter {
                    "stage_up" => config.stage_up = tuned,
                    _ => config.stage_down = tuned,
                }
            }
            "min_run_ticks" => {
                config.min_run_ticks = params::tune_u64(&self.name, parameter, value)?;
            }
            "min_start_interval_ticks" => {
                config.min_start_interval_ticks = params::tune_u64(&self.name, parameter, value)?;
            }
            "vent_ticks" => {
                let tuned = params::tune_u64(&self.name, parameter, value)?;
                if tuned < 1 {
                    return Err(params::invalid_parameter(
                        &self.name,
                        parameter,
                        "must be at least 1",
                    ));
                }
                config.vent_ticks = tuned;
            }
            "rotation" => {
                let code = params::tune_u64(&self.name, parameter, value)?;
                config.rotation = BlowerRotation::decode(code as i64).ok_or_else(|| {
                    params::invalid_parameter(
                        &self.name,
                        parameter,
                        "expected 0 (none), 1 (equalize runtime), or 2 (fixed order)",
                    )
                })?;
            }
            _ => {
                let Some(indexed) = parameter.strip_prefix("unit_") else {
                    return Err(params::unknown_parameter(&self.name, parameter));
                };
                let (index, field) = indexed
                    .strip_suffix("_min_flow")
                    .map(|index| (index, 0usize))
                    .or_else(|| {
                        indexed
                            .strip_suffix("_max_flow")
                            .map(|index| (index, 1usize))
                    })
                    .or_else(|| {
                        indexed
                            .strip_suffix("_max_current")
                            .map(|index| (index, 2usize))
                    })
                    .ok_or_else(|| params::unknown_parameter(&self.name, parameter))?;
                let index: usize = index
                    .parse()
                    .ok()
                    .filter(|index| (1..=self.units.len()).contains(index))
                    .ok_or_else(|| params::unknown_parameter(&self.name, parameter))?;
                let tuned = params::tune_f64(&self.name, parameter, value)?;
                if !tuned.is_finite() || tuned < 0.0 {
                    return Err(params::invalid_parameter(
                        &self.name,
                        parameter,
                        "must be a non-negative finite value",
                    ));
                }
                let unit = &mut bounds[index - 1];
                match field {
                    0 => unit.min_flow = tuned,
                    1 => unit.max_flow = tuned,
                    _ => unit.max_current = tuned,
                }
                if unit.min_flow > unit.max_flow || unit.min_flow > unit.max_current {
                    return Err(params::invalid_parameter(
                        &self.name,
                        parameter,
                        "the declared bounds must keep min_flow <= max_flow and min_flow <= max_current",
                    ));
                }
            }
        }
        self.config = config;
        self.bounds = bounds;
        Ok(())
    }

    /// Whether a transition leg is in flight — a unit joining or
    /// departing, or a rotation's banked departure.
    fn in_flight(&self) -> bool {
        self.rotation_pair.is_some()
            || self
                .state
                .iter()
                .any(|unit| matches!(unit.phase, Phase::Joining | Phase::Departing))
    }

    /// Whether the unit may start: offline, available, and past its
    /// start interval.
    fn joinable(&self, index: usize, available: &[bool], tick: Tick) -> bool {
        let unit = &self.state[index];
        unit.phase == Phase::Offline
            && available[index]
            && (unit.stopped_at == 0
                || tick.0 >= unit.stopped_at + self.config.min_start_interval_ticks)
    }

    /// Whether the joined unit may depart: its minimum run time served.
    fn departable(&self, index: usize, tick: Tick) -> bool {
        let unit = &self.state[index];
        unit.phase == Phase::Joined && tick.0 >= unit.started_at + self.config.min_run_ticks
    }

    /// The eligible unit a join picks: the least run-hours under
    /// equalize-runtime, the lowest index under the other policies.
    fn join_pick(&self, available: &[bool], tick: Tick) -> Option<usize> {
        (0..self.units.len())
            .filter(|&index| self.joinable(index, available, tick))
            .min_by_key(|&index| {
                (
                    if self.config.rotation == BlowerRotation::EqualizeRuntime {
                        self.state[index].run_hours
                    } else {
                        0
                    },
                    index,
                )
            })
    }

    /// The joined units in the policy's departure preference order:
    /// most recently joined under no-rotation, highest index under
    /// fixed order, most run-hours under equalize-runtime.
    fn depart_order(&self) -> Vec<usize> {
        match self.config.rotation {
            BlowerRotation::NoRotation => self.joined_order.iter().rev().copied().collect(),
            BlowerRotation::FixedOrder => {
                let mut order = self.joined_order.clone();
                order.sort_by_key(|&index| std::cmp::Reverse(index));
                order
            }
            BlowerRotation::EqualizeRuntime => {
                let mut order = self.joined_order.clone();
                order.sort_by_key(|&index| (std::cmp::Reverse(self.state[index].run_hours), index));
                order
            }
        }
    }

    /// Opens the unit's vent and asserts its run request — the
    /// offline-start leg of the join choreography.
    fn initiate_join(&mut self, index: usize, tick: Tick) {
        let unit = &mut self.state[index];
        unit.phase = Phase::Joining;
        unit.phase_ticks = 0;
        unit.started_at = tick.0;
    }

    /// Drops the unit's run request behind its open vent — the
    /// departure leg of the choreography.
    fn initiate_depart(&mut self, index: usize) {
        let unit = &mut self.state[index];
        unit.phase = Phase::Departing;
        unit.phase_ticks = 0;
        self.joined_order.retain(|&joined| joined != index);
    }
}

impl Component for BlowerGroup {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        let mut requirements = Vec::with_capacity(self.units.len() * 6 + 7);
        requirements.push(IoRequirement::input::<f64>("demand", self.demand));
        if let Some(approve) = self.approve {
            requirements.push(IoRequirement::input::<bool>("approve", approve));
        }
        for (index, unit) in self.units.iter().enumerate() {
            let index = index + 1;
            requirements.push(IoRequirement::output::<bool>(
                format!("cmd_{index}"),
                unit.cmd,
            ));
            requirements.push(IoRequirement::input::<bool>(
                format!("run_{index}"),
                unit.run,
            ));
            requirements.push(IoRequirement::input::<bool>(
                format!("fault_{index}"),
                unit.fault,
            ));
            requirements.push(IoRequirement::input::<bool>(
                format!("avail_{index}"),
                unit.avail,
            ));
            requirements.push(IoRequirement::output::<f64>(
                format!("capacity_{index}"),
                unit.capacity,
            ));
            requirements.push(IoRequirement::output::<bool>(
                format!("vent_{index}"),
                unit.vent,
            ));
        }
        requirements.push(IoRequirement::output::<i64>("staged", self.outputs.staged));
        requirements.push(IoRequirement::output::<bool>(
            "none_available",
            self.outputs.none_available,
        ));
        requirements.push(IoRequirement::output::<bool>(
            "all_faulted",
            self.outputs.all_faulted,
        ));
        requirements.push(IoRequirement::output::<bool>(
            "staging_pending",
            self.outputs.staging_pending,
        ));
        requirements.push(IoRequirement::output::<bool>(
            "transition",
            self.outputs.transition,
        ));
        requirements
    }

    fn step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError> {
        let count = self.units.len();

        // The stage demand: a Good finite value refreshes the held
        // demand (clamped non-negative); anything else holds the last
        // Good one.
        let demand = io.read_typed::<f64>(self.demand)?;
        if demand.quality.is_good() && demand.value.is_finite() {
            self.last_demand = demand.value.max(0.0);
        }
        let demand_effective = self.last_demand;

        // The operator release: only a Good true grants, and the grant
        // is consumed by the change it releases — a Good false re-arms
        // the point for the next approval.
        let approve_asserted = match self.approve {
            Some(approve) => {
                let sample = io.read_typed::<bool>(approve)?;
                if sample.quality.is_good() && !sample.value {
                    self.approve_used = false;
                }
                sample.quality.is_good() && sample.value
            }
            None => false,
        };

        // Fail-safe per-unit readings: an untrusted input cannot vouch
        // for its condition — untrusted availability is unavailable, an
        // untrusted fault flag is failed, and untrusted feedback proves
        // neither running nor stopped and accumulates no run-hours.
        let mut running = vec![false; count];
        let mut proven_stopped = vec![false; count];
        let mut faulted = vec![false; count];
        let mut available = vec![false; count];
        for (index, unit) in self.units.iter().enumerate() {
            let run = io.read_typed::<bool>(unit.run)?;
            let fault = io.read_typed::<bool>(unit.fault)?;
            let avail = io.read_typed::<bool>(unit.avail)?;
            running[index] = run.quality.is_good() && run.value;
            proven_stopped[index] = run.quality.is_good() && !run.value;
            faulted[index] = fault.value || !fault.quality.is_good();
            available[index] = avail.quality.is_good() && avail.value && !faulted[index];
            if running[index] {
                self.state[index].run_hours = self.state[index].run_hours.saturating_add(1);
            }
        }

        // The choreography machines advance first: a joining unit
        // proves its run or times out back to offline — faulting,
        // losing availability, or the demand falling to nothing aborts
        // the join; a joined unit faulting or losing availability
        // departs at this scan; a departing unit resolves offline on a
        // proven stop or at the window's end.
        for index in 0..count {
            match self.state[index].phase {
                Phase::Joining => {
                    if faulted[index] || !available[index] || demand_effective <= 0.0 {
                        self.state[index].phase = Phase::Offline;
                        self.state[index].phase_ticks = 0;
                        self.state[index].stopped_at = tick.0;
                    } else if running[index] {
                        self.state[index].phase = Phase::Joined;
                        self.state[index].phase_ticks = 0;
                        self.joined_order.push(index);
                    } else {
                        self.state[index].phase_ticks += 1;
                        if self.state[index].phase_ticks >= self.config.vent_ticks {
                            self.state[index].phase = Phase::Offline;
                            self.state[index].phase_ticks = 0;
                            self.state[index].stopped_at = tick.0;
                        }
                    }
                }
                Phase::Joined => {
                    if faulted[index] || !available[index] {
                        self.initiate_depart(index);
                    }
                }
                Phase::Departing => {
                    if proven_stopped[index] {
                        self.state[index].phase = Phase::Offline;
                        self.state[index].phase_ticks = 0;
                        self.state[index].stopped_at = tick.0;
                    } else {
                        self.state[index].phase_ticks += 1;
                        if self.state[index].phase_ticks >= self.config.vent_ticks {
                            self.state[index].phase = Phase::Offline;
                            self.state[index].stopped_at = tick.0;
                        }
                    }
                }
                Phase::Offline => {}
            }
        }

        // A rotation's second leg: the unit banked for departure goes
        // the scan its join has completed — never ahead of it, and not
        // at all when the join aborts or the banked unit has already
        // left `Joined` on its own (a forced departure).
        if let Some((join, depart)) = self.rotation_pair {
            match self.state[join].phase {
                Phase::Joining => {}
                Phase::Joined => {
                    self.rotation_pair = None;
                    if self.state[depart].phase == Phase::Joined {
                        self.initiate_depart(depart);
                    }
                }
                _ => self.rotation_pair = None,
            }
        }

        // The committed capacity is the joining plus joined units'
        // effective ceilings — a unit proves its start against the
        // capacity it will deliver.
        let committed_capacity: f64 = (0..count)
            .filter(|&index| matches!(self.state[index].phase, Phase::Joining | Phase::Joined))
            .map(|index| self.bounds[index].effective_max())
            .sum();
        let depart_order = self.depart_order();
        let up_warranted = demand_effective > self.config.stage_up * committed_capacity
            && (0..count).any(|index| self.state[index].phase == Phase::Offline);
        let down_warranted = !depart_order.is_empty()
            && (demand_effective <= 0.0
                || demand_effective
                    < self.config.stage_down
                        * (committed_capacity - self.bounds[depart_order[0]].effective_max()));
        // The equalize-runtime swap: while demand stands met, the
        // least-run available standby rotates in for the most-run
        // joined unit once the spread reaches the declared margin.
        let rotation_margin = self.config.min_run_ticks.max(1);
        let rotate_warranted = self.config.rotation == BlowerRotation::EqualizeRuntime
            && !up_warranted
            && !down_warranted
            && match (
                (0..count)
                    .filter(|&index| self.state[index].phase == Phase::Offline && available[index])
                    .map(|index| self.state[index].run_hours)
                    .min(),
                depart_order
                    .iter()
                    .map(|&index| self.state[index].run_hours)
                    .max(),
            ) {
                (Some(least), Some(most)) => most >= least + rotation_margin,
                _ => false,
            };

        // The request resolves to an actionable change only when its
        // unit is eligible — a warranted change whose candidate is held
        // out waits, flagged or not by the authority.
        let request = if up_warranted {
            self.join_pick(&available, tick).map(Request::StageUp)
        } else if down_warranted {
            depart_order
                .iter()
                .copied()
                .find(|&index| self.departable(index, tick))
                .map(Request::StageDown)
        } else if rotate_warranted {
            match (
                self.join_pick(&available, tick),
                depart_order
                    .iter()
                    .copied()
                    .find(|&index| self.departable(index, tick)),
            ) {
                (Some(join), Some(depart)) => Some(Request::Rotate(join, depart)),
                _ => None,
            }
        } else {
            None
        };
        let warranted = up_warranted || down_warranted || rotate_warranted;

        // The authority decides whether an eligible request executes:
        // automatic stages at once, operator-approval waits on the
        // `approve` grant, flag-only never commands.
        let mut executed = false;
        if let Some(request) = request {
            let granted = match self.config.staging_authority {
                StagingAuthority::Automatic => true,
                StagingAuthority::OperatorApproval => approve_asserted && !self.approve_used,
                StagingAuthority::FlagOnly => false,
            };
            if granted && !self.in_flight() {
                match request {
                    Request::StageUp(index) => self.initiate_join(index, tick),
                    Request::StageDown(index) => self.initiate_depart(index),
                    Request::Rotate(join, depart) => {
                        self.initiate_join(join, tick);
                        self.rotation_pair = Some((join, depart));
                    }
                }
                if self.config.staging_authority == StagingAuthority::OperatorApproval {
                    self.approve_used = true;
                }
                executed = true;
            }
        }

        // The reported pending set: a warranted change standing
        // unexecuted under a non-automatic authority — held for the
        // operator under approval, the recommendation under flag-only.
        let staging_pending =
            warranted && !executed && self.config.staging_authority != StagingAuthority::Automatic;
        let transition = self.in_flight();

        // Outputs reflect the post-evaluation phases: the same scan
        // that initiates a join already shows the open vent and the
        // asserted command.
        let joined_count = self
            .state
            .iter()
            .filter(|unit| unit.phase == Phase::Joined)
            .count();
        let share = if joined_count > 0 {
            demand_effective / joined_count as f64
        } else {
            0.0
        };
        for (index, unit) in self.units.iter().enumerate() {
            let phase = self.state[index].phase;
            let commanded = matches!(phase, Phase::Joining | Phase::Joined);
            let vented = matches!(phase, Phase::Joining | Phase::Departing);
            let capacity = match phase {
                Phase::Joined => share.clamp(
                    self.bounds[index].min_flow,
                    self.bounds[index].effective_max(),
                ),
                Phase::Joining => self.bounds[index].min_flow,
                Phase::Offline | Phase::Departing => 0.0,
            };
            io.write_sample(
                unit.cmd,
                Sample::new(Value::Bool(commanded), Quality::Good, tick),
            )?;
            io.write_sample(
                unit.capacity,
                Sample::new(Value::Float(capacity), Quality::Good, tick),
            )?;
            io.write_sample(
                unit.vent,
                Sample::new(Value::Bool(vented), Quality::Good, tick),
            )?;
        }
        io.write_sample(
            self.outputs.staged,
            Sample::new(
                Value::Int(
                    self.state
                        .iter()
                        .filter(|unit| matches!(unit.phase, Phase::Joining | Phase::Joined))
                        .count() as i64,
                ),
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
        io.write_sample(
            self.outputs.staging_pending,
            Sample::new(Value::Bool(staging_pending), Quality::Good, tick),
        )?;
        io.write_sample(
            self.outputs.transition,
            Sample::new(Value::Bool(transition), Quality::Good, tick),
        )?;
        Ok(())
    }

    /// Describes the group: `demand` is the capacity demand the group
    /// is driven toward and `approve` the operator's release; each
    /// `cmd_i`/`capacity_i`/`vent_i` a driven command and each
    /// `run_i`/`fault_i`/`avail_i` the measured equipment state it acts
    /// on; `staged`/`none_available`/`all_faulted`/`staging_pending`/
    /// `transition` the reported conditions; the declared parameters
    /// with their ranges.
    fn describe(&self) -> ComponentDescriptor {
        let mut roles: Vec<(String, PortRole)> = vec![("demand".to_string(), PortRole::Setpoint)];
        if self.approve.is_some() {
            roles.push(("approve".to_string(), PortRole::Setpoint));
        }
        for index in 1..=self.units.len() {
            roles.push((format!("cmd_{index}"), PortRole::Output));
            roles.push((format!("run_{index}"), PortRole::ProcessValue));
            roles.push((format!("fault_{index}"), PortRole::ProcessValue));
            roles.push((format!("avail_{index}"), PortRole::ProcessValue));
            roles.push((format!("capacity_{index}"), PortRole::Output));
            roles.push((format!("vent_{index}"), PortRole::Output));
        }
        roles.extend([
            ("staged".to_string(), PortRole::Status),
            ("none_available".to_string(), PortRole::Status),
            ("all_faulted".to_string(), PortRole::Status),
            ("staging_pending".to_string(), PortRole::Status),
            ("transition".to_string(), PortRole::Status),
        ]);
        let mut parameters = vec![
            describe::parameter("staging_authority", ValueKind::Int, Some(CODE_RANGE)),
            describe::parameter(
                "stage_up",
                ValueKind::Float,
                Some(describe::NONNEGATIVE_F64),
            ),
            describe::parameter(
                "stage_down",
                ValueKind::Float,
                Some(describe::NONNEGATIVE_F64),
            ),
            describe::parameter(
                "min_run_ticks",
                ValueKind::Int,
                Some(describe::NONNEGATIVE_INT),
            ),
            describe::parameter(
                "min_start_interval_ticks",
                ValueKind::Int,
                Some(describe::NONNEGATIVE_INT),
            ),
        ];
        for index in 1..=self.units.len() {
            for suffix in ["min_flow", "max_flow", "max_current"] {
                parameters.push(describe::parameter(
                    &format!("unit_{index}_{suffix}"),
                    ValueKind::Float,
                    Some(describe::NONNEGATIVE_F64),
                ));
            }
        }
        parameters.extend([
            describe::parameter("vent_ticks", ValueKind::Int, Some(describe::POSITIVE_INT)),
            describe::parameter("rotation", ValueKind::Int, Some(CODE_RANGE)),
        ]);
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &roles,
            parameters,
        )
    }

    /// Tunes the declared parameters at the scan boundary — the
    /// recorded mechanism for operator-adjusted thresholds, timers,
    /// per-unit bounds, authority, and rotation. A refused value names
    /// the parameter and changes nothing.
    fn apply_parameter(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        self.tune(parameter, value)
    }

    /// Reports the tuned parameters — the same fields
    /// [`capture_state`](Self::capture_state) checkpoints, so the
    /// faceplate and a tracking standby read one vocabulary.
    fn report_parameters(&self) -> StateMap {
        let mut parameters = StateMap::new();
        parameters.insert(
            "staging_authority",
            Value::Int(self.config.staging_authority.code()),
        );
        parameters.insert("stage_up", Value::Float(self.config.stage_up));
        parameters.insert("stage_down", Value::Float(self.config.stage_down));
        parameters.insert(
            "min_run_ticks",
            Value::Int(self.config.min_run_ticks as i64),
        );
        parameters.insert(
            "min_start_interval_ticks",
            Value::Int(self.config.min_start_interval_ticks as i64),
        );
        for (index, bounds) in self.bounds.iter().enumerate() {
            let index = index + 1;
            parameters.insert(
                format!("unit_{index}_min_flow"),
                Value::Float(bounds.min_flow),
            );
            parameters.insert(
                format!("unit_{index}_max_flow"),
                Value::Float(bounds.max_flow),
            );
            parameters.insert(
                format!("unit_{index}_max_current"),
                Value::Float(bounds.max_current),
            );
        }
        parameters.insert("vent_ticks", Value::Int(self.config.vent_ticks as i64));
        parameters.insert("rotation", Value::Int(self.config.rotation.code()));
        parameters
    }

    /// Captures the held demand, the approval bookkeeping, the
    /// rotation pair, the per-unit choreography phase and prove
    /// timers, the run-hours accumulators, the start/stop interval
    /// references, the join order, and the tuned parameters — the
    /// whole of the decision-20 run state, so a checkpointed standby
    /// resumes mid-join or mid-rotation without restarting a
    /// half-joined unit.
    fn capture_state(&self) -> StateMap {
        let mut state = self.report_parameters();
        state.insert("last_demand", Value::Float(self.last_demand));
        state.insert("approve_used", Value::Bool(self.approve_used));
        state.insert(
            "rotation_join",
            Value::Int(self.rotation_pair.map_or(0, |(join, _)| join as i64 + 1)),
        );
        state.insert(
            "rotation_depart",
            Value::Int(
                self.rotation_pair
                    .map_or(0, |(_, depart)| depart as i64 + 1),
            ),
        );
        for (index, unit) in self.state.iter().enumerate() {
            let index = index + 1;
            state.insert(format!("phase_{index}"), Value::Int(unit.phase.code()));
            state.insert(
                format!("phase_ticks_{index}"),
                Value::Int(unit.phase_ticks as i64),
            );
            state.insert(
                format!("run_hours_{index}"),
                Value::Int(unit.run_hours as i64),
            );
            state.insert(
                format!("started_at_{index}"),
                Value::Int(unit.started_at as i64),
            );
            state.insert(
                format!("stopped_at_{index}"),
                Value::Int(unit.stopped_at as i64),
            );
            state.insert(
                format!("join_rank_{index}"),
                Value::Int(
                    self.joined_order
                        .iter()
                        .position(|&joined| joined == index - 1)
                        .map_or(0, |rank| rank as i64 + 1),
                ),
            );
        }
        state
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        let count = self.units.len() as i64;
        let mut known: Vec<String> = [
            "staging_authority",
            "stage_up",
            "stage_down",
            "min_run_ticks",
            "min_start_interval_ticks",
            "vent_ticks",
            "rotation",
            "last_demand",
            "approve_used",
            "rotation_join",
            "rotation_depart",
        ]
        .into_iter()
        .map(str::to_string)
        .collect();
        for index in 1..=count {
            known.push(format!("unit_{index}_min_flow"));
            known.push(format!("unit_{index}_max_flow"));
            known.push(format!("unit_{index}_max_current"));
            for field in [
                "phase",
                "phase_ticks",
                "run_hours",
                "started_at",
                "stopped_at",
                "join_rank",
            ] {
                known.push(format!("{field}_{index}"));
            }
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
        let non_negative = |field: &str| -> Result<u64, StateError> {
            let value = state.require_i64(&self.name, field)?;
            if value < 0 {
                return Err(invalid(field, Value::Int(value)));
            }
            Ok(value as u64)
        };
        let non_negative_f64 = |field: &str| -> Result<f64, StateError> {
            let value = state.require_f64(&self.name, field)?;
            if !value.is_finite() || value < 0.0 {
                return Err(invalid(field, Value::Float(value)));
            }
            Ok(value)
        };

        let authority_code = state.require_i64(&self.name, "staging_authority")?;
        let staging_authority = StagingAuthority::decode(authority_code)
            .ok_or_else(|| invalid("staging_authority", Value::Int(authority_code)))?;
        let stage_up = non_negative_f64("stage_up")?;
        let stage_down = non_negative_f64("stage_down")?;
        let min_run_ticks = non_negative("min_run_ticks")?;
        let min_start_interval_ticks = non_negative("min_start_interval_ticks")?;
        let vent_ticks = non_negative("vent_ticks")?;
        if vent_ticks < 1 {
            return Err(invalid("vent_ticks", Value::Int(vent_ticks as i64)));
        }
        let rotation_code = state.require_i64(&self.name, "rotation")?;
        let rotation = BlowerRotation::decode(rotation_code)
            .ok_or_else(|| invalid("rotation", Value::Int(rotation_code)))?;

        let mut bounds = Vec::with_capacity(count as usize);
        for index in 1..=count {
            let min_flow = non_negative_f64(&format!("unit_{index}_min_flow"))?;
            let max_flow = non_negative_f64(&format!("unit_{index}_max_flow"))?;
            let max_current = non_negative_f64(&format!("unit_{index}_max_current"))?;
            if min_flow > max_flow {
                return Err(invalid(
                    &format!("unit_{index}_max_flow"),
                    Value::Float(max_flow),
                ));
            }
            if min_flow > max_current {
                return Err(invalid(
                    &format!("unit_{index}_max_current"),
                    Value::Float(max_current),
                ));
            }
            bounds.push(UnitBounds {
                min_flow,
                max_flow,
                max_current,
            });
        }

        let last_demand = state.require_f64(&self.name, "last_demand")?;
        if !last_demand.is_finite() || last_demand < 0.0 {
            return Err(invalid("last_demand", Value::Float(last_demand)));
        }
        let approve_used = state.require_bool(&self.name, "approve_used")?;
        let rotation_join = state.require_i64(&self.name, "rotation_join")?;
        if !(0..=count).contains(&rotation_join) {
            return Err(invalid("rotation_join", Value::Int(rotation_join)));
        }
        let rotation_depart = state.require_i64(&self.name, "rotation_depart")?;
        if !(0..=count).contains(&rotation_depart) {
            return Err(invalid("rotation_depart", Value::Int(rotation_depart)));
        }
        // A recorded pair is exactly one in-flight join with its banked
        // departure — one leg without the other is not a map this
        // component captures.
        if (rotation_join > 0) != (rotation_depart > 0) {
            return Err(invalid(
                if rotation_join > 0 {
                    "rotation_depart"
                } else {
                    "rotation_join"
                },
                Value::Int(rotation_join.max(rotation_depart)),
            ));
        }

        let mut units = Vec::with_capacity(count as usize);
        let mut joined_order = Vec::new();
        for index in 1..=count {
            let phase_code = state.require_i64(&self.name, &format!("phase_{index}"))?;
            let phase = Phase::decode(phase_code)
                .ok_or_else(|| invalid(&format!("phase_{index}"), Value::Int(phase_code)))?;
            let phase_ticks = non_negative(&format!("phase_ticks_{index}"))?;
            // A joining or departing unit's prove window is always part
            // elapsed in a captured map — never whole or over — and a
            // settled phase banks no count.
            match phase {
                Phase::Joining | Phase::Departing if phase_ticks >= vent_ticks => {
                    return Err(invalid(
                        &format!("phase_ticks_{index}"),
                        Value::Int(phase_ticks as i64),
                    ));
                }
                Phase::Offline | Phase::Joined if phase_ticks != 0 => {
                    return Err(invalid(
                        &format!("phase_ticks_{index}"),
                        Value::Int(phase_ticks as i64),
                    ));
                }
                _ => {}
            }
            let run_hours = non_negative(&format!("run_hours_{index}"))?;
            let started_at = non_negative(&format!("started_at_{index}"))?;
            if phase != Phase::Offline && started_at == 0 {
                return Err(invalid(&format!("started_at_{index}"), Value::Int(0)));
            }
            let stopped_at = non_negative(&format!("stopped_at_{index}"))?;
            let join_rank = state.require_i64(&self.name, &format!("join_rank_{index}"))?;
            if !(0..=count).contains(&join_rank) || (join_rank > 0) != (phase == Phase::Joined) {
                return Err(invalid(
                    &format!("join_rank_{index}"),
                    Value::Int(join_rank),
                ));
            }
            if join_rank > 0 {
                joined_order.push((join_rank, index as usize - 1));
            }
            units.push(BlowerState {
                phase,
                phase_ticks,
                run_hours,
                started_at,
                stopped_at,
            });
        }
        // The recorded ranks must be the contiguous `1..=k` join order —
        // a duplicate or a gap is not a map this component captures.
        joined_order.sort_by_key(|&(rank, _)| rank);
        let joined_order: Vec<usize> = joined_order
            .iter()
            .enumerate()
            .map(|(position, &(rank, index))| {
                if rank == position as i64 + 1 {
                    Ok(index)
                } else {
                    Err(invalid("join_rank", Value::Int(rank)))
                }
            })
            .collect::<Result<_, _>>()?;
        if rotation_join > 0 {
            // The pair persists only while the join is still proving —
            // the join completing releases the departure the same scan
            // — and the banked unit is still joined.
            if units[rotation_join as usize - 1].phase != Phase::Joining {
                return Err(invalid("rotation_join", Value::Int(rotation_join)));
            }
            if units[rotation_depart as usize - 1].phase != Phase::Joined {
                return Err(invalid("rotation_depart", Value::Int(rotation_depart)));
            }
        }
        if staging_authority == StagingAuthority::OperatorApproval && self.approve.is_none() {
            return Err(invalid("staging_authority", Value::Int(authority_code)));
        }

        self.config = BlowerGroupConfig {
            staging_authority,
            stage_up,
            stage_down,
            min_run_ticks,
            min_start_interval_ticks,
            vent_ticks,
            rotation,
        };
        self.bounds = bounds;
        self.last_demand = last_demand;
        self.approve_used = approve_used;
        self.rotation_pair = if rotation_join == 0 {
            None
        } else {
            Some((rotation_join as usize - 1, rotation_depart as usize - 1))
        };
        self.state = units;
        self.joined_order = joined_order;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::TestIo;
    use dcs_core::{Direction, QualityReason};

    const DEMAND: PointId = PointId(10);
    const APPROVE: PointId = PointId(11);
    const STAGED: PointId = PointId(20);
    const NONE_AVAIL: PointId = PointId(21);
    const ALL_FAULTED: PointId = PointId(22);
    const PENDING: PointId = PointId(23);
    const TRANSITION: PointId = PointId(24);

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
    const fn capacity(index: usize) -> PointId {
        PointId(500 + index as u64)
    }
    const fn vent(index: usize) -> PointId {
        PointId(600 + index as u64)
    }

    const OUTPUTS: BlowerOutputs = BlowerOutputs {
        staged: STAGED,
        none_available: NONE_AVAIL,
        all_faulted: ALL_FAULTED,
        staging_pending: PENDING,
        transition: TRANSITION,
    };

    fn blowers(count: usize) -> Vec<BlowerIo> {
        (1..=count)
            .map(|index| BlowerIo {
                cmd: cmd(index),
                run: run(index),
                fault: fault(index),
                avail: avail(index),
                capacity: capacity(index),
                vent: vent(index),
            })
            .collect()
    }

    /// Uniform declared bounds: floor 20, the current bound capping the
    /// 100 flow ceiling at 90.
    fn bounds(count: usize) -> Vec<UnitBounds> {
        vec![
            UnitBounds {
                min_flow: 20.0,
                max_flow: 100.0,
                max_current: 90.0,
            };
            count
        ]
    }

    fn config() -> BlowerGroupConfig {
        BlowerGroupConfig {
            staging_authority: StagingAuthority::Automatic,
            stage_up: 0.9,
            stage_down: 0.8,
            min_run_ticks: 0,
            min_start_interval_ticks: 0,
            vent_ticks: 2,
            rotation: BlowerRotation::NoRotation,
        }
    }

    fn component(config: BlowerGroupConfig) -> BlowerGroup {
        component_of(3, config, None)
    }

    fn component_of(
        count: usize,
        config: BlowerGroupConfig,
        approve: Option<PointId>,
    ) -> BlowerGroup {
        BlowerGroup::new(
            "bg",
            DEMAND,
            approve,
            blowers(count),
            bounds(count),
            OUTPUTS,
            config,
        )
        .unwrap()
    }

    fn io(count: usize, approve: bool) -> TestIo {
        let mut points = vec![
            (
                DEMAND,
                Direction::In,
                Sample::good(Value::Float(0.0), Tick::ZERO),
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
            (
                PENDING,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
            (
                TRANSITION,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
        ];
        if approve {
            points.push((
                APPROVE,
                Direction::In,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ));
        }
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
                (
                    capacity(index),
                    Direction::Out,
                    Sample::good(Value::Float(0.0), Tick::ZERO),
                ),
                (
                    vent(index),
                    Direction::Out,
                    Sample::good(Value::Bool(false), Tick::ZERO),
                ),
            ]);
        }
        TestIo::new(&points)
    }

    fn feed(io: &TestIo, point: PointId, value: Value, tick: u64) {
        io.feed(point, Sample::good(value, Tick(tick)));
    }

    fn feed_demand(io: &TestIo, value: f64, tick: u64) {
        feed(io, DEMAND, Value::Float(value), tick);
    }

    fn feed_run(io: &TestIo, index: usize, value: bool, tick: u64) {
        feed(io, run(index), Value::Bool(value), tick);
    }

    fn feed_fault(io: &TestIo, index: usize, value: bool, tick: u64) {
        feed(io, fault(index), Value::Bool(value), tick);
    }

    fn feed_avail(io: &TestIo, index: usize, value: bool, tick: u64) {
        feed(io, avail(index), Value::Bool(value), tick);
    }

    fn feed_approve(io: &TestIo, value: bool, tick: u64) {
        feed(io, APPROVE, Value::Bool(value), tick);
    }

    fn out_bool(io: &TestIo, point: PointId) -> bool {
        match io.written(point).unwrap().value {
            Value::Bool(value) => value,
            value => panic!("expected Bool, found {value:?}"),
        }
    }

    fn out_int(io: &TestIo, point: PointId) -> i64 {
        match io.written(point).unwrap().value {
            Value::Int(value) => value,
            value => panic!("expected Int, found {value:?}"),
        }
    }

    fn out_f64(io: &TestIo, point: PointId) -> f64 {
        match io.written(point).unwrap().value {
            Value::Float(value) => value,
            value => panic!("expected Float, found {value:?}"),
        }
    }

    /// Steps the group with `demand` fed and the units' scripted inputs
    /// already in place.
    fn scan(group: &mut BlowerGroup, io: &TestIo, demand: f64, tick: u64) {
        feed_demand(io, demand, tick);
        group.step(io, Tick(tick)).unwrap();
    }

    #[test]
    fn join_choreography_stages_up_and_splits_the_demand() {
        let mut group = component(config());
        let io = io(3, false);
        scan(&mut group, &io, 0.0, 1);
        assert_eq!(out_int(&io, STAGED), 0);
        assert!(!out_bool(&io, cmd(1)));
        assert!(!out_bool(&io, TRANSITION));
        assert!(!out_bool(&io, PENDING));

        // Demand 100 against zero committed capacity stages the first
        // unit: the offline start — vent open and command asserted —
        // with the capacity demand at the declared minimum.
        scan(&mut group, &io, 100.0, 2);
        assert!(out_bool(&io, cmd(1)));
        assert!(out_bool(&io, vent(1)));
        assert_eq!(out_f64(&io, capacity(1)), 20.0);
        assert_eq!(out_int(&io, STAGED), 1);
        assert!(out_bool(&io, TRANSITION));

        // The run feedback proves inside the vent window: the vent
        // closes, the unit joins the header, and the still-unmet demand
        // immediately starts the next unit — one elective transition
        // per scan.
        feed_run(&io, 1, true, 3);
        scan(&mut group, &io, 100.0, 3);
        assert!(!out_bool(&io, vent(1)));
        assert_eq!(out_f64(&io, capacity(1)), 90.0);
        assert!(out_bool(&io, cmd(2)));
        assert!(out_bool(&io, vent(2)));
        assert!(out_bool(&io, TRANSITION));

        // Unit 2 proves and joins: 100 over the 180 committed is met
        // and shares equally.
        feed_run(&io, 2, true, 4);
        scan(&mut group, &io, 100.0, 4);
        assert!(!out_bool(&io, vent(2)));
        assert_eq!(out_f64(&io, capacity(1)), 50.0);
        assert_eq!(out_f64(&io, capacity(2)), 50.0);
        assert_eq!(out_int(&io, STAGED), 2);
        assert!(!out_bool(&io, TRANSITION));
    }

    #[test]
    fn stage_up_and_down_hold_at_the_declared_thresholds() {
        let mut group = component(config());
        let io = io(3, false);
        // Join unit 1 and leave it alone: demand at exactly
        // stage_up × committed is not an exceedance.
        scan(&mut group, &io, 81.0, 1);
        feed_run(&io, 1, true, 2);
        scan(&mut group, &io, 81.0, 2);
        assert_eq!(out_int(&io, STAGED), 1);
        scan(&mut group, &io, 81.0, 3);
        assert_eq!(out_int(&io, STAGED), 1);
        // The first tick above the threshold stages the next unit.
        feed_run(&io, 2, true, 4);
        scan(&mut group, &io, 81.1, 4);
        assert_eq!(out_int(&io, STAGED), 2);
        assert!(out_bool(&io, vent(2)));

        // With both joined (committed 180), dropping the most recent
        // leaves 90: demand at exactly stage_down × 90 does not
        // de-stage, the first tick below it does.
        scan(&mut group, &io, 72.0, 5);
        assert_eq!(out_int(&io, STAGED), 2);
        feed_run(&io, 2, false, 6);
        scan(&mut group, &io, 71.9, 6);
        assert!(!out_bool(&io, cmd(2)));
        assert!(out_bool(&io, vent(2)));
        assert!(out_bool(&io, TRANSITION));
        scan(&mut group, &io, 71.9, 7);
        assert_eq!(out_int(&io, STAGED), 1);
        assert!(!out_bool(&io, vent(2)));
        assert_eq!(out_f64(&io, capacity(2)), 0.0);
    }

    #[test]
    fn capacity_split_clamps_to_the_per_unit_bounds() {
        // Asymmetric declared bounds: unit 1's current bound caps it
        // at 60 against the others' 90.
        let mut bounds = bounds(3);
        bounds[0].max_current = 60.0;
        let mut group =
            BlowerGroup::new("bg", DEMAND, None, blowers(3), bounds, OUTPUTS, config()).unwrap();
        let io = io(3, false);
        // Join all three at 240 — past the 216 stage-up of the 240
        // committed ceiling — then settle.
        for (index, tick) in [(1usize, 1u64), (2, 2), (3, 3)] {
            scan(&mut group, &io, 240.0, tick);
            feed_run(&io, index, true, tick + 1);
        }
        scan(&mut group, &io, 240.0, 4);
        assert_eq!(out_int(&io, STAGED), 3);
        // The equal share 80 clamps unit 1 to its 60 current bound.
        assert_eq!(out_f64(&io, capacity(1)), 60.0);
        assert_eq!(out_f64(&io, capacity(2)), 80.0);
        assert_eq!(out_f64(&io, capacity(3)), 80.0);
        // Demand 60 de-stages unit 3 — dropping it leaves 150
        // committed against stage_down × 150 = 120 — and the equal
        // share lands inside both bounds.
        feed_run(&io, 3, false, 6);
        scan(&mut group, &io, 60.0, 5);
        assert!(out_bool(&io, vent(3)));
        scan(&mut group, &io, 60.0, 6);
        assert_eq!(out_f64(&io, capacity(1)), 30.0);
        assert_eq!(out_f64(&io, capacity(2)), 30.0);
        // Demand 10 de-stages unit 2; the lone unit's share falls
        // below its floor and clamps up to min_flow.
        feed_run(&io, 2, false, 8);
        scan(&mut group, &io, 10.0, 7);
        assert!(out_bool(&io, vent(2)));
        scan(&mut group, &io, 10.0, 8);
        assert_eq!(out_int(&io, STAGED), 1);
        assert_eq!(out_f64(&io, capacity(1)), 20.0);
    }

    #[test]
    fn min_run_ticks_holds_a_warranted_departure() {
        let mut group = component(BlowerGroupConfig {
            min_run_ticks: 3,
            ..config()
        });
        let io = io(3, false);
        // Units 1 and 2 join at ticks 1 and 2.
        scan(&mut group, &io, 150.0, 1);
        feed_run(&io, 1, true, 2);
        scan(&mut group, &io, 150.0, 2);
        feed_run(&io, 2, true, 3);
        // The demand collapses the same scan unit 2 proves: the
        // departure is warranted but both units sit inside their
        // three-tick minimum run — the request stands, nothing moves.
        scan(&mut group, &io, 20.0, 3);
        assert_eq!(out_int(&io, STAGED), 2);
        assert!(out_bool(&io, cmd(1)));
        assert!(out_bool(&io, cmd(2)));
        assert!(!out_bool(&io, TRANSITION));
        // Tick 4 clears unit 1's timer — unit 2's still holds — and
        // the standing request departs the first eligible unit.
        feed_run(&io, 1, false, 5);
        scan(&mut group, &io, 20.0, 4);
        assert!(!out_bool(&io, cmd(1)));
        assert!(out_bool(&io, vent(1)));
        assert!(out_bool(&io, cmd(2)));
        // The proven stop resolves unit 1; unit 2 — the last unit
        // standing against a positive demand — stays on the header.
        scan(&mut group, &io, 20.0, 5);
        assert_eq!(out_int(&io, STAGED), 1);
        assert!(out_bool(&io, cmd(2)));
    }

    #[test]
    fn start_interval_ticks_hold_the_restart() {
        let mut group = component(BlowerGroupConfig {
            min_start_interval_ticks: 4,
            ..config()
        });
        let io = io(3, false);
        // Unit 1 joins at 2, proves at 3; demand drops at 4 and it
        // departs, proven stopped at 5 — its restart holds until tick
        // 5 + 4 = 9.
        scan(&mut group, &io, 50.0, 2);
        feed_run(&io, 1, true, 3);
        scan(&mut group, &io, 50.0, 3);
        feed_run(&io, 1, false, 4);
        scan(&mut group, &io, 0.0, 4);
        assert!(out_bool(&io, vent(1)));
        scan(&mut group, &io, 0.0, 5);
        assert!(!out_bool(&io, vent(1)));

        // Demand returns at 6: unit 1 is held out, so the lowest
        // eligible unit — unit 2 — takes the demand.
        feed_run(&io, 2, true, 7);
        scan(&mut group, &io, 50.0, 6);
        assert!(out_bool(&io, cmd(2)));
        assert!(!out_bool(&io, cmd(1)));
        scan(&mut group, &io, 50.0, 7);
        assert!(!out_bool(&io, vent(2)));
        // Still inside unit 1's interval, a second warranted stage
        // picks unit 3 rather than waiting on it.
        feed_run(&io, 3, true, 9);
        scan(&mut group, &io, 100.0, 8);
        assert!(out_bool(&io, cmd(3)));
        assert!(!out_bool(&io, cmd(1)));
        scan(&mut group, &io, 100.0, 9);
        // Tick 9 clears unit 1's interval: demand 200 needs a third.
        feed_run(&io, 1, true, 11);
        scan(&mut group, &io, 200.0, 10);
        assert!(out_bool(&io, cmd(1)));
        scan(&mut group, &io, 200.0, 11);
        assert_eq!(out_int(&io, STAGED), 3);
    }

    #[test]
    fn a_faulted_joined_unit_departs_and_the_standby_picks_up() {
        let mut group = component(config());
        let io = io(3, false);
        // Units 1 and 2 join on demand 150.
        scan(&mut group, &io, 150.0, 1);
        feed_run(&io, 1, true, 2);
        scan(&mut group, &io, 150.0, 2);
        feed_run(&io, 2, true, 3);
        scan(&mut group, &io, 150.0, 3);
        assert_eq!(out_int(&io, STAGED), 2);

        // Unit 1's fault asserts: it departs through the choreography —
        // vent open, command dropped — at the same scan, while the
        // serialized elective start waits one scan for the handover.
        feed_fault(&io, 1, true, 4);
        feed_run(&io, 1, false, 5);
        feed_run(&io, 3, true, 6);
        scan(&mut group, &io, 150.0, 4);
        assert!(!out_bool(&io, cmd(1)));
        assert!(out_bool(&io, vent(1)));
        assert!(out_bool(&io, TRANSITION));
        assert!(!out_bool(&io, cmd(3)));
        // The proven stop resolves the departure and the standby joins
        // at the next scan.
        scan(&mut group, &io, 150.0, 5);
        assert!(out_bool(&io, cmd(3)));
        assert!(out_bool(&io, vent(3)));
        scan(&mut group, &io, 150.0, 6);
        assert_eq!(out_int(&io, STAGED), 2);
        assert!(!out_bool(&io, vent(3)));
        assert_eq!(out_f64(&io, capacity(3)), 75.0);
        // The faulted unit never re-enters the staging pool.
        scan(&mut group, &io, 250.0, 7);
        assert!(!out_bool(&io, cmd(1)));
    }

    #[test]
    fn unavailable_and_faulted_units_are_excluded() {
        let mut group = component(config());
        let io = io(3, false);
        // Unit 1 unavailable and unit 2 faulted from the start: the
        // demand stages only unit 3.
        feed_avail(&io, 1, false, 1);
        feed_fault(&io, 2, true, 1);
        feed_run(&io, 3, true, 2);
        scan(&mut group, &io, 100.0, 1);
        assert!(out_bool(&io, cmd(3)));
        assert!(!out_bool(&io, cmd(1)));
        assert!(!out_bool(&io, cmd(2)));
        assert!(!out_bool(&io, NONE_AVAIL));
        scan(&mut group, &io, 100.0, 2);
        assert_eq!(out_int(&io, STAGED), 1);
    }

    #[test]
    fn an_unproven_join_aborts_after_vent_ticks() {
        let mut group = component_of(
            1,
            BlowerGroupConfig {
                min_start_interval_ticks: 3,
                ..config()
            },
            None,
        );
        let io = io(1, false);
        // The join starts at tick 1 with no run feedback: the first
        // unproven scan counts one, the second reaches vent_ticks and
        // the start aborts — command and vent off, the unit offline.
        scan(&mut group, &io, 100.0, 1);
        assert!(out_bool(&io, cmd(1)));
        assert!(out_bool(&io, vent(1)));
        scan(&mut group, &io, 100.0, 2);
        assert!(out_bool(&io, cmd(1)));
        scan(&mut group, &io, 100.0, 3);
        assert!(!out_bool(&io, cmd(1)));
        assert!(!out_bool(&io, vent(1)));
        assert_eq!(out_f64(&io, capacity(1)), 0.0);
        // The abort banks the start interval: the standing demand
        // cannot restart the unit until tick 3 + 3.
        scan(&mut group, &io, 100.0, 4);
        assert!(!out_bool(&io, cmd(1)));
        scan(&mut group, &io, 100.0, 5);
        assert!(!out_bool(&io, cmd(1)));
        scan(&mut group, &io, 100.0, 6);
        assert!(out_bool(&io, cmd(1)));
        assert!(out_bool(&io, vent(1)));
    }

    #[test]
    fn an_unproven_stop_resolves_offline_after_vent_ticks() {
        let mut group = component(config());
        let io = io(3, false);
        scan(&mut group, &io, 50.0, 1);
        feed_run(&io, 1, true, 2);
        scan(&mut group, &io, 50.0, 2);
        // Demand falls to nothing: unit 1 departs but its run feedback
        // never drops — after vent_ticks the group releases it anyway
        // and the still-running machine keeps accumulating hours.
        scan(&mut group, &io, 0.0, 3);
        assert!(out_bool(&io, vent(1)));
        assert!(!out_bool(&io, cmd(1)));
        scan(&mut group, &io, 0.0, 4);
        assert!(out_bool(&io, vent(1)));
        scan(&mut group, &io, 0.0, 5);
        assert!(!out_bool(&io, vent(1)));
        assert_eq!(out_int(&io, STAGED), 0);
        let state = group.capture_state();
        assert!(
            matches!(state.get("run_hours_1"), Some(Value::Int(hours)) if hours > 0),
            "the failed-stop unit's run-hours kept accumulating"
        );
    }

    #[test]
    fn no_rotation_departs_the_most_recently_joined() {
        let mut group = component(config());
        let io = io(3, false);
        // All three join in index order on demand 250.
        for (index, tick) in [(1usize, 1u64), (2, 2), (3, 3)] {
            scan(&mut group, &io, 250.0, tick);
            feed_run(&io, index, true, tick + 1);
        }
        scan(&mut group, &io, 250.0, 4);
        assert_eq!(out_int(&io, STAGED), 3);
        // Falling demand departs the tail of the join order — unit 3
        // first, then unit 2.
        feed_run(&io, 3, false, 5);
        scan(&mut group, &io, 140.0, 5);
        assert!(!out_bool(&io, cmd(3)));
        assert!(out_bool(&io, cmd(2)));
        scan(&mut group, &io, 140.0, 6);
        feed_run(&io, 2, false, 7);
        scan(&mut group, &io, 60.0, 7);
        assert!(!out_bool(&io, cmd(2)));
        assert!(out_bool(&io, cmd(1)));
    }

    #[test]
    fn fixed_order_departs_the_highest_index() {
        let mut group = component(BlowerGroupConfig {
            rotation: BlowerRotation::FixedOrder,
            ..config()
        });
        let io = io(3, false);
        // Units 1 and 2 join, unit 1 faults out, unit 3 takes over —
        // the join order is 2,3 while the fixed policy still departs
        // the highest index.
        scan(&mut group, &io, 150.0, 1);
        feed_run(&io, 1, true, 2);
        scan(&mut group, &io, 150.0, 2);
        feed_run(&io, 2, true, 3);
        scan(&mut group, &io, 150.0, 3);
        feed_fault(&io, 1, true, 4);
        feed_run(&io, 1, false, 4);
        feed_run(&io, 3, true, 6);
        scan(&mut group, &io, 150.0, 4);
        scan(&mut group, &io, 150.0, 5);
        scan(&mut group, &io, 150.0, 6);
        assert_eq!(out_int(&io, STAGED), 2);
        // Demand 100 still warrants two joined over 180 committed? No —
        // 100 < 0.8 × (180 − 90) = 72 fails, so both stay. Demand 60
        // de-stages: unit 3 — the highest joined index — departs even
        // though unit 2 joined before it under LIFO too. The
        // distinguishing case: unit 1 recovered and re-joined, so the
        // join order ends …3,1 while the fixed departure picks 3.
        feed_fault(&io, 1, false, 7);
        feed_run(&io, 1, true, 9);
        scan(&mut group, &io, 250.0, 7);
        scan(&mut group, &io, 250.0, 8);
        scan(&mut group, &io, 250.0, 9);
        assert_eq!(out_int(&io, STAGED), 3);
        feed_run(&io, 3, false, 10);
        scan(&mut group, &io, 140.0, 10);
        assert!(!out_bool(&io, cmd(3)), "highest joined index departs");
        assert!(out_bool(&io, cmd(1)));
        assert!(out_bool(&io, cmd(2)));
    }

    #[test]
    fn equalize_runtime_rotates_through_the_choreography() {
        let mut group = component_of(
            2,
            BlowerGroupConfig {
                rotation: BlowerRotation::EqualizeRuntime,
                min_run_ticks: 3,
                ..config()
            },
            None,
        );
        let io = io(2, false);
        // Unit 1 joins at 1 and proves at 2; its run-hours reach the
        // three-tick margin over the zero-hour standby at scan 4 —
        // the rotation fires: unit 2 starts off-header.
        scan(&mut group, &io, 50.0, 1);
        feed_run(&io, 1, true, 2);
        scan(&mut group, &io, 50.0, 2);
        scan(&mut group, &io, 50.0, 3);
        assert!(!out_bool(&io, cmd(2)), "the margin is not yet met");
        scan(&mut group, &io, 50.0, 4);
        assert!(out_bool(&io, cmd(2)));
        assert!(out_bool(&io, vent(2)));
        assert!(out_bool(&io, TRANSITION));
        // The join proves; the same scan's handover departs the
        // most-run unit — transition stays asserted across the pair.
        feed_run(&io, 2, true, 5);
        scan(&mut group, &io, 50.0, 5);
        assert!(!out_bool(&io, cmd(1)));
        assert!(out_bool(&io, vent(1)));
        assert!(out_bool(&io, TRANSITION));
        feed_run(&io, 1, false, 6);
        scan(&mut group, &io, 50.0, 6);
        assert!(!out_bool(&io, vent(1)));
        assert!(!out_bool(&io, TRANSITION));
        assert_eq!(out_int(&io, STAGED), 1);
        assert_eq!(out_f64(&io, capacity(2)), 50.0);
    }

    #[test]
    fn equalize_runtime_picks_least_run_to_join_and_most_run_to_depart() {
        let mut group = component(BlowerGroupConfig {
            rotation: BlowerRotation::EqualizeRuntime,
            min_run_ticks: 2,
            ..config()
        });
        let io = io(3, false);
        // Unit 2 ran two scans before the group ever commanded it —
        // its accumulated hours put it behind the zero-hour units on
        // the join ranking.
        for tick in 1..=2 {
            feed_run(&io, 2, true, tick);
            scan(&mut group, &io, 0.0, tick);
        }
        feed_run(&io, 2, false, 3);
        scan(&mut group, &io, 50.0, 3);
        assert!(out_bool(&io, cmd(1)), "the least-run unit joins");
        feed_run(&io, 1, true, 4);
        scan(&mut group, &io, 50.0, 4);
        // Demand 150 stages a second unit: unit 3's zero hours beat
        // unit 2's two.
        scan(&mut group, &io, 150.0, 5);
        assert!(out_bool(&io, cmd(3)));
        assert!(!out_bool(&io, cmd(2)));
        feed_run(&io, 3, true, 6);
        scan(&mut group, &io, 150.0, 6);
        // Demand falls: the most-run joined unit — unit 1 — departs
        // before unit 3.
        feed_run(&io, 1, false, 7);
        scan(&mut group, &io, 30.0, 7);
        assert!(!out_bool(&io, cmd(1)));
        assert!(out_bool(&io, cmd(3)));
    }

    #[test]
    fn operator_approval_holds_the_request_until_granted() {
        let mut group = component_of(
            2,
            BlowerGroupConfig {
                staging_authority: StagingAuthority::OperatorApproval,
                ..config()
            },
            Some(APPROVE),
        );
        let io = io(2, true);
        // Demand 100 warrants a stage: pending asserts and nothing
        // commands.
        scan(&mut group, &io, 100.0, 1);
        assert!(out_bool(&io, PENDING));
        assert!(!out_bool(&io, cmd(1)));
        scan(&mut group, &io, 100.0, 2);
        assert!(out_bool(&io, PENDING));
        // The approve grant executes the held request at the scan it
        // reads; the join choreography runs normally.
        feed_approve(&io, true, 3);
        feed_run(&io, 1, true, 4);
        scan(&mut group, &io, 100.0, 3);
        assert!(!out_bool(&io, PENDING));
        assert!(out_bool(&io, cmd(1)));
        assert!(out_bool(&io, vent(1)));
        scan(&mut group, &io, 100.0, 4);
        assert!(!out_bool(&io, vent(1)));
        // The standing approve was consumed: the still-warranted second
        // stage pends while the point re-arms on a false, and the next
        // true grants it.
        scan(&mut group, &io, 100.0, 5);
        assert!(out_bool(&io, PENDING));
        assert!(!out_bool(&io, cmd(2)));
        feed_approve(&io, false, 6);
        scan(&mut group, &io, 100.0, 6);
        assert!(out_bool(&io, PENDING));
        feed_approve(&io, true, 7);
        feed_run(&io, 2, true, 8);
        scan(&mut group, &io, 100.0, 7);
        assert!(out_bool(&io, cmd(2)));
        assert!(out_bool(&io, vent(2)));
        scan(&mut group, &io, 100.0, 8);
        assert_eq!(out_int(&io, STAGED), 2);
        assert!(!out_bool(&io, PENDING));
    }

    #[test]
    fn a_standing_approve_grants_the_next_request() {
        let mut group = component_of(
            2,
            BlowerGroupConfig {
                staging_authority: StagingAuthority::OperatorApproval,
                ..config()
            },
            Some(APPROVE),
        );
        let io = io(2, true);
        // Approve standing true ahead of the request pre-authorizes the
        // next eligible change — it executes the scan the demand
        // arrives.
        feed_approve(&io, true, 1);
        feed_run(&io, 1, true, 2);
        scan(&mut group, &io, 50.0, 1);
        assert!(out_bool(&io, cmd(1)));
        assert!(!out_bool(&io, PENDING));
        // The grant is consumed: a later request pends on the still-
        // standing approve.
        scan(&mut group, &io, 100.0, 2);
        assert!(out_bool(&io, PENDING));
        assert!(!out_bool(&io, cmd(2)));
    }

    #[test]
    fn flag_only_flags_the_request_and_never_commands() {
        let mut group = component_of(
            2,
            BlowerGroupConfig {
                staging_authority: StagingAuthority::FlagOnly,
                ..config()
            },
            None,
        );
        let io = io(2, false);
        scan(&mut group, &io, 100.0, 1);
        assert!(out_bool(&io, PENDING));
        assert!(!out_bool(&io, cmd(1)));
        assert_eq!(out_f64(&io, capacity(1)), 0.0);
        assert_eq!(out_int(&io, STAGED), 0);
        // The recommendation stands while the demand does — the
        // operation stays manual.
        for tick in 2..=4 {
            scan(&mut group, &io, 100.0, tick);
            assert!(out_bool(&io, PENDING));
            assert!(!out_bool(&io, cmd(1)));
        }
        scan(&mut group, &io, 0.0, 5);
        assert!(!out_bool(&io, PENDING));
    }

    #[test]
    fn operator_approval_requires_a_bound_approve_point() {
        let error = BlowerGroup::new(
            "bg",
            DEMAND,
            None,
            blowers(1),
            bounds(1),
            OUTPUTS,
            BlowerGroupConfig {
                staging_authority: StagingAuthority::OperatorApproval,
                ..config()
            },
        )
        .unwrap_err();
        match error {
            ParameterError::Invalid { parameter, .. } => assert_eq!(parameter, "approve"),
            other => panic!("expected Invalid, got {other:?}"),
        }
    }

    #[test]
    fn none_available_and_all_faulted_report_the_group_conditions() {
        let mut group = component_of(2, config(), None);
        let io = io(2, false);
        scan(&mut group, &io, 0.0, 1);
        assert!(!out_bool(&io, NONE_AVAIL));
        assert!(!out_bool(&io, ALL_FAULTED));
        // Every availability lost — none_available asserts without a
        // fault standing.
        for index in 1..=2 {
            feed_avail(&io, index, false, 2);
        }
        scan(&mut group, &io, 0.0, 2);
        assert!(out_bool(&io, NONE_AVAIL));
        assert!(!out_bool(&io, ALL_FAULTED));
        // Every fault flag failed: both conditions stand, since a
        // faulted unit is never available.
        for index in 1..=2 {
            feed_avail(&io, index, true, 3);
            feed_fault(&io, index, true, 3);
        }
        scan(&mut group, &io, 0.0, 3);
        assert!(out_bool(&io, NONE_AVAIL));
        assert!(out_bool(&io, ALL_FAULTED));
    }

    #[test]
    fn non_good_inputs_take_the_fail_safe_reading() {
        let mut group = component(config());
        let io = io(3, false);
        scan(&mut group, &io, 50.0, 1);
        feed_run(&io, 1, true, 2);
        scan(&mut group, &io, 50.0, 2);
        assert_eq!(out_int(&io, STAGED), 1);

        // A non-Good demand cannot move the group: the held 50 neither
        // stages down nor up while the bad reading stands.
        io.feed(
            DEMAND,
            Sample::new(
                Value::Float(0.0),
                Quality::Bad(QualityReason::CommunicationFault),
                Tick(3),
            ),
        );
        group.step(&io, Tick(3)).unwrap();
        assert_eq!(out_int(&io, STAGED), 1);
        assert!(out_bool(&io, cmd(1)));

        // A non-Good availability reads unavailable — the joined unit
        // departs the same scan.
        io.feed(
            avail(2),
            Sample::new(
                Value::Bool(true),
                Quality::Bad(QualityReason::CommunicationFault),
                Tick(4),
            ),
        );
        feed_run(&io, 3, true, 6);
        io.feed(
            avail(1),
            Sample::new(
                Value::Bool(true),
                Quality::Bad(QualityReason::CommunicationFault),
                Tick(4),
            ),
        );
        feed_run(&io, 1, false, 4);
        feed_demand(&io, 50.0, 4);
        group.step(&io, Tick(4)).unwrap();
        assert!(!out_bool(&io, cmd(1)));
        assert!(out_bool(&io, vent(1)));
        // The demand hands to unit 3 — unit 2's untrusted availability
        // excludes it.
        group.step(&io, Tick(5)).unwrap();
        assert!(out_bool(&io, cmd(3)));
        group.step(&io, Tick(6)).unwrap();
        assert_eq!(out_int(&io, STAGED), 1);
        assert!(!out_bool(&io, vent(3)));
    }

    #[test]
    fn a_non_good_approve_grants_nothing() {
        let mut group = component_of(
            2,
            BlowerGroupConfig {
                staging_authority: StagingAuthority::OperatorApproval,
                ..config()
            },
            Some(APPROVE),
        );
        let io = io(2, true);
        scan(&mut group, &io, 100.0, 1);
        assert!(out_bool(&io, PENDING));
        io.feed(
            APPROVE,
            Sample::new(
                Value::Bool(true),
                Quality::Bad(QualityReason::CommunicationFault),
                Tick(2),
            ),
        );
        group.step(&io, Tick(2)).unwrap();
        assert!(out_bool(&io, PENDING));
        assert!(!out_bool(&io, cmd(1)));
    }

    #[test]
    fn capture_restore_mid_join_resumes_identically() {
        // The checkpoint lands inside unit 1's prove window: the
        // restored instance continues the same join, run-hours, and
        // held demand — the outputs agree scan for scan.
        let run_pair = |group: &mut BlowerGroup| {
            let io = io(3, false);
            let mut outputs = Vec::new();
            for tick in 1..=5 {
                feed_run(&io, 1, true, 3);
                feed_run(&io, 2, true, 5);
                scan(group, &io, 150.0, tick);
                outputs.push((
                    io.written(cmd(1)).unwrap().value,
                    io.written(vent(1)).unwrap().value,
                    io.written(cmd(2)).unwrap().value,
                    io.written(vent(2)).unwrap().value,
                    io.written(STAGED).unwrap().value,
                    io.written(TRANSITION).unwrap().value,
                    io.written(capacity(1)).unwrap().value,
                    io.written(capacity(2)).unwrap().value,
                ));
            }
            outputs
        };
        let mut reference = component(config());
        let expected = run_pair(&mut reference);

        let mut interrupted = component(config());
        let io = io(3, false);
        scan(&mut interrupted, &io, 150.0, 1);
        let checkpoint = interrupted.capture_state();
        assert_eq!(checkpoint.get("phase_1"), Some(Value::Int(1)));
        assert_eq!(checkpoint.get("last_demand"), Some(Value::Float(150.0)));

        let mut standby = component(config());
        standby.restore_state(&checkpoint).unwrap();
        let mut outputs = vec![(
            io.written(cmd(1)).unwrap().value,
            io.written(vent(1)).unwrap().value,
            io.written(cmd(2)).unwrap().value,
            io.written(vent(2)).unwrap().value,
            io.written(STAGED).unwrap().value,
            io.written(TRANSITION).unwrap().value,
            io.written(capacity(1)).unwrap().value,
            io.written(capacity(2)).unwrap().value,
        )];
        for tick in 2..=5 {
            feed_run(&io, 1, true, 3);
            feed_run(&io, 2, true, 5);
            scan(&mut standby, &io, 150.0, tick);
            outputs.push((
                io.written(cmd(1)).unwrap().value,
                io.written(vent(1)).unwrap().value,
                io.written(cmd(2)).unwrap().value,
                io.written(vent(2)).unwrap().value,
                io.written(STAGED).unwrap().value,
                io.written(TRANSITION).unwrap().value,
                io.written(capacity(1)).unwrap().value,
                io.written(capacity(2)).unwrap().value,
            ));
        }
        assert_eq!(outputs, expected);
    }

    #[test]
    fn checkpoint_round_trips_position_hours_and_timers() {
        let mut group = component(BlowerGroupConfig {
            min_start_interval_ticks: 5,
            ..config()
        });
        let io = io(3, false);
        // Unit 1 joined and running; unit 2 in flight; unit 3 ran one
        // uncommanded scan — the checkpoint carries phases, prove
        // timers, run-hours, and the interval references.
        feed_run(&io, 3, true, 0);
        scan(&mut group, &io, 50.0, 1);
        feed_run(&io, 3, false, 1);
        feed_run(&io, 1, true, 2);
        scan(&mut group, &io, 50.0, 2);
        scan(&mut group, &io, 50.0, 3);
        scan(&mut group, &io, 150.0, 4);
        let state = group.capture_state();
        assert_eq!(state.get("phase_1"), Some(Value::Int(2)));
        assert_eq!(state.get("phase_2"), Some(Value::Int(1)));
        assert_eq!(state.get("phase_3"), Some(Value::Int(0)));
        assert_eq!(state.get("phase_ticks_2"), Some(Value::Int(0)));
        assert_eq!(state.get("started_at_1"), Some(Value::Int(1)));
        assert_eq!(state.get("join_rank_1"), Some(Value::Int(1)));
        assert_eq!(state.get("join_rank_2"), Some(Value::Int(0)));
        // Unit 3 was never commanded: the scan its feedback stood true
        // banked a run-hour without a stop reference.
        assert_eq!(state.get("run_hours_3"), Some(Value::Int(1)));
        assert_eq!(state.get("stopped_at_3"), Some(Value::Int(0)));

        let mut standby = component(BlowerGroupConfig {
            min_start_interval_ticks: 5,
            ..config()
        });
        standby.restore_state(&state).unwrap();
        assert_eq!(standby.capture_state(), state);
    }

    #[test]
    fn restore_rejects_malformed_state_naming_the_field() {
        let group = || component_of(2, config(), None);
        let valid = group().capture_state();

        // An unknown field.
        let mut extra = valid.clone();
        extra.insert("bogus", Value::Int(0));
        assert!(matches!(
            group().restore_state(&extra),
            Err(StateError::UnknownField { ref field, .. }) if field == "bogus"
        ));

        // An undeclared phase code.
        let mut bad = valid.clone();
        bad.insert("phase_1", Value::Int(9));
        assert!(matches!(
            group().restore_state(&bad),
            Err(StateError::InvalidValue { ref field, .. }) if field == "phase_1"
        ));

        // A rank on a unit that is not joined.
        let mut bad = valid.clone();
        bad.insert("join_rank_1", Value::Int(1));
        assert!(matches!(
            group().restore_state(&bad),
            Err(StateError::InvalidValue { ref field, .. }) if field == "join_rank_1"
        ));

        // One rotation leg without the other.
        let mut bad = valid.clone();
        bad.insert("rotation_depart", Value::Int(1));
        assert!(matches!(
            group().restore_state(&bad),
            Err(StateError::InvalidValue { ref field, .. }) if field == "rotation_join"
        ));

        // A bounds inversion.
        let mut bad = valid.clone();
        bad.insert("unit_1_max_flow", Value::Float(5.0));
        assert!(matches!(
            group().restore_state(&bad),
            Err(StateError::InvalidValue { ref field, .. }) if field == "unit_1_max_flow"
        ));

        // A negative timer and a zero prove window.
        let mut bad = valid.clone();
        bad.insert("min_run_ticks", Value::Int(-1));
        assert!(matches!(
            group().restore_state(&bad),
            Err(StateError::InvalidValue { ref field, .. }) if field == "min_run_ticks"
        ));
        let mut bad = valid.clone();
        bad.insert("vent_ticks", Value::Int(0));
        assert!(matches!(
            group().restore_state(&bad),
            Err(StateError::InvalidValue { ref field, .. }) if field == "vent_ticks"
        ));
    }

    #[test]
    fn from_parameters_reads_the_declared_map() {
        let parameters: Parameters = [
            ("staging_authority", Value::Int(0)),
            ("stage_up", Value::Float(0.9)),
            ("stage_down", Value::Float(0.8)),
            ("min_run_ticks", Value::Int(3)),
            ("min_start_interval_ticks", Value::Int(4)),
            ("unit_1_min_flow", Value::Float(20.0)),
            ("unit_1_max_flow", Value::Float(100.0)),
            ("unit_1_max_current", Value::Float(90.0)),
            ("unit_2_min_flow", Value::Float(20.0)),
            ("unit_2_max_flow", Value::Float(100.0)),
            ("unit_2_max_current", Value::Float(90.0)),
            ("vent_ticks", Value::Int(2)),
            ("rotation", Value::Int(1)),
        ]
        .into_iter()
        .map(|(name, value)| (name.to_string(), value))
        .collect();
        let group =
            BlowerGroup::from_parameters("bg", DEMAND, None, blowers(2), OUTPUTS, &parameters)
                .unwrap();
        let config = group.config();
        assert_eq!(config.staging_authority, StagingAuthority::Automatic);
        assert_eq!(config.rotation, BlowerRotation::EqualizeRuntime);
        assert_eq!(config.min_run_ticks, 3);
        assert_eq!(config.vent_ticks, 2);
    }

    #[test]
    fn from_parameters_names_the_offending_parameter() {
        let full: Parameters = [
            ("staging_authority", Value::Int(0)),
            ("stage_up", Value::Float(0.9)),
            ("stage_down", Value::Float(0.8)),
            ("min_run_ticks", Value::Int(0)),
            ("min_start_interval_ticks", Value::Int(0)),
            ("unit_1_min_flow", Value::Float(20.0)),
            ("unit_1_max_flow", Value::Float(100.0)),
            ("unit_1_max_current", Value::Float(90.0)),
            ("vent_ticks", Value::Int(2)),
            ("rotation", Value::Int(0)),
        ]
        .into_iter()
        .map(|(name, value)| (name.to_string(), value))
        .collect();
        let build = |parameters: &Parameters| {
            BlowerGroup::from_parameters("bg", DEMAND, None, blowers(1), OUTPUTS, parameters)
        };

        let mut missing = full.clone();
        missing.remove("staging_authority");
        assert!(matches!(
            build(&missing),
            Err(ParameterError::Missing { ref parameter, .. }) if parameter == "staging_authority"
        ));
        let mut missing = full.clone();
        missing.remove("unit_1_max_flow");
        assert!(matches!(
            build(&missing),
            Err(ParameterError::Missing { ref parameter, .. }) if parameter == "unit_1_max_flow"
        ));

        let mut bad = full.clone();
        bad.insert("staging_authority".to_string(), Value::Int(7));
        assert!(matches!(
            build(&bad),
            Err(ParameterError::Invalid { ref parameter, .. }) if parameter == "staging_authority"
        ));
        let mut bad = full.clone();
        bad.insert("rotation".to_string(), Value::Int(3));
        assert!(matches!(
            build(&bad),
            Err(ParameterError::Invalid { ref parameter, .. }) if parameter == "rotation"
        ));
        let mut bad = full.clone();
        bad.insert("unit_1_max_current".to_string(), Value::Float(10.0));
        assert!(matches!(
            build(&bad),
            Err(ParameterError::Invalid { ref parameter, .. }) if parameter == "unit_1_max_current"
        ));
        let mut bad = full.clone();
        bad.insert("vent_ticks".to_string(), Value::Int(0));
        assert!(matches!(
            build(&bad),
            Err(ParameterError::Invalid { ref parameter, .. }) if parameter == "vent_ticks"
        ));
        let mut bad = full.clone();
        bad.insert("stage_up".to_string(), Value::Float(-0.5));
        assert!(matches!(
            build(&bad),
            Err(ParameterError::Invalid { ref parameter, .. }) if parameter == "stage_up"
        ));
    }

    #[test]
    fn an_empty_unit_set_is_a_construction_error() {
        assert!(matches!(
            BlowerGroup::new("bg", DEMAND, None, vec![], vec![], OUTPUTS, config()),
            Err(ParameterError::Invalid { ref parameter, .. }) if parameter == "units"
        ));
    }

    #[test]
    fn a_bounds_count_mismatch_is_a_construction_error() {
        assert!(matches!(
            BlowerGroup::new("bg", DEMAND, None, blowers(2), bounds(1), OUTPUTS, config()),
            Err(ParameterError::Invalid { ref parameter, .. }) if parameter == "bounds"
        ));
    }

    #[test]
    fn apply_parameter_tunes_and_refuses() {
        let mut group = component_of(2, config(), None);
        group
            .apply_parameter("stage_up", Value::Float(0.75))
            .unwrap();
        assert_eq!(group.config().stage_up, 0.75);
        group.apply_parameter("rotation", Value::Int(2)).unwrap();
        assert_eq!(group.config().rotation, BlowerRotation::FixedOrder);
        group
            .apply_parameter("unit_2_max_flow", Value::Float(80.0))
            .unwrap();
        group
            .apply_parameter("min_start_interval_ticks", Value::Int(7))
            .unwrap();
        assert_eq!(group.config().min_start_interval_ticks, 7);

        // Refusals name the parameter and change nothing.
        assert!(matches!(
            group.apply_parameter("vent_ticks", Value::Int(0)),
            Err(CommandError::InvalidParameter { ref parameter, .. }) if parameter == "vent_ticks"
        ));
        assert!(matches!(
            group.apply_parameter("unit_1_min_flow", Value::Float(200.0)),
            Err(CommandError::InvalidParameter { ref parameter, .. }) if parameter == "unit_1_min_flow"
        ));
        assert!(matches!(
            group.apply_parameter("rotation", Value::Int(9)),
            Err(CommandError::InvalidParameter { ref parameter, .. }) if parameter == "rotation"
        ));
        // Approval without a bound approve point is refused.
        assert!(matches!(
            group.apply_parameter("staging_authority", Value::Int(1)),
            Err(CommandError::InvalidParameter { ref parameter, .. })
                if parameter == "staging_authority"
        ));
        assert!(matches!(
            group.apply_parameter("nope", Value::Int(0)),
            Err(CommandError::UnknownParameter { ref parameter, .. }) if parameter == "nope"
        ));
        assert!(matches!(
            group.apply_parameter("unit_9_min_flow", Value::Float(1.0)),
            Err(CommandError::UnknownParameter { ref parameter, .. })
                if parameter == "unit_9_min_flow"
        ));
    }

    #[test]
    fn describe_reports_roles_and_parameter_ranges() {
        let group = component_of(2, config(), Some(APPROVE));
        let descriptor = group.describe();
        assert_eq!(descriptor.kind, BlowerGroup::KIND);

        let port = |name: &str| {
            descriptor
                .ports
                .iter()
                .find(|port| port.name == name)
                .unwrap_or_else(|| panic!("missing port {name}"))
        };
        assert_eq!(port("demand").direction, Direction::In);
        assert_eq!(port("demand").kind, ValueKind::Float);
        assert_eq!(port("demand").role, Some(PortRole::Setpoint));
        assert_eq!(port("approve").role, Some(PortRole::Setpoint));
        assert_eq!(port("cmd_1").role, Some(PortRole::Output));
        assert_eq!(port("run_2").role, Some(PortRole::ProcessValue));
        assert_eq!(port("capacity_2").direction, Direction::Out);
        assert_eq!(port("capacity_2").kind, ValueKind::Float);
        assert_eq!(port("vent_2").direction, Direction::Out);
        assert_eq!(port("staged").kind, ValueKind::Int);
        assert_eq!(port("transition").role, Some(PortRole::Status));
        // The unbound form declares no `approve` port.
        assert!(
            component_of(2, config(), None)
                .describe()
                .ports
                .iter()
                .all(|port| port.name != "approve")
        );

        let names: Vec<&str> = descriptor
            .parameters
            .iter()
            .map(|parameter| parameter.name.as_str())
            .collect();
        assert_eq!(
            names,
            [
                "staging_authority",
                "stage_up",
                "stage_down",
                "min_run_ticks",
                "min_start_interval_ticks",
                "unit_1_min_flow",
                "unit_1_max_flow",
                "unit_1_max_current",
                "unit_2_min_flow",
                "unit_2_max_flow",
                "unit_2_max_current",
                "vent_ticks",
                "rotation",
            ]
        );
        let authority = descriptor
            .parameters
            .iter()
            .find(|parameter| parameter.name == "staging_authority")
            .unwrap();
        assert_eq!(authority.range.unwrap().min, Value::Int(0));
        assert_eq!(authority.range.unwrap().max, Value::Int(2));
    }

    #[test]
    fn identical_input_runs_capture_identical_state() {
        let run_to = |group: &mut BlowerGroup| {
            let io = io(3, false);
            for tick in 1..=8 {
                feed_run(&io, 1, true, 2);
                feed_run(&io, 2, true, 4);
                feed_run(&io, 3, true, 6);
                scan(group, &io, if tick >= 5 { 200.0 } else { 100.0 }, tick);
            }
            group.capture_state()
        };
        assert_eq!(
            run_to(&mut component(config())),
            run_to(&mut component(config()))
        );
    }
}
