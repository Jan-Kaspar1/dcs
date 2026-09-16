# Customer quick start

The entry point for engineering a customer plant is the
**`reference-plant/` tree** in this repository — a complete,
verbatim-publishable consumer repository pinning the v0.1.0 release
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
   checked-in rig definition the check holds in lockstep.
7. **Upgrade** by repinning to a compatible release; an incompatible
   crossing surfaces as a named diagnostic (`pin-unresolvable`,
   `surface-incompatible`, `tooling-rejected`,
   `manifest-fingerprint-mismatch`, …) rather than silent misbehavior.

The boundary is proven from the workspace by
`crates/dcs-build/tests/reference_plant.rs`, which materializes the
tree outside the workspace and runs its own clean-CI check green.
