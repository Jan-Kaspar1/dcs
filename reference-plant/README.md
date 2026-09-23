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
ci/alarm_validation.py the alarm-validation leg — the emitted model's
                       managed-alarm record audited, doctored copies
                       refused by the released `dcs-controller
                       --check`, and the driven run's served
                       components/parameters sections reporting the
                       same record
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
ci/pair.py             the redundant-pair leg — the manifest-declared
                       standby pair run, switched, and checked against
                       its persisted state and journal files
ci/negotiation.py      the checkpoint-negotiation leg — a third
                       released controller launched --standby at the
                       pair's active on a foreign-fingerprint model
                       document, asserting the named non-converged
                       state, the refused promote, and the
                       undisturbed active
ci/refusal.py          the pair contract's refusal half — the
                       pre-transfer promote answering not_converged,
                       the standby-directed write answering not_active,
                       the same promote succeeding once tracking
ci/handover.py         the pair contract's failure-handover leg — a
                       proven duty-pump failure handing duty to the
                       standby pump inside the declared bound, the
                       all-out none_available/all_faulted annunciation,
                       and the declared recovery with the pair's
                       controller roles unmoved
ci/takeover.py         the pair contract's takeover leg — the settled
                       pair driven until the pump group holds a duty
                       demand, then the declared per-pump mode seam
                       exercised through the receipted path: mode
                       cutting the pump off the group's request,
                       hand running it under the declared guards and
                       a driven protection input, oos inhibiting it,
                       and the restore returning it to group control
ci/force_carryover.py  the pair contract's force-carryover leg — a
                       receipted force on a declared writable In point
                       while tracking, asserted on both peers'
                       snapshots, then still standing on the promoted
                       peer after the switch, released through the
                       receipted path, and the pair's roles restored
ci/tune_carryover.py   the pair contract's tune-carryover leg — a
                       receipted set_parameter on a declared writable
                       configuration point while tracking, asserted
                       live on both peers' parameter reports and the
                       bound point's declared-signal reading, then
                       still live on the promoted peer after the
                       switch with the promotion journaled after the
                       tune's settlement, a further tune settling
                       applied with a fresh receipt, and the pair's
                       roles restored
ci/force_release.py    the pair contract's force-release leg — a
                        receipted force substituted at
                        Uncertain(Substituted) across scans on the
                        settled pair, the receipted unforce releasing
                        it at its apply tick with the live value
                        resumed and the forced set cleared, the
                        released state carried through the switch and
                        the launch roles restored, and every
                        transition journaled on the durable record
                        with the standby's adopted log answering the
                        same receipts
ci/stale_checkpoint.py  the pair contract's stale-checkpoint leg — a
                        receipted force carried through the switch
                        and receipted-released on the new active,
                        then the tracking peer restarted onto its
                        declared state/journal files so it re-adopts:
                        the released force must not re-stand, the
                        live value keeps serving unforced, the
                        adopted log preserves the unforce settlement,
                        and no phantom force receipt journals on
                        either peer, with the launch roles restored
ci/burst_order.py      the pair contract's alarm-burst leg — a
                       deterministic consequential cascade driven
                       through the plant protocol on the settled pair,
                       every driven alarm asserted through the active's
                       monitor, and the durable journal's ordered
                       point_changed record audited for the driven
                       activation order with no dropped or reordered
                       entries, the restores journaled in order, and
                       the pair's roles unchanged
ci/peer_announce.py    the pair contract's peer-announce leg — a
                       foreign GET /checkpoint?peer=<closed-port> on
                       the tracking pair's field owner refused while
                       the checkpoint read answers, the demote/promote
                       switch reconverging the demoted peer tracking
                       on its real successor, and the pair's launch
                       roles restored
ci/availability.py     the pair contract's command-availability leg —
                       the tracking pair's served per-command verdicts
                       audited self-consistent and identical across the
                       peers, every served-unavailable bound-point
                       command probe-submitted through POST /command
                       settling its named refusal rather than applied,
                       a served-available command settling applied into
                       both peers' adopted receipt log, and the
                       kind-declared advance's refusal exercised where
                       the tooling publishes verdicts
ci/failover.py         the pair contract's automatic-failover leg —
                       the manifest's failover_budget arming the
                       standby's --auto-promote, the field-owning
                       container stopped, the surviving peer's miss
                       run and self-promotion at the declared budget
                       asserted through its served surface and the
                       plant's writer claim, the run continuing
                       bumplessly, and the durable journal's
                       transition record audited beside a
                       severed-standby variant reporting no failover —
                       then the declared measurement contract on the
                       settled pair: the primary level source's
                       quality fault serving the backup under the
                       managed alarm's journaled annunciation, the
                       backup's as well engaging the declared
                       on_bad_demand fallback, and the restores
                       returning the selection and the alarms per
                       their lifecycle
ci/divergence.py       the pair contract's staged-vs-field
                       divergence leg — the tracking standby's
                       checkpoint pulls withheld through an
                       observation window while a field-side write
                       lands through the run's plant-protocol
                       client, the stale peer's served diverged
                       report naming the perturbed output and its
                       promotion refused not_converged with no field
                       hand-off, and a write-free control window
                       reconverging and promoting normally
ci/standby_restart.py  the pair contract's standby-restart leg —
                       the tracking standby's container stopped and
                       relaunched onto its declared
                       --state-file/--journal-file, resuming at the
                       persisted tick and reconverging to tracking
                       inside the declared window while the active's
                       writes, receipts, and journal run undisturbed,
                       then promoting to prove the pair left whole
ci/startup_claim.py    the pair contract's startup-claim ordering
                       leg — with the pair switched so the field
                       owner holds the plant's writer claim, a third
                       released controller launched against the same
                       plant address on a doomed --journal-file
                       (a corrupt first record its startup replay
                       cannot read), asserting the doomed spawn
                       aborts before its preemptive claim while the
                       incumbent keeps role, tick, writes, receipts,
                       and the claim's fencing, and the pair restores
                       its launch roles once the process stops
