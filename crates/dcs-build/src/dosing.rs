//! The reference chemical dosing skid — the issue-#258 composition at
//! the builder seam decisions 50–55 fix.
//!
//! [`dosing_skid`] composes the skid entirely through typed spec
//! handles and emits the versioned [`PlantModel`] document; the
//! checked-in copy lives at `crates/dcs-demo/fixtures/dosing_skid.json`
//! beside its dynamics document `dosing_skid_dynamics.json` — the same
//! recorded fixture convention the station uses — and
//! `dcs-build/tests/dosing_skid.rs` asserts the emitted document equals
//! that artifact.
//!
//! ## The composition
//!
//! - **Ratio demand (decision 50):** `flow-paced-ratio` paces the dose.
//!   `flow` binds the measured process flow, `dose` binds a writable
//!   internal `In` point so operator dose writes ride the journaled
//!   receipted path and the held value crosses checkpoints, `demand`
//!   feeds the actuation path, and `clamped`/`fallback_active` surface
//!   as Status-role points. The reference records its `trim` choice as
//!   *unwired* — the spec declares no `trim` port, so the kind paces
//!   untrimmed (unity); an analyzer trim lands on the same port where a
//!   plant declares one. `on_bad_flow` is declared `2` (drive
//!   `fallback_rate`): the fallback demand stands at the ratio's output
//!   marked with the untrusted quality, and the decision-51 interlock
//!   still refuses it — the untrusted-versus-unproven separation made
//!   visible. `on_bad_trim` declares `0` (pace untrimmed).
//! - **Permissive chain (decision 51):** declared wiring, not kind
//!   inputs. `digital-input` inversions and a `bool-gate` `and` fold
//!   the healthy legs — flow proven, tank above the empty inhibit, bund
//!   clear, no external inhibit, at least one pump available — into
//!   `dosing-permitted` on the `interlock`'s `permissive` input, while
//!   the individual inhibits land on `trip_N` in the decision's order:
//!   `trip_1` flow not proven, `trip_2` tank at or below the empty
//!   inhibit, `trip_3` bund flood, `trip_4` external inhibit, `trip_5`
//!   no pump available (`pump-group.none_available`). Loss of a
//!   declared permissive drops `demand` to `safe_value` and restart
//!   resumes the first scan every condition holds again —
//!   `interlock`'s non-latching semantics; no `sr-latch` is declared.
//! - **Actuation (decision 52):** per pump, `motor` (run command out,
//!   run feedback in, computed `fault`) beside `analog-output` carrying
//!   the interlocked demand. Because each metering pump takes its own
//!   speed input, the shared demand passes a per-pump `interlock` whose
//!   permissive is that pump's proven run contact — each pump's field
//!   speed point carries the demand only while its run feedback proves
//!   it running, which is what lets the metered discharge rate stay
//!   honest about a no-delivery fault. Stroke length is absent — an
//!   engineering setting the contract records. `manual-station` sits on
//!   the demand path for operator takeover beside the equipment's
//!   local-selected field indication, and the reference declares a
//!   duty/standby pair through `pump-group` on the two motors, the
//!   fault and availability signals landing on `fault_i`/`avail_i`
//!   exactly as the station records.
//! - **Accounting (decision 53):** `totalizer` integrates the commanded
//!   `demand`; a `counter` accumulates each pump's stroke-pulse field
//!   contact where declared; `deviation-monitor` compares the commanded
//!   rate against the measured discharge the dynamics document
//!   produces, so the dose-not-confirmed path is exercisable rather
//!   than only commanded totalization.
//! - **Alarm set (decision 55):** the owner list lands as wired
//!   `latching-alarm`/`bool-latching-alarm` instances — per-pump motor
//!   fault and pump fault/no-discharge, tank low and tank empty, loss
//!   of the pacing signal (`fallback_active`), bund flood, external
//!   inhibit, and dose not confirmed (`deviating`) — each with its
//!   model-wired writable internal `ack` point. `clamped` is exposed as
//!   a Status-role flag only: alarm-versus-flag is the recorded open
//!   assumption, and the wiring supports either answer.
//!
//! The tank-empty condition reaches the permissive trip as the
//! *latched* `empty` alarm's `alarm` output — the standing two-stage
//! level condition feeding the stop, per decision 55 — so the trip and
//! the alarm always agree; its healthy side reaches the permissive
//! gate through an inversion of the same flag.
//!
//! ## The declared point-id scheme
//!
//! Field points occupy fixed blocks the checked-in dynamics document is
//! written against: `10` flow, `11` flow source, `12` tank level, `13`
//! discharge rate, `14` net draw, `15` tank refill, `20`/`21`/`22` the
//! skid digital inputs, `30+i`/`34+i` the per-pump draw and metered
//! rate, `40+i` run, `50+i` local, `60+i` pump fault, `70+i` stroke,
//! `100+i` command, `110+i` speed (`i` the 0-based pump index,
//! `pumps <= 10`). Internal carriers start at `200`, per-pump internal
//! blocks at `300 + 32·i`, skid-alarm points at `1000 + 10·a`, and
//! every point's signal sits at `10000 + point`. The scheme is
//! deterministic in declaration order, so identical builder invocations
//! emit identical documents.
//!
//! Bool state signals and the index-valued `duty`/`staged` declare an
//! empty unit — a deliberate "unitless" marker rather than an omitted
//! one, so the document lints clean.

use crate::specs::{
    AnalogOutputSpec, BoolGateSpec, BoolLatchingAlarmInstance, BoolLatchingAlarmSpec, CounterSpec,
    DeviationMonitorSpec, DigitalInputSpec, FlowPacedRatioSpec, InterlockSpec,
    LatchingAlarmInstance, LatchingAlarmSpec, ManualStationSpec, MotorSpec, PumpGroupInstance,
    PumpGroupSpec, ThresholdChainSpec, TotalizerSpec,
};
use crate::station::{AlarmLayout, rationalization};
use crate::{
    BuildError, ChannelRef, Direction, OutPoint, PlantBuilder, PointId, SignalId, Sink, Source,
    Value, parameters,
};
use dcs_model::{ComponentId, PlantModel};

/// Field point ids — the fixed block the dynamics document addresses.
pub mod points {
    use crate::PointId;

    /// The measured process flow (`Float`, `In`) — the dynamics
    /// document's `flow_sum` over the declared source.
    pub const FLOW: PointId = PointId(10);
    /// The declared process-flow source (`Float`, `In`) — the
    /// flowmeter feed the dynamics document supplies.
    pub const FLOW_SOURCE: PointId = PointId(11);
    /// Chemical tank level (`Float`, `In`) — the integrator's output.
    pub const TANK_LEVEL: PointId = PointId(12);
    /// Measured chemical discharge rate (`Float`, `In`) — the
    /// `flow_sum` over the per-pump metered rates; the
    /// measured-consumption signal.
    pub const DISCHARGE_RATE: PointId = PointId(13);
    /// Net tank drawdown rate (`Float`, `In`) — the `flow_sum` of the
    /// pump draws plus the refill line; the integrator's input.
    pub const NET_DRAW: PointId = PointId(14);
    /// Tank refill inflow (`Float`, `In`) — a declared field input.
    pub const TANK_REFILL: PointId = PointId(15);
    /// Flow-proven contact (`Bool`, `In`) — the process running and
    /// flow confirmed.
    pub const FLOW_PROVEN: PointId = PointId(20);
    /// Bund flood contact (`Bool`, `In`).
    pub const BUND_FLOOD: PointId = PointId(21);
    /// External (wet-weather) inhibit contact (`Bool`, `In`).
    pub const EXTERNAL_INHIBIT: PointId = PointId(22);
    /// Pump `index`'s chemical draw rate (`Float`, `In`) — the
    /// `bool_flow` element's output.
    pub fn draw(index: usize) -> PointId {
        PointId(30 + index as u64)
    }
    /// Pump `index`'s metered discharge rate (`Float`, `In`) — the
    /// `scaled_flow` element's output.
    pub fn rate(index: usize) -> PointId {
        PointId(34 + index as u64)
    }
    /// Pump `index`'s run feedback (`Bool`, `In`).
    pub fn run(index: usize) -> PointId {
        PointId(40 + index as u64)
    }
    /// Pump `index`'s local-selected indication (`Bool`, `In`).
    pub fn local(index: usize) -> PointId {
        PointId(50 + index as u64)
    }
    /// Pump `index`'s fault / no-discharge contact (`Bool`, `In`).
    pub fn pump_fault(index: usize) -> PointId {
        PointId(60 + index as u64)
    }
    /// Pump `index`'s stroke pulse (`Bool`, `In`).
    pub fn stroke(index: usize) -> PointId {
        PointId(70 + index as u64)
    }
    /// Pump `index`'s field run command (`Bool`, `Out`).
    pub fn cmd(index: usize) -> PointId {
        PointId(100 + index as u64)
    }
    /// Pump `index`'s analog speed demand (`Float`, `Out`).
    pub fn speed(index: usize) -> PointId {
        PointId(110 + index as u64)
    }
}

