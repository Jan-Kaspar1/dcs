//! The reference granular-media filter bank — the issue-#274
//! composition at the builder seam decision 31 fixed.
//!
//! [`filter_bank`] composes the bank entirely through typed spec
//! handles and emits the versioned [`PlantModel`] document; the
//! checked-in copy lives at `crates/dcs-demo/fixtures/filter_bank.json`
//! beside its dynamics document `filter_bank_dynamics.json` — the
//! recorded fixture location the station and the dosing skid share.
//! `dcs-build/tests/filter_bank.rs` asserts the emitted document equals
//! that artifact.
//!
//! ## The composition
//!
//! - **Arbitration (decision 56):** one `backwash-coordinator` over
//!   `filters` filters, its `supply_ok`/`waste_ok`/`flow_ok` grant
//!   permissives aggregated upstream from declared plant state — the
//!   two availability contacts are field inputs and `flow_ok` is the
//!   inverse of the same online-flow-disturbance bound the disturbance
//!   alarm annunciates (decision 61), so the permissive and the
//!   annunciation read one measurement. `reorder` binds a writable
//!   internal `In` point, so the operator's standing queue instruction
//!   rides the receipted, actor-attributed path.
//! - **Triggers and attribution (decision 58):** the primaries compose
//!   upstream on separate inputs so the sequence owns attribution — a
//!   `timer` on the filter's filtering state for elapsed run time, an
//!   `alarm-monitor` on headloss for the terminal bound, an
//!   `alarm-monitor` on effluent turbidity for its bound, and the
//!   operator's writable start point. `auto_start` carries the
//!   jurisdiction-mandated permission as declared data.
//! - **Steps and equipment patterns (decision 57):** a six-step
//!   `backwash-sequence` table per filter — drain-down on the measured
//!   drain depth, air scour, high-rate wash, filter-to-waste on the
//!   turbidity bound, a reference-flow step that captures the
//!   clean-bed-headloss baseline, and the post-wash verification
//!   window. Each step's `phase_<n>` output composes the equipment
//!   pattern as `bool-gate` ORs; single-active-step semantics
//!   structurally exclude air scour and high-rate wash from asserting
//!   together, and a declared `interlock` on the wash-water demand path
//!   tripped by the air-scour phase flag is the belt-and-suspenders
//!   form. The drain-down filtration-rate hold is a `rate-limiter`
//!   bounding the wash-water demand's slew.
//! - **Grant handshake and the meanwhile state (decision 57):**
//!   `request` reaches `request_i` and `grant_i` returns as the run
//!   permissive; `done`/`aborted` drop `request`, which is the grant
//!   release — no separate protocol. The declared `queued_state` picks
//!   the in-service guard's trip source the helper emits: `active`
//!   (keep filtering until granted) or `request` (offline once
//!   queued).
//! - **Fault aggregate (decision 60):** a per-filter `bool-gate` OR of
//!   the 1 NTU turbidity tier's flag — decision 61's declared
//!   alarm-or-trip scope — the filter's equipment-fault contact, and
//!   every actuator's proven `fault`/`discrepancy` flag, wired into the
//!   sequence's `fault`; `on_fault_step`, `on_fault_policy`, and
//!   `abort_step` are declared table data.
//! - **Post-wash verification (decision 61):** the `phase-monitor` pair
//!   per filter — clean-bed headloss in `mode 1` against the baseline
//!   captured during the reference-flow step, and ripening turbidity in
//!   `mode 0` against the declared bound, both across the post-wash
//!   window.
//! - **Alarms (decision 61's set on the decision-71–73 managed kinds):**
//!   each filter carries the 0.3 NTU effluent-turbidity tier, the 1 NTU
//!   tier, terminal headloss, the aborted-backwash status, the
//!   equipment-fault aggregate, the CBHL excursion, and the ripening
//!   excursion; the bank carries the resource-blocked queue and the
//!   online-flow disturbance. Every managed status point is `journaled`
//!   under decision 74 — the consolidated M10 acceptance record's
//!   lifecycle-adoption requirement.
//!
//! ## The declared point-id scheme
//!
//! Bank field points occupy `10` supply-available, `11`
//! waste-available, `12` online-flow-disturbance. Per-filter field
//! points occupy `20+i`…`30+i` the headloss, effluent turbidity, its
//! simulated forcing, the water level above the bed, and the dynamics'
//! declared balance points; `40+i`…`47+i` the actuator contacts and the
//! equipment-fault report; `60+i`…`66+i` the seven driven commands; and
//! `70+i`/`80+i` the wash-water valve command and its position feedback
//! (`i` the 0-based filter index, `filters <= 12`). Bank carriers start
//! at `200`; each filter owns the block at `FILTER_BASE +
//! FILTER_STRIDE·i`, whose offsets the helper allocates in declaration
//! order — the operator's writable points first, then the in-service
//! aggregation, the coordinator handshake, the sequence's status
//! outputs, the phase-flag carriers with their delivered copies, and the
//! verification flags. Every alarm owns a ten-point block at
//! `ALARM_BASE + 10·a` with the managed layout the reference
//! compositions share: `ack`/`shelve`/`oos` at offsets 0–2 where the
//! instance declares the input, and
//! `alarm`/`unacknowledged`/`shelved`/`suppressed`/`out_of_service` at
//! 3–7. Bank alarms take `a` = 0–1 and filter `i`'s seven alarms
//! `a = 2 + 7·i + 0..=6`, in the order declared below. Every point's
//! signal sits at `SIGNAL_BASE + point`. The scheme is deterministic in
//! declaration order, so identical builder invocations emit identical
//! documents.
//!
//! ## Declared units (the dimensional-discipline decision)
//!
//! The bank is the filter-side adoption of architecture decision 106's
//! declared-unit metadata: headloss and the water level in `m`, effluent
//! turbidity in `NTU`, the wash-water demand and its feedback in `%`,
//! every `*_ticks` interval in `ticks`, and each analog alarm's bounds
//! in the unit of the value it observes. The Bool permissive, alarm,
//! status, and code carriers stay undeclared, their signals keeping the
//! explicit `""` marker.

use crate::specs::{
    AlarmMonitorSpec, BackwashCoordinatorInstance, BackwashCoordinatorSpec, BackwashSequenceSpec,
    BoolGateSpec, DigitalInputSpec, DigitalOutputSpec, InterlockSpec, ManagedAlarmHandles,
    ManagedBoolLatchingAlarmSpec, ManagedInputs, ManagedLatchingAlarmSpec, MotorSpec,
    PhaseMonitorSpec, RateLimiterSpec, TimerSpec, ValveSpec,
};
use crate::station::{AlarmLayout, rationalization};
use crate::{
    BuildError, DeviceId, Direction, InPoint, OutPoint, PlantBuilder, PointId, SignalId, Sink,
    Source, Value, parameters, unit,
};
use dcs_model::{ComponentId, PlantModel};

/// The filter count every checked-in document and its six-step table
/// assume: the bank-width bound the point-id scheme and the alarm
/// numbering share.
pub const REFERENCE_FILTERS: usize = 3;

/// The step count every reference filter's table declares — the six
/// phases the equipment patterns and the post-wash window address.
pub const STEPS: usize = 6;

/// The alarms each filter declares, in declaration order.
pub const FILTER_ALARMS: u64 = 7;

/// Field point ids — the fixed blocks the dynamics document addresses.
///
/// Per-filter field points live in one block per filter,
/// `FILTER_FIELD_BASE + FILTER_FIELD_STRIDE·index` plus the offsets below,
/// so a filter's eleven analog points, its eleven contacts, and its eight
/// driven commands never collide with a neighbour's.
pub mod points {
    use crate::PointId;

    /// The first per-filter field block.
    pub const FILTER_FIELD_BASE: u64 = 20;
    /// The per-filter field-block stride — wide enough for every offset the
    /// reference bank declares.
    pub const FILTER_FIELD_STRIDE: u64 = 64;
    /// The analog offsets within a filter's field block.
    pub mod analog {
        /// The headloss across the bed (`Float`, `In`, `m`).
        pub const HEADLOSS: u64 = 0;
        /// The effluent turbidity (`Float`, `In`, `NTU`).
        pub const TURBIDITY: u64 = 1;
        /// The simulated effluent-turbidity forcing (`Float`, `In`, `NTU`).
        pub const TURBIDITY_FORCING: u64 = 2;
        /// The water level above the bed (`Float`, `In`, `m`).
        pub const LEVEL: u64 = 3;
        /// The simulated fill rate above the bed (`Float`, `In`, `m/scan`).
        pub const INFLOW: u64 = 4;
        /// The simulated drain rate while the bed drains (`Float`, `In`,
        /// `m/scan`).
        pub const DRAIN_RATE: u64 = 5;
        /// The summed net rate the level integrator advances on (`Float`,
        /// `In`, `m/scan`).
        pub const NET_DRAW: u64 = 6;
        /// The measured drain depth — the drain step's measured input
        /// (`Float`, `In`, `m`).
        pub const DRAIN_DEPTH: u64 = 7;
        /// The accumulated filtered volume the headloss rise rides on
        /// (`Float`, `In`, `m3`).
        pub const FILTERED_VOLUME: u64 = 8;
        /// The accumulated headloss the filtered volume produces (`Float`,
        /// `In`, `m`).
        pub const HEADLOSS_RISE: u64 = 9;
        /// The headloss the open wash-water valve removes (`Float`, `In`,
        /// `m`).
        pub const WASH_DRAW: u64 = 10;
        /// The wash-water valve position command (`Float`, `Out`, `%`).
        pub const WASH_VALVE_CMD: u64 = 26;
        /// The wash-water valve position feedback (`Float`, `In`, `%`).
        pub const WASH_VALVE_POS: u64 = 27;
    }
    /// The contact offsets within a filter's field block.
    pub mod contact {
        /// The inlet-valve open contact (`Bool`, `In`).
        pub const INLET_FB: u64 = 11;
        /// The outlet-valve open contact (`Bool`, `In`).
        pub const OUTLET_FB: u64 = 12;
        /// The waste-valve open contact (`Bool`, `In`).
        pub const WASTE_FB: u64 = 13;
        /// The air-valve open contact (`Bool`, `In`).
        pub const AIR_FB: u64 = 14;
        /// The air-scour blower run contact (`Bool`, `In`).
        pub const BLOWER_RUN: u64 = 15;
        /// The backwash-pump run contact (`Bool`, `In`).
        pub const PUMP_RUN: u64 = 16;
        /// The filter's equipment-fault contact (`Bool`, `In`) — the
        /// declared plant-side protection report.
        pub const FAULT: u64 = 17;
        /// The plant-side drained-bed contact (`Bool`, `In`) — the
        /// dynamics' falling-bound test the drain step's measured input is
        /// built on.
        pub const DRAINED: u64 = 18;
        /// The inlet-valve command (`Bool`, `Out`).
        pub const INLET_CMD: u64 = 19;
        /// The outlet-valve command (`Bool`, `Out`).
        pub const OUTLET_CMD: u64 = 20;
        /// The waste-valve command (`Bool`, `Out`).
        pub const WASTE_CMD: u64 = 21;
        /// The air-valve command (`Bool`, `Out`).
        pub const AIR_CMD: u64 = 22;
        /// The air-scour blower run command (`Bool`, `Out`).
        pub const BLOWER_CMD: u64 = 23;
        /// The backwash-pump run command (`Bool`, `Out`).
        pub const PUMP_CMD: u64 = 24;
        /// The drain-down request the simulated plant observes (`Bool`,
        /// `Out`).
        pub const DRAIN_CMD: u64 = 25;
    }

    /// The backwash supply-source availability contact (`Bool`, `In`).
    pub const SUPPLY_AVAILABLE: PointId = PointId(10);
    /// The waste-path capacity-availability contact (`Bool`, `In`).
    pub const WASTE_AVAILABLE: PointId = PointId(11);
    /// The online-flow-disturbance measurement (`Float`, `In`) — the one
    /// signal both the `flow_ok` grant permissive and the disturbance alarm
    /// read.
    pub const FLOW_DISTURBANCE: PointId = PointId(12);

    /// Filter `index`'s point at analog offset `offset`.
    fn at(index: usize, offset: u64) -> PointId {
        PointId(FILTER_FIELD_BASE + FILTER_FIELD_STRIDE * index as u64 + offset)
    }

