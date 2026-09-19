# QA findings ingestion and fix verification

Closes the findings-to-fix loop described in
[docs/lenovo-hardware-qa-plan.md](lenovo-hardware-qa-plan.md): QA evidence from
the Lenovo lane returns to the WSL supervisor, which is the **sole GitHub
publisher**. QA runners and exploratory agents never hold GitHub credentials;
they deliver one versioned JSON report per assessed revision into the
supervisor's findings inbox, and `agent_pool/findings.py` does the rest.

## Report contract

The wire contract is `qa_lane/report.py` — the single source of truth.
Schema v2 adds the optional `verifications` channel (v1 documents remain
valid input). `findings.validate_report` delegates to
`qa_lane.report.validate_report`, then `adapt_report` maps the validated
document onto the lane's internal shape:

| Report field | Internal field |
| --- | --- |
| `completed_sha` or `attempted_sha` | `sha` |
| `outcome` `passed`/`failed` | `status: completed` (a finished assessment) |
| `outcome` `blocked`/`inconclusive`/`interrupted` | `status` = the outcome |
| `host.name` (sanitized) | `rig` |

Findings are **derived** from the report's three evidence channels — the
runner emits scenarios and limitation/failure records, not findings:

| Report channel | Finding kind | Route |
| --- | --- | --- |
| scenario `outcome: failed` | `defect` (severity medium, confidence high — a deterministic case reproduced on the exact tested revision; schema-v3 scenario fields override the defaults) | managed issue |
| `capability_limitations[]` | `capability` (severity medium when `blocking`, else low) | planner candidate |
| `infrastructure_failures[]` | `infrastructure` | operational record |
| scenario `outcome: blocked`/`inconclusive` | `infrastructure` (rig-side cause, not a product defect) | operational record |

Finding keys are the scenario/limitation/failure keys — stable across runs,
so re-observation dedups by key (occurrences increment, evidence refreshes).
The schema-v2 `verifications` channel carries fix-verification results from
dedicated verification runs (run ids `qav-*`) on the Lenovo lane; each entry
names the finding key, the original case identity, the merged fix SHA, the
revision actually tested, the runner's ancestry check, the verdict, and the
evidence.

Schema v3 adds the exploratory lane (run ids `qax-*`): a time-bounded Devin
session explores the running rig under a self-chosen charter and reports
through `results/agent-result.json`. Its scenario entries may carry explicit
`module`, `mode`, `reproduction`, `severity`, `confidence`,
`test_requirements`, and `product_cause` fields — when present they replace
the derived defaults, so an exploratory defect publishes a real ticket (true
module, true reproduction, the agent's own severity/confidence judgments,
and a worker regression contract) instead of a generic `qa-lane/<key>` stub.
Queue items whose case identity maps to no deterministic scenario are marked
`replay: agent`: `qav-*` dispatch skips them and the exploration lane re-runs
the reproduction itself, reporting through the same `verifications` channel.

Only `completed` reports route findings; inconclusive/blocked/interrupted
runs are recorded as evidence, matching the review lane's rule that partial
output is never accepted as authoritative. Credential-shaped text is
redacted before persistence and issue publication.

## Delivery seam

Reports arrive as files in `<state_root>/qa/reports/*.json` (configurable via
`qa.report_dir`). The live transport is the WSL relay
(`dcs-qa-sync.timer` → `dcs-qa-sync.sh`): it pulls new `reports/*.json` off
the Lenovo each pass and drops a raw copy into the inbox, in addition to
publishing the sanitized `qa/run-<id>.json`, `qa/latest.json`,
`qa/runs.json`, and `qa/activity.json` documents to the Pi. Ingestion is
file-based and idempotent — any equivalent drop works. Validated reports
move to `qa/processed/`; rejected ones to `qa/rejected/` with an
`.error.txt` alongside. A repeated `run_id` is a duplicate, not a new
report.

## Routing

| Finding | Route |
| --- | --- |
| `defect` (or `infrastructure` with `product_cause`) | Managed issue: `<!-- dcs-task: -->` metadata + reproduction, expected behavior, evidence, worker-test requirements; `agent:ready` label |
| `capability` | Planner candidate via the review-lane `candidates` table (`source: "qa"`); the planner accepts/defers/rejects it and any accepted issue maps back through `improvement` |
| `infrastructure` | Operational record only (`status: infra`) |

Dedup and reconciliation, in order: the `qa_findings` row keyed by the stable
finding key suppresses re-reporting (occurrences increment, evidence refreshes);
`<!-- dcs-agent-key:qa-<key> -->` markers in the full issue inventory are
matched before creating anything, so a finding that already has an issue —
open or closed — is adopted or treated as a regression rather than
duplicated.

Severity maps to priority labels `{critical, high -> P1, medium -> P2, low ->
P3}`: the lane never emits P0, which stays reserved for human escalation.
Confidence is recorded separately and carried into the issue body. The
concurrency group is the affected module name, so QA defects serialize per
module rather than through a single `hw-bugs` group.

## Lifecycle and the verification chain

`qa_findings.status`:

```
recorded -> held | infra | candidate | issue-open
held --------(capacity frees)--------> issue-open
candidate --(planner)--> accepted | deferred | rejected
issue-open --(job done, PR merged)--> fix-merged --(report verification)--> verified
fix-merged --(verification failed)--> failed --(bounded follow-up)--> redispatched -> fix-merged
verified --(same defect reproduced)--> failed
failed --(max_fix_cycles exhausted)--> unresolved
```

