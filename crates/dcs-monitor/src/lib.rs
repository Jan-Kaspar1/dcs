//! HTTP+JSON monitoring access to a running executor.
//!
//! [`Monitor`] exposes a [`dcs_runtime::Executor`] over `tiny_http` — a
//! small synchronous HTTP server, so no async runtime is involved.
//! Requests dispatch across two worker lanes: a submission lane for
//! requests that can hold a worker on a client-paced wait —
//! `POST /command` and `POST /scan`, the only handlers that read a
//! request body, plus any request still carrying a body the client
//! owes (dropping its live reader drains the remainder, the same
//! unbounded wait) — and a serving lane for everything else. A client
//! that stalls mid-body pins at most the small submission pool; the
//! serving lane — `GET /checkpoint` among it, the heartbeat a tracking
//! standby measures the active's liveness by — keeps answering, so
//! request-body traffic can never impersonate a dead active. Within a
//! lane a request whose handling legitimately waits on the network —
//! a driven `POST /scan` batch's per-scan checkpoint pull, a
//! promotion's final-sync fetch — stalls only its own worker instead
//! of head-of-line blocking every endpoint behind it, and body reads
//! themselves are bounded: a declared or delivered body past
//! [`MAX_REQUEST_BODY`] is refused `413`. The executor lives behind a
//! [`Mutex`]
//! the control-plane endpoints and the scan loop share — scans, commands,
//! checkpoints, and role changes hold it for their mutation, so a
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
//!   changes it, so a submission between scans is immediately visible.
//!   The log is a bounded tail
//!   ([`Executor::with_receipt_log_capacity`](dcs_runtime::Executor::with_receipt_log_capacity)):
//!   settled receipts evict oldest-first while pending ones never do,
//!   and the evicted count reads off the snapshot's
//!   `command_queue.attempts` minus the served length — the same
//!   numbering-gap convention the history and journal streams use
//! - `GET /history` → `200` `Vec<`[`PointHistory`]`>` — each mapped
//!   point's retained samples in tick order from the store's bounded
//!   rings; `?point=<id>` (repeatable) selects points and
//!   `?since=<seq>` returns only samples newer than the caller's last
//!   seen sequence — an evicted stretch surfaces as a numbering gap
//! - `GET /journal` → `200` `Vec<`[`JournalEntry`]`>` — the transition
//!   journal in scan order; `?since=<seq>` filters likewise. The tail
//!   is bounded, but `run_boundary` entries are pinned: evicting one
//!   would silently merge two process lifetimes in the served audit,
//!   so the markers outlive ordinary event volume and answer ahead of
//!   the retained tail in `seq` order
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
//!   refusal a submission would meet, and the attributed event streams
//!   — the retained journal tail's entries (its bound points'
//!   transitions, its settled command receipts, its step failures, its
//!   `Journal`-retained and undeclared emissions) beside its
//!   `History`-retained emissions' bounded ring records and its
//!   `Latest`-retained emissions' standing records, each entry's
//!   `retention` marking the store it came from. Collections are
//!   parallel to the interface's, so a
//!   consumer zips the schema and resource views by index or joins by
//!   `name`. Both reads serve published copies — never the executor
//!   lock — exactly like `/snapshot`
//! - `GET /checkpoint` → `200` [`Checkpoint`] — the executor's current
//!   transferable state. This is the peer-sync endpoint a standby
//!   controller pulls from (the peer-transport decision): like every
//!   request it is served at a scan boundary under the executor lock, so
//!   the checkpoint is always a consistent between-scans capture. A
//!   pull's `?peer=` announces the pulling monitor's own address — the
//!   follow-peer half of the tracking-source contract, accepted only
//!   when it names the request's own source address (a wildcard-bound
//!   puller's `0.0.0.0` resolves to that address, so a demotion never
//!   follows an undialable source) — so this instance
//!   knows where to track if it is later demoted
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
//!   (`accepted` / `rejected` outcome); an unparseable body → `400`,
//!   a body past [`MAX_REQUEST_BODY`] → `413`.
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
//!   metrics ride the snapshot's `command_queue` section. With a
//!   [`CommandPersist`] installed ([`Monitor::with_command_persist`],
//!   the controller's `--state-file` wiring), an accepted admission is
//!   persisted before the `200` answers, so a restart between admission
//!   and the applying scan re-queues the carried receipt rather than
//!   losing the command unaudited
//! - `POST /scan`, body [`ScanRequest`] → runs that many scans → `200`
//!   [`TelemetrySnapshot`] taken after the last one; a failing
//!   [`Driven::after_scan`] hook → `500`; refused with `409` on a paced
//!   monitor before the body is even read (see below), and the same
//!   `413` bound [`MAX_REQUEST_BODY`] gives `/command`
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
//! marker line separates process lifetimes within the file, and a
//! restart's marker also journals once as a `run_boundary` entry so a
//! `GET /journal` consumer attributes entries to a process lifetime —
//! so `GET /journal` answers continuously across a restart. Those
//! served markers are pinned out of the tail's bound: ordinary event
//! volume can age the boundary past the retained window, but the
//! served answer keeps it — pinned entries answer ahead of the
//! retained tail, and the replay keeps a marker its own tail bound
//! evicted. A file that
//! cannot be replayed fails startup naming the file and the offending
//! record, and a missing file is a cold start. A run resumed through
//! `--state-file` diffs its first scan against the restored executor
//! state and the replayed record's last observations, so the audit
//! trail continues rather than re-journaling what it already recorded.
//! Point history stays volatile; only the journal persists. The file is
//! single-writer: the bind holds an exclusive advisory lock on the path
//! for the monitor's lifetime, so two monitors configured with the same
//! `journal_file` cannot interleave duplicate `seq`s into one
//! un-replayable record — the second bind fails naming the file and the
//! live-holder conflict, and a dead holder's lock releases with its
//! descriptor so a restart re-acquires it.
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
pub use pair::{
    CONVERGENCE_GRACE, PAIR_FAULT_KINDS_VERSION, PairClient, PairError, PairFaultKind, PairHealth,
    PeerStatus, PeerView,
};
pub use recorder::MonitorConfig;
pub use store::{Publication, PublicationGap, PublicationPage};

