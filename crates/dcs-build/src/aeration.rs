//! The reference diffused-air aeration train — the issue-#289
//! composition at the builder seam decisions 62–69 fix.
//!
//! [`aeration_train`] composes the train entirely through typed spec
//! handles and emits the versioned [`PlantModel`] document; the
//! checked-in copy lives at
//! `crates/dcs-demo/fixtures/aeration_train.json` beside its dynamics
//! document `aeration_train_dynamics.json` — the recorded fixture
//! location the station, the dosing skid, and the filter bank share —
//! and `dcs-build/tests/aeration_train.rs` asserts the emitted document
//! equals that artifact.
//!
//! ## The composition
//!
//! - **The zone demand path (decisions 65, 66):** each zone votes its
//!   three redundant DO probes through a `median-voter`, filters the
//!   voted measurement through a `signal-filter`, and closes
//!   the DO `pid` feedback loop on it. Where the plant declares the
//!   layer, a `flow-paced-ratio` paces the proportional feed-forward
//!   term — the ratio engine the decision reuses from the landed dosing
//!   kind — and a `feedforward-sum` adds the DO loop's trim under the
//!   declared authority bounds, the additive law the decision separates
//!   from `flow-paced-ratio`'s multiplicative `trim`. A
//!   `demand-fallback` sits on the loop's output carrying the declared
//!   all-bad-DO response and its alarmed engagement, and a
//!   `manual-station` sits between the served demand and the field
//!   output as the declared operator-takeover point.
//! - **Actuation and coordination (decisions 62, 63, 67):** each zone
//!   air valve is a `valve` with `fb` position feedback, its
//!   engineering demand scaled onto the field command by an
//!   `analog-output` — so the demand chain stays `sm3/h` end to end
//!   while the raw loop stays `%`. The shared `header-coordinator`
//!   carries the declared strategy, its bounded set-point and floored
//!   aggregate demand, the most-open selection, and the `max_pulsing`
//!   pulse cap over per-grid `timer`-driven requests. A `rate-limiter`
//!   bounds the set-point slew the decision requires. The `blower-group`
//!   stages the coordinator's `blower_demand` under the declared
//!   authority, thresholds, rotation, and per-unit bounds, and each
//!   machine is the decision-67 `motor` + `analog-output` composition
//!   with the protective-status family as declared field inputs feeding
//!   the `avail_i`/`fault_i` aggregation.
//! - **Surge guard (decision 64):** a `surge-guard` per machine between
//!   the group's `capacity_i` and the machine's `analog-output`, fed the
//!   unit's discharge airflow, the header pressure, the motor current,
//!   and the hardwired proven-surge device's status — the machine's own
//!   protection honored in the demand path, never re-implemented.
//! - **Accounting (decision 68):** a `totalizer` on each zone's
//!   delivered airflow and on the header's aggregate — the
//!   control-domain share of the measured airflow, with the derived
//!   specific-power metrics deferred to reporting as the decision
//!   records.
//! - **Alarms (decision 69):** the set lands as wired
//!   `managed-latching-alarm`/`managed-bool-latching-alarm` instances,
//!   each carrying its decision-70 rationalization record and its
//!   writable internal `ack` point. Every flag's alarm-versus-status
//!   choice is declared rather than defaulted: the per-zone DO tiers,
//!   the airflow floor, the DO-probe spread, the fallback engagement,
//!   and the manual-mode engagement alarm; the header-pressure bounds,
//!   the coordinator's `at_bound`, the pulse cap's `pulse_blocked`, and
//!   the group's `staging_pending`/`none_available`/`all_faulted`
//!   alarm; and per machine the proven `tripped` and the protective
//!   aggregate. The surge guard's `guarding` — an operating point
//!   inside the surge region — is carried as a Status-role flag only.
//!   Every managed status point is `journaled` under decision 74, the
//!   consolidated M10 acceptance record's lifecycle-adoption
//!   requirement.
//!
//! ## The declared point-id scheme
//!
//! Bank field points occupy `10` the header pressure, `11` the header's
//! aggregate metered airflow, `12` the influent flow the proportional
//! pacing engine reads, `13` the summed machine airflow the pressure
//! element reads, and `14` its scaled view. Per-zone field points occupy
//! one block per zone, `ZONE_FIELD_BASE + ZONE_FIELD_STRIDE·i` plus the
//! offsets below. Per-blower field points occupy
//! `BLOWER_FIELD_BASE + BLOWER_FIELD_STRIDE·b` plus the offsets below
//! (`i` the 0-based zone index, `b` the 0-based blower index,
//! `zones <= 10`, `blowers <= 8`).
//!
//! Bank carriers start at `200`; each zone owns the internal block at
//! `ZONE_BASE + ZONE_STRIDE·i` and each blower the block at
//! `BLOWER_BASE + BLOWER_STRIDE·b`, whose offsets the helper allocates
//! in declaration order — the operator's writable points first, then the
//! loop's carriers and their delivered copies. Every alarm owns a
//! ten-point block at `ALARM_BASE + 10·a` with the managed layout the
//! reference compositions share: `ack`/`shelve`/`oos` at offsets 0–2
//! where the instance declares the input, and
//! `alarm`/`unacknowledged`/`shelved`/`suppressed`/`out_of_service` at
//! 3–7. Bank alarms take `a` = 0–5, zone `i`'s six alarms
//! `a = 6 + 6·i + 0..=5`, and blower `b`'s two alarms
//! `a = 6 + 6·zones + 2·b + 0..=1`, in the order declared below. Every
//! point's signal sits at `SIGNAL_BASE + point`. The scheme is
//! deterministic in declaration order, so identical builder invocations
//! emit identical documents.
//!
//! ## Declared units (the dimensional-discipline decision)
//!
//! The train is the aeration-side adoption of architecture decision
//! 106's declared-unit metadata: the header pressure in `kPa`, the
//! airflow demand, measurement, and totalization in `sm3/h`/`sm3`, the
//! proportional pacing ratio in `sm3/sm3`, the DO measurement and its
//! alarm tiers in `mg/L`, the valve position and the analog raw loops in
//! `%`, the motor current in `A`, and every `*_ticks` interval in
//! `ticks`. The Bool permissive, alarm, status, and code carriers stay
//! undeclared, their signals keeping the explicit `""` marker.
//!
//! ## The dynamics document's contract
//!
//! The checked-in dynamics document expresses the train through the
//! existing `ProcessElement` vocabulary: per zone a `scaled_flow`
//! turning the commanded valve opening into the delivered airflow, a
//! `scaled_flow` of that airflow into the oxygen transfer and a second
//! of the declared uptake forcing into the drain, a `flow_sum` per probe
//! carrying its declared baseline bias, and a `first_order_lag` per
//! probe; per machine a `scaled_flow` of the commanded capacity onto the
//! scaled discharge-airflow demand, a `first_order_lag` standing the
//! machine's own response, and a `scaled_flow` of that airflow onto the
//! motor current; and bank-wide a `flow_sum` over the machines' airflow,
//! a `scaled_flow` onto the header's pressure demand, and a
//! `first_order_lag` carrying the header pressure the coordinator reads.
//! No dynamics behavior the vocabulary cannot express is worked around —
//! the aeration research's fouling and specific-power analytics stay
//! outside the contract, as decision 68 records.

use crate::specs::{
    AnalogOutputSpec, BlowerGroupInstance, BlowerGroupSpec, BoolGateSpec, DemandFallbackSpec,
    DigitalInputSpec, FeedforwardSumSpec, FlowPacedRatioSpec, HeaderCoordinatorInstance,
    HeaderCoordinatorSpec, InterlockSpec, ManagedAlarmHandles, ManagedBoolLatchingAlarmSpec,
    ManagedInputs, ManagedLatchingAlarmSpec, ManualStationSpec, MedianVoterSpec, MotorSpec,
    PidSpec, RateLimiterSpec, SignalFilterSpec, SurgeGuardSpec, TimerSpec, TotalizerSpec,
    ValveSpec,
};
use crate::station::{AlarmLayout, rationalization};
use crate::{
    BuildError, ComponentId, DeviceId, Direction, InPoint, OutPoint, PlantBuilder, PointId,
    SignalId, Sink, Source, Value, parameters, unit,
};
use dcs_model::PlantModel;

/// The zone count the reference train declares: two diffused-air zones
/// sharing one discharge header.
pub const REFERENCE_ZONES: usize = 2;
/// The redundant DO probe count the reference train declares — the 2oo3
/// `median-voter` each zone closes its feedback loop through.
pub const REFERENCE_PROBES: usize = 3;
/// The blower count the reference train declares — the N+1 pair the
/// `blower-group` stages and rotates.
pub const REFERENCE_BLOWERS: usize = 2;
/// The bank-level alarms the reference train declares.
pub const BANK_ALARMS: u64 = 6;
/// The alarms each zone declares, in declaration order.
pub const ZONE_ALARMS: u64 = 6;
/// The alarms each machine declares, in declaration order.
pub const BLOWER_ALARMS: u64 = 2;

/// Field point ids — the fixed blocks the dynamics document addresses.
pub mod points {
    use crate::PointId;

    /// The discharge-header pressure (`Float`, `In`, `kPa`) — the
    /// `first_order_lag` the coordinator and its bounds read.
    pub const PRESSURE: PointId = PointId(10);
    /// The header's aggregate metered airflow (`Float`, `In`, `sm3/h`) —
    /// the header `totalizer`'s rate input.
    pub const HEADER_AIRFLOW: PointId = PointId(11);
    /// The plant influent flow (`Float`, `In`, `sm3/h`) — the
    /// proportional pacing engine's `flow`.
    pub const INFLUENT_FLOW: PointId = PointId(12);
    /// The summed machine discharge airflow (`Float`, `In`, `sm3/h`) —
    /// the `flow_sum` the header pressure element reads.
    pub const BLOWER_SUM: PointId = PointId(13);
    /// The header's scaled total airflow (`Float`, `In`, `sm3/h`) — the
    /// `scaled_flow` the pressure sum's baseline rides on.
    pub const PRESSURE_SCALED: PointId = PointId(15);
    /// The header's pressure demand (`Float`, `In`, `kPa`) — the
    /// `flow_sum` the pressure lag reads, carrying the declared
    /// plant-side baseline bias.
    pub const PRESSURE_DEMAND: PointId = PointId(14);

    /// The first per-zone field block.
    pub const ZONE_FIELD_BASE: u64 = 20;
    /// The per-zone field-block stride.
    pub const ZONE_FIELD_STRIDE: u64 = 40;
    /// The analog offsets within a zone's field block.
    pub mod zone_analog {
        /// Dissolved-oxygen probe 1 (`Float`, `In`, `mg/L`).
        pub const DO_1: u64 = 0;
        /// Dissolved-oxygen probe 2 (`Float`, `In`, `mg/L`).
        pub const DO_2: u64 = 1;
        /// Dissolved-oxygen probe 3 (`Float`, `In`, `mg/L`).
        pub const DO_3: u64 = 2;
        /// The declared oxygen-uptake forcing (`Float`, `In`,
        /// `mg/L/scan`) — the plant-side load the DO response drains on.
        pub const UPTAKE: u64 = 3;
        /// The delivered zone airflow (`Float`, `In`, `sm3/h`) — the
        /// `scaled_flow` of the commanded valve opening, and the
        /// measurement the zone `totalizer` and the mixing-floor alarm
        /// read.
        pub const AIRFLOW: u64 = 4;
        /// The oxygen-transfer contribution (`Float`, `In`, `mg/L`) — the
        /// `scaled_flow` of the delivered airflow the probe balances
        /// read.
        pub const TRANSFER: u64 = 5;
        /// The oxygen-uptake drain term (`Float`, `In`, `mg/L`) — the
        /// `scaled_flow` of the declared uptake forcing.
        pub const DRAIN: u64 = 6;
        /// Probe 1's summed oxygen balance (`Float`, `In`, `mg/L`).
        pub const DO_NET_1: u64 = 7;
        /// Probe 2's summed oxygen balance (`Float`, `In`, `mg/L`).
        pub const DO_NET_2: u64 = 8;
        /// Probe 3's summed oxygen balance (`Float`, `In`, `mg/L`).
        pub const DO_NET_3: u64 = 9;
        /// The zone air valve position command (`Float`, `Out`, `%`).
        pub const VALVE_CMD: u64 = 10;
        /// The zone air valve position feedback (`Float`, `In`, `%`).
        pub const VALVE_POS: u64 = 11;
    }

