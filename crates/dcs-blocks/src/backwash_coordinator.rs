//! Shared-supply backwash arbitration: the exclusive-grant contract
//! architecture decision 56 records for filter backwash — the
//! `WW-CTL-004` requirement `docs/requirements/water-wastewater.md`
//! carries (`docs/research/filter-backwash.md`). An ordered request
//! queue and the held grant are per-scan run state — `pump-group`
//! stages demanded members rather than serializing them, and no Bool
//! fold holds queue order — so the arbitration is a component kind,
//! not a wiring composition.

use crate::describe;
use crate::params::{self, ParameterError, Parameters};
use dcs_core::{
    CommandError, ComponentDescriptor, ParameterRange, PointId, PortRole, Quality, Sample,
    StateError, StateMap, Tick, Value, ValueKind,
};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// How a [`BackwashCoordinator`] orders queued requests.
///
/// The model's parameter vocabulary has no string type, so the
/// `queue_policy` parameter carries the choice as an `Int` code — the
/// values [`code`](Self::code) reports and [`decode`](Self::decode)
/// accepts. All three codes are the decision's recorded
/// customer-validation assumptions, carried as declared data rather
/// than defaults.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum QueuePolicy {
    /// `0` — first-in/first-out: the queue order is the order the
    /// `request_i` inputs first asserted.
    Fifo,
    /// `1` — priority by trigger: the queue orders by filter index, a
    /// lower-numbered request always ahead of a higher-numbered one
    /// regardless of arrival. The coordinator sees only `request_i` —
    /// the sequence's trigger class does not reach it — so the
    /// plant's trigger-to-priority mapping composes in bank wiring by
    /// numbering the filters in priority order.
    PriorityByTrigger,
    /// `2` — operator-managed ordering: requests still join the queue
    /// in assertion order so `position_i`/`queued` report the bank's
    /// state, but no grant issues until the standing `reorder`
    /// instruction names a queued member — that member moves to the
    /// head and takes the grant. A plant declaring this policy exposes
    /// the `reorder` point as the queue's ordering authority.
    OperatorManaged,
}

impl QueuePolicy {
    /// The `Int` code the `queue_policy` parameter carries.
    pub const fn code(self) -> i64 {
        match self {
            Self::Fifo => 0,
            Self::PriorityByTrigger => 1,
            Self::OperatorManaged => 2,
        }
    }

    /// The policy the `Int` code `code` selects, or `None` when the
    /// code declares no policy.
    pub const fn decode(code: i64) -> Option<Self> {
        match code {
            0 => Some(Self::Fifo),
            1 => Some(Self::PriorityByTrigger),
            2 => Some(Self::OperatorManaged),
            _ => None,
        }
    }
}

/// What a queued filter does while it waits — the `queued_state`
/// parameter's `Int` code.
///
/// The coordinator does not itself drive filter process outputs — the
/// choice's effect composes in per-plant wiring from `request_i` and
/// `grant_i`. The kind carries it as declared, checkable contract data
/// so the assumption is explicit rather than a hidden default.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum QueuedState {
    /// `0` — the queued filter keeps filtering until granted.
    KeepFiltering,
    /// `1` — the queued filter goes offline, a standby filter covering
    /// its duty.
    OfflineWithStandby,
}

impl QueuedState {
    /// The `Int` code the `queued_state` parameter carries.
    pub const fn code(self) -> i64 {
        match self {
            Self::KeepFiltering => 0,
            Self::OfflineWithStandby => 1,
        }
    }

    /// The state the `Int` code `code` selects, or `None` when the
    /// code declares no state.
    pub const fn decode(code: i64) -> Option<Self> {
        match code {
            0 => Some(Self::KeepFiltering),
            1 => Some(Self::OfflineWithStandby),
            _ => None,
        }
    }
}

/// The grant permissives a [`BackwashCoordinator`] reads: `supply_ok`,
/// `waste_ok`, and `flow_ok` (`In`, `Bool`) — the shared-supply
/// conditions aggregated upstream through plant wiring. A non-`Good`
/// permissive reads as not-OK.
#[derive(Debug, Clone, Copy)]
pub struct PermissiveInputs {
    /// `supply_ok` — the backwash supply source is available.
    pub supply_ok: PointId,
    /// `waste_ok` — the waste path can accept the wash.
    pub waste_ok: PointId,
    /// `flow_ok` — the shared flow capacity is available.
    pub flow_ok: PointId,
}

/// One filter's bound points within a [`BackwashCoordinator`],
/// declared `i` as the filter's 1-based index: `request_i` (`In`,
/// `Bool`) is the armed backwash request from the sequence, `grant_i`
/// (`Out`, `Bool`) the exclusive supply grant, and `position_i`
/// (`Out`, `Int`) the 1-based queue position — `0` while the filter is
/// not queued, the grant holder included (it holds the grant, not a
/// queue slot; `active` identifies it).
#[derive(Debug, Clone, Copy)]
pub struct FilterIo {
    /// `request_i` — the armed backwash request.
    pub request: PointId,
    /// `grant_i` — the exclusive supply grant.
    pub grant: PointId,
    /// `position_i` — the 1-based queue position, `0` while not queued.
    pub position: PointId,
}

/// The bank-level output points a [`BackwashCoordinator`] reports on:
/// `active` (`Out`, `Int`) the 1-based index of the filter holding the
/// grant — `0` while none does — `queued` (`Out`, `Int`) the pending
/// request count, and `resource_blocked` (`Out`, `Bool`) asserted
/// while a request stands first in queue and a grant permissive fails.
#[derive(Debug, Clone, Copy)]
pub struct CoordinatorOutputs {
    /// `active` — the grant holder's 1-based index, `0` while none
    /// holds it.
    pub active: PointId,
    /// `queued` — the count of requests pending in the queue.
    pub queued: PointId,
    /// `resource_blocked` — asserts while a request stands first in
    /// queue and a grant permissive fails.
    pub resource_blocked: PointId,
}

/// The tuned behavior a [`BackwashCoordinator`] runs under — its
/// parameter map's two keys as one value. Both are the decision's
/// recorded customer-validation assumptions carried as declared data,
/// so both are required parameters — never silently defaulted.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BackwashCoordinatorConfig {
    /// The queue ordering policy — the `queue_policy` code.
    pub queue_policy: QueuePolicy,
    /// What a queued filter does while it waits — the `queued_state`
    /// code; declared data whose effect composes in bank wiring.
    pub queued_state: QueuedState,
}

