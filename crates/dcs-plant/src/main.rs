//! `dcs-plant-server`: the shared simulated plant as its own process.
//!
//! Usage: `dcs-plant-server <model-file> [--dynamics <file>] --listen <addr>`
//!        `dcs-plant-server <model-file> --check-dynamics <file>`
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
//! `second_order_lag`, `integrator`, `dead_time`, `noise`, `bool_flow`,
//! `flow_sum`, `scaled_flow`, `threshold`) standing in for field physics. The
//! document is kept out
//! of the model on purpose — process physics are simulation internals,
//! not part of the engineering contract the model shares with
//! controllers and the monitoring UI. Each element is validated as it is
//! merged, so a rejection names the element and the point it drives.
//!
//! `--check-dynamics` is the document's standalone preflight — the
//! mirror of `dcs-controller --check`'s model preflight: the model is
//! loaded and its channel map resolved, then every element is merged
//! and validated under the same rules `--dynamics` applies, each
//! rejection named by its element index and driving point, and the
//! process exits nonzero on any failure without binding a listener —
//! so a consumer's CI can validate the document without starting the
//! server.
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
    /// `--check-dynamics FILE`: preflight a dynamics document against
    /// the model's channel map and exit — the mode binds no listener.
    check_dynamics: Option<PathBuf>,
    /// The address the plant protocol is served on — `None` only under
    /// `--check-dynamics`, which binds nothing.
    listen: Option<String>,
}

const USAGE: &str = "\
Usage: dcs-plant-server <model-file> [--dynamics <file>] --listen <addr>
       dcs-plant-server <model-file> --check-dynamics <file>

Loads and validates the plant model, resolves its simulated channel map,
and serves the shared simulated plant on ADDR until signaled
(SIGINT/SIGTERM). With --check-dynamics the process instead preflights
the named dynamics document against the model's channel map and exits —
no listener binds.

  --listen ADDR    serve the plant protocol on ADDR (required unless
                   --check-dynamics is given); a port of 0 binds an
                   ephemeral port, reported on stderr
  --dynamics FILE  merge a JSON list of process-element declarations —
                   first_order_lag, second_order_lag, integrator,
                   dead_time, noise, bool_flow, flow_sum, scaled_flow,
                   threshold — into the simulated plant; process physics
                   are simulation internals, not part of the model
                   contract
  --check-dynamics FILE
                   preflight a dynamics document without serving: every
                   element is merged and validated under the same rules
                   --dynamics applies, each rejection named by its
                   element index and driving point; exits nonzero on any
                   rejection or on a model load failure
  -h, --help       show this text";

