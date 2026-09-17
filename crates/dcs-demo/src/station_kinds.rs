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
//! devices' register maps. All three overlay devices declare the
//! server's one address; each `sim-bus` attachment probes and serves
//! only its own device's channels.
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
//! externally paced machinery `dcs-controller --driven` uses: an
//! unpaced [`Monitor`] armed with [`Driven`] wiring, advanced one scan
//! per `POST /scan` request through [`MonitorClient`]. The scenario —
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
//! while the well refills with the group stood down, a manual takeover
//! hand-drives `p101` — the command register standing while the level
//! measurably drains, then released; a `Bad` primary flips the
//! failover to the backup measurement; a `Bad` backup on its own —
//! the issue-#502 reproduction — raises the `backup-unhealthy`
//! annunciation while the primary keeps serving; sustained `Bad` run contacts
//! prove the motor faults and drop both pumps from the group;
//! out-of-service blocks a hand start; power-fail drops every pump's
//! availability; a thermal contact trips its per-pump alarm.
//!
//! # What equality means here
//!
//! The same comparison [`two_kinds`](crate::two_kinds) records:
//! per-scan [`TelemetrySnapshot`]s — every point's value, quality, and
//! tick, component diagnostics, descriptors, live parameters, force
//! set, and the executor's I/O-health counters — and the transition
//! journal's [`JournalEntry`] sequence, with `io_health.driver`
//! normalized as the driver's own legitimately kind-specific transport
//! report.

use dcs_assembly::{DriverRegistry, FanoutDriver, assemble, resolve_drivers};
use dcs_core::{IoDriver, PointId, Quality, Value, ValueKind};
use dcs_model::{Endpoint, PlantModel};
use dcs_monitor::{Driven, Monitor, MonitorClient};
use dcs_runtime::Peer;
use dcs_sim::{Fault, ProcessElement, SimDriver};
use dcs_sim_bus::{
    BusDriver, BusServer, DeviceParameters, PointRegister, RegisterBank, RegisterDecl,
};
use std::collections::BTreeMap;
use std::sync::Mutex;
use std::thread;

use crate::two_kinds::{OperatorAction, TwoKindsError, VariantRun};

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
pub const TOTAL_SCANS: u64 = 115;

/// The station's point ids — the checked-in document's fixed blocks,
/// named for the scenario and its tests.
pub mod points {
    use dcs_core::PointId;