/// The inclusive `Int` bound the `queue_policy` parameter accepts: the
/// declared [`QueuePolicy`] codes.
const QUEUE_POLICY_RANGE: ParameterRange = ParameterRange {
    min: Value::Int(0),
    max: Value::Int(2),
};

/// The inclusive `Int` bound the `queued_state` parameter accepts: the
/// declared [`QueuedState`] codes.
const QUEUED_STATE_RANGE: ParameterRange = ParameterRange {
    min: Value::Int(0),
    max: Value::Int(1),
};

/// A shared-supply backwash coordinator: arbitrates the exclusive
/// grant across `N` filters declared `request_1` … `request_N`,
/// `grant_1` … `grant_N`, `position_1` … `position_N` under the
/// `trip_N` indexed-port convention — the filter count is discovered
/// from the bound port names at construction.
///
/// **Queue and grant.** A filter whose `request_i` reads `true` with
/// `Good` quality is *requesting*; a request that cannot be trusted —
/// a non-`Good` `request_i` — reads as not asserted, so it neither
/// enters nor holds a queue slot, and a granted request going
/// non-`Good` releases the grant. Requesting filters that hold no
/// grant sit in the queue in the declared [`QueuePolicy`] order —
/// assertion order under `Fifo`, ascending filter index under
/// `PriorityByTrigger`, and assertion order under `OperatorManaged`.
/// At most one filter holds the grant at a time; the queue's head
/// takes it the scan no grant stands, every permissive holds, and —
/// under `OperatorManaged` — the standing reorder instruction selects
/// it. The grant holds while the granted filter's `request_i` stands
/// and releases the scan it drops; completion and abort release
/// identically through the request drop, so the arbiter carries no
/// completion vocabulary. The released scan can hand the grant to the
/// next queue head the same scan.
///
/// **Permissive gating.** `grant_i` asserts only while every declared
/// permissive — `supply_ok`, `waste_ok`, `flow_ok` — reads `true` with
/// `Good` quality; a non-`Good` permissive reads as not-OK. The gating
/// holds the asserted output low while the *holding* stands: a grant
/// whose holder keeps its request asserted through a permissive
/// outage re-asserts the scan the permissives restore, without
/// re-queuing. `active` reports the holder for the holding's duration,
/// `0` only while no grant is held. `resource_blocked` asserts while
/// the queue is non-empty and a permissive fails — a request stands
/// first in queue and cannot take the grant.
///
/// **Operator reorder.** Where the model binds `reorder` (`In`,
/// `Int`) — conventionally to a writable internal `In` point so writes
/// ride the journaled receipted command path — the point carries a
/// *standing* instruction: a `Good` value `f` in `1..=N` naming a
/// queued member moves it to the head of the queue, re-applied every
/// scan while the value stands (`0`, out-of-range values, and values
/// naming no queued member are no-ops; a non-`Good` read applies
/// nothing). Under `OperatorManaged` the same standing value is the
/// grant selection — the named member must be at the head for the
/// grant to issue, so an unbound or idle `reorder` point under that
/// policy holds every request ungranted. Under `Fifo` and
/// `PriorityByTrigger` the instruction only reorders — a standing
/// value keeps its member pinned at the head. A plant not exposing
/// reorder leaves the port unbound: the instance declares no `reorder`
/// port at all.
///
/// **Status outputs.** `position_i` reports the 1-based queue slot of
/// a queued request, `0` while the filter is not queued — the grant
/// holder reports `0`, having left the queue; `active` reports the
/// holder's 1-based index, `0` while none; `queued` counts the pending
/// requests. Every output carries [`Quality::Good`]: the values are
/// the coordinator's own computed decision under these rules.
///
/// Declared I/O: `supply_ok`, `waste_ok`, `flow_ok` (`In`, `Bool`);
/// `reorder` (`In`, `Int`) declared only where bound; per filter
/// `request_i` (`In`, `Bool`), `grant_i` (`Out`, `Bool`), `position_i`
/// (`Out`, `Int`); `active` (`Out`, `Int`), `queued` (`Out`, `Int`),
/// `resource_blocked` (`Out`, `Bool`).
///
/// Parameters: `queue_policy` — required `Int` carrying a
/// [`QueuePolicy`] code — and `queued_state` — required `Int` carrying
/// a [`QueuedState`] code. Both tune through `set_parameter`; a
/// `queue_policy` retune leaves the standing queue order and held
/// grant untouched — the new policy applies to subsequent arrivals and
/// grants.
#[derive(Debug)]
pub struct BackwashCoordinator {
    name: String,
    permissives: PermissiveInputs,
    reorder: Option<PointId>,
    filters: Vec<FilterIo>,
    outputs: CoordinatorOutputs,
    config: BackwashCoordinatorConfig,
    /// The ordered queue — 0-based indices of asserted, ungranted
    /// requests in service order. `position_i` reports the 1-based
    /// slot.
    queue: Vec<usize>,
    /// The grant holder — a 0-based index into `filters`. The holding
    /// stands while the holder's `request_i` asserts; the `grant_i`
    /// output additionally gates on the permissives.
    granted: Option<usize>,
}

impl BackwashCoordinator {
    /// The component-kind string the model-driven registry maps onto
    /// this type's constructor.
    pub const KIND: &'static str = "backwash-coordinator";

    /// Builds the component from explicit points and the tuned
    /// configuration, or reports an inconsistent one as a
    /// [`ParameterError`].
    ///
    /// `filters` are the managed filters in declared order —
    /// `filters[i]` is filter `i + 1` — and must not be empty.
    /// `reorder` is `Some` only where the model wires the operator
    /// reorder point; an unwired instance declares no `reorder` port.
    pub fn new(
        name: impl Into<String>,
        permissives: PermissiveInputs,
        reorder: Option<PointId>,
        filters: Vec<FilterIo>,
        outputs: CoordinatorOutputs,
        config: BackwashCoordinatorConfig,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        if filters.is_empty() {
            return Err(params::invalid(
                &name,
                "filters",
                "must declare at least one filter".to_string(),
            ));
        }
        Ok(Self {
            name,
            permissives,
            reorder,
            filters,
            outputs,
            config,
            queue: Vec::new(),
            granted: None,
        })
    }

    /// The coordinator's tuned configuration — the reported parameter
    /// set.
    pub fn config(&self) -> BackwashCoordinatorConfig {
        self.config
    }

