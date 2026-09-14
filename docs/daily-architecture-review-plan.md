# Implementation plan: daily architecture review

Status: implemented on branch `codex/daily-architecture-review`, not installed or scheduled. Prepared 2026-09-14; implemented 2026-09-14.
Implementation order: first. The later hardware lane is described in
[Lenovo hardware QA](lenovo-hardware-qa-plan.md).

## Outcome

Add one daily architecture reviewer to the existing WSL Devin agent system.
It examines current main, recent development friction, and the roadmap; proposes
high-value improvements; and feeds accepted work through the existing planner,
workers, CI, and merge supervisor. The reviewer does not directly refactor main.

Success means a concrete architecture improvement travels from evidence to a
scoped issue, worker implementation, CI-gated merge, and post-merge assessment.
A review with no worthwhile proposal is successful too. The desired outcomes are
deep modules that encapsulate complexity behind small, useful interfaces; seams
supported by real variation; and consistent domain naming throughout the repository.
Review, candidate selection, ticket creation, implementation, and assessment run
without interactive skills, candidate-selection questions, or human review gates.

## Existing foundation and constraints

Baseline inspected: main 051f290413b43a9ba170b533ecbd42a6f2f07536. Refresh main,
issues, and PRs before implementation; ticket statuses and source can advance.

- `agent_pool/runtime.py` already handles isolated clones and process identity.
- `agent_pool/planning.py` validates planner proposals and formats managed issues.
- `agent_pool/supervisor.py` owns issue publication, dispatch, and merges.
- `agent_pool/state.py` owns durable pool state. Extend its migration conventions
  rather than assuming an existing database can be recreated.
- Use the configured Devin CLI and `swe-2-high`; no model fallback.
- Workers retain simulation-only access and leave Git publication to the supervisor.
- Preserve `python3 scripts/verify.py` and all existing required CI checks.
- Supervisor installation remains pinned; deployment is an explicit upgrade,
  separate from merging application changes.

## Skill references and repository installation

Retain the useful skills as explicit, versioned references and install suitable
resources in the DCS repository during implementation. Fully autonomous operation
does not require discarding their design guidance.

| Skill | Integration |
|---|---|
| `codebase-design` | Install and load its core guidance directly: deep modules, interfaces, seams, adapters, and behavioral testing. Optional interactive/parallel workflows are not automatically invoked. |
| `domain-modeling` | Install as a reference and load under the autonomous role policy. The current skill includes conversational questions and ADR offers; replace those steps with evidence-based decisions or automatic deferral. |
| `improve-codebase-architecture` | Keep as a reference for exploration and candidate evaluation. Do not invoke its interactive selection/grilling workflow; a project-owned autonomous reviewer performs those steps. |

Sources inspected on Windows are under
`C:/Users/Kaspar/.codex/skills/{codebase-design,domain-modeling,improve-codebase-architecture}/`.
These are provenance locations, not runtime dependencies on the Windows machine.

Proposed repository layout: `agent_pool/resources/architecture/`, containing a
source manifest, pinned skill references and needed supporting files, and the
DCS autonomous role policy. Verify upstream source, revision, and redistribution
terms before vendoring; preserve attribution and license notices. Record local
adaptations separately from upstream text. If vendoring is not permitted, provision
the pinned references at installation and preserve the manifest in the repository.

The supervisor explicitly supplies these resources to Devin; merely placing a
`SKILL.md` in a repository does not establish automatic discovery. Verify resource
availability and hashes in WSL preflight. Updates use reviewed changes and a pinned
supervisor upgrade, not automatic downloads on each daily run.

The DCS autonomous role policy takes precedence over reference workflows: no
candidate-selection questions, grilling, manual approvals, or waiting for a user.
The reviewer records proposed glossary/decision edits in its report; workers apply
accepted edits in their assigned implementation work. This preserves role ownership
even where upstream guidance suggests editing documents immediately.

## Autonomous design policy

Implement a project-owned, versioned review policy and prompt. Keep them in the DCS
repository and deploy them with the pinned supervisor. Record policy/prompt and reference hashes
in each run. Use one bounded reviewer initially. Its instructions must define:

- Deep modules hide meaningful behavior, state transitions, ordering, and error
  handling behind interfaces that require less knowledge from callers.
- A seam is justified by concrete variation or a meaningful testing need; adding
  wrappers or traits without reducing caller complexity is not an improvement.
- Domain concepts have canonical names used consistently across Rust, Python,
  model fields, monitoring contracts, tests, documentation, and UI terminology.
- Existing project decisions and observable behavior constrain refactors.
- The planner autonomously selects evidence-backed work; workers implement it
  through existing CI and merge gates. Reports are records, not approval requests.