use dcs_core::{
    CarryoverReport, Command, CommandError, CommandOutcome, CommandReceipt, JournalEntry,
    PointHistory, PointId, PublicationHealth, ResourceView, RoleReport, SchemaView, SwitchError,
    TelemetrySnapshot, Tick,
};
use dcs_model::SignalIndex;
use dcs_runtime::{
    ApplyError, Checkpoint, Executor, Peer, SUPPORTED_FORMAT_VERSIONS, TrackReport, Transfer,
};
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use std::collections::VecDeque;
use std::io::{self, Cursor, Read, Write};
use std::net::{SocketAddr, TcpStream, ToSocketAddrs};
use std::sync::mpsc;
use std::sync::{Arc, Condvar, Mutex};
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

/// How far ahead of this run's own tick a checkpoint pulled to verify
/// an announced demotion hint may serve: a successor tracking this run
/// applies this run's checkpoints and scans alongside it, so it can
/// honestly sit a few ticks ahead — but a same-generation stream far
/// ahead of the run's tick is not this line's continuation. It is the
/// signature of a forged or foreign stream served from an
/// attacker-chosen endpoint, and the demotion refuses it rather than
/// moving the run onto it. Thirty-two ticks is a short skew window at
/// any deployed scan period — far beyond the lockstep drift of a real
/// tracking peer, far below any forgery worth serving.
const MAX_ANNOUNCED_AHEAD: u64 = 32;

/// The worker count [`Monitor::serve`] dispatches the serving lane
/// across — every request that cannot hold a worker on a client-paced
/// wait: the GET reads, the `POST /promote`/`POST /demote` role
/// changes, and the `404`s. tiny_http queues accepted requests
/// internally; a dispatcher routes each onto this lane's own queue,
/// and each worker handles one end to end. The pool exists so a
/// request whose work legitimately waits on the network — a
/// promotion's final-sync fetch — stalls only its own worker while
/// every other endpoint keeps answering; control-plane mutations
/// still serialize on the shared lock, the pool only choosing which
/// request waits on it next.
const SERVE_WORKERS: usize = 4;

/// The worker count serving the submission lane — `POST /command` and
/// `POST /scan`, the only handlers that read a request body, plus any
/// request still carrying a body the client owes: reading that body
/// waits on the client, and dropping its reader drains the remainder,
/// the same unbounded wait. tiny_http exposes no socket timeout to
/// bound either wait: a client that stalls mid-body holds its worker
/// for as long as it cares to. Those client-paced waits are
/// quarantined on this lane so the serving lane — `GET /checkpoint`
/// among it, the heartbeat a tracking standby measures the active's
/// liveness by — keeps answering through a stalled-body flood, per
/// the disposable-consumer contract. Two workers keep a long
/// `POST /scan` batch from queueing every command behind it; a flood
/// beyond the lane's width can still starve submissions, but never
/// the served surface.
const SUBMIT_WORKERS: usize = 2;

/// The bound on a request body the monitor will read — far past the
/// largest legitimate body, a `Command` envelope or `ScanRequest` of
/// tens of bytes. A request declaring more is refused `413` before a
/// byte is read, and the read itself is `take`-bounded so a chunked or
/// understated body cannot grow the buffer past the cap either.
const MAX_REQUEST_BODY: u64 = 64 * 1024;

/// Runs once after each completed requested scan, receiving the peer —
/// the plant step the driving request paces the run to (its field
/// ownership decides the step), and the checkpoint a state-file run
/// persists at that boundary. A failure fails the request with `500`.
pub type AfterScan<'d> = Box<dyn Fn(&Peer<'d>) -> Result<(), String> + Send + Sync + 'd>;

