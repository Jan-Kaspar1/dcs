# Release record: v0.10.0

The tenth release of the DCS platform, cutting the consumer contract
line forward from the `v0.9.0` record: at the commit carrying this
record the tranche owes no supported-surface, model-grammar,
manifest-shape, served-wire, or checkpoint change at all — `main`
carries the `v0.9.0` record's own landing (#1426) and the `v0.9.0`
publication's registry and repin half (#1427) since that record, and
neither touched crate library source — so all four artifacts this
record pins are byte-identical to the ones the `v0.9.0` record pins,
and the minor bump exists to open the next `0.10` line a
`version = "0.9"` requirement cannot silently resolve. It is cut under
the procedure in `docs/release-contract.md` (decision 80). Fields
marked *pending* are filled mechanically by the release procedure when
the supervisor cuts the tag and publishes the images; the checked-in
schemas are the current emission, byte-pinned by the drift tests so
they cannot diverge from the code before the tag is cut, and so a
contract landing between this record and the cut cannot move an
emission into the tag unrecorded — the same pin every earlier record
carried.

| Field | Value |
|---|---|
| Tag | `v0.10.0` — *pending*: the release tag is placed on the recorded commit when the release is cut |
| Commit | *pending* — the tagged `main` commit carrying this record, the revision this record's schemas are emitted at |
| Crate versions | `0.10.0` for every crate in the release set — one workspace version covers `dcs-build`, `dcs-core`, `dcs-model` (and the `dcs-model` / `dcs-controller` binaries built from it), `dcs-monitor` (shipping `dcs-ctl` and `dcs-alarm-report`), `dcs-plant` (`dcs-plant-server`), and `dcs-sim-net` (`dcs-plant-ctl`) — *pending*: the `[workspace.package]` bump lands with the cut |
| Plant-model JSON Schema | `plant-model.schema.json` beside this record — `dcs-model schema` emitted at the recorded commit, pinned byte-for-byte with its sha256 by the schema drift test in `crates/dcs-model/tests/schema.rs`. Byte-identical to `v0.9.0`'s recorded artifact — the emission last moved with #548's declared-unit metadata, which landed on `main` while the `v0.8.0` tag was still uncut and is therefore already carried by every record from `v0.8.0`'s on, this one included |
| Plant-model schema sha256 | `07f9f93d1475c7bc783e99e4e7807fe5706a1549302a3701b67212bb3e793301` |
| Served-registry JSON Schema | `block-interfaces.schema.json` beside this record — `dcs-model interface-schema` emitted at the recorded commit, pinned byte-for-byte with its sha256 by the drift test in `crates/dcs-model/tests/interface_schema.rs`. Byte-identical to `v0.9.0`'s recorded artifact. Unchanged since `v0.3.0`'s recorded commit — decision 108's `usurped` sync state and `standby_usurped` pair-fault kind are payload values the registry's kinds do not enumerate |
| Served-registry schema sha256 | `ddc00496814a4e8cd0d6ec8a5d9fbb95e83f518dcd927b17a4802f13ac84013a` |
| Dynamics-document JSON Schema | `dynamics.schema.json` beside this record — `dcs-plant-server --dynamics-schema` emitted at the recorded commit, pinned byte-for-byte with its sha256 by the drift test in `crates/dcs-plant/tests/dynamics_schema.rs`. Byte-identical to `v0.9.0`'s recorded artifact. Unchanged since `v0.3.0`'s recorded commit |
| Dynamics-document schema sha256 | `98fb4a4298c5974b8ab0adf1374cd6d53b0c2cfbd2e24a31c874090235b46f02` |
| Deployment-manifest JSON Schema | `deploy-manifest.schema.json` beside this record — `dcs-model deploy-schema` emitted at the recorded commit, pinned byte-for-byte with its sha256 by the drift test in `crates/dcs-model/tests/deploy_schema.rs`. Byte-identical to `v0.9.0`'s recorded artifact — the emission last moved with #1282's additive-optional `history_file` per-controller declaration; #1341's single-writer rule binds at the launch and in the contract's declared-path prose, not in the emitted shape, so no schema move is owed |
| Deployment-manifest schema sha256 | `980430ca8725af997a7b5063f00d2a9663fe4619ca00a542917bde24f270cfa9` |
| `dcs-controller` image digest | *pending* — `dcs-controller@sha256:<digest>`, the image `docker build` produces from `Dockerfile` at the tag |
| `dcs-plant-server` image digest | *pending* — `dcs-plant-server@sha256:<digest>`, the image `docker build -f Dockerfile.plant` produces at the tag |

## Compatibility notes

`v0.10.0` is the minor bump the post-`v0.9.0` state of the consumer
contract takes under the release versioning policy. The determination
this record owes: the tranche's changes are release mechanics and the
prose that names them — they hold `MODEL_VERSION`, the checkpoint
format set, and all four recorded emissions — and the bump exists so a
`version = "0.9"` requirement cannot silently resolve this line, not
because any supported item broke.

