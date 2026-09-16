//! `dcs-sim-bus-device`: a register-mapped simulated fieldbus device as
//! its own process.
//!
//! Usage: `dcs-sim-bus-device <model-file> --device <id> [--dynamics <file>] [--listen <addr>]`
//!
//! The binary loads and validates the plant model, finds the declared
//! `sim-bus` or `sim-cyclic` device, and serves its register bank
//! through [`BusServer`]'s documented frame protocol — the same
//! `parameters` the driver-side factory parses, so a rig's controller
//! and device server read one declaration: each channel's register
//! index and optional power-on `initial` value come from the model, and
//! `--listen` defaults to the declared `"address"`. A `sim-cyclic`
//! device's declared station layout is served with the bank so a
//! station-attributed scripted exchange withholds the device's own
//! registers. Register state changes only on writes, quality injection,
//! the explicit `step` request, and the `exchange` request's staged
//! outputs, so identical request sequences produce identical responses
//! on every run.
//!
//! `--dynamics` merges a device-side dynamics document: a JSON list of
//! [`ProcessElement`] declarations (`first_order_lag`,
//! `second_order_lag`, `integrator`, `dead_time`, `noise`, `bool_flow`,
//! `flow_sum`, `scaled_flow`, `threshold`) standing in for field physics — the same
//! seam `dcs-plant-server --dynamics` serves, with the document's
//! point-valued fields carrying register addresses. Each element is
//! validated as it merges, so a rejection names the element and the
//! register it drives. Element state is bank state — the served field,
//! not checkpointed controller state — and each `step` request's `dt`
//! advances it.
//!
//! Load, validation, device-selection, and bind failures exit nonzero
//! naming the offending element or address; argument errors print usage
//! and exit 2. Shutdown is graceful: SIGINT or SIGTERM stops the accept
//! loop and closes client connections before the process exits.

use dcs_model::{DeviceId, PlantModel};
use dcs_sim_bus::{
    BusServer, CYCLIC_DEVICE_KIND, CyclicDeviceParameters, DEVICE_KIND, DeviceParameters,
    ProcessElement, RegisterBank, RegisterDecl,
};
use signal_hook::consts::{SIGINT, SIGTERM};
use signal_hook::iterator::Signals;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

/// Parsed command line.
struct Options {
    /// The plant model document naming the device.
    model: PathBuf,
    /// The `sim-bus` or `sim-cyclic` device to serve.
    device: u64,
    /// The optional process-element list to merge over the bank's
    /// registers.
    dynamics: Option<PathBuf>,
    /// The address the register protocol is served on; defaults to the
    /// device's declared `address` parameter.
    listen: Option<String>,
}

const USAGE: &str = "\
Usage: dcs-sim-bus-device <model-file> --device <id> [--dynamics <file>] [--listen <addr>]

Loads and validates the plant model, builds the register bank the
declared sim-bus or sim-cyclic device's parameters describe — one
register per channel, at its declared index and optional initial value
— and serves it on ADDR until signaled (SIGINT/SIGTERM). A sim-cyclic
device's station layout is served with the bank, so a scripted
short-station exchange withholds its declared registers.

  --device ID      the sim-bus or sim-cyclic device to serve (required)
  --dynamics FILE  merge a JSON list of process-element declarations —
                   first_order_lag, second_order_lag, integrator,
                   dead_time, noise, bool_flow, flow_sum, scaled_flow,
                   threshold — over the bank's registers; an element's
                   point-valued fields carry register addresses
  --listen ADDR    serve the register protocol on ADDR; defaults to the
                   device's declared \"address\" parameter — a port of 0
                   binds an ephemeral port, reported on stderr
  -h, --help       show this text";

impl Options {
    fn parse(args: impl Iterator<Item = String>) -> Result<Self, String> {
        let mut model = None;
        let mut device = None;
        let mut dynamics = None;
        let mut listen = None;
        let mut args = args;
        while let Some(arg) = args.next() {
            let mut value = |flag: &str| {
                args.next()
                    .ok_or_else(|| format!("{flag} requires a value"))
            };
            match arg.as_str() {
                "--device" => {
                    let id = value("--device")?;
                    device = Some(
                        id.parse::<u64>()
                            .map_err(|error| format!("--device {id:?}: {error}"))?,
                    );
                }
                "--dynamics" => dynamics = Some(PathBuf::from(value("--dynamics")?)),
                "--listen" => listen = Some(value("--listen")?),
                "-h" | "--help" => {
                    println!("{USAGE}");
                    std::process::exit(0);
                }
                _ if arg.starts_with('-') => {
                    return Err(format!("unknown option {arg:?}"));
                }
                _ if model.is_none() => model = Some(PathBuf::from(arg)),
                _ => return Err(format!("unexpected argument {arg:?}")),
            }
        }
        let model = model.ok_or_else(|| "missing <model-file>".to_string())?;
        let device = device.ok_or_else(|| "missing --device <id>".to_string())?;
        Ok(Self {
            model,
            device,
            dynamics,
            listen,
        })
    }
}