/// Internal carrier ids — the skid-level wiring.
mod carriers {
    /// `flow-paced-ratio.demand` — the raw paced demand.
    pub const RATIO_DEMAND: u64 = 200;
    /// `ratio-demand`'s consumer feeding `interlock.in`.
    pub const RATIO_DEMAND_IN: u64 = 201;
    /// `flow-paced-ratio.clamped` — the Status-role saturation flag.
    pub const CLAMPED: u64 = 202;
    /// `flow-paced-ratio.fallback_active` — bad-flow fallback engaged.
    pub const FALLBACK_ACTIVE: u64 = 203;
    /// `fallback-active`'s consumer feeding the pacing alarm.
    pub const FALLBACK_ACTIVE_IN: u64 = 204;
    /// The inverted flow-proven contact — the flow-not-proven trip.
    pub const FLOW_NOT_PROVEN: u64 = 205;
    /// `flow-not-proven`'s consumer feeding `interlock.trip_1`.
    pub const FLOW_NOT_PROVEN_IN: u64 = 206;
    /// The tank-empty alarm flag's consumer feeding
    /// `interlock.trip_2`.
    pub const TANK_EMPTY_TRIP_IN: u64 = 208;
    /// The tank-empty alarm flag's consumer feeding the healthy-side
    /// inversion.
    pub const TANK_EMPTY_INV_IN: u64 = 209;
    /// Inverted tank-empty — the healthy tank leg.
    pub const TANK_OK: u64 = 210;
    /// `tank-ok`'s consumer feeding the permissive gate.
    pub const TANK_OK_IN: u64 = 211;
    /// Inverted bund-flood — the bund-clear leg.
    pub const BUND_OK: u64 = 212;
    /// `bund-ok`'s consumer feeding the permissive gate.
    pub const BUND_OK_IN: u64 = 213;
    /// Inverted external inhibit.
    pub const EXTERNAL_OK: u64 = 214;
    /// `external-ok`'s consumer feeding the permissive gate.
    pub const EXTERNAL_OK_IN: u64 = 215;
    /// `pump-group.none_available` carrier.
    pub const NONE_AVAILABLE: u64 = 216;
    /// `none-available`'s consumer — `interlock.trip_5` and the
    /// availability inversion share one consumer point.
    pub const NONE_AVAILABLE_IN: u64 = 217;
    /// At least one pump available — the inverted `none_available`.
    pub const PUMP_AVAIL: u64 = 218;
    /// `pump-avail`'s consumer feeding the permissive gate.
    pub const PUMP_AVAIL_IN: u64 = 219;
    /// `bool-gate` `and` — the aggregated dosing-permitted condition.
    pub const DOSING_PERMITTED: u64 = 220;
    /// `dosing-permitted`'s consumer feeding `interlock.permissive`.
    pub const PERMITTED_IN: u64 = 221;
    /// `interlock.out` — the permissive-gated demand.
    pub const GATED_DEMAND: u64 = 222;
    /// `gated-demand`'s consumer feeding `manual-station.control`.
    pub const GATED_DEMAND_IN: u64 = 223;
    /// `interlock.tripped` — Status-role flag.
    pub const INTERLOCK_TRIPPED: u64 = 224;
    /// `manual-station.out` — the commanded chemical rate.
    pub const DEMAND: u64 = 225;
    /// `demand`'s consumer feeding `threshold-chain.level`.
    pub const DEMAND_CHAIN_IN: u64 = 226;
    /// `demand`'s consumer feeding `totalizer.rate`.
    pub const DEMAND_TOTAL_IN: u64 = 227;
    /// `demand`'s consumer feeding `deviation-monitor.expected`.
    pub const DEMAND_DEVMON_IN: u64 = 228;
    /// `manual-station.manual_active` — Status-role flag.
    pub const MANUAL_ACTIVE: u64 = 229;
    /// `threshold-chain.demand` — the stage count.
    pub const STAGE_DEMAND: u64 = 230;
    /// `stage-demand`'s consumer feeding `pump-group.demand`.
    pub const STAGE_DEMAND_IN: u64 = 231;
    /// `pump-group.duty` — the duty pump index.
    pub const DUTY: u64 = 232;
    /// `pump-group.staged` — the running pump count.
    pub const STAGED: u64 = 233;
    /// `pump-group.all_faulted` — Status-role flag.
    pub const ALL_FAULTED: u64 = 234;
    /// `threshold-chain.duty_call` — Status-role flag.
    pub const DUTY_CALL: u64 = 235;
    /// `threshold-chain.lag_call` — Status-role flag.
    pub const LAG_CALL: u64 = 236;
    /// `threshold-chain.below_cutoff` — Status-role flag.
    pub const BELOW_CUTOFF: u64 = 237;
    /// `threshold-chain.high_level` — Status-role flag.
    pub const HIGH_LEVEL: u64 = 238;
    /// The operator dose setpoint — writable internal `In`.
    pub const DOSE: u64 = 240;
    /// Manual-station mode select — writable internal `In`.
    pub const MANUAL_MODE: u64 = 241;
    /// Manual-station manual rate — writable internal `In`.
    pub const MANUAL_RATE: u64 = 242;
    /// Totalizer reset — writable internal `In`.
    pub const TOTALIZER_RESET: u64 = 243;
    /// `totalizer.total` — the commanded chemical total.
    pub const DOSE_TOTAL: u64 = 244;
    /// `deviation-monitor.deviation` — the windowed relative deviation.
    pub const DEVIATION: u64 = 245;
    /// `deviation-monitor.deviating` — the dose-not-confirmed
    /// condition.
    pub const DEVIATING: u64 = 246;
    /// `deviating`'s consumer feeding the dose-not-confirmed alarm.
    pub const DEVIATING_IN: u64 = 247;
}

/// The first per-pump internal block: pump `i` owns
/// `PUMP_BASE + i * PUMP_STRIDE .. +PUMP_STRIDE`.
const PUMP_BASE: u64 = 300;
/// Point ids each pump's internal block spans.
const PUMP_STRIDE: u64 = 32;
/// Skid-alarm points: alarm `a` owns `ALARM_BASE + a * 10 .. +10` —
/// `ack`, `alarm`, `unacknowledged` at offsets 0, 1, 2.
const ALARM_BASE: u64 = 1000;
/// Every point's signal id is `SIGNAL_BASE + point`.
const SIGNAL_BASE: u64 = 10_000;

/// `bool-gate`'s `operation` codes — `GateOperation::And`/`Or` from
/// `dcs-blocks`, mirrored as data because `dcs-build` cannot depend on
/// the blocks crate.
const GATE_AND: i64 = 0;

/// The bound a single-sided level alarm parks its unused limit at —
/// far outside any measurable span, so only the declared threshold side
/// can trip.
const PARKED_LIMIT: f64 = 1.0e9;

/// Per-pump internal point offsets — `PUMP_BASE + index·PUMP_STRIDE +
/// offset` — mirroring the station's per-pump block layout.
mod pump_point {
    /// Operator out-of-service flag — writable internal `In`.
    pub const OUT_OF_SERVICE: u64 = 0;
    /// Stroke-counter reset — writable internal `In`.
    pub const STROKE_RESET: u64 = 1;
    /// `pump-group.cmd_i` carrier.
    pub const GROUP_CMD: u64 = 2;
    /// `group-cmd`'s consumer feeding `motor.cmd`.
    pub const GROUP_CMD_IN: u64 = 3;
    /// Inverted local-selected — the remote/available leg.
    pub const LOCAL_OK: u64 = 4;
    /// `local-ok`'s consumer feeding the availability gate.
    pub const LOCAL_OK_IN: u64 = 5;
    /// Inverted out-of-service flag.
    pub const OOS_OK: u64 = 6;
    /// `oos-ok`'s consumer feeding the availability gate.
    pub const OOS_OK_IN: u64 = 7;
    /// Inverted pump fault / no-discharge contact.
    pub const PFAULT_OK: u64 = 8;
    /// `pfault-ok`'s consumer feeding the availability gate.
    pub const PFAULT_OK_IN: u64 = 9;
    /// The aggregated `avail_i` the group consumes.
    pub const AVAIL: u64 = 10;
    /// `avail`'s consumer feeding `pump-group.avail_i`.
    pub const AVAIL_IN: u64 = 11;
    /// `motor.fault` — the proven command/feedback disagreement.
    pub const MOTOR_FAULT: u64 = 12;
    /// `motor-fault`'s consumer feeding `pump-group.fault_i`.
    pub const MOTOR_FAULT_GROUP_IN: u64 = 13;
    /// `motor-fault`'s consumer feeding the motor-fault alarm.
    pub const MOTOR_FAULT_ALARM_IN: u64 = 14;
    /// `demand`'s consumer feeding this pump's speed gate.
    pub const DEMAND_IN: u64 = 15;
    /// The run-gated speed demand — the speed interlock's `out`.
    pub const SPEED_ENG: u64 = 16;
    /// `speed-eng`'s consumer feeding `analog-output.eng`.
    pub const SPEED_ENG_IN: u64 = 17;
    /// The speed interlock's `tripped` — Status-role flag.
    pub const SPEED_GATED: u64 = 18;
    /// `counter.count` — the accumulated stroke count.
    pub const STROKES: u64 = 19;
    /// `counter.done` — the preset-reached flag.
    pub const STROKES_DONE: u64 = 20;
    /// Motor-fault alarm ack — writable internal `In`.
    pub const FAULT_ACK: u64 = 21;
    /// Motor-fault alarm — the standing condition.
    pub const FAULT_ALARM: u64 = 22;
    /// Motor-fault alarm — the latched unacknowledged flag.
    pub const FAULT_UNACK: u64 = 23;
    /// Pump fault / no-discharge alarm ack — writable internal `In`.
    pub const PFAULT_ACK: u64 = 24;
    /// Pump fault / no-discharge alarm — the standing condition.
    pub const PFAULT_ALARM: u64 = 25;
    /// Pump fault / no-discharge alarm — the latched unacknowledged
    /// flag.
    pub const PFAULT_UNACK: u64 = 26;
}

/// The skid's tunable contract — dose and rate bounds, permissive and
/// fallback policy, staging policy, and alarm thresholds.
/// [`reference`](Self::reference) is the checked-in document's
/// configuration.
#[derive(Debug, Clone, PartialEq)]
pub struct DosingSkidConfig {
    /// The pump count `N`: `pump-group`'s `cmd_i`/`run_i`/`fault_i`/
    /// `avail_i` families and the per-pump wiring repeat `1..=N` times.
    /// The point-id scheme admits at most 10.
    pub pumps: usize,
    /// Engineered lower bound on the dose term (`min_dose`, mg/L).
    pub min_dose: f64,
    /// Engineered upper bound on the dose term (`max_dose`, mg/L).
    pub max_dose: f64,
    /// Lower bound on the actuator demand (`min_rate`, g/h).
    pub min_rate: f64,
    /// Upper bound on the actuator demand (`max_rate`, g/h) — also the
    /// analog-output engineering-range ceiling.
    pub max_rate: f64,
    /// The fixed demand `on_bad_flow = 2` drives (`fallback_rate`, g/h).
    pub fallback_rate: f64,
    /// The declared bad-flow response code: `0` stop, `1` hold the last
    /// `Good` demand, `2` drive `fallback_rate`.
    pub on_bad_flow: i64,
    /// The declared bad-trim response code: `0` pace untrimmed, `1`
    /// hold the last `Good` trim.
    pub on_bad_trim: i64,
    /// The `interlock` safe demand driven while the permissive fails.
    pub safe_value: f64,
    /// `manual-station`'s slew toward the control value at a manual
    /// transition (`transfer_delta`, g/h per scan).
    pub transfer_delta: f64,
    /// The `threshold-chain` duty-start level (`start`, g/h): any
    /// commanded demand at or above it calls a pump.
    pub demand_call: f64,
    /// `pump-group`'s rotation policy code — `0` alternates duty each
    /// pump-down cycle.
    pub rotation: i64,
    /// The timed-rotation interval; declared only when the policy uses
    /// it.
    pub rotation_ticks: Option<i64>,
    /// `pump-group`'s `start_delay_ticks`.
    pub start_delay_ticks: i64,
    /// `pump-group`'s `restage_delay_ticks`.
    pub restage_delay_ticks: i64,
    /// `pump-group`'s `min_off_ticks`.
    pub min_off_ticks: i64,
    /// Each `motor`'s `fault_ticks` — the command-versus-feedback
    /// disagreement budget.
    pub motor_fault_ticks: i64,
    /// The tank-low alarm level (L).
    pub tank_low: f64,
    /// The tank-empty inhibit level (L) — at or below it the permissive
    /// trips and the empty alarm stands.
    pub tank_empty: f64,
    /// The hysteresis band both level alarms share (L).
    pub tank_hysteresis: f64,
    /// `deviation-monitor`'s relative-deviation limit.
    pub deviation_limit: f64,
    /// `deviation-monitor`'s comparison window in scans.
    pub window_ticks: i64,
    /// The stroke-counter preset — effectively never reached; the
    /// running count is the datum.
    pub stroke_preset: i64,
    /// The initial operator dose setpoint (mg/L) the writable `dose`
    /// point seeds.
    pub initial_dose: f64,
}

