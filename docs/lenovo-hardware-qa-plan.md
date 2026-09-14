# Implementation plan: Lenovo hardware QA

Status: proposed, not deployed. Prepared 2026-09-14.
Implementation order: second, after [daily architecture review](daily-architecture-review-plan.md).

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
   expected outcomes. It verifies pending fixes before unrelated exploration.
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
