# Water and wastewater requirements

This is the initial requirements baseline for the first target market. It is intentionally narrower than a complete water-industry DCS specification. The simulated reference plant is a duty/standby pumping station with a wet well or tank, level measurement, discharge measurement, motor feedback, permissives, trips, and operator controls.

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
- **Acceptance evidence:** The reference pumping station demonstrates these states end to end for its pumps and valves, including descriptor-driven faceplates and receipted commands.

### WW-OPS-002 — Alarm and event handling

- **Status:** accepted
- **Need:** Abnormal process and equipment conditions are visible with lifecycle state, timestamps, acknowledgment, and actor attribution sufficient for an operator to understand what happened.
- **Acceptance evidence:** The reference application generates representative process and equipment alarms, exposes current alarm state and transition history, and preserves the durable journal across a controller restart.

### WW-OPS-003 — Signal and communication confidence

- **Status:** accepted; partially implemented
- **Need:** Operators and control components can distinguish healthy, uncertain, bad, stale, and deliberately substituted data, and can see field-communication and scan-health problems separately from process alarms.
- **Acceptance evidence:** Simulated sensor faults, link loss, and forces propagate through control behavior, telemetry, diagnostics, and UI with deterministic recovery behavior.

### WW-CTL-001 — Pump duty management

- **Status:** accepted
- **Need:** An engineer can compose a pump group that selects duty and standby pumps, rotates duty by declared policy, starts additional capacity on demand, respects availability and permissives, and handles a running pump failure.
- **Acceptance evidence:** A deterministic simulation covers normal alternation, unavailable duty pump, failed start or run feedback, assist demand, and return to normal capacity.

### WW-CTL-002 — Level-based station control

- **Status:** accepted
- **Need:** The reference station controls level using declared start and stop thresholds or a continuous controller, with explicit behavior for bad level measurement and manual operation.
- **Acceptance evidence:** Scenarios cover normal inflow, high and low levels, changing inflow, bad measurement, manual takeover, and recovery without an unintended output step.

### WW-ENG-001 — Reusable plant composition

- **Status:** accepted; partially implemented
- **Need:** An engineer composes a plant from typed reusable components and receives validation errors for missing parameters, incompatible ports, invalid mappings, and incomplete operator metadata before deployment.
- **Acceptance evidence:** The reference station is produced through the typed build path and passes model validation, engineering lint, assembly, and full-stack simulation.

### WW-LCM-001 — Controller continuity and recovery

- **Status:** accepted; partially implemented
- **Need:** A water plant can continue control across controller takeover and recover state after restart, while the operator sees one logical controller and the health of its redundant peers.
- **Acceptance evidence:** The reference application demonstrates bumpless promotion, state and runtime-tuning continuity, durable operator-event continuity, and named rejection of incompatible state.

## Candidate requirements needing evidence

These subjects are likely relevant but are not implementation authority yet:

- `WW-ALM-001`: alarm priorities, shelving, suppression, out-of-service state, and flood handling;
- `WW-CTL-003`: chemical dosing with flow pacing, ratio limits, permissives, and totalization;
- `WW-CTL-004`: filter sequencing, backwash coordination, and shared-resource arbitration;
- `WW-CTL-005`: aeration or dissolved-oxygen control;
- `WW-ENG-002`: bulk engineering, templates, naming conventions, and site-specific parameter sets;
- `WW-LCM-002`: deployment topology, backup and restore, version upgrades, rollback, and commissioning workflow;
- `WW-SEC-001`: identities, roles, authentication, authorization, audit retention, and security-zone integration;
- `WW-REP-001`: operating reports, regulatory records, energy metrics, and maintenance exports.

Each candidate needs a research note containing primary-source evidence and explicit customer-validation questions before the planner creates implementation tickets.
