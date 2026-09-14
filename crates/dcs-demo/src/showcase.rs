//! The showcase plant: a pump-and-tank line exercising the whole
//! `dcs-blocks` library through the standard model-driven assembly path —
//! the plant the monitoring UI demos against.
//!
//! `fixtures/showcase.json` declares the line:
//!
//! ```text
//!            P-101                LV-101
//!  supply ──▶ pump ──▶ inlet valve ──▶ T-101 tank ──▶ drain
//! ```
//!
//! The tank's level loop runs `LT-101` (`analog-input`) → `LIC-101`
//! (`pid`, setpoint ramped by a `rate-limiter`) → `I-101` (`interlock`)
//! → `LY-101` (`override-select`) → `LV-101` (`valve`) → `analog-output`
//! → the field, with the valve's position feedback reconditioned by a
//! second `analog-input`. `LA-101` (`alarm-monitor`) watches the level
//! and drives a horn through a `digital-output`; `LSH-101`'s high-high
//! switch, conditioned by a `digital-input`, is the interlock's trip
//! input. The pump's chain runs the operator's start request through a
//! `timer` start delay into a `motor` whose field loopback supplies run
//! feedback — conditioned by a `digital-input`, fanned out to the
//! motor's `run` port and the interlock's `permissive` — and a `counter`
//! records the interlock's trips. Every `dcs-blocks` kind is on the
//! sheet: channel blocks (`analog-input` ×2, `analog-output`,
//! `digital-input` ×2, `digital-output`), control (`pid`,
//! `rate-limiter`, `timer`, `counter`), actuators (`valve`, `motor`),
//! and safety (`interlock`, `alarm-monitor`, `override-select`).
//!
//! Operator-facing values are channel-less internal points declared
//! `writable` — the level setpoint, the pump start request, the valve's
//! manual position and select, and the trip counter's reset — so
//! `Command::WriteValue` submissions land at scan boundaries per the
//! command contract. Wiring uses every connection shape: `point → port`
//! bindings, `port → port` links (synthesized internal pairs), declared
//! internal `Out`/`In` carriers (fan-out of the conditioned level and
//! run status), and `point → point` wires — the field loopbacks making
//! the pump's run feedback follow its command and the valve's position
//! follow its command, plus the declared internal links driving the
//! carriers' consumers.
//!
//! The simulated plant lives in the driver, not the model — a
//! [`FirstOrderLag`] drives `lt101_raw` from `lv101_raw`, so the level
//! follows the valve's raw command with the tank's lag.
//!
//! # The documented run
//!
//! [`run`] drives the plant through four phases; every boundary is a
//! documented scan tick:
//!
//! 1. **Settle** — [`SETTLE_SCANS`] scans at the declared 60% setpoint.
//!    The pump's start delay holds its command back two scans, the run
//!    loopback and debounce take two more to prove the permissive, so the
//!    interlock holds the valve at `safe_value` for the first scans and
//!    registers that start-delay trip as the counter's first count. With
//!    the permissive proven the loop closes; the level dips while the
//!    valve is held shut, then recovers and settles at 60%.
//! 2. **Setpoint command** — at the boundary after settle the run
//!    submits `WriteValue` moving the setpoint to [`MOVED_SETPOINT`]%; the
//!    receipt reports `Accepted { apply_tick: SETTLE_SCANS + 1 }` and the
//!    queued value lands at that scan's head. The rate limiter ramps the
//!    pid's setpoint 5%/scan, and after [`MOVED_SCANS`] scans the level
//!    sits at 40%.
//! 3. **Injected fault** — [`Fault::Disconnected`] on `p101_run`: the
//!    field read fails, so the run feedback's image sample keeps its
//!    last value marked `Bad(CommunicationFault)`. The conditioned run
//!    status goes bad with it; the interlock trips on the bad
//!    permissive — its documented rule — and drives the valve to
//!    `safe_value` 0%, the motor's run-feedback check cannot prove
//!    agreement and asserts its `fault` after `fault_ticks` scans, and
//!    the level drains through the low alarm limit: `LA-101` asserts and
//!    the horn follows. The second trip edge brings the counter to its
//!    preset of two — `done` asserts, showing the latch the reset
//!    command clears.
//! 4. **Recovery** — clearing the fault restores the feedback read; the
//!    permissive proves `Good` again and the interlock auto-resets (it
//!    holds no latch), the motor fault clears on the first agreeing
//!    scan, and the level climbs back through the alarm's deadband to
//!    40%. The run then writes the trip counter's `reset` — count and
//!    `done` clear — and releases it, rearming the counter.
//!
//! Everything is tick-domain — the executor's virtual ticks, the
//! driver's logical tick per [`FanoutDriver::step`], and the pid's `dt`
//! tuned to [`SCAN_PERIOD`] — so identical runs produce identical
//! snapshots.

