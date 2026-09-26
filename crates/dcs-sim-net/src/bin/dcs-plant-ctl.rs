//! `dcs-plant-ctl`: a development-side operator tool for a running
//! [`PlantServer`](dcs_sim_net::PlantServer).
//!
//! The tool speaks the plant server's documented wire protocol — one
//! subcommand per request — to inspect and perturb a live demonstration:
//! a controller plus the monitoring page attached to one shared simulated
//! plant. Injecting a fault, writing a field input, or forcing a plant
//! step makes quality faults, journal transitions, and alarm states
//! appear on the monitoring surface without recompiling fixtures. It is
//! development tooling, not part of the operator contract.
//!
//! Each invocation connects, sends one request, and prints the server's
//! answer as JSON. The field-mutating commands — `write` and `step` —
//! ride the tool's own conditional claim: `ensure_writer` under a tool
//! token, never `claim_writer`, so a plant already serving a field owner
//! keeps it and the command fences exactly as a bare mutation would,
//! then `release_writer` hands the claim back so the tool cannot leave
//! a dead token standing against the field owner's re-arm. An
//! unreachable address exits nonzero naming the address; a request the
//! server rejects exits nonzero with the reported error; malformed
//! arguments print usage and exit nonzero — never a panic.

use dcs_core::{IoDriver, PointId, Quality, QualityReason, Value};
use dcs_sim::Fault;
use dcs_sim_net::{PlantResponse, RemoteDriver, RemoteError};
use std::process::ExitCode;

const USAGE: &str = "\
usage: dcs-plant-ctl <addr> <command> [args]

commands:
  list                      list every point the plant serves
  read <point>              read a point's latest sample
  write <point> <value>     write a field input; <value> is true|false,
                            an integer, or a float
  fault <point> <fault>     inject a fault: disconnected, timeout,
                            uncertain[:<reason>], or bad[:<reason>]
  clear-fault <point>       remove an injected fault
  step <dt>                 advance the plant one tick of <dt> time units
  ping                      the liveness probe — the server answers its
                            plant tick; the container health check's
                            request

quality reasons: unspecified, substituted, stale, out_of_range,
communication_fault, device_fault, configuration_fault

example: dcs-plant-ctl 127.0.0.1:4700 fault 10 bad:device_fault";

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match run(&args) {
        Ok(output) => {
            println!("{output}");
            ExitCode::SUCCESS
        }
        Err(message) => {
            eprintln!("{message}");
            ExitCode::FAILURE
        }
    }
}

/// Runs one command line, returning the server's answer for stdout or
/// the error message for stderr.
fn run(args: &[String]) -> Result<String, String> {
    let (addr, action) = parse(args)?;
    let driver = RemoteDriver::connect(addr).map_err(|error| {
        format!("dcs-plant-ctl: cannot reach the plant server at {addr}: {error}")
    })?;
    let response =
        execute(&driver, &action).map_err(|error| format!("dcs-plant-ctl: {addr}: {error}"))?;
    serde_json::to_string_pretty(&response)
        .map_err(|error| format!("dcs-plant-ctl: cannot encode the server response: {error}"))
}

/// The owner token the tool's field mutations ride under — claimed
/// conditionally (`ensure_writer`, never `claim_writer`) so the tool
/// joins an unclaimed field or its own standing claim but can never
/// preempt a live field owner, and released after the mutation so the
/// claim dies with the invocation rather than fencing the owner's
/// re-arm.
const TOOL_OWNER: u64 = 0x6463_732d_706c_7463; // "dcs-pltc" — a tool, not a controller

/// One parsed command line: the validated action to send.
enum Action {
    List,
    Read(PointId),
    Write(PointId, Value),
    InjectFault(PointId, Fault),
    ClearFault(PointId),
    Step(f64),
    Ping,
}

/// Validates the command line without touching the network, so malformed
/// arguments fail with usage text even when no server is listening.
fn parse(args: &[String]) -> Result<(&str, Action), String> {
    let usage = |error: String| format!("{error}\n{USAGE}");
    let [addr, command, rest @ ..] = args else {
        return Err(usage("expected a server address and a command".to_string()));
    };
    let action = match (command.as_str(), rest) {
        ("list", []) => Action::List,
        ("read", [point]) => Action::Read(parse_point(point).map_err(usage)?),
        ("write", [point, value]) => Action::Write(
            parse_point(point).map_err(usage)?,
            parse_value(value).map_err(usage)?,
        ),
        ("fault", [point, fault]) => Action::InjectFault(
            parse_point(point).map_err(usage)?,
            parse_fault(fault).map_err(usage)?,
        ),
        ("clear-fault", [point]) => Action::ClearFault(parse_point(point).map_err(usage)?),
        ("step", [dt]) => Action::Step(parse_dt(dt).map_err(usage)?),
        ("ping", []) => Action::Ping,
        ("list" | "read" | "write" | "fault" | "clear-fault" | "step" | "ping", _) => {
            return Err(usage(format!("wrong arguments for {command:?}")));
        }
        _ => return Err(usage(format!("unknown command {command:?}"))),
    };
    Ok((addr.as_str(), action))
}