impl DosingSkidConfig {
    /// The reference skid the checked-in documents record: a
    /// duty/standby metering pair pacing a nominal 2 mg/L dose inside
    /// the declared bounds, the declared fallback rate on an untrusted
    /// flow, and the documented tank thresholds.
    pub fn reference() -> Self {
        Self {
            pumps: 2,
            min_dose: 0.5,
            max_dose: 4.0,
            min_rate: 10.0,
            max_rate: 100.0,
            fallback_rate: 40.0,
            on_bad_flow: 2,
            on_bad_trim: 0,
            safe_value: 0.0,
            transfer_delta: 25.0,
            demand_call: 1.0,
            rotation: 0,
            rotation_ticks: None,
            start_delay_ticks: 1,
            restage_delay_ticks: 0,
            min_off_ticks: 1,
            motor_fault_ticks: 2,
            tank_low: 150.0,
            tank_empty: 60.0,
            tank_hysteresis: 10.0,
            deviation_limit: 0.2,
            window_ticks: 6,
            stroke_preset: 1_000_000,
            initial_dose: 2.0,
        }
    }
}

/// Pump `index`'s place in the emitted document.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DosingPumpLayout {
    /// The 1-based pump index — the `cmd_i`/`run_i`/`fault_i`/`avail_i`
    /// family member.
    pub index: usize,
    /// The field run-command `Out` point (`cmd` channel).
    pub cmd: PointId,
    /// The run-feedback field `In` point — looped back to `cmd`.
    pub run: PointId,
    /// The local-selected field `In` point.
    pub local: PointId,
    /// The pump fault / no-discharge field `In` point.
    pub pump_fault: PointId,
    /// The stroke-pulse field `In` point.
    pub stroke: PointId,
    /// The analog speed-demand field `Out` point.
    pub speed: PointId,
    /// The chemical-draw field `In` point a `bool_flow` drives.
    pub draw: PointId,
    /// The metered discharge-rate field `In` point a `scaled_flow`
    /// drives.
    pub rate: PointId,
    /// The writable internal out-of-service flag.
    pub out_of_service: PointId,
    /// The writable internal stroke-counter reset.
    pub stroke_reset: PointId,
    /// The carrier carrying the group's `cmd_i` request.
    pub group_cmd: PointId,
    /// The aggregated `avail_i` carrier.
    pub avail: PointId,
    /// The `motor.fault` carrier.
    pub motor_fault: PointId,
    /// The run-gated speed demand carrier — the speed interlock's
    /// `out`.
    pub speed_eng: PointId,
    /// The speed interlock's `tripped` flag carrier.
    pub speed_gated: PointId,
    /// The `counter.count` carrier — the stroke total.
    pub strokes: PointId,
    /// The `counter.done` carrier — preset reached.
    pub strokes_done: PointId,
    /// The `motor` instance's id.
    pub motor: ComponentId,
    /// The per-pump speed `interlock` instance's id.
    pub speed_gate: ComponentId,
    /// The `analog-output` instance's id.
    pub analog_output: ComponentId,
    /// The `counter` instance's id.
    pub counter: ComponentId,
    /// The motor-fault alarm.
    pub fault_alarm: AlarmLayout,
    /// The pump fault / no-discharge alarm.
    pub pump_fault_alarm: AlarmLayout,
}

/// Where everything the composition declares landed — the ids the
/// dynamics document, the scripted run, and any embedding surface
/// address.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DosingSkidLayout {
    /// The measured process flow field point.
    pub flow: PointId,
    /// The declared process-flow source field point.
    pub flow_source: PointId,
    /// The chemical tank level field point — the integrator's output.
    pub tank_level: PointId,
    /// The measured discharge rate field point — the
    /// measured-consumption signal.
    pub discharge_rate: PointId,
    /// The net tank drawdown rate field point.
    pub net_draw: PointId,
    /// The tank refill inflow field point.
    pub tank_refill: PointId,
    /// The flow-proven contact field point.
    pub flow_proven: PointId,
    /// The bund flood contact field point.
    pub bund_flood: PointId,
    /// The external inhibit contact field point.
    pub external_inhibit: PointId,
    /// The `flow-paced-ratio.demand` carrier — the raw paced demand.
    pub ratio_demand: PointId,
    /// The `interlock.out` carrier — the permissive-gated demand.
    pub gated_demand: PointId,
    /// The `manual-station.out` carrier — the commanded chemical rate.
    pub demand: PointId,
    /// The `threshold-chain.demand` carrier — the stage count.
    pub stage_demand: PointId,
    /// The `pump-group.duty` carrier.
    pub duty: PointId,
    /// The `pump-group.staged` carrier.
    pub staged: PointId,
    /// The `pump-group.none_available` carrier.
    pub none_available: PointId,
    /// The `pump-group.all_faulted` carrier.
    pub all_faulted: PointId,
    /// The `flow-paced-ratio.clamped` carrier.
    pub clamped: PointId,
    /// The `flow-paced-ratio.fallback_active` carrier.
    pub fallback_active: PointId,
    /// The aggregated dosing-permitted condition carrier.
    pub dosing_permitted: PointId,
    /// The `interlock.tripped` carrier.
    pub interlock_tripped: PointId,
    /// The `manual-station.manual_active` carrier.
    pub manual_active: PointId,
    /// The `totalizer.total` carrier — the commanded chemical total.
    pub dose_total: PointId,
    /// The `deviation-monitor.deviation` carrier.
    pub deviation: PointId,
    /// The `deviation-monitor.deviating` carrier.
    pub deviating: PointId,
    /// The writable operator dose setpoint.
    pub dose: PointId,
    /// The writable manual-station mode select.
    pub manual_mode: PointId,
    /// The writable manual-station manual rate.
    pub manual_rate: PointId,
    /// The writable totalizer reset.
    pub totalizer_reset: PointId,
    /// The `flow-paced-ratio` instance's id.
    pub ratio: ComponentId,
    /// The decision-51 demand `interlock` instance's id.
    pub interlock: ComponentId,
    /// The `manual-station` instance's id.
    pub manual_station: ComponentId,
    /// The `threshold-chain` instance's id.
    pub threshold_chain: ComponentId,
    /// The `pump-group` instance's id.
    pub pump_group: ComponentId,
    /// The `totalizer` instance's id.
    pub totalizer: ComponentId,
    /// The `deviation-monitor` instance's id.
    pub deviation_monitor: ComponentId,
    /// The tank-low latching alarm.
    pub tank_low_alarm: AlarmLayout,
    /// The tank-empty latching alarm — its `alarm` flag also feeds
    /// `interlock.trip_2` and the healthy-side inversion.
    pub tank_empty_alarm: AlarmLayout,
    /// The pacing-signal-lost alarm.
    pub pacing_alarm: AlarmLayout,
    /// The bund-flood alarm.
    pub bund_alarm: AlarmLayout,
    /// The external-inhibit alarm.
    pub external_alarm: AlarmLayout,
    /// The dose-not-confirmed alarm.
    pub deviation_alarm: AlarmLayout,
    /// Per-pump layouts, in `index` order.
    pub pumps: Vec<DosingPumpLayout>,
}

/// The composed skid: the emitted document plus the layout every
/// declared id landed on.
#[derive(Debug, Clone)]
pub struct DosingSkid {
    /// The versioned plant-model document.
    pub model: PlantModel,
    /// The id map into it.
    pub layout: DosingSkidLayout,
}

