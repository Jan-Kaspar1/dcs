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
