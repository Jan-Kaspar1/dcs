//! Managed latching alarm: `latching-alarm`'s limit checking and
//! operator-acknowledgment latch plus the shelving, suppression, and
//! out-of-service lifecycle architecture decisions 71–73 record — the
//! Float-`in` managed sibling.

use crate::alarm_monitor::{Alarm, AlarmLimits};
use crate::describe;
use crate::managed::{ManagedAlarmConfig, ManagedAlarmIo, ManagedState};
use crate::params::{ParameterError, Parameters};
use dcs_core::{
    CommandError, ComponentDescriptor, PortRole, Quality, QualityReason, Sample, StateError,
    StateMap, Tick, Value, ValueKind,
};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// A managed latching alarm: [`LatchingAlarm`](crate::LatchingAlarm)'s
/// `in`/`ack`/`alarm`/`unacknowledged` vocabulary exactly — the same
/// hysteresis rule, the same level-sensitive ack-dominates latch —
/// plus the managed-alarm surface decisions 71–73 record, shared
/// verbatim with [`ManagedBoolLatchingAlarm`](crate::ManagedBoolLatchingAlarm).
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
///   a trip arriving mid-shelve still latches `unacknowledged`, and an
///   `ack` arriving mid-shelve still clears it — the flag reroutes the
///   alarm pane's presentation, never the record.
/// - **Out of service** — `oos` `true` takes the alarm out of service,
///   `false` returns it; the state is manual in both directions with
///   no automatic return. A trip arriving mid-OOS evaluates and latches
///   normally — `alarm` reports process truth throughout.
/// - **Suppression** — `suppress` `true` asserts `suppressed` and
///   withholds annunciation: the `unacknowledged` latch is held clear
///   while the suppress level stands, `alarm` still reporting the
///   limit state. On release the alarm evaluates fresh, so a condition
///   that outlasted its suppression arrives as a new `unacknowledged`
///   transition rather than a stale standing flag.
///
/// `alarm` and `unacknowledged` carry the sibling's worst-of-`in`-and-
/// `ack` quality merge — a `NaN` input additionally merging
/// `Bad(DeviceFault)` — while each managed flag output carries its
/// driving input's quality, `Good` for an unbound input.
///
/// Declared I/O: `in` (`In`, `Float`), `ack` (`In`, `Bool`), the bound
/// subset of `shelve`/`oos`/`suppress` (`In`, `Bool`), `alarm`,
/// `unacknowledged`, `shelved`, `suppressed`, `out_of_service` (all
/// `Out`, `Bool`).
///
/// Parameters: the sibling's `low_limit`/`high_limit`/`hysteresis`
/// (required finite `Float` limits, `low_limit < high_limit`; optional
/// non-negative `hysteresis`, default `0.0`) plus
/// [`ManagedAlarmConfig`]'s required non-negative `Int` set —
/// `max_shelve_ticks` and the decision-70 `priority`/`class`/
/// `response_ticks` rationalization fields.
#[derive(Debug)]
pub struct ManagedLatchingAlarm {
    name: String,
    io: ManagedAlarmIo,
    limits: AlarmLimits,
    config: ManagedAlarmConfig,
    state: Alarm,
    /// The acknowledgment latch: set on a fresh trip, cleared while
    /// `ack` reads `true`, withheld while `suppress` stands.
    latched: bool,
    /// The managed-state machine's run state — the shelve-expiry timer,
    /// the suppression baseline, the out-of-service level.
    managed: ManagedState,
}

impl ManagedLatchingAlarm {
    /// The component-kind string the model-driven registry maps onto this
    /// type's constructor.
    pub const KIND: &'static str = "managed-latching-alarm";

