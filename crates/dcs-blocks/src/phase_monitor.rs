//! Phase monitor: the phase-conditioned verification checks
//! architecture decision 61 records for `WW-CTL-004`/`WW-OPS-002`
//! (`docs/research/filter-backwash.md`) — ripening turbidity under a
//! declared bound within a declared duration of return to service, and
//! clean-bed headloss within a declared deviation of its captured
//! baseline at a given flow.

use crate::describe;
use crate::params::{self, ParameterError, Parameters};
use dcs_core::{
    CommandError, ComponentDescriptor, ParameterRange, PointId, PortRole, Quality, QualityReason,
    Sample, StateError, StateMap, Tick, Value, ValueKind,
};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// What a [`PhaseMonitor`]'s `bound` applies to.
///
/// The model's parameter vocabulary has no string type, so the `mode`
/// parameter carries the choice as an `Int` code: `0` for `Absolute`,
/// `1` for `Deviation` — the values [`code`](Self::code) reports and
/// [`decode`](Self::decode) accepts.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PhaseMode {
    /// `bound` is an upper bound on `in` itself — the ripening check's
    /// "turbidity under a declared bound".
    Absolute,
    /// `bound` is a magnitude bound on `in − baseline`, the baseline
    /// the `capture` input holds — the clean-bed-headloss check's
    /// "deviation from the captured reference".
    Deviation,
}

impl PhaseMode {
    /// The `Int` code the `mode` parameter carries.
    pub const fn code(self) -> i64 {
        match self {
            Self::Absolute => 0,
            Self::Deviation => 1,
        }
    }

    /// The mode the `Int` code `code` selects, or `None` when the code
    /// declares no mode.
    pub const fn decode(code: i64) -> Option<Self> {
        match code {
            0 => Some(Self::Absolute),
            1 => Some(Self::Deviation),
            _ => None,
        }
    }
}

/// A phase monitor: verifies a measured value against a declared bound
/// while a condition window stands — the comparisons `alarm-monitor`
/// cannot express ungated: a phase gate, a value capture, and a
/// deadline.
///
/// **The window.** `phase` (`In`, `Bool`) is the condition window —
/// wired from decoded phase flags or the return-to-service state. The
/// window opens on the scan `phase` reads `true` and closes on the scan
/// it reads `false`. Each window carries its own verdict state — the
/// scans it has stood (`elapsed`), whether the bound has been met
/// (`met`), and the captured baseline — all reset as the window opens,
/// so every phase verifies on its own readings.
///
/// **The capture.** While `capture` (`In`, `Bool`) reads `true` inside
/// the window, the monitor tracks `in` as its baseline — the CBHL
/// "original at a given flow" reference; the last tracked value holds
/// when `capture` releases. The window owns the baseline's lifecycle:
/// `capture` asserted outside the window acts on nothing, and the held
/// baseline clears as the window closes — a fresh window verifies on a
/// fresh reference, never a stale one.
///
/// **The bound check.** `deviation` (`Out`, `Float`) reports
/// `in − baseline` — `0.0` until the window's first capture — and
/// `exceeded` (`Out`, `Bool`) is the excursion condition the alarm set
/// consumes. Under `mode` `0` the bound applies to `in` directly:
/// `exceeded` stands while `in` reads above `bound`. Under `mode` `1`
/// it applies to the deviation magnitude once a baseline exists:
/// `exceeded` stands while `|in − baseline|` exceeds `bound`; with no
/// captured baseline the window cannot evaluate and `exceeded` stays
/// clear. A difference overflowing `f64` saturates at `±f64::MAX` — a
/// deviation beyond the largest expressible magnitude trips any finite
/// `bound`.
///
/// **The deadline.** `overdue` (`Out`, `Bool`) is the bound-not-met
/// condition: the first window scan the bound is satisfied latches
/// `met` for the window's remainder; while `met` stands `overdue`
/// cannot assert, and once `elapsed` has passed `limit_ticks` without
/// it, `overdue` stands — clearing only if the bound is met late or the
/// window closes. Under `mode` `1` only a captured deviation can be
/// met: a window whose `capture` never runs meets nothing, so `overdue`
/// flags the unverified window at the deadline.
///
/// **Quiescent rule.** Outside the window the monitor is inert:
/// `deviation` reports `0.0`, `exceeded` and `overdue` stand clear, and
/// no state accrues — a `capture` read `true` while the window is
/// closed captures nothing.
///
/// **Quality rule.** A control or process input whose quality is not
/// `Good` is not trusted to act: a scan whose `in`, `phase`, or
/// `capture` sample is not `Good` — or whose `in` is not finite — is a
/// held scan: the window neither opens nor closes, `elapsed` stands,
/// nothing captures, and the outputs keep their standing values stamped
/// with the merged worst of the three input qualities, plus
/// `Bad(DeviceFault)` for a non-finite reading the point did not
/// report.
///
/// Declared I/O: `in` (`In`, `Float`), `phase` (`In`, `Bool`),
/// `capture` (`In`, `Bool`), `deviation` (`Out`, `Float`), `exceeded`
/// (`Out`, `Bool`), `overdue` (`Out`, `Bool`).
///
/// Parameters: `bound` — required finite, non-negative `Float`, the
/// absolute bound (`mode` `0`) or maximum deviation magnitude (`mode`
/// `1`); `limit_ticks` — required `Int` in `1..=i64::MAX`, the scans of
/// the open window within which the bound must first be met; and
/// `mode` — required `Int` carrying a [`PhaseMode`] code: `0` absolute,
/// `1` deviation. All three tune through `set_parameter`; retuning
/// keeps the open window's verdict state, so a shortened `limit_ticks`
/// past the banked `elapsed` asserts `overdue` at the next trusted
/// scan.
#[derive(Debug)]
pub struct PhaseMonitor {
    name: String,
    input: PointId,
    phase: PointId,
    capture: PointId,
    deviation_out: PointId,
    exceeded_out: PointId,
    overdue_out: PointId,
    /// The bound `exceeded`/`met` evaluate against — on `in` under
    /// `mode` `0`, on `|deviation|` under `mode` `1`.
    bound: f64,
    /// The scans of the open window the bound must first be met within.
    limit_ticks: u64,
    /// What `bound` applies to.
    mode: PhaseMode,
    /// Whether the phase window currently stands.
    window_open: bool,
    /// The trusted scans the open window has stood, clamped at
    /// `limit_ticks + 1` — the first count asserting `overdue`.
    elapsed: u64,
    /// The bound satisfied at least once this window — the latched
    /// answer to the deadline.
    met: bool,
    /// The window's captured `in` reference — `None` until the first
    /// capture of the open window.
    baseline: Option<f64>,
    /// The standing `in − baseline` report — `0.0` outside the window
    /// and before the window's first capture.
    deviation: f64,
    /// The standing excursion verdict.
    exceeded: bool,
    /// The standing deadline verdict.
    overdue: bool,
}

