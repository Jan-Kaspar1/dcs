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

### Landed 2026-09-26 (keyed probe pair on its own field, #1058)

- The keyed announced-source contract needs per-revision exercise
  even where the run config deploys its redundant pair unkeyed —
  `qax-20260926-002` evidenced the gap: 2050's keyed halves and
  2060's keyed precondition kept reporting inconclusive on absent
  capability rather than on the contract, and the earlier
  `keyed-island-replay-not-run` deferral parked on absent staging.
  The runner now stages a second, always-keyed redundant pair per
  rig: its own sim-serve plant — `probe_pair`'s own
  `dcs-plant-server` deployment on bridge-placed endpoints with
  its own declared dynamics (`qa_lane/fixtures/
  probe_dynamics.json`), so the probe pair owns a dedicated field
  and never touches the deployed pair's plant or claim tokens —
  plus two controllers sharing the block's `--pair-token`
  (`dcs-qa-pair`), tracking each other with `line_proof`
  verification on, published monitor ports on host loopback, and
  distinct `probe_*` owner-token pins.
- The keyed legs select their subject through the same ctx shape
  the `@require`-style gating consumes: `ctx['probe']` re-points
  every endpoint key, runner action, owner token, and
  state/journal path at the probe pair, and `common._keyed_subject`
  returns the deployed ctx while `pair_token` keys it, else the
  probe subject — so an unkeyed-primary run still reports real
  keyed-contract verdicts instead of capability skips, and the
  parked keyed-island replay can stage. The posture split is
  recorded in the deploy config shape (`pair_token` vs
  `probe_pair.pair_token`, `probe_*` entries in
  `plant_owner_tokens` and `endpoint_placement`) and documented in
  `qa_lane/deploy/README.md`; a `probe_pair: null` stages none
  and restores the absent-capability inconclusive.

### Landed 2026-09-27 (stranded-standby re-join leg, #1059)

- The stranded-standby re-join contract — decision 101 as #1042 and
  #1045 implemented it, in service of WW-LCM-001 — is exercised on
  the deployed pair by scenario leg
  `2370_stranded_standby_no_resync` against the live reproduction of
  finding `stranded-standby-no-resync`. With the pair settled the
  leg issues `POST /promote` to the tracking standby while the field
  owner is still alive — the involuntary-demote entry the finding
  drives, no `POST /demote` first — and the promoted peer preempts
  the field claim. The ex-owner is fenced, demotes in place, and
  must re-join `tracking` inside the lane's tick bound rather than
  wedge `unsynchronized`: on a keyed rig through the verified
  announced or claimed-monitor source, on an unkeyed rig through the
  claim's declared monitor where the declaration is routable. The
  leg journals the contract's receipts — `field_claim_lost`
  attributed to the promoted successor, the fencing verdict naming
  the successor's declared monitor, and `tracking_source_adopted`
  from the peer that owes it — and a second promote cycle in the
  opposite direction proves the pair stays promotable both ways
  before the launch layout is restored.
- The #1045 monitor-less-foreign-claim window is staged where the
  plant tool admits it: a held tool claim declaring no monitor
  leaves the demoted peer `unsynchronized` only until a
  controller-owned claim re-seats the field, and a raw `/step`
  fencing probe confirms no improper source adoption while the
  foreign claim stands. Named diagnostics are
  `stranded-standby-no-resync-failed` and
  `stranded-standby-no-resync-nondeterministic`; two passes produce
  identical digests; a rig that is unreachable, that predates the
  serve/plant seams the leg needs, or whose field census shows an
  open claim reports inconclusive.

### Landed 2026-09-27 (keyed announced-source lifecycle residual leg, #1074)

- The accepted finding `keyed-announced-source-untested`'s mapped
  issue had never been emitted — scenario 2050's keyed halves cover
  the forged receipt-window and planted-internal refusals and the
  honest keyed adoption, but four announced-source lifecycle classes
  stayed unexercised. Scenario leg `2150_keyed_announced_source`
  stages them on the lane-staged keyed probe pair's own simulated
  plant and endpoints — never the deployed pair, keyed or not: the
  misordered `POST /promote` preempts the probe field's claim, and
  the superseded owner — the peer with no configured source —
  demotes in place, journals the attributed `field_claim_lost`,
  resolves a verified source under keying, journals one
  `tracking_source_adopted` naming the promoted peer's monitor, and
  re-joins `tracking` inside the lane's tick bound. With the
  announced-only window open (the tracking peer stopped, the owner
  warm-restarted — the persisted checkpoint generation survives, so
  a captured document still claims this line), `POST /demote`
  refuses `no_tracking_source` for the keyed endpoint serving the
  receipt-window-forked document and for the replayed checkpoint —
  the keyed peer's own `?prove=` answer captured under the leg's
  nonce and staged verbatim on a tokenless endpoint, so only the
  proof's rolled nonce window can convict it. The keyed endpoint
  serving the honest standby-shaped document then completes the
  lifecycle — announce, signed verify pull, journaled adoption,
  standing pulls converging the demoted peer — and the adopted
  endpoint's hostile restage, a signed document planting an
  internal `In` sample, convicts on the pull path `degraded`
  without the planted value landing before the honest restage
  restores convergence and the peer re-promotes. Named diagnostics
  are `keyed-announced-source-failed`,
  `keyed-announced-source-nondeterministic`, and the self-check's
  `keyed-announced-source-unchecked`; two passes produce identical
  digests, and a run with no keyed probe pair staged reports
  inconclusive rather than touching the deployed pair.

### Landed 2026-09-27 (bounded liveness-report leg, #1160)

- The bounded liveness contract — #991's `GET /health` answer feeding
  the container `HEALTHCHECK`, and the #1147 fix keeping `/health`
  and `/role` on the liveness mirror while a scan wedged in field
  I/O holds the executor lock — is exercised on the deployed pair by
  scenario leg `3675_bounded_liveness` against WW-OPS-003's
  field-confidence clause. The runner's new
  `pause_plant`/`unpause_plant` actions — `docker pause` on the
  run's plant container, handed to the scenario ctx beside the
  stop/start levers — stage the wedge the liveness regression
  names: the remote driver's socket stays open but unanswered, so
  each peer's in-flight scan parks inside its field timeout with no
  fencing reconnect and no role move. Through the ~4 s hold both
  peers' `/health` and `/role` must answer inside the declared 1 s
  bound while `/health`'s `last_scan_age_ms` grows past the wedge
  floor, and the published-copy reads `/snapshot` and `/journal`
  stay at baseline latency; the unpause must re-stamp the scan age
  to cadence freshness with the pair's launch roles restored.
- Named diagnostics are `bounded-liveness-failed` and
  `bounded-liveness-nondeterministic`, with the self-check's
  `bounded-liveness-unchecked`; two passes produce identical
  digests; a run whose monitors predate the `/health` contract,
  whose pair never settles, or whose pause lever cannot land
  reports inconclusive.

### Landed 2026-09-27 (aborted bounded-command honest-verdict leg, #1191)

- The aborted bounded-command honest-verdict contract — #1036's fix
  of the WW-LCM-001 receipt-as-truth clause and the bounded
  command-admission contract — is exercised on the deployed pair by
  scenario leg `3360_command_abort_verdict`. The leg mirrors the
  unit reproduction's transport stand-in on the real rig: the field
  owner's single command worker pins on a stalled head — a
  `POST /command` whose declared body arrives half-sent — so a
  labeled writable-point submission lands buffered in the lane
  while its client-side bound ends the wait unanswered, exactly the
  `AbortSignal.timeout` abandon the page's `submitCommand` mints the
  indeterminate verdict for. The mirrored post-facing report must
  answer the honest "outcome unknown" — never `command failed` for
  a submission whose fate stayed open — while the served
  `/receipts`, the served `/journal`, and each peer's durable
  `--journal-file` carry the admission's true terminal verdict
  (applied or the named refusal), exactly one `command_settled`
  stands for the admission across both peers, and the pair's launch
  roles are restored.
- Named diagnostics are `command-abort-verdict-failed` and
  `command-abort-verdict-nondeterministic`, with the self-check's
  `command-abort-verdict-unchecked`; two consecutive passes produce
  identical digests; a run whose served page predates the
  honest-verdict surface, whose pair never settles, or whose ctx
  carries no journal-file paths reports inconclusive — and an
  abort window that never staged (the pinned lane answering inside
  the tightened bound) likewise reports inconclusive rather than a
  verdict.

### Landed 2026-09-27 (malformed-dynamics admission-refusal leg, #1193)

- The malformed-dynamics named-refusal contract — the #957 fix's
  refuse-rather-than-panic rule and the #958 field-ownership
  boundary — is exercised on the deployed rig by scenario leg
  `3680_dynamics_admission_refusal`. The runner's new
  `admit_dynamics` action — the harness's per-run variant seam for a
  doctored dynamics document — stages a document inside the bounded
  run dir and drives the run's plant image through both admission
  gates in labeled scratch containers: the released
  `--check-dynamics` preflight exiting with the merge verdict, and a
  detached `--dynamics` serving load polled across a bind grace
  where still-running is the accepted verdict. The leg stages each
  recorded malformed class — a self-point `bool_flow`/`threshold`
  document (the shape that passed schema and `--check-dynamics`,
  then panicked the first step and poisoned the state mutex) and a
  document whose threshold elements drive the controller-owned bool
  Out points (the every-step command stomp) — bound off the live
  field census so the refusal names real points, behind an honest
  control element that proves 'accepted' is observable first. Each
  class must meet `dynamics element <i> (driving point <p>) is
  invalid:` lines, a nonzero exit, and no `listening on` bind at
  both gates — never acceptance, an unnamed refusal, or a panic
  unwind — while the serving plant's census, field tick, and
  shared-claim probes keep answering (no poisoned mutex) and every
  bound Out point's stored value stays the field owner's commanded
  value.
- Named diagnostics are `dynamics-admission-failed` and
  `dynamics-admission-nondeterministic`, with the self-check's
  `dynamics-admission-unchecked`; two passes produce identical
  digests; a run whose run context carries no `admit_dynamics`
  lever, whose plant tooling predates `--check-dynamics`, whose
  census binds none of the point shapes the classes need, or whose
  pair never settles reports inconclusive; the pair leaves on its
  launch roles.

### Landed 2026-09-27 (failover-refusal journal leg, #1146/#1158)

