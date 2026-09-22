//! The managed-alarm surface architecture decisions 71–73 record,
//! shared verbatim by `managed-latching-alarm` and
//! `managed-bool-latching-alarm`: the optional `shelve`/`oos`/`suppress`
//! inputs, the uniform `shelved`/`suppressed`/`out_of_service` status
//! outputs, the `max_shelve_ticks` bound and the decision-70
//! rationalization parameters, and the per-scan managed-state machine
//! both kinds' documented contracts delegate to.
//!
//! The contract in one place:
//!
//! - `shelve` (`In`, `Bool`), bound to a writable internal point, is
//!   level-observed: `true` requests shelving, `false` is
//!   the manual unshelve. `shelved` asserts on the request's first
//!   scan and holds for `max_shelve_ticks` scans — the asserting scan
//!   counts as the first — then expiry drops it at the bound even while
//!   the request stands, so a re-shelve requires the request to cycle
//!   through `false`. `max_shelve_ticks = 0` declares the alarm
//!   never-shelvable: a bound request is inert beneath it. The stronger
//!   rejections are the model's: an unbound `shelve` port exposes no
//!   shelving surface at all, and a bound-but-unwritable point answers
//!   `NotWritable` at command submission.
//! - `oos` (`In`, `Bool`) is level-observed in both directions:
//!   `true` takes the alarm out of service, `false` returns it — manual
//!   only, no automatic return.
//! - `suppress` (`In`, `Bool`) is the declared designed-suppression
//!   wiring: while it reads `true` the kind asserts `suppressed` and
//!   withholds annunciation — the `unacknowledged` latch is held clear
//!   — while `alarm` keeps reporting process truth. On release the
//!   alarm evaluates fresh, so a condition that outlasted its
//!   suppression arrives as a new `unacknowledged` transition rather
//!   than a stale standing flag.
//! - The three managed flags are orthogonal and combine: `shelved` and
//!   `out_of_service` never touch the two-flag evaluation — a trip
//!   mid-shelve or mid-OOS latches normally, an `ack` mid-shelve clears
//!   the latch normally — while `suppress` alone withholds the latch.
//! - Each managed flag output carries its driving input's quality —
//!   `Good` for an unbound input — so a degraded request marks the flag
//!   it drives. `alarm`/`unacknowledged` keep the sibling kinds'
//!   worst-of-`in`-and-`ack` merge.

