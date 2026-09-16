# Release record: v0.1.0

The first release of the DCS platform, cut under the procedure in
`docs/release-contract.md` (decision 80). Fields marked *pending* are
filled mechanically by the release procedure when the supervisor cuts
the tag and publishes the images.

| Field | Value |
|---|---|
| Tag | `v0.1.0` — the release tag on the recorded commit |
| Commit | `a2b1b13e7b4273b133bc0fff55cb97e16c5d3197` — the `main` commit landing the release contract (issue #348), the revision this record's schema was emitted at |
| Crate versions | `0.1.0` for every crate in the release set — one workspace version covers `dcs-build`, `dcs-core`, `dcs-model` (and the `dcs-model` / `dcs-controller` binaries built from it) |
| Plant-model JSON Schema | `plant-model.schema.json` beside this record — `dcs-model schema` emitted at the recorded commit, pinned byte-for-byte by the schema drift test in `crates/dcs-model/tests/schema.rs` |
| Schema sha256 | `06bee597e568423481244bd30e255fc05c850448077e7c10d41bdffbe782b74d` |
| `dcs-controller` image digest | `dcs-controller@sha256:7f9d8b83567bd218cf66cf14f01e862290feaa36f71f235c28df20457bd4522e` — the image `docker build` produces from `Dockerfile` at the tag |
| `dcs-plant-server` image digest | `dcs-plant-server@sha256:95e5dd2d5921212787c2ceb2147a5c9c9d8546c25b427925937e90d48dae3413` — the image `docker build -f Dockerfile.plant` produces at the tag |

## Compatibility notes

`v0.1.0` is the first release: there is no earlier release to break, so
this record establishes the baseline the compatibility policy measures
from.

- `MODEL_VERSION` is `1`; `PlantModel::load` accepts exactly that
  version. Checkpoint `format_version` negotiates against
  `SUPPORTED_FORMAT_VERSIONS` (`{0, 1}`; absent reads as `0`).
- Per the release versioning policy: `0.1.x` patch releases are
  drop-in repins — the supported surface keeps compiling,
  `MODEL_VERSION` and the checkpoint format set hold. `0.2.0` may
  break the supported API, `MODEL_VERSION`, or the checkpoint format;
  its record will name the breakage and the migration expectation.
- The `dcs-build` `station`, `dosing`, `ijmuiden`, and `ethercat`
  modules are platform-owned reference compositions outside the
  compatibility policy (decision 81); a consumer composes from the
  supported primitives or copies a pattern.

## Consumer pins

- Crates: `dcs-build = { git = "<repo>", tag = "v0.1.0" }` — or
  `rev = "a2b1b13e7b4273b133bc0fff55cb97e16c5d3197"` for the identical
  immutable commit; `dcs-core` and `dcs-model` under the same pin.
- Tooling: `cargo install --git <repo> --tag v0.1.0 dcs-model` (and
  `dcs-controller`), or binaries built from the tag.
- Images: `dcs-controller@sha256:7f9d8b83567bd218cf66cf14f01e862290feaa36f71f235c28df20457bd4522e`,
  `dcs-plant-server@sha256:95e5dd2d5921212787c2ceb2147a5c9c9d8546c25b427925937e90d48dae3413`
  — the digests above — or `docker build` /
  `docker build -f Dockerfile.plant` at the tag.
