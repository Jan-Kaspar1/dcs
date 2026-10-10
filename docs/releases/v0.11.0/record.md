# Connected-control candidate contract: v0.11.0

Status: **pending candidate, not a published release**. The assigned clone stays
on the 0.10 development version. The clean-consumer proof stages all crates and
generic tooling as `v0.11.0-rc.1` at one immutable scratch Git revision, recorded
with binary/model hashes in `target/water-area-evidence/customer.json`. That
local release origin proves resolution and execution; it is not a durable
customer download or published container. The supervisor owns the version bump,
reviewed publication commit, tag, registry digests and customer repin.

## Compatibility determination

Legacy model version 1 remains supported by the new tooling. Older strict view
readers reject new display metadata even in a version-1 document. Models declaring authoritative
numeric equipment-control limits use version 2. Older controllers refuse 2
before scanning, rather than silently dropping limits. This is a model format
boundary, separate from the 0.x Rust minor-version boundary. Existing station
emissions and fixture fingerprints remain unchanged. Optional display bounds and
typed control roles describe presentation; limits govern writes and forces at
admission and dispatch, including restored runtimes.

Rust consumers constructing equipment controls, view nodes, `IoPoint` or
`PointSignal` directly must supply the new optional fields. Point display bounds
use the same `MeasurementDisplay` type as view nodes and pass through the derived
signal index; contextual drawing bounds may override them. Exhaustive matches must handle the new validation,
command-range, bounded-integrator, gated-flow and inventory-flow variants. Equipment/view records
with floating-point metadata implement PartialEq rather than Eq. These source
changes require a minor release. Modulating actuator factories now take typed
transfer/tolerance/dwell configuration instead of fixed diagnostic constants. The negative-gain PID anti-windup correction
changes draining-loop behavior at saturation; review that control change when
commissioning an existing loop. No checkpoint/journal/history format changes
are introduced. Same-model restart and compatible peer takeover are proven;
changed-model state migration is not implemented by this milestone.

The new dynamics schema adds bounded integration, Boolean-gated numeric flow and conserved inventory/overflow flow.
Prior release records retain their existing bytes and digest pins. Drift tests
compare this candidate to current emission instead of rewriting older records.
The served-registry and deployment schemas are unchanged.

| Candidate artifact | sha256 |
| --- | --- |
| `plant-model.schema.json` | `3ceb14e825ef914c49c3ab201efc7a980131b00fd4bf2d7804f3cfc3b5d462e2` |
| `block-interfaces.schema.json` | `ddc00496814a4e8cd0d6ec8a5d9fbb95e83f518dcd927b17a4802f13ac84013a` |
| `dynamics.schema.json` | `730a3dd6d80bf69f744a22d64e5c356e2d342f9603bbd9226cca5d2b2da0313d` |
| `deploy-manifest.schema.json` | `980430ca8725af997a7b5063f00d2a9663fe4619ca00a542917bde24f270cfa9` |

## Publication gates and ownership

Before cutting: run the required verification on the reviewed publication
commit, complete actual browser review, bump the entire release set to 0.11.0,
fill the immutable commit and image digests, publish the source tag and images,
and repin/re-resolve the independent customer lockfile. Run its own CI using
those exact artifacts with no workspace-tool substitution. Archive the emitted
schemas and customer/operator evidence with the release. The current v0.10
record is still pending and its customer lock resolves an earlier 0.9 commit;
this candidate does not retroactively publish or repair that old release line.

Plant activation remains the proposal in
[connected-water-activation](../../milestones/connected-water-activation.md).
There is no implemented plan/apply, state migration, authentication policy or
production deployment acceptance implied by this candidate record.