use crate::describe;
use crate::params::{self, ParameterError, Parameters};
use dcs_core::{
    CommandError, ParameterDescriptor, PointId, PortRole, Quality, Sample, StateError, StateMap,
    Tick, Value, ValueKind,
};
use dcs_runtime::{ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// The full port binding of a managed alarm kind — the sibling's
/// `in`/`ack`/`alarm`/`unacknowledged` vocabulary exactly, plus the
/// optional managed inputs and the three mandatory status outputs.
///
/// `shelve`, `oos`, and `suppress` are `None` where the model leaves
/// the port unbound — the model selects each alarm's managed surface by
/// which points it binds and marks writable: an unbound `shelve`
/// exposes no shelving surface, an unbound `oos` never takes the alarm
/// out of service, and an unbound `suppress` never suppresses. The
/// status outputs are always declared — decision 71's uniform
/// vocabulary — reporting `false` for a state the model never wired.
#[derive(Debug, Clone, Copy)]
pub struct ManagedAlarmIo {
    /// `in` (`In`, `Float` or `Bool` by kind): the alarm condition.
    pub input: PointId,
    /// `ack` (`In`, `Bool`): the operator's clearing command — a
    /// writable internal point, consumed on its rising edge like the
    /// siblings': a held level acknowledges once and cannot
    /// pre-acknowledge a later trip.
    pub ack: PointId,
    /// `shelve` (`In`, `Bool`): the level-observed shelve request —
    /// `true` requests shelving, `false` is the manual return.
    pub shelve: Option<PointId>,
    /// `oos` (`In`, `Bool`): the level-observed out-of-service command —
    /// manual in both directions; there is no automatic return.
    pub oos: Option<PointId>,
    /// `suppress` (`In`, `Bool`): the designed-suppression condition —
    /// declared wiring, decision 73's named seam.
    pub suppress: Option<PointId>,
    /// `alarm` (`Out`, `Bool`): the reported standing trip state —
    /// process truth under every managed flag.
    pub alarm: PointId,
    /// `unacknowledged` (`Out`, `Bool`): the trip-until-acknowledged
    /// latch — the annunciation demand `suppress` withholds.
    pub unacknowledged: PointId,
    /// `shelved` (`Out`, `Bool`): asserted while a shelve request stands
    /// inside the declared bound.
    pub shelved: PointId,
    /// `suppressed` (`Out`, `Bool`): asserted while `suppress` reads
    /// `true`.
    pub suppressed: PointId,
    /// `out_of_service` (`Out`, `Bool`): asserted while `oos` reads
    /// `true`.
    pub out_of_service: PointId,
}

impl ManagedAlarmIo {
    /// The bound managed inputs' requirements, in `shelve`/`oos`/
    /// `suppress` order — an unbound input declares nothing.
    pub(crate) fn input_requirements(&self) -> Vec<IoRequirement> {
        [
            ("shelve", self.shelve),
            ("oos", self.oos),
            ("suppress", self.suppress),
        ]
        .into_iter()
        .filter_map(|(name, point)| point.map(|point| IoRequirement::input::<bool>(name, point)))
        .collect()
    }

    /// The three managed status outputs' requirements, in declared
    /// order — always all three.
    pub(crate) fn output_requirements(&self) -> Vec<IoRequirement> {
        vec![
            IoRequirement::output::<bool>("shelved", self.shelved),
            IoRequirement::output::<bool>("suppressed", self.suppressed),
            IoRequirement::output::<bool>("out_of_service", self.out_of_service),
        ]
    }

    /// The `Status` role hints the managed ports carry into the
    /// descriptor: every bound managed input and all three outputs.
    /// Unbound inputs name no port, so they take no hint — matching
    /// `describe::component`'s declared-ports rule.
    pub(crate) fn roles(&self) -> Vec<(&'static str, PortRole)> {
        let mut roles = Vec::with_capacity(6);
        for (name, bound) in [
            ("shelve", self.shelve),
            ("oos", self.oos),
            ("suppress", self.suppress),
        ] {
            if bound.is_some() {
                roles.push((name, PortRole::Status));
            }
        }
        roles.extend([
            ("shelved", PortRole::Status),
            ("suppressed", PortRole::Status),
            ("out_of_service", PortRole::Status),
        ]);
        roles
    }
}

/// The parameters every managed alarm kind declares: the shelving bound
/// decision 72 records plus the decision-70 rationalization record
/// fields — all required non-negative `Int` data, the same set every
/// alarm kind carries so the rationalization enforcement lands on the
/// managed kinds too. The site vocabulary the codes name — priority
/// levels, class rules — is decision 70's open customer assumption.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ManagedAlarmConfig {
    /// `max_shelve_ticks` — the pre-assigned maximum shelving time in
    /// scans; `0` declares the alarm never-shelvable.
    pub max_shelve_ticks: u64,
    /// `priority` — the site priority code.
    pub priority: u64,
    /// `class` — the administrative class code.
    pub class: u64,
    /// `response_ticks` — the allowable operator response time in scans.
    pub response_ticks: u64,
}

impl ManagedAlarmConfig {
    /// The declared parameter set — the descriptors `describe` reports,
    /// in this order, and the order the `dcs-build` specs mirror.
    pub(crate) fn parameters() -> Vec<ParameterDescriptor> {
        vec![
            describe::parameter(
                "max_shelve_ticks",
                ValueKind::Int,
                Some(describe::NONNEGATIVE_INT),
            ),
            describe::parameter("priority", ValueKind::Int, Some(describe::NONNEGATIVE_INT)),
            describe::parameter("class", ValueKind::Int, Some(describe::NONNEGATIVE_INT)),
            describe::parameter(
                "response_ticks",
                ValueKind::Int,
                Some(describe::NONNEGATIVE_INT),
            ),
        ]
    }

