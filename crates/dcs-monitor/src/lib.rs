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
//! - `GET /role` → `200` [`RoleReport`] — the instance's reported role
//!   in a redundant pair (`active`, `standby`, or a transition state)
//!   plus the standby's convergence — the pair-as-one-controller
//!   contract of the monitoring-under-redundancy decision
//! - `POST /promote`, `POST /demote` → `200` [`RoleReport`] — the
//!   switchover actions of the switchover-semantics decision: promotion
//!   lifts the standby's write gate at the request's scan boundary,
//!   demotion re-closes the active's; a refusal — a standby that has not
//!   converged, or a repeated promotion — answers `409` with the named
//!   [`SwitchError`]
//! - `POST /command`, body a [`Command`] → `200` [`CommandReceipt`]
//!   (`accepted` / `rejected` outcome); an unparseable body → `400`.
//!   Only a peer reporting settled `active` accepts commands — on a
//!   standby or mid-transition instance the command is refused with a
//!   [`CommandError::NotActive`] rejection receipt, so an operator write
//!   is never reported applied while the write gate keeps it from the
//!   field
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
//! The page is the monitoring and control UI consuming the unified
//! contract: a static, dependency-free HTML+JavaScript asset ([`PAGE`],
//! no build toolchain) that fetches `/signals` once for point labels,
//! units, display groups, and the model-declared `writable` marks — the
//! point listing organizes itself under the model-declared groups, with
//! ungrouped points filed under the documented `"ungrouped"` default —
//! then polls `/snapshot`, `/history`, and `/journal` on one shared
//! one-second cadence — the snapshot refreshes each point's value,
//! quality, and tick, and marks each point in its force set: the forced
//! badge beside the point's value in the listing and on every faceplate
//! element bound to it, the `Substituted` quality drawn distinctly from
//! ordinary `Uncertain`, a release affordance issuing `unforce_point`,
//! and a force affordance on every model-declared writable `In` point —
//! the force contract's only legal target — issuing `force_point`, both
//! through the same receipted command path with rejections surfaced in
//! the receipt pane and the journal; the
//! history increments grow each point's
//! inline-SVG trend through `since`-cursor polling; the journal pane
//! lists quality transitions and settled command receipts in tick order
//! — and submits `write_value` commands to `/command`, displaying the
//! returned receipt. The snapshot's `io_health` section renders as the
//! I/O-health pane: the executor's boundary counters (failed reads,
//! failed writes, consecutive failures) with the last fault's tick and
//! point attribution, the driver's volunteered transport diagnostics as
//! named link degradation distinct from per-point quality, and the
//! pacing shell's scan-overrun count — placed beside the pair view's
//! role and convergence reporting so operator-facing health reads as
//! one surface.
//!
//! Each snapshot's `descriptors` render one faceplate per component
//! instance, generically — no per-kind page code: a port's
//! [`PortRole`](dcs_core::PortRole) hint picks the conventional element
//! (process-value display, setpoint field, driven output, status flag),
//! and the instance's diagnostics join by name. The executor annotates
//! every served `PortDescriptor` with the `point` its port is bound to,
//! so a port's live value comes straight from the snapshot's point
//! telemetry and its signal metadata from the index — including the
//! `writable` mark that gates every command affordance: only a
//! model-declared writable point is ever offered a write, and a
//! rejection is visible in the receipt pane and the journal.
//!
//! Declared parameters are the faceplate's other editable surface: each
//! [`ParameterDescriptor`](dcs_core::ParameterDescriptor) renders an
//! edit control typed to its declared [`ValueKind`](dcs_core::ValueKind)
//! and labeled with its declared
//! [`ParameterRange`](dcs_core::ParameterRange) where present, and
//! submitting issues a `set_parameter` command addressed to the
//! instance through the same receipted path — an out-of-range or
//! mistyped entry is warned client-side against the declared range but
//! still sent, the receipted path staying the authority, and the
//! receipt's applied tick or named rejection lands on the parameter's
//! row and in the journal. A kind declaring no parameters renders no
//! edit affordance. A descriptor carrying no role hints and no
//! parameters — a kind with no custom `describe` — degrades to a
//! generic name-plus-diagnostics-plus-wired-points faceplate, never an
//! error.
//!
//! ## The pair view
//!
//! Under redundancy — the monitoring-under-redundancy decision — the page
//! presents an active/standby pair as one logical controller. It is
//! configured with both peers' monitor addresses: the serving origin is
//! one peer, and each `?peer=host:port` URL parameter names another —
//! e.g. `http://active:8080/?peer=standby:8081`. Every refresh polls
//! `GET /role` on each configured peer; data fetches go to the peer
//! reporting `active`, so the point listing, trends, and journal are the
//! one logical controller's, while the pair section renders per-peer
//! role, convergence, and reachability — an unreachable peer is a named
//! redundancy fault, not a plant fault. Commands submit only to the
//! settled-active peer; a `not_active` rejection — the command landed
//! mid-transition — triggers a role re-poll and one retry. The contract
//! endpoints answer cross-origin reads (`Access-Control-Allow-Origin: *`)
//! so the page can reach a peer on another host. [`PairClient`] is the
//! same pair view for in-process consumers — tests and tooling — and
//! carries the testable half of the routing rules.
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
//! An externally paced run that still needs the loop's per-scan wiring —
//! a tracking standby's checkpoint pull, and the plant step a
//! field-owning run paces to its ticks — installs it as [`Driven`]
//! through [`Monitor::driven`], and each requested scan runs it inside
//! the request's boundary.
//!
//! [`MonitorClient`] is the matching lightweight in-process client used by
//! tests and simple tooling; it speaks plain HTTP/1.0-style requests over a
//! `TcpStream` and needs no extra dependencies.

