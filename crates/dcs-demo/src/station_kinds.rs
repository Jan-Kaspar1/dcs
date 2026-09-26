//! The station-level WW-FND-002 evidence: the reference pumping
//! station runs unchanged over two registered driver kinds.
//!
//! The requirement asks that the reference application — the M9
//! pumping station — run unchanged against at least two registered
//! driver kinds. [`two_kinds`](crate::two_kinds) proved the seam on a
//! small fixture pair; this module runs the real station over it:
//!
//! - `fixtures/pump_station.json` is the checked-in primary document:
//!   the `sim-ai`/`sim-di`/`sim-do` local-sim binding `dcs-build`'s
//!   `pumping_station` helper emits, driven by
//!   `fixtures/pump_station_dynamics.json` merged into its shared sim
//!   map — the `dcs-plant-server --dynamics` seam.
//! - `fixtures/pump_station_bus.json` is the checked-in `sim-bus`
//!   overlay: the same document with each device's `kind` and
//!   kind-specific `parameters` rebound onto register-mapped fieldbus
//!   channels — the `dcs-sim-bus-device --dynamics` seam.
//!
//! The recorded binding choice: a checked-in overlay document rebinding
//! only the `devices` section. `io_points`, `signals`, `components`,
//! `connections`, and `version` are byte-identical between the two
//! documents — the test target asserts it — and each overlay device
//! keeps the primary's id and declared channel set; only `kind` and
//! `parameters` differ. Every channel maps to the register equal to its
//! bound point's id, so `fixtures/pump_station_bus_dynamics.json` — the
//! register-oriented dynamics document — carries the same declaration
//! list as the point-addressed primary's.
//!
//! # The shared bank
//!
//! The station's dynamics span all three devices' channels — a
//! `bool_flow` reads a `sim-do` command channel and writes a `sim-ai`
//! draw channel — so one register bank serves the field: a [`BusServer`]
//! hosting every mapped register with the bus dynamics merged, the same
//! [`RegisterBank::with_dynamics`] construction
//! `dcs-sim-bus-device --dynamics` performs, over the union of the three
//! devices' register maps — the [`BankDerivation::Declared`] derivation
//! the run names when it serves the bank. All three overlay devices
//! declare the server's one address; each `sim-bus` attachment probes
//! and serves only its own device's channels.
//!
//! Because the three attachments share one bank, the run paces the
//! field from the field side: the driven `after_scan` hook — installed
//! by the run, as the `--driven` wiring allows — applies the boundary's
//! field ops, carries the model's two point→point field wires (run
//! feedback follows the command register — the value-only copy a
//! cross-backend [`FanoutDriver`] route performs), re-applies standing
//! quality injections a wire write would overwrite, and steps the bank
//! once through a second [`BusDriver`] attachment. `FanoutDriver::step`
//! is not used on the bus variant: its per-backend stepping would pace
//! the shared bank three times per scan. The primary variant's
//! `FanoutDriver::step` drives its single local backend — routes, then
//! loopbacks, then elements — the same order the bus rig reproduces
//! field-side.
//!
//! # The driven run
//!
//! [`run_local`] and [`run_bus`] run the identical scenario through the
//! shared driven-mode orchestration [`equivalence`](crate::equivalence)
//! carries — the externally paced machinery `dcs-controller --driven`
//! uses: an unpaced [`Monitor`](dcs_monitor::Monitor) armed with
//! [`Driven`](dcs_monitor::Driven) wiring, advanced one scan per
//! `POST /scan` request through
//! [`MonitorClient`](dcs_monitor::MonitorClient). The scenario —
//! [`field_ops`]'s boundary-keyed field writes and quality injections
//! plus [`actions`]'s operator commands — is the `dcs-build` station
//! scenario kept inside the vocabulary both field sides share: value
//! writes, `Quality` faults (never an error fault — the register
//! protocol injects quality only), and writable-point commands. A
//! `Write` never lands on an injected point: the sim keeps an injected
//! fault across a value write while the bank's real write clears it, so
//! the scenario keeps the two disjoint. The run quality-injects only
//! points no field wire targets — a wire copy is a value write, which
//! the bank counts as the injection's overwrite; the rig re-applies a
//! standing injection after each boundary's wire copies so the two
//! sides observe the same sustained `Bad`.
//!
//! The run length is [`TOTAL_SCANS`]: level climbs on the declared
//! inflow, the chain stages duty then lag and the pumps drain the well
//! back through `stop`; the level alarms trip, latch, and acknowledge;
//! while the recovered well parks mid-band with the group stood down,
//! a manual takeover hand-drives `p101` — the command register
//! standing once the protection holdout passes the operator's held
//! request, draining the level to the dry-run cutoff where the
//! protection interlock releases it, then released; a `Bad` primary
//! flips the failover to the backup measurement; a `Bad` backup on
//! its own — the issue-#502 reproduction — raises the
//! `backup-unhealthy` annunciation while the primary keeps serving;
//! sustained `Bad` run contacts
//! prove the motor faults and drop both pumps from the group;
//! out-of-service blocks a hand start; power-fail drops every pump's
//! availability; a thermal contact trips its per-pump alarm.
//!
//! # The frozen-field leg
//!
//! The `run_frozen_*` variants prove freshness in the deterministic
//! tick domain: inside [`FIELD_FREEZE`] the boundary applies the
//! script's ops but the field's own work — the local
//! `FanoutDriver::step`, or the bank's wires, re-injections, and step —
//! holds, so the field keeps serving its last reports while the driven
//! scans advance. The `net-flow` point's declared
//! `stale_after_ticks: 5` is the model's only freshness declaration:
//! lagging past it presents `Uncertain(Stale)` — the freshness
//! condition, distinct from a quality injection — while the
//! unbudgeted neighbors stay `Good`, and the resumed step's next
//! fresh report restores `Good`. It is the tick-deterministic
//! analogue of the QA lane's writer-stop freeze, the induction
//! `tests/stale_freshness.rs` asserts the published contract over.
//!
//! # What equality means here
//!
//! The same comparison [`two_kinds`](crate::two_kinds) records:
//! per-scan [`TelemetrySnapshot`](dcs_core::TelemetrySnapshot)s — every
//! point's value, quality, and tick, component diagnostics,
//! descriptors, live parameters, force set, and the executor's
//! I/O-health counters — and the transition journal's
//! [`JournalEntry`](dcs_core::JournalEntry) sequence, with
//! `io_health.driver` normalized as the driver's own legitimately
//! kind-specific transport report.

