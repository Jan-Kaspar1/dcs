# Release record: v0.5.0

The fifth release of the DCS platform, carrying the post-`v0.4.0`
correction of the consumer contract — the redundant-pair arbitration
and recovery fixes whose consumer-boundary mirror legs report
`inconclusive` while the pinned release predates them: the yielded
claim's re-arm, the claim-declared monitor's wildcard normalization,
the stranded tracking-source retry and the journaled orphan
re-target, the failover window's re-arm after a refused armed
self-promotion, and the bounded liveness answer's published-mirror
move — cut under the procedure in `docs/release-contract.md`
(decision 80). Fields marked *pending* are filled mechanically by the
release procedure when the supervisor cuts the tag and publishes the
images; the checked-in schemas are the current emission, byte-pinned
by the drift tests so they cannot diverge from the code before the
tag is cut.

| Field | Value |
|---|---|
| Tag | `v0.5.0` — *pending*: the release tag is placed on the recorded commit when the release is cut |
| Commit | *pending* — the tagged `main` commit carrying this record, the revision this record's schemas are emitted at |
| Crate versions | `0.5.0` for every crate in the release set — one workspace version covers `dcs-build`, `dcs-core`, `dcs-model` (and the `dcs-model` / `dcs-controller` binaries built from it), `dcs-monitor` (shipping `dcs-ctl` and `dcs-alarm-report`), `dcs-plant` (`dcs-plant-server`), and `dcs-sim-net` (`dcs-plant-ctl`); the `[workspace.package]` bump lands with the cut |
| Plant-model JSON Schema | `plant-model.schema.json` beside this record — `dcs-model schema` emitted at the recorded commit, pinned byte-for-byte with its sha256 by the schema drift test in `crates/dcs-model/tests/schema.rs`. The emission moved twice since `v0.3.0`'s recorded commit: #907's `record` io_point field — the durable-history decision's declared recording duty — added its `recording-duty` definition, and #548's declared-unit metadata added optional `unit` declarations on io points, ports, and signals plus `parameter_units` beside `parameters` — additive optional fields a `v0.4.0` document never carries |
| Plant-model schema sha256 | `91f175fa58b2f45bd44c1ce2a4c138ba9c583706f4bc4ccbcd5cc171540b2f7c` |
| Served-registry JSON Schema | `block-interfaces.schema.json` beside this record — `dcs-model interface-schema` emitted at the recorded commit, pinned byte-for-byte with its sha256 by the drift test in `crates/dcs-model/tests/interface_schema.rs`. Unchanged since `v0.3.0`'s recorded commit |
| Served-registry schema sha256 | `ddc00496814a4e8cd0d6ec8a5d9fbb95e83f518dcd927b17a4802f13ac84013a` |
| Dynamics-document JSON Schema | `dynamics.schema.json` beside this record — `dcs-plant-server --dynamics-schema` emitted at the recorded commit, pinned byte-for-byte with its sha256 by the drift test in `crates/dcs-plant/tests/dynamics_schema.rs`. Unchanged since `v0.3.0`'s recorded commit |
| Dynamics-document schema sha256 | `98fb4a4298c5974b8ab0adf1374cd6d53b0c2cfbd2e24a31c874090235b46f02` |
| Deployment-manifest JSON Schema | `deploy-manifest.schema.json` beside this record — `dcs-model deploy-schema` emitted at the recorded commit, pinned byte-for-byte with its sha256 by the drift test in `crates/dcs-model/tests/deploy_schema.rs`. Moved once since `v0.3.0`'s recorded commit: #1282's additive-optional `history_file` per-controller declaration — decision 102's durable process-history mount; a manifest predating the field validates unchanged |
| Deployment-manifest schema sha256 | `980430ca8725af997a7b5063f00d2a9663fe4619ca00a542917bde24f270cfa9` |
| `dcs-controller` image digest | *pending* — `dcs-controller@sha256:<digest>`, the image `docker build` produces from `Dockerfile` at the tag |
| `dcs-plant-server` image digest | *pending* — `dcs-plant-server@sha256:<digest>`, the image `docker build -f Dockerfile.plant` produces at the tag |

