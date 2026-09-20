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
| `dcs-model` CLI | `validate`, `summary`, `signal-index`, `diff`, `lint`, `schema` over a model document; `interface-schema` emits the served-registry schema. | `cargo install --git <repo> --tag v<X.Y.Z> dcs-model`, or a binary built from the tag |
| `dcs-controller` binary | The generic controller; `--check` is the consumer's assemble-check (load, validate, resolve devices, construct components — no scan). | `cargo install --git <repo> --tag v<X.Y.Z> dcs-controller`, or the container image below |
| `dcs-controller` image | The generic controller container (`Dockerfile`); runs any operator-supplied model (decision 46). | Image digest: `dcs-controller@sha256:<digest>` recorded in the release record; or `docker build` at the tag |
| `dcs-plant-server` image | The shared simulated-plant container (`Dockerfile.plant`) for the consumer's simulation runs. | Image digest recorded in the release record; or `docker build -f Dockerfile.plant` at the tag |
| `dcs-ctl` CLI | The shipped operator CLI for the monitor contract — `invoke` submits a kind-declared command through the bounded receipted path with `--actor` attribution; `resources`/`schema`/`events`/`snapshot`/`signals`/`receipts`/`journal`/`history`/`role` read the served block-interface surface; `write`/`set-parameter`/`force`/`unforce`/`promote`/`demote`/`scan` cover the rest of the receipted and role surface. | `cargo install --git <repo> --tag v<X.Y.Z> dcs-monitor` — the binary ships in the `dcs-monitor` package — or a binary built from the tag |
| `dcs-alarm-report` CLI | The shipped alarm flood and performance report — `dcs-alarm-report <addr>` computes the declared `AlarmReport` metric set over the monitor's served journal; `dcs-alarm-report <addr> --journal-file <path>` computes it over the durable journal file, the restart-surviving dataset; `--config <path>` carries the declared report thresholds. Tooling-side aggregation over the served surface and the file — it adds no served endpoint. | `cargo install --git <repo> --tag v<X.Y.Z> dcs-monitor` — the binary ships in the `dcs-monitor` package — or a binary built from the tag |
| Plant-model JSON Schema | `dcs-model schema`'s emitted draft 2020-12 schema for non-Rust tooling (decision 40). | Recorded in the release record at `docs/releases/<tag>/plant-model.schema.json`, fetchable at the tag; its sha256 is in the record |
| Served-registry JSON Schema | `dcs-model interface-schema`'s emitted draft 2020-12 schema for the `GET /schema` block-interface registry document (decision 82's served contract) — a non-Rust consumer checks the served surface against it. | Recorded in the release record at `docs/releases/<tag>/block-interfaces.schema.json`, fetchable at the tag; its sha256 is in the record |
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
  `BuildError`; from `dcs-core` the served-registry types
  (`SchemaView`, `BlockInterface`, `INTERFACE_VERSION`) and
  `SchemaView::json_schema`; and from `dcs-model` the `PlantModel`
  document type, `MODEL_VERSION`, `LoadError`, `SignalIndex`,
  `ModelDiff`, `LintFinding`/`LintRule`, and `PlantModel::json_schema`.

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
    {
      "name": "ctrl-a",
      "listen": "0.0.0.0:8080",
      "state_file": "/var/tmp/state.json",
      "journal_file": "/var/tmp/journal.jsonl"
    },
    {
      "name": "ctrl-b",
      "listen": "0.0.0.0:8081",
      "standby": "ctrl-a:8080",
      "state_file": "/var/tmp/state.json",
      "journal_file": "/var/tmp/journal.jsonl"
    }
  ]
}
```

Each field maps to the documented run commands in
`docs/packaging.md`: `model.path`/`dynamics.path` are the mounted
documents, `plant.listen` is the plant server's `--listen`,
`standby` is the tracking peer's `--standby` address, and
`model.fingerprint` is the identity the checkpoint negotiation
verifies on the wire. A controller entry's `state_file` and
`journal_file` are **optional** per-controller container paths
carried to the invocation's `--state-file` and `--journal-file`
flags: `state_file` is decision 35's restart-recovery checkpoint — a
container restart resumes in place at the last persisted scan — and
`journal_file` is decision 36's durable journal, the attributed
operator-action record that survives the process lifetime. Each
declared path must live on writable deployment storage — a named
volume in the checked-in rig definition — while the model and
dynamics mounts stay read-only; a consumer without durable storage
omits both fields, and the flags are then absent. The shape is a
recorded contract, not yet a
schema-enforced document — `reference-plant/deploy/manifest.json`
instantiates it, `reference-plant/deploy/compose.yaml` instantiates
the manifest itself as a checked-in rig definition (the consumer-side
counterpart of this repository's `compose.yaml`), and the template's
`ci/check.sh` ties its recorded fingerprint to a fresh emit and its
`deploy` stage asserts the definition and the manifest agree on every
field — images, mounts, fingerprint, addresses, and pair wiring.

## The release procedure

Producing a release is a supervisor-owned git operation; the mechanics
it follows:

1. Land release-affecting changes on `main`; bump
   `[workspace.package] version` when the compat policy requires it.
2. Tag the commit `v<version>`.
3. Produce the release record `docs/releases/<tag>/`:
   - `record.md` — tag, commit sha, the release set's crate versions,
     each recorded schema's sha256, the published image digests, and
     the compat notes the version bump owes.
   - `plant-model.schema.json` — `dcs-model schema` emitted at the tag.
   - `block-interfaces.schema.json` — `dcs-model interface-schema`
     emitted at the tag.
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
fields are filled when the release is cut. `v0.1.0`'s recorded commit
predates the served block-interface registry (#375), so its record
carries no `block-interfaces.schema.json`; release records carry it
from the first tag whose tooling emits it.

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
locally built binaries — `validate`/`lint`/`--check` acceptance, the
`dcs-model schema` and `interface-schema` emissions pinned
byte-identical to the release record's schema artifacts fetched from
the pinned revision through the same git remote, `dcs-model diff`
legs over a doctored compatible revision and the identical document,
and `summary`/`signal-index` outputs recorded as run evidence — the
manifest fingerprint check, two runs
of the scripted simulation, the served-operator-surface stage —
the signal index, monitoring page, snapshot descriptors, and journal
the driven controller serves, asserted against the emitted model's
declaration, and the `GET /schema` document's structural conformance
to the fetched block-interfaces artifact (a required-keys/field-shape
check in stdlib-only python — full draft-2020-12 validation of the
served document stays workspace-side, where the `jsonschema`
dependency exists) — the `pair` stage, which runs the
manifest-declared standby pair on the released tooling: the second
controller converging to `tracking` through `GET /role`, scans driven
through `POST /scan` keeping the peers' images identical, a receipted
`demote`/`promote` switching the roles, and the run continuing
bumplessly with the adopted receipts and the durable journal files'
transition records intact — plus the pair contract's refusal half on
the same declared deployment: `POST /promote` on a freshly launched
standby before its first transfer answering the named `not_converged`
refusal with no field hand-off, a receipted write against a declared
writable point submitted to the tracking standby's monitor answering
the named `not_active` rejection with the point unchanged in the
active's served snapshot and no command-side journal entry on either
peer recording it as anything but the refusal, and the same promote
succeeding once the standby tracks — the active's field writes,
receipts, and journal undisturbed throughout — the `consumers` stage,
which replays that driven run
under each consumer schedule — no UI attached, normal polling, a
stalled reader, disconnect/reconnect churn, malformed and flooded
traffic within the declared limits, and a UI process restart —
requiring identical output and receipt digests across the schedules
and across two passes — the `ctl` stage, which drives the same run
through the released `dcs-ctl` operator CLI: `invoke` settling an
applied receipt visible through `receipts` and the journal,
`resources` reporting the per-command availability and the named
refusals, the read subcommands answering the served contract, and the
refusal modes exiting nonzero — the pair stage's report leg, which
runs the released `dcs-alarm-report` over the driven pair's record:
one managed alarm driven through its annunciation/ack/return
lifecycle, the declared `AlarmReport` metric set asserted over the
field owner's served journal and over its manifest-declared durable
journal file alike — the file's report answering the served report's
metrics identically with only the run-boundary accounting added — and
the unreachable-monitor and unreadable-file refusals exiting nonzero —
the pair stage's emit-identical leg, which drives the emitted
model's sequencer through the field owner's receipted path until a
counted `step_completed` set stands and asserts both peers'
`GET /resources` views serve the same routed `event_emitted`
records — identical component attribution, declared identities,
ordered fields, tick, and retention — while the standby reports
`tracking` and its writes stay gated — and
the `upgrade` stage, which repins
the materialized tree to the checkout's `HEAD` and re-runs the pipeline
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
| `schema-drift` | `dcs-model schema` or `dcs-model interface-schema` at the pinned rev did not emit the release record's recorded artifact bytes (`plant-model.schema.json` / `block-interfaces.schema.json`) — the emitted schema drifted from what the release record pins. Reported by the reference plant's `ci/check.sh`. |
| `schema-mismatch` | The driven run's `GET /schema` document failed the recorded artifact's structural conformance — a required field absent or mistyped, a vocabulary outside its `enum`/`const`, or an undeclared field under `additionalProperties: false`. Reported by the reference plant's `ci/check.sh`. |
| `diff-mismatch` | A `dcs-model diff` leg's expectation failed — a revised document's actual differences went unnamed, the identical document reported differences, or a document outside `MODEL_VERSION` was diffed instead of refused. Reported by the reference plant's `ci/check.sh`. |
| `crossing-unrefused` | An incompatible crossing this contract names was not refused: the released tooling accepted a document outside `MODEL_VERSION`, or a pin resolved that must not. |
| `stale-artifact` | A checked-in artifact (`model/plant.json`, `ci/scenario.json`) no longer matches a fresh emit — the committed approved document drifted from the composition. Reported by the reference plant's `ci/check.sh`. |
| `manifest-fingerprint-mismatch` | The emitted model's `ModelFingerprint` differs from the `model.fingerprint` the consumer's deployment manifest records — the deployment declaration no longer names the approved model. Reported by the reference plant's `ci/check.sh`. |
| `scenario-failed` | The scripted simulation's declared leg outcomes did not hold against the checked-in model and dynamics. Reported by the reference plant's `ci/check.sh`. |
| `scenario-nondeterministic` | Two scripted-simulation runs produced different outcome digests. Reported by the reference plant's `ci/check.sh`. |
| `surface-mismatch` | The driven controller's served operator surface — the `GET /signals` index, the `GET /` page, the `GET /schema` block-interface registry's coverage of the declared kinds, a kind-declared command's structured receipt through `POST /command`, a kind-emitted event's arrival in `GET /journal`/`GET /resources`, the snapshot's `descriptors`, or `GET /journal` — diverged from the emitted model's declared surface. Reported by the reference plant's `ci/check.sh`. |
| `consumer-interference` | A consumer schedule changed the driven run's outputs or command receipts, or the schedule's own evidence failed — a consumer met a server fault, a held response arrived incomplete, malformed traffic went unrefused, or a restarted UI process found no freshness metadata. Reported by the reference plant's `ci/check.sh`, naming the schedule. |
| `consumer-nondeterministic` | Two passes of the consumer-schedule stage produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `restart-resume-failed` | The restart-recovery leg did not hold: the relaunched controller did not resume at the persisted tick (a missing state file's cold start included), its leg outcomes, receipts, or field image diverged from the uninterrupted reference pass, the journal's `seq` order did not continue across the run-boundary marker, or an unparseable state file failed startup without the named refusal. Reported by the reference plant's `ci/check.sh`. |
| `restart-resume-nondeterministic` | Two passes of the restart-recovery leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `ctl-failed` | The released `dcs-ctl` leg did not hold against the driven run: an `invoke` did not settle its applied receipt through `receipts` and the journal, `resources` did not report a command's availability or its named refusal, a refusal mode exited zero or unnamed, or a read subcommand did not answer the served contract. Reported by the reference plant's `ci/check.sh`, with the leg's evidence lines on stderr. |
| `ctl-nondeterministic` | Two passes of the `dcs-ctl` leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `pair-failed` | The redundant-pair leg did not hold: the manifest-declared standby did not converge to `tracking`, the peers' images or adopted receipt logs diverged, the receipted `demote`/`promote` switch did not answer its named reports or refusals, the run did not continue bumplessly, or a declared `--state-file`/`--journal-file` path was not honored — the durable records missing the run's transitions or `seq` order. Reported by the reference plant's `ci/check.sh`. |
| `pair-nondeterministic` | Two passes of the redundant-pair leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `refusal-failed` | The pair contract's refusal half did not hold: a `POST /promote` on the freshly launched standby before its first transfer did not answer the named `not_converged` refusal or handed the field off, a receipted write to the tracking standby's monitor did not answer the named `not_active` rejection — or moved the point in the active's served snapshot, entered a peer's adopted receipt log, or left a journal entry recording it as anything but the refusal — the same promote did not succeed once the standby tracked, or the active's field writes, receipts, or journal did not run undisturbed. Reported by the reference plant's `ci/check.sh`. |
| `refusal-nondeterministic` | Two passes of the role-gated refusal leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `takeover-failed` | The pair contract's manual-takeover leg did not hold: a receipted `mode`/`hand`/`oos` write did not answer `accepted` or settle `applied` into both peers' adopted log, the pump's delivered command did not leave the group's `cmd_1` on the manual selection or the pump-group status did not reflect the exclusion, the operator demand did not run the pump under the declared thermal/moisture guards, the plant-protocol protection input did not assert the proven fault and its managed alarm, the out-of-service write did not assert the maintenance inhibit, the restore did not return the pump to group control, or the served journal did not carry each attributed transition in order. Reported by the reference plant's `ci/check.sh`. |
| `takeover-nondeterministic` | Two passes of the manual-takeover leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `force-carryover-failed` | The pair contract's force-carryover leg did not hold: a receipted `force_point` on a declared writable `In` point did not answer `accepted` or settle `applied` into both peers' adopted log, the snapshot's `forces` entry or the `Uncertain(Substituted)` sample did not appear on a peer while tracking, the promoted peer did not keep the force — the `forces` entry dropped or the sample no longer the forced value at substituted quality — a receipted `unforce_point` on the new active did not settle `applied`, empty the `forces` list, and resume the point's unforced serve, or the pair was not left in its declared roles. Reported by the reference plant's `ci/check.sh`. |
| `force-carryover-nondeterministic` | Two passes of the force-carryover leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `tune-carryover-failed` | The pair contract's tune-carryover leg did not hold: a receipted `set_parameter` on a declared writable configuration parameter did not answer `accepted` or settle `applied` into both peers' adopted log, the tuned value did not appear in both peers' served parameter report or in the bound point's declared-signal reading while tracking, the promoted peer did not keep the tuned value on its served snapshot/signal surface after the `demote`/`promote` switch, the promoted peer's served journal did not order the promotion's `role_changed` entries after the tune's `command_settled` entry, a further `set_parameter` on the new active did not settle `applied` with a fresh receipt ordered after the promotion, or the pair was not left in its declared roles. Reported by the reference plant's `ci/check.sh`. |
| `tune-carryover-nondeterministic` | Two passes of the tune-carryover leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `force-release-failed` | The pair contract's force-release leg did not hold: a receipted `force_point` on a declared writable `In` point did not answer `accepted`, settle `applied` at the applying scan's tick into both peers' adopted log, or leave the `Uncertain(Substituted)` sample badged on both peers across scans, the journaled settlement missing from the active's record; a receipted `unforce_point` did not settle `applied` at the release's apply tick, empty the `forces` set, and resume the point's live serve — the held image re-stamped `Good`, never re-substituted; the restore write did not land the pre-force held value; the promoted peer resurrected the released force or dropped a settled receipt; the pair was not restored to its launch roles; or the field owner's durable journal file did not carry each attributed transition in `seq` order with the standby's adopted log answering the same receipts. Reported by the reference plant's `ci/check.sh`. |
| `force-release-nondeterministic` | Two passes of the force-release leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `peer-announce-failed` | The pair contract's peer-announce leg did not hold: a foreign `GET /checkpoint?peer=<addr>` naming a source the pulling connection does not own disturbed the checkpoint read or landed as the demotion tracking source — the demoted field owner stranding `unsynchronized` instead of reconverging `tracking` on its real successor — or the pair was not left in its declared roles. Reported by the reference plant's `ci/check.sh`. |
| `peer-announce-nondeterministic` | Two passes of the peer-announce leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `availability-failed` | The pair contract's command-availability leg did not hold: a `GET /resources` command row was not self-consistent — an `available: false` row without a named refusal or an `available` row carrying one — a served-unavailable bound-point-writable command's `POST /command` submission did not settle a named rejection — or settled a different refusal than the row served on a declared bound point — a served-available command did not settle `applied` into both peers' adopted receipt log, a kind-declared command's standing refusal did not echo verbatim through the settled `command_refused` where the tooling publishes verdicts, or the tracking standby reported different verdicts than the field owner. Reported by the reference plant's `ci/check.sh`. |
| `availability-nondeterministic` | Two passes of the command-availability leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `failover-failed` | The pair contract's automatic-failover leg did not hold: the manifest-declared `failover_budget` did not arm the standby's `--auto-promote`, the field-owning controller's severance left the surviving peer's miss run unreported — `GET /role` not serving `standby` under the `degraded` sync state — the self-promotion did not land at the declared budget's scan boundary settling `active`, the plant's writer claim did not fence a foreign attachment while the promoted peer's writes landed — or moved off a recorded dead owner's token — subsequent driven scans or a receipted kind-declared command did not continue, the promoted peer's durable `--journal-file` did not carry `standby → promoting → active` in `seq` order attributed after the budget expiry — or recorded an operator actor on the automatic switch where no non-operator marker has landed — or the standby-severed variant disturbed the active's writes or reported a failover. Reported by the reference plant's `ci/check.sh`. |
| `failover-nondeterministic` | Two passes of the automatic-failover leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `divergence-missed` | The pair contract's staged-vs-field divergence leg did not hold: the withheld checkpoint pulls did not leave the tracking standby partitioned through the observation window, the field-side write through the run's dedicated plant-protocol client — the field's writer claim joined under the duty's recorded owner token — did not land the perturbed output in the field, the resumed pull did not leave the stale peer's served `GET /role` reporting `standby` under the `diverged` sync state naming the diverging output with both sides' values, the standby's served or durable journal did not carry the `divergence_detected` record, `POST /promote` did not answer the named `not_converged` refusal or handed the field the stale image — a role transition landing on either peer's journal, the plant's writer claim moving, or the injected write overwritten — the active's field writes, settled receipts, or journal did not run undisturbed, the duty's continued writes did not restore the field so the standby's next same-tick comparison resolved the verdict, or the write-free control window did not reconverge to `tracking` and promote normally. Reported by the reference plant's `ci/check.sh`. |
| `divergence-nondeterministic` | Two passes of the staged-vs-field divergence leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `standby-restart-failed` | The pair contract's standby-restart leg did not hold: the tracking standby's restart onto its declared `--state-file`/`--journal-file` did not resume at the persisted tick (a missing state file's cold start included), the resumed peer did not rejoin in `standby` and reconverge to `tracking` inside the leg's declared window, the durable journal's restart boundary did not order after the pre-restart entries with `seq` order intact, the active's field writes, receipts, or journal did not run undisturbed across the restart, or the pair did not promote afterward. Reported by the reference plant's `ci/check.sh`. |
| `standby-restart-nondeterministic` | Two passes of the standby-restart leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `report-failed` | The released `dcs-alarm-report` leg did not hold: the tool shipped no binary, the served-journal report did not cover the emitted model's alarm set per instance or did not measure the driven lifecycle's activation, annunciation, and attributed acknowledgment, the report's cross-section accounting disagreed with itself or the served record's stretch, the durable journal file's report diverged from the served journal's or miscounted the run's lifetimes, or a refusal mode — an unreachable monitor, an unreadable journal file — exited zero or unnamed. Reported by the reference plant's `ci/check.sh`, with the leg's evidence lines on stderr. |
| `report-nondeterministic` | Two passes of the `dcs-alarm-report` leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `command-switch-failed` | The pair contract's command-switch leg did not hold: the release tooling shipped no `dcs-ctl` binary, the exercise sequencer's kind-declared `advance` invoked through the released `dcs-ctl invoke` did not settle `applied` with exactly one `command_settled` journal entry on the serving peer — before the switch on the field owner, after it on the promoted peer, each settlement attributed to the peer that served it with the old peer's settlement replayed on neither side — the promoted peer's emitted `step_completed` record did not continue the pre-promotion entries in tick order with unchanged component attribution or re-emitted one, the `advance` submitted immediately before the restore switch did not settle exactly once `applied` on the new active — lost at the boundary or double-applied — or the pair was not left in its declared roles. Reported by the reference plant's `ci/check.sh`, with the leg's evidence lines on stderr. |
| `command-switch-nondeterministic` | Two passes of the command-switch leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `event-parity-failed` | The pair contract's emit-identical standby leg did not hold: the tracking peer never converged, the counted `step_completed` set never stood on the active, the standby's served `event_emitted` records diverged from the active's — a missing record, a re-attributed component, a changed identity, ordered field, tick, or retention — or the standby's role moved or its writes went ungated. Reported by the reference plant's `ci/check.sh`. |
| `event-parity-nondeterministic` | Two passes of the emit-identical standby leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `rig-invalid` | The consumer's checked-in rig definition does not parse — `docker compose config` or the fallback YAML parser rejected it. Reported by the reference plant's `ci/check.sh`. |
| `rig-unverifiable` | The rig-definition consistency check could not run: neither `docker compose` nor PyYAML is available to parse the definition. Reported by the reference plant's `ci/check.sh`. |
| `rig-mismatch` | The consumer's checked-in rig definition diverges from its deployment manifest — images, mounted model or dynamics paths, the propagated model fingerprint, listen addresses, the controller pair's standby wiring, the standby's declared `failover_budget` against its `--auto-promote` flag (a declared budget with no flag, a flag with no declaration, a diverging value, or the field placed on the duty entry), or the declared persistence paths' mounts and flags disagree with what the manifest declares. Reported by the reference plant's `ci/check.sh`. |
| `<leg>-unchecked` | The paired self-check diagnostic every checked leg carries: `ci/check.sh` plants a negative case for each leg — a doctored input or tampered expectation the leg must refuse with its named diagnostic — and reports `<leg>-unchecked`, formed on the leg stem its failure (`<leg>-failed` or, for the divergence leg, `<leg>-missed`) and `<leg>-nondeterministic` diagnostics share, when the planted case passes or the leg answers a name other than its declared one (e.g. a drifted record artifact passing the interface-schema non-drift leg reports `schema-drift-unchecked`, not `schema-drift`). It is distinct from `<leg>-failed`: the failed diagnostic is the leg's contract check failing on a real divergence; the unchecked diagnostic is the leg's own negative self-test failing — the leg can no longer be trusted to catch what it names. Reported by the reference plant's `ci/check.sh`; the emitted set grows with each leg that plants a negative case — currently `schema-drift-unchecked`, `diff-mismatch-unchecked`, `rig-mismatch-unchecked`, `restart-resume-unchecked`, `schema-mismatch-unchecked`, `pair-unchecked`, `refusal-unchecked`, `takeover-unchecked`, `force-carryover-unchecked`, `tune-carryover-unchecked`, `force-release-unchecked`, `burst-order-unchecked`, `peer-announce-unchecked`, `availability-unchecked`, `failover-unchecked`, `divergence-unchecked`, `standby-restart-unchecked`, `report-unchecked`, `command-switch-unchecked`, `event-parity-unchecked` — and this convention entry declares each new name without a per-leg row. |