use dcs_assembly::{DriverRegistry, FanoutDriver, resolve_drivers};
use dcs_core::{IoDriver, PointId, Quality, Value, ValueKind};
use dcs_model::PlantModel;
use dcs_sim::{Fault, SimDriver};
use dcs_sim_bus::{BusDriver, BusServer};
use std::collections::BTreeMap;
use std::sync::Mutex;

use crate::equivalence::{
    self, BankDerivation, EquivalenceError, OperatorAction, VariantRun, driven_run, field_bindings,
    field_wires, with_served_bank,
};

/// The checked-in primary station document: the `dcs-build` helper's
/// emitted `sim-ai`/`sim-di`/`sim-do` local-sim binding.
pub const LOCAL_DOCUMENT: &str = include_str!("../fixtures/pump_station.json");

/// The checked-in `sim-bus` overlay: the same document with each
/// device's `kind` and `parameters` rebound; its `address` is the
/// [`BUS_ADDRESS_PLACEHOLDER`] a run substitutes its server's bound
/// address for.
pub const BUS_DOCUMENT: &str = include_str!("../fixtures/pump_station_bus.json");

/// The primary document's point-addressed dynamics declaration, merged
/// into the local sim map.
pub const LOCAL_DYNAMICS: &str = include_str!("../fixtures/pump_station_dynamics.json");

/// The overlay's register-addressed dynamics declaration, merged into
/// the served register bank. The register numbering — each register
/// address equals its bound point id — makes this document identical to
/// [`LOCAL_DYNAMICS`]; the checked-in copy records that the register
/// side carries the same process physics.
pub const BUS_DYNAMICS: &str = include_str!("../fixtures/pump_station_bus_dynamics.json");

/// The address placeholder [`BUS_DOCUMENT`] carries — the run serves
/// the register bank on an ephemeral port and substitutes the bound
/// address, as `dcs-assembly`'s `mixed_bus` fixture does.
pub const BUS_ADDRESS_PLACEHOLDER: &str = "__BUS_ADDR__";

