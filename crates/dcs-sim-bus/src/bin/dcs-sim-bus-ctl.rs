//! `dcs-sim-bus-ctl`: a development-side tool for a running
//! [`BusServer`](dcs_sim_bus::BusServer) — the register protocol's
//! client-side companion to `dcs-sim-bus-device`, and the bus analogue
//! of `dcs-sim-net`'s `dcs-plant-ctl`.
//!
//! The tool speaks the device server's documented register protocol
//! over the [`BusDriver`] client — one subcommand per request — to
//! inspect and perturb a scripted rig's `sim-bus` devices without a
//! controller in the loop: list the served registers, read and write
//! them by address, inject and clear a register's reported quality —
//! the bus analogue of `dcs-plant-ctl`'s `fault`/`clear-fault` — and
//! step the bank's logical tick explicitly. It is development tooling,
//! not part of the operator contract.
//!
//! Each invocation connects, sends its request, and prints the server's
//! answer as JSON. `write` first reads the register to learn its
//! declared kind, so the value literal is interpreted the way the
//! register holds it — `1` on a float register stores `1.0`. An
//! unreachable address exits nonzero naming the address; a server
//! error answer exits nonzero naming the reported error; malformed
//! arguments print usage and exit nonzero — never a panic.

use dcs_core::{Quality, QualityReason, Tick, Value, ValueKind};
use dcs_sim_bus::{BusDriver, BusError, BusRequest, BusResponse, LinkError};
use std::fmt;
use std::process::ExitCode;

const USAGE: &str = "\
usage: dcs-sim-bus-ctl <addr> <command> [args]

commands:
  list                      list every register the device serves: address,
                            declared kind, and stored sample
  read <register>           read a register's stored sample
  write <register> <value>  write a register; <value> is parsed as the
                            register's declared kind — true|false for bool,
                            an integer for int, a number for float — so
                            `write 4 1` stores 1.0 on a float register; a
                            literal of another kind is still sent, and the
                            server names the kind mismatch
  step [n]                  advance the device's logical tick n times
                            (default: 1)
  inject-quality <register> <quality>
                            stamp a register's stored sample with a
                            declared quality — good,
                            uncertain[:<reason>], or bad[:<reason>] —
                            until cleared or a real write overwrites
                            it; injection is open to every attachment,
                            the writer claim does not fence it
  clear-quality <register>  restore a register's stored sample to good
                            quality

quality reasons: unspecified, substituted, stale, out_of_range,
communication_fault, device_fault, configuration_fault

example: dcs-sim-bus-ctl 127.0.0.1:5502 write 4 2.5";

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
    let driver = BusDriver::connect(addr, &[]).map_err(|error| {
        format!("dcs-sim-bus-ctl: cannot reach the device server at {addr}: {error}")
    })?;
    let response =
        execute(&driver, &action).map_err(|error| format!("dcs-sim-bus-ctl: {addr}: {error}"))?;
    serde_json::to_string_pretty(&response)
        .map_err(|error| format!("dcs-sim-bus-ctl: cannot encode the server response: {error}"))
}

/// One parsed command line: the validated request to send.
enum Action {
    List,
    Read(u16),
    /// The register, the literal as given, and the generic value parse
    /// already accepted — the declared-kind interpretation waits for
    /// the register's kind, which only the server knows.
    Write(u16, String, Value),
    Step(u64),
    InjectQuality(u16, Quality),
    ClearQuality(u16),
}

/// One command's failure.
enum Failure {
    /// The exchange itself failed; see [`LinkError`].
    Link(LinkError),
    /// The server answered a [`BusResponse::Error`].
    Server(BusError),
    /// The answer decodes but is not the request's response — the peer
    /// is not a device server speaking this protocol.
    Unexpected(BusResponse),
}

impl fmt::Display for Failure {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Link(error) => error.fmt(f),
            Self::Server(error) => write!(f, "device server error: {error}"),
            Self::Unexpected(response) => write!(
                f,
                "unexpected response {response:?}: the peer is not speaking the register protocol"
            ),
        }
    }
}

/// Validates the command line without touching the network, so
/// malformed arguments fail with usage text even when no server is
/// listening.
fn parse(args: &[String]) -> Result<(&str, Action), String> {
    let usage = |error: String| format!("{error}\n{USAGE}");
    let [addr, command, rest @ ..] = args else {
        return Err(usage("expected a server address and a command".to_string()));
    };
    let action = match (command.as_str(), rest) {
        ("list", []) => Action::List,
        ("read", [register]) => Action::Read(parse_register(register).map_err(usage)?),
        ("write", [register, value]) => Action::Write(
            parse_register(register).map_err(usage)?,
            value.clone(),
            parse_literal(value).map_err(usage)?,
        ),
        ("step", []) => Action::Step(1),
        ("step", [count]) => Action::Step(parse_count(count).map_err(usage)?),
        ("inject-quality", [register, quality]) => Action::InjectQuality(
            parse_register(register).map_err(usage)?,
            parse_quality(quality).map_err(usage)?,
        ),
        ("clear-quality", [register]) => {
            Action::ClearQuality(parse_register(register).map_err(usage)?)
        }
        ("list" | "read" | "write" | "step" | "inject-quality" | "clear-quality", _) => {
            return Err(usage(format!("wrong arguments for {command:?}")));
        }
        _ => return Err(usage(format!("unknown command {command:?}"))),
    };
    Ok((addr.as_str(), action))
}