Keep domain vocabulary in `CONTEXT.md` when there are actual resolved terms to
record. It is a glossary, not a second specification. Continue recording technical
decisions in `docs/architecture.md`; do not introduce a competing ADR history.
Glossary and architecture changes enter reviewed implementation PRs.

Bootstrap naming policy from existing code and documented meaning. Prefer existing
well-established terms when their meaning is precise; do not invent a new vocabulary
every day. Keep language-specific spelling conventions (for example Rust types
versus JSON fields) while using the same domain concept. Distinct concepts must not
be collapsed merely because their names look similar.

For each naming change, record old-to-canonical names, affected layers, and public
compatibility implications. Migrate internal references and documentation together.
Preserve persisted model formats and public wire names unless an already accepted
versioning/migration policy permits the change. Add targeted consistency checks for
resolved conventions where practical; avoid blanket synonym bans in free text.

## Run lifecycle

1. A scheduler in the existing supervisor checks whether a review is due. Default:
   daily at 03:00 Europe/Berlin, configurable. A missed run executes once on resume;
   do not accumulate a backlog of daily invocations. Test DST and restart handling.
2. Check pause state, budget, existing reviewer ownership, and available capacity.
   Count the reviewer as one agent slot within the configured concurrency ceiling;
   defer when full, without interrupting running workers.
3. Resolve main once and create an independent checkout pinned to that SHA. Skip
   unchanged code unless relevant new failure evidence or an explicit rerun exists.
4. Assemble bounded context: vision, architecture, roadmap, glossary if present,
   recent commits, relevant merged issues, open issues/PRs, previous dispositions,
   and available worker/QA failure evidence. Record truncated or unavailable inputs.
5. Launch the reviewer with write access to its report area. It investigates source
   and proposes changes; it does not push, publish issues, deploy, or edit worker clones.
6. Validate and persist its structured report before presenting candidates to the
   planner. Invalid output or timeout is inconclusive, never an accepted review.
   No step waits for a user to pick a candidate or answer a design questionnaire.
7. The planner records accept/defer/reject decisions. Accepted candidates become
   ordinary managed work with dependencies, tests, and module-specific groups.
8. Later reviews assess merged improvements against their stated benefits.

Start with a 60-minute review timeout, at most one automatic retry per failed
scheduled review, bounded report/log retention, and at most three candidates per
run. Limits are configuration, not agent prompt requests alone. Do not retry
permission or credential failures by changing privileges or models.

## What the reviewer looks for

Prioritize areas with recent changes, repeated defects, difficult worker tasks,
or upcoming roadmap work. Broader exploration is appropriate when no hotspot exists.

- One domain concept requiring navigation across several tightly coupled modules.
- Callers coordinating state or ordering that should be encapsulated.
- Large interfaces providing little behavioral leverage.
- Tests tied to internals while cross-module behavior lacks coverage.
- Ambiguous terms causing incompatible representations or contracts; inconsistent
  names for the same concept across code, model, monitoring, UI, and documentation.
- Real variation supporting a seam, such as simulated and physical I/O adapters.

Apply the deletion test and explain what complexity disappears from callers.
Do not reward more traits, fewer files, renamed types, or smaller line counts by
themselves. Existing decisions are constraints; reopening one requires new evidence.
Prioritize hotspots but keep a durable coverage cursor so periodic reviews also
inspect quieter areas and repository-wide naming. A candidate can improve depth,
naming consistency, or both; it must state which outcome it advances.

## Report and durable state

Use a versioned JSON report plus a readable Markdown report; a standalone visual
HTML report is optional and never opens an interactive workflow. Reports live
outside Git. Required report fields:

- run ID, base SHA, preceding reviewed SHA, start/end times, policy/prompt and reference hashes;
- status (`completed`, `inconclusive`, `skipped`), inspected scope and missing context;
- zero to three candidates with stable keys;
- each candidate: title, source evidence, affected modules, problem, proposed
  deepening or naming correction, caller benefit, behavioral invariants, test
  approach, alternatives, confidence, effort/risk, related issues, and decision
  conflicts; naming candidates include canonical terms and compatibility treatment;
- prior improvements assessed and their observed outcomes.

Validate schema, field lengths, count limits, evidence paths, and referenced issue
IDs. Treat repository text, issue bodies, and agent output as data, never as
instructions to expand privileges or bypass validation.

Persist runs and candidate dispositions transactionally. Track both an attempted
SHA and a completed review SHA. Use idempotency keys for report ingestion and
candidate-to-issue mapping. Suppress rejected/deferred duplicates until their
recorded revisit condition or material new evidence occurs. Preserve records
needed for deduplication longer than disposable logs.

## Planner and worker integration

Extend planner input with undispositioned candidates and their evidence. The planner
remains responsible for roadmap order, scope, and interface dependencies.