/// Simulated process time each scan advances — the `dt` passed to the
/// field step; the dynamics' rates are per second.
pub const SCAN_PERIOD: f64 = 1.0;

/// The run's documented length in scans.
pub const TOTAL_SCANS: u64 = 135;

/// The frozen-field leg's window: `after_scan` boundaries in this range
/// run the script's ops but skip the field's own step — the
/// stored samples keep their stamps while the driven scans keep
/// reading them, the tick-deterministic analogue of the QA lane's
/// writer-stop induction.
///
/// The last step runs at boundary `start - 1`, so scan `start` reads
/// the field's last fresh report and the lag clock starts there: the
/// `net-flow` point's declared `stale_after_ticks: 5` budget presents
/// `Uncertain(Stale)` once the lag passes it — from scan `start + 6`
/// through scan `end + 1`, the last read of the held report before
/// the resumed step at boundary `end + 1` refreshes it. With the
/// declared budget that window is scans 12..=14, recovery at 15.
pub const FIELD_FREEZE: std::ops::RangeInclusive<u64> = 6..=13;

/// The station's point ids — the checked-in document's fixed blocks,
/// named for the scenario and its tests.
pub mod points {
    use dcs_core::PointId;

    /// The primary wet-well level measurement — the integrator's output.
    pub const LEVEL_PRIMARY: PointId = PointId(10);
    /// The backup level measurement — the second net-flow integrator's
    /// lower-datum output, decoupled from the primary's quality.
    pub const LEVEL_BACKUP: PointId = PointId(11);
    /// The declared station inflow — a `flow_sum` input.
    pub const INFLOW: PointId = PointId(12);
    /// The `flow_sum` net-flow carrier — the model's one
    /// `stale_after_ticks` declaration (a five-tick freshness budget),
    /// the point the frozen-field leg watches present
    /// `Uncertain(Stale)` once the field's reports lag past it.
    pub const NET_FLOW: PointId = PointId(13);
    /// The failover-selected level the chain and alarms control on.
    pub const LEVEL_SELECTED: PointId = PointId(200);
    /// The chain's stage-count demand carrier.
    pub const DEMAND: PointId = PointId(204);
    /// The group's duty-index carrier.
    pub const DUTY: PointId = PointId(210);
    /// The group's staged-count carrier.
    pub const STAGED: PointId = PointId(211);
    /// The failover's `backup_active` carrier.
    pub const BACKUP_ACTIVE: PointId = PointId(216);
    /// The group's `none_available` carrier.
    pub const NONE_AVAILABLE: PointId = PointId(217);
    /// The station power-fail field contact.
    pub const POWER_FAIL: PointId = PointId(120);
    /// Pump `index`'s simulated draw (`Float`, `In`) — the `bool_flow`
    /// output.
    pub fn draw(index: usize) -> PointId {
        PointId(20 + index as u64)
    }
    /// Pump `index`'s run-feedback field input — wired to follow its
    /// command.
    pub fn run(index: usize) -> PointId {
        PointId(40 + index as u64)
    }
    /// Pump `index`'s thermal-overload field contact.
    pub fn thermal(index: usize) -> PointId {
        PointId(60 + index as u64)
    }
    /// Pump `index`'s field command (`Bool`, `Out`) — register
    /// `100 + index` on the bus overlay.
    pub fn cmd(index: usize) -> PointId {
        PointId(100 + index as u64)
    }
    /// Pump `index`'s writable `mode` point — `false` auto, `true`
    /// manual.
    pub fn mode(index: usize) -> PointId {
        PointId(300 + 32 * index as u64)
    }
    /// Pump `index`'s writable `hand` run request.
    pub fn hand(index: usize) -> PointId {
        PointId(301 + 32 * index as u64)
    }
    /// Pump `index`'s writable out-of-service flag.
    pub fn out_of_service(index: usize) -> PointId {
        PointId(302 + 32 * index as u64)
    }
    /// The failover's `backup_unhealthy` carrier — asserts while the
    /// unused backup's own sample is untrusted.
    pub const BACKUP_UNHEALTHY: PointId = PointId(222);
    /// Pump `index`'s managed motor-fault alarm's writable ack — the
    /// alarm region's `1000 + 10·a` blocks start per-pump alarms at
    /// `a = 7 + 3·index` (fault/thermal/moisture in order), after the
    /// seven station alarms.
    pub fn fault_ack(index: usize) -> PointId {
        PointId(1000 + 10 * (7 + 3 * index as u64))
    }
    /// Pump `index`'s managed thermal alarm's writable ack.
    pub fn thermal_ack(index: usize) -> PointId {
        PointId(1000 + 10 * (8 + 3 * index as u64))
    }
    /// The backup-unhealthy alarm's writable ack.
    pub const BUH_ACK: PointId = PointId(1060);
    /// The high-level alarm's writable ack.
    pub const LAH_ACK: PointId = PointId(1000);
    /// The low-level alarm's writable ack.
    pub const LAL_ACK: PointId = PointId(1010);
    /// The backup-active alarm's writable ack.
    pub const BA_ACK: PointId = PointId(1020);
    /// The none-available alarm's writable ack.
    pub const NA_ACK: PointId = PointId(1030);
    /// The all-faulted alarm's writable ack.
    pub const AF_ACK: PointId = PointId(1040);
    /// The power-fail alarm's writable ack.
    pub const PW_ACK: PointId = PointId(1050);
}

