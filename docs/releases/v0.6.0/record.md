# Release record: v0.6.0

The sixth release of the DCS platform, carrying the post-`v0.5.0`
correction of the consumer contract — the contract fixes whose
consumer-boundary mirror legs report `inconclusive` while the pinned
release predates them: the configured tracking source's per-pull
rediscovery, the driven-scan batch's per-request bound, the status
line's honest data-source label, the operator page's bounded history
backfill, the born-active's recorded startup-failure dispositions,
and the declared-duty durable process-history store — cut under the
procedure in `docs/release-contract.md` (decision 80). Fields marked
*pending* are filled mechanically by the release procedure when the
supervisor cuts the tag and publishes the images; the checked-in
schemas are the current emission, byte-pinned by the drift tests so
they cannot diverge from the code before the tag is cut.

| Field | Value |
|---|---|
| Tag | `v0.6.0` — *pending*: the release tag is placed on the recorded commit when the release is cut |
| Commit | *pending* — the tagged `main` commit carrying this record, the revision this record's schemas are emitted at |
| Crate versions | `0.6.0` for every crate in the release set — one workspace version covers `dcs-build`, `dcs-core`, `dcs-model` (and the `dcs-model` / `dcs-controller` binaries built from it), `dcs-monitor` (shipping `dcs-ctl` and `dcs-alarm-report`), `dcs-plant` (`dcs-plant-server`), and `dcs-sim-net` (`dcs-plant-ctl`); the `[workspace.package]` bump lands with the cut |
| Plant-model JSON Schema | `plant-model.schema.json` beside this record — `dcs-model schema` emitted at the recorded commit, pinned byte-for-byte with its sha256 by the schema drift test in `crates/dcs-model/tests/schema.rs`. The emission is byte-identical to `v0.5.0`'s recorded artifact — #907's `record` io_point field, the `recording-duty` definition that record names, is the only model-grammar move this tranche carries — and moved once more after this record was written: #548's declared-unit metadata added optional `unit` declarations on io points, ports, and signals plus `parameter_units` beside `parameters`, additive optional grammar a `v0.5.0` document never carries |
| Plant-model schema sha256 | `ad3550f3d30ee9b0bbe16744aa92e51f1f95cf7d9c2a35381c945575d4361b1c` |
| Served-registry JSON Schema | `block-interfaces.schema.json` beside this record — `dcs-model interface-schema` emitted at the recorded commit, pinned byte-for-byte with its sha256 by the drift test in `crates/dcs-model/tests/interface_schema.rs`. Unchanged since `v0.3.0`'s recorded commit |
| Served-registry schema sha256 | `ddc00496814a4e8cd0d6ec8a5d9fbb95e83f518dcd927b17a4802f13ac84013a` |
| Dynamics-document JSON Schema | `dynamics.schema.json` beside this record — `dcs-plant-server --dynamics-schema` emitted at the recorded commit, pinned byte-for-byte with its sha256 by the drift test in `crates/dcs-plant/tests/dynamics_schema.rs`. Unchanged since `v0.3.0`'s recorded commit |
| Dynamics-document schema sha256 | `98fb4a4298c5974b8ab0adf1374cd6d53b0c2cfbd2e24a31c874090235b46f02` |
| Deployment-manifest JSON Schema | `deploy-manifest.schema.json` beside this record — `dcs-model deploy-schema` emitted at the recorded commit, pinned byte-for-byte with its sha256 by the drift test in `crates/dcs-model/tests/deploy_schema.rs`. Moved once since `v0.3.0`'s recorded commit: #1282's additive-optional `history_file` per-controller declaration — decision 102's durable process-history mount, landing in this record's tranche beside the store itself; a manifest predating the field validates unchanged |
| Deployment-manifest schema sha256 | `980430ca8725af997a7b5063f00d2a9663fe4619ca00a542917bde24f270cfa9` |
| `dcs-controller` image digest | *pending* — `dcs-controller@sha256:<digest>`, the image `docker build` produces from `Dockerfile` at the tag |
| `dcs-plant-server` image digest | *pending* — `dcs-plant-server@sha256:<digest>`, the image `docker build -f Dockerfile.plant` produces at the tag |

## Compatibility notes

`v0.6.0` is the minor bump the post-`v0.5.0` correction of the
consumer contract takes under the release versioning policy. The
determination this record owes: the tranche's changes are corrective
and additive — they hold `MODEL_VERSION` and the checkpoint format
set — and the bump exists so a `version = "0.5"` requirement cannot
silently resolve the corrected release line, not because any
supported item broke.