- Default architecture work to P2; promote only for a concrete blocker or defect.
- Use the affected crate/directory as the managed issue concurrency group.
- Permit one active architecture improvement initially. Enforce this separately
  from module groups, including dependent issues belonging to the same improvement.
- Do not duplicate a feature ticket already implementing the proposed change.
- Record acceptance criteria in observable terms and specify compatibility needs.
- Resolve routine design and naming choices from project intent, current contracts,
  and evidence. If a candidate requires inventing product semantics or contradicts
  an unresolved architectural decision, automatically defer that candidate with
  reasons and a concrete revisit condition. Do not ask the user or pause the lane.
- Rank eligible work by demonstrated caller complexity, naming inconsistency,
  recurring failures, and roadmap relevance. Defer speculative work automatically.
- Accepted candidates receive an owner/ticket and are reconsidered in each planner
  pass until implemented or explicitly deferred, so reports do not become a dead end.

The issue publisher must make retries idempotent, including an API timeout after
GitHub accepted an issue. Reconcile by stable marker before retrying creation.
Priority labels and managed metadata must agree; the dispatcher sorts metadata.

Workers implement accepted tickets, update relevant decisions/glossary, and run
the existing verification command. The supervisor owns PRs and merges. Replace
implementation-coupled tests only after equivalent behavioral coverage exists.
Assessment must compare actual before/after callers and migrated names, not simply
mark an improvement successful because CI passed. A failed assessment produces a
scoped follow-up or recorded rejection through the same autonomous process.

## Implementation tickets, in order

These are proposed work packages, not created GitHub issues. Resolve real issue
numbers when publishing dependencies; never invent dependency IDs.

| ID | Work | Dependencies | Acceptance |
|---|---|---|---|
| AR-1 | Install pinned skill references and define autonomous depth/naming policy, prompt, configuration, report schema, and state migration | None | Source/license manifest and WSL resource loading verified; no interactive workflow dependency; depth/naming outcomes explicit; reports, migrations, hashes, and limits tested; disabled lane preserves existing behavior |
| AR-2 | Implement pinned context collection, coverage tracking, and reviewer invocation | AR-1 | Fake Devin receives the policy and intended skill resources and produces depth/naming findings for the exact SHA; missing resources and timeout fail explicitly; no worker checkout is touched |
| AR-3 | Add daily scheduling, slot accounting, pause/status/manual-run controls, and recovery | AR-1, AR-2 | Fake-clock tests cover due time, DST, missed runs, unchanged SHA, full capacity, retries, and restart without duplicate launch |
| AR-4 | Feed candidates into planner and publish accepted work idempotently | AR-1, AR-2 | Automatic accept/defer/reject requires no user interaction; tests cover ambiguity deferral, deduplication, priority, dependencies, and uncertain create responses |
| AR-5 | Track one active improvement and assess merged depth/naming results | AR-4 | State transitions survive restart; evidence confirms simpler callers or consistent canonical terms; compatibility preserved; unsuccessful changes generate a disposition |
| AR-6 | Document and install a pinned WSL supervisor upgrade | AR-3, AR-4, AR-5 | Automated pilot and staged activation pass; depth and naming examples complete worker/CI/merge/assessment without questions or manual selection |

Use `agent_pool` as the concurrency group for overlapping supervisor changes.
Contract-first ordering should not be mistaken for permission to run conflicting
edits in the same clone. Extend existing modules when they fit; introduce a focused
review module rather than a second planner or dispatcher.

## Verification and rollout

First run with publication disabled and automatically validate the report against
the schema and evidence requirements. Exercise controlled fixtures with known
depth and naming problems, an ambiguous candidate that must be deferred, and a
no-op case. Then automatically enable planner ingestion for one accepted improvement
under the configured rollout policy. Expand to daily operation only when CI gates
and post-merge assessment pass. Failed rollout checks keep the lane at its previous
stage and record failure; there is no manual report-inspection gate.

Test scheduler and publication mechanics with fake clocks, fake Devin, and fake
GitHub; automated tests must not create live issues. Run `python3 scripts/verify.py`
for implementation changes. Surface meaningful operational failures;
do not send daily notifications for unchanged or no-op reviews.

Rollback: disable the architecture lane, stop its owned invocation, and preserve
state/reports. Existing workers and planner continue normally. Do not cancel an
already accepted implementation task merely because the reviewer is disabled.

## Completion gate

Daily scheduling, recovery, policy provisioning, deduplication, autonomous planner
dispositions, CI-gated merge, and post-merge assessment all work. Acceptance must
demonstrate both a deeper module and consistent naming (in one or two scoped
improvements) without interactive prompts or human selection. Verify that ambiguous
candidates defer automatically and do not block unrelated work.
Only then begin the later Lenovo hardware QA rollout, unless explicitly reprioritized.