The chain `finding -> issue -> merged fix SHA -> verification case -> result`
is persisted: `issue` and `fix_sha` columns link each finding to its managed
issue and the squash-merge SHA; `pending_verifications` (state accessor and the
top of `qa/findings.json`) hands the QA scheduler the merged fix's **original
reproduction** so pending verification runs ahead of fresh exploration.

## Fix verification (Task 2)

The queue flows WSL -> Lenovo and the verdict flows back:

1. `findings.verification_queue` renders every `fix-merged` finding with a
   known `fix_sha` as a `qa-verifications/1` document — finding key,
   original case identity, fix SHA, reproduction, expected — written
   beside the report inbox (`<report_dir>/../verifications.json`,
   content-hashed/idempotent). The WSL relay pushes it to
   `/srv/dcs-hwtest/verifications.json` and keeps the lane's bare git
   mirror (`/srv/dcs-hwtest/repo.git`) current, so verification work
   reaches Lenovo without GitHub credentials.
2. Each Lenovo cycle dispatches due verifications **before** the
   newest-SHA assessment (`qav-*` runs). A verification run proves the
   tested revision *contains* the fix — `git merge-base --is-ancestor`
   in the mirror — before any build; a non-containing revision produces
   a `blocked` report naming the failed check instead of a verdict. A
   containing revision replays exactly the original scenario case and
   records verdict + ancestry + evidence in `verifications`.
3. Back on WSL, `apply_verification` certifies only when the entry
   matches the awaiting finding, the original case identity, and this
   report's tested revision; carries `fix_ancestry.contained` and real
   evidence; and the supervisor's own containment lookup
   (`github.includes_main(tested_sha, fix_sha)`, the compare-API
   equivalent of merge-base) agrees. Anything less stays pending:
   `case-mismatch`, `missing-evidence`, `ancestry-unproven`,
   `untested-revision`, `sha-mismatch`, `not-contained`.
4. Failure preservation: a failed/ambiguous merge-SHA lookup leaves the
   finding `issue-open` and retries next poll; a transient GitHub
   containment failure parks the entry in `qa:verification_retries` and
   `retry_verifications` re-applies it next poll. Neither ever advances
   a chain on an unknown fix SHA.

Merged does not mean verified. For a failed fix the lane creates a *linked
follow-up issue* (`qa-<key>-fix<N>`, dependency on the prior issue, failing
evidence embedded) — a bare reopen is insufficient because the dispatcher
skips issues already recorded in the jobs table. `qa.max_fix_cycles` bounds
the loop (default 2 fix attempts); exhaustion leaves the finding `unresolved`
for human triage. `qa.max_open` bounds in-flight findings (overflow goes to
`held`), and `qa.max_issues_per_report` bounds issue creation per report and
per sweep.

## Configuration (`~/.config/dcs-agents/config.json`)

| Key | Default | Meaning |
| --- | --- | --- |
| `qa.enabled` | `false` | Master switch for report ingestion. |
| `qa.mode` | `record` | `record` validates and records only; `route` additionally creates issues/candidates and follow-ups. Staged rollout: `record` first, then `route`. |
| `qa.dashboard` | `false` | Publish `qa/findings.json` to the Pi dashboard. Keep off until the Task 2 view renders it. |
| `qa.report_dir` | `<state_root>/qa/reports` | Inbox directory. |
| `qa.max_open` / `max_issues_per_report` / `max_fix_cycles` | `20` / `5` / `2` | Bounds. |
| `qa.publish.*` | provisioned Pi channel | `document` (default `findings`), `identity`, `host`, `known_hosts`. |

Publication is opportunistic and content-hashed: the document is only pushed
when it changes, and a down channel logs an error and retries next cycle.

## Seeded-defect demo (isolated target)

Covered end to end by `tests/test_findings.py::EndToEndTests` with fakes. For
a live demonstration use a throwaway repository — never inject a defect into
production `main`:

1. `gh repo create Jan-Kaspar1/dcs-qa-sandbox --private` and create the
   `agent:ready`/`priority:P1`/`priority:P2`/`priority:P3` labels.
2. Point a scratch `State` + `findings` at a temp `report_dir` and the sandbox
   repo (`GitHub('Jan-Kaspar1/dcs-qa-sandbox')`); drop a seeded defect report.
3. `findings.poll(...)` produces exactly one issue (`qa-<key>` marker); a
   second run and a second report produce none — the marker and the findings
   table dedup.
4. Exercise the worker/CI flow by hand or with the dispatcher fakes; once the
   job is `done`, `reconcile_merged` records the merge SHA and the finding
   goes `fix-merged`; `document()['pending_verifications']` carries the
   original reproduction and `verifications.json` reaches the Lenovo lane.
5. The verification run replays the original case on a containing
   revision (`passed`) → `verified`; (`failed`) → a linked
   `qa-<key>-fix2` follow-up, bounded by `max_fix_cycles`.

Remaining for a fully live demo: a real worker pass against the sandbox repo
needs CI checks (`rust-*`, `supervisor-tests`) and a dispatcher instance; both
are exercised by the fakes test, and a real-agent run is gated on Task 1's
runner plus deliberate supervisor config (`qa.enabled`/`mode`).
