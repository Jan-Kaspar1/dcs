//! HTTP+JSON monitoring access to a running executor.
//!
//! [`Monitor`] exposes a [`dcs_runtime::Executor`] over `tiny_http` — a
//! small synchronous HTTP server, so no async runtime is involved and every
//! request is handled one at a time. The executor lives behind a [`Mutex`]:
//! each request holds the lock for its whole handling, so a snapshot can
//! never observe a half-run scan and commands always interleave between
//! scans, where the executor's documented boundary applies them.
//!
//! All bodies are JSON and all protocol types are shared serde contracts:
//!
//! - `GET /snapshot` → `200` [`TelemetrySnapshot`]
//! - `GET /signals` → `200` [`SignalIndex`] — the loaded model's
//!   point-to-signal metadata: every known point's signal name, unit,
//!   description, display group, direction, and value type
//! - `GET /receipts` → `200` `Vec<`[`CommandReceipt`]`>` — the executor's
//!   receipt log, retrievable alongside the snapshot
//! - `GET /history` → `200` `Vec<`[`PointHistory`]`>` — each mapped
//!   point's retained samples in tick order; `?point=<id>` (repeatable)
//!   selects points and `?since=<seq>` returns only samples newer than
//!   the caller's last seen sequence
//! - `GET /journal` → `200` `Vec<`[`JournalEntry`]`>` — the transition
//!   journal in scan order; `?since=<seq>` filters likewise
//! - `GET /checkpoint` → `200` [`Checkpoint`] — the executor's current
//!   transferable state. This is the peer-sync endpoint a standby
//!   controller pulls from (the peer-transport decision): like every
//!   request it is served at a scan boundary under the executor lock, so
//!   the checkpoint is always a consistent between-scans capture
//! - `POST /command`, body a [`Command`] → `200` [`CommandReceipt`]
//!   (`accepted` / `rejected` outcome); an unparseable body → `400`
//! - `POST /scan`, body [`ScanRequest`] → runs that many scans → `200`
//!   [`TelemetrySnapshot`] taken after the last one; a `ScanError` → `500`;
//!   refused with `409` on a paced monitor (see below)
//! - `GET /` (also `/index.html`) → `200` `text/html` — the monitoring
//!   page described below
//!
//! The monitor records bounded run history server-side, at one documented
//! point: after each completed scan it journals the command receipts the
//! scan boundary settled, appends each point's fresh image sample to that
//! point's history ring, and journals quality transitions and step
//! failures — in the scan's own phase order. Both streams are bounded by
//! [`MonitorConfig`] with oldest-first eviction, and every entry carries
//! a monotonically increasing `seq`, so a polling consumer detects an
//! evicted stretch as a numbering gap instead of silently missing it.
//!
//! The page is the trend slice of the monitoring and control UI consuming
//! the unified contract: a static, dependency-free HTML+JavaScript asset
//! ([`PAGE`], no build toolchain) that fetches `/signals` once for point
//! labels, units, and display groups — the point listing organizes itself
//! under the model-declared groups, with ungrouped points filed under the
//! documented `"ungrouped"` default — then polls `/snapshot`, `/history`,
//! and `/journal` on one shared one-second cadence — the snapshot
//! refreshes each point's value, quality, and tick; the history
//! increments grow each point's inline-SVG trend through `since`-cursor
//! polling; the journal pane lists quality transitions and settled
//! command receipts in tick order — and submits `write_value` commands to
//! `/command` through a form, displaying the returned receipt.
//!
//! [`Monitor::serve`] runs the blocking accept loop; callers run it on a
//! dedicated thread — a scoped thread suffices when the driver's borrow
//! isn't `'static` — and [`Monitor::shutdown`] stops it.
//!
//! ## Externally requested scans under pacing
//!
//! `POST /scan` exists for externally driven runs — tests and tooling
//! that advance the executor through the endpoint. A process pacing its
//! own scan loop instead binds with [`Monitor::bind_paced`] and drives
//! [`Monitor::paced_scan`] on its wall-clock schedule; `POST /scan` then
//! answers `409`, because injecting endpoint-driven ticks would break the
//! paced schedule's determinism claims — one tick per paced period, with
//! commands applied at the scan boundary. Paced scans run through the
//! same mutex and are recorded exactly like endpoint-driven ones, so
//! every endpoint — snapshot, receipts, history, journal — tracks the
//! paced run.
//!
//! [`MonitorClient`] is the matching lightweight in-process client used by
//! tests and simple tooling; it speaks plain HTTP/1.0-style requests over a
//! `TcpStream` and needs no extra dependencies.

