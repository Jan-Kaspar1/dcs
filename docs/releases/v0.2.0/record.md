# Release record: v0.2.0

The second release of the DCS platform, carrying the M12
schema-driven-blocks and disposable-UI tranche over `v0.1.0`, cut
under the procedure in `docs/release-contract.md` (decision 80).
Fields marked *pending* are filled mechanically by the release
procedure when the supervisor cuts the tag and publishes the images;
the checked-in schemas are the current emission, byte-pinned by the
drift tests so they cannot diverge from the code before the tag is
cut.

| Field | Value |
|---|---|
| Tag | `v0.2.0` — the release tag on the recorded commit |
| Commit | `c2b5694d9fd6f6168b85c1dfc2e1542b369b3a3f` — the tagged `main` commit closing the M12 tranche, the revision this record's schemas are emitted at |
| Crate versions | `0.2.0` for every crate in the release set — one workspace version covers `dcs-build`, `dcs-core`, `dcs-model` (and the `dcs-model` / `dcs-controller` binaries built from it); the `[workspace.package]` bump lands with the cut |
| Plant-model JSON Schema | `plant-model.schema.json` beside this record — `dcs-model schema` emitted at the recorded commit, pinned byte-for-byte with its sha256 by the schema drift test in `crates/dcs-model/tests/schema.rs` |
| Plant-model schema sha256 | `68f77f99081a8e7bdc5e63b180c643b0b2e33b9459a8275ccd0da84362f26fe4` |
| Served-registry JSON Schema | `block-interfaces.schema.json` beside this record — `dcs-model interface-schema` emitted at the recorded commit, pinned byte-for-byte with its sha256 by the drift test in `crates/dcs-model/tests/interface_schema.rs`. The first record carrying it: `v0.1.0`'s recorded commit predates the served block-interface registry (#375) |
| Served-registry schema sha256 | `ddc00496814a4e8cd0d6ec8a5d9fbb95e83f518dcd927b17a4802f13ac84013a` |
| `dcs-controller` image digest | `dcs-controller@sha256:1831b590945ded4c1e0cecdc2505aab78a5e2217c608e23ba5f15ef7f5b89831` — the image `docker build` produces from `Dockerfile` at the tag |
| `dcs-plant-server` image digest | `dcs-plant-server@sha256:c6a8e29b4e4909ae584684a7dc33b1b89f850bdd28d6eeb08585cc2cea3c316c` — the image `docker build -f Dockerfile.plant` produces at the tag |

## Compatibility notes

`v0.2.0` is the minor bump the M12 tranche's growth of the consumer
contract takes under the release versioning policy. The determination
this record owes: the tranche's additions are additive — they hold
`MODEL_VERSION` and the checkpoint format set — and the bump exists so
a `version = "0.1"` requirement cannot silently resolve the enlarged
release line, not because any supported item broke.

What the tranche changed for consumers:

- The served block-interface registry joined the release contract
  (decision 82): `dcs-monitor`'s `GET /schema` answers the `SchemaView`
  of every component's `BlockInterface` — the five resource
  collections (measurements, configuration, runtime state, commands,
  events) at `INTERFACE_VERSION` 1 — and `GET /resources` answers the
  same publication's live resource state (#375). `dcs-core`'s
  `SchemaView`, `BlockInterface`, `INTERFACE_VERSION`, and
  `SchemaView::json_schema` joined the supported engineering surface,
  and `dcs-model interface-schema` emits the recorded
  `block-interfaces.schema.json` for non-Rust consumers (#394).
- Blocks declare named commands and typed emitted events on the
  interface (#374): `CommandDecl`s carry a typed request and
  availability; `EventDecl`s carry a typed payload and declared
  retention. Declared commands dispatch at the scan boundary through
  the existing bounded, receipted path (#377) and journal-retained
  emissions land as `event_emitted` entries; decision 84 pins the
  emit-identical standby semantics, the exactly-once carried
  invocation at promotion, and `Peer::final_sync` (#381).
- Monitoring reads moved onto immutable post-scan publications in
  bounded storage outside the executor lock, and command ingress is
  bounded (decision 83; #364, #365): a full queue answers the named
  `queue_full` rejection receipt, read endpoints serve published
  copies with sequence/freshness metadata or a named gap, and a slow,
  disconnected, or restarted consumer cannot pace or stop the scan —
  proven by `dcs-monitor/tests/noninterference.rs` (#376) and the
  reference plant's `consumers` clean-CI stage (#378).
- The monitoring page renders all five resource categories for any
  declared kind with no kind-specific markup required (#379), and
  `dcs-ctl` gained `invoke` plus read access to the served schema and
  recent emitted events (#422, #390).
- The model document gained the `sim-cyclic` device kind — a
  simulated cyclic-I/O device exercising the decision-78 exchange
  contract (#406) — the only plant-model schema change since
  `v0.1.0`, additive under decision 3's optional-field convention.

The determination:

- `MODEL_VERSION` holds at `1`; `PlantModel::load` still accepts
  exactly that version. A `version: 1` document written against
  `v0.1.0` validates unchanged under `v0.2.0` tooling.
- The checkpoint format set holds: `Checkpoint.format_version` still
  negotiates against `SUPPORTED_FORMAT_VERSIONS` (`{0, 1}`; absent
  reads as `0`). Checkpoints cross the bump under the same
  per-connection negotiation and fingerprint gate — no checkpoint
  migration is owed.
- The supported engineering surface grew only by addition — the
  served-registry types and the `interface-schema` subcommand — so a
  consumer's composition code compiles unchanged on the repin. The
  migration expectation is the repin itself plus, for non-Rust
  consumers of the served contract, pinning the second schema
  artifact below.
- The `dcs-build` `station`, `dosing`, `ijmuiden`, and `ethercat`
  modules remain platform-owned reference compositions outside the
  compatibility policy (decision 81); a consumer composes from the
  supported primitives or copies a pattern.

## Consumer pins

- Crates: `dcs-build = { git = "<repo>", tag = "v0.2.0" }` — or
  `rev = "c2b5694d9fd6f6168b85c1dfc2e1542b369b3a3f"` for the identical
  immutable commit; `dcs-core` and `dcs-model` under the same pin.
- Tooling: `cargo install --git <repo> --tag v0.2.0 dcs-model` (and
  `dcs-controller`), or binaries built from the tag.
- Images: `dcs-controller@sha256:1831b590945ded4c1e0cecdc2505aab78a5e2217c608e23ba5f15ef7f5b89831`
  and
  `dcs-plant-server@sha256:c6a8e29b4e4909ae584684a7dc33b1b89f850bdd28d6eeb08585cc2cea3c316c`,
  or `docker build` / `docker build -f Dockerfile.plant` at the tag.
