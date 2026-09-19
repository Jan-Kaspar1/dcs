//! The showcase plant: a pump-and-tank line exercising the whole
//! `dcs-blocks` library through the standard model-driven assembly path —
//! the plant the monitoring UI demos against.
//!
//! `fixtures/showcase.json` declares the line; the issue-155 extension
//! grew that one fixture rather than adding a sibling, so the showcase
//! stays a single documented plant covering every registered kind:
//!
//! ```text
//!            P-101                LV-101
//!  supply ──▶ pump ──▶ inlet valve ──▶ T-101 tank ──▶ drain
//! ```
//!
//! The tank's level loop runs three redundant transmitters —
//! `LT-101A/B/C` (`analog-input` ×3) — into `LY-101` (`median-voter`),
//! through `LIF-101` (`signal-filter`) into `LIC-101` (`pid`, whose
//! setpoint `B-101` (`override-select`) switches between the operator's
//! rate-limited value and the batch program `B-101` (`sequencer`)
//! drives), then `I-101` (`interlock`) → `HIC-101` (`manual-station`)
//! → `LV-101` (`valve`) → `analog-output` → the field, with the valve's
//! position feedback reconditioned by a fourth `analog-input`. `FT-101`
//! (`analog-input`) reads the inlet flow the valve position carries and
//! `FQ-101` (`totalizer`) accumulates it. `LA-101` (`alarm-monitor`)
//! watches the level and drives a horn through a `digital-output`;
//! `LAL-102` (`latching-alarm`) watches the same level, latches trips
//! until the operator's writable internal ack point clears them through
//! the ordinary command path — decision 33 — and drives a panel lamp
//! through a second `digital-output`. `LSH-101`'s high-high switch,
//! conditioned by a `digital-input`, is the interlock's trip input. The
//! pump's chain runs the operator's start request through a `timer`
//! start delay into a `motor` whose field loopback supplies run
//! feedback — conditioned by a `digital-input`, fanned out to the
//! motor's `run` port and the interlock's `permissive` — and a `counter`
//! records the interlock's trips. Every `dcs-blocks` kind is on the
//! sheet: channel blocks (`analog-input` ×4, `analog-output`,
//! `digital-input` ×2, `digital-output` ×2), control (`pid`,
//! `rate-limiter`, `timer`, `counter`, `signal-filter`, `totalizer`),
//! voting and selection (`median-voter`, `override-select`,
//! `manual-station`, `sequencer`), actuators (`valve`, `motor`), and
//! safety (`interlock`, `alarm-monitor`, `latching-alarm`).
//!
//! Operator-facing values are channel-less internal points declared
//! `writable` — the level setpoint, the pump start request, the valve
//! station's manual position and mode select, the batch sequence's run,
//! reset, and program select, the latching alarm's acknowledgment, and
//! the totalizer's and trip counter's resets — so `Command::WriteValue`
//! submissions land at scan boundaries per the command contract. Wiring
//! uses every connection shape: `point → port` bindings, `port → port`
//! links (synthesized internal pairs), declared internal `Out`/`In`
//! carriers (fan-out of the voted and filtered level, the conditioned
//! run status, the tripped and alarm statuses, the sequencer's program,
//! and the scaled flow), and `point → point` wires — the field loopbacks
//! making the pump's run feedback follow its command and the valve's
//! position follow its command, plus the declared internal links driving
//! the carriers' consumers.
//!
//! The simulated plant lives in the driver, not the model —
//! `fixtures/showcase_dynamics.json`, the same document
//! `dcs-plant-server --dynamics` loads, is merged into the still-open
//! channel map: a [`SecondOrderLag`] drives `lt101a_raw` from
//! `lv101_raw` — the underdamped tank a retune visibly changes the
//! overshoot of — first-order transmitter lags carry it to
//! `lt101b_raw`/`lt101c_raw`, and a first-order lag drives `ft101_raw`
//! from the valve's raw position feedback.
//!
//! # The documented run
//!
//! [`run`] drives the plant through seven phases; every boundary is a
//! documented scan tick:
//!
//! 1. **Settle** — [`SETTLE_SCANS`] scans at the declared 60% setpoint.
//!    The pump's start delay holds its command back two scans, the run
//!    loopback and debounce take two more to prove the permissive, so the
//!    interlock holds the valve at `safe_value` for the first scans and
//!    registers that start-delay trip as the counter's first count. With
//!    the permissive proven the loop closes; the level dips while the
//!    valve is held shut, then recovers and settles at 60%, and the
//!    totalizer starts accumulating the inlet flow.
//! 2. **Setpoint command** — at the boundary after settle the run
//!    submits `WriteValue` moving the setpoint to [`MOVED_SETPOINT`]%;
//!    the receipt reports `Accepted { apply_tick: SETTLE_SCANS + 1 }`
//!    and the queued value lands at that scan's head. The rate limiter
//!    ramps the pid's setpoint 5%/scan, and after [`MOVED_SCANS`] scans
//!    the level sits at 40%.
//! 3. **Batch** — the operator writes the batch-mode select and the
//!    sequence's `run`; `B-101`'s table walks the pid's setpoint through
//!    a fill–hold–drain–fill–hold recipe over [`BATCH_SCANS`] scans and
//!    asserts `done` on the last. Mid-table the run retunes the pid's
//!    `kp` through `Command::SetParameter`, so the recipe's two
//!    identical up-steps bracket a tuned overshoot the level trace
//!    shows — the demonstration the underdamped tank exists for. The
//!    batch then exits: `run` and the mode select drop and `reset`
//!    pulses the sequencer back to its first step.
//! 4. **Voter discrepancy** — the run overwrites `lt101c_raw` for
//!    [`DEVIANT_SCANS`] scans, the way a stuck or drifting transmitter
//!    presents: the voter's `discrepancy` asserts while its median keeps
//!    the level on the good pair — the loop never sees the deviant leg.
//!    Released, the lagged leg reconverges and the discrepancy clears.
//! 5. **Manual transfer** — the operator writes the station's manual
//!    position and mode select; `HIC-101` slews the valve command from
//!    the control value to the manual one at `transfer_delta` per scan
//!    and reports `manual_active` — the snapshot mid-slew catches the
//!    command between the two sources. Dropping the mode slews back to
//!    the controller's output and the loop re-settles at the operator
//!    setpoint.
//! 6. **Injected fault** — [`Fault::Disconnected`] on `p101_run`: the
//!    field read fails, so the run feedback's image sample keeps its
//!    last value marked `Bad(CommunicationFault)`. The conditioned run
//!    status goes bad with it; the interlock trips on the bad
//!    permissive — its documented rule — and drives the valve to
//!    `safe_value` 0%, the motor's run-feedback check cannot prove
//!    agreement and asserts its `fault` after `fault_ticks` scans, and
//!    the level drains through the low alarm limit: `LA-101` asserts and
//!    the horn follows while `LAL-102` latches the trip — alarm on,
//!    `unacknowledged` on, the beacon lamp lit. The second trip edge
//!    brings the counter to its preset of two — `done` asserts. The
//!    operator then writes the ack point through the ordinary
//!    `WriteValue` command path — decision 33: a writable internal `In`
//!    point, receipted like any other write — and the latch clears while
//!    the standing alarm remains asserted until the level returns.
//! 7. **Recovery** — clearing the fault restores the feedback read; the
//!    permissive proves `Good` again and the interlock auto-resets (it
//!    holds no latch), the motor fault clears on the first agreeing
//!    scan, and the level climbs back through the alarm's deadband to
//!    40%. The run then pulses the totalizer's and the trip counter's
//!    writable resets — the accumulated flow drops to zero and the
//!    counter's count and `done` clear, rearmed.
//!
//! Everything is tick-domain — the executor's virtual ticks, the
//! driver's logical tick per [`FanoutDriver::step`], and the pid's `dt`
//! tuned to [`SCAN_PERIOD`] — so identical runs produce identical
//! snapshots.

