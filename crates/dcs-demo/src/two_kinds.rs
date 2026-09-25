//! One logical plant run against two registered driver kinds — the
//! interim WW-FND-002 evidence.
//!
//! The requirement asks that the reference application run unchanged
//! against at least two registered driver kinds; the full proof waits
//! for the M9 station, but the seam is exercised here by a checked-in
//! fixture pair sharing one logical plant:
//!
//! - `fixtures/two_kinds_scripted.json` binds every field channel to a
//!   `sim-scripted` device — tick-indexed playback declared in the
//!   model's device parameters, stepped by the scan cycle.
//! - `fixtures/two_kinds_bus.json` binds the same channels to a
//!   `sim-bus` device — a register-mapped fieldbus reached over its own
//!   binary TCP protocol, served by a [`BusServer`] the run owns, with
//!   the test side driving input registers through a second
//!   [`BusDriver`] attachment.
//!
//! The two documents are identical outside `devices[0]` — the same
//! `io_points`, `signals`, `components`, and `connections`, and the same
//! declared channel set; only the kind and its kind-specific parameters
//! differ. The test target asserts that sharing, so the fixture pair is
//! a recorded instance of "one logical fixture plus a per-kind device
//! overlay".
//!
//! The plant is a small water-station slice: `LT-201`'s raw wet-well
//! level feeds an `analog-input` scaling into the `LIC-201` `pid`, whose
//! output positions `LV-201` through a `valve` actuator checking its
//! field-echoed position feedback (a `point → point` field wire — a
//! cross-backend route on both variants), while `P-201`'s operator start
//! request drives a `motor` verifying its `digital-input`-conditioned
//! run feedback. The operator points — the level setpoint and the start
//! request — are writable internal points; `level_raw` is a writable
//! field `In` point so the run can force the measurement.
//!
//! # The driven run
//!
//! [`run_scripted`] and [`run_bus`] run the identical scenario through
//! the same externally paced machinery `dcs-controller --driven` uses:
//! an unpaced [`Monitor`] armed with [`Driven`] wiring, advanced one
//! scan per `POST /scan` request through [`MonitorClient`]. Each
//! requested scan carries the plant step inside the request's boundary
//! — and, on the bus variant, the field feed for the next scan — so
//! the field side and the scan stay in lockstep and nothing reads a
//! wall clock.
//!
//! The single field program [`FIELD_PROGRAM`] is the scenario's input:
//! `(tick, point, value)` entries naming the value a field `In` point
//! presents to the scan running at `tick`. The scripted fixture encodes
//! it as model-declared script entries — a script entry at driver tick
//! `t` applies on the step after scan `t`, so it is first observed by
//! scan `t + 1`; the test pins that encoding to the table. The bus
//! variant's feed writes the same entries to the device's registers
//! through the field-side attachment, in the same boundary position.
//!
//! Between the documented scans the run submits operator commands —
//! the setpoint move, the pump start and stop, a force and release on
//! `level_raw`, and a mid-run `kp` retune — so the recorded journal
//! carries settled receipts and quality transitions, not just first
//! observations.
//!
//! # What equality means here
//!
//! The assertion compares what the control plane observed: per-scan
//! [`TelemetrySnapshot`]s — every point's value, quality, and tick, the
//! component diagnostics, descriptors, live parameters, force set, and
//! the executor-collected I/O-health counters — and the transition
//! journal's [`JournalEntry`] sequence. One snapshot field is
//! legitimately kind-specific and normalized before comparison:
//! `io_health.driver`, the driver's own volunteered transport
//! diagnostics — the `sim-bus` link reports itself connected while the
//! scripted backend has no transport to report. That is the same
//! normalization `dcs-assembly`'s `sim-bus` test records: the executor's
//! view is identical; each transport's health report is its own.

use dcs_assembly::{AssemblyError, DriverRegistry, FanoutDriver, assemble, resolve_drivers};
use dcs_core::{
    Command, CommandReceipt, IoDriver, JournalEntry, PointHistory, PointId, TelemetrySnapshot,
    Value, ValueKind,
};
use dcs_model::{LoadError, PlantModel};
use dcs_monitor::{Driven, Monitor, MonitorClient};
use dcs_runtime::Peer;
use dcs_sim_bus::{BusDriver, BusServer, PointRegister, RegisterBank, RegisterDecl};
use std::fmt;
use std::io;
use std::thread;

