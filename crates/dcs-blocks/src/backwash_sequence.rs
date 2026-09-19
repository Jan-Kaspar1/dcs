//! Per-filter backwash sequence: the step contract architecture
//! decisions 57–60 record for `WW-CTL-004`
//! (`docs/requirements/water-wastewater.md`,
//! `docs/research/filter-backwash.md`). A dedicated kind beside the
//! general `sequencer` — measured advance, the coordinator grant
//! handshake, trigger attribution, and the fault/abort paths are
//! sequence-domain contract the plain timed table does not carry.

use crate::describe;
use crate::params::{self, ParameterError, Parameters};
use dcs_core::{
    CommandError, ComponentDescriptor, ParameterRange, PointId, PortRole, Quality, Sample,
    StateError, StateMap, Tick, Value, ValueKind,
};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// How a [`BackwashSequence`] step ends — the `step_<n>_advance`
/// parameter's `Int` code.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AdvanceMode {
    /// `0` — timed: the step advances when it has held `step_<n>_ticks`
    /// scans, the `sequencer` rule.
    Timed,
    /// `1` — measured: the step advances when its selected `meas_i`
    /// input satisfies `step_<n>_bound`; `step_<n>_ticks` stands as the
    /// overrun timeout governed by `step_<n>_on_overrun`.
    Measured,
    /// `2` — whichever comes first: the measured bound or
    /// `step_<n>_ticks`, a plain duration bound — no overrun.
    FirstOf,
}

impl AdvanceMode {
    /// The `Int` code `step_<n>_advance` carries.
    pub const fn code(self) -> i64 {
        match self {
            Self::Timed => 0,
            Self::Measured => 1,
            Self::FirstOf => 2,
        }
    }

    /// The mode `code` selects, or `None` when it declares none.
    pub const fn decode(code: i64) -> Option<Self> {
        match code {
            0 => Some(Self::Timed),
            1 => Some(Self::Measured),
            2 => Some(Self::FirstOf),
            _ => None,
        }
    }
}

/// What a measured step does when its `step_<n>_ticks` timeout stands
/// without the bound satisfied — the `step_<n>_on_overrun` code.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OverrunPolicy {
    /// `0` — advance anyway: the timeout ends the step like a timed
    /// advance, `overrun` asserting on the overrun scan.
    Advance,
    /// `1` — hold and raise `overrun`: the step keeps waiting for its
    /// bound, `overrun` asserted while it stands past `step_<n>_ticks`.
    Hold,
}

impl OverrunPolicy {
    /// The `Int` code `step_<n>_on_overrun` carries.
    pub const fn code(self) -> i64 {
        match self {
            Self::Advance => 0,
            Self::Hold => 1,
        }
    }

    /// The policy `code` selects, or `None` when it declares none.
    pub const fn decode(code: i64) -> Option<Self> {
        match code {
            0 => Some(Self::Advance),
            1 => Some(Self::Hold),
            _ => None,
        }
    }
}

/// Whether automatic triggers may start a wash — the `auto_start`
/// parameter's `Int` code (decision 58's jurisdiction-mandated choice).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AutoStart {
    /// `0` — an automatic trigger's armed request asserts `request`
    /// directly.
    Direct,
    /// `1` — an automatic trigger arms and latches `pending`; only
    /// `trig_operator` releases it to `request`.
    Pending,
}

impl AutoStart {
    /// The `Int` code `auto_start` carries.
    pub const fn code(self) -> i64 {
        match self {
            Self::Direct => 0,
            Self::Pending => 1,
        }
    }

    /// The mode `code` selects, or `None` when it declares none.
    pub const fn decode(code: i64) -> Option<Self> {
        match code {
            0 => Some(Self::Direct),
            1 => Some(Self::Pending),
            _ => None,
        }
    }
}

/// What a proven `fault` does mid-sequence — the `on_fault_policy`
/// parameter's `Int` code (decision 60).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FaultPolicy {
    /// `0` — hold at `on_fault_step` pending operator action. The held
    /// fault step keeps `request` armed — the grant stays held, the
    /// documented choice decision 60 leaves to this ticket, so a
    /// drain-to-safe step can still drive shared-supply equipment —
    /// while the table parks without advancing and `active` falls. The
    /// hold ends on the receipted operator vocabulary: `trig_operator`
    /// resumes the table from the held step once `fault` no longer
    /// reads asserted; `abort` diverts to the abort path.
    Hold,
    /// `1` — abort: drive `abort_step`, assert `aborted`, drop
    /// `request` so the grant releases and the filter may re-queue on
    /// a fresh trigger.
    Abort,
}

impl FaultPolicy {
    /// The `Int` code `on_fault_policy` carries.
    pub const fn code(self) -> i64 {
        match self {
            Self::Hold => 0,
            Self::Abort => 1,
        }
    }

    /// The policy `code` selects, or `None` when it declares none.
    pub const fn decode(code: i64) -> Option<Self> {
        match code {
            0 => Some(Self::Hold),
            1 => Some(Self::Abort),
            _ => None,
        }
    }
}

/// One step of a [`BackwashSequence`]'s table.
///
/// `ticks` and `value` are `sequencer`'s entries — the step's scan
/// duration (or overrun timeout under [`AdvanceMode::Measured`]) and
/// the value `out` drives while the step reports. `bound` and `meas`
/// belong to the measured modes — both `Some` when `advance` is
/// [`Measured`](AdvanceMode::Measured) or [`FirstOf`](AdvanceMode::FirstOf),
/// `meas` the 0-based index into the bound `meas_1`…`meas_K` inputs;
/// a timed step may still carry them (declared, validated, unused).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct BackwashStep {
    /// How many scans the step holds — its duration under `Timed`/`FirstOf`,
    /// its overrun timeout under `Measured`. `0` behaves as `1`: a step
    /// always reports for at least the scan it is entered on.
    pub ticks: u64,
    /// The value `out` carries while the step reports.
    pub value: f64,
    /// How the step ends.
    pub advance: AdvanceMode,
    /// The measured bound — the step's selected `meas_i` satisfies it
    /// while it reads `Good` and `>= bound`. Required for the measured
    /// modes.
    pub bound: Option<f64>,
    /// The selected `meas_i` — a 0-based index into the bound
    /// `meas_1`…`meas_K` input set. Required for the measured modes.
    pub meas: Option<usize>,
    /// The measured step's timeout behavior.
    pub on_overrun: OverrunPolicy,
}

/// The declared input points a [`BackwashSequence`] reads: the four
/// trigger primaries, the grant handshake's `grant`, the operator
/// `abort`, the equipment `fault` aggregate, and the indexed
/// `meas_1`…`meas_K` measured inputs in bound order — `meas[i]` is
/// `meas_{i+1}`.
#[derive(Debug, Clone)]
pub struct BackwashSequenceInputs {
    /// `trig_time` — elapsed-run-time trigger.
    pub trig_time: PointId,
    /// `trig_headloss` — terminal-headloss trigger.
    pub trig_headloss: PointId,
    /// `trig_turbidity` — effluent-turbidity trigger.
    pub trig_turbidity: PointId,
    /// `trig_operator` — operator start; bound to a writable internal
    /// `In` point so writes ride the journaled receipted command path.
    pub trig_operator: PointId,
    /// `grant` — the coordinator's exclusive supply grant, the run
    /// permissive.
    pub grant: PointId,
    /// `abort` — operator abort; bound to a writable internal `In`
    /// point like `trig_operator`.
    pub abort: PointId,
    /// `fault` — the per-filter equipment-fault aggregate, composed
    /// upstream through plant wiring.
    pub fault: PointId,
    /// `meas_1`…`meas_K` — the measured inputs `step_<n>_meas` selects
    /// among, in bound order.
    pub meas: Vec<PointId>,
}

/// The declared output points a [`BackwashSequence`] drives: the grant
/// handshake's `request`, the status vocabulary, the position reports,
/// and the generated `phase_1`…`phase_N` flags — `phases[i]` is
/// `phase_{i+1}` and must number exactly the declared steps.
#[derive(Debug, Clone)]
pub struct BackwashSequenceOutputs {
    /// `request` — the armed backwash request, wired to the
    /// coordinator's `request_i`.
    pub request: PointId,
    /// `active` — stepping-under-grant report.
    pub active: PointId,
    /// `pending` — the `auto_start = 1` armed-but-held latch.
    pub pending: PointId,
    /// `done` — the table ran to its end.
    pub done: PointId,
    /// `aborted` — the decision-60 abort paths fired.
    pub aborted: PointId,
    /// `overrun` — a measured step stands past its tick bound.
    pub overrun: PointId,
    /// `trigger_source` — the held attribution code.
    pub trigger_source: PointId,
    /// `step` — the reported position, 1-based.
    pub step: PointId,
    /// `out` — the reported step's `step_<n>_out`.
    pub out: PointId,
    /// `phase_1`…`phase_N` — the per-step equipment flags, one per
    /// declared step in order.
    pub phases: Vec<PointId>,
}

/// The tuned behavior a [`BackwashSequence`] runs under — the
/// scalar parameters as one value. `abort_step` and `on_fault_step`
/// are stored 0-based; the parameter map carries them 1-based.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BackwashSequenceConfig {
    /// Whether automatic triggers may start a wash.
    pub auto_start: AutoStart,
    /// The step `abort` — and `on_fault_policy` `1` — drives to,
    /// 0-based.
    pub abort_step: usize,
    /// The step a proven fault drives to under `on_fault_policy` `0`,
    /// 0-based.
    pub on_fault_step: usize,
    /// What a proven `fault` does mid-sequence.
    pub on_fault_policy: FaultPolicy,
}

/// The inclusive `Int` bound `step_<n>_advance` accepts: the declared
/// [`AdvanceMode`] codes.
const ADVANCE_RANGE: ParameterRange = ParameterRange {
    min: Value::Int(0),
    max: Value::Int(2),
};

/// The inclusive `Int` bound the binary code parameters accept:
/// `auto_start`, `on_fault_policy`, `step_<n>_on_overrun`.
const BINARY_CODE_RANGE: ParameterRange = ParameterRange {
    min: Value::Int(0),
    max: Value::Int(1),
};

/// The four trigger sources, in declaration and precedence order:
/// `trig_time`, `trig_headloss`, `trig_turbidity`, `trig_operator` —
/// `trigger_source` codes 1–4, `0` reporting none.
const TRIGGER_COUNT: usize = 4;

