//! Threshold chain: the ordered start/stop setpoint table driving a
//! pumping station's stage-count demand — the threshold-control half of
//! the station level-control contract architecture decision 42 records.
//!
//! The chain holds a demand between `0` (all pumps stopped) and `2`
//! (duty plus lag) and advances it as the level crosses the declared
//! setpoints; the `start`/`stop` and `lag_start`/`start` pairs are the
//! hysteresis keeping pump calls from chattering at their thresholds.

use crate::describe;
use crate::params::{self, ParameterError, Parameters};
use dcs_core::{
    CommandError, ComponentDescriptor, ParameterRange, PointId, PortRole, Quality, Sample,
    StateError, StateMap, Tick, Value, ValueKind,
};
use dcs_runtime::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};

/// The ordered setpoint table a [`ThresholdChain`] runs under — its
/// parameter map's six keys as one value.
///
/// The setpoints are engineering-unit levels and must be strictly
/// increasing: `cutoff` < `stop` < `start` < `lag_start` < `high`. All
/// five must be finite. `on_bad_demand` is the stage count the chain
/// emits while the level measurement is untrusted — the decision's
/// declared answer to the all-measurement-bad case, never a silent
/// default — and must be `0`, `1`, or `2`.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct SetpointTable {
    /// The low-water cut-off: at or below it every pump call releases —
    /// the dry-run protection floor.
    pub cutoff: f64,
    /// The pump-down stop level: a held demand releases to `0` at or
    /// below it — the bottom of the duty call's hysteresis band.
    pub stop: f64,
    /// The duty start level: demand rises to `1` at or above it — the
    /// top of the duty call's band, and the lag call's release level.
    pub start: f64,
    /// The lag start level: demand rises to `2` at or above it.
    pub lag_start: f64,
    /// The high-level alarm threshold: `high_level` asserts at or above
    /// it.
    pub high: f64,
    /// The stage count emitted while `level` is non-`Good`: `0`, `1`,
    /// or `2`.
    pub on_bad_demand: i64,
}

/// The output points a [`ThresholdChain`] reports on: `demand` (`Out`,
/// `Int`) — the stage count feeding `pump-group.demand` — `duty_call`
/// and `lag_call` (`Out`, `Bool`), the pump calls the alarm set and
/// faceplate consume, and the threshold-condition flags `below_cutoff`
/// and `high_level` (`Out`, `Bool`).
#[derive(Debug, Clone, Copy)]
pub struct ThresholdOutputs {
    /// `demand` — the held stage count: `0` all stopped, `1` duty
    /// called, `2` duty plus lag.
    pub demand: PointId,
    /// `duty_call` — asserts while the held demand is at least `1`.
    pub duty_call: PointId,
    /// `lag_call` — asserts while the held demand is `2`.
    pub lag_call: PointId,
    /// `below_cutoff` — asserts while a `Good` level reads at or below
    /// `cutoff`; reports `false` while the level is untrusted.
    pub below_cutoff: PointId,
    /// `high_level` — asserts while a `Good` level reads at or above
    /// `high`; reports `false` while the level is untrusted.
    pub high_level: PointId,
}

/// The inclusive `Int` bound `on_bad_demand` accepts: the chain's stage
/// vocabulary `0`, `1`, `2`.
const DEMAND_RANGE: ParameterRange = ParameterRange {
    min: Value::Int(0),
    max: Value::Int(2),
};