ci/report.py           the pair contract's alarm-report leg — the
                        released `dcs-alarm-report` computing the
                        declared AlarmReport metric set over the
                        driven pair's served journal and the field
                        owner's durable journal file, its refusal
                        modes exiting nonzero
ci/command_switch.py   the pair contract's command-switch leg — the
                        exercise sequencer's kind-declared `advance`
                        invoked through the released `dcs-ctl invoke`
                        before and after a receipted demote/promote,
                        each submission settling exactly once with
                        the receipt attributed to the serving peer,
                        the emitted events continuing monotonically
                        with unchanged attribution, a carried invoke
                        settling exactly once across the restore
                        switch, and the pair's launch roles restored
ci/demote_pending.py   the pair contract's demote-boundary
                        pending-command leg — a receipted write
                        admitted on the active and demoted past
                        inside its pending window settling exactly
                        once through the carry or the named
                        superseded rejection, both peers' journals,
                        receipt logs, images, and durable files
                        audited for the single audited settle, and
                        the pair's launch roles restored
ci/demote_reconvergence.py  the pair contract's demote-follow
                        reconvergence leg — under the manifest's
                        declared 0.0.0.0 listen binds, the documented
                        switch in both directions reconverging each
                        demoted peer to tracking on the successor's
                        dialable announced source — never the
                        wildcard, itself, or a foreign address —
                        held across the pull train, and the pair's
                        launch roles restored
ci/managed_lifecycle.py  the pair contract's managed-alarm
                        lifecycle leg — the emitted model's whole
                        managed-alarm surface exercised end to end
                        on the deployed pair: activation, attributed
                        ack, the bounded shelve and its auto-release,
                        the named never-shelvable refusal, and the
                        designed oos/suppress wiring through its
                        suppressed trip and return to service
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
ci/monitor_starvation.py  the pair contract's monitor-starvation leg
                       — the saturating set of incomplete-body
                       connections held against the armed pair's field
                       owner while the serving lane's GET /role,
                       /snapshot, and /checkpoint keep answering
                       inside the declared bound on both peers, the
                       standby's checkpoint pulls keep landing past
                       the armed miss budget with no role transition
                       or field_claim_lost journaled, fencing probes
                       stay fenced, and closing the set restores
                       driven scans and a receipted command with the
                       pair's roles unchanged
ci/consumers.py        the consumer-boundary driver — replays the same
                       driven run under each consumer schedule
ci/ctl.py              the dcs-ctl leg — the released operator CLI
                       driving the same run: receipted invoke, reads,
                       and the named refusal modes
ci/deploy_rig.py       the rig-definition consistency check —
                       deploy/compose.yaml against the manifest
ci/schema_conformance.py  the served-registry structural conformance
                       check — the driven run's `GET /schema` document
                       against the release record's
                       block-interfaces.schema.json (stdlib-only)
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
`promote`/`demote`/`scan` cover the rest of the operator surface — and
`dcs-alarm-report`, the alarm flood and performance report —
`dcs-alarm-report <addr>` computes the declared `AlarmReport` metric
set over the served journal, `dcs-alarm-report <addr> --journal-file
<path>` over the durable journal file.