    /// Filter `index`'s headloss across the bed.
    pub fn headloss(index: usize) -> PointId {
        at(index, analog::HEADLOSS)
    }
    /// Filter `index`'s effluent turbidity.
    pub fn turbidity(index: usize) -> PointId {
        at(index, analog::TURBIDITY)
    }
    /// Filter `index`'s simulated effluent-turbidity forcing.
    pub fn turbidity_forcing(index: usize) -> PointId {
        at(index, analog::TURBIDITY_FORCING)
    }
    /// Filter `index`'s water level above the bed.
    pub fn level(index: usize) -> PointId {
        at(index, analog::LEVEL)
    }
    /// Filter `index`'s simulated fill rate above the bed.
    pub fn inflow(index: usize) -> PointId {
        at(index, analog::INFLOW)
    }
    /// Filter `index`'s simulated drain rate.
    pub fn drain_rate(index: usize) -> PointId {
        at(index, analog::DRAIN_RATE)
    }
    /// Filter `index`'s summed net rate above the bed.
    pub fn net_draw(index: usize) -> PointId {
        at(index, analog::NET_DRAW)
    }
    /// Filter `index`'s measured drain depth.
    pub fn drain_depth(index: usize) -> PointId {
        at(index, analog::DRAIN_DEPTH)
    }
    /// Filter `index`'s accumulated filtered volume.
    pub fn filtered_volume(index: usize) -> PointId {
        at(index, analog::FILTERED_VOLUME)
    }
    /// Filter `index`'s accumulated headloss rise.
    pub fn headloss_rise(index: usize) -> PointId {
        at(index, analog::HEADLOSS_RISE)
    }
    /// Filter `index`'s wash draw-down contribution to headloss.
    pub fn wash_draw(index: usize) -> PointId {
        at(index, analog::WASH_DRAW)
    }
    /// Filter `index`'s inlet-valve open contact.
    pub fn inlet_fb(index: usize) -> PointId {
        at(index, contact::INLET_FB)
    }
    /// Filter `index`'s outlet-valve open contact.
    pub fn outlet_fb(index: usize) -> PointId {
        at(index, contact::OUTLET_FB)
    }
    /// Filter `index`'s waste-valve open contact.
    pub fn waste_fb(index: usize) -> PointId {
        at(index, contact::WASTE_FB)
    }
    /// Filter `index`'s air-valve open contact.
    pub fn air_fb(index: usize) -> PointId {
        at(index, contact::AIR_FB)
    }
    /// Filter `index`'s air-scour blower run contact.
    pub fn blower_run(index: usize) -> PointId {
        at(index, contact::BLOWER_RUN)
    }
    /// Filter `index`'s backwash-pump run contact.
    pub fn pump_run(index: usize) -> PointId {
        at(index, contact::PUMP_RUN)
    }
    /// Filter `index`'s equipment-fault contact.
    pub fn fault_contact(index: usize) -> PointId {
        at(index, contact::FAULT)
    }
    /// Filter `index`'s plant-side drained-bed contact.
    pub fn drained_contact(index: usize) -> PointId {
        at(index, contact::DRAINED)
    }
    /// Filter `index`'s inlet-valve command.
    pub fn inlet_cmd(index: usize) -> PointId {
        at(index, contact::INLET_CMD)
    }
    /// Filter `index`'s outlet-valve command.
    pub fn outlet_cmd(index: usize) -> PointId {
        at(index, contact::OUTLET_CMD)
    }
    /// Filter `index`'s waste-valve command.
    pub fn waste_cmd(index: usize) -> PointId {
        at(index, contact::WASTE_CMD)
    }
    /// Filter `index`'s air-valve command.
    pub fn air_cmd(index: usize) -> PointId {
        at(index, contact::AIR_CMD)
    }
    /// Filter `index`'s air-scour blower run command.
    pub fn blower_cmd(index: usize) -> PointId {
        at(index, contact::BLOWER_CMD)
    }
    /// Filter `index`'s backwash-pump run command.
    pub fn pump_cmd(index: usize) -> PointId {
        at(index, contact::PUMP_CMD)
    }
    /// Filter `index`'s drain-down request the simulated plant observes.
    pub fn drain_cmd(index: usize) -> PointId {
        at(index, contact::DRAIN_CMD)
    }
    /// Filter `index`'s wash-water valve position command.
    pub fn wash_valve_cmd(index: usize) -> PointId {
        at(index, analog::WASH_VALVE_CMD)
    }
    /// Filter `index`'s wash-water valve position feedback.
    pub fn wash_valve_pos(index: usize) -> PointId {
        at(index, analog::WASH_VALVE_POS)
    }
}

/// The first per-filter internal block; each filter owns
/// `FILTER_BASE + FILTER_STRIDE·i ..= +FILTER_STRIDE-1`.
const FILTER_BASE: u64 = 1000;
/// The per-filter block stride.
const FILTER_STRIDE: u64 = 220;
/// Alarm points: alarm `a` owns `ALARM_BASE + a·10 ..= +7` with the
/// managed layout the reference compositions share.
const ALARM_BASE: u64 = 10_000;
/// The first per-filter alarm index, after the two bank alarms.
const FILTER_ALARM_BASE: u64 = 2;
/// Every point's signal id is `SIGNAL_BASE + point`.
const SIGNAL_BASE: u64 = 100_000;

/// `bool-gate`'s `operation` codes — `GateOperation::And`/`Or` from
/// `dcs-blocks`, mirrored as data because `dcs-build` cannot depend on
/// the blocks crate.
const GATE_AND: i64 = 0;
const GATE_OR: i64 = 1;

/// The bound a single-sided analog alarm parks its unused limit at —
/// far outside any measurable span, so only the declared threshold side
/// can trip.
const PARKED_LIMIT: f64 = 1.0e9;

/// The declared step-table phases, in table order. A filter's
/// `backwash-sequence` numbers them `1..=6`, so these are the 0-based
/// indices the composition addresses.
pub mod step {
    /// Drain-down — measured advance on the measured drain depth.
    pub const DRAIN: usize = 0;
    /// Air scour — timed.
    pub const AIR_SCOUR: usize = 1;
    /// High-rate wash — timed, never together with air scour.
    pub const HIGH_RATE_WASH: usize = 2;
    /// Filter-to-waste — the measured turbidity bound or the tick bound,
    /// whichever comes first.
    pub const FILTER_TO_WASTE: usize = 3;
    /// Reference flow — the clean-bed-headloss baseline capture.
    pub const REFERENCE_FLOW: usize = 4;
    /// Post-wash verification — the CBHL and ripening window.
    pub const VERIFY: usize = 5;
}

/// One filter's declared equipment pattern — which steps drive each
/// actuator. A reference filter declares the same pattern on every unit,
/// and the table is load-bearing public data: the helper builds the
/// `bool-gate` arities from it, so an embedding surface renders the same
/// pattern the composition wires.
pub struct EquipmentPattern {
    /// Steps whose union drives the inlet valve open.
    pub inlet: &'static [usize],
    /// Steps whose union drives the outlet valve open. The reference
    /// table is empty: the outlet stands open for every wash step, which
    /// the helper composes from the sequence's `active` flag instead of
    /// from per-step copies.
    pub outlet: &'static [usize],
    /// Steps whose union drives the waste valve open.
    pub waste: &'static [usize],
    /// The steps that drive the air valve and the air-scour blower.
    pub air: &'static [usize],
    /// The steps that drive the backwash pump.
    pub wash: &'static [usize],
    /// The steps whose wash-water demand the wash-water valve carries.
    pub water: &'static [usize],
}

/// The reference bank's declared equipment pattern: the canonical
/// granular-media backwash decision 57 describes.
pub const EQUIPMENT_PATTERN: EquipmentPattern = EquipmentPattern {
    inlet: &[
        step::DRAIN,
        step::AIR_SCOUR,
        step::REFERENCE_FLOW,
        step::VERIFY,
    ],
    outlet: &[],
    waste: &[
        step::DRAIN,
        step::AIR_SCOUR,
        step::HIGH_RATE_WASH,
        step::FILTER_TO_WASTE,
    ],
    air: &[step::AIR_SCOUR],
    wash: &[step::HIGH_RATE_WASH],
    water: &[step::HIGH_RATE_WASH, step::FILTER_TO_WASTE],
};

/// The bank's tunable contract — the declared arbitration codes, the step
/// table, the trigger and verification bounds, and the alarm policy.
/// [`reference`](Self::reference) is the checked-in document's
/// configuration.
///
/// Every field is declared data rather than a default: the arbitration
/// codes (`queue_policy`, `queued_state`, `auto_start`) are the
/// owner-convergent-but-universal choices the decisions mark
/// assumption-marked, the per-step table is the owner's step vocabulary,
/// and the bounds are the alarm set's declared limits.
#[derive(Debug, Clone, PartialEq)]
pub struct FilterBankConfig {
    /// The filter count `N`.
    pub filters: usize,
    /// `backwash-coordinator`'s `queue_policy` code — `0` FIFO,
    /// `1` priority by filter index, `2` operator-managed.
    pub queue_policy: i64,
    /// `backwash-coordinator`'s `queued_state` code — `0` keep filtering
    /// until granted, `1` offline once queued.
    pub queued_state: i64,
    /// Whether the coordinator declares and wires its `reorder` input.
    pub reorder: bool,
    /// `backwash-sequence`'s `auto_start` code — `0` an automatic trigger
    /// asserts the request directly, `1` it arms `pending` for the
    /// operator.
    pub auto_start: i64,
    /// The step the operator abort drives the sequence to, 1-based.
    pub abort_step: i64,
    /// The step a proven equipment fault drives the sequence to, 1-based.
    pub on_fault_step: i64,
    /// `backwash-sequence`'s `on_fault_policy` code — `0` hold at the
    /// fault step, `1` abort.
    pub on_fault_policy: i64,
    /// Per step, 1-based: how many scans the step holds — its duration
    /// under the timed modes, its overrun timeout under the measured
    /// mode.
    pub step_ticks: [i64; STEPS],
    /// Per step, 1-based: the wash-water valve position in percent the
    /// step's `out` carries while it reports.
    pub step_out: [f64; STEPS],
    /// Per step, 1-based: `step_<n>_advance` — `0` timed, `1` measured,
    /// `2` whichever comes first.
    pub step_advance: [i64; STEPS],
    /// Per step, 1-based: `step_<n>_on_overrun` — `0` advance anyway,
    /// `1` hold and raise `overrun`.
    pub step_on_overrun: [i64; STEPS],
    /// Per step, 1-based: the measured bound for the measured modes.
    /// Declared on the timed steps too, so the whole table is uniform
    /// data rather than a partly-optional structure.
    pub step_bound: [f64; STEPS],
    /// Per step, 1-based: the `meas_<n>` input the measured modes
    /// select. `meas_1` is the measured drain depth and `meas_2` the
    /// effluent turbidity; the timed steps name `meas_1` so the table is
    /// uniform.
    pub step_meas: [i64; STEPS],
    /// The run-time trigger's `delay_ticks` — the filter run length after
    /// which a backwash request arms.
    pub run_time_ticks: i64,
    /// The terminal-headloss bound in metres — both the trigger's
    /// `alarm-monitor` and the headloss latching alarm read it.
    pub headloss_bound_m: f64,
    /// The turbidity-trigger bound in NTU — the threshold whose crossing
    /// arms a request.
    pub turbidity_trigger_ntu: f64,
    /// The 0.3 NTU per-filter effluent-turbidity alarm tier.
    pub turbidity_alarm_ntu: f64,
    /// The 1 NTU shutdown tier, whose flag additionally feeds the
    /// sequence's fault aggregate — decision 61's declared
    /// alarm-or-trip scope.
    pub turbidity_trip_ntu: f64,
    /// The clean-bed-headloss deviation bound in metres — the CBHL
    /// `phase-monitor`'s `bound` in its deviation mode.
    pub cbhl_bound_m: f64,
    /// The ripening turbidity bound in NTU — the ripening `phase-monitor`'s
    /// `bound` in its absolute mode.
    pub ripening_bound_ntu: f64,
    /// The post-wash verification deadline in scans — both
    /// `phase-monitor`s' `limit_ticks`.
    pub verify_limit_ticks: i64,
    /// The online-flow-disturbance bound in `m3/h`.
    pub flow_disturbance_bound: f64,
    /// The drain-down filtration-rate hold's `max_delta` — the
    /// rate-limiter's per-scan slew bound on the wash-water demand, in
    /// percent open.
    pub rate_limit: f64,
    /// The actuators' feedback-discrepancy budget in scans — each
    /// `motor`'s `fault_ticks` and the wash-water `valve`'s
    /// `discrepancy_ticks`.
    pub actuator_fault_ticks: i64,
    /// The wash-water `valve`'s position tolerance in percent. It must
    /// exceed [`rate_limit`](Self::rate_limit): the simulated plant's
    /// position feedback follows the command one scan later, so a tolerance
    /// below the declared slew would prove a feedback discrepancy on every
    /// commanded ramp rather than on a real disagreement.
    pub valve_tolerance_pct: f64,
    /// The turbidity alarms' hysteresis band in NTU.
    pub turbidity_hysteresis_ntu: f64,
    /// The headloss alarms' hysteresis band in metres.
    pub headloss_hysteresis_m: f64,
    /// The shelvable nuisance alarm's `max_shelve_ticks`. Which bank
    /// alarms are shelvable and their bounds are the site's declared
    /// WW-ALM-002 policy — open customer assumptions carried as data.
    pub turbidity_alarm_max_shelve_ticks: i64,
}

impl FilterBankConfig {
    /// The reference bank the checked-in documents record: a three-unit
    /// bank arbitrated FIFO with queued filters kept filtering, the
    /// canonical six-step table, automatic triggers permitted to start a
    /// wash directly, a held fault step, and the declared tier and
    /// verification bounds.
    pub fn reference() -> Self {
        Self {
            filters: REFERENCE_FILTERS,
            queue_policy: 0,
            queued_state: 0,
            reorder: true,
            auto_start: 0,
            abort_step: 6,
            on_fault_step: 6,
            on_fault_policy: 0,
            step_ticks: [6, 4, 5, 6, 5, 12],
            step_out: [0.0, 0.0, 100.0, 50.0, 0.0, 0.0],
            step_advance: [1, 0, 0, 2, 0, 0],
            step_on_overrun: [1, 0, 0, 0, 0, 0],
            step_bound: [1.0, 0.0, 0.0, 0.5, 0.0, 0.0],
            step_meas: [1, 1, 1, 2, 1, 1],
            run_time_ticks: 90,
            headloss_bound_m: 1.5,
            turbidity_trigger_ntu: 0.2,
            turbidity_alarm_ntu: 0.3,
            turbidity_trip_ntu: 1.0,
            cbhl_bound_m: 0.25,
            ripening_bound_ntu: 0.4,
            verify_limit_ticks: 8,
            flow_disturbance_bound: 0.4,
            rate_limit: 40.0,
            actuator_fault_ticks: 2,
            valve_tolerance_pct: 45.0,
            turbidity_hysteresis_ntu: 0.05,
            headloss_hysteresis_m: 0.05,
            turbidity_alarm_max_shelve_ticks: 8,
        }
    }
}