/// Sends the action's one request and returns the server's answer.
///
/// `read`/`write` go through the [`IoDriver`] boundary like a
/// controller's accesses; a server-reported [`IoError`] is re-wrapped as
/// the [`RemoteError`] the protocol carried it in, so every failure
/// surfaces through one display path.
fn execute(driver: &RemoteDriver, action: &Action) -> Result<PlantResponse, RemoteError> {
    match *action {
        Action::List => driver
            .list_points()
            .map(|points| PlantResponse::Points { points }),
        Action::Read(point) => driver
            .read(point)
            .map(|sample| PlantResponse::Sample { sample })
            .map_err(RemoteError::Io),
        Action::Write(point, value) => claimed(driver, || {
            driver
                .write(point, value)
                .map(|()| PlantResponse::Done)
                .map_err(RemoteError::Io)
        }),
        Action::InjectFault(point, fault) => driver
            .inject_fault(point, fault)
            .map(|()| PlantResponse::Done),
        Action::ClearFault(point) => driver.clear_fault(point).map(|()| PlantResponse::Done),
        Action::Step(dt) => claimed(driver, || {
            driver.step(dt).map(|tick| PlantResponse::Stepped { tick })
        }),
        Action::Ping => driver.ping().map(|tick| PlantResponse::Alive { tick }),
    }
}

/// Runs a field-mutating command inside the tool's conditional claim:
/// `ensure_writer` grants only while the field is unclaimed or already
/// the tool's — a plant serving a field owner fences the command, the
/// same refusal a bare mutation would have met — and `release_writer`
/// hands the field back to `unclaimed` afterward, so the invocation's
/// claim cannot outlive its connection and fence a returning owner's
/// re-arm. A release failure cannot undo an applied mutation, so it
/// warns on stderr rather than falsifying the command's result.
fn claimed(
    driver: &RemoteDriver,
    mutation: impl FnOnce() -> Result<PlantResponse, RemoteError>,
) -> Result<PlantResponse, RemoteError> {
    driver.ensure_writer(TOOL_OWNER)?;
    let result = mutation();
    if let Err(error) = driver.release_writer() {
        eprintln!(
            "dcs-plant-ctl: the mutation applied but releasing the tool's field claim failed: {error}"
        );
    }
    result
}

fn parse_point(arg: &str) -> Result<PointId, String> {
    arg.parse::<u64>()
        .map(PointId)
        .map_err(|_| format!("invalid point id {arg:?}: expected a non-negative integer"))
}

/// Parses a `<value>` argument: `true`/`false` are `Bool`, an integer
/// literal `Int`, anything else parsing as `f64` `Float` — so `1` is an
/// `Int` write and a `Float` point takes `1.0`. A non-finite float has
/// no wire representation and is rejected here.
fn parse_value(arg: &str) -> Result<Value, String> {
    Ok(match arg {
        "true" => Value::Bool(true),
        "false" => Value::Bool(false),
        _ => {
            if let Ok(int) = arg.parse::<i64>() {
                Value::Int(int)
            } else if let Ok(float) = arg.parse::<f64>()
                && float.is_finite()
            {
                Value::Float(float)
            } else {
                return Err(format!(
                    "invalid value {arg:?}: expected true|false, an integer, or a float"
                ));
            }
        }
    })
}

fn parse_dt(arg: &str) -> Result<f64, String> {
    match arg.parse::<f64>() {
        Ok(dt) if dt.is_finite() => Ok(dt),
        _ => Err(format!("invalid step {arg:?}: expected a finite number")),
    }
}

/// Parses a `<fault>` argument: `disconnected` and `timeout` are the
/// error faults; `uncertain[:<reason>]` and `bad[:<reason>]` are quality
/// faults, defaulting to `unspecified` when the reason is omitted.
fn parse_fault(arg: &str) -> Result<Fault, String> {
    let (kind, reason) = match arg.split_once(':') {
        Some((kind, reason)) => (kind, Some(reason)),
        None => (arg, None),
    };
    match (kind, reason) {
        ("disconnected", None) => Ok(Fault::Disconnected),
        ("timeout", None) => Ok(Fault::Timeout),
        ("uncertain", reason) => Ok(Fault::Quality(Quality::Uncertain(parse_reason(reason)?))),
        ("bad", reason) => Ok(Fault::Quality(Quality::Bad(parse_reason(reason)?))),
        _ => Err(format!(
            "invalid fault {arg:?}: expected disconnected, timeout, uncertain[:<reason>], or bad[:<reason>]"
        )),
    }
}

fn parse_reason(arg: Option<&str>) -> Result<QualityReason, String> {
    match arg {
        None | Some("unspecified") => Ok(QualityReason::Unspecified),
        Some("substituted") => Ok(QualityReason::Substituted),
        Some("stale") => Ok(QualityReason::Stale),
        Some("out_of_range") => Ok(QualityReason::OutOfRange),
        Some("communication_fault") => Ok(QualityReason::CommunicationFault),
        Some("device_fault") => Ok(QualityReason::DeviceFault),
        Some("configuration_fault") => Ok(QualityReason::ConfigurationFault),
        Some(other) => Err(format!("invalid quality reason {other:?}")),
    }
}