/// One field-side change at a scan boundary — the vocabulary both field
/// sides share: a value write, a standing quality injection, or its
/// clear. Applied inside the driven `after_scan` hook after scan `t`,
/// so scan `t + 1` first observes it — the same position the
/// `dcs-build` station scenario's `sim.write`/`inject_fault` calls sit.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum FieldOp {
    /// Write `value` to the field `In` point — the sim's `SimDriver`
    /// write, the register bank's `WriteRegister`.
    Write {
        /// The field point the write lands on.
        point: PointId,
        /// The presented value; its kind is the point's declared kind.
        value: Value,
    },
    /// Stamp `quality` on the point's field sample — the sim's
    /// `Fault::Quality`, the bank's `inject_quality`. Stands until a
    /// [`Clear`](Self::Clear): on the bus side the rig re-applies it
    /// after each boundary's wire copies, since a register write is the
    /// injection's documented overwrite.
    Inject {
        /// The field point carrying the quality.
        point: PointId,
        /// The injected quality.
        quality: Quality,
    },
    /// Lift any injection on the point — the sim's `clear_fault`, the
    /// bank's `clear_quality`.
    Clear {
        /// The field point to clear.
        point: PointId,
    },
}

/// The scenario's field ops, keyed by the scan boundary they apply at:
/// ops at boundary `t` run inside the `after_scan` hook following scan
/// `t`, observed by scan `t + 1`. Boundary `0` seeds the declared
/// inflow before the first scan.
pub fn field_ops() -> BTreeMap<u64, Vec<FieldOp>> {
    use FieldOp::*;
    let bad = Quality::Bad(dcs_core::QualityReason::CommunicationFault);
    [
        // The declared inflow, standing from the run's start.
        (
            0,
            vec![Write {
                point: points::INFLOW,
                value: Value::Float(0.6),
            }],
        ),
        // Pump-down done — drop the inflow below zero so the wet well
        // drains past the dry-run cutoff, then restore it.
        (
            21,
            vec![Write {
                point: points::INFLOW,
                value: Value::Float(-0.5),
            }],
        ),
        (
            26,
            vec![Write {
                point: points::INFLOW,
                value: Value::Float(0.6),
            }],
        ),
        // The recovered well parks mid-band: the cutoff cleared, the
        // start setpoint unreached — the group stands down while the
        // manual-takeover leg's hand-driven pump has the well to
        // itself.
        (
            41,
            vec![Write {
                point: points::INFLOW,
                value: Value::Float(0.1),
            }],
        ),
        (
            57,
            vec![Write {
                point: points::INFLOW,
                value: Value::Float(0.6),
            }],
        ),
        // The primary level transmitter goes Bad — the failover
        // switches to the backup and raises its alarm.
        (
            62,
            vec![Inject {
                point: points::LEVEL_PRIMARY,
                quality: bad,
            }],
        ),
        (
            71,
            vec![Clear {
                point: points::LEVEL_PRIMARY,
            }],
        ),
        // The issue-#502 leg: the backup transmitter goes Bad on its
        // own while the primary keeps serving — the failover stays put
        // and the `backup-unhealthy` carrier and alarm annunciate the
        // standby already lost.
        (
            72,
            vec![Inject {
                point: points::LEVEL_BACKUP,
                quality: bad,
            }],
        ),
        (
            80,
            vec![Clear {
                point: points::LEVEL_BACKUP,
            }],
        ),
        // Both run contacts go Bad while their pumps run: the motors
        // prove the fault and the group drops the pumps.
        (
            86,
            vec![Inject {
                point: points::run(0),
                quality: bad,
            }],
        ),
        (
            88,
            vec![Inject {
                point: points::run(1),
                quality: bad,
            }],
        ),
        (
            98,
            vec![Clear {
                point: points::run(0),
            }],
        ),
        (
            99,
            vec![Clear {
                point: points::run(1),
            }],
        ),
        // Station power fails: every pump's availability drops.
        (
            112,
            vec![Write {
                point: points::POWER_FAIL,
                value: Value::Bool(true),
            }],
        ),
        (
            120,
            vec![Write {
                point: points::POWER_FAIL,
                value: Value::Bool(false),
            }],
        ),
        // A per-pump field contact: pump 1's thermal overload.
        (
            122,
            vec![Write {
                point: points::thermal(0),
                value: Value::Bool(true),
            }],
        ),
        (
            128,
            vec![Write {
                point: points::thermal(0),
                value: Value::Bool(false),
            }],
        ),
    ]
    .into_iter()
    .collect()
}