    /// Builds the component from a plant-model parameter map, reading
    /// the parameters listed on the type's docs.
    pub fn from_parameters(
        name: impl Into<String>,
        permissives: PermissiveInputs,
        reorder: Option<PointId>,
        filters: Vec<FilterIo>,
        outputs: CoordinatorOutputs,
        parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        let policy_code = params::required_u64(&name, parameters, "queue_policy")?;
        let queue_policy = QueuePolicy::decode(policy_code as i64).ok_or_else(|| {
            params::invalid(
                &name,
                "queue_policy",
                format!(
                    "expected 0 (FIFO), 1 (priority by trigger), or 2 (operator managed), found {policy_code}"
                ),
            )
        })?;
        let state_code = params::required_u64(&name, parameters, "queued_state")?;
        let queued_state = QueuedState::decode(state_code as i64).ok_or_else(|| {
            params::invalid(
                &name,
                "queued_state",
                format!(
                    "expected 0 (keep filtering until granted) or 1 (offline with standby cover), found {state_code}"
                ),
            )
        })?;
        Self::new(
            name,
            permissives,
            reorder,
            filters,
            outputs,
            BackwashCoordinatorConfig {
                queue_policy,
                queued_state,
            },
        )
    }

    /// A bound `reorder` reading `Good` carrying `1..=N` yields the
    /// 0-based index the standing instruction names; anything else —
    /// `0`, out of range, or a non-`Good` read — applies nothing.
    fn reorder_instruction(&self, io: &dyn ComponentIo) -> Result<Option<usize>, StepError> {
        match self.reorder {
            Some(point) => {
                let instruction = io.read_typed::<i64>(point)?;
                Ok(
                    if instruction.quality.is_good()
                        && (1..=self.filters.len() as i64).contains(&instruction.value)
                    {
                        Some(instruction.value as usize - 1)
                    } else {
                        None
                    },
                )
            }
            None => Ok(None),
        }
    }
}

