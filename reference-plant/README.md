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
takeover and out-of-service declarations, and the managed alarm set —
a never-shelvable high-level alarm, a shelvable low-level alarm, and
the equipment alarms the site policy declares.

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
                       --surface asserts the served operator surface
deploy/manifest.json   the deployment declaration
```

## The customer path

### 1. Create your repository

Copy this tree into a fresh repository. Nothing in it references the
platform checkout — the only platform coupling is the pinned release in
`Cargo.toml`.

### 2. Pin a release

`Cargo.toml` pins the release crates by immutable revision:

```toml
dcs-build = { git = "https://github.com/Jan-Kaspar1/dcs.git", rev = "a2b1b13…" }
dcs-model = { git = "https://github.com/Jan-Kaspar1/dcs.git", rev = "a2b1b13…" }
```

`tag = "v0.1.0"` names the identical commit once the release tag
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
cargo install --git https://github.com/Jan-Kaspar1/dcs.git --rev a2b1b13… \
    dcs-model dcs-controller dcs-plant
```

### 5. Run the simulation

`ci/check.sh` drives `ci/simulate.py`: `dcs-plant-server` serves the
checked-in model plus `model/dynamics.json`, `dcs-controller --driven`
advances declared scans, and the script asserts the scenario's
observable outcomes — duty and lag staging, the high-level alarm's
`not_writable` shelve refusal, ack latching, instrument failover and
recovery, manual takeover, out-of-service suppression, and the
pumped-down all-stop — identically on every run.

The check's `surface` stage then drives the same deterministic
`--driven` run — `ci/simulate.py --surface` — asserting the served
operator surface against the emitted model's declaration: `GET
/signals` must serve exactly the declared signal index, so every
writable command point the composition declares (the alarm
`ack`/`shelve`/`oos` points, the per-pump `mode`/`hand`/`oos` takeover
points) appears `writable: true` while the never-shelvable high-level
alarm's `shelve` point stays read-only, and every named signal carries
its declared group; `GET /` must serve the monitoring page; the
snapshot's `descriptors` must cover every composed component; and
`GET /journal` must answer the run's recorded transitions. A
divergence fails `surface-mismatch`.

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

The deployment maps directly onto the platform's documented run
commands: `dcs-plant-server <model> --dynamics <doc> --listen <addr>`
and `dcs-controller <model> --remote <addr> [--standby <peer>]
--listen <addr>`.

### 7. Upgrade by repinning

A compatible upgrade is a repin: change the `rev`/`tag` in
`Cargo.toml`, run `cargo update` to move the lockfile, and re-run
`ci/check.sh`. Within a release's minor series the supported API and
`MODEL_VERSION` are unchanged — the check passing is the upgrade's
acceptance.

An **incompatible** crossing fails with named diagnostics, never
silently: a pin that resolves no release crates is `pin-unresolvable`;
a pin whose supported API no longer compiles your composition is
`surface-incompatible`; a model document the release's tooling refuses
is `tooling-rejected`; a model whose semantic content changed under a
re-recorded fingerprint is `manifest-fingerprint-mismatch`; a served
operator surface diverging from the emitted model's declaration is
`surface-mismatch`. The names are recorded in the platform's
`docs/release-contract.md` — the same vocabulary the platform's own
consumer-boundary checks report.
