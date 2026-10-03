//! `dcs-forge`: the QA lane's bridge-placed checkpoint endpoint for the
//! announced-source demote-verify legs.
//!
//! The demote-forged-standby-source scenario needs a hostile endpoint
//! standing at the address an owner recorded as its announced tracking
//! source: the endpoint serves a mutable checkpoint document — forged
//! standby-shaped shapes the scenario stages, then the honest one —
//! and answers the demote verify's `?prove=` pull. On the hardware rig
//! the endpoint runs in a labeled container on the run's rig bridge
//! (`endpoint_placement['forge'] == 'bridge'`): the host egress policy
//! refuses rig-to-host traffic, so a host-bound socket could never be
//! dialed by the controller verifying it.
//!
//! The document is re-read from `--document` on every pull, so the
//! scenario's next staged shape lands by rewriting the bind-mounted
//! file between `POST /demote` calls — no relaunch. `--announce` runs
//! the follow-peer half of the tracking contract: the endpoint pulls
//! `GET /checkpoint?peer=0.0.0.0:<listen-port>` on the named monitor at
//! `--announce-ms` intervals, the wildcard claim resolving to this
//! container's own bridge address exactly like a real tracking peer's
//! `?peer=` announce — so the recorded hint names an endpoint the
//! verify pull can actually dial.
//!
//! `--pair-token` selects the endpoint's posture. Unkeyed it is the
//! threat-model forgery: every answer carries no `line_proof`, so the
//! keyed puller's proof gate alone refuses it — the unproven-document
//! leg. Keyed it signs each `?prove=` answer with the line's own
//! [`line_proof`] — the strongest interposer shape, where the pulled
//! document is genuinely attested and only its content can convict it
//! — so the receipt-window-fork and planted-internal legs reach the
//! demote verify's command-record audit rather than the proof gate.
//!
//! Every served pull and every announce attempt appends one JSONL
//! record to `--hits` — `{"kind":"serve",...,"signed":...}` and
//! `{"kind":"announce",...,"ok":...}` — the self-verifying ledger the
//! scenario reads to prove the verify pull reached this endpoint and
//! how it was answered.

use dcs_monitor::{MonitorClient, line_proof, pair_key};
use dcs_runtime::Checkpoint;
use serde_json::{Value, json};
use std::io::Write;
use std::net::{IpAddr, Ipv4Addr, SocketAddr, ToSocketAddrs};
use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tiny_http::{Header, Method, Request, Response, Server};

const USAGE: &str = "\
usage: dcs-forge --listen <addr> --document <path> --hits <path>
                 [--announce <host:port>] [--announce-ms <ms>]
                 [--pair-token <token>]

Serves the checkpoint document staged at --document on GET /checkpoint,
re-read per pull; --announce pulls ?peer=0.0.0.0:<listen-port> on the
named monitor every --announce-ms (default 400); --pair-token signs
?prove= answers with the pair's keyed line_proof. Every pull and
announce appends one JSONL record to --hits.";

/// One invocation's parsed command line.
struct Config {
    /// The socket the endpoint binds.
    listen: SocketAddr,
    /// The staged checkpoint document, re-read on every pull.
    document: PathBuf,
    /// The hits ledger — the self-verifying record.
    hits: PathBuf,
    /// The monitor to announce this endpoint's address to.
    announce: Option<String>,
    /// The announce cadence.
    announce_ms: u64,
    /// The pair's tracking token — `Some` signs `?prove=` answers.
    pair_token: Option<String>,
}

/// The hits ledger: one JSON record per served pull and announce
/// attempt, appended under the lock so the serve and announce loops
/// never interleave a line. The record lands before the response it
/// describes, so a completed request already carries its ledger entry.
struct Hits {
    /// The ledger's path — opened per record so a host-side rewrite of
    /// the directory never strands the appends on a deleted inode.
    path: PathBuf,
}

impl Hits {
    /// Appends one record to the ledger; a write failure loses the
    /// record but never the answer the endpoint owes the puller.
    fn note(&self, record: Value) {
        if let Ok(mut file) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)
        {
            let _ = writeln!(file, "{record}");
        }
    }
}

/// The `name=value` query parameter's value, or `None` when absent.
fn query_param<'a>(query: &'a str, name: &str) -> Option<&'a str> {
    query.split('&').find_map(|pair| {
        pair.split_once('=')
            .and_then(|(key, value)| (key == name).then_some(value))
    })
}

/// The JSON response helper — the monitor's `json` shape: encoded body,
/// status, and content type. A response the connection can no longer
/// take is dropped, never retried.
fn respond(request: Request, status: u16, body: &Value) {
    let response = Response::from_data(serde_json::to_vec(body).unwrap_or_default())
        .with_status_code(status)
        .with_header(
            Header::from_bytes(&b"Content-Type"[..], &b"application/json"[..])
                .expect("static header is valid"),
        );
    let _ = request.respond(response);
}

