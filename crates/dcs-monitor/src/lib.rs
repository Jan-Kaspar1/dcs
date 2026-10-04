//! HTTP+JSON monitoring access to a running executor.
//!
//! [`Monitor`] exposes a [`dcs_runtime::Executor`] over `tiny_http` — a
//! small synchronous HTTP server, so no async runtime is involved.
//! Requests dispatch across five worker lanes: a command lane for
//! `POST /command` — one worker draining a queue deep enough to hold
//! the pipelined wave the bounded-ingress contract must answer, so
//! every submission takes its receipted settlement or the named
//! `queue_full` rejection in submission order — a submission lane for
//! the other requests that can hold a worker on a client-paced wait —
//! `POST /scan`, plus any request still carrying a body the client
//! owes (dropping its live reader drains the remainder, the same
//! unbounded wait) — a heartbeat lane for the pair-liveness reads,
//! `GET /health`, `GET /checkpoint`, and `GET /role`, a control lane
//! for the switchover POSTs `POST /promote` and `POST /demote` — bare
//! or carrying the small attributed body, which arrives already
//! buffered under tiny_http's eager-read bound, so a client-paced
//! wait can never reach it; a switch request still owing the client
//! body bytes takes the submission lane's quarantine instead — and a
//! serving lane for everything else. A client that stalls mid-body
//! pins at most the command worker or the small submission pool, so
//! request-body traffic can never impersonate a dead active. The
//! serving lane still holds one client-paced wait the body quarantine
//! cannot reach: the response write itself — tiny_http exposes no
//! socket timeout, so a client that never reads a large response pins
//! its worker in `respond` for as long as the connection stays open,
//! and enough wedged connections pin every serving worker. The
//! heartbeat lane is the liveness half of the quarantine for that
//! wait: the standby's checkpoint pulls and the pair view's role
//! reads answer from it even while every serving worker sits blocked
//! mid-write to a dead consumer, so a wedged read-side connection can
//! never impersonate a dead active either. The control lane is the
//! actuation half: the switchover endpoints answer from their own
//! small pool, so an operator's promote or demote during an incident —
//! exactly when consoles wedge — never queues silently behind four
//! dead connections, the starvation the serving lane's bulk reads
//! remain exposed to by design.
//! Within a lane a request whose handling legitimately waits on the
//! network — a driven `POST /scan` batch's per-scan checkpoint pull, a
//! promotion's final-sync fetch — stalls only its own worker instead
//! of head-of-line blocking every endpoint behind it, and body reads
//! themselves are bounded: a declared or delivered body past
//! [`MAX_REQUEST_BODY`] is refused `413`. Lane queues are bounded too
//! ([`LANE_QUEUE_DEPTH`], the command lane's [`command_lane_depth`]):
//! a request arriving while its lane's queue is full falls to the
//! refuse worker's `503` instead of queueing without limit, while a
//! `POST /command` past its lane's bound defers to the command lane's
//! own bounded overflow deck — the admission contract owes it a
//! receipted answer, and the deck's feeder hands it back to the one
//! command worker in dispatch order — so a request flood pinned
//! behind wedged workers cannot grow memory or scramble the receipt
//! log's submission order. The executor lives behind a
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
//! beyond the scan and the publication swap. The small liveness reads
//! follow the same rule rather than the lock: the control plane
//! refreshes the store's published liveness mirror — `GET /role`'s
//! report and the role, tick, and last-scan stamp `GET /health`
//! derives — wherever either changes under the executor lock, so a
//! scan wedged in field I/O cannot stall the very answer that exists
//! to expose the wedge.
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
//!   seen sequence — an evicted stretch surfaces as a numbering gap.
//!   History `seq`s ride the run's tick domain, so a restart that
//!   continues the domain (a checkpoint-adopted standby, a
//!   `--state-file` resume) keeps numbering across the seam and the
//!   unserved stretch reads as the gap it is; each served envelope also
//!   stamps the run's lifetime ordinal (`run` — the same counter the
//!   journal's `run_boundary` markers carry), so a restart beginning a
//!   new tick domain is detectable on the envelope itself even while
//!   the cursor still filters every restarted sample out
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
//!   knows where to track if it is later demoted. The announced
//!   follow-peer contract is keyed-only — this endpoint is public, so
//!   an unkeyed run can prove nothing about an announced endpoint and
//!   an announced-only demotion refuses `no_tracking_source` there.
//!   A `?prove=<nonce>`
//!   on a keyed monitor — one launched under the pair's `--pair-token`
//!   — gets the served document's keyed `line_proof` stamped over it:
//!   the digest only a peer holding the token produces, which the
//!   announced-source demotion's verify pull and the adopted source's
//!   standing pulls then demand, so an endpoint that merely replays
//!   or fabricates this line's checkpoints can neither arm a demotion
//!   nor feed the demoted peer
//! - `GET /role` → `200` [`RoleReport`] — the instance's reported role
//!   in a redundant pair (`active`, `standby`, or a transition state)
//!   plus the standby's convergence, the field's claim observation, and
//!   the armed failover gate's evidence — the pair-as-one-controller
//!   contract of the monitoring-under-redundancy decision. The report
//!   is the store's published liveness mirror — refreshed wherever the
//!   control-plane lock changes it — so the role poll cannot queue
//!   behind a scan wedged in field I/O
//! - `GET /health` → `200` [`HealthReport`] — the bounded liveness
//!   answer the published image's `HEALTHCHECK` probes (the
//!   container-health contract): the listener's own live declaration,
//!   the instance's reported role, and the wall-clock age of the last
//!   completed scan. It takes the heartbeat lane like `/checkpoint`
//!   and `/role`, and answers from the published liveness mirror
//!   rather than the executor lock, so the probe still answers while
//!   wedged consumers starve the bulk reads and while a wedged scan
//!   holds the lock the liveness fetch once waited on
//! - `POST /promote`, `POST /demote` → `200` [`RoleReport`] — the
//!   switchover actions of the switchover-semantics decision: promotion
//!   lifts the standby's write gate at the request's scan boundary,
//!   demotion re-closes the active's; a refusal — a standby that has not
//!   converged, or a repeated promotion — answers `409` with the named
//!   [`SwitchError`]. Either accepts the attributed body
//!   [`SwitchRequest`] — `{"actor":…}`, the same declared-identity
//!   convention the command envelope records — beside the still-accepted
//!   bare request; the journaled `RoleChanged` entries the switch
//!   produces carry the actor it declared, and `origin` marks a
//!   requested switch apart from the peer's failover self-promotion
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
//!   metrics ride the snapshot's `command_queue` section. With
//!   `state_file` configured on [`MonitorConfig`] (the controller's
//!   `--state-file` wiring), an accepted admission's checkpoint is
//!   captured and queued to the sink under the admission's own lock
//!   hold, then the `200` attests the file caught up through that
//!   capture before answering — so a restart between admission and the
//!   applying scan re-queues the carried receipt rather than losing
//!   the command unaudited
//! - `POST /scan`, body [`ScanRequest`] → runs that many scans → `200`
//!   [`TelemetrySnapshot`] taken after the last one; a failing
//!   [`Driven::after_scan`] hook → `500`; refused with `409` on a paced
//!   monitor before the body is even read (see below), `400` for a
//!   `scans` past the per-request bound [`MAX_SCANS_PER_REQUEST`] —
//!   every accepted batch is a bounded, terminating unit of work —
//!   and the same `413` bound [`MAX_REQUEST_BODY`] gives `/command`
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
//! state instead of ever backpressuring execution. History samples draw
//! their `seq` from the producing scan's tick rather than a per-process
//! append count, so a restart that continues the tick domain keeps the
//! axis — the unserved stretch reads as the numbering gap it is — and
//! every served [`PointHistory`] stamps the run's lifetime ordinal, so
//! a restart beginning a new tick domain is detectable even while a
//! `since` cursor still filters its samples out. The store's
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
//! so `GET /journal` answers continuously across a restart. The
//! append itself drains on a dedicated writer off the executor lock —
//! the journal-append isolation decision (#942, adopting unmanaged
//! finding #546): the recording point hands each record to a bounded
//! queue through a non-blocking send, so a slow or stalled sink can
//! neither lengthen a scan nor pin the lock; the queue's declared
//! bound (`journal_drain_capacity`) is where a lagging sink turns the
//! fatal-on-append-failure rule into a refused push, the writer's
//! FIFO keeps the file's `seq` order, and the drain's standing
//! health — `healthy`/`lagging`/`failed` with the lost-record
//! accounting — is the named degraded state stamped into every
//! published snapshot's `publication.journal_sink` section. A
//! mutating request's answer, and every `GET /journal`, waits the
//! writer's queue out first — the request worker's own bounded wait,
//! never the lock's — so the answer attests the durable record caught
//! up through the request's effects. Those
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
//! Point history stays volatile; only the journal persists — though the
//! file's run count is the lifetime ordinal the volatile history's
//! served envelopes stamp, so a restarted seq axis is attributable on
//! the history read too. The file is
//! single-writer: the bind holds an exclusive advisory lock on the path
//! for the monitor's lifetime, so two monitors configured with the same
//! `journal_file` cannot interleave duplicate `seq`s into one
//! un-replayable record — the second bind fails naming the file and the
//! live-holder conflict, and a dead holder's lock releases with its
//! descriptor so a restart re-acquires it.
//!
//! With `state_file` set on [`MonitorConfig`], the run's transferable
//! [`Checkpoint`] persists to that path at its documented boundaries —
//! every completed scan cycle's end (after the plant step) and every
//! accepted command's admission — the recovery state a restarted
//! controller resumes through. The checkpoint file shares the journal
//! sink's drain shape (the state-file persist isolation fix, #982):
//! the capture rides the shared executor lock exactly where the write
//! always did, then hands to a bounded [`Drain`] whose dedicated
//! writer serializes and atomically replaces the file — so a slow or
//! stalled disk can neither lengthen a scan nor pin the serving lane
//! behind it. The queue's declared bound (`state_drain_capacity`) is
//! where a lagging sink turns fatal at the push, the writer's FIFO on
//! the lock-serialized captures is what makes a stale overwrite
//! impossible, and the drain's standing health —
//! `healthy`/`lagging`/`failed` with the lost-checkpoint accounting —
//! is the named degraded state stamped into every published snapshot's
//! `publication.state_sink` section. The answers whose contract
//! promises durability — an accepted command's `200`, a driven scan
//! batch's — wait the writer's queue out through the request's own
//! pushed ordinal first, the request worker's own bounded wait, never
//! the lock's; every other path — the paced loop's cycle-end persist
//! included — only queues, never waits.
//!
//! The checkpoint is one run's state, so its file is single-writer
//! like the two append sinks: a [`StateWriterLock`] holds an exclusive
//! advisory lock on a `.lock` sidecar beside the path — a sibling the
//! write-then-rename never replaces, which is why a lock taken on the
//! checkpoint itself would detach from the path at the first save.
//! A second live writer on one state file is refused where the sink
//! opens, naming the checkpoint, the sidecar, and the live-holder
//! conflict, instead of leaving last-writer-wins for a restart to
//! discover as a foreign run's tick domain, receipts, and component
//! state adopted under a matching model fingerprint. A caller that
//! holds the claim itself — the controller takes it before it reads
//! the checkpoint to resume — hands it in through `state_writer` so
//! the resume and every later save are one guard. A dead holder's lock
//! releases with its descriptor, so the restart that resumes the dead
//! run's checkpoint re-acquires it.
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
//! returned receipt. Every request the page issues rides a shared
//! abort deadline, so a hung connection degrades into the named feed
//! state instead of pinning the view on last-known values. The page
//! also reports its own delivery honesty —
//! the consumer-side gap/freshness state the bounded-publication
//! decision requires: a `since`-cursor read stepping over an evicted
//! stretch marks the feed line "publication gap", a snapshot re-serving
//! the same publication's seq and tick marks it "stale publication",
//! a regressed publication identity, a new journal `run_boundary`, or a
//! changed history `run` mark names the source's restart and resets the
//! cursors to re-read the new lifetime's streams, each rendered beside
//! the view — distinct from a peer's unreachable
//! redundancy fault and from a point's non-Good quality — and cleared
//! on the next in-sequence, fresh publication. Command and force value
//! entries parse strictly against the declared kind — a Boolean accepts
//! only the named tokens — so arbitrary text is refused client-side
//! rather than reaching the wire coerced to `false`. The snapshot's
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
//! consumed on its rising edge.
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
//! paced run. The driven contract's other half is that a request
//! terminates: `scans` is bounded by [`MAX_SCANS_PER_REQUEST`], so a
//! batch is a bounded unit of work inside its request — never an
//! uncancellable run that outlives the client asking for it, and a
//! submission worker's pin on any one request stays bounded too.
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
mod drain;
mod history_file;
mod journal_file;
mod pair;
mod recorder;
mod serve;
mod state_file;
mod store;

use crate::store::Store;
pub use history_file::{HistoryBoundary, HistoryData, read_history_file};
pub use journal_file::{JournalData, RunBoundary, read_journal_file};
pub use pair::{
    CONVERGENCE_GRACE, PAIR_FAULT_KINDS_VERSION, PairClient, PairError, PairFaultKind, PairHealth,
    PeerStatus, PeerView,
};
pub use recorder::MonitorConfig;
pub use state_file::{DEFAULT_STATE_DRAIN_CAPACITY, StateSink, StateWriterLock};
pub use store::{Publication, PublicationGap, PublicationPage};

use dcs_core::{
    CarryoverReport, Command, CommandError, CommandOutcome, CommandReceipt, DurableEntry,
    HistorySinkHealth, JournalEntry, JournalSinkHealth, PointHistory, PointId, PublicationHealth,
    ResourceView, RestartConsultOutcome, Role, RoleReport, SchemaView, StandbySync,
    StateSinkHealth, SwitchError, TelemetrySnapshot, Tick,
};
use dcs_model::SignalIndex;
use dcs_runtime::{
    ApplyError, Checkpoint, Executor, Peer, PeerEvent, SUPPORTED_FORMAT_VERSIONS, TrackReport,
    Transfer, mint_generation,
};
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use sha2::{Digest, Sha256};
use std::collections::{BTreeSet, VecDeque};
use std::io::{self, Cursor, Read, Write};
use std::net::{IpAddr, SocketAddr, TcpStream, ToSocketAddrs};
use std::sync::mpsc;
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};
use tiny_http::{Header, Method, Request, Response, Server};

/// Request body of `POST /scan`: how many scans the executor should run.
/// The request is one bounded unit of work — a `scans` past the
/// monitor's declared per-request bound is refused rather than
/// starting a batch the run cannot terminate.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct ScanRequest {
    /// The number of scans to run — at most the monitor's declared
    /// per-request bound, or the request is refused.
    pub scans: u64,
}

/// The attributed `POST /command` envelope — the wire shape recorded
/// for the command-path audit-identity decision:
/// `{"command": <Command>, "actor": "<identity>", "reason": "<why>"}`
/// submitted beside the still-accepted bare [`Command`]. `actor` is the
/// submitter's *declared* identity — attestation, not authentication —
/// and `reason` the declared justification the shelving-reason decision
/// carries on the same envelope; both ride the [`CommandReceipt`] and
/// so the journaled `CommandSettled` entry, `reason` independently of
/// `actor`, and a deployment fronting the monitor with an
/// authenticating proxy fills `actor` from verified context. Strict
/// fields: an envelope carrying neither key's expected shape is a
/// `400`, so a stray top-level `actor` or `reason` beside a bare
/// command is refused rather than silently dropped.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct CommandEnvelope {
    /// The command to submit.
    command: Command,
    /// The submitter's declared actor identity; absent submits
    /// unattributed.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    actor: Option<String>,
    /// The declared reason the submission carries; absent submits
    /// reasonless — refused at admission only when the target point's
    /// declaration marks the command reason-carrying.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    reason: Option<String>,
}

/// Request body of `POST /promote` and `POST /demote`: the attributed
/// switch shape — `{"actor": "<identity>"}` — the command-path
/// audit-identity convention extended to the switch endpoints, submitted
/// beside the still-accepted bare request (an empty body, the
/// pre-attribution shape). `actor` is the requester's *declared*
/// identity — attestation, not authentication, exactly as on
/// [`CommandReceipt`]'s field — carried onto the journaled
/// `RoleChanged` entries the switch produces; an absent actor journals
/// unattributed, never a rejection. The `origin` those entries carry is
/// the peer's own marking — `request` here, `failover` for its
/// self-promotion — not a field a requester can declare. Strict fields:
/// an envelope key the shape does not declare is a `400` rather than a
/// silently dropped attribution.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SwitchRequest {
    /// The requester's declared actor identity; absent switches journal
    /// unattributed.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub actor: Option<String>,
}

/// The `GET /health` answer — the bounded liveness report the published
/// image's `HEALTHCHECK` probes (the container-health contract): the
/// listener's own liveness declaration, the instance's reported role,
/// and the run's scan freshness. Bounded like the heartbeat lane's
/// other reads — one fetch of the store's published liveness mirror, a
/// fixed small body, no network wait and no executor lock — so the
/// probe answers while wedged consumers starve the bulk reads and
/// while a scan wedged in field I/O holds the lock this fetch once
/// took: the growing `last_scan_age_ms` is the wedge's report.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HealthReport {
    /// The process liveness declaration: `true` by construction — an
    /// answer at all is the proof, and the field is the contract a
    /// probe decodes rather than a port a connect alone attests.
    pub live: bool,
    /// The instance's reported redundancy role — the same verdict
    /// `GET /role` serves: `active`, `standby`, or a transition state.
    pub role: Role,
    /// The run's current tick — the same domain `GET /role` reports.
    pub tick: Tick,
    /// Wall-clock milliseconds since the last completed scan — the
    /// freshness `live` alone cannot attest: a wedged scan loop reads
    /// as a growing age, a run that has not scanned yet as `null`.
    pub last_scan_age_ms: Option<u64>,
    /// The build this process is: its crate version and, where the
    /// build could supply one, the git revision it was compiled from.
    ///
    /// Identity is the first half of what an operator must be able to
    /// read off a field run — "which build is talking to my rig, and
    /// which model is it running" — and it is here rather than on the
    /// bulk snapshot because the answer is fixed for the process's
    /// life: it must still read while the bulk reads starve.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub build: Option<BuildIdentity>,
}

/// The build identity a monitoring endpoint reports: the crate version
/// and the git revision the binary was compiled from, where the build
/// could supply one.
///
/// `git_sha` is absent rather than invented when the build did not carry
/// one — a `vergen`-style revision embedded at compile time, or the
/// controller's own `--build-sha` arm for a container whose binary
/// came from a prebuilt image. An absent revision reports as absent, so
/// a consumer can tell "not recorded" from "recorded, and here it is".
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BuildIdentity {
    /// The crate version this process was compiled from.
    pub version: String,
    /// The git revision this process was compiled from, when recorded.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub git_sha: Option<String>,
    /// The model fingerprint the served plant model carries, when the
    /// process recorded one — the same value `/checkpoint` stamps, so an
    /// operator reads the served bytes' identity beside the build's.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub model_fingerprint: Option<String>,
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

/// How long an outstanding fetch may run before the puller treats its
/// worker as unable to answer and rebinds a fresh one — several pull
/// bounds, so a fetch that is merely slow (a large document over a slow
/// link, every one of its reads inside its own bound) keeps the worker
/// it is making progress on, while a fetch wedged on the one wait no
/// bound covers — a configured name whose resolution never answers —
/// is replaced instead of stranding the pull path for the life of the
/// process.
///
/// The reported [`PullMiss::Stalled`] stage names the stall from the
/// first bound out; this is the point at which the pull path stops
/// waiting on it and moves.
const CHECKPOINT_PULL_STALL: Duration = Duration::from_secs(4);

/// How many wedged fetch workers one puller may retire before it
/// reports the stall without rebinding again. A retired worker holds
/// its thread until the fetch it is stuck in resolves, so the
/// replacement is bounded rather than a leak; a fetch that lands clears
/// the count, since it proves the path answers again.
const MAX_RETIRED_PULL_WORKERS: usize = 2;

/// The bound a durability-attesting answer gives the journal sink's
/// writer — how long a mutating request or a `GET /journal` waits for
/// the drain to take every queued record before answering anyway.
/// The wait is the request worker's own, off the executor lock; a
/// healthy sink drains in microseconds, so reaching the bound means
/// the writer is stalled — and the queue's own bound has already made
/// the run's pushes fatal rather than let the file trail silently.
const JOURNAL_DRAIN_WAIT: Duration = Duration::from_secs(30);

/// The bound a durability-attesting answer gives the state-file
/// sink's writer — how long `POST /command`'s accepted admission or a
/// driven `POST /scan` waits for the drain to have replaced the file
/// through the request's own pushed checkpoint before the answer can
/// promise it durable. The wait is the request worker's own, off the
/// executor lock; a healthy sink drains in microseconds, so reaching
/// the bound means the writer is stalled — and the queue's own bound
/// has already made the run's pushes fatal rather than let the file
/// trail silently.
const STATE_DRAIN_WAIT: Duration = crate::state_file::STATE_DRAIN_WAIT;

/// How many announced follow-peer hints the tracking-source contract
/// retains: every standby pulling `GET /checkpoint?peer=` lands its
/// own monitor address, newest first, so a demotion with several
/// standbys verifies the whole set — preferring the candidate that
/// serves the line as its field owner — rather than inheriting
/// whichever sibling announced last. The bound keeps an announce
/// flood from growing the set unboundedly; a real redundancy group is
/// a handful of peers.
const MAX_ANNOUNCED: usize = 8;

/// How long the tracking path waits before re-probing endpoints a
/// verify pass already refused — the re-probe bound shared by the
/// involuntary path's announced hint set, the claim's declared
/// monitor, and the orphan-resolution probe's candidate set: a peer
/// demoted mid-run with only unproven hints re-verifies when the set
/// changes — a new announce lands or the order shifts — or once this
/// much wall time has passed, so a dead or hostile set cannot stall
/// every scan cycle on a bounded pull burst while an endpoint that
/// recovers still earns a fresh probe inside a bounded window.
const ANNOUNCED_VERIFY_RETRY: Duration = Duration::from_secs(4);

/// How many distinct `(source, reason)` adoption-refusal signatures
/// one tracking epoch journals — the dedup bound behind
/// [`Monitor::note_source_refusal`]: each stands as durable audit
/// while a flooding adversary minting fresh reasons per probe cannot
/// grow the journal unboundedly. The set clears on every landed
/// adoption, so a re-strand audits its refusals fresh.
const MAX_REFUSAL_SIGNATURES: usize = 64;

/// How many consecutive produced-nothing pulls a learned tracking pin
/// — the orphan probe's `resolved` owner or the verified `adopted`
/// source — absorbs before the pull path releases it: the
/// wedge-escape slots outrank the configured source only while they
/// answer, and a pin that stops answering must not become the wedge
/// it was pinned to escape. At the bound the pin clears and
/// [`Monitor::resolve_tracking_source`] re-probes inside the same
/// cycle, so a live successor — a healthy configured source
/// included — earns the pin back on the probe's own verification
/// while a dead one drops the pulls onto the declared slot, which the
/// tracking contract already retries without a bound. A produced
/// checkpoint resets the run: the endpoint answered, so the pin
/// stands.
const PIN_MISS_BUDGET: u32 = 4;

/// The worker count [`Monitor::serve`] dispatches the serving lane
/// across — every request that cannot hold a worker on a client-paced
/// wait and is neither a pair-liveness read nor a switchover action:
/// the bulk GETs and the `404`s. tiny_http queues accepted requests
/// internally; a dispatcher routes each onto this lane's own queue,
/// and each worker handles one end to end. The pool exists so a
/// request whose work legitimately takes a moment — a large read's
/// serialization — stalls only its own worker while every other
/// endpoint keeps answering. The response write is the one
/// client-paced wait left on this lane: a consumer that never reads a
/// large answer pins its worker in `respond` until the connection
/// dies — tiny_http exposes no socket timeout to bound it — so wedged
/// readers can still starve the bulk reads, exactly the residual the
/// heartbeat lane quarantines the pair's liveness from and the
/// control lane quarantines its actuation from.
const SERVE_WORKERS: usize = 4;

/// The worker count serving the heartbeat lane — `GET /health`,
/// `GET /checkpoint`, and `GET /role`, the reads a redundant pair's
/// liveness runs on: the standby's pull-per-scan-cycle heartbeat
/// measures the active by the second, the operator's failover verdict
/// and the pair view read the third, and the container health check
/// probes the first. `/health` and `/role` answer from the store's
/// published liveness mirror and `/checkpoint` from the shared lock
/// — no network wait — so the only client-paced wait on the lane is
/// the response write, and these answers are small: a write blocks
/// only for a client that already left earlier pipelined responses
/// unread past its buffers. Two workers keep the heartbeat answering
/// through one such wedged connection; a deliberate attack can still
/// spend both, which is why the lane's queue is bounded and its
/// overflow is refused rather than queued without limit.
const HEARTBEAT_WORKERS: usize = 2;

/// The worker count serving the control lane — `POST /promote` and
/// `POST /demote`, the switchover actuation a redundant pair's
/// failover concludes with. They are control-plane mutations, not
/// bulk reads, so they cannot ride the serving lane the
/// undrained-response residual still starves: four dead connections
/// holding large answers would pin every serving worker and an
/// operator's promote or demote — issued during an incident, exactly
/// when consoles wedge — would queue behind them indefinitely. They
/// cannot ride the heartbeat lane either: `switchover` carries a
/// bounded network wait the liveness contract excludes — a
/// promotion's final-sync fetch and a demotion's hint verification,
/// each under [`CHECKPOINT_PULL_TIMEOUT`] — and unauthenticated
/// control POSTs beside the heartbeat reads would be a new way to
/// spend the lane the pair measures life by. A lane of their own
/// keeps both halves of failover — detection and actuation —
/// answerable through the wedge. Two workers, the heartbeat lane's
/// sizing: the role changes still serialize on the shared lock, and
/// the only client-paced wait here is the response write — a small
/// answer, so a write blocks only for a client that left earlier
/// pipelined responses unread past its buffers, which one spare
/// worker absorbs.
const CONTROL_WORKERS: usize = 2;

/// The worker count serving the submission lane — `POST /scan`, the
/// one remaining handler that reads a request body once `POST
/// /command` peels off to the command lane, plus any request still
/// carrying a body the client owes: reading that body waits on the
/// client, and dropping its reader drains the remainder, the same
/// unbounded wait. tiny_http exposes no socket timeout to bound
/// either wait: a client that stalls mid-body holds its worker for as
/// long as it cares to. Those client-paced waits are quarantined on
/// this lane so the heartbeat lane — `GET /checkpoint` among it, the
/// pull a tracking standby measures the active's liveness by — and
/// the serving lane keep answering through a stalled-body flood, per
/// the disposable-consumer contract. Two workers keep a wedged body
/// read from starving the lane outright; a flood beyond the lane's
/// width can still starve submissions, but never the served surface.
const SUBMIT_WORKERS: usize = 2;

/// The command lane's queue bound: [`LANE_QUEUE_DEPTH`] plus twice the
/// served `command_queue` capacity. `POST /command` is owed a
/// receipted answer for every submission — a settlement or the named
/// `queue_full` rejection, appended to the executor's log in
/// submission order — so its lane must hold the whole wave a client
/// can pipeline before the first response returns: the admission
/// bound is only observable by overshooting it, which makes a burst
/// of twice the bound the contract's declared flood shape. The single
/// draining worker keeps the log in dispatch order — two workers
/// racing the shared lock could invert a settlement against a
/// `queue_full` refusal — and a stalled command body pins only this
/// lane. Past the bound a submission defers to the lane's own
/// overflow deck rather than a second worker running the admission
/// path beside it: the deferred request re-enters at the queue's
/// drained tail, so every receipt still lands in dispatch order.
fn command_lane_depth(command_capacity: usize) -> usize {
    LANE_QUEUE_DEPTH.saturating_add(command_capacity.saturating_mul(2))
}

/// The bound on every lane's pending-request queue — the flood half
/// of the undrained-response fix. A lane's queue only fills while its
/// workers are pinned (a wedged body read or an undrained response
/// write, both client-paced); a request arriving past the bound gets
/// a `503` from the refuse worker instead of queueing without limit,
/// so a flood held open behind pinned workers stays a bounded number
/// of queued requests rather than memory growth. Sixty-four is far
/// past any legitimate burst — a standby pulls one checkpoint a scan
/// cycle, an operator reads seconds apart — and far below memory
/// trouble: each queued request is a parsed head plus its socket.
const LANE_QUEUE_DEPTH: usize = 64;

/// The bound on a request body the monitor will read — far past the
/// largest legitimate body, a `Command` envelope or `ScanRequest` of
/// tens of bytes. A request declaring more is refused `413` before a
/// byte is read, and the read itself is `take`-bounded so a chunked or
/// understated body cannot grow the buffer past the cap either.
const MAX_REQUEST_BODY: u64 = 64 * 1024;

/// tiny_http's eager-read bound: a request whose declared
/// `Content-Length` fits under it arrives at routing with the body
/// already buffered — read inside `new_request` on the connection
/// task — so no handler read or dropped-reader drain can ever wait on
/// the client for it. The attributed switch body is a
/// [`SwitchRequest`] of tens of bytes, always under the bound; the
/// control lane takes it on that proof. Only a `Content-Length`
/// request earns the eager path: a chunked stream, an
/// `Expect: 100-continue` handshake, or a connection upgrade leaves
/// the reader live on the socket at any size.
const EAGER_BODY_LIMIT: usize = 1024;

/// The bound on the scans one `POST /scan` request may ask for — the
/// unbounded-batch fix (#1203). The externally paced contract is that
/// the run is exactly as deterministic as the requests driving it,
/// which requires every request to terminate: a `scans` past the bound
/// is refused `400` before the first scan, and an accepted batch is a
/// bounded unit of work whose per-scan costs are themselves bounded —
/// the tracking pull under [`CHECKPOINT_PULL_TIMEOUT`], the hinted and
/// claimed source verifies under the same per-pull bound, the
/// `after_scan` step the driving caller installed — so it always ends
/// inside its own request. A client gone mid-batch can hold its
/// submission worker only for the bounded remainder: tiny_http hands
/// the request no socket state to poll, so the bound is the
/// cancellation — the batch below any plausible request is the
/// batch that always terminates. The bound stays far past every
/// driven batch the contract's own legs pace (a handful of scans per
/// request): a caller wanting a longer advance issues the further
/// requests, each its own bounded, answered unit.
const MAX_SCANS_PER_REQUEST: u64 = 256;

/// Runs once after each completed requested scan, receiving the peer —
/// the plant step the driving request paces the run to (its field
/// ownership decides the step). A failure fails the request with `500`.
/// The checkpoint a `state_file`-configured run persists at that same
/// boundary is the monitor's own — queued to the [`StateSink`] under
/// the lock just after the hook, never part of it.
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

/// A configured tracking source — the operator-declared peer endpoint a
/// standby pulls checkpoints from: the `--standby`/`--peer`/`track`
/// target a deployment names.
///
/// A configured source is a name, not an address: the deployment
/// declares `host:port` and every pull the tracking path makes against
/// it resolves the name fresh — the peer restarting onto a new address
/// under the same name is the routine redundant-pair condition (a
/// container recreate, a rescheduled pod), and only per-pull
/// resolution follows it. A name that does not answer — the stopped
/// container's name gone from DNS, the mid-failover condition the pair
/// exists for — produces the same failed pull an unreachable endpoint
/// does: a counted tracking miss, never a startup failure. When the
/// name answers again the next pull resolves it and tracking resumes,
/// so a member restarted mid-failover rejoins without reconfiguration.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum TrackTarget {
    /// A resolved monitor address — the shape every learned or proven
    /// source (a verified announced adoption, a probe-resolved owner,
    /// the field-declared successor) takes, and the shape a caller
    /// already holding a concrete address configures.
    Addr(SocketAddr),
    /// A configured `host:port`. Resolution is deferred to the pull
    /// itself — each pull resolves the name anew, so an address move
    /// under it is followed rather than pinned stale; while the name
    /// does not answer, the target is the same miss an unreachable
    /// endpoint is.
    Name(String),
}

