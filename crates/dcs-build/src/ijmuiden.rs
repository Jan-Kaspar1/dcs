//! The IJmuiden-pattern consequential-annunciation plant — the
//! issue-#297 reference scenario decisions 75 and 77 record.
//!
//! [`ijmuiden`] composes the scenario entirely through typed spec
//! handles and emits the versioned [`PlantModel`] document; the
//! checked-in copy lives at `crates/dcs-demo/fixtures/ijmuiden.json`
//! beside its dynamics document `ijmuiden_dynamics.json` — the recorded
//! fixture convention the station uses — and
//! `dcs-build/tests/ijmuiden.rs` asserts the emitted document equals
//! that artifact.
//!
//! The scenario is the IJmuiden near miss [docs/research/alarm-
//! management.md sources 18–19] mapped onto the recorded seams: a
//! discharge gate whose field fault switches it to local manual and
//! holds it open while the tide pushes the canal level up toward the
//! independent high-high layer's action.
//!
//! ## The composition
//!
//! - **Measurement:** the canal `level` (the dynamics document's
//!   integrator output) feeds `failover-select` as primary with the
//!   scripted `level-remote` repeater as backup; the selected level
//!   passes `signal-filter` into the `threshold-chain`, the managed
//!   high-level alarm, and the divergence detector.
//! - **Mode/authority (decision 75):** `manual-station` sits on the
//!   gate-demand path; its `mode` input is the scripted `gate-mode`
//!   field point — the field-reported local/auto switch the incident's
//!   fault drove — and `manual_active` lands on a managed
//!   `bool-latching-alarm`, the status point `journaled` so the
//!   automatic-to-manual transition is durable (decision 74).
//! - **Confirmed-state/demand discrepancy:** `valve` carries the gate
//!   demand to the `gate-cmd` field output and proves `gate-fb` against
//!   it; `discrepancy` — the demanded-safe-but-confirmed-open mismatch —
//!   feeds a managed `bool-latching-alarm` whose `suppress` input binds
//!   the SIS actuation contact, the decision-73/76 engineered
//!   consequential suppression: while the protection layer owns the
//!   hazard the standing mismatch stays in the record without
//!   re-annunciating.
//! - **Worsening process condition:** the rising level rides the
//!   `threshold-chain`'s `duty_call`/`lag_call`/`high_level` ladder and
//!   the managed `latching-alarm` at the declared `high` limit. The
//!   rate-of-rise annunciation is the implementing ticket's documented
//!   choice (decision 75): a `deviation-monitor` compares the filtered
//!   level against a slower `signal-filter` trend — a sustained
//!   divergence detector composed from existing kinds, not a dedicated
//!   derivative kind. The recorded contract gap: no per-tick signed
//!   rate-of-rise kind exists, so the composed monitor flags fast
//!   excursions in either direction; a one-sided derivative kind would
//!   revisit this.
//! - **Bad or stale data:** `level-remote` declares
//!   `stale_after_ticks` — the scripted playback's declared silence
//!   through the incident presents `Uncertain(Stale)`, never a healthy
//!   last-known value, and serving from it carries that quality onto
//!   the whole alarm path.
//! - **Managed lifecycle (decisions 71–73):** the high-level alarm
//!   declares bound writable `shelve` and `oos` points with a bounded
//!   `max_shelve_ticks`; the mode alarm declares no managed inputs and
//!   a zero shelving bound — never-shelvable, the incident's critical
//!   annunciation; the discrepancy alarm binds `suppress` to
//!   `sis-active`; every managed status point is `journaled`.
//! - **The protection boundary (decision 77):** the independent
//!   high-high layer is plant-side — never a component. Its reported
//!   states are declared `sim-scripted` field points: `sis-available`,
//!   `sis-fault`, `sis-trip`, `sis-proof-test`, and the
//!   operator-writable `sis-bypass` (the receipted attributed path the
//!   plant exposes — the lint's one named `WritableFieldPoint`
//!   finding). `sis-active` is the actuation contact the dynamics
//!   document's `bool_flow` gates on — the emergency draw acts on the
//!   process every plant step, whether or not the controller scans.
//!   The layer's trip *decision* is a level-triggered threshold the
//!   `ProcessElement` vocabulary does not express — the recorded
//!   contract gap this composition reports rather than works around —
//!   so the reference scenario asserts the contact on the declared
//!   `schedule::SIS_TRIP` tick, the same tick the scripted `sis-trip`
//!   report plays back: the layer's declared behavior stands on the
//!   schedule, its action remains the dynamics' own. Trip, bypass, and
//!   fault annunciate through `bool-latching-alarm`s grouped under the
//!   `protection` signal group with the states themselves, all
//!   `journaled`.
//!
//! ## The declared point-id scheme
//!
//! Field points occupy fixed blocks the checked-in dynamics document is
//! written against — `10..=16` the analog field points, `20` the gate
//! command, `30` the SIS actuation contact, `40..=45` the scripted
//! mode and protection states. Internal carriers start at `200`, alarm
//! points at `1000 + 10·a` (managed: `ack`/`shelve`/`oos` at offsets
//! 0–2, `alarm`/`unacknowledged`/`shelved`/`suppressed`/
//! `out_of_service` at 3–7; unmanaged: `ack`/`alarm`/`unacknowledged`
//! at 0–2), and every point's signal sits at `10000 + point`. The
//! scheme is deterministic in declaration order, so identical builder
//! invocations emit identical documents.
//!
//! Bool state signals declare an empty unit — a deliberate "unitless"
//! marker rather than an omitted one — so the document's only lint
//! finding is the bypass point's documented `WritableFieldPoint`.

use crate::specs::{
    BoolLatchingAlarmInstance, BoolLatchingAlarmSpec, DeviationMonitorSpec, FailoverSelectSpec,
    ManagedAlarmHandles, ManagedBoolLatchingAlarmSpec, ManagedInputs, ManagedLatchingAlarmSpec,
    ManualStationSpec, SignalFilterSpec, ThresholdChainSpec, ValveSpec,
};
use crate::station::{AlarmLayout, rationalization};
use crate::{
    BuildError, Direction, PlantBuilder, PointId, SignalId, Sink, Source, Value, parameters,
};
use dcs_model::{ComponentId, PlantModel};

/// Field point ids — the fixed block the dynamics document addresses.
pub mod points {
    use crate::PointId;

