# Release record: v0.10.0

The tenth release of the DCS platform opens the `0.10` line and adds a
reusable pump engineering API with an equipment surface in the unified
plant model. Equipment metadata owns existing component identities,
lists ordered summary points, and declares labeled controls through the
existing validated command path. The plant-model schema and served
signal index carry these additive declarations; existing flat JSON
documents still load. Direct Rust construction of the expanded public
records needs the new fields described below. The manifest and
checkpoint formats remain unchanged. This release is cut under the
procedure in `docs/release-contract.md` (decision 80).
Fields marked *pending* are filled mechanically by the release procedure
when the supervisor cuts the tag and publishes the images; the
checked-in schemas are the current emission, byte-pinned by the drift
tests so they cannot diverge from the code before the tag is cut, and
so a contract landing between this record and the cut cannot move an
emission into the tag unrecorded — the same pin every earlier record
carried.

| Field | Value |
|---|---|
| Tag | `v0.10.0` — *pending*: the release tag is placed on the recorded commit below when the release is cut (`git tag v0.10.0 <recorded sha>`; the tag names this release's commit, not whatever `main` carries afterwards) |
| Commit | *pending* — the tagged `main` commit carrying this record, the revision this record's schemas are emitted at. A commit cannot name its own sha, so the supervisor's publication commit fills this field; the cut lands on it and `reference-plant/Cargo.lock` is re-resolved against it in the same step (see the post-cut checklist) |
| Crate versions | `0.10.0` for every crate in the release set — one workspace version covers `dcs-build`, `dcs-core`, `dcs-model` (and the `dcs-model` / `dcs-controller` binaries built from it), `dcs-monitor` (shipping `dcs-ctl` and `dcs-alarm-report`), `dcs-plant` (`dcs-plant-server`), and `dcs-sim-net` (`dcs-plant-ctl`); the `[workspace.package]` bump and the regenerated workspace `Cargo.lock` land with this publication |
| Plant-model JSON Schema | `plant-model.schema.json` beside this record — `dcs-model schema` emitted at the recorded commit, pinned byte-for-byte with its sha256 by the schema drift test in `crates/dcs-model/tests/schema.rs`. Includes optional equipment ownership, ordered summary points, and labeled controls; pending artifact copies track this emission under the existing drift-test procedure |
| Plant-model schema sha256 | `ad3550f3d30ee9b0bbe16744aa92e51f1f95cf7d9c2a35381c945575d4361b1c` |
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
- The QA lane's born-controller launch through the shared argv builder
  (#1430) and the consumer lockfile's staleness proof (#1433): the
  first is rig-side harness plumbing under `qa_lane/` with no crate,
  served, or model edit; the second is the consumer boundary's own
  check — `ci/check.sh`'s `lockfile` stage, which compares the
  committed `Cargo.lock` against the manifest's declared pin *before*
  any fetch can re-resolve it and names `lockfile-stale`, plus the
  regenerated `v0.9.0` lockfile it shipped with. Both strengthen the
  consumer proof; neither touches a released artifact.
- This release's own publication (#1440): the
  `[workspace.package]` bump to `0.10.0` and the regenerated workspace
  `Cargo.lock` — the Crate versions field above — the reference plant's
  repin onto this line (`Cargo.toml`'s `tag = "v0.10.0"`,
  `Cargo.lock`'s regenerated pin, `README.md`'s pin and install
  command, `ci/check.sh`'s `DCS_REV` default and its header prose,
  `deploy/manifest.json`'s `dcs_release` and image tags, and
  `deploy/compose.yaml`'s `x-dcs-release` and images),
  `docs/customer-quickstart.md` and `docs/requirements/water-wastewater.md`'s
  `WW-ENG-003` status naming this record and publication, and this
  record's own filled fields. That publication prepared the repin;
  the equipment amendment below adds a supported library surface.
- The reusable pump and equipment amendment (`WW-FND-001`,
  `WW-OPS-001`, `WW-CTL-001`, `WW-CTL-002`): the public pump helper
  composes existing control and alarm blocks through typed bindings.
  `PlantBuilder::equipment` registers `Equipment` with component
  ownership, summary points, and labeled `EquipmentControl`s. Model
  validation rejects unresolved or repeated members, conflicting
  ownership, and controls that are not listed writable inputs feeding
  a member. Boolean action labels require a Boolean point. The served
  `SignalIndex` carries the same equipment declaration and optional
  `ComponentRecord.id` identities for descriptor joins. Equipment-only
  changes also appear in `ModelDiff` and the model CLI's diff listing.

The equipment amendment grows the supported engineering surface and
the optional model and signal-index metadata. It uses existing point
values, component interfaces, command receipts, alarms, and checkpoint
state rather than defining another execution or command schema.
Manifest, journal, durable-history, and checkpoint shapes do not move.

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
So the repin below is what moves them off `inconclusive`: with
`dcs_release: "v0.10.0"` the consumer-boundary mirror legs stop staging
against a release that predates the contract they exercise and report
their own verdicts, which is the acceptance evidence the post-cut
check re-run captures.

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
  `v0.9.0` validates unchanged under `v0.10.0` tooling. The new schema
  adds optional `equipment`; older documents omit it and decode an
  empty list, and empty lists serialize without that key.
- The checkpoint format set holds: `Checkpoint.format_version` still
  negotiates against `SUPPORTED_FORMAT_VERSIONS` (`{0, 1}`; absent
  reads as `0`), and `CHECKPOINT_FORMAT_VERSION` is still `1`.
  Checkpoints cross the bump under the same per-connection negotiation
  and fingerprint gate — no checkpoint migration is owed, and no
  checkpoint, journal, or durable-history file changed shape in this
  tranche.
- Existing `PlantBuilder` compositions keep their signatures and
  emit flat documents until they register equipment. Direct Rust
  literals of `PlantModel`, `SignalIndex`, and `ModelDiff` must supply
  `equipment: Vec::new()` when unused; a `ComponentRecord` literal
  supplies `id: None` when no model identity is available. These source
  additions are named under the `0.x` minor-release policy. Exhaustive
  matches on `BuildError` must handle `InvalidConfiguration`, and
  exhaustive matches on `ValidationError` must handle the new equipment
  validation variants. Non-Rust
  consumers use this record's updated plant-model schema when screening
  equipment-bearing documents; the other three schemas are unchanged.
- The served signal index adds optional equipment metadata and
  component identities. Legacy indexes deserialize with those fields
  absent, and existing consumers that ignore additional JSON fields
  remain usable. The additive growth the `v0.9.0` line carries also
  stays readable by an older consumer: a
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
  same pin. The reference plant's committed `Cargo.lock` records the
  tag's pin resolved at the revision the cut lands on, so a fresh clone
  resolves under `cargo fetch --locked`; while the tag is uncut that
  revision is the commit carrying this record and the supervisor's cut
  re-points the lockfile at the tagged commit in the same step (see
  the post-cut checklist).
- Tooling: `cargo install --git <repo> --tag v0.10.0 dcs-model
  dcs-controller dcs-plant dcs-monitor dcs-sim-net` — `dcs-monitor`
  ships `dcs-ctl` and `dcs-alarm-report`, `dcs-sim-net` ships
  `dcs-plant-ctl` — or binaries built from the tag. Every one of these
  binaries is built from this workspace's `0.10.0` crates, so the
  install resolves from the tag alone with no publication step behind
  it; the images below are the only release artifacts a registry
  publication decides.
- Images: *pending* — `dcs-controller@sha256:<digest>` and
  `dcs-plant-server@sha256:<digest>` once the record's digest fields
  are filled, or `docker build` / `docker build -f Dockerfile.plant`
  at the tag. The reference plant's `deploy/manifest.json` and
  `deploy/compose.yaml` name `dcs-controller:v0.10.0` and
  `dcs-plant-server:v0.10.0`, the tag-named tags a local `docker build`
  at the tag produces and a registry publication can then replace with
  the recorded digests.

## Post-cut checklist

Landed with this publication — the non-registry half:

- The record itself and the four emitted schema artifacts, each
  byte-pinned with its published sha256 by the drift tests, so the
  schema non-drift legs continue to cover all four recorded
  artifacts — `deploy-schema` and `--dynamics-schema` beside
  `schema`/`interface-schema`.
- `[workspace.package]` version `0.10.0` and the regenerated workspace
  `Cargo.lock` — one crate version across the release set, the Crate
  versions field above.
- The reference plant's repin: `Cargo.toml`'s `tag = "v0.10.0"`,
  `README.md`'s pin block, install command, and recorded-record
  pointer, `ci/check.sh`'s `DCS_REV` default `v0.10.0`,
  `deploy/manifest.json`'s `dcs_release: "v0.10.0"` and `v0.10.0` image
  tags, and `deploy/compose.yaml`'s `x-dcs-release` and images. Its
  `DCS_UPGRADE_REV` default stays at `07ec24f` — #983's baseline, the
  earliest `v0.8.0`-line rev whose builder API carries this
  composition's declared dimensional metadata, so the `upgrade` stage
  materializes the tree there, repins it to `v0.10.0`, and proves the
  named crossing from a baseline this tree's own source still compiles
  against. `ci/check.sh`'s `DCS_REV` header prose names the release this
  tree pins and moves with the default; this pin carries the same
  contract set the `v0.9.0` repin named, so no entry in that list
  changes.
- The reference plant's regenerated `Cargo.lock`: the release crates
  recorded at the `tag = "v0.10.0"` source on this repository's
  published remote, so the shipped consumer artifact satisfies the
  shipped manifest and `cargo fetch --locked` resolves it without
  re-resolving. Its precise revision is the commit carrying this
  record, because a tag's target does not exist until the supervisor
  cuts it — the same ordering the `v0.9.0` line recorded, and the one
  step below re-points it. `ci/check.sh`'s `lockfile` stage compares the
  committed lockfile against the manifest's pin and this record's filled
  `Commit` field *before* any fetch can re-resolve it, so a lockfile
  left at another revision is reported `lockfile-stale` rather than
  absorbed — #1433's fix, which is why the regeneration cannot wait for
  the tag.
- The release-line prose this publication advances:
  `docs/customer-quickstart.md` and
  `docs/requirements/water-wastewater.md`'s `WW-ENG-003` status now name
  the `v0.10.0` record beside the `v0.9.0` one, the line the `v0.9.0`
  publication advanced and this one supersedes.

The remaining items are the release procedure's mechanical fill the
tag's own coordinates decide, and the supervisor's publication
operations — the fields this record still marks *pending*:

- Cut `v0.10.0` on the commit carrying the version bump and this repin
  once `rust-proofs` is green on that exact commit; fill the Commit
  field with the tagged sha and clear the Tag field's *pending* marker.
  The tag lands on the publication commit, not on the commit this
  record already sits on: that one carries the record without the
  `[workspace.package]` bump, so a cut there would publish crates
  labelled `0.10.0` that are the `v0.9.0` line's bytes.
- Re-point `reference-plant/Cargo.lock` at the tagged commit
  (`cargo update -p dcs-build -p dcs-core -p dcs-model` in the consumer
  tree, README §7's documented step) and record the release crates at
  `version = "0.10.0"` from
  `git+<repo>?tag=v0.10.0#<the Commit field's sha>`. The two steps are
  one: the `lockfile` stage compares the committed lockfile against
  this record's `Commit` field and reports `lockfile-stale` when the
  two diverge, so a cut on any other commit must be followed by both.
- Build and publish the `dcs-controller` and `dcs-plant-server`
  images; fill the two digest fields above and the Images entry under
  Consumer pins.
- Re-run `reference-plant/ci/check.sh` end to end against the
  published artifacts and capture its output as the release's
  consumer evidence; the legs this record's pin reaches must report
  verdicts rather than `inconclusive`, and the upgrade stage's
  doctored-version refusals must still report their named diagnostics.
