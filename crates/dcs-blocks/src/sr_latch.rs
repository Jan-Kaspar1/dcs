//! Set/reset latch: a Boolean state set by `set`, cleared by `reset`,
//! reset-dominant on simultaneous assertion.

use crate::describe;
use crate::params::{ParameterError, Parameters};
use dcs_core::{ComponentDescriptor, PointId, PortRole, Sample, StateError, StateMap, Tick, Value};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// A set/reset latch: `out` holds the latched state — asserted by `set`,
/// cleared by `reset`.
///
/// **Precedence rule (reset-dominant):** while `reset` reads `true` the
/// latch clears whatever `set` reads — a scan asserting both inputs
/// deasserts `out`, the conservative choice for a safety-adjacent
/// primitive: an unterminated set pulse cannot pin the output against a
/// standing reset. The recorded rule makes the latch an `SR` bistable in
/// the IEC sense read literally as *reset* wins; a deployment needing
/// set-dominance composes one by swapping the wired points.
///
/// **Quality rule (hold):** while `set` or `reset` reads with non-
/// [`Quality::Good`](dcs_core::Quality::Good) quality the latch holds
/// where it stands — an input that cannot be trusted neither sets nor
/// clears — while `out` keeps reporting the held state stamped with the
/// worst of the two inputs' qualities, so a degraded control signal
/// marks the output it failed to move.
///
/// Declared I/O: `set` (`In`, `Bool`), `reset` (`In`, `Bool`), `out`
/// (`Out`, `Bool`).
///
/// The kind declares no parameters.
#[derive(Debug)]
pub struct SrLatch {
    name: String,
    set: PointId,
    reset: PointId,
    output: PointId,
    /// The latched state `out` reports.
    state: bool,
}

impl SrLatch {
    /// The component-kind string the model-driven registry maps onto
    /// this type's constructor.
    pub const KIND: &'static str = "sr-latch";

    /// Builds the component from explicit points; the latch starts
    /// cleared.
    pub fn new(name: impl Into<String>, set: PointId, reset: PointId, output: PointId) -> Self {
        Self {
            name: name.into(),
            set,
            reset,
            output,
            state: false,
        }
    }

    /// Builds the component from a plant-model parameter map; the kind
    /// declares no parameters, so the map is unread.
    pub fn from_parameters(
        name: impl Into<String>,
        set: PointId,
        reset: PointId,
        output: PointId,
        _parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        Ok(Self::new(name, set, reset, output))
    }
}

