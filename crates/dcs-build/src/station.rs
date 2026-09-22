//! The reference duty/standby pumping station — the issue-#220
//! composition at the builder seam decision 31 fixed.
//!
//! [`pumping_station`] composes the station entirely through typed spec
//! handles and emits the versioned [`PlantModel`] document; the checked-
//! in copy lives at `crates/dcs-demo/fixtures/pump_station.json` beside
//! its dynamics document `pump_station_dynamics.json` — the recorded
//! fixture location for the issue. `dcs-build/tests/pump_station.rs`
//! asserts the emitted document equals that artifact.
//!
//! ## The composition
//!
//! - **Measurement (decision 42):** a `failover-select` over the primary
//!   and backup level channels feeds a `threshold-chain`, whose `demand`
//!   stage count feeds `pump-group.demand`. A port's output may drive
//!   only one endpoint, so fan-out goes through a declared internal
//!   `Out` carrier plus one `In` consumer per reader — each link
//!   crossing the one-scan boundary.
//! - **Pumps (decision 41):** `pumps` `motor` instances sit under one
//!   `pump-group`; the group's `cmd_i`/`run_i`/`fault_i`/`avail_i`
//!   indexed ports wire per pump as the decision records. `avail_i` is
//!   the plant's availability aggregation: a `bool-gate` `and` of
//!   in-auto (`not mode_i`), in-service (`not out_of_service_i`),
//!   station power, and the healthy thermal/moisture contacts.
//! - **Manual takeover (decision 87's recorded shape):** per pump, a
//!   writable `mode_i` point selects between the group's `cmd_i` and
//!   the operator's writable `hand_i` request — `motor.cmd_i =
//!   ((cmd_i and not mode_i) or (hand_i and mode_i and the held
//!   protection set)) and protections-ok`. A per-pump `interlock`
//!   aggregates the protections under the kind's fail-safe quality
//!   rule — the power-fail, thermal, and moisture contacts, the
//!   chain's `below_cutoff`, and in-service as its permissive, with
//!   the selected level on its `in` so an untrusted measurement
//!   cannot prove the dry-run trip clear — and the inverted
//!   `tripped` guards the command in both modes while a `timer` at
//!   `min_off_ticks` holds the hand leg out until the protections
//!   have stood.
//! - **Alarms (decision 43's set on the decision-71–73 managed
//!   kinds):** every alarm is a wired managed latching instance with a
//!   writable internal `ack` point — `managed-latching-alarm`s on the
//!   selected level at the declared `high` and `cutoff` thresholds,
//!   `managed-bool-latching-alarm`s on each motor's fault flag, the
//!   failover's `backup_active` and `backup_unhealthy`, the group's
//!   `none_available`/`all_faulted`, the per-pump thermal and moisture
//!   contacts, and the station power-fail field point. Every managed
//!   status point is `journaled` (decision 74), and the lifecycle
//!   wiring is the station's declared WW-ALM-002 site policy — open
//!   customer assumptions carried as data:
//!   - the high-level alarm is never-shelvable twice over —
//!     `max_shelve_ticks = 0` and its `shelve` port bound to a
//!     read-only point, so a shelve request answers `NotWritable` at
//!     submission, the documented rejection path;
//!   - the low-level alarm is the shelvable nuisance case — an
//!     extended low well holds the condition while the site works —
//!     its `shelve` bound to a writable point under
//!     `config.lal_max_shelve_ticks`, the receipted, actor-attributed
//!     command path;
//!   - every per-pump fault alarm takes the pump's declared
//!     maintenance-inhibit state as its managed surface: `oos` binds
//!     the pump's own writable `out_of_service` point — the declared
//!     state's writable point covering the out-of-service path — and
//!     `suppress` reads the same state through the delivered copy a
//!     `digital-input` pass-through composes, a component binding each
//!     point once. The textbook designed suppression (decision 73): a
//!     fault alarm on a deliberately offline machine stays named and
//!     countable without annunciating.
//!
//! ## The declared point-id scheme
//!
//! Field points occupy fixed blocks the checked-in dynamics document is
//! written against — `10` level-primary, `11` level-backup, `12`
//! inflow, `13` net-flow, `20+i` per-pump draw, `40+i` run, `60+i`
//! thermal, `80+i` moisture, `100+i` command, `120` power-fail (`i` the
//! 0-based pump index, `pumps <= 20`). Internal carriers start at `200`,
//! per-pump internal blocks at `300 + 32·i`, and every alarm — station
//! and per-pump — owns a ten-point block at `1000 + 10·a` with the
//! managed layout the reference compositions share: `ack`/`shelve`/
//! `oos` at offsets 0–2 where the instance declares the input,
//! `alarm`/`unacknowledged`/`shelved`/`suppressed`/`out_of_service` at
//! 3–7. The station alarms take `a` = 0–6 in declaration order and pump
//! `i`'s fault/thermal/moisture alarms `a` = 7 + 3·i + 0/1/2; every
//! point's signal sits at `10000 + point`. The scheme is deterministic
//! in declaration order, so identical builder invocations emit
//! identical documents.
//!
//! Bool state signals and the index-valued `duty` declare an empty
//! unit — a deliberate "unitless" marker rather than an omitted one, so
//! the document lints clean.

use crate::specs::{
    BoolGateInstance, BoolGateSpec, DigitalInputSpec, FailoverSelectSpec, InterlockSpec,
    ManagedAlarmHandles, ManagedBoolLatchingAlarmSpec, ManagedInputs, ManagedLatchingAlarmSpec,
    MotorSpec, PumpGroupInstance, PumpGroupSpec, ThresholdChainSpec, TimerSpec,
};
use crate::{
    BuildError, ChannelRef, Direction, InPoint, OutPoint, PlantBuilder, PointId, SignalId, Sink,
    Source, Value, parameters,
};
use dcs_model::{ComponentId, PlantModel, Rationalization};

/// Field point ids — the fixed block the dynamics document addresses.
pub mod points {
    use crate::PointId;

    /// The primary wet-well level measurement (`Float`, `In`).
    pub const LEVEL_PRIMARY: PointId = PointId(10);
    /// The backup wet-well level measurement (`Float`, `In`).
    pub const LEVEL_BACKUP: PointId = PointId(11);
    /// The declared station inflow (`Float`, `In`) — a dynamics input.
    pub const INFLOW: PointId = PointId(12);
    /// The summed net flow the level integrator advances (`Float`, `In`).
    pub const NET_FLOW: PointId = PointId(13);
    /// Pump `index`'s simulated discharge flow (`Float`, `In`) — the
    /// `bool_flow` element's output.
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
    /// The station power-fail contact (`Bool`, `In`).
    pub const POWER_FAIL: PointId = PointId(120);
}