- The fired-but-refused automatic-promotion durable-trail
  contract — WW-FND-004's named-evidence journal clause and
  WW-LCM-001's availability audit — is exercised on the deployed
  rig by scenario leg `2280_failover_refusal_journal`: with the
  launched pair settled, the leg promotes the armed standby over
  the field (the incumbent's fencing-loss demotes it in place),
  stops the launched owner's container, and demotes the armed peer
  onto its configured — now dead — tracking source, so the
  produced-nothing misses reach the declared budget with the
  convergence proof already voided. The served role must read
  standby through the climb and the hold past it, the durable
  `--journal-file` and the served `/journal` tail must each hold
  exactly one `promotion_refused` entry naming the `not_converged`
  cause and the fired miss count — the row that distinguishes a
  refused fire from a never-armed peer — and no ownership
  transition may journal beside it. The pass's second half then
  proves the refusal did not latch the gate closed: the restarted
  owner re-claims the field and re-stands the peer's proof, the
  owner is stopped again, and the armed-and-eligible fire at the
  same budget must promote the peer — served role and journaled
  transition — beside the still-single refusal row; a parked gate,
  a fire below the named boundary, an unjournaled promotion, or a
  relapsing second refusal row are each named contract misses.
- Named diagnostics are `failover-refusal-journal-failed` and
  `failover-refusal-journal-nondeterministic`, with the
  self-check's `failover-refusal-journal-unchecked`; two passes
  produce identical digests; a run carrying one endpoint, no
  controller stop/start actions, no armed failover evidence, or no
  standby journal file — or a pair that never settles its launch
  layout — reports inconclusive; the pair leaves on its launch
  roles.

### Landed 2026-09-28 (quiesced-standby settle leg, #1195)

- The quiesced-standby no-phantom-settle contract — the #689 fix
  serving WW-LCM-001's receipt-as-truth clause and the write gate's
  quiescence rule — is exercised on the deployed rig by scenario
  leg `1750_quiesced_standby_settle`. A tracking standby's quiesced
  scan must never settle an adopted pending command on its gated
  image: the defect applied the adopted receipt on the staged
  image, minted a phantom settled verdict that journaled before
  any real boundary, and was never re-executed on the live line.
  Staging a standby that holds an adopted still-pending receipt
  takes the run's driven third controller — a paced standby's
  pending window is one apply boundary wide and unobservable, so
  the leg submits a receipted writable-point command on the field
  owner just past its scan boundary and drives one scan on the
  driven peer so its checkpoint pull lands inside the pending
  window. While the carried receipt stands `accepted` the audit
  reads the quiesced standby's serving monitor and durable
  `--journal-file`: no terminal verdict in `/receipts`, no
  `command_settled` in either journal, the gated image unchanged.
  The leg then promotes the standby and asserts the carried
  command resolves exactly once at the true boundary on every
  peer — the adopted record carrying the line's applied verdict
  and apply tick verbatim, never a fresh local mint — and restores
  the launch roles.
- Named diagnostics are `quiesced-settle-failed` and
  `quiesced-settle-nondeterministic`, with the self-check's
  `quiesced-settle-unchecked` covering the planted phantom settle
  and the planted missing boundary resolution; two consecutive
  passes produce identical digests; a run whose served surfaces
  predate the receipt-attribution and checkpoint-window contract,
  whose ctx carries no driven-controller seam or journal files,
  whose pair never settles tracking, or whose staging never lands
  a pull inside the pending window reports inconclusive; the pair
  leaves on its launch roles.

### Landed 2026-09-30 (persistence-path alias-refusal leg, #1295)

- The persistence-path distinctness startup contract — #1292's fix
  serving WW-LCM-001's continuity clause — is exercised on the
  deployed rig by scenario leg
  `3695_persistence_path_alias_refusal`. `--state-file`,
  `--journal-file`, and `--history-file` are distinct-file
  declarations with distinct formats — a write-then-rename
  checkpoint beside two append-only record streams — and an
  aliased pair must fail startup by name rather than divert the
  append stream onto the orphaned inode the observed run produced
  (a healthy /history/durable writing nowhere reachable, then a
  crash-loop on restart reading the checkpoint document as a
  history record). The leg stages each recorded pair through the
  run context's `admit_persistence` lever — the harness's
  per-run variant seam for a doctored launch: the run's own
  controller image in a labeled `--rm` networkless scratch
  container mounting a per-probe run-dir directory, launched on
  the rig's pacing shape bounded by `--ticks`. The
  `--state-file`/`--history-file` and `--state-file`/`--journal-file`
  aliases must each exit nonzero naming both conflicting flags
  and the shared path; the `--journal-file`/`--history-file`
  append-append alias is the standing contrast the finding
  recorded — every build fails it closed, the named parse refusal
  replacing the second sink's writer-lock conflict once the
  contract lands; and the correctly-distinct launch of the same
  shape must scan its ticks and write each sink's own file. The
  deployed pair never moves — the scratch containers share no
  file, port, or field claim with the running members — and no
  member launch is rebuilt, so the launch configuration stands
  untouched throughout.
- Named diagnostics are `persistence-alias-accepted` and
  `persistence-alias-nondeterministic`, with the self-check's
  `persistence-alias-unchecked` covering the planted accepted
  aliases, unnamed refusals, refused control, and disturbed pair;
  two consecutive passes produce identical digests; a run whose
  ctx carries no `admit_persistence` lever, whose pair is
  unreachable or never settles tracking, or whose staged revision
  predates the contract — the append-append alias still failing
  closed through the writer lock alone — reports inconclusive;
  the pair leaves on its launch roles.

### Landed 2026-10-01 (monitor-less foreign-claim release leg, #1224/#1167)

- The amended intended-`unsynchronized` bound #1167 recorded is
  exercised on the deployed rig by scenario leg
  `2385_foreign_claim_release` — the per-revision lane evidence for the
  scripted reproduction `field_attested_source.rs`'s
  `unkeyed_fenced_demote_reclaims_the_released_field` drives, and the
  one bound the adjacent legs cannot pin: 2350's claim-reclaim proves
  the released-preemption re-seat, 2380's claim-monitor rendezvous the
  dialable declared monitor and its journaled adoption, 2370's
  stranded-standby the same-claim-monitor succession — none asserts
  that the served un-converged reading covers exactly the monitor-less
  claim's window and no more.
- With the pair settled one active plus one tracking standby on the
  unkeyed posture, a dedicated plant-socket attachment issues
  `claim_writer` under a foreign token with `controller: false` and no
  monitor declared — the monitor-less tool claim the shipped
  `dcs-plant-ctl` does not expose — and holds it. The fenced ex-owner's
  first write must demote it in place, its serving monitor answering
  every poll rather than dying; the served journal and the peer's
  durable `--journal-file` must carry exactly one `field_claim_lost`
  attributed to the induction token; and the plant's non-mutating
  claim surface must keep naming that token with no monitor declared.
- While the monitor-less claim stands the fenced ex-owner must report
  `standby` and un-converged on every poll of the claim's standing
  window — `unsynchronized`, or the orphan-tracked cousin `orphaned`
  for an ex-owner already carrying a verified tracking pin its own
  journal records — never the clean `tracking` verdict. That window is
  the claim's own: releasing it must resolve the field through one of
  the two recorded paths, the ex-owner's loss-marked bound conditional
  reclaim re-arming its token and walking back `standby → promoting →
  active` under the `reclaim` origin with the sibling tracking, or a
  successor's declared monitor adopted with the journaled
  `tracking_source_adopted`. The resolved claim must name something to
  track, the field's writes must land again (the watch point's tick
  advancing past the release-time anchor), the durable journal must
  open no new `run_boundary`, and the pair must return to its launch
  roles. A peer still un-converged past the release bound is the
  permanent strand the bound forbids; a peer reporting clean while the
  monitor-less claim stands is the same verdict asserted past its
  window.
- Named diagnostics are `foreign-claim-release-failed` and
  `foreign-claim-release-nondeterministic`, with the self-check's
  `foreign-claim-release-unchecked` covering every planted clause —
  the peer asserted resolved while stranded past the release, the
  only-while-claimed verdict asserted while the served monitor reports
  clean, the orphan-tracked reading with no learned pin behind it, the
  silent or misattributed loss, the unjournaled re-seat, the durable
  mirror going silent, the released field naming nothing to track; two
  consecutive passes produce identical digests; a run whose claim
  surface, declared monitor, claim-staging lever, claim-owner pins, or
  journal files are absent, whose pair never settles or is unreachable,
  or whose pair runs keyed reports inconclusive — the bound names the
  unkeyed posture, where the announced verify hands a keyed demote
  `orphaned` instead.

### Landed 2026-10-01 (sim-bus device server in the lane image set, #1368)

- The sim-bus rig legs stage against the released protocol server: the
  bounded builder adds `-p dcs-sim-bus --bin dcs-sim-bus-device` to
  its compile set and the ship map carries that binary into the
  controller image beside `dcs-controller`, so a leg launches the
  real register-protocol server out of the revision under test —
  the `dcs-plant-ctl` precedent #654 recorded for the plant image,
  and the `dcs-forge --entrypoint` precedent on the same image —
  instead of a disposable in-session server or a Python
  reimplementation of the wire protocol. The two reported digests,
  the controller entrypoint, and the host-side `dcs-ctl` seam are
  unchanged; a dedicated bus image would have changed the digest set.
