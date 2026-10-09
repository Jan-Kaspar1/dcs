# Release record: v0.4.0

The fourth release of the DCS platform, carrying the post-`v0.3.0`
growth of the consumer contract — the redundant-pair claim, audit,
and receipt-convergence contracts, the durable-sink scan isolation,
and the served-health and deployment-topology additions the
consumer-boundary mirror legs gate on — cut under the procedure in
`docs/release-contract.md` (decision 80). Fields marked *pending* are
filled mechanically by the release procedure when the supervisor cuts
the tag and publishes the images; the checked-in schemas are the
current emission, byte-pinned by the drift tests so they cannot
diverge from the code before the tag is cut.

| Field | Value |
|---|---|
| Tag | `v0.4.0` — *pending*: the release tag is placed on the recorded commit when the release is cut |
| Commit | *pending* — the tagged `main` commit carrying this record, the revision this record's schemas are emitted at |
| Crate versions | `0.4.0` for every crate in the release set — one workspace version covers `dcs-build`, `dcs-core`, `dcs-model` (and the `dcs-model` / `dcs-controller` binaries built from it), `dcs-monitor` (shipping `dcs-ctl` and `dcs-alarm-report`), `dcs-plant` (`dcs-plant-server`), and `dcs-sim-net` (`dcs-plant-ctl`); the `[workspace.package]` bump lands with the cut |
| Plant-model JSON Schema | `plant-model.schema.json` beside this record — `dcs-model schema` emitted at the recorded commit, pinned byte-for-byte with its sha256 by the schema drift test in `crates/dcs-model/tests/schema.rs`. The emission moved twice since `v0.3.0`'s recorded commit: #907's `record` io_point field — the durable-history decision's declared recording duty — added its `recording-duty` definition, and #548's declared-unit metadata added optional `unit` declarations on io points, ports, and signals plus `parameter_units` beside `parameters` — additive optional fields a `v0.3.0` document never carries |
| Plant-model schema sha256 | `91f175fa58b2f45bd44c1ce2a4c138ba9c583706f4bc4ccbcd5cc171540b2f7c` |
| Served-registry JSON Schema | `block-interfaces.schema.json` beside this record — `dcs-model interface-schema` emitted at the recorded commit, pinned byte-for-byte with its sha256 by the drift test in `crates/dcs-model/tests/interface_schema.rs`. Unchanged since `v0.3.0`'s recorded commit |
| Served-registry schema sha256 | `ddc00496814a4e8cd0d6ec8a5d9fbb95e83f518dcd927b17a4802f13ac84013a` |
| Dynamics-document JSON Schema | `dynamics.schema.json` beside this record — `dcs-plant-server --dynamics-schema` emitted at the recorded commit, pinned byte-for-byte with its sha256 by the drift test in `crates/dcs-plant/tests/dynamics_schema.rs`. Unchanged since `v0.3.0`'s recorded commit |
| Dynamics-document schema sha256 | `98fb4a4298c5974b8ab0adf1374cd6d53b0c2cfbd2e24a31c874090235b46f02` |
| Deployment-manifest JSON Schema | `deploy-manifest.schema.json` beside this record — `dcs-model deploy-schema` emitted at the recorded commit, pinned byte-for-byte with its sha256 by the drift test in `crates/dcs-model/tests/deploy_schema.rs`. Moved once since `v0.3.0`'s recorded commit: #1282's additive-optional `history_file` per-controller declaration — decision 102's durable process-history mount; a manifest predating the field validates unchanged |
| Deployment-manifest schema sha256 | `980430ca8725af997a7b5063f00d2a9663fe4619ca00a542917bde24f270cfa9` |
| `dcs-controller` image digest | *pending* — `dcs-controller@sha256:<digest>`, the image `docker build` produces from `Dockerfile` at the tag |
| `dcs-plant-server` image digest | *pending* — `dcs-plant-server@sha256:<digest>`, the image `docker build -f Dockerfile.plant` produces at the tag |

## Compatibility notes

`v0.4.0` is the minor bump the post-`v0.3.0` growth of the consumer
contract takes under the release versioning policy. The determination
this record owes: the tranche's additions are additive — they hold
`MODEL_VERSION` and the checkpoint format set — and the bump exists so
a `version = "0.3"` requirement cannot silently resolve the enlarged
release line, not because any supported item broke.

What the tranche changed for consumers — the contract additions since
`v0.3.0` the consumer-boundary mirror legs gate on:

- The receipted command path carries a declared shelving reason
  (#908): an optional `reason` text joins `actor` on the attributed
  `POST /command` envelope, is stamped onto the `CommandReceipt`
  beside `actor`, and echoes into the journaled `CommandSettled`
  entry — one durable record carrying actor, action, outcome, and
  reason. A point's declared `requires_reason` mark makes the field
  mandatory on its receipted writes, a required-but-absent reason
  refusing admission by name; serde-optional throughout under
  decision 3's convention.
- The durable sinks moved off the executor lock (#942, #982): the
  `--journal-file` append and the `--state-file` checkpoint persist
  each hand their record to a bounded queue — `journal_drain_capacity`
  and `state_drain_capacity` — drained by a dedicated writer in push
  order, so a stalled mount can neither lengthen a scan nor pin the
  serving lane. `GET /journal` and every mutating request attest the
  drain caught up off-lock inside a declared 30-second bound, the
  additive `publication.journal_sink` and `publication.state_sink`
  health sections report each sink's `healthy`/`lagging`/`failed`
  state with its accepted/drained/lost counters, and a queue full
  past the bound or a failed writer fails the run at the recording
  point with the loss accounted — never a silent gap.
- The graceful-shutdown contract (#820): the monitor's shutdown stops
  the serve loop and drains and joins the durable-sink writers, so a
  graceful close leaves the journal and state files complete through
  the last accepted record; an abrupt kill ends the file where the
  writer reached, which replay reads as the run's recorded end —
  never a torn record. The plant server closes its accept loop on
  SIGINT/SIGTERM the same way.
- The durable-history store's recorded contract and drain seam
  (#894, #907): a monitor-local append-only history file in the
  durable journal's pattern, recording declared-duty points at their
  declared cadence and replayed at bind — declared recording duty is
  model data under the optional-field convention, the `Drain`
  machinery the journal and state-file sinks already ride is the
  seam its writer reuses, and the multi-year retention obligation
  stays with the downstream records system the file and since-cursor
  endpoints export to.
- Declared-command `invoke` submissions validate their arguments at
  admission (#981): an argument the kind's `CommandDecl` does not
  declare refuses by name rather than silently dispatching, so the
  served argument schema — the same declaration `dcs-ctl invoke`
  parses against — is enforced at the bounded receipted path, not
  only client-side.
- The claim and observed-claimant contracts (#935, #987; decision
  97): the field's fencing verdicts carry the standing claim's owner
  token, so `field_claim_lost` attributes the takeover in the durable
  journal, and the demoted peer's fencing-loss mark drives a bound
  conditional re-grant each scan — granted where the field stands
  unclaimed or already names the run's token, refused while a
  different owner's claim stands, never preempting — so a released
  rogue claim ends with the ex-owner holding the field again. A
  refused conditional grant journals one `field_claim_observed` per
  distinct claimant the arbitration names, so a foreign `claim_writer`
  episode a peer only ever met through its probes enters the audit
  record once.
- The failover gate's standing proof is served (#1029; decision
  100): `GET /role` exposes the evidence `self_promote` reads — the
  standing convergence proof plus the consecutive-miss count against
  the armed budget — as an additive `RoleReport` field, so a
  `degraded` verdict inside a miss window reads as "verdict degraded,
  proof stands, failover fires at *N*" and a `not_converged` refusal
  on the requested path reads as the verdict gate working, not a
  contradiction.
- The born-active startup-failure contract (#985, #1017): a launched
  active's claim-then-lift activation runs only after every fallible
  local startup step — journal replay, monitor bind, peer-address
  resolution — so a doomed startup never leaves a stale claim fencing
  the field's standing owner; an unreachable field or a refused
  conditional grant fails startup with the named `FieldClaimFailed`,
  while a `--standby` tracking source that cannot resolve degrades to
  counted pull misses rather than failing the run.
- The incumbent-consultation gate (#735; decision 98): the
  restart-as-active takeover contract — the startup claim's recorded
  precondition that the restartee consult every incumbent endpoint the
  run can name before the grant fires, the attributed takeover record
  a granted claim owes the durable journal, and the receipt
  adjudication an older resumed baseline runs against the incumbent's
  settled line.
- The suspended-receipt convergence contract (decision 95; #1050,
  #1056, #1057, #1080, #1081): the pair's receipt log is one
  submission sequence — terminal outcomes absorb, an owner's verdict
  arbitrates, indices mint under the claim — and its suspended-entry
  duty now holds across the exercised crossings: a suspended
  `Accepted` settles once on a same-run demote→promote rather than
  deferring to a later successor; a restartee never re-applies a
  command the peer's log already settled; receipts mint in submission
  order under lane overflow; an adopted window's identical commands
  alias-audit suspended entries rather than vanishing them; and a
  restartee whose `--standby` name cannot resolve exits rather than
  resuming as a stale field owner.
- The announced-source verification fixes (decisions 12, 92, 101):
  recorded `?peer=` announces are demotion candidates, never pull
  targets — the demote verify and the lazy involuntary pass apply the
  document checks plus the keyed `line_proof` attestation, and on an
  unkeyed run announced hints are inert outright — while the field's
  write-ownership claim now carries the owner's declared monitor
  endpoint (#1042, #1045), giving the unkeyed pair's fenced-out peer
  the claim-arbitrated candidate that is its only provable rendezvous
  back into tracking.
- The served liveness contract (#991): `GET /health` answers a
  bounded `HealthReport` — the listener's `live` declaration, the
  served role, and the last completed scan's age — on the heartbeat
  lane so the probe answers while wedged consumers starve the bulk
  reads; `dcs-ctl health` is the shipped probe, the plant server's
  `ping` op is its matching plant half, and both published images
  declare `HEALTHCHECK` running them against the loopback listener.
- The deployment manifest's `topology` section (decision 99; #998):
  optional named redundant pairs declared over the `controllers`
  entries — members naming declared controllers, memberships
  disjoint, the pair's standby wiring closing inside it — with the
  one-duty-per-deployment bound refusing a second field-owning
  claimant by name as `rig-mismatch`; a site running several pairs
  composes one manifest per field.
- The in-library kinds declare their `History` and `Latest` event
  retentions (#1001), completing the decision-82 convention the
  `sequencer` set under `v0.3.0`, so the served surface carries every
  kind's declared retentions without consumer wiring.

The determination:

- `MODEL_VERSION` holds at `1`; `PlantModel::load` still accepts
  exactly that version. A `version: 1` document written against
  `v0.3.0` validates unchanged under `v0.4.0` tooling. The
  plant-model schema emission moved twice: #907's `record` io_point
  field added the `recording-duty` definition and #548's `unit` /
  `parameter_units` declarations — additive optional grammar both, so
  a `v0.3.0` document still validates unchanged.
- The checkpoint format set holds: `Checkpoint.format_version` still
  negotiates against `SUPPORTED_FORMAT_VERSIONS` (`{0, 1}`; absent
  reads as `0`). Checkpoints cross the bump under the same
  per-connection negotiation and fingerprint gate — no checkpoint
  migration is owed.
- The supported engineering surface grew only by addition, so a
  consumer's composition code compiles unchanged on the repin. The
  migration expectation is the repin itself — the reference plant's
  `ci/check.sh` `upgrade` stage proves the `v0.3.0` → `v0.4.0`
  crossing byte-identically; non-Rust consumers pin the same four
  schema artifacts `v0.3.0` recorded, their emissions unchanged.
- The `dcs-build` `station`, `dosing`, `ijmuiden`, and `ethercat`
  modules remain platform-owned reference compositions outside the
  compatibility policy (decision 81); a consumer composes from the
  supported primitives or copies a pattern.

## Consumer pins

- Crates: `dcs-build = { git = "<repo>", tag = "v0.4.0" }` — or
  `rev = "<commit>"` for the identical immutable commit, the recorded
  commit above once it is filled; `dcs-core` and `dcs-model` under the
  same pin.
- Tooling: `cargo install --git <repo> --tag v0.4.0 dcs-model
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

- `[workspace.package]` version `0.4.0` and the regenerated workspace
  `Cargo.lock` — one crate version across the release set.
- The reference plant's repin: `Cargo.toml`'s `tag = "v0.4.0"`,
  `ci/check.sh`'s `DCS_REV` default `v0.4.0` and `DCS_UPGRADE_REV`
  default at the `v0.3.0` recorded rev — the `upgrade` stage
  materializes the tree at the `v0.3.0` pin and repins to `v0.4.0`,
  proving the named crossing — `deploy/manifest.json`'s
  `dcs_release: "v0.4.0"` and `v0.4.0` image tags, and
  `deploy/compose.yaml`'s `x-dcs-release` and images.
- The check's schema non-drift legs continue to cover all four
  recorded artifacts — `deploy-schema` and `--dynamics-schema`
  beside `schema`/`interface-schema`.

The remaining items are the supervisor's publication operations:

- Cut `v0.4.0` on the `main` commit carrying this record once
  `rust-proofs` is green on that exact commit; fill the Commit field
  with the tagged sha.
- Build and publish the `dcs-controller` and `dcs-plant-server`
  images; fill the two digest fields above.
- Regenerate `reference-plant/Cargo.lock` against the published tag
  (`cargo update` in the consumer tree, README §7's documented step)
  so the committed lockfile records the `tag = "v0.4.0"` source — the
  lockfile cannot name the tag's target before the tag exists, so the
  repin commit carries the previous resolution and the check's resolve
  leg re-resolves on the first post-tag run.
- Re-run `reference-plant/ci/check.sh` end to end against the
  published artifacts and capture its output as the release's
  consumer evidence; the upgrade stage's doctored-version refusals
  must still report their named diagnostics.