/// Filter `index`'s place in the emitted document.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FilterLayout {
    /// The 1-based filter index — the `request_i`/`grant_i`/`position_i`
    /// port-family member.
    pub index: usize,
    /// The headloss field measurement.
    pub headloss: PointId,
    /// The effluent-turbidity field measurement.
    pub turbidity: PointId,
    /// The water-level-above-the-bed field measurement.
    pub level: PointId,
    /// The measured drain depth — the drain step's measured input.
    pub drain_depth: PointId,
    /// The equipment-fault field contact.
    pub fault_contact: PointId,
    /// The writable operator start point — `trig_operator`'s surface.
    pub operator_start: PointId,
    /// The writable operator abort point.
    pub abort: PointId,
    /// The writable hold-in-service point.
    pub in_service: PointId,
    /// The writable maintenance-inhibit point.
    pub out_of_service: PointId,
    /// The in-service guard's `tripped` flag — the filter is held out of its
    /// filtering service either by its own mode and maintenance state or by the
    /// declared meanwhile-state condition.
    pub service_blocked: PointId,
    /// The filter's actual filtering condition — in service and not
    /// washing.
    pub filtering: PointId,
    /// The sequence's armed request.
    pub request: PointId,
    /// The coordinator's exclusive grant for this filter.
    pub grant: PointId,
    /// The sequence's stepping-under-grant report.
    pub active: PointId,
    /// The armed-but-held report under `auto_start = 1`.
    pub pending: PointId,
    /// The table-ran-to-its-end report.
    pub done: PointId,
    /// An abort path fired.
    pub aborted: PointId,
    /// A measured step standing past its tick bound.
    pub overrun: PointId,
    /// The held trigger attribution — `0` none, `1` time, `2` headloss,
    /// `3` turbidity, `4` operator.
    pub trigger_source: PointId,
    /// The reported 1-based step.
    pub step: PointId,
    /// The reported step's declared wash-water valve position.
    pub seq_out: PointId,
    /// The 1-based queue position, `0` while not queued.
    pub position: PointId,
    /// The rate-limited wash-water demand.
    pub rate_limited: PointId,
    /// The guarded wash-water demand the valve carries.
    pub guarded_rate: PointId,
    /// The air-scour exclusion's inverted permissive.
    pub exclusion_ok: PointId,
    /// The wash-water permissive the exclusion guard rides on.
    pub water_permissive: PointId,
    /// The exclusion guard's proven trip flag.
    pub exclusion_tripped: PointId,
    /// The per-filter equipment-fault aggregate.
    pub fault: PointId,
    /// The CBHL monitor's reported deviation.
    pub cbhl_deviation: PointId,
    /// The CBHL monitor's excursion flag.
    pub cbhl_exceeded: PointId,
    /// The CBHL monitor's deadline flag.
    pub cbhl_overdue: PointId,
    /// The ripening monitor's excursion flag.
    pub ripening_exceeded: PointId,
    /// The ripening monitor's deadline flag.
    pub ripening_overdue: PointId,
    /// The ripening monitor's combined excursion.
    pub ripening_fault: PointId,
    /// The elapsed-run-time trigger.
    pub trig_time: PointId,
    /// The terminal-headloss trigger.
    pub trig_headloss: PointId,
    /// The effluent-turbidity trigger.
    pub trig_turbidity: PointId,
    /// The 1 NTU shutdown tier's standing flag as the fault aggregate's
    /// first declared input.
    pub turbidity_trip: PointId,
    /// Step `n`'s phase-flag carrier — the kind's raw step report.
    pub phase: [PointId; STEPS],
    /// Step `n`'s step-active carrier — the phase flag under the grant,
    /// the form the declared equipment pattern reads.
    pub step_active: [PointId; STEPS],
    /// The driven commands.
    /// The inlet-valve command.
    pub inlet_cmd: PointId,
    /// The outlet-valve command.
    pub outlet_cmd: PointId,
    /// The waste-valve command.
    pub waste_cmd: PointId,
    /// The air-valve command.
    pub air_cmd: PointId,
    /// The air-scour blower run command.
    pub blower_cmd: PointId,
    /// The backwash-pump run command.
    pub pump_cmd: PointId,
    /// The wash-water valve position command, in percent open.
    pub wash_valve_cmd: PointId,
    /// The `backwash-sequence` instance's id.
    pub sequence: ComponentId,
    /// The clean-bed-headloss `phase-monitor` instance's id.
    pub cbhl: ComponentId,
    /// The ripening `phase-monitor` instance's id.
    pub ripening: ComponentId,
    /// The 0.3 NTU effluent-turbidity managed alarm.
    pub turbidity_alarm: AlarmLayout,
    /// The 1 NTU shutdown-tier managed alarm — its `alarm` output also
    /// feeds the fault aggregate.
    pub turbidity_trip_alarm: AlarmLayout,
    /// The terminal-headloss managed alarm.
    pub headloss_alarm: AlarmLayout,
    /// The aborted-backwash managed alarm.
    pub aborted_alarm: AlarmLayout,
    /// The equipment-fault managed alarm.
    pub fault_alarm: AlarmLayout,
    /// The clean-bed-headloss excursion managed alarm.
    pub cbhl_alarm: AlarmLayout,
    /// The ripening excursion managed alarm.
    pub ripening_alarm: AlarmLayout,
}

/// Where everything the composition declares landed — the ids the
/// dynamics document, the scripted run, and any embedding surface
/// address.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FilterBankLayout {
    /// The backwash supply-source availability contact.
    pub supply_available: PointId,
    /// The waste-path capacity-availability contact.
    pub waste_available: PointId,
    /// The online-flow-disturbance measurement.
    pub flow_disturbance: PointId,
    /// The `flow_ok` grant permissive's carrier — the disturbance bound
    /// holding.
    pub flow_ok: PointId,
    /// The coordinator's grant-holder index.
    pub active: PointId,
    /// The coordinator's pending-request count.
    pub queued: PointId,
    /// The coordinator's resource-blocked flag.
    pub resource_blocked: PointId,
    /// The operator's standing queue-reorder point — writable, so writes
    /// ride the receipted path.
    pub reorder: PointId,
    /// The `backwash-coordinator` instance's id.
    pub coordinator: ComponentId,
    /// Per-filter layouts, in `index` order.
    pub filters: Vec<FilterLayout>,
    /// The resource-blocked-queue managed alarm.
    pub resource_blocked_alarm: AlarmLayout,
    /// The online-flow-disturbance managed alarm.
    pub flow_disturbance_alarm: AlarmLayout,
}

/// The composed bank: the emitted document plus the layout every declared
/// id landed on.
#[derive(Debug, Clone)]
pub struct FilterBank {
    /// The versioned plant-model document.
    pub model: PlantModel,
    /// The id map into it.
    pub layout: FilterBankLayout,
}

/// Composes the bank under `config` and emits its [`PlantModel`].
///
/// Every component registers through its typed spec and every connection
/// goes through typed handles — port existence, direction, and value kind
/// are compile-time-checked where the types reach and `build`-checked
/// where they do not.
///
/// # Panics
///
/// `config.filters` outside `1..=12` exceeds the declared point-id
/// scheme.
pub fn filter_bank(config: &FilterBankConfig) -> Result<FilterBank, BuildError> {
    assert!(
        (1..=12).contains(&config.filters),
        "the bank's point-id scheme admits 1..=12 filters, got {}",
        config.filters
    );
    let mut plant = PlantBuilder::new();

    // The simulated I/O devices — all under the `sim` prefix, so the
    // standard driver registry serves them from the local SimDriver.
    let ai = plant.device("sim-ai").id;
    let di = plant.device("sim-di").id;
    let d_o = plant.device("sim-do").id;
    let ao = plant.device("sim-ao").id;

    let supply_channel = plant.channel::<bool>(di, "supply-available", Direction::In);
    let waste_channel = plant.channel::<bool>(di, "waste-available", Direction::In);
    let flow_channel = plant.channel::<f64>(ai, "flow-disturbance", Direction::In);
    let supply_available =
        plant.field_input::<bool>(points::SUPPLY_AVAILABLE, supply_channel, false);
    let waste_available = plant.field_input::<bool>(points::WASTE_AVAILABLE, waste_channel, false);
    let flow_disturbance = plant.field_input::<f64>(points::FLOW_DISTURBANCE, flow_channel, false);
    // Decision 77's protection-layer report: the two availability contacts
    // are the shared supply's declared status, so their transitions land in
    // the durable record beside the alarms they gate (decision 74).
    plant.journaled(supply_available);
    plant.journaled(waste_available);
    // Decision 102's declared recording duty: the bank's flow series
    // records every scan.
    plant.record(flow_disturbance, 1);

    signal(
        &mut plant,
        points::SUPPLY_AVAILABLE,
        "supply-available",
        "",
        "Backwash supply-source availability contact",
        "backwash-supply",
    );
    signal(
        &mut plant,
        points::WASTE_AVAILABLE,
        "waste-available",
        "",
        "Waste-path capacity availability contact",
        "backwash-supply",
    );
    signal(
        &mut plant,
        points::FLOW_DISTURBANCE,
        "flow-disturbance",
        unit::M3_PER_H,
        "Online-flow disturbance the wash must not exceed",
        "backwash-supply",
    );

    // The bank carriers. The `supply_ok`/`waste_ok` permissives need no
    // carrier pair: a field `In` point feeds many port inputs directly.
    let flow_exceeded = plant.internal_output::<bool>(PointId(901), false);
    let flow_exceeded_in = plant.internal_input::<bool>(PointId(902), false, false);
    let flow_ok = plant.internal_output::<bool>(PointId(903), true);
    let flow_ok_in = plant.internal_input::<bool>(PointId(904), false, false);
    let bank_active = plant.internal_output::<i64>(PointId(905), 0);
    let bank_queued = plant.internal_output::<i64>(PointId(906), 0);
    let bank_resource_blocked = plant.internal_output::<bool>(PointId(907), false);
    let resource_blocked_in = plant.internal_input::<bool>(PointId(908), false, false);
    let reorder = plant.internal_input::<i64>(PointId(909), 0, true);
    for point in [flow_exceeded, flow_ok, bank_resource_blocked] {
        plant.journaled(point);
    }
    for point in [bank_active, bank_queued] {
        plant.journaled(point);
    }
    plant.journaled(reorder);
    for (point, name, described_unit, description) in [
        (
            PointId(901),
            "flow-disturbance-exceeded",
            "",
            "The online-flow-disturbance bound is crossed",
        ),
        (
            PointId(902),
            "flow-disturbance-exceeded-in",
            "",
            "Disturbance excursion delivered to the permissive inversion",
        ),
        (
            PointId(903),
            "flow-ok",
            "",
            "The online-flow disturbance is within its bound",
        ),
        (
            PointId(904),
            "flow-ok-in",
            "",
            "Flow permissive delivered to the coordinator",
        ),
        (
            PointId(905),
            "active",
            "",
            "1-based index of the filter holding the grant",
        ),
        (
            PointId(906),
            "queued",
            "",
            "How many backwash requests stand pending",
        ),
        (
            PointId(907),
            "resource-blocked",
            "",
            "A request stands first in queue while a grant permissive fails",
        ),
        (
            PointId(908),
            "resource-blocked-in",
            "",
            "Resource-blocked flag delivered to its alarm",
        ),
        (
            PointId(909),
            "reorder",
            "",
            "Operator's standing queue-reorder instruction — 0 leaves the queue policy's order",
        ),
    ] {
        signal(
            &mut plant,
            point,
            name,
            described_unit,
            description,
            "backwash-supply",
        );
    }

    // The disturbance monitor is the shared measurement behind both the
    // `flow_ok` permissive and the disturbance alarm — decision 61's "the
    // same signal the `flow_ok` grant permissive consumes".
    let flow_monitor = plant.add(AlarmMonitorSpec::new(parameters([
        ("low_limit", Value::Float(-PARKED_LIMIT)),
        ("high_limit", Value::Float(config.flow_disturbance_bound)),
        (
            "hysteresis",
            Value::Float(config.flow_disturbance_bound / 10.0),
        ),
    ])));
    plant.connect(flow_disturbance, &flow_monitor.input);
    plant.connect(&flow_monitor.alarm, flow_exceeded);
    plant.connect(flow_exceeded_in, flow_exceeded);
    let inv_flow = plant.add(DigitalInputSpec::new(parameters([(
        "invert",
        Value::Bool(true),
    )])));
    plant.connect(flow_exceeded_in, &inv_flow.input);
    plant.connect(&inv_flow.out, flow_ok);
    // The coordinator's `flow_ok` permissive rides the inverted carrier, so
    // the permissive's own condition stays a named, served status point.
    plant.port_unit(flow_monitor.id, "in", unit::M3_PER_H);
    for parameter in ["low_limit", "high_limit", "hysteresis"] {
        plant.param_unit(flow_monitor.id, parameter, unit::M3_PER_H);
    }

    let coordinator = plant.add(BackwashCoordinatorSpec::new(
        parameters([
            ("queue_policy", Value::Int(config.queue_policy)),
            ("queued_state", Value::Int(config.queued_state)),
        ]),
        config.filters,
        config.reorder,
    ));
    plant.connect(supply_available, &coordinator.supply_ok);
    plant.connect(waste_available, &coordinator.waste_ok);
    plant.connect(flow_ok_in, &coordinator.flow_ok);
    plant.connect(flow_ok_in, flow_ok);
    if let Some(port) = coordinator.reorder.as_ref() {
        plant.connect(reorder, port);
    }
    plant.connect(&coordinator.active, bank_active);
    plant.connect(&coordinator.queued, bank_queued);
    plant.connect(&coordinator.resource_blocked, bank_resource_blocked);
    plant.connect(resource_blocked_in, bank_resource_blocked);

    let resource_blocked_instance = plant.add(ManagedBoolLatchingAlarmSpec::new(
        parameters([
            ("max_shelve_ticks", Value::Int(0)),
            ("priority", Value::Int(2)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(30)),
        ]),
        ManagedInputs::default(),
        rationalization(
            "A filter needs washing but the shared supply cannot serve it",
            "Clear the washwater or waste capacity restriction and inspect the online-flow disturbance",
            "resource-blocked-alarm",
        ),
    ));
    plant.connect(resource_blocked_in, &resource_blocked_instance.input);
    for parameter in ["max_shelve_ticks", "response_ticks"] {
        plant.param_unit(resource_blocked_instance.id, parameter, unit::TICKS);
    }
    let resource_blocked_alarm = managed_alarm(
        &mut plant,
        0,
        resource_blocked_instance.id,
        &resource_blocked_instance.ack,
        &resource_blocked_instance.managed,
        &resource_blocked_instance.alarm,
        &resource_blocked_instance.unacknowledged,
        "resource-blocked",
        "backwash-supply",
    );

    let flow_alarm_instance = plant.add(ManagedLatchingAlarmSpec::new(
        parameters([
            ("low_limit", Value::Float(-PARKED_LIMIT)),
            ("high_limit", Value::Float(config.flow_disturbance_bound)),
            (
                "hysteresis",
                Value::Float(config.flow_disturbance_bound / 10.0),
            ),
            ("max_shelve_ticks", Value::Int(0)),
            ("priority", Value::Int(2)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(30)),
        ]),
        ManagedInputs::default(),
        rationalization(
            "Backwashing disturbs the online flow past the declared bound",
            "Suspend the wash or restore the treated flow the disturbance comes from",
            "flow-disturbance-alarm",
        ),
    ));
    plant.connect(flow_disturbance, &flow_alarm_instance.input);
    plant.port_unit(flow_alarm_instance.id, "in", unit::M3_PER_H);
    for parameter in ["low_limit", "high_limit", "hysteresis"] {
        plant.param_unit(flow_alarm_instance.id, parameter, unit::M3_PER_H);
    }
    for parameter in ["max_shelve_ticks", "response_ticks"] {
        plant.param_unit(flow_alarm_instance.id, parameter, unit::TICKS);
    }
    let flow_disturbance_alarm = managed_alarm(
        &mut plant,
        1,
        flow_alarm_instance.id,
        &flow_alarm_instance.ack,
        &flow_alarm_instance.managed,
        &flow_alarm_instance.alarm,
        &flow_alarm_instance.unacknowledged,
        "flow-disturbance",
        "backwash-supply",
    );

    let mut filters = Vec::with_capacity(config.filters);
    for index in 0..config.filters {
        filters.push(wire_filter(
            &mut plant,
            config,
            index,
            ai,
            di,
            d_o,
            ao,
            &coordinator,
        ));
    }

    let model = plant.build()?;
    Ok(FilterBank {
        model,
        layout: FilterBankLayout {
            supply_available: points::SUPPLY_AVAILABLE,
            waste_available: points::WASTE_AVAILABLE,
            flow_disturbance: points::FLOW_DISTURBANCE,
            flow_ok: PointId(903),
            active: PointId(905),
            queued: PointId(906),
            resource_blocked: PointId(907),
            reorder: PointId(909),
            coordinator: coordinator.id,
            filters,
            resource_blocked_alarm,
            flow_disturbance_alarm,
        },
    })
}

