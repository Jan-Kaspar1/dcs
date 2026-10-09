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
//! - **Manual takeover (decision 88's recorded shape):** per pump, a
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
//!   contacts, and the station power-fail field point. The cause
//!   alarms read quality-aware conditions — the station power guard's
//!   `tripped` and one per-pump cause guard per contact — so an
//!   asserted *or* untrusted contact annunciates on the same reading
//!   that trips the pump (decision 88, issue #809). Every managed
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
//!
//! ## Declared units (the dimensional-discipline decision)
//!
//! The station is the pump-side adoption of architecture decision 106's
//! declared-unit metadata: every quantity-bearing point declares its
//! engineering unit through [`PlantBuilder::unit`](crate::PlantBuilder::unit)
//! — the wet-well levels in `m`, the station flow in `m3/h`, the
//! chain's and group's stage counts in `pumps` — and each signal
//! inherits its point's declaration at `build` rather than carrying a
//! display string that could drift beside it. The unit-transparent
//! kinds the station composes declare theirs through
//! [`PlantBuilder::port_unit`](crate::PlantBuilder::port_unit) and
//! [`PlantBuilder::param_unit`](crate::PlantBuilder::param_unit): the
//! `failover-select`'s three level ports and the `threshold-chain`'s
//! `level` are `m`, the chain's rungs and the level alarms' limits and
//! hysteresis are `m`, the `pump-group`'s `demand`/`duty`/`staged` and
//! the chain's `demand` are `pumps`, and the `*_ticks` intervals each
//! spec fixes as `ticks` — the staging bounds, the hand-leg holdout,
//! the motor's fault budget, and every managed alarm's shelving bound
//! and decision-70 response budget.
//!
//! Two families stay deliberately undeclared. The station power guard
//! and the per-pump cause guards bind a held analog anchor on `in`
//! rather than a process quantity, so a dimension there would claim
//! engineering the wiring does not carry; and the Bool permissive,
//! alarm, status, and index-valued carriers stay uncheckable rather
//! than dimensioned, their signals keeping the explicit `""` marker the
//! lint asks for.