/// The checked-in `sim-scripted` variant of the shared logical plant.
pub const SCRIPTED_DOCUMENT: &str = include_str!("../fixtures/two_kinds_scripted.json");

/// The checked-in `sim-bus` variant: the same document with the device's
/// kind and parameters swapped; its `address` is the
/// [`BUS_ADDRESS_PLACEHOLDER`] a run substitutes its server's bound
/// address for.
pub const BUS_DOCUMENT: &str = include_str!("../fixtures/two_kinds_bus.json");

/// The address placeholder [`BUS_DOCUMENT`] carries — the run serves
/// the device's register bank on an ephemeral port and substitutes the
/// bound address, exactly as `dcs-assembly`'s `mixed_bus` fixture does.
pub const BUS_ADDRESS_PLACEHOLDER: &str = "__BUS_ADDR__";

/// Simulated process time each scan advances — the `dt` passed to
/// [`FanoutDriver::step`] and the period the pid's `dt` parameter is
/// tuned for.
pub const SCAN_PERIOD: f64 = 0.1;

/// The run's documented length in scans.
pub const TOTAL_SCANS: u64 = 64;

/// The shared model's point ids, named for the scenario and its tests.
pub mod points {
    use dcs_core::PointId;

    /// `LT-201` raw wet-well level input, mA — the feed's analog
    /// program; writable so the run can force it.
    pub const LEVEL_RAW: PointId = PointId(10);
    /// `P-201` run feedback input — the feed's bool program: follows
    /// the command late, drops mid-run, returns, then follows the stop.
    pub const PUMP_RUN: PointId = PointId(11);
    /// `LV-201` position feedback input — field-wired to follow
    /// `VALVE_CMD`, driven by the cross-backend route, never fed.
    pub const VALVE_FEEDBACK: PointId = PointId(12);
    /// `LV-201` valve command output, mA.
    pub const VALVE_CMD: PointId = PointId(20);
    /// `P-201` starter command output.
    pub const PUMP_CMD: PointId = PointId(21);
    /// `LIC-201` level setpoint, % — writable internal point.
    pub const LEVEL_SETPOINT: PointId = PointId(50);
    /// `P-201` operator start request — writable internal point.
    pub const PUMP_START: PointId = PointId(51);
    /// `P-201` run-feedback fault — the motor's status output.
    pub const PUMP_FAULT: PointId = PointId(70);
    /// `LV-201` feedback discrepancy — the valve's status output.
    pub const VALVE_DISCREPANCY: PointId = PointId(71);
}

/// One entry of the shared field program: the value `point` presents to
/// the scan running at `tick` — the first scan is tick 1 — holding until
/// the point's next entry.
///
/// The scripted fixture encodes each change as a script entry at driver
/// tick `tick - 1`: a step applies entries whose tick it reaches, and
/// the driven cycle steps after each scan, so the value applied at
/// driver tick `tick - 1` is first read by scan `tick`. The bus
/// variant's feed writes the change's register during the scan
/// boundary preceding scan `tick` — inside the driven `after_scan`
/// hook — which is the same position in the cycle.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct FieldChange {
    /// The scan tick that first observes the value.
    pub tick: u64,
    /// The field `In` point.
    pub point: PointId,
    /// The presented value; its kind is the point's declared kind.
    pub value: Value,
}

