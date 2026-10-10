# Plant activation: plan/apply proposal

Proposal arising from the connected-area consumer proof; not a new command or
implemented rollout orchestrator. Candidate `WW-LCM-002` remains candidate.
Terraform may provision infrastructure. DCS must own the plant activation
contract because neither an infrastructure diff nor replacing a container
establishes control-state compatibility or exclusive field ownership.

## Artifact and plan identity

A plant build should emit a signed/hashed activation bundle: source revision,
engineering release and Cargo lock, canonical model SHA-256 and existing model
fingerprint, dynamics/simulation identity, schema/interface registry identity,
controller image digest, device bindings/topology, runtime settings, persistence
schema and explicit migration policy. A readable tag is insufficient identity.
The proof found this concretely: current binaries cannot substitute for the pending v0.10.0
record once shared schemas changed, and the existing tag pin/lock is not a
completed public release. Build tooling and engineering artifacts from
the same immutable release revision and record binary/image identities.

A DCS plan reads the current active model/runtime identity, owner/fencing epoch,
peer compatibility and convergence, pending commands, checkpoint basis, operator
setting revision and persistence health. It reports whether the proposed bundle
supports a compatible takeover, a declared state migration or a stopped
recommissioning. Model diff plus block/driver state compatibility determine
that result; container health alone does not. A stable model fingerprint and
checkpoint shape do not establish identical block semantics across artifacts.
This milestone's negative-gain anti-windup fix is a concrete example: its
same-artifact pair proof does not prove a mixed old/new draining-loop rollout.
Such an update needs declared semantic compatibility and an operating review. It lists removed/added points,
changed I/O ownership, command limits, initial values, alarm rationalization,
control tuning and setting conflicts. An unrecognized state/layout change is
refused, not silently cold-started under the old output claim.

Persist a plan identity covering both desired artifacts and observed current
basis, including a expiry or revision precondition. Apply must re-read that basis
and refuse stale plans, a changed field owner, lost convergence, failed persistence
or operator-setting conflicts. An approval/audit record belongs to the customer
role policy; this slice does not invent that role hierarchy.

The storage-saturation proof also establishes a recovery boundary: a failed
request may contain an admitted receipt while its persistence is unattested.
The controller closes its field-write gate and exits within a bounded drain
budget; it does not claim a physical safe-state write. A plan must reconcile
receipts, field feedback and the last attested checkpoint or compatible peer
before activation. A stale durable tail cannot undo outputs already applied.
Automatic retry of an uncertain command is unsafe; changing an image cannot
resolve that uncertainty. This proof does not choose a site fail-safe policy.

## Operator-setting ownership

Keep three explicit layers: engineered policy/limits and defaults; live operator
settings with actor, receipt and revision; authoritative component/driver state.
Current writable points and tuned parameters reside in checkpoints. Loading a
new default must not silently overwrite an operator's running setpoint or mode.
For each setting, a plan declares preserve, reset-to-engineered-value or migrate,
shows old/new values and verifies the result is legal under new limits. A value
outside new bounds needs a named policy decision; silently clamping it changes
plant behavior. Resetting mode/manual request can start equipment and belongs in
the plan's operational effect, not in infrastructure drift handling.

## Transfer, apply and recovery

For a compatible update: launch the candidate without field authority; validate
bindings and schema/state correspondence; adopt a fresh attested checkpoint;
converge measurements, integral/transfer state, alarm lifecycle, command queue
and receipts; confirm durable sinks; then use the existing demote/promote and
conditional field-claim protocol. Controller identity, model basis and ownership
must be checked at the final switch. The UI is a replaceable consumer throughout.
A pending request is carried once with its settlement; an applied receipt does
not prove physical position. New command limits must be checked again when a
carried command applies.

Record a durable operation id and phases so retries reconcile actual ownership
instead of repeating a switch blindly. Failure before handover leaves the old
owner controlling. Failure after handover requires discovering the actual owner
and current checkpoint before deciding to converge a fallback. Rolling an image
back is not necessarily a compatible state rollback: counters, alarms, pending
commands and physical process time have advanced. A recovery plan must name the
state basis it restores and its fencing/field-safe behavior.

The current full-model fingerprint also covers operator geometry/display
metadata. A presentation-only edit can therefore prevent checkpoint adoption
despite unchanged control/state semantics. A future compatibility plan must
prove which differences can safely preserve state; the runtime currently has no
separate control-state identity or approved migration path to waive that gate.

For incompatible changes, propose a separately approved process stop and
recommissioning procedure, not a hot-swap promise. Configuration backup must
include the activation bundle, live-setting revision, relevant checkpoint,
journal/history continuity, I/O mapping and recovery procedure. Secret handling,
retention, acceptable interruption and restore testing need customer answers.

## Gaps exposed and next evidence

The candidate consumer proves versioned code composition, exact-revision generic
tooling and compatible-model restart/takeover. It does not implement plan/apply,
online model migration, public artifact publication, setting conflict resolution,
operation recovery or multi-pair orchestration. The deployment manifest is not an
activation transaction. Close these only against a named owner deployment plan,
as required by the existing roadmap; do not generate an automatic backlog.

The legacy acceptance rerun also exposed persistence overload: a driven
256-scan batch outran the immutable baseline's 64-entry state-file queue,
panicked the handler and left an unbounded client waiting. The history harness
now uses 32-scan requests, each durability-attested before continuing, and
bounded HTTP with no retry for an unknown timeout outcome. This preserves the
history assertions; it does not fix the runtime's panic/poison recovery or
prove that a saturated persistence sink has an acceptable production outcome.
A deployment plan needs recording throughput, scan budget, overload disposition
and recovery evidence for its real storage and field-time behavior.

Terraform's [core workflow](https://developer.hashicorp.com/terraform/intro/core-workflow)
provides a familiar plan/apply infrastructure pattern. The distinction above is
DCS's proposed control lifecycle, not a claim that Terraform manages controller
state or guarantees bumpless process activation.
