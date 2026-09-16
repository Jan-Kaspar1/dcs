//! Step sequencer: stepping through a declared ordered table of steps,
//! each driving `out` for a configured number of ticks.

use crate::describe;
use crate::params::{self, ParameterError, Parameters};
use dcs_core::{
    CommandArgument, CommandAvailability, CommandDecl, CommandError, ComponentDescriptor,
    EmittedEvent, EventDecl, EventField, EventFieldKind, EventRetention, EventValue, PointId,
    PortRole, Sample, StateError, StateMap, Tick, Value, ValueKind,
};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};
use std::collections::BTreeMap;

/// One step of a [`Sequencer`]'s table: `out` drives `value` while the
/// step is active, and the step holds for `ticks` scans.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct SequencerStep {
    /// How many scans the step stays active. `0` behaves as `1`: a step
    /// always drives its output for at least the scan it is entered on.
    pub ticks: u64,
    /// The value `out` carries while the step is active.
    pub value: f64,
}

/// A step sequencer: while `run` holds it walks a declared table of
/// steps in order — each step driving `out` at its configured value for
/// its configured tick duration — reports the active step on `step`,
/// and asserts `done` when the table has run to its end.
///
/// All timing is in scans — the executor's virtual tick — never wall
/// clock, per the execution-model decision.
///
/// The sequencer is always positioned at a step: `out` drives the
/// active step's value and `step` reports its 1-based index every scan,
/// running or not — a fresh or reset sequencer sits on step 1 and
/// already drives that step's value.
///
/// **Advance rule:** while `run` reads `true` the sequencer counts
/// scans in the active step; the scan on which the count reaches the
/// step's `ticks` is still that step's — `out` drives its value and
/// `step` reports its index one last time — and the next scan belongs
/// to the following step. Each step therefore drives for exactly
/// `ticks` scans, with `ticks` of `0` behaving as `1`. While `run`
/// reads `false` the sequencer holds: the active step and its banked
/// count stand, and `out`/`step` keep reporting it.
///
/// **Done rule (hold-at-end):** the scan the final step completes —
/// its `ticks`th scan — asserts `done` while still reporting the final
/// step, and the sequencer holds there: `out` keeps driving the final
/// step's value and `done` stays asserted while `run` holds. `done`
/// clears only through `reset`; a completed sequencer does not wrap.
///
/// **Reset rule:** while `reset` reads `true` the sequencer returns to
/// the first step — `step` reports `1`, `out` drives the first step's
/// value, the banked count and `done` clear — and reset dominates a
/// simultaneously held `run`.
///
/// **Quality rule:** a control input whose quality is not `Good` is not
/// trusted to act: while `run` or `reset` reads non-`Good` the
/// sequencer holds where it stands — neither advancing nor resetting on
/// a value that may not be real — while `out`, `step`, and `done` keep
/// reporting the held step, every output stamped with the worst of the
/// two input qualities.
///
/// **Declared commands and events:** the kind's native command surface —
/// `advance` moves the active step forward `count` steps (one when the
/// argument is absent) and `reset` restarts the table — applies at the
/// scan boundary through `invoke_command`; `advance` is
/// `KindDeclared`-available and refuses a completed table, the standing
/// refusal `command_refusal` publishes on the snapshot's
/// `command_verdicts` section. The declared
/// `step_completed` event emits on the scan a step runs its `ticks` out
/// and journals at the producing tick. None of the commands aliases a
/// writable point: `reset` the command is a one-shot action where the
/// `reset` input is a held condition.
///
/// Declared I/O: `run` (`In`, `Bool`), `reset` (`In`, `Bool`), `out`
/// (`Out`, `Float`), `step` (`Out`, `Int`), `done` (`Out`, `Bool`).
///
/// **Step-table parameters:** `step_count` — required positive `Int`
/// (or integral `Float`) no larger than `i64::MAX` — declares the table
/// length `N`, and each step `n` in `1..=N` declares `step_<n>_ticks`
/// — required non-negative `Int` no larger than `i64::MAX` — and
/// `step_<n>_out` — required finite `Float` (or losslessly
/// representable `Int`). A table missing any entry, or carrying a
/// mistyped or out-of-domain one, fails construction with a
/// [`ParameterError`] naming the parameter; keys outside the declared
/// `1..=N` set are not part of the table and are ignored.
#[derive(Debug)]
pub struct Sequencer {
    name: String,
    run: PointId,
    reset: PointId,
    out: PointId,
    step: PointId,
    done: PointId,
    steps: Vec<SequencerStep>,
    /// The active step's index into `steps` — `step` reports it 1-based.
    current: usize,
    /// Scans banked in `steps[current]`, clamped at its `ticks`.
    elapsed: u64,
    /// Set when the final step completes; holds until `reset`.
    completed: bool,
    /// Events emitted since the last drain — the executor empties this
    /// after every `step`, so it is always empty between scans and
    /// never part of the checkpointed state.
    pending_events: Vec<EmittedEvent>,
}

impl Sequencer {
    /// The component-kind string the model-driven registry maps onto this
    /// type's constructor.
    pub const KIND: &'static str = "sequencer";

