# Rolling milestone plan

## Current baseline

The Rust workspace holds eleven implemented crates — `dcs-core`, `dcs-model`, `dcs-runtime`, `dcs-sim`, `dcs-sim-net`, `dcs-blocks`, `dcs-monitor`, `dcs-assembly`, `dcs-controller`, `dcs-plant`, and `dcs-demo` — covering the shared contracts, the versioned plant model, the deterministic executor with checkpoint/restore and the tracking standby, the simulated I/O backend with process elements and fault injection, the TCP-served shared simulated plant and remote driver plus the `dcs-plant-ctl` operator tool, the reusable component library with per-kind descriptors and timer/counter/rate-limiter kinds (#85), model-driven assembly with the device-kind driver registry (#47), the paced controller binary with active/standby modes (#32) and Docker packaging, the standalone shared-plant server accepting a declared dynamics document (#81), HTTP+JSON monitoring with signal groups (#86), history, journal, and the live page, and the end-to-end simulated tank loop. The remaining M2 gap is writable points (#49). Decisions are recorded in `docs/architecture.md` (issues #4, #18, #33, #53, #89).

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

### M2: Model-driven controller with monitoring access — nearly done

A `dcs-controller` binary loads a plant model path, assembles its driver and executor from the validated model, paces the scan to wall-clock, and exposes monitoring and operator commands to the future UI. Architecture decisions 6–9 record the approach; the real per-layer status:

| Layer | Scope | Status |
|---|---|---|
| `dcs-assembly` | Model → driver/executor resolution, component-kind registry | Implemented (#19): resolution into `ChannelMap`/`PointMap`/port bindings, the explicit `ComponentRegistry`, and `assemble`; device-kind factory registry and `FanoutDriver` done (#47); internal points and links resolved under #30 (decision 14) |
| `dcs-controller` | Paced controller binary | Implemented (#19): `--scan-ms` pacing; monitoring served in-process via `Monitor::bind_paced` with `POST /scan` refused while pacing (#48); `--standby`/`--remote` modes track an active's checkpoints (#32); Docker packaging done (#39) |
| `dcs-monitor` | HTTP+JSON monitoring transport | Implemented: `/snapshot`, `/receipts`, `/command`, `/scan` (#20); `/signals` and the live page (#34); `/history` and `/journal` with since-cursors (#35, decision 17); trend and journal panes (#51) |
| `dcs-core` command contract | `Command`/`Receipt` applied at the scan boundary | Implemented (#20); model-declared writable targets proposed under #49 (decision 18); component-targeted `set_parameter` proposed under #82 (decision 20) |
| `dcs-model` internal points | Channel-less operator and port-to-port points | Implemented (#30): channel-less `io_point`s carry `initial` in the scan image — held `In` setpoints, monitored `Out` writes, and internal links serving port-to-port wiring (decision 14) |
| `dcs-blocks` | Component library registered with the assembly registry | Implemented (#11, #22, #38, #52); every kind ships `KIND`, `from_parameters`, and a `ComponentDescriptor` (#50, #61, decision 16) |

Done when:

- `dcs-assembly` resolves a validated `PlantModel` into a configured `Executor` and `SimDriver`, constructing components through a kind registry and rejecting unknown kinds before any scan (decision 6; #19) — **done**;
- `dcs-controller` runs the paced scan and serves `TelemetrySnapshot` and the signal index over HTTP + JSON (decisions 8, 9; #19, #48, #34) — **done**;
- internal points carry operator values and port-to-port wiring as model declarations (decision 14; #30) — **done** — with writable points restricting the command surface (#49) — **open**;
- the monitoring surface serves signal metadata, a minimal live page, and bounded history (decision 8; #34, #35) — **done**.

### M3: Redundant hot-swap controllers — in progress

Active/standby controller peers run the same plant model; the standby restores deterministic checkpoints of component, executor, and driver state and takes over without a missed or divergent output. Direction recorded in decision 10; per-topic semantics in decisions 11–15 (#33).

Ticket breakdown:

- #21 — checkpoint/restore contract: `StateMap`/`StateError`, driver hooks, `Executor::checkpoint`/`restore`. **Done.**
- #33 — M3 decision record and plan refresh: decisions 10–15. **Done.**
- #31 — remote simulated I/O driver over TCP: `dcs-sim-net`'s `PlantServer`/`RemoteDriver`, one shared simulated plant two controller processes attach to. **Done.**
- #68 — `dcs-plant-ctl`, the plant-side operator tool speaking the plant server's protocol. **Done.**
- #39 — `dcs-controller` Docker packaging: a redundant pair is two containers on separate hosts sharing one plant model. **Done.**
- #81 — `dcs-plant-server` binary running the shared simulated plant as a standalone process, merging a declared dynamics document via `--dynamics` (decision 24). **Done.**
- #32 — standby synchronization: checkpoint transfer over the monitoring transport (decision 12) into a tracking standby covering both driver-observation modes (decision 13). **Done** — `GET /checkpoint` plus `MonitorClient::checkpoint`, `Executor::apply`, `Standby`/`StandbyState`, `WriteGate`, and `dcs-controller --standby`/`--listen`/`--remote`.
- #47 — device-kind driver factory registry in `dcs-assembly`, including the remote-sim kind. **Done** — `DriverRegistry::standard` resolves `sim*` and `sim-tcp`, and `FanoutDriver` presents one `IoDriver` over the backends.
- #46 — switchover and promotion: role contract, output quiescence behind a driver-boundary write gate, bumpless promotion at a scan boundary (decision 15); also fixes the role-reporting shape the UI consumes (decision 19). **Open.**
- #64 — end-to-end two-controller hot swap over the shared simulated plant. **Open.**

Done when: two simulated peers run one model; the active checkpoints state to the standby over the monitoring transport; the standby scans output-quiesced until promotion; after promotion the new active's scans continue the run bumplessly against the shared simulated plant; and the monitoring path reports the pair's roles so the UI sees one logical controller.

### M4: Monitoring and control UI — in progress

A browser-based monitoring and control UI over the decision-8 transport, consuming the same plant model and contract types the controllers run: live telemetry and signal metadata, descriptor-driven component faceplates, trend and journal views, and receipt-answered operator commands on model-declared writable points. Decisions 16–19 record the approach (#53, this record); the M2 monitoring layers above supply the transport, and M3's role contract (decision 19) supplies the single-logical-controller view.

Ticket breakdown:

- #53 — M4 decision record and plan refresh (decisions 16–19). **Done.**
- #34 — signal-metadata endpoint and minimal live page on the monitor. **Done.**
- #35 — bounded per-point history and the transition journal served with since-cursors (decision 17). **Done.**
- #50 — `ComponentDescriptor` contract and per-kind descriptors, served to the UI (decision 16). **Done** — per-kind descriptors completed under #61.
- #51 — the page's trend slice: live telemetry plus signal metadata, per-point inline-SVG trends fed by `/history`, and the transition-journal pane fed by `/journal`. **Done.**
- #49 — model-declared `writable` points narrowing the command surface (decision 18). **Open** — M2 layer gating #62's affordances and #84's forcing.
- #62 — descriptor-driven faceplates with writable command affordances (decisions 16, 18). **Open.**
- #63 — the pair-as-one-controller view (decision 19; needs #46's role reporting). **Open.**
- #69 — showcase plant model exercising the full component library. **Open.**

Done when:

- the monitoring surface serves the signal index, per-kind `ComponentDescriptor`s, bounded point history, and the transition journal over the decision-8 transport (#34, #35, #50) — **done**;
- a served page renders live telemetry and signal metadata, faceplates generated from descriptors without per-kind UI code, and trend and journal panes fed by incremental since-cursor polling (#51, #62) — **partially done** (trend and journal slices landed; faceplates open);
- the page submits operator commands through the receipt-answered path and offers command affordances only on model-declared writable points, with rejections visible in the journal (decisions 7, 17, 18; #49, #62) — **open**;
- under redundancy the page presents an active/standby pair as one logical controller, displaying the active's telemetry plus pair health from reported roles (decision 19; #32, #46, #63) — **open**.

### M5: Lifecycle and device integration — registered

The redundant pair becomes a lifecycle platform: automatic promotion when the active is lost, checkpoint versioning for cross-build replacement, declared-behavior device kinds beyond `sim`, an integration guide for new component and device kinds, and in-service model revision through the pair. The milestone's own decision record lands with #70; decisions 25 and 26 (recorded under #89) already supply the model-revision and divergence-gate semantics. No layer is implemented; every ticket is open.

Ticket breakdown:

- #70 — M5 decision record and plan refresh.
- #65 — detect active-controller loss and promote the converged standby automatically.
- #66 — version the checkpoint format for cross-build controller replacement (decision 11's deferred `version` field).
- #67 — scripted-scenario simulated device kind proving the driver registry (#47).
- #71 — component-kind and device-kind integration guide.
- #87 — roll a revised plant model into production through the redundant pair, with the carryover rule and report (decision 25).
- #88 — standby output-divergence detection as a promotion gate (decision 26).

### M6: Operator ergonomics and library breadth — defined

The operator-facing surface and the reusable library grow together on the contracts already implemented: receipted parameter tuning on component targets, persistent input forcing, a named I/O-health and scan-overrun surface, model-declared display grouping, a broader `dcs-blocks` kind set, and plant-side dynamics as declared data beside the model. Decisions 20–24 record the approach (#89, this record); decisions 25–26 record the redundancy-hardening semantics the M5 tickets #87 and #88 implement.

Ticket breakdown:

- #89 — this decision record and plan refresh. **Done with this change.**
- #82 — `set_parameter` commands tuning component parameters at the scan boundary, descriptor-driven range enforcement, and the checkpoint-carryover obligation for tuned parameters (decision 20). **Open** — issue filed under M4.
- #83 — I/O-health counters, the optional `IoDriver::diagnostics` hook, and the paced-loop overrun feed in the telemetry snapshot (decision 22). **Open** — issue filed under M4.
- #84 — persistent forcing of writable field `In` points at `Uncertain(Substituted)` quality, released at the scan boundary, listed and journaled (decision 21; depends on #49). **Open** — issue filed under M4.
- #86 — `Signal.group` carried through `SignalIndex` into page grouping (decision 23). **Done** — issue filed under M4.
- #85 — timer, counter, and rate-limiter kinds in `dcs-blocks`, each shipping `KIND`/`from_parameters`/`describe` per the established convention. **Done.**
- Declared plant-side dynamics (decision 24). **Done** — `dcs-plant-server --dynamics` merges a JSON list of `ProcessElement` declarations (#81).

Done when:

- an operator tunes a declared parameter through a receipted `set_parameter` command — applied at the scan boundary, with named rejections for unknown component, unknown parameter, type mismatch, and out-of-range — and the tuned value restores through a checkpoint into a fresh executor (#82) — **open**;
- a writable field `In` point can be forced across scans at substituted quality, released at the scan boundary, badged in the snapshot, journaled, and preserved across a checkpoint (#84, with #49) — **open**;
- the telemetry snapshot reports executor-collected I/O-health counters, per-kind driver diagnostics where the driver implements the optional hook, and paced scan overruns fed by the controller shell (#83) — **open**;
- the monitoring page organizes its signal list by the model's declared `group` field, with ungrouped signals under a documented default (#86) — **done**;
- `dcs-blocks` ships the timer, counter, and rate-limiter kinds with descriptors and checkpoint coverage like every existing kind (#85) — **done**;
- plant-side dynamics load from a declared document beside the model for the shared plant server and test rigs, leaving `PlantModel` schema untouched (decision 24; #81) — **done**.

## Next planning pass

Inspect current main, open issues, and PRs before updating this plan. M2's critical path is now #49: it gates #62's command affordances and #84's forcing. With #32, #47, and #81 landed, M3's critical path is #46 (promotion and role reporting) then #64 proving the swap end-to-end; M5's lifecycle tickets follow the pair mechanics, with #67 able to start against the shipped driver registry. M6's remaining ergonomics tickets — #82, #83, #84 — are independently startable against the existing executor, monitor, and model. Keep between six and twenty ready issues only when that much independent work exists.

Update this document when milestones change or complete. Reference actual issue and PR numbers once created; mark blocked dependencies and distinguish completed work from planned work.
