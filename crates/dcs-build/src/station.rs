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
//! - **Manual takeover (decision 42's recorded shape):** per pump, a
//!   writable `mode_i` point selects between the group's `cmd_i` and
//!   the operator's writable `hand_i` request — `motor.cmd_i = (cmd_i
//!   and not mode_i) or (hand_i and mode_i)`, guarded by in-service,
//!   through `bool-gate` instances and `digital-input` inversions.
//! - **Alarms (decision 43):** every alarm is a wired latching instance
//!   with a writable internal `ack` point — analog `latching-alarm`s on
//!   the selected level at the declared `high` and `cutoff` thresholds,
//!   `bool-latching-alarm`s on each motor's fault flag, the failover's
//!   `backup_active`, the group's `none_available`/`all_faulted`, the
//!   per-pump thermal and moisture contacts, and the station power-fail
//!   field point.
//!
//! ## The declared point-id scheme
//!
//! Field points occupy fixed blocks the checked-in dynamics document is
//! written against — `10` level-primary, `11` level-backup, `12`
//! inflow, `13` net-flow, `20+i` per-pump draw, `40+i` run, `60+i`
//! thermal, `80+i` moisture, `100+i` command, `120` power-fail (`i` the
//! 0-based pump index, `pumps <= 20`). Internal carriers start at `200`,
//! per-pump internal blocks at `300 + 32·i`, station-alarm points at
//! `1000 + 10·a`, and every point's signal sits at `10000 + point`. The
//! scheme is deterministic in declaration order, so identical builder
//! invocations emit identical documents.
//!
//! Bool state signals and the index-valued `duty` declare an empty
//! unit — a deliberate "unitless" marker rather than an omitted one, so
//! the document lints clean.

use crate::specs::{
    BoolGateSpec, BoolLatchingAlarmInstance, BoolLatchingAlarmSpec, DigitalInputSpec,
    FailoverSelectSpec, LatchingAlarmInstance, LatchingAlarmSpec, MotorSpec, PumpGroupInstance,
    PumpGroupSpec, ThresholdChainSpec,
};
use crate::{
    BuildError, ChannelRef, Direction, OutPoint, PlantBuilder, PointId, SignalId, Sink, Source,
    Value, parameters,
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
}