    /// Builds the component from explicit points and the step table.
    ///
    /// The table must hold at least one step, each `ticks` may not
    /// exceed `i64::MAX` — the reported index and captured state are
    /// `Int`s — and each `value` must be finite. Violations are
    /// reported as a [`ParameterError`] naming `step_count` or the
    /// offending `step_<n>_ticks`/`step_<n>_out` entry.
    pub fn new(
        name: impl Into<String>,
        run: PointId,
        reset: PointId,
        out: PointId,
        step: PointId,
        done: PointId,
        steps: Vec<SequencerStep>,
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
        }
        Ok(Self {
            name,
            run,
            reset,
            out,
            step,
            done,
            steps,
            current: 0,
            elapsed: 0,
            completed: false,
            pending_events: Vec::new(),
        })
    }

    /// Builds the component from a plant-model parameter map, reading the
    /// step table listed on the type's docs.
    pub fn from_parameters(
        name: impl Into<String>,
        run: PointId,
        reset: PointId,
        out: PointId,
        step: PointId,
        done: PointId,
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
            steps.push(SequencerStep { ticks, value });
        }
        Self::new(name, run, reset, out, step, done, steps)
    }
}

/// Parses a `step_<n>_ticks`/`step_<n>_out` parameter name into its
/// 1-based step index and which entry it names — `None` for any other
/// parameter.
fn step_entry(parameter: &str) -> Option<(usize, &'static str)> {
    let rest = parameter.strip_prefix("step_")?;
    let (index, entry) = rest.rsplit_once('_')?;
    let index: usize = index.parse().ok()?;
    match entry {
        "ticks" => Some((index, "ticks")),
        "out" => Some((index, "out")),
        _ => None,
    }
}

