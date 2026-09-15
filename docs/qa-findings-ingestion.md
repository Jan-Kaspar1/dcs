# QA findings ingestion and fix verification

Closes the findings-to-fix loop described in
[docs/lenovo-hardware-qa-plan.md](lenovo-hardware-qa-plan.md): QA evidence from
the Lenovo lane returns to the WSL supervisor, which is the **sole GitHub
publisher**. QA runners and exploratory agents never hold GitHub credentials;
they deliver one versioned JSON report per assessed revision into the
supervisor's findings inbox, and `agent_pool/findings.py` does the rest.

## Contract seam (Task 1)

`findings.validate_report` accepts a vendored copy of the report contract
documented in the plan's "Findings and roadmap integration" section. Task 1
lands the authoritative `qa_lane/` schema; when it merges, reconcile field
names in `validate_report`/`validate_finding` against it. Current accepted
shape:

```json
{
  "schema_version": 1,
  "run_id": "qa-20260915-a1b2c3",
  "sha": "<40-hex main revision under test>",
  "image_digest": "sha256:...",        // optional
  "model": "swe-2-high",
  "rig": "simulated",                  // rig identity string
  "started_at": "...", "ended_at": "...",   // ISO-8601
  "status": "completed",               // completed | inconclusive | failed
  "capabilities": [], "limitations": [],
  "findings": [{
    "key": "stable-kebab-case-finding-key",   // stable across runs
    "kind": "defect",                         // defect | capability | infrastructure
    "module": "crates/dcs-core",              // affected module -> concurrency group
    "severity": "medium",                     // low|medium|high|critical
    "confidence": "high",                     // low|medium|high (recorded separately)
    "title": "...", "summary": "...",
    "reproduction": "...", "expected": "...",  // required for defects
    "test_requirements": "...",               // worker-test requirements
    "evidence": [{"detail": "...", "source": "..."}],
    "product_cause": false                    // infrastructure only: evidence
                                              // identifies a product cause
  }],
  "verifications": [{
    "finding_key": "...", "fix_sha": "<40-hex>",
    "case": "original reproduction re-run",
    "outcome": "passed",                      // passed | failed | inconclusive
    "evidence": ["..."]
  }]
}
```

Only `completed` reports route findings; inconclusive/failed runs are recorded
as evidence, matching the review lane's rule that partial output is never
accepted as authoritative. Credential-shaped text is redacted before
persistence and issue publication.

## Delivery seam (Task 1)

Reports arrive as files in `<state_root>/qa/reports/*.json` (configurable via
`qa.report_dir`). Task 1's Lenovo side can drop them there via SSH pull/push,
rsync, or any transport — ingestion is file-based and idempotent. Validated
reports move to `qa/processed/`; rejected ones to `qa/rejected/` with an
`.error.txt` alongside. A repeated `run_id` is a duplicate, not a new report.

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
   original reproduction.
5. Drop a verification report (`passed`) → `verified`; (`failed`) → a linked
   `qa-<key>-fix2` follow-up, bounded by `max_fix_cycles`.

Remaining for a fully live demo: a real worker pass against the sandbox repo
needs CI checks (`rust-*`, `supervisor-tests`) and a dispatcher instance; both
are exercised by the fakes test, and a real-agent run is gated on Task 1's
runner plus deliberate supervisor config (`qa.enabled`/`mode`).
