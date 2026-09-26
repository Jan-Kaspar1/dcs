# reference-plant — a DCS consumer repository

This repository is a **customer plant project**: it owns a duty/standby
pumping-station composition and deploys it through the generic DCS
controller runtime. It is *not* part of the DCS platform repository —
it consumes the platform's **versioned release artifacts** only, exactly
as `docs/release-contract.md` in the platform repository records. Use
it as the starting point for your own plant repository.

The station: a wet well with a failover-selected level measurement
(primary and backup instruments), a threshold chain turning level into
pump demand, a two-pump duty/standby `pump-group`, per-pump manual
takeover and out-of-service declarations, the managed alarm set —
a never-shelvable high-level alarm, a shelvable low-level alarm, and
the equipment alarms the site policy declares — and a small exercise
program (`sequencer`) whose kind-declared `advance`/`reset` commands
and kind-emitted `step_completed`/`sequence_completed`/`progress`
events — one declared per retention class — carry the declared
command/event surface the clean check proves.

## Layout

```
Cargo.toml             the package — git-pinned to the release tag
Cargo.lock             the resolved pin — release crates from git only
rust-toolchain.toml    the toolchain the release declares
src/station.rs         the consumer-owned station composition
src/main.rs            the emit entry point
src/scenario.rs        the scripted-simulation declaration
model/plant.json       the emitted, approved plant model
model/dynamics.json    the declared simulation dynamics
ci/scenario.json       the generated scenario the CI drives
ci/check.sh            the clean-CI check a fresh clone runs
ci/alarm_validation.py the alarm-validation leg — the emitted model's
                       managed-alarm record audited, doctored copies
                       refused by the released `dcs-controller
                       --check`, and the driven run's served
                       components/parameters sections reporting the
                       same record
ci/fingerprint.py      the manifest-fingerprint authorization leg —
                       the declared pair launched on the released
                       tooling and each peer's /checkpoint-stamped
                       model digest held equal to the manifest's
                       recorded fingerprint, a doctored served model
                       with renumbered point ids reporting the named
                       mismatch with the expected vs served
                       fingerprint and the first diverging section
ci/alarm_rationalization.py  the pair contract's alarm-
                       rationalization leg — the emitted model's
                       managed-alarm record asserted verbatim on the
                       deployed pair's served signal index and
                       snapshot parameters, before and after the
                       changeover, and the pair's launch roles
                       restored
ci/claim_fencing.py    the pair contract's claim-fencing leg — a
                       dedicated third sim-net attachment exercising
                       the standing writer claim's whole lifecycle on
                       the settled pair's spawned plant: fenced write
                       and step probes, the ensure/release verbs under
                       foreign and owner tokens, the same mutations
                       driven through the shipped `dcs-plant-ctl`, a
                       rogue claim's settled answer, and the pair
                       restored with its launch roles and the claim's
                       owner unchanged
ci/simulate.py         the deterministic scripted-simulation runner;
                       --surface asserts the served operator surface —
                       signal index, interface registry, declared
                       commands, emitted events
ci/dynamics_fingerprint.py  the dynamics-fingerprint authorization
                       leg — the manifest-declared pair launched on
                       the deployment's declared `dynamics.path`, the
                       document it serves fingerprinted canonically
                       and held equal to the manifest's optional
                       `dynamics.fingerprint` and the checked-in
                       artifact, a doctored served document with
                       renumbered point references reporting the
                       named mismatch with the expected vs served
                       fingerprint and the first diverging element
ci/restart.py          the restart-recovery leg — the driven
                       controller stopped mid-scenario and relaunched
                       onto the same state/journal files
ci/legs.py             the pair stage's leg driver — discovers the
                       ci/legs/*.py legs in declared order, runs each
                       twice requiring identical digests, and exercises
                       each leg's declared doctored cases
ci/legs/               the pair stage's legs — one file per leg, each
                       carrying its explanatory docstring and its stage
                       registration in a module-level LEG literal (its
                       declared order, titles, tool flags, and doctored
                       cases). ci/legs/pair.py is the redundant-pair
                       leg and the shared launch/settle/restore harness
                       the legs run on. Adding a leg is one new file —
                       no edit to the check script, the lint, or this
                       list
ci/managed_carryover.py  the pair contract's managed run-state
                        carryover leg — the managed alarm kinds'
                        checkpointed run state proven carried across a
                        promotion on the deployed pair: a mid-shelve
                        countdown releasing at its continued expiry,
                        never a restarted bound, and the wired
                        out_of_service standing with evaluation held
ci/staging.py          the pair contract's staging leg — the emitted
                       threshold chain's level-driven demand staging
                       exercised on the deployed pair: the unopposed
                       rise through the declared crossings, the
                       high-level annunciation, the bounded staging
                       response, and the declared de-stage order
ci/oos.py              the pair contract's out-of-service leg — a
                       receipted maintenance inhibit on the duty
                       pump's declared `oos` point excluding it from
                       availability and handing `duty` to the sibling
                       on the deployed pair, the managed alarms
                       reporting their declared
                       `out_of_service`/`suppressed` states with
                       `alarm` still reporting process truth mid-OOS,
                       and the false write returning the pump to
                       availability and the duty rotation
ci/power_trip.py       the pair contract's power-fail interlock leg —
                       the station power-fail contact driven through
                       the plant protocol on the settled pair under a
                       standing duty demand: `power-ok` and both
                       pumps' availability dropping, the motor
                       commands releasing while the chain's demand
                       stands, `none-available` and the managed
                       `power-fail` alarm annunciating with journaled
                       evidence, the receipted `power-fail-ack`
                       clearing the latch mid-condition, and the
                       released contact re-staging the demand inside
                       the declared bounds with the pair's roles
                       unchanged
ci/consumers.py        the consumer-boundary driver — replays the same
                       driven run under each consumer schedule
ci/ctl.py              the dcs-ctl leg — the released operator CLI
                       driving the same run: receipted invoke, reads,
                       and the named refusal modes
ci/deploy_rig.py       the rig-definition consistency check —
                       deploy/compose.yaml against the manifest
ci/schema_conformance.py  the structural conformance check — the
                       driven run's `GET /schema` document against the
                       release record's block-interfaces.schema.json,
                       and the checked-in deploy/manifest.json /
                       model/dynamics.json against the record's
                       deploy-manifest.schema.json /
                       dynamics.schema.json (stdlib-only)
deploy/manifest.json   the deployment declaration
deploy/compose.yaml    the checked-in rig definition instantiating it
```

