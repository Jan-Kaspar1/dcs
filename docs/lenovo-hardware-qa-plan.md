# Implementation plan: Lenovo hardware QA

Status: simulation lane implemented and deployed. Prepared 2026-09-14;
runner + report contract landed 2026-09-15 (see below).
Implementation order: second, after [daily architecture review](daily-architecture-review-plan.md).

## Landed 2026-09-15 (HQ-1, HQ-2 deterministic slice, HQ-5 provisioning slice)

- `qa_lane/report.py`: versioned run-report contract (schema_version 1)
  with validator and passed/failed/interrupted fixtures — run identity,
  attempted vs completed SHA, image digests, per-scenario outcomes,
  capability limitations, infrastructure failures, action timeline.
- `qa_lane/state.py` + `qa_lane/runner.py`: SQLite-backed Lenovo
  supervisor — exact-SHA queue (newest wins, intervening range
  preserved), one active run via flock + oneshot unit, daily budget
  (4/day) and one auto-retry per inconclusive SHA, 2 h hard timeout,
  restart reconciliation of dead runs and labeled orphan containers.
- `qa_lane/scenarios/` (was `qa_lane/scenarios.py` until #928):
  deterministic checks over the simulated rig —
  active role + telemetry, standby convergence, writable-point command,
  controller restart recovery (`--state-file`/`--journal-file` on
  runner-owned per-controller paths, with a runner-owned container
  stop/start action on the run timeline), demote/promote failover,
  receipts/journal evidence — all through the documented monitor
  endpoints.
- `qa_lane/relay.py`: WSL-side sanitizer + publisher pushing
  `qa/latest.json` and `qa/run-<id>.json` through the Pi report key.
- `qa_lane/deploy/`: generic systemd unit/timer and config example.
- Build choice: no CI image pinning exists, so images are compiled on
  the Lenovo from the pushed `git archive` of the exact SHA inside a
  cpuset/memory/PID-limited builder container running the checked-in
  Dockerfile's build stage verbatim, then packaged into the same
  bookworm-slim + uid 10001 + entrypoint runtime contract. Documented
  as the interim path until CI-pinned images exist.
- Report-only: no GitHub issue publication (finding ingestion is a
  later task). Devin exploration phase gated behind the deterministic
  scenarios per the plan's ordering.

### Landed 2026-09-16 (charter-driven exploration, Task 3)

- `qa_lane/explorer.py` + `qa_lane/explorer_prompt.md`: `qax-*`
  exploration runs dispatch when no verification or assessment is due —
  the same build/rig skeleton, then a time-bounded Devin session
  (`swe-2-high`, dangerous permissions) on the host receives a rendered
  charter prompt with the run id, exact revision, changed range, mode,
  endpoints, UI URL, rig manifest, recent merges, open-backlog snapshot,
  pending fix verifications, the exploration ledger, and the forbidden
  actions list. The agent chooses its own charter under a novelty rule;
  there is no scenario list.
- Agent output contract: `results/agent-result.json` (charter, dynamic
  scenario results with stable mechanism keys, capability limitations,
  infrastructure failures, verification replays, coverage ledger,
  timeline) plus `exploration-summary.md`, `evidence/`, `scripts/` —
  validated and folded into the run report; malformed entries degrade to
  named infrastructure failures instead of sinking the run.
- `qa_lane/report.py` schema v3: optional top-level `mode` and
  `exploration` channel; per-scenario explicit finding fields
  (`module`, `mode`, `reproduction`, `severity`, `confidence`,
  `test_requirements`, `product_cause`) so exploratory defects publish
  real tickets instead of coordinator-default stubs. v1/v2 reports
  remain valid input.
- `agent_pool/findings.py`: `_scenario_finding` prefers the explicit
  schema-v3 fields; the pending-verification queue marks cases with no
  deterministic scenario as `replay: agent` — the exploration lane
  re-runs those reproductions, `qav-*` skips them.
- Run bookkeeping scopes to `qa-` prefixes so an exploration of an older
  verdicted revision never regresses `last_attempted_sha`, triggers a
  scenario retry, or lets a queued dedicated run be superseded.
- Cadence: `exploration_interval_seconds` spacing plus
  `max_explorations_per_day`, outside the assessment's daily budget;
  budgets are config so cadence can tighten without a code change.
- Session sandbox: host-level Devin process (not a container — it needs
  host loopback for the rig's monitor ports and egress to the Devin
  API), read-only worktree source, results dir under the run dir,
  process-group kill at the time budget. An ephemeral agent container
  remains future hardening work.

### Landed 2026-09-15 (fix verification, Task 2)

- `qa_lane/report.py` schema v2: optional `verifications` channel —
  finding key, original case identity, fix SHA, tested SHA, ancestry
  record, verdict, evidence. v1 reports remain valid input.
- `qa_lane/verify.py` + runner/state hooks: pending verifications arrive
  as `verifications.json` (`qa-verifications/1`) pushed by the WSL
  relay; `qav-*` verification runs dispatch ahead of the newest-SHA
  assessment, prove the tested revision contains the fix via
  `git merge-base --is-ancestor` in the lane's bare mirror (`git_dir`),
  and replay exactly the original case. A non-containing revision is a
  `blocked` report naming the check, never a verdict.
- `agent_pool/findings.py`: `apply_verification` requires matching
  finding key + case identity + this report's tested revision + ancestry
  proof + real evidence, plus the supervisor's own containment lookup;
  failed/ambiguous merge-SHA lookups stay pending and retry next poll,
  and transient containment failures park for retry. The pending queue
  is emitted as `verifications.json` beside the report inbox.

### Landed 2026-09-23 (rig endpoint placement record)

- The rig bridge-to-host reachability rule the qax-20260922-001,
  qax-20260922-005, and qax-20260923-001 exploration runs
  demonstrated is recorded: the host egress policy drops every
  rig-network packet aimed at a host socket, so a lane endpoint a
  rig container must dial (a checkpoint interposer, the
  forged-checkpoint endpoint the demote-forged-standby-source leg
  announces, or a plant-probe listener) runs bridge-placed in a
  labeled
  rig-bridge container dialed by container name, while host-side
  attachments use the published loopback ports only.
  `qa_lane/runner.py` records the selection in the run config's
  `endpoint_placement`, validates it before a launch trusts it, and
  hands it to the scenario ctx beside `rig_network`; the deploy
  README and the tracking-source-auth/shared-claim scenario docs
  reference it. Enabler for the WW-LCM-001 takeover-integrity legs.

### Landed 2026-09-24 (scenario module split, #928)

- `qa_lane/scenarios.py` became the `qa_lane/scenarios/` package: one
  module per schedule leg (`NNNN_<slug>.py`) carrying its `scenario_*`
  function, its leg-private helpers, and its private tunables;
  `common.py` holds the shared seam — the report `Case`, the HTTP and
  plant probe helpers, the settle/judge machinery, and the shared
  tunables — which every leg module binds through
  `from .common import *`. The package `__init__.py` facade re-exports
  every name the legs and common define, and its ModuleType
  `__setattr__` propagates `patch.object(scenarios, ...)` writes into
  common and every leg module binding the name, so the pool tests'
  module-attribute seam resolves exactly as it did on the monolith.
- Ordering rule: the `NNNN_` filename prefix is the run position;
  numbers are spaced by 100 so a new leg inserts between neighbors
  without renumbering. `SCENARIOS` is discovered by sorted glob over
  `[0-9]*_*.py`, so a new leg is exactly one new file and edits no
  shared file. Which window a leg may occupy is declared in its own
  module (later cases degrade to inconclusive when earlier rig state
  never landed; the dcs-ctl case deliberately closes the schedule).
- Motivation (scripts/merge_flow.py, #906): dispatch-to-merge lead
  time between adjacent 7-day windows collapsed — p50 0.64h -> 4.81h,
  p90 1.92h -> 61.92h — while redispatches went 58 -> 111 and
  WIP-preservation repairs 19 -> 41, because every open lane leg
  appended to the same ~19,900-line `scenarios.py` and serialized on
  identical regions.

### Landed 2026-09-24 (scenario test-module split, #940)

- `tests/test_qa_scenarios.py` (a ~16,400-line monolith) became one
  test module per leg — `tests/test_qa_scenario_NNNN_<slug>.py`,
  pairing by stem with `qa_lane/scenarios/NNNN_<slug>.py` — carrying
  the leg's feed fakes and TestCase classes verbatim. Fakes and
  helpers more than one leg's tests use live in the shared seam
  `tests/qa_scenario_support.py`, which leg modules bind through
  `from qa_scenario_support import *` — the same pattern the
  scenario modules use on `common.py`, so `patch.object(scenarios,
  ...)` seams keep resolving through the #928 facade.
- The ordering pin is distributed: instead of appending to a shared
  `EXPECTED_ORDER` list, each leg module declares the window it
  needs — `RUNS_AFTER`/`RUNS_BEFORE` frozensets over `scenario_*`
  names, `RUNS_LAST` for the dcs-ctl leg that closes the schedule —
  beside the ordering prose that moved with it, and
  `tests/test_qa_scenario_modules.py` derives the schedule check by
  validating every declaration against the discovered run order.
  Each leg test module also pins its own `EXPECTED_CASES` (its
  `Class.test_*` set), and the same structure module asserts the
  union reproduces the pre-split suite's coverage under unittest
  discovery.
- Convention: a new leg is exactly two new files —
  `qa_lane/scenarios/NNNN_<slug>.py` (carrying its ordering
  declarations) plus `tests/test_qa_scenario_NNNN_<slug>.py`
  (carrying its fakes, cases, and `EXPECTED_CASES`) — and edits no
  shared file; the pool tests prove a synthetic leg joins both the
  run order and test discovery that way.

### Landed 2026-09-24 (forged standby-source demote-verify leg, #882)

- The announced-source demote-verify contract (#850, landed #863
  a9b2a6f) is exercised on the deployed pair by scenario leg
  `2050_demote_forged_standby_source` in service of WW-LCM-001's
  takeover continuity and WW-FND-004's command integrity. Each pass
  opens the announced-only demotion window — the tracking peer
  stopped, the field owner warm-restarted — then stands the
  bridge-placed forged-checkpoint endpoint: `dcs-forge`, a
  monitor-crate binary the controller image ships beside
  dcs-controller and the runner launches with `--entrypoint
  dcs-forge` through the new ctx['start_forge']/ctx['stop_forge']
  actions, announcing `?peer=0.0.0.0:<port>` to the named owner's
  monitor so its bridge address is the hint POST /demote verifies.
- The endpoint serves a checkpoint document staged as a
  bind-mounted file inside the run dir — the scenario rewrites it
  between demote calls to run the receipt-window-forked and
  internal-`In`-planted forgeries, then the honest standby-shaped
  continuation — signs `?prove=` answers under the run's
  `--pair-token` only when launched keyed (the tokenless launch is
  the unproven leg's shape), and appends a hits JSONL ledger the
  scenario audits for the verify pull's arrival and signature. A
  refused demote must journal no tracking-source adoption and no
  role change, leave the peer field owner, and settle
  `no_tracking_source`; the honest document must adopt, journal its
  adoption naming the forge's address, and leave the demoted peer
  reconverged (orphaned) and re-promotable. Two passes must produce
  identical digests; named diagnostics are
  `demote-forged-standby-failed` and
  `demote-forged-standby-nondeterministic`.

### Landed 2026-09-26 (state-file sink-isolation leg, #999)

- The `--state-file` persistence-isolation contract (#982, in service
  of WW-FND-004's "no durable sink may pace the scan") is exercised on
  the deployed pair by scenario leg `3650_state_file_isolation`. The
  run config's new `state_file_mounts` map declares each controller
  endpoint's impede lever — the only declared kind, `fifo`, stages a
  reader-less FIFO at the sink's write-then-rename temporary sibling
  of `state.json` inside the controller's runner-owned bind mount, so
  the drain writer's next `open()` blocks inside the mount while
  captures queue behind the bounded handoff. The runner's
  `impede_state_file`/`restore_state_file` actions are handed to the
  scenario ctx; restore attaches a host reader that pairs the stalled
  open, drains the pending write through its rename onto the state
  path, and waits for an ordinary `state.json` again, unlinking the
  orphaned node when no writer attaches inside the grace.
- With the pair settled and tracking the leg stalls the field owner's
  mount, then through the serving monitor asserts the contract's
  claims: `publication.state_sink` reports the named `lagging` state
  while the served tick and `io_health` counters keep advancing inside
  the documented cadence bound, and a receipted command submitted
  inside the impeded window stands unanswered until the file has
  caught up through its admission — answered only after the drain
  covered it, then settled `applied`, with the durable file audited to
  cover the admission. Restoring the mount must drain the sink to
  `healthy` with no lost captures and reconverge the pair to one
  active plus one tracking standby with launch roles restored. Named
  diagnostics are `state-file-isolation-failed` and
  `state-file-isolation-nondeterministic`; two passes produce
  identical digests; a rig that is unreachable, that predates the
  served `state_sink` section, or whose run config declares no mount
  lever reports inconclusive.

## Outcome

Add a QA agent on the Lenovo ThinkCentre that evaluates an exact main revision,
understands recently delivered behavior, runs the DCS product on the dedicated
Wago rig, and returns defects and capability gaps to the existing WSL planner and
worker pool. The delivered DCS controller owns EtherCAT communication. Devin is
the tester, not the hardware driver or a runtime dependency of the product.

Begin with available simulated behavior. Missing controller packaging or EtherCAT
support should produce useful, deduplicated roadmap evidence rather than prevent
all QA activity. Physical tests become available as product capabilities land.

## Known baseline and unresolved hardware facts

Repository baseline inspected: main 051f290413b43a9ba170b533ecbd42a6f2f07536.
Refresh the current code and backlog before implementation. Existing foundation
tickets include #19 (controller/assembly), #39 (image), #48 (monitor integration),
#30/#49 (internal and writable points), and #47 (driver registry). #31 is remote
simulated I/O, not EtherCAT support. Reuse these tickets; do not duplicate them.

The photograph identifies a Wago 750-354 EtherCAT coupler. Attached labels appear
to identify a 750-501 two-channel digital output and 750-400 two-channel digital
input module. Confirm the labels, revisions, complete terminal order, and supply/
end modules before writing the device profile. No physical signal loopback has
been verified. Do not infer one from the power wiring in the photograph.

The proposed dedicated host NIC is `enx00e04c751f7c`; verify its live identity and
actual rig connection before deployment. A single EtherCAT station exposes its
terminal process data through the coupler; do not assume each terminal is a
separate EtherCAT endpoint.

Homelab documentation describes a Lenovo with 16 GB RAM, shared application
services, NVMe storage, and a Docker daemon whose lifetime depends on the HDD
mount. NVMe-only QA containers inherit that daemon dependency. Read current
homelab access, storage, networking, and operations instructions before changes.

## Architecture and ownership

| Part | Responsibility |
|---|---|
| Lenovo QA supervisor | SHA selection, artifacts, lifecycle, exclusive rig lease, budgets, reports, and recovery |
| DCS controller container | Delivered controller binary, plant model, monitoring, control logic, and EtherCAT driver |
| Ephemeral Devin container | Inspect source/intent, choose exploratory actions, operate DCS, and collect evidence |
| Existing WSL supervisor | Publish validated findings, coordinate planner decisions, dispatch workers, and merge through CI |
| Existing planner | Deduplicate capability gaps, choose dependencies, and maintain the roadmap |

Prefer an authenticated, narrowly scoped report upload or a WSL-side pull over
giving the exploratory agent GitHub write credentials. For an initial installation,
the host QA supervisor can publish through an explicitly scoped GitHub credential
using the same validation/idempotency contract. Choose one publisher before rollout;
two independent publishers must not race to create the same finding.

Keep the supervisor pinned and separate from the main revision being tested.
An application merge must not automatically upgrade host orchestration code.

## Repository and deployment layout

- DCS repository: a focused QA harness module (proposed `qa_lane/`), versioned
  report schemas/prompts, controller Dockerfile, simulated fixtures, rig model,
  and generic deployment examples.
- Homelab repository: sanitized Lenovo Compose/systemd configuration, resource
  allocations, networking policy, and dated operational verification.
- Lenovo private state: credentials, reports, exported conversations, run records,
  and configured host interface bindings, outside Git.

Suggested paths are `/srv/homelab/dcs-hwtest/` for deployment configuration and
`/srv/dcs-hwtest/` for bounded NVMe run storage. Confirm these paths against live
state before creation. Mount only the current run's results directory writable
into Devin; keep the scheduler database and previous reports supervisor-owned.

Reuse the architecture lane's report ingestion and candidate disposition contracts
where appropriate. Do not build a second general-purpose issue dispatcher.

## QA session lifecycle

1. Resolve main to one SHA. Trigger on new main, explicit reevaluation, or a bounded
   retry after an inconclusive run. Rig recovery can trigger a same-SHA rerun.
2. Acquire an exclusive host-side rig lease; reconcile orphaned containers and
   incomplete records before any new hardware session.
3. Prepare the exact source and a controller artifact linked to that SHA. Prefer a
   CI-produced image pinned by digest. If a build is required on Lenovo, isolate
   it from execution and apply build limits. Do not silently test a different SHA.
4. Gather changes since the previous assessed SHA, merged issue acceptance criteria,
   architecture/roadmap, open issues, rig manifest, and pending fix verifications.
5. Preflight artifact identity, resources, rig availability, and expected capabilities.
   Start the controller in simulation when hardware execution is not yet supported.
6. Start ephemeral Devin with the configured CLI and `swe-2-high`. The agent first
   determines what works today, then chooses an exploratory plan with observable
   expected outcomes. It verifies pending fixes before unrelated exploration
   (deterministic slice landed: `qav-*` verification runs replay a finding's
   original case ahead of the newest-SHA assessment).
7. Devin operates monitoring/commands/browser UI and approved harness actions.
   Persist evidence during the run so a timeout does not erase all diagnostics.
8. Validate the report, reconcile findings with the backlog, stop the controller,
   record the resulting rig state, and destroy run containers. Cleanup belongs to
   the supervisor and must also work after agent death.
9. Queue the newest main SHA if merges occurred during this run. Preserve the full
   intervening change range; testing every individual commit is not required.

Initial limits: one QA run, two-hour hard timeout, configurable maximum four runs
per day, and one automatic retry per inconclusive SHA. These are proposed defaults.
Store attempted SHA separately from completed assessment and hardware-pass status.
An assessed revision can legitimately be blocked by a known capability gap.

## Test subject: EtherCAT inside DCS

Create a product `dcs-ethercat` crate beneath the logical I/O interface. Evaluate
a userspace Rust master such as EtherCrab on the actual rig before choosing it.
Stack selection must verify compatibility, dependency terms, timing, and recovery.
No IgH host module is assumed by this plan.

The existing point-wise `IoDriver` needs explicit cyclic semantics: latch coherent
inputs, stage logical outputs, and publish the completed output image. Network
transactions must not occur for every point read/write. Define exchange timing,
stale-data quality, partial-scan failure behavior, and output-delivery diagnostics.
Current `Executor::read_inputs` restamps samples with the scan tick; separate
acquisition freshness from logical observation time before caching hardware data.

Extend the unified model for bus/device identity, profile and channel mappings,
and startup/fallback policy. Resolve a logical bus name to a host interface through
deployment configuration. One master owns each bus and serves its configured
channels. Unknown identities or incompatible process-data layouts fail startup
before outputs are enabled. Do not silently substitute simulation for requested
hardware operation.

Use the registry work in #47; split its generic contract from remote-simulation
integration if that prevents EtherCAT from depending unnecessarily on #31.

The paced controller owns scans. Manual `/scan` must be unavailable in hardware
run mode. Monitoring must identify the build/model, operational state, freshness,
communication failures, and missed deadlines. Successful command submission or
output staging is not proof of physical actuation.

## Rig contract and physical acceptance

Create a sanitized manifest describing verified module order, approved channels,
electrical ranges, expected device identity, interface binding, startup state,
watchdog response, and the physical feedback available. Record unverified fields
explicitly. Physical wiring is a commissioning prerequisite, not an agent guess.

First hardware slice:

1. Discover and validate the 750-354 station and configured process-data layout.
2. Read the digital inputs with meaningful quality/freshness.
3. Command DO1 through the DCS operator path and reusable control logic.
4. Observe both transitions independently through a verified DO1-to-DI1 loopback.
5. Repeat on channel 2 and check that the other channel remains unaffected.
6. Exercise controller shutdown/kill, link loss, and recovery using only available
   approved rig controls; document manual tests that cannot be automated.

Specify allowed response-time and watchdog bounds before acceptance, using the
actual module documentation and measured host behavior. Test under representative
homelab load. A prompt, final report, or software shutdown handler is not a
substitute for a verified device response when communication disappears.

Hardware redundancy is a later milestone. Checkpoint transfer and an application
write gate do not alone prove exclusive EtherCAT ownership or uninterrupted
takeover across hosts. Do not run two masters on this segment to test redundancy
without an independently accepted ownership/topology design.

## Isolation and lifecycle checks

Only the controller receives the dedicated field network and required capability
set. Validate macvlan/raw-socket operation with this USB NIC before committing to
that topology. No host networking, Docker socket, or broad host mounts for Devin.
Drop unneeded capabilities and retain standard container security controls.

Enforce egress policy on the host: a Docker bridge is not a GitHub-only restriction.
Permit actual auth/inference/source dependencies and deny unintended host, LAN,
other-container, and IPv6 access. Keep the controller monitoring network private
to QA. Mount only explicitly needed credential files, not a whole config directory.

Bound CPU, memory, swap, PIDs, build storage, results, and logs. Check free space
before launch; weekly pruning alone cannot prevent one run filling the disk.
Validate effects on existing services under load. Add restart reconciliation,
timeout teardown, and checks for the documented Docker HDD dependency.

## Findings and roadmap integration

Use a versioned report with run ID, SHA/image digest, model and rig identity,
capabilities exercised, coverage limitations, action timeline, receipts, telemetry,
physical evidence, and per-case outcomes. Redact credentials before persistence
or issue publication. Treat source, issue text, and report content as untrusted data.

| Finding | Route |
|---|---|
| Reproduced product defect | Validated managed issue with reproduction, expected behavior, evidence, and worker-test requirements |
| Missing capability | Planner candidate, or evidence attached to an existing roadmap issue |
| Rig, build, credential, or agent failure | Infrastructure/inconclusive record; product ticket only when evidence identifies a product cause |

Use stable finding keys and reconcile existing issues before publication. Record
severity separately from confidence. Do not label every discovery P0. Keep priority
metadata and labels consistent; dispatcher order is based on metadata. Use the
affected module as concurrency group rather than serializing all bugs as `hw-bugs`.

The planner accepts, defers, or rejects capability proposals and updates the roadmap
through normal reviewed changes. Workers reproduce with simulation or captured
data wherever possible; physical access stays in the dedicated QA role.

Persist `finding -> issue -> merged fix SHA -> verification case -> result`.
Merged does not mean hardware-verified. For failed fixes, create a linked follow-up
or explicitly support redispatch: the current dispatcher skips already recorded
issue jobs, so merely reopening an issue is insufficient.

## Implementation tickets, in order

Proposed work packages only; assign real issue IDs when publishing. Existing
product prerequisites should remain their current tickets.

| ID | Work | Dependencies | Acceptance |
|---|---|---|---|
| HQ-1 | Define QA report/rig schemas, role, state, and ownership contracts | Architecture lane ingestion conventions | Simulated reports validate; rig unknowns explicit; secrets excluded |
| HQ-2 | Implement exact-SHA simulation sessions and bounded Devin execution | HQ-1; available controller path | Real report from available DCS behavior; crash/restart and newest-SHA queue tested with fakes |
| HQ-3 | Integrate finding publication, planner candidates, and fix queue | HQ-1, HQ-2 | Seeded simulated defect enters worker loop once and is reverified after merge |
| HQ-4 | Deliver cyclic I/O, driver configuration, and EtherCAT/Wago integration | Existing #19/#47 path; accepted cyclic/model contracts | Simulation contract tests plus actual rig compatibility evidence; shipped image contains the driver |
| HQ-5 | Provision Lenovo deployment and commission rig | HQ-2, HQ-4; #39/#48 and writable command path | Isolation/resource tests pass; hardware identity, wiring, watchdog, and digital feedback verified |
| HQ-6 | Complete unattended hardware feedback loop | HQ-3, HQ-5 | Hardware finding becomes a worker fix, passes CI, merges, and passes the original physical reproduction |

Split HQ-4 into contract, per-crate implementation, integration, and hardware
acceptance tickets once the stack/profile experiment resolves exact requirements.
Do not mark hardware acceptance ready for simulation-only workers. Generic QA
plumbing can proceed before the hardware prerequisites are complete.

## Verification, rollout, and rollback

Test state transitions, report parsing, GitHub idempotency, retries, timeouts, and
container reconciliation with fakes. Run `python3 scripts/verify.py` for software
changes. Use a test repository or isolated dispatcher target for seeded defects;
never silently inject a deliberate bug into production main.

Roll out simulation/report-only, then controlled publication, then supervised
hardware acceptance, then unattended cycles. Record dates and untested steps in
homelab documentation after actual infrastructure changes.

Rollback disables the QA scheduler, stops its owned containers, and preserves
reports/state. Verify the rig's configured fallback state. Leave unrelated Docker
stacks, WSL workers, and already accepted backlog items intact.

## Completion gate

The Lenovo tests the delivered DCS artifact, independently observes digital
feedback, publishes evidence without duplicates, feeds capability gaps into the
roadmap, survives interrupted sessions, and verifies a worker-produced fix on
hardware. Scheduling and deployment are not authorized merely by this document.