/// The scenario's operator actions — a [`Command::WriteValue`] on a
/// writable `In` point submitted between scans `tick - 1` and `tick`,
/// applying at scan `tick`'s head. Identical for both variants, so the
/// settled receipts journal identically.
pub fn actions() -> Vec<OperatorAction> {
    use dcs_core::Command;
    let write = |tick: u64, point: PointId, value: bool| OperatorAction {
        tick,
        command: Command::WriteValue {
            point,
            kind: ValueKind::Bool,
            value: Value::Bool(value),
        },
    };
    vec![
        // Acknowledge the level alarms and the startup none-available
        // latch; release the acks two scans later.
        write(29, points::LAH_ACK, true),
        write(29, points::LAL_ACK, true),
        write(29, points::NA_ACK, true),
        write(31, points::LAH_ACK, false),
        write(31, points::LAL_ACK, false),
        write(31, points::NA_ACK, false),
        // Manual takeover on pump 1 while the parked well has the
        // group stood down: `mode` selects hand, the operator's `hand`
        // request passes the protection holdout once `protections-ok`
        // has stood `min_off_ticks`, and the command register stands
        // alone — the field's only draw — draining the level to the
        // dry-run cutoff, where the protection interlock releases the
        // delivered command while `mode`/`hand` still stand; the
        // operator then releases the request and hands the pump back
        // to auto.
        write(44, points::mode(0), true),
        write(46, points::hand(0), true),
        write(60, points::hand(0), false),
        write(61, points::mode(0), false),
        // Acknowledge and release the backup-active alarm the failover
        // raised.
        write(68, points::BA_ACK, true),
        write(70, points::BA_ACK, false),
        // Acknowledge and release the backup-unhealthy alarm the
        // standby leg raised — the failover stayed on the primary.
        write(82, points::BUH_ACK, true),
        write(84, points::BUH_ACK, false),
        // Acknowledge the motor-fault, all-faulted, and none-available
        // latches the run-contact failures raised.
        write(95, points::fault_ack(0), true),
        write(95, points::AF_ACK, true),
        write(95, points::NA_ACK, true),
        write(97, points::fault_ack(0), false),
        write(97, points::AF_ACK, false),
        write(97, points::NA_ACK, false),
        // Out of service: even an operator's hand request cannot start
        // pump 2 — the guard blocks it.
        write(103, points::out_of_service(1), true),
        write(104, points::mode(1), true),
        write(105, points::hand(1), true),
        write(108, points::hand(1), false),
        write(108, points::mode(1), false),
        write(109, points::out_of_service(1), false),
        // Acknowledge the power-fail and none-available latches, release.
        write(118, points::PW_ACK, true),
        write(118, points::NA_ACK, true),
        write(120, points::PW_ACK, false),
        write(120, points::NA_ACK, false),
        // Acknowledge pump 1's thermal alarm, then release.
        write(127, points::thermal_ack(0), true),
        write(129, points::thermal_ack(0), false),
    ]
}

