"""Deterministic acceptance scenarios for the simulated QA rig.

Each scenario drives the redundant controller pair through the monitor
endpoints documented in docs/packaging.md (GET /role, /signals,
/snapshot, /receipts, /journal, /history, /schema, /resources; POST
/command, /demote, /promote) and returns one report-schema scenario case. Stdlib
only — the Lenovo host needs nothing but Python and Docker. The
restart scenario also triggers the runner-owned container lifecycle
action ctx['restart_controller'] carries and reads the per-controller
--journal-file the rig bind-mounts into the run directory; the
source-restart scenario triggers ctx['cold_restart_controller'] — the
same lifecycle shape plus the host-side state.json drop that makes
the resumed checkpoint stream regress — and reads both peers' journal
files and the plant's fencing answers on ctx['plant']. The
model-revision scenario likewise triggers ctx['start_revised'] — the
runner action that derives the recipe's revised model and launches the
run's third controller on it — and reads field-side truth off the
simulated plant's sim-net service at ctx['plant']. The
checkpoint-negotiation scenario triggers ctx['start_foreign'] — the
same derivation launched --standby <active> WITHOUT --revised so the
fingerprint gate must refuse it — and removes the peer through
ctx['stop_foreign']. The doomed-startup-claim scenario reuses the same
foreign launch/teardown actions after writing a corrupt first record
into the foreign peer's runner-owned --journal-file, proving through
the serving monitors and the plant's fencing probes that a startup
aborting before the preemptive claim never disturbs the incumbent.
The link-loss
scenario drives the runner-owned plant stop/start actions
ctx['stop_plant']/ctx['start_plant'] carry and probes the run's plant
server directly on ctx['plant'] — the field's own fencing evidence.
The dead-peer-latency scenario launches the run's driven third
controller through ctx['start_driven'] — a `--standby` peer whose
checkpoint pulls happen only inside `POST /scan`, so a batch on its
monitor is the per-request pull chain — isolates the pair's
checkpoint source through the controller stop/start actions, and
removes the peer again through ctx['stop_driven']; its pair-health
surface is the page's own `pairHealth` rule applied to the /role
reports the scenario polls. The standby-loss scenario drives the same
controller stop/start actions ctx['stop_controller']/
ctx['start_controller'] carry — the rig launches controllers with
--restart no, so a stopped container holds a real down-window — and
reads both peers' --journal-file paths for the refusal and
non-interference audits. The tracking-source-auth scenario drives the
same controller lifecycle seam to open the announced-only demotion
window — the tracking peer stopped, the field owner warm-restarted so
no announce is recorded — probes the `GET /checkpoint?peer=` contract
with crafted announce hints and a forged-checkpoint server placed per
the run config's recorded `endpoint_placement` ('bridge': a labeled
container on the run's rig network, since the host egress policy
drops every packet a rig peer aims at a host socket — a forge bound
on the scenario host is unreachable from the rig), and audits the
served journals for the adoption entries every verified source owes.

The field-fault, backup-health, and unavailable-fallback cases
inject and clear per-point faults on the shared simulated field
through the shipped
`dcs-plant-ctl` binary — the plant-side tool the dcs-plant-server
image carries, exec'd inside the run's plant container against its
loopback listener through ctx['plant_ctl'] — so the lane drives the
tool's own contract rather than a second Python implementation of
the wire protocol crates/dcs-sim-net/src/protocol.rs documents. The
lag-staging case instead opens the raw protocol surface on the
published plant port and ensures the field writer claim under the
settled active's pinned --owner-token (ctx['plant_owner']) — the
designed shared-claim path for a test harness — so its inflow writes
drive the dynamics' declared forcing input while the single-writer
fencing keeps every other owner out; that claim shape and the bare
`step` fencing probe are the ops the tool does not expose, so those
legs stay on the raw client. The unclaimed-rearm case uses the same
split: its field census and watched-output reads ride the shipped
tool, while the preempt-and-release induction and the bare `step`
fencing probes stay on the raw client — `claim_writer` is an op the
tool does not expose, and its conditional ensure could never preempt
the standing owner into the unclaimed window. The field-claim case
keeps the whole lifecycle on a third sim-net attachment on the same
published endpoint — fenced step/write probes, the conditional and
shared grants under foreign and owner tokens (ctx['plant_owner']
names the standing owner's pin), the release legs, and a rogue
preemption are all ops the tool does not expose — while its field
census rides the tool's `list`. Every plant-probe attachment above
stays host-side on the published loopback port — the placement the
run config's recorded `endpoint_placement` gives the shared-claim
legs; an attachment that must sit inside the rig network instead
runs bridge-placed in a labeled rig-bridge container, dialed by
container name, because the egress policy refuses rig-network
traffic to host sockets. The fenced-writer-degrade case
drives the misordered promote the same claim makes survivable —
POST /promote on the tracking standby while the active still runs —
then asserts the superseded peer's demote-in-place contract through
its serving monitor, its --journal-file the rig bind-mounts per
controller, and the field's own claim answers on ctx['plant'].

Evidence is written into the run's evidence/ directory as each response
arrives, so a killed run still leaves inspectable artifacts behind.

Package layout (split under #928): each leg of the schedule lives in
its own module — qa_lane/scenarios/NNNN_<slug>.py — carrying its
scenario_* function, its leg-private helpers, and its private
tunables. The shared helpers, the Case record, and the patch-point
tunables live in qa_lane/scenarios/common.py; every leg module binds
them through `from .common import *`. The package facade re-exports
every name the legs and common define, so qa_lane.scenarios.<name>
resolves exactly as the monolith's did, and a ModuleType __setattr__
on the facade propagates writes into common and every leg module
binding the name — patch.object(scenarios, ...) test seams keep
resolving through the facade unchanged. SCENARIOS is discovered, not
listed: the NNNN_ filename prefix is the run position (numbers are
spaced by 100 so a new leg inserts without renumbering), so a new leg
is exactly one new file and edits no shared file. The ordering
comment above SCENARIOS records which window each leg needs.
"""
import importlib
import sys
import types
from pathlib import Path

