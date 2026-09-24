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
is exactly one new file and edits no shared file. Each leg declares
the window it needs in its own module — RUNS_AFTER/RUNS_BEFORE
constraints over scenario_* names, and RUNS_LAST for the leg that
closes the schedule — and the derivation check in
tests/test_qa_scenario_modules.py validates the declarations against
the discovered order.
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


# Ordering is declared, not listed: each leg module states the
# window it needs as RUNS_AFTER/RUNS_BEFORE constraints over
# scenario_* names — and RUNS_LAST for the dcs-ctl case, which
# closes the schedule — with the prose for its window beside the
# declaration. The derivation check in
# tests/test_qa_scenario_modules.py validates every declared
# constraint against this discovered order, so a new leg's
# ordering intent lives in its own file and edits no shared file.
# Later cases degrade to inconclusive when rig state an earlier
# case was to establish never landed.
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