/// The scenario's field inputs, in scheduled order — the single program
/// both variants present: the scripted device replays it from its
/// declared script, the test feeds it to the bus device's registers.
pub const FIELD_PROGRAM: &[FieldChange] = &[
    // The wet well starts at 8 mA (25%) and climbs on inflow; the pump
    // draws it down from tick 45 while its feedback is lost, and the
    // well refills once the pump stops.
    FieldChange {
        tick: 1,
        point: points::LEVEL_RAW,
        value: Value::Float(8.0),
    },
    FieldChange {
        tick: 12,
        point: points::LEVEL_RAW,
        value: Value::Float(9.5),
    },
    FieldChange {
        tick: 24,
        point: points::LEVEL_RAW,
        value: Value::Float(11.0),
    },
    FieldChange {
        tick: 36,
        point: points::LEVEL_RAW,
        value: Value::Float(12.5),
    },
    FieldChange {
        tick: 45,
        point: points::LEVEL_RAW,
        value: Value::Float(13.5),
    },
    FieldChange {
        tick: 57,
        point: points::LEVEL_RAW,
        value: Value::Float(12.0),
    },
    // The starter's run contact follows the command three scans late,
    // drops mid-run at tick 45 — the motor flags its fault once the
    // disagreement outlives `fault_ticks` — returns at 53, and follows
    // the stop at 58.
    FieldChange {
        tick: 1,
        point: points::PUMP_RUN,
        value: Value::Bool(false),
    },
    FieldChange {
        tick: 9,
        point: points::PUMP_RUN,
        value: Value::Bool(true),
    },
    FieldChange {
        tick: 45,
        point: points::PUMP_RUN,
        value: Value::Bool(false),
    },
    FieldChange {
        tick: 53,
        point: points::PUMP_RUN,
        value: Value::Bool(true),
    },
    FieldChange {
        tick: 58,
        point: points::PUMP_RUN,
        value: Value::Bool(false),
    },
];

/// One scripted operator action: `command` is submitted between scans
/// `tick - 1` and `tick`, so it applies at scan `tick`'s head — the
/// receipt's `Accepted { apply_tick: tick }`.
#[derive(Debug, Clone, PartialEq)]
pub struct OperatorAction {
    /// The scan tick the command applies at.
    pub tick: u64,
    /// The command submitted through `POST /command`.
    pub command: Command,
}

/// The scenario's operator actions, in submission order — identical for
/// both variants, so their settled receipts journal identically.
pub fn actions() -> Vec<OperatorAction> {
    vec![
        // The pump start request — applied at scan 6; the feedback
        // follows at 9, inside `fault_ticks`.
        OperatorAction {
            tick: 6,
            command: Command::WriteValue {
                point: points::PUMP_START,
                kind: ValueKind::Bool,
                value: Value::Bool(true),
            },
        },
        // The operator's setpoint move, applied at scan 11.
        OperatorAction {
            tick: 11,
            command: Command::WriteValue {
                point: points::LEVEL_SETPOINT,
                kind: ValueKind::Float,
                value: Value::Float(62.0),
            },
        },
        // A force on the field measurement, applied at scan 31: the
        // image substitutes the pinned value stamped
        // `Uncertain(Substituted)` — a quality transition both kinds
        // journal identically, since the force never touches the field.
        OperatorAction {
            tick: 31,
            command: Command::ForcePoint {
                point: points::LEVEL_RAW,
                kind: ValueKind::Float,
                value: Value::Float(15.0),
            },
        },
        // The release, applied at scan 39: the next input phase reads
        // the field again.
        OperatorAction {
            tick: 39,
            command: Command::UnforcePoint {
                point: points::LEVEL_RAW,
            },
        },
        // The stop request, applied at scan 56; the feedback follows
        // at 58, inside `fault_ticks`.
        OperatorAction {
            tick: 56,
            command: Command::WriteValue {
                point: points::PUMP_START,
                kind: ValueKind::Bool,
                value: Value::Bool(false),
            },
        },
        // A mid-run retune through the parameter path, applied at 59.
        OperatorAction {
            tick: 59,
            command: Command::SetParameter {
                component: "pid:2".to_string(),
                name: "kp".to_string(),
                value: Value::Float(0.8),
            },
        },
    ]
}

/// Why loading, assembling, or running a two-kinds variant failed.
#[derive(Debug)]
pub enum TwoKindsError {
    /// The model document failed [`PlantModel::load`].
    Load(LoadError),
    /// Device resolution, component construction, or wiring failed.
    Assembly(AssemblyError),
    /// A monitor request or server operation failed.
    Io(io::Error),
    /// The field side failed: the register bank, the field attachment,
    /// or a feed write. The driven cycle's after-scan wiring reports
    /// through the requesting `POST /scan`, surfacing here as [`Io`].
    Field(String),
}