    /// The primary wet-well level measurement — the integrator's output.
    pub const LEVEL_PRIMARY: PointId = PointId(10);
    /// The backup level measurement — the first-order lag's output.
    pub const LEVEL_BACKUP: PointId = PointId(11);
    /// The declared station inflow — a `flow_sum` input.
    pub const INFLOW: PointId = PointId(12);
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
        // The primary level transmitter goes Bad — the failover
        // switches to the backup and raises its alarm.
        (
            38,
            vec![Inject {
                point: points::LEVEL_PRIMARY,
                quality: bad,
            }],
        ),
        (
            47,
            vec![Clear {
                point: points::LEVEL_PRIMARY,
            }],
        ),
        // The issue-#502 leg: the backup transmitter goes Bad on its
        // own while the primary keeps serving — the failover stays put
        // and the `backup-unhealthy` carrier and alarm annunciate the
        // standby already lost.
        (
            48,
            vec![Inject {
                point: points::LEVEL_BACKUP,
                quality: bad,
            }],
        ),
        (
            56,
            vec![Clear {
                point: points::LEVEL_BACKUP,
            }],
        ),
        // Both run contacts go Bad while their pumps run: the motors
        // prove the fault and the group drops the pumps.
        (
            62,
            vec![Inject {
                point: points::run(0),
                quality: bad,
            }],
        ),
        (
            64,
            vec![Inject {
                point: points::run(1),
                quality: bad,
            }],
        ),
        (
            74,
            vec![Clear {
                point: points::run(0),
            }],
        ),
        (
            75,
            vec![Clear {
                point: points::run(1),
            }],
        ),
        // Station power fails: every pump's availability drops.
        (
            88,
            vec![Write {
                point: points::POWER_FAIL,
                value: Value::Bool(true),
            }],
        ),
        (
            96,
            vec![Write {
                point: points::POWER_FAIL,
                value: Value::Bool(false),
            }],
        ),
        // A per-pump field contact: pump 1's thermal overload.
        (
            98,
            vec![Write {
                point: points::thermal(0),
                value: Value::Bool(true),
            }],
        ),
        (
            104,
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
        // Manual takeover on pump 1 while the refilling well has the
        // group stood down: `mode` selects hand, the operator's `hand`
        // request is then the only request reaching the motor — the
        // command register stands alone, drains the level, and its
        // release hands the pump back to auto. The request propagates
        // through three port-to-port gate hops, so `hand` applied at
        // scan 29 asserts the command register at scan 32 and its
        // release at scan 33 drops it at scan 36.
        write(27, points::mode(0), true),
        // Acknowledge the level alarms and the startup none-available
        // latch; the hand request rides the same tick; release the
        // acks two scans later.
        write(29, points::LAH_ACK, true),
        write(29, points::LAL_ACK, true),
        write(29, points::NA_ACK, true),
        write(29, points::hand(0), true),
        write(31, points::LAH_ACK, false),
        write(31, points::LAL_ACK, false),
        write(31, points::NA_ACK, false),
        write(33, points::hand(0), false),
        write(34, points::mode(0), false),
        // Acknowledge and release the backup-active alarm the failover
        // raised.
        write(44, points::BA_ACK, true),
        write(46, points::BA_ACK, false),
        // Acknowledge and release the backup-unhealthy alarm the
        // standby leg raised — the failover stayed on the primary.
        write(58, points::BUH_ACK, true),
        write(60, points::BUH_ACK, false),
        // Acknowledge the motor-fault, all-faulted, and none-available
        // latches the run-contact failures raised.
        write(71, points::fault_ack(0), true),
        write(71, points::AF_ACK, true),
        write(71, points::NA_ACK, true),
        write(73, points::fault_ack(0), false),
        write(73, points::AF_ACK, false),
        write(73, points::NA_ACK, false),
        // Out of service: even an operator's hand request cannot start
        // pump 2 — the guard blocks it.
        write(79, points::out_of_service(1), true),
        write(80, points::mode(1), true),
        write(81, points::hand(1), true),
        write(84, points::hand(1), false),
        write(84, points::mode(1), false),
        write(85, points::out_of_service(1), false),
        // Acknowledge the power-fail and none-available latches, release.
        write(94, points::PW_ACK, true),
        write(94, points::NA_ACK, true),
        write(96, points::PW_ACK, false),
        write(96, points::NA_ACK, false),
        // Acknowledge pump 1's thermal alarm, then release.
        write(103, points::thermal_ack(0), true),
        write(105, points::thermal_ack(0), false),
    ]
}

/// The value kind's neutral initial — the `0`/`false`/`0.0` the driver
/// bindings and an unwritten register seed.
fn neutral(kind: ValueKind) -> Value {
    match kind {
        ValueKind::Bool => Value::Bool(false),
        ValueKind::Int => Value::Int(0),
        ValueKind::Float => Value::Float(0.0),
    }
}

/// The primary document's dynamics declaration, parsed as
/// `dcs-plant-server --dynamics` parses it.
fn local_dynamics() -> Result<Vec<ProcessElement>, TwoKindsError> {
    serde_json::from_str(LOCAL_DYNAMICS)
        .map_err(|error| TwoKindsError::Field(format!("invalid dynamics document: {error}")))
}

/// The overlay's register-addressed dynamics declaration, parsed as
/// `dcs-sim-bus-device --dynamics` parses it.
fn bus_dynamics() -> Result<Vec<ProcessElement>, TwoKindsError> {
    serde_json::from_str(BUS_DYNAMICS)
        .map_err(|error| TwoKindsError::Field(format!("invalid dynamics document: {error}")))
}

/// The union register declarations the shared bank serves — every
/// overlay device's `registers` map, parsed through the same
/// [`DeviceParameters`] contract the driver-side factory and the
/// `dcs-sim-bus-device` binary read.
fn register_decls(model: &PlantModel) -> Result<Vec<RegisterDecl>, TwoKindsError> {
    let mut decls = Vec::new();
    for device in &model.devices {
        let channels: BTreeMap<String, ValueKind> = device
            .channels
            .iter()
            .map(|(name, channel)| (name.clone(), channel.value_type))
            .collect();
        let parameters =
            DeviceParameters::parse(&device.parameters, &channels).map_err(|error| {
                TwoKindsError::Field(format!(
                    "device {} parameters rejected: {error}",
                    device.id.0
                ))
            })?;
        decls.extend(parameters.registers.iter().map(|(name, declaration)| {
            RegisterDecl {
                register: declaration.register,
                initial: declaration
                    .initial
                    .unwrap_or_else(|| neutral(channels[name.as_str()])),
            }
        }));
    }
    Ok(decls)
}