#![warn(missing_docs)]

mod recorder;

pub use recorder::MonitorConfig;

use dcs_core::{
    Command, CommandReceipt, JournalEntry, PointHistory, PointId, TelemetrySnapshot, Tick,
};
use dcs_model::SignalIndex;
use dcs_runtime::{Checkpoint, Executor, ScanError};
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use std::io::{self, Cursor, Read, Write};
use std::net::{SocketAddr, TcpStream, ToSocketAddrs};
use std::sync::Mutex;
use tiny_http::{Header, Method, Request, Response, Server};

/// Request body of `POST /scan`: how many scans the executor should run.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct ScanRequest {
    /// The number of scans to run.
    pub scans: u64,
}

/// The monitoring page served at `GET /` — see the crate docs.
pub const PAGE: &str = include_str!("page.html");

/// The `409` body `POST /scan` answers on a paced monitor.
const SCAN_REFUSED_WHEN_PACED: &str = "refused: scans are paced to wall-clock time by this \
     controller; externally requested scans would inject ticks outside the schedule";

/// A monitoring server sharing one executor over HTTP+JSON.
///
/// See the crate docs for the endpoint contract and the single-lock
/// concurrency model.
pub struct Monitor<'d> {
    shared: Mutex<Shared<'d>>,
    signals: SignalIndex,
    server: Server,
    /// When set — [`bind_paced`](Self::bind_paced) — the hosting process
    /// paces scans itself through [`paced_scan`](Self::paced_scan) and
    /// `POST /scan` is refused: the wall clock owns the scan schedule.
    paced: bool,
}

/// The executor plus the history recorder, behind one lock so a request
/// never observes a half-recorded scan.
struct Shared<'d> {
    executor: Executor<'d>,
    recorder: recorder::Recorder,
}

impl<'d> Monitor<'d> {
    /// Binds an HTTP listener on `addr` and returns a monitor sharing
    /// `executor` and the loaded model's `signals` index — typically
    /// [`PlantModel::signal_index`](dcs_model::PlantModel::signal_index)
    /// applied to the model the executor was assembled from.
    ///
    /// `("127.0.0.1", 0)` binds an ephemeral port;
    /// [`local_addr`](Self::local_addr) reports the bound address.
    /// History and journal retention follow [`MonitorConfig::default`].
    pub fn bind<A: ToSocketAddrs>(
        addr: A,
        executor: Executor<'d>,
        signals: SignalIndex,
    ) -> io::Result<Self> {
        Self::bind_with(addr, executor, signals, MonitorConfig::default())
    }

    /// As [`bind`](Self::bind) with explicit `config` retention bounds.
    pub fn bind_with<A: ToSocketAddrs>(
        addr: A,
        executor: Executor<'d>,
        signals: SignalIndex,
        config: MonitorConfig,
    ) -> io::Result<Self> {
        Ok(Self {
            shared: Mutex::new(Shared {
                executor,
                recorder: recorder::Recorder::new(config),
            }),
            signals,
            server: Server::http(addr).map_err(io::Error::other)?,
            paced: false,
        })
    }