/// Registers point `point`'s monitoring signal — `SIGNAL_BASE + point` —
/// carrying the full unit/description/group metadata WW-FND-001 asks the
/// surface to render from.
///
/// A non-empty `unit` is declared once, on the point — the wiring contract
/// the connection and validation checks read — and the signal inherits it
/// at `build`, so the string the operator surface renders cannot drift
/// from the unit the value is carried in. The empty `""` stays
/// signal-side: the deliberate "dimensionless" marker for the status,
/// flag, and code points the wiring layer leaves uncheckable.
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
/// `ack`, the declared `shelve` request point where the reference plant
/// declares the nuisance alarm shelvable, and the five status outputs.
/// Every status point is `journaled` under decision 74, so activation,
/// return, the latch's clear, shelving assertion and expiry, suppression,
/// and out-of-service entry and return all land as durable entries; the
/// shelve request point journals too, since its transitions are the
/// lifecycle actions recorded beside their attributed receipts. `ack`
/// stays receipted-only — its writes are already the record.
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
) -> AlarmLayout {
    managed_alarm_carrying(
        plant,
        index,
        component,
        ack_port,
        managed,
        alarm_port,
        unacknowledged_port,
        prefix,
        group,
        None,
    )
}

/// [`managed_alarm`], plus the standing-alarm carrier's handle when the
/// caller needs to read the flag decision 61's declared alarm-or-trip scope
/// feeds onward — its `alarm` port binds the carrier exactly once, so a
/// second reader takes this handle rather than a second binding.
#[allow(clippy::too_many_arguments)]
fn managed_alarm_carrying(
    plant: &mut PlantBuilder,
    index: u64,
    component: ComponentId,
    ack_port: &Sink<bool>,
    managed: &ManagedAlarmHandles,
    alarm_port: &Source<bool>,
    unacknowledged_port: &Source<bool>,
    prefix: &str,
    group: &str,
    alarm_carrier: Option<&mut Option<OutPoint<bool>>>,
) -> AlarmLayout {
    let base = ALARM_BASE + index * 10;
    let ack = plant.internal_input::<bool>(PointId(base), false, true);
    let shelve = managed.shelve.as_ref().map(|_| {
        // The declared shelving policy: a writable `shelve` point rides the
        // receipted, actor-attributed request path.
        plant.internal_input::<bool>(PointId(base + 1), false, true)
    });
    let oos = managed
        .oos
        .as_ref()
        .map(|_| plant.internal_input::<bool>(PointId(base + 2), false, true));
    let alarm = plant.internal_output::<bool>(PointId(base + 3), false);
    let unacknowledged = plant.internal_output::<bool>(PointId(base + 4), false);
    let shelved = plant.internal_output::<bool>(PointId(base + 5), false);
    let suppressed = plant.internal_output::<bool>(PointId(base + 6), false);
    let out_of_service = plant.internal_output::<bool>(PointId(base + 7), false);

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
    if let Some(slot) = alarm_carrier {
        *slot = Some(alarm);
    }
    AlarmLayout {
        component,
        ack: PointId(base),
        alarm: PointId(base + 3),
        unacknowledged: PointId(base + 4),
    }
}

/// One filter's internal carrier block: a bump allocator over the
/// filter's reserved id range that declares each carrier with its
/// monitoring metadata as it goes, so the offsets stay deterministic in
/// declaration order without a hand-maintained table.
struct Block {
    next: u64,
    end: u64,
    tag: String,
    group: String,
}

impl Block {
    /// A block over `FILTER_BASE + FILTER_STRIDE·index`, tagged
    /// `tag` and filed under signal group `group`.
    fn new(index: usize, tag: String, group: String) -> Self {
        Self {
            next: FILTER_BASE + FILTER_STRIDE * index as u64,
            end: FILTER_BASE + FILTER_STRIDE * (index as u64 + 1),
            tag,
            group,
        }
    }

    fn reserve(&mut self, count: u64) -> u64 {
        assert!(
            self.next + count <= self.end,
            "filter {}'s carrier block overflows its declared stride",
            self.tag
        );
        let first = self.next;
        self.next += count;
        first
    }

    /// Declares a `Bool` carrier with a served signal.
    fn out_bool(
        &mut self,
        plant: &mut PlantBuilder,
        name: &str,
        description: &str,
        initial: bool,
    ) -> OutPoint<bool> {
        let point = PointId(self.reserve(1));
        let handle = plant.internal_output::<bool>(point, initial);
        let tag = self.tag.clone();
        let group = self.group.clone();
        let full = format!("{tag}-{name}");
        signal(plant, point, &full, "", description, &group);
        handle
    }

    /// Declares a `Float` carrier with a served signal.
    fn out_float(
        &mut self,
        plant: &mut PlantBuilder,
        name: &str,
        unit: &str,
        description: &str,
        initial: f64,
    ) -> OutPoint<f64> {
        let point = PointId(self.reserve(1));
        let handle = plant.internal_output::<f64>(point, initial);
        let tag = self.tag.clone();
        let group = self.group.clone();
        let full = format!("{tag}-{name}");
        signal(plant, point, &full, unit, description, &group);
        handle
    }

    /// Declares an `Int` carrier with a served signal.
    fn out_int(
        &mut self,
        plant: &mut PlantBuilder,
        name: &str,
        description: &str,
        initial: i64,
    ) -> OutPoint<i64> {
        let point = PointId(self.reserve(1));
        let handle = plant.internal_output::<i64>(point, initial);
        let tag = self.tag.clone();
        let group = self.group.clone();
        let full = format!("{tag}-{name}");
        signal(plant, point, &full, "", description, &group);
        handle
    }

    /// Declares a read-only `Bool` deliverable — one per reader of a
    /// component `Out` port, since a port drives exactly one endpoint.
    fn deliver_bool(
        &mut self,
        plant: &mut PlantBuilder,
        name: &str,
        description: &str,
    ) -> InPoint<bool> {
        self.deliver_n(plant, name, description, 1)
            .pop()
            .expect("one deliverable was declared")
    }

    /// Declares `count` read-only `Bool` deliverables named
    /// `{name}-1`…`{name}-{count}`.
    fn deliver_n(
        &mut self,
        plant: &mut PlantBuilder,
        name: &str,
        description: &str,
        count: u64,
    ) -> Vec<InPoint<bool>> {
        let first = self.reserve(count);
        let tag = self.tag.clone();
        let group = self.group.clone();
        (0..count)
            .map(|offset| {
                let point = PointId(first + offset);
                let handle = plant.internal_input::<bool>(point, false, false);
                signal(
                    plant,
                    point,
                    &format!("{tag}-{name}-{}", offset + 1),
                    "",
                    description,
                    &group,
                );
                handle
            })
            .collect()
    }

    /// Declares a read-only `Float` deliverable.
    fn deliver_float(
        &mut self,
        plant: &mut PlantBuilder,
        name: &str,
        unit: &str,
        description: &str,
    ) -> InPoint<f64> {
        let point = PointId(self.reserve(1));
        let handle = plant.internal_input::<f64>(point, 0.0, false);
        let tag = self.tag.clone();
        let group = self.group.clone();
        let full = format!("{tag}-{name}");
        signal(plant, point, &full, unit, description, &group);
        handle
    }

    /// Declares a writable `Bool` operator point, journaled under decision
    /// 74 so the operator action lands beside its attributed receipt.
    fn writable_bool(
        &mut self,
        plant: &mut PlantBuilder,
        name: &str,
        description: &str,
        initial: bool,
    ) -> InPoint<bool> {
        let point = PointId(self.reserve(1));
        let handle = plant.internal_input::<bool>(point, initial, true);
        let tag = self.tag.clone();
        let group = self.group.clone();
        let full = format!("{tag}-{name}");
        signal(plant, point, &full, "", description, &group);
        plant.journaled(point);
        handle
    }

    /// Declares a read-only `Float` held feed — the constant `Good` anchor
    /// a discrete `interlock` binds on `in`.
    fn anchor(&mut self, plant: &mut PlantBuilder, name: &str, description: &str) -> InPoint<f64> {
        let point = PointId(self.reserve(1));
        let handle = plant.internal_input::<f64>(point, 0.0, false);
        let tag = self.tag.clone();
        let group = self.group.clone();
        let full = format!("{tag}-{name}");
        signal(plant, point, &full, "", description, &group);
        handle
    }
}

