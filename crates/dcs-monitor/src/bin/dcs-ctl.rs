//! `dcs-ctl`: an operator command-line tool for a running controller's
//! monitor — the controller-side counterpart of `dcs-plant-ctl`.
//!
//! Where `dcs-plant-ctl` speaks the plant server's protocol to perturb
//! the simulated world, `dcs-ctl` speaks the monitoring contract — the
//! HTTP+JSON transport decision's endpoints, through
//! [`MonitorClient`] — to inspect and command the controller itself:
//! the read-side subcommands fetch the served contract payloads, the
//! write-side subcommands submit the [`Command`] their arguments
//! describe, and `promote`/`demote`/`scan` drive the redundancy and
//! externally-paced-run endpoints. A running controller — paced,
//! driven, active, or standby — can be queried and commanded from a
//! terminal without the browser page. It is operator and development
//! tooling over the existing surface: no new endpoints, no new wire
//! types.
//!
//! Every subcommand prints the server's answer as JSON on stdout — the
//! decoded contract payload re-encoded, so a malformed answer is a
//! decode error rather than passed through — including a rejected
//! command's [`CommandReceipt`]. Exit status is nonzero on failure with
//! stderr naming it: a transport failure names the monitor address, a
//! rejected receipt names the [`CommandError`], a refused promotion or
//! demotion names the [`SwitchError`], and malformed arguments print
//! usage. The tool never panics.
//!
//! Point and parameter values parse per the declared
//! [`ValueKind`]: a point's kind comes from the served `SignalIndex`,
//! a parameter's from the served `ComponentDescriptor` — so `write 10
//! 1` writes `1.0` to a `Float` point, and `write` to a `Bool` point
//! accepts only `true`/`false`. When the target is not declared at all
//! — a point absent from the index, a component or parameter absent
//! from the descriptors — the literal's own kind is sent instead, so
//! the server's receipt still answers with the contract's named
//! rejection. `invoke`'s `<name>=<value>` arguments follow the same
//! rule — parsed per the declared request kind when the served schema
//! declares it, as literals when it does not — the settled
//! [`CommandReceipt`] staying the authority over every submission.
//!
//! `invoke <component> <command> [<name>=<value>]...` exercises the
//! declared-command surface: the served `SchemaView`'s per-instance
//! [`BlockInterface`](dcs_core::BlockInterface) supplies the command's
//! request schema, each `name=value` pair parses per the declared
//! argument's [`ValueKind`], and the submission is the
//! [`Command::Invoke`] variant — the same receipted path the page's
//! interface surface drives, reachable without a browser. A component
//! or command the schema does not declare keeps the literal-kind
//! fallback, so the server's `unknown_component`/`unknown_command`
//! rejection still answers by name; a kind's own refusal — the
//! `command_refused` an unavailable declared command settles — arrives
//! as the ordinary rejected receipt.
//!
//! The interface surface's read half is `schema`, `resources`, and
//! `events`: `schema` prints the served
//! [`SchemaView`](dcs_core::SchemaView) — the block-interface
//! registry, every served instance's declared measurements,
//! configuration, state, commands, and events; `resources
//! [<component>]` prints the matching live half, the served
//! [`ResourceView`](dcs_core::ResourceView) verbatim or the named
//! instance's `ComponentResources` entry — its measurements and
//! state with quality, current configuration, and each command's
//! `available`/`refusal` beside the attributed events; and `events
//! [<component>]` prints that view's per-instance `events` — the
//! retained journal tail attributed to the named component beside its
//! routed `History`/`Latest` emission records, each entry's
//! `retention` marking its store — or every
//! component's list keyed by name when the argument is absent. A
//! name the served registry does not carry fails the invocation
//! naming it — the read is a lookup, never a submission, so there is
//! no receipt to answer with.
//!
//! Every receipted submission can declare the actor identity the
//! command-path audit-attribution contract journals — the tool-side
//! source beside the page's `?operator=` parameter: `--actor <name>`
//! on a write-side subcommand declares it for that invocation, and the
//! `DCS_ACTOR` environment variable is the configured default the flag
//! overrides. A declared actor rides the attributed envelope
//! `POST /command` accepts onto the returned [`CommandReceipt`] and so
//! into the journaled `CommandSettled` entry; an invocation declaring
//! neither submits the bare [`Command`] body and journals
//! unattributed, exactly as before. `promote`/`demote` are switch
//! requests outside the `Command` path — the landed contract carries
//! no actor on them, so they take no flag and journal their role
//! change unattributed.

