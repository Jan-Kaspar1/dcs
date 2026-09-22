//! The duty/standby lift-station composition — the consumer-owned
//! plant model this repository emits.
//!
//! The station is composed entirely through `dcs-build`'s supported
//! engineering surface: typed specs for every component, typed endpoint
//! handles for every connection, and `PlantBuilder`'s declared io
//! points, channels, signals, and journal flags. `dcs-build`'s own
//! `station` module is a platform-owned conformance fixture; this file
//! is the consumer's own composition — its point-id scheme, its site
//! parameterization, and its alarm policy live here, not in the
//! platform, and are free to diverge.
//!
//! ## The composition
//!
//! - **Measurement:** a `failover-select` over the primary and backup
//!   level channels feeds a `threshold-chain`, whose stage-count
//!   `demand` drives `pump-group.demand`. A port output may drive only
//!   one endpoint, so every fan-out goes through a declared internal
//!   `Out` carrier plus one `In` consumer per reader — each link
//!   crossing the one-scan boundary.
//! - **Pumps:** `N` `motor` instances under one `pump-group`; the
//!   group's `cmd_i`/`run_i`/`fault_i`/`avail_i` indexed ports wire
//!   per pump. `avail_i` is the aggregated availability: in-auto, in
//!   service, station power healthy, thermal and moisture contacts
//!   healthy.
//! - **Manual takeover (the platform's decision-88 contract this
//!   composition implements):** per pump, writable `mode`/`hand`/`oos`
//!   internal points select `motor.cmd = ((group cmd and not mode) or
//!   (hand and mode and the held protection set)) and protections-ok`.
//!   A per-pump `interlock` aggregates the protections under the
//!   kind's fail-safe quality rule — the power-fail, thermal, and
//!   moisture contacts, the chain's `below_cutoff`, and in-service as
//!   its permissive, with the delivered selected level on its `in` so
//!   an untrusted measurement cannot prove the dry-run trip clear —
//!   and the inverted `tripped` guards the command in both modes
//!   while a `timer` at `min_off_ticks` holds the hand leg out until
//!   the protections have stood.
//! - **Alarms:** every alarm is a managed latching instance with a
//!   writable `ack` point and the journaled managed status set —
//!   `managed-latching-alarm`s on the selected level at the `high` and
//!   `cutoff` setpoints, `managed-bool-latching-alarm`s on the
//!   failover's `backup_active`, the group's `none_available` and
//!   `all_faulted`, the station power-fail contact, and each pump's
//!   fault, thermal, and moisture conditions. The cause alarms read
//!   quality-aware conditions — the station power guard's `tripped`
//!   and one per-pump cause guard per contact — so an asserted *or*
//!   untrusted contact annunciates on the same reading that trips
//!   the pump. The site's shelving
//!   policy is declared data: the high-level alarm is never-shelvable
//!   twice over — `max_shelve_ticks = 0` and a read-only shelve point,
//!   so a shelve request answers `NotWritable` at submission — while
//!   the low-level alarm is the shelvable nuisance case under
//!   `alarms.low_level_shelve_ticks`. Each pump fault alarm binds its
//!   `oos` to the pump's own maintenance-inhibit point and its
//!   `suppress` to the delivered copy, so a deliberately offline
//!   machine's fault stays named without annunciating.
//! - **Program:** the exercise program — the station's `sequencer`
//!   instance — steps its declared table while its `run` input holds,
//!   reporting `step`/`done` and emitting `step_completed` per
//!   finished step; the kind's declared `advance`/`reset` commands
//!   pace or restart it through the receipted command path.
//!
//! ## The declared point-id scheme
//!
//! Field points occupy fixed blocks the checked-in dynamics document
//! is written against — `10`/`11` the primary/backup levels, `12`
//! inflow, `13` net flow, `14` power-fail, `15` the high-well contact
//! the dynamics document's `threshold` element drives, and per pump
//! `i` (0-based, `pumps <= 20`): `20+i` draw, `40+i` run, `60+i`
//! thermal, `80+i` moisture, `100+i` command. Internal carriers start
//! at `200`, the exercise program's block sits at `240`, per-pump
//! internal blocks at `300 + 32·i`, and every alarm
//! owns a ten-point block at `1000 + 10·a`: `ack`/`shelve`/`oos` at
//! offsets 0–2 where the instance declares the input, `alarm`,
//! `unacknowledged`, `shelved`, `suppressed`, `out_of_service` at 3–7.
//! The station alarms take `a` = 0–5 and pump `i`'s alarms
//! `a` = 6 + 3·i + 0/1/2. Every point's signal sits at `10000 +
//! point`. The scheme is deterministic in declaration order, so
//! identical builder invocations emit identical documents.

use dcs_build::specs::{
    BoolGateInstance, BoolGateSpec, DigitalInputSpec, FailoverSelectSpec, InterlockSpec,
    ManagedAlarmHandles, ManagedBoolLatchingAlarmSpec, ManagedInputs, ManagedLatchingAlarmSpec,
    MotorSpec, PumpGroupInstance, PumpGroupSpec, SequencerSpec, ThresholdChainSpec, TimerSpec,
};
use dcs_build::{
    parameters, BuildError, Direction, InPoint, PlantBuilder, PointId, Rationalization, SignalId,
    Sink, Source, Value,
};
use dcs_model::PlantModel;

/// Field point ids — the fixed block the dynamics document addresses.
pub mod points {
    use dcs_build::PointId;

    /// The primary wet-well level measurement (`Float`, `In`).
    pub const LEVEL_PRIMARY: PointId = PointId(10);
    /// The backup wet-well level measurement (`Float`, `In`).
    pub const LEVEL_BACKUP: PointId = PointId(11);
    /// The declared station inflow (`Float`, `In`) — a dynamics input.
    pub const INFLOW: PointId = PointId(12);
    /// The summed net flow the level integrator advances (`Float`, `In`).
    pub const NET_FLOW: PointId = PointId(13);
    /// The station power-fail contact (`Bool`, `In`).
    pub const POWER_FAIL: PointId = PointId(14);
    /// The high-well contact (`Bool`, `In`) — the dynamics document's
    /// `threshold` element asserts it when the level overflows the
    /// high setpoint, cutting the simulated inflow.
    pub const WELL_FULL: PointId = PointId(15);
    /// Pump `index`'s simulated discharge flow (`Float`, `In`).
    pub fn draw(index: usize) -> PointId {
        PointId(20 + index as u64)
    }
    /// Pump `index`'s run feedback (`Bool`, `In`).
    pub fn run(index: usize) -> PointId {
        PointId(40 + index as u64)
    }
    /// Pump `index`'s thermal-overload contact (`Bool`, `In`).
    pub fn thermal(index: usize) -> PointId {
        PointId(60 + index as u64)
    }
    /// Pump `index`'s moisture contact (`Bool`, `In`).
    pub fn moisture(index: usize) -> PointId {
        PointId(80 + index as u64)
    }
    /// Pump `index`'s field command (`Bool`, `Out`).
    pub fn cmd(index: usize) -> PointId {
        PointId(100 + index as u64)
    }
}

/// Internal carrier ids — the station-level wiring.
mod carriers {
    pub const LEVEL_SEL: u64 = 200;
    pub const LEVEL_CHAIN_IN: u64 = 201;
    pub const LEVEL_LAH_IN: u64 = 202;
    pub const LEVEL_LAL_IN: u64 = 203;
    pub const DEMAND: u64 = 204;
    pub const DEMAND_IN: u64 = 205;
    pub const POWER_OK: u64 = 206;
    pub const DUTY: u64 = 210;
    pub const STAGED: u64 = 211;
    pub const DUTY_CALL: u64 = 212;
    pub const LAG_CALL: u64 = 213;
    pub const BELOW_CUTOFF: u64 = 214;
    pub const HIGH_LEVEL: u64 = 215;
    pub const BACKUP_ACTIVE: u64 = 216;
    pub const NONE_AVAILABLE: u64 = 217;
    pub const ALL_FAULTED: u64 = 218;
    pub const BACKUP_ACTIVE_IN: u64 = 219;
    pub const NONE_AVAILABLE_IN: u64 = 220;
    pub const ALL_FAULTED_IN: u64 = 221;
    /// The `power-ok` copy the station power interlock's permissive
    /// reads.
    pub const POWER_OK_GUARD_IN: u64 = 222;
    /// The held analog feed the power interlock's `in` binds — the
    /// declared conditions are discrete, so a constant `Good` input
    /// leaves `tripped` reporting the permissive alone.
    pub const POWER_GUARD_ANCHOR: u64 = 223;
    /// The power interlock's unused pass-through carrier.
    pub const POWER_GUARD_OUT: u64 = 224;
    /// The power interlock's `tripped` carrier — `journaled`: the
    /// station power trip's durable transition, asserted on the
    /// contact's value or its untrusted quality alike.
    pub const POWER_TRIPPED: u64 = 225;
    /// The `tripped` copy delivered to the power-fail alarm.
    pub const POWER_TRIPPED_IN: u64 = 226;
    /// The any-pump-manual carrier — the `none-available` alarm's
    /// declared suppression condition.
    pub const ANY_MANUAL: u64 = 227;
    /// The delivered copy the `none-available` alarm's `suppress`
    /// reads.
    pub const ANY_MANUAL_IN: u64 = 228;
    /// The held analog feed the per-pump cause guards' `in` binds —
    /// the declared conditions are discrete, so a constant `Good`
    /// input leaves each `tripped` reporting the contact alone.
    pub const GUARD_ANCHOR: u64 = 229;
    /// The held permissive the per-pump cause guards bind — a cause
    /// alarm stands in every service state, so `oos-ok` is not its
    /// permissive.
    pub const GUARD_TRUE: u64 = 230;
    /// The exercise program's held `run` request — the writable point
    /// an operator writes to start the table.
    pub const EXERCISE_RUN: u64 = 240;
    /// The exercise program's held `reset` condition — bound read-only
    /// because the kind's declared `reset` command is the one-shot
    /// operator path.
    pub const EXERCISE_RESET: u64 = 241;
    /// The active exercise step's declared demand.
    pub const EXERCISE_OUT: u64 = 242;
    /// The active exercise step, 1-based.
    pub const EXERCISE_STEP: u64 = 243;
    /// The exercise program's run-to-end flag.
    pub const EXERCISE_DONE: u64 = 244;
}

