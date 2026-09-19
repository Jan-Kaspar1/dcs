//! Motor: a discrete actuator that echoes a start/stop command to the
//! field and flags a fault when the run feedback stops following it.

use crate::describe;
use crate::params::{self, ParameterError, Parameters};
use dcs_core::{
    CommandError, ComponentDescriptor, PointId, PortRole, Sample, StateError, StateMap, Tick,
    Value, ValueKind,
};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// A motor: drives the boolean `cmd` start/stop request onto the field
/// `out` point and verifies the `run` feedback follows it.
///
/// Each step copies `cmd` onto `out` — the starter's field command —
/// then compares `run` against the commanded state. The feedback
/// *agrees* only when both inputs carry `Good` quality and
/// `run == cmd`; every other state — an opposite feedback or a
/// non-`Good` input that cannot prove agreement — counts as
/// disagreeing, the fail-safe reading. `fault` asserts once the count
/// of consecutive disagreeing scans reaches `fault_ticks` (`0` or `1`
/// flags the first disagreeing scan) and clears on the first scan the
/// feedback agrees again. That covers both failure directions: a `cmd`
/// of `true` the `run` never follows is a failure to start, and a `cmd`
/// of `false` a stuck `run` ignores is a failure to stop.
///
/// `out` carries `cmd`'s quality — a degraded command produces a
/// degraded field write. `fault` carries the worst of the `cmd` and
/// `run` qualities, so a `Bad` feedback both counts as disagreeing and
/// marks the diagnostic itself untrusted.
///
/// Declared I/O: `cmd` (`In`, `Bool`), `out` (`Out`, `Bool`), `run`
/// (`In`, `Bool`), `fault` (`Out`, `Bool`).
///
/// Parameters: `fault_ticks` — optional non-negative `Int` (or integral
/// `Float`), default `0`, the consecutive disagreeing scans the flag
/// waits for. Set it to the actuator's expected response time so a
/// healthy start or stop does not flag mid-travel.
#[derive(Debug)]
pub struct Motor {
    name: String,
    command: PointId,
    output: PointId,
    run: PointId,
    fault: PointId,
    fault_ticks: u64,
    /// Consecutive disagreeing scans observed so far.
    disagreeing: u64,
}

impl Motor {
    /// The component-kind string the model-driven registry maps onto this
    /// type's constructor.
    pub const KIND: &'static str = "motor";

    /// Builds the component from explicit points.
    pub fn new(
        name: impl Into<String>,
        command: PointId,
        output: PointId,
        run: PointId,
        fault: PointId,
    ) -> Self {
        Self {
            name: name.into(),
            command,
            output,
            run,
            fault,
            fault_ticks: 0,
            disagreeing: 0,
        }
    }

    /// Sets how many consecutive disagreeing scans `fault` waits for
    /// before asserting; `0` or `1` flags the first disagreeing scan.
    pub fn with_fault_ticks(mut self, fault_ticks: u64) -> Self {
        self.fault_ticks = fault_ticks;
        self
    }

    /// Builds the component from a plant-model parameter map, reading the
    /// parameters listed on the type's docs.
    pub fn from_parameters(
        name: impl Into<String>,
        command: PointId,
        output: PointId,
        run: PointId,
        fault: PointId,
        parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        let fault_ticks = params::optional_u64(&name, parameters, "fault_ticks")?.unwrap_or(0);
        Ok(Self::new(name, command, output, run, fault).with_fault_ticks(fault_ticks))
    }
}