use dcs_core::{
    Command, CommandOutcome, CommandReceipt, ComponentResources, PointId, ResourceEvent,
    ResourceView, RoleReport, SwitchError, Value, ValueKind,
};
use dcs_monitor::MonitorClient;
use serde::Serialize;
use std::collections::BTreeMap;
use std::io;
use std::net::{SocketAddr, ToSocketAddrs};
use std::process::ExitCode;

const USAGE: &str = "\
usage: dcs-ctl <addr> <command> [args]

read commands:
  snapshot                    the executor's current TelemetrySnapshot
  signals                     the served SignalIndex
  schema                      the served block-interface registry — each
                              instance's declared ports, parameters,
                              commands, and events
  events [<component>]        the recently emitted events attributed to
                              <component>, or every component's keyed
                              by name
  resources [<component>]     the served ResourceView — every
                              instance's live resource state — or the
                              named component's ComponentResources
                              entry
  role                        the instance's RoleReport
  receipts                    the executor's receipt log
  journal [--since <seq>]     journal entries with a seq above <seq>
  history --point <id>... [--since <seq>]
                              retained samples of the selected points

operator commands:
  write <point> <value> [--actor <name>]
                              write <value> to a writable point
  set-parameter <component> <name> <value> [--actor <name>]
                              tune a component's declared parameter
  force <point> <value> [--actor <name>]
                              pin a writable In point to <value>
  unforce <point> [--actor <name>]
                              release a forced point
  invoke <component> <command> [<name>=<value>]... [--actor <name>]
                              invoke a component's declared command;
                              arguments parse per the served schema's
                              declared request kinds
  promote                     promote a converged standby to active
  demote                      demote the field-owning peer to standby
  scan <n>                    run <n> scans; only a driven, unpaced
                              instance accepts — a paced one refuses

actor: --actor <name> declares the identity the command's receipt and
journaled CommandSettled entry carry; DCS_ACTOR is the configured
default the flag overrides, and an invocation declaring neither submits
unattributed — never a rejection. promote/demote carry no actor: the
switch-request contract has no field for one.

values: <value> parses per the declared value kind — true|false for
Bool, an integer for Int, a finite number for Float — declared by the
served signal index for points, by the component's descriptors for
parameters, and by the served schema's request schema for invoke
arguments. Command subcommands print the CommandReceipt; a rejected
receipt still prints and the exit status is nonzero naming the
CommandError. promote/demote print the resulting RoleReport; a refusal
exits nonzero naming the SwitchError. An unreachable <addr> exits
nonzero naming it; malformed arguments print this text.

example: dcs-ctl 127.0.0.1:8080 write 10 2.5";

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match run(&args) {
        Ok(answer) => {
            println!("{answer}");
            ExitCode::SUCCESS
        }
        Err(failure) => {
            // The server's answer still deserves stdout when one
            // exists — a rejected receipt is the answer to a command
            // subcommand — before the named failure on stderr.
            if let Some(answer) = failure.answer {
                println!("{answer}");
            }
            eprintln!("{}", failure.message);
            ExitCode::FAILURE
        }
    }
}