/// One served request: `GET /checkpoint` answers the staged document —
/// signed when the endpoint is keyed and the pull carries `prove=` —
/// every other route answers 404. Each answer is ledgered first.
fn serve(request: Request, config: &Config, key: Option<u64>, hits: &Mutex<Hits>) {
    let remote = request.remote_addr().map(|addr| addr.to_string());
    let url = request.url().to_string();
    let (path, query) = url.split_once('?').unwrap_or((&url, ""));
    if request.method() != &Method::Get || path != "/checkpoint" {
        hits.lock().unwrap().note(json!({
            "kind": "serve", "remote": remote,
            "path": path, "query": query, "status": 404}));
        respond(request, 404, &json!("not found"));
        return;
    }
    let staged = std::fs::read_to_string(&config.document)
        .map_err(|error| error.to_string())
        .and_then(|text| {
            serde_json::from_str::<Checkpoint>(&text).map_err(|error| error.to_string())
        });
    match staged {
        Ok(mut checkpoint) => {
            // The keyed half of the endpoint: a `?prove=` pull gets the
            // document's line_proof stamped under the pair's key — the
            // same decoration a keyed monitor's checkpoint route adds.
            let nonce = query_param(query, "prove").and_then(|text| text.parse::<u64>().ok());
            let signed = match (key, nonce) {
                (Some(key), Some(nonce)) => {
                    checkpoint.line_proof = Some(line_proof(key, nonce, &checkpoint));
                    true
                }
                _ => false,
            };
            hits.lock().unwrap().note(json!({
                "kind": "serve", "remote": remote,
                "path": path, "query": query,
                "signed": signed, "status": 200}));
            respond(
                request,
                200,
                &serde_json::to_value(&checkpoint).unwrap_or_default(),
            );
        }
        Err(error) => {
            hits.lock().unwrap().note(json!({
                "kind": "serve", "remote": remote,
                "path": path, "query": query,
                "status": 500, "error": error}));
            respond(request, 500, &json!(error));
        }
    }
}

/// One announce pull: `GET /checkpoint?peer=0.0.0.0:<port>` on the
/// named monitor — the wildcard claim resolving to this endpoint's own
/// bridge address on the receiving side, exactly like a tracking
/// peer's announce. `Ok(())` means the monitor answered — and so
/// recorded the hint this endpoint's staged document will be verified
/// through.
fn announce_once(target: &str, port: u16) -> Result<(), String> {
    let addr = target
        .to_socket_addrs()
        .map_err(|error| format!("resolve {target}: {error}"))?
        .next()
        .ok_or_else(|| format!("resolve {target}: no addresses"))?;
    let announced = SocketAddr::new(IpAddr::V4(Ipv4Addr::UNSPECIFIED), port);
    MonitorClient::with_timeout(addr, Duration::from_secs(1))
        .checkpoint_tracking(Some(announced), None)
        .map(|_| ())
        .map_err(|error| error.to_string())
}

/// The announce loop: one bounded pull per interval, ledgered — the
/// endpoint may start before its target resolves, so the target name
/// re-resolves on every attempt.
fn announce_loop(target: String, port: u16, interval: Duration, hits: Arc<Mutex<Hits>>) {
    loop {
        let outcome = announce_once(&target, port);
        hits.lock().unwrap().note(json!({
            "kind": "announce", "target": target,
            "ok": outcome.is_ok(),
            "detail": outcome.err()}));
        std::thread::sleep(interval);
    }
}

/// Parses the command line; malformed input fails with the usage text.
fn parse(args: &[String]) -> Result<Config, String> {
    let usage = |error: String| format!("{error}\n{USAGE}");
    let mut config = Config {
        listen: SocketAddr::from(([0, 0, 0, 0], 8090)),
        document: PathBuf::new(),
        hits: PathBuf::new(),
        announce: None,
        announce_ms: 400,
        pair_token: None,
    };
    let mut document_seen = false;
    let mut hits_seen = false;
    let mut index = 0;
    while index < args.len() {
        let flag = args[index].as_str();
        let value = |flag: &str| -> Result<&str, String> {
            args.get(index + 1)
                .map(String::as_str)
                .ok_or_else(|| usage(format!("{flag} expects a value")))
        };
        match flag {
            "--listen" => {
                let text = value(flag)?;
                config.listen = text
                    .parse()
                    .map_err(|error| usage(format!("invalid --listen {text:?}: {error}")))?;
            }
            "--document" => {
                config.document = PathBuf::from(value(flag)?);
                document_seen = true;
            }
            "--hits" => {
                config.hits = PathBuf::from(value(flag)?);
                hits_seen = true;
            }
            "--announce" => config.announce = Some(value(flag)?.to_string()),
            "--announce-ms" => {
                let text = value(flag)?;
                config.announce_ms = text
                    .parse()
                    .map_err(|_| usage(format!("invalid --announce-ms {text:?}")))?;
            }
            "--pair-token" => config.pair_token = Some(value(flag)?.to_string()),
            other => return Err(usage(format!("unknown flag {other:?}"))),
        }
        index += 2;
    }
    if !document_seen {
        return Err(usage("--document is required".to_string()));
    }
    if !hits_seen {
        return Err(usage("--hits is required".to_string()));
    }
    Ok(config)
}

/// Runs the endpoint until the process is signaled.
fn run(args: &[String]) -> Result<(), String> {
    let config = parse(args)?;
    let key = config.pair_token.as_deref().map(pair_key);
    let hits = Arc::new(Mutex::new(Hits {
        path: config.hits.clone(),
    }));
    if let Some(target) = config.announce.clone() {
        let hits = Arc::clone(&hits);
        let port = config.listen.port();
        let interval = Duration::from_millis(config.announce_ms);
        std::thread::spawn(move || announce_loop(target, port, interval, hits));
    }
    let server =
        Server::http(config.listen).map_err(|error| format!("bind {}: {error}", config.listen))?;
    for request in server.incoming_requests() {
        serve(request, &config, key, &hits);
    }
    Ok(())
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(failure) => {
            eprintln!("{failure}");
            ExitCode::FAILURE
        }
    }
}