/// The point → register bindings the field-side [`BusDriver`]
/// attachment drives through, derived from the overlay's own
/// channel→register maps so the rig and the controller cannot disagree
/// about the mapping.
fn field_bindings(model: &PlantModel) -> Result<Vec<PointRegister>, TwoKindsError> {
    let mut registers: BTreeMap<(u64, String), u16> = BTreeMap::new();
    for device in &model.devices {
        let channels: BTreeMap<String, ValueKind> = device
            .channels
            .iter()
            .map(|(name, channel)| (name.clone(), channel.value_type))
            .collect();
        let parameters =
            DeviceParameters::parse(&device.parameters, &channels).map_err(|error| {
                TwoKindsError::Field(format!(
                    "device {} parameters rejected: {error}",
                    device.id.0
                ))
            })?;
        registers.extend(
            parameters
                .registers
                .iter()
                .map(|(name, declaration)| ((device.id.0, name.clone()), declaration.register)),
        );
    }
    model
        .io_points
        .iter()
        .filter_map(|point| {
            let channel = point.channel.as_ref()?;
            Some(Ok(PointRegister {
                point: point.id,
                register: registers[&(channel.device.0, channel.name.clone())],
                kind: point.value_type,
            }))
        })
        .collect()
}

/// The model's field wires — `point → point` connections with both
/// endpoints channel-bound — as `(output, input)` pairs, the direction
/// [`resolve_drivers`] gives them: the `to` (`Out`) point drives the
/// `from` (`In`) point. On the primary binding these are loopbacks
/// inside the shared sim map; on the overlay they are the cross-backend
/// routes the rig carries field-side.
fn field_wires(model: &PlantModel) -> Vec<(PointId, PointId)> {
    let bound: std::collections::BTreeSet<PointId> = model
        .io_points
        .iter()
        .filter(|point| point.channel.is_some())
        .map(|point| point.id)
        .collect();
    model
        .connections
        .iter()
        .filter_map(|connection| match (&connection.from, &connection.to) {
            (Endpoint::Point(input), Endpoint::Point(output))
                if bound.contains(input) && bound.contains(output) =>
            {
                Some((*output, *input))
            }
            _ => None,
        })
        .collect()
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

/// Runs the documented scenario against an assembled variant through
/// the driven-mode machinery: an unpaced monitor whose `POST /scan`
/// requests each run one scan plus the cycle's `after_scan` wiring —
/// the boundary's field ops, wire copies, and single field step.
/// `boundary(0)` runs before the first request so scan 1 reads the
/// seeded field.
///
/// Determinism: requests are serialized by the monitor's lock, the
/// client waits for each response before issuing the next, and both
/// field sides are tick-domain — identical request sequences produce
/// identical runs.
fn driven_run<'d>(
    model: &PlantModel,
    driver: &'d FanoutDriver,
    boundary: impl Fn(u64) -> Result<(), String> + Send + Sync + 'd,
) -> Result<VariantRun, TwoKindsError> {
    boundary(0).map_err(TwoKindsError::Field)?;
    let executor = assemble(model, &dcs_controller::registry(), driver)?;
    let monitor = Monitor::bind(("127.0.0.1", 0), executor, model.signal_index())
        .map_err(TwoKindsError::Io)?
        .driven(Driven {
            track: None,
            after_scan: Some(Box::new(move |peer: &Peer<'d>| {
                // The scan cycle's field boundary — the position the
                // `--driven` controller's plant step occupies — carrying
                // the boundary's ops, the field wires, and the one
                // field step.
                boundary(peer.tick().0)
            })),
        });
    let addr = monitor.local_addr();
    thread::scope(|scope| {
        scope.spawn(|| monitor.serve());
        let client = MonitorClient::new(addr);
        let actions = actions();
        let result = (|| {
            let mut snapshots = Vec::with_capacity(TOTAL_SCANS as usize);
            let mut receipts = Vec::with_capacity(actions.len());
            for tick in 1..=TOTAL_SCANS {
                // Actions scheduled for this scan are submitted while
                // the run sits between scans — the executor applies
                // them at the coming scan's head.
                for action in actions.iter().filter(|action| action.tick == tick) {
                    receipts.push(client.command(&action.command)?);
                }
                snapshots.push(client.advance(1)?);
            }
            let journal = client.journal(0)?;
            Ok(VariantRun {
                snapshots,
                journal,
                receipts,
            })
        })();
        monitor.shutdown();
        result
    })
}

