# Architecture decisions

## Bootstrap baseline

The repository begins as a Rust workspace with a `dcs-core` library reserved for shared contracts. No controller or plant-model API has been chosen yet. Python standard-library tooling coordinates development; it is not the plant runtime.

The project vision in `AGENTS.md` is the source for product decisions. All initial I/O is simulated, and development excludes physical plant equipment and live deployment.

## Recording decisions

When choosing a shared contract, module boundary, persistence format, or runtime behavior, the planner records the problem, chosen approach, alternatives, consequences, and affected tickets here. Changes are committed and pass the same CI gate as implementation. Accepted decisions must distinguish implemented behavior from proposed behavior.

## Decisions

### 1. Workspace crate layout

- **Status:** Accepted. `dcs-core` is implemented; `dcs-model`, `dcs-runtime`, `dcs-sim`, and `dcs-blocks` are proposed and do not exist yet.
- **Problem:** The system needs crate boundaries so that shared contracts, the plant model, the executor, simulated I/O, and reusable control components can evolve independently with an explicit dependency direction, instead of growing as one tangled crate.
- **Chosen approach:** One Cargo workspace. `dcs-core` holds shared contracts (signal types, quality, component and I/O traits). Planned members: `dcs-model` (plant model types and serialization), `dcs-runtime` (cyclic executor and component model), `dcs-sim` (simulated I/O backend), and `dcs-blocks` (reusable control components). Dependencies point inward: `dcs-model`, `dcs-runtime`, `dcs-sim`, and `dcs-blocks` may depend on `dcs-core`; `dcs-core` depends on none of them.
- **Alternatives considered:** A single monolithic crate (simpler, but couples contract stability to every feature); one crate per device or per component (fragments the workspace and multiplies publishing/versioning overhead).
- **Consequences:** Shared contracts change in one place and downstream crates break visibly at compile time. Each planned crate must be added as a workspace member before use. Component and test code can depend only on the layers it needs, keeping builds incremental.
- **Affected tickets:** #4 (this record); the planned M1 tickets that create `dcs-model`, `dcs-runtime`, `dcs-sim`, and `dcs-blocks`.

### 2. Signal representation