/// The six points a [`PhaseMonitor`] binds — the measured value, the
/// condition window, and the capture request it reads (`In`), and the
/// deviation report and the two verdicts it drives (`Out`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PhaseMonitorIo {
    /// `in` (`In`, `Float`): the measured value the check verifies.
    pub input: PointId,
    /// `phase` (`In`, `Bool`): the condition window.
    pub phase: PointId,
    /// `capture` (`In`, `Bool`): the baseline track-and-hold request.
    pub capture: PointId,
    /// `deviation` (`Out`, `Float`): the reported `in − baseline`.
    pub deviation: PointId,
    /// `exceeded` (`Out`, `Bool`): the excursion condition the alarm
    /// set consumes.
    pub exceeded: PointId,
    /// `overdue` (`Out`, `Bool`): the bound-not-met verdict.
    pub overdue: PointId,
}

/// The inclusive `Int` bound the `mode` parameter accepts: the declared
/// [`PhaseMode`] codes.
const MODE_RANGE: ParameterRange = ParameterRange {
    min: Value::Int(0),
    max: Value::Int(1),
};

impl PhaseMonitor {
    /// The component-kind string the model-driven registry maps onto
    /// this type's constructor.
    pub const KIND: &'static str = "phase-monitor";

    /// Builds the component from explicit points, the bound, the
    /// deadline in scans, and the check mode.
    ///
    /// `bound` must be finite and non-negative; `limit_ticks` must be
    /// at least `1` and may not exceed `i64::MAX` — the captured count
    /// is an `Int`. Violations are reported as a [`ParameterError`]
    /// naming the parameter.
    pub fn new(
        name: impl Into<String>,
        io: PhaseMonitorIo,
        bound: f64,
        limit_ticks: u64,
        mode: PhaseMode,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        Self::check_config(&name, bound, limit_ticks)?;
        Ok(Self {
            name,
            input: io.input,
            phase: io.phase,
            capture: io.capture,
            deviation_out: io.deviation,
            exceeded_out: io.exceeded,
            overdue_out: io.overdue,
            bound,
            limit_ticks,
            mode,
            window_open: false,
            elapsed: 0,
            met: false,
            baseline: None,
            deviation: 0.0,
            exceeded: false,
            overdue: false,
        })
    }

    /// The parameter invariants every construction and tuning path
    /// enforces, reported as a [`ParameterError`] naming `component`
    /// and the offending parameter.
    fn check_config(component: &str, bound: f64, limit_ticks: u64) -> Result<(), ParameterError> {
        if !bound.is_finite() {
            return Err(params::invalid(
                component,
                "bound",
                "must be finite".to_string(),
            ));
        }
        if bound < 0.0 {
            return Err(params::invalid(
                component,
                "bound",
                "must be non-negative".to_string(),
            ));
        }
        if limit_ticks == 0 {
            return Err(params::invalid(
                component,
                "limit_ticks",
                "must be at least 1".to_string(),
            ));
        }
        if limit_ticks > i64::MAX as u64 {
            return Err(params::invalid(
                component,
                "limit_ticks",
                format!("must not exceed i64::MAX, found {limit_ticks}"),
            ));
        }
        Ok(())
    }

    /// Builds the component from a plant-model parameter map, reading
    /// the parameters listed on the type's docs.
    pub fn from_parameters(
        name: impl Into<String>,
        io: PhaseMonitorIo,
        parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        let bound = params::required_f64(&name, parameters, "bound")?;
        let limit_ticks = params::required_u64(&name, parameters, "limit_ticks")?;
        let code = params::required_u64(&name, parameters, "mode")?;
        let mode = PhaseMode::decode(code as i64).ok_or_else(|| {
            params::invalid(
                &name,
                "mode",
                format!("expected 0 (absolute) or 1 (deviation), found {code}"),
            )
        })?;
        Self::new(name, io, bound, limit_ticks, mode)
    }

