//! HTTP+JSON monitoring access to a running executor.
//!
//! [`Monitor`] exposes a [`dcs_runtime::Executor`] over `tiny_http` — a
//! small synchronous HTTP server, so no async runtime is involved and every
//! request is handled one at a time. The executor lives behind a [`Mutex`]
//! the control-plane endpoints and the scan loop share — scans, commands,
//! checkpoints, and role changes hold it for their whole handling, so a
//! snapshot can never observe a half-run scan and commands always
//! interleave between scans, where the executor's documented boundary
//! applies them. The read endpoints do not touch it: after each completed
//! scan the monitor publishes one immutable, already-materialized
//! [`Publication`] — the snapshot plus the history and journal deltas
//! that scan appended — into bounded storage owned outside the executor
//! lock (the disposable-consumer decision's read side), and the read
//! endpoints serialize and write those published copies. A stalled,
//! absent, or slow reader therefore cannot extend the lock's hold
//! beyond the scan and the publication swap.
//!
//! All bodies are JSON and all protocol types are shared serde contracts:
//!
//! - `GET /snapshot` → `200` [`TelemetrySnapshot`] — the latest
//!   published read model's snapshot: materialized once per completed
//!   scan regardless of request rate, serialized from the published
//!   copy outside the executor lock, and carrying the `publication`
//!   section's store overload counters
//! - `GET /signals` → `200` [`SignalIndex`] — the loaded model's
//!   point-to-signal metadata: every known point's signal name, unit,
//!   description, display group, direction, and value type, plus the
//!   index's `components` section — one
//!   [`ComponentRecord`](dcs_model::ComponentRecord) per declared
//!   instance, the per-instance model data the descriptors do not
//!   carry (the decision-70 rationalization block), joined by the
//!   `kind:id` diagnostic name the descriptor reports
//! - `GET /receipts` → `200` `Vec<`[`CommandReceipt`]`>` — the receipt
//!   log's published mirror, refreshed wherever the control-plane lock
//!   changes it, so a submission between scans is immediately visible
//! - `GET /history` → `200` `Vec<`[`PointHistory`]`>` — each mapped
//!   point's retained samples in tick order from the store's bounded
//!   rings; `?point=<id>` (repeatable) selects points and
//!   `?since=<seq>` returns only samples newer than the caller's last
//!   seen sequence — an evicted stretch surfaces as a numbering gap
//! - `GET /journal` → `200` `Vec<`[`JournalEntry`]`>` — the transition
//!   journal in scan order; `?since=<seq>` filters likewise
//! - `GET /schema` → `200` [`SchemaView`] — the served block-interface
//!   registry of the schema-driven-interface decision: one instance-level
//!   [`BlockInterface`](dcs_core::BlockInterface) per component instance,
//!   in scan order, derived
//!   from the latest publication's bound-point-annotated descriptors
//!   with each measurement's `unit` resolved through the signal index —
//!   so every kind the run instantiated serves its five-category schema
//!   (measurements, configuration, state, commands, events) without the
//!   consumer reconstructing it from scattered surfaces. The view
//!   stamps the publication `seq`/`tick` it was derived from; the
//!   document's JSON Schema is `dcs-model interface-schema`'s emitted
//!   artifact
//! - `GET /resources` → `200` [`ResourceView`] — the same publication's
//!   live half: per instance, each measurement's and state resource's
//!   latest value and quality from the bound point's sample, each
//!   configuration resource's current value from the snapshot's
//!   `parameters` section, each command's `available` flag or the named
//!   refusal a submission would meet, and the retained journal tail's
//!   entries attributed to the instance (its bound points' transitions,
//!   its settled command receipts, its step failures, its emitted
//!   events). Collections are parallel to the interface's, so a
//!   consumer zips the schema and resource views by index or joins by
//!   `name`. Both reads serve published copies — never the executor
//!   lock — exactly like `/snapshot`
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
//!   The attributed envelope `{"command":…,"actor":…}` is also accepted
//!   — the wire shape the command-path audit-identity decision records:
//!   `actor` is the submitter's *declared* identity (attestation, not
//!   authentication — verifying it is deployment machinery such as a
//!   fronting proxy filling it from authenticated context), stamped
//!   onto the receipt and carried into the journaled `CommandSettled`
//!   entry; an absent actor journals unattributed, never a rejection.
//!   Only a peer reporting settled `active` accepts commands — on a
//!   standby or mid-transition instance the command is refused with a
//!   [`CommandError::NotActive`] rejection receipt, so an operator write
//!   is never reported applied while the write gate keeps it from the
//!   field. The executor's pending queue is bounded
//!   ([`Executor::with_command_queue_capacity`]): a validated command
//!   arriving while the queue is full is refused with a
//!   [`CommandError::QueueFull`] rejection receipt — admission is
//!   receipted, never fire-and-forget — and the queue's admission
//!   metrics ride the snapshot's `command_queue` section
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
//! failures — in the scan's own phase order. Every recorded append lands
//! in the publication store's served rings immediately — including
//! events the control plane journals between scans, like a refused
//! command or a role change — and the completed scan then publishes one
//! immutable [`Publication`] carrying its monotonic sequence, its tick,
//! and the deltas appended since the previous publication into the
//! store's bounded retained window. All streams are bounded by
//! [`MonitorConfig`] with oldest-first eviction, and every entry
//! carries a monotonically increasing `seq`, so a polling consumer
//! detects an evicted stretch as a numbering gap — or, on the
//! seq-cursor publication read ([`Monitor::publications_since`]), the
//! named [`PublicationGap`] — and coalesces onto retained or latest
//! state instead of ever backpressuring execution. The store's
//! overload accounting — publications produced, publications evicted
//! and coalesced, retained depth, the configured window bound — rides
//! each served snapshot's `publication` section: the documented
//! placement of the read-side overload metrics. With zero consumers
//! the counters still advance and storage stays bounded.
//!
//! With `journal_file` set on [`MonitorConfig`], every journaled entry is
//! also appended to that path as one line-delimited JSON record at the
//! same recording point — the journal-persistence decision's durable
//! audit trail, monitor-local tooling beside the controller's
//! `--state-file`: the checkpoint resumes the run's state, the journal
//! preserves the run's record. Startup replays the file into the ring
//! and continues `seq` numbering where it left off — a run-boundary
//! marker line separates process lifetimes within the file — so
//! `GET /journal` answers continuously across a restart; a file that
//! cannot be replayed fails startup naming the file and the offending
//! record, and a missing file is a cold start. Point history stays
//! volatile; only the journal persists.
//!
//! The alarm flood and performance report (`dcs-alarm-report`, backed by
//! [`alarm_report`]) is tooling-side aggregation over that record — the
//! flood-and-performance decision's computing surface: it consumes
//! `GET /journal` or the journal file plus `GET /history`, joining the
//! alarm surface from `GET /snapshot` and `GET /signals`, and prints the
//! declared metric set as JSON. No served metrics endpoint exists; the
//! report is computed data, not contract.
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
//! returned receipt. The page also reports its own delivery honesty —
//! the consumer-side gap/freshness state the bounded-publication
//! decision requires: a `since`-cursor read stepping over an evicted
//! stretch marks the feed line "publication gap", a snapshot re-serving
//! the same publication's seq and tick marks it "stale publication",
//! each rendered beside the view — distinct from a peer's unreachable
//! redundancy fault and from a point's non-Good quality — and cleared
//! on the next in-sequence, fresh publication. The snapshot's
//! `io_health` section renders as the
//! I/O-health pane: the executor's boundary counters (failed reads,
//! failed writes, failed cyclic exchanges, consecutive failures) with
//! the last fault's tick and point attribution, the driver's
//! volunteered transport diagnostics as named link degradation
//! distinct from per-point quality — including a cyclic driver's
//! exchange counters (attempted/succeeded, working-counter mismatches,
//! missed deadlines, the last completed exchange's tick) — and the
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
//! [`ParameterRange`](dcs_core::ParameterRange) where present, with the
//! parameter's current value beside it — joined by component and
//! parameter name from the same snapshot's `parameters` section, so a
//! receipted `set_parameter` shows its standing tune on the next poll
//! and a snapshot lacking the section renders the column empty rather
//! than failing. Submitting issues a `set_parameter` command addressed
//! to the instance through the same receipted path — an out-of-range or
//! mistyped entry is warned client-side against the declared range but
//! still sent, the receipted path staying the authority, and the
//! receipt's applied tick or named rejection lands on the parameter's
//! row and in the journal. A kind declaring no parameters renders no
//! edit affordance. A descriptor carrying no role hints and no
//! parameters — a kind with no custom `describe` — degrades to a
//! generic name-plus-diagnostics-plus-wired-points faceplate, never an
//! error.
//!
//! Every faceplate also carries the schema-driven-interface decision's
//! generic surface: a per-component disclosure rendering the served
//! `BlockInterface`'s five resource categories, `GET /schema`'s
//! per-instance registry joined by component and resource name onto
//! `GET /resources`' live state. Measurements and runtime state render
//! as labeled readouts — direction, kind, bound point or `unwired`,
//! value with unit, and quality or `no sample`; configuration rows
//! show the current tune and give `tunable` properties the parameter
//! table's own edit controls, so a configuration edit is the unchanged
//! receipted `set_parameter` path; every named command — the adapted
//! `write_value`/`force_point`/`unforce_point`/`set_parameter`
//! variants and the kind's declared commands alike — is an invocable
//! row whose typed request arguments render controls, whose served
//! unavailability disables the row with the refusal reason shown, and
//! whose submission flows through `submitCommand` so a boundary
//! refusal or an applied tick lands on the row and in the journal; and
//! the recent-events list shows the component's attributed journal
//! tail — bound-point transitions, settled receipts, step failures,
//! kind-emitted events — newest last. A peer predating the two
//! endpoints answers 404 and the disclosure renders nothing — the
//! absent-section convention — and the operator's open/closed choice
//! and in-flight argument entries survive the one-second re-render.
//! For a kind with no dedicated presentation the disclosure is the
//! control surface itself and opens by default; beside dedicated
//! presentation it stays collapsed until opened — kind-specific
//! markup is an enhancement, never the only usable surface.
//!
//! The alarm pane applies the same descriptor-to-point join to the
//! two-flag alarm surface: every status-role `Out` port whose live
//! sample asserts lists with its component, value, quality, and the
//! tick the value last changed in the point's retained history — a
//! non-good quality draws the row degraded rather than as a clean
//! assertion. The managed-alarm decisions extend the pane: the uniform
//! `shelved`/`suppressed`/`out_of_service` status vocabulary routes a
//! component's rows to the named managed list — joined by port name
//! across kinds, the row still reporting `alarm`/`unacknowledged`
//! truth and still counting toward the totals — the declared
//! `priority` renders as colour plus the textual level from the page's
//! declared site vocabulary beside `class` and `response_ticks`, the
//! instance's rationalization record discloses from the served
//! components section, and a state select filters rows by active,
//! unacknowledged, and each managed state. The journal entries
//! touching the alarm-bound points and their components — `journaled`
//! points' `point_changed` transitions included — list in the durable
//! journal's `seq` order beside it, so a burst's initiating cause
//! stands first. Points whose signal `group` a `?protection=<group>`
//! parameter declares render on the visually distinct
//! protection-layer section, the HMI alarm path never presented as
//! the safeguard. Where the component declares an `ack` input wired
//! to a model-declared writable
//! point the pane offers an acknowledge button issuing an ordinary
//! receipted `write_value` — no alarm-specific protocol — pulsed back
//! to `false` once a scan has observed it, because the kind's `ack` is
//! level-observed.
//!
//! ## The pair view
//!
//! Under redundancy — the monitoring-under-redundancy decision — the page
//! presents an active/standby pair as one logical controller. It is
//! configured with both peers' monitor addresses: the serving origin is
//! one peer, and each `?peer=host:port` URL parameter names another —
//! e.g. `http://active:8080/?peer=standby:8081`. `?operator=<name>`
//! configures the declared actor identity the page stamps on every
//! command submission — the audit-attribution decision's page-side
//! source, attestation rather than authentication. Every refresh polls
//! `GET /role` on each configured peer; data fetches go to the peer
//! reporting `active`, so the point listing, trends, and journal are the
//! one logical controller's, while the pair section renders per-peer
//! role, convergence, and reachability — an unreachable peer is a named
//! redundancy fault, not a plant fault. Two peers reporting `active` at
//! once is the dual-active split-brain that contract makes impossible:
//! the pair summary names it as a redundancy fault rather than rendering
//! the pair healthy, and commands find no unique target — nothing is
//! sent while the fault stands. Commands submit only to the
//! settled-active peer; a `not_active` rejection — the command landed
//! mid-transition — triggers a role re-poll and one retry. The contract
//! endpoints answer cross-origin reads (`Access-Control-Allow-Origin: *`)
//! so the page can reach a peer on another host. [`PairClient`] is the
//! same pair view for in-process consumers — tests and tooling — and
//! carries the testable half of the routing rules, including the
//! dual-active verdict through [`PairClient::health`].
//!
//! ## The plant overview
//!
//! With one or more `?pair=<name>=<host:port>,<host:port>` URL
//! parameters — each naming one pair's peers outright in the same
//! `host:port` convention `?peer` uses, with an optional display name —
//! the page switches to the plant-wide overview of the
//! plant-wide-supervision decision: one summary card per configured
//! pair carrying the active peer's identity, each peer's reported role
//! and convergence, the unreachable-peer redundancy fault, and the
//! I/O-health line, each card linking into that pair's own pair view.
//! Aggregation stays page-side over the same JSON endpoints — the
//! served contract is unchanged — and an unreachable pair degrades to a
//! named card fault without interrupting the other cards.
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