/// A failed invocation: the message for stderr, plus the server's
/// answer when one still deserves stdout — a rejected receipt is the
/// answer to a command subcommand, printed so the run's record shows
/// exactly what the controller answered.
struct Failure {
    answer: Option<String>,
    message: String,
}

impl Failure {
    /// A failure carrying only a message.
    fn message(message: String) -> Self {
        Self {
            answer: None,
            message,
        }
    }

    /// A malformed-command-line failure: the message followed by the
    /// usage text.
    fn usage(message: String) -> Self {
        Self::message(format!("{message}\n{USAGE}"))
    }
}

/// Runs one command line, returning the server's answer for stdout or
/// the failure for stderr.
fn run(args: &[String]) -> Result<String, Failure> {
    let (addr_arg, action) = parse(args).map_err(Failure::message)?;
    let addr = resolve(addr_arg)?;
    let client = MonitorClient::new(addr);
    execute(&client, addr, &action)
}

/// Resolves the `<addr>` argument to a socket address — `host:port`
/// names resolve through `ToSocketAddrs`. An address that does not
/// parse is malformed input and prints usage.
fn resolve(arg: &str) -> Result<SocketAddr, Failure> {
    let mut addrs = arg
        .to_socket_addrs()
        .map_err(|error| Failure::usage(format!("invalid monitor address {arg:?}: {error}")))?;
    addrs.next().ok_or_else(|| {
        Failure::usage(format!(
            "invalid monitor address {arg:?}: resolves to nothing"
        ))
    })
}

/// One parsed command line: the request to send. `<value>` arguments
/// stay unparsed here — the declared kind the served contract reports
/// rules their parse, which only happens once the server answers.
enum Action {
    Snapshot,
    Signals,
    Schema,
    Events {
        /// `None` prints every served component's list.
        component: Option<String>,
    },
    Resources {
        /// `None` prints the whole served `ResourceView`.
        component: Option<String>,
    },
    Role,
    Receipts,
    Journal {
        since: u64,
    },
    History {
        points: Vec<PointId>,
        since: u64,
    },
    Write {
        point: PointId,
        text: String,
        actor: Option<String>,
    },
    SetParameter {
        component: String,
        name: String,
        text: String,
        actor: Option<String>,
    },
    Force {
        point: PointId,
        text: String,
        actor: Option<String>,
    },
    Unforce {
        point: PointId,
        actor: Option<String>,
    },
    Invoke {
        component: String,
        command: String,
        /// The `<name>=<value>` pairs, unparsed — the served schema's
        /// declared request kinds rule their parse at execution.
        arguments: Vec<(String, String)>,
        actor: Option<String>,
    },
    Promote,
    Demote,
    Scan {
        scans: u64,
    },
}