fn fail(message: impl std::fmt::Display) -> ExitCode {
    eprintln!("error: {message}");
    ExitCode::FAILURE
}

fn read_file(path: &Path, what: &str) -> Result<String, String> {
    std::fs::read_to_string(path)
        .map_err(|error| format!("cannot read {what} file {}: {error}", path.display()))
}

/// Parses the dynamics document — a JSON list of [`ProcessElement`]
/// declarations whose point-valued fields carry register addresses —
/// for merging over the bank's registers. The parse itself is the only
/// failure here; the bank's merge names the element a rejection
/// belongs to.
fn load_dynamics(path: &Path) -> Result<Vec<ProcessElement>, String> {
    let source = read_file(path, "dynamics")?;
    serde_json::from_str(&source)
        .map_err(|error| format!("invalid dynamics document {}: {error}", path.display()))
}

/// Loads the inputs and binds the server — every failure the process
/// can report before serving begins, as a message naming the
/// responsible element or address.
fn build(options: &Options) -> Result<(BusServer, String), String> {
    let source = std::fs::read_to_string(&options.model).map_err(|error| {
        format!(
            "cannot read model file {}: {error}",
            options.model.display()
        )
    })?;
    let model = PlantModel::load(&source).map_err(|error| error.to_string())?;
    let id = DeviceId(options.device);
    let device = model
        .devices
        .iter()
        .find(|device| device.id == id)
        .ok_or_else(|| format!("model declares no device {}", id.0))?;
    let channels: BTreeMap<String, dcs_core::ValueKind> = device
        .channels
        .iter()
        .map(|(name, channel)| (name.clone(), channel.value_type))
        .collect();
    // Both served kinds share the register bank; a cyclic device adds
    // its station layout to the server so station-attributed short
    // exchanges withhold the device's own declaration.
    let (decls, address, stations) = match device.kind.as_str() {
        DEVICE_KIND => {
            let parameters = DeviceParameters::parse(&device.parameters, &channels)
                .map_err(|error| format!("device {} has invalid parameters: {error}", id.0))?;
            let decls: Vec<RegisterDecl> = parameters
                .registers
                .iter()
                .map(|(name, declaration)| RegisterDecl {
                    register: declaration.register,
                    initial: declaration
                        .initial
                        .unwrap_or_else(|| neutral(channels[name.as_str()])),
                })
                .collect();
            (decls, parameters.address, BTreeMap::new())
        }
        CYCLIC_DEVICE_KIND => {
            let parameters = CyclicDeviceParameters::parse(&device.parameters, &channels)
                .map_err(|error| format!("device {} has invalid parameters: {error}", id.0))?;
            let decls = parameters.register_decls(&channels);
            let stations = parameters.station_registers();
            (decls, parameters.address, stations)
        }
        other => {
            return Err(format!(
                "device {} has kind {other:?}, which is neither {DEVICE_KIND:?} nor {CYCLIC_DEVICE_KIND:?}",
                id.0
            ));
        }
    };
    let bank = match &options.dynamics {
        Some(path) => RegisterBank::with_dynamics(decls, load_dynamics(path)?)
            .map_err(|error| error.to_string())?,
        None => RegisterBank::new(decls).map_err(|error| error.to_string())?,
    };
    let listen = options.listen.clone().unwrap_or(address);
    let server = BusServer::bind_stationed(listen.as_str(), bank, stations)
        .map_err(|error| format!("cannot bind {listen}: {error}"))?;
    Ok((server, listen))
}

/// A channel's value before the first write: the neutral value of its
/// declared kind.
fn neutral(kind: dcs_core::ValueKind) -> dcs_core::Value {
    match kind {
        dcs_core::ValueKind::Bool => dcs_core::Value::Bool(false),
        dcs_core::ValueKind::Int => dcs_core::Value::Int(0),
        dcs_core::ValueKind::Float => dcs_core::Value::Float(0.0),
    }
}

fn main() -> ExitCode {
    let options = match Options::parse(std::env::args().skip(1)) {
        Ok(options) => options,
        Err(error) => {
            eprintln!("error: {error}\n\n{USAGE}");
            return ExitCode::from(2);
        }
    };
    let (server, listen) = match build(&options) {
        Ok(built) => built,
        Err(error) => return fail(error),
    };
    // With a port of 0 the bound address is learnable only here; stderr
    // keeps the protocol's only output on the wire.
    match server.local_addr() {
        Ok(addr) => eprintln!(
            "serving device {} on {addr} (declared {listen})",
            options.device
        ),
        Err(error) => return fail(format!("cannot report the bound address: {error}")),
    }
    let mut signals = match Signals::new([SIGINT, SIGTERM]) {
        Ok(signals) => signals,
        Err(error) => return fail(format!("cannot install signal handlers: {error}")),
    };
    std::thread::scope(|scope| {
        scope.spawn(|| server.serve());
        // Park until SIGINT or SIGTERM, then stop the accept loop and
        // force-close client connections; the scope's join completes
        // the graceful shutdown.
        let _ = signals.forever().next();
        server.shutdown();
    });
    eprintln!("stopped");
    ExitCode::SUCCESS
}
