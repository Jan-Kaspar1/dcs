# Release record: v0.9.0

The ninth release of the DCS platform, carrying the post-`v0.8.0`
correction of the consumer contract — the contract fixes whose
filed legs report `inconclusive` while the pinned release predates
them: the checkpoint path's cross-process single-writer refusal, the
promotion claim's basis bound, the keyed pair's `usurped` verdict and
the self-service reclaim it arms, and the sim-bus scripted `miss`
outcome's in-band `missed` answer — beside the declared-unit
discipline the typed composition seam gained while the `v0.8.0` tag
was still uncut, cut under the procedure in
`docs/release-contract.md` (decision 80). Fields marked *pending*
are filled mechanically by the release procedure when the supervisor
cuts the tag and publishes the images; the checked-in schemas are the
current emission, byte-pinned by the drift tests so they cannot
diverge from the code before the tag is cut.

| Field | Value |
|---|---|
| Tag | `v0.9.0` — *pending*: the release tag is placed on the recorded commit below when the release is cut (`git tag v0.9.0 a94a525`; the tag names this release's commit, not whatever `main` carries afterwards) |
| Commit | `a94a525e4a75a165d4043bd1a1aa81823c16dd6d` — the `main` commit that published this release and repinned the reference plant to it, the revision this record's schemas are emitted at and the revision `reference-plant/Cargo.lock` records for the `tag = "v0.9.0"` pin. Recorded before the cut so the shipped consumer artifact names an immutable revision: the reference plant's `lockfile` stage compares the committed lockfile against this field and reports `lockfile-stale` when the two diverge, which is what a cut on any other commit must be followed by (re-record the field and regenerate the lockfile) |
| Crate versions | `0.9.0` for every crate in the release set — one workspace version covers `dcs-build`, `dcs-core`, `dcs-model` (and the `dcs-model` / `dcs-controller` binaries built from it), `dcs-monitor` (shipping `dcs-ctl` and `dcs-alarm-report`), `dcs-plant` (`dcs-plant-server`), and `dcs-sim-net` (`dcs-plant-ctl`); the `[workspace.package]` bump lands with the cut |
| Plant-model JSON Schema | `plant-model.schema.json` beside this record — `dcs-model schema` emitted at the recorded commit, pinned byte-for-byte with its sha256 by the schema drift test in `crates/dcs-model/tests/schema.rs`. Byte-identical to `v0.8.0`'s recorded artifact — the emission last moved with #548's declared-unit metadata, which landed on `main` while the `v0.8.0` tag was still uncut and is therefore already carried by the `v0.8.0` record's artifact set |
| Plant-model schema sha256 | `91f175fa58b2f45bd44c1ce2a4c138ba9c583706f4bc4ccbcd5cc171540b2f7c` |
| Served-registry JSON Schema | `block-interfaces.schema.json` beside this record — `dcs-model interface-schema` emitted at the recorded commit, pinned byte-for-byte with its sha256 by the drift test in `crates/dcs-model/tests/interface_schema.rs`. Byte-identical to `v0.8.0`'s recorded artifact. Unchanged since `v0.3.0`'s recorded commit — decision 108's `usurped` sync state and `standby_usurped` pair-fault kind are payload values the registry's kinds do not enumerate |
| Served-registry schema sha256 | `ddc00496814a4e8cd0d6ec8a5d9fbb95e83f518dcd927b17a4802f13ac84013a` |
| Dynamics-document JSON Schema | `dynamics.schema.json` beside this record — `dcs-plant-server --dynamics-schema` emitted at the recorded commit, pinned byte-for-byte with its sha256 by the drift test in `crates/dcs-plant/tests/dynamics_schema.rs`. Byte-identical to `v0.8.0`'s recorded artifact. Unchanged since `v0.3.0`'s recorded commit |
| Dynamics-document schema sha256 | `98fb4a4298c5974b8ab0adf1374cd6d53b0c2cfbd2e24a31c874090235b46f02` |
| Deployment-manifest JSON Schema | `deploy-manifest.schema.json` beside this record — `dcs-model deploy-schema` emitted at the recorded commit, pinned byte-for-byte with its sha256 by the drift test in `crates/dcs-model/tests/deploy_schema.rs`. Byte-identical to `v0.8.0`'s recorded artifact — the emission last moved with #1282's additive-optional `history_file` per-controller declaration; #1341's single-writer rule binds at the launch and in the contract's declared-path prose, not in the emitted shape, so no schema move is owed |
| Deployment-manifest schema sha256 | `980430ca8725af997a7b5063f00d2a9663fe4619ca00a542917bde24f270cfa9` |
| `dcs-controller` image digest | *pending* — `dcs-controller@sha256:<digest>`, the image `docker build` produces from `Dockerfile` at the tag |
| `dcs-plant-server` image digest | *pending* — `dcs-plant-server@sha256:<digest>`, the image `docker build -f Dockerfile.plant` produces at the tag |

## Compatibility notes

`v0.9.0` is the minor bump the post-`v0.8.0` correction of the
consumer contract takes under the release versioning policy. The
determination this record owes: the tranche's changes are corrective
and additive — they hold `MODEL_VERSION` and the checkpoint format
set — and the bump exists so a `version = "0.8"` requirement cannot
silently resolve the corrected release line, not because any
supported item broke.

What the tranche changed for consumers — the contract corrections on
`main` since the `v0.8.0` record that the filed legs gate on:

- The checkpoint path's cross-process single-writer refusal (#1341):
  two live controllers on one mounted `--state-file` had no guard at
  all — each run persisted every scan through the same
  write-then-rename and a restart resumed whichever wrote last,
  silently, with the two runs' tick domains, receipts, force sets, and
  component state overwriting each other in between. Each of the three
  declared sinks now takes an exclusive advisory lock on a sibling
  `<path>.lock` sidecar, a file the checkpoint's rename never
  replaces, so the lock stays attached to the declared path across
  every save; the checkpoint's is held from before the resume read
  through the process's exit. A second live writer on one path exits
  nonzero naming the writer-lock conflict, the sidecar it could not
  take, and the consequence — two writers on one `--state-file`
  overwrite each other's run's tick domain, receipts, and component
  state, and a restart resumes whichever wrote last — while
  argument parse keeps the three declared paths and both sidecars
  pairwise distinct. The contract's declared-path prose states the
  rule a consumer relies on: each declared path is that controller's
  own, which is why the reference rig keeps a separate volume per
  controller. Nothing on the wire, in the checkpoint document, or in
  the emitted manifest schema moved.
- The promotion claim's basis bound (#1409, decision 107): the claim
  path had no tick-domain check, so a standby whose own run tick
  accrued several for every one the field owner's did converged
  `tracking` on the live incumbent, posted `POST /promote`, and took
  the field — the handover machinery correct on every step, decided on
  a pair of stamps no translation stood behind. `Peer::claim_basis`
  declares the basis (this run's own `run` tick, the tracked line's
  last served `source` stamp, the `lead` between them, the `baseline`
  the current alignment was established at, and the `growth` past it)
  and `check_claim_basis` refuses the promotion as
  `SwitchError::FieldClaimFailed` — `409 field_claim_failed` — once
  the growth passes `MAX_CLAIM_LEAD` (64 run ticks), naming the bound,
  both stamps, the lead, the baseline, and the two remedies an
  operator acts on: bring the claimant to the field owner's cadence
  with `--scan-ms`, or `release_writer` the standing claim and promote
  onto the free field. Two escapes keep it from stranding what it must
  not: this scan's own claim probe answered `unclaimed`, and the
  tracked line is not serving this run (`misses != 0`, which also
  leaves the armed failover arm untouched — it never reads a basis).
  The field's unconditional `claim_writer`, the conditional startup
  grant, the orphaned promotion's grant, and the convergence-gated
  fencing-loss reclaim are all unchanged. `Peer::claim_basis` and
  `ClaimBasis` are public, so a consumer can read the basis it would
  claim on without parsing a refusal.
- The keyed pair's usurped verdict and its self-service reclaim
  (#1410, decision 108): `--pair-token` authenticated the pulls and
  the announced-source adoption but never the field's own claim
  arbitration, so an unkeyed third attachment could converge on a
  keyed pair entirely through public unkey-gated pulls, promote, and
  take the field outright — and the pair's own recovery then met the
  conditional orphan grant's live-incumbent refusal, leaving the
  remedy on the usurper's own surface. `StandbySync` gains
  `Usurped { aligned }`, served as `usurped` beside `field_claim:
  held`: the field's write-ownership claim does stand under a live
  writer, but the monitor endpoint that writer declared could not prove
  this line's key, so the tracked line's ownerlessness is a foreign
  claim rather than an ownerless one. The diagnosis is the pair key's
  one job nothing else can do — `Monitor::diagnose_foreign_writer`
  reads the standing writer's declared endpoint from the field's own
  arbitration and pulls it under a fresh `?prove=` nonce, calling it
  foreign only on the one answer that is evidence (the endpoint
  answered and its document carried no valid `line_proof`; silence is
  not a verdict) — and `Peer::claim_gate` routes `Usurped` to the
  unconditional `Claim` instead of the conditional `OrphanClaim`, for
  the requested `promote` and the armed `self_promote` alike, queueing
  a `ForeignClaimPreempt` naming the endpoint the claim was taken from
  and journaling it as `ForeignClaimPreempted`. The served wire grows
  one sync variant and one journal event, both additive under the
  serde-optional conventions the sibling records established — a
  payload an older peer serves decodes unchanged — and
  `PAIR_FAULT_KINDS_VERSION` moves 3 → 4 for the added
  `standby_usurped` kind under decision 19's versioned-extension rule,
  the existing eight kinds keeping their indices. `Tracking`,
  `Reinitialized`, and plain `Orphaned` promotions route exactly as
  before, and the absences stay honest where the evidence is missing:
  an unkeyed run holds no key to ask under, so it stays `Orphaned`
  with the conditional grant, and a claim declaring no monitor names
  no writer — the reference plant's unkeyed pair is unaffected by
  design. The pair key stays on the invocation
  (`docs/conduit-boundaries.md`): the peer stores the fact, never the
  secret, and re-earns it per verification window.
- The sim-bus scripted `miss` outcome's in-band answer (#1413,
  #1415): a scripted `miss` consumed by an exchange dropped the
  consuming connection unanswered, and teardown released that
  attachment's claim hold — so any unfenced attachment could free
  another attachment's field ownership by queuing one scripted
  outcome. The outcome now answers `missed` in-band
  (`BusResponse::Missed`): the exchange completes nothing while the
  link and the claim bound to it stay up, keeping the script queue
  outside the arbitration vocabulary — a scripted flaky device costs
  its consuming attachment the cycle, and the claim still releases on
  holder disconnect or `release_writer` alone. The response tag is
  new on the wire; every earlier response spelling decodes unchanged.
- The declared-unit discipline the composition seam gained (#548,
  decision 106, adopted across the checked-in compositions by #983):
  `IoPoint`, `Port`, and `ComponentInstance` carry optional unit
  declarations — `unit` on the point and port, `parameter_units`
  beside the parameter map — and `PlantBuilder::unit`,
  `port_unit`, and `param_unit` declare them, with
  `dcs_build::unit` naming the published vocabulary. Every field is
  serde-optional and omitted when absent, so existing documents load
  unchanged; a consumer composition that adopts units gets checked
  declarations at every seam the document crosses, and one that does
  not emits byte-identical bytes across this repin.
  Enforcement is by name where the declarations resolve:
  `ConnectionUnitMismatch` for a disagreement between two declared
  connection ends on every built or loaded document, `SignalUnitMismatch`
  for a signal's unit against its point's — `build` fills an
  undeclared `Signal::unit` from the point, so the display string
  inherits the checked declaration instead of drifting beside it — and
  `UnknownParameter` for a `parameter_units` key the instance's map
  does not carry. An undeclared end stays admissible as an
  uncheckable wire, never a failure. This is the tranche's
  model-grammar move, and it is already carried by the `v0.8.0`
  record's artifact set because it landed while that tag was pending;
  #983's adoption across the platform-owned compositions and the
  independent `reference-plant` composition changed only the additive
  fields this decision names, with the reference plant's
  `model.fingerprint` re-pinned and its `upgrade` stage's recorded
  baseline advanced to the earliest rev whose builder API carries the
  declarations.

The filed legs:

- The filed legs for the contracts this tranche carries — the
  cross-peer `--state-file` single-writer legs (rig #1347 beside
  consumer mirror #1348, on #1341's fix), the claim-basis skew-bound
  legs (#1345/#1346, on #1409's fix), and the ahead-bound rejoin legs
  (rig #1275 beside the consumer-boundary mirror, on #1336's fix the
  `v0.8.0` record already carries) — each report `inconclusive`, never
  its failure, while the pinned release predates the contract it
  exercises; this release is the pin that lets them report.
- Two of this tranche's contracts have no consumer-boundary mirror by
  construction: the usurped-verdict reclaim's filed rig leg (#1417)
  exercises a `--pair-token` pair, which the reference deployment
  deliberately leaves unkeyed, and the scripted-miss claim-hold's rig
  leg (#1418) exercises a `sim-bus` backend, which the consumer
  deployment mounts no instance of. Their rig legs carry the contract;
  a consumer mirror would have to mount the very configuration the
  reference deployment declares it does not run.
- The filed legs for contracts already on `main` gate on this pin the
  same way — the announced-source verification cluster (#867, the
  mirrors #1222/#1223 and #924/#1111) and the graceful-shutdown
  contract (#820, the mirrors #975/#984) — each reporting
  `inconclusive` until a release line carries the contract it
  exercises.
- The filed legs whose contracts are still in flight each report
  `inconclusive` until their contract lands and a release carries it:
  the attributed-switch control-lane isolation mirror (#1266, on
  #1264's fix with PR #1396 in flight), the stale_after_ticks
  cadence-domain contract (#1411), the quality-aware cause-alarm
  contract (#827, mirror #1134), and whichever defect-fix contracts
  merge before the cut.

The determination:

- `MODEL_VERSION` holds at `1`; `PlantModel::load` still accepts
  exactly that version. A `version: 1` document written against
  `v0.8.0` validates unchanged under `v0.9.0` tooling; the
  plant-model schema emission is byte-identical to `v0.8.0`'s
  recorded artifact, and the declared-unit grammar it carries is
  additive optional, so a `v0.8.0` document still validates
  unchanged.
- The checkpoint format set holds: `Checkpoint.format_version` still
  negotiates against `SUPPORTED_FORMAT_VERSIONS` (`{0, 1}`; absent
  reads as `0`). Checkpoints cross the bump under the same
  per-connection negotiation and fingerprint gate — no checkpoint
  migration is owed. The checkpoint's single-writer lock is a
  filesystem-side guard around the same document, not a change to it.
- The tranche corrected the persistence paths' admission, the claim
  path's clock basis, the pair's foreign-claim diagnosis, and the
  scripted-outcome tooling's claim blast radius, and added the
  declared-unit metadata to the typed seam — additive and corrective,
  without removing or re-shaping any supported engineering surface, so
  a consumer's composition code compiles unchanged on the repin. The
  migration expectation is the repin itself — the reference plant's
  `ci/check.sh` `upgrade` stage proves the `v0.8.0` → `v0.9.0`
  crossing byte-identically; non-Rust consumers pin the same four
  schema artifacts `v0.8.0` recorded, all four emissions
  byte-identical. A consumer that adopts declared units gets the
  named document-level checks; one that does not is unaffected, and
  an adoption is not required by this release.
- The served wire's additive growth stays readable by an older
  consumer: a payload an older peer serves decodes unchanged, an older
  page renders the new sync state as `orphaned`-shaped detail rather
  than crashing, and the pair-fault vocabulary's version field is what
  tells a consumer which kind list it is reading.
- The `dcs-build` `station`, `dosing`, `ijmuiden`, and `ethercat`
  modules remain platform-owned reference compositions outside the
  compatibility policy (decision 81); a consumer composes from the
  supported primitives or copies a pattern. #983's unit adoption
  across them is an example-composition change, not a supported-surface
  change.

## Consumer pins

- Crates: `dcs-build = { git = "<repo>", tag = "v0.9.0" }` — or
  `rev = "<commit>"` for the identical immutable commit, the recorded
  commit above; `dcs-core` and `dcs-model` under the same pin. The
  reference plant's committed `Cargo.lock` records the same pin
  resolved — `?tag=v0.9.0#a94a525e4a75a165d4043bd1a1aa81823c16dd6d`,
  the commit this record names — so a fresh clone resolves under
  `cargo fetch --locked`.
- Tooling: `cargo install --git <repo> --tag v0.9.0 dcs-model
  dcs-controller dcs-plant dcs-monitor dcs-sim-net` — `dcs-monitor`
  ships `dcs-ctl` and `dcs-alarm-report`, `dcs-sim-net` ships
  `dcs-plant-ctl` — or binaries built from the tag.
- Images: *pending* — `dcs-controller@sha256:<digest>` and
  `dcs-plant-server@sha256:<digest>` once the record's digest fields
  are filled, or `docker build` / `docker build -f Dockerfile.plant`
  at the tag.

## Post-cut checklist

Landed with the commit carrying this record — the publication's
non-registry half:

- The record itself and the four emitted schema artifacts, each
  byte-pinned with its published sha256 by the drift tests, so the
  schema non-drift legs continue to cover all four recorded
  artifacts — `deploy-schema` and `--dynamics-schema` beside
  `schema`/`interface-schema`.
- `[workspace.package]` version `0.9.0` and the regenerated workspace
  `Cargo.lock` — one crate version across the release set, the Crate
  versions field above.
- The reference plant's repin: `Cargo.toml`'s `tag = "v0.9.0"`,
  `ci/check.sh`'s `DCS_REV` default `v0.9.0`, `deploy/manifest.json`'s
  `dcs_release: "v0.9.0"` and `v0.9.0` image tags, and
  `deploy/compose.yaml`'s `x-dcs-release` and images. Its
  `DCS_UPGRADE_REV` default stays at `07ec24f` — #983's baseline, the
  earliest `v0.8.0`-line rev whose builder API carries this
  composition's declared dimensional metadata, so the `upgrade` stage
  materializes the tree there, repins it to `v0.9.0`, and proves the
  named crossing from a baseline this tree's own source still compiles
  against.
- The reference plant's regenerated `Cargo.lock`: the release crates
  recorded at `version = "0.9.0"` from
  `git+<repo>?tag=v0.9.0#a94a525e4a75a165d4043bd1a1aa81823c16dd6d`,
  the Commit field above, so the shipped consumer artifact satisfies
  the shipped manifest and `cargo fetch --locked` resolves it without
  re-resolving. The record's Commit field is filled in the same step
  for the reason the check's `lockfile` stage requires it: a lockfile
  cannot name a tag's target before the tag exists, so the commit is
  recorded first and the tag cut onto it.

The remaining items are the release procedure's mechanical fill the
tag's own coordinates decide, and the supervisor's publication
operations — the fields this record still marks *pending*:

- Cut `v0.9.0` on the Commit field's recorded sha (`git tag v0.9.0
  a94a525e4a75a165d4043bd1a1aa81823c16dd6d`) once `rust-proofs` is
  green on that exact commit, and clear the Tag field's *pending*
  marker. The tag lands on the recorded commit, not on whatever `main`
  carries when the cut runs: a tag on any other commit makes the
  reference plant's committed lockfile stale against it, which the
  consumer's own `lockfile` stage reports as `lockfile-stale`. Re-point
  the Commit field and regenerate `reference-plant/Cargo.lock`
  (`cargo update` in the consumer tree, README §7's documented step)
  before re-running the check if the cut lands elsewhere.
- Build and publish the `dcs-controller` and `dcs-plant-server`
  images; fill the two digest fields above and the Images entry under
  Consumer pins.
- Re-run `reference-plant/ci/check.sh` end to end against the
  published artifacts and capture its output as the release's
  consumer evidence; the legs this record's pin unblocks — the
  cross-peer `--state-file` refusal, the claim-basis skew bound, and
  the ahead-bound rejoin — must report verdicts rather than
  `inconclusive`, and the upgrade stage's doctored-version refusals
  must still report their named diagnostics.
