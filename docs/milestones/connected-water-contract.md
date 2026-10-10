# Connected water area contract review

Assigned scope: Kaspar's 2026-10-09 milestone, after PR #1526. This review
precedes implementation and is part of the same CI-gated change set.

Reuse `pump`, `pumping_station`, `motor`, `valve`, `analog-input`, `pid`,
`manual-station`, `interlock`, and managed latching alarms. A discrete isolation
valve uses the existing Boolean actuator's command/feedback timing semantics;
it is an equipment composition, not a second executor block kind. The present
motor semantics check one end-position contact, not independent open/closed
limit switches. Name that limitation explicitly.

Add two field simulation declarations: `gated_flow` multiplies a finite Float
by an engineered gain only while its typed Boolean gate stands;
`bounded_integrator` clamps a finite Euler step to engineered bounds. Both
retain the existing hold-last-finite, quality-propagation and field-state
capture rules. A clamped tank boundary represents external overflow or an
empty vessel, not conserved storage beyond the boundary; the area documents
this simplification and alarms before overflow. Existing unbounded integrators
and historical accelerated station fixtures keep their existing semantics.

Add optional numeric `display` metadata to a drawing node: finite min/max and
an optional normal band inside them. Only a numeric point-bound measurement
may carry it. The value, unit, quality and command rights still come from the
bound point. Display limits never become control or alarm limits. This remains
one model, with no customer JavaScript, inferred bounds or UI-authoritative
state. Legacy documents omit the metadata. Rust struct constructors need the
new optional member; this requires a versioned engineering release before
customers repin. Render exact numeric readings beside the compact indicator,
show off-scale values and degraded quality, and never normalize an unknown
sample into a plausible good indication.

Equipment summaries must show units and all declared process feedback, plus
member interfaces for tuning/commands/events. Demand writes retain the existing
receipted meaning: applied means the request was stored, not that physical
feedback was achieved. Protections may block a standing request. Display the
blocking state and command settlement separately. Runtime admission remains
bounded and controller-owned. No change to checkpoint compatibility, field
ownership, alarm acknowledgment or manual protection policy is introduced.

Dependencies: retain serde/serde_json, the existing vanilla SVG/HTML monitor,
and standard-library acceptance harness. A chart framework adds no benefit to
one compact bar and the existing bounded history plot; preserve the established
trend and keyboard paths. This is simulated process training, not a validated
hydraulic design, independent protection system, or new active market.

Review amendment from implementation inspection: numeric writable demands had
no admission-time bounds. Add optional typed `limits: ParameterRange` to an
equipment control. Model validation requires finite, ordered bounds of the
point's kind and rejects conflicting declarations. Assembly places the limits
in the authoritative `PointMap`; both writes and forces validate at admission
and again at application, including carried commands after promotion. A named
`point_out_of_range` receipt identifies the point, value and bounds. The monitor
uses the same limits on all write affordances. Display bounds remain separate.
This changes the shared operator-command contract and requires a release cut;
it must not be disguised as a browser-only validation rule.

Review amendment: an optional typed `role` on equipment controls identifies
operating mode explicitly. The symbol reads this metadata, never a label/tag
regular expression. Other roles identify requests, setpoints, manual outputs,
service, acknowledgment and shelving. Omitted roles preserve legacy documents.

The existing `pumping_station` canonical emitter omits optional control roles
to retain its checked-in artifacts/fingerprints. The new area attaches roles
using typed station layout point identities; the reusable standalone pump
factory declares them. Historical release fixtures are not republished.

Compatibility review from the exact historical-tool check: old runtimes silently
ignore new equipment-limit fields in a version-1 document. Bounded-control
models therefore emit document version 2. Current loading/schema support legacy
version 1 and version 2, but limits in version 1 are rejected. Historical runtimes
refuse version 2 before scanning. Current tooling accepts presentation-only metadata in version 1, but older
strict view readers reject the added display field. Legacy documents themselves
remain accepted; this is not a promise that new metadata loads on old tooling. A versioned release is necessary, not merely recommended.

Release-record review: the model-version and dynamics changes have a separate
pending [v0.11 candidate record](../releases/v0.11.0/record.md). Historical schema
bytes are frozen at their PR #1526 checkout content; only the new candidate's
emission must match the changed tooling. This prevents an uncut historical
record from silently acquiring a different control contract. Publication and
customer repinning remain explicit release operations.

Existing evidence gap: the checked-in v0.1/v0.2 plant-schema copies already
differ from the digests in their historical records at the starting revision.
The new guard freezes those starting bytes; it does not certify historical
release identity or repair those records. A release audit must reconcile the
tagged emission and published artifacts separately. No historical schema or
record is rewritten by this milestone.

CI evidence transport uses the official
[`actions/upload-artifact`](https://github.com/actions/upload-artifact) action,
pinned to v7.0.2's full commit identity. The proof job retains the candidate and
customer Git bundles plus hashed JSON documents for 30 days, even if a later
proof fails. Git bundles preserve source and file modes when restored; this
does not package runtime executables or establish public release publication.
If the proof fails before producing artifacts, the upload warns and the proof's
own failure remains authoritative. Retention is a review aid, not a permanent
release archive.