/// A station threshold chain: reads `level` (`In`, `Float`) — normally
/// the `failover-select`'s chosen measurement — and drives `demand`
/// (`Out`, `Int`), the stage count a `pump-group` consumes, plus the
/// pump-call and alarm flags the station surface reports.
///
/// **The setpoint chain.** The table is ordered `cutoff` < `stop` <
/// `start` < `lag_start` < `high`; a `level` at or below `stop` —
/// including at or below the `cutoff` floor — releases every call
/// (`demand` `0`). Above `stop` the held demand advances:
///
/// - from `0`, a level at or above `start` calls the duty pump
///   (`demand` `1`), and a level at or above `lag_start` calls the lag
///   directly (`demand` `2`) — a fast fill need not wait through the
///   duty stage;
/// - from `1`, a level at or above `lag_start` raises `demand` to `2`;
/// - from `2`, a level at or below `start` releases the lag first —
///   `demand` `1` — and a level at or below `stop` ends the pump-down
///   (`demand` `0`), matching the recorded lag-stops-first behavior.
///
/// The `start`/`stop` gap is the duty call's hysteresis: a called duty
/// pump runs until the level is pumped down to `stop`, and a stopped
/// station stays stopped until the level reaches `start`. The lag
/// call's hysteresis is the `lag_start`/`start` pair likewise. Inside
/// each band the demand holds its last value.
///
/// **Bad-measurement rule.** A `level` whose quality is not `Good`
/// cannot drive the chain: the demand falls to the declared
/// `on_bad_demand` and the held state follows it, so recovery resumes
/// the chain from the fallback stage — not from whatever the failed
/// measurement last implied. The condition flags report `false` on an
/// untrusted level: the chain never proves a threshold condition it
/// cannot read, and the measurement failure itself surfaces through
/// the level point's own quality and the failover source's alarmed
/// `backup_active` — the source-failover interaction decision 42
/// records. A `Good`-quality `NaN` satisfies no comparison: the demand
/// holds and the flags clear, the same hold-the-state rule
/// `alarm-monitor` documents.
///
/// **Quality.** Every output is written with [`Quality::Good`]: each is
/// the chain's own computed decision under these rules — `demand`
/// included, so a downstream `pump-group` honors the declared
/// `on_bad_demand` rather than holding its last `Good` stage request.
///
/// Declared I/O: `level` (`In`, `Float`); `demand` (`Out`, `Int`),
/// `duty_call` (`Out`, `Bool`), `lag_call` (`Out`, `Bool`),
/// `below_cutoff` (`Out`, `Bool`), `high_level` (`Out`, `Bool`).
///
/// Parameters: `cutoff`, `stop`, `start`, `lag_start`, `high` —
/// required finite `Float`s in strictly increasing order — and
/// `on_bad_demand`, a required `Int` in `0..=2`. All six are tunable
/// through `set_parameter`; a setpoint retune that would break the
/// ordering is refused naming the parameter.
#[derive(Debug)]
pub struct ThresholdChain {
    name: String,
    level: PointId,
    outputs: ThresholdOutputs,
    table: SetpointTable,
    /// The held stage count — `0`, `1`, or `2`; the chain's one piece
    /// of run state, checkpointed so a standby resumes mid-cycle.
    demand: i64,
}

impl SetpointTable {
    /// Validates the invariants every construction path enforces — all
    /// setpoints finite and strictly increasing, `on_bad_demand` in
    /// `0..=2` — reporting a violation as a [`ParameterError`] naming
    /// `component` and the offending parameter: the setpoint that fails
    /// its "must exceed its predecessor" constraint.
    pub(crate) fn checked(component: &str, table: Self) -> Result<Self, ParameterError> {
        for (parameter, value) in [
            ("cutoff", table.cutoff),
            ("stop", table.stop),
            ("start", table.start),
            ("lag_start", table.lag_start),
            ("high", table.high),
        ] {
            if !value.is_finite() {
                return Err(params::invalid(
                    component,
                    parameter,
                    "must be finite".to_string(),
                ));
            }
        }
        Self::check_order(component, &table)?;
        if !(0..=2).contains(&table.on_bad_demand) {
            return Err(params::invalid(
                component,
                "on_bad_demand",
                "must be 0, 1, or 2".to_string(),
            ));
        }
        Ok(table)
    }

    /// The ordering half of [`checked`](Self::checked): each setpoint
    /// must strictly exceed its predecessor, and the reported parameter
    /// is the one that failed that constraint — `stop` when
    /// `stop <= cutoff`, and so on up the chain.
    fn check_order(component: &str, table: &Self) -> Result<(), ParameterError> {
        for (lower_name, lower, upper_name, upper) in [
            ("cutoff", table.cutoff, "stop", table.stop),
            ("stop", table.stop, "start", table.start),
            ("start", table.start, "lag_start", table.lag_start),
            ("lag_start", table.lag_start, "high", table.high),
        ] {
            if lower >= upper {
                return Err(params::invalid(
                    component,
                    upper_name,
                    format!("must exceed {lower_name}"),
                ));
            }
        }
        Ok(())
    }
}

/// Advances the held demand one scan under a `Good` level — the
/// documented chain rules; `held` is already `0..=2`.
fn advance(held: i64, level: f64, table: &SetpointTable) -> i64 {
    if level <= table.stop {
        // Pumped down — at or below `stop` every call releases, and at
        // or below the `cutoff` floor the same rule holds the station
        // off regardless of where the level came from.
        return 0;
    }
    match held {
        2 if level <= table.start => 1,
        _ if level >= table.lag_start => 2,
        0 if level >= table.start => 1,
        held => held,
    }
}