impl Component for Motor {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        vec![
            IoRequirement::input::<bool>("cmd", self.command),
            IoRequirement::output::<bool>("out", self.output),
            IoRequirement::input::<bool>("run", self.run),
            IoRequirement::output::<bool>("fault", self.fault),
        ]
    }

    fn step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError> {
        let command = io.read_typed::<bool>(self.command)?;
        let run = io.read_typed::<bool>(self.run)?;
        io.write_sample(
            self.output,
            Sample::new(Value::Bool(command.value), command.quality, tick),
        )?;

        // Agreement must be proven: both inputs Good and the feedback in
        // the commanded state. A non-Good input cannot vouch for
        // agreement, so it counts as disagreeing — the fail-safe reading.
        let agrees =
            command.quality.is_good() && run.quality.is_good() && run.value == command.value;
        if agrees {
            self.disagreeing = 0;
        } else {
            self.disagreeing = self.disagreeing.saturating_add(1);
        }
        let fault = self.disagreeing >= self.fault_ticks.max(1);
        io.write_sample(
            self.fault,
            Sample::new(Value::Bool(fault), command.quality.merge(run.quality), tick),
        )?;
        Ok(())
    }

    /// Describes the actuator: `cmd` is the start/stop request the block
    /// is driven toward, `out` the field command, `run` the measured run
    /// feedback it verifies, `fault` the reported diagnostic; the
    /// `fault_ticks` parameter.
    fn describe(&self) -> ComponentDescriptor {
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &[
                ("cmd", PortRole::Setpoint),
                ("out", PortRole::Output),
                ("run", PortRole::ProcessValue),
                ("fault", PortRole::Status),
            ],
            vec![describe::parameter(
                "fault_ticks",
                ValueKind::Int,
                Some(describe::NONNEGATIVE_INT),
            )],
        )
    }

    /// Tunes `fault_ticks` at the scan boundary; retuning below the
    /// banked `disagreeing` count asserts `fault` on the next scan.
    fn apply_parameter(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        match parameter {
            "fault_ticks" => {
                self.fault_ticks = params::tune_u64(&self.name, parameter, value)?;
            }
            _ => return Err(params::unknown_parameter(&self.name, parameter)),
        }
        Ok(())
    }

    /// Reports the declared `fault_ticks` — the same field
    /// [`capture_state`](Self::capture_state) checkpoints, so the
    /// faceplate and a tracking standby read one vocabulary.
    fn report_parameters(&self) -> StateMap {
        let mut parameters = StateMap::new();
        parameters.insert("fault_ticks", Value::Int(self.fault_ticks as i64));
        parameters
    }

    /// Captures the banked disagreeing-scan count and the tuned
    /// `fault_ticks` so a tracking standby continues the count under
    /// the same tuning.
    fn capture_state(&self) -> StateMap {
        let mut state = self.report_parameters();
        state.insert("disagreeing", Value::Int(self.disagreeing as i64));
        state
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        state.ensure_known_fields(&self.name, &["disagreeing", "fault_ticks"])?;
        let disagreeing = state.require_i64(&self.name, "disagreeing")?;
        if disagreeing < 0 {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "disagreeing".to_string(),
                value: Value::Int(disagreeing),
            });
        }
        let fault_ticks = state.require_i64(&self.name, "fault_ticks")?;
        if fault_ticks < 0 {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "fault_ticks".to_string(),
                value: Value::Int(fault_ticks),
            });
        }
        self.disagreeing = disagreeing as u64;
        self.fault_ticks = fault_ticks as u64;
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

    const CMD: PointId = PointId(100);
    const OUT: PointId = PointId(101);
    const RUN: PointId = PointId(102);
    const FAULT: PointId = PointId(103);

    /// A motor idling in agreement: command and run feedback both false.
    fn component() -> Motor {
        Motor::new("mtr", CMD, OUT, RUN, FAULT)
    }

    fn io() -> TestIo {
        TestIo::new(&[
            (
                CMD,
                Direction::In,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
            (
                OUT,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
            (
                RUN,
                Direction::In,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
            (
                FAULT,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
        ])
    }

    fn feed_cmd(io: &TestIo, value: bool, tick: u64) {
        io.feed(CMD, Sample::good(Value::Bool(value), Tick(tick)));
    }

    fn feed_run(io: &TestIo, value: bool, tick: u64) {
        io.feed(RUN, Sample::good(Value::Bool(value), Tick(tick)));
    }

    fn fault(io: &TestIo) -> Sample {
        io.written(FAULT).unwrap()
    }

    #[test]
    fn echoes_command_to_the_field_while_feedback_follows() {
        let mut motor = component().with_fault_ticks(2);
        let io = io();
        motor.step(&io, Tick(1)).unwrap();
        assert_eq!(io.written(OUT).unwrap().value, Value::Bool(false));
        assert_eq!(fault(&io).value, Value::Bool(false));

        // Start the motor; the run feedback follows inside the delay.
        feed_cmd(&io, true, 2);
        motor.step(&io, Tick(2)).unwrap();
        let out = io.written(OUT).unwrap();
        assert_eq!(out.value, Value::Bool(true));
        assert_eq!(out.quality, Quality::Good);
        assert_eq!(out.tick, Tick(2));
        assert_eq!(fault(&io).value, Value::Bool(false));
        feed_run(&io, true, 3);
        motor.step(&io, Tick(3)).unwrap();
        assert_eq!(fault(&io).value, Value::Bool(false));
    }

    #[test]
    fn fault_on_failed_start_after_the_configured_tick_count() {
        let mut motor = component().with_fault_ticks(2);
        let io = io();
        feed_cmd(&io, true, 1);
        // run stays false: the first disagreeing scan does not flag.
        motor.step(&io, Tick(1)).unwrap();
        assert_eq!(fault(&io).value, Value::Bool(false));
        // The second consecutive disagreeing scan asserts the fault.
        motor.step(&io, Tick(2)).unwrap();
        assert_eq!(fault(&io).value, Value::Bool(true));
    }

    #[test]
    fn zero_delay_fault_flags_the_first_disagreeing_scan() {
        let mut motor = component();
        let io = io();
        feed_cmd(&io, true, 1);
        motor.step(&io, Tick(1)).unwrap();
        assert_eq!(fault(&io).value, Value::Bool(true));
    }

    #[test]
    fn fault_on_failed_stop_and_clear_when_feedback_recovers() {
        let mut motor = component().with_fault_ticks(2);
        let io = io();
        // Running motor commanded to stop; run feedback stays true.
        feed_cmd(&io, true, 1);
        feed_run(&io, true, 1);
        motor.step(&io, Tick(1)).unwrap();
        assert_eq!(fault(&io).value, Value::Bool(false));

        feed_cmd(&io, false, 2);
        motor.step(&io, Tick(2)).unwrap();
        assert_eq!(io.written(OUT).unwrap().value, Value::Bool(false));
        assert_eq!(fault(&io).value, Value::Bool(false));
        motor.step(&io, Tick(3)).unwrap();
        assert_eq!(fault(&io).value, Value::Bool(true));

        // The feedback drops on the next scan: the fault clears and the
        // count resets, so a later disagreement needs the full delay.
        feed_run(&io, false, 4);
        motor.step(&io, Tick(4)).unwrap();
        assert_eq!(fault(&io).value, Value::Bool(false));
        feed_run(&io, true, 5);
        motor.step(&io, Tick(5)).unwrap();
        assert_eq!(fault(&io).value, Value::Bool(false));
        motor.step(&io, Tick(6)).unwrap();
        assert_eq!(fault(&io).value, Value::Bool(true));
    }

    #[test]
    fn bad_run_feedback_counts_as_disagreeing_and_marks_the_fault() {
        let mut motor = component();
        let io = io();
        // A Bad-quality feedback cannot prove it followed the command
        // even while its value agrees.
        io.feed(
            RUN,
            Sample::new(
                Value::Bool(false),
                Quality::Bad(QualityReason::CommunicationFault),
                Tick(1),
            ),
        );
        motor.step(&io, Tick(1)).unwrap();
        let flag = fault(&io);
        assert_eq!(flag.value, Value::Bool(true));
        assert_eq!(
            flag.quality,
            Quality::Bad(QualityReason::CommunicationFault)
        );
    }

    #[test]
    fn command_quality_propagates_to_the_field_write_and_fault() {
        let mut motor = component();
        let io = io();
        io.feed(
            CMD,
            Sample::new(
                Value::Bool(false),
                Quality::Uncertain(QualityReason::Substituted),
                Tick(1),
            ),
        );
        motor.step(&io, Tick(1)).unwrap();
        assert_eq!(
            io.written(OUT).unwrap().quality,
            Quality::Uncertain(QualityReason::Substituted)
        );
        assert_eq!(
            fault(&io).quality,
            Quality::Uncertain(QualityReason::Substituted)
        );
    }

    #[test]
    fn builds_from_parameter_map() {
        let parameters: Parameters = [("fault_ticks".to_string(), Value::Int(3))]
            .into_iter()
            .collect();
        let instance = ComponentInstance {
            id: ComponentId(10),
            kind: Motor::KIND.to_string(),
            parameters,
            rationalization: None,
            ports: BTreeMap::new(),
        };
        let mut motor =
            Motor::from_parameters("mtr", CMD, OUT, RUN, FAULT, &instance.parameters).unwrap();
        assert_eq!(motor.fault_ticks, 3);

        // A failed start under the built config flags on the third
        // disagreeing scan.
        let io = io();
        feed_cmd(&io, true, 1);
        for tick in 1..=2 {
            motor.step(&io, Tick(tick)).unwrap();
            assert_eq!(fault(&io).value, Value::Bool(false), "tick={tick}");
        }
        motor.step(&io, Tick(3)).unwrap();
        assert_eq!(fault(&io).value, Value::Bool(true));

        // The parameter is optional; an empty map builds the
        // zero-delay block.
        let motor =
            Motor::from_parameters("mtr", CMD, OUT, RUN, FAULT, &Parameters::new()).unwrap();
        assert_eq!(motor.fault_ticks, 0);

        let wrong: Parameters = [("fault_ticks".to_string(), Value::Int(-1))]
            .into_iter()
            .collect();
        assert!(matches!(
            Motor::from_parameters("mtr", CMD, OUT, RUN, FAULT, &wrong).unwrap_err(),
            ParameterError::Invalid { ref parameter, .. } if parameter == "fault_ticks"
        ));
    }

    #[test]
    fn describes_itself() {
        let descriptor = component().describe();
        assert_eq!(descriptor.name, "mtr");
        assert_eq!(descriptor.kind, Motor::KIND);
        assert_eq!(descriptor.label, "mtr");
        assert_eq!(
            descriptor.ports,
            [
                PortDescriptor {
                    name: "cmd".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Setpoint),
                    point: None,
                },
                PortDescriptor {
                    name: "out".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Output),
                    point: None,
                },
                PortDescriptor {
                    name: "run".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::ProcessValue),
                    point: None,
                },
                PortDescriptor {
                    name: "fault".to_string(),
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
            [ParameterDescriptor {
                name: "fault_ticks".to_string(),
                kind: ValueKind::Int,
                range: Some(describe::NONNEGATIVE_INT),
            }]
        );
    }
}
