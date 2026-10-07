# Release record: v0.7.0

The seventh release of the DCS platform, carrying the post-`v0.6.0`
correction of the consumer contract — the contract fixes whose
consumer-boundary mirror legs report `inconclusive` while the pinned
release predates them: the voluntary demotion's released-claim
hand-back, the persistence paths' startup distinctness refusal, the
deferred startup-claim refusal's named dispositions, and the remote
born-active's deferred correspondence gate — cut under the procedure
in `docs/release-contract.md` (decision 80). Fields marked *pending*
are filled mechanically by the release procedure when the supervisor
cuts the tag and publishes the images; the checked-in schemas are the
current emission, byte-pinned by the drift tests so they cannot
diverge from the code before the tag is cut.

| Field | Value |
|---|---|
| Tag | `v0.7.0` — *pending*: the release tag is placed on the recorded commit when the release is cut |
| Commit | *pending* — the tagged `main` commit carrying this record, the revision this record's schemas are emitted at |
| Crate versions | `0.7.0` for every crate in the release set — one workspace version covers `dcs-build`, `dcs-core`, `dcs-model` (and the `dcs-model` / `dcs-controller` binaries built from it), `dcs-monitor` (shipping `dcs-ctl` and `dcs-alarm-report`), `dcs-plant` (`dcs-plant-server`), and `dcs-sim-net` (`dcs-plant-ctl`); the `[workspace.package]` bump lands with the cut |
| Plant-model JSON Schema | `plant-model.schema.json` beside this record — `dcs-model schema` emitted at the recorded commit, pinned byte-for-byte with its sha256 by the schema drift test in `crates/dcs-model/tests/schema.rs`. The emission is byte-identical to `v0.6.0`'s recorded artifact — no model-grammar move this tranche carries — and moved once after this record was written: #548's declared-unit metadata added optional `unit` declarations on io points, ports, and signals plus `parameter_units` beside `parameters`, additive optional grammar a `v0.6.0` document never carries |
| Plant-model schema sha256 | `ad3550f3d30ee9b0bbe16744aa92e51f1f95cf7d9c2a35381c945575d4361b1c` |
| Served-registry JSON Schema | `block-interfaces.schema.json` beside this record — `dcs-model interface-schema` emitted at the recorded commit, pinned byte-for-byte with its sha256 by the drift test in `crates/dcs-model/tests/interface_schema.rs`. Unchanged since `v0.3.0`'s recorded commit |
| Served-registry schema sha256 | `ddc00496814a4e8cd0d6ec8a5d9fbb95e83f518dcd927b17a4802f13ac84013a` |
| Dynamics-document JSON Schema | `dynamics.schema.json` beside this record — `dcs-plant-server --dynamics-schema` emitted at the recorded commit, pinned byte-for-byte with its sha256 by the drift test in `crates/dcs-plant/tests/dynamics_schema.rs`. Unchanged since `v0.3.0`'s recorded commit |
| Dynamics-document schema sha256 | `98fb4a4298c5974b8ab0adf1374cd6d53b0c2cfbd2e24a31c874090235b46f02` |
| Deployment-manifest JSON Schema | `deploy-manifest.schema.json` beside this record — `dcs-model deploy-schema` emitted at the recorded commit, pinned byte-for-byte with its sha256 by the drift test in `crates/dcs-model/tests/deploy_schema.rs`. Byte-identical to `v0.6.0`'s recorded artifact — the emission moved once since `v0.3.0`'s recorded commit, with #1282's additive-optional `history_file` per-controller declaration the `v0.6.0` tranche already carries |
| Deployment-manifest schema sha256 | `980430ca8725af997a7b5063f00d2a9663fe4619ca00a542917bde24f270cfa9` |
| `dcs-controller` image digest | *pending* — `dcs-controller@sha256:<digest>`, the image `docker build` produces from `Dockerfile` at the tag |
| `dcs-plant-server` image digest | *pending* — `dcs-plant-server@sha256:<digest>`, the image `docker build -f Dockerfile.plant` produces at the tag |

## Compatibility notes