What the tranche changed for consumers — the contract corrections
since `v0.5.0` the consumer-boundary mirror legs gate on:

- The configured tracking source's address rediscovery (#1202): a
  `--standby` target resolved once at startup into the dial address
  every pull pinned, so a peer restarted onto a new address under
  the same name — the routine container-recreate condition a
  redundant pair exists for — kept answering while the pulls dialed
  the old one forever, stranding the standby `degraded` and, with
  `--auto-promote` armed, manufacturing a failover against the live
  owner. The declared `host:port` now stays the name each pull
  re-resolves, so a peer's move under its name is followed without
  reconfiguration; a name that does not resolve degrades tracking to
  pull misses as before, never a startup error, and the resumed
  regressed stream journals its `source_restarted` record as before.
- The driven-scan batch's per-request bound (#1203): `POST /scan`
  accepted an unbounded `scans`, letting one request pin its
  submission worker and the run's timeline for as long as the batch
  cared to run — a client gone mid-batch left a scan run nobody
  could terminate. A `scans` past the declared bound
  (`MAX_SCANS_PER_REQUEST`, 256) is now refused `400` before the
  first scan, the refusal naming the asked count and the bound;
  every accepted batch is a bounded, terminating unit of work, and a
  longer advance composes of further bounded requests.
- The served status line's data-source label (#1196): the operator
  page titled its fallback source `active peer` whenever the pair
  list held two entries — even while that peer's own role report
  said `standby` or `promoting` through the failover gap,
  contradicting the pair-table row and the standing no-active-peer
  fault beside it. The note now reads `active peer` only while the
  serving peer's own latest role report settles `active`; otherwise
  it names the serving peer with the role it reports, a peer whose
  role poll failed or never answered carrying no role claim.
- The operator page's bounded history backfill (#1211): a cold load
  or a source switch issued one `?since=` read for the whole
  retained history, an answer that over a remote link never fit the
  one-second poll's abort bound — so the backfill retried unbounded
  every poll and a refresh could never finish. The catch-up now
  pages its reads (`HISTORY_PAGE_POINTS` points a page) under a
  per-poll `HISTORY_BACKFILL_BUDGET_MS`, each landed page's cursors
  standing so progress survives a later page's failure, and the feed
  line names the backfill's remaining streams while it lasts.
- The born-active's startup-failure dispositions (#985's record —
  decision 103 — implemented under #1017): a born-active never exits
  for a field-side startup condition. An unreachable field transport
  now boots the run into the pending-claim state — `role: standby`,
  `sync: unsynchronized`, write-quiesced, `POST /command` refusing
  `not_active`, driver diagnostics reporting `LinkState::
  Disconnected` — with the conditional startup grant deferred to the
  first answered contact, so restart-as-active can be launched
  inside the plant outage it exists to recover. A refused grant
  where the launch declared `--peer` rejoins the declared pair as
  its tracking standby — the refusal message's named remedy made
  configured behavior — and exits `FieldClaimFailed` naming the
  incumbent only where no pair was declared; an inconclusive
  verdict, the `Err` leg, holds pending and re-issues the
  idempotent conditional grant once per answered contact.
