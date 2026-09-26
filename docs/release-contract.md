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
| `dcs-model` CLI | `validate`, `summary`, `signal-index`, `diff`, `lint`, `schema` over a model document; `interface-schema` emits the served-registry schema; `deploy-schema` emits the deployment-manifest schema. | `cargo install --git <repo> --tag v<X.Y.Z> dcs-model`, or a binary built from the tag |
| `dcs-controller` binary | The generic controller; `--check` is the consumer's assemble-check (load, validate, resolve devices, construct components — no scan). | `cargo install --git <repo> --tag v<X.Y.Z> dcs-controller`, or the container image below |
| `dcs-controller` image | The generic controller container (`Dockerfile`); runs any operator-supplied model (decision 46). | Image digest: `dcs-controller@sha256:<digest>` recorded in the release record; or `docker build` at the tag |
| `dcs-plant-server` image | The shared simulated-plant container (`Dockerfile.plant`) for the consumer's simulation runs. | Image digest recorded in the release record; or `docker build -f Dockerfile.plant` at the tag |
| `dcs-ctl` CLI | The shipped operator CLI for the monitor contract — `invoke` submits a kind-declared command through the bounded receipted path with `--actor` attribution; `resources`/`schema`/`events`/`snapshot`/`signals`/`receipts`/`journal`/`history`/`role` read the served block-interface surface; `write`/`set-parameter`/`force`/`unforce`/`promote`/`demote`/`scan` cover the rest of the receipted and role surface. | `cargo install --git <repo> --tag v<X.Y.Z> dcs-monitor` — the binary ships in the `dcs-monitor` package — or a binary built from the tag |
| `dcs-alarm-report` CLI | The shipped alarm flood and performance report — `dcs-alarm-report <addr>` computes the declared `AlarmReport` metric set over the monitor's served journal; `dcs-alarm-report <addr> --journal-file <path>` computes it over the durable journal file, the restart-surviving dataset; `--config <path>` carries the declared report thresholds. Tooling-side aggregation over the served surface and the file — it adds no served endpoint. | `cargo install --git <repo> --tag v<X.Y.Z> dcs-monitor` — the binary ships in the `dcs-monitor` package — or a binary built from the tag |
| Plant-model JSON Schema | `dcs-model schema`'s emitted draft 2020-12 schema for non-Rust tooling (decision 40). | Recorded in the release record at `docs/releases/<tag>/plant-model.schema.json`, fetchable at the tag; its sha256 is in the record |
| Served-registry JSON Schema | `dcs-model interface-schema`'s emitted draft 2020-12 schema for the `GET /schema` block-interface registry document (decision 82's served contract) — a non-Rust consumer checks the served surface against it. | Recorded in the release record at `docs/releases/<tag>/block-interfaces.schema.json`, fetchable at the tag; its sha256 is in the record |
| Dynamics-document JSON Schema | `dcs-plant-server --dynamics-schema`'s emitted draft 2020-12 schema for the `--dynamics`/`--check-dynamics` declaration list (decision 24's document, decision 93's emission) — a non-Rust consumer checks the dynamics document the manifest names against it before the merge's own validation runs. | Recorded in the release record at `docs/releases/<tag>/dynamics.schema.json`, fetchable at the tag; its sha256 is in the record |
| Deployment-manifest JSON Schema | `dcs-model deploy-schema`'s emitted draft 2020-12 schema for the consumer-owned deployment declaration — the documented shape below (decision 96's emission) — a non-Rust consumer screens its manifest against it before the deploy stage's rig agreement check runs. | Recorded in the release record at `docs/releases/<tag>/deploy-manifest.schema.json`, fetchable at the tag; its sha256 is in the record |
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
  `ModelDiff`, `LintFinding`/`LintRule`, `PlantModel::json_schema`,
  and `deployment_manifest_schema` — the emitted deployment-manifest
  schema behind `dcs-model deploy-schema`.

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
  "dynamics": {
    "path": "model/dynamics.json",
    "fingerprint": "<canonical dynamics fingerprint of the approved document>"
  },
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
verifies on the wire. `dynamics.fingerprint` is **optional** and has
no wire role — the dynamics document is simulation internals the
plant server alone consumes, and decision 46 keeps content off the
wire — so it is a check-side authorization: the same canonical
fingerprint contract the model carries (FNV-1a over the parsed
document's reserialization, the sixteen-hex-digit shape), naming the
dynamics document the deployment's `dynamics.path` serves. When the
field is recorded, the consumer's check holds the served document —
the pair launched on the declared path — and the checked-in artifact
equal to it; a manifest omitting the field declares no dynamics pin.
A controller entry's `state_file` and
`journal_file` are **optional** per-controller container paths
carried to the invocation's `--state-file` and `--journal-file`
flags: `state_file` is decision 35's restart-recovery checkpoint — a
container restart resumes in place at the last persisted scan — and
`journal_file` is decision 36's durable journal, the attributed
operator-action record that survives the process lifetime. Each
declared path must live on writable deployment storage — a named
volume in the checked-in rig definition — while the model and
dynamics mounts stay read-only; a consumer without durable storage
omits both fields, and the flags are then absent. What the shape
deliberately never records is the pair's `--pair-token`: the shared
tracking secret the keyed announced-source contract runs on is a
deployment secret, carried on the invocation alone — the reference
rig definition shows the flag with a demonstration value — never in
the checked-in declaration. An optional
top-level `topology` section declares the deployment's named
redundant pairs — the plant-index artifact decision 47 deferred —
beyond the single-pair default:
`"topology": {"pairs": [{"name": "<pair>", "members": ["<controller>",
"<controller>"]}]}`. Each member names a `controllers` entry,
memberships stay disjoint across pairs, and the pair's standby
wiring closes inside it — one member tracking the other; the
reference rig's `deploy` stage fails `rig-mismatch` on a member the
manifest does not declare, a shared member, or wiring that leaves
the declared pair. Each declared pair deploys its members against
the manifest's one (model, plant) binding — one deployment is one
field, whose single-writer claim admits exactly one field-owning
duty run — so decision 99's bound refuses a second pair's duty
member, like any second `controllers` entry without `standby`, as
`rig-mismatch`; a site running several pairs composes one manifest
per field and the overview's `?pair=` URL names addresses across
them. A single-pair manifest omits the section: URL
`?pair=` configuration remains the interface the overview consumes,
and the section carries no runtime, wire, or persisted-format
change. The shape is a schema-enforced
document: `dcs-model deploy-schema` emits its draft 2020-12 JSON
Schema — recorded in the release record as
`deploy-manifest.schema.json`, its sha256 pinned like the other
recorded schemas — covering structure, field types, and the
membership shape the schema vocabulary can express; the referential
rules above (members naming declared controllers, disjoint
memberships, the pair's wiring closing inside it, and the
one-duty-per-deployment bound) stay check-side
with the `deploy` stage's rig agreement, the same split the model
schema records for its cross-reference limits.
`reference-plant/deploy/manifest.json`
instantiates it, `reference-plant/deploy/compose.yaml` instantiates
the manifest itself as a checked-in rig definition (the consumer-side
counterpart of this repository's `compose.yaml`), and the template's
`ci/check.sh` ties its recorded fingerprint to a fresh emit and to the
model digest the declared pair serves through `GET /checkpoint`, and its
`deploy` stage asserts the definition and the manifest agree on every
field — images, mounts, fingerprint, addresses, and pair wiring.

## The release procedure

Producing a release is a supervisor-owned git operation; the mechanics
it follows:

1. Land release-affecting changes on `main`; bump
   `[workspace.package] version` when the compat policy requires it.
   The `rust-proofs` release-assembly check — the nested clean-target
   consumer and reference-plant proofs — runs on every `main` push, so
   each candidate commit arrives with its assembly evidence.
2. Tag the commit `v<version>` only when `rust-proofs` is green on that
   exact commit; the tag push reruns the gate as final confirmation.
3. Produce the release record `docs/releases/<tag>/`:
   - `record.md` — tag, commit sha, the release set's crate versions,
     each recorded schema's sha256, the published image digests, and
     the compat notes the version bump owes.
   - `plant-model.schema.json` — `dcs-model schema` emitted at the tag.
   - `block-interfaces.schema.json` — `dcs-model interface-schema`
     emitted at the tag.
   - `dynamics.schema.json` — `dcs-plant-server --dynamics-schema`
     emitted at the tag.
   - `deploy-manifest.schema.json` — `dcs-model deploy-schema`
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
carries no `block-interfaces.schema.json`, and `v0.2.0`'s predates
the dynamics-document schema emission (#870), so neither record
carries `dynamics.schema.json`; both likewise predate the
deployment-manifest schema emission (#909), so neither carries
`deploy-manifest.schema.json`; release records carry each schema
artifact from the first tag whose tooling emits it.

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
manifest fingerprint check, which authorizes the served bytes: the
emitted model's fingerprint held equal to the recorded
`model.fingerprint`, the deployed pair's served model digest — each
peer's checkpoint-stamped `model_fingerprint` on the
manifest-declared deployment — held equal to it, a doctored served
document with renumbered point ids over identical components
reporting the named mismatch with the expected vs served fingerprint
and the first diverging section, and the dynamics document the
manifest-declared pair serves — launched on the deployment's
declared `dynamics.path`, the served point census answering —
fingerprinted canonically and held equal to the recorded
`dynamics.fingerprint` and the checked-in artifact, a doctored served
document with renumbered point references over identical element
content reporting the named mismatch with the expected vs served
fingerprint and the first diverging element — two runs
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
transition records intact — the stage's negotiation leg then launching
a third released standby on a foreign-fingerprint model document and
asserting the deployed pair degrades honestly: the peer reporting the
named non-converged `degraded` state through `GET /role`, `POST
/promote` answering `409 not_converged` carrying that state, the
active's writes, receipts, and journal undisturbed throughout, and a
control peer on the pair's own model converging and promoting
normally — plus the pair contract's refusal half on
the same declared deployment: `POST /promote` on a freshly launched
standby before its first transfer answering the named `not_converged`
refusal with no field hand-off, a receipted write against a declared
writable point submitted to the tracking standby's monitor answering
the named `not_active` rejection with the point unchanged in the
active's served snapshot and no command-side journal entry on either
peer recording it as anything but the refusal, and the same promote
succeeding once the standby tracks — the active's field writes,
receipts, and journal undisturbed throughout — and the pair
contract's failure-handover leg on the same declared deployment: with
the pair settled and the group holding a duty demand, a proven
duty-pump field-channel fault injected through the plant protocol's
declared `inject_fault` handing `duty` to the standby pump inside the
declared bound — `staged` reporting the surviving pump against the
standing demand, the faulted pump's `fault`/`avail` reporting the
exclusion, and the managed fault alarm annunciating with journaled
`point_changed` evidence — the remaining pump's faulted channel then
annunciating `none_available`/`all_faulted` with their managed alarms,
each restored input producing the declared recovery, and the pair's
controller roles unmoved throughout — the `consumers` stage,
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
the pair stage's managed-lifecycle leg, which exercises the emitted
model's whole managed-alarm surface on the deployed pair: a
field-driven activation asserting `alarm`/`unacknowledged` with the
journaled record, the receipted `ack` clearing the latch under the
leg's actor, the bounded shelve reporting `shelved` and
auto-releasing at the declared `max_shelve_ticks`, the
never-shelvable shelve write answering the named `not_writable`
refusal with no state changed, and the pump's `oos` drive reporting
the declared `out_of_service`/`suppressed` wiring through the
suppressed trip and the return to service — every transition
receipted and attributed, the durable journal carrying the ordered
record, the pair's roles and driven inputs restored — the pair
stage's emit-identical leg, which drives the emitted model's
sequencer through the field owner's receipted path until a counted
`step_completed` set stands and asserts both peers' `GET /resources`
views serve the same routed `event_emitted` records — identical
component attribution, declared identities, ordered fields, tick,
and retention — while the standby reports `tracking` and its writes
stay gated — the pair stage's commissioning/handover record leg,
which materializes the record
`docs/releases/commissioning-record.md` declares from one
deterministic driven run on the declared deployment: the plant
protocol's point census audited against the emitted model's declared
channel set (the I/O checkout record), the declared measurement
driven at marks across the threshold chain's span and the receipted
`mode`/`hand` path exercising the output loop through the field's
delivered command and returned run feedback (the loop-check
evidence), every managed alarm instance's declared record audited
served verbatim on both peers (the alarm rationalization sign-off),
the documented `demote`/`promote` switch and restore (the handover
procedure), and the deployment's document set digested with each
peer's checkpoint-stamped fingerprint and durable journal/state
files (the documentation turnover) — the completeness audit failing
any record missing a named artifact by name, two passes producing
identical digests — and the `upgrade` stage, which materializes
the tree at the previous release's recorded rev, repins it to the
recorded release — under the workspace proof, the stand-in's release
tag resolving to the checkout's release-candidate commit — and re-runs
the pipeline under the repin, requiring byte-identical emitted bytes
and refusing the named incompatible crossings. Its negative cases
prove the template's new stage names surface as the diagnostics below.

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
| `record-missing` | The substituted `DCS_RECORD_DIR` tree serves no `docs/releases/<tag>/` artifact the schema-drift leg compares against — only reachable when a record-tree substitution is in effect; the contract's own shape fetches the record through the pinned rev and reports `pin-unresolvable` instead. Reported by the reference plant's `ci/check.sh`. |
| `surface-incompatible` | The release crates resolved but the consumer's use of the supported API fails to compile — an incompatible pin reaching compile time. |
| `path-dependency-leak` | The consumer lockfile records a `path` source for a released crate — the no-path-dependency proof itself failed. |
| `emit-nondeterministic` | Two emission runs produced different bytes. |
| `emit-divergent` | The unchanged consumer source emitted different model bytes under the repinned revision — the same-minor repin was not the drop-in upgrade this policy promises. |
| `tooling-rejected` | `dcs-model validate`/`lint` or `dcs-controller --check` refused the emitted model. |
| `schema-drift` | `dcs-model schema` or `dcs-model interface-schema` at the pinned rev did not emit the release record's recorded artifact bytes (`plant-model.schema.json` / `block-interfaces.schema.json`) — the emitted schema drifted from what the release record pins. Reported by the reference plant's `ci/check.sh`. |
| `schema-mismatch` | The driven run's `GET /schema` document failed the recorded artifact's structural conformance — a required field absent or mistyped, a vocabulary outside its `enum`/`const`, or an undeclared field under `additionalProperties: false`. Reported by the reference plant's `ci/check.sh`. |
| `diff-mismatch` | A `dcs-model diff` leg's expectation failed — a revised document's actual differences went unnamed, the identical document reported differences, or a document outside `MODEL_VERSION` was diffed instead of refused. Reported by the reference plant's `ci/check.sh`. |
| `alarm-validation-failed` | The alarm-validation leg did not hold: a managed alarm instance in the emitted model lacked its decision-70 record — the `rationalization` prose or the `priority`/`class`/`response_ticks` codes — a doctored copy of the emitted document was not refused by the released `dcs-controller --check` with a rejection naming the missing element (a silent load or an advisory-only finding), or the driven run's served `components`/`parameters` sections did not report the declared record. Reported by the reference plant's `ci/check.sh`, with the leg's `alarm-validation: …` evidence lines on stderr. |
| `alarm-validation-nondeterministic` | Two passes of the alarm-validation leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `crossing-unrefused` | An incompatible crossing this contract names was not refused: the released tooling accepted a document outside `MODEL_VERSION`, or a pin resolved that must not. |
| `stale-artifact` | A checked-in artifact (`model/plant.json`, `ci/scenario.json`) no longer matches a fresh emit — the committed approved document drifted from the composition. Reported by the reference plant's `ci/check.sh`. |
| `manifest-fingerprint-mismatch` | The emitted model's `ModelFingerprint` differs from the `model.fingerprint` the consumer's deployment manifest records — or the deployed pair's served model digest differs from it: a peer's checkpoint-stamped `model_fingerprint` not equal to the recorded fingerprint, the diagnostic naming the diverging peer, the expected and served fingerprints, and the first top-level document section the served model diverges in (a silent point-id renumbering over identical components included) — or the dynamics fingerprint diverges: the manifest's recorded `dynamics.fingerprint` against the deployed pair's served document or the checked-in artifact, or the served document against the checked-in artifact, the diagnostic carrying the expected and served fingerprints and the first diverging element (a silent point-reference renumbering over identical element content included). Either way the deployment declaration no longer names the approved documents. Reported by the reference plant's `ci/check.sh`. |
| `fingerprint-failed` | The manifest-fingerprint leg did not hold: the declared pair did not launch or serve its stamped model digests, or a served digest diverged from the manifest's recorded fingerprint. Reported by the reference plant's `ci/check.sh`, with the leg's `fingerprint: …`/`manifest-fingerprint-mismatch: …` evidence lines on stderr. |
| `fingerprint-nondeterministic` | Two passes of the manifest-fingerprint leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `dynamics-fingerprint-failed` | The dynamics-fingerprint leg did not hold: the manifest-declared pair did not launch on the declared `dynamics.path`, the served point census did not answer, or a served/checked-in/manifest fingerprint comparison diverged — reported with the leg's `manifest-fingerprint-mismatch: …`/`dynamics-fingerprint: …` evidence lines on stderr. Reported by the reference plant's `ci/check.sh`. |
| `dynamics-fingerprint-nondeterministic` | Two passes of the dynamics-fingerprint leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
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
| `negotiation-failed` | The checkpoint-negotiation leg did not hold: the standby launched on a foreign-fingerprint model document did not report the named non-converged `degraded` state through `GET /role` for the observation window, `POST /promote` against it did not answer `409 not_converged` carrying that state, the field owner's receipts or journal showed disturbance from the refused attempt, the foreign peer's teardown left the declared pair diverged, or the control peer on the pair's own model did not converge and promote — the refusal a mismatched deployment must answer honestly. Reported by the reference plant's `ci/check.sh`, with the leg's evidence lines on stderr. |
| `negotiation-nondeterministic` | Two passes of the checkpoint-negotiation leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `startup-claim-failed` | The startup-claim ordering leg did not hold: a third released controller launched against the pair's plant address on a doomed `--journal-file` — a corrupt first record its startup replay cannot read — did not abort at startup validation naming the replay failure (a listener reported, a clean or missing exit, or the preemptive write-ownership claim logged before the failed step), or the incumbent showed disturbance across the attempt — leaving `active`, stalling its tick, dropping field writes, refusing the mid-window receipted command, or gaining role/fencing/divergence/restart journal records — a foreign attachment's mutation probe did not stay `fenced` under the standing claim, the doomed peer's journal file gained records or its state file appeared, or the pair did not restore its launch roles once the foreign process stopped. Reported by the reference plant's `ci/check.sh`, with the leg's `startup-claim: …` evidence lines on stderr. |
| `startup-claim-nondeterministic` | Two passes of the startup-claim ordering leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `refusal-failed` | The pair contract's refusal half did not hold: a `POST /promote` on the freshly launched standby before its first transfer did not answer the named `not_converged` refusal or handed the field off, a receipted write to the tracking standby's monitor did not answer the named `not_active` rejection — or moved the point in the active's served snapshot, entered a peer's adopted receipt log, or left a journal entry recording it as anything but the refusal — the same promote did not succeed once the standby tracked, or the active's field writes, receipts, or journal did not run undisturbed. Reported by the reference plant's `ci/check.sh`. |
| `refusal-nondeterministic` | Two passes of the role-gated refusal leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `handover-failed` | The pair contract's duty-pump failure-handover leg did not hold: a proven duty-pump field-channel fault did not move `duty` to the standby pump inside the declared bound, `staged` did not report the surviving pump against the standing demand, the faulted pump's `fault`/`avail` did not report the exclusion, the managed fault alarm did not annunciate or lacked its journaled `point_changed` evidence, losing every pump did not annunciate `none_available`/`all_faulted` with their managed alarms, restoring the inputs did not produce the declared recovery — fault flags clearing, annunciation returning, the duty designation reassigning under the declared rotation, the unacknowledged latches holding — or the pair's controller roles moved. Reported by the reference plant's `ci/check.sh`. |
| `handover-nondeterministic` | Two passes of the duty-pump failure-handover leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `takeover-failed` | The pair contract's manual-takeover leg did not hold: a receipted `mode`/`hand`/`oos` write did not answer `accepted` or settle `applied` into both peers' adopted log, the pump's delivered command did not leave the group's `cmd_1` on the manual selection or the pump-group status did not reflect the exclusion, the operator demand did not run the pump under the declared thermal/moisture guards, the plant-protocol protection input did not assert the proven fault and its managed alarm, the out-of-service write did not assert the maintenance inhibit, the restore did not return the pump to group control, or the served journal did not carry each attributed transition in order. Reported by the reference plant's `ci/check.sh`. |
| `takeover-nondeterministic` | Two passes of the manual-takeover leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `force-carryover-failed` | The pair contract's force-carryover leg did not hold: a receipted `force_point` on a declared writable `In` point did not answer `accepted` or settle `applied` into both peers' adopted log, the snapshot's `forces` entry or the `Uncertain(Substituted)` sample did not appear on a peer while tracking, the promoted peer did not keep the force — the `forces` entry dropped or the sample no longer the forced value at substituted quality — a receipted `unforce_point` on the new active did not settle `applied`, empty the `forces` list, and resume the point's unforced serve, or the pair was not left in its declared roles. Reported by the reference plant's `ci/check.sh`. |
| `force-carryover-nondeterministic` | Two passes of the force-carryover leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `tune-carryover-failed` | The pair contract's tune-carryover leg did not hold: a receipted `set_parameter` on a declared writable configuration parameter did not answer `accepted` or settle `applied` into both peers' adopted log, the tuned value did not appear in both peers' served parameter report or in the bound point's declared-signal reading while tracking, the promoted peer did not keep the tuned value on its served snapshot/signal surface after the `demote`/`promote` switch, the promoted peer's served journal did not order the promotion's `role_changed` entries after the tune's `command_settled` entry, a further `set_parameter` on the new active did not settle `applied` with a fresh receipt ordered after the promotion, or the pair was not left in its declared roles. Reported by the reference plant's `ci/check.sh`. |
| `tune-carryover-nondeterministic` | Two passes of the tune-carryover leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `force-release-failed` | The pair contract's force-release leg did not hold: a receipted `force_point` on a declared writable `In` point did not answer `accepted`, settle `applied` at the applying scan's tick into both peers' adopted log, or leave the `Uncertain(Substituted)` sample badged on both peers across scans, the journaled settlement missing from the active's record; a receipted `unforce_point` did not settle `applied` at the release's apply tick, empty the `forces` set, and resume the point's live serve — the held image re-stamped `Good`, never re-substituted; the restore write did not land the pre-force held value; the promoted peer resurrected the released force or dropped a settled receipt; the pair was not restored to its launch roles; or the field owner's durable journal file did not carry each attributed transition in `seq` order with the standby's adopted log answering the same receipts. Reported by the reference plant's `ci/check.sh`. |
| `force-release-nondeterministic` | Two passes of the force-release leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `stale-checkpoint-failed` | The pair contract's stale-checkpoint leg did not hold: a receipted `force_point` on the declared writable internal `In` point did not answer `accepted` or settle `applied` with the `Uncertain(Substituted)` sample, the promoted peer did not still carry the force after the `demote`/`promote` switch, a receipted `unforce_point` on the new active did not settle `applied` at the release's apply tick, empty the `forces` set, and resume the point's live serve with the journaled release on both peers' adopted records, the restarted tracking peer did not resume at its persisted tick and reconverge to `tracking` inside the declared window, the re-adoption re-stood the released force — the served `forces` non-empty, the live value no longer serving unforced, the adopted receipt log no longer preserving the unforce settlement exactly once, or a phantom force receipt journaled on either peer's served or durable journal — or the pair was not restored to its launch roles with each durable journal file carrying its attributed transitions in `seq` order. Reported by the reference plant's `ci/check.sh`. |
| `stale-checkpoint-nondeterministic` | Two passes of the stale-checkpoint leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `peer-announce-failed` | The pair contract's peer-announce leg did not hold: a foreign `GET /checkpoint?peer=<addr>` naming a source the pulling connection does not own disturbed the checkpoint read or landed as the demotion tracking source — the demoted field owner stranding `unsynchronized` instead of reconverging `tracking` on its real successor — or the pair was not left in its declared roles. Reported by the reference plant's `ci/check.sh`. |
| `peer-announce-nondeterministic` | Two passes of the peer-announce leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `availability-failed` | The pair contract's command-availability leg did not hold: a `GET /resources` command row was not self-consistent — an `available: false` row without a named refusal or an `available` row carrying one — a served-unavailable bound-point-writable command's `POST /command` submission did not settle a named rejection — or settled a different refusal than the row served on a declared bound point — a served-available command did not settle `applied` into both peers' adopted receipt log, a kind-declared command's standing refusal did not echo verbatim through the settled `command_refused` where the tooling publishes verdicts, or the tracking standby reported different verdicts than the field owner. Reported by the reference plant's `ci/check.sh`. |
| `availability-nondeterministic` | Two passes of the command-availability leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `failover-failed` | The pair contract's failover leg did not hold: the manifest-declared `failover_budget` did not arm the standby's `--auto-promote`, the field-owning controller's severance left the surviving peer's miss run unreported — `GET /role` not serving `standby` under the `degraded` sync state — the self-promotion did not land at the declared budget's scan boundary settling `active`, the plant's writer claim did not fence a foreign attachment while the promoted peer's writes landed — or moved off a recorded dead owner's token — subsequent driven scans or a receipted kind-declared command did not continue, the promoted peer's durable `--journal-file` did not carry `standby → promoting → active` in `seq` order attributed after the budget expiry — or recorded an operator actor on the automatic switch where no non-operator marker has landed — or the standby-severed variant disturbed the active's writes or reported a failover — or the leg's measurement run broke the declared measurement contract: a degraded primary level source failing to select the backup or to annunciate the managed `backup-active` alarm with its journaled `point_changed` evidence, the station not controlling on the selected backup measurement, the all-sources-bad state failing the declared `on_bad_demand` fallback or its alarmed state — or controlling on bad data — or the restores not returning the selection to the primary and the alarms per their declared lifecycle, the receipted ack's clearing of the standing latch, or the pair's unchanged roles. Reported by the reference plant's `ci/check.sh`. |
| `failover-nondeterministic` | Two passes of the failover leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `divergence-missed` | The pair contract's staged-vs-field divergence leg did not hold: the withheld checkpoint pulls did not leave the tracking standby partitioned through the observation window, the field-side write through the run's dedicated plant-protocol client — the field's writer claim joined under the duty's recorded owner token — did not land the perturbed output in the field, the resumed pull did not leave the stale peer's served `GET /role` reporting `standby` under the `diverged` sync state naming the diverging output with both sides' values, the standby's served or durable journal did not carry the `divergence_detected` record, `POST /promote` did not answer the named `not_converged` refusal or handed the field the stale image — a role transition landing on either peer's journal, the plant's writer claim moving, or the injected write overwritten — the active's field writes, settled receipts, or journal did not run undisturbed, the duty's continued writes did not restore the field so the standby's next same-tick comparison resolved the verdict, or the write-free control window did not reconverge to `tracking` and promote normally. Reported by the reference plant's `ci/check.sh`. |
| `divergence-nondeterministic` | Two passes of the staged-vs-field divergence leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `standby-restart-failed` | The pair contract's standby-restart leg did not hold: the tracking standby's restart onto its declared `--state-file`/`--journal-file` did not resume at the persisted tick (a missing state file's cold start included), the resumed peer did not rejoin in `standby` and reconverge to `tracking` inside the leg's declared window, the durable journal's restart boundary did not order after the pre-restart entries with `seq` order intact, the active's field writes, receipts, or journal did not run undisturbed across the restart, or the pair did not promote afterward. Reported by the reference plant's `ci/check.sh`. |
| `standby-restart-nondeterministic` | Two passes of the standby-restart leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `report-failed` | The released `dcs-alarm-report` leg did not hold: the tool shipped no binary, the served-journal report did not cover the emitted model's alarm set per instance or did not measure the driven lifecycle's activation, annunciation, and attributed acknowledgment, the report's cross-section accounting disagreed with itself or the served record's stretch, the durable journal file's report diverged from the served journal's or miscounted the run's lifetimes, or a refusal mode — an unreachable monitor, an unreadable journal file — exited zero or unnamed. Reported by the reference plant's `ci/check.sh`, with the leg's evidence lines on stderr. |
| `report-nondeterministic` | Two passes of the `dcs-alarm-report` leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `command-switch-failed` | The pair contract's command-switch leg did not hold: the release tooling shipped no `dcs-ctl` binary, the exercise sequencer's kind-declared `advance` invoked through the released `dcs-ctl invoke` did not settle `applied` with exactly one `command_settled` journal entry on the serving peer — before the switch on the field owner, after it on the promoted peer, each settlement attributed to the peer that served it with the old peer's settlement replayed on neither side — the promoted peer's emitted `step_completed` record did not continue the pre-promotion entries in tick order with unchanged component attribution or re-emitted one, the `advance` submitted immediately before the restore switch did not settle exactly once `applied` on the new active — lost at the boundary or double-applied — or the pair was not left in its declared roles. Reported by the reference plant's `ci/check.sh`, with the leg's evidence lines on stderr. |
| `command-switch-nondeterministic` | Two passes of the command-switch leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `demote-pending-failed` | The pair contract's demote-boundary pending-command leg did not hold: the receipted `write_value` did not answer `accepted` on the field owner, the promoted peer did not hold the still-`Accepted` admission through the boundary's final sync — lost or pre-settled — the demoted peer's first quiesced scan journaled a `command_settled` for the admission, dropped its suspended receipt, or left the baseline image — a phantom applied settle or unaudited drop on the fenced image — the admission did not settle exactly once across both peers (the single terminal outcome `applied` once per peer through the carry or `Rejected{superseded}` journaled on the demoted peer alone), the peers' adopted receipt logs diverged or contradicted the journaled outcome, a served image missed the applied value or carried a superseded one, a declared durable journal file did not carry the same settle record in `seq` order, or the pair was not left in its declared roles. Reported by the reference plant's `ci/check.sh`, with the leg's evidence lines on stderr. |
| `demote-pending-nondeterministic` | Two passes of the demote-boundary pending-command leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `demote-reconvergence-failed` | The pair contract's demote-follow reconvergence leg did not hold: a controller did not bind the manifest's declared `0.0.0.0` listen host, the declared standby did not converge to `tracking`, a documented `demote`/`promote` switch step did not answer its named reports, a demoted peer did not reconverge `tracking` or dropped out of it across the driven pull train, a served role report carried a wildcard tracking source or a `degraded` detail naming the demoted peer's own address or a foreign endpoint rather than the successor's dialable monitor address, an announced-source demotion journaled no `tracking_source_adopted` or adopted a source that was not the successor's dialable address, the peers' served snapshots or adopted receipt logs diverged, a durable journal file missed the run's own `role_changed` transitions or `seq` order or gained a restart-like run boundary, a declared `--state-file` did not checkpoint the run's final tick under the manifest's fingerprint, or the pair was not left in its declared launch roles. Reported by the reference plant's `ci/check.sh`, with the leg's `demote-reconvergence: …` evidence lines on stderr. |
| `demote-reconvergence-nondeterministic` | Two passes of the demote-follow reconvergence leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `managed-lifecycle-failed` | The pair contract's managed-alarm lifecycle leg did not hold: the field-driven activation did not assert the managed alarm's `alarm`/`unacknowledged` with the journaled `point_changed` record, the receipted `ack` did not settle `applied` under the leg's actor or clear the latch, the bounded shelve did not report `shelved` or did not auto-release at the declared `max_shelve_ticks` — the journaled assertion-to-expiry span measuring the bound — the never-shelvable shelve write did not answer the named `not_writable` refusal on the attributed receipt or changed state, the pump's `oos` drive did not report the declared `out_of_service`/`suppressed` states or leaked onto alarms the model wires without those inputs, the suppressed trip did not report `alarm`'s process truth with the latch withheld, the return to service did not evaluate the standing condition as a fresh trip the receipted ack clears, a driven input or the pair's roles were not restored, the peers' adopted receipt logs diverged, or the durable journal did not carry the lifecycle's attributed entries in `seq` order with the served journal answering the same record. Reported by the reference plant's `ci/check.sh`, with the leg's evidence lines on stderr. |
| `managed-lifecycle-nondeterministic` | Two passes of the managed-alarm lifecycle leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `event-parity-failed` | The pair contract's emit-identical standby leg did not hold: the tracking peer never converged, the counted `step_completed` set never stood on the active, the standby's served `event_emitted` records diverged from the active's — a missing record, a re-attributed component, a changed identity, ordered field, tick, or retention — or the standby's role moved or its writes went ungated. Reported by the reference plant's `ci/check.sh`. |
| `event-parity-nondeterministic` | Two passes of the emit-identical standby leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `carry-failed` | The pair contract's managed run-state carryover leg did not hold: a receipted `oos` or `shelve` write did not answer `accepted` or settle `applied` into both peers' adopted log, the `out_of_service`/`suppressed` states or the standing suppressed trip did not report before the switch, the `demote`/`promote` did not land inside the declared `max_shelve_ticks` bound, the promoted peer dropped `shelved` or released it at a tick other than the continued countdown's expiry — the journaled assertion-to-expiry span on the demoted owner's record not measuring the declared bound, or the promoted peer's expiry landing later as a restarted bound — the carried `out_of_service`/`suppressed` did not stand with `alarm`'s process truth and the `unacknowledged` latch withheld, a written point did not ride the checkpoint, a durable journal's ordered record broke across the switch, a driven input or the pair's launch roles were not restored, or the peers' adopted receipt logs diverged. Reported by the reference plant's `ci/check.sh`, with the leg's evidence lines on stderr. |
| `carry-nondeterministic` | Two passes of the managed run-state carryover leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `staging-failed` | The pair contract's staging leg did not hold: a receipted `write_value` on a pump's declared writable `oos` point did not answer `accepted` or settle `applied` into both peers' adopted receipt log, the out-of-service hold did not keep the group's `staged` count and motor commands at zero while the declared inflow raised the level unopposed, the emitted threshold chain's `demand` did not move 0→1→2 at the declared `start`/`lag_start` crossings — staging ahead of or behind the level the chain read — `duty_call`/`lag_call` did not report with their crossings, the `high` crossing did not assert `high_level` beside the managed high-level alarm's `alarm`/`unacknowledged`, the releases did not return the pumps' availability, the standing demand did not stage the duty pump first and the lag inside the declared `start_delay_ticks`, a staged pump's `cmd`/`run` field outputs did not prove the delivered start, the falling edge did not release the demand at the declared `start`/`stop` crossings or de-staged out of the declared lag-first order, the pumped-down well did not report the `below-cutoff` floor, the receipted `ack` did not clear the alarm's latch, a driven input or the pair's roles were not restored, the peers' adopted receipt logs diverged, or the durable journal did not carry the holds, the annunciation, the staged runs, and the lag-first de-stage in `seq` order with the served journal answering the same record. Reported by the reference plant's `ci/check.sh`, with the leg's evidence lines on stderr. |
| `staging-nondeterministic` | Two passes of the staging leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `oos-failed` | The pair contract's per-pump out-of-service leg did not hold: a receipted `write_value` on the duty pump's declared writable journaled `oos` point did not answer `accepted` or settle `applied` under the leg's actor, the exclusion did not land inside the declared wiring bound — the `oos-ok` cone, the aggregated `avail`, or its delivered `avail-in` copy not dropping, or `duty` not handing to the sibling — `staged` reported past the available count or `none_available` reported with the sibling available, the held pump's `cmd` re-asserted while the hold stood, a managed per-pump alarm did not report the `out_of_service`/`suppressed`/`shelved` states its declared lifecycle bindings select or an unbound or sibling alarm reported one, the mid-OOS injected fault did not assert `alarm` as process truth with the `unacknowledged` latch withheld, the receipted false write did not return the pump to availability or re-annunciate the outlasted trip on suppression's release, the receipted `ack` did not clear the standing latch, the next completed cycle's declared rotation did not hand `duty` back, the peers' adopted receipt logs diverged, the durable journal did not carry every managed transition as ordered `point_changed` entries beside the attributed settlements — or the journaled edges measured past the declared bound — with the served journal answering the same record, or the pair's controller roles moved. Reported by the reference plant's `ci/check.sh`, with the leg's evidence lines on stderr. |
| `oos-nondeterministic` | Two passes of the per-pump out-of-service leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `power-trip-failed` | The pair contract's power-fail interlock leg did not hold: the `power-fail` contact driven through the plant protocol did not drop `power-ok` and both pumps' availability aggregates — the delivered `power-ok-in` legs included — the motor commands did not release while the chain's `demand` still stood and `staged` reached zero, `none-available` or the managed `power-fail` alarm's `alarm`/`unacknowledged` did not annunciate with journaled `point_changed` evidence, a field output moved outside the driven scan sequence, the receipted `power-fail-ack` did not settle `applied` under the leg's actor or clear the latch while the condition stood, the released contact did not return the permissives and alarms — the acknowledged latch staying down while the never-acknowledged `none-available` latch holds — or re-stage the standing demand inside the declared `min_off_ticks`/`start_delay_ticks` bounds with each `cmd`/`run` field pair proving the delivered start, the peers' images or adopted receipt logs diverged, the pair's controller roles moved, or the field owner's durable journal did not carry the driven transitions and attributed settlements in `seq` order with the served journal answering the same record. Reported by the reference plant's `ci/check.sh`, with the leg's evidence lines on stderr. |
| `power-trip-nondeterministic` | Two passes of the power-fail interlock leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `monitor-starvation-failed` | The pair contract's monitor-starvation leg did not hold: under the saturating set of incomplete-body connections held against the armed pair's field owner, a serving-lane read — `GET /role`, `GET /snapshot`, or `GET /checkpoint` on either peer — did not answer inside the declared per-request bound, a peer's reported role left its declared state, the standby's per-scan checkpoint pulls stopped landing or aligned off the owner's checkpoint — one pull past the armed `failover_budget` witnessed inside the window — a foreign attachment's field probe was not fenced, either durable journal carried `role_changed` or `field_claim_lost`, or closing the set did not restore the submission lane — a driven `POST /scan` on the flooded owner not advancing the tick, a receipted kind-declared command not settling `applied` into both peers' adopted log, or the pair's roles moved. Reported by the reference plant's `ci/check.sh`, with the leg's evidence lines on stderr. |
| `monitor-starvation-nondeterministic` | Two passes of the monitor-starvation leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `commissioning-failed` | The pair contract's commissioning/handover record leg did not hold: the field census did not match the emitted model's declared channel set or a point's served sample diverged from the field's scan-boundary values, a driven span mark did not land in the field or was not served identically by both peers, the chain's `demand`/staging response was not recorded, a receipted `mode`/`hand` write did not settle `applied` or the output loop's delivered command and run feedback did not read back in the field, a managed alarm's declared rationalization record was not served verbatim on both peers, the documented `demote`/`promote` switch or the restore did not leave the pair in its declared roles, a peer's checkpoint-stamped fingerprint diverged from the manifest's recorded fingerprint, a declared durable journal/state file was absent or misreported, or the assembled record was missing a named artifact — `the record carries no <artifact> artifact`. Reported by the reference plant's `ci/check.sh`, with the leg's `commissioning: …` evidence lines on stderr. |
| `commissioning-nondeterministic` | Two passes of the commissioning/handover record leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `commissioning-unchecked` | A doctored commissioning record missing a named artifact (`missing-io-checkout`, `missing-loop-check`, `missing-alarm-signoff`, `missing-documentation-turnover`) passed the leg silently, or the completeness audit's failure did not name the dropped artifact. Reported by the reference plant's `ci/check.sh`. |
| `journal-boundary-failed` | The pair contract's journal-boundary flood leg did not hold: after floods of receipted journal-producing commands past the served journal's retained bound around two tracking-standby restarts onto its declared `--state-file`/`--journal-file`, the standby's served `GET /journal` did not answer every served lifetime's `run_boundary` entry pinned ahead of the retained tail — a restart's relaunch not resuming at the persisted tick or not re-tracking included — the served stream was not in strict `seq` order or the flood's eviction did not read as the numbering gap between the last boundary and the tail, the `?since=` cursor past the last boundary did not answer exactly the retained tail, a durable journal file did not retain every `run_boundary` marker in order with contiguous entry `seq`s, the field owner's single-lifetime record served a boundary or lost its cold-start marker, or the pair's roles moved. Reported by the reference plant's `ci/check.sh`, with the leg's evidence lines on stderr. |
| `journal-boundary-nondeterministic` | Two passes of the journal-boundary flood leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `command-admission-failed` | The pair contract's command-admission leg did not hold: with the declared pair converged in driven mode, a flood of receipted `write_value` submissions twice the field owner's served `command_queue.capacity` did not answer every submission its structured receipt — the first `capacity` `accepted`, each submission past the bound the named `queue_full` rejection carrying the declared bound and the refused target — the held queue moved the run's tick, disturbed the served reads, or moved a role, the live receipt mirror did not carry the flood's verdicts in submission order with the overflow terminal `rejected`, the first drain scan did not land the owner's tick exactly one boundary on, both peers' `command_queue` sections did not report the flood's admission metrics identically — every submission an `attempt`, the over-bound half `full_rejections`, `high_water` at the bound, `depth` drained — the adopted receipt logs diverged or did not settle the admitted half `applied` in submission order while the overflow stayed `queue_full`, the owner's served journal did not carry each overflow's rejection at the flood's tick and each admitted command's `command_settled` in apply order at the drain tick, or the pair was not left in its launch roles. Reported by the reference plant's `ci/check.sh`, with the leg's `command-admission: …` evidence lines on stderr. |
| `command-admission-nondeterministic` | Two passes of the command-admission leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `shelving-reason-failed` | The pair contract's shelving-reason leg did not hold: with the declared pair converged in driven mode, the reasoned shelve on the first shelvable managed alarm — a writable `shelve` input under a nonzero declared `max_shelve_ticks` — did not settle `applied` through the receipted path with the leg's declared actor and reason on the settled receipt, the journaled `command_settled` did not echo actor and reason beside the `shelved` `point_changed` transition at the applied tick, the served managed list did not report the standing shelve, the served index did not carry the `requires_reason` mark, the shelve did not expire at applied + `max_shelve_ticks` with the request still standing and the release transition pairing no command, the reasonless shelve on the marked point did not answer the named `reason_required` admission verdict on the attributed and journaled receipt, the reasonless shelve on the unmarked point did not settle `applied` carrying no reason, the reasoned release did not restore the standing request, the peers' adopted receipt logs diverged or the served journal did not answer the durable file's lifecycle record, or the pair's launch roles moved. Reported by the reference plant's `ci/check.sh`, with the leg's `shelving-reason: …` evidence lines on stderr. |
| `shelving-reason-nondeterministic` | Two passes of the shelving-reason leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `history-run-failed` | The pair contract's history run-marker leg did not hold: with the declared pair converged in driven mode and a `since` cursor held on one of the tracking peer's accumulating `GET /history` points, the standby's restart onto its manifest-declared `--state-file`/`--journal-file` did not resume at the persisted tick, the relaunched peer's served envelopes did not stamp the new lifetime's `run` ordinal — on the pre-scan pages' empty `samples` included, an unmarked or stale mark reading as a phantom-idle stream — the cursor-filtered page did not carry every post-restart sample, a cursor parked past the axis's end did not answer its stamped envelope, the resumed run's full axis was not strictly ascending or did not open at or above the persisted tick — the tick domain a state-file resume continues — the untouched field owner's served `run` moved, the standby's durable journal file did not carry the run-2 `run_boundary` marker at the persisted tick beside run 1's cold-start marker, or the pair was not left in its launch roles. Reported by the reference plant's `ci/check.sh`, with the leg's `history-run: …` evidence lines on stderr. |
| `history-run-nondeterministic` | Two passes of the history run-marker leg produced different digests. Reported by the reference plant's `ci/check.sh`. |
| `rig-invalid` | The consumer's checked-in rig definition does not parse — `docker compose config` or the fallback YAML parser rejected it. Reported by the reference plant's `ci/check.sh`. |
| `rig-unverifiable` | The rig-definition consistency check could not run: neither `docker compose` nor PyYAML is available to parse the definition. Reported by the reference plant's `ci/check.sh`. |
| `rig-mismatch` | The consumer's checked-in rig definition diverges from its deployment manifest — images, mounted model or dynamics paths, the propagated model fingerprint, listen addresses, the controller pair's standby wiring, the standby's declared `failover_budget` against its `--auto-promote` flag (a declared budget with no flag, a flag with no declaration, a diverging value, or the field placed on the duty entry), the declared persistence paths' mounts and flags, or the optional `topology` section's named pairs (a member the manifest does not declare, a member two pairs share, or a declared pair whose standby wiring does not close inside it) disagree with what the manifest declares. Reported by the reference plant's `ci/check.sh`. |
| `pair-legs-invalid` | The pair stage's leg driver could not register the legs directory: a `ci/legs/*.py` file carried no `LEG` literal, a `LEG` record did not parse or lacked a required field (`order`, `title`, `passes`, or a malformed `failed`/`tools`/`tampers` entry), two legs declared the same `order`, or the directory held no leg files. The diagnostic names the offending file; a leg is never silently dropped. Reported by the reference plant's `ci/check.sh`. |
| `<leg>-unchecked` | The paired self-check diagnostic every checked leg carries: `ci/check.sh` plants a negative case for each leg — a doctored input or tampered expectation the leg must refuse with its named diagnostic — and reports `<leg>-unchecked`, formed on the leg stem its failure (`<leg>-failed` or, for the divergence leg, `<leg>-missed`) and `<leg>-nondeterministic` diagnostics share, when the planted case passes or the leg answers a name other than its declared one (e.g. a drifted record artifact passing the interface-schema non-drift leg reports `schema-drift-unchecked`, not `schema-drift`). It is distinct from `<leg>-failed`: the failed diagnostic is the leg's contract check failing on a real divergence; the unchecked diagnostic is the leg's own negative self-test failing — the leg can no longer be trusted to catch what it names. Reported by the reference plant's `ci/check.sh`; the emitted set grows with each leg that plants a negative case — currently `schema-drift-unchecked`, `diff-mismatch-unchecked`, `alarm-validation-unchecked`, `fingerprint-unchecked`, `dynamics-fingerprint-unchecked`, `rig-mismatch-unchecked`, `restart-resume-unchecked`, `schema-mismatch-unchecked`, `pair-unchecked`, `negotiation-unchecked`, `startup-claim-unchecked`, `refusal-unchecked`, `handover-unchecked`, `takeover-unchecked`, `force-carryover-unchecked`, `tune-carryover-unchecked`, `force-release-unchecked`, `stale-checkpoint-unchecked`, `burst-order-unchecked`, `peer-announce-unchecked`, `availability-unchecked`, `failover-unchecked`, `divergence-unchecked`, `standby-restart-unchecked`, `report-unchecked`, `command-switch-unchecked`, `demote-pending-unchecked`, `managed-lifecycle-unchecked`, `carry-unchecked`, `staging-unchecked`, `oos-unchecked`, `power-trip-unchecked`, `monitor-starvation-unchecked`, `event-parity-unchecked`, `commissioning-unchecked`, `journal-boundary-unchecked`, `command-admission-unchecked`, `shelving-reason-unchecked`, `history-run-unchecked` — and this convention entry declares each new name without a per-leg row. |