    /// The canal level measurement (`Float`, `In`) — the dynamics
    /// document's `integrator` output.
    pub const LEVEL: PointId = PointId(10);
    /// The remote operating position's level repeater (`Float`, `In`)
    /// — the `sim-scripted` channel whose declared silence presents
    /// `Uncertain(Stale)` through the incident.
    pub const LEVEL_REMOTE: PointId = PointId(11);
    /// The declared tide forcing (`Float`, `In`) — a dynamics input.
    pub const INFLOW: PointId = PointId(12);
    /// The summed net flow the level integrator advances (`Float`,
    /// `In`).
    pub const NET_FLOW: PointId = PointId(13);
    /// The inflow the confirmed-open gate admits (`Float`, `In`) — the
    /// `scaled_flow` on `gate-fb`.
    pub const GATE_FLOW: PointId = PointId(14);
    /// The independent layer's emergency discharge (`Float`, `In`) —
    /// the `bool_flow` gated on `sis-active`.
    pub const SIS_DRAW: PointId = PointId(15);
    /// The gate's confirmed position (`Float`, `In`).
    pub const GATE_FB: PointId = PointId(16);
    /// The gate position demand (`Float`, `Out`).
    pub const GATE_CMD: PointId = PointId(20);
    /// The protection layer's actuation contact (`Bool`, `In`) — the
    /// plant-side action the dynamics gate on, `journaled`.
    pub const SIS_ACTIVE: PointId = PointId(30);
    /// The field-reported gate-control mode (`Bool`, `In`,
    /// `journaled`) — `false` automatic, `true` local manual.
    pub const GATE_MODE: PointId = PointId(40);
    /// The protection layer's reported availability (`Bool`, `In`,
    /// `journaled`).
    pub const SIS_AVAILABLE: PointId = PointId(41);
    /// The protection layer's reported fault (`Bool`, `In`,
    /// `journaled`).
    pub const SIS_FAULT: PointId = PointId(42);
    /// The protection layer's reported trip state (`Bool`, `In`,
    /// `journaled`).
    pub const SIS_TRIP: PointId = PointId(43);
    /// The protection layer's proof-test state (`Bool`, `In`,
    /// `journaled`).
    pub const SIS_PROOF_TEST: PointId = PointId(44);
    /// The plant-exposed protection bypass (`Bool`, `In`, `journaled`)
    /// — the operator's bypass rides the receipted writable-point
    /// surface, decision 77's declared path.
    pub const SIS_BYPASS: PointId = PointId(45);
}

/// Internal carrier ids — the scenario wiring.
mod carriers {
    pub const LEVEL_SEL: u64 = 200;
    pub const LEVEL_FILT_IN: u64 = 201;
    pub const LEVEL_TREND_IN: u64 = 202;
    pub const LEVEL_FILTERED: u64 = 203;
    pub const LEVEL_CHAIN: u64 = 204;
    pub const LEVEL_LAH: u64 = 205;
    pub const LEVEL_ROR: u64 = 206;
    pub const LEVEL_TREND: u64 = 207;
    pub const LEVEL_TREND_DEV: u64 = 208;
    pub const DEVIATION: u64 = 209;
    pub const DEVIATING: u64 = 210;
    pub const DEVIATING_IN: u64 = 211;
    pub const BACKUP_ACTIVE: u64 = 212;
    pub const BACKUP_ACTIVE_IN: u64 = 213;
    pub const MANUAL_ACTIVE: u64 = 214;
    pub const MANUAL_ACTIVE_IN: u64 = 215;
    pub const DISCREPANCY: u64 = 216;
    pub const DISCREPANCY_IN: u64 = 217;
    pub const GATE_DEMAND: u64 = 220;
    pub const GATE_DEMAND_IN: u64 = 221;
    pub const CHAIN_DEMAND: u64 = 230;
    pub const DUTY_CALL: u64 = 231;
    pub const LAG_CALL: u64 = 232;
    pub const BELOW_CUTOFF: u64 = 233;
    pub const HIGH_LEVEL: u64 = 234;
    pub const GATE_AUTO_DEMAND: u64 = 240;
    pub const GATE_MANUAL_DEMAND: u64 = 241;
}

/// The declared incident schedule — the scripted field timeline the
/// checked-in model encodes, in driver ticks (one `driver.step` per
/// scan, so a tick lands in the same-numbered scan's input read).
pub mod schedule {
    /// The field fault switches the gate to local manual.
    pub const MODE_TO_MANUAL: u64 = 6;
    /// The field mode reports automatic again.
    pub const MODE_TO_AUTO: u64 = 45;
    /// The remote repeater's last update before the comms freeze —
    /// `level-remote` presents `Uncertain(Stale)` once
    /// `stale_after_ticks` elapses past this tick.
    pub const REMOTE_LAST_UPDATE: u64 = 10;
    /// The remote repeater's comms recover.
    pub const REMOTE_RECOVERY: u64 = 30;
    /// The independent high-high layer trips — its reported `sis-trip`
    /// state asserts and the scenario drives `sis-active` the same
    /// scan so the dynamics' relief acts.
    pub const SIS_TRIP: u64 = 24;
    /// The protection layer's reported fault asserts.
    pub const SIS_FAULT_ON: u64 = 34;
    /// The protection layer's reported fault clears.
    pub const SIS_FAULT_OFF: u64 = 38;
    /// The protection layer's proof test begins.
    pub const PROOF_TEST_ON: u64 = 47;
    /// The protection layer's proof test ends.
    pub const PROOF_TEST_OFF: u64 = 49;
}

/// Alarm points: alarm `a` owns `ALARM_BASE + a * 10 .. +10`.
const ALARM_BASE: u64 = 1000;
/// Every point's signal id is `SIGNAL_BASE + point`.
const SIGNAL_BASE: u64 = 10_000;

/// The bound a single-sided level alarm parks its unused limit at.
const PARKED_LIMIT: f64 = 1.0e9;

/// The remote repeater's declared freshness budget — three scans past
/// the last scripted update the image sample lands `Uncertain(Stale)`.
const REMOTE_STALE_AFTER: u64 = 3;

/// The scenario's tunable contract — level setpoints, filter and
/// verification constants, and the shelving bound.
/// [`reference`](Self::reference) is the checked-in document's
/// configuration.
#[derive(Debug, Clone, PartialEq)]
pub struct IjmuidenConfig {
    /// Threshold-chain setpoints in metres; must satisfy
    /// `cutoff < stop < start < lag_start < high`. For a discharge gate
    /// the calls are annunciation rungs, not pump stages.
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
    /// The alarm-path `signal-filter` smoothing factor.
    pub filter_alpha: f64,
    /// The slower `signal-filter` factor producing the divergence
    /// detector's lagged trend.
    pub trend_alpha: f64,
    /// The divergence detector's windowed relative-deviation bound.
    pub deviation_limit: f64,
    /// The divergence detector's accumulation window, in scans.
    pub window_ticks: i64,
    /// `manual-station`'s per-scan bumpless-transfer bound.
    pub transfer_delta: f64,
    /// `valve`'s command-versus-feedback agreement tolerance.
    pub valve_tolerance: f64,
    /// `valve`'s consecutive-deviating-scans budget before
    /// `discrepancy` asserts.
    pub discrepancy_ticks: i64,
    /// The high-level alarm's hysteresis band, in metres.
    pub lah_hysteresis: f64,
    /// The high-level alarm's shelving bound — the asserting scan
    /// counts as the first.
    pub lah_max_shelve_ticks: i64,
}

