//! `dcs-sim-bus-device`: a register-mapped simulated fieldbus device as
//! its own process.
//!
//! Usage: `dcs-sim-bus-device <model-file> --device <id> [--listen <addr>]`
//!
//! The binary loads and validates the plant model, finds the declared
//! `sim-bus` device, and serves its register bank through
//! [`BusServer`]'s documented frame protocol — the same `parameters`
//! the driver-side factory parses, so a rig's controller and device
//! server read one declaration: each channel's register index and
//! optional power-on `initial` value come from the model, and `--listen`
//! defaults to the declared `"address"`. Register state changes only on
//! writes and the explicit `step` request, so identical request
//! sequences produce identical responses on every run.
//!
//! Load, validation, device-selection, and bind failures exit nonzero
//! naming the offending element or address; argument errors print usage
//! and exit 2. Shutdown is graceful: SIGINT or SIGTERM stops the accept
//! loop and closes client connections before the process exits.

use dcs_model::{DeviceId, PlantModel};
use dcs_sim_bus::{BusServer, DEVICE_KIND, DeviceParameters, RegisterBank, RegisterDecl};
use signal_hook::consts::{SIGINT, SIGTERM};
use signal_hook::iterator::Signals;
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::process::ExitCode;

/// Parsed command line.
struct Options {
    /// The plant model document naming the device.
    model: PathBuf,
    /// The `sim-bus` device to serve.
    device: u64,
    /// The address the register protocol is served on; defaults to the
    /// device's declared `address` parameter.
    listen: Option<String>,
}

const USAGE: &str = "\
Usage: dcs-sim-bus-device <model-file> --device <id> [--listen <addr>]

Loads and validates the plant model, builds the register bank the
declared sim-bus device's parameters describe — one register per
channel, at its declared index and optional initial value — and serves
it on ADDR until signaled (SIGINT/SIGTERM).

  --device ID     the sim-bus device to serve (required)
  --listen ADDR   serve the register protocol on ADDR; defaults to the
                  device's declared \"address\" parameter — a port of 0
                  binds an ephemeral port, reported on stderr
  -h, --help      show this text";

impl Options {
    fn parse(args: impl Iterator<Item = String>) -> Result<Self, String> {
        let mut model = None;
        let mut device = None;
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
            listen,
        })
    }
}

fn fail(message: impl std::fmt::Display) -> ExitCode {
    eprintln!("error: {message}");
    ExitCode::FAILURE
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
    if device.kind != DEVICE_KIND {
        return Err(format!(
            "device {} has kind {:?}, which is not {DEVICE_KIND:?}",
            id.0, device.kind
        ));
    }
    let channels: BTreeMap<String, dcs_core::ValueKind> = device
        .channels
        .iter()
        .map(|(name, channel)| (name.clone(), channel.value_type))
        .collect();
    let parameters = DeviceParameters::parse(&device.parameters, &channels)
        .map_err(|error| format!("device {} has invalid parameters: {error}", id.0))?;
    let decls = parameters
        .registers
        .iter()
        .map(|(name, declaration)| RegisterDecl {
            register: declaration.register,
            initial: declaration
                .initial
                .unwrap_or_else(|| neutral(channels[name.as_str()])),
        });
    let bank = RegisterBank::new(decls).map_err(|error| error.to_string())?;
    let listen = options
        .listen
        .clone()
        .unwrap_or_else(|| parameters.address.clone());
    let server = BusServer::bind(listen.as_str(), bank)
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