    /// Builds the component from explicit points, limits, and managed
    /// configuration, or reports the limits' inconsistency as a
    /// [`ParameterError`].
    pub fn new(
        name: impl Into<String>,
        io: ManagedAlarmIo,
        limits: AlarmLimits,
        config: ManagedAlarmConfig,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        Ok(Self {
            limits: AlarmLimits::checked(&name, limits)?,
            name,
            io,
            config,
            state: Alarm::Clear,
            latched: false,
            managed: ManagedState::default(),
        })
    }

    /// Builds the component from a plant-model parameter map, reading the
    /// parameters listed on the type's docs — a missing or invalid key
    /// failing as a [`ParameterError`] naming it.
    pub fn from_parameters(
        name: impl Into<String>,
        io: ManagedAlarmIo,
        parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        let limits = AlarmLimits::from_parameters(&name, parameters)?;
        let config = ManagedAlarmConfig::from_parameters(&name, parameters)?;
        Self::new(name, io, limits, config)
    }
}

impl Component for ManagedLatchingAlarm {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        let mut requirements = vec![
            IoRequirement::input::<f64>("in", self.io.input),
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
        let sample = io.read_typed::<f64>(self.io.input)?;
        let ack = io.read_typed::<bool>(self.io.ack)?;
        let managed = self
            .managed
            .step(io, &self.io, self.config.max_shelve_ticks, tick)?;

        let pv = sample.value;
        let previous = self.state;
        self.state = previous.evaluate(pv, self.limits);
        let fresh_trip =
            self.state != Alarm::Clear && (self.state != previous || managed.was_suppressed);
        self.latched = (self.latched || fresh_trip) && !ack.value && !managed.suppressed;
        let mut quality = sample.quality.merge(ack.quality);
        if pv.is_nan() {
            quality = quality.merge(Quality::Bad(QualityReason::DeviceFault));
        }
        io.write_sample(
            self.io.alarm,
            Sample::new(Value::Bool(self.state != Alarm::Clear), quality, tick),
        )?;
        io.write_sample(
            self.io.unacknowledged,
            Sample::new(Value::Bool(self.latched), quality, tick),
        )?;
        Ok(())
    }