use crate::specs::{
    BoolGateInstance, BoolGateSpec, DigitalInputSpec, FailoverSelectSpec, InterlockSpec,
    ManagedAlarmHandles, ManagedBoolLatchingAlarmSpec, ManagedInputs, ManagedLatchingAlarmSpec,
    PumpGroupInstance, PumpGroupSpec, ThresholdChainSpec,
};
use crate::{
    BuildError, ChannelRef, Direction, InPoint, OutPoint, PlantBuilder, PointId, SignalId, Sink,
    Source, Value, parameters, unit,
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
    /// The held analog feed the per-pump cause guards' `in` binds —
    /// the declared conditions are discrete, so a constant `Good`
    /// input leaves each `tripped` reporting the contact alone.
    pub const GUARD_ANCHOR: u64 = 231;
    /// The held permissive the per-pump cause guards bind — a cause
    /// alarm stands in every service state, so `oos-ok` is not its
    /// permissive.
    pub const GUARD_TRUE: u64 = 232;
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
pub use crate::pump::ManagedAlarmLayout;

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
    // the dynamics document's elements, so their handles serve only the
    // durable-duty marks below.
    let level_primary = plant.field_input::<f64>(points::LEVEL_PRIMARY, level_primary_ch, false);
    let level_backup = plant.field_input::<f64>(points::LEVEL_BACKUP, level_backup_ch, false);
    let inflow = plant.field_input::<f64>(points::INFLOW, inflow_ch, false);
    let net_flow = plant.field_input_stale_after::<f64>(
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
    // Decision 102's declared recording duty: the wet-well compliance
    // series — the level measurements and the flow channels — record
    // into the durable history file every scan. Cadence 1 keeps the
    // QA lane's retention leg inside its bounded wait: the served
    // window's declared bound fills in under a minute of scans.
    plant.record(level_primary, 1);
    plant.record(level_backup, 1);
    plant.record(inflow, 1);
    plant.record(net_flow, 1);

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
    // Decision 88's cause-alarm wiring: `power_ok`'s delivered copy is
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
    // — no pump available to the group is then designed state, not a
    // fault (decision 73's pattern). The carrier keeps reporting truth;
    // the `suppressed` flag names the withholding.
    let any_manual = plant.internal_output::<bool>(PointId(carriers::ANY_MANUAL), false);
    let any_manual_in =
        plant.internal_input::<bool>(PointId(carriers::ANY_MANUAL_IN), false, false);
    // The per-pump cause guards' held feeds — the power guard's
    // held-anchor trick again: a constant `Good` `in` and `permissive`
    // leave each guard's `tripped` reporting the contact alone —
    // asserted or untrusted alike — so a degraded thermal or moisture
    // contact annunciates its own cause alarm on the same reading
    // that trips the pump (issue #809).
    let guard_anchor = plant.internal_input::<f64>(PointId(carriers::GUARD_ANCHOR), 0.0, false);
    let guard_true = plant.internal_input::<bool>(PointId(carriers::GUARD_TRUE), true, false);
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
    // Decision 88: the station power interlock evaluates the
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
        // The carrier asserts on availability alone — the consequence
        // names the empty roster, never a standing demand (#825).
        rationalization(
            "No pump is available; the station cannot pump",
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

    // The dimensional contract (architecture decision 106): the
    // quantity-bearing kinds are unit-transparent — a `failover-select`
    // carries whatever level it is fed and a `threshold-chain`'s rungs
    // take its `level` port's unit — so the composition declares each
    // quantity port's and bound's unit on the instance, beside the
    // point declarations the same connection and validation checks
    // read. The `*_ticks` intervals are the kinds' own dimension: each
    // spec's `ParamDecl::with_unit(unit::TICKS)` makes `ticks` the only
    // declaration `param_unit` accepts, and the instance declares it so
    // the document records what each interval counts. The levels are
    // `m`, the station flow is `m3/h`, and the chain's and group's
    // stage counts are `pumps` — the count the group's `demand` port
    // takes and its `duty`/`staged` outputs report.
    plant.port_unit(failover.id, "primary", unit::M);
    plant.port_unit(failover.id, "backup", unit::M);
    plant.port_unit(failover.id, "out", unit::M);
    plant.port_unit(chain.id, "level", unit::M);
    plant.port_unit(chain.id, "demand", unit::PUMPS);
    for parameter in ["cutoff", "stop", "start", "lag_start", "high"] {
        plant.param_unit(chain.id, parameter, unit::M);
    }
    plant.port_unit(group.id, "demand", unit::PUMPS);
    plant.port_unit(group.id, "duty", unit::PUMPS);
    plant.port_unit(group.id, "staged", unit::PUMPS);
    for parameter in ["start_delay_ticks", "restage_delay_ticks", "min_off_ticks"] {
        plant.param_unit(group.id, parameter, unit::TICKS);
    }
    if config.rotation_ticks.is_some() {
        plant.param_unit(group.id, "rotation_ticks", unit::TICKS);
    }
    // The two level alarms observe the selected level in `m` and bound
    // it in `m`; their shelving bound and decision-70 response budget
    // are the managed kind's declared `ticks`. The station power guard
    // is deliberately left undeclared: its `in` binds a held analog
    // anchor rather than a process quantity, so a dimension there would
    // claim engineering the wiring does not carry.
    for alarm in [lah.id, lal.id] {
        plant.port_unit(alarm, "in", unit::M);
        for parameter in ["low_limit", "high_limit", "hysteresis"] {
            plant.param_unit(alarm, parameter, unit::M);
        }
        for parameter in ["max_shelve_ticks", "response_ticks"] {
            plant.param_unit(alarm, parameter, unit::TICKS);
        }
    }
    for alarm in [
        backup_alarm.id,
        backup_unhealthy_alarm.id,
        none_available_alarm.id,
        all_faulted_alarm.id,
        power_fail_alarm.id,
    ] {
        for parameter in ["max_shelve_ticks", "response_ticks"] {
            plant.param_unit(alarm, parameter, unit::TICKS);
        }
    }

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
            guard_anchor,
            guard_true,
            &any_manual_gate,
            &group,
        )?);
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
///
/// A non-empty `unit` is declared once, on the point — the wiring
/// contract the connection and validation checks read — and the
/// signal inherits it at `build`, so the string the operator surface
/// renders cannot drift from the unit the value is carried in. The
/// empty `""` stays signal-side: the deliberate "dimensionless" marker
/// for the status, flag, and code points the wiring layer leaves
/// uncheckable.
fn signal(
    plant: &mut PlantBuilder,
    point: PointId,
    name: &str,
    unit: &str,
    description: &str,
    group: &str,
) {
    if unit.is_empty() {
        plant
            .signal(SignalId(SIGNAL_BASE + point.0), name, point)
            .unit(unit)
            .description(description)
            .group(group);
    } else {
        plant.unit(point, unit);
        plant
            .signal(SignalId(SIGNAL_BASE + point.0), name, point)
            .description(description)
            .group(group);
    }
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

/// Binds one station pump's field I/O and surrounding duty/standby group.
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
    guard_anchor: InPoint<f64>,
    guard_true: InPoint<bool>,
    any_manual: &BoolGateInstance,
    group: &PumpGroupInstance,
) -> Result<PumpLayout, BuildError> {
    let i = index as u64;
    let tag = pump_tag(index);
    let group_name = format!("pump-{tag}");
    plant.field_input::<f64>(points::draw(index), draw_ch, false);
    let run = plant.field_input::<bool>(points::run(index), run_ch, false);
    let thermal = plant.field_input::<bool>(points::thermal(index), thermal_ch, false);
    let moisture = plant.field_input::<bool>(points::moisture(index), moisture_ch, false);
    let cmd = plant.field_output::<bool>(points::cmd(index), cmd_ch);
    signal(
        plant,
        points::draw(index),
        &format!("{tag}-draw"),
        "m3/h",
        "Simulated discharge flow the bool_flow element drives",
        &group_name,
    );
    // A station simulation binding, independent of the pump equipment library.
    plant.connect(run, cmd);
    let mut pump_config = crate::pump::PumpConfig::new(
        tag,
        PUMP_BASE + i * PUMP_STRIDE,
        ALARM_BASE + (PUMP_ALARM_BASE + 3 * i) * 10,
    );
    pump_config.min_off_ticks = config.min_off_ticks;
    pump_config.motor_fault_ticks = config.motor_fault_ticks;
    let instance = crate::pump::pump(
        plant,
        &pump_config,
        crate::pump::PumpInputs {
            run,
            thermal,
            moisture,
            command: cmd,
            automatic_request: group.cmd(index + 1),
            power_fail,
            power_ok,
            dry_run: below_cutoff,
            protective_measurement: level_chain,
            guard_anchor,
            guard_true,
            links: crate::pump::PumpLinks {
                run: Some(group.run(index + 1)),
                fault: Some(group.fault(index + 1)),
                available: Some(group.avail(index + 1)),
                manual: Some(any_manual.input(index + 1)),
            },
        },
    )?;
    let p = instance.layout;
    Ok(PumpLayout {
        index: index + 1,
        cmd: p.cmd,
        run: p.run,
        draw: points::draw(index),
        thermal: p.thermal,
        moisture: p.moisture,
        mode: p.mode,
        hand: p.hand,
        out_of_service: p.out_of_service,
        group_cmd: p.automatic_request,
        fault: p.fault,
        avail: p.avail,
        protect_tripped: p.protect_tripped,
        protections_ok: p.protections_ok,
        motor: p.motor,
        fault_alarm: p.fault_alarm,
        thermal_alarm: p.thermal_alarm,
        moisture_alarm: p.moisture_alarm,
    })
}