## The customer path

### 1. Create your repository

Copy this tree into a fresh repository. Nothing in it references the
platform checkout — the only platform coupling is the pinned release in
`Cargo.toml`.

### 2. Pin a release

`Cargo.toml` pins the release crates by release tag:

```toml
dcs-build = { git = "https://github.com/Jan-Kaspar1/dcs.git", tag = "v0.3.0" }
dcs-model = { git = "https://github.com/Jan-Kaspar1/dcs.git", tag = "v0.3.0" }
```

`rev = "<commit>"` names the identical immutable commit — the recorded
commit `docs/releases/v0.3.0/record.md` carries — and is always
supported. `Cargo.lock` is committed so every build resolves the same
sources; a tag pin resolves the tag once and the committed lockfile
records the commit it landed on.

### 3. Compose and emit

Edit `src/station.rs` — site setpoints, pump count, alarm policy —
against the supported `dcs-build` primitives. Then:

```sh
cargo run            # emit the plant model to stdout
cargo run -- --fingerprint   # the model's canonical fingerprint
cargo run -- --scenario      # the scripted-simulation declaration
```

Emission is deterministic: identical sources emit identical bytes, and
`ci/check.sh` byte-compares a fresh emit against the checked-in
`model/plant.json` and `ci/scenario.json`. When the composition
changes, regenerate the checked-in artifacts:

```sh
cargo run > model/plant.json
cargo run -- --scenario > ci/scenario.json
```

### 4. Validate, lint, check

The released tooling accepts the emitted model — `ci/check.sh` runs
`dcs-model validate`, `dcs-model lint`, and `dcs-controller --check`
over `model/plant.json`. Install the tooling from the pinned release:

```sh
cargo install --git https://github.com/Jan-Kaspar1/dcs.git --tag v0.3.0 \
    dcs-model dcs-controller dcs-plant dcs-monitor
```