    /// Reads the four required non-negative `Int` parameters, a missing
    /// or invalid key failing as a [`ParameterError`] naming it.
    pub(crate) fn from_parameters(
        component: &str,
        parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        Ok(Self {
            max_shelve_ticks: params::required_u64(component, parameters, "max_shelve_ticks")?,
            priority: params::required_u64(component, parameters, "priority")?,
            class: params::required_u64(component, parameters, "class")?,
            response_ticks: params::required_u64(component, parameters, "response_ticks")?,
        })
    }

    /// Retunes one declared parameter, returning the updated config or
    /// a [`CommandError`] naming `component`; an undeclared name is
    /// `UnknownParameter`. A `max_shelve_ticks` retune applies to a
    /// standing shelve on the next scan — the flag is derived against
    /// the current bound, so retuning to `0` drops it immediately.
    pub(crate) fn tune(
        self,
        component: &str,
        parameter: &str,
        value: Value,
    ) -> Result<Self, CommandError> {
        let tuned = params::tune_u64(component, parameter, value)?;
        let mut config = self;
        match parameter {
            "max_shelve_ticks" => config.max_shelve_ticks = tuned,
            "priority" => config.priority = tuned,
            "class" => config.class = tuned,
            "response_ticks" => config.response_ticks = tuned,
            _ => return Err(params::unknown_parameter(component, parameter)),
        }
        Ok(config)
    }

    /// Reports the four parameters into `map` — report and checkpoint
    /// share the one vocabulary.
    pub(crate) fn report(&self, map: &mut StateMap) {
        map.insert("max_shelve_ticks", Value::Int(self.max_shelve_ticks as i64));
        map.insert("priority", Value::Int(self.priority as i64));
        map.insert("class", Value::Int(self.class as i64));
        map.insert("response_ticks", Value::Int(self.response_ticks as i64));
    }

    /// The restore-path mirror of [`from_parameters`](Self::from_parameters):
    /// reads the four `Int` fields, a negative value failing
    /// `InvalidValue` so a rejected checkpoint changes nothing.
    pub(crate) fn restore(component: &str, state: &StateMap) -> Result<Self, StateError> {
        let read = |field: &str| -> Result<u64, StateError> {
            let value = state.require_i64(component, field)?;
            if value < 0 {
                return Err(StateError::InvalidValue {
                    element: component.to_string(),
                    field: field.to_string(),
                    value: Value::Int(value),
                });
            }
            Ok(value as u64)
        };
        Ok(Self {
            max_shelve_ticks: read("max_shelve_ticks")?,
            priority: read("priority")?,
            class: read("class")?,
            response_ticks: read("response_ticks")?,
        })
    }
}

/// The managed-state machine's run state — every value the machine
/// carries between scans, checkpointed under decision 20's rule so a
/// tracking standby inherits the shelve countdown, the suppression
/// baseline, and the out-of-service state.
#[derive(Debug, Clone, Copy, Default)]
pub(crate) struct ManagedState {
    /// Consecutive scans the `shelve` request has stood — the
    /// shelve-expiry timer; `0` while no request stands. `shelved`
    /// asserts while the count sits within `max_shelve_ticks`: the
    /// request's asserting scan counts as the first shelved scan, the
    /// flag drops the scan the count passes the bound even while the
    /// request stands, and a standing request never re-arms — a
    /// re-shelve after expiry requires the request to cycle.
    pub shelve_elapsed: u64,
    /// Whether suppression held on the last scan — the baseline the
    /// kinds reset their trip-edge detection against, so a release
    /// evaluates the alarm fresh.
    pub suppressed: bool,
    /// The last observed `oos` level — the out-of-service state,
    /// level-observed with no automatic return.
    pub out_of_service: bool,
}