use dcs_assembly::{
    AssemblyError, DriverRegistry, FanoutDriver, StepError, assemble, resolve_drivers,
};
use dcs_core::{
    Command, CommandReceipt, IoError, PointId, Sample, TelemetrySnapshot, Value, ValueKind,
};
use dcs_model::{LoadError, PlantModel};
use dcs_runtime::{Executor, ScanError};
use dcs_sim::{Fault, FirstOrderLag, ProcessElement};
use std::fmt;

/// The checked-in showcase model document: the pump-and-tank line this
/// module runs.
pub const SHOWCASE_DOCUMENT: &str = include_str!("../fixtures/showcase.json");

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

/// Scans the settle phase holds the initial setpoint: the pump start
/// delay, the permissive's conditioning, the dip while the interlock
/// holds the valve shut, and the loop's recovery and convergence all fit
/// inside it.
pub const SETTLE_SCANS: u64 = 200;

/// Scans after the setpoint command: the rate limiter's ramp plus
/// reconvergence at the moved setpoint.
pub const MOVED_SCANS: u64 = 200;

/// Scans under the injected fault: the interlock trip and motor fault
/// land in the first few, and the level drains past the low alarm limit
/// well inside the phase.
pub const FAULT_SCANS: u64 = 150;

/// Scans after the fault clears — interlock auto-reset, recovery through
/// the alarm deadband — including the two command-boundary scans the
/// trip-counter reset takes.
pub const RECOVERY_SCANS: u64 = 200;

/// Total scans a [`run`] drives: the run's documented length.
pub const TOTAL_SCANS: u64 = SETTLE_SCANS + MOVED_SCANS + FAULT_SCANS + RECOVERY_SCANS;

/// The tank's lag: the level follows the valve's raw command with this
/// time constant, in the same time units as [`SCAN_PERIOD`].
const TANK_TIME_CONSTANT: f64 = 2.0;

/// The lag's initial output: the tank starts at its setpoint — 60% of
/// the 4–20 mA range is 13.6 mA — so the settle phase exercises the
/// dip-and-recovery transient, not a first fill.
const TANK_LEVEL_START_RAW: f64 = 13.6;

/// The showcase model's point ids — the field points, the writable
/// operator points, and the declared internal carriers — so callers and
/// tests name them instead of repeating the numbers.
pub mod points {
    use dcs_core::PointId;

    /// `LT-101` raw level input, mA — the lag's output point.
    pub const LEVEL_RAW: PointId = PointId(10);
    /// `LY-101` raw valve-position feedback input, mA — field-wired to
    /// follow `VALVE_RAW`.
    pub const VALVE_FEEDBACK_RAW: PointId = PointId(11);
    /// `LV-101` raw valve command output, mA — the lag's input point.
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
    /// `LIC-101` level setpoint, % — writable internal point; the run's
    /// setpoint command lands here.
    pub const LEVEL_SETPOINT: PointId = PointId(50);
    /// `P-101` operator start request — writable internal point.
    pub const PUMP_START: PointId = PointId(51);
    /// `LY-101` manual valve position, % — writable internal point the
    /// override select switches to.
    pub const VALVE_MANUAL: PointId = PointId(52);
    /// `LY-101` manual/auto select — writable internal point.
    pub const VALVE_MANUAL_SELECT: PointId = PointId(53);
    /// `I-101` trip counter reset — writable internal point the run's
    /// post-trip reset lands on.
    pub const TRIP_COUNT_RESET: PointId = PointId(54);
    /// Conditioned tank level, % — internal carrier `LT-101`'s
    /// `analog-input` publishes and the pid and alarm consume through
    /// declared links.
    pub const LEVEL_PERCENT: PointId = PointId(60);
    /// The level carrier's pid-side copy.
    pub const LEVEL_FOR_PID: PointId = PointId(61);
    /// The level carrier's alarm-side copy.
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
}

