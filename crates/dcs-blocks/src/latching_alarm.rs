//! Latching alarm: high/low limit checking with hysteresis plus an
//! operator-acknowledgment latch.

use crate::alarm_monitor::{Alarm, AlarmLimits};
use crate::describe;
use crate::params::{ParameterError, Parameters};
use crate::rationalization::Rationalization;
use dcs_core::{
    CommandError, ComponentDescriptor, PointId, PortRole, Quality, QualityReason, Sample,
    StateError, StateMap, Tick, Value, ValueKind,
};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// A latching alarm: reads an analog `In` point and a boolean `ack`
/// point, and drives a boolean `alarm` output plus a boolean
/// `unacknowledged` latch.
///
/// `alarm` reports the limit state following
/// [`AlarmMonitor`](crate::AlarmMonitor)'s documented hysteresis rule
/// exactly: the high alarm trips when the input reaches `high_limit` and
/// clears once it falls strictly below `high_limit - hysteresis`; the low
/// alarm trips at `low_limit` and clears once the input rises strictly
/// above `low_limit + hysteresis`. Inside a deadband the state holds, a
/// `NaN` input satisfies no comparison and also holds, and a plunge past
/// the opposite limit transitions the alarm directly.
///
/// **Acknowledgment rule:** `unacknowledged` latches on a fresh trip —
/// a scan whose evaluated limit state becomes `High` or `Low` from a
/// *different* state, a direct high-to-low transition included — and
/// clears on every scan `ack` reads `true`, even while the alarm still
/// stands. `ack` is level-sensitive and dominates: a trip arriving on a
/// scan whose `ack` reads `true` does not latch, so a permanently
/// asserted `ack` point suppresses the flag entirely — the operator
/// point, typically a writable internal point the page commands, should
/// be pulsed or written `false` between acknowledgments. Acknowledging
/// never clears `alarm`: the limit state stands until hysteresis clears
/// it, and an alarm that trips again after acknowledgment latches again.
///
/// Both outputs carry the worst of the two inputs' qualities, so a
/// degraded measurement or a degraded `ack` marks the indications it
/// drives; a `NaN` input additionally merges `Bad(DeviceFault)`,
/// matching the crate's non-finite-input convention.
///
/// Declared I/O: `in` (`In`, `Float`), `ack` (`In`, `Bool`), `alarm`
/// (`Out`, `Bool`), `unacknowledged` (`Out`, `Bool`).
///
/// Parameters: shared with [`AlarmMonitor`](crate::AlarmMonitor) —
/// `low_limit` and `high_limit` required finite `Float` or losslessly
/// representable `Int`, `low_limit < high_limit`; `hysteresis` optional,
/// finite and non-negative, default `0.0` — plus the decision-70
/// rationalization codes `priority`, `class`, and `response_ticks`,
/// required non-negative `Int`s. The instance's `rationalization` prose
/// block is a separate obligation the registry checks where kind and
/// instance meet.
#[derive(Debug)]
pub struct LatchingAlarm {
    name: String,
    input: PointId,
    ack: PointId,
    alarm: PointId,
    unacknowledged: PointId,
    limits: AlarmLimits,
    rationalization: Rationalization,
    state: Alarm,
    /// The acknowledgment latch: set on a fresh trip, cleared while
    /// `ack` reads `true`.
    latched: bool,
}

impl LatchingAlarm {
    /// The component-kind string the model-driven registry maps onto this
    /// type's constructor.
    pub const KIND: &'static str = "latching-alarm";

    /// Builds the component from explicit points, limits, and the
    /// declared rationalization codes, or reports the limits'
    /// inconsistency as a [`ParameterError`].
    pub fn new(
        name: impl Into<String>,
        input: PointId,
        ack: PointId,
        alarm: PointId,
        unacknowledged: PointId,
        limits: AlarmLimits,
        rationalization: Rationalization,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        Ok(Self {
            limits: AlarmLimits::checked(&name, limits)?,
            name,
            input,
            ack,
            alarm,
            unacknowledged,
            rationalization,
            state: Alarm::Clear,
            latched: false,
        })
    }