/// Internal carrier ids — the station-level wiring.
mod carriers {
    pub const LEVEL_SEL: u64 = 200;
    pub const LEVEL_CHAIN: u64 = 201;
    pub const LEVEL_LAH: u64 = 202;
    pub const LEVEL_LAL: u64 = 203;
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
    pub const BACKUP_UNHEALTHY: u64 = 222;
    pub const BACKUP_UNHEALTHY_IN: u64 = 223;
    /// The `power-ok` copy the station power interlock's permissive
    /// reads.
    pub const POWER_OK_ALARM_IN: u64 = 224;
    /// The held analog feed the power interlock's `in` binds — the
    /// declared conditions are discrete, so a constant `Good` input
    /// leaves `tripped` reporting the permissive alone.
    pub const POWER_GUARD_ANCHOR: u64 = 225;
    /// The power interlock's unused pass-through carrier.
    pub const POWER_GUARD_OUT: u64 = 226;
    /// The power interlock's `tripped` carrier — `journaled`: the
    /// station power trip's durable transition, asserted on the
    /// contact's value or its untrusted quality alike.
    pub const POWER_TRIPPED: u64 = 227;
    /// The `tripped` copy delivered to the power-fail alarm.
    pub const POWER_TRIPPED_IN: u64 = 228;
    /// The any-pump-manual carrier — the `none-available` alarm's
    /// declared suppression condition.
    pub const ANY_MANUAL: u64 = 229;
    /// The delivered copy the `none-available` alarm's `suppress`
    /// reads.
    pub const ANY_MANUAL_IN: u64 = 230;
}

/// The first per-pump internal block: pump `i` owns
/// `PUMP_BASE + i * PUMP_STRIDE .. +PUMP_STRIDE`.
const PUMP_BASE: u64 = 300;
const PUMP_STRIDE: u64 = 32;
/// Alarm points: alarm `a` owns `ALARM_BASE + a * 10 .. +10` with the
/// managed layout — `ack`/`shelve`/`oos` at offsets 0–2 where declared,
/// `alarm`/`unacknowledged`/`shelved`/`suppressed`/`out_of_service` at
/// 3–7.
const ALARM_BASE: u64 = 1000;
/// The first per-pump alarm index: pump `i`'s fault/thermal/moisture
/// alarms take `PUMP_ALARM_BASE + 3·i` + 0/1/2, after the seven station
/// alarms.
const PUMP_ALARM_BASE: u64 = 7;
/// Every point's signal id is `SIGNAL_BASE + point`.
const SIGNAL_BASE: u64 = 10_000;

/// `bool-gate`'s `operation` codes — `GateOperation::And`/`Or` from
/// `dcs-blocks`, mirrored as data because `dcs-build` cannot depend on
/// the blocks crate.
const GATE_AND: i64 = 0;
const GATE_OR: i64 = 1;

/// The bound a single-sided level alarm parks its unused limit at —
/// far outside any measurable span, so only the declared threshold side
/// can trip.
const PARKED_LIMIT: f64 = 1.0e9;

/// `net-flow`'s declared freshness budget — the one `stale_after_ticks`
/// declaration the reference station carries, on the point the QA
/// lane's stale-freshness scenario probes. Sizing: a healthy field read
/// lags the scan by a tick or two at most, so `5` never trips in
/// service, while a stopped writer freezes the driver stamps far past
/// it inside the failover budget that bounds the outage. The point is
/// deliberately one no component port consumes — the staleness evidence
/// is presentational, not a perturbation of the control path.
const NET_FLOW_STALE_AFTER: u64 = 5;

/// The station's tunable contract — setpoints, staging policy, and
/// alarm hysteresis. [`reference`](Self::reference) is the checked-in
/// document's configuration.
#[derive(Debug, Clone, PartialEq)]
pub struct PumpStationConfig {
    /// The pump count `N`: `pump-group`'s `cmd_i`/`run_i`/`fault_i`/
    /// `avail_i` families and the per-pump wiring repeat `1..=N` times.
    /// The point-id scheme admits at most 20.
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
    /// The demand the chain emits while the selected level is untrusted
    /// — `0`, `1`, or `2`.
    pub on_bad_demand: i64,
    /// `pump-group`'s rotation policy code.
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
    /// The level alarms' hysteresis band, in metres.
    pub level_alarm_hysteresis: f64,
    /// The low-level alarm's `max_shelve_ticks` — the shelvable
    /// nuisance alarm's bound, the request's asserting scan counting as
    /// the first. Which station alarms are shelvable and their bounds
    /// are the site's declared WW-ALM-002 policy — open customer
    /// assumptions carried as data, not defaults.
    pub lal_max_shelve_ticks: i64,
}

impl PumpStationConfig {
    /// The reference station the checked-in documents record: a duplex
    /// set alternating duty each cycle, the documented wet-well
    /// setpoints, and zero demand on an untrusted measurement.
    pub fn reference() -> Self {
        Self {
            pumps: 2,
            cutoff: 0.5,
            stop: 1.0,
            start: 2.0,
            lag_start: 3.0,
            high: 4.0,
            on_bad_demand: 0,
            rotation: 0,
            rotation_ticks: None,
            start_delay_ticks: 1,
            restage_delay_ticks: 0,
            min_off_ticks: 2,
            motor_fault_ticks: 2,
            level_alarm_hysteresis: 0.1,
            lal_max_shelve_ticks: 8,
        }
    }
}

/// One alarm's place in the emitted document: the latching instance and
/// the points its writable ack and two outputs landed on.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AlarmLayout {
    /// The alarm component instance's id.
    pub component: ComponentId,
    /// The writable internal `In` point the operator ack lands on.
    pub ack: PointId,
    /// The internal `Out` point carrying the standing `alarm` output.
    pub alarm: PointId,
    /// The internal `Out` point carrying the `unacknowledged` latch.
    pub unacknowledged: PointId,
}

