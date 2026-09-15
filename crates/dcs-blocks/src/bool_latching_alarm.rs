//! Bool latching alarm: the two-flag alarm lifecycle for Bool-sourced
//! alarm conditions — the Bool-input sibling of `latching-alarm`.

use crate::describe;
use crate::params::{ParameterError, Parameters};
use dcs_core::{ComponentDescriptor, PointId, PortRole, Sample, StateError, StateMap, Tick, Value};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// A Bool latching alarm: reads a boolean `in` point and a boolean
/// `ack` point, and drives a boolean `alarm` output plus a boolean
/// `unacknowledged` latch — the [`LatchingAlarm`](crate::LatchingAlarm)
/// vocabulary carried onto the Bool-sourced alarms decision 43 maps:
/// a motor's `fault` flag, a failover-select's `backup_active`, a
/// pump-group's `none_available`/`all_faulted`, and field Bool points
/// like station power failure or equipment-fault conditions.
///
/// `alarm` is the Bool analogue of the standing limit state: it
/// follows `in` exactly — asserted while `in` reads `true`, cleared
/// when it reads `false`. No hysteresis applies; there is no deadband
/// on a Boolean condition.
///
/// **Acknowledgment rule** — the same level-sensitive, ack-dominates
/// rule the Float sibling documents: `unacknowledged` latches on a
/// fresh assertion — a scan whose `in` reads `true` while the previous
/// scan read `false`, the false-to-true edge — and clears on every
/// scan `ack` reads `true`, even while `alarm` still stands. A
/// permanently asserted `ack` point suppresses the flag entirely — a
/// trip arriving on a scan whose `ack` reads `true` does not latch —
/// so the operator point, typically a writable internal point the page
/// commands, should be pulsed or written `false` between
/// acknowledgments. Acknowledging never clears `alarm`: it follows
/// `in`, and an assertion that returns after acknowledgment latches
/// again.
///
/// Both outputs carry the worst of the two inputs' qualities, so a
/// degraded condition or a degraded `ack` marks the indications it
/// drives; the values still evaluate — a `Bad` `in` reading `true`
/// asserts and latches, a `Bad` `ack` reading `true` clears — the
/// degraded flag reporting the untrusted read rather than hiding it.
///
/// Declared I/O: `in` (`In`, `Bool`), `ack` (`In`, `Bool`), `alarm`
/// (`Out`, `Bool`), `unacknowledged` (`Out`, `Bool`).
///
/// The kind declares no parameters: the Bool analogue of the standing
/// limit state has no limits and no hysteresis to tune.
///
/// **Recorded choice (decision 43):** the Bool sibling is its own
/// kind, not a `point_kind`-dispatched `latching-alarm` variant —
/// the sibling's contract differs in the `in` port's value kind *and*
/// carries no parameter set, so one kind string would name two
/// different port signatures and parameter vocabularies. A dedicated
/// kind keeps each `kind` naming exactly one contract, keeps the
/// spec↔descriptor drift guard one-to-one, and lets `dcs-build` check
/// the Bool `in` at connect time.
#[derive(Debug)]
pub struct BoolLatchingAlarm {
    name: String,
    input: PointId,
    ack: PointId,
    alarm: PointId,
    unacknowledged: PointId,
    /// The `in` level observed on the previous scan — the edge the
    /// latch arms on. `false` before the first scan, so a `true`
    /// first read is a fresh assertion — the same convention
    /// [`EdgeTrigger`](crate::EdgeTrigger)'s edge detection follows.
    state: bool,
    /// The acknowledgment latch: set on a fresh assertion, cleared
    /// while `ack` reads `true`.
    latched: bool,
}

impl BoolLatchingAlarm {
    /// The component-kind string the model-driven registry maps onto
    /// this type's constructor.
    pub const KIND: &'static str = "bool-latching-alarm";

