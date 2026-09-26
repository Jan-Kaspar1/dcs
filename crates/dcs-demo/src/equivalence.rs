//! The shared driven-equivalence orchestration: one logical plant run
//! against each driver-kind variant a scenario binds, so the
//! equivalence assertion can compare what the control plane observed.
//!
//! An equivalence runner — [`two_kinds`](crate::two_kinds),
//! [`station_kinds`](crate::station_kinds), and the M10
//! closing-verification runner — supplies only scenario content: the
//! fixture documents (a primary binding plus a `sim-bus` overlay whose
//! `address` is a placeholder the run substitutes), the field program
//! or boundary-keyed op set, the operator actions, and the scenario
//! pins (scan count, scan period, point ids). Everything the driven
//! cycle requires lives here, once:
//!
//! - [`driven_run`] runs the scenario through the externally paced
//!   machinery `dcs-controller --driven` uses: an unpaced [`Monitor`]
//!   armed with [`Driven`] wiring — no track target, the caller's
//!   boundary hook as `after_scan` — advanced one scan per
//!   `POST /scan` request through [`MonitorClient`] on a scope thread.
//!   The per-tick loop submits each scheduled [`OperatorAction`]
//!   between scans so it applies at the coming scan's head, then
//!   collects the receipts, the per-scan snapshots, the transition
//!   journal, and the bounded point histories into [`VariantRun`].
//! - [`serve_bus_bank`], [`bus_variant`], and [`with_served_bank`]
//!   carry the `sim-bus` overlay's bind/substitute/resolve pattern:
//!   the register bank derived from the model's own declared maps —
//!   [`BankDerivation`] names how each runner seeds it — served on an
//!   ephemeral port, its bound address substituted for the document's
//!   placeholder, serving on a scope thread while the run resolves the
//!   controller side and a field-side [`BusDriver`] attachment.
//! - [`local_variant`] resolves the primary document through the
//!   standard registry, merging declared dynamics into the shared sim
//!   map as `dcs-plant-server --dynamics` does.
//! - [`neutral`], [`register_decls`], [`field_bindings`],
//!   [`field_wires`], and [`parse_dynamics`] are the bank- and
//!   model-derivation helpers the rig and the controller share, so the
//!   two cannot disagree about the mapping.
//!
//! Determinism: requests are serialized by the monitor's lock, the
//! client waits for each response before issuing the next, and the
//! field sides are tick-domain — identical request sequences produce
//! identical runs.

use dcs_assembly::{AssemblyError, DriverRegistry, FanoutDriver, assemble, resolve_drivers};
use dcs_core::{
    Command, CommandReceipt, JournalEntry, PointHistory, PointId, TelemetrySnapshot, Value,
    ValueKind,
};
use dcs_model::{Endpoint, LoadError, PlantModel};
use dcs_monitor::{Driven, Monitor, MonitorClient};
use dcs_runtime::Peer;
use dcs_sim::ProcessElement;
use dcs_sim_bus::{BusServer, DeviceParameters, PointRegister, RegisterBank, RegisterDecl};
use std::collections::BTreeMap;
use std::fmt;
use std::io;
use std::net::SocketAddr;
use std::thread;

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

/// One finished driven run: what the control plane observed, the
/// payloads the equivalence assertion compares.
#[derive(Debug)]
pub struct VariantRun {
    /// The `POST /scan` response after each scan — the executor's
    /// per-scan snapshot.
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

/// Why loading, assembling, or running an equivalence variant failed.
#[derive(Debug)]
pub enum EquivalenceError {
    /// The model document failed [`PlantModel::load`].
    Load(LoadError),
    /// Device resolution, component construction, or wiring failed.
    Assembly(AssemblyError),
    /// A monitor request or server operation failed.
    Io(io::Error),
    /// The field side failed: the register bank, the field attachment,
    /// or a boundary op. The driven cycle's after-scan wiring reports
    /// through the requesting `POST /scan`, surfacing here as
    /// [`Io`](Self::Io).
    Field(String),
}

impl fmt::Display for EquivalenceError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Load(error) => write!(f, "{error}"),
            Self::Assembly(error) => write!(f, "{error}"),
            Self::Io(error) => write!(f, "{error}"),
            Self::Field(detail) => write!(f, "field side failed: {detail}"),
        }
    }
}

