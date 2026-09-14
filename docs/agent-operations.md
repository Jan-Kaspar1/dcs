# Local agent pool operations

Run the commands below in Ubuntu WSL as `kaspar`, except for the Windows startup registration. Authenticate `gh` and the local `devin` CLI before starting. Every managed agent invocation explicitly selects `swe-2-high` with `--permission-mode dangerous`, authorized by the user on 2026-09-14. This gives local Devin full tool access as the WSL user; clones provide work separation, not a security sandbox. Model fallback is rejected.

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

The personal `/home/kaspar/workspace/dcs` checkout is separate from managed clones under `/home/kaspar/workspace/dcs-agent-pool`. Worker clones are allocated as needed. Successful automated merge-and-issue-closure reconciliations raise capacity from five to ten after five merges, and to twenty after fifteen. One planner may run alongside workers. Dependencies and occupied concurrency groups can leave slots idle.

Each agent invocation has a two-hour default limit. CI repair attempts are limited to three. GitHub inventory polling defaults to sixty seconds and errors increase the delay. `python3 scripts/verify.py` shares two heavy-build slots across clones and limits Cargo to four build threads; direct Cargo commands bypass the shared semaphore.

## Recovery and upgrades

Inspect `status`, the invocation receipt, and its output before retrying a failed job. A dirty managed clone is renamed with a `-quarantine-` suffix before replacement; this retains uncommitted files and local branches. Recover useful work by inspecting that clone and transferring a reviewed commit to the appropriate issue branch. Do not delete quarantines or reset a worker checkout merely to clear an error. If a process ownership record is inconsistent, stop the service and resolve the recorded process/workspace state before resuming.

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