/// Validates the command line without touching the network, so
/// malformed arguments fail with usage text even when no monitor is
/// listening.
fn parse(args: &[String]) -> Result<(&str, Action), String> {
    let usage = |error: String| format!("{error}\n{USAGE}");
    let [addr, command, rest @ ..] = args else {
        return Err(usage(
            "expected a monitor address and a command".to_string(),
        ));
    };
    let action = match (command.as_str(), rest) {
        ("snapshot", []) => Action::Snapshot,
        ("signals", []) => Action::Signals,
        ("schema", []) => Action::Schema,
        ("events", rest) => match rest {
            [] => Action::Events { component: None },
            [component] if !component.starts_with("--") => Action::Events {
                component: Some((*component).to_string()),
            },
            _ => return Err(usage(format!("wrong arguments for {command:?}"))),
        },
        ("resources", rest) => match rest {
            [] => Action::Resources { component: None },
            [component] if !component.starts_with("--") => Action::Resources {
                component: Some((*component).to_string()),
            },
            _ => return Err(usage(format!("wrong arguments for {command:?}"))),
        },
        ("role", []) => Action::Role,
        ("receipts", []) => Action::Receipts,
        ("journal", rest) => Action::Journal {
            since: parse_since(rest).map_err(usage)?,
        },
        ("history", rest) => parse_history(rest).map_err(usage)?,
        ("write", rest) => {
            let (positional, actor) = command_args(rest).map_err(usage)?;
            match positional.as_slice() {
                [point, value] => Action::Write {
                    point: parse_point(point).map_err(usage)?,
                    text: (*value).to_string(),
                    actor,
                },
                _ => return Err(usage(format!("wrong arguments for {command:?}"))),
            }
        }
        ("set-parameter", rest) => {
            let (positional, actor) = command_args(rest).map_err(usage)?;
            match positional.as_slice() {
                [component, name, value] => Action::SetParameter {
                    component: (*component).to_string(),
                    name: (*name).to_string(),
                    text: (*value).to_string(),
                    actor,
                },
                _ => return Err(usage(format!("wrong arguments for {command:?}"))),
            }
        }
        ("force", rest) => {
            let (positional, actor) = command_args(rest).map_err(usage)?;
            match positional.as_slice() {
                [point, value] => Action::Force {
                    point: parse_point(point).map_err(usage)?,
                    text: (*value).to_string(),
                    actor,
                },
                _ => return Err(usage(format!("wrong arguments for {command:?}"))),
            }
        }
        ("unforce", rest) => {
            let (positional, actor) = command_args(rest).map_err(usage)?;
            match positional.as_slice() {
                [point] => Action::Unforce {
                    point: parse_point(point).map_err(usage)?,
                    actor,
                },
                _ => return Err(usage(format!("wrong arguments for {command:?}"))),
            }
        }
        ("invoke", rest) => {
            let (positional, actor) = command_args(rest).map_err(usage)?;
            match positional.as_slice() {
                [component, command, arguments @ ..] => Action::Invoke {
                    component: (*component).to_string(),
                    command: (*command).to_string(),
                    arguments: parse_invoke_arguments(arguments).map_err(usage)?,
                    actor,
                },
                _ => return Err(usage(format!("wrong arguments for {command:?}"))),
            }
        }
        ("promote", []) => Action::Promote,
        ("demote", []) => Action::Demote,
        ("scan", [scans]) => Action::Scan {
            scans: parse_count(scans).map_err(usage)?,
        },
        (
            "snapshot" | "signals" | "schema" | "role" | "receipts" | "promote" | "demote" | "scan",
            _,
        ) => {
            return Err(usage(format!("wrong arguments for {command:?}")));
        }
        _ => return Err(usage(format!("unknown command {command:?}"))),
    };
    Ok((addr.as_str(), action))
}

/// The `journal` flag list: an optional `--since <seq>` cursor.
fn parse_since(rest: &[String]) -> Result<u64, String> {
    match rest {
        [] => Ok(0),
        [flag, value] if flag == "--since" => parse_seq(value),
        _ => Err("journal takes [--since <seq>]".to_string()),
    }
}

/// The `history` flag list: one or more `--point <id>` selections and
/// an optional `--since <seq>` cursor, in any order.
fn parse_history(rest: &[String]) -> Result<Action, String> {
    let mut points = Vec::new();
    let mut since = 0;
    let mut args = rest.iter();
    while let Some(flag) = args.next() {
        match flag.as_str() {
            "--point" => points.push(parse_point(
                args.next()
                    .ok_or_else(|| "--point expects a point id".to_string())?,
            )?),
            "--since" => {
                since = parse_seq(
                    args.next()
                        .ok_or_else(|| "--since expects a sequence number".to_string())?,
                )?
            }
            other => return Err(format!("unknown history argument {other:?}")),
        }
    }
    if points.is_empty() {
        return Err("history selects points: pass at least one --point <id>".to_string());
    }
    Ok(Action::History { points, since })
}