/// Sends `request`, mapping a transport failure to [`Failure::Link`]
/// and the server's error answer to [`Failure::Server`].
fn exchange(driver: &BusDriver, request: &BusRequest) -> Result<BusResponse, Failure> {
    match driver.request(request) {
        Ok(BusResponse::Error { error }) => Err(Failure::Server(error)),
        Ok(response) => Ok(response),
        Err(error) => Err(Failure::Link(error)),
    }
}

/// The server's answer, if it carries the request's expected variant.
fn expect(
    driver: &BusDriver,
    request: &BusRequest,
    expected: impl FnOnce(&BusResponse) -> bool,
) -> Result<BusResponse, Failure> {
    let response = exchange(driver, request)?;
    if expected(&response) {
        Ok(response)
    } else {
        Err(Failure::Unexpected(response))
    }
}

/// Runs the command's exchange and returns the server's answer — for
/// `step [n]`, the last of `n` step answers.
fn execute(driver: &BusDriver, action: &Action) -> Result<BusResponse, Failure> {
    match action {
        Action::List => expect(driver, &BusRequest::ListRegisters, |response| {
            matches!(response, BusResponse::Registers { .. })
        }),
        Action::Read(register) => expect(
            driver,
            &BusRequest::ReadRegister {
                register: *register,
            },
            |response| matches!(response, BusResponse::Sample { .. }),
        ),
        Action::Write(register, text, literal) => {
            // Read the register first: its declared kind is the stored
            // sample's kind — kind-checked writes can never change it —
            // and it decides how the literal is interpreted. An
            // unmapped register's error answer surfaces here, before
            // any write.
            let BusResponse::Sample { sample } = expect(
                driver,
                &BusRequest::ReadRegister {
                    register: *register,
                },
                |response| matches!(response, BusResponse::Sample { .. }),
            )?
            else {
                unreachable!("the response matched the sample variant")
            };
            let value = parse_for_kind(sample.value.kind(), text, *literal);
            expect(
                driver,
                &BusRequest::WriteRegister {
                    register: *register,
                    value,
                },
                |response| matches!(response, BusResponse::Written { .. }),
            )
        }
        Action::Step(count) => {
            let mut answer = BusResponse::Stepped { tick: Tick::ZERO };
            for _ in 0..*count {
                answer = expect(driver, &BusRequest::Step, |response| {
                    matches!(response, BusResponse::Stepped { .. })
                })?;
            }
            Ok(answer)
        }
        Action::InjectQuality(register, quality) => expect(
            driver,
            &BusRequest::InjectQuality {
                register: *register,
                quality: *quality,
            },
            |response| matches!(response, BusResponse::Done),
        ),
        Action::ClearQuality(register) => expect(
            driver,
            &BusRequest::ClearQuality {
                register: *register,
            },
            |response| matches!(response, BusResponse::Done),
        ),
    }
}

fn parse_register(arg: &str) -> Result<u16, String> {
    arg.parse::<u16>()
        .map_err(|_| format!("invalid register {arg:?}: expected an address 0..=65535"))
}

/// Parses an `inject-quality` `<quality>` argument: `good`, or
/// `uncertain[:<reason>]` and `bad[:<reason>]` defaulting to
/// `unspecified` when the reason is omitted — the same quality
/// vocabulary `dcs-plant-ctl`'s `fault` command accepts, minus the
/// error faults a register bank does not model.
fn parse_quality(arg: &str) -> Result<Quality, String> {
    let (severity, reason) = match arg.split_once(':') {
        Some((severity, reason)) => (severity, Some(reason)),
        None => (arg, None),
    };
    match (severity, reason) {
        ("good", None) => Ok(Quality::Good),
        ("uncertain", reason) => Ok(Quality::Uncertain(parse_reason(reason)?)),
        ("bad", reason) => Ok(Quality::Bad(parse_reason(reason)?)),
        _ => Err(format!(
            "invalid quality {arg:?}: expected good, uncertain[:<reason>], or bad[:<reason>]"
        )),
    }
}

/// Parses a `:<reason>` qualifier — the named `QualityReason`s, with
/// `unspecified` and an absent reason interchangeable.
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

/// Parses `step`'s `[n]`: a positive count of ticks to advance.
fn parse_count(arg: &str) -> Result<u64, String> {
    match arg.parse::<u64>() {
        Ok(count) if count >= 1 => Ok(count),
        _ => Err(format!(
            "invalid step count {arg:?}: expected a positive integer"
        )),
    }
}

/// Parses a `<value>` argument as *some* value literal — `true`/`false`
/// `Bool`, an integer `Int`, a finite float `Float` — so `write`'s
/// declared-kind interpretation has a fallback and a value that parses
/// as nothing fails here, before any connection. A non-finite float
/// has no wire representation and is rejected.
fn parse_literal(arg: &str) -> Result<Value, String> {
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

/// Interprets `arg` as the register's declared `kind` — `1` on a float
/// register means `1.0`, which the generic literal parse would have
/// made an `Int` the server refuses. Anything not parsing as `kind`
/// falls back to `literal`, the generic parse `parse` already
/// accepted, so a deliberately other-kinded value like `true` still
/// goes to the device, which answers its named kind mismatch.
fn parse_for_kind(kind: ValueKind, arg: &str, literal: Value) -> Value {
    match kind {
        ValueKind::Bool => match arg {
            "true" => Value::Bool(true),
            "false" => Value::Bool(false),
            _ => literal,
        },
        ValueKind::Int => arg.parse::<i64>().map(Value::Int).unwrap_or(literal),
        ValueKind::Float => arg
            .parse::<f64>()
            .ok()
            .filter(|value| value.is_finite())
            .map(Value::Float)
            .unwrap_or(literal),
    }
}