impl fmt::Display for TwoKindsError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Load(error) => write!(f, "{error}"),
            Self::Assembly(error) => write!(f, "{error}"),
            Self::Io(error) => write!(f, "{error}"),
            Self::Field(detail) => write!(f, "field side failed: {detail}"),
        }
    }
}

impl std::error::Error for TwoKindsError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Load(error) => Some(error),
            Self::Assembly(error) => Some(error),
            Self::Io(error) => Some(error),
            Self::Field(_) => None,
        }
    }
}

impl From<LoadError> for TwoKindsError {
    fn from(error: LoadError) -> Self {
        Self::Load(error)
    }
}

impl From<AssemblyError> for TwoKindsError {
    fn from(error: AssemblyError) -> Self {
        Self::Assembly(error)
    }
}

impl From<io::Error> for TwoKindsError {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}

/// One finished driven run: what the control plane observed, the
/// payloads the equivalence assertion compares.
#[derive(Debug)]
pub struct VariantRun {
    /// The `POST /scan` response after each of [`TOTAL_SCANS`] scans —
    /// the executor's per-scan snapshot.
    pub snapshots: Vec<TelemetrySnapshot>,
    /// The full transition journal after the run — `GET /journal`'s
    /// payload.
    pub journal: Vec<JournalEntry>,
    /// The receipts the run's operator commands returned, in
    /// submission order.
    pub receipts: Vec<CommandReceipt>,
    /// The bounded point-history rings after the run — `GET /history`'s
    /// payload: every point's retained samples in `seq` order, the
    /// durable record a lagging consumer reads back.
    pub history: Vec<PointHistory>,
}

/// The value kind's neutral initial — the same `0`/`false`/`0.0` the
/// driver bindings seed, matching the scripted bindings' initials so
/// unwritten registers read identically.
fn neutral(kind: ValueKind) -> Value {
    match kind {
        ValueKind::Bool => Value::Bool(false),
        ValueKind::Int => Value::Int(0),
        ValueKind::Float => Value::Float(0.0),
    }
}

/// The register bank the bus variant's device serves: one register per
/// declared channel, at the indices the model's `registers` parameter
/// maps, initialized to the channels' neutral values.
fn register_bank(model: &PlantModel) -> Result<RegisterBank, TwoKindsError> {
    let device = model
        .devices
        .first()
        .expect("the bus fixture declares device 1");
    let registers = device
        .parameters
        .get("registers")
        .and_then(|registers| registers.as_object())
        .expect("the bus fixture declares a registers map");
    let decls = device.channels.iter().map(|(name, channel)| {
        let register = registers[name.as_str()]
            .as_u64()
            .expect("the bus fixture maps every channel to a register index");
        RegisterDecl {
            register: register as u16,
            initial: neutral(channel.value_type),
        }
    });
    RegisterBank::new(decls)
        .map_err(|error| TwoKindsError::Field(format!("register bank rejected: {error}")))
}

/// The point → register bindings a field-side [`BusDriver`] feeds
/// through, derived from the model's own channel→register map so the
/// rig and the controller cannot disagree about the mapping.
fn field_bindings(model: &PlantModel) -> Vec<PointRegister> {
    let device = model
        .devices
        .first()
        .expect("the bus fixture declares device 1");
    let registers = device
        .parameters
        .get("registers")
        .and_then(|registers| registers.as_object())
        .expect("the bus fixture declares a registers map");
    model
        .io_points
        .iter()
        .filter_map(|point| {
            let channel = point.channel.as_ref()?;
            Some(PointRegister {
                point: point.id,
                register: registers[channel.name.as_str()].as_u64().unwrap() as u16,
                kind: point.value_type,
            })
        })
        .collect()
}