    /// The first per-blower field block.
    pub const BLOWER_FIELD_BASE: u64 = 400;
    /// The per-blower field-block stride.
    pub const BLOWER_FIELD_STRIDE: u64 = 24;
    /// The analog offsets within a machine's field block.
    pub mod blower_analog {
        /// The scaled discharge-airflow demand (`Float`, `In`,
        /// `sm3/h`) — the `scaled_flow` of the commanded capacity.
        pub const FLOW_DEMAND: u64 = 0;
        /// The measured discharge airflow (`Float`, `In`, `sm3/h`) —
        /// the `first_order_lag` standing the machine's own response,
        /// and the `surge-guard`'s `flow` and the header's `flow_sum`
        /// read.
        pub const FLOW: u64 = 1;
        /// The motor current (`Float`, `In`, `A`) — the `scaled_flow` of
        /// the discharge airflow, the minimum-amperage surge proxy.
        pub const CURRENT: u64 = 2;
        /// The machine capacity command (`Float`, `Out`, `%`).
        pub const SPEED_CMD: u64 = 3;
    }
    /// The contact offsets within a machine's field block.
    pub mod blower_contact {
        /// The run feedback (`Bool`, `In`).
        pub const RUN_FB: u64 = 4;
        /// The declared availability (`Bool`, `In`).
        pub const AVAIL: u64 = 5;
        /// The hardwired bearing over-temperature report (`Bool`, `In`).
        pub const BEARING: u64 = 6;
        /// The hardwired vibration-level report (`Bool`, `In`).
        pub const VIBRATION: u64 = 7;
        /// The hardwired motor-overload report (`Bool`, `In`).
        pub const OVERLOAD: u64 = 8;
        /// The hardwired oil-system report (`Bool`, `In`).
        pub const OIL: u64 = 9;
        /// The hardwired proven-surge shutdown (`Bool`, `In`).
        pub const SURGE_TRIP: u64 = 10;
        /// The startup/shutdown override state (`Bool`, `In`).
        pub const OVERRIDE: u64 = 11;
        /// The run command (`Bool`, `Out`).
        pub const RUN_CMD: u64 = 12;
        /// The unloading/vent valve command (`Bool`, `Out`).
        pub const VENT_CMD: u64 = 13;
        /// The unloading/vent valve open contact (`Bool`, `In`).
        pub const VENT_FB: u64 = 14;
    }

    fn zone(index: usize, offset: u64) -> PointId {
        PointId(ZONE_FIELD_BASE + ZONE_FIELD_STRIDE * index as u64 + offset)
    }

    fn blower(index: usize, offset: u64) -> PointId {
        PointId(BLOWER_FIELD_BASE + BLOWER_FIELD_STRIDE * index as u64 + offset)
    }

    /// Zone `index`'s dissolved-oxygen probe `probe` (0-based).
    pub fn do_probe(index: usize, probe: usize) -> PointId {
        zone(index, zone_analog::DO_1 + probe as u64)
    }
    /// Zone `index`'s declared oxygen-uptake forcing.
    pub fn uptake(index: usize) -> PointId {
        zone(index, zone_analog::UPTAKE)
    }
    /// Zone `index`'s delivered airflow.
    pub fn airflow(index: usize) -> PointId {
        zone(index, zone_analog::AIRFLOW)
    }
    /// Zone `index`'s oxygen-transfer contribution.
    pub fn transfer(index: usize) -> PointId {
        zone(index, zone_analog::TRANSFER)
    }
    /// Zone `index`'s oxygen-uptake drain term.
    pub fn drain(index: usize) -> PointId {
        zone(index, zone_analog::DRAIN)
    }
    /// Zone `index`'s probe `probe` (0-based) summed oxygen balance.
    pub fn do_net(index: usize, probe: usize) -> PointId {
        zone(index, zone_analog::DO_NET_1 + probe as u64)
    }
    /// Zone `index`'s air valve position command.
    pub fn valve_cmd(index: usize) -> PointId {
        zone(index, zone_analog::VALVE_CMD)
    }
    /// Zone `index`'s air valve position feedback.
    pub fn valve_pos(index: usize) -> PointId {
        zone(index, zone_analog::VALVE_POS)
    }
    /// Machine `index`'s scaled discharge-airflow demand.
    pub fn flow_demand(index: usize) -> PointId {
        blower(index, blower_analog::FLOW_DEMAND)
    }
    /// Machine `index`'s measured discharge airflow.
    pub fn flow(index: usize) -> PointId {
        blower(index, blower_analog::FLOW)
    }
    /// Machine `index`'s motor current.
    pub fn current(index: usize) -> PointId {
        blower(index, blower_analog::CURRENT)
    }
    /// Machine `index`'s capacity command.
    pub fn speed_cmd(index: usize) -> PointId {
        blower(index, blower_analog::SPEED_CMD)
    }
    /// Machine `index`'s run feedback.
    pub fn run_fb(index: usize) -> PointId {
        blower(index, blower_contact::RUN_FB)
    }
    /// Machine `index`'s declared availability.
    pub fn avail(index: usize) -> PointId {
        blower(index, blower_contact::AVAIL)
    }
    /// Machine `index`'s hardwired bearing over-temperature report.
    pub fn bearing(index: usize) -> PointId {
        blower(index, blower_contact::BEARING)
    }
    /// Machine `index`'s hardwired vibration-level report.
    pub fn vibration(index: usize) -> PointId {
        blower(index, blower_contact::VIBRATION)
    }
    /// Machine `index`'s hardwired motor-overload report.
    pub fn overload(index: usize) -> PointId {
        blower(index, blower_contact::OVERLOAD)
    }
    /// Machine `index`'s hardwired oil-system report.
    pub fn oil(index: usize) -> PointId {
        blower(index, blower_contact::OIL)
    }
    /// Machine `index`'s hardwired proven-surge shutdown.
    pub fn surge_trip(index: usize) -> PointId {
        blower(index, blower_contact::SURGE_TRIP)
    }
    /// Machine `index`'s startup/shutdown override state.
    pub fn override_state(index: usize) -> PointId {
        blower(index, blower_contact::OVERRIDE)
    }
    /// Machine `index`'s run command.
    pub fn run_cmd(index: usize) -> PointId {
        blower(index, blower_contact::RUN_CMD)
    }
    /// Machine `index`'s unloading/vent valve command.
    pub fn vent_cmd(index: usize) -> PointId {
        blower(index, blower_contact::VENT_CMD)
    }
    /// Machine `index`'s unloading/vent valve open contact.
    pub fn vent_fb(index: usize) -> PointId {
        blower(index, blower_contact::VENT_FB)
    }
}

/// The first bank internal carrier block.
const BANK_BASE: u64 = 200;
/// The bank block's capacity in points.
const BANK_CAPACITY: u64 = 40;
/// The first per-zone internal block; zone `i` owns
/// `ZONE_BASE + ZONE_STRIDE·i ..= +ZONE_STRIDE-1`.
const ZONE_BASE: u64 = 1000;
/// The per-zone internal block stride.
const ZONE_STRIDE: u64 = 200;
/// The first per-machine internal block; machine `b` owns
/// `BLOWER_BASE + BLOWER_STRIDE·b ..= +BLOWER_STRIDE-1`.
const BLOWER_BASE: u64 = 2000;
/// The per-machine internal block stride.
const BLOWER_STRIDE: u64 = 120;
/// Alarm points: alarm `a` owns `ALARM_BASE + a·10 ..= +7` with the
/// managed layout the reference compositions share.
const ALARM_BASE: u64 = 10_000;
/// Every point's signal id is `SIGNAL_BASE + point`.
const SIGNAL_BASE: u64 = 100_000;

/// `bool-gate`'s `operation` codes — `GateOperation::And`/`Or` from
/// `dcs-blocks`, mirrored as data because `dcs-build` cannot depend on
/// the blocks crate.
const GATE_AND: i64 = 0;
const GATE_OR: i64 = 1;

/// The bound a single-sided analog alarm parks its unused limit at —
/// far outside any measurable span, so only the declared threshold side
/// can trip — and the totalizers' `rollover`, far above any total the
/// train accumulates.
const PARKED_LIMIT: f64 = 1.0e9;

/// The train's tunable contract — the declared coordination codes and
/// bounds, the staging policy, the surge-region bounds, the declared
/// DO-loss response, the feed-forward layer's bounds, and the alarm
/// policy. [`reference`](Self::reference) is the checked-in document's
/// configuration.
///
/// Every field is declared data rather than a default: the coordination
/// strategy and its bounds, the staging authority and rotation policy,
/// the per-unit machine bounds, the surge-region bounds, and the DO-loss
/// response are the owner-convergent choices the decisions mark
/// assumption-marked — carried as declared data, never kind defaults.
#[derive(Debug, Clone, PartialEq)]
pub struct AerationTrainConfig {
    /// The zone count `N`.
    pub zones: usize,
    /// The redundant DO probe count per zone — `3` closes the 2oo3
    /// `median-voter` the decision's measurement architecture declares.
    pub probes: usize,
    /// The machine count `M`.
    pub blowers: usize,
    /// Whether the zone declares the proportional feed-forward layer —
    /// the `flow-paced-ratio` pacing engine and the `feedforward-sum`
    /// additive trim. `false` composes the DO-feedback-only zone the
    /// decision records as equally expressible.
    pub feedforward: bool,
    /// Whether the train declares the per-grid mixing-pulse layer — the
    /// `timer`-driven `pulsing_i` requests the coordinator's
    /// `max_pulsing` cap arbitrates.
    pub pulsed_mixing: bool,
    /// The header coordinator's `strategy` code — `0` constant header
    /// pressure, `1` most-open-valve reset, `2` direct-airflow.
    pub strategy: i64,
    /// The held header-pressure set-point in `kPa` — the constant- and
    /// direct-airflow strategies' reference and the walking set-point's
    /// seed.
    pub pressure_hold: f64,
    /// The emitted set-point's lower bound in `kPa`.
    pub pressure_min: f64,
    /// The emitted set-point's upper bound in `kPa`.
    pub pressure_max: f64,
    /// The near-open band's lower edge in percent — the most-open-valve
    /// reset's declared band.
    pub mov_band_lo: f64,
    /// The near-open band's upper edge in percent.
    pub mov_band_hi: f64,
    /// The minimum interval between set-point moves, in scans.
    pub adjust_ticks: i64,
    /// The header-level mixing floor in `sm3/h` — the aggregate demand
    /// cannot fall below it.
    pub min_total_airflow: f64,
    /// The cap on simultaneously admitted mixing pulses.
    pub max_pulsing: i64,
    /// The per-scan set-point slew bound in `kPa` the `rate-limiter`
    /// declares.
    pub pressure_slew_kpa: f64,
    /// The per-grid mixing timer's dwell in scans.
    pub pulse_ticks: i64,
    /// The blower group's `staging_authority` code — `0` automatic,
    /// `1` operator approval, `2` flag-only.
    pub staging_authority: i64,
    /// The stage-up demand threshold as a fraction of the committed
    /// capacity.
    pub stage_up: f64,
    /// The stage-down demand threshold as a fraction of the committed
    /// capacity.
    pub stage_down: f64,
    /// The minimum run time in scans and the rotation margin it doubles
    /// as.
    pub min_run_ticks: i64,
    /// The minimum interval between starts in scans.
    pub min_start_interval_ticks: i64,
    /// The declared offline-vent dwell in scans before a unit joins the
    /// header.
    pub vent_ticks: i64,
    /// The lead/standby alternation code — `0` none, `1` equalize
    /// runtime, `2` fixed order.
    pub rotation: i64,
    /// Per machine's minimum flow in `sm3/h`.
    pub unit_min_flow: Vec<f64>,
    /// Per machine's maximum flow in `sm3/h`.
    pub unit_max_flow: Vec<f64>,
    /// Per machine's maximum motor current expressed in the demand
    /// coordinate — the airflow the machine's current bound permits, so
    /// `blower-group`'s effective ceiling is
    /// `min(unit_max_flow, unit_max_current)`.
    pub unit_max_current: Vec<f64>,
    /// The surge region's low-flow edge in `sm3/h`.
    pub surge_min_flow: f64,
    /// The surge region's high-pressure edge in `kPa`.
    pub surge_max_pressure: f64,
    /// The surge region's minimum-amperage proxy in `A`.
    pub surge_min_current: f64,
    /// The `surge-guard`'s `on_guard` code — `0` clamp at the bound and
    /// continue, `1` trip to `trip_value`.
    pub surge_on_guard: i64,
    /// The demand a proven surge or declared trip emits, in `sm3/h`.
    pub surge_trip_value: f64,
    /// The `demand-fallback`'s `on_bad` code — `0` hold the last `Good`
    /// demand, `1` drive `fallback_flow`, `2` drive `safe_flow`.
    pub on_bad: i64,
    /// The fixed airflow the `on_bad = 1` response drives, in `sm3/h`.
    pub fallback_flow: f64,
    /// The declared safe airflow the `on_bad = 2` response drives, in
    /// `sm3/h`.
    pub safe_flow: f64,
    /// The feed-forward layer's trim-authority lower bound in `sm3/h`.
    pub trim_min: f64,
    /// The feed-forward layer's trim-authority upper bound in `sm3/h`.
    pub trim_max: f64,
    /// The summed-demand lower bound in `sm3/h`.
    pub min_demand: f64,
    /// The summed-demand upper bound in `sm3/h` — the full-open demand
    /// one zone's valve delivers at 100 %.
    pub max_demand: f64,
    /// The feed-forward term's untrusted response — `0` drop the term,
    /// `1` hold its last `Good` value.
    pub on_bad_ff: i64,
    /// The trim term's untrusted response — `0` drop the term, `1` hold
    /// its last `Good` value.
    pub on_bad_trim: i64,
    /// The seeded proportional pacing ratio in `sm3/sm3` of air per unit
    /// influent flow — the operator setpoint the writable point carries.
    pub air_per_flow: f64,
    /// The DO loop's proportional gain, in `(sm3/h)` per `(mg/L)`.
    pub pid_kp: f64,
    /// The DO loop's time step in seconds.
    pub pid_dt: f64,
    /// The seeded DO set-point in `mg/L`.
    pub do_sp: f64,
    /// The DO measurement's smoothing factor — the `signal-filter`'s
    /// `alpha`, in `(0, 1]`.
    pub do_alpha: f64,
    /// The probe spread in `mg/L` — the `median-voter`'s tolerance.
    pub do_spread: f64,
    /// The per-zone low-DO alarm tier in `mg/L` — the permit-relevant
    /// direction.
    pub do_low: f64,
    /// The per-zone high-DO alarm tier in `mg/L`.
    pub do_high: f64,
    /// The declared per-zone mixing floor in `sm3/h` — the airflow
    /// alarm's bound.
    pub airflow_floor: f64,
    /// The header-pressure alarm's lower bound in `kPa`.
    pub pressure_alarm_min: f64,
    /// The header-pressure alarm's upper bound in `kPa`.
    pub pressure_alarm_max: f64,
    /// The header-pressure freshness budget in scans — the whole-train
    /// stale command path's single declared budget, exactly as the
    /// station declares one on its net-flow measurement: a header
    /// pressure that stops changing presents `Uncertain(Stale)`, and
    /// the coordinator's most-open reset then holds its last emitted
    /// set-point rather than walking an untrusted reading.
    pub pressure_stale_ticks: u64,
    /// The air-valve position tolerance in percent.
    pub valve_tolerance_pct: f64,
    /// The air-valve feedback-discrepancy budget in scans.
    pub valve_discrepancy_ticks: i64,
    /// The motor feedback-discrepancy budget in scans.
    pub motor_fault_ticks: i64,
    /// The DO and pressure alarm hysteresis band, in each quantity's own
    /// unit.
    pub hysteresis: f64,
    /// The nuisance airflow alarm's `max_shelve_ticks` — the site's
    /// declared `WW-ALM-002` policy, carried as data.
    pub airflow_alarm_max_shelve_ticks: i64,
}