impl Component for Sequencer {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        vec![
            IoRequirement::input::<bool>("run", self.run),
            IoRequirement::input::<bool>("reset", self.reset),
            IoRequirement::output::<f64>("out", self.out),
            IoRequirement::output::<i64>("step", self.step),
            IoRequirement::output::<bool>("done", self.done),
        ]
    }

    fn step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError> {
        let run = io.read_typed::<bool>(self.run)?;
        let reset = io.read_typed::<bool>(self.reset)?;
        let quality = run.quality.merge(reset.quality);
        // The step whose value and index this scan reports: the one the
        // scan began on — so a step still drives the scan it completes
        // on — or the first step when `reset` parks the sequencer there.
        let reported = if quality.is_good() && reset.value {
            self.current = 0;
            self.elapsed = 0;
            self.completed = false;
            0
        } else {
            let reported = self.current;
            if quality.is_good() && run.value && !self.completed {
                self.elapsed += 1;
                if self.elapsed >= self.steps[self.current].ticks.max(1) {
                    // The reported step ran its declared ticks out — the
                    // declared `step_completed` event, journaled at this
                    // scan's tick.
                    self.pending_events.push(EmittedEvent {
                        event: "step_completed".to_string(),
                        component: self.name.clone(),
                        fields: [(
                            "step".to_string(),
                            EventValue::Value(Value::Int(reported as i64 + 1)),
                        )]
                        .into_iter()
                        .collect(),
                    });
                    if self.current == self.steps.len() - 1 {
                        // Hold-at-end: the count clamps at the step's
                        // declared duration, so a captured `done` state
                        // always reads `elapsed == ticks`.
                        self.completed = true;
                        self.elapsed = self.steps[self.current].ticks;
                    } else {
                        self.current += 1;
                        self.elapsed = 0;
                    }
                }
            }
            reported
        };
        let driven = self.steps[reported].value;
        io.write_sample(self.out, Sample::new(Value::Float(driven), quality, tick))?;
        io.write_sample(
            self.step,
            Sample::new(Value::Int(reported as i64 + 1), quality, tick),
        )?;
        io.write_sample(
            self.done,
            Sample::new(Value::Bool(self.completed), quality, tick),
        )?;
        Ok(())
    }

    /// Describes the sequencer: `run` is the signal stepping the table,
    /// `reset` the return-to-start condition, `out` the driven value,
    /// `step` and `done` the reported progress; the `step_count` and
    /// per-step `step_<n>_ticks`/`step_<n>_out` parameters
    /// `from_parameters` reads.
    fn describe(&self) -> ComponentDescriptor {
        let mut parameters = vec![describe::parameter(
            "step_count",
            ValueKind::Int,
            Some(describe::POSITIVE_INT),
        )];
        for index in 1..=self.steps.len() {
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
        }
        let mut descriptor = describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &[
                ("run", PortRole::ProcessValue),
                ("reset", PortRole::Status),
                ("out", PortRole::Output),
                ("step", PortRole::Status),
                ("done", PortRole::Status),
            ],
            parameters,
        );
        // The declared command surface — the invoke-side actions beside
        // the `run`/`reset` level inputs: `advance` paces the table by
        // command, `reset` restarts it. Neither is a writable-point
        // alias: `reset`'s input must be held, the command is a
        // one-shot action.
        descriptor.commands = vec![
            CommandDecl {
                name: "advance".to_string(),
                request: vec![CommandArgument {
                    name: "count".to_string(),
                    kind: ValueKind::Int,
                }],
                availability: CommandAvailability::KindDeclared,
            },
            CommandDecl {
                name: "reset".to_string(),
                request: Vec::new(),
                availability: CommandAvailability::Always,
            },
        ];
        descriptor.events = vec![EventDecl {
            name: "step_completed".to_string(),
            payload: vec![EventField {
                name: "step".to_string(),
                kind: EventFieldKind::Value(ValueKind::Int),
                optional: false,
            }],
            retention: EventRetention::Journal,
        }];
        descriptor
    }

    /// The standing-availability probe the executor's post-scan
    /// verdict evaluation calls for the descriptor's `KindDeclared`
    /// commands: `advance` reports the same standing refusal dispatch
    /// checks — a completed table — and `reset` stands invocable. The
    /// argument-dependent refusal (a `count` below 1) stays with
    /// dispatch: the probe answers whether the command is invocable at
    /// all now, not what a given request would meet.
    fn command_refusal(&self, command: &str) -> Option<String> {
        match command {
            "advance" if self.completed => {
                Some("the sequence has run to its end; reset restarts it".to_string())
            }
            _ => None,
        }
    }

    /// The invoke surface the descriptor declares: `advance` moves the
    /// active step forward `count` steps — absent `count` means one —
    /// and `reset` returns the run to the first step, the one-shot
    /// equivalent of the held `reset` input.
    ///
    /// `advance` is `KindDeclared`-available: a completed table refuses
    /// it with the declared reason, and `count` must be at least 1.
    /// The standing half is [`command_refusal`](Self::command_refusal)'s
    /// — dispatch consults the probe so the published verdict and the
    /// refusal can never disagree. Landing past the last step completes
    /// the run — the same hold-at-end state a `run`-paced table
    /// reaches. `reset` is always available. Both mutate only the
    /// checkpointed `step`/`elapsed`/`done` run state, so a tracking
    /// standby inherits the effect through the ordinary checkpoint.
    fn invoke_command(
        &mut self,
        command: &str,
        arguments: &BTreeMap<String, Value>,
    ) -> Result<(), String> {
        match command {
            "advance" => {
                if let Some(reason) = self.command_refusal(command) {
                    return Err(reason);
                }
                let count = match arguments.get("count") {
                    None => 1,
                    Some(Value::Int(count)) => *count,
                    Some(_) => unreachable!("submission validates the declared argument kind"),
                };
                if count < 1 {
                    return Err("count must be at least 1".to_string());
                }
                let remaining = self.steps.len() - self.current;
                if count as u64 >= remaining as u64 {
                    self.current = self.steps.len() - 1;
                    self.elapsed = self.steps[self.current].ticks;
                    self.completed = true;
                } else {
                    self.current += count as usize;
                    self.elapsed = 0;
                }
                Ok(())
            }
            "reset" => {
                self.current = 0;
                self.elapsed = 0;
                self.completed = false;
                Ok(())
            }
            _ => unreachable!("submission validates the declared command name"),
        }
    }

    /// Empties the emitted-event queue the `step_completed` emissions
    /// land in — the executor drains it after every `step`.
    fn drain_events(&mut self) -> Vec<EmittedEvent> {
        std::mem::take(&mut self.pending_events)
    }

    /// Tunes a step's declared duration or driven value at the scan
    /// boundary.
    ///
    /// `step_<n>_out` applies on the next scan step `n` drives —
    /// immediately when `n` is the active step. `step_<n>_ticks` retunes
    /// the step's duration; shrinking the active step's `ticks` below
    /// the banked `elapsed` clamps the count, so the step completes on
    /// the next running scan. `step_count` is fixed at construction —
    /// the table's length is model structure, not a tunable — and
    /// retuning it is refused with `InvalidParameter` naming it.
    fn apply_parameter(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        match step_entry(parameter) {
            Some((index, "ticks")) if index >= 1 && index <= self.steps.len() => {
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
                    // Keep the banked count inside the step's in-flight
                    // domain — a shrunk duration completes on the next
                    // running scan — and a completed sequencer's clamped
                    // count follows the retuned `ticks`.
                    self.elapsed = if self.completed {
                        tuned
                    } else {
                        self.elapsed.min(tuned.max(1) - 1)
                    };
                }
            }
            Some((index, "out")) if index >= 1 && index <= self.steps.len() => {
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
            _ if parameter == "step_count" => {
                return Err(params::invalid_parameter(
                    &self.name,
                    parameter,
                    "the step count is fixed at construction",
                ));
            }
            _ => return Err(params::unknown_parameter(&self.name, parameter)),
        }
        Ok(())
    }

    /// Reports the declared step table — `step_count` plus each step's
    /// `ticks`/`out` — the same fields
    /// [`capture_state`](Self::capture_state) checkpoints, so the
    /// faceplate and a tracking standby read one vocabulary.
    fn report_parameters(&self) -> StateMap {
        let mut parameters = StateMap::new();
        parameters.insert("step_count", Value::Int(self.steps.len() as i64));
        for (index, entry) in self.steps.iter().enumerate() {
            parameters.insert(
                format!("step_{}_ticks", index + 1),
                Value::Int(entry.ticks as i64),
            );
            parameters.insert(format!("step_{}_out", index + 1), Value::Float(entry.value));
        }
        parameters
    }

    /// Captures the active step, its banked count, the completion flag,
    /// and the tuned step table, so a checkpointed sequencer continues
    /// mid-sequence under the same tuning: a restored sequencer sitting
    /// `elapsed` scans into step `step` advances exactly as the captured
    /// one would have.
    fn capture_state(&self) -> StateMap {
        let mut state = self.report_parameters();
        state.insert("step", Value::Int(self.current as i64 + 1));
        state.insert("elapsed", Value::Int(self.elapsed as i64));
        state.insert("done", Value::Bool(self.completed));
        state
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        let mut known: Vec<String> = vec![
            "step".to_string(),
            "elapsed".to_string(),
            "done".to_string(),
            "step_count".to_string(),
        ];
        for index in 1..=self.steps.len() {
            known.push(format!("step_{index}_ticks"));
            known.push(format!("step_{index}_out"));
        }
        state.ensure_known_fields(
            &self.name,
            &known.iter().map(String::as_str).collect::<Vec<_>>(),
        )?;
        let step = state.require_i64(&self.name, "step")?;
        let elapsed = state.require_i64(&self.name, "elapsed")?;
        let completed = state.require_bool(&self.name, "done")?;
        let count = state.require_i64(&self.name, "step_count")?;
        if count != self.steps.len() as i64 {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "step_count".to_string(),
                value: Value::Int(count),
            });
        }
        if !(1..=count).contains(&step) {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "step".to_string(),
                value: Value::Int(step),
            });
        }
        let current = (step - 1) as usize;
        let mut steps = Vec::with_capacity(self.steps.len());
        for index in 1..=self.steps.len() {
            let ticks_field = format!("step_{index}_ticks");
            let ticks = state.require_i64(&self.name, &ticks_field)?;
            if ticks < 0 {
                return Err(StateError::InvalidValue {
                    element: self.name.clone(),
                    field: ticks_field,
                    value: Value::Int(ticks),
                });
            }
            let out_field = format!("step_{index}_out");
            let value = state.require_f64(&self.name, &out_field)?;
            if !value.is_finite() {
                return Err(StateError::InvalidValue {
                    element: self.name.clone(),
                    field: out_field,
                    value: Value::Float(value),
                });
            }
            steps.push(SequencerStep {
                ticks: ticks as u64,
                value,
            });
        }
        // A captured in-flight count sits below the step's effective
        // duration — the scan reaching it advances or completes — so
        // `elapsed` belongs to `0..max(ticks, 1)` while `done` is clear.
        if elapsed < 0 || (!completed && elapsed >= steps[current].ticks.max(1) as i64) {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "elapsed".to_string(),
                value: Value::Int(elapsed),
            });
        }
        // A completed sequencer always captures its final step with the
        // count clamped at `ticks`.
        if completed && (current != steps.len() - 1 || elapsed != steps[current].ticks as i64) {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "done".to_string(),
                value: Value::Bool(completed),
            });
        }
        self.steps = steps;
        self.current = current;
        self.elapsed = elapsed as u64;
        self.completed = completed;
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

    const RUN: PointId = PointId(190);
    const RESET: PointId = PointId(191);
    const OUT: PointId = PointId(192);
    const STEP: PointId = PointId(193);
    const DONE: PointId = PointId(194);

    /// A three-step sequencer: step 1 drives 10.0 for two scans, step 2
    /// drives 20.0 for two, step 3 drives 30.0 for one.
    fn component() -> Sequencer {
        Sequencer::new(
            "seq",
            RUN,
            RESET,
            OUT,
            STEP,
            DONE,
            vec![
                SequencerStep {
                    ticks: 2,
                    value: 10.0,
                },
                SequencerStep {
                    ticks: 2,
                    value: 20.0,
                },
                SequencerStep {
                    ticks: 1,
                    value: 30.0,
                },
            ],
        )
        .unwrap()
    }

    fn io() -> TestIo {
        TestIo::new(&[
            (
                RUN,
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
                Sample::good(Value::Float(0.0), Tick::ZERO),
            ),
            (
                STEP,
                Direction::Out,
                Sample::good(Value::Int(0), Tick::ZERO),
            ),
            (
                DONE,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
        ])
    }

    /// Feeds `run`/`reset` at `Good` quality and steps once.
    fn step(block: &mut Sequencer, io: &TestIo, run: bool, reset: bool, tick: u64) {
        io.feed(RUN, Sample::good(Value::Bool(run), Tick(tick)));
        io.feed(RESET, Sample::good(Value::Bool(reset), Tick(tick)));
        block.step(io, Tick(tick)).unwrap();
    }

    fn out(io: &TestIo) -> f64 {
        match io.written(OUT).unwrap().value {
            Value::Float(value) => value,
            other => panic!("out must be Float, found {other:?}"),
        }
    }

    fn active(io: &TestIo) -> i64 {
        match io.written(STEP).unwrap().value {
            Value::Int(index) => index,
            other => panic!("step must be Int, found {other:?}"),
        }
    }

    fn done(io: &TestIo) -> bool {
        io.written(DONE).unwrap().value == Value::Bool(true)
    }

    #[test]
    fn each_step_drives_for_its_declared_ticks() {
        let mut block = component();
        let io = io();

        // Parked at step 1 before `run` ever asserts: the first step's
        // value is already driven and `step` reports it.
        step(&mut block, &io, false, false, 1);
        assert_eq!(out(&io), 10.0);
        assert_eq!(active(&io), 1);
        assert!(!done(&io));

        // Step 1 drives ticks 2-3; tick 4 belongs to step 2.
        for (tick, expected_out, expected_step) in
            [(2, 10.0, 1), (3, 10.0, 1), (4, 20.0, 2), (5, 20.0, 2)]
        {
            step(&mut block, &io, true, false, tick);
            assert_eq!(out(&io), expected_out, "tick={tick}");
            assert_eq!(active(&io), expected_step, "tick={tick}");
            assert!(!done(&io), "tick={tick}");
        }
        // Tick 6 is step 3's single scan; done asserts on it — the
        // final step's completing scan — while it still reports step 3.
        step(&mut block, &io, true, false, 6);
        assert_eq!(out(&io), 30.0);
        assert_eq!(active(&io), 3);
        assert!(done(&io));
    }

    #[test]
    fn done_holds_at_the_final_step_until_reset() {
        let mut block = component();
        let io = io();
        for tick in 1..=5 {
            step(&mut block, &io, true, false, tick);
        }
        assert!(done(&io));

        // Held at the end: `run` still high keeps step 3 driving.
        step(&mut block, &io, true, false, 6);
        step(&mut block, &io, true, false, 7);
        assert_eq!(out(&io), 30.0);
        assert_eq!(active(&io), 3);
        assert!(done(&io));

        // `run` falling does not clear `done` — only `reset` does.
        step(&mut block, &io, false, false, 8);
        assert!(done(&io));
        assert_eq!(active(&io), 3);

        // Reset parks the sequencer back on step 1.
        step(&mut block, &io, false, true, 9);
        assert_eq!(out(&io), 10.0);
        assert_eq!(active(&io), 1);
        assert!(!done(&io));

        // And a fresh run walks the table again.
        step(&mut block, &io, true, false, 10);
        assert_eq!(active(&io), 1);
        assert_eq!(out(&io), 10.0);
    }

    #[test]
    fn run_falling_pauses_and_rising_resumes() {
        let mut block = component();
        let io = io();

        // One scan into step 1, then `run` falls: the position holds.
        step(&mut block, &io, true, false, 1);
        for tick in 2..=4 {
            step(&mut block, &io, false, false, tick);
            assert_eq!(out(&io), 10.0, "tick={tick}");
            assert_eq!(active(&io), 1, "tick={tick}");
        }
        // Resuming banks the second scan of step 1, then advances.
        step(&mut block, &io, true, false, 5);
        assert_eq!(active(&io), 1);
        step(&mut block, &io, true, false, 6);
        assert_eq!(active(&io), 2);
        assert_eq!(out(&io), 20.0);
    }

    #[test]
    fn reset_dominates_run_and_restarts_the_table() {
        let mut block = component();
        let io = io();

        for tick in 1..=3 {
            step(&mut block, &io, true, false, tick);
        }
        assert_eq!(active(&io), 2);

        // Reset asserted together with a held `run` wins.
        step(&mut block, &io, true, true, 4);
        assert_eq!(active(&io), 1);
        assert_eq!(out(&io), 10.0);

        // Held across two scans the table does not advance into it.
        step(&mut block, &io, true, true, 5);
        assert_eq!(active(&io), 1);

        // Released under a still-held `run`, step 1 dwells its full
        // duration again before advancing.
        step(&mut block, &io, true, false, 6);
        step(&mut block, &io, true, false, 7);
        assert_eq!(active(&io), 1);
        step(&mut block, &io, true, false, 8);
        assert_eq!(active(&io), 2);
    }

    #[test]
    fn zero_tick_steps_hold_one_scan() {
        let mut block = Sequencer::new(
            "seq",
            RUN,
            RESET,
            OUT,
            STEP,
            DONE,
            vec![
                SequencerStep {
                    ticks: 0,
                    value: 10.0,
                },
                SequencerStep {
                    ticks: 1,
                    value: 20.0,
                },
            ],
        )
        .unwrap();
        let io = io();
        // A `ticks` of 0 or 1 both hold the step for its entry scan.
        step(&mut block, &io, true, false, 1);
        assert_eq!(out(&io), 10.0);
        assert_eq!(active(&io), 1);
        assert!(!done(&io));
        step(&mut block, &io, true, false, 2);
        assert_eq!(out(&io), 20.0);
        assert_eq!(active(&io), 2);
        assert!(done(&io));
    }

    #[test]
    fn non_good_control_inputs_hold_the_sequence() {
        let mut block = component();
        let io = io();
        step(&mut block, &io, true, false, 1);

        // A Bad `run` neither advances nor reports Good: the step holds
        // and the outputs carry the merged quality.
        io.feed(
            RUN,
            Sample::new(
                Value::Bool(true),
                Quality::Bad(QualityReason::CommunicationFault),
                Tick(2),
            ),
        );
        block.step(&io, Tick(2)).unwrap();
        assert_eq!(active(&io), 1);
        assert_eq!(
            io.written(OUT).unwrap().quality,
            Quality::Bad(QualityReason::CommunicationFault)
        );
        assert_eq!(
            io.written(STEP).unwrap().quality,
            Quality::Bad(QualityReason::CommunicationFault)
        );
        assert_eq!(
            io.written(DONE).unwrap().quality,
            Quality::Bad(QualityReason::CommunicationFault)
        );

        // A recovered `run` banks step 1's second scan — its completing
        // scan still reports step 1.
        step(&mut block, &io, true, false, 3);
        assert_eq!(active(&io), 1);

        // An Uncertain `reset` does not restart the table either; the
        // held position — now step 2 — is stamped with it.
        io.feed(
            RESET,
            Sample::new(
                Value::Bool(true),
                Quality::Uncertain(QualityReason::Stale),
                Tick(4),
            ),
        );
        block.step(&io, Tick(4)).unwrap();
        assert_eq!(active(&io), 2);
        assert_eq!(out(&io), 20.0);
        assert_eq!(
            io.written(OUT).unwrap().quality,
            Quality::Uncertain(QualityReason::Stale)
        );

        // Recovered inputs act again on the next scan.
        step(&mut block, &io, true, true, 5);
        assert_eq!(active(&io), 1);
        assert_eq!(io.written(STEP).unwrap().quality, Quality::Good);
    }

    #[test]
    fn restored_sequencer_continues_mid_sequence() {
        let mut block = component();
        let io = io();

        // One scan into step 2, then checkpoint: `step` is 2 (1-based),
        // `elapsed` has 1 banked of the step's 2 ticks.
        for tick in 1..=3 {
            step(&mut block, &io, true, false, tick);
        }
        assert_eq!(active(&io), 2);
        let state = block.capture_state();
        assert_eq!(state.get("step"), Some(Value::Int(2)));
        assert_eq!(state.get("elapsed"), Some(Value::Int(1)));
        assert_eq!(state.get("done"), Some(Value::Bool(false)));
        assert_eq!(state.get("step_count"), Some(Value::Int(3)));
        assert_eq!(state.get("step_2_ticks"), Some(Value::Int(2)));

        // A standby restores and finishes the step identically: step 2
        // completes on its second banked scan, then step 3 runs and
        // `done` asserts at the table's end.
        let mut standby = component();
        standby.restore_state(&state).unwrap();
        assert_eq!(standby.capture_state(), state);
        step(&mut standby, &io, true, false, 4);
        assert_eq!(active(&io), 2);
        assert_eq!(out(&io), 20.0);
        step(&mut standby, &io, true, false, 5);
        assert_eq!(active(&io), 3);
        assert_eq!(out(&io), 30.0);
        assert!(done(&io));
    }

    #[test]
    fn incompatible_state_is_a_named_error() {
        let mut block = component();
        let mut state = block.capture_state();
        // A step index beyond the table.
        state.insert("step", Value::Int(4));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::InvalidValue { ref field, .. }) if field == "step"
        ));

        let mut state = block.capture_state();
        // An in-flight count at the step's duration — the scan reaching
        // it advances, so capture never produces this.
        state.insert("elapsed", Value::Int(2));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::InvalidValue { ref field, .. }) if field == "elapsed"
        ));

        let mut state = block.capture_state();
        // `done` set away from the table's end.
        state.insert("done", Value::Bool(true));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::InvalidValue { ref field, .. }) if field == "done"
        ));

        let mut state = block.capture_state();
        // A foreign field the kind never captures.
        state.insert("step_4_ticks", Value::Int(1));
        assert!(matches!(
            block.restore_state(&state),
            Err(StateError::UnknownField { ref field, .. }) if field == "step_4_ticks"
        ));
        assert!(matches!(
            block.restore_state(&StateMap::new()),
            Err(StateError::MissingField { ref field, .. }) if field == "step"
        ));
    }

    #[test]
    fn builds_from_parameter_map() {
        let parameters: Parameters = [
            ("step_count".to_string(), Value::Int(3)),
            ("step_1_ticks".to_string(), Value::Int(2)),
            ("step_1_out".to_string(), Value::Float(10.0)),
            ("step_2_ticks".to_string(), Value::Int(2)),
            ("step_2_out".to_string(), Value::Float(20.0)),
            ("step_3_ticks".to_string(), Value::Int(1)),
            // A losslessly representable Int is accepted for `out`.
            ("step_3_out".to_string(), Value::Int(30)),
        ]
        .into_iter()
        .collect();
        let instance = ComponentInstance {
            id: ComponentId(11),
            kind: Sequencer::KIND.to_string(),
            parameters,
            rationalization: None,
            ports: BTreeMap::new(),
        };
        let block =
            Sequencer::from_parameters("seq", RUN, RESET, OUT, STEP, DONE, &instance.parameters)
                .unwrap();
        assert_eq!(block.steps.len(), 3);
        assert_eq!(block.steps[2].value, 30.0);
    }

    #[test]
    fn malformed_step_tables_fail_naming_the_parameter() {
        let build = |parameters: Parameters| {
            Sequencer::from_parameters("seq", RUN, RESET, OUT, STEP, DONE, &parameters)
        };
        let complete: Parameters = [
            ("step_count".to_string(), Value::Int(2)),
            ("step_1_ticks".to_string(), Value::Int(2)),
            ("step_1_out".to_string(), Value::Float(10.0)),
            ("step_2_ticks".to_string(), Value::Int(1)),
            ("step_2_out".to_string(), Value::Float(20.0)),
        ]
        .into_iter()
        .collect();

        // `step_count` missing, zero, or mistyped.
        let mut missing = complete.clone();
        missing.remove("step_count");
        assert!(matches!(
            build(missing).unwrap_err(),
            ParameterError::Missing { ref parameter, .. } if parameter == "step_count"
        ));
        let mut zero = complete.clone();
        zero.insert("step_count".to_string(), Value::Int(0));
        assert!(matches!(
            build(zero).unwrap_err(),
            ParameterError::Invalid { ref parameter, .. } if parameter == "step_count"
        ));
        let mut mistyped = complete.clone();
        mistyped.insert("step_count".to_string(), Value::Bool(true));
        assert!(matches!(
            build(mistyped).unwrap_err(),
            ParameterError::Invalid { ref parameter, .. } if parameter == "step_count"
        ));

        // A missing `ticks` or `out` entry names the indexed key.
        let mut no_ticks = complete.clone();
        no_ticks.remove("step_2_ticks");
        assert!(matches!(
            build(no_ticks).unwrap_err(),
            ParameterError::Missing { ref parameter, .. } if parameter == "step_2_ticks"
        ));
        let mut no_out = complete.clone();
        no_out.remove("step_1_out");
        assert!(matches!(
            build(no_out).unwrap_err(),
            ParameterError::Missing { ref parameter, .. } if parameter == "step_1_out"
        ));

        // Mistyped or out-of-domain entries name the indexed key.
        let mut bad_ticks = complete.clone();
        bad_ticks.insert("step_1_ticks".to_string(), Value::Int(-1));
        assert!(matches!(
            build(bad_ticks).unwrap_err(),
            ParameterError::Invalid { ref parameter, .. } if parameter == "step_1_ticks"
        ));
        let mut bad_out = complete.clone();
        bad_out.insert("step_2_out".to_string(), Value::Float(f64::NAN));
        assert!(matches!(
            build(bad_out).unwrap_err(),
            ParameterError::Invalid { ref parameter, .. } if parameter == "step_2_out"
        ));
        let mut wrong_kind = complete.clone();
        wrong_kind.insert("step_2_out".to_string(), Value::Bool(true));
        assert!(matches!(
            build(wrong_kind).unwrap_err(),
            ParameterError::Invalid { ref parameter, .. } if parameter == "step_2_out"
        ));

        // An empty table and an oversized `ticks` fail through `new`'s
        // validation too.
        assert!(matches!(
            Sequencer::new("seq", RUN, RESET, OUT, STEP, DONE, Vec::new()),
            Err(ParameterError::Invalid { ref parameter, .. }) if parameter == "step_count"
        ));
        assert!(matches!(
            Sequencer::new(
                "seq",
                RUN,
                RESET,
                OUT,
                STEP,
                DONE,
                vec![SequencerStep {
                    ticks: i64::MAX as u64 + 1,
                    value: 0.0,
                }],
            ),
            Err(ParameterError::Invalid { ref parameter, .. }) if parameter == "step_1_ticks"
        ));
    }

    #[test]
    fn tunes_step_entries_at_the_scan_boundary() {
        let mut block = component();
        let io = io();
        step(&mut block, &io, true, false, 1);

        // Retune the active step's driven value: the next scan carries it.
        block
            .apply_parameter("step_1_out", Value::Float(15.0))
            .unwrap();
        step(&mut block, &io, true, false, 2);
        assert_eq!(out(&io), 15.0);

        // Lengthening step 2 to 3 scans delays the advance one scan.
        block
            .apply_parameter("step_2_ticks", Value::Int(3))
            .unwrap();
        step(&mut block, &io, true, false, 3);
        assert_eq!(active(&io), 2);
        step(&mut block, &io, true, false, 4);
        step(&mut block, &io, true, false, 5);
        assert_eq!(active(&io), 2);
        step(&mut block, &io, true, false, 6);
        assert_eq!(active(&io), 3);

        // `step_count` is fixed at construction; unknown names reject.
        assert!(matches!(
            block.apply_parameter("step_count", Value::Int(4)),
            Err(CommandError::InvalidParameter { ref parameter, .. }) if parameter == "step_count"
        ));
        assert!(matches!(
            block.apply_parameter("step_4_ticks", Value::Int(1)),
            Err(CommandError::UnknownParameter { ref parameter, .. }) if parameter == "step_4_ticks"
        ));
        assert!(matches!(
            block.apply_parameter("dwell", Value::Int(1)),
            Err(CommandError::UnknownParameter { ref parameter, .. }) if parameter == "dwell"
        ));
        assert!(matches!(
            block.apply_parameter("step_1_ticks", Value::Int(-1)),
            Err(CommandError::InvalidParameter { ref parameter, .. }) if parameter == "step_1_ticks"
        ));

        // The tuned table is part of the captured state a standby gets.
        let state = block.capture_state();
        assert_eq!(state.get("step_2_ticks"), Some(Value::Int(3)));
        assert_eq!(state.get("step_1_out"), Some(Value::Float(15.0)));
    }

    #[test]
    fn describes_itself() {
        let descriptor = component().describe();
        assert_eq!(descriptor.name, "seq");
        assert_eq!(descriptor.kind, Sequencer::KIND);
        assert_eq!(descriptor.label, "seq");
        assert_eq!(
            descriptor.ports,
            [
                PortDescriptor {
                    name: "run".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::ProcessValue),
                    point: None,
                },
                PortDescriptor {
                    name: "reset".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Status),
                    point: None,
                },
                PortDescriptor {
                    name: "out".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Float,
                    role: Some(PortRole::Output),
                    point: None,
                },
                PortDescriptor {
                    name: "step".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Int,
                    role: Some(PortRole::Status),
                    point: None,
                },
                PortDescriptor {
                    name: "done".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Status),
                    point: None,
                },
            ]
        );
        // Drift guard: the descriptor's parameter names are exactly the
        // keys `from_parameters` reads — `step_count` plus the indexed
        // `step_<n>_ticks`/`step_<n>_out` pairs.
        let names: Vec<&str> = descriptor
            .parameters
            .iter()
            .map(|parameter| parameter.name.as_str())
            .collect();
        assert_eq!(
            names,
            [
                "step_count",
                "step_1_ticks",
                "step_1_out",
                "step_2_ticks",
                "step_2_out",
                "step_3_ticks",
                "step_3_out",
            ]
        );
        assert_eq!(
            descriptor.parameters[0],
            ParameterDescriptor {
                name: "step_count".to_string(),
                kind: ValueKind::Int,
                range: Some(describe::POSITIVE_INT),
            }
        );
        assert_eq!(
            descriptor.parameters[1],
            ParameterDescriptor {
                name: "step_1_ticks".to_string(),
                kind: ValueKind::Int,
                range: Some(describe::NONNEGATIVE_INT),
            }
        );
        assert_eq!(
            descriptor.parameters[2],
            ParameterDescriptor {
                name: "step_1_out".to_string(),
                kind: ValueKind::Float,
                range: Some(describe::FINITE_F64),
            }
        );
    }

    /// Invokes the declared command surface — the hook
    /// `Command::Invoke` dispatches to at the scan boundary.
    fn invoke(
        block: &mut Sequencer,
        command: &str,
        arguments: &[(&str, Value)],
    ) -> Result<(), String> {
        block.invoke_command(
            command,
            &arguments
                .iter()
                .map(|(name, value)| (name.to_string(), *value))
                .collect(),
        )
    }

    #[test]
    fn declares_its_command_and_event_surface() {
        let descriptor = component().describe();
        assert_eq!(
            descriptor.commands,
            [
                CommandDecl {
                    name: "advance".to_string(),
                    request: vec![CommandArgument {
                        name: "count".to_string(),
                        kind: ValueKind::Int,
                    }],
                    availability: CommandAvailability::KindDeclared,
                },
                CommandDecl {
                    name: "reset".to_string(),
                    request: Vec::new(),
                    availability: CommandAvailability::Always,
                },
            ]
        );
        assert_eq!(
            descriptor.events,
            [EventDecl {
                name: "step_completed".to_string(),
                payload: vec![EventField {
                    name: "step".to_string(),
                    kind: EventFieldKind::Value(ValueKind::Int),
                    optional: false,
                }],
                retention: EventRetention::Journal,
            }]
        );
    }

    #[test]
    fn advance_and_reset_invoke_through_the_declared_hook() {
        let mut block = component();
        let seq_io = io();
        step(&mut block, &seq_io, true, false, 1);
        assert_eq!(active(&seq_io), 1);

        // `advance` without a count moves one step; the next scan
        // reports it.
        invoke(&mut block, "advance", &[]).unwrap();
        step(&mut block, &seq_io, true, false, 2);
        assert_eq!(active(&seq_io), 2);
        assert_eq!(out(&seq_io), 20.0);

        // A typed `count` argument paces further.
        invoke(&mut block, "advance", &[("count", Value::Int(1))]).unwrap();
        step(&mut block, &seq_io, true, false, 3);
        assert_eq!(active(&seq_io), 3);

        // Landing past the last step completes the run — the same
        // hold-at-end a `run`-paced table reaches.
        let mut at_end = component();
        let end_io = io();
        invoke(&mut at_end, "advance", &[("count", Value::Int(9))]).unwrap();
        step(&mut at_end, &end_io, true, false, 1);
        assert_eq!(active(&end_io), 3);
        assert!(done(&end_io));

        // `KindDeclared` availability: a completed table refuses
        // `advance` with the declared reason while `reset` serves.
        let refusal = invoke(&mut at_end, "advance", &[]).unwrap_err();
        assert_eq!(
            refusal,
            "the sequence has run to its end; reset restarts it"
        );
        invoke(&mut at_end, "reset", &[]).unwrap();
        step(&mut at_end, &end_io, true, false, 2);
        assert_eq!(active(&end_io), 1);
        assert!(!done(&end_io));

        // `count` below one is the kind invariant the schema cannot
        // express — refused, changing nothing.
        assert_eq!(
            invoke(&mut at_end, "advance", &[("count", Value::Int(0))]).unwrap_err(),
            "count must be at least 1"
        );
    }

    #[test]
    fn the_probe_reports_the_standing_advance_refusal() {
        // The probe is dispatch's standing predicate: `advance` answers
        // the completed-table refusal — the same text a refused
        // invocation settles — while `reset` stands invocable, and the
        // `count` domain stays dispatch-only.
        let mut block = component();
        let seq_io = io();

        // Mid-table: no standing refusal, dispatch applies.
        assert_eq!(block.command_refusal("advance"), None);
        invoke(&mut block, "advance", &[]).unwrap();

        // Run the table out: the probe's answer is the refusal the
        // receipted path would carry.
        for tick in 1..=6 {
            step(&mut block, &seq_io, true, false, tick);
        }
        assert!(done(&seq_io));
        let standing = block.command_refusal("advance");
        assert_eq!(
            standing.as_deref(),
            Some("the sequence has run to its end; reset restarts it")
        );
        assert_eq!(
            invoke(&mut block, "advance", &[]).unwrap_err(),
            standing.unwrap()
        );

        // `reset` is invocable throughout — probe and dispatch agree —
        // and clears the standing refusal again.
        assert_eq!(block.command_refusal("reset"), None);
        invoke(&mut block, "reset", &[]).unwrap();
        assert_eq!(block.command_refusal("advance"), None);
    }

    #[test]
    fn step_completed_emits_on_the_completing_scan() {
        let mut block = component();
        let io = io();

        // Step 1 banks its first scan: nothing completes, nothing
        // drains.
        step(&mut block, &io, true, false, 1);
        assert!(block.drain_events().is_empty());

        // Tick 2 runs step 1's declared ticks out — the event names the
        // completed step's 1-based index.
        step(&mut block, &io, true, false, 2);
        let events = block.drain_events();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].event, "step_completed");
        assert_eq!(events[0].fields["step"], EventValue::Value(Value::Int(1)));
        // The drain emptied the queue.
        assert!(block.drain_events().is_empty());

        // A parked or held scan emits nothing.
        step(&mut block, &io, false, false, 3);
        assert!(block.drain_events().is_empty());

        // The run walks to the end: step 2's second running scan
        // completes it on tick 5 and step 3's single scan completes the
        // table on tick 6 — one event each, in emission order.
        step(&mut block, &io, true, false, 4);
        assert!(block.drain_events().is_empty());
        step(&mut block, &io, true, false, 5);
        assert_eq!(
            block.drain_events()[0].fields["step"],
            EventValue::Value(Value::Int(2))
        );
        step(&mut block, &io, true, false, 6);
        assert_eq!(
            block.drain_events()[0].fields["step"],
            EventValue::Value(Value::Int(3))
        );
        // Held at the end, no further emissions.
        step(&mut block, &io, true, false, 7);
        assert!(block.drain_events().is_empty());
    }

    #[test]
    fn command_effects_ride_the_checkpoint() {
        // `advance` mutates only the checkpointed run state: the
        // captured `step`/`elapsed`/`done` carry the command's effect
        // to a restored instance.
        let mut block = component();
        let seq_io = io();
        step(&mut block, &seq_io, true, false, 1);
        invoke(&mut block, "advance", &[("count", Value::Int(2))]).unwrap();
        let state = block.capture_state();
        assert_eq!(state.get("step"), Some(Value::Int(3)));
        assert_eq!(state.get("elapsed"), Some(Value::Int(0)));
        assert_eq!(state.get("done"), Some(Value::Bool(false)));

        let mut restored = component();
        restored.restore_state(&state).unwrap();
        let restored_io = io();
        step(&mut restored, &restored_io, true, false, 1);
        assert_eq!(active(&restored_io), 3);
        assert_eq!(out(&restored_io), 30.0);
    }
}