    /// As [`bind`](Self::bind) for a process pacing its own scan loop:
    /// `POST /scan` answers `409` and the paced loop drives
    /// [`paced_scan`](Self::paced_scan) — see the crate docs for the
    /// interleaving rule this enforces.
    pub fn bind_paced<A: ToSocketAddrs>(
        addr: A,
        executor: Executor<'d>,
        signals: SignalIndex,
    ) -> io::Result<Self> {
        let mut monitor = Self::bind_with(addr, executor, signals, MonitorConfig::default())?;
        monitor.paced = true;
        Ok(monitor)
    }

    /// The address the listener is bound to.
    pub fn local_addr(&self) -> SocketAddr {
        self.server
            .server_addr()
            .to_ip()
            .expect("monitor listens on TCP")
    }

    /// Serves requests until [`shutdown`](Self::shutdown).
    ///
    /// Blocking: run this on a dedicated thread. Requests are handled
    /// serially — one at a time, in arrival order — which keeps the
    /// executor's command/scan interleaving deterministic.
    pub fn serve(&self) {
        for request in self.server.incoming_requests() {
            self.handle(request);
        }
    }

    /// Stops a [`serve`](Self::serve) loop running on another thread.
    pub fn shutdown(&self) {
        self.server.unblock();
    }

    /// Runs one executor scan through the shared lock and records it —
    /// the entry point for a process pacing its own scan loop once the
    /// monitor owns the executor.
    ///
    /// Holding the mutex for the whole scan keeps the documented
    /// interleaving: a request never observes a half-run scan, and a
    /// command submitted between scans still applies at the next scan's
    /// boundary. The scan is recorded exactly like an endpoint-driven
    /// one, so `/history` and `/journal` advance under pacing.
    pub fn paced_scan(&self) -> Result<Tick, ScanError> {
        let mut shared = self.shared.lock().unwrap();
        let Shared { executor, recorder } = &mut *shared;
        let tick = executor.scan()?;
        recorder.record_scan(executor, tick);
        Ok(tick)
    }

    /// The executor's current telemetry snapshot, taken under the lock.
    pub fn snapshot(&self) -> TelemetrySnapshot {
        self.shared.lock().unwrap().executor.snapshot()
    }

    /// The executor's current virtual tick.
    pub fn tick(&self) -> Tick {
        self.shared.lock().unwrap().executor.tick()
    }

    fn handle(&self, mut request: Request) {
        let method = request.method().clone();
        let url = request.url().to_string();
        let (path, query) = url.split_once('?').unwrap_or((url.as_str(), ""));
        let response = match (method, path) {
            (Method::Get, "/") | (Method::Get, "/index.html") => html(PAGE),
            (Method::Get, "/signals") => json(200, &self.signals),
            (Method::Get, "/snapshot") => {
                json(200, &self.shared.lock().unwrap().executor.snapshot())
            }
            (Method::Get, "/receipts") => {
                json(200, self.shared.lock().unwrap().executor.receipts())
            }
            (Method::Get, "/checkpoint") => {
                json(200, &self.shared.lock().unwrap().executor.checkpoint())
            }
            (Method::Get, "/history") => match history_query(query) {
                Ok((points, since)) => {
                    let shared = self.shared.lock().unwrap();
                    let Shared { executor, recorder } = &*shared;
                    json(200, &recorder.history(executor, &points, since))
                }
                Err(message) => json(400, &message),
            },
            (Method::Get, "/journal") => match journal_query(query) {
                Ok(since) => json(200, &self.shared.lock().unwrap().recorder.journal(since)),
                Err(message) => json(400, &message),
            },
            (Method::Post, "/command") => match read_json::<Command>(&mut request) {
                Ok(command) => {
                    let mut shared = self.shared.lock().unwrap();
                    let Shared { executor, recorder } = &mut *shared;
                    let receipt = executor.submit_command(command);
                    let index = executor.receipts().len() - 1;
                    let tick = executor.tick();
                    recorder.note_command(index, receipt.clone(), tick);
                    json(200, &receipt)
                }
                Err(response) => response,
            },
            (Method::Post, "/scan") => match read_json::<ScanRequest>(&mut request) {
                Ok(_) if self.paced => json(409, SCAN_REFUSED_WHEN_PACED),
                Ok(body) => {
                    let mut shared = self.shared.lock().unwrap();
                    let Shared { executor, recorder } = &mut *shared;
                    let mut failure = None;
                    for _ in 0..body.scans {
                        match executor.scan() {
                            Ok(tick) => recorder.record_scan(executor, tick),
                            Err(error) => {
                                failure = Some(error);
                                break;
                            }
                        }
                    }
                    match failure {
                        Some(error) => json(500, &error.to_string()),
                        None => json(200, &executor.snapshot()),
                    }
                }
                Err(response) => response,
            },
            _ => json(404, "not found"),
        };
        // A dropped client connection makes respond fail; the request is
        // already handled, so the error is ignored.
        let _ = request.respond(response);
    }
}

