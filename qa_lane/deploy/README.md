# QA lane deployment (Lenovo)

Generic deployment assets for the hardware/simulation QA lane. The
concrete, sanitized record of what is installed lives in the homelab
repository; this directory is the contract those copies mirror.

## Layout

- `/srv/homelab/dcs-hwtest/` — deployment config: a pinned copy of the
  `qa_lane/` Python module and `config.json`. The lane code is pinned
  separately from the revision under test; upgrading it is a deliberate
  deployment step, not a side effect of a merge.
- `/srv/dcs-hwtest/` — bounded NVMe run storage: `state.db`, `lock`,
  `src/<sha>.tar` + extracted source, `build-cache/`, `cargo-cache/`,
  `runs/<run_id>/` (report.json, evidence/, timeline.jsonl),
  `reports/<run_id>.json` staged for the WSL relay, `repo.git/` the
  bare mirror the relay pushes (fix-ancestry checks for verification
  runs), and `verifications.json` the pending fix-verification queue
  the relay delivers from the supervisor. `build-cache/` also holds
  the bounded builder's `target/` — beside the image binaries it
  carries the host-side `dcs-ctl` the dcs-ctl scenario execs against
  the published monitor ports (`cargo build --release --locked
  -p dcs-monitor --bin dcs-ctl` in the revision under test).

## Install

```sh
sudo mkdir -p /srv/homelab/dcs-hwtest /srv/dcs-hwtest
sudo chown jan-kaspar:jan-kaspar /srv/homelab/dcs-hwtest /srv/dcs-hwtest
# copy qa_lane/ from a chosen commit of the dcs repo
cp -r qa_lane /srv/homelab/dcs-hwtest/
cp config.example.json /srv/homelab/dcs-hwtest/config.json
sudo cp dcs-hwtest.service dcs-hwtest.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dcs-hwtest.timer
```

The service runs oneshot cycles as user `jan-kaspar` (docker group); a
flock in the state directory plus the oneshot unit type guarantee one
active run at a time.

A second unit, `dcs-hwtest-netpolicy.service` (root, oneshot,
`After=docker.service`), installs the host egress firewall policy at
boot:

```sh
sudo cp dcs-hwtest-netpolicy.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dcs-hwtest-netpolicy.service
```

## Operation

- WSL dispatch pushes `src/<sha>.tar` (`git archive` of the resolved
  main revision) and calls `python3 -m qa_lane enqueue <sha>`.
- Each cycle reconciles dead runs and orphaned labeled containers,
  verifies exclusive ownership (fail-closed), reclaims expired
  retention, re-queues one retry for an inconclusive SHA, enforces the
  daily budget, verifies the egress policy, then dispatches any due
  fix verification (`qav-*` run ids) ahead of the newest queued
  revision. The daily budget counts runs by the UTC date they
  actually begin executing (`begin()` stamps `day` at start), so a
  run dispatched at 23:55 and started at 00:30 counts once, on the
  later date.
- A verification run first proves the tested revision contains the
  merged fix — `git merge-base --is-ancestor` inside `git_dir` — then
  replays exactly the finding's original case and records verdict,
  ancestry check, and evidence in the report's `verifications`
  channel (schema v2). An unproven or non-containing revision never
  produces a verdict.
- When `exploration_enabled` is set and nothing else is due, a cycle
  dispatches a `qax-*` exploration run: same build + rig skeleton, then a
  time-bounded Devin session (`exploration_devin`, `exploration_model`,
  `exploration_time_budget_seconds`) gets the rendered
  `qa_lane/explorer_prompt.md` charter prompt with run context (revision,
  endpoints, UI URL, rig manifest, recent merges, backlog snapshot,
  pending verifications, exploration ledger, forbidden actions). The
  agent writes `runs/<id>/results/agent-result.json` +
  `exploration-summary.md` + `evidence/` + `scripts/`; the runner
  validates the document and folds it into a schema-v3 report whose
  scenario entries may carry explicit finding fields. Spacing is
  `exploration_interval_seconds` between starts and
  `max_explorations_per_day`, both outside the assessment's
  `max_runs_per_day` budget. `python3 -m qa_lane ledger` prints the
  exploration ledger. Prerequisite: `devin auth login` on the host for
  the lane user — the CLI runs on the host (loopback rig ports + Devin
  API egress), not in a container.
- `python3 -m qa_lane status` reports queue, active run, block reason,
  cleanup-failure ledger, retention pins, pending verifications,
  storage usage, and history.
- `python3 -m qa_lane preserve run:<id>|sha:<sha> [on|off]` pins or
  unpins evidence the retention reconciler must keep — the
  findings/verification lane uses this so runs tied to unresolved
  findings or queued fix verifications are never reaped.

## Isolation and limits

- Rig containers run on a dedicated labeled bridge per run; the bridge
  interface is named `dcsqar` and the builder bridge `dcsqab` via
  `com.docker.network.bridge.name`. `--internal` is rejected on
  purpose (it would block the loopback-published monitor ports);
  disabled masquerade plus the host firewall policy provide the real
  isolation.
- The egress policy (`qa_lane/netpolicy.py`, installed by
  `dcs-hwtest-netpolicy.service`, verified every cycle): rig bridges
  get no new outbound connections at all — no LAN, no other container
  subnets, no host sockets, no IPv6; only replies to the
  loopback-published monitor ports pass. The builder bridge may open
  TCP 80/443 to non-private destinations only — that is the smallest
  firewall-expressible bound covering crates.io (index.crates.io +
  static.crates.io are Fastly CDN edges with rotating addresses; the
  dependency set is registry-only, verified in Cargo.lock). DNS works
  through Docker's embedded resolver and never enters the forwarding
  path. A missing policy fails the cycle closed with
  `egress-policy-missing`.
- Every run object carries the `dcs-hwtest.managed=1` +
  `dcs-hwtest.run=<id>` labels reconciliation uses to reap orphans;
  cleanup failures are recorded in the durable cleanup ledger, appear
  in every subsequent run report's `infrastructure_failures`, and
  while labeled leftovers survive reconcile the lane refuses new runs
  (`cleanup-incomplete` / `active-run-conflict`).
- CPU, memory, swap, PIDs, and log bounds apply to every rig container
  and to the builder container; free space is checked before each run.
- The docker daemon's RequiresMountsFor/BindsTo dependency on the HDD
  mount is preserved untouched — QA containers inherit it like all
  others on this host.

## Storage bound and retention

The lane's whole footprint — `src/` archives and extractions,
`cargo-cache/`, `build-cache/`, `runs/` evidence, `reports/` staging,
`state.db`, plus lane-owned Docker images (`dcs-hwtest/*`, the builder
image) and Docker build cache — is bounded by
`qa_storage_max_bytes` (default 60 GiB). Enforcement is measured
accounting plus a hard-fail preflight: a run refuses to start
(`blocked`, `preflight-storage-bound`) when the measured footprint
plus `run_headroom_bytes` would exceed the bound. A filesystem quota
was considered and rejected: Docker image and build-cache storage
live in the shared daemon (`/var/lib/docker`) and cannot be covered
by a per-directory quota, so accounting is the only mechanism that
bounds everything the lane creates on a single NVMe.

Every cycle, `reclaim()` removes only QA-owned artifacts: `src/` trees
for SHAs nothing references (beyond `src_keep` recent), terminal run
dirs beyond `runs_keep`, orphan staged reports older than
`reports_orphan_days`, `dcs-hwtest/*` images beyond `images_keep`
recent SHAs, and cargo/build caches over their caps. Global prunes
(`docker system prune`) are never used; preserved pins and
non-terminal runs are untouchable.