pub mod alarm_report;
mod journal_file;
mod pair;
mod recorder;
mod serve;
mod store;

use crate::store::Store;
pub use journal_file::{JournalData, RunBoundary, read_journal_file};
pub use pair::{CONVERGENCE_GRACE, PairClient, PairError, PairHealth, PeerStatus, PeerView};
pub use recorder::MonitorConfig;
pub use store::{Publication, PublicationGap, PublicationPage};

use dcs_core::{
    Command, CommandError, CommandOutcome, CommandReceipt, JournalEntry, PointHistory, PointId,
    PublicationHealth, ResourceView, RoleReport, SchemaView, SwitchError, TelemetrySnapshot, Tick,
};
use dcs_model::SignalIndex;
use dcs_runtime::{ApplyError, Checkpoint, Executor, Peer, ScanError, TrackReport, Transfer};
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use std::io::{self, Cursor, Read, Write};
use std::net::{SocketAddr, TcpStream, ToSocketAddrs};
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tiny_http::{Header, Method, Request, Response, Server};

/// Request body of `POST /scan`: how many scans the executor should run.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct ScanRequest {
    /// The number of scans to run.
    pub scans: u64,
}

/// The attributed `POST /command` envelope — the wire shape recorded
/// for the command-path audit-identity decision:
/// `{"command": <Command>, "actor": "<identity>"}` submitted beside the
/// still-accepted bare [`Command`]. `actor` is the submitter's
/// *declared* identity — attestation, not authentication — carried onto
/// the [`CommandReceipt`] and so into the journaled `CommandSettled`
/// entry; a deployment fronting the monitor with an authenticating
/// proxy fills it from verified context. Strict fields: an envelope
/// carrying neither key's expected shape is a `400`, so a stray
/// top-level `actor` beside a bare command is refused rather than
/// silently dropped.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct CommandEnvelope {
    /// The command to submit.
    command: Command,
    /// The submitter's declared actor identity; absent submits
    /// unattributed.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    actor: Option<String>,
}