    /// Builds the component from a plant-model parameter map, reading the
    /// parameters listed on the type's docs — the limits plus the
    /// required rationalization codes.
    pub fn from_parameters(
        name: impl Into<String>,
        input: PointId,
        ack: PointId,
        alarm: PointId,
        unacknowledged: PointId,
        parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        let limits = AlarmLimits::from_parameters(&name, parameters)?;
        let rationalization = Rationalization::from_parameters(&name, parameters)?;
        Self::new(
            name,
            input,
            ack,
            alarm,
            unacknowledged,
            limits,
            rationalization,
        )
    }
}

impl Component for LatchingAlarm {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        vec![
            IoRequirement::input::<f64>("in", self.input),
            IoRequirement::input::<bool>("ack", self.ack),
            IoRequirement::output::<bool>("alarm", self.alarm),
            IoRequirement::output::<bool>("unacknowledged", self.unacknowledged),
        ]
    }

    fn step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError> {
        let sample = io.read_typed::<f64>(self.input)?;
        let ack = io.read_typed::<bool>(self.ack)?;
        let pv = sample.value;
        let previous = self.state;
        self.state = previous.evaluate(pv, self.limits);
        let fresh_trip = self.state != Alarm::Clear && self.state != previous;
        self.latched = (self.latched || fresh_trip) && !ack.value;
        let mut quality = sample.quality.merge(ack.quality);
        if pv.is_nan() {
            quality = quality.merge(Quality::Bad(QualityReason::DeviceFault));
        }
        io.write_sample(
            self.alarm,
            Sample::new(Value::Bool(self.state != Alarm::Clear), quality, tick),
        )?;
        io.write_sample(
            self.unacknowledged,
            Sample::new(Value::Bool(self.latched), quality, tick),
        )?;
        Ok(())
    }

    /// Describes the block: `in` is the measured process value checked
    /// against the limits, `ack` the operator's clearing command,
    /// `alarm` the reported trip state, `unacknowledged` the reported
    /// latch; the shared limit and hysteresis parameters plus the
    /// decision-70 rationalization codes `from_parameters` reads.
    fn describe(&self) -> ComponentDescriptor {
        let mut parameters = vec![
            describe::parameter("low_limit", ValueKind::Float, Some(describe::FINITE_F64)),
            describe::parameter("high_limit", ValueKind::Float, Some(describe::FINITE_F64)),
            describe::parameter(
                "hysteresis",
                ValueKind::Float,
                Some(describe::NONNEGATIVE_F64),
            ),
        ];
        parameters.extend(Rationalization::parameters());
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &[
                ("in", PortRole::ProcessValue),
                ("ack", PortRole::Status),
                ("alarm", PortRole::Status),
                ("unacknowledged", PortRole::Status),
            ],
            parameters,
        )
    }

    /// Tunes a declared parameter at the scan boundary — the limits with
    /// the same invariants [`AlarmMonitor`](crate::AlarmMonitor)
    /// accepts, the rationalization codes as non-negative `Int`s.
    fn apply_parameter(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        match parameter {
            "priority" | "class" | "response_ticks" => {
                self.rationalization = self.rationalization.tune(&self.name, parameter, value)?;
            }
            _ => self.limits = self.limits.tune(&self.name, parameter, value)?,
        }
        Ok(())
    }

    /// Reports the declared limit tuning and the rationalization codes —
    /// the same fields [`capture_state`](Self::capture_state)
    /// checkpoints, so the faceplate and a tracking standby read one
    /// vocabulary.
    fn report_parameters(&self) -> StateMap {
        let mut parameters = StateMap::new();
        parameters.insert("low_limit", Value::Float(self.limits.low));
        parameters.insert("high_limit", Value::Float(self.limits.high));
        parameters.insert("hysteresis", Value::Float(self.limits.hysteresis));
        self.rationalization.report(&mut parameters);
        parameters
    }

    /// Captures the limit state, the acknowledgment latch — so a
    /// tracking standby inherits unacknowledged alarms — and the tuned
    /// parameters, sharing [`AlarmMonitor`](crate::AlarmMonitor)'s
    /// `state` encoding.
    fn capture_state(&self) -> StateMap {
        let mut state = self.report_parameters();
        state.insert("state", Value::Int(self.state.code()));
        state.insert("unacknowledged", Value::Bool(self.latched));
        state
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        state.ensure_known_fields(
            &self.name,
            &[
                "state",
                "unacknowledged",
                "low_limit",
                "high_limit",
                "hysteresis",
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
        self.limits = limits;
        self.rationalization = Rationalization::restore(&self.name, state)?;
        self.state = restored;
        self.latched = latched;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::TestIo;
    use dcs_core::{Direction, ParameterDescriptor, PortDescriptor, Quality, QualityReason};
    use dcs_model::{ComponentId, ComponentInstance};
    use std::collections::BTreeMap;

    const IN: PointId = PointId(140);
    const ACK: PointId = PointId(141);
    const ALARM: PointId = PointId(142);
    const UNACK: PointId = PointId(143);

    /// The rationalization codes the tests build against.
    const RATIONALIZATION: Rationalization = Rationalization {
        priority: 1,
        class: 2,
        response_ticks: 30,
    };

    /// Limits 10 <= low, 90 <= high with a 5-unit hysteresis.
    fn component() -> LatchingAlarm {
        LatchingAlarm::new(
            "lal",
            IN,
            ACK,
            ALARM,
            UNACK,
            AlarmLimits {
                low: 10.0,
                high: 90.0,
                hysteresis: 5.0,
            },
            RATIONALIZATION,
        )
        .unwrap()
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
                ALARM,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
            (
                UNACK,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
        ])
    }

    /// Feeds `in`/`ack` and steps once.
    fn step(block: &mut LatchingAlarm, io: &TestIo, pv: f64, ack: bool, tick: u64) {
        io.feed(IN, Sample::good(Value::Float(pv), Tick(tick)));
        io.feed(ACK, Sample::good(Value::Bool(ack), Tick(tick)));
        block.step(io, Tick(tick)).unwrap();
    }

    fn alarmed(io: &TestIo) -> bool {
        io.written(ALARM).unwrap().value == Value::Bool(true)
    }

    fn unacknowledged(io: &TestIo) -> bool {
        io.written(UNACK).unwrap().value == Value::Bool(true)
    }

    #[test]
    fn trip_asserts_alarm_and_unacknowledged() {
        let mut block = component();
        let io = io();

        // Inside the limits: neither output asserted.
        step(&mut block, &io, 50.0, false, 1);
        assert!(!alarmed(&io));
        assert!(!unacknowledged(&io));

        // Reaching the high limit trips both outputs.
        step(&mut block, &io, 90.0, false, 2);
        assert!(alarmed(&io));
        assert!(unacknowledged(&io));

        // Reaching the low limit does the same.
        step(&mut block, &io, 50.0, false, 3);
        step(&mut block, &io, 10.0, false, 4);
        assert!(alarmed(&io));
        assert!(unacknowledged(&io));
    }

    #[test]
    fn latch_holds_after_the_alarm_clears() {
        let mut block = component();
        let io = io();

        step(&mut block, &io, 95.0, false, 1);
        assert!(alarmed(&io));
        assert!(unacknowledged(&io));

        // The input recedes strictly below high - hysteresis: the alarm
        // clears but the latch stands — the operator has not seen it.
        step(&mut block, &io, 84.9, false, 2);
        assert!(!alarmed(&io));
        assert!(unacknowledged(&io));
    }

    #[test]
    fn ack_clears_the_latch_while_the_alarm_still_stands() {
        let mut block = component();
        let io = io();

        step(&mut block, &io, 95.0, false, 1);
        assert!(alarmed(&io));
        assert!(unacknowledged(&io));

        // Acknowledged mid-trip: the latch clears while the limit state
        // still reports alarmed.
        step(&mut block, &io, 95.0, true, 2);
        assert!(alarmed(&io));
        assert!(!unacknowledged(&io));

        // Releasing ack re-arms nothing — the standing alarm stays
        // acknowledged until it trips afresh.
        step(&mut block, &io, 95.0, false, 3);
        assert!(alarmed(&io));
        assert!(!unacknowledged(&io));

        // Inside the deadband the acknowledged alarm still stands; a
        // retreat below high - hysteresis clears it.
        step(&mut block, &io, 87.0, false, 4);
        assert!(alarmed(&io));
        step(&mut block, &io, 84.9, false, 5);
        assert!(!alarmed(&io));
        assert!(!unacknowledged(&io));

        // A fresh trip after acknowledgment latches again.
        step(&mut block, &io, 92.0, false, 6);
        assert!(alarmed(&io));
        assert!(unacknowledged(&io));
    }

    #[test]
    fn ack_clears_the_latch_after_the_alarm_cleared() {
        let mut block = component();
        let io = io();

        // Trip and let the alarm clear on its own: the latch stands.
        step(&mut block, &io, 95.0, false, 1);
        step(&mut block, &io, 50.0, false, 2);
        assert!(!alarmed(&io));
        assert!(unacknowledged(&io));

        // The ack still clears the latch — a fleeting alarm stays
        // visible until the operator sees it.
        step(&mut block, &io, 50.0, true, 3);
        assert!(!alarmed(&io));
        assert!(!unacknowledged(&io));
    }

    #[test]
    fn held_ack_dominates_a_simultaneous_trip() {
        let mut block = component();
        let io = io();

        // Documented rule: a scan whose `ack` reads true does not latch,
        // a trip arriving that scan included — a permanently asserted
        // ack suppresses the flag while `alarm` still reports the trip.
        step(&mut block, &io, 95.0, true, 1);
        assert!(alarmed(&io));
        assert!(!unacknowledged(&io));

        // Releasing ack while the alarm stands re-latches nothing.
        step(&mut block, &io, 95.0, false, 2);
        assert!(alarmed(&io));
        assert!(!unacknowledged(&io));

        // A fresh trip — here the direct high-to-low transition —
        // latches once ack no longer stands.
        step(&mut block, &io, 5.0, false, 3);
        assert!(alarmed(&io));
        assert!(unacknowledged(&io));
    }

    #[test]
    fn bad_quality_on_either_input_marks_both_outputs() {
        let mut block = component();
        let io = io();

        // A Bad `in` still trips — the indication is degraded, not
        // hidden — and both outputs carry the input's quality.
        io.feed(
            IN,
            Sample::new(
                Value::Float(95.0),
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

        // A Bad `ack` is worst-of merged: its value still clears the
        // latch, and both outputs are marked.
        io.feed(IN, Sample::good(Value::Float(95.0), Tick(2)));
        io.feed(
            ACK,
            Sample::new(
                Value::Bool(true),
                Quality::Bad(QualityReason::DeviceFault),
                Tick(2),
            ),
        );
        block.step(&io, Tick(2)).unwrap();
        assert!(alarmed(&io));
        assert!(!unacknowledged(&io));
        for point in [ALARM, UNACK] {
            assert_eq!(
                io.written(point).unwrap().quality,
                Quality::Bad(QualityReason::DeviceFault)
            );
        }

        // An Uncertain `ack` degrades without hiding Good elsewhere.
        io.feed(
            ACK,
            Sample::new(
                Value::Bool(false),
                Quality::Uncertain(QualityReason::Stale),
                Tick(3),
            ),
        );
        block.step(&io, Tick(3)).unwrap();
        assert_eq!(
            io.written(UNACK).unwrap().quality,
            Quality::Uncertain(QualityReason::Stale)
        );
    }

    #[test]
    fn nan_input_holds_state_and_marks_bad() {
        let mut block = component();
        let io = io();

        step(&mut block, &io, 95.0, false, 1);
        assert!(alarmed(&io));
        assert!(unacknowledged(&io));

        // A NaN input satisfies no comparison: limit state and latch
        // hold, both outputs marked Bad(DeviceFault).
        step(&mut block, &io, f64::NAN, false, 2);
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
    fn capture_restore_mid_latch_continues_identically() {
        let mut block = component();
        let active_io = io();

        // Tripped high and still unacknowledged — checkpoint mid-latch,
        // with a tuned limit riding along.
        step(&mut block, &active_io, 95.0, false, 1);
        block
            .apply_parameter("high_limit", Value::Float(100.0))
            .unwrap();
        let state = block.capture_state();
        assert_eq!(state.get("state"), Some(Value::Int(1)));
        assert_eq!(state.get("unacknowledged"), Some(Value::Bool(true)));
        assert_eq!(state.get("high_limit"), Some(Value::Float(100.0)));

        // The standby inherits the latch and the tuned limit: the same
        // input sequence produces the same outputs.
        let mut standby = component();
        standby.restore_state(&state).unwrap();
        assert_eq!(standby.capture_state(), state);
        let standby_io = io();
        step(&mut standby, &standby_io, 99.0, false, 2);
        // 99 sits inside the retuned limit, but the captured High state
        // holds through the deadband — and the latch rode along.
        assert!(alarmed(&standby_io));
        assert!(unacknowledged(&standby_io));
        step(&mut standby, &standby_io, 99.0, true, 3);
        assert!(alarmed(&standby_io));
        assert!(!unacknowledged(&standby_io));
    }

    #[test]
    fn incompatible_state_is_a_named_error() {
        let mut block = component();
        let mut state = StateMap::new();
        state.insert("state", Value::Int(7));
        state.insert("unacknowledged", Value::Bool(true));
        state.insert("low_limit", Value::Float(10.0));
        state.insert("high_limit", Value::Float(90.0));
        state.insert("hysteresis", Value::Float(5.0));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::InvalidValue { ref field, .. }) if field == "state"
        ));

        let mut state = StateMap::new();
        state.insert("state", Value::Int(1));
        state.insert("low_limit", Value::Float(10.0));
        state.insert("high_limit", Value::Float(90.0));
        state.insert("hysteresis", Value::Float(5.0));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::MissingField { ref field, .. }) if field == "unacknowledged"
        ));

        // A negative rationalization code in the checkpoint is named —
        // a rejected restore changes nothing.
        let mut state = block.capture_state();
        state.insert("priority", Value::Int(-1));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::InvalidValue { ref field, .. }) if field == "priority"
        ));

        // A field the kind never captured is rejected, not ignored.
        let mut state = block.capture_state();
        state.insert("trip_count", Value::Int(3));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::UnknownField { ref field, .. }) if field == "trip_count"
        ));
    }

    #[test]
    fn builds_from_parameter_map() {
        let parameters: Parameters = [
            ("low_limit".to_string(), Value::Int(10)),
            ("high_limit".to_string(), Value::Int(90)),
            ("hysteresis".to_string(), Value::Float(5.0)),
            ("priority".to_string(), Value::Int(1)),
            ("class".to_string(), Value::Int(2)),
            ("response_ticks".to_string(), Value::Int(30)),
        ]
        .into_iter()
        .collect();
        let instance = ComponentInstance {
            id: ComponentId(12),
            kind: LatchingAlarm::KIND.to_string(),
            parameters,
            rationalization: None,
            ports: BTreeMap::new(),
        };
        let mut block =
            LatchingAlarm::from_parameters("lal", IN, ACK, ALARM, UNACK, &instance.parameters)
                .unwrap();
        assert_eq!(
            block.limits,
            AlarmLimits {
                low: 10.0,
                high: 90.0,
                hysteresis: 5.0,
            }
        );
        assert_eq!(block.rationalization, RATIONALIZATION);

        let io = io();
        step(&mut block, &io, 95.0, false, 1);
        assert!(alarmed(&io));
        assert!(unacknowledged(&io));

        // Every construction failure names the offending parameter.
        assert!(matches!(
            LatchingAlarm::from_parameters("lal", IN, ACK, ALARM, UNACK, &Parameters::new())
                .unwrap_err(),
            ParameterError::Missing { ref parameter, .. } if parameter == "low_limit"
        ));
        let reversed: Parameters = [
            ("low_limit".to_string(), Value::Float(90.0)),
            ("high_limit".to_string(), Value::Float(10.0)),
        ]
        .into_iter()
        .collect();
        assert!(matches!(
            LatchingAlarm::from_parameters("lal", IN, ACK, ALARM, UNACK, &reversed)
                .unwrap_err(),
            ParameterError::Invalid { ref parameter, .. } if parameter == "high_limit"
        ));
        let negative: Parameters = [
            ("low_limit".to_string(), Value::Float(10.0)),
            ("high_limit".to_string(), Value::Float(90.0)),
            ("hysteresis".to_string(), Value::Float(-1.0)),
        ]
        .into_iter()
        .collect();
        assert!(matches!(
            LatchingAlarm::from_parameters("lal", IN, ACK, ALARM, UNACK, &negative)
                .unwrap_err(),
            ParameterError::Invalid { ref parameter, .. } if parameter == "hysteresis"
        ));
        let mistyped: Parameters = [
            ("low_limit".to_string(), Value::Float(10.0)),
            ("high_limit".to_string(), Value::Bool(true)),
        ]
        .into_iter()
        .collect();
        assert!(matches!(
            LatchingAlarm::from_parameters("lal", IN, ACK, ALARM, UNACK, &mistyped)
                .unwrap_err(),
            ParameterError::Invalid { ref parameter, .. } if parameter == "high_limit"
        ));

        // The rationalization codes are required too — a map carrying
        // the limits but no `priority` names it.
        let mut unrationalized = instance.parameters.clone();
        unrationalized.remove("priority");
        assert!(matches!(
            LatchingAlarm::from_parameters("lal", IN, ACK, ALARM, UNACK, &unrationalized)
                .unwrap_err(),
            ParameterError::Missing { ref parameter, .. } if parameter == "priority"
        ));
    }

    #[test]
    fn apply_parameter_tunes_the_limit_parameters() {
        let mut block = component();
        block
            .apply_parameter("high_limit", Value::Float(80.0))
            .unwrap();
        block
            .apply_parameter("hysteresis", Value::Float(2.0))
            .unwrap();
        assert_eq!(block.limits.high, 80.0);
        assert_eq!(block.limits.hysteresis, 2.0);

        // The rationalization codes tune as non-negative Ints.
        block.apply_parameter("priority", Value::Int(3)).unwrap();
        assert_eq!(block.rationalization.priority, 3);
        assert_eq!(
            block.apply_parameter("class", Value::Int(-1)).unwrap_err(),
            CommandError::InvalidParameter {
                component: "lal".to_string(),
                parameter: "class".to_string(),
                detail: "must be non-negative".to_string(),
            }
        );

        let io = io();
        step(&mut block, &io, 85.0, false, 1);
        assert!(alarmed(&io));
        assert!(unacknowledged(&io));

        // An undeclared name is a named rejection; crossing low over
        // high refuses and changes nothing.
        assert_eq!(
            block
                .apply_parameter("delay_ticks", Value::Float(1.0))
                .unwrap_err(),
            CommandError::UnknownParameter {
                component: "lal".to_string(),
                parameter: "delay_ticks".to_string(),
            }
        );
        assert!(matches!(
            block
                .apply_parameter("low_limit", Value::Float(95.0))
                .unwrap_err(),
            CommandError::InvalidParameter {
                ref component,
                ref parameter,
                ..
            } if component == "lal" && parameter == "low_limit"
        ));
        assert_eq!(block.limits.low, 10.0);
    }

    #[test]
    fn describes_itself() {
        let descriptor = component().describe();
        assert_eq!(descriptor.name, "lal");
        assert_eq!(descriptor.kind, LatchingAlarm::KIND);
        assert_eq!(descriptor.label, "lal");
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
                    name: "ack".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Status),
                    point: None,
                },
                PortDescriptor {
                    name: "alarm".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Status),
                    point: None,
                },
                PortDescriptor {
                    name: "unacknowledged".to_string(),
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
                    name: "low_limit".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::FINITE_F64),
                },
                ParameterDescriptor {
                    name: "high_limit".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::FINITE_F64),
                },
                ParameterDescriptor {
                    name: "hysteresis".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::NONNEGATIVE_F64),
                },
                ParameterDescriptor {
                    name: "priority".to_string(),
                    kind: ValueKind::Int,
                    range: Some(describe::NONNEGATIVE_INT),
                },
                ParameterDescriptor {
                    name: "class".to_string(),
                    kind: ValueKind::Int,
                    range: Some(describe::NONNEGATIVE_INT),
                },
                ParameterDescriptor {
                    name: "response_ticks".to_string(),
                    kind: ValueKind::Int,
                    range: Some(describe::NONNEGATIVE_INT),
                },
            ]
        );
    }
}