use dcs_assembly::{
    AssemblyError, DriverRegistry, FanoutDriver, StepError, assemble, resolve_drivers,
};
use dcs_core::{
    Command, CommandReceipt, IoDriver, IoError, PointId, Sample, TelemetrySnapshot, Value,
    ValueKind,
};
use dcs_model::{LoadError, PlantModel};
use dcs_runtime::Executor;
use dcs_sim::{Fault, ProcessElement};
use std::fmt;

/// The checked-in showcase model document: the pump-and-tank line this
/// module runs.
pub const SHOWCASE_DOCUMENT: &str = include_str!("../fixtures/showcase.json");

/// The checked-in dynamics document — the same file
/// `dcs-plant-server --dynamics` loads — declaring the tank's
/// second-order lag, the two transmitter lags, and the flow lag.
/// [`driver`] merges it into the in-process channel map so the scripted
/// run and the served plant share one physics description.
pub const SHOWCASE_DYNAMICS_DOCUMENT: &str = include_str!("../fixtures/showcase_dynamics.json");

/// Simulated process time each scan advances, in the process's time
/// units — the `dt` passed to [`FanoutDriver::step`] and the period the
/// pid's `dt` parameter is tuned for.
pub const SCAN_PERIOD: f64 = 0.1;

/// The level setpoint the model's writable internal setpoint point is
/// declared with, in percent — the value the run settles on first.
pub const INITIAL_SETPOINT: f64 = 60.0;

/// The setpoint the run's operator command writes at the documented
/// boundary, in percent.
pub const MOVED_SETPOINT: f64 = 40.0;

/// The manual position the operator writes the valve station, in
/// percent — above the regulating command so the excursion is visible
/// without winding the integrator deep into a recovery dip.
pub const MANUAL_POSITION: f64 = 52.0;

/// The raw value the deviant-sensor phase writes `lt101c_raw` — 20 mA,
/// a full-scale stuck reading while the good pair sits near 10.4 mA.
pub const DEVIANT_RAW: f64 = 20.0;

/// The pid's tuned `kp` — the mid-batch `SetParameter` retune. The
/// batch table's second up-step is identical to its first, so the level
/// trace's two peaks bracket the retune: the second overshoots visibly
/// more on the underdamped tank. The run restores `BASE_KP` when the
/// batch exits.
pub const RETUNED_KP: f64 = 2.0;

/// The pid's declared `kp` — restored by `SetParameter` as the batch
/// exits so the rest of the run regulates on the operator's tune.
pub const BASE_KP: f64 = 1.0;

/// Scans the settle phase holds the initial setpoint: the pump start
/// delay, the permissive's conditioning, the dip while the interlock
/// holds the valve shut, and the loop's recovery and convergence all fit
/// inside it.
pub const SETTLE_SCANS: u64 = 200;

/// Scans after the setpoint command: the rate limiter's ramp plus
/// reconvergence at the moved setpoint.
pub const MOVED_SCANS: u64 = 200;

/// Batch scans before the retune command — the table's first two steps
/// plus most of the step-3 hold, so the retune lands between the
/// recipe's two identical up-steps while the level is settled.
pub const BATCH_PRE_TUNE_SCANS: u64 = 200;