/// A write-side subcommand's argument list split into its positional
/// arguments and the declared actor: `--actor <name>` anywhere in the
/// list supplies it — the same flag convention `history` scans — and
/// `DCS_ACTOR` is the configured default an absent flag leaves in
/// place. A flag missing its name, a repeated `--actor`, or any other
/// `--` flag is malformed usage.
fn command_args(rest: &[String]) -> Result<(Vec<&str>, Option<String>), String> {
    let mut positional = Vec::new();
    let mut actor = None;
    let mut args = rest.iter();
    while let Some(arg) = args.next() {
        if arg == "--actor" {
            let name = args
                .next()
                .ok_or_else(|| "--actor expects a name".to_string())?;
            if actor.replace(name.clone()).is_some() {
                return Err("--actor takes a single name".to_string());
            }
        } else if arg.starts_with("--") {
            return Err(format!("unknown flag {arg:?}"));
        } else {
            positional.push(arg.as_str());
        }
    }
    Ok((positional, actor.or_else(configured_actor)))
}

/// The `invoke` subcommand's trailing `<name>=<value>` pairs — split
/// into key and unparsed text here, parsed per the declared argument
/// kinds once the served schema answers. A pair with no `=`, an empty
/// name, or a repeated name is malformed usage, never a submission.
fn parse_invoke_arguments(args: &[&str]) -> Result<Vec<(String, String)>, String> {
    let mut parsed = Vec::new();
    for arg in args {
        let (name, value) = arg
            .split_once('=')
            .filter(|(name, _)| !name.is_empty())
            .ok_or_else(|| format!("invalid invoke argument {arg:?}: expected <name>=<value>"))?;
        if parsed
            .iter()
            .any(|(seen, _): &(String, String)| seen == name)
        {
            return Err(format!("repeated invoke argument {name:?}"));
        }
        parsed.push((name.to_string(), value.to_string()));
    }
    Ok(parsed)
}

/// The environment's configured default actor — a non-empty
/// `DCS_ACTOR` — the `--actor` flag's fallback on the receipted
/// subcommands.
fn configured_actor() -> Option<String> {
    std::env::var("DCS_ACTOR")
        .ok()
        .filter(|name| !name.is_empty())
}

fn parse_point(arg: &str) -> Result<PointId, String> {
    arg.parse::<u64>()
        .map(PointId)
        .map_err(|_| format!("invalid point id {arg:?}: expected a non-negative integer"))
}

fn parse_seq(arg: &str) -> Result<u64, String> {
    arg.parse::<u64>()
        .map_err(|_| format!("invalid seq cursor {arg:?}: expected a non-negative integer"))
}

fn parse_count(arg: &str) -> Result<u64, String> {
    arg.parse::<u64>()
        .map_err(|_| format!("invalid scan count {arg:?}: expected a non-negative integer"))
}