    /// Builds the component from explicit points; the latch starts
    /// cleared and the tracked `in` level starts `false`.
    pub fn new(
        name: impl Into<String>,
        input: PointId,
        ack: PointId,
        alarm: PointId,
        unacknowledged: PointId,
    ) -> Self {
        Self {
            name: name.into(),
            input,
            ack,
            alarm,
            unacknowledged,
            state: false,
            latched: false,
        }
    }

    /// Builds the component from a plant-model parameter map; the kind
    /// declares no parameters, so the map is unread — an unexpected key
    /// is the `dcs-build` spec's `UnknownParameter` case at composition,
    /// not a construction failure.
    pub fn from_parameters(
        name: impl Into<String>,
        input: PointId,
        ack: PointId,
        alarm: PointId,
        unacknowledged: PointId,
        _parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        Ok(Self::new(name, input, ack, alarm, unacknowledged))
    }
}

impl Component for BoolLatchingAlarm {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        vec![
            IoRequirement::input::<bool>("in", self.input),
            IoRequirement::input::<bool>("ack", self.ack),
            IoRequirement::output::<bool>("alarm", self.alarm),
            IoRequirement::output::<bool>("unacknowledged", self.unacknowledged),
        ]
    }

    fn step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError> {
        let sample = io.read_typed::<bool>(self.input)?;
        let ack = io.read_typed::<bool>(self.ack)?;
        let fresh_trip = sample.value && !self.state;
        self.state = sample.value;
        self.latched = (self.latched || fresh_trip) && !ack.value;
        let quality = sample.quality.merge(ack.quality);
        io.write_sample(
            self.alarm,
            Sample::new(Value::Bool(self.state), quality, tick),
        )?;
        io.write_sample(
            self.unacknowledged,
            Sample::new(Value::Bool(self.latched), quality, tick),
        )?;
        Ok(())
    }

    /// Describes the block with the sibling's roles: `in` is the Bool
    /// alarm condition, `ack` the operator's clearing command, `alarm`
    /// the reported standing state, `unacknowledged` the reported
    /// latch. The kind declares no parameters.
    fn describe(&self) -> ComponentDescriptor {
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
            vec![],
        )
    }

    /// Captures the tracked `in` level — so a restored standby does not
    /// re-latch a still-asserted, already-acknowledged alarm — and the
    /// acknowledgment latch, so a tracking standby inherits
    /// unacknowledged alarms; the Bool analogue of the sibling's
    /// `state`/`unacknowledged` vocabulary.
    fn capture_state(&self) -> StateMap {
        let mut state = StateMap::new();
        state.insert("state", Value::Bool(self.state));
        state.insert("unacknowledged", Value::Bool(self.latched));
        state
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        state.ensure_known_fields(&self.name, &["state", "unacknowledged"])?;
        let restored = state.require_bool(&self.name, "state")?;
        let latched = state.require_bool(&self.name, "unacknowledged")?;
        self.state = restored;
        self.latched = latched;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::TestIo;
    use dcs_core::{Direction, PortDescriptor, Quality, QualityReason, ValueKind};
    use dcs_model::{ComponentId, ComponentInstance};
    use std::collections::BTreeMap;

    const IN: PointId = PointId(150);
    const ACK: PointId = PointId(151);
    const ALARM: PointId = PointId(152);
    const UNACK: PointId = PointId(153);

    fn component() -> BoolLatchingAlarm {
        BoolLatchingAlarm::new("bal", IN, ACK, ALARM, UNACK)
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
    fn step(block: &mut BoolLatchingAlarm, io: &TestIo, input: bool, ack: bool, tick: u64) {
        io.feed(IN, Sample::good(Value::Bool(input), Tick(tick)));
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
    fn assertion_asserts_alarm_and_latches_unacknowledged() {
        let mut block = component();
        let io = io();

        // Condition clear: neither output asserted.
        step(&mut block, &io, false, false, 1);
        assert!(!alarmed(&io));
        assert!(!unacknowledged(&io));

        // A fresh assertion trips both outputs.
        step(&mut block, &io, true, false, 2);
        assert!(alarmed(&io));
        assert!(unacknowledged(&io));

        // The standing alarm follows `in` while it holds — a held level
        // is not a fresh assertion, so the latch does not re-arm once
        // cleared on a held `ack`.
        step(&mut block, &io, true, false, 3);
        assert!(alarmed(&io));
        assert!(unacknowledged(&io));
    }

    #[test]
    fn latch_holds_after_the_alarm_clears() {
        let mut block = component();
        let io = io();

        step(&mut block, &io, true, false, 1);
        assert!(alarmed(&io));
        assert!(unacknowledged(&io));

        // The condition clears: `alarm` follows `in` down — no
        // hysteresis — but the latch stands until the operator
        // acknowledges.
        step(&mut block, &io, false, false, 2);
        assert!(!alarmed(&io));
        assert!(unacknowledged(&io));
    }

    #[test]
    fn ack_clears_the_latch_while_the_alarm_still_stands() {
        let mut block = component();
        let io = io();

        step(&mut block, &io, true, false, 1);
        assert!(alarmed(&io));
        assert!(unacknowledged(&io));

        // Acknowledged mid-alarm: the latch clears while the standing
        // indication still reports asserted.
        step(&mut block, &io, true, true, 2);
        assert!(alarmed(&io));
        assert!(!unacknowledged(&io));

        // Releasing ack re-arms nothing — the standing alarm stays
        // acknowledged until `in` asserts afresh.
        step(&mut block, &io, true, false, 3);
        assert!(alarmed(&io));
        assert!(!unacknowledged(&io));

        // Deasserting clears the alarm with nothing left latched.
        step(&mut block, &io, false, false, 4);
        assert!(!alarmed(&io));
        assert!(!unacknowledged(&io));

        // A fresh assertion after acknowledgment latches again.
        step(&mut block, &io, true, false, 5);
        assert!(alarmed(&io));
        assert!(unacknowledged(&io));
    }

    #[test]
    fn ack_clears_the_latch_after_the_alarm_cleared() {
        let mut block = component();
        let io = io();

        // Trip and let the condition clear on its own: the latch
        // stands.
        step(&mut block, &io, true, false, 1);
        step(&mut block, &io, false, false, 2);
        assert!(!alarmed(&io));
        assert!(unacknowledged(&io));

        // The ack still clears the latch — a fleeting alarm stays
        // visible until the operator sees it.
        step(&mut block, &io, false, true, 3);
        assert!(!alarmed(&io));
        assert!(!unacknowledged(&io));
    }

    #[test]
    fn held_ack_dominates_a_simultaneous_trip() {
        let mut block = component();
        let io = io();

        // Documented rule: a scan whose `ack` reads true does not
        // latch, a trip arriving that scan included — a permanently
        // asserted ack suppresses the flag while `alarm` still reports
        // the condition.
        step(&mut block, &io, true, true, 1);
        assert!(alarmed(&io));
        assert!(!unacknowledged(&io));

        // Releasing ack while the condition stands re-latches nothing —
        // the assertion was never fresh under the held ack.
        step(&mut block, &io, true, false, 2);
        assert!(alarmed(&io));
        assert!(!unacknowledged(&io));

        // A fresh assertion — the condition clearing and returning —
        // latches once ack no longer stands.
        step(&mut block, &io, false, false, 3);
        step(&mut block, &io, true, false, 4);
        assert!(alarmed(&io));
        assert!(unacknowledged(&io));
    }

    #[test]
    fn bad_quality_on_either_input_marks_both_outputs() {
        let mut block = component();
        let io = io();

        // A Bad `in` still asserts — the indication is degraded, not
        // hidden — and both outputs carry the input's quality.
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

        // A Bad `ack` is worst-of merged: its value still clears the
        // latch, and both outputs are marked.
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
        io.feed(IN, Sample::good(Value::Bool(true), Tick(3)));
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
    fn capture_restore_mid_latch_continues_identically() {
        let mut block = component();
        let active_io = io();

        // Asserted and still unacknowledged — checkpoint mid-latch.
        step(&mut block, &active_io, true, false, 1);
        let state = block.capture_state();
        assert_eq!(state.get("state"), Some(Value::Bool(true)));
        assert_eq!(state.get("unacknowledged"), Some(Value::Bool(true)));

        // The standby inherits the latch and the tracked level: the
        // still-asserted input is not a fresh edge, so the same input
        // sequence produces the same outputs.
        let mut standby = component();
        standby.restore_state(&state).unwrap();
        assert_eq!(standby.capture_state(), state);
        let standby_io = io();
        step(&mut standby, &standby_io, true, false, 2);
        assert!(alarmed(&standby_io));
        assert!(unacknowledged(&standby_io));
        step(&mut standby, &standby_io, true, true, 3);
        assert!(alarmed(&standby_io));
        assert!(!unacknowledged(&standby_io));
    }

    #[test]
    fn restored_standing_alarm_does_not_re_latch() {
        let mut block = component();
        let active_io = io();

        // Trip, acknowledge while the condition still stands, then
        // checkpoint: the standing alarm is alarmed-but-acknowledged.
        step(&mut block, &active_io, true, false, 1);
        step(&mut block, &active_io, true, true, 2);
        let state = block.capture_state();
        assert_eq!(state.get("state"), Some(Value::Bool(true)));
        assert_eq!(state.get("unacknowledged"), Some(Value::Bool(false)));

        // A standby restoring mid-assertion must not read the held
        // level as a fresh edge — the acknowledged alarm stays
        // acknowledged across the switchover.
        let mut standby = component();
        standby.restore_state(&state).unwrap();
        let standby_io = io();
        step(&mut standby, &standby_io, true, false, 3);
        assert!(alarmed(&standby_io));
        assert!(!unacknowledged(&standby_io));
    }

    #[test]
    fn incompatible_state_is_a_named_error() {
        let mut block = component();

        // A missing field is named.
        assert!(matches!(
            block.restore_state(&StateMap::new()),
            Err(StateError::MissingField { ref field, .. }) if field == "state"
        ));

        // A wrong-typed field is named.
        let mut state = StateMap::new();
        state.insert("state", Value::Int(1));
        state.insert("unacknowledged", Value::Bool(true));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::IncompatibleField { ref field, .. }) if field == "state"
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
        // The kind declares no parameters: the empty map builds, and a
        // stray key is unread here — undeclared keys are the spec's
        // `UnknownParameter` case at composition time.
        let instance = ComponentInstance {
            id: ComponentId(13),
            kind: BoolLatchingAlarm::KIND.to_string(),
            parameters: Parameters::new(),
            ports: BTreeMap::new(),
        };
        let block =
            BoolLatchingAlarm::from_parameters("bal", IN, ACK, ALARM, UNACK, &instance.parameters)
                .unwrap();
        assert!(!block.state);
        assert!(!block.latched);
    }

    #[test]
    fn describes_itself() {
        let descriptor = component().describe();
        assert_eq!(descriptor.name, "bal");
        assert_eq!(descriptor.kind, BoolLatchingAlarm::KIND);
        assert_eq!(descriptor.label, "bal");
        // The sibling's vocabulary exactly: `in`/`ack`/`alarm`/
        // `unacknowledged`, with `in` role-hinted ProcessValue and the
        // rest Status — `in` is Bool where the sibling's is Float.
        assert_eq!(
            descriptor.ports,
            [
                PortDescriptor {
                    name: "in".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Bool,
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
        // Drift guard: the kind declares no parameters and
        // `from_parameters` reads none.
        assert!(descriptor.parameters.is_empty());
    }

    #[test]
    fn identical_inputs_produce_identical_outputs() {
        let run = || {
            let mut block = component();
            let io = io();
            let mut trace = Vec::new();
            for (tick, (input, ack)) in [
                (false, false),
                (true, false),
                (false, false),
                (false, true),
                (true, false),
            ]
            .into_iter()
            .enumerate()
            {
                step(&mut block, &io, input, ack, tick as u64 + 1);
                trace.push((io.written(ALARM).unwrap(), io.written(UNACK).unwrap()));
            }
            trace
        };
        assert_eq!(run(), run());
    }
}
