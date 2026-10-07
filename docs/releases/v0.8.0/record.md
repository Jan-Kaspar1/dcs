# Release record: v0.8.0

The eighth release of the DCS platform, carrying the post-`v0.7.0`
correction of the consumer contract — the contract fixes whose
consumer-boundary mirror legs report `inconclusive` while the pinned
release predates them: the remote attachment's bounded contact backoff
under a frozen or blackholed field, the convergence-gated
fencing-loss reclaim, the self-address tracking-source refusal, the
sim-bus claim family's conditional grant and claim-introspection
surface with the point-wise driver's re-attach and the cyclic
exchange's claim-loss demotion, the deferred startup-claim refusal's
no-declared-peer disposition, the announced-source checkpoint
verification, the announced/owner skew bound, and the checkpoint
puller's transient-miss recovery — cut under the procedure in
`docs/release-contract.md` (decision 80). Fields marked *pending*
are filled mechanically by the release procedure when the supervisor
cuts the tag and publishes the images; the checked-in schemas are the
current emission, byte-pinned by the drift tests so they cannot
diverge from the code before the tag is cut.

| Field | Value |
|---|---|
| Tag | `v0.8.0` — *pending*: the release tag is placed on the recorded commit when the release is cut |
| Commit | *pending* — the tagged `main` commit carrying this record, the revision this record's schemas are emitted at |
| Crate versions | `0.8.0` for every crate in the release set — one workspace version covers `dcs-build`, `dcs-core`, `dcs-model` (and the `dcs-model` / `dcs-controller` binaries built from it), `dcs-monitor` (shipping `dcs-ctl` and `dcs-alarm-report`), `dcs-plant` (`dcs-plant-server`), and `dcs-sim-net` (`dcs-plant-ctl`); the `[workspace.package]` bump lands with the cut |
| Plant-model JSON Schema | `plant-model.schema.json` beside this record — `dcs-model schema` emitted at the recorded commit, pinned byte-for-byte with its sha256 by the schema drift test in `crates/dcs-model/tests/schema.rs`. The emission moved once this tranche: #548's declared-unit metadata — optional `unit` declarations on io points, ports, and signals plus `parameter_units` beside a component's `parameters` — additive optional fields a `v0.7.0` document never carries |
| Plant-model schema sha256 | `ad3550f3d30ee9b0bbe16744aa92e51f1f95cf7d9c2a35381c945575d4361b1c` |
| Served-registry JSON Schema | `block-interfaces.schema.json` beside this record — `dcs-model interface-schema` emitted at the recorded commit, pinned byte-for-byte with its sha256 by the drift test in `crates/dcs-model/tests/interface_schema.rs`. Unchanged since `v0.3.0`'s recorded commit |
| Served-registry schema sha256 | `ddc00496814a4e8cd0d6ec8a5d9fbb95e83f518dcd927b17a4802f13ac84013a` |
| Dynamics-document JSON Schema | `dynamics.schema.json` beside this record — `dcs-plant-server --dynamics-schema` emitted at the recorded commit, pinned byte-for-byte with its sha256 by the drift test in `crates/dcs-plant/tests/dynamics_schema.rs`. Unchanged since `v0.3.0`'s recorded commit |
| Dynamics-document schema sha256 | `98fb4a4298c5974b8ab0adf1374cd6d53b0c2cfbd2e24a31c874090235b46f02` |
| Deployment-manifest JSON Schema | `deploy-manifest.schema.json` beside this record — `dcs-model deploy-schema` emitted at the recorded commit, pinned byte-for-byte with its sha256 by the drift test in `crates/dcs-model/tests/deploy_schema.rs`. Byte-identical to `v0.7.0`'s recorded artifact — the emission last moved with #1282's additive-optional `history_file` per-controller declaration, the `v0.6.0` tranche's carry |
| Deployment-manifest schema sha256 | `980430ca8725af997a7b5063f00d2a9663fe4619ca00a542917bde24f270cfa9` |
| `dcs-controller` image digest | *pending* — `dcs-controller@sha256:<digest>`, the image `docker build` produces from `Dockerfile` at the tag |
| `dcs-plant-server` image digest | *pending* — `dcs-plant-server@sha256:<digest>`, the image `docker build -f Dockerfile.plant` produces at the tag |

