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
and kind-emitted `step_completed` event carry the declared
command/event surface the clean check proves.

## Layout

```
Cargo.toml             the package — git-pinned to the release rev
Cargo.lock             the resolved pin — release crates from git only
rust-toolchain.toml    the toolchain the release declares
src/station.rs         the consumer-owned station composition
src/main.rs            the emit entry point
src/scenario.rs        the scripted-simulation declaration
model/plant.json       the emitted, approved plant model
model/dynamics.json    the declared simulation dynamics
ci/scenario.json       the generated scenario the CI drives
ci/check.sh            the clean-CI check a fresh clone runs
ci/simulate.py         the deterministic scripted-simulation runner;
                       --surface asserts the served operator surface —
                       signal index, interface registry, declared
                       commands, emitted events
ci/restart.py          the restart-recovery leg — the driven
                       controller stopped mid-scenario and relaunched
                       onto the same state/journal files
ci/consumers.py        the consumer-boundary driver — replays the same
                       driven run under each consumer schedule
ci/ctl.py              the dcs-ctl leg — the released operator CLI
                       driving the same run: receipted invoke, reads,
                       and the named refusal modes
ci/deploy_rig.py       the rig-definition consistency check —
                       deploy/compose.yaml against the manifest
deploy/manifest.json   the deployment declaration
deploy/compose.yaml    the checked-in rig definition instantiating it
```

## The customer path

### 1. Create your repository

Copy this tree into a fresh repository. Nothing in it references the
platform checkout — the only platform coupling is the pinned release in
`Cargo.toml`.

### 2. Pin a release

`Cargo.toml` pins the release crates by immutable revision:

```toml
dcs-build = { git = "https://github.com/Jan-Kaspar1/dcs.git", rev = "c2b5694…" }
dcs-model = { git = "https://github.com/Jan-Kaspar1/dcs.git", rev = "c2b5694…" }
```

`tag = "v0.2.0"` names the identical commit once the release tag
exists; a `rev` pin is always supported. `Cargo.lock` is committed so
every build resolves the same sources.

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
cargo install --git https://github.com/Jan-Kaspar1/dcs.git --rev c2b5694… \
    dcs-model dcs-controller dcs-plant dcs-monitor
```

The `dcs-monitor` package ships `dcs-ctl`, the released operator CLI —
`dcs-ctl <addr> schema|resources|events|snapshot|signals|receipts|
journal` reads the running controller's served contract, `invoke`
submits a component's declared command through the bounded receipted
path with `--actor` attribution, and `write`/`set-parameter`/`force`/
`promote`/`demote`/`scan` cover the rest of the operator surface.

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
`command_settled` record; every kind-emitted event — the sequencer's
`step_completed` — must reach the consumer-visible record once the run
drives its declaring component to emission, appearing in `GET
/journal`'s `event_emitted` entries and the instance-attributed
`events` of `GET /resources`; the snapshot's `descriptors` must cover
every composed component; and `GET /journal` must answer the run's
recorded transitions. A divergence fails `surface-mismatch`.

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
bound point's `not declared writable`, the completed table's
`command_refused` in the attributed events), that the kind-emitted
`step_completed` reaches `dcs-ctl events`, and that the refusal modes
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
- `plant.listen` — the plant server's listen address.
- `controllers` — the duty controller and its tracking standby
  (`standby` names the peer it follows).
- `controllers[].state_file` / `controllers[].journal_file` —
  **optional** per-controller container paths for the runtime's two
  durability files: `state_file` is the restart-recovery checkpoint —
  a restarted container resumes in place at the last persisted scan —
  and `journal_file` is the durable attributed operator-action record
  surviving the process lifetime. Each declared path must land on
  writable deployment storage and rides the invocation's
  `--state-file`/`--journal-file` flags; a deployment without durable
  storage omits both fields and the flags stay absent.

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
fingerprint, listen addresses, and pair wiring; a divergence fails
`rig-mismatch`, an unparsable file `rig-invalid`, a host with
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

The documented switchover is `POST /demote` on the field-owning peer's
published port followed by `POST /promote` on the converged standby's;
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
`ci/check.sh`. Within a release's minor series the supported API and
`MODEL_VERSION` are unchanged — the check passing is the upgrade's
acceptance. `ci/check.sh` proves the path itself: its `upgrade` stage
materializes this tree at the recorded release rev, repins it to a
later compatible revision, and re-runs the full check requiring a
byte-identical `model/plant.json`.

An **incompatible** crossing fails with named diagnostics, never
silently: a pin that resolves no release crates is `pin-unresolvable`;
a pin whose supported API no longer compiles your composition is
`surface-incompatible`; a model document the release's tooling refuses
is `tooling-rejected`; a model whose semantic content changed under a
re-recorded fingerprint is `manifest-fingerprint-mismatch`; a served
operator surface diverging from the emitted model's declaration is
`surface-mismatch`; a restarted controller losing its persisted run —
or failing to name a refused checkpoint — is `restart-resume-failed`;
two restart-leg passes diverging is `restart-resume-nondeterministic`;
a consumer schedule changing the driven run's
outputs or receipts — or failing its own evidence — is
`consumer-interference`; and two consumer-stage passes diverging is
`consumer-nondeterministic`. The names are recorded in the platform's
`docs/release-contract.md` — the same vocabulary the platform's own
consumer-boundary checks report.
