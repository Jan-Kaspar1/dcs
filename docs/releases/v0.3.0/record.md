# Release record: v0.3.0

The third release of the DCS platform, carrying the post-`v0.2.0`
growth of the consumer contract — the served event-retention routing,
the declared-command availability verdicts, the attributed role
switch, and the enlarged shipped-tooling set — cut under the
procedure in `docs/release-contract.md` (decision 80). Fields marked
*pending* are filled mechanically by the release procedure when the
supervisor cuts the tag and publishes the images; the checked-in
schemas are the current emission, byte-pinned by the drift tests so
they cannot diverge from the code before the tag is cut.

| Field | Value |
|---|---|
| Tag | `v0.3.0` — *pending*: the release tag is placed on the recorded commit when the release is cut |
| Commit | *pending* — the tagged `main` commit carrying this record, the revision this record's schemas are emitted at |
| Crate versions | `0.3.0` for every crate in the release set — one workspace version covers `dcs-build`, `dcs-core`, `dcs-model` (and the `dcs-model` / `dcs-controller` binaries built from it); the `[workspace.package]` bump lands with the cut |
| Plant-model JSON Schema | `plant-model.schema.json` beside this record — `dcs-model schema` emitted at the recorded commit, pinned byte-for-byte with its sha256 by the schema drift test in `crates/dcs-model/tests/schema.rs` |
| Plant-model schema sha256 | `b5dc56f7306bb4ad7f791ff05075401a871bebc0f5dc9babba52ad2c57252600` |
| Served-registry JSON Schema | `block-interfaces.schema.json` beside this record — `dcs-model interface-schema` emitted at the recorded commit, pinned byte-for-byte with its sha256 by the drift test in `crates/dcs-model/tests/interface_schema.rs` |
| Served-registry schema sha256 | `ddc00496814a4e8cd0d6ec8a5d9fbb95e83f518dcd927b17a4802f13ac84013a` |
| Dynamics-document JSON Schema | `dynamics.schema.json` beside this record — `dcs-plant-server --dynamics-schema` emitted at the recorded commit, pinned byte-for-byte with its sha256 by the drift test in `crates/dcs-plant/tests/dynamics_schema.rs`. The first record carrying it: `v0.2.0`'s recorded commit predates the dynamics-document schema emission (#870) |
| Dynamics-document schema sha256 | `98fb4a4298c5974b8ab0adf1374cd6d53b0c2cfbd2e24a31c874090235b46f02` |
| Deployment-manifest JSON Schema | `deploy-manifest.schema.json` beside this record — `dcs-model deploy-schema` emitted at the recorded commit, pinned byte-for-byte with its sha256 by the drift test in `crates/dcs-model/tests/deploy_schema.rs`. The first record carrying it: `v0.2.0`'s recorded commit predates the deployment-manifest schema emission (#909) |
| Deployment-manifest schema sha256 | `b43dadc6cf3455cb26b20ab1656137e892f0609387b9dbedfc3291716afd1005` |
| `dcs-controller` image digest | *pending* — `dcs-controller@sha256:<digest>`, the image `docker build` produces from `Dockerfile` at the tag |
| `dcs-plant-server` image digest | *pending* — `dcs-plant-server@sha256:<digest>`, the image `docker build -f Dockerfile.plant` produces at the tag |

## Compatibility notes

`v0.3.0` is the minor bump the post-`v0.2.0` growth of the consumer
contract takes under the release versioning policy. The determination
this record owes: the tranche's additions are additive — they hold
`MODEL_VERSION` and the checkpoint format set — and the bump exists so
a `version = "0.2"` requirement cannot silently resolve the enlarged
release line, not because any supported item broke.

What the tranche changed for consumers:

- Kind-emitted events gained declared retention classes with served
  routing (#478, #458): an `EventDecl`'s `retention` marks each
  emission `journal`, `history`, or `latest`, and the runtime routes
  them to the matching consumer-visible store — journal-retained
  emissions land in `GET /journal`'s `event_emitted` entries,
  `history`/`latest` emissions in the bounded routed stores, and all
  of them in the instance-attributed `events` of `GET /resources`
  under their marks. The in-library kinds declare their retentions,
  so the served surface carries them without consumer wiring.
- Declared commands gained live availability verdicts (#461):
  `GET /resources` joins each kind-declared command's published
  verdict — an `available: false` row carries the kind's named
  refusal — so a consumer reads whether a command will be admitted
  before submitting it, and the same verdict echoes verbatim through
  the settled `command_refused` record.
- Role-switch requests are attributed (#467): the durable journal's
  `demote`/`promote` transition records carry the requesting actor,
  so the attributed record now names who switched the pair.
- The release set gained two consumer-pinnable tools: `dcs-ctl`, the
  shipped operator CLI for the monitor contract — `invoke` submits a
  kind-declared command through the bounded receipted path with
  `--actor` attribution beside the read subcommands (#481) — and
  `dcs-alarm-report`, the alarm flood and performance report computed
  over the served journal or the durable journal file (#479). Both
  ship in the `dcs-monitor` package.
- The release record itself grew two artifacts: `dynamics.schema.json`
  — `dcs-plant-server --dynamics-schema`'s emission for the
  `--dynamics`/`--check-dynamics` declaration list (#870) — and
  `deploy-manifest.schema.json` — `dcs-model deploy-schema`'s emission
  for the consumer-owned deployment manifest (#909). This is the first
  record carrying them; the plant-model and served-registry schemas
  are unchanged since `v0.2.0`'s cut.
- The model document's lint gained an advisory class — field `In`
  points without a declared `stale_after_ticks` freshness budget
  (#992). Advisories remain non-fatal; the reference plant's clean-CI
  lint gate names the class explicitly.

The determination:

- `MODEL_VERSION` holds at `1`; `PlantModel::load` still accepts
  exactly that version. A `version: 1` document written against
  `v0.2.0` validates unchanged under `v0.3.0` tooling.
- The checkpoint format set holds: `Checkpoint.format_version` still
  negotiates against `SUPPORTED_FORMAT_VERSIONS` (`{0, 1}`; absent
  reads as `0`). Checkpoints cross the bump under the same
  per-connection negotiation and fingerprint gate — no checkpoint
  migration is owed.
- The supported engineering surface grew only by addition, so a
  consumer's composition code compiles unchanged on the repin. The
  migration expectation is the repin itself — the reference plant's
  `ci/check.sh` `upgrade` stage proves the `v0.2.0` → `v0.3.0`
  crossing byte-identically — plus, for non-Rust consumers, pinning
  the two schema artifacts this record adds.
- The `dcs-build` `station`, `dosing`, `ijmuiden`, and `ethercat`
  modules remain platform-owned reference compositions outside the
  compatibility policy (decision 81); a consumer composes from the
  supported primitives or copies a pattern.

## Consumer pins

- Crates: `dcs-build = { git = "<repo>", tag = "v0.3.0" }` — or
  `rev = "<commit>"` for the identical immutable commit, the recorded
  commit above once it is filled; `dcs-core` and `dcs-model` under the
  same pin.
- Tooling: `cargo install --git <repo> --tag v0.3.0 dcs-model
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

- `[workspace.package]` version `0.3.0` and the regenerated workspace
  `Cargo.lock` — one crate version across the release set.
- The reference plant's repin: `Cargo.toml`'s `tag = "v0.3.0"`,
  `ci/check.sh`'s `DCS_REV` default `v0.3.0` and `DCS_UPGRADE_REV`
  default at the `v0.2.0` recorded rev — the `upgrade` stage
  materializes the tree at the `v0.2.0` pin and repins to `v0.3.0`,
  proving the named crossing — `deploy/manifest.json`'s
  `dcs_release: "v0.3.0"` and `v0.3.0` image tags, and
  `deploy/compose.yaml`'s `x-dcs-release` and images.
- The check's schema non-drift legs now cover all four recorded
  artifacts — `deploy-schema` and `--dynamics-schema` joined the
  pinned emissions beside `schema`/`interface-schema`.

The remaining items are the supervisor's publication operations:

- Cut `v0.3.0` on the `main` commit carrying this record once
  `rust-proofs` is green on that exact commit; fill the Commit field
  with the tagged sha.
- Build and publish the `dcs-controller` and `dcs-plant-server`
  images; fill the two digest fields above.
- Regenerate `reference-plant/Cargo.lock` against the published tag
  (`cargo update` in the consumer tree, README §7's documented step)
  so the committed lockfile records the `tag = "v0.3.0"` source — the
  lockfile cannot name the tag's target before the tag exists, so the
  repin commit carries the previous resolution and the check's resolve
  leg re-resolves on the first post-tag run.
- Re-run `reference-plant/ci/check.sh` end to end against the
  published artifacts and capture its output as the release's
  consumer evidence; the upgrade stage's doctored-version refusals
  must still report their named diagnostics.