/// The first per-pump internal block: pump `i` owns
/// `PUMP_BASE + i * PUMP_STRIDE .. +PUMP_STRIDE`.
const PUMP_BASE: u64 = 300;
const PUMP_STRIDE: u64 = 32;
/// Alarm points: alarm `a` owns `ALARM_BASE + a * 10 .. +10`.
const ALARM_BASE: u64 = 1000;
/// The first per-pump alarm index, after the six station alarms.
const PUMP_ALARM_BASE: u64 = 6;
/// Every point's signal id is `SIGNAL_BASE + point`.
const SIGNAL_BASE: u64 = 10_000;

/// `bool-gate`'s `operation` codes — `and`/`or` mirrored as data.
const GATE_AND: i64 = 0;
const GATE_OR: i64 = 1;

/// The bound a single-sided level alarm parks its unused limit at —
/// far outside any measurable span, so only the declared side trips.
const PARKED_LIMIT: f64 = 1.0e9;

/// One annunciation tier's class data — the site's alarm vocabulary,
/// carried as the managed alarms' `priority`/`class`/`response_ticks`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AlarmClass {
    /// The annunciation priority the site assigns.
    pub priority: i64,
    /// The alarm class the site assigns.
    pub class: i64,
    /// The operator-response budget in scans.
    pub response_ticks: i64,
}

/// The site's alarm-lifecycle policy — the shelving and annunciation
/// decisions the managed alarm set carries as data.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AlarmPolicy {
    /// The tier the wet-well level alarms take.
    pub level: AlarmClass,
    /// The tier the station and pump equipment alarms take.
    pub equipment: AlarmClass,
    /// The low-level alarm's `max_shelve_ticks` — the shelvable
    /// nuisance case's bound, the asserting scan counting as the
    /// first. The high-level alarm is never shelvable: bound `0` plus
    /// a read-only request point.
    pub low_level_shelve_ticks: i64,
}

/// The station's site parameterization — pump count, threshold
/// setpoints, staging policy, and the alarm policy.
#[derive(Debug, Clone, PartialEq)]
pub struct SiteConfig {
    /// The pump count `N`: `pump-group`'s indexed port families and the
    /// per-pump wiring repeat `1..=N` times; the id scheme admits 20.
    pub pumps: usize,
    /// Threshold-chain setpoints in metres; must satisfy
    /// `cutoff < stop < start < lag_start < high`.
    pub cutoff: f64,
    /// See [`cutoff`](Self::cutoff).
    pub stop: f64,
    /// See [`cutoff`](Self::cutoff).
    pub start: f64,
    /// See [`cutoff`](Self::cutoff).
    pub lag_start: f64,
    /// See [`cutoff`](Self::cutoff); also the high-level alarm's trip.
    pub high: f64,
    /// The demand the chain emits while the selected level is
    /// untrusted — `0`, `1`, or `2`.
    pub on_bad_demand: i64,
    /// `pump-group`'s rotation policy code: `0` alternate each
    /// pump-down cycle, `1` timed interval, `2` least-run-hours first.
    pub rotation: i64,
    /// The timed-rotation interval; declared only when `rotation` is
    /// `1`.
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
    /// The level alarms' hysteresis band, in metres.
    pub level_alarm_hysteresis: f64,
    /// The site's alarm-lifecycle policy.
    pub alarms: AlarmPolicy,
}

impl SiteConfig {
    /// This site's declared parameterization: a duplex set alternating
    /// duty each pump-down cycle, the wet-well setpoints the
    /// commissioning record agreed, and the declared alarm policy.
    pub fn declared() -> Self {
        Self {
            pumps: 2,
            cutoff: 1.5,
            stop: 3.3,
            start: 3.7,
            lag_start: 4.2,
            high: 4.6,
            on_bad_demand: 0,
            rotation: 0,
            rotation_ticks: None,
            start_delay_ticks: 1,
            restage_delay_ticks: 0,
            min_off_ticks: 2,
            motor_fault_ticks: 2,
            level_alarm_hysteresis: 0.1,
            alarms: AlarmPolicy {
                level: AlarmClass {
                    priority: 1,
                    class: 1,
                    response_ticks: 30,
                },
                equipment: AlarmClass {
                    priority: 2,
                    class: 2,
                    response_ticks: 60,
                },
                low_level_shelve_ticks: 8,
            },
        }
    }
}

/// One managed alarm's place in the emitted document — the points its
/// declared inputs bound and its status outputs landed on.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AlarmLayout {
    /// The writable internal `In` point the operator ack lands on.
    pub ack: PointId,
    /// The point the `shelve` input binds — `Some` only where the
    /// instance declares the port. The point's `writable` flag is the
    /// declared shelving policy: writable is the receipted operator
    /// request; read-only is the never-shelvable declaration whose
    /// writes answer `NotWritable` at submission.
    pub shelve: Option<PointId>,
    /// The point the `oos` input binds — `Some` only where the
    /// instance declares the port and a dedicated point carries it.
    pub oos: Option<PointId>,
    /// The internal `Out` point carrying the standing `alarm` output.
    pub alarm: PointId,
    /// The internal `Out` point carrying the `unacknowledged` latch.
    pub unacknowledged: PointId,
    /// The internal `Out` point carrying the `shelved` status.
    pub shelved: PointId,
    /// The internal `Out` point carrying the `suppressed` status.
    pub suppressed: PointId,
    /// The internal `Out` point carrying the `out_of_service` status.
    pub out_of_service: PointId,
}

/// Pump `index`'s place in the emitted document — the ids the dynamics
/// document and the scripted scenario address.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PumpLayout {
    /// The field command `Out` point.
    pub cmd: PointId,
    /// The run-feedback field `In` point.
    pub run: PointId,
    /// The simulated draw field `In` point a `bool_flow` drives.
    pub draw: PointId,
    /// The writable `mode` point — `false` auto, `true` manual.
    pub mode: PointId,
    /// The writable `hand` run request.
    pub hand: PointId,
    /// The writable out-of-service flag.
    pub out_of_service: PointId,
    /// The motor's proven fault carrier — `journaled`.
    pub fault: PointId,
    /// The aggregated `avail_i` carrier — `journaled`.
    pub avail: PointId,
    /// The protection interlock's `tripped` carrier — `journaled`.
    pub protect_tripped: PointId,
    /// The inverted `protections-ok` carrier the command guard and the
    /// hand holdout read — `journaled`.
    pub protections_ok: PointId,
    /// The managed motor-fault alarm.
    pub fault_alarm: AlarmLayout,
    /// The managed thermal-overload alarm.
    pub thermal_alarm: AlarmLayout,
    /// The managed moisture alarm.
    pub moisture_alarm: AlarmLayout,
}

/// Where the station-level declarations landed — the ids the dynamics
/// document and the scripted scenario address.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StationLayout {
    /// The failover-selected level carrier the chain and alarms read.
    pub level_selected: PointId,
    /// The chain's stage-count demand carrier.
    pub demand: PointId,
    /// The group's duty-index carrier.
    pub duty: PointId,
    /// The group's staged-count carrier.
    pub staged: PointId,
    /// The chain's `duty_call` flag carrier.
    pub duty_call: PointId,
    /// The chain's `lag_call` flag carrier.
    pub lag_call: PointId,
    /// The chain's `high_level` flag carrier — `journaled`.
    pub high_level: PointId,
    /// The chain's `below_cutoff` flag carrier — `journaled`.
    pub below_cutoff: PointId,
    /// The failover's `backup_active` carrier — `journaled`.
    pub backup_active: PointId,
    /// The group's `none_available` carrier — `journaled`.
    pub none_available: PointId,
    /// The group's `all_faulted` carrier — `journaled`.
    pub all_faulted: PointId,
    /// The station power interlock's `tripped` carrier — the
    /// `power-fail` alarm's quality-aware condition, `journaled`.
    pub power_tripped: PointId,
    /// The any-pump-manual carrier — the `none-available` alarm's
    /// declared suppression condition.
    pub any_manual: PointId,
    /// Per-pump layouts, in `index` order.
    pub pumps: Vec<PumpLayout>,
    /// The managed high-level alarm — never-shelvable.
    pub high_level_alarm: AlarmLayout,
    /// The managed low-level alarm — the shelvable nuisance case.
    pub low_level_alarm: AlarmLayout,
    /// The managed backup-measurement-serving alarm.
    pub backup_active_alarm: AlarmLayout,
    /// The managed no-pump-available alarm.
    pub none_available_alarm: AlarmLayout,
    /// The managed every-pump-faulted alarm.
    pub all_faulted_alarm: AlarmLayout,
    /// The managed station power-fail alarm.
    pub power_fail_alarm: AlarmLayout,
}

