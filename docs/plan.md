# Rolling milestone plan

## Current baseline

The Rust workspace holds four implemented crates — `dcs-core`, `dcs-model`, `dcs-runtime`, and `dcs-sim` — covering the shared contracts, the versioned plant model, the deterministic executor, and the simulated I/O backend. No reusable component library, model-driven assembly, controller binary, or monitoring transport exists yet. Foundational and M2/M3-direction decisions are recorded in `docs/architecture.md` (issues #4, #18).

## Milestones

### M1: Simulated single-controller loop — in progress

One controller runs a deterministic fixed-step scan entirely against simulated I/O: read simulated inputs, step reusable components, write simulated outputs, with the plant defined by a versioned model document.

| Crate | Layer | Status |
|---|---|---|
| `dcs-core` | Shared contracts: signal (value + quality + tick timestamp), component and logical I/O traits | Implemented — signal model (#5), logical-I/O abstraction (#6), `TelemetrySnapshot` monitoring contract (#10) |
| `dcs-model` | Plant model types, device/logical-I/O mapping, versioned JSON serialization | Implemented — versioned schema and validation (#7), `SignalIndex` for monitoring consumers (#23) |
| `dcs-runtime` | Fixed-step cyclic executor and component model driven by a virtual tick | Implemented — `Component` contract, wiring checks, scan, diagnostics (#9), `Executor::snapshot` (#10) |
| `dcs-sim` | Simulated I/O backend implementing the driver boundary | Implemented — `SimDriver` from a `ChannelMap`, loopback and process elements, fault injection (#8) |
| `dcs-blocks` | Reusable control components declaring typed logical I/O | Planned — no components exist yet |

Done when a controller binary or test harness loads a plant model, scans components from `dcs-blocks` against `dcs-sim`, and produces deterministic per-tick outputs. Remaining gap: `dcs-blocks` components and the model-loading harness (planned under M2's `dcs-assembly`, decision 6).

### M2: Model-driven controller with monitoring access — planned

A `dcs-controller` binary loads a plant model path, assembles its driver and executor from the validated model, paces the scan to wall-clock, and exposes monitoring and operator commands to the future UI. Architecture decisions 6–9 record the approach.

Done when:

- `dcs-assembly` resolves a validated `PlantModel` into a configured `Executor` and `SimDriver`, constructing components through a kind registry and rejecting unknown kinds before any scan (decision 6);
- `dcs-blocks` provides the first reusable components registered with that registry, completing M1's model-loaded scan;
- `dcs-controller` runs the paced scan and serves `TelemetrySnapshot` and the signal index over HTTP + JSON (decisions 8, 9);
- a serde `Command`/`Receipt` contract in `dcs-core` lets an operator submit commands applied at the documented scan boundary, with structured rejections reported per command and in diagnostics (decision 7).

### M3: Redundant hot-swap controllers — upcoming

Active/standby controller peers run the same plant model; the standby restores deterministic checkpoints of component, executor, and driver state and takes over without a missed or divergent output. Recorded as proposed direction in decision 10; decomposition into tickets waits until M2 lands.

Done when (proposed): two simulated peers run one model; the active checkpoints state to the standby; on switchover the new active's scans continue the run bumplessly against simulated I/O, and the monitoring path follows the active peer.

## Next planning pass

Inspect current main, open issues, and PRs before updating this plan. M1's remaining work folds into M2's `dcs-assembly`/`dcs-blocks` tickets; break M2 into independently testable tickets with acceptance criteria and dependencies, ordered so the `dcs-core` command contract and `dcs-assembly` land before the binary and transport. Keep between six and twenty ready issues only when that much independent work exists.

Update this document when milestones change or complete. Reference actual issue and PR numbers once created; mark blocked dependencies and distinguish completed work from planned work.