/// One managed alarm's place in the emitted document — the two-flag
/// surface plus the decision-71 managed status points, and the points
/// the declared `shelve`/`oos` lifecycle inputs bind.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ManagedAlarmLayout {
    /// The alarm component instance's id.
    pub component: ComponentId,
    /// The writable internal `In` point the operator ack lands on.
    pub ack: PointId,
    /// The point the `shelve` input binds — `Some` only where the
    /// instance declares the port. The point's `writable` flag is the
    /// declared shelving policy: a writable point carries the
    /// receipted, actor-attributed operator request; a read-only one
    /// is the never-shelvable declaration whose writes answer
    /// `NotWritable` at submission.
    pub shelve: Option<PointId>,
    /// The point the `oos` input binds — `Some` only where the
    /// instance declares the port: the operator's out-of-service
    /// command point, or the pump's own maintenance-inhibit point for
    /// the per-pump set.
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

/// Pump `index`'s place in the emitted document.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PumpLayout {
    /// The 1-based pump index — the `cmd_i`/`run_i`/`fault_i`/`avail_i`
    /// family member.
    pub index: usize,
    /// The field command `Out` point (`cmd` channel).
    pub cmd: PointId,
    /// The run-feedback field `In` point.
    pub run: PointId,
    /// The simulated draw field `In` point a `bool_flow` drives.
    pub draw: PointId,
    /// The thermal-overload contact field `In` point.
    pub thermal: PointId,
    /// The moisture contact field `In` point.
    pub moisture: PointId,
    /// The writable internal `mode` point — `false` auto, `true` manual.
    pub mode: PointId,
    /// The writable internal `hand` run request.
    pub hand: PointId,
    /// The writable internal out-of-service flag.
    pub out_of_service: PointId,
    /// The carrier carrying the group's `cmd_i` request — the auto
    /// leg's input; monitoring distinguishes the group's request from
    /// the driven `cmd`.
    pub group_cmd: PointId,
    /// The motor's proven fault carrier — the `fault` output fanned
    /// out to the group and the alarm, `journaled`.
    pub fault: PointId,
    /// The aggregated `avail_i` carrier the group consumes —
    /// `journaled`.
    pub avail: PointId,
    /// The protection interlock's `tripped` carrier — `journaled`.
    pub protect_tripped: PointId,
    /// The inverted `protections-ok` carrier the command guard and the
    /// hand holdout read — `journaled`.
    pub protections_ok: PointId,
    /// The `motor` instance's id.
    pub motor: ComponentId,
    /// The managed motor-fault alarm — `oos` bound to the pump's
    /// `out_of_service` point, `suppress` to its delivered copy.
    pub fault_alarm: ManagedAlarmLayout,
    /// The managed thermal-overload alarm.
    pub thermal_alarm: ManagedAlarmLayout,
    /// The managed moisture alarm.
    pub moisture_alarm: ManagedAlarmLayout,
}

/// Where everything the composition declares landed — the ids the
/// dynamics document, the scripted run, and any embedding surface
/// address.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PumpStationLayout {
    /// The primary level field point — the integrator's output.
    pub level_primary: PointId,
    /// The backup level field point — the lagged copy.
    pub level_backup: PointId,
    /// The failover-selected level carrier the chain and alarms read.
    pub level_selected: PointId,
    /// The declared inflow field point.
    pub inflow: PointId,
    /// The summed net flow field point.
    pub net_flow: PointId,
    /// The station power-fail field point.
    pub power_fail: PointId,
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
    /// The chain's `below_cutoff` flag carrier.
    pub below_cutoff: PointId,
    /// The chain's `high_level` flag carrier.
    pub high_level: PointId,
    /// The failover's `backup_active` carrier.
    pub backup_active: PointId,
    /// The failover's `backup_unhealthy` carrier — `journaled`; asserts
    /// while the unused backup's own sample is untrusted.
    pub backup_unhealthy: PointId,
    /// The group's `none_available` carrier.
    pub none_available: PointId,
    /// The group's `all_faulted` carrier.
    pub all_faulted: PointId,
    /// The station power interlock's `tripped` carrier — the
    /// `power-fail` alarm's quality-aware condition, `journaled`.
    pub power_tripped: PointId,
    /// The any-pump-manual carrier — the `none-available` alarm's
    /// declared suppression condition.
    pub any_manual: PointId,
    /// The `failover-select` instance's id.
    pub failover: ComponentId,
    /// The `threshold-chain` instance's id.
    pub threshold_chain: ComponentId,
    /// The `pump-group` instance's id.
    pub pump_group: ComponentId,
    /// Per-pump layouts, in `index` order.
    pub pumps: Vec<PumpLayout>,
    /// The managed high-level latching alarm (trips at `config.high`) —
    /// never-shelvable: `max_shelve_ticks = 0` and a read-only bound
    /// `shelve` point, so a shelve request answers `NotWritable`.
    pub high_level_alarm: ManagedAlarmLayout,
    /// The managed low-level latching alarm (trips at `config.cutoff`) —
    /// the shelvable nuisance case, `shelve` writable under
    /// `config.lal_max_shelve_ticks`.
    pub low_level_alarm: ManagedAlarmLayout,
    /// The managed backup-measurement-serving alarm.
    pub backup_active_alarm: ManagedAlarmLayout,
    /// The managed backup-measurement-unhealthy alarm — the
    /// standby-loss annunciation QA's issue-#502 finding called for.
    pub backup_unhealthy_alarm: ManagedAlarmLayout,
    /// The managed no-pump-available alarm.
    pub none_available_alarm: ManagedAlarmLayout,
    /// The managed every-pump-faulted alarm.
    pub all_faulted_alarm: ManagedAlarmLayout,
    /// The managed station power-fail alarm.
    pub power_fail_alarm: ManagedAlarmLayout,
}

/// The composed station: the emitted document plus the layout every
/// declared id landed on.
#[derive(Debug, Clone)]
pub struct PumpStation {
    /// The versioned plant-model document.
    pub model: PlantModel,
    /// The id map into it.
    pub layout: PumpStationLayout,
}

