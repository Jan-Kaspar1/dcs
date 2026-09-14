//! `dcs-plant-server`: the shared simulated plant as its own process.
//!
//! Usage: `dcs-plant-server <model-file> [--dynamics <file>] --listen <addr>`
//!
//! The binary loads and validates the plant model, resolves the simulated
//! channel map through [`dcs_assembly::sim_channel_map`] — so the served
//! plant carries every declared `io_point` plus the internal points and
//! loopbacks the model's wiring implies — and serves it through
//! [`PlantServer`]'s documented protocol on `--listen` until SIGINT or
//! SIGTERM stops it. Controller processes and the `dcs-plant-ctl`
//! development tool attach through
//! [`RemoteDriver`](dcs_sim_net::RemoteDriver) and observe one shared,
//! deterministically stepped field: plant state changes only on explicit
//! `step` requests, so identical request sequences produce identical
//! responses on every run.
//!
//! `--dynamics` merges a plant-side dynamics document: a JSON list of
//! [`ProcessElement`] declarations (`first_order_lag`,
//! `second_order_lag`, `integrator`, `dead_time`) standing in for field
//! physics. The document is kept out
//! of the model on purpose — process physics are simulation internals,
//! not part of the engineering contract the model shares with
//! controllers and the monitoring UI. Each element is validated as it is
//! merged, so a rejection names the element and the point it drives.
//!
//! Load, validation, and bind failures exit nonzero naming the offending
//! element or address; argument errors print usage and exit 2. Shutdown
//! is graceful: a signal stops the accept loop and closes client
//! connections before the process exits.

use dcs_assembly::sim_channel_map;
use dcs_model::PlantModel;
use dcs_sim::{ChannelMap, ProcessElement, SimDriver};
use dcs_sim_net::PlantServer;
use signal_hook::consts::{SIGINT, SIGTERM};
use signal_hook::iterator::Signals;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

/// Parsed command line.
struct Options {
    /// The plant model document to serve.
    model: PathBuf,
    /// The optional process-element list to merge into the channel map.
    dynamics: Option<PathBuf>,
    /// The address the plant protocol is served on.
    listen: String,
}

const USAGE: &str = "\
Usage: dcs-plant-server <model-file> [--dynamics <file>] --listen <addr>

Loads and validates the plant model, resolves its simulated channel map,
and serves the shared simulated plant on ADDR until signaled
(SIGINT/SIGTERM).

  --listen ADDR    serve the plant protocol on ADDR (required); a port of
                   0 binds an ephemeral port, reported on stderr
  --dynamics FILE  merge a JSON list of process-element declarations —
                   first_order_lag, second_order_lag, integrator,
                   dead_time — into the simulated plant; process physics
                   are simulation internals, not part of the model
                   contract
  -h, --help       show this text";

impl Options {
    fn parse(args: impl Iterator<Item = String>) -> Result<Self, String> {
        let mut model = None;
        let mut dynamics = None;
        let mut listen = None;
        let mut args = args;
        while let Some(arg) = args.next() {
            let mut value = |flag: &str| {
                args.next()
                    .ok_or_else(|| format!("{flag} requires a value"))
            };
            match arg.as_str() {
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
        let listen = listen.ok_or_else(|| "missing --listen <addr>".to_string())?;
        Ok(Self {
            model,
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

/// Parses the dynamics document and merges its elements into `map`.
///
/// The document is a JSON list of [`ProcessElement`] declarations. Each
/// element is validated as it lands, so a rejection names the element —
/// its position in the list and the point it drives — rather than a byte
/// offset.
fn merge_dynamics(mut map: ChannelMap, source: &str, path: &Path) -> Result<ChannelMap, String> {
    let elements: Vec<ProcessElement> = serde_json::from_str(source)
        .map_err(|error| format!("invalid dynamics document {}: {error}", path.display()))?;
    for (index, element) in elements.into_iter().enumerate() {
        let output = element.output();
        map = map.with_element(element);
        if let Err(error) = map.validate() {
            return Err(format!(
                "dynamics element {index} (driving point {}) is invalid: {error}",
                output.0
            ));
        }
    }
    Ok(map)
}

/// Loads the inputs and binds the server — every failure the process can
/// report before serving begins, as a message naming the responsible
/// element or address.
fn build(options: &Options) -> Result<PlantServer, String> {
    let source = read_file(&options.model, "model")?;
    let model = PlantModel::load(&source).map_err(|error| error.to_string())?;
    let mut channel_map = sim_channel_map(&model).map_err(|error| error.to_string())?;
    if let Some(path) = &options.dynamics {
        let source = read_file(path, "dynamics")?;
        channel_map = merge_dynamics(channel_map, &source, path)?;
    }
    let driver = SimDriver::new(channel_map)
        .map_err(|error| format!("simulated channel map is inconsistent: {error}"))?;
    PlantServer::bind(options.listen.as_str(), driver)
        .map_err(|error| format!("cannot bind {}: {error}", options.listen))
}

fn main() -> ExitCode {
    let options = match Options::parse(std::env::args().skip(1)) {
        Ok(options) => options,
        Err(error) => {
            eprintln!("error: {error}\n\n{USAGE}");
            return ExitCode::from(2);
        }
    };
    let server = match build(&options) {
        Ok(server) => server,
        Err(error) => return fail(error),
    };
    // With a port of 0 the bound address is learnable only here; stderr
    // keeps the protocol's only output on the wire.
    match server.local_addr() {
        Ok(addr) => eprintln!("listening on {addr}"),
        Err(error) => return fail(format!("cannot report the bound address: {error}")),
    }
    let mut signals = match Signals::new([SIGINT, SIGTERM]) {
        Ok(signals) => signals,
        Err(error) => return fail(format!("cannot install signal handlers: {error}")),
    };
    std::thread::scope(|scope| {
        scope.spawn(|| server.serve());
        // Park until SIGINT or SIGTERM, then stop the accept loop and
        // force-close client connections; the scope's join completes the
        // graceful shutdown.
        let _ = signals.forever().next();
        server.shutdown();
    });
    eprintln!("stopped");
    ExitCode::SUCCESS
}