#![warn(missing_docs)]

mod pair;
mod recorder;

pub use pair::{PairClient, PairError, PeerStatus, PeerView};
pub use recorder::MonitorConfig;

use dcs_core::{
    Command, CommandError, CommandOutcome, CommandReceipt, JournalEntry, PointHistory, PointId,
    RoleReport, TelemetrySnapshot, Tick,
};
use dcs_model::SignalIndex;
use dcs_runtime::{ApplyError, Checkpoint, Executor, Peer, ScanError};
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

/// Runs once after each completed requested scan, receiving the peer —
/// the plant step the driving request paces the run to (its field
/// ownership decides the step), and the checkpoint a state-file run
/// persists at that boundary. A failure fails the request like a scan
/// failure.
pub type AfterScan<'d> = Box<dyn Fn(&Peer<'d>) -> Result<(), String> + Send + Sync + 'd>;

/// The wiring an unpaced [`Monitor`]'s `POST /scan` runs around each
/// requested scan — see [`Monitor::driven`].
///
/// A process serving an externally paced run — a scripted test or
/// operator driving ticks through `POST /scan` rather than a wall-clock
/// schedule — still owes the run the two steps a paced scan loop wraps
/// around each scan: a tracking standby's checkpoint pull (the
/// peer-transport decision) and the plant step a field-owning run paces
/// to its ticks. `Driven` carries both so the request keeps them.
#[derive(Default)]
pub struct Driven<'d> {
    /// The tracking source: while the peer does not own the field, each
    /// requested scan first pulls `GET /checkpoint` from the active's
    /// monitor at this address and applies it — the pull-per-scan-cycle
    /// resync a paced standby's loop performs. A field-owning peer skips
    /// the pull: a promoted standby continues its own run.
    pub track: Option<SocketAddr>,
    /// The [`AfterScan`] hook — typically the plant step a field-owning
    /// run paces to its ticks.
    pub after_scan: Option<AfterScan<'d>>,
}

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
    /// The per-requested-scan wiring [`driven`](Self::driven) installed —
    /// consulted only on an unpaced monitor, where `POST /scan` runs.
    driven: Driven<'d>,
}