/// The monitoring page served at `GET /` — see the crate docs.
pub const PAGE: &str = include_str!("page.html");

/// The `409` body `POST /scan` answers on a paced monitor.
const SCAN_REFUSED_WHEN_PACED: &str = "refused: scans are paced to wall-clock time by this \
     controller; externally requested scans would inject ticks outside the schedule";

/// The dedicated bound on one peer checkpoint fetch — the connect and
/// I/O budget [`CheckpointPuller`]'s pulls, the driven `POST /scan`
/// tracking pull, and the promotion boundary's `final_sync` fetch all
/// carry. An unreachable or wedged active must never stall request
/// serving or the scan cadence on its own network wait: a fetch
/// exceeding the bound simply fails like any refused pull, and a
/// tracking cycle whose pull is still in flight already counts its
/// heartbeat miss.
const CHECKPOINT_PULL_TIMEOUT: Duration = Duration::from_secs(1);

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
/// See the crate docs for the endpoint contract and the two-lock split:
/// `shared` serializes the control plane — scans, commands,
/// checkpoints, role changes — while `store` holds the published read
/// models and served rings every read endpoint copies out without
/// touching it.
pub struct Monitor<'d> {
    shared: Mutex<Shared<'d>>,
    /// The read side: the bounded publication store the read endpoints
    /// serve, owned outside the executor lock. A fetch clones an `Arc`
    /// or an owned copy and releases the store's own small lock before
    /// any serialization or socket I/O; each completed scan swaps a new
    /// immutable [`Publication`] in.
    store: Store,
    signals: SignalIndex,
    server: Server,
    /// When set — [`bind_paced`](Self::bind_paced) — the hosting process
    /// paces scans itself through [`paced_scan`](Self::paced_scan) and
    /// `POST /scan` is refused: the wall clock owns the scan schedule.
    paced: bool,
    /// The per-requested-scan wiring [`driven`](Self::driven) installed —
    /// consulted only on an unpaced monitor, where `POST /scan` runs.
    driven: Driven<'d>,
    /// The tracking source a standby pulls checkpoints from — the
    /// `--standby` target on a paced standby's monitor, `--peer` on a
    /// launched active's, or `Driven`'s `track` on a driven one.
    /// `POST /promote` runs one final pull against it before the gate
    /// lifts ([`Peer::final_sync`]), so a command the active admitted up
    /// to the promote request is carried into the promoted run.
    standby_source: Option<SocketAddr>,
    /// The monitor address a tracking peer announced through its
    /// `GET /checkpoint?peer=` pulls — the follow-peer half of the
    /// tracking-source contract: a peer with no configured source that
    /// is later demoted tracks its successor here, so a launched active
    /// demoted mid-run reconverges and stays promotable instead of
    /// stranding `unsynchronized` forever. Outside `shared`: the value
    /// is request-path bookkeeping, never part of a scan's state.
    announced: Mutex<Option<SocketAddr>>,
}