from . import common as _common
from .common import *  # noqa: F401,F403 — the shared seam, re-exported


def _leg_stems():
    """Every leg module's stem in schedule order — the NNNN_ numeric
    filename prefix is the run position."""
    found = {}
    for entry in __path__:
        for path in Path(entry).glob('[0-9]*_*.py'):
            stem = path.stem
            number, _, slug = stem.partition('_')
            if not slug or not number.isdigit():
                raise ValueError('scenario leg module ' + path.name
                                 + ' must be named NNNN_<slug>.py')
            found.setdefault(stem, (int(number), path.name))
    return [stem for stem, _ in sorted(found.items(),
                                       key=lambda item: item[1])]


def _leg_modules():
    """Import each leg module in schedule order."""
    return tuple(importlib.import_module('.' + stem, __name__)
                 for stem in _leg_stems())


def _scenario_functions(modules):
    """The discovered scenario_* functions, in schedule order."""
    fns = []
    for mod in modules:
        fns.extend(sorted(
            (value for name, value in vars(mod).items()
             if name.startswith('scenario_')),
            key=lambda fn: fn.__name__))
    return fns


def _reexport(modules):
    """Copy every leg module's names onto the facade so the pre-split
    qa_lane.scenarios.<name> surface is unchanged."""
    for mod in modules:
        for name, value in vars(mod).items():
            if not name.startswith('__'):
                globals()[name] = value


_LEG_MODULES = _leg_modules()
_reexport(_LEG_MODULES)

# Every module whose dict holds the shared or leg-private names — a
# patch.object(scenarios, ...) write propagates into each of them.
_SEAM = (_common,) + _LEG_MODULES