/// A per-filter backwash sequence: walks a declared step table in
/// `sequencer`'s shape — extended with measured advance, the
/// coordinator grant handshake, trigger attribution, and the
/// abort/fault paths — under the `WW-CTL-004` contract decisions
/// 57–60 record.
///
/// All timing is in scans — the executor's virtual tick — never wall
/// clock, per the execution-model decision.
///
/// **Position and phases.** The sequence is always positioned at a
/// step: `step` reports its 1-based index and `out` drives its
/// `step_<n>_out` every scan — a fresh sequence sits on step 1 before
/// any trigger. `phase_<n>` mirrors the position: it asserts while
/// `step` reports `n`, exactly one at a time. Equipment commands
/// compose in bank wiring — a `bool-gate` OR over the phase flags that
/// drive each valve, pump, or blower — and a pattern that must engage
/// only during the wash gates the flags on `request` or `active`
/// there; single-active-step semantics structurally exclude
/// cross-phase pairs such as air scour with high-rate wash.
///
/// **Arming and the grant handshake** (decisions 57–58). The trigger
/// primaries — `trig_time`, `trig_headloss`, `trig_turbidity`,
/// `trig_operator` (`In`, `Bool`) — arm on their *rising edges*: a
/// source still standing when a backwash completes does not re-arm
/// until it releases and re-asserts, so a held level trigger cannot
/// chain washes. The first edge while no run stands arms `request`
/// (`Out`, `Bool`), which holds until the table completes or aborts —
/// the request drop is the grant release, no separate protocol —
/// while `trigger_source` (`Out`, `Int`: `0` none, `1` time,
/// `2` headloss, `3` turbidity, `4` operator) captures the arming
/// edge's source and holds it through the backwash and its `done`/
/// `aborted` stand until the next arming. Simultaneous edges resolve
/// in declaration order — time before headloss before turbidity before
/// operator. Edges while a run is in progress are ignored; a fresh
/// edge while `done` or `aborted` stand starts a new backwash from
/// step 1. Under `auto_start` `0` every edge arms directly — an
/// operator start included; under `auto_start` `1` an automatic edge
/// latches `pending` instead, capturing its source, and only a
/// `trig_operator` edge releases the latch: the request then arms with
/// the *captured* automatic source (the recorded reason the wash was
/// needed), while an operator edge with none pending arms directly as
/// source `4`. `grant` (`In`, `Bool`) is the run permissive: the table
/// advances only while it reads `true` with `Good` quality — a queued
/// filter sits armed on its first step, `request` standing and
/// `active` low — and `active` (`Out`, `Bool`) reports each scan the
/// table stepped under grant, the completing scan included.
///
/// **Step advance** (decision 57). Each stepping scan banks a count in
/// the reported step; the report-then-advance convention is
/// `sequencer`'s — the scan reaching a step's end still reports that
/// step, the next scan belongs to its successor. `step_<n>_advance`
/// `0` holds the step for `step_<n>_ticks` scans (`0` behaving as
/// `1`); `1` advances when the selected `meas_i` reads `Good` and
/// `>= step_<n>_bound`, with `step_<n>_ticks` as the overrun timeout —
/// `step_<n>_on_overrun` `0` advancing anyway at the bound, `1`
/// holding the step past it while `overrun` (`Out`, `Bool`) asserts
/// until the bound is met; `2` advances on whichever comes first, a
/// plain duration bound carrying no overrun. A held `on_overrun` `1`
/// step clamps its banked count at the timeout. The final step's
/// completing scan asserts `done` while still reporting it, drops
/// `request`, and holds there — `done` clears only on a fresh arming.
///
/// **Abort and fault** (decisions 59–60). A `Good` `abort` edge while
/// a run is in progress drives the sequence to `abort_step`, asserts
/// `aborted`, and drops `request` — the grant releases — holding at
/// the abort step until a fresh trigger re-arms; with no run armed it
/// only cancels a latched `pending`. A `Good` `fault` edge while a run
/// is in progress follows `on_fault_policy`: `0` drives to
/// `on_fault_step` and holds — `request` stays armed, so *the held
/// fault step keeps the grant* (this ticket's documented detail: a
/// fault step such as drain-to-safe may still need the shared supply;
/// the supply frees on the operator's abort or a resumed run's
/// completion) — parking the table with `active` low and neither
/// `done` nor `aborted` asserted, until `trig_operator` resumes
/// stepping from the held step once `fault` no longer reads asserted,
/// or `abort` diverts; `1` takes the abort path — `abort_step`,
/// `aborted`, `request` dropped — so the filter may re-queue on a
/// fresh trigger. `aborted` is a distinct status from `done`. Hold,
/// advance, and manual-step are decision 59's recorded open
/// assumptions and carry no inputs here.
///
/// **Quality rule.** A control input that cannot be trusted does not
/// act: a non-`Good` `grant` reads as not granted and holds the table;
/// a non-`Good` `meas_i` cannot satisfy its bound, so the ticks/
/// overrun rule governs; a non-`Good` trigger, `abort`, or `fault`
/// reads as not asserted — and counts as released for edge detection,
/// so a recovering trigger re-edges. Every output carries the merged
/// worst-of qualities of the inputs the scan read — the four triggers,
/// `grant`, `abort`, `fault`, and the reported step's selected
/// `meas_i` while a run in progress sits on a measured step — so an
/// untrusted input marks the reported state untrusted even where the
/// fail-safe reading held the table.
///
/// Declared I/O: `trig_time`, `trig_headloss`, `trig_turbidity`,
/// `trig_operator`, `grant`, `abort`, `fault` (`In`, `Bool`);
/// `meas_1`…`meas_K` (`In`, `Float`) discovered from bound port names
/// under the indexed-family convention; `request`, `active`,
/// `pending`, `done`, `aborted`, `overrun` (`Out`, `Bool`);
/// `trigger_source`, `step` (`Out`, `Int`); `out` (`Out`, `Float`);
/// `phase_1`…`phase_N` (`Out`, `Bool`) generated from `step_count`.
///
/// **Parameters:** `step_count` — required positive `Int` — declares
/// the table length `N`, and each step `n` in `1..=N` declares
/// `step_<n>_ticks` (required non-negative `Int`),
/// `step_<n>_out` (required finite `Float`), `step_<n>_advance`
/// (required [`AdvanceMode`] code), `step_<n>_bound` and
/// `step_<n>_meas` (required for the measured modes — finite `Float`,
/// and `Int` in `1..=K`; accepted and validated on timed steps too),
/// and `step_<n>_on_overrun` (required [`OverrunPolicy`] code); plus
/// `auto_start`, `abort_step`, `on_fault_step` (`Int` in `1..=N`), and
/// `on_fault_policy`. A missing or out-of-domain entry fails
/// construction with a [`ParameterError`] naming it; keys outside the
/// declared set are ignored.
#[derive(Debug)]
pub struct BackwashSequence {
    name: String,
    inputs: BackwashSequenceInputs,
    outputs: BackwashSequenceOutputs,
    steps: Vec<BackwashStep>,
    config: BackwashSequenceConfig,
    /// The reported position's index into `steps` — `step` reports it
    /// 1-based.
    current: usize,
    /// Scans banked in `steps[current]`, clamped at its effective tick
    /// bound.
    elapsed: u64,
    /// The armed request — `request` mirrors it.
    armed: bool,
    /// The `auto_start = 1` armed-but-held latch and the automatic
    /// source it captured (`0` while none).
    pending: bool,
    pending_source: i64,
    /// The arming edge's source, held through the backwash and its
    /// `done`/`aborted` stand until the next arming.
    trigger_source: i64,
    /// Table ran to its end — `done` mirrors it until a fresh arming.
    completed: bool,
    /// An abort path fired — `aborted` mirrors it until a fresh arming.
    aborted: bool,
    /// The `on_fault_policy` `0` fault-step hold: parked at
    /// `on_fault_step`, `request` still armed, pending operator action.
    fault_hold: bool,
    /// Last scan's asserted reading per trigger, for edge detection.
    trigger_prev: [bool; TRIGGER_COUNT],
}

impl BackwashSequence {
    /// The component-kind string the model-driven registry maps onto
    /// this type's constructor.
    pub const KIND: &'static str = "backwash-sequence";

    /// Builds the component from its bound points, step table, and
    /// tuned configuration, or reports an inconsistent one as a
    /// [`ParameterError`].
    ///
    /// `outputs.phases` must number exactly `steps` — one `phase_<n>`
    /// port per declared step — and every measured step's `bound`/`meas`
    /// must be present with `meas` inside `1..=inputs.meas.len()`.
    /// `config.abort_step`/`on_fault_step` are 0-based indices into
    /// `steps`. Violations name `step_count` or the offending
    /// `step_<n>_*`/`abort_step`/`on_fault_step` entry.
    pub fn new(
        name: impl Into<String>,
        inputs: BackwashSequenceInputs,
        outputs: BackwashSequenceOutputs,
        steps: Vec<BackwashStep>,
        config: BackwashSequenceConfig,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        if steps.is_empty() {
            return Err(params::invalid(
                &name,
                "step_count",
                "must declare at least one step".to_string(),
            ));
        }
        if steps.len() > i64::MAX as usize {
            return Err(params::invalid(
                &name,
                "step_count",
                format!("must not exceed i64::MAX, found {}", steps.len()),
            ));
        }
        if outputs.phases.len() != steps.len() {
            return Err(params::invalid(
                &name,
                "step_count",
                format!(
                    "declares {} steps but {} phase ports are bound",
                    steps.len(),
                    outputs.phases.len()
                ),
            ));
        }
        for (index, entry) in steps.iter().enumerate() {
            if entry.ticks > i64::MAX as u64 {
                return Err(params::invalid(
                    &name,
                    &format!("step_{}_ticks", index + 1),
                    format!("must not exceed i64::MAX, found {}", entry.ticks),
                ));
            }
            if !entry.value.is_finite() {
                return Err(params::invalid(
                    &name,
                    &format!("step_{}_out", index + 1),
                    "must be finite".to_string(),
                ));
            }
            let measured = entry.advance != AdvanceMode::Timed;
            if let Some(bound) = entry.bound {
                if !bound.is_finite() {
                    return Err(params::invalid(
                        &name,
                        &format!("step_{}_bound", index + 1),
                        "must be finite".to_string(),
                    ));
                }
            } else if measured {
                return Err(params::invalid(
                    &name,
                    &format!("step_{}_bound", index + 1),
                    "a measured step must declare its bound".to_string(),
                ));
            }
            match entry.meas {
                Some(meas) if meas < inputs.meas.len() => {}
                Some(_) => {
                    return Err(params::invalid(
                        &name,
                        &format!("step_{}_meas", index + 1),
                        format!(
                            "must select a bound meas input in 1..={}",
                            inputs.meas.len()
                        ),
                    ));
                }
                None if measured => {
                    return Err(params::invalid(
                        &name,
                        &format!("step_{}_meas", index + 1),
                        "a measured step must select a meas input".to_string(),
                    ));
                }
                None => {}
            }
        }
        for (parameter, step) in [
            ("abort_step", config.abort_step),
            ("on_fault_step", config.on_fault_step),
        ] {
            if step >= steps.len() {
                return Err(params::invalid(
                    &name,
                    parameter,
                    format!("must name a declared step in 1..={}", steps.len()),
                ));
            }
        }
        Ok(Self {
            name,
            inputs,
            outputs,
            steps,
            config,
            current: 0,
            elapsed: 0,
            armed: false,
            pending: false,
            pending_source: 0,
            trigger_source: 0,
            completed: false,
            aborted: false,
            fault_hold: false,
            trigger_prev: [false; TRIGGER_COUNT],
        })
    }

    /// Builds the component from a plant-model parameter map, reading
    /// the step table and scalars listed on the type's docs.
    pub fn from_parameters(
        name: impl Into<String>,
        inputs: BackwashSequenceInputs,
        outputs: BackwashSequenceOutputs,
        parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        let count = params::required_u64(&name, parameters, "step_count")?;
        if count == 0 {
            return Err(params::invalid(
                &name,
                "step_count",
                "must be at least 1".to_string(),
            ));
        }
        if count > i64::MAX as u64 {
            return Err(params::invalid(
                &name,
                "step_count",
                format!("must not exceed i64::MAX, found {count}"),
            ));
        }
        let measurements = inputs.meas.len();
        let mut steps = Vec::with_capacity(count as usize);
        for index in 1..=count {
            let ticks = params::required_u64(&name, parameters, &format!("step_{index}_ticks"))?;
            if ticks > i64::MAX as u64 {
                return Err(params::invalid(
                    &name,
                    &format!("step_{index}_ticks"),
                    format!("must not exceed i64::MAX, found {ticks}"),
                ));
            }
            let value = params::required_f64(&name, parameters, &format!("step_{index}_out"))?;
            let advance_code =
                params::required_u64(&name, parameters, &format!("step_{index}_advance"))?;
            let advance = AdvanceMode::decode(advance_code as i64).ok_or_else(|| {
                params::invalid(
                    &name,
                    &format!("step_{index}_advance"),
                    format!(
                        "expected 0 (timed), 1 (measured), or 2 (first of), found {advance_code}"
                    ),
                )
            })?;
            // The measured-mode entries are required when `advance`
            // selects them, accepted and validated on timed steps too.
            let bound = params::optional_f64(&name, parameters, &format!("step_{index}_bound"))?;
            if bound.is_none() && advance != AdvanceMode::Timed {
                return Err(ParameterError::Missing {
                    component: name.clone(),
                    parameter: format!("step_{index}_bound"),
                });
            }
            let meas = match params::optional_u64(&name, parameters, &format!("step_{index}_meas"))?
            {
                Some(selection) if selection >= 1 && selection as usize <= measurements => {
                    Some(selection as usize - 1)
                }
                Some(selection) => {
                    return Err(params::invalid(
                        &name,
                        &format!("step_{index}_meas"),
                        format!(
                            "must select a bound meas input in 1..={measurements}, found {selection}"
                        ),
                    ));
                }
                None if advance != AdvanceMode::Timed => {
                    return Err(ParameterError::Missing {
                        component: name.clone(),
                        parameter: format!("step_{index}_meas"),
                    });
                }
                None => None,
            };
            let overrun_code =
                params::required_u64(&name, parameters, &format!("step_{index}_on_overrun"))?;
            let on_overrun = OverrunPolicy::decode(overrun_code as i64).ok_or_else(|| {
                params::invalid(
                    &name,
                    &format!("step_{index}_on_overrun"),
                    format!("expected 0 (advance anyway) or 1 (hold and raise overrun), found {overrun_code}"),
                )
            })?;
            steps.push(BackwashStep {
                ticks,
                value,
                advance,
                bound,
                meas,
                on_overrun,
            });
        }
        let step_index = |key: &str| -> Result<usize, ParameterError> {
            let declared = params::required_u64(&name, parameters, key)?;
            if !(1..=count).contains(&declared) {
                return Err(params::invalid(
                    &name,
                    key,
                    format!("must name a declared step in 1..={count}, found {declared}"),
                ));
            }
            Ok(declared as usize - 1)
        };
        let config = BackwashSequenceConfig {
            auto_start: AutoStart::decode(
                params::required_u64(&name, parameters, "auto_start")? as i64
            )
            .ok_or_else(|| {
                params::invalid(
                    &name,
                    "auto_start",
                    "expected 0 (automatic triggers request directly) or 1 (operator release of pending)".to_string(),
                )
            })?,
            abort_step: step_index("abort_step")?,
            on_fault_step: step_index("on_fault_step")?,
            on_fault_policy: FaultPolicy::decode(
                params::required_u64(&name, parameters, "on_fault_policy")? as i64
            )
            .ok_or_else(|| {
                params::invalid(
                    &name,
                    "on_fault_policy",
                    "expected 0 (hold at the fault step) or 1 (abort)".to_string(),
                )
            })?,
        };
        Self::new(name, inputs, outputs, steps, config)
    }

