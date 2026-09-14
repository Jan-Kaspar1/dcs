# Rolling milestone plan

## Current baseline

The Rust workspace holds eleven implemented crates — `dcs-core`, `dcs-model`, `dcs-runtime`, `dcs-sim`, `dcs-sim-net`, `dcs-blocks`, `dcs-monitor`, `dcs-assembly`, `dcs-controller`, `dcs-plant`, and `dcs-demo` — covering the shared contracts, the versioned plant model, the deterministic executor with checkpoint/restore and the tracking standby, the simulated I/O backend with process elements and fault injection, the TCP-served shared simulated plant and remote driver plus the `dcs-plant-ctl` operator tool, the reusable component library with per-kind descriptors and timer/counter/rate-limiter kinds (#85), model-driven assembly with the device-kind driver registry (#47) now serving `sim*`, `sim-tcp`, and `sim-scripted` (#67), the paced controller binary with active/standby modes (#32), role machine, and scan-boundary promotion/demotion with role reporting (#46) plus Docker packaging, the standalone shared-plant server accepting a declared dynamics document (#81), HTTP+JSON monitoring with signal groups (#86), history, journal, and the live page, and the end-to-end simulated tank loop. The integration guide (`docs/integration-guide.md`, #71) documents both extension seams. The M2 gap — writable points (#49) — has landed. Decisions are recorded in `docs/architecture.md` (issues #4, #18, #33, #53, #89, #70, #104).

## Milestones

### M1: Simulated single-controller loop — done

One controller runs a deterministic fixed-step scan entirely against simulated I/O: read simulated inputs, step reusable components, write simulated outputs, with the plant defined by a versioned model document.

| Crate | Layer | Status |
|---|---|---|
| `dcs-core` | Shared contracts: signal (value + quality + tick timestamp), component and logical I/O traits | Implemented — signal model (#5), logical-I/O abstraction (#6), `TelemetrySnapshot` (#10), `Command`/`Receipt` (#20), `StateMap`/`StateError` (#21) |
| `dcs-model` | Plant model types, device/logical-I/O mapping, versioned JSON serialization | Implemented — versioned schema and validation (#7), `SignalIndex` (#23), model CLI (#36) with validate/summary/signal-index and the revision `diff` (#114) |
| `dcs-runtime` | Fixed-step cyclic executor and component model driven by a virtual tick | Implemented — `Component` contract, wiring checks, scan, diagnostics (#9), `Executor::snapshot` (#10), scan-boundary commands (#20), `checkpoint`/`restore` (#21) |
| `dcs-sim` | Simulated I/O backend implementing the driver boundary | Implemented — `SimDriver` from a `ChannelMap`, loopback and process elements, fault injection (#8), dead-time element (#37), driver state capture (#21) |
| `dcs-blocks` | Reusable control components declaring typed logical I/O | Implemented — analog and digital channel blocks and PID (#11), alarm monitor (#22), interlock and override (#38), valve and motor (#52) |

Done criterion met by `dcs-demo` (#12): the binary loads the checked-in `tank_level.json` model, assembles a `SimDriver` and `Executor` plus `dcs-blocks` components, and produces deterministic per-tick outputs and a final telemetry snapshot.

### M2: Model-driven controller with monitoring access — done

A `dcs-controller` binary loads a plant model path, assembles its driver and executor from the validated model, paces the scan to wall-clock, and exposes monitoring and operator commands to the future UI. Architecture decisions 6–9 record the approach; the real per-layer status:

| Layer | Scope | Status |
|---|---|---|
| `dcs-assembly` | Model → driver/executor resolution, component-kind registry | Implemented (#19): resolution into `ChannelMap`/`PointMap`/port bindings, the explicit `ComponentRegistry`, and `assemble`; device-kind factory registry and `FanoutDriver` done (#47); internal points and links resolved under #30 (decision 14) |
| `dcs-controller` | Paced controller binary | Implemented (#19): `--scan-ms` pacing; monitoring served in-process via `Monitor::bind_paced` with `POST /scan` refused while pacing (#48); `--standby`/`--remote` modes track an active's checkpoints (#32); `--driven` mode paces scans through `POST /scan` for externally timed runs (#64); Docker packaging done (#39) |
| `dcs-monitor` | HTTP+JSON monitoring transport | Implemented: `/snapshot`, `/receipts`, `/command`, `/scan` (#20); `/signals` and the live page (#34); `/history` and `/journal` with since-cursors (#35, decision 17); trend and journal panes (#51) |
| `dcs-core` command contract | `Command`/`Receipt` applied at the scan boundary | Implemented (#20); component-targeted `set_parameter` implemented under #82 (decision 20); model-declared writable targets implemented under #49 (decision 18) |
| `dcs-model` internal points | Channel-less operator and port-to-port points | Implemented (#30): channel-less `io_point`s carry `initial` in the scan image — held `In` setpoints, monitored `Out` writes, and internal links serving port-to-port wiring (decision 14) |
| `dcs-blocks` | Component library registered with the assembly registry | Implemented (#11, #22, #38, #52); every kind ships `KIND`, `from_parameters`, and a `ComponentDescriptor` (#50, #61, decision 16) |

Done when:

- `dcs-assembly` resolves a validated `PlantModel` into a configured `Executor` and `SimDriver`, constructing components through a kind registry and rejecting unknown kinds before any scan (decision 6; #19) — **done**;
- `dcs-controller` runs the paced scan and serves `TelemetrySnapshot` and the signal index over HTTP + JSON (decisions 8, 9; #19, #48, #34) — **done**;
- internal points carry operator values and port-to-port wiring as model declarations (decision 14; #30) — **done** — with writable points restricting the command surface (#49) — **done**;
- the monitoring surface serves signal metadata, a minimal live page, and bounded history (decision 8; #34, #35) — **done**.

### M3: Redundant hot-swap controllers — done

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
- #46 — switchover and promotion: role contract, output quiescence behind a driver-boundary write gate, bumpless promotion at a scan boundary (decision 15); also fixes the role-reporting shape the UI consumes (decision 19). **Done** — `Role`/`StandbySync`/`RoleReport`/`SwitchError` in `dcs-core`, the `Peer` role machine in `dcs-runtime`, `GET /role` plus `POST /promote`/`/demote` and role-gated commands on the monitor, `JournalEvent::RoleChanged`, and `dcs-controller --standby` + `--listen` as the promotable standby over `--remote`.
- #64 — end-to-end two-controller hot swap over the shared simulated plant. **Done** — `crates/dcs-controller/tests/hot_swap.rs` runs the milestone's done-when as one scripted, tick-paced scenario: a `dcs-plant-server` owning the shared plant, two `dcs-controller` processes on one model whose devices are `sim-tcp` (the registry's remote-sim kind), both in the new `--driven` mode where `POST /scan` requests pace each tick, carrying the standby's checkpoint pull and the field-owning peer's plant step (`Monitor::driven`'s `track`/`after_scan` wiring); a third driven controller on a second plant server is the uninterrupted reference run the promoted peer's outputs equal for a stated tick count. The write gate now covers field-facing points inside the registry fan-out (`WriteGate::closed_covering` over `DeviceBackend.field_facing`, `FanoutDriver::step_local` leaving field backends' clocks to the owner); promoting the unconverged standby answers `SwitchError::NotConverged` and a mid-run plant-server restart surfaces documented `IoError`s rather than divergence.

Done when: two simulated peers run one model; the active checkpoints state to the standby over the monitoring transport; the standby scans output-quiesced until promotion; after promotion the new active's scans continue the run bumplessly against the shared simulated plant; and the monitoring path reports the pair's roles so the UI sees one logical controller.

### M4: Monitoring and control UI — in progress

A browser-based monitoring and control UI over the decision-8 transport, consuming the same plant model and contract types the controllers run: live telemetry and signal metadata, descriptor-driven component faceplates, trend and journal views, and receipt-answered operator commands on model-declared writable points. Decisions 16–19 record the approach (#53, this record); the M2 monitoring layers above supply the transport, and M3's role contract (decision 19) supplies the single-logical-controller view.

Ticket breakdown:

- #53 — M4 decision record and plan refresh (decisions 16–19). **Done.**
- #34 — signal-metadata endpoint and minimal live page on the monitor. **Done.**
- #35 — bounded per-point history and the transition journal served with since-cursors (decision 17). **Done.**
- #50 — `ComponentDescriptor` contract and per-kind descriptors, served to the UI (decision 16). **Done** — per-kind descriptors completed under #61.
- #51 — the page's trend slice: live telemetry plus signal metadata, per-point inline-SVG trends fed by `/history`, and the transition-journal pane fed by `/journal`. **Done.**
- #49 — model-declared `writable` points narrowing the command surface (decision 18). **Done** — `IoPoint::writable` (optional, default unset) validated `In`-only, carried through assembly into `PointSpec::writable`, enforced at `submit_command` as `CommandError::NotWritable`, reported in the signal index, and filtering the page's command select; `Out`-point writes are the recorded rejection rule.
- #62 — descriptor-driven faceplates with writable command affordances (decisions 16, 18). **Done** — `PortDescriptor.point` carries the bound `PointId` as serving-layer annotation (serde-optional, joined by the executor from the declared `IoRequirement`s — the recorded wiring choice); the page renders one generic faceplate per instance with role-hinted elements, parameter kinds and ranges, and name-joined diagnostics, degrading to the generic name-plus-diagnostics-plus-wired-points view for a kind with no custom descriptor; inline write affordances render only on model-marked writable points and submit through the pair-aware `submitCommand`, with rejections visible in the receipt pane and journal. Its landing unblocks the #105, #106, and #108 page slices.
- #63 — the pair-as-one-controller view (decision 19; consumes the role reporting #46 landed). **Done** — `PairClient`/`PeerStatus`/`PairError` in `dcs-monitor`, the page's `?peer=` pair view with per-peer role polling, active-only command routing with mid-transition retry, tick-continuity merging, and cross-origin reads on the JSON endpoints.
- #69 — showcase plant model exercising the full component library. **Open** — gates #116's full-stack demonstration.
- #105 — descriptor-driven parameter editing on faceplates through `set_parameter`, with the applied tick or named rejection displayed (decision 34's interim surface; depends on #62, #82). **Open.**
- #106 — forced points badged and releasable on the page (decision 21's UI slice; depends on #62, #84). **Open.**
- #107 — I/O-health counters, driver diagnostics, and scan overruns rendered on the page (decision 22's UI slice; depends on #63, #83). **Open.**
- #108 — an alarm summary pane driven by status-role ports and the journal (decision 33's flags; depends on #62). **Open.**

Done when:

- the monitoring surface serves the signal index, per-kind `ComponentDescriptor`s, bounded point history, and the transition journal over the decision-8 transport (#34, #35, #50) — **done**;
- a served page renders live telemetry and signal metadata, faceplates generated from descriptors without per-kind UI code, and trend and journal panes fed by incremental since-cursor polling (#51, #62) — **done**;
- the page submits operator commands through the receipt-answered path and offers command affordances only on model-declared writable points, with rejections visible in the journal (decisions 7, 17, 18; #49, #62) — **done** (the generic command select and the faceplate affordances both gate on the metadata's `writable` mark);
- under redundancy the page presents an active/standby pair as one logical controller, displaying the active's telemetry plus pair health from reported roles (decision 19; #32, #46, #63) — **done**;
- the page's operator slices land on the faceplate and contract bases: descriptor-driven parameter editing through `set_parameter` (#105), forced-point badging and release (#106), I/O-health and overrun rendering (#107), and the alarm summary pane (#108) — **open**.

### M5: Lifecycle and device integration — in progress

The redundant pair becomes a lifecycle platform: automatic promotion when the active is lost, checkpoint versioning for cross-build replacement, declared-behavior device kinds beyond `sim`, an integration guide for new component and device kinds, and in-service model revision through the pair. Decisions 27–30 record this milestone's approach (#70, this record); decisions 25 and 26 (recorded under #89) already supply the model-revision and divergence-gate semantics. Four layers are implemented — the scripted device kind (#67) proves the registered device seam, the integration guide (#71) documents it, the divergence gate (#88) promotes only standbys proven to track the field, and checkpoint versioning (#66) lets a standby negotiate cross-build compatibility by name; the lifecycle tickets (#65, #87, #114, #116) remain open and #115's restart-recovery state file has landed (decision 35).

Ticket breakdown:

- #70 — M5 decision record and plan refresh (decisions 27–30). **Done with this change.**
- #65 — detect active-controller loss and promote the converged standby automatically: checkpoint-pull heartbeat, missed-transfer budget, convergence-gated self-promotion, and the field-side single-writer fencing rule (decision 28). **Open.**
- #66 — version the checkpoint format for cross-build controller replacement (decision 11's deferred `version` field, decision 27's version + fingerprint negotiation). **Done** — `Checkpoint` carries `format_version` (`CHECKPOINT_FORMAT_VERSION`, accepted set `SUPPORTED_FORMAT_VERSIONS` including the pre-versioned shape 0) and `model_fingerprint` (`dcs_core::ModelFingerprint`, FNV-1a over the canonical model document via `PlantModel::fingerprint`, stamped by `dcs-assembly::assemble`); restore negotiates both before any state applies with named `RestoreError::UnsupportedVersion`/`FingerprintMismatch`.
- #67 — scripted-scenario simulated device kind proving the driver registry (#47). **Done** — `SIM_SCRIPTED_KIND` resolves to `ScriptedDriver`, the `"script"` device parameter declares tick-indexed playback, and `FanoutDriver::inspect` reaches the recorded-write log (decision 29).
- #71 — component-kind and device-kind integration guide. **Done** — `docs/integration-guide.md` walks both seams end to end with compiling worked examples.
- #87 — roll a revised plant model into production through the redundant pair, with the carryover rule and report (decision 25). **Open** — depends on #66.
- #88 — standby output-divergence detection as a promotion gate (decision 26). **Done** — `Peer` stashes each quiesced scan's staged field `Out` image and compares it, at the same-tick checkpoint transfer, against the standby's own reads of those points (`divergence::compare_staged`, exact for `Bool`/`Int`, `FLOAT_TOLERANCE` for `Float`); a mismatch lands the named `StandbySync::Diverged` carrying the evidence, refuses promotion with `SwitchError::NotConverged`, keeps the gate closed, journals `DivergenceDetected` at the compared tick, and clears on a clean resync — all proven by the two-peer loopback test `crates/dcs-controller/tests/divergence.rs`.
- #114 — a model-revision diff subcommand on the `dcs-model` CLI reporting what changed between two documents — the revision-review tooling #87's roll needs (depends on #36). **Open.**
- #115 — persist controller checkpoints to a file for restart recovery (depends on #21, #66). **Done** — `dcs-controller --state-file PATH` writes the serde `Checkpoint` (versioned and fingerprinted, decisions 11 and 27) at the end of every completed scan cycle by write-then-rename; an existing file resumes the run through `Executor::apply` before pacing begins, a missing file is a cold start, and every resume failure — corrupt file, unsupported version, fingerprint or structural mismatch — exits nonzero naming the reason (decision 35). The standby path is unchanged; a redundant peer remains the preferred recovery where redundancy exists.
- #116 — demonstrate the full stack end to end: shared plant, redundant pair, and monitoring surface (depends on #64, #69). **Open.**

Done when:

- a standby detects active loss through its checkpoint pulls — a configured budget of consecutive failed transfers — and promotes itself at a scan boundary only while converged and inside the budget, with the shared field refusing a fenced-out old active's writes (decision 28; #65) — **open**;
- a checkpoint carries an explicit `version` and a fingerprint of the model and kind set it was captured under, a standby negotiates compatibility at fetch time with a named rejection on mismatch, and a new-build standby proves the rolling upgrade end to end (decisions 11, 27; #66) — **open**;
- a declared-behavior device kind beyond plain `sim` instantiates from model data through the driver registry — `sim-scripted`'s tick-indexed playback (decision 29; #67) — **done**;
- the integration guide walks a new component kind and a new device kind from `Component`/`IoDriver` implementation through registration to a scanning model, with both worked examples compiling in the test suite (decision 29; #71) — **done**;
- a revised plant model rolls into production through the pair: the new-model standby enters the named `reinitialized` state, the carryover report names what crossed the revision, and promotion moves the field writer preserving exactly-one-writer (decision 25; #87) — **open**;
- a tracking standby's staged `Out` image is compared against the shared field, a mismatch lands it in a journaled `diverged` state blocking promotion until resync (decision 26; #88) — **done**;
- the `dcs-model` CLI reports a structural diff between two model documents, naming the elements a revision changed (#114) — **open**;
- a controller's checkpoints persist to a file and a restarted process recovers the run from one (#115) — **done**;
- a scripted demonstration runs the full stack — a `dcs-plant-server` plant, the redundant `dcs-controller` pair, and the monitoring surface — end to end (#116) — **open**.

### M6: Operator ergonomics and library breadth — in progress

The operator-facing surface and the reusable library grow together on the contracts already implemented: receipted parameter tuning on component targets, persistent input forcing, a named I/O-health and scan-overrun surface, model-declared display grouping, a broader `dcs-blocks` kind set, and plant-side dynamics as declared data beside the model. Decisions 20–24 record the approach (#89); decisions 25–26 record the redundancy-hardening semantics the M5 tickets #87 and #88 implement; decision 33 records the alarm-acknowledgment semantics #109 implements. The tuning, grouping, kind-set, dynamics, health, and forcing layers are done (#82, #85, #86, #81, #83, #84); the new library tickets (#109–#111) remain open.

Ticket breakdown:

- #89 — the M6 decision record and plan refresh (decisions 20–26). **Done.**
- #82 — `set_parameter` commands tuning component parameters at the scan boundary, descriptor-driven range enforcement, and the checkpoint-carryover obligation for tuned parameters (decision 20). **Done.**
- #83 — I/O-health counters, the optional `IoDriver::diagnostics` hook, and the paced-loop overrun feed in the telemetry snapshot (decision 22). **Done** — issue filed under M4; #107 is its page slice.
- #84 — persistent forcing of writable field `In` points at `Uncertain(Substituted)` quality, released at the scan boundary, listed and journaled (decision 21; its #49 dependency has landed). **Done** — `force_point`/`unforce_point` commands, the snapshot `forces` list, checkpointed force state, and the page badge; issue filed under M4; #106's page affordances build on it.
- #86 — `Signal.group` carried through `SignalIndex` into page grouping (decision 23). **Done** — issue filed under M4.
- #85 — timer, counter, and rate-limiter kinds in `dcs-blocks`, each shipping `KIND`/`from_parameters`/`describe` per the established convention. **Done.**
- Declared plant-side dynamics (decision 24). **Done** — `dcs-plant-server --dynamics` merges a JSON list of `ProcessElement` declarations (#81).
- #109 — the latching alarm kind with a declared `ack` input and the two-flag `alarm`/`unacknowledged` outputs (decision 33). **Open.**
- #110 — manual/auto station and signal filter kinds in `dcs-blocks`. **Open.**
- #111 — median voter and totalizer kinds in `dcs-blocks`. **Open.**

Done when:

- an operator tunes a declared parameter through a receipted `set_parameter` command — applied at the scan boundary, with named rejections for unknown component, unknown parameter, type mismatch, and out-of-range — and the tuned value restores through a checkpoint into a fresh executor (#82) — **done**;
- a writable field `In` point can be forced across scans at substituted quality, released at the scan boundary, badged in the snapshot, journaled, and preserved across a checkpoint (#84, with #49) — **done**;
- the telemetry snapshot reports executor-collected I/O-health counters, per-kind driver diagnostics where the driver implements the optional hook, and paced scan overruns fed by the controller shell (#83) — **done**;
- the monitoring page organizes its signal list by the model's declared `group` field, with ungrouped signals under a documented default (#86) — **done**;
- `dcs-blocks` ships the timer, counter, and rate-limiter kinds with descriptors and checkpoint coverage like every existing kind (#85) — **done**;
- plant-side dynamics load from a declared document beside the model for the shared plant server and test rigs, leaving `PlantModel` schema untouched (decision 24; #81) — **done**;
- `dcs-blocks` grows the latching-alarm, manual/auto-station and filter, and voter and totalizer kinds, each with the established `KIND`/`from_parameters`/`describe`/checkpoint convention — the latching kind acked through a writable internal `ack` point per decision 33 (#109, #110, #111) — **open**.

### M7: Engineering workflow and field-interface breadth — in progress

Engineering moves from hand-authored JSON to typed composition, and the hardware-abstraction seam is proven across a second transport shape. A `dcs-build` crate composes a plant in Rust — per-kind spec data mirroring the registered kinds' descriptors, typed port handles checking direction and value kind at compile time where the kind is statically known, and `build()` emitting the identical versioned `PlantModel` document with named errors for what types cannot carry (decision 31). A `dcs-sim-bus` register-mapped fieldbus kind — its own wire protocol, channel→register addressing in device parameters, and driver-owned `IoError` mapping — joins `sim`, `sim-tcp`, and `sim-scripted` under the driver registry (decision 32). The record also fixes two semantics other milestones consume: alarm acknowledgment rides a writable internal point wired to a declared `ack` input under the two-flag `alarm`/`unacknowledged` model, journaled through the ordinary settled receipt (decision 33 — implemented by M6's #109, displayed by M4's #108); and faceplate parameters stay descriptor-authoritative for ranges while current values are proposed as snapshot telemetry, with #105's declared-metadata edit surface as the accepted interim (decision 34).

Ticket breakdown:

- #104 — this decision record and plan refresh (decisions 31–34). **Done with this change.**
- #112 — the `dcs-build` typed composition seam: per-kind spec structs beside the descriptor vocabulary, a model builder whose `connect`/`bind` calls check direction and value kind at compile time, `build()` emitting the versioned document with named errors, a spec-versus-descriptor drift test, and a worked example reproducing the checked-in tank-loop fixture (decision 31). **Open.**
- #113 — the `dcs-sim-bus` register-mapped fieldbus: the wire protocol and `BusDriver` implementing `IoDriver`, the device-server binary, the `sim-bus` kind registered in `DriverRegistry`, and mixed-kind fan-out under `FanoutDriver` (decision 32). **Open.**

Done when:

- a plant composed in Rust emits a document that loads, validates, and assembles identically to a checked-in fixture; port existence, direction, and value kind are checked at compile time where the kind's spec is statically known; wrong-kind or wrong-direction connections and missing required parameters fail `build()` with named errors; and a checked drift test pins the specs to the registered kinds' `describe()` output (decision 31; #112) — **open**;
- a `sim-bus` device assembles through the registry with channel→register addressing declared in device parameters, scans deterministically beside the existing kinds under `FanoutDriver`, maps protocol failures onto the named `IoError` variants, and keeps stepping explicit and field-owned per decision 30 (decision 32; #113) — **open**;
- the semantics parallel milestones consume are recorded: latching alarms acknowledged through a writable internal `ack` point with the settled `WriteValue` receipt as the audit entry and the latch carried in checkpoints (decision 33; #109 implements, #108 displays), and descriptor-declared ranges governing faceplate edits with current parameter values proposed as snapshot telemetry (decision 34; #105 lands the interim surface) — **recorded**.

## Next planning pass

Inspect current main, open issues, and PRs before updating this plan. M2 closed with #49: #62's command affordances, #84's forcing, and the writable `ack` points decision 33 relies on now build on its writable-point contract. M3 is complete — #32, #46, #47, and #81 landed the pair mechanics and #64 proved the swap end-to-end between two controller processes over the shared simulated plant; automatic failure detection stays deliberately post-M3 as #65 under M5. M4's remaining bulk is the page work — #62's faceplates gate the #105, #106, and #108 slices, while #83 and #84 supply the contracts #106 and #107 render, and #69's showcase model gates #116. M5's lifecycle set runs on the shipped pair machinery: #65 is in progress, #66 has landed the versioned and fingerprinted checkpoint #87 and #115 build on, #88 has landed the divergence gate, and #114–#116 add revision-diff tooling, checkpoint persistence, and the full-stack demonstration. M6's open work is the #109–#111 library kinds — #83 landed the I/O-health and overrun surface and #84 the forcing contract. M7's two implementation tickets — #112's `dcs-build` and #113's `dcs-sim-bus` — are independent of each other. Keep between six and twenty ready issues only when that much independent work exists.

Update this document when milestones change or complete. Reference actual issue and PR numbers once created; mark blocked dependencies and distinguish completed work from planned work.