/// Batch scans after the retune — the rest of the step-3 hold, the
/// second up-step, and the final hold completing the table.
pub const BATCH_POST_TUNE_SCANS: u64 = 200;

/// The batch table's total scans: steps 40+80+120+80+80, the
/// sequencer's declared recipe.
pub const BATCH_SCANS: u64 = BATCH_PRE_TUNE_SCANS + BATCH_POST_TUNE_SCANS;

/// Scans the batch's `reset` is held — a one-scan pulse — plus the
/// release scan, then [`COOLDOWN_SCANS`] while the loop returns to the
/// operator setpoint.
pub const BATCH_EXIT_SCANS: u64 = 2;

/// Scans after the batch exits for the loop to re-settle before the
/// deviant phase — the table ends at the operator setpoint, so this is
/// short.
pub const COOLDOWN_SCANS: u64 = 60;

/// Scans the deviant phase holds `lt101c_raw` at its stuck value.
pub const DEVIANT_SCANS: u64 = 40;

/// Scans after the deviant leg is released for its lag to reconverge
/// and the voter's discrepancy to clear.
pub const AGREE_SCANS: u64 = 40;

/// Scans into the manual transfer the mid-slew snapshot lands on — the
/// station is partway from the control value to [`MANUAL_POSITION`].
pub const MANUAL_TRANSFER_SCANS: u64 = 3;

/// Scans the station sits in manual after the slew completes — the
/// tank's level follows the manually-positioned valve.
pub const MANUAL_HOLD_SCANS: u64 = 47;

/// Scans after the mode returns to auto for the loop to re-settle at
/// the operator setpoint — the excursion wound the integrator down, so
/// the return dips before it recovers.
pub const MANUAL_RETURN_SCANS: u64 = 140;

/// Scans under the injected fault before the ack command: the interlock
/// trip and motor fault land in the first few, and the level drains past
/// the low alarm limit well inside the window, so `LAL-102`'s latch is
/// standing when the ack arrives.
pub const FAULT_PRE_ACK_SCANS: u64 = 80;

/// Scans under the fault after the ack pulse — the latch stays cleared
/// while the standing alarm remains asserted.
pub const FAULT_POST_ACK_SCANS: u64 = 68;

/// Total scans under the injected fault: the pre-ack window, the two
/// command-boundary scans the ack pulse takes, and the post-ack window.
pub const FAULT_SCANS: u64 = FAULT_PRE_ACK_SCANS + 2 + FAULT_POST_ACK_SCANS;

/// Scans after the fault clears — interlock auto-reset, recovery through
/// the alarm deadband — including the command-boundary scans the
/// totalizer and trip-counter resets take. The refill from a dry tank
/// overshoots on the underdamped process and rings back, so the window
/// is long.
pub const RECOVERY_SCANS: u64 = 244;

/// Total scans a [`run`] drives: the run's documented length.
pub const TOTAL_SCANS: u64 = SETTLE_SCANS
    + MOVED_SCANS
    + BATCH_SCANS
    + BATCH_EXIT_SCANS
    + COOLDOWN_SCANS
    + DEVIANT_SCANS
    + AGREE_SCANS
    + MANUAL_TRANSFER_SCANS
    + MANUAL_HOLD_SCANS
    + MANUAL_RETURN_SCANS
    + FAULT_SCANS
    + RECOVERY_SCANS;

/// The tick the settle phase ends on — the `settled` snapshot's tick.
pub const SETTLED_TICK: u64 = SETTLE_SCANS;
/// The tick the moved-setpoint phase ends on.
pub const MOVED_TICK: u64 = SETTLED_TICK + MOVED_SCANS;
/// The tick the batch table completes on — `done` asserts.
pub const BATCHED_TICK: u64 = MOVED_TICK + BATCH_SCANS;
/// The tick the deviant window ends on.
pub const DEVIATED_TICK: u64 = BATCHED_TICK + BATCH_EXIT_SCANS + COOLDOWN_SCANS + DEVIANT_SCANS;
/// The tick the reconverged window ends on.
pub const AGREED_TICK: u64 = DEVIATED_TICK + AGREE_SCANS;
/// The mid-slew snapshot's tick.
pub const MANUAL_ENGAGED_TICK: u64 = AGREED_TICK + MANUAL_TRANSFER_SCANS;
/// The tick the manual excursion ends on.
pub const MANUAL_TICK: u64 = MANUAL_ENGAGED_TICK + MANUAL_HOLD_SCANS + MANUAL_RETURN_SCANS;
/// The mid-fault snapshot's tick — the standing latch.
pub const TRIPPED_TICK: u64 = MANUAL_TICK + FAULT_PRE_ACK_SCANS;
/// The post-ack snapshot's tick — the cleared latch under the standing
/// alarm.
pub const ACKED_TICK: u64 = TRIPPED_TICK + 2;
/// The tick the faulted phase ends on.
pub const FAULTED_TICK: u64 = ACKED_TICK + FAULT_POST_ACK_SCANS;

/// The batch table's step lengths in ticks — the sequencer's declared
/// recipe: hold, fill, long hold, fill, hold.
pub const BATCH_STEP1_TICKS: u64 = 40;
/// Step 2's fill-and-hold — the pre-tune up-step.
pub const BATCH_STEP2_TICKS: u64 = 80;
/// Step 3's hold — long enough to re-settle before the second fill.
pub const BATCH_STEP3_TICKS: u64 = 120;
/// Step 4's fill-and-hold — the post-tune up-step.
pub const BATCH_STEP4_TICKS: u64 = 80;

