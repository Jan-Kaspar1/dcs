//! Managed Bool latching alarm: `bool-latching-alarm`'s two-flag
//! lifecycle for Bool-sourced conditions plus the shelving,
//! suppression, and out-of-service lifecycle architecture decisions
//! 71–73 record — the Bool-`in` managed sibling.

use crate::describe;
use crate::managed::{ManagedAlarmConfig, ManagedAlarmIo, ManagedState};
use crate::params::{ParameterError, Parameters};
use dcs_core::{
    CommandError, ComponentDescriptor, PortRole, Sample, StateError, StateMap, Tick, Value,
};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// A managed Bool latching alarm:
/// [`BoolLatchingAlarm`](crate::BoolLatchingAlarm)'s
/// `in`/`ack`/`alarm`/`unacknowledged` vocabulary exactly — `alarm`
/// following the Bool condition, `unacknowledged` latching the
/// false-to-true edge under the same consumed-edge ack rule — plus the
/// managed-alarm surface decisions 71–73 record, shared verbatim with
/// [`ManagedLatchingAlarm`](crate::ManagedLatchingAlarm).
///
/// **Managed surface (decisions 71–73):** three optional level-observed
/// inputs — `shelve`, `oos`, `suppress`, each `In` `Bool` and typically
/// bound to a writable internal point so the operator's command travels
/// the ordinary receipted, actor-attributed write path — and three
/// mandatory `Out` `Bool` status outputs reporting which managed state
/// stands: `shelved`, `suppressed`, `out_of_service`. The model selects
/// each alarm's managed surface by which inputs it binds: an unbound
/// `shelve` port exposes no shelving surface at all — the strongest
/// rejection — while a bound-but-unwritable `shelve` point answers
/// `NotWritable` at command submission, and a `max_shelve_ticks` of `0`
/// declares the alarm never-shelvable so even a delivered request is
/// inert beneath it.
///
/// - **Shelving** — `shelve` `true` requests shelving, `false` is the
///   manual unshelve; `shelved` asserts on the request's first scan and
///   holds for `max_shelve_ticks` scans — the asserting scan counts as
///   the first — then expiry drops it at the bound even while the
///   request stands, so a re-shelve requires the request to cycle
///   through `false`. Shelving never touches the two-flag evaluation:
///   an assertion arriving mid-shelve still latches `unacknowledged`,
///   and an `ack` arriving mid-shelve still clears it — the flag
///   reroutes the alarm pane's presentation, never the record.
/// - **Out of service** — `oos` `true` takes the alarm out of service,
///   `false` returns it; the state is manual in both directions with
///   no automatic return. An assertion arriving mid-OOS evaluates and
///   latches normally — `alarm` reports the condition's truth
///   throughout.
/// - **Suppression** — `suppress` `true` asserts `suppressed` and
///   withholds annunciation: the `unacknowledged` latch is held clear
///   while the suppress level stands, `alarm` still following `in`. On
///   release the alarm evaluates fresh, so a condition that outlasted
///   its suppression arrives as a new `unacknowledged` transition
///   rather than a stale standing flag.
///
/// `alarm` and `unacknowledged` carry the sibling's worst-of-`in`-and-
/// `ack` quality merge — the values still evaluating under degraded
/// reads — while each managed flag output carries its driving input's
/// quality, `Good` for an unbound input.
///
/// Declared I/O: `in` (`In`, `Bool`), `ack` (`In`, `Bool`), the bound
/// subset of `shelve`/`oos`/`suppress` (`In`, `Bool`), `alarm`,
/// `unacknowledged`, `shelved`, `suppressed`, `out_of_service` (all
/// `Out`, `Bool`).
///
/// Parameters: [`ManagedAlarmConfig`]'s required non-negative `Int`
/// set — `max_shelve_ticks` plus the decision-70
/// `priority`/`class`/`response_ticks` rationalization fields the Bool
/// sibling carries like every alarm kind. The Bool analogue of the
/// standing limit state has no limits or hysteresis to tune.
#[derive(Debug)]
pub struct ManagedBoolLatchingAlarm {
    name: String,
    io: ManagedAlarmIo,
    config: ManagedAlarmConfig,
    /// The `in` level observed on the previous scan — the edge the
    /// latch arms on. `false` before the first scan, so a `true`
    /// first read is a fresh assertion — the sibling's convention.
    state: bool,
    /// The `ack` level observed on the previous scan — the baseline
    /// the acknowledgment's rising edge is detected against. `false`
    /// before the first scan, so a `true` first read is an
    /// acknowledgment.
    ack_seen: bool,
    /// The acknowledgment latch: set on a fresh assertion, cleared on
    /// `ack`'s rising edge, withheld while `suppress` stands.
    latched: bool,
    /// The managed-state machine's run state — the shelve-expiry timer,
    /// the suppression baseline, the out-of-service level.
    managed: ManagedState,
}

