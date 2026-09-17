# Local agent pool operations

Run the commands below in Ubuntu WSL as `kaspar`, except for the Windows startup registration. Authenticate `gh` and the local `devin` CLI before starting. Every managed agent invocation runs with full tool access — `devin -p --permission-mode dangerous` for `swe-2-*` models, `opencode run --auto` for `opencode/*` models — authorized by the user on 2026-09-14 and extended to OpenCode free models on 2026-09-17. This gives agents full tool access as the WSL user; clones provide work separation, not a security sandbox. The `models` config list is validated against a verified free-model allow-list; paid fallback is rejected. Worker clones rotate through `models` by slot so parallel jobs spread across separate model quotas; the planner and review lane use the first configured model. OpenCode invocations are not resumable through the Devin session id, so retries carry repair context in the prompt instead.

## Installation and startup

From a reviewed, clean, committed DCS checkout:

```sh
python3 scripts/verify.py
python3 scripts/install_agents.py
~/.local/bin/dcs-agents start
~/.local/bin/dcs-agents status
```

Installation copies a pinned revision beneath `~/.local/share/dcs-agents/releases/`, creates the launcher and systemd user service, and preserves an existing active configuration. The default configuration is `~/.config/dcs-agents/config.json`; state, invocation prompts, receipts, and logs are under `~/.local/share/dcs-agents/state/`. Keep these files and credentials outside Git. A successful install alone does not demonstrate a successful autonomous issue cycle.