impl IjmuidenConfig {
    /// The reference scenario the checked-in documents record: the
    /// canal level starting at 3.0 m rising through `start`, `lag_start`,
    /// and `high` toward the independent layer's trip region.
    pub fn reference() -> Self {
        Self {
            cutoff: 1.0,
            stop: 1.5,
            start: 3.5,
            lag_start: 4.2,
            high: 5.0,
            on_bad_demand: 0,
            filter_alpha: 0.6,
            trend_alpha: 0.15,
            deviation_limit: 0.05,
            window_ticks: 3,
            transfer_delta: 0.5,
            valve_tolerance: 0.05,
            discrepancy_ticks: 2,
            lah_hysteresis: 0.1,
            lah_max_shelve_ticks: 6,
        }
    }
}

/// One managed alarm's place in the emitted document — the two-flag
/// surface plus the decision-71 managed status points, and the
/// declared lifecycle inputs where bound.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ManagedAlarmLayout {
    /// The alarm component instance's id.
    pub component: ComponentId,
    /// The writable internal `In` point the operator ack lands on.
    pub ack: PointId,
    /// The writable internal `In` point carrying the shelve request —
    /// `Some` only where the instance declares the `shelve` port.
    pub shelve: Option<PointId>,
    /// The writable internal `In` point carrying the out-of-service
    /// command — `Some` only where the instance declares the `oos`
    /// port.
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

/// Where everything the composition declares landed — the ids the
/// dynamics document, the scripted run, and any embedding surface
/// address.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IjmuidenLayout {
    /// The canal level field point — the integrator's output.
    pub level: PointId,
    /// The stale-bearing remote repeater field point.
    pub level_remote: PointId,
    /// The declared tide forcing field point.
    pub inflow: PointId,
    /// The summed net flow field point.
    pub net_flow: PointId,
    /// The gate-admitted inflow field point.
    pub gate_flow: PointId,
    /// The protection layer's discharge field point.
    pub sis_draw: PointId,
    /// The gate's confirmed-position field point.
    pub gate_fb: PointId,
    /// The gate demand field output.
    pub gate_cmd: PointId,
    /// The protection layer's actuation contact.
    pub sis_active: PointId,
    /// The field-reported gate-control mode point.
    pub gate_mode: PointId,
    /// The protection layer's reported availability.
    pub sis_available: PointId,
    /// The protection layer's reported fault.
    pub sis_fault: PointId,
    /// The protection layer's reported trip.
    pub sis_trip: PointId,
    /// The protection layer's proof-test state.
    pub sis_proof_test: PointId,
    /// The plant-exposed writable protection bypass.
    pub sis_bypass: PointId,
    /// The failover-selected level carrier.
    pub level_selected: PointId,
    /// The filtered level carrier the chain and alarms read.
    pub level_filtered: PointId,
    /// The slow-trend level carrier.
    pub level_trend: PointId,
    /// The divergence detector's windowed deviation carrier.
    pub deviation: PointId,
    /// The divergence detector's `deviating` carrier — `journaled`.
    pub deviating: PointId,
    /// The failover's `backup_active` carrier — `journaled`.
    pub backup_active: PointId,
    /// The station's `manual_active` carrier — `journaled`.
    pub manual_active: PointId,
    /// The valve's `discrepancy` carrier — `journaled`.
    pub discrepancy: PointId,
    /// The station's driven gate-demand carrier.
    pub gate_demand: PointId,
    /// The chain's stage-count demand carrier.
    pub chain_demand: PointId,
    /// The chain's `duty_call` flag carrier — `journaled`.
    pub duty_call: PointId,
    /// The chain's `lag_call` flag carrier — `journaled`.
    pub lag_call: PointId,
    /// The chain's `below_cutoff` flag carrier — `journaled`.
    pub below_cutoff: PointId,
    /// The chain's `high_level` flag carrier — `journaled`.
    pub high_level: PointId,
    /// The automatic layer's standing gate demand.
    pub gate_auto_demand: PointId,
    /// The operator's writable manual gate demand.
    pub gate_manual_demand: PointId,
    /// The `failover-select` instance's id.
    pub failover: ComponentId,
    /// The alarm-path `signal-filter` instance's id.
    pub filter: ComponentId,
    /// The trend `signal-filter` instance's id.
    pub trend: ComponentId,
    /// The `threshold-chain` instance's id.
    pub threshold_chain: ComponentId,
    /// The `deviation-monitor` instance's id.
    pub divergence: ComponentId,
    /// The `manual-station` instance's id.
    pub manual_station: ComponentId,
    /// The `valve` instance's id.
    pub valve: ComponentId,
    /// The managed high-level alarm — the shelvable, out-of-service
    /// path.
    pub high_level_alarm: ManagedAlarmLayout,
    /// The managed mode-change alarm — never-shelvable.
    pub mode_alarm: ManagedAlarmLayout,
    /// The managed confirmed-state discrepancy alarm — the
    /// designed-suppression path.
    pub discrepancy_alarm: ManagedAlarmLayout,
    /// The divergence (rate-of-rise) alarm.
    pub rate_of_rise_alarm: AlarmLayout,
    /// The backup-measurement-serving alarm.
    pub backup_active_alarm: AlarmLayout,
    /// The protection-layer trip alarm.
    pub sis_trip_alarm: AlarmLayout,
    /// The protection-layer bypass alarm.
    pub sis_bypass_alarm: AlarmLayout,
    /// The protection-layer fault alarm.
    pub sis_fault_alarm: AlarmLayout,
}

/// The composed scenario: the emitted document plus the layout every
/// declared id landed on.
#[derive(Debug, Clone)]
pub struct Ijmuiden {
    /// The versioned plant-model document.
    pub model: PlantModel,
    /// The id map into it.
    pub layout: IjmuidenLayout,
}

/// One scripted channel's declared entries — `(tick, value)` pairs
/// emitted into the device `script` parameter.
fn script_json(channels: &[(&str, serde_json::Value)]) -> serde_json::Value {
    let entries: serde_json::Map<String, serde_json::Value> = channels
        .iter()
        .map(|(name, entries)| (name.to_string(), entries.clone()))
        .collect();
    serde_json::Value::Object(entries)
}