/// One scan's managed-state observation: the three flag values and the
/// quality each flag output carries — its driving input's, `Good` for
/// an unbound input.
pub(crate) struct ManagedScan {
    /// `shelved` — a shelve request standing inside the bound.
    pub shelved: bool,
    /// `suppressed` — the suppress level this scan.
    pub suppressed: bool,
    /// The suppress level the *previous* scan observed — the release
    /// edge the kinds reset their fresh-trip baseline against, so a
    /// condition that outlasted its suppression arrives as a new
    /// `unacknowledged` transition.
    pub was_suppressed: bool,
    /// `out_of_service` — the oos level this scan.
    pub out_of_service: bool,
    /// The `shelved` output's quality.
    pub shelved_quality: Quality,
    /// The `suppressed` output's quality.
    pub suppressed_quality: Quality,
    /// The `out_of_service` output's quality.
    pub out_of_service_quality: Quality,
}

impl ManagedState {
    /// Reads the bound managed inputs — an unbound input reads as a
    /// `Good` `false` — advances the machine one scan, and writes the
    /// three flag outputs. The returned observation lets the caller
    /// apply suppression's latch-withholding rule.
    pub(crate) fn step(
        &mut self,
        io: &dyn ComponentIo,
        ports: &ManagedAlarmIo,
        max_shelve_ticks: u64,
        tick: Tick,
    ) -> Result<ManagedScan, StepError> {
        let shelve = ports
            .shelve
            .map(|point| io.read_typed::<bool>(point))
            .transpose()?;
        let oos = ports
            .oos
            .map(|point| io.read_typed::<bool>(point))
            .transpose()?;
        let suppress = ports
            .suppress
            .map(|point| io.read_typed::<bool>(point))
            .transpose()?;

        let request = shelve.map(|sample| sample.value).unwrap_or(false);
        self.shelve_elapsed = if request {
            self.shelve_elapsed.saturating_add(1)
        } else {
            0
        };
        let shelved = request && self.shelve_elapsed <= max_shelve_ticks;
        self.out_of_service = oos.map(|sample| sample.value).unwrap_or(false);
        let was_suppressed = self.suppressed;
        self.suppressed = suppress.map(|sample| sample.value).unwrap_or(false);

        let observation = ManagedScan {
            shelved,
            suppressed: self.suppressed,
            was_suppressed,
            out_of_service: self.out_of_service,
            shelved_quality: shelve.map(|sample| sample.quality).unwrap_or(Quality::Good),
            suppressed_quality: suppress
                .map(|sample| sample.quality)
                .unwrap_or(Quality::Good),
            out_of_service_quality: oos.map(|sample| sample.quality).unwrap_or(Quality::Good),
        };
        io.write_sample(
            ports.shelved,
            Sample::new(
                Value::Bool(observation.shelved),
                observation.shelved_quality,
                tick,
            ),
        )?;
        io.write_sample(
            ports.suppressed,
            Sample::new(
                Value::Bool(observation.suppressed),
                observation.suppressed_quality,
                tick,
            ),
        )?;
        io.write_sample(
            ports.out_of_service,
            Sample::new(
                Value::Bool(observation.out_of_service),
                observation.out_of_service_quality,
                tick,
            ),
        )?;
        Ok(observation)
    }

    /// Folds the run state into `state` — the checkpoint vocabulary
    /// both managed kinds share.
    pub(crate) fn capture(&self, state: &mut StateMap) {
        state.insert("shelve_elapsed", Value::Int(self.shelve_elapsed as i64));
        state.insert("suppressed", Value::Bool(self.suppressed));
        state.insert("out_of_service", Value::Bool(self.out_of_service));
    }

    /// The restore-path read of [`capture`](Self::capture)'s fields.
    pub(crate) fn restore(component: &str, state: &StateMap) -> Result<Self, StateError> {
        let elapsed = state.require_i64(component, "shelve_elapsed")?;
        if elapsed < 0 {
            return Err(StateError::InvalidValue {
                element: component.to_string(),
                field: "shelve_elapsed".to_string(),
                value: Value::Int(elapsed),
            });
        }
        Ok(Self {
            shelve_elapsed: elapsed as u64,
            suppressed: state.require_bool(component, "suppressed")?,
            out_of_service: state.require_bool(component, "out_of_service")?,
        })
    }
}