impl std::error::Error for EquivalenceError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Load(error) => Some(error),
            Self::Assembly(error) => Some(error),
            Self::Io(error) => Some(error),
            Self::Field(_) => None,
        }
    }
}

impl From<LoadError> for EquivalenceError {
    fn from(error: LoadError) -> Self {
        Self::Load(error)
    }
}

impl From<AssemblyError> for EquivalenceError {
    fn from(error: AssemblyError) -> Self {
        Self::Assembly(error)
    }
}

impl From<io::Error> for EquivalenceError {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}

/// The value kind's neutral initial — the same `0`/`false`/`0.0` the
/// driver bindings seed, matching the scripted bindings' initials so
/// unwritten registers read identically.
pub fn neutral(kind: ValueKind) -> Value {
    match kind {
        ValueKind::Bool => Value::Bool(false),
        ValueKind::Int => Value::Int(0),
        ValueKind::Float => Value::Float(0.0),
    }
}

/// Which register-bank derivation a runner's `sim-bus` overlay needs —
/// the named choice [`serve_bus_bank`], [`bus_variant`], and
/// [`with_served_bank`] take, so a runner declares its derivation
/// rather than guessing a copy to clone.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BankDerivation {
    /// Every declared register seeded at its channel kind's [`neutral`]
    /// value — the overlay maps channels onto bare register addresses
    /// and declares no `initial` values (the `two_kinds` overlay).
    Neutral,
    /// Each channel's declared `initial` honored, [`neutral`] where a
    /// declaration carries none — the overlay's registers seed their
    /// documented power-on values (the `station_kinds` overlay).
    Declared,
}

/// Parses a `--dynamics`-style document into the [`ProcessElement`]
/// list [`local_variant`] and [`serve_bus_bank`] merge — the same form
/// `dcs-plant-server --dynamics` and `dcs-sim-bus-device --dynamics`
/// read.
pub fn parse_dynamics(source: &str) -> Result<Vec<ProcessElement>, EquivalenceError> {
    serde_json::from_str(source)
        .map_err(|error| EquivalenceError::Field(format!("invalid dynamics document: {error}")))
}

/// The union register declarations the overlay's bank serves — every
/// `sim-bus` device's `registers` map, parsed through the same
/// [`DeviceParameters`] contract the driver-side factory and the
/// `dcs-sim-bus-device` binary read, seeded per `derivation`.
pub fn register_decls(
    model: &PlantModel,
    derivation: BankDerivation,
) -> Result<Vec<RegisterDecl>, EquivalenceError> {
    let mut decls = Vec::new();
    for device in &model.devices {
        let channels: BTreeMap<String, ValueKind> = device
            .channels
            .iter()
            .map(|(name, channel)| (name.clone(), channel.value_type))
            .collect();
        let parameters =
            DeviceParameters::parse(&device.parameters, &channels).map_err(|error| {
                EquivalenceError::Field(format!(
                    "device {} parameters rejected: {error}",
                    device.id.0
                ))
            })?;
        decls.extend(parameters.registers.iter().map(|(name, declaration)| {
            RegisterDecl {
                register: declaration.register,
                initial: match derivation {
                    BankDerivation::Neutral => neutral(channels[name.as_str()]),
                    BankDerivation::Declared => declaration
                        .initial
                        .unwrap_or_else(|| neutral(channels[name.as_str()])),
                },
            }
        }));
    }
    Ok(decls)
}