What the tranche changed for consumers — every commit on `main` since
the `v0.9.0` record landed:

- The `v0.9.0` record's own landing (#1426): `docs/releases/v0.9.0/`
  with its `record.md` and the four emitted schema artifacts, plus the
  four drift-test pins that keep those artifacts byte-equal to what the
  tooling emits. No crate library source changed — the workspace's only
  `crates/` edits are the four `recorded_release_*` drift tests
  themselves — so no supported surface, no served wire, no model
  document, and no checkpoint moved.
- The `v0.9.0` publication's registry and repin half (#1427): the
  `[workspace.package]` bump to `0.9.0` and the regenerated workspace
  `Cargo.lock` (the Crate versions field above names the version this
  release's own bump supersedes), `docs/customer-quickstart.md` and
  `docs/requirements/water-wastewater.md`'s `WW-ENG-003` status naming
  the `v0.9.0` record and publication, and the reference plant's repin
  onto it — `Cargo.toml`'s `tag = "v0.9.0"`, `ci/check.sh`'s `DCS_REV`
  default, `deploy/manifest.json`'s `dcs_release` and image tags, and
  `deploy/compose.yaml`'s `x-dcs-release` and images. This is the
  consumer tree's pin, not the platform's contract: no crate library
  source changed, so nothing a consumer compiles against moved.

So this tranche carries no contract correction, no model-grammar move,
no manifest-shape move, no served-wire growth, and no checkpoint move.
What it carries is the release line itself: a `0.10` minor series a
consumer's `version = "0.9"` requirement cannot resolve, the four
artifacts a non-Rust consumer pins unchanged from `v0.9.0`, and the
`main` commit the `v0.9.0` record's contract corrections stand on —
the `v0.9.0` line and this one agree on every emitted byte.

### What a contract landing before the cut owes this record

The tranche above is what `main` carries at this record's commit. A
contract fix that lands between this record and the tag cut joins the
tranche through this record's amendment, before the publication step
runs — the same convention every earlier record in this chain used,
which is why each of them landed close to the cut whose tranche it
named. Nothing about that amendment is mechanical, so it stays with
the record and not with the publish step: the contract is named under
"What the tranche changed for consumers" with its issue, its decision,
and what it changes for a consumer; and if it moves a model-grammar or
manifest field, the affected artifact beside this record is re-emitted
and its published sha256 replaced — the drift test in
`crates/dcs-model/tests/schema.rs`,
`crates/dcs-model/tests/interface_schema.rs`,
`crates/dcs-model/tests/deploy_schema.rs`, and
`crates/dcs-plant/tests/dynamics_schema.rs` fails by name on the stale
bytes until both are regenerated, so a moved emission cannot reach the
tag unrecorded. A wire-only or runtime-only correction leaves all four
artifacts where they are, exactly as the `v0.9.0` tranche's own fixes
did.

### The filed legs this pin reaches

The reference plant's pin moves to this release with the repin, so the
consumer-boundary mirror legs report against it. The filed legs whose
contracts are already on `main` — the cross-peer `--state-file`
single-writer legs (rig #1347 beside consumer mirror #1348, on #1341's
fix), the claim-basis skew-bound legs (#1345/#1346, on #1409's fix), the
ahead-bound rejoin legs (rig #1275 beside the consumer-boundary mirror,
on #1336's fix), the announced-source verification cluster (#867, the
mirrors #1222/#1223 and #924/#1111), and the graceful-shutdown contract
(#820, the mirrors #975/#984) — each report `inconclusive` rather than
its own failure while the staged release predates the contract it
exercises; a pin carrying the contract is what lets them report, and
this record carries the same contract line the `v0.9.0` record does.

Two contracts in that line have no consumer-boundary mirror by
construction, and this pin does not change that: the usurped-verdict
reclaim's filed rig leg (#1417) exercises a `--pair-token` pair, which
the reference deployment deliberately leaves unkeyed, and the
scripted-miss claim-hold's rig leg (#1418) exercises a `sim-bus`
backend, which the consumer deployment mounts no instance of. Their rig
legs carry the contract; a consumer mirror would have to mount the very
configuration the reference deployment declares it does not run.

The filed legs whose contracts are still in flight each report
`inconclusive` until their contract lands and a release carries it, and
this record does not name any of them as carried: the
attributed-switch control-lane isolation mirror (#1266, on #1264's fix),
the `stale_after_ticks` cadence-domain contract (#1411), the
quality-aware cause-alarm contract (#827, mirror #1134), and whichever
defect-fix contracts merge before the cut.

The determination:

- `MODEL_VERSION` holds at `1`; `PlantModel::load` still accepts
  exactly that version. A `version: 1` document written against
  `v0.9.0` validates unchanged under `v0.10.0` tooling; the
  plant-model schema emission is byte-identical to `v0.9.0`'s recorded
  artifact, so a `v0.9.0` document still validates unchanged against the
  recorded schema as well.
- The checkpoint format set holds: `Checkpoint.format_version` still
  negotiates against `SUPPORTED_FORMAT_VERSIONS` (`{0, 1}`; absent
  reads as `0`), and `CHECKPOINT_FORMAT_VERSION` is still `1`.
  Checkpoints cross the bump under the same per-connection negotiation
  and fingerprint gate — no checkpoint migration is owed, and no
  checkpoint, journal, or durable-history file changed shape in this
  tranche.
- Nothing on the supported engineering surface was removed or
  re-shaped, so a consumer's composition code compiles unchanged on the
  repin. The migration expectation is the repin itself — the reference
  plant's `ci/check.sh` `upgrade` stage proves the `07ec24f` →
  `v0.10.0` crossing byte-identically from a baseline this tree's own
  source still compiles against, the baseline the repin below names;
  non-Rust consumers pin the same four schema artifacts `v0.9.0`
  recorded, all four emissions byte-identical, so a consumer's own
  schema screening is unaffected.
- The served wire did not grow this tranche, and the additive growth
  the `v0.9.0` line carries stays readable by an older consumer: a
  payload an older peer serves decodes unchanged, an older page renders
  the new sync state as `orphaned`-shaped detail rather than crashing,
  and the pair-fault vocabulary's version field is what tells a
  consumer which kind list it is reading — `PAIR_FAULT_KINDS_VERSION`
  is still `4`, the eight kinds decision 19's vocabulary carried before
  `standby_usurped` keeping their indices.
- The claim-bound and process-shape constants this line publishes are
  unchanged: `MAX_CLAIM_LEAD` is still 64 run ticks, so the
  `v0.9.0` line's claim-basis refusal answers the same bound with the
  same two escapes.
- The `dcs-build` `station`, `dosing`, `ijmuiden`, and `ethercat`
  modules remain platform-owned reference compositions outside the
  compatibility policy (decision 81); a consumer composes from the
  supported primitives or copies a pattern.

## Consumer pins

- Crates: `dcs-build = { git = "<repo>", tag = "v0.10.0" }` — or
  `rev = "<commit>"` for the identical immutable commit, the recorded
  commit above once it is filled; `dcs-core` and `dcs-model` under the
  same pin.
- Tooling: `cargo install --git <repo> --tag v0.10.0 dcs-model
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
- `[workspace.package]` version `0.10.0` and the regenerated workspace
  `Cargo.lock` — one crate version across the release set, the Crate
  versions field above.
- The reference plant's repin: `Cargo.toml`'s `tag = "v0.10.0"`,
  `ci/check.sh`'s `DCS_REV` default `v0.10.0`, `deploy/manifest.json`'s
  `dcs_release: "v0.10.0"` and `v0.10.0` image tags, and
  `deploy/compose.yaml`'s `x-dcs-release` and images. Its
  `DCS_UPGRADE_REV` default stays at `07ec24f` — #983's baseline, the
  earliest `v0.8.0`-line rev whose builder API carries this
  composition's declared dimensional metadata, so the `upgrade` stage
  materializes the tree there, repins it to `v0.10.0`, and proves the
  named crossing from a baseline this tree's own source still compiles
  against. `ci/check.sh`'s `DCS_REV` header prose names the release this
  tree pins and moves with the default; this pin carries the same
  contract set the `v0.9.0` repin named, so no entry in that list
  changes.

The remaining items are the release procedure's mechanical fill the
tag's own coordinates decide, and the supervisor's publication
operations — the fields this record still marks *pending*:

- Cut `v0.10.0` on the `main` commit carrying this record once
  `rust-proofs` is green on that exact commit; fill the Commit field
  with the tagged sha and clear the Tag field's *pending* marker.
- Name this record beside the `v0.9.0` one in
  `docs/customer-quickstart.md` and in `docs/requirements/water-wastewater.md`'s
  `WW-ENG-003` status, the release-line prose the `v0.9.0` publication
  advanced and this one supersedes.
- Build and publish the `dcs-controller` and `dcs-plant-server`
  images; fill the two digest fields above and the Images entry under
  Consumer pins.
- Regenerate `reference-plant/Cargo.lock` against the published tag
  (`cargo update` in the consumer tree, README §7's documented step)
  so the committed lockfile records the `tag = "v0.10.0"` source — the
  lockfile cannot name the tag's target before the tag exists, so the
  repin commit carries the previous resolution and the check's resolve
  leg re-resolves on the first post-tag run.
- Re-run `reference-plant/ci/check.sh` end to end against the
  published artifacts and capture its output as the release's
  consumer evidence; the legs this record's pin reaches must report
  verdicts rather than `inconclusive`, and the upgrade stage's
  doctored-version refusals must still report their named diagnostics.