    /// Retunes one declared parameter, or reports a [`CommandError`]
    /// naming `component` and the offending parameter. A refused value
    /// changes nothing.
    fn tune(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        match parameter {
            "bound" => {
                let tuned = params::tune_f64(&self.name, parameter, value)?;
                if !tuned.is_finite() || tuned < 0.0 {
                    return Err(params::invalid_parameter(
                        &self.name,
                        parameter,
                        "must be finite and non-negative",
                    ));
                }
                self.bound = tuned;
            }
            "limit_ticks" => {
                let tuned = params::tune_u64(&self.name, parameter, value)?;
                if tuned == 0 || tuned > i64::MAX as u64 {
                    return Err(params::invalid_parameter(
                        &self.name,
                        parameter,
                        "must be in 1..=i64::MAX",
                    ));
                }
                // The banked count stands: a shortened deadline may
                // already have passed — `overdue` evaluates under the
                // new bound at the next trusted scan.
                self.limit_ticks = tuned;
            }
            "mode" => {
                let code = params::tune_u64(&self.name, parameter, value)?;
                self.mode = PhaseMode::decode(code as i64).ok_or_else(|| {
                    params::invalid_parameter(
                        &self.name,
                        parameter,
                        "expected 0 (absolute) or 1 (deviation)",
                    )
                })?;
            }
            _ => return Err(params::unknown_parameter(&self.name, parameter)),
        }
        Ok(())
    }

    /// The `elapsed` clamp: the first count asserting `overdue`,
    /// `limit_ticks + 1` — capped at `i64::MAX` so captured state stays
    /// `Int`-representable, a maximal deadline effectively never
    /// expiring.
    fn deadline_cap(&self) -> u64 {
        self.limit_ticks.saturating_add(1).min(i64::MAX as u64)
    }
}