/// Splits a URL query into `key=value` pairs. The monitoring endpoints
/// take only numeric values, so no percent-decoding is applied.
fn query_pairs(query: &str) -> impl Iterator<Item = (&str, &str)> {
    query
        .split('&')
        .filter(|pair| !pair.is_empty())
        .map(|pair| pair.split_once('=').unwrap_or((pair, "")))
}

/// The `/history` query: `point` (repeatable) selects points — empty
/// selects all — and `since` keeps only samples with a higher `seq`.
/// Unknown keys are ignored so the endpoint stays forward-compatible.
fn history_query(query: &str) -> Result<(Vec<PointId>, u64), String> {
    let mut points = Vec::new();
    let mut since = 0;
    for (key, value) in query_pairs(query) {
        match key {
            "point" => points.push(PointId(
                value
                    .parse()
                    .map_err(|_| format!("invalid point id {value:?}"))?,
            )),
            "since" => {
                since = value
                    .parse()
                    .map_err(|_| format!("invalid since cursor {value:?}"))?;
            }
            _ => {}
        }
    }
    Ok((points, since))
}

/// The `/journal` query: `since` keeps only entries with a higher `seq`.
fn journal_query(query: &str) -> Result<u64, String> {
    let mut since = 0;
    for (key, value) in query_pairs(query) {
        if key == "since" {
            since = value
                .parse()
                .map_err(|_| format!("invalid since cursor {value:?}"))?;
        }
    }
    Ok(since)
}

/// Reads and parses a JSON request body; parse failures produce the `400`
/// response directly.
fn read_json<T: DeserializeOwned>(request: &mut Request) -> Result<T, Response<Cursor<Vec<u8>>>> {
    let mut body = String::new();
    if request.as_reader().read_to_string(&mut body).is_err() {
        return Err(json(400, "unreadable request body"));
    }
    serde_json::from_str(&body).map_err(|error| json(400, &error.to_string()))
}

/// An HTML response with a `Content-Type: text/html` header.
fn html(body: &'static str) -> Response<Cursor<Vec<u8>>> {
    Response::from_data(body.as_bytes().to_vec())
        .with_status_code(200)
        .with_header(
            Header::from_bytes(&b"Content-Type"[..], &b"text/html; charset=utf-8"[..])
                .expect("static header is valid"),
        )
}

/// A JSON response with a `Content-Type: application/json` header.
fn json<T: Serialize + ?Sized>(status: u16, value: &T) -> Response<Cursor<Vec<u8>>> {
    let body = serde_json::to_vec(value).expect("monitoring contract types serialize");
    Response::from_data(body)
        .with_status_code(status)
        .with_header(
            Header::from_bytes(&b"Content-Type"[..], &b"application/json"[..])
                .expect("static header is valid"),
        )
}