# The restart case runs ahead of the failover case: the peer it stops
# is ctrl-a — launched without --standby, so its resumed process comes
# back active — while ctrl-b is the tracking standby the settle check
# watches reconverge. The source-restart case runs in the same
# pre-switch window — it needs ctrl-a field owner so ctrl-b is the
# tracked adopter, drives its own a->b leg for the demoted-peer
# regression, and ends back on the launch roles with ctrl-a owning
# the field again. The field-claim case shares that window: the
# settled pair's pinned owner token is the standing claim its third
# attachment probes, shares, and rogue-preempts, and its demote and
# re-promote of the same peer lands the pair back on the launch
# roles before the cases that follow. The fenced-writer-degrade case
# shares that restored window: the tracking standby takes the
# misordered promote, the superseded peer demotes in place, and the
# documented demote/promote order lands the pair back on the launch
# roles before the cases that follow. The dead-peer-latency case
# sits in the same
# restored window: it isolates ctrl-a — the source ctrl-b and its
# driven third peer pull from — restores it before the armed failover
# bound, and removes the driven peer, so the launch roles still hold
# for the cases that follow. The monitor-starvation case runs in the
# same armed window: it needs ctrl-b — the only peer launched
# --auto-promote — as the tracking standby whose checkpoint pulls
# measure the starved ctrl-a monitor, and it leaves the launch roles
# untouched, so it must run before the tune case's a->b switch. The
# duty-rotation case shares that
# restored window: it cycles demand through the writable maintenance
# points, runs its own mid-cycle a->b switch for the carried rotation
# position, and fails back to the launch roles before the force case.
# The force-carryover case runs on the
# same pre-switch window — ctrl-a active, ctrl-b tracking — driving
# its own a->b leg for the forced-point evidence and failing back to
# the launch roles before the tune case runs its switch. The
# backup-health case sits in the same restored window: only with the
# pair settled and tracking does a backup-only field fault have a
# standby whose takeover the annunciation must precede — the leg
# injects, annunciates, acks, clears, and restores without moving the
# selection or the roles. The source-failover case shares that
# window: the complementary primary-faulted leg faults the field
# source the selection currently rides, watches the failover-select
# engage the backup and the wired alarm's two-flag lifecycle, acks,
# clears, and restores — field, latch, and launch roles as found.
# The
# lag-staging case sits in the same restored window: the settled
# pair's pinned owner token is the shared claim its inflow drive
# needs, and the leg writes, stages, annunciates, acks, drains, and
# restores — inflow back to baseline, the ack input re-armed, no pump
# operator state touched, no role moved. The standby-loss case sits
# in the same restored window: it needs a tracking standby to refuse
# and to lose, drives its own a->b switch for the promotion-gate leg,
# and demote/promotes back to the launch roles, so it runs before the
# tune case's a->b switch. The demote-settle-uniqueness case shares
# that window: it races receipted submissions against the documented
# demote on whichever peer owns the field, cycles the switch twice per
# pass, and lands the pair back on the launch roles before the tune
# case's a->b switch. The demote-carry-settle case shares that
# window: it races receipted submissions on whichever peer owns the
# field around a rogue claim's preemption — the involuntary demote
# the #829 boundary contract covers — lets the tracking peer carry
# the suspended admissions, promotes it, and lands the pair back on
# the launch roles before the tune case's a->b switch. The
# peer-announce case shares that window: it
# guards the demotion's announced tracking source with a foreign
# checkpoint announce, drives its own demote-then-promote switch for
# the reconvergence and fencing legs, and restores the launch roles
# before the tune case's a->b switch. The
# parameter-tune case also runs ahead of the
# failover leg: only ctrl-b tracks (its --standby source is ctrl-a),
# so a tuned value can cross a checkpoint only from ctrl-a to ctrl-b,
# and the promotion it performs is the run's one a->b switch — the
# failover leg behind it demotes whichever peer reports settled active
# and promotes the converged one back. The checkpoint-negotiation case
# sits between them and the model-revision case: it needs the pair
# still on the mounted fingerprint so the recipe-derived document is
# foreign, and it removes its foreign peer before the revision launch
# takes the third-controller seat. The doomed-startup-claim case
# shares that foreign seat beside it — launched onto a corrupt journal
# file against the settled pair and torn down before either revision
# case claims the seat. The model-revision case runs
# behind the failover: whichever peer holds the field then is the one
# its third --revised controller stands by on and supersedes, so every
# case after it already exercises the revised model document. The
# incompatible-revision case sits immediately ahead of it: its
# carryover-breaking peer never promotes, so the field writer is
# unchanged, and the compatible case's launch replaces the degraded
# third container and performs the control's promote leg in the same
# run. The command-availability case shares the
# post-failover window: it is self-contained on either role layout —
# it probes whichever endpoint reports settled active and reads the
# tracking peer for the parity leg — and its only mutation is a
# served-available command the earlier command cases already issue.
# The plant-link-loss case
# follows later in the schedule: its plant container cycling cannot
# contaminate an earlier case, and whichever endpoint owns the field
# by then keeps it through the outage and recovery the scenario
# drives. The field-fault case is self-contained on either role
# layout — including the post-recovery rig — and leaves the rig as it
# found it. The event-retention case rides beside the
# served-interface case — the same registry surface, the same
# either-layout self-containment, and nothing but receipted drives on
# the field-owning peer. The alarm-rationalization case rides
# beside them — the same registry surface plus the field owner's
# durable journal, the same either-layout self-containment, and one
# receipted retune it restores before returning. The
# unclaimed-rearm case is the same shape:
# its preempt-and-release induction opens the ownerless window behind
# whichever peer owns the field, watches the recorded owner's inline
# re-arm and the fencing it restores, and leaves the claim state and
# launch roles as found. The unavailable-fallback case is
# self-contained on either role layout as well: it drives per-point
# faults on the shared field through the shipped plant tool and
# clears them all in teardown. The power-fail-trip case is the same
# shape: it writes the field contact through the plant protocol under
# whichever peer owns the field, restores the contact and re-arms every
# alarm latch it drove, and perturbs no role — a simulated process
# trip is not peer loss. The dcs-ctl case closes the schedule:
# it observes the post-failover role layout and perturbs nothing
# earlier cases established.
SCENARIOS = tuple(_scenario_functions(_LEG_MODULES))


class _Facade(types.ModuleType):
    """Patch-seam facade: patch.object(qa_lane.scenarios, name, value)
    sets the facade attribute and then rewrites every binding of `name`
    in common and in the leg modules, so scenario code keeps resolving
    patched names through module globals exactly as it did when all of
    them lived in the monolith's single namespace."""

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name.startswith('__') and name.endswith('__'):
            return
        for mod in _SEAM:
            if name in vars(mod):
                setattr(mod, name, value)


sys.modules[__name__].__class__ = _Facade


def run_all(ctx, timeline):
    """Run every scenario in order; later cases degrade to inconclusive
    when the rig state they need was never established."""
    results = []
    for fn in SCENARIOS:
        if ctx.get('deadline') and time.monotonic() > ctx['deadline']:
            record = Case(fn.__name__.replace('scenario_', '')
                          .replace('_', '-'),
                          fn.__doc__ or '', 'run deadline reached'
                          ).finish('inconclusive',
                                   'hard timeout reached before this '
                                   'scenario could run')
            results.append(record)
            continue
        record = fn(ctx)
        if record['outcome'] != 'passed':
            degraded = True
        results.append(record)
        timeline('scenario-' + record['outcome'],
                 record['key'] + ': ' + record['title'])
    return results