impl TrackTarget {
    /// The dialable monitor address — an `Addr` is already one; a
    /// `Name` resolves it now, `Err` naming the resolution failure
    /// while it does not resolve. A `Name` resolves on every call:
    /// the peer appearing under it later is picked up without a
    /// restart.
    fn resolve(&self) -> Result<SocketAddr, String> {
        match self {
            Self::Addr(addr) => Ok(*addr),
            Self::Name(name) => name
                .to_socket_addrs()
                .map_err(|error| format!("cannot resolve: {error}"))?
                .next()
                .ok_or_else(|| "cannot resolve: no address".to_string()),
        }
    }
}

impl std::fmt::Display for TrackTarget {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Addr(addr) => std::fmt::Display::fmt(addr, formatter),
            Self::Name(name) => std::fmt::Display::fmt(name, formatter),
        }
    }
}

/// A monitoring server sharing one executor over HTTP+JSON.
///
/// See the crate docs for the endpoint contract and the two-lock split:
/// `shared` serializes the control plane — scans, commands,
/// checkpoints, role changes — while `store` holds the published read
/// models, served rings, and the liveness mirror every read endpoint
/// copies out without touching it.
pub struct Monitor<'d> {
    shared: Mutex<Shared<'d>>,
    /// The read side: the bounded publication store the read endpoints
    /// serve, owned outside the executor lock. A fetch clones an `Arc`
    /// or an owned copy and releases the store's own small lock before
    /// any serialization or socket I/O; each completed scan swaps a new
    /// immutable [`Publication`] in, and the control plane refreshes
    /// the liveness mirror — `GET /role`'s report and `GET /health`'s
    /// last-scan stamp — wherever the peer's report or the scan stamp
    /// changes, so a wedged scan's hold on `shared` stalls neither the
    /// bulk reads nor the liveness answer that reports it.
    store: Store,
    signals: SignalIndex,
    server: Server,
    /// When set — [`bind_paced`](Self::bind_paced) — the hosting process
    /// paces scans itself through [`paced_scan`](Self::paced_scan) and
    /// `POST /scan` is refused: the wall clock owns the scan schedule.
    paced: bool,
    /// The build identity `GET /health` reports — set by
    /// [`with_build`](Self::with_build) once the hosting process knows
    /// its own version, revision, and the model it loaded.
    build: Option<BuildIdentity>,
    /// The per-requested-scan wiring [`driven`](Self::driven) installed —
    /// consulted only on an unpaced monitor, where `POST /scan` runs.
    driven: Driven<'d>,
    /// The `--state-file` checkpoint sink — `Some` when
    /// [`MonitorConfig::state_file`] names a path: every capture
    /// boundary (a scan cycle's end, an accepted command's admission)
    /// hands the just-captured checkpoint to its bounded queue under
    /// the shared lock, while the writer thread serializes and
    /// atomically replaces the file off it — the state-file persist
    /// isolation fix (#982). Sits outside `shared` so the
    /// durability-attesting waits never touch the executor lock; the
    /// pushes themselves only ever run under it, keeping the queue's
    /// FIFO order the run's true capture order.
    state_sink: Option<StateSink>,
    /// The tracking source a standby pulls checkpoints from — the
    /// `--standby` target on a paced standby's monitor, `--peer` on a
    /// launched active's, or `Driven`'s `track` on a driven one.
    /// `POST /promote` runs one final pull against it before the gate
    /// lifts ([`Peer::final_sync`]), so a command the active admitted up
    /// to the promote request is carried into the promoted run. A
    /// configured source is a [`TrackTarget::Name`] resolved per pull:
    /// the peer's name being gone from DNS while it is down is the
    /// routine redundancy condition, not a misconfiguration, and the
    /// peer's address moving under the name — the routine recreate
    /// condition — is followed by the same per-pull resolution, so the
    /// target degrades as the same pull misses an unreachable endpoint
    /// produces until the name answers where the peer now lives.
    standby_source: Option<TrackTarget>,
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
    /// checkpoint from each hint and adopts a candidate only when the
    /// pull proves the endpoint under the pair's key and the
    /// checkpoint continues this run's line in a way this run's own
    /// `/checkpoint` could not have served — a field-owning document
    /// not ahead of this run's tick being the replayable shape —
    /// preferring the candidate that serves the line as field owner
    /// over one that merely tracks it, and journaling
    /// the adopted source either way. On an unkeyed run the hints are
    /// inert: the public `/checkpoint` hands a forge every document
    /// shape, so no announced endpoint can authenticate — they never
    /// resolve as a tracking source and can never arm a demotion.
    /// On a keyed run the same proof gates the involuntary path too:
    /// a field claim's mid-run loss demotes the peer in place with no
    /// `POST /demote` boundary ever running it, so the tracking path
    /// verifies lazily instead
    /// ([`adopt_announced_source`](Self::adopt_announced_source)) —
    /// a recorded hint is a candidate, never a pull target. The set
    /// is bounded at
    /// [`MAX_ANNOUNCED`], newest first: with several standbys every
    /// announcer stays a demotion candidate rather than the last pull
    /// silently evicting the rest. A dead, replayed, or forged hint
    /// refuses `NoTrackingSource` instead of stranding or adopting.
    /// Outside `shared`: the value is request-path bookkeeping, never
    /// part of a scan's state.
    announced: Mutex<VecDeque<SocketAddr>>,
    /// The last verification pass the tracking path ran over
    /// `announced` — the set probed and when. The involuntary
    /// demotion path runs the demote verify's scrutiny lazily, at the
    /// first tracking cycle after the demotion; a failed set is
    /// remembered here so the next cycle's resolution does not
    /// re-probe the same endpoints until the set changes or
    /// [`ANNOUNCED_VERIFY_RETRY`] elapses — the bound a dead hint
    /// falls back within. Outside `shared`: request-path bookkeeping
    /// like `announced`.
    announced_verify: Mutex<Option<AnnouncedVerify>>,
    /// The last field-arbitrated verify pass the tracking path ran —
    /// the monitor endpoint the claim's fencing verdicts named and
    /// when the pull ran. The claimed-monitor resolution is the
    /// unkeyed pair's only provable rendezvous: a failed pass is
    /// remembered so the next cycle's resolution does not re-pull the
    /// same endpoint until the field's verdict names a changed
    /// monitor or [`ANNOUNCED_VERIFY_RETRY`] elapses — the same bound
    /// a dead announced hint falls back within. Outside `shared`:
    /// request-path bookkeeping like `announced_verify`.
    claimed_verify: Mutex<Option<ClaimedVerify>>,
    /// The last foreign-writer diagnosis pass the tracking path ran —
    /// the monitor endpoint the field's standing claim named, when
    /// the keyed pull ran, and what it answered. This is the pair
    /// key's one job the rest of the platform cannot do: proving
    /// whether the process holding the field belongs to the keyed line
    /// at all. A pass inside its [`ANNOUNCED_VERIFY_RETRY`] window
    /// suppresses the re-pull, so a dead or hostile declared endpoint
    /// costs one bounded pull per window rather than one per scan,
    /// while a changed declaration — a new owner — is diagnosed on the
    /// next cycle. Outside `shared`: pull-path bookkeeping beside
    /// `claimed_verify`.
    foreign_verify: Mutex<Option<ForeignVerify>>,
    /// The last orphan-resolution probe pass's bookkeeping — the
    /// candidate set probed, in probe order, and the wall-clock time
    /// the pass ran. `track_cycle` runs the probe on every orphaned
    /// apply, so an unrecorded failure would re-dial every dead
    /// candidate — the standing claim's declared monitor first — once
    /// per scan, one [`CHECKPOINT_PULL_TIMEOUT`] each: the paced
    /// cadence collapses under the burst while a foreign claim stands
    /// (the QA finding
    /// `orphan-resolution-dead-claimed-monitor-stalls-scan`). A failed
    /// pass is remembered here so the next cycle's resolution
    /// re-probes only a changed set — a fresh claim declaration, a
    /// moved owner stamp, a new announcer — or the same one after
    /// [`ANNOUNCED_VERIFY_RETRY`] elapses, the same bound a dead
    /// declared monitor falls back within on the claimed path. A pass
    /// that resolves a candidate clears the record: the set proved an
    /// owner. Outside `shared`: pull-path bookkeeping like
    /// `claimed_verify`.
    resolve_verify: Mutex<Option<ResolveVerify>>,
    /// The tracking source a verified announced demotion pinned — the
    /// endpoint `POST /demote` proved serves this run's continuation
    /// under the pair's key and journaled as the adopted source. The
    /// demoted peer's pulls
    /// target it rather than re-reading `announced`, so a later
    /// `?peer=` rewrite — the same unauthenticated mutation that
    /// planted the hint — cannot move the tracking onto an endpoint
    /// the demotion never proved. The pin is provisional, though: the
    /// adopted endpoint may itself be a standby tracking onward, so
    /// while the tracked line reports no field owner the
    /// [`resolve_tracking_source`](Self::resolve_tracking_source)
    /// probe may re-resolve onto the owner the line names. `None`
    /// until a keyed announced-only demotion verifies one; a
    /// configured
    /// source always outranks it, and the next announced-only demotion
    /// re-proves and re-pins. The pin carries the learned-source
    /// liveness bound: [`PIN_MISS_BUDGET`] consecutive
    /// produced-nothing pulls against it release it back to the
    /// resolution paths a fresh probe can re-earn. Outside `shared`:
    /// request-path bookkeeping like `announced`.
    adopted: Mutex<Option<SocketAddr>>,
    /// The tracking source the orphan-resolution probe verified — an
    /// endpoint that served this run's line *as its field owner* while
    /// the tracked source reported none. The orphaned peer's pulls
    /// re-target it: the announce and adopted slots can only say who
    /// tracks this line, never who owns it — only a pulled
    /// `source_owns_field: true` document on this line's generation
    /// (plus, on a keyed run, its `line_proof`) can say that — so the
    /// resolution outranks every softer source, letting a demoted peer
    /// and the sibling it adopted follow the line onward to the real
    /// owner instead of orphaned-tracking each other forever. `None`
    /// until a probe verifies one; cleared when the line reports an
    /// owner again — either the peer itself promoted, or a candidate
    /// refused the line and `resolved` falls back to the softer slots —
    /// and released on [`PIN_MISS_BUDGET`] consecutive produced-nothing
    /// pulls against it, the same re-resolution pass running then and
    /// there so a live successor keeps the pin on its own proof while a
    /// dead one cannot strand the peer. Outside `shared`: request-path
    /// bookkeeping like `announced`.
    resolved: Mutex<Option<SocketAddr>>,
    /// The tracking target the last resolved pull was aimed at — the
    /// answer [`verified_tracking_source`](Self::verified_tracking_source)
    /// returned most recently, recorded so a produced-nothing pull in
    /// [`track_cycle`](Self::track_cycle) can attribute its miss to the
    /// slot that named it: only a learned pin (`resolved`/`adopted`)
    /// carries the [`PIN_MISS_BUDGET`] liveness bound; a configured
    /// source is retried without one. Outside `shared`: pull-path
    /// bookkeeping like `resolved`.
    pulling: Mutex<Option<TrackTarget>>,
    /// The consecutive produced-nothing pulls the current learned pin
    /// has absorbed — [`PinMiss`], keyed by the pinned address so a
    /// re-pin onto a different successor restarts the run, and `None`
    /// after any produced checkpoint: the endpoint answered, so the
    /// pin stands. Outside `shared`: pull-path bookkeeping like
    /// `pulling`.
    pin_misses: Mutex<Option<PinMiss>>,
    /// The owner address the last pulled checkpoint propagated — its
    /// `line_owner` stamp: the serving run's own address when it owns
    /// the field, the recorded owner's onward when it does not —
    /// served on this monitor's own `/checkpoint` answers, so a peer
    /// several tracking hops from the owner still learns where the
    /// line's field ownership lives. Serve-time bookkeeping like
    /// `announced`; a field owner stamps itself and never reads this.
    line_owner: Mutex<Option<SocketAddr>>,
    /// The adoption-refusal signatures this run's tracking path has
    /// already journaled — (endpoint, reason) pairs, one
    /// `tracking_source_refused` entry each, so a persistently
    /// refusing successor the resolution re-probes every cycle audits
    /// once rather than flooding the journal — and never silently.
    /// Cleared when a source adoption lands: a new tracking epoch can
    /// meet the same refusal fresh. Outside `shared`: pull-path
    /// bookkeeping like `pin_misses`.
    refused: Mutex<BTreeSet<(SocketAddr, String)>>,
    /// This run's half of the pair's shared tracking secret — the key
    /// [`with_pair_key`](Self::with_pair_key) installs from
    /// dcs-controller's `--pair-token`. A `GET /checkpoint?prove=<nonce>`
    /// response on a keyed monitor stamps the served document's
    /// [`line_proof`], and the pulls this monitor makes toward an
    /// adopted announced source — plus the one that verifies a
    /// demotion's announced hint — each carry a fresh nonce whose
    /// returned proof must match. Unset, `?prove=` answers are plain
    /// and the announced-source contract is closed: the public
    /// `/checkpoint` hands a forge every document shape, so no
    /// announced hint can ever arm a demotion or resolve as a
    /// tracking source.
    pair_key: Option<u64>,
}

/// The peer — executor plus redundancy role — and the history recorder,
/// behind one lock so a scan never runs half-recorded and every
/// mutation settles at a scan boundary. The publication store is
/// deliberately outside it: reader work never joins this lock —
/// `GET /health` and `GET /role` included, served from the liveness
/// mirror the mutations below refresh while they hold it.
struct Shared<'d> {
    peer: Peer<'d>,
    recorder: recorder::Recorder,
}

/// One announced-hint verification pass's bookkeeping — the hint set
/// probed, in announced order, and the wall-clock time the pass ran.
/// The tracking path re-probes only a changed set or one whose pass
/// aged past [`ANNOUNCED_VERIFY_RETRY`].
struct AnnouncedVerify {
    /// The announced hint set the pass probed, newest first.
    hints: Vec<SocketAddr>,
    /// When the pass ran.
    at: Instant,
}

/// One field-arbitrated verify pass's bookkeeping — the monitor
/// endpoint the standing claim's verdicts declared and the wall-clock
/// time the pull ran. The tracking path re-pulls only a changed
/// declaration or one whose pass aged past
/// [`ANNOUNCED_VERIFY_RETRY`].
struct ClaimedVerify {
    /// The declared monitor endpoint the pass pulled.
    monitor: SocketAddr,
    /// When the pass ran.
    at: Instant,
}

/// What one diagnosis pass answered for the field's standing writer —
/// the two readings [`Monitor::diagnose_foreign_writer`] can return.
/// There is no third: every case where no fresh answer was earned is
/// `Absent`, so the narrower verdict can never outlive the writer it
/// was earned against.
enum WriterDiagnosis {
    /// No writer is outside the line: a pass ran and answered — the
    /// named writer proved the line's key, or could not be reached — or
    /// no writer could be asked at all (an unkeyed run, a field owner, a
    /// claim declaring no monitor, an own-monitor or undialable
    /// declaration). Either way the run falls back to the orphan
    /// verdict's conditional claim.
    Absent,
    /// A pass ran and diagnosed the named writer: it answered and its
    /// document could not prove this line's pair key.
    Foreign(SocketAddr),
}

/// One foreign-writer diagnosis pass's bookkeeping — the monitor
/// endpoint the field's standing claim named, the wall-clock time the
/// keyed pull ran, and what it answered. The tracking path re-pulls
/// only a changed declaration, or one whose pass aged past
/// [`ANNOUNCED_VERIFY_RETRY`]; a pass that answered is the peer's
/// standing verdict, a suppressed one leaves it untouched.
struct ForeignVerify {
    /// The declared monitor endpoint the pass pulled.
    monitor: SocketAddr,
    /// When the pass ran.
    at: Instant,
    /// Whether the pass diagnosed the writer: the endpoint answered and
    /// its document could not prove the line's pair key.
    foreign: bool,
}

/// One orphan-resolution probe pass's bookkeeping — the candidate set
/// probed, in probe order, and the wall-clock time the pass ran. The
/// tracking path re-probes only a changed set or one whose pass aged
/// past [`ANNOUNCED_VERIFY_RETRY`], so a dead claimed monitor or
/// announced hint costs one bounded pull per window rather than one
/// per scan.
struct ResolveVerify {
    /// The candidate set the pass probed, in probe order.
    candidates: Vec<SocketAddr>,
    /// When the pass ran.
    at: Instant,
}

/// One learned tracking pin's liveness run — the pinned monitor
/// endpoint and the consecutive produced-nothing pulls it has
/// absorbed. Reaching [`PIN_MISS_BUDGET`] releases the pin: a pull
/// aimed at a different address starts a fresh run, and any produced
/// checkpoint clears it — the endpoint answered, so the pin stands.
struct PinMiss {
    /// The pinned endpoint the misses accrued against.
    source: SocketAddr,
    /// The consecutive produced-nothing pulls so far.
    misses: u32,
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

    /// The build identity `GET /health` reports — the hosting process's
    /// own declaration of what it is: its crate version, the git
    /// revision it was compiled from where one was recorded, and the
    /// fingerprint of the model it loaded. Call once, before serving
    /// traffic; a later call replaces the declaration.
    pub fn with_build(mut self, build: BuildIdentity) -> Self {
        self.build = Some(build);
        self
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
        // The three persistence paths must be distinct files — the
        // bind refuses the aliased configuration the controller's
        // option parse already refuses on its flags, so a
        // programmatically assembled `MonitorConfig` cannot smuggle it
        // in either (finding state-file-alias-clobbers-append-durable-files).
        config.check_persistence_paths()?;
        // The state-file sink's writer spawns at bind beside the
        // recorder's journal drain — its shared counters stamp every
        // publication's `state_sink` section from the bind-time read
        // model on. The sink takes the checkpoint's single-writer lock
        // unless the caller already holds it for this process
        // (`state_writer`): a second live writer on the same path is
        // refused here naming the conflict (finding
        // state-file-shared-between-processes-not-detected), which is
        // the checkpoint sink's half of the append sinks' single-writer
        // rule.
        let state_sink = match (config.state_file.as_deref(), config.state_writer.clone()) {
            (None, _) => None,
            (Some(path), Some(writer)) => Some(StateSink::with_writer(
                path,
                config.state_drain_capacity,
                writer,
            )?),
            (Some(path), None) => Some(StateSink::new(path, config.state_drain_capacity)?),
        };
        let mut recorder = recorder::Recorder::new(config, peer.tick(), peer.executor().anchor())?;
        // A `--state-file`-restored executor already carries the run's
        // state — the receipt log and the restored image are this run's
        // own record continuing, not new events to journal.
        recorder.observe_standing(peer.executor());
        let store = recorder.store();
        if let Some(sink) = &state_sink {
            store.set_state_sink(sink.shared());
        }
        // The bind-time read model is the first publication — any
        // journal tail a configured file replayed rides its event
        // delta — so the read endpoints serve from the store from the
        // moment the monitor exists. The liveness mirror seeds beside
        // it: `GET /health` and `GET /role` answer the starting report
        // until the first scan's refresh.
        store.publish(peer.tick(), peer.snapshot(), peer.receipts());
        store.sync_liveness(peer.report());
        Ok(Self {
            shared: Mutex::new(Shared { peer, recorder }),
            store,
            signals,
            server: Server::http(addr).map_err(io::Error::other)?,
            paced: false,
            build: None,
            driven: Driven::default(),
            state_sink,
            standby_source: None,
            announced: Mutex::new(VecDeque::new()),
            announced_verify: Mutex::new(None),
            claimed_verify: Mutex::new(None),
            foreign_verify: Mutex::new(None),
            resolve_verify: Mutex::new(None),
            adopted: Mutex::new(None),
            resolved: Mutex::new(None),
            pulling: Mutex::new(None),
            pin_misses: Mutex::new(None),
            line_owner: Mutex::new(None),
            refused: Mutex::new(BTreeSet::new()),
            pair_key: None,
        })
    }

    /// Arms `POST /scan` with `driven` wiring and returns the monitor —
    /// see [`Driven`]. Meaningful only on an unpaced monitor: a paced
    /// one refuses `POST /scan`, so the wiring never runs. The `track`
    /// address also becomes the promotion-boundary pull's source, and
    /// mirrors into the peer so every checkpoint it serves — and the
    /// `after_scan` state-file persist — stamps it as
    /// `tracking_source`. A driven peer whose tracking source is
    /// declared by *name* — the `host:port` a `--standby`/`--peer`
    /// argument carries — installs it through
    /// [`with_standby_target`](Self::with_standby_target) instead, so
    /// each pull resolves the name anew rather than pinning the address
    /// it resolved to once.
    pub fn driven(mut self, driven: Driven<'d>) -> Self {
        self.standby_source = driven.track.map(TrackTarget::Addr);
        if let Some(track) = driven.track {
            self.shared.lock().unwrap().peer.note_tracking_source(track);
        }
        self.driven = driven;
        self
    }