/// Loads the primary document and resolves its driver side through the
/// standard [`DriverRegistry`], merging the checked-in dynamics into
/// the shared sim map exactly as `dcs-plant-server --dynamics` does —
/// each element lands through `with_element` and revalidates the map.
pub fn local_variant() -> Result<(PlantModel, FanoutDriver), TwoKindsError> {
    let model = PlantModel::load(LOCAL_DOCUMENT)?;
    let mut plan = resolve_drivers(&model, &DriverRegistry::standard())?;
    for element in local_dynamics()? {
        plan.sim_map = plan.sim_map.with_element(element);
        plan.sim_map
            .validate()
            .map_err(|error| TwoKindsError::Field(format!("dynamics merge rejected: {error}")))?;
    }
    Ok((model, plan.build()?))
}

/// Serves the overlay's shared register bank: the union of the three
/// devices' declared register maps plus the register-addressed
/// dynamics, merged through [`RegisterBank::with_dynamics`] — the
/// construction `dcs-sim-bus-device --dynamics` performs — on an
/// ephemeral port.
pub fn serve_bus_bank() -> Result<BusServer, TwoKindsError> {
    let declared = PlantModel::load(BUS_DOCUMENT)?;
    let bank = RegisterBank::with_dynamics(register_decls(&declared)?, bus_dynamics()?)
        .map_err(|error| TwoKindsError::Field(format!("register bank rejected: {error}")))?;
    BusServer::bind(("127.0.0.1", 0), bank).map_err(TwoKindsError::Io)
}

/// Binds a [`BusServer`] serving the overlay's shared register bank and
/// substitutes its bound address for the overlay's
/// [`BUS_ADDRESS_PLACEHOLDER`], returning the resolved model with the
/// server. The caller runs `server.serve()` on its own thread:
/// resolving the `sim-bus` devices connects and probes every mapped
/// register, so the server must be serving before assembly runs.
pub fn bus_variant() -> Result<(PlantModel, BusServer), TwoKindsError> {
    // The register maps are model data: the bank, the controller's
    // bindings, and the field feed all derive from the one fixture.
    let server = serve_bus_bank()?;
    let addr = server.local_addr().map_err(TwoKindsError::Io)?;
    let model =
        PlantModel::load(&BUS_DOCUMENT.replace(BUS_ADDRESS_PLACEHOLDER, &addr.to_string()))?;
    Ok((model, server))
}

/// Runs the scenario on the primary `sim-ai`/`sim-di`/`sim-do` binding:
/// the dynamics merge into the shared sim map, and each boundary is the
/// `FanoutDriver::step` the `--driven` wiring installs — the single
/// local backend's loopbacks and elements.
pub fn run_local() -> Result<VariantRun, TwoKindsError> {
    let (model, driver) = local_variant()?;
    let driver = &driver;
    let sim = driver
        .sim()
        .expect("the primary binding's devices all serve the local sim");
    let ops = field_ops();
    let boundary = move |tick: u64| -> Result<(), String> {
        apply_local(&ops, tick, sim)?;
        driver
            .step(SCAN_PERIOD)
            .map_err(|error| format!("plant step failed: {error}"))
    };
    driven_run(&model, driver, boundary)
}

/// Runs the scenario on the `sim-bus` overlay: a [`BusServer`] serves
/// the union register bank with the dynamics merged, the overlay's
/// address placeholder is substituted, and a second [`BusDriver`]
/// attachment — the run's field side — applies each boundary's ops,
/// carries the two run-feedback field wires, re-applies standing
/// quality injections, and steps the bank once, inside the driven scan
/// boundary. No writer claim is taken in the run, so the bank stays
/// open to every attachment.
pub fn run_bus() -> Result<VariantRun, TwoKindsError> {
    let (model, server) = bus_variant()?;
    thread::scope(|scope| {
        scope.spawn(|| server.serve());
        let result = (|| {
            let driver = resolve_drivers(&model, &DriverRegistry::standard())?.build()?;
            let field = BusDriver::connect(server.local_addr()?, &field_bindings(&model)?)?;
            let ops = field_ops();
            let wires = field_wires(&model);
            let standing: Mutex<BTreeMap<u16, Quality>> = Mutex::new(BTreeMap::new());
            let boundary = move |tick: u64| -> Result<(), String> {
                apply_bus(&ops, tick, &field, &standing)?;
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
            driven_run(&model, &driver, boundary)
        })();
        server.shutdown();
        result
    })
}