/// The first per-pump internal block: pump `i` owns
/// `PUMP_BASE + i * PUMP_STRIDE .. +PUMP_STRIDE`.
const PUMP_BASE: u64 = 300;
const PUMP_STRIDE: u64 = 32;
/// Station-alarm points: alarm `a` owns `ALARM_BASE + a * 10 .. +10` —
/// `ack`, `alarm`, `unacknowledged` at offsets 0, 1, 2.
const ALARM_BASE: u64 = 1000;
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
    /// The `motor` instance's id.
    pub motor: ComponentId,
    /// The motor-fault alarm.
    pub fault_alarm: AlarmLayout,
    /// The thermal-overload alarm.
    pub thermal_alarm: AlarmLayout,
    /// The moisture alarm.
    pub moisture_alarm: AlarmLayout,
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
    /// The group's `none_available` carrier.
    pub none_available: PointId,
    /// The group's `all_faulted` carrier.
    pub all_faulted: PointId,
    /// The `failover-select` instance's id.
    pub failover: ComponentId,
    /// The `threshold-chain` instance's id.
    pub threshold_chain: ComponentId,
    /// The `pump-group` instance's id.
    pub pump_group: ComponentId,
    /// Per-pump layouts, in `index` order.
    pub pumps: Vec<PumpLayout>,
    /// The high-level latching alarm (trips at `config.high`).
    pub high_level_alarm: AlarmLayout,
    /// The low-level latching alarm (trips at `config.cutoff`).
    pub low_level_alarm: AlarmLayout,
    /// The backup-measurement-serving alarm.
    pub backup_active_alarm: AlarmLayout,
    /// The no-pump-available alarm.
    pub none_available_alarm: AlarmLayout,
    /// The every-pump-faulted alarm.
    pub all_faulted_alarm: AlarmLayout,
    /// The station power-fail alarm.
    pub power_fail_alarm: AlarmLayout,
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
    plant.field_input::<f64>(points::NET_FLOW, net_flow_ch, false);
    let power_fail = plant.field_input::<bool>(points::POWER_FAIL, power_fail_ch, false);

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
    // The decision-70 codes are declared data — the site priority/class
    // vocabulary and response budgets stay an open customer assumption.
    let lah = plant.add(LatchingAlarmSpec::new(
        parameters([
            ("low_limit", Value::Float(-PARKED_LIMIT)),
            ("high_limit", Value::Float(config.high)),
            ("hysteresis", Value::Float(config.level_alarm_hysteresis)),
            ("priority", Value::Int(1)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(30)),
        ]),
        rationalization(
            "The wet well overflows the bench",
            "Start a pump and investigate why the demand did not call one",
            "lah-alarm",
        ),
    ));
    let lal = plant.add(LatchingAlarmSpec::new(
        parameters([
            ("low_limit", Value::Float(config.cutoff)),
            ("high_limit", Value::Float(PARKED_LIMIT)),
            ("hysteresis", Value::Float(config.level_alarm_hysteresis)),
            ("priority", Value::Int(1)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(30)),
        ]),
        rationalization(
            "The wet well pumps dry and the running pumps cavitate",
            "Stop the running pumps and investigate the low level",
            "lal-alarm",
        ),
    ));
    let backup_alarm = plant.add(BoolLatchingAlarmSpec::new(
        parameters([
            ("priority", Value::Int(2)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(30)),
        ]),
        rationalization(
            "The backup level instrument carries the station unnoticed",
            "Check the primary level instrument",
            "backup-active-alarm",
        ),
    ));
    let none_available_alarm = plant.add(BoolLatchingAlarmSpec::new(
        parameters([
            ("priority", Value::Int(1)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(30)),
        ]),
        rationalization(
            "Demand stands with no pump available to meet it",
            "Restore a pump to service or clear its faults",
            "none-available-alarm",
        ),
    ));
    let all_faulted_alarm = plant.add(BoolLatchingAlarmSpec::new(
        parameters([
            ("priority", Value::Int(1)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(30)),
        ]),
        rationalization(
            "Every pump is faulted; the station cannot pump",
            "Dispatch maintenance to clear the pump faults",
            "all-faulted-alarm",
        ),
    ));
    let power_fail_alarm = plant.add(BoolLatchingAlarmSpec::new(
        parameters([
            ("priority", Value::Int(1)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(30)),
        ]),
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
    plant.connect(backup_active_in, backup_active);
    plant.connect(none_available_in, none_available);
    plant.connect(all_faulted_in, all_faulted);

    // The station-level alarms — each latching on its condition and its
    // own writable ack point.
    let high_level_alarm = station_alarm(&mut plant, 0, &lah, "lah", "station");
    plant.connect(level_lah, &lah.input);
    let low_level_alarm = station_alarm(&mut plant, 1, &lal, "lal", "station");
    plant.connect(level_lal, &lal.input);
    let backup_active_alarm =
        station_alarm(&mut plant, 2, &backup_alarm, "backup-active", "station");
    plant.connect(backup_active_in, &backup_alarm.input);
    let none_available_alarm_layout = station_alarm(
        &mut plant,
        3,
        &none_available_alarm,
        "none-available",
        "station",
    );
    plant.connect(none_available_in, &none_available_alarm.input);
    let all_faulted_alarm_layout =
        station_alarm(&mut plant, 4, &all_faulted_alarm, "all-faulted", "station");
    plant.connect(all_faulted_in, &all_faulted_alarm.input);
    let power_fail_alarm_layout =
        station_alarm(&mut plant, 5, &power_fail_alarm, "power-fail", "station");
    plant.connect(power_fail, &power_fail_alarm.input);

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
            power_ok,
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
            none_available: PointId(carriers::NONE_AVAILABLE),
            all_faulted: PointId(carriers::ALL_FAULTED),
            failover: failover.id,
            threshold_chain: chain.id,
            pump_group: group.id,
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

/// What the shared alarm wiring needs of either latching kind: the
/// instance id, the ack sink, and the two output handles.
trait AlarmHandles {
    /// The instance's component id.
    fn id(&self) -> ComponentId;
    /// The `ack` port.
    fn ack(&self) -> Sink<bool>;
    /// The `alarm` port.
    fn alarm(&self) -> Source<bool>;
    /// The `unacknowledged` port.
    fn unacknowledged(&self) -> Source<bool>;
}

impl AlarmHandles for LatchingAlarmInstance {
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

impl AlarmHandles for BoolLatchingAlarmInstance {
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
/// signals under `{prefix}-ack`/`-alarm`/`-unacknowledged`. The
/// caller wires the alarm's `in` port — its value kind differs between
/// the analog and Bool kinds.
fn station_alarm<A: AlarmHandles>(
    plant: &mut PlantBuilder,
    index: u64,
    instance: &A,
    prefix: &str,
    group: &str,
) -> AlarmLayout {
    let base = ALARM_BASE + index * 10;
    let ack = plant.internal_input::<bool>(PointId(base), false, true);
    let alarm = plant.internal_output::<bool>(PointId(base + 1), false);
    let unacknowledged = plant.internal_output::<bool>(PointId(base + 2), false);
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
    AlarmLayout {
        component: instance.id(),
        ack: PointId(base),
        alarm: PointId(base + 1),
        unacknowledged: PointId(base + 2),
    }
}

/// Wires pump `index` (`0`-based): field points, the availability
/// aggregation, the manual-takeover gates, the motor, and its three
/// alarms — then connects the group's indexed ports.
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
    power_ok: OutPoint<bool>,
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
    // service.
    let mode = plant.internal_input::<bool>(PointId(base), false, true);
    let hand = plant.internal_input::<bool>(PointId(base + 1), false, true);
    let oos = plant.internal_input::<bool>(PointId(base + 2), false, true);
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
            "In-service delivered to the command guard",
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
        2,
    ));
    let select = plant.add(BoolGateSpec::new(
        parameters([("operation", Value::Int(GATE_OR))]),
        2,
    ));
    let guard = plant.add(BoolGateSpec::new(
        parameters([("operation", Value::Int(GATE_AND))]),
        2,
    ));
    let motor = plant.add(MotorSpec::new(parameters([(
        "fault_ticks",
        Value::Int(config.motor_fault_ticks),
    )])));
    let fault_alarm = plant.add(BoolLatchingAlarmSpec::new(
        parameters([
            ("priority", Value::Int(2)),
            ("class", Value::Int(2)),
            ("response_ticks", Value::Int(60)),
        ]),
        rationalization(
            "The pump cannot run while its fault stands",
            "Clear the motor fault and reset the pump",
            &format!("{tag}-fault-alarm"),
        ),
    ));
    let thermal_alarm = plant.add(BoolLatchingAlarmSpec::new(
        parameters([
            ("priority", Value::Int(2)),
            ("class", Value::Int(2)),
            ("response_ticks", Value::Int(60)),
        ]),
        rationalization(
            "The motor overheats and the pump trips out",
            "Investigate the thermal overload and reset the contact",
            &format!("{tag}-thermal-alarm"),
        ),
    ));
    let moisture_alarm = plant.add(BoolLatchingAlarmSpec::new(
        parameters([
            ("priority", Value::Int(3)),
            ("class", Value::Int(2)),
            ("response_ticks", Value::Int(60)),
        ]),
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

    // The recorded manual-takeover shape: &motor.cmd = (group cmd and
    // not mode) or (hand and mode), guarded by in-service.
    plant.connect(group_cmd_in, auto_leg.input(1));
    plant.connect(auto_leg_in, auto_leg.input(2));
    plant.connect(hand, hand_leg.input(1));
    plant.connect(mode, hand_leg.input(2));
    plant.connect(&auto_leg.out, select.input(1));
    plant.connect(&hand_leg.out, select.input(2));
    plant.connect(&select.out, guard.input(1));
    plant.connect(oos_ok_guard_in, guard.input(2));
    plant.connect(&guard.out, &motor.cmd);
    plant.connect(run, &motor.run);
    plant.connect(&motor.out, cmd);

    // The group's run/fault feedback.
    plant.connect(run, group.run(index + 1));
    plant.connect(&motor.fault, fault);
    plant.connect(fault_group_in, fault);
    plant.connect(fault_group_in, group.fault(index + 1));
    plant.connect(fault_alarm_in, fault);
    plant.connect(fault_alarm_in, fault_alarm.input);

    // The pump's three alarms — motor fault, thermal contact, moisture
    // contact — each on its own writable ack.
    let fault_ack = plant.internal_input::<bool>(PointId(base + 15), false, true);
    let fault_alarm_out = plant.internal_output::<bool>(PointId(base + 16), false);
    let fault_unack_out = plant.internal_output::<bool>(PointId(base + 17), false);
    let thermal_ack = plant.internal_input::<bool>(PointId(base + 18), false, true);
    let thermal_alarm_out = plant.internal_output::<bool>(PointId(base + 19), false);
    let thermal_unack_out = plant.internal_output::<bool>(PointId(base + 20), false);
    let moisture_ack = plant.internal_input::<bool>(PointId(base + 21), false, true);
    let moisture_alarm_out = plant.internal_output::<bool>(PointId(base + 22), false);
    let moisture_unack_out = plant.internal_output::<bool>(PointId(base + 23), false);
    plant.connect(fault_ack, &fault_alarm.ack);
    plant.connect(&fault_alarm.alarm, fault_alarm_out);
    plant.connect(&fault_alarm.unacknowledged, fault_unack_out);
    plant.connect(thermal, &thermal_alarm.input);
    plant.connect(thermal_ack, &thermal_alarm.ack);
    plant.connect(&thermal_alarm.alarm, thermal_alarm_out);
    plant.connect(&thermal_alarm.unacknowledged, thermal_unack_out);
    plant.connect(moisture, &moisture_alarm.input);
    plant.connect(moisture_ack, &moisture_alarm.ack);
    plant.connect(&moisture_alarm.alarm, moisture_alarm_out);
    plant.connect(&moisture_alarm.unacknowledged, moisture_unack_out);
    for (point, name, description) in [
        (
            base + 15,
            "fault-ack",
            "Operator acknowledgment for the motor-fault alarm",
        ),
        (base + 16, "fault-alarm", "Standing motor-fault alarm"),
        (
            base + 17,
            "fault-unack",
            "Motor fault latched until acknowledged",
        ),
        (
            base + 18,
            "thermal-ack",
            "Operator acknowledgment for the thermal alarm",
        ),
        (
            base + 19,
            "thermal-alarm",
            "Standing thermal-overload alarm",
        ),
        (
            base + 20,
            "thermal-unack",
            "Thermal overload latched until acknowledged",
        ),
        (
            base + 21,
            "moisture-ack",
            "Operator acknowledgment for the moisture alarm",
        ),
        (
            base + 22,
            "moisture-alarm",
            "Standing moisture-ingress alarm",
        ),
        (
            base + 23,
            "moisture-unack",
            "Moisture ingress latched until acknowledged",
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
        motor: motor.id,
        fault_alarm: AlarmLayout {
            component: fault_alarm.id,
            ack: PointId(base + 15),
            alarm: PointId(base + 16),
            unacknowledged: PointId(base + 17),
        },
        thermal_alarm: AlarmLayout {
            component: thermal_alarm.id,
            ack: PointId(base + 18),
            alarm: PointId(base + 19),
            unacknowledged: PointId(base + 20),
        },
        moisture_alarm: AlarmLayout {
            component: moisture_alarm.id,
            ack: PointId(base + 21),
            alarm: PointId(base + 22),
            unacknowledged: PointId(base + 23),
        },
    }
}