/// Composes the dosing skid under `config` and emits its
/// [`PlantModel`].
///
/// Every component registers through its typed spec and every
/// connection goes through typed handles — port existence, direction,
/// and value kind are compile-time-checked where the types reach and
/// `build`-checked where they do not.
///
/// # Panics
///
/// `config.pumps` outside `1..=10` exceeds the declared point-id
/// scheme.
pub fn dosing_skid(config: &DosingSkidConfig) -> Result<DosingSkid, BuildError> {
    assert!(
        (1..=10).contains(&config.pumps),
        "the dosing skid's point-id scheme admits 1..=10 pumps, got {}",
        config.pumps
    );
    let mut plant = PlantBuilder::new();

    // The simulated I/O devices — all under the `sim` prefix, so the
    // standard driver registry serves them from the local SimDriver:
    // analog in, digital in, digital out, analog out — the
    // terminal-strip split a real skid's I/O list records.
    let ai = plant.device("sim-ai").id;
    let di = plant.device("sim-di").id;
    let d_o = plant.device("sim-do").id;
    let ao = plant.device("sim-ao").id;

    let flow_ch = plant.channel::<f64>(ai, "flow", Direction::In);
    let flow_source_ch = plant.channel::<f64>(ai, "flow-src", Direction::In);
    let tank_level_ch = plant.channel::<f64>(ai, "tank-level", Direction::In);
    let discharge_rate_ch = plant.channel::<f64>(ai, "discharge-rate", Direction::In);
    let net_draw_ch = plant.channel::<f64>(ai, "net-draw", Direction::In);
    let tank_refill_ch = plant.channel::<f64>(ai, "tank-refill", Direction::In);
    let flow_proven_ch = plant.channel::<bool>(di, "flow-proven", Direction::In);
    let bund_flood_ch = plant.channel::<bool>(di, "bund-flood", Direction::In);
    let external_inhibit_ch = plant.channel::<bool>(di, "external-inhibit", Direction::In);
    let pump_channels: Vec<_> = (0..config.pumps)
        .map(|i| {
            let tag = pump_tag(i);
            (
                plant.channel::<f64>(ai, &format!("{tag}-draw"), Direction::In),
                plant.channel::<f64>(ai, &format!("{tag}-rate"), Direction::In),
                plant.channel::<bool>(di, &format!("{tag}-run"), Direction::In),
                plant.channel::<bool>(di, &format!("{tag}-local"), Direction::In),
                plant.channel::<bool>(di, &format!("{tag}-fault"), Direction::In),
                plant.channel::<bool>(di, &format!("{tag}-stroke"), Direction::In),
                plant.channel::<bool>(d_o, &format!("{tag}-cmd"), Direction::Out),
                plant.channel::<f64>(ao, &format!("{tag}-speed"), Direction::Out),
            )
        })
        .collect();

    // Field points — `flow`, `discharge-rate`, `net-draw`, the draws,
    // and the rates are produced by the dynamics document's elements,
    // so some handles go unused here.
    let flow = plant.field_input::<f64>(points::FLOW, flow_ch, false);
    plant.field_input::<f64>(points::FLOW_SOURCE, flow_source_ch, false);
    let tank_level = plant.field_input::<f64>(points::TANK_LEVEL, tank_level_ch, false);
    let discharge_rate = plant.field_input::<f64>(points::DISCHARGE_RATE, discharge_rate_ch, false);
    plant.field_input::<f64>(points::NET_DRAW, net_draw_ch, false);
    plant.field_input::<f64>(points::TANK_REFILL, tank_refill_ch, false);
    let flow_proven = plant.field_input::<bool>(points::FLOW_PROVEN, flow_proven_ch, false);
    let bund_flood = plant.field_input::<bool>(points::BUND_FLOOD, bund_flood_ch, false);
    let external_inhibit =
        plant.field_input::<bool>(points::EXTERNAL_INHIBIT, external_inhibit_ch, false);
    // The permissive contacts are the protection layer's reported
    // states — decision 74's durable record marks them `journaled` so
    // their transitions land in the journal beside the alarms and
    // trips they raise.
    plant.journaled(flow_proven);
    plant.journaled(bund_flood);
    plant.journaled(external_inhibit);

    signal(
        &mut plant,
        points::FLOW,
        "flow",
        "m3/h",
        "Measured process flow — the pacing signal",
        "dosing-skid",
    );
    signal(
        &mut plant,
        points::FLOW_SOURCE,
        "flow-src",
        "m3/h",
        "Process flow source — the dynamics document's forcing input",
        "dosing-skid",
    );
    signal(
        &mut plant,
        points::TANK_LEVEL,
        "tank-level",
        "L",
        "Chemical tank level",
        "dosing-skid",
    );
    signal(
        &mut plant,
        points::DISCHARGE_RATE,
        "discharge-rate",
        "g/h",
        "Measured chemical discharge rate",
        "dosing-skid",
    );
    signal(
        &mut plant,
        points::NET_DRAW,
        "net-draw",
        "L/scan",
        "Net tank drawdown rate — draws plus refill",
        "dosing-skid",
    );
    signal(
        &mut plant,
        points::TANK_REFILL,
        "tank-refill",
        "L/scan",
        "Tank refill inflow — the delivery line",
        "dosing-skid",
    );
    signal(
        &mut plant,
        points::FLOW_PROVEN,
        "flow-proven",
        "",
        "Flow-proven contact — the process running and flow confirmed",
        "dosing-skid",
    );
    signal(
        &mut plant,
        points::BUND_FLOOD,
        "bund-flood",
        "",
        "Bund flood contact",
        "dosing-skid",
    );
    signal(
        &mut plant,
        points::EXTERNAL_INHIBIT,
        "external-inhibit",
        "",
        "External (wet-weather) inhibit contact",
        "dosing-skid",
    );

    // The shared internal carriers. A carrier's `initial` is what its
    // consumers see at scan 1's input phase — the healthy-side seeds
    // read the declared cold-start state (tank healthy, bund clear, no
    // external inhibit, a pump available) while the condition-side
    // seeds read the fail-safe state (not permitted, nothing tripped
    // computed yet).
    let ratio_demand = plant.internal_output::<f64>(PointId(carriers::RATIO_DEMAND), 0.0);
    let ratio_demand_in =
        plant.internal_input::<f64>(PointId(carriers::RATIO_DEMAND_IN), 0.0, false);
    let clamped = plant.internal_output::<bool>(PointId(carriers::CLAMPED), false);
    let fallback_active = plant.internal_output::<bool>(PointId(carriers::FALLBACK_ACTIVE), false);
    let fallback_active_in =
        plant.internal_input::<bool>(PointId(carriers::FALLBACK_ACTIVE_IN), false, false);
    let flow_not_proven = plant.internal_output::<bool>(PointId(carriers::FLOW_NOT_PROVEN), false);
    let flow_not_proven_in =
        plant.internal_input::<bool>(PointId(carriers::FLOW_NOT_PROVEN_IN), false, false);
    let tank_empty_trip_in =
        plant.internal_input::<bool>(PointId(carriers::TANK_EMPTY_TRIP_IN), false, false);
    let tank_empty_inv_in =
        plant.internal_input::<bool>(PointId(carriers::TANK_EMPTY_INV_IN), false, false);
    let tank_ok = plant.internal_output::<bool>(PointId(carriers::TANK_OK), true);
    let tank_ok_in = plant.internal_input::<bool>(PointId(carriers::TANK_OK_IN), false, false);
    let bund_ok = plant.internal_output::<bool>(PointId(carriers::BUND_OK), true);
    let bund_ok_in = plant.internal_input::<bool>(PointId(carriers::BUND_OK_IN), false, false);
    let external_ok = plant.internal_output::<bool>(PointId(carriers::EXTERNAL_OK), true);
    let external_ok_in =
        plant.internal_input::<bool>(PointId(carriers::EXTERNAL_OK_IN), false, false);
    let none_available = plant.internal_output::<bool>(PointId(carriers::NONE_AVAILABLE), false);
    let none_available_in =
        plant.internal_input::<bool>(PointId(carriers::NONE_AVAILABLE_IN), false, false);
    let pump_avail = plant.internal_output::<bool>(PointId(carriers::PUMP_AVAIL), true);
    let pump_avail_in =
        plant.internal_input::<bool>(PointId(carriers::PUMP_AVAIL_IN), false, false);
    let dosing_permitted =
        plant.internal_output::<bool>(PointId(carriers::DOSING_PERMITTED), false);
    let permitted_in = plant.internal_input::<bool>(PointId(carriers::PERMITTED_IN), false, false);
    let gated_demand = plant.internal_output::<f64>(PointId(carriers::GATED_DEMAND), 0.0);
    let gated_demand_in =
        plant.internal_input::<f64>(PointId(carriers::GATED_DEMAND_IN), 0.0, false);
    let interlock_tripped =
        plant.internal_output::<bool>(PointId(carriers::INTERLOCK_TRIPPED), false);
    let demand = plant.internal_output::<f64>(PointId(carriers::DEMAND), 0.0);
    let demand_chain_in =
        plant.internal_input::<f64>(PointId(carriers::DEMAND_CHAIN_IN), 0.0, false);
    let demand_total_in =
        plant.internal_input::<f64>(PointId(carriers::DEMAND_TOTAL_IN), 0.0, false);
    let demand_devmon_in =
        plant.internal_input::<f64>(PointId(carriers::DEMAND_DEVMON_IN), 0.0, false);
    let manual_active = plant.internal_output::<bool>(PointId(carriers::MANUAL_ACTIVE), false);
    let stage_demand = plant.internal_output::<i64>(PointId(carriers::STAGE_DEMAND), 0);
    let stage_demand_in = plant.internal_input::<i64>(PointId(carriers::STAGE_DEMAND_IN), 0, false);
    let duty = plant.internal_output::<i64>(PointId(carriers::DUTY), 0);
    let staged = plant.internal_output::<i64>(PointId(carriers::STAGED), 0);
    let all_faulted = plant.internal_output::<bool>(PointId(carriers::ALL_FAULTED), false);
    let duty_call = plant.internal_output::<bool>(PointId(carriers::DUTY_CALL), false);
    let lag_call = plant.internal_output::<bool>(PointId(carriers::LAG_CALL), false);
    let below_cutoff = plant.internal_output::<bool>(PointId(carriers::BELOW_CUTOFF), false);
    let high_level = plant.internal_output::<bool>(PointId(carriers::HIGH_LEVEL), false);

    // Operator points — writable internal `In` points so dose,
    // takeover, and reset writes ride the journaled receipted path and
    // the held values cross checkpoints.
    let dose = plant.internal_input::<f64>(PointId(carriers::DOSE), config.initial_dose, true);
    let manual_mode = plant.internal_input::<bool>(PointId(carriers::MANUAL_MODE), false, true);
    // The mode select is decision 75's mode-change point — `journaled`
    // so the transition is durable beside its receipt; the float
    // setpoints can't carry the flag (`JournaledFloat`).
    plant.journaled(manual_mode);
    let manual_rate = plant.internal_input::<f64>(PointId(carriers::MANUAL_RATE), 0.0, true);
    let totalizer_reset =
        plant.internal_input::<bool>(PointId(carriers::TOTALIZER_RESET), false, true);

    let dose_total = plant.internal_output::<f64>(PointId(carriers::DOSE_TOTAL), 0.0);
    let deviation = plant.internal_output::<f64>(PointId(carriers::DEVIATION), 0.0);
    let deviating = plant.internal_output::<bool>(PointId(carriers::DEVIATING), false);
    let deviating_in = plant.internal_input::<bool>(PointId(carriers::DEVIATING_IN), false, false);

    // Decision 74's durable record: the fallback-engaged flag, the
    // group's availability roll-ups, the aggregated permissive, the
    // interlock's trip flag, the manual station's selection report
    // (decision 75's mode-change point), and the dose-not-confirmed
    // flag are the skid's protection-relevant status carriers — each
    // `journaled`. The inverted legs, delivered copies, and the
    // demand-side carriers stay off the record; the measurement and
    // demand points keep the history-ring-only path.
    for point in [
        fallback_active,
        none_available,
        dosing_permitted,
        interlock_tripped,
        manual_active,
        all_faulted,
        deviating,
    ] {
        plant.journaled(point);
    }

    for (point, name, unit, description) in [
        (
            carriers::RATIO_DEMAND,
            "ratio-demand",
            "g/h",
            "Paced demand before the permissive gate",
        ),
        (
            carriers::RATIO_DEMAND_IN,
            "ratio-demand-in",
            "g/h",
            "Paced demand delivered to the interlock",
        ),
        (
            carriers::CLAMPED,
            "clamped",
            "",
            "Demand saturating at a declared dose or rate bound",
        ),
        (
            carriers::FALLBACK_ACTIVE,
            "fallback-active",
            "",
            "Declared bad-flow fallback engaged",
        ),
        (
            carriers::FALLBACK_ACTIVE_IN,
            "fallback-active-in",
            "",
            "Bad-flow fallback delivered to its alarm",
        ),
        (
            carriers::FLOW_NOT_PROVEN,
            "flow-not-proven",
            "",
            "Flow not proven — the inverted flow-proven contact",
        ),
        (
            carriers::FLOW_NOT_PROVEN_IN,
            "flow-not-proven-in",
            "",
            "Flow-not-proven delivered to the interlock",
        ),
        (
            carriers::TANK_EMPTY_TRIP_IN,
            "tank-empty-trip-in",
            "",
            "Tank-empty condition delivered to the interlock",
        ),
        (
            carriers::TANK_EMPTY_INV_IN,
            "tank-empty-inv-in",
            "",
            "Tank-empty condition delivered to the healthy-side inversion",
        ),
        (
            carriers::TANK_OK,
            "tank-ok",
            "",
            "Tank above the empty inhibit",
        ),
        (
            carriers::TANK_OK_IN,
            "tank-ok-in",
            "",
            "Tank-healthy delivered to the permissive gate",
        ),
        (carriers::BUND_OK, "bund-ok", "", "Bund clear"),
        (
            carriers::BUND_OK_IN,
            "bund-ok-in",
            "",
            "Bund-clear delivered to the permissive gate",
        ),
        (
            carriers::EXTERNAL_OK,
            "external-ok",
            "",
            "No external inhibit",
        ),
        (
            carriers::EXTERNAL_OK_IN,
            "external-ok-in",
            "",
            "No-external-inhibit delivered to the permissive gate",
        ),
        (
            carriers::NONE_AVAILABLE,
            "none-available",
            "",
            "No dosing pump reports itself available",
        ),
        (
            carriers::NONE_AVAILABLE_IN,
            "none-available-in",
            "",
            "No-pump-available delivered to its consumers",
        ),
        (
            carriers::PUMP_AVAIL,
            "pump-avail",
            "",
            "At least one dosing pump available",
        ),
        (
            carriers::PUMP_AVAIL_IN,
            "pump-avail-in",
            "",
            "Pump-available delivered to the permissive gate",
        ),
        (
            carriers::DOSING_PERMITTED,
            "dosing-permitted",
            "",
            "Dosing permitted — the aggregated permissive chain",
        ),
        (
            carriers::PERMITTED_IN,
            "permitted-in",
            "",
            "Dosing-permitted delivered to the interlock",
        ),
        (
            carriers::GATED_DEMAND,
            "gated-demand",
            "g/h",
            "Permissive-gated demand",
        ),
        (
            carriers::GATED_DEMAND_IN,
            "gated-demand-in",
            "g/h",
            "Permissive-gated demand delivered to the manual station",
        ),
        (
            carriers::INTERLOCK_TRIPPED,
            "interlock-tripped",
            "",
            "Demand interlock tripped",
        ),
        (carriers::DEMAND, "demand", "g/h", "Commanded chemical rate"),
        (
            carriers::DEMAND_CHAIN_IN,
            "demand-chain-in",
            "g/h",
            "Commanded rate delivered to stage selection",
        ),
        (
            carriers::DEMAND_TOTAL_IN,
            "demand-total-in",
            "g/h",
            "Commanded rate delivered to totalization",
        ),
        (
            carriers::DEMAND_DEVMON_IN,
            "demand-devmon-in",
            "g/h",
            "Commanded rate delivered to dose confirmation",
        ),
        (
            carriers::MANUAL_ACTIVE,
            "manual-active",
            "",
            "Manual mode active",
        ),
        (
            carriers::STAGE_DEMAND,
            "stage-demand",
            "pumps",
            "Stage-count demand the threshold chain computes",
        ),
        (
            carriers::STAGE_DEMAND_IN,
            "stage-demand-in",
            "pumps",
            "Stage-count demand delivered to the pump group",
        ),
        (
            carriers::DUTY,
            "duty",
            "pumps",
            "1-based index of the pump holding duty; 0 while none does",
        ),
        (
            carriers::STAGED,
            "staged",
            "pumps",
            "How many pumps the group currently commands",
        ),
        (
            carriers::ALL_FAULTED,
            "all-faulted",
            "",
            "Every dosing pump's fault flag reads failed",
        ),
        (
            carriers::DUTY_CALL,
            "duty-call",
            "",
            "The chain's call for the duty pump",
        ),
        (
            carriers::LAG_CALL,
            "lag-call",
            "",
            "The chain's call for the lag pump",
        ),
        (
            carriers::BELOW_CUTOFF,
            "below-cutoff",
            "",
            "Demand at or below the chain cut-off",
        ),
        (
            carriers::HIGH_LEVEL,
            "high-level",
            "",
            "Demand at or above the high setpoint",
        ),
        (
            carriers::DOSE,
            "dose",
            "mg/L",
            "Operator dose setpoint per flow unit",
        ),
        (
            carriers::MANUAL_MODE,
            "man-mode",
            "",
            "Manual takeover — false auto, true manual",
        ),
        (
            carriers::MANUAL_RATE,
            "man-rate",
            "g/h",
            "Operator's manual chemical rate",
        ),
        (
            carriers::TOTALIZER_RESET,
            "totalizer-reset",
            "",
            "Commanded-total reset request",
        ),
        (
            carriers::DOSE_TOTAL,
            "dose-total",
            "g",
            "Commanded chemical total",
        ),
        (
            carriers::DEVIATION,
            "deviation",
            "",
            "Commanded-versus-measured relative deviation",
        ),
        (
            carriers::DEVIATING,
            "deviating",
            "",
            "Dose not confirmed — the deviation window tripped",
        ),
        (
            carriers::DEVIATING_IN,
            "deviating-in",
            "",
            "Dose-not-confirmed delivered to its alarm",
        ),
    ] {
        signal(
            &mut plant,
            PointId(point),
            name,
            unit,
            description,
            "dosing-skid",
        );
    }

    // ---------- the skid-level components ----------
    let ratio = plant.add(FlowPacedRatioSpec::new(
        parameters([
            ("min_dose", Value::Float(config.min_dose)),
            ("max_dose", Value::Float(config.max_dose)),
            ("min_rate", Value::Float(config.min_rate)),
            ("max_rate", Value::Float(config.max_rate)),
            ("on_bad_flow", Value::Int(config.on_bad_flow)),
            ("fallback_rate", Value::Float(config.fallback_rate)),
            ("on_bad_trim", Value::Int(config.on_bad_trim)),
        ]),
        false, // trim unwired — the reference paces untrimmed (unity)
    ));
    let invert = || parameters([("invert", Value::Bool(true))]);
    let inv_flow = plant.add(DigitalInputSpec::new(invert()));
    let inv_tank = plant.add(DigitalInputSpec::new(invert()));
    let inv_bund = plant.add(DigitalInputSpec::new(invert()));
    let inv_external = plant.add(DigitalInputSpec::new(invert()));
    let inv_none = plant.add(DigitalInputSpec::new(invert()));
    let permitted = plant.add(BoolGateSpec::new(
        parameters([("operation", Value::Int(GATE_AND))]),
        5,
    ));
    let interlock = plant.add(InterlockSpec::new(
        parameters([("safe_value", Value::Float(config.safe_value))]),
        5,
    ));
    let manual = plant.add(ManualStationSpec::new(parameters([(
        "transfer_delta",
        Value::Float(config.transfer_delta),
    )])));
    let chain = plant.add(ThresholdChainSpec::new(parameters([
        ("cutoff", Value::Float(-1.0)),
        ("stop", Value::Float(0.0)),
        ("start", Value::Float(config.demand_call)),
        ("lag_start", Value::Float(PARKED_LIMIT)),
        ("high", Value::Float(2.0 * PARKED_LIMIT)),
        ("on_bad_demand", Value::Int(0)),
    ])));
    let mut group_parameters = parameters([
        ("rotation", Value::Int(config.rotation)),
        ("start_delay_ticks", Value::Int(config.start_delay_ticks)),
        (
            "restage_delay_ticks",
            Value::Int(config.restage_delay_ticks),
        ),
        ("min_off_ticks", Value::Int(config.min_off_ticks)),
    ]);
    if let Some(rotation_ticks) = config.rotation_ticks {
        group_parameters.insert("rotation_ticks".to_string(), Value::Int(rotation_ticks));
    }
    let group = plant.add(PumpGroupSpec::new(group_parameters, config.pumps));
    let totalizer = plant.add(TotalizerSpec::new(parameters([])));
    let devmon = plant.add(DeviationMonitorSpec::new(parameters([
        ("deviation_limit", Value::Float(config.deviation_limit)),
        ("window_ticks", Value::Int(config.window_ticks)),
    ])));

    // The decision-55 alarm set — each latching on its condition and
    // its own writable ack point. The decision-70 codes are declared
    // data — the site priority/class vocabulary and response budgets
    // stay an open customer assumption.
    let low_alarm = plant.add(LatchingAlarmSpec::new(
        parameters([
            ("low_limit", Value::Float(config.tank_low)),
            ("high_limit", Value::Float(PARKED_LIMIT)),
            ("hysteresis", Value::Float(config.tank_hysteresis)),
            ("priority", Value::Int(2)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(30)),
        ]),
        rationalization(
            "The chemical tank runs low and dosing capacity narrows",
            "Arrange a chemical refill before the tank empties",
            "tank-low-alarm",
        ),
    ));
    let empty_alarm = plant.add(LatchingAlarmSpec::new(
        parameters([
            ("low_limit", Value::Float(config.tank_empty)),
            ("high_limit", Value::Float(PARKED_LIMIT)),
            ("hysteresis", Value::Float(config.tank_hysteresis)),
            ("priority", Value::Int(1)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(30)),
        ]),
        rationalization(
            "The chemical tank is empty and dosing stops",
            "Refill the tank and confirm the permissive clears",
            "tank-empty-alarm",
        ),
    ));
    let pacing_alarm = plant.add(BoolLatchingAlarmSpec::new(
        parameters([
            ("priority", Value::Int(2)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(30)),
        ]),
        rationalization(
            "Dosing runs un-paced against the fallback demand",
            "Check the process flowmeter and the pacing source",
            "pacing-lost-alarm",
        ),
    ));
    let bund_alarm = plant.add(BoolLatchingAlarmSpec::new(
        parameters([
            ("priority", Value::Int(1)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(30)),
        ]),
        rationalization(
            "A chemical spill floods the bund unannounced",
            "Inspect the skid and clear the bund",
            "bund-flood-alarm",
        ),
    ));
    let external_alarm = plant.add(BoolLatchingAlarmSpec::new(
        parameters([
            ("priority", Value::Int(2)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(30)),
        ]),
        rationalization(
            "An external inhibit holds dosing off unnoticed",
            "Trace the external inhibit contact",
            "external-inhibit-alarm",
        ),
    ));
    let deviation_alarm = plant.add(BoolLatchingAlarmSpec::new(
        parameters([
            ("priority", Value::Int(2)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(30)),
        ]),
        rationalization(
            "The delivered dose deviates from the demand unnoticed",
            "Check the pump delivery and the dose calculation",
            "dose-deviation-alarm",
        ),
    ));

    // ---------- the ratio demand and the permissive chain ----------
    // The operator dose rides the writable internal point; `demand`,
    // `clamped`, and `fallback_active` land on carriers, each fan-out
    // consumer reading through its own `In`.
    plant.connect(flow, &ratio.flow);
    plant.connect(dose, &ratio.dose);
    plant.connect(&ratio.demand, ratio_demand);
    plant.connect(ratio_demand_in, ratio_demand);
    plant.connect(&ratio.clamped, clamped);
    plant.connect(&ratio.fallback_active, fallback_active);
    plant.connect(fallback_active_in, fallback_active);

    // The healthy legs: `digital-input` inversions of the inhibit
    // contacts, folded through a `bool-gate` `and` into
    // `dosing-permitted`.
    plant.connect(flow_proven, &inv_flow.input);
    plant.connect(&inv_flow.out, flow_not_proven);
    plant.connect(flow_not_proven_in, flow_not_proven);
    plant.connect(tank_empty_inv_in, &inv_tank.input);
    plant.connect(&inv_tank.out, tank_ok);
    plant.connect(tank_ok_in, tank_ok);
    plant.connect(bund_flood, &inv_bund.input);
    plant.connect(&inv_bund.out, bund_ok);
    plant.connect(bund_ok_in, bund_ok);
    plant.connect(external_inhibit, &inv_external.input);
    plant.connect(&inv_external.out, external_ok);
    plant.connect(external_ok_in, external_ok);
    plant.connect(none_available_in, &inv_none.input);
    plant.connect(&inv_none.out, pump_avail);
    plant.connect(pump_avail_in, pump_avail);
    plant.connect(flow_proven, permitted.input(1));
    plant.connect(tank_ok_in, permitted.input(2));
    plant.connect(bund_ok_in, permitted.input(3));
    plant.connect(external_ok_in, permitted.input(4));
    plant.connect(pump_avail_in, permitted.input(5));
    plant.connect(&permitted.out, dosing_permitted);
    plant.connect(permitted_in, dosing_permitted);

    // The demand interlock — `permissive` on the aggregated chain,
    // `trip_N` on the individual inhibits in the decision's order.
    plant.connect(ratio_demand_in, &interlock.input);
    plant.connect(permitted_in, &interlock.permissive);
    plant.connect(flow_not_proven_in, interlock.trip(1));
    plant.connect(tank_empty_trip_in, interlock.trip(2));
    plant.connect(bund_flood, interlock.trip(3));
    plant.connect(external_inhibit, interlock.trip(4));
    plant.connect(none_available_in, interlock.trip(5));
    plant.connect(&interlock.out, gated_demand);
    plant.connect(gated_demand_in, gated_demand);
    plant.connect(&interlock.tripped, interlock_tripped);

    // Operator takeover on the demand path.
    plant.connect(gated_demand_in, &manual.control);
    plant.connect(manual_rate, &manual.manual);
    plant.connect(manual_mode, &manual.mode);
    plant.connect(&manual.out, demand);
    plant.connect(&manual.manual_active, manual_active);

    // The commanded rate fans out through per-consumer `In` carriers:
    // stage selection, commanded totalization, dose confirmation, and
    // each pump's speed gate below.
    plant.connect(demand_chain_in, demand);
    plant.connect(demand_total_in, demand);
    plant.connect(demand_devmon_in, demand);

    // Stage selection — the demand-level threshold chain calls the
    // duty pump while any demand stands; `lag_start`/`high` park far
    // outside the demand span since duty/standby never stages a second
    // pump on demand.
    plant.connect(demand_chain_in, &chain.level);
    plant.connect(&chain.demand, stage_demand);
    plant.connect(stage_demand_in, stage_demand);
    plant.connect(stage_demand_in, &group.demand);
    plant.connect(&chain.duty_call, duty_call);
    plant.connect(&chain.lag_call, lag_call);
    plant.connect(&chain.below_cutoff, below_cutoff);
    plant.connect(&chain.high_level, high_level);
    plant.connect(&group.duty, duty);
    plant.connect(&group.staged, staged);
    plant.connect(&group.none_available, none_available);
    plant.connect(none_available_in, none_available);
    plant.connect(&group.all_faulted, all_faulted);

    // Accounting — the commanded total and the dose-confirmation
    // window against the measured discharge.
    plant.connect(demand_total_in, &totalizer.rate);
    plant.connect(totalizer_reset, &totalizer.reset);
    plant.connect(&totalizer.total, dose_total);
    plant.connect(demand_devmon_in, &devmon.expected);
    plant.connect(discharge_rate, &devmon.measured);
    plant.connect(&devmon.deviation, deviation);
    plant.connect(&devmon.deviating, deviating);
    plant.connect(deviating_in, deviating);

    // ---------- the decision-55 alarm set ----------
    let (tank_low_alarm, _) = skid_alarm(&mut plant, 0, &low_alarm, "tank-low", "dosing-skid");
    plant.connect(tank_level, &low_alarm.input);
    let (tank_empty_alarm, tank_empty_flag) =
        skid_alarm(&mut plant, 1, &empty_alarm, "tank-empty", "dosing-skid");
    plant.connect(tank_level, &empty_alarm.input);
    // The empty condition is the standing alarm flag itself: the trip
    // and the healthy-side inversion read it through their own `In`
    // consumers so trip and alarm always agree.
    plant.connect(tank_empty_trip_in, tank_empty_flag);
    plant.connect(tank_empty_inv_in, tank_empty_flag);
    let (pacing_alarm_layout, _) =
        skid_alarm(&mut plant, 2, &pacing_alarm, "pacing-lost", "dosing-skid");
    plant.connect(fallback_active_in, &pacing_alarm.input);
    let (bund_alarm_layout, _) =
        skid_alarm(&mut plant, 3, &bund_alarm, "bund-flood", "dosing-skid");
    plant.connect(bund_flood, &bund_alarm.input);
    let (external_alarm_layout, _) = skid_alarm(
        &mut plant,
        4,
        &external_alarm,
        "external-inhibit",
        "dosing-skid",
    );
    plant.connect(external_inhibit, &external_alarm.input);
    let (deviation_alarm_layout, _) = skid_alarm(
        &mut plant,
        5,
        &deviation_alarm,
        "dose-not-confirmed",
        "dosing-skid",
    );
    plant.connect(deviating_in, &deviation_alarm.input);

    // Per-pump wiring.
    let mut pumps = Vec::with_capacity(config.pumps);
    for (index, (draw_ch, rate_ch, run_ch, local_ch, pfault_ch, stroke_ch, cmd_ch, speed_ch)) in
        pump_channels.into_iter().enumerate()
    {
        pumps.push(wire_pump(
            &mut plant, config, index, draw_ch, rate_ch, run_ch, local_ch, pfault_ch, stroke_ch,
            cmd_ch, speed_ch, demand, &group,
        ));
    }

    let model = plant.build()?;
    Ok(DosingSkid {
        model,
        layout: DosingSkidLayout {
            flow: points::FLOW,
            flow_source: points::FLOW_SOURCE,
            tank_level: points::TANK_LEVEL,
            discharge_rate: points::DISCHARGE_RATE,
            net_draw: points::NET_DRAW,
            tank_refill: points::TANK_REFILL,
            flow_proven: points::FLOW_PROVEN,
            bund_flood: points::BUND_FLOOD,
            external_inhibit: points::EXTERNAL_INHIBIT,
            ratio_demand: PointId(carriers::RATIO_DEMAND),
            gated_demand: PointId(carriers::GATED_DEMAND),
            demand: PointId(carriers::DEMAND),
            stage_demand: PointId(carriers::STAGE_DEMAND),
            duty: PointId(carriers::DUTY),
            staged: PointId(carriers::STAGED),
            none_available: PointId(carriers::NONE_AVAILABLE),
            all_faulted: PointId(carriers::ALL_FAULTED),
            clamped: PointId(carriers::CLAMPED),
            fallback_active: PointId(carriers::FALLBACK_ACTIVE),
            dosing_permitted: PointId(carriers::DOSING_PERMITTED),
            interlock_tripped: PointId(carriers::INTERLOCK_TRIPPED),
            manual_active: PointId(carriers::MANUAL_ACTIVE),
            dose_total: PointId(carriers::DOSE_TOTAL),
            deviation: PointId(carriers::DEVIATION),
            deviating: PointId(carriers::DEVIATING),
            dose: PointId(carriers::DOSE),
            manual_mode: PointId(carriers::MANUAL_MODE),
            manual_rate: PointId(carriers::MANUAL_RATE),
            totalizer_reset: PointId(carriers::TOTALIZER_RESET),
            ratio: ratio.id,
            interlock: interlock.id,
            manual_station: manual.id,
            threshold_chain: chain.id,
            pump_group: group.id,
            totalizer: totalizer.id,
            deviation_monitor: devmon.id,
            tank_low_alarm,
            tank_empty_alarm,
            pacing_alarm: pacing_alarm_layout,
            bund_alarm: bund_alarm_layout,
            external_alarm: external_alarm_layout,
            deviation_alarm: deviation_alarm_layout,
            pumps,
        },
    })
}