/// Runs at a command's admission boundary — inside `POST /command`,
/// after the accepted receipt lands in the executor's log and before
/// the request answers — receiving the run's just-captured
/// [`Checkpoint`]. A `--state-file` run installs its persist here so an
/// accepted command is durable before its `200` receipt is promised:
/// the checkpoint's receipt log carries the admission and a resume
/// re-queues it, so a restart between admission and the applying scan
/// re-applies the command instead of losing it unaudited — the same
/// takeover guarantee the checkpoint contract gives a promoted standby.
/// The call runs under the shared lock, serializing with the cycle-end
/// persist a paced or driven run performs through
/// [`persist_state`](Monitor::persist_state), so the two writes can
/// never interleave into a stale overwrite. A failure is fatal — the
/// run dies naming it rather than answering a receipt it cannot
/// recover, the same rule the journal-file sink applies.
pub type CommandPersist = Box<dyn Fn(&Checkpoint) -> Result<(), String> + Send + Sync>;

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
    /// The persist a `--state-file` run performs at a command's
    /// admission boundary — see [`with_command_persist`](Self::with_command_persist).
    command_persist: Option<CommandPersist>,
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
    /// stranding `unsynchronized` forever. An announce lands only when
    /// it names the pulling connection's own source address — or the
    /// wildcard a `0.0.0.0`-bound puller sends, which resolves to that
    /// address — so the read endpoint can neither rewrite the tracking
    /// source for an unrelated client nor record one no peer can
    /// dial. Landing is not trusting: the serving side cannot tell the
    /// puller's monitor port from any other same-IP port the
    /// connection's source claims, so a recorded hint stays unverified
    /// until a demotion proves it — `POST /demote` pulls one
    /// checkpoint from the hint and adopts it only when the checkpoint
    /// continues this run's line, journaling the adopted source. A
    /// dead or forged hint refuses `NoTrackingSource` instead of
    /// stranding or adopting. Outside `shared`: the value is
    /// request-path bookkeeping, never part of a scan's state.
    announced: Mutex<Option<SocketAddr>>,
    /// The tracking source a verified announced demotion pinned — the
    /// endpoint `POST /demote` proved serves this run's continuation
    /// and journaled as the adopted source. The demoted peer's pulls
    /// target it rather than re-reading `announced`, so a later
    /// `?peer=` rewrite — the same unauthenticated mutation that
    /// planted the hint — cannot move the tracking onto an endpoint
    /// the demotion never proved. `None` until an announced-only
    /// demotion verifies one; a configured source always outranks it,
    /// and the next announced-only demotion re-proves and re-pins.
    /// Outside `shared`: request-path bookkeeping like `announced`.
    adopted: Mutex<Option<SocketAddr>>,
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
        let mut recorder = recorder::Recorder::new(config, peer.tick())?;
        // A `--state-file`-restored executor already carries the run's
        // state — the receipt log and the restored image are this run's
        // own record continuing, not new events to journal.
        recorder.observe_standing(peer.executor());
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
            command_persist: None,
            standby_source: None,
            announced: Mutex::new(None),
            adopted: Mutex::new(None),
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

    /// Installs the persist a `--state-file` run performs at a command's
    /// admission boundary: `POST /command` invokes it with the
    /// just-captured checkpoint after an accepted receipt lands in the
    /// executor's log and before the request answers — the admission is
    /// durable before its `200` receipt is promised, so a restart before
    /// the applying scan re-queues the carried `Accepted` receipt
    /// instead of losing the command with no audit trace. Only an
    /// `Accepted` admission invokes it — a refused command settles at
    /// submission and is already journaled. See [`CommandPersist`].
    pub fn with_command_persist(mut self, persist: CommandPersist) -> Self {
        self.command_persist = Some(persist);
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
    /// when set, else the pinned adoption a verified announced
    /// demotion recorded, else the monitor address a tracking peer
    /// announced through its `GET /checkpoint?peer=` pulls — an
    /// announce accepted only from the connection it names as its own
    /// address, with a wildcard-announced IP resolved to that address
    /// and a port-0 claim refused, so the recorded source is always
    /// one a demotion could dial. A recorded hint alone does not arm
    /// a demotion: the serving side cannot verify the puller's monitor
    /// port from the connection, so `POST /demote` toward an
    /// announced-only source first pulls one checkpoint from it and
    /// proceeds only when that checkpoint continues this run's line —
    /// adopting the source into the journal and pinning it as the
    /// tracking target, so a later `?peer=` rewrite cannot redirect
    /// the demoted peer's pulls — and otherwise refuses
    /// `NoTrackingSource`. The announced fallback is the follow-peer
    /// half of the tracking-source contract: a peer launched without
    /// a source — an active never told its peer — that is later
    /// demoted tracks its successor here and reconverges instead of
    /// stranding `unsynchronized` and unpromotable.
    pub fn tracking_source(&self) -> Option<SocketAddr> {
        self.driven
            .track
            .or(self.standby_source)
            .or_else(|| *self.adopted.lock().unwrap())
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
    /// Blocking: run this on a dedicated thread. One dispatcher drains
    /// tiny_http's internal queue and routes each request onto one of
    /// two lanes by [`submission`]: requests that can hold a worker on
    /// a client-paced wait — the body-reading `POST /command` and
    /// `POST /scan`, plus any request still carrying a body the client
    /// owes, whose dropped reader drains the rest the same way — go to
    /// the submission lane's [`SUBMIT_WORKERS`] workers; everything
    /// else to the serving lane's [`SERVE_WORKERS`]. The split exists
    /// because the body wait is the one serving step with no bound —
    /// a client that stalls mid-body holds its worker indefinitely,
    /// while every other wait is bounded: the serving pool's own
    /// network waits carry [`CHECKPOINT_PULL_TIMEOUT`]. Quarantining
    /// the unbounded wait keeps `GET /checkpoint`, `GET /role`, and
    /// every other endpoint answering through a stalled-body flood —
    /// the standby heartbeat measures the active's liveness, never its
    /// request-body traffic. The executor's command/scan interleaving
    /// stays deterministic either way: the pools only decide which
    /// request waits on the shared lock next, and scans, commands,
    /// checkpoints, and role changes still serialize on it.
    pub fn serve(&self) {
        let submissions = Lane::new();
        let served = Lane::new();
        std::thread::scope(|scope| {
            scope.spawn(|| {
                while let Ok(request) = self.server.recv() {
                    if submission(&request) {
                        submissions.push(request);
                    } else {
                        served.push(request);
                    }
                }
                // `recv` ending — `unblock` or a dead listener —
                // drains both lanes and releases their workers.
                submissions.close();
                served.close();
            });
            for _ in 0..SERVE_WORKERS {
                let lane = &served;
                scope.spawn(move || {
                    while let Some(request) = lane.pop() {
                        self.handle(request);
                    }
                });
            }
            for _ in 0..SUBMIT_WORKERS {
                let lane = &submissions;
                scope.spawn(move || {
                    while let Some(request) = lane.pop() {
                        self.handle(request);
                    }
                });
            }
        });
    }

    /// Stops a [`serve`](Self::serve) loop running on another thread.
    /// One `unblock` ends the dispatcher's `recv`; its lane close
    /// releases every worker once queued requests drain. Extra
    /// unblocks only pad the dead queue, so the original
    /// per-worker count stays as the harmless upper bound.
    pub fn shutdown(&self) {
        for _ in 0..SERVE_WORKERS {
            self.server.unblock();
        }
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
    pub fn paced_scan(&self) -> Tick {
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
    pub fn checkpoint(&self) -> Checkpoint {
        self.shared.lock().unwrap().peer.checkpoint()
    }

    /// Captures the run's checkpoint under the shared lock and hands it
    /// to `persist` still held — the serialization point every
    /// `--state-file` write goes through: a pacing loop's end-of-cycle
    /// write and a command's admission-boundary write
    /// ([`with_command_persist`](Self::with_command_persist)) order
    /// against each other at a scan boundary, so a persist slow on its
    /// own I/O can never overwrite a newer admission's file with an
    /// older capture.
    pub fn persist_state(
        &self,
        persist: impl FnOnce(&Checkpoint) -> Result<(), String>,
    ) -> Result<(), String> {
        let shared = self.shared.lock().unwrap();
        persist(&shared.peer.checkpoint())
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

    /// Starts field ownership on the bound peer — the launched active's
    /// startup half of the claim contract, run under the same lock that
    /// serializes scans. The caller runs it once, after the bind that
    /// proves the process can serve — a configured journal file
    /// replayed, the listener bound — and before the first scan: the
    /// claim [`Peer::activate`](dcs_runtime::Peer::activate) takes
    /// preempts unconditionally and outlives a dead holder, so it must
    /// be the run's last local startup step and its first shared-field
    /// side effect — a process that fails earlier leaves no stale claim
    /// fencing the field's standing owner. The refusal is the peer's
    /// own [`SwitchError`](dcs_core::SwitchError): a claim the field
    /// refuses fails the start with `FieldClaimFailed`, and a peer that
    /// is not a launched active with `NotActive`.
    pub fn activate(&self) -> Result<(), dcs_core::SwitchError> {
        let mut shared = self.shared.lock().unwrap();
        let Shared { peer, recorder } = &mut *shared;
        peer.activate()?;
        for change in peer.take_role_changes() {
            recorder.note_role_change(change.tick, change.from, change.to);
        }
        Ok(())
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
        for resolution in peer.take_resolutions() {
            recorder.note_resolution(resolution);
        }
        for restart in peer.take_source_restarts() {
            recorder.note_source_restart(restart);
        }
        for receipt in peer.take_superseded_commands() {
            recorder.note_settled(receipt, peer.tick());
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
        for resolution in peer.take_resolutions() {
            recorder.note_resolution(resolution);
        }
        for report in peer.take_reinitializations() {
            recorder.note_reinitialized(report);
        }
        for restart in peer.take_source_restarts() {
            recorder.note_source_restart(restart);
        }
        for receipt in peer.take_superseded_commands() {
            recorder.note_settled(receipt, peer.tick());
        }
        self.store.sync_receipts(peer.receipts());
        result
    }

    /// Journals a model-boundary crossing that ran before the monitor
    /// bound — the `--revised` state-file resume's lone-roll carryover
    /// — attributed, like a pulled crossing's entry, to the tick the
    /// run resumed at. Landing it after the bind keeps the durable
    /// record in process-lifetime order: the run-boundary marker first,
    /// the crossing's [`CarryoverReport`] behind it.
    pub fn note_reinitialized(&self, report: CarryoverReport) {
        self.shared
            .lock()
            .unwrap()
            .recorder
            .note_reinitialized(report);
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
        let remote = request.remote_addr().copied();
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
                // track if it is later demoted. The announce is the
                // puller's claim about itself, so it lands only from
                // the connection it claims — `checkpoint_peer` accepts
                // a `?peer=` naming the request's own source address
                // (resolving a wildcard-bound puller's `0.0.0.0` to it)
                // and ignores any other, so the read endpoint cannot
                // rewrite the demotion tracking source for an
                // unrelated client or record one that cannot be dialed.
                if let Some(announced) = checkpoint_peer(query, remote) {
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
                    &serve::resource_view(
                        &publication,
                        &self.signals,
                        &self.store.journal(0),
                        &self.store.routed_events(),
                    ),
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
                        // The journal diff keys on the receipt's
                        // absolute submission index — the bounded log's
                        // evictions shift positions, the index does not.
                        // `base + len` is `attempts` — the submission's
                        // own index is one below — and the subtraction
                        // saturates so a `capacity = 0` log that already
                        // evicted this receipt cannot underflow it.
                        let index =
                            (peer.receipt_base() + peer.receipts().len() as u64).saturating_sub(1);
                        let tick = peer.tick();
                        recorder.note_command(index, receipt.clone(), tick);
                        // An accepted admission is owed durability
                        // before its receipt answers: a `--state-file`
                        // run persists the just-captured checkpoint —
                        // receipt log included — at this boundary, so a
                        // restart before the applying scan re-queues the
                        // carried receipt rather than losing the command
                        // unaudited. The write rides the shared lock
                        // like the cycle-end persist, so the two never
                        // interleave into a stale overwrite.
                        if matches!(receipt.outcome, CommandOutcome::Accepted { .. })
                            && let Some(persist) = &self.command_persist
                        {
                            persist(&peer.checkpoint()).unwrap_or_else(|error| panic!("{error}"));
                        }
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
            // The paced refusal needs no body — answering it ahead of
            // the read keeps the wall clock's ownership obvious and
            // skips a read the response never used.
            (Method::Post, "/scan") if self.paced => json(409, SCAN_REFUSED_WHEN_PACED),
            (Method::Post, "/scan") => match read_json::<ScanRequest>(&mut request) {
                Ok(body) => {
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
                        //
                        // `track_cycle` runs the fetch outside the
                        // shared lock — a slow or unreachable tracking
                        // source stalls only this request's worker,
                        // never the lock the other endpoints queue on —
                        // and consumes the result under it, re-applying
                        // the owns-field gate.
                        if let Some(active) = self.tracking_source() {
                            let own = self.local_addr();
                            self.track_cycle(|| {
                                MonitorClient::with_timeout(active, CHECKPOINT_PULL_TIMEOUT)
                                    .checkpoint_announcing(own)
                                    .map_err(|error| format!("fetch from {active}: {error}"))
                            });
                        }
                        // Each scan takes the lock fresh and releases
                        // it at the boundary, so a request queued
                        // mid-batch — a command, a role poll — lands
                        // between scans instead of waiting the batch
                        // out.
                        let mut shared = self.shared.lock().unwrap();
                        scan_and_record(&mut shared, &self.store);
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
                        None => match self.store.latest() {
                            Some(publication) => json(200, &publication.snapshot),
                            None => json(503, "no publication yet"),
                        },
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
    /// the promoted run and settles at its next boundary, even when the
    /// serving checkpoint's tick is older than the standby's own (the
    /// driven cadence's resting shape) and its state cannot land. A
    /// failed pull leaves the standing convergence to decide, exactly
    /// as an unpulled promote would.
    ///
    /// A demotion toward an announced-only source — nothing configured,
    /// only a `?peer=` hint recorded — first verifies the hint the
    /// same way outside the lock: one checkpoint pull against it that
    /// must continue this run's line, or the demotion refuses
    /// `NoTrackingSource`. The verified adoption journals naming the
    /// source, ahead of the role change it enables.
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
        // The demotion-hint verification runs outside the shared lock
        // under the same bound and for the same reason: a hint naming
        // a dead or hostile endpoint must fail as that endpoint's own
        // network wait, never as the lock's hold. A demotion with a
        // configured source, a promotion, or a non-owner verifies
        // nothing.
        let verified = if promote {
            None
        } else {
            match self.verify_demote_hint() {
                Ok(hint) => hint,
                Err(response) => return response,
            }
        };
        let mut shared = self.shared.lock().unwrap();
        let Shared { peer, recorder } = &mut *shared;
        let result = if promote {
            if let Some(pulled) = pulled {
                peer.final_sync(|| pulled);
                for divergence in peer.take_divergences() {
                    recorder.note_divergence(divergence.tick, divergence.mismatches);
                }
                for resolution in peer.take_resolutions() {
                    recorder.note_resolution(resolution);
                }
                for report in peer.take_reinitializations() {
                    recorder.note_reinitialized(report);
                }
                for restart in peer.take_source_restarts() {
                    recorder.note_source_restart(restart);
                }
                for receipt in peer.take_superseded_commands() {
                    recorder.note_settled(receipt, peer.tick());
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
        } else if !promote && peer.owns_field() && self.configured_source().is_none() {
            // An announced-only demotion: the hint must still be the
            // one verified above — a re-announce that landed mid-verify
            // un-verifies the record, and the safe answer is refusal,
            // never a demotion toward an unproven endpoint. The
            // verified adoption journals naming its source ahead of
            // the role change it enables, so the run's move onto the
            // announced endpoint is never silent — and pins that
            // endpoint as the tracking target, so a `?peer=` rewrite
            // landing after the demotion cannot redirect the demoted
            // peer's pulls onto a source the demotion never proved.
            match (*self.announced.lock().unwrap(), verified) {
                (Some(current), Some(source)) if current == source => {
                    recorder.note_tracking_source(peer.tick(), source);
                    *self.adopted.lock().unwrap() = Some(source);
                    peer.demote()
                }
                _ => Err(SwitchError::NoTrackingSource),
            }
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

    /// The explicitly configured tracking source — `Driven`'s `track`
    /// or [`with_standby_source`](Self::with_standby_source) — when
    /// set: operator-declared, so a demotion follows it without
    /// proving anything. The announced follow-peer hint is not one of
    /// these, and never satisfies the demotion guard on its own.
    fn configured_source(&self) -> Option<SocketAddr> {
        self.driven.track.or(self.standby_source)
    }

    /// Proves the announced follow-peer hint a demotion would follow,
    /// or refuses the demotion — the `?peer=` hardening: the serving
    /// side cannot tell the puller's monitor port from any other
    /// same-IP port the connection claims, so a recorded hint is
    /// unverified until one checkpoint pulled from it proves it
    /// continues this run's line. Returns the verified hint, or `None`
    /// when the demotion needs no proof — a configured source covers
    /// it, or the peer owns no field — or the `409 NoTrackingSource`
    /// refusal when the owner has only an unproven hint: nothing
    /// announced, an unreachable hint, or a hint whose checkpoint is
    /// not this run's continuation.
    fn verify_demote_hint(&self) -> Result<Option<SocketAddr>, Response<Cursor<Vec<u8>>>> {
        if self.configured_source().is_some() {
            return Ok(None);
        }
        if !self.shared.lock().unwrap().peer.owns_field() {
            return Ok(None);
        }
        let hint = match *self.announced.lock().unwrap() {
            Some(hint) => hint,
            None => return Err(json(409, &SwitchError::NoTrackingSource)),
        };
        let own = self.shared.lock().unwrap().peer.checkpoint();
        let pulled = match MonitorClient::with_timeout(hint, CHECKPOINT_PULL_TIMEOUT).checkpoint() {
            Ok(pulled) => pulled,
            Err(_) => return Err(json(409, &SwitchError::NoTrackingSource)),
        };
        if verify_announced_checkpoint(&pulled, &own).is_err() {
            return Err(json(409, &SwitchError::NoTrackingSource));
        }
        Ok(Some(hint))
    }
}

/// Whether the request can hold a worker on a client-paced wait —
/// the routing [`Monitor::serve`] applies. Two request shapes can:
///
/// - `POST /command` and `POST /scan`, the only handlers that read a
///   request body: tiny_http hands them the body still on the socket,
///   and a client that stalls mid-body holds the reader as long as it
///   cares to.
/// - Any request still owing the client body bytes, whatever its
///   path: a declared `Content-Length` past the library's small eager
///   buffer leaves the remainder on a live reader whose drop drains
///   the rest — the same unbounded wait as the read — and a chunked
///   body reads off the socket the same way.
///
/// Everything else — plain GETs and the bounded control-plane POSTs,
/// including a `GET /role` whose client never promised a body — is
/// answered without ever waiting on the client and belongs on the
/// serving lane.
fn submission(request: &Request) -> bool {
    let reads_body = request.method() == &Method::Post
        && matches!(
            request.url().split('?').next(),
            Some("/command") | Some("/scan")
        );
    reads_body
        || request.body_length().is_some_and(|length| length > 0)
        || request
            .headers()
            .iter()
            .any(|header| header.field.equiv("Transfer-Encoding"))
}

/// One serving lane's request queue — [`Monitor::serve`]'s dispatcher
/// pushes, the lane's workers pop. [`close`](Self::close) releases
/// every blocked worker once the queued requests drain, so `shutdown`
/// reaching the dispatcher propagates down both lanes.
struct Lane {
    inner: Mutex<LaneInner>,
    ready: Condvar,
}

struct LaneInner {
    queue: VecDeque<Request>,
    closed: bool,
}

impl Lane {
    fn new() -> Self {
        Self {
            inner: Mutex::new(LaneInner {
                queue: VecDeque::new(),
                closed: false,
            }),
            ready: Condvar::new(),
        }
    }

    fn push(&self, request: Request) {
        let mut inner = self.inner.lock().unwrap();
        if !inner.closed {
            inner.queue.push_back(request);
            self.ready.notify_one();
        }
    }

    fn pop(&self) -> Option<Request> {
        let mut inner = self.inner.lock().unwrap();
        loop {
            if let Some(request) = inner.queue.pop_front() {
                return Some(request);
            }
            if inner.closed {
                return None;
            }
            inner = self.ready.wait(inner).unwrap();
        }
    }

    fn close(&self) {
        self.inner.lock().unwrap().closed = true;
        self.ready.notify_all();
    }
}

/// One standby tracking cycle plus its journal recording — the body
/// `track_cycle` runs, and `POST /scan` reaches through it:
/// `Peer::track_once` runs the
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
    for resolution in peer.take_resolutions() {
        recorder.note_resolution(resolution);
    }
    for report in peer.take_reinitializations() {
        recorder.note_reinitialized(report);
    }
    for restart in peer.take_source_restarts() {
        recorder.note_source_restart(restart);
    }
    for change in peer.take_role_changes() {
        recorder.note_role_change(change.tick, change.from, change.to);
    }
    // Pending commands an adopted checkpoint abandoned — the demoted
    // run's suspended queue the tracked line never carried — settle
    // `superseded` here rather than vanishing from the audit.
    for receipt in peer.take_superseded_commands() {
        recorder.note_settled(receipt, peer.tick());
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
fn scan_and_record(shared: &mut Shared<'_>, store: &Store) -> Tick {
    let Shared { peer, recorder } = shared;
    let tick = peer.scan();
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
    tick
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
/// demotion — validated against `remote`, the request's source
/// address. The announce is the puller's claim about itself, so it is
/// accepted only when its IP is the connection's source IP; a wildcard
/// announced IP — a `0.0.0.0`-bound puller announcing "my port on
/// every interface" — resolves to the source the connection proves.
/// Any other value is a client claiming an address that is not its
/// own and announces nothing, as do an absent or unparseable `peer`,
/// a request whose source cannot be read, and a port-0 claim: no
/// monitor listens on port 0, so installing the resolved
/// `<remote>:0` would record a source no demotion could ever pull —
/// the same stranding the wildcard address caused. The checkpoint
/// itself is still served in every case, keeping older pullers and
/// plain `GET /checkpoint` readers compatible.
fn checkpoint_peer(query: &str, remote: Option<SocketAddr>) -> Option<SocketAddr> {
    let announced = query_pairs(query).find_map(|(key, value)| {
        if key == "peer" {
            value.parse::<SocketAddr>().ok()
        } else {
            None
        }
    })?;
    if announced.port() == 0 {
        return None;
    }
    let remote = remote?;
    if announced.ip() == remote.ip() {
        Some(announced)
    } else if announced.ip().is_unspecified() {
        Some(SocketAddr::new(remote.ip(), announced.port()))
    } else {
        None
    }
}

/// Why a checkpoint pulled to verify an announced demotion hint is
/// not this run's continuation — [`verify_announced_checkpoint`]'s
/// named refusals. Each means the announced endpoint serves a stream
/// this run's tracked line does not produce, so the demotion the
/// hint would have armed refuses `NoTrackingSource` rather than
/// moving the run onto it.
#[derive(Debug, Clone, PartialEq)]
enum AnnouncedCheckpointError {
    /// The pulled document's format version is not one this build
    /// reads — not a checkpoint of this line at all.
    UnreadableVersion {
        /// The version the pulled document declares.
        found: u32,
    },
    /// The pulled checkpoint names a different tick-domain
    /// generation — a restarted or unrelated stream, not the line
    /// this run's checkpoints feed. A tracking peer adopts this run's
    /// generation verbatim on every apply — and a reinitialized
    /// successor keeps it across a model revision's fingerprint
    /// crossing — so any inequality, including an identified stream
    /// against this run's unidentified one, is not the tracked
    /// continuation.
    ForeignGeneration,
    /// The pulled checkpoint's tick runs more than
    /// [`MAX_ANNOUNCED_AHEAD`] past this run's own: a successor
    /// tracking this run sits only a few ticks ahead of it, so a
    /// same-generation stream that far ahead is not this line's
    /// continuation — it is a forged or foreign tick domain wearing
    /// this line's identity.
    Ahead {
        /// The tick the pulled checkpoint claims.
        pulled: Tick,
        /// This run's own tick when the hint was verified.
        own: Tick,
    },
}

/// Whether one checkpoint pulled from an announced demotion hint
/// proves the hinted endpoint serves this run's continuation — the
/// demote-side half of the `?peer=` hardening. The serving side
/// cannot tell the puller's monitor port from any other same-IP port
/// a connection claims, so the recorded hint is trusted only after
/// the endpoint's own checkpoint answers as this line's successor
/// would: a readable format, this run's generation — the line's
/// tick-domain identity, which a tracking peer adopts verbatim on
/// every apply and a reinitialized successor keeps across the
/// fingerprint crossing — and a tick no further ahead of this run's
/// than [`MAX_ANNOUNCED_AHEAD`], the honest skew of a peer applying
/// this run's checkpoints and scanning alongside it. The model
/// fingerprint is deliberately absent here: a revised successor's
/// checkpoints legitimately carry the next model's — the demoted
/// peer's own apply gate refuses a foreign fingerprint at adoption
/// and reports `degraded`, the revision roll's designed shape, so a
/// fingerprint inequality is not a demote-side refusal. A checkpoint
/// behind this run's tick is no rejection either: a lagging
/// successor is still this line, and the demoted peer's pulls simply
/// reconverge it.
fn verify_announced_checkpoint(
    pulled: &Checkpoint,
    own: &Checkpoint,
) -> Result<(), AnnouncedCheckpointError> {
    if !SUPPORTED_FORMAT_VERSIONS.contains(&pulled.format_version) {
        return Err(AnnouncedCheckpointError::UnreadableVersion {
            found: pulled.format_version,
        });
    }
    if pulled.generation != own.generation {
        return Err(AnnouncedCheckpointError::ForeignGeneration);
    }
    if pulled.tick.0 > own.tick.0.saturating_add(MAX_ANNOUNCED_AHEAD) {
        return Err(AnnouncedCheckpointError::Ahead {
            pulled: pulled.tick,
            own: own.tick,
        });
    }
    Ok(())
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

/// Reads a request body up to [`MAX_REQUEST_BODY`]: a declared length
/// past the bound is refused `413` before a byte is read, and the read
/// itself is `take`-bounded so a chunked or understated body cannot
/// deliver more either — no worker ever buffers or waits on a body
/// past the cap. Read failures produce the `400` response directly.
fn read_body(request: &mut Request) -> Result<Vec<u8>, Response<Cursor<Vec<u8>>>> {
    if request
        .body_length()
        .is_some_and(|length| length as u64 > MAX_REQUEST_BODY)
    {
        return Err(json(413, "request body too large"));
    }
    let mut body = Vec::new();
    match request
        .as_reader()
        .take(MAX_REQUEST_BODY + 1)
        .read_to_end(&mut body)
    {
        Err(_) => Err(json(400, "unreadable request body")),
        Ok(_) if body.len() as u64 > MAX_REQUEST_BODY => Err(json(413, "request body too large")),
        Ok(_) => Ok(body),
    }
}

/// Reads and parses a JSON request body within [`read_body`]'s bound;
/// parse failures produce the `400` response directly.
fn read_json<T: DeserializeOwned>(request: &mut Request) -> Result<T, Response<Cursor<Vec<u8>>>> {
    let body = read_body(request)?;
    serde_json::from_slice(&body).map_err(|error| json(400, &error.to_string()))
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
    let body = read_body(request)?;
    let value: serde_json::Value = match serde_json::from_slice(&body) {
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

    /// `GET /receipts`: the executor's receipt log — the bounded tail
    /// of retained receipts, pending ones included.
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
    /// monitor records as its tracking source for a later demotion —
    /// landing only because it names the pulling connection's own
    /// source address.
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
    /// `seq` above `since` (`0` fetches everything retained) — the
    /// pinned `run_boundary` markers included, ahead of the bounded
    /// tail.
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

    /// The `?peer=` announce contract behind the
    /// `checkpoint-announced-peer-unroutable-wildcard` finding: the
    /// recorded tracking source is always a dialable address — the
    /// puller's own claim when the connection proves it, or the
    /// wildcard resolved to that proven source — and an unroutable or
    /// foreign announce never lands at all.
    #[test]
    fn checkpoint_peer_records_only_a_dialable_self_claim() {
        let remote: SocketAddr = "172.22.0.4:51000".parse().unwrap();
        // The wildcard announce a `0.0.0.0`-bound puller sends —
        // unroutable verbatim — resolves to the connection's proven
        // source with the puller's announced monitor port.
        assert_eq!(
            checkpoint_peer("peer=0.0.0.0:8081", Some(remote)),
            Some("172.22.0.4:8081".parse().unwrap())
        );
        assert_eq!(
            checkpoint_peer("peer=[::]:8081", Some(remote)),
            Some("172.22.0.4:8081".parse().unwrap())
        );
        // A claim naming the connection's own address lands verbatim.
        assert_eq!(
            checkpoint_peer("peer=172.22.0.4:8081", Some(remote)),
            Some("172.22.0.4:8081".parse().unwrap())
        );
        // A foreign address announces nothing — the read endpoint
        // cannot plant a tracking source for an unrelated client.
        assert_eq!(checkpoint_peer("peer=10.9.9.9:8081", Some(remote)), None);
        // Nothing parseable, absent, or sourceless lands either.
        assert_eq!(checkpoint_peer("", Some(remote)), None);
        assert_eq!(checkpoint_peer("peer=nonsense", Some(remote)), None);
        assert_eq!(checkpoint_peer("peer=host:8081", Some(remote)), None);
        assert_eq!(checkpoint_peer("peer=0.0.0.0:8081", None), None);
        // A port-0 announce can never name a listening monitor:
        // accepting it would install an undialable `<remote>:0`
        // tracking source — the same strand the wildcard caused.
        assert_eq!(checkpoint_peer("peer=0.0.0.0:0", Some(remote)), None);
        assert_eq!(checkpoint_peer("peer=172.22.0.4:0", Some(remote)), None);
    }

    /// The demote-side half of the `?peer=` hardening at the document
    /// level: the checkpoint pulled to prove an announced hint must be
    /// this line's continuation — readable format, this run's model
    /// fingerprint and generation, and a tick within the honest
    /// successor skew — or the hint proves nothing and the demotion
    /// refuses.
    #[test]
    fn verify_announced_checkpoint_accepts_only_this_lines_continuation() {
        let checkpoint = || Checkpoint {
            format_version: dcs_runtime::CHECKPOINT_FORMAT_VERSION,
            model_fingerprint: Some(dcs_core::ModelFingerprint(7)),
            generation: Some(11),
            tick: Tick(100),
            components: Default::default(),
            driver: None,
            outputs: Default::default(),
            internal: Default::default(),
            forces: Default::default(),
            receipts: Vec::new(),
            command_admission: Default::default(),
        };
        let own = checkpoint();

        // The honest successor shapes: this line at the same tick, a
        // few ticks ahead inside the skew window, and far behind — a
        // lagging successor is still this line.
        for ahead in [0, 1, MAX_ANNOUNCED_AHEAD] {
            let mut pulled = checkpoint();
            pulled.tick = Tick(own.tick.0 + ahead);
            assert_eq!(verify_announced_checkpoint(&pulled, &own), Ok(()));
        }
        let mut lagging = checkpoint();
        lagging.tick = Tick(3);
        assert_eq!(verify_announced_checkpoint(&lagging, &own), Ok(()));
        // The revision roll's successor: the next model's fingerprint
        // on this line's generation — the demoted peer's own apply
        // gate answers the foreign fingerprint at adoption, so the
        // demote-side check lets the crossing through.
        let mut revised = checkpoint();
        revised.model_fingerprint = Some(dcs_core::ModelFingerprint(8));
        assert_eq!(verify_announced_checkpoint(&revised, &own), Ok(()));
        // An unidentified line — both unminted — still verifies on
        // generation and tick: the unminted test/legacy shape.
        let mut unminted_own = checkpoint();
        unminted_own.model_fingerprint = None;
        unminted_own.generation = None;
        let mut pulled = unminted_own.clone();
        pulled.tick = Tick(101);
        assert_eq!(verify_announced_checkpoint(&pulled, &unminted_own), Ok(()));

        // The reproduction's forgery: this line's identity at a tick
        // far ahead of the run's — refused.
        let mut forged = checkpoint();
        forged.tick = Tick(99999);
        assert_eq!(
            verify_announced_checkpoint(&forged, &own),
            Err(AnnouncedCheckpointError::Ahead {
                pulled: Tick(99999),
                own: Tick(100),
            })
        );
        let mut just_past = checkpoint();
        just_past.tick = Tick(own.tick.0 + MAX_ANNOUNCED_AHEAD + 1);
        assert!(matches!(
            verify_announced_checkpoint(&just_past, &own),
            Err(AnnouncedCheckpointError::Ahead { .. })
        ));
        // A foreign generation — a restarted or unrelated stream, and
        // an identified stream against this run's unidentified one —
        // and an unreadable format are refusals too.
        let mut restarted = checkpoint();
        restarted.generation = Some(12);
        assert_eq!(
            verify_announced_checkpoint(&restarted, &own),
            Err(AnnouncedCheckpointError::ForeignGeneration)
        );
        let mut identified = checkpoint();
        identified.model_fingerprint = None;
        identified.generation = Some(11);
        assert_eq!(
            verify_announced_checkpoint(&identified, &unminted_own),
            Err(AnnouncedCheckpointError::ForeignGeneration)
        );
        let mut unreadable = checkpoint();
        unreadable.format_version = 999;
        assert_eq!(
            verify_announced_checkpoint(&unreadable, &own),
            Err(AnnouncedCheckpointError::UnreadableVersion { found: 999 })
        );
    }
}