/// Why loading, assembling, or running the showcase failed.
#[derive(Debug)]
pub enum ShowcaseError {
    /// The model document failed [`PlantModel::load`].
    Load(LoadError),
    /// Device resolution, component construction, or wiring failed.
    Assembly(AssemblyError),
    /// A scan's output phase failed.
    Scan(ScanError),
    /// The driver failed to step the simulated plant.
    Step(StepError),
    /// A driver access — fault injection or a point read — failed.
    Io(IoError),
}

impl fmt::Display for ShowcaseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Load(error) => write!(f, "{error}"),
            Self::Assembly(error) => write!(f, "{error}"),
            Self::Scan(error) => write!(f, "{error}"),
            Self::Step(error) => write!(f, "plant step failed: {error}"),
            Self::Io(error) => write!(f, "{error}"),
        }
    }
}

impl std::error::Error for ShowcaseError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Load(error) => Some(error),
            Self::Assembly(error) => Some(error),
            Self::Scan(error) => Some(error),
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

impl From<ScanError> for ShowcaseError {
    fn from(error: ScanError) -> Self {
        Self::Scan(error)
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
/// plant — the tank's [`FirstOrderLag`] from the valve's raw command to
/// the level's raw input — added to the still-open local channel map
/// before [`DriverPlan::build`](dcs_assembly::DriverPlan::build) finishes
/// the [`FanoutDriver`]. The field loopbacks — run feedback and valve
/// position — are model-declared `point → point` wires and arrive with
/// the resolved map.
///
/// Assemble the returned driver through the standard component registry
/// — `dcs_controller::registry()` — with [`assemble`].
pub fn driver(model: &PlantModel) -> Result<FanoutDriver, AssemblyError> {
    let mut plan = resolve_drivers(model, &DriverRegistry::standard())?;
    plan.sim_map = plan
        .sim_map
        .with_element(ProcessElement::FirstOrderLag(FirstOrderLag {
            input: points::VALVE_RAW,
            output: points::LEVEL_RAW,
            time_constant: TANK_TIME_CONSTANT,
            initial: TANK_LEVEL_START_RAW,
        }));
    plan.build()
}

/// A finished showcase run: the per-scan level trace, the boundary
/// snapshots, and the command receipts — everything the documented
/// behavior asserts against.
#[derive(Debug, Clone, PartialEq)]
pub struct Run {
    /// The conditioned tank level after each scan, in percent — one
    /// entry per scan, in scan order, across all four phases.
    pub levels: Vec<f64>,
    /// The snapshot at the end of the settle phase: the loop regulating
    /// at [`INITIAL_SETPOINT`]%, with the start-delay trip already
    /// counted.
    pub settled: TelemetrySnapshot,
    /// The snapshot at the end of the moved-setpoint phase: the command
    /// applied at its boundary and the loop re-settled at
    /// [`MOVED_SETPOINT`]%.
    pub moved: TelemetrySnapshot,
    /// The snapshot at the end of the faulted phase: the documented
    /// interlock trip, motor fault, and asserted low alarm.
    pub faulted: TelemetrySnapshot,
    /// The snapshot at the end of the recovery phase — also the run's
    /// final snapshot: auto-reset interlock, cleared alarm and motor
    /// fault, level back at the moved setpoint, trip counter reset and
    /// rearmed.
    pub recovered: TelemetrySnapshot,
    /// The run's command receipts as submission returned them — the
    /// setpoint move, then the reset's assert and release — each
    /// `Accepted` with the documented apply tick.
    pub receipts: Vec<CommandReceipt>,
}

/// Runs `count` scans: `scan`, step the plant, record the level — the
/// fixed-step cycle every phase shares.
fn scans(
    executor: &mut Executor<'_>,
    driver: &FanoutDriver,
    count: u64,
    levels: &mut Vec<f64>,
) -> Result<(), ShowcaseError> {
    for _ in 0..count {
        executor.scan()?;
        driver.step(SCAN_PERIOD)?;
        let value = executor
            .sample(points::LEVEL_PERCENT)
            .map(|sample| sample.value)
            .expect("the level carrier is written every scan");
        let Value::Float(level) = value else {
            unreachable!("the level carrier is a Float point")
        };
        levels.push(level);
    }
    Ok(())
}

/// Loads `source` as a plant model, builds the field driver through the
/// standard driver registry, assembles every component through the
/// standard component registry — `dcs_controller::registry()`, the set
/// `dcs-controller` deploys — with no manual wiring, and runs the
/// documented four-phase scenario of [`TOTAL_SCANS`] scans.
///
/// The run is fully deterministic: driver and executor are tick-domain,
/// so two calls with the same `source` return equal [`Run`]s.
pub fn run(source: &str) -> Result<Run, ShowcaseError> {
    let model = PlantModel::load(source)?;
    let driver = driver(&model)?;
    let mut executor = assemble(&model, &dcs_controller::registry(), &driver)?;
    let mut levels = Vec::with_capacity(TOTAL_SCANS as usize);
    let mut receipts = Vec::new();

    // Phase 1 — settle at the declared setpoint. The pump's timer holds
    // its start back two scans and the run loopback plus debounce take
    // two more, so the interlock holds the valve shut until the
    // permissive proves — the counter's first trip. The level dips while
    // the loop is held open, then recovers and settles at 60%.
    scans(&mut executor, &driver, SETTLE_SCANS, &mut levels)?;
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
    scans(&mut executor, &driver, MOVED_SCANS, &mut levels)?;
    let moved = executor.snapshot();

    // Phase 3 — the injected input fault: `p101_run` disconnected. The
    // executor keeps the last value marked `Bad(CommunicationFault)`;
    // the conditioned run status goes bad with it, the interlock trips
    // on the bad permissive and drives the valve to `safe_value`, and
    // the level drains past the low alarm limit — the alarm asserts and
    // the horn follows. The trip edge is the counter's second, landing
    // it on its preset: `done` latches. The motor's run-feedback check
    // cannot prove agreement and flags its fault after `fault_ticks`.
    let sim = driver
        .sim()
        .expect("the showcase's field points are all sim-served");
    sim.inject_fault(points::PUMP_RUN, Fault::Disconnected)?;
    scans(&mut executor, &driver, FAULT_SCANS, &mut levels)?;
    let faulted = executor.snapshot();

    // Phase 4 — recovery: clearing the fault restores the feedback read,
    // the permissive proves Good again, and the interlock auto-resets —
    // it holds no latch — so the loop reopens the valve and the level
    // climbs back through the alarm's deadband to the moved setpoint.
    // Then the operator's post-trip action: the counter's writable
    // reset clears the latched count and `done`, and releasing it
    // rearms the counter.
    sim.clear_fault(points::PUMP_RUN)?;
    scans(&mut executor, &driver, RECOVERY_SCANS - 2, &mut levels)?;
    receipts.push(executor.submit_command(Command::WriteValue {
        point: points::TRIP_COUNT_RESET,
        kind: ValueKind::Bool,
        value: Value::Bool(true),
    }));
    scans(&mut executor, &driver, 1, &mut levels)?;
    receipts.push(executor.submit_command(Command::WriteValue {
        point: points::TRIP_COUNT_RESET,
        kind: ValueKind::Bool,
        value: Value::Bool(false),
    }));
    scans(&mut executor, &driver, 1, &mut levels)?;
    let recovered = executor.snapshot();

    Ok(Run {
        levels,
        settled,
        moved,
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