/// The 1-based tag "p201", "p202", … matching the recorded dosing
/// fixtures.
fn pump_tag(index: usize) -> String {
    format!("p{}", 201 + index)
}

/// Registers point `point`'s monitoring signal — `10000 + point` —
/// carrying the full unit/description/group metadata WW-FND-001 asks
/// the surface to render from.
fn signal(
    plant: &mut PlantBuilder,
    point: PointId,
    name: &str,
    unit: &str,
    description: &str,
    group: &str,
) {
    plant
        .signal(SignalId(SIGNAL_BASE + point.0), name, point)
        .unit(unit)
        .description(description)
        .group(group);
}

/// What the shared alarm wiring needs of either latching kind: the
/// instance id, the ack sink, and the two output handles.
trait SkidAlarmPorts {
    /// The instance's component id.
    fn id(&self) -> ComponentId;
    /// The `ack` port.
    fn ack(&self) -> Sink<bool>;
    /// The `alarm` port.
    fn alarm(&self) -> Source<bool>;
    /// The `unacknowledged` port.
    fn unacknowledged(&self) -> Source<bool>;
}

impl SkidAlarmPorts for LatchingAlarmInstance {
    fn id(&self) -> ComponentId {
        self.id
    }
    fn ack(&self) -> Sink<bool> {
        self.ack.clone()
    }
    fn alarm(&self) -> Source<bool> {
        self.alarm.clone()
    }
    fn unacknowledged(&self) -> Source<bool> {
        self.unacknowledged.clone()
    }
}

