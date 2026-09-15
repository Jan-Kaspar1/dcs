# Water and wastewater requirements

This is the initial requirements baseline for the first target market. It is intentionally narrower than a complete water-industry DCS specification. The simulated reference plant is a duty/standby pumping station with a wet well or tank, level measurement, discharge measurement, motor feedback, permissives, trips, and operator controls.

Station operating-policy evidence and open assumptions are recorded in `docs/research/pumping-station.md` (issue #195); the requirement entries below cite it where the sources sharpen or qualify the acceptance evidence. Alarm-lifecycle evidence for the `WW-ALM-001` candidate is recorded in `docs/research/alarm-management.md` (issue #234).

## Accepted foundation requirements

### WW-FND-001 — One plant contract

- **Status:** accepted; substantially implemented
- **Need:** An engineer declares devices, signals, relationships, control components, and display metadata once. The controller and operator UI consume the same versioned model.
- **Acceptance evidence:** A checked-in reference model validates, assembles, runs in simulation, and supplies the metadata used by its monitoring UI without a second hand-maintained UI configuration.

### WW-FND-002 — Hardware-independent control

- **Status:** accepted; substantially implemented
- **Need:** Water library logic addresses typed logical I/O. A plant mapping selects simulation or a field-interface driver without changing component logic.
- **Acceptance evidence:** The reference application runs unchanged against at least two registered driver kinds, with mapping and type failures rejected before the first scan.

### WW-OPS-001 — Consistent equipment operation

- **Status:** accepted
- **Need:** An operator can understand and control common equipment through consistent modes, status, commands, permissives, interlocks, trips, feedback discrepancy, and unavailable states.
- **Acceptance evidence:** The reference pumping station demonstrates these states end to end for its pumps and valves, including descriptor-driven faceplates and receipted commands. Per `docs/research/pumping-station.md`, evidence supports: per-pump mode selection (automatic, manual, and an out-of-service/maintenance inhibit), permissive gating of starts, interlock trips, failed-feedback flags after a declared delay, and an aggregated "available" state per pump. The full vendor-style mode vocabulary (program, override, maintenance, hand) is an open assumption pending customer validation — the reference station needs only the modes its operator workflow uses.

### WW-OPS-002 — Alarm and event handling

- **Status:** accepted
- **Need:** Abnormal process and equipment conditions are visible with lifecycle state, timestamps, acknowledgment, and actor attribution sufficient for an operator to understand what happened.
- **Acceptance evidence:** The reference application generates representative process and equipment alarms, exposes current alarm state and transition history, and preserves the durable journal across a controller restart. The research note sharpens the representative set: high and low wet-well level, per-pump fail-to-start and fail-to-stop, measurement fault, backup-control-mode engagement, and station power/communication faults — each a latching alarm with a standing flag, an unacknowledged latch, and a journaled acknowledgment. Alarm priorities, shelving, suppression, and classes remain deferred (decision 38; feeds `WW-ALM-001` — lifecycle evidence and the seam mapping are in `docs/research/alarm-management.md`); which alarms must latch until acknowledged is a recorded customer-validation question.

### WW-OPS-003 — Signal and communication confidence

- **Status:** accepted; partially implemented
- **Need:** Operators and control components can distinguish healthy, uncertain, bad, stale, and deliberately substituted data, and can see field-communication and scan-health problems separately from process alarms.
- **Acceptance evidence:** Simulated sensor faults, link loss, and forces propagate through control behavior, telemetry, diagnostics, and UI with deterministic recovery behavior. Station-facing sharpening from the research note: level measurement uses redundant sources with a declared failover — a bad or lost primary measurement switches the station to a declared backup (secondary transmitter or float-backup mode) and raises an alarmed transition rather than silently controlling on bad data. The single-primary-plus-float-backup versus multi-transmitter master-selection choice is a customer-validation question.

### WW-CTL-001 — Pump duty management

- **Status:** accepted
- **Need:** An engineer can compose a pump group that selects duty and standby pumps, rotates duty by declared policy, starts additional capacity on demand, respects availability and permissives, and handles a running pump failure.
- **Acceptance evidence:** A deterministic simulation covers normal alternation, unavailable duty pump, failed start or run feedback, assist demand, and return to normal capacity. Evidence-supported specifics from the research note: the rotation policy is a declared choice — alternate each cycle, alternate on a timed interval, or least-run-hours first — because owner policies demonstrably differ; a lag pump stages at its own start level when the duty pump cannot hold the level; a failed or unavailable duty pump hands duty to the standby; start/stop sequences respect a declared inter-pump start delay and engineered starts-per-hour limits. Open assumptions to validate: the first client's default rotation policy, whether failure hands over automatically or alarms-and-waits, and whether a failed pump latches out until reset.

### WW-CTL-002 — Level-based station control

- **Status:** accepted
- **Need:** The reference station controls level using declared start and stop thresholds or a continuous controller, with explicit behavior for bad level measurement and manual operation.
- **Acceptance evidence:** Scenarios cover normal inflow, high and low levels, changing inflow, bad measurement, manual takeover, and recovery without an unintended output step. Evidence-supported specifics from the research note: the threshold form is an ordered setpoint chain — low cut-off, duty start/stop, lag start, high-level alarm — with operator-adjustable setpoints; the continuous form is constant-level control for variable-speed stations with a minimum-speed cycling bound; a bad primary measurement fails over to a declared backup mode or voted source with an alarmed transition; manual takeover is a per-pump mode that persists declared protections (e.g., motor over-temperature) while the operator holds the demand. Open assumptions to validate: which control form is primary for the first client, behavior when all level sources are bad, and which protections persist in manual.

### WW-CTL-003 — Chemical dosing and flow-paced control

- **Status:** accepted
- **Need:** An engineer can compose a dosing function that paces a metering pump to a measured process flow at an operator-set dose, bounds the demand within declared dose and rate limits, respects declared permissives and interlocks, accounts for chemical consumed, and responds predictably to a lost pacing signal or a proven pump fault.
- **Acceptance evidence:** A simulated dosing loop demonstrates: flow-paced ratio control delivering the declared dose per flow unit across the flow range; the demand clamped at declared minimum and maximum dose and pump-rate limits with a clamped indication; dosing inhibited until declared permissives hold (process flow proven, metering pump available, chemical tank above the low level) and dropped to the safe value on loss of any permissive; the declared response to a bad pacing-flow signal — stop, hold, or fallback rate — taken with an alarmed transition; consumption totalized from the commanded or measured chemical rate; and the declared skid alarm set raised. Evidence-supported specifics from `docs/research/chemical-dosing.md`: flow-paced ratio control is the primary mode owner specifications require — dose per flow unit (e.g., mg/L) times measured flow — with fixed-rate as a declared alternative for steady-flow service and diurnal profile dosing as a specified third form; feedback trim by a residual or quality analyzer is an optional compound layer over pacing; actuation is a speed demand or an on/off run command, with stroke length an engineering/operator setting rather than a control output; loss of any dosing permissive stops the pump and inhibits restart; a proven no-discharge or pump fault alarms and can hand over to a standby pump. Open assumptions to validate: the first client's primary mode, the declared bad-flow-signal behavior, whether analyzer trim is in scope, and the required skid alarm set and latch policy.

### WW-CTL-004 — Filter sequencing and backwash coordination

- **Status:** accepted
- **Need:** An engineer can compose a bank of N granular-media filters, each stepping through a declared backwash sequence against a shared air/water backwash supply that serves one filter at a time, with declared trigger sources, per-step advance conditions, exclusive-grant arbitration among contending filters, declared mid-sequence failure behavior, operator intervention, and a declared alarm set.
- **Acceptance evidence:** A deterministic filter-bank simulation demonstrates: a backwash request raised by each declared trigger — elapsed filter run time, terminal headloss, effluent turbidity above its bound, and operator start — with the firing trigger recorded per backwash; the declared step sequence advancing on time and on measured state per the step declaration (drain-down ending on level reached, wash phases on declared durations, filter-to-waste ending on a turbidity and/or volume bound); each step driving its declared valve/pump/blower pattern subject to phase-internal guards (e.g., no simultaneous air scour and high-rate wash); at most one filter in backwash at a time, contending requests ordered by the declared queue policy with the grant conditional on declared capacity permissives (washwater supply available, waste capacity, online-flow disturbance bound) and released on completion or abort; a queued filter in its declared meanwhile state (keep filtering until granted, or offline with standby cover); operator start, abort, and queue reorder through the receipted command path; a proven equipment fault mid-sequence driving the filter to its declared on-fault step and raising an aborted status distinct from complete; and the declared alarm set raised (per-filter effluent turbidity tiers, terminal headloss, aborted/failed backwash, resource-blocked queue, post-wash clean-bed-headloss and ripening excursions). Evidence-supported specifics from `docs/research/filter-backwash.md`: the canonical sequence is timed cleaning phases bounded by measured-state steps (drain-to-level, filter-to-waste-to-turbidity/volume) with an operator-adjustable resting period; the trigger set — run time, headloss, effluent turbidity, operator start — is owner-convergent while whether automatic triggers may *start* a wash versus only queue a request is jurisdiction-specific (both directions are mandated in different rules), so auto-start permission is itself declared; an ordered backwash queue with one-at-a-time execution and operator reorder is owner-evidenced (FIFO default); operator abort and override are required vocabulary. Open assumptions to validate: the queue discipline and reorder surface, the queued filter's meanwhile state and grant permissives, the on-fault step and resume-versus-abort policy, whether hold/advance/manual-step operator commands are required, the client's step vocabulary, and which alarms latch until acknowledged.

### WW-ENG-001 — Reusable plant composition

- **Status:** accepted; partially implemented
- **Need:** An engineer composes a plant from typed reusable components and receives validation errors for missing parameters, incompatible ports, invalid mappings, and incomplete operator metadata before deployment.
- **Acceptance evidence:** The reference station is produced through the typed build path and passes model validation, engineering lint, assembly, and full-stack simulation.

### WW-LCM-001 — Controller continuity and recovery

- **Status:** accepted; partially implemented
- **Need:** A water plant can continue control across controller takeover and recover state after restart, while the operator sees one logical controller and the health of its redundant peers.
- **Acceptance evidence:** The reference application demonstrates bumpless promotion, state and runtime-tuning continuity, durable operator-event continuity, and named rejection of incompatible state. Station-facing sharpening from the research note (a proposal — sources treat stations as unattended with telemetry and do not specify controller redundancy): duty-rotation position, accumulated run hours, unacknowledged alarm latches, and tuned setpoints must carry across promotion and restart so the duty assignment and operator-visible state survive a takeover.

## Candidate requirements needing evidence

These subjects are likely relevant but are not implementation authority yet:

- `WW-ALM-001`: alarm priorities, shelving, suppression, out-of-service state, and flood handling — still candidate. `docs/research/alarm-management.md` (issue #234) maps the standards' lifecycle (ISA-18.2 / IEC 62682) onto the implemented two-flag model and the decision-38 per-kind extension seam: the normal/acknowledged/returned-to-normal axis, per-alarm latch policy, designed and state-based suppression through `in`-gating wiring, priority and class as declared metadata, shelving as a per-kind sibling, and flood/performance metrics over the journal and history all land without contract work; named managed-state visibility, dedicated lifecycle journal events, a shelve/suppress command variant, and an in-model rationalization record are the items that would need it. Promotion stays withheld: the owner evidence for managed states, priorities, and flood handling sits at alarm-program scale (a utility-wide ISA-18.2 alarm standard; a citywide network's documented flood problem), while small-station specifications require the alarm set and its telemetry delivery — so no observable acceptance criteria are justified yet.
  - Open assumptions for customer validation (enumerated in the note): whether the client runs an ISA-18.2/IEC 62682 alarm philosophy at all; which alarms must latch until acknowledged; the required priority scheme; shelving policy, authority, and per-alarm bounds if shelving is wanted; which alarms are state-dependent; out-of-service versus the equipment maintenance-inhibit mode; flood-monitoring versus flood-suppression expectations; and alarm delivery/escalation for unattended stations.
  - Revisit condition: promote when a first-client alarm philosophy or owner specification names managed-state handling (shelving, suppression, out-of-service), priority classes, or flood handling as required — or when the reference station's acceptance scenario demonstrates a nuisance-alarm or flood condition the two-flag model cannot make observable;
- `WW-CTL-005`: aeration or dissolved-oxygen control;
- `WW-ENG-002`: bulk engineering, templates, naming conventions, and site-specific parameter sets;
- `WW-LCM-002`: deployment topology, backup and restore, version upgrades, rollback, and commissioning workflow;
- `WW-SEC-001`: identities, roles, authentication, authorization, audit retention, and security-zone integration;
- `WW-REP-001`: operating reports, regulatory records, energy metrics, and maintenance exports.

Each candidate needs a research note containing primary-source evidence and explicit customer-validation questions before the planner creates implementation tickets.