## Compatibility notes

`v0.8.0` is the minor bump the post-`v0.7.0` correction of the
consumer contract takes under the release versioning policy. The
determination this record owes: the tranche's changes are corrective
and additive — they hold `MODEL_VERSION` and the checkpoint format
set — and the bump exists so a `version = "0.7"` requirement cannot
silently resolve the corrected release line, not because any
supported item broke.

What the tranche changed for consumers — the contract corrections
since `v0.7.0` the consumer-boundary mirror legs gate on:

- The remote attachment's bounded contact backoff under a frozen
  field (#1324, #1303): a `--remote` attachment whose endpoint
  completes the handshake but never answers — a `docker pause`-frozen
  or blackholed plant — charged every request the full request
  timeout: `RemoteDriver`'s re-attach backoff armed only on a connect
  failure, so a pending born-active's scan serialized one stall per
  point into a minutes-long cycle behind the executor lock, starving
  every lock-taking endpoint, and an ownerless attachment — the
  pending born-active's `connect_deferred` shape — never armed the
  window at all, its re-attach skipping the `ensure_writer` exchange
  the owner's backoff came from. A failed exchange now counts as the
  window's contact attempt for every attachment shape: a dead
  endpoint costs one refused connect per `REATTACH_INTERVAL` and an
  answering-but-silent one costs one timed-out exchange per interval,
  so a scan's burst of probes and point reads stalls once near the
  request timeout and every in-window access fails fast.
- The convergence-gated fencing-loss reclaim (#1317, decision 105):
  decision 97's arm — `was_owner && !yielded && (fencing_lost ||
  orphaned)` — issued the bound `reclaim_writer` ask on that evidence
  alone, and the grant's field-side rule preempts any different-owner
  *holderless* claim. The field's liveness model cannot tell a dead
  owner's holderless claim from a live incumbent's during a transport
  freeze, so a fenced ex-owner that never tracked could preempt a
  merely transport-frozen incumbent and roll back its ownership epoch
  on ordering alone. The arm still stands on the loss mark or the
  orphaned pull, but the ask now issues only where this scan's own
  claim probe answered `unclaimed` — the released-preemption wedge
  the reclaim exists to close — or while `Peer::converged` holds, the
  standing promotable-verdict proof `self_promote` reads: an
  unconverged ex-owner's probe never issues against a standing claim,
  held or holderless, while a converged peer keeps the full bound
  grant. `reclaim_writer`'s grant semantics, the mark lifecycle, and
  the `field_claim_observed` attribution are unchanged.
- The self-address `--standby`/`--peer` refusal (#1340): a tracking
  source naming the instance's own `--listen` socket passed every
  check — every pull returned the run's own checkpoint, which a
  standby's document always stamps `source_owns_field: false`, so
  each apply scored a heartbeat miss: an unarmed run reported
  `standby` covering no peer forever, and an armed one manufactured
  a failover against itself at every budget. Argument parse now
  resolves both spellings — an equal socket, a wildcard listen
  reached through any of this host's addresses, and a wildcard
  target the loopback stack answers — and refuses a self-addressed
  `--standby` or `--peer` as a usage error before the run exists; a
  target that does not resolve stays the documented degraded source,
  the pull-miss contract rather than a startup error, since it
  cannot be shown to be this instance.
- The sim-bus claim family's conditional grant and introspection
  surface (#1350, #1354): the register protocol's `claim_writer` was
  unconditional — a born-active launch over a `sim-bus`/`sim-cyclic`
  field preempted a live claim holder, wedging the incumbent and
  orphaning the redundant line — and carried none of the
  claim-status surface the plant protocol arbitrates on. The wire now
  carries `claim_writer_unless_held` (op `0x0b`), decision 89's
  conditional startup grant on the register protocol — granted while
  the field stands unclaimed or the standing claim already names the
  token, refused `BusError::Fenced` while a different owner's claim
  stands, a standing claim on this protocol always having live
  holders, so the refused ask is exactly the live-incumbent verdict
  the born-active startup contract refuses on — beside `ensure_writer`
  (the re-attached owner's re-arm, also the fencing-loss reclaim's
  grant) and `probe_writer` (the read-only claim-status observation).
  Every claim may declare the claimant's monitor endpoint — wildcard
  declarations resolving to the claiming connection's proven source —
  and every `fenced` verdict names the standing claim's owner token
  beside that declared monitor, so decision 97's attribution and
  decision 101's field-arbitrated rendezvous reach the controller
  over either transport. The new ops and fields are additive on the
  wire: every earlier request spelling and verdict shape decodes
  unchanged.
- The point-wise sim-bus `BusDriver`'s re-attach and the cyclic
  claim-loss demotion (#1351, #1352): the point-wise driver never
  re-attached — a device restart or a ~2 s stall permanently severed
  every attached controller — and a `sim-cyclic` driver's per-point
  `write` only stages, so the field's fencing verdict arrived at the
  `exchange` unrecorded and a link flap's auto-released claim left a
  zombie `active` re-presenting its staged image. `BusDriver` now
  re-attaches lazily on the same `REATTACH_INTERVAL` bound and
  re-arms a recorded writer claim through the conditional
  `ensure_writer` on re-attach — refusing to preempt a different
  owner that claimed during the outage — so its `release_writer`
  carries the demotion contract: a demoted attachment forgets the
  token it would otherwise re-assert. `Executor::exchange_image`
  records a refused `IoError::Fenced` exchange into the same
  `fenced_write` mark a point write sets, the demotion path
  unchanged, and `CyclicBusDriver::drop_pending_outputs` drops the
  staged-but-unpublished output image so the demoted run's exchanges
  go census-only.
- The deferred startup-claim refusal's no-declared-peer disposition
  (#1314): the `v0.7.0` tranche's latched-refusal contract settled a
  deferred `Ok(false)` under the identical disposition the
  activation-time answer takes, but the pairless launch's exit
  message read "rejoin as a standby instead" — the role the run
  *would* land in, not the flag an operator types — so a stranded
  run's only last words never carried the relaunch the remedy
  actually is. The shared refusal (`startup_claim_refused`, so the
  boot-time and deferred verdicts cannot drift) and the shell's
  undeclared arm now name `--standby ADDRESS` explicitly — the
  `FieldClaimFailed` detail itself tells the operator to "relaunch
  with `--standby ADDRESS` to rejoin as the incumbent's tracking
  standby", and the pairless shell appends that no `--peer` was
  declared and the same relaunch tracks the field's live owner.
- The announced-source checkpoint verification (#867): an unkeyed
  announced-source verify still adopted a standby-shaped forged
  checkpoint — an endpoint that merely replayed or fabricated the
  line's checkpoints could arm a demotion and feed the demoted peer
  forged state. The keyed tracking contract every real redundant
  deployment declares is now the only path an announced endpoint can
  arm: an unkeyed run cannot authenticate an announced endpoint, so
  an announced-only demotion refuses `no_tracking_source`, and under
  `--pair-token` the demotion's verify pull plus every checkpoint the
  adopted source serves must carry the token-keyed `line_proof`.
- The announced/owner skew bound (#1336): the line-membership bound a
  demoted or orphaned peer verified a pulled document against
  compared the document's declared stream position against the
  detached prober's *own* paced position — but run ticks are not
  synchronized to the line and a detached prober's positions advance
  at its own scan cadence, so the gap measured pace asymmetry, never
  line membership: a slow-scanning demoted ex-owner `Ahead`-refused a
  faster successor forever, stranding `standby`/`unsynchronized`
  while the successor stayed healthy. `MAX_ANNOUNCED_AHEAD` and its
  `stream_position` resolution are gone; both verifies keep only the
  document's own consistency — a `stream_tick` declared ahead of the
  `tick` it rides refuses `ImpossibleLead`, nonsense no honest run
  serves — a forged stream's conviction staying where it can be
  carried, the keyed `line_proof` and the command audit at the call
  sites.
- The checkpoint puller's transient-miss recovery (#1315): a
  checkpoint-pull worker tracking a pending born-active latched a
  permanent `EAGAIN` for process life — a completed document was
  discarded as stale once older than `CHECKPOINT_PULL_TIMEOUT`, a
  fixed bound standing in for the puller's cadence, so any scan
  period past the fetch bound or any single overrun made every
  document too old to apply, and the discard then replayed the one
  refused read a standby's first pull into a pending source produced,
  on every cycle until a restart reset the record. A document's wait
  is now measured against the puller's own cadence
  (`CheckpointPuller::staleness` — twice the longest gap between two
  of its polls, never below the pull bound, reset once a completed
  document has been acted on), a landed document clears the failure
  preceding it, and a fetch that has outrun its own bound reports the
  stall it is rather than replaying an earlier error. The pull path's
  stages are observable on the same seam — `poll_staged` answers a
  `PullMiss` (`InFlight`, `Stalled`, `Stale`, `Unresolved`,
  `Refused`, `WorkerGone`) whose detail is what the served `degraded`
  sync names — and a fetch wedged past `CHECKPOINT_PULL_STALL` costs
  its worker rather than the run's convergence: the puller binds a
  fresh fetch worker (`MAX_RETIRED_PULL_WORKERS`-bounded), and a
  field-owning cycle drops the puller with the ownership, so no
  checkpoint fetched before a promotion can land after it.
- The consumer-boundary mirror legs for the tranche's contract fixes
  gate on this pin — the deferred startup-claim refusal's
  exit-nonzero legs (#1307/#1308), the remote born-active's
  foreign-model correspondence legs (#1309/#1310), the ownerless
  remote-attachment backoff legs (#1311/#1312), the
  convergence-gated reclaim legs (#1321/#1322), the blackhole-field
  bounded-serving legs (#1327/#1328), the same-process
  persistence-distinctness leg that has already run (#1337), the
  self-address refusal legs (#1343/#1344), the cross-peer
  single-writer legs (#1347/#1348), the checkpoint-pull recovery
  legs (#1319/#1320), the announced/owner skew legs (#1345/#1346),
  the announced-source verification legs (#1222/#1223, #924/#1111),
  and the sim-bus claim rig legs (#1355–#1357) — each reporting
  `inconclusive`, never its failure, while the pinned release
  predates the contract it exercises; this release is the pin that
  lets them report. The record also re-carries the corrections the
  `v0.7.0` record named — the voluntary-demote released-claim
  hand-back, the same-process persistence-path distinctness refusal
  (#1292), the deferred startup-claim refusal's named dispositions
  (#1301, #1316), and the remote born-active's deferred
  correspondence gate (#1302) — unchanged; the filed legs for
  contract fixes still in flight — the cross-peer single-writer
  persistence refusal (#1341) — each report `inconclusive` until
  their contract lands and a release carries it.

The determination:

- `MODEL_VERSION` holds at `1`; `PlantModel::load` still accepts
  exactly that version. A `version: 1` document written against
  `v0.7.0` validates unchanged under `v0.8.0` tooling; the
  plant-model schema emission moved once this tranche — #548's
  declared-unit grammar is additive optional, so a `v0.7.0` document
  still validates unchanged.
- The checkpoint format set holds: `Checkpoint.format_version` still
  negotiates against `SUPPORTED_FORMAT_VERSIONS` (`{0, 1}`; absent
  reads as `0`). Checkpoints cross the bump under the same
  per-connection negotiation and fingerprint gate — no checkpoint
  migration is owed.
- The tranche corrected the launch, arbitration, and tracking
  contracts — the field attachment's contact backoff, the reclaim's
  convergence gate, the tracking-source self-address refusal, the
  sim-bus claim family's conditional grant and introspection, the
  bus drivers' re-attach and claim-loss surfaces, the deferred
  refusal's named remedy, the announced-source verify's keyed
  enforcement, the skew bound's own-consistency check, and the pull
  path's miss recovery — without removing or re-shaping any supported
  engineering surface, so a consumer's composition code compiles
  unchanged on the repin. The migration expectation is the repin
  itself — the reference plant's `ci/check.sh` `upgrade` stage proves
  the `v0.7.0` → `v0.8.0` crossing byte-identically; non-Rust
  consumers pin the same four schema artifacts `v0.7.0` recorded,
  all four emissions byte-identical.
- The `dcs-build` `station`, `dosing`, `ijmuiden`, and `ethercat`
  modules remain platform-owned reference compositions outside the
  compatibility policy (decision 81); a consumer composes from the
  supported primitives or copies a pattern.

## Consumer pins

- Crates: `dcs-build = { git = "<repo>", tag = "v0.8.0" }` — or
  `rev = "<commit>"` for the identical immutable commit, the recorded
  commit above once it is filled; `dcs-core` and `dcs-model` under the
  same pin.
- Tooling: `cargo install --git <repo> --tag v0.8.0 dcs-model
  dcs-controller dcs-plant dcs-monitor dcs-sim-net` — `dcs-monitor`
  ships `dcs-ctl` and `dcs-alarm-report`, `dcs-sim-net` ships
  `dcs-plant-ctl` — or binaries built from the tag.
- Images: *pending* — `dcs-controller@sha256:<digest>` and
  `dcs-plant-server@sha256:<digest>` once the record's digest fields
  are filled, or `docker build` / `docker build -f Dockerfile.plant`
  at the tag.

## Post-cut checklist

Landed with the commit carrying this record — the publication's
non-registry half:

- The record itself and the four emitted schema artifacts, each
  byte-pinned with its published sha256 by the drift tests, so the
  schema non-drift legs continue to cover all four recorded
  artifacts — `deploy-schema` and `--dynamics-schema` beside
  `schema`/`interface-schema`.

The remaining items are the release procedure's mechanical fills and
the supervisor's publication operations:

- Bump `[workspace.package]` to `0.8.0` and regenerate the workspace
  `Cargo.lock` — one crate version across the release set; the
  Crate versions field above names the version the bump lands.
- Repin the reference plant: `Cargo.toml`'s `tag = "v0.8.0"`,
  `ci/check.sh`'s `DCS_REV` default `v0.8.0` and `DCS_UPGRADE_REV`
  default at the `v0.7.0` recorded rev — the `upgrade` stage
  materializes the tree at the `v0.7.0` pin and repins to `v0.8.0`,
  proving the named crossing — `deploy/manifest.json`'s
  `dcs_release: "v0.8.0"` and `v0.8.0` image tags, and
  `deploy/compose.yaml`'s `x-dcs-release` and images.
- Cut `v0.8.0` on the `main` commit carrying this record once
  `rust-proofs` is green on that exact commit; fill the Commit field
  with the tagged sha.
- Build and publish the `dcs-controller` and `dcs-plant-server`
  images; fill the two digest fields above.
- Regenerate `reference-plant/Cargo.lock` against the published tag
  (`cargo update` in the consumer tree, README §7's documented step)
  so the committed lockfile records the `tag = "v0.8.0"` source — the
  lockfile cannot name the tag's target before the tag exists, so the
  repin commit carries the previous resolution and the check's resolve
  leg re-resolves on the first post-tag run.
- Re-run `reference-plant/ci/check.sh` end to end against the
  published artifacts and capture its output as the release's
  consumer evidence; the upgrade stage's doctored-version refusals
  must still report their named diagnostics.