/// Wires filter `index` (`0`-based): the field points and their metadata,
/// the in-service aggregation and the declared meanwhile-state guard, the
/// trigger primaries, the coordinator handshake, the six-phase equipment
/// pattern with the drain-down rate hold and the air-scour exclusion, the
/// fault aggregate, the post-wash verification pair, and the seven
/// declared alarms.
#[allow(clippy::too_many_lines)]
#[allow(clippy::too_many_arguments)]
fn wire_filter(
    plant: &mut PlantBuilder,
    config: &FilterBankConfig,
    index: usize,
    ai: DeviceId,
    di: DeviceId,
    d_o: DeviceId,
    ao: DeviceId,
    coordinator: &BackwashCoordinatorInstance,
) -> FilterLayout {
    let tag = format!("f1{:02}", index + 1);
    let group = format!("filter-{tag}");
    let mut b = Block::new(index, tag.clone(), group.clone());

    // ----------------------------------------------------------------
    // Field points.
    // ----------------------------------------------------------------
    let channel = |plant: &mut PlantBuilder, device: DeviceId, kind: &str, name: String| match kind
    {
        "f" => plant.channel::<f64>(device, &name, Direction::In),
        "b" => plant.channel::<bool>(device, &name, Direction::In),
        _ => plant.channel::<bool>(device, &name, Direction::Out),
    };
    macro_rules! ai_ch {
        ($suffix:literal) => {
            channel(plant, ai, "f", format!("{tag}-{}", $suffix))
        };
    }
    macro_rules! di_ch {
        ($suffix:literal) => {
            channel(plant, di, "b", format!("{tag}-{}", $suffix))
        };
    }
    macro_rules! do_ch {
        ($suffix:literal) => {
            channel(plant, d_o, "o", format!("{tag}-{}", $suffix))
        };
    }
    // The device channels — declared once, then bound to their field points.
    let headloss_channel = ai_ch!("headloss");
    let turbidity_channel = ai_ch!("turbidity");
    let turbidity_forcing_channel = ai_ch!("turbidity-forcing");
    let level_channel = ai_ch!("level");
    let inflow_channel = ai_ch!("inflow");
    let drain_rate_channel = ai_ch!("drain-rate");
    let net_draw_channel = ai_ch!("net-draw");
    let drain_depth_channel = ai_ch!("drain-depth");
    let filtered_volume_channel = ai_ch!("filtered-volume");
    let headloss_rise_channel = ai_ch!("headloss-rise");
    let wash_draw_channel = ai_ch!("wash-draw");
    let wash_valve_pos_channel = ai_ch!("wash-valve-pos");
    let inlet_fb_channel = di_ch!("inlet-fb");
    let outlet_fb_channel = di_ch!("outlet-fb");
    let waste_fb_channel = di_ch!("waste-fb");
    let air_fb_channel = di_ch!("air-fb");
    let blower_run_channel = di_ch!("blower-run");
    let pump_run_channel = di_ch!("pump-run");
    let fault_channel = di_ch!("fault");
    let drained_contact_channel = di_ch!("drained-contact");
    let inlet_cmd_channel = do_ch!("inlet-cmd");
    let outlet_cmd_channel = do_ch!("outlet-cmd");
    let waste_cmd_channel = do_ch!("waste-cmd");
    let air_cmd_channel = do_ch!("air-cmd");
    let blower_cmd_channel = do_ch!("blower-cmd");
    let pump_cmd_channel = do_ch!("pump-cmd");
    let drain_cmd_channel = do_ch!("drain-cmd");
    let wash_valve_cmd_channel =
        plant.channel::<f64>(ao, &format!("{tag}-wash-valve-cmd"), Direction::Out);

    let headloss = plant.field_input::<f64>(points::headloss(index), headloss_channel, false);
    let turbidity = plant.field_input::<f64>(points::turbidity(index), turbidity_channel, false);
    plant.field_input::<f64>(
        points::turbidity_forcing(index),
        turbidity_forcing_channel,
        false,
    );
    let level = plant.field_input::<f64>(points::level(index), level_channel, false);
    plant.field_input::<f64>(points::inflow(index), inflow_channel, false);
    plant.field_input::<f64>(points::drain_rate(index), drain_rate_channel, false);
    plant.field_input::<f64>(points::net_draw(index), net_draw_channel, false);
    let drain_depth =
        plant.field_input::<f64>(points::drain_depth(index), drain_depth_channel, false);
    plant.field_input::<f64>(
        points::filtered_volume(index),
        filtered_volume_channel,
        false,
    );
    plant.field_input::<f64>(points::headloss_rise(index), headloss_rise_channel, false);
    plant.field_input::<f64>(points::wash_draw(index), wash_draw_channel, false);
    let inlet_fb = plant.field_input::<bool>(points::inlet_fb(index), inlet_fb_channel, false);
    let outlet_fb = plant.field_input::<bool>(points::outlet_fb(index), outlet_fb_channel, false);
    let waste_fb = plant.field_input::<bool>(points::waste_fb(index), waste_fb_channel, false);
    let air_fb = plant.field_input::<bool>(points::air_fb(index), air_fb_channel, false);
    let blower_run =
        plant.field_input::<bool>(points::blower_run(index), blower_run_channel, false);
    let pump_run = plant.field_input::<bool>(points::pump_run(index), pump_run_channel, false);
    let fault_contact =
        plant.field_input::<bool>(points::fault_contact(index), fault_channel, false);
    plant.field_input::<bool>(
        points::drained_contact(index),
        drained_contact_channel,
        false,
    );
    let inlet_cmd = plant.field_output::<bool>(points::inlet_cmd(index), inlet_cmd_channel);
    let outlet_cmd = plant.field_output::<bool>(points::outlet_cmd(index), outlet_cmd_channel);
    let waste_cmd = plant.field_output::<bool>(points::waste_cmd(index), waste_cmd_channel);
    let air_cmd = plant.field_output::<bool>(points::air_cmd(index), air_cmd_channel);
    let blower_cmd = plant.field_output::<bool>(points::blower_cmd(index), blower_cmd_channel);
    let pump_cmd = plant.field_output::<bool>(points::pump_cmd(index), pump_cmd_channel);
    let drain_cmd = plant.field_output::<bool>(points::drain_cmd(index), drain_cmd_channel);
    let wash_valve_cmd =
        plant.field_output::<f64>(points::wash_valve_cmd(index), wash_valve_cmd_channel);
    let wash_valve_pos =
        plant.field_input::<f64>(points::wash_valve_pos(index), wash_valve_pos_channel, false);

    // Decision 102's declared recording duty: the filter's compliance
    // series records every scan.
    plant.record(headloss, 1);
    plant.record(turbidity, 1);
    plant.record(level, 1);
    // Decision 77's protection-layer report: the equipment-fault contact is
    // the plant-side declared status, so its transitions land in the
    // durable record.
    plant.journaled(fault_contact);

    for (point, name, described_unit, description) in [
        (
            points::headloss(index),
            "headloss",
            unit::M,
            "Headloss across the filter bed",
        ),
        (
            points::turbidity(index),
            "turbidity",
            unit::NTU,
            "Effluent turbidity — the declared tier and ripening measurement",
        ),
        (
            points::turbidity_forcing(index),
            "turbidity-forcing",
            unit::NTU,
            "Simulated effluent-turbidity forcing the dynamics lags",
        ),
        (
            points::level(index),
            "level",
            unit::M,
            "Water level above the filter bed",
        ),
        (
            points::inflow(index),
            "inflow",
            "m/scan",
            "Simulated fill rate above the bed while the filter runs",
        ),
        (
            points::drain_rate(index),
            "drain-rate",
            "m/scan",
            "Simulated drain rate while the bed drains",
        ),
        (
            points::net_draw(index),
            "net-draw",
            "m/scan",
            "Summed fill and drain rate the level integrator advances on",
        ),
        (
            points::drain_depth(index),
            "drain-depth",
            unit::M,
            "Measured drain depth — the drain step's measured input",
        ),
        (
            points::filtered_volume(index),
            "filtered-volume",
            "m3",
            "Accumulated filtered volume the headloss rise rides on",
        ),
        (
            points::headloss_rise(index),
            "headloss-rise",
            unit::M,
            "Accumulated headloss the filtered volume produces",
        ),
        (
            points::wash_draw(index),
            "wash-draw",
            unit::M,
            "Headloss the open wash-water valve removes",
        ),
        (
            points::inlet_fb(index),
            "inlet-fb",
            "",
            "Inlet-valve open contact",
        ),
        (
            points::outlet_fb(index),
            "outlet-fb",
            "",
            "Outlet-valve open contact",
        ),
        (
            points::waste_fb(index),
            "waste-fb",
            "",
            "Waste-valve open contact",
        ),
        (
            points::air_fb(index),
            "air-fb",
            "",
            "Air-valve open contact",
        ),
        (
            points::blower_run(index),
            "blower-run",
            "",
            "Air-scour blower run contact",
        ),
        (
            points::pump_run(index),
            "pump-run",
            "",
            "Backwash-pump run contact",
        ),
        (
            points::fault_contact(index),
            "fault",
            "",
            "Filter equipment-fault contact",
        ),
        (
            points::drained_contact(index),
            "drained-contact",
            "",
            "Plant-side drained-bed contact the drain step's measured input reads",
        ),
        (
            points::inlet_cmd(index),
            "inlet-cmd",
            "",
            "Inlet-valve command",
        ),
        (
            points::outlet_cmd(index),
            "outlet-cmd",
            "",
            "Outlet-valve command",
        ),
        (
            points::waste_cmd(index),
            "waste-cmd",
            "",
            "Waste-valve command",
        ),
        (points::air_cmd(index), "air-cmd", "", "Air-valve command"),
        (
            points::blower_cmd(index),
            "blower-cmd",
            "",
            "Air-scour blower run command",
        ),
        (
            points::pump_cmd(index),
            "pump-cmd",
            "",
            "Backwash-pump run command",
        ),
        (
            points::drain_cmd(index),
            "drain-cmd",
            "",
            "Drain-down request the simulated plant observes",
        ),
        (
            points::wash_valve_cmd(index),
            "wash-valve-cmd",
            unit::PERCENT,
            "Wash-water valve position command",
        ),
        (
            points::wash_valve_pos(index),
            "wash-valve-pos",
            unit::PERCENT,
            "Wash-water valve position feedback",
        ),
    ] {
        signal(
            plant,
            point,
            &format!("{tag}-{name}"),
            described_unit,
            description,
            &group,
        );
    }

    // The actuators' feedback loopbacks: the simulated plant observes the
    // driven commands, and a scripted write to the observing point is what
    // proves a feedback-discrepancy fault.
    plant.connect(inlet_fb, inlet_cmd);
    plant.connect(outlet_fb, outlet_cmd);
    plant.connect(waste_fb, waste_cmd);
    plant.connect(air_fb, air_cmd);
    plant.connect(blower_run, blower_cmd);
    plant.connect(pump_run, pump_cmd);
    plant.connect(wash_valve_pos, wash_valve_cmd);

    // ----------------------------------------------------------------
    // Writable operator points and the in-service aggregation.
    // ----------------------------------------------------------------
    let operator_start = b.writable_bool(
        plant,
        "operator-start",
        "Operator backwash start — arms or releases the request per the declared auto-start permission",
        false,
    );
    let abort = b.writable_bool(
        plant,
        "abort",
        "Operator backwash abort — drives the sequence to the declared abort step",
        false,
    );
    let in_service = b.writable_bool(
        plant,
        "in-service",
        "Operator's hold-in-service command",
        true,
    );
    let oos = b.writable_bool(
        plant,
        "oos",
        "Out of service — a maintenance inhibit on the filter",
        false,
    );

    let inv_oos = plant.add(DigitalInputSpec::new(parameters([(
        "invert",
        Value::Bool(true),
    )])));
    let service = b.out_bool(plant, "service", "The filter is held in service", true);
    let trip_ok_a = b.out_bool(
        plant,
        "in-service-ok",
        "In service — the inverted maintenance inhibit",
        true,
    );
    let service_gate = plant.add(BoolGateSpec::new(
        parameters([("operation", Value::Int(GATE_AND))]),
        2,
    ));
    let service_in = b.deliver_bool(
        plant,
        "service-in",
        "In-service delivered to the guard and the filtering gate",
    );
    let trip_ok_b = b.out_bool(
        plant,
        "filter-out-of-service-ok",
        "Not held out of service — the inverted guard trip",
        true,
    );
    let trip_ok_b_in = b.deliver_bool(
        plant,
        "filter-out-of-service-ok-in",
        "Not-held-out-of-service delivered to the filtering gate",
    );

    let service_anchor = b.anchor(
        plant,
        "service-anchor",
        "Held analog feed for the in-service guard — its conditions are discrete",
    );
    let service_guard = plant.add(InterlockSpec::new(
        parameters([("safe_value", Value::Float(0.0))]),
        1,
    ));
    let service_guard_out = b.out_float(
        plant,
        "service-guard-out",
        "",
        "The in-service guard's analog pass-through — unused",
        0.0,
    );
    let service_trip = b.out_bool(
        plant,
        "service-trip",
        "The filter is held out of service by the declared meanwhile state",
        false,
    );
    let service_trip_in = b.deliver_bool(
        plant,
        "service-trip-in",
        "In-service trip delivered to its inversion",
    );
    plant.journaled(service_trip);
    let inv_trip = plant.add(DigitalInputSpec::new(parameters([(
        "invert",
        Value::Bool(true),
    )])));
    let filtering = b.out_bool(
        plant,
        "filtering",
        "The filter is filtering — in service and not washing",
        true,
    );
    plant.journaled(filtering);
    let filtering_in = b.deliver_bool(
        plant,
        "filtering-in",
        "Filtering delivered to the run timer and the equipment patterns",
    );

    plant.connect(oos, &inv_oos.input);
    plant.connect(&inv_oos.out, trip_ok_a);
    plant.connect(in_service, service_gate.input(1));
    let trip_ok_a_in = b.deliver_bool(
        plant,
        "in-service-ok-in",
        "In-service delivered to the service gate",
    );
    plant.connect(trip_ok_a_in, trip_ok_a);
    plant.connect(trip_ok_a_in, service_gate.input(2));
    plant.connect(&service_gate.out, service);
    plant.connect(service_in, service);
    plant.connect(service_in, &service_guard.permissive);
    plant.connect(service_anchor, &service_guard.input);
    plant.connect(&service_guard.out, service_guard_out);
    plant.connect(&service_guard.tripped, service_trip);
    plant.connect(service_trip_in, service_trip);
    plant.connect(service_trip_in, &inv_trip.input);
    plant.connect(&inv_trip.out, trip_ok_b);
    plant.connect(trip_ok_b_in, trip_ok_b);
    let filtering_gate = plant.add(BoolGateSpec::new(
        parameters([("operation", Value::Int(GATE_AND))]),
        2,
    ));
    plant.connect(service_in, filtering_gate.input(1));
    plant.connect(trip_ok_b_in, filtering_gate.input(2));
    plant.connect(&filtering_gate.out, filtering);
    plant.connect(filtering_in, filtering);

    // ----------------------------------------------------------------
    // Trigger primaries (decision 58).
    // ----------------------------------------------------------------
    let trig_time = b.out_bool(plant, "trig-time", "The elapsed-run-time trigger", false);
    let trig_headloss = b.out_bool(
        plant,
        "trig-headloss",
        "The terminal-headloss trigger",
        false,
    );
    let trig_turbidity = b.out_bool(
        plant,
        "trig-turbidity",
        "The effluent-turbidity trigger",
        false,
    );
    let run_timer = plant.add(TimerSpec::new(parameters([(
        "delay_ticks",
        Value::Int(config.run_time_ticks),
    )])));
    plant.param_unit(run_timer.id, "delay_ticks", unit::TICKS);
    plant.connect(filtering_in, &run_timer.input);
    plant.connect(&run_timer.out, trig_time);
    let headloss_monitor = plant.add(AlarmMonitorSpec::new(parameters([
        ("low_limit", Value::Float(-PARKED_LIMIT)),
        ("high_limit", Value::Float(config.headloss_bound_m)),
        ("hysteresis", Value::Float(config.headloss_hysteresis_m)),
    ])));
    plant.connect(headloss, &headloss_monitor.input);
    plant.connect(&headloss_monitor.alarm, trig_headloss);
    let turbidity_monitor = plant.add(AlarmMonitorSpec::new(parameters([
        ("low_limit", Value::Float(-PARKED_LIMIT)),
        ("high_limit", Value::Float(config.turbidity_trigger_ntu)),
        ("hysteresis", Value::Float(config.turbidity_hysteresis_ntu)),
    ])));
    plant.connect(turbidity, &turbidity_monitor.input);
    plant.connect(&turbidity_monitor.alarm, trig_turbidity);
    plant.port_unit(headloss_monitor.id, "in", unit::M);
    for parameter in ["low_limit", "high_limit", "hysteresis"] {
        plant.param_unit(headloss_monitor.id, parameter, unit::M);
    }
    plant.port_unit(turbidity_monitor.id, "in", unit::NTU);
    for parameter in ["low_limit", "high_limit", "hysteresis"] {
        plant.param_unit(turbidity_monitor.id, parameter, unit::NTU);
    }

    // ----------------------------------------------------------------
    // The step table and the grant handshake (decisions 57, 59, 60).
    // ----------------------------------------------------------------
    let mut step_parameters = parameters([
        ("step_count", Value::Int(STEPS as i64)),
        ("auto_start", Value::Int(config.auto_start)),
        ("abort_step", Value::Int(config.abort_step)),
        ("on_fault_step", Value::Int(config.on_fault_step)),
        ("on_fault_policy", Value::Int(config.on_fault_policy)),
    ]);
    for n in 0..STEPS {
        let one = i64::try_from(n + 1).expect("the step index stays in range");
        step_parameters.insert(
            format!("step_{one}_ticks"),
            Value::Int(config.step_ticks[n]),
        );
        step_parameters.insert(format!("step_{one}_out"), Value::Float(config.step_out[n]));
        step_parameters.insert(
            format!("step_{one}_advance"),
            Value::Int(config.step_advance[n]),
        );
        step_parameters.insert(
            format!("step_{one}_on_overrun"),
            Value::Int(config.step_on_overrun[n]),
        );
        step_parameters.insert(
            format!("step_{one}_bound"),
            Value::Float(config.step_bound[n]),
        );
        step_parameters.insert(format!("step_{one}_meas"), Value::Int(config.step_meas[n]));
    }
    let sequence = plant.add(BackwashSequenceSpec::new(step_parameters, 2, STEPS));
    plant.port_unit(sequence.id, "meas_1", unit::M);
    plant.port_unit(sequence.id, "meas_2", unit::NTU);
    plant.port_unit(sequence.id, "out", unit::PERCENT);
    plant.param_unit(sequence.id, "step_1_bound", unit::M);
    plant.param_unit(sequence.id, "step_4_bound", unit::NTU);

    let request = b.out_bool(plant, "request", "The armed backwash request", false);
    plant.journaled(request);
    let request_coord = b.deliver_bool(
        plant,
        "request-coord",
        "Request delivered to the coordinator",
    );
    let grant = b.out_bool(
        plant,
        "grant",
        "The coordinator's exclusive supply grant",
        false,
    );
    plant.journaled(grant);
    let grant_seq = b.deliver_bool(
        plant,
        "grant-seq",
        "Grant delivered to the sequence's run permissive",
    );
    let active = b.out_bool(
        plant,
        "active",
        "The sequence is stepping under the grant",
        false,
    );
    plant.journaled(active);
    let active_in = b.deliver_bool(
        plant,
        "active-in",
        "Stepping delivered to the equipment patterns",
    );
    let pending = b.out_bool(
        plant,
        "pending",
        "The request is armed and awaiting the operator's start",
        false,
    );
    plant.journaled(pending);
    let done = b.out_bool(plant, "done", "The step table ran to its end", false);
    plant.journaled(done);
    let aborted = b.out_bool(plant, "aborted", "An abort path fired", false);
    plant.journaled(aborted);
    let aborted_in = b.deliver_bool(plant, "aborted-in", "Abort status delivered to its alarm");
    let overrun = b.out_bool(
        plant,
        "overrun",
        "A measured step stands past its tick bound",
        false,
    );
    plant.journaled(overrun);
    let trigger = b.out_int(plant, "trigger-source", "The held trigger attribution", 0);
    plant.journaled(trigger);
    let step_position = b.out_int(plant, "step", "The reported 1-based step", 0);
    plant.journaled(step_position);
    let seq_out = b.out_float(
        plant,
        "seq-out",
        unit::PERCENT,
        "The reported step's declared wash-water valve position",
        0.0,
    );
    let position = b.out_int(
        plant,
        "position",
        "The 1-based queue position; 0 while not queued",
        0,
    );
    plant.journaled(position);
    let rate_in = b.deliver_float(
        plant,
        "step-out",
        unit::PERCENT,
        "The reported step's declared wash-water valve position",
    );

    let trig_time_in = b.deliver_bool(
        plant,
        "trig-time-in",
        "Elapsed-run-time trigger delivered to the sequence",
    );
    let trig_headloss_in = b.deliver_bool(
        plant,
        "trig-headloss-in",
        "Terminal-headloss trigger delivered to the sequence",
    );
    let trig_turbidity_in = b.deliver_bool(
        plant,
        "trig-turbidity-in",
        "Effluent-turbidity trigger delivered to the sequence",
    );

    plant.connect(trig_time_in, trig_time);
    plant.connect(trig_time_in, &sequence.trig_time);
    plant.connect(trig_headloss_in, trig_headloss);
    plant.connect(trig_headloss_in, &sequence.trig_headloss);
    plant.connect(trig_turbidity_in, trig_turbidity);
    plant.connect(trig_turbidity_in, &sequence.trig_turbidity);
    plant.connect(operator_start, &sequence.trig_operator);
    plant.connect(abort, &sequence.abort);
    plant.connect(grant_seq, &sequence.grant);
    plant.connect(drain_depth, sequence.meas(1));
    plant.connect(turbidity, sequence.meas(2));
    plant.connect(&sequence.request, request);
    plant.connect(request_coord, request);
    plant.connect(request_coord, coordinator.request(index + 1));
    plant.connect(coordinator.grant(index + 1), grant);
    plant.connect(grant_seq, grant);
    plant.connect(&sequence.active, active);
    plant.connect(active_in, active);
    plant.connect(&sequence.pending, pending);
    plant.connect(&sequence.done, done);
    plant.connect(&sequence.aborted, aborted);
    plant.connect(aborted_in, aborted);
    plant.connect(&sequence.overrun, overrun);
    plant.connect(&sequence.trigger_source, trigger);
    plant.connect(&sequence.step, step_position);
    plant.connect(&sequence.out, seq_out);
    plant.connect(rate_in, seq_out);
    plant.connect(coordinator.position(index + 1), position);

    // Decision 57's meanwhile-state wiring: `queued_state = 0` gates the
    // filter's in-service path on `active` — keep filtering until the wash
    // starts — and `queued_state = 1` on `request`, going offline once
    // queued. The trip lands on the in-service interlock, so the declared
    // choice is one wiring difference.
    b.out_bool(
        plant,
        "meanwhile",
        "The declared meanwhile-state condition holding the filter out of service",
        false,
    );
    let meanwhile_in = b.deliver_bool(
        plant,
        "meanwhile-in",
        "Meanwhile-state condition delivered to the in-service guard",
    );
    let trip_source = if config.queued_state == 0 {
        active
    } else {
        request
    };
    plant.connect(meanwhile_in, trip_source);
    plant.connect(meanwhile_in, service_guard.trip(1));

    // ----------------------------------------------------------------
    // The phase flags and the declared equipment pattern.
    // ----------------------------------------------------------------
    let mut phase_carriers = Vec::with_capacity(STEPS);
    let mut phase = [PointId(0); STEPS];
    for (n, slot) in phase.iter_mut().enumerate() {
        let name = format!("phase-{}", n + 1);
        let carrier = b.out_bool(
            plant,
            &name,
            &format!("Step {} is the active phase", n + 1),
            false,
        );
        plant.journaled(carrier);
        plant.connect(sequence.phase(n + 1), carrier);
        *slot = carrier.into();
        phase_carriers.push(carrier);
    }
    // The step-active carriers: `active AND phase_<n>`. The kind reports
    // step 1 while idle and while a queued filter sits armed on its first
    // step, so the equipment pattern reads the step-asserted-under-grant
    // form — the phase flags alone would drive the drain-down equipment
    // permanently. The raw phase flags stay served and journaled beside
    // them, so the step report and the equipment pattern are separately
    // visible.
    let mut step_carriers = Vec::with_capacity(STEPS);
    for (n, phase_carrier) in phase_carriers.iter().enumerate() {
        let gate = plant.add(BoolGateSpec::new(
            parameters([("operation", Value::Int(GATE_AND))]),
            2,
        ));
        let permissive = b.deliver_bool(
            plant,
            &format!("active-step-{}", n + 1),
            "Stepping delivered to a step-active carrier",
        );
        plant.connect(permissive, active);
        let flag = b.deliver_bool(
            plant,
            &format!("step-flag-{}", n + 1),
            "The phase flag delivered to its step-active gate",
        );
        plant.connect(flag, *phase_carrier);
        let carrier = b.out_bool(
            plant,
            &format!("step-active-{}", n + 1),
            &format!("Step {} is the active phase under the grant", n + 1),
            false,
        );
        plant.journaled(carrier);
        plant.connect(permissive, gate.input(1));
        plant.connect(flag, gate.input(2));
        plant.connect(&gate.out, carrier);
        step_carriers.push(carrier);
    }

    // A phase flag's delivered copies: a component `Out` port drives one
    // endpoint, so every reader beyond the first takes its own internal
    // `In` consumer one scan later.
    let mut fanout = [0usize; STEPS];
    let mut copies: Vec<Vec<InPoint<bool>>> = (0..STEPS).map(|_| Vec::new()).collect();
    let copy = |plant: &mut PlantBuilder,
                b: &mut Block,
                fanout: &mut [usize; STEPS],
                copies: &mut Vec<Vec<InPoint<bool>>>,
                carrier: OutPoint<bool>,
                n: usize,
                role: &str| {
        let name = format!("step-{}-{}", n + 1, fanout[n] + 1);
        let delivered = b.deliver_bool(
            plant,
            &name,
            &format!("Step {} delivered to the {role}", n + 1),
        );
        plant.connect(delivered, carrier);
        fanout[n] += 1;
        copies[n].push(delivered);
        delivered
    };

    // The drain-down request the simulated plant observes: the phase flag
    // through a `digital-output`, so the plant-side dynamics element reads
    // it on the field side like every other driven command.
    let drain_flag = copy(
        plant,
        &mut b,
        &mut fanout,
        &mut copies,
        step_carriers[step::DRAIN],
        step::DRAIN,
        "simulated plant",
    );
    let drain_output = plant.add(DigitalOutputSpec::new(parameters([])));
    plant.connect(drain_flag, &drain_output.input);
    plant.connect(&drain_output.out, drain_cmd);

    // The inlet pattern: filtering plus every step the declared equipment
    // table opens the inlet for.
    let inlet_pattern = EQUIPMENT_PATTERN.inlet;
    let inlet_gate = plant.add(BoolGateSpec::new(
        parameters([("operation", Value::Int(GATE_OR))]),
        inlet_pattern.len() + 1,
    ));
    let inlet_motor = plant.add(MotorSpec::new(parameters([(
        "fault_ticks",
        Value::Int(config.actuator_fault_ticks),
    )])));
    plant.param_unit(inlet_motor.id, "fault_ticks", unit::TICKS);
    plant.connect(filtering_in, inlet_gate.input(1));
    for (slot, n) in inlet_pattern.iter().enumerate() {
        let delivered = copy(
            plant,
            &mut b,
            &mut fanout,
            &mut copies,
            step_carriers[*n],
            *n,
            "inlet pattern gate",
        );
        plant.connect(delivered, inlet_gate.input(slot + 2));
    }
    plant.connect(&inlet_gate.out, &inlet_motor.cmd);
    plant.connect(inlet_fb, &inlet_motor.run);
    plant.connect(&inlet_motor.out, inlet_cmd);

    // The outlet pattern: the reference table holds the outlet open for
    // every wash step, so the helper composes it from `active` beside the
    // filtering condition rather than from per-step copies.
    let outlet_pattern = EQUIPMENT_PATTERN.outlet;
    let outlet_gate = plant.add(BoolGateSpec::new(
        parameters([("operation", Value::Int(GATE_OR))]),
        outlet_pattern.len() + 2,
    ));
    let outlet_motor = plant.add(MotorSpec::new(parameters([(
        "fault_ticks",
        Value::Int(config.actuator_fault_ticks),
    )])));
    plant.param_unit(outlet_motor.id, "fault_ticks", unit::TICKS);
    plant.connect(filtering_in, outlet_gate.input(1));
    plant.connect(active_in, outlet_gate.input(2));
    for (slot, n) in outlet_pattern.iter().enumerate() {
        let delivered = copy(
            plant,
            &mut b,
            &mut fanout,
            &mut copies,
            step_carriers[*n],
            *n,
            "outlet pattern gate",
        );
        plant.connect(delivered, outlet_gate.input(slot + 3));
    }
    plant.connect(&outlet_gate.out, &outlet_motor.cmd);
    plant.connect(outlet_fb, &outlet_motor.run);
    plant.connect(&outlet_motor.out, outlet_cmd);

    // The waste pattern: the union of the steps that send water to waste.
    let waste_pattern = EQUIPMENT_PATTERN.waste;
    let waste_gate = plant.add(BoolGateSpec::new(
        parameters([("operation", Value::Int(GATE_OR))]),
        waste_pattern.len(),
    ));
    let waste_motor = plant.add(MotorSpec::new(parameters([(
        "fault_ticks",
        Value::Int(config.actuator_fault_ticks),
    )])));
    plant.param_unit(waste_motor.id, "fault_ticks", unit::TICKS);
    for (slot, n) in waste_pattern.iter().enumerate() {
        let delivered = copy(
            plant,
            &mut b,
            &mut fanout,
            &mut copies,
            step_carriers[*n],
            *n,
            "waste pattern gate",
        );
        plant.connect(delivered, waste_gate.input(slot + 1));
    }
    plant.connect(&waste_gate.out, &waste_motor.cmd);
    plant.connect(waste_fb, &waste_motor.run);
    plant.connect(&waste_motor.out, waste_cmd);

    // The air pattern: single-active-step semantics keep air scour and
    // high-rate wash from asserting together, and the exclusion guard below
    // is the belt-and-suspenders enforcement on the high-rate path.
    let air_pattern = EQUIPMENT_PATTERN.air;
    let air_gate = plant.add(BoolGateSpec::new(
        parameters([("operation", Value::Int(GATE_AND))]),
        air_pattern.len(),
    ));
    let air_motor = plant.add(MotorSpec::new(parameters([(
        "fault_ticks",
        Value::Int(config.actuator_fault_ticks),
    )])));
    let blower_motor = plant.add(MotorSpec::new(parameters([(
        "fault_ticks",
        Value::Int(config.actuator_fault_ticks),
    )])));
    plant.param_unit(air_motor.id, "fault_ticks", unit::TICKS);
    plant.param_unit(blower_motor.id, "fault_ticks", unit::TICKS);
    for (slot, n) in air_pattern.iter().enumerate() {
        let delivered = copy(
            plant,
            &mut b,
            &mut fanout,
            &mut copies,
            step_carriers[*n],
            *n,
            "air pattern gate",
        );
        plant.connect(delivered, air_gate.input(slot + 1));
    }
    plant.connect(&air_gate.out, &air_motor.cmd);
    plant.connect(air_fb, &air_motor.run);
    plant.connect(&air_motor.out, air_cmd);
    for n in air_pattern.iter() {
        let delivered = copy(
            plant,
            &mut b,
            &mut fanout,
            &mut copies,
            step_carriers[*n],
            *n,
            "air-scour blower command",
        );
        plant.connect(delivered, &blower_motor.cmd);
    }
    plant.connect(blower_run, &blower_motor.run);
    plant.connect(&blower_motor.out, blower_cmd);

    // The wash pattern: the backwash pump runs only while the air-scour
    // exclusion holds.
    let exclusion_ok = b.out_bool(
        plant,
        "exclusion-ok",
        "High-rate wash is allowed — the air-scour phase is not asserting",
        true,
    );
    plant.journaled(exclusion_ok);
    let inv_air = plant.add(DigitalInputSpec::new(parameters([(
        "invert",
        Value::Bool(true),
    )])));
    let air_exclusion = copy(
        plant,
        &mut b,
        &mut fanout,
        &mut copies,
        step_carriers[step::AIR_SCOUR],
        step::AIR_SCOUR,
        "exclusion inversion",
    );
    plant.connect(air_exclusion, &inv_air.input);
    plant.connect(&inv_air.out, exclusion_ok);
    let wash_pattern = EQUIPMENT_PATTERN.wash;
    let wash_gate = plant.add(BoolGateSpec::new(
        parameters([("operation", Value::Int(GATE_AND))]),
        wash_pattern.len() + 1,
    ));
    let pump_motor = plant.add(MotorSpec::new(parameters([(
        "fault_ticks",
        Value::Int(config.actuator_fault_ticks),
    )])));
    plant.param_unit(pump_motor.id, "fault_ticks", unit::TICKS);
    for (slot, n) in wash_pattern.iter().enumerate() {
        let delivered = copy(
            plant,
            &mut b,
            &mut fanout,
            &mut copies,
            step_carriers[*n],
            *n,
            "pump gate",
        );
        plant.connect(delivered, wash_gate.input(slot + 1));
    }
    let exclusion_ok_in = b.deliver_bool(
        plant,
        "exclusion-ok-in",
        "Air-scour exclusion delivered to the pump gate",
    );
    plant.connect(exclusion_ok_in, exclusion_ok);
    plant.connect(exclusion_ok_in, wash_gate.input(wash_pattern.len() + 1));
    plant.connect(&wash_gate.out, &pump_motor.cmd);
    plant.connect(pump_run, &pump_motor.run);
    plant.connect(&pump_motor.out, pump_cmd);

    // The wash-water demand path: the declared step value through the
    // drain-down filtration-rate hold, then the exclusion guard the decision
    // records as the high-rate path's protection.
    let water_pattern = EQUIPMENT_PATTERN.water;
    let water_gate = plant.add(BoolGateSpec::new(
        parameters([("operation", Value::Int(GATE_OR))]),
        water_pattern.len(),
    ));
    for (slot, n) in water_pattern.iter().enumerate() {
        let delivered = copy(
            plant,
            &mut b,
            &mut fanout,
            &mut copies,
            step_carriers[*n],
            *n,
            "wash-water permissive",
        );
        plant.connect(delivered, water_gate.input(slot + 1));
    }
    let water_permissive_gate = plant.add(BoolGateSpec::new(
        parameters([("operation", Value::Int(GATE_AND))]),
        2,
    ));
    let water_gate_out = b.out_bool(plant, "water-step", "A wash-water step is asserting", false);
    let water_gate_in = b.deliver_bool(
        plant,
        "water-step-in",
        "Wash-water step delivered to the permissive gate",
    );
    let exclusion_ok_water = b.deliver_bool(
        plant,
        "exclusion-ok-water",
        "Air-scour exclusion delivered to the wash-water permissive",
    );
    let water_permissive = b.out_bool(
        plant,
        "water-permissive",
        "Wash water may flow — a wash step stands and air scour is clear",
        false,
    );
    let water_permissive_in = b.deliver_bool(
        plant,
        "water-permissive-in",
        "Wash-water permissive delivered to the high-rate exclusion guard",
    );
    plant.connect(&water_gate.out, water_gate_out);
    plant.connect(water_gate_in, water_gate_out);
    plant.connect(water_gate_in, water_permissive_gate.input(1));
    plant.connect(exclusion_ok_water, exclusion_ok);
    plant.connect(exclusion_ok_water, water_permissive_gate.input(2));
    plant.connect(&water_permissive_gate.out, water_permissive);
    plant.connect(water_permissive_in, water_permissive);

    let limiter = plant.add(RateLimiterSpec::new(parameters([(
        "max_delta",
        Value::Float(config.rate_limit),
    )])));
    plant.port_unit(limiter.id, "in", unit::PERCENT);
    plant.port_unit(limiter.id, "out", unit::PERCENT);
    let rate_limited = b.out_float(
        plant,
        "rate-limited",
        unit::PERCENT,
        "The rate-limited wash-water demand",
        0.0,
    );
    let limiter_in = b.deliver_float(
        plant,
        "limiter-in",
        unit::PERCENT,
        "The rate-limited demand delivered to the exclusion guard",
    );
    let exclusion_guard = plant.add(InterlockSpec::new(
        parameters([("safe_value", Value::Float(0.0))]),
        1,
    ));
    plant.port_unit(exclusion_guard.id, "in", unit::PERCENT);
    plant.port_unit(exclusion_guard.id, "out", unit::PERCENT);
    plant.param_unit(exclusion_guard.id, "safe_value", unit::PERCENT);
    let guarded_rate = b.out_float(
        plant,
        "wash-demand",
        unit::PERCENT,
        "The guarded wash-water demand the valve carries",
        0.0,
    );
    let exclusion_tripped = b.out_bool(
        plant,
        "exclusion-tripped",
        "The high-rate exclusion guard tripped — air scour asserted with wash water",
        false,
    );
    plant.journaled(exclusion_tripped);
    let wash_valve = plant.add(ValveSpec::new(parameters([
        ("tolerance", Value::Float(config.valve_tolerance_pct)),
        ("discrepancy_ticks", Value::Int(config.actuator_fault_ticks)),
    ])));
    plant.port_unit(wash_valve.id, "cmd", unit::PERCENT);
    plant.port_unit(wash_valve.id, "out", unit::PERCENT);
    plant.port_unit(wash_valve.id, "fb", unit::PERCENT);
    plant.param_unit(wash_valve.id, "tolerance", unit::PERCENT);
    plant.param_unit(wash_valve.id, "discrepancy_ticks", unit::TICKS);
    let valve_cmd = b.deliver_float(
        plant,
        "valve-cmd",
        unit::PERCENT,
        "The guarded demand delivered to the wash-water valve",
    );

    plant.connect(rate_in, &limiter.input);
    plant.connect(&limiter.out, rate_limited);
    plant.connect(limiter_in, rate_limited);
    plant.connect(limiter_in, &exclusion_guard.input);
    plant.connect(water_permissive_in, &exclusion_guard.permissive);
    let guard_trip = copy(
        plant,
        &mut b,
        &mut fanout,
        &mut copies,
        step_carriers[step::AIR_SCOUR],
        step::AIR_SCOUR,
        "high-rate exclusion guard",
    );
    plant.connect(guard_trip, exclusion_guard.trip(1));
    plant.connect(&exclusion_guard.out, guarded_rate);
    plant.connect(&exclusion_guard.tripped, exclusion_tripped);
    plant.connect(valve_cmd, guarded_rate);
    plant.connect(valve_cmd, &wash_valve.cmd);
    plant.connect(&wash_valve.out, wash_valve_cmd);
    plant.connect(wash_valve_pos, &wash_valve.fb);

    // ----------------------------------------------------------------
    // The fault aggregate (decisions 60, 61).
    // ----------------------------------------------------------------
    let fault = b.out_bool(
        plant,
        "fault",
        "The aggregated per-filter equipment fault",
        false,
    );
    plant.journaled(fault);
    let fault_seq = b.deliver_bool(
        plant,
        "fault-seq",
        "Fault aggregate delivered to the sequence",
    );
    let fault_alarm_in = b.deliver_bool(
        plant,
        "fault-alarm-in",
        "Fault aggregate delivered to its alarm",
    );
    let fault_gate = plant.add(BoolGateSpec::new(
        parameters([("operation", Value::Int(GATE_OR))]),
        9,
    ));
    // The 1 NTU turbidity tier's standing flag is the filter's first
    // declared fault input — decision 61's alarm-or-trip scope.
    let mut next_fault_input = 2;
    // The filter's own equipment-fault contact binds directly: a field
    // `In` point feeds many port inputs.
    plant.connect(fault_contact, fault_gate.input(1));
    for actuator in [
        &inlet_motor.fault,
        &outlet_motor.fault,
        &waste_motor.fault,
        &air_motor.fault,
        &blower_motor.fault,
        &pump_motor.fault,
    ] {
        plant.connect(actuator, fault_gate.input(next_fault_input));
        next_fault_input += 1;
    }
    plant.connect(&wash_valve.discrepancy, fault_gate.input(next_fault_input));
    let fault_trip_input = b.deliver_bool(
        plant,
        "turbidity-trip",
        "The 1 NTU tier's standing flag — the filter's first declared fault input",
    );
    plant.connect(&fault_gate.out, fault);
    plant.connect(fault_seq, fault);
    plant.connect(fault_seq, &sequence.fault);
    plant.connect(fault_alarm_in, fault);

    // ----------------------------------------------------------------
    // The post-wash verification pair (decision 61).
    // ----------------------------------------------------------------
    let cbhl = plant.add(PhaseMonitorSpec::new(parameters([
        ("bound", Value::Float(config.cbhl_bound_m)),
        ("limit_ticks", Value::Int(config.verify_limit_ticks)),
        ("mode", Value::Int(1)),
    ])));
    plant.port_unit(cbhl.id, "in", unit::M);
    plant.port_unit(cbhl.id, "deviation", unit::M);
    plant.param_unit(cbhl.id, "bound", unit::M);
    plant.param_unit(cbhl.id, "limit_ticks", unit::TICKS);
    let ripening = plant.add(PhaseMonitorSpec::new(parameters([
        ("bound", Value::Float(config.ripening_bound_ntu)),
        ("limit_ticks", Value::Int(config.verify_limit_ticks)),
        ("mode", Value::Int(0)),
    ])));
    plant.port_unit(ripening.id, "in", unit::NTU);
    plant.port_unit(ripening.id, "deviation", unit::NTU);
    plant.param_unit(ripening.id, "bound", unit::NTU);
    plant.param_unit(ripening.id, "limit_ticks", unit::TICKS);

    // The CBHL check captures its clean-bed baseline during the
    // reference-flow step and verifies the deviation across the post-wash
    // window; the ripening check bounds turbidity absolutely across the
    // same window.
    // The clean-bed-headloss check's window spans the reference-flow step and
    // the post-wash verification step together: the kind clears its captured
    // baseline when the window opens, so a window that opened only at the
    // verification step would discard the reference-flow capture. The capture
    // input rides the reference-flow step alone, so the baseline is the clean
    // bed's headloss at the declared reference flow and the deviation is
    // measured across the window that follows.
    let cbhl_window_gate = plant.add(BoolGateSpec::new(
        parameters([("operation", Value::Int(GATE_OR))]),
        2,
    ));
    let cbhl_capture = copy(
        plant,
        &mut b,
        &mut fanout,
        &mut copies,
        step_carriers[step::REFERENCE_FLOW],
        step::REFERENCE_FLOW,
        "CBHL baseline capture",
    );
    let cbhl_window_reference = copy(
        plant,
        &mut b,
        &mut fanout,
        &mut copies,
        step_carriers[step::REFERENCE_FLOW],
        step::REFERENCE_FLOW,
        "CBHL window",
    );
    let cbhl_window_verify = copy(
        plant,
        &mut b,
        &mut fanout,
        &mut copies,
        step_carriers[step::VERIFY],
        step::VERIFY,
        "CBHL window",
    );
    let cbhl_window = b.out_bool(
        plant,
        "cbhl-window",
        "The clean-bed-headloss check's window — the reference-flow and post-wash steps",
        false,
    );
    let cbhl_window_in = b.deliver_bool(
        plant,
        "cbhl-window-in",
        "The clean-bed-headloss window delivered to its check",
    );
    plant.connect(cbhl_window_reference, cbhl_window_gate.input(1));
    plant.connect(cbhl_window_verify, cbhl_window_gate.input(2));
    plant.connect(&cbhl_window_gate.out, cbhl_window);
    plant.connect(cbhl_window_in, cbhl_window);
    let ripening_phase = copy(
        plant,
        &mut b,
        &mut fanout,
        &mut copies,
        step_carriers[step::VERIFY],
        step::VERIFY,
        "ripening window",
    );
    let ripening_capture = copy(
        plant,
        &mut b,
        &mut fanout,
        &mut copies,
        step_carriers[step::VERIFY],
        step::VERIFY,
        "ripening capture",
    );
    let cbhl_dev = b.out_float(
        plant,
        "cbhl-deviation",
        unit::M,
        "The clean-bed-headloss deviation",
        0.0,
    );
    let cbhl_exceeded = b.out_bool(
        plant,
        "cbhl-exceeded",
        "The clean-bed headloss left its declared band",
        false,
    );
    let cbhl_overdue = b.out_bool(
        plant,
        "cbhl-overdue",
        "The clean-bed headloss has not settled within the declared deadline",
        false,
    );
    let cbhl_alarm_in = b.deliver_bool(
        plant,
        "cbhl-alarm-in",
        "Clean-bed-headloss excursion delivered to its alarm",
    );
    let ripening_exceeded = b.out_bool(
        plant,
        "ripening-exceeded",
        "The effluent turbidity sits above its declared bound",
        false,
    );
    let ripening_overdue = b.out_bool(
        plant,
        "ripening-overdue",
        "The effluent turbidity has not ripened within the declared deadline",
        false,
    );
    let ripening_gate = plant.add(BoolGateSpec::new(
        parameters([("operation", Value::Int(GATE_OR))]),
        2,
    ));
    let ripening_exceeded_in = b.deliver_bool(
        plant,
        "ripening-exceeded-in",
        "The ripening excursion delivered to its alarm",
    );
    let ripening_overdue_in = b.deliver_bool(
        plant,
        "ripening-overdue-in",
        "The ripening deadline delivered to its alarm",
    );
    let ripening_deviation = b.out_float(
        plant,
        "ripening-deviation",
        unit::NTU,
        "The ripening monitor's reported deviation — the captured baseline's offset",
        0.0,
    );
    let ripening_fault = b.out_bool(
        plant,
        "ripening-fault",
        "The ripening excursion or its deadline stands",
        false,
    );
    let ripening_alarm_in = b.deliver_bool(
        plant,
        "ripening-alarm-in",
        "Ripening excursion delivered to its alarm",
    );

    plant.connect(headloss, &cbhl.input);
    plant.connect(cbhl_capture, &cbhl.capture);
    plant.connect(cbhl_window_in, &cbhl.phase);
    plant.connect(&cbhl.deviation, cbhl_dev);
    plant.connect(&cbhl.exceeded, cbhl_exceeded);
    plant.connect(&cbhl.overdue, cbhl_overdue);
    plant.connect(cbhl_alarm_in, cbhl_exceeded);
    plant.connect(turbidity, &ripening.input);
    plant.connect(ripening_phase, &ripening.phase);
    plant.connect(ripening_capture, &ripening.capture);
    plant.connect(&ripening.exceeded, ripening_exceeded);
    plant.connect(&ripening.overdue, ripening_overdue);
    plant.connect(&ripening.deviation, ripening_deviation);
    plant.connect(ripening_exceeded_in, ripening_exceeded);
    plant.connect(ripening_exceeded_in, ripening_gate.input(1));
    plant.connect(ripening_overdue_in, ripening_overdue);
    plant.connect(ripening_overdue_in, ripening_gate.input(2));
    plant.connect(&ripening_gate.out, ripening_fault);
    plant.connect(ripening_alarm_in, ripening_fault);

    // ----------------------------------------------------------------
    // The declared alarm set (decision 61).
    // ----------------------------------------------------------------
    let alarm_base = FILTER_ALARM_BASE + FILTER_ALARMS * index as u64;

    let turbidity_alarm_instance = plant.add(ManagedLatchingAlarmSpec::new(
        parameters([
            ("low_limit", Value::Float(-PARKED_LIMIT)),
            ("high_limit", Value::Float(config.turbidity_alarm_ntu)),
            ("hysteresis", Value::Float(config.turbidity_hysteresis_ntu)),
            (
                "max_shelve_ticks",
                Value::Int(config.turbidity_alarm_max_shelve_ticks),
            ),
            ("priority", Value::Int(2)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(30)),
        ]),
        ManagedInputs {
            shelve: true,
            ..ManagedInputs::default()
        },
        rationalization(
            "The filter passes solids; the bed is letting them through",
            "Inspect the bed and schedule a backwash",
            &format!("{tag}-turbidity-alarm"),
        ),
    ));
    plant.connect(turbidity, &turbidity_alarm_instance.input);
    plant.port_unit(turbidity_alarm_instance.id, "in", unit::NTU);
    for parameter in ["low_limit", "high_limit", "hysteresis"] {
        plant.param_unit(turbidity_alarm_instance.id, parameter, unit::NTU);
    }
    for parameter in ["max_shelve_ticks", "response_ticks"] {
        plant.param_unit(turbidity_alarm_instance.id, parameter, unit::TICKS);
    }
    let turbidity_alarm = managed_alarm(
        plant,
        alarm_base,
        turbidity_alarm_instance.id,
        &turbidity_alarm_instance.ack,
        &turbidity_alarm_instance.managed,
        &turbidity_alarm_instance.alarm,
        &turbidity_alarm_instance.unacknowledged,
        &format!("{tag}-turbidity"),
        &group,
    );

    let turbidity_trip_instance = plant.add(ManagedLatchingAlarmSpec::new(
        parameters([
            ("low_limit", Value::Float(-PARKED_LIMIT)),
            ("high_limit", Value::Float(config.turbidity_trip_ntu)),
            ("hysteresis", Value::Float(config.turbidity_hysteresis_ntu)),
            ("max_shelve_ticks", Value::Int(0)),
            ("priority", Value::Int(1)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(15)),
        ]),
        ManagedInputs::default(),
        rationalization(
            "The filter effluent is above the declared shutdown tier",
            "Take the filter out of service and backwash it",
            &format!("{tag}-turbidity-trip"),
        ),
    ));
    plant.connect(turbidity, &turbidity_trip_instance.input);
    plant.port_unit(turbidity_trip_instance.id, "in", unit::NTU);
    for parameter in ["low_limit", "high_limit", "hysteresis"] {
        plant.param_unit(turbidity_trip_instance.id, parameter, unit::NTU);
    }
    for parameter in ["max_shelve_ticks", "response_ticks"] {
        plant.param_unit(turbidity_trip_instance.id, parameter, unit::TICKS);
    }
    let mut trip_alarm_carrier: Option<OutPoint<bool>> = None;
    let turbidity_trip_alarm = managed_alarm_carrying(
        plant,
        alarm_base + 1,
        turbidity_trip_instance.id,
        &turbidity_trip_instance.ack,
        &turbidity_trip_instance.managed,
        &turbidity_trip_instance.alarm,
        &turbidity_trip_instance.unacknowledged,
        &format!("{tag}-turbidity-trip"),
        &group,
        Some(&mut trip_alarm_carrier),
    );

    // Decision 61's declared alarm-or-trip scope: the 1 NTU tier's standing
    // flag is the filter's first declared fault input, so crossing the
    // shutdown tier drives the declared fault step. Its `alarm` output is
    // already bound to the alarm's own status carrier, so the aggregate reads
    // a delivered copy of that carrier rather than binding the port twice.
    plant.connect(
        fault_trip_input,
        trip_alarm_carrier.expect("the trip alarm declared its status carrier"),
    );
    plant.connect(fault_trip_input, fault_gate.input(9));

    let headloss_alarm_instance = plant.add(ManagedLatchingAlarmSpec::new(
        parameters([
            ("low_limit", Value::Float(-PARKED_LIMIT)),
            ("high_limit", Value::Float(config.headloss_bound_m)),
            ("hysteresis", Value::Float(config.headloss_hysteresis_m)),
            ("max_shelve_ticks", Value::Int(0)),
            ("priority", Value::Int(2)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(60)),
        ]),
        ManagedInputs::default(),
        rationalization(
            "The bed is fouling; the filter approaches its terminal headloss",
            "Backwash before the run reaches its terminal loss",
            &format!("{tag}-headloss-alarm"),
        ),
    ));
    plant.connect(headloss, &headloss_alarm_instance.input);
    plant.port_unit(headloss_alarm_instance.id, "in", unit::M);
    for parameter in ["low_limit", "high_limit", "hysteresis"] {
        plant.param_unit(headloss_alarm_instance.id, parameter, unit::M);
    }
    for parameter in ["max_shelve_ticks", "response_ticks"] {
        plant.param_unit(headloss_alarm_instance.id, parameter, unit::TICKS);
    }
    let headloss_alarm = managed_alarm(
        plant,
        alarm_base + 2,
        headloss_alarm_instance.id,
        &headloss_alarm_instance.ack,
        &headloss_alarm_instance.managed,
        &headloss_alarm_instance.alarm,
        &headloss_alarm_instance.unacknowledged,
        &format!("{tag}-headloss"),
        &group,
    );

    let aborted_instance = plant.add(ManagedBoolLatchingAlarmSpec::new(
        parameters([
            ("max_shelve_ticks", Value::Int(0)),
            ("priority", Value::Int(1)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(15)),
        ]),
        ManagedInputs::default(),
        rationalization(
            "The backwash stopped before its table completed",
            "Find the abort or fault that ended the wash and restart it",
            &format!("{tag}-aborted-alarm"),
        ),
    ));
    plant.connect(aborted_in, &aborted_instance.input);
    for parameter in ["max_shelve_ticks", "response_ticks"] {
        plant.param_unit(aborted_instance.id, parameter, unit::TICKS);
    }
    let aborted_alarm = managed_alarm(
        plant,
        alarm_base + 3,
        aborted_instance.id,
        &aborted_instance.ack,
        &aborted_instance.managed,
        &aborted_instance.alarm,
        &aborted_instance.unacknowledged,
        &format!("{tag}-aborted"),
        &group,
    );

    let fault_alarm_instance = plant.add(ManagedBoolLatchingAlarmSpec::new(
        parameters([
            ("max_shelve_ticks", Value::Int(0)),
            ("priority", Value::Int(1)),
            ("class", Value::Int(2)),
            ("response_ticks", Value::Int(15)),
        ]),
        ManagedInputs::default(),
        rationalization(
            "The filter's equipment faulted mid-wash; the sequence runs its declared fault step",
            "Repair the failed actuator and restart or abort the wash",
            &format!("{tag}-fault-alarm"),
        ),
    ));
    plant.connect(fault_alarm_in, &fault_alarm_instance.input);
    for parameter in ["max_shelve_ticks", "response_ticks"] {
        plant.param_unit(fault_alarm_instance.id, parameter, unit::TICKS);
    }
    let fault_alarm = managed_alarm(
        plant,
        alarm_base + 4,
        fault_alarm_instance.id,
        &fault_alarm_instance.ack,
        &fault_alarm_instance.managed,
        &fault_alarm_instance.alarm,
        &fault_alarm_instance.unacknowledged,
        &format!("{tag}-fault"),
        &group,
    );

    let cbhl_alarm_instance = plant.add(ManagedBoolLatchingAlarmSpec::new(
        parameters([
            ("max_shelve_ticks", Value::Int(0)),
            ("priority", Value::Int(2)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(120)),
        ]),
        ManagedInputs::default(),
        rationalization(
            "The washed bed did not return to its clean-bed headloss",
            "Investigate short-circuiting, blinding, or a damaged underdrain",
            &format!("{tag}-cbhl-alarm"),
        ),
    ));
    plant.connect(cbhl_alarm_in, &cbhl_alarm_instance.input);
    for parameter in ["max_shelve_ticks", "response_ticks"] {
        plant.param_unit(cbhl_alarm_instance.id, parameter, unit::TICKS);
    }
    let cbhl_alarm = managed_alarm(
        plant,
        alarm_base + 5,
        cbhl_alarm_instance.id,
        &cbhl_alarm_instance.ack,
        &cbhl_alarm_instance.managed,
        &cbhl_alarm_instance.alarm,
        &cbhl_alarm_instance.unacknowledged,
        &format!("{tag}-cbhl"),
        &group,
    );

    let ripening_alarm_instance = plant.add(ManagedBoolLatchingAlarmSpec::new(
        parameters([
            ("max_shelve_ticks", Value::Int(0)),
            ("priority", Value::Int(2)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(60)),
        ]),
        ManagedInputs::default(),
        rationalization(
            "The washed bed is not ripening to its declared effluent turbidity",
            "Re-inspect the bed, the wash rate, and the filter-to-waste step",
            &format!("{tag}-ripening-alarm"),
        ),
    ));
    plant.connect(ripening_alarm_in, &ripening_alarm_instance.input);
    for parameter in ["max_shelve_ticks", "response_ticks"] {
        plant.param_unit(ripening_alarm_instance.id, parameter, unit::TICKS);
    }
    let ripening_alarm = managed_alarm(
        plant,
        alarm_base + 6,
        ripening_alarm_instance.id,
        &ripening_alarm_instance.ack,
        &ripening_alarm_instance.managed,
        &ripening_alarm_instance.alarm,
        &ripening_alarm_instance.unacknowledged,
        &format!("{tag}-ripening"),
        &group,
    );

    FilterLayout {
        index: index + 1,
        headloss: points::headloss(index),
        turbidity: points::turbidity(index),
        level: points::level(index),
        drain_depth: points::drain_depth(index),
        fault_contact: points::fault_contact(index),
        operator_start: operator_start.into(),
        abort: abort.into(),
        in_service: in_service.into(),
        out_of_service: oos.into(),
        service_blocked: service_trip.into(),
        filtering: filtering.into(),
        request: request.into(),
        grant: grant.into(),
        active: active.into(),
        pending: pending.into(),
        done: done.into(),
        aborted: aborted.into(),
        overrun: overrun.into(),
        trigger_source: trigger.into(),
        step: step_position.into(),
        seq_out: seq_out.into(),
        position: position.into(),
        rate_limited: rate_limited.into(),
        guarded_rate: guarded_rate.into(),
        exclusion_ok: exclusion_ok.into(),
        water_permissive: water_permissive.into(),
        exclusion_tripped: exclusion_tripped.into(),
        fault: fault.into(),
        cbhl_deviation: cbhl_dev.into(),
        cbhl_exceeded: cbhl_exceeded.into(),
        cbhl_overdue: cbhl_overdue.into(),
        ripening_exceeded: ripening_exceeded.into(),
        ripening_overdue: ripening_overdue.into(),
        ripening_fault: ripening_fault.into(),
        trig_time: trig_time.into(),
        trig_headloss: trig_headloss.into(),
        trig_turbidity: trig_turbidity.into(),
        turbidity_trip: fault_trip_input.into(),
        phase,
        step_active: core::array::from_fn(|n| step_carriers[n].into()),
        inlet_cmd: points::inlet_cmd(index),
        outlet_cmd: points::outlet_cmd(index),
        waste_cmd: points::waste_cmd(index),
        air_cmd: points::air_cmd(index),
        blower_cmd: points::blower_cmd(index),
        pump_cmd: points::pump_cmd(index),
        wash_valve_cmd: points::wash_valve_cmd(index),
        sequence: sequence.id,
        cbhl: cbhl.id,
        ripening: ripening.id,
        turbidity_alarm,
        turbidity_trip_alarm,
        headloss_alarm,
        aborted_alarm,
        fault_alarm,
        cbhl_alarm,
        ripening_alarm,
    }
}