/// Sends the action's request and returns the server's answer for
/// stdout, or the failure naming what went wrong.
fn execute(client: &MonitorClient, addr: SocketAddr, action: &Action) -> Result<String, Failure> {
    match action {
        Action::Snapshot => print_json(&client.snapshot().map_err(|e| transport(addr, e))?, addr),
        Action::Signals => print_json(&client.signals().map_err(|e| transport(addr, e))?, addr),
        Action::Schema => print_json(&client.schema().map_err(|e| transport(addr, e))?, addr),
        Action::Events { component } => {
            // The resource view's per-instance `events` is the served
            // event record — the retained journal tail attributed to
            // each instance beside its routed `History`/`Latest`
            // emission records, each entry's `retention` marking the
            // store it came from — so between-scans entries (a refused
            // command, say) appear ahead of the stamped publication.
            let view = client.resources().map_err(|e| transport(addr, e))?;
            match component {
                Some(name) => print_json(&resource_entry(&view, name, addr)?.events, addr),
                None => {
                    let events: BTreeMap<&str, &[ResourceEvent]> = view
                        .components
                        .iter()
                        .map(|entry| (entry.name.as_str(), entry.events.as_slice()))
                        .collect();
                    print_json(&events, addr)
                }
            }
        }
        Action::Resources { component } => {
            // The served view verbatim, or the named instance's
            // entry — whatever command state the publication carries,
            // this accessor only surfaces it.
            let view = client.resources().map_err(|e| transport(addr, e))?;
            match component {
                Some(name) => print_json(resource_entry(&view, name, addr)?, addr),
                None => print_json(&view, addr),
            }
        }
        Action::Role => print_json(&client.role().map_err(|e| transport(addr, e))?, addr),
        Action::Receipts => print_json(&client.receipts().map_err(|e| transport(addr, e))?, addr),
        Action::Journal { since } => print_json(
            &client.journal(*since).map_err(|e| transport(addr, e))?,
            addr,
        ),
        Action::History { points, since } => print_json(
            &client
                .history(points, *since)
                .map_err(|e| transport(addr, e))?,
            addr,
        ),
        Action::Write { point, text, actor } => {
            let declared = declared_point_kind(client, addr, *point)?;
            let value = parse_operand(declared, text).map_err(Failure::usage)?;
            command(
                client,
                addr,
                Command::WriteValue {
                    point: *point,
                    // The declared kind when the index knows the point,
                    // else the literal's own — the server's rejection
                    // names an undeclared point either way.
                    kind: declared.unwrap_or_else(|| value.kind()),
                    value,
                },
                actor.as_deref(),
            )
        }
        Action::SetParameter {
            component,
            name,
            text,
            actor,
        } => {
            let kind = declared_parameter_kind(client, addr, component, name)?;
            let value = parse_operand(kind, text).map_err(Failure::usage)?;
            command(
                client,
                addr,
                Command::SetParameter {
                    component: component.clone(),
                    name: name.clone(),
                    value,
                },
                actor.as_deref(),
            )
        }
        Action::Force { point, text, actor } => {
            let declared = declared_point_kind(client, addr, *point)?;
            let value = parse_operand(declared, text).map_err(Failure::usage)?;
            command(
                client,
                addr,
                Command::ForcePoint {
                    point: *point,
                    kind: declared.unwrap_or_else(|| value.kind()),
                    value,
                },
                actor.as_deref(),
            )
        }
        Action::Unforce { point, actor } => command(
            client,
            addr,
            Command::UnforcePoint { point: *point },
            actor.as_deref(),
        ),
        Action::Invoke {
            component,
            command: name,
            arguments,
            actor,
        } => {
            let arguments = invoke_arguments(client, addr, component, name, arguments)?;
            command(
                client,
                addr,
                Command::Invoke {
                    component: component.clone(),
                    command: name.clone(),
                    arguments,
                },
                actor.as_deref(),
            )
        }
        Action::Promote => switchover(client, addr, "/promote", "promote"),
        Action::Demote => switchover(client, addr, "/demote", "demote"),
        Action::Scan { scans } => print_json(
            &client.advance(*scans).map_err(|e| transport(addr, e))?,
            addr,
        ),
    }
}

/// One instance's [`ComponentResources`] in the served view — the
/// name lookup `resources <component>` and `events <component>`
/// share: a name the served registry does not carry fails the
/// invocation naming it, the read being a lookup rather than a
/// submission.
fn resource_entry<'a>(
    view: &'a ResourceView,
    name: &str,
    addr: SocketAddr,
) -> Result<&'a ComponentResources, Failure> {
    view.components
        .iter()
        .find(|entry| entry.name == name)
        .ok_or_else(|| {
            Failure::message(format!(
                "dcs-ctl: {addr}: no served component named {name:?}"
            ))
        })
}

/// A transport-level failure, naming the monitor the request went to.
fn transport(addr: SocketAddr, error: io::Error) -> Failure {
    Failure::message(format!("dcs-ctl: {addr}: {error}"))
}