impl Component for SrLatch {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        vec![
            IoRequirement::input::<bool>("set", self.set),
            IoRequirement::input::<bool>("reset", self.reset),
            IoRequirement::output::<bool>("out", self.output),
        ]
    }

    fn step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError> {
        let set = io.read_typed::<bool>(self.set)?;
        let reset = io.read_typed::<bool>(self.reset)?;
        if set.quality.is_good() && reset.quality.is_good() {
            // Reset-dominant: a standing reset clears a simultaneous set.
            self.state = (self.state || set.value) && !reset.value;
        }
        io.write_sample(
            self.output,
            Sample::new(
                Value::Bool(self.state),
                set.quality.merge(reset.quality),
                tick,
            ),
        )?;
        Ok(())
    }

    /// Describes the latch: `set` and `reset` are the control
    /// conditions, `out` the driven latched state. The kind declares no
    /// parameters.
    fn describe(&self) -> ComponentDescriptor {
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &[
                ("set", PortRole::Status),
                ("reset", PortRole::Status),
                ("out", PortRole::Output),
            ],
            Vec::new(),
        )
    }

    /// Captures the latched state so a tracking standby continues
    /// holding — or clears — exactly as the active would.
    fn capture_state(&self) -> StateMap {
        let mut state = StateMap::new();
        state.insert("state", Value::Bool(self.state));
        state
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        state.ensure_known_fields(&self.name, &["state"])?;
        self.state = state.require_bool(&self.name, "state")?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::TestIo;
    use dcs_core::{Direction, PortDescriptor, Quality, QualityReason};
    use dcs_model::{ComponentId, ComponentInstance};
    use std::collections::BTreeMap;

    const SET: PointId = PointId(210);
    const RESET: PointId = PointId(211);
    const OUT: PointId = PointId(212);

    fn component() -> SrLatch {
        SrLatch::new("srl", SET, RESET, OUT)
    }

    fn io() -> TestIo {
        TestIo::new(&[
            (
                SET,
                Direction::In,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
            (
                RESET,
                Direction::In,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
            (
                OUT,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
        ])
    }

    /// Feeds `set`/`reset` and steps once.
    fn step(block: &mut SrLatch, io: &TestIo, set: bool, reset: bool, tick: u64) {
        io.feed(SET, Sample::good(Value::Bool(set), Tick(tick)));
        io.feed(RESET, Sample::good(Value::Bool(reset), Tick(tick)));
        block.step(io, Tick(tick)).unwrap();
    }

    fn out(io: &TestIo) -> Sample {
        io.written(OUT).unwrap()
    }

    fn driven(io: &TestIo) -> bool {
        out(io).value == Value::Bool(true)
    }

    #[test]
    fn set_latches_and_reset_clears() {
        let mut block = component();
        let io = io();

        // A one-scan set pulse latches.
        step(&mut block, &io, true, false, 1);
        assert!(driven(&io));
        for tick in 2..=3 {
            step(&mut block, &io, false, false, tick);
            assert!(driven(&io), "tick={tick}");
        }

        // A one-scan reset pulse clears; the latch stays cleared.
        step(&mut block, &io, false, true, 4);
        assert!(!driven(&io));
        step(&mut block, &io, false, false, 5);
        assert!(!driven(&io));
    }

    #[test]
    fn simultaneous_set_and_reset_resolves_reset_dominant() {
        let mut block = component();
        let io = io();

        // The documented precedence rule: reset dominates a
        // simultaneous set, whether the latch stood set or clear.
        step(&mut block, &io, true, true, 1);
        assert!(!driven(&io));

        step(&mut block, &io, true, false, 2);
        assert!(driven(&io));
        step(&mut block, &io, true, true, 3);
        assert!(!driven(&io));
    }

    #[test]
    fn a_standing_reset_holds_against_a_standing_set() {
        let mut block = component();
        let io = io();

        // Set held with reset held: stays cleared through every scan,
        // then latches the still-asserted set the scan reset releases.
        for tick in 1..=2 {
            step(&mut block, &io, true, true, tick);
            assert!(!driven(&io), "tick={tick}");
        }
        step(&mut block, &io, true, false, 3);
        assert!(driven(&io));
    }

    #[test]
    fn untrusted_inputs_hold_the_latched_state() {
        let mut block = component();
        let io = io();
        step(&mut block, &io, true, false, 1);
        assert!(driven(&io));

        // A Bad reset cannot clear the held state; the held output is
        // stamped with the worst of the two qualities.
        io.feed(
            RESET,
            Sample::new(
                Value::Bool(true),
                Quality::Bad(QualityReason::CommunicationFault),
                Tick(2),
            ),
        );
        block.step(&io, Tick(2)).unwrap();
        assert_eq!(out(&io).value, Value::Bool(true));
        assert_eq!(
            out(&io).quality,
            Quality::Bad(QualityReason::CommunicationFault)
        );

        // Nor can an Uncertain set move a cleared latch.
        let mut cleared = component();
        step(&mut cleared, &io, false, false, 3);
        io.feed(
            SET,
            Sample::new(
                Value::Bool(true),
                Quality::Uncertain(QualityReason::Stale),
                Tick(4),
            ),
        );
        cleared.step(&io, Tick(4)).unwrap();
        assert_eq!(out(&io).value, Value::Bool(false));
        assert_eq!(out(&io).quality, Quality::Uncertain(QualityReason::Stale));

        // Once both inputs read Good again the latch evaluates normally.
        io.feed(RESET, Sample::good(Value::Bool(false), Tick(5)));
        io.feed(SET, Sample::good(Value::Bool(true), Tick(5)));
        cleared.step(&io, Tick(5)).unwrap();
        assert_eq!(out(&io).value, Value::Bool(true));
        assert_eq!(out(&io).quality, Quality::Good);
    }

    #[test]
    fn restored_latch_continues_holding() {
        let mut block = component();
        let io = io();
        step(&mut block, &io, true, false, 1);
        let state = block.capture_state();
        assert_eq!(state.get("state"), Some(Value::Bool(true)));

        // The standby inherits the latched state and the same reset
        // clears it on the same scan.
        let mut standby = component();
        standby.restore_state(&state).unwrap();
        assert_eq!(standby.capture_state(), state);
        step(&mut standby, &io, false, false, 2);
        assert!(driven(&io));
        step(&mut standby, &io, false, true, 3);
        assert!(!driven(&io));
    }

    #[test]
    fn incompatible_state_is_a_named_error() {
        let mut block = component();
        let mut state = StateMap::new();
        state.insert("state", Value::Int(1));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::IncompatibleField { ref field, .. }) if field == "state"
        ));
        assert!(matches!(
            block.restore_state(&StateMap::new()),
            Err(StateError::MissingField { ref field, .. }) if field == "state"
        ));
        let mut foreign = StateMap::new();
        foreign.insert("state", Value::Bool(true));
        foreign.insert("extra", Value::Bool(false));
        assert!(matches!(
            block.restore_state(&foreign),
            Err(StateError::UnknownField { ref field, .. }) if field == "extra"
        ));
    }

    #[test]
    fn builds_from_parameter_map() {
        let instance = ComponentInstance {
            id: ComponentId(31),
            kind: SrLatch::KIND.to_string(),
            parameters: Parameters::new(),
            ports: BTreeMap::new(),
        };
        let block = SrLatch::from_parameters("srl", SET, RESET, OUT, &instance.parameters).unwrap();
        assert!(!block.state);
    }

    #[test]
    fn describes_itself() {
        let descriptor = component().describe();
        assert_eq!(descriptor.name, "srl");
        assert_eq!(descriptor.kind, SrLatch::KIND);
        assert_eq!(descriptor.label, "srl");
        assert_eq!(
            descriptor.ports,
            [
                PortDescriptor {
                    name: "set".to_string(),
                    direction: Direction::In,
                    kind: dcs_core::ValueKind::Bool,
                    role: Some(PortRole::Status),
                    point: None,
                },
                PortDescriptor {
                    name: "reset".to_string(),
                    direction: Direction::In,
                    kind: dcs_core::ValueKind::Bool,
                    role: Some(PortRole::Status),
                    point: None,
                },
                PortDescriptor {
                    name: "out".to_string(),
                    direction: Direction::Out,
                    kind: dcs_core::ValueKind::Bool,
                    role: Some(PortRole::Output),
                    point: None,
                },
            ]
        );
        // Drift guard: the kind declares no parameters and
        // `from_parameters` reads none.
        assert!(descriptor.parameters.is_empty());
    }
}