impl Component for PhaseMonitor {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        vec![
            IoRequirement::input::<f64>("in", self.input),
            IoRequirement::input::<bool>("phase", self.phase),
            IoRequirement::input::<bool>("capture", self.capture),
            IoRequirement::output::<f64>("deviation", self.deviation_out),
            IoRequirement::output::<bool>("exceeded", self.exceeded_out),
            IoRequirement::output::<bool>("overdue", self.overdue_out),
        ]
    }

    fn step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError> {
        let input = io.read_typed::<f64>(self.input)?;
        let phase = io.read_typed::<bool>(self.phase)?;
        let capture = io.read_typed::<bool>(self.capture)?;

        // Every output carries the worst of the three inputs, and a
        // non-finite reading is a device fault the point did not
        // report — the verdict is only as trusted as its signals.
        let mut quality = input.quality.merge(phase.quality).merge(capture.quality);
        if !input.value.is_finite() {
            quality = quality.merge(Quality::Bad(QualityReason::DeviceFault));
        }

        // Only a fully trusted scan acts: an untrusted control input
        // neither opens nor closes the window, and an untrusted
        // measurement neither captures nor evaluates — the held scan.
        if quality.is_good() {
            if phase.value {
                if !self.window_open {
                    // The opening scan resets the window's verdict:
                    // each phase verifies on its own readings.
                    self.window_open = true;
                    self.elapsed = 0;
                    self.met = false;
                    self.baseline = None;
                    self.deviation = 0.0;
                    self.exceeded = false;
                    self.overdue = false;
                }
                self.elapsed = (self.elapsed + 1).min(self.deadline_cap());
                if capture.value {
                    self.baseline = Some(input.value);
                }
                self.deviation = match self.baseline {
                    Some(baseline) => {
                        let difference = input.value - baseline;
                        if difference.is_finite() {
                            difference
                        } else {
                            difference.signum() * f64::MAX
                        }
                    }
                    None => 0.0,
                };
                match self.mode {
                    PhaseMode::Absolute => {
                        self.exceeded = input.value > self.bound;
                        if input.value <= self.bound {
                            self.met = true;
                        }
                    }
                    PhaseMode::Deviation => {
                        // Uncaptured the window cannot evaluate:
                        // nothing is met and nothing exceeds.
                        if self.baseline.is_some() {
                            self.exceeded = self.deviation.abs() > self.bound;
                            if !self.exceeded {
                                self.met = true;
                            }
                        } else {
                            self.exceeded = false;
                        }
                    }
                }
                self.overdue = !self.met && self.elapsed > self.limit_ticks;
            } else {
                // The closed window is quiescent: outputs stand clear
                // and nothing accrues.
                self.window_open = false;
                self.elapsed = 0;
                self.met = false;
                self.baseline = None;
                self.deviation = 0.0;
                self.exceeded = false;
                self.overdue = false;
            }
        }

        io.write_sample(
            self.deviation_out,
            Sample::new(Value::Float(self.deviation), quality, tick),
        )?;
        io.write_sample(
            self.exceeded_out,
            Sample::new(Value::Bool(self.exceeded), quality, tick),
        )?;
        io.write_sample(
            self.overdue_out,
            Sample::new(Value::Bool(self.overdue), quality, tick),
        )?;
        Ok(())
    }

    /// Describes the monitor: `in` is the measured value, `phase` the
    /// condition window, `capture` the baseline hold, `deviation` the
    /// reported `in − baseline`, `exceeded` and `overdue` the
    /// conditions the alarm set consumes; the declared parameter set is
    /// the three keys `from_parameters` reads.
    fn describe(&self) -> ComponentDescriptor {
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &[
                ("in", PortRole::ProcessValue),
                ("phase", PortRole::ProcessValue),
                ("capture", PortRole::Status),
                ("deviation", PortRole::Output),
                ("exceeded", PortRole::Status),
                ("overdue", PortRole::Status),
            ],
            vec![
                describe::parameter("bound", ValueKind::Float, Some(describe::NONNEGATIVE_F64)),
                describe::parameter("limit_ticks", ValueKind::Int, Some(describe::POSITIVE_INT)),
                describe::parameter("mode", ValueKind::Int, Some(MODE_RANGE)),
            ],
        )
    }

    /// Tunes the declared parameters at the scan boundary — the
    /// recorded mechanism for operator-adjustable bounds. Retuning
    /// keeps the open window's verdict state; a refused value changes
    /// nothing.
    fn apply_parameter(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        self.tune(parameter, value)
    }

    /// Reports the tuned `bound`/`limit_ticks`/`mode` — the same fields
    /// [`capture_state`](Self::capture_state) checkpoints, so the
    /// faceplate and a tracking standby read one vocabulary.
    fn report_parameters(&self) -> StateMap {
        let mut parameters = StateMap::new();
        parameters.insert("bound", Value::Float(self.bound));
        parameters.insert("limit_ticks", Value::Int(self.limit_ticks as i64));
        parameters.insert("mode", Value::Int(self.mode.code()));
        parameters
    }

    /// Captures the tuned parameters and the open window's verdict
    /// state — the held baseline, the deadline count, the `met` latch,
    /// and the standing outputs — so a checkpointed standby continues a
    /// mid-window verification identically.
    fn capture_state(&self) -> StateMap {
        let mut state = self.report_parameters();
        state.insert("window_open", Value::Bool(self.window_open));
        state.insert("elapsed", Value::Int(self.elapsed as i64));
        state.insert("met", Value::Bool(self.met));
        state.insert("captured", Value::Bool(self.baseline.is_some()));
        state.insert("baseline", Value::Float(self.baseline.unwrap_or(0.0)));
        state.insert("deviation", Value::Float(self.deviation));
        state.insert("exceeded", Value::Bool(self.exceeded));
        state.insert("overdue", Value::Bool(self.overdue));
        state
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        state.ensure_known_fields(
            &self.name,
            &[
                "bound",
                "limit_ticks",
                "mode",
                "window_open",
                "elapsed",
                "met",
                "captured",
                "baseline",
                "deviation",
                "exceeded",
                "overdue",
            ],
        )?;

        // The whole map is validated before any state mutates, so a
        // rejected checkpoint leaves the component on its last-good
        // state rather than half-applied.
        let bound = state.require_f64(&self.name, "bound")?;
        let limit_ticks = state.require_i64(&self.name, "limit_ticks")?;
        let mode = state.require_i64(&self.name, "mode")?;
        let window_open = state.require_bool(&self.name, "window_open")?;
        let elapsed = state.require_i64(&self.name, "elapsed")?;
        let met = state.require_bool(&self.name, "met")?;
        let captured = state.require_bool(&self.name, "captured")?;
        let baseline = state.require_f64(&self.name, "baseline")?;
        let deviation = state.require_f64(&self.name, "deviation")?;
        let exceeded = state.require_bool(&self.name, "exceeded")?;
        let overdue = state.require_bool(&self.name, "overdue")?;

        // The same invariants `new` and `apply_parameter` enforce.
        if !bound.is_finite() || bound < 0.0 {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "bound".to_string(),
                value: Value::Float(bound),
            });
        }
        if !(1..=i64::MAX).contains(&limit_ticks) {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "limit_ticks".to_string(),
                value: Value::Int(limit_ticks),
            });
        }
        let mode = PhaseMode::decode(mode).ok_or_else(|| StateError::InvalidValue {
            element: self.name.clone(),
            field: "mode".to_string(),
            value: Value::Int(mode),
        })?;
        if !baseline.is_finite() || !deviation.is_finite() {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: if baseline.is_finite() {
                    "deviation".to_string()
                } else {
                    "baseline".to_string()
                },
                value: Value::Float(if baseline.is_finite() {
                    deviation
                } else {
                    baseline
                }),
            });
        }
        let limit_ticks = limit_ticks as u64;
        // The count is clamped at the deadline cap — `1..=cap` is the
        // only range an open window's capture produces, and a closed
        // window captures only the cleared set.
        let cap = limit_ticks.saturating_add(1).min(i64::MAX as u64);
        if window_open {
            if !(1..=cap as i64).contains(&elapsed) {
                return Err(StateError::InvalidValue {
                    element: self.name.clone(),
                    field: "elapsed".to_string(),
                    value: Value::Int(elapsed),
                });
            }
            if !captured && (baseline != 0.0 || deviation != 0.0) {
                return Err(StateError::InvalidValue {
                    element: self.name.clone(),
                    field: "baseline".to_string(),
                    value: Value::Float(baseline),
                });
            }
            // `overdue` is `!met && elapsed > limit_ticks` — the only
            // verdict a captured open window reports.
            if overdue && (met || elapsed <= limit_ticks as i64) {
                return Err(StateError::InvalidValue {
                    element: self.name.clone(),
                    field: "overdue".to_string(),
                    value: Value::Bool(overdue),
                });
            }
        } else if elapsed != 0
            || met
            || captured
            || baseline != 0.0
            || deviation != 0.0
            || exceeded
            || overdue
        {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "window_open".to_string(),
                value: Value::Bool(window_open),
            });
        }

        self.bound = bound;
        self.limit_ticks = limit_ticks;
        self.mode = mode;
        self.window_open = window_open;
        self.elapsed = elapsed as u64;
        self.met = met;
        self.baseline = captured.then_some(baseline);
        self.deviation = deviation;
        self.exceeded = exceeded;
        self.overdue = overdue;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::TestIo;
    use dcs_core::{Direction, ParameterDescriptor, PortDescriptor};

    const IN: PointId = PointId(60);
    const PHASE: PointId = PointId(61);
    const CAPTURE: PointId = PointId(62);
    const DEVIATION: PointId = PointId(70);
    const EXCEEDED: PointId = PointId(71);
    const OVERDUE: PointId = PointId(72);

    fn points() -> PhaseMonitorIo {
        PhaseMonitorIo {
            input: IN,
            phase: PHASE,
            capture: CAPTURE,
            deviation: DEVIATION,
            exceeded: EXCEEDED,
            overdue: OVERDUE,
        }
    }

    /// A mode-0 monitor: `in` bounded above by 0.5, deadline 5 scans.
    fn component() -> PhaseMonitor {
        PhaseMonitor::new("phm", points(), 0.5, 5, PhaseMode::Absolute).unwrap()
    }

    /// A mode-1 monitor: `|in − baseline|` bounded by 0.1, deadline 5.
    fn deviation_component() -> PhaseMonitor {
        PhaseMonitor::new("phm", points(), 0.1, 5, PhaseMode::Deviation).unwrap()
    }

    fn io(field: Sample) -> TestIo {
        TestIo::new(&[
            (IN, Direction::In, field),
            (
                PHASE,
                Direction::In,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
            (
                CAPTURE,
                Direction::In,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
            (
                DEVIATION,
                Direction::Out,
                Sample::good(Value::Float(0.0), Tick::ZERO),
            ),
            (
                EXCEEDED,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
            (
                OVERDUE,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
        ])
    }

    fn feed(io: &TestIo, input: f64, phase: bool, capture: bool, tick: u64) {
        io.feed(IN, Sample::good(Value::Float(input), Tick(tick)));
        io.feed(PHASE, Sample::good(Value::Bool(phase), Tick(tick)));
        io.feed(CAPTURE, Sample::good(Value::Bool(capture), Tick(tick)));
    }

    fn deviation(io: &TestIo) -> f64 {
        match io.written(DEVIATION).unwrap().value {
            Value::Float(value) => value,
            value => panic!("expected Float, got {value:?}"),
        }
    }

    fn exceeded(io: &TestIo) -> bool {
        io.written(EXCEEDED).unwrap().value == Value::Bool(true)
    }

    fn overdue(io: &TestIo) -> bool {
        io.written(OVERDUE).unwrap().value == Value::Bool(true)
    }

    #[test]
    fn absolute_mode_trips_and_meets_within_the_window() {
        let mut block = component();
        let io = io(Sample::good(Value::Float(0.0), Tick::ZERO));

        // Quiescent before the window: `in` already violates the bound
        // but the monitor reports nothing while `phase` stands clear.
        feed(&io, 2.0, false, false, 1);
        block.step(&io, Tick(1)).unwrap();
        assert!(!exceeded(&io));
        assert!(!overdue(&io));

        // The opening scan evaluates: above bound — exceeded, unmet.
        feed(&io, 2.0, true, false, 2);
        block.step(&io, Tick(2)).unwrap();
        assert!(exceeded(&io));
        assert!(!overdue(&io));

        // `in` coming within bound latches `met`: `exceeded` clears.
        feed(&io, 0.3, true, false, 3);
        block.step(&io, Tick(3)).unwrap();
        assert!(!exceeded(&io));

        // A re-excursion past the deadline cannot raise `overdue`: the
        // bound was met within it.
        for tick in 4..=9 {
            feed(&io, 0.7, true, false, tick);
            block.step(&io, Tick(tick)).unwrap();
            assert!(exceeded(&io), "tick={tick}");
            assert!(!overdue(&io), "tick={tick}");
        }

        // The closing scan returns the monitor to quiescent.
        feed(&io, 0.7, false, false, 10);
        block.step(&io, Tick(10)).unwrap();
        assert!(!exceeded(&io));
        assert_eq!(deviation(&io), 0.0);
    }

    #[test]
    fn overdue_asserts_at_the_deadline_and_clears_on_a_late_meet() {
        let mut block = component();
        let io = io(Sample::good(Value::Float(2.0), Tick::ZERO));

        // Window opens at tick 1; scans 1..=5 are within the deadline.
        feed(&io, 2.0, true, false, 1);
        for tick in 1..=5 {
            block.step(&io, Tick(tick)).unwrap();
            assert!(!overdue(&io), "tick={tick}");
            assert!(exceeded(&io), "tick={tick}");
        }
        // Tick 6 is the first scan past `limit_ticks` unmet.
        block.step(&io, Tick(6)).unwrap();
        assert!(overdue(&io));
        assert!(exceeded(&io));

        // The bound met late clears both flags: the deadline's answer
        // stands recorded downstream, the standing condition is over.
        feed(&io, 0.4, true, false, 7);
        block.step(&io, Tick(7)).unwrap();
        assert!(!overdue(&io));
        assert!(!exceeded(&io));
    }

    #[test]
    fn a_window_open_on_bound_never_overdues() {
        let mut block = component();
        let io = io(Sample::good(Value::Float(0.2), Tick::ZERO));

        // Within bound from the opening scan: `met` latches at once
        // and the deadline never fires however long the window stands.
        feed(&io, 0.2, true, false, 1);
        for tick in 1..=10 {
            block.step(&io, Tick(tick)).unwrap();
            assert!(!exceeded(&io), "tick={tick}");
            assert!(!overdue(&io), "tick={tick}");
        }
    }

    #[test]
    fn deviation_mode_captures_the_baseline_then_monitors() {
        let mut block = deviation_component();
        let io = io(Sample::good(Value::Float(0.0), Tick::ZERO));

        // Capture asserted outside the window acts on nothing.
        feed(&io, 9.0, false, true, 1);
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(deviation(&io), 0.0);
        assert!(!exceeded(&io));

        // Window opens with `capture` standing: the baseline tracks
        // `in`, `deviation` reports 0, and the bound reads met.
        feed(&io, 1.0, true, true, 2);
        for tick in 2..=4 {
            block.step(&io, Tick(tick)).unwrap();
            assert_eq!(deviation(&io), 0.0, "tick={tick}");
            assert!(!exceeded(&io), "tick={tick}");
        }

        // Release holds the last tracked baseline; `deviation`
        // reports `in − baseline` and `exceeded` bounds its magnitude.
        feed(&io, 1.0, true, false, 5);
        block.step(&io, Tick(5)).unwrap();
        assert_eq!(deviation(&io), 0.0);
        feed(&io, 1.25, true, false, 6);
        block.step(&io, Tick(6)).unwrap();
        assert_eq!(deviation(&io), 0.25);
        assert!(exceeded(&io));
        // A negative excursion trips the magnitude bound the same way.
        feed(&io, 0.875, true, false, 7);
        block.step(&io, Tick(7)).unwrap();
        assert_eq!(deviation(&io), -0.125);
        assert!(exceeded(&io));
        feed(&io, 0.9375, true, false, 8);
        block.step(&io, Tick(8)).unwrap();
        assert_eq!(deviation(&io), -0.0625);
        assert!(!exceeded(&io), "0.0625 is within bound 0.1");

        // The closing scan clears the baseline: a fresh window
        // re-captures rather than verifying on a stale reference.
        feed(&io, 0.85, false, false, 9);
        block.step(&io, Tick(9)).unwrap();
        assert_eq!(deviation(&io), 0.0);
        feed(&io, 5.0, true, false, 10);
        block.step(&io, Tick(10)).unwrap();
        assert_eq!(
            deviation(&io),
            0.0,
            "no baseline carried into the new window"
        );
        assert!(!exceeded(&io));
    }

    #[test]
    fn deviation_mode_without_capture_reports_overdue() {
        let mut block = deviation_component();
        let io = io(Sample::good(Value::Float(2.0), Tick::ZERO));

        // No `capture` ever runs: no baseline, nothing meets the
        // bound, and `exceeded` stays clear — the window is
        // unverifiable, so `overdue` flags it at the deadline.
        feed(&io, 2.0, true, false, 1);
        for tick in 1..=5 {
            block.step(&io, Tick(tick)).unwrap();
            assert!(!overdue(&io), "tick={tick}");
            assert!(!exceeded(&io), "tick={tick}");
            assert_eq!(deviation(&io), 0.0, "tick={tick}");
        }
        block.step(&io, Tick(6)).unwrap();
        assert!(overdue(&io));
    }

    #[test]
    fn held_scans_freeze_the_window_on_any_untrusted_input() {
        let mut block = component();
        let io = io(Sample::good(Value::Float(2.0), Tick::ZERO));

        // Window standing three scans unmet, `exceeded` asserted.
        feed(&io, 2.0, true, false, 1);
        for tick in 1..=3 {
            block.step(&io, Tick(tick)).unwrap();
        }
        assert!(exceeded(&io));

        // A non-`Good` `phase` holds the window open — the deadline
        // count stands and the outputs keep their standing verdicts
        // stamped with the merged quality.
        io.feed(
            PHASE,
            Sample::new(
                Value::Bool(true),
                Quality::Uncertain(QualityReason::Stale),
                Tick(4),
            ),
        );
        block.step(&io, Tick(4)).unwrap();
        assert!(exceeded(&io));
        assert_eq!(
            io.written(EXCEEDED).unwrap().quality,
            Quality::Uncertain(QualityReason::Stale)
        );

        // A non-`Good` `in` holds likewise; a non-finite `in` reads as
        // a device fault on top.
        io.feed(PHASE, Sample::good(Value::Bool(true), Tick(5)));
        io.feed(
            IN,
            Sample::new(Value::Float(f64::NAN), Quality::Good, Tick(5)),
        );
        block.step(&io, Tick(5)).unwrap();
        assert!(exceeded(&io));
        assert_eq!(
            io.written(OVERDUE).unwrap().quality,
            Quality::Bad(QualityReason::DeviceFault)
        );

        // A non-`Good` `capture` holds too — nothing captures on an
        // untrusted assertion.
        io.feed(IN, Sample::good(Value::Float(0.4), Tick(6)));
        io.feed(
            CAPTURE,
            Sample::new(
                Value::Bool(true),
                Quality::Bad(QualityReason::CommunicationFault),
                Tick(6),
            ),
        );
        block.step(&io, Tick(6)).unwrap();
        assert_eq!(
            io.written(DEVIATION).unwrap().quality,
            Quality::Bad(QualityReason::CommunicationFault)
        );

        // The held scans counted nothing: the deadline still lands on
        // its declared count — elapsed resumes at 4 of 5.
        io.feed(CAPTURE, Sample::good(Value::Bool(false), Tick(7)));
        feed(&io, 2.0, true, false, 7);
        block.step(&io, Tick(7)).unwrap();
        assert!(!overdue(&io), "elapsed 4 of 5");
        feed(&io, 2.0, true, false, 8);
        block.step(&io, Tick(8)).unwrap();
        assert!(!overdue(&io), "elapsed 5 of 5");
        feed(&io, 2.0, true, false, 9);
        block.step(&io, Tick(9)).unwrap();
        assert!(overdue(&io), "elapsed 6 past the deadline");
    }

    #[test]
    fn a_restored_monitor_continues_mid_window() {
        let mut block = deviation_component();
        let io = io(Sample::good(Value::Float(0.0), Tick::ZERO));

        // Capture across two scans, release, then deviate past the
        // bound: the window's baseline, count, and verdict are banked.
        feed(&io, 1.0, true, true, 1);
        for tick in 1..=2 {
            block.step(&io, Tick(tick)).unwrap();
        }
        feed(&io, 1.0, true, false, 3);
        block.step(&io, Tick(3)).unwrap();
        let state = block.capture_state();
        assert_eq!(state.get("baseline"), Some(Value::Float(1.0)));
        assert_eq!(state.get("captured"), Some(Value::Bool(true)));
        assert_eq!(state.get("elapsed"), Some(Value::Int(3)));
        assert_eq!(state.get("met"), Some(Value::Bool(true)));

        let mut standby = deviation_component();
        standby.restore_state(&state).unwrap();
        assert_eq!(standby.capture_state(), state);

        feed(&io, 1.25, true, false, 4);
        standby.step(&io, Tick(4)).unwrap();
        assert_eq!(deviation(&io), 0.25);
        assert!(exceeded(&io));
    }

    #[test]
    fn restore_validates_the_captured_vocabulary() {
        let mut block = component();
        let valid = block.capture_state();

        // Foreign and missing fields are named errors.
        let mut foreign = valid.clone();
        foreign.insert("foreign", Value::Int(1));
        assert!(matches!(
            block.restore_state(&foreign),
            Err(StateError::UnknownField { ref field, .. }) if field == "foreign"
        ));
        let mut missing = StateMap::new();
        missing.insert("bound", Value::Float(0.5));
        missing.insert("limit_ticks", Value::Int(5));
        missing.insert("mode", Value::Int(0));
        missing.insert("window_open", Value::Bool(false));
        assert!(matches!(
            block.restore_state(&missing),
            Err(StateError::MissingField { ref field, .. }) if field == "elapsed"
        ));
        let mut mistyped = valid.clone();
        mistyped.insert("baseline", Value::Int(1));
        assert!(matches!(
            block.restore_state(&mistyped),
            Err(StateError::IncompatibleField { ref field, .. }) if field == "baseline"
        ));

        // Cross-field invariants: an open window's count sits in
        // `1..=limit_ticks + 1`, and `overdue` cannot stand where the
        // bound was met or the deadline unreached.
        let mut open = valid.clone();
        open.insert("window_open", Value::Bool(true));
        open.insert("elapsed", Value::Int(0));
        assert!(matches!(
            block.restore_state(&open),
            Err(StateError::InvalidValue { ref field, .. }) if field == "elapsed"
        ));
        open.insert("elapsed", Value::Int(6));
        open.insert("overdue", Value::Bool(true));
        open.insert("met", Value::Bool(true));
        assert!(matches!(
            block.restore_state(&open),
            Err(StateError::InvalidValue { ref field, .. }) if field == "overdue"
        ));
    }

    #[test]
    fn builds_from_parameter_map() {
        let parameters: Parameters = [
            ("bound".to_string(), Value::Float(0.5)),
            ("limit_ticks".to_string(), Value::Int(5)),
            ("mode".to_string(), Value::Int(1)),
        ]
        .into_iter()
        .collect();
        let block = PhaseMonitor::from_parameters("phm", points(), &parameters).unwrap();
        assert_eq!(block.bound, 0.5);
        assert_eq!(block.limit_ticks, 5);
        assert_eq!(block.mode, PhaseMode::Deviation);

        for (parameters, parameter) in [
            (Parameters::new(), "bound"),
            (
                [
                    ("bound".to_string(), Value::Float(-0.5)),
                    ("limit_ticks".to_string(), Value::Int(5)),
                    ("mode".to_string(), Value::Int(0)),
                ]
                .into_iter()
                .collect(),
                "bound",
            ),
            (
                [
                    ("bound".to_string(), Value::Float(0.5)),
                    ("limit_ticks".to_string(), Value::Int(0)),
                    ("mode".to_string(), Value::Int(0)),
                ]
                .into_iter()
                .collect(),
                "limit_ticks",
            ),
            (
                [
                    ("bound".to_string(), Value::Float(0.5)),
                    ("limit_ticks".to_string(), Value::Int(5)),
                    ("mode".to_string(), Value::Int(2)),
                ]
                .into_iter()
                .collect(),
                "mode",
            ),
        ] {
            assert!(matches!(
                PhaseMonitor::from_parameters("phm", points(), &parameters).unwrap_err(),
                ParameterError::Missing { parameter: ref p, .. }
                | ParameterError::Invalid { parameter: ref p, .. } if p == parameter
            ));
        }
    }

    #[test]
    fn tunes_declared_parameters_at_the_scan_boundary() {
        let mut block = component();
        let io = io(Sample::good(Value::Float(0.0), Tick::ZERO));

        block.apply_parameter("bound", Value::Float(1.0)).unwrap();
        block.apply_parameter("limit_ticks", Value::Int(3)).unwrap();
        block.apply_parameter("mode", Value::Int(1)).unwrap();
        assert_eq!(block.bound, 1.0);
        assert_eq!(block.limit_ticks, 3);
        assert_eq!(block.mode, PhaseMode::Deviation);

        // A refused value changes nothing, naming the parameter.
        assert!(matches!(
            block.apply_parameter("bound", Value::Float(-1.0)),
            Err(CommandError::InvalidParameter { ref parameter, .. }) if parameter == "bound"
        ));
        assert!(matches!(
            block.apply_parameter("mode", Value::Int(4)),
            Err(CommandError::InvalidParameter { ref parameter, .. }) if parameter == "mode"
        ));
        assert!(matches!(
            block.apply_parameter("nope", Value::Int(0)),
            Err(CommandError::UnknownParameter { ref parameter, .. }) if parameter == "nope"
        ));

        // An uncaptured deviation window meets nothing: the tuned
        // deadline fires on the first scan past `limit_ticks`.
        feed(&io, 2.0, true, false, 1);
        for tick in 1..=3 {
            block.step(&io, Tick(tick)).unwrap();
            assert!(!overdue(&io), "elapsed {tick} of 3");
        }
        block.step(&io, Tick(4)).unwrap();
        assert!(overdue(&io), "elapsed 4 past the deadline");
    }

    #[test]
    fn describes_itself() {
        let descriptor = component().describe();
        assert_eq!(descriptor.name, "phm");
        assert_eq!(descriptor.kind, PhaseMonitor::KIND);
        assert_eq!(descriptor.label, "phm");
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
                    name: "phase".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::ProcessValue),
                    point: None,
                },
                PortDescriptor {
                    name: "capture".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Status),
                    point: None,
                },
                PortDescriptor {
                    name: "deviation".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Float,
                    role: Some(PortRole::Output),
                    point: None,
                },
                PortDescriptor {
                    name: "exceeded".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Status),
                    point: None,
                },
                PortDescriptor {
                    name: "overdue".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Status),
                    point: None,
                },
            ]
        );
        assert_eq!(
            descriptor.parameters,
            [
                ParameterDescriptor {
                    name: "bound".to_string(),
                    kind: ValueKind::Float,
                    range: Some(describe::NONNEGATIVE_F64),
                },
                ParameterDescriptor {
                    name: "limit_ticks".to_string(),
                    kind: ValueKind::Int,
                    range: Some(describe::POSITIVE_INT),
                },
                ParameterDescriptor {
                    name: "mode".to_string(),
                    kind: ValueKind::Int,
                    range: Some(MODE_RANGE),
                },
            ]
        );
        // The descriptor's names are exactly the `from_parameters`
        // key set.
        let mut keyless = Parameters::new();
        for parameter in &descriptor.parameters {
            let value = match parameter.name.as_str() {
                "bound" => Value::Float(0.5),
                "limit_ticks" | "mode" => Value::Int(1),
                other => panic!("undocumented parameter {other}"),
            };
            keyless.insert(parameter.name.clone(), value);
        }
        PhaseMonitor::from_parameters("phm", points(), &keyless).unwrap();
    }

    #[test]
    fn identical_input_sequences_produce_identical_state() {
        let run = || {
            let mut block = deviation_component();
            let io = io(Sample::good(Value::Float(0.0), Tick::ZERO));
            let mut written = Vec::new();
            for (tick, input, phase, capture) in [
                (1, 1.0, true, true),
                (2, 1.0, true, true),
                (3, 1.2, true, false),
                (4, 0.9, true, false),
                (5, 0.9, false, false),
            ] {
                feed(&io, input, phase, capture, tick);
                block.step(&io, Tick(tick)).unwrap();
                written.push(io.written(EXCEEDED).unwrap());
            }
            (block.capture_state(), written)
        };
        assert_eq!(run(), run());
    }
}