/// The point → register bindings a field-side
/// [`BusDriver`](dcs_sim_bus::BusDriver) attachment drives through,
/// derived from the overlay's own channel→register maps so the rig and
/// the controller cannot disagree about the mapping.
pub fn field_bindings(model: &PlantModel) -> Result<Vec<PointRegister>, EquivalenceError> {
    let mut registers: BTreeMap<(u64, String), u16> = BTreeMap::new();
    for device in &model.devices {
        let channels: BTreeMap<String, ValueKind> = device
            .channels
            .iter()
            .map(|(name, channel)| (name.clone(), channel.value_type))
            .collect();
        let parameters =
            DeviceParameters::parse(&device.parameters, &channels).map_err(|error| {
                EquivalenceError::Field(format!(
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
/// `from` (`In`) point. On a primary binding these are loopbacks
/// inside the shared sim map; on an overlay they are the cross-backend
/// routes a field-side rig carries.
pub fn field_wires(model: &PlantModel) -> Vec<(PointId, PointId)> {
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

/// Loads the primary document and resolves its driver side through the
/// standard [`DriverRegistry`], merging the optional declared
/// `dynamics` into the shared sim map exactly as
/// `dcs-plant-server --dynamics` does — each element lands through
/// `with_element` and revalidates the map.
pub fn local_variant(
    document: &str,
    dynamics: Option<&str>,
) -> Result<(PlantModel, FanoutDriver), EquivalenceError> {
    let model = PlantModel::load(document)?;
    let mut plan = resolve_drivers(&model, &DriverRegistry::standard())?;
    if let Some(source) = dynamics {
        for element in parse_dynamics(source)? {
            plan.sim_map = plan.sim_map.with_element(element);
            plan.sim_map.validate().map_err(|error| {
                EquivalenceError::Field(format!("dynamics merge rejected: {error}"))
            })?;
        }
    }
    Ok((model, plan.build()?))
}

/// Serves the overlay's register bank on an ephemeral port: the union
/// of the devices' declared register maps, seeded per `derivation`,
/// plus the optional register-addressed `dynamics` merged through
/// [`RegisterBank::with_dynamics`] — the construction
/// `dcs-sim-bus-device --dynamics` performs.
pub fn serve_bus_bank(
    document: &str,
    derivation: BankDerivation,
    dynamics: Option<&str>,
) -> Result<BusServer, EquivalenceError> {
    let declared = PlantModel::load(document)?;
    let decls = register_decls(&declared, derivation)?;
    let bank = match dynamics {
        Some(source) => RegisterBank::with_dynamics(decls, parse_dynamics(source)?)
            .map_err(|error| EquivalenceError::Field(format!("register bank rejected: {error}")))?,
        None => RegisterBank::new(decls)
            .map_err(|error| EquivalenceError::Field(format!("register bank rejected: {error}")))?,
    };
    BusServer::bind(("127.0.0.1", 0), bank).map_err(EquivalenceError::Io)
}

/// Binds a [`BusServer`] serving the overlay's register bank and
/// substitutes its bound address for the document's `placeholder`,
/// returning the resolved model with the server. The caller runs
/// `server.serve()` on its own thread — [`with_served_bank`] is that
/// caller for a variant run: resolving the `sim-bus` devices connects
/// and probes every mapped register, so the server must be serving
/// before assembly runs.
pub fn bus_variant(
    document: &str,
    placeholder: &str,
    derivation: BankDerivation,
    dynamics: Option<&str>,
) -> Result<(PlantModel, BusServer), EquivalenceError> {
    // The register maps are model data: the bank, the controller's
    // bindings, and the field feed all derive from the one fixture.
    let server = serve_bus_bank(document, derivation, dynamics)?;
    let addr = server.local_addr().map_err(EquivalenceError::Io)?;
    let model = PlantModel::load(&document.replace(placeholder, &addr.to_string()))?;
    Ok((model, server))
}

/// The bus variant's full serve pattern: binds and serves the
/// overlay's bank on a scope thread, substitutes its bound address for
/// `placeholder`, and calls `run` with the resolved model and the
/// server's address — where the run resolves the controller side,
/// attaches its field-side [`BusDriver`](dcs_sim_bus::BusDriver), and
/// drives the scenario. The server is shut down once `run` returns,
/// however it returns.
pub fn with_served_bank<R>(
    document: &str,
    placeholder: &str,
    derivation: BankDerivation,
    dynamics: Option<&str>,
    run: impl FnOnce(&PlantModel, SocketAddr) -> Result<R, EquivalenceError>,
) -> Result<R, EquivalenceError> {
    let (model, server) = bus_variant(document, placeholder, derivation, dynamics)?;
    thread::scope(|scope| {
        scope.spawn(|| server.serve());
        let result = (|| run(&model, server.local_addr()?))();
        server.shutdown();
        result
    })
}

/// Runs a scenario against an assembled variant through the
/// driven-mode machinery `dcs-controller --driven` uses: an unpaced
/// monitor whose `POST /scan` requests each run one scan plus the
/// cycle's `after_scan` wiring — here the caller's `boundary` hook.
/// `boundary(0)` runs before the first request so scan 1 reads the
/// seeded field; `boundary(t)` runs inside the request boundary
/// following scan `t`, so scan `t + 1` first observes its effects.
/// Actions scheduled for a tick are submitted while the run sits
/// between scans — the executor applies them at the coming scan's
/// head.
///
/// Determinism: requests are serialized by the monitor's lock, the
/// client waits for each response before issuing the next, and
/// tick-domain field sides make identical request sequences produce
/// identical runs.
pub fn driven_run<'d>(
    model: &PlantModel,
    driver: &'d FanoutDriver,
    scans: u64,
    actions: &[OperatorAction],
    boundary: impl Fn(u64) -> Result<(), String> + Send + Sync + 'd,
) -> Result<VariantRun, EquivalenceError> {
    boundary(0).map_err(EquivalenceError::Field)?;
    let executor = assemble(model, &dcs_controller::registry(), driver)?;
    let monitor = Monitor::bind(("127.0.0.1", 0), executor, model.signal_index())
        .map_err(EquivalenceError::Io)?
        .driven(Driven {
            track: None,
            after_scan: Some(Box::new(move |peer: &Peer<'d>| {
                // The scan cycle's field boundary — the position the
                // `--driven` controller's plant step occupies — what it
                // carries is the scenario's: ops, wire copies,
                // re-injections, the one field step.
                boundary(peer.tick().0)
            })),
        });
    let addr = monitor.local_addr();
    thread::scope(|scope| {
        scope.spawn(|| monitor.serve());
        let client = MonitorClient::new(addr);
        let result = (|| {
            let mut snapshots = Vec::with_capacity(scans as usize);
            let mut receipts = Vec::with_capacity(actions.len());
            for tick in 1..=scans {
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

#[cfg(test)]
mod tests {
    //! A third minimal runner built from scenario content alone: one
    //! field `In` point presented once by `sim-scripted` playback and
    //! once by a `sim-bus` register feed — the smallest new runner the
    //! shared orchestration can carry.
    use super::*;
    use dcs_core::IoDriver;
    use dcs_sim_bus::BusDriver;

    /// The scripted variant of the minimal shared plant: a script
    /// entry at driver tick `t` applies on the step after scan `t` and
    /// is first observed by scan `t + 1`.
    const SCRIPTED: &str = r#"{
        "version": 1,
        "devices": [
            {
                "id": 1,
                "kind": "sim-scripted",
                "parameters": {
                    "script": {
                        "level": [
                            { "tick": 0, "value": 4.0 },
                            { "tick": 2, "value": 9.0 }
                        ]
                    }
                },
                "channels": {
                    "level": { "direction": "in", "value_type": "float" }
                }
            }
        ],
        "io_points": [
            {
                "id": 10,
                "direction": "in",
                "value_type": "float",
                "channel": { "device": 1, "name": "level" }
            }
        ],
        "signals": [],
        "components": [],
        "connections": []
    }"#;

    /// The bus overlay: the same logical plant with the channel bound
    /// to a register; `PLACEHOLDER` is substituted at serve time.
    const BUS: &str = r#"{
        "version": 1,
        "devices": [
            {
                "id": 1,
                "kind": "sim-bus",
                "parameters": {
                    "address": "__BUS_ADDR__",
                    "registers": { "level": 10 }
                },
                "channels": {
                    "level": { "direction": "in", "value_type": "float" }
                }
            }
        ],
        "io_points": [
            {
                "id": 10,
                "direction": "in",
                "value_type": "float",
                "channel": { "device": 1, "name": "level" }
            }
        ],
        "signals": [],
        "components": [],
        "connections": []
    }"#;

    /// The address placeholder [`BUS`] carries.
    const PLACEHOLDER: &str = "__BUS_ADDR__";

    /// The minimal run's length in scans.
    const SCANS: u64 = 4;

    /// The fed point.
    const LEVEL: PointId = PointId(10);

    /// Simulated process time each scan advances.
    const SCAN_PERIOD: f64 = 0.1;

    /// The scenario's field program: the value `LEVEL` first presents
    /// to the scan at each listed tick.
    const PROGRAM: &[(u64, f64)] = &[(1, 4.0), (3, 9.0)];

    /// The minimal scripted runner: the declared script is the field
    /// side, so each boundary is only the `FanoutDriver::step` the
    /// `--driven` wiring installs.
    fn run_scripted() -> Result<VariantRun, EquivalenceError> {
        let (model, driver) = local_variant(SCRIPTED, None)?;
        let driver = &driver;
        driven_run(&model, driver, SCANS, &[], move |boundary| {
            if boundary == 0 {
                return Ok(());
            }
            driver
                .step(SCAN_PERIOD)
                .map_err(|error| format!("plant step failed: {error}"))
        })
    }

    /// The minimal bus runner: the register feed writes each program
    /// change inside the boundary preceding its scan.
    fn run_bus() -> Result<VariantRun, EquivalenceError> {
        with_served_bank(
            BUS,
            PLACEHOLDER,
            BankDerivation::Neutral,
            None,
            |model, addr| {
                let driver = resolve_drivers(model, &DriverRegistry::standard())?.build()?;
                let field = BusDriver::connect(addr, &field_bindings(model)?)?;
                let driver = &driver;
                let boundary = move |tick: u64| -> Result<(), String> {
                    if tick > 0 {
                        driver
                            .step(SCAN_PERIOD)
                            .map_err(|error| format!("plant step failed: {error}"))?;
                    }
                    for &(_, value) in PROGRAM.iter().filter(|(at, _)| *at == tick + 1) {
                        field.write(LEVEL, Value::Float(value)).map_err(|error| {
                            format!("register feed for point {} failed: {error}", LEVEL.0)
                        })?;
                    }
                    Ok(())
                };
                driven_run(model, driver, SCANS, &[], boundary)
            },
        )
    }

    #[test]
    fn a_runner_built_from_scenario_content_runs_identically_across_kinds() {
        let scripted = run_scripted().expect("the scripted run completes");
        let bus = run_bus().expect("the bus run completes");
        assert_eq!(scripted.snapshots.len() as u64, SCANS);
        assert_eq!(bus.snapshots.len() as u64, SCANS);

        for (index, (scripted, bus)) in scripted.snapshots.iter().zip(&bus.snapshots).enumerate() {
            let mut scripted = scripted.clone();
            let mut bus = bus.clone();
            scripted.io_health.driver = None;
            bus.io_health.driver = None;
            assert_eq!(
                serde_json::to_value(&scripted).unwrap(),
                serde_json::to_value(&bus).unwrap(),
                "scan {} snapshots diverged across transports",
                index + 1
            );
        }
        assert_eq!(scripted.journal, bus.journal);
        assert_eq!(scripted.receipts, bus.receipts);

        // The program landed on the scans it scheduled: 4.0 until the
        // tick-3 change, 9.0 after — pinned on one variant since
        // equality carries it to the other.
        for (scan, expected) in [(1u64, 4.0), (2, 4.0), (3, 9.0), (4, 9.0)] {
            let sample = scripted.snapshots[scan as usize - 1]
                .points
                .iter()
                .find(|telemetry| telemetry.point == LEVEL)
                .and_then(|telemetry| telemetry.sample.as_ref())
                .unwrap_or_else(|| panic!("scan {scan} serves no level sample"));
            assert_eq!(sample.value, Value::Float(expected), "level at scan {scan}");
        }
    }
}