`v0.7.0` is the minor bump the post-`v0.6.0` correction of the
consumer contract takes under the release versioning policy. The
determination this record owes: the tranche's changes are corrective
and additive — they hold `MODEL_VERSION` and the checkpoint format
set — and the bump exists so a `version = "0.6"` requirement cannot
silently resolve the corrected release line, not because any
supported item broke.

What the tranche changed for consumers — the contract corrections
since `v0.6.0` the consumer-boundary mirror legs gate on:

- The voluntary demotion's released claim (#1270): `POST /demote`'s
  `release_writer{keep_claim}` hand-back left the standing claim
  marked `yielded` under the demoted peer's token — but the same
  peer's orphan-cycle ensure re-armed it under that token within a
  scan of the tracked line reporting the field ownerless, standing a
  live claim under a member reporting `standby` and fencing every
  conditional path the demotion handed the field to. The `yielded`
  mark the request-origin release sets now suppresses both ex-owner
  re-arm paths — the orphan cycle's unbound ensure and the loss
  mark's bound reclaim probe — for the claim this run handed back,
  while a fencing-loss demotion's coverage keeps: the orphaned pull's
  ownerless evidence re-arms the reclaim where the mark stood down
  for a successor that has since left, the arm reading "this run
  owned the field, the claim was preempted under it, and no owner
  stands now" — never preempting a live attachment. A granted
  orphan-cycle re-arm journals `field_claim_rearmed`, one record per
  granted streak, attributing the re-take the orphaned transition
  alone could not name; a same-owner live-controller re-grant clears
  the mark as before.
- The persistence paths' startup distinctness (#1292): a
  `--state-file` aliased with `--journal-file` or `--history-file`
  passed every validation — the append sinks' single-writer contract
  runs on an advisory lock the checkpoint sink never takes, so the
  checkpoint's write-then-rename orphaned the append writer's
  descriptor onto the renamed-away inode, the durable record landing
  nowhere the declared path reaches while the visible file read as
  checkpoint JSON the next startup's strict replay refused — a
  crash-loop the launch admitted silently. The three persistence
  paths are now distinct files on pain of a usage error refused at
  argument parse, before either sink opens — the refusal naming both
  flags and the shared path.
- The deferred startup-claim refusal's dispositions (#1301, #1316):
  decision 103's born-active contract hands the conditional startup
  grant's refused verdict to the run's shell — rejoin the declared
  pair as its tracking standby, exit `FieldClaimFailed` where none
  was declared — but the verdict had no delivery path when it
  answered at the pending run's first answered contact rather than
  at activation: the deferred `Ok(false)` landed inside a scan no
  activation result could leave, so a peerless run stood on the
  pending stand-down's standby surface forever where the boot-time
  verdict exits nonzero, and an undeclared born-active hung inert in
  the launch the refusal exists to end. The deferred refusal now
  latches once for the run's shell — the identical
  `SwitchError::FieldClaimFailed` the activation-time answer carries
  — journals `startup_claim_refused` once per refused grant beside
  the observed-claimant record attributing who refused, and settles
  under the identical disposition: a declared `--peer` keeps the run
  tracking the incumbent it converged on, a pairless launch exits
  naming the incumbent and the `--standby` remedy.
- The `--remote` born-active's correspondence gate (#1302): a
  born-active attached through `--remote` whose plant could not
  answer at launch held its declared-point correspondence probes
  unrun — the launch-time check the `sim-tcp` device factory runs
  eagerly has no answer on a silent transport — and the startup
  claim's landing took write-ownership of whatever field the address
  served, a foreign plant model's included. The outstanding probes
  now ride the remote attachment's deferred list and re-run ahead of
  every claim ask — the conditional startup grant and the
  unconditional promotion claim alike — so a plant answering under a
  different model refuses the claim rather than letting a
  mis-deployed controller seize a field it was never composed for.