- **Status:** Accepted and implemented in `dcs-core` (issue #5): `Sample { value: Value, quality: Quality, tick: Tick }` with `Value` = `Bool`/`Int`/`Float`, `Quality` = `Good`/`Uncertain`/`Bad` carrying a `QualityReason`, and `Tick` a logical `u64` counter.
- **Problem:** Control logic and the monitoring UI need one uniform way to represent a process value that may be good, bad, stale, or substituted, and to know when it was sampled — bare scalars cannot express this.
- **Chosen approach:** A signal is a typed value plus a quality flag plus a timestamp: conceptually `Signal<T> { value: T, quality: Quality, timestamp: Tick }`, defined in `dcs-core`. `Quality` starts as a small enum (good, bad, uncertain/substituted) and may grow. The timestamp is a virtual tick count (see decision 4), not wall-clock time.
- **Alternatives considered:** Bare typed values with no quality (loses diagnostics the vision requires); OPC UA-style status codes and source/server timestamps (broad but heavyweight for a first contract); bespoke per-signal structs (no uniform handling in the executor or UI).
- **Consequences:** Every logical I/O point carries quality, so components must define behavior for non-good inputs and the executor can propagate bad quality to outputs. Timestamps are comparable only within a run's tick domain, which keeps tests deterministic.
- **Affected tickets:** #4; the M1 ticket implementing `dcs-core` signal contracts, and every later component and driver ticket.

### 3. Plant model serialization format

- **Status:** Accepted as proposed behavior; `dcs-model` and its schema do not exist yet.
- **Problem:** The plant model is the single contract shared by engineering data, controllers, and the monitoring UI. It needs a serde-based on-disk format that is versioned, diffable, and readable by tooling outside the Rust workspace.
- **Chosen approach:** JSON via `serde`/`serde_json`. Every document carries an explicit top-level `version` field; the loader accepts only known versions and migration happens on load. The plant model is treated as an engineered artifact, typically produced and consumed by tools rather than hand-edited.
- **Alternatives considered:** TOML — friendly for hand-editing and comments, but awkward for the deeply nested device/signal/mapping lists the model needs and less natural for a web-based monitoring UI. RON — Rust-native with comments, but essentially no non-Rust tooling, which undermines the model's role as a cross-language contract.
- **Consequences:** Any tool that can parse JSON can read or generate the model, including the future monitoring UI. Lack of comments means explanatory text lives in model fields, not alongside the data. All model documents must state a version, so schema changes are explicit rather than silently ambiguous.
- **Affected tickets:** #4; the M1 ticket implementing `dcs-model`, and every later ticket that loads or emits plant configuration.

### 4. Execution model

- **Status:** Accepted as proposed behavior; `dcs-runtime` does not exist yet.
- **Problem:** The controller must execute reproducibly: the same plant model and input sequence must produce the same outputs on any host, and tests must be able to step through runs without real-time waits.
- **Chosen approach:** A deterministic fixed-step scan. Each cycle reads all inputs, steps components in a defined order, then writes all outputs. Time is a virtual tick counter advanced by the executor; components never read a wall clock. Pacing a run to real time, when needed, is the outer driver's concern, not the components'.
- **Alternatives considered:** Event-driven or `async`-task execution (flexible but ordering and reproducibility become scheduling-dependent); wall-clock timers inside components (nondeterministic, untestable); variable-step execution (breaks fixed-scan assumptions familiar from PLC runtimes).
- **Consequences:** Runs are deterministic and replayable; `dcs-sim` and tests can advance the executor one tick at a time. Components must express delays and periods in ticks. Real-time jitter is isolated to the pacing layer and cannot leak into control logic.
- **Affected tickets:** #4; the M1 ticket implementing `dcs-runtime`, and all component and simulation tickets.

### 5. I/O abstraction

- **Status:** Accepted. The contract side is implemented in `dcs-core` (issue #6): `IoDriver` provides untyped `read`/`write` over `PointId` returning `Result<_, IoError>` (unknown point, disconnected, timeout, type mismatch — each carrying the point identity), and `Input<T>`/`Output<T>` handles give components typed logical I/O with strict, never-coercing value matching. Concrete drivers do not exist yet.
- **Problem:** Per `AGENTS.md`, control logic must be hardware-independent: the same component must work whether a point is backed by local I/O, EtherCAT, Ethernet, or simulation, without code changes.
- **Chosen approach:** Components declare typed logical I/O points — a name, a signal type (decision 2), and a direction. Binding a logical point to a physical device channel lives in the plant model (decision 3); resolving that binding and talking to field hardware lives in drivers beneath `dcs-runtime`. Component code never names a device, bus, or address.
- **Alternatives considered:** Injecting driver handles into components (couples logic to a driver API); per-component mapping config (duplicates mapping outside the plant model, breaking the single-contract goal); a global string-keyed I/O registry (loses compile-time typing).
- **Consequences:** Components are portable and fully testable against `dcs-sim`. Rewiring hardware is a plant-model edit, not a code change. Mapping errors surface at model load or driver initialization rather than mid-scan.
- **Affected tickets:** #4; the M1 tickets implementing `dcs-core` I/O traits, `dcs-model` mapping, `dcs-runtime` drivers, and `dcs-sim`.

### 6. Model-driven assembly

- **Status:** Accepted as proposed behavior; `dcs-assembly` and `dcs-blocks` do not exist yet. What exists: `PlantModel::load` yields a validated model (#7), `SimDriver` is built from a `ChannelMap` (#8), and `Executor::new` wires `Vec<Box<dyn Component>>` against a `PointMap` (#9). Nothing yet translates a model into those runtime inputs.
- **Problem:** The plant model declares component instances by opaque `kind` string plus parameters and port wiring, while the executor takes constructed components and a resolved point map. Something must turn a validated model into a configured driver and executor — and that translation has to live in a crate whose dependency direction respects decision 1.
- **Chosen approach:** A new workspace crate `dcs-assembly`, depending on `dcs-core`, `dcs-model`, `dcs-runtime`, and `dcs-sim`. It provides a component registry mapping each `ComponentInstance.kind` string to a constructor that receives the instance's parameters and resolved port-to-point bindings and returns `Box<dyn Component>`; reusable components from `dcs-blocks` register their kinds there. A builder takes a validated `PlantModel`, resolves devices into a driver-side `ChannelMap` and `SimDriver`, resolves `io_points` into the executor's `PointMap` (synthesizing internal points for port-to-port connections), constructs each component through the registry, and returns the configured executor and driver. Registration is explicit: an unknown kind is a build error naming the component instance, raised before any scan runs.
- **Alternatives considered:** Construction inside `dcs-runtime` (couples the deterministic executor to the model schema); inside `dcs-model` (inverts the dependency direction — the serialization contract would depend on the runtime); global self-registration via link-time tricks like `inventory`/`linkme` (component kinds silently missing when a translation unit is not linked); assembly code in each binary (duplicates the model-to-runtime logic across binaries and tests).
- **Consequences:** Adding a device or component kind means registering a constructor — instantiation, wiring checks, and diagnostics come from the shared layers instead of per-layer engineering. Model instantiation errors surface at build time, before the first scan. `dcs-model` stays serialization-only and `dcs-runtime` stays model-agnostic, so both remain independently testable.
- **Affected tickets:** #18 (this record); the M2 tickets creating `dcs-assembly`, the component registry, and `dcs-blocks` components that register with it.

### 7. Operator command path

- **Status:** Accepted as proposed behavior; no command contract or application point exists yet. The implemented executor already provides the boundary this builds on: `Executor::scan` advances the tick, then runs read inputs → step components → write outputs as one atomic cycle (#9), and `TelemetrySnapshot` reports per-component diagnostics (#10).
- **Problem:** The monitoring and control UI must change a run's behavior — setpoints, mode switches, manual output overrides — through the same unified contract, and every attempted change needs an explicit accept/reject answer. The deterministic scan must define exactly when a command takes effect, or command ordering becomes timing-dependent and runs stop being reproducible.
- **Chosen approach:** A serde `Command`/`Receipt` pair defined in `dcs-core`, beside `TelemetrySnapshot`, so any transport or UI needs only the shared contracts. A `Command` names its target (a component instance or a point), an operation, and parameters. Commands are applied at one documented point: at the scan boundary, before the input-read phase of the next scan — never mid-step — so a queued command's effect begins at a known tick and the read → step → write order is preserved. Every command produces exactly one `Receipt`: accepted, reporting the tick it applied at; or rejected with a structured reason (unknown target, invalid or mistyped value, operation not permitted in the current state). Rejections are also visible through telemetry diagnostics so the UI can display them.
- **Alternatives considered:** Applying commands the moment they arrive, mid-scan (nondeterministic — the effect depends on where in the cycle the message lands); commands as raw driver writes bypassing components (loses component validation, scoping, and auditability); fire-and-forget commands with no receipt (the UI cannot distinguish failure from delay).
- **Consequences:** Command handling is deterministic and replayable: same model, same input sequence, same commands at the same ticks produce the same run. Targets opt into the operations they accept; everything else rejects with a reason rather than failing silently. The receipt contract gives the UI a uniform answer surface for every operator action.
- **Affected tickets:** #18 (this record); the M2 tickets defining `Command`/`Receipt` in `dcs-core` and wiring command application into the executor boundary; the monitoring transport ticket carrying commands and receipts.

### 8. Monitoring transport

- **Status:** Accepted as proposed behavior; no transport exists yet. The payload side is implemented: `TelemetrySnapshot` (#10) and the model's `SignalIndex` (#23) are already serde-JSON types, and decision 7 proposes `Command`/`Receipt` in the same form.
- **Problem:** The monitoring and control UI needs a wire protocol to fetch telemetry snapshots and signal metadata and to submit commands with receipts. The choice should serve the future browser-based UI directly rather than through a gateway, without pulling a heavy stack into the controller.
- **Chosen approach:** HTTP with JSON bodies on the controller process: GET endpoints return the `TelemetrySnapshot` and the model's signal index; a POST endpoint accepts a `Command` and answers with its `Receipt`. Rationale: JSON is already the contract's serialization (decision 3), an embedded HTTP server needs no async runtime or external service, and browser `fetch` plus every scripting tool can consume it. Snapshots report state, not history, so simple polling matches their semantics; if faster updates are needed later, server-sent events or a WebSocket can be layered on without changing the contract types.
- **Alternatives considered:** Newline-delimited JSON over raw TCP (simple and streamable, but browsers cannot open raw TCP — the UI would need a proxy, undermining the direct-access goal); WebSocket as the first transport (native push and browser-friendly, but adds framing and async machinery for a payload that is pull-shaped today); an industrial protocol such as OPC UA (rich semantics, but a heavy dependency premature for a first monitoring path).
- **Consequences:** The first UI and all tests need only HTTP and JSON; wire types are the same serde contracts the runtime already produces. Transport stays replaceable — the contracts live in `dcs-core` and the server only adapts them — so polling cadence and any later push upgrade are deployment choices, not contract changes.
- **Affected tickets:** #18 (this record); the M2 ticket adding the monitoring server to the controller process; future UI tickets.

### 9. Controller process

- **Status:** Accepted as proposed behavior; no controller binary exists yet. Implemented pieces it composes: model loading and validation (#7), `SimDriver` (#8), the deterministic `Executor` (#9), and the telemetry snapshot (#10).
- **Problem:** The stack needs a runnable deliverable: a process that loads a plant model, assembles its driver and executor, paces scans to wall-clock time for real operation, and exposes monitoring — without letting wall-clock concerns leak into components, which decision 4 forbids.
- **Chosen approach:** A thin `dcs-controller` binary crate taking a model path (e.g. `dcs-controller <model.json>` plus a scan-period option). It loads and validates the model through `dcs-model`, builds the driver and executor through `dcs-assembly` (decision 6), then runs a loop that paces `Executor::scan` to wall-clock and applies pending commands at the scan boundary (decision 7). The monitoring HTTP endpoints (decision 8) are served alongside the loop. The binary holds no control logic and no component code: pacing, transport, and process lifecycle live in this outer shell; everything inside the executor remains virtual ticks.
- **Alternatives considered:** Pacing inside `Executor::run` (leaks wall-clock into the deterministic core and makes runs non-reproducible); per-component timers (rejected with decision 4); control logic compiled into the binary (defeats model-driven assembly — the plant would be code, not a model).
- **Consequences:** The determinism boundary is explicit: inside the executor everything is tick-domain and reproducible; outside it the process paces, serves, and can be stopped, updated, or replaced. The binary is the unit that redundancy (decision 10) later checkpoints and swaps. Tests keep using the unpaced executor, so pacing code stays thin and separately exercisable.
- **Affected tickets:** #18 (this record); the M2 ticket creating the `dcs-controller` binary; the M3 tickets that wrap it in peer management.

### 10. Redundancy direction

- **Status:** Proposed — direction only, recorded as the basis of milestone M3. The enabler it relies on is implemented: the executor's virtual-tick determinism (#9) makes transferred state meaningful. The state-capture half is now also implemented (#21): `dcs-core` carries the `StateMap`/`StateError` contract and driver hooks on `IoDriver`, components carry `capture_state`/`restore_state`, `SimDriver` transfers the simulated field state, and `Executor::checkpoint`/`restore` move a run between executor instances. Peer management and I/O-ownership arbitration remain unimplemented.
- **Problem:** Per `AGENTS.md`, a redundant controller instance on another machine must take over without interrupting the process: the active controller keeps running while the standby is updated, replaced, or assumes control. This needs a recorded direction before M3 tickets decompose it.
- **Chosen approach (proposed):** Active/standby peers running the same plant model. The standby periodically receives a deterministic checkpoint — executor tick, component state, scan image, and driver-side state — in a serde-serializable form like the rest of the contract, and restores it so that on switchover its next scan produces outputs identical to what the active would have produced (bumpless switchover). Determinism (decision 4) is what makes this work: same model plus same state plus same inputs gives the same outputs on any host. Components and drivers gain checkpoint/restore hooks; a component that cannot checkpoint is reported when the model is built, not at failover. Switchover is a peer-level decision that also moves the field-facing identity — monitoring endpoint and I/O write ownership — to the new active.
- **Alternatives considered:** Restart-based redeploy (interrupts the process — the vision rules it out); lockstep parallel execution of both controllers (doubles I/O ownership questions and adds arbitration complexity for no first-version benefit); stateless takeover replayed from an input journal (requires recording every input and unbounded replay to reconstruct state).
- **Consequences:** Checkpoint/restore becomes a contract on components and drivers, adding hooks to `dcs-core`, `dcs-runtime`, and `dcs-sim` in M3 tickets. I/O ownership transfer — which peer may write the field — needs defined arbitration, initially simulated. The monitoring path (decisions 7 and 8) must follow whichever peer is active, so the UI treats the pair as one logical controller.
- **Affected tickets:** #18 (this record); all M3 "Redundant hot-swap controllers" tickets.
