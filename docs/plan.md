# Rolling milestone plan

## Current baseline

The Rust workspace holds eight implemented crates — `dcs-core`, `dcs-model`, `dcs-runtime`, `dcs-sim`, `dcs-sim-net`, `dcs-blocks`, `dcs-monitor`, and `dcs-demo` — covering the shared contracts, the versioned plant model, the deterministic executor, the simulated I/O backend, the TCP-served shared simulated plant and remote driver, the reusable component library, HTTP+JSON monitoring access, and the end-to-end simulated tank loop. The operator command path (#20) and deterministic checkpoint/restore (#21) are implemented; the `dcs-assembly` wiring crate and `dcs-controller` binary do not exist yet. Decisions are recorded in `docs/architecture.md` (issues #4, #18, #33, #53).

## Milestones

### M1: Simulated single-controller loop — done

One controller runs a deterministic fixed-step scan entirely against simulated I/O: read simulated inputs, step reusable components, write simulated outputs, with the plant defined by a versioned model document.

| Crate | Layer | Status |
|---|---|---|
| `dcs-core` | Shared contracts: signal (value + quality + tick timestamp), component and logical I/O traits | Implemented — signal model (#5), logical-I/O abstraction (#6), `TelemetrySnapshot` (#10), `Command`/`Receipt` (#20), `StateMap`/`StateError` (#21) |
| `dcs-model` | Plant model types, device/logical-I/O mapping, versioned JSON serialization | Implemented — versioned schema and validation (#7), `SignalIndex` (#23), model CLI (#36) |
| `dcs-runtime` | Fixed-step cyclic executor and component model driven by a virtual tick | Implemented — `Component` contract, wiring checks, scan, diagnostics (#9), `Executor::snapshot` (#10), scan-boundary commands (#20), `checkpoint`/`restore` (#21) |
| `dcs-sim` | Simulated I/O backend implementing the driver boundary | Implemented — `SimDriver` from a `ChannelMap`, loopback and process elements, fault injection (#8), dead-time element (#37), driver state capture (#21) |
| `dcs-blocks` | Reusable control components declaring typed logical I/O | Implemented — analog and digital channel blocks and PID (#11), alarm monitor (#22), interlock and override (#38), valve and motor (#52) |

Done criterion met by `dcs-demo` (#12): the binary loads the checked-in `tank_level.json` model, assembles a `SimDriver` and `Executor` plus `dcs-blocks` components, and produces deterministic per-tick outputs and a final telemetry snapshot.

### M2: Model-driven controller with monitoring access — in progress

A `dcs-controller` binary loads a plant model path, assembles its driver and executor from the validated model, paces the scan to wall-clock, and exposes monitoring and operator commands to the future UI. Architecture decisions 6–9 record the approach; the real per-layer status:

| Layer | Scope | Status |
|---|---|---|
| `dcs-assembly` | Model → driver/executor resolution, component-kind registry | Not started — #19 open (currently blocked) |
| `dcs-controller` | Paced controller binary | Not started — #19 open; monitoring integration tracked by #48 |
| `dcs-monitor` | HTTP+JSON monitoring transport | Implemented (#20): `/snapshot`, `/receipts`, `/command`, `/scan`; bounded history and the transition journal implemented (#35): `/history`, `/journal` with since-cursors (decision 17); signal-metadata endpoint and minimal page proposed under #34 (blocked) |
| `dcs-core` command contract | `Command`/`Receipt` applied at the scan boundary | Implemented (#20); model-declared writable targets proposed under #49 (decision 18) |
| `dcs-model` internal points | Channel-less operator and port-to-port points | Proposed — #30 open; the demo's synthesized internal-device pairs are the implemented precursor (#12) |
| `dcs-blocks` | Component library registered with the assembly registry | Implemented (#11, #22, #38, #52); `KIND` constants and `from_parameters` are in place; per-kind `ComponentDescriptor`s proposed under #50 (decision 16) |

Done when:

- `dcs-assembly` resolves a validated `PlantModel` into a configured `Executor` and `SimDriver`, constructing components through a kind registry and rejecting unknown kinds before any scan (decision 6; #19);
- `dcs-controller` runs the paced scan and serves `TelemetrySnapshot` and the signal index over HTTP + JSON (decisions 8, 9; #19, #48);
- internal points carry operator values and port-to-port wiring as model declarations (decision 14; #30), with writable points restricting the command surface (#49);
- the monitoring surface serves signal metadata, a minimal live page, and bounded history (decision 8; #34, #35).

### M3: Redundant hot-swap controllers — in progress

Active/standby controller peers run the same plant model; the standby restores deterministic checkpoints of component, executor, and driver state and takes over without a missed or divergent output. Direction recorded in decision 10; per-topic semantics in decisions 11–15 (#33).

Ticket breakdown:

- #21 — checkpoint/restore contract: `StateMap`/`StateError`, driver hooks, `Executor::checkpoint`/`restore`. **Done.**
- #33 — M3 decision record and plan refresh: decisions 10–15. **Done.**
- #31 — remote simulated I/O driver over TCP: `dcs-sim-net`'s `PlantServer`/`RemoteDriver`, one shared simulated plant two controller processes attach to. **Done.**
- #32 — standby synchronization: checkpoint transfer over the monitoring transport (decision 12) into a tracking standby covering both driver-observation modes (decision 13).
- #46 — switchover and promotion: role contract, output quiescence behind a driver-boundary write gate, bumpless promotion at a scan boundary (decision 15); also fixes the role-reporting shape the UI consumes (decision 19).
- #47 — device-kind driver factory registry in `dcs-assembly`, including the remote-sim kind.
- #39 — `dcs-controller` Docker packaging: a redundant pair is two containers on separate hosts sharing one plant model.

Done when: two simulated peers run one model; the active checkpoints state to the standby over the monitoring transport; the standby scans output-quiesced until promotion; after promotion the new active's scans continue the run bumplessly against the shared simulated plant; and the monitoring path reports the pair's roles so the UI sees one logical controller.

### M4: Monitoring and control UI — in progress

A browser-based monitoring and control UI over the decision-8 transport, consuming the same plant model and contract types the controllers run: live telemetry and signal metadata, descriptor-driven component faceplates, trend and journal views, and receipt-answered operator commands on model-declared writable points. Decisions 16–19 record the approach (#53, this record); the M2 monitoring layers above supply the transport, and M3's role contract (decision 19) supplies the single-logical-controller view.

Ticket breakdown:

- #53 — this decision record and plan refresh. **Done with this change.**
- #34 — signal-metadata endpoint and minimal live page on the monitor (currently blocked with M2's controller path).
- #35 — bounded per-point history and the transition journal served with since-cursors (decision 17). **Done.**
- #49 — model-declared `writable` points narrowing the command surface (decision 18).
- #50 — `ComponentDescriptor` contract and per-kind descriptors, served to the UI (decision 16).
- #51 — the page itself, built in slices. **Trend slice done:** live telemetry plus signal metadata (#34), per-point inline-SVG trends fed by `/history`, and the transition-journal pane fed by `/journal`, all polled incrementally on the snapshot's cadence. Remaining page work: descriptor-driven faceplates (#50), command affordances restricted to writable points (#49), and the pair-as-one-controller view (#46).

Done when:

- the monitoring surface serves the signal index, per-kind `ComponentDescriptor`s, bounded point history, and the transition journal over the decision-8 transport (#34, #35, #50);
- a served page renders live telemetry and signal metadata, faceplates generated from descriptors without per-kind UI code, and trend and journal panes fed by incremental since-cursor polling (#51);
- the page submits operator commands through the receipt-answered path and offers command affordances only on model-declared writable points, with rejections visible in the journal (decisions 7, 17, 18; #49, #51);
- under redundancy the page presents an active/standby pair as one logical controller, displaying the active's telemetry plus pair health from reported roles (decision 19; #32, #46).

## Next planning pass

Inspect current main, open issues, and PRs before updating this plan. M2's critical path is #19 (`dcs-assembly`/`dcs-controller`): it gates #48, #34's page serving, and M3's #32, #39, #46, and #47. M3's #31 and M4's contract-side tickets — #35 (history and journal on the existing monitor), #49 (writable points), and #50 (component descriptors) — are independently startable against `dcs-monitor` and the model without the controller binary; #51's page work follows once #34 and #35 land the monitoring surface. Keep between six and twenty ready issues only when that much independent work exists.

Update this document when milestones change or complete. Reference actual issue and PR numbers once created; mark blocked dependencies and distinguish completed work from planned work.