/// The peer — executor plus redundancy role — and the history recorder,
/// behind one lock so a scan never runs half-recorded and every
/// mutation settles at a scan boundary. The publication store is
/// deliberately outside it: reader work never joins this lock.
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
        Self::bind_paced_peer_with(addr, peer, signals, MonitorConfig::default())
    }

    /// As [`bind_paced_peer`](Self::bind_paced_peer) with explicit
    /// `config` retention bounds and journal-file sink.
    pub fn bind_paced_peer_with<A: ToSocketAddrs>(
        addr: A,
        peer: Peer<'d>,
        signals: SignalIndex,
        config: MonitorConfig,
    ) -> io::Result<Self> {
        let mut monitor = Self::bind_peer_with(addr, peer, signals, config)?;
        monitor.paced = true;
        Ok(monitor)
    }

    /// As [`bind_peer`](Self::bind_peer) with explicit `config`
    /// retention bounds and journal-file sink. Replaying a configured
    /// journal file happens at bind: a file that cannot be replayed
    /// fails startup naming the file and the offending record.
    pub fn bind_peer_with<A: ToSocketAddrs>(
        addr: A,
        peer: Peer<'d>,
        signals: SignalIndex,
        config: MonitorConfig,
    ) -> io::Result<Self> {
        let recorder = recorder::Recorder::new(config, peer.tick())?;
        let store = recorder.store();
        // The bind-time read model is the first publication — any
        // journal tail a configured file replayed rides its event
        // delta — so the read endpoints serve from the store from the
        // moment the monitor exists.
        store.publish(peer.tick(), peer.snapshot(), peer.receipts());
        Ok(Self {
            shared: Mutex::new(Shared { peer, recorder }),
            store,
            signals,
            server: Server::http(addr).map_err(io::Error::other)?,
            paced: false,
            driven: Driven::default(),
            standby_source: None,
            announced: Mutex::new(None),
        })
    }

    /// Arms `POST /scan` with `driven` wiring and returns the monitor —
    /// see [`Driven`]. Meaningful only on an unpaced monitor: a paced
    /// one refuses `POST /scan`, so the wiring never runs. The `track`
    /// address also becomes the promotion-boundary pull's source.
    pub fn driven(mut self, driven: Driven<'d>) -> Self {
        self.standby_source = driven.track;
        self.driven = driven;
        self
    }

    /// Records the address a tracking standby pulls checkpoints from —
    /// a paced `--standby` run's target, which the pacing loop owns and
    /// `Driven` never sees. `POST /promote` runs one final pull against
    /// it so the promoted run carries every command the active admitted
    /// up to the promote request.
    pub fn with_standby_source(mut self, source: SocketAddr) -> Self {
        self.standby_source = Some(source);
        self
    }

    /// The checkpoint address this peer tracks — `Driven`'s `track` or
    /// the configured [`with_standby_source`](Self::with_standby_source)
    /// when set, else the monitor address a tracking peer announced
    /// through its `GET /checkpoint?peer=` pulls. The announced fallback
    /// is the follow-peer half of the tracking-source contract: a peer
    /// launched without a source — an active never told its peer — that
    /// is later demoted tracks its successor here and reconverges
    /// instead of stranding `unsynchronized` and unpromotable.
    pub fn tracking_source(&self) -> Option<SocketAddr> {
        self.driven
            .track
            .or(self.standby_source)
            .or_else(|| *self.announced.lock().unwrap())
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

    /// Runs one executor scan through the shared lock, records it, and
    /// publishes its immutable read model — the entry point for a
    /// process pacing its own scan loop once the monitor owns the
    /// executor.
    ///
    /// Holding the mutex for the scan, recording, and publication swap
    /// keeps the documented interleaving: a request never observes a
    /// half-run scan, and a command submitted between scans still
    /// applies at the next scan's boundary. The scan is recorded and
    /// published exactly like an endpoint-driven one, so the read
    /// endpoints track the paced run; their serving work stays off the
    /// lock. A pending role transition settles on the completed scan
    /// and its journal entry follows the scan's own events.
    pub fn paced_scan(&self) -> Result<Tick, ScanError> {
        let mut shared = self.shared.lock().unwrap();
        scan_and_record(&mut shared, &self.store)
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
    ///
    /// This is the control-plane view — the executor's own report,
    /// built on demand — not the published read model: a consumer that
    /// wants the served copy, built once per completed scan, reads
    /// [`published`](Self::published).
    pub fn snapshot(&self) -> TelemetrySnapshot {
        self.shared.lock().unwrap().peer.snapshot()
    }

    /// The latest published read model — the immutable copy `GET
    /// /snapshot` serializes: materialized once per completed scan and
    /// carrying its publication sequence, its scan's tick, the history
    /// and journal deltas appended since the previous publication, and
    /// the receipt log as of the scan. `None` only before the bind-time
    /// publication every `bind_*` performs.
    ///
    /// Holding the returned `Arc` never blocks a scan: publications are
    /// immutable, and the store ages its bounded retained window
    /// forward around them.
    pub fn published(&self) -> Option<Arc<Publication>> {
        self.store.latest()
    }

    /// Reads the retained publication window from a seq cursor — the
    /// in-process form of a consumer's "everything newer than what I
    /// last saw" read. Publications newer than `seq` come back oldest
    /// first; when the cursor's successors already aged out of the
    /// bounded window the page carries the named [`PublicationGap`],
    /// and the consumer coalesces onto the retained tail or
    /// [`published`](Self::published)'s latest state instead of ever
    /// backpressuring the run.
    pub fn publications_since(&self, seq: u64) -> PublicationPage {
        self.store.page_since(seq)
    }

    /// The publication store's overload counters as they stand now —
    /// the same [`PublicationHealth`] report the latest publication's
    /// snapshot `publication` section carries as of its publish.
    pub fn publication_health(&self) -> PublicationHealth {
        self.store.health()
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
        // An adopted checkpoint carries the active's receipt log —
        // refresh the store's mirror so `GET /receipts` stays current
        // before the next scan publishes.
        self.store.sync_receipts(peer.receipts());
        result
    }

    /// Consumes a checkpoint pulled from the active peer under the same
    /// lock — the pull entry point for a peer that may be armed for a
    /// rolling model revision. A matching fingerprint applies as
    /// ordinary convergence exactly like
    /// [`apply_checkpoint`](Self::apply_checkpoint); a foreign
    /// fingerprint on a revision-armed peer crosses the model boundary
    /// and the returned [`Transfer::Reinitialized`] carries its
    /// [`CarryoverReport`](dcs_core::CarryoverReport), also journaled
    /// once per transition at the resumed tick.
    pub fn transfer_checkpoint(&self, checkpoint: &Checkpoint) -> Result<Transfer, ApplyError> {
        let mut shared = self.shared.lock().unwrap();
        let Shared { peer, recorder } = &mut *shared;
        let result = peer.transfer(checkpoint);
        for report in peer.take_divergences() {
            recorder.note_divergence(report.tick, report.mismatches);
        }
        for report in peer.take_reinitializations() {
            recorder.note_reinitialized(report);
        }
        self.store.sync_receipts(peer.receipts());
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

    /// Runs the standby's per-scan tracking cycle — the paced loop's
    /// pre-scan half: `pull` fetches the active's checkpoint once,
    /// routed through
    /// [`Peer::track_once`](dcs_runtime::Peer::track_once)'s owns-field
    /// gate, miss accounting, and promote-on-budget sequence — and the
    /// transitions it queued (divergence detections, reinitialization
    /// reports, the role change a self-promotion reported) drain into
    /// the recorder. The returned [`TrackReport`] is the caller's to
    /// present; the journal already holds its transitions.
    ///
    /// The fetch itself runs *outside* the shared lock — an
    /// unreachable or slow active must not serialize request serving
    /// behind the pull's network wait. The owns-field gate is checked
    /// under the lock first (a field owner pulls nothing), `pull` runs
    /// unlocked, and its result is consumed under the lock, where
    /// `track_once` re-applies the gate: a checkpoint fetched while a
    /// promotion landed is discarded. Callers should still keep `pull`
    /// cheap — e.g. a [`CheckpointPuller::poll`] consuming a fetch
    /// worker's completed pull — since the cycle itself waits on it.
    pub fn track_cycle(&self, pull: impl FnOnce() -> Result<Checkpoint, String>) -> TrackReport {
        if self.shared.lock().unwrap().peer.owns_field() {
            return TrackReport::OwnsField;
        }
        let pulled = pull();
        track_and_record(&mut self.shared.lock().unwrap(), &self.store, move || {
            pulled
        })
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
            // The read endpoints fetch the published copy — an `Arc`
            // clone or an owned stream — releasing the store's lock
            // inside the call, then serialize and write it: no part of
            // serving a reader ever holds the executor lock.
            (Method::Get, "/snapshot") => match self.store.latest() {
                Some(publication) => json(200, &publication.snapshot),
                // Bind always publishes the seed read model; a store
                // without one can only mean the monitor was never bound.
                None => json(503, "no publication yet"),
            },
            (Method::Get, "/receipts") => json(200, &*self.store.receipts()),
            (Method::Get, "/checkpoint") => {
                // The follow-peer half of the tracking-source
                // contract: a tracking peer announces its own monitor
                // address on the pull, so this instance knows where to
                // track if it is later demoted.
                if let Some(announced) = checkpoint_peer(query) {
                    *self.announced.lock().unwrap() = Some(announced);
                }
                json(200, &self.shared.lock().unwrap().peer.checkpoint())
            }
            (Method::Get, "/role") => json(200, &self.shared.lock().unwrap().peer.report()),
            (Method::Get, "/history") => match history_query(query) {
                Ok((points, since)) => json(200, &self.store.history(&points, since)),
                Err(message) => json(400, &message),
            },
            (Method::Get, "/journal") => match journal_query(query) {
                Ok(since) => json(200, &self.store.journal(since)),
                Err(message) => json(400, &message),
            },
            // The schema and resource views derive from the published
            // read model exactly like `/snapshot` — one `Arc` fetch, the
            // store's small lock released before the join or any socket
            // I/O, the journal tail read from the same served ring.
            (Method::Get, "/schema") => match self.store.latest() {
                Some(publication) => json(200, &serve::schema_view(&publication, &self.signals)),
                None => json(503, "no publication yet"),
            },
            (Method::Get, "/resources") => match self.store.latest() {
                Some(publication) => json(
                    200,
                    &serve::resource_view(&publication, &self.signals, &self.store.journal(0)),
                ),
                None => json(503, "no publication yet"),
            },
            (Method::Post, "/promote") => self.switchover(true),
            (Method::Post, "/demote") => self.switchover(false),
            (Method::Post, "/command") => match read_command_submission(&mut request) {
                Ok(CommandEnvelope { command, actor }) => {
                    let mut shared = self.shared.lock().unwrap();
                    let Shared { peer, recorder } = &mut *shared;
                    // Only the settled-active peer accepts commands: on a
                    // standby or mid-transition instance the write gate
                    // would keep the write from the field, so refuse with
                    // a receipt rather than report a phantom application.
                    // Either way the declared actor is stamped onto the
                    // receipt — the settled entry the journal echoes.
                    let receipt = if peer.accepts_commands() {
                        let receipt = peer.submit_command_as(command, actor);
                        let index = peer.receipts().len() - 1;
                        let tick = peer.tick();
                        recorder.note_command(index, receipt.clone(), tick);
                        // Refresh the store's mirror so `GET /receipts`
                        // answers the just-submitted receipt before its
                        // scan boundary settles it.
                        self.store.sync_receipts(peer.receipts());
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
                            actor,
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
                        // before its scan, consolidated in
                        // `Peer::track_once`. What the pull did — a
                        // rejected checkpoint, a failed fetch, a refused
                        // promotion — reports through the peer's named
                        // sync state, which `GET /role` serves; the scan
                        // still runs on its last-known state.
                        if let Some(active) = self.tracking_source() {
                            let own = self.local_addr();
                            track_and_record(&mut shared, &self.store, || {
                                MonitorClient::with_timeout(active, CHECKPOINT_PULL_TIMEOUT)
                                    .checkpoint_announcing(own)
                                    .map_err(|error| format!("fetch from {active}: {error}"))
                            });
                        }
                        if let Err(error) = scan_and_record(&mut shared, &self.store) {
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
                        // The last scan already published its read model —
                        // answer with that immutable copy, released from
                        // the executor lock before serialization.
                        None => {
                            drop(shared);
                            match self.store.latest() {
                                Some(publication) => json(200, &publication.snapshot),
                                None => json(503, "no publication yet"),
                            }
                        }
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
    ///
    /// Promotion runs one best-effort final synchronization against the
    /// tracking source first ([`Peer::final_sync`]): a command the
    /// active admitted after the standby's last tracking pull — still
    /// `Accepted`, riding the checkpoint's receipt log — carries into
    /// the promoted run and settles at its next boundary. A failed or
    /// stale pull leaves the standing convergence to decide, exactly as
    /// an unpulled promote would.
    fn switchover(&self, promote: bool) -> Response<Cursor<Vec<u8>>> {
        // The final-sync fetch runs outside the shared lock under the
        // dedicated pull bound — like the tracking pull it can wait on
        // an unreachable peer, and that wait must stall only this
        // request, never the lock's hold or the paced scan. A peer
        // already owning the field has no source to sync from — the
        // gate `Peer::final_sync` itself applies — so it fetches
        // nothing; the consume below re-applies the gate, discarding a
        // checkpoint fetched while a concurrent promotion landed.
        let pulled = match self.tracking_source() {
            Some(source) if promote && !self.shared.lock().unwrap().peer.owns_field() => Some(
                MonitorClient::with_timeout(source, CHECKPOINT_PULL_TIMEOUT)
                    .checkpoint_announcing(self.local_addr())
                    .map_err(|error| format!("fetch from {source}: {error}")),
            ),
            _ => None,
        };
        let mut shared = self.shared.lock().unwrap();
        let Shared { peer, recorder } = &mut *shared;
        let result = if promote {
            if let Some(pulled) = pulled {
                peer.final_sync(|| pulled);
                for divergence in peer.take_divergences() {
                    recorder.note_divergence(divergence.tick, divergence.mismatches);
                }
                for report in peer.take_reinitializations() {
                    recorder.note_reinitialized(report);
                }
                self.store.sync_receipts(peer.receipts());
            }
            peer.promote()
        } else if peer.owns_field() && self.tracking_source().is_none() {
            // A field owner with no tracking source — nothing
            // configured and no standby that announced itself — would
            // demote into a permanently unsynchronized standby that no
            // pull can ever reconverge; refuse up front rather than
            // silently marooning the instance.
            Err(SwitchError::NoTrackingSource)
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

/// One standby tracking cycle plus its journal recording — the body
/// `track_cycle` and `POST /scan` share: `Peer::track_once` runs the
/// owns-field gate, the pull routing, the miss accounting, and the
/// promote-on-budget sequence, then the queues it filled — divergence
/// detections, reinitialization reports, the role change a
/// self-promotion reported — drain into the recorder in report order.
/// An applied checkpoint adopts the peer's receipt log, so the store's
/// mirror refreshes before the next scan publishes. Only the consume
/// half belongs under the lock — `track_cycle` runs its fetch before
/// taking it, and `POST /scan`'s in-request pull carries the dedicated
/// [`CHECKPOINT_PULL_TIMEOUT`] bound.
fn track_and_record(
    shared: &mut Shared<'_>,
    store: &Store,
    pull: impl FnOnce() -> Result<Checkpoint, String>,
) -> TrackReport {
    let Shared { peer, recorder } = shared;
    let report = peer.track_once(pull);
    for divergence in peer.take_divergences() {
        recorder.note_divergence(divergence.tick, divergence.mismatches);
    }
    for report in peer.take_reinitializations() {
        recorder.note_reinitialized(report);
    }
    for change in peer.take_role_changes() {
        recorder.note_role_change(change.tick, change.from, change.to);
    }
    store.sync_receipts(peer.receipts());
    report
}

/// One executor scan, its recording, and its publication — the body
/// `paced_scan` and `POST /scan` share: the scan runs under the lock,
/// its history and journal entries are attributed to the scan's tick, a
/// role transition the scan settled is journaled after the scan's own
/// events, and the once-materialized snapshot publishes into the
/// bounded store the read endpoints serve. The lock's hold ends at the
/// swap — the consumer side never joins it.
fn scan_and_record(shared: &mut Shared<'_>, store: &Store) -> Result<Tick, ScanError> {
    let Shared { peer, recorder } = shared;
    let tick = match peer.scan() {
        Ok(tick) => tick,
        Err(error) => {
            // A scan aborted mid-way is not recorded — the run ends at
            // it — but its boundary already counted the I/O faults
            // into `io_health`: publish the faulted boundary's state
            // so the served read model reports the fault rather than
            // sitting on the last healthy scan. A fenced write on a
            // peer that cannot quiesce it — no gate — still lands here
            // carrying its claim-loss report, which journals the same
            // way: the event belongs to the run's audit trail, not only
            // the exit cause.
            for loss in peer.take_fencing_losses() {
                recorder.note_field_claim_lost(loss.tick, loss.point);
            }
            store.publish(peer.tick(), peer.snapshot(), peer.receipts());
            return Err(error);
        }
    };
    let snapshot = recorder.record_scan(peer.executor(), tick);
    // A field write the plant fenced — the claim this owner held was
    // preempted — completed the scan degraded and demoted the peer
    // inside it rather than failing it: the claim loss and the role
    // transition it drove journal beside the scan's own events, cause
    // before effect, beside the `io_health` fault the boundary
    // already counted.
    for loss in peer.take_fencing_losses() {
        recorder.note_field_claim_lost(loss.tick, loss.point);
    }
    for change in peer.take_role_changes() {
        recorder.note_role_change(change.tick, change.from, change.to);
    }
    store.publish(tick, snapshot, peer.receipts());
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

/// The `/checkpoint` query's `peer` key — the pulling monitor's own
/// address, announced so this instance knows where to track after a
/// demotion. An absent or unparseable value simply announces nothing:
/// the checkpoint itself is still served, keeping older pullers and
/// plain `GET /checkpoint` readers compatible.
fn checkpoint_peer(query: &str) -> Option<SocketAddr> {
    query_pairs(query).find_map(|(key, value)| {
        if key == "peer" {
            value.parse().ok()
        } else {
            None
        }
    })
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

/// Reads a `POST /command` body into its [`CommandEnvelope`]. The
/// attributed shape — `{"command":…,"actor":…}` — is selected by either
/// envelope key; anything else parses as the bare [`Command`]
/// pre-attribution shape, so existing clients submit unchanged and
/// journal unattributed (`actor: None`). A body naming `command` or
/// `actor` without the envelope's shape is a `400` — an attribution the
/// body meant to carry never silently drops. Parse failures produce the
/// `400` response directly.
fn read_command_submission(
    request: &mut Request,
) -> Result<CommandEnvelope, Response<Cursor<Vec<u8>>>> {
    let mut body = String::new();
    if request.as_reader().read_to_string(&mut body).is_err() {
        return Err(json(400, "unreadable request body"));
    }
    let value: serde_json::Value = match serde_json::from_str(&body) {
        Ok(value) => value,
        Err(error) => return Err(json(400, &error.to_string())),
    };
    let attributed = value
        .as_object()
        .is_some_and(|object| object.contains_key("command") || object.contains_key("actor"));
    if attributed {
        serde_json::from_value::<CommandEnvelope>(value)
            .map_err(|error| json(400, &error.to_string()))
    } else {
        serde_json::from_value::<Command>(value)
            .map(|command| CommandEnvelope {
                command,
                actor: None,
            })
            .map_err(|error| json(400, &error.to_string()))
    }
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

/// A tracking standby's checkpoint-fetch worker: performs the
/// pull-per-scan-cycle resync on its own thread so the network wait —
/// connect, transfer, or a peer that never answers — is never part of
/// the scan cycle's or the monitor lock's critical path.
///
/// The worker runs one fetch at a time against the active's monitor,
/// each bounded by [`CHECKPOINT_PULL_TIMEOUT`], and only when a scan
/// cycle asked for one — the documented cadence stays one pull per
/// cycle, never a free-running poll hammering the serving peer. A
/// cycle calls [`poll`](Self::poll) exactly once as its tracking pull:
/// non-blocking, it consumes the latest completed fetch and requests
/// the next, answering `Ok(checkpoint)` when a fetch produced one
/// since the previous poll and `Err` otherwise — the fetch's own
/// error detail, or the still-in-flight state while it runs. A cycle
/// whose pull has not yet produced a checkpoint is a heartbeat miss
/// exactly like a refused one, so the failover budget keeps measuring
/// wall time — budget × scan period — instead of fetch latency: an
/// active that cannot serve a checkpoint within the window is declared
/// lost on the same cadence a refused connect would be.
///
/// Feed `poll` to [`Monitor::track_cycle`] or
/// [`Peer::track_once`](dcs_runtime::Peer::track_once) as the `pull`.
/// A field-owning peer's cycle never invokes it, so fetching idles on
/// promotion and resumes on demotion; dropping the puller ends the
/// worker thread once its in-flight fetch resolves.
pub struct CheckpointPuller {
    /// The pull target — the active's monitor address.
    active: SocketAddr,
    /// Wakes the worker for one fetch; a send arms `pending`.
    requests: mpsc::Sender<()>,
    /// Completed fetches with their completion instant, consumed by
    /// [`poll`](Self::poll).
    results: mpsc::Receiver<(Instant, Result<Checkpoint, String>)>,
    /// A fetch request is outstanding — sent and not yet consumed.
    pending: bool,
    /// The last completed fetch's error, kept so a cycle polling while
    /// a fetch is still in flight reports the most recent real detail
    /// rather than only the in-flight state.
    last_error: Option<String>,
}

impl CheckpointPuller {
    /// Spawns the fetch worker for a standby tracking the active at
    /// `active` — its monitor address, the same `--standby` target.
    /// `announce`, when set, is this monitor's own address carried on
    /// each fetch as `?peer=` — the announcement that gives the serving
    /// peer somewhere to track if it is demoted later.
    pub fn new(active: SocketAddr, announce: Option<SocketAddr>) -> Self {
        let (requests, request_rx) = mpsc::channel::<()>();
        let (result_tx, results) = mpsc::channel();
        std::thread::spawn(move || {
            let client = MonitorClient::with_timeout(active, CHECKPOINT_PULL_TIMEOUT);
            // One fetch per request; the channels closing — the puller
            // dropped — ends the loop.
            while request_rx.recv().is_ok() {
                let pulled = match announce {
                    Some(own) => client.checkpoint_announcing(own),
                    None => client.checkpoint(),
                }
                .map_err(|error| format!("fetch from {active}: {error}"));
                if result_tx.send((Instant::now(), pulled)).is_err() {
                    return;
                }
            }
        });
        Self {
            active,
            requests,
            results,
            pending: false,
            last_error: None,
        }
    }

    /// The tracking cycle's pull, once per scan: consumes the latest
    /// completed fetch — `Ok` applies it, `Err` counts the cycle's
    /// heartbeat miss — and requests the next when none is
    /// outstanding. A fetch still in flight answers `Err` carrying the
    /// last completed fetch's error, or the in-flight state when no
    /// fetch has finished yet; the miss is honest either way — this
    /// cycle produced no checkpoint.
    ///
    /// A completed result older than [`CHECKPOINT_PULL_TIMEOUT`] is
    /// discarded as stale rather than applied: polls pause while the
    /// peer owns the field, and a checkpoint captured before a
    /// promotion would otherwise land long after its fetch — rewinding
    /// the run to a tick it already passed. Discarding it simply
    /// counts the cycle's miss; the next fetch reconverges fresh.
    pub fn poll(&mut self) -> Result<Checkpoint, String> {
        let mut latest = None;
        while let Ok(result) = self.results.try_recv() {
            self.pending = false;
            if let Err(detail) = &result.1 {
                self.last_error = Some(detail.clone());
            }
            latest = Some(result);
        }
        // A result is fresh only while its fetch could still have
        // completed inside the pull bound.
        if let Some((completed, _)) = &latest
            && completed.elapsed() > CHECKPOINT_PULL_TIMEOUT
        {
            latest = None;
        }
        if !self.pending {
            if self.requests.send(()).is_err() {
                return Err(format!(
                    "fetch from {}: the pull worker is gone",
                    self.active
                ));
            }
            self.pending = true;
        }
        latest.map(|(_, result)| result).unwrap_or_else(|| {
            Err(self.last_error.clone().unwrap_or_else(|| {
                format!(
                    "fetch from {}: checkpoint pull still in flight",
                    self.active
                )
            }))
        })
    }
}

/// A lightweight in-process client for the [`Monitor`] endpoints.
///
/// One request per call over a fresh `TcpStream`; responses are decoded
/// into the `dcs-core` contract types. Any non-`200` status surfaces as an
/// [`io::Error`] carrying the response body.
pub struct MonitorClient {
    addr: SocketAddr,
    /// When set — [`with_timeout`](Self::with_timeout) — the connect,
    /// read, and write bound every request carries.
    timeout: Option<Duration>,
}

impl MonitorClient {
    /// A client for the monitor bound at `addr` (see
    /// [`Monitor::local_addr`]).
    pub fn new(addr: SocketAddr) -> Self {
        Self {
            addr,
            timeout: None,
        }
    }

    /// As [`new`](Self::new) with `timeout` bounding every request's
    /// connect, read, and write — the client a caller reaches for when
    /// the wait itself must stay bounded, like a peer checkpoint fetch
    /// that must never stall the serving or scan side on an
    /// unreachable peer.
    pub fn with_timeout(addr: SocketAddr, timeout: Duration) -> Self {
        Self {
            addr,
            timeout: Some(timeout),
        }
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

    /// `GET /checkpoint?peer=<addr>`: the tracking pull — the
    /// checkpoint fetch plus the follow-peer announcement: `peer`
    /// names this client's own monitor address, which the serving
    /// monitor records as its tracking source for a later demotion.
    pub fn checkpoint_announcing(&self, peer: SocketAddr) -> io::Result<Checkpoint> {
        self.get_json(&format!("/checkpoint?peer={peer}"))
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

    /// `GET /schema`: the served block-interface registry — every
    /// component instance's `BlockInterface`, stamped with the
    /// publication it was derived from.
    pub fn schema(&self) -> io::Result<SchemaView> {
        self.get_json("/schema")
    }

    /// `GET /resources`: per-instance live resource state — measurement
    /// and state values with quality, current configuration, per-command
    /// availability or refusal, and the retained events attributed to
    /// each instance.
    pub fn resources(&self) -> io::Result<ResourceView> {
        self.get_json("/resources")
    }

    /// `POST /command`: submits `command`, returning its receipt —
    /// `accepted` when queued for the next scan boundary, `rejected` with
    /// a named reason otherwise. The bare-`Command` body submits
    /// unattributed.
    pub fn command(&self, command: &Command) -> io::Result<CommandReceipt> {
        self.post_json("/command", command)
    }

    /// `POST /command` with `actor` as the submitter's declared identity
    /// — the attributed envelope `{"command":…,"actor":…}`; the returned
    /// receipt and the journaled `CommandSettled` carry the attribution.
    /// `None` submits the same bare-`Command` body
    /// [`command`](Self::command) sends, journaling unattributed.
    pub fn command_as(&self, command: &Command, actor: Option<&str>) -> io::Result<CommandReceipt> {
        match actor {
            Some(actor) => self.post_json(
                "/command",
                &CommandEnvelope {
                    command: command.clone(),
                    actor: Some(actor.to_string()),
                },
            ),
            None => self.command(command),
        }
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
        let mut stream = match self.timeout {
            Some(timeout) => TcpStream::connect_timeout(&self.addr, timeout)?,
            None => TcpStream::connect(self.addr)?,
        };
        if let Some(timeout) = self.timeout {
            stream.set_read_timeout(Some(timeout))?;
            stream.set_write_timeout(Some(timeout))?;
        }
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