/// Encodes the decoded answer for stdout — `serde_json` pretty output,
/// deterministic for the same payload.
fn print_json<T: Serialize>(answer: &T, addr: SocketAddr) -> Result<String, Failure> {
    serde_json::to_string_pretty(answer).map_err(|error| {
        Failure::message(format!(
            "dcs-ctl: {addr}: cannot encode the server answer: {error}"
        ))
    })
}

/// The point's declared kind: the served `SignalIndex` is the
/// declaration, so `GET /signals` resolves it before the command is
/// built. A point absent from the index has no declared kind — `None`
/// sends the literal's own kind so the server's `unknown_point`
/// rejection still answers by name.
fn declared_point_kind(
    client: &MonitorClient,
    addr: SocketAddr,
    point: PointId,
) -> Result<Option<ValueKind>, Failure> {
    let index = client.signals().map_err(|e| transport(addr, e))?;
    Ok(index.get(point).map(|entry| entry.value_type))
}

/// The parameter's declared kind: the served snapshot's component
/// descriptors are the declaration, so `GET /snapshot` resolves the
/// component's parameter list before the command is built. A component
/// or parameter absent from the descriptors has no declared kind —
/// `None` sends the literal's own kind so the server's
/// `unknown_component`/`unknown_parameter` rejection still answers by
/// name.
fn declared_parameter_kind(
    client: &MonitorClient,
    addr: SocketAddr,
    component: &str,
    name: &str,
) -> Result<Option<ValueKind>, Failure> {
    let snapshot = client.snapshot().map_err(|e| transport(addr, e))?;
    Ok(snapshot
        .descriptors
        .iter()
        .find(|descriptor| descriptor.name == component)
        .and_then(|descriptor| {
            descriptor
                .parameters
                .iter()
                .find(|parameter| parameter.name == name)
        })
        .map(|parameter| parameter.kind))
}

/// The invocation's typed argument map: `GET /schema` resolves the
/// component's declared command — the served `SchemaView` is the
/// declaration the same instance's page surface reads — and each
/// `<name>=<value>` pair parses per the request argument's declared
/// [`ValueKind`], strictly. A component or command absent from the
/// served schema has no request to parse against — each literal parses
/// as it reads, so the server's `unknown_component`/`unknown_command`
/// rejection still answers by name; a pair naming an argument the
/// declared request does not carry likewise keeps the literal's kind,
/// the receipted path staying the authority.
fn invoke_arguments(
    client: &MonitorClient,
    addr: SocketAddr,
    component: &str,
    command: &str,
    arguments: &[(String, String)],
) -> Result<BTreeMap<String, Value>, Failure> {
    let schema = client.schema().map_err(|e| transport(addr, e))?;
    let spec = schema
        .interfaces
        .iter()
        .find(|entry| entry.name == component)
        .and_then(|entry| {
            entry
                .interface
                .commands
                .iter()
                .find(|spec| spec.name == command)
        });
    let mut parsed = BTreeMap::new();
    for (name, text) in arguments {
        let kind = spec.and_then(|spec| {
            spec.request
                .iter()
                .find(|argument| argument.name == *name)
                .map(|argument| argument.kind)
        });
        parsed.insert(
            name.clone(),
            parse_operand(kind, text).map_err(Failure::usage)?,
        );
    }
    Ok(parsed)
}

/// Parses a `<value>` argument: per the declared kind when the target
/// declares one, else as the literal reads — `true`/`false` a `Bool`,
/// an integer literal an `Int`, a finite float a `Float`.
fn parse_operand(kind: Option<ValueKind>, text: &str) -> Result<Value, String> {
    match kind {
        Some(kind) => parse_value_as(kind, text),
        None => parse_literal(text),
    }
}