/// `(tick, value)` bool entries as script JSON.
fn bool_script(entries: &[(u64, bool)]) -> serde_json::Value {
    serde_json::Value::Array(
        entries
            .iter()
            .map(|(tick, value)| serde_json::json!({ "tick": tick, "value": value }))
            .collect(),
    )
}

/// `(tick, value)` float entries as script JSON.
fn float_script(entries: &[(u64, f64)]) -> serde_json::Value {
    serde_json::Value::Array(
        entries
            .iter()
            .map(|(tick, value)| serde_json::json!({ "tick": tick, "value": value }))
            .collect(),
    )
}

/// Composes the IJmuiden-pattern scenario under `config` and emits its
/// [`PlantModel`].
///
/// Every component registers through its typed spec and every
/// connection goes through typed handles — port existence, direction,
/// and value kind are compile-time-checked where the types reach and
/// `build`-checked where they do not.
pub fn ijmuiden(config: &IjmuidenConfig) -> Result<Ijmuiden, BuildError> {
    let mut plant = PlantBuilder::new();

    // The local simulated devices: analog in, the gate command out,
    // and the SIS actuation contact.
    let ai = plant.device("sim-ai").id;
    let ao = plant.device("sim-ao").id;
    let di = plant.device("sim-di").id;

    // The scripted devices: the remote operating position's repeater
    // and the field mode switch on one panel, the protection layer's
    // reported states on the other — decision 77's declared-behavior
    // device, the layer's states playing back on schedule.
    let remote = {
        use schedule::*;
        let device = plant.device("sim-scripted");
        device.parameters.insert(
            "script".to_string(),
            script_json(&[
                (
                    "level-remote",
                    float_script(&[
                        (0, 3.0),
                        (4, 3.5),
                        (8, 4.0),
                        (REMOTE_LAST_UPDATE, 4.3),
                        (REMOTE_RECOVERY, 4.2),
                        (34, 2.9),
                        (38, 1.6),
                        (42, 1.3),
                        (46, 1.8),
                        (50, 2.3),
                        (54, 2.8),
                        (58, 3.3),
                        (62, 3.9),
                        (66, 4.4),
                        (70, 4.9),
                        (74, 5.4),
                    ]),
                ),
                (
                    "gate-mode",
                    bool_script(&[(0, false), (MODE_TO_MANUAL, true), (MODE_TO_AUTO, false)]),
                ),
            ]),
        );
        device.id
    };
    let sis_panel = {
        use schedule::*;
        let device = plant.device("sim-scripted");
        device.parameters.insert(
            "script".to_string(),
            script_json(&[
                ("sis-available", bool_script(&[(0, true)])),
                (
                    "sis-fault",
                    bool_script(&[(0, false), (SIS_FAULT_ON, true), (SIS_FAULT_OFF, false)]),
                ),
                ("sis-trip", bool_script(&[(0, false), (SIS_TRIP, true)])),
                (
                    "sis-proof-test",
                    bool_script(&[(0, false), (PROOF_TEST_ON, true), (PROOF_TEST_OFF, false)]),
                ),
                ("sis-bypass", bool_script(&[(0, false)])),
            ]),
        );
        device.id
    };

    let level_ch = plant.channel::<f64>(ai, "level", Direction::In);
    let inflow_ch = plant.channel::<f64>(ai, "inflow", Direction::In);
    let net_flow_ch = plant.channel::<f64>(ai, "net-flow", Direction::In);
    let gate_flow_ch = plant.channel::<f64>(ai, "gate-flow", Direction::In);
    let sis_draw_ch = plant.channel::<f64>(ai, "sis-draw", Direction::In);
    let gate_fb_ch = plant.channel::<f64>(ai, "gate-fb", Direction::In);
    let gate_cmd_ch = plant.channel::<f64>(ao, "gate-cmd", Direction::Out);
    let sis_active_ch = plant.channel::<bool>(di, "sis-active", Direction::In);
    let level_remote_ch = plant.channel::<f64>(remote, "level-remote", Direction::In);
    let gate_mode_ch = plant.channel::<bool>(remote, "gate-mode", Direction::In);
    let sis_available_ch = plant.channel::<bool>(sis_panel, "sis-available", Direction::In);
    let sis_fault_ch = plant.channel::<bool>(sis_panel, "sis-fault", Direction::In);
    let sis_trip_ch = plant.channel::<bool>(sis_panel, "sis-trip", Direction::In);
    let sis_proof_test_ch = plant.channel::<bool>(sis_panel, "sis-proof-test", Direction::In);
    let sis_bypass_ch = plant.channel::<bool>(sis_panel, "sis-bypass", Direction::In);

    // Field points — `inflow`, `net-flow`, `gate-flow`, and `sis-draw`
    // are produced by the dynamics document's elements, so their
    // handles go unused here.
    let level = plant.field_input::<f64>(points::LEVEL, level_ch, false);
    plant.field_input::<f64>(points::INFLOW, inflow_ch, false);
    plant.field_input::<f64>(points::NET_FLOW, net_flow_ch, false);
    plant.field_input::<f64>(points::GATE_FLOW, gate_flow_ch, false);
    plant.field_input::<f64>(points::SIS_DRAW, sis_draw_ch, false);
    let gate_fb = plant.field_input::<f64>(points::GATE_FB, gate_fb_ch, false);
    let gate_cmd = plant.field_output::<f64>(points::GATE_CMD, gate_cmd_ch);
    let sis_active = plant.field_input::<bool>(points::SIS_ACTIVE, sis_active_ch, false);
    // The remote repeater carries the scenario's declared freshness
    // budget: a frozen reading presents `Uncertain(Stale)`, never a
    // healthy last-known value (decision 45's seam in decision 75).
    let level_remote = plant.field_input_stale_after::<f64>(
        points::LEVEL_REMOTE,
        level_remote_ch,
        false,
        REMOTE_STALE_AFTER,
    );
    let gate_mode = plant.field_input::<bool>(points::GATE_MODE, gate_mode_ch, false);
    let sis_available = plant.field_input::<bool>(points::SIS_AVAILABLE, sis_available_ch, false);
    let sis_fault = plant.field_input::<bool>(points::SIS_FAULT, sis_fault_ch, false);
    let sis_trip = plant.field_input::<bool>(points::SIS_TRIP, sis_trip_ch, false);
    let sis_proof_test =
        plant.field_input::<bool>(points::SIS_PROOF_TEST, sis_proof_test_ch, false);
    // The operator bypass rides the receipted writable-point surface the
    // plant exposes — the document's one named `WritableFieldPoint`
    // lint finding.
    let sis_bypass = plant.field_input::<bool>(points::SIS_BYPASS, sis_bypass_ch, true);

    // Decision 74's durable record: the mode point and every
    // protection-layer state are `journaled` — each transition lands in
    // the journal beside the receipts that caused it.
    for point in [
        sis_active,
        gate_mode,
        sis_available,
        sis_fault,
        sis_trip,
        sis_proof_test,
        sis_bypass,
    ] {
        plant.journaled(point);
    }

    signal(
        &mut plant,
        points::LEVEL,
        "level",
        "m",
        "Canal level — the integrator the dynamics document advances",
        "level",
    );
    signal(
        &mut plant,
        points::LEVEL_REMOTE,
        "level-remote",
        "m",
        "Remote operating position's level repeater — frozen presents stale",
        "level",
    );
    signal(
        &mut plant,
        points::INFLOW,
        "inflow",
        "m/s",
        "Declared tide forcing — the dynamics document's forcing input",
        "level",
    );
    signal(
        &mut plant,
        points::NET_FLOW,
        "net-flow",
        "m/s",
        "Net canal flow: tide plus gate-admitted flow minus the relief draw",
        "level",
    );
    signal(
        &mut plant,
        points::GATE_FLOW,
        "gate-flow",
        "m/s",
        "Inflow the confirmed-open gate admits",
        "gate",
    );
    signal(
        &mut plant,
        points::SIS_DRAW,
        "sis-draw",
        "m/s",
        "Independent layer's emergency discharge — acts whether or not the controller scans",
        "protection",
    );
    signal(
        &mut plant,
        points::GATE_FB,
        "gate-fb",
        "fraction",
        "Gate confirmed position — the feedback the command proves against",
        "gate",
    );
    signal(
        &mut plant,
        points::GATE_CMD,
        "gate-cmd",
        "fraction",
        "Gate position demand driven to the field",
        "gate",
    );
    signal(
        &mut plant,
        points::SIS_ACTIVE,
        "sis-active",
        "",
        "Protection layer's actuation contact — the relief's drive",
        "protection",
    );
    signal(
        &mut plant,
        points::GATE_MODE,
        "gate-mode",
        "",
        "Field-reported gate-control mode — true is local manual",
        "gate",
    );
    signal(
        &mut plant,
        points::SIS_AVAILABLE,
        "sis-available",
        "",
        "Protection layer reports itself available",
        "protection",
    );
    signal(
        &mut plant,
        points::SIS_FAULT,
        "sis-fault",
        "",
        "Protection layer's reported fault",
        "protection",
    );
    signal(
        &mut plant,
        points::SIS_TRIP,
        "sis-trip",
        "",
        "Protection layer's reported high-high trip",
        "protection",
    );
    signal(
        &mut plant,
        points::SIS_PROOF_TEST,
        "sis-proof-test",
        "",
        "Protection layer's proof-test state",
        "protection",
    );
    signal(
        &mut plant,
        points::SIS_BYPASS,
        "sis-bypass",
        "",
        "Protection layer's bypass — the plant-exposed operator path",
        "protection",
    );

    // The shared internal carriers: the failover's selected level, the
    // filtered and trend levels, the chain's ladder, the demand path,
    // and every annunciation consumer. A carrier's `initial` seeds the
    // healthy cold-start state (mid-band level, gate held open,
    // automatic mode) so no alarm trips before the first real samples
    // land. A port's output may drive only one endpoint, so fan-out
    // goes through a declared `Out` carrier plus one `In` consumer per
    // reader — each link crossing the one-scan boundary.
    let level_sel = plant.internal_output::<f64>(PointId(carriers::LEVEL_SEL), 3.0);
    let level_filt_in = plant.internal_input::<f64>(PointId(carriers::LEVEL_FILT_IN), 3.0, false);
    let level_trend_in = plant.internal_input::<f64>(PointId(carriers::LEVEL_TREND_IN), 3.0, false);
    let level_filtered = plant.internal_output::<f64>(PointId(carriers::LEVEL_FILTERED), 3.0);
    let level_chain = plant.internal_input::<f64>(PointId(carriers::LEVEL_CHAIN), 3.0, false);
    let level_lah = plant.internal_input::<f64>(PointId(carriers::LEVEL_LAH), 3.0, false);
    let level_ror = plant.internal_input::<f64>(PointId(carriers::LEVEL_ROR), 3.0, false);
    let level_trend = plant.internal_output::<f64>(PointId(carriers::LEVEL_TREND), 3.0);
    let level_trend_dev =
        plant.internal_input::<f64>(PointId(carriers::LEVEL_TREND_DEV), 3.0, false);
    let deviation = plant.internal_output::<f64>(PointId(carriers::DEVIATION), 0.0);
    let deviating = plant.internal_output::<bool>(PointId(carriers::DEVIATING), false);
    let deviating_in = plant.internal_input::<bool>(PointId(carriers::DEVIATING_IN), false, false);
    let backup_active = plant.internal_output::<bool>(PointId(carriers::BACKUP_ACTIVE), false);
    let backup_active_in =
        plant.internal_input::<bool>(PointId(carriers::BACKUP_ACTIVE_IN), false, false);
    let manual_active = plant.internal_output::<bool>(PointId(carriers::MANUAL_ACTIVE), false);
    let manual_active_in =
        plant.internal_input::<bool>(PointId(carriers::MANUAL_ACTIVE_IN), false, false);
    let discrepancy = plant.internal_output::<bool>(PointId(carriers::DISCREPANCY), false);
    let discrepancy_in =
        plant.internal_input::<bool>(PointId(carriers::DISCREPANCY_IN), false, false);
    let gate_demand = plant.internal_output::<f64>(PointId(carriers::GATE_DEMAND), 1.0);
    let gate_demand_in = plant.internal_input::<f64>(PointId(carriers::GATE_DEMAND_IN), 1.0, false);
    let chain_demand = plant.internal_output::<i64>(PointId(carriers::CHAIN_DEMAND), 0);
    let duty_call = plant.internal_output::<bool>(PointId(carriers::DUTY_CALL), false);
    let lag_call = plant.internal_output::<bool>(PointId(carriers::LAG_CALL), false);
    let below_cutoff = plant.internal_output::<bool>(PointId(carriers::BELOW_CUTOFF), false);
    let high_level = plant.internal_output::<bool>(PointId(carriers::HIGH_LEVEL), false);
    // The automatic layer's standing demand — the program holds the
    // gate open for discharge — beside the operator's writable manual
    // entry, the receipted command path's target.
    let gate_auto_demand =
        plant.internal_input::<f64>(PointId(carriers::GATE_AUTO_DEMAND), 1.0, false);
    let gate_manual_demand =
        plant.internal_input::<f64>(PointId(carriers::GATE_MANUAL_DEMAND), 1.0, true);

    // The decision-74 durable record: the annunciation flags and the
    // mode status point are `journaled`.
    for point in [
        deviating,
        backup_active,
        manual_active,
        discrepancy,
        duty_call,
        lag_call,
        below_cutoff,
        high_level,
    ] {
        plant.journaled(point);
    }

    for (point, name, unit, description, group) in [
        (
            carriers::LEVEL_SEL,
            "level-selected",
            "m",
            "Failover-selected level the annunciation path controls on",
            "level",
        ),
        (
            carriers::LEVEL_FILT_IN,
            "level-filt-in",
            "m",
            "Selected level delivered to the alarm-path filter",
            "level",
        ),
        (
            carriers::LEVEL_TREND_IN,
            "level-trend-in",
            "m",
            "Selected level delivered to the trend filter",
            "level",
        ),
        (
            carriers::LEVEL_FILTERED,
            "level-filtered",
            "m",
            "Filtered selected level the chain and alarms read",
            "level",
        ),
        (
            carriers::LEVEL_CHAIN,
            "level-chain",
            "m",
            "Filtered level delivered to the threshold chain",
            "level",
        ),
        (
            carriers::LEVEL_LAH,
            "level-lah",
            "m",
            "Filtered level delivered to the high-level alarm",
            "level",
        ),
        (
            carriers::LEVEL_ROR,
            "level-ror",
            "m",
            "Filtered level delivered to the divergence detector",
            "level",
        ),
        (
            carriers::LEVEL_TREND,
            "level-trend",
            "m",
            "Slow level trend the divergence detector compares against",
            "level",
        ),
        (
            carriers::LEVEL_TREND_DEV,
            "level-trend-dev",
            "m",
            "Level trend delivered to the divergence detector",
            "level",
        ),
        (
            carriers::DEVIATION,
            "deviation",
            "fraction",
            "Windowed level-versus-trend relative deviation",
            "level",
        ),
        (
            carriers::DEVIATING,
            "deviating",
            "",
            "Level diverging from its trend — the composed rate-of-rise flag",
            "level",
        ),
        (
            carriers::DEVIATING_IN,
            "deviating-in",
            "",
            "Divergence flag delivered to its alarm",
            "level",
        ),
        (
            carriers::BACKUP_ACTIVE,
            "backup-active",
            "",
            "The remote repeater is serving the level path",
            "level",
        ),
        (
            carriers::BACKUP_ACTIVE_IN,
            "backup-active-in",
            "",
            "Backup-serving flag delivered to its alarm",
            "level",
        ),
        (
            carriers::MANUAL_ACTIVE,
            "manual-active",
            "",
            "The station reports manual mode — the mode annunciation's status",
            "gate",
        ),
        (
            carriers::MANUAL_ACTIVE_IN,
            "manual-active-in",
            "",
            "Manual-active flag delivered to its alarm",
            "gate",
        ),
        (
            carriers::DISCREPANCY,
            "discrepancy",
            "",
            "Gate demanded-but-not-confirmed mismatch",
            "gate",
        ),
        (
            carriers::DISCREPANCY_IN,
            "discrepancy-in",
            "",
            "Discrepancy flag delivered to its alarm",
            "gate",
        ),
        (
            carriers::GATE_DEMAND,
            "gate-demand",
            "fraction",
            "Gate demand the station drives",
            "gate",
        ),
        (
            carriers::GATE_DEMAND_IN,
            "gate-demand-in",
            "fraction",
            "Gate demand delivered to the valve",
            "gate",
        ),
        (
            carriers::CHAIN_DEMAND,
            "chain-demand",
            "stages",
            "Stage count the threshold chain holds",
            "level",
        ),
        (
            carriers::DUTY_CALL,
            "duty-call",
            "",
            "Level at or above the first annunciation rung",
            "level",
        ),
        (
            carriers::LAG_CALL,
            "lag-call",
            "",
            "Level at or above the urgent annunciation rung",
            "level",
        ),
        (
            carriers::BELOW_CUTOFF,
            "below-cutoff",
            "",
            "Level at or below the low-water mark",
            "level",
        ),
        (
            carriers::HIGH_LEVEL,
            "high-level",
            "",
            "Level at or above the high setpoint",
            "level",
        ),
        (
            carriers::GATE_AUTO_DEMAND,
            "gate-auto-demand",
            "fraction",
            "The automatic layer's standing gate demand",
            "gate",
        ),
        (
            carriers::GATE_MANUAL_DEMAND,
            "gate-manual-demand",
            "fraction",
            "The operator's manual gate demand",
            "gate",
        ),
    ] {
        signal(&mut plant, PointId(point), name, unit, description, group);
    }

    // The components, each through its typed spec.
    let failover = plant.add(FailoverSelectSpec::new(parameters([])));
    let filter = plant.add(SignalFilterSpec::new(parameters([(
        "alpha",
        Value::Float(config.filter_alpha),
    )])));
    let trend = plant.add(SignalFilterSpec::new(parameters([(
        "alpha",
        Value::Float(config.trend_alpha),
    )])));
    let chain = plant.add(ThresholdChainSpec::new(parameters([
        ("cutoff", Value::Float(config.cutoff)),
        ("stop", Value::Float(config.stop)),
        ("start", Value::Float(config.start)),
        ("lag_start", Value::Float(config.lag_start)),
        ("high", Value::Float(config.high)),
        ("on_bad_demand", Value::Int(config.on_bad_demand)),
    ])));
    // The composed rate-of-rise detector: the filtered level against
    // its slower trend, windowed — a sustained-divergence stand-in for
    // a dedicated derivative kind, the implementing ticket's
    // documented choice under decision 75.
    let divergence = plant.add(DeviationMonitorSpec::new(parameters([
        ("deviation_limit", Value::Float(config.deviation_limit)),
        ("window_ticks", Value::Int(config.window_ticks)),
    ])));
    let station = plant.add(ManualStationSpec::new(parameters([(
        "transfer_delta",
        Value::Float(config.transfer_delta),
    )])));
    let gate = plant.add(ValveSpec::new(parameters([
        ("tolerance", Value::Float(config.valve_tolerance)),
        ("discrepancy_ticks", Value::Int(config.discrepancy_ticks)),
    ])));

    // The decision-70 codes are declared data — the site priority/class
    // vocabulary and response budgets stay an open customer assumption.
    let lah = plant.add(ManagedLatchingAlarmSpec::new(
        parameters([
            ("low_limit", Value::Float(-PARKED_LIMIT)),
            ("high_limit", Value::Float(config.high)),
            ("hysteresis", Value::Float(config.lah_hysteresis)),
            ("max_shelve_ticks", Value::Int(config.lah_max_shelve_ticks)),
            ("priority", Value::Int(1)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(30)),
        ]),
        ManagedInputs {
            shelve: true,
            oos: true,
            suppress: false,
        },
        rationalization(
            "The canal overtops toward Amsterdam — the near-miss level",
            "Close the discharge gates and verify the protection layer's state",
            "lah-alarm",
        ),
    ));
    let mode_alarm = plant.add(ManagedBoolLatchingAlarmSpec::new(
        parameters([
            // Never shelvable, twice over: no `shelve` port is declared
            // and the bound is zero — the incident's critical
            // annunciation cannot be hidden.
            ("max_shelve_ticks", Value::Int(0)),
            ("priority", Value::Int(1)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(15)),
        ]),
        ManagedInputs::default(),
        rationalization(
            "The gate answers the field, not the control layer — the incident's root annunciation",
            "Confirm local control at the gate and verify the commanded position",
            "mode-alarm",
        ),
    ));
    let discrepancy_alarm = plant.add(ManagedBoolLatchingAlarmSpec::new(
        parameters([
            ("max_shelve_ticks", Value::Int(0)),
            ("priority", Value::Int(1)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(15)),
        ]),
        // The designed-suppression wiring: while the protection layer's
        // actuation stands, the standing mismatch is the trip's
        // consequence, not a new demand — decision 73's declared
        // `suppress`, the decision-76 engineered flood shape.
        ManagedInputs {
            shelve: false,
            oos: false,
            suppress: true,
        },
        rationalization(
            "The gate is demanded safe but confirmed open — water keeps coming in",
            "Verify the gate's local control and the protection layer's action",
            "disc-alarm",
        ),
    ));
    let ror_alarm = plant.add(BoolLatchingAlarmSpec::new(
        parameters([
            ("priority", Value::Int(2)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(30)),
        ]),
        rationalization(
            "The canal level is diverging from its trend — rising or falling fast",
            "Check the level trend and the gate positions",
            "ror-alarm",
        ),
    ));
    let backup_alarm = plant.add(BoolLatchingAlarmSpec::new(
        parameters([
            ("priority", Value::Int(2)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(30)),
        ]),
        rationalization(
            "The remote repeater carries the level path — degraded or stale data may be serving",
            "Check the primary level instrument and the remote link",
            "backup-alarm",
        ),
    ));
    let sis_trip_alarm = plant.add(BoolLatchingAlarmSpec::new(
        parameters([
            ("priority", Value::Int(1)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(5)),
        ]),
        rationalization(
            "The independent high-high layer has tripped — the canal is beyond the DCS's protection",
            "Confirm the layer's action and the gate state; do not credit the alarm path",
            "sis-trip-alarm",
        ),
    ));
    let sis_bypass_alarm = plant.add(BoolLatchingAlarmSpec::new(
        parameters([
            ("priority", Value::Int(2)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(30)),
        ]),
        rationalization(
            "The protection layer is bypassed — its independent action is unavailable",
            "Restore the bypass and re-verify the layer",
            "sis-bypass-alarm",
        ),
    ));
    let sis_fault_alarm = plant.add(BoolLatchingAlarmSpec::new(
        parameters([
            ("priority", Value::Int(2)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(30)),
        ]),
        rationalization(
            "The protection layer reports a fault — its availability is in question",
            "Investigate the layer's fault report",
            "sis-fault-alarm",
        ),
    ));

    // Measurement path: the failover selects between the canal level
    // and the remote repeater; the filtered selection fans out to the
    // chain, the level alarm, and the divergence detector; the slower
    // trend feeds the detector's `expected`.
    plant.connect(level, &failover.primary);
    plant.connect(level_remote, &failover.backup);
    plant.connect(&failover.out, level_sel);
    plant.connect(&failover.backup_active, backup_active);
    plant.connect(level_filt_in, level_sel);
    plant.connect(level_trend_in, level_sel);
    plant.connect(level_filt_in, &filter.input);
    plant.connect(&filter.out, level_filtered);
    plant.connect(level_trend_in, &trend.input);
    plant.connect(&trend.out, level_trend);
    plant.connect(level_chain, level_filtered);
    plant.connect(level_lah, level_filtered);
    plant.connect(level_ror, level_filtered);
    plant.connect(level_trend_dev, level_trend);
    plant.connect(level_chain, &chain.level);
    plant.connect(&chain.demand, chain_demand);
    plant.connect(&chain.duty_call, duty_call);
    plant.connect(&chain.lag_call, lag_call);
    plant.connect(&chain.below_cutoff, below_cutoff);
    plant.connect(&chain.high_level, high_level);
    plant.connect(level_trend_dev, &divergence.expected);
    plant.connect(level_ror, &divergence.measured);
    plant.connect(&divergence.deviation, deviation);
    plant.connect(&divergence.deviating, deviating);
    plant.connect(deviating_in, deviating);
    plant.connect(backup_active_in, backup_active);

    // The command path: the field-reported mode selects between the
    // automatic layer's standing demand and the operator's manual
    // entry; the station drives the valve's command, and `gate-cmd`
    // leaves to the field. `manual_active` and `discrepancy` are the
    // incident's two annunciations.
    plant.connect(gate_auto_demand, &station.control);
    plant.connect(gate_manual_demand, &station.manual);
    plant.connect(gate_mode, &station.mode);
    plant.connect(&station.out, gate_demand);
    plant.connect(&station.manual_active, manual_active);
    plant.connect(gate_demand_in, gate_demand);
    plant.connect(manual_active_in, manual_active);
    plant.connect(gate_demand_in, &gate.cmd);
    plant.connect(&gate.out, gate_cmd);
    plant.connect(gate_fb, &gate.fb);
    plant.connect(&gate.discrepancy, discrepancy);
    plant.connect(discrepancy_in, discrepancy);

    // The alarm set — each latching on its condition, the managed
    // instances carrying their decision-71 surfaces.
    let high_level_alarm = managed_alarm(
        &mut plant,
        0,
        lah.id,
        &lah.ack,
        &lah.managed,
        &lah.alarm,
        &lah.unacknowledged,
        "lah",
        "alarms",
    );
    plant.connect(level_lah, &lah.input);
    let mode_alarm_layout = managed_alarm(
        &mut plant,
        1,
        mode_alarm.id,
        &mode_alarm.ack,
        &mode_alarm.managed,
        &mode_alarm.alarm,
        &mode_alarm.unacknowledged,
        "mode",
        "alarms",
    );
    plant.connect(manual_active_in, &mode_alarm.input);
    let discrepancy_alarm_layout = managed_alarm(
        &mut plant,
        2,
        discrepancy_alarm.id,
        &discrepancy_alarm.ack,
        &discrepancy_alarm.managed,
        &discrepancy_alarm.alarm,
        &discrepancy_alarm.unacknowledged,
        "disc",
        "alarms",
    );
    plant.connect(discrepancy_in, &discrepancy_alarm.input);
    // The designed-suppression wiring: the protection layer's
    // actuation contact drives the discrepancy alarm's `suppress`
    // input — the consequential burst withheld while the independent
    // action owns the hazard, the standing truth still on record.
    plant.connect(
        sis_active,
        discrepancy_alarm
            .managed
            .suppress
            .as_ref()
            .expect("the discrepancy alarm declares `suppress`"),
    );

    let rate_of_rise_alarm = unmanaged_alarm(&mut plant, 3, &ror_alarm, "ror", "alarms");
    plant.connect(deviating_in, &ror_alarm.input);
    let backup_active_alarm = unmanaged_alarm(&mut plant, 4, &backup_alarm, "backup", "alarms");
    plant.connect(backup_active_in, &backup_alarm.input);
    let sis_trip_alarm_layout =
        unmanaged_alarm(&mut plant, 5, &sis_trip_alarm, "sis-trip", "protection");
    plant.connect(sis_trip, &sis_trip_alarm.input);
    let sis_bypass_alarm_layout =
        unmanaged_alarm(&mut plant, 6, &sis_bypass_alarm, "sis-bypass", "protection");
    plant.connect(sis_bypass, &sis_bypass_alarm.input);
    let sis_fault_alarm_layout =
        unmanaged_alarm(&mut plant, 7, &sis_fault_alarm, "sis-fault", "protection");
    plant.connect(sis_fault, &sis_fault_alarm.input);

    let model = plant.build()?;
    Ok(Ijmuiden {
        model,
        layout: IjmuidenLayout {
            level: points::LEVEL,
            level_remote: points::LEVEL_REMOTE,
            inflow: points::INFLOW,
            net_flow: points::NET_FLOW,
            gate_flow: points::GATE_FLOW,
            sis_draw: points::SIS_DRAW,
            gate_fb: points::GATE_FB,
            gate_cmd: points::GATE_CMD,
            sis_active: points::SIS_ACTIVE,
            gate_mode: points::GATE_MODE,
            sis_available: points::SIS_AVAILABLE,
            sis_fault: points::SIS_FAULT,
            sis_trip: points::SIS_TRIP,
            sis_proof_test: points::SIS_PROOF_TEST,
            sis_bypass: points::SIS_BYPASS,
            level_selected: PointId(carriers::LEVEL_SEL),
            level_filtered: PointId(carriers::LEVEL_FILTERED),
            level_trend: PointId(carriers::LEVEL_TREND),
            deviation: PointId(carriers::DEVIATION),
            deviating: PointId(carriers::DEVIATING),
            backup_active: PointId(carriers::BACKUP_ACTIVE),
            manual_active: PointId(carriers::MANUAL_ACTIVE),
            discrepancy: PointId(carriers::DISCREPANCY),
            gate_demand: PointId(carriers::GATE_DEMAND),
            chain_demand: PointId(carriers::CHAIN_DEMAND),
            duty_call: PointId(carriers::DUTY_CALL),
            lag_call: PointId(carriers::LAG_CALL),
            below_cutoff: PointId(carriers::BELOW_CUTOFF),
            high_level: PointId(carriers::HIGH_LEVEL),
            gate_auto_demand: PointId(carriers::GATE_AUTO_DEMAND),
            gate_manual_demand: PointId(carriers::GATE_MANUAL_DEMAND),
            failover: failover.id,
            filter: filter.id,
            trend: trend.id,
            threshold_chain: chain.id,
            divergence: divergence.id,
            manual_station: station.id,
            valve: gate.id,
            high_level_alarm,
            mode_alarm: mode_alarm_layout,
            discrepancy_alarm: discrepancy_alarm_layout,
            rate_of_rise_alarm,
            backup_active_alarm,
            sis_trip_alarm: sis_trip_alarm_layout,
            sis_bypass_alarm: sis_bypass_alarm_layout,
            sis_fault_alarm: sis_fault_alarm_layout,
        },
    })
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
/// `ack`, the declared `shelve`/`oos` request points, and the five
/// status outputs — all `journaled` so every lifecycle transition
/// lands in the durable record beside the receipts that caused it.
/// Both managed kinds expose the same `id`/`ack`/`managed`/`alarm`/
/// `unacknowledged` fields, so the one helper serves either; the
/// caller wires the alarm's `in` port — its value kind differs
/// between them.
#[allow(clippy::too_many_arguments)]
fn managed_alarm(
    plant: &mut PlantBuilder,
    index: u64,
    component: ComponentId,
    ack_port: &Sink<bool>,
    managed: &ManagedAlarmHandles,
    alarm_port: &Source<bool>,
    unacknowledged_port: &Source<bool>,
    prefix: &str,
    group: &str,
) -> ManagedAlarmLayout {
    let base = ALARM_BASE + index * 10;
    let ack = plant.internal_input::<bool>(PointId(base), false, true);
    let shelve = managed
        .shelve
        .as_ref()
        .map(|_| plant.internal_input::<bool>(PointId(base + 1), false, true));
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
    // as durable `point_changed` entries. The writable request points
    // journal too: their transitions are the operator's lifecycle
    // actions recorded beside their attributed receipts. `ack` stays
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
        group,
    );
    if shelve.is_some() {
        signal(
            plant,
            PointId(base + 1),
            &format!("{prefix}-shelve"),
            "",
            "Operator shelving request for the alarm",
            group,
        );
    }
    if oos.is_some() {
        signal(
            plant,
            PointId(base + 2),
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
        (7, "out-of-service", "Taken out of service by the operator"),
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

/// Declares one unmanaged `bool-latching-alarm`'s points — the
/// writable `ack` and the `journaled` `alarm`/`unacknowledged` status
/// outputs — wires them, and registers their signals. The caller wires
/// the alarm's `in` port.
fn unmanaged_alarm(
    plant: &mut PlantBuilder,
    index: u64,
    instance: &BoolLatchingAlarmInstance,
    prefix: &str,
    group: &str,
) -> AlarmLayout {
    let base = ALARM_BASE + index * 10;
    let ack = plant.internal_input::<bool>(PointId(base), false, true);
    let alarm = plant.internal_output::<bool>(PointId(base + 1), false);
    let unacknowledged = plant.internal_output::<bool>(PointId(base + 2), false);
    plant.journaled(alarm);
    plant.journaled(unacknowledged);
    plant.connect(ack, instance.ack.clone());
    plant.connect(instance.alarm.clone(), alarm);
    plant.connect(instance.unacknowledged.clone(), unacknowledged);
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
        component: instance.id,
        ack: PointId(base),
        alarm: PointId(base + 1),
        unacknowledged: PointId(base + 2),
    }
}