    /// Records the address a tracking standby pulls checkpoints from —
    /// a paced `--standby` run's target, which the pacing loop owns and
    /// `Driven` never sees. `POST /promote` runs one final pull against
    /// it so the promoted run carries every command the active admitted
    /// up to the promote request. The same address mirrors into the
    /// peer so its served and persisted checkpoints stamp it as
    /// `tracking_source`.
    pub fn with_standby_source(self, source: SocketAddr) -> Self {
        self.with_standby_target(TrackTarget::Addr(source))
    }

    /// As [`with_standby_source`](Self::with_standby_source) for a
    /// configured target declared as a [`TrackTarget::Name`] — the
    /// shape a `host:port` argument takes: every pull re-resolves the
    /// name, so the peer's address moving under it — the routine
    /// recreate or reschedule condition — is followed without
    /// reconfiguration, and a name that does not answer reports as the
    /// same tracking misses an unreachable endpoint produces until the
    /// peer returns under it.
    pub fn with_standby_target(mut self, target: TrackTarget) -> Self {
        self.standby_source = Some(target.clone());
        // A configured `Addr` mirrors into the peer so its served and
        // persisted checkpoints stamp it as `tracking_source`, naming
        // the incumbent's checkpoint stream to a later restart-as-active
        // consult. A `Name` carries no address until a pull resolves
        // it, so it names no stamped source — the consult then falls
        // back to whatever the served line announced.
        if let TrackTarget::Addr(source) = target {
            self.shared
                .lock()
                .unwrap()
                .peer
                .note_tracking_source(source);
        }
        self
    }

    /// Installs this run's half of the pair's shared tracking secret —
    /// `key` is the deployment's `--pair-token` hashed through
    /// [`pair_key`]; both peers of a redundant pair launch with the
    /// same one. Then `GET /checkpoint?prove=<nonce>` answers with the
    /// served document's keyed [`line_proof`] stamped over it, and the
    /// announced-source contract demands the matching proof back: the
    /// demotion's verify pull and every pull the adopted source later
    /// answers must return the digest only a peer holding the key can
    /// produce — bound to that nonce and that document — so an
    /// endpoint that merely replays or fabricates this line's
    /// checkpoints can neither arm the demotion nor feed the demoted
    /// peer forged state. Unset, the deployment configured no shared
    /// secret: `?prove=` answers stay plain and the announced-source
    /// contract stays closed — the public `/checkpoint` hands a forge
    /// every document shape, so no announced endpoint can
    /// authenticate and an announced-only demotion refuses
    /// `NoTrackingSource`.
    pub fn with_pair_key(mut self, key: u64) -> Self {
        self.pair_key = Some(key);
        self
    }

    /// The checkpoint address this peer tracks — `Driven`'s `track` or
    /// the configured [`with_standby_source`](Self::with_standby_source)
    /// when set, else the pinned adoption a verified announced
    /// demotion recorded, else — on a keyed run only — the monitor
    /// address a tracking peer announced through its
    /// `GET /checkpoint?peer=` pulls — an
    /// announce accepted only from the connection it names as its own
    /// address, with a wildcard-announced IP resolved to that address
    /// and a port-0 claim refused, so the recorded source is always
    /// one a demotion could dial. A recorded hint alone does not arm
    /// a demotion: the serving side cannot verify the puller's monitor
    /// port from the connection, so `POST /demote` toward an
    /// announced-only source first pulls one checkpoint from it and
    /// proceeds only when that checkpoint continues this run's line in
    /// a way this run's own public `/checkpoint` could not have
    /// produced — and carries the keyed `line_proof` only a peer
    /// holding the pair's token stamps — adopting the source
    /// into the journal and pinning it as the tracking target, so a
    /// later `?peer=` rewrite cannot redirect the demoted peer's
    /// pulls — and otherwise refuses `NoTrackingSource`. The
    /// announced fallback is keyed-only by construction: `/checkpoint`
    /// is public, so on an unkeyed run every document shape an
    /// endpoint could serve — the standby's `source_owns_field: false`
    /// included — is derivable from this run's own answers and proves
    /// nothing about who serves it. A bare hint on an unkeyed run is
    /// therefore no tracking source at all, and an announced-only
    /// demotion refuses outright. On a keyed run the announced
    /// fallback is the follow-peer
    /// half of the tracking-source contract: a peer launched without
    /// a source — an active never told its peer — that is later
    /// demoted tracks its successor here and reconverges instead of
    /// stranding `unsynchronized` and unpromotable.
    ///
    /// A [`resolved`](Self::resolved) source — the endpoint the
    /// orphan-resolution probe proved serves this line *as its field
    /// owner* — outranks even the configured slots: a `--standby`
    /// target that itself tracks onward is exactly the wedge this
    /// resolution exists to escape, and every softer slot can only
    /// say who tracks the line, never who owns it.
    ///
    /// The announced tail of this answer is a *recorded hint*, never
    /// a pull target on its own: every cycle that actually fetches a
    /// checkpoint resolves through
    /// [`verified_tracking_source`](Self::verified_tracking_source),
    /// which spends an announced hint only after the demote verify's
    /// scrutiny proves it. Read this for the recorded resolution —
    /// diagnostics and the demote guard's "any candidate exists" test
    /// — not as the pull's destination. A configured source still
    /// waiting on DNS resolves here only while its name currently
    /// answers: the recording is an address or nothing, while the pull
    /// path keeps the name and retries it per cycle.
    pub fn tracking_source(&self) -> Option<SocketAddr> {
        self.tracking_target()
            .and_then(|target| target.resolve().ok())
    }

    /// The recorded tracking source as the pull path sees it — the
    /// [`TrackTarget`] form of [`tracking_source`](Self::tracking_source):
    /// a configured name that does not resolve yet is still the
    /// declared source here (a `Name` the pulls retry), where the
    /// address-shaped answer above can only report `None`.
    fn tracking_target(&self) -> Option<TrackTarget> {
        self.pull_source().or_else(|| {
            // The bare announced hint resolves only under the keyed
            // contract — an unkeyed run could never prove the
            // endpoint, so it is no tracking source at all there.
            self.pair_key
                .and_then(|_| self.announced.lock().unwrap().front().copied())
                .map(TrackTarget::Addr)
        })
    }

    /// The checkpoint source a tracking pull may target without
    /// further proof — the orphan probe's verified owner
    /// ([`resolved`](Self::resolved)) first, then the configured
    /// `Driven`/`with_standby_source` target, then the pinned
    /// adoption a verified announced source recorded. The recorded
    /// `announced` hints are deliberately absent: they are unproven
    /// claims — the serving side cannot tell the puller's monitor
    /// port from any other same-IP port the connection claims — so no
    /// pull ever follows one until the demote verify's scrutiny
    /// proves it and pins it into `adopted`. The learned pins this
    /// answers — `resolved` and `adopted` — hold their rank only
    /// while they answer: [`PIN_MISS_BUDGET`] consecutive
    /// produced-nothing pulls against one releases it back through
    /// [`track_cycle`](Self::track_cycle), so a dead pin cannot
    /// shadow a healthy configured slot forever.
    fn pull_source(&self) -> Option<TrackTarget> {
        self.resolved
            .lock()
            .unwrap()
            .map(TrackTarget::Addr)
            .or_else(|| self.driven.track.map(TrackTarget::Addr))
            .or_else(|| self.standby_source.clone())
            .or_else(|| self.adopted.lock().unwrap().map(TrackTarget::Addr))
    }