From Windows PowerShell, register the login task using the script in the same checkout:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "\\wsl.localhost\Ubuntu-26.04\home\kaspar\workspace\dcs-bootstrap\scripts\install_windows_startup.ps1" -Distro Ubuntu-26.04
```

This registers `DCS Local Devin Agents` for the current user's next login, replacing a task with that name. Its WSL foreground process starts and waits for the service. Registration is not proof of login recovery: verify the task after a real subsequent login. Windows sleep or shutdown interrupts execution; process receipts and repository work remain available for reconciliation afterward.

## Daily controls

```sh
dcs-agents status
dcs-agents logs
journalctl --user -u dcs-agents.service -n 100
dcs-agents pause
dcs-agents resume
dcs-agents stop
dcs-agents start
dcs-agents retry 123
```

`status` reports reservations, capacity, merge count, planner metadata, pause reason, and errors. `logs` prints recent supervisor events; per-invocation `output.log` files contain agent output. Treat logs as private because they may include task contents.

`pause` prevents new dispatch and merging while active processes may finish and publish their results. `resume` reopens dispatch unless an integrity failure remains. `stop` terminates managed processes through the service and preserves their checkouts. `retry` applies only to a blocked issue after the pool is resumed; inspect its error and workspace first. A retry is not evidence that the underlying blocker has been fixed.

The personal `/home/kaspar/workspace/dcs` checkout is separate from managed clones under `/home/kaspar/workspace/dcs-agent-pool`. Worker clones are allocated as needed. Successful automated merge-and-issue-closure reconciliations raise capacity from five to ten after five merges, and to twenty after fifteen. One planner may run alongside workers. Only dependencies and worker-slot capacity can leave slots idle; concurrency groups are descriptive metadata and no longer serialize dispatch, so same-area tickets run in parallel and overlapping edits are resolved through serialized merges and conflict repairs.

The planner reads `docs/product-strategy.md`, the applicable `docs/requirements/` files, and cited notes under `docs/research/` before proposing work. Product issue scopes begin with stable requirement IDs. When customer-specific semantics or measurable acceptance criteria lack evidence, the planner creates a `docs/research` issue first. The assigned worker then runs as the research role: it updates a cited research note and requirement status, and implementation waits for a later planning pass. This separates evidence gathering from the planner's backlog and dependency decisions without requiring a permanently running research session.

The ordinary planner runs at least every two hours and checks for low work after fifteen minutes. Low work means fewer than six dependency-ready tasks; tickets carrying `agent:ready` behind open prerequisites do not suppress planning. A proposal may depend on another item in the same proposal by its stable key. The supervisor validates that DAG, creates its issues in dependency order, and persists only resolved GitHub issue numbers. This lets one pass publish a contract ticket plus its later parallel fan-out without inventing issue numbers or waiting for another two-hour cycle.

Each agent invocation has a two-hour default limit. CI repair attempts are limited to three. GitHub inventory polling defaults to sixty seconds and errors increase the delay. `python3 scripts/verify.py` shares four heavy-build slots across clones and limits Cargo to four build threads; direct Cargo commands bypass the shared semaphore.

## Architecture review lane

The supervisor can run a daily architecture reviewer: a read-and-report invocation pinned to the resolved `main` SHA that proposes deep-module and naming-consistency improvements. It does not refactor `main` itself — accepted candidates become ordinary managed issues that workers implement through the existing CI and merge gates.

Configure it in `~/.config/dcs-agents/config.json` under `review`:

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Master switch. Disabling stops a running reviewer invocation; state, reports, and already-accepted implementation work are preserved. |
| `mode` | `report` | `report` keeps reports and candidates internal; `pilot` feeds candidates to the planner until one accepted improvement passes CI and post-merge assessment, which promotes the lane to `full` daily operation. |
| `auto_promote` | `true` | In `report` mode, promote to `pilot` automatically after the first completed report. A rejected assessment rolls the lane back to `report` and records the failure; re-enable deliberately by setting `mode` to `full`. |
| `time` / `timezone` | `03:00` / `Europe/Berlin` | Daily schedule. A missed run executes once on resume; no backlog accumulates. |
| `timeout_seconds` | `3600` | Per-review invocation limit. |
| `max_candidates` | `3` | Report candidate cap. |
| `retention_days` | `30` | Report file retention under `state/reviews/`; dedup records outlive logs. |

Controls: `dcs-agents review` prints lane status (stage, running run, latest result, pending candidates and assessments); `dcs-agents review run` queues a manual run for the next cycle. The reviewer occupies one agent slot, so a full pool defers it; `pause` also defers scheduled and manual runs. One automatic retry follows a failed scheduled review. Unchanged `main` is skipped unless new blocked-job evidence or a manual run exists, and only one architecture improvement is active at a time across its dependent issues.

Reviewer guidance is pinned in `agent_pool/resources/architecture/` (project policy, prompt template, MIT-licensed vendored references with a hash manifest). Preflight verifies every resource hash of the *installed* release before launch and stages a copy beside the report; the reviewed checkout's copies are never used, so a main commit cannot change live reviewer behavior without a pinned upgrade. A mismatch fails the run explicitly. Regenerate `MANIFEST.json` after changing any resource: `python3 -c "from agent_pool.review import write_manifest; write_manifest('agent_pool/resources/architecture', '<upstream revision>')"` — a test fails when the manifest is stale.

Rollout: install the pinned upgrade, keep `enabled` at `false` to stage, then set `enabled`/`mode` per the table. `mode: report` with `auto_promote` exercises the staged path — one validated report promotes to `pilot` (planner ingestion for at most one active improvement), and a confirmed post-merge assessment of that pilot improvement promotes to `full` — without a manual report-inspection gate. A rejected assessment rolls back to `report`.

## QA findings lane

The supervisor ingests Lenovo QA run reports from `<state_root>/qa/reports/` and is the sole GitHub publisher for QA findings: reproduced defects become managed issues (severity maps to P1-P3, never P0; the affected module is the concurrency group), missing capabilities become planner candidates, and rig/build/credential/agent failures stay operational records. Merged fixes chain finding -> issue -> fix SHA -> verification; a failed fix yields a linked follow-up issue bounded by `max_fix_cycles`, never a bare reopen. See `docs/qa-findings-ingestion.md` for the report contract, lifecycle, and seeded-defect demo.

| Key | Default | Meaning |
| --- | --- | --- |
| `qa.enabled` | `false` | Ingest QA reports each cycle. |
| `qa.mode` | `record` | `record` validates/records only; `route` publishes issues, candidates, and follow-ups. |
| `qa.dashboard` | `false` | Publish `qa/findings.json` to the Pi dashboard (content-hashed, tolerant of outages). Keep off until the Pi view exists. |
| `qa.report_dir` | `<state_root>/qa/reports` | Report inbox; processed and rejected files move beside it. |
| `qa.max_open` / `qa.max_issues_per_report` / `qa.max_fix_cycles` | `20` / `5` / `2` | Open-finding bound, per-pass issue cap, and fix-attempt bound. |

`dcs-agents status` reports `qa` lane counts, pending verifications, and the last publication error. The lane is fail-safe: a malformed report is quarantined to `qa/rejected/` and never wedges the dispatch loop.

## Recovery and upgrades

Inspect `status`, the invocation receipt, and its output before retrying a failed job. When a job blocks, the supervisor records a recovery state (branch, original checkout, last known commit, and whether preserved work exists) and commits dirty work-in-progress onto the issue branch while its checkout is still assigned. `retry` then recovers under an exclusive clone lease: an idle original checkout is switched back to the job branch; a busy one is bypassed by fetching the preserved ref into a free worker clone; and a fresh branch from current main is created only after every checkout, quarantine, and the remote prove no work exists. Recovery failures retain the recorded state and report a specific error rather than implying no work exists. A dirty managed clone is renamed with a `-quarantine-` suffix before replacement; recovery also surveys quarantines for preserved refs. Do not delete quarantines or reset a worker checkout merely to clear an error. If a process ownership record is inconsistent, stop the service and resolve the recorded process/workspace state before resuming.

Upgrade only from a clean, committed checkout after checking its diff and running the full verification command:

```sh
dcs-agents stop
dcs-agents upgrade /path/to/reviewed/dcs-checkout
dcs-agents start
dcs-agents status
```

Ordinary merges do not switch the installed supervisor release. The explicit upgrade changes the installation's `current` pointer; prior releases remain available for investigation. Avoid reinstalling while the service is active.

## Merge policy and acceptance evidence

The supervisor serializes squash merges and requires the named `rust-format`, `rust-clippy`, `rust-tests`, and `supervisor-tests` checks for the PR revision. Branch updates require fresh checks. A job is completed only after GitHub confirms both PR merge and linked issue closure.

This is supervisor policy, not server-side branch protection. If GitHub branch protection is unavailable for the repository's plan, an unrelated direct push or manual merge can bypass it. Workers operate on software and simulated I/O only; physical equipment access and live deployment are outside the autonomous loop.

Before calling rollout complete, record a real issue-to-PR-to-CI-to-merge-to-closure cycle, repeated worker assignment, process-restart reconciliation, and a real Windows login recovery check. Passing unit tests or registering a service/task establishes only those individual checks, not end-to-end acceptance.
# Git ownership and permissions

Workers edit and test files; the supervisor stages and commits completed edits before publishing. This ownership remains explicit even with full Devin tool access. Permission rejections, including those accompanied by a zero exit status, block the issue and preserve its workspace. Initial smart-mode restrictions were replaced with user-authorized full tool access after live validation.