/// The showcase model's point ids — the field points, the writable
/// operator points, and the declared internal carriers — so callers and
/// tests name them instead of repeating the numbers.
pub mod points {
    use dcs_core::PointId;

    /// `LT-101A` raw level input, mA — the tank lag's output point.
    pub const LEVEL_RAW: PointId = PointId(10);
    /// `LY-101` raw valve-position feedback input, mA — field-wired to
    /// follow `VALVE_RAW`.
    pub const VALVE_FEEDBACK_RAW: PointId = PointId(11);
    /// `LT-101B` raw level input, mA — the first redundant leg's
    /// transmitter lag.
    pub const LEVEL_B_RAW: PointId = PointId(12);
    /// `LT-101C` raw level input, mA — the second redundant leg's
    /// transmitter lag; the deviant phase's stuck reading lands here.
    pub const LEVEL_C_RAW: PointId = PointId(13);
    /// `FT-101` raw inlet flow input, mA — the flow lag's output point.
    pub const FLOW_RAW: PointId = PointId(14);
    /// `LV-101` raw valve command output, mA — the tank lag's input
    /// point.
    pub const VALVE_RAW: PointId = PointId(20);
    /// `P-101` run feedback input — the fault the documented run injects
    /// lands here.
    pub const PUMP_RUN: PointId = PointId(30);
    /// `LSH-101` high-high level switch input — the interlock's trip.
    pub const HIGH_SWITCH: PointId = PointId(31);
    /// `P-101` starter command output.
    pub const PUMP_COMMAND: PointId = PointId(40);
    /// `LA-101` horn output.
    pub const HORN: PointId = PointId(41);
    /// `LAL-102` unacknowledged-alarm beacon lamp output.
    pub const BEACON: PointId = PointId(42);
    /// `LIC-101` level setpoint, % — writable internal point; the run's
    /// setpoint command lands here.
    pub const LEVEL_SETPOINT: PointId = PointId(50);
    /// `P-101` operator start request — writable internal point.
    pub const PUMP_START: PointId = PointId(51);
    /// `HIC-101` manual valve position, % — writable internal point the
    /// manual station slews to.
    pub const VALVE_MANUAL: PointId = PointId(52);
    /// `HIC-101` manual/auto mode select — writable internal point.
    pub const VALVE_MANUAL_SELECT: PointId = PointId(53);
    /// `I-101` trip counter reset — writable internal point the run's
    /// post-trip reset lands on.
    pub const TRIP_COUNT_RESET: PointId = PointId(54);
    /// `B-101` batch sequence run request — writable internal point.
    pub const BATCH_RUN: PointId = PointId(55);
    /// `B-101` batch sequence reset — writable internal point.
    pub const BATCH_RESET: PointId = PointId(56);
    /// `B-101` program select — writable internal point switching the
    /// pid's setpoint between the batch program and the operator's.
    pub const BATCH_MODE: PointId = PointId(57);
    /// `LAL-102` acknowledgment — writable internal `In` point; the
    /// ack lands through the ordinary `WriteValue` command path per
    /// decision 33.
    pub const ALARM_ACK: PointId = PointId(58);
    /// `FQ-101` totalizer reset — writable internal point.
    pub const TOTAL_RESET: PointId = PointId(59);
    /// Median-voted tank level, % — internal carrier `LY-101`'s
    /// `median-voter` publishes and the filter consumes through a
    /// declared link.
    pub const LEVEL_PERCENT: PointId = PointId(60);
    /// The filtered level carrier's pid-side copy.
    pub const LEVEL_FOR_PID: PointId = PointId(61);
    /// The filtered level carrier's alarm-side copy.
    pub const LEVEL_FOR_ALARM: PointId = PointId(62);
    /// `P-101` conditioned run status — the internal carrier the
    /// `digital-input` publishes.
    pub const PUMP_RUNNING: PointId = PointId(63);
    /// The run-status carrier's motor-side copy.
    pub const RUN_FOR_MOTOR: PointId = PointId(64);
    /// The run-status carrier's interlock-side copy — the permissive.
    pub const RUN_FOR_PERMISSIVE: PointId = PointId(65);
    /// `I-101` tripped status.
    pub const INTERLOCK_TRIPPED: PointId = PointId(66);
    /// The tripped carrier's counter-side copy.
    pub const TRIP_PULSE: PointId = PointId(67);
    /// `LA-101` alarm status.
    pub const LEVEL_ALARM: PointId = PointId(68);
    /// The alarm carrier's horn-side copy.
    pub const HORN_COMMAND: PointId = PointId(69);
    /// `P-101` run-feedback fault status.
    pub const PUMP_FAULT: PointId = PointId(70);
    /// `LV-101` position discrepancy status.
    pub const VALVE_DISCREPANCY: PointId = PointId(71);
    /// `I-101` cumulative trip count.
    pub const TRIP_COUNT: PointId = PointId(72);
    /// `I-101` trip-count-at-preset latch.
    pub const TRIPS_DONE: PointId = PointId(73);
    /// `LY-101` transmitter-spread discrepancy status.
    pub const VOTER_DISCREPANCY: PointId = PointId(74);
    /// The voted level carrier's filter-side copy.
    pub const LEVEL_FOR_FILTER: PointId = PointId(75);
    /// `LIF-101` filtered level, % — the carrier the pid and both alarms
    /// consume through declared links.
    pub const LEVEL_FILTERED: PointId = PointId(76);
    /// The filtered level carrier's latching-alarm-side copy.
    pub const LEVEL_FOR_LATCH: PointId = PointId(77);
    /// `LAL-102` latched alarm status.
    pub const LATCH_ALARM: PointId = PointId(78);
    /// `LAL-102` unacknowledged-trip latch.
    pub const LATCH_UNACKNOWLEDGED: PointId = PointId(79);
    /// The unacknowledged carrier's lamp-side copy.
    pub const BEACON_COMMAND: PointId = PointId(80);
    /// `B-101` driven setpoint, % — the sequencer's program carrier.
    pub const BATCH_PROGRAM: PointId = PointId(81);
    /// The program carrier's select-side copy.
    pub const PROGRAM_FOR_SELECT: PointId = PointId(82);
    /// `B-101` active step number.
    pub const BATCH_STEP: PointId = PointId(83);
    /// `B-101` table-complete status.
    pub const BATCH_DONE: PointId = PointId(84);
    /// `HIC-101` manual-mode status.
    pub const STATION_MANUAL: PointId = PointId(85);
    /// `FT-101` scaled inlet flow, % — the carrier the totalizer
    /// consumes through a declared link.
    pub const FLOW_PERCENT: PointId = PointId(86);
    /// The flow carrier's totalizer-side copy.
    pub const FLOW_FOR_TOTAL: PointId = PointId(87);
    /// `FQ-101` accumulated inlet flow.
    pub const FLOW_TOTAL: PointId = PointId(88);
}