The stage also exercises the contract's remaining `dcs-model` surfaces.
The release record's schema artifacts —
`docs/releases/<tag>/plant-model.schema.json` and
`docs/releases/<tag>/block-interfaces.schema.json`, where `<tag>` is
the manifest's `dcs_release` — are fetched through the same git remote
and pinned revision the crates and tooling resolve over (the record
lives in the release repository's tree at the pinned commit), and
`dcs-model schema` / `dcs-model interface-schema` at the pinned rev
must emit those bytes exactly — a divergence is `schema-drift`.
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

The check's `fingerprint` stage then authorizes the deployment's
recorded fingerprints against the approved documents and the bytes
the deployed pair serves. The emitted model's fingerprint must equal
the manifest's recorded `model.fingerprint` — a divergence is
`manifest-fingerprint-mismatch`. For dynamics the same canonical
fingerprint contract applies: `ci/dynamics_fingerprint.py` launches
the manifest-declared pair on the released tooling serving the
manifest's declared `dynamics.path` — the document the rig mounts
read-only and `dcs-plant-server --dynamics` merges; the plant
protocol exposes no dynamics surface and none is added, so the leg
pins the deployment-declared document the pair actually runs — and
fingerprints it canonically (FNV-1a over the parsed document's
reserialization, the same sixteen-hex-digit shape
`model.fingerprint` carries). The served fingerprint must equal both
the manifest's optional `dynamics.fingerprint` and the checked-in
`model/dynamics.json`; any divergence reports
`manifest-fingerprint-mismatch` naming the expected and served
fingerprints and the first diverging element. A manifest omitting
the optional field declares no dynamics pin; the
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
kind-emitted event — the sequencer's `step_completed` — must reach the
consumer-visible record once the run drives its declaring component to
emission, appearing in `GET /journal`'s `event_emitted` entries and
the instance-attributed `events` of `GET /resources`; the snapshot's
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
wiring and persistence fields statically, while `ci/pair.py` reads
those fields out of `deploy/manifest.json` and spawns
`dcs-plant-server` plus two released `dcs-controller --driven --remote`
instances wired exactly as the manifest declares — the standby's
`--standby` flag at the peer it names, each controller's declared
`--state-file`/`--journal-file` carried at runner-owned scratch paths.
The leg converges the standby to `tracking` through the served `GET
/role`, drives scans through `POST /scan` on each peer keeping their
served snapshots identical, submits a kind-declared command the owner
receipts `accepted` and the standby refuses `not_active`, and issues
the receipted switchover — `POST /demote` on the field owner, `POST
/promote` on the converged standby, each answered by its role report.
The run must continue bumplessly: the promoted peer settles `active` at
the continuing tick, the demoted peer reconverges `tracking`, the
adopted receipt log stays identical on both peers, and each peer's
durable journal file carries the run's `role_changed` transitions
beside the settled receipt — the persistence vocabulary's first
behavioral exercise, not just its static agreement. A violated
contract fails `pair-failed`; two passes must produce the identical
`pair-digest`, a divergence failing `pair-nondeterministic`.

The stage's negotiation leg then proves the deployed pair degrades
honestly on the misconfiguration writing your own manifests can
produce: `ci/negotiation.py` launches a third released
`dcs-controller` as `--standby <active>` on a foreign document — the
emitted model doctored inside `MODEL_VERSION` to a fingerprint the
running pair does not serve, without `--revised` — and drives scans
through `POST /scan`. For the whole observation window the peer must
report `standby` plus the named `degraded` negotiation state — the
pulled checkpoint's model fingerprint named against its own —
`POST /promote` against it must answer the named refusal, `409
not_converged` carrying that state rather than a silent or wrong
verdict, and the field owner's writes, receipt log, and journal must
stay undisturbed: the refusal is the honest answer to a mismatched
deployment, never a silent adoption or a disturbed pair. The foreign
peer tears down before the later legs run, and a control peer — the
same third controller on the pair's own model — converges and promotes
normally, so the refusal names the negotiation failure rather than a
rig defect. A violated contract fails `negotiation-failed`; two
passes must produce the identical `negotiation-digest`, a divergence
failing `negotiation-nondeterministic`.

The stage's startup-claim ordering leg — `ci/startup_claim.py` on
the same declared deployment — proves a doomed startup can never
strand the plant's write claim over the running active. With the
pair switched so the field owner holds the claim, the leg launches
a third released `dcs-controller` against the same plant address on
a doomed `--journal-file` — a corrupt first record its startup
replay cannot read. The spawn must abort at startup validation
naming the replay failure — never reporting a listener, never
logging the preemptive claim — while the incumbent keeps `active`,
its tick advances, its field writes and a mid-window receipted
command keep landing, a foreign attachment's mutation probe stays
`fenced` under the standing claim, and its journal gains no role,
fencing, divergence, or restart records. The doomed peer's journal
file must still hold exactly the corrupt record and its state file
must never appear; once the foreign process is gone the pair
restores its launch roles. A violated contract fails
`startup-claim-failed`; two passes must produce the identical
`startup-claim-digest`, a divergence failing
`startup-claim-nondeterministic`.
The stage's refusal half — `ci/refusal.py` on the same declared
deployment — then proves the pair refuses honestly at its role
boundaries, the half a customer driving their own pair needs the
deployed controllers to answer. `POST /promote` on the freshly
launched standby — before its first checkpoint transfer completes —
answers the named `409 not_converged` refusal and hands nothing off:
the duty stays `active`, the standby stays `unsynchronized`, and no
`role_changed` lands in either peer's journal. A receipted
`write_value` against a declared writable point submitted to the
tracking standby's monitor answers the named `not_active` rejection —
the point unchanged in the active's served snapshot, the write absent
from both peers' adopted receipt logs, and no `command_settled`
journal entry on either peer recording it as anything but the named
refusal. And once the standby tracks, the same `POST /promote`
succeeds through the documented `demote`/`promote` switch — the
active's field writes, receipts, and journal undisturbed throughout.
A violated contract fails `refusal-failed`; two passes must produce
the identical `refusal-digest`, a divergence failing
`refusal-nondeterministic`.

The stage's failure-handover leg — `ci/handover.py` on the same
declared deployment — then proves decision 41's central behavior on
the consumer pair: with the pair settled and the group holding a duty
demand, the leg faults the `p101-run` field channel through the plant
protocol's declared `inject_fault` — the honest failure lever, bad
quality on the channel — and asserts through the active's monitor that
`duty` moves to the standby pump inside the declared bound (the
motor's `fault_ticks` proof plus the carrier hop the group's `fault_i`
read crosses), `staged` reports the surviving pump against the
standing demand, `p101-fault` reports the proven exclusion while
`p101-avail` keeps reporting the permissive aggregate it wires, and
the managed `p101-fault` alarm annunciates with journaled
`point_changed` evidence on the fault flag, the alarm, and the
unacknowledged latch. Faulting the remaining pump's channel then
annunciates the all-out conditions — `none_available`, `all_faulted`,
and both managed alarms — and clearing each injected fault restores
the declared recovery: the fault flags clear once command and
feedback agree, the annunciation returns, the duty designation
reassigns under the declared rotation, and the unacknowledged latches
hold awaiting an acknowledgment the leg never sends. The pair's
controller roles never move — a field fault is a plant event, not a
failover — and the peers' images stay identical through every driven
scan. A violated contract fails `handover-failed`; two passes must
produce the identical `handover-digest`, a divergence failing
`handover-nondeterministic`.

The stage's takeover leg — `ci/takeover.py` on the same declared
deployment — then proves the per-pump mode seam the emitted model
declares actually holds for an operator on the customer's pair. With
the pair tracking and the pump group holding a duty demand on pump 1,
the leg submits a receipted `write_value` on `p101-mode` through the
active's `POST /command` — the ticket's chosen released-tooling path —
and asserts the settled `applied` receipt landing identically in both
peers' adopted log while the pump's delivered command leaves the
group's `cmd_1`: the auto-leg carriers report the manual selection and
the pump-group status reflects the exclusion, the standing demand
handed to pump 2. A receipted `p101-hand` then runs the pump on the
operator demand while the declared thermal/moisture guards still gate
it inside the availability aggregation; the leg drives the protection
input through the plant protocol — an injected non-Good on the run
contact, the simulator's unfenced diagnostic surface — asserting the
proven command/feedback `fault` and its managed alarm's standing and
unacknowledged flags, cleared through `clear_fault` and a receipted
`ack`. A receipted `p101-oos` asserts the maintenance inhibit — the
in-service guard cutting the hand request and the managed
`out_of_service`/`suppressed` states annunciating — and the restore
returns `mode`/`oos`/`hand`, the pump rejoining the group's roster
with its delivered command following the group request again. The
active's served `GET /journal` must carry each attributed transition
in `seq` order — every receipted write's `applied` settlement named to
the leg's actor beside the journaled mode, protection, alarm, inhibit,
and restore transitions. A violated contract fails `takeover-failed`;
two passes must produce the identical `takeover-digest`, a divergence
failing `takeover-nondeterministic`.

The stage's force-carryover leg — `ci/force_carryover.py` on the same
declared deployment — then proves a standing operator force survives
the switch on the customer's pair, the continuity clause applied to
forcing. With the pair tracking, the leg records the target point's
unforced sample — the control proving the unforced value is what
would otherwise have served — and submits `force_point` through the
active's `POST /command` — the leg's chosen receipted path. The
emitted model marks only *internal* `In` points writable (the
operator seam: alarm acks, per-pump `mode`/`hand`/`oos`, the exercise
`run` request) — the released contract accepts internal targets, and
the leg's `p101-hand` is the honest one: gated by the manual-mode AND
while the pump stands in auto, so forcing it perturbs nothing
downstream. The `accepted` submission must settle `applied`
identically into both peers' adopted log, and the snapshot's `forces`
entry must name the point with the forced value stamped
`Uncertain(Substituted)` on both peers — the tracking standby's scans
run adopted state, so its snapshot already carries the force. The
leg then issues the `demote`/`promote` switch and asserts on the
promoted peer — across driven scans — that `forces` still names the
point and the sample stays the forced value at substituted quality:
the force set rides the checkpoint through the promotion exactly as
it does on the platform's lane, never released and never silently
re-substituted. A receipted `unforce_point` on the new active must
settle `applied`, empty the `forces` list, and resume the point's
unforced serve — for an internal target the held-value rule leaves
the force's last stamp re-stamped `Good`, the observable state a
same-value `write_value` produces (a field point would resume the
driver's read; the emitted model declares no writable field `In`
point). The leg then restores the pair's declared roles — `demote`
on the new owner, `promote` on the reconverged peer — leaving the
manifest's duty controller `active` and its standby `tracking`. A
violated contract fails `force-carryover-failed`; two passes must
produce the identical `force-digest`, a divergence failing
`force-carryover-nondeterministic`.

The stage's tune-carryover leg — `ci/tune_carryover.py` on the same
declared deployment — then proves a customer's runtime parameter
tuning survives the switch on the customer's pair, the
runtime-tuning-continuity clause applied to the receipted
`set_parameter` path exactly as the platform's lane proves it. With
the pair tracking, the leg records the target's served value — the
control proving the un-tuned default is what an un-carried tune
would read — and submits `set_parameter` through the active's
`POST /command`. The target is the exercise `sequencer`'s
`step_1_out`, the honest writable configuration point the emitted
model declares: a descriptor-declared sequence parameter whose
served registry entry carries its `set_parameter` command, and
whose parked table's demand lands on the `out` port's next sample
while nothing downstream consumes it — the point the declared
`exercise-out` signal sources. The `accepted` submission must
settle `applied` identically into both peers' adopted log, the
tuned value live on both peers' served parameter reports and on
the signal's reading. The leg then issues the `demote`/`promote`
switch and asserts on the promoted peer — across driven scans —
that the parameter report and the signal's reading still carry the
tuned value: runtime tuning rides the checkpoint through the
promotion exactly as it does on the lane, never reverted to the
model-declared default. The promoted peer's served journal must
order the promotion's `role_changed` entries after the tune's
`command_settled`, and a further `set_parameter` on the new active
must settle `applied` with a fresh receipt — the promoted peer's
own command path proven live. The leg then restores the pair's
declared roles, leaving the manifest's duty controller `active`
and its standby `tracking`. A violated contract fails
`tune-carryover-failed`; two passes must produce the identical
`tune-digest`, a divergence failing
`tune-carryover-nondeterministic`. The leg's doctored case — an
expectation asserting the parameter's original value after the
tune — must report the named diagnostic rather than pass silently.

The stage's force-release leg — `ci/force_release.py` on the same
declared deployment — then proves the release half of the forcing
contract on the customer's pair, the substituted-data honesty the
carryover leg's carry leaves unproven. With the pair tracking, the
leg records the target's unforced sample — the control proving the
unforced value is what would otherwise have served — and submits
`force_point` on the same writable internal target through the
active's `POST /command`, asserting the `applied` settlement at the
applying scan's tick identical in both peers' adopted log, the
`forces` entry and the `Uncertain(Substituted)` sample on both peers,
the substituted stamp standing across further driven scans, and the
active's served journal carrying the attributed settlement. A
receipted `unforce_point` must then release the point at its apply
tick: the `forces` set empty and the live value resumed — for the
internal target the held-value rule leaves the force's last stamp
re-stamped `Good`, the observable state a same-value `write_value`
produces — on both peers, its `applied` settlement landing at that
scan boundary in the identical adopted log and the held image never
re-substituting across further scans. A receipted `write_value`
returning the pre-force held value proves the live write path
resumed. The leg then issues the `demote`/`promote` switch and
asserts the promoted peer serves the released state — no resurrected
force, the live image carried like any run state — before restoring
the pair's launch roles, leaving the manifest's duty controller
`active` and its standby `tracking`. The field owner's durable
journal file must carry the force, release, and restore settlements
attributed to the leg's actor beside the quality transitions and the
pair's role records in `seq` order — the served `GET /journal`
answering the same record — while the standby's adopted receipt log
answers the same settled receipts throughout. A violated contract
fails `force-release-failed`; two passes must produce the identical
`force-release-digest`, a divergence failing
`force-release-nondeterministic`.

The stage's stale-checkpoint leg — `ci/stale_checkpoint.py` on the
same declared deployment — then pins the receipted-command
contract's adoption rule on the customer's pair (WW-ENG-003,
WW-LCM-001): with the pair tracking, a receipted `force_point` on
the declared writable internal `In` point through the active's
receipted path — the honest internal target per the
force-carryover convention — settling `applied` with the
`Uncertain(Substituted)` stamp, the documented switch carrying the
force onto the promoted peer, and a receipted `unforce_point` on
the new active settling `applied` with the emptied `forces` entry
and the journaled release on both peers' adopted records. The
tracking peer then restarts onto its declared
`--state-file`/`--journal-file` — the field owner driven through
the downtime, the resumed peer reconverging to `tracking` inside
the declared window — driving a staler-image adoption per the
reproduction shape, and the leg asserts the force does not
re-stand: the served `forces` stays empty, the point's live value
keeps serving unforced, the adopted receipt log preserves the
unforce settlement exactly once, and no phantom force receipt
appears on either peer's served or durable journal — before the
pair's launch roles restore. A violated contract fails
`stale-checkpoint-failed`; two passes must produce the identical
`stale-checkpoint-digest`, a divergence failing
`stale-checkpoint-nondeterministic`.

The stage's alarm-burst leg — `ci/burst_order.py` on the same declared
deployment — then proves the durable ordered transition record keeps
first-out order through a consequential alarm burst on the customer's
pair, the incident-review evidence the emitted alarm set exists to
give. With the pair tracking and the pump group holding a full demand
— both pumps staged and running — the leg drives the cascade through
the plant protocol's unfenced surface: a quality fault on
`level-primary` fails the measurement over so `backup-active` and its
managed alarm annunciate first; the `power-fail` contact is written so
the station permissives drop — the availability aggregation losing its
power-ok leg, both pumps de-staging, `none-available` annunciating —
and the power alarm fires while the undrawn level climbs again; then
both run contacts' quality is faulted so each motor's proven
command/feedback `fault` asserts and the group's `all_faulted`
roll-up lands last. Through the active's monitor the leg asserts every
driven alarm's `alarm`/`unacknowledged` standing, then restores each
input in driven order — the cleared instrument, the healthy contact,
the good run feedback — so the journal carries the returns beside the
assertions. The field owner's durable journal file must carry every
driven activation and return in `seq` order — the first-out record,
equal-tick transitions ordered by sequence rather than tick — with no
dropped or reordered entries, the served `GET /journal` answering the
same record, and the pair's roles unchanged throughout. A violated
contract fails `burst-order-failed`; two passes must produce the
identical `burst-order-digest`, a divergence failing
`burst-order-nondeterministic`. The leg's doctored cases — a record
dropping a driven transition or carrying them out of order — must
report the named diagnostic rather than pass silently.

The stage's peer-announce leg — `ci/peer_announce.py` on the same
declared deployment — then proves the checkpoint `?peer=` announce
acceptance contract on the customer's pair. A tracking standby's
per-scan pull announces its own monitor address on the field owner —
the tracking source a demoted owner later follows — and the serving
monitor records an announce only when it names the pulling
connection's own source address: a foreign client cannot rewrite
where a demoted field owner tracks, the poisoning that would strand
it `unsynchronized` and unpromotable. With the pair tracking and the
genuine announce recorded, the leg issues a `GET
/checkpoint?peer=<closed-port>` naming a dead address on a foreign
IP — a source the pulling connection does not own. The checkpoint
read must still answer the owner's checkpoint while the crafted
announce is refused — it cannot overwrite the recorded tracking
source. The `demote`/`promote` switch then proves the record: the
crafted announce is issued again at the decisive point — after the
promote's own re-announce, before the demoted peer's first tracking
pull, the last write its fallback would follow — and the demoted
field owner reconverges `tracking` on its real successor where a
landed foreign address would have stranded it on a dead pull. The
leg restores the pair's launch roles, leaving the
manifest's duty controller `active` and its standby `tracking`. A
violated contract fails `peer-announce-failed`; two passes must
produce the identical `peer-announce-digest`, a divergence failing
`peer-announce-nondeterministic`. The leg's doctored case — a
crafted announce naming the pulling connection's own source,
landing exactly as it would on a controller whose acceptance check
regressed — must strand the demoted peer and report the named
diagnostic rather than pass silently.

The stage's command-availability leg — `ci/availability.py` on the
same declared deployment — then proves the served per-command
verdicts agree with what the receipted path settles: a read model
reporting a command invocable while dispatch refuses it — or the
reverse — is exactly the consumer-facing dishonesty the served
interface exists to prevent. With the pair tracking, the leg audits
every `GET /resources` command row the active serves: an
`available: false` row must carry a named refusal, an `available`
row none. Every `bound_point_writable` command the row reports
unavailable is then probe-submitted through the active's `POST
/command` — each must settle a named rejection rather than
`applied`, and where the bound point is one the model declares, the
receipt names the same refusal the row served. One served-available
command — the exercise program's kind-declared `advance` — must
settle `applied` identically into both peers' adopted receipt log,
and where the tooling publishes `command_verdicts` the leg drives
`advance` to its standing refusal: the row then serves `available:
false` with the kind's named reason, and a resubmission must settle
`command_refused` carrying that refusal verbatim. A release
publishing no verdicts records that coverage limitation in the
leg's evidence rather than failing on machinery it lacks. The
tracking standby's `GET /resources` must report identical verdicts
throughout — the same-adopted-state rule means availability never
diverges across the pair. A violated contract fails
`availability-failed`; two passes must produce the identical
`availability-digest`, a divergence failing
`availability-nondeterministic`. The leg's doctored cases — a
served-available command settling a refusal at the standby's role
gate, and a standby reporting different verdicts — must report the
named diagnostic rather than pass silently.

The stage's automatic-failover leg — `ci/failover.py` on the same
declared deployment — then proves the unattended half of the
redundancy contract on the customer's pair: a dead active's armed
standby self-promoting at its declared miss budget with no operator
request. The manifest's `failover_budget` is what arms it — the
declaration instantiates as the standby's `--auto-promote` flag, so
a declared pair without the field tracks and switches manually but
never self-promotes. The leg converges the pair, stops the
field-owning container — the honest severance a dead controller is —
and drives the surviving peer's scans: each `POST /scan`'s
checkpoint pull fails, `GET /role` reports the miss run as `standby`
under the `degraded` sync state, and the field's writer claim keeps
fencing a foreign attachment — the dead owner's silence is exactly
the failure the claim exists to fence. At the declared budget's scan
boundary the leg asserts `GET /role` settles `active` at the
expected tick, and probes the plant's writer claim through the
run's plant-protocol client: a foreign attachment's mutation stays
`fenced` while the promoted peer's own writes demonstrably land —
the claim the promotion took before its gate lifted now names this
peer. The run must continue bumplessly: subsequent driven scans
keep writing the field, the promoted peer's steps move the process
the dead owner left frozen, and a receipted kind-declared command
settles `applied`. The promoted peer's durable `--journal-file`
must carry `standby → promoting → active` in `seq` order attributed
to the budget boundary — the automatic transition reading
distinguishably from an operator-requested switch: where the
recorded non-operator marker of the attributed role switch has
landed the entry names it; where it has not, the entry carries no
operator actor rather than a fabricated one. A variant run severs
the standby instead: the field owner's writes run undisturbed and
nothing reports a failover. A measurement run on a freshly
converged pair then exercises the declared measurement contract —
the emitted model's failover-select, threshold chain, and managed
`backup-active` alarm resolved from the artifact's own wiring:
degrading the primary level source's field channel asserts
`backup_active` while the station keeps controlling on the
selected backup measurement and the managed alarm annunciates with
journaled `point_changed` evidence; degrading the backup as well
engages the declared all-sources-bad fallback — the selection
untrusted, `demand` at `on_bad_demand` with the alarmed state
standing — rather than control on bad data; restoring the backup
then the primary returns the selection to the primary and the
alarm per its declared lifecycle — the unacknowledged latch
standing until the receipted ack clears it — the durable journal
carrying the transitions in driven order and the pair's roles
unchanged. A violated contract fails
`failover-failed`; two passes must produce the identical
`failover-digest`, a divergence failing
`failover-nondeterministic`.

The stage's staged-vs-field divergence leg — `ci/divergence.py` on
the same declared deployment — then proves the failover-integrity
clause's divergence half on the customer's pair, the honest answer
promoting a stale standby gets. With the pair settled and tracking,
the leg withholds the tracking standby's checkpoint pulls for an
observation window — the driven run makes the partition literal, no
scan reaching either peer — while a field-side write lands through
the run's dedicated plant-protocol client, the same connection the
simulate stage's `inject_fault`/`clear_fault` ops use: the client
joins the field's writer claim under the duty's recorded owner
token, writes the carried `p101-cmd` output the standby's staged
image covers, and hands the hold back, so the standby's staged copy
no longer matches the field it would take over. The pull path
resumed, the stale peer's served `GET /role` must report `standby`
under the `diverged` sync state naming the perturbed output with
both sides' values — the journaled `divergence_detected` record
carrying the same evidence on the served and durable records — and
`POST /promote` must answer the named `409 not_converged` refusal
with the diverged report attached: the gate never hands the field
to an image the field no longer matches. No hand-off occurs — the
duty stays `active`, the stale peer stays `standby` and diverged,
no `role_changed` lands on either peer's journal, the injected
write stands un-overwritten — and the active's writes, receipts,
and journal run undisturbed throughout: the duty's next driven
scan's write lands and restores the field, so the standby's next
same-tick comparison matches again and journals
`divergence_resolved`, the verdict clearing back to `tracking` on
positive evidence. A control leg runs a fresh pair through the
identical partition window with no field-side write: the resumed
pull's comparison matches, the standby stays `tracking`, and the
receipted `demote`/`promote` switch succeeds — proving the refusal
names the staged-vs-field divergence rather than mere partition
staleness. A violated contract fails `divergence-missed`; two
passes must produce the identical `divergence-digest`, a
divergence failing `divergence-nondeterministic`. The leg's
doctored case — the field-side write skipped while the refusal is
still asserted — must report the named diagnostic rather than
pass silently.

The stage's standby-restart leg — `ci/standby_restart.py` on the
same declared deployment — then proves the standby half of the
restart-recovery contract on the customer's pair: the
non-field-owning peer restarted onto its declared files must
resume, re-pull, and reconverge to tracking while the active's
field ownership never falters. With the pair tracking and a
receipted command settled `applied` into both peers' adopted log,
the leg stops the tracking standby's container and keeps driving
the field owner — each driven scan's writes landing on the
simulated field and a second command settling `applied` — then
relaunches the standby on the manifest's wiring with its declared
`--state-file`/`--journal-file`. The startup preamble must report
the resume at the persisted tick — never a silent cold start at
tick zero — the served `GET /role` must report `standby`
unsynchronized rather than a field claim, and the durable journal
must carry the restart's `run_boundary` ordered after run 1's
entries with the `seq` order continuing across it. Driven
tracking-first ticks must reconverge the resumed peer to
`tracking` inside the leg's declared window, the adopted receipt
log identical on both peers — the downtime command included — and
the active's journal running undisturbed throughout. The
documented `demote`/`promote` switch on the reconverged pair then
proves the restart left no wedge: the restarted peer promotes and
the demoted owner reconverges `tracking` on it. A violated
contract fails `standby-restart-failed`; two passes must produce
the identical `standby-restart-digest`, a divergence failing
`standby-restart-nondeterministic`. The leg's doctored cases — a
state file gone missing at the restart point, and the restart's
assertions held against a peer never restarted — must report the
named diagnostic rather than pass silently.

The stage's alarm-report leg — `ci/report.py` on the same declared
deployment — then proves the released alarm flood and performance
tool produces the declared `AlarmReport` metric set (WW-ALM-004)
from the customer's released artifacts, the tooling-side computation
the flood-and-performance decision promises with no served endpoint
added. With the pair tracking, the leg drives one managed alarm
through its lifecycle — a non-Good quality on `level-primary`
annunciating the failover's managed alarm, a receipted `ack` write
through the active's `POST /command` pairing the annunciation to its
attributed acknowledgment, and the cleared instrument returning it —
then runs `dcs-alarm-report <addr>` against the field owner's
monitor: the report must cover the emitted model's whole alarm set
per instance — signal names, bound alarm points, declared
`priority`/`response_ticks` — measure the driven lifecycle's one
activation, annunciation, and acknowledgment, carry the response
pair attributed to the leg's actor within the declared bound, and
hold its cross-section accounting (the rates section equal to the
per-instance detail, the priority distribution covering every
annunciation, the source stretch agreeing with the served journal).
`dcs-alarm-report <addr> --journal-file <path>` over the field
owner's manifest-declared durable journal file must answer the
identical metric set — the file and the bounded served view being
one record for this run — with only its run-boundary accounting
added. The tool's refusal modes must exit nonzero naming the
failure: an unreachable monitor and an unreadable journal file,
never a silent pass. A violated contract fails `report-failed`; two
passes must produce the identical `report-digest`, a divergence
failing `report-nondeterministic`. The leg's doctored cases — an
expectation asserting the driven alarm left no activation, a dead
monitor address, and a missing journal path — must each report the
named diagnostic rather than pass silently.

The stage's command-switch leg — `ci/command_switch.py` on the same
declared deployment — then proves the consumer side of the
command-across-promotion contract on the customer's pair: a declared
command invoked through the released tooling while a switchover lands
keeps its receipt — never lost, never double-applied. With the pair
settled and tracking, the leg holds the exercise sequencer's `run`
input across its declared step length so one `step_completed` emits,
then submits the kind-declared `advance` through `dcs-ctl invoke` on
the field owner — the `accepted` submission must settle `applied`
with exactly one `command_settled` journal entry on the serving peer,
adopted identically on the tracking standby. The `demote`/`promote`
switch lands; on the promoted peer the leg reopens the table and
submits the same declared command in its default-one shape — exactly
one settlement there too, each submission's count attributed to the
peer that served it and the old peer's settlement replayed on neither
side. The promoted peer's emitted-event record must continue the
pre-promotion `step_completed` entries as its prefix — new entries at
greater ticks with unchanged component attribution and none
re-emitted. A further `advance` submitted immediately before the
restore switch — still `Accepted` when the boundary lands — rides the
promotion's final-sync checkpoint and must settle exactly once
`applied` on the restored active with no applied settlement on the
demoted peer. The leg restores the pair's launch roles, leaving the
manifest's duty controller `active` and its standby `tracking`. A
violated contract fails `command-switch-failed`; two passes must
produce the identical `command-switch-digest`, a divergence failing
`command-switch-nondeterministic`. The leg's doctored cases — a
submission settling zero times and one settling twice — must each
report the named diagnostic rather than pass silently.

The stage's demote-pending leg — `ci/demote_pending.py` on the same
declared deployment — then exercises the demote-boundary
pending-command settlement contract on the customer's pair: a command
an operator races against a demotion must meet the audited settle,
never a silent loss or a phantom application. With the pair settled
and tracking, the leg submits a receipted `write_value` on a declared
writable point through the field owner's `POST /command` and leaves
it pending, then issues the documented `demote` on the owner inside
that window and the `promote` on the converged standby — the
promotion's final-sync checkpoint carrying the still-`Accepted`
admission into the successor's log. The demoted peer's first
quiesced scan, driven before the promoted peer's first field-owning
scan, is the decisive observation: its fenced image must journal no
`command_settled` for the admission, must still hold the suspended
`accepted` receipt, and must still read the baseline — a quiesced
scan mints no `applied` the line never ordered and drops no pending
entry unaudited. The promoted peer then settles the admission
exactly once — `applied` through the carry, or `Rejected{superseded}`
with the settle journaled on the demoted peer alone — and the audit
asserts both peers' served journals and adopted receipt logs carry
the same single outcome, the served images agree, and each
manifest-declared durable journal file records the settle in `seq`
order. The leg restores the pair's launch roles, leaving the
manifest's duty controller `active` and its standby `tracking`. A
violated contract fails `demote-pending-failed`; two passes must
produce the identical `demote-pending-digest`, a divergence failing
`demote-pending-nondeterministic`. The leg's doctored cases — an
expectation asserting the phantom applied settle the fenced image
must never journal, and one asserting the pending entry vanished
from every receipt surface and journal — must each report the named
diagnostic rather than pass silently.

The stage's demote-reconvergence leg — `ci/demote_reconvergence.py`
on the same declared deployment — then pins the demote-follow
tracking-source contract on the customer's pair (WW-ENG-003,
WW-LCM-001): a demoted field owner must reconverge `tracking` on the
successor its own checkpoint pulls announced, and under the
manifest's declared `0.0.0.0` listen binds that announced source
must resolve to the pull connection's proven address — a dialable
peer address, never the wildcard bind recorded verbatim, never the
demoted peer's own monitor, never a foreign endpoint. Both
controllers bind the declared wildcard host, so the standby's
`?peer=` pulls announce the wildcard address the defect family once
recorded as-is. The leg converges the pair, then runs the
documented `demote`/`promote` switch in both directions: the
launched active — whose only tracking source is the recorded
announce — demotes onto the verified adoption its journal records
by name, reconverges `tracking`, and holds it across a driven pull
train; the reverse switch then demotes the promoted peer onto its
configured source and promotes the reconverged peer back. Every
served role report across the handovers is audited for a wildcard,
self-addressed, or foreign pull target; the served snapshots and
adopted receipt logs stay identical on both peers; each durable
journal file carries its own `role_changed` transitions in `seq`
order under the single cold-start boundary — a restart boundary
would be a process restart — and each declared state file holds the
run's final tick under the manifest's fingerprint. The leg restores
the pair's launch roles, leaving the manifest's duty controller
`active` and its standby `tracking`. A violated contract fails
`demote-reconvergence-failed`; two passes must produce the
identical `demote-reconvergence-digest`, a divergence failing
`demote-reconvergence-nondeterministic`. The leg's doctored case —
a crafted `?peer=` announce naming the field owner's own monitor
address, landing on the pulling connection's own source exactly as
a self-claim — plants a self-addressed demotion hint whose verify
pull reads the owner's own document: the replayable own-document
shape an announced demotion refuses, so `POST /demote` must answer
`409 no_tracking_source` rather than adopt the self-pin.

The stage's managed-lifecycle leg — `ci/managed_lifecycle.py` on the
same declared deployment — then exercises the emitted model's whole
managed-alarm surface end to end on the customer-owned pair
(WW-ENG-003, WW-ALM-001, WW-ALM-002), the lifecycle the platform's
own fixtures previously proved alone. With the pair settled and
tracking, a field-held fault — the injected non-Good on
`level-primary` — fails the measurement over so the `backup-active`
managed Bool alarm's `alarm`/`unacknowledged` annunciate on the
active's monitor with the journaled `point_changed` record; the
receipted `ack` write settles `applied` under the leg's declared
actor and clears the latch. The shelvable low-level alarm's writable
`shelve` point takes the receipted request: `shelved` must report
while the request stands and auto-release at the emitted model's
declared `max_shelve_ticks` — the durable journal's assertion and
expiry ticks measuring the bound exactly — while the
never-shelvable high-level alarm's bound-but-unwritable `shelve`
point answers the named `not_writable` refusal on the attributed
receipt, journaled as a `command_settled` rejection with no state
changed. The pump's `oos` point then drives the declared
designed-suppression wiring (decision 73): the fault alarm reports
`out_of_service`/`suppressed` while the alarms the model wires
without those inputs report neither; a driven run-contact fault
proves `alarm` still reports the process truth while suppression
withholds `unacknowledged`; and the return to service evaluates the
standing condition as a fresh `unacknowledged` transition the
receipted `ack` clears before the field fault clears. Every
commanded transition settles through the receipted path with actor
attribution; the pair's roles never move and every driven input is
restored; the field owner's durable journal file must carry the
lifecycle's receipts and transitions in `seq` order, the served
journal answering the same record. A violated contract fails
`managed-lifecycle-failed`; two passes must produce the identical
`managed-lifecycle-digest`, a divergence failing
`managed-lifecycle-nondeterministic`. The leg's doctored cases — an
expectation asserting the never-shelvable write settled applied,
and one asserting the `shelved` flag still stands after the
declared bound's own auto-release — must each report the named
diagnostic rather than pass silently.

The stage's emit-identical event-parity leg — `ci/event_parity.py` on
the same declared deployment — then proves the tracking standby's
published event record is not a hollow copy: the property that lets a
UI consumer fail its event feed over between the peers without a gap
in attribution. With the pair settled and tracking, the exercise
sequencer's `run` input is held through the field owner's receipted
path — the same write refused `not_active` at the standby's role
boundary — and driven scans run the declared step table out until the
counted `step_completed` set stands. `GET /resources` on both peers
must then serve the same routed `event_emitted` records — identical
outer and inner component attribution, declared event identities,
ordered payload fields, tick, and retention, the stream-local `seq`
positions excluded — while the standby still reports `tracking`. A
violated contract fails `event-parity-failed`; two passes must
produce the identical `event-parity-digest`, a divergence failing
`event-parity-nondeterministic`. The leg's doctored cases — a standby
record set missing an emission or carrying one re-attributed — must
each report the named diagnostic rather than pass silently.

The stage's monitor-starvation leg — `ci/monitor_starvation.py` on
the same declared deployment — then proves the serving-lane
resilience contract on the customer-owned pair (WW-ENG-003,
WW-FND-004), the stalled-client half of the consumer-boundary
schedules a lone driven run cannot reach: a controller whose monitor
starved its reads under an incomplete-body flood would read as a
dead active to its armed standby — the spurious-failover window the
bounded submission lane exists to close. With the pair settled and
the standby's `--auto-promote` carrying the manifest's declared
`failover_budget`, the leg opens the saturating set of
incomplete-body connections against the field owner's monitor —
requests whose declared bodies never follow, pinning the quarantined
body-reading handlers — and asserts through the hold that `GET
/role`, `GET /snapshot`, and `GET /checkpoint` keep answering inside
the declared per-request bound on both peers, that the standby's
per-scan checkpoint pulls keep landing — one more driven pull than
the armed budget, so a starved heartbeat would self-promote inside
the window — that a foreign attachment's field probe stays fenced,
and that neither peer's durable journal carries `role_changed` or
`field_claim_lost`. Closing the set must free the submission lane:
a driven `POST /scan` on the flooded owner answers again and
advances the tick, and a receipted kind-declared command settles
`applied` into both peers' adopted log with the pair's roles
unchanged. A violated contract fails `monitor-starvation-failed`;
two passes must produce the identical `monitor-starvation-digest`,
a divergence failing `monitor-starvation-nondeterministic`. The
leg's doctored cases — a run whose liveness reads starve under a
zeroed declared bound, and one whose standby reports a genuine role
change mid-hold — must each report the named diagnostic rather than
pass silently.

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
the kind-emitted
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
  `ci/standby_restart.py` exercises that promise on the deployed
  pair.

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
fingerprint, listen addresses, pair wiring, and the standby's
`failover_budget` against its `--auto-promote` flag — a declared
budget with no flag, a flag with no declaration, a diverging value,
or the field placed on the duty entry each fail `rig-mismatch`, an
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
re-recorded fingerprint is `manifest-fingerprint-mismatch`; a recorded
schema artifact the pinned tooling no longer emits byte-identically is
`schema-drift`; a served registry document failing the artifact's
required structure is `schema-mismatch`; a `dcs-model diff` leg whose
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