/// Parses `<value>` as the declared kind, strictly: a `Bool` takes
/// `true`/`false`, an `Int` a signed 64-bit integer, a `Float` a finite
/// number — a non-finite float has no wire representation, so `nan`
/// and `inf` are malformed input, not values.
fn parse_value_as(kind: ValueKind, arg: &str) -> Result<Value, String> {
    match kind {
        ValueKind::Bool => match arg {
            "true" => Ok(Value::Bool(true)),
            "false" => Ok(Value::Bool(false)),
            _ => Err(format!("invalid value {arg:?}: a Bool takes true|false")),
        },
        ValueKind::Int => arg
            .parse::<i64>()
            .map(Value::Int)
            .map_err(|_| format!("invalid value {arg:?}: an Int takes a signed integer")),
        ValueKind::Float => match arg.parse::<f64>() {
            Ok(value) if value.is_finite() => Ok(Value::Float(value)),
            _ => Err(format!(
                "invalid value {arg:?}: a Float takes a finite number"
            )),
        },
    }
}

/// Parses `<value>` as the literal reads, for a target with no
/// declaration to parse against — the server's named rejection still
/// answers the command.
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

/// Submits a command and renders its receipt: the receipt JSON is the
/// answer — printed whether the command was accepted or rejected — and
/// a rejection additionally fails the invocation, naming the
/// [`CommandError`]. `actor` is the submitter's declared identity from
/// `--actor`/`DCS_ACTOR`: `Some` sends the attributed envelope the
/// receipt and journaled `CommandSettled` carry, `None` sends the bare
/// `Command` body and journals unattributed.
fn command(
    client: &MonitorClient,
    addr: SocketAddr,
    command: Command,
    actor: Option<&str>,
) -> Result<String, Failure> {
    let receipt: CommandReceipt = client
        .command_as(&command, actor)
        .map_err(|e| transport(addr, e))?;
    let answer = print_json(&receipt, addr)?;
    match &receipt.outcome {
        CommandOutcome::Rejected { reason } => Err(Failure {
            answer: Some(answer),
            message: format!(
                "dcs-ctl: {addr}: command rejected: {}: {reason}",
                variant_name(reason)
            ),
        }),
        _ => Ok(answer),
    }
}

/// `POST /promote` or `POST /demote`: prints the post-change
/// [`RoleReport`] on success; a `409` refusal decodes the named
/// [`SwitchError`] the endpoint answered and fails the invocation with
/// it.
fn switchover(
    client: &MonitorClient,
    addr: SocketAddr,
    path: &str,
    verb: &str,
) -> Result<String, Failure> {
    let (status, body) = client
        .request("POST", path, None)
        .map_err(|e| transport(addr, e))?;
    match status {
        200 => {
            let report: RoleReport = serde_json::from_str(&body).map_err(|error| {
                Failure::message(format!(
                    "dcs-ctl: {addr}: cannot decode the POST {path} answer: {error}: {body}"
                ))
            })?;
            print_json(&report, addr)
        }
        409 => {
            let error: SwitchError = serde_json::from_str(&body).map_err(|error| {
                Failure::message(format!(
                    "dcs-ctl: {addr}: cannot decode the POST {path} refusal: {error}: {body}"
                ))
            })?;
            Err(Failure::message(format!(
                "dcs-ctl: {addr}: {verb} refused: {}: {error}",
                variant_name(&error)
            )))
        }
        status => Err(Failure::message(format!(
            "dcs-ctl: {addr}: POST {path} answered HTTP {status}: {body}"
        ))),
    }
}

/// A contract error's wire variant name — `not_writable`,
/// `already_active` — so a refusal reads as the same identifier the
/// JSON contract uses. Externally tagged serde enums encode as a
/// single-key object, or a bare string for a unit variant.
fn variant_name<T: Serialize>(value: &T) -> String {
    match serde_json::to_value(value) {
        Ok(serde_json::Value::Object(map)) if map.len() == 1 => map.keys().next().unwrap().clone(),
        Ok(serde_json::Value::String(name)) => name,
        _ => "unknown".to_string(),
    }
}
