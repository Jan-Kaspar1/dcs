# Completion-first factory

The production target is 50 CI-gated merged PRs per rolling 24 hours. This is an
objective measured independently of supervisor health, backlog size and configured
concurrency. No policy can manufacture provider quota.

## Decision and ownership

The original coordinator has independent planner, reviewer, retry and dispatch
launch paths, granted in loop order. The replacement uses `Factory.advance` as the
production decision interface: observe receipts and delivery, build one ordered
worker-demand projection, pull work through Admission, then consider background
work. State and Runtime retain durable jobs, process identities, launch intents,
branches, sessions and workspace recovery. The existing process lock and GitHub
adapter retain one publication and merge owner.

Three alternatives were evaluated: persistent multi-ticket inference sessions,
a staged delivery pipeline, and durable demand with one inference dispatcher.
The demand queue is selected because it can replace launch ownership without
replacing the proven preservation machinery. The installed CLI exposes ACP, but
multi-ticket session/cwd isolation has not been validated; it is not used or
claimed to save quota in this release.

The queue is reconstructed from SQLite jobs and the existing recovery/retry
records plus durable `repair:<issue>` and `delivery:<issue>` records. It does
not maintain a second issue ledger. Issue eligibility and actual launch still
cross the same dependency, improvement, workspace and atomic admission guards.
Within issue priority, repairs precede verified preserved work, then fresh work;
area allocation and oldest work break remaining ties. Closed issues cannot resume.

## Supply and delivery

A provider rate/endpoint failure leaves a valid item waiting. Its not-before time
honors the provider reset or the configured fallback delay. Diagnostic quota
counters never exhaust its eligibility in completion-first mode. Semantic code/CI
repair remains bounded; quota resumes preserve that separate budget. Auth/credit
blocks cannot be reopened by queue policy or model switching.

Exactly one recovery probe may be leased. Worker demand, including delayed
provider-waiting work, prevents optional planning or review taking that probe.
An empty worker frontier may need a planner to supply work. Architecture review
requires normal provider admission. Existing background invocations are harvested
and preserved rather than killed when a worker appears. Periodic/manual requests
remain pending until eligible; sustained worker demand can defer optional review.

There are four inference worker slots and eight workspace slots. PRs waiting for
CI retain their checkout while other checkouts run workers. Planner/reviewer share
provider admission and can run only when no worker or delivery demand remains.
Successful inference releases its lease before publication. A publication outage
preserves its durable receipt/workspace and does not rerun the implementation.
Other completed workers and green PRs proceed when another item needs repair or
an auxiliary stage fails. During an inventory outage, cached inventory permits
local receipt reconciliation but never new assignments or merges.

Provider feedback still governs adaptive concurrency. Desired ceiling is four;
recovery begins with one productive worker probe and grows after useful work and
loaded demand. A permanent three-slot floor is not evidence of three available
slots and is removed from the live policy. Active work is not killed on contraction.

## Activation and rollback

A validated `factory` object activates the new orchestration:

```json
"factory": {"worker_slots": 4, "workspace_slots": 8, "daily_merge_goal": 50}
```

Absent that object, the previous orchestration remains available for rollback;
legacy quota budget settings continue to apply there. This is a migration seam,
not permission for two coordinators. Enable only from a clean merged revision,
after protected config and online SQLite backups and work-preserving stop.
Selectively translate still-open blocked items only when their latest durable
outcome is rate/endpoint, the blocked reason is the corresponding local failure,
and preserved work is known. Preserve branch, clone, HEAD, session and unrelated
operator blockers. Seed the existing provider wait rather than clearing it.

Removing `factory` and restarting restores legacy orchestration; keep backups for
state rollback after assessing any new progress. Never blindly restore a database
over running/newly merged jobs. Newly recorded queued repairs must be reconciled
before a release rollback.

`dcs-agents status` exposes `factory`: actual worker inference, delivery backlog,
waiting issue/kind/deadline, workspace limits, merged-last-24h and goal, and phase
errors. A healthy service with zero productive workers is visible as such.

## Acceptance evidence

The factory-cycle tests use the real SQLite ledger and existing Runtime/GitHub
adapter fixtures. They cover worker-owned probes with due background work;
provider waits through exhausted legacy budgets/restart; deadline/auth guards;
publication outage without duplicate implementation; closed/dependency exclusion;
queued delivery repair precedence; independent inference and CI workspaces;
auxiliary failure isolation; cached-inventory receipt reconciliation; multiple
publication failures releasing leases; green PR delivery during queued repair and
quota wait; and semantic repair budgets surviving quota resumes.

The pre-change regression run failed on worker/probe selection, lifetime quota
exhaustion, publication capacity, queue repair and factory absence. Full repository
verification and installer preflight are required before rollout. A live recovery
probe, useful completion and post-change merges establish rollout results; tests
alone do not establish sustained 50/day throughput.

## Session start pacing

Factory mode spaces every managed session start by at least five seconds,
including fresh work, resumed work, repairs and background sessions. Set
`factory.launch_spacing_seconds` to a number from 0 through 30 (default 5);
zero disables pacing for comparison. A durable timestamp preserves the gap
across supervisor restarts. Concurrency remains bounded separately at four;
spacing changes admission bursts, not provider account limits or reset times.
Provider errors still trigger the existing recovery policy. This is an
experiment motivated by successful staggered desktop sessions; it does not
prove that launch bursts explain all observed rate-limit errors.