    /// Arms a fresh backwash: `request` asserts, the position returns
    /// to step 1, the completion/abort/fault stands clear, and
    /// `trigger_source` captures the arming `source` code.
    fn arm(&mut self, source: i64) {
        self.armed = true;
        self.pending = false;
        self.pending_source = 0;
        self.trigger_source = source;
        self.completed = false;
        self.aborted = false;
        self.fault_hold = false;
        self.current = 0;
        self.elapsed = 0;
    }

    /// The abort path — the operator `abort` input and
    /// `on_fault_policy` `1` share it: position at `abort_step`,
    /// `aborted` raised, `request` dropped so the grant releases.
    fn enter_abort(&mut self) {
        self.armed = false;
        self.aborted = true;
        self.completed = false;
        self.fault_hold = false;
        self.pending = false;
        self.pending_source = 0;
        self.current = self.config.abort_step;
        self.elapsed = 0;
    }

    /// The effective tick bound of a step: `ticks` with `0` behaving
    /// as `1`, the `sequencer` rule.
    fn tick_bound(step: &BackwashStep) -> u64 {
        step.ticks.max(1)
    }
}

/// Parses a `step_<n>_<entry>` parameter name into its 1-based step
/// index and which entry it names — `None` for any other parameter.
/// `on_overrun` carries a two-word suffix, so it is matched first.
fn step_entry(parameter: &str) -> Option<(usize, &'static str)> {
    let rest = parameter.strip_prefix("step_")?;
    if let Some(index) = rest.strip_suffix("_on_overrun") {
        return index.parse().ok().map(|index| (index, "on_overrun"));
    }
    let (index, entry) = rest.rsplit_once('_')?;
    let index: usize = index.parse().ok()?;
    match entry {
        "ticks" => Some((index, "ticks")),
        "out" => Some((index, "out")),
        "advance" => Some((index, "advance")),
        "bound" => Some((index, "bound")),
        "meas" => Some((index, "meas")),
        _ => None,
    }
}

