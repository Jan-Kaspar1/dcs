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
# verify the pinned copy's shipped-binary contract against the revision
# the dispatcher will push from (see "Keeping the pinned lane copy at
# the contract"); this fails by name when the copy is behind it
python3 -m qa_lane ship /srv/dcs-hwtest/src/<sha>
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
- `python3 -m qa_lane ship [<extracted-src-dir>]` prints the pinned
  copy's recorded shipped-binary contract and checks it against the
  contract the given revision records — the deploy step's own check
  that the copy is not behind the revisions it will be asked to test.

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
- Bridge-to-host reachability rule (demonstrated by the
  qax-20260922-001, qax-20260922-005, and qax-20260923-001
  exploration runs): no socket bound on the host is reachable from
  the rig bridge — the INPUT half of the egress policy drops every
  rig-sourced packet aimed at the host, covering host loopback, the
  host LAN address, and other stacks' published ports reached via
  the host. Lane endpoint placement follows: an endpoint a rig
  container must dial — a checkpoint interposer or the
  forged-checkpoint server the tracking-source/auth legs announce,
  or a plant-probe listener — runs in a labeled container on the
  run's rig bridge and is dialed by container name; host-side
  scenario attachments reach rig services only through the
  127.0.0.1-published ports. The selection is recorded in the run
  config under `endpoint_placement` and validated before a launch
  trusts it.
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

## Keyed posture: deployed pair and the staged probe pair

The keyed announced-source contract (`--pair-token` /
`line_proof` verification) is exercised per revision by the
tracking-source legs (2050's keyed halves, 2060's keyed
precondition). Two config keys record the posture split:

- `pair_token` is the **deployed** pair's posture: the shared
  tracking secret both deployed controllers and the deployed-pair
  revised/driven peers sign under. Set to `null` to run the
  deployed pair unkeyed — its demote/announce path then answers
  unkeyed, exactly the posture `qax-20260926-002` ran.
- `probe_pair` is the **lane-staged probe pair**, always keyed: a
  second `sim-serve` deployment (`dcs-plant-server` on the rig's
  dedicated bridge — `probe_plant` records placement `bridge`, so
  nothing host-side dials it) with its own declared dynamics
  (`dynamics_fixture`) and its own field — field ownership
  arbitration is per-plant, so the probe pair never shares the
  deployed pair's field — plus two controllers (`probe_active`,
  `probe_standby`) launched with the block's `--pair-token`,
  tracking each other with `line_proof` verification exercised.
  Their monitors publish on host loopback
  (`active_port`/`standby_port`, plus `driven_port` for the probe
  driven peer a keyed island leg may stage); their
  `--owner-token` pins are the distinct `probe_*` entries in
  `plant_owner_tokens` — never the deployed pair's tokens. A
  `probe_pair: null` stages none.

The keyed legs select their subject through the same ctx the
`@require`-style gating consumes: the deployed pair while the run
config keys it, else `ctx['probe']` — so an unkeyed-primary run
still produces real keyed-contract verdicts instead of
capability-based inconclusive results. Probe objects carry the
same managed/run labels and are reaped by the same teardown and
reconciliation as the rest of the rig; a probe launch failure is
an ordinary rig-start failure and leaves no orphaned objects.

## Image build contract

The bounded builder compiles the lane's runtime binaries out of the
revision under test — the controller and plant servers, the
sim-net/sim-bus libraries, the monitor's operator CLI, the plant-side
`dcs-plant-ctl`, the `dcs-forge` checkpoint endpoint, and the
sim-bus `dcs-sim-bus-device` register-protocol server — then
packages two minimal images, `dcs-hwtest/controller:<sha>` and
`dcs-hwtest/plant:<sha>`. Both report the same two digests as
before; only the binaries riding inside them changed:

- `dcs-hwtest/plant` ships `dcs-plant-server` as its entrypoint plus
  `dcs-plant-ctl` beside it. The lane's plant ops `docker exec` that
  tool inside the container against the server's loopback listener,
  so covered operations run through the released binary rather than a
  second implementation of the plant wire protocol (#654).
- `dcs-hwtest/controller` ships `dcs-controller` as its entrypoint
  plus `dcs-forge` and `dcs-sim-bus-device` beside it. Both are
  launched through `--entrypoint` on that one image: the forge is the
  bridge-placed checkpoint endpoint the tracking-source/auth legs
  announce, and the device server is the bridge-placed sim-bus field
  the sim-bus rig legs stage (#1368). A separate bus image would
  change the reported digest set, so the existing image carries it.
- `dcs-ctl` is not in either image: it stays a host-side binary in
  `build-cache/target/release/`, exec'd against the pair's published
  monitor ports.

`qa_lane/ship.json` is where that payload is *recorded*: the compile
groups the bounded builder runs in `/src` with the binaries each one
produces, the two images with their entrypoints and the binaries beside
them, and the host-side tools. `_build_images` derives its cargo chain,
each image's payload, and its assertions from that document, so the two
lists cannot drift apart — and because the document is data it also
ships inside the revision's own `git archive`, which is what makes the
pinned-copy check below possible.

Three things fail the run by name instead of leaving a leg to discover a
phantom tool:

- A contract naming a binary no compile group produces — the ship-map
  gap — fails before the compile: `no recorded compile target produces …`.
- A compile that produces no one of an image's binaries fails the run:
  `build produced no dcs-sim-bus-device`.
- A staged context that does not carry every binary its image's record
  names — read back from the generated context directory and Dockerfile,
  not from the copy loop that wrote them — fails the run:
  `controller image stages no dcs-forge`. The staged payload is
  recorded on the run's timeline as `image-staged`, beside the digests
  the report persists.

### Keeping the pinned lane copy at the contract

The pinned `qa_lane/` copy and the revision under test advance
separately, which is how the recorded ship list failed to reach the
runs' images at cabe3b3: three exploration runs tested a revision
carrying both #1368's sim-bus ship list and #654's `dcs-plant-ctl`
precedent, but the deployed copy that built their images predated
both, so the images carried their entrypoints alone and the sim-bus,
keyed-interposer, and claim-probing legs fell back to bind-mounting
`build-cache/target/release/` binaries. Nothing in the report said so —
only the two digests were recorded.

Two checks now carry that knowledge, both naming the binary:

- **At deploy time:** `python3 -m qa_lane ship [<extracted-src-dir>]`
  prints the deployed copy's recorded payload and compares it against
  the contract an extracted revision records at `qa_lane/ship.json`.
  Run it after copying `qa_lane/` from the commit the dispatcher pushes
  revisions from; a copy predating that revision's ship list exits with
  `the deployed qa_lane copy predates the shipped-binary contract the
  revision under test records: …`.
- **In every run:** the image build makes the same comparison against
  the run's own extracted source tree, so an assessment,
  exploration, or fix-verification run refuses before it compiles
  anything rather than staging entrypoint-only images.

A revision predating `ship.json` records none and is left alone. Upgrade
the pinned copy whenever a merge changes the recorded payload; a run
whose revision carries no contract cannot catch a pin that is behind.

## The lane's sim-bus device server

`sim_bus_device` names the sim-bus rig's field: the `device` id the
server serves out of the model, the bridge `port` its register
protocol binds, and the `model_fixture` declaring that device. A leg
launches it through the run context's `start_sim_bus_device`, which
stages the fixture inside the run's directory with that device's
`address` parameter — the `__BUS_ADDR__` placeholder the checked-in
bus models carry — bound to the device container's rig-bridge name,
mounts the staged document into a controller-image container run
under `--entrypoint dcs-sim-bus-device`, and waits for the server's
own report of the address it serves on. The launch returns the bridge
address the rig's sim-bus attachments dial and the staged document a
leg mounts into the controller it points at the field, so both ends
of the register protocol read one declaration. `start_sim_bus_device`
also accepts `timeout_ms`, stamped onto the staged document's device
parameters — a leg staging a field stall needs the driver's declared
per-request timeout to sit under the outage, where the fixtures'
five-second default would not.
`stop_sim_bus_device` removes the container outright, for a leg's
device-outage induction. `restart_sim_bus_device` severs every
attachment's control connection while the same server comes back —
the register bank and the connection-bound claim reset with the fresh
process — and `freeze_sim_bus_device`/`thaw_sim_bus_device` hold the
attachments' sockets open and unanswered for a bounded stall instead,
the field's state surviving. `sim_bus_device_serving` reports whether
the server is answering right now, so a leg separates a field that
never came back from a driver that never re-attached. The container
carries the run's
`dcs-hwtest.managed=1` / `dcs-hwtest.run=<id>` labels, so ordinary
teardown and reconciliation reap a leg that leaves one running.

The endpoint is bridge-placed: `endpoint_placement` must record
`sim_bus_device` as `bridge`, and nothing host-side dials the
register protocol — a host socket is unreachable from the rig bridge.
Add both keys to the deployed `config.json` when the pinned lane copy
is upgraded; `sim_bus_device: null` stages no device server, and the
placement key is required, so a config predating this contract
refuses the run rather than launching an unreachable endpoint.

Normal-path staging runs the released device server; a fault-injection
leg that needs the server itself to misbehave can still stage a
protocol double of its own — but the register-protocol evidence runs
against the shipped binary.

### Attaching a controller to the bus field

The born-seat launcher takes a document-addressed launch alongside its
`sim-tcp` shape: `start_born_controller(seat, remote, peer=,
standby=, document=)` mounts the caller's `document` in place of the
run's own model, and a `remote` of None runs with no `--remote` flag
at all. That is how a leg points a seat at the device server: mount
the staged document `start_sim_bus_device` returned and the
attachment dials the device's declared `address` straight out of the
model, so the server's register map and the attachment's dialed
endpoint come out of one file. A launch carrying neither a `remote`
nor a `document` is refused, as is one whose document is not a file.
Everything else about the launch is the rig's born-seat shape
unchanged — the seat's pinned `--owner-token`, the published monitor,
the cold state/journal/history reset, the `--peer`/`--standby`
wiring, the refuse-to-replace guard — and the return value's `model`
names the document actually mounted beside `remote: None`, so a leg
can evidence that both ends read the one declaration it staged.

The launcher also takes a per-container cadence: `start_born_controller
(seat, remote, peer=, standby=, document=, scan_ms=)` sets that
container's own `--scan-ms`, and the return value's `scan_ms` names the
pace it actually runs at. A run's tick accrues one per scan, so a seat
launched at 25 ms accrues four ticks for every one a 100 ms-paced peer
does — the rig's own clock skew, staged on the rig instead of injected,
which the claim-skew leg (`2490`) reads back off both seats' served
`/role` ticks before a claimant computes its claim. Omitting `scan_ms`
keeps the documented `BORN_SCAN_MS` (100) pace, and a non-positive or
non-integer cadence is refused by name rather than silently paced at
something else.

Two sim-bus legs stage through it. The sim-cyclic fencing-loss leg
(`2470`) launches a pair on the staged `sim-cyclic` document and
asserts the fenced-exchange demotion; the sim-bus startup-claim-
refusal leg (`2480`) is the other: a first controller takes the
device's write-ownership claim, a second born-active declaring no
`--peer` must exit nonzero naming the live-holder refusal rather than
preempting the incumbent, and a `--standby` launch in the same shape
converges behind it. A rig whose `sim_bus_device` is null, whose
monitor ports carry no born seats, or whose `plant_owner_tokens` pin
nothing for that leg's incumbent seat, reports it inconclusive rather
than staging against an endpoint it was never granted or auditing a
claimant it cannot attribute. The leg sweeps its three seats and the
device server at the end of every pass and reads the rig back
afterwards — a seat's presence through `born_controller_state`, the
device server's own removal error, the deployed pair framed once more
— so a leftover claim surfaces as a failed leg instead of an inherited
one.

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