impl SkidAlarmPorts for BoolLatchingAlarmInstance {
    fn id(&self) -> ComponentId {
        self.id
    }
    fn ack(&self) -> Sink<bool> {
        self.ack.clone()
    }
    fn alarm(&self) -> Source<bool> {
        self.alarm.clone()
    }
    fn unacknowledged(&self) -> Source<bool> {
        self.unacknowledged.clone()
    }
}

/// Declares one alarm's points — the writable `ack`, the `alarm` and
/// `unacknowledged` outputs — wires them, and registers the three
/// signals under `{prefix}-ack`/`-alarm`/`-unacknowledged`. Returns
/// the layout plus the `alarm` flag's `Out` handle so a caller can fan
/// the condition out to declared consumers; the caller wires the
/// alarm's `in` port — its value kind differs between the analog and
/// Bool kinds.
fn skid_alarm<A: SkidAlarmPorts>(
    plant: &mut PlantBuilder,
    index: u64,
    instance: &A,
    prefix: &str,
    group: &str,
) -> (AlarmLayout, OutPoint<bool>) {
    let base = ALARM_BASE + index * 10;
    let ack = plant.internal_input::<bool>(PointId(base), false, true);
    let alarm = plant.internal_output::<bool>(PointId(base + 1), false);
    let unacknowledged = plant.internal_output::<bool>(PointId(base + 2), false);
    // Decision 74's lifecycle audit: the `alarm`/`unacknowledged`
    // status points are `journaled` — activation, return, and the
    // latch's clear all land as durable `point_changed` entries. The
    // `ack` point stays receipted-only: its writes are already the
    // attributed record.
    plant.journaled(alarm);
    plant.journaled(unacknowledged);
    plant.connect(ack, instance.ack());
    plant.connect(instance.alarm(), alarm);
    plant.connect(instance.unacknowledged(), unacknowledged);
    for (offset, suffix, description) in [
        (0, "ack", "Operator acknowledgment for the alarm"),
        (1, "alarm", "Standing alarm state"),
        (
            2,
            "unacknowledged",
            "Latched until the operator acknowledges",
        ),
    ] {
        signal(
            plant,
            PointId(base + offset),
            &format!("{prefix}-{suffix}"),
            "",
            description,
            group,
        );
    }
    (
        AlarmLayout {
            component: instance.id(),
            ack: PointId(base),
            alarm: PointId(base + 1),
            unacknowledged: PointId(base + 2),
        },
        alarm,
    )
}