impl AerationTrainConfig {
    /// The reference train the checked-in documents record: two zones
    /// closed on three redundant DO probes each with the declared
    /// feed-forward layer, coordinated most-open-valve reset over a
    /// two-machine group staging under operator approval with
    /// equalize-runtime rotation, each machine guarded against its
    /// declared surge region, and the DO-loss response declared as the
    /// safe airflow.
    pub fn reference() -> Self {
        Self {
            zones: REFERENCE_ZONES,
            probes: REFERENCE_PROBES,
            blowers: REFERENCE_BLOWERS,
            feedforward: true,
            pulsed_mixing: true,
            strategy: 1,
            pressure_hold: 50.0,
            pressure_min: 35.0,
            pressure_max: 65.0,
            mov_band_lo: 45.0,
            mov_band_hi: 95.0,
            adjust_ticks: 12,
            min_total_airflow: 200.0,
            max_pulsing: 1,
            pressure_slew_kpa: 4.0,
            pulse_ticks: 6,
            staging_authority: 1,
            stage_up: 0.85,
            stage_down: 0.45,
            min_run_ticks: 10,
            min_start_interval_ticks: 4,
            vent_ticks: 4,
            rotation: 1,
            unit_min_flow: vec![150.0, 150.0],
            unit_max_flow: vec![700.0, 700.0],
            unit_max_current: vec![600.0, 600.0],
            surge_min_flow: 120.0,
            surge_max_pressure: 58.0,
            surge_min_current: 10.0,
            surge_on_guard: 0,
            surge_trip_value: 0.0,
            on_bad: 2,
            fallback_flow: 300.0,
            safe_flow: 250.0,
            trim_min: -300.0,
            trim_max: 300.0,
            min_demand: 0.0,
            max_demand: 550.0,
            on_bad_ff: 0,
            on_bad_trim: 1,
            air_per_flow: 0.02,
            pid_kp: 120.0,
            pid_dt: 1.0,
            do_sp: 2.0,
            do_alpha: 0.4,
            do_spread: 0.2,
            do_low: 1.0,
            do_high: 3.0,
            airflow_floor: 120.0,
            pressure_alarm_min: 35.0,
            pressure_alarm_max: 62.0,
            pressure_stale_ticks: 25,
            valve_tolerance_pct: 12.0,
            valve_discrepancy_ticks: 2,
            motor_fault_ticks: 2,
            hysteresis: 0.05,
            airflow_alarm_max_shelve_ticks: 8,
        }
    }
}

/// Zone `index`'s place in the emitted document.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ZoneLayout {
    /// The 1-based zone index — the `valve_pos_i`/`airflow_i`/
    /// `pulsing_i`/`pulse_grant_i` port-family member.
    pub index: usize,
    /// The zone's `In` dissolved-oxygen probes.
    pub probes: Vec<PointId>,
    /// The declared oxygen-uptake forcing the plant-side load rides on.
    pub uptake: PointId,
    /// The delivered zone airflow.
    pub airflow: PointId,
    /// The air-valve position command.
    pub valve_cmd: PointId,
    /// The air-valve position feedback.
    pub valve_pos: PointId,
    /// The operator's DO set-point — a writable internal `In` point.
    pub do_sp: PointId,
    /// The operator's proportional pacing ratio — writable.
    pub air_per_flow: PointId,
    /// The operator's manual-mode select — writable.
    pub manual_mode: PointId,
    /// The operator's manual airflow rate — writable.
    pub manual_rate: PointId,
    /// The operator's mixing-pulse start — writable.
    pub pulse_start: PointId,
    /// The voted DO measurement.
    pub do_selected: PointId,
    /// The probe-spread diagnostic.
    pub do_discrepancy: PointId,
    /// The filtered DO measurement the loop and the fallback read.
    pub do_filtered: PointId,
    /// The DO loop's additive trim.
    pub pid_trim: PointId,
    /// The feed-forward layer's clamped flag.
    pub clamped: PointId,
    /// The paced term's own clamped flag — decision 50's status
    /// vocabulary on the reused ratio engine.
    pub ratio_clamped: PointId,
    /// The paced term's declared bad-influent response.
    pub ratio_fallback: PointId,
    /// The additive layer's declared bad-term response.
    pub ff_fallback: PointId,
    /// The demand the declared all-bad-DO response serves.
    pub served: PointId,
    /// The zone's commanded airflow after the operator takeover — the
    /// value the valve's engineering scaling carries and the coordinator's
    /// zone input reads.
    pub commanded: PointId,
    /// The fallback's alarmed engagement flag.
    pub fallback_active: PointId,
    /// The operator takeover's engaged flag.
    pub manual_active: PointId,
    /// The air valve's proven feedback discrepancy.
    pub valve_discrepancy: PointId,
    /// The zone's totalized delivered airflow.
    pub zone_total: PointId,
    /// The coordinator's pulse admission for this zone.
    pub pulse_grant: PointId,
    /// The per-grid mixing-pulse request.
    pub pulsing: PointId,
    /// The low-DO managed alarm.
    pub do_low_alarm: AlarmLayout,
    /// The high-DO managed alarm.
    pub do_high_alarm: AlarmLayout,
    /// The airflow-below-mixing-floor managed alarm — the declared
    /// nuisance candidate, so the one shelvable alarm of the train.
    pub airflow_alarm: AlarmLayout,
    /// The DO-probe spread managed alarm.
    pub do_discrepancy_alarm: AlarmLayout,
    /// The DO-loss fallback-engagement managed alarm.
    pub fallback_alarm: AlarmLayout,
    /// The operator-takeover engagement managed alarm.
    pub manual_alarm: AlarmLayout,
}

/// Machine `index`'s place in the emitted document.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BlowerLayout {
    /// The 1-based machine index — the `cmd_i`/`run_i`/`fault_i`/
    /// `avail_i`/`capacity_i`/`vent_i` port-family member.
    pub index: usize,
    /// The measured discharge airflow.
    pub flow: PointId,
    /// The motor current.
    pub current: PointId,
    /// The machine capacity command.
    pub speed_cmd: PointId,
    /// The run command.
    pub run_cmd: PointId,
    /// The run feedback.
    pub run_fb: PointId,
    /// The declared availability.
    pub avail: PointId,
    /// The unloading/vent valve command.
    pub vent_cmd: PointId,
    /// The unloading/vent valve open contact.
    pub vent_fb: PointId,
    /// The hardwired proven-surge shutdown contact.
    pub surge_trip: PointId,
    /// The group's split capacity demand for this machine.
    pub capacity: PointId,
    /// The surge-guarded demand the machine's output carries.
    pub guarded: PointId,
    /// The surge region's engagement flag — Status-role only, the
    /// declared alarm-versus-status choice.
    pub guarding: PointId,
    /// The guard's proven-trip flag.
    pub tripped: PointId,
    /// The motor's proven fault.
    pub motor_fault: PointId,
    /// The hardwired protective-status family's aggregate.
    pub protective: PointId,
    /// The machine's availability as the group reads it.
    pub group_avail: PointId,
    /// The machine's fault as the group reads it.
    pub group_fault: PointId,
    /// The run gate's own trip status — a machine whose run feedback does
    /// not prove it running withholds its capacity demand.
    pub run_gated: PointId,
    /// The group's commanded run request as the motor reads it.
    pub group_cmd: PointId,
    /// The proven-surge managed alarm.
    pub trip_alarm: AlarmLayout,
    /// The hardwired protective-status managed alarm.
    pub protective_alarm: AlarmLayout,
}

/// Where everything the composition declares landed — the ids the
/// dynamics document, the scripted run, and any embedding surface
/// address.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AerationTrainLayout {
    /// The discharge-header pressure.
    pub pressure: PointId,
    /// The header's aggregate metered airflow.
    pub header_airflow: PointId,
    /// The plant influent flow the pacing engine reads.
    pub influent_flow: PointId,
    /// The operator's shared totalizer reset — writable.
    pub total_reset: PointId,
    /// The operator's release of the group's pending stage change —
    /// writable under the declared approval authority.
    pub approve: PointId,
    /// The coordinator's emitted set-point.
    pub pressure_sp: PointId,
    /// The slew-bounded set-point the decision's downstream
    /// `rate-limiter` carries.
    pub pressure_sp_slew: PointId,
    /// The coordinator's aggregate capacity demand.
    pub blower_demand: PointId,
    /// The coordinator's most-open zone identity.
    pub most_open: PointId,
    /// The coordinator's at-bound flag.
    pub at_bound: PointId,
    /// The coordinator's pulse-cap refusal flag.
    pub pulse_blocked: PointId,
    /// The group's staged machine count.
    pub staged: PointId,
    /// The group's in-flight transition flag.
    pub transition: PointId,
    /// The group's staging-pending flag.
    pub staging_pending: PointId,
    /// The group's no-machine-available flag.
    pub none_available: PointId,
    /// The group's all-faulted flag.
    pub all_faulted: PointId,
    /// The header's totalized aggregate airflow.
    pub header_total: PointId,
    /// The `header-coordinator` instance's id.
    pub coordinator: ComponentId,
    /// The `blower-group` instance's id.
    pub group: ComponentId,
    /// The set-point slew limiter's id.
    pub slew: ComponentId,
    /// Per-zone layouts, in `index` order.
    pub zones: Vec<ZoneLayout>,
    /// Per-machine layouts, in `index` order.
    pub blowers: Vec<BlowerLayout>,
    /// The header-pressure-bounds managed alarm.
    pub pressure_alarm: AlarmLayout,
    /// The coordinator's at-bound managed alarm.
    pub at_bound_alarm: AlarmLayout,
    /// The pulse-cap managed alarm.
    pub pulse_blocked_alarm: AlarmLayout,
    /// The group's staging-pending managed alarm.
    pub staging_pending_alarm: AlarmLayout,
    /// The group's no-machine-available managed alarm.
    pub none_available_alarm: AlarmLayout,
    /// The group's all-faulted managed alarm.
    pub all_faulted_alarm: AlarmLayout,
}

/// The composed train: the emitted document plus the layout every
/// declared id landed on.
#[derive(Debug, Clone)]
pub struct AerationTrain {
    /// The versioned plant-model document.
    pub model: PlantModel,
    /// The id map into it.
    pub layout: AerationTrainLayout,
}

