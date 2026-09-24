# Pilot customer-acceptance baseline — water/wastewater

- **Checked:** 2026-09-17
- **Question:** Which open assumptions recorded across the water/wastewater requirements and research notes must be settled — and by whom — before a first customer pilot can be dispatched, which items are already answered by landed product evidence, and which stay out of scope? This note consolidates the dispersed assumption lists into one per-phase pilot checklist so planning can dispatch each item to the right lane.
- **Affected requirements:** `WW-OPS-001`–`WW-OPS-003`, `WW-CTL-001`–`WW-CTL-005`, `WW-ALM-001`–`WW-ALM-005`, `WW-ENG-001`, `WW-LCM-001`, and the candidate requirements `WW-ENG-002`, `WW-LCM-002`, `WW-SEC-001`, `WW-REP-001`.

## How to read this baseline

Each checklist item carries four attributes:

- **Class.** `satisfied-by-product` — landed product evidence already answers the item; the pilot consumes it as configuration or procedure, not a decision or new capability. `needs-first-client-decision` — the pilot cannot start until the named lane answers. `out-of-scope` — recorded outside the pilot or product boundary; the revisit condition says what reopens it.
- **Owner.** `customer`, `integrator`, or `platform owner` — the lane a decision or artifact is dispatched to. Customer answers are plant-owner policy; integrator answers are deployment-produced artifacts and procedures; platform-owner answers are recorded-decision or contract questions the DCS itself must close (often gated on a customer answer).
- **Evidence.** Whether the acceptance artifact must be `platform-generated` — model declarations, served surfaces, journaled records, scripted runs bound to a model fingerprint — or `deployment-produced` — the deployment's own tooling and document workflows: signed test records, backup sets, reports, identity and access logs. The citations on each item back that split.
- **Revisit.** The condition that reopens the item; per-requirement revisit conditions are collected at the end.

Bracketed source numbers cite the named research note's own source list. This baseline consolidates existing evidence and does not change any requirement status.

## Sources