## Compatibility notes

`v0.5.0` is the minor bump the post-`v0.4.0` correction of the
consumer contract takes under the release versioning policy. The
determination this record owes: the tranche's changes are corrective
and additive — they hold `MODEL_VERSION` and the checkpoint format
set — and the bump exists so a `version = "0.4"` requirement cannot
silently resolve the corrected release line, not because any
supported item broke.

What the tranche changed for consumers — the contract corrections
since `v0.4.0` the consumer-boundary mirror legs gate on:

- The yielded claim's lifecycle end (#1123): a demoted owner's
  lingering claim stands marked `yielded` — fencing the field,
  preemptable by a successor's conditional grant — until the claim
  itself ends; a same-owner live-controller re-grant now clears the
  mark, so a re-armed incumbent's claim reads as the live incumbent
  it is rather than staying preemptable behind its own standing
  attachment.
- The claim-declared monitor's wildcard normalization (#1135,
  #1166): a claimant bound to the wildcard — every
  `--listen 0.0.0.0` container, the documented deployment — declares
  its *bind* address, which a fenced peer would dial as its own
  loopback, so the field-arbitrated rendezvous could never fire on
  the deployment shape it exists to serve. The plant server now
  substitutes the claiming connection's proven source for an
  unspecified declared IP — keeping the declared port, the same
  substitute a `?peer=` wildcard announce resolves to — on every
  claim op that carries the field, so the stored monitor is never
  unspecified; and the consumer side never dials an unspecified
  declaration — no pull, no retry window — where a pre-normalization
  verdict still carries one.
- The learned tracking pins' liveness bound (#1136): the orphan
  probe's `resolved` owner and the verified `adopted` source outrank
  the configured `--standby` source only while they answer —
  `PIN_MISS_BUDGET` consecutive produced-nothing pulls releases a
  learned pin and re-runs the resolution probe in the same cycle, so
  a dead successor's pin can no longer shadow the declared slot the
  way a never-retried configured source let it. A live successor —
  the healthy configured source included — re-earns the pin on the
  probe's own verification while a dead one drops the pulls back
  onto the declared source, which the tracking contract retries
  without a bound.