/// Composes the station under `config` and emits its [`PlantModel`].
///
/// Every component registers through its typed spec and every
/// connection goes through typed handles — port existence, direction,
/// and value kind are compile-time-checked where the types reach and
/// `build`-checked where they do not.
///
/// # Panics
///
/// `config.pumps` outside `1..=20` exceeds the declared point-id scheme.
pub fn pumping_station(config: &PumpStationConfig) -> Result<PumpStation, BuildError> {
    assert!(
        (1..=20).contains(&config.pumps),
        "the station's point-id scheme admits 1..=20 pumps, got {}",
        config.pumps
    );
    let mut plant = PlantBuilder::new();

    // The simulated I/O devices — all under the `sim` prefix, so the
    // standard driver registry serves them from the local SimDriver.
    let ai = plant.device("sim-ai").id;
    let di = plant.device("sim-di").id;
    let d_o = plant.device("sim-do").id;

    let level_primary_ch = plant.channel::<f64>(ai, "level-primary", Direction::In);
    let level_backup_ch = plant.channel::<f64>(ai, "level-backup", Direction::In);
    let inflow_ch = plant.channel::<f64>(ai, "inflow", Direction::In);
    let net_flow_ch = plant.channel::<f64>(ai, "net-flow", Direction::In);
    let power_fail_ch = plant.channel::<bool>(di, "power-fail", Direction::In);
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

    // Field points — `inflow`, `net-flow`, and the draws are produced by
    // the dynamics document's elements, so their handles go unused here.
    let level_primary = plant.field_input::<f64>(points::LEVEL_PRIMARY, level_primary_ch, false);
    let level_backup = plant.field_input::<f64>(points::LEVEL_BACKUP, level_backup_ch, false);
    plant.field_input::<f64>(points::INFLOW, inflow_ch, false);
    plant.field_input_stale_after::<f64>(
        points::NET_FLOW,
        net_flow_ch,
        false,
        NET_FLOW_STALE_AFTER,
    );
    let power_fail = plant.field_input::<bool>(points::POWER_FAIL, power_fail_ch, false);
    // The power-fail contact is a protection-layer reported state —
    // decision 74's durable record marks it `journaled` so its
    // transitions land in the journal beside its alarm's.
    plant.journaled(power_fail);

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

    // The shared internal carriers: the failover's selected level, the
    // chain's demand, power-ok, the status outputs, and the three alarm
    // condition consumers. A carrier's `initial` is what its consumers
    // see at scan 1's input phase — seeds read as the healthy cold-start
    // state (mid-band level, power healthy) so no alarm trips before the
    // first real samples land.
    let level_sel = plant.internal_output::<f64>(PointId(carriers::LEVEL_SEL), 1.5);
    let level_chain = plant.internal_input::<f64>(PointId(carriers::LEVEL_CHAIN), 0.0, false);
    let level_lah = plant.internal_input::<f64>(PointId(carriers::LEVEL_LAH), 0.0, false);
    let level_lal = plant.internal_input::<f64>(PointId(carriers::LEVEL_LAL), 0.0, false);
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
    let backup_unhealthy =
        plant.internal_output::<bool>(PointId(carriers::BACKUP_UNHEALTHY), false);
    let backup_unhealthy_in =
        plant.internal_input::<bool>(PointId(carriers::BACKUP_UNHEALTHY_IN), false, false);
    // Decision 87's cause-alarm wiring: `power_ok`'s delivered copy is
    // the station power interlock's permissive — false or untrusted
    // trips alike — and the `tripped` carrier pair feeds the
    // `power-fail` alarm's condition, so the cause alarm asserts on
    // the same reading availability takes. The `in` port's Float is
    // the held anchor: the declared conditions are discrete.
    let power_ok_alarm_in =
        plant.internal_input::<bool>(PointId(carriers::POWER_OK_ALARM_IN), false, false);
    let power_guard_anchor =
        plant.internal_input::<f64>(PointId(carriers::POWER_GUARD_ANCHOR), 0.0, false);
    let power_guard_out = plant.internal_output::<f64>(PointId(carriers::POWER_GUARD_OUT), 0.0);
    let power_tripped = plant.internal_output::<bool>(PointId(carriers::POWER_TRIPPED), false);
    let power_tripped_in =
        plant.internal_input::<bool>(PointId(carriers::POWER_TRIPPED_IN), false, false);
    // The `none-available` alarm's declared suppression: any pump held
    // in manual is the operator withdrawing it from the group's roster
    // — "demand stands with no pump available" is then designed state,
    // not a fault (decision 73's pattern). The carrier keeps reporting
    // truth; the `suppressed` flag names the withholding.
    let any_manual = plant.internal_output::<bool>(PointId(carriers::ANY_MANUAL), false);
    let any_manual_in =
        plant.internal_input::<bool>(PointId(carriers::ANY_MANUAL_IN), false, false);
    plant.journaled(power_tripped);

    // The protection-relevant status carriers decision 74's durable
    // record names — the chain's dry-run and high flags, the failover's
    // backup-serving and standby-health states, and the group's
    // availability roll-ups — marked `journaled` so every transition
    // lands in the journal.
    for point in [
        below_cutoff,
        high_level,
        backup_active,
        backup_unhealthy,
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
            carriers::LEVEL_CHAIN,
            "level-chain",
            "Selected level delivered to the threshold chain",
        ),
        (
            carriers::LEVEL_LAH,
            "level-lah",
            "Selected level delivered to the high-level alarm",
        ),
        (
            carriers::LEVEL_LAL,
            "level-lal",
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
            carriers::BACKUP_UNHEALTHY,
            "backup-unhealthy",
            "The backup level measurement is untrusted while the primary serves",
        ),
        (
            carriers::BACKUP_UNHEALTHY_IN,
            "backup-unhealthy-in",
            "Backup-unhealthy flag delivered to its alarm",
        ),
        (
            carriers::POWER_OK_ALARM_IN,
            "power-ok-alarm-in",
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
    ] {
        let unit = match point {
            carriers::LEVEL_SEL
            | carriers::LEVEL_CHAIN
            | carriers::LEVEL_LAH
            | carriers::LEVEL_LAL => "m",
            carriers::DEMAND | carriers::DEMAND_IN | carriers::STAGED => "pumps",
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

    // The station-level components. The failover declares its optional
    // `backup_unhealthy` output: a failed standby annunciates while the
    // primary still serves, closing the silent-redundancy-loss gap QA
    // found (issue #502).
    let failover = plant.add(FailoverSelectSpec::new(parameters([])).with_backup_unhealthy());
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
    // Decision 87: the station power interlock evaluates the
    // conditioned `power-ok` — an unasserted *or* untrusted permissive
    // trips — and its `tripped` flag is the `power-fail` alarm's
    // condition, so the cause annunciates on the same reading that
    // collapses `avail_i` (a `Bad` contact can no longer leave the
    // alarm clean while the station declares no pump available). Its
    // `in` binds a held anchor: the declared conditions are discrete.
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
    // The decision-70 codes are declared data — the site priority/class
    // vocabulary and response budgets stay an open customer assumption,
    // as does the shelving policy: which alarms are shelvable and their
    // bounds are WW-ALM-002's recorded site decisions, not defaults.
    // The high-level alarm is the never-shelvable critical
    // annunciation, twice over: `max_shelve_ticks = 0` makes a delivered
    // request inert and the declared `shelve` port binds a read-only
    // point, so a shelve command answers `NotWritable` at submission —
    // the documented rejection path the reference plant exercises.
    let lah = plant.add(ManagedLatchingAlarmSpec::new(
        parameters([
            ("low_limit", Value::Float(-PARKED_LIMIT)),
            ("high_limit", Value::Float(config.high)),
            ("hysteresis", Value::Float(config.level_alarm_hysteresis)),
            ("max_shelve_ticks", Value::Int(0)),
            ("priority", Value::Int(1)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(30)),
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
    // The low-level alarm is the shelvable nuisance case: an extended
    // low well holds the condition while the site works, so its
    // `shelve` binds a writable point — the receipted,
    // actor-attributed request path — bounded by the declared
    // `lal_max_shelve_ticks`.
    let lal = plant.add(ManagedLatchingAlarmSpec::new(
        parameters([
            ("low_limit", Value::Float(config.cutoff)),
            ("high_limit", Value::Float(PARKED_LIMIT)),
            ("hysteresis", Value::Float(config.level_alarm_hysteresis)),
            ("max_shelve_ticks", Value::Int(config.lal_max_shelve_ticks)),
            ("priority", Value::Int(1)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(30)),
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
        parameters([
            ("max_shelve_ticks", Value::Int(0)),
            ("priority", Value::Int(2)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(30)),
        ]),
        ManagedInputs::default(),
        rationalization(
            "The backup level instrument carries the station unnoticed",
            "Check the primary level instrument",
            "backup-active-alarm",
        ),
    ));
    // The standby-health annunciation — the issue-#502 alarm: the
    // failover's `backup_unhealthy` says the unused leg is already lost
    // while the primary still carries the measurement, so the alarm
    // stands before the failover would need the dead input.
    let backup_unhealthy_alarm = plant.add(ManagedBoolLatchingAlarmSpec::new(
        parameters([
            ("max_shelve_ticks", Value::Int(0)),
            ("priority", Value::Int(2)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(30)),
        ]),
        ManagedInputs::default(),
        rationalization(
            "The backup level instrument has failed while the primary still serves — failover redundancy is already lost",
            "Repair the backup level instrument before the primary fails",
            "backup-unhealthy-alarm",
        ),
    ));
    let none_available_alarm = plant.add(ManagedBoolLatchingAlarmSpec::new(
        parameters([
            ("max_shelve_ticks", Value::Int(0)),
            ("priority", Value::Int(1)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(30)),
        ]),
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
        parameters([
            ("max_shelve_ticks", Value::Int(0)),
            ("priority", Value::Int(1)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(30)),
        ]),
        ManagedInputs::default(),
        rationalization(
            "Every pump is faulted; the station cannot pump",
            "Dispatch maintenance to clear the pump faults",
            "all-faulted-alarm",
        ),
    ));
    let power_fail_alarm = plant.add(ManagedBoolLatchingAlarmSpec::new(
        parameters([
            ("max_shelve_ticks", Value::Int(0)),
            ("priority", Value::Int(1)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(30)),
        ]),
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
    plant.connect(level_chain, level_sel);
    plant.connect(level_lah, level_sel);
    plant.connect(level_lal, level_sel);
    plant.connect(&failover.backup_active, backup_active);
    plant.connect(
        failover
            .backup_unhealthy
            .as_ref()
            .expect("the station spec declares backup_unhealthy"),
        backup_unhealthy,
    );
    plant.connect(level_chain, chain.level);
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
    plant.connect(power_ok_alarm_in, power_ok);
    plant.connect(power_guard_anchor, &power_guard.input);
    plant.connect(power_ok_alarm_in, &power_guard.permissive);
    plant.connect(&power_guard.out, power_guard_out);
    plant.connect(&power_guard.tripped, power_tripped);
    plant.connect(power_tripped_in, power_tripped);
    plant.connect(&any_manual_gate.out, any_manual);
    plant.connect(any_manual_in, any_manual);
    plant.connect(backup_active_in, backup_active);
    plant.connect(none_available_in, none_available);
    plant.connect(all_faulted_in, all_faulted);
    plant.connect(backup_unhealthy_in, backup_unhealthy);

    // The station-level alarms — each latching on its condition, its
    // own writable ack point, and the declared lifecycle surface the
    // site policy names. `lah`'s `shelve` binds read-only — the
    // never-shelvable rejection path; `lal`'s binds writable — the
    // bounded, receipted operator request.
    let high_level_alarm = managed_station_alarm(
        &mut plant,
        0,
        lah.id,
        &lah.ack,
        &lah.managed,
        &lah.alarm,
        &lah.unacknowledged,
        false,
        "lah",
        "station",
    );
    plant.connect(level_lah, &lah.input);
    let low_level_alarm = managed_station_alarm(
        &mut plant,
        1,
        lal.id,
        &lal.ack,
        &lal.managed,
        &lal.alarm,
        &lal.unacknowledged,
        true,
        "lal",
        "station",
    );
    plant.connect(level_lal, &lal.input);
    let backup_active_alarm = managed_station_alarm(
        &mut plant,
        2,
        backup_alarm.id,
        &backup_alarm.ack,
        &backup_alarm.managed,
        &backup_alarm.alarm,
        &backup_alarm.unacknowledged,
        false,
        "backup-active",
        "station",
    );
    plant.connect(backup_active_in, &backup_alarm.input);
    let none_available_alarm_layout = managed_station_alarm(
        &mut plant,
        3,
        none_available_alarm.id,
        &none_available_alarm.ack,
        &none_available_alarm.managed,
        &none_available_alarm.alarm,
        &none_available_alarm.unacknowledged,
        false,
        "none-available",
        "station",
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
    let all_faulted_alarm_layout = managed_station_alarm(
        &mut plant,
        4,
        all_faulted_alarm.id,
        &all_faulted_alarm.ack,
        &all_faulted_alarm.managed,
        &all_faulted_alarm.alarm,
        &all_faulted_alarm.unacknowledged,
        false,
        "all-faulted",
        "station",
    );
    plant.connect(all_faulted_in, &all_faulted_alarm.input);
    let power_fail_alarm_layout = managed_station_alarm(
        &mut plant,
        5,
        power_fail_alarm.id,
        &power_fail_alarm.ack,
        &power_fail_alarm.managed,
        &power_fail_alarm.alarm,
        &power_fail_alarm.unacknowledged,
        false,
        "power-fail",
        "station",
    );
    plant.connect(power_tripped_in, &power_fail_alarm.input);
    let backup_unhealthy_alarm_layout = managed_station_alarm(
        &mut plant,
        6,
        backup_unhealthy_alarm.id,
        &backup_unhealthy_alarm.ack,
        &backup_unhealthy_alarm.managed,
        &backup_unhealthy_alarm.alarm,
        &backup_unhealthy_alarm.unacknowledged,
        false,
        "backup-unhealthy",
        "station",
    );
    plant.connect(backup_unhealthy_in, &backup_unhealthy_alarm.input);

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
            level_chain,
            below_cutoff,
            &any_manual_gate,
            &group,
        ));
    }

    let model = plant.build()?;
    Ok(PumpStation {
        model,
        layout: PumpStationLayout {
            level_primary: points::LEVEL_PRIMARY,
            level_backup: points::LEVEL_BACKUP,
            level_selected: PointId(carriers::LEVEL_SEL),
            inflow: points::INFLOW,
            net_flow: points::NET_FLOW,
            power_fail: points::POWER_FAIL,
            demand: PointId(carriers::DEMAND),
            duty: PointId(carriers::DUTY),
            staged: PointId(carriers::STAGED),
            duty_call: PointId(carriers::DUTY_CALL),
            lag_call: PointId(carriers::LAG_CALL),
            below_cutoff: PointId(carriers::BELOW_CUTOFF),
            high_level: PointId(carriers::HIGH_LEVEL),
            backup_active: PointId(carriers::BACKUP_ACTIVE),
            backup_unhealthy: PointId(carriers::BACKUP_UNHEALTHY),
            none_available: PointId(carriers::NONE_AVAILABLE),
            all_faulted: PointId(carriers::ALL_FAULTED),
            power_tripped: PointId(carriers::POWER_TRIPPED),
            any_manual: PointId(carriers::ANY_MANUAL),
            failover: failover.id,
            threshold_chain: chain.id,
            pump_group: group.id,
            pumps,
            high_level_alarm,
            low_level_alarm,
            backup_active_alarm,
            backup_unhealthy_alarm: backup_unhealthy_alarm_layout,
            none_available_alarm: none_available_alarm_layout,
            all_faulted_alarm: all_faulted_alarm_layout,
            power_fail_alarm: power_fail_alarm_layout,
        },
    })
}

/// An alarm's decision-70 rationalization record — the prose half of
/// the alarm contract, carried on the component instance so the plant
/// model is the master alarm database. `reference` names the standing
/// alarm `Signal` (`{prefix}-alarm`) the operator display resolves.
pub(crate) fn rationalization(
    consequence: &str,
    required_action: &str,
    reference: &str,
) -> Rationalization {
    Rationalization {
        consequence: consequence.to_string(),
        required_action: required_action.to_string(),
        reference: reference.to_string(),
    }
}

/// The 1-based tag "p101", "p102", … matching the recorded station
/// fixtures.
fn pump_tag(index: usize) -> String {
    format!("p{}", 101 + index)
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

/// Declares one managed alarm's points and wires them: the writable
/// `ack`, each declared `shelve`/`oos` request point — `shelve`
/// carrying the alarm's declared writability policy — and the five
/// status outputs. Both managed kinds expose the same
/// `id`/`ack`/`managed`/`alarm`/`unacknowledged` fields, so the one
/// helper serves either; the caller wires the alarm's `in` port — its
/// value kind differs between them — and any `suppress` input, whose
/// source is declared plant state rather than a per-alarm request
/// point.
#[allow(clippy::too_many_arguments)]
fn managed_station_alarm(
    plant: &mut PlantBuilder,
    index: u64,
    component: ComponentId,
    ack_port: &Sink<bool>,
    managed: &ManagedAlarmHandles,
    alarm_port: &Source<bool>,
    unacknowledged_port: &Source<bool>,
    shelve_writable: bool,
    prefix: &str,
    group: &str,
) -> ManagedAlarmLayout {
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

    // Decision 74's lifecycle audit: every status point is `journaled`
    // — activation, return, the latch's clear, shelving assertion and
    // expiry, suppression, and out-of-service entry and return all land
    // as durable `point_changed` entries. The declared request points
    // journal too: their transitions are the lifecycle actions recorded
    // beside their attributed receipts. `ack` stays receipted-only —
    // its writes are already the record.
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
        group,
    );
    if let Some(point) = shelve {
        signal(
            plant,
            point.into(),
            &format!("{prefix}-shelve"),
            "",
            "Operator shelving request for the alarm",
            group,
        );
    }
    if let Some(point) = oos {
        signal(
            plant,
            point.into(),
            &format!("{prefix}-oos"),
            "",
            "Operator out-of-service command for the alarm",
            group,
        );
    }
    for (offset, suffix, description) in [
        (
            3,
            "alarm",
            "Standing alarm state — process truth under every managed flag",
        ),
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
            group,
        );
    }
    ManagedAlarmLayout {
        component,
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
/// and the five status outputs — wires them, and binds the declared
/// `oos`/`suppress` inputs to the pump's maintenance-inhibit state:
/// `oos` reads `inhibit` — the pump's own writable out-of-service
/// point — while `suppress` reads `suppress_in`, the delivered copy
/// `wire_pump` composes, since a component binds each point once. A
/// fault alarm on a deliberately offline machine stays named and
/// countable without annunciating — decision 73's station wiring. The
/// caller wires the alarm's `in` port.
#[allow(clippy::too_many_arguments)]
fn pump_alarm(
    plant: &mut PlantBuilder,
    index: u64,
    component: ComponentId,
    ack_port: &Sink<bool>,
    managed: &ManagedAlarmHandles,
    alarm_port: &Source<bool>,
    unacknowledged_port: &Source<bool>,
    inhibit: InPoint<bool>,
    suppress_in: InPoint<bool>,
    prefix: &str,
    group: &str,
) -> ManagedAlarmLayout {
    let base = ALARM_BASE + index * 10;
    let ack = plant.internal_input::<bool>(PointId(base), false, true);
    let alarm = plant.internal_output::<bool>(PointId(base + 3), false);
    let unacknowledged = plant.internal_output::<bool>(PointId(base + 4), false);
    let shelved = plant.internal_output::<bool>(PointId(base + 5), false);
    let suppressed = plant.internal_output::<bool>(PointId(base + 6), false);
    let out_of_service = plant.internal_output::<bool>(PointId(base + 7), false);

    // The same decision-74 lifecycle audit as the station set: all five
    // status points are `journaled`.
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
        (
            3,
            "alarm",
            "Standing alarm state — process truth under every managed flag",
        ),
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
    ManagedAlarmLayout {
        component,
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
/// manual-takeover gates, the motor, and its three alarms — then
/// connects the group's indexed ports.
#[allow(clippy::too_many_arguments)]
fn wire_pump(
    plant: &mut PlantBuilder,
    config: &PumpStationConfig,
    index: usize,
    draw_ch: ChannelRef,
    run_ch: ChannelRef,
    thermal_ch: ChannelRef,
    moisture_ch: ChannelRef,
    cmd_ch: ChannelRef,
    power_fail: InPoint<bool>,
    power_ok: OutPoint<bool>,
    level_chain: InPoint<f64>,
    below_cutoff: OutPoint<bool>,
    any_manual: &BoolGateInstance,
    group: &PumpGroupInstance,
) -> PumpLayout {
    let i = index as u64;
    let tag = pump_tag(index);
    let group_name = format!("pump-{tag}");
    let base = PUMP_BASE + i * PUMP_STRIDE;

    // Field points; `draw` is produced by the dynamics document's
    // `bool_flow`, so its handle goes unused.
    plant.field_input::<f64>(points::draw(index), draw_ch, false);
    let run = plant.field_input::<bool>(points::run(index), run_ch, false);
    let thermal = plant.field_input::<bool>(points::thermal(index), thermal_ch, false);
    let moisture = plant.field_input::<bool>(points::moisture(index), moisture_ch, false);
    let cmd = plant.field_output::<bool>(points::cmd(index), cmd_ch);
    // The run, thermal, and moisture contacts are the protection
    // layer's reported states — activation/demand and fault — so the
    // durable record carries their transitions (decisions 74, 77).
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
    // mode-change and managed-state transitions (decisions 74, 75);
    // `hand` is a demand request — its writes are already the
    // attributed receipts, so it stays off the journaled set.
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
    // consumer.
    let group_cmd = plant.internal_output::<bool>(PointId(base + 3), false);
    let group_cmd_in = plant.internal_input::<bool>(PointId(base + 4), false, false);
    // The `auto`/`oos-ok`/`power-ok` carriers seed `true` — the
    // cold-start image reads as in-auto, in-service, powered until the
    // first computed values land, so `avail_i` and the guard don't
    // flicker the pump out for a scan.
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
    // The healthy-contact and aggregated-availability carriers — seeded
    // `true` like `auto`/`oos-ok` so scan 1 reads the declared cold-start
    // state, not a transient trip.
    let thermal_ok = plant.internal_output::<bool>(PointId(base + 24), true);
    let thermal_ok_in = plant.internal_input::<bool>(PointId(base + 25), false, false);
    let moisture_ok = plant.internal_output::<bool>(PointId(base + 26), true);
    let moisture_ok_in = plant.internal_input::<bool>(PointId(base + 27), false, false);
    let avail_carrier = plant.internal_output::<bool>(PointId(base + 28), true);
    let avail_in = plant.internal_input::<bool>(PointId(base + 29), false, false);
    // Decision 87's protection carriers: the dry-run flag's delivered
    // copy, the interlock's `tripped` and pass-through, and the
    // inverted `protections-ok` pair the command guard and the hand
    // holdout read. `protections-ok` seeds `true` like the other
    // healthy-state carriers — cold start reads protected until the
    // first computed values land.
    let below_cutoff_in = plant.internal_input::<bool>(PointId(base + 15), false, false);
    let protect_tripped = plant.internal_output::<bool>(PointId(base + 16), false);
    let protect_tripped_in = plant.internal_input::<bool>(PointId(base + 17), false, false);
    let protections_ok = plant.internal_output::<bool>(PointId(base + 18), true);
    let protections_ok_in = plant.internal_input::<bool>(PointId(base + 19), false, false);
    let protect_out = plant.internal_output::<f64>(PointId(base + 20), 0.0);
    // The proven fault, the aggregated availability, and the
    // protection state are protection-relevant status — `journaled`
    // like the contacts feeding them; the inverted and delivered
    // copies stay off the record, their sources already carry it.
    plant.journaled(fault);
    plant.journaled(avail_carrier);
    plant.journaled(protect_tripped);
    plant.journaled(protections_ok);
    for (point, name, description) in [
        (
            base + 3,
            "group-cmd",
            "The pump group's automatic run request",
        ),
        (
            base + 4,
            "group-cmd-in",
            "Group request delivered to the auto leg",
        ),
        (base + 5, "auto", "In auto — the inverted manual-mode point"),
        (base + 6, "auto-leg-in", "In-auto delivered to the auto leg"),
        (
            base + 7,
            "auto-avail-in",
            "In-auto delivered to availability",
        ),
        (
            base + 8,
            "oos-ok",
            "In service — the inverted out-of-service point",
        ),
        (
            base + 9,
            "oos-ok-avail-in",
            "In-service delivered to availability",
        ),
        (
            base + 10,
            "oos-ok-guard-in",
            "In-service delivered to the protection permissive",
        ),
        (
            base + 11,
            "power-ok-in",
            "Station power-ok delivered to availability",
        ),
        (
            base + 12,
            "fault",
            "The motor's proven command/feedback fault",
        ),
        (
            base + 13,
            "fault-group-in",
            "Motor fault delivered to the pump group",
        ),
        (
            base + 14,
            "fault-alarm-in",
            "Motor fault delivered to the alarm",
        ),
        (
            base + 15,
            "below-cutoff-in",
            "Dry-run cutoff delivered to the protection interlock",
        ),
        (
            base + 16,
            "protect-tripped",
            "Protection interlock tripped — a condition asserted or untrusted",
        ),
        (
            base + 17,
            "protect-tripped-in",
            "Protection trip delivered to its inversion",
        ),
        (
            base + 18,
            "protections-ok",
            "Protections clear and trusted — the inverted interlock trip",
        ),
        (
            base + 19,
            "protections-ok-in",
            "Protections-clear delivered to the guard and holdout",
        ),
        (
            base + 20,
            "protect-out",
            "The protection interlock's analog pass-through — unused",
        ),
        (
            base + 24,
            "thermal-ok",
            "Thermal contact healthy — the inverted contact",
        ),
        (
            base + 25,
            "thermal-ok-in",
            "Thermal-healthy delivered to availability",
        ),
        (
            base + 26,
            "moisture-ok",
            "Moisture contact healthy — the inverted contact",
        ),
        (
            base + 27,
            "moisture-ok-in",
            "Moisture-healthy delivered to availability",
        ),
        (
            base + 28,
            "avail",
            "Aggregated availability for the pump group",
        ),
        (
            base + 29,
            "avail-in",
            "Availability delivered to the pump group",
        ),
    ] {
        signal(
            plant,
            PointId(point),
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
    // Decision 87's protection aggregation: the `interlock` trips on
    // an asserted *or* untrusted condition alike — the thermal,
    // moisture, and power-fail contacts and the chain's `below_cutoff`
    // as its trips, in-service as its permissive, and the delivered
    // selected level on its `in` so an untrusted measurement cannot
    // prove the dry-run trip clear. The inverted `tripped` guards the
    // command in both modes, and the `timer` at `min_off_ticks` holds
    // the hand leg out until the protections have stood — the group's
    // `min_off`/`restage` discipline reaches only the auto leg.
    let protect = plant.add(InterlockSpec::new(
        parameters([("safe_value", Value::Float(0.0))]),
        4,
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
    // The pump's three alarms, all managed. The fault alarm declares
    // `oos`/`suppress` — both bound below to the pump's own
    // out-of-service point, so the maintenance-inhibit state doubles
    // as the designed-suppression condition: a deliberately offline
    // pump's fault stays named and countable without annunciating
    // (decision 73). The contact alarms declare no lifecycle inputs —
    // never-shelvable with no shelving surface, never suppressed,
    // never out of service; their managed status outputs still
    // report.
    let fault_alarm = plant.add(ManagedBoolLatchingAlarmSpec::new(
        parameters([
            ("max_shelve_ticks", Value::Int(0)),
            ("priority", Value::Int(2)),
            ("class", Value::Int(2)),
            ("response_ticks", Value::Int(60)),
        ]),
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
        parameters([
            ("max_shelve_ticks", Value::Int(0)),
            ("priority", Value::Int(2)),
            ("class", Value::Int(2)),
            ("response_ticks", Value::Int(60)),
        ]),
        ManagedInputs::default(),
        rationalization(
            "The motor overheats and the pump trips out",
            "Investigate the thermal overload and reset the contact",
            &format!("{tag}-thermal-alarm"),
        ),
    ));
    let moisture_alarm = plant.add(ManagedBoolLatchingAlarmSpec::new(
        parameters([
            ("max_shelve_ticks", Value::Int(0)),
            ("priority", Value::Int(3)),
            ("class", Value::Int(2)),
            ("response_ticks", Value::Int(60)),
        ]),
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
    // moisture-ok — decision 41's aggregated availability, delivered to
    // the group through its own seeded carrier pair.
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

    // Decision 87's protection aggregation: the raw contacts bind the
    // interlock's trips directly — a point may feed many port inputs —
    // so an asserted *or* untrusted thermal, moisture, power-fail, or
    // dry-run condition trips the pump; `oos` stands as the permissive
    // and the delivered selected level on `in` fails the same way. The
    // inverted `tripped` is `protections-ok`; the `timer` holds the
    // hand leg out for `min_off_ticks` after the protections clear.
    plant.connect(below_cutoff_in, below_cutoff);
    plant.connect(level_chain, &protect.input);
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

    // The manual-takeover shape under decision 87: `motor.cmd =
    // ((group cmd and not mode) or (hand and mode and the held
    // protection set)) and protections-ok` — the operator's `hand`
    // request stays a demand the declared protections bound, not a
    // bypass. A pump held in manual feeds the `any-manual`
    // aggregation the `none-available` alarm suppresses on.
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

    // The pump's three managed alarms — motor fault, thermal contact,
    // moisture contact — laid out in the alarm region at
    // `PUMP_ALARM_BASE + 3 * index + offset`, each on its own writable
    // ack. The fault alarm's `oos` binds the pump's `oos` point — the
    // same writable maintenance-inhibit point the operator commands —
    // directly, while its `suppress` reads the pass-through copy a
    // `digital-input` composes: a component binds each point once, so
    // the same declared state reaches the second input through the
    // carrier pair one scan later.
    let oos_copy = plant.add(DigitalInputSpec::new(parameters([(
        "invert",
        Value::Bool(false),
    )])));
    let fault_sup = plant.internal_output::<bool>(PointId(base + 30), false);
    let fault_sup_in = plant.internal_input::<bool>(PointId(base + 31), false, false);
    plant.connect(oos, &oos_copy.input);
    plant.connect(&oos_copy.out, fault_sup);
    plant.connect(fault_sup_in, fault_sup);
    signal(
        plant,
        PointId(base + 30),
        &format!("{tag}-fault-sup"),
        "",
        "Out-of-service state delivered to the fault alarm's suppression",
        &group_name,
    );
    signal(
        plant,
        PointId(base + 31),
        &format!("{tag}-fault-sup-in"),
        "",
        "The fault alarm's suppression condition",
        &group_name,
    );
    let fault_alarm_layout = pump_alarm(
        plant,
        PUMP_ALARM_BASE + 3 * i,
        fault_alarm.id,
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
    let thermal_alarm_layout = pump_alarm(
        plant,
        PUMP_ALARM_BASE + 3 * i + 1,
        thermal_alarm.id,
        &thermal_alarm.ack,
        &thermal_alarm.managed,
        &thermal_alarm.alarm,
        &thermal_alarm.unacknowledged,
        oos,
        fault_sup_in,
        &format!("{tag}-thermal"),
        &group_name,
    );
    plant.connect(thermal, &thermal_alarm.input);
    let moisture_alarm_layout = pump_alarm(
        plant,
        PUMP_ALARM_BASE + 3 * i + 2,
        moisture_alarm.id,
        &moisture_alarm.ack,
        &moisture_alarm.managed,
        &moisture_alarm.alarm,
        &moisture_alarm.unacknowledged,
        oos,
        fault_sup_in,
        &format!("{tag}-moisture"),
        &group_name,
    );
    plant.connect(moisture, &moisture_alarm.input);

    PumpLayout {
        index: index + 1,
        cmd: points::cmd(index),
        run: points::run(index),
        draw: points::draw(index),
        thermal: points::thermal(index),
        moisture: points::moisture(index),
        mode: PointId(base),
        hand: PointId(base + 1),
        out_of_service: PointId(base + 2),
        group_cmd: PointId(base + 3),
        fault: PointId(base + 12),
        avail: PointId(base + 28),
        protect_tripped: PointId(base + 16),
        protections_ok: PointId(base + 18),
        motor: motor.id,
        fault_alarm: fault_alarm_layout,
        thermal_alarm: thermal_alarm_layout,
        moisture_alarm: moisture_alarm_layout,
    }
}