- The consumer-boundary mirror legs for the tranche's contract fixes
  gate on this pin — the voluntary-demote released-claim legs
  (#1273/#1274), the persistence-distinctness legs (#1295/#1296), and
  the legs this record's proposal emits for the filed rig findings
  #1301/#1302/#1303 — each reporting `inconclusive`, never its
  failure, while the pinned release predates the contract it
  exercises; this release is the pin that lets them report. The
  filed legs for contract fixes still in flight — #867's
  announced-source verification cluster (#1222/#1223, #924, #1111),
  the pairless deferred-refusal disposition's remaining finding
  (#1314), and #1303's filed finding — each report `inconclusive`
  until their contract lands and a release carries it.

The determination:

- `MODEL_VERSION` holds at `1`; `PlantModel::load` still accepts
  exactly that version. A `version: 1` document written against
  `v0.6.0` validates unchanged under `v0.7.0` tooling, and the
  plant-model schema emission is byte-identical to `v0.6.0`'s
  recorded artifact — the tranche carries no model-grammar move; the
  emission moved once after this record was written, #548's
  declared-unit grammar being additive optional, so a `v0.6.0`
  document still validates unchanged.
- The checkpoint format set holds: `Checkpoint.format_version` still
  negotiates against `SUPPORTED_FORMAT_VERSIONS` (`{0, 1}`; absent
  reads as `0`). Checkpoints cross the bump under the same
  per-connection negotiation and fingerprint gate — no checkpoint
  migration is owed.
- The tranche corrected the served and arbitration contracts — the
  released claim's hand-back semantics, the persistence paths'
  admission, the deferred startup refusal's dispositions, and the
  remote attachment's correspondence gate — and added the
  `field_claim_rearmed` and `startup_claim_refused` journal records
  beside them, without removing or re-shaping any supported
  engineering surface, so a consumer's composition code compiles
  unchanged on the repin. The migration expectation is the repin
  itself — the reference plant's `ci/check.sh` `upgrade` stage proves
  the `v0.6.0` → `v0.7.0` crossing byte-identically; non-Rust
  consumers pin the same four schema artifacts `v0.6.0` recorded,
  all four emissions byte-identical.
- The `dcs-build` `station`, `dosing`, `ijmuiden`, and `ethercat`
  modules remain platform-owned reference compositions outside the
  compatibility policy (decision 81); a consumer composes from the
  supported primitives or copies a pattern.

## Consumer pins

- Crates: `dcs-build = { git = "<repo>", tag = "v0.7.0" }` — or
  `rev = "<commit>"` for the identical immutable commit, the recorded
  commit above once it is filled; `dcs-core` and `dcs-model` under the
  same pin.
- Tooling: `cargo install --git <repo> --tag v0.7.0 dcs-model
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

- `[workspace.package]` version `0.7.0` and the regenerated workspace
  `Cargo.lock` — one crate version across the release set.
- The reference plant's repin: `Cargo.toml`'s `tag = "v0.7.0"`,
  `ci/check.sh`'s `DCS_REV` default `v0.7.0` and `DCS_UPGRADE_REV`
  default at the `v0.6.0` recorded rev — the `upgrade` stage
  materializes the tree at the `v0.6.0` pin and repins to `v0.7.0`,
  proving the named crossing — `deploy/manifest.json`'s
  `dcs_release: "v0.7.0"` and `v0.7.0` image tags, and
  `deploy/compose.yaml`'s `x-dcs-release` and images.
- The check's schema non-drift legs continue to cover all four
  recorded artifacts — `deploy-schema` and `--dynamics-schema`
  beside `schema`/`interface-schema`.

The remaining items are the supervisor's publication operations:

- Cut `v0.7.0` on the `main` commit carrying this record once
  `rust-proofs` is green on that exact commit; fill the Commit field
  with the tagged sha.
- Build and publish the `dcs-controller` and `dcs-plant-server`
  images; fill the two digest fields above.
- Regenerate `reference-plant/Cargo.lock` against the published tag
  (`cargo update` in the consumer tree, README §7's documented step)
  so the committed lockfile records the `tag = "v0.7.0"` source — the
  lockfile cannot name the tag's target before the tag exists, so the
  repin commit carries the previous resolution and the check's resolve
  leg re-resolves on the first post-tag run.
- Re-run `reference-plant/ci/check.sh` end to end against the
  published artifacts and capture its output as the release's
  consumer evidence; the upgrade stage's doctored-version refusals
  must still report their named diagnostics.