- `docs/research/pumping-station.md` (issue #195) — station operating policies, measurement redundancy, alarm expectations, commissioning practice for `WW-OPS-001`–`WW-OPS-003`, `WW-CTL-001`, `WW-CTL-002`, `WW-LCM-001`.
- `docs/research/alarm-management.md` (issue #234) — lifecycle depth, rationalization and priority, flood metrics, the protection boundary, and the IJmuiden 2023 near miss for `WW-ALM-001`–`WW-ALM-005`.
- `docs/research/chemical-dosing.md` — `WW-CTL-003`.
- `docs/research/filter-backwash.md` (issue #233) — `WW-CTL-004`.
- `docs/research/aeration-do-control.md` (issue #241) — `WW-CTL-005`.
- `docs/research/bulk-engineering.md` (issue #243) — `WW-ENG-002`.
- `docs/research/deployment-commissioning.md` (issue #242) — `WW-LCM-002`.
- `docs/research/security-identity.md` (issue #255) — `WW-SEC-001`.
- `docs/research/operations-reporting.md` (issue #256) — `WW-REP-001`.

## Already satisfied by landed product evidence

These baseline topics need no customer decision and no new capability; the pilot consumes them as configuration and procedure. Where a related site-*policy* choice remains open it is listed as a checklist item — the machinery itself is not.

- **Alarm lifecycle.** The two-flag acknowledgment axis, the managed `shelved`/`suppressed`/`out_of_service` status vocabulary on the managed sibling kinds with declared `max_shelve_ticks` and automatic expiry, designed and state-based suppression as declared wiring with the named `suppressed` state, declared `priority`/`class`/`response_ticks` rationalization parameters served through the block interface with an optional `rationalization` record per instance, and the durable transition record through the declared `journaled` point flag — recorded as decisions 70–77 and implemented as `managed-latching-alarm`/`managed-bool-latching-alarm` and the `journaled` point attribute.
- **Redundancy and continuity.** The hot-standby pair — per-scan checkpoint pull, scan-boundary promotion/demotion preserving exactly one field writer, staged-output divergence detection as a promotion gate, heartbeat-loss self-promotion with field-side fencing, the pair-as-one-logical-controller view — cross-build checkpoint/`model_fingerprint` negotiation, the `--revised` in-service model revision with its `CarryoverReport`, and the `--state-file` lone-controller restart — decisions 10–15, 19, 25–28, 35; `WW-LCM-001` accepted, partially implemented.
- **Model-first engineering.** The versioned plant-model document with `dcs-model` `validate`/`lint`/`diff`/`schema`/`interface-schema` and `dcs-controller --check`, the typed `dcs-build` composition emitting byte-stable documents, and the independently owned consumer release contract proven by the separate reference-plant repository — `WW-FND-001`, `WW-FND-003`, `WW-ENG-001`, `WW-ENG-003`; decisions 3, 31, 40, 46, 79–81.
- **Receipted commands.** Bounded, validated, scan-boundary command ingress with `writable`-declared targets, named admission rejections (`NotWritable`, `queue_full`), declared-command dispatch with typed arguments and refusal receipts, and actor-attributed settled receipts — decisions 7, 18, 39, 84; `WW-FND-004` implemented.
- **Journaled record retention.** The durable append-only `--journal-file` — `CommandSettled` receipts, `QualityChanged`/`PointChanged` transitions, role changes, divergence and reinitialization events — across restarts with run-boundary markers and fatal-on-append-failure, served over `GET /journal` — decisions 36, 39, 74. Retention *period*, export, and tamper-evidence remain checklist items (SE-4), not the record itself.
- **Measurement-confidence machinery.** Signal quality propagation, declared freshness budgets (`stale_after_ticks`, decision 45), `failover-select` quality-driven primary/backup switching with an alarmed `backup_active`, `median-voter` 2oo3 voting with spread discrepancy, and I/O-health counters — decisions 22, 42, 45; `WW-OPS-003` partially implemented.
- **Commissioning primitives.** Persistent forcing with substituted quality (journaled and checkpointed — the loop-check substitution primitive), scan-overrun and I/O-health reporting, and the scripted rig tests plus checked-in compose rig as FAT-equivalent engineering evidence — decisions 21, 22, 49.

## Phase 1 — Pre-engineering: documents, formats, and conventions to confirm before engineering starts

- **PE-1 · Control narrative and engineering deliverable set** (`WW-ENG-002`, `WW-OPS-001`–`WW-OPS-003`). Confirm which documents the client requires — a process control narrative or owner-template functional descriptions, setpoint submittal, the alarm set list, the cause-and-effect matrix — and in which formats. Owner evidence: an owner-published standard control narrative instantiated across a station fleet (pumping-station.md [5]); setpoint submittals required during design (pumping-station.md [7 §2.11]); functional descriptions, I/O lists, alarm registers, and C&E matrices as named deliverables on published owner templates (bulk-engineering.md [12 §7.9, 13, 15]).
  - Class: needs-first-client-decision. Owner: customer names the deliverable set; integrator produces the documents.
  - Evidence: deployment-produced — the deliverables are owner-format spreadsheets and document templates that must *agree* with the configured system, not platform-emitted documents (bulk-engineering.md [13–15, 19, 22]). The platform's part — `dcs-model` `validate`/`lint`/`diff` and `dcs-controller --check` as the design-stage check (pumping-station.md proposal; deployment-commissioning.md seams) — is satisfied-by-product.
  - Revisit: a client naming a deliverable the platform must generate reopens `WW-ENG-002` gap 6.
- **PE-2 · Owner tag/naming convention** (`WW-ENG-002`). Confirm which convention applies — an ISA-5.1-derived loop scheme, an owner-specific fragment format, or IEC 81346-style reference designations — and whether the model must *store* owner tags or only *export* them into deliverables. Conventions demonstrably diverge per owner (bulk-engineering.md [10, 18, 20; 1–3]), and the owner's asset numbering is authoritative — tag identity flows down, not up ([10 §6, 11]).
  - Class: needs-first-client-decision. Owner: customer (its asset-numbering standard); whether tag carriage is model data is a platform-owner recorded decision (bulk-engineering.md gap 1).
  - Evidence: platform-generated only if the client names in-model tag carriage (optional declared tag plus lint uniqueness — schema work); otherwise deployment-produced documents.
  - Revisit: the recorded `WW-ENG-002` condition — a client spec naming an owner tag convention the model must carry.
- **PE-3 · I/O-list artifacts** (`WW-ENG-002`). Confirm the client's I/O-list format and field set, and the interchange direction: export-only from the model, or import/reconciliation from the client's spreadsheet or tag database. The I/O list is simultaneously a design submittal, build input, commissioning reference, and maintained record (bulk-engineering.md [12 §7.9.2, 13 TEM-00210, 14, 22, 23]); it is also named among the artifacts a restore needs (deployment-commissioning.md [7]).
  - Class: needs-first-client-decision. Owner: customer names the format; integrator maintains the artifact; import mapping is a platform-owner contract question (bulk-engineering.md gap 2).
  - Evidence: deployment-produced document; the platform holds every field an I/O list names — a `dcs-model`-level export is the proposed first interchange (gap 2, proposal).
  - Revisit: a client-named deliverable format promotes `WW-ENG-002`; import stays deferred until a source format and reconciliation rules are named.
- **PE-4 · Alarm priority vocabulary and rationalization record** (`WW-ALM-001`). Confirm the site priority vocabulary — level count, names, colour/indication conventions — the class rules and highly-managed membership, and the per-alarm response-time values; and whether priority must ever be state-driven. The *carriage* is satisfied-by-product (decision 70): `priority`/`class`/`response_ticks` are declared parameters served through the interface and the `rationalization` block is model data.
  - Class: needs-first-client-decision. Owner: customer (its alarm philosophy, ISA-18.2/IEC 62682-conformant where one exists — alarm-management.md [8, 13]); integrator supplies the rationalization output.
  - Evidence: platform-generated — the vocabulary lands as declared parameters; the master-alarm-database equivalent is the model plus journal (alarm-management.md proposal).
  - Revisit: state-driven priority or an in-model full rationalization record reopens decision 70's framing (see per-requirement conditions).
- **PE-5 · Alarm set and latch policy** (`WW-OPS-002`, `WW-ALM-001`). Confirm the required alarm set — the owner minimum (power failure, pump failure, high/low level, communication fault — pumping-station.md [1 §46, 10, 11]) — and which alarms latch until acknowledged. The only owner datapoint gathered is a latched high-level alarm requiring operator reset (alarm-management.md [10]); the rest is per-alarm customer policy.
  - Class: needs-first-client-decision. Owner: customer.
  - Evidence: platform-generated — latch policy is the per-instance kind choice, model data (alarm-management.md seams; decision 43).
  - Revisit: an alarm list requiring states or delivery beyond the landed lifecycle contract reopens `WW-OPS-002`.
- **PE-6 · Station operating policies** (`WW-CTL-001`, `WW-CTL-002`, `WW-OPS-001`, `WW-LCM-001`). Confirm: the duty-rotation policy — per-cycle, timed, or least-run-hours (pumping-station.md [6, 9] vs [4] vs [1 §42.4, 19]) — and whether standby pumps alternate ([12] vs [4]); whether a failed duty hands over automatically or alarms and waits, and whether the failed unit latches out until reset ([5, 4, 18]); the primary control form — the ordered threshold setpoint chain or continuous constant-level control ([4, 9, 10] vs [2, 8]); the behavior when *all* level sources are bad; and which protections persist in manual takeover ([6]).
  - Class: needs-first-client-decision. Owner: customer (owner policies demonstrably differ).
  - Evidence: platform-generated — each is a declared parameter or kind choice in the model.
  - Revisit: a policy not expressible as the declared rotation/failover/control-form framing reopens the matching requirement.
- **PE-7 · Measurement architecture** (`WW-OPS-003`, `WW-CTL-002`). Confirm the level-measurement architecture: single transmitter plus hardwired float backup (pumping-station.md [6, 10, 20]) versus redundant transmitters with master selection ([5]), and whether inter-transmitter discrepancy alarms. The machinery — `failover-select`, `median-voter`, declared freshness — is satisfied-by-product.
  - Class: needs-first-client-decision. Owner: customer; integrator sizes the field instrumentation.
  - Evidence: platform-generated declared failover/voting wiring; the field-instrument count is deployment data.
  - Revisit: a master-selection rule the existing kinds cannot express reopens `WW-OPS-003`.
- **PE-8 · Mode vocabulary and manual-takeover scope** (`WW-OPS-001`, `WW-CTL-002`). Confirm which equipment modes the operator workflow uses — the reference station needs only the modes its workflow uses; the full vendor-style vocabulary (program, override, maintenance, hand — pumping-station.md [18]) is unconfirmed — and the manual-takeover scope: panel HAND, remote manual over the operator path, or both, with the protections that persist ([6]).
  - Class: needs-first-client-decision. Owner: customer.
  - Evidence: platform-generated mode declarations on the equipment kinds.
  - Revisit: a client requiring the full vendor mode set reopens `WW-OPS-001`.
- **PE-9 · Declared process policies — dosing** (`WW-CTL-003`). Confirm: the primary dosing mode (flow-paced ratio, fixed-rate, or a named diurnal profile — chemical-dosing.md [3, 7]); the declared `on_bad_flow` response and whether it is operator-selectable; whether a clamped demand alarms or only flags; the per-pump actuation contract (speed-only, speed-plus-stroke-servo, run-only) and whether pulse-per-stroke pacing is an input form ([8, 10, 12, 13, 16]); analyzer-trim scope ([4, 9, 17]); dose-confirmation signal ([3, 7, 12, 16]); manual-takeover scope and persisting permissives; the skid alarm set and latch policy; and whether chemical-tank inventory belongs to the dosing contract.
  - Class: needs-first-client-decision. Owner: customer (process and skid vendor data via integrator).
  - Evidence: platform-generated declared parameters; diurnal profile and tank-inventory scope are out-of-scope items below.
  - Revisit: a named diurnal profile reopens decision 54; a required `blower`-like actuation coupling or pulse-pacing input reopens the actuation contract.
- **PE-10 · Declared process policies — filter backwash** (`WW-CTL-004`). Confirm: whether automatic triggers may *start* a wash or only queue a request (both directions are mandated in different jurisdictions — filter-backwash.md [1, 3, 7]); the queue discipline and reorder authority; the queued filter's meanwhile state and the grant permissives the client's hydraulics require; the on-fault step, resume-versus-abort policy, and whether an aborted filter returns to service without a completed wash; whether hold/advance/manual-step commands are required (integrator practice, not owner-evidenced — [23]); the step vocabulary and which step ends are measured versus timed; and the latch policy including the 1 NTU tier's alarm-versus-trip scope ([3]).
  - Class: needs-first-client-decision. Owner: customer (hydraulics and operating rules); integrator confirms equipment pattern per filter.
  - Evidence: platform-generated declared policy (`auto_start`, `queue_policy`, `queued_state`, `on_fault`); the `backwash-sequence` kind and filter-bank composition are recorded open implementation slices (decision 57; `docs/plan.md`).
  - Revisit: a client policy inexpressible as declared data reopens `WW-CTL-004`.
- **PE-11 · Declared process policies — aeration** (`WW-CTL-005`). Confirm: the primary mode and whether ammonia-based supervision or load feed-forward is in scope (aeration-do-control.md [4, 5, 9, 13, 14, 17]); the header strategy and bounds including the most-open-valve band ([5, 19]); the all-DO-bad `on_bad` response and whether it is operator-selectable; the per-zone measurement architecture and whether discrepancy flags alarm; the staging authority, rotation, and per-unit bounds ([4 §6.5.1.2, 6]); the per-blower actuation form and whether any machine couples the composition halves in a way that reopens a `blower` kind ([5 §4.3.1, 6]); the alarm set and latch policy including comm-fail and staging-pending; and whether mixing-pulse coordination is required or specific to pulsed-aeration designs ([7 §3.4]).
  - Class: needs-first-client-decision. Owner: customer.
  - Evidence: platform-generated declared strategy/bounds on `header-coordinator`, `blower-group`, `demand-fallback`; mechanical aerators are out of scope below.
  - Revisit: the aeration note's recorded condition — a client spec fixing the header strategy, DO-loss fallback, or staging authority beyond the declared-parameter framing.

## Phase 2 — Deployment: redundancy, backup/restore, upgrade/rollback, commissioning records

- **DE-1 · Redundancy expectation** (`WW-LCM-001`, `WW-LCM-002`). Confirm whether the client's station class expects controller redundancy at all — plant- and utility-scale owner standards specify hot-standby controllers and servers (deployment-commissioning.md [13, 20, 21]) while small-station specifications emphasize telemetry delivery and a backup control mode over redundant controllers ([22, 23]). Controller redundancy is this product's differentiator, not owner-specified (pumping-station.md assumptions).
  - Class: needs-first-client-decision. Owner: customer.
  - Evidence: the pair machinery itself is satisfied-by-product; the client's stated expectation gates whether `WW-LCM-002` acceptance leans on the pair or on the lone-controller `--state-file` path.
  - Revisit: the recorded `WW-LCM-002` condition — a deployment plan naming redundancy or availability targets.
- **DE-2 · Availability target and failover demonstration** (`WW-LCM-002`). Confirm the availability target, manual promotion versus armed `--auto-promote`, and whether failover must be demonstrated as witnessed availability testing (deployment-commissioning.md [15 §13.2; 17]).
  - Class: needs-first-client-decision. Owner: customer names the target; integrator runs the witnessed test.
  - Evidence: deployment-produced — owner availability tests are witnessed acceptance activities over the platform machinery (gap 7); a platform-generated demonstration artifact is needed only if the client names it.
  - Revisit: a client requiring platform-generated availability evidence reopens gap 7 as tooling work.
- **DE-3 · Upgrade expectation** (`WW-LCM-002`). Confirm whether in-service model revision without process interruption is required, or a scheduled outage plus `--state-file` resume suffices (deployment-commissioning.md [4 B.8.4]). The uninterrupted mechanism — revised build as standby, fingerprint negotiation, carryover report, scan-boundary promote — is satisfied-by-product (decisions 25, 27).
  - Class: needs-first-client-decision. Owner: customer.
  - Evidence: platform-generated for the revision roll; a scheduled-outage path is deployment procedure.
  - Revisit: none beyond the `WW-LCM-002` promotion condition.
- **DE-4 · Backup set and restore** (`WW-LCM-002`). Confirm the backup scope, cadence, retention, off-site/3-2-1 practice, integrity expectations (hashing, write-once), and whether restore must be periodically demonstrated (deployment-commissioning.md [7, 8; 5 SR 7.3]).
  - Class: needs-first-client-decision. Owner: customer policy; integrator runs the procedure; the backup-manifest definition is a platform-owner item (gap 2).
  - Evidence: deployment-produced — sector guidance makes backup creation, isolation, and testing the utility's own program ([7, 8]); the platform contributes the enumerated artifact set (model document + fingerprint, dynamics document, state file, journal file, build identity — proposal).
  - Revisit: a client naming backup obligations the platform must carry promotes `WW-LCM-002`.
- **DE-5 · Rollback semantics** (`WW-LCM-002`). Confirm what rollback means for the client — promote-the-retained-old-build versus restore-from-backup — and whether runtime state must carry backward across a revision (deployment-commissioning.md [4 B.6.7]; gap 3).
  - Class: needs-first-client-decision gated on a platform-owner recorded decision — rollback is not a named direction today (decisions 25, 27 are forward-tolerant only).
  - Owner: platform owner records the semantics; customer confirms the expectation.
  - Evidence: platform-generated where rollback is retained-old-build promotion (existing machinery); deployment-produced where it is backup restore.
  - Revisit: state carryback across the revision boundary is out of scope until the recorded decision lands (gap 3).
- **DE-6 · Commissioning records** (`WW-LCM-002`). Confirm whether owner-witnessed FAT/SAT-style signed reports must be generated by the platform — a scripted witnessed run bound to a model fingerprint — or the scripted rig evidence plus the client's own site tests suffice (deployment-commissioning.md [1, 15, 24]; gap 4); the document-turnover list expected (O&M manuals, calibration records, software backups — [17, 18, 19]); and the force/inhibit handover policy — whether outstanding forces must be enumerated, alarmed, or expired before handover (gap 5; [3, 15]).
  - Class: needs-first-client-decision. Owner: customer names the obligation; integrator produces and signs the records; platform owner only if platform-generated acceptance records are required.
  - Evidence: deployment-produced — witnessed test plans, punch lists, and signed-off reports are external document workflows in every cited source ([1, 15, 16, 24]); commissioning *primitives* (forcing, I/O health, scripted rig) are satisfied-by-product.
  - Revisit: a client naming platform-generated commissioning records promotes `WW-LCM-002` and opens tooling surface (gap 4).
- **DE-7 · Deployment topology** (`WW-LCM-002`). Confirm how many controller pairs/sites a first deployment describes and whether a checked-in topology document is wanted — which would reopen decision 47 (deployment-commissioning.md gap 1; owner practice expects a control-system network diagram among design deliverables — [14 §7.9.1]).
  - Class: needs-first-client-decision. Owner: integrator describes the deployment; platform owner decides the topology-artifact question.
  - Evidence: deployment-produced network diagram; a platform artifact only if the client names it.
  - Revisit: a pair/site count or handover need exceeding URL configuration and per-file artifacts promotes `WW-LCM-002`.

## Phase 3 — Operations: operational and alarm-management duties, response time, escalation, reporting

- **OP-1 · Alarm-management duties and managed-state policy** (`WW-ALM-002`). Confirm which alarms may be shelved and their maximum shelve times (including non-shelvable alarms), whether the operator chooses the shelve period within the bound, shelving authority and how the reason is captured, and the shift-review workflow (alarm-management.md [15; 2 §14.3]); the suppression policy — which alarms are state-dependent; and whether out-of-service is the client's expected alarm state or the equipment maintenance-inhibit mode covers the intent.
  - Class: needs-first-client-decision. Owner: customer (alarm philosophy); integrator applies it per alarm.
  - Evidence: platform-generated for the managed states and journaled transitions; the shift-review *procedure* is deployment-produced.
  - Revisit: requirements beyond the landed managed-state contract — dedicated lifecycle journal events or a shelve command variant — reopen `WW-ALM-002` and decisions 33/38 in narrowed form.
- **OP-2 · Operator response-time expectations** (`WW-ALM-001`). Confirm the per-alarm allowable response times — the rationalization output behind `response_ticks` (alarm-management.md [4]; IEC 62682 §5.4 response timeline — [2]).
  - Class: needs-first-client-decision. Owner: customer with integrator rationalization.
  - Evidence: platform-generated declared parameter.
  - Revisit: none beyond PE-4's state-driven-priority question.
- **OP-3 · Annunciation and escalation for the unattended station** (`WW-ALM-003`). Confirm the site priority names, colours, and audible-annunciation policy; the delivery and escalation expectations — destination, call-out, re-alarming — for the unattended station (alarm-management.md [11; 5 §8]; pumping-station.md [1 §46]); and whether a discrete communication-failure alarm is required beyond the I/O-health surface.
  - Class: needs-first-client-decision. Owner: customer names destinations and policy; integrator builds the notification path.
  - Evidence: deployment-produced — alarm *delivery* is notification machinery the sources place outside the control contract (alarm-management.md gap; deployment-commissioning.md gap 8); priority rendering and first-out order on the operator surface are satisfied-by-product (decisions 70, 74, 75).
  - Revisit: a client requiring in-contract delivery or escalation opens a new surface — recorded out-of-scope below.
- **OP-4 · Flood and performance expectations** (`WW-ALM-004`). Confirm which metrics are acceptance evidence — alarm and peak rate, flood periods, standing and stale counts, chattering and most-frequent rankings, shelving duration, acknowledgment and response time — the site's flood thresholds versus the conventional 10-per-10-minutes bound (alarm-management.md [1 §16, 16]), whether engineered flood suppression is required ([5 §6.7]), and whether metrics must ever be served contract data rather than computed presentation.
  - Class: needs-first-client-decision. Owner: customer.
  - Evidence: platform-generated record — every transition lands in the durable journal and history — with aggregation page-side or deployment-computed (decision 76; decision-37 precedent).
  - Revisit: metrics-as-contract-data reopens `WW-ALM-004`.
- **OP-5 · High-consequence protection boundary** (`WW-ALM-005`). Confirm the hazard-analysis boundary — which functions require an independent protective layer — whether that classification must land as declared model data, and the bypass authority and proof-test workflow expectations (alarm-management.md [21]; decision 77).
  - Class: needs-first-client-decision. Owner: customer (hazard analysis is owner work); integrator declares the layer's state points.
  - Evidence: platform-generated — the layer's availability/activation/fault/bypass/trip/proof-test states are declared journaled field points presented distinctly; the protective function itself stays outside the DCS contract.
  - Revisit: classification-as-model-data reopens `WW-ALM-005`.
- **OP-6 · Reporting obligations** (`WW-REP-001`). Confirm the client's required artifact set, formats, and cadence — state MOR/DMR obligations (operations-reporting.md [3–5]), internal daily/weekly reports ([15]) — and whether any artifact must be platform-generated versus produced in the client's existing historian/WIMS/LIMS layer ([15, 18, 21]); whether a plant historian or data-management product exists to feed; the event/operator record's retention and whether alarm transitions must join the durable record; which period aggregates and derived metrics must be in-contract versus computed downstream and the client's reporting-period boundaries; the maintenance-export form (tag-mapped CMMS points, a periodic file, or the journal alone — [23]); and civil-time anchoring of records across restarts and promotions.
  - Class: needs-first-client-decision. Owner: customer names the obligations; integrator wires historian/CMMS/reporting; platform owner holds the durable-history, period-aggregation, derived-value, and export-mechanism recorded decisions (operations-reporting.md gaps 1–3, 6).
  - Evidence: deployment-produced — the regulator-facing artifacts are produced by a reporting layer downstream of the control system in every cited owner and tool source ([3, 5, 15, 21]); the platform supplies the feedstock (totalizers, counters, checkpointed run-hours, volatile history, durable journal), which is satisfied-by-product at the observable-telemetry level.
  - Revisit: the recorded `WW-REP-001` promotion condition.
- **OP-7 · Telemetry destination and call-out duty** (`WW-OPS-002`, `WW-ALM-003`). Confirm where station alarms are delivered — the staffed-24-hour facility or responsible personnel the standards describe (pumping-station.md [1 §46]) — and who answers. This is the operational duty behind OP-3's machinery question.
  - Class: needs-first-client-decision. Owner: customer.
  - Evidence: deployment-produced notification chain.
  - Revisit: folds into OP-3's in-contract-delivery question.

## Phase 4 — Security and identity: the client's role model and audit-retention rules

- **SE-1 · Role model and action-class mapping** (`WW-SEC-001`). Confirm the client's role model — a named ladder (operator/supervisor/engineer/administrator — security-identity.md [16, 21, 23]), per-point numeric access levels ([16]), or directory-group mapping ([22]) — and which action classes (setpoint writes, tuning, forces, acknowledgment, promotion) map to which tier; owner practice records an access level per setpoint and control ([19, 23]).
  - Class: needs-first-client-decision. Owner: customer.
  - Evidence: platform-generated only if privilege tiers land as declared model data the proxy enforces (security-identity.md gap 1 — a platform-owner recorded decision extending decision 18); otherwise deployment-produced at the fronting layer.
  - Revisit: a client spec naming role tiers on the command surface promotes `WW-SEC-001`.
- **SE-2 · Enforcement boundary** (`WW-SEC-001`). Confirm whether authorization must be enforced inside the product or is satisfied by an authenticating fronting proxy against the site's directory — the decision-48 reopening condition. Authentication machinery lives in site infrastructure in every gathered owner document ([16, 22, 23]), and the owner-program standard states it does not require the IACS itself to carry the technical requirements ([3]).
  - Class: needs-first-client-decision. Owner: customer (specification); platform owner if in-product enforcement is required.
  - Evidence: deployment-produced directory/VPN/MFA enforcement; the platform's share — verified `actor` carried into the journaled record — is satisfied-by-product (decision 48).
  - Revisit: a pilot demonstrating the fronting seam cannot carry a required control promotes `WW-SEC-001`.
- **SE-3 · High-consequence action authority** (`WW-SEC-001`). Confirm whether promotion/demotion, forcing, or alarm acknowledgment must be role-gated, dual-approved, or merely attributed (security-identity.md [1 SR 2.1 RE 3–4]); `promote`/`demote` currently carry no `actor` — the highest-consequence action is the one the journal cannot attribute (gap 2).
  - Class: needs-first-client-decision. Owner: customer policy; the unattributed-switch fix is a platform-owner item (gap 2).
  - Evidence: platform-generated attributed envelopes and journaled receipts once extended; dual approval would be contract work.
  - Revisit: attributed or gated switch requests named in a client spec promote `WW-SEC-001`.
- **SE-4 · Audit retention and integrity** (`WW-SEC-001`, `WW-LCM-002` gap 6, `WW-REP-001`). Confirm the required retention period for the operator-action journal, whether it must be exportable or tamper-evident, and whether a fronting proxy supplies `actor` per decision 48. The sources fix investigatory availability, log protection, and retrievability across format changes (security-identity.md [1 SR 2.9, 6.1; 6 2.T–2.U; 10; 25 §3.7.4]) — not a number; the regulatory 3–10-year periods bind the compliance monitoring record produced *from* the journal (deployment-commissioning.md [10, 11]).
  - Class: needs-first-client-decision. Owner: customer (retention policy and any tamper-evidence requirement); platform owner for the declared retention/export convention and any integrity mechanism (gap 4); integrator runs rotation/archival.
  - Evidence: the durable append-only record and `GET /journal` access are platform-generated and satisfied-by-product; retention handling is deployment-produced procedure (proposal).
  - Revisit: a stated journal-retention or tamper-evidence requirement promotes `WW-SEC-001`.
- **SE-5 · Session obligations at unattended interfaces** (`WW-SEC-001`). Confirm whether unattended-station interfaces need in-product session lock or inactivity logout (security-identity.md [1 SR 2.5–2.7; 22]), or whether the station surface stays exposure-minimized with session policy at the OS/HMI layer.
  - Class: needs-first-client-decision. Owner: customer; in-product sessions would be a platform-owner recorded position (gap 6).
  - Evidence: deployment-produced OS/HMI-layer session policy where required.
  - Revisit: a client requiring in-product session machinery promotes `WW-SEC-001`.
- **SE-6 · Account granularity** (`WW-SEC-001`). Confirm unique individual credentials versus shared role logins — sector guidance and regulation push unique ([5 2.C; 10; 22; 25 §3.8.3]) while one city HMI standard uses shared role logins ([16]) — and, if shared, what the `actor` string attests.
  - Class: needs-first-client-decision. Owner: customer.
  - Evidence: deployment-produced identity practice; the journal records the declared `actor` either way (decision 39).
  - Revisit: none beyond SE-1/SE-2.
- **SE-7 · Security zones and SL-T** (`WW-SEC-001`). Confirm whether the client assigns IEC 62443 target security levels per zone (security-identity.md [2]) and, if so, which SL-T the controller's zone must meet — that answer decides how much of FR 1/FR 2 is the product's versus compensating measures ([3]). Remote-access paths (VPN into the station router, directory logins) are site infrastructure ([22, 23]).
  - Class: needs-first-client-decision. Owner: customer (owner risk process assigns SL-T); integrator draws the conduits; the documented conduit-boundary list is the landed platform-owner documentation item — `docs/conduit-boundaries.md` (gap 8 closed as documentation).
  - Evidence: deployment-produced zone/conduit design; the platform's externally reachable transports — monitor HTTP, peer checkpoint link, remote-driver plant link, sim-bus register protocol, EtherCAT cyclic binding — are the named partition points `docs/conduit-boundaries.md` enumerates.
  - Revisit: an assigned SL-T promotes `WW-SEC-001`.

## Phase 5 — Engineering scale: whether bulk machinery is needed at the first client's point count

- **SC-1 · Point and station count** (`WW-ENG-002`). Confirm the first plant's point count and station count — the scale gate itself: a reference station runs tens of points where per-point engineering is tolerable, and no gathered source answers the count for a first client (bulk-engineering.md assumptions).
  - Class: needs-first-client-decision. Owner: customer/integrator (deployment facts).
  - Evidence: platform-generated — the emitted model and signal index already enumerate the point set.
  - Revisit: the recorded `WW-ENG-002` condition — a plant-scale model making per-point engineering demonstrably unworkable.
- **SC-2 · Template mechanism and provenance** (`WW-ENG-002`). Confirm whether templates must be in-model, client-editable declarations or remain Rust builder code, and whether type→instance provenance inside the document is required for the client's review workflow (bulk-engineering.md [3 §5.6, 4 §6.3.6, 25, 26]; gap 3).
  - Class: needs-first-client-decision gated on scale. Owner: customer workflow preference; in-model templates or in-document provenance are platform-owner contract work.
  - Evidence: platform-generated where provenance lands as generated-by metadata (proposal); builder-side templates are satisfied-by-product (`pumping_station` composition helper — decision 31).
  - Revisit: the `WW-ENG-002` promotion condition.
- **SC-3 · Bulk-edit seam** (`WW-ENG-002`). Confirm whether bulk edit means regenerate-from-builder plus `dcs-model diff` review — the proposed answer matching owner document control (bulk-engineering.md [14]) — or a distinct apply-patch operation on the emitted document (gap 4).
  - Class: needs-first-client-decision gated on scale. Owner: platform owner (recorded decision) once the client names a bulk-editing need.
  - Evidence: platform-generated diff as the review record.
  - Revisit: the `WW-ENG-002` promotion condition.
- **SC-4 · Per-tag-class checkout evidence** (`WW-ENG-002`). Confirm whether per-tag-class checkout records — loop-check forms, inspection test certificates, C&E runs — must be platform-generated (a scripted run bound to a model fingerprint) or the client's own site tests suffice (bulk-engineering.md [8, 13, 23]; gap 5).
  - Class: needs-first-client-decision. Owner: customer names the obligation; integrator produces the records; platform owner only if generation is required.
  - Evidence: deployment-produced in every cited owner source; forcing plus I/O health are the platform's checkout primitives.
  - Revisit: mirrors DE-6's platform-generated-records question.
- **SC-5 · Site count and topology document** (`WW-LCM-002`). Cross-reference DE-7: the pair/site count a deployment topology must describe and whether a checked-in topology artifact is wanted.
  - Class: needs-first-client-decision. Owner and evidence as DE-7.
  - Revisit: the `WW-LCM-002` promotion condition.

## Out of scope for the first pilot — with revisit conditions

- **P&ID and loop-drawing interop** (`WW-ENG-002` gap 7; IEC 62424 CAEX exchange — bulk-engineering.md [4]). Recorded out of scope. Revisit if a client names P&ID-tool integration.
- **Certified electronic submission** (`WW-REP-001`; NetDMR/state eDMR under 40 CFR part 127 — operations-reporting.md [3]). Permanently downstream of the control system in every cited source. Revisit only if a client names it in scope.
- **In-contract alarm delivery and call-out** (`WW-ALM-003`; alarm-management.md gap; deployment-commissioning.md gap 8). Notification machinery is deployment integration. Revisit if a client requires it in-contract.
- **Diurnal profile dosing** (`WW-CTL-003`; decision 54). Specified third form, deferred. Revisit when a customer names it.
- **Mechanical aerators** (`WW-CTL-005`; decision 67). Plain `motor` compositions cover them. Revisit if the first client runs them.
- **State carryback across a rollback revision** (`WW-LCM-002` gap 3). Out of scope until the rollback-semantics recorded decision lands (DE-5).
- **In-product session machinery** (`WW-SEC-001` gap 6). Session policy stays at the OS/HMI layer. Revisit if a client requires in-product session lock (SE-5).
- **In-product read-side access control** (`WW-SEC-001` gap 5). Monitor GETs are unauthenticated today; grading lives at the proxy. Revisit if a client requires in-product read restriction.
- **Metrics as served contract data** (`WW-ALM-004`). Aggregation stays computed over the durable record. Revisit if a client names metrics as contract data (OP-4).
- **Chemical-tank inventory in the dosing contract** (`WW-CTL-003`). Revisit if the client scopes tank management into dosing rather than a separate component (PE-9).
- **The protective function itself** (`WW-ALM-005`; decision 77). An independent protection layer is never the DCS contract; only its declared state presentation is. No revisit — hazard analysis is owner work.

## Revisit conditions per requirement ID

| Requirement | Revisit condition |
|---|---|
| `WW-OPS-001` | Revisit if the client's operator workflow requires the full vendor-style mode vocabulary (program, override, maintenance, hand — pumping-station.md [18]) beyond the declared modes (PE-8). |
| `WW-OPS-002` | Revisit if the client's alarm set or lifecycle expectations exceed the landed two-flag plus managed-state contract — e.g. dedicated lifecycle journal events (OP-1). |
| `WW-OPS-003` | Revisit if the client's measurement architecture requires selection semantics `failover-select`/`median-voter` cannot express (PE-7). |
| `WW-CTL-001` | Revisit if the client's rotation, handover, or latch-out policy is not expressible as the pump group's declared policy (PE-6). |
| `WW-CTL-002` | Revisit if the client requires a control form or an all-sources-bad behavior outside the declared threshold-chain/PID plus declared-backup framing (PE-6). |
| `WW-CTL-003` | Revisit for a named diurnal-profile requirement (decision 54), a pulse-per-stroke pacing input, tank-inventory scope, or an actuation coupling the `motor` + `analog-output` composition cannot express (PE-9). |
| `WW-CTL-004` | Revisit if the client's auto-start permission, queue discipline, or on-fault policy is not expressible as declared data, or if hold/advance/manual-step is required (decision 59 records it open; PE-10). |
| `WW-CTL-005` | Recorded condition (aeration-do-control.md): revisit if a first-client specification fixes the header strategy, the DO-loss fallback, or the staging authority in a way the declared-parameter framing cannot express (PE-11). |
| `WW-ALM-001` | Revisit if priority must be state-driven — a status port rather than declared metadata — or the full rationalization record must live inside the model (PE-4). |
| `WW-ALM-002` | Revisit if the client requires lifecycle journal events beyond `PointChanged` or a dedicated shelve command — narrowed reopens of decisions 33/38 (OP-1). |
| `WW-ALM-003` | Revisit if alarm delivery, call-out, or escalation must be in-contract rather than deployment integration (OP-3, OP-7). |
| `WW-ALM-004` | Revisit if metrics must be served contract data rather than computed over the durable record (OP-4). |
| `WW-ALM-005` | Revisit if the hazard-analysis classification must land as declared model data (OP-5). |
| `WW-ENG-001` | No open pilot item; partially implemented composition continues under library tickets. |
| `WW-LCM-001` | Revisit if the client's availability target or topology exceeds one pair per model — joins `WW-LCM-002` items DE-1 and DE-7. |
| `WW-ENG-002` | Recorded condition: promote when a first-client specification names an I/O-list or tag-database deliverable format, an owner tag convention the model must carry, or a deliverable set the platform must generate — or when a multi-station or plant-scale model makes per-point engineering demonstrably unworkable (PE-2, PE-3, SC-1–SC-4). |
| `WW-LCM-002` | Recorded condition: promote when a first-client deployment plan or owner specification names lifecycle obligations — redundancy or availability targets, backup-and-restore scope and restore testing, uninterrupted-upgrade or rollback expectations, witnessed commissioning records, or audit-retention requirements — or when a pilot's topology and handover needs exceed URL configuration and the per-file artifacts (DE-1–DE-7). |
| `WW-SEC-001` | Recorded condition: promote when a first-client security specification or applicable regulation names obligations the platform itself must carry — role tiers on the command surface, attributed or gated switch requests, a stated journal-retention or tamper-evidence requirement, or an assigned SL-T — or when a pilot deployment demonstrates the decision-48 fronting seam cannot carry a required control (SE-1–SE-7). |
| `WW-REP-001` | Recorded condition: promote when a first-client specification names a reporting obligation the platform itself must carry — a periodic report artifact or export format, a historian-feed or CMMS-export contract, a durable recording duty for named points, a journal retention/integrity requirement, or in-contract derived metrics — or when `WW-CTL-004`/`WW-CTL-005` implementation needs durable process history or computed derived values (OP-6). |

## Dispatch summary

- **Customer lane** — policy answers to collect before engineering and deployment: PE-1–PE-11 (documents, conventions, declared process policies), DE-1–DE-4 (lifecycle expectations), OP-1–OP-7 (alarm and reporting policy), SE-1, SE-3–SE-7 (identity and retention policy), SC-1 (scale facts).
- **Integrator lane** — deployment-produced artifacts and procedures: deliverable documents and I/O lists (PE-1, PE-3), witnessed tests and loop checks (DE-2, DE-6, SC-4), the backup/restore procedure (DE-4), notification and call-out integration (OP-3, OP-7), historian/CMMS wiring (OP-6), zone/conduit deployment (SE-7), session policy at the OS/HMI layer (SE-5).
- **Platform-owner lane** — recorded decisions or contract work the customer answers gate: tag carriage and I/O-list export (PE-2, PE-3), the backup manifest and rollback semantics (DE-4, DE-5), platform-generated commissioning and checkout records if named (DE-6, SC-4), the topology artifact (DE-7, SC-5), durable history, period aggregation, derived values, and export (OP-6), privilege-tier declaration, promote/demote attribution, the retention/export convention, and the session position (SE-1–SE-5), in-model templates and the bulk-edit seam (SC-2, SC-3).