    /// Describes the block with the sibling's roles — `in` the measured
    /// process value, `ack` the operator's clearing command, `alarm` the
    /// reported trip state, `unacknowledged` the reported latch — plus
    /// the Status-role managed surface, and the sibling's limit
    /// parameters beside the managed configuration set.
    fn describe(&self) -> ComponentDescriptor {
        let mut roles: Vec<(&'static str, PortRole)> = vec![
            ("in", PortRole::ProcessValue),
            ("ack", PortRole::Status),
            ("alarm", PortRole::Status),
            ("unacknowledged", PortRole::Status),
        ];
        roles.extend(self.io.roles());
        let mut parameters = vec![
            describe::parameter("low_limit", ValueKind::Float, Some(describe::FINITE_F64)),
            describe::parameter("high_limit", ValueKind::Float, Some(describe::FINITE_F64)),
            describe::parameter(
                "hysteresis",
                ValueKind::Float,
                Some(describe::NONNEGATIVE_F64),
            ),
        ];
        parameters.extend(ManagedAlarmConfig::parameters());
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &roles,
            parameters,
        )
    }

    /// Tunes a declared parameter at the scan boundary — the sibling's
    /// limit tunables and invariants, or one of the managed
    /// configuration fields; a `max_shelve_ticks` retune applies to a
    /// standing shelve on the next scan.
    fn apply_parameter(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        match parameter {
            "low_limit" | "high_limit" | "hysteresis" => {
                self.limits = self.limits.tune(&self.name, parameter, value)?;
            }
            _ => {
                self.config = self.config.tune(&self.name, parameter, value)?;
            }
        }
        Ok(())
    }

    /// Reports the declared tuning — the same fields
    /// [`capture_state`](Self::capture_state) checkpoints.
    fn report_parameters(&self) -> StateMap {
        let mut parameters = StateMap::new();
        parameters.insert("low_limit", Value::Float(self.limits.low));
        parameters.insert("high_limit", Value::Float(self.limits.high));
        parameters.insert("hysteresis", Value::Float(self.limits.hysteresis));
        self.config.report(&mut parameters);
        parameters
    }

    /// Captures the sibling's `state`/`unacknowledged` vocabulary, the
    /// tuned parameters, and the managed run state — the shelve-expiry
    /// timer, the suppression baseline, the out-of-service level — so a
    /// tracking standby continues a mid-shelve countdown identically.
    fn capture_state(&self) -> StateMap {
        let mut state = self.report_parameters();
        state.insert("state", Value::Int(self.state.code()));
        state.insert("unacknowledged", Value::Bool(self.latched));
        self.managed.capture(&mut state);
        state
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        state.ensure_known_fields(
            &self.name,
            &[
                "state",
                "unacknowledged",
                "shelve_elapsed",
                "suppressed",
                "out_of_service",
                "low_limit",
                "high_limit",
                "hysteresis",
                "max_shelve_ticks",
                "priority",
                "class",
                "response_ticks",
            ],
        )?;
        let restored = Alarm::from_code(&self.name, state.require_i64(&self.name, "state")?)?;
        let latched = state.require_bool(&self.name, "unacknowledged")?;
        let limits = AlarmLimits {
            low: state.require_f64(&self.name, "low_limit")?,
            high: state.require_f64(&self.name, "high_limit")?,
            hysteresis: state.require_f64(&self.name, "hysteresis")?,
        };
        // The same invariants `new` and `apply_parameter` enforce.
        limits.check_restored(&self.name)?;
        let config = ManagedAlarmConfig::restore(&self.name, state)?;
        let managed = ManagedState::restore(&self.name, state)?;
        self.limits = limits;
        self.config = config;
        self.managed = managed;
        self.state = restored;
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
    };
    use dcs_model::{ComponentId, ComponentInstance};
    use std::collections::BTreeMap;

    const IN: PointId = PointId(240);
    const ACK: PointId = PointId(241);
    const SHELVE: PointId = PointId(242);
    const OOS: PointId = PointId(243);
    const SUPPRESS: PointId = PointId(244);
    const ALARM: PointId = PointId(245);
    const UNACK: PointId = PointId(246);
    const SHELVED: PointId = PointId(247);
    const SUPPRESSED: PointId = PointId(248);
    const OUT_OF_SERVICE: PointId = PointId(249);

    fn limits() -> AlarmLimits {
        AlarmLimits {
            low: 10.0,
            high: 90.0,
            hysteresis: 5.0,
        }
    }

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

    /// Limits 10 <= low, 90 <= high with a 5-unit hysteresis and a
    /// five-scan shelving bound.
    fn component() -> ManagedLatchingAlarm {
        ManagedLatchingAlarm::new("mlal", io_all(), limits(), config(5)).unwrap()
    }

    fn io() -> TestIo {
        TestIo::new(&[
            (
                IN,
                Direction::In,
                Sample::good(Value::Float(0.0), Tick::ZERO),
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
        pv: f64,
        ack: bool,
        shelve: bool,
        oos: bool,
        suppress: bool,
    }

    /// Feeds the input image and steps once.
    fn step(block: &mut ManagedLatchingAlarm, io: &TestIo, inputs: Inputs, tick: u64) {
        io.feed(IN, Sample::good(Value::Float(inputs.pv), Tick(tick)));
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
    fn trip_latches_and_flags_report_clear() {
        let mut block = component();
        let io = io();

        step(
            &mut block,
            &io,
            Inputs {
                pv: 50.0,
                ..Inputs::default()
            },
            1,
        );
        assert!(!alarmed(&io));
        assert!(!unacknowledged(&io));
        assert!(!shelved(&io));
        assert!(!suppressed(&io));
        assert!(!out_of_service(&io));

        step(
            &mut block,
            &io,
            Inputs {
                pv: 95.0,
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

        // The request's first scan asserts the flag.
        step(
            &mut block,
            &io,
            Inputs {
                pv: 50.0,
                shelve: true,
                ..Inputs::default()
            },
            1,
        );
        assert!(shelved(&io));

        // Releasing the request is the manual unshelve.
        step(
            &mut block,
            &io,
            Inputs {
                pv: 50.0,
                ..Inputs::default()
            },
            2,
        );
        assert!(!shelved(&io));
    }

    #[test]
    fn shelve_expires_at_the_bound_while_the_request_stands() {
        let mut block = component();
        let io = io();

        // The asserting scan counts as the first: a 5-scan bound
        // reports shelved on request scans 1..=5 and drops at 6 even
        // though the request still stands.
        for tick in 1..=5 {
            step(
                &mut block,
                &io,
                Inputs {
                    pv: 50.0,
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
                pv: 50.0,
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
                    pv: 50.0,
                    shelve: true,
                    ..Inputs::default()
                },
                tick,
            );
        }
        // Expired; the still-standing request never re-arms the shelve.
        assert!(!shelved(&io));
        step(
            &mut block,
            &io,
            Inputs {
                pv: 50.0,
                shelve: true,
                ..Inputs::default()
            },
            7,
        );
        assert!(!shelved(&io));

        // Cycling the request through false re-arms a full bound.
        step(
            &mut block,
            &io,
            Inputs {
                pv: 50.0,
                ..Inputs::default()
            },
            8,
        );
        step(
            &mut block,
            &io,
            Inputs {
                pv: 50.0,
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
                    pv: 50.0,
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
                pv: 50.0,
                shelve: true,
                ..Inputs::default()
            },
            14,
        );
        assert!(!shelved(&io));
    }

    #[test]
    fn a_zero_bound_declares_the_alarm_never_shelvable() {
        let mut block = ManagedLatchingAlarm::new("mlal", io_all(), limits(), config(0)).unwrap();
        let io = io();

        // Even a delivered request is inert beneath a declared
        // never-shelvable alarm — the flag never asserts.
        step(
            &mut block,
            &io,
            Inputs {
                pv: 50.0,
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
                pv: 50.0,
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
        let block = ManagedLatchingAlarm::new("mlal", io, limits(), config(5)).unwrap();
        // No `shelve` requirement is declared; `shelved` still is.
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

        // Tripped and unacknowledged; shelving reroutes presentation
        // but the record stands: `alarm` reports the truth and the
        // latch stays latched.
        step(
            &mut block,
            &io,
            Inputs {
                pv: 95.0,
                ..Inputs::default()
            },
            1,
        );
        step(
            &mut block,
            &io,
            Inputs {
                pv: 95.0,
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
                pv: 95.0,
                ..Inputs::default()
            },
            1,
        );
        step(
            &mut block,
            &io,
            Inputs {
                pv: 95.0,
                shelve: true,
                ..Inputs::default()
            },
            2,
        );
        // Acknowledging while shelved: the latch clears normally, the
        // standing trip still reports, and the shelve holds.
        step(
            &mut block,
            &io,
            Inputs {
                pv: 95.0,
                ack: true,
                shelve: true,
                ..Inputs::default()
            },
            3,
        );
        assert!(alarmed(&io));
        assert!(!unacknowledged(&io));
        assert!(shelved(&io));

        // The acknowledged record persists past the shelve's end —
        // unshelving reveals an alarmed, acknowledged alarm.
        for tick in 4..=7 {
            step(
                &mut block,
                &io,
                Inputs {
                    pv: 95.0,
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
    fn a_trip_mid_oos_evaluates_and_latches() {
        let mut block = component();
        let io = io();

        // Out of service: the flag follows the request level.
        step(
            &mut block,
            &io,
            Inputs {
                pv: 50.0,
                oos: true,
                ..Inputs::default()
            },
            1,
        );
        assert!(out_of_service(&io));

        // A trip while out of service still evaluates and latches —
        // the record stands behind the flag.
        step(
            &mut block,
            &io,
            Inputs {
                pv: 95.0,
                oos: true,
                ..Inputs::default()
            },
            2,
        );
        assert!(out_of_service(&io));
        assert!(alarmed(&io));
        assert!(unacknowledged(&io));

        // The manual return — no automatic one — reveals the standing
        // trip the OOS period recorded.
        step(
            &mut block,
            &io,
            Inputs {
                pv: 95.0,
                ..Inputs::default()
            },
            3,
        );
        assert!(!out_of_service(&io));
        assert!(alarmed(&io));
        assert!(unacknowledged(&io));
    }

    #[test]
    fn oos_stands_until_manual_return_only() {
        let mut block = component();
        let io = io();

        step(
            &mut block,
            &io,
            Inputs {
                pv: 50.0,
                oos: true,
                ..Inputs::default()
            },
            1,
        );
        for tick in 2..=10 {
            step(
                &mut block,
                &io,
                Inputs {
                    pv: 50.0,
                    oos: true,
                    ..Inputs::default()
                },
                tick,
            );
            // No bound applies: the flag stands while the request does.
            assert!(out_of_service(&io));
        }
    }

    #[test]
    fn suppression_withholds_annunciation_and_releases_fresh() {
        let mut block = component();
        let io = io();

        // Suppressed: a trip arriving under suppression reports on
        // `alarm` but the latch is withheld.
        step(
            &mut block,
            &io,
            Inputs {
                pv: 95.0,
                suppress: true,
                ..Inputs::default()
            },
            1,
        );
        assert!(suppressed(&io));
        assert!(alarmed(&io));
        assert!(!unacknowledged(&io));

        // The condition outlasts its suppression: releasing evaluates
        // fresh — the standing trip arrives as a new latch transition.
        step(
            &mut block,
            &io,
            Inputs {
                pv: 95.0,
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

        // A trip comes and goes entirely under suppression.
        step(
            &mut block,
            &io,
            Inputs {
                pv: 95.0,
                suppress: true,
                ..Inputs::default()
            },
            1,
        );
        step(
            &mut block,
            &io,
            Inputs {
                pv: 50.0,
                suppress: true,
                ..Inputs::default()
            },
            2,
        );
        assert!(!alarmed(&io));
        assert!(!unacknowledged(&io));

        // Release finds nothing to announce — the cleared condition
        // never latched.
        step(
            &mut block,
            &io,
            Inputs {
                pv: 50.0,
                ..Inputs::default()
            },
            3,
        );
        assert!(!suppressed(&io));
        assert!(!alarmed(&io));
        assert!(!unacknowledged(&io));
    }

    #[test]
    fn a_standing_latch_survives_suppression_cycles() {
        let mut block = component();
        let io = io();

        // Latched before suppression; suppressing does not erase the
        // record — the latch stands behind the flag...
        step(
            &mut block,
            &io,
            Inputs {
                pv: 95.0,
                ..Inputs::default()
            },
            1,
        );
        assert!(unacknowledged(&io));
        step(
            &mut block,
            &io,
            Inputs {
                pv: 95.0,
                suppress: true,
                ..Inputs::default()
            },
            2,
        );
        assert!(suppressed(&io));
        assert!(alarmed(&io));
        assert!(!unacknowledged(&io));

        // ...but the withheld latch does not return on release: the
        // standing condition re-arrives fresh — a new transition, the
        // same standing record.
        step(
            &mut block,
            &io,
            Inputs {
                pv: 95.0,
                ..Inputs::default()
            },
            3,
        );
        assert!(unacknowledged(&io));
    }

    #[test]
    fn the_managed_flags_combine() {
        let mut block = component();
        let io = io();

        // Shelved and out-of-service at once — orthogonal flags over
        // the same record.
        step(
            &mut block,
            &io,
            Inputs {
                pv: 95.0,
                shelve: true,
                oos: true,
                ..Inputs::default()
            },
            1,
        );
        assert!(shelved(&io));
        assert!(out_of_service(&io));
        assert!(alarmed(&io));
        assert!(unacknowledged(&io));
    }

    #[test]
    fn managed_inputs_propagate_their_quality_to_their_flags() {
        let mut block = component();
        let io = io();

        // A degraded shelve request still shelves — the flag reports
        // the untrusted read rather than hiding it.
        io.feed(
            SHELVE,
            Sample::new(
                Value::Bool(true),
                Quality::Bad(QualityReason::CommunicationFault),
                Tick(1),
            ),
        );
        io.feed(IN, Sample::good(Value::Float(50.0), Tick(1)));
        block.step(&io, Tick(1)).unwrap();
        assert!(shelved(&io));
        assert_eq!(
            io.written(SHELVED).unwrap().quality,
            Quality::Bad(QualityReason::CommunicationFault)
        );

        // The alarm outputs keep the sibling merge — an untrusted
        // shelve does not mark them.
        assert_eq!(io.written(ALARM).unwrap().quality, Quality::Good);
    }

    #[test]
    fn alarm_outputs_keep_the_sibling_quality_rule() {
        let mut block = component();
        let io = io();

        // Bad `in` trips and marks both alarm outputs; the managed
        // flags are unaffected — their quality follows their own
        // inputs.
        io.feed(
            IN,
            Sample::new(
                Value::Float(95.0),
                Quality::Bad(QualityReason::CommunicationFault),
                Tick(1),
            ),
        );
        io.feed(SUPPRESS, Sample::good(Value::Bool(true), Tick(1)));
        block.step(&io, Tick(1)).unwrap();
        for point in [ALARM, UNACK] {
            assert_eq!(
                io.written(point).unwrap().quality,
                Quality::Bad(QualityReason::CommunicationFault)
            );
        }
        assert_eq!(io.written(SUPPRESSED).unwrap().quality, Quality::Good);
        assert_eq!(io.written(SHELVED).unwrap().quality, Quality::Good);
        assert_eq!(io.written(OUT_OF_SERVICE).unwrap().quality, Quality::Good);
    }

    #[test]
    fn nan_input_holds_state_and_marks_bad() {
        let mut block = component();
        let io = io();

        step(
            &mut block,
            &io,
            Inputs {
                pv: 95.0,
                ..Inputs::default()
            },
            1,
        );
        step(
            &mut block,
            &io,
            Inputs {
                pv: f64::NAN,
                ..Inputs::default()
            },
            2,
        );
        assert!(alarmed(&io));
        assert!(unacknowledged(&io));
        for point in [ALARM, UNACK] {
            assert_eq!(
                io.written(point).unwrap().quality,
                Quality::Bad(QualityReason::DeviceFault)
            );
        }
    }

    #[test]
    fn capture_restore_mid_shelve_continues_the_countdown() {
        let mut block = component();
        let active_io = io();

        // Two of five shelved scans elapsed when the checkpoint lands.
        for tick in 1..=2 {
            step(
                &mut block,
                &active_io,
                Inputs {
                    pv: 50.0,
                    shelve: true,
                    ..Inputs::default()
                },
                tick,
            );
        }
        assert!(shelved(&active_io));
        let state = block.capture_state();
        assert_eq!(state.get("shelve_elapsed"), Some(Value::Int(2)));

        let mut standby = component();
        standby.restore_state(&state).unwrap();
        assert_eq!(standby.capture_state(), state);
        let standby_io = io();

        // Scans 3..=5 shelve; scan 6 expires — identically on both.
        for tick in 3..=5 {
            step(
                &mut standby,
                &standby_io,
                Inputs {
                    pv: 50.0,
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
                pv: 50.0,
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

        // Tripped, out of service, and suppressed when the checkpoint
        // lands — every managed flag plus the latch rides along.
        step(
            &mut block,
            &active_io,
            Inputs {
                pv: 95.0,
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

        // Release suppression and OOS together on the standby: the
        // outlasted trip arrives fresh and latches — the restored
        // suppression baseline produces the release transition.
        step(
            &mut standby,
            &standby_io,
            Inputs {
                pv: 95.0,
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
    fn tuning_the_bound_mid_shelve_applies_on_the_next_scan() {
        let mut block = component();
        let io = io();

        step(
            &mut block,
            &io,
            Inputs {
                pv: 50.0,
                shelve: true,
                ..Inputs::default()
            },
            1,
        );
        assert!(shelved(&io));

        // Retuning the bound to zero drops a standing shelve — the
        // flag is derived against the current bound.
        block
            .apply_parameter("max_shelve_ticks", Value::Int(0))
            .unwrap();
        step(
            &mut block,
            &io,
            Inputs {
                pv: 50.0,
                shelve: true,
                ..Inputs::default()
            },
            2,
        );
        assert!(!shelved(&io));

        // Undeclared names are rejected, wrong kinds typed-out.
        assert!(matches!(
            block.apply_parameter("shelve_for", Value::Int(5)),
            Err(CommandError::UnknownParameter { ref parameter, .. }) if parameter == "shelve_for"
        ));
        assert!(matches!(
            block.apply_parameter("max_shelve_ticks", Value::Int(-1)),
            Err(CommandError::InvalidParameter { ref parameter, .. }) if parameter == "max_shelve_ticks"
        ));
        assert!(matches!(
            block.apply_parameter("priority", Value::Float(1.0)),
            Err(CommandError::ParameterTypeMismatch { ref parameter, .. }) if parameter == "priority"
        ));
        // The sibling's limit tunables still apply.
        block
            .apply_parameter("high_limit", Value::Float(100.0))
            .unwrap();
    }

    #[test]
    fn incompatible_state_is_a_named_error() {
        let mut block = component();

        // A missing field is named.
        assert!(matches!(
            block.restore_state(&StateMap::new()),
            Err(StateError::MissingField { ref field, .. }) if field == "state"
        ));

        // A negative timer is rejected, not clamped.
        let mut state = block.capture_state();
        state.insert("shelve_elapsed", Value::Int(-1));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::InvalidValue { ref field, .. }) if field == "shelve_elapsed"
        ));

        // A negative parameter is rejected likewise.
        let mut state = block.capture_state();
        state.insert("max_shelve_ticks", Value::Int(-1));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::InvalidValue { ref field, .. }) if field == "max_shelve_ticks"
        ));

        // A field the kind never captured is rejected, not ignored.
        let mut state = block.capture_state();
        state.insert("shelve_for", Value::Int(3));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::UnknownField { ref field, .. }) if field == "shelve_for"
        ));
    }

    #[test]
    fn builds_from_parameter_map_naming_failures() {
        let parameters: Parameters = [
            ("low_limit".to_string(), Value::Int(10)),
            ("high_limit".to_string(), Value::Int(90)),
            ("hysteresis".to_string(), Value::Float(5.0)),
            ("max_shelve_ticks".to_string(), Value::Int(5)),
            ("priority".to_string(), Value::Int(1)),
            ("class".to_string(), Value::Int(2)),
            ("response_ticks".to_string(), Value::Int(30)),
        ]
        .into_iter()
        .collect();
        let instance = ComponentInstance {
            id: ComponentId(13),
            kind: ManagedLatchingAlarm::KIND.to_string(),
            parameters,
            ports: BTreeMap::new(),
        };
        let block =
            ManagedLatchingAlarm::from_parameters("mlal", io_all(), &instance.parameters).unwrap();
        assert_eq!(block.config.max_shelve_ticks, 5);
        assert_eq!(block.config.priority, 1);
        assert_eq!(block.config.class, 2);
        assert_eq!(block.config.response_ticks, 30);

        // A missing managed parameter is named.
        let mut incomplete = instance.parameters.clone();
        incomplete.remove("max_shelve_ticks");
        assert!(matches!(
            ManagedLatchingAlarm::from_parameters("mlal", io_all(), &incomplete).unwrap_err(),
            ParameterError::Missing { ref parameter, .. } if parameter == "max_shelve_ticks"
        ));

        // A negative one is named and invalid.
        let mut invalid = instance.parameters.clone();
        invalid.insert("response_ticks".to_string(), Value::Int(-1));
        assert!(matches!(
            ManagedLatchingAlarm::from_parameters("mlal", io_all(), &invalid).unwrap_err(),
            ParameterError::Invalid { ref parameter, .. } if parameter == "response_ticks"
        ));
    }

    #[test]
    fn describes_itself() {
        let descriptor = component().describe();
        assert_eq!(descriptor.name, "mlal");
        assert_eq!(descriptor.kind, ManagedLatchingAlarm::KIND);
        assert_eq!(descriptor.label, "mlal");
        // The sibling's vocabulary plus the managed surface: `in`,
        // `ack`, `shelve`, `oos`, `suppress` in; `alarm`,
        // `unacknowledged`, `shelved`, `suppressed`, `out_of_service`
        // out — `in` ProcessValue, everything else Status.
        let port = |name: &str, direction, kind, role| PortDescriptor {
            name: name.to_string(),
            direction,
            kind,
            role,
            point: None,
        };
        assert_eq!(
            descriptor.ports,
            [
                port(
                    "in",
                    Direction::In,
                    ValueKind::Float,
                    Some(PortRole::ProcessValue)
                ),
                port(
                    "ack",
                    Direction::In,
                    ValueKind::Bool,
                    Some(PortRole::Status)
                ),
                port(
                    "shelve",
                    Direction::In,
                    ValueKind::Bool,
                    Some(PortRole::Status)
                ),
                port(
                    "oos",
                    Direction::In,
                    ValueKind::Bool,
                    Some(PortRole::Status)
                ),
                port(
                    "suppress",
                    Direction::In,
                    ValueKind::Bool,
                    Some(PortRole::Status)
                ),
                port(
                    "alarm",
                    Direction::Out,
                    ValueKind::Bool,
                    Some(PortRole::Status)
                ),
                port(
                    "unacknowledged",
                    Direction::Out,
                    ValueKind::Bool,
                    Some(PortRole::Status)
                ),
                port(
                    "shelved",
                    Direction::Out,
                    ValueKind::Bool,
                    Some(PortRole::Status)
                ),
                port(
                    "suppressed",
                    Direction::Out,
                    ValueKind::Bool,
                    Some(PortRole::Status)
                ),
                port(
                    "out_of_service",
                    Direction::Out,
                    ValueKind::Bool,
                    Some(PortRole::Status)
                ),
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
                describe::parameter("low_limit", ValueKind::Float, Some(describe::FINITE_F64)),
                describe::parameter("high_limit", ValueKind::Float, Some(describe::FINITE_F64)),
                describe::parameter(
                    "hysteresis",
                    ValueKind::Float,
                    Some(describe::NONNEGATIVE_F64)
                ),
                int_parameter("max_shelve_ticks"),
                int_parameter("priority"),
                int_parameter("class"),
                int_parameter("response_ticks"),
            ]
        );
    }

    #[test]
    fn an_unbound_shelve_describes_without_the_port() {
        let mut io = io_all();
        io.shelve = None;
        let block = ManagedLatchingAlarm::new("mlal", io, limits(), config(5)).unwrap();
        let descriptor = block.describe();
        assert!(descriptor.ports.iter().all(|port| port.name != "shelve"));
        assert_eq!(descriptor.ports.len(), 9);
    }
}