/// Runs the documented scenario against an assembled variant through
/// the driven-mode machinery: an unpaced monitor whose `POST /scan`
/// requests each run one scan plus the cycle's `after_scan` wiring —
/// the plant step, then `feed` presenting the next scan's field inputs.
/// `feed(1)` runs before the first request so scan 1 reads the
/// program's first values.
///
/// Determinism: requests are serialized by the monitor's lock, the
/// client waits for each response before issuing the next, and both
/// driver kinds are tick-domain — identical request sequences produce
/// identical runs.
fn driven_run<'d>(
    model: &PlantModel,
    driver: &'d FanoutDriver,
    feed: impl Fn(u64) -> Result<(), String> + Send + Sync + 'd,
) -> Result<VariantRun, TwoKindsError> {
    feed(1).map_err(TwoKindsError::Field)?;
    let executor = assemble(model, &dcs_controller::registry(), driver)?;
    let monitor = Monitor::bind(("127.0.0.1", 0), executor, model.signal_index())
        .map_err(TwoKindsError::Io)?
        .driven(Driven {
            track: None,
            after_scan: Some(Box::new(move |peer: &Peer<'d>| {
                // The scan cycle's plant step — the same call the
                // `--driven` controller installs — then the field's
                // next presentation, still inside the request boundary.
                driver
                    .step(SCAN_PERIOD)
                    .map_err(|error| format!("plant step failed: {error}"))?;
                feed(peer.tick().0 + 1)
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
            let history = client.history(&[], 0)?;
            Ok(VariantRun {
                snapshots,
                journal,
                receipts,
                history,
            })
        })();
        monitor.shutdown();
        result
    })
}

/// Runs the scenario on the `sim-scripted` variant: the fixture's
/// declared script is the field side, so the feed is a no-op — the
/// driven step alone advances playback.
pub fn run_scripted() -> Result<VariantRun, TwoKindsError> {
    let model = PlantModel::load(SCRIPTED_DOCUMENT)?;
    let driver = resolve_drivers(&model, &DriverRegistry::standard())?.build()?;
    driven_run(&model, &driver, |_| Ok(()))
}

/// Binds a [`BusServer`] serving the bus fixture's declared register map
/// on an ephemeral port, substitutes its bound address for the
/// fixture's [`BUS_ADDRESS_PLACEHOLDER`], and returns the resolved model
/// with the server. The caller runs `server.serve()` on its own thread:
/// resolving the `sim-bus` device connects and probes every mapped
/// register, so the server must be serving before assembly runs.
pub fn bus_variant() -> Result<(PlantModel, BusServer), TwoKindsError> {
    // The register map is model data: the bank, the controller's
    // bindings, and the field feed all derive from the one fixture.
    let declared = PlantModel::load(BUS_DOCUMENT)?;
    let bank = register_bank(&declared)?;
    let server = BusServer::bind(("127.0.0.1", 0), bank).map_err(TwoKindsError::Io)?;
    let addr = server.local_addr().map_err(TwoKindsError::Io)?;
    let model =
        PlantModel::load(&BUS_DOCUMENT.replace(BUS_ADDRESS_PLACEHOLDER, &addr.to_string()))?;
    Ok((model, server))
}

/// Runs the scenario on the `sim-bus` variant: a [`BusServer`] serves
/// the model's declared register map on an ephemeral port, the
/// fixture's address placeholder is substituted, and a second
/// [`BusDriver`] attachment — the test's field side — writes each
/// [`FIELD_PROGRAM`] change's register inside the driven scan boundary.
/// No writer claim is taken in the run, so the device stays open to
/// both attachments.
pub fn run_bus() -> Result<VariantRun, TwoKindsError> {
    let (model, server) = bus_variant()?;
    thread::scope(|scope| {
        scope.spawn(|| server.serve());
        let result = (|| {
            let driver = resolve_drivers(&model, &DriverRegistry::standard())?.build()?;
            let field = BusDriver::connect(server.local_addr()?, &field_bindings(&model))?;
            let feed = move |tick: u64| -> Result<(), String> {
                for change in FIELD_PROGRAM.iter().filter(|change| change.tick == tick) {
                    field.write(change.point, change.value).map_err(|error| {
                        format!("register feed for point {} failed: {error}", change.point.0)
                    })?;
                }
                Ok(())
            };
            driven_run(&model, &driver, feed)
        })();
        server.shutdown();
        result
    })
}