impl ThresholdChain {
    /// The component-kind string the model-driven registry maps onto
    /// this type's constructor.
    pub const KIND: &'static str = "threshold-chain";

    /// Builds the component from explicit points and the declared
    /// setpoint table, or reports an inconsistent one as a
    /// [`ParameterError`].
    pub fn new(
        name: impl Into<String>,
        level: PointId,
        outputs: ThresholdOutputs,
        table: SetpointTable,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        Ok(Self {
            table: SetpointTable::checked(&name, table)?,
            name,
            level,
            outputs,
            demand: 0,
        })
    }

    /// The declared setpoint table — the reported parameter set.
    pub fn table(&self) -> SetpointTable {
        self.table
    }

    /// The held stage demand — `0`, `1`, or `2`.
    pub fn held_demand(&self) -> i64 {
        self.demand
    }

    /// Builds the component from a plant-model parameter map, reading
    /// the parameters listed on the type's docs.
    pub fn from_parameters(
        name: impl Into<String>,
        level: PointId,
        outputs: ThresholdOutputs,
        parameters: &Parameters,
    ) -> Result<Self, ParameterError> {
        let name = name.into();
        let table = SetpointTable {
            cutoff: params::required_f64(&name, parameters, "cutoff")?,
            stop: params::required_f64(&name, parameters, "stop")?,
            start: params::required_f64(&name, parameters, "start")?,
            lag_start: params::required_f64(&name, parameters, "lag_start")?,
            high: params::required_f64(&name, parameters, "high")?,
            on_bad_demand: params::required_u64(&name, parameters, "on_bad_demand")? as i64,
        };
        Self::new(name, level, outputs, table)
    }

    /// Retunes one declared parameter, returning the updated table or a
    /// [`CommandError`] naming `component`. The ordering invariant is
    /// re-checked after a setpoint change; a refused value changes
    /// nothing.
    fn tune(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        let mut table = self.table;
        match parameter {
            "cutoff" | "stop" | "start" | "lag_start" | "high" => {
                let tuned = params::tune_f64(&self.name, parameter, value)?;
                if !tuned.is_finite() {
                    return Err(params::invalid_parameter(
                        &self.name,
                        parameter,
                        "must be finite",
                    ));
                }
                match parameter {
                    "cutoff" => table.cutoff = tuned,
                    "stop" => table.stop = tuned,
                    "start" => table.start = tuned,
                    "lag_start" => table.lag_start = tuned,
                    _ => table.high = tuned,
                }
                SetpointTable::check_order(&self.name, &table).map_err(|_| {
                    params::invalid_parameter(
                        &self.name,
                        parameter,
                        "setpoints must stay strictly increasing: \
                         cutoff < stop < start < lag_start < high",
                    )
                })?;
            }
            "on_bad_demand" => {
                let tuned = params::tune_u64(&self.name, parameter, value)?;
                if tuned > 2 {
                    return Err(params::invalid_parameter(
                        &self.name,
                        parameter,
                        "must be 0, 1, or 2",
                    ));
                }
                table.on_bad_demand = tuned as i64;
            }
            _ => return Err(params::unknown_parameter(&self.name, parameter)),
        }
        self.table = table;
        Ok(())
    }
}

impl Component for ThresholdChain {
    fn name(&self) -> &str {
        &self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        vec![
            IoRequirement::input::<f64>("level", self.level),
            IoRequirement::output::<i64>("demand", self.outputs.demand),
            IoRequirement::output::<bool>("duty_call", self.outputs.duty_call),
            IoRequirement::output::<bool>("lag_call", self.outputs.lag_call),
            IoRequirement::output::<bool>("below_cutoff", self.outputs.below_cutoff),
            IoRequirement::output::<bool>("high_level", self.outputs.high_level),
        ]
    }

    fn step(&mut self, io: &dyn ComponentIo, tick: Tick) -> Result<(), StepError> {
        let level = io.read_typed::<f64>(self.level)?;
        if level.quality.is_good() {
            self.demand = advance(self.demand, level.value, &self.table);
        } else {
            // The declared answer to an untrusted measurement: emit the
            // fallback stage count and let the held state follow it.
            self.demand = self.table.on_bad_demand;
        }
        let trusted = level.quality.is_good();
        let writes = [
            (self.outputs.demand, Value::Int(self.demand)),
            (self.outputs.duty_call, Value::Bool(self.demand >= 1)),
            (self.outputs.lag_call, Value::Bool(self.demand >= 2)),
            (
                self.outputs.below_cutoff,
                Value::Bool(trusted && level.value <= self.table.cutoff),
            ),
            (
                self.outputs.high_level,
                Value::Bool(trusted && level.value >= self.table.high),
            ),
        ];
        for (point, value) in writes {
            io.write_sample(point, Sample::new(value, Quality::Good, tick))?;
        }
        Ok(())
    }