/// Composes the train under `config` and emits its [`PlantModel`].
///
/// Every component registers through its typed spec and every connection
/// goes through typed handles — port existence, direction, and value kind
/// are compile-time-checked where the types reach and `build`-checked
/// where they do not.
///
/// # Panics
///
/// `config.zones` outside `1..=10`, `config.blowers` outside `1..=8`,
/// `config.probes` other than `3`, or per-machine bound vectors of the
/// wrong length exceeds the declared point-id and port-family scheme.
pub fn aeration_train(config: &AerationTrainConfig) -> Result<AerationTrain, BuildError> {
    assert!(
        (1..=10).contains(&config.zones),
        "the train's point-id scheme admits 1..=10 zones, got {}",
        config.zones
    );
    assert!(
        (1..=8).contains(&config.blowers),
        "the train's point-id scheme admits 1..=8 machines, got {}",
        config.blowers
    );
    assert_eq!(
        config.probes, 3,
        "each zone closes its feedback loop through a 2oo3 median-voter over three probes, got {}",
        config.probes
    );
    assert_eq!(
        config.unit_min_flow.len(),
        config.blowers,
        "each machine declares its own bounds"
    );
    assert_eq!(config.unit_max_flow.len(), config.blowers);
    assert_eq!(config.unit_max_current.len(), config.blowers);

    let mut plant = PlantBuilder::new();

    // The simulated I/O devices — all under the `sim` prefix, so the
    // standard driver registry serves them from the local SimDriver.
    let ai = plant.device("sim-ai").id;
    let di = plant.device("sim-di").id;
    let d_o = plant.device("sim-do").id;
    let ao = plant.device("sim-ao").id;

    // The field-point declaring helpers, so a channel and its point are
    // bound in one expression without a second borrow of the builder.
    let in_f = |plant: &mut PlantBuilder, point: PointId, name: &str| {
        let channel = plant.channel::<f64>(ai, name, Direction::In);
        plant.field_input::<f64>(point, channel, false)
    };
    let in_f_stale = |plant: &mut PlantBuilder, point: PointId, name: &str, budget: u64| {
        let channel = plant.channel::<f64>(ai, name, Direction::In);
        plant.field_input_stale_after::<f64>(point, channel, false, budget)
    };

    // ----------------------------------------------------------------
    // The bank's field points.
    // ----------------------------------------------------------------
    let pressure = in_f_stale(
        &mut plant,
        points::PRESSURE,
        "header-pressure",
        config.pressure_stale_ticks,
    );
    let header_airflow = in_f(&mut plant, points::HEADER_AIRFLOW, "header-airflow");
    let influent_flow = in_f(&mut plant, points::INFLUENT_FLOW, "influent-flow");
    in_f(&mut plant, points::BLOWER_SUM, "blower-sum");
    in_f(&mut plant, points::PRESSURE_SCALED, "pressure-scaled");
    in_f(&mut plant, points::PRESSURE_DEMAND, "pressure-demand");
    // Decision 102's declared recording duty: the compliance series
    // records every scan.
    plant.record(pressure, 1);
    plant.record(header_airflow, 1);
    plant.record(influent_flow, 1);
    for (point, name, declared_unit, description) in [
        (
            points::PRESSURE,
            "header-pressure",
            unit::KPA,
            "Discharge-header pressure the coordinator and its bounds read",
        ),
        (
            points::HEADER_AIRFLOW,
            "header-airflow",
            unit::SM3_PER_H,
            "Aggregate metered airflow the header totalizer accumulates",
        ),
        (
            points::INFLUENT_FLOW,
            "influent-flow",
            unit::SM3_PER_H,
            "Plant influent flow the proportional pacing engines read",
        ),
        (
            points::BLOWER_SUM,
            "blower-sum",
            unit::SM3_PER_H,
            "Summed machine discharge airflow the pressure element reads",
        ),
        (
            points::PRESSURE_SCALED,
            "pressure-scaled",
            unit::SM3_PER_H,
            "Scaled total airflow the header pressure sum rides on",
        ),
        (
            points::PRESSURE_DEMAND,
            "pressure-demand",
            unit::KPA,
            "Header pressure demand the pressure lag carries",
        ),
    ] {
        signal(
            &mut plant,
            point,
            name,
            declared_unit,
            description,
            "aeration-header",
        );
    }

    // ----------------------------------------------------------------
    // The bank's carriers, the coordinator, and the header accounting.
    // ----------------------------------------------------------------
    let mut bank = Block::new(
        BANK_BASE,
        BANK_CAPACITY,
        "bank".to_string(),
        "aeration-header".to_string(),
    );
    let pressure_sp = bank.out_float(
        &mut plant,
        "pressure-sp",
        unit::KPA,
        "The coordinator's emitted header-pressure set-point",
        config.pressure_hold,
    );
    let pressure_sp_in = bank.deliver_float(
        &mut plant,
        "pressure-sp-in",
        unit::KPA,
        "Set-point delivered to the slew limiter",
    );
    let pressure_sp_slew = bank.out_float(
        &mut plant,
        "pressure-sp-slew",
        unit::KPA,
        "The slew-bounded set-point a capacity-follow reads",
        config.pressure_hold,
    );
    let blower_demand = bank.out_float(
        &mut plant,
        "blower-demand",
        unit::SM3_PER_H,
        "The coordinator's aggregate capacity demand",
        0.0,
    );
    let blower_demand_in = bank.deliver_float(
        &mut plant,
        "blower-demand-in",
        unit::SM3_PER_H,
        "Aggregate demand delivered to the blower group",
    );
    let most_open = bank.out_int(
        &mut plant,
        "most-open",
        "The 1-based index of the zone whose valve is most open",
        0,
    );
    let at_bound = bank.out_bool(
        &mut plant,
        "at-bound",
        "The coordinator can no longer optimize — a declared bound is engaged",
        false,
    );
    let at_bound_in = bank.deliver_bool(
        &mut plant,
        "at-bound-in",
        "At-bound flag delivered to its alarm",
    );
    let pulse_blocked = bank.out_bool(
        &mut plant,
        "pulse-blocked",
        "A mixing-pulse request stands refused by the declared cap",
        false,
    );
    let pulse_blocked_in = bank.deliver_bool(
        &mut plant,
        "pulse-blocked-in",
        "Pulse-cap refusal delivered to its alarm",
    );
    let staged = bank.out_int(
        &mut plant,
        "staged",
        "How many machines the group commands",
        0,
    );
    let transition = bank.out_bool(
        &mut plant,
        "transition",
        "A join, departure, or rotation handover is in progress",
        false,
    );
    let staging_pending = bank.out_bool(
        &mut plant,
        "staging-pending",
        "A stage change stands unexecuted under the declared authority",
        false,
    );
    let staging_pending_in = bank.deliver_bool(
        &mut plant,
        "staging-pending-in",
        "Staging-pending flag delivered to its alarm",
    );
    let none_available = bank.out_bool(
        &mut plant,
        "none-available",
        "No machine is available to the group",
        false,
    );
    let none_available_in = bank.deliver_bool(
        &mut plant,
        "none-available-in",
        "No-machine-available flag delivered to its alarm",
    );
    let all_faulted = bank.out_bool(
        &mut plant,
        "all-faulted",
        "Every machine's fault input reads failed",
        false,
    );
    let all_faulted_in = bank.deliver_bool(
        &mut plant,
        "all-faulted-in",
        "All-faulted flag delivered to its alarm",
    );
    let header_total = bank.out_float(
        &mut plant,
        "header-total",
        unit::SM3,
        "Totalized aggregate airflow — decision 68's header accounting",
        0.0,
    );
    let total_reset = bank.writable_bool(
        &mut plant,
        "total-reset",
        "Operator's shared reset for the train's airflow accounting",
        false,
    );
    let approve = bank.writable_bool(
        &mut plant,
        "approve",
        "Operator's release of the group's pending stage change",
        false,
    );

    let coordinator = plant.add(HeaderCoordinatorSpec::new(
        parameters([
            ("strategy", Value::Int(config.strategy)),
            ("pressure_hold", Value::Float(config.pressure_hold)),
            ("pressure_min", Value::Float(config.pressure_min)),
            ("pressure_max", Value::Float(config.pressure_max)),
            ("mov_band_lo", Value::Float(config.mov_band_lo)),
            ("mov_band_hi", Value::Float(config.mov_band_hi)),
            ("adjust_ticks", Value::Int(config.adjust_ticks)),
            ("min_total_airflow", Value::Float(config.min_total_airflow)),
            ("max_pulsing", Value::Int(config.max_pulsing)),
        ]),
        config.zones,
    ));
    plant.port_unit(coordinator.id, "pressure", unit::KPA);
    plant.port_unit(coordinator.id, "pressure_sp", unit::KPA);
    plant.port_unit(coordinator.id, "blower_demand", unit::SM3_PER_H);
    for parameter in ["pressure_hold", "pressure_min", "pressure_max"] {
        plant.param_unit(coordinator.id, parameter, unit::KPA);
    }
    plant.param_unit(coordinator.id, "min_total_airflow", unit::SM3_PER_H);
    for parameter in ["mov_band_lo", "mov_band_hi"] {
        plant.param_unit(coordinator.id, parameter, unit::PERCENT);
    }

    let slew = plant.add(RateLimiterSpec::new(parameters([(
        "max_delta",
        Value::Float(config.pressure_slew_kpa),
    )])));
    plant.port_unit(slew.id, "in", unit::KPA);
    plant.port_unit(slew.id, "out", unit::KPA);
    plant.param_unit(slew.id, "max_delta", unit::KPA);

    plant.connect(pressure, &coordinator.pressure);
    plant.connect(&coordinator.pressure_sp, pressure_sp);
    plant.connect(pressure_sp_in, pressure_sp);
    plant.connect(pressure_sp_in, &slew.input);
    plant.connect(&slew.out, pressure_sp_slew);
    plant.connect(&coordinator.blower_demand, blower_demand);
    plant.connect(blower_demand_in, blower_demand);
    plant.connect(&coordinator.most_open, most_open);
    plant.connect(&coordinator.at_bound, at_bound);
    plant.connect(at_bound_in, at_bound);
    plant.connect(&coordinator.pulse_blocked, pulse_blocked);
    plant.connect(pulse_blocked_in, pulse_blocked);

    let header_totalizer = plant.add(TotalizerSpec::new(parameters([
        ("rate_unit", Value::Float(1.0)),
        ("rollover", Value::Float(PARKED_LIMIT)),
    ])));
    plant.port_unit(header_totalizer.id, "rate", unit::SM3_PER_H);
    plant.port_unit(header_totalizer.id, "total", unit::SM3);
    plant.param_unit(header_totalizer.id, "rollover", unit::SM3);
    plant.connect(header_airflow, &header_totalizer.rate);
    plant.connect(total_reset, &header_totalizer.reset);
    plant.connect(&header_totalizer.total, header_total);

    // ----------------------------------------------------------------
    // The zones.
    // ----------------------------------------------------------------
    let mut zones = Vec::with_capacity(config.zones);
    for index in 0..config.zones {
        zones.push(wire_zone(
            &mut plant,
            config,
            index,
            ai,
            ao,
            &coordinator,
            influent_flow,
            total_reset,
            BANK_ALARMS + ZONE_ALARMS * index as u64,
        ));
    }

    // ----------------------------------------------------------------
    // The blower group and its machines.
    // ----------------------------------------------------------------
    // The `blower-group` parameter set is indexed by unit count, so the
    // shared keys come from the fixed array and the per-unit machine
    // bounds are inserted under their computed names.
    let mut group_parameters = parameters([
        ("staging_authority", Value::Int(config.staging_authority)),
        ("stage_up", Value::Float(config.stage_up)),
        ("stage_down", Value::Float(config.stage_down)),
        ("min_run_ticks", Value::Int(config.min_run_ticks)),
        (
            "min_start_interval_ticks",
            Value::Int(config.min_start_interval_ticks),
        ),
        ("vent_ticks", Value::Int(config.vent_ticks)),
        ("rotation", Value::Int(config.rotation)),
    ]);
    for index in 0..config.blowers {
        let unit = index + 1;
        group_parameters.insert(
            format!("unit_{unit}_min_flow"),
            Value::Float(config.unit_min_flow[index]),
        );
        group_parameters.insert(
            format!("unit_{unit}_max_flow"),
            Value::Float(config.unit_max_flow[index]),
        );
        group_parameters.insert(
            format!("unit_{unit}_max_current"),
            Value::Float(config.unit_max_current[index]),
        );
    }
    let group = plant.add(BlowerGroupSpec::new(
        group_parameters,
        config.blowers,
        config.staging_authority == 1,
    ));
    plant.port_unit(group.id, "demand", unit::SM3_PER_H);
    for index in 0..config.blowers {
        let unit = index + 1;
        plant.port_unit(group.id, &format!("capacity_{unit}"), unit::SM3_PER_H);
        plant.param_unit(group.id, &format!("unit_{unit}_min_flow"), unit::SM3_PER_H);
        plant.param_unit(group.id, &format!("unit_{unit}_max_flow"), unit::SM3_PER_H);
        plant.param_unit(
            group.id,
            &format!("unit_{unit}_max_current"),
            unit::SM3_PER_H,
        );
    }
    plant.connect(blower_demand_in, &group.demand);
    if let Some(port) = group.approve.as_ref() {
        plant.connect(approve, port);
    }
    plant.connect(&group.staged, staged);
    plant.connect(&group.transition, transition);
    plant.connect(&group.staging_pending, staging_pending);
    plant.connect(staging_pending_in, staging_pending);
    plant.connect(&group.none_available, none_available);
    plant.connect(none_available_in, none_available);
    plant.connect(&group.all_faulted, all_faulted);
    plant.connect(all_faulted_in, all_faulted);

    let mut blowers = Vec::with_capacity(config.blowers);
    let machine_alarm_base = BANK_ALARMS + ZONE_ALARMS * config.zones as u64;
    for index in 0..config.blowers {
        blowers.push(wire_blower(
            &mut plant,
            config,
            index,
            ai,
            di,
            d_o,
            ao,
            &group,
            pressure,
            machine_alarm_base + BLOWER_ALARMS * index as u64,
        ));
    }

    // ----------------------------------------------------------------
    // The bank's alarm set.
    // ----------------------------------------------------------------
    let pressure_alarm_instance = plant.add(ManagedLatchingAlarmSpec::new(
        parameters([
            ("low_limit", Value::Float(config.pressure_alarm_min)),
            ("high_limit", Value::Float(config.pressure_alarm_max)),
            ("hysteresis", Value::Float(config.hysteresis)),
            ("max_shelve_ticks", Value::Int(0)),
            ("priority", Value::Int(1)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(30)),
        ]),
        ManagedInputs::default(),
        rationalization(
            "Discharge-header pressure sits outside the declared bounds",
            "Restore the blowers to the header or reduce the zone demand the header is carrying",
            "header-pressure-alarm",
        ),
    ));
    plant.connect(pressure, &pressure_alarm_instance.input);
    plant.port_unit(pressure_alarm_instance.id, "in", unit::KPA);
    for parameter in ["low_limit", "high_limit", "hysteresis"] {
        plant.param_unit(pressure_alarm_instance.id, parameter, unit::KPA);
    }
    for parameter in ["max_shelve_ticks", "response_ticks"] {
        plant.param_unit(pressure_alarm_instance.id, parameter, unit::TICKS);
    }
    let pressure_alarm = managed_alarm(
        &mut plant,
        0,
        pressure_alarm_instance.id,
        &pressure_alarm_instance.ack,
        &pressure_alarm_instance.managed,
        &pressure_alarm_instance.alarm,
        &pressure_alarm_instance.unacknowledged,
        "header-pressure",
        "aeration-header",
        None,
    );

    let at_bound_alarm = bool_alarm(
        &mut plant,
        1,
        at_bound_in,
        "The coordinator has no headroom left to optimize",
        "Reduce the zone air demand or open the header's declared bounds",
        "coordinator-at-bound-alarm",
        1,
        "aeration-header",
        "at-bound",
        ManagedInputs::default(),
    );
    let pulse_blocked_alarm = bool_alarm(
        &mut plant,
        2,
        pulse_blocked_in,
        "A grid's mixing pulse is refused by the declared simultaneous-pulse cap",
        "Reduce the number of grids pulsing at once or raise the declared cap",
        "pulse-cap-alarm",
        2,
        "aeration-header",
        "pulse-blocked",
        ManagedInputs::default(),
    );
    let staging_pending_alarm = bool_alarm(
        &mut plant,
        3,
        staging_pending_in,
        "A blower stage change is waiting for the operator's release",
        "Release or refuse the pending stage change on the blower group",
        "staging-pending-alarm",
        2,
        "aeration-header",
        "staging-pending",
        ManagedInputs::default(),
    );
    let none_available_alarm = bool_alarm(
        &mut plant,
        4,
        none_available_in,
        "No blower is available to the header",
        "Restore availability on a machine or start the train on the machines that remain",
        "no-blower-available-alarm",
        1,
        "aeration-header",
        "none-available",
        ManagedInputs::default(),
    );
    let all_faulted_alarm = bool_alarm(
        &mut plant,
        5,
        all_faulted_in,
        "Every blower reads faulted",
        "Attend the reported machine faults before restarting the header",
        "all-blowers-faulted-alarm",
        1,
        "aeration-header",
        "all-faulted",
        ManagedInputs::default(),
    );

    let model = plant.build()?;
    Ok(AerationTrain {
        model,
        layout: AerationTrainLayout {
            pressure: points::PRESSURE,
            header_airflow: points::HEADER_AIRFLOW,
            influent_flow: points::INFLUENT_FLOW,
            total_reset: total_reset.into(),
            approve: approve.into(),
            pressure_sp: pressure_sp.into(),
            pressure_sp_slew: pressure_sp_slew.into(),
            blower_demand: blower_demand.into(),
            most_open: most_open.into(),
            at_bound: at_bound.into(),
            pulse_blocked: pulse_blocked.into(),
            staged: staged.into(),
            transition: transition.into(),
            staging_pending: staging_pending.into(),
            none_available: none_available.into(),
            all_faulted: all_faulted.into(),
            header_total: header_total.into(),
            coordinator: coordinator.id,
            group: group.id,
            slew: slew.id,
            zones,
            blowers,
            pressure_alarm,
            at_bound_alarm,
            pulse_blocked_alarm,
            staging_pending_alarm,
            none_available_alarm,
            all_faulted_alarm,
        },
    })
}