/// Why loading, assembling, or running the showcase failed.
#[derive(Debug)]
pub enum ShowcaseError {
    /// The model document failed [`PlantModel::load`].
    Load(LoadError),
    /// The dynamics document failed to parse — unreachable for the
    /// checked-in document; the variant keeps `?` honest for callers
    /// building a driver from their own source.
    Dynamics(serde_json::Error),
    /// Device resolution, component construction, or wiring failed.
    Assembly(AssemblyError),
    /// The driver failed to step the simulated plant.
    Step(StepError),
    /// A driver access — fault injection, a point write, or a point
    /// read — failed.
    Io(IoError),
}

impl fmt::Display for ShowcaseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Load(error) => write!(f, "{error}"),
            Self::Dynamics(error) => write!(f, "dynamics document failed to parse: {error}"),
            Self::Assembly(error) => write!(f, "{error}"),
            Self::Step(error) => write!(f, "plant step failed: {error}"),
            Self::Io(error) => write!(f, "{error}"),
        }
    }
}

impl std::error::Error for ShowcaseError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Load(error) => Some(error),
            Self::Dynamics(error) => Some(error),
            Self::Assembly(error) => Some(error),
            Self::Step(error) => Some(error),
            Self::Io(error) => Some(error),
        }
    }
}

impl From<LoadError> for ShowcaseError {
    fn from(error: LoadError) -> Self {
        Self::Load(error)
    }
}

impl From<AssemblyError> for ShowcaseError {
    fn from(error: AssemblyError) -> Self {
        Self::Assembly(error)
    }
}

impl From<StepError> for ShowcaseError {
    fn from(error: StepError) -> Self {
        Self::Step(error)
    }
}

impl From<IoError> for ShowcaseError {
    fn from(error: IoError) -> Self {
        Self::Io(error)
    }
}

/// Builds the showcase's field driver: the model's `sim*` devices
/// resolved through [`DriverRegistry::standard`], with the simulated
/// plant — [`SHOWCASE_DYNAMICS_DOCUMENT`]'s process elements, the same
/// declarations `dcs-plant-server --dynamics` loads — merged into the
/// still-open local channel map before
/// [`DriverPlan::build`](dcs_assembly::DriverPlan::build) finishes the
/// [`FanoutDriver`]. The field loopbacks — run feedback and valve
/// position — are model-declared `point → point` wires and arrive with
/// the resolved map.
///
/// Assemble the returned driver through the standard component registry
/// — `dcs_controller::registry()` — with [`assemble`].
pub fn driver(model: &PlantModel) -> Result<FanoutDriver, ShowcaseError> {
    let mut plan = resolve_drivers(model, &DriverRegistry::standard())?;
    let elements: Vec<ProcessElement> =
        serde_json::from_str(SHOWCASE_DYNAMICS_DOCUMENT).map_err(ShowcaseError::Dynamics)?;
    for element in elements {
        plan.sim_map = plan.sim_map.with_element(element);
    }
    Ok(plan.build()?)
}