impl ManagedBoolLatchingAlarm {
    /// The component-kind string the model-driven registry maps onto
    /// this type's constructor.
    pub const KIND: &'static str = "managed-bool-latching-alarm";

    /// Builds the component from explicit points and managed
    /// configuration; the latch starts cleared, the tracked `in` level
    /// starts `false`, and no managed flag stands.
    pub fn new(name: impl Into<String>, io: ManagedAlarmIo, config: ManagedAlarmConfig) -> Self {
        Self {
            name: name.into(),
            io,
            config,
            state: false,
            ack_seen: false,
            latched: false,
            managed: ManagedState::default(),
        }
    }

    /// Builds the component from a plant-model parameter map, reading
    /// the parameters listed on the type's docs — a missing or invalid
    /// key failing as a [`ParameterError`] naming it.
    pub fn from_parameters(
        name: impl Into<String>,
        io: ManagedAlarmIo,
        parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        let config = ManagedAlarmConfig::from_parameters(&name, parameters)?;
        Ok(Self::new(name, io, config))
    }
}

impl Component for ManagedBoolLatchingAlarm {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        let mut requirements = vec![
            IoRequirement::input::<bool>("in", self.io.input),
            IoRequirement::input::<bool>("ack", self.io.ack),
        ];
        requirements.extend(self.io.input_requirements());
        requirements.push(IoRequirement::output::<bool>("alarm", self.io.alarm));
        requirements.push(IoRequirement::output::<bool>(
            "unacknowledged",
            self.io.unacknowledged,
        ));
        requirements.extend(self.io.output_requirements());
        requirements
    }

    fn step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError> {
        let sample = io.read_typed::<bool>(self.io.input)?;
        let ack = io.read_typed::<bool>(self.io.ack)?;
        let managed = self
            .managed
            .step(io, &self.io, self.config.max_shelve_ticks, tick)?;

        let fresh_trip = sample.value && (!self.state || managed.was_suppressed);
        let acknowledged = ack.value && !self.ack_seen;
        self.state = sample.value;
        self.ack_seen = ack.value;
        self.latched = ((self.latched && !acknowledged) || fresh_trip) && !managed.suppressed;
        let quality = sample.quality.merge(ack.quality);
        io.write_sample(
            self.io.alarm,
            Sample::new(Value::Bool(self.state), quality, tick),
        )?;
        io.write_sample(
            self.io.unacknowledged,
            Sample::new(Value::Bool(self.latched), quality, tick),
        )?;
        Ok(())
    }

    /// Describes the block with the sibling's roles — `in` the Bool
    /// alarm condition, `ack` the operator's clearing command, `alarm`
    /// the reported standing state, `unacknowledged` the reported
    /// latch — plus the Status-role managed surface and the managed
    /// configuration parameter set.
    fn describe(&self) -> ComponentDescriptor {
        let mut roles: Vec<(&'static str, PortRole)> = vec![
            ("in", PortRole::ProcessValue),
            ("ack", PortRole::Status),
            ("alarm", PortRole::Status),
            ("unacknowledged", PortRole::Status),
        ];
        roles.extend(self.io.roles());
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &roles,
            ManagedAlarmConfig::parameters(),
        )
    }

    /// Tunes a declared managed-configuration parameter at the scan
    /// boundary; a `max_shelve_ticks` retune applies to a standing
    /// shelve on the next scan.
    fn apply_parameter(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        self.config = self.config.tune(&self.name, parameter, value)?;
        Ok(())
    }

    /// Reports the declared tuning — the same fields
    /// [`capture_state`](Self::capture_state) checkpoints.
    fn report_parameters(&self) -> StateMap {
        let mut parameters = StateMap::new();
        self.config.report(&mut parameters);
        parameters
    }

    /// Captures the tracked `in` level, the observed `ack` level — so
    /// a standby tracking a held `ack` does not read a phantom
    /// acknowledgment edge — and the acknowledgment latch — the
    /// sibling's `state`/`unacknowledged` vocabulary — plus the
    /// managed run state and tuned configuration, so a tracking standby
    /// continues a mid-shelve countdown identically.
    fn capture_state(&self) -> StateMap {
        let mut state = self.report_parameters();
        state.insert("state", Value::Bool(self.state));
        state.insert("ack", Value::Bool(self.ack_seen));
        state.insert("unacknowledged", Value::Bool(self.latched));
        self.managed.capture(&mut state);
        state
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        state.ensure_known_fields(
            &self.name,
            &[
                "state",
                "ack",
                "unacknowledged",
                "shelve_elapsed",
                "suppressed",
                "out_of_service",
                "max_shelve_ticks",
                "priority",
                "class",
                "response_ticks",
            ],
        )?;
        let restored = state.require_bool(&self.name, "state")?;
        let latched = state.require_bool(&self.name, "unacknowledged")?;
        let config = ManagedAlarmConfig::restore(&self.name, state)?;
        let managed = ManagedState::restore(&self.name, state)?;
        self.config = config;
        self.managed = managed;
        self.state = restored;
        // `ack` is absent from checkpoints predating the field; a held
        // level then reads as an edge on the first restored scan — the
        // same clearing the older level-rule applied every scan.
        self.ack_seen = state.optional_bool(&self.name, "ack")?.unwrap_or(false);
        self.latched = latched;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::TestIo;
    use dcs_core::{
        Direction, ParameterDescriptor, ParameterRange, PointId, PortDescriptor, Quality,
        QualityReason, ValueKind,
    };
    use dcs_model::{ComponentId, ComponentInstance};
    use std::collections::BTreeMap;

    const IN: PointId = PointId(250);
    const ACK: PointId = PointId(251);
    const SHELVE: PointId = PointId(252);
    const OOS: PointId = PointId(253);
    const SUPPRESS: PointId = PointId(254);
    const ALARM: PointId = PointId(255);
    const UNACK: PointId = PointId(256);
    const SHELVED: PointId = PointId(257);
    const SUPPRESSED: PointId = PointId(258);
    const OUT_OF_SERVICE: PointId = PointId(259);

    fn config(max_shelve_ticks: u64) -> ManagedAlarmConfig {
        ManagedAlarmConfig {
            max_shelve_ticks,
            priority: 1,
            class: 2,
            response_ticks: 30,
        }
    }

    /// Every managed input bound — the fully wired surface.
    fn io_all() -> ManagedAlarmIo {
        ManagedAlarmIo {
            input: IN,
            ack: ACK,
            shelve: Some(SHELVE),
            oos: Some(OOS),
            suppress: Some(SUPPRESS),
            alarm: ALARM,
            unacknowledged: UNACK,
            shelved: SHELVED,
            suppressed: SUPPRESSED,
            out_of_service: OUT_OF_SERVICE,
        }
    }

    /// A five-scan shelving bound.
    fn component() -> ManagedBoolLatchingAlarm {
        ManagedBoolLatchingAlarm::new("mbal", io_all(), config(5))
    }

    fn io() -> TestIo {
        TestIo::new(&[
            (
                IN,
                Direction::In,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
            (
                ACK,
                Direction::In,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
            (
                SHELVE,
                Direction::In,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
            (
                OOS,
                Direction::In,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
            (
                SUPPRESS,
                Direction::In,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
            (
                ALARM,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
            (
                UNACK,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
            (
                SHELVED,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
            (
                SUPPRESSED,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
            (
                OUT_OF_SERVICE,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
        ])
    }

    /// One scan's input image.
    #[derive(Default)]
    struct Inputs {
        condition: bool,
        ack: bool,
        shelve: bool,
        oos: bool,
        suppress: bool,
    }

    /// Feeds the input image and steps once.
    fn step(block: &mut ManagedBoolLatchingAlarm, io: &TestIo, inputs: Inputs, tick: u64) {
        io.feed(IN, Sample::good(Value::Bool(inputs.condition), Tick(tick)));
        io.feed(ACK, Sample::good(Value::Bool(inputs.ack), Tick(tick)));
        io.feed(SHELVE, Sample::good(Value::Bool(inputs.shelve), Tick(tick)));
        io.feed(OOS, Sample::good(Value::Bool(inputs.oos), Tick(tick)));
        io.feed(
            SUPPRESS,
            Sample::good(Value::Bool(inputs.suppress), Tick(tick)),
        );
        block.step(io, Tick(tick)).unwrap();
    }

    fn flag(io: &TestIo, point: PointId) -> bool {
        io.written(point).unwrap().value == Value::Bool(true)
    }

    fn alarmed(io: &TestIo) -> bool {
        flag(io, ALARM)
    }

    fn unacknowledged(io: &TestIo) -> bool {
        flag(io, UNACK)
    }

    fn shelved(io: &TestIo) -> bool {
        flag(io, SHELVED)
    }

    fn suppressed(io: &TestIo) -> bool {
        flag(io, SUPPRESSED)
    }

    fn out_of_service(io: &TestIo) -> bool {
        flag(io, OUT_OF_SERVICE)
    }

    #[test]
    fn assertion_latches_and_flags_report_clear() {
        let mut block = component();
        let io = io();

        step(&mut block, &io, Inputs::default(), 1);
        assert!(!alarmed(&io));
        assert!(!unacknowledged(&io));
        assert!(!shelved(&io));
        assert!(!suppressed(&io));
        assert!(!out_of_service(&io));

        step(
            &mut block,
            &io,
            Inputs {
                condition: true,
                ..Inputs::default()
            },
            2,
        );
        assert!(alarmed(&io));
        assert!(unacknowledged(&io));
        assert!(!shelved(&io));
        assert!(!suppressed(&io));
        assert!(!out_of_service(&io));
    }

    #[test]
    fn shelve_asserts_until_manual_release() {
        let mut block = component();
        let io = io();

        step(
            &mut block,
            &io,
            Inputs {
                shelve: true,
                ..Inputs::default()
            },
            1,
        );
        assert!(shelved(&io));

        step(&mut block, &io, Inputs::default(), 2);
        assert!(!shelved(&io));
    }

    #[test]
    fn shelve_expires_at_the_bound_while_the_request_stands() {
        let mut block = component();
        let io = io();

        for tick in 1..=5 {
            step(
                &mut block,
                &io,
                Inputs {
                    shelve: true,
                    ..Inputs::default()
                },
                tick,
            );
            assert!(shelved(&io), "request scan {tick} inside the bound");
        }
        step(
            &mut block,
            &io,
            Inputs {
                shelve: true,
                ..Inputs::default()
            },
            6,
        );
        assert!(!shelved(&io));
    }

    #[test]
    fn reshelving_after_expiry_requires_the_request_to_cycle() {
        let mut block = component();
        let io = io();

        for tick in 1..=6 {
            step(
                &mut block,
                &io,
                Inputs {
                    shelve: true,
                    ..Inputs::default()
                },
                tick,
            );
        }
        assert!(!shelved(&io));
        step(
            &mut block,
            &io,
            Inputs {
                shelve: true,
                ..Inputs::default()
            },
            7,
        );
        assert!(!shelved(&io));

        // Cycling the request through false re-arms a full bound.
        step(&mut block, &io, Inputs::default(), 8);
        step(
            &mut block,
            &io,
            Inputs {
                shelve: true,
                ..Inputs::default()
            },
            9,
        );
        assert!(shelved(&io));
        for tick in 10..=13 {
            step(
                &mut block,
                &io,
                Inputs {
                    shelve: true,
                    ..Inputs::default()
                },
                tick,
            );
            assert!(shelved(&io), "re-shelved scan {tick} inside the bound");
        }
        step(
            &mut block,
            &io,
            Inputs {
                shelve: true,
                ..Inputs::default()
            },
            14,
        );
        assert!(!shelved(&io));
    }

    #[test]
    fn a_zero_bound_declares_the_alarm_never_shelvable() {
        let mut block = ManagedBoolLatchingAlarm::new("mbal", io_all(), config(0));
        let io = io();

        step(
            &mut block,
            &io,
            Inputs {
                shelve: true,
                ..Inputs::default()
            },
            1,
        );
        assert!(!shelved(&io));
        step(
            &mut block,
            &io,
            Inputs {
                shelve: true,
                ..Inputs::default()
            },
            2,
        );
        assert!(!shelved(&io));
    }

    #[test]
    fn an_unbound_shelve_port_exposes_no_shelving_surface() {
        let mut io = io_all();
        io.shelve = None;
        let block = ManagedBoolLatchingAlarm::new("mbal", io, config(5));
        assert!(
            block
                .io_requirements()
                .iter()
                .all(|requirement| requirement.name != "shelve")
        );
        assert!(
            block
                .io_requirements()
                .iter()
                .any(|requirement| requirement.name == "shelved")
        );
    }

    #[test]
    fn shelving_an_active_alarm_changes_no_alarm_state() {
        let mut block = component();
        let io = io();

        step(
            &mut block,
            &io,
            Inputs {
                condition: true,
                ..Inputs::default()
            },
            1,
        );
        step(
            &mut block,
            &io,
            Inputs {
                condition: true,
                shelve: true,
                ..Inputs::default()
            },
            2,
        );
        assert!(alarmed(&io));
        assert!(unacknowledged(&io));
        assert!(shelved(&io));
    }

    #[test]
    fn an_ack_mid_shelve_still_clears_the_latch() {
        let mut block = component();
        let io = io();

        step(
            &mut block,
            &io,
            Inputs {
                condition: true,
                ..Inputs::default()
            },
            1,
        );
        step(
            &mut block,
            &io,
            Inputs {
                condition: true,
                shelve: true,
                ..Inputs::default()
            },
            2,
        );
        step(
            &mut block,
            &io,
            Inputs {
                condition: true,
                ack: true,
                shelve: true,
                ..Inputs::default()
            },
            3,
        );
        assert!(alarmed(&io));
        assert!(!unacknowledged(&io));
        assert!(shelved(&io));

        // Past the bound the shelve expires; the acknowledged record
        // persists.
        for tick in 4..=7 {
            step(
                &mut block,
                &io,
                Inputs {
                    condition: true,
                    shelve: true,
                    ..Inputs::default()
                },
                tick,
            );
        }
        assert!(!shelved(&io));
        assert!(alarmed(&io));
        assert!(!unacknowledged(&io));
    }

    #[test]
    fn an_ack_held_since_before_the_trip_does_not_disarm_the_latch() {
        let mut block = component();
        let io = io();

        // The QA finding's reproduction: `ack` written `true` while the
        // alarm stands clear and never released. Under the consumed-
        // edge rule the edge cleared an empty latch, so the fresh `in`
        // edge still annunciates — no `suppressed`/`shelved`/
        // `out_of_service` state asserts to name a withholding, because
        // there is none.
        step(
            &mut block,
            &io,
            Inputs {
                ack: true,
                ..Inputs::default()
            },
            1,
        );
        assert!(!alarmed(&io));
        assert!(!unacknowledged(&io));

        step(
            &mut block,
            &io,
            Inputs {
                condition: true,
                ack: true,
                ..Inputs::default()
            },
            2,
        );
        assert!(alarmed(&io));
        assert!(
            unacknowledged(&io),
            "the held ack cannot pre-acknowledge the fresh trip"
        );
        assert!(!suppressed(&io));
        assert!(!shelved(&io));
        assert!(!out_of_service(&io));

        // Releasing `ack` while the condition stands leaves the latch
        // standing — the alarm remains on the unacknowledged pane.
        step(
            &mut block,
            &io,
            Inputs {
                condition: true,
                ..Inputs::default()
            },
            3,
        );
        assert!(alarmed(&io));
        assert!(unacknowledged(&io));

        // A second pulse acknowledges the standing alarm normally.
        step(
            &mut block,
            &io,
            Inputs {
                condition: true,
                ack: true,
                ..Inputs::default()
            },
            4,
        );
        assert!(alarmed(&io));
        assert!(!unacknowledged(&io));
    }

    #[test]
    fn a_fresh_trip_latches_even_on_the_ack_edge_scan() {
        let mut block = component();
        let io = io();

        // A trip and the acknowledgment edge arriving on one scan: the
        // pulse acknowledged what stood before it, so the fresh
        // assertion latches rather than passing silently.
        step(
            &mut block,
            &io,
            Inputs {
                condition: true,
                ack: true,
                ..Inputs::default()
            },
            1,
        );
        assert!(alarmed(&io));
        assert!(unacknowledged(&io));

        // The held `ack` consumes nothing further — the latch stands.
        step(
            &mut block,
            &io,
            Inputs {
                condition: true,
                ack: true,
                ..Inputs::default()
            },
            2,
        );
        assert!(unacknowledged(&io));

        // Suppression still withholds: the named managed state is the
        // only gate on the latch.
        step(
            &mut block,
            &io,
            Inputs {
                suppress: true,
                ..Inputs::default()
            },
            3,
        );
        step(
            &mut block,
            &io,
            Inputs {
                condition: true,
                suppress: true,
                ..Inputs::default()
            },
            4,
        );
        assert!(suppressed(&io));
        assert!(!unacknowledged(&io));
    }

    #[test]
    fn a_trip_mid_oos_evaluates_and_latches() {
        let mut block = component();
        let io = io();

        step(
            &mut block,
            &io,
            Inputs {
                oos: true,
                ..Inputs::default()
            },
            1,
        );
        assert!(out_of_service(&io));

        step(
            &mut block,
            &io,
            Inputs {
                condition: true,
                oos: true,
                ..Inputs::default()
            },
            2,
        );
        assert!(out_of_service(&io));
        assert!(alarmed(&io));
        assert!(unacknowledged(&io));

        // Only the manual return clears the flag — the standing trip
        // it recorded remains.
        step(
            &mut block,
            &io,
            Inputs {
                condition: true,
                ..Inputs::default()
            },
            3,
        );
        assert!(!out_of_service(&io));
        assert!(alarmed(&io));
        assert!(unacknowledged(&io));
    }

    #[test]
    fn suppression_withholds_annunciation_and_releases_fresh() {
        let mut block = component();
        let io = io();

        // A condition arriving under suppression reports on `alarm`
        // but never latches.
        step(
            &mut block,
            &io,
            Inputs {
                condition: true,
                suppress: true,
                ..Inputs::default()
            },
            1,
        );
        assert!(suppressed(&io));
        assert!(alarmed(&io));
        assert!(!unacknowledged(&io));

        // The condition outlasts its suppression: release evaluates
        // fresh and the standing assertion arrives as a new latch.
        step(
            &mut block,
            &io,
            Inputs {
                condition: true,
                ..Inputs::default()
            },
            2,
        );
        assert!(!suppressed(&io));
        assert!(alarmed(&io));
        assert!(unacknowledged(&io));
    }

    #[test]
    fn suppression_release_of_a_cleared_condition_latches_nothing() {
        let mut block = component();
        let io = io();

        step(
            &mut block,
            &io,
            Inputs {
                condition: true,
                suppress: true,
                ..Inputs::default()
            },
            1,
        );
        step(
            &mut block,
            &io,
            Inputs {
                suppress: true,
                ..Inputs::default()
            },
            2,
        );
        assert!(!alarmed(&io));
        assert!(!unacknowledged(&io));

        step(&mut block, &io, Inputs::default(), 3);
        assert!(!suppressed(&io));
        assert!(!alarmed(&io));
        assert!(!unacknowledged(&io));
    }

    #[test]
    fn bad_quality_on_inputs_marks_the_outputs_they_drive() {
        let mut block = component();
        let io = io();

        // A Bad `in` still asserts — both alarm outputs carry it.
        io.feed(
            IN,
            Sample::new(
                Value::Bool(true),
                Quality::Bad(QualityReason::CommunicationFault),
                Tick(1),
            ),
        );
        block.step(&io, Tick(1)).unwrap();
        assert!(alarmed(&io));
        assert!(unacknowledged(&io));
        for point in [ALARM, UNACK] {
            assert_eq!(
                io.written(point).unwrap().quality,
                Quality::Bad(QualityReason::CommunicationFault)
            );
        }

        // A degraded `oos` marks only its own flag.
        io.feed(IN, Sample::good(Value::Bool(false), Tick(2)));
        io.feed(
            OOS,
            Sample::new(
                Value::Bool(true),
                Quality::Uncertain(QualityReason::Stale),
                Tick(2),
            ),
        );
        block.step(&io, Tick(2)).unwrap();
        assert!(out_of_service(&io));
        assert_eq!(
            io.written(OUT_OF_SERVICE).unwrap().quality,
            Quality::Uncertain(QualityReason::Stale)
        );
        assert_eq!(io.written(ALARM).unwrap().quality, Quality::Good);
    }

    #[test]
    fn capture_restore_mid_shelve_continues_the_countdown() {
        let mut block = component();
        let active_io = io();

        for tick in 1..=2 {
            step(
                &mut block,
                &active_io,
                Inputs {
                    shelve: true,
                    ..Inputs::default()
                },
                tick,
            );
        }
        let state = block.capture_state();
        assert_eq!(state.get("shelve_elapsed"), Some(Value::Int(2)));

        let mut standby = component();
        standby.restore_state(&state).unwrap();
        assert_eq!(standby.capture_state(), state);
        let standby_io = io();

        for tick in 3..=5 {
            step(
                &mut standby,
                &standby_io,
                Inputs {
                    shelve: true,
                    ..Inputs::default()
                },
                tick,
            );
            assert!(shelved(&standby_io), "restored scan {tick}");
        }
        step(
            &mut standby,
            &standby_io,
            Inputs {
                shelve: true,
                ..Inputs::default()
            },
            6,
        );
        assert!(!shelved(&standby_io));
    }

    #[test]
    fn capture_restore_mid_oos_and_suppression_continues_identically() {
        let mut block = component();
        let active_io = io();

        step(
            &mut block,
            &active_io,
            Inputs {
                condition: true,
                oos: true,
                suppress: true,
                ..Inputs::default()
            },
            1,
        );
        let state = block.capture_state();
        assert_eq!(state.get("out_of_service"), Some(Value::Bool(true)));
        assert_eq!(state.get("suppressed"), Some(Value::Bool(true)));

        let mut standby = component();
        standby.restore_state(&state).unwrap();
        let standby_io = io();

        // Releasing both flags on the standby: the outlasted condition
        // re-arrives fresh and latches.
        step(
            &mut standby,
            &standby_io,
            Inputs {
                condition: true,
                ..Inputs::default()
            },
            2,
        );
        assert!(!suppressed(&standby_io));
        assert!(!out_of_service(&standby_io));
        assert!(alarmed(&standby_io));
        assert!(unacknowledged(&standby_io));
    }

    #[test]
    fn incompatible_state_is_a_named_error() {
        let mut block = component();

        assert!(matches!(
            block.restore_state(&StateMap::new()),
            Err(StateError::MissingField { ref field, .. }) if field == "state"
        ));

        let mut state = block.capture_state();
        state.insert("priority", Value::Int(-1));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::InvalidValue { ref field, .. }) if field == "priority"
        ));

        let mut state = block.capture_state();
        state.insert("trip_count", Value::Int(3));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::UnknownField { ref field, .. }) if field == "trip_count"
        ));
    }

    #[test]
    fn builds_from_parameter_map_naming_failures() {
        let parameters: Parameters = [
            ("max_shelve_ticks".to_string(), Value::Int(5)),
            ("priority".to_string(), Value::Int(1)),
            ("class".to_string(), Value::Int(2)),
            ("response_ticks".to_string(), Value::Int(30)),
        ]
        .into_iter()
        .collect();
        let instance = ComponentInstance {
            id: ComponentId(13),
            kind: ManagedBoolLatchingAlarm::KIND.to_string(),
            parameters,
            rationalization: None,
            ports: BTreeMap::new(),
        };
        let block =
            ManagedBoolLatchingAlarm::from_parameters("mbal", io_all(), &instance.parameters)
                .unwrap();
        assert_eq!(block.config.max_shelve_ticks, 5);

        // A missing managed parameter is named.
        let mut incomplete = instance.parameters.clone();
        incomplete.remove("class");
        assert!(matches!(
            ManagedBoolLatchingAlarm::from_parameters("mbal", io_all(), &incomplete).unwrap_err(),
            ParameterError::Missing { ref parameter, .. } if parameter == "class"
        ));
    }

    #[test]
    fn tuning_applies_and_rejects_by_name() {
        let mut block = component();
        let io = io();

        step(
            &mut block,
            &io,
            Inputs {
                shelve: true,
                ..Inputs::default()
            },
            1,
        );
        assert!(shelved(&io));
        block
            .apply_parameter("max_shelve_ticks", Value::Int(0))
            .unwrap();
        step(
            &mut block,
            &io,
            Inputs {
                shelve: true,
                ..Inputs::default()
            },
            2,
        );
        assert!(!shelved(&io));

        assert!(matches!(
            block.apply_parameter("shelve_for", Value::Int(5)),
            Err(CommandError::UnknownParameter { ref parameter, .. }) if parameter == "shelve_for"
        ));
        block.apply_parameter("priority", Value::Int(4)).unwrap();
    }

    #[test]
    fn describes_itself() {
        let descriptor = component().describe();
        assert_eq!(descriptor.name, "mbal");
        assert_eq!(descriptor.kind, ManagedBoolLatchingAlarm::KIND);
        assert_eq!(descriptor.label, "mbal");
        let port = |name: &str, direction, role| PortDescriptor {
            name: name.to_string(),
            direction,
            kind: ValueKind::Bool,
            role,
            point: None,
        };
        assert_eq!(
            descriptor.ports,
            [
                port("in", Direction::In, Some(PortRole::ProcessValue)),
                port("ack", Direction::In, Some(PortRole::Status)),
                port("shelve", Direction::In, Some(PortRole::Status)),
                port("oos", Direction::In, Some(PortRole::Status)),
                port("suppress", Direction::In, Some(PortRole::Status)),
                port("alarm", Direction::Out, Some(PortRole::Status)),
                port("unacknowledged", Direction::Out, Some(PortRole::Status)),
                port("shelved", Direction::Out, Some(PortRole::Status)),
                port("suppressed", Direction::Out, Some(PortRole::Status)),
                port("out_of_service", Direction::Out, Some(PortRole::Status)),
            ]
        );
        let int_parameter = |name: &str| ParameterDescriptor {
            name: name.to_string(),
            kind: ValueKind::Int,
            range: Some(ParameterRange {
                min: Value::Int(0),
                max: Value::Int(i64::MAX),
            }),
        };
        assert_eq!(
            descriptor.parameters,
            [
                int_parameter("max_shelve_ticks"),
                int_parameter("priority"),
                int_parameter("class"),
                int_parameter("response_ticks"),
            ]
        );
    }
}