    /// The checkpoint source a tracking cycle pulls now — the
    /// proven half of [`tracking_source`](Self::tracking_source)
    /// ([`pull_source`](Self::pull_source)), and when none stands
    /// while the peer owns no field on a keyed run, the announced
    /// hint set probed through the demote verify's own checks
    /// ([`adopt_announced_source`](Self::adopt_announced_source)),
    /// a passing candidate pinning into `adopted`. A bare `?peer=`
    /// announce can therefore never redirect a pull — the involuntary
    /// path applies the same scrutiny `POST /demote` does: a field
    /// claim's mid-run loss demotes the peer in place with no request
    /// boundary to hang the verification on, so it runs lazily here,
    /// and an endpoint that cannot prove it serves this run's
    /// continuation is refused the same way — a peer with only
    /// unproven hints pulls nothing. When no announced source
    /// verifies — on an unkeyed run, where none ever can — the
    /// field-arbitrated successor stands last
    /// ([`adopt_claimed_source`](Self::adopt_claimed_source)): the
    /// monitor endpoint the field's standing claim declares, the
    /// only address the pair's arbitration itself can vouch for.
    ///
    /// The answer is also recorded as the pull the tracking cycle is
    /// about to run — [`pulling`](Self::pulling) — so a
    /// produced-nothing result attributes its miss to the slot that
    /// named it and the learned pins' [`PIN_MISS_BUDGET`] liveness
    /// bound can release a dead one.
    pub fn verified_tracking_source(&self) -> Option<TrackTarget> {
        let source = self
            .pull_source()
            .or_else(|| self.adopt_announced_source().map(TrackTarget::Addr))
            .or_else(|| self.adopt_claimed_source().map(TrackTarget::Addr));
        *self.pulling.lock().unwrap() = source.clone();
        source
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
    /// five lanes. [`command_request`] wins first — `POST /command`
    /// takes the dedicated command lane, the receipted-ingress path
    /// the bounded-admission contract owns: every submission must
    /// answer with a settlement or the named `queue_full` rejection,
    /// appended to the executor's log in submission order, so the lane
    /// drains through one worker and queues [`command_lane_depth`]
    /// deep — enough to hold the whole pipelined wave the contract's
    /// flood shape can send before a response returns. [`role_change`]
    /// next — the switchover POSTs split on [`buffered_body`]: one
    /// whose body is already off the wire — the bare request, and the
    /// attributed `{"actor":…}` body that always fits tiny_http's
    /// eager-read bound — takes the control lane's
    /// [`CONTROL_WORKERS`], keeping the incident-time actuation
    /// guarantee for the operator's declared-identity form; one still
    /// owing the client body bytes takes the submission lane instead.
    /// [`submission`] next — the remaining requests that can hold a
    /// worker on a client-paced body
    /// wait (`POST /scan`, plus any request still carrying a body the
    /// client owes, whose dropped reader drains the rest the same
    /// way) go to the submission lane's [`SUBMIT_WORKERS`] workers,
    /// whatever their path: a stalled-body `GET /checkpoint` must not
    /// pin a heartbeat worker either. Bodiless pair-liveness reads —
    /// `GET /health`,
    /// `GET /checkpoint`, `GET /role` — go to the heartbeat lane's
    /// [`HEARTBEAT_WORKERS`]
    /// workers; everything else goes to the serving lane's
    /// [`SERVE_WORKERS`].
    ///
    /// The split exists because serving holds two waits no handler can
    /// bound: the body read and the response write — tiny_http exposes
    /// no socket timeout for either, so a client that stalls mid-body
    /// or never drains a large answer pins its worker for as long as
    /// the connection stays open. Quarantining body waits keeps the
    /// reads answering through a stalled-body flood; quarantining the
    /// pair-liveness reads keeps the standby heartbeat and the role
    /// surface answering through an undrained-response flood — a
    /// wedged reader can starve the bulk GETs but can never
    /// impersonate a dead active. The control lane closes the same
    /// gap on the actuation side: a role change queued behind serving
    /// workers pinned by dead readers would wait out the wedge
    /// silently, so the switchover endpoints answer from a pool the
    /// bulk reads can never reach. Every lane's queue is bounded — the
    /// command lane by its [`command_lane_depth`] wave, the rest by
    /// [`LANE_QUEUE_DEPTH`]; a non-command overflow is refused `503`
    /// by the refuse worker rather than queueing without limit, while
    /// an overflowed `POST /command` defers to the command lane's own
    /// bounded overflow deck — the admission contract owes every
    /// submission a receipted answer, and the deck's feeder hands it
    /// back to the one draining worker in dispatch order, so the
    /// receipt log keeps submission order exactly where the flood is
    /// deepest. A request that cannot even queue for refusal is
    /// dropped on its own detached thread so the dispatcher itself
    /// never joins a client-paced wait. The executor's command/scan interleaving
    /// stays deterministic either way: the pools only decide which
    /// request waits on the shared lock next, and scans, commands,
    /// checkpoints, and role changes still serialize on it.
    pub fn serve(&self) {
        let command_depth = command_lane_depth(
            self.shared
                .lock()
                .unwrap()
                .peer
                .executor()
                .command_queue_capacity(),
        );
        let submissions = Lane::new();
        let commands = CommandLane::with_depth(command_depth);
        let command_overflow = Lane::new();
        let heartbeat = Lane::new();
        let control = Lane::new();
        let served = Lane::new();
        let refused = Lane::new();
        std::thread::scope(|scope| {
            scope.spawn(|| {
                while let Ok(request) = self.server.recv() {
                    // `POST /command` owns its routing: a full lane
                    // defers to the command overflow lane — whose
                    // feeder hands each submission back to the lane's
                    // drained tail — so the one command worker still
                    // mints its receipt in dispatch order rather than
                    // a second worker racing the queued wave on the
                    // shared lock. Past even the overflow bound the
                    // request drops like any over-bound request; its
                    // deferral first goes back so the count keeps
                    // matching the submissions the feeder still owes.
                    if command_request(&request) {
                        let Some(request) = commands.push(request) else {
                            continue;
                        };
                        if let Some(request) = command_overflow.push(request) {
                            commands.undefer();
                            std::thread::spawn(move || drop(request));
                        }
                        continue;
                    }
                    let lane = if role_change(&request) {
                        // A switchover request splits on whether its
                        // body can still wait on the client: the bare
                        // request and the attributed `{"actor":…}` body
                        // — always inside tiny_http's eager-read bound —
                        // arrive already buffered and keep the control
                        // lane's incident-time guarantee, while one
                        // still owing the client body bytes takes the
                        // submission lane's quarantine like every other
                        // body wait.
                        if buffered_body(&request) {
                            &control
                        } else {
                            &submissions
                        }
                    } else if submission(&request) {
                        &submissions
                    } else if pair_liveness(&request) {
                        &heartbeat
                    } else {
                        &served
                    };
                    // A full lane never holds the dispatcher: the
                    // request falls to the refuse lane — a `503` —
                    // and past even that bound it is dropped on a
                    // detached thread: a dropped request answers `500`
                    // on the way out, itself a write a wedged
                    // connection can stall, so the drop runs off the
                    // dispatcher's thread rather than ever blocking
                    // routing.
                    let Some(request) = lane.push(request) else {
                        continue;
                    };
                    if let Some(request) = refused.push(request) {
                        std::thread::spawn(move || drop(request));
                    }
                }
                // `recv` ending — `unblock` or a dead listener —
                // drains every lane and releases their workers. The
                // overflow lane closes ahead of the command lane so
                // its feeder can still place the deferred submissions
                // it is holding before the lane stops taking them.
                submissions.close();
                command_overflow.close();
                commands.close();
                heartbeat.close();
                control.close();
                served.close();
                refused.close();
            });
            for _ in 0..SERVE_WORKERS {
                let lane = &served;
                scope.spawn(move || {
                    while let Some(request) = lane.pop() {
                        self.handle(request);
                    }
                });
            }
            for _ in 0..HEARTBEAT_WORKERS {
                let lane = &heartbeat;
                scope.spawn(move || {
                    while let Some(request) = lane.pop() {
                        self.handle(request);
                    }
                });
            }
            for _ in 0..CONTROL_WORKERS {
                let lane = &control;
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
            // One worker drains the command lane: a single consumer
            // keeps the receipt log in dispatch order — the
            // submission-sequence ordering the receipted-ingress audit
            // correlates by — and a stalled command body pins only this
            // worker while `POST /scan` keeps its own lane.
            let lane = &commands;
            scope.spawn(move || {
                while let Some(request) = lane.pop() {
                    self.handle(request);
                }
            });
            // The overflow feeder places the command lane's deferred
            // submissions: `push_wait` lands each at the queue's
            // drained tail — behind every earlier submission still
            // queued — so the single command worker mints every
            // receipt in dispatch order, flooded lane or not. A closed
            // lane hands the request back for the same detached drop
            // the dispatcher uses.
            let overflow = &command_overflow;
            let commands = &commands;
            scope.spawn(move || {
                while let Some(request) = overflow.pop() {
                    if let Some(request) = commands.push_wait(request) {
                        std::thread::spawn(move || drop(request));
                    }
                }
            });
            // The refuse worker answers the overflow every other lane
            // shares: one bounded queue of requests that get a `503`
            // instead of an unbounded wait. Its respond can wedge on a
            // dead connection like any client-paced wait, quarantined
            // to this one worker, whose own queue stays bounded the
            // same way.
            let lane = &refused;
            scope.spawn(move || {
                while let Some(request) = lane.pop() {
                    let _ = request.respond(json(503, "serving overloaded"));
                }
            });
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

    /// The deferred startup grant's latched refusal verdict, drained —
    /// [`Peer::drain_startup_refusal`](dcs_runtime::Peer::drain_startup_refusal)
    /// under the shared lock. `Some` once per run: a scan settled the
    /// pending born-active's conditional ask into the deferred half of
    /// the [`Activation::Refused`](dcs_runtime::Activation::Refused)
    /// verdict, which no activation result can carry to the run's
    /// shell. The caller disposes of it under the identical contract
    /// the activation-time answer takes — rejoin the declared pair, or
    /// exit where none was declared. A driven run's serve loop stands
    /// down when that verdict lands pairless so the shell settles it;
    /// a paced loop drains it inside its own scan cycle.
    ///
    /// The one accessor outside [`Peer::drain_pending`](dcs_runtime::Peer::drain_pending),
    /// and deliberately so: the latch answers how this run *ends*, which
    /// the run's shell decides at the boundary the verdict answers, while
    /// the drained events answer what the run *observed* — the journal's
    /// account. The verdict's journal entry is queued and does drain with
    /// the rest.
    pub fn drain_startup_refusal(&self) -> Option<SwitchError> {
        self.shared.lock().unwrap().peer.drain_startup_refusal()
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

    /// The durable journal sink's live drain report — the named
    /// backpressure state the journal-append isolation decision
    /// (#942) stamps into every publication's `journal_sink` section:
    /// `healthy` while the writer keeps up, `lagging` while records
    /// wait in its bounded queue, `failed` after a sink write error —
    /// with `lost` accounting the queued records the file never took.
    /// `None` when no journal file is configured.
    pub fn journal_sink_health(&self) -> Option<JournalSinkHealth> {
        self.store.journal_sink_health()
    }

    /// Waits — at most `timeout` — for the journal sink's writer to
    /// have appended or accounted every queued record, and returns
    /// the standing health either way: `drained + lost == accepted`
    /// says the durable file caught up. `None` when no journal file
    /// is configured. The wait rides the caller's thread alone — the
    /// graceful-shutdown and durability-attestation flush, never the
    /// executor lock.
    pub fn flush_journal_sink(&self, timeout: Duration) -> Option<JournalSinkHealth> {
        self.store.wait_journal_drained(timeout)
    }

    /// The durable history-file sink's live drain report — the named
    /// backpressure state the durable-history decision stamps into
    /// every publication's `history_sink` section: `healthy` while the
    /// writer keeps up, `lagging` while records wait in its bounded
    /// queue, `failed` after a sink write error — with `lost`
    /// accounting the queued records the file never took. `None` when
    /// no history file is configured.
    pub fn history_sink_health(&self) -> Option<HistorySinkHealth> {
        self.store.history_sink_health()
    }

    /// Waits — at most `timeout` — for the history sink's writer to
    /// have appended or accounted every queued record, and returns
    /// the standing health either way: `drained + lost == accepted`
    /// says the durable file caught up. `None` when no history file
    /// is configured. The wait rides the caller's thread alone — the
    /// graceful-shutdown and durability-attestation flush, never the
    /// executor lock.
    pub fn flush_history_sink(&self, timeout: Duration) -> Option<HistorySinkHealth> {
        self.store.wait_history_drained(timeout)
    }

    /// The `--state-file` sink's live drain report — the named
    /// backpressure state the persist-isolation fix (#982) stamps into
    /// every publication's `state_sink` section: `healthy` while the
    /// writer keeps up, `lagging` while checkpoints wait in its
    /// bounded queue, `failed` after a write error — with `lost`
    /// accounting the queued captures the file never took. `None`
    /// when no state file is configured.
    pub fn state_sink_health(&self) -> Option<StateSinkHealth> {
        self.store.state_sink_health()
    }

    /// Waits — at most `timeout` — for the state-file sink's writer to
    /// have replaced the file through or accounted every queued
    /// capture, and returns the standing health either way:
    /// `drained + lost == accepted` says the durable file caught up.
    /// `None` when no state file is configured. The wait rides the
    /// caller's thread alone — the graceful-shutdown and
    /// durability-attestation flush, never the executor lock.
    pub fn flush_state_sink(&self, timeout: Duration) -> Option<StateSinkHealth> {
        self.store.wait_state_drained(timeout)
    }

    /// The executor's current transferable state, taken under the lock —
    /// the same between-scans [`Checkpoint`] `GET /checkpoint` serves.
    pub fn checkpoint(&self) -> Checkpoint {
        self.shared.lock().unwrap().peer.checkpoint()
    }

    /// The pacing loop's end-of-cycle `--state-file` persist: captures
    /// the run's checkpoint under the shared lock — after the scan and
    /// the plant step, where the caller invokes it — and hands it to
    /// the state sink's bounded queue, so the write drains off the
    /// lock on the sink's own writer instead of lengthening the cycle.
    /// The push can never wait on the sink: a queue full past its
    /// declared bound — or a recorded write failure — is refused here
    /// with the named error the caller fails the run on, the same
    /// fatal rule the journal sink applies. `Ok` once the checkpoint
    /// is queued — FIFO behind every earlier capture, so a stale
    /// overwrite is impossible — and a no-op `Ok` when no state file
    /// is configured.
    pub fn persist_state(&self) -> Result<(), String> {
        let Some(sink) = &self.state_sink else {
            return Ok(());
        };
        let shared = self.shared.lock().unwrap();
        sink.offer(shared.peer.checkpoint()).map(|_| ())
    }

    /// Captures `peer`'s checkpoint and hands it to the state-file
    /// sink's bounded queue — every in-handler persist, the caller
    /// already holding the shared lock so the queue's FIFO order is
    /// the run's true capture order and a stale overwrite is
    /// impossible. Returns the pushed record's ordinal — what a
    /// durability-attesting answer waits through — or `None` when no
    /// state file is configured. A refusal — the queue full past its
    /// declared bound, or a recorded write failure — is the run's
    /// fatal point and panics naming the sink, the same rule the
    /// journal's pushes apply.
    fn push_state_checkpoint(&self, peer: &Peer<'_>) -> Option<u64> {
        self.state_sink.as_ref().map(|sink| {
            sink.offer(peer.checkpoint())
                .unwrap_or_else(|error| panic!("{error}"))
        })
    }

    /// Test seam: swap in a constructed state sink — the stalled- and
    /// failing-writer coverage drives the writer through closures a
    /// real file cannot produce deterministically, the same seam the
    /// journal sink's tests use. The store's probe follows, so the
    /// publication stamp reports the injected sink's counters.
    #[cfg(test)]
    pub(crate) fn with_state_sink(&mut self, sink: StateSink) -> &mut Self {
        self.store.set_state_sink(sink.shared());
        self.state_sink = Some(sink);
        self
    }

    /// The executor's current virtual tick.
    pub fn tick(&self) -> Tick {
        self.shared.lock().unwrap().peer.tick()
    }

    /// The instance's reported redundancy role — what `GET /role`
    /// serves — the store's published liveness mirror as the control
    /// plane last refreshed it, so the read cannot queue behind a scan
    /// wedged in field I/O.
    pub fn role_report(&self) -> RoleReport {
        self.store
            .liveness()
            .expect("the bind seeds the liveness mirror")
            .report
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
    /// outlives a dead holder, so it must be the run's last local
    /// startup step and its first shared-field side effect — a process
    /// that fails earlier leaves no stale claim fencing the field's
    /// standing owner. Where the conditional startup grant is installed
    /// the claim preempts a dead owner's standing claim but refuses a
    /// *live* incumbent's, so a stale restart cannot silently roll back
    /// state the incumbent receipted.
    ///
    /// The answer is the startup's settled [`Activation`], with the
    /// stand-down a refusal or an inconclusive ask already run inside
    /// the peer — `Granted` owns the field, `Pending` waits on the
    /// deferred grant behind the served standby surface, `Refused`
    /// carries the `FieldClaimFailed` the caller's disposition decides
    /// (rejoin a declared pair, or exit undeclared), and the refusal's
    /// observed claimant plus the settled role transition journal here
    /// before the caller learns them. `Err` is the launch-defect half —
    /// a peer that is not a launched active, or the unconditional
    /// claim's own failure — still fatal.
    pub fn activate(&self) -> Result<dcs_runtime::Activation, dcs_core::SwitchError> {
        let mut shared = self.shared.lock().unwrap();
        let Shared { peer, recorder, .. } = &mut *shared;
        let activation = peer.activate()?;
        // One drain, one match: the activation's whole account. The
        // kinds this path does not expect are named and dropped rather
        // than left queued for a later site — an activation queues
        // nothing else, and a kind that did arrive has its own producer
        // and its own journal site.
        for event in peer.drain_pending() {
            match event {
                PeerEvent::ClaimObservation(observation) => {
                    recorder.note_claim_observed(observation);
                }
                PeerEvent::StartupRefusal(refusal) => {
                    recorder.note_startup_claim_refused(refusal);
                }
                PeerEvent::RoleChange(change) => recorder.note_role_change(&change),
                PeerEvent::Divergence(_)
                | PeerEvent::Resolution(_)
                | PeerEvent::Reinitialization(_)
                | PeerEvent::Orphan(_)
                | PeerEvent::FencingLoss(_)
                | PeerEvent::ClaimRearm(_)
                | PeerEvent::SourceRestart(_)
                | PeerEvent::PromotionRefusal(_)
                | PeerEvent::ForeignClaimPreempt(_)
                | PeerEvent::SupersededCommand { .. }
                | PeerEvent::RehomedReceipt { .. }
                | PeerEvent::AdoptionReceipt(_) => {}
            }
        }
        self.store.sync_liveness(peer.report());
        Ok(activation)
    }

    /// Applies a checkpoint pulled from the active peer — the standby's
    /// tracking half of the redundancy contract, taken under the same
    /// lock that serializes scans, so the apply lands at a scan
    /// boundary. A field-owning instance refuses with
    /// [`ApplyError::OwnsField`]; a rejected checkpoint rolls back and
    /// the peer reports itself degraded.
    ///
    /// The apply also runs the peer's staged-output divergence check; a
    /// transition into `diverged` is journaled at the run tick the
    /// apply landed on.
    pub fn apply_checkpoint(&self, checkpoint: &Checkpoint) -> Result<(), ApplyError> {
        let mut shared = self.shared.lock().unwrap();
        let Shared { peer, recorder, .. } = &mut *shared;
        let result = peer.apply(checkpoint);
        let tick = peer.tick();
        // One drain, one match — the applied checkpoint's whole
        // account, in the producer's category order, which is the
        // order this path journaled them in by hand.
        for event in peer.drain_pending() {
            match event {
                PeerEvent::Divergence(report) => {
                    recorder.note_divergence(report.tick, report.mismatches);
                }
                PeerEvent::Resolution(resolution) => recorder.note_resolution(resolution),
                PeerEvent::Orphan(orphan) => recorder.note_field_orphaned(orphan),
                PeerEvent::SourceRestart(restart) => recorder.note_source_restart(restart),
                PeerEvent::SupersededCommand { index, receipt } => {
                    recorder.note_settled(Some(index), receipt, tick);
                }
                PeerEvent::RehomedReceipt {
                    prior,
                    index,
                    receipt,
                } => recorder.note_rehomed(prior, index, receipt, tick),
                PeerEvent::AdoptionReceipt(receipt) => {
                    recorder.note_settled(None, receipt, tick);
                }
                PeerEvent::Reinitialization(_)
                | PeerEvent::FencingLoss(_)
                | PeerEvent::ClaimRearm(_)
                | PeerEvent::ClaimObservation(_)
                | PeerEvent::StartupRefusal(_)
                | PeerEvent::PromotionRefusal(_)
                | PeerEvent::ForeignClaimPreempt(_)
                | PeerEvent::RoleChange(_) => {}
            }
        }
        // An adopted checkpoint carries the active's receipt log —
        // refresh the store's mirror so `GET /receipts` stays current
        // before the next scan publishes. The apply also moved the
        // report — tick and convergence — so the liveness mirror
        // refreshes beside it.
        self.store.sync_receipts(peer.receipts());
        self.store.sync_liveness(peer.report());
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
        let Shared { peer, recorder, .. } = &mut *shared;
        let result = peer.transfer(checkpoint);
        let tick = peer.tick();
        // The transfer's own drain, one match: the same applied-checkpoint
        // account the plain apply journals, plus the model-boundary
        // crossing a rolling revision adds, in the producer's category
        // order.
        for event in peer.drain_pending() {
            match event {
                PeerEvent::Divergence(report) => {
                    recorder.note_divergence(report.tick, report.mismatches);
                }
                PeerEvent::Resolution(resolution) => recorder.note_resolution(resolution),
                PeerEvent::Reinitialization(report) => recorder.note_reinitialized(report),
                PeerEvent::Orphan(orphan) => recorder.note_field_orphaned(orphan),
                PeerEvent::SourceRestart(restart) => recorder.note_source_restart(restart),
                PeerEvent::SupersededCommand { index, receipt } => {
                    recorder.note_settled(Some(index), receipt, tick);
                }
                PeerEvent::RehomedReceipt {
                    prior,
                    index,
                    receipt,
                } => recorder.note_rehomed(prior, index, receipt, tick),
                PeerEvent::AdoptionReceipt(receipt) => {
                    recorder.note_settled(None, receipt, tick);
                }
                PeerEvent::FencingLoss(_)
                | PeerEvent::ClaimRearm(_)
                | PeerEvent::ClaimObservation(_)
                | PeerEvent::StartupRefusal(_)
                | PeerEvent::PromotionRefusal(_)
                | PeerEvent::ForeignClaimPreempt(_)
                | PeerEvent::RoleChange(_) => {}
            }
        }
        self.store.sync_receipts(peer.receipts());
        self.store.sync_liveness(peer.report());
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

    /// Journals a restart-as-active incumbent consult that ran before
    /// this monitor bound — the pre-claim check a relaunched
    /// launched-active made against the checkpoint stream its persisted
    /// state (or configured `--peer`) named — attributed to the run's
    /// resumed tick. Landing it after the bind keeps the durable record
    /// in process-lifetime order: the run-boundary marker first, the
    /// consult's audit behind it, before the startup claim.
    pub fn note_restart_consult(&self, source: String, outcome: RestartConsultOutcome) {
        let mut shared = self.shared.lock().unwrap();
        let tick = shared.peer.tick();
        shared.recorder.note_restart_consult(tick, source, outcome);
    }

    /// Journals pending commands a restart-as-active consult's adoption
    /// adjudicated — the restartee's own still-`Accepted` receipts the
    /// adopted line's submission window passed without carrying, each
    /// already settled `Rejected` carrying `Superseded` beside its
    /// absolute submission index — so the settle dedup the ordinary
    /// settle path runs on applies to them too — attributed to the
    /// resumed tick, behind the consult's own entry.
    pub fn note_superseded(&self, superseded: Vec<(u64, CommandReceipt)>) {
        let mut shared = self.shared.lock().unwrap();
        let tick = shared.peer.tick();
        for (index, receipt) in superseded {
            shared.recorder.note_settled(Some(index), receipt, tick);
        }
    }

    /// Marks a tracking peer degraded after a checkpoint fetch produced
    /// nothing — an unreachable active or a refused request — and counts
    /// the heartbeat miss toward the failover budget.
    pub fn note_transfer_failed(&self, detail: impl std::fmt::Display) {
        let mut shared = self.shared.lock().unwrap();
        shared.peer.note_transfer_failed(detail);
        self.store.sync_liveness(shared.peer.report());
    }

    /// Journals a tracking-source probe's refusal of the endpoint it
    /// verified against — `source`'s served checkpoint failed the
    /// line-membership bar — once per distinct `(source, reason)`
    /// signature per tracking epoch: a demoted or orphaned peer
    /// re-probes the standing refusal every cycle, and the durable
    /// record should name it once, not once per scan, while a source
    /// adoption clears the epoch so a re-strand audits fresh. Past
    /// [`MAX_REFUSAL_SIGNATURES`] distinct signatures the epoch's
    /// audit stops recording new reasons rather than letting a
    /// rotating forgery flood the journal.
    fn note_source_refusal(&self, source: SocketAddr, detail: String) {
        {
            let mut refused = self.refused.lock().unwrap();
            if refused.len() >= MAX_REFUSAL_SIGNATURES || !refused.insert((source, detail.clone()))
            {
                return;
            }
        }
        let mut shared = self.shared.lock().unwrap();
        let tick = shared.peer.tick();
        shared
            .recorder
            .note_tracking_source_refused(tick, source, detail);
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
    ///
    /// A pulled document's `line_owner` stamp also lands — the address
    /// this line currently names as its field owner — so this monitor's
    /// own `/checkpoint` propagates it onward. When the apply's verdict
    /// is orphaned — the tracked run is not the line's owner — the
    /// cycle ends with [`resolve_tracking_source`](Self::resolve_tracking_source),
    /// the probe that follows the propagated owner name and re-targets
    /// the pulls onto it: a demoted peer pinned onto a sibling standby
    /// reconverges on the real owner instead of orphaned-tracking the
    /// island forever.
    ///
    /// A produced-nothing pull counts against the learned pin it
    /// targeted — `resolved`/`adopted`, whichever
    /// [`verified_tracking_source`](Self::verified_tracking_source)
    /// named — and at [`PIN_MISS_BUDGET`] consecutive misses the pin
    /// releases and the same re-resolution probe runs inside this
    /// cycle ([`note_pull_missed`](Self::note_pull_missed)): a live
    /// successor, a healthy configured source included, re-earns the
    /// pulls on the probe's own verification while a dead pin drops
    /// them back onto the declared slot instead of stranding the peer
    /// on a dead endpoint the orphan path could never reach. A
    /// produced checkpoint resets the run — the endpoint answered, so
    /// the pin stands.
    pub fn track_cycle(&self, pull: impl FnOnce() -> Result<Checkpoint, String>) -> TrackReport {
        if self.shared.lock().unwrap().peer.owns_field() {
            return TrackReport::OwnsField;
        }
        let pulled = pull();
        if let Ok(checkpoint) = &pulled {
            *self.line_owner.lock().unwrap() = checkpoint.line_owner;
            *self.pin_misses.lock().unwrap() = None;
        } else {
            self.note_pull_missed();
        }
        let report = track_and_record(&mut self.shared.lock().unwrap(), &self.store, move || {
            pulled
        });
        let orphaned = matches!(
            self.shared.lock().unwrap().peer.report().sync,
            Some(StandbySync::Orphaned { .. } | StandbySync::Usurped { .. })
        );
        if matches!(report, TrackReport::Applied(_)) && orphaned {
            self.resolve_tracking_source();
        }
        // The tracked line's ownerless verdict is where the pair key's
        // one remaining question gets asked: is the writer the field
        // does have a member of this line, or a process that took the
        // field outright? The diagnosis runs on the same cycle as the
        // orphan verdict it reframes — outside `shared`, so its bounded
        // pull stalls this request and not the paced scan — and its
        // answer lands on the peer, where both promotion gates read it.
        if orphaned {
            match self.diagnose_foreign_writer() {
                WriterDiagnosis::Foreign(writer) => self.note_foreign_writer(Some(writer)),
                WriterDiagnosis::Absent => self.note_foreign_writer(None),
            }
        } else {
            // A run no longer reporting an ownerless verdict is not
            // tracking a non-owner, so it keeps no diagnosis. The
            // reading is dropped outright rather than left standing for
            // the next orphaned apply to pick up unverified: the writer
            // may have changed hands many times over since the pass
            // that earned it, and the promotion gate the next apply
            // routes would be reading that old verdict.
            self.note_foreign_writer(None);
        }
        report
    }

    /// Records the diagnosis of the field's standing writer — the
    /// narrower ownerless verdict the served `sync` reports when the
    /// endpoint the field's own arbitration named could not prove this
    /// line's pair key, and the claim shape both promotion gates read
    /// from it. `Some(writer)` records the diagnosis,
    /// `None` clears it back to the plain
    /// [`StandbySync::Orphaned`] verdict.
    ///
    /// [`track_cycle`](Self::track_cycle) runs
    /// [`diagnose_foreign_writer`](Self::diagnose_foreign_writer) and
    /// records through here on every ownerless cycle; a consumer that
    /// asks the question on its own cadence — rather than per tracking
    /// pull — records its answer through the same seam, and the served
    /// report refreshes with it.
    pub fn note_foreign_writer(&self, writer: Option<SocketAddr>) {
        let mut shared = self.shared.lock().unwrap();
        shared.peer.note_foreign_writer(writer);
        self.store.sync_liveness(shared.peer.report());
    }

    /// Counts one produced-nothing pull against the learned pin the
    /// pull was aimed at — the liveness bound the `resolved`/`adopted`
    /// slots carry: they outrank the configured source by the
    /// wedge-escape design, but only while they answer. The target the
    /// last [`verified_tracking_source`](Self::verified_tracking_source)
    /// recorded decides the attribution: a miss against the configured
    /// slot counts nothing here — the declared source's own contract
    /// already retries it without a bound — and a pull aimed at no
    /// standing pin counts nothing either.
    ///
    /// At [`PIN_MISS_BUDGET`] consecutive misses the pin releases and
    /// [`resolve_tracking_source`](Self::resolve_tracking_source) runs
    /// inside the same cycle — the orphan probe is the only
    /// re-resolution the tracking path owns, and with the pin dead it
    /// is also the only pass that can tell a merely-unreachable line
    /// from a moved one: a live successor — including the configured
    /// source healthy again — earns `resolved` back on the probe's own
    /// verification, so the pin keeps meaning "the proven endpoint",
    /// while a dead one cannot strand the peer the way an unbounded
    /// pin did. The released `adopted` slot falls through to the lazy
    /// verification paths on the next cycle, which re-prove a
    /// candidate rather than re-trusting the dead one. Runs outside
    /// `shared` under the probe's own per-candidate bound.
    fn note_pull_missed(&self) {
        let pulled = self.pulling.lock().unwrap().clone();
        let Some(TrackTarget::Addr(addr)) = pulled else {
            return;
        };
        let pinned = *self.resolved.lock().unwrap() == Some(addr)
            || *self.adopted.lock().unwrap() == Some(addr);
        if !pinned {
            // The miss was aimed at the configured slot — or at an
            // announced-contract source the verify paths handed out,
            // which carries its own bounded re-probe bookkeeping.
            return;
        }
        let release = {
            let mut run = self.pin_misses.lock().unwrap();
            let misses = match &mut *run {
                Some(run) if run.source == addr => {
                    run.misses += 1;
                    run.misses
                }
                _ => {
                    *run = Some(PinMiss {
                        source: addr,
                        misses: 1,
                    });
                    1
                }
            };
            if misses >= PIN_MISS_BUDGET {
                *run = None;
                true
            } else {
                false
            }
        };
        if release {
            // Every slot pinned to the dead address inherits the
            // verdict — the miss run measured the endpoint, not the
            // slot that named it.
            if *self.resolved.lock().unwrap() == Some(addr) {
                *self.resolved.lock().unwrap() = None;
            }
            if *self.adopted.lock().unwrap() == Some(addr) {
                *self.adopted.lock().unwrap() = None;
            }
            self.resolve_tracking_source();
        }
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
        let Shared { peer, recorder, .. } = &mut *shared;
        peer.self_promote()?;
        // One drain, one match: the failover walk's reported transition,
        // with the refusal a refused streak queues beside it.
        for event in peer.drain_pending() {
            match event {
                PeerEvent::RoleChange(change) => recorder.note_role_change(&change),
                PeerEvent::PromotionRefusal(refusal) => recorder.note_promotion_refused(refusal),
                // An armed gate that fired off a usurped verdict took
                // the field from the standing writer the pair had
                // diagnosed as outside its line — the audit record
                // naming the endpoint the claim came from.
                PeerEvent::ForeignClaimPreempt(preempt) => {
                    recorder.note_foreign_claim_preempted(preempt);
                }
                PeerEvent::Divergence(_)
                | PeerEvent::Resolution(_)
                | PeerEvent::Reinitialization(_)
                | PeerEvent::Orphan(_)
                | PeerEvent::FencingLoss(_)
                | PeerEvent::ClaimRearm(_)
                | PeerEvent::ClaimObservation(_)
                | PeerEvent::StartupRefusal(_)
                | PeerEvent::SourceRestart(_)
                | PeerEvent::SupersededCommand { .. }
                | PeerEvent::RehomedReceipt { .. }
                | PeerEvent::AdoptionReceipt(_) => {}
            }
        }
        let report = peer.report();
        self.store.sync_liveness(report.clone());
        Ok(report)
    }

    fn handle(&self, mut request: Request) {
        let method = request.method().clone();
        let url = request.url().to_string();
        let remote = request.remote_addr().copied();
        let (path, query) = url.split_once('?').unwrap_or((url.as_str(), ""));
        // The journal drain runs off the executor lock, so the durable
        // file trails the recording point by the writer's beat. An
        // answer that carries or follows journaled state — every
        // mutation's, and every journal read's — waits the standing
        // queue out first, on the request's own worker and never the
        // lock, so the answer attests the durable record caught up
        // through the request's effects. A sink stalled past the
        // bound answers degraded — its pushes are already fatal.
        let attests_durable =
            method == Method::Post || (method == Method::Get && path == "/journal");
        // The history file's drain is the same kind of off-lock sink:
        // a durable-history read that attests "the durable record
        // caught up through the last recorded scan" waits the standing
        // queue out on this worker first, the same shape the journal
        // read's wait takes. A sink stalled past the bound answers
        // degraded — its pushes are already fatal.
        let attests_history = method == Method::Get && path == "/history/durable";
        // The state-file ordinal an accepted `POST /command` pushed —
        // the `200` answers only after the durable file has caught up
        // through it, so a restart between admission and the applying
        // scan re-queues the carried receipt rather than losing the
        // command unaudited. The wait runs below, off the lock.
        let mut admission = None;
        // The deferred startup refusal's pairless disposition: a
        // `POST /scan` whose scan settled the pending born-active's
        // refused conditional grant on a run that declared no pair.
        // The request still answers the refusal — the durable-record
        // waits below run first — then the serve loop stands down so
        // the run's shell settles the latched verdict as the exit the
        // activation-time answer would already have taken.
        let mut terminal = false;
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
                // With several standbys every announcer stays a
                // candidate — the set is newest-first bounded — rather
                // than the last pull's hint silently evicting the rest.
                if let Some(announced) = checkpoint_peer(query, remote) {
                    let mut hints = self.announced.lock().unwrap();
                    hints.retain(|&hint| hint != announced);
                    hints.push_front(announced);
                    hints.truncate(MAX_ANNOUNCED);
                    // Mirror the newest accepted hint into the peer so
                    // the checkpoints it serves — and a `--state-file`
                    // persist — stamp it as `tracking_source`, naming
                    // this instance's successor to a later
                    // restart-as-active consult.
                    self.shared
                        .lock()
                        .unwrap()
                        .peer
                        .note_announced_source(announced);
                }
                let mut checkpoint = self.shared.lock().unwrap().peer.checkpoint();
                // Where this line's field ownership lives: a field
                // owner stamps itself; a non-owning serving run
                // propagates the owner the checkpoints it pulls
                // carried — so a peer several tracking hops out still
                // learns the real owner's address and an orphaned pair
                // can re-resolve onto it instead of tracking each
                // other forever.
                if checkpoint.source_owns_field == Some(true) {
                    checkpoint.line_owner = Some(self.local_addr());
                } else {
                    checkpoint.line_owner = *self.line_owner.lock().unwrap();
                }
                // The keyed attestation half of the contract: a
                // `?prove=<nonce>` pull on a keyed monitor gets the
                // served document's `line_proof` stamped over it —
                // the digest only a peer holding the pair's key can
                // produce — so the puller can tell this peer's
                // production from any endpoint replaying or
                // fabricating the line's checkpoints. Unkeyed
                // monitors answer every pull plain, as unkeyed
                // deployments always did.
                if let (Some(key), Some(nonce)) = (self.pair_key, checkpoint_prove(query)) {
                    checkpoint.line_proof = Some(line_proof(key, nonce, &checkpoint));
                }
                json(200, &checkpoint)
            }
            // The liveness reads serve the store's published mirror —
            // the control plane refreshes it under the executor lock
            // wherever the report or the scan stamp changes — so a
            // scan wedged in field I/O holds that lock without ever
            // stalling the answer that reports the wedge.
            (Method::Get, "/role") => match self.store.liveness() {
                Some(liveness) => json(200, &liveness.report),
                // Bind always seeds the mirror; a store without one
                // can only mean the monitor was never bound.
                None => json(503, "no liveness yet"),
            },
            // The container health contract's probe — the bounded
            // liveness answer: the listener's own declaration, the
            // served role, and the wall-clock age of the last
            // completed scan, all from the same published mirror —
            // never the executor lock — so the probe still answers
            // while wedged consumers starve the bulk reads and while
            // the wedged scan it reports holds that lock.
            (Method::Get, "/health") => match self.store.liveness() {
                Some(liveness) => json(
                    200,
                    &HealthReport {
                        live: true,
                        role: liveness.report.role,
                        tick: liveness.report.tick,
                        last_scan_age_ms: liveness
                            .last_scan
                            .map(|at| u64::try_from(at.elapsed().as_millis()).unwrap_or(u64::MAX)),
                        build: self.build.clone(),
                    },
                ),
                None => json(503, "no liveness yet"),
            },
            (Method::Get, "/history") => match history_query(query) {
                Ok((points, since)) => json(200, &self.store.history(&points, since)),
                Err(message) => json(400, &message),
            },
            (Method::Get, "/journal") => match journal_query(query) {
                Ok(since) => json(200, &self.store.journal(since)),
                Err(message) => json(400, &message),
            },
            // The durable process-history stream — the declared-duty
            // record's served window: entries the configured history
            // file recorded, `seq`-cursor read like the journal's,
            // `point` filtering to one declared point's series. The
            // pinned run-boundary and domain markers answer ahead of
            // the tail, so lifetimes and tick-domain seams stay
            // attributable under retention eviction; an evicted
            // stretch reads as a numbering gap.
            (Method::Get, "/history/durable") => match history_query(query) {
                Ok((points, since)) => json(
                    200,
                    &self
                        .store
                        .durable(since)
                        .into_iter()
                        .filter(|entry| match &entry.event {
                            dcs_core::DurableEvent::Sampled { point, .. } => {
                                points.is_empty() || points.contains(point)
                            }
                            _ => true,
                        })
                        .collect::<Vec<DurableEntry>>(),
                ),
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
            (Method::Post, "/promote") => match read_switch_submission(&mut request) {
                Ok(actor) => self.switchover(true, actor),
                Err(response) => response,
            },
            (Method::Post, "/demote") => match read_switch_submission(&mut request) {
                Ok(actor) => self.switchover(false, actor),
                Err(response) => response,
            },
            (Method::Post, "/command") => match read_command_submission(&mut request) {
                Ok(CommandEnvelope {
                    command,
                    actor,
                    reason,
                }) => {
                    let mut shared = self.shared.lock().unwrap();
                    let Shared { peer, recorder, .. } = &mut *shared;
                    // Only the settled-active peer accepts commands: on a
                    // standby or mid-transition instance the write gate
                    // would keep the write from the field, so refuse with
                    // a receipt rather than report a phantom application.
                    // Either way the declared actor and reason are
                    // stamped onto the receipt — the settled entry the
                    // journal echoes.
                    let receipt = if peer.accepts_commands() {
                        let receipt = peer.submit_command_attributed(command, actor, reason);
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
                        // run queues the just-captured checkpoint —
                        // receipt log included — to the sink at this
                        // boundary, under this same lock hold so the
                        // queue's order is the run's and no stale
                        // capture can overwrite it. The write itself
                        // drains on the sink's writer; the request
                        // attests the file caught up through this
                        // ordinal before its `200` answers — the wait
                        // below, off the lock.
                        if matches!(receipt.outcome, CommandOutcome::Accepted { .. }) {
                            admission = self.push_state_checkpoint(peer);
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
                            reason,
                            submission: None,
                        };
                        recorder.note_settled(None, receipt.clone(), peer.tick());
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
                // A batch past the declared bound is refused before
                // the first scan: an accepted batch is a bounded unit
                // of work inside its request — a request that cannot
                // end would hand one client the run's whole timeline
                // and pin its submission worker for as long as it
                // cared to. The refused request runs nothing; the
                // caller's larger advance composes of bounded
                // requests.
                Ok(body) if body.scans > MAX_SCANS_PER_REQUEST => json(
                    400,
                    &format!(
                        "refused: scans {} exceeds the per-request bound of \
                         {MAX_SCANS_PER_REQUEST}",
                        body.scans
                    ),
                ),
                Ok(body) => {
                    let mut failure = None;
                    // The last state-file checkpoint the batch pushed —
                    // the `200` attests the durable file caught up
                    // through it, so a restart after this answer
                    // resumes through every scan the batch ran.
                    let mut attested = None;
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
                        // the owns-field gate. The source resolution
                        // never spends an unproven `?peer=` hint: a
                        // peer demoted in place — the field claim's
                        // loss path, with no `POST /demote` boundary —
                        // verifies the recorded hints here under the
                        // same per-hint bound before any pull targets
                        // one.
                        if let Some(active) = self.verified_tracking_source() {
                            self.track_cycle(|| self.fetch_checkpoint(&active));
                        }
                        // Each scan takes the lock fresh and releases
                        // it at the boundary, so a request queued
                        // mid-batch — a command, a role poll — lands
                        // between scans instead of waiting the batch
                        // out.
                        let mut shared = self.shared.lock().unwrap();
                        scan_and_record(&mut shared, &self.store);
                        // A deferred startup grant's refusal settling
                        // inside this scan is the born-active
                        // contract's `Refused` verdict arriving
                        // mid-run, with no activation result to carry
                        // it. A run whose pair was declared keeps
                        // standing — the standby surface the pending
                        // stand-down reported is the rejoin the
                        // contract prescribes, and the drained verdict
                        // needs no further settle — while a pairless
                        // run has nothing to rejoin: the verdict stays
                        // latched for the shell, the batch ends on the
                        // refusal's answer, and the serve loop stands
                        // down for the run's exit.
                        if shared.peer.startup_refusal().is_some() {
                            if self.configured_source().is_some() {
                                shared.peer.drain_startup_refusal();
                            } else {
                                failure =
                                    shared.peer.startup_refusal().map(|error| error.to_string());
                                terminal = true;
                                break;
                            }
                        }
                        if let Some(after_scan) = &self.driven.after_scan
                            && let Err(error) = after_scan(&shared.peer)
                        {
                            failure = Some(error);
                            break;
                        }
                        // The scan cycle's end-of-cycle boundary —
                        // after the plant step, the same point the
                        // paced loop persists at: the checkpoint queues
                        // to the state sink under this lock hold, the
                        // write itself draining on the sink's writer.
                        attested = self.push_state_checkpoint(&shared.peer);
                    }
                    // The batch's `200` attests the file caught up
                    // through its last pushed checkpoint — FIFO makes
                    // that one ordinal cover every capture the batch
                    // queued. The wait is this worker's own, the lock
                    // long released; a failure the wait names fails
                    // the request rather than promising state the
                    // file never took.
                    if failure.is_none()
                        && let Some(ordinal) = attested
                        && let Err(error) = self
                            .state_sink
                            .as_ref()
                            .unwrap()
                            .attest(ordinal, STATE_DRAIN_WAIT)
                    {
                        failure = Some(error);
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
        // The admission-durability boundary: an accepted command's
        // checkpoint must have replaced the state file before its
        // receipt answers — the wait is this worker's own, the shared
        // lock long released. A wait that cannot attest — a recorded
        // write failure, a writer stalled past the bound — is the
        // run's fatal point: it dies naming the file rather than
        // answering a receipt it cannot recover, the same rule the
        // admission's synchronous write applied.
        if let Some(ordinal) = admission {
            self.state_sink
                .as_ref()
                .unwrap()
                .attest(ordinal, STATE_DRAIN_WAIT)
                .unwrap_or_else(|error| panic!("{error}"));
        }
        if attests_durable {
            self.store.wait_journal_drained(JOURNAL_DRAIN_WAIT);
        }
        if attests_history {
            self.store.wait_history_drained(JOURNAL_DRAIN_WAIT);
        }
        // A dropped client connection makes respond fail; the request is
        // already handled, so the error is ignored.
        let _ = request.respond(response);
        if terminal {
            self.shutdown();
        }
    }

    /// `POST /promote` (`promote` true) or `POST /demote` (`false`):
    /// applies the role change at the request's scan boundary and
    /// answers the post-change [`RoleReport`], or `409` with the named
    /// [`SwitchError`](dcs_core::SwitchError) on refusal. `actor` is the
    /// declared identity the request body carried — `None` for the bare
    /// request — stamped onto the journaled `RoleChanged` entries the
    /// switch queues: the request transition at the tick the peer
    /// attributes it to, and the settle transition when the first scan
    /// under the new mode completes. A refusal journals nothing.
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
    /// must carry the `?prove=` nonce's keyed `line_proof` — the
    /// announced-source contract is keyed-only, so an unkeyed run
    /// refuses `NoTrackingSource` whatever the hints could serve —
    /// and continue this run's line without being a replayable copy
    /// of this run's own document — a field-owning document not ahead
    /// of this run's tick is replayable, not a successor — or the
    /// demotion refuses
    /// `NoTrackingSource`. The verified adoption journals naming the
    /// source, ahead of the role change it enables.
    fn switchover(&self, promote: bool, actor: Option<String>) -> Response<Cursor<Vec<u8>>> {
        // The final-sync fetch runs outside the shared lock under the
        // dedicated pull bound — like the tracking pull it can wait on
        // an unreachable peer, and that wait must stall only this
        // request, never the lock's hold or the paced scan. The source
        // is the proven resolution — the announced tail of
        // `tracking_source` is a recorded hint no pull follows
        // unverified, here least of all: the fetched document's receipt
        // log is about to carry commands into the promoted run. A peer
        // already owning the field has no source to sync from — the
        // gate `Peer::final_sync` itself applies — so it fetches
        // nothing; the consume below re-applies the gate, discarding a
        // checkpoint fetched while a concurrent promotion landed.
        let pulled = match self.pull_source() {
            Some(source) if promote && !self.shared.lock().unwrap().peer.owns_field() => {
                Some(self.fetch_checkpoint(&source))
            }
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
        let Shared { peer, recorder, .. } = &mut *shared;
        let result = if promote {
            if let Some(pulled) = pulled {
                peer.final_sync(|| pulled);
                let tick = peer.tick();
                // The final sync's applied-checkpoint drain, one match:
                // the transfer's account in the producer's category
                // order, ahead of the role change the promotion below
                // walks.
                for event in peer.drain_pending() {
                    match event {
                        PeerEvent::Divergence(report) => {
                            recorder.note_divergence(report.tick, report.mismatches);
                        }
                        PeerEvent::Resolution(resolution) => recorder.note_resolution(resolution),
                        PeerEvent::Reinitialization(report) => recorder.note_reinitialized(report),
                        PeerEvent::Orphan(orphan) => recorder.note_field_orphaned(orphan),
                        PeerEvent::SourceRestart(restart) => recorder.note_source_restart(restart),
                        PeerEvent::SupersededCommand { index, receipt } => {
                            recorder.note_settled(Some(index), receipt, tick);
                        }
                        PeerEvent::RehomedReceipt {
                            prior,
                            index,
                            receipt,
                        } => recorder.note_rehomed(prior, index, receipt, tick),
                        PeerEvent::AdoptionReceipt(receipt) => {
                            recorder.note_settled(None, receipt, tick);
                        }
                        PeerEvent::FencingLoss(_)
                        | PeerEvent::ClaimRearm(_)
                        | PeerEvent::ClaimObservation(_)
                        | PeerEvent::StartupRefusal(_)
                        | PeerEvent::PromotionRefusal(_)
                        | PeerEvent::ForeignClaimPreempt(_)
                        | PeerEvent::RoleChange(_) => {}
                    }
                }
                self.store.sync_receipts(peer.receipts());
                self.store.sync_liveness(peer.report());
            }
            peer.promote_as(actor)
        } else if peer.owns_field() && self.tracking_target().is_none() {
            // A field owner with no tracking source — nothing
            // configured and no standby that announced itself — would
            // demote into a permanently unsynchronized standby that no
            // pull can ever reconverge; refuse up front rather than
            // silently marooning the instance.
            Err(SwitchError::NoTrackingSource)
        } else if peer.owns_field() && self.configured_source().is_none() {
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
            match verified {
                // The verified source must still be an announcer — a
                // re-announce that evicted it mid-verify un-verifies
                // the record, and the safe answer is refusal, never a
                // demotion toward an unproven endpoint.
                Some(source) if self.announced.lock().unwrap().contains(&source) => {
                    recorder.note_tracking_source(peer.tick(), source);
                    *self.adopted.lock().unwrap() = Some(source);
                    self.refused.lock().unwrap().clear();
                    peer.demote_as(actor)
                }
                _ => Err(SwitchError::NoTrackingSource),
            }
        } else {
            peer.demote_as(actor)
        };
        match result {
            Ok(()) => {
                // The switch's own drain, one match: the role walk the
                // promotion or demotion just reported.
                for event in peer.drain_pending() {
                    match event {
                        PeerEvent::RoleChange(change) => recorder.note_role_change(&change),
                        // The granted promotion took the field from a
                        // standing writer the pair had diagnosed as
                        // unable to prove its line's key — journaled
                        // beside the role transitions the request
                        // walked, so the record names both ends of the
                        // takeover.
                        PeerEvent::ForeignClaimPreempt(preempt) => {
                            recorder.note_foreign_claim_preempted(preempt);
                        }
                        PeerEvent::Divergence(_)
                        | PeerEvent::Resolution(_)
                        | PeerEvent::Reinitialization(_)
                        | PeerEvent::Orphan(_)
                        | PeerEvent::FencingLoss(_)
                        | PeerEvent::ClaimRearm(_)
                        | PeerEvent::ClaimObservation(_)
                        | PeerEvent::StartupRefusal(_)
                        | PeerEvent::SourceRestart(_)
                        | PeerEvent::PromotionRefusal(_)
                        | PeerEvent::SupersededCommand { .. }
                        | PeerEvent::RehomedReceipt { .. }
                        | PeerEvent::AdoptionReceipt(_) => {}
                    }
                }
                self.store.sync_liveness(peer.report());
                json(200, &peer.report())
            }
            Err(error) => json(409, &error),
        }
    }

    /// The explicitly configured tracking source — `Driven`'s `track`
    /// or [`with_standby_source`](Self::with_standby_source) — when
    /// set: operator-declared, so a demotion follows it without
    /// proving anything, a still-unresolved [`TrackTarget::Name`]
    /// included — the declared peer's name being down is the routine
    /// redundancy condition, and the demoted run's pulls retry it.
    /// The announced follow-peer hint is not one of
    /// these, and never satisfies the demotion guard on its own.
    fn configured_source(&self) -> Option<TrackTarget> {
        self.driven
            .track
            .map(TrackTarget::Addr)
            .or_else(|| self.standby_source.clone())
    }

    /// The pair key a pull toward `source` must prove under — `Some`
    /// when `source` is the endpoint a verified announced demotion
    /// adopted, the endpoint the orphan-resolution probe verified, or
    /// a recorded announced hint a non-owner's pulls can resolve to
    /// (a demotion that never ran the verify, like the fencing
    /// demote), and this run is keyed: the demotion or probe proved
    /// the endpoint once, but every checkpoint it serves afterward
    /// must keep proving it came from a key-holding peer of this
    /// line, so an announced endpoint that only replays or
    /// fabricates this line's documents feeds the tracking peer
    /// nothing. `None` for a configured source
    /// — operator-declared, no proof owed — and whenever the run is
    /// unkeyed, where no announced address ever resolves as a source.
    pub fn pull_proof_key(&self, source: &TrackTarget) -> Option<u64> {
        let key = self.pair_key?;
        let TrackTarget::Addr(addr) = source else {
            // A configured name is operator-declared like the address
            // it resolves to: the announced contract's proof does not
            // apply to it.
            return None;
        };
        if *self.adopted.lock().unwrap() == Some(*addr)
            || *self.resolved.lock().unwrap() == Some(*addr)
            || self.announced.lock().unwrap().contains(addr)
        {
            Some(key)
        } else {
            None
        }
    }

    /// Whether `pulled` satisfies the proof `nonce` demands of it —
    /// `true` whenever the pull carried no nonce (the unkeyed shape),
    /// and for a keyed pull only when the document's `line_proof` is
    /// the keyed digest binding that nonce to that document, which
    /// only a peer holding the pair's key could have produced.
    fn proven(&self, pulled: &Checkpoint, nonce: Option<u64>) -> bool {
        match (self.pair_key, nonce) {
            (Some(key), Some(nonce)) => pulled.line_proof == Some(line_proof(key, nonce, pulled)),
            _ => true,
        }
    }

    /// One checkpoint fetched from `source` the tracking pull's way —
    /// announcing this monitor's own address on it, and when `source`
    /// is an announced-contract endpoint — the adopted source, a
    /// probe-resolved owner, or a bare hint a keyed non-owner's pulls
    /// resolve to — also carrying a fresh
    /// `?prove=` nonce whose keyed `line_proof` the returned document
    /// must match, or the fetch fails like any refused pull.
    fn fetch_checkpoint(&self, source: &TrackTarget) -> Result<Checkpoint, String> {
        // A configured name resolves inside this fetch — the same
        // bounded pull path an address takes, so an unresolvable name
        // lands as the fetch failure it is rather than a startup
        // fault: the cycle counts the miss and the next pull retries.
        let addr = source
            .resolve()
            .map_err(|detail| format!("fetch from {source}: {detail}"))?;
        let client = MonitorClient::with_timeout(addr, CHECKPOINT_PULL_TIMEOUT);
        let nonce = self.pull_proof_key(source).map(|_| mint_generation());
        let pulled = client
            .checkpoint_tracking(Some(self.local_addr()), nonce)
            .map_err(|error| format!("fetch from {source}: {error}"))?;
        if !self.proven(&pulled, nonce) {
            return Err(format!(
                "fetch from {source}: checkpoint carried no valid line proof"
            ));
        }
        Ok(pulled)
    }

    /// Proves the announced follow-peer hints a demotion would follow,
    /// or refuses the demotion — the `?peer=` hardening: the serving
    /// side cannot tell the puller's monitor port from any other
    /// same-IP port the connection claims, so a recorded hint is
    /// unverified until one checkpoint pulled from it proves it
    /// continues this run's line. The contract is keyed-only:
    /// `/checkpoint` is public and unauthenticated, so on an unkeyed
    /// run every document shape an endpoint could serve — a replayed
    /// owner document or the standby's `source_owns_field: false`
    /// alike — is derivable from this run's own answers and attests
    /// nothing about who serves it; an announced-only demotion there
    /// refuses `NoTrackingSource` outright. On a keyed run, with
    /// several standbys every announcer is a candidate: a hint serving
    /// this line *as its field owner* wins — the strictly-ahead owner
    /// document carrying the pull's keyed `line_proof`; an owner
    /// document not ahead of this run's tick is the replayable
    /// own-document shape and refuses, since this run's own public
    /// `/checkpoint` serves that exact stamp — while
    /// an announcer that only tracks this run, a sibling standby
    /// replaying this run's own line back, is remembered as the
    /// provisional fallback, adopted only when no owner verified, so
    /// the orphan-resolution probe can still re-resolve onto an owner
    /// that appears later. Returns the verified hint, or `None` when
    /// the demotion needs no proof — a configured source covers it,
    /// or the peer owns no field — or the `409 NoTrackingSource`
    /// refusal when the owner has only unproven hints: nothing
    /// announced, unreachable hints, hints answering no
    /// valid `line_proof` — replaying this run's own checkpoint
    /// or fabricating one that merely continues the line produces
    /// none — or checkpoints that are not this
    /// run's continuation. A standby-shaped document additionally
    /// answers to this
    /// run's command audit: a field owner holds the line's receipt
    /// log and held-value image itself, so a document whose receipt
    /// window forks that log or whose internal `In` samples plant a
    /// value no settled verdict produced is forged rather than a
    /// continuation, and loses to the next candidate the same way.
    fn verify_demote_hint(&self) -> Result<Option<SocketAddr>, Response<Cursor<Vec<u8>>>> {
        if self.configured_source().is_some() {
            return Ok(None);
        }
        if !self.shared.lock().unwrap().peer.owns_field() {
            return Ok(None);
        }
        // An unkeyed run can prove nothing about any hinted endpoint —
        // every document shape one might serve is derivable from this
        // run's public `/checkpoint`, the standby's
        // `source_owns_field: false` included — so no hint can ever
        // arm a demotion.
        if self.pair_key.is_none() {
            return Err(json(409, &SwitchError::NoTrackingSource));
        }
        let hints: Vec<SocketAddr> = self.announced.lock().unwrap().iter().copied().collect();
        if hints.is_empty() {
            return Err(json(409, &SwitchError::NoTrackingSource));
        }
        let own = self.shared.lock().unwrap().peer.checkpoint();
        match self.probe_announced_hints(&hints, &own) {
            Some(hint) => Ok(Some(hint)),
            None => Err(json(409, &SwitchError::NoTrackingSource)),
        }
    }

    /// One bounded checkpoint pull against each announced hint — the
    /// shared prove-half of `POST /demote`'s verification
    /// ([`verify_demote_hint`](Self::verify_demote_hint)) and the
    /// involuntary path's
    /// [`adopt_announced_source`](Self::adopt_announced_source). The
    /// announced-source contract is keyed-only outright: an unkeyed
    /// run can prove nothing about any hinted endpoint, so no hint
    /// earns a pull there.
    ///
    /// Newest announcer first, one bounded pull each under
    /// [`CHECKPOINT_PULL_TIMEOUT`]. Every verify pull demands the
    /// proof only a key-holding peer of this line can produce: a
    /// fresh nonce whose returned document must carry the keyed
    /// `line_proof` binding that nonce to that document — the
    /// attestation the document checks run on. A candidate serving
    /// the line as field owner wins outright — every pull reaching
    /// the document checks is attested, so a strictly-ahead owner
    /// document is a real successor's while one at or behind this
    /// run's tick is replayable and refuses. A candidate that only
    /// tracks the line is remembered as the fallback — adopted
    /// provisional, since the orphan-resolution probe can still
    /// re-resolve onto the owner the line later names. And every
    /// candidate answers this run's command audit: a document whose
    /// receipt window forks the settled log or whose internal `In`
    /// samples plant a value no settled verdict produced is forged,
    /// not a continuation, and loses to the next candidate. `None`
    /// when no hint proves out — dead, unreachable, replayed, or
    /// foreign endpoints all lose the same way.
    fn probe_announced_hints(&self, hints: &[SocketAddr], own: &Checkpoint) -> Option<SocketAddr> {
        // The verify pull demands the proof only a key-holding peer of
        // this line can produce — an unkeyed run cannot authenticate
        // any hinted endpoint, so no hint earns a pull there.
        self.pair_key?;
        let nonce = Some(mint_generation());
        let mut tracked = None;
        for &hint in hints {
            // A hint naming this monitor could only ever serve this
            // run's own document back — never a successor — and on
            // the involuntary path the demoted run's own standby
            // document would pass the continuation checks, wedging
            // the peer onto pulling itself. The refusal journals once
            // per signature like the rest — a hint that can never
            // prove succession is the same auditable refusal.
            if hint == self.local_addr() {
                self.note_source_refusal(hint, "the hint names this run's own monitor".to_string());
                continue;
            }
            let pulled = match MonitorClient::with_timeout(hint, CHECKPOINT_PULL_TIMEOUT)
                .checkpoint_tracking(None, nonce)
            {
                Ok(pulled) => pulled,
                // An unanswered pull refuses the adoption like any
                // served refusal — journaled once per signature, so a
                // set of dead hints strands auditably, not silently.
                Err(error) => {
                    self.note_source_refusal(
                        hint,
                        format!("the verify pull produced no checkpoint: {error}"),
                    );
                    continue;
                }
            };
            // Every refusal class journals once per signature: the
            // hinted endpoint that answers but cannot prove this
            // line's continuation is the strand the announced contract
            // exists to refuse — and it refuses auditably.
            if !self.proven(&pulled, nonce) {
                self.note_source_refusal(
                    hint,
                    "the pull's line proof did not verify under the pair key".to_string(),
                );
                continue;
            }
            if let Err(reason) = verify_announced_checkpoint(&pulled, own) {
                self.note_source_refusal(hint, reason.to_string());
                continue;
            }
            // `unaccounted` runs under the shared lock, so its answer
            // binds and releases before the refusal journals — the
            // note takes `shared` itself.
            let unaccounted = self.shared.lock().unwrap().peer.unaccounted(&pulled);
            if let Some(detail) = unaccounted {
                self.note_source_refusal(hint, detail.to_string());
                continue;
            }
            if pulled.source_owns_field == Some(true) {
                return Some(hint);
            }
            tracked.get_or_insert(hint);
        }
        tracked
    }

    /// The involuntary-demotion half of the announced-source contract
    /// — the QA finding
    /// `involuntary-demote-unverified-announced-hint`: `POST /demote`
    /// proves the recorded hints before demoting
    /// ([`verify_demote_hint`](Self::verify_demote_hint)), but a field
    /// claim's mid-run loss — `Peer::demote` on a fenced write — and
    /// every other non-request demotion cross the same boundary with
    /// nothing to hang the verification on. The tracking path runs it
    /// lazily here instead: while the peer owns no field and no
    /// proven source stands, the recorded hints get one bounded pull
    /// each under the demote verify's checks, and a candidate proving
    /// it serves this run's continuation pins into `adopted` and
    /// journals the adoption exactly like the request path — so a
    /// foreign monitor that announced itself onto a launched active
    /// can never strand the demoted peer pulling it, and the
    /// legitimate successor's hint wins the pass on its own proof.
    /// The announced contract is keyed-only outright — an unkeyed run
    /// can prove nothing about any hinted endpoint, so no hint earns
    /// even a verify pull there. When no keyed hint proves a
    /// continuation, the recorded set still
    /// gets the orphan-resolution probe's owner check
    /// ([`resolve_tracking_source`](Self::resolve_tracking_source)):
    /// a successor that already claimed the field but ticks at or
    /// behind this run serves a document the demote verify refuses —
    /// the owner-stamp shape that cannot prove succession — while the
    /// owner check proves it serves this line's field, so the
    /// promoted legitimate successor resolves where a dead or foreign
    /// endpoint cannot. Every hint failing both passes leaves the
    /// peer sourceless — the same answer `POST /demote` gives an
    /// unproven hint — rather than following one verbatim, and the
    /// failed set is remembered so a later cycle re-probes only a
    /// changed set, or the same one after
    /// [`ANNOUNCED_VERIFY_RETRY`]: a dead recorded hint falls back
    /// inside one bounded pass and earns a fresh probe inside a
    /// bounded window. Runs outside the shared lock under
    /// [`CHECKPOINT_PULL_TIMEOUT`] per hint — the stall bound is the
    /// verify pass, never a hint's own patience.
    fn adopt_announced_source(&self) -> Option<SocketAddr> {
        // Keyed-only, exactly like `verify_demote_hint`: on an
        // unkeyed run every document shape is derivable from this
        // run's public `/checkpoint`, so no announced endpoint can
        // ever prove itself — a bare hint is no tracking source.
        if self.pair_key.is_none() || self.shared.lock().unwrap().peer.owns_field() {
            return None;
        }
        let hints: Vec<SocketAddr> = self.announced.lock().unwrap().iter().copied().collect();
        if hints.is_empty() {
            return None;
        }
        {
            let last = self.announced_verify.lock().unwrap();
            if let Some(last) = &*last
                && last.hints == hints
                && last.at.elapsed() < ANNOUNCED_VERIFY_RETRY
            {
                return None;
            }
        }
        let own = self.shared.lock().unwrap().peer.checkpoint();
        let verified = self.probe_announced_hints(&hints, &own);
        *self.announced_verify.lock().unwrap() = Some(AnnouncedVerify {
            hints,
            at: Instant::now(),
        });
        // Pin only a still-recorded hint — a re-announce that evicted
        // it mid-verify un-verifies the record, the same guard the
        // request path applies — and only while nothing proven stands:
        // a promotion or orphan resolution landing mid-verify already
        // answered where the pulls go.
        if let Some(source) =
            verified.filter(|source| self.announced.lock().unwrap().contains(source))
        {
            let mut shared = self.shared.lock().unwrap();
            if shared.peer.owns_field() || self.pull_source().is_some() {
                return None;
            }
            let Shared { peer, recorder, .. } = &mut *shared;
            recorder.note_tracking_source(peer.tick(), source);
            drop(shared);
            *self.adopted.lock().unwrap() = Some(source);
            self.refused.lock().unwrap().clear();
            return Some(source);
        }
        // No hint proved this run's continuation — but a successor
        // that already claimed the field while ticking at or behind
        // this run serves a document the demote verify must refuse:
        // the owner-stamp shape cannot prove succession there. The
        // orphan-resolution probe's owner check is the scrutiny such
        // a document can pass — the same one the post-demotion orphan
        // cycle applies to these hints — so a promoted legitimate
        // successor still earns the pulls while a dead or foreign
        // endpoint cannot.
        self.resolve_tracking_source()
    }

    /// Diagnoses the field's standing writer against this line's pair
    /// key — the one question no other path on this surface can ask,
    /// and the one the pair's own recovery turns on. The field's own
    /// arbitration names the writer ([`Peer::claimed_monitor`], the
    /// endpoint the standing claim declared, refreshed by every claim
    /// verdict this run's attachments meet); this asks *that endpoint*
    /// for a checkpoint under a fresh `?prove=` nonce and reads the
    /// answer. A member of this keyed line proves its line; a process
    /// outside it cannot, whatever it serves.
    ///
    /// The verdict has exactly one shape that earns the diagnosis: the
    /// endpoint **answered** and its document carried no valid
    /// `line_proof`. Silence is not a verdict — a dead or frozen
    /// writer is no proof of anything, and a probe that cannot reach it
    /// leaves the run on the orphan verdict's conditional claim, where
    /// the field's own arbitration decides. An endpoint that proves
    /// the key is a legitimate pair member whatever the field says,
    /// so the diagnosis clears. Suppressed inside the
    /// [`ANNOUNCED_VERIFY_RETRY`] window on an unchanged declaration,
    /// the same bound every other verify path here runs under, so a
    /// hostile or dead endpoint costs one bounded pull per window
    /// rather than one per scan.
    ///
    /// Keyed-only, and only while this run owns no field: an unkeyed
    /// run holds no key to ask under, and a field owner has no writer
    /// outside its line. Every case where *no writer can be asked* is
    /// [`WriterDiagnosis::Absent`] rather than a shrug — an unkeyed run,
    /// a field owner, a claim declaring no monitor (the field
    /// arbitrating no writer at all, which a released foreign claim
    /// leaves standing), a declaration naming this run's own monitor or
    /// an undialable wildcard bind. All of those are the plain
    /// [`StandbySync::Orphaned`] verdict with its conditional claim, and
    /// saying so is what keeps the narrower verdict honest about a
    /// writer that is no longer there.
    ///
    /// A diagnosed writer journals its own refusal like every other
    /// probe here ([`TrackingSourceRefused`](dcs_core::JournalEvent::TrackingSourceRefused)),
    /// so the strand's cause is durable audit, and the peer's report
    /// then names it as [`StandbySync::Usurped`] rather than
    /// [`StandbySync::Orphaned`].
    fn diagnose_foreign_writer(&self) -> WriterDiagnosis {
        // Keyed-only: there is no line key to ask a candidate under on
        // an unkeyed run, and an unkeyed run's peers never claimed a
        // line in the first place.
        if self.pair_key.is_none() {
            return WriterDiagnosis::Absent;
        }
        let claimed = {
            let shared = self.shared.lock().unwrap();
            if shared.peer.owns_field() {
                return WriterDiagnosis::Absent;
            }
            // The field named no endpoint for its claim — an unclaimed
            // field, a tool's claim, or a build predating the
            // declaration. There is no writer to be outside the line,
            // so the verdict is the ownerless one: the narrower reading
            // is not a shrug that survives the writer's departure.
            match shared.peer.claimed_monitor() {
                Some(claimed) => claimed,
                None => return WriterDiagnosis::Absent,
            }
        };
        // A verdict naming this run's own monitor names no other
        // writer, and an undialable bind address is no endpoint at
        // all — the same two refusals the adoption paths apply, and the
        // same plain ownerless verdict they leave behind.
        if claimed == self.local_addr() || claimed.ip().is_unspecified() {
            return WriterDiagnosis::Absent;
        }
        {
            // A pass inside its window stands as the verdict: the
            // suppression bounds the *pull*, never the diagnosis, and a
            // cleared answer keeps clearing until a fresh pass says
            // otherwise.
            let last = self.foreign_verify.lock().unwrap();
            if let Some(last) = &*last
                && last.monitor == claimed
                && last.at.elapsed() < ANNOUNCED_VERIFY_RETRY
            {
                return if last.foreign {
                    WriterDiagnosis::Foreign(claimed)
                } else {
                    WriterDiagnosis::Absent
                };
            }
        }
        let nonce = Some(mint_generation());
        // The whole question in one bounded ask: an endpoint that
        // answers without the key is outside this line, and one that
        // does not answer at all is unknown.
        let foreign = match MonitorClient::with_timeout(claimed, CHECKPOINT_PULL_TIMEOUT)
            .checkpoint_tracking(None, nonce)
        {
            Ok(pulled) => !self.proven(&pulled, nonce),
            Err(_) => false,
        };
        *self.foreign_verify.lock().unwrap() = Some(ForeignVerify {
            monitor: claimed,
            at: Instant::now(),
            foreign,
        });
        if !foreign {
            return WriterDiagnosis::Absent;
        }
        self.note_source_refusal(
            claimed,
            "the field's standing writer cannot prove this line's pair key".to_string(),
        );
        WriterDiagnosis::Foreign(claimed)
    }

    /// The field-arbitrated successor half of the tracking-source
    /// contract — the answer to the QA finding
    /// `demoted-peer-strands-unsynchronized`. A demoted peer whose run
    /// is unkeyed can prove no announced hint — every document shape
    /// an endpoint could serve is derivable from this run's own
    /// public `/checkpoint` — so without this it stays
    /// `standby`/`unsynchronized` forever and the pair's redundancy
    /// silently ends. The field's write-ownership claim is the
    /// rendezvous the announced contract was never meant to be: the
    /// fencing verdicts the claim produces carry the monitor endpoint
    /// the claim's owner *declared* — where it serves the tracking
    /// surface — so the demoted peer learns the successor's address
    /// from the same arbitration that demoted it, and only actually
    /// holding the claim could have put a monitor under it.
    ///
    /// The declaration is a candidate, never a pull target on its
    /// own: the one bounded pull it earns must serve this run's line
    /// *as its field owner* — the orphan-resolution probe's owner
    /// check, whose `source_owns_field` stamp a standby's document
    /// cannot produce — plus, on a keyed run, the pull's
    /// `line_proof`. A passing endpoint pins into `adopted` and
    /// journals the adoption exactly like a verified announced
    /// source; one that cannot prove ownership — a stale verdict
    /// naming a demoted peer, a dead monitor, a foreign claim's
    /// undeclared endpoint — refuses, and the recorded pass re-arms
    /// only when the field's verdict names a changed monitor or
    /// [`ANNOUNCED_VERIFY_RETRY`] elapses, so a dead declared
    /// endpoint costs one bounded pull per window rather than one
    /// per scan. Runs outside `shared` under
    /// [`CHECKPOINT_PULL_TIMEOUT`].
    fn adopt_claimed_source(&self) -> Option<SocketAddr> {
        let (claimed, own) = {
            let shared = self.shared.lock().unwrap();
            if shared.peer.owns_field() {
                return None;
            }
            (shared.peer.claimed_monitor()?, shared.peer.checkpoint())
        };
        // The verdict could name this monitor itself — a stale
        // record the field supplanted with a fresher claim would
        // only ever serve this run's own standby document back,
        // which can never prove succession. The self-name journals
        // like a served refusal: a verdict that never updates is the
        // same permanent strand, and it must not sit silent.
        if claimed == self.local_addr() {
            self.note_source_refusal(
                claimed,
                "the standing claim declares this run's own monitor".to_string(),
            );
            return None;
        }
        // A wildcard declaration is a bind address, not a dialable
        // endpoint — every peer resolving it dials its own loopback
        // (the QA finding `claimed-monitor-wildcard-undialable`). The
        // field's arbitration is the one authority that can
        // substitute — the claiming connection's proven source — so
        // a verdict still naming the wildcard names no successor this
        // side can recover; like an undeclared claim it earns no
        // pull, and it spends no retry window so the first routable
        // verdict verifies on the next pass.
        if claimed.ip().is_unspecified() {
            self.note_source_refusal(
                claimed,
                "the claim's declared monitor is undialable".to_string(),
            );
            return None;
        }
        {
            let last = self.claimed_verify.lock().unwrap();
            if let Some(last) = &*last
                && last.monitor == claimed
                && last.at.elapsed() < ANNOUNCED_VERIFY_RETRY
            {
                return None;
            }
        }
        *self.claimed_verify.lock().unwrap() = Some(ClaimedVerify {
            monitor: claimed,
            at: Instant::now(),
        });
        let nonce = self.pair_key.map(|_| mint_generation());
        // A declared monitor that does not answer refuses the
        // adoption as surely as one the checks refuse — journaled
        // once per signature, so a dead claimed endpoint leaves the
        // strand auditable rather than silent.
        let pulled = match MonitorClient::with_timeout(claimed, CHECKPOINT_PULL_TIMEOUT)
            .checkpoint_tracking(None, nonce)
        {
            Ok(pulled) => pulled,
            Err(error) => {
                self.note_source_refusal(
                    claimed,
                    format!("the claim's declared monitor answered no checkpoint: {error}"),
                );
                return None;
            }
        };
        // A serving endpoint the line-membership or proof checks
        // refuse is the strand the adoption exists to close, so the
        // refusal journals once per signature — a permanently
        // unacceptable successor is durable audit, never journal
        // silence.
        if let Err(reason) = verify_owner_checkpoint(&pulled, &own) {
            self.note_source_refusal(claimed, reason.to_string());
            return None;
        }
        if !self.proven(&pulled, nonce) {
            self.note_source_refusal(
                claimed,
                "the pull's line proof did not verify under the pair key".to_string(),
            );
            return None;
        }
        // Pin only while nothing proven stands — a promotion or an
        // orphan resolution landing mid-pull already answered where
        // the pulls go — and journal the adoption exactly like the
        // announced path does.
        let mut shared = self.shared.lock().unwrap();
        if shared.peer.owns_field() || self.pull_source().is_some() {
            return None;
        }
        let Shared { peer, recorder, .. } = &mut *shared;
        recorder.note_tracking_source(peer.tick(), claimed);
        drop(shared);
        *self.adopted.lock().unwrap() = Some(claimed);
        self.refused.lock().unwrap().clear();
        Some(claimed)
    }

    /// Re-resolves the tracking source while the tracked line reports
    /// no field owner — the orphan half of the tracking-source
    /// contract: `StandbySync::Orphaned` proves only that the tracked
    /// run is not the owner, not that the line has none. The line's
    /// own `line_owner` propagation names where ownership lives, so a
    /// demoted peer pinned onto a sibling standby — two runs
    /// orphaned-tracking each other while a third owns the field —
    /// follows the name onward instead of wedging forever. The probe
    /// pulls one checkpoint from the named owner and accepts it only
    /// when it verifiably serves this line *as its field owner* — on
    /// a keyed run, with the pull's `line_proof` — before re-targeting
    /// the pulls through the `resolved` slot and journaling the
    /// adoption exactly like the announced- and claimed-source pins:
    /// the resolved slot outranks the configured source they and it
    /// share, so the durable audit records where the orphaned peer
    /// moved its pulls. A candidate that cannot prove ownership clears
    /// any prior resolution rather than pinning a guess. Returns the
    /// tracking target after resolution, or `None` when the line named
    /// no routable owner and nothing resolves.
    ///
    /// The probe runs on the scan thread once per orphaned apply, so a
    /// pass that proves nothing is remembered in `resolve_verify`: a
    /// later cycle re-probes only when the candidate set changed or
    /// [`ANNOUNCED_VERIFY_RETRY`] elapsed — the same bound
    /// [`adopt_claimed_source`](Self::adopt_claimed_source) applies to
    /// a dead declared endpoint — so a foreign claim's undialable
    /// declared monitor costs one bounded pull burst per window rather
    /// than one per scan (the QA finding
    /// `orphan-resolution-dead-claimed-monitor-stalls-scan`).
    pub fn resolve_tracking_source(&self) -> Option<SocketAddr> {
        let (own, claimed) = {
            let shared = self.shared.lock().unwrap();
            if shared.peer.owns_field() {
                return None;
            }
            (shared.peer.checkpoint(), shared.peer.claimed_monitor())
        };
        let mut candidates: Vec<SocketAddr> = Vec::new();
        // The field's own word for its claim holder leads — the
        // monitor endpoint the standing claim declares, vouched for
        // by the arbitration itself rather than announced — then the
        // line's own word for its owner: a wildcard stamp
        // dials through the tracked source's IP, the monitor that
        // served it; then the tracked source itself — a standby may
        // simply not have propagated the stamp yet — then the recorded
        // announcers, any of which may already own the field.
        // A wildcard claim declaration earns no candidate at all:
        // undialable like the verdict `adopt_claimed_source` refuses,
        // and unlike the `line_owner` stamp it has no serving monitor
        // whose proven source could substitute.
        if let Some(monitor) = claimed.filter(|monitor| !monitor.ip().is_unspecified()) {
            candidates.push(monitor);
        }
        let tracked = self
            .driven
            .track
            .map(TrackTarget::Addr)
            .or_else(|| self.standby_source.clone())
            .or_else(|| self.adopted.lock().unwrap().map(TrackTarget::Addr))
            .or_else(|| {
                // The bare announced hint resolves only under the
                // keyed contract, exactly as in `tracking_source`.
                self.pair_key
                    .and_then(|_| self.announced.lock().unwrap().front().copied())
                    .map(TrackTarget::Addr)
            });
        // A configured source still waiting on DNS offers no address
        // to probe: it contributes no owner fallback and no candidate
        // of its own while unresolvable — the pull path keeps retrying
        // it regardless — and the probe's other candidates stand.
        let tracked_addr = tracked.and_then(|target| target.resolve().ok());
        if let Some(owner) = *self.line_owner.lock().unwrap() {
            let fallback = tracked_addr.map(|source| source.ip());
            if let Some(owner) = routable_addr(owner, fallback) {
                candidates.push(owner);
            }
        }
        if let Some(source) = tracked_addr
            && !candidates.contains(&source)
        {
            candidates.push(source);
        }
        // The unverified announcer set joins the probe only under the
        // keyed contract — an unkeyed run cannot authenticate any of
        // them, so they are never candidates there.
        let mut extra: Vec<SocketAddr> = if self.pair_key.is_some() {
            self.announced.lock().unwrap().iter().copied().collect()
        } else {
            Vec::new()
        };
        extra.retain(|hint| !candidates.contains(hint) && Some(*hint) != tracked_addr);
        candidates.extend(extra);
        // A candidate set a probe pass already refused earns a fresh
        // pass only when the set changed — a fresh claim declaration,
        // a moved owner stamp, a new or reordered announcer — or once
        // [`ANNOUNCED_VERIFY_RETRY`] ages the record out: every
        // candidate's bounded pull is paid once per window rather than
        // once per orphaned scan. The recorded pass already cleared
        // `resolved`, and this probe is its only writer, so the cached
        // verdict still carries the line's no-owner answer.
        {
            let last = self.resolve_verify.lock().unwrap();
            if let Some(last) = &*last
                && last.candidates == candidates
                && last.at.elapsed() < ANNOUNCED_VERIFY_RETRY
            {
                return None;
            }
        }
        let nonce = self.pair_key.map(|_| mint_generation());
        for &candidate in &candidates {
            // A candidate naming this run's own monitor can never
            // prove succession — journaled once per signature like
            // the served refusals.
            if candidate == self.local_addr() {
                self.note_source_refusal(
                    candidate,
                    "the candidate names this run's own monitor".to_string(),
                );
                continue;
            }
            let pulled = match MonitorClient::with_timeout(candidate, CHECKPOINT_PULL_TIMEOUT)
                .checkpoint_tracking(None, nonce)
            {
                Ok(pulled) => pulled,
                // The unanswered candidate is the same refusal class
                // as a refused document — journaled once per
                // signature, so a strand on dead endpoints audits.
                Err(error) => {
                    self.note_source_refusal(
                        candidate,
                        format!("the resolution pull produced no checkpoint: {error}"),
                    );
                    continue;
                }
            };
            match verify_owner_checkpoint(&pulled, &own) {
                // A refused candidate re-probes on every orphaned
                // cycle while it stands — the strand that names no
                // endpoint is the silent one, so each distinct
                // refusal journals once.
                Err(reason) => {
                    self.note_source_refusal(candidate, reason.to_string());
                    continue;
                }
                Ok(()) if !self.proven(&pulled, nonce) => {
                    self.note_source_refusal(
                        candidate,
                        "the pull's line proof did not verify under the pair key".to_string(),
                    );
                    continue;
                }
                _ => {}
            }
            // Pin only while the peer still owns no field — a
            // promotion landing mid-probe already answered where
            // the pulls go — and journal the re-target exactly
            // like the announced- and claimed-source adoptions:
            // the resolved pin carries the same pull-target
            // authority, outranking the configured tracking
            // source, so the durable audit records where the
            // orphaned peer moved its pulls.
            let mut shared = self.shared.lock().unwrap();
            if shared.peer.owns_field() {
                return None;
            }
            let Shared { peer, recorder, .. } = &mut *shared;
            recorder.note_tracking_source(peer.tick(), candidate);
            drop(shared);
            // The set proved an owner — the failure record no
            // longer describes it, so a later orphaned cycle on
            // the same candidates probes fresh.
            *self.resolve_verify.lock().unwrap() = None;
            *self.resolved.lock().unwrap() = Some(candidate);
            self.refused.lock().unwrap().clear();
            return Some(candidate);
        }
        *self.resolve_verify.lock().unwrap() = Some(ResolveVerify {
            candidates,
            at: Instant::now(),
        });
        *self.resolved.lock().unwrap() = None;
        None
    }
}

/// The dialable form of an address a checkpoint propagated — a
/// `line_owner` stamp may name a wildcard-bound listener's `0.0.0.0`
/// or `::` address, which no peer can dial; the monitor that served
/// the stamp is reachable at the tracked source's IP, so it
/// substitutes, exactly like a `?peer=` wildcard resolving to its
/// connection's source. `None` for a wildcard with no routable
/// fallback.
fn routable_addr(addr: SocketAddr, fallback: Option<IpAddr>) -> Option<SocketAddr> {
    if !addr.ip().is_unspecified() {
        return Some(addr);
    }
    fallback.map(|ip| SocketAddr::new(ip, addr.port()))
}

/// Whether the request is a `POST /command` — the receipted ingress
/// path. [`Monitor::serve`] routes these ahead of [`submission`] onto
/// the dedicated command lane and its overflow deck: they still
/// qualify there (the handler reads a body, and a stalled one pins
/// that lane's single worker), but their queue bound and drain order
/// belong to the bounded admission contract, not the generic
/// submission pool.
fn command_request(request: &Request) -> bool {
    request.method() == &Method::Post && request.url().split('?').next() == Some("/command")
}

/// Whether the request can hold a worker on a client-paced wait —
/// the routing [`Monitor::serve`] applies after the switchover POSTs
/// split off through [`role_change`] (a switch request still owing
/// client body bytes lands here too, on the quarantine side of its
/// split). Two request shapes can:
///
/// - `POST /command` and `POST /scan`, the only body-reading handlers
///   left on this routing step — `POST /promote` and `POST /demote`
///   already routed — plus tiny_http hands them the body still on the
///   socket whenever it passed the eager buffer, and a client that
///   stalls mid-body holds the reader as long as it cares to.
/// - Any request still owing the client body bytes, whatever its
///   path: a declared `Content-Length` past the library's small eager
///   buffer leaves the remainder on a live reader whose drop drains
///   the rest — the same unbounded wait as the read — and a chunked
///   body reads off the socket the same way.
///
/// Everything else — plain GETs and the bounded control-plane POSTs,
/// including a `GET /role` whose client never promised a body — is
/// answered without ever waiting on the client and belongs on the
/// heartbeat, control, or serving lane.
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

/// Whether the request is a pair-liveness read — [`Monitor::serve`]'s
/// second routing step, after [`submission`]. `GET /checkpoint` is the
/// heartbeat a tracking standby measures the active's liveness by,
/// `GET /role` is the verdict the pair view and a failover decision
/// read, and `GET /health` is the container health check's probe;
/// `/role` and `/health` answer from the store's published liveness
/// mirror and `/checkpoint` from the shared lock, none of them on a
/// network wait, so they take the dedicated heartbeat lane — the one
/// pool a wedged response write on a bulk read can never pin. The
/// query string is ignored (`/checkpoint?peer=` is the same pull).
fn pair_liveness(request: &Request) -> bool {
    request.method() == &Method::Get
        && matches!(
            request.url().split('?').next(),
            Some("/health") | Some("/checkpoint") | Some("/role")
        )
}

/// Whether the request is a switchover action — `POST /promote` or
/// `POST /demote` — [`Monitor::serve`]'s routing step ahead of
/// [`submission`]. Role changes are the pair's control-plane
/// actuation, not bulk reads: they take the dedicated control lane so
/// an operator's promote or demote — issued during an incident,
/// exactly when consoles wedge — never queues behind serving workers
/// pinned by undrained responses or submission workers pinned by a
/// dead client's owed body. That guarantee extends to the attributed
/// shape: a [`SwitchRequest`] `{"actor":…}` body is tens of bytes, so
/// [`buffered_body`] proves it arrived already read and it rides the
/// control lane beside the bare request. A switchover request whose
/// body could still wait on the client — a longer declared body, a
/// chunked or `Expect`-deferred one, a connection upgrade — takes the
/// submission lane instead, where its read or drain quarantine
/// belongs. The query string is ignored.
fn role_change(request: &Request) -> bool {
    request.method() == &Method::Post
        && matches!(
            request.url().split('?').next(),
            Some("/promote") | Some("/demote")
        )
}

/// Whether the request's body is already off the wire — the second
/// half of [`Monitor::serve`]'s switchover routing, deciding which
/// lane a [`role_change`] request takes. tiny_http reads a declared
/// `Content-Length` of at most [`EAGER_BODY_LIMIT`] eagerly while
/// assembling the request, on the connection task before the request
/// ever reaches routing: such a body is a buffered cursor here, and a
/// request declaring none carries an empty reader the same way, so no
/// body read or dropped-reader drain on this lane can ever wait on
/// the client. Every other shape still owes client-paced bytes — a
/// declared body past the bound, an `Expect: 100-continue` handshake
/// (which holds even a small declared body live on the socket), a
/// chunked stream, or a connection upgrade — and stays on the
/// submission lane's quarantine.
///
/// The header tests below mirror tiny_http's own `new_request`
/// predicates exactly — its upgrade test is a case-insensitive
/// substring match on the `Connection` value, not a token match —
/// because this function's verdict is only sound when it agrees with
/// the reader shape the request was actually built with: a `lazy`
/// answer here must cover every request tiny_http handed a live
/// socket reader.
fn buffered_body(request: &Request) -> bool {
    let lazy = request.headers().iter().any(|header| {
        header.field.equiv("Transfer-Encoding")
            || header.field.equiv("Expect")
            || (header.field.equiv("Connection")
                && header
                    .value
                    .as_str()
                    .to_ascii_lowercase()
                    .contains("upgrade"))
    });
    !lazy
        && request
            .body_length()
            .is_none_or(|length| length <= EAGER_BODY_LIMIT)
}

/// One lane's bounded request queue — [`Monitor::serve`]'s dispatcher
/// pushes, the lane's workers pop. The queue caps at
/// [`LANE_QUEUE_DEPTH`]: [`push`](Self::push) hands the request back
/// once the lane is full or closed rather than queueing without
/// limit, so a flood pinned behind wedged workers stays a bounded
/// count of waiting requests — the dispatcher routes the overflow to
/// the refuse lane. [`close`](Self::close) releases every blocked
/// worker once the queued requests drain, so `shutdown` reaching the
/// dispatcher propagates down all lanes.
struct Lane {
    inner: Mutex<LaneInner>,
    ready: Condvar,
    /// The queue bound [`push`](Self::push) enforces —
    /// [`LANE_QUEUE_DEPTH`].
    depth: usize,
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
            depth: LANE_QUEUE_DEPTH,
        }
    }

    /// Queues `request` for the lane's workers, or hands it back —
    /// `Some(request)` — when the lane is closed or already holding
    /// `depth` requests. Never waits: a push past the bound is the
    /// caller's signal to refuse the request elsewhere, so a pinned
    /// lane's queue is the flood's hard bound.
    fn push(&self, request: Request) -> Option<Request> {
        let mut inner = self.inner.lock().unwrap();
        if inner.closed || inner.queue.len() >= self.depth {
            return Some(request);
        }
        inner.queue.push_back(request);
        self.ready.notify_one();
        None
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

/// The command lane's bounded submission queue — a [`Lane`]-shaped
/// queue with the overflow half built in, because `POST /command`'s
/// contract is stronger than a generic lane's: every submission is
/// owed a receipted answer, and the executor's log must append those
/// receipts in submission order — the dispatch order the one command
/// worker drains by, which a second consumer racing the shared lock
/// would invert. A submission the lane cannot hold is *deferred* —
/// [`push`](Self::push) counts it and hands it back for the
/// dispatcher to queue on the overflow lane — and while any deferral
/// stands unpaid the dispatcher defers every later submission too,
/// so the overflow lane's FIFO is exactly the arrival order of the
/// requests the lane could not hold. The overflow feeder then hands
/// each one back through [`push_wait`](Self::push_wait), which admits
/// it only at the queue's drained tail — behind every earlier
/// submission still waiting — so the single command worker mints
/// every receipt in dispatch order, flooded lane or not.
/// [`close`](Self::close) ends the drain the same way a `Lane`'s
/// does.
struct CommandLane {
    inner: Mutex<CommandLaneInner>,
    /// Signals a queued request — the worker's wait.
    ready: Condvar,
    /// Signals a freed slot — a waiting feeder's wait.
    room: Condvar,
    /// The queue bound [`push`](Self::push) enforces —
    /// [`command_lane_depth`]'s wave.
    depth: usize,
}

struct CommandLaneInner {
    queue: VecDeque<Request>,
    /// Submissions the dispatcher handed to the overflow lane that the
    /// feeder has not placed yet — those still queued there plus one
    /// the feeder may be handing back right now. While nonzero,
    /// [`push`](CommandLane::push) defers every later submission too:
    /// a direct push would cut ahead of an earlier request still
    /// waiting its turn — the receipt-order inversion this count
    /// exists to bar.
    deferred: usize,
    closed: bool,
}

impl CommandLane {
    fn with_depth(depth: usize) -> Self {
        Self {
            inner: Mutex::new(CommandLaneInner {
                queue: VecDeque::new(),
                deferred: 0,
                closed: false,
            }),
            ready: Condvar::new(),
            room: Condvar::new(),
            depth,
        }
    }

    /// Queues `request` for the command worker, or hands it back —
    /// `Some(request)` — when the lane is closed, already holding
    /// `depth` requests, or still owes an earlier deferred submission
    /// its place. The hand-back counts a deferral: the dispatcher
    /// owes the request to the overflow lane — or
    /// [`undefer`](Self::undefer), when even that bound is met — so
    /// the count always matches the submissions the feeder has left
    /// to place. Never waits: queue room is the feeder's wait, not
    /// the dispatcher's.
    fn push(&self, request: Request) -> Option<Request> {
        let mut inner = self.inner.lock().unwrap();
        if inner.closed || inner.deferred > 0 || inner.queue.len() >= self.depth {
            inner.deferred += 1;
            return Some(request);
        }
        inner.queue.push_back(request);
        self.ready.notify_one();
        None
    }

    /// Places a deferred `request` back on the lane — the overflow
    /// feeder's half of the submission-order guarantee. It waits for
    /// queue room, lands the request at the drained tail behind every
    /// earlier submission still queued, and releases its deferral so
    /// a later arrival may queue directly once no older one is ahead
    /// of it. A closed lane hands the request back for the
    /// dispatcher's drop path instead.
    fn push_wait(&self, request: Request) -> Option<Request> {
        let mut inner = self.inner.lock().unwrap();
        loop {
            if inner.closed {
                inner.deferred -= 1;
                return Some(request);
            }
            if inner.queue.len() < self.depth {
                inner.queue.push_back(request);
                inner.deferred -= 1;
                self.ready.notify_one();
                return None;
            }
            inner = self.room.wait(inner).unwrap();
        }
    }

    /// Releases a deferral whose request the overflow lane could not
    /// hold — the drop path's half of the count, so `deferred` keeps
    /// matching the submissions the feeder will place.
    fn undefer(&self) {
        self.inner.lock().unwrap().deferred -= 1;
    }

    fn pop(&self) -> Option<Request> {
        let mut inner = self.inner.lock().unwrap();
        loop {
            if let Some(request) = inner.queue.pop_front() {
                self.room.notify_one();
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
        self.room.notify_all();
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
    let Shared { peer, recorder, .. } = shared;
    let report = peer.track_once(pull);
    let tick = peer.tick();
    // The tracking cycle's drain, one match: the whole account of the
    // pull, the orphan cycle's claim evidence, the refused
    // self-promotion, the role walk, and the settlements the adoption
    // carried — in the producer's category order, which is the order
    // this path journaled them in by hand.
    for event in peer.drain_pending() {
        match event {
            PeerEvent::Divergence(report) => {
                recorder.note_divergence(report.tick, report.mismatches);
            }
            PeerEvent::Resolution(resolution) => recorder.note_resolution(resolution),
            PeerEvent::Reinitialization(report) => recorder.note_reinitialized(report),
            PeerEvent::Orphan(orphan) => recorder.note_field_orphaned(orphan),
            // An orphan-cycle re-arm that actually landed the claim —
            // the record naming who re-took the field the orphan
            // detection alone cannot attribute — journals beside the
            // orphan record too.
            PeerEvent::ClaimRearm(rearm) => recorder.note_claim_rearmed(rearm),
            // A foreign owner the orphan cycle's re-arm probe just met
            // — the claimant token the refusal named — journals beside
            // the orphan record it answered, once per distinct claimant.
            PeerEvent::ClaimObservation(observation) => {
                recorder.note_claim_observed(observation);
            }
            PeerEvent::SourceRestart(restart) => recorder.note_source_restart(restart),
            // A self-promotion the boundary refused — a live incumbent's
            // standing claim, a transient claim ask, a voided proof —
            // leaves no role change of its own, so the journal takes it
            // here, one entry per distinct refusal cause the streak
            // produced.
            PeerEvent::PromotionRefusal(refusal) => recorder.note_promotion_refused(refusal),
            // The budget-th orphaned pull that self-promoted off a
            // usurped verdict took the field from the standing writer
            // the diagnosis named — the audit record the automatic
            // half of the recovery owes beside its transitions.
            PeerEvent::ForeignClaimPreempt(preempt) => {
                recorder.note_foreign_claim_preempted(preempt);
            }
            PeerEvent::RoleChange(change) => recorder.note_role_change(&change),
            // Pending commands an adopted checkpoint abandoned — the
            // demoted run's suspended queue the tracked line never
            // carried — settle `superseded` here rather than vanishing
            // from the audit. Force-set and held-value changes the
            // adoption authored beyond the receipted log — a re-stood
            // or dropped force, or a reverted receipted write, no
            // settled verdict backs — journal here too, each receipt's
            // actor naming the adopting checkpoint.
            PeerEvent::SupersededCommand { index, receipt } => {
                recorder.note_settled(Some(index), receipt, tick);
            }
            // A settled receipt the same adoption displaced by a
            // submission-index collision and re-minted past the
            // adopted window's high-water: its verdict journaled under
            // the prior index already, so the settle record only
            // re-keys onto the index the served window now carries.
            PeerEvent::RehomedReceipt {
                prior,
                index,
                receipt,
            } => recorder.note_rehomed(prior, index, receipt, tick),
            PeerEvent::AdoptionReceipt(receipt) => {
                recorder.note_settled(None, receipt, tick);
            }
            PeerEvent::FencingLoss(_) | PeerEvent::StartupRefusal(_) => {}
        }
    }
    store.sync_receipts(peer.receipts());
    // The cycle's apply, miss accounting, or a self-promotion moved
    // the report — the liveness mirror refreshes beside the receipt
    // one so `GET /role` and `GET /health` serve it at once.
    store.sync_liveness(peer.report());
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
    let Shared { peer, recorder, .. } = shared;
    let tick = peer.scan();
    let snapshot = recorder.record_scan(peer.executor(), tick);
    // The scan's drain, one match, beside the scan's own events: a field
    // write the plant fenced — the claim this owner held was preempted
    // — completed the scan degraded and demoted the peer inside it
    // rather than failing it, so the claim loss and the role transition
    // it drove journal here, cause before effect, beside the
    // `io_health` fault the boundary already counted; the foreign owner
    // the reclaim probe met journals beside them, once per distinct
    // claimant; and a deferred startup grant's refusal — the pending
    // born-active's re-issued ask answered mid-scan — records the
    // settled verdict once, the entry that makes the pending state's
    // terminal settle durable on run shapes whose shell never regains
    // control.
    for event in peer.drain_pending() {
        match event {
            PeerEvent::FencingLoss(loss) => {
                recorder.note_field_claim_lost(loss.tick, loss.point, loss.claimant);
            }
            PeerEvent::ClaimObservation(observation) => {
                recorder.note_claim_observed(observation);
            }
            PeerEvent::StartupRefusal(refusal) => {
                recorder.note_startup_claim_refused(refusal);
            }
            PeerEvent::RoleChange(change) => recorder.note_role_change(&change),
            PeerEvent::Divergence(_)
            | PeerEvent::Resolution(_)
            | PeerEvent::Reinitialization(_)
            | PeerEvent::Orphan(_)
            | PeerEvent::ClaimRearm(_)
            | PeerEvent::SourceRestart(_)
            | PeerEvent::PromotionRefusal(_)
            | PeerEvent::ForeignClaimPreempt(_)
            | PeerEvent::SupersededCommand { .. }
            | PeerEvent::RehomedReceipt { .. }
            | PeerEvent::AdoptionReceipt(_) => {}
        }
    }
    store.publish(tick, snapshot, peer.receipts());
    // The scan's wall-clock completion stamp — `GET /health`'s
    // `last_scan_age_ms` freshness — beside the post-scan report, into
    // the liveness mirror the heartbeat lane serves. Set once the
    // publication the scan produced is served, so the age the probe
    // reports is the age of what consumers can already read.
    store.note_scanned(peer.report(), Instant::now());
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

/// The `/checkpoint` query's `prove` key — the nonce a keyed pull
/// carries so the served document's `line_proof` attests the answer
/// came from a peer holding the pair's key. `None` on an absent or
/// unparseable value: the checkpoint is served plain either way, so a
/// pull that never asked for proof changes nothing.
fn checkpoint_prove(query: &str) -> Option<u64> {
    query_pairs(query).find_map(|(key, value)| {
        if key == "prove" {
            value.parse().ok()
        } else {
            None
        }
    })
}

/// The key material a `--pair-token` deployment gives both peers'
/// monitors — the token hashed to the fixed-width key [`line_proof`]
/// and [`Monitor::with_pair_key`] take. The token is the deployment
/// secret; the key is only ever compared, never served.
pub fn pair_key(token: &str) -> u64 {
    let digest = Sha256::digest([b"dcs-pair-key".as_slice(), token.as_bytes()].concat());
    u64::from_le_bytes(digest[..8].try_into().unwrap())
}

/// The keyed line proof a `?prove=` checkpoint response stamps — the
/// digest binding `nonce` to the served document's semantic content
/// under `key`, which only a peer holding the pair's key can produce.
/// The proof covers the document as both peers decode it — every
/// contract field, `line_proof` itself excepted — so a document
/// server cannot pair a stolen proof with fabricated state, and a
/// relayed answer proves only the document it carried. Additive wire
/// fields a build does not know stay outside the digest on both
/// sides. Public like [`pair_key`] so a key-holding peer — or a test
/// endpoint exercising the keyed contract — can stamp its answers.
pub fn line_proof(key: u64, nonce: u64, checkpoint: &Checkpoint) -> u64 {
    let mut document = serde_json::to_value(checkpoint).unwrap_or_default();
    if let Some(object) = document.as_object_mut() {
        object.remove("line_proof");
    }
    let mut hasher = Sha256::new();
    hasher.update(b"dcs-line-proof");
    hasher.update(key.to_le_bytes());
    hasher.update(nonce.to_le_bytes());
    hasher.update(document.to_string().as_bytes());
    let digest = hasher.finalize();
    u64::from_le_bytes(digest[..8].try_into().unwrap())
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
    /// The pulled checkpoint declares a stream position ahead of its
    /// own run tick: `stream_tick` stamps the captured `tick` minus
    /// the lead the serving run's paced clock accrued over the line,
    /// so an honest declaration never exceeds the tick it rides — a
    /// document claiming one is nonsense the line's stream cannot
    /// have produced, a forged or corrupt document wearing this
    /// line's identity.
    ImpossibleLead {
        /// The stream position the pulled checkpoint declares.
        declared: Tick,
        /// The run tick the pulled checkpoint claims.
        tick: Tick,
    },
    /// The pulled checkpoint claims its source owns the field — the
    /// stamp every document this run's own `/checkpoint` answer
    /// carries — at a tick this run's own answer could already have
    /// served. `/checkpoint` is public and freely fetchable, so an
    /// endpoint serving a field-owning document may simply be
    /// replaying one of this run's own: at or behind this run's tick
    /// the replay is verbatim or stale and proves nothing about the
    /// endpoint. Two productions never take this shape: a tracking
    /// standby stamps `source_owns_field: false`, and a successor
    /// that already owns the field continues this run's line strictly
    /// ahead of it — the one owner document an attested pull may
    /// accept, the `line_proof` the caller demanded being what
    /// separates it from the replayed bump.
    OwnDocument {
        /// The tick the pulled checkpoint claims.
        pulled: Tick,
        /// This run's own tick when the hint was verified.
        own: Tick,
    },
    /// The pulled checkpoint does not claim the field —
    /// `source_owns_field` absent or `false` — so its endpoint is a
    /// standby tracking this line onward, not the owner the
    /// orphan-resolution probe is looking for: resolving onto it
    /// would pin the demoted peer onto another subordinate of the
    /// same stale island.
    NotOwner,
}

impl std::fmt::Display for AnnouncedCheckpointError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::UnreadableVersion { found } => {
                write!(formatter, "unreadable checkpoint format version {found}")
            }
            Self::ForeignGeneration => {
                write!(
                    formatter,
                    "the pulled checkpoint names a foreign generation"
                )
            }
            Self::ImpossibleLead { declared, tick } => write!(
                formatter,
                "the pulled checkpoint declares stream position {} ahead of its \
                 own run tick {} — an honest document's declared lead is never \
                 negative",
                declared.0, tick.0
            ),
            Self::OwnDocument { pulled, own } => write!(
                formatter,
                "the pulled field-owning checkpoint at {} is not ahead of this \
                 run's {} — the replayable own-document shape",
                pulled.0, own.0
            ),
            Self::NotOwner => write!(formatter, "the pulled checkpoint does not claim the field"),
        }
    }
}

/// Whether one checkpoint pulled from an announced demotion hint
/// proves the hinted endpoint serves this run's continuation — the
/// demote-side half of the `?peer=` hardening, run only on a pull the
/// caller already proved under the pair's key: the announced-source
/// contract is keyed-only, so every document reaching this check is
/// key-attested and these are the *document* checks on top of the
/// endpoint's proof, never a substitute for it. The serving side
/// cannot tell the puller's monitor port from any other same-IP port
/// a connection claims, so the recorded hint is trusted only after
/// the endpoint's own checkpoint answers as this line's successor
/// would: a readable format, this run's generation — the line's
/// tick-domain identity, which a tracking peer adopts verbatim on
/// every apply and a reinitialized successor keeps across the
/// fingerprint crossing — and an internally consistent declared
/// stream position. There is deliberately no skew bound against this
/// run's own position: a detached prober's `tick` and `stream_tick`
/// alike advance at its own scan cadence while the pulled stream
/// advances at its source's, so the gap between them measures pace
/// asymmetry, never line membership — a same-generation successor
/// that is simply ahead in stream position is this line's
/// continuation regardless of how fast it advances relative to this
/// run's clock (QA finding
/// `skew-bound-strands-slower-cadence-ex-owner`, where a
/// slow-scanning demoted peer refused a faster successor forever).
/// The position check that survives is the document's own
/// consistency: `stream_tick` stamps `tick` minus the serving run's
/// accrued lead, so a declaration ahead of the document's own run
/// tick is nonsense no honest run produces — the forgery shape,
/// refused. The model fingerprint is deliberately absent here: a
/// revised successor's
/// checkpoints legitimately carry the next model's — the demoted
/// peer's own apply gate refuses a foreign fingerprint at adoption
/// and reports `degraded`, the revision roll's designed shape, so a
/// fingerprint inequality is not a demote-side refusal. A checkpoint
/// behind this run's tick is no rejection either — a lagging
/// successor is still this line, and the demoted peer's pulls simply
/// reconverge it. One document shape is refused outright: a
/// field-owning source not ahead of this run's tick — the exact
/// stamp this run's own `/checkpoint` answers, which being public
/// any endpoint can replay, so at or behind this run's tick the
/// document is verbatim or stale and proves nothing a successor
/// could not have replayed. A real tracking peer's checkpoint never
/// needs the shape — a standby stamps `source_owns_field: false` —
/// and a successor that already owns the field serves this line
/// strictly ahead under the pull's proof.
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
    // The one positional check the document can honestly carry: its
    // declared stream position is its own tick minus the accrued
    // outage lead, so a declaration ahead of the tick itself is
    // nonsense — a forged or corrupt document, refused.
    if let Some(declared) = pulled.stream_tick
        && declared > pulled.tick
    {
        return Err(AnnouncedCheckpointError::ImpossibleLead {
            declared,
            tick: pulled.tick,
        });
    }
    if pulled.source_owns_field == Some(true) && pulled.tick.0 <= own.tick.0 {
        return Err(AnnouncedCheckpointError::OwnDocument {
            pulled: pulled.tick,
            own: own.tick,
        });
    }
    Ok(())
}

/// Whether one checkpoint pulled while the tracked line reports no
/// field owner proves its source serves this line *as that owner* —
/// the orphan-resolution probe's bar. The document checks are the
/// demote verification's readable-format, same-generation,
/// internally-consistent ones — no skew bound against this run's own
/// position stands on a detached prober, whose stream position
/// advances at its own scan cadence and is no line-membership
/// reference: an owner ahead of it by any distance is this line's
/// continuation (QA finding
/// `skew-bound-strands-slower-cadence-ex-owner`). On top of them the
/// document must itself claim
/// the field — `source_owns_field: true` — since a standby-line
/// endpoint merely tracking this line onward proves nothing about
/// ownership and would only re-pin the demoted peer onto another
/// island member. The demote check's own-document rejection does not
/// apply here: the probing run is already a standby, so an owner a
/// few ticks behind its local tick is still this line's owner.
fn verify_owner_checkpoint(
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
    // As in `verify_announced_checkpoint`: the only positional check
    // the document honestly carries is internal consistency — a
    // declared stream position ahead of the document's own run tick
    // is nonsense no honest run serves.
    if let Some(declared) = pulled.stream_tick
        && declared > pulled.tick
    {
        return Err(AnnouncedCheckpointError::ImpossibleLead {
            declared,
            tick: pulled.tick,
        });
    }
    if pulled.source_owns_field != Some(true) {
        return Err(AnnouncedCheckpointError::NotOwner);
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

/// Reads a `POST /promote`/`POST /demote` body into its declared actor
/// within [`read_body`]'s bound. The bare request — an empty or
/// whitespace body, the pre-attribution shape — submits unattributed
/// (`Ok(None)`); a present body parses as the attributed
/// [`SwitchRequest`], with a parse failure the `400` response
/// directly: an attribution the body meant to carry never silently
/// drops.
fn read_switch_submission(
    request: &mut Request,
) -> Result<Option<String>, Response<Cursor<Vec<u8>>>> {
    let body = read_body(request)?;
    if body.iter().all(|byte| byte.is_ascii_whitespace()) {
        return Ok(None);
    }
    serde_json::from_slice::<SwitchRequest>(&body)
        .map(|body| body.actor)
        .map_err(|error| json(400, &error.to_string()))
}

/// Reads and parses a JSON request body within [`read_body`]'s bound;
/// parse failures produce the `400` response directly.
fn read_json<T: DeserializeOwned>(request: &mut Request) -> Result<T, Response<Cursor<Vec<u8>>>> {
    let body = read_body(request)?;
    serde_json::from_slice(&body).map_err(|error| json(400, &error.to_string()))
}

/// Reads a `POST /command` body into its [`CommandEnvelope`]. The
/// attributed shape — `{"command":…,"actor":…,"reason":…}` — is
/// selected by any envelope key; anything else parses as the bare
/// [`Command`] pre-attribution shape, so existing clients submit
/// unchanged and journal unattributed (`actor: None`, `reason: None`).
/// A body naming `command`, `actor`, or `reason` without the envelope's
/// shape is a `400` — an attribution the body meant to carry never
/// silently drops. Parse failures produce the `400` response directly.
fn read_command_submission(
    request: &mut Request,
) -> Result<CommandEnvelope, Response<Cursor<Vec<u8>>>> {
    let body = read_body(request)?;
    let value: serde_json::Value = match serde_json::from_slice(&body) {
        Ok(value) => value,
        Err(error) => return Err(json(400, &error.to_string())),
    };
    let attributed = value.as_object().is_some_and(|object| {
        object.contains_key("command")
            || object.contains_key("actor")
            || object.contains_key("reason")
    });
    if attributed {
        serde_json::from_value::<CommandEnvelope>(value)
            .map_err(|error| json(400, &error.to_string()))
    } else {
        serde_json::from_value::<Command>(value)
            .map(|command| CommandEnvelope {
                command,
                actor: None,
                reason: None,
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

/// Why one tracking cycle's pull produced no checkpoint.
///
/// Every stage is a property of the cycle that observed it, never a
/// detail carried forward from an earlier one: a transient failure
/// names itself for as long as it is the most recent thing the worker
/// reported and never past the point that condition could still
/// describe the endpoint. That is what keeps a single early failure —
/// the one refused read a standby's first pull against a source still
/// coming up produces — from standing as the run's permanent tracking
/// verdict while the same source answers every later fetch.
///
/// The stages also separate the pull path's own waits, which is the
/// observation the QA finding `pending-source-pull-latches-egain`
/// needed and could not get from a rig: whether a produced-nothing
/// cycle is waiting on the name, on a fetch still inside its bound, on
/// one that has outrun it, or on a document this cycle discarded, is
/// the difference between an endpoint that is away and a pull path that
/// has stopped taking what the endpoint serves.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PullMiss {
    /// The cycle's fetch is still outstanding and still inside the
    /// bound it fetches under.
    InFlight,
    /// The cycle's fetch is still outstanding past
    /// [`CHECKPOINT_PULL_TIMEOUT`] — the worker's own bound, so a
    /// resolution or a connection the worker cannot finish will not
    /// answer inside the window the endpoint is measured in. The miss is
    /// reported as the stall it is: the standby's budget keeps counting,
    /// and the detail no longer replays whatever the endpoint last said.
    /// A stall that also outruns [`CHECKPOINT_PULL_STALL`] costs the
    /// worker and the puller binds a fresh one, so the stage is a
    /// recovery rather than a verdict.
    Stalled,
    /// The cycle's fetch completed and its document went stale before
    /// the cycle could take it — a document that waited longer than the
    /// puller's own cadence accounts for, so it is dropped rather than
    /// applied. The next fetch reconverges fresh; the stage names the
    /// discard, never an earlier fetch's error.
    Stale,
    /// The configured name did not resolve for this cycle's fetch — the
    /// detail is the resolution failure, the one wait inside a fetch
    /// that no connect bound covers.
    Unresolved(String),
    /// The cycle's fetch resolved with this error: the endpoint
    /// refused, the read ran out of its bound, or the answer failed the
    /// pair's line proof. The detail is the worker's own, prefixed with
    /// the pull target.
    Refused(String),
    /// The fetch worker is gone — its thread ended, so no request this
    /// puller arms can complete.
    WorkerGone,
}

impl PullMiss {
    /// The `degraded` detail this miss reports — the named stage, so
    /// the served `sync` states which of the pull path's waits a
    /// produced-nothing cycle is actually in.
    pub fn detail(&self, target: &TrackTarget) -> String {
        match self {
            Self::Refused(detail) => detail.clone(),
            Self::Unresolved(detail) => format!("fetch from {target}: {detail}"),
            Self::InFlight => {
                format!("fetch from {target}: checkpoint pull still in flight")
            }
            Self::Stalled => format!(
                "fetch from {target}: the checkpoint fetch is stalled — the pull \
                 worker has not answered within its bound"
            ),
            Self::Stale => format!(
                "fetch from {target}: the fetched checkpoint went stale before this \
                 cycle could take it"
            ),
            Self::WorkerGone => {
                format!("fetch from {target}: the pull worker is gone")
            }
        }
    }
}

/// How far one fetch got before it answered — the stage the *worker*
/// reached, kept apart from the outcome so an unresolved name is never
/// reported as an endpoint refusal.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum FetchStage {
    /// The name resolved and the request ran: the outcome is the
    /// endpoint's document or its own error.
    Reached,
    /// The configured name did not resolve this cycle, so no request ran.
    Unresolved,
}

/// One completed fetch: when the worker finished it, how far it got,
/// and what it produced.
struct Completed {
    /// When the fetch resolved — the instant a document's wait is
    /// measured from when the cycle comes to take it.
    at: Instant,
    stage: FetchStage,
    result: Result<Checkpoint, String>,
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
/// error detail, or the named stage ([`PullMiss`]) while it runs. A
/// cycle whose pull has not yet produced a checkpoint is a heartbeat
/// miss exactly like a refused one, so the failover budget keeps
/// measuring wall time — budget × scan period — instead of fetch
/// latency: an active that cannot serve a checkpoint within the window
/// is declared lost on the same cadence a refused connect would be.
///
/// Feed `poll` to [`Monitor::track_cycle`] or
/// [`Peer::track_once`](dcs_runtime::Peer::track_once) as the `pull`.
/// A field-owning peer's cycle never invokes it, so a caller that
/// promotes drops the puller there and binds a fresh one on demotion:
/// the worker thread ends with it, and no answer fetched before the
/// promotion can land after it.
///
/// Nothing a puller learns outlives the condition that produced it. A
/// failure names itself only while it is the most recent thing the
/// worker reported — a landed document clears it, and so does a fetch
/// that has outrun its bound — so the one refused read a standby's
/// first pull against a source still coming up produces cannot stand as
/// the run's tracking verdict once that source serves (the QA finding
/// `pending-source-pull-latches-egain`). A document the cycle finds too
/// old to apply is measured against the puller's own cadence rather
/// than a fixed bound, and a fetch wedged past [`CHECKPOINT_PULL_STALL`]
/// costs its worker rather than the run's convergence.
pub struct CheckpointPuller {
    /// The pull target — the active's monitor address, or a configured
    /// [`TrackTarget::Name`] each fetch resolves anew.
    target: TrackTarget,
    /// The address each fetch announces as `?peer=`, or none.
    announce: Option<SocketAddr>,
    /// The pair's key each fetch proves its answer under, or none.
    proof_key: Option<u64>,
    /// Wakes the worker for one fetch; a send arms `pending`.
    requests: mpsc::Sender<()>,
    /// Completed fetches, consumed by [`poll`](Self::poll).
    results: mpsc::Receiver<Completed>,
    /// A fetch request is outstanding — sent and not yet consumed.
    pending: bool,
    /// When the outstanding request was armed, so a fetch that has
    /// outrun [`CHECKPOINT_PULL_TIMEOUT`] is reported as the stall it
    /// is instead of as the previous fetch's error.
    armed: Option<Instant>,
    /// The last completed fetch's stage and error, kept so a cycle
    /// polling while the next fetch is still inside its bound reports
    /// the most recent real detail rather than only the in-flight
    /// state — and dropped the moment a fetch produces a document or
    /// the outstanding one overruns its bound, so it can never stand
    /// as this run's tracking verdict.
    last_error: Option<(FetchStage, String)>,
    /// When the previous cycle polled, so the puller can measure its own
    /// cadence — see [`staleness`](Self::staleness).
    last_poll: Option<Instant>,
    /// The longest interval two consecutive polls have been apart, the
    /// cadence a completed fetch's wait is measured against.
    cadence: Duration,
    /// How many wedged fetch workers this puller has retired — the
    /// bound [`MAX_RETIRED_PULL_WORKERS`] places on rebinding, cleared
    /// by a fetch that lands.
    retired: usize,
}

impl CheckpointPuller {
    /// Spawns the fetch worker for a standby tracking the active at
    /// `active` — its monitor address, the same `--standby` target.
    /// `announce`, when set, is this monitor's own address carried on
    /// each fetch as `?peer=` — the announcement that gives the serving
    /// peer somewhere to track if it is demoted later.
    pub fn new(active: SocketAddr, announce: Option<SocketAddr>) -> Self {
        Self::for_target(TrackTarget::Addr(active), announce, None)
    }

    /// As [`new`](Self::new) with each fetch carrying a fresh `?prove=`
    /// nonce and its returned document required to carry `key`'s
    /// matching `line_proof` — the fetch fails like a refused pull when
    /// the answer does not prove a peer holding the pair's key produced
    /// it. This is the puller a keyed demoted peer runs against its
    /// adopted announced source: an endpoint that only replays or
    /// fabricates this line's checkpoints feeds the tracking peer
    /// nothing, and the heartbeat budget measures the miss.
    pub fn with_pair_proof(active: SocketAddr, announce: Option<SocketAddr>, key: u64) -> Self {
        Self::for_target(TrackTarget::Addr(active), announce, Some(key))
    }

    /// The general [`new`](Self::new): `target` is the resolved
    /// tracking source the pull bound to — or a configured
    /// [`TrackTarget::Name`] the worker resolves inside each fetch, so
    /// an unresolvable name is the same fetch failure an unreachable
    /// endpoint produces and the name answering later tracks again
    /// without a respawn. `proof_key`, when set, carries the
    /// `?prove=` contract [`with_pair_proof`](Self::with_pair_proof)
    /// describes.
    pub fn for_target(
        target: TrackTarget,
        announce: Option<SocketAddr>,
        proof_key: Option<u64>,
    ) -> Self {
        let (requests, results) = spawn_worker(&target, announce, proof_key);
        Self {
            target,
            announce,
            proof_key,
            requests,
            results,
            pending: false,
            armed: None,
            last_error: None,
            last_poll: None,
            cadence: CHECKPOINT_PULL_TIMEOUT,
            retired: 0,
        }
    }

    /// Retires this puller's fetch worker and binds a fresh one, so a
    /// worker that cannot finish the fetch it took does not decide what
    /// every later cycle reports.
    ///
    /// The retired worker keeps its thread until the fetch it is stuck
    /// in resolves — nothing can preempt a blocking read — but its
    /// channels go with it, so the answer it eventually produces is
    /// never consumed and its thread ends there. The bound on how often
    /// this happens is [`MAX_RETIRED_PULL_WORKERS`], and a fetch that
    /// lands clears the count.
    fn rebind_worker(&mut self) {
        let (requests, results) = spawn_worker(&self.target, self.announce, self.proof_key);
        self.requests = requests;
        self.results = results;
        self.pending = false;
        self.armed = None;
        self.retired += 1;
    }

    /// The wall-clock age a completed fetch's document must still be
    /// under when a cycle comes to take it — twice the longest gap two
    /// consecutive polls have been apart, never below the pull bound.
    ///
    /// The measurement is the puller's own cadence, not a constant
    /// standing in for one. A deployed standby's period is whatever its
    /// `--scan-ms` says, and a scan cycle that overruns — a contended
    /// executor, the orphan probe's own bounded pulls, a host under
    /// load — stretches the gap between two polls without anything
    /// having gone wrong with the fetch. Against a fixed bound a period
    /// past that bound, or a single cycle that overran it, made *every*
    /// completed document too old to apply: the puller discarded each
    /// one, so the peer never converged, and — the latch the QA finding
    /// `pending-source-pull-latches-egain` recorded — the discard
    /// reported the earliest fetch's error on every cycle after it,
    /// naming a read that ran out of its bound against a source whose
    /// pending window had long since closed, for the life of the
    /// process. The bound follows the cadence instead, so a document is
    /// stale only when its wait is one the cadence cannot account for.
    ///
    /// The measurement resets once a completed document has been acted
    /// on, so a stretched cycle costs at most that one document: the
    /// run's convergence is never traded for a stretch the run has
    /// already absorbed.
    ///
    /// What is left is the case the bound exists for: polls pause while
    /// the peer owns the field, and a document captured before a
    /// promotion must not land after it — rewinding the run to a tick it
    /// already passed. A caller that promotes closes the pause itself: a
    /// field-owning cycle drops the puller, and the fresh one the run
    /// binds on demotion holds no answer from before the promotion.
    fn staleness(&self) -> Duration {
        self.cadence.max(CHECKPOINT_PULL_TIMEOUT) * 2
    }

    /// The tracking cycle's pull, once per scan: consumes the latest
    /// completed fetch — `Ok` applies it, `Err` counts the cycle's
    /// heartbeat miss — and requests the next when none is
    /// outstanding.
    pub fn poll(&mut self) -> Result<Checkpoint, String> {
        self.poll_staged().map_err(|miss| miss.detail(&self.target))
    }

    /// [`poll`](Self::poll) with the stage the cycle reached kept
    /// machine-readable — the same outcome a consumer acts on, and the
    /// observation a regression or an operator reads to tell the pull
    /// path's waits apart: the fetch's own error when it resolved, the
    /// name that did not answer, the in-flight state while a fetch is
    /// still inside its bound, the stall once one has outrun that bound,
    /// the stale-discard for a document that waited past the puller's
    /// cadence, and the gone worker that can no longer answer at all.
    /// The miss is honest either way — this cycle produced no
    /// checkpoint — and no stage carries a detail forward from an
    /// earlier cycle: the state a puller reports is the state the fetch
    /// it is waiting on is actually in.
    pub fn poll_staged(&mut self) -> Result<Checkpoint, PullMiss> {
        let now = Instant::now();
        if let Some(previous) = self.last_poll.replace(now) {
            self.cadence = self.cadence.max(now.duration_since(previous));
        }
        let mut latest = None;
        while let Ok(completed) = self.results.try_recv() {
            self.pending = false;
            self.armed = None;
            latest = Some(completed);
        }
        if !self.pending {
            if self.requests.send(()).is_err() {
                return Err(PullMiss::WorkerGone);
            }
            self.pending = true;
            self.armed = Some(now);
        }
        match latest {
            // A produced document is the only result the staleness bound
            // can cost: only it carries state whose landing could rewind
            // the run, and a resolved failure is already the truth about
            // the endpoint, however long the cycle that takes it waited.
            Some(Completed {
                at,
                stage: _,
                result: Ok(checkpoint),
            }) if at.elapsed() <= self.staleness() => {
                // The endpoint answered: nothing of the failure that
                // preceded it still describes it, and the stretch the
                // cadence measured has been paid for.
                self.last_error = None;
                self.cadence = CHECKPOINT_PULL_TIMEOUT;
                // The path answers again: the retirement budget the
                // earlier wedged workers spent is restored.
                self.retired = 0;
                Ok(checkpoint)
            }
            Some(Completed { result: Ok(_), .. }) => {
                // A document too old to apply is not evidence the
                // endpoint has gone away — it answered, and this cycle
                // was merely late. The earlier failure goes with it, so
                // a stretched cadence cannot stand the pending window's
                // verdict back up on the cycles the next fetch has not
                // answered yet.
                self.last_error = None;
                self.cadence = CHECKPOINT_PULL_TIMEOUT;
                Err(PullMiss::Stale)
            }
            Some(Completed {
                stage,
                result: Err(detail),
                ..
            }) => {
                let miss = staged_miss(stage, &detail);
                self.last_error = Some((stage, detail));
                Err(miss)
            }
            // Nothing completed this cycle: the fetch this cycle's
            // predecessor armed is still outstanding. While it is inside
            // the bound it fetches under, the most recent thing the
            // worker actually reported is the honest detail; past that
            // bound the fetch cannot be expected to answer at all, and an
            // earlier error says nothing about the endpoint now.
            None => {
                let waited = self.armed.map(|armed| armed.elapsed());
                match waited {
                    // Past the stall bound the worker is not coming back
                    // with this fetch: a fresh one takes the next
                    // request, so a wedged fetch cannot stand as this
                    // run's pull path for the life of the process. The
                    // budget bounds how often a run may do this — a
                    // fetch that lands restores it.
                    Some(waited) if waited > CHECKPOINT_PULL_STALL => {
                        self.last_error = None;
                        if self.retired < MAX_RETIRED_PULL_WORKERS {
                            self.rebind_worker();
                        }
                        Err(PullMiss::Stalled)
                    }
                    // Past the pull bound but inside the stall bound,
                    // the fetch is simply not answering: the miss is
                    // reported as the stall it is, and whatever the
                    // endpoint last said stops describing it.
                    Some(waited) if waited > CHECKPOINT_PULL_TIMEOUT => {
                        self.last_error = None;
                        Err(PullMiss::Stalled)
                    }
                    // Inside the bound it fetches under, the outstanding
                    // fetch is still the honest answer: the most recent
                    // thing the worker reported, or the state it is in.
                    Some(_) => Err(match &self.last_error {
                        Some((stage, detail)) => staged_miss(*stage, detail),
                        None => PullMiss::InFlight,
                    }),
                    None => Err(PullMiss::InFlight),
                }
            }
        }
    }
}

/// Spawns one fetch worker for `target` — the request/result channels
/// a [`CheckpointPuller`] arms and consumes.
///
/// The worker runs one fetch at a time and only when a cycle asked for
/// one — the documented cadence stays one pull per cycle, never a
/// free-running poll hammering the serving peer. A named target's
/// resolution runs here per fetch, inside the worker like the network
/// wait itself, never on the scan cycle's critical path.
fn spawn_worker(
    target: &TrackTarget,
    announce: Option<SocketAddr>,
    proof_key: Option<u64>,
) -> (mpsc::Sender<()>, mpsc::Receiver<Completed>) {
    let (requests, request_rx) = mpsc::channel::<()>();
    let (result_tx, results) = mpsc::channel();
    let worker = target.clone();
    std::thread::spawn(move || {
        // One fetch per request; the channels closing — the puller
        // dropped, or this worker retired for a fetch it could not
        // finish — ends the loop.
        while request_rx.recv().is_ok() {
            let nonce = proof_key.map(|_| mint_generation());
            let (stage, pulled) = match worker.resolve() {
                Ok(active) => (
                    FetchStage::Reached,
                    MonitorClient::with_timeout(active, CHECKPOINT_PULL_TIMEOUT)
                        .checkpoint_tracking(announce, nonce)
                        .map_err(|error| error.to_string()),
                ),
                // No request ran: the name itself did not answer, which
                // is a stage of its own and not an endpoint that refused
                // this cycle's fetch.
                Err(detail) => (FetchStage::Unresolved, Err(detail)),
            };
            let pulled = pulled
                .map_err(|detail| format!("fetch from {worker}: {detail}"))
                .and_then(|checkpoint| match (proof_key, nonce) {
                    (Some(key), Some(nonce))
                        if checkpoint.line_proof != Some(line_proof(key, nonce, &checkpoint)) =>
                    {
                        Err(format!(
                            "fetch from {worker}: checkpoint carried no valid line proof"
                        ))
                    }
                    _ => Ok(checkpoint),
                });
            let completed = Completed {
                at: Instant::now(),
                stage,
                result: pulled,
            };
            if result_tx.send(completed).is_err() {
                return;
            }
        }
    });
    (requests, results)
}

/// The miss naming how far the fetch that produced `detail` got — the
/// name that did not answer, or the endpoint that refused.
fn staged_miss(stage: FetchStage, detail: &str) -> PullMiss {
    match stage {
        FetchStage::Reached => PullMiss::Refused(detail.to_owned()),
        FetchStage::Unresolved => PullMiss::Unresolved(detail.to_owned()),
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
        self.checkpoint_tracking(Some(peer), None)
    }

    /// `GET /checkpoint` with the tracking-pull query: `peer` names
    /// this client's own monitor address — the follow-peer
    /// announcement — and `prove` carries the nonce a keyed pull
    /// stamps, which a keyed serving monitor answers by signing the
    /// document's `line_proof`. Either absent issues the plain read
    /// the endpoint always answered.
    pub fn checkpoint_tracking(
        &self,
        peer: Option<SocketAddr>,
        prove: Option<u64>,
    ) -> io::Result<Checkpoint> {
        let mut path = "/checkpoint".to_string();
        let mut separator = '?';
        if let Some(peer) = peer {
            path.push(separator);
            separator = '&';
            path.push_str(&format!("peer={peer}"));
        }
        if let Some(prove) = prove {
            path.push(separator);
            path.push_str(&format!("prove={prove}"));
        }
        self.get_json(&path)
    }

    /// `GET /role`: the instance's reported redundancy role and standby
    /// convergence — the pair-as-one-controller contract.
    pub fn role(&self) -> io::Result<RoleReport> {
        self.get_json("/role")
    }

    /// `GET /health`: the bounded liveness answer — the listener's live
    /// declaration, the served role, and the last completed scan's
    /// wall-clock age.
    pub fn health(&self) -> io::Result<HealthReport> {
        self.get_json("/health")
    }

    /// `POST /promote`: lifts the standby's write gate at the request's
    /// scan boundary, returning the post-change [`RoleReport`]. A
    /// refusal — `not_converged`, `already_active` — surfaces as an
    /// error whose message carries the `409` body: the named
    /// `SwitchError` JSON. The bare request journals unattributed;
    /// [`promote_as`](Self::promote_as) carries a declared actor.
    pub fn promote(&self) -> io::Result<RoleReport> {
        let (status, body) = self.request("POST", "/promote", None)?;
        decode(status, &body)
    }

    /// `POST /promote` with `actor` as the requester's declared identity
    /// — the attributed body `{"actor":…}` the journaled `RoleChanged`
    /// entries carry; `None` sends the same bare request
    /// [`promote`](Self::promote) sends, journaling unattributed.
    pub fn promote_as(&self, actor: Option<&str>) -> io::Result<RoleReport> {
        match actor {
            Some(actor) => self.post_json(
                "/promote",
                &SwitchRequest {
                    actor: Some(actor.to_string()),
                },
            ),
            None => self.promote(),
        }
    }

    /// `POST /demote`: re-closes the field owner's write gate at the
    /// request's scan boundary, returning the post-change
    /// [`RoleReport`]. A refusal on a non-owner surfaces like
    /// [`promote`](Self::promote)'s.
    pub fn demote(&self) -> io::Result<RoleReport> {
        let (status, body) = self.request("POST", "/demote", None)?;
        decode(status, &body)
    }

    /// `POST /demote` with `actor` as the requester's declared identity
    /// — the attributed-body variant [`promote_as`](Self::promote_as)
    /// documents.
    pub fn demote_as(&self, actor: Option<&str>) -> io::Result<RoleReport> {
        match actor {
            Some(actor) => self.post_json(
                "/demote",
                &SwitchRequest {
                    actor: Some(actor.to_string()),
                },
            ),
            None => self.demote(),
        }
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

    /// `GET /history/durable`: the retained durable-history entries
    /// with a `seq` above `since` (`0` fetches everything retained),
    /// `point` filtering `sampled` records to the declared points —
    /// `None` serves every declared-duty point. The pinned
    /// `run_boundary`/`domain` markers answer ahead of the bounded
    /// tail regardless of the filter, and an evicted stretch reads as
    /// a numbering gap on the `seq` axis.
    pub fn durable_history(&self, points: &[PointId], since: u64) -> io::Result<Vec<DurableEntry>> {
        let mut path = format!("/history/durable?since={since}");
        for point in points {
            path.push_str(&format!("&point={}", point.0));
        }
        self.get_json(&path)
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
                    reason: None,
                },
            ),
            None => self.command(command),
        }
    }

    /// `POST /command` with `actor` and `reason` both declared — the
    /// fully attributed envelope `{"command":…,"actor":…,"reason":…}`
    /// the shelving-reason decision records; the returned receipt and
    /// the journaled `CommandSettled` carry both fields. A `reason`
    /// declared without an `actor` still sends the envelope — the
    /// reason is independent submission metadata — while `None`/`None`
    /// submits the same bare-`Command` body [`command`](Self::command)
    /// sends, journaling unattributed.
    pub fn command_attributed(
        &self,
        command: &Command,
        actor: Option<&str>,
        reason: Option<&str>,
    ) -> io::Result<CommandReceipt> {
        match (actor, reason) {
            (None, None) => self.command(command),
            _ => self.post_json(
                "/command",
                &CommandEnvelope {
                    command: command.clone(),
                    actor: actor.map(str::to_string),
                    reason: reason.map(str::to_string),
                },
            ),
        }
    }

    /// `POST /scan`: runs `scans` scans, returning the snapshot taken
    /// after the last one. A `scans` past the monitor's declared
    /// per-request bound surfaces as an error carrying the `400`
    /// refusal naming the bound — a longer advance composes of
    /// bounded requests.
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
        let mut stream = loop {
            let attempt = match self.timeout {
                Some(timeout) => TcpStream::connect_timeout(&self.addr, timeout),
                None => TcpStream::connect(self.addr),
            };
            match attempt {
                // An interrupted connect attempt is abandoned with its
                // socket and retried fresh — a caught signal (e.g. a
                // spawned helper's `SIGCHLD`) is not a reachability
                // verdict on the address.
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                other => break other?,
            }
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

/// Reads into `chunk`, retrying an interrupted wait — a caught signal
/// (e.g. a spawned helper's `SIGCHLD`) is not link trouble, and reporting
/// it as one would turn a stray signal into a false endpoint failure.
fn read_chunk(stream: &mut TcpStream, chunk: &mut [u8]) -> io::Result<usize> {
    loop {
        match stream.read(chunk) {
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            other => return other,
        }
    }
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
        match read_chunk(stream, &mut chunk)? {
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
                match read_chunk(stream, &mut chunk)? {
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
                match read_chunk(stream, &mut chunk)? {
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
                    match read_chunk(stream, &mut chunk)? {
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
    fn health_report_serde_roundtrip_and_shape() {
        for report in [
            HealthReport {
                live: true,
                role: Role::Active,
                tick: Tick(7),
                last_scan_age_ms: Some(12),
                build: None,
            },
            HealthReport {
                live: true,
                role: Role::Standby,
                tick: Tick::ZERO,
                last_scan_age_ms: None,
                build: None,
            },
        ] {
            let json = serde_json::to_string(&report).unwrap();
            assert_eq!(serde_json::from_str::<HealthReport>(&json).unwrap(), report);
        }
        // The pinned wire shape: liveness, the served role in the
        // shared snake_case vocabulary, the run's tick, and the
        // last-scan age — `null` before the first scan completes.
        assert_eq!(
            serde_json::to_string(&HealthReport {
                live: true,
                role: Role::Active,
                tick: Tick(7),
                last_scan_age_ms: Some(12),
                build: None,
            })
            .unwrap(),
            r#"{"live":true,"role":"active","tick":7,"last_scan_age_ms":12}"#
        );
        assert_eq!(
            serde_json::to_string(&HealthReport {
                live: true,
                role: Role::Standby,
                tick: Tick::ZERO,
                last_scan_age_ms: None,
                build: None,
            })
            .unwrap(),
            r#"{"live":true,"role":"standby","tick":0,"last_scan_age_ms":null}"#
        );
    }

    /// A driver with no points — the liveness reads must never reach
    /// field I/O anyway, so the empty map serves.
    struct NoDriver;

    impl dcs_core::IoDriver for NoDriver {
        fn read(&self, point: PointId) -> Result<dcs_core::Sample, dcs_core::IoError> {
            Err(dcs_core::IoError::UnknownPoint(point))
        }

        fn write(&self, point: PointId, _value: dcs_core::Value) -> Result<(), dcs_core::IoError> {
            Err(dcs_core::IoError::UnknownPoint(point))
        }
    }

    /// The `liveness-reads-stall-behind-wedged-field-io` contract at the
    /// handler level: with the executor lock held — the shape a scan
    /// wedged in field I/O takes — `GET /health` and `GET /role` still
    /// answer, served from the store's published liveness mirror rather
    /// than `shared`. A handler still taking `shared` would stall each
    /// probe past the client's bound for as long as the lock stays held.
    #[test]
    fn liveness_reads_answer_without_the_executor_lock() {
        let driver = &*Box::leak(Box::new(NoDriver));
        let executor = Executor::new(driver, dcs_runtime::PointMap::new(), Vec::new()).unwrap();
        let monitor = Monitor::bind(
            "127.0.0.1:0",
            executor,
            SignalIndex {
                points: Vec::new(),
                components: Vec::new(),
            },
        )
        .unwrap();
        let monitor = Arc::new(monitor);
        let serving = std::thread::spawn({
            let monitor = monitor.clone();
            move || monitor.serve()
        });
        let client = MonitorClient::with_timeout(monitor.local_addr(), Duration::from_secs(2));
        // Probe once before locking so the assertions below measure the
        // held-lock case, not listener startup.
        client.health().expect("listener came up");

        // The wedged scan's shape: the shared lock held for as long as
        // the field I/O stalls — on the rig, the driver's whole
        // field-I/O timeout per in-flight scan.
        let wedged = monitor.shared.lock().unwrap();
        let health = client
            .health()
            .expect("GET /health queued behind the executor lock");
        assert!(health.live);
        client
            .role()
            .expect("GET /role queued behind the executor lock");
        drop(wedged);

        monitor.shutdown();
        serving.join().unwrap();
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
    /// generation, and an internally consistent declared stream
    /// position — or the hint proves nothing and the demotion
    /// refuses. The check runs only on pulls the caller proved under
    /// the pair's key — the announced-source contract is keyed-only —
    /// so a field-owning document strictly ahead of this run's tick
    /// is a real successor's, while one at or behind it is the
    /// replayable own-document shape and refuses. No skew bound
    /// compares the pulled position against this run's own: a
    /// detached prober's positions advance at its own scan cadence,
    /// so a same-generation successor that is simply ahead in stream
    /// position verifies — the `skew-bound-strands-slower-cadence-
    /// ex-owner` contract.
    #[test]
    fn verify_announced_checkpoint_accepts_only_this_lines_continuation() {
        let checkpoint = || Checkpoint {
            format_version: dcs_runtime::CHECKPOINT_FORMAT_VERSION,
            model_fingerprint: Some(dcs_core::ModelFingerprint(7)),
            generation: Some(11),
            anchor: None,
            tick: Tick(100),
            stream_tick: None,
            components: Default::default(),
            driver: None,
            outputs: Default::default(),
            internal: Default::default(),
            forces: Default::default(),
            receipts: Vec::new(),
            command_admission: Default::default(),
            source_owns_field: None,
            line_owner: None,
            line_proof: None,
            tracking_source: None,
        };
        let own = checkpoint();

        // The honest successor shapes: this line at the same tick,
        // ahead by any distance — a faster-paced successor is still
        // this line — and far behind — a lagging successor is too.
        // None of them stamps field ownership, which a tracking peer's
        // checkpoint never claims.
        for ahead in [0, 1, 32, 99999] {
            let mut pulled = checkpoint();
            pulled.tick = Tick(own.tick.0 + ahead);
            assert_eq!(verify_announced_checkpoint(&pulled, &own), Ok(()));
        }
        let mut lagging = checkpoint();
        lagging.tick = Tick(3);
        assert_eq!(verify_announced_checkpoint(&lagging, &own), Ok(()));
        // A successor that already owns the field is this line's
        // continuation only strictly ahead of the run it replaces.
        // Every document this run's own `/checkpoint` answers takes
        // that same field-owning shape, so at or behind this run's
        // tick it may just be the replayed answer — the
        // `demote-verify-own-document-check-bypassed-by-tick-bump`
        // reproduction — which the demotion refuses; strictly ahead
        // under the caller's key attestation it is a real successor's.
        for ahead in [1, 10, 32] {
            let mut owner_ahead = checkpoint();
            owner_ahead.source_owns_field = Some(true);
            owner_ahead.tick = Tick(own.tick.0 + ahead);
            assert_eq!(verify_announced_checkpoint(&owner_ahead, &own), Ok(()));
        }
        for lag in [0, 1, 50] {
            let mut replay = checkpoint();
            replay.source_owns_field = Some(true);
            replay.tick = Tick(own.tick.0 - lag);
            assert_eq!(
                verify_announced_checkpoint(&replay, &own),
                Err(AnnouncedCheckpointError::OwnDocument {
                    pulled: replay.tick,
                    own: own.tick,
                })
            );
        }
        // The reproduction verbatim: the victim's own checkpoint
        // document served back to it — stamped field-owning at the
        // run's own tick — is the refused shape.
        let mut verbatim = checkpoint();
        verbatim.source_owns_field = Some(true);
        assert_eq!(
            verify_announced_checkpoint(&verbatim, &own),
            Err(AnnouncedCheckpointError::OwnDocument {
                pulled: own.tick,
                own: own.tick,
            })
        );
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

        // This line's identity at a position arbitrarily far ahead of
        // the run's own is still this line's continuation: the prober's
        // detached positions are no line-membership reference — the QA
        // finding `skew-bound-strands-slower-cadence-ex-owner`, where a
        // slow-scanning demoted peer's skew bound refused a faster
        // successor forever. A forged stream's conviction lives in the
        // keyed `line_proof` and the command audit at the call sites,
        // never in a positional bound this run's own clock cannot
        // supply.
        let mut far_ahead = checkpoint();
        far_ahead.tick = Tick(99999);
        assert_eq!(verify_announced_checkpoint(&far_ahead, &own), Ok(()));
        // The QA finding
        // `tracking-verify-own-tick-ahead-bound-permanent-strand`: a
        // successor whose paced clock accrued a source-outage lead
        // serves `tick` arbitrarily far ahead while its `stream_tick`
        // honestly locates the line's position — and verifies; a
        // probed run whose own clock carries a lead declares it the
        // same way, so neither side's outage history moves the answer.
        let mut led = checkpoint();
        led.tick = Tick(own.tick.0 + 700);
        led.stream_tick = Some(Tick(own.tick.0 + 4));
        assert_eq!(verify_announced_checkpoint(&led, &own), Ok(()));
        let mut led_own = checkpoint();
        led_own.stream_tick = Some(Tick(own.tick.0 - 10));
        assert_eq!(verify_announced_checkpoint(&led, &led_own), Ok(()));
        // The new finding's shape verbatim: the faster successor's
        // declared stream position runs genuinely ahead of the
        // detached prober's own — same generation, honestly declared,
        // and one tick past the retired thirty-two-tick window where
        // the defect's refusal fired — and verifies rather than
        // refusing `Ahead` forever.
        let mut runaway = checkpoint();
        runaway.tick = Tick(own.tick.0 + 700);
        runaway.stream_tick = Some(Tick(own.tick.0 + 33));
        assert_eq!(verify_announced_checkpoint(&runaway, &own), Ok(()));
        // The one positional refusal that survives: a stream position
        // declared ahead of the document's own run tick is nonsense no
        // honest run produces — the forgery shape.
        let mut nonsense = checkpoint();
        nonsense.tick = Tick(own.tick.0 + 33);
        nonsense.stream_tick = Some(Tick(nonsense.tick.0 + 100));
        assert_eq!(
            verify_announced_checkpoint(&nonsense, &own),
            Err(AnnouncedCheckpointError::ImpossibleLead {
                declared: nonsense.stream_tick.unwrap(),
                tick: nonsense.tick,
            })
        );
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

    /// The orphan-resolution probe's bar: the same readable-format,
    /// same-generation, internally-consistent document checks, plus
    /// the pulled checkpoint must itself claim the field — a
    /// standby-line source merely tracking the line onward is
    /// `NotOwner`, so the demoted peer can never re-resolve onto
    /// another island member.
    #[test]
    fn verify_owner_checkpoint_rejects_a_standby_line_source() {
        let checkpoint = || Checkpoint {
            format_version: dcs_runtime::CHECKPOINT_FORMAT_VERSION,
            model_fingerprint: Some(dcs_core::ModelFingerprint(7)),
            generation: Some(11),
            anchor: None,
            tick: Tick(100),
            stream_tick: None,
            components: Default::default(),
            driver: None,
            outputs: Default::default(),
            internal: Default::default(),
            forces: Default::default(),
            receipts: Vec::new(),
            command_admission: Default::default(),
            source_owns_field: None,
            line_owner: None,
            line_proof: None,
            tracking_source: None,
        };
        let own = checkpoint();

        // The owner shapes: this line stamped field-owning — at the
        // probing standby's tick, ahead of it by any distance, and
        // behind it: the probe runs on a stalled demoted peer, so an
        // owner a few ticks back is still this line's owner, where the
        // demote check's own-document rejection does not apply.
        for lag in [0, 1, 50] {
            let mut owner = checkpoint();
            owner.source_owns_field = Some(true);
            owner.tick = Tick(own.tick.0 - lag);
            assert_eq!(verify_owner_checkpoint(&owner, &own), Ok(()));
        }
        let mut ahead = checkpoint();
        ahead.source_owns_field = Some(true);
        ahead.tick = Tick(own.tick.0 + 32);
        assert_eq!(verify_owner_checkpoint(&ahead, &own), Ok(()));
        // The QA finding
        // `tracking-verify-own-tick-ahead-bound-permanent-strand`'s
        // reproduction shape: the promoted successor's paced clock
        // carries the standby's accrued source-outage lead — `tick`
        // arbitrarily far ahead — while its declared `stream_tick`
        // honestly locates the line's position, so the legitimate
        // owner verifies and the demoted ex-owner rejoins.
        let mut successor = checkpoint();
        successor.source_owns_field = Some(true);
        successor.tick = Tick(own.tick.0 + 700);
        successor.stream_tick = Some(Tick(own.tick.0 + 5));
        assert_eq!(verify_owner_checkpoint(&successor, &own), Ok(()));
        // The QA finding
        // `skew-bound-strands-slower-cadence-ex-owner`'s reproduction
        // shape: the promoted successor's declared stream position
        // runs genuinely ahead of the detached prober's own — same
        // generation, honestly declared, simply faster-paced, one
        // tick past the retired thirty-two-tick window — and
        // verifies: the prober's own stream position is no
        // line-membership reference, so no skew bound may compare
        // against it.
        let mut declared_ahead = checkpoint();
        declared_ahead.source_owns_field = Some(true);
        declared_ahead.tick = Tick(own.tick.0 + 700);
        declared_ahead.stream_tick = Some(Tick(own.tick.0 + 33));
        assert_eq!(verify_owner_checkpoint(&declared_ahead, &own), Ok(()));

        // The reproduction's shape: a sibling standby's checkpoint —
        // this line's continuation stamped `source_owns_field: false`
        // — is exactly what the probe refuses: resolving onto it would
        // re-pin the demoted peer onto the stale island. The un-
        // stamped pre-stamping shape refuses the same way.
        let mut standby = checkpoint();
        standby.source_owns_field = Some(false);
        standby.tick = Tick(own.tick.0 + 3);
        assert_eq!(
            verify_owner_checkpoint(&standby, &own),
            Err(AnnouncedCheckpointError::NotOwner)
        );
        let mut unstamped = checkpoint();
        unstamped.tick = Tick(own.tick.0 + 3);
        assert_eq!(
            verify_owner_checkpoint(&unstamped, &own),
            Err(AnnouncedCheckpointError::NotOwner)
        );

        // A foreign generation, an impossible declared position, and
        // an unreadable format refuse before the ownership question is
        // ever asked. A field-owning document at an arbitrary tick —
        // the faster successor's honest shape — is no refusal.
        let mut restarted = checkpoint();
        restarted.generation = Some(12);
        restarted.source_owns_field = Some(true);
        assert_eq!(
            verify_owner_checkpoint(&restarted, &own),
            Err(AnnouncedCheckpointError::ForeignGeneration)
        );
        let mut nonsense = checkpoint();
        nonsense.source_owns_field = Some(true);
        nonsense.tick = Tick(99999);
        nonsense.stream_tick = Some(Tick(99999 + 100));
        assert_eq!(
            verify_owner_checkpoint(&nonsense, &own),
            Err(AnnouncedCheckpointError::ImpossibleLead {
                declared: nonsense.stream_tick.unwrap(),
                tick: nonsense.tick,
            })
        );
        let mut unreadable = checkpoint();
        unreadable.format_version = 999;
        unreadable.source_owns_field = Some(true);
        assert_eq!(
            verify_owner_checkpoint(&unreadable, &own),
            Err(AnnouncedCheckpointError::UnreadableVersion { found: 999 })
        );
    }
}