impl Options {
    fn parse(args: impl Iterator<Item = String>) -> Result<Self, String> {
        let mut model = None;
        let mut dynamics = None;
        let mut check_dynamics = None;
        let mut listen = None;
        let mut args = args;
        while let Some(arg) = args.next() {
            let mut value = |flag: &str| {
                args.next()
                    .ok_or_else(|| format!("{flag} requires a value"))
            };
            match arg.as_str() {
                "--dynamics" => dynamics = Some(PathBuf::from(value("--dynamics")?)),
                "--check-dynamics" => {
                    check_dynamics = Some(PathBuf::from(value("--check-dynamics")?));
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
        if check_dynamics.is_some() {
            // The preflight binds no listener, so the serve-mode options
            // have no meaning and are rejected rather than silently
            // ignored.
            let mut rejected = Vec::new();
            for (flag, present) in [
                ("--dynamics", dynamics.is_some()),
                ("--listen", listen.is_some()),
            ] {
                if present {
                    rejected.push(flag);
                }
            }
            if !rejected.is_empty() {
                return Err(format!(
                    "--check-dynamics validates the document without serving; {} do not apply",
                    rejected.join(", ")
                ));
            }
            return Ok(Self {
                model,
                dynamics: None,
                check_dynamics,
                listen: None,
            });
        }
        let listen = listen.ok_or_else(|| "missing --listen <addr>".to_string())?;
        Ok(Self {
            model,
            dynamics,
            check_dynamics,
            listen: Some(listen),
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

/// Parses a dynamics document into its declared elements — a JSON list
/// of [`ProcessElement`] declarations.
fn parse_dynamics(source: &str, path: &Path) -> Result<Vec<ProcessElement>, String> {
    serde_json::from_str(source)
        .map_err(|error| format!("invalid dynamics document {}: {error}", path.display()))
}

/// Merges one element into `map` under the merge-time rules: the element
/// lands, then the whole map validates. A rejection leaves `map`
/// unchanged and names the element — its position in the list and the
/// point it drives — rather than a byte offset.
fn merge_element(
    map: &ChannelMap,
    index: usize,
    element: ProcessElement,
) -> Result<ChannelMap, String> {
    let output = element.output();
    let merged = map.clone().with_element(element);
    merged.validate().map(|()| merged).map_err(|error| {
        format!(
            "dynamics element {index} (driving point {}) is invalid: {error}",
            output.0
        )
    })
}

/// Parses the dynamics document and merges its elements into `map`.
///
/// The document is a JSON list of [`ProcessElement`] declarations. Each
/// element is validated as it lands, so a rejection names the element —
/// its position in the list and the point it drives — rather than a byte
/// offset.
fn merge_dynamics(mut map: ChannelMap, source: &str, path: &Path) -> Result<ChannelMap, String> {
    for (index, element) in parse_dynamics(source, path)?.into_iter().enumerate() {
        map = merge_element(&map, index, element)?;
    }
    Ok(map)
}

/// The `--check-dynamics` preflight: every element goes through the same
/// merge-and-validate step `merge_dynamics` applies, but a rejection is
/// collected — and the element rolled back — instead of ending the run,
/// so one pass reports every malformed element by its index and driving
/// point. Returns the merged map on a clean document.
fn check_dynamics(
    mut map: ChannelMap,
    source: &str,
    path: &Path,
) -> Result<ChannelMap, Vec<String>> {
    // The map the elements merge into is checked first, with the same
    // message `SimDriver::new` raises at startup — a fault in it names
    // itself rather than arriving attributed to element 0.
    if let Err(error) = map.validate() {
        return Err(vec![format!(
            "simulated channel map is inconsistent: {error}"
        )]);
    }
    let elements = match parse_dynamics(source, path) {
        Ok(elements) => elements,
        Err(error) => return Err(vec![error]),
    };
    let mut errors = Vec::new();
    for (index, element) in elements.into_iter().enumerate() {
        match merge_element(&map, index, element) {
            Ok(merged) => map = merged,
            Err(error) => errors.push(error),
        }
    }
    if errors.is_empty() {
        Ok(map)
    } else {
        Err(errors)
    }
}

/// The element's serde kind tag — the snake_case variant name the
/// document writes; a new variant fails this match at compile time.
fn element_kind(element: &ProcessElement) -> &'static str {
    match element {
        ProcessElement::FirstOrderLag(_) => "first_order_lag",
        ProcessElement::SecondOrderLag(_) => "second_order_lag",
        ProcessElement::Integrator(_) => "integrator",
        ProcessElement::DeadTime(_) => "dead_time",
        ProcessElement::Noise(_) => "noise",
        ProcessElement::BoolFlow(_) => "bool_flow",
        ProcessElement::FlowSum(_) => "flow_sum",
        ProcessElement::ScaledFlow(_) => "scaled_flow",
        ProcessElement::Threshold(_) => "threshold",
    }
}

/// The `--check-dynamics` run: the same model load and channel-map
/// resolution `build` performs, then the dynamics preflight — no driver
/// constructed, no listener bound. On success returns the merged map for
/// the summary report.
fn check(options: &Options, path: &Path) -> Result<ChannelMap, Vec<String>> {
    let source = read_file(&options.model, "model").map_err(|error| vec![error])?;
    let model = PlantModel::load(&source).map_err(|error| vec![error.to_string()])?;
    let map = sim_channel_map(&model).map_err(|error| vec![error.to_string()])?;
    let source = read_file(path, "dynamics").map_err(|error| vec![error])?;
    check_dynamics(map, &source, path)
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
    let listen = options
        .listen
        .as_deref()
        .expect("Options::parse requires --listen outside --check-dynamics");
    PlantServer::bind(listen, driver).map_err(|error| format!("cannot bind {listen}: {error}"))
}

fn main() -> ExitCode {
    let options = match Options::parse(std::env::args().skip(1)) {
        Ok(options) => options,
        Err(error) => {
            eprintln!("error: {error}\n\n{USAGE}");
            return ExitCode::from(2);
        }
    };

    // The dynamics preflight: the same model load, channel-map
    // resolution, and per-element merge rules a serving run applies —
    // then the process exits at the report instead of binding a
    // listener.
    if let Some(path) = &options.check_dynamics {
        return match check(&options, path) {
            Ok(map) => {
                println!("check ok: {}", path.display());
                println!("elements: {}", map.elements.len());
                let mut kinds = std::collections::BTreeMap::new();
                for element in &map.elements {
                    *kinds.entry(element_kind(element)).or_insert(0usize) += 1;
                }
                for (kind, count) in kinds {
                    println!("  {kind}: {count}");
                }
                ExitCode::SUCCESS
            }
            Err(errors) => {
                for error in &errors {
                    eprintln!("error: {error}");
                }
                ExitCode::FAILURE
            }
        };
    }

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