/// Wires pump `index` (`0`-based): field points, the availability
/// aggregation, the run-gated speed interlock, the motor, the analog
/// output, the stroke counter, and the pump's two alarms — then
/// connects the group's indexed ports.
#[allow(clippy::too_many_arguments)]
fn wire_pump(
    plant: &mut PlantBuilder,
    config: &DosingSkidConfig,
    index: usize,
    draw_ch: ChannelRef,
    rate_ch: ChannelRef,
    run_ch: ChannelRef,
    local_ch: ChannelRef,
    pfault_ch: ChannelRef,
    stroke_ch: ChannelRef,
    cmd_ch: ChannelRef,
    speed_ch: ChannelRef,
    demand: OutPoint<f64>,
    group: &PumpGroupInstance,
) -> DosingPumpLayout {
    let i = index as u64;
    let tag = pump_tag(index);
    let group_name = format!("pump-{tag}");
    let base = PUMP_BASE + i * PUMP_STRIDE;

    // Field points; `draw` and `rate` are produced by the dynamics
    // document's `bool_flow`/`scaled_flow` elements, so their handles
    // go unused.
    plant.field_input::<f64>(points::draw(index), draw_ch, false);
    plant.field_input::<f64>(points::rate(index), rate_ch, false);
    let run = plant.field_input::<bool>(points::run(index), run_ch, false);
    let local = plant.field_input::<bool>(points::local(index), local_ch, false);
    let pfault = plant.field_input::<bool>(points::pump_fault(index), pfault_ch, false);
    let stroke = plant.field_input::<bool>(points::stroke(index), stroke_ch, false);
    // `run` is the activation state, `local` the authority state, and
    // `pfault` the pump's fault contact — the protection layer's
    // reported states, journaled per decisions 74 and 77. The stroke
    // pulse is per-stroke churn — history-ring-only.
    plant.journaled(run);
    plant.journaled(local);
    plant.journaled(pfault);
    let cmd = plant.field_output::<bool>(points::cmd(index), cmd_ch);
    let speed = plant.field_output::<f64>(points::speed(index), speed_ch);
    for (point, name, unit, description) in [
        (
            points::draw(index),
            "draw",
            "L/scan",
            "Chemical draw the bool_flow element drives",
        ),
        (
            points::rate(index),
            "rate",
            "g/h",
            "Metered discharge the scaled_flow element drives",
        ),
        (points::run(index), "run", "", "Run feedback contact"),
        (
            points::local(index),
            "local",
            "",
            "Local-selected — the pump on its own controls",
        ),
        (
            points::pump_fault(index),
            "fault",
            "",
            "Pump fault / no-discharge contact",
        ),
        (
            points::stroke(index),
            "stroke",
            "",
            "Stroke pulse the counter accumulates",
        ),
        (points::cmd(index), "cmd", "", "Field run command"),
        (points::speed(index), "speed", "%", "Analog speed demand"),
    ] {
        signal(
            plant,
            point,
            &format!("{tag}-{name}"),
            unit,
            description,
            &group_name,
        );
    }

    // The run contact loopback: the sim observes the driven command —
    // `connect`'s `from` is the observing `In` point, `to` the driving
    // `Out` point.
    plant.connect(run, cmd);

    // Writable operator points: out of service and the stroke-counter
    // reset. `out_of_service` is the equipment's managed-state flag —
    // journaled; the reset pulse rides its attributed receipt.
    let oos = plant.internal_input::<bool>(PointId(base + pump_point::OUT_OF_SERVICE), false, true);
    plant.journaled(oos);
    let stroke_reset =
        plant.internal_input::<bool>(PointId(base + pump_point::STROKE_RESET), false, true);
    signal(
        plant,
        PointId(base + pump_point::OUT_OF_SERVICE),
        &format!("{tag}-oos"),
        "",
        "Out of service — inhibits automatic operation",
        &group_name,
    );
    signal(
        plant,
        PointId(base + pump_point::STROKE_RESET),
        &format!("{tag}-stroke-reset"),
        "",
        "Stroke-counter reset request",
        &group_name,
    );

    // Carriers and consumers. The healthy-side seeds read the declared
    // cold-start state — remote-selected, in service, no pump fault —
    // so `avail_i` and the permissive don't flicker for a scan.
    let group_cmd = plant.internal_output::<bool>(PointId(base + pump_point::GROUP_CMD), false);
    let group_cmd_in =
        plant.internal_input::<bool>(PointId(base + pump_point::GROUP_CMD_IN), false, false);
    let local_ok = plant.internal_output::<bool>(PointId(base + pump_point::LOCAL_OK), true);
    let local_ok_in =
        plant.internal_input::<bool>(PointId(base + pump_point::LOCAL_OK_IN), false, false);
    let oos_ok = plant.internal_output::<bool>(PointId(base + pump_point::OOS_OK), true);
    let oos_ok_in =
        plant.internal_input::<bool>(PointId(base + pump_point::OOS_OK_IN), false, false);
    let pfault_ok = plant.internal_output::<bool>(PointId(base + pump_point::PFAULT_OK), true);
    let pfault_ok_in =
        plant.internal_input::<bool>(PointId(base + pump_point::PFAULT_OK_IN), false, false);
    let avail_carrier = plant.internal_output::<bool>(PointId(base + pump_point::AVAIL), true);
    let avail_in = plant.internal_input::<bool>(PointId(base + pump_point::AVAIL_IN), false, false);
    let motor_fault = plant.internal_output::<bool>(PointId(base + pump_point::MOTOR_FAULT), false);
    let fault_group_in = plant.internal_input::<bool>(
        PointId(base + pump_point::MOTOR_FAULT_GROUP_IN),
        false,
        false,
    );
    let fault_alarm_in = plant.internal_input::<bool>(
        PointId(base + pump_point::MOTOR_FAULT_ALARM_IN),
        false,
        false,
    );
    let demand_in = plant.internal_input::<f64>(PointId(base + pump_point::DEMAND_IN), 0.0, false);
    let speed_eng = plant.internal_output::<f64>(PointId(base + pump_point::SPEED_ENG), 0.0);
    let speed_eng_in =
        plant.internal_input::<f64>(PointId(base + pump_point::SPEED_ENG_IN), 0.0, false);
    let speed_gated = plant.internal_output::<bool>(PointId(base + pump_point::SPEED_GATED), false);
    // The aggregated availability, the proven motor fault, and the
    // speed interlock's gated flag are protection-relevant status —
    // `journaled`; the inverted legs and delivered copies stay off the
    // record, their sources already carry it.
    plant.journaled(avail_carrier);
    plant.journaled(motor_fault);
    plant.journaled(speed_gated);
    let strokes = plant.internal_output::<i64>(PointId(base + pump_point::STROKES), 0);
    let strokes_done =
        plant.internal_output::<bool>(PointId(base + pump_point::STROKES_DONE), false);
    for (point, name, description) in [
        (
            pump_point::GROUP_CMD,
            "group-cmd",
            "The pump group's automatic run request",
        ),
        (
            pump_point::GROUP_CMD_IN,
            "group-cmd-in",
            "Group request delivered to the motor",
        ),
        (
            pump_point::LOCAL_OK,
            "local-ok",
            "Remote-selected — the inverted local indication",
        ),
        (
            pump_point::LOCAL_OK_IN,
            "local-ok-in",
            "Remote-selected delivered to availability",
        ),
        (
            pump_point::OOS_OK,
            "oos-ok",
            "In service — the inverted out-of-service point",
        ),
        (
            pump_point::OOS_OK_IN,
            "oos-ok-in",
            "In-service delivered to availability",
        ),
        (
            pump_point::PFAULT_OK,
            "pfault-ok",
            "Pump fault contact healthy — the inverted contact",
        ),
        (
            pump_point::PFAULT_OK_IN,
            "pfault-ok-in",
            "Pump-healthy delivered to availability",
        ),
        (
            pump_point::AVAIL,
            "avail",
            "Aggregated availability for the pump group",
        ),
        (
            pump_point::AVAIL_IN,
            "avail-in",
            "Availability delivered to the pump group",
        ),
        (
            pump_point::MOTOR_FAULT,
            "motor-fault",
            "The motor's proven command/feedback fault",
        ),
        (
            pump_point::MOTOR_FAULT_GROUP_IN,
            "motor-fault-group-in",
            "Motor fault delivered to the pump group",
        ),
        (
            pump_point::MOTOR_FAULT_ALARM_IN,
            "motor-fault-alarm-in",
            "Motor fault delivered to the alarm",
        ),
        (
            pump_point::DEMAND_IN,
            "demand-in",
            "Commanded rate delivered to the speed gate",
        ),
        (pump_point::SPEED_ENG, "speed-eng", "Run-gated speed demand"),
        (
            pump_point::SPEED_ENG_IN,
            "speed-eng-in",
            "Speed demand delivered to the analog output",
        ),
        (
            pump_point::SPEED_GATED,
            "speed-gated",
            "Speed demand gated while the pump is not proven running",
        ),
        (pump_point::STROKES, "strokes", "Accumulated stroke count"),
        (
            pump_point::STROKES_DONE,
            "strokes-done",
            "Stroke preset reached",
        ),
    ] {
        let unit = match point {
            pump_point::DEMAND_IN | pump_point::SPEED_ENG | pump_point::SPEED_ENG_IN => "g/h",
            pump_point::STROKES => "strokes",
            _ => "",
        };
        signal(
            plant,
            PointId(base + point),
            &format!("{tag}-{name}"),
            unit,
            description,
            &group_name,
        );
    }

    // The availability aggregation and the actuation components.
    let invert = || parameters([("invert", Value::Bool(true))]);
    let inv_local = plant.add(DigitalInputSpec::new(invert()));
    let inv_oos = plant.add(DigitalInputSpec::new(invert()));
    let inv_pfault = plant.add(DigitalInputSpec::new(invert()));
    let avail = plant.add(BoolGateSpec::new(
        parameters([("operation", Value::Int(GATE_AND))]),
        3,
    ));
    let motor = plant.add(MotorSpec::new(parameters([(
        "fault_ticks",
        Value::Int(config.motor_fault_ticks),
    )])));
    // The per-pump speed gate: the shared demand passes only while the
    // pump's run contact proves it running — what keeps the metered
    // discharge honest about a no-delivery fault.
    let speed_gate = plant.add(InterlockSpec::new(
        parameters([("safe_value", Value::Float(0.0))]),
        0,
    ));
    let analog_out = plant.add(AnalogOutputSpec::<f64>::new(parameters([
        ("raw_min", Value::Float(0.0)),
        ("raw_max", Value::Float(100.0)),
        ("eng_min", Value::Float(0.0)),
        ("eng_max", Value::Float(config.max_rate)),
    ])));
    let counter = plant.add(CounterSpec::new(parameters([(
        "preset",
        Value::Int(config.stroke_preset),
    )])));
    let fault_alarm = plant.add(BoolLatchingAlarmSpec::new(
        parameters([
            ("priority", Value::Int(2)),
            ("class", Value::Int(2)),
            ("response_ticks", Value::Int(60)),
        ]),
        rationalization(
            "The pump cannot dose while its fault stands",
            "Clear the motor fault and reset the pump",
            &format!("{tag}-fault-alarm"),
        ),
    ));
    let pfault_alarm = plant.add(BoolLatchingAlarmSpec::new(
        parameters([
            ("priority", Value::Int(2)),
            ("class", Value::Int(2)),
            ("response_ticks", Value::Int(60)),
        ]),
        rationalization(
            "The pump runs without proven discharge",
            "Check the pump's discharge path and fault contact",
            &format!("{tag}-pfault-alarm"),
        ),
    ));

    // avail_i = remote-selected and in-service and pump-fault contact
    // healthy — the decision-52 availability wiring.
    plant.connect(local, &inv_local.input);
    plant.connect(&inv_local.out, local_ok);
    plant.connect(local_ok_in, local_ok);
    plant.connect(oos, &inv_oos.input);
    plant.connect(&inv_oos.out, oos_ok);
    plant.connect(oos_ok_in, oos_ok);
    plant.connect(pfault, &inv_pfault.input);
    plant.connect(&inv_pfault.out, pfault_ok);
    plant.connect(pfault_ok_in, pfault_ok);
    plant.connect(local_ok_in, avail.input(1));
    plant.connect(oos_ok_in, avail.input(2));
    plant.connect(pfault_ok_in, avail.input(3));
    plant.connect(&avail.out, avail_carrier);
    plant.connect(avail_in, avail_carrier);
    plant.connect(avail_in, group.avail(index + 1));

    // The motor: the group's request straight to `cmd`, run feedback in
    // and out, `fault` fanned to the group and the alarm.
    plant.connect(group.cmd(index + 1), group_cmd);
    plant.connect(group_cmd_in, group_cmd);
    plant.connect(group_cmd_in, &motor.cmd);
    plant.connect(run, &motor.run);
    plant.connect(&motor.out, cmd);
    plant.connect(run, group.run(index + 1));
    plant.connect(&motor.fault, motor_fault);
    plant.connect(fault_group_in, motor_fault);
    plant.connect(fault_group_in, group.fault(index + 1));
    plant.connect(fault_alarm_in, motor_fault);
    plant.connect(fault_alarm_in, &fault_alarm.input);

    // The speed path: the shared demand, gated by the proven run
    // contact, scaled onto the field output.
    plant.connect(demand_in, demand);
    plant.connect(demand_in, &speed_gate.input);
    plant.connect(run, &speed_gate.permissive);
    plant.connect(&speed_gate.out, speed_eng);
    plant.connect(speed_eng_in, speed_eng);
    plant.connect(speed_eng_in, &analog_out.eng);
    plant.connect(&speed_gate.tripped, speed_gated);
    plant.connect(&analog_out.raw, speed);

    // The stroke counter.
    plant.connect(stroke, &counter.input);
    plant.connect(stroke_reset, &counter.reset);
    plant.connect(&counter.count, strokes);
    plant.connect(&counter.done, strokes_done);

    // The pump's two alarms — the motor's proven fault and the pump's
    // own fault / no-discharge contact — each on its own writable ack.
    let fault_ack =
        plant.internal_input::<bool>(PointId(base + pump_point::FAULT_ACK), false, true);
    let fault_alarm_out =
        plant.internal_output::<bool>(PointId(base + pump_point::FAULT_ALARM), false);
    let fault_unack_out =
        plant.internal_output::<bool>(PointId(base + pump_point::FAULT_UNACK), false);
    let pfault_ack =
        plant.internal_input::<bool>(PointId(base + pump_point::PFAULT_ACK), false, true);
    let pfault_alarm_out =
        plant.internal_output::<bool>(PointId(base + pump_point::PFAULT_ALARM), false);
    let pfault_unack_out =
        plant.internal_output::<bool>(PointId(base + pump_point::PFAULT_UNACK), false);
    // Every alarm's `alarm`/`unacknowledged` status points join the
    // durable record — decision 74's lifecycle audit.
    for point in [
        fault_alarm_out,
        fault_unack_out,
        pfault_alarm_out,
        pfault_unack_out,
    ] {
        plant.journaled(point);
    }
    plant.connect(fault_ack, &fault_alarm.ack);
    plant.connect(&fault_alarm.alarm, fault_alarm_out);
    plant.connect(&fault_alarm.unacknowledged, fault_unack_out);
    plant.connect(pfault, &pfault_alarm.input);
    plant.connect(pfault_ack, &pfault_alarm.ack);
    plant.connect(&pfault_alarm.alarm, pfault_alarm_out);
    plant.connect(&pfault_alarm.unacknowledged, pfault_unack_out);
    for (point, name, description) in [
        (
            pump_point::FAULT_ACK,
            "fault-ack",
            "Operator acknowledgment for the motor-fault alarm",
        ),
        (
            pump_point::FAULT_ALARM,
            "fault-alarm",
            "Standing motor-fault alarm",
        ),
        (
            pump_point::FAULT_UNACK,
            "fault-unack",
            "Motor fault latched until acknowledged",
        ),
        (
            pump_point::PFAULT_ACK,
            "pfault-ack",
            "Operator acknowledgment for the pump-fault alarm",
        ),
        (
            pump_point::PFAULT_ALARM,
            "pfault-alarm",
            "Standing pump fault / no-discharge alarm",
        ),
        (
            pump_point::PFAULT_UNACK,
            "pfault-unack",
            "Pump fault latched until acknowledged",
        ),
    ] {
        signal(
            plant,
            PointId(base + point),
            &format!("{tag}-{name}"),
            "",
            description,
            &group_name,
        );
    }

    DosingPumpLayout {
        index: index + 1,
        cmd: points::cmd(index),
        run: points::run(index),
        local: points::local(index),
        pump_fault: points::pump_fault(index),
        stroke: points::stroke(index),
        speed: points::speed(index),
        draw: points::draw(index),
        rate: points::rate(index),
        out_of_service: PointId(base + pump_point::OUT_OF_SERVICE),
        stroke_reset: PointId(base + pump_point::STROKE_RESET),
        group_cmd: PointId(base + pump_point::GROUP_CMD),
        avail: PointId(base + pump_point::AVAIL),
        motor_fault: PointId(base + pump_point::MOTOR_FAULT),
        speed_eng: PointId(base + pump_point::SPEED_ENG),
        speed_gated: PointId(base + pump_point::SPEED_GATED),
        strokes: PointId(base + pump_point::STROKES),
        strokes_done: PointId(base + pump_point::STROKES_DONE),
        motor: motor.id,
        speed_gate: speed_gate.id,
        analog_output: analog_out.id,
        counter: counter.id,
        fault_alarm: AlarmLayout {
            component: fault_alarm.id,
            ack: PointId(base + pump_point::FAULT_ACK),
            alarm: PointId(base + pump_point::FAULT_ALARM),
            unacknowledged: PointId(base + pump_point::FAULT_UNACK),
        },
        pump_fault_alarm: AlarmLayout {
            component: pfault_alarm.id,
            ack: PointId(base + pump_point::PFAULT_ACK),
            alarm: PointId(base + pump_point::PFAULT_ALARM),
            unacknowledged: PointId(base + pump_point::PFAULT_UNACK),
        },
    }
}