/// Applies the boundary's field ops against the local sim — the
/// `sim.write`/`inject_fault`/`clear_fault` vocabulary the `dcs-build`
/// station scenario uses.
fn apply_local(
    ops: &BTreeMap<u64, Vec<FieldOp>>,
    tick: u64,
    sim: &SimDriver,
) -> Result<(), String> {
    for op in ops.get(&tick).into_iter().flatten() {
        match *op {
            FieldOp::Write { point, value } => sim
                .write(point, value)
                .map_err(|error| format!("field write to point {} failed: {error}", point.0))?,
            FieldOp::Inject { point, quality } => sim
                .inject_fault(point, Fault::Quality(quality))
                .map_err(|error| {
                    format!("quality injection on point {} failed: {error}", point.0)
                })?,
            FieldOp::Clear { point } => sim
                .clear_fault(point)
                .map_err(|error| format!("fault clear on point {} failed: {error}", point.0))?,
        }
    }
    Ok(())
}

/// Applies the boundary's field ops against the shared register bank
/// through the field-side attachment — `WriteRegister`,
/// `inject_quality`, `clear_quality` — recording standing injections in
/// `standing` so the boundary's wire copies can be followed by their
/// re-application.
fn apply_bus(
    ops: &BTreeMap<u64, Vec<FieldOp>>,
    tick: u64,
    field: &BusDriver,
    standing: &Mutex<BTreeMap<u16, Quality>>,
) -> Result<(), String> {
    for op in ops.get(&tick).into_iter().flatten() {
        match *op {
            FieldOp::Write { point, value } => field
                .write(point, value)
                .map_err(|error| format!("field write to point {} failed: {error}", point.0))?,
            FieldOp::Inject { point, quality } => {
                standing.lock().unwrap().insert(point.0 as u16, quality);
                field
                    .inject_quality(point.0 as u16, quality)
                    .map_err(|error| {
                        format!("quality injection on register {} failed: {error}", point.0)
                    })?;
            }
            FieldOp::Clear { point } => {
                standing.lock().unwrap().remove(&(point.0 as u16));
                field.clear_quality(point.0 as u16).map_err(|error| {
                    format!("quality clear on register {} failed: {error}", point.0)
                })?;
            }
        }
    }
    Ok(())
}

/// Loads the primary document and resolves its driver side through the
/// standard [`DriverRegistry`], merging the checked-in dynamics into
/// the shared sim map exactly as `dcs-plant-server --dynamics` does —
/// each element lands through `with_element` and revalidates the map.
pub fn local_variant() -> Result<(PlantModel, FanoutDriver), EquivalenceError> {
    equivalence::local_variant(LOCAL_DOCUMENT, Some(LOCAL_DYNAMICS))
}

/// Serves the overlay's shared register bank: the union of the three
/// devices' declared register maps plus the register-addressed
/// dynamics, merged through [`RegisterBank::with_dynamics`] — the
/// construction `dcs-sim-bus-device --dynamics` performs — on an
/// ephemeral port.
pub fn serve_bus_bank() -> Result<BusServer, EquivalenceError> {
    equivalence::serve_bus_bank(BUS_DOCUMENT, BankDerivation::Declared, Some(BUS_DYNAMICS))
}

/// Binds a [`BusServer`] serving the overlay's shared register bank and
/// substitutes its bound address for the overlay's
/// [`BUS_ADDRESS_PLACEHOLDER`], returning the resolved model with the
/// server. The caller runs `server.serve()` on its own thread:
/// resolving the `sim-bus` devices connects and probes every mapped
/// register, so the server must be serving before assembly runs.
pub fn bus_variant() -> Result<(PlantModel, BusServer), EquivalenceError> {
    equivalence::bus_variant(
        BUS_DOCUMENT,
        BUS_ADDRESS_PLACEHOLDER,
        BankDerivation::Declared,
        Some(BUS_DYNAMICS),
    )
}

/// Runs the scenario on the primary `sim-ai`/`sim-di`/`sim-do` binding:
/// the dynamics merge into the shared sim map, and each boundary is the
/// `FanoutDriver::step` the `--driven` wiring installs — the single
/// local backend's loopbacks and elements.
pub fn run_local() -> Result<VariantRun, EquivalenceError> {
    run_local_inner(false)
}