    /// Describes the chain: `level` is the measured value it acts on,
    /// `demand` the driven stage request, the four `Bool` ports the
    /// reported call and threshold conditions, and the six declared
    /// parameters with their ranges.
    fn describe(&self) -> ComponentDescriptor {
        describe::component(
            &self.name,
            Self::KIND,
            &self.io_requirements(),
            &[
                ("level", PortRole::ProcessValue),
                ("demand", PortRole::Output),
                ("duty_call", PortRole::Status),
                ("lag_call", PortRole::Status),
                ("below_cutoff", PortRole::Status),
                ("high_level", PortRole::Status),
            ],
            vec![
                describe::parameter("cutoff", ValueKind::Float, Some(describe::FINITE_F64)),
                describe::parameter("stop", ValueKind::Float, Some(describe::FINITE_F64)),
                describe::parameter("start", ValueKind::Float, Some(describe::FINITE_F64)),
                describe::parameter("lag_start", ValueKind::Float, Some(describe::FINITE_F64)),
                describe::parameter("high", ValueKind::Float, Some(describe::FINITE_F64)),
                describe::parameter("on_bad_demand", ValueKind::Int, Some(DEMAND_RANGE)),
            ],
        )
    }

    /// Tunes the declared parameters at the scan boundary — the
    /// recorded mechanism for operator-adjustable setpoints. A setpoint
    /// retune that would break the ordering is refused naming the
    /// tuned parameter; `on_bad_demand` accepts `0`, `1`, or `2`.
    fn apply_parameter(&mut self, parameter: &str, value: Value) -> Result<(), CommandError> {
        self.tune(parameter, value)
    }

    /// Reports the tuned table — the same fields
    /// [`capture_state`](Self::capture_state) checkpoints, so the
    /// faceplate and a tracking standby read one vocabulary.
    fn report_parameters(&self) -> StateMap {
        let mut parameters = StateMap::new();
        parameters.insert("cutoff", Value::Float(self.table.cutoff));
        parameters.insert("stop", Value::Float(self.table.stop));
        parameters.insert("start", Value::Float(self.table.start));
        parameters.insert("lag_start", Value::Float(self.table.lag_start));
        parameters.insert("high", Value::Float(self.table.high));
        parameters.insert("on_bad_demand", Value::Int(self.table.on_bad_demand));
        parameters
    }

    /// Captures the held demand and the tuned table — a checkpointed
    /// standby resumes the pump-down cycle from the same stage.
    fn capture_state(&self) -> StateMap {
        let mut state = self.report_parameters();
        state.insert("demand", Value::Int(self.demand));
        state
    }

