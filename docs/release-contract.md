# Consumer release contract

Decision 79 made customer plants external consumers of a released
platform. This document records the contract that statement binds: what
a release contains, how a consumer pins each artifact, how releases are
numbered, what compatibility a consumer can rely on, and how a release
is produced. A consumer should be able to identify and pin a release
from this document alone, without reading the platform source tree.

The mechanics are proven workspace-side by
`crates/dcs-build/tests/consumer_release.rs`, which resolves the
supported crates from an immutable revision of this repository with no
`path` dependency, composes and deterministically emits a model, and
passes it through the released tooling, by
`crates/dcs-build/tests/consumer_upgrade.rs`, which proves the
same-minor repin is a drop-in upgrade and that the incompatible
crossings refuse with named diagnostics, and by
`crates/dcs-build/tests/reference_plant.rs`, which materializes the
independent reference-plant repository — the `reference-plant/` tree
in this repository, publishable verbatim as the customer's own
repository — outside the workspace, substitutes only the `file://`
remote stand-in for the published origin, and runs the template's own
clean-CI check (`ci/check.sh`) green. The reference plant pins this
contract's first release.

## The supported release set

One release is one version of every artifact below, cut from one commit
of this repository.

| Artifact | What it is | Pin mechanism |
|---|---|---|
| `dcs-build` crate | The typed composition API — the consumer's engineering seam. Its public transitive surface (`dcs-core`, `dcs-model`) resolves with it. | Cargo `git` dependency: `dcs-build = { git = "<repo>", tag = "v<X.Y.Z>" }` or `rev = "<commit>"` |
| `dcs-core`, `dcs-model` crates | The contract vocabulary (`Value`, `Direction`, ids) and the emitted `PlantModel` document type. A consumer may also declare them directly — e.g. to assert `dcs_model::MODEL_VERSION` — under the same pin. | Same `git` dependency form; identical tag or rev |
| `dcs-model` CLI | `validate`, `summary`, `signal-index`, `diff`, `lint`, `schema` over a model document. | `cargo install --git <repo> --tag v<X.Y.Z> dcs-model`, or a binary built from the tag |
| `dcs-controller` binary | The generic controller; `--check` is the consumer's assemble-check (load, validate, resolve devices, construct components — no scan). | `cargo install --git <repo> --tag v<X.Y.Z> dcs-controller`, or the container image below |
| `dcs-controller` image | The generic controller container (`Dockerfile`); runs any operator-supplied model (decision 46). | Image digest: `dcs-controller@sha256:<digest>` recorded in the release record; or `docker build` at the tag |
| `dcs-plant-server` image | The shared simulated-plant container (`Dockerfile.plant`) for the consumer's simulation runs. | Image digest recorded in the release record; or `docker build -f Dockerfile.plant` at the tag |
| Plant-model JSON Schema | `dcs-model schema`'s emitted draft 2020-12 schema for non-Rust tooling (decision 40). | Recorded in the release record at `docs/releases/<tag>/plant-model.schema.json`, fetchable at the tag; its sha256 is in the record |
| Deployment manifest | The consumer-owned deployment declaration — the documented shape below. | A file in the consumer repository pinning the release's artifacts |

All `git` pins resolve through Cargo's git support: a tag names the
release, a `rev` pins the identical immutable commit. Pinning `rev`
directly is always supported — tags exist so a release has a name; the
two are interchangeable coordinates for the same artifact.

## The supported engineering surface

The contract covers the `dcs-build` composition primitives and the
vocabulary they re-export — the API the consumer-resolution check
compiles against:

- `PlantBuilder` and `SignalBuilder` (`dcs-build`'s `builder` module):
  `device`, `channel`, `field_input`, `field_input_stale_after`,
  `field_output`, `internal_input`, `internal_output`, `journaled`,
  `signal`, `add`, `connect`, `build`.
- The typed endpoint handles (`endpoint`): `Source<T>`, `Sink<T>`,
  `InPoint<T>`, `OutPoint<T>`, `Dynamic`.
- The spec vocabulary (`spec`): `Spec`, `DynamicSpec`,
  `DynamicInstance`, `PortDecl`, `ParamDecl`, `Parameters`, `port`,
  `required`, `optional`, `parameters`, and the declared
  `ParameterRange` constants (`FINITE_F64`, `POSITIVE_F64`,
  `NONNEGATIVE_F64`, `NONNEGATIVE_INT`, `POSITIVE_INT`, `FRACTION_F64`).
- The per-kind spec data (`specs`): one spec struct per registered
  `dcs-blocks` kind, pinned to the kinds' descriptors by the
  spec-drift test.
- The re-exported contract vocabulary: `Direction`, `PointId`,
  `SignalId`, `Value`, `ValueKind`, `PointType`, `ChannelRef`,
  `ComponentId`, `DeviceId`, `Endpoint`, `PortRef`, `Rationalization`,
  `BuildError`; and from `dcs-model` the `PlantModel` document type,
  `MODEL_VERSION`, `LoadError`, `SignalIndex`, `ModelDiff`,
  `LintFinding`/`LintRule`, and `PlantModel::json_schema`.

`dcs-build`'s `station`, `dosing`, `ijmuiden`, and `ethercat` modules
are **platform-owned reference compositions**: they remain compilable
and usable from a release — the reference plant's station is free to
read them as worked examples — but they are conformance and example
code, not the customer-project boundary. Their signatures and behavior
are not covered by the compatibility policy below; a consumer composes
its own plant from the primitives or copies a pattern, it does not pin
semantics against these helpers. (Decision 81.)

## Release versioning

- One version number covers the whole release set: the workspace's
  `[workspace.package] version` in the root `Cargo.toml`, shared by
  every released crate. There are no per-crate versions.
- A release is a git tag `v<version>` on a `main` commit — `v0.1.0`
  for the first release.
- At `0.x`, cargo's `^0.x` semantics apply: `version = "0.1"` accepts
  `>=0.1.0, <0.2.0`. The policy a consumer can rely on:
  - **Patch bumps** (`0.1.0` → `0.1.1`) keep the supported surface
    compiling, keep `MODEL_VERSION`, and keep the checkpoint format
    set — a repin is a drop-in upgrade.
  - **Minor bumps** (`0.1.x` → `0.2.0`) may change the supported API,
    `MODEL_VERSION`, or the checkpoint format; the release record
    names the breakage and the migration expectation.
  - Additive serde-optional model fields land without a version bump
    (decision 3's optional-field convention): older documents load
    with the field unset, and consumers on an older patch release
    parse newer documents that carry it.

## Model and API compatibility policy

- **Model format version.** `PlantModel.version` is `MODEL_VERSION`
  (currently `1`); `PlantModel::load` accepts exactly that version and
  rejects any other with `LoadError::UnsupportedVersion` naming `found`
  and `supported`. A consumer emitting `version: 1` documents can
  validate them against any release whose `MODEL_VERSION` is `1`.
- **Fingerprint semantics.** `PlantModel::fingerprint` is FNV-1a over
  the model's canonical reserialization — semantic identity, not text
  identity: whitespace and key order do not change it, any semantic
  difference does. Checkpoints carry it, and a peer negotiating a
  checkpoint across models rejects with `FingerprintMismatch`.
- **Checkpoint format.** `Checkpoint.format_version` must be in
  `SUPPORTED_FORMAT_VERSIONS` (`{0, 1}`; absent reads as `0`).
  Restore rejects an unknown version with
  `RestoreError::UnsupportedVersion` before any state applies —
  checkpoint compatibility is negotiated per-connection and never
  assumed from the release version.
- **API compatibility.** At `0.x` the supported surface follows the
  semver expectations above: within a minor series a consumer's
  composition code compiles unchanged; a minor bump may break it and
  says so in the release record.
- **Failure shape.** Incompatible crossings are named diagnostics, not
  silent misbehavior: `LoadError::UnsupportedVersion`,
  `RestoreError::UnsupportedVersion`, `FingerprintMismatch`, cargo's
  `failed to select a version`, and this document's check diagnostics
  below.

## The deployment-manifest shape

A consumer repository owns a deployment manifest — the declaration that
binds its model artifact to the pinned release's images and the
topology decision 46 leaves to deployment. The shape (JSON shown;
YAML is equivalent data):

```json
{
  "dcs_release": "v0.1.0",
  "images": {
    "controller": "dcs-controller@sha256:<digest>",
    "plant_server": "dcs-plant-server@sha256:<digest>"
  },
  "model": {
    "path": "model/plant.json",
    "fingerprint": "<ModelFingerprint of the approved document>"
  },
  "dynamics": { "path": "model/dynamics.json" },
  "plant": { "listen": "0.0.0.0:9001" },
  "controllers": [
    { "name": "ctrl-a", "listen": "0.0.0.0:8080" },
    { "name": "ctrl-b", "listen": "0.0.0.0:8081", "standby": "ctrl-a:8080" }
  ]
}
```

Each field maps to the documented run commands in
`docs/packaging.md`: `model.path`/`dynamics.path` are the mounted
documents, `plant.listen` is the plant server's `--listen`,
`standby` is the tracking peer's `--standby` address, and
`model.fingerprint` is the identity the checkpoint negotiation
verifies on the wire. The shape is a recorded contract, not yet a
schema-enforced document — `reference-plant/deploy/manifest.json`
instantiates it, and the template's `ci/check.sh` ties its recorded
fingerprint to a fresh emit.

## The release procedure

Producing a release is a supervisor-owned git operation; the mechanics
it follows:

1. Land release-affecting changes on `main`; bump
   `[workspace.package] version` when the compat policy requires it.
2. Tag the commit `v<version>`.
3. Produce the release record `docs/releases/<tag>/`:
   - `record.md` — tag, commit sha, the release set's crate versions,
     the schema's sha256, the published image digests, and the compat
     notes the version bump owes.
   - `plant-model.schema.json` — `dcs-model schema` emitted at the tag.
4. Build and publish the `dcs-controller` and `dcs-plant-server`
   images; record their digests in `record.md`.

### The first release

`v0.1.0`, tagged on the `main` commit landing this contract (issue
#348). Its record is landed at `docs/releases/v0.1.0/` — `record.md`
with the fillable fields populated and `plant-model.schema.json`
emitted at the recorded commit, pinned byte-for-byte to `dcs-model
schema`'s output by the drift test in
`crates/dcs-model/tests/schema.rs`. The reference plant pins
`tag = "v0.1.0"` (or the recorded rev) for the crates and the recorded
digests for the images. The tag itself and the record's image-digest
fields are filled when the release is cut.

## The consumer-resolution check

`crates/dcs-build/tests/consumer_release.rs` is the workspace-side
proof that the recorded pin mechanism works. From a clean checkout:

```sh
cargo test -p dcs-build --test consumer_release
```

It writes a scratch consumer crate outside the workspace, pins
`dcs-build` and `dcs-model` to the checkout's `HEAD` commit through a
`file://` remote of this repository (the local stand-in for the
published remote), resolves and builds it, asserts the lockfile
records `git` — never `path` — sources for the release crates, emits
the model twice and compares bytes, then runs `dcs-model validate`,
`dcs-model lint`, and `dcs-controller --check` over the emitted
document. A pinned `rev` works against any commit; consumers pin the
release tag the same way.

`crates/dcs-build/tests/reference_plant.rs` extends the proof to the
full consumer repository: it copies `reference-plant/` to a scratch
directory outside the workspace, rewrites only the dependency remote
to the same `file://` stand-in — the recorded `rev` pin untouched —
and runs the template's own `ci/check.sh` end to end, including its
git-only lockfile assertion, its released-tooling stage against
locally built binaries, the manifest fingerprint check, two runs
of the scripted simulation, the served-operator-surface stage —
the signal index, monitoring page, snapshot descriptors, and journal
the driven controller serves, asserted against the emitted model's
declaration — the `consumers` stage, which replays that driven run
under each consumer schedule — no UI attached, normal polling, a
stalled reader, disconnect/reconnect churn, malformed and flooded
traffic within the declared limits, and a UI process restart —
requiring identical output and receipt digests across the schedules
and across two passes — and the `upgrade` stage, which repins the
materialized tree to the checkout's `HEAD` and re-runs the pipeline
under the repin, requiring byte-identical emitted bytes and refusing
the named incompatible crossings. Its negative cases prove the
template's new stage names surface as the diagnostics below.

```sh
cargo test -p dcs-build --test reference_plant
```

The upgrade half of the compatibility policy is proven by
`crates/dcs-build/tests/consumer_upgrade.rs`:

```sh
cargo test -p dcs-build --test consumer_upgrade
```

It materializes the same scratch consumer pinned at the revision this
document records for `v0.1.0` — the release tag once it exists in the
checkout, until then the commit that landed this contract — and runs
the identical resolve/build/deterministic-emit/released-tooling
pipeline green. It then repins the unchanged consumer source to the
checkout's `HEAD` — a later commit in the same minor series — and
reruns the pipeline, asserting the two pins emit byte-identical model
documents: a divergence is itself the signal this policy asks a
release record to name, and fails named rather than passing silently.
The same target records the incompatible crossings: a document
doctored one version past `MODEL_VERSION` is refused by the released
`dcs-model validate` with `LoadError::UnsupportedVersion` naming the
found and supported versions, and an unresolvable tag name fails
`pin-unresolvable` beside the version-requirement exclusion.

The checks' failures are named diagnostics:

| Diagnostic | Meaning |
|---|---|
| `pin-unresolvable` | The pinned source does not resolve: an unfetchable ref/tag, or a revision whose crates satisfy no declared release requirement (e.g. `version = ">=99"`). |
| `surface-incompatible` | The release crates resolved but the consumer's use of the supported API fails to compile — an incompatible pin reaching compile time. |
| `path-dependency-leak` | The consumer lockfile records a `path` source for a released crate — the no-path-dependency proof itself failed. |
| `emit-nondeterministic` | Two emission runs produced different bytes. |
| `emit-divergent` | The unchanged consumer source emitted different model bytes under the repinned revision — the same-minor repin was not the drop-in upgrade this policy promises. |
| `tooling-rejected` | `dcs-model validate`/`lint` or `dcs-controller --check` refused the emitted model. |
| `crossing-unrefused` | An incompatible crossing this contract names was not refused: the released tooling accepted a document outside `MODEL_VERSION`, or a pin resolved that must not. |
| `stale-artifact` | A checked-in artifact (`model/plant.json`, `ci/scenario.json`) no longer matches a fresh emit — the committed approved document drifted from the composition. Reported by the reference plant's `ci/check.sh`. |
| `manifest-fingerprint-mismatch` | The emitted model's `ModelFingerprint` differs from the `model.fingerprint` the consumer's deployment manifest records — the deployment declaration no longer names the approved model. Reported by the reference plant's `ci/check.sh`. |
| `scenario-failed` | The scripted simulation's declared leg outcomes did not hold against the checked-in model and dynamics. Reported by the reference plant's `ci/check.sh`. |
| `scenario-nondeterministic` | Two scripted-simulation runs produced different outcome digests. Reported by the reference plant's `ci/check.sh`. |
| `surface-mismatch` | The driven controller's served operator surface — the `GET /signals` index, the `GET /` page, the snapshot's `descriptors`, or `GET /journal` — diverged from the emitted model's declared surface. Reported by the reference plant's `ci/check.sh`. |
| `consumer-interference` | A consumer schedule changed the driven run's outputs or command receipts, or the schedule's own evidence failed — a consumer met a server fault, a held response arrived incomplete, malformed traffic went unrefused, or a restarted UI process found no freshness metadata. Reported by the reference plant's `ci/check.sh`, naming the schedule. |
| `consumer-nondeterministic` | Two passes of the consumer-schedule stage produced different digests. Reported by the reference plant's `ci/check.sh`. |
