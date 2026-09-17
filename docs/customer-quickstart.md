# Customer quick start

The entry point for engineering a customer plant is the
**`reference-plant/` tree** in this repository — a complete,
verbatim-publishable consumer repository pinning the v0.2.0 release
contract (`docs/release-contract.md`). Copy it into a new repository and
it becomes your plant project: no platform checkout, no path
dependencies, only the pinned release crates.

Its `README.md` walks the full customer path:

1. **Create the repository** from the tree — every file it needs is
   inside it.
2. **Pin a release** — `Cargo.toml`'s `git`/`rev` dependency on the
   release crates, locked by the committed `Cargo.lock`.
3. **Compose and emit** — edit `src/station.rs` against the supported
   `dcs-build` primitives; `cargo run` emits the model deterministically.
4. **Validate** — `dcs-model validate`/`lint` and
   `dcs-controller --check` over the emitted model through the released
   tooling.
5. **Simulate** — `ci/check.sh` runs `ci/simulate.py`: the scripted
   deterministic scenario over `dcs-plant-server` plus the declared
   dynamics and a `dcs-controller --driven`, asserting the station's
   observable behavior leg by leg.
6. **Deploy** — `deploy/manifest.json` binds the approved model and its
   fingerprint to the release's images and the redundant controller
   pair, and `deploy/compose.yaml` instantiates the manifest as a
   checked-in rig definition the check holds in lockstep. The
   manifest's optional per-controller `state_file`/`journal_file`
   fields name container paths on writable volumes that the rig
   definition mounts and carries to the invocation's
   `--state-file`/`--journal-file` flags: `state_file` lets a
   restarted container resume in place at its persisted checkpoint,
   `journal_file` keeps the attributed operator-action record durable
   past the process lifetime. These fields are also what make the
   recorded dead-active recovery a warm resume rather than a cold
   start — when an active dies while its standby cannot promote, the
   deliberate path is restarting a fresh active, whose unconditional
   startup claim preempts the dead owner's field claim (decision 86,
   `docs/architecture.md`). A consumer without durable storage
   omits both fields and the flags stay absent.
7. **Upgrade** by repinning to a compatible release; an incompatible
   crossing surfaces as a named diagnostic (`pin-unresolvable`,
   `surface-incompatible`, `tooling-rejected`,
   `manifest-fingerprint-mismatch`, …) rather than silent misbehavior.

The check's `pair` stage proves the declared redundant pair runs — not
just that its definition parses: `ci/pair.py` reads the standby wiring
and persistence fields out of `deploy/manifest.json` and spawns the two
declared controllers on released tooling, converging the standby to
`tracking`, issuing the receipted `demote`/`promote` switchover, and
asserting the run continues bumplessly with the adopted receipts and
the durable journal files' transition records intact.

The served operator surface a monitoring or UI consumer can rely on —
proved by the check's `surface` stage against the emitted model — is:

- **`GET /schema`** — the block-interface registry: one versioned
  interface per declared component covering its ports as
  measurement/state resources, its parameters as configuration, and
  its command/event vocabulary.
- **Declared commands** — a kind's declared commands submit through
  `POST /command`'s `invoke` variant and answer a structured receipt
  that settles through the journaled `command_settled` record.
- **Emitted events** — a kind's declared events reach the
  consumer-visible record: `GET /journal`'s `event_emitted` entries
  and the per-instance `events` of `GET /resources`.
- **`GET /signals`, `GET /journal`, and the snapshot descriptors** —
  the signal index, the run's recorded transitions, and the composed
  `<kind>:<id>` inventory.

The boundary is proven from the workspace by
`crates/dcs-build/tests/reference_plant.rs`, which materializes the
tree outside the workspace and runs its own clean-CI check green.