/// The composed station: the emitted document plus the layout every
/// declared id landed on.
#[derive(Debug, Clone)]
pub struct Station {
    /// The versioned plant-model document.
    pub model: PlantModel,
    /// The id map into it.
    pub layout: StationLayout,
}

/// Composes the station under `config` and emits its [`PlantModel`].
///
/// # Panics
///
/// `config.pumps` outside `1..=20` exceeds the declared point-id
/// scheme; a setpoint ordering other than `cutoff < stop < start <
/// lag_start < high` is a `threshold-chain` parameter error at
/// `build`.
pub fn lift_station(config: &SiteConfig) -> Result<Station, BuildError> {
    assert!(
        (1..=20).contains(&config.pumps),
        "the station's point-id scheme admits 1..=20 pumps, got {}",
        config.pumps
    );
    let mut plant = PlantBuilder::new();

    // The simulated I/O devices — all under the `sim` prefix, so the
    // standard driver registry serves them locally and the shared
    // simulated plant serves them remotely.
    let ai = plant.device("sim-ai").id;
    let di = plant.device("sim-di").id;
    let d_o = plant.device("sim-do").id;

    let level_primary_ch = plant.channel::<f64>(ai, "level-primary", Direction::In);
    let level_backup_ch = plant.channel::<f64>(ai, "level-backup", Direction::In);
    let inflow_ch = plant.channel::<f64>(ai, "inflow", Direction::In);
    let net_flow_ch = plant.channel::<f64>(ai, "net-flow", Direction::In);
    let power_fail_ch = plant.channel::<bool>(di, "power-fail", Direction::In);
    let well_full_ch = plant.channel::<bool>(di, "well-full", Direction::In);
    let pump_channels: Vec<_> = (0..config.pumps)
        .map(|i| {
            let tag = pump_tag(i);
            (
                plant.channel::<f64>(ai, &format!("{tag}-draw"), Direction::In),
                plant.channel::<bool>(di, &format!("{tag}-run"), Direction::In),
                plant.channel::<bool>(di, &format!("{tag}-thermal"), Direction::In),
                plant.channel::<bool>(di, &format!("{tag}-moisture"), Direction::In),
                plant.channel::<bool>(d_o, &format!("{tag}-cmd"), Direction::Out),
            )
        })
        .collect();

    // Field points. `inflow`, `net-flow`, `well-full`, and the draws are
    // produced by the dynamics document's elements, so their handles
    // go unused. The power-fail contact is a protection-layer reported
    // state — `journaled` so its transitions land in the durable
    // record beside its alarm's.
    let level_primary = plant.field_input::<f64>(points::LEVEL_PRIMARY, level_primary_ch, false);
    let level_backup = plant.field_input::<f64>(points::LEVEL_BACKUP, level_backup_ch, false);
    plant.field_input::<f64>(points::INFLOW, inflow_ch, false);
    plant.field_input::<f64>(points::NET_FLOW, net_flow_ch, false);
    let power_fail = plant.field_input::<bool>(points::POWER_FAIL, power_fail_ch, false);
    plant.journaled(power_fail);
    plant.field_input::<bool>(points::WELL_FULL, well_full_ch, false);

    signal(
        &mut plant,
        points::LEVEL_PRIMARY,
        "level-primary",
        "m",
        "Primary wet-well level measurement",
        "wet-well",
    );
    signal(
        &mut plant,
        points::LEVEL_BACKUP,
        "level-backup",
        "m",
        "Backup wet-well level measurement",
        "wet-well",
    );
    signal(
        &mut plant,
        points::INFLOW,
        "inflow",
        "m3/h",
        "Declared station inflow — the dynamics document's forcing input",
        "wet-well",
    );
    signal(
        &mut plant,
        points::NET_FLOW,
        "net-flow",
        "m3/h",
        "Net wet-well flow: inflow plus the pump draws",
        "wet-well",
    );
    signal(
        &mut plant,
        points::POWER_FAIL,
        "power-fail",
        "",
        "Station power-fail contact",
        "station",
    );
    signal(
        &mut plant,
        points::WELL_FULL,
        "well-full",
        "",
        "High-well contact the dynamics threshold asserts",
        "wet-well",
    );

    // The shared internal carriers: the failover's selected level, the
    // chain's demand, power-ok, the status outputs, and the three
    // alarm-condition consumers. A carrier's `initial` is what its
    // consumers see at scan 1's input phase — seeded as the healthy
    // cold-start state (mid-band level, power healthy) so no alarm
    // trips before the first real samples land.
    // The level consumers' delivered copies seed the cold-start level
    // too — a `0.0` seed would read as below the dry-run cutoff on the
    // first scan and trip the low-level alarm on a phantom sample.
    let level_sel = plant.internal_output::<f64>(PointId(carriers::LEVEL_SEL), 3.5);
    let level_chain_in = plant.internal_input::<f64>(PointId(carriers::LEVEL_CHAIN_IN), 3.5, false);
    let level_lah_in = plant.internal_input::<f64>(PointId(carriers::LEVEL_LAH_IN), 3.5, false);
    let level_lal_in = plant.internal_input::<f64>(PointId(carriers::LEVEL_LAL_IN), 3.5, false);
    let demand = plant.internal_output::<i64>(PointId(carriers::DEMAND), 0);
    let demand_in = plant.internal_input::<i64>(PointId(carriers::DEMAND_IN), 0, false);
    let power_ok = plant.internal_output::<bool>(PointId(carriers::POWER_OK), true);
    let duty = plant.internal_output::<i64>(PointId(carriers::DUTY), 0);
    let staged = plant.internal_output::<i64>(PointId(carriers::STAGED), 0);
    let duty_call = plant.internal_output::<bool>(PointId(carriers::DUTY_CALL), false);
    let lag_call = plant.internal_output::<bool>(PointId(carriers::LAG_CALL), false);
    let below_cutoff = plant.internal_output::<bool>(PointId(carriers::BELOW_CUTOFF), false);
    let high_level = plant.internal_output::<bool>(PointId(carriers::HIGH_LEVEL), false);
    let backup_active = plant.internal_output::<bool>(PointId(carriers::BACKUP_ACTIVE), false);
    let none_available = plant.internal_output::<bool>(PointId(carriers::NONE_AVAILABLE), false);
    let all_faulted = plant.internal_output::<bool>(PointId(carriers::ALL_FAULTED), false);
    let backup_active_in =
        plant.internal_input::<bool>(PointId(carriers::BACKUP_ACTIVE_IN), false, false);
    let none_available_in =
        plant.internal_input::<bool>(PointId(carriers::NONE_AVAILABLE_IN), false, false);
    let all_faulted_in =
        plant.internal_input::<bool>(PointId(carriers::ALL_FAULTED_IN), false, false);
    // The cause-alarm wiring: `power-ok`'s delivered copy is the
    // station power interlock's permissive — false or untrusted trips
    // alike — and the `tripped` carrier pair feeds the `power-fail`
    // alarm's condition, so the cause alarm asserts on the same
    // reading availability takes. The `in` port's Float is the held
    // anchor: the declared conditions are discrete.
    let power_ok_guard_in =
        plant.internal_input::<bool>(PointId(carriers::POWER_OK_GUARD_IN), false, false);
    let power_guard_anchor =
        plant.internal_input::<f64>(PointId(carriers::POWER_GUARD_ANCHOR), 0.0, false);
    let power_guard_out =
        plant.internal_output::<f64>(PointId(carriers::POWER_GUARD_OUT), 0.0);
    let power_tripped =
        plant.internal_output::<bool>(PointId(carriers::POWER_TRIPPED), false);
    let power_tripped_in =
        plant.internal_input::<bool>(PointId(carriers::POWER_TRIPPED_IN), false, false);
    // The `none-available` alarm's declared suppression: any pump held
    // in manual is the operator withdrawing it from the group's roster
    // — "demand stands with no pump available" is then designed state,
    // not a fault. The carrier keeps reporting truth; the `suppressed`
    // flag names the withholding.
    let any_manual = plant.internal_output::<bool>(PointId(carriers::ANY_MANUAL), false);
    let any_manual_in =
        plant.internal_input::<bool>(PointId(carriers::ANY_MANUAL_IN), false, false);
    // The per-pump cause guards' held feeds — the power guard's
    // held-anchor trick again: a constant `Good` `in` and `permissive`
    // leave each guard's `tripped` reporting the contact alone —
    // asserted or untrusted alike — so a degraded thermal or moisture
    // contact annunciates its own cause alarm on the same reading
    // that trips the pump.
    let guard_anchor =
        plant.internal_input::<f64>(PointId(carriers::GUARD_ANCHOR), 0.0, false);
    let guard_true =
        plant.internal_input::<bool>(PointId(carriers::GUARD_TRUE), true, false);
    plant.journaled(power_tripped);

    // The protection-relevant status carriers the durable record
    // names — the chain's dry-run and high flags, the failover's
    // backup-serving state, and the group's availability roll-ups —
    // marked `journaled` so every transition lands in the journal.
    for point in [
        below_cutoff,
        high_level,
        backup_active,
        none_available,
        all_faulted,
    ] {
        plant.journaled(point);
    }

    for (point, name, description) in [
        (
            carriers::LEVEL_SEL,
            "level-selected",
            "Failover-selected level the station controls on",
        ),
        (
            carriers::LEVEL_CHAIN_IN,
            "level-chain-in",
            "Selected level delivered to the threshold chain",
        ),
        (
            carriers::LEVEL_LAH_IN,
            "level-lah-in",
            "Selected level delivered to the high-level alarm",
        ),
        (
            carriers::LEVEL_LAL_IN,
            "level-lal-in",
            "Selected level delivered to the low-level alarm",
        ),
        (
            carriers::DEMAND,
            "demand",
            "Stage-count demand the threshold chain computes",
        ),
        (
            carriers::DEMAND_IN,
            "demand-in",
            "Stage-count demand delivered to the pump group",
        ),
        (
            carriers::POWER_OK,
            "power-ok",
            "Station power healthy — the inverted power-fail contact",
        ),
        (
            carriers::DUTY,
            "duty",
            "1-based index of the pump holding duty; 0 while none does",
        ),
        (
            carriers::STAGED,
            "staged",
            "How many pumps the group currently commands",
        ),
        (
            carriers::DUTY_CALL,
            "duty-call",
            "The chain's call for the duty pump",
        ),
        (
            carriers::LAG_CALL,
            "lag-call",
            "The chain's call for the lag pump",
        ),
        (
            carriers::BELOW_CUTOFF,
            "below-cutoff",
            "Selected level at or below the dry-run cutoff",
        ),
        (
            carriers::HIGH_LEVEL,
            "high-level",
            "Selected level at or above the high setpoint",
        ),
        (
            carriers::BACKUP_ACTIVE,
            "backup-active",
            "The backup level measurement is serving",
        ),
        (
            carriers::NONE_AVAILABLE,
            "none-available",
            "No pump reports itself available",
        ),
        (
            carriers::ALL_FAULTED,
            "all-faulted",
            "Every pump's fault flag reads failed",
        ),
        (
            carriers::BACKUP_ACTIVE_IN,
            "backup-active-in",
            "Backup-serving flag delivered to its alarm",
        ),
        (
            carriers::NONE_AVAILABLE_IN,
            "none-available-in",
            "No-pump-available flag delivered to its alarm",
        ),
        (
            carriers::ALL_FAULTED_IN,
            "all-faulted-in",
            "All-faulted flag delivered to its alarm",
        ),
        (
            carriers::POWER_OK_GUARD_IN,
            "power-ok-guard-in",
            "Station power-ok delivered to the power interlock",
        ),
        (
            carriers::POWER_GUARD_ANCHOR,
            "power-guard-anchor",
            "Held analog feed for the power interlock — its conditions are discrete",
        ),
        (
            carriers::POWER_GUARD_OUT,
            "power-guard-out",
            "The power interlock's analog pass-through — unused",
        ),
        (
            carriers::POWER_TRIPPED,
            "power-tripped",
            "Station power tripped — the contact asserted or untrusted",
        ),
        (
            carriers::POWER_TRIPPED_IN,
            "power-tripped-in",
            "The power trip delivered to the power-fail alarm",
        ),
        (
            carriers::ANY_MANUAL,
            "any-manual",
            "Any pump held in manual — the none-available alarm's designed suppression",
        ),
        (
            carriers::ANY_MANUAL_IN,
            "any-manual-in",
            "Any-manual delivered to the none-available alarm's suppress",
        ),
        (
            carriers::GUARD_ANCHOR,
            "guard-anchor",
            "Held analog feed for the per-pump cause guards — their conditions are discrete",
        ),
        (
            carriers::GUARD_TRUE,
            "guard-true",
            "Held permissive for the per-pump cause guards — a cause alarm stands in every service state",
        ),
    ] {
        let unit = match point {
            carriers::LEVEL_SEL
            | carriers::LEVEL_CHAIN_IN
            | carriers::LEVEL_LAH_IN
            | carriers::LEVEL_LAL_IN => "m",
            carriers::DEMAND | carriers::DEMAND_IN | carriers::DUTY | carriers::STAGED => "pumps",
            _ => "",
        };
        signal(
            &mut plant,
            PointId(point),
            name,
            unit,
            description,
            "station",
        );
    }

    // The station-level components.
    let failover = plant.add(FailoverSelectSpec::new(parameters([])));
    let chain = plant.add(ThresholdChainSpec::new(parameters([
        ("cutoff", Value::Float(config.cutoff)),
        ("stop", Value::Float(config.stop)),
        ("start", Value::Float(config.start)),
        ("lag_start", Value::Float(config.lag_start)),
        ("high", Value::Float(config.high)),
        ("on_bad_demand", Value::Int(config.on_bad_demand)),
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
    let inv_power = plant.add(DigitalInputSpec::new(parameters([(
        "invert",
        Value::Bool(true),
    )])));
    // The station power interlock evaluates the conditioned
    // `power-ok` — an unasserted *or* untrusted permissive trips — and
    // its `tripped` flag is the `power-fail` alarm's condition, so the
    // cause annunciates on the same reading that collapses `avail_i`
    // (a `Bad` contact can no longer leave the alarm clean while the
    // station declares no pump available). Its `in` binds a held
    // anchor: the declared conditions are discrete.
    let power_guard = plant.add(InterlockSpec::new(
        parameters([("safe_value", Value::Float(0.0))]),
        0,
    ));
    // Any pump held in manual is the `none-available` alarm's declared
    // suppression — the operator withdrawing a pump from the group's
    // roster makes "no pump available" designed state, not a fault.
    let any_manual_gate = plant.add(BoolGateSpec::new(
        parameters([("operation", Value::Int(GATE_OR))]),
        config.pumps,
    ));

    // The station alarms. The high-level alarm is the never-shelvable
    // critical annunciation, twice over: `max_shelve_ticks = 0` makes a
    // delivered request inert and the declared `shelve` port binds a
    // read-only point, so a shelve command answers `NotWritable` at
    // submission — the documented rejection path the scripted
    // simulation exercises. The low-level alarm is the shelvable
    // nuisance case under the site's declared bound.
    let level = config.alarms.level;
    let equipment = config.alarms.equipment;
    let lah = plant.add(ManagedLatchingAlarmSpec::new(
        parameters([
            ("low_limit", Value::Float(-PARKED_LIMIT)),
            ("high_limit", Value::Float(config.high)),
            ("hysteresis", Value::Float(config.level_alarm_hysteresis)),
            ("max_shelve_ticks", Value::Int(0)),
            ("priority", Value::Int(level.priority)),
            ("class", Value::Int(level.class)),
            ("response_ticks", Value::Int(level.response_ticks)),
        ]),
        ManagedInputs {
            shelve: true,
            ..ManagedInputs::default()
        },
        rationalization(
            "The wet well overflows the bench",
            "Start a pump and investigate why the demand did not call one",
            "lah-alarm",
        ),
    ));
    let lal = plant.add(ManagedLatchingAlarmSpec::new(
        parameters([
            ("low_limit", Value::Float(config.cutoff)),
            ("high_limit", Value::Float(PARKED_LIMIT)),
            ("hysteresis", Value::Float(config.level_alarm_hysteresis)),
            (
                "max_shelve_ticks",
                Value::Int(config.alarms.low_level_shelve_ticks),
            ),
            ("priority", Value::Int(level.priority)),
            ("class", Value::Int(level.class)),
            ("response_ticks", Value::Int(level.response_ticks)),
        ]),
        ManagedInputs {
            shelve: true,
            ..ManagedInputs::default()
        },
        rationalization(
            "The wet well pumps dry and the running pumps cavitate",
            "Stop the running pumps and investigate the low level",
            "lal-alarm",
        ),
    ));
    // The remaining station alarms declare no lifecycle inputs —
    // never-shelvable with no shelving surface, never suppressed,
    // never out of service; their managed status outputs still report.
    let backup_alarm = plant.add(ManagedBoolLatchingAlarmSpec::new(
        managed_parameters(equipment, 0),
        ManagedInputs::default(),
        rationalization(
            "The backup level instrument carries the station unnoticed",
            "Check the primary level instrument",
            "backup-active-alarm",
        ),
    ));
    let none_available_alarm = plant.add(ManagedBoolLatchingAlarmSpec::new(
        managed_parameters(equipment, 0),
        ManagedInputs {
            suppress: true,
            ..ManagedInputs::default()
        },
        rationalization(
            "Demand stands with no pump available to meet it",
            "Restore a pump to service or clear its faults",
            "none-available-alarm",
        ),
    ));
    let all_faulted_alarm = plant.add(ManagedBoolLatchingAlarmSpec::new(
        managed_parameters(equipment, 0),
        ManagedInputs::default(),
        rationalization(
            "Every pump is faulted; the station cannot pump",
            "Dispatch maintenance to clear the pump faults",
            "all-faulted-alarm",
        ),
    ));
    let power_fail_alarm = plant.add(ManagedBoolLatchingAlarmSpec::new(
        managed_parameters(equipment, 0),
        ManagedInputs::default(),
        rationalization(
            "Station power is lost; the pumps cannot run",
            "Switch to backup power and investigate the supply",
            "power-fail-alarm",
        ),
    ));

    // Measurement path: failover-select's output fans out to the chain
    // and both level alarms through the carrier; the chain's demand
    // reaches the group through its own pair.
    plant.connect(level_primary, &failover.primary);
    plant.connect(level_backup, &failover.backup);
    plant.connect(&failover.out, level_sel);
    plant.connect(level_chain_in, level_sel);
    plant.connect(level_lah_in, level_sel);
    plant.connect(level_lal_in, level_sel);
    plant.connect(&failover.backup_active, backup_active);
    plant.connect(level_chain_in, &chain.level);
    plant.connect(&chain.demand, demand);
    plant.connect(demand_in, demand);
    plant.connect(demand_in, &group.demand);
    plant.connect(&chain.duty_call, duty_call);
    plant.connect(&chain.lag_call, lag_call);
    plant.connect(&chain.below_cutoff, below_cutoff);
    plant.connect(&chain.high_level, high_level);
    plant.connect(&group.duty, duty);
    plant.connect(&group.staged, staged);
    plant.connect(&group.none_available, none_available);
    plant.connect(&group.all_faulted, all_faulted);
    plant.connect(power_fail, &inv_power.input);
    plant.connect(&inv_power.out, power_ok);
    plant.connect(power_ok_guard_in, power_ok);
    plant.connect(power_guard_anchor, &power_guard.input);
    plant.connect(power_ok_guard_in, &power_guard.permissive);
    plant.connect(&power_guard.out, power_guard_out);
    plant.connect(&power_guard.tripped, power_tripped);
    plant.connect(power_tripped_in, power_tripped);
    plant.connect(&any_manual_gate.out, any_manual);
    plant.connect(any_manual_in, any_manual);
    plant.connect(backup_active_in, backup_active);
    plant.connect(none_available_in, none_available);
    plant.connect(all_faulted_in, all_faulted);

    // The station-level alarms — `lah`'s `shelve` binds read-only (the
    // never-shelvable rejection path); `lal`'s binds writable (the
    // bounded, receipted operator request).
    let high_level_alarm = wire_station_alarm(
        &mut plant,
        0,
        &lah.ack,
        &lah.managed,
        &lah.alarm,
        &lah.unacknowledged,
        false,
        "lah",
    );
    plant.connect(level_lah_in, &lah.input);
    let low_level_alarm = wire_station_alarm(
        &mut plant,
        1,
        &lal.ack,
        &lal.managed,
        &lal.alarm,
        &lal.unacknowledged,
        true,
        "lal",
    );
    plant.connect(level_lal_in, &lal.input);
    let backup_active_alarm = wire_station_alarm(
        &mut plant,
        2,
        &backup_alarm.ack,
        &backup_alarm.managed,
        &backup_alarm.alarm,
        &backup_alarm.unacknowledged,
        false,
        "backup-active",
    );
    plant.connect(backup_active_in, &backup_alarm.input);
    let none_available_alarm_layout = wire_station_alarm(
        &mut plant,
        3,
        &none_available_alarm.ack,
        &none_available_alarm.managed,
        &none_available_alarm.alarm,
        &none_available_alarm.unacknowledged,
        false,
        "none-available",
    );
    plant.connect(none_available_in, &none_available_alarm.input);
    plant.connect(
        any_manual_in,
        none_available_alarm
            .managed
            .suppress
            .as_ref()
            .expect("the none-available alarm declares suppress"),
    );
    let all_faulted_alarm_layout = wire_station_alarm(
        &mut plant,
        4,
        &all_faulted_alarm.ack,
        &all_faulted_alarm.managed,
        &all_faulted_alarm.alarm,
        &all_faulted_alarm.unacknowledged,
        false,
        "all-faulted",
    );
    plant.connect(all_faulted_in, &all_faulted_alarm.input);
    let power_fail_alarm_layout = wire_station_alarm(
        &mut plant,
        5,
        &power_fail_alarm.ack,
        &power_fail_alarm.managed,
        &power_fail_alarm.alarm,
        &power_fail_alarm.unacknowledged,
        false,
        "power-fail",
    );
    plant.connect(power_tripped_in, &power_fail_alarm.input);

    // Per-pump wiring.
    let mut pumps = Vec::with_capacity(config.pumps);
    for (index, (draw_ch, run_ch, thermal_ch, moisture_ch, cmd_ch)) in
        pump_channels.into_iter().enumerate()
    {
        pumps.push(wire_pump(
            &mut plant,
            config,
            index,
            draw_ch,
            run_ch,
            thermal_ch,
            moisture_ch,
            cmd_ch,
            power_fail,
            power_ok,
            level_chain_in,
            below_cutoff,
            guard_anchor,
            guard_true,
            &any_manual_gate,
            &group,
        ));
    }

    // The exercise program — the station's `sequencer` and the kind
    // carrying the declared command/event vocabulary (`advance`,
    // `reset`, and the kind-emitted `step_completed` event) the
    // consumer surface proof exercises. `run` is the writable held
    // request that starts the table; `reset` binds read-only — the
    // declared command is the one-shot path. Each step's declared
    // demand lands on `out`; `step`/`done` report progress, and
    // `done` is journaled so the finished program leaves a durable
    // record beside its emitted events.
    let exercise = plant.add(SequencerSpec::new(parameters([
        ("step_count", Value::Int(2)),
        ("step_1_ticks", Value::Int(2)),
        ("step_1_out", Value::Float(1.0)),
        ("step_2_ticks", Value::Int(2)),
        ("step_2_out", Value::Float(0.5)),
    ])));
    let exercise_run =
        plant.internal_input::<bool>(PointId(carriers::EXERCISE_RUN), false, true);
    let exercise_reset =
        plant.internal_input::<bool>(PointId(carriers::EXERCISE_RESET), false, false);
    let exercise_out = plant.internal_output::<f64>(PointId(carriers::EXERCISE_OUT), 0.0);
    let exercise_step = plant.internal_output::<i64>(PointId(carriers::EXERCISE_STEP), 0);
    let exercise_done = plant.internal_output::<bool>(PointId(carriers::EXERCISE_DONE), false);
    plant.journaled(exercise_done);
    plant.connect(exercise_run, &exercise.run);
    plant.connect(exercise_reset, &exercise.reset);
    plant.connect(&exercise.out, exercise_out);
    plant.connect(&exercise.step, exercise_step);
    plant.connect(&exercise.done, exercise_done);
    for (point, name, unit, description) in [
        (
            carriers::EXERCISE_RUN,
            "exercise-run",
            "",
            "Exercise program run request",
        ),
        (
            carriers::EXERCISE_RESET,
            "exercise-reset",
            "",
            "Exercise program held reset condition",
        ),
        (
            carriers::EXERCISE_OUT,
            "exercise-out",
            "",
            "Active exercise step's declared demand",
        ),
        (
            carriers::EXERCISE_STEP,
            "exercise-step",
            "",
            "Active exercise step, 1-based",
        ),
        (
            carriers::EXERCISE_DONE,
            "exercise-done",
            "",
            "Exercise program run to completion",
        ),
    ] {
        signal(&mut plant, PointId(point), name, unit, description, "program");
    }

    let model = plant.build()?;
    Ok(Station {
        model,
        layout: StationLayout {
            level_selected: PointId(carriers::LEVEL_SEL),
            demand: PointId(carriers::DEMAND),
            duty: PointId(carriers::DUTY),
            staged: PointId(carriers::STAGED),
            duty_call: PointId(carriers::DUTY_CALL),
            lag_call: PointId(carriers::LAG_CALL),
            high_level: PointId(carriers::HIGH_LEVEL),
            below_cutoff: PointId(carriers::BELOW_CUTOFF),
            backup_active: PointId(carriers::BACKUP_ACTIVE),
            none_available: PointId(carriers::NONE_AVAILABLE),
            all_faulted: PointId(carriers::ALL_FAULTED),
            power_tripped: PointId(carriers::POWER_TRIPPED),
            any_manual: PointId(carriers::ANY_MANUAL),
            pumps,
            high_level_alarm,
            low_level_alarm,
            backup_active_alarm,
            none_available_alarm: none_available_alarm_layout,
            all_faulted_alarm: all_faulted_alarm_layout,
            power_fail_alarm: power_fail_alarm_layout,
        },
    })
}

/// One annunciation tier's managed-alarm parameter map.
fn managed_parameters(class: AlarmClass, max_shelve_ticks: i64) -> dcs_build::Parameters {
    parameters([
        ("max_shelve_ticks", Value::Int(max_shelve_ticks)),
        ("priority", Value::Int(class.priority)),
        ("class", Value::Int(class.class)),
        ("response_ticks", Value::Int(class.response_ticks)),
    ])
}

/// An alarm's rationalization record — the prose half of the alarm
/// contract, carried on the component instance so the plant model is
/// the master alarm database.
fn rationalization(consequence: &str, required_action: &str, reference: &str) -> Rationalization {
    Rationalization {
        consequence: consequence.to_string(),
        required_action: required_action.to_string(),
        reference: reference.to_string(),
    }
}

/// The 1-based tag "p101", "p102", … naming the pump sets.
pub fn pump_tag(index: usize) -> String {
    format!("p{}", 101 + index)
}

/// Registers point `point`'s monitoring signal — `10000 + point` —
/// carrying the unit/description/group metadata the monitoring
/// surface renders from.
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

/// Declares one station alarm's points and wires them: the writable
/// `ack`, each declared `shelve`/`oos` request point — `shelve`
/// carrying the alarm's declared writability policy — and the five
/// journaled status outputs. The caller wires the alarm's `in` port.
#[allow(clippy::too_many_arguments)]
fn wire_station_alarm(
    plant: &mut PlantBuilder,
    index: u64,
    ack_port: &Sink<bool>,
    managed: &ManagedAlarmHandles,
    alarm_port: &Source<bool>,
    unacknowledged_port: &Source<bool>,
    shelve_writable: bool,
    prefix: &str,
) -> AlarmLayout {
    let base = ALARM_BASE + index * 10;
    let ack = plant.internal_input::<bool>(PointId(base), false, true);
    let shelve = managed
        .shelve
        .as_ref()
        .map(|_| plant.internal_input::<bool>(PointId(base + 1), false, shelve_writable));
    let oos = managed
        .oos
        .as_ref()
        .map(|_| plant.internal_input::<bool>(PointId(base + 2), false, true));
    let alarm = plant.internal_output::<bool>(PointId(base + 3), false);
    let unacknowledged = plant.internal_output::<bool>(PointId(base + 4), false);
    let shelved = plant.internal_output::<bool>(PointId(base + 5), false);
    let suppressed = plant.internal_output::<bool>(PointId(base + 6), false);
    let out_of_service = plant.internal_output::<bool>(PointId(base + 7), false);

    // The lifecycle audit the site declares: every managed status
    // point is `journaled` — activation, return, the latch's clear,
    // shelving assertion and expiry, suppression, and out-of-service
    // entry and return all land as durable `point_changed` entries.
    // The declared request points journal too; `ack` stays
    // receipted-only — its writes are already the record.
    for point in [alarm, unacknowledged, shelved, suppressed, out_of_service] {
        plant.journaled(point);
    }
    for point in [shelve, oos].into_iter().flatten() {
        plant.journaled(point);
    }

    plant.connect(ack, ack_port);
    if let (Some(point), Some(port)) = (shelve, managed.shelve.as_ref()) {
        plant.connect(point, port);
    }
    if let (Some(point), Some(port)) = (oos, managed.oos.as_ref()) {
        plant.connect(point, port);
    }
    plant.connect(alarm_port, alarm);
    plant.connect(unacknowledged_port, unacknowledged);
    plant.connect(&managed.shelved, shelved);
    plant.connect(&managed.suppressed, suppressed);
    plant.connect(&managed.out_of_service, out_of_service);

    signal(
        plant,
        PointId(base),
        &format!("{prefix}-ack"),
        "",
        "Operator acknowledgment for the alarm",
        "alarms",
    );
    if let Some(point) = shelve {
        signal(
            plant,
            point.into(),
            &format!("{prefix}-shelve"),
            "",
            "Operator shelving request for the alarm",
            "alarms",
        );
    }
    if let Some(point) = oos {
        signal(
            plant,
            point.into(),
            &format!("{prefix}-oos"),
            "",
            "Operator out-of-service command for the alarm",
            "alarms",
        );
    }
    for (offset, suffix, description) in [
        (3, "alarm", "Standing alarm state under every managed flag"),
        (
            4,
            "unacknowledged",
            "Latched until the operator acknowledges",
        ),
        (5, "shelved", "Shelved within the declared bound"),
        (6, "suppressed", "Suppressed by the declared condition"),
        (7, "out-of-service", "Out of service on the declared path"),
    ] {
        signal(
            plant,
            PointId(base + offset),
            &format!("{prefix}-{suffix}"),
            "",
            description,
            "alarms",
        );
    }
    AlarmLayout {
        ack: PointId(base),
        shelve: shelve.map(|_| PointId(base + 1)),
        oos: oos.map(|_| PointId(base + 2)),
        alarm: PointId(base + 3),
        unacknowledged: PointId(base + 4),
        shelved: PointId(base + 5),
        suppressed: PointId(base + 6),
        out_of_service: PointId(base + 7),
    }
}

/// Declares one per-pump managed alarm's points — the writable `ack`
/// and the five journaled status outputs — and binds the declared
/// `oos`/`suppress` inputs to the pump's maintenance-inhibit state:
/// `oos` reads `inhibit` — the pump's own writable out-of-service
/// point — while `suppress` reads `suppress_in`, the delivered copy
/// `wire_pump` composes, since a component binds each point once. The
/// caller wires the alarm's `in` port.
#[allow(clippy::too_many_arguments)]
fn wire_pump_alarm(
    plant: &mut PlantBuilder,
    index: u64,
    ack_port: &Sink<bool>,
    managed: &ManagedAlarmHandles,
    alarm_port: &Source<bool>,
    unacknowledged_port: &Source<bool>,
    inhibit: InPoint<bool>,
    suppress_in: InPoint<bool>,
    prefix: &str,
    group: &str,
) -> AlarmLayout {
    let base = ALARM_BASE + index * 10;
    let ack = plant.internal_input::<bool>(PointId(base), false, true);
    let alarm = plant.internal_output::<bool>(PointId(base + 3), false);
    let unacknowledged = plant.internal_output::<bool>(PointId(base + 4), false);
    let shelved = plant.internal_output::<bool>(PointId(base + 5), false);
    let suppressed = plant.internal_output::<bool>(PointId(base + 6), false);
    let out_of_service = plant.internal_output::<bool>(PointId(base + 7), false);

    for point in [alarm, unacknowledged, shelved, suppressed, out_of_service] {
        plant.journaled(point);
    }

    plant.connect(ack, ack_port);
    if let Some(port) = managed.oos.as_ref() {
        plant.connect(inhibit, port);
    }
    if let Some(port) = managed.suppress.as_ref() {
        plant.connect(suppress_in, port);
    }
    plant.connect(alarm_port, alarm);
    plant.connect(unacknowledged_port, unacknowledged);
    plant.connect(&managed.shelved, shelved);
    plant.connect(&managed.suppressed, suppressed);
    plant.connect(&managed.out_of_service, out_of_service);

    signal(
        plant,
        PointId(base),
        &format!("{prefix}-ack"),
        "",
        "Operator acknowledgment for the alarm",
        group,
    );
    for (offset, suffix, description) in [
        (3, "alarm", "Standing alarm state under every managed flag"),
        (
            4,
            "unacknowledged",
            "Latched until the operator acknowledges",
        ),
        (5, "shelved", "Shelved within the declared bound"),
        (
            6,
            "suppressed",
            "Suppressed while the pump is out of service",
        ),
        (
            7,
            "out-of-service",
            "Out of service with the pump's maintenance state",
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
    AlarmLayout {
        ack: PointId(base),
        shelve: None,
        oos: managed.oos.as_ref().map(|_| inhibit.into()),
        alarm: PointId(base + 3),
        unacknowledged: PointId(base + 4),
        shelved: PointId(base + 5),
        suppressed: PointId(base + 6),
        out_of_service: PointId(base + 7),
    }
}

/// Wires pump `index` (`0`-based): field points, the availability
/// aggregation, the protection interlock and holdout, the
/// manual-takeover gates, the motor, and its three managed alarms —
/// then connects the group's indexed ports.
#[allow(clippy::too_many_arguments)]
fn wire_pump(
    plant: &mut PlantBuilder,
    config: &SiteConfig,
    index: usize,
    draw_ch: dcs_build::ChannelRef,
    run_ch: dcs_build::ChannelRef,
    thermal_ch: dcs_build::ChannelRef,
    moisture_ch: dcs_build::ChannelRef,
    cmd_ch: dcs_build::ChannelRef,
    power_fail: InPoint<bool>,
    power_ok: dcs_build::OutPoint<bool>,
    level_chain_in: InPoint<f64>,
    below_cutoff: dcs_build::OutPoint<bool>,
    guard_anchor: InPoint<f64>,
    guard_true: InPoint<bool>,
    any_manual: &BoolGateInstance,
    group: &PumpGroupInstance,
) -> PumpLayout {
    let i = index as u64;
    let tag = pump_tag(index);
    let group_name = format!("pump-{tag}");
    let base = PUMP_BASE + i * PUMP_STRIDE;

    // Field points; `draw` is produced by the dynamics document's
    // `bool_flow`, so its handle goes unused. The run, thermal, and
    // moisture contacts are the protection layer's reported states —
    // `journaled` like the carriers feeding them. Field-side fault
    // scenarios ride the plant server's `inject_fault` surface, so the
    // points stay outside the operator command surface.
    plant.field_input::<f64>(points::draw(index), draw_ch, false);
    let run = plant.field_input::<bool>(points::run(index), run_ch, false);
    let thermal = plant.field_input::<bool>(points::thermal(index), thermal_ch, false);
    let moisture = plant.field_input::<bool>(points::moisture(index), moisture_ch, false);
    let cmd = plant.field_output::<bool>(points::cmd(index), cmd_ch);
    plant.journaled(run);
    plant.journaled(thermal);
    plant.journaled(moisture);
    signal(
        plant,
        points::draw(index),
        &format!("{tag}-draw"),
        "m3/h",
        "Simulated discharge flow the bool_flow element drives",
        &group_name,
    );
    signal(
        plant,
        points::run(index),
        &format!("{tag}-run"),
        "",
        "Run feedback contact",
        &group_name,
    );
    signal(
        plant,
        points::thermal(index),
        &format!("{tag}-thermal"),
        "",
        "Thermal-overload contact",
        &group_name,
    );
    signal(
        plant,
        points::moisture(index),
        &format!("{tag}-moisture"),
        "",
        "Moisture ingress contact",
        &group_name,
    );
    signal(
        plant,
        points::cmd(index),
        &format!("{tag}-cmd"),
        "",
        "Field run command",
        &group_name,
    );

    // The run contact loopback: the sim observes the driven command —
    // `connect`'s `from` is the observing `In` point, `to` the driving
    // `Out` point.
    plant.connect(run, cmd);

    // Writable operator points: manual mode, hand request, out of
    // service. `mode` and `out_of_service` carry the durable record's
    // mode-change and managed-state transitions; `hand` is a demand
    // request — its writes are already the attributed receipts, so it
    // stays off the journaled set.
    let mode = plant.internal_input::<bool>(PointId(base), false, true);
    let hand = plant.internal_input::<bool>(PointId(base + 1), false, true);
    let oos = plant.internal_input::<bool>(PointId(base + 2), false, true);
    plant.journaled(mode);
    plant.journaled(oos);
    signal(
        plant,
        PointId(base),
        &format!("{tag}-mode"),
        "",
        "Manual takeover — false auto, true hand",
        &group_name,
    );
    signal(
        plant,
        PointId(base + 1),
        &format!("{tag}-hand"),
        "",
        "Operator's hand run request while manual",
        &group_name,
    );
    signal(
        plant,
        PointId(base + 2),
        &format!("{tag}-oos"),
        "",
        "Out of service — inhibits auto and hand operation",
        &group_name,
    );

    // Carriers and consumers: the group's cmd_i output, the inverted
    // mode, the inverted out-of-service, station power-ok, and the
    // motor's fault flag each fan out through an `Out`/`In` pair per
    // consumer. The `auto`/`oos-ok`/`power-ok`/`avail` carriers seed
    // `true` — the cold-start image reads in-auto, in-service, powered
    // until the first computed values land.
    let group_cmd = plant.internal_output::<bool>(PointId(base + 3), false);
    let group_cmd_in = plant.internal_input::<bool>(PointId(base + 4), false, false);
    let auto = plant.internal_output::<bool>(PointId(base + 5), true);
    let auto_leg_in = plant.internal_input::<bool>(PointId(base + 6), false, false);
    let auto_avail_in = plant.internal_input::<bool>(PointId(base + 7), false, false);
    let oos_ok = plant.internal_output::<bool>(PointId(base + 8), true);
    let oos_ok_avail_in = plant.internal_input::<bool>(PointId(base + 9), false, false);
    let oos_ok_guard_in = plant.internal_input::<bool>(PointId(base + 10), false, false);
    let power_ok_in = plant.internal_input::<bool>(PointId(base + 11), false, false);
    let fault = plant.internal_output::<bool>(PointId(base + 12), false);
    let fault_group_in = plant.internal_input::<bool>(PointId(base + 13), false, false);
    let fault_alarm_in = plant.internal_input::<bool>(PointId(base + 14), false, false);
    let fault_sup = plant.internal_output::<bool>(PointId(base + 15), false);
    let fault_sup_in = plant.internal_input::<bool>(PointId(base + 16), false, false);
    let thermal_ok = plant.internal_output::<bool>(PointId(base + 17), true);
    let thermal_ok_in = plant.internal_input::<bool>(PointId(base + 18), false, false);
    let moisture_ok = plant.internal_output::<bool>(PointId(base + 19), true);
    let moisture_ok_in = plant.internal_input::<bool>(PointId(base + 20), false, false);
    let avail_carrier = plant.internal_output::<bool>(PointId(base + 21), true);
    let avail_in = plant.internal_input::<bool>(PointId(base + 22), false, false);
    // The protection carriers: the dry-run flag's delivered copy, the
    // interlock's `tripped` and pass-through, and the inverted
    // `protections-ok` pair the command guard and the hand holdout
    // read. `protections-ok` seeds `true` like the other healthy-state
    // carriers — cold start reads protected until the first computed
    // values land.
    let below_cutoff_in = plant.internal_input::<bool>(PointId(base + 23), false, false);
    let protect_tripped = plant.internal_output::<bool>(PointId(base + 24), false);
    let protect_tripped_in = plant.internal_input::<bool>(PointId(base + 25), false, false);
    let protections_ok = plant.internal_output::<bool>(PointId(base + 26), true);
    let protections_ok_in = plant.internal_input::<bool>(PointId(base + 27), false, false);
    let protect_out = plant.internal_output::<f64>(PointId(base + 28), 0.0);
    // The cause guards' pass-through carriers — unused like
    // `protect-out`: each guard exists for its `tripped` flag.
    let thermal_guard_out = plant.internal_output::<f64>(PointId(base + 29), 0.0);
    let moisture_guard_out = plant.internal_output::<f64>(PointId(base + 30), 0.0);

    // The proven fault, the aggregated availability, and the
    // protection state are protection-relevant status — `journaled`.
    plant.journaled(fault);
    plant.journaled(avail_carrier);
    plant.journaled(protect_tripped);
    plant.journaled(protections_ok);
    for (offset, name, description) in [
        (3, "group-cmd", "The pump group's automatic run request"),
        (4, "group-cmd-in", "Group request delivered to the auto leg"),
        (5, "auto", "In auto — the inverted manual-mode point"),
        (6, "auto-leg-in", "In-auto delivered to the auto leg"),
        (7, "auto-avail-in", "In-auto delivered to availability"),
        (
            8,
            "oos-ok",
            "In service — the inverted out-of-service point",
        ),
        (9, "oos-ok-avail-in", "In-service delivered to availability"),
        (
            10,
            "oos-ok-guard-in",
            "In-service delivered to the protection permissive",
        ),
        (
            11,
            "power-ok-in",
            "Station power-ok delivered to availability",
        ),
        (12, "fault", "The motor's proven command/feedback fault"),
        (
            13,
            "fault-group-in",
            "Motor fault delivered to the pump group",
        ),
        (14, "fault-alarm-in", "Motor fault delivered to the alarm"),
        (
            15,
            "fault-sup",
            "Out-of-service state delivered to the fault alarm's suppression",
        ),
        (
            16,
            "fault-sup-in",
            "The fault alarm's suppression condition",
        ),
        (
            17,
            "thermal-ok",
            "Thermal contact healthy — the inverted contact",
        ),
        (
            18,
            "thermal-ok-in",
            "Thermal-healthy delivered to availability",
        ),
        (
            19,
            "moisture-ok",
            "Moisture contact healthy — the inverted contact",
        ),
        (
            20,
            "moisture-ok-in",
            "Moisture-healthy delivered to availability",
        ),
        (21, "avail", "Aggregated availability for the pump group"),
        (22, "avail-in", "Availability delivered to the pump group"),
        (
            23,
            "below-cutoff-in",
            "Dry-run cutoff delivered to the protection interlock",
        ),
        (
            24,
            "protect-tripped",
            "Protection interlock tripped — a condition asserted or untrusted",
        ),
        (
            25,
            "protect-tripped-in",
            "Protection trip delivered to its inversion",
        ),
        (
            26,
            "protections-ok",
            "Protections clear and trusted — the inverted interlock trip",
        ),
        (
            27,
            "protections-ok-in",
            "Protections-clear delivered to the guard and holdout",
        ),
        (
            28,
            "protect-out",
            "The protection interlock's analog pass-through — unused",
        ),
        (
            29,
            "thermal-guard-out",
            "The thermal cause guard's analog pass-through — unused",
        ),
        (
            30,
            "moisture-guard-out",
            "The moisture cause guard's analog pass-through — unused",
        ),
    ] {
        signal(
            plant,
            PointId(base + offset),
            &format!("{tag}-{name}"),
            "",
            description,
            &group_name,
        );
    }

    // The availability aggregation and the manual-takeover gates.
    let invert = || parameters([("invert", Value::Bool(true))]);
    let inv_mode = plant.add(DigitalInputSpec::new(invert()));
    let inv_oos = plant.add(DigitalInputSpec::new(invert()));
    let inv_thermal = plant.add(DigitalInputSpec::new(invert()));
    let inv_moisture = plant.add(DigitalInputSpec::new(invert()));
    let avail = plant.add(BoolGateSpec::new(
        parameters([("operation", Value::Int(GATE_AND))]),
        5,
    ));
    let auto_leg = plant.add(BoolGateSpec::new(
        parameters([("operation", Value::Int(GATE_AND))]),
        2,
    ));
    let hand_leg = plant.add(BoolGateSpec::new(
        parameters([("operation", Value::Int(GATE_AND))]),
        3,
    ));
    let select = plant.add(BoolGateSpec::new(
        parameters([("operation", Value::Int(GATE_OR))]),
        2,
    ));
    let guard = plant.add(BoolGateSpec::new(
        parameters([("operation", Value::Int(GATE_AND))]),
        2,
    ));
    // The protection aggregation: the `interlock` trips on an asserted
    // *or* untrusted condition alike — the thermal, moisture, and
    // power-fail contacts and the chain's `below_cutoff` as its trips,
    // in-service as its permissive, and the delivered selected level
    // on its `in` so an untrusted measurement cannot prove the dry-run
    // trip clear. The inverted `tripped` guards the command in both
    // modes, and the `timer` at `min_off_ticks` holds the hand leg out
    // until the protections have stood — the group's
    // `min_off`/`restage` discipline reaches only the auto leg.
    let protect = plant.add(InterlockSpec::new(
        parameters([("safe_value", Value::Float(0.0))]),
        4,
    ));
    // The cause guards — the annunciation half for the per-pump
    // contacts, the station power guard's shape repeated per cause:
    // a held `Good` `in` and `permissive` leave `tripped` reporting
    // the contact alone, asserted or untrusted alike. Each contact
    // alarm binds its guard's `tripped` directly, so a degraded
    // contact can no longer trip the pump while its cause alarm
    // stays clean.
    let thermal_guard = plant.add(InterlockSpec::new(
        parameters([("safe_value", Value::Float(0.0))]),
        1,
    ));
    let moisture_guard = plant.add(InterlockSpec::new(
        parameters([("safe_value", Value::Float(0.0))]),
        1,
    ));
    let inv_protect = plant.add(DigitalInputSpec::new(invert()));
    let holdout = plant.add(TimerSpec::new(parameters([(
        "delay_ticks",
        Value::Int(config.min_off_ticks),
    )])));
    let motor = plant.add(MotorSpec::new(parameters([(
        "fault_ticks",
        Value::Int(config.motor_fault_ticks),
    )])));

    // The pump's three managed alarms: the fault alarm declares
    // `oos`/`suppress` — bound below to the pump's own out-of-service
    // state, so a deliberately offline pump's fault stays named and
    // countable without annunciating — while the contact alarms
    // declare no lifecycle inputs.
    let equipment = config.alarms.equipment;
    let fault_alarm = plant.add(ManagedBoolLatchingAlarmSpec::new(
        managed_parameters(equipment, 0),
        ManagedInputs {
            oos: true,
            suppress: true,
            ..ManagedInputs::default()
        },
        rationalization(
            "The pump cannot run while its fault stands",
            "Clear the motor fault and reset the pump",
            &format!("{tag}-fault-alarm"),
        ),
    ));
    let thermal_alarm = plant.add(ManagedBoolLatchingAlarmSpec::new(
        managed_parameters(equipment, 0),
        ManagedInputs::default(),
        rationalization(
            "The motor overheats and the pump trips out",
            "Investigate the thermal overload and reset the contact",
            &format!("{tag}-thermal-alarm"),
        ),
    ));
    let moisture_alarm = plant.add(ManagedBoolLatchingAlarmSpec::new(
        managed_parameters(equipment, 0),
        ManagedInputs::default(),
        rationalization(
            "Water ingress degrades the motor insulation",
            "Schedule a seal inspection for the pump",
            &format!("{tag}-moisture-alarm"),
        ),
    ));

    // Mode and service inversions; the group request carrier.
    plant.connect(mode, &inv_mode.input);
    plant.connect(oos, &inv_oos.input);
    plant.connect(thermal, &inv_thermal.input);
    plant.connect(moisture, &inv_moisture.input);
    plant.connect(group.cmd(index + 1), group_cmd);
    plant.connect(group_cmd_in, group_cmd);
    plant.connect(&inv_mode.out, auto);
    plant.connect(auto_leg_in, auto);
    plant.connect(auto_avail_in, auto);
    plant.connect(&inv_oos.out, oos_ok);
    plant.connect(oos_ok_avail_in, oos_ok);
    plant.connect(oos_ok_guard_in, oos_ok);
    plant.connect(power_ok_in, power_ok);

    // avail_i = in-auto and in-service and power-ok and thermal-ok and
    // moisture-ok — the aggregated availability, delivered to the
    // group through its own seeded carrier pair.
    plant.connect(&inv_thermal.out, thermal_ok);
    plant.connect(thermal_ok_in, thermal_ok);
    plant.connect(&inv_moisture.out, moisture_ok);
    plant.connect(moisture_ok_in, moisture_ok);
    plant.connect(auto_avail_in, avail.input(1));
    plant.connect(oos_ok_avail_in, avail.input(2));
    plant.connect(power_ok_in, avail.input(3));
    plant.connect(thermal_ok_in, avail.input(4));
    plant.connect(moisture_ok_in, avail.input(5));
    plant.connect(&avail.out, avail_carrier);
    plant.connect(avail_in, avail_carrier);
    plant.connect(avail_in, group.avail(index + 1));

    // The protection aggregation: the raw contacts bind the
    // interlock's trips directly — a point may feed many port inputs —
    // so an asserted *or* untrusted thermal, moisture, power-fail, or
    // dry-run condition trips the pump; `oos` stands as the permissive
    // and the delivered selected level on `in` fails the same way. The
    // inverted `tripped` is `protections-ok`; the `timer` holds the
    // hand leg out for `min_off_ticks` after the protections clear.
    plant.connect(below_cutoff_in, below_cutoff);
    plant.connect(level_chain_in, &protect.input);
    plant.connect(oos_ok_guard_in, &protect.permissive);
    plant.connect(thermal, protect.trip(1));
    plant.connect(moisture, protect.trip(2));
    plant.connect(power_fail, protect.trip(3));
    plant.connect(below_cutoff_in, protect.trip(4));
    plant.connect(&protect.out, protect_out);
    plant.connect(&protect.tripped, protect_tripped);
    plant.connect(protect_tripped_in, protect_tripped);
    plant.connect(protect_tripped_in, &inv_protect.input);
    plant.connect(&inv_protect.out, protections_ok);
    plant.connect(protections_ok_in, protections_ok);
    plant.connect(protections_ok_in, &holdout.input);

    // The per-pump cause guards: one `interlock` per contact whose
    // `tripped` feeds the cause alarm's condition — the port-to-port
    // wire synthesizes the delivered copy, so the asserted *or*
    // untrusted contact annunciates on the same reading that trips
    // the pump.
    plant.connect(guard_anchor, &thermal_guard.input);
    plant.connect(guard_true, &thermal_guard.permissive);
    plant.connect(thermal, thermal_guard.trip(1));
    plant.connect(&thermal_guard.out, thermal_guard_out);
    plant.connect(&thermal_guard.tripped, &thermal_alarm.input);
    plant.connect(guard_anchor, &moisture_guard.input);
    plant.connect(guard_true, &moisture_guard.permissive);
    plant.connect(moisture, moisture_guard.trip(1));
    plant.connect(&moisture_guard.out, moisture_guard_out);
    plant.connect(&moisture_guard.tripped, &moisture_alarm.input);

    // The manual-takeover shape: `motor.cmd = ((group cmd and not
    // mode) or (hand and mode and the held protection set)) and
    // protections-ok` — the operator's `hand` request stays a demand
    // the declared protections bound, not a bypass. A pump held in
    // manual feeds the `any-manual` aggregation the `none-available`
    // alarm suppresses on.
    plant.connect(group_cmd_in, auto_leg.input(1));
    plant.connect(auto_leg_in, auto_leg.input(2));
    plant.connect(hand, hand_leg.input(1));
    plant.connect(mode, hand_leg.input(2));
    plant.connect(&holdout.out, hand_leg.input(3));
    plant.connect(&auto_leg.out, select.input(1));
    plant.connect(&hand_leg.out, select.input(2));
    plant.connect(&select.out, guard.input(1));
    plant.connect(protections_ok_in, guard.input(2));
    plant.connect(&guard.out, &motor.cmd);
    plant.connect(mode, any_manual.input(index + 1));
    plant.connect(run, &motor.run);
    plant.connect(&motor.out, cmd);

    // The group's run/fault feedback.
    plant.connect(run, group.run(index + 1));
    plant.connect(&motor.fault, fault);
    plant.connect(fault_group_in, fault);
    plant.connect(fault_group_in, group.fault(index + 1));
    plant.connect(fault_alarm_in, fault);

    // The fault alarm's `oos` binds the pump's `oos` point directly,
    // while its `suppress` reads the pass-through copy a
    // `digital-input` composes: a component binds each point once, so
    // the same declared state reaches the second input through the
    // carrier pair one scan later.
    let oos_copy = plant.add(DigitalInputSpec::new(parameters([(
        "invert",
        Value::Bool(false),
    )])));
    plant.connect(oos, &oos_copy.input);
    plant.connect(&oos_copy.out, fault_sup);
    plant.connect(fault_sup_in, fault_sup);

    let fault_alarm_layout = wire_pump_alarm(
        plant,
        PUMP_ALARM_BASE + 3 * i,
        &fault_alarm.ack,
        &fault_alarm.managed,
        &fault_alarm.alarm,
        &fault_alarm.unacknowledged,
        oos,
        fault_sup_in,
        &format!("{tag}-fault"),
        &group_name,
    );
    plant.connect(fault_alarm_in, &fault_alarm.input);
    let thermal_alarm_layout = wire_pump_alarm(
        plant,
        PUMP_ALARM_BASE + 3 * i + 1,
        &thermal_alarm.ack,
        &thermal_alarm.managed,
        &thermal_alarm.alarm,
        &thermal_alarm.unacknowledged,
        oos,
        fault_sup_in,
        &format!("{tag}-thermal"),
        &group_name,
    );
    let moisture_alarm_layout = wire_pump_alarm(
        plant,
        PUMP_ALARM_BASE + 3 * i + 2,
        &moisture_alarm.ack,
        &moisture_alarm.managed,
        &moisture_alarm.alarm,
        &moisture_alarm.unacknowledged,
        oos,
        fault_sup_in,
        &format!("{tag}-moisture"),
        &group_name,
    );

    PumpLayout {
        cmd: points::cmd(index),
        run: points::run(index),
        draw: points::draw(index),
        mode: PointId(base),
        hand: PointId(base + 1),
        out_of_service: PointId(base + 2),
        fault: PointId(base + 12),
        avail: PointId(base + 21),
        protect_tripped: PointId(base + 24),
        protections_ok: PointId(base + 26),
        fault_alarm: fault_alarm_layout,
        thermal_alarm: thermal_alarm_layout,
        moisture_alarm: moisture_alarm_layout,
    }
}