/// A finished showcase run: the per-scan level trace, the boundary
/// snapshots, the batch's per-scan step trace, and the command receipts
/// — everything the documented behavior asserts against.
#[derive(Debug, Clone, PartialEq)]
pub struct Run {
    /// The median-voted tank level after each scan, in percent — one
    /// entry per scan, in scan order, across all seven phases. The
    /// batch's two identical up-steps bracket the mid-table retune:
    /// their peaks in [`BATCH_STEP2_TICKS`]' and [`BATCH_STEP4_TICKS`]'s
    /// windows show the tuned overshoot.
    pub levels: Vec<f64>,
    /// The raw valve command after each scan, in mA — the value the
    /// field carries; the manual-transfer phase's mid-slew snapshot and
    /// the fault's safe-value drive read it here.
    pub valve_raws: Vec<f64>,
    /// The sequencer's `step` output after each batch scan — the
    /// table's advance trace: step 1 through 5 in order,
    /// 40+80+120+80+80 scans.
    pub batch_steps: Vec<i64>,
    /// The snapshot at the end of the settle phase: the loop regulating
    /// at [`INITIAL_SETPOINT`]%, with the start-delay trip already
    /// counted and the totalizer accumulating.
    pub settled: TelemetrySnapshot,
    /// The snapshot at the end of the moved-setpoint phase: the command
    /// applied at its boundary and the loop re-settled at
    /// [`MOVED_SETPOINT`]%.
    pub moved: TelemetrySnapshot,
    /// The snapshot at the end of the batch: the table complete —
    /// `step` 5, `done` asserted.
    pub batched: TelemetrySnapshot,
    /// The snapshot at the end of the deviant window: `LT-101C` stuck at
    /// full scale, the voter's `discrepancy` asserted, the voted level
    /// still on the good pair.
    pub deviated: TelemetrySnapshot,
    /// The snapshot [`MANUAL_TRANSFER_SCANS`] into the manual transfer:
    /// `manual_active` asserted, the valve command mid-slew between the
    /// control value and [`MANUAL_POSITION`].
    pub manual_engaged: TelemetrySnapshot,
    /// The snapshot at the end of the manual excursion: the station back
    /// in auto, the loop re-settled at the operator setpoint.
    pub manual: TelemetrySnapshot,
    /// The snapshot mid-fault before the ack: the documented interlock
    /// trip, motor fault, asserted alarms — `LAL-102`'s latch standing
    /// and the beacon lamp lit.
    pub tripped: TelemetrySnapshot,
    /// The snapshot after the ack pulse, still under the fault:
    /// `LAL-102`'s latch cleared — `unacknowledged` off, the beacon
    /// dark — while the standing alarm remains asserted.
    pub acked: TelemetrySnapshot,
    /// The snapshot at the end of the faulted phase.
    pub faulted: TelemetrySnapshot,
    /// The snapshot at the end of the recovery phase — also the run's
    /// final snapshot: auto-reset interlock, cleared alarms and motor
    /// fault, level back at the moved setpoint, totalizer and trip
    /// counter reset and rearmed.
    pub recovered: TelemetrySnapshot,
    /// The run's command receipts as submission returned them — the
    /// setpoint move, the batch's mode/run/reset writes, the retune, the
    /// station's position and mode writes, the ack pulse, and the
    /// resets — each `Accepted` with the documented apply tick.
    pub receipts: Vec<CommandReceipt>,
}

/// One scan: `scan`, step the plant, record the level and the raw
/// valve command — the fixed-step cycle every phase shares.
fn scan(
    executor: &mut Executor<'_>,
    driver: &FanoutDriver,
    levels: &mut Vec<f64>,
    valve_raws: &mut Vec<f64>,
) -> Result<(), ShowcaseError> {
    executor.scan();
    driver.step(SCAN_PERIOD)?;
    let value = executor
        .sample(points::LEVEL_PERCENT)
        .map(|sample| sample.value)
        .expect("the level carrier is written every scan");
    let Value::Float(level) = value else {
        unreachable!("the level carrier is a Float point")
    };
    levels.push(level);
    let value = executor
        .sample(points::VALVE_RAW)
        .map(|sample| sample.value)
        .expect("the valve command is written every scan");
    let Value::Float(raw) = value else {
        unreachable!("the valve command is a Float point")
    };
    valve_raws.push(raw);
    Ok(())
}

/// Runs `count` scans.
fn scans(
    executor: &mut Executor<'_>,
    driver: &FanoutDriver,
    count: u64,
    levels: &mut Vec<f64>,
    valve_raws: &mut Vec<f64>,
) -> Result<(), ShowcaseError> {
    for _ in 0..count {
        scan(executor, driver, levels, valve_raws)?;
    }
    Ok(())
}

/// The batch phase's scans: like [`scans`], plus recording the
/// sequencer's step output — the advance trace the assertions read.
fn batch_scans(
    executor: &mut Executor<'_>,
    driver: &FanoutDriver,
    count: u64,
    levels: &mut Vec<f64>,
    valve_raws: &mut Vec<f64>,
    batch_steps: &mut Vec<i64>,
) -> Result<(), ShowcaseError> {
    for _ in 0..count {
        scan(executor, driver, levels, valve_raws)?;
        let value = executor
            .sample(points::BATCH_STEP)
            .map(|sample| sample.value)
            .expect("the step carrier is written every scan");
        let Value::Int(step) = value else {
            unreachable!("the step carrier is an Int point")
        };
        batch_steps.push(step);
    }
    Ok(())
}

/// The deviant phase's scans: like [`scans`], except each scan first
/// overwrites `lt101c_raw` with the stuck reading — the deviant
/// transmitter the voter's discrepancy reports. The write lands on the
/// channel before the scan reads inputs; the lag's next step drives the
/// point back toward the real level, so the hold rewrites every scan.
fn deviant_scans(
    executor: &mut Executor<'_>,
    driver: &FanoutDriver,
    sim: &dcs_sim::SimDriver,
    count: u64,
    levels: &mut Vec<f64>,
    valve_raws: &mut Vec<f64>,
) -> Result<(), ShowcaseError> {
    for _ in 0..count {
        sim.write(points::LEVEL_C_RAW, Value::Float(DEVIANT_RAW))?;
        scan(executor, driver, levels, valve_raws)?;
    }
    Ok(())
}