- The `sim_bus_device` config block names the device the server
  serves, its bridge port, and the bus model declaring it;
  `endpoint_placement` records it `bridge` like the probe field,
  since no host socket is reachable from the rig. The launch stages
  the fixture inside the run directory with that device's
  `__BUS_ADDR__` placeholder bound to the device container's bridge
  name, mounts it into a controller-image container run under
  `--entrypoint dcs-sim-bus-device`, waits for the server's own
  bound-address report, and returns the bridge address and the staged
  document a leg mounts into the controller it points at the field.
  An enabler for the sim-bus rig legs (#1355/#1356/#1357); a leg
  needing the server itself to misbehave may still stage a double.

### Landed 2026-10-01 (sim-bus born-active claim refusal leg, #1356)

- The born-active startup-claim refusal now has per-revision lane
  evidence over a sim-bus field, not only over the sim-tcp plant: leg
  `2480_sim_bus_startup_claim_refusal` stages the lane's shipped
  `dcs-sim-bus-device`, launches a first controller onto it and
  asserts it takes the field's write-ownership claim, then stages a
  second born-active declaring the same model with no `--peer` and
  asserts it exits nonzero inside the documented bound carrying the
  live-holder verdict — the sim-tcp refusal reproduced on the register
  protocol, which the conditional `claim_writer_unless_held` grant
  makes possible there, since a bus claim dies with its last holder's
  link and a standing claim is therefore always a live incumbent to
  name. A `--standby` launch in the same shape is the positive
  control: it must converge `tracking` behind the incumbent with its
  command gate closed and no tracking-source refusal journaled — the
  arm the defect orphaned. The incumbent's claim and role must be
  undisturbed throughout: no claim-owner flip, no fenced demotion
  journaled on it, its scan still moving. Named diagnostics
  `sim-bus-claim-refusal-failed` and
  `sim-bus-claim-refusal-nondeterministic`, with
  `sim-bus-claim-refusal-unchecked` covering the planted negatives;
  two consecutive passes produce identical digests; a rig with no
  staged device server, no pinned `--owner-token` for the incumbent
  seat, an unsettled deployed pair, or a staged revision predating the
  contract — the second born-active claiming the field and going
  active — reports inconclusive. Each pass ends with the three born
  seats and the device server removed, and the sweep is audited back
  over the rig rather than assumed: each seat's presence read through
  the read-only state lever, the device server's own removal error,
  and the deployed pair framed once more with the leg's claim gone.
  A seat or a device server that outlived the sweep is a claim the
  legs behind this one would inherit, so it reports
  `sim-bus-claim-refusal-nondeterministic` instead of leaving them a
  dirty rig; the control's closed gate is read the same way, an
  unreadable SignalIndex leaving the leg with nothing to submit
  reported as an unread surface rather than a gate the control
  crossed.
- The leg rides the born-seat launcher's own field seam rather than
  adding one: `start_born_controller(seat, remote, document=)` mounts
  a staged model of the leg's choosing in place of the run's own, and
  a `remote` of None launches with no `--remote` attachment at all —
  the shape a `sim-bus`/`sim-cyclic` model needs, since the device
  carries its address in its parameters. This leg is the second user
  of the seam #1357's leg landed, and the return value's `model` is
  what lets it evidence that both ends of the register protocol read
  one declaration: the device server serves the document the seat
  mounted. The leg runs after `2470` and before the revision legs,
  sharing the driven/foreign born seats and the device server with it
  and sweeping both behind itself.

### Landed 2026-10-01 (holderless-claim reclaim recovery leg, #1260)

- The holderless-claim reclaim recovery contract — the continuity
  clause behind #1255's bound `reclaim_writer` grant, serving
  WW-LCM-001 — is exercised on the deployed pair by scenario leg
  `2390_holderless_claim_recovery`, the per-revision lane evidence
  that a standing claim with zero holders is the dead-owner shape
  the reclaim exists to preempt and that the fencing-loss peer's
  bound grant never refuses it forever. The defect the leg stages is
  the one the shipped unkeyed pair recorded: two successive
  ex-owners orphaned — the launch owner demoted by the standby's
  operator `POST /promote`, the successor fenced by a foreign tool
  claim — then the field's freeing racing both peers' unbound orphan
  probes so the winner left a holderless claim standing under the
  cleared peer's stale token, and the pre-fix bound re-grant refusing
  that placeholder on every scan while the placeholder fenced the
  field. The pair lost all field ownership for minutes until an
  operator promoted.
- With the pair settled and tracking the leg posts `/promote` on the
  standby so the active demotes and re-joins tracking with its loss
  mark cleared (the first ex-owner), drives a scenario attachment's
  `claim_writer` through the lane's claim-aware seam under a foreign
  tool token so the promoted owner is fenced and demotes orphaned
  with its loss mark standing (the second ex-owner), and reads both
  peers until they report the orphan island. The held foreign claim
  must refuse every conditional path for the window's rounds. A
  second attachment's `claim_writer` plus `release_writer` then frees
  the field while a staged unbound `ensure_writer` — the orphan
  cycle's own probe shape — raises the cleared ex-owner's holderless
  placeholder. From there no operator call and no restart: the
  loss-marked peer's bound reclaim must take that placeholder inside
  the resolution bound, the winner's journaled walk carrying
  `origin: reclaim` and never `request`, its writes landing through
  the re-seated claim, and the pair reconverging to exactly one
  active plus one tracking standby.
- Every stage is audited through both peers' serving monitors, the
  `probe_writer` claim surface, and the bind-mounted
  `--journal-file` mirrors: the attributed `field_claim_lost` on each
  ex-owner (the promote's successor token on the first, the foreign
  token on the second), the `field_orphaned` transitions, the
  refused probes' `field_claim_observed` claimant records, the
  loser's quiet journal, and the durable kinds each mirror holds. The
  doctored negative the leg must refuse is a pair *asserted*
  recovered — the fenced peer's monitor reporting `role=active` while
  `probe_writer` still names the cleared ex-owner's holderless claim
  and both peers stay orphaned. The orphan budget's own recorded
  `claim_writer_unless_held` rescue is the broken reclaim's symptom,
  never an alternative success, so a rig whose `--auto-promote`
  budget would fire inside the resolution watch reports inconclusive.
- Named diagnostics are `holderless-reclaim-failed` and
  `holderless-reclaim-nondeterministic`, with the self-check's
  `holderless-reclaim-unchecked` covering the planted asserted
  recovery, the planted wedge, and the planted operator-origin
  recovery; two consecutive passes produce identical digests; a run
  whose served surfaces predate the contract (no `probe_writer` claim
  surface, no owner/monitor attribution on the fencing verdicts, no
  pinned owner tokens, no `plant_ctl` seam, no per-controller journal
  mounts) reports inconclusive; the pair leaves on its launch roles.

### Landed 2026-10-01 (sim-cyclic fencing-loss demotion leg, #1357)

- The sim-cyclic fencing-loss demotion contract — the per-revision
  lane evidence for the single-writer/one-active invariant
  WW-FND-002 requires, over the claim protocol #1352's fix
  establishes — is exercised on the deployed rig by scenario leg
  `2470_sim_cyclic_fencing_loss_demote`. The leg stages the lane's
  `dcs-sim-bus-device` server on the run config's
  `sim_bus_device.cyclic_model` document (the block carries one
  fixture per register-protocol model the legs serve), launches a
  controller pair onto the staged `sim-cyclic` device on the
  driven/foreign born seats — a register-protocol model carries its
  device address in its parameters, so the pair needs no `--remote`
  — and severs every attachment's control connection with a device
  restart, the rig-durable link flap that releases the
  connection-bound claim to nobody. POST /promote on the tracking
  member then takes the claim, so the ex-owner's next staged
  exchange meets the fence: the leg asserts the demotion through
  the named path — served role walking `demoting` to `standby`, the
  durable `--journal-file` carrying `role_changed` with origin
  `fenced` beside exactly one `field_claim_lost` attributed to the
  promoted peer's owner token — inside the documented bound, with
  never two peers reporting `role:active` at any poll. The promoted
  owner's exchanges must keep completing with the claim held, the
  demoted peer's go census-only once its staged image releases, and
  the pair reconverges to its launch roles; the deployed pair is
  framed undisturbed before and after.
- Named diagnostics are `cyclic-fencing-loss-failed` and
  `cyclic-fencing-loss-nondeterministic`, with the self-check's
  `cyclic-fencing-loss-unchecked`; two passes produce identical
  digests. A run staging no device server, no `cyclic_model`, a
  staged model whose served device is not `sim-cyclic` or declares
  no output channel, a pair that never settles — or the recorded
  defect signature itself, the ex-owner whose io_health never names
  a fenced exchange and who stays active beside its promoted peer —
  reports inconclusive; the pair leaves on its launch roles.

### Landed 2026-10-01 (sim-bus driver lazy-reattach leg, #1355)

- The point-wise `BusDriver`'s lazy-reattach contract — #1351's fix
  serving WW-LCM-001's continuity clause and the health model's rule
  that a field communication fault is a transient, retriable event —
  is exercised per revision by scenario leg
  `3560_sim_bus_driver_reattach`. `dcs-sim-bus`'s `BusDriver` and
  `dcs-sim-net`'s `RemoteDriver` implement their recovery
  independently, so a rig with no register-mapped device says
  nothing about this one; the leg stages the subject through the
  lane's `sim_bus_device` seam — `start_sim_bus_device` launches the
  revision's own shipped `dcs-sim-bus-device` binary on the rig
  bridge, serving the run config's bus model with a `timeout_ms`
  stamped under the leg's ~2 s stall so a frozen device times out
  mid-exchange and the driver has a failed exchange to recover from.
  A controller pair is born-launched onto the `driven`/`foreign`
  seats with the staged document mounted and no `--remote` — a
  register-protocol model carries its device address in its
  parameters — so each member's serving monitor is the leg's window
  on its own driver's health. The pre-fix defect dropped the
  point-wise driver's stream on the first failed exchange and
  answered `Disconnected` for the life of the process: the active's
  scanning `communication_fault` values never cleared and the
  standby stayed unpromotable, because a promotion must claim the
  device and the claim request rode a dead link. The leg drives
  both outage classes the finding records — (a) the device server
  restarted under the pair, returning with its claim table empty so
  the owner must re-attach *and* re-arm, and (b) the device frozen
  ~2 s then thawed through the lane's `freeze_sim_bus_device`/
  `thaw_sim_bus_device` levers, a transient unanswerable window with
  nothing dying — and through each member's serving monitor asserts
  the healthy baseline carries a connected link and no standing
  `last_error`; the outage is counted in the served `io_health` —
  the boundary counters moved over the baseline with the fault
  stamped — and the first serve reporting the link `connected`
  after the outage already carries the cleared standing record, the
  reset failure streak, and the kept cumulative history and recorded
  fault, with the served tick and scan cadence resumed without
  rewinding past the running peak, which would be a controller
  restart rather than a driver recovery; the pair's launch roles
  hold throughout; and `POST /promote` on the converged standby must
  answer inside its own bound once the backend is back — the
  promotion path's device claim riding the recovered link — after
  which the launch roles are restored by promoting the original
  owner back. The re-attach is fast by design, so the window in
  which the link is observably down is about one re-attach interval
  wide and no poll can prove it caught it: the stall class, whose
  freeze the leg owns, therefore *holds* the device down past the
  documented stall until every member has surfaced the outage as
  `disconnected` with the severing failure named, and only that
  class gates on the transient — the restart class lets the device
  return on its own schedule and witnesses an outage that fell
  between two polls through the moved counters instead.
- Named diagnostics are `sim-bus-reattach-failed` and
  `sim-bus-reattach-nondeterministic`, with the self-check's
  `sim-bus-reattach-unchecked` covering the planted lingering
  record, standing streak, reset history, uncounted outage, and held
  tick; both outage classes run twice with identical digests; a run
  context carrying no `sim_bus_device` device-server seam or born-
  controller levers, a rig whose device or pair never answers, a
  device that never serves again after a restart, a pair that never
  settles tracking, a staging lever that never completes, a restore
  the leg cannot classify, and a staged revision whose served
  `io_health` cannot express the driver's diagnostics and
  failed-exchange accounting — every build, until #1351's fix lands
  — report inconclusive; the rig leaves on its launch roles with
  the staged born seats and device container torn down.

### Landed 2026-10-02 (scripted-miss claim-hold leg, #1418)

- The scripted-miss claim-hold contract — the per-revision lane
  evidence for the contract #1413's fix establishes on the register
  protocol, the exchange semantics WW-FND-002's claim lifecycle rides
  — is exercised on the deployed rig by scenario leg
  `2482_sim_bus_scripted_miss_claim_hold`. A queued `miss` is
  development tooling deciding what the next exchange observes, so it
  may cost the consuming attachment its cycle; it must never cost that
  attachment its connection, and through it the write-ownership claim
  only a disconnect or `release_writer` may release. The defect the
  contract answers: the scripted `miss` dropped the consuming
  connection unanswered, teardown released the claim hold bound to it,
  and since a bus claim stands only while an attachment holds it, any
  unfenced attachment could free another's field ownership by queuing
  one outcome onto the shared device. The outcome now answers `missed`
  in band, keeping the script queue outside the arbitration
  vocabulary.
- The staging is the rig's own register-protocol field: the lane's
  shipped `dcs-sim-bus-device` server on the run config's
  `sim_bus_device.cyclic_model` document, and one born-active
  controller launched onto that staged document on the `driven` born
  seat with no `--remote` and no pair, so it takes the device's
  write-ownership claim and serves `field_claim: held`. The `sim-cyclic`
  document is the one whose per-scan process-image `exchange` is the
  only traffic the scripted queue feeds, so a queued outcome lands on
  the claim holder's own scan and nowhere else; the field is kept
  single-attached on purpose, since the queue is device-global and a
  second exchanging attachment could consume the queued miss and make
  the attribution nondeterministic. The device's continued service is
  witnessed through the holder's own following exchanges and through
  the shipped `dcs-sim-bus-ctl` the leg scripts and reads it with.
- That tool is a new lane seam: `sim_bus_ctl(*args)` `docker exec`s it
  inside the device server's own container against its loopback
  listener, the register protocol's counterpart to the plant image's
  `dcs-plant-ctl` — nothing host-side reaches the rig bridge, and the
  ops ride the revision's own binary rather than a second Python
  implementation of the wire protocol. The binary rides the controller
  image beside `dcs-sim-bus-device` through the lane's recorded
  shipped-binary contract (`qa_lane/ship.json`): its compile group gains
  `--bin dcs-sim-bus-ctl` and the controller payload gains the tool
  itself, so #1419's `no recorded compile target produces …` check
  holds for it and a pinned lane copy behind this leg's contract is
  refused by name at the deployment step. The entrypoints, the two
  reported digests, and the host-side `dcs-ctl` seam are unchanged.
- The pass queues `miss` through that tool and reads the failed cycle
  the scan recorded: the cumulative `failed_exchanges` moved past the
  settled baseline, and the driver's own standing description of the
  failed exchange names the *in-band* scripted miss — the line a
  severed link cannot produce, since it reads as the dropped
  connection instead. Across the window the claim the connection
  carries must survive: the run still reports the device claim held,
  still `active`, its run tick still moving, and its durable
  `--journal-file` carrying no `field_claim_lost`, no fenced
  `active → demoting` walk, no observed foreign claimant, and no
  second run boundary. The `complete` queued afterwards must answer on
  the same link — the exchange successes moving past the missed cycle,
  the link verdict back to connected, the boundary's failure streak
  back to zero — with the device answering the tool's own served read.
- A staged revision predating the contract is inconclusive, and the
  signature is read off the rig rather than assumed: the queued miss
  answered as a sever and the claim bound to that connection went with
  it. Named diagnostics are `sim-bus-scripted-miss-failed` and
  `sim-bus-scripted-miss-nondeterministic`, with the self-check's
  `sim-bus-scripted-miss-unchecked` covering the planted negatives —
  the issue's doctored case, a record asserting the claim survived
  while the device severed the link, plus the claim freed anyway, the
  fenced and operator demotions, the restarted process, the miss no
  exchange consumed, a recovery that never completed, and a frozen run
  clock; two consecutive passes produce identical digests; every pass
  ends with the seat and the device server swept and the sweep audited
  back over the rig, the deployed pair framed before, after, and once
  the leg's own claim is gone.

### Landed 2026-10-02 (skewed-claim preemption bound leg, #1345)

- The claim-skew preemption bound is exercised per revision by
  scenario leg `2490_claim_skew_bound` — the lane evidence for the
  single-field-writer contract over the tick-domain comparability
  rule `CONTEXT.md` records (WW-FND-002's failover continuity). The
  rule bounds the tracking path today: #1336 retired the window
  that stood between a pulled document's declared stream position
  and a detached prober's own paced position, since the gap between
  them measures pace asymmetry and never line membership. Claim
  arbitration has no equivalent bound — a standby computing its
  claim against a live incumbent reads the claimant's basis and the
  incumbent's stamps directly and takes the field whenever the
  comparison lands past the promotion bound, so the incumbent's next
  staged exchange meets the fence, it demotes itself in place under
  the field-arbitration origin, and the claimant writes the field
  from a position the comparability rule forbids resolving on. The
  contract the leg asserts: a claim whose basis exceeds the recorded
  skew bound is refused or bounded **by name**, so a live incumbent
  keeps write-ownership, while a claim whose basis sits inside the
  bound is unaffected, so the documented switchover still lands.
- The rig stages the skew rather than injecting it, through the born
  launcher's new per-container `scan_ms` seam: a run's tick accrues
  one per scan, so a seat launched at 25 ms accrues four ticks for
  every one the holder's 100 ms pace does. One pass stages one field
  and one proven holder per arm, so the skew is the only variable
  either arm turns. The holder (`revised`) is a born-active
  declaring no pair over the born legs' scratch field — its
  conditional startup grant lands and `probe_writer` serves the
  claim posture through `/role`'s `field_claim: held`. The in-bound
  arm (`driven`) declares `--standby <holder>` at the documented
  cadence, converges `tracking`, reads its basis against the
  holder's immediately before claiming — a freshly launched
  tracker's own tick trails the holder's by the holder's accrued
  scans, well inside the leg's recorded 64-tick staging bound — and
  its `POST /promote` must land the documented switchover: the seat
  settles `active` with the claim held and the holder demotes in
  place through the fenced-origin `active → demoting → standby`
  walk beside exactly one `field_claim_lost` naming the promoted
  peer's owner token. The skewed arm (`foreign`) declares `--standby
  <the promoted peer>` at 25 ms, and the leg waits for the *measured*
  separation between the two seats' served `/role` ticks to pass the
  bound before the claim goes out: an attempt computed inside the
  bound proves nothing, so a staging that never separated reports
  nondeterministic rather than a verdict.
- The attempt's answer is the subject. `POST /promote` carries the
  documented unconditional claim — the winner-take-all grant a
  switchover relies on — and must not take the field from the live
  incumbent; either outcome the contract allows counts (refused, or
  granted and then bounded), but it must be named, so the refusal is
  auditable rather than an unexplained gate that stays shut. A
  refusal carrying one of the promote gate's own names
  (`not_converged`, `already_active`, `no_tracking_source`) is not
  that audit trail — the claim was never computed — and is reported
  as the staging race it is. Across the attempt the promoted holder
  must be untouched: still the field's writer, still `active`, its
  scan advancing, its journal carrying no `field_claim_lost` and no
  fenced-origin demotion. The deployed pair never enters the staging
  and is framed before, after, and once the leg's own seats are gone.
- No staged revision predates this contract in a shape the leg
  declines on: a skewed claim that resolves the field is the defect
  the leg names, not an unread surface, so every inconclusive verdict
  is rig-side — an unstaged lever, an unreachable endpoint, an
  unsettled pair, or a staging that never produced the basis it
  declared. Named diagnostics are `claim-skew-bound-failed` and
  `claim-skew-bound-nondeterministic`, with the self-check's
  `claim-skew-unchecked` covering the planted negatives — the issue's
  doctored case, the attempt read as refused and bounded while the
  skewed claim preempts the field; two consecutive passes produce
  identical digests; every pass ends with the three born seats and the
  scratch field swept, the sweep audited back over the rig (each
  seat's presence through the read-only state lever, the field's own
  shipped tool refusing), and the pair left on its launch roles.

### Landed 2026-10-02 (reclaim convergence-gate leg, #1321)

- The convergence-gated fencing-loss reclaim contract — the
  per-revision lane evidence for the contract #1317's fix establishes
  (WW-LCM-001 continuity and the ownership-epoch integrity the
  failover-miss budget's staleness bound rests on) — is exercised per
  revision by scenario leg `2485_reclaim_convergence_gate`. The
  fencing-loss reclaim exists so a fenced ex-owner can re-take its own
  released claim and escape the released-preemption wedge; the
  conditional-claim paths that break that deadlock may only run where
  the asker can prove convergence with the field's line, so a run
  with zero convergence evidence — never tracked, unsynchronized, no
  adoptable tracking source — must never preempt a different owner's
  claim on stale state. The defect the contract answers let the
  least-converged participant win the post-outage race purely on
  transport ordering: the stale ex-owner's armed reclaim landed
  before the incumbent's re-attach, re-activating it on a stale image
  while the incumbent was fenced and demoted on its own re-attach.
- The leg stages the finding's deterministic sequence on the lane's
  scratch sim-serve field, so the deployed pair is never moved:
  `start_born_field('serving')` serves the plant and a born-active on
  the `driven` seat claims it to `active`; `start_born_field('serving')`
  again re-serves the field under the same container name — a fresh
  server with an empty claim table and the ex-owner's control
  connection severed — so a born-active on the `foreign` seat launches
  pending and claims first while the ex-owner reattaches, meets the
  fence, and settles `standby`/`unsynchronized` with the loss mark
  armed and no adoptable tracking source; `pause_born_field` freezes
  the plant and the leg holds it there until both seats' served
  `io_health` reports their control connection down, and
  `unpause_born_field` thaws. A `dcs-sim-net` claim outlives its
  holders, so the thaw leaves the incumbent's claim standing
  holderless — the shape the field's arbitration cannot tell from a
  dead owner's, and the shape the defect's reclaim exploited.
- On the thaw, through both seats' serving monitors and their durable
  `--journal-file`s, the leg asserts that the ex-owner's reclaim
  cannot preempt the live claim: the incumbent's re-attach keeps or
  retakes ownership on its live line (it settles `active` with the
  claim `held` and its served tick advancing across the window), the
  unsynchronized ex-owner stays `standby` without the field across
  every poll, and no ownership epoch rolls back to the staler image —
  the ex-owner's journal carries no `reclaim`-origin promotion, no
  re-armed claim, and no `field_claim_observed` naming the incumbent,
  and the incumbent's journal carries no walk away from `active`. The
  positive control then proves the probe still does its designed work:
  on a freshly re-served field a born-active on the `revised` seat
  claims it declaring the incumbent seat its tracking source, the
  re-serve and the incumbent's claim fence it in place, it converges
  `tracking` on the live line, and once the incumbent is removed and
  the claim stands holderless its bound reclaim of its own released
  claim preempts by design — walking `standby → promoting → active`
  under the `reclaim` origin with the claim `held`. The deployed pair
  is framed before and after, every staged seat and the scratch field
  are torn down, and the launch configuration and roles are restored.
- Named diagnostics are `reclaim-convergence-failed` and
  `reclaim-convergence-nondeterministic`, with the self-check's
  `reclaim-convergence-unchecked` covering the planted asserted-gating
  record, the ownership-epoch rollback, the refused ask, the displaced
  and fenced incumbent, the silent positive control, the unrestored
  pair, and the instability shapes; two passes produce identical
  digests. A run context carrying no born-field staging levers or
  per-seat journal files, a deployed pair off its settled launch shape,
  and a read that dropped report inconclusive. #1317's fix is a
  behavioural guard with no new served field and no new durable
  record, so the leg's pre-contract signature is behavioural too: the
  ex-owner's reclaim *issued* — its journal carries the granted
  `reclaim`-origin promotion or a refused `field_claim_observed` — and
  its own serving monitor showed it taking the field is a revision
  predating the contract, and reports inconclusive. A record whose
  served surfaces report the gating holding while its durable journal
  proves the reclaim took the field is not that signature: the served
  monitors never saw the take, so the leg names the contradiction as
  the contract failure it is.

### Landed 2026-10-02 (attributed-switch control-lane isolation leg, #1265)

- The attributed-switch control-lane isolation contract — the
  per-revision lane evidence for the contract #1264's fix establishes
  (WW-FND-004's disposable-consumer quarantine extended to the
  actuation half; decision 83's schedule) — is exercised per revision
  by scenario leg `1020_attributed_switch_isolation`. The monitor's
  `role_change` routing documents `POST /promote` and `POST /demote`
  as control-plane actuation on a dedicated control lane, so an
  operator's switch — issued during an incident, exactly when
  consoles wedge — never queues behind serving workers pinned by
  undrained responses. The attributed `{"actor": …}` form carries a
  body, and before the fix every request with `body_length > 0` was
  quarantined on the submission lane beside `POST /scan`: an
  operator's declared-identity switch queued behind severed scan
  batches (~40 s behind two `scans: 40` batches on a driven peer
  whose source had gone silent) and stalled outright behind
  raw-socket clients that declare a `Content-Length` and never
  deliver it, both submit workers pinned with no read timeout to
  bound them. The attributed body is tens of bytes — always inside
  tiny_http's eager-read bound — so it arrives already buffered and
  rides the control lane beside the bare request.
- The leg stages both congestion shapes on the lane's own driven
  standby, never on the deployed pair's monitors: `start_driven`
  launches the run's third monitor `--standby <field owner> --driven`,
  so every checkpoint pull it performs happens inside a `POST /scan`
  request, and `pause_controller` on the owner freezes that source in
  place — the listener completes the handshake and nothing answers, so
  each scan of a batch waits the documented `CHECKPOINT_PULL_TIMEOUT`
  out and the batch holds its submission worker for its whole span.
  The first shape is the holding set: two raw sockets send a head
  declaring a body past the eager-read bound plus a partial body and
  then hold, pinning both submit workers for as long as the leg keeps
  them. The second is the reproduction's own: two severed `POST
  /scan` batches sized to occupy both submit workers, sent whole and
  never read.
- Under each shape the leg proves the pin is real — a one-scan
  submission that must not answer inside the declared probe bound —
  and then asserts the contract through the congested peer: an
  attributed `POST /demote` and an attributed `POST /promote` each
  answer inside the declared bound with the verdict the peer's own
  reported posture earns (`not_active` from the non-owner,
  `not_converged` from the standby whose final-sync pull just failed
  against the silent source), never a lane-timeout silence, while
  `GET /health`, `GET /role`, and the bodiless `POST /demote` stay at
  baseline. The congestion is then released — the lane must serve a
  scan again, the source thawed, the pair reconverging with its
  launch roles restored — and the run's driven standby is torn down
  before the legs behind it see the rig as they launched it.
- Named diagnostics are
  `attributed-switch-isolation-failed` and
  `attributed-switch-isolation-nondeterministic`, with the
  self-check's `attributed-switch-isolation-unchecked` covering the
  planted negatives: isolation asserted held while an attributed
  switch sits queued behind the staged batches or never answers behind
  them, the same shared-lane wait under the holding set, an attributed
  answer carrying a verdict the peer's posture does not earn, a
  baseline lane starved or late, an unreleased lane, an unrestored
  pair, and the instability shapes. Two consecutive passes produce
  identical digests. A run context carrying no driven-launch or
  frozen-source seam, a pair off its converged launch shape, a
  congestion shape that never stood, and a read that dropped report
  inconclusive.
- #1264's fix is a behavioural routing guard with no new served field
  and no new durable record, so the leg's pre-contract signature is
  behavioural too — and positive rather than a mere lateness: an
  attributed switch that *shared* the submission lane's wait under the
  holding set, its answer landing only once the staged congestion
  drained or never while it stood, with the bare control lanes beside
  it answering at baseline, is a monitored revision that routes bodied
  switches onto the submission lane and reports inconclusive. Every
  released and staged build predates the contract until the fix lands,
  so that is the honest verdict for them. A revision whose holding
  shape answered in isolation and whose batch shape then queued or
  timed out is not that signature — the contract was demonstrated
  present on that very monitor — and the leg names it as the contract
  failure it is.

### Landed 2026-10-02 (demoted ex-owner's ahead-bound re-join leg, #1275)

- The demoted ex-owner's ahead-bound line rejoin contract — the
  per-revision lane evidence for the contract #1269's fix establishes
  (WW-LCM-001's continuity clause) — is exercised per revision by
  scenario leg `2375_own_tick_ahead_rejoin`. A fenced or demoted field
  owner that declares no configured `--standby` must re-join the line
  through the field-arbitrated claimed-monitor rendezvous
  (`adopt_claimed_source`, dcs-monitor `lib.rs` ~L3059 — the unkeyed
  pair's only provable rendezvous) *regardless of how far its own paced
  tick has drifted from the line's*: `verify_owner_checkpoint` and
  `verify_announced_checkpoint` must not treat the prober's own tick
  plus the retired `MAX_ANNOUNCED_AHEAD` window (32) as the
  line-membership reference, because a detached prober's own clock has
  no authority over where the line moved — its `tick` and `stream_tick`
  alike advance at its own scan cadence, so the gap measures pace
  asymmetry, never line membership (#1336 retired the bound).
- The defect the leg stages is self-reinforcing and journal-empty. A
  tracking peer's paced clock keeps counting through every source
  outage it survives while the pulled stream stands still, so the lead
  it accrues is permanent and the promotion carries it: once the
  promoted successor's served tick led the demoted ex-owner's own by
  more than the thirty-two-tick window, the ex-owner read that lead as
  a forged position, refused the field's own `Ahead` on every unkeyed
  rejoin path, and — with no configured source, no proven announced
  hint, and no line proof to fall back on — stranded
  `standby`/`unsynchronized` with a silent journal, the conditional
  startup grant refused while the claim stood, and the operator's only
  recovery a restart-as-standby.
- The staging rides the lane's own lifecycle seam on the deployed
  paced pair: `active` (ctrl-a) is the field owner declaring no pair at
  all, so the claim's declared monitor is the only candidate its
  re-join can resolve, and `standby` (ctrl-b) is the pair's
  `--standby` tracker. `stop_controller` on the owner holds its
  container down while the tracker keeps its 100 ms cadence, so every
  pull is a produced-nothing miss while the tracker's own run tick
  advances a scan at a time against a frozen line; the leg waits for
  the *measured* separation to clear the retired window plus a margin,
  brings the owner back, waits for the pair to reconverge, and reads
  both members' own served run ticks again to record the lead the
  promotion will carry. A promotion computed inside the retired window
  would prove nothing about one computed past it, so a staging that
  never separated reports nondeterministic and stops there — the
  launch owner's container is thawed either way.
- The routine promote rides the lead-carrying tracker with no
  `POST /demote` on the owner first, so the field's arbitration
  preempts the standing claim and the owner's first fenced write
  demotes it in place; the promote gate's own named refusals
  (`not_converged`, `already_active`, `no_tracking_source`) answer
  before any switchover is computed and name a race in the staging
  rather than the contract, so they report nondeterministic. The
  lead-carrying checkpoint must itself carry the declared stream
  position (#1269's own surface) at or behind the run tick it rides;
  a document declaring a position ahead of its own tick is the nonsense
  shape and a contract failure, and a document declaring no position
  at all is a pinned revision predating the contract, which the leg
  reports inconclusive on rather than asserting.
- The re-join is asserted through the demoted ex-owner's serving
  monitor and its runner-owned `--journal-file`: the sync verdict
  converging to `tracking` rather than parking `unsynchronized` past
  the documented lane bound of 60 of its own paced scans, the
  fenced-origin `active → demoting → standby` walk beside exactly one
  `field_claim_lost` attributed to the promoted claim, the field's own
  post-promotion fencing verdict naming that claim and its declared
  monitor, exactly one `tracking_source_adopted` naming the successor's
  declared endpoint, no `tracking_source_refused` naming the successor,
  the re-joined peer's served document stamping the successor's
  ownership honestly, and no further `run_boundary` — the operator's
  restart-as-standby must not be what converged it. The adoption audit
  reads the ex-owner's current *run*, not one switchover's cursor:
  `adopt_claimed_source` is a process-lifetime pin, so one adoption
  legitimately serves every re-join that run makes and a second
  promotion in the same run owes none. A later `POST /promote` on the
  converged ex-owner answers the converged path, and the pair's launch
  roles restore; the launch-layout clauses ride a switch back that
  actually took, since a refused or unanswered one leaves the leg
  nothing to audit about the layout.
- Named diagnostics are `ahead-bound-rejoin-failed` and
  `ahead-bound-rejoin-nondeterministic`, with the self-check's
  `ahead-bound-rejoin-unchecked` covering the planted negatives: the
  issue's doctored case — the re-join asserted while the ex-owner stays
  unsynchronized past the documented bound — a re-join that lands only
  after a wait the bound does not admit, an adoption that is missing,
  duplicated, foreign, or accompanied by a journaled source refusal, an
  adoption a restart performed, a silent or duplicated or unattributed
  claim loss, an unwalked or unattributed demotion, an off-script
  served role walk, a promoted successor that never holds the field, a
  fencing verdict that does not fence or names another claim or no
  declared monitor or another member's port, a dishonest served
  document, a refused switch back, unrestored launch roles, and every
  instability class. Two consecutive passes produce identical digests.
  A run context carrying no controller lifecycle seam, an unreachable
  member, a bridge-placed endpoint, no journal file, no pinned owner
  token, a pair off its launch layout, a served surface missing the
  ownership stamps, an integer run tick, or the sync vocabulary, and a
  lead-carrying document declaring no stream position report
  inconclusive.
- #1269's fix has no new served field beyond the declared stream
  position and no new durable record, so the leg's pre-contract
  signature is that surface's absence: a lead-carrying checkpoint that
  declares no `stream_tick`, a role report with no sync vocabulary, or
  a served document without the `source_owns_field`/`line_owner`
  stamps the rendezvous rides. Every released and staged build predates
  the contract until the fix lands, so that is the honest verdict for
  them. A revision whose ex-owner re-joins through the claimed monitor
  with the adoption journaled is not that signature — the contract was
  demonstrated present on that very monitor — and the leg names it as
  the contract failure it is.

### Landed 2026-10-02 (voluntary-demote released-claim leg, #1273)

- The voluntary-demote released-claim contract — the per-revision lane
  evidence for the contract #1270's fix establishes (WW-LCM-001
  continuity) — is exercised per revision by scenario leg
  `2495_demote_release_stays_released`. A voluntary `POST /demote` is a
  deliberate hand-back of the field's write claim, and the
  just-demoted member's orphan-cycle ensure must not re-arm that claim
  under its own token: the released field then stands — no owner
  serving it — until a documented conditional path takes it where it
  stands, the fencing-loss-armed ex-owner's bound reclaim, a
  conditional promote, or a startup grant. The finding the contract
  answers left a claim standing under a member reporting `standby`,
  a run whose gate is closed so it serves no owner checkpoints, while
  the field's own arbitration answered every conditional,
  non-preemptive path `fenced` — and the re-arm journaled nothing at
  all, so the durable trail could not even show who re-took the claim.
- The leg stages the finding's own sequence on the deployed pair, over
  the pair's own control plane and the pair's own field. With the pair
  settled on its launch layout — the launch owner holding the field
  with its sibling converged `tracking` behind it, and the
  claim-aware attachment reading the claim under the launch owner's
  pinned `--owner-token` — `POST /promote` on the tracking peer
  preempts the live owner's claim, the former owner's next field write
  meets the fence and demotes it in place, and the fenced ex-owner
  converges `tracking` on the successor the standing claim declares
  (its journal's `field_claim_lost` names the promoting token, the
  loss mark that arms the reclaim). `POST /demote` on the new owner is
  then the deliberate hand-back: the claim it leaves is `yielded` and
  holderless where the demotion keeps it standing for the successors'
  conditional paths, and the field reads `unclaimed` where the release
  frees it outright — the leg accepts either and records which it saw
  in the digest's `release` word.
- The hand-off window is polled on both peers' serving monitors, on a
  dedicated attachment's read-only `probe_writer` verdicts (the
  claim-aware seam the shipped `dcs-plant-ctl` cannot serve — it wraps
  its own mutations in its own conditional claim — so the probe stays
  on the raw plant-protocol client, as the field-claim and
  claim-reclaim legs' claim probes do), and on both durable
  `--journal-file`s above cursors taken where the demotion settled.
  The clauses: the demoted member journals no `field_claim_rearmed` of
  the claim it handed back; any re-arm that does land names its field
  point; the released claim never stands under the demoted member's
  token at the end of the window while that member serves no field
  writes; whichever member took the field took it through a walk the
  durable record names — the `reclaim`-origin promotion the
  fencing-loss arm drives or the re-arm record the grant journals —
  and never through a `request`-origin promotion the leg never issued;
  the pair reconverges to one active plus one tracking standby; the
  watched field output's tick advances through the shipped tool, so the
  reconverged owner really writes again; and the launch claim state
  and roles are restored afterwards.
- Named diagnostics are `demote-release-rearm-failed` and
  `demote-release-rearm-nondeterministic`, with the self-check's
  `demote-release-rearm-unchecked` covering the planted doctored
  record the issue calls out (the release asserted as staying released
  while the demoted peer's token still holds the claim), the re-arm
  that lands with no point named, the resolution no durable record
  names, the operator re-promote, the pair that never reconverged, the
  frozen field, a third party's claim, the unrestored launch layout,
  and the instability shapes. Two consecutive passes produce identical
  digests. A run context carrying no plant endpoint, no shipped plant
  tool, no pinned owner tokens, or no per-peer journal files, a
  third-party mutation answering unfenced, a fencing verdict naming no
  owner, a field answering no claim observation, and a read that
  dropped report inconclusive. #1270's fix adds a durable record
  (`field_claim_rearmed`) and a behavioural suppression, so the
  leg's pre-contract signature is both: the demoted member's own orphan
  probe re-armed the released claim under its own token, the claim
  never left that token across the whole window, no peer journaled a
  re-arm naming it, and the only member that ever stood on the field
  was the demoted one re-taking the claim it had handed back — the
  shape a build without the `yielded` suppression presents, and what
  every released and staged build predates until the fix lands. A
  re-arm journaled *by name* under that token is not that signature:
  the `yielded` suppression is then demonstrably absent while the
  durable record is present, and the leg names it as the contract
  failure it is.

### Landed 2026-10-02 (self-standby startup-claim refusal leg, #1343)

- The self-standby startup-claim refusal contract — the per-revision
  lane evidence for the contract #1340's fix establishes (the
  startup-claim arbitration WW-FND-002's redundant failover depends
  on) — is exercised per revision by scenario leg
  `2497_self_standby_refusal`. A tracking source must name a
  *different* instance: a `--standby` (or `--peer`) target resolving
  to this run's own `--listen` socket is a usage error at launch,
  refused before the run exists. The condition the gate can only
  catch by resolving, never by string-comparing, is the launch whose
  tracking seat *is* its own announced address: the finding left that
  declaration passing every check, so each pull returned the run's own
  checkpoint — a standby's document always stamps
  `source_owns_field: false`, so every apply scored a heartbeat miss —
  and the run lived as a legitimate-looking seat in the pair,
  indistinguishable on the monitor from a standby that tracks a real
  peer, where the same class of verdict at boot (a startup claim a
  live peer holds, a `--peer` naming this run, a persistence alias) is
  a nonzero exit carrying the named refusal.
- The leg stages exactly that on the deploy fixture's labeled
  containers: a born seat launched `--standby` at *its own* rig-bridge
  container name and monitor port, under its own pinned
  `--owner-token` — the address its own checkpoint pulls announce
  through `?peer=` to every participant that pulls from it, the seat
  included, and the address the field sees it announce itself as.
  Nothing in the launch spells "me": the name is a different string
  from the wildcard `--listen` bind and resolves to the container's
  bridge IP rather than loopback, so only the product's resolution
  tells the two apart. Before judging anything the leg proves the
  staging landed — the launch's own returned target must equal the
  seat's own advertised address, else it reports the self-referential
  condition as never staged and judges no launch.
- The clauses: the launch is refused inside the bound by a nonzero
  exit whose stderr names the verdict — the offending `--standby`
  flag, the resolution that made the target this run's own socket,
  that `--listen` socket, the "must be a different instance" reason,
  and the staged address itself (the usage block the shell prints
  below the refusal repeats the flag and the reason, so the
  resolution clause and the address echo are what tell the verdict
  line from the boilerplate — and the born seat's process-verdict
  read takes a `docker logs` tail wide enough to hold that whole
  error, the refusal line plus the controller's ~150-line usage
  block); it never occupies a seat, so the process is gone, its
  monitor never answered once across the refusal window, and its
  durable journal carries no run boundary and no entry, which is
  where the refusal classes' contract carries a boot refusal (the
  process's own stderr, precisely because the run never existed to
  journal); the field never moved, a read-only `probe_writer` still
  naming the incumbent's own pinned owner token and never the refused
  seat's; a legitimate `--standby` launch naming a *different*
  instance is not refused and converges `tracking` on the incumbent
  with the incumbent's claim observed and its own run boundary
  journaled, so the gate cannot pass by refusing every tracking
  source; and the deployed pair is undisturbed throughout — its field
  owner `active` with the claim held and its tick advancing, its
  member `standby` — with every staged seat removed afterwards. Two
  consecutive passes produce identical digests.
- Named diagnostics are `self-standby-refusal-failed` and
  `self-standby-refusal-nondeterministic`, with the self-check's
  `self-standby-refusal-unchecked` covering the planted negatives the
  issue calls out (the self-standby asserting its own refusal while
  it occupies a legitimate standby seat), the zero exit, the refusal
  that never named itself or that dropped the resolution, the own
  socket, or the staged address, the silent exit, the refused seat
  that served a monitor anyway, the run record its journal should not
  carry, the field that opened or moved off the incumbent or under the
  refused seat's own token, the legitimate standby the gate
  over-refused or that never tracked or unobserved the claim or
  journaled no run boundary or was staged on the self-addressed target
  itself, the incumbent's demotion or stall, the pair's disturbed
  member, the seat left behind, and the instability shapes. A run
  context carrying no born-seat staging levers, no published monitor
  for a born seat or the pair, no per-seat journal files, no published
  plant endpoint, a deployed pair that never settled on its launch
  layout, a staging whose own-address identity cannot be proven, and
  a read that dropped report inconclusive.
- #1340's fix is a boot-time argument check with no served field and
  no durable record, so the leg's pre-contract signature is
  behavioural and is exactly the finding: the self-addressed launch
  still running after the bound, serving `standby` under an honest
  `unsynchronized` verdict — covering no peer at all — with its
  journal's own run boundary present, and the pair otherwise
  undisturbed, is a revision whose self-reference gate missed the
  own-token announcement. Every released and staged build predates
  the contract until the fix lands, and the leg names that shape as
  the contract failure it is rather than a silent pass. A build that
  refuses the launch but exits zero, leaves the address unnamed, or
  has bound a monitor first is named the same way — the gate
  demonstrably present, the verdict not yet the contract's.

### Landed 2026-10-02 (cross-peer shared-state-file refusal leg, #1347)

- The cross-peer persistence-path distinctness refusal — the per-revision
  lane evidence for the contract #1341's fix establishes, the family
  the 3695 leg pins within one launch — is exercised on the deployed rig
  by scenario leg `3697_shared_state_file_refusal`. The `--state-file`
  checkpoint carries one run's tick domain, receipt log, and component
  state, so it is single-writer: the guard rides a `.lock` sidecar the
  write-then-rename never replaces, which is what keeps the claim
  attached to the path across every save. The guard compares the
  declared paths inside one launch, so the deployed pair had never been
  read against it: two peers configured with the same `--state-file` —
  a shared volume, a copied bind-mount stanza, one member's directory
  mounted over the other's — each replace the other's checkpoint on
  every capture, and nothing reports the second writer. A restart
  resuming that file adopts whichever run renamed last as its own
  state, under the model fingerprint any same-model writer matches.
  #1341's fix refuses the configuration at startup: the launch whose
  `--state-file` a live peer already claimed exits nonzero naming the
  file, its sidecar, and the conflict it found held.
- The leg stages that misconfiguration on the deployed pair through the
  run context's new `share_state_with` deployment doctoring: the
  runner's relaunch lever rebuilds the non-owner's launch with its
  declared persistence directory bind-mounted onto the live owner's,
  the member's own directory still mounted at a second path serving
  only its `--journal-file`/`--history-file` append sinks. The pair's
  identical declarations then resolve to one backing checkpoint, the
  owner's live writer lock included, and the only refusal available is
  the cross-peer single-writer claim — never an append sink's own lock,
  never a same-launch path comparison. The member relaunched is the
  pair's non-owner, so the staged refusal costs the field nothing.
- The claims: the correctly-pathed pair settles and tracks with each
  member's own checkpoint carrying its own run `generation`; the
  aliased member's container reads down with a nonzero exit (or no live
  process) and its captured output naming the shared checkpoint, its
  `.lock` sidecar, and the writer-lock conflict, its monitor falling
  silent; the shared checkpoint sampled across the staged window keeps
  the owner's generation while the owner's tick keeps advancing and the
  aliased member's own file stands untouched, the field owner still
  `role=active` and answering every poll; and the correctly-pathed
  relaunch restores the member so the pair reconverges to one active
  plus a tracking standby. The leg runs after the same-peer
  alias-refusal leg and before the unclaimed-rearm leg, so the 3695
  verdict is read on its own scratch probes and the leg leaves the
  launch roles the cases behind it need.
- Named diagnostics are `shared-state-file-accepted` (a contract miss:
  an aliased launch that served, exited 0, refused unnamed, kept its
  monitor up, or left the shared checkpoint carrying both runs' writes)
  and `shared-state-file-nondeterministic` (unread verdicts, an
  unsettled, moved, frozen, or silent owner, a stalled shared
  checkpoint, an unrestored pair, two passes disagreeing), with the
  self-check's `shared-state-file-unchecked` covering every planted
  negative. Two consecutive passes produce identical digests; a run
  context carrying no relaunch lever, no container-state probe, or no
  per-member `--state-file` path, a single-endpoint, unreachable, or
  never-settling rig, and a staging that never landed report
  inconclusive.

### Landed 2026-10-02 (recorded shipped-binary contract, #1419)

- The three exploration runs at cabe3b3 — a revision carrying both
  c8b0cde's sim-bus ship list (#1368) and the earlier `dcs-plant-ctl`
  shipping precedent (#654) — each measured the bounded image set
  carrying only the two images' entrypoints, so the keyed-interposer,
  sim-bus, and claim-probing legs fell back to bind-mounting recovered
  binaries out of the host build cache or probing the claim protocol
  raw. The ship list was not the gap; its *reach* was. The lane code is
  pinned on the host at `/srv/homelab/dcs-hwtest/qa_lane/` separately
  from the revision under test (the deploy README's own rule: upgrading
  it is a deliberate deployment step, not a side effect of a merge), and
  the deployed copy that built those three runs' images predated both
  ship lists, so its image build staged entrypoints alone while the
  tested revision's tree compiled every shipped binary — into
  `build-cache/target/release/`, which is exactly where the exploring
  sessions found the binaries to bind-mount instead. Nothing in a run
  said so: the report recorded the two image digests and nothing about
  the payload inside them, so a stale lane pin, an image-cache reuse
  path, and a ship-map gap all looked identical from the evidence.
- `qa_lane/ship.json` is now the lane's recorded shipped-binary
  contract: the compile groups the bounded builder runs in `/src` with
  the binaries each produces, the two report-fixed images with their
  entrypoints and the extra binaries beside them, and the host-side
  tools that stay out of both images. `qa_lane/ship.py` reads and
  shape-checks it, and `_build_images` derives its cargo chain, each
  image's payload, and the presence assertions from it instead of
  keeping its own lists — so a shipped binary cannot be added without a
  compile target (`no recorded compile target produces …`), a compile
  that produces no one of an image's binaries fails the run by name
  (`build produced no …`, unchanged), and a staging that leaves one out
  of the generated context fails the run by name too
  (`<image> image stages no …`) because the assertion reads the staged
  directory and its Dockerfile back against the contract rather than
  trusting the copy loop that wrote them. The staged payload rides the
  run's timeline as `image-staged`, so the evidence of what each image
  carried sits beside the digests the report persists. The cargo chain
  is the literal it replaces, extended by #1418's control tool: the
  `dcs-sim-bus` group gains `--bin dcs-sim-bus-ctl` and the controller
  payload gains that tool beside `dcs-sim-bus-device`, so the two
  digests, the entrypoints, and the host-side `dcs-ctl` seam are
  unchanged while one more shipped binary is built, staged, and
  asserted like the rest.
- The contract is data rather than code because it ships inside the
  revision's own `git archive` at `src/qa_lane/ship.json`. Every build —
  assessment, exploration, and fix-verification alike, since all three
  reach `_build_images` — now compares the payload its deployed copy
  stages against the one the tested revision records and refuses the run
  before compiling anything when the pinned copy is behind
  (`the deployed qa_lane copy predates the shipped-binary contract the
  revision under test records: controller ships no dcs-sim-bus-device`),
  which is the stale-pin case the three runs hit and the check could
  not be written for inside the copy that is behind. A revision
  predating the document records none and is left alone.
- `python3 -m qa_lane ship [<extracted-src-dir>]` is the deployment
  step's own check: it prints the deployed copy's contract and runs the
  same comparison against an extracted revision, so upgrading the
  pinned copy under `/srv/homelab/dcs-hwtest/` is verified before a run
  spends an attempt on entrypoint-only images. Lane verification is the
  next rig run's sim-bus and interposer legs staging the revision's own
  binaries with no bind-mount recovery, which this change makes the
  recorded contract's precondition rather than a hope about the pinned
  copy.

### Landed 2026-10-03 (usurped foreign-claim reclaim leg, #1417)

- The usurped verdict and foreign-claim reclaim contract — the
  per-revision lane evidence for the fix #1410 pins (decision 108's
  keyed-pair self-service recovery, WW-FND-002's redundant failover
  integrity) — is exercised on the lane's keyed probe pair by scenario
  leg `2498_usurped_foreign_claim`. A `--pair-token` pair whose field
  an unkeyed writer takes must tell a foreign held claim from true
  ownerlessness: the field's own arbitration names the standing
  writer's declared monitor, the keyed monitoring surface pulls that
  endpoint under a fresh `?prove=` nonce, and a live endpoint that
  cannot prove the line's key convicts the claim as `usurped` — not
  the plain `orphaned` — because the two verdicts want opposite
  answers from `POST /promote`: the conditional orphan grant refuses
  a live incumbent, while the usurped diagnosis arms the
  unconditional claim that takes the field back and journals
  `foreign_claim_preempted` naming the endpoint it was taken from.
  The pair-fault surface carries the new verdict as `standby_usurped`
  under `PAIR_FAULT_KINDS_VERSION` 4.
- The live usurper is a real unkeyed driven attachment —
  `start_driven` with `keyed=False`, a launch posture the lever
  gains for this leg — converged on the keyed owner through the
  public, unkey-gated checkpoint pulls its tokenless shape still
  allows, then promoted through the same unconditionally preempting
  `claim_writer` the foreign-claim legs stage, paced through
  `POST /scan` while the pair's own free-running scans fence, demote,
  diagnose, and reclaim. The honest-absence halves stage the shapes
  no launched controller can raise — a claim declaring no monitor at
  all, and one declaring a routable address nothing serves — through
  the runner's new raw field-attachment seam: `hold_field_claim`
  keeps a labeled bridge container's `claim_writer` attachment open
  so the claim's holder set stays occupied exactly as a live foreign
  writer's would, `drop_field_claim` ends it holderless, and
  `field_request`'s `probe_writer` reads the standing claim's
  attributed verdict — the probe pair's plant is bridge-placed, so
  no host socket reaches the claim surface these ops ride. The
  unkeyed-run half relaunches a probe member without `--pair-token`
  over a held claim declaring the usurper's *live* monitor — the
  same claim the keyed sibling convicts.
- The claims: the surviving keyed peers report `usurped` beside
  `field_claim: held` — never `orphaned` — and the pair-fault
  surface names `standby_usurped`; the demoted owner's
  `POST /promote` routes to the unconditional claim and takes the
  field back while the usurper lives, journaling
  `foreign_claim_preempted` naming the usurper's declared endpoint
  in both the served journal and the durable `--journal-file`; the
  fenced usurper demotes but stays serving — the reclaim landed
  while it lived; and the sibling's verdict clears to tracking
  under the keyed writer, so the pair never reads its own member as
  foreign. Each absence half keeps `orphaned` across the held
  window and meets the conditional grant's named
  `field_claim_failed` refusal while the foreign holder lives — no
  preemption armed on evidence the pair key never produced — and
  the holderless claim hands the field back through the ordinary
  conditional grant.
- Named diagnostics are `usurped-foreign-claim-failed` (a contract
  miss: an `orphaned` verdict beside the live foreign writer, a
  refused or conditional-gated reclaim, a missing or misattributed
  `foreign_claim_preempted`, a surviving active usurper, a latched
  `usurped` under the keyed writer, a preemption armed on the
  monitor-less, dead-declared, or unkeyed shapes, or a holderless
  claim the ordinary grant never took) and
  `usurped-foreign-claim-nondeterministic` (a dropped probe, a
  refused staging claim, an unsettled restore, two passes'
  digests diverging), with the self-check's
  `usurped-foreign-claim-unchecked` covering every planted
  negative. Two consecutive passes produce identical digests; a run
  carrying no keyed probe pair, no driven/unkeyed launch lever, no
  raw claim seam, or no settled owner-plus-tracker layout reports
  inconclusive.

### Landed 2026-10-04 (divergence gate, lifecycle, and wedge-recovery legs, #1476)

- Three scenario legs and one consumer-boundary reference-plant leg
  complete the staged-versus-field divergence evidence the plan's
  failover-integrity rows call for: `2330_standby_divergence` (#444),
  `2335_divergence_resolution` (#573), and
  `2340_diverged_field_recovery` (#922) in the lane, and
  `ci/legs/diverged_field_recovery.py` (#923) at the customer
  boundary. The consolidation that carried them (#1476, from #730,
  #922, #923, #444, #573) added no roadmap scope: the product defect
  #730 recorded was already answered by decision 94 (#915) and
  decision 97's fencing-loss reclaim, and these legs grade the
  contracts those decisions rest on.
- The induction seam is the **shared** writer claim — the shape
  `dcs-controller --owner-token` exists for ("pins it when an
  external attachment must share the claim"). An attachment
  `ensure_writer`s the claim under the settled active's pinned token,
  the field answers `claimed_shared`, and the active keeps its
  ownership, its writes, and its `active` role across the whole
  episode. That seam is a *finding* about the contract, not a
  convenience: an attachment's unconditional `claim_writer` preempt
  lands the owner's next field write fenced, the owner demotes in
  place, and every later checkpoint of that seat stamps
  `source_owns_field: false` — which decision 87's `orphaned` verdict
  uses to supersede a `Diverged` verdict outright, and which the
  promote gate admits. So the preempting seam lands the sibling wedge
  (`scenario_unclaimed_rearm`, `scenario_claim_reclaim`), never
  decision 26's staged-versus-field verdict; the shared claim is the
  only claim a third attachment may take while the tracked line keeps
  reporting that it owns the field.
- `2330_standby_divergence` grades the gate whole: the diverged report
  naming the mismatched point with both sides' values, one journaled
  `divergence_detected`, `POST /promote` answering the named
  `not_converged` carrying that report with the active neither fenced
  nor demoted, the reported staged value written back reconverging the
  peer on fresh evidence rather than latching, the promoted peer's own
  claim preempting the attachment's, and the closing demote/promote
  restoring the launch roles. Because the active rewrites its outputs
  every scan, a single poke opens only a one-scan window; the leg
  holds the field off-image with repeated pokes under the shared claim
  until the comparison convicts it, which is what makes the induction
  deterministic on a continuously-scanning rig.
- `2335_divergence_resolution` grades the lifecycle's other two legs:
  the blocked clear — an error fault injected on the compared field
  points through the shipped tool's `fault` subcommand leaves the
  standing verdict across the applies whose field reads cannot complete,
  with no `divergence_resolved` journaled and the gate still closed
  (decision 26's #541 positive-evidence rule) — and the journaled
  resolution, where the first fully-read matching comparison returns
  the peer to `tracking` with exactly one `divergence_resolved`
  carrying every compared point with both sides' values and reopens
  the promote gate.
- `2340_diverged_field_recovery` and the reference-plant leg stage the
  #730 wedge itself — an interposer's `claim_writer` preempt, one field
  `Out` write off the staged value, `release_writer` — and run
  decision 94's recorded remedy. Two contract facts bound what either
  can observe, and both legs assert rather than assume them: the
  `Diverged` verdict is reachable only while the tracked line's source
  still stamps ownership, so the driven consumer leg orders its scans
  into that one window and the rig leg grades whichever named
  un-converged verdict the contract serves (`diverged` with the point
  and both values where the comparison convicted, `orphaned` where the
  demoted source's stamp superseded it); and decision 97's
  fencing-loss reclaim takes a free field back on the ex-owner's next
  scan, so the unclaimed-field surface is observable only with that
  actor gone — the leg's first `stop_controller` is the recorded
  remedy's own first half, and the `start_controller` on the same seat
  is its second: a fresh active whose conditional startup grant takes
  the free field, whose declared image overwrites the un-commanded
  actuation, and after which the survivor's next same-tick comparison
  clears it to `tracking` in place. Both legs then audit the record
  that reconvergence owes: the transition into the served un-converged
  verdict journals once — exactly once in the consumer leg, whose scan
  order puts the comparison convicting it inside the one window where
  it is reachable, and at most once in the rig leg, which grades
  whichever named verdict the contract serves — and a standing
  `Diverged` verdict resolves exactly once, carrying every compared
  field `Out` point with both sides' values. Zero resolutions is the
  honest count where the demoted source's stamp superseded the
  divergence and the survivor reconverged through the ordinary pull,
  and a flap is refused either way. The pair's launch roles restore
  either way.
  The gate is graded on *every* peer while the interposer's claim
  still stands — the reproduction's "every promote refused" clause:
  the convergence gate answers a `diverged` peer with
  `not_converged` carrying the report it declined on, and the field's
  own arbitration answers a peer whose verdict is promotable but whose
  conditional orphan claim a live foreign claim refuses (decision 91).
  Neither hands the field off and neither has an override. Nothing is
  posted after the release: a free field is the state the remedy
  needs, and a promotable survivor would take it through the ordinary
  promote — decision 94's own recorded lighter recovery boundary —
  instead of the relaunch being graded. The wedge's duration and the
  field state across it land in the leg's evidence beside the served
  verdicts, the measure the finding's reproduction reported as minutes
  of frozen un-commanded actuation.
- Named diagnostics: `standby-divergence-failed` /
  `-nondeterministic`, `divergence-resolution-failed` /
  `-nondeterministic`, `wedge-recovery-failed` /
  `-nondeterministic` in the lane, and
  `wedge-recovery-failed` / `wedge-recovery-nondeterministic` /
  `diverged-field-recovery-unchecked` in the consumer stage. Two
  consecutive lane passes must produce identical evidence digests; the
  consumer leg's two doctored cases (`expect-promote`, which wants the
  wedge's promote admitted, and `skip-relaunch`, which never relaunches
  the wedged field owner) must each fail naming their evidence.

### Landed 2026-10-03 (stale-budget cadence-domain leg, #1411)

- The declared freshness budget's measurement domain — the per-revision
  lane evidence for the contract #1411's fix declares (decision 45's
  `stale_after_ticks`, WW-OPS-003's stale-data surface, the tick domains
  CONTEXT.md separates) — is exercised on the lane's own scratch fields
  by scenario leg `2499_stale_budget_cadence`. A declared budget is
  measured in the *reader's* run ticks, so a peer scanning faster than
  the field owner's step cadence reads the identical driver report on
  every scan in between; before the fix a budget below the owner's step
  period paced a `quality_changed` pair per field step on that peer — the
  recorded ~118 stale/good flap pairs in ~6 s and ~40 journal records per
  second — while the field stayed healthy. The filed legs so far pinned
  the symmetric case only (#434's rig leg, #704's consumer mirror),
  where the two domains happen to agree.
- Two arms, two scratch fields, one pass, staged through the
  per-container `scan_ms` lever `2490_claim_skew_bound` introduced, so
  the cadence is the only variable between them. The control arm paces
  its reader exactly like the field owner, stops that owner (the only
  way the shared plant's stepping stops under the single-writer claim),
  and requires the declared-budget input to age to `Uncertain(Stale)`
  while the undeclared input beside it keeps Good — the declared
  staleness behavior the widening must leave alone, and the leg's
  measurement of the declared budget off the rig. The subject arm
  launches the same reader shape at a tenth of the owner's pace on a
  fresh field and, once that reader has watched the field publish twice
  (the two-gap arrival evidence the contract's own cold-start limit
  names), judges its window: the declared-budget input Good on every
  served sample.
- The pace witness is what makes the cadence-domain claim measurable on
  the served surface: the reader cannot see the driver report's own
  stamp, which is minted in the field owner's step domain, but it sees
  every value that step moves — so the undeclared level input doubles as
  the witness, and the reader-tick gaps between its served changes are
  the field's demonstrated step period in the reader's own domain. The
  claims are the contract's two directions: a stale presentation inside
  the patience floor (the greater of the measured declared budget and the
  last two witnessed gaps) is the recorded defect — the reader called a
  publication the field had not sent yet stale — and a witnessed silence
  longer than that floor and than any gap the field itself demonstrated,
  with the budgeted input still Good, is the opposite doctoring: the leg
  asserting freshness over a genuine starvation. The durable half rides
  the reader's own `--journal-file`: only the transitions the contract
  names — the cold-start pair before the arrival evidence exists, and
  nothing after it.
- Named diagnostics are `stale-budget-cadence-failed` (a frozen field
  whose declared-budget input never presented stale, a verdict leaked
  onto an undeclared point, a declared staleness transition that reached
  no durable record, a stale presentation inside the declared patience,
  journaled quality_changed traffic past the arrival evidence, freshness
  asserted over a starvation longer than the declared budget, or the
  control reader's budgeted input stale before any induction) and
  `stale-budget-cadence-nondeterministic` (refused staging calls, an
  owner that never claimed, a reader that never converged or never paced
  as declared, a freeze that never took effect, a declared budget or
  arrival period the rig never showed, a starved watch, an empty judged
  window, an unreadable journal, a degradation the freshness contract
  does not name, a moved pair, a rig the sweep did not restore), with
  the self-check's `stale-budget-cadence-unchecked` covering every
  planted negative. Two consecutive passes produce identical digests; a
  run carrying no born staging lever, no per-seat journal file, or no
  settled pair reports inconclusive.

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