impl Component for BackwashCoordinator {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        let mut requirements = Vec::with_capacity(self.filters.len() * 3 + 7);
        requirements.push(IoRequirement::input::<bool>(
            "supply_ok",
            self.permissives.supply_ok,
        ));
        requirements.push(IoRequirement::input::<bool>(
            "waste_ok",
            self.permissives.waste_ok,
        ));
        requirements.push(IoRequirement::input::<bool>(
            "flow_ok",
            self.permissives.flow_ok,
        ));
        if let Some(reorder) = self.reorder {
            requirements.push(IoRequirement::input::<i64>("reorder", reorder));
        }
        for (index, filter) in self.filters.iter().enumerate() {
            let index = index + 1;
            requirements.push(IoRequirement::input::<bool>(
                format!("request_{index}"),
                filter.request,
            ));
            requirements.push(IoRequirement::output::<bool>(
                format!("grant_{index}"),
                filter.grant,
            ));
            requirements.push(IoRequirement::output::<i64>(
                format!("position_{index}"),
                filter.position,
            ));
        }
        requirements.push(IoRequirement::output::<i64>("active", self.outputs.active));
        requirements.push(IoRequirement::output::<i64>("queued", self.outputs.queued));
        requirements.push(IoRequirement::output::<bool>(
            "resource_blocked",
            self.outputs.resource_blocked,
        ));
        requirements
    }

    fn step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError> {
        let count = self.filters.len();

        // Fail-safe input readings: a request that cannot be trusted
        // reads as not asserted, and a non-Good permissive reads as
        // not-OK.
        let mut requesting = vec![false; count];
        for (index, filter) in self.filters.iter().enumerate() {
            let request = io.read_typed::<bool>(filter.request)?;
            requesting[index] = request.quality.is_good() && request.value;
        }
        let mut permissives_ok = true;
        for point in [
            self.permissives.supply_ok,
            self.permissives.waste_ok,
            self.permissives.flow_ok,
        ] {
            let permissive = io.read_typed::<bool>(point)?;
            permissives_ok &= permissive.quality.is_good() && permissive.value;
        }

        // The held grant releases the scan its request drops.
        if self.granted.is_some_and(|holder| !requesting[holder]) {
            self.granted = None;
        }

        // Membership sync: entries whose request dropped leave; newly
        // asserted requests join in the policy's order — the tail under
        // FIFO and operator-managed, ahead of every higher-indexed
        // member under priority-by-trigger.
        self.queue.retain(|&member| requesting[member]);
        for (index, &asserted) in requesting.iter().enumerate() {
            if asserted && self.granted != Some(index) && !self.queue.contains(&index) {
                match self.config.queue_policy {
                    QueuePolicy::PriorityByTrigger => {
                        let slot = self
                            .queue
                            .iter()
                            .position(|&member| member > index)
                            .unwrap_or(self.queue.len());
                        self.queue.insert(slot, index);
                    }
                    QueuePolicy::Fifo | QueuePolicy::OperatorManaged => {
                        self.queue.push(index);
                    }
                }
            }
        }

        // The standing reorder instruction: a queued member the held
        // value names moves to the head — under operator-managed the
        // same standing value is the grant selection.
        let instruction = self.reorder_instruction(io)?;
        if let Some(target) = instruction.filter(|target| self.queue.contains(target)) {
            self.queue.retain(|&member| member != target);
            self.queue.insert(0, target);
        }

        // The grant issues to the queue's head while no grant stands,
        // every permissive holds, and — under operator management — the
        // standing instruction selects it.
        if self.granted.is_none() && permissives_ok && !self.queue.is_empty() {
            let serve = match self.config.queue_policy {
                QueuePolicy::OperatorManaged => instruction == Some(self.queue[0]),
                QueuePolicy::Fifo | QueuePolicy::PriorityByTrigger => true,
            };
            if serve {
                self.granted = Some(self.queue.remove(0));
            }
        }

        for (index, filter) in self.filters.iter().enumerate() {
            io.write_sample(
                filter.grant,
                Sample::new(
                    Value::Bool(self.granted == Some(index) && permissives_ok),
                    Quality::Good,
                    tick,
                ),
            )?;
            let position = self
                .queue
                .iter()
                .position(|&member| member == index)
                .map_or(0, |position| position as i64 + 1);
            io.write_sample(
                filter.position,
                Sample::new(Value::Int(position), Quality::Good, tick),
            )?;
        }
        io.write_sample(
            self.outputs.active,
            Sample::new(
                Value::Int(self.granted.map_or(0, |holder| holder as i64 + 1)),
                Quality::Good,
                tick,
            ),
        )?;
        io.write_sample(
            self.outputs.queued,
            Sample::new(Value::Int(self.queue.len() as i64), Quality::Good, tick),
        )?;
        io.write_sample(
            self.outputs.resource_blocked,
            Sample::new(
                Value::Bool(!self.queue.is_empty() && !permissives_ok),
                Quality::Good,
                tick,
            ),
        )?;
        Ok(())
    }

    /// Describes the coordinator: the three permissives and each
    /// `request_i` are the measured conditions it acts on, `reorder`
    /// the operator's standing queue instruction, each `grant_i` the
    /// driven exclusive grant, and `position_i`/`active`/`queued`/
    /// `resource_blocked` the reported queue state; the two declared
    /// parameters with their code ranges.
    fn describe(&self) -> ComponentDescriptor {
        let mut roles: Vec<(String, PortRole)> = vec![
            ("supply_ok".to_string(), PortRole::ProcessValue),
            ("waste_ok".to_string(), PortRole::ProcessValue),
            ("flow_ok".to_string(), PortRole::ProcessValue),
        ];
        if self.reorder.is_some() {
            roles.push(("reorder".to_string(), PortRole::Setpoint));
        }
        for index in 1..=self.filters.len() {
            roles.push((format!("request_{index}"), PortRole::ProcessValue));
            roles.push((format!("grant_{index}"), PortRole::Output));
            roles.push((format!("position_{index}"), PortRole::Status));
        }
        roles.extend([
            ("active".to_string(), PortRole::Status),
            ("queued".to_string(), PortRole::Status),
            ("resource_blocked".to_string(), PortRole::Status),
        ]);
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &roles,
            vec![
                describe::parameter("queue_policy", ValueKind::Int, Some(QUEUE_POLICY_RANGE)),
                describe::parameter("queued_state", ValueKind::Int, Some(QUEUED_STATE_RANGE)),
            ],
        )
    }

    /// Tunes the declared parameters at the scan boundary. A
    /// `queue_policy` retune leaves the standing queue order and the
    /// held grant untouched — the new policy applies to subsequent
    /// arrivals and grants.
    fn apply_parameter(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        match parameter {
            "queue_policy" => {
                let code = params::tune_u64(&self.name, parameter, value)?;
                self.config.queue_policy = QueuePolicy::decode(code as i64).ok_or_else(|| {
                    params::invalid_parameter(
                        &self.name,
                        parameter,
                        "expected 0 (FIFO), 1 (priority by trigger), or 2 (operator managed)",
                    )
                })?;
            }
            "queued_state" => {
                let code = params::tune_u64(&self.name, parameter, value)?;
                self.config.queued_state = QueuedState::decode(code as i64).ok_or_else(|| {
                    params::invalid_parameter(
                        &self.name,
                        parameter,
                        "expected 0 (keep filtering until granted) or 1 (offline with standby cover)",
                    )
                })?;
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
        parameters.insert("queue_policy", Value::Int(self.config.queue_policy.code()));
        parameters.insert("queued_state", Value::Int(self.config.queued_state.code()));
        parameters
    }

    /// Captures the ordered queue — `queued_count` plus the `queue_k`
    /// members in service order — the held grant, and the tuned
    /// parameters, so a checkpointed standby inherits the queue order
    /// and the grant and continues the run identically.
    fn capture_state(&self) -> StateMap {
        let mut state = self.report_parameters();
        state.insert(
            "granted",
            Value::Int(self.granted.map_or(0, |holder| holder as i64 + 1)),
        );
        state.insert("queued_count", Value::Int(self.queue.len() as i64));
        for (position, member) in self.queue.iter().enumerate() {
            state.insert(
                format!("queue_{}", position + 1),
                Value::Int(*member as i64 + 1),
            );
        }
        state
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        let count = self.filters.len() as i64;
        // The queue key set is map-driven: read the declared count
        // first so the known-field check covers exactly the carried
        // entries — a `queue_k` beyond the count is `UnknownField`.
        let queued_count = state.require_i64(&self.name, "queued_count")?;
        if !(0..=count).contains(&queued_count) {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "queued_count".to_string(),
                value: Value::Int(queued_count),
            });
        }
        let mut known: Vec<String> = ["queue_policy", "queued_state", "granted", "queued_count"]
            .into_iter()
            .map(str::to_string)
            .collect();
        for position in 1..=queued_count {
            known.push(format!("queue_{position}"));
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
        let policy_code = state.require_i64(&self.name, "queue_policy")?;
        let queue_policy = QueuePolicy::decode(policy_code)
            .ok_or_else(|| invalid("queue_policy", Value::Int(policy_code)))?;
        let state_code = state.require_i64(&self.name, "queued_state")?;
        let queued_state = QueuedState::decode(state_code)
            .ok_or_else(|| invalid("queued_state", Value::Int(state_code)))?;

        let granted = state.require_i64(&self.name, "granted")?;
        if !(0..=count).contains(&granted) {
            return Err(invalid("granted", Value::Int(granted)));
        }

        let mut queue: Vec<usize> = Vec::with_capacity(queued_count as usize);
        for position in 1..=queued_count {
            let field = format!("queue_{position}");
            let member = state.require_i64(&self.name, &field)?;
            if !(1..=count).contains(&member) || queue.contains(&(member as usize - 1)) {
                return Err(invalid(&field, Value::Int(member)));
            }
            queue.push(member as usize - 1);
        }
        // The grant holder has left the queue — a held grant queued as
        // a pending request is not a map this component captures.
        if granted > 0 && queue.contains(&(granted as usize - 1)) {
            return Err(invalid("granted", Value::Int(granted)));
        }

        self.config = BackwashCoordinatorConfig {
            queue_policy,
            queued_state,
        };
        self.granted = if granted == 0 {
            None
        } else {
            Some(granted as usize - 1)
        };
        self.queue = queue;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::TestIo;
    use dcs_core::{Direction, ParameterDescriptor, PortDescriptor, QualityReason};

    const SUPPLY: PointId = PointId(10);
    const WASTE: PointId = PointId(11);
    const FLOW: PointId = PointId(12);
    const REORDER: PointId = PointId(13);
    const ACTIVE: PointId = PointId(14);
    const QUEUED: PointId = PointId(15);
    const BLOCKED: PointId = PointId(16);

    const fn request(index: usize) -> PointId {
        PointId(100 + index as u64)
    }
    const fn grant(index: usize) -> PointId {
        PointId(200 + index as u64)
    }
    const fn position(index: usize) -> PointId {
        PointId(300 + index as u64)
    }

    const PERMISSIVES: PermissiveInputs = PermissiveInputs {
        supply_ok: SUPPLY,
        waste_ok: WASTE,
        flow_ok: FLOW,
    };
    const OUTPUTS: CoordinatorOutputs = CoordinatorOutputs {
        active: ACTIVE,
        queued: QUEUED,
        resource_blocked: BLOCKED,
    };

    fn filters(count: usize) -> Vec<FilterIo> {
        (1..=count)
            .map(|index| FilterIo {
                request: request(index),
                grant: grant(index),
                position: position(index),
            })
            .collect()
    }

    fn config() -> BackwashCoordinatorConfig {
        BackwashCoordinatorConfig {
            queue_policy: QueuePolicy::Fifo,
            queued_state: QueuedState::KeepFiltering,
        }
    }

    /// A `count`-filter coordinator under the given config, with the
    /// `reorder` point bound.
    fn component_of(
        count: usize,
        config: BackwashCoordinatorConfig,
        reorder: Option<PointId>,
    ) -> BackwashCoordinator {
        BackwashCoordinator::new("bwc", PERMISSIVES, reorder, filters(count), OUTPUTS, config)
            .unwrap()
    }

    fn component(count: usize) -> BackwashCoordinator {
        component_of(count, config(), Some(REORDER))
    }

    /// The test rig: every declared point plus `reorder`, all
    /// permissives satisfied and no requests.
    fn io(count: usize) -> TestIo {
        let mut points = vec![
            (
                SUPPLY,
                Direction::In,
                Sample::good(Value::Bool(true), Tick::ZERO),
            ),
            (
                WASTE,
                Direction::In,
                Sample::good(Value::Bool(true), Tick::ZERO),
            ),
            (
                FLOW,
                Direction::In,
                Sample::good(Value::Bool(true), Tick::ZERO),
            ),
            (
                REORDER,
                Direction::In,
                Sample::good(Value::Int(0), Tick::ZERO),
            ),
            (
                ACTIVE,
                Direction::Out,
                Sample::good(Value::Int(0), Tick::ZERO),
            ),
            (
                QUEUED,
                Direction::Out,
                Sample::good(Value::Int(0), Tick::ZERO),
            ),
            (
                BLOCKED,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
        ];
        for index in 1..=count {
            points.extend([
                (
                    request(index),
                    Direction::In,
                    Sample::good(Value::Bool(false), Tick::ZERO),
                ),
                (
                    grant(index),
                    Direction::Out,
                    Sample::good(Value::Bool(false), Tick::ZERO),
                ),
                (
                    position(index),
                    Direction::Out,
                    Sample::good(Value::Int(0), Tick::ZERO),
                ),
            ]);
        }
        TestIo::new(&points)
    }

    fn feed_request(io: &TestIo, index: usize, value: bool, tick: u64) {
        io.feed(request(index), Sample::good(Value::Bool(value), Tick(tick)));
    }

    fn feed_permissive(io: &TestIo, point: PointId, value: bool, tick: u64) {
        io.feed(point, Sample::good(Value::Bool(value), Tick(tick)));
    }

    fn feed_reorder(io: &TestIo, value: i64, tick: u64) {
        io.feed(REORDER, Sample::good(Value::Int(value), Tick(tick)));
    }

    fn granted_to(io: &TestIo, index: usize) -> bool {
        matches!(io.written(grant(index)), Some(sample) if sample.value == Value::Bool(true))
    }

    fn granted_count(io: &TestIo, count: usize) -> usize {
        (1..=count).filter(|&index| granted_to(io, index)).count()
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

    fn scan(coordinator: &mut BackwashCoordinator, io: &TestIo, tick: u64) {
        coordinator.step(io, Tick(tick)).unwrap();
    }

    #[test]
    fn the_first_requester_takes_the_grant_exclusively() {
        let mut coordinator = component(3);
        let io = io(3);
        feed_request(&io, 1, true, 1);
        scan(&mut coordinator, &io, 1);
        assert!(granted_to(&io, 1));
        assert_eq!(granted_count(&io, 3), 1);
        assert_eq!(output_int(&io, ACTIVE), 1);
        assert_eq!(output_int(&io, QUEUED), 0);
        // The grant holder reports position 0 — it holds the grant, not
        // a queue slot.
        assert_eq!(output_int(&io, position(1)), 0);
        assert!(!output_bool(&io, BLOCKED));

        // Later requests queue behind the held grant, never alongside.
        feed_request(&io, 2, true, 2);
        feed_request(&io, 3, true, 2);
        scan(&mut coordinator, &io, 2);
        assert_eq!(granted_count(&io, 3), 1);
        assert_eq!(output_int(&io, ACTIVE), 1);
        assert_eq!(output_int(&io, QUEUED), 2);
        assert_eq!(output_int(&io, position(2)), 1);
        assert_eq!(output_int(&io, position(3)), 2);
    }

    #[test]
    fn fifo_serves_the_queue_in_assertion_order() {
        let mut coordinator = component(3);
        let io = io(3);
        // Requests assert out of index order while filter 1 holds:
        // filter 3 ahead of filter 2.
        feed_request(&io, 1, true, 1);
        scan(&mut coordinator, &io, 1);
        feed_request(&io, 3, true, 2);
        scan(&mut coordinator, &io, 2);
        feed_request(&io, 2, true, 3);
        scan(&mut coordinator, &io, 3);
        assert_eq!(output_int(&io, position(3)), 1);
        assert_eq!(output_int(&io, position(2)), 2);

        // The held request dropping releases the grant and hands it to
        // the queue's head at the same scan.
        feed_request(&io, 1, false, 4);
        scan(&mut coordinator, &io, 4);
        assert!(granted_to(&io, 3));
        assert!(!granted_to(&io, 1));
        assert_eq!(output_int(&io, ACTIVE), 3);
        assert_eq!(output_int(&io, QUEUED), 1);
        assert_eq!(output_int(&io, position(2)), 1);
        assert_eq!(output_int(&io, position(3)), 0);

        feed_request(&io, 3, false, 5);
        scan(&mut coordinator, &io, 5);
        assert!(granted_to(&io, 2));
        assert_eq!(output_int(&io, ACTIVE), 2);

        feed_request(&io, 2, false, 6);
        scan(&mut coordinator, &io, 6);
        assert_eq!(output_int(&io, ACTIVE), 0);
        assert_eq!(output_int(&io, QUEUED), 0);
    }

    #[test]
    fn every_permissive_gates_the_grant() {
        for permissive in [SUPPLY, WASTE, FLOW] {
            let mut coordinator = component(2);
            let io = io(2);
            feed_permissive(&io, permissive, false, 1);
            feed_request(&io, 1, true, 1);
            scan(&mut coordinator, &io, 1);
            // The request stands first in queue while the permissive
            // fails: no grant, and the block flag asserts the same
            // scan.
            assert_eq!(granted_count(&io, 2), 0);
            assert_eq!(output_int(&io, ACTIVE), 0);
            assert_eq!(output_int(&io, QUEUED), 1);
            assert_eq!(output_int(&io, position(1)), 1);
            assert!(output_bool(&io, BLOCKED));

            feed_permissive(&io, permissive, true, 2);
            scan(&mut coordinator, &io, 2);
            assert!(granted_to(&io, 1));
            assert_eq!(output_int(&io, ACTIVE), 1);
            assert!(!output_bool(&io, BLOCKED));
        }
    }

    #[test]
    fn a_non_good_permissive_reads_as_not_ok() {
        let mut coordinator = component(1);
        let io = io(1);
        feed_request(&io, 1, true, 1);
        io.feed(
            WASTE,
            Sample::new(
                Value::Bool(true),
                Quality::Bad(QualityReason::DeviceFault),
                Tick(1),
            ),
        );
        scan(&mut coordinator, &io, 1);
        assert!(!granted_to(&io, 1));
        assert!(output_bool(&io, BLOCKED));
    }

    #[test]
    fn a_non_good_request_cannot_queue_or_hold_the_grant() {
        let mut coordinator = component(2);
        let io = io(2);
        // An untrusted request never enters the queue.
        io.feed(
            request(1),
            Sample::new(
                Value::Bool(true),
                Quality::Uncertain(QualityReason::Stale),
                Tick(1),
            ),
        );
        scan(&mut coordinator, &io, 1);
        assert_eq!(output_int(&io, QUEUED), 0);
        assert_eq!(output_int(&io, ACTIVE), 0);

        // A granted request going non-Good releases the grant that
        // scan — identically to the request dropping.
        feed_request(&io, 1, true, 2);
        feed_request(&io, 2, true, 2);
        scan(&mut coordinator, &io, 2);
        assert!(granted_to(&io, 1));
        io.feed(
            request(1),
            Sample::new(
                Value::Bool(true),
                Quality::Uncertain(QualityReason::Stale),
                Tick(3),
            ),
        );
        scan(&mut coordinator, &io, 3);
        assert!(!granted_to(&io, 1));
        assert!(granted_to(&io, 2));
        assert_eq!(output_int(&io, ACTIVE), 2);
    }

    #[test]
    fn the_held_grant_survives_a_permissive_outage() {
        let mut coordinator = component(2);
        let io = io(2);
        feed_request(&io, 1, true, 1);
        feed_request(&io, 2, true, 1);
        scan(&mut coordinator, &io, 1);
        assert!(granted_to(&io, 1));

        // The permissive outage gates the asserted output while the
        // holding stands: `active` still reports the holder, and the
        // queued request reads as blocked.
        feed_permissive(&io, SUPPLY, false, 2);
        scan(&mut coordinator, &io, 2);
        assert!(!granted_to(&io, 1));
        assert_eq!(output_int(&io, ACTIVE), 1);
        assert!(output_bool(&io, BLOCKED));

        // The holding re-asserts the scan the permissives restore —
        // no re-queue, no reorder.
        feed_permissive(&io, SUPPLY, true, 3);
        scan(&mut coordinator, &io, 3);
        assert!(granted_to(&io, 1));
        assert_eq!(output_int(&io, position(2)), 1);
        assert!(!output_bool(&io, BLOCKED));
    }

    #[test]
    fn priority_by_trigger_orders_the_queue_by_filter_index() {
        let mut coordinator = component_of(
            3,
            BackwashCoordinatorConfig {
                queue_policy: QueuePolicy::PriorityByTrigger,
                ..config()
            },
            Some(REORDER),
        );
        let io = io(3);
        feed_request(&io, 1, true, 1);
        scan(&mut coordinator, &io, 1);
        // Filter 3 asserts ahead of filter 2 — index order puts 2
        // ahead anyway.
        feed_request(&io, 3, true, 2);
        scan(&mut coordinator, &io, 2);
        feed_request(&io, 2, true, 3);
        scan(&mut coordinator, &io, 3);
        assert_eq!(output_int(&io, position(2)), 1);
        assert_eq!(output_int(&io, position(3)), 2);

        feed_request(&io, 1, false, 4);
        scan(&mut coordinator, &io, 4);
        assert!(granted_to(&io, 2));
        feed_request(&io, 2, false, 5);
        scan(&mut coordinator, &io, 5);
        assert!(granted_to(&io, 3));
    }

    #[test]
    fn operator_managed_serves_only_the_standing_selection() {
        let mut coordinator = component_of(
            3,
            BackwashCoordinatorConfig {
                queue_policy: QueuePolicy::OperatorManaged,
                ..config()
            },
            Some(REORDER),
        );
        let io = io(3);
        // Requests queue and report position — but no grant issues
        // while the reorder point stands idle.
        feed_request(&io, 1, true, 1);
        feed_request(&io, 2, true, 1);
        scan(&mut coordinator, &io, 1);
        assert_eq!(granted_count(&io, 3), 0);
        assert_eq!(output_int(&io, QUEUED), 2);
        assert_eq!(output_int(&io, position(1)), 1);
        assert_eq!(output_int(&io, position(2)), 2);
        assert_eq!(output_int(&io, ACTIVE), 0);

        // The operator's standing pick moves to the head and takes the
        // grant.
        feed_reorder(&io, 2, 2);
        scan(&mut coordinator, &io, 2);
        assert!(granted_to(&io, 2));
        assert_eq!(output_int(&io, ACTIVE), 2);
        assert_eq!(output_int(&io, position(1)), 1);

        // The grant releasing on the request drop serves nothing while
        // the standing instruction names no queued member — filter 2's
        // request is down.
        feed_request(&io, 2, false, 3);
        scan(&mut coordinator, &io, 3);
        assert_eq!(granted_count(&io, 3), 0);
        assert_eq!(output_int(&io, ACTIVE), 0);
        assert_eq!(output_int(&io, QUEUED), 1);

        // A fresh selection serves the remaining request.
        feed_reorder(&io, 1, 4);
        scan(&mut coordinator, &io, 4);
        assert!(granted_to(&io, 1));
    }

    #[test]
    fn the_reorder_point_promotes_a_queued_member_to_the_head() {
        let mut coordinator = component(3);
        let io = io(3);
        feed_request(&io, 1, true, 1);
        feed_request(&io, 2, true, 1);
        feed_request(&io, 3, true, 1);
        scan(&mut coordinator, &io, 1);
        assert!(granted_to(&io, 1));
        assert_eq!(output_int(&io, position(2)), 1);
        assert_eq!(output_int(&io, position(3)), 2);

        // The held instruction reorders the queue under FIFO.
        feed_reorder(&io, 3, 2);
        scan(&mut coordinator, &io, 2);
        assert_eq!(output_int(&io, position(3)), 1);
        assert_eq!(output_int(&io, position(2)), 2);

        // On release the promoted member — not the earlier arrival —
        // takes the grant.
        feed_request(&io, 1, false, 3);
        scan(&mut coordinator, &io, 3);
        assert!(granted_to(&io, 3));
        assert_eq!(output_int(&io, ACTIVE), 3);
    }

    #[test]
    fn out_of_range_and_untrusted_reorder_values_apply_nothing() {
        let mut coordinator = component(3);
        let io = io(3);
        feed_request(&io, 1, true, 1);
        feed_request(&io, 2, true, 1);
        feed_request(&io, 3, true, 1);
        scan(&mut coordinator, &io, 1);

        // `0`, a value naming no queued member, an out-of-range value,
        // and a non-Good read all leave the order untouched.
        feed_reorder(&io, 0, 2);
        scan(&mut coordinator, &io, 2);
        feed_reorder(&io, 9, 3);
        scan(&mut coordinator, &io, 3);
        io.feed(
            REORDER,
            Sample::new(
                Value::Int(3),
                Quality::Bad(QualityReason::DeviceFault),
                Tick(4),
            ),
        );
        scan(&mut coordinator, &io, 4);
        assert_eq!(output_int(&io, position(2)), 1);
        assert_eq!(output_int(&io, position(3)), 2);

        // The granted filter is not queued — naming it is a no-op too.
        feed_reorder(&io, 1, 5);
        scan(&mut coordinator, &io, 5);
        assert_eq!(output_int(&io, position(2)), 1);
    }

    #[test]
    fn capture_and_restore_mid_queue_continue_identically() {
        let mut coordinator = component(3);
        let io = io(3);
        feed_request(&io, 1, true, 1);
        feed_request(&io, 3, true, 1);
        scan(&mut coordinator, &io, 1);
        feed_request(&io, 2, true, 2);
        scan(&mut coordinator, &io, 2);
        let state = coordinator.capture_state();
        assert_eq!(state.get("granted"), Some(Value::Int(1)));
        assert_eq!(state.get("queued_count"), Some(Value::Int(2)));
        assert_eq!(state.get("queue_1"), Some(Value::Int(3)));
        assert_eq!(state.get("queue_2"), Some(Value::Int(2)));

        // A standby restores the queue order and the held grant — its
        // own capture reproduces the map — and the run continues
        // identically: filter 1's drop hands the grant to filter 3
        // ahead of filter 2.
        let mut standby = component(3);
        standby.restore_state(&state).unwrap();
        assert_eq!(standby.capture_state(), state);
        feed_request(&io, 1, false, 3);
        scan(&mut standby, &io, 3);
        assert!(granted_to(&io, 3));
        assert!(!granted_to(&io, 1));
        assert_eq!(output_int(&io, ACTIVE), 3);
        assert_eq!(output_int(&io, position(2)), 1);
    }

    #[test]
    fn a_restored_queue_rejects_malformed_maps() {
        let mut coordinator = component(2);
        let io = io(2);
        feed_request(&io, 1, true, 1);
        feed_request(&io, 2, true, 1);
        scan(&mut coordinator, &io, 1);
        let state = coordinator.capture_state();

        // Unknown fields, missing members, out-of-range and duplicate
        // queue entries, and a granted member still queued all reject —
        // and the rejected map changes nothing.
        let mut unknown = state.clone();
        unknown.insert("mystery", Value::Int(1));
        assert!(matches!(
            coordinator.restore_state(&unknown),
            Err(StateError::UnknownField { ref field, .. }) if field == "mystery"
        ));

        let mut missing = StateMap::new();
        missing.insert("queued_count", Value::Int(0));
        assert!(matches!(
            coordinator.restore_state(&missing),
            Err(StateError::MissingField { ref field, .. }) if field == "queue_policy"
        ));

        let mut duplicate = coordinator.capture_state();
        duplicate.insert("queued_count", Value::Int(2));
        duplicate.insert("queue_1", Value::Int(1));
        duplicate.insert("queue_2", Value::Int(1));
        assert!(matches!(
            coordinator.restore_state(&duplicate),
            Err(StateError::InvalidValue { ref field, .. }) if field == "queue_2"
        ));

        let mut held_queued = state.clone();
        held_queued.insert("queued_count", Value::Int(2));
        held_queued.insert("queue_1", Value::Int(1));
        held_queued.insert("queue_2", Value::Int(2));
        assert!(matches!(
            coordinator.restore_state(&held_queued),
            Err(StateError::InvalidValue { ref field, .. }) if field == "granted"
        ));
        // The rejected maps left the live state untouched.
        assert_eq!(coordinator.capture_state(), state);
    }

    #[test]
    fn malformed_parameters_fail_construction_naming_the_parameter() {
        let mut parameters = Parameters::new();
        assert_eq!(
            BackwashCoordinator::from_parameters(
                "bwc",
                PERMISSIVES,
                None,
                filters(2),
                OUTPUTS,
                &parameters,
            )
            .unwrap_err(),
            ParameterError::Missing {
                component: "bwc".to_string(),
                parameter: "queue_policy".to_string(),
            }
        );

        parameters.insert("queue_policy".to_string(), Value::Int(9));
        assert_eq!(
            BackwashCoordinator::from_parameters(
                "bwc",
                PERMISSIVES,
                None,
                filters(2),
                OUTPUTS,
                &parameters,
            )
            .unwrap_err(),
            ParameterError::Invalid {
                component: "bwc".to_string(),
                parameter: "queue_policy".to_string(),
                detail:
                    "expected 0 (FIFO), 1 (priority by trigger), or 2 (operator managed), found 9"
                        .to_string(),
            }
        );

        parameters.insert("queue_policy".to_string(), Value::Int(0));
        assert!(matches!(
            BackwashCoordinator::from_parameters(
                "bwc",
                PERMISSIVES,
                None,
                filters(2),
                OUTPUTS,
                &parameters,
            )
            .unwrap_err(),
            ParameterError::Missing { ref parameter, .. } if parameter == "queued_state"
        ));

        parameters.insert("queued_state".to_string(), Value::Int(7));
        assert!(matches!(
            BackwashCoordinator::from_parameters(
                "bwc",
                PERMISSIVES,
                None,
                filters(2),
                OUTPUTS,
                &parameters,
            )
            .unwrap_err(),
            ParameterError::Invalid { ref parameter, .. } if parameter == "queued_state"
        ));

        // A filterless instance fails construction too.
        parameters.insert("queued_state".to_string(), Value::Int(0));
        assert!(matches!(
            BackwashCoordinator::from_parameters(
                "bwc",
                PERMISSIVES,
                None,
                Vec::new(),
                OUTPUTS,
                &parameters,
            )
            .unwrap_err(),
            ParameterError::Invalid { ref parameter, .. } if parameter == "filters"
        ));
    }

    #[test]
    fn descriptor_reports_the_declared_ports_and_parameters() {
        let coordinator = component(2);
        let descriptor = coordinator.describe();
        assert_eq!(descriptor.kind, BackwashCoordinator::KIND);
        assert_eq!(descriptor.name, "bwc");

        // The ports mirror io_requirements, every one role-hinted.
        let ports: Vec<(String, _, _)> = descriptor
            .ports
            .iter()
            .map(|port| (port.name.clone(), port.direction, port.kind))
            .collect();
        assert_eq!(
            ports,
            coordinator
                .io_requirements()
                .iter()
                .map(|requirement| (
                    requirement.name.clone(),
                    requirement.direction,
                    requirement.kind
                ))
                .collect::<Vec<_>>()
        );
        assert!(descriptor.ports.iter().all(|port| port.role.is_some()));
        assert_eq!(
            descriptor.ports,
            vec![
                PortDescriptor {
                    name: "supply_ok".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::ProcessValue),
                    point: None,
                },
                PortDescriptor {
                    name: "waste_ok".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::ProcessValue),
                    point: None,
                },
                PortDescriptor {
                    name: "flow_ok".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::ProcessValue),
                    point: None,
                },
                PortDescriptor {
                    name: "reorder".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Int,
                    role: Some(PortRole::Setpoint),
                    point: None,
                },
                PortDescriptor {
                    name: "request_1".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::ProcessValue),
                    point: None,
                },
                PortDescriptor {
                    name: "grant_1".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Output),
                    point: None,
                },
                PortDescriptor {
                    name: "position_1".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Int,
                    role: Some(PortRole::Status),
                    point: None,
                },
                PortDescriptor {
                    name: "request_2".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::ProcessValue),
                    point: None,
                },
                PortDescriptor {
                    name: "grant_2".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Output),
                    point: None,
                },
                PortDescriptor {
                    name: "position_2".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Int,
                    role: Some(PortRole::Status),
                    point: None,
                },
                PortDescriptor {
                    name: "active".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Int,
                    role: Some(PortRole::Status),
                    point: None,
                },
                PortDescriptor {
                    name: "queued".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Int,
                    role: Some(PortRole::Status),
                    point: None,
                },
                PortDescriptor {
                    name: "resource_blocked".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Status),
                    point: None,
                },
            ]
        );

        // The descriptor's parameter names are exactly the key set
        // `from_parameters` reads, ranges included.
        assert_eq!(
            descriptor.parameters,
            vec![
                ParameterDescriptor {
                    name: "queue_policy".to_string(),
                    kind: ValueKind::Int,
                    range: Some(QUEUE_POLICY_RANGE),
                },
                ParameterDescriptor {
                    name: "queued_state".to_string(),
                    kind: ValueKind::Int,
                    range: Some(QUEUED_STATE_RANGE),
                },
            ]
        );

        // An unwired `reorder` instance declares no `reorder` port.
        let unwired = component_of(1, config(), None);
        assert!(
            !unwired
                .describe()
                .ports
                .iter()
                .any(|port| port.name == "reorder")
        );
        assert!(
            !unwired
                .io_requirements()
                .iter()
                .any(|requirement| requirement.name == "reorder")
        );
    }

    #[test]
    fn tuning_the_declared_parameters_rejects_bad_codes() {
        let mut coordinator = component(3);
        let io = io(3);

        // Both codes tune within their declared ranges and report back.
        coordinator
            .apply_parameter("queue_policy", Value::Int(1))
            .unwrap();
        coordinator
            .apply_parameter("queued_state", Value::Int(1))
            .unwrap();
        assert_eq!(
            coordinator.config(),
            BackwashCoordinatorConfig {
                queue_policy: QueuePolicy::PriorityByTrigger,
                queued_state: QueuedState::OfflineWithStandby,
            }
        );
        assert_eq!(
            coordinator.report_parameters().get("queue_policy"),
            Some(Value::Int(1))
        );

        // The retuned policy orders subsequent arrivals by index:
        // filter 1 holds the grant, filter 3 queues ahead of filter 2
        // by arrival, and the retuned priority order inserts 2 ahead.
        feed_request(&io, 1, true, 1);
        scan(&mut coordinator, &io, 1);
        feed_request(&io, 3, true, 2);
        scan(&mut coordinator, &io, 2);
        assert_eq!(output_int(&io, position(3)), 1);
        feed_request(&io, 2, true, 3);
        scan(&mut coordinator, &io, 3);
        assert_eq!(output_int(&io, position(2)), 1);
        assert_eq!(output_int(&io, position(3)), 2);

        // Out-of-range codes and undeclared names are refused naming
        // the parameter; the tuned state stands.
        assert!(matches!(
            coordinator.apply_parameter("queue_policy", Value::Int(5)),
            Err(CommandError::InvalidParameter { ref parameter, .. }) if parameter == "queue_policy"
        ));
        assert!(matches!(
            coordinator.apply_parameter("queued_state", Value::Int(2)),
            Err(CommandError::InvalidParameter { ref parameter, .. }) if parameter == "queued_state"
        ));
        assert!(matches!(
            coordinator.apply_parameter("stride", Value::Int(1)),
            Err(CommandError::UnknownParameter { ref parameter, .. }) if parameter == "stride"
        ));
        assert_eq!(
            coordinator.config().queue_policy,
            QueuePolicy::PriorityByTrigger
        );
    }
}