/// A lightweight in-process client for the [`Monitor`] endpoints.
///
/// One request per call over a fresh `TcpStream`; responses are decoded
/// into the `dcs-core` contract types. Any non-`200` status surfaces as an
/// [`io::Error`] carrying the response body.
pub struct MonitorClient {
    addr: SocketAddr,
}

impl MonitorClient {
    /// A client for the monitor bound at `addr` (see
    /// [`Monitor::local_addr`]).
    pub fn new(addr: SocketAddr) -> Self {
        Self { addr }
    }

    /// `GET /`: the monitoring page's HTML source.
    pub fn page(&self) -> io::Result<String> {
        let (status, body) = self.request("GET", "/", None)?;
        if status != 200 {
            return Err(io::Error::other(format!("HTTP {status}: {body}")));
        }
        Ok(body)
    }

    /// `GET /signals`: the loaded model's point-to-signal metadata index.
    pub fn signals(&self) -> io::Result<SignalIndex> {
        self.get_json("/signals")
    }

    /// `GET /snapshot`: the executor's current telemetry snapshot.
    pub fn snapshot(&self) -> io::Result<TelemetrySnapshot> {
        self.get_json("/snapshot")
    }

    /// `GET /receipts`: the executor's full receipt log.
    pub fn receipts(&self) -> io::Result<Vec<CommandReceipt>> {
        self.get_json("/receipts")
    }

    /// `GET /checkpoint`: the executor's current transferable state —
    /// the endpoint a standby pulls checkpoints from, per the
    /// peer-transport decision.
    pub fn checkpoint(&self) -> io::Result<Checkpoint> {
        self.get_json("/checkpoint")
    }

    /// `GET /history`: the retained samples of `points` — or of every
    /// mapped point when empty — keeping only samples with a `seq` above
    /// `since` (`0` fetches everything retained).
    pub fn history(&self, points: &[PointId], since: u64) -> io::Result<Vec<PointHistory>> {
        let mut path = format!("/history?since={since}");
        for point in points {
            path.push_str(&format!("&point={}", point.0));
        }
        self.get_json(&path)
    }

    /// `GET /journal`: the retained transition-journal entries with a
    /// `seq` above `since` (`0` fetches everything retained).
    pub fn journal(&self, since: u64) -> io::Result<Vec<JournalEntry>> {
        self.get_json(&format!("/journal?since={since}"))
    }

    /// `POST /command`: submits `command`, returning its receipt —
    /// `accepted` when queued for the next scan boundary, `rejected` with
    /// a named reason otherwise.
    pub fn command(&self, command: &Command) -> io::Result<CommandReceipt> {
        self.post_json("/command", command)
    }

    /// `POST /scan`: runs `scans` scans, returning the snapshot taken
    /// after the last one.
    pub fn advance(&self, scans: u64) -> io::Result<TelemetrySnapshot> {
        self.post_json("/scan", &ScanRequest { scans })
    }

    fn get_json<T: DeserializeOwned>(&self, path: &str) -> io::Result<T> {
        let (status, body) = self.request("GET", path, None)?;
        decode(status, &body)
    }

    fn post_json<T: DeserializeOwned>(
        &self,
        path: &str,
        payload: &impl Serialize,
    ) -> io::Result<T> {
        let body = serde_json::to_string(payload)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, error))?;
        let (status, body) = self.request("POST", path, Some(&body))?;
        decode(status, &body)
    }

    /// Sends one raw request and returns `(status, body)` — an escape
    /// hatch for endpoints the typed helpers don't cover.
    ///
    /// The client sends `Connection: close` and reads the body by
    /// `Content-Length`, so responses work whether or not the server keeps
    /// the connection alive.
    pub fn request(
        &self,
        method: &str,
        path: &str,
        body: Option<&str>,
    ) -> io::Result<(u16, String)> {
        let mut stream = TcpStream::connect(self.addr)?;
        let mut head = format!(
            "{method} {path} HTTP/1.1\r\nHost: {}\r\nAccept: application/json\r\nConnection: close\r\n",
            self.addr
        );
        if let Some(body) = body {
            head.push_str(&format!(
                "Content-Type: application/json\r\nContent-Length: {}\r\n",
                body.len()
            ));
        }
        head.push_str("\r\n");
        stream.write_all(head.as_bytes())?;
        if let Some(body) = body {
            stream.write_all(body.as_bytes())?;
        }
        read_response(&mut stream)
    }
}