    fn restore_state(&mut self, state: &StateMap) -> Result<(), StateError> {
        state.ensure_known_fields(
            &self.name,
            &[
                "cutoff",
                "stop",
                "start",
                "lag_start",
                "high",
                "on_bad_demand",
                "demand",
            ],
        )?;

        // The whole map is validated before any state mutates, so a
        // rejected checkpoint leaves the component on its last-good
        // state rather than half-applied.
        let table = SetpointTable {
            cutoff: state.require_f64(&self.name, "cutoff")?,
            stop: state.require_f64(&self.name, "stop")?,
            start: state.require_f64(&self.name, "start")?,
            lag_start: state.require_f64(&self.name, "lag_start")?,
            high: state.require_f64(&self.name, "high")?,
            on_bad_demand: state.require_i64(&self.name, "on_bad_demand")?,
        };
        let demand = state.require_i64(&self.name, "demand")?;

        for (field, value) in [
            ("cutoff", table.cutoff),
            ("stop", table.stop),
            ("start", table.start),
            ("lag_start", table.lag_start),
            ("high", table.high),
        ] {
            if !value.is_finite() {
                return Err(StateError::InvalidValue {
                    element: self.name.clone(),
                    field: field.to_string(),
                    value: Value::Float(value),
                });
            }
        }
        if let Err(ParameterError::Invalid { parameter, .. }) =
            SetpointTable::check_order(&self.name, &table)
        {
            let value = match parameter.as_str() {
                "stop" => table.stop,
                "start" => table.start,
                "lag_start" => table.lag_start,
                _ => table.high,
            };
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: parameter,
                value: Value::Float(value),
            });
        }
        if !(0..=2).contains(&table.on_bad_demand) {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "on_bad_demand".to_string(),
                value: Value::Int(table.on_bad_demand),
            });
        }
        if !(0..=2).contains(&demand) {
            return Err(StateError::InvalidValue {
                element: self.name.clone(),
                field: "demand".to_string(),
                value: Value::Int(demand),
            });
        }

        self.table = table;
        self.demand = demand;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::TestIo;
    use dcs_core::{Direction, PortDescriptor, QualityReason};

    const LEVEL: PointId = PointId(10);
    const DEMAND: PointId = PointId(20);
    const DUTY: PointId = PointId(21);
    const LAG: PointId = PointId(22);
    const CUTOFF: PointId = PointId(23);
    const HIGH: PointId = PointId(24);

    fn table() -> SetpointTable {
        SetpointTable {
            cutoff: 1.0,
            stop: 2.0,
            start: 4.0,
            lag_start: 6.0,
            high: 8.0,
            on_bad_demand: 0,
        }
    }

    fn outputs() -> ThresholdOutputs {
        ThresholdOutputs {
            demand: DEMAND,
            duty_call: DUTY,
            lag_call: LAG,
            below_cutoff: CUTOFF,
            high_level: HIGH,
        }
    }

    fn component() -> ThresholdChain {
        ThresholdChain::new("lch", LEVEL, outputs(), table()).unwrap()
    }

    fn io(level: f64) -> TestIo {
        TestIo::new(&[
            (
                LEVEL,
                Direction::In,
                Sample::good(Value::Float(level), Tick::ZERO),
            ),
            (
                DEMAND,
                Direction::Out,
                Sample::good(Value::Int(0), Tick::ZERO),
            ),
            (
                DUTY,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
            (
                LAG,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
            (
                CUTOFF,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
            (
                HIGH,
                Direction::Out,
                Sample::good(Value::Bool(false), Tick::ZERO),
            ),
        ])
    }

    fn feed(io: &TestIo, value: f64) {
        io.feed(LEVEL, Sample::good(Value::Float(value), Tick::ZERO));
    }

    fn demand(io: &TestIo) -> i64 {
        match io.written(DEMAND).unwrap().value {
            Value::Int(demand) => demand,
            value => panic!("demand must be Int, got {value:?}"),
        }
    }

    fn flag(io: &TestIo, point: PointId) -> bool {
        match io.written(point).unwrap().value {
            Value::Bool(flag) => flag,
            value => panic!("expected Bool, got {value:?}"),
        }
    }

    /// Steps the component once at `level` and returns the emitted
    /// demand — the scripted sweep's unit step.
    fn step_at(block: &mut ThresholdChain, io: &TestIo, level: f64, tick: u64) -> i64 {
        feed(io, level);
        block.step(io, Tick(tick)).unwrap();
        demand(io)
    }

    #[test]
    fn the_chain_walks_the_sweep_in_order_with_hysteresis() {
        let mut block = component();
        let io = io(0.5);

        // Below cutoff: nothing runs and the dry-run flag asserts.
        assert_eq!(step_at(&mut block, &io, 0.5, 1), 0);
        assert!(flag(&io, CUTOFF));
        assert!(!flag(&io, DUTY));

        // Inside the stop/start band the station stays stopped; the
        // flag cleared the scan the level receded above the cutoff.
        assert_eq!(step_at(&mut block, &io, 3.0, 2), 0);
        assert!(!flag(&io, CUTOFF));

        // At `start` the duty call asserts; just below it the station
        // would still hold off.
        assert_eq!(step_at(&mut block, &io, 4.0, 3), 1);
        assert!(flag(&io, DUTY));
        assert!(!flag(&io, LAG));

        // Mid-band holds; at `lag_start` the lag joins.
        assert_eq!(step_at(&mut block, &io, 5.9, 4), 1);
        assert_eq!(step_at(&mut block, &io, 6.0, 5), 2);
        assert!(flag(&io, LAG));

        // At `high` the alarm condition asserts while the calls hold.
        assert_eq!(step_at(&mut block, &io, 8.0, 6), 2);
        assert!(flag(&io, HIGH));

        // Falling: the lag's own stop is `start` — it releases first at
        // 4.0 while the duty keeps its hysteresis down to `stop`.
        assert_eq!(step_at(&mut block, &io, 7.9, 7), 2);
        assert!(!flag(&io, HIGH));
        assert_eq!(step_at(&mut block, &io, 4.5, 8), 2);
        assert_eq!(step_at(&mut block, &io, 4.0, 9), 1);
        assert!(!flag(&io, LAG));
        assert!(flag(&io, DUTY));
        assert_eq!(step_at(&mut block, &io, 2.1, 10), 1);
        assert_eq!(step_at(&mut block, &io, 2.0, 11), 0);
        assert!(!flag(&io, DUTY));
    }

    #[test]
    fn a_fast_fill_calls_the_lag_directly() {
        let mut block = component();
        let io = io(0.5);
        assert_eq!(step_at(&mut block, &io, 7.0, 1), 2);
        assert!(flag(&io, DUTY));
        assert!(flag(&io, LAG));
    }

    #[test]
    fn a_plunge_below_stop_releases_everything() {
        let mut block = component();
        let io = io(0.5);
        assert_eq!(step_at(&mut block, &io, 7.0, 1), 2);
        // From demand 2 straight to the cutoff floor: both calls drop
        // the same scan rather than stepping down through the lag
        // release.
        assert_eq!(step_at(&mut block, &io, 0.5, 2), 0);
        assert!(flag(&io, CUTOFF));
        assert!(!flag(&io, DUTY));
    }

    #[test]
    fn the_cutoff_overrides_any_held_demand() {
        let mut block = component();
        let io = io(0.5);
        assert_eq!(step_at(&mut block, &io, 5.0, 1), 1);
        assert_eq!(step_at(&mut block, &io, 1.0, 2), 0);
        assert!(flag(&io, CUTOFF));
    }

    #[test]
    fn a_non_good_level_emits_the_declared_fallback() {
        let mut block = component();
        let io = io(0.5);

        // Demand 1 holding mid-band; the level goes Bad.
        assert_eq!(step_at(&mut block, &io, 5.0, 1), 1);
        io.feed(
            LEVEL,
            Sample::new(
                Value::Float(5.0),
                Quality::Bad(QualityReason::CommunicationFault),
                Tick(2),
            ),
        );
        block.step(&io, Tick(2)).unwrap();
        assert_eq!(demand(&io), 0);
        assert!(!flag(&io, DUTY));
        assert!(!flag(&io, CUTOFF));
        assert!(!flag(&io, HIGH));

        // Uncertain is untrusted too — the same fallback applies.
        io.feed(
            LEVEL,
            Sample::new(
                Value::Float(9.0),
                Quality::Uncertain(QualityReason::Stale),
                Tick(3),
            ),
        );
        block.step(&io, Tick(3)).unwrap();
        assert_eq!(demand(&io), 0);
    }

    #[test]
    fn recovery_resumes_from_the_fallback_stage() {
        // on_bad_demand = 1: keep the duty called on a failed
        // measurement; recovery re-evaluates from that stage.
        let mut block = ThresholdChain::new(
            "lch",
            LEVEL,
            outputs(),
            SetpointTable {
                on_bad_demand: 1,
                ..table()
            },
        )
        .unwrap();
        let io = io(0.5);
        io.feed(
            LEVEL,
            Sample::new(
                Value::Float(0.5),
                Quality::Bad(QualityReason::DeviceFault),
                Tick(1),
            ),
        );
        block.step(&io, Tick(1)).unwrap();
        assert_eq!(demand(&io), 1);
        assert!(flag(&io, DUTY));

        // The level returns mid-band: the held duty stage persists
        // until the level falls to `stop` — the fallback became the
        // chain's state, not a side channel.
        assert_eq!(step_at(&mut block, &io, 3.0, 2), 1);
        assert_eq!(step_at(&mut block, &io, 1.5, 3), 0);
    }

    #[test]
    fn a_nan_level_holds_the_demand() {
        let mut block = component();
        let io = io(0.5);
        assert_eq!(step_at(&mut block, &io, 5.0, 1), 1);
        feed(&io, f64::NAN);
        block.step(&io, Tick(2)).unwrap();
        assert_eq!(demand(&io), 1);
        assert!(!flag(&io, CUTOFF));
        assert!(!flag(&io, HIGH));
    }

    #[test]
    fn outputs_always_report_good_quality() {
        let mut block = component();
        let io = io(0.5);
        block.step(&io, Tick(1)).unwrap();
        for point in [DEMAND, DUTY, LAG, CUTOFF, HIGH] {
            assert_eq!(io.written(point).unwrap().quality, Quality::Good);
        }
        io.feed(
            LEVEL,
            Sample::new(
                Value::Float(5.0),
                Quality::Bad(QualityReason::DeviceFault),
                Tick(2),
            ),
        );
        block.step(&io, Tick(2)).unwrap();
        for point in [DEMAND, DUTY, LAG, CUTOFF, HIGH] {
            assert_eq!(io.written(point).unwrap().quality, Quality::Good);
        }
    }

    #[test]
    fn identical_input_sequences_produce_identical_outputs() {
        let run = || {
            let mut block = component();
            let io = io(0.5);
            let mut trace = Vec::new();
            for (tick, level) in [0.5, 3.0, 4.5, 6.5, 8.5, 6.5, 4.0, 2.5, 1.5, 0.5]
                .into_iter()
                .enumerate()
            {
                feed(&io, level);
                block.step(&io, Tick(tick as u64 + 1)).unwrap();
                trace.push(io.written(DEMAND).unwrap());
            }
            trace
        };
        assert_eq!(run(), run());
    }

    #[test]
    fn setpoint_tuning_applies_and_ordering_violations_are_refused() {
        let mut block = component();
        let io = io(0.5);

        // A retuned `start` moves the duty-call threshold.
        block.apply_parameter("start", Value::Float(5.0)).unwrap();
        assert_eq!(step_at(&mut block, &io, 4.5, 1), 0);
        assert_eq!(step_at(&mut block, &io, 5.0, 2), 1);

        // Raising `start` to `lag_start` would break the ordering —
        // refused, and the table is unchanged.
        let error = block
            .apply_parameter("start", Value::Float(6.0))
            .unwrap_err();
        assert_eq!(
            error,
            CommandError::InvalidParameter {
                component: "lch".to_string(),
                parameter: "start".to_string(),
                detail: "setpoints must stay strictly increasing: \
                         cutoff < stop < start < lag_start < high"
                    .to_string(),
            }
        );
        assert_eq!(block.table().start, 5.0);

        // Non-finite and mistyped setpoints are refused; an undeclared
        // name is unknown.
        assert!(matches!(
            block.apply_parameter("stop", Value::Float(f64::INFINITY)),
            Err(CommandError::InvalidParameter { .. })
        ));
        assert_eq!(
            block.apply_parameter("stop", Value::Int(3)),
            Err(CommandError::ParameterTypeMismatch {
                component: "lch".to_string(),
                parameter: "stop".to_string(),
                expected: ValueKind::Float,
                found: Value::Int(3),
            })
        );
        assert_eq!(
            block.apply_parameter("sept", Value::Float(3.0)),
            Err(CommandError::UnknownParameter {
                component: "lch".to_string(),
                parameter: "sept".to_string(),
            })
        );

        // `on_bad_demand` tunes within its declared stage vocabulary.
        block
            .apply_parameter("on_bad_demand", Value::Int(2))
            .unwrap();
        assert_eq!(block.table().on_bad_demand, 2);
        assert!(matches!(
            block.apply_parameter("on_bad_demand", Value::Int(3)),
            Err(CommandError::InvalidParameter { .. })
        ));
    }

    #[test]
    fn malformed_tables_fail_construction_naming_the_parameter() {
        // Each setpoint must strictly exceed its predecessor; the named
        // parameter is the one that failed that constraint.
        let cases: &[(&str, SetpointTable)] = &[
            (
                "stop",
                SetpointTable {
                    stop: 1.0,
                    ..table()
                },
            ),
            (
                "start",
                SetpointTable {
                    start: 2.0,
                    ..table()
                },
            ),
            (
                "lag_start",
                SetpointTable {
                    lag_start: 4.0,
                    ..table()
                },
            ),
            (
                "high",
                SetpointTable {
                    high: 6.0,
                    ..table()
                },
            ),
            (
                "cutoff",
                SetpointTable {
                    cutoff: f64::NAN,
                    ..table()
                },
            ),
            (
                "on_bad_demand",
                SetpointTable {
                    on_bad_demand: 3,
                    ..table()
                },
            ),
        ];
        for (parameter, table) in cases {
            match ThresholdChain::new("lch", LEVEL, outputs(), *table) {
                Err(ParameterError::Invalid {
                    parameter: found, ..
                }) => assert_eq!(found, *parameter),
                other => panic!("{parameter}: expected Invalid, got {other:?}"),
            }
        }

        // Through the parameter map: a missing key is Missing, a
        // mistyped one Invalid.
        let mut parameters = Parameters::new();
        for (name, value) in [
            ("cutoff", 1.0),
            ("stop", 2.0),
            ("start", 4.0),
            ("lag_start", 6.0),
            ("high", 8.0),
        ] {
            parameters.insert(name.to_string(), Value::Float(value));
        }
        match ThresholdChain::from_parameters("lch", LEVEL, outputs(), &parameters) {
            Err(ParameterError::Missing { parameter, .. }) => {
                assert_eq!(parameter, "on_bad_demand")
            }
            other => panic!("expected Missing on_bad_demand, got {other:?}"),
        }
        parameters.insert("on_bad_demand".to_string(), Value::Int(0));
        ThresholdChain::from_parameters("lch", LEVEL, outputs(), &parameters).unwrap();
        parameters.insert("high".to_string(), Value::Float(5.0));
        match ThresholdChain::from_parameters("lch", LEVEL, outputs(), &parameters) {
            Err(ParameterError::Invalid { parameter, .. }) => assert_eq!(parameter, "high"),
            other => panic!("expected Invalid high, got {other:?}"),
        }
    }

    #[test]
    fn capture_restore_continues_the_sequence_identically() {
        let mut block = component();
        let io_a = io(0.5);
        let io_b = io(0.5);
        for (tick, level) in [(1, 0.5), (2, 5.0), (3, 6.5)] {
            feed(&io_a, level);
            block.step(&io_a, Tick(tick)).unwrap();
        }
        assert_eq!(block.held_demand(), 2);
        block.apply_parameter("start", Value::Float(4.5)).unwrap();
        let state = block.capture_state();

        let mut restored = component();
        restored.restore_state(&state).unwrap();
        assert_eq!(restored.held_demand(), 2);
        assert_eq!(restored.table(), block.table());
        assert_eq!(restored.capture_state(), state);

        // Both continue the sweep identically — the held demand and the
        // tuned table both carried.
        let mut restored = component();
        restored.restore_state(&state).unwrap();
        for (tick, level) in [(4, 4.5), (5, 4.4), (6, 2.1), (7, 1.9)] {
            feed(&io_a, level);
            block.step(&io_a, Tick(tick)).unwrap();
            feed(&io_b, level);
            restored.step(&io_b, Tick(tick)).unwrap();
            for point in [DEMAND, DUTY, LAG, CUTOFF, HIGH] {
                assert_eq!(io_a.written(point), io_b.written(point));
            }
        }
    }

    #[test]
    fn restore_rejects_an_incompatible_state_map() {
        let mut block = component();
        // Unknown field.
        let mut foreign = block.capture_state();
        foreign.insert("surprise", Value::Int(1));
        assert!(matches!(
            block.restore_state(&foreign),
            Err(StateError::UnknownField { .. })
        ));

        // Missing field.
        assert!(matches!(
            block.restore_state(&StateMap::new()),
            Err(StateError::MissingField { .. })
        ));

        // Out-of-domain demand.
        let mut bad = block.capture_state();
        bad.insert("demand", Value::Int(7));
        assert_eq!(
            block.restore_state(&bad),
            Err(StateError::InvalidValue {
                element: "lch".to_string(),
                field: "demand".to_string(),
                value: Value::Int(7),
            })
        );

        // An unordered table fails the same invariant construction
        // does, naming the field that broke it.
        let mut bad = block.capture_state();
        bad.insert("start", Value::Float(1.5));
        assert_eq!(
            block.restore_state(&bad),
            Err(StateError::InvalidValue {
                element: "lch".to_string(),
                field: "start".to_string(),
                value: Value::Float(1.5),
            })
        );
    }

    #[test]
    fn describes_itself() {
        let descriptor = component().describe();
        assert_eq!(descriptor.name, "lch");
        assert_eq!(descriptor.kind, ThresholdChain::KIND);
        assert_eq!(descriptor.label, "lch");
        assert_eq!(
            descriptor.ports,
            [
                PortDescriptor {
                    name: "level".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: Some(PortRole::ProcessValue),
                    point: None,
                },
                PortDescriptor {
                    name: "demand".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Int,
                    role: Some(PortRole::Output),
                    point: None,
                },
                PortDescriptor {
                    name: "duty_call".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Status),
                    point: None,
                },
                PortDescriptor {
                    name: "lag_call".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Status),
                    point: None,
                },
                PortDescriptor {
                    name: "below_cutoff".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Status),
                    point: None,
                },
                PortDescriptor {
                    name: "high_level".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Status),
                    point: None,
                },
            ]
        );
        // The declared parameter set: five setpoints plus the fallback.
        let names: Vec<&str> = descriptor
            .parameters
            .iter()
            .map(|parameter| parameter.name.as_str())
            .collect();
        assert_eq!(
            names,
            [
                "cutoff",
                "stop",
                "start",
                "lag_start",
                "high",
                "on_bad_demand"
            ]
        );
        assert_eq!(descriptor.parameters[5].range, Some(DEMAND_RANGE));
    }
}