/// [`run_local`] with the frozen-field leg: inside [`FIELD_FREEZE`] the
/// script's ops still land but the field never steps — the `net-flow`
/// report's freshness budget is the only declaration that degrades
/// while the driven scans outrun the held samples.
pub fn run_frozen_local() -> Result<VariantRun, EquivalenceError> {
    run_local_inner(true)
}

fn run_local_inner(frozen: bool) -> Result<VariantRun, EquivalenceError> {
    let (model, driver) = local_variant()?;
    let driver = &driver;
    let sim = driver
        .sim()
        .expect("the primary binding's devices all serve the local sim");
    let ops = field_ops();
    let boundary = move |tick: u64| -> Result<(), String> {
        apply_local(&ops, tick, sim)?;
        // The frozen leg: the field holds its last reports — the
        // step that would route loopbacks and advance the elements
        // does not run.
        if frozen && FIELD_FREEZE.contains(&tick) {
            return Ok(());
        }
        driver
            .step(SCAN_PERIOD)
            .map_err(|error| format!("plant step failed: {error}"))
    };
    driven_run(&model, driver, TOTAL_SCANS, &actions(), boundary)
}

/// Runs the scenario on the `sim-bus` overlay: a [`BusServer`] serves
/// the union register bank with the dynamics merged, the overlay's
/// address placeholder is substituted, and a second [`BusDriver`]
/// attachment — the run's field side — applies each boundary's ops,
/// carries the two run-feedback field wires, re-applies standing
/// quality injections, and steps the bank once, inside the driven scan
/// boundary. No writer claim is taken in the run, so the bank stays
/// open to every attachment.
pub fn run_bus() -> Result<VariantRun, EquivalenceError> {
    run_bus_inner(false)
}

/// [`run_bus`] with the frozen-field leg: inside [`FIELD_FREEZE`] the
/// register bank holds its last reports — the wire copies, standing
/// re-injections, and the bank step the boundary would carry all hold
/// with it, so the two transports freeze identically.
pub fn run_frozen_bus() -> Result<VariantRun, EquivalenceError> {
    run_bus_inner(true)
}

fn run_bus_inner(frozen: bool) -> Result<VariantRun, EquivalenceError> {
    with_served_bank(
        BUS_DOCUMENT,
        BUS_ADDRESS_PLACEHOLDER,
        BankDerivation::Declared,
        Some(BUS_DYNAMICS),
        |model, addr| {
            let driver = resolve_drivers(model, &DriverRegistry::standard())?.build()?;
            let field = BusDriver::connect(addr, &field_bindings(model)?)?;
            let ops = field_ops();
            let wires = field_wires(model);
            let standing: Mutex<BTreeMap<u16, Quality>> = Mutex::new(BTreeMap::new());
            let boundary = move |tick: u64| -> Result<(), String> {
                apply_bus(&ops, tick, &field, &standing)?;
                // The frozen leg: the field's own work — the wire
                // copies, the standing re-injections, the bank step —
                // holds with the bank, so the reports the scans read
                // stop changing exactly as the local sim's do.
                if frozen && FIELD_FREEZE.contains(&tick) {
                    return Ok(());
                }
                // The field wires: run feedback follows the command
                // register — the value-only copy a cross-backend route
                // performs.
                for (output, input) in &wires {
                    let value = field
                        .read(*output)
                        .map_err(|error| {
                            format!("field wire read of point {} failed: {error}", output.0)
                        })?
                        .value;
                    field.write(*input, value).map_err(|error| {
                        format!("field wire write to point {} failed: {error}", input.0)
                    })?;
                }
                // A wire copy is a real write — the injection's
                // documented overwrite — so standing injections are
                // re-applied after the copies.
                for (&register, &quality) in standing.lock().unwrap().iter() {
                    field.inject_quality(register, quality).map_err(|error| {
                        format!("quality re-injection on register {register} failed: {error}")
                    })?;
                }
                field
                    .step(SCAN_PERIOD)
                    .map_err(|error| format!("bank step failed: {error}"))?;
                Ok(())
            };
            driven_run(model, &driver, TOTAL_SCANS, &actions(), boundary)
        },
    )
}