/// The peer — executor plus redundancy role — and the history recorder,
/// behind one lock so a request never observes a half-recorded scan.
struct Shared<'d> {
    peer: Peer<'d>,
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
        Self::bind_peer_with(addr, Peer::active(executor, None), signals, config)
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
        Self::bind_paced_peer(addr, Peer::active(executor, None), signals)
    }

    /// As [`bind`](Self::bind) for an instance carrying an explicit
    /// redundancy role: `peer` bundles the executor with the write gate
    /// and starting [`Role`](dcs_core::Role) — `active` or tracking
    /// `standby` — that `GET /role`, `POST /promote`, and `POST /demote`
    /// then serve and drive.
    pub fn bind_peer<A: ToSocketAddrs>(
        addr: A,
        peer: Peer<'d>,
        signals: SignalIndex,
    ) -> io::Result<Self> {
        Self::bind_peer_with(addr, peer, signals, MonitorConfig::default())
    }

    /// As [`bind_paced`](Self::bind_paced) for an instance carrying an
    /// explicit redundancy role — see [`bind_peer`](Self::bind_peer).
    pub fn bind_paced_peer<A: ToSocketAddrs>(
        addr: A,
        peer: Peer<'d>,
        signals: SignalIndex,
    ) -> io::Result<Self> {
        let mut monitor = Self::bind_peer_with(addr, peer, signals, MonitorConfig::default())?;
        monitor.paced = true;
        Ok(monitor)
    }

    /// The shared constructor: `peer` in the lock, `config` retention
    /// bounds, unpaced.
    fn bind_peer_with<A: ToSocketAddrs>(
        addr: A,
        peer: Peer<'d>,
        signals: SignalIndex,
        config: MonitorConfig,
    ) -> io::Result<Self> {
        Ok(Self {
            shared: Mutex::new(Shared {
                peer,
                recorder: recorder::Recorder::new(config),
            }),
            signals,
            server: Server::http(addr).map_err(io::Error::other)?,
            paced: false,
            driven: Driven::default(),
        })
    }

    /// Arms `POST /scan` with `driven` wiring and returns the monitor —
    /// see [`Driven`]. Meaningful only on an unpaced monitor: a paced
    /// one refuses `POST /scan`, so the wiring never runs.
    pub fn driven(mut self, driven: Driven<'d>) -> Self {
        self.driven = driven;
        self
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
    /// one, so `/history` and `/journal` advance under pacing. A pending
    /// role transition settles on the completed scan and its journal
    /// entry follows the scan's own events.
    pub fn paced_scan(&self) -> Result<Tick, ScanError> {
        let mut shared = self.shared.lock().unwrap();
        scan_and_record(&mut shared)
    }

    /// Records one scan cycle that overran its wall-clock period — the
    /// paced scan loop's feed for the snapshot's `io_health.scan_overruns`,
    /// taken under the same lock that serializes scans. Wall-clock pacing
    /// is the shell's business, so the loop detects the overrun and this
    /// call only reports it into telemetry.
    pub fn record_scan_overrun(&self) {
        self.shared.lock().unwrap().peer.record_scan_overrun();
    }

    /// The executor's current telemetry snapshot, taken under the lock.
    pub fn snapshot(&self) -> TelemetrySnapshot {
        self.shared.lock().unwrap().peer.snapshot()
    }

    /// The executor's current transferable state, taken under the lock —
    /// the same between-scans [`Checkpoint`] `GET /checkpoint` serves.
    /// A paced loop uses it to persist the run's state file at its
    /// documented per-cycle boundary.
    pub fn checkpoint(&self) -> Checkpoint {
        self.shared.lock().unwrap().peer.checkpoint()
    }

    /// The executor's current virtual tick.
    pub fn tick(&self) -> Tick {
        self.shared.lock().unwrap().peer.tick()
    }

    /// The instance's reported redundancy role — what `GET /role`
    /// serves — taken under the lock.
    pub fn role_report(&self) -> RoleReport {
        self.shared.lock().unwrap().peer.report()
    }

    /// Whether the instance currently owns field writes — `active`, or
    /// `promoting` with the gate already lifted. A paced loop uses this
    /// to decide whether its scan may step a shared plant.
    pub fn owns_field(&self) -> bool {
        self.shared.lock().unwrap().peer.owns_field()
    }

    /// Applies a checkpoint pulled from the active peer — the standby's
    /// tracking half of the redundancy contract, taken under the same
    /// lock that serializes scans, so the apply lands at a scan
    /// boundary. A field-owning instance refuses with
    /// [`ApplyError::OwnsField`]; a rejected checkpoint rolls back and
    /// the peer reports itself degraded.
    ///
    /// The apply also runs the peer's staged-output divergence check; a
    /// transition into `diverged` is journaled at the tick the compared
    /// staged image belonged to.
    pub fn apply_checkpoint(&self, checkpoint: &Checkpoint) -> Result<(), ApplyError> {
        let mut shared = self.shared.lock().unwrap();
        let Shared { peer, recorder } = &mut *shared;
        let result = peer.apply(checkpoint);
        for report in peer.take_divergences() {
            recorder.note_divergence(report.tick, report.mismatches);
        }
        result
    }

    /// Marks a tracking peer degraded after a checkpoint fetch produced
    /// nothing — an unreachable active or a refused request — and counts
    /// the heartbeat miss toward the failover budget.
    pub fn note_transfer_failed(&self, detail: impl std::fmt::Display) {
        self.shared
            .lock()
            .unwrap()
            .peer
            .note_transfer_failed(detail);
    }

    /// Whether the heartbeat's consecutive failed pulls have reached the
    /// peer's configured failover budget — the scan boundary at which a
    /// still-converged standby may self-promote. See
    /// [`Peer::failover_due`](dcs_runtime::Peer::failover_due).
    pub fn failover_due(&self) -> bool {
        self.shared.lock().unwrap().peer.failover_due()
    }

    /// The automatic-failover half of `POST /promote`, for the scan loop
    /// that detects active loss: applies the self-promotion at this
    /// boundary under the lock and journals the reported transition.
    /// A refusal — the named [`SwitchError`](dcs_core::SwitchError) —
    /// leaves the peer reporting its convergence state, which `GET
    /// /role` already serves; the scan cycle continues.
    pub fn self_promote(&self) -> Result<RoleReport, dcs_core::SwitchError> {
        let mut shared = self.shared.lock().unwrap();
        let Shared { peer, recorder } = &mut *shared;
        peer.self_promote()?;
        for change in peer.take_role_changes() {
            recorder.note_role_change(change.tick, change.from, change.to);
        }
        Ok(peer.report())
    }

    fn handle(&self, mut request: Request) {
        let method = request.method().clone();
        let url = request.url().to_string();
        let (path, query) = url.split_once('?').unwrap_or((url.as_str(), ""));
        let response = match (method, path) {
            (Method::Get, "/") | (Method::Get, "/index.html") => html(PAGE),
            (Method::Get, "/signals") => json(200, &self.signals),
            (Method::Get, "/snapshot") => json(200, &self.shared.lock().unwrap().peer.snapshot()),
            (Method::Get, "/receipts") => json(200, self.shared.lock().unwrap().peer.receipts()),
            (Method::Get, "/checkpoint") => {
                json(200, &self.shared.lock().unwrap().peer.checkpoint())
            }
            (Method::Get, "/role") => json(200, &self.shared.lock().unwrap().peer.report()),
            (Method::Get, "/history") => match history_query(query) {
                Ok((points, since)) => {
                    let shared = self.shared.lock().unwrap();
                    let Shared { peer, recorder } = &*shared;
                    json(200, &recorder.history(peer.executor(), &points, since))
                }
                Err(message) => json(400, &message),
            },
            (Method::Get, "/journal") => match journal_query(query) {
                Ok(since) => json(200, &self.shared.lock().unwrap().recorder.journal(since)),
                Err(message) => json(400, &message),
            },
            (Method::Post, "/promote") => self.switchover(true),
            (Method::Post, "/demote") => self.switchover(false),
            (Method::Post, "/command") => match read_json::<Command>(&mut request) {
                Ok(command) => {
                    let mut shared = self.shared.lock().unwrap();
                    let Shared { peer, recorder } = &mut *shared;
                    // Only the settled-active peer accepts commands: on a
                    // standby or mid-transition instance the write gate
                    // would keep the write from the field, so refuse with
                    // a receipt rather than report a phantom application.
                    let receipt = if peer.accepts_commands() {
                        let receipt = peer.submit_command(command);
                        let index = peer.receipts().len() - 1;
                        let tick = peer.tick();
                        recorder.note_command(index, receipt.clone(), tick);
                        receipt
                    } else {
                        let receipt = CommandReceipt {
                            command: command.clone(),
                            outcome: CommandOutcome::Rejected {
                                reason: CommandError::NotActive {
                                    point: command.point(),
                                    role: peer.role(),
                                },
                            },
                        };
                        recorder.note_settled(receipt.clone(), peer.tick());
                        receipt
                    };
                    json(200, &receipt)
                }
                Err(response) => response,
            },
            (Method::Post, "/scan") => match read_json::<ScanRequest>(&mut request) {
                Ok(_) if self.paced => json(409, SCAN_REFUSED_WHEN_PACED),
                Ok(body) => {
                    let mut shared = self.shared.lock().unwrap();
                    let mut failure = None;
                    for _ in 0..body.scans {
                        // A tracking standby resynchronizes once per scan
                        // cycle — the pull a paced standby's loop runs
                        // before its scan. A rejected checkpoint degrades
                        // the peer but the scan still runs on its
                        // last-known state.
                        if let Some(active) = self.driven.track
                            && !shared.peer.owns_field()
                        {
                            match MonitorClient::new(active).checkpoint() {
                                Ok(checkpoint) => {
                                    let _ = shared.peer.apply(&checkpoint);
                                    // The apply's divergence check
                                    // journals a transition into
                                    // `diverged` at the compared tick.
                                    for report in shared.peer.take_divergences() {
                                        shared
                                            .recorder
                                            .note_divergence(report.tick, report.mismatches);
                                    }
                                }
                                Err(error) => shared
                                    .peer
                                    .note_transfer_failed(format!("fetch from {active}: {error}")),
                            }
                            // Active loss detected at this boundary: the
                            // miss budget is met, so a still-converged
                            // standby promotes itself; a refusal is the
                            // peer's named convergence state, which
                            // `GET /role` serves — the scan runs either
                            // way.
                            if shared.peer.failover_due() && shared.peer.self_promote().is_ok() {
                                for change in shared.peer.take_role_changes() {
                                    shared.recorder.note_role_change(
                                        change.tick,
                                        change.from,
                                        change.to,
                                    );
                                }
                            }
                        }
                        if let Err(error) = scan_and_record(&mut shared) {
                            failure = Some(error.to_string());
                            break;
                        }
                        if let Some(after_scan) = &self.driven.after_scan
                            && let Err(error) = after_scan(&shared.peer)
                        {
                            failure = Some(error);
                            break;
                        }
                    }
                    match failure {
                        Some(error) => json(500, &error),
                        None => json(200, &shared.peer.snapshot()),
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

    /// `POST /promote` (`promote` true) or `POST /demote` (`false`):
    /// applies the role change at the request's scan boundary and
    /// answers the post-change [`RoleReport`], or `409` with the named
    /// [`SwitchError`](dcs_core::SwitchError) on refusal. The reported
    /// transition — the request is itself a boundary event — is
    /// journaled at the tick the peer attributes it to.
    fn switchover(&self, promote: bool) -> Response<Cursor<Vec<u8>>> {
        let mut shared = self.shared.lock().unwrap();
        let Shared { peer, recorder } = &mut *shared;
        let result = if promote {
            peer.promote()
        } else {
            peer.demote()
        };
        match result {
            Ok(()) => {
                for change in peer.take_role_changes() {
                    recorder.note_role_change(change.tick, change.from, change.to);
                }
                json(200, &peer.report())
            }
            Err(error) => json(409, &error),
        }
    }
}

/// One executor scan plus its recording — the body `paced_scan` and
/// `POST /scan` share: the scan runs under the lock, its history and
/// journal entries are attributed to the scan's tick, and a role
/// transition the scan settled is journaled after the scan's own events.
fn scan_and_record(shared: &mut Shared<'_>) -> Result<Tick, ScanError> {
    let Shared { peer, recorder } = shared;
    let tick = peer.scan()?;
    recorder.record_scan(peer.executor(), tick);
    for change in peer.take_role_changes() {
        recorder.note_role_change(change.tick, change.from, change.to);
    }
    Ok(tick)
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

/// A JSON response with `Content-Type: application/json` and
/// `Access-Control-Allow-Origin: *` headers — the latter so the
/// monitoring page, configured with both peers' addresses, can poll a
/// peer whose monitor sits on another origin.
fn json<T: Serialize + ?Sized>(status: u16, value: &T) -> Response<Cursor<Vec<u8>>> {
    let body = serde_json::to_vec(value).expect("monitoring contract types serialize");
    Response::from_data(body)
        .with_status_code(status)
        .with_header(
            Header::from_bytes(&b"Content-Type"[..], &b"application/json"[..])
                .expect("static header is valid"),
        )
        .with_header(
            Header::from_bytes(&b"Access-Control-Allow-Origin"[..], &b"*"[..])
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

    /// `GET /role`: the instance's reported redundancy role and standby
    /// convergence — the pair-as-one-controller contract.
    pub fn role(&self) -> io::Result<RoleReport> {
        self.get_json("/role")
    }

    /// `POST /promote`: lifts the standby's write gate at the request's
    /// scan boundary, returning the post-change [`RoleReport`]. A
    /// refusal — `not_converged`, `already_active` — surfaces as an
    /// error whose message carries the `409` body: the named
    /// `SwitchError` JSON.
    pub fn promote(&self) -> io::Result<RoleReport> {
        let (status, body) = self.request("POST", "/promote", None)?;
        decode(status, &body)
    }

    /// `POST /demote`: re-closes the field owner's write gate at the
    /// request's scan boundary, returning the post-change
    /// [`RoleReport`]. A refusal on a non-owner surfaces like
    /// [`promote`](Self::promote)'s.
    pub fn demote(&self) -> io::Result<RoleReport> {
        let (status, body) = self.request("POST", "/demote", None)?;
        decode(status, &body)
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

/// Reads one HTTP response: headers up to the blank line, then the body
/// by `Content-Length`, by `Transfer-Encoding: chunked` framing (which a
/// server may pick over a known length once a body grows past its
/// threshold), or to EOF when neither is given.
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
    let header = |name: &str| {
        headers
            .split("\r\n")
            .filter_map(|line| line.split_once(':'))
            .find(|(key, _)| key.trim().eq_ignore_ascii_case(name))
            .map(|(_, value)| value.trim().to_string())
    };
    let content_length = header("content-length").and_then(|value| value.parse::<usize>().ok());
    let chunked = header("transfer-encoding").is_some_and(|value| {
        value
            .split(',')
            .any(|coding| coding.trim().eq_ignore_ascii_case("chunked"))
    });

    let body_start = header_end + 4;
    if chunked {
        // Chunk framing: `<hex size>\r\n<data>\r\n` repeated, ended by a
        // zero-size chunk. Trailers may follow; the body is complete at
        // the zero-size chunk.
        let mut body = Vec::new();
        let mut pos = body_start;
        loop {
            let line_end = loop {
                if let Some(end) = find_subslice(&buf[pos..], b"\r\n") {
                    break pos + end;
                }
                match stream.read(&mut chunk)? {
                    0 => {
                        return Err(io::Error::new(
                            io::ErrorKind::UnexpectedEof,
                            "connection closed mid-chunked body",
                        ));
                    }
                    n => buf.extend_from_slice(&chunk[..n]),
                }
            };
            let size = std::str::from_utf8(&buf[pos..line_end])
                .ok()
                .and_then(|line| usize::from_str_radix(line.trim(), 16).ok())
                .ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidData, "malformed chunk size")
                })?;
            pos = line_end + 2;
            if size == 0 {
                break;
            }
            while buf.len() - pos < size + 2 {
                match stream.read(&mut chunk)? {
                    0 => {
                        return Err(io::Error::new(
                            io::ErrorKind::UnexpectedEof,
                            "connection closed mid-chunked body",
                        ));
                    }
                    n => buf.extend_from_slice(&chunk[..n]),
                }
            }
            body.extend_from_slice(&buf[pos..pos + size]);
            pos += size + 2;
        }
        Ok((status, String::from_utf8_lossy(&body).into_owned()))
    } else {
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