- The journaled orphan re-target (#1137):
  `resolve_tracking_source`'s re-pin through the `resolved` slot
  journals `tracking_source_adopted` exactly like the announced- and
  claimed-source adoptions it outranks — the resolved slot carries
  the same pull-target authority, so the durable audit records where
  an orphaned peer moved its pulls; a promotion landing mid-probe
  suppresses the pin, the owning run already having answered where
  its pulls go.
- The failover window's re-arm (#1165): a refused armed
  self-promotion no longer permanently closes the failover window —
  the budget-th miss remains the boundary, an attempt on a voided
  proof reporting its named refusal rather than promoting, but past
  it the boundary stays open exactly while landed applies keep
  re-proving the run: each due cycle retries the gate a genuinely
  dead owner needs, while an evidence-free miss run past the budget
  voids the proof for staleness. `GET /role`'s `failover` evidence
  reporting `converged: true` beside `misses` past `budget` is that
  armed gate still live, and the durable journal gains
  `promotion_refused` — one entry per distinct refusal cause a
  continuous refused streak produces — so the attempt the gate made
  is recorded where no role transition landed.
- The bounded liveness answer's published-mirror move (#1147):
  `GET /health` and `GET /role` answer from the store's published
  liveness mirror — refreshed wherever the control-plane lock
  changes them — rather than queueing behind the executor lock a
  scan wedged in field I/O can hold, so the bounded answer the
  `v0.4.0` liveness contract declared cannot stall on the very wedge
  it exists to expose.
- The consumer-boundary mirror legs gained their named diagnostics
  and land on the reference plant's clean CI — the stranded-standby
  re-join (`stranded-rejoin`), the health-gated orchestrated restart
  (`orchestrated-restart`), the resume-settle-once crossing
  (`resume-settle-once`), and the wedged-field bounded-liveness leg
  (`bounded-liveness`) — each reporting `inconclusive`, never its
  failure, while the pinned release predates the contract it
  exercises; this release is the pin that lets them report.

The determination:

- `MODEL_VERSION` holds at `1`; `PlantModel::load` still accepts
  exactly that version. A `version: 1` document written against
  `v0.4.0` validates unchanged under `v0.5.0` tooling. The
  plant-model schema emission moved twice: #907's `record` io_point
  field added the `recording-duty` definition and #548's `unit` /
  `parameter_units` declarations — additive optional grammar both, so
  a `v0.4.0` document still validates unchanged.
- The checkpoint format set holds: `Checkpoint.format_version` still
  negotiates against `SUPPORTED_FORMAT_VERSIONS` (`{0, 1}`; absent
  reads as `0`). Checkpoints cross the bump under the same
  per-connection negotiation and fingerprint gate — no checkpoint
  migration is owed.
- The tranche corrected the served and arbitration contracts —
  claim arbitration, tracking-source recovery, the failover gate's
  evidence, and the liveness reads — without removing or re-shaping
  any supported engineering surface, so a consumer's composition
  code compiles unchanged on the repin. The migration expectation is
  the repin itself — the reference plant's `ci/check.sh` `upgrade`
  stage proves the `v0.4.0` → `v0.5.0` crossing byte-identically;
  non-Rust consumers pin the same four schema artifacts `v0.4.0`
  recorded, their emissions unchanged.
- The `dcs-build` `station`, `dosing`, `ijmuiden`, and `ethercat`
  modules remain platform-owned reference compositions outside the
  compatibility policy (decision 81); a consumer composes from the
  supported primitives or copies a pattern.

## Consumer pins

- Crates: `dcs-build = { git = "<repo>", tag = "v0.5.0" }` — or
  `rev = "<commit>"` for the identical immutable commit, the recorded
  commit above once it is filled; `dcs-core` and `dcs-model` under the
  same pin.
- Tooling: `cargo install --git <repo> --tag v0.5.0 dcs-model
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

- `[workspace.package]` version `0.5.0` and the regenerated workspace
  `Cargo.lock` — one crate version across the release set.
- The reference plant's repin: `Cargo.toml`'s `tag = "v0.5.0"`,
  `ci/check.sh`'s `DCS_REV` default `v0.5.0` and `DCS_UPGRADE_REV`
  default at the `v0.4.0` recorded rev — the `upgrade` stage
  materializes the tree at the `v0.4.0` pin and repins to `v0.5.0`,
  proving the named crossing — `deploy/manifest.json`'s
  `dcs_release: "v0.5.0"` and `v0.5.0` image tags, and
  `deploy/compose.yaml`'s `x-dcs-release` and images.
- The check's schema non-drift legs continue to cover all four
  recorded artifacts — `deploy-schema` and `--dynamics-schema`
  beside `schema`/`interface-schema`.

The remaining items are the supervisor's publication operations:

- Cut `v0.5.0` on the `main` commit carrying this record once
  `rust-proofs` is green on that exact commit; fill the Commit field
  with the tagged sha.
- Build and publish the `dcs-controller` and `dcs-plant-server`
  images; fill the two digest fields above.
- Regenerate `reference-plant/Cargo.lock` against the published tag
  (`cargo update` in the consumer tree, README §7's documented step)
  so the committed lockfile records the `tag = "v0.5.0"` source — the
  lockfile cannot name the tag's target before the tag exists, so the
  repin commit carries the previous resolution and the check's resolve
  leg re-resolves on the first post-tag run.
- Re-run `reference-plant/ci/check.sh` end to end against the
  published artifacts and capture its output as the release's
  consumer evidence; the upgrade stage's doctored-version refusals
  must still report their named diagnostics.