/// Decodes a `200` JSON response body; any other status is an error
/// carrying the body text.
fn decode<T: DeserializeOwned>(status: u16, body: &str) -> io::Result<T> {
    if status != 200 {
        return Err(io::Error::other(format!("HTTP {status}: {body}")));
    }
    serde_json::from_str(body)
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, format!("{error}: {body}")))
}

/// Reads one HTTP response: headers up to the blank line, then exactly
/// `Content-Length` body bytes (or to EOF when no length is given).
fn read_response(stream: &mut TcpStream) -> io::Result<(u16, String)> {
    let mut buf = Vec::with_capacity(4096);
    let mut chunk = [0u8; 4096];
    let header_end = loop {
        if let Some(end) = find_subslice(&buf, b"\r\n\r\n") {
            break end;
        }
        match stream.read(&mut chunk)? {
            0 => {
                return Err(io::Error::new(
                    io::ErrorKind::UnexpectedEof,
                    "connection closed before response headers",
                ));
            }
            n => buf.extend_from_slice(&chunk[..n]),
        }
    };

    let headers = String::from_utf8_lossy(&buf[..header_end]);
    let mut lines = headers.split("\r\n");
    let status = lines
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|code| code.parse::<u16>().ok())
        .ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                format!("malformed status line: {headers}"),
            )
        })?;
    let content_length = lines
        .filter_map(|line| line.split_once(':'))
        .find(|(name, _)| name.trim().eq_ignore_ascii_case("content-length"))
        .and_then(|(_, value)| value.trim().parse::<usize>().ok());

    let body_start = header_end + 4;
    match content_length {
        Some(length) => {
            while buf.len() - body_start < length {
                match stream.read(&mut chunk)? {
                    0 => break,
                    n => buf.extend_from_slice(&chunk[..n]),
                }
            }
            let end = (body_start + length).min(buf.len());
            let body = String::from_utf8_lossy(&buf[body_start..end]).into_owned();
            Ok((status, body))
        }
        None => {
            stream.read_to_end(&mut buf)?;
            let body = String::from_utf8_lossy(&buf[body_start..]).into_owned();
            Ok((status, body))
        }
    }
}

fn find_subslice(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack
        .windows(needle.len())
        .position(|window| window == needle)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scan_request_serde_roundtrip() {
        for scans in [0, 1, u64::MAX] {
            let json = serde_json::to_string(&ScanRequest { scans }).unwrap();
            assert_eq!(
                serde_json::from_str::<ScanRequest>(&json).unwrap(),
                ScanRequest { scans }
            );
        }
        assert_eq!(
            serde_json::to_string(&ScanRequest { scans: 2 }).unwrap(),
            "{\"scans\":2}"
        );
    }

    #[test]
    fn history_query_parses_points_and_since() {
        assert_eq!(
            history_query("point=10&point=20&since=5"),
            Ok((vec![PointId(10), PointId(20)], 5))
        );
        assert_eq!(history_query(""), Ok((Vec::new(), 0)));
        assert_eq!(history_query("unknown=ignored"), Ok((Vec::new(), 0)));
        assert!(history_query("point=abc").is_err());
        assert!(history_query("since=-1").is_err());
    }

    #[test]
    fn journal_query_parses_since() {
        assert_eq!(journal_query("since=7"), Ok(7));
        assert_eq!(journal_query(""), Ok(0));
        assert!(journal_query("since=soon").is_err());
    }
}