The `dcs-monitor` package ships `dcs-ctl`, the released operator CLI —
`dcs-ctl <addr> schema|resources|events|snapshot|signals|receipts|
journal` reads the running controller's served contract, `invoke`
submits a component's declared command through the bounded receipted
path with `--actor` attribution, and `write`/`set-parameter`/`force`/
`promote`/`demote`/`scan` cover the rest of the operator surface — and
`dcs-alarm-report`, the alarm flood and performance report —
`dcs-alarm-report <addr>` computes the declared `AlarmReport` metric
set over the served journal, `dcs-alarm-report <addr> --journal-file
<path>` over the durable journal file.

The stage also exercises the contract's remaining `dcs-model` surfaces.
The release record's schema artifacts —
`docs/releases/<tag>/plant-model.schema.json`,
`docs/releases/<tag>/block-interfaces.schema.json`,
`docs/releases/<tag>/deploy-manifest.schema.json`, and
`docs/releases/<tag>/dynamics.schema.json`, where `<tag>` is
the manifest's `dcs_release` — are fetched through the same git remote
and pinned revision the crates and tooling resolve over (the record
lives in the release repository's tree at the pinned commit), and
`dcs-model schema` / `dcs-model interface-schema` /
`dcs-model deploy-schema` / `dcs-plant-server --dynamics-schema` at the
pinned rev must emit those bytes exactly — a divergence is
`schema-drift`. The stage then screens the checked-in consumer
documents against the artifacts declaring their shapes —
`deploy/manifest.json` against `deploy-manifest.schema.json` and
`model/dynamics.json` against `dynamics.schema.json`, the same
required-keys/field-shape conformance `ci/schema_conformance.py` runs
for the served registry document — each screened twice with identical
digests required, and a doctored schema-violating copy of each refused
as `schema-mismatch` (a silent pass is `schema-mismatch-unchecked`).
`dcs-model diff` runs two legs: against a doctored *compatible*
revision of the checked-in model it must name the actual change, and
against the identical document it must report `no changes` — a leg
whose expectation fails is `diff-mismatch`. Finally `dcs-model
summary` and `dcs-model signal-index` run over the checked-in model
with their outputs recorded to the run's evidence — the check
transcript carries them verbatim with their sha256 digests, identical
on every pass.

The stage's `alarm-validation` leg then proves the rejection half of
the alarm contract on a customer-owned document — `ci/alarm_validation.py`
audits that every managed alarm instance in the emitted model carries
its `rationalization` block plus `priority`/`class`/`response_ticks`,
doctors copies of the document — one instance's `required_action`
removed, the same field emptied, another kind's `priority` removed —
and requires the released `dcs-controller --check` to refuse each,
naming the missing element rather than loading silently or warning
only, and checks the driven run's served `components`/`parameters`
sections report the same record. Two passes must produce the
identical `alarm-validation-digest`; a violated contract fails
`alarm-validation-failed`, a divergence
`alarm-validation-nondeterministic`, and a run whose skipped
doctoring passes `alarm-validation-unchecked`.

The `fingerprint` stage then authorizes the model bytes the deployed
pair serves, not just the checked-in file: beside comparing the fresh
emit's fingerprint against the manifest's recorded
`model.fingerprint`, `ci/fingerprint.py` launches the
manifest-declared pair on the released tooling and pulls each peer's
`GET /checkpoint` — the `model_fingerprint` a peer stamps is the
digest of the model document it was assembled from and serves. Any
divergence from the recorded fingerprint, including a silent one the
component set cannot see, fails `manifest-fingerprint-mismatch`
naming the diverging peer, the expected and served fingerprints, and
the first top-level document section the served model diverges in.
The stage's doctored case proves that diagnostic fires: a served
document whose point ids were renumbered — references carried, the
component set verbatim — must report the mismatch and name the
`io_points` section, or the leg reports `fingerprint-unchecked`. Two
passes must produce the identical `fingerprint-digest`; a violated
contract fails `fingerprint-failed`, a divergence
`fingerprint-nondeterministic`.

For dynamics the same canonical fingerprint contract applies:
`ci/dynamics_fingerprint.py` launches the manifest-declared pair on
the released tooling serving the manifest's declared `dynamics.path`
— the document the rig mounts read-only and `dcs-plant-server
--dynamics` merges; the plant protocol exposes no dynamics surface
and none is added, so the leg pins the deployment-declared document
the pair actually runs — and fingerprints it canonically (FNV-1a
over the parsed document's reserialization, the same
sixteen-hex-digit shape `model.fingerprint` carries). The served
fingerprint must equal both the manifest's optional
`dynamics.fingerprint` and the checked-in `model/dynamics.json`; any
divergence reports `manifest-fingerprint-mismatch` naming the
expected and served fingerprints and the first diverging element. A
manifest omitting the optional field declares no dynamics pin; the
served-vs-checked-in comparison still stands. The stage's doctored
case proves the diagnostic fires: a served deployment whose point
references were renumbered — identical element content — must
report the mismatch, or the leg reports
`dynamics-fingerprint-unchecked`. Two passes must produce the
identical `dynamics-fingerprint-digest`; a violated contract fails
`dynamics-fingerprint-failed`, a divergence
`dynamics-fingerprint-nondeterministic`.

### 5. Run the simulation

`ci/check.sh` drives `ci/simulate.py`: `dcs-plant-server` serves the
checked-in model plus `model/dynamics.json`, `dcs-controller --driven`
advances declared scans, and the script asserts the scenario's
observable outcomes — duty and lag staging, the high-level alarm's
`not_writable` shelve refusal, ack latching, instrument failover and
recovery, manual takeover, out-of-service suppression, and the
pumped-down all-stop — identically on every run.

The check's `restart` stage then proves the recovery contract the
manifest's persistence fields declare — `ci/restart.py` launches the
field-owning `dcs-controller --driven` with the manifest-declared
`--state-file`/`--journal-file` flags pointed at runner-owned scratch
paths, drives the deterministic scenario past a leg boundary — far
enough to leave applied receipts and journaled transitions — stops the
controller, and relaunches it onto the same files. The resumed run
must report its resume and continue at the persisted tick — never a
silent cold start at tick zero — its leg outcomes, receipts, and
served field image equal to an uninterrupted reference pass, and the
journal's `seq` order must continue across the file's `run_boundary`
marker. A corrupt state file is refused at startup naming the file; a
missing one cold-starts the resumed run and the leg reports the lost
checkpoint rather than passing silently. Two passes must produce the
identical `restart-digest`; a divergence fails
`restart-resume-nondeterministic`, a violated contract
`restart-resume-failed`.

The check's `surface` stage then drives the same deterministic
`--driven` run — `ci/simulate.py --surface` — asserting the served
operator surface against the emitted model's declaration: `GET
/signals` must serve exactly the declared signal index, so every
writable command point the composition declares (the alarm
`ack`/`shelve`/`oos` points, the per-pump `mode`/`hand`/`oos` takeover
points, the exercise program's `run` request) appears `writable: true`
while the never-shelvable high-level alarm's `shelve` point stays
read-only, and every named signal carries its declared group; `GET /`
must serve the monitoring page; `GET /schema` must serve the
block-interface registry — one versioned interface per declared
component covering its declared ports as measurement/state resources
with their resolved bound points, its declared parameters as
configuration with their `set_parameter` commands, and the adapted
point-command verbs on every `In` port; every kind-declared command a
served interface carries — the exercise `sequencer`'s `advance` and
`reset` — must answer a structured receipt through `POST /command`'s
`invoke` variant and settle `applied` through the journaled
`command_settled` record; `GET /resources` must join each
`kind_declared` command's published availability verdict — `advance`
reporting `available` mid-table, then `available: false` carrying the
kind's named refusal once the run completes the table; every
kind-emitted event — the sequencer's `step_completed`,
`sequence_completed`, and `progress`, one declared per retention
class — must reach the consumer-visible record once the run drives
its declaring component to emission, each landing where its served
`retention` mark routes it: `journal`-retained emissions in `GET
/journal`'s `event_emitted` entries, `history`/`latest` emissions in
the bounded routed stores, and all three in the instance-attributed
`events` of `GET /resources` under their marks; the snapshot's
`descriptors` must cover every composed component; and `GET /journal`
must answer the run's recorded transitions. A divergence fails
`surface-mismatch`.

The stage then checks the served `GET /schema` document itself against
the fetched release-record artifact — `ci/schema_conformance.py` runs
the consumer-side structural conformance the served-registry contract
intends for non-Rust consumers: every `required` key present, declared
properties and items carrying their declared JSON types, `enum`/`const`
vocabularies held, `additionalProperties: false` enforced, and local
`$ref`/`anyOf`/`oneOf` combinators resolved through the artifact's own
`$defs`. The boundary is deliberate: this is a required-keys/field-shape
check in stdlib-only python, while full draft-2020-12 validation —
every keyword the draft defines — stays with the platform's own checks,
where the `jsonschema` dependency exists. A missing or mistyped
required field — any structural divergence — fails `schema-mismatch`.

The check's `pair` stage then proves the declared pair runs — not just
that its definition parses: `ci/deploy_rig.py` verifies the standby
wiring and persistence fields statically, while the stage's legs run
the manifest-declared deployment on the released tooling —
`dcs-plant-server` plus two released `dcs-controller --driven --remote`
instances wired exactly as the manifest declares, the standby's
`--standby` flag at the peer it names, each controller's declared
`--state-file`/`--journal-file` carried at runner-owned scratch paths,
plus the shared `--pair-token` the keyed announced-source contract
runs on — converging the standby to `tracking`, driving scans through
`POST /scan` on each peer keeping their served snapshots identical,
submitting kind-declared commands through the receipted path, and
issuing the documented `demote`/`promote` switchover with the run
continuing bumplessly. The pair stage's attributed-switch leg then
audits who the durable record says asked: an attributed `POST
/promote`/`POST /demote` declaring a `ci` actor journals `role_changed`
entries carrying that actor on *both* peers' durable records, ordered
after the request at its scan boundary, with the tracking peer's
checkpoint-adopted journal showing the same attributed record, while
the armed standby's automatic promotion at the miss budget journals
`origin: "failover"` with no operator actor — reading distinguishably
from any operator request before the pair's roles are restored.

The stage's legs are files, not entries in the check script: every
`ci/legs/<name>.py` is one leg — a runnable script carrying its
contract prose in its own docstring and its stage registration in a
module-level `LEG` literal naming the leg's declared order, its
titles, any extra released-tool flags it takes, and the doctored cases
each of which must fail carrying its named evidence. `ci/legs.py`
discovers the legs in declared order — each file's own `order` field,
unique across the directory — runs each leg twice requiring identical
digests, then exercises its declared tampers. The legs share the
launch/settle/restore harness consolidated under #647 —
`ci/legs/pair.py`'s `launch_pair`/`PairRig`, itself the stage's first
leg — and each leg restores the pair's launch roles for the next. A
leg's diagnostic stem is its file name with underscores turned to
dashes: a violated contract fails `<stem>-failed`, two passes
producing different digests fail `<stem>-nondeterministic`, and a
doctored case passing silently or missing its named evidence fails
`<stem>-unchecked`. Adding a leg is exactly one new file under
`ci/legs/` — no edit to `ci/check.sh`, the boundary lint, or this
document; the leg set and each leg's contract prose live in the
directory and its docstrings.

The check's `consumers` stage then proves the replaceable-consumer
boundary end to end — `ci/consumers.py --schedule <name>` replays the
identical driven run once per consumer schedule: `zero-clients` (no UI
attached, the control run), `polling`, `stalled-reader` (a `GET
/snapshot` response held unread across the whole run),
`disconnect-reconnect` (connect/read/drop churn, sometimes dropped
mid-response), `malformed-and-flood` (garbage bytes, half-sent
requests, refused verbs, and malformed bodies within the declared
limits, beside a read flood over every surface), and `ui-restart` (a
separate consumer process killed mid-run and restarted). Every
schedule must produce the identical `consumer-digest` over the run's
leg outcomes and command receipts, and two full passes must produce
identical digests — a divergence fails `consumer-interference`, a
nondeterministic stage `consumer-nondeterministic`.

The check's `ctl` stage then proves the shipped operator CLI from the
released artifacts — `ci/ctl.py` replays the deterministic driven run
with `dcs-ctl` as the only driver: `dcs-ctl scan` paces the run,
`dcs-ctl write` holds the exercise program's `run` input, and `dcs-ctl
invoke sequencer:<id> advance|reset` submits the kind-declared commands
through the bounded receipted path with `--actor` attribution — each
`accepted` receipt settling `applied` at the scan boundary, visible
through `dcs-ctl receipts` and journaled as `command_settled`. The leg
asserts the read subcommands answer the served contract — `signals`,
`schema`, `snapshot`, `events`, `resources` — that `resources` reports
the per-command availability beside the named refusals (the unwritable
bound point's `not declared writable`, the completed table's `advance`
reporting `available: false` carrying the kind's named refusal — the
published verdict joined into the command state — with the refused
submission's `command_refused` receipt in the attributed events), that
the kind-emitted `step_completed`/`progress` reach `dcs-ctl events`
under their `history`/`latest` marks while `sequence_completed`
journals, and that the refusal modes
exit nonzero naming the failure: an undeclared command answers
`unknown_command`, a malformed `invoke` argument fails its
declared-kind parse, and an unreachable monitor names its address. Two
passes must produce the identical `ctl-digest` — a divergence fails
`ctl-failed`, a nondeterministic stage `ctl-nondeterministic`.

The obligations this demonstrates for any monitoring or UI consumer:

- **Tolerate sequence gaps and freshness metadata.** Reads are served
  from bounded storage; a consumer that falls behind finds its `since`
  cursor's successors evicted — visible as a numbering gap on
  `GET /journal` and `GET /history` — and the served snapshot's
  `publication` section reports how much was published and coalesced
  while it was away. Coalesce onto the retained tail or the latest
  snapshot; never assume continuity.
- **Never treat UI or session loss as a plant-stopping event.** The
  controller owns execution; a consumer that stalls, disconnects, or
  restarts changes nothing — the `ui-restart` schedule's run produces
  byte-identical outputs and receipts. A transport failure is consumer
  health, not a plant condition.
- **Submit mutations only through the bounded receipted path.**
  `POST /command` is validated, bounded, and answered with a receipt —
  accepted, or a named rejection — settling at the scan boundary.
  Nothing else mutates the run: malformed bodies are refused at parse
  (`400`), refused verbs get their named status, and a consumer can
  never turn a read into a write.

Run the whole check yourself:

```sh
ci/check.sh
```

### 6. Deploy

`deploy/manifest.json` binds the approved model to the release's images
and the redundant controller pair:

- `dcs_release` — the release this deployment runs.
- `images` — the controller and plant-server image references (tags or
  recorded digests).
- `model.path` / `model.fingerprint` — the checked-in model and the
  identity checkpoint negotiation verifies on the wire.
- `dynamics.path` — the simulation dynamics `dcs-plant-server` merges.
- `dynamics.fingerprint` — **optional**: the canonical fingerprint of
  the approved dynamics document (FNV-1a over its parsed
  reserialization, `ci/dynamics_fingerprint.py --fingerprint
  model/dynamics.json` prints it). No wire role — the dynamics never
  cross the protocol — it is the check-side pin authorizing the
  document the deployment's `dynamics.path` serves.
- `plant.listen` — the plant server's listen address.
- `controllers` — the duty controller and its tracking standby
  (`standby` names the peer it follows).
- `controllers[].failover_budget` — **optional**, on a standby entry
  only: the consecutive-missed-pull budget at which the tracking
  standby self-promotes when its active dies — the declaration that
  arms automatic failover. It instantiates as the invocation's
  `--auto-promote` flag carrying exactly it; declaring the pair's
  `standby` wiring alone never arms it. A standby deployment without
  the field switches only through the operator's receipted
  `demote`/`promote` path.
- `controllers[].state_file` / `controllers[].journal_file` —
  **optional** per-controller container paths for the runtime's two
  durability files: `state_file` is the restart-recovery checkpoint —
  a restarted container resumes in place at the last persisted scan —
  and `journal_file` is the durable attributed operator-action record
  surviving the process lifetime. Each declared path must land on
  writable deployment storage and rides the invocation's
  `--state-file`/`--journal-file` flags; a deployment without durable
  storage omits both fields and the flags stay absent. A restarted
  tracking standby resumes from its declared files exactly as the
  lone controller does — rejoining in standby at the persisted tick
  and reconverging to `tracking` on its peer's checkpoints —
  `ci/legs/standby_restart.py` exercises that promise on the deployed
  pair.
- `topology` — **optional**: the deployment's named redundant pairs
  beyond the single-pair default, each `{"name": …, "members":
  [<controller>, <controller>]}` entry naming two `controllers`
  members whose standby wiring closes inside the pair — the
  declared pair index a `?pair=` overview URL is generated from.
  Each declared pair deploys its members against the manifest's one
  (model, plant) binding, so one deployment is one field — whose
  single-writer claim admits exactly one field-owning duty run
  (decision 99). A second pair's duty member, like any second
  `controllers` entry without `standby`, is a manifest that
  validates yet describes a rig that cannot run, and the deploy
  stage refuses it `rig-mismatch`; a site running several pairs
  deploys one manifest per field and names the addresses across
  them in the overview URL. A single-pair deployment omits the
  section; this manifest does.

The deployment maps directly onto the platform's documented run
commands: `dcs-plant-server <model> --dynamics <doc> --listen <addr>`
and `dcs-controller <model> --remote <addr> [--standby <peer>]
--listen <addr>`.

`deploy/compose.yaml` instantiates the manifest as a checked-in rig
definition — the same declaration the platform's `docs/packaging.md`
records as run commands: one `dcs-plant-server` container serving the
mounted model and dynamics documents read-only, and the `ctrl-a` /
`ctrl-b` pair attaching to its listener through `--remote`, the standby
following the duty's monitor through `--standby`, both monitor ports
published. Each peer's declared `state_file`/`journal_file` lands on
its writable named volume — `ctrl-a-data`/`ctrl-b-data` — and rides
the invocation's `--state-file`/`--journal-file` flags, while the
model and dynamics mounts stay read-only. Each controller invocation
carries the manifest's fingerprint in its `DCS_MODEL_FINGERPRINT`
environment — the identity checkpoint negotiation verifies on the
wire. The check's `deploy` stage parses the file through `docker
compose config` (or an equivalent YAML parser) and asserts it agrees
with the manifest on every field — release, images, mounts,
fingerprint, listen addresses, pair wiring, the standby's
`failover_budget` against its `--auto-promote` flag, and the
optional `topology` section's declared pairs — a declared
budget with no flag, a flag with no declaration, a diverging value,
the field placed on the duty entry, a pair member the manifest does
not declare, a member two pairs share, a declared pair whose
standby wiring does not close inside it, or a second field-owning
duty controller over the manifest's one plant — the shape a second
declared pair needs (decision 99) — each fail `rig-mismatch`, an
unparsable file `rig-invalid`, a host with
neither parser `rig-unverifiable`.

Running the rig is the customer action: with the release's images
available — pulled by their recorded digests or built at the release
tag per the platform's `docs/packaging.md` —

```sh
docker compose -f deploy/compose.yaml up -d
```

starts the three containers. `docker compose -f deploy/compose.yaml
ps` shows them; the monitoring page presents the pair as one logical
controller — open either peer's published monitor port and pass the
other as `?peer=`:

```
http://localhost:8080/?peer=localhost:8081
```

The declared `failover_budget` rides ctrl-b's `--auto-promote` flag:
with ctrl-a dead — stopped, crashed, or partitioned — the tracking
standby's checkpoint pulls miss, and at the third consecutive miss it
self-promotes at a scan boundary, taking the plant's writer claim so
a still-alive old owner's writes fence at the field rather than
doubling them. The documented manual switchover stays `POST /demote`
on the field-owning peer's published port followed by `POST /promote`
on the converged standby's;
`docker compose -f deploy/compose.yaml down` removes the containers
and the rig network.

The two-machine shape is the same declaration spread across hosts with
published addresses in place of network names — the cross-host form
the platform's `docs/packaging.md` records: the plant publishes its
`9001` listener on its host, each controller runs on its own machine
with `--remote <plant-host>:9001`, and the standby's `--standby` names
the duty's reachable monitor address, `<active-host>:8080` in place of
`ctrl-a:8080`.

### 7. Upgrade by repinning

A compatible upgrade is a repin: change the `rev`/`tag` in
`Cargo.toml`, run `cargo update` to move the lockfile, and re-run
`ci/check.sh`. Within a compatible crossing the supported API and
`MODEL_VERSION` are unchanged — the check passing is the upgrade's
acceptance. `ci/check.sh` proves the path itself: its `upgrade` stage
materializes this tree at the recorded upgrade-from revision — the
earliest revision the composition still builds and emits under; this
tree's `requires_reason` marks need the v0.3.0-line builder API, so
`v0.2.0`'s recorded commit is behind it — repins it to this tree's
recorded release, and re-runs the full check requiring a
byte-identical `model/plant.json`.

An **incompatible** crossing fails with named diagnostics, never
silently: a pin that resolves no release crates is `pin-unresolvable`;
a pin whose supported API no longer compiles your composition is
`surface-incompatible`; a model document the release's tooling refuses
is `tooling-rejected`; a model whose semantic content changed under a
re-recorded fingerprint is `manifest-fingerprint-mismatch`; a recorded
schema artifact the pinned tooling no longer emits byte-identically is
`schema-drift`; a served registry document or a checked-in
manifest/dynamics document failing its artifact's required structure
is `schema-mismatch`; two screening passes diverging is
`schema-mismatch-nondeterministic`; a `dcs-model diff` leg whose
expectation fails is `diff-mismatch`; a served
operator surface diverging from the emitted model's declaration is
`surface-mismatch`; a restarted controller losing its persisted run —
or failing to name a refused checkpoint — is `restart-resume-failed`;
two restart-leg passes diverging is `restart-resume-nondeterministic`;
a declared pair failing to converge, switch, or keep its journaled
record is `pair-failed`; two pair-leg passes diverging is
`pair-nondeterministic`;
a foreign-model standby failing to report its named negotiation
state, a promote against it answering anything but the named refusal,
or the attempt disturbing the active is `negotiation-failed`; two
negotiation-leg passes diverging is `negotiation-nondeterministic`;
a role boundary refusing dishonestly — a
pre-transfer promote not answering `not_converged`, a standby-directed
write not answering `not_active`, or a refusal disturbing the active —
is `refusal-failed`; two refusal-leg passes diverging is
`refusal-nondeterministic`; a proven duty-pump failure not handing
`duty` to the standby pump inside the declared bound — or the all-out
annunciation, the declared recovery, or the journaled evidence not
holding — is `handover-failed`; two handover-leg passes diverging is
`handover-nondeterministic`; a per-pump manual-takeover leg failing to
hold — a receipted mode/hand/oos write unsettled, the manual
selection's declared signals unreported, the guards ungated, the
managed alarm unannunciated, the restore not returning the pump to
group control, or the journal missing an attributed transition — is
`takeover-failed`; two takeover-leg passes diverging is
`takeover-nondeterministic`; a standing force dropped or silently
re-substituted by a promotion — the `forces` entry missing from the
promoted peer's snapshot or the sample no longer the forced value at
substituted quality — or a release leaving the set non-empty is
`force-carryover-failed`; two force-carryover passes diverging is
`force-carryover-nondeterministic`;
a foreign `?peer=` announce
landing on the field owner's monitor — the checkpoint read refusing
to answer, or the demoted peer stranding `unsynchronized` instead of
tracking its real successor — is `peer-announce-failed`; two
peer-announce passes diverging is `peer-announce-nondeterministic`;
an armed standby failing to self-promote at its declared budget —
the miss run unreported, the writer claim not fencing foreign writes
while the promoted peer's writes land, the run not continuing, or
the durable journal's transition record diverging — is
`failover-failed`; two failover-leg passes diverging is
`failover-nondeterministic`;
a staged-vs-field divergence leg failing to hold — the withheld-pull
window's field-side write not landing through the plant protocol, the
resumed pull not leaving the stale peer's report `diverged` naming the
perturbed output, the promote not answering `not_converged` or handing
the field off, the active disturbed, or the write-free control window
not reconverging and promoting — is `divergence-missed`; two
divergence-leg passes diverging is `divergence-nondeterministic`;
a tracking standby failing its declared-files restart — the resume
unreported or at the wrong tick, the rejoin claiming the field, the
reconvergence out of window, the durable boundary unordered, the
active's writes, receipts, or journal disturbed, or the pair unable
to promote afterward — is `standby-restart-failed`; two
standby-restart passes diverging is
`standby-restart-nondeterministic`;
a tracking standby whose served event
records diverge from the active's — or whose counted emission set
never stands — is `event-parity-failed`; two event-parity passes
diverging is `event-parity-nondeterministic`;
a consumer schedule changing the driven run's
outputs or receipts — or failing its own evidence — is
`consumer-interference`; and two consumer-stage passes diverging is
`consumer-nondeterministic`. The names are recorded in the platform's
`docs/release-contract.md` — the same vocabulary the platform's own
consumer-boundary checks report.