/// Registers point `point`'s monitoring signal — `SIGNAL_BASE + point` —
/// carrying the full unit/description/group metadata WW-FND-001 asks
/// the surface to render from.
///
/// A non-empty `unit` is declared once, on the point — the wiring
/// contract the connection and validation checks read — and the signal
/// inherits it at `build`. The empty `""` stays signal-side: the
/// deliberate "dimensionless" marker for the status, flag, and code
/// points the wiring layer leaves uncheckable.
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
    alarm_carrier: Option<&mut Option<OutPoint<bool>>>,
) -> AlarmLayout {
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

/// Declares a `managed-bool-latching-alarm` on `input` with its
/// rationalization record and writable ack, and wires its managed status
/// points.
#[allow(clippy::too_many_arguments)]
fn bool_alarm(
    plant: &mut PlantBuilder,
    index: u64,
    input: InPoint<bool>,
    consequence: &str,
    required_action: &str,
    reference: &str,
    priority: i64,
    group: &str,
    prefix: &str,
    managed: ManagedInputs,
) -> AlarmLayout {
    let instance = plant.add(ManagedBoolLatchingAlarmSpec::new(
        parameters([
            ("max_shelve_ticks", Value::Int(0)),
            ("priority", Value::Int(priority)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(60)),
        ]),
        managed,
        rationalization(consequence, required_action, reference),
    ));
    for parameter in ["max_shelve_ticks", "response_ticks"] {
        plant.param_unit(instance.id, parameter, unit::TICKS);
    }
    plant.connect(input, &instance.input);
    managed_alarm(
        plant,
        index,
        instance.id,
        &instance.ack,
        &instance.managed,
        &instance.alarm,
        &instance.unacknowledged,
        prefix,
        group,
        None,
    )
}

/// One block's internal carriers: a bump allocator over a reserved id
/// range that declares each carrier with its monitoring metadata as it
/// goes, so the offsets stay deterministic in declaration order without
/// a hand-maintained table.
struct Block {
    next: u64,
    end: u64,
    tag: String,
    group: String,
}

impl Block {
    /// A block over `base ..= base + capacity - 1`, tagged `tag` and
    /// filed under signal group `group`.
    fn new(base: u64, capacity: u64, tag: String, group: String) -> Self {
        Self {
            next: base,
            end: base + capacity,
            tag,
            group,
        }
    }

    fn reserve(&mut self, count: u64) -> u64 {
        assert!(
            self.next + count <= self.end,
            "{}'s carrier block overflows its declared capacity",
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
        let point = PointId(self.reserve(1));
        let handle = plant.internal_input::<bool>(point, false, false);
        let tag = self.tag.clone();
        let group = self.group.clone();
        let full = format!("{tag}-{name}");
        signal(plant, point, &full, "", description, &group);
        handle
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

    /// Declares a writable `Float` operator point. The write itself is
    /// the durable record — decision 74's `journaled` flag covers the
    /// declared `Bool`/`Int` lifecycle and mode points only, and the
    /// model's validation bound rejects a `Float` point carrying it, so
    /// a setpoint's history rides the per-point ring beside the settled
    /// receipt the write produces.
    fn writable_float(
        &mut self,
        plant: &mut PlantBuilder,
        name: &str,
        unit: &str,
        description: &str,
        initial: f64,
    ) -> InPoint<f64> {
        let point = PointId(self.reserve(1));
        let handle = plant.internal_input::<f64>(point, initial, true);
        let tag = self.tag.clone();
        let group = self.group.clone();
        let full = format!("{tag}-{name}");
        signal(plant, point, &full, unit, description, &group);
        handle
    }

    /// Declares a writable `Bool` operator point, journaled under
    /// decision 74 so the operator action lands beside its attributed
    /// receipt.
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
}

/// Wires zone `index` (`0`-based): the field points and their metadata,
/// the redundant-probe measurement chain, the DO feedback loop with its
/// declared feed-forward layer and DO-loss fallback, the operator
/// takeover, the air-valve actuation with its field command, the zone's
/// totalizer, the coordinator handshake, the per-grid mixing pulse, and
/// the six declared alarms.
#[allow(clippy::too_many_lines, clippy::too_many_arguments)]
fn wire_zone(
    plant: &mut PlantBuilder,
    config: &AerationTrainConfig,
    index: usize,
    ai: DeviceId,
    ao: DeviceId,
    coordinator: &HeaderCoordinatorInstance,
    influent_flow: InPoint<f64>,
    total_reset: InPoint<bool>,
    alarm_base: u64,
) -> ZoneLayout {
    let tag = format!("z1{:02}", index + 1);
    let group = format!("aeration-zone-{tag}");
    let mut b = Block::new(
        ZONE_BASE + ZONE_STRIDE * index as u64,
        ZONE_STRIDE,
        tag.clone(),
        group.clone(),
    );

    // The field-point declaring helpers for this zone's device channels.
    let in_f = |plant: &mut PlantBuilder, point: PointId, name: &str| {
        let channel = plant.channel::<f64>(ai, name, Direction::In);
        plant.field_input::<f64>(point, channel, false)
    };
    // ----------------------------------------------------------------
    // Field points.
    // ----------------------------------------------------------------
    let mut probes = Vec::with_capacity(config.probes);
    for probe in 0..config.probes {
        probes.push(in_f(
            plant,
            points::do_probe(index, probe),
            &format!("{tag}-do-{}", probe + 1),
        ));
    }
    let uptake = in_f(plant, points::uptake(index), &format!("{tag}-uptake"));
    let airflow = in_f(plant, points::airflow(index), &format!("{tag}-airflow"));
    in_f(plant, points::transfer(index), &format!("{tag}-transfer"));
    in_f(plant, points::drain(index), &format!("{tag}-drain"));
    for probe in 0..config.probes {
        in_f(
            plant,
            points::do_net(index, probe),
            &format!("{tag}-do-net-{}", probe + 1),
        );
    }
    let valve_cmd_channel = plant.channel::<f64>(ao, &format!("{tag}-valve-cmd"), Direction::Out);
    let valve_cmd = plant.field_output::<f64>(points::valve_cmd(index), valve_cmd_channel);
    let valve_pos = in_f(plant, points::valve_pos(index), &format!("{tag}-valve-pos"));
    // Decision 102's declared recording duty: the zone's compliance
    // series records every scan.
    plant.record(airflow, 1);
    for probe in probes.iter() {
        plant.record(*probe, 1);
    }
    // The simulated plant's loopback: the valve's position feedback
    // follows the commanded position, so a scripted write to the
    // feedback point is what proves a discrepancy.
    plant.connect(valve_pos, valve_cmd);

    for probe in 0..config.probes {
        signal(
            plant,
            points::do_probe(index, probe),
            &format!("{tag}-do-{}", probe + 1),
            unit::MG_PER_L,
            &format!("Dissolved-oxygen probe {}", probe + 1),
            &group,
        );
        signal(
            plant,
            points::do_net(index, probe),
            &format!("{tag}-do-net-{}", probe + 1),
            unit::MG_PER_L,
            &format!("Probe {}'s summed oxygen balance", probe + 1),
            &group,
        );
    }
    for (point, name, declared_unit, description) in [
        (
            points::uptake(index),
            "uptake",
            "mg/L/scan",
            "Declared oxygen-uptake forcing the DO response drains on",
        ),
        (
            points::airflow(index),
            "airflow",
            unit::SM3_PER_H,
            "Delivered zone airflow — the mixing floor and totalizer read",
        ),
        (
            points::transfer(index),
            "transfer",
            unit::MG_PER_L,
            "Oxygen-transfer contribution the probe balances read",
        ),
        (
            points::drain(index),
            "drain",
            unit::MG_PER_L,
            "Oxygen-uptake drain term the probe balances read",
        ),
        (
            points::valve_cmd(index),
            "valve-cmd",
            unit::PERCENT,
            "Zone air valve position command",
        ),
        (
            points::valve_pos(index),
            "valve-pos",
            unit::PERCENT,
            "Zone air valve position feedback — the coordinator's most-open input",
        ),
    ] {
        signal(
            plant,
            point,
            &format!("{tag}-{name}"),
            declared_unit,
            description,
            &group,
        );
    }

    // ----------------------------------------------------------------
    // Writable operator points.
    // ----------------------------------------------------------------
    let do_sp = b.writable_float(
        plant,
        "do-sp",
        unit::MG_PER_L,
        "Operator's dissolved-oxygen set-point for the zone loop",
        config.do_sp,
    );
    let air_per_flow = b.writable_float(
        plant,
        "air-per-flow",
        unit::SM3_PER_SM3,
        "Operator's proportional air-per-influent pacing ratio",
        config.air_per_flow,
    );
    let manual_mode = b.writable_bool(
        plant,
        "manual-mode",
        "Operator's takeover select — the zone's manual demand serves",
        false,
    );
    let manual_rate = b.writable_float(
        plant,
        "manual-rate",
        unit::SM3_PER_H,
        "Operator's manual zone airflow rate",
        config.safe_flow,
    );
    let pulse_start = b.writable_bool(
        plant,
        "pulse-start",
        "Operator's mixing-pulse start — the per-grid timer's request",
        false,
    );

    // ----------------------------------------------------------------
    // The measurement chain: 2oo3 voting, deadband, DO feedback loop.
    // ----------------------------------------------------------------
    let voter = plant.add(MedianVoterSpec::new(parameters([(
        "tolerance",
        Value::Float(config.do_spread),
    )])));
    plant.port_unit(voter.id, "in_1", unit::MG_PER_L);
    plant.port_unit(voter.id, "in_2", unit::MG_PER_L);
    plant.port_unit(voter.id, "in_3", unit::MG_PER_L);
    plant.port_unit(voter.id, "out", unit::MG_PER_L);
    plant.param_unit(voter.id, "tolerance", unit::MG_PER_L);
    plant.connect(probes[0], &voter.in_1);
    plant.connect(probes[1], &voter.in_2);
    plant.connect(probes[2], &voter.in_3);
    let do_selected = b.out_float(
        plant,
        "do-selected",
        unit::MG_PER_L,
        "The voted dissolved-oxygen measurement",
        config.do_sp,
    );
    let do_selected_in = b.deliver_float(
        plant,
        "do-selected-in",
        unit::MG_PER_L,
        "Voted measurement delivered to the deadband filter",
    );
    let do_discrepancy = b.out_bool(
        plant,
        "do-discrepancy",
        "The probe spread exceeds the declared tolerance",
        false,
    );
    let do_discrepancy_in = b.deliver_bool(
        plant,
        "do-discrepancy-in",
        "Probe spread delivered to its alarm",
    );
    plant.connect(&voter.out, do_selected);
    plant.connect(do_selected_in, do_selected);
    plant.connect(&voter.discrepancy, do_discrepancy);
    plant.connect(do_discrepancy_in, do_discrepancy);

    let filter = plant.add(SignalFilterSpec::new(parameters([(
        "alpha",
        Value::Float(config.do_alpha),
    )])));
    plant.port_unit(filter.id, "in", unit::MG_PER_L);
    plant.port_unit(filter.id, "out", unit::MG_PER_L);
    plant.param_unit(filter.id, "alpha", unit::FRACTION);
    plant.connect(do_selected_in, &filter.input);
    let do_filtered = b.out_float(
        plant,
        "do-filtered",
        unit::MG_PER_L,
        "The filtered DO measurement the loop and the fallback read",
        config.do_sp,
    );
    let do_filtered_pid = b.deliver_float(
        plant,
        "do-filtered-pid",
        unit::MG_PER_L,
        "Filtered measurement delivered to the DO loop",
    );
    let do_filtered_fb = b.deliver_float(
        plant,
        "do-filtered-fallback",
        unit::MG_PER_L,
        "Filtered measurement delivered to the DO-loss fallback",
    );
    let do_filtered_alarm = b.deliver_float(
        plant,
        "do-filtered-alarm",
        unit::MG_PER_L,
        "Filtered measurement delivered to the DO tier alarms",
    );
    plant.connect(&filter.out, do_filtered);
    plant.connect(do_filtered_pid, do_filtered);
    plant.connect(do_filtered_fb, do_filtered);
    plant.connect(do_filtered_alarm, do_filtered);

    let pid = plant.add(PidSpec::new(parameters([
        ("kp", Value::Float(config.pid_kp)),
        ("dt", Value::Float(config.pid_dt)),
        ("out_min", Value::Float(config.trim_min)),
        ("out_max", Value::Float(config.trim_max)),
    ])));
    plant.port_unit(pid.id, "sp", unit::MG_PER_L);
    plant.port_unit(pid.id, "pv", unit::MG_PER_L);
    plant.port_unit(pid.id, "out", unit::SM3_PER_H);
    plant.param_unit(pid.id, "kp", unit::SM3_PER_H);
    plant.param_unit(pid.id, "out_min", unit::SM3_PER_H);
    plant.param_unit(pid.id, "out_max", unit::SM3_PER_H);
    plant.connect(do_sp, &pid.sp);
    plant.connect(do_filtered_pid, &pid.pv);
    let pid_trim = b.out_float(
        plant,
        "pid-trim",
        unit::SM3_PER_H,
        "The DO loop's additive airflow correction",
        0.0,
    );
    let pid_trim_in = b.deliver_float(
        plant,
        "pid-trim-in",
        unit::SM3_PER_H,
        "The DO loop's correction delivered to the demand path",
    );
    plant.connect(&pid.out, pid_trim);
    plant.connect(pid_trim_in, pid_trim);

    // ----------------------------------------------------------------
    // The demand path: the declared feed-forward layer and the DO-loss
    // fallback.
    // ----------------------------------------------------------------
    let clamped = b.out_bool(
        plant,
        "clamped",
        "A declared trim-authority or demand bound is engaged",
        false,
    );
    let ratio_clamped = b.out_bool(
        plant,
        "ratio-clamped",
        "A declared dose or rate bound on the paced term is engaged",
        false,
    );
    let ratio_fallback = b.out_bool(
        plant,
        "ratio-fallback",
        "The paced term's declared bad-influent response is running",
        false,
    );
    let ff_fallback = b.out_bool(
        plant,
        "ff-fallback",
        "A declared bad-term response on the additive sum is running",
        false,
    );
    let zone_demand = b.out_float(
        plant,
        "zone-demand",
        unit::SM3_PER_H,
        "The summed zone demand before the DO-loss fallback",
        0.0,
    );
    let zone_demand_fb = b.deliver_float(
        plant,
        "zone-demand-fallback",
        unit::SM3_PER_H,
        "Summed demand delivered to the DO-loss fallback",
    );

    if config.feedforward {
        let ratio = plant.add(FlowPacedRatioSpec::new(
            parameters([
                ("min_dose", Value::Float(0.0)),
                ("max_dose", Value::Float(config.max_demand / 1000.0)),
                ("min_rate", Value::Float(0.0)),
                ("max_rate", Value::Float(config.max_demand)),
                ("on_bad_flow", Value::Int(2)),
                ("fallback_rate", Value::Float(config.safe_flow)),
                ("on_bad_trim", Value::Int(0)),
            ]),
            false,
        ));
        plant.port_unit(ratio.id, "flow", unit::SM3_PER_H);
        plant.port_unit(ratio.id, "dose", unit::SM3_PER_SM3);
        plant.port_unit(ratio.id, "demand", unit::SM3_PER_H);
        plant.param_unit(ratio.id, "min_dose", unit::SM3_PER_SM3);
        plant.param_unit(ratio.id, "max_dose", unit::SM3_PER_SM3);
        plant.param_unit(ratio.id, "min_rate", unit::SM3_PER_H);
        plant.param_unit(ratio.id, "max_rate", unit::SM3_PER_H);
        plant.param_unit(ratio.id, "fallback_rate", unit::SM3_PER_H);
        // The bank-level influent flow feeds every zone's pacing engine:
        // the one measurement the proportional law shares.
        plant.connect(influent_flow, &ratio.flow);
        plant.connect(air_per_flow, &ratio.dose);
        let ff = plant.add(FeedforwardSumSpec::new(parameters([
            ("trim_min", Value::Float(config.trim_min)),
            ("trim_max", Value::Float(config.trim_max)),
            ("min_demand", Value::Float(config.min_demand)),
            ("max_demand", Value::Float(config.max_demand)),
            ("on_bad_ff", Value::Int(config.on_bad_ff)),
            ("on_bad_trim", Value::Int(config.on_bad_trim)),
        ])));
        plant.port_unit(ff.id, "ff", unit::SM3_PER_H);
        plant.port_unit(ff.id, "trim", unit::SM3_PER_H);
        plant.port_unit(ff.id, "out", unit::SM3_PER_H);
        for parameter in ["trim_min", "trim_max", "min_demand", "max_demand"] {
            plant.param_unit(ff.id, parameter, unit::SM3_PER_H);
        }
        plant.connect(&ratio.clamped, ratio_clamped);
        plant.connect(&ratio.fallback_active, ratio_fallback);
        plant.connect(&ff.fallback_active, ff_fallback);
        plant.connect(&ratio.demand, &ff.ff);
        plant.connect(pid_trim_in, &ff.trim);
        plant.connect(&ff.out, zone_demand);
        plant.connect(&ff.clamped, clamped);
    } else {
        // The DO-feedback-only zone the decision records as equally
        // expressible: the loop's own output is the whole demand, and no
        // bounds are declared on it, so every status flag stays clear.
        let clear = b.deliver_bool(
            plant,
            "status-clear",
            "A zone declaring no paced term or bounds reports no clamp and no fallback",
        );
        plant.connect(pid_trim_in, zone_demand);
        for carrier in [clamped, ratio_clamped, ratio_fallback, ff_fallback] {
            let _ = carrier;
        }
        plant.connect(clear, clamped);
        plant.connect(clear, ratio_clamped);
        plant.connect(clear, ratio_fallback);
        plant.connect(clear, ff_fallback);
    }
    plant.connect(zone_demand_fb, zone_demand);

    let fallback = plant.add(DemandFallbackSpec::new(parameters([
        ("on_bad", Value::Int(config.on_bad)),
        ("fallback_flow", Value::Float(config.fallback_flow)),
        ("safe_flow", Value::Float(config.safe_flow)),
    ])));
    plant.port_unit(fallback.id, "in", unit::SM3_PER_H);
    plant.port_unit(fallback.id, "pv", unit::MG_PER_L);
    plant.port_unit(fallback.id, "out", unit::SM3_PER_H);
    plant.param_unit(fallback.id, "fallback_flow", unit::SM3_PER_H);
    plant.param_unit(fallback.id, "safe_flow", unit::SM3_PER_H);
    plant.connect(zone_demand_fb, &fallback.input);
    plant.connect(do_filtered_fb, &fallback.pv);
    let served = b.out_float(
        plant,
        "served",
        unit::SM3_PER_H,
        "The demand the zone's DO-loss response serves",
        0.0,
    );
    let served_out = b.deliver_float(
        plant,
        "served-out",
        unit::SM3_PER_H,
        "Served demand delivered to the operator takeover",
    );
    let fallback_active = b.out_bool(
        plant,
        "fallback-active",
        "The declared DO-loss response is engaged",
        false,
    );
    let fallback_active_in = b.deliver_bool(
        plant,
        "fallback-active-in",
        "Fallback engagement delivered to its alarm",
    );
    plant.connect(&fallback.out, served);
    plant.connect(served_out, served);
    plant.connect(&fallback.fallback_active, fallback_active);
    plant.connect(fallback_active_in, fallback_active);

    let manual = plant.add(ManualStationSpec::new(parameters([(
        "transfer_delta",
        Value::Float(50.0),
    )])));
    plant.port_unit(manual.id, "control", unit::SM3_PER_H);
    plant.port_unit(manual.id, "manual", unit::SM3_PER_H);
    plant.port_unit(manual.id, "out", unit::SM3_PER_H);
    plant.param_unit(manual.id, "transfer_delta", unit::SM3_PER_H);
    plant.connect(served_out, &manual.control);
    plant.connect(manual_rate, &manual.manual);
    plant.connect(manual_mode, &manual.mode);
    let manual_out = b.out_float(
        plant,
        "manual-out",
        unit::SM3_PER_H,
        "The zone's commanded airflow after the operator takeover",
        0.0,
    );
    let manual_out_in = b.deliver_float(
        plant,
        "manual-out-in",
        unit::SM3_PER_H,
        "The zone's commanded airflow, delivered to the valve's engineering scaling and the coordinator's zone input",
    );
    let manual_active = b.out_bool(
        plant,
        "manual-active",
        "The operator's takeover is engaged",
        false,
    );
    let manual_active_in = b.deliver_bool(
        plant,
        "manual-active-in",
        "Takeover engagement delivered to its alarm",
    );
    plant.connect(&manual.out, manual_out);
    plant.connect(manual_out_in, manual_out);
    plant.connect(&manual.manual_active, manual_active);
    plant.connect(manual_active_in, manual_active);

    // ----------------------------------------------------------------
    // The air valve: the engineering demand scaled onto the field
    // command, then echoed and verified.
    // ----------------------------------------------------------------
    let analog = plant.add(AnalogOutputSpec::<f64>::new(parameters([
        ("raw_min", Value::Float(0.0)),
        ("raw_max", Value::Float(100.0)),
        ("eng_min", Value::Float(0.0)),
        ("eng_max", Value::Float(config.max_demand)),
    ])));
    plant.port_unit(analog.id, "eng", unit::SM3_PER_H);
    plant.port_unit(analog.id, "raw", unit::PERCENT);
    plant.param_unit(analog.id, "eng_min", unit::SM3_PER_H);
    plant.param_unit(analog.id, "eng_max", unit::SM3_PER_H);
    plant.param_unit(analog.id, "raw_min", unit::PERCENT);
    plant.param_unit(analog.id, "raw_max", unit::PERCENT);
    plant.connect(manual_out_in, &analog.eng);
    // The header coordinates on what the zone actually asks for, so an
    // operator takeover is visible to the coordinator as the changed
    // zone demand it is.
    plant.connect(manual_out_in, coordinator.airflow(index + 1));

    let valve = plant.add(ValveSpec::new(parameters([
        ("tolerance", Value::Float(config.valve_tolerance_pct)),
        (
            "discrepancy_ticks",
            Value::Int(config.valve_discrepancy_ticks),
        ),
    ])));
    plant.port_unit(valve.id, "cmd", unit::PERCENT);
    plant.port_unit(valve.id, "out", unit::PERCENT);
    plant.port_unit(valve.id, "fb", unit::PERCENT);
    plant.param_unit(valve.id, "tolerance", unit::PERCENT);
    plant.connect(&analog.raw, &valve.cmd);
    plant.connect(valve_pos, &valve.fb);
    plant.connect(&valve.out, valve_cmd);
    let valve_discrepancy = b.out_bool(
        plant,
        "valve-discrepancy",
        "The commanded and proven valve positions disagree",
        false,
    );
    plant.connect(&valve.discrepancy, valve_discrepancy);

    // ----------------------------------------------------------------
    // The zone's totalized delivered airflow — decision 68's
    // control-domain accounting share.
    // ----------------------------------------------------------------
    let totalizer = plant.add(TotalizerSpec::new(parameters([
        ("rate_unit", Value::Float(1.0)),
        ("rollover", Value::Float(PARKED_LIMIT)),
    ])));
    plant.port_unit(totalizer.id, "rate", unit::SM3_PER_H);
    plant.port_unit(totalizer.id, "total", unit::SM3);
    plant.param_unit(totalizer.id, "rollover", unit::SM3);
    plant.connect(airflow, &totalizer.rate);
    plant.connect(total_reset, &totalizer.reset);
    let zone_total = b.out_float(
        plant,
        "zone-total",
        unit::SM3,
        "Totalized delivered airflow for the zone",
        0.0,
    );
    plant.connect(&totalizer.total, zone_total);

    // ----------------------------------------------------------------
    // The coordinator handshake and the per-grid mixing pulse.
    // ----------------------------------------------------------------
    plant.connect(valve_pos, coordinator.valve_pos(index + 1));

    let pulse_timer = plant.add(TimerSpec::new(parameters([
        ("delay_ticks", Value::Int(config.pulse_ticks)),
        ("off_delay", Value::Bool(false)),
    ])));
    plant.connect(pulse_start, &pulse_timer.input);
    let pulsing = b.out_bool(plant, "pulsing", "The grid's mixing-pulse request", false);
    let pulsing_in = b.deliver_bool(
        plant,
        "pulsing-in",
        "Pulse request delivered to the coordinator",
    );
    plant.connect(&pulse_timer.out, pulsing);
    plant.connect(pulsing_in, pulsing);
    if config.pulsed_mixing {
        plant.connect(pulsing_in, coordinator.pulsing(index + 1));
    }
    let pulse_grant = b.out_bool(
        plant,
        "pulse-grant",
        "The coordinator's pulse admission for this zone",
        false,
    );
    plant.connect(coordinator.pulse_grant(index + 1), pulse_grant);

    // ----------------------------------------------------------------
    // The zone's six declared alarms.
    // ----------------------------------------------------------------
    let do_low_instance = plant.add(ManagedLatchingAlarmSpec::new(
        parameters([
            ("low_limit", Value::Float(config.do_low)),
            ("high_limit", Value::Float(PARKED_LIMIT)),
            ("hysteresis", Value::Float(config.hysteresis)),
            ("max_shelve_ticks", Value::Int(0)),
            ("priority", Value::Int(1)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(120)),
        ]),
        ManagedInputs::default(),
        rationalization(
            "The zone's dissolved oxygen fell below the declared tier — the permit-relevant direction",
            "Restore the zone's air supply or reduce the oxygen load the zone is carrying",
            "zone-do-low-alarm",
        ),
    ));
    plant.connect(do_filtered_alarm, &do_low_instance.input);
    plant.port_unit(do_low_instance.id, "in", unit::MG_PER_L);
    for parameter in ["low_limit", "high_limit", "hysteresis"] {
        plant.param_unit(do_low_instance.id, parameter, unit::MG_PER_L);
    }
    for parameter in ["max_shelve_ticks", "response_ticks"] {
        plant.param_unit(do_low_instance.id, parameter, unit::TICKS);
    }
    let do_low_alarm = managed_alarm(
        plant,
        alarm_base,
        do_low_instance.id,
        &do_low_instance.ack,
        &do_low_instance.managed,
        &do_low_instance.alarm,
        &do_low_instance.unacknowledged,
        "do-low",
        &group,
        None,
    );

    let do_high_instance = plant.add(ManagedLatchingAlarmSpec::new(
        parameters([
            ("low_limit", Value::Float(-PARKED_LIMIT)),
            ("high_limit", Value::Float(config.do_high)),
            ("hysteresis", Value::Float(config.hysteresis)),
            ("max_shelve_ticks", Value::Int(0)),
            ("priority", Value::Int(2)),
            ("class", Value::Int(2)),
            ("response_ticks", Value::Int(180)),
        ]),
        ManagedInputs::default(),
        rationalization(
            "The zone's dissolved oxygen rose past the declared tier — air is being wasted or the measurement has drifted",
            "Check the zone's DO probe and reduce the air the zone is delivering",
            "zone-do-high-alarm",
        ),
    ));
    plant.connect(do_filtered_alarm, &do_high_instance.input);
    plant.port_unit(do_high_instance.id, "in", unit::MG_PER_L);
    for parameter in ["low_limit", "high_limit", "hysteresis"] {
        plant.param_unit(do_high_instance.id, parameter, unit::MG_PER_L);
    }
    for parameter in ["max_shelve_ticks", "response_ticks"] {
        plant.param_unit(do_high_instance.id, parameter, unit::TICKS);
    }
    let do_high_alarm = managed_alarm(
        plant,
        alarm_base + 1,
        do_high_instance.id,
        &do_high_instance.ack,
        &do_high_instance.managed,
        &do_high_instance.alarm,
        &do_high_instance.unacknowledged,
        "do-high",
        &group,
        None,
    );

    let airflow_instance = plant.add(ManagedLatchingAlarmSpec::new(
        parameters([
            ("low_limit", Value::Float(config.airflow_floor)),
            ("high_limit", Value::Float(PARKED_LIMIT)),
            ("hysteresis", Value::Float(config.airflow_floor / 10.0)),
            (
                "max_shelve_ticks",
                Value::Int(config.airflow_alarm_max_shelve_ticks),
            ),
            ("priority", Value::Int(2)),
            ("class", Value::Int(1)),
            ("response_ticks", Value::Int(60)),
        ]),
        ManagedInputs {
            shelve: true,
            oos: false,
            suppress: false,
        },
        rationalization(
            "The zone's delivered airflow fell below the declared mixing floor",
            "Restore the zone's air supply or raise the declared floor for a genuinely lower design load",
            "zone-airflow-floor-alarm",
        ),
    ));
    plant.connect(airflow, &airflow_instance.input);
    plant.port_unit(airflow_instance.id, "in", unit::SM3_PER_H);
    for parameter in ["low_limit", "hysteresis"] {
        plant.param_unit(airflow_instance.id, parameter, unit::SM3_PER_H);
    }
    for parameter in ["max_shelve_ticks", "response_ticks"] {
        plant.param_unit(airflow_instance.id, parameter, unit::TICKS);
    }
    let airflow_alarm = managed_alarm(
        plant,
        alarm_base + 2,
        airflow_instance.id,
        &airflow_instance.ack,
        &airflow_instance.managed,
        &airflow_instance.alarm,
        &airflow_instance.unacknowledged,
        "airflow-floor",
        &group,
        None,
    );

    let do_discrepancy_alarm = bool_alarm(
        plant,
        alarm_base + 3,
        do_discrepancy_in,
        "The zone's DO probe spread exceeds the declared tolerance",
        "Check or replace the disagreeing DO probe",
        "do-discrepancy-alarm",
        3,
        &group,
        "do-discrepancy",
        ManagedInputs::default(),
    );
    let fallback_alarm = bool_alarm(
        plant,
        alarm_base + 4,
        fallback_active_in,
        "The zone's declared DO-loss response is engaged",
        "Restore the zone's DO measurement or accept the declared safe airflow",
        "do-fallback-alarm",
        1,
        &group,
        "do-fallback",
        ManagedInputs::default(),
    );
    let manual_alarm = bool_alarm(
        plant,
        alarm_base + 5,
        manual_active_in,
        "The zone's air demand is under operator takeover",
        "Return the zone to automatic control or confirm the manual rate is intended",
        "zone-manual-alarm",
        3,
        &group,
        "zone-manual",
        ManagedInputs::default(),
    );

    ZoneLayout {
        index: index + 1,
        probes: probes.iter().map(|point| (*point).into()).collect(),
        uptake: uptake.into(),
        airflow: airflow.into(),
        valve_cmd: valve_cmd.into(),
        valve_pos: valve_pos.into(),
        do_sp: do_sp.into(),
        air_per_flow: air_per_flow.into(),
        manual_mode: manual_mode.into(),
        manual_rate: manual_rate.into(),
        pulse_start: pulse_start.into(),
        do_selected: do_selected.into(),
        do_discrepancy: do_discrepancy.into(),
        do_filtered: do_filtered.into(),
        pid_trim: pid_trim.into(),
        clamped: clamped.into(),
        ratio_clamped: ratio_clamped.into(),
        ratio_fallback: ratio_fallback.into(),
        ff_fallback: ff_fallback.into(),
        served: served.into(),
        commanded: manual_out.into(),
        fallback_active: fallback_active.into(),
        manual_active: manual_active.into(),
        valve_discrepancy: valve_discrepancy.into(),
        zone_total: zone_total.into(),
        pulse_grant: pulse_grant.into(),
        pulsing: pulsing.into(),
        do_low_alarm,
        do_high_alarm,
        airflow_alarm,
        do_discrepancy_alarm,
        fallback_alarm,
        manual_alarm,
    }
}

/// Wires machine `index` (`0`-based): the field points and their
/// metadata, the group's handshake, the decision-64 `surge-guard` on the
/// demand path, the decision-67 `motor` + `analog-output` actuation, the
/// protective-status family's availability and fault aggregation, and the
/// two declared alarms.
#[allow(clippy::too_many_lines, clippy::too_many_arguments)]
fn wire_blower(
    plant: &mut PlantBuilder,
    config: &AerationTrainConfig,
    index: usize,
    ai: DeviceId,
    di: DeviceId,
    d_o: DeviceId,
    ao: DeviceId,
    group: &BlowerGroupInstance,
    header_pressure: InPoint<f64>,
    alarm_base: u64,
) -> BlowerLayout {
    let tag = format!("b1{:02}", index + 1);
    let signal_group = format!("aeration-blower-{tag}");
    let mut b = Block::new(
        BLOWER_BASE + BLOWER_STRIDE * index as u64,
        BLOWER_STRIDE,
        tag.clone(),
        signal_group.clone(),
    );

    // ----------------------------------------------------------------
    // Field points.
    // ----------------------------------------------------------------
    let in_f = |plant: &mut PlantBuilder, point: PointId, name: &str| {
        let channel = plant.channel::<f64>(ai, name, Direction::In);
        plant.field_input::<f64>(point, channel, false)
    };
    let in_b = |plant: &mut PlantBuilder, point: PointId, name: &str| {
        let channel = plant.channel::<bool>(di, name, Direction::In);
        plant.field_input::<bool>(point, channel, false)
    };

    let flow = in_f(plant, points::flow(index), &format!("{tag}-flow"));
    let current = in_f(plant, points::current(index), &format!("{tag}-current"));
    in_f(
        plant,
        points::flow_demand(index),
        &format!("{tag}-flow-demand"),
    );
    let speed_cmd_channel = plant.channel::<f64>(ao, &format!("{tag}-speed-cmd"), Direction::Out);
    let speed_cmd = plant.field_output::<f64>(points::speed_cmd(index), speed_cmd_channel);
    let run_fb = in_b(plant, points::run_fb(index), &format!("{tag}-run-fb"));
    let avail = in_b(plant, points::avail(index), &format!("{tag}-avail"));
    let bearing = in_b(plant, points::bearing(index), &format!("{tag}-bearing"));
    let vibration = in_b(plant, points::vibration(index), &format!("{tag}-vibration"));
    let overload = in_b(plant, points::overload(index), &format!("{tag}-overload"));
    let oil = in_b(plant, points::oil(index), &format!("{tag}-oil"));
    let surge_trip = in_b(
        plant,
        points::surge_trip(index),
        &format!("{tag}-surge-trip"),
    );
    let override_state = in_b(
        plant,
        points::override_state(index),
        &format!("{tag}-override"),
    );
    let run_cmd_channel = plant.channel::<bool>(d_o, &format!("{tag}-run-cmd"), Direction::Out);
    let run_cmd = plant.field_output::<bool>(points::run_cmd(index), run_cmd_channel);
    let vent_cmd_channel = plant.channel::<bool>(d_o, &format!("{tag}-vent-cmd"), Direction::Out);
    let vent_cmd = plant.field_output::<bool>(points::vent_cmd(index), vent_cmd_channel);
    let vent_fb = in_b(plant, points::vent_fb(index), &format!("{tag}-vent-fb"));
    // Decision 102's declared recording duty: the machine's airflow
    // series records every scan.
    plant.record(flow, 1);
    // Decision 77's protection-layer report: the hardwired protective
    // family and the machine's declared availability are plant-side
    // statuses, so their transitions land in the durable record.
    for point in [
        bearing,
        vibration,
        overload,
        oil,
        surge_trip,
        override_state,
    ] {
        plant.journaled(point);
    }
    plant.journaled(avail);
    // The simulated plant's loopbacks: the machine's run and vent
    // feedback follow the commands.
    plant.connect(run_fb, run_cmd);
    plant.connect(vent_fb, vent_cmd);

    for (point, name, declared_unit, description) in [
        (
            points::flow_demand(index),
            "flow-demand",
            unit::SM3_PER_H,
            "Scaled discharge-airflow demand the machine's response carries",
        ),
        (
            points::flow(index),
            "flow",
            unit::SM3_PER_H,
            "Measured discharge airflow — the surge guard's flow and the header sum read",
        ),
        (
            points::current(index),
            "current",
            unit::AMPERES,
            "Motor current — the surge guard's minimum-amperage proxy",
        ),
        (
            points::speed_cmd(index),
            "speed-cmd",
            unit::PERCENT,
            "Machine capacity command",
        ),
        (points::run_fb(index), "run-fb", "", "Machine run feedback"),
        (
            points::avail(index),
            "avail",
            "",
            "Declared machine availability",
        ),
        (
            points::bearing(index),
            "bearing",
            "",
            "Hardwired bearing over-temperature report",
        ),
        (
            points::vibration(index),
            "vibration",
            "",
            "Hardwired vibration-level report",
        ),
        (
            points::overload(index),
            "overload",
            "",
            "Hardwired motor-overload report",
        ),
        (points::oil(index), "oil", "", "Hardwired oil-system report"),
        (
            points::surge_trip(index),
            "surge-trip",
            "",
            "Hardwired proven-surge shutdown the demand path honors",
        ),
        (
            points::override_state(index),
            "override",
            "",
            "Machine startup/shutdown override state",
        ),
        (points::run_cmd(index), "run-cmd", "", "Machine run command"),
        (
            points::vent_cmd(index),
            "vent-cmd",
            "",
            "Unloading/vent valve command",
        ),
        (
            points::vent_fb(index),
            "vent-fb",
            "",
            "Unloading/vent valve open contact",
        ),
    ] {
        signal(
            plant,
            point,
            &format!("{tag}-{name}"),
            declared_unit,
            description,
            &signal_group,
        );
    }

    // ----------------------------------------------------------------
    // The group's handshake and the operator's staging release.
    // ----------------------------------------------------------------
    let group_cmd = b.out_bool(
        plant,
        "group-cmd",
        "The group's run request for this machine",
        false,
    );
    let group_cmd_in = b.deliver_bool(
        plant,
        "group-cmd-in",
        "Run request delivered to the machine's motor",
    );
    let capacity = b.out_float(
        plant,
        "capacity",
        unit::SM3_PER_H,
        "The group's split capacity demand inside the unit's bounds",
        0.0,
    );
    let capacity_in = b.deliver_float(
        plant,
        "capacity-in",
        unit::SM3_PER_H,
        "Split demand delivered to the surge guard",
    );
    let group_avail = b.out_bool(
        plant,
        "group-avail",
        "The machine's availability as the group reads it",
        true,
    );
    let group_avail_in = b.deliver_bool(
        plant,
        "group-avail-in",
        "Availability delivered to the group's port family",
    );
    let group_fault = b.out_bool(
        plant,
        "group-fault",
        "The machine's fault as the group reads it",
        false,
    );
    let group_fault_in = b.deliver_bool(
        plant,
        "group-fault-in",
        "Fault delivered to the group's port family",
    );
    plant.connect(group.cmd(index + 1), group_cmd);
    plant.connect(group_cmd_in, group_cmd);
    plant.connect(group.capacity(index + 1), capacity);
    plant.connect(capacity_in, capacity);
    plant.connect(group.vent(index + 1), vent_cmd);
    plant.connect(run_fb, group.run(index + 1));

    // ----------------------------------------------------------------
    // The decision-64 surge guard between the split demand and the
    // machine's output.
    // ----------------------------------------------------------------
    let guard = plant.add(SurgeGuardSpec::new(
        parameters([
            ("min_flow", Value::Float(config.surge_min_flow)),
            ("max_pressure", Value::Float(config.surge_max_pressure)),
            ("min_current", Value::Float(config.surge_min_current)),
            ("on_guard", Value::Int(config.surge_on_guard)),
            ("trip_value", Value::Float(config.surge_trip_value)),
        ]),
        true,
    ));
    plant.port_unit(guard.id, "demand", unit::SM3_PER_H);
    plant.port_unit(guard.id, "flow", unit::SM3_PER_H);
    plant.port_unit(guard.id, "pressure", unit::KPA);
    plant.port_unit(guard.id, "current", unit::AMPERES);
    plant.port_unit(guard.id, "out", unit::SM3_PER_H);
    plant.param_unit(guard.id, "min_flow", unit::SM3_PER_H);
    plant.param_unit(guard.id, "max_pressure", unit::KPA);
    plant.param_unit(guard.id, "min_current", unit::AMPERES);
    plant.param_unit(guard.id, "trip_value", unit::SM3_PER_H);
    plant.connect(capacity_in, &guard.demand);
    plant.connect(flow, &guard.flow);
    plant.connect(header_pressure, &guard.pressure);
    plant.connect(
        current,
        guard
            .current
            .as_ref()
            .expect("the reference declares the current port"),
    );
    plant.connect(surge_trip, &guard.surge_trip);
    let guarded = b.out_float(
        plant,
        "guarded",
        unit::SM3_PER_H,
        "The surge-guarded demand the machine's output carries",
        0.0,
    );
    let guarded_in = b.deliver_float(
        plant,
        "guarded-in",
        unit::SM3_PER_H,
        "Guarded demand delivered to the machine's engineering scaling",
    );
    let guarding = b.out_bool(
        plant,
        "guarding",
        "The machine sits inside its declared surge region",
        false,
    );
    let tripped = b.out_bool(
        plant,
        "tripped",
        "A proven surge or declared trip stands",
        false,
    );
    let tripped_in = b.deliver_bool(plant, "tripped-in", "Proven trip delivered to its alarm");
    plant.connect(&guard.out, guarded);
    plant.connect(guarded_in, guarded);
    plant.connect(&guard.guarding, guarding);
    plant.connect(&guard.tripped, tripped);
    plant.connect(tripped_in, tripped);

    // ----------------------------------------------------------------
    // The decision-67 `motor` + `analog-output` actuation, gated on the
    // group's proven run request the way the dosing skid gates its
    // metering pumps.
    // ----------------------------------------------------------------
    let run_gate = plant.add(InterlockSpec::new(
        parameters([("safe_value", Value::Float(0.0))]),
        0,
    ));
    plant.port_unit(run_gate.id, "in", unit::SM3_PER_H);
    plant.port_unit(run_gate.id, "out", unit::SM3_PER_H);
    plant.param_unit(run_gate.id, "safe_value", unit::SM3_PER_H);
    let run_gated = b.out_bool(
        plant,
        "run-gated",
        "The run gate withholds the capacity demand from an unproven machine",
        false,
    );
    plant.connect(group_cmd_in, &run_gate.permissive);
    plant.connect(guarded_in, &run_gate.input);
    plant.connect(&run_gate.tripped, run_gated);

    let motor = plant.add(MotorSpec::new(parameters([(
        "fault_ticks",
        Value::Int(config.motor_fault_ticks),
    )])));
    plant.connect(group_cmd_in, &motor.cmd);
    plant.connect(run_fb, &motor.run);
    plant.connect(&motor.out, run_cmd);
    let motor_fault = b.out_bool(
        plant,
        "motor-fault",
        "The motor's proven command-versus-feedback fault",
        false,
    );
    let motor_fault_in = b.deliver_bool(
        plant,
        "motor-fault-in",
        "Proven motor fault delivered to the machine's fault aggregate",
    );
    plant.connect(&motor.fault, motor_fault);
    plant.connect(motor_fault_in, motor_fault);

    let analog = plant.add(AnalogOutputSpec::<f64>::new(parameters([
        ("raw_min", Value::Float(0.0)),
        ("raw_max", Value::Float(100.0)),
        ("eng_min", Value::Float(0.0)),
        ("eng_max", Value::Float(config.max_demand)),
    ])));
    plant.port_unit(analog.id, "eng", unit::SM3_PER_H);
    plant.port_unit(analog.id, "raw", unit::PERCENT);
    plant.param_unit(analog.id, "eng_min", unit::SM3_PER_H);
    plant.param_unit(analog.id, "eng_max", unit::SM3_PER_H);
    plant.param_unit(analog.id, "raw_min", unit::PERCENT);
    plant.param_unit(analog.id, "raw_max", unit::PERCENT);
    plant.connect(&run_gate.out, &analog.eng);
    plant.connect(&analog.raw, speed_cmd);

    // ----------------------------------------------------------------
    // The protective-status family's availability and fault
    // aggregation — the hardwired reports honored, never re-implemented.
    // ----------------------------------------------------------------
    let protective_gate = plant.add(BoolGateSpec::new(
        parameters([("operation", Value::Int(GATE_OR))]),
        6,
    ));
    for (offset, source) in [
        bearing,
        vibration,
        overload,
        oil,
        surge_trip,
        override_state,
    ]
    .into_iter()
    .enumerate()
    {
        plant.connect(source, protective_gate.input(offset + 1));
    }
    let protective = b.out_bool(
        plant,
        "protective",
        "A hardwired protective device reports a trip",
        false,
    );
    let protective_in = b.deliver_bool(
        plant,
        "protective-in",
        "Protective aggregate delivered to its alarm",
    );
    plant.connect(&protective_gate.out, protective);
    plant.connect(protective_in, protective);

    // The availability leg: the machine is available while its declared
    // availability stands and no protective device reports — the
    // decision's `avail_i` aggregation.
    let protective_inv = plant.add(DigitalInputSpec::new(parameters([(
        "invert",
        Value::Bool(true),
    )])));
    let protective_ok = b.out_bool(
        plant,
        "protective-ok",
        "No hardwired protective device reports",
        true,
    );
    let protective_in_inv = b.deliver_bool(
        plant,
        "protective-in-invert",
        "Protective aggregate delivered to its inversion",
    );
    plant.connect(protective_in_inv, protective);
    plant.connect(protective_in_inv, &protective_inv.input);
    plant.connect(&protective_inv.out, protective_ok);
    let protective_ok_in = b.deliver_bool(
        plant,
        "protective-ok-in",
        "Protective-clear leg delivered to the availability gate",
    );
    plant.connect(protective_ok_in, protective_ok);
    let avail_gate = plant.add(BoolGateSpec::new(
        parameters([("operation", Value::Int(GATE_AND))]),
        2,
    ));
    plant.connect(avail, avail_gate.input(1));
    plant.connect(protective_ok_in, avail_gate.input(2));
    plant.connect(&avail_gate.out, group_avail);
    plant.connect(group_avail_in, group_avail);
    plant.connect(group_avail_in, group.avail(index + 1));

    // The fault leg: the motor's proven fault beside the protective
    // aggregate.
    let fault_gate = plant.add(BoolGateSpec::new(
        parameters([("operation", Value::Int(GATE_OR))]),
        2,
    ));
    plant.connect(motor_fault_in, fault_gate.input(1));
    plant.connect(protective_in, fault_gate.input(2));
    plant.connect(&fault_gate.out, group_fault);
    plant.connect(group_fault_in, group_fault);
    plant.connect(group_fault_in, group.fault(index + 1));

    // ----------------------------------------------------------------
    // The machine's two declared alarms.
    // ----------------------------------------------------------------
    let trip_alarm = bool_alarm(
        plant,
        alarm_base,
        tripped_in,
        "The machine's surge guard stands on a proven surge or a declared trip",
        "Attend the machine before restarting it on the header",
        "blower-surge-trip-alarm",
        1,
        &signal_group,
        "surge-trip",
        ManagedInputs::default(),
    );
    let protective_alarm = bool_alarm(
        plant,
        alarm_base + 1,
        protective_in,
        "A hardwired protective device on the machine reports a trip",
        "Attend the reported protective device and restore the machine",
        "blower-protective-alarm",
        1,
        &signal_group,
        "protective",
        ManagedInputs::default(),
    );

    BlowerLayout {
        index: index + 1,
        flow: flow.into(),
        current: current.into(),
        speed_cmd: speed_cmd.into(),
        run_cmd: run_cmd.into(),
        run_fb: run_fb.into(),
        avail: avail.into(),
        vent_cmd: vent_cmd.into(),
        vent_fb: vent_fb.into(),
        surge_trip: surge_trip.into(),
        capacity: capacity.into(),
        guarded: guarded.into(),
        guarding: guarding.into(),
        tripped: tripped.into(),
        motor_fault: motor_fault.into(),
        protective: protective.into(),
        group_avail: group_avail.into(),
        group_fault: group_fault.into(),
        run_gated: run_gated.into(),
        group_cmd: group_cmd.into(),
        trip_alarm,
        protective_alarm,
    }
}