- The declared-duty durable process-history store (#894's record —
  decision 102 — implemented under #907): the model gains the
  serde-optional `record` io_point field, an `every_ticks` cadence
  and a `retain_days` sizing declaration naming the series that
  carry a recording duty — the compliance datasets decision 102
  scopes (the per-filter turbidity series, the daily
  disinfection/CT record, interval energy data). The controller's
  new `--history-file` is the journal file's sibling append-only
  durable store: the recorder samples declared-duty points'
  post-scan image at the declared cadence under the monitor lock,
  the file replays at bind to seed the bounded served window
  `GET /history/durable`, run-boundary records mark process
  lifetimes, the tick domain's civil-time anchor stamps into the
  file so downstream calendar aggregation is computable, and an
  append the file cannot take stays fatal under decision 36's rule
  rather than silently gapping the record. The volatile per-point
  ring is unchanged — declared duty is the only path to durability.
- The consumer-boundary mirror legs for the tranche's contract
  fixes gained their named diagnostics and land on the reference
  plant's clean CI and the simulated QA rig — the tracking-source
  rediscovery legs (`track-source-rediscovery`, #1204/#1205) and
  the driven-scan bound legs (`scan-batch-bound`, #1206/#1207) —
  each reporting `inconclusive`, never its failure, while the
  pinned release predates the contract it exercises; this release
  is the pin that lets them report. The filed legs for the landed
  born-active and durable-history contracts (#1033/#1078 and
  #966/#972) gate on the same pin, and the filed legs for the
  contract fixes still in flight or blocked — #867's
  announced-source verification (#1222/#1223) and the defect-fix
  contracts (#690, #708, #709, #735, #730, #828, #683, #775, #827,
  #638, #694) — each report `inconclusive` until their contract
  lands and a release carries it.

The determination:

- `MODEL_VERSION` holds at `1`; `PlantModel::load` still accepts
  exactly that version. A `version: 1` document written against
  `v0.5.0` validates unchanged under `v0.6.0` tooling, and the
  plant-model schema emission is byte-identical to `v0.5.0`'s
  recorded artifact — the `recording-duty` grammar is additive
  optional; the emission moved once more after this record was
  written, #548's declared-unit grammar being additive optional too,
  so a `v0.5.0` document still validates unchanged.
- The checkpoint format set holds: `Checkpoint.format_version` still
  negotiates against `SUPPORTED_FORMAT_VERSIONS` (`{0, 1}`; absent
  reads as `0`). Checkpoints cross the bump under the same
  per-connection negotiation and fingerprint gate — no checkpoint
  migration is owed.
- The tranche corrected the served and arbitration contracts —
  tracking-source resolution, driven-scan admission, the born-active
  startup dispositions, and the monitoring page's reporting — and
  added the opt-in `record` model field, the `--history-file` flag,
  and the `GET /history/durable` read beside them, without removing
  or re-shaping any supported engineering surface, so a consumer's
  composition code compiles unchanged on the repin. The migration
  expectation is the repin itself — the reference plant's
  `ci/check.sh` `upgrade` stage proves the `v0.5.0` → `v0.6.0`
  crossing byte-identically; non-Rust consumers pin the same four
  schema artifacts `v0.5.0` recorded — three byte-identical, the
  deployment-manifest emission moving once with #1282's
  additive-optional `history_file` controller declaration, so a
  `v0.5.0` manifest still validates unchanged.
- The `dcs-build` `station`, `dosing`, `ijmuiden`, and `ethercat`
  modules remain platform-owned reference compositions outside the
  compatibility policy (decision 81); a consumer composes from the
  supported primitives or copies a pattern.

## Consumer pins

- Crates: `dcs-build = { git = "<repo>", tag = "v0.6.0" }` — or
  `rev = "<commit>"` for the identical immutable commit, the recorded
  commit above once it is filled; `dcs-core` and `dcs-model` under the
  same pin.
- Tooling: `cargo install --git <repo> --tag v0.6.0 dcs-model
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

- `[workspace.package]` version `0.6.0` and the regenerated workspace
  `Cargo.lock` — one crate version across the release set.
- The reference plant's repin: `Cargo.toml`'s `tag = "v0.6.0"`,
  `ci/check.sh`'s `DCS_REV` default `v0.6.0` and `DCS_UPGRADE_REV`
  default at the `v0.5.0` recorded rev — the `upgrade` stage
  materializes the tree at the `v0.5.0` pin and repins to `v0.6.0`,
  proving the named crossing — `deploy/manifest.json`'s
  `dcs_release: "v0.6.0"` and `v0.6.0` image tags, and
  `deploy/compose.yaml`'s `x-dcs-release` and images.
- The check's schema non-drift legs continue to cover all four
  recorded artifacts — `deploy-schema` and `--dynamics-schema`
  beside `schema`/`interface-schema`.

The remaining items are the supervisor's publication operations:

- Cut `v0.6.0` on the `main` commit carrying this record once
  `rust-proofs` is green on that exact commit; fill the Commit field
  with the tagged sha.
- Build and publish the `dcs-controller` and `dcs-plant-server`
  images; fill the two digest fields above.
- Regenerate `reference-plant/Cargo.lock` against the published tag
  (`cargo update` in the consumer tree, README §7's documented step)
  so the committed lockfile records the `tag = "v0.6.0"` source — the
  lockfile cannot name the tag's target before the tag exists, so the
  repin commit carries the previous resolution and the check's resolve
  leg re-resolves on the first post-tag run.
- Re-run `reference-plant/ci/check.sh` end to end against the
  published artifacts and capture its output as the release's
  consumer evidence; the upgrade stage's doctored-version refusals
  must still report their named diagnostics.
