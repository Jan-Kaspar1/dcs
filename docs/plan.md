# Rolling milestone plan

## Current baseline

The bootstrap establishes the Rust workspace, automated checks, and local worker orchestration. `crates/dcs-core` exists as an empty shared-contract crate; product functionality has not yet been implemented. Foundational decisions are recorded in `docs/architecture.md` (issue #4).

## Milestones

### M1: Simulated single-controller loop — planned

One controller runs a deterministic fixed-step scan entirely against simulated I/O: read simulated inputs, step reusable components, write simulated outputs, with the plant defined by a versioned model document. No layer below is implemented yet; all are planned.

| Crate | Layer | Status |
|---|---|---|
| `dcs-core` | Shared contracts: signal (value + quality + tick timestamp), component and logical I/O traits | Planned — crate stub exists, contracts not implemented |
| `dcs-model` | Plant model types, device/logical-I/O mapping, versioned JSON serialization | Planned |
| `dcs-runtime` | Fixed-step cyclic executor and component model driven by a virtual tick | Planned |
| `dcs-sim` | Simulated I/O backend implementing the driver boundary | Planned |
| `dcs-blocks` | Reusable control components declaring typed logical I/O | Planned |

Done when a controller binary or test harness loads a plant model, scans components from `dcs-blocks` against `dcs-sim`, and produces deterministic per-tick outputs.

## Next planning pass

Inspect current main, open issues, and PRs before updating this plan. Break M1 into independently testable tickets with acceptance criteria and dependencies, ordered so `dcs-core` contracts land first. Keep between six and twenty ready issues only when that much independent work exists.

Update this document when milestones change or complete. Reference actual issue and PR numbers once created; mark blocked dependencies and distinguish completed work from planned work.