/// Loads `source` as a plant model, builds the field driver through the
/// standard driver registry, assembles every component through the
/// standard component registry — `dcs_controller::registry()`, the set
/// `dcs-controller` deploys — with no manual wiring, and runs the
/// documented seven-phase scenario of [`TOTAL_SCANS`] scans.
///
/// The run is fully deterministic: driver and executor are tick-domain,
/// so two calls with the same `source` return equal [`Run`]s.
pub fn run(source: &str) -> Result<Run, ShowcaseError> {
    let model = PlantModel::load(source)?;
    let driver = driver(&model)?;
    let mut executor = assemble(&model, &dcs_controller::registry(), &driver)?;
    let mut levels = Vec::with_capacity(TOTAL_SCANS as usize);
    let mut valve_raws = Vec::with_capacity(TOTAL_SCANS as usize);
    let mut batch_steps = Vec::with_capacity(BATCH_SCANS as usize);
    let mut receipts = Vec::new();

    // Phase 1 — settle at the declared setpoint. The pump's timer holds
    // its start back two scans and the run loopback plus debounce take
    // two more, so the interlock holds the valve shut until the
    // permissive proves — the counter's first trip. The level dips while
    // the loop is held open, then recovers and settles at 60%; the
    // totalizer accumulates the inlet flow from the first open-valve
    // scan.
    scans(
        &mut executor,
        &driver,
        SETTLE_SCANS,
        &mut levels,
        &mut valve_raws,
    )?;
    let settled = executor.snapshot();

    // Phase 2 — the operator's setpoint command, queued between scans so
    // it applies at the next scan's head: the receipt's documented
    // apply_tick is `SETTLE_SCANS + 1`. The rate limiter ramps the pid's
    // setpoint at 5%/scan and the loop re-settles at 40%.
    receipts.push(executor.submit_command(Command::WriteValue {
        point: points::LEVEL_SETPOINT,
        kind: ValueKind::Float,
        value: Value::Float(MOVED_SETPOINT),
    }));
    scans(
        &mut executor,
        &driver,
        MOVED_SCANS,
        &mut levels,
        &mut valve_raws,
    )?;
    let moved = executor.snapshot();

    // Phase 3 — the batch: the operator writes the program select so the
    // sequencer's output — not the operator's rate-limited setpoint —
    // drives the pid, then raises `run`. The table walks the setpoint
    // through hold–fill–hold–fill–hold and asserts `done` on the last
    // scan. Mid-table the run retunes the pid's `kp` through the
    // ordinary parameter path, so the recipe's two identical up-steps
    // bracket the tune and the trace's second peak visibly overshoots
    // the first on the underdamped tank.
    receipts.push(executor.submit_command(Command::WriteValue {
        point: points::BATCH_MODE,
        kind: ValueKind::Bool,
        value: Value::Bool(true),
    }));
    receipts.push(executor.submit_command(Command::WriteValue {
        point: points::BATCH_RUN,
        kind: ValueKind::Bool,
        value: Value::Bool(true),
    }));
    batch_scans(
        &mut executor,
        &driver,
        BATCH_PRE_TUNE_SCANS,
        &mut levels,
        &mut valve_raws,
        &mut batch_steps,
    )?;
    receipts.push(executor.submit_command(Command::SetParameter {
        component: "pid:7".to_string(),
        name: "kp".to_string(),
        value: Value::Float(RETUNED_KP),
    }));
    batch_scans(
        &mut executor,
        &driver,
        BATCH_POST_TUNE_SCANS,
        &mut levels,
        &mut valve_raws,
        &mut batch_steps,
    )?;
    let batched = executor.snapshot();

    // The batch exits: the program select and `run` drop, `reset` pulses
    // the sequencer back to its first step, and a short cooldown lets
    // the loop settle back at the operator setpoint the table ended on.
    receipts.push(executor.submit_command(Command::WriteValue {
        point: points::BATCH_MODE,
        kind: ValueKind::Bool,
        value: Value::Bool(false),
    }));
    receipts.push(executor.submit_command(Command::WriteValue {
        point: points::BATCH_RUN,
        kind: ValueKind::Bool,
        value: Value::Bool(false),
    }));
    receipts.push(executor.submit_command(Command::WriteValue {
        point: points::BATCH_RESET,
        kind: ValueKind::Bool,
        value: Value::Bool(true),
    }));
    // The retune's demonstration is done — restore the declared `kp`
    // through the same parameter path so the rest of the run regulates
    // on the operator's tune.
    receipts.push(executor.submit_command(Command::SetParameter {
        component: "pid:7".to_string(),
        name: "kp".to_string(),
        value: Value::Float(BASE_KP),
    }));
    scans(&mut executor, &driver, 1, &mut levels, &mut valve_raws)?;
    receipts.push(executor.submit_command(Command::WriteValue {
        point: points::BATCH_RESET,
        kind: ValueKind::Bool,
        value: Value::Bool(false),
    }));
    scans(&mut executor, &driver, 1, &mut levels, &mut valve_raws)?;
    scans(
        &mut executor,
        &driver,
        COOLDOWN_SCANS,
        &mut levels,
        &mut valve_raws,
    )?;

    // Phase 4 — the deviant transmitter: `lt101c_raw` held at full scale
    // while the good pair sits near the regulating level. The voter's
    // median keeps the loop on the good pair — the level trace shows no
    // excursion — and `discrepancy` reports the spread above tolerance.
    // Released, the lagged leg reconverges and the discrepancy clears.
    let sim = driver
        .sim()
        .expect("the showcase's field points are all sim-served");
    deviant_scans(
        &mut executor,
        &driver,
        sim,
        DEVIANT_SCANS,
        &mut levels,
        &mut valve_raws,
    )?;
    let deviated = executor.snapshot();
    scans(
        &mut executor,
        &driver,
        AGREE_SCANS,
        &mut levels,
        &mut valve_raws,
    )?;

    // Phase 5 — the manual transfer: the operator writes the station's
    // manual position and mode select, and `HIC-101` slews the valve
    // command toward the manual source at `transfer_delta` per scan —
    // the mid-slew snapshot catches the command between the two sources
    // with `manual_active` asserted. The excursion lifts the level;
    // dropping the mode slews back to the controller's output and the
    // loop re-settles at the operator setpoint.
    receipts.push(executor.submit_command(Command::WriteValue {
        point: points::VALVE_MANUAL,
        kind: ValueKind::Float,
        value: Value::Float(MANUAL_POSITION),
    }));
    receipts.push(executor.submit_command(Command::WriteValue {
        point: points::VALVE_MANUAL_SELECT,
        kind: ValueKind::Bool,
        value: Value::Bool(true),
    }));
    scans(
        &mut executor,
        &driver,
        MANUAL_TRANSFER_SCANS,
        &mut levels,
        &mut valve_raws,
    )?;
    let manual_engaged = executor.snapshot();
    scans(
        &mut executor,
        &driver,
        MANUAL_HOLD_SCANS,
        &mut levels,
        &mut valve_raws,
    )?;
    receipts.push(executor.submit_command(Command::WriteValue {
        point: points::VALVE_MANUAL_SELECT,
        kind: ValueKind::Bool,
        value: Value::Bool(false),
    }));
    scans(
        &mut executor,
        &driver,
        MANUAL_RETURN_SCANS,
        &mut levels,
        &mut valve_raws,
    )?;
    let manual = executor.snapshot();

    // Phase 6 — the injected input fault: `p101_run` disconnected. The
    // executor keeps the last value marked `Bad(CommunicationFault)`;
    // the conditioned run status goes bad with it, the interlock trips
    // on the bad permissive and drives the valve to `safe_value`, and
    // the level drains past the low alarm limit — `LA-101` asserts and
    // the horn follows while `LAL-102` latches the trip: alarm on,
    // `unacknowledged` on, the beacon lamp lit. The trip edge is the
    // counter's second, landing it on its preset: `done` latches. The
    // motor's run-feedback check cannot prove agreement and flags its
    // fault after `fault_ticks`.
    //
    // Mid-fault the operator acknowledges through the ordinary command
    // path — decision 33: `ALARM_ACK` is a writable internal `In` point
    // and the `WriteValue` is receipted and applied like any other
    // write. The latch clears — `unacknowledged` and the beacon drop —
    // while the standing alarm remains asserted.
    sim.inject_fault(points::PUMP_RUN, Fault::Disconnected)?;
    scans(
        &mut executor,
        &driver,
        FAULT_PRE_ACK_SCANS,
        &mut levels,
        &mut valve_raws,
    )?;
    let tripped = executor.snapshot();
    receipts.push(executor.submit_command(Command::WriteValue {
        point: points::ALARM_ACK,
        kind: ValueKind::Bool,
        value: Value::Bool(true),
    }));
    scans(&mut executor, &driver, 1, &mut levels, &mut valve_raws)?;
    receipts.push(executor.submit_command(Command::WriteValue {
        point: points::ALARM_ACK,
        kind: ValueKind::Bool,
        value: Value::Bool(false),
    }));
    scans(&mut executor, &driver, 1, &mut levels, &mut valve_raws)?;
    let acked = executor.snapshot();
    scans(
        &mut executor,
        &driver,
        FAULT_POST_ACK_SCANS,
        &mut levels,
        &mut valve_raws,
    )?;
    let faulted = executor.snapshot();

    // Phase 7 — recovery: clearing the fault restores the feedback read,
    // the permissive proves Good again, and the interlock auto-resets —
    // it holds no latch — so the loop reopens the valve and the level
    // climbs back through the alarm's deadband to the moved setpoint.
    // Then the operator's housekeeping: the totalizer's writable reset
    // drops the accumulated flow to zero, and the counter's writable
    // reset clears the latched count and `done`, rearming it.
    sim.clear_fault(points::PUMP_RUN)?;
    scans(
        &mut executor,
        &driver,
        RECOVERY_SCANS - 4,
        &mut levels,
        &mut valve_raws,
    )?;
    receipts.push(executor.submit_command(Command::WriteValue {
        point: points::TOTAL_RESET,
        kind: ValueKind::Bool,
        value: Value::Bool(true),
    }));
    scans(&mut executor, &driver, 1, &mut levels, &mut valve_raws)?;
    receipts.push(executor.submit_command(Command::WriteValue {
        point: points::TOTAL_RESET,
        kind: ValueKind::Bool,
        value: Value::Bool(false),
    }));
    scans(&mut executor, &driver, 1, &mut levels, &mut valve_raws)?;
    receipts.push(executor.submit_command(Command::WriteValue {
        point: points::TRIP_COUNT_RESET,
        kind: ValueKind::Bool,
        value: Value::Bool(true),
    }));
    scans(&mut executor, &driver, 1, &mut levels, &mut valve_raws)?;
    receipts.push(executor.submit_command(Command::WriteValue {
        point: points::TRIP_COUNT_RESET,
        kind: ValueKind::Bool,
        value: Value::Bool(false),
    }));
    scans(&mut executor, &driver, 1, &mut levels, &mut valve_raws)?;
    let recovered = executor.snapshot();

    Ok(Run {
        levels,
        valve_raws,
        batch_steps,
        settled,
        moved,
        batched,
        deviated,
        manual_engaged,
        manual,
        tripped,
        acked,
        faulted,
        recovered,
        receipts,
    })
}

/// The snapshot's latest sample for `point`, if a scan produced one —
/// the reading a phase assertion inspects.
pub fn sample(snapshot: &TelemetrySnapshot, point: PointId) -> Option<Sample> {
    snapshot
        .points
        .iter()
        .find(|telemetry| telemetry.point == point)
        .and_then(|telemetry| telemetry.sample)
}