impl Component for BackwashSequence {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        let mut requirements =
            Vec::with_capacity(7 + self.inputs.meas.len() + 9 + self.steps.len());
        requirements.push(IoRequirement::input::<bool>(
            "trig_time",
            self.inputs.trig_time,
        ));
        requirements.push(IoRequirement::input::<bool>(
            "trig_headloss",
            self.inputs.trig_headloss,
        ));
        requirements.push(IoRequirement::input::<bool>(
            "trig_turbidity",
            self.inputs.trig_turbidity,
        ));
        requirements.push(IoRequirement::input::<bool>(
            "trig_operator",
            self.inputs.trig_operator,
        ));
        requirements.push(IoRequirement::input::<bool>("grant", self.inputs.grant));
        requirements.push(IoRequirement::input::<bool>("abort", self.inputs.abort));
        requirements.push(IoRequirement::input::<bool>("fault", self.inputs.fault));
        for (index, meas) in self.inputs.meas.iter().enumerate() {
            requirements.push(IoRequirement::input::<f64>(
                format!("meas_{}", index + 1),
                *meas,
            ));
        }
        requirements.push(IoRequirement::output::<bool>(
            "request",
            self.outputs.request,
        ));
        requirements.push(IoRequirement::output::<bool>("active", self.outputs.active));
        requirements.push(IoRequirement::output::<bool>(
            "pending",
            self.outputs.pending,
        ));
        requirements.push(IoRequirement::output::<bool>("done", self.outputs.done));
        requirements.push(IoRequirement::output::<bool>(
            "aborted",
            self.outputs.aborted,
        ));
        requirements.push(IoRequirement::output::<bool>(
            "overrun",
            self.outputs.overrun,
        ));
        requirements.push(IoRequirement::output::<i64>(
            "trigger_source",
            self.outputs.trigger_source,
        ));
        requirements.push(IoRequirement::output::<i64>("step", self.outputs.step));
        requirements.push(IoRequirement::output::<f64>("out", self.outputs.out));
        for (index, phase) in self.outputs.phases.iter().enumerate() {
            requirements.push(IoRequirement::output::<bool>(
                format!("phase_{}", index + 1),
                *phase,
            ));
        }
        requirements
    }

    fn step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError> {
        // Fail-safe input readings: a control input that cannot be
        // trusted reads as not asserted — `grant` included, so an
        // unproven grant holds the table.
        let trigger_points = [
            self.inputs.trig_time,
            self.inputs.trig_headloss,
            self.inputs.trig_turbidity,
            self.inputs.trig_operator,
        ];
        let mut quality = Quality::Good;
        let mut asserted = [false; TRIGGER_COUNT];
        for (index, point) in trigger_points.iter().enumerate() {
            let trigger = io.read_typed::<bool>(*point)?;
            quality = quality.merge(trigger.quality);
            asserted[index] = trigger.quality.is_good() && trigger.value;
        }
        let grant = io.read_typed::<bool>(self.inputs.grant)?;
        quality = quality.merge(grant.quality);
        let granted = grant.quality.is_good() && grant.value;
        let abort = io.read_typed::<bool>(self.inputs.abort)?;
        quality = quality.merge(abort.quality);
        let abort_asserted = abort.quality.is_good() && abort.value;
        let fault = io.read_typed::<bool>(self.inputs.fault)?;
        quality = quality.merge(fault.quality);
        let fault_asserted = fault.quality.is_good() && fault.value;

        // Trigger rising edges; a non-Good read counts as released, so
        // a recovering trigger re-edges.
        let mut edge = [false; TRIGGER_COUNT];
        for index in 0..TRIGGER_COUNT {
            edge[index] = asserted[index] && !self.trigger_prev[index];
            self.trigger_prev[index] = asserted[index];
        }
        let operator_edge = edge[TRIGGER_COUNT - 1];

        if self.armed {
            if abort_asserted {
                // The operator abort dominates everything a run can do.
                self.enter_abort();
            } else if fault_asserted && !self.fault_hold {
                match self.config.on_fault_policy {
                    FaultPolicy::Abort => self.enter_abort(),
                    FaultPolicy::Hold => {
                        self.fault_hold = true;
                        self.current = self.config.on_fault_step;
                        self.elapsed = 0;
                    }
                }
            }
            // The held fault step ends on the operator vocabulary: a
            // `trig_operator` edge with the fault no longer asserted
            // resumes the table from the held step.
            if self.armed && self.fault_hold && operator_edge && !fault_asserted {
                self.fault_hold = false;
            }
        } else if abort_asserted {
            // No run stands to abort: `abort` cancels a latched
            // `pending` and dominates any same-scan trigger edge.
            self.pending = false;
            self.pending_source = 0;
        } else if self.pending {
            if operator_edge {
                // The released latch arms with the captured automatic
                // source — the recorded reason the wash was needed.
                self.arm(self.pending_source);
            }
        } else if edge.iter().any(|asserted| *asserted) {
            match self.config.auto_start {
                AutoStart::Direct => {
                    // Simultaneous edges resolve in declaration order —
                    // the lowest-numbered source wins.
                    let source = 1 + edge.iter().position(|asserted| *asserted).unwrap() as i64;
                    self.arm(source);
                }
                AutoStart::Pending => {
                    if operator_edge {
                        self.arm(TRIGGER_COUNT as i64);
                    } else {
                        self.pending = true;
                        self.pending_source = 1 + edge[..TRIGGER_COUNT - 1]
                            .iter()
                            .position(|asserted| *asserted)
                            .unwrap() as i64;
                    }
                }
            }
        }

        // The reported step: the position the transitions left — the
        // scan an abort or fault lands on already reports its target —
        // before the advance evaluation moves it for the next scan.
        let reported = self.current;
        let reported_step = self.steps[reported];

        // A run in progress on a measured step reads its selected
        // `meas_i` — its bound check and its quality merge alike.
        let mut satisfied = false;
        if self.armed && reported_step.advance != AdvanceMode::Timed {
            let meas = io.read_typed::<f64>(self.inputs.meas[reported_step.meas.unwrap()])?;
            quality = quality.merge(meas.quality);
            satisfied = meas.quality.is_good() && meas.value >= reported_step.bound.unwrap();
        }

        let stepping = self.armed && !self.fault_hold && granted;
        let mut overran = false;
        if stepping {
            self.elapsed += 1;
            let bound = Self::tick_bound(&reported_step);
            let timed_out = self.elapsed >= bound;
            overran = reported_step.advance == AdvanceMode::Measured && timed_out && !satisfied;
            let advance = match reported_step.advance {
                AdvanceMode::Timed => timed_out,
                AdvanceMode::Measured => {
                    satisfied || (timed_out && reported_step.on_overrun == OverrunPolicy::Advance)
                }
                AdvanceMode::FirstOf => satisfied || timed_out,
            };
            if advance {
                if reported == self.steps.len() - 1 {
                    // Hold-at-end: the completing scan still reports the
                    // final step, `done` asserting and `request`
                    // dropping — the grant release. The banked count
                    // clamps at the tick bound it may have reached.
                    self.completed = true;
                    self.armed = false;
                    self.elapsed = self.elapsed.min(bound);
                } else {
                    self.current += 1;
                    self.elapsed = 0;
                }
            } else {
                // A held measured step clamps its banked count at the
                // overrun timeout; every other hold already stands at
                // or below it.
                self.elapsed = self.elapsed.min(bound);
            }
        }

        let phase_position = reported + 1;
        io.write_sample(
            self.outputs.request,
            Sample::new(Value::Bool(self.armed), quality, tick),
        )?;
        io.write_sample(
            self.outputs.active,
            Sample::new(Value::Bool(stepping), quality, tick),
        )?;
        io.write_sample(
            self.outputs.pending,
            Sample::new(Value::Bool(self.pending), quality, tick),
        )?;
        io.write_sample(
            self.outputs.done,
            Sample::new(Value::Bool(self.completed), quality, tick),
        )?;
        io.write_sample(
            self.outputs.aborted,
            Sample::new(Value::Bool(self.aborted), quality, tick),
        )?;
        io.write_sample(
            self.outputs.overrun,
            Sample::new(Value::Bool(overran), quality, tick),
        )?;
        io.write_sample(
            self.outputs.trigger_source,
            Sample::new(Value::Int(self.trigger_source), quality, tick),
        )?;
        io.write_sample(
            self.outputs.step,
            Sample::new(Value::Int(phase_position as i64), quality, tick),
        )?;
        io.write_sample(
            self.outputs.out,
            Sample::new(Value::Float(reported_step.value), quality, tick),
        )?;
        for (index, phase) in self.outputs.phases.iter().enumerate() {
            io.write_sample(
                *phase,
                Sample::new(Value::Bool(index + 1 == phase_position), quality, tick),
            )?;
        }
        Ok(())
    }

    /// Describes the sequence: the trigger primaries, `grant`, `fault`,
    /// and the `meas_i` set are the measured conditions it acts on,
    /// `trig_operator` and `abort` the operator's receipted command
    /// surface, `request`/`out`/`phase_<n>` the driven contract, and the
    /// remaining outputs the reported status; the scalar parameters and
    /// the per-step `step_<n>_*` vocabulary `from_parameters` reads.
    fn describe(&self) -> ComponentDescriptor {
        let count = self.steps.len();
        let step_range = ParameterRange {
            min: Value::Int(1),
            max: Value::Int(count as i64),
        };
        let meas_range = ParameterRange {
            min: Value::Int(1),
            max: Value::Int(self.inputs.meas.len().max(1) as i64),
        };
        let mut roles: Vec<(String, PortRole)> = vec![
            ("trig_time".to_string(), PortRole::ProcessValue),
            ("trig_headloss".to_string(), PortRole::ProcessValue),
            ("trig_turbidity".to_string(), PortRole::ProcessValue),
            ("trig_operator".to_string(), PortRole::Setpoint),
            ("grant".to_string(), PortRole::ProcessValue),
            ("abort".to_string(), PortRole::Setpoint),
            ("fault".to_string(), PortRole::ProcessValue),
        ];
        for index in 1..=self.inputs.meas.len() {
            roles.push((format!("meas_{index}"), PortRole::ProcessValue));
        }
        roles.extend([
            ("request".to_string(), PortRole::Output),
            ("active".to_string(), PortRole::Status),
            ("pending".to_string(), PortRole::Status),
            ("done".to_string(), PortRole::Status),
            ("aborted".to_string(), PortRole::Status),
            ("overrun".to_string(), PortRole::Status),
            ("trigger_source".to_string(), PortRole::Status),
            ("step".to_string(), PortRole::Status),
            ("out".to_string(), PortRole::Output),
        ]);
        for index in 1..=count {
            roles.push((format!("phase_{index}"), PortRole::Output));
        }
        let mut parameters = vec![
            describe::parameter("step_count", ValueKind::Int, Some(describe::POSITIVE_INT)),
            describe::parameter("auto_start", ValueKind::Int, Some(BINARY_CODE_RANGE)),
            describe::parameter("abort_step", ValueKind::Int, Some(step_range)),
            describe::parameter("on_fault_step", ValueKind::Int, Some(step_range)),
            describe::parameter("on_fault_policy", ValueKind::Int, Some(BINARY_CODE_RANGE)),
        ];
        for (index, entry) in self.steps.iter().enumerate() {
            let index = index + 1;
            parameters.push(describe::parameter(
                &format!("step_{index}_ticks"),
                ValueKind::Int,
                Some(describe::NONNEGATIVE_INT),
            ));
            parameters.push(describe::parameter(
                &format!("step_{index}_out"),
                ValueKind::Float,
                Some(describe::FINITE_F64),
            ));
            parameters.push(describe::parameter(
                &format!("step_{index}_advance"),
                ValueKind::Int,
                Some(ADVANCE_RANGE),
            ));
            // The conditional measured entries declare only where the
            // step carries them — the same condition
            // `report_parameters` reports under, so the faceplate and
            // the drift guards read one vocabulary.
            if entry.bound.is_some() {
                parameters.push(describe::parameter(
                    &format!("step_{index}_bound"),
                    ValueKind::Float,
                    Some(describe::FINITE_F64),
                ));
            }
            if entry.meas.is_some() {
                parameters.push(describe::parameter(
                    &format!("step_{index}_meas"),
                    ValueKind::Int,
                    Some(meas_range),
                ));
            }
            parameters.push(describe::parameter(
                &format!("step_{index}_on_overrun"),
                ValueKind::Int,
                Some(BINARY_CODE_RANGE),
            ));
        }
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &roles,
            parameters,
        )
    }

    /// Tunes the declared scalars and per-step entries at the scan
    /// boundary.
    ///
    /// `step_<n>_out` applies on the next scan step `n` reports.
    /// `step_<n>_ticks` retunes the step's duration or overrun timeout;
    /// shrinking the reported step's `ticks` below the banked count
    /// clamps it, so the step completes or re-times-out on the next
    /// stepping scan. `step_<n>_bound` tunes a declared bound — on a
    /// step carrying none it is refused with `InvalidParameter` naming
    /// it. `auto_start`, `on_fault_policy`, `abort_step`, and
    /// `on_fault_step` retune the declared policy — a standing
    /// `pending` latch or fault hold stands under the retune.
    /// `step_count`, `step_<n>_advance`, `step_<n>_meas`, and
    /// `step_<n>_on_overrun` are model structure — the table's shape
    /// and port selection are fixed at construction — and retuning them
    /// is refused with `InvalidParameter` naming them.
    fn apply_parameter(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        match step_entry(parameter) {
            Some((index, entry)) if index >= 1 && index <= self.steps.len() => match entry {
                "ticks" => {
                    let tuned = params::tune_u64(&self.name, parameter, value)?;
                    if tuned > i64::MAX as u64 {
                        return Err(params::invalid_parameter(
                            &self.name,
                            parameter,
                            "must not exceed i64::MAX",
                        ));
                    }
                    self.steps[index - 1].ticks = tuned;
                    if self.current == index - 1 {
                        // Keep the banked count inside the step's
                        // in-flight domain, the `sequencer` rule.
                        self.elapsed = self.elapsed.min(tuned.max(1) - 1);
                    }
                }
                "out" => {
                    let tuned = params::tune_f64(&self.name, parameter, value)?;
                    if !tuned.is_finite() {
                        return Err(params::invalid_parameter(
                            &self.name,
                            parameter,
                            "must be finite",
                        ));
                    }
                    self.steps[index - 1].value = tuned;
                }
                "bound" => {
                    let tuned = params::tune_f64(&self.name, parameter, value)?;
                    if !tuned.is_finite() {
                        return Err(params::invalid_parameter(
                            &self.name,
                            parameter,
                            "must be finite",
                        ));
                    }
                    match self.steps[index - 1].bound {
                        Some(_) => self.steps[index - 1].bound = Some(tuned),
                        None => {
                            return Err(params::invalid_parameter(
                                &self.name,
                                parameter,
                                "the step declares no measured bound",
                            ));
                        }
                    }
                }
                _ => {
                    return Err(params::invalid_parameter(
                        &self.name,
                        parameter,
                        "the step's advance mode, meas selection, and overrun policy are fixed at construction",
                    ));
                }
            },
            Some(_) => return Err(params::unknown_parameter(&self.name, parameter)),
            None => match parameter {
                "auto_start" => {
                    let code = params::tune_u64(&self.name, parameter, value)?;
                    self.config.auto_start =
                        AutoStart::decode(code as i64).ok_or_else(|| {
                            params::invalid_parameter(
                                &self.name,
                                parameter,
                                "expected 0 (automatic triggers request directly) or 1 (operator release of pending)",
                            )
                        })?;
                }
                "on_fault_policy" => {
                    let code = params::tune_u64(&self.name, parameter, value)?;
                    self.config.on_fault_policy =
                        FaultPolicy::decode(code as i64).ok_or_else(|| {
                            params::invalid_parameter(
                                &self.name,
                                parameter,
                                "expected 0 (hold at the fault step) or 1 (abort)",
                            )
                        })?;
                }
                "abort_step" | "on_fault_step" => {
                    let tuned = params::tune_u64(&self.name, parameter, value)?;
                    if !(1..=self.steps.len() as u64).contains(&tuned) {
                        return Err(params::invalid_parameter(
                            &self.name,
                            parameter,
                            "must name a declared step",
                        ));
                    }
                    let step = (tuned - 1) as usize;
                    if parameter == "abort_step" {
                        self.config.abort_step = step;
                    } else {
                        self.config.on_fault_step = step;
                    }
                }
                "step_count" => {
                    return Err(params::invalid_parameter(
                        &self.name,
                        parameter,
                        "the step count is fixed at construction",
                    ));
                }
                _ => return Err(params::unknown_parameter(&self.name, parameter)),
            },
        }
        Ok(())
    }

    /// Reports the declared step table and scalars — the same fields
    /// [`capture_state`](Self::capture_state) checkpoints, so the
    /// faceplate and a tracking standby read one vocabulary. The
    /// conditional `step_<n>_bound`/`step_<n>_meas` report only where
    /// the instance declares them.
    fn report_parameters(&self) -> StateMap {
        let mut parameters = StateMap::new();
        parameters.insert("step_count", Value::Int(self.steps.len() as i64));
        parameters.insert("auto_start", Value::Int(self.config.auto_start.code()));
        parameters.insert("abort_step", Value::Int(self.config.abort_step as i64 + 1));
        parameters.insert(
            "on_fault_step",
            Value::Int(self.config.on_fault_step as i64 + 1),
        );
        parameters.insert(
            "on_fault_policy",
            Value::Int(self.config.on_fault_policy.code()),
        );
        for (index, entry) in self.steps.iter().enumerate() {
            let index = index + 1;
            parameters.insert(
                format!("step_{index}_ticks"),
                Value::Int(entry.ticks as i64),
            );
            parameters.insert(format!("step_{index}_out"), Value::Float(entry.value));
            parameters.insert(
                format!("step_{index}_advance"),
                Value::Int(entry.advance.code()),
            );
            if let Some(bound) = entry.bound {
                parameters.insert(format!("step_{index}_bound"), Value::Float(bound));
            }
            if let Some(meas) = entry.meas {
                parameters.insert(format!("step_{index}_meas"), Value::Int(meas as i64 + 1));
            }
            parameters.insert(
                format!("step_{index}_on_overrun"),
                Value::Int(entry.on_overrun.code()),
            );
        }
        parameters
    }

    /// Captures the position, banked count, armed request and held
    /// trigger, the `pending` latch and its captured source, the
    /// `done`/`aborted`/fault-hold stands, the trigger edge memory, and
    /// the tuned table — so a checkpointed standby continues a held
    /// request mid-sequence, an armed trigger source, or a standing
    /// overrun identically under decision 20's rule.
    fn capture_state(&self) -> StateMap {
        let mut state = self.report_parameters();
        state.insert("step", Value::Int(self.current as i64 + 1));
        state.insert("elapsed", Value::Int(self.elapsed as i64));
        state.insert("request", Value::Bool(self.armed));
        state.insert("pending", Value::Bool(self.pending));
        state.insert("pending_source", Value::Int(self.pending_source));
        state.insert("trigger_source", Value::Int(self.trigger_source));
        state.insert("done", Value::Bool(self.completed));
        state.insert("aborted", Value::Bool(self.aborted));
        state.insert("fault_hold", Value::Bool(self.fault_hold));
        // The standing overrun is run state: a measured step parked at
        // its tick bound unsatisfied.
        state.insert(
            "overrun",
            Value::Bool(
                self.armed
                    && !self.fault_hold
                    && self.steps[self.current].advance == AdvanceMode::Measured
                    && self.elapsed >= Self::tick_bound(&self.steps[self.current]),
            ),
        );
        let held = self
            .trigger_prev
            .iter()
            .enumerate()
            .fold(0i64, |mask, (index, asserted)| {
                mask | ((*asserted as i64) << index)
            });
        state.insert("trigger_held", Value::Int(held));
        state
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        let count = self.steps.len() as i64;
        let mut known: Vec<String> = [
            "step",
            "elapsed",
            "request",
            "pending",
            "pending_source",
            "trigger_source",
            "done",
            "aborted",
            "fault_hold",
            "overrun",
            "trigger_held",
            "step_count",
            "auto_start",
            "abort_step",
            "on_fault_step",
            "on_fault_policy",
        ]
        .into_iter()
        .map(str::to_string)
        .collect();
        for index in 1..=self.steps.len() {
            for entry in ["ticks", "out", "advance", "bound", "meas", "on_overrun"] {
                known.push(format!("step_{index}_{entry}"));
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

        let declared = state.require_i64(&self.name, "step_count")?;
        if declared != count {
            return Err(invalid("step_count", Value::Int(declared)));
        }
        let auto_code = state.require_i64(&self.name, "auto_start")?;
        let auto_start = AutoStart::decode(auto_code)
            .ok_or_else(|| invalid("auto_start", Value::Int(auto_code)))?;
        let abort_code = state.require_i64(&self.name, "abort_step")?;
        if !(1..=count).contains(&abort_code) {
            return Err(invalid("abort_step", Value::Int(abort_code)));
        }
        let fault_step_code = state.require_i64(&self.name, "on_fault_step")?;
        if !(1..=count).contains(&fault_step_code) {
            return Err(invalid("on_fault_step", Value::Int(fault_step_code)));
        }
        let policy_code = state.require_i64(&self.name, "on_fault_policy")?;
        let on_fault_policy = FaultPolicy::decode(policy_code)
            .ok_or_else(|| invalid("on_fault_policy", Value::Int(policy_code)))?;

        // The step table restores like-for-like: tunables (`ticks`,
        // `out`, `bound`) take the checkpoint's values; the structural
        // entries (`advance`, `meas`, `on_overrun`) must equal the
        // constructed table — a checkpoint from another table shape is
        // not this component's state.
        let mut steps = Vec::with_capacity(self.steps.len());
        for index in 1..=self.steps.len() {
            let ticks_field = format!("step_{index}_ticks");
            let ticks = state.require_i64(&self.name, &ticks_field)?;
            if ticks < 0 {
                return Err(invalid(&ticks_field, Value::Int(ticks)));
            }
            let out_field = format!("step_{index}_out");
            let value = state.require_f64(&self.name, &out_field)?;
            if !value.is_finite() {
                return Err(invalid(&out_field, Value::Float(value)));
            }
            let advance_field = format!("step_{index}_advance");
            let advance_code = state.require_i64(&self.name, &advance_field)?;
            let advance = AdvanceMode::decode(advance_code)
                .ok_or_else(|| invalid(&advance_field, Value::Int(advance_code)))?;
            if advance != self.steps[index - 1].advance {
                return Err(invalid(&advance_field, Value::Int(advance_code)));
            }
            let bound_field = format!("step_{index}_bound");
            let bound = match (state.get(&bound_field), self.steps[index - 1].bound) {
                (Some(value), _) => {
                    let bound = f64::try_from(value).map_err(|_| StateError::InvalidValue {
                        element: self.name.clone(),
                        field: bound_field.clone(),
                        value,
                    })?;
                    if !bound.is_finite() {
                        return Err(invalid(&bound_field, Value::Float(bound)));
                    }
                    Some(bound)
                }
                (None, Some(_)) => {
                    return Err(StateError::MissingField {
                        element: self.name.clone(),
                        field: bound_field,
                    });
                }
                (None, None) => None,
            };
            let meas_field = format!("step_{index}_meas");
            let meas = match (state.get(&meas_field), self.steps[index - 1].meas) {
                (Some(value), constructed) => {
                    let selection = i64::try_from(value).map_err(|_| StateError::InvalidValue {
                        element: self.name.clone(),
                        field: meas_field.clone(),
                        value,
                    })?;
                    if !(1..=self.inputs.meas.len() as i64).contains(&selection) {
                        return Err(invalid(&meas_field, Value::Int(selection)));
                    }
                    if constructed != Some(selection as usize - 1) {
                        return Err(invalid(&meas_field, Value::Int(selection)));
                    }
                    Some(selection as usize - 1)
                }
                (None, Some(_)) => {
                    return Err(StateError::MissingField {
                        element: self.name.clone(),
                        field: meas_field,
                    });
                }
                (None, None) => None,
            };
            let overrun_field = format!("step_{index}_on_overrun");
            let overrun_code = state.require_i64(&self.name, &overrun_field)?;
            let on_overrun = OverrunPolicy::decode(overrun_code)
                .ok_or_else(|| invalid(&overrun_field, Value::Int(overrun_code)))?;
            if on_overrun != self.steps[index - 1].on_overrun {
                return Err(invalid(&overrun_field, Value::Int(overrun_code)));
            }
            steps.push(BackwashStep {
                ticks: ticks as u64,
                value,
                advance,
                bound,
                meas,
                on_overrun,
            });
        }

        let step = state.require_i64(&self.name, "step")?;
        if !(1..=count).contains(&step) {
            return Err(invalid("step", Value::Int(step)));
        }
        let current = (step - 1) as usize;
        let elapsed = state.require_i64(&self.name, "elapsed")?;
        // A captured count never stands past the step's effective tick
        // bound — the scan reaching it advances or clamps there.
        if elapsed < 0 || elapsed as u64 > Self::tick_bound(&steps[current]) {
            return Err(invalid("elapsed", Value::Int(elapsed)));
        }
        let armed = state.require_bool(&self.name, "request")?;
        let pending = state.require_bool(&self.name, "pending")?;
        let pending_source = state.require_i64(&self.name, "pending_source")?;
        if !(0..=3).contains(&pending_source) || (pending_source != 0) != pending {
            return Err(invalid("pending_source", Value::Int(pending_source)));
        }
        if pending && (armed || auto_start != AutoStart::Pending) {
            return Err(invalid("pending", Value::Bool(pending)));
        }
        let trigger_source = state.require_i64(&self.name, "trigger_source")?;
        if !(0..=TRIGGER_COUNT as i64).contains(&trigger_source) {
            return Err(invalid("trigger_source", Value::Int(trigger_source)));
        }
        let completed = state.require_bool(&self.name, "done")?;
        if completed && (armed || current != steps.len() - 1) {
            return Err(invalid("done", Value::Bool(completed)));
        }
        let aborted = state.require_bool(&self.name, "aborted")?;
        if aborted && (armed || current != abort_code as usize - 1 || elapsed != 0) {
            return Err(invalid("aborted", Value::Bool(aborted)));
        }
        if armed && completed || armed && aborted {
            return Err(invalid("request", Value::Bool(armed)));
        }
        let fault_hold = state.require_bool(&self.name, "fault_hold")?;
        if fault_hold
            && (!armed
                || on_fault_policy != FaultPolicy::Hold
                || current != fault_step_code as usize - 1)
        {
            return Err(invalid("fault_hold", Value::Bool(fault_hold)));
        }
        // A standing overrun only exists parked at a measured step's
        // tick bound mid-run.
        let overrun = state.require_bool(&self.name, "overrun")?;
        if overrun
            && !(armed
                && !fault_hold
                && steps[current].advance == AdvanceMode::Measured
                && elapsed as u64 >= Self::tick_bound(&steps[current]))
        {
            return Err(invalid("overrun", Value::Bool(overrun)));
        }
        if trigger_source != 0 && !(armed || completed || aborted || pending) {
            return Err(invalid("trigger_source", Value::Int(trigger_source)));
        }
        let held = state.require_i64(&self.name, "trigger_held")?;
        if !(0..(1 << TRIGGER_COUNT)).contains(&held) {
            return Err(invalid("trigger_held", Value::Int(held)));
        }

        self.config = BackwashSequenceConfig {
            auto_start,
            abort_step: abort_code as usize - 1,
            on_fault_step: fault_step_code as usize - 1,
            on_fault_policy,
        };
        self.steps = steps;
        self.current = current;
        self.elapsed = elapsed as u64;
        self.armed = armed;
        self.pending = pending;
        self.pending_source = pending_source;
        self.trigger_source = trigger_source;
        self.completed = completed;
        self.aborted = aborted;
        self.fault_hold = fault_hold;
        self.trigger_prev = [false; TRIGGER_COUNT];
        for index in 0..TRIGGER_COUNT {
            self.trigger_prev[index] = held & (1 << index) != 0;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::TestIo;
    use dcs_core::{Direction, Quality, QualityReason};

    const TRIG_TIME: PointId = PointId(500);
    const TRIG_HEADLOSS: PointId = PointId(501);
    const TRIG_TURBIDITY: PointId = PointId(502);
    const TRIG_OPERATOR: PointId = PointId(503);
    const GRANT: PointId = PointId(504);
    const ABORT: PointId = PointId(505);
    const FAULT: PointId = PointId(506);
    const MEAS_1: PointId = PointId(510);
    const MEAS_2: PointId = PointId(511);
    const REQUEST: PointId = PointId(520);
    const ACTIVE: PointId = PointId(521);
    const PENDING: PointId = PointId(522);
    const DONE: PointId = PointId(523);
    const ABORTED: PointId = PointId(524);
    const OVERRUN: PointId = PointId(525);
    const SOURCE: PointId = PointId(526);
    const STEP: PointId = PointId(527);
    const OUT: PointId = PointId(528);
    const PHASE_1: PointId = PointId(530);
    const PHASE_2: PointId = PointId(531);
    const PHASE_3: PointId = PointId(532);
    const PHASE_4: PointId = PointId(533);

    fn timed(ticks: u64, value: f64) -> BackwashStep {
        BackwashStep {
            ticks,
            value,
            advance: AdvanceMode::Timed,
            bound: None,
            meas: None,
            on_overrun: OverrunPolicy::Advance,
        }
    }

    fn measured(
        value: f64,
        meas: usize,
        bound: f64,
        ticks: u64,
        on_overrun: OverrunPolicy,
    ) -> BackwashStep {
        BackwashStep {
            ticks,
            value,
            advance: AdvanceMode::Measured,
            bound: Some(bound),
            meas: Some(meas),
            on_overrun,
        }
    }

    fn first_of(value: f64, meas: usize, bound: f64, ticks: u64) -> BackwashStep {
        BackwashStep {
            ticks,
            value,
            advance: AdvanceMode::FirstOf,
            bound: Some(bound),
            meas: Some(meas),
            on_overrun: OverrunPolicy::Advance,
        }
    }

    /// The four-step table the tests share: step 1 timed two scans,
    /// step 2 measured on `meas_1` >= 50 with a three-scan overrun
    /// timeout held, step 3 first-of on `meas_2` >= 1 or two scans,
    /// step 4 timed one scan. `abort_step`/`on_fault_step` are step 4.
    fn steps() -> Vec<BackwashStep> {
        vec![
            timed(2, 10.0),
            measured(20.0, 0, 50.0, 3, OverrunPolicy::Hold),
            first_of(30.0, 1, 1.0, 2),
            timed(1, 40.0),
        ]
    }

    fn inputs() -> BackwashSequenceInputs {
        BackwashSequenceInputs {
            trig_time: TRIG_TIME,
            trig_headloss: TRIG_HEADLOSS,
            trig_turbidity: TRIG_TURBIDITY,
            trig_operator: TRIG_OPERATOR,
            grant: GRANT,
            abort: ABORT,
            fault: FAULT,
            meas: vec![MEAS_1, MEAS_2],
        }
    }

    fn outputs() -> BackwashSequenceOutputs {
        BackwashSequenceOutputs {
            request: REQUEST,
            active: ACTIVE,
            pending: PENDING,
            done: DONE,
            aborted: ABORTED,
            overrun: OVERRUN,
            trigger_source: SOURCE,
            step: STEP,
            out: OUT,
            phases: vec![PHASE_1, PHASE_2, PHASE_3, PHASE_4],
        }
    }

    fn config(auto_start: AutoStart, on_fault_policy: FaultPolicy) -> BackwashSequenceConfig {
        BackwashSequenceConfig {
            auto_start,
            abort_step: 3,
            on_fault_step: 3,
            on_fault_policy,
        }
    }

    fn component() -> BackwashSequence {
        BackwashSequence::new(
            "bws",
            inputs(),
            outputs(),
            steps(),
            config(AutoStart::Direct, FaultPolicy::Hold),
        )
        .unwrap()
    }

    fn io() -> TestIo {
        let good = Sample::good(Value::Bool(false), Tick::ZERO);
        TestIo::new(&[
            (TRIG_TIME, Direction::In, good),
            (TRIG_HEADLOSS, Direction::In, good),
            (TRIG_TURBIDITY, Direction::In, good),
            (TRIG_OPERATOR, Direction::In, good),
            (GRANT, Direction::In, good),
            (ABORT, Direction::In, good),
            (FAULT, Direction::In, good),
            (
                MEAS_1,
                Direction::In,
                Sample::good(Value::Float(0.0), Tick::ZERO),
            ),
            (
                MEAS_2,
                Direction::In,
                Sample::good(Value::Float(0.0), Tick::ZERO),
            ),
            (REQUEST, Direction::Out, good),
            (ACTIVE, Direction::Out, good),
            (PENDING, Direction::Out, good),
            (DONE, Direction::Out, good),
            (ABORTED, Direction::Out, good),
            (OVERRUN, Direction::Out, good),
            (
                SOURCE,
                Direction::Out,
                Sample::good(Value::Int(0), Tick::ZERO),
            ),
            (
                STEP,
                Direction::Out,
                Sample::good(Value::Int(0), Tick::ZERO),
            ),
            (
                OUT,
                Direction::Out,
                Sample::good(Value::Float(0.0), Tick::ZERO),
            ),
            (PHASE_1, Direction::Out, good),
            (PHASE_2, Direction::Out, good),
            (PHASE_3, Direction::Out, good),
            (PHASE_4, Direction::Out, good),
        ])
    }

    /// Feeds every control input at `Good` quality and steps once —
    /// measured inputs stay at their last feed.
    fn drive(
        block: &mut BackwashSequence,
        io: &TestIo,
        tick: u64,
        trigs: [bool; 4],
        grant: bool,
        abort: bool,
        fault: bool,
    ) {
        let points = [TRIG_TIME, TRIG_HEADLOSS, TRIG_TURBIDITY, TRIG_OPERATOR];
        for (index, asserted) in trigs.iter().enumerate() {
            io.feed(
                points[index],
                Sample::good(Value::Bool(*asserted), Tick(tick)),
            );
        }
        io.feed(GRANT, Sample::good(Value::Bool(grant), Tick(tick)));
        io.feed(ABORT, Sample::good(Value::Bool(abort), Tick(tick)));
        io.feed(FAULT, Sample::good(Value::Bool(fault), Tick(tick)));
        block.step(io, Tick(tick)).unwrap();
    }

    fn feed(io: &TestIo, point: PointId, value: Value, tick: u64) {
        io.feed(point, Sample::good(value, Tick(tick)));
    }

    fn boolean(io: &TestIo, point: PointId) -> bool {
        io.written(point).unwrap().value == Value::Bool(true)
    }

    fn int(io: &TestIo, point: PointId) -> i64 {
        match io.written(point).unwrap().value {
            Value::Int(value) => value,
            other => panic!("expected Int, found {other:?}"),
        }
    }

    fn out(io: &TestIo) -> f64 {
        match io.written(OUT).unwrap().value {
            Value::Float(value) => value,
            other => panic!("expected Float, found {other:?}"),
        }
    }

    /// The asserted `phase_<n>` indices this scan — exactly one while
    /// the sequence reports a position.
    fn phases(io: &TestIo) -> Vec<usize> {
        [PHASE_1, PHASE_2, PHASE_3, PHASE_4]
            .iter()
            .enumerate()
            .filter(|(_, point)| boolean(io, **point))
            .map(|(index, _)| index + 1)
            .collect()
    }

    #[test]
    fn idle_sequence_sits_on_step_one_without_requesting() {
        let mut block = component();
        let io = io();
        drive(&mut block, &io, 1, [false; 4], false, false, false);
        assert_eq!(int(&io, STEP), 1);
        assert_eq!(out(&io), 10.0);
        assert!(!boolean(&io, REQUEST));
        assert!(!boolean(&io, ACTIVE));
        assert!(!boolean(&io, PENDING));
        assert_eq!(int(&io, SOURCE), 0);
        assert_eq!(phases(&io), vec![1]);
    }

    #[test]
    fn request_arms_on_the_first_trigger_and_holds_while_ungranted() {
        let mut block = component();
        let io = io();

        // The trig_time edge arms the request the same scan; ungranted,
        // the sequence sits on step 1 without banking.
        drive(
            &mut block,
            &io,
            1,
            [true, false, false, false],
            false,
            false,
            false,
        );
        assert!(boolean(&io, REQUEST));
        assert_eq!(int(&io, SOURCE), 1);
        assert!(!boolean(&io, ACTIVE));
        assert_eq!(int(&io, STEP), 1);

        // Held ungranted: the table does not advance and the armed
        // request stands — the queued dwell.
        for tick in 2..=4 {
            drive(&mut block, &io, tick, [false; 4], false, false, false);
            assert!(boolean(&io, REQUEST), "tick={tick}");
            assert_eq!(int(&io, STEP), 1, "tick={tick}");
            assert!(!boolean(&io, ACTIVE), "tick={tick}");
        }

        // The grant stepping the table: two granted scans of step 1's
        // declared duration, then step 2.
        drive(&mut block, &io, 5, [false; 4], true, false, false);
        assert!(boolean(&io, ACTIVE));
        assert_eq!(int(&io, STEP), 1);
        drive(&mut block, &io, 6, [false; 4], true, false, false);
        assert_eq!(int(&io, STEP), 1);
        drive(&mut block, &io, 7, [false; 4], true, false, false);
        assert_eq!(int(&io, STEP), 2);
        assert_eq!(out(&io), 20.0);
        assert_eq!(phases(&io), vec![2]);
    }

    #[test]
    fn grant_falling_mid_step_pauses_the_table() {
        let mut block = component();
        let io = io();
        drive(
            &mut block,
            &io,
            1,
            [true, false, false, false],
            true,
            false,
            false,
        );
        assert_eq!(int(&io, STEP), 1);

        // The grant drops after one banked scan: the count stands and
        // `active` falls while the position holds.
        for tick in 2..=4 {
            drive(&mut block, &io, tick, [false; 4], false, false, false);
            assert_eq!(int(&io, STEP), 1, "tick={tick}");
            assert!(!boolean(&io, ACTIVE), "tick={tick}");
        }
        // Resumed, the step banks its second scan and advances.
        drive(&mut block, &io, 5, [false; 4], true, false, false);
        assert_eq!(int(&io, STEP), 1);
        drive(&mut block, &io, 6, [false; 4], true, false, false);
        assert_eq!(int(&io, STEP), 2);
    }

    #[test]
    fn measured_step_advances_when_its_input_crosses_the_bound() {
        let mut block = component();
        let io = io();
        drive(
            &mut block,
            &io,
            1,
            [true, false, false, false],
            true,
            false,
            false,
        );
        drive(&mut block, &io, 2, [false; 4], true, false, false);
        drive(&mut block, &io, 3, [false; 4], true, false, false);
        assert_eq!(int(&io, STEP), 2);

        // `meas_1` below the bound: the step stands — its ticks are the
        // overrun timeout, not a duration.
        drive(&mut block, &io, 4, [false; 4], true, false, false);
        assert_eq!(int(&io, STEP), 2);
        assert!(!boolean(&io, OVERRUN));

        // The bound reached on a stepping scan still reports step 2 —
        // the next scan is step 3's.
        feed(&io, MEAS_1, Value::Float(52.0), 5);
        drive(&mut block, &io, 5, [false; 4], true, false, false);
        assert_eq!(int(&io, STEP), 2);
        drive(&mut block, &io, 6, [false; 4], true, false, false);
        assert_eq!(int(&io, STEP), 3);
        assert_eq!(out(&io), 30.0);
    }

    #[test]
    fn first_of_advances_on_the_bound_or_the_ticks_whichever_first() {
        // The measured bound first: step 3 advances on the satisfying
        // scan, well before its two-tick duration.
        let mut block = component();
        let io = io();
        drive(
            &mut block,
            &io,
            1,
            [true, false, false, false],
            true,
            false,
            false,
        );
        drive(&mut block, &io, 2, [false; 4], true, false, false);
        drive(&mut block, &io, 3, [false; 4], true, false, false);
        feed(&io, MEAS_1, Value::Float(60.0), 4);
        drive(&mut block, &io, 4, [false; 4], true, false, false);
        drive(&mut block, &io, 5, [false; 4], true, false, false);
        assert_eq!(int(&io, STEP), 3);
        feed(&io, MEAS_2, Value::Float(1.5), 6);
        drive(&mut block, &io, 6, [false; 4], true, false, false);
        assert_eq!(int(&io, STEP), 3);
        drive(&mut block, &io, 7, [false; 4], true, false, false);
        assert_eq!(int(&io, STEP), 4);

        // A fresh run reaching step 3 with the bound never met takes
        // the tick bound instead — no overrun, `2` is a plain duration.
        let mut block = component();
        let fresh = self::io();
        drive(
            &mut block,
            &fresh,
            1,
            [true, false, false, false],
            true,
            false,
            false,
        );
        drive(&mut block, &fresh, 2, [false; 4], true, false, false);
        drive(&mut block, &fresh, 3, [false; 4], true, false, false);
        feed(&fresh, MEAS_1, Value::Float(60.0), 4);
        drive(&mut block, &fresh, 4, [false; 4], true, false, false);
        for tick in 5..=6 {
            drive(&mut block, &fresh, tick, [false; 4], true, false, false);
            assert_eq!(int(&fresh, STEP), 3, "tick={tick}");
            assert!(!boolean(&fresh, OVERRUN), "tick={tick}");
        }
        drive(&mut block, &fresh, 7, [false; 4], true, false, false);
        assert_eq!(int(&fresh, STEP), 4);
    }

    #[test]
    fn measured_overrun_hold_stands_past_the_timeout_until_satisfied() {
        let mut block = component();
        let io = io();
        drive(
            &mut block,
            &io,
            1,
            [true, false, false, false],
            true,
            false,
            false,
        );
        drive(&mut block, &io, 2, [false; 4], true, false, false);
        drive(&mut block, &io, 3, [false; 4], true, false, false);
        assert_eq!(int(&io, STEP), 2);

        // Three stepping scans reach the step's overrun timeout: the
        // third reports step 2 with `overrun` asserting, and the step
        // keeps standing past its bound under the hold policy.
        drive(&mut block, &io, 4, [false; 4], true, false, false);
        assert!(!boolean(&io, OVERRUN));
        drive(&mut block, &io, 5, [false; 4], true, false, false);
        assert!(boolean(&io, OVERRUN));
        for tick in 6..=8 {
            drive(&mut block, &io, tick, [false; 4], true, false, false);
            assert_eq!(int(&io, STEP), 2, "tick={tick}");
            assert!(boolean(&io, OVERRUN), "tick={tick}");
        }

        // The bound met late still advances the step; `overrun` clears.
        feed(&io, MEAS_1, Value::Float(55.0), 9);
        drive(&mut block, &io, 9, [false; 4], true, false, false);
        assert_eq!(int(&io, STEP), 2);
        assert!(!boolean(&io, OVERRUN));
        drive(&mut block, &io, 10, [false; 4], true, false, false);
        assert_eq!(int(&io, STEP), 3);
    }

    #[test]
    fn measured_overrun_advance_anyway_moves_on_at_the_timeout() {
        let mut block = BackwashSequence::new(
            "bws",
            inputs(),
            outputs(),
            vec![
                timed(1, 10.0),
                measured(20.0, 0, 50.0, 2, OverrunPolicy::Advance),
                timed(1, 30.0),
                timed(1, 40.0),
            ],
            config(AutoStart::Direct, FaultPolicy::Hold),
        )
        .unwrap();
        let io = io();

        // Step 1's single scan completes inside the arming scan.
        drive(
            &mut block,
            &io,
            1,
            [true, false, false, false],
            true,
            false,
            false,
        );
        drive(&mut block, &io, 2, [false; 4], true, false, false);
        assert_eq!(int(&io, STEP), 2);
        assert!(!boolean(&io, OVERRUN));

        // The bound never met: the timeout's reaching scan reports the
        // step with `overrun` asserted and advances anyway; the next
        // scan sits on step 3 with `overrun` cleared.
        drive(&mut block, &io, 3, [false; 4], true, false, false);
        assert_eq!(int(&io, STEP), 2);
        assert!(boolean(&io, OVERRUN));
        drive(&mut block, &io, 4, [false; 4], true, false, false);
        assert_eq!(int(&io, STEP), 3);
        assert!(!boolean(&io, OVERRUN));
    }

    #[test]
    fn the_table_runs_to_done_and_request_releases_the_grant() {
        let mut block = component();
        let io = io();
        drive(
            &mut block,
            &io,
            1,
            [true, false, false, false],
            true,
            false,
            false,
        );
        drive(&mut block, &io, 2, [false; 4], true, false, false);
        feed(&io, MEAS_1, Value::Float(60.0), 3);
        drive(&mut block, &io, 3, [false; 4], true, false, false);
        feed(&io, MEAS_2, Value::Float(2.0), 4);
        drive(&mut block, &io, 4, [false; 4], true, false, false);

        // Step 4's completing scan: `done` asserts while still
        // reporting step 4 and `request` drops — the grant release.
        drive(&mut block, &io, 5, [false; 4], true, false, false);
        assert_eq!(int(&io, STEP), 4);
        assert!(boolean(&io, DONE));
        assert!(!boolean(&io, REQUEST));
        assert!(!boolean(&io, ABORTED));

        // `done` holds while no fresh trigger edges; `trigger_source`
        // still reports the arming source.
        drive(&mut block, &io, 6, [false; 4], true, false, false);
        assert!(boolean(&io, DONE));
        assert_eq!(int(&io, SOURCE), 1);

        // A fresh edge re-arms from step 1 — a trigger still standing
        // through the wash does not: only its new edge counts.
        drive(
            &mut block,
            &io,
            8,
            [false, true, false, false],
            false,
            false,
            false,
        );
        assert!(boolean(&io, REQUEST));
        assert!(!boolean(&io, DONE));
        assert_eq!(int(&io, STEP), 1);
        assert_eq!(int(&io, SOURCE), 2);
    }

    #[test]
    fn trigger_attribution_holds_through_the_backwash() {
        for (trigs, source) in [
            ([true, false, false, false], 1),
            ([false, true, false, false], 2),
            ([false, false, true, false], 3),
            ([false, false, false, true], 4),
        ] {
            let mut block = component();
            let io = io();
            drive(&mut block, &io, 1, trigs, true, false, false);
            assert_eq!(int(&io, SOURCE), source, "trigs={trigs:?}");
            assert!(boolean(&io, REQUEST));
            // The source holds while the table steps and the arming
            // input has long released.
            for tick in 2..=3 {
                drive(&mut block, &io, tick, [false; 4], true, false, false);
                assert_eq!(int(&io, SOURCE), source, "trigs={trigs:?} tick={tick}");
            }
        }

        // Simultaneous edges resolve in declaration order — time wins.
        let mut block = component();
        let io = io();
        drive(
            &mut block,
            &io,
            1,
            [true, true, false, true],
            true,
            false,
            false,
        );
        assert_eq!(int(&io, SOURCE), 1);
    }

    #[test]
    fn auto_start_pending_latches_until_the_operator_releases() {
        let mut block = BackwashSequence::new(
            "bws",
            inputs(),
            outputs(),
            steps(),
            config(AutoStart::Pending, FaultPolicy::Hold),
        )
        .unwrap();
        let io = io();

        // An automatic edge latches `pending` instead of requesting.
        drive(
            &mut block,
            &io,
            1,
            [false, false, true, false],
            false,
            false,
            false,
        );
        assert!(boolean(&io, PENDING));
        assert!(!boolean(&io, REQUEST));
        assert_eq!(int(&io, SOURCE), 0);

        // A second automatic edge keeps the first source.
        drive(
            &mut block,
            &io,
            2,
            [true, false, false, false],
            false,
            false,
            false,
        );
        assert!(boolean(&io, PENDING));
        assert!(!boolean(&io, REQUEST));

        // The operator edge releases the latch: the request arms with
        // the captured automatic source — the recorded reason.
        drive(&mut block, &io, 3, [false; 4], false, false, false);
        drive(
            &mut block,
            &io,
            4,
            [false, false, false, true],
            true,
            false,
            false,
        );
        assert!(!boolean(&io, PENDING));
        assert!(boolean(&io, REQUEST));
        assert_eq!(int(&io, SOURCE), 3);

        // An operator edge with nothing pending starts a wash
        // unconditionally, attributed to the operator.
        let mut block = BackwashSequence::new(
            "bws",
            inputs(),
            outputs(),
            steps(),
            config(AutoStart::Pending, FaultPolicy::Hold),
        )
        .unwrap();
        let fresh = self::io();
        drive(
            &mut block,
            &fresh,
            1,
            [false, false, false, true],
            false,
            false,
            false,
        );
        assert!(boolean(&fresh, REQUEST));
        assert!(!boolean(&fresh, PENDING));
        assert_eq!(int(&fresh, SOURCE), 4);
    }

    #[test]
    fn abort_drives_the_declared_step_and_drops_the_request() {
        let mut block = component();
        let io = io();
        drive(
            &mut block,
            &io,
            1,
            [true, false, false, false],
            true,
            false,
            false,
        );
        drive(&mut block, &io, 2, [false; 4], true, false, false);
        assert_eq!(int(&io, STEP), 1);

        // The abort scan already reports `abort_step`, `aborted`
        // raised, `request` dropped — the grant release.
        drive(&mut block, &io, 3, [false; 4], true, true, false);
        assert_eq!(int(&io, STEP), 4);
        assert_eq!(out(&io), 40.0);
        assert_eq!(phases(&io), vec![4]);
        assert!(boolean(&io, ABORTED));
        assert!(!boolean(&io, DONE));
        assert!(!boolean(&io, REQUEST));
        assert!(!boolean(&io, ACTIVE));

        // `aborted` stands until a fresh trigger re-arms a new run.
        drive(&mut block, &io, 4, [false; 4], false, false, false);
        assert!(boolean(&io, ABORTED));
        assert_eq!(int(&io, STEP), 4);
        drive(
            &mut block,
            &io,
            5,
            [false, false, false, true],
            false,
            false,
            false,
        );
        assert!(boolean(&io, REQUEST));
        assert!(!boolean(&io, ABORTED));
        assert_eq!(int(&io, STEP), 1);
        assert_eq!(int(&io, SOURCE), 4);
    }

    #[test]
    fn abort_while_queued_ungranted_releases_the_request() {
        let mut block = component();
        let io = io();
        drive(
            &mut block,
            &io,
            1,
            [true, false, false, false],
            false,
            false,
            false,
        );
        assert!(boolean(&io, REQUEST));
        drive(&mut block, &io, 2, [false; 4], false, true, false);
        assert!(!boolean(&io, REQUEST));
        assert!(boolean(&io, ABORTED));
        assert_eq!(int(&io, STEP), 4);
    }

    #[test]
    fn fault_hold_policy_parks_at_the_fault_step_keeping_the_grant() {
        let mut block = component();
        let io = io();
        drive(
            &mut block,
            &io,
            1,
            [true, false, false, false],
            true,
            false,
            false,
        );
        drive(&mut block, &io, 2, [false; 4], true, false, false);
        drive(&mut block, &io, 3, [false; 4], true, false, false);
        assert_eq!(int(&io, STEP), 2);

        // A proven fault drives `on_fault_step` and holds — `request`
        // stands so the grant keeps, `active` falls, neither `done`
        // nor `aborted` asserts.
        drive(&mut block, &io, 4, [false; 4], true, false, true);
        assert_eq!(int(&io, STEP), 4);
        assert!(boolean(&io, REQUEST));
        assert!(!boolean(&io, ACTIVE));
        assert!(!boolean(&io, ABORTED));
        assert!(!boolean(&io, DONE));

        // The table parks at the held step while the fault stands — an
        // operator edge alone does not resume it.
        drive(&mut block, &io, 5, [false; 4], true, false, true);
        drive(
            &mut block,
            &io,
            6,
            [false, false, false, true],
            true,
            false,
            true,
        );
        assert_eq!(int(&io, STEP), 4);
        assert!(boolean(&io, REQUEST));
        assert!(!boolean(&io, ACTIVE));

        // The fault cleared, the receipted operator edge resumes the
        // table from the held step — step 4's single scan completes the
        // run on the next stepping scan.
        drive(&mut block, &io, 7, [false; 4], true, false, false);
        drive(
            &mut block,
            &io,
            8,
            [false, false, false, true],
            true,
            false,
            false,
        );
        assert_eq!(int(&io, STEP), 4);
        drive(&mut block, &io, 9, [false; 4], true, false, false);
        assert!(boolean(&io, DONE));
        assert!(!boolean(&io, REQUEST));
        assert!(!boolean(&io, ABORTED));
    }

    #[test]
    fn fault_abort_policy_takes_the_abort_path() {
        let mut block = BackwashSequence::new(
            "bws",
            inputs(),
            outputs(),
            steps(),
            config(AutoStart::Direct, FaultPolicy::Abort),
        )
        .unwrap();
        let io = io();
        drive(
            &mut block,
            &io,
            1,
            [true, false, false, false],
            true,
            false,
            false,
        );
        drive(&mut block, &io, 2, [false; 4], true, false, false);

        // A proven fault under policy 1 drives `abort_step`, asserts
        // `aborted`, and drops `request` — distinct from `done`.
        drive(&mut block, &io, 3, [false; 4], true, false, true);
        assert_eq!(int(&io, STEP), 4);
        assert!(boolean(&io, ABORTED));
        assert!(!boolean(&io, DONE));
        assert!(!boolean(&io, REQUEST));

        // A fresh trigger re-queues the filter — a new run from step 1.
        drive(&mut block, &io, 4, [false; 4], true, false, false);
        drive(
            &mut block,
            &io,
            5,
            [false, true, false, false],
            false,
            false,
            false,
        );
        assert!(boolean(&io, REQUEST));
        assert!(!boolean(&io, ABORTED));
        assert_eq!(int(&io, STEP), 1);
    }

    #[test]
    fn non_good_inputs_take_the_fail_safe_reading() {
        let mut block = component();
        let io = io();

        // A non-Good trigger cannot arm.
        io.feed(
            TRIG_TIME,
            Sample::new(
                Value::Bool(true),
                Quality::Bad(QualityReason::CommunicationFault),
                Tick(1),
            ),
        );
        block.step(&io, Tick(1)).unwrap();
        assert!(!boolean(&io, REQUEST));
        assert_eq!(
            io.written(REQUEST).unwrap().quality,
            Quality::Bad(QualityReason::CommunicationFault)
        );

        drive(
            &mut block,
            &io,
            2,
            [true, false, false, false],
            true,
            false,
            false,
        );
        drive(&mut block, &io, 3, [false; 4], true, false, false);
        drive(&mut block, &io, 4, [false; 4], true, false, false);
        assert_eq!(int(&io, STEP), 2);

        // A non-Good `meas_i` cannot satisfy its bound: the measured
        // step stands on the ticks/overrun rule and the merged quality
        // marks the outputs.
        feed(&io, MEAS_1, Value::Float(60.0), 5);
        io.feed(
            MEAS_1,
            Sample::new(
                Value::Float(60.0),
                Quality::Uncertain(QualityReason::Stale),
                Tick(5),
            ),
        );
        drive(&mut block, &io, 5, [false; 4], true, false, false);
        assert_eq!(int(&io, STEP), 2);
        assert_eq!(
            io.written(STEP).unwrap().quality,
            Quality::Uncertain(QualityReason::Stale)
        );
        // Past the overrun timeout it still holds under the hold policy.
        for tick in 6..=8 {
            io.feed(
                MEAS_1,
                Sample::new(
                    Value::Float(60.0),
                    Quality::Uncertain(QualityReason::Stale),
                    Tick(tick),
                ),
            );
            drive(&mut block, &io, tick, [false; 4], true, false, false);
        }
        assert_eq!(int(&io, STEP), 2);
        assert!(boolean(&io, OVERRUN));

        // A non-Good `grant` holds the table mid-step even with the
        // measured bound satisfied — feed it directly rather than
        // through `drive`, which always presents `grant` Good.
        feed(&io, MEAS_1, Value::Float(60.0), 9);
        for point in [
            TRIG_TIME,
            TRIG_HEADLOSS,
            TRIG_TURBIDITY,
            TRIG_OPERATOR,
            ABORT,
            FAULT,
        ] {
            io.feed(point, Sample::good(Value::Bool(false), Tick(9)));
        }
        io.feed(
            GRANT,
            Sample::new(
                Value::Bool(true),
                Quality::Bad(QualityReason::CommunicationFault),
                Tick(9),
            ),
        );
        block.step(&io, Tick(9)).unwrap();
        assert_eq!(int(&io, STEP), 2);
        assert!(!boolean(&io, ACTIVE));
        assert_eq!(
            io.written(REQUEST).unwrap().quality,
            Quality::Bad(QualityReason::CommunicationFault)
        );

        // A recovered grant steps again — the satisfied bound advances
        // on the stepping scan, the next scan reports the successor.
        drive(&mut block, &io, 10, [false; 4], true, false, false);
        assert_eq!(int(&io, STEP), 2);
        drive(&mut block, &io, 11, [false; 4], true, false, false);
        assert_eq!(int(&io, STEP), 3);
    }

    #[test]
    fn capture_restore_mid_sequence_continues_identically() {
        let mut block = component();
        let io = io();

        // Arm on turbidity, step into the measured step, and stand it
        // in overrun — the capture carries the held request, the armed
        // source, the banked count, and the overrun stand.
        drive(
            &mut block,
            &io,
            1,
            [false, false, true, false],
            true,
            false,
            false,
        );
        drive(&mut block, &io, 2, [false; 4], true, false, false);
        drive(&mut block, &io, 3, [false; 4], true, false, false);
        for tick in 4..=7 {
            drive(&mut block, &io, tick, [false; 4], true, false, false);
        }
        assert_eq!(int(&io, STEP), 2);
        assert!(boolean(&io, OVERRUN));
        let state = block.capture_state();
        assert_eq!(state.get("request"), Some(Value::Bool(true)));
        assert_eq!(state.get("trigger_source"), Some(Value::Int(3)));
        assert_eq!(state.get("overrun"), Some(Value::Bool(true)));
        assert_eq!(state.get("step"), Some(Value::Int(2)));
        assert_eq!(state.get("elapsed"), Some(Value::Int(3)));

        // A standby restores and continues identically: the bound met
        // late advances, the table completes, `request` drops.
        let mut standby = component();
        standby.restore_state(&state).unwrap();
        assert_eq!(standby.capture_state(), state);
        feed(&io, MEAS_1, Value::Float(75.0), 8);
        drive(&mut standby, &io, 8, [false; 4], true, false, false);
        assert_eq!(int(&io, STEP), 2);
        drive(&mut standby, &io, 9, [false; 4], true, false, false);
        assert_eq!(int(&io, STEP), 3);
    }

    #[test]
    fn capture_restore_carries_the_pending_latch_and_fault_hold() {
        // The `auto_start = 1` latch: source captured, pending held.
        let mut pending_block = BackwashSequence::new(
            "bws",
            inputs(),
            outputs(),
            steps(),
            config(AutoStart::Pending, FaultPolicy::Hold),
        )
        .unwrap();
        let io = io();
        drive(
            &mut pending_block,
            &io,
            1,
            [false, true, false, false],
            false,
            false,
            false,
        );
        let state = pending_block.capture_state();
        assert_eq!(state.get("pending"), Some(Value::Bool(true)));
        assert_eq!(state.get("pending_source"), Some(Value::Int(2)));
        let mut standby = BackwashSequence::new(
            "bws",
            inputs(),
            outputs(),
            steps(),
            config(AutoStart::Pending, FaultPolicy::Hold),
        )
        .unwrap();
        standby.restore_state(&state).unwrap();
        drive(
            &mut standby,
            &io,
            2,
            [false, false, false, true],
            false,
            false,
            false,
        );
        assert!(boolean(&io, REQUEST));
        assert_eq!(int(&io, SOURCE), 2);

        // The fault-hold stand: parked at `on_fault_step`, request kept.
        let mut block = component();
        let fresh = self::io();
        drive(
            &mut block,
            &fresh,
            1,
            [true, false, false, false],
            true,
            false,
            false,
        );
        drive(&mut block, &fresh, 2, [false; 4], true, false, true);
        let state = block.capture_state();
        assert_eq!(state.get("fault_hold"), Some(Value::Bool(true)));
        assert_eq!(state.get("step"), Some(Value::Int(4)));
        let mut standby = component();
        standby.restore_state(&state).unwrap();
        drive(&mut standby, &fresh, 3, [false; 4], true, false, false);
        assert_eq!(int(&fresh, STEP), 4);
        assert!(boolean(&fresh, REQUEST));
        assert!(!boolean(&fresh, ACTIVE));
    }

    #[test]
    fn incompatible_state_is_a_named_error() {
        let mut block = component();
        let mut state = block.capture_state();
        state.insert("step", Value::Int(5));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::InvalidValue { ref field, .. }) if field == "step"
        ));

        let mut state = block.capture_state();
        // `done` standing away from the table's end.
        state.insert("done", Value::Bool(true));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::InvalidValue { ref field, .. }) if field == "done"
        ));

        let mut state = block.capture_state();
        // A pending latch under `auto_start = 0`.
        state.insert("pending", Value::Bool(true));
        state.insert("pending_source", Value::Int(1));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::InvalidValue { ref field, .. }) if field == "pending"
        ));

        let mut state = block.capture_state();
        // A structural entry from another table shape.
        state.insert("step_2_advance", Value::Int(0));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::InvalidValue { ref field, .. }) if field == "step_2_advance"
        ));

        let mut state = block.capture_state();
        state.insert("step_5_ticks", Value::Int(1));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::UnknownField { ref field, .. }) if field == "step_5_ticks"
        ));
        assert!(matches!(
            block.restore_state(&StateMap::new()),
            Err(StateError::MissingField { ref field, .. }) if field == "step_count"
        ));
    }

    #[test]
    fn malformed_tables_and_parameters_fail_naming_the_parameter() {
        let parameters: Parameters = [
            ("step_count".to_string(), Value::Int(4)),
            ("auto_start".to_string(), Value::Int(0)),
            ("abort_step".to_string(), Value::Int(4)),
            ("on_fault_step".to_string(), Value::Int(4)),
            ("on_fault_policy".to_string(), Value::Int(0)),
            ("step_1_ticks".to_string(), Value::Int(2)),
            ("step_1_out".to_string(), Value::Float(10.0)),
            ("step_1_advance".to_string(), Value::Int(0)),
            ("step_1_on_overrun".to_string(), Value::Int(0)),
            ("step_2_ticks".to_string(), Value::Int(3)),
            ("step_2_out".to_string(), Value::Float(20.0)),
            ("step_2_advance".to_string(), Value::Int(1)),
            ("step_2_bound".to_string(), Value::Float(50.0)),
            ("step_2_meas".to_string(), Value::Int(1)),
            ("step_2_on_overrun".to_string(), Value::Int(1)),
            ("step_3_ticks".to_string(), Value::Int(2)),
            ("step_3_out".to_string(), Value::Float(30.0)),
            ("step_3_advance".to_string(), Value::Int(2)),
            ("step_3_bound".to_string(), Value::Float(1.0)),
            ("step_3_meas".to_string(), Value::Int(2)),
            ("step_3_on_overrun".to_string(), Value::Int(0)),
            ("step_4_ticks".to_string(), Value::Int(1)),
            ("step_4_out".to_string(), Value::Float(40.0)),
            ("step_4_advance".to_string(), Value::Int(0)),
            ("step_4_on_overrun".to_string(), Value::Int(0)),
        ]
        .into_iter()
        .collect();
        let build = |parameters: &Parameters| {
            BackwashSequence::from_parameters("bws", inputs(), outputs(), parameters)
        };
        assert_eq!(build(&parameters).unwrap().steps.len(), 4);

        for (key, value) in [
            ("step_count", Value::Int(0)),
            ("auto_start", Value::Int(2)),
            ("abort_step", Value::Int(5)),
            ("on_fault_step", Value::Int(0)),
            ("on_fault_policy", Value::Int(2)),
            ("step_1_ticks", Value::Int(-1)),
            ("step_1_out", Value::Float(f64::NAN)),
            ("step_2_advance", Value::Int(3)),
            ("step_2_bound", Value::Float(f64::INFINITY)),
            ("step_2_meas", Value::Int(3)),
            ("step_2_on_overrun", Value::Int(2)),
        ] {
            let mut broken = parameters.clone();
            broken.insert(key.to_string(), value);
            assert!(
                matches!(
                    build(&broken).unwrap_err(),
                    ParameterError::Invalid { ref parameter, .. } if parameter == key
                ),
                "{key} should fail Invalid naming itself"
            );
        }
        for key in [
            "step_count",
            "auto_start",
            "abort_step",
            "on_fault_policy",
            "step_1_ticks",
            "step_1_out",
            "step_1_advance",
            "step_1_on_overrun",
            "step_2_bound",
            "step_2_meas",
        ] {
            let mut broken = parameters.clone();
            broken.remove(key);
            assert!(
                matches!(
                    build(&broken).unwrap_err(),
                    ParameterError::Missing { ref parameter, .. } if parameter == key
                ),
                "{key} should fail Missing naming itself"
            );
        }
        // `bound`/`meas` are not required on a timed step: a table of
        // timed steps builds without them.
        let mut timed_table = parameters.clone();
        timed_table.insert("step_2_advance".to_string(), Value::Int(0));
        timed_table.remove("step_2_bound");
        timed_table.remove("step_2_meas");
        assert!(build(&timed_table).is_ok());
    }

    #[test]
    fn tuning_applies_at_the_scan_boundary() {
        let mut block = component();
        let io = io();
        drive(
            &mut block,
            &io,
            1,
            [true, false, false, false],
            true,
            false,
            false,
        );

        // The reported step's `out` retune lands on the next scan.
        block
            .apply_parameter("step_1_out", Value::Float(15.0))
            .unwrap();
        drive(&mut block, &io, 2, [false; 4], true, false, false);
        assert_eq!(out(&io), 15.0);

        // A measured bound retunes mid-run.
        block
            .apply_parameter("step_2_bound", Value::Float(10.0))
            .unwrap();
        drive(&mut block, &io, 3, [false; 4], true, false, false);
        assert_eq!(int(&io, STEP), 2);
        feed(&io, MEAS_1, Value::Float(12.0), 4);
        drive(&mut block, &io, 4, [false; 4], true, false, false);
        drive(&mut block, &io, 5, [false; 4], true, false, false);
        assert_eq!(int(&io, STEP), 3);

        // Structural entries and `step_count` are construction-fixed;
        // unknown and out-of-range names reject.
        for parameter in [
            "step_count",
            "step_2_advance",
            "step_2_meas",
            "step_2_on_overrun",
        ] {
            assert!(matches!(
                block.apply_parameter(parameter, Value::Int(0)),
                Err(CommandError::InvalidParameter {
                    parameter: ref p,
                    ..
                }) if p == parameter
            ));
        }
        assert!(matches!(
            block.apply_parameter("step_9_ticks", Value::Int(1)),
            Err(CommandError::UnknownParameter { ref parameter, .. }) if parameter == "step_9_ticks"
        ));
        assert!(matches!(
            block.apply_parameter("dwell", Value::Int(1)),
            Err(CommandError::UnknownParameter { ref parameter, .. }) if parameter == "dwell"
        ));
        assert!(matches!(
            block.apply_parameter("abort_step", Value::Int(9)),
            Err(CommandError::InvalidParameter { ref parameter, .. }) if parameter == "abort_step"
        ));

        // The tuned table is part of the captured state a standby gets.
        let state = block.capture_state();
        assert_eq!(state.get("step_1_out"), Some(Value::Float(15.0)));
        assert_eq!(state.get("step_2_bound"), Some(Value::Float(10.0)));
    }

    #[test]
    fn describes_itself() {
        let descriptor = component().describe();
        assert_eq!(descriptor.name, "bws");
        assert_eq!(descriptor.kind, BackwashSequence::KIND);
        assert_eq!(descriptor.ports.len(), 7 + 2 + 9 + 4);
        let port = |name: &str| {
            descriptor
                .ports
                .iter()
                .find(|port| port.name == name)
                .unwrap()
        };
        assert_eq!(port("trig_time").direction, Direction::In);
        assert_eq!(port("meas_2").kind, ValueKind::Float);
        assert_eq!(port("request").role, Some(PortRole::Output));
        assert_eq!(port("abort").role, Some(PortRole::Setpoint));
        assert_eq!(port("trigger_source").kind, ValueKind::Int);
        assert_eq!(port("phase_4").direction, Direction::Out);

        // The declared parameter vocabulary mirrors the
        // `from_parameters` key set: the five scalars plus the
        // per-step entries — four on the timed steps, six where the
        // measured modes add `bound`/`meas`.
        let names: Vec<&str> = descriptor
            .parameters
            .iter()
            .map(|parameter| parameter.name.as_str())
            .collect();
        assert_eq!(
            names[..5],
            [
                "step_count",
                "auto_start",
                "abort_step",
                "on_fault_step",
                "on_fault_policy"
            ]
        );
        assert_eq!(names.len(), 5 + 4 + 6 + 6 + 4);
        assert!(names.contains(&"step_2_bound"));
        assert!(!names.contains(&"step_1_bound"));
        assert!(names.contains(&"step_4_on_overrun"));
    }

    #[test]
    fn identical_runs_produce_identical_outputs() {
        // A deterministic scripted exercise: arm, grant mid-way, stand
        // the measured step into overrun, satisfy it, complete.
        let run = || {
            let mut block = component();
            let io = io();
            let mut trace = Vec::new();
            for tick in 1..=14u64 {
                let trigs = if tick == 1 {
                    [true, false, false, false]
                } else {
                    [false; 4]
                };
                let grant = tick != 3 && tick != 4;
                if tick == 9 {
                    feed(&io, MEAS_1, Value::Float(60.0), tick);
                }
                if tick == 11 {
                    feed(&io, MEAS_2, Value::Float(2.0), tick);
                }
                drive(&mut block, &io, tick, trigs, grant, false, false);
                trace.push((
                    io.written(REQUEST).unwrap(),
                    io.written(STEP).unwrap(),
                    io.written(OUT).unwrap(),
                    io.written(OVERRUN).unwrap(),
                    io.written(DONE).unwrap(),
                ));
            }
            trace
        };
        assert_eq!(run(), run());
    }
}
