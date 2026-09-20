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
non-interference audits.

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
census rides the tool's `list`. The fenced-writer-degrade case
drives the misordered promote the same claim makes survivable —
POST /promote on the tracking standby while the active still runs —
then asserts the superseded peer's demote-in-place contract through
its serving monitor, its --journal-file the rig bind-mounts per
controller, and the field's own claim answers on ctx['plant'].

Evidence is written into the run's evidence/ directory as each response
arrives, so a killed run still leaves inspectable artifacts behind.
"""
import json
import math
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

SCENARIO_TIMEOUT = 120  # per-scenario wall clock bound
POLL_INTERVAL = 2.0
RESTART_POLL = 1.0              # cadence watching the pair mid-restart
RESTART_RETURN_DEADLINE = 60  # bound on the restarted monitor's return
RESTART_SETTLE_DEADLINE = 60  # bound on active/standby roles settling
RESTART_JOURNAL_DEADLINE = 30  # bound on the run-boundary record landing
# Bound on the restart-window command's verdict: the point serving the
# second write's value and the durable journal carrying its
# CommandSettled — on whichever side of the run boundary it landed.
RESTART_COMMAND_DEADLINE = 30
# Scans the persisted checkpoint may lag the last served snapshot: the
# state file is written at the end of each completed scan cycle, so a
# /snapshot answer can interleave before that cycle's write lands.
RESTART_SLACK_TICKS = 4


class Case:
    """One scenario's accumulating report record."""

    def __init__(self, key, title, expected):
        self.record = {'key': key, 'title': title, 'expected': expected,
                       'outcome': 'inconclusive', 'observations': [],
                       'evidence': []}

    def observe(self, text):
        self.record['observations'].append(text)

    def evidence(self, kind, ref, detail=None):
        entry = {'kind': kind, 'ref': ref}
        if detail:
            entry['detail'] = detail
        self.record['evidence'].append(entry)

    def finish(self, outcome, detail=None):
        self.record['outcome'] = outcome
        if detail:
            self.record['detail'] = detail
        return self.record


def http_json(method, url, body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, json.loads(response.read() or b'null')


def wait_for(check, deadline, interval=POLL_INTERVAL):
    """Poll `check` until it returns a truthy value or the deadline passes."""
    last = None
    while time.monotonic() < deadline:
        last = check()
        if last:
            return last
        time.sleep(interval)
    return last


def save_evidence(evidence_dir, name, payload):
    path = Path(evidence_dir) / name
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + '\n')
    return 'evidence/' + name


def _role(ctx, base):
    _, body = http_json('GET', base + '/role')
    return body


def _snapshot(ctx, base):
    _, body = http_json('GET', base + '/snapshot')
    return body


def _point_value(snapshot, point):
    for entry in snapshot.get('points', []):
        if entry.get('point') == point and entry.get('sample'):
            value = entry['sample'].get('value')
            if isinstance(value, dict):
                return next(iter(value.values()), None)
            return value
    return None


def _history_qualities(payload, point):
    """One point's served history as (seq, quality-key, tick) triples out
    of a `/history` answer — the record the stale interval must survive
    in."""
    if isinstance(payload, list):
        for entry in payload:
            if entry.get('point') == point:
                return [(sample.get('seq'),
                         _quality_key(
                             (sample.get('sample') or {}).get('quality')),
                         (sample.get('sample') or {}).get('tick'))
                        for sample in entry.get('samples', [])]
    return []


def _receipt_list(payload):
    """The receipt records out of either wire shape — a bare list or a
    `{"receipts": [...]}` envelope."""
    if isinstance(payload, list):
        return payload
    return payload.get('receipts', [])


def _journal_list(payload):
    """The journal records out of either wire shape — a bare list or an
    `entries`/`journal` envelope."""
    if isinstance(payload, list):
        return payload
    return payload.get('entries', payload.get('journal', []))


def _command_covered(receipts, point):
    """Whether a `GET /receipts` payload carries the scenario's own
    submission — the run's audit reaching that peer."""
    for receipt in _receipt_list(receipts):
        write = receipt.get('command', {}).get('write_value', {})
        if write.get('point') == point:
            return True
    return False


def _journal_covers(journal, point):
    """Whether a `GET /journal` payload recorded the scenario command's
    settlement."""
    for entry in _journal_list(journal):
        receipt = entry.get('event', {}).get('command_settled', {}) \
            .get('receipt', {})
        write = receipt.get('command', {}).get('write_value', {})
        if write.get('point') == point:
            return True
    return False


def _settled_active(ctx):
    """The ctx endpoint key whose peer currently reports role=active,
    or None while the pair is mid-transition. The model-revision
    case's third endpoint answers on 'revised' once its action ran —
    an unlaunched key reads as a refused connection and is skipped."""
    for name in ('active', 'standby', 'revised'):
        if ctx.get(name) is None:
            continue
        try:
            if _role(ctx, ctx[name]).get('role') == 'active':
                return name
        except Exception:
            pass
    return None


def _writable_bool_point(signals):
    """The scenarios' command target out of a SignalIndex: the
    pump-station 'p101-oos' writable bool in-point when the model
    declares it, else any writable bool input."""
    target = None
    for entry in signals.get('points', []):
        if entry.get('name') == 'p101-oos' and entry.get('writable'):
            return entry
        if target is None and entry.get('writable') \
                and entry.get('direction') == 'in' \
                and entry.get('value_type') == 'bool':
            target = entry
    return target


def _journal_records(path):
    """The ordered records of a `--journal-file`: {'boundary': {'run',
    'tick'}} markers and {'seq': n} entry lines. A torn final line — a
    crash mid-append — is skipped; any earlier unparseable or
    unrecognized line raises."""
    items = []
    lines = Path(path).read_text().splitlines()
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            if index == len(lines) - 1:
                continue
            raise ValueError('journal file ' + str(path) + ' line '
                             + str(index + 1) + ' does not parse')
        if isinstance(record, dict) and 'run_boundary' in record:
            items.append({'boundary': record['run_boundary']})
        elif isinstance(record, dict) and 'entry' in record:
            items.append({'seq': (record['entry'] or {}).get('seq')})
        else:
            raise ValueError('journal file ' + str(path) + ' line '
                             + str(index + 1)
                             + ' is not a journal record')
    return items


def scenario_controller_active(ctx):
    """The launched active peer owns the field and produces telemetry."""
    case = Case('controller-active',
                'Active controller owns the simulated field',
                'ctrl-a reports role=active and /snapshot tick advances')
    try:
        role = _role(ctx, ctx['active'])
        ref = save_evidence(ctx['evidence_dir'],
                            'controller-active-role.json', role)
        case.evidence('file', ref, 'RoleReport from the launched active')
        if role.get('role') != 'active':
            case.observe('role=' + str(role.get('role')))
            return case.finish('failed', 'launched peer is not active')
        case.observe('ctrl-a reports role=active at tick ' +
                     str(role.get('tick')))
        first = _snapshot(ctx, ctx['active'])
        deadline = time.monotonic() + 30
        grown = wait_for(
            lambda: (s.get('tick', 0) > first.get('tick', 0) and s or None)
            if (s := _snapshot(ctx, ctx['active'])) else None,
            deadline)
        if not grown:
            return case.finish('failed', 'telemetry tick did not advance')
        ref = save_evidence(ctx['evidence_dir'],
                            'controller-active-snapshot.json', grown)
        case.evidence('file', ref,
                      'tick ' + str(first.get('tick')) + ' -> '
                      + str(grown.get('tick')))
        case.observe('snapshot tick advanced ' + str(first.get('tick'))
                     + ' -> ' + str(grown.get('tick')))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', 'monitor unreachable: ' + str(exc))


def scenario_standby_tracking(ctx):
    """The standby converges behind the active via checkpoints."""
    case = Case('standby-tracking',
                'Standby converges behind the active',
                'ctrl-b reports role=standby with tracking convergence '
                'within ' + str(SCENARIO_TIMEOUT) + 's')
    deadline = time.monotonic() + SCENARIO_TIMEOUT
    try:
        final = None
        report = {}
        while time.monotonic() < deadline and final is None:
            report = _role(ctx, ctx['standby'])
            sync = report.get('sync')
            if report.get('role') == 'standby' and isinstance(sync, dict) \
                    and 'tracking' in sync:
                final = report
            else:
                time.sleep(POLL_INTERVAL)
        ref = save_evidence(ctx['evidence_dir'],
                            'standby-tracking-role.json', report)
        case.evidence('file', ref, 'final RoleReport from ctrl-b')
        if final is None:
            case.observe('last report: role=' + str(report.get('role'))
                         + ' sync=' + json.dumps(report.get('sync')))
            return case.finish('failed',
                               'standby did not reach tracking convergence')
        case.observe('ctrl-b tracking, aligned at tick '
                     + str(final['sync']['tracking'].get('aligned')))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', 'monitor unreachable: ' + str(exc))


def scenario_operator_command(ctx):
    """A writable-point command applies at a scan boundary."""
    case = Case('operator-command',
                'Writable point command applies at a scan boundary',
                'POST /command writing the p101-oos point true is accepted '
                'and the value is visible in the next snapshot')
    try:
        _, signals = http_json('GET', ctx['active'] + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'operator-command-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming writable points')
        target = _writable_bool_point(signals)
        if target is None:
            return case.finish('inconclusive',
                               'no writable bool point in the model')
        point = target['point']
        case.observe('command target: ' + str(target.get('name'))
                     + ' point ' + str(point))
        command = {'command': {'write_value': {
            'point': point, 'kind': 'bool', 'value': {'bool': True}}},
            'actor': 'qa-lane'}
        status, receipt = http_json('POST', ctx['active'] + '/command',
                                    command)
        ref = save_evidence(ctx['evidence_dir'],
                            'operator-command-receipt.json',
                            {'status': status, 'body': receipt})
        case.evidence('file', ref, 'submission receipt')
        case.observe('POST /command answered ' + str(status) + ': '
                     + json.dumps(receipt))
        if status != 200:
            return case.finish('failed', 'command refused: ' + str(receipt))
        deadline = time.monotonic() + 30
        observed = wait_for(
            lambda: _point_value(_snapshot(ctx, ctx['active']), point)
            is True or None, deadline)
        snap = _snapshot(ctx, ctx['active'])
        ref = save_evidence(ctx['evidence_dir'],
                            'operator-command-snapshot.json', snap)
        case.evidence('file', ref, 'snapshot after command')
        if not observed:
            return case.finish('failed',
                               'point ' + str(point)
                               + ' did not read true in telemetry')
        case.observe('point ' + str(point) + ' reads true in telemetry')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))


def scenario_failover(ctx):
    """Demote the active, promote the converged standby."""
    case = Case('failover',
                'Demote/promote switchover preserves the field',
                'POST /demote on the settled active then POST /promote '
                'on the converged peer leaves ctrl-b active with '
                'telemetry advancing')
    try:
        # The demote target is whichever endpoint reports settled
        # active: a lone replay finds ctrl-a, while the suite reaches
        # this case after the parameter-tune case already ran the a->b
        # switch — the leg then re-cycles ctrl-b through its own
        # demote, reconvergence on ctrl-a's checkpoints, and
        # promote-back. ctrl-b stays the promote target either way: it
        # is the rig's only checkpoint-tracking peer.
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        status, body = http_json('POST', ctx[active] + '/demote')
        case.observe('demote ' + active + ': ' + str(status) + ' '
                     + json.dumps(body))
        if status != 200:
            return case.finish('failed', 'demote refused: ' + str(body))
        deadline = time.monotonic() + 30
        promoted = None
        while time.monotonic() < deadline and promoted is None:
            try:
                status, body = http_json('POST', ctx['standby'] + '/promote')
                if status == 200:
                    promoted = body
                else:
                    time.sleep(POLL_INTERVAL)
            except urllib.error.HTTPError as exc:
                if exc.code == 409:
                    time.sleep(POLL_INTERVAL)
                else:
                    raise
        ref = save_evidence(ctx['evidence_dir'], 'failover-promote.json',
                            promoted or {'refused': True})
        case.evidence('file', ref, 'promote response from ctrl-b')
        if promoted is None:
            return case.finish('failed',
                               'promote never succeeded within 30s')
        deadline = time.monotonic() + 30
        settled = wait_for(
            lambda: (r.get('role') == 'active' and r or None)
            if (r := _role(ctx, ctx['standby'])) else None, deadline)
        ref = save_evidence(ctx['evidence_dir'], 'failover-roles.json',
                            {'ctrl-b': settled})
        case.evidence('file', ref)
        if not settled:
            return case.finish('failed', 'ctrl-b did not settle active')
        first = _snapshot(ctx, ctx['standby'])
        deadline = time.monotonic() + 30
        grown = wait_for(
            lambda: (s.get('tick', 0) > first.get('tick', 0) and s or None)
            if (s := _snapshot(ctx, ctx['standby'])) else None, deadline)
        ref = save_evidence(ctx['evidence_dir'], 'failover-snapshot.json',
                            grown or first)
        case.evidence('file', ref)
        if not grown:
            return case.finish('failed',
                               'promoted peer telemetry did not advance')
        case.observe('ctrl-b active, tick advancing after switchover')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))


# --------------------------------------------------------------------
# The receipted parameter-tuning path and its carryover across
# promotion — WW-OPS-001's bounded, validated, receipted tuning clause
# and WW-LCM-001's runtime-tuning continuity. A descriptor-declared
# Float parameter out of the served registry is retuned through
# POST /command; the settled receipt, the served parameter report, and
# the transition journal must all reflect it; an out-of-range tune
# must meet the named out_of_range rejection and change nothing; and
# the promoted peer's parameter report must still carry the tuned
# value — the tuned state rode the checkpoint the tracking standby
# adopted rather than re-initializing to the model default.
#
# The case must run while ctrl-a is still the active: ctrl-b is the
# rig's only checkpoint-tracking peer (its --standby source is ctrl-a),
# so a tuned value can cross a checkpoint only from ctrl-a to ctrl-b.
# The switch it performs is also the run's one promotion — once ctrl-b
# claims the plant's single-writer claim, ctrl-a can never take the
# field back — so the case sits immediately ahead of the failover leg,
# which then re-cycles the surviving peer.

TUNE_DEADLINE = 30  # bound on the receipt, report, and switch waits


def _parameter_value(snapshot, component, name):
    """The live value a snapshot's `parameters` section reports for one
    descriptor-declared parameter — the section the checkpoint's
    component state reports through — or None when absent."""
    for entry in snapshot.get('parameters') or []:
        if entry.get('name') == component:
            value = (entry.get('values') or {}).get(name)
            if isinstance(value, dict):
                return next(iter(value.values()), None)
            return value
    return None


def _float_tune_plan(schema, snapshot):
    """The scenario's tune target out of the served registry and live
    parameter report: (component, parameter, current, tuned, outside)
    for the first descriptor-declared Float whose inclusive [min, max]
    bounds hold a changed finite in-range value and admit a finite
    out-of-range submission. Returns None when no served parameter can
    exercise both legs."""
    reported = {entry.get('name'): entry.get('values') or {}
                for entry in snapshot.get('parameters') or []}
    for entry in schema.get('interfaces') or []:
        component = entry.get('name')
        values = reported.get(component)
        if not isinstance(values, dict):
            continue
        for prop in (entry.get('interface') or {}) \
                .get('configuration') or []:
            if prop.get('kind') != 'float' \
                    or prop.get('capability', 'tunable') != 'tunable':
                continue
            bounds = prop.get('range') or {}
            lo = (bounds.get('min') or {}).get('float')
            hi = (bounds.get('max') or {}).get('float')
            name = prop.get('name')
            raw = values.get(name)
            current = raw.get('float') if isinstance(raw, dict) \
                else None
            if lo is None or hi is None or name is None \
                    or current is None or not lo < hi:
                continue
            tuned = next(
                (c for c in (lo + 1.0, current + 1.0, hi - 1.0,
                             (lo + hi) / 2.0, lo, hi)
                 if math.isfinite(c) and lo <= c <= hi
                 and c != current), None)
            if tuned is None:
                continue
            below = lo - 1.0
            if not below < lo:
                below = math.nextafter(lo, -math.inf)
            above = hi + 1.0
            if not above > hi:
                above = math.nextafter(hi, math.inf)
            outside = below if below < lo \
                else above if above > hi else None
            if outside is None or not math.isfinite(outside):
                continue
            return component, name, current, tuned, outside
    return None


def _journal_covers_parameter(journal, component, name):
    """Whether a `GET /journal` payload recorded the scenario
    `set_parameter` command's settlement."""
    for entry in _journal_list(journal):
        receipt = entry.get('event', {}).get('command_settled', {}) \
            .get('receipt', {})
        tune = receipt.get('command', {}).get('set_parameter', {})
        if tune.get('component') == component \
                and tune.get('name') == name:
            return True
    return False


# --------------------------------------------------------------------
# The rolling model-revision contract (WW-LCM-001's deployment-update
# clause, the rolling model-revision decision): a site deploys an
# updated plant model by launching a third controller on the revised
# document with `--standby <active> --revised`; once the peer reports
# the named `reinitialized` convergence carrying its carryover report,
# the documented demote-then-promote order moves the field writer onto
# the revised model's fingerprint while field writes, receipts, and
# the durable journal continue across the boundary — and the demoted
# peer settles rather than resuming writes (the plant's single-writer
# fence meets any leaked attempt).

REVISION_CONVERGE_DEADLINE = 120  # bound on the revised peer's crossing
REVISION_SETTLE_DEADLINE = 60     # bound on demote/promote role settles
REVISION_FIELD_ROUNDS = 6         # field/telemetry reads after the roll
REVISION_POLL = 0.5               # cadence watching the roll's peers


def _field_sample(ctx, point):
    """The plant's stored sample for `point` — `{'value', 'quality',
    'tick'}` — or None when the read dropped; a lost observation, never
    the leg's verdict. The shipped tool's `read` serves it."""
    body = _try_plant_ctl(ctx, 'read', str(point))
    if not isinstance(body, dict):
        return None
    return body.get('sample')


def _field_out_points(ctx):
    """Every field `out` point the simulated plant serves — the points
    a field-owning scan writes — from the plant's own census, listed by
    the shipped tool."""
    body = _try_plant_ctl(ctx, 'list')
    points = (body or {}).get('points') or []
    return [entry['point'] for entry in points
            if isinstance(entry, dict)
            and entry.get('direction') == 'out']


def _journal_entries(path):
    """The parsed records of a `--journal-file` with their full bodies:
    `{'run_boundary': {...}}` markers and `{'entry': {'seq', 'tick',
    'event'}}` lines. A torn final line — a crash mid-append — is
    skipped; any earlier unparseable or unrecognized line raises."""
    items = []
    lines = Path(path).read_text().splitlines()
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            if index == len(lines) - 1:
                continue
            raise ValueError('journal file ' + str(path) + ' line '
                             + str(index + 1) + ' does not parse')
        if isinstance(record, dict) \
                and ('run_boundary' in record or 'entry' in record):
            items.append(record)
        else:
            raise ValueError('journal file ' + str(path) + ' line '
                             + str(index + 1)
                             + ' is not a journal record')
    return items


def _journal_settled(item):
    """The receipt a journal record's `command_settled` event carries,
    or None — for a `--journal-file` record (`{'entry': {...}}`) and a
    served `GET /journal` entry alike."""
    body = item.get('entry') if isinstance(item.get('entry'), dict) \
        else item
    event = (body or {}).get('event') or {}
    return (event.get('command_settled') or {}).get('receipt')


def _journal_observation(item):
    """(kind, point, from, to) when the record is a `quality_changed`
    or `point_changed` entry, else None — the observation record the
    restart-census leg folds."""
    body = item.get('entry') if isinstance(item.get('entry'), dict) \
        else item
    event = (body or {}).get('event') or {}
    for kind in ('quality_changed', 'point_changed'):
        if isinstance(event.get(kind), dict):
            change = event[kind]
            return kind, change.get('point'), \
                change.get('from'), change.get('to')
    return None


def _receipt_key(receipt):
    """A receipt's canonical identity — the dedup legs compare the
    journaled record, not the receipt's position."""
    return json.dumps(receipt, sort_keys=True)


def scenario_model_revision(ctx):
    """Roll a revised model in through a third `--revised` controller."""
    case = Case('model-revision',
                'In-service model revision rolls the field writer',
                'a third controller launched --standby <active> '
                '--revised on the recipe-derived revised model converges '
                'reporting reinitialized with its carryover report, the '
                'demote-then-promote order moves the field writer onto '
                'the revised fingerprint, field writes continue '
                'bumplessly, receipts and the journal file continue '
                'their sequence, and the demoted peer settles without '
                'serving writes')
    try:
        start = ctx.get('start_revised')
        revised = ctx.get('revised')
        if start is None or revised is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no model-revision action or '
                               'revised endpoint')
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        base = ctx[active]
        case.observe('field writer: ' + active + ' (' + base + ')')

        # The operator state the carryover must name: one applied
        # write to the run's writable bool point, plus the audit
        # positions the roll must continue — the active's model
        # fingerprint, its receipt log, and the field's outputs.
        _, signals = http_json('GET', base + '/signals')
        target = _writable_bool_point(signals)
        if target is None:
            return case.finish('inconclusive',
                               'no writable bool point in the model')
        point = target['point']
        status, receipt = http_json(
            'POST', base + '/command',
            {'command': {'write_value': {
                'point': point, 'kind': 'bool',
                'value': {'bool': True}}},
             'actor': 'qa-lane'})
        if status != 200:
            return case.finish('failed', 'the pre-roll command '
                               'refused: ' + str(receipt))
        applied = wait_for(
            lambda: _point_value(_try_snapshot(ctx, base) or {}, point)
            is True or None, time.monotonic() + 30)
        if not applied:
            return case.finish('failed', 'the pre-roll write never '
                               'applied at point ' + str(point))
        _, checkpoint = http_json('GET', base + '/checkpoint')
        from_fp = checkpoint.get('model_fingerprint')
        if from_fp is None:
            return case.finish('failed', 'the active peer serves no '
                               'model fingerprint')
        _, body = http_json('GET', base + '/receipts')
        receipts0 = _receipt_list(body)
        commands0 = [(r.get('command'), r.get('actor'))
                     for r in receipts0]
        demoted_health0 = (_try_snapshot(ctx, base) or {}) \
            .get('io_health') or {}
        try:
            field_points = _field_out_points(ctx)
        except Exception as exc:
            return case.finish('inconclusive', 'the simulated plant '
                               'is unreachable: ' + str(exc)[:200])
        if not field_points:
            return case.finish('inconclusive', 'the simulated plant '
                               'serves no field output to watch')
        watch = min(field_points)
        ref = save_evidence(
            ctx['evidence_dir'], 'model-revision-before.json',
            {'active': active, 'model_fingerprint': from_fp,
             'tick': checkpoint.get('tick'),
             'receipts': len(receipts0),
             'field': {str(p): _field_sample(ctx, p)
                       for p in field_points}})
        case.evidence('file', ref, 'the pre-roll audit positions')
        case.observe('pre-roll: fingerprint ' + str(from_fp) + ', '
                     + str(len(receipts0)) + ' receipts, watching '
                     'field point ' + str(watch))

        # The runner-owned action: derive the revised document through
        # the checked-in recipe and launch the third labeled controller
        # on it as --standby <active> --revised.
        try:
            info = start(active)
        except Exception as exc:
            return case.finish('inconclusive', 'the model-revision '
                               'action never completed: '
                               + str(exc)[:300])
        added = info.get('added_points') or []
        case.observe('revised peer ' + str(info.get('container'))
                     + ' launched; the recipe added points '
                     + str(added))
        document = json.loads(Path(info['document']).read_text())
        ref = save_evidence(ctx['evidence_dir'],
                            'model-revision-document.json', document)
        case.evidence('file', ref, 'the recipe-derived revised model '
                      'document')

        # Convergence: the revised peer must report the named
        # reinitialized state — a foreign-fingerprint checkpoint
        # applied through the carryover rule. A same-model `tracking`
        # or a `diverged` report is an outright contract violation; a
        # transient `degraded` is a retryable pull failure that only
        # fails the case when it persists to the deadline; and a peer
        # still unsynchronized then never converged — inconclusive.
        last = {}

        def converged():
            role = _try_role(ctx, revised)
            if role is None:
                return None
            last['role'] = role
            sync = role.get('sync')
            if isinstance(sync, dict) and set(sync) & {
                    'reinitialized', 'diverged', 'tracking'}:
                return role
            return None

        settled = wait_for(converged,
                           time.monotonic() + REVISION_CONVERGE_DEADLINE,
                           interval=REVISION_POLL)
        ref = save_evidence(ctx['evidence_dir'],
                            'model-revision-role.json',
                            last.get('role') or {})
        case.evidence('file', ref, 'the revised peer\'s convergence')
        if settled is None:
            sync = (last.get('role') or {}).get('sync')
            if isinstance(sync, dict) and 'degraded' in sync:
                return case.finish('failed', 'the revised peer never '
                                   'converged — its pulls stay '
                                   'degraded: '
                                   + json.dumps(sync)[:300])
            return case.finish('inconclusive', 'the revised peer never '
                               'converged: ' + json.dumps(sync)[:300])
        sync = settled.get('sync') or {}
        if 'reinitialized' not in sync:
            return case.finish('failed', 'the revised peer did not '
                               'converge as reinitialized: '
                               + json.dumps(sync)[:400])
        report = sync['reinitialized'].get('report') or {}
        ref = save_evidence(ctx['evidence_dir'],
                            'model-revision-carryover.json', report)
        case.evidence('file', ref, 'the carryover report')
        to_fp = report.get('to')
        if report.get('from') != from_fp or not to_fp \
                or to_fp == from_fp:
            return case.finish('failed', 'the carryover report does '
                               'not name the mounted and revised '
                               'fingerprints: ' + json.dumps(
                                   {'from': report.get('from'),
                                    'to': to_fp,
                                    'mounted': from_fp}))
        carried = report.get('carried') or []
        if not any(c.get('point') == point
                   and c.get('value') == {'bool': True}
                   for c in carried):
            return case.finish('failed', 'the carryover report does '
                               'not name the carried operator write at '
                               'point ' + str(point) + ': carried '
                               + json.dumps(carried)[:400])
        initialized = report.get('initialized') or []
        missing = [p for p in added if p not in initialized]
        if missing:
            return case.finish('failed', 'the recipe\'s added points '
                               'never initialized: ' + str(missing))
        if not report.get('reinitialized'):
            return case.finish('failed', 'the carryover report '
                               'reinitialized no components')
        case.observe('reinitialized at tick '
                     + str(report.get('resumed_at')) + ': '
                     + str(len(carried)) + ' carried, '
                     + str(len(initialized)) + ' initialized, '
                     + str(len(report.get('reinitialized') or []))
                     + ' components reinitialized, '
                     + str(len(report.get('reverted_tuning') or []))
                     + ' tuned parameters reverted')

        # The documented order: demote the field's owner first — its
        # write gate closes at the request's scan boundary — then
        # promote the reinitialized peer. The writer-less window must
        # hold the field's last write exactly.
        try:
            status, body = http_json('POST', base + '/demote')
        except urllib.error.HTTPError as exc:
            return case.finish('failed', 'demote refused: HTTP '
                               + str(exc.code))
        case.observe('demote ' + active + ': ' + str(status) + ' '
                     + json.dumps(body)[:200])
        if status != 200:
            return case.finish('failed', 'demote refused: '
                               + str(body))
        held = _field_sample(ctx, watch)
        regressions = []

        def demoted_settled():
            sample = _field_sample(ctx, watch)
            if held is not None and sample is not None \
                    and sample != held:
                regressions.append(sample)
            role = _try_role(ctx, base)
            return role if role and role.get('role') == 'standby' \
                else None

        demoted = wait_for(demoted_settled,
                           time.monotonic() + REVISION_SETTLE_DEADLINE,
                           interval=REVISION_POLL)
        if regressions:
            return case.finish('failed', 'the field moved during the '
                               'writer-less window after demote: '
                               + json.dumps(regressions[:3])[:400])
        if not demoted:
            return case.finish('failed', 'the demoted peer never '
                               'settled standby')
        try:
            status, body = http_json('POST', revised + '/promote')
        except urllib.error.HTTPError as exc:
            return case.finish('failed', 'promote refused: HTTP '
                               + str(exc.code))
        case.observe('promote revised: ' + str(status) + ' '
                     + json.dumps(body)[:200])
        if status != 200:
            return case.finish('failed', 'promote refused: '
                               + str(body))
        promoted = wait_for(
            lambda: (r.get('role') == 'active' and r or None)
            if (r := _try_role(ctx, revised)) else None,
            time.monotonic() + REVISION_SETTLE_DEADLINE,
            interval=REVISION_POLL)
        if not promoted:
            return case.finish('failed', 'the revised peer did not '
                               'settle active')

        # The promoted peer scans on the revised fingerprint — the
        # boundary's `to` — and the run ends there.
        _, after = http_json('GET', revised + '/checkpoint')
        if after.get('model_fingerprint') != to_fp:
            return case.finish('failed', 'the promoted peer does not '
                               'run the revised fingerprint: '
                               + str(after.get('model_fingerprint'))
                               + ' != ' + str(to_fp))
        first = _snapshot(ctx, revised)
        grown = wait_for(
            lambda: (s.get('tick', 0) > first.get('tick', 0)
                     and s or None)
            if (s := _try_snapshot(ctx, revised)) else None,
            time.monotonic() + 30)
        if not grown:
            return case.finish('failed', 'the promoted peer\'s '
                               'telemetry did not advance')

        # Bumpless writes: every field read through the post-roll
        # window carries the promoted peer's staged image (allowing
        # one scan of observation lag), and the demoted peer's write
        # gate holds — a demoted peer still attempting writes meets
        # the plant's fence, which counts the rejections in its
        # io_health.
        baseline = demoted_health0.get('failed_writes') or 0
        trace = []
        consecutive = 0
        quiesce_violation = None
        for _ in range(REVISION_FIELD_ROUNDS):
            promoted_snap = _try_snapshot(ctx, revised) or {}
            demoted_snap = _try_snapshot(ctx, base) or {}
            sample = _field_sample(ctx, watch)
            staged = _point_value(promoted_snap, watch)
            value = (sample or {}).get('value')
            if isinstance(value, dict):
                value = next(iter(value.values()), None)
            health = demoted_snap.get('io_health') or {}
            trace.append({'field': value,
                          'promoted_staged': staged,
                          'demoted_staged': _point_value(demoted_snap,
                                                         watch),
                          'demoted_failed_writes':
                              health.get('failed_writes')})
            if (health.get('failed_writes') or 0) > baseline \
                    or health.get('last_error'):
                quiesce_violation = health
            if value is not None and staged is not None \
                    and value != staged:
                consecutive += 1
            else:
                consecutive = 0
            time.sleep(REVISION_POLL)
        ref = save_evidence(ctx['evidence_dir'],
                            'model-revision-field.json',
                            {'watch': watch, 'held': held,
                             'trace': trace})
        case.evidence('file', ref, 'field reads across the roll')
        if consecutive >= 2:
            return case.finish('failed', 'the field regressed across '
                               'the roll — field reads do not follow '
                               'the promoted peer\'s image: '
                               + json.dumps(trace[-3:])[:400])
        if quiesce_violation is not None:
            return case.finish('failed', 'the demoted peer kept '
                               'serving writes — the plant fenced '
                               'them: '
                               + json.dumps(quiesce_violation)[:300])

        # The audit trail crosses the boundary verbatim: the promoted
        # peer's receipt log opens with the old run's receipts in
        # order, and new submissions continue the sequence.
        _, body = http_json('GET', revised + '/receipts')
        receipts1 = _receipt_list(body)
        commands1 = [(r.get('command'), r.get('actor'))
                     for r in receipts1]
        if commands1[:len(commands0)] != commands0:
            return case.finish('failed', 'the receipt log did not '
                               'carry across the roll: '
                               + str(len(commands0)) + ' pre-roll '
                               'commands vs '
                               + json.dumps(commands1[:len(commands0)
                                                     + 1])[:300])
        status, receipt = http_json(
            'POST', revised + '/command',
            {'command': {'write_value': {
                'point': point, 'kind': 'bool',
                'value': {'bool': False}}},
             'actor': 'qa-lane'})
        if status != 200:
            return case.finish('failed', 'the post-roll command '
                               'refused: ' + str(receipt))
        _, body = http_json('GET', revised + '/receipts')
        receipts2 = _receipt_list(body)
        if len(receipts2) <= len(receipts1):
            return case.finish('failed', 'the post-roll command did '
                               'not extend the receipt log')

        # The durable journal files: the revised peer's file records
        # the crossing (the reinitialized entry) and its promotion in
        # its one lifetime's continuing seqs; the demoted peer's file
        # keeps appending continuing seqs through the demotion — the
        # roll never restarts a process.
        journals = ctx.get('journal_files') or {}
        parsed = {}

        def journals_ready():
            try:
                parsed['revised'] = _journal_entries(
                    journals['revised'])
                parsed['demoted'] = _journal_entries(
                    journals[active])
            except (KeyError, OSError, ValueError) as exc:
                parsed['error'] = str(exc)
                return None
            parsed.pop('error', None)
            crossed = any('reinitialized' in
                          ((r.get('entry') or {}).get('event') or {})
                          for r in parsed['revised'])
            settled_down = any(
                ((r.get('entry') or {}).get('event') or {})
                .get('role_changed', {}).get('to') == 'standby'
                for r in parsed['demoted'])
            return parsed if crossed and settled_down else None

        ready = wait_for(journals_ready,
                         time.monotonic() + RESTART_JOURNAL_DEADLINE,
                         interval=REVISION_POLL)
        ref = save_evidence(ctx['evidence_dir'],
                            'model-revision-journals.json',
                            {'revised': parsed.get('revised'),
                             'demoted': parsed.get('demoted'),
                             'error': parsed.get('error')})
        case.evidence('file', ref, 'the journal files across the roll')
        if parsed.get('error'):
            return case.finish('inconclusive', 'the journal files are '
                               'unreadable: ' + str(parsed['error']))
        if not ready:
            return case.finish('failed', 'the journal files did not '
                               'record the crossing and the demotion')
        for name in ('revised', 'demoted'):
            seqs = [(r.get('entry') or {}).get('seq')
                    for r in parsed[name] if 'entry' in r]
            bounds = [r['run_boundary'] for r in parsed[name]
                      if 'run_boundary' in r]
            if len(bounds) != 1:
                return case.finish('failed', name + ' journal file '
                                   'holds ' + str(len(bounds))
                                   + ' lifetimes — the roll must not '
                                   'restart a process')
            if not seqs or any(not isinstance(s, int) for s in seqs) \
                    or seqs != sorted(seqs) \
                    or len(set(seqs)) != len(seqs):
                return case.finish('failed', 'journal seqs do not '
                                   'continue across the roll on '
                                   + name + ': ' + str(seqs[:20]))

        # The run ends on the revised fingerprint: the field writer is
        # the revised peer, the demoted peer a settled standby.
        roles = {name: _try_role(ctx, ctx[name])
                 for name in (active, 'revised')}
        ref = save_evidence(ctx['evidence_dir'],
                            'model-revision-after.json',
                            {'roles': roles,
                             'model_fingerprint':
                                 after.get('model_fingerprint'),
                             'receipts': len(receipts2)})
        case.evidence('file', ref, 'the post-roll pair state')
        if (roles.get('revised') or {}).get('role') != 'active':
            return case.finish('failed', 'the revised peer did not '
                               'stay active')
        if (roles.get(active) or {}).get('role') != 'standby':
            return case.finish('failed', 'the demoted peer did not '
                               'stay standby')
        case.observe('rolled: ' + active + ' demoted, revised peer '
                     'active on fingerprint ' + str(to_fp))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))


# The rolling-revision refusal half: WW-LCM-001's deployment-update
# clause requires the carryover rule to refuse a revision that retypes
# a carried point — never silently loading the incompatible document.
# The case derives the recipe's revised document exactly as the
# compatible roll does, then applies the checked-in post-derivation
# step (qa_lane/revision-incompatible.json) that retypes one carried
# writable internal point bool->int, so every pulled checkpoint fails
# with the named InternalKindMismatch and the peer settles degraded —
# distinguishably from the foreign-fingerprint degrade an unarmed
# standby reports — refuses POST /promote with the not_converged
# SwitchError carrying the same detail, and leaves the field writer
# undisturbed. The control half relaunches the same slot on the same
# document minus the retype and must converge reinitialized — proving
# the refusal names the carryover violation rather than a rig defect.
# The scenario sits immediately ahead of scenario_model_revision in
# the schedule: it never promotes, so the field writer is unchanged
# and the compatible case behind it relaunches the third slot and
# performs the control's promote leg in the same run.


def scenario_incompatible_revision(ctx):
    """A --revised peer on a carryover-breaking document is refused."""
    case = Case('incompatible-revision',
                'Incompatible model revision meets the named refusal',
                'a third controller launched --standby <active> '
                '--revised on the recipe-derived document plus the '
                'checked-in incompatible retype settles standby '
                'reporting sync degraded with the detail naming the '
                'carryover refusal and the retyped point — not the '
                'foreign-fingerprint degrade — POST /promote answers '
                '409 not_converged carrying the same degraded detail, '
                'the active\'s field writes, receipts, and journal '
                'stay undisturbed across the observation window, and '
                'the same document minus the retype converges '
                'reinitialized as the control half')
    try:
        start = ctx.get('start_revised')
        revised = ctx.get('revised')
        if start is None or revised is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no model-revision action or '
                               'revised endpoint')
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        if active not in ('active', 'standby'):
            return case.finish('inconclusive', 'the field writer is '
                               'already the revised peer — the '
                               'incompatible variant has no pair '
                               'member to stand by on')
        base = ctx[active]
        case.observe('field writer: ' + active + ' (' + base + ')')

        # The audit positions the refusal must leave untouched: the
        # active's receipt log, its durable journal file, and one
        # field output its scan keeps writing.
        _, body = http_json('GET', base + '/receipts')
        receipts0 = _receipt_list(body)
        commands0 = [(r.get('command'), r.get('actor'))
                     for r in receipts0]
        journals = ctx.get('journal_files') or {}
        journal_path = journals.get(active)
        journal0 = None
        if journal_path:
            try:
                journal0 = _journal_entries(journal_path)
            except (OSError, ValueError) as exc:
                return case.finish('inconclusive', 'the active\'s '
                                   'journal file is unreadable: '
                                   + str(exc))
        try:
            field_points = _field_out_points(ctx)
        except Exception as exc:
            return case.finish('inconclusive', 'the simulated plant '
                               'is unreachable: ' + str(exc)[:200])
        if not field_points:
            return case.finish('inconclusive', 'the simulated plant '
                               'serves no field output to watch')
        watch = min(field_points)
        ref = save_evidence(
            ctx['evidence_dir'], 'incompatible-revision-before.json',
            {'active': active, 'receipts': len(receipts0),
             'journal_records': len(journal0 or []),
             'field': {str(p): _field_sample(ctx, p)
                       for p in field_points}})
        case.evidence('file', ref, 'the pre-refusal audit positions')

        # The runner-owned action on the refusal half: the additive
        # recipe derives the revised document first, then the
        # checked-in step retypes a carried point so the carryover
        # crossing — not the document's load — is what fails.
        try:
            info = start(active, incompatible=True)
        except Exception as exc:
            return case.finish('inconclusive', 'the incompatible '
                               'model-revision action never '
                               'completed: ' + str(exc)[:300])
        retyped = info.get('retyped_point')
        if not isinstance(retyped, int):
            return case.finish('inconclusive', 'the incompatible '
                               'derivation did not name the retyped '
                               'point: ' + json.dumps(info)[:200])
        case.observe('revised peer ' + str(info.get('container'))
                     + ' launched on the incompatible document — '
                     'retyped point ' + str(retyped))
        document = json.loads(Path(info['document']).read_text())
        ref = save_evidence(ctx['evidence_dir'],
                            'incompatible-revision-document.json',
                            document)
        case.evidence('file', ref, 'the incompatible derived model '
                      'document')

        # The refusal: every pulled checkpoint crosses into the
        # carryover rule and fails it, so the peer settles degraded
        # permanently — not the transient degrade of a fetch failure —
        # with the detail naming the carryover error and the retyped
        # point. A `reinitialized` report here would mean the
        # incompatible document silently crossed: the contract break
        # this case exists to catch.
        last = {}

        def refusal_report():
            role = _try_role(ctx, revised)
            if role is None:
                return None
            last['role'] = role
            sync = role.get('sync')
            if not isinstance(sync, dict):
                return None
            if 'reinitialized' in sync:
                return role
            detail = str((sync.get('degraded') or {}).get('detail'))
            if 'internal point ' + str(retyped) in detail \
                    and 'retype must rename' in detail:
                return role
            return None

        settled = wait_for(refusal_report,
                           time.monotonic() + REVISION_CONVERGE_DEADLINE,
                           interval=REVISION_POLL)
        ref = save_evidence(ctx['evidence_dir'],
                            'incompatible-revision-role.json',
                            last.get('role') or {})
        case.evidence('file', ref, 'the incompatible peer\'s role '
                      'report')
        sync = ((settled or last.get('role') or {}).get('sync'))
        if isinstance(sync, dict) and 'reinitialized' in sync:
            return case.finish('failed', 'the incompatible document '
                               'converged reinitialized — the '
                               'carryover rule did not refuse the '
                               'retype of point ' + str(retyped))
        if settled is None:
            if isinstance(sync, dict) and 'degraded' in sync:
                return case.finish('failed', 'the peer degraded but '
                                   'not on the named carryover '
                                   'refusal: ' + json.dumps(sync)[:300])
            return case.finish('inconclusive', 'the incompatible '
                               'peer never reported a checkpoint '
                               'crossing: ' + json.dumps(sync)[:300])
        detail = str((sync.get('degraded') or {}).get('detail'))
        if 'fingerprint' in detail:
            return case.finish('failed', 'the degrade is a '
                               'fingerprint rejection, not the '
                               'carryover refusal: ' + detail[:300])
        if settled.get('role') != 'standby':
            return case.finish('failed', 'the refused peer reports '
                               'role ' + str(settled.get('role')))
        case.observe('degraded on the named refusal: ' + detail)

        # The named refusal on the switch path: POST /promote meets
        # the degraded peer's convergence gate — HTTP 409 carrying the
        # not_converged SwitchError whose embedded sync repeats the
        # carryover detail.
        try:
            status, refusal = http_json('POST', revised + '/promote')
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                refusal = json.loads(exc.read() or b'null')
            except ValueError:
                refusal = None
            finally:
                exc.close()
        ref = save_evidence(ctx['evidence_dir'],
                            'incompatible-revision-refusal.json',
                            {'status': status, 'body': refusal})
        case.evidence('file', ref, 'the refused promotion')
        if status != 409:
            return case.finish('failed', 'POST /promote answered '
                               + str(status) + ' — the degraded peer '
                               'must refuse with 409: '
                               + json.dumps(refusal)[:300])
        refusal_sync = (((refusal or {}).get('not_converged') or {})
                        .get('sync'))
        refusal_detail = str((refusal_sync.get('degraded') or {})
                             .get('detail')) \
            if isinstance(refusal_sync, dict) else ''
        if 'internal point ' + str(retyped) not in refusal_detail \
                or 'retype must rename' not in refusal_detail:
            return case.finish('failed', 'the promotion refusal does '
                               'not carry the named carryover '
                               'failure: ' + json.dumps(refusal)[:400])
        case.observe('promotion refused: ' + refusal_detail)

        # The undisturbed active: across the observation window the
        # field keeps following the active's staged image (one scan of
        # lag allowed), the receipt log keeps its exact contents, and
        # the durable journal file records nothing new — the refused
        # peer never wrote, never promoted, never touched the run's
        # audit trail.
        trace = []
        consecutive = 0
        for _ in range(REVISION_FIELD_ROUNDS):
            sample = _field_sample(ctx, watch)
            staged = _point_value(_try_snapshot(ctx, base) or {},
                                  watch)
            value = (sample or {}).get('value')
            if isinstance(value, dict):
                value = next(iter(value.values()), None)
            trace.append({'field': value, 'active_staged': staged})
            if value is not None and staged is not None \
                    and value != staged:
                consecutive += 1
            else:
                consecutive = 0
            time.sleep(REVISION_POLL)
        _, body = http_json('GET', base + '/receipts')
        receipts1 = _receipt_list(body)
        commands1 = [(r.get('command'), r.get('actor'))
                     for r in receipts1]
        journal1 = None
        journal_error = None
        if journal_path:
            try:
                journal1 = _journal_entries(journal_path)
            except (OSError, ValueError) as exc:
                journal_error = str(exc)
        ref = save_evidence(
            ctx['evidence_dir'], 'incompatible-revision-field.json',
            {'watch': watch, 'trace': trace,
             'receipts': [len(receipts0), len(receipts1)],
             'journal_records': [len(journal0 or []),
                                 None if journal1 is None
                                 else len(journal1)]})
        case.evidence('file', ref, 'the active across the refusal '
                      'window')
        if consecutive >= 2:
            return case.finish('failed', 'the field stopped following '
                               'the still-active peer\'s image during '
                               'the refusal: '
                               + json.dumps(trace[-3:])[:400])
        if commands1 != commands0:
            return case.finish('failed', 'the active\'s receipt log '
                               'changed across the refusal window: '
                               + str(len(receipts0)) + ' -> '
                               + str(len(receipts1)))
        if journal_error:
            return case.finish('inconclusive', 'the active\'s '
                               'journal file turned unreadable: '
                               + journal_error)
        if journal0 is not None and journal1 != journal0:
            return case.finish('failed', 'the active\'s journal file '
                               'gained records across the refusal '
                               'window: ' + str(len(journal0)) + ' -> '
                               + str(len(journal1)))

        # The control half: the same recipe-derived document minus the
        # retype must converge reinitialized — the refusal above named
        # the carryover violation, not a rig defect. The relaunch also
        # exercises the action's third-slot replacement over the
        # degraded peer. The promote leg belongs to the sibling
        # model-revision case scheduled immediately behind, which
        # relaunches the slot and performs the demote-then-promote.
        try:
            control = start(active)
        except Exception as exc:
            return case.finish('inconclusive', 'the control '
                               'relaunch never completed: '
                               + str(exc)[:300])
        document = json.loads(Path(control['document']).read_text())
        ref = save_evidence(ctx['evidence_dir'],
                            'incompatible-revision-control.json',
                            document)
        case.evidence('file', ref, 'the control document — the same '
                      'revision minus the retype')
        last.clear()

        def control_converged():
            role = _try_role(ctx, revised)
            if role is None:
                return None
            last['role'] = role
            sync = role.get('sync')
            if isinstance(sync, dict) and 'reinitialized' in sync:
                return role
            return None

        converged = wait_for(control_converged,
                             time.monotonic()
                             + REVISION_CONVERGE_DEADLINE,
                             interval=REVISION_POLL)
        ref = save_evidence(ctx['evidence_dir'],
                            'incompatible-revision-control-role.json',
                            last.get('role') or {})
        case.evidence('file', ref, 'the control peer\'s convergence')
        if converged is None:
            sync = (last.get('role') or {}).get('sync')
            return case.finish('failed', 'the control half did not '
                               'converge reinitialized — the refusal '
                               'may name a rig defect rather than the '
                               'retype: ' + json.dumps(sync)[:300])
        report = ((converged.get('sync') or {})
                  .get('reinitialized') or {}).get('report') or {}
        carried = report.get('carried') or []
        if not any(c.get('point') == retyped
                   and isinstance(c.get('value'), dict)
                   and set(c['value']) == {'bool'}
                   for c in carried):
            return case.finish('failed', 'the control crossing did '
                               'not carry the retyped point as its '
                               'old kind: carried '
                               + json.dumps(carried)[:300])
        case.observe('control half converged reinitialized — point '
                     + str(retyped) + ' carried under its declared '
                     'bool kind')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))


# --------------------------------------------------------------------
# The named rejection of incompatible state — WW-LCM-001's
# checkpoint-negotiation clause: a third controller launched
# --standby <active> on a document whose model fingerprint differs
# from the pair's (the recipe-derived revised model WITHOUT the
# --revised opt-in) must never converge — every pulled checkpoint meets
# the fingerprint gate's named refusal, the peer reports degraded for
# the observation window, and POST /promote answers the named
# not_converged refusal — while the active peer's ticks, field writes,
# and receipt log continue undisturbed. The case then removes the
# foreign container so later cases — the model-revision launch above
# all — see a clean rig. It runs while the pair still runs the mounted
# model: after the revision roll the same document would no longer be
# foreign.

NEGOTIATION_DEADLINE = 60   # bound on the named degraded report
NEGOTIATION_ROUNDS = 6      # observation-window polls once degraded
NEGOTIATION_POLL = 0.5      # cadence watching the foreign peer


def scenario_checkpoint_negotiation(ctx):
    """A foreign-fingerprint standby never converges and refuses
    promotion."""
    case = Case('checkpoint-negotiation',
                'Foreign-model standby refuses checkpoint negotiation',
                'a third controller launched --standby <active> on the '
                'recipe-derived foreign-fingerprint document without '
                '--revised reports the named degraded negotiation '
                'failure for the observation window, POST /promote '
                'answers the named not_converged refusal, the active '
                'peer\'s ticks, field writes, and receipts continue '
                'undisturbed, and the case removes the foreign '
                'container afterward')
    start = ctx.get('start_foreign')
    stop = ctx.get('stop_foreign')
    foreign = ctx.get('foreign')
    if start is None or stop is None or foreign is None:
        return case.finish('inconclusive', 'the run context carries '
                           'no checkpoint-negotiation launch action, '
                           'teardown action, or foreign endpoint')
    try:
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        base = ctx[active]
        case.observe('field writer: ' + active + ' (' + base + ')')

        # The audit positions the refusal must leave untouched: the
        # pair's model fingerprint, the active's advancing tick, its
        # receipt log, and the field output it keeps writing.
        _, checkpoint = http_json('GET', base + '/checkpoint')
        pair_fp = checkpoint.get('model_fingerprint')
        if pair_fp is None:
            return case.finish('failed', 'the active peer serves no '
                               'model fingerprint')
        tick0 = (_try_snapshot(ctx, base) or {}).get('tick')
        _, body = http_json('GET', base + '/receipts')
        commands0 = [(r.get('command'), r.get('actor'))
                     for r in _receipt_list(body)]
        try:
            field_points = _field_out_points(ctx)
        except Exception as exc:
            return case.finish('inconclusive', 'the simulated plant '
                               'is unreachable: ' + str(exc)[:200])
        if not field_points:
            return case.finish('inconclusive', 'the simulated plant '
                               'serves no field output to watch')
        watch = min(field_points)
        ref = save_evidence(
            ctx['evidence_dir'], 'negotiation-before.json',
            {'active': active, 'model_fingerprint': pair_fp,
             'tick': tick0, 'receipts': len(commands0),
             'field': {str(p): _field_sample(ctx, p)
                       for p in field_points}})
        case.evidence('file', ref, 'the pre-launch audit positions')
        case.observe('baseline: fingerprint ' + str(pair_fp)
                     + ', tick ' + str(tick0) + ', '
                     + str(len(commands0)) + ' receipts, watching '
                     'field point ' + str(watch))

        # The runner-owned action: derive the foreign document through
        # the checked-in recipe and launch the third labeled controller
        # on it as --standby <active> without --revised, so its
        # fingerprint gate refuses every checkpoint it pulls.
        try:
            info = start(active)
        except Exception as exc:
            return case.finish('inconclusive', 'the checkpoint-'
                               'negotiation action never completed: '
                               + str(exc)[:300])
        container = str(info.get('container'))
        case.observe('foreign peer ' + container + ' launched on the '
                     'derived document without --revised')

        def attempt():
            """Everything the case asserts while the foreign peer is
            up — the refusal report, the observation window, the
            promote refusal, and the undisturbed-active checks."""
            document = json.loads(Path(info['document']).read_text())
            ref = save_evidence(ctx['evidence_dir'],
                                'negotiation-document.json', document)
            case.evidence('file', ref, 'the foreign-fingerprint model '
                          'document')

            # The named negotiation refusal: the foreign peer's pulls
            # land — the active serves — but every apply meets the
            # fingerprint gate, so the peer reports degraded naming the
            # mismatch and never converges. A converged report is an
            # outright contract violation; a peer still unsynchronized
            # or fetch-failing at the deadline never exercised the
            # negotiation — inconclusive.
            last = {}

            def verdict():
                role = _try_role(ctx, foreign)
                if role is None:
                    return None
                last['role'] = role
                sync = role.get('sync')
                if not isinstance(sync, dict):
                    return None
                if set(sync) & {'tracking', 'reinitialized',
                                'diverged'}:
                    return role
                detail = (sync.get('degraded') or {}).get('detail')
                if isinstance(detail, str) and 'fingerprint' in detail:
                    return role
                return None

            settled = wait_for(verdict,
                               time.monotonic() + NEGOTIATION_DEADLINE,
                               interval=NEGOTIATION_POLL)
            ref = save_evidence(ctx['evidence_dir'],
                                'negotiation-role.json',
                                last.get('role') or {})
            case.evidence('file', ref, 'the foreign peer\'s '
                          'negotiation state report')
            if settled is None:
                sync = (last.get('role') or {}).get('sync')
                detail = ((sync or {}).get('degraded') or {}) \
                    .get('detail') if isinstance(sync, dict) else None
                if isinstance(detail, str) and detail \
                        and 'fetch' not in detail:
                    return case.finish('failed', 'the foreign peer\'s '
                                       'refusal never named the '
                                       'fingerprint negotiation: '
                                       + detail[:300])
                return case.finish('inconclusive', 'the foreign peer '
                                   'never reached a negotiation '
                                   'verdict: ' + json.dumps(sync)[:300])
            sync = settled.get('sync') or {}
            if 'degraded' not in sync:
                return case.finish('failed', 'the foreign-fingerprint '
                                   'peer reports a converged state — '
                                   'the negotiation was not refused: '
                                   + json.dumps(sync)[:400])
            detail = str(sync['degraded'].get('detail'))
            case.observe('negotiation refused: ' + detail[:200])
            if isinstance(pair_fp, int) \
                    and format(pair_fp, '016x') not in detail:
                return case.finish('failed', 'the degraded report does '
                                   'not name the pair\'s fingerprint '
                                   + format(pair_fp, '016x') + ': '
                                   + detail[:300])

            # The observation window: the refusal must hold — every
            # poll keeps reporting standby+degraded — while the active
            # peer's tick keeps advancing and its field writes keep
            # landing.
            window = []
            last_tick = tick0
            field_tick = None
            mismatch = 0
            violation = None
            for _ in range(NEGOTIATION_ROUNDS):
                role = _try_role(ctx, foreign)
                snap = _try_snapshot(ctx, base)
                sample = _field_sample(ctx, watch)
                staged = _point_value(snap or {}, watch)
                tick = (snap or {}).get('tick')
                value = (sample or {}).get('value')
                if isinstance(value, dict):
                    value = next(iter(value.values()), None)
                ftick = (sample or {}).get('tick')
                window.append({'role': role, 'tick': tick,
                               'staged': staged, 'field': value,
                               'field_tick': ftick})
                sync = (role or {}).get('sync')
                if not (isinstance(role, dict)
                        and role.get('role') == 'standby'
                        and isinstance(sync, dict)
                        and 'degraded' in sync):
                    violation = ('the foreign peer left the refused '
                                 'state mid-window: '
                                 + json.dumps(role)[:300])
                if tick is not None and last_tick is not None \
                        and tick <= last_tick:
                    violation = ('the active peer\'s tick stalled at '
                                 + str(tick))
                if tick is not None:
                    last_tick = tick
                if ftick is not None and field_tick is not None \
                        and ftick <= field_tick:
                    violation = ('the field stopped receiving the '
                                 'active\'s writes at tick '
                                 + str(ftick))
                if ftick is not None:
                    field_tick = ftick
                if value is not None and staged is not None \
                        and value != staged:
                    mismatch += 1
                else:
                    mismatch = 0
                if violation:
                    break
                time.sleep(NEGOTIATION_POLL)
            ref = save_evidence(ctx['evidence_dir'],
                                'negotiation-window.json',
                                {'watch': watch, 'window': window})
            case.evidence('file', ref, 'the observation window: '
                          'foreign reports, active ticks, field reads')
            if violation:
                return case.finish('failed', violation)
            if mismatch >= 2:
                return case.finish('failed', 'the active peer\'s field '
                                   'writes stopped landing during the '
                                   'attempt: '
                                   + json.dumps(window[-3:])[:400])
            if not any(isinstance(w.get('role'), dict)
                       for w in window):
                return case.finish('inconclusive', 'the observation '
                                   'window saw no foreign report')
            case.observe('the refusal held across '
                         + str(len(window)) + ' polls; the active '
                         'peer\'s tick reached ' + str(last_tick))

            # The promotion gate: a never-converged peer must answer
            # the named not_converged refusal carrying its degraded
            # negotiation state — never a promotion.
            try:
                status, refusal = http_json('POST',
                                            foreign + '/promote')
            except urllib.error.HTTPError as exc:
                status = exc.code
                try:
                    refusal = json.loads(exc.read() or b'null')
                except ValueError:
                    refusal = None
                finally:
                    exc.close()
            ref = save_evidence(ctx['evidence_dir'],
                                'negotiation-refusal.json',
                                {'status': status, 'body': refusal})
            case.evidence('file', ref, 'POST /promote\'s answer')
            named = refusal.get('not_converged') \
                if isinstance(refusal, dict) else None
            if status != 409 or not isinstance(named, dict):
                return case.finish('failed', 'promote did not answer '
                                   'the named not_converged refusal: '
                                   'HTTP ' + str(status) + ' '
                                   + json.dumps(refusal)[:300])
            refused_sync = named.get('sync')
            if not isinstance(refused_sync, dict) \
                    or 'degraded' not in refused_sync:
                return case.finish('failed', 'the not_converged '
                                   'refusal does not carry the '
                                   'degraded negotiation state: '
                                   + json.dumps(refusal)[:300])
            case.observe('promote refused not_converged carrying '
                         + json.dumps(refused_sync)[:200])

            # The attempt touched nothing: the receipt log is exactly
            # the baseline's, the active's tick kept advancing, and its
            # writes still reach the field.
            _, body = http_json('GET', base + '/receipts')
            commands1 = [(r.get('command'), r.get('actor'))
                         for r in _receipt_list(body)]
            snap1 = _try_snapshot(ctx, base) or {}
            sample1 = _field_sample(ctx, watch)
            ref = save_evidence(ctx['evidence_dir'],
                                'negotiation-after.json',
                                {'tick': snap1.get('tick'),
                                 'receipts': len(commands1),
                                 'field': sample1})
            case.evidence('file', ref, 'the post-attempt audit '
                          'positions')
            if commands1 != commands0:
                return case.finish('failed', 'the active peer\'s '
                                   'receipt log changed across the '
                                   'attempt')
            if tick0 is not None and snap1.get('tick') is not None \
                    and snap1['tick'] <= tick0:
                return case.finish('failed', 'the active peer\'s tick '
                                   'did not advance through the '
                                   'attempt')
            if sample1 is None:
                return case.finish('inconclusive', 'the field stopped '
                                   'answering after the attempt')
            return case.finish('passed')

        try:
            record = attempt()
        except Exception as exc:
            record = case.finish('inconclusive', str(exc))
        # Teardown is unconditional once the peer is up: later cases —
        # the model-revision launch above all — need a clean rig. A
        # teardown that fails leaves that rig dirty, so a case that
        # otherwise passed cannot claim the contract held end to end.
        try:
            stop()
            case.observe('foreign peer ' + container + ' removed — '
                         'later cases see a clean rig')
        except Exception as exc:
            case.observe('the foreign peer teardown failed: '
                         + str(exc)[:200])
            if record['outcome'] == 'passed':
                record['outcome'] = 'inconclusive'
                record['detail'] = ('the foreign peer was never '
                                    'removed: ' + str(exc)[:300])
        return record
    except Exception as exc:
        return case.finish('inconclusive', str(exc))


# The doomed-startup claim-ordering case's cadence: polls through the
# window a doomed foreign launch runs in — the abort lands inside the
# first polls, and a claim-then-died startup would surface as the
# incumbent demoted by a dead claim within the same window.
DOOMED_STARTUP_POLL = 0.5    # cadence watching the pair mid-attempt
DOOMED_STARTUP_ROUNDS = 10   # observation-window polls once launched
# The corrupt first record the case writes into the foreign peer's
# --journal-file before launch: valid JSON that is not a journal
# record, so the startup replay fails by name on line 1 and the bind —
# and with it any field claim — never happens.
DOOMED_CORRUPT_RECORD = ('{"qa-lane": "corrupt first record — '
                         'the doomed-startup induction"}')


def _probe_sample(ctx, point):
    """The plant's stored sample for `point` through the shipped
    tool's `read` — `{'value', 'quality', 'tick'}` — or None when the
    read dropped; a lost observation, never the leg's verdict."""
    body = _try_plant_ctl(ctx, 'read', str(point))
    if not isinstance(body, dict):
        return None
    return body.get('sample')


def _probe_error(response):
    """The named refusal a plant answer carries — 'fenced',
    'unclaimed' — or None on a success or malformed answer."""
    error = (response or {}).get('error')
    return error.get('kind') if isinstance(error, dict) else None


def scenario_doomed_startup_claim(ctx):
    """A foreign peer whose startup aborts on an unreplayable journal
    file never strands a claim fencing the incumbent."""
    case = Case('doomed-startup-claim',
                'A doomed startup never fences the incumbent',
                'with the pair settled and the active holding the '
                'field claim, a foreign peer launched onto a journal '
                'file whose first record cannot be replayed aborts '
                'before its preemptive claim can run — the incumbent '
                'keeps role=active with its tick, field writes, and '
                'receipted command path undisturbed, every third-party '
                'mutation probe stays fenced under the standing claim '
                '(never unclaimed, never silently writable), no peer '
                'reports a spurious role change, and the rig returns '
                'clean once the peer is removed')
    start = ctx.get('start_foreign')
    stop = ctx.get('stop_foreign')
    foreign = ctx.get('foreign')
    journal = (ctx.get('journal_files') or {}).get('foreign')
    state_file = (ctx.get('state_files') or {}).get('foreign')
    if start is None or stop is None or foreign is None \
            or journal is None or not ctx.get('plant'):
        return case.finish('inconclusive', 'the run context carries no '
                           'foreign-peer launch/teardown action, '
                           'endpoint, journal-file path, or plant '
                           'address')
    try:
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        if active not in ('active', 'standby'):
            return case.finish('inconclusive', 'the settled field '
                               'owner is not a pair peer the foreign '
                               'launch can stand by on: ' + str(active))
        peer = 'standby' if active == 'active' else 'active'
        base, peer_base = ctx[active], ctx[peer]
        expected = {active: 'active', peer: 'standby'}
        case.observe('field owner: ' + active + ' (' + base + ')')

        # The audit positions the doomed startup must leave untouched:
        # the incumbent's role and advancing tick, its receipted
        # command path, the standing claim's fencing verdict, and the
        # field output it keeps writing.
        _, signals = http_json('GET', base + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'doomed-startup-claim-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming the '
                      'receipted-path target')
        target = _writable_bool_point(signals)
        if target is None:
            return case.finish('inconclusive',
                               'no writable bool point in the model — '
                               'the incumbent\'s receipted path cannot '
                               'be probed')
        point = target['point']
        census = _try_plant_ctl(ctx, 'list')
        if census is None:
            return case.finish('inconclusive', 'the simulated plant '
                               'did not answer its point census')
        points = (census or {}).get('points') or []
        field_out = sorted(entry['point'] for entry in points
                           if isinstance(entry, dict)
                           and entry.get('direction') == 'out')
        if not field_out:
            return case.finish('inconclusive', 'the simulated plant '
                               'serves no field output to watch')
        watch = min(field_out)
        probe0 = _try_plant(ctx, {'op': 'step', 'dt': 0})
        if probe0 is None:
            return case.finish('inconclusive', 'the simulated plant '
                               'did not answer a fencing probe')
        if not _fenced(probe0):
            return case.finish('failed', 'the field held no writer '
                               'claim before the doomed launch — a '
                               'third attachment\'s mutation probe '
                               'answered ' + json.dumps(probe0)[:300])
        role0 = _try_role(ctx, base)
        peer_role0 = _try_role(ctx, peer_base)
        snap0 = _try_snapshot(ctx, base)
        if snap0 is None:
            return case.finish('inconclusive', 'the incumbent never '
                               'served a snapshot for the baseline')
        tick0 = snap0.get('tick')
        sample0 = _probe_sample(ctx, watch)
        _, body = http_json('GET', base + '/receipts')
        receipts0 = [(r.get('command'), r.get('actor'))
                     for r in _receipt_list(body)]
        ref = save_evidence(ctx['evidence_dir'],
                            'doomed-startup-claim-before.json',
                            {'active': active, 'tick': tick0,
                             'incumbent': role0, 'partner': peer_role0,
                             'receipts': len(receipts0),
                             'probe': probe0, 'watch': watch,
                             'field': sample0})
        case.evidence('file', ref, 'the pre-launch audit positions')
        case.observe('baseline: incumbent tick ' + str(tick0) + ', '
                     + str(len(receipts0)) + ' receipts, field point '
                     + str(watch) + ' fenced under its claim')

        # The induction: the foreign peer's --journal-file gains a
        # first record its startup replay cannot read. The file lives
        # in the runner-owned state/journal directory the launch
        # bind-mounts, so the corrupt record is the file's line 1 when
        # the container replays it.
        journal_path = Path(journal)
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        journal_path.write_text(DOOMED_CORRUPT_RECORD + '\n')
        case.observe('corrupt first record written to '
                     + str(journal_path))
        try:
            info = start(active)
        except Exception as exc:
            try:
                journal_path.unlink(missing_ok=True)
            except OSError:
                pass  # restore is best-effort; the launch never ran
            return case.finish('inconclusive', 'the foreign-peer '
                               'launch action never completed: '
                               + str(exc)[:300])
        container = str(info.get('container'))
        case.observe('foreign peer ' + container + ' launched onto '
                     'the corrupt journal file')

        command = {'command': {'write_value': {
            'point': point, 'kind': 'bool', 'value': {'bool': True}}},
            'actor': 'qa-lane'}

        def attempt():
            """Everything the case asserts across the doomed startup —
            the observation window over the pair's roles, the
            incumbent's tick and field writes, the fencing probes, the
            receipted command — and the post-attempt audit."""
            document = json.loads(Path(info['document']).read_text())
            ref = save_evidence(
                ctx['evidence_dir'],
                'doomed-startup-claim-induction.json',
                {'corrupt_record': DOOMED_CORRUPT_RECORD,
                 'document': document, 'container': container})
            case.evidence('file', ref, 'the corrupt first record and '
                          'the derived document the doomed launch ran')

            # The observation window: the startup's abort lands inside
            # the first polls — a startup that claimed the field on
            # its way out would show here as the incumbent demoted by
            # the dead claim, its fenced writes, or the field going
            # unclaimed.
            window = []
            last_tick = tick0
            field_tick = (sample0 or {}).get('tick')
            served = None
            violation = None
            unanswered = 0
            probes = 0
            reads = 0
            submission = None
            for round_no in range(DOOMED_STARTUP_ROUNDS):
                foreign_report = _try_role(ctx, foreign)
                if foreign_report is not None and served is None:
                    served = foreign_report
                incumbent = _try_role(ctx, base)
                partner = _try_role(ctx, peer_base)
                snap = _try_snapshot(ctx, base)
                probe = _try_plant(ctx, {'op': 'step', 'dt': 0})
                sample = _probe_sample(ctx, watch)
                tick = (snap or {}).get('tick')
                ftick = (sample or {}).get('tick')
                window.append({'round': round_no, 'incumbent': incumbent,
                               'partner': partner,
                               'foreign': foreign_report, 'tick': tick,
                               'probe': probe, 'field_tick': ftick})
                # The first violation wins the detail — the root
                # cause, not the cascade of legs it tripped.
                if incumbent is None:
                    unanswered += 1
                elif violation is None \
                        and incumbent.get('role') != expected[active]:
                    violation = ('the incumbent left role=active — the '
                                 'doomed startup disturbed the field '
                                 'owner: ' + json.dumps(incumbent)[:300])
                if violation is None and partner is not None \
                        and partner.get('role') != expected[peer]:
                    violation = ('the tracking peer reported a '
                                 'spurious role change: '
                                 + json.dumps(partner)[:300])
                if probe is not None:
                    probes += 1
                    if violation is None:
                        kind = _probe_error(probe)
                        if kind == 'unclaimed':
                            violation = ('the field entered the '
                                         'unclaimed window — no '
                                         'writer claim stands: '
                                         + json.dumps(probe)[:300])
                        elif kind != 'fenced':
                            violation = ('the field answered a third '
                                         'attachment\'s mutation '
                                         'probe without the standing '
                                         'claim\'s fencing: '
                                         + json.dumps(probe)[:300])
                if tick is not None:
                    if violation is None and last_tick is not None \
                            and tick <= last_tick:
                        violation = ('the incumbent\'s tick stalled at '
                                     + str(tick))
                    last_tick = tick
                if sample is not None:
                    reads += 1
                if ftick is not None:
                    if violation is None and field_tick is not None \
                            and ftick <= field_tick:
                        violation = ('the field stopped receiving the '
                                     'incumbent\'s writes at tick '
                                     + str(ftick))
                    field_tick = ftick
                # The receipted-path probe: one write_value submitted
                # to the incumbent inside the window must be admitted
                # and answered — a dropped connection retries next
                # round, a refusal is the violation.
                if submission is None:
                    try:
                        status, receipt = http_json(
                            'POST', base + '/command', command)
                        submission = {'status': status,
                                      'receipt': receipt}
                        outcome = (receipt or {}).get('outcome') or {}
                        if violation is None and (
                                status != 200 or 'rejected' in outcome):
                            violation = ('the incumbent refused the '
                                         'mid-window command: '
                                         + str(status) + ' '
                                         + json.dumps(receipt)[:300])
                    except urllib.error.HTTPError as exc:
                        body = exc.read()
                        try:
                            receipt = json.loads(body or b'null')
                        except ValueError:
                            receipt = None
                        finally:
                            exc.close()
                        submission = {'status': exc.code,
                                      'receipt': receipt}
                        if violation is None:
                            violation = ('the incumbent refused the '
                                         'mid-window command: HTTP '
                                         + str(exc.code) + ' '
                                         + json.dumps(receipt)[:300])
                    except Exception:
                        pass  # one dropped poll — retried next round
                if violation or served is not None:
                    break
                time.sleep(DOOMED_STARTUP_POLL)
            ref = save_evidence(ctx['evidence_dir'],
                                'doomed-startup-claim-window.json',
                                {'watch': watch, 'command': command,
                                 'submission': submission,
                                 'window': window})
            case.evidence('file', ref, 'the observation window: pair '
                          'roles, incumbent ticks, fencing probes, and '
                          'the field\'s own writes')

            # The post-attempt audit: the incumbent's receipt log must
            # carry the mid-window command once more than the baseline
            # did, the doomed peer's journal file must still hold
            # exactly the corrupt record — the replay failed before
            # this run's boundary could append — and its state file
            # must never have appeared.
            try:
                _, body = http_json('GET', base + '/receipts')
                receipts1 = [(r.get('command'), r.get('actor'))
                             for r in _receipt_list(body)]
            except Exception:
                receipts1 = None
            key = (command['command'], 'qa-lane')
            landed = receipts1 is not None \
                and receipts1.count(key) > receipts0.count(key)
            journal_after = (journal_path.read_text()
                             if journal_path.is_file() else None)
            state_written = (Path(state_file).is_file()
                             if state_file else None)
            snap1 = _try_snapshot(ctx, base)
            probe1 = _try_plant(ctx, {'op': 'step', 'dt': 0})
            ref = save_evidence(
                ctx['evidence_dir'],
                'doomed-startup-claim-after.json',
                {'tick': (snap1 or {}).get('tick'),
                 'receipts': len(receipts1)
                 if receipts1 is not None else None,
                 'command_landed': landed,
                 'probe': probe1, 'journal': journal_after,
                 'state_file': state_written})
            case.evidence('file', ref, 'the post-attempt audit '
                          'positions — the receipt log, the fencing '
                          'probe, and the doomed peer\'s files')

            corrupt_line = DOOMED_CORRUPT_RECORD + '\n'
            if served is not None:
                if journal_after is not None \
                        and journal_after.startswith(corrupt_line):
                    return case.finish('failed', 'the doomed startup '
                                       'served its monitor — the '
                                       'corrupt first record did not '
                                       'fail its journal replay: '
                                       + json.dumps(served)[:300])
                return case.finish('inconclusive', 'the foreign peer '
                                   'served its monitor — the corrupt '
                                   'record never reached the journal '
                                   'file it replayed: '
                                   + str(journal_after)[:300])
            if violation:
                return case.finish('failed', violation)
            if unanswered >= len(window):
                return case.finish('inconclusive', 'the incumbent '
                                   'never answered during the window')
            if probes == 0:
                return case.finish('inconclusive', 'the plant never '
                                   'answered a fencing probe')
            if reads == 0:
                return case.finish('inconclusive', 'the field never '
                                   'answered a read during the window')
            if submission is None:
                return case.finish('inconclusive', 'the incumbent '
                                   'never answered a command '
                                   'submission during the window')
            if journal_after is None:
                return case.finish('inconclusive', 'the foreign '
                                   'journal file vanished mid-attempt')
            if journal_after != corrupt_line:
                return case.finish('failed', 'the doomed startup ran '
                                   'past the failed replay — its '
                                   'journal file gained records: '
                                   + journal_after[:300])
            if state_written:
                return case.finish('failed', 'the doomed startup '
                                   'persisted a checkpoint — it ran '
                                   'past the failed journal replay')
            if receipts1 is None:
                return case.finish('inconclusive', 'the incumbent '
                                   'never answered the post-attempt '
                                   'receipt audit')
            if not landed:
                return case.finish('failed', 'the incumbent\'s '
                                   'receipt log never carried the '
                                   'mid-window command')
            if not _fenced(probe1):
                return case.finish('failed', 'the field\'s fencing '
                                   'changed across the attempt — a '
                                   'third attachment\'s probe answered '
                                   + json.dumps(probe1)[:300])
            case.observe('the incumbent held role=active across '
                         + str(len(window)) + ' polls — tick '
                         + str(tick0) + ' -> '
                         + str((snap1 or {}).get('tick')) + ', '
                         + str(probes) + ' probes fenced, the '
                         'mid-window command receipted')
            return case.finish('passed')

        try:
            record = attempt()
        except Exception as exc:
            record = case.finish('inconclusive', str(exc))
        # Teardown is unconditional once the peer is up: the foreign
        # seat must be clean for later cases, and the induction
        # artifact comes back out with it — the seat returns to the
        # never-written state the launch found.
        try:
            stop()
            case.observe('foreign peer ' + container + ' removed — '
                         'the seat is clean for later cases')
        except Exception as exc:
            case.observe('the foreign peer teardown failed: '
                         + str(exc)[:200])
            if record['outcome'] == 'passed':
                record['outcome'] = 'inconclusive'
                record['detail'] = ('the foreign peer was never '
                                    'removed: ' + str(exc)[:300])
        try:
            journal_path.unlink(missing_ok=True)
        except OSError as exc:
            case.observe('the corrupt journal record could not be '
                         'removed: ' + str(exc)[:200])
            if record['outcome'] == 'passed':
                record['outcome'] = 'inconclusive'
                record['detail'] = ('the induction record was left in '
                                    'the foreign journal file: '
                                    + str(exc)[:300])
        # The clean-rig leg: the pair still serves its settled roles
        # and the field still fences third-party mutation under the
        # incumbent's standing claim.
        if record['outcome'] == 'passed':
            try:
                roles = {name: _try_role(ctx, ctx[name])
                         for name in (active, peer)}
                probe = _try_plant(ctx, {'op': 'step', 'dt': 0})
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'doomed-startup-claim-teardown.json',
                    {'roles': roles, 'probe': probe})
                case.evidence('file', ref, 'the rig after teardown')
                if (roles.get(active) or {}).get('role') != 'active' \
                        or (roles.get(peer) or {}).get('role') \
                        != 'standby':
                    record = case.finish('failed', 'the pair did not '
                                         'return to its settled '
                                         'roles: '
                                         + json.dumps(roles)[:300])
                elif not _fenced(probe):
                    record = case.finish('failed', 'the field no '
                                         'longer fences under the '
                                         'incumbent\'s claim after '
                                         'teardown: '
                                         + json.dumps(probe)[:300])
                else:
                    case.observe('the rig returned clean — settled '
                                 'roles, the standing claim fencing')
            except Exception as exc:
                record = case.finish('inconclusive', 'the '
                                     'post-teardown rig could not be '
                                     'verified: ' + str(exc)[:300])
        return record
    except Exception as exc:
        return case.finish('inconclusive', str(exc))


def scenario_parameter_tune_carryover(ctx):
    """A descriptor-declared Float parameter retuned through the
    receipted path survives the pair's promotion."""
    case = Case('parameter-tune-carryover',
                'Receipted parameter tune carries across promotion',
                'a descriptor-declared Float parameter retuned through '
                'POST /command settles applied, the served parameter '
                'report and the journal reflect it, an out-of-range '
                'tune meets the named out_of_range rejection and '
                'leaves the value unchanged, and the promoted peer\'s '
                'parameter report still carries the tuned value rather '
                'than the model-declared default')
    try:
        # In suite order the settled active is ctrl-a — the peer whose
        # checkpoints the tracking standby pulls — and the peer must
        # report tracking convergence for the promotion leg to carry
        # anything. A lone replay on a fresh rig finds the same layout.
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        peer = 'standby' if active == 'active' else 'active'
        base, peer_base = ctx[active], ctx[peer]
        case.observe('tuning against ' + active + ' (' + base + ')')

        def converged():
            try:
                report = _role(ctx, peer_base)
            except Exception:
                return None
            sync = report.get('sync') or {}
            return report if 'tracking' in sync else None

        tracking = wait_for(converged, time.monotonic() + TUNE_DEADLINE)
        ref = save_evidence(ctx['evidence_dir'],
                            'parameter-tune-roles.json',
                            {'peer': tracking})
        case.evidence('file', ref, 'the tracking peer\'s role report')
        if not tracking:
            return case.finish('inconclusive', 'the peer never '
                               'reported tracking convergence — the '
                               'promotion leg cannot be exercised')

        # The tune target: a descriptor-declared Float parameter whose
        # declared range holds a changed value and refuses a finite
        # out-of-range one, with its live value in the served report.
        _, schema = http_json('GET', base + '/schema')
        snapshot = _snapshot(ctx, base)
        ref = save_evidence(ctx['evidence_dir'],
                            'parameter-tune-interface.json',
                            {'schema': schema, 'snapshot': snapshot})
        case.evidence('file', ref, 'the served registry and live '
                      'parameter report the target is discovered from')
        plan = _float_tune_plan(schema, snapshot)
        if plan is None:
            return case.finish('inconclusive', 'no descriptor-declared '
                               'Float parameter with a violatable range '
                               'is served')
        component, name, current, tuned, outside = plan
        case.observe('tune target: ' + str(component) + ' '
                     + str(name) + ' ' + str(current) + ' -> '
                     + str(tuned) + ' (out-of-range probe '
                     + str(outside) + ')')

        # The receipted in-range tune: one submission, one receipt, the
        # settlement lands at the next scan boundary.
        command = {'command': {'set_parameter': {
            'component': component, 'name': name,
            'value': {'float': tuned}}}, 'actor': 'qa-lane'}
        _, before = http_json('GET', base + '/receipts')
        index = len(_receipt_list(before))
        status, receipt = http_json('POST', base + '/command', command)
        ref = save_evidence(ctx['evidence_dir'],
                            'parameter-tune-submission.json',
                            {'command': command, 'status': status,
                             'receipt': receipt})
        case.evidence('file', ref, 'the in-range tune submission and '
                      'its receipt')
        if status != 200:
            return case.finish('failed', 'the in-range tune was '
                               'refused: ' + str(status) + ' '
                               + json.dumps(receipt)[:300])
        settled = wait_for(
            lambda: _settled_outcome(ctx, base, index),
            time.monotonic() + TUNE_DEADLINE)
        if settled != 'applied':
            return case.finish('failed', 'the in-range tune lacks a '
                               'settled receipt: '
                               + str(settled or 'never settled'))
        case.observe('tune receipt settled ' + settled)

        # The served parameter report reflects the standing tune — the
        # live read of the same fields the checkpoint captures.
        reflected = wait_for(
            lambda: (_parameter_value(s, component, name) == tuned
                     and s or None)
            if (s := _try_snapshot(ctx, base)) else None,
            time.monotonic() + TUNE_DEADLINE)
        report_snapshot = _try_snapshot(ctx, base) or {}
        ref = save_evidence(ctx['evidence_dir'],
                            'parameter-tune-report.json',
                            report_snapshot.get('parameters'))
        case.evidence('file', ref, 'the parameter report after the '
                      'tune settled')
        if not reflected:
            return case.finish('inconclusive', 'the served parameter '
                               'report never reflected the tune: '
                               + str(component) + ' ' + str(name)
                               + ' still reads '
                               + str(_parameter_value(report_snapshot,
                                                      component, name)))
        case.observe('the parameter report reads ' + str(tuned))

        # The run's audit: the settled tune's receipt is journaled.
        def journaled():
            try:
                _, journal = http_json('GET', base + '/journal')
            except Exception:
                return None
            journaled.last = journal
            return _journal_covers_parameter(journal, component, name) \
                or None

        journaled.last = []
        covered = wait_for(journaled, time.monotonic() + TUNE_DEADLINE)
        ref = save_evidence(ctx['evidence_dir'],
                            'parameter-tune-journal.json',
                            journaled.last)
        case.evidence('file', ref, 'the active\'s journal after the '
                      'tune settled')
        if not covered:
            return case.finish('failed', 'the journaled evidence '
                               'misses the receipted tune')

        # The out-of-range tune meets the named validation rejection at
        # submission — the descriptor range speaks before the queue —
        # and the standing tune is untouched.
        probe = {'command': {'set_parameter': {
            'component': component, 'name': name,
            'value': {'float': outside}}}, 'actor': 'qa-lane'}
        status, receipt = http_json('POST', base + '/command', probe)
        held = _parameter_value(_try_snapshot(ctx, base) or {},
                                component, name)
        ref = save_evidence(ctx['evidence_dir'],
                            'parameter-tune-rejection.json',
                            {'command': probe, 'status': status,
                             'receipt': receipt, 'reported': held})
        case.evidence('file', ref, 'the out-of-range submission, its '
                      'receipt, and the parameter report')
        outcome = _outcome_key(receipt if isinstance(receipt, dict)
                               else {})
        if status != 200 or outcome != 'rejected:out_of_range':
            return case.finish('failed', 'the out-of-range tune was '
                               'not rejected by name: ' + str(status)
                               + ' ' + json.dumps(receipt)[:300])
        if held != tuned:
            return case.finish('failed', 'the rejected out-of-range '
                               'tune changed the parameter: '
                               + str(held))
        case.observe('out-of-range tune rejected by name; '
                     + str(name) + ' still reads ' + str(held))

        # The tuned value must reach the tracking peer through the
        # checkpoint stream before the switch — the carryover the
        # promotion is about to prove.
        carried = wait_for(
            lambda: (_parameter_value(s, component, name) == tuned
                     and s or None)
            if (s := _try_snapshot(ctx, peer_base)) else None,
            time.monotonic() + TUNE_DEADLINE)
        standby_snapshot = _try_snapshot(ctx, peer_base) or {}
        ref = save_evidence(ctx['evidence_dir'],
                            'parameter-tune-standby.json',
                            standby_snapshot.get('parameters'))
        case.evidence('file', ref, 'the tracking peer\'s parameter '
                      'report before the switch')
        if not carried:
            return case.finish('inconclusive', 'the tracking peer\'s '
                               'parameter report never reflected the '
                               'tune — the checkpoint carryover cannot '
                               'be observed')

        # The switch: demote the tuned active, promote the converged
        # standby, and read the promoted peer's own parameter report.
        status, body = http_json('POST', base + '/demote')
        case.observe('demote ' + active + ': ' + str(status) + ' '
                     + json.dumps(body))
        if status != 200:
            return case.finish('failed', 'demote refused: '
                               + str(body))
        promoted = None
        deadline = time.monotonic() + TUNE_DEADLINE
        while time.monotonic() < deadline and promoted is None:
            try:
                status, body = http_json('POST', peer_base + '/promote')
                if status == 200:
                    promoted = body
                else:
                    time.sleep(POLL_INTERVAL)
            except urllib.error.HTTPError as exc:
                if exc.code == 409:
                    time.sleep(POLL_INTERVAL)
                else:
                    raise
        settled_role = wait_for(
            lambda: (r.get('role') == 'active' and r or None)
            if (r := _role(ctx, peer_base)) else None,
            time.monotonic() + TUNE_DEADLINE)
        ref = save_evidence(ctx['evidence_dir'],
                            'parameter-tune-promotion.json',
                            {'demoted': active, 'promote': promoted,
                             'role': settled_role})
        case.evidence('file', ref, 'the demote/promote responses and '
                      'the promoted peer\'s role')
        if promoted is None:
            return case.finish('failed', 'the converged standby never '
                               'promoted within ' + str(TUNE_DEADLINE)
                               + 's')
        if not settled_role:
            return case.finish('failed', 'the promoted peer did not '
                               'settle active')

        promoted_report = wait_for(
            lambda: (_parameter_value(s, component, name) == tuned
                     and s or None)
            if (s := _try_snapshot(ctx, peer_base)) else None,
            time.monotonic() + TUNE_DEADLINE)
        last = _try_snapshot(ctx, peer_base)
        if last is None:
            return case.finish('inconclusive', 'the promoted peer\'s '
                               'monitor never served a parameter '
                               'report')
        ref = save_evidence(ctx['evidence_dir'],
                            'parameter-tune-promoted.json',
                            last.get('parameters'))
        case.evidence('file', ref, 'the promoted peer\'s parameter '
                      'report')
        if not promoted_report:
            found = _parameter_value(last, component, name)
            if found == current:
                return case.finish('failed', 'the promoted peer '
                                   'reverted ' + str(name) + ' to the '
                                   'model-declared default '
                                   + str(current) + ' — the tune did '
                                   'not ride the checkpoint')
            return case.finish('failed', 'the promoted peer lost the '
                               'tuned value: ' + str(name)
                               + ' reads ' + str(found))
        case.observe('the promoted peer still reports ' + str(name)
                     + ' = ' + str(tuned) + ' — the tune crossed the '
                     'checkpoint')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))


def scenario_evidence_capture(ctx):
    """The monitor exposes receipts and a transition journal."""
    case = Case('evidence-capture',
                'Monitor exposes receipts and journal',
                'GET /receipts and GET /journal return records covering the '
                'run so far')
    try:
        # Self-contained on either role layout: replayed alone the rig
        # is fresh (ctrl-a active), while the full suite reaches this
        # case after the failover (ctrl-b active). The run's own command
        # goes to whichever peer reports settled active — the original
        # finding read the audit off the pair after a command and a
        # switchover.
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        other = 'standby' if active == 'active' else 'active'
        case.observe('command peer: ' + active)

        # A writable bool input — the same target the operator-command
        # case picks.
        _, signals = http_json('GET', ctx[active] + '/signals')
        target = _writable_bool_point(signals)
        if target is None:
            return case.finish('inconclusive',
                               'no writable bool point in the model')
        point = target['point']

        command = {'command': {'write_value': {
            'point': point, 'kind': 'bool',
            'value': {'bool': True}}},
            'actor': 'qa-lane'}
        status, receipt = http_json('POST', ctx[active] + '/command',
                                    command)
        case.observe('POST /command on ' + active + ': ' + str(status)
                     + ' ' + json.dumps(receipt))
        if status != 200 or 'rejected' in receipt.get('outcome', {}):
            return case.finish('failed',
                               'command refused: ' + str(receipt))

        deadline = time.monotonic() + SCENARIO_TIMEOUT
        last = {}

        def receipts_cover(name):
            try:
                last[name] = http_json('GET', ctx[name] + '/receipts')[1]
            except Exception:
                return False
            return _command_covered(last[name], point)

        # The accepting peer's log covers the command once its boundary
        # settled it — the submission's own record.
        if not wait_for(lambda: receipts_cover(active) or None,
                        deadline):
            save_evidence(ctx['evidence_dir'],
                          'evidence-receipts.json', last)
            case.evidence('file', 'evidence/evidence-receipts.json')
            case.observe('receipts=' + str(
                len(_receipt_list(last.get(active, [])))))
            return case.finish('failed', 'no command receipts recorded')

        def tracking(name):
            try:
                sync = _role(ctx, ctx[name]).get('sync') or {}
            except Exception:
                return None
            return name if 'tracking' in sync else None

        # The pair's one audit travels the checkpoint stream: a peer
        # reporting itself tracking the command's owner must converge to
        # the same receipts — the coverage the finding reported empty.
        # A peer that left the lineage (a demoted half pulling nothing)
        # keeps only the records its own participation produced and is
        # not asked for more.
        tracked = wait_for(lambda: tracking(other),
                           min(deadline, time.monotonic() + 20))
        other_ok = True
        if tracked:
            other_ok = wait_for(
                lambda: receipts_cover(other) or None, deadline)
        else:
            try:
                last[other] = http_json(
                    'GET', ctx[other] + '/receipts')[1]
            except Exception:
                last[other] = None
        ref = save_evidence(ctx['evidence_dir'],
                            'evidence-receipts.json', last)
        case.evidence('file', ref, 'GET /receipts from both endpoints')
        case.observe('receipts: ' + active + '='
                     + str(len(_receipt_list(last.get(active, []))))
                     + ' ' + other + '='
                     + str(len(_receipt_list(last.get(other) or []))))
        if not other_ok:
            return case.finish('failed',
                               'tracking peer serves no command receipts')

        # The journal is the run's transition record: every endpoint
        # holds entries, and the peers inside the lineage record this
        # command's settlement — the adopting peer journals the
        # transferred receipt like a local submission's.
        last_journal = {}

        def journal_cover(name, need_command):
            try:
                last_journal[name] = http_json(
                    'GET', ctx[name] + '/journal')[1]
            except Exception:
                return False
            entries = _journal_list(last_journal[name])
            if not entries:
                return False
            if need_command and not _journal_covers(entries, point):
                return False
            return True

        journal_ok = wait_for(
            lambda: (journal_cover(active, True)
                     and journal_cover(other, bool(tracked))) or None,
            deadline)
        ref = save_evidence(ctx['evidence_dir'],
                            'evidence-journal.json', last_journal)
        case.evidence('file', ref, 'GET /journal from both endpoints')
        case.observe('journal entries: ' + json.dumps(
            {name: len(_journal_list(payload or []))
             for name, payload in last_journal.items()}))
        if not journal_ok:
            return case.finish('failed',
                               'journal does not cover the run')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))


# --------------------------------------------------------------------
# The lone-controller recovery contract (WW-LCM-001's restart clause,
# decision 35's --state-file and decision 36's --journal-file): the
# runner-owned restart action stops the active peer's container and
# starts it again, and the resumed process must continue the persisted
# run — the tick domain, the operator state the checkpoint carried,
# and the journal file's seq numbering all continue across the two
# process lifetimes, and the pair settles back to active/standby. The
# restart-window leg submits a second receipted write and invokes the
# restart inside its admission-to-application window: the accepted
# admission rides the checkpoint's receipt log, so the resumed run
# re-queues it rather than losing it unaudited — the point serves the
# commanded value and the durable journal carries its CommandSettled
# on whichever side of the run boundary the settlement landed. The
# same leg audits the journal's restart integrity: exactly one
# run-boundary marker per resumed lifetime — file-side and in its
# served entry form — no pre-restart settled receipt or standing point
# observation re-journaled past it, and the tracking peer's journal
# undisturbed.


def scenario_controller_restart(ctx):
    """Stop the active peer's container and restart it: the run resumes
    from --state-file rather than cold-starting."""
    case = Case('controller-restart',
                'Restarted controller resumes its persisted run',
                'stopping and starting the active controller container '
                'leaves the resumed run continuing the persisted tick '
                'domain rather than restarting at zero, the '
                'pre-restart point write still applied, a command '
                'admitted at the restart boundary never silently lost '
                '— the point serves its value and the durable journal '
                'carries its CommandSettled — the journal file '
                'carrying a run_boundary marker with continuing seqs '
                'across both process lifetimes, no settled receipt or '
                'observation re-journaled past it, the served journal '
                'carrying the boundary once, the tracking peer\'s '
                'journal undisturbed, and the pair settled back to '
                'active/standby')
    try:
        restart = ctx.get('restart_controller')
        if restart is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no controller-restart action')
        # Whichever endpoint currently reports active is the restart
        # target — in suite order this runs ahead of the failover case,
        # so it is ctrl-a; a lone replay finds the fresh rig the same
        # way.
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        peer = 'standby' if active == 'active' else 'active'
        base, peer_base = ctx[active], ctx[peer]
        case.observe('restart target: ' + active + ' (' + base + ')')

        # Establish the operator state the checkpoint must carry — the
        # same writable bool point the command scenarios use.
        _, signals = http_json('GET', base + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'controller-restart-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming the '
                      'state-carryover target')
        target = _writable_bool_point(signals)
        if target is None:
            return case.finish('inconclusive',
                               'no writable bool point in the model')
        point = target['point']
        command = {'command': {'write_value': {
            'point': point, 'kind': 'bool', 'value': {'bool': True}}},
            'actor': 'qa-lane'}
        status, receipt = http_json('POST', base + '/command', command)
        if status != 200:
            return case.finish('failed', 'pre-restart command refused: '
                             + str(receipt))
        applied = wait_for(
            lambda: _point_value(_try_snapshot(ctx, base) or {}, point)
            is True or None, time.monotonic() + 30)
        if not applied:
            return case.finish('failed', 'the pre-restart write never '
                               'applied at point ' + str(point))
        before = _snapshot(ctx, base)
        tick0 = before.get('tick') or 0

        # The restart-window command: a second receipted write on the
        # same point, admitted and answered ahead of its applying scan.
        journal = (ctx.get('journal_files') or {}).get(active)
        peer_journal = (ctx.get('journal_files') or {}).get(peer)
        if journal is None or peer_journal is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no journal-file path for the '
                               'restarting pair')
        pending = {'command': {'write_value': {
            'point': point, 'kind': 'bool',
            'value': {'bool': False}}},
            'actor': 'qa-lane'}
        status, pending_receipt = http_json('POST', base + '/command',
                                            pending)
        if status != 200 or 'rejected' in \
                ((pending_receipt or {}).get('outcome') or {}):
            return case.finish('failed', 'the restart-window command '
                               'refused: ' + str(pending_receipt))

        # The audit baselines the restart-integrity legs diff against:
        # the durable journal's parsed records and the served journal
        # tail at the moment of admission, and the tracking peer's own
        # journal record.
        records0 = _journal_entries(journal)
        served0 = _journal_list(http_json('GET', base + '/journal')[1])
        peer_records0 = _journal_entries(peer_journal)
        bounds0 = [item['run_boundary'] for item in records0
                   if 'run_boundary' in item]
        peer_bounds0 = [item['run_boundary'] for item in peer_records0
                        if 'run_boundary' in item]
        ref = save_evidence(ctx['evidence_dir'],
                            'controller-restart-before.json',
                            {'tick': tick0, 'point': point,
                             'receipt': receipt,
                             'pending': pending_receipt})
        case.evidence('file', ref, 'pre-restart tick, applied write, '
                      'and the admitted restart-window command')
        ref = save_evidence(ctx['evidence_dir'],
                            'controller-restart-journal-before.json',
                            {'path': str(journal),
                             'peer_path': str(peer_journal),
                             'records': records0, 'served': served0,
                             'peer_records': peer_records0})
        case.evidence('file', ref, 'the durable and served journals '
                      'plus the tracking peer\'s journal at admission')
        case.observe('point ' + str(point) + ' applied true at tick '
                     + str(tick0) + '; restart-window write admitted '
                     + json.dumps(
                         (pending_receipt or {}).get('outcome')))

        # The runner-owned lifecycle action, invoked immediately —
        # docker stop + start on the already-running container,
        # recorded on the run's timeline. On the paced rig the stop
        # lands inside the admission-to-application window almost
        # always: exactly the case the accepted-command persistence
        # finding caught.
        try:
            restart(active)
        except Exception as exc:
            return case.finish('inconclusive', 'the restart action '
                               'never completed: ' + str(exc)[:300])
        case.observe('controller restart action returned')

        # Wait for the restarted peer's monitor while watching the
        # other endpoint for a spurious promotion.
        promoted = []

        def returned():
            try:
                report = _role(ctx, peer_base)
            except Exception:
                report = {}
            if report.get('role') == 'active':
                promoted.append(report)
            try:
                report = _role(ctx, base)
            except Exception:
                return None
            return report if report.get('role') == 'active' else None

        back = wait_for(returned,
                        time.monotonic() + RESTART_RETURN_DEADLINE,
                        interval=RESTART_POLL)
        if promoted:
            ref = save_evidence(ctx['evidence_dir'],
                                'controller-restart-roles.json',
                                {'peer': promoted[0]})
            case.evidence('file', ref)
            return case.finish('failed', 'the peer reported active '
                               'while the restarted controller was '
                               'down: ' + json.dumps(promoted[0])[:400])
        if back is None:
            return case.finish('inconclusive', 'the restarted '
                               'controller never returned')
        case.observe('restarted peer serving again, role '
                     + str(back.get('role')) + ' at tick '
                     + str(back.get('tick')))

        # The resumed run's tick domain continues the persisted
        # checkpoint: a cold start or a stale resume answers below the
        # pre-restart mark, and a resumed run keeps advancing.
        resumed = _snapshot(ctx, base)
        tick1 = resumed.get('tick') or 0
        grown = wait_for(
            lambda: (s.get('tick', 0) > tick1 and s or None)
            if (s := _try_snapshot(ctx, base)) else None,
            time.monotonic() + 30)
        ref = save_evidence(ctx['evidence_dir'],
                            'controller-restart-snapshot.json',
                            grown or resumed)
        case.evidence('file', ref, 'resumed snapshot: tick '
                      + str(tick0) + ' -> ' + str(tick1))
        if tick1 + RESTART_SLACK_TICKS < tick0:
            return case.finish('failed', 'tick regressed across the '
                               'restart: ' + str(tick0) + ' -> '
                               + str(tick1) + ' — cold-start or stale '
                               'state file')
        if not grown:
            return case.finish('failed', 'the resumed run did not '
                               'advance its tick')
        case.observe('resumed at tick ' + str(tick1) + ' (pre-restart '
                     + str(tick0) + ')')

        # The restart-window command was never silently lost — the
        # clause the admission-time persist bought. The point reaches
        # the second write's value whether the command applied before
        # the stop or re-queued through the persisted Accepted receipt
        # and applied on a resumed scan, and the durable journal
        # carries its CommandSettled on whichever side of the run
        # boundary the settlement landed.
        landed = wait_for(
            lambda: _point_value(_try_snapshot(ctx, base) or {}, point)
            is False or None,
            time.monotonic() + RESTART_COMMAND_DEADLINE)
        settlement = {}

        def command_settled():
            try:
                items = _journal_entries(journal)
            except (OSError, ValueError) as exc:
                settlement['error'] = str(exc)
                return None
            settlement['items'] = items
            # The resumed run's side of the record opens at the marker
            # for run > 1: until it lands nothing is post-boundary, and
            # a settlement found already is a pre-stop application.
            marks = [i for i, item in enumerate(items)
                     if 'run_boundary' in item
                     and (item['run_boundary'] or {}).get('run', 0) > 1]
            edge = marks[-1] if marks else -1
            for index, item in enumerate(items):
                body = _journal_settled(item)
                if (body or {}).get('command') == pending['command']:
                    return {'seq': (item.get('entry') or {}).get('seq'),
                            'where': 'post-boundary'
                                     if 0 <= edge < index
                                     else 'pre-boundary'}
            return None

        found = wait_for(command_settled,
                         time.monotonic() + RESTART_COMMAND_DEADLINE,
                         interval=RESTART_POLL)
        ref = save_evidence(ctx['evidence_dir'],
                            'controller-restart-settlement.json',
                            {'command': pending['command'],
                             'landed': bool(landed),
                             'settlement': found,
                             'error': settlement.get('error')})
        case.evidence('file', ref, 'the restart-window command\'s '
                      'served value and journaled settlement')
        case.observe('restart-window write: point '
                     + ('serves false' if landed else
                        'still reads ' + str(_point_value(
                            _try_snapshot(ctx, base) or {}, point)))
                     + '; journal settlement '
                     + (json.dumps(found) if found else 'absent'))
        if not landed and not found:
            return case.finish('failed', 'the restart-window command '
                               'was silently lost: the point never '
                               'served the commanded value and the '
                               'durable journal carries no settlement '
                               '— the admission-time loss the '
                               'persisted Accepted receipt closed')
        if not landed:
            return case.finish('failed', 'the restart-window command '
                               'journaled its settlement but the '
                               'point never served the commanded '
                               'value')
        if not found:
            return case.finish('failed', 'the restart-window command '
                               'reached the point but the durable '
                               'journal carries no CommandSettled '
                               'for it: '
                               + str(settlement.get('error')))
        case.observe('the restart-window command settled '
                     + found['where'] + ' — '
                     + ('applied before the stop'
                        if found['where'] == 'pre-boundary' else
                        're-queued through the admission-time persist '
                        'and applied after the resume'))

        # The durable audit record: the journal file must hold a
        # run_boundary marker opening the restarted lifetime at the
        # restored tick, with entry seqs continuing across it. A fresh
        # settled command guarantees a post-boundary entry exists.
        status, receipt = http_json('POST', base + '/command',
                                    {'command': {'write_value': {
                                        'point': point, 'kind': 'bool',
                                        'value': {'bool': False}}},
                                     'actor': 'qa-lane'})
        if status != 200:
            return case.finish('failed', 'the post-restart command '
                               'refused: ' + str(receipt))
        parsed = {}

        def post_boundary():
            try:
                parsed['items'] = _journal_records(journal)
            except (OSError, ValueError) as exc:
                parsed['error'] = str(exc)
                return None
            items = parsed['items']
            marks = [i for i, item in enumerate(items)
                     if 'boundary' in item]
            if len(marks) < 2:
                return None
            return [item['seq'] for item in items[marks[1] + 1:]
                    if 'seq' in item] or None

        post = wait_for(post_boundary,
                        time.monotonic() + RESTART_JOURNAL_DEADLINE,
                        interval=RESTART_POLL)
        ref = save_evidence(ctx['evidence_dir'],
                            'controller-restart-journal.json',
                            {'path': str(journal),
                             'records': parsed.get('items'),
                             'error': parsed.get('error')})
        case.evidence('file', ref, 'the journal file across the '
                      'restart')
        items = parsed.get('items') or []
        bounds = [item['boundary'] for item in items
                  if 'boundary' in item]
        seqs = [item['seq'] for item in items if 'seq' in item]
        if len(bounds) < 2:
            return case.finish('failed', 'the journal file lacks the '
                               'run-boundary marker for the restarted '
                               'lifetime: ' + str(parsed.get('error')
                               or bounds))
        if [b.get('run') for b in bounds] \
                != list(range(1, len(bounds) + 1)):
            return case.finish('failed', 'journal run numbering does '
                               'not continue the file\'s lifetimes: '
                               + json.dumps(bounds)[:400])
        if len(bounds) != len(bounds0) + 1:
            return case.finish('failed', 'the restart did not add '
                               'exactly one run-boundary marker to the '
                               'journal file: ' + str(len(bounds0))
                               + ' -> ' + str(len(bounds)))
        if not bounds[-1].get('tick'):
            return case.finish('failed', 'the restarted lifetime\'s '
                               'boundary records a cold start: '
                               + json.dumps(bounds[-1]))
        if not seqs or any(not isinstance(seq, int) for seq in seqs) \
                or seqs != sorted(seqs) or len(set(seqs)) != len(seqs):
            return case.finish('failed', 'journal seqs do not '
                               'continue across the restart: '
                               + str(seqs[:20]))
        if not post:
            return case.finish('failed', 'no journaled entry follows '
                               'the restarted lifetime\'s boundary '
                               'marker')
        case.observe('journal: ' + str(len(bounds)) + ' lifetimes, '
                     'run ' + str(bounds[-1].get('run'))
                     + ' resumed at tick ' + str(bounds[-1].get('tick'))
                     + ', ' + str(len(seqs)) + ' entries with '
                     'continuing seqs')

        # The pair settles back: the restarted peer active, the other
        # reporting standby — a tracking peer reconverged behind it.
        def roles_settled():
            try:
                resumed_role = _role(ctx, base)
                peer_role = _role(ctx, peer_base)
            except Exception:
                return None
            if resumed_role.get('role') != 'active' \
                    or peer_role.get('role') != 'standby':
                return None
            if peer == 'standby' and 'tracking' not in \
                    (peer_role.get('sync') or {}):
                return None
            return {'restarted': resumed_role, 'peer': peer_role}

        settled = wait_for(roles_settled,
                           time.monotonic() + RESTART_SETTLE_DEADLINE,
                           interval=RESTART_POLL)
        ref = save_evidence(ctx['evidence_dir'],
                            'controller-restart-roles.json',
                            settled or {})
        case.evidence('file', ref, 'post-restart role reports')
        if not settled:
            return case.finish('failed', 'the pair did not settle '
                               'back to active/standby after the '
                               'restart')
        case.observe('roles settled: restarted peer active, '
                     + peer + ' standby'
                     + (' tracking' if peer == 'standby' else ''))

        # The served-journal restart contract, audited now the pair
        # has re-settled: the durable file carries no settled receipt
        # or standing observation re-journaled past the boundary, the
        # served journal holds the resumed run's boundary exactly once
        # with seqs continuing, and the tracking peer's journal is
        # undisturbed by the active's restart.
        items1 = _journal_entries(journal)
        marks1 = [i for i, item in enumerate(items1)
                  if 'run_boundary' in item]
        pre_items = items1[:marks1[-1]]
        post_items = items1[marks1[-1] + 1:]
        pre_keys = [_receipt_key(body) for body in
                    (_journal_settled(i) for i in pre_items)
                    if body is not None]
        all_keys = [_receipt_key(body) for body in
                    (_journal_settled(i) for i in items1)
                    if body is not None]
        post_keys = {_receipt_key(body) for body in
                     (_journal_settled(i) for i in post_items)
                     if body is not None}
        duplicated = [key for key in set(pre_keys)
                      if key in post_keys
                      or all_keys.count(key) != pre_keys.count(key)]
        if duplicated:
            return case.finish('failed', 'pre-restart settled '
                               'receipts are re-journaled across the '
                               'run boundary: '
                               + str(duplicated[:2])[:400])
        observed = {}
        for item in pre_items:
            seen = _journal_observation(item)
            if seen:
                observed[(seen[0], seen[1])] = seen[3]
        phantoms = []
        for item in post_items:
            seen = _journal_observation(item)
            if not seen:
                continue
            kind, changed, prior, to = seen
            key = (kind, changed)
            if key in observed \
                    and (prior is None or to == observed[key]):
                phantoms.append(item.get('entry'))
            observed[key] = to
        if phantoms:
            return case.finish('failed', 'the resumed run re-journaled '
                               'a phantom first-observation census '
                               'past the run boundary: '
                               + json.dumps(phantoms[:2])[:400])
        served = _journal_list(http_json('GET', base + '/journal')[1])
        served_marks = [(entry.get('event') or {}).get('run_boundary')
                        for entry in served
                        if 'run_boundary' in (entry.get('event') or {})]
        if [mark.get('run') for mark in served_marks] \
                != [bounds[-1].get('run')]:
            return case.finish('failed', 'the served journal does not '
                               'carry exactly one run_boundary entry '
                               'for the resumed run: '
                               + json.dumps(served_marks)[:300])
        served_seqs = [entry.get('seq') for entry in served]
        if any(not isinstance(seq, int) for seq in served_seqs) \
                or served_seqs != sorted(served_seqs) \
                or len(set(served_seqs)) != len(served_seqs):
            return case.finish('failed', 'served journal seqs do not '
                               'continue across the restart: '
                               + str(served_seqs[:20]))
        peer_records1 = _journal_entries(peer_journal)
        peer_bounds1 = [item['run_boundary'] for item in peer_records1
                        if 'run_boundary' in item]
        if peer_records1[:len(peer_records0)] != peer_records0 \
                or len(peer_bounds1) != len(peer_bounds0):
            return case.finish('failed', 'the tracking peer\'s journal '
                               'was disturbed by the active\'s '
                               'restart')
        case.observe('journal integrity: run '
                     + str(bounds[-1].get('run')) + ' boundary '
                     'singular file-side and served, no settled '
                     'receipt re-journaled, no phantom census, the '
                     'tracking peer\'s journal undisturbed')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))


# --------------------------------------------------------------------
# WW-LCM-001's continuity clause on the contract QA finding #532 landed
# (merged as #535): a tracking standby whose checkpoint source
# cold-restarts or is replaced — the stream's served tick falling below
# the last alignment, or below the run's own tick before any alignment
# stood — adopts the regressed state at its own run tick under a
# generation offset (crates/dcs-runtime/src/peer.rs `SourceRestart` /
# `tick_offset`), never rewinding the tick domain /history and
# /journal attribute into, and journals exactly one `source_restarted`
# entry per regression carrying the resync tick, the prior alignment,
# and the resumed stream tick (JournalEvent::SourceRestarted).
#
# The induction needs a cold start the state-file-preserving
# `restart_controller` cannot produce: ctx['cold_restart_controller']
# stops the named container, drops its host-side state.json inside the
# bounded run dir — the journal file stays, its new run-boundary marker
# part of the evidence the resume was cold — and starts it again. Leg
# one cold-restarts the settled active: the tracking standby's served
# tick stays monotonic and never below its pre-restart mark, /history
# stays newest-last, and the journaled resync carries the prior
# alignment. Leg two promotes the tracked peer and cold-restarts the
# demoted peer's container: the fresh process's startup claim fences
# the promoted writer into demotion, and the demoted peer's first pull
# — its alignment cleared by the demotion — regresses against the
# run's own tick, journaling the same shape with was_aligned null.
# Both legs leave the launch layout standing (ctrl-a active, ctrl-b
# tracking) for the cases behind this one.

SOURCE_RESTART_POLL = 0.5            # cadence watching the pair mid-restart
SOURCE_RESTART_RETURN_DEADLINE = 60  # the cold peer's monitor returning
SOURCE_RESTART_SETTLE_DEADLINE = 60  # adoption and role settles
SOURCE_RESTART_JOURNAL_DEADLINE = 30  # the source_restarted entry landing
SOURCE_RESTART_BASELINE_DEADLINE = 45  # settled tracking + tick clearance
# The adopting peer's run tick must stand well past where a cold
# process resumes, so the regressed stream is unambiguous.
SOURCE_RESTART_MIN_TICK = 60
# The pull-per-scan cadence re-applies the stream's latest checkpoint
# every cycle, so a tracking peer's served tick jitters by a scan or
# two around the tracked line — the regression the induction produces
# rewinds the domain by the whole run tick, orders of magnitude
# wider.
SOURCE_RESTART_JITTER = 16


def _source_restarts(items):
    """The `source_restarted` events a journal carries, each as
    {'seq', 'tick', 'was_aligned', 'resumed_at'} — for a parsed
    --journal-file's records and a served `GET /journal` tail alike."""
    found = []
    for item in items:
        body = item.get('entry') if isinstance(item.get('entry'), dict) \
            else item
        event = (body or {}).get('event') or {}
        restart = event.get('source_restarted')
        if not isinstance(restart, dict):
            continue
        found.append({'seq': body.get('seq'), 'tick': body.get('tick'),
                      'was_aligned': restart.get('was_aligned'),
                      'resumed_at': restart.get('resumed_at')})
    return found


def _tick_rewind(ticks, floor, jitter):
    """The first served-tick sample that rewound the tick domain — a
    drop below `floor`, or more than the pull-cadence `jitter` below
    the running peak — or None. Returns the offending sample index."""
    peak = floor
    for index, tick in enumerate(ticks):
        if not isinstance(tick, int):
            return index
        if tick < peak - jitter:
            return index
        peak = max(peak, tick)
    return None


def _history_disorder(payload, jitter):
    """The first newest-last violation in a `/history` answer — the
    point id plus the offending ordering — or None when every point's
    rows are strictly-increasing seqs whose ticks never rewind past
    the pull-cadence `jitter` below the running peak."""
    if not isinstance(payload, list):
        return 'unrecognized history payload'
    for entry in payload:
        rows = entry.get('samples') or []
        seqs = [row.get('seq') for row in rows]
        ticks = [(row.get('sample') or {}).get('tick') for row in rows]
        if seqs != sorted(seqs) or len(set(seqs)) != len(seqs):
            return 'point ' + str(entry.get('point')) \
                + ' seqs not strictly increasing: ' + str(seqs[:20])
        if any(not isinstance(tick, int) for tick in ticks):
            return 'point ' + str(entry.get('point')) \
                + ' carries a sample without an integer tick'
        rewound = _tick_rewind(ticks, ticks[0] if ticks else 0, jitter)
        if rewound is not None:
            return 'point ' + str(entry.get('point')) \
                + ' ticks rewound at sample ' + str(rewound) \
                + ': ' + str(ticks[:20])
    return None


def _tracking_aligned(report):
    """The aligned stream tick a standby's RoleReport carries, or None
    while it is unsynchronized, degraded, diverged, or field-owning."""
    sync = (report or {}).get('sync')
    if not isinstance(sync, dict):
        return None
    aligned = (sync.get('tracking') or {}).get('aligned')
    return aligned if isinstance(aligned, int) else None


def scenario_source_restart(ctx):
    """Cold-restart the active peer's container — its state file
    dropped — and prove the tracking standby adopts the regressed
    checkpoint stream at its own run tick; a second leg promotes the
    tracked peer and cold-restarts the demoted peer, journaling the
    no-prior-alignment form."""
    case = Case('source-restart',
                'Regressed checkpoint stream adopts at the run tick',
                'cold-restarting the field owner\'s container — its '
                'host-side state file dropped, the journal retained — '
                'leaves the tracking standby adopting the regressed '
                'checkpoint stream without rewinding its run tick: '
                'the served snapshot tick stays monotonic and never '
                'below the pre-restart mark, /history stays '
                'newest-last, and the durable journal carries exactly '
                'one source_restarted entry per regression naming the '
                'resync tick, the prior alignment, and the resumed '
                'stream tick; the demoted peer\'s first pull records '
                'the no-prior-alignment form; the standby never '
                'spuriously promotes, the field\'s writer claim stands '
                'throughout, and the pair re-settles to one active '
                'and a tracking standby')
    try:
        cold_restart = ctx.get('cold_restart_controller')
        if cold_restart is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no cold-restart action — the '
                               'regression induction has no '
                               'documented seam')
        journals = ctx.get('journal_files') or {}
        journal = journals.get('standby')
        owner_journal = journals.get('active')
        if journal is None or owner_journal is None \
                or ctx.get('plant') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no journal-file paths for the '
                               'pair or no plant endpoint for the '
                               'fencing probes')
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        if active != 'active':
            return case.finish('inconclusive', 'the field owner is '
                               + active + ' — the rig\'s only '
                               'checkpoint-tracking peer — so no '
                               'tracked endpoint can adopt a '
                               'regressed stream')
        base, peer_base = ctx['active'], ctx['standby']
        case.observe('cold-restart target: active (' + base
                     + '); adopting peer standby (' + peer_base + ')')

        # The settled baseline: a tracking peer whose run tick stands
        # well past where a cold process resumes — the regression the
        # induction produces must be unambiguous.
        def tracking():
            report = _try_role(ctx, peer_base)
            if (report or {}).get('role') == 'standby' \
                    and _tracking_aligned(report) is not None:
                return report
            return None

        report = wait_for(tracking,
                          time.monotonic()
                          + SOURCE_RESTART_BASELINE_DEADLINE,
                          interval=SOURCE_RESTART_POLL)
        if report is None:
            return case.finish('inconclusive', 'the standby is not a '
                               'tracking peer — the regression has no '
                               'adopter')
        aligned0 = _tracking_aligned(report)
        snap0 = wait_for(
            lambda: (s.get('tick', 0) >= SOURCE_RESTART_MIN_TICK
                     and s or None)
            if (s := _try_snapshot(ctx, peer_base)) else None,
            time.monotonic() + SOURCE_RESTART_BASELINE_DEADLINE,
            interval=SOURCE_RESTART_POLL)
        if snap0 is None:
            return case.finish('inconclusive', 'the tracking peer\'s '
                               'run tick never reached '
                               + str(SOURCE_RESTART_MIN_TICK)
                               + ' — no room for a regressed stream')
        tick0 = snap0['tick']
        restarts0 = _source_restarts(_journal_entries(journal))
        ref = save_evidence(ctx['evidence_dir'],
                            'source-restart-before.json',
                            {'peer_tick': tick0, 'aligned': aligned0,
                             'source_restarts': restarts0})
        case.evidence('file', ref, 'pre-restart baseline: run tick, '
                      'stream alignment, journaled resyncs')
        case.observe('baseline: peer run tick ' + str(tick0)
                     + ', aligned at stream tick ' + str(aligned0))

        # Leg one: cold-restart the field owner. While its monitor is
        # down the tracking peer's checkpoint pulls miss — the armed
        # failover budget stays unreached on a healthy rig — and the
        # fresh process's first served checkpoint is the regressed
        # stream. The watch records the peer's served tick and role
        # plus the plant's fencing answers on every poll.
        watch = {'peer_ticks': [], 'peer_roles': [], 'fencing': []}
        promoted = []
        # The standby's armed failover budget in seconds — a promotion
        # past it is the documented bound on the outage, not a defect;
        # one inside it is the spurious promotion the contract forbids.
        budget = (ctx.get('failover_misses') or 0) * 0.1

        def restart_returned():
            report = _try_role(ctx, peer_base)
            if report is not None:
                watch['peer_roles'].append(
                    {'role': report.get('role'),
                     'aligned': _tracking_aligned(report)})
                if report.get('role') in ('promoting', 'active'):
                    promoted.append(
                        (report, time.monotonic() - stopped_at))
            snap = _try_snapshot(ctx, peer_base)
            if snap is not None:
                watch['peer_ticks'].append(snap.get('tick'))
            probe = _try_plant(ctx, {'op': 'step', 'dt': 0})
            if probe is not None:
                watch['fencing'].append(bool(_fenced(probe)))
            report = _try_role(ctx, base)
            if report is None or report.get('role') != 'active':
                return None
            return report

        stopped_at = time.monotonic()
        try:
            cold_restart('active')
        except Exception as exc:
            return case.finish('inconclusive', 'the cold-restart '
                               'action never completed: '
                               + str(exc)[:300])
        case.observe('cold restart action returned — watching the '
                     'tracked peer across the outage')
        returned = wait_for(restart_returned,
                            time.monotonic()
                            + SOURCE_RESTART_RETURN_DEADLINE,
                            interval=SOURCE_RESTART_POLL)
        resumed = (returned or {}).get('tick')
        problem = None
        if promoted:
            report, elapsed = promoted[0]
            if budget and elapsed > budget:
                problem = ('inconclusive', 'the tracked peer promoted '
                           + str(elapsed)[:5] + 's into the outage — '
                           'past the armed failover budget of '
                           + str(budget) + 's, the documented bound '
                           'rather than a defect')
            else:
                problem = ('failed', 'the tracked peer reported '
                           + str(report.get('role'))
                           + ' while the cold-restarted controller '
                           'was down — a spurious promotion inside '
                           'the armed failover budget: '
                           + json.dumps(report)[:400])
        elif returned is None:
            problem = ('inconclusive', 'the cold-restarted '
                       'controller never returned')
        elif not isinstance(resumed, int) or resumed >= aligned0:
            problem = ('inconclusive', 'the restarted peer resumed '
                       'at tick ' + str(resumed) + ' — not below the '
                       'last alignment ' + str(aligned0) + ', so the '
                       'induction never produced a regressed stream')
        case.observe('restarted peer '
                     + ('active again at tick ' + str(resumed)
                        if isinstance(resumed, int) else 'not back')
                     + '; tracked peer at '
                     + str(watch['peer_ticks'][-1]
                           if watch['peer_ticks'] else None))
        converged = None
        if problem is None:
            # The adoption itself: the peer reconverges onto the new
            # generation's stream — an alignment below the
            # pre-restart mark names the regression it just absorbed.
            # The watch keeps recording its served tick: the apply
            # lands under the generation offset here, so this window
            # is part of the monotonicity evidence.
            def realigned():
                report = _try_role(ctx, peer_base)
                snap = _try_snapshot(ctx, peer_base)
                if snap is not None:
                    watch['peer_ticks'].append(snap.get('tick'))
                aligned = _tracking_aligned(report)
                if (report or {}).get('role') == 'standby' \
                        and isinstance(aligned, int) \
                        and aligned < aligned0:
                    return report
                return None

            converged = wait_for(realigned,
                                 time.monotonic()
                                 + SOURCE_RESTART_SETTLE_DEADLINE,
                                 interval=SOURCE_RESTART_POLL)
            if converged is None:
                problem = ('failed', 'the tracked peer never '
                           'reconverged onto the regressed stream')
        ref = save_evidence(ctx['evidence_dir'],
                            'source-restart-watch.json', watch)
        case.evidence('file', ref, 'the tracked peer\'s roles and '
                      'ticks plus the field\'s fencing answers across '
                      'the cold restart')
        if problem is not None:
            return case.finish(problem[0], problem[1])
        case.observe('peer tracking the new generation, aligned at '
                     'stream tick '
                     + str(_tracking_aligned(converged)))
        rewound = _tick_rewind(watch['peer_ticks'],
                               tick0 - SOURCE_RESTART_JITTER,
                               SOURCE_RESTART_JITTER)
        if rewound is not None:
            return case.finish('failed', 'the tracked peer\'s served '
                               'tick rewound across the adoption: '
                               'pre-restart ' + str(tick0)
                               + ', observed '
                               + json.dumps(watch['peer_ticks'][:30]))
        if watch['fencing'] and not all(watch['fencing']):
            return case.finish('failed', 'the field\'s writer claim '
                               'lapsed across the cold restart — a '
                               'fencing probe answered unclaimed')
        case.observe('tracked peer\'s served tick stayed monotonic, '
                     'never below ' + str(tick0) + '; the field\'s '
                     'claim fenced throughout')

        # /history on the adopting peer: the run-tick domain it
        # attributes into never rewound, so the served rows stay
        # newest-last with no out-of-order ticks.
        _, history = http_json('GET', peer_base + '/history?since=0')
        disorder = _history_disorder(history, SOURCE_RESTART_JITTER)
        ref = save_evidence(ctx['evidence_dir'],
                            'source-restart-history.json', history)
        case.evidence('file', ref, 'the tracked peer\'s /history '
                      'across the adoption')
        if disorder:
            return case.finish('failed', '/history is not '
                               'newest-last across the resync: '
                               + disorder)

        # The durable audit record: exactly one source_restarted entry
        # for the regression — its entry tick the run tick the resync
        # landed at, was_aligned the alignment the regression broke,
        # resumed_at the regressed checkpoint's own tick.
        resynced = {}

        def journaled():
            try:
                items = _journal_entries(journal)
            except (OSError, ValueError) as exc:
                resynced['error'] = str(exc)
                return None
            resynced['items'] = items
            restarts = _source_restarts(items)
            resynced['restarts'] = restarts
            return restarts if len(restarts) > len(restarts0) \
                else None

        wait_for(journaled,
                 time.monotonic() + SOURCE_RESTART_JOURNAL_DEADLINE,
                 interval=SOURCE_RESTART_POLL)
        restarts1 = resynced.get('restarts') or []
        added = restarts1[len(restarts0):] \
            if restarts1[:len(restarts0)] == restarts0 else None
        _, served_journal = http_json('GET', peer_base + '/journal')
        served_restarts = _source_restarts(_journal_list(
            served_journal))
        ref = save_evidence(ctx['evidence_dir'],
                            'source-restart-journal.json',
                            {'path': str(journal), 'added': added,
                             'restarts': restarts1,
                             'served_restarts': served_restarts,
                             'error': resynced.get('error')})
        case.evidence('file', ref, 'the tracked peer\'s durable '
                      'journal around the resync, with the served '
                      'journal\'s resync records')
        if added is None:
            return case.finish('failed', 'the tracked peer\'s journal '
                               'changed existing records — attribution '
                               'corrupted: ' + str(resynced.get('error')))
        if len(added) != 1:
            return case.finish('failed', 'expected exactly one '
                               'source_restarted entry for the '
                               'regression, found ' + str(len(added))
                               + ': ' + json.dumps(added)[:300])
        entry = added[0]
        if not isinstance(entry.get('was_aligned'), int) \
                or entry['was_aligned'] < aligned0:
            return case.finish('failed', 'the journaled resync does '
                               'not carry the prior alignment ('
                               + str(aligned0) + '): '
                               + json.dumps(entry))
        if not isinstance(entry.get('resumed_at'), int) \
                or entry['resumed_at'] >= entry['was_aligned']:
            return case.finish('failed', 'the journaled resync\'s '
                               'resumed tick is not below the prior '
                               'alignment — the stream never '
                               'regressed or the fields misreport: '
                               + json.dumps(entry))
        if not isinstance(entry.get('tick'), int) \
                or entry['tick'] < tick0:
            return case.finish('failed', 'the journaled resync is '
                               'attributed below the run\'s own '
                               'pre-restart tick — the adoption '
                               'rewound the tick domain: '
                               + json.dumps(entry))
        # The served journal answers from the same record: the
        # resync the durable file carries must appear identically on
        # the monitor's view.
        if not any(s.get('tick') == entry['tick']
                   and s.get('was_aligned') == entry['was_aligned']
                   and s.get('resumed_at') == entry['resumed_at']
                   for s in served_restarts):
            return case.finish('failed', 'the served journal does '
                               'not carry the journaled resync — the '
                               'served and durable records diverge: '
                               + json.dumps(served_restarts)[-300:])
        case.observe('journaled resync at run tick '
                     + str(entry['tick']) + ': was_aligned '
                     + str(entry['was_aligned']) + ', resumed_at '
                     + str(entry['resumed_at']))

        # The cold-resume evidence the retained journal file owes: the
        # restarted peer's file opens a new lifetime whose boundary
        # marker records tick 0 — a warm resume would carry the
        # restored tick instead.
        bounds = {}

        def cold_boundary():
            try:
                bounds['items'] = _journal_entries(owner_journal)
            except (OSError, ValueError) as exc:
                bounds['error'] = str(exc)
                return None
            marks = [item['run_boundary'] for item in bounds['items']
                     if 'run_boundary' in item]
            bounds['marks'] = marks
            return marks if len(marks) >= 2 else None

        marks = wait_for(cold_boundary,
                         time.monotonic()
                         + SOURCE_RESTART_JOURNAL_DEADLINE,
                         interval=SOURCE_RESTART_POLL)
        if marks is None or (marks[-1] or {}).get('tick') != 0:
            return case.finish('failed', 'the cold-restarted peer\'s '
                               'journal lacks a run-boundary marker at '
                               'tick 0 — the cold resume is not on '
                               'the record: '
                               + str(bounds.get('error')
                                     or (marks or [])[-1:]))
        case.observe('the restarted peer\'s journal opened run '
                     + str(marks[-1].get('run')) + ' at tick 0 — '
                     'the cold start on the durable record')

        # Leg two — the no-prior-alignment form. Demote the restarted
        # peer, promote the tracked one, then cold-restart the demoted
        # peer's container: the fresh process's startup claim preempts
        # the promoted writer, whose first fenced write demotes it in
        # place — its alignment cleared by the demotion, so the first
        # pull of the new stream regresses against the run's own tick.
        status, body = http_json('POST', base + '/demote')
        case.observe('demote the restarted peer for leg two: '
                     + str(status) + ' ' + json.dumps(body)[:200])
        if status != 200:
            return case.finish('failed', 'demote refused: '
                               + json.dumps(body)[:300])
        promoted_peer = None
        deadline = time.monotonic() + SOURCE_RESTART_SETTLE_DEADLINE
        while time.monotonic() < deadline and promoted_peer is None:
            try:
                status, body = http_json('POST', peer_base + '/promote')
                if status == 200:
                    promoted_peer = body
                else:
                    time.sleep(SOURCE_RESTART_POLL)
            except urllib.error.HTTPError as exc:
                if exc.code == 409:
                    time.sleep(SOURCE_RESTART_POLL)
                else:
                    raise
        if promoted_peer is None:
            return case.finish('failed', 'the tracked peer never '
                               'promoted — leg two has no writer to '
                               'fence')
        case.observe('tracked peer promoted: '
                     + json.dumps(promoted_peer)[:200])
        snap2 = _snapshot(ctx, peer_base)
        tick2 = snap2.get('tick') or 0

        leg2_stopped = time.monotonic()
        try:
            cold_restart('active')
        except Exception as exc:
            return case.finish('inconclusive', 'the second '
                               'cold-restart action never completed: '
                               + str(exc)[:300])
        leg2 = {'peer_ticks': [], 'peer_roles': [], 'fencing': [],
                'owner_roles': []}
        seen = {'demoted': False}
        repromoted = []

        def leg2_settled():
            report = _try_role(ctx, peer_base)
            if report is not None:
                leg2['peer_roles'].append(report.get('role'))
                if report.get('role') in ('demoting', 'standby'):
                    seen['demoted'] = True
                elif report.get('role') in ('promoting', 'active') \
                        and seen['demoted']:
                    repromoted.append(
                        (report, time.monotonic() - leg2_stopped))
            snap = _try_snapshot(ctx, peer_base)
            if snap is not None:
                leg2['peer_ticks'].append(snap.get('tick'))
            probe = _try_plant(ctx, {'op': 'step', 'dt': 0})
            if probe is not None:
                leg2['fencing'].append(bool(_fenced(probe)))
            owner = _try_role(ctx, base)
            if owner is not None:
                leg2['owner_roles'].append(owner.get('role'))
            if not seen['demoted'] or owner is None \
                    or owner.get('role') != 'active':
                return None
            if (report or {}).get('role') == 'standby' \
                    and _tracking_aligned(report) is not None:
                return {'owner': owner, 'peer': report}
            return None

        settled = wait_for(leg2_settled,
                           time.monotonic()
                           + SOURCE_RESTART_SETTLE_DEADLINE,
                           interval=SOURCE_RESTART_POLL)
        ref = save_evidence(ctx['evidence_dir'],
                            'source-restart-leg2-watch.json', leg2)
        case.evidence('file', ref, 'the fenced demotion and the '
                      'demoted-peer first pull across the second '
                      'cold restart')
        if repromoted:
            report, elapsed = repromoted[0]
            if budget and elapsed > budget:
                return case.finish('inconclusive', 'the demoted peer '
                                   're-promoted ' + str(elapsed)[:5]
                                   + 's into the second outage — past '
                                   'the armed failover budget of '
                                   + str(budget) + 's, the documented '
                                   'bound rather than a defect')
            return case.finish('failed', 'the fenced peer re-took '
                               'the field inside the armed failover '
                               'budget after its demotion: '
                               + json.dumps(report)[:400])
        if 'active' not in leg2['owner_roles']:
            return case.finish('inconclusive', 'the second cold '
                               'restart never returned the demoted '
                               'peer')
        if not seen['demoted']:
            return case.finish('failed', 'the promoted writer was '
                               'never fenced off the field by the '
                               'cold-restarted peer\'s claim')
        if settled is None:
            return case.finish('failed', 'the demoted peer never '
                               'reconverged onto the new stream')
        rewound = _tick_rewind(leg2['peer_ticks'],
                               tick2 - SOURCE_RESTART_JITTER,
                               SOURCE_RESTART_JITTER)
        if rewound is not None:
            return case.finish('failed', 'the demoted peer\'s served '
                               'tick rewound across the no-alignment '
                               'adoption: induction-time '
                               + str(tick2) + ', observed '
                               + json.dumps(leg2['peer_ticks'][:30]))
        if leg2['fencing'] and not all(leg2['fencing']):
            return case.finish('failed', 'the field\'s writer claim '
                               'lapsed across the second cold '
                               'restart — a fencing probe answered '
                               'unclaimed')
        owner_tick = (settled['owner'] or {}).get('tick')
        if not isinstance(owner_tick, int) or owner_tick >= tick2:
            return case.finish('inconclusive', 'the demoted peer '
                               'resumed at tick ' + str(owner_tick)
                               + ' — not below the adopting peer\'s '
                               + str(tick2) + ', so the second '
                               'induction never produced a regressed '
                               'stream')

        # The same journaled shape, no-prior-alignment form: exactly
        # one new source_restarted entry carrying was_aligned null —
        # the demotion cleared the alignment — the resumed stream
        # tick, and an entry tick at the run's own.
        resynced2 = {}

        def journaled2():
            try:
                items = _journal_entries(journal)
            except (OSError, ValueError) as exc:
                resynced2['error'] = str(exc)
                return None
            resynced2['items'] = items
            restarts = _source_restarts(items)
            resynced2['restarts'] = restarts
            return restarts if len(restarts) > len(restarts1) \
                else None

        wait_for(journaled2,
                 time.monotonic() + SOURCE_RESTART_JOURNAL_DEADLINE,
                 interval=SOURCE_RESTART_POLL)
        restarts2 = resynced2.get('restarts') or []
        added2 = restarts2[len(restarts1):] \
            if restarts2[:len(restarts1)] == restarts1 else None
        items2 = resynced2.get('items') or []
        ref = save_evidence(
            ctx['evidence_dir'], 'source-restart-leg2-journal.json',
            {'path': str(journal), 'added': added2,
             'restarts': restarts2,
             'entries_added':
                 items2[len(resynced.get('items') or []):],
             'error': resynced2.get('error')})
        case.evidence('file', ref, 'the demoted peer\'s journal '
                      'through the fenced demotion and regressed '
                      'first pull')
        if added2 is None:
            return case.finish('failed', 'the demoted peer\'s '
                               'journal changed existing records or '
                               'never carried the resync: '
                               + str(resynced2.get('error')))
        if len(added2) != 1:
            return case.finish('failed', 'expected exactly one '
                               'source_restarted entry for the '
                               'demoted-peer regression, found '
                               + str(len(added2)) + ': '
                               + json.dumps(added2)[:300])
        entry2 = added2[0]
        if entry2.get('was_aligned') is not None:
            return case.finish('failed', 'the demoted peer\'s first '
                               'pull journaled a prior alignment — '
                               'the no-prior-alignment form was not '
                               'exercised: ' + json.dumps(entry2))
        if not isinstance(entry2.get('resumed_at'), int) \
                or entry2['resumed_at'] >= tick2:
            return case.finish('failed', 'the demoted-peer resync\'s '
                               'resumed tick is not below the run\'s '
                               'own tick — the no-alignment '
                               'regression never happened or the '
                               'fields misreport: '
                               + json.dumps(entry2))
        if not isinstance(entry2.get('tick'), int) \
                or entry2['tick'] < tick2:
            return case.finish('failed', 'the demoted-peer resync is '
                               'attributed below the run\'s own '
                               'tick: ' + json.dumps(entry2))
        case.observe('demoted-peer resync journaled at run tick '
                     + str(entry2['tick']) + ': was_aligned null, '
                     'resumed_at ' + str(entry2['resumed_at']))

        # Attribution across both legs: the adopting peer's file is
        # one process lifetime, so every journaled entry's run-tick
        # attribution must read non-decreasing in seq order.
        seqs = [item['entry'].get('seq') for item in items2
                if isinstance(item.get('entry'), dict)]
        entry_ticks = [item['entry'].get('tick') for item in items2
                       if isinstance(item.get('entry'), dict)]
        if any(not isinstance(seq, int) for seq in seqs) \
                or seqs != sorted(seqs) or len(set(seqs)) != len(seqs) \
                or any(not isinstance(tick, int) for tick in entry_ticks) \
                or _tick_rewind(entry_ticks, 0,
                                SOURCE_RESTART_JITTER) is not None:
            return case.finish('failed', 'journal attribution is not '
                               'monotonic in seq order across the '
                               'adoptions: seqs ' + str(seqs[:20])
                               + ', ticks ' + str(entry_ticks[:20]))

        # The second cold restart owes the same cold-resume marker on
        # the restarted peer's retained journal: one more lifetime,
        # opened at tick 0.
        marks2 = [item['run_boundary'] for item in
                  _journal_entries(owner_journal)
                  if 'run_boundary' in item]
        ref = save_evidence(ctx['evidence_dir'],
                            'source-restart-boundaries.json',
                            {'owner_journal': str(owner_journal),
                             'owner_boundaries': marks2})
        case.evidence('file', ref, 'the cold-restarted peer\'s '
                      'run-boundary markers — each lifetime opened '
                      'cold')
        if len(marks2) != len(marks) + 1 \
                or (marks2[-1] or {}).get('tick') != 0:
            return case.finish('failed', 'the second cold restart '
                               'did not add a run-boundary marker at '
                               'tick 0 to the restarted peer\'s '
                               'journal: '
                               + json.dumps(marks2)[-300:])
        case.observe('cold-resume markers on the restarted peer\'s '
                     'journal: ' + json.dumps(marks2))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))


# --------------------------------------------------------------------
# The declared freshness budget (WW-OPS-003's stale-data surface,
# WW-ALM-003's rule that stale data never presents as a healthy
# last-known value): the rig model's `net-flow` field input carries a
# declared `stale_after_ticks` — a per-point declaration, not a global
# rule. Stopping the writer-holding controller freezes the shared
# plant's stepping (the sim-net single-writer claim means no surviving
# peer steps it), so the tracking standby's scans outrun the frozen
# driver stamps: the budgeted point must present Uncertain(Stale) while
# the undeclared `level-primary` keeps reporting Good. The outage is
# bounded by the standby's armed failover budget — the writer's restart
# lands inside it, the resumed checkpoint stream realigns the tracking
# peer's tick domain to the plant's, and the budgeted point returns
# Good. Should the restart ever land late, the armed self-promotion is
# the documented bound: the promoted peer reclaims the writer and
# resumes stepping, and the case reports whether the point recovers on
# that path instead. `GET /history` preserves the stale interval either
# way — the durable evidence when live polling lands late.

STALE_FRESHNESS_POLL = 0.2   # cadence polling the surviving peer mid-freeze
STALE_HOLD_TICKS = 10        # keep sampling past first stale — the relapse check
FREEZE_MAX_TICKS = 60        # hard bound on the outage, under the armed failover budget
STALE_WALL_DEADLINE = 20     # backstop when the peer's tick stops serving
STALE_RECOVER_DEADLINE = 45  # bound on the point returning Good after the restart
STALE_RETURN_DEADLINE = 45   # bound on the restarted writer's monitor returning

# The probe pair out of the served SignalIndex: the model's one declared
# stale_after_ticks point, and an undeclared field input sharing the
# same frozen driver — the per-point-contract contrast.
STALE_BUDGETED_NAME = 'net-flow'
STALE_COMPARISON_NAME = 'level-primary'


def scenario_stale_freshness(ctx):
    """Freeze the shared plant's stepping by stopping the writer-holding
    controller: the declared-budget input presents Uncertain(Stale) on
    the surviving standby while an undeclared field input keeps Good;
    the writer's restart realigns the tracking peer inside the armed
    failover bound and the point returns Good — /history preserving the
    stale interval."""
    case = Case('stale-freshness',
                'Declared freshness budget presents stale on writer loss',
                'stopping the writer-holding controller freezes the '
                'shared plant\'s stepping, so the field input carrying '
                'the declared stale_after_ticks presents Uncertain(Stale) '
                'on the surviving standby while an undeclared field input '
                'keeps Good; the writer\'s restart lands inside the armed '
                'failover bound, the resumed checkpoints realign the '
                'tracking peer, and the point returns Good — with the '
                '/history record preserving the stale interval')
    try:
        stop = ctx.get('stop_controller')
        start = ctx.get('start_controller')
        if stop is None or start is None:
            return case.finish('inconclusive', 'the run context carries '
                               'no controller stop/start action — the '
                               'writer-loss induction has no documented '
                               'seam')
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        peer = 'standby' if active == 'active' else 'active'
        base, peer_base = ctx[active], ctx[peer]
        budget = ctx.get('failover_misses')
        case.observe('seam=writer-stop: stop ' + active + ' (' + base
                     + '), observe ' + peer + ' (' + peer_base + ')'
                     + (', failover budget ' + str(budget) + ' misses'
                        if budget else ''))

        # The probe pair out of the served index. The freshness budget
        # itself is model data — the index names the declared point by
        # its signal name.
        _, signals = http_json('GET', peer_base + '/signals')
        named = {entry.get('name'): entry
                 for entry in signals.get('points', [])}
        budgeted = named.get(STALE_BUDGETED_NAME)
        comparison = named.get(STALE_COMPARISON_NAME)
        ref = save_evidence(ctx['evidence_dir'],
                            'stale-freshness-signals.json',
                            {'budgeted': budgeted,
                             'comparison': comparison})
        case.evidence('file', ref, 'the probe points\' served entries')
        if budgeted is None or comparison is None:
            return case.finish('inconclusive', 'the rig model declares '
                               'no ' + STALE_BUDGETED_NAME + '/'
                               + STALE_COMPARISON_NAME
                               + ' field input — the freshness probe is '
                               'not declared')
        b_point, c_point = budgeted['point'], comparison['point']
        case.observe('budgeted probe: ' + STALE_BUDGETED_NAME
                     + ' point ' + str(b_point) + '; comparison: '
                     + STALE_COMPARISON_NAME + ' point ' + str(c_point))

        # The pre-freeze baseline: the surviving peer a tracking
        # standby, both probes Good.
        def tracking():
            try:
                report = _role(ctx, peer_base)
            except Exception:
                return None
            return report if report.get('role') == 'standby' \
                and 'tracking' in (report.get('sync') or {}) else None

        if wait_for(tracking, time.monotonic() + 45,
                    interval=STALE_FRESHNESS_POLL) is None:
            return case.finish('inconclusive', 'the surviving peer is '
                               'not a tracking standby — the writer-loss '
                               'induction has no observation point')
        baseline = _snapshot(ctx, peer_base)
        baseline_q = {p: _sample_quality(baseline, p)
                      for p in (b_point, c_point)}
        case.observe('baseline qualities: ' + STALE_BUDGETED_NAME + '='
                     + str(baseline_q[b_point]) + ' '
                     + STALE_COMPARISON_NAME + '='
                     + str(baseline_q[c_point]))
        if baseline_q[b_point] != 'good':
            return case.finish('failed', 'the budgeted point presents '
                               + str(baseline_q[b_point])
                               + ' before any induction — the declared '
                               'budget misfires on a healthy rig')
        if baseline_q[c_point] != 'good':
            return case.finish('inconclusive', 'the comparison point '
                               'presents ' + str(baseline_q[c_point])
                               + ' before the freeze — no healthy '
                               'baseline to contrast against')

        # Induce: stop the writer. The shared plant's stamps freeze;
        # the tracking peer's scans outrun them while its checkpoint
        # pulls miss. The freeze is tick-bounded — the peer's own scan
        # tick paces the miss cadence — and capped under the armed
        # failover budget so the restart lands first.
        try:
            stop(active)
        except Exception as exc:
            return case.finish('inconclusive', 'the writer-stop '
                               'induction never completed: '
                               + str(exc)[:300])
        case.observe('writer stopped — polling ' + peer)

        tick0 = None
        stale_tick = None
        freeze_obs = []
        degraded = False
        promoted = False
        relapse = None
        comparison_seen = set()
        wall = time.monotonic() + STALE_WALL_DEADLINE
        while time.monotonic() < wall:
            try:
                report = _role(ctx, peer_base)
                snap = _snapshot(ctx, peer_base)
            except Exception:
                time.sleep(STALE_FRESHNESS_POLL)
                continue
            tick = snap.get('tick') or 0
            if tick0 is None:
                tick0 = tick
            sync = report.get('sync') or {}
            sync_key = next(iter(sync), None)
            if sync_key == 'degraded':
                degraded = True
            if report.get('role') in ('promoting', 'active'):
                promoted = True
            qb = _sample_quality(snap, b_point)
            qc = _sample_quality(snap, c_point)
            freeze_obs.append({'tick': tick, 'budgeted': qb,
                               'comparison': qc,
                               'role': report.get('role'),
                               'sync': sync_key})
            if qc is not None:
                comparison_seen.add(qc)
            if qb == 'uncertain:stale' and stale_tick is None:
                stale_tick = tick
            elif stale_tick is not None and qb == 'good':
                relapse = {'tick': tick}
            if promoted or relapse:
                break
            if stale_tick is not None \
                    and tick - stale_tick >= STALE_HOLD_TICKS:
                break
            if tick - tick0 >= FREEZE_MAX_TICKS:
                break
            time.sleep(STALE_FRESHNESS_POLL)
        case.observe('freeze: ' + str(len(freeze_obs))
                     + ' polls over ' + str((freeze_obs[-1]['tick']
                                             - tick0)
                                            if freeze_obs and tick0
                                            is not None else 0)
                     + ' peer ticks; stale first seen '
                     + ('at tick ' + str(stale_tick) if stale_tick
                        is not None else 'never')
                     + (', peer promoted' if promoted else ''))

        # End the outage inside the failover bound: the restarted writer
        # resumes its persisted run, reclaims the plant, and its
        # checkpoint stream realigns the tracking peer — the documented
        # recovery path. (If the peer already promoted, the freeze ended
        # at the armed failover bound instead; the restart is then
        # fenced out of the field and the promoted peer is the writer —
        # the recovery check below reads that path's result either way.)
        try:
            start(active)
        except Exception as exc:
            return case.finish('inconclusive', 'the writer restart '
                               'never completed: ' + str(exc)[:300])
        case.observe('writer restart issued')

        # Recovery on the observing peer: the budgeted point back to
        # Good. On the restart path the resumed checkpoints rewind the
        # tracking peer's tick to the plant's — the lag closes and the
        # declared budget clears.
        def back_to_good():
            try:
                report = _role(ctx, peer_base)
                snap = _snapshot(ctx, peer_base)
            except Exception:
                return None
            if _sample_quality(snap, b_point) != 'good':
                return None
            return {'role': report.get('role'), 'tick': snap.get('tick')}

        recovered = wait_for(back_to_good,
                             time.monotonic() + STALE_RECOVER_DEADLINE,
                             interval=STALE_FRESHNESS_POLL)
        ref = save_evidence(
            ctx['evidence_dir'], 'stale-freshness-freeze.json',
            {'seam': 'writer-stop', 'promoted': promoted,
             'observations': freeze_obs,
             'recovery': recovered})
        case.evidence('file', ref, 'per-poll qualities through the '
                      'freeze and the recovery read')

        # The durable record: the peer's /history must preserve the
        # stale interval even where live polling landed late, bracketed
        # by Good — never interrupted by a healthy last-known value.
        _, history_body = http_json(
            'GET', peer_base + '/history?point=' + str(b_point)
            + '&point=' + str(c_point) + '&since=0')
        b_hist = _history_qualities(history_body, b_point)
        c_hist = _history_qualities(history_body, c_point)
        stale_pos = [i for i, (_s, q, _t) in enumerate(b_hist)
                     if q == 'uncertain:stale']
        c_stale = [s for s, q, _t in c_hist if q == 'uncertain:stale']
        interval = None
        if stale_pos:
            first, last = stale_pos[0], stale_pos[-1]
            interval = {
                'first_seq': b_hist[first][0],
                'last_seq': b_hist[last][0],
                'ticks': [t for _s, _q, t in b_hist[first:last + 1]],
                'bracket': [{'seq': s, 'quality': q, 'tick': t}
                            for s, q, t in b_hist[max(0, first - 1):
                                                  last + 2]],
                'good_inside': any(q == 'good'
                                   for _s, q, _t
                                   in b_hist[first:last + 1]),
                'recovered': any(q == 'good'
                                 for _s, q, _t in b_hist[last + 1:]),
            }
        ref = save_evidence(
            ctx['evidence_dir'], 'stale-freshness-history.json',
            {'budgeted': {'point': b_point, 'interval': interval,
                          'samples': len(b_hist)},
             'comparison': {'point': c_point, 'stale_seqs': c_stale}})
        case.evidence('file', ref, 'the stale interval in the peer\'s '
                      'served history')

        # Verdicts, in contract order: an induction that never took
        # effect is inconclusive; everything else names the broken
        # clause.
        freeze_proven = degraded or promoted or stale_tick is not None \
            or 'uncertain:stale' in comparison_seen
        if not freeze_proven:
            return case.finish('inconclusive', 'the writer-stop '
                               'induction never took effect — the peer '
                               'kept tracking and no stamp froze, so no '
                               'staleness absence can be attributed')
        if 'uncertain:stale' in comparison_seen or c_stale:
            return case.finish('failed', 'the undeclared comparison '
                               'point presented stale — the budget '
                               'leaked past its declaration')
        if comparison_seen - {'good'}:
            return case.finish('failed', 'the undeclared comparison '
                               'point presented '
                               + str(sorted(comparison_seen - {'good'}))
                               + ' during the freeze — expected Good '
                               'throughout')
        if relapse:
            return case.finish('failed', 'the stale interval reverted '
                               'to a healthy last-known value at tick '
                               + str(relapse['tick'])
                               + ' while the plant stayed frozen')
        if stale_tick is None and not stale_pos:
            return case.finish('failed', 'the budgeted point never '
                               'presented Uncertain(Stale) while the '
                               'plant\'s stepping was frozen')
        if recovered is None:
            note = 'the point did not return Good after the writer '
            if promoted:
                note += ('lost the field to the peer\'s failover-budget '
                         'self-promotion — the promoted run resumes '
                         'stepping but its scan ticks lead the frozen '
                         'stamps by the outage length, so the lag never '
                         'closes')
            else:
                note += ('resumed stepping — the tracking peer never '
                         'realigned inside ' + str(STALE_RECOVER_DEADLINE)
                         + 's')
            return case.finish('failed', note)
        if not stale_pos:
            return case.finish('failed', 'the /history record does not '
                               'preserve the stale interval live '
                               'polling observed')
        if interval['good_inside']:
            return case.finish('failed', 'a healthy last-known value '
                               'sits inside the recorded stale interval')
        if not interval['recovered']:
            return case.finish('failed', 'the /history record never '
                               'shows the interval closing — no Good '
                               'after the last stale sample')
        case.observe('stale interval: seqs '
                     + str(interval['first_seq']) + '..'
                     + str(interval['last_seq']) + ', '
                     + str(len(stale_pos)) + ' samples, bracketed by '
                     'Good in history')
        if promoted:
            case.observe('the peer\'s failover-budget self-promotion '
                         'reclaimed the writer and the point recovered '
                         'on the promoted run')
        else:
            case.observe('the restarted writer\'s checkpoints realigned '
                         'the tracking peer — the point returned Good '
                         'inside the failover bound')

        # Leave the rig the way the suite expects it: the restarted
        # endpoint serving again. Only meaningful when no promotion
        # happened — a promoted peer owns the field and the restarted
        # container stays fenced out.
        if not promoted:
            def serving_again():
                try:
                    report = _role(ctx, base)
                except Exception:
                    return None
                return report if report.get('role') == 'active' else None

            back = wait_for(serving_again,
                            time.monotonic() + STALE_RETURN_DEADLINE,
                            interval=STALE_FRESHNESS_POLL)
            if back is None:
                return case.finish('inconclusive', 'the restarted '
                                   'writer never returned')
            case.observe('the restarted writer is serving as active '
                         'at tick ' + str(back.get('tick')))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))

# --------------------------------------------------------------------
# The plant-link boundary (WW-OPS-003's communication confidence and
# WW-FND-002's remote-I/O evidence, ahead of HQ-5's hardware link-loss
# checks, pinning the settled #507 field-loss contract and the #531
# restart-window half of the claim contract as per-revision lane
# evidence): the runner-owned plant stop/start action severs both
# controllers' remote-driver connections mid-run — the non-cyclic
# remote form of decision 78's exchange-loss shape, and unlike a
# per-point fault a dead plant fails every field point at once at the
# link boundary. The active's telemetry must degrade honestly — held
# values re-marked Bad past the read boundary, the driver link
# reporting disconnected with per-direction failures counted and still
# advancing and a last_error, the loss journaled as quality_changed —
# while scans keep running and the pair's roles hold: the standby's
# checkpoint-pull heartbeat is peer-to-peer, not field traffic, so it
# stays tracking and never promotes on field loss — its promotion path
# still following the convergence gate. A restarted plant is a new
# server lifetime — its single-writer claim died with the old process —
# so the return first serves a fail-closed unclaimed window every
# third attachment's mutation probe must answer `unclaimed` (never
# `stepped`, never a silent write) while reads stay open; recovery
# means the field owner re-attaches and re-arms the recorded claim
# through `ensure_writer`: probes fence again, reads return Good with
# the outage's failures still counted in io_health rather than
# silently reset, and no controller restarts.

LINK_POLL = 1.0                # cadence watching the pair mid-outage
LINK_DEGRADE_DEADLINE = 45     # bound on the telemetry degrading
LINK_SETTLE = 6.0              # extra role watch once degradation shows
LINK_RECOVERY_DEADLINE = 90    # bound on the plant's return + re-claim
LINK_WINDOW_POLL = 0.2         # cadence probing the fail-closed window


def _try_role(ctx, base):
    """`/role` or None — for the outage watch a dropped read is one
    lost poll, not the leg's verdict."""
    try:
        return _role(ctx, base)
    except Exception:
        return None


def _plant_probe(ctx, request, timeout=5):
    """One request/response against the run's plant server on a fresh
    connection — the `dcs-sim-net` wire protocol on the published
    endpoint ctx['plant'] carries. The link-loss scenario uses it for
    the field's own evidence: `list_points` is the census of points the
    boundary fails at once, and a `step` mutation is the fencing probe
    — answered `fenced` while any attachment holds the plant's
    single-writer claim, `unclaimed` while none does: the field fails
    closed across a restart, so `unclaimed` means "waiting on the
    owner's re-arm", not an open window. Each probe takes a new
    connection because the outage it watches is exactly a dead
    listener; the probe never sends `claim_writer` — claiming from
    here would preempt the field owner it is checking for. The probe
    stays on the raw client rather than `dcs-plant-ctl`'s `step`
    subcommand for the same reason: the tool wraps its mutation in its
    own conditional claim — `ensure_writer` under the tool token, then
    `release_writer` — so an unclaimed field would answer `stepped`
    under a fleeting tool claim instead of the `unclaimed` verdict the
    leg watches for, and that claim window could fence the field
    owner's re-arm it polls for. The bare `step` is a request shape
    the tool does not expose."""
    stream = _plant_connect(ctx, timeout=timeout)
    try:
        stream.settimeout(timeout)
        return _plant_request(stream, request)
    finally:
        stream.close()


def _try_plant(ctx, request):
    """`_plant_probe` or None — a refused probe is one lost poll, not
    the leg's verdict."""
    try:
        return _plant_probe(ctx, request)
    except Exception:
        return None


def _fenced(response):
    """Whether a fencing probe's answer says a writer claim stands —
    the shared field refused a third attachment's mutation."""
    return (response or {}).get('error', {}).get('kind') == 'fenced'


def _sample_quality(snapshot, point):
    """The point's latest served quality flattened for comparison —
    'good', 'uncertain:stale', 'bad:communication_fault' — or None when
    no sample exists."""
    for entry in snapshot.get('points', []):
        if entry.get('point') == point and entry.get('sample'):
            quality = entry['sample'].get('quality')
            if isinstance(quality, dict):
                kind, reason = next(iter(quality.items()))
                return kind + ':' + str(reason)
            return quality
    return None


def scenario_plant_link_loss(ctx):
    """Stop the run's plant container mid-run, prove the settled
    field-loss contract through the monitor and the durable journal,
    then restart it, prove the fail-closed unclaimed window, and prove
    the recorded owner's re-arm and recovery without a restart."""
    case = Case('plant-link-loss',
                'Plant-link loss degrades honestly and recovers',
                'stopping the run\'s plant container leaves the active '
                'scanning with its field reads Bad at the link '
                'boundary — io_health counting and advancing the '
                'per-direction failures, the driver link reporting '
                'disconnected, a last_error recorded, the loss '
                'journaled as quality_changed — the standby staying '
                'tracking with its promotion path on the convergence '
                'gate; restarting the plant serves a fail-closed '
                'unclaimed window every mutation probe answers '
                'unclaimed — never stepped, never a silent write — '
                'while reads stay open; the recorded owner\'s re-attach '
                're-arms the claim through ensure_writer so probes '
                'fence again, reads return Good with the outage\'s '
                'failures still counted, and no controller restarts')

    def role_violation(name, report, expected_roles, seen):
        ref = save_evidence(ctx['evidence_dir'],
                            'plant-link-loss-roles.json',
                            {'expected': expected_roles,
                             'offender': report, 'seen': seen})
        case.evidence('file', ref)
        return case.finish('failed', 'field loss moved ' + name
                           + ' to role ' + str(report.get('role'))
                           + ': ' + json.dumps(report)[:300])

    try:
        stop = ctx.get('stop_plant')
        start = ctx.get('start_plant')
        if stop is None or start is None or not ctx.get('plant'):
            return case.finish('inconclusive', 'the run context '
                               'carries no plant stop/start action or '
                               'plant address')
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        peer = 'standby' if active == 'active' else 'active'
        base, peer_base = ctx[active], ctx[peer]
        expected = {active: 'active', peer: 'standby'}
        case.observe('field owner: ' + active + ' (' + base + ')')

        # The baseline: the field census names the points the link
        # boundary fails at once, and the fencing probe proves the
        # writer claim the outage must lose and recovery must re-take.
        # The served journal cursor opens the loss-journaled leg, the
        # standby's sync opens the convergence-gate leg, and the
        # baseline health and tick open the advancing-counters and
        # no-restart legs.
        census = _try_plant_ctl(ctx, 'list')
        points = (census or {}).get('points', [])
        field_in = sorted(entry.get('point') for entry in points
                          if entry.get('direction') == 'in')
        field_out = any(entry.get('direction') == 'out'
                        for entry in points)
        probe = _try_plant(ctx, {'op': 'step', 'dt': 0})
        before = _try_snapshot(ctx, base)
        try:
            _, journal_body = http_json('GET', base + '/journal?since=0')
            cursor = _journal_seqs(journal_body)
            cursor = cursor[-1] if cursor else 0
        except Exception as exc:
            return case.finish('inconclusive', 'the active\'s journal '
                               'never answered its baseline cursor: '
                               + str(exc)[:200])
        peer_report = _try_role(ctx, peer_base)
        write_target = None
        for entry in sorted(points, key=lambda item: item.get('point')):
            sample = entry.get('sample') or {}
            if entry.get('direction') == 'in' \
                    and sample.get('value') is not None:
                write_target = (entry.get('point'), sample['value'])
                break
        ref = save_evidence(ctx['evidence_dir'],
                            'plant-link-loss-baseline.json',
                            {'census': census, 'probe': probe,
                             'qualities': {
                                 point: _sample_quality(before or {},
                                                        point)
                                 for point in field_in},
                             'journal_cursor': cursor,
                             'standby_sync': (peer_report or {}).get(
                                 'sync'),
                             'io_health': (before or {}).get('io_health'),
                             'tick': (before or {}).get('tick'),
                             'write_target': write_target})
        case.evidence('file', ref, 'the plant census, the pre-outage '
                      'fencing probe, the baseline qualities, journal '
                      'cursor, standby sync, health, and tick')
        if census is None:
            return case.finish('inconclusive', 'the plant did not '
                               'answer its point census')
        if not field_in:
            return case.finish('inconclusive', 'the plant census '
                               'lists no field input points')
        if not _fenced(probe):
            return case.finish('failed', 'the field held no writer '
                               'claim before the outage — a third '
                               'attachment\'s mutation probe answered '
                               + json.dumps(probe)[:300])
        fresh = {point: _sample_quality(before or {}, point)
                 for point in field_in}
        if before is None or any(q != 'good' for q in fresh.values()):
            return case.finish('inconclusive', 'the rig never showed '
                               'a healthy field baseline: '
                               + json.dumps(fresh, sort_keys=True))
        if peer_report is None:
            return case.finish('inconclusive', 'the standby peer never '
                               'answered /role — the convergence-gate '
                               'leg has no observation point')
        if peer_report.get('role') != 'standby' \
                or 'tracking' not in (peer_report.get('sync') or {}):
            return case.finish('inconclusive', 'the pair never settled '
                               'tracking — ' + peer + ' reports '
                               + str(peer_report.get('role')) + ' '
                               + json.dumps(peer_report.get('sync'))[:200])
        health0 = (before or {}).get('io_health') or {}
        case.observe('field inputs ' + json.dumps(field_in)
                     + ' reading good under the active\'s claim; '
                     'standby tracking at journal cursor '
                     + str(cursor))

        # The runner-owned lifecycle action: docker stop on the run's
        # plant container, recorded on the run's action timeline.
        try:
            stop()
        except Exception as exc:
            return case.finish('inconclusive', 'the plant stop action '
                               'never completed: ' + str(exc)[:300])
        case.observe('plant container stopped; watching the pair '
                     'through the outage')

        outage = {'roles': {}, 'snapshots': 0, 'silent': 0,
                  'tail_silent': 0, 'first_tick': None, 'tick': None,
                  'qualities': {}, 'health': None, 'health_trace': []}
        degraded_at = None
        deadline = time.monotonic() + LINK_DEGRADE_DEADLINE
        while time.monotonic() < deadline:
            for name, url in ((active, base), (peer, peer_base)):
                report = _try_role(ctx, url)
                if report is None:
                    continue
                outage['roles'][name] = report
                if report.get('role') != expected[name]:
                    return role_violation(name, report, expected,
                                          outage['roles'])
            snap = _try_snapshot(ctx, base)
            if snap is None:
                outage['silent'] += 1
                outage['tail_silent'] += 1
            else:
                outage['snapshots'] += 1
                outage['tail_silent'] = 0
                tick = snap.get('tick') or 0
                if outage['first_tick'] is None:
                    outage['first_tick'] = tick
                outage['tick'] = tick
                outage['qualities'] = {
                    point: _sample_quality(snap, point)
                    for point in field_in}
                outage['health'] = snap.get('io_health')
                seen = snap.get('io_health') or {}
                outage['health_trace'].append(
                    {'tick': tick,
                     'failed_reads': seen.get('failed_reads', 0),
                     'failed_writes': seen.get('failed_writes', 0)})
            if outage['snapshots'] == 0 and outage['silent'] >= 3:
                break  # the monitor is gone — the run aborted
            health = outage['health'] or {}
            degraded = outage['snapshots'] > 0 \
                and outage['tick'] > outage['first_tick'] \
                and all(q not in (None, 'good')
                        for q in outage['qualities'].values()) \
                and health.get('failed_reads', 0) > 0 \
                and (health.get('driver') or {}).get('link') \
                == 'disconnected'
            if degraded:
                if degraded_at is None:
                    degraded_at = time.monotonic()
                elif time.monotonic() - degraded_at > LINK_SETTLE:
                    break
            time.sleep(LINK_POLL)
        ref = save_evidence(ctx['evidence_dir'],
                            'plant-link-loss-outage.json', outage)
        case.evidence('file', ref, 'the pair\'s served state through '
                      'the outage')
        health = outage['health'] or {}
        if outage['snapshots'] == 0:
            return case.finish('failed', 'the active\'s monitor never '
                               'answered after the plant stop — field '
                               'loss aborted the run rather than '
                               'degrading its telemetry')
        if outage['tail_silent']:
            return case.finish('failed', 'the active\'s monitor '
                               'stopped answering during the outage — '
                               'the run aborted on field loss')
        if not outage['tick'] > outage['first_tick']:
            return case.finish('failed', 'the active\'s scans did not '
                               'continue through the outage — the '
                               'served tick held at '
                               + str(outage['tick']))
        still_fresh = [str(point) for point, q
                       in outage['qualities'].items() if q == 'good']
        if still_fresh:
            return case.finish('failed', 'field inputs kept reading '
                               'good through the outage: '
                               + ','.join(still_fresh))
        not_bad = {str(point): q for point, q
                   in outage['qualities'].items()
                   if q != 'bad:communication_fault'}
        if not_bad:
            return case.finish('failed', 'field inputs never reached '
                               'Bad(communication_fault) through the '
                               'outage: '
                               + json.dumps(not_bad, sort_keys=True))
        if health.get('failed_reads', 0) == 0 \
                or (health.get('driver') or {}).get('link') \
                != 'disconnected' \
                or not health.get('last_error'):
            return case.finish('failed', 'io_health lacks the counted '
                               'link failure: '
                               + json.dumps(health)[:400])
        if field_out and health.get('failed_writes', 0) == 0:
            return case.finish('failed', 'io_health counted the read '
                               'failures but no write failures though '
                               'the field serves output points: '
                               + json.dumps(health)[:400])
        trace = outage['health_trace']
        reads_grew = len(trace) >= 2 \
            and trace[-1]['failed_reads'] > trace[0]['failed_reads'] \
            and trace[-1]['failed_reads'] \
            > (health0.get('failed_reads') or 0)
        writes_grew = (not field_out) or (
            len(trace) >= 2
            and trace[-1]['failed_writes']
            > trace[0]['failed_writes'])
        if not reads_grew or not writes_grew:
            return case.finish('failed', 'io_health counters never '
                               'advanced through the outage — the loss '
                               'counted once but never grew: '
                               + json.dumps(trace[-3:])[:300])
        writes_held = all(entry['failed_writes']
                          >= trace[0]['failed_writes']
                          for entry in trace)
        if not writes_held:
            return case.finish('failed', 'io_health lost failures '
                               'through the outage — the counters '
                               'regressed: '
                               + json.dumps(trace[-3:])[:300])
        outage_reads = health.get('failed_reads', 0)
        outage_writes = health.get('failed_writes', 0)
        case.observe('degradation confirmed by tick '
                     + str(outage['tick']) + ': '
                     + json.dumps(outage['qualities'], sort_keys=True)
                     + ', io_health ' + json.dumps(health)[:300])

        # The journal leg: the served journal past the baseline cursor
        # must carry the loss as quality_changed on a failed field
        # input — the durable annunciation, not just the live
        # telemetry.
        try:
            _, journal_body = http_json(
                'GET', base + '/journal?since=' + str(cursor))
        except Exception as exc:
            return case.finish('inconclusive', 'the active\'s journal '
                               'never served the outage audit: '
                               + str(exc)[:200])
        journaled = []
        for entry in _journal_list(journal_body):
            observed = _journal_observation(entry)
            if observed is not None and observed[0] == 'quality_changed' \
                    and observed[1] in field_in:
                journaled.append(observed)
        ref = save_evidence(ctx['evidence_dir'],
                            'plant-link-loss-journal.json',
                            {'cursor': cursor,
                             'quality_changed': journaled,
                             'seqs': _journal_seqs(journal_body)})
        case.evidence('file', ref, 'the loss in the served journal '
                      'past the baseline cursor')
        if not journaled:
            return case.finish('failed', 'the loss was never journaled '
                               '— the served journal past seq '
                               + str(cursor) + ' carries no '
                               'quality_changed for a failed field '
                               'input')
        case.observe(str(len(journaled)) + ' quality_changed records '
                     'journal the loss past seq ' + str(cursor))

        # The standby-gate leg: the checkpoint heartbeat is
        # peer-to-peer, so the outage must leave the standby tracking
        # — its promotion path still on the convergence gate, with no
        # spontaneous promotion behind the field loss.
        standby_report = (outage['roles'] or {}).get(peer) \
            or _try_role(ctx, peer_base)
        ref = save_evidence(ctx['evidence_dir'],
                            'plant-link-loss-standby.json',
                            {'report': standby_report})
        case.evidence('file', ref, 'the standby\'s convergence through '
                      'the outage')
        if standby_report is None:
            return case.finish('inconclusive', 'the standby peer never '
                               'answered /role through the outage — '
                               'the convergence-gate leg has no '
                               'observation point')
        if standby_report.get('role') != 'standby' \
                or 'tracking' not in (standby_report.get('sync') or {}):
            return case.finish('failed', 'the standby left the '
                               'tracking convergence through field '
                               'loss — its promotion path no longer '
                               'follows the gate: '
                               + json.dumps(standby_report)[:300])
        case.observe('standby still tracking through the outage — '
                     'the promotion path holds on the gate')

        # The recovery half: the plant container comes back as a new
        # server lifetime, so the single-writer claim is gone until the
        # field owner re-attaches and re-arms it through ensure_writer.
        # One tight watch covers both the fail-closed window and the
        # recovery: every iteration probes the field's own fencing
        # answers and the serving monitor, so no probe between the
        # plant's return and the owner's re-arm can slip past the
        # unclaimed verdict.
        try:
            start()
        except Exception as exc:
            return case.finish('inconclusive', 'the plant start '
                               'action never completed: '
                               + str(exc)[:300])
        case.observe('plant container started; watching the '
                     'fail-closed window and the re-arm')

        window = []
        recovery = {'plant': False, 'probe': None, 'snapshot': None,
                    'roles': {}, 'first_tick': None, 'tick_grew': False}
        deadline = time.monotonic() + LINK_RECOVERY_DEADLINE
        while time.monotonic() < deadline:
            for name, url in ((active, base), (peer, peer_base)):
                report = _try_role(ctx, url)
                if report is None:
                    continue
                recovery['roles'][name] = report
                if report.get('role') != expected[name]:
                    return role_violation(name, report, expected,
                                          recovery['roles'])
            listed = _try_plant_ctl(ctx, 'list')
            if listed is not None:
                recovery['plant'] = True
            step_probe = _try_plant(ctx, {'op': 'step', 'dt': 0})
            write_probe = None
            if write_target is not None:
                write_probe = _try_plant(
                    ctx, {'op': 'write', 'point': write_target[0],
                          'value': write_target[1]})
            if step_probe is not None:
                recovery['probe'] = step_probe
            snap = _try_snapshot(ctx, base)
            if snap is not None:
                recovery['snapshot'] = snap
                tick = snap.get('tick') or 0
                if recovery['first_tick'] is None:
                    recovery['first_tick'] = tick
                elif tick > recovery['first_tick']:
                    recovery['tick_grew'] = True
            snap_health = (snap or {}).get('io_health') or {}
            window.append(
                {'plant': listed is not None,
                 'step': _probe_error(step_probe)
                 if step_probe is not None else None,
                 'step_result': (step_probe or {}).get('result'),
                 'write': _probe_error(write_probe)
                 if write_probe is not None
                 else ('skipped' if write_target is None else None),
                 'write_result': (write_probe or {}).get('result'),
                 'tick': (snap or {}).get('tick'),
                 'snapshot': snap is not None})
            if recovery['plant'] and _fenced(step_probe) \
                    and snap is not None and recovery['tick_grew'] \
                    and all(_sample_quality(snap, point) == 'good'
                            for point in field_in) \
                    and (snap_health.get('driver') or {}).get('link') \
                    == 'connected':
                break
            time.sleep(LINK_WINDOW_POLL)
        ref = save_evidence(ctx['evidence_dir'],
                            'plant-link-loss-window.json', window)
        case.evidence('file', ref, 'every fencing probe and monitor '
                      'read between the plant\'s return and the '
                      'recovery')
        ref = save_evidence(ctx['evidence_dir'],
                            'plant-link-loss-recovery.json', recovery)
        case.evidence('file', ref, 'the plant\'s return, the fencing '
                      'probe, and the recovered snapshot')
        if not recovery['plant']:
            return case.finish('inconclusive', 'the restarted plant '
                               'container never served again')

        # The fail-closed verdicts come before the re-arm verdict: a
        # probe that mutated through the window is the sharper defect
        # than a claim that never re-armed.
        if any(round_['step_result'] == 'stepped' for round_ in window):
            return case.finish('failed', 'a mutation probe stepped '
                               'through the unclaimed window — the '
                               'restarted field stood open: '
                               + json.dumps(window)[:300])
        if any(round_['write_result'] == 'done' for round_ in window):
            return case.finish('failed', 'a mutation probe wrote '
                               'silently through the window — the '
                               'restarted field stood open: '
                               + json.dumps(window)[:300])
        # The fail-closed window: the probes before the first fenced
        # answer are the unclaimed window — each must carry the named
        # refusal, never a stepped mutation and never a silent write —
        # while the serving monitor kept answering with advancing
        # scans throughout.
        first_fenced = next(
            (index for index, round_ in enumerate(window)
             if round_['step'] == 'fenced'), None)
        if first_fenced is None:
            return case.finish('failed', 'the restarted plant never '
                               're-armed the single-writer claim — a '
                               'third attachment\'s mutation answered '
                               + json.dumps(recovery['probe'])[:300])
        window_rounds = window[:first_fenced]
        answered = [round_ for round_ in window_rounds
                    if round_['step'] is not None]
        if not answered or any(round_['step'] != 'unclaimed'
                               for round_ in answered):
            return case.finish('failed', 'the fail-closed window never '
                               'observed — no mutation probe answered '
                               'the named unclaimed refusal between '
                               'the plant\'s return and the re-arm: '
                               + json.dumps(answered[-3:])[:300])
        if any(not round_['snapshot'] for round_ in window):
            return case.finish('failed', 'reads went silent through '
                               'the window — the monitor stopped '
                               'answering while the plant returned')
        window_ticks = [round_['tick'] for round_ in window
                        if isinstance(round_['tick'], int)]
        if len(window_ticks) < 2 \
                or window_ticks[-1] <= window_ticks[0]:
            return case.finish('failed', 'the active\'s scans stalled '
                               'through the window — the served tick '
                               'held at ' + str(window_ticks[-1:]))
        case.observe(str(len(answered)) + ' window probes answered '
                     'unclaimed before the re-arm; reads stayed open')

        snap = recovery['snapshot'] or {}
        recovered = {point: _sample_quality(snap, point)
                     for point in field_in}
        if any(q != 'good' for q in recovered.values()):
            return case.finish('failed', 'reads never returned to '
                               'Good after the plant\'s return: '
                               + json.dumps(recovered,
                                            sort_keys=True))
        if not recovery['tick_grew']:
            return case.finish('failed', 'the active\'s scans stalled '
                               'across the plant\'s return — the '
                               'served tick held at '
                               + str(snap.get('tick')))
        health = snap.get('io_health') or {}
        if (health.get('driver') or {}).get('link') != 'connected':
            return case.finish('failed', 'the driver link never '
                               'reported connected after the plant\'s '
                               'return: ' + json.dumps(health)[:400])
        if health.get('failed_reads', 0) < outage_reads \
                or health.get('failed_writes', 0) < outage_writes \
                or not health.get('last_error'):
            return case.finish('failed', 'io_health lost the '
                               'outage\'s recorded failures — the '
                               'counters reset across the recovery: '
                               + json.dumps(health)[:400])
        if not (snap.get('tick') or 0) >= (outage['tick'] or 0):
            return case.finish('failed', 'the served tick regressed '
                               'across the plant\'s return — tick '
                               + str(snap.get('tick')) + ' after '
                               + str(outage['tick']) + ': a controller '
                               'restarted instead of riding out the '
                               'loss')
        case.observe('recovered: reads Good, the writer claim '
                     're-armed, io_health still records '
                     + str(health.get('failed_reads')) + ' failed '
                     'reads and ' + str(health.get('failed_writes'))
                     + ' failed writes')

        # The no-restart leg: neither controller's durable journal file
        # may show a second lifetime — the pair rode the loss out in
        # place. A missing path predates the lane's journal-file seam
        # and skips; an unreadable one cannot prove the leg.
        journals = ctx.get('journal_files') or {}
        lifetimes = {}
        for name in (active, peer):
            path = journals.get(name)
            if path is None:
                continue
            try:
                parsed = _journal_entries(path)
            except (OSError, ValueError) as exc:
                return case.finish('inconclusive', 'the ' + name
                                   + ' journal file is unreadable: '
                                   + str(exc)[:200])
            bounds = [record['run_boundary'] for record in parsed
                      if 'run_boundary' in record]
            seqs = [(record.get('entry') or {}).get('seq')
                    for record in parsed if 'entry' in record]
            lifetimes[name] = {'lifetimes': len(bounds), 'seqs': seqs}
            if len(bounds) != 1:
                return case.finish('failed', name + ' journal file '
                                   'holds ' + str(len(bounds))
                                   + ' lifetimes — a controller '
                                   'restarted across the field loss')
            if not seqs or any(not isinstance(seq, int)
                               for seq in seqs) \
                    or seqs != sorted(seqs) \
                    or len(set(seqs)) != len(seqs):
                return case.finish('failed', name + ' journal seqs do '
                                   'not continue across the loss: '
                                   + str(seqs[:10]))
        ref = save_evidence(ctx['evidence_dir'],
                            'plant-link-loss-lifetimes.json', lifetimes)
        case.evidence('file', ref, 'the journal-file lifetimes across '
                      'the loss')
        case.observe('journal files hold one lifetime each — no '
                     'controller restarted' if lifetimes
                     else 'no journal-file paths in context — '
                     'the restart leg rests on the served tick')

        # The normalized digest: the deterministic rerun contract —
        # categorical verdicts, identical across clean passes.
        digest = {'baseline': 'fenced-good',
                  'outage': 'bad-counted-disconnected',
                  'counters': 'advancing',
                  'journal': 'quality_changed',
                  'standby_gate': 'tracking',
                  'window': 'unclaimed-then-fenced',
                  'writes': 'refused',
                  'reads': 'open',
                  'recovery': 'good-connected-retained',
                  'restarts': 'none'}
        ref = save_evidence(ctx['evidence_dir'],
                            'plant-link-loss-digest.json', digest)
        case.evidence('file', ref, 'the normalized deterministic '
                      'digest')

        # Restore the rig for later scenarios: the plant serving, the
        # pair settled on its entry assignment.
        settled = wait_for(lambda: _settled_active(ctx),
                           time.monotonic() + 30)
        if settled != active:
            return case.finish('failed', 'the pair did not settle back '
                               'on its entry assignment — ' + active
                               + ' no longer reports active')
        case.observe('rig restored: plant serving, ' + active
                     + ' active, ' + peer + ' standby')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))


# --------------------------------------------------------------------
# The served block-interface contract (WW-FND-003, decision 82): every
# assessed run proves the schema-driven surface the tranche ships —
# GET /schema's registry covering every kind the rig model declares
# with all five collections, a declared command settling through the
# receipted command path, and GET /resources' emitted-events view
# reflecting a produced event.

INTERFACE_COLLECTIONS = ('measurements', 'configuration', 'state',
                         'commands', 'events')
CONTRACT_DEADLINE = 30  # bound on the receipt and emitted-event waits


def _command_for_spec(component, spec):
    """The receipted-path command a served `commands` entry denotes,
    rebuilt from the entry's declared provenance: `declared` entries
    submit as `invoke` with the declared request schema's arguments,
    `write_value` entries as the point write against the entry's bound
    point, and `set_parameter` entries as the parameter tune. Returns
    None for an entry this driver cannot translate."""
    defaults = {'bool': {'bool': True}, 'int': {'int': 1},
                'float': {'float': 1.0}}
    request = spec.get('request') or []
    adapted = spec.get('adapted')
    if adapted == 'declared':
        arguments = {}
        for argument in request:
            value = defaults.get(argument.get('kind'))
            if value is None:
                return None
            arguments[argument['name']] = value
        return {'invoke': {'component': component,
                           'command': spec.get('name'),
                           'arguments': arguments}}
    if adapted == 'set_parameter' and ':' in str(spec.get('name')):
        kind = request[0].get('kind') if request else None
        value = defaults.get(kind)
        if value is None:
            return None
        return {'set_parameter': {'component': component,
                                  'name': str(spec['name']).split(':', 1)[1],
                                  'value': value}}
    if adapted == 'write_value' and spec.get('point') is not None:
        kind = request[0].get('kind') if request else None
        value = defaults.get(kind)
        if value is None:
            return None
        return {'write_value': {'point': spec['point'], 'kind': kind,
                                'value': value}}
    return None


def _pick_declared_command(interfaces, signals):
    """The scenario's probe command out of the served `commands`
    collections, in preference order: a kind-declared (`declared`-
    provenance) entry a kind offers natively; then the `write_value`
    adapted entry bound to the run's preferred writable bool point —
    'p101-oos', the target the other command scenarios use; then any
    writable bool point's entry; then any remaining translated entry
    (a `set_parameter` tune, or a refused point write — a rejection is
    still a receipted, journaled answer). Returns
    (component, spec, submission) or None."""
    writable = {entry.get('point')
                for entry in signals.get('points', [])
                if entry.get('writable')
                and entry.get('direction') == 'in'
                and entry.get('value_type') == 'bool'}
    preferred = {entry.get('point')
                 for entry in signals.get('points', [])
                 if entry.get('name') == 'p101-oos'} & writable
    best = None
    for entry in interfaces:
        component = entry.get('name')
        for spec in (entry.get('interface') or {}).get('commands') or []:
            submission = _command_for_spec(component, spec)
            if submission is None:
                continue
            adapted = spec.get('adapted')
            if adapted == 'declared':
                rank = 0
            elif adapted == 'write_value' \
                    and spec.get('point') in preferred:
                rank = 1
            elif adapted == 'write_value' \
                    and spec.get('point') in writable:
                rank = 2
            else:
                rank = 3
            if best is None or rank < best[0]:
                best = (rank, component, spec, submission)
    if best is None:
        return None
    return best[1], best[2], best[3]


def scenario_served_interface(ctx):
    """The served block-interface contract against the rig: registry
    coverage, a receipted declared command, and the emitted-events
    view reflecting the produced event."""
    case = Case('served-interface',
                'Served block-interface contract covers the model',
                'GET /schema covers every component kind the rig model '
                'declares with all five collections, a declared command '
                'submitted through POST /command returns a structured '
                'receipt, and GET /resources attributes a produced '
                'event to the issuing instance')
    try:
        # Self-contained on either role layout, like evidence-capture:
        # replayed alone the rig is fresh (ctrl-a active), while the
        # full suite reaches this case after the failover.
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        base = ctx[active]
        case.observe('served contract against ' + active
                     + ' (' + base + ')')

        # Each documented endpoint is part of the served contract: an
        # answered error means the surface itself is missing — a
        # failed check, where a monitor that cannot be reached at all
        # stays inconclusive.
        bodies = {}
        for path in ('/signals', '/schema', '/resources'):
            try:
                _, bodies[path] = http_json('GET', base + path)
            except urllib.error.HTTPError as exc:
                return case.finish('failed', 'GET ' + path
                                   + ' answered ' + str(exc.code))
        signals, schema = bodies['/signals'], bodies['/schema']
        ref = save_evidence(ctx['evidence_dir'],
                            'served-interface-signals.json', signals)
        case.evidence('file', ref, 'declared component records')
        ref = save_evidence(ctx['evidence_dir'],
                            'served-interface-schema.json', schema)
        case.evidence('file', ref, 'the served interface registry')

        declared = signals.get('components') or []
        if not declared:
            return case.finish('inconclusive',
                               'the signal index serves no component '
                               'records to check coverage against')
        served = {}
        for entry in schema.get('interfaces') or []:
            if isinstance(entry, dict):
                served[entry.get('name')] = entry.get('interface') or {}
        missing = [record for record in declared
                   if (served.get(record.get('name')) or {}).get('kind')
                   != record.get('kind')]
        if missing:
            return case.finish(
                'failed', 'the served registry misses declared kinds '
                + ', '.join(sorted({str(r.get('kind'))
                                    for r in missing}))
                + ' (instances: '
                + ', '.join(str(r.get('name')) for r in missing[:8])
                + ')')
        short = {}
        for entry in schema.get('interfaces') or []:
            interface = (entry or {}).get('interface') or {}
            absent = [name for name in INTERFACE_COLLECTIONS
                      if not isinstance(interface.get(name), list)]
            if absent:
                short[str(entry.get('name'))] = absent
        if short:
            return case.finish(
                'failed', 'served interfaces miss collections: '
                + json.dumps(short, sort_keys=True)[:600])
        kinds = sorted({str(record.get('kind')) for record in declared})
        case.observe('registry covers ' + str(len(declared))
                     + ' declared instances across '
                     + str(len(kinds)) + ' kinds ('
                     + ', '.join(kinds) + ') at publication '
                     + str(schema.get('publication'))
                     + ' tick ' + str(schema.get('tick')))

        picked = _pick_declared_command(
            schema.get('interfaces') or [], signals)
        if picked is None:
            return case.finish('inconclusive',
                               'no served command translates to the '
                               'receipted path')
        component, spec, command = picked
        case.observe('declared command: ' + str(component) + ' '
                     + str(spec.get('name')) + ' -> '
                     + json.dumps(command, sort_keys=True))
        try:
            status, receipt = http_json(
                'POST', base + '/command',
                {'command': command, 'actor': 'qa-lane'})
        except urllib.error.HTTPError as exc:
            return case.finish('failed', 'the declared command '
                               'returned no receipt: HTTP '
                               + str(exc.code))
        ref = save_evidence(ctx['evidence_dir'],
                            'served-interface-receipt.json',
                            {'status': status, 'body': receipt})
        case.evidence('file', ref, 'the declared command\'s receipt')
        outcome = receipt.get('outcome') \
            if isinstance(receipt, dict) else None
        if status != 200 or not isinstance(receipt, dict) \
                or not isinstance(receipt.get('command'), dict) \
                or not isinstance(outcome, dict) or not outcome:
            return case.finish(
                'failed', 'the declared command returned no '
                'structured receipt: ' + str(status) + ' '
                + json.dumps(receipt)[:400])
        case.observe('receipt outcome: '
                     + json.dumps(outcome, sort_keys=True))

        # The emitted-events view is the instance's attributed journal
        # tail: the produced event is the submission's settled receipt
        # — journaled whether it applied or refused — or a kind-
        # emitted event the run produced.
        observed = {'events': None, 'match': None}

        def events_cover():
            try:
                _, view = http_json('GET', base + '/resources')
            except urllib.error.HTTPError:
                raise
            except Exception:
                return None
            for entry in view.get('components') or []:
                if entry.get('name') != component:
                    continue
                observed['events'] = entry.get('events') or []
                for candidate in observed['events']:
                    event = (candidate or {}).get('event') or {}
                    settled = (event.get('command_settled') or {}) \
                        .get('receipt') or {}
                    if settled.get('command') == command \
                            or event.get('event_emitted'):
                        observed['match'] = candidate
                        return True
            return None

        covered = wait_for(events_cover,
                           time.monotonic() + CONTRACT_DEADLINE,
                           interval=POLL_INTERVAL)
        ref = save_evidence(
            ctx['evidence_dir'], 'served-interface-events.json',
            {'component': component, 'match': observed['match'],
             'events': observed['events'] or []})
        case.evidence('file', ref, 'emitted events attributed to '
                      + str(component))
        if not covered:
            return case.finish(
                'failed', 'the emitted-events view never reflected a '
                'produced event for ' + str(component))
        match = (observed['match'] or {}).get('event') or {}
        case.observe('emitted-events view covers '
                     + next(iter(match), '?') + ' for '
                     + str(component) + ' ('
                     + str(len(observed['events'] or []))
                     + ' entries)')
        return case.finish('passed')
    except urllib.error.HTTPError as exc:
        return case.finish('failed', 'the emitted-events view answered '
                           + str(exc.code))
    except Exception as exc:
        return case.finish('inconclusive', str(exc))


# --------------------------------------------------------------------
# Declared-event retention routing (WW-FND-003's EventRetention
# semantics): every served interface declares each event's retention —
# `history` routes the emission to the bounded event-history record,
# `latest` to the standing latest-emission view, and only `journal`
# emissions journal `event_emitted`. The case locates a component
# declaring both routed classes through GET /schema — model-declared,
# never hardcoded — drives the declared emission path through the
# receipted command surface the same registry exposes, and asserts on
# the active's /resources that each emission adds an event-history
# record for the History name while the Latest name only refreshes
# its standing record: repeated emissions grow the ring and overwrite
# the view. On the tracking standby the descriptor is served but the
# command path refuses at the role boundary — the tracking peer emits
# the adopted run's events identically (decision 84), so the parity
# this leg pins is that the routed events never journal there: no
# event_emitted records accrue for the driven run's History/Latest
# names, and the command-driven path stays refused until promotion.

RETENTION_DEADLINE = 30    # bound on the tracking and served-record waits
RETENTION_MAX_DRIVES = 4   # receipted candidates tried per run
RETENTION_LEAK_POLLS = 3   # journal reads the standby accrual watch takes


def _routed_declarations(interface):
    """The interface's History/Latest-declared event names:
    {name: 'history'|'latest'} — the retention classes the serving
    layer routes to their own stores instead of journaling."""
    declared = {}
    for spec in (interface or {}).get('events') or []:
        if spec.get('retention') in ('history', 'latest') \
                and spec.get('name'):
            declared[str(spec['name'])] = spec['retention']
    return declared


def _emission_records(view, component):
    """The event_emitted records one /resources component entry
    carries, keyed by emitted event name — the attributed journal tail
    beside the routed History/Latest records, each entry's `retention`
    naming the store it landed in."""
    found = {}
    for entry in (view or {}).get('components') or []:
        if entry.get('name') != component:
            continue
        for record in entry.get('events') or []:
            emitted = ((record or {}).get('event') or {}) \
                .get('event_emitted', {}).get('event') or {}
            if emitted.get('component') != component \
                    or not emitted.get('event'):
                continue
            found.setdefault(str(emitted['event']), []).append(record)
    return found


def _journal_emitted(payload, component, names):
    """The event_emitted records a journal payload carries for `names`
    on `component` — the durable-side accrual a routed emission must
    never produce."""
    hits = []
    for entry in _journal_list(payload):
        emitted = (entry.get('event') or {}).get('event_emitted', {}) \
            .get('event') or {}
        if emitted.get('component') == component \
                and emitted.get('event') in names:
            hits.append(entry)
    return hits


def _record_key(record):
    """One served event record's identity — a superseded Latest record
    reads as a fresh key while an unchanged one does not."""
    emitted = ((record.get('event') or {}).get('event_emitted', {})
               .get('event') or {})
    return (emitted.get('event'), record.get('retention'),
            record.get('seq'), record.get('tick'),
            json.dumps(emitted.get('fields'), sort_keys=True))


def _emission_drives(component, interface, signals):
    """The receipted-path submissions that can drive the component's
    declared emissions, in preference order: the kind-declared
    commands first, then the adapted set_parameter/write_value
    surface, then writes against the writable bound point an
    on_observed_change event names."""
    candidates = []
    for spec in (interface or {}).get('commands') or []:
        submission = _command_for_spec(component, spec)
        if submission is None:
            continue
        rank = 0 if spec.get('adapted') == 'declared' else 1
        candidates.append((rank, json.dumps(submission,
                                            sort_keys=True),
                           submission))
    candidates.sort(key=lambda item: (item[0], item[1]))
    drives = [submission for _, _, submission in candidates]
    points = {entry.get('point'): entry
              for entry in signals.get('points', [])}
    defaults = {'bool': {'bool': True}, 'int': {'int': 1},
                'float': {'float': 1.0}}
    seen = {json.dumps(drive, sort_keys=True) for drive in drives}
    for spec in (interface or {}).get('events') or []:
        if spec.get('emission') != 'on_observed_change':
            continue
        entry = points.get(spec.get('point'))
        if entry is None or not entry.get('writable') \
                or entry.get('direction') != 'in':
            continue
        value = defaults.get(entry.get('value_type'))
        if value is None:
            continue
        drive = {'write_value': {'point': spec['point'],
                                 'kind': entry['value_type'],
                                 'value': value}}
        if json.dumps(drive, sort_keys=True) not in seen:
            drives.append(drive)
    return drives


def _repeat_drive(submission):
    """The same drive again — a value-bearing variant flips its
    argument so the repeat still changes state where a held write to
    the same value would observe no transition."""
    repeat = json.loads(json.dumps(submission))
    for variant in ('write_value', 'set_parameter'):
        body = repeat.get(variant)
        if not isinstance(body, dict):
            continue
        value = body.get('value')
        if isinstance(value, dict):
            if 'bool' in value:
                value['bool'] = not value['bool']
            elif 'int' in value:
                value['int'] = value['int'] + 1
            elif 'float' in value:
                value['float'] = value['float'] + 1.0
    return repeat


def scenario_event_retention(ctx):
    """Declared event retentions route to their own served stores —
    History accumulates, Latest overwrites, and the tracking standby
    journals none of the routed emissions."""
    case = Case('event-retention',
                'Declared event retention routes to its stores',
                'a served component declaring both History and Latest '
                'event retentions is located through GET /schema and '
                'driven through the receipted command path the '
                'interface exposes: on the active each emission adds '
                'an event-history record for the History name while '
                'the Latest name only refreshes its standing record, '
                'and on the tracking standby the descriptor is '
                'published, the command path refuses, and no '
                'event_emitted journal records accrue for the routed '
                'events')
    try:
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        base = ctx[active]
        case.observe('retention routing against ' + active
                     + ' (' + base + ')')

        _, signals = http_json('GET', base + '/signals')
        _, schema = http_json('GET', base + '/schema')
        _, resources = http_json('GET', base + '/resources')
        _, history = http_json('GET', base + '/history?since=0')
        _, journal = http_json('GET', base + '/journal?since=0')
        ref = save_evidence(ctx['evidence_dir'],
                            'event-retention-schema.json', schema)
        case.evidence('file', ref, 'the served registry the qualifying '
                      'component is located through')
        ref = save_evidence(ctx['evidence_dir'],
                            'event-retention-served.json',
                            {'signals': signals, 'resources': resources,
                             'history': history, 'journal': journal})
        case.evidence('file', ref, 'the signal index, resource, '
                      'point-history, and journal records before the '
                      'driven emissions')

        # The qualifying component is model-declared, never hardcoded:
        # an instance the registry declares — and the resource view
        # serves — carrying at least one History-retained and one
        # Latest-retained event.
        served = {str(entry.get('name'))
                  for entry in resources.get('components') or []
                  if entry.get('name')}
        qualifying = None
        for entry in schema.get('interfaces') or []:
            interface = (entry or {}).get('interface') or {}
            declared = _routed_declarations(interface)
            if 'history' in declared.values() \
                    and 'latest' in declared.values() \
                    and str(entry.get('name')) in served:
                qualifying = (str(entry.get('name')), interface,
                              declared)
                break
        if qualifying is None:
            return case.finish('inconclusive', 'the served registry '
                               'declares no component carrying both '
                               'History and Latest event retentions')
        component, interface, declared = qualifying
        case.observe('qualifying component: ' + component
                     + ' declares '
                     + json.dumps(declared, sort_keys=True))
        drives = _emission_drives(component, interface, signals)
        if not drives:
            return case.finish('inconclusive', component + ' exposes '
                               'no receipted command or bound-point '
                               'path the declared emissions can be '
                               'driven through')

        # `seen` holds every served record key so a poll's diff is the
        # new emissions; `harvest` merges each fresh /resources view.
        seen = set()
        view = {'last': resources}

        def harvest(payload):
            new = []
            for name, recs in _emission_records(payload,
                                                component).items():
                for record in recs:
                    key = _record_key(record)
                    if key in seen:
                        continue
                    seen.add(key)
                    new.append((name, record))
            return new

        def collect():
            try:
                _, latest = http_json('GET', base + '/resources')
            except Exception:
                return None
            view['last'] = latest
            return harvest(latest) or None

        def misrouted(records):
            return [name + ' declared ' + declared[name]
                    + ' retained in the '
                    + str(record.get('retention')) + ' store'
                    for name, record in records
                    if declared.get(name)
                    and record.get('retention') != declared[name]]

        def accumulated(payload):
            """Latest-declared events standing as more than one
            record in a single view — the overwrite that wasn't."""
            counts = {}
            for name, recs in _emission_records(payload,
                                                component).items():
                if declared.get(name) != 'latest':
                    continue
                counts[name] = sum(
                    1 for record in recs
                    if record.get('retention') == 'latest')
            return [name + ' accumulates standing latest records'
                    for name, count in counts.items() if count > 1]

        failures = misrouted(harvest(resources)) + accumulated(resources)
        produced = {}
        covered = {}
        drives_log = []
        for index, submission in enumerate(
                drives[:RETENTION_MAX_DRIVES]):
            if failures or len(covered) == 2:
                break
            status, receipt = http_json(
                'POST', base + '/command',
                {'command': submission, 'actor': 'qa-lane'})
            entry = {'submission': submission, 'status': status,
                     'receipt': receipt}
            drives_log.append(entry)
            if status != 200 or not isinstance(receipt, dict) \
                    or not isinstance(receipt.get('outcome'), dict):
                ref = save_evidence(ctx['evidence_dir'],
                                    'event-retention-drives.json',
                                    drives_log)
                case.evidence('file', ref, 'the driven submissions '
                              'and their receipts')
                return case.finish('failed', 'the emission path '
                                   'returned no structured receipt: '
                                   + str(status) + ' '
                                   + json.dumps(receipt)[:300])
            entry['outcome'] = _outcome_key(receipt)
            if 'rejected' in (receipt.get('outcome') or {}):
                case.observe('drive refused: ' + entry['outcome'])
                continue
            new = wait_for(collect,
                           time.monotonic() + RETENTION_DEADLINE,
                           interval=POLL_INTERVAL) or []
            produced[index] = sorted({name for name, _ in new})
            entry['produced'] = produced[index]
            case.observe('drive '
                         + json.dumps(submission, sort_keys=True)[:160]
                         + ' produced '
                         + (', '.join(produced[index])
                            if produced[index] else 'no emissions'))
            failures += misrouted(new) + accumulated(view['last'])
            for name in produced[index]:
                retention = declared.get(name)
                if retention and retention not in covered:
                    covered[retention] = index

        # The repeat leg: each drive that produced a routed class runs
        # once more — the History side must grow with the fresh
        # emission and the Latest side must overwrite, never
        # accumulate and never sit stale.
        repeats = {}
        if not failures:
            for index in sorted(set(covered.values())):
                submission = _repeat_drive(drives[index])
                status, receipt = http_json(
                    'POST', base + '/command',
                    {'command': submission, 'actor': 'qa-lane'})
                entry = {'submission': submission, 'status': status,
                         'receipt': receipt, 'repeat': True}
                drives_log.append(entry)
                entry['outcome'] = _outcome_key(receipt) \
                    if isinstance(receipt, dict) else 'none'
                if status != 200 or 'rejected' in (
                        (receipt or {}).get('outcome') or {}):
                    repeats[index] = None
                    continue
                new = wait_for(collect,
                               time.monotonic() + RETENTION_DEADLINE,
                               interval=POLL_INTERVAL) or []
                entry['produced'] = sorted({name for name, _ in new})
                failures += misrouted(new) + accumulated(view['last'])
                repeats[index] = new
        ref = save_evidence(ctx['evidence_dir'],
                            'event-retention-drives.json', drives_log)
        case.evidence('file', ref, 'the driven submissions, their '
                      'receipts, and the events each produced')
        ref = save_evidence(ctx['evidence_dir'],
                            'event-retention-resources.json',
                            view['last'])
        case.evidence('file', ref, 'the served resource records the '
                      'routed emissions landed in')
        if failures:
            return case.finish('failed', '; '.join(failures))
        missing = [cls for cls in ('history', 'latest')
                   if cls not in covered]
        if missing:
            return case.finish(
                'inconclusive', 'the reachable emission paths produced '
                'no ' + '-retained or '.join(missing)
                + '-retained emission for ' + component)
        unproven = [index for index, new in repeats.items() if not new]
        if unproven:
            return case.finish(
                'inconclusive', 'the repeated drive produced no '
                'observable emissions — the retention growth check '
                'cannot run')
        unverified = []
        for index, new in repeats.items():
            fresh = {name for name, _ in new}
            rerun = [name for name in (produced.get(index) or [])
                     if name in declared]
            if not any(name in fresh for name in rerun):
                unverified.append(index)
                continue
            for name in rerun:
                if name in fresh:
                    continue
                if declared[name] == 'history':
                    failures.append(
                        'the repeated emission did not grow the '
                        'event-history record for ' + name)
                else:
                    failures.append(
                        'the repeated emission left the '
                        'latest-emission record for ' + name
                        + ' stale')
        if failures:
            return case.finish('failed', '; '.join(failures))
        if unverified:
            return case.finish(
                'inconclusive', 'the repeated drive emitted none of '
                'the first run\'s routed events — the retention '
                'growth check cannot run')
        case.observe('repeated emissions grew the history record and '
                     'refreshed the standing latest records')

        # The routed events never journal — on either peer.
        _, journal = http_json('GET', base + '/journal?since=0')
        ref = save_evidence(ctx['evidence_dir'],
                            'event-retention-journal.json', journal)
        case.evidence('file', ref, 'the active\'s journal after the '
                      'driven emissions')
        hits = _journal_emitted(journal, component, set(declared))
        if hits:
            return case.finish(
                'failed', 'the active journaled event_emitted records '
                'for routed events: '
                + json.dumps(hits, sort_keys=True)[:400])

        # The tracking-standby leg: the descriptor publishes, the
        # command-driven emission path refuses at the role boundary —
        # emission through it resumes only after promotion — and no
        # event_emitted journal records accrue for the routed names.
        peer = wait_for(lambda: _tracking_peer(ctx, active),
                        time.monotonic() + RETENTION_DEADLINE,
                        interval=POLL_INTERVAL)
        if peer is None:
            return case.finish('inconclusive', 'no pair peer reports '
                               'a tracking standby — the suppression '
                               'leg cannot be exercised')
        peer_base = ctx[peer]
        _, peer_schema = http_json('GET', peer_base + '/schema')
        _, peer_resources = http_json('GET', peer_base + '/resources')
        peer_interface = next(
            ((entry or {}).get('interface') or {}
             for entry in peer_schema.get('interfaces') or []
             if entry.get('name') == component), {})
        descriptor = set(declared) \
            <= set(_routed_declarations(peer_interface))
        published = any(entry.get('name') == component
                        for entry in
                        peer_resources.get('components') or [])
        submission = drives[sorted(set(covered.values()))[0]]
        try:
            status, refusal = http_json(
                'POST', peer_base + '/command',
                {'command': submission, 'actor': 'qa-lane'})
            refused = status != 200 or 'rejected' in (
                (refusal or {}).get('outcome') or {})
        except urllib.error.HTTPError as exc:
            status, refusal, refused = exc.code, str(exc.code), True
        leak = {'journal': None}
        hits = []
        for _poll in range(RETENTION_LEAK_POLLS):
            try:
                _, leak['journal'] = http_json(
                    'GET', peer_base + '/journal?since=0')
            except Exception:
                continue
            hits += _journal_emitted(leak['journal'], component,
                                     set(declared))
            if hits:
                break
            time.sleep(POLL_INTERVAL)
        ref = save_evidence(ctx['evidence_dir'],
                            'event-retention-standby.json',
                            {'peer': peer, 'schema': peer_schema,
                             'resources': peer_resources,
                             'submission': submission,
                             'status': status, 'receipt': refusal,
                             'journal': leak['journal']})
        case.evidence('file', ref, 'the tracking standby\'s '
                      'descriptor, refusal, and journal')
        if not (descriptor and published):
            return case.finish('failed', 'the tracking standby '
                               'publishes no descriptor for '
                               + component)
        if not refused:
            return case.finish('failed', 'the tracking standby '
                               'admitted the driven command instead '
                               'of refusing it at the role boundary')
        if hits:
            return case.finish('failed', 'the tracking standby '
                               'journaled event_emitted records for '
                               'the driven run')
        case.observe('the tracking standby publishes the descriptor, '
                     'refuses the drive, and journals none of the '
                     'routed events')
        return case.finish('passed')
    except urllib.error.HTTPError as exc:
        return case.finish('failed', 'a served endpoint answered '
                           + str(exc.code))
    except Exception as exc:
        return case.finish('inconclusive', str(exc))


# --------------------------------------------------------------------
# Receipted point forcing and release (WW-OPS-003's substituted
# quality, WW-FND-004's settled receipts): `force_point` pins a
# writable `In` point at Uncertain(Substituted) across scans and badges
# it in the snapshot's `forces` list; `unforce_point` lifts it at a
# scan boundary. The rig's target is its writable internal `In` point
# p101-oos — the executor's force path accepts writable internal
# points, the operator-setpoint surface, so the model declares no
# writable loopback field point (a channel-bound `writable` mark is
# exactly what the model lint names). Releasing an internal point
# resumes the held-value rule — and the release boundary itself
# re-stamps the held image Good (finding #498's merged fix): no
# channel rewrites a held internal point, so without that re-stamp
# the force's last Substituted mark would stand forever, tainting the
# downstream p101-oos-ok cone and the tracking standby's adopted
# snapshot. The recovery legs therefore observe the release before
# any restamp write — the held-value write afterward is the suite's
# restore step, not what proves the release took.

FORCE_DEADLINE = 30  # bound on each boundary/settlement wait


def _point_sample(snapshot, point):
    """The served sample of one point in a /snapshot payload, or None
    when the point is absent — callers test or guard the None rather
    than assume a dict."""
    for entry in (snapshot or {}).get('points', []):
        if entry.get('point') == point:
            return entry.get('sample')
    return None


def _point_quality(snapshot, point):
    """The served quality of one point, or None when the point is
    absent — the missing-point answer a diagnostics leg reports
    instead of raising."""
    sample = _point_sample(snapshot, point)
    return sample.get('quality') if sample is not None else None


def _forced_entry(snapshot, point):
    """The snapshot's `forces` badge for `point`, or None."""
    for entry in (snapshot or {}).get('forces', []):
        if entry.get('point') == point:
            return entry
    return None


def _settled_receipts(journal):
    """The receipts the journal settled — `command_settled` payloads."""
    return [entry.get('event', {}).get('command_settled', {})
            .get('receipt') or {}
            for entry in _journal_list(journal)]


def _ctl_write_legs(ctx, case, ctl, signals, schema, roles):
    """The dcs-ctl case's mutation legs over the closure's ctl: the
    named rejections (a write to a non-writable point, a command aimed
    at the standby peer), a receipted write, a descriptor-bounded
    set-parameter, force and unforce, promote/demote through the CLI,
    and the driven scan probe. `roles` maps each ctx endpoint key to
    the role string the case's role reads already established. Returns
    (outcome, detail, legs) — legs carries each leg's argv, exit,
    printed answer, and journaled settlement for the per-leg evidence
    files."""
    active = next((name for name, role in roles.items()
                   if role == 'active'), None)
    if active is None:
        return 'inconclusive', 'no peer reports role=active', {}
    standby = next((name for name, role in roles.items()
                    if role == 'standby'), None)
    base = ctx[active]
    target = reject = None
    for entry in signals.get('points', []):
        if entry.get('name') == 'p101-oos' and entry.get('writable') \
                and entry.get('direction') == 'in' \
                and entry.get('value_type') == 'bool':
            target = entry.get('point')
        elif reject is None and not entry.get('writable'):
            reject = entry
    if target is None:
        return 'inconclusive', 'the rig model declares no writable ' \
            'bool p101-oos point for the mutation legs', {}
    if reject is None:
        return 'inconclusive', 'the rig model declares no ' \
            'non-writable point for the named-rejection leg', {}
    rc0, baseline, _e0 = ctl(base, 'snapshot')
    if rc0 != 0 or not isinstance(baseline, dict):
        return 'inconclusive', 'dcs-ctl snapshot failed ahead of the ' \
            'mutation legs', {}
    held = _point_value(baseline, target)
    if not isinstance(held, bool):
        return 'inconclusive', 'the write target holds no bool ' \
            'served baseline: ' \
            + json.dumps(_point_sample(baseline, target))[:300], {}
    written = not held
    case.observe('mutation legs on ' + active + ' (' + base + '): '
                 'target point ' + str(target) + ' holds '
                 + str(held) + ' in the served snapshot')
    tuned = _float_tune_plan(schema, baseline)
    if tuned is None:
        return 'inconclusive', 'the served registry offers no ' \
            'descriptor-declared Float parameter the tune leg can ' \
            'exercise', {}
    tune_component, tune_name, tune_current, tune_value, _outside = tuned
    legs = {}
    seq = {'n': 0}
    pre_roles = dict(roles)

    def ctl_legged(base_url, *args):
        """One receipted submission under this leg's own actor — the
        per-leg `--actor` the journaled settlement must echo."""
        seq['n'] += 1
        actor = CTL_ACTOR + '-leg-' + str(seq['n'])
        rc, body, err = ctl(base_url, *args, '--actor', actor)
        return rc, body, err, actor

    def admitted(receipt, actor):
        """The printed receipt's half of a submission leg: a dict with
        a non-rejected outcome echoing the declared actor."""
        outcome = (receipt or {}).get('outcome') \
            if isinstance(receipt, dict) else None
        return isinstance(outcome, dict) and bool(outcome) \
            and 'rejected' not in outcome \
            and receipt.get('actor') == actor

    def journal_since(s):
        rc, entries, _err = ctl(base, 'journal', '--since', str(s))
        return entries if rc == 0 and isinstance(entries, list) else None

    def snap_of(name):
        rc, snap, _err = ctl(ctx[name], 'snapshot')
        return snap if rc == 0 and isinstance(snap, dict) else None

    def wait_snap(name, pred):
        def check():
            snap = snap_of(name)
            return snap if snap is not None and pred(snap) else None
        return wait_for(check, time.monotonic() + CTL_DEADLINE)

    def wait_role(name, role):
        def check():
            rc, report, _err = ctl(ctx[name], 'role')
            if rc == 0 and isinstance(report, dict) \
                    and report.get('role') == role:
                return report
            return None
        return wait_for(check, time.monotonic() + CTL_DEADLINE)

    def journaled_role(name, to):
        """The `role_changed` journal entry naming `to` on `name`'s
        own journal — each peer audits its own transitions."""
        def check():
            rc, journal, _err = ctl(ctx[name], 'journal',
                                    '--since', '0')
            if rc != 0 or not isinstance(journal, list):
                return None
            for item in journal:
                changed = (((item or {}).get('event') or {})
                           .get('role_changed') or {})
                if changed.get('to') == to:
                    return item
            return None
        return wait_for(check, time.monotonic() + CTL_DEADLINE)

    def wait_settled(command, actor):
        """The journaled CommandSettled receipt for this leg's command
        under this leg's actor — actor-matched like the invoke leg's
        read so an earlier leg's identical command or a checkpoint's
        carryover echo can never stand in for this submission."""
        def check():
            entries = journal_since(floor)
            if entries is None:
                return None
            for item in entries:
                receipt = (((item or {}).get('event') or {})
                           .get('command_settled') or {}) \
                          .get('receipt') or {}
                if receipt.get('command') == command \
                        and receipt.get('actor') == actor:
                    return receipt
            return None
        return wait_for(check, time.monotonic() + CTL_DEADLINE)

    def submitted(leg_name, argv, command):
        """The shared assertion head of a receipted leg: nonzero or a
        non-admitted receipt fails naming the leg; the journaled
        applied echo under the leg's actor is recorded beside the
        printed receipt. Returns the failure detail or None."""
        rc, receipt, err, actor = ctl_legged(base, *argv)
        legs[leg_name] = {'argv': list(argv) + ['--actor', actor],
                          'exit': rc, 'receipt': receipt,
                          'stderr': err, 'actor': actor}
        if rc != 0 or not admitted(receipt, actor):
            return 'the ' + leg_name + ' leg was not admitted under ' \
                'its declared actor: exit ' + str(rc) + ' ' \
                + json.dumps(receipt)[:300] + ' ' + str(err)[:200]
        settle = wait_settled(command, actor)
        legs[leg_name]['settled'] = settle
        if not isinstance(settle, dict) \
                or 'applied' not in (settle.get('outcome') or {}):
            return 'the served journal never settled the ' \
                + leg_name + ' leg applied under ' + actor
        return None

    state = {'wrote': False, 'tuned': False, 'forced': False}
    restored = {'done': False}

    def restore():
        """Best-effort return to the pre-scenario plant: roles first —
        the value, parameter, and force reverts need `base` owning the
        field again — each under the run's restore actor so the audit
        trail tells a revert from a leg."""
        if restored['done']:
            return
        restored['done'] = True
        try:
            if pre_roles:
                settled_roles = {}
                for name in pre_roles:
                    rc, report, _err = ctl(ctx[name], 'role')
                    settled_roles[name] = report.get('role') \
                        if rc == 0 and isinstance(report, dict) \
                        else None
                want = next((name for name, role in pre_roles.items()
                             if role == 'active'), None)
                holder = next((name for name, role
                               in settled_roles.items()
                               if role == 'active'), None)
                if holder is not None and holder != want:
                    ctl(ctx[holder], 'demote')
                    wait_role(holder, 'standby')
                if want is not None \
                        and settled_roles.get(want) != 'active':
                    ctl(ctx[want], 'promote')
                    wait_role(want, 'active')
            if state['forced']:
                ctl(base, 'unforce', str(target), '--actor',
                    CTL_ACTOR + '-restore')
            if state['tuned']:
                ctl(base, 'set-parameter', str(tune_component),
                    str(tune_name), repr(tune_current), '--actor',
                    CTL_ACTOR + '-restore')
            if state['wrote']:
                ctl(base, 'write', str(target),
                    'true' if held else 'false', '--actor',
                    CTL_ACTOR + '-restore')
        except Exception:
            pass

    try:
        rc, journal, err = ctl(base, 'journal', '--since', '0')
        if rc != 0 or not isinstance(journal, list):
            return 'inconclusive', 'dcs-ctl journal failed ahead of ' \
                'the mutation legs: exit ' + str(rc) + ' ' \
                + str(err)[:200], legs
        floor = max((item.get('seq') or 0 for item in journal
                     if isinstance(item, dict)), default=0)

        # The named rejections lead: both are refused at admission, so
        # they can run ahead of the mutating legs without perturbing
        # them. A write to a non-writable point names not_writable; the
        # same writable write aimed at the tracking peer names
        # not_active — and must leave the served point untouched.
        literal = {'bool': 'true', 'int': '1',
                   'float': '1.0'}.get(reject.get('value_type'), '1.0')
        rc, receipt, err, actor = ctl_legged(
            base, 'write', str(reject.get('point')), literal)
        legs['write-nonwritable'] = {
            'argv': ['write', str(reject.get('point')), literal,
                     '--actor', actor],
            'exit': rc, 'receipt': receipt, 'stderr': err,
            'actor': actor, 'point': reject.get('point')}
        if rc == 0 or _outcome_key(receipt) \
                != 'rejected:not_writable' \
                or 'not_writable' not in str(err):
            return 'failed', 'the write to a non-writable point was ' \
                'not refused by name: exit ' + str(rc) + ' ' \
                + json.dumps(receipt)[:300] + ' ' + str(err)[:200], legs
        case.observe('non-writable write refused by name: '
                     'rejected:not_writable')

        if standby is None:
            return 'inconclusive', 'the pair layout offers no ' \
                'standby peer for the directed-rejection leg', legs
        rc, receipt, err, actor = ctl_legged(
            ctx[standby], 'write', str(target),
            'true' if written else 'false')
        legs['standby-directed'] = {
            'argv': ['write', str(target),
                     'true' if written else 'false', '--actor', actor],
            'exit': rc, 'receipt': receipt, 'stderr': err,
            'actor': actor, 'peer': standby}
        if rc == 0 or _outcome_key(receipt) != 'rejected:not_active' \
                or 'not_active' not in str(err):
            return 'failed', 'the command aimed at the standby peer ' \
                'was not refused by name: exit ' + str(rc) + ' ' \
                + json.dumps(receipt)[:300] + ' ' + str(err)[:200], legs
        snap = snap_of(active)
        if snap is None or _point_value(snap, target) != held:
            return 'failed', 'the standby-directed write reached ' \
                'the served point: ' \
                + json.dumps(_point_sample(snap or {}, target))[:300], \
                legs
        case.observe('standby-directed write refused by name: '
                     'rejected:not_active; the served point still '
                     'holds ' + str(held))

        failure = submitted(
            'write', ['write', str(target),
                      'true' if written else 'false'],
            {'write_value': {'point': target, 'kind': 'bool',
                             'value': {'bool': written}}})
        if failure:
            return 'failed', failure, legs
        snap = wait_snap(active, lambda s: _point_value(s, target)
                         == written)
        if snap is None:
            return 'failed', 'the written value never surfaced in ' \
                'the served snapshot', legs
        state['wrote'] = True
        case.observe('write leg: admitted under '
                     + legs['write']['actor'] + ', journaled applied, '
                     'served snapshot now holds ' + str(written))

        failure = submitted(
            'set-parameter',
            ['set-parameter', str(tune_component), str(tune_name),
             repr(tune_value)],
            {'set_parameter': {'component': tune_component,
                               'name': tune_name,
                               'value': {'float': tune_value}}})
        if failure:
            return 'failed', failure, legs
        snap = wait_snap(active, lambda s: _parameter_value(
            s, tune_component, tune_name) == tune_value)
        if snap is None:
            return 'failed', 'the tuned value never surfaced in ' \
                'the served parameter report', legs
        state['tuned'] = True
        case.observe('set-parameter leg: ' + str(tune_component)
                     + '.' + str(tune_name) + ' -> '
                     + repr(tune_value) + ' applied, journaled, '
                     'and served')

        failure = submitted(
            'force', ['force', str(target),
                      'true' if written else 'false'],
            {'force_point': {'point': target, 'kind': 'bool',
                             'value': {'bool': written}}})
        if failure:
            return 'failed', failure, legs

        def forced(s):
            badge = _forced_entry(s, target)
            return badge is not None \
                and badge.get('value') == {'bool': written} \
                and _point_value(s, target) == written \
                and _point_quality(s, target) \
                == {'uncertain': 'substituted'}

        if wait_snap(active, forced) is None:
            return 'failed', 'the forced point never served the ' \
                'substituted badge and value', legs
        state['forced'] = True
        case.observe('force leg: point ' + str(target)
                     + ' badged under snapshot.forces at '
                     'Uncertain(Substituted) holding ' + str(written))

        failure = submitted('unforce', ['unforce', str(target)],
                            {'unforce_point': {'point': target}})
        if failure:
            return 'failed', failure, legs

        def released(s):
            return _forced_entry(s, target) is None \
                and _point_quality(s, target) == 'good'

        if wait_snap(active, released) is None:
            return 'failed', 'the forces badge never cleared ' \
                'after the release', legs
        state['forced'] = False
        case.observe('unforce leg: badge cleared, the held value '
                     're-stamped Good')

        # The switch legs: demote the field owner and promote the
        # converged tracking peer through the binary's switch verbs —
        # no --actor, per the shipped contract — each answering its
        # transitional RoleReport, settling through GET /role, and
        # journaling its own role_changed audit entries.
        rc, report, err = ctl(ctx[active], 'demote')
        legs['demote'] = {'argv': ['demote'], 'exit': rc,
                          'report': report, 'stderr': err}
        if rc != 0 or not isinstance(report, dict) \
                or report.get('role') not in ('demoting', 'standby'):
            return 'failed', 'the demote leg did not answer a ' \
                'transitional RoleReport: exit ' + str(rc) + ' ' \
                + json.dumps(report)[:300] + ' ' + str(err)[:200], legs
        legs['demote']['settled'] = wait_role(active, 'standby')
        if legs['demote']['settled'] is None:
            return 'failed', 'the demoted peer never settled ' \
                'standby through GET /role', legs
        case.observe('demote leg: transitional report '
                     + json.dumps(report.get('role'))
                     + ', peer settled standby')
        if standby is None:
            return 'inconclusive', 'the pair layout offers no ' \
                'peer to promote', legs
        rc, report, err = ctl(ctx[standby], 'promote')
        legs['promote'] = {'argv': ['promote'], 'exit': rc,
                           'report': report, 'stderr': err}
        if rc != 0 or not isinstance(report, dict) \
                or report.get('role') not in ('promoting', 'active'):
            return 'failed', 'the promote leg did not answer a ' \
                'transitional RoleReport: exit ' + str(rc) + ' ' \
                + json.dumps(report)[:300] + ' ' + str(err)[:200], legs
        legs['promote']['settled'] = wait_role(standby, 'active')
        if legs['promote']['settled'] is None:
            return 'failed', 'the promoted peer never settled ' \
                'active through GET /role', legs
        legs['demote']['role_changed'] = journaled_role(
            active, 'standby')
        legs['promote']['role_changed'] = journaled_role(
            standby, 'active')
        if legs['demote']['role_changed'] is None \
                or legs['promote']['role_changed'] is None:
            return 'failed', 'the switch legs\' role_changed ' \
                'transitions never journaled on their own peers', legs
        case.observe('switch legs: demote and promote settled '
                     'through the CLI; each peer journaled its '
                     'role_changed transition')

        # The driven-scan probe: only an externally driven instance
        # accepts POST /scan — a paced monitor answers the named
        # refusal, which is this rig's honest scan-leg evidence. The
        # run's dedicated driven peer is the natural target when the
        # context names one; otherwise the post-switch active stands
        # in and a paced refusal records the skip.
        scan_base = ctx.get('driven') or ctx[standby]
        rc, snap, err = ctl(scan_base, 'scan', '1')
        legs['scan'] = {'argv': ['scan', '1'], 'exit': rc,
                        'answer': snap, 'stderr': err,
                        'peer': scan_base}
        if rc != 0 and 'paced' in str(err):
            legs['scan']['skipped'] = 'paced monitor — the driven ' \
                'scan refused by name'
            case.observe('scan leg: paced monitor refused by name — '
                         + str(err)[:200])
        elif rc != 0:
            return 'inconclusive', 'the scan leg failed: exit ' \
                + str(rc) + ' ' + json.dumps(snap)[:300] + ' ' \
                + str(err)[:200], legs
        elif not isinstance(snap, dict):
            return 'inconclusive', 'the scan leg answered no ' \
                'snapshot: ' + json.dumps(snap)[:300], legs
        else:
            def served_at_scan():
                rc, body, _err = ctl(scan_base, 'snapshot')
                if rc == 0 and isinstance(body, dict) \
                        and (body.get('tick') or 0) \
                        >= (snap.get('tick') or 0):
                    return body
                return None

            served = wait_for(served_at_scan,
                              time.monotonic() + CTL_DEADLINE)
            if served is None:
                return 'failed', 'the served snapshot never reached ' \
                    'the scan leg\'s returned tick', legs
            case.observe('scan leg: driven scan returned tick '
                         + str(snap.get('tick'))
                         + ', the served snapshot reached it')
        return 'passed', 'mutation legs complete', legs
    except Exception as exc:
        return 'inconclusive', str(exc), legs
    finally:
        restore()


def _release_recovery_unmet(snap, target, follower, value, cone_value):
    """The unmet clauses of the post-release recovery contract on one
    served snapshot — the badge gone, the released point holding the
    force's last stamp at Good, the inverted cone untainted at the
    unforced read. Empty when the observation shows the recovered
    state."""
    unmet = []
    if _forced_entry(snap, target) is not None:
        unmet.append('an empty forces list')
    if _point_value(snap, target) != value:
        unmet.append('the held image at ' + str(value))
    if _point_quality(snap, target) != 'good':
        unmet.append('Good quality — the point reads '
                     + json.dumps(_point_quality(snap, target)))
    if _point_value(snap, follower) != cone_value:
        unmet.append('p101-oos-ok reading ' + str(cone_value))
    if _point_quality(snap, follower) != 'good':
        unmet.append('the p101-oos-ok cone untainted')
    return unmet


def scenario_force_release(ctx):
    """A receipted force pins p101-oos at Substituted quality with the
    control image following it; its release re-stamps the held image
    Good on the active and on the tracking standby's adopted
    snapshot, then the restore write returns the pre-force held
    value — every command journaled as a settled, attributed
    receipt."""
    case = Case('force-release',
                'Receipted forcing and release on a writable point',
                'force_point on the writable p101-oos point serves the '
                'forced value at Uncertain(Substituted), lists the '
                'point under snapshot.forces, and the inverted '
                'p101-oos-ok carrier follows the forced value; '
                'unforce_point clears the badge and re-stamps the '
                'held image Good on the same observation — the '
                'released sample reads the persisted stamp and the '
                'p101-oos-ok cone untaints, and the tracking '
                'standby\'s adopted snapshot shows the same '
                'post-release state — before the restore write '
                'returns the pre-force held value; both commands '
                'journal as settled receipts attributed to qa-lane')
    try:
        # Self-contained on either role layout, like evidence-capture:
        # replayed alone the rig is fresh (ctrl-a active), while the
        # full suite reaches this case after the failover.
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        peer = 'standby' if active == 'active' else 'active'
        base, peer_base = ctx[active], ctx[peer]
        case.observe('forcing against ' + active + ' (' + base
                     + '); tracking peer ' + peer + ' (' + peer_base
                     + ')')

        # The standby-parity leg needs a settled pair: without a
        # tracking peer the adopted-state check cannot be exercised.
        def converged():
            try:
                report = _role(ctx, peer_base)
            except Exception:
                return None
            sync = report.get('sync') or {}
            return report if 'tracking' in sync else None

        tracking = wait_for(converged, time.monotonic() + FORCE_DEADLINE)
        ref = save_evidence(ctx['evidence_dir'],
                            'force-release-peer-role.json',
                            {'peer': peer, 'report': tracking})
        case.evidence('file', ref, 'the tracking peer\'s role report')
        if not tracking:
            return case.finish('inconclusive', 'the peer never '
                               'reported tracking convergence — the '
                               'standby parity leg cannot be exercised')

        _, signals = http_json('GET', base + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'force-release-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming the force target')
        target = follower = None
        for entry in signals.get('points', []):
            if entry.get('name') == 'p101-oos' and entry.get('writable') \
                    and entry.get('direction') == 'in':
                target = entry.get('point')
            elif entry.get('name') == 'p101-oos-ok':
                follower = entry.get('point')
        if target is None or follower is None:
            return case.finish(
                'inconclusive',
                'the rig model lacks the writable p101-oos point or '
                'its p101-oos-ok in-service carrier')

        # The held value the release leg restores — whatever the run's
        # earlier commands left the operator point holding.
        baseline = _snapshot(ctx, base)
        held = _point_value(baseline, target)
        if not isinstance(held, bool):
            return case.finish(
                'inconclusive',
                'the force target holds no bool baseline: '
                + json.dumps(_point_sample(baseline, target))[:300])
        forced_value = not held
        case.observe('force target: p101-oos point ' + str(target)
                     + ' held ' + str(held) + '; control probe '
                     'p101-oos-ok point ' + str(follower)
                     + ' (the inverted in-service carrier)')

        force_body = {'point': target, 'kind': 'bool',
                      'value': {'bool': forced_value}}
        status, receipt = http_json(
            'POST', base + '/command',
            {'command': {'force_point': force_body}, 'actor': 'qa-lane'})
        ref = save_evidence(ctx['evidence_dir'],
                            'force-release-force-receipt.json',
                            {'status': status, 'body': receipt})
        case.evidence('file', ref, 'the force submission receipt')
        outcome = (receipt or {}).get('outcome') or {}
        if status != 200 or 'rejected' in outcome:
            return case.finish('failed', 'force refused: ' + str(status)
                               + ' ' + json.dumps(receipt)[:400])
        case.observe('force admitted: '
                     + json.dumps(outcome, sort_keys=True))

        observed = {}

        def forced_state():
            try:
                snap = _snapshot(ctx, base)
            except Exception:
                return None
            observed['forced'] = snap
            badge = _forced_entry(snap, target)
            if _point_value(snap, target) == forced_value \
                    and _point_quality(snap, target) \
                    == {'uncertain': 'substituted'} \
                    and (badge or {}).get('value') \
                    == {'bool': forced_value} \
                    and _point_value(snap, follower) == held:
                return snap
            return None

        forced = wait_for(forced_state, time.monotonic() + FORCE_DEADLINE)
        ref = save_evidence(ctx['evidence_dir'],
                            'force-release-forced.json',
                            observed.get('forced') or {})
        case.evidence('file', ref, 'snapshot while the force stands')
        if forced is None:
            snap = observed.get('forced') or {}
            unmet = []
            if _point_value(snap, target) != forced_value:
                unmet.append('the forced value ' + str(forced_value))
            if _point_quality(snap, target) \
                    != {'uncertain': 'substituted'}:
                unmet.append('Uncertain(Substituted) quality')
            if (_forced_entry(snap, target) or {}).get('value') \
                    != {'bool': forced_value}:
                unmet.append('a snapshot.forces entry')
            if _point_value(snap, follower) != held:
                unmet.append('control following the force '
                             '(p101-oos-ok reading ' + str(held) + ')')
            return case.finish('failed', 'forced telemetry never '
                               'showed ' + ' + '.join(unmet))
        case.observe('forced: point ' + str(target) + ' reads '
                     + str(forced_value)
                     + ' at Uncertain(Substituted), badged under '
                     'snapshot.forces; p101-oos-ok follows at '
                     + str(held))

        unforce_body = {'point': target}
        status, receipt = http_json(
            'POST', base + '/command',
            {'command': {'unforce_point': unforce_body},
             'actor': 'qa-lane'})
        ref = save_evidence(ctx['evidence_dir'],
                            'force-release-release-receipt.json',
                            {'status': status, 'body': receipt})
        case.evidence('file', ref, 'the release submission receipt')
        outcome = (receipt or {}).get('outcome') or {}
        if status != 200 or 'rejected' in outcome:
            return case.finish('failed', 'release refused: '
                               + str(status) + ' '
                               + json.dumps(receipt)[:400])
        case.observe('release admitted: '
                     + json.dumps(outcome, sort_keys=True))

        def released():
            try:
                snap = _snapshot(ctx, base)
            except Exception:
                return None
            observed['released'] = snap
            return _forced_entry(snap, target) is None and snap

        cleared = wait_for(released, time.monotonic() + FORCE_DEADLINE)
        ref = save_evidence(ctx['evidence_dir'],
                            'force-release-released.json',
                            observed.get('released') or {})
        case.evidence('file', ref, 'snapshot after the release settled')
        if not cleared:
            return case.finish('failed',
                               'the forces badge never cleared after '
                               'unforce_point')

        # Finding #498's pinned regression: the release boundary
        # re-stamps the held image Good — the force's last stamp
        # persists as the held sample — and the inverted p101-oos-ok
        # cone untaints on the same observation. A released point
        # still stamped Substituted here is the stuck image the
        # finding reported; the restore write below must not be what
        # papers it over.
        def release_recovered():
            try:
                snap = _snapshot(ctx, base)
            except Exception:
                return None
            observed['release_recovered'] = snap
            return not _release_recovery_unmet(
                snap, target, follower, forced_value, held) and snap

        if not wait_for(release_recovered,
                        time.monotonic() + FORCE_DEADLINE):
            unmet = _release_recovery_unmet(
                observed.get('release_recovered') or {},
                target, follower, forced_value, held)
            return case.finish('failed', 'the released point did not '
                               'recover: ' + ' + '.join(unmet))
        ref = save_evidence(ctx['evidence_dir'],
                            'force-release-release-recovered.json',
                            observed.get('release_recovered') or {})
        case.evidence('file', ref, 'the released snapshot before the '
                      'restore write — held image at Good, cone '
                      'untainted')
        case.observe('released: point ' + str(target) + ' reads '
                     + str(forced_value) + ' at Good with the '
                     'p101-oos-ok cone untainted at ' + str(held)
                     + ' — no restamp write needed')

        # The finding tainted the tracking standby too: its adopted
        # snapshot must carry the same post-release state — never a
        # permanently Substituted cone on either peer.
        def peer_recovered():
            try:
                snap = _snapshot(ctx, peer_base)
            except Exception:
                return None
            observed['peer_recovered'] = snap
            return not _release_recovery_unmet(
                snap, target, follower, forced_value, held) and snap

        if not wait_for(peer_recovered,
                        time.monotonic() + FORCE_DEADLINE):
            unmet = _release_recovery_unmet(
                observed.get('peer_recovered') or {},
                target, follower, forced_value, held)
            return case.finish('failed', 'the tracking standby\'s '
                               'adopted snapshot never showed the '
                               'released state: ' + ' + '.join(unmet))
        ref = save_evidence(ctx['evidence_dir'],
                            'force-release-peer-recovered.json',
                            observed.get('peer_recovered') or {})
        case.evidence('file', ref, 'the tracking standby\'s adopted '
                      'snapshot carrying the same recovered state')
        case.observe('the tracking standby\'s adopted snapshot '
                     'matches: point ' + str(target) + ' at Good, '
                     'the cone untainted')

        # The restore step: the release is proven above, so this
        # receipted write only restamps the pre-force held value —
        # later scenarios find the rig in its prior state.
        status, receipt = http_json(
            'POST', base + '/command',
            {'command': {'write_value': {
                'point': target, 'kind': 'bool',
                'value': {'bool': held}}},
             'actor': 'qa-lane'})
        ref = save_evidence(ctx['evidence_dir'],
                            'force-release-restore-receipt.json',
                            {'status': status, 'body': receipt})
        case.evidence('file', ref, 'the restore-write submission receipt')
        outcome = (receipt or {}).get('outcome') or {}
        if status != 200 or 'rejected' in outcome:
            return case.finish('failed', 'the restore write was '
                               'refused: ' + str(status) + ' '
                               + json.dumps(receipt)[:400])

        def recovered():
            try:
                snap = _snapshot(ctx, base)
            except Exception:
                return None
            observed['recovered'] = snap
            if _point_value(snap, target) == held \
                    and _point_quality(snap, target) == 'good' \
                    and _forced_entry(snap, target) is None \
                    and _point_value(snap, follower) == forced_value:
                return snap
            return None

        if not wait_for(recovered, time.monotonic() + FORCE_DEADLINE):
            snap = observed.get('recovered') or {}
            unmet = []
            if _point_value(snap, target) != held:
                unmet.append('the held value ' + str(held))
            if _point_quality(snap, target) != 'good':
                unmet.append('Good quality')
            if _forced_entry(snap, target) is not None:
                unmet.append('an empty forces list')
            if _point_value(snap, follower) != forced_value:
                unmet.append('control recovering (p101-oos-ok reading '
                             + str(forced_value) + ')')
            return case.finish('failed', 'telemetry did not recover '
                               'after release: ' + ' + '.join(unmet))
        ref = save_evidence(ctx['evidence_dir'],
                            'force-release-recovered.json',
                            observed.get('recovered') or {})
        case.evidence('file', ref, 'snapshot after the restore write')
        case.observe('released and restored: point ' + str(target)
                     + ' reads ' + str(held) + ' at Good, forces '
                     'cleared, p101-oos-ok back at '
                     + str(forced_value))

        # Both commands must journal as settled receipts carrying the
        # run's actor — the audit half of the receipted-command
        # contract.
        found = {'force': None, 'release': None}

        def settled():
            try:
                _, journal = http_json('GET', base + '/journal?since=0')
            except Exception:
                return None
            observed['journal'] = journal
            for entry in _settled_receipts(journal):
                command = entry.get('command') or {}
                if command.get('force_point') == force_body:
                    found['force'] = entry
                elif command.get('unforce_point') == unforce_body:
                    found['release'] = entry
            return (found['force'] is not None
                    and found['release'] is not None) or None

        wait_for(settled, time.monotonic() + FORCE_DEADLINE)
        ref = save_evidence(ctx['evidence_dir'],
                            'force-release-journal.json',
                            observed.get('journal') or [])
        case.evidence('file', ref, 'journal tail with the settled '
                      'receipts')
        unmet = []
        for name, entry in (('force', found['force']),
                            ('release', found['release'])):
            if entry is None:
                unmet.append('no settled ' + name
                             + ' receipt journaled')
                continue
            if entry.get('actor') != 'qa-lane':
                unmet.append('the ' + name + ' receipt is unattributed '
                             '(actor='
                             + json.dumps(entry.get('actor')) + ')')
            if 'applied' not in (entry.get('outcome') or {}):
                unmet.append('the ' + name + ' receipt did not settle '
                             'applied: '
                             + json.dumps(entry.get('outcome'))[:200])
        if unmet:
            return case.finish('failed', 'journal audit: '
                               + '; '.join(unmet))
        case.observe('journal: force and release settled as applied '
                     'receipts attributed to qa-lane')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))


# --------------------------------------------------------------------
# The receipted force set as carried run state (WW-LCM-001's
# continuity clause, WW-OPS-003's substituted-quality semantics,
# decision 21's checkpoint-carried run state): `force_point` on the
# same writable `In` point the static-active case uses rides the
# checkpoint stream — the tracking standby's own snapshot reports the
# badge because its scans run the adopted state — and a promoted peer
# must inherit the force rather than silently releasing it. The
# release on the promoted peer settles applied and the held-value
# rule resumes: the last-stamped sample re-stamps Good. The case runs
# inside the pre-switch window — a settled active plus a tracking
# peer — on either role layout (ctrl-a/ctrl-b ahead of the tune
# case's switch, or the mirrored post-failover pair a replay finds),
# and it restores the launch role assignment through the documented
# demote/promote fail-back before later cases run.


def scenario_force_carryover(ctx):
    """A receipted force riding the checkpoint survives the pair's
    promotion; the release on the promoted peer settles applied and
    the held-value rule resumes at Good — then the pair returns to
    its launch role assignment."""
    case = Case('force-carryover',
                'Receipted force carries across promotion',
                'force_point on the writable p101-oos point badges the '
                'point under snapshot.forces at Uncertain(Substituted) '
                'on the active AND on the tracking standby\'s own '
                'snapshot; the promoted peer still badges it at the '
                'forced value and substituted quality with scans '
                'advancing; unforce_point on the new active settles '
                'applied, clears the badge, and the point resumes its '
                'unforced value at Good; the pair returns to its '
                'pre-scenario role assignment')
    try:
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        peer = 'standby' if active == 'active' else 'active'
        base, peer_base = ctx[active], ctx[peer]
        case.observe('forcing against ' + active + ' (' + base
                     + '); tracking peer ' + peer + ' (' + peer_base
                     + ')')

        def converged():
            try:
                report = _role(ctx, peer_base)
            except Exception:
                return None
            sync = report.get('sync') or {}
            return report if 'tracking' in sync else None

        tracking = wait_for(converged, time.monotonic() + FORCE_DEADLINE)
        ref = save_evidence(ctx['evidence_dir'],
                            'force-carryover-peer-role.json',
                            {'peer': peer, 'report': tracking})
        case.evidence('file', ref, 'the tracking peer\'s role report')
        if not tracking:
            return case.finish('inconclusive', 'the peer never '
                               'reported tracking convergence — the '
                               'carryover cannot be exercised')

        _, signals = http_json('GET', base + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'force-carryover-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming the force '
                      'target')
        target = None
        for entry in signals.get('points', []):
            if entry.get('name') == 'p101-oos' and entry.get('writable') \
                    and entry.get('direction') == 'in':
                target = entry.get('point')
                break
        if target is None:
            return case.finish('inconclusive', 'the rig model lacks '
                               'the writable p101-oos point')

        baseline = _snapshot(ctx, base)
        held = _point_value(baseline, target)
        if not isinstance(held, bool):
            return case.finish(
                'inconclusive',
                'the force target holds no bool baseline: '
                + json.dumps(_point_sample(baseline, target))[:300])
        forced_value = not held
        case.observe('force target: p101-oos point ' + str(target)
                     + ' held ' + str(held) + '; forcing '
                     + str(forced_value))

        force_body = {'point': target, 'kind': 'bool',
                      'value': {'bool': forced_value}}
        status, receipt = http_json(
            'POST', base + '/command',
            {'command': {'force_point': force_body}, 'actor': 'qa-lane'})
        ref = save_evidence(ctx['evidence_dir'],
                            'force-carryover-force-receipt.json',
                            {'status': status, 'body': receipt})
        case.evidence('file', ref, 'the force submission receipt')
        outcome = (receipt or {}).get('outcome') or {}
        if status != 200 or 'rejected' in outcome:
            return case.finish('failed', 'force refused: ' + str(status)
                               + ' ' + json.dumps(receipt)[:400])
        case.observe('force admitted: '
                     + json.dumps(outcome, sort_keys=True))

        observed = {}

        def forced_state():
            try:
                snap = _snapshot(ctx, base)
            except Exception:
                return None
            observed['forced'] = snap
            badge = _forced_entry(snap, target)
            if _point_value(snap, target) == forced_value \
                    and _point_quality(snap, target) \
                    == {'uncertain': 'substituted'} \
                    and (badge or {}).get('value') \
                    == {'bool': forced_value}:
                return snap
            return None

        forced = wait_for(forced_state,
                          time.monotonic() + FORCE_DEADLINE)
        ref = save_evidence(ctx['evidence_dir'],
                            'force-carryover-forced.json',
                            observed.get('forced') or {})
        case.evidence('file', ref, 'the active\'s snapshot while the '
                      'force stands')
        if forced is None:
            snap = observed.get('forced') or {}
            unmet = []
            if _point_value(snap, target) != forced_value:
                unmet.append('the forced value ' + str(forced_value))
            if _point_quality(snap, target) \
                    != {'uncertain': 'substituted'}:
                unmet.append('Uncertain(Substituted) quality')
            if (_forced_entry(snap, target) or {}).get('value') \
                    != {'bool': forced_value}:
                unmet.append('a snapshot.forces entry')
            return case.finish('failed', 'forced telemetry never '
                               'showed ' + ' + '.join(unmet))
        case.observe('forced on the active: point ' + str(target)
                     + ' reads ' + str(forced_value)
                     + ' at Uncertain(Substituted), badged under '
                     'snapshot.forces')

        # The standby's own snapshot must report the same force while
        # it tracks — its scans run the adopted state the checkpoint
        # carries, badge and substituted stamp included.
        def peer_forced():
            try:
                snap = _snapshot(ctx, peer_base)
            except Exception:
                return None
            observed['peer_forced'] = snap
            badge = _forced_entry(snap, target)
            if _point_value(snap, target) == forced_value \
                    and _point_quality(snap, target) \
                    == {'uncertain': 'substituted'} \
                    and (badge or {}).get('value') \
                    == {'bool': forced_value}:
                return snap
            return None

        peer_snap = wait_for(peer_forced,
                             time.monotonic() + FORCE_DEADLINE)
        ref = save_evidence(ctx['evidence_dir'],
                            'force-carryover-peer-forced.json',
                            observed.get('peer_forced') or {})
        case.evidence('file', ref, 'the tracking standby\'s own '
                      'snapshot while the force stands')
        if peer_snap is None:
            return case.finish('failed', 'the tracking standby never '
                               'reported the force on its own snapshot '
                               '— the adopted state dropped the badge')
        case.observe('the tracking standby reports the same force at '
                     'Uncertain(Substituted) — the badge rides the '
                     'checkpoint')

        # The switch: demote the forced active, promote the converged
        # standby — the receipted run state must cross with it.
        status, body = http_json('POST', base + '/demote')
        case.observe('demote ' + active + ': ' + str(status) + ' '
                     + json.dumps(body))
        if status != 200:
            return case.finish('failed', 'demote refused: '
                               + str(body))
        promoted = None
        deadline = time.monotonic() + FORCE_DEADLINE
        while time.monotonic() < deadline and promoted is None:
            try:
                status, body = http_json('POST', peer_base + '/promote')
                if status == 200:
                    promoted = body
                else:
                    time.sleep(POLL_INTERVAL)
            except urllib.error.HTTPError as exc:
                if exc.code == 409:
                    time.sleep(POLL_INTERVAL)
                else:
                    raise
        settled_role = wait_for(
            lambda: (r.get('role') == 'active' and r or None)
            if (r := _role(ctx, peer_base)) else None,
            time.monotonic() + FORCE_DEADLINE)
        ref = save_evidence(ctx['evidence_dir'],
                            'force-carryover-promotion.json',
                            {'demoted': active, 'promote': promoted,
                             'role': settled_role})
        case.evidence('file', ref, 'the demote/promote responses and '
                      'the promoted peer\'s role')
        if promoted is None:
            return case.finish('failed', 'the converged standby never '
                               'promoted within ' + str(FORCE_DEADLINE)
                               + 's')
        if not settled_role:
            return case.finish('failed', 'the promoted peer did not '
                               'settle active')

        # The promoted peer: the badge must still name the point at
        # the forced value and substituted quality while its scans
        # keep advancing — a force silently cleared or reverted at
        # the promotion boundary fails here.
        first = _try_snapshot(ctx, peer_base) or {}

        def carried():
            try:
                snap = _snapshot(ctx, peer_base)
            except Exception:
                return None
            observed['carried'] = snap
            badge = _forced_entry(snap, target)
            if _point_value(snap, target) == forced_value \
                    and _point_quality(snap, target) \
                    == {'uncertain': 'substituted'} \
                    and (badge or {}).get('value') \
                    == {'bool': forced_value} \
                    and snap.get('tick', 0) > first.get('tick', 0):
                return snap
            return None

        carried_snap = wait_for(carried,
                                time.monotonic() + FORCE_DEADLINE)
        ref = save_evidence(ctx['evidence_dir'],
                            'force-carryover-promoted.json',
                            observed.get('carried') or first)
        case.evidence('file', ref, 'the promoted peer\'s snapshot')
        if carried_snap is None:
            snap = observed.get('carried') or first
            unmet = []
            if (_forced_entry(snap, target) or {}).get('value') \
                    != {'bool': forced_value}:
                unmet.append('the forces badge for point '
                             + str(target))
            if _point_value(snap, target) != forced_value:
                unmet.append('the forced value ' + str(forced_value))
            if _point_quality(snap, target) \
                    != {'uncertain': 'substituted'}:
                unmet.append('Uncertain(Substituted) quality')
            if not snap or snap.get('tick', 0) <= first.get('tick', 0):
                unmet.append('advancing scans')
            return case.finish('failed', 'the promoted peer lost '
                               + ' + '.join(unmet)
                               + ' — the force did not ride the '
                               'checkpoint')
        case.observe('the promoted peer still badges point '
                     + str(target) + ' at ' + str(forced_value)
                     + '/substituted, tick ' + str(first.get('tick'))
                     + ' -> ' + str(carried_snap.get('tick')))

        # The release on the new active: a settled `applied` receipt,
        # an empty forces list, and the held-value rule resuming at
        # Good — the force's last stamp persists as the held sample.
        _, before = http_json('GET', peer_base + '/receipts')
        index = len(_receipt_list(before))
        unforce_body = {'point': target}
        status, receipt = http_json(
            'POST', peer_base + '/command',
            {'command': {'unforce_point': unforce_body},
             'actor': 'qa-lane'})
        ref = save_evidence(ctx['evidence_dir'],
                            'force-carryover-release-receipt.json',
                            {'status': status, 'body': receipt})
        case.evidence('file', ref, 'the release submission receipt')
        outcome = (receipt or {}).get('outcome') or {}
        if status != 200 or 'rejected' in outcome:
            return case.finish('failed', 'release refused: '
                               + str(status) + ' '
                               + json.dumps(receipt)[:400])
        settled = wait_for(
            lambda: _settled_outcome(ctx, peer_base, index),
            time.monotonic() + FORCE_DEADLINE)
        if settled != 'applied':
            return case.finish('failed', 'the release on the promoted '
                               'peer did not settle applied: '
                               + str(settled or 'never settled'))

        def released():
            try:
                snap = _snapshot(ctx, peer_base)
            except Exception:
                return None
            observed['released'] = snap
            if _forced_entry(snap, target) is None \
                    and _point_value(snap, target) == forced_value \
                    and _point_quality(snap, target) == 'good':
                return snap
            return None

        if not wait_for(released, time.monotonic() + FORCE_DEADLINE):
            snap = observed.get('released') or {}
            unmet = []
            if _forced_entry(snap, target) is not None:
                unmet.append('an empty forces list')
            if _point_value(snap, target) != forced_value:
                unmet.append('the held value ' + str(forced_value))
            if _point_quality(snap, target) != 'good':
                unmet.append('Good quality (the point re-substituted?)')
            return case.finish('failed', 'the released point did not '
                               'recover: ' + ' + '.join(unmet))
        ref = save_evidence(ctx['evidence_dir'],
                            'force-carryover-released.json',
                            observed.get('released') or {})
        case.evidence('file', ref, 'the snapshot after the release '
                      'settled')
        case.observe('released on the promoted peer: forces cleared, '
                     'point ' + str(target) + ' reads '
                     + str(forced_value) + ' at Good')

        # The run's audit on the promoted peer: the release settles
        # journaled as applied, attributed to qa-lane.
        found = {}

        def journaled():
            try:
                _, journal = http_json('GET', peer_base
                                       + '/journal?since=0')
            except Exception:
                return None
            observed['journal'] = journal
            for entry in _settled_receipts(journal):
                if (entry.get('command') or {}) \
                        .get('unforce_point') == unforce_body:
                    found['release'] = entry
            return found.get('release') is not None or None

        wait_for(journaled, time.monotonic() + FORCE_DEADLINE)
        ref = save_evidence(ctx['evidence_dir'],
                            'force-carryover-journal.json',
                            observed.get('journal') or [])
        case.evidence('file', ref, 'the promoted peer\'s journal')
        entry = found.get('release')
        if entry is None:
            return case.finish('failed', 'no settled release receipt '
                               'journaled on the promoted peer')
        if entry.get('actor') != 'qa-lane':
            return case.finish('failed', 'the release receipt is '
                               'unattributed (actor='
                               + json.dumps(entry.get('actor')) + ')')
        if 'applied' not in (entry.get('outcome') or {}):
            return case.finish('failed', 'the release receipt did not '
                               'settle applied: '
                               + json.dumps(entry.get('outcome'))[:200])

        # Restore the pre-scenario role assignment: demote the new
        # active, promote the reconverged original back — the
        # documented fail-back the suite's later cases rely on.
        status, body = http_json('POST', peer_base + '/demote')
        case.observe('restore demote ' + peer + ': ' + str(status)
                     + ' ' + json.dumps(body))
        if status != 200:
            return case.finish('failed', 'the restore demote was '
                               'refused: ' + str(body))
        restored = None
        deadline = time.monotonic() + FORCE_DEADLINE
        while time.monotonic() < deadline and restored is None:
            try:
                status, body = http_json('POST', base + '/promote')
                if status == 200:
                    restored = body
                else:
                    time.sleep(POLL_INTERVAL)
            except urllib.error.HTTPError as exc:
                if exc.code == 409:
                    time.sleep(POLL_INTERVAL)
                else:
                    raise
        settled_back = wait_for(
            lambda: (r.get('role') == 'active' and r or None)
            if (r := _role(ctx, base)) else None,
            time.monotonic() + FORCE_DEADLINE)
        # The launch assignment is a role pair, not one role: the
        # demoted successor must be back tracking the restored active,
        # or the next carryover leg has no converged peer to promote.
        peer_back = wait_for(
            lambda: (r.get('role') == 'standby'
                     and 'tracking' in (r.get('sync') or {})
                     and r or None)
            if (r := _role(ctx, peer_base)) else None,
            time.monotonic() + FORCE_DEADLINE)
        ref = save_evidence(ctx['evidence_dir'],
                            'force-carryover-restored.json',
                            {'demoted': peer, 'promote': restored,
                             'role': settled_back, 'peer': peer_back})
        case.evidence('file', ref, 'the fail-back responses and the '
                      'restored roles')
        if restored is None or not settled_back or not peer_back:
            return case.finish('failed', 'the pair is not restored '
                               'to its pre-scenario role assignment')
        case.observe('restored: ' + active + ' reports active again, '
                     + peer + ' returns to tracking')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))


# --------------------------------------------------------------------
# The consumer-failure schedule (WW-FND-004, decision 83): the lane's
# per-revision proof that a slow, disconnected, malformed, or restarted
# consumer can never reach the control loop. Every leg measures the
# same signature — one publication per completed scan, an honest
# seq-cursor history stream, identical receipted command outcomes —
# while one consumer behavior overlaps it, and each interference leg's
# signature must equal the bracketing no-consumer reference legs'.

LEG_TICKS = 8       # completed scans each leg's window spans
LEG_POLL = 0.1      # measurement cadence inside a leg's window
LEG_DEADLINE = 30   # bound on one leg's window or a settlement wait
FLOOD_BATCH = 40    # journaled submissions per journal-flood round
FLOOD_ROUNDS = 40   # rounds cap — 1600 submissions bound the roll

# The command-admission flood (decision 83's bounded-ingress half):
# each pipelined burst submits twice the served queue bound on one
# keep-alive connection — the whole batch lands inside the server's
# read buffer faster than a scan boundary can drain pending entries —
# and rounds repeat until the named queue_full rejection appears. A
# trickle keeps validated submissions arriving inside the measured leg.
ADMISSION_ROUNDS = 8        # pipelined bursts before the flood is 'insufficient'
ADMISSION_TRICKLE = 8       # submissions per poll round inside a flood leg
ADMISSION_MAX_CAPACITY = 512  # a served bound past this is beyond the lane's reach

# The malformed set the consumer schedules declare, each with the
# status the documented endpoints answer: unparseable bodies and bad
# queries are 400, unknown paths and refused verbs 404, a valid
# POST /scan meets the paced monitor's named 409, and POST /promote on
# the settled active the named already_active 409. No well-formed
# command appears — a receipted command is a run input, not
# interference.
MALFORMED_PROBES = (
    ('GET', '/nonexistent', None, 404),
    ('POST', '/snapshot', None, 404),
    ('DELETE', '/receipts', None, 404),
    ('PUT', '/scan', None, 404),
    ('GET', '/history?point=abc', None, 400),
    ('GET', '/history?since=-1', None, 400),
    ('GET', '/journal?since=soon', None, 400),
    ('POST', '/command', '{', 400),
    ('POST', '/command', '{"command":{"bogus":1}}', 400),
    ('POST', '/command', '{"actor":3}', 400),
    ('POST', '/command',
     '{"write_value":{"point":10,"kind":"float","value":"high"}}', 400),
    ('POST', '/scan', '{', 400),
    ('POST', '/scan', '{"scans":-1}', 400),
    ('POST', '/scan', '{"scans":1}', 409),
    ('POST', '/promote', None, 409),
)


def _request_status(method, url, body=None, timeout=10):
    """(status, raw body) — unlike http_json, a non-2xx answer returns
    instead of raising: the malformed probes' refusals are the data."""
    if isinstance(body, str):
        data = body.encode()
    elif body is not None:
        data = json.dumps(body).encode()
    else:
        data = None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        exc.read()
        return exc.code, None


def _connect(base, timeout=5):
    """A raw TCP connection to a monitor base URL — the transport the
    held/churning/raw-probe consumers speak below the HTTP layer."""
    host = base.split('://', 1)[-1]
    hostname, _, port = host.rpartition(':')
    return socket.create_connection(
        (hostname or '127.0.0.1', int(port or 80)), timeout=timeout)


def _parse_responses(raw):
    """Split `raw` into as many complete HTTP responses as it holds.
    Returns ([(status, json-body-or-None)], leftover) — a truncated or
    unframed answer stays in leftover for the next chunk."""
    replies = []
    while raw:
        head, sep, rest = raw.partition(b'\r\n\r\n')
        if not sep:
            break
        lines = head.split(b'\r\n')
        try:
            status = int(lines[0].split(None, 2)[1])
        except (IndexError, ValueError):
            status = None
        length = None
        for line in lines[1:]:
            name, colon, value = line.partition(b':')
            if colon and name.strip().lower() == b'content-length':
                try:
                    length = int(value.strip())
                except ValueError:
                    length = None
        if length is None or len(rest) < length:
            break
        payload, raw = rest[:length], rest[length:]
        try:
            replies.append((status, json.loads(payload)))
        except ValueError:
            replies.append((status, None))
    return replies, raw


def _pipelined_commands(base, commands, timeout=15):
    """POST every command envelope on one keep-alive connection, all
    requests sent back-to-back before the first answer is read — the
    flood channel: submissions land inside the server's read buffer
    faster than a scan boundary can drain the pending queue. Returns
    [(status, receipt-or-None)] in submission order, one entry per
    command; a missing or unparseable answer reads (None, None) — the
    no-receipt case the admission contract forbids."""
    bodies = [json.dumps(command).encode() for command in commands]
    request = b''
    for index, body in enumerate(bodies):
        tail = b'Connection: close\r\n' if index == len(bodies) - 1 else b''
        request += (b'POST /command HTTP/1.1\r\nHost: qa\r\n'
                    b'Content-Type: application/json\r\nContent-Length: '
                    + str(len(body)).encode() + b'\r\n' + tail + b'\r\n'
                    + body)
    stream = _connect(base, timeout=timeout)
    try:
        stream.sendall(request)
        stream.settimeout(timeout)
        replies, raw = [], b''
        deadline = time.monotonic() + timeout
        while len(replies) < len(commands) \
                and time.monotonic() < deadline:
            try:
                chunk = stream.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            raw += chunk
            found, raw = _parse_responses(raw)
            replies += found
        found, raw = _parse_responses(raw)
        replies += found
        return (replies + [(None, None)] * len(commands))[:len(commands)]
    finally:
        stream.close()


def _publication(snapshot):
    """A snapshot's `publication` section — the store's overload
    counters as of that publish: produced, coalesced, retained depth,
    configured window."""
    return snapshot.get('publication') or {}


def _history_seqs(payload, point):
    """One point's served history seqs out of a `/history` answer."""
    if isinstance(payload, list):
        for entry in payload:
            if entry.get('point') == point:
                return [sample.get('seq')
                        for sample in entry.get('samples', [])]
    return []


def _history_cursor(ctx, base, point):
    """A consumer's `?since=` cursor on the point's history stream —
    the newest served seq, or 0 while the stream is empty."""
    _, body = http_json('GET', base + '/history?point=' + str(point)
                        + '&since=0')
    seqs = _history_seqs(body, point)
    return seqs[-1] if seqs else 0


def _journal_seqs(payload):
    return [entry.get('seq') for entry in _journal_list(payload)]


def _journal_cursor(ctx, base):
    """The newest served journal seq — a consumer's `?since=` cursor."""
    _, body = http_json('GET', base + '/journal?since=0')
    seqs = _journal_seqs(body)
    return seqs[-1] if seqs else 0


def _journal_first(ctx, base):
    """The oldest retained journal seq — where the bounded window
    currently opens."""
    _, body = http_json('GET', base + '/journal?since=0')
    seqs = _journal_seqs(body)
    return seqs[0] if seqs else 0


def _outcome_key(receipt):
    """A receipt's normalized verdict — 'accepted', 'applied', or
    'rejected:<reason>' — the cross-leg comparable."""
    outcome = (receipt or {}).get('outcome')
    if not isinstance(outcome, dict) or not outcome:
        return 'unknown'
    name = next(iter(outcome))
    if name == 'rejected':
        body = outcome.get('rejected')
        reason = body.get('reason') if isinstance(body, dict) else {}
        return 'rejected:' + (next(iter(reason))
                              if isinstance(reason, dict) and reason
                              else '?')
    return name


def _settled_outcome(ctx, base, index):
    """The outcome key of `receipts[index]` once it is final — None
    while it still reads `accepted` or the log cannot be read."""
    try:
        _, body = http_json('GET', base + '/receipts')
    except Exception:
        return None
    receipts = _receipt_list(body)
    if len(receipts) <= index:
        return None
    outcome = _outcome_key(receipts[index])
    return None if outcome == 'accepted' else outcome


def _try_snapshot(ctx, base):
    """`/snapshot` or None — for wait loops a dropped read is one lost
    sample, not the leg's verdict."""
    try:
        return _snapshot(ctx, base)
    except Exception:
        return None


def _signal_targets(signals):
    """The scenario's probe points out of the SignalIndex: a writable
    bool command point (the operator-command target 'p101-oos' when
    present), any non-writable point for the named rejection, and the
    point whose history stream the legs watch."""
    write = reject = None
    for entry in signals.get('points', []):
        if entry.get('name') == 'p101-oos' and entry.get('writable'):
            write = entry.get('point')
        elif write is None and entry.get('writable') \
                and entry.get('direction') == 'in' \
                and entry.get('value_type') == 'bool':
            write = entry.get('point')
        if reject is None and not entry.get('writable'):
            reject = entry.get('point')
    if write is None or reject is None:
        return None
    return {'write': write, 'reject': reject, 'watch': write}


class _Overlay:
    """One leg's consumer behavior, driven inline: `start` runs before
    the leg's tick window, `poll` once per measurement round inside it,
    `finish` after the leg's probes. The scenario stays single-threaded
    and deterministic, and its own measurement requests are never the
    interference under test."""

    def __init__(self, kind, base, watch, flood=None):
        self.kind = kind
        self.base = base
        self.statuses = []    # every HTTP status the consumer read back
        self.errors = []      # transport failures the consumer met
        self.probes = 0       # raw socket probes that became no request
        self.held = None      # the stalled reader's held (status, body)
        self.malformed = {}   # 'METHOD path' -> status
        self._held_socket = None
        self._index = 0
        self.surfaces = ['/snapshot', '/receipts', '/journal?since=0',
                         '/history?point=' + str(watch) + '&since=0',
                         '/checkpoint', '/role', '/signals', '/']
        # The command-flood leg's admission record: flood carries the
        # served bound and the probe point; submissions logs every
        # (status, normalized outcome, receipt-log index) the flood met.
        self.flood = flood
        self.submissions = []
        self._receipts_base = None

    def _send(self, method, path, body=None):
        try:
            status, _ = _request_status(method, self.base + path, body)
        except Exception as exc:
            self.errors.append(str(exc)[:200])
            return None
        self.statuses.append(status)
        return status

    def _next_surface(self):
        path = self.surfaces[self._index % len(self.surfaces)]
        self._index += 1
        return path

    def _raw_probes(self):
        # Garbage bytes and a half-sent request — traffic that never
        # becomes a request at all.
        for payload, how in (
                (b'\x89not-an-http-request\x90\r\n\r\n', socket.SHUT_WR),
                (b'GET /snapshot HTT', socket.SHUT_RDWR)):
            try:
                stream = _connect(self.base)
            except OSError as exc:
                self.errors.append(str(exc)[:200])
                continue
            try:
                stream.sendall(payload)
                stream.shutdown(how)
                stream.settimeout(0.25)
                try:
                    stream.recv(4096)
                except OSError:
                    pass
            except OSError as exc:
                self.errors.append(str(exc)[:200])
            finally:
                stream.close()
            self.probes += 1

    def _churn(self):
        # Connect, issue a read, take some or none of the response,
        # drop — the disconnect mid-session.
        path = self._next_surface()
        try:
            stream = _connect(self.base)
        except OSError as exc:
            self.errors.append(str(exc)[:200])
            return
        try:
            stream.sendall(('GET ' + path + ' HTTP/1.1\r\nHost: x\r\n'
                            'Connection: close\r\n\r\n').encode())
            stream.settimeout(0.25)
            try:
                head = stream.recv(512)
            except OSError:
                head = b''
            if head.startswith(b'HTTP'):
                try:
                    self.statuses.append(int(head.split(None, 2)[1]))
                except (ValueError, IndexError):
                    pass
        except OSError as exc:
            self.errors.append(str(exc)[:200])
        finally:
            stream.close()

    @property
    def queue_fulls(self):
        """The flood submissions the named queue_full rejection met."""
        return sum(1 for submission in self.submissions
                   if submission['outcome'] == 'rejected:queue_full')

    def _flood_command(self):
        return {'command': {'write_value': {
            'point': self.flood['point'], 'kind': 'bool',
            'value': {'bool': True}}},
            'actor': 'qa-lane'}

    def _record_submission(self, status, receipt):
        """One flood submission's verdict: its HTTP status, its
        receipt's normalized outcome, and the receipt-log index the
        append-only log assigns it — every POST /command appends exactly
        one receipt, in submission order."""
        if status is not None:
            self.statuses.append(status)
        self.submissions.append({
            'status': status,
            'outcome': _outcome_key(receipt) if isinstance(receipt, dict)
            else 'none',
            'index': self._receipts_base + len(self.submissions)
            if self._receipts_base is not None else None})

    def _flood_burst(self, count):
        for status, receipt in _pipelined_commands(
                self.base, [self._flood_command()] * count):
            self._record_submission(status, receipt)

    def start(self):
        if self.kind == 'stalled-reader':
            # Issue the request, then go silent without reading a byte
            # of the response until the leg ends — the held-connection
            # case the publication split exists for.
            try:
                stream = _connect(self.base)
                stream.sendall(b'GET /snapshot HTTP/1.1\r\nHost: x\r\n'
                               b'Connection: close\r\n\r\n')
                self._held_socket = stream
            except OSError as exc:
                self.errors.append(str(exc)[:200])
        elif self.kind == 'malformed-and-flood':
            for method, path, body, _expected in MALFORMED_PROBES:
                status = self._send(method, path, body)
                if status is not None:
                    self.malformed[method + ' ' + path] = status
        elif self.kind == 'command-flood':
            # The bounded admission flood: pipelined bursts of twice the
            # served queue bound until the named queue_full rejection
            # appears — the whole batch lands inside one server read
            # buffer, faster than a scan boundary drains pending entries.
            try:
                _, body = http_json('GET', self.base + '/receipts')
                self._receipts_base = len(_receipt_list(body))
            except Exception as exc:
                self.errors.append('receipts base: ' + str(exc)[:150])
            rounds = 0
            while rounds < ADMISSION_ROUNDS and not self.queue_fulls:
                self._flood_burst(2 * self.flood['capacity'])
                rounds += 1

    def poll(self):
        if self.kind == 'polling':
            self._send('GET', self._next_surface())
        elif self.kind == 'disconnect-reconnect':
            self._churn()
        elif self.kind == 'malformed-and-flood':
            self._send('GET', self._next_surface())
            if self._index % 4 == 1:
                self._raw_probes()
        elif self.kind == 'command-flood':
            # A small pipelined trickle each measurement round — the
            # admission path stays loaded through the leg's scan window.
            self._flood_burst(ADMISSION_TRICKLE)

    def finish(self):
        """Drains held resources and returns the leg's named evidence
        failures — interference that never happened, or a consumer that
        met a server fault."""
        failures = []
        if self._held_socket is not None:
            stream, self._held_socket = self._held_socket, None
            try:
                stream.settimeout(5)
                chunks = []
                while True:
                    try:
                        chunk = stream.recv(65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    chunks.append(chunk)
                text = b''.join(chunks).decode(errors='replace')
                try:
                    status = int(text.split(None, 2)[1]) \
                        if text.startswith('HTTP') else 0
                except (ValueError, IndexError):
                    status = 0
                self.held = (status, text[:2000])
            finally:
                stream.close()
        if self.kind == 'stalled-reader':
            if self.held is None:
                failures.append('the stalled reader never held a '
                                'response')
            elif self.held[0] != 200:
                failures.append('the held response answered '
                                + str(self.held[0]))
            elif '"tick"' not in self.held[1]:
                failures.append('the held response was not a complete '
                                'snapshot')
        if self.kind in ('polling', 'disconnect-reconnect') \
                and not self.statuses:
            failures.append('the ' + self.kind + ' consumers never ran')
        if self.kind == 'malformed-and-flood':
            if not self.probes:
                failures.append('no raw probes reached the socket')
            expected = {method + ' ' + path: want
                        for method, path, _body, want
                        in MALFORMED_PROBES}
            wrong = {key: [self.malformed.get(key), want]
                     for key, want in expected.items()
                     if self.malformed.get(key) != want}
            if wrong:
                failures.append('malformed probes answered outside the '
                                'declared limits: '
                                + json.dumps(wrong, sort_keys=True)[:600])
        if self.kind == 'command-flood':
            if not self.submissions:
                failures.append('the command flood never ran')
            no_receipt = sum(1 for submission in self.submissions
                             if submission['status'] is None)
            if no_receipt:
                failures.append(str(no_receipt) + ' flood submissions '
                                'returned no receipt')
            http_errors = sorted({submission['status']
                                  for submission in self.submissions
                                  if submission['status'] is not None
                                  and submission['status'] != 200})
            if http_errors:
                failures.append('flood submissions met HTTP-layer '
                                'errors: ' + str(http_errors))
            outside = sorted({submission['outcome']
                              for submission in self.submissions
                              if submission['status'] == 200
                              and submission['outcome'] not in
                              ('accepted', 'applied',
                               'rejected:queue_full')})
            if outside:
                failures.append('flood receipts answered outside the '
                                'admission vocabulary: ' + str(outside))
            if self.submissions and not self.queue_fulls:
                failures.append('the named queue_full rejection never '
                                'appeared under '
                                + str(len(self.submissions))
                                + ' submissions against the served '
                                'capacity ' + str(self.flood['capacity']))
            elif not any(submission['outcome'] == 'accepted'
                         for submission in self.submissions):
                failures.append('no flood submission was admitted')
            if self._receipts_base is None:
                failures.append('the receipt log was unreadable at '
                                'flood start — settlement cannot be '
                                'audited')
            elif not (no_receipt or http_errors or outside):
                # Every admitted command's receipt must settle applied
                # at its scan boundary — the receipt log is append-only
                # and each submission's index is known.
                pending = [submission['index']
                           for submission in self.submissions
                           if submission['outcome'] == 'accepted']

                def drained():
                    try:
                        _, body = http_json('GET',
                                            self.base + '/receipts')
                    except Exception:
                        return None
                    receipts = _receipt_list(body)
                    for index in pending:
                        if len(receipts) <= index \
                                or _outcome_key(receipts[index]) \
                                != 'applied':
                            return None
                    return True

                if pending and not wait_for(
                        drained, time.monotonic() + LEG_DEADLINE,
                        interval=LEG_POLL):
                    failures.append('admitted flood commands never '
                                    'settled applied at a scan '
                                    'boundary')
        if any(status >= 500 for status in self.statuses):
            failures.append('a consumer saw a server fault: '
                            + str(sorted(set(self.statuses))))
        if self.errors:
            failures.append('consumer transport errors: '
                            + '; '.join(self.errors[:3]))
        return failures


def _consumer_leg(ctx, base, targets, overlay, want):
    """One measured leg: LEG_TICKS completed scans under the overlay's
    consumer behavior, then the leg's two receipted probe commands.

    Returns (signature, failures) — signature is None when the leg's
    scan outputs stopped advancing; that failure is named in failures.
    """
    failures = []
    start = _snapshot(ctx, base)
    tick0 = start.get('tick') or 0
    published0 = _publication(start).get('published') or 0
    h0 = _history_cursor(ctx, base, targets['watch'])
    deadline = time.monotonic() + LEG_DEADLINE
    try:
        overlay.start()
    except Exception as exc:
        overlay.errors.append('start: ' + str(exc)[:150])
    end = None
    while time.monotonic() < deadline:
        try:
            overlay.poll()
        except Exception as exc:
            overlay.errors.append('poll: ' + str(exc)[:150])
        try:
            snap = _snapshot(ctx, base)
        except Exception:
            snap = None
        if snap and (snap.get('tick') or 0) >= tick0 + LEG_TICKS:
            end = snap
            break
        time.sleep(LEG_POLL)
    if end is None:
        failures.append('scan outputs stopped advancing under '
                        + overlay.kind + ' at tick ' + str(tick0))
        failures += overlay.finish()
        return None, failures

    # The leg's receipted probes: one writable write (the leg's
    # alternating value), one statically invalid write — the two
    # command-path outcomes every leg must reproduce identically.
    _, receipts_body = http_json('GET', base + '/receipts')
    index = len(_receipt_list(receipts_body))
    http_json('POST', base + '/command',
              {'command': {'write_value': {
                  'point': targets['write'], 'kind': 'bool',
                  'value': {'bool': want}}},
               'actor': 'qa-lane'})
    _, rejected = http_json('POST', base + '/command',
                            {'command': {'write_value': {
                                'point': targets['reject'],
                                'kind': 'bool',
                                'value': {'bool': True}}},
                             'actor': 'qa-lane'})
    settled = wait_for(lambda: _settled_outcome(ctx, base, index),
                       time.monotonic() + LEG_DEADLINE, interval=LEG_POLL)
    applied = wait_for(
        lambda: _point_value(_try_snapshot(ctx, base) or {},
                             targets['write']) == want or None,
        time.monotonic() + LEG_DEADLINE, interval=LEG_POLL)
    _, history_body = http_json(
        'GET', base + '/history?point=' + str(targets['watch'])
        + '&since=' + str(h0))
    seqs = _history_seqs(history_body, targets['watch'])
    failures += overlay.finish()

    tick1 = end.get('tick') or 0
    published1 = _publication(end).get('published') or 0
    if not seqs or any(not isinstance(seq, int) or seq <= h0
                       for seq in seqs):
        honesty = 'stale'
    elif seqs != list(range(seqs[0], seqs[0] + len(seqs))):
        honesty = 'dishonest'
    else:
        honesty = 'gapped' if seqs[0] > h0 + 1 else 'contiguous'
    signature = {
        'scan': tick1 > tick0 and tick1 - tick0 == published1 - published0,
        'history': honesty,
        'write': settled or 'never-settled',
        'reject': _outcome_key(rejected),
        'applied': bool(applied),
    }
    return signature, failures


def scenario_consumer_schedule(ctx):
    """Slow, disconnected, malformed, and restarted consumers cannot
    reach the control loop — the WW-FND-004 / decision-83 schedule on
    the simulated rig."""
    case = Case('consumer-schedule',
                'Consumer-failure schedule leaves the control loop '
                'untouched',
                'polling, a stalled reader, disconnect/reconnect, and '
                'malformed-within-limits traffic leave scan outputs and '
                'command receipts identical to the no-consumer legs, '
                'and a lagging seq-cursor read gets the named '
                'gap/coalesced answer')
    try:
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30, interval=LEG_POLL)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        base = ctx[active]
        case.observe('consumer schedule against ' + active
                     + ' (' + base + ')')
        _, signals = http_json('GET', base + '/signals')
        targets = _signal_targets(signals)
        if targets is None:
            return case.finish('inconclusive',
                               'no writable bool command point or '
                               'non-writable point in the model')
        ref = save_evidence(ctx['evidence_dir'],
                            'consumer-schedule-signals.json', signals)
        case.evidence('file', ref, 'signal index naming the probe points')
        case.observe('probe points: write ' + str(targets['write'])
                     + ' reject ' + str(targets['reject']))
        journal_cursor = _journal_cursor(ctx, base)

        legs = []
        signatures = {}
        for index, kind in enumerate(
                ('reference', 'polling', 'stalled-reader',
                 'disconnect-reconnect', 'malformed-and-flood',
                 'reference')):
            name = 'reference-' + ('a' if not signatures else 'b') \
                if kind == 'reference' else kind
            overlay = _Overlay(kind, base, targets['watch'])
            try:
                signature, failures = _consumer_leg(
                    ctx, base, targets, overlay, index % 2 == 0)
            except Exception as exc:
                # An interference leg that lost the monitor mid-run is
                # the consumer reaching the plant — a named failure; a
                # reference leg that cannot read the rig at all is
                # inconclusive like the other scenarios.
                if kind == 'reference':
                    raise
                signature, failures = None, ['leg errored: '
                                             + str(exc)[:200]]
            legs.append({'leg': name, 'signature': signature,
                         'statuses': overlay.statuses[:40],
                         'errors': overlay.errors[:5],
                         'probes': overlay.probes,
                         'held': (overlay.held or [None])[0]})
            ref = save_evidence(ctx['evidence_dir'],
                                'consumer-schedule-legs.json', legs)
            if len(legs) == 1:
                case.evidence('file', ref)
            if failures:
                return case.finish('failed',
                                   name + ': ' + '; '.join(failures))
            case.observe('leg ' + name + ': '
                         + json.dumps(signature, sort_keys=True))
            signatures[name] = signature
            if kind == 'reference':
                continue
            if signature != signatures['reference-a']:
                return case.finish(
                    'failed',
                    name + ' diverged from the no-consumer legs: '
                    + json.dumps(signature, sort_keys=True) + ' vs '
                    + json.dumps(signatures['reference-a'],
                                 sort_keys=True))
        reference = signatures['reference-a']
        for key, healthy in (('scan', True), ('applied', True),
                             ('write', 'applied'),
                             ('reject', 'rejected:not_writable')):
            if reference[key] != healthy:
                return case.finish(
                    'failed', 'the no-consumer reference leg is '
                    'unhealthy at ' + key + ': '
                    + json.dumps(reference, sort_keys=True))
        # An honest gap is a fine answer; silently stale or dishonest
        # history in even the no-consumer legs is not.
        if reference['history'] not in ('contiguous', 'gapped'):
            return case.finish(
                'failed', 'the no-consumer reference leg is unhealthy '
                'at history: ' + json.dumps(reference, sort_keys=True))
        if signatures['reference-b'] != reference:
            return case.finish('failed',
                               'the post-schedule reference leg '
                               'diverged from the first')

        # A consumer holding a cursor behind the retained publication
        # window gets the named gap, not silent staleness: the served
        # snapshot's publication section accounts the evicted stretch
        # (`coalesced`) and answers the latest state.
        before = _snapshot(ctx, base)
        cursor = _publication(before).get('published') or 0
        lagged = None
        deadline = time.monotonic() + LEG_DEADLINE
        while time.monotonic() < deadline and lagged is None:
            snap = _snapshot(ctx, base)
            health = _publication(snap)
            if (health.get('published') or 0) \
                    - (health.get('depth') or 0) > cursor:
                lagged = (snap, health)
            else:
                time.sleep(LEG_POLL)
        if lagged is None:
            return case.finish('failed',
                               'the retained publication window never '
                               'rolled past the held cursor '
                               + str(cursor))
        snap, health = lagged
        through = (health.get('published') or 0) \
            - (health.get('depth') or 0)
        coalesced = health.get('coalesced') or 0
        ref = save_evidence(ctx['evidence_dir'],
                            'consumer-schedule-gap.json',
                            {'cursor': cursor,
                             'before': _publication(before),
                             'after': health, 'tick': snap.get('tick')})
        case.evidence('file', ref, 'lagged publication-cursor read')
        if coalesced < through:
            return case.finish(
                'failed', 'the lost stretch through publication '
                + str(through) + ' went unaccounted: coalesced '
                + str(coalesced))
        if (snap.get('tick') or 0) <= (before.get('tick') or 0):
            return case.finish('failed',
                               'the lagged read served a stale '
                               'snapshot')
        case.observe('lagged publication cursor ' + str(cursor)
                     + ': gap through ' + str(through) + ', coalesced '
                     + str(coalesced) + ', serving tick '
                     + str(snap.get('tick')))

        # The literal seq-cursor read: journaled submissions roll the
        # bounded journal window past the cursor recorded at the
        # scenario's start, then `?since=` it — the answer must open on
        # the retained tail (the numbering gap naming the evicted
        # stretch), never fabricate the lost entries.
        rolled = 0
        rounds = 0
        while rounds < FLOOD_ROUNDS and not rolled:
            for _ in range(FLOOD_BATCH):
                status, receipt = http_json(
                    'POST', base + '/command',
                    {'command': {'write_value': {
                        'point': targets['reject'], 'kind': 'bool',
                        'value': {'bool': True}}},
                     'actor': 'qa-lane'})
                if status != 200 or 'rejected' not in \
                        ((receipt or {}).get('outcome') or {}):
                    return case.finish(
                        'failed', 'a flood probe was not refused by '
                        'name: ' + json.dumps(receipt)[:300])
            rounds += 1
            first = _journal_first(ctx, base)
            if first > journal_cursor + 1:
                rolled = first
        if not rolled:
            return case.finish(
                'inconclusive', 'the journal window never rolled past '
                'cursor ' + str(journal_cursor) + ' under '
                + str(rounds * FLOOD_BATCH) + ' journaled submissions')
        _, page_body = http_json('GET', base + '/journal?since='
                                 + str(journal_cursor))
        page = _journal_seqs(page_body)
        ref = save_evidence(ctx['evidence_dir'],
                            'consumer-schedule-journal-gap.json',
                            {'cursor': journal_cursor,
                             'retained_from': rolled,
                             'page_head': page[:5],
                             'page_len': len(page)})
        case.evidence('file', ref, 'seq-cursor read since the lagged '
                      'journal cursor')
        if not page or page[0] <= journal_cursor + 1 \
                or page[0] < rolled:
            return case.finish(
                'failed', 'the lagging seq-cursor read returned '
                'silently stale data: cursor ' + str(journal_cursor)
                + ' answered from seq '
                + str(page[0] if page else None)
                + ' while retention starts at ' + str(rolled))
        if page != list(range(page[0], page[0] + len(page))):
            return case.finish('failed',
                               'the retained tail is not the '
                               'contiguous coalesced answer')
        case.observe('journal cursor ' + str(journal_cursor)
                     + ' lags retention from seq ' + str(rolled)
                     + ': the read opens at ' + str(page[0])
                     + ' — the named gap')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))


# --------------------------------------------------------------------
# The field-fault schedule (WW-OPS-003's signal-confidence clause and
# WW-FND-002's remote-I/O degradation path): the lane drives the same
# per-point failure surface the hardware lane will grade for channel
# faults — InjectFault/ClearFault over the plant's newline-JSON
# protocol on the run's published plant port. A quality fault must
# present the point degraded — the substituted quality stamped on the
# stored field value, never a silently healthy last-known — and
# clearing it must restore the field value at Good; an error fault must
# surface through the driver's IoError path into io_health (the
# per-direction counters, last_error with tick and direction) while the
# scan continues and no role change follows — field faults are not
# peer loss.

FAULT_DEADLINE = 30   # bound on one injection surfacing or a clear
FAULT_PROBE = 1.0     # plant-step window between stability probes
PLANT_MAX_MESSAGE = 64 * 1024  # the protocol's documented line bound


def _quality_key(quality):
    """A served quality's comparable form: 'good', or
    'bad:<reason>'/'uncertain:<reason>' for a degraded stamp."""
    if quality == 'good':
        return 'good'
    if isinstance(quality, dict) and quality:
        name = next(iter(quality))
        return str(name) + ':' + str(quality[name])
    return 'unknown'


def _plant_connect(ctx, timeout=5):
    """A TCP connection to the run's published plant-protocol endpoint —
    ctx['plant'] carries the published 'host:port'."""
    host, _, port = str(ctx['plant']).rpartition(':')
    return socket.create_connection(
        (host or '127.0.0.1', int(port or 0)), timeout=timeout)


def _plant_request(stream, request):
    """One plant-protocol round trip: write the request object plus the
    newline delimiter, read back exactly one response line, enforcing
    the protocol's message bound."""
    stream.sendall(json.dumps(request).encode() + b'\n')
    line = b''
    while not line.endswith(b'\n'):
        chunk = stream.recv(PLANT_MAX_MESSAGE)
        if not chunk:
            raise ConnectionError('the plant server closed the '
                                  'connection mid-request')
        line += chunk
        if len(line) > PLANT_MAX_MESSAGE:
            raise ConnectionError('a plant response exceeded the '
                                  'protocol message bound')
    return json.loads(line)


def _plant_ctl(ctx, *args):
    """One `dcs-plant-ctl` invocation through the run's `plant_ctl`
    seam — the shipped plant-side tool, exec'd inside the plant
    container against its loopback listener, so the lane drives the
    binary the image carries rather than a second Python
    implementation of the wire protocol. The answer is the same
    response object the raw wire produced (`{"result":"done"}`,
    `{"result":"sample","sample":{...}}`, ...); a refused request or a
    failed invocation comes back error-shaped, `detail` carrying the
    tool's stderr."""
    run = ctx.get('plant_ctl')
    if run is None:
        raise ConnectionError('the run context carries no plant_ctl '
                              'seam for the shipped plant tool')
    result = run(*args)
    if result.returncode == 0:
        try:
            answer = json.loads(result.stdout)
        except ValueError:
            raise ConnectionError('dcs-plant-ctl printed an '
                                  'unparseable answer: '
                                  + str(result.stdout)[:300])
        if not isinstance(answer, dict):
            raise ConnectionError('dcs-plant-ctl printed a non-object '
                                  'answer: ' + str(result.stdout)[:300])
        return answer
    return {'result': 'error',
            'error': {'kind': 'tool_failed',
                      'detail': (result.stderr or '').strip()[:400],
                      'exit': result.returncode}}


def _try_plant_ctl(ctx, *args):
    """`_plant_ctl` or None — a refused invocation is one lost poll,
    not the leg's verdict (the `_try_plant` contract applied to the
    shipped tool's subcommands)."""
    try:
        answer = _plant_ctl(ctx, *args)
    except Exception:
        return None
    return answer if answer.get('result') != 'error' else None


def _plant_read(ctx, point):
    """The stored field sample for `point` — the shipped tool's
    `read`, answered as a `sample` result."""
    response = _plant_ctl(ctx, 'read', str(point))
    if response.get('result') != 'sample':
        raise ConnectionError('plant read on point ' + str(point)
                              + ' answered '
                              + json.dumps(response)[:300])
    return response.get('sample') or {}


def _field_inputs(ctx):
    """{point: PointInfo entry} for every 'in'-direction point the
    plant serves — the shipped tool's `list`, the field side's own
    account of what the scenario may fault."""
    response = _plant_ctl(ctx, 'list')
    if response.get('result') != 'points':
        raise ConnectionError('plant list answered '
                              + json.dumps(response)[:300])
    return {entry.get('point'): entry
            for entry in response.get('points') or []
            if entry.get('direction') == 'in'}


def scenario_field_fault(ctx):
    """Injected field-point faults degrade through the monitor and
    clear — never masquerading as healthy, never costing the active
    its role."""
    case = Case('field-fault',
                'Injected field faults degrade, recover, and keep role',
                'a quality fault injected on a field In point serves '
                'the point with the substituted quality stamped — '
                'never a silently Good value — clearing it restores '
                'the simulated field value at Good quality, and a '
                'disconnected-class fault on a second field point '
                'surfaces on io_health (failed_reads, last_error with '
                'tick and direction) while the scan continues and no '
                'role change follows')
    injected = []
    try:
        # Self-contained on either role layout, like evidence-capture:
        # whichever peer reports settled active is the observed surface.
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        base = ctx[active]
        if ctx.get('plant') is None:
            return case.finish('inconclusive',
                               'the run publishes no plant endpoint')
        case.observe('plant tool at ' + str(ctx['plant'])
                     + '; observing ' + active + ' (' + base + ')')

        # Fault targets must be points the field itself holds still:
        # only a stable stored value can prove the clear restored the
        # field value rather than a moved one. Two list_points probes
        # straddling a few plant steps find them, and each must already
        # read Good on the monitor — a forced or degraded point cannot
        # evidence a fault it would mask. The field ops ride the
        # shipped dcs-plant-ctl: read, list, and the fault commands need
        # no writer claim, so they run beside the field owner's
        # standing claim exactly as the raw requests did.
        first = _field_inputs(ctx)
        time.sleep(FAULT_PROBE)
        second = _field_inputs(ctx)
        snap = _snapshot(ctx, base)
        served_good = {
            entry.get('point') for entry in snap.get('points', [])
            if _quality_key((entry.get('sample') or {}).get('quality'))
            == 'good'}
        stable = sorted(
            point for point, info in first.items()
            if point in second and point in served_good
            and _quality_key((info.get('sample') or {}).get('quality'))
            == 'good'
            and _quality_key((second[point].get('sample') or {})
                             .get('quality')) == 'good'
            and (info.get('sample') or {}).get('value')
            == (second[point].get('sample') or {}).get('value'))
        ref = save_evidence(ctx['evidence_dir'],
                            'field-fault-points.json',
                            {'served': sorted(second), 'stable': stable})
        case.evidence('file', ref, 'the list_points census and the '
                      'stability probe')
        if not stable:
            return case.finish('inconclusive',
                               'no stable healthy field In point to '
                               'fault')
        quality_point = stable[0]
        alternates = [point for point in stable if point != quality_point]
        if not alternates:
            alternates = sorted(point for point in second
                                if point != quality_point)
        if not alternates:
            return case.finish('inconclusive',
                               'no second field In point to fault')
        error_point = alternates[0]
        case.observe('fault targets: quality point '
                     + str(quality_point) + ', error point '
                     + str(error_point))
        last = {}

        # Leg 1: a quality fault substitutes the served quality, leaving
        # the stored field value — the bad-data-confidence clause's
        # "degraded, never silently healthy" half.
        field_value = _plant_read(ctx, quality_point).get('value')
        verdict = _plant_ctl(ctx, 'fault', str(quality_point),
                             'bad:device_fault')
        if verdict.get('result') != 'done':
            return case.finish('failed', 'inject_fault refused: '
                               + json.dumps(verdict)[:300])
        injected.append(quality_point)

        def degraded():
            snap = _try_snapshot(ctx, base)
            if snap is None:
                return None
            last['snap'] = snap
            sample = _point_sample(snap, quality_point)
            if sample and _quality_key(sample.get('quality')) \
                    == 'bad:device_fault':
                return sample
            return None

        hit = wait_for(degraded, time.monotonic() + FAULT_DEADLINE)
        ref = save_evidence(
            ctx['evidence_dir'], 'field-fault-degraded.json',
            {'point': quality_point,
             'sample': hit or _point_sample(last.get('snap') or {},
                                          quality_point)})
        case.evidence('file', ref, 'the faulted point under injection')
        if not hit:
            return case.finish(
                'failed', 'the quality fault on point '
                + str(quality_point) + ' never surfaced: the served '
                'sample stayed '
                + json.dumps(_point_sample(last.get('snap') or {},
                                           quality_point))[:300])
        if hit.get('value') != field_value:
            return case.finish(
                'failed', 'the degraded sample replaced the field '
                'value ' + json.dumps(field_value) + ' with '
                + json.dumps(hit.get('value')))
        case.observe('point ' + str(quality_point)
                     + ' serves bad:device_fault over the stored field '
                     'value ' + json.dumps(field_value))
        if _settled_active(ctx) != active:
            return case.finish('failed', 'a quality fault moved the '
                               'active role — a field fault is not '
                               'peer loss')

        verdict = _plant_ctl(ctx, 'clear-fault', str(quality_point))
        if verdict.get('result') != 'done':
            return case.finish('failed', 'clear_fault refused: '
                               + json.dumps(verdict)[:300])
        injected.remove(quality_point)

        def recovered():
            snap = _try_snapshot(ctx, base)
            if snap is None:
                return None
            last['snap'] = snap
            sample = _point_sample(snap, quality_point)
            if not sample or _quality_key(sample.get('quality')) \
                    != 'good':
                return None
            try:
                field = _plant_read(ctx, quality_point)
            except Exception:
                return None
            if sample.get('value') == field.get('value'):
                return sample
            return None

        hit = wait_for(recovered, time.monotonic() + FAULT_DEADLINE)
        ref = save_evidence(
            ctx['evidence_dir'], 'field-fault-recovered.json',
            {'point': quality_point,
             'sample': hit or _point_sample(last.get('snap') or {},
                                          quality_point)})
        case.evidence('file', ref, 'the point after clearing')
        if not hit:
            return case.finish(
                'failed', 'clearing the fault on point '
                + str(quality_point) + ' never restored the field '
                'value at Good quality; last served '
                + json.dumps(_point_sample(last.get('snap') or {},
                                           quality_point))[:300])
        case.observe('point ' + str(quality_point)
                     + ' recovered to the field value at Good')

        # Leg 2: an error fault answers the driver's read with an
        # IoError — the remote-I/O degradation path. It must surface on
        # io_health attributed to the point and the in direction, the
        # served sample must degrade rather than pose as healthy
        # last-known, the link must stay up (a point fault is not link
        # loss), the scan must not stall, and the role must not move.
        def healthy_second():
            snap = _try_snapshot(ctx, base)
            if snap is None:
                return None
            last['snap'] = snap
            sample = _point_sample(snap, error_point)
            if sample and _quality_key(sample.get('quality')) \
                    == 'good':
                return snap
            return None

        # The second leg's baseline: the target must read healthy ahead
        # of its injection, so the counters it moves are attributable.
        before = wait_for(healthy_second,
                          time.monotonic() + FAULT_DEADLINE)
        if not before:
            return case.finish(
                'inconclusive', 'the error-fault target point '
                + str(error_point) + ' never read healthy ahead of '
                'injection')
        health0 = before.get('io_health') or {}
        tick0 = before.get('tick') or 0
        verdict = _plant_ctl(ctx, 'fault', str(error_point),
                             'disconnected')
        if verdict.get('result') != 'done':
            return case.finish('failed', 'inject_fault refused: '
                               + json.dumps(verdict)[:300])
        injected.append(error_point)

        def surfaced():
            snap = _try_snapshot(ctx, base)
            if snap is None:
                return None
            last['snap'] = snap
            health = snap.get('io_health') or {}
            fault = health.get('last_error') or {}
            if (health.get('failed_reads') or 0) \
                    > (health0.get('failed_reads') or 0) \
                    and fault.get('point') == error_point \
                    and fault.get('direction') == 'in' \
                    and fault.get('error') \
                    == {'disconnected': error_point}:
                return snap
            return None

        snap = wait_for(surfaced, time.monotonic() + FAULT_DEADLINE)
        ref = save_evidence(
            ctx['evidence_dir'], 'field-fault-io-health.json',
            {'point': error_point,
             'io_health': (last.get('snap') or {}).get('io_health'),
             'sample': _point_sample(last.get('snap') or {},
                                     error_point)})
        case.evidence('file', ref, 'io_health under the error fault')
        if not snap:
            return case.finish(
                'failed', 'the error fault on point '
                + str(error_point) + ' never surfaced on io_health: '
                + json.dumps((last.get('snap') or {})
                             .get('io_health'))[:400])
        health = snap.get('io_health') or {}
        fault = health.get('last_error') or {}
        if (fault.get('tick') or 0) < tick0:
            return case.finish('failed', 'last_error predates the '
                               'injection: ' + json.dumps(fault)[:300])
        if (health.get('failed_writes') or 0) \
                != (health0.get('failed_writes') or 0):
            return case.finish('failed', 'an in-point read fault '
                               'moved the out-direction counter: '
                               + json.dumps(health)[:400])
        link = (health.get('driver') or {}).get('link')
        if link != 'connected':
            return case.finish('failed', 'a point fault presented as '
                               'link loss: ' + str(link))
        sample = _point_sample(snap, error_point)
        if _quality_key((sample or {}).get('quality')) \
                != 'bad:communication_fault':
            return case.finish(
                'failed', 'the error-faulted point did not serve '
                'degraded: ' + json.dumps(sample)[:300])
        if (snap.get('tick') or 0) <= tick0:
            return case.finish('failed', 'the scan did not advance '
                               'past the injection')
        case.observe('io_health attributes point ' + str(error_point)
                     + ': failed_reads '
                     + str(health0.get('failed_reads') or 0) + ' -> '
                     + str(health.get('failed_reads'))
                     + ', last_error ' + json.dumps(fault)
                     + ', link ' + str(link))
        grown = wait_for(
            lambda: (s.get('tick', 0) > snap.get('tick', 0) and s
                     or None)
            if (s := _try_snapshot(ctx, base)) else None,
            time.monotonic() + FAULT_DEADLINE)
        if not grown:
            return case.finish('failed', 'the scan stalled while the '
                               'error fault stood')
        roles = {}
        for name in ('active', 'standby'):
            try:
                roles[name] = _role(ctx, ctx[name])
            except Exception as exc:
                roles[name] = {'unreachable': str(exc)[:200]}
        ref = save_evidence(ctx['evidence_dir'],
                            'field-fault-roles.json', roles)
        case.evidence('file', ref, 'roles while the error fault stands')
        if _settled_active(ctx) != active:
            return case.finish('failed', 'an error fault moved the '
                               'active role — a field fault is not '
                               'peer loss: ' + json.dumps(roles)[:300])
        case.observe('scan advancing (tick ' + str(snap.get('tick'))
                     + ' -> ' + str(grown.get('tick'))
                     + '), ' + active + ' still active under the '
                     'error fault')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
    finally:
        # The injected points are the run's shared field: a case that
        # leaves them faulted poisons every later scenario.
        for point in injected:
            try:
                _plant_ctl(ctx, 'clear-fault', str(point))
            except Exception:
                pass


# --------------------------------------------------------------------
# The backup-instrument-health annunciation (WW-OPS-003's redundant-
# measurement clause — the station alarm set's latent-degradation
# leg): issue #505's wiring ships in the deployed fixture, so a
# backup-side fault is producible through the plant protocol's
# inject_fault on the backup field point — the fault applies at the
# read seam, independent of which dynamics element writes the value.
# With the deployed pair settled and tracking, the injected non-Good
# quality must surface through the failover-select's backup_unhealthy
# output and the wired managed bool-latching alarm's standing and
# unacknowledged flags — each transition journaled on its
# declared-journaled point — while backup_active stays clear, the
# selection keeps serving the primary, and no role change follows. The
# receipted ack settles applied and journals attributed; clearing the
# fault lands the journaled return transitions; the rig is restored
# for later cases. The complementary primary-faulted leg stays with
# #463 — this leg faults only the backup.

BACKUP_HEALTH_DEADLINE = 30   # bound on each surfacing/settlement wait
BACKUP_HEALTH_ACTOR = 'qa-lane'


def _tracking_peer(ctx, active):
    """The pair endpoint other than `active` reporting role=standby
    with tracking convergence — the deployed pair's standby half — or
    None while unsettled, unreachable, or unconverged."""
    for name in ('active', 'standby'):
        if name == active or ctx.get(name) is None:
            continue
        try:
            report = _role(ctx, ctx[name])
        except Exception:
            continue
        sync = report.get('sync')
        if report.get('role') == 'standby' and isinstance(sync, dict) \
                and 'tracking' in sync:
            return name
    return None


def _journal_point_changes(journal):
    """{point: [to-value, ...]} the point_changed entries a served
    `GET /journal` payload carries — the durable transition record the
    declared-journaled points land."""
    changes = {}
    for entry in _journal_list(journal):
        change = (entry.get('event') or {}).get('point_changed')
        if isinstance(change, dict):
            changes.setdefault(change.get('point'), []) \
                .append(change.get('to'))
    return changes


def scenario_backup_health(ctx):
    """A backup-only field fault annunciates through the wired alarm
    set — journaled, acknowledged through the receipted path, and
    cleared — while the failover selection and the pair's roles never
    move."""
    case = Case('backup-health',
                'Backup-instrument degradation annunciates without '
                'failover',
                'with the deployed pair settled and tracking, an '
                'injected non-Good quality on the level-backup field '
                'input asserts the failover-select\'s backup_unhealthy '
                'output and stands the wired managed alarm '
                'unacknowledged — each transition journaled on its '
                'declared-journaled point — while backup_active stays '
                'clear, the selection keeps serving the primary, and '
                'no role change follows; the receipted ack settles '
                'applied and journals attributed to the lane actor, '
                'and clearing the fault lands the journaled return '
                'transitions')
    injected = None      # the backup field point, while faulted
    restore_ack = None   # (base, point) while the ack write stands
    try:
        # The leg needs the deployed pair settled and tracking — the
        # pre-switch window where a converged standby could take over,
        # so the unused backup leg's loss is exactly what must
        # annunciate before it is needed.
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        base = ctx[active]
        tracking = wait_for(lambda: _tracking_peer(ctx, active),
                            time.monotonic() + BACKUP_HEALTH_DEADLINE,
                            interval=POLL_INTERVAL)
        if tracking is None:
            return case.finish('inconclusive',
                               'no tracking peer — the deployed pair '
                               'never settled')
        case.observe('settled pair: ' + active + ' active, '
                     + tracking + ' tracking')

        _, signals = http_json('GET', base + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'backup-health-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming the '
                      'annunciation path')
        names = {'level-primary': 'primary',
                 'level-backup': 'backup',
                 'level-selected': 'selected',
                 'backup-active': 'backup_active',
                 'backup-unhealthy': 'unhealthy',
                 'backup-unhealthy-in': 'unhealthy_in',
                 'backup-unhealthy-ack': 'ack',
                 'backup-unhealthy-alarm': 'alarm',
                 'backup-unhealthy-unacknowledged': 'unack'}
        entries = {entry.get('name'): entry
                   for entry in signals.get('points', [])
                   if entry.get('name') in names}
        missing = sorted(set(names) - set(entries))
        if missing:
            return case.finish('inconclusive', 'the deployed model '
                               'lacks the backup-health annunciation '
                               'wiring — no signals '
                               + ', '.join(missing))
        ack_entry = entries['backup-unhealthy-ack']
        if not ack_entry.get('writable') \
                or ack_entry.get('direction') != 'in' \
                or ack_entry.get('value_type') != 'bool':
            return case.finish('inconclusive', 'the '
                               'backup-unhealthy-ack point is not the '
                               'alarm\'s writable bool ack input: '
                               + json.dumps(ack_entry)[:300])
        points = {names[name]: entry.get('point')
                  for name, entry in entries.items()}
        case.observe('annunciation path: '
                     + json.dumps({name: entry.get('point')
                                   for name, entry in
                                   sorted(entries.items())},
                                  sort_keys=True))

        if ctx.get('plant') is None:
            return case.finish('inconclusive',
                               'the run publishes no plant endpoint')
        # The field census and the fault commands ride the shipped
        # dcs-plant-ctl: neither takes the writer claim, so they run
        # beside the field owner's standing claim exactly as the raw
        # requests did.
        field = _field_inputs(ctx)
        if points['backup'] not in field:
            return case.finish('inconclusive', 'the level-backup '
                               'signal\'s point ' + str(points['backup'])
                               + ' is not a field in-point the plant '
                               'serves')
        case.observe('plant tool answering; backup field point '
                     + str(points['backup']))

        last = {}

        def healthy():
            snap = _try_snapshot(ctx, base)
            if snap is None:
                return None
            last['snap'] = snap
            if _quality_key((_point_sample(snap, points['backup'])
                             or {}).get('quality')) != 'good' \
                    or _quality_key((_point_sample(snap,
                                                 points['primary'])
                                     or {}).get('quality')) != 'good':
                return None
            for key in ('unhealthy', 'backup_active', 'alarm', 'unack'):
                if _point_value(snap, points[key]) is not False:
                    return None
            return snap

        baseline = wait_for(healthy,
                            time.monotonic() + BACKUP_HEALTH_DEADLINE,
                            interval=POLL_INTERVAL)
        ref = save_evidence(ctx['evidence_dir'],
                            'backup-health-baseline.json',
                            baseline or last.get('snap') or {})
        case.evidence('file', ref, 'the settled healthy baseline')
        if baseline is None:
            return case.finish('inconclusive', 'the annunciation path '
                               'never read settled-healthy ahead of '
                               'the injection')

        # The journal cursor ahead of the injection: transitions and
        # settlements from earlier legs already sit in the retained
        # tail — this leg's records are the ones above the floor.
        _, journal0 = http_json('GET', base + '/journal?since=0')
        floor = max((entry.get('seq') or 0
                     for entry in _journal_list(journal0)
                     if isinstance(entry, dict)), default=0)

        verdict = _plant_ctl(ctx, 'fault', str(points['backup']),
                             'bad:device_fault')
        if verdict.get('result') != 'done':
            return case.finish('failed', 'inject_fault on the backup '
                               'point refused: '
                               + json.dumps(verdict)[:300])
        injected = points['backup']
        case.observe('bad:device_fault injected on backup field point '
                     + str(injected) + ' — the fault applies at the '
                     'read seam, independent of the dynamics element '
                     'writing the value')

        def asserted():
            snap = _try_snapshot(ctx, base)
            if snap is None:
                return None
            last['snap'] = snap
            if _quality_key((_point_sample(snap, points['backup'])
                             or {}).get('quality')) == 'good':
                return None
            if _quality_key((_point_sample(snap, points['primary'])
                             or {}).get('quality')) != 'good':
                return None
            if _point_value(snap, points['unhealthy']) is not True \
                    or _point_value(snap, points['alarm']) is not True \
                    or _point_value(snap, points['unack']) is not True:
                return None
            if _point_value(snap, points['backup_active']) is not False:
                return None
            if _point_value(snap, points['selected']) \
                    != _point_value(snap, points['primary']):
                return None
            return snap

        hit = wait_for(asserted, time.monotonic()
                       + BACKUP_HEALTH_DEADLINE, interval=POLL_INTERVAL)
        snap = last.get('snap') or {}
        ref = save_evidence(
            ctx['evidence_dir'], 'backup-health-asserted.json',
            {'tick': snap.get('tick'),
             'samples': {name: _point_sample(snap, entry.get('point'))
                         for name, entry in sorted(entries.items())}})
        case.evidence('file', ref, 'the served snapshot under the '
                      'backup fault')
        if not hit:
            unmet = []
            if _quality_key((_point_sample(snap, points['backup'])
                             or {}).get('quality')) == 'good':
                unmet.append('the backup point still serves Good')
            if _quality_key((_point_sample(snap, points['primary'])
                             or {}).get('quality')) != 'good':
                unmet.append('the primary degraded alongside the '
                             'injected backup')
            if _point_value(snap, points['unhealthy']) is not True:
                unmet.append('backup_unhealthy never asserted')
            if _point_value(snap, points['alarm']) is not True:
                unmet.append('the wired alarm never stood')
            if _point_value(snap, points['unack']) is not True:
                unmet.append('the alarm never latched unacknowledged')
            if _point_value(snap, points['backup_active']) is not False:
                unmet.append('backup_active asserted — the selection '
                             'moved to the backup')
            if _point_value(snap, points['selected']) \
                    != _point_value(snap, points['primary']):
                unmet.append('the selection left the primary')
            return case.finish('failed', 'the backup-only fault never '
                               'annunciated: ' + '; '.join(unmet))
        case.observe('backup point ' + str(points['backup']) + ' serves '
                     + _quality_key(
                         (_point_sample(hit, points['backup']) or {})
                         .get('quality'))
                     + '; backup_unhealthy asserted, the wired alarm '
                     'standing unacknowledged, backup_active clear, '
                     'the selection still on the primary')
        if _settled_active(ctx) != active:
            return case.finish('failed', 'a backup-side field fault '
                               'moved the active role — a field fault '
                               'is not peer loss')

        # The durable half: each assertion lands its point_changed on
        # the declared-journaled point — backup_unhealthy, alarm,
        # unacknowledged — while the selection's backup_active and the
        # pair's roles record nothing.
        found = {}
        violations = {}

        def journaled():
            try:
                _, journal = http_json('GET', base + '/journal?since='
                                       + str(floor))
            except Exception:
                return None
            last['journal'] = journal
            changes = _journal_point_changes(journal)
            for key in ('unhealthy', 'alarm', 'unack'):
                if {'bool': True} in changes.get(points[key], []):
                    found[key] = True
            if {'bool': True} in changes.get(points['backup_active'],
                                             []):
                violations['source-transition'] = \
                    'backup_active journaled a source transition'
            if changes.get(points['unhealthy_in']):
                violations['unjournaled-carrier'] = \
                    'the non-journaled carrier point ' \
                    + str(points['unhealthy_in']) \
                    + ' journaled a transition'
            if any('role_changed' in (entry.get('event') or {})
                   for entry in _journal_list(journal)):
                violations['role-change'] = 'a role_changed event ' \
                    'journaled under a backup-only fault'
            if len(found) == 3 or violations:
                return journal
            return None

        wait_for(journaled, time.monotonic() + BACKUP_HEALTH_DEADLINE,
                 interval=POLL_INTERVAL)
        ref = save_evidence(
            ctx['evidence_dir'], 'backup-health-journal.json',
            {'floor': floor, 'asserted': sorted(found),
             'violations': sorted(violations),
             'entries': last.get('journal') or []})
        case.evidence('file', ref, 'the journaled transitions above '
                      'the pre-injection floor')
        if violations:
            return case.finish('failed', '; '.join(
                violations[key] for key in sorted(violations)))
        missing = [key for key in ('unhealthy', 'alarm', 'unack')
                   if key not in found]
        if missing:
            return case.finish('failed', 'the served journal never '
                               'recorded point_changed to true on: '
                               + ', '.join(missing))
        case.observe('journaled: backup_unhealthy, alarm, and '
                     'unacknowledged transitions landed on the '
                     'declared-journaled points; no source transition, '
                     'no role change')

        # The acknowledgment leg: a receipted write on the alarm's
        # declared ack input clears the latch while the condition still
        # stands — the managed alarm's ack-dominates rule — and the
        # settlement journals attributed.
        write = {'point': points['ack'], 'kind': 'bool',
                 'value': {'bool': True}}
        status, receipt = http_json(
            'POST', base + '/command',
            {'command': {'write_value': write},
             'actor': BACKUP_HEALTH_ACTOR})
        ref = save_evidence(ctx['evidence_dir'],
                            'backup-health-ack-receipt.json',
                            {'status': status, 'body': receipt})
        case.evidence('file', ref, 'the ack submission receipt')
        outcome = (receipt or {}).get('outcome') or {}
        if status != 200 or 'rejected' in outcome:
            return case.finish('failed', 'the ack write was refused: '
                               + str(status) + ' '
                               + json.dumps(receipt)[:400])
        restore_ack = (base, points['ack'])

        def acked():
            snap = _try_snapshot(ctx, base)
            if snap is None:
                return None
            last['snap'] = snap
            if _point_value(snap, points['unack']) is not False \
                    or _point_value(snap, points['alarm']) is not True:
                return None
            return snap

        acknowledged = wait_for(acked, time.monotonic()
                                + BACKUP_HEALTH_DEADLINE,
                                interval=POLL_INTERVAL)
        snap = last.get('snap') or {}
        ref = save_evidence(
            ctx['evidence_dir'], 'backup-health-acknowledged.json',
            {'tick': snap.get('tick'),
             'samples': {name: _point_sample(snap, entry.get('point'))
                         for name, entry in
                         (('backup-unhealthy-alarm',
                           entries['backup-unhealthy-alarm']),
                          ('backup-unhealthy-unacknowledged',
                           entries['backup-unhealthy-unacknowledged']))}})
        case.evidence('file', ref, 'the snapshot after the settled '
                      'ack — the latch cleared while the condition '
                      'stands')
        if not acknowledged:
            return case.finish(
                'failed', 'the settled ack never cleared the '
                'unacknowledged latch while the alarm stood: last '
                'served unacknowledged='
                + json.dumps(_point_sample(snap, points['unack']))
                + ' alarm='
                + json.dumps(_point_sample(snap, points['alarm']))[:300])

        settled = {}

        def settled_journal():
            try:
                _, journal = http_json('GET', base + '/journal?since='
                                       + str(floor))
            except Exception:
                return None
            last['journal'] = journal
            for receipt_ in _settled_receipts(journal):
                if (receipt_.get('command') or {}).get('write_value') \
                        == write:
                    settled['receipt'] = receipt_
            if {'bool': False} in _journal_point_changes(journal) \
                    .get(points['unack'], []):
                settled['unack_cleared'] = True
            if 'receipt' in settled and 'unack_cleared' in settled:
                return journal
            return None

        wait_for(settled_journal,
                 time.monotonic() + BACKUP_HEALTH_DEADLINE,
                 interval=POLL_INTERVAL)
        ref = save_evidence(
            ctx['evidence_dir'], 'backup-health-ack-journal.json',
            {'receipt': settled.get('receipt'),
             'unack_cleared': settled.get('unack_cleared')})
        case.evidence('file', ref, 'the journaled ack settlement')
        settled_receipt = settled.get('receipt')
        if settled_receipt is None:
            return case.finish('failed', 'the ack\'s CommandSettled '
                               'never journaled')
        if 'applied' not in (settled_receipt.get('outcome') or {}):
            return case.finish('failed', 'the ack receipt did not '
                               'settle applied: '
                               + json.dumps(settled_receipt
                                            .get('outcome'))[:200])
        if settled_receipt.get('actor') != BACKUP_HEALTH_ACTOR:
            return case.finish('failed', 'the journaled ack receipt '
                               'is unattributed: actor='
                               + json.dumps(settled_receipt
                                            .get('actor')))
        if not settled.get('unack_cleared'):
            return case.finish('failed', 'the unacknowledged flag\'s '
                               'clearing never journaled')
        case.observe('ack settled applied, journaled attributed to '
                     + BACKUP_HEALTH_ACTOR + ', unacknowledged '
                     'cleared while the alarm stood')

        # The recovery leg: clearing the fault returns the backup
        # sample to Good and lands the return transitions — the
        # health output and the standing alarm dropping on their
        # declared-journaled points — with the selection unmoved.
        verdict = _plant_ctl(ctx, 'clear-fault',
                             str(points['backup']))
        if verdict.get('result') != 'done':
            return case.finish('failed', 'clear_fault on the backup '
                               'point refused: '
                               + json.dumps(verdict)[:300])
        injected = None

        def recovered():
            snap = _try_snapshot(ctx, base)
            if snap is None:
                return None
            last['snap'] = snap
            if _quality_key((_point_sample(snap, points['backup'])
                             or {}).get('quality')) != 'good':
                return None
            if _point_value(snap, points['unhealthy']) is not False \
                    or _point_value(snap, points['alarm']) is not False \
                    or _point_value(snap, points['unack']) is not False \
                    or _point_value(snap, points['backup_active']) \
                    is not False:
                return None
            if _point_value(snap, points['selected']) \
                    != _point_value(snap, points['primary']):
                return None
            return snap

        hit = wait_for(recovered, time.monotonic()
                       + BACKUP_HEALTH_DEADLINE, interval=POLL_INTERVAL)
        snap = last.get('snap') or {}
        ref = save_evidence(
            ctx['evidence_dir'], 'backup-health-recovered.json',
            {'tick': snap.get('tick'),
             'samples': {name: _point_sample(snap, entry.get('point'))
                         for name, entry in sorted(entries.items())}})
        case.evidence('file', ref, 'the snapshot after the clear')
        if not hit:
            return case.finish(
                'failed', 'the annunciation never returned after the '
                'clear: last served backup_unhealthy='
                + json.dumps(_point_sample(snap, points['unhealthy']))
                + ' alarm='
                + json.dumps(_point_sample(snap, points['alarm']))
                + ' backup='
                + json.dumps(_point_sample(snap, points['backup']))
                [:300])

        returned = {}

        def return_journaled():
            try:
                _, journal = http_json('GET', base + '/journal?since='
                                       + str(floor))
            except Exception:
                return None
            last['journal'] = journal
            changes = _journal_point_changes(journal)
            for key in ('unhealthy', 'alarm'):
                if {'bool': False} in changes.get(points[key], []):
                    returned[key] = True
            return len(returned) == 2 and journal

        wait_for(return_journaled,
                 time.monotonic() + BACKUP_HEALTH_DEADLINE,
                 interval=POLL_INTERVAL)
        ref = save_evidence(
            ctx['evidence_dir'],
            'backup-health-return-journal.json',
            {'floor': floor, 'returned': sorted(returned)})
        case.evidence('file', ref, 'the journaled return transitions')
        missing = [key for key in ('unhealthy', 'alarm')
                   if key not in returned]
        if missing:
            return case.finish('failed', 'the return transition never '
                               'journaled on: ' + ', '.join(missing))
        case.observe('recovery journaled: backup_unhealthy and the '
                     'standing alarm returned false on their '
                     'declared-journaled points')

        # Restore the rig for later cases: the operator ack point back
        # to its declared initial through the same receipted path —
        # a standing true would hold the latch clear for every later
        # leg — and the field fault is already cleared.
        restore = {'point': points['ack'], 'kind': 'bool',
                   'value': {'bool': False}}
        status, receipt = http_json(
            'POST', base + '/command',
            {'command': {'write_value': restore},
             'actor': BACKUP_HEALTH_ACTOR})
        ref = save_evidence(ctx['evidence_dir'],
                            'backup-health-restored.json',
                            {'status': status, 'body': receipt})
        case.evidence('file', ref, 'the ack-restore receipt')
        outcome = (receipt or {}).get('outcome') or {}
        if status != 200 or 'rejected' in outcome:
            return case.finish('failed', 'the ack restore write was '
                               'refused: ' + str(status) + ' '
                               + json.dumps(receipt)[:400])

        def restored():
            snap = _try_snapshot(ctx, base)
            if snap is None:
                return None
            last['snap'] = snap
            return _point_value(snap, points['ack']) is False and snap

        if not wait_for(restored,
                        time.monotonic() + BACKUP_HEALTH_DEADLINE,
                        interval=POLL_INTERVAL):
            return case.finish('failed', 'the ack point never '
                               'returned to false — the rig is left '
                               'with the ack standing')
        restore_ack = None
        if _settled_active(ctx) != active:
            return case.finish('failed', 'the active role moved '
                               'during the leg')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
    finally:
        # The injected point is the run's shared field and the ack
        # point the alarm's operator input: a case that leaves either
        # standing poisons every later scenario.
        if injected is not None:
            try:
                _plant_ctl(ctx, 'clear-fault', str(injected))
            except Exception:
                pass
        if restore_ack is not None:
            rbase, rpoint = restore_ack
            try:
                http_json('POST', rbase + '/command',
                          {'command': {'write_value': {
                              'point': rpoint, 'kind': 'bool',
                              'value': {'bool': False}}},
                           'actor': BACKUP_HEALTH_ACTOR})
            except Exception:
                pass


# --------------------------------------------------------------------
# The source-failover leg (WW-OPS-003's declared-failover clause and
# WW-CTL-002's alarmed backup-source transition): the complementary
# primary-faulted half the backup-health header leaves to this issue.
# Everything the leg drives is model-declared wiring discovered
# through the served registry — the failover-select instance names its
# bound primary/backup source points through GET /schema's
# measurements and GET /signals resolves their direction and names,
# the flag's managed alarm is the served instance whose `in` binds the
# flag's declared <name>-in carrier, and the threshold-chain
# instance's bound level/demand plus its served setpoints back the
# demand-evaluation check — never a hardcoded id. The injected
# bad:device_fault applies at the field read seam through the shipped
# dcs-plant-ctl exactly as the field-fault leg drives it: the
# selection must move to the backup — `out` tracking the backup's
# live value at Good, never the degraded source nor a frozen
# last-known — backup_active asserts, the wired alarm stands
# unacknowledged on its declared-journaled points, and the chain keeps
# evaluating the failover-fed level rather than stalling demand. The
# clear re-selects the primary, releases backup_active, returns the
# standing alarm, and leaves the unacknowledged latch standing until
# the receipted ack settles applied — the two-flag lifecycle unchanged
# by the excursion. No role change follows: a field fault is not peer
# loss.

SOURCE_FAILOVER_DEADLINE = 30  # bound on each surfacing/settlement wait
SOURCE_FAILOVER_ACTOR = 'qa-lane'
SOURCE_FAILOVER_OUT_SLACK = 4  # scans a fresh selected output may lag


def _interface_resources(entry):
    """The served interface entry's named resources across the
    measurements and state collections — {name: resource} — the
    bound-point wiring the schema serves per port."""
    resources = {}
    interface = entry.get('interface') or {}
    for collection in ('measurements', 'state'):
        for resource in interface.get(collection) or []:
            if isinstance(resource, dict) and resource.get('name'):
                resources[resource['name']] = resource
    return resources


def _advance_demand(held, level, table):
    """The threshold-chain's documented one-scan demand rule — the
    value the served demand must equal for the chain to be evaluating
    the failover-fed level rather than holding a stalled or fallback
    answer."""
    if level <= table['stop']:
        return 0
    if held == 2 and level <= table['start']:
        return 1
    if level >= table['lag_start']:
        return 2
    if held == 0 and level >= table['start']:
        return 1
    return held


def scenario_source_failover(ctx):
    """An injected Bad primary selects the healthy backup — served,
    alarmed, journaled, demand-live — and the clear re-selects the
    primary while the latch stands for the receipted ack."""
    case = Case('source-failover',
                'Primary-source failover selects the backup and '
                're-selects on recovery',
                'on the settled active, an injected bad:device_fault '
                'on the failover-select\'s primary field source — the '
                'instance and its bound primary/backup sources located '
                'through GET /schema and GET /signals — moves the '
                'selection to the backup: the selected level tracks '
                'the backup point\'s live value at Good quality rather '
                'than clamping or holding last-known, backup_active '
                'asserts, the wired managed alarm stands '
                'unacknowledged on its declared-journaled points, and '
                'the threshold chain keeps evaluating on the '
                'failover-fed level; clearing the fault re-selects '
                'the primary, releases backup_active, returns the '
                'standing alarm, and leaves the unacknowledged latch '
                'standing until the receipted ack settles applied — '
                'the two-flag lifecycle unchanged — and no role '
                'change follows')
    injected = None       # the primary field point, while faulted
    restore_ack = None    # (base, point) while the ack write stands
    clear_latch = None    # (base, point) while a standing latch is owed
    try:
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        base = ctx[active]
        if ctx.get('plant') is None:
            return case.finish('inconclusive',
                               'the run publishes no plant endpoint')
        case.observe('observing ' + active + ' (' + base
                     + '); plant tool at ' + str(ctx['plant']))

        _, schema = http_json('GET', base + '/schema')
        _, signals = http_json('GET', base + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'source-failover-schema.json',
                            {'schema': schema, 'signals': signals})
        case.evidence('file', ref, 'the served interface registry '
                      'and SignalIndex the wiring resolves through')

        select = None
        for entry in schema.get('interfaces') or []:
            if (entry.get('interface') or {}).get('kind') \
                    != 'failover-select':
                continue
            resources = _interface_resources(entry)
            if all((resources.get(name) or {}).get('point') is not None
                   for name in ('primary', 'backup', 'out',
                                'backup_active')):
                select = (entry, resources)
                break
        if select is None:
            return case.finish(
                'inconclusive', 'the rig model declares no '
                'failover-select instance wired with primary, backup, '
                'out, and backup_active')
        select_entry, resources = select
        points = {name: resources[name].get('point')
                  for name in ('primary', 'backup', 'out',
                               'backup_active')}
        if (resources.get('backup_unhealthy') or {}).get('point') \
                is not None:
            points['backup_unhealthy'] = \
                resources['backup_unhealthy'].get('point')
        select_name = select_entry.get('name')

        sigmap = {entry.get('point'): entry
                  for entry in signals.get('points', [])}
        for key in ('primary', 'backup'):
            entry = sigmap.get(points[key])
            if entry is None or entry.get('direction') != 'in' \
                    or entry.get('value_type') != 'float':
                return case.finish(
                    'inconclusive', 'the failover-select\'s ' + key
                    + ' source point ' + str(points[key])
                    + ' is not a served float field input: '
                    + json.dumps(entry)[:300])
        if points['primary'] == points['backup']:
            return case.finish('inconclusive', 'the failover-select '
                               'binds one source point twice — no '
                               'distinct pair to switch between')

        # The alarmed transition's managed alarm: the served instance
        # whose `in` binds the flag's declared <name>-in carrier — the
        # model's carrier convention, verified live below when the
        # carrier follows backup_active into the standing alarm.
        flag_name = (sigmap.get(points['backup_active']) or {}) \
            .get('name')
        carrier = None
        if flag_name:
            for entry in signals.get('points', []):
                if entry.get('name') == str(flag_name) + '-in' \
                        and entry.get('direction') == 'in':
                    carrier = entry.get('point')
                    break
        alarm_res = None
        if carrier is not None:
            for entry in schema.get('interfaces') or []:
                res = _interface_resources(entry)
                if (res.get('in') or {}).get('point') != carrier:
                    continue
                if all((res.get(name) or {}).get('point') is not None
                       for name in ('alarm', 'unacknowledged', 'ack')):
                    alarm_res = res
                    break
        if alarm_res is None:
            return case.finish('inconclusive', 'no managed alarm is '
                               'wired to the failover-select\'s '
                               'backup_active flag ' + str(flag_name))
        points['carrier'] = carrier
        for name in ('alarm', 'unacknowledged', 'ack'):
            points[name] = alarm_res[name].get('point')
        ack_entry = sigmap.get(points['ack']) or {}
        if not ack_entry.get('writable') \
                or ack_entry.get('direction') != 'in' \
                or ack_entry.get('value_type') != 'bool':
            return case.finish('inconclusive', 'the alarm\'s ack '
                               'point is not the declared writable '
                               'bool input: '
                               + json.dumps(ack_entry)[:300])

        # The failover-fed demand path: the threshold-chain instance's
        # bound level/demand points and whichever standing flags it
        # wires.
        chain = None
        for entry in schema.get('interfaces') or []:
            if (entry.get('interface') or {}).get('kind') \
                    != 'threshold-chain':
                continue
            res = _interface_resources(entry)
            if (res.get('level') or {}).get('point') is not None \
                    and (res.get('demand') or {}).get('point') \
                    is not None:
                chain = (entry, res)
                break
        if chain is None:
            return case.finish('inconclusive', 'the served registry '
                               'declares no threshold-chain evaluating '
                               'the failover-fed level')
        chain_entry, chain_res = chain
        chain_name = chain_entry.get('name')
        points['level'] = chain_res['level'].get('point')
        points['demand'] = chain_res['demand'].get('point')
        flags = {name: chain_res[name].get('point')
                 for name in ('duty_call', 'lag_call', 'below_cutoff',
                              'high_level')
                 if (chain_res.get(name) or {}).get('point') is not None}
        case.observe('failover wiring through ' + str(select_name)
                     + ': ' + json.dumps({k: points[k]
                                          for k in sorted(points)},
                                         sort_keys=True))

        # Two distinct healthy field source points, per the field's
        # own census — the pair the switch may choose between.
        field = _field_inputs(ctx)
        for key in ('primary', 'backup'):
            if points[key] not in field:
                return case.finish('inconclusive', 'the ' + key
                                   + ' source point '
                                   + str(points[key]) + ' is not a '
                                   'field in-point the plant serves')
        healthy_field = all(
            _quality_key((field[points[key]].get('sample') or {})
                         .get('quality')) == 'good'
            for key in ('primary', 'backup'))
        ref = save_evidence(ctx['evidence_dir'],
                            'source-failover-field.json',
                            {'points': {k: points[k]
                                        for k in sorted(points)},
                             'field': {str(points[k]): field[points[k]]
                                       for k in ('primary', 'backup')}})
        case.evidence('file', ref, 'the field census over the bound '
                      'source points')
        if not healthy_field:
            return case.finish('inconclusive', 'no two distinct '
                               'healthy field source points to '
                               'switch between')

        last = {}

        def settled():
            snap = _try_snapshot(ctx, base)
            if snap is None:
                return None
            last['snap'] = snap
            for key in ('primary', 'backup'):
                if _quality_key((_point_sample(snap, points[key])
                                 or {}).get('quality')) != 'good':
                    return None
            if _point_value(snap, points['out']) \
                    != _point_value(snap, points['primary']):
                return None
            if _point_value(snap, points['backup_active']) is not False \
                    or _point_value(snap, points['alarm']) is not False \
                    or _point_value(snap, points['unacknowledged']) \
                    is not False:
                return None
            if 'backup_unhealthy' in points \
                    and _point_value(snap, points['backup_unhealthy']) \
                    is not False:
                return None
            if _point_sample(snap, points['level']) is None \
                    or _point_sample(snap, points['demand']) is None:
                return None
            return snap

        baseline = wait_for(settled,
                            time.monotonic() + SOURCE_FAILOVER_DEADLINE,
                            interval=POLL_INTERVAL)
        snap = last.get('snap') or {}
        ref = save_evidence(ctx['evidence_dir'],
                            'source-failover-baseline.json',
                            {'tick': snap.get('tick'),
                             'samples': {key: _point_sample(
                                             snap, points[key])
                                         for key in sorted(points)}})
        case.evidence('file', ref, 'the settled healthy baseline the '
                      'excursion departs from')
        if not baseline:
            return case.finish('inconclusive', 'the failover path '
                               'never read settled-healthy ahead of '
                               'the injection')

        table = {key: _parameter_value(baseline, chain_name, key)
                 for key in ('cutoff', 'stop', 'start', 'lag_start',
                             'high', 'on_bad_demand')}
        if any(not isinstance(table[key], (int, float))
               or isinstance(table[key], bool)
               or not math.isfinite(table[key])
               for key in ('cutoff', 'stop', 'start', 'lag_start',
                           'high')) \
                or not isinstance(table['on_bad_demand'], int) \
                or isinstance(table['on_bad_demand'], bool):
            return case.finish('inconclusive', 'the threshold-chain '
                               'serves no usable setpoint table: '
                               + json.dumps(table)[:300])
        case.observe('the ' + str(chain_name) + ' setpoints: '
                     + json.dumps(table, sort_keys=True))

        # The journal floor ahead of the excursion: this leg's records
        # are the ones above it.
        _, journal0 = http_json('GET', base + '/journal?since=0')
        floor = max((entry.get('seq') or 0
                     for entry in _journal_list(journal0)), default=0)

        verdict = _plant_ctl(ctx, 'fault', str(points['primary']),
                             'bad:device_fault')
        ref = save_evidence(ctx['evidence_dir'],
                            'source-failover-inject.json',
                            {'point': points['primary'],
                             'fault': 'bad:device_fault',
                             'verdict': verdict})
        case.evidence('file', ref, 'the inject_fault verdict')
        if verdict.get('result') != 'done':
            return case.finish('failed', 'inject_fault on the primary '
                               'source refused: '
                               + json.dumps(verdict)[:300])
        injected = points['primary']
        case.observe('injected bad:device_fault on primary field '
                     'point ' + str(injected))

        def failed_over():
            snap = _try_snapshot(ctx, base)
            if snap is None:
                return None
            last['snap'] = snap
            if _quality_key((_point_sample(snap, points['primary'])
                             or {}).get('quality')) \
                    != 'bad:device_fault':
                return None
            backup = _point_sample(snap, points['backup'])
            if _quality_key((backup or {}).get('quality')) != 'good':
                return None
            selected = _point_sample(snap, points['out'])
            tick = (selected or {}).get('tick')
            if _quality_key((selected or {}).get('quality')) != 'good' \
                    or (selected or {}).get('value') \
                    != (backup or {}).get('value') \
                    or not isinstance(tick, int) \
                    or (snap.get('tick') or 0) - tick \
                    > SOURCE_FAILOVER_OUT_SLACK:
                return None
            if _point_value(snap, points['backup_active']) is not True \
                    or _point_value(snap, points['carrier']) is not True:
                return None
            if _point_value(snap, points['alarm']) is not True \
                    or _point_value(snap, points['unacknowledged']) \
                    is not True:
                return None
            if 'backup_unhealthy' in points \
                    and _point_value(snap, points['backup_unhealthy']) \
                    is not False:
                return None
            return snap

        hit = wait_for(failed_over, time.monotonic()
                       + SOURCE_FAILOVER_DEADLINE,
                       interval=POLL_INTERVAL)
        snap = last.get('snap') or {}
        ref = save_evidence(ctx['evidence_dir'],
                            'source-failover-failed-over.json',
                            {'tick': snap.get('tick'),
                             'samples': {key: _point_sample(
                                             snap, points[key])
                                         for key in sorted(points)}})
        case.evidence('file', ref, 'the served failover state — '
                      'backup selected at Good, alarm latched')
        if not hit:
            unmet = []
            if _quality_key((_point_sample(snap, points['primary'])
                             or {}).get('quality')) \
                    != 'bad:device_fault':
                unmet.append('the primary never served the injected '
                             'bad:device_fault')
            backup = _point_sample(snap, points['backup'])
            if _quality_key((backup or {}).get('quality')) != 'good':
                unmet.append('the backup degraded alongside the '
                             'injected primary')
            selected = _point_sample(snap, points['out'])
            if _quality_key((selected or {}).get('quality')) != 'good' \
                    or (selected or {}).get('value') \
                    != (backup or {}).get('value'):
                unmet.append('the selection kept tracking the '
                             'degraded source or a frozen last-known')
            if _point_value(snap, points['backup_active']) is not True:
                unmet.append('backup_active never asserted')
            if _point_value(snap, points['alarm']) is not True:
                unmet.append('the wired alarm never stood')
            if _point_value(snap, points['unacknowledged']) is not True:
                unmet.append('the alarm never latched unacknowledged')
            return case.finish('failed', 'the primary-source fault '
                               'never failed over: '
                               + '; '.join(unmet))
        case.observe('backup serving at '
                     + json.dumps(_point_value(hit, points['out']))
                     + ' Good; backup_active, alarm, and '
                     'unacknowledged standing')
        clear_latch = (base, points['ack'])
        if _settled_active(ctx) != active:
            return case.finish('failed', 'a primary-source field '
                               'fault moved the active role to '
                               + str(_settled_active(ctx)))

        # The failover-fed demand path keeps evaluating while the
        # backup serves: the chain's level input carries the selected
        # output's image at Good and the demand sample advances its
        # per-scan evaluation — never frozen, never the untrusted-input
        # fallback while the level reads healthy.
        demand = {'prev': None, 'first_tick': None, 'latest_tick': None,
                  'violations': {}, 'recent': []}

        def demand_continues():
            snap = _try_snapshot(ctx, base)
            if snap is None:
                return None
            last['demand_snap'] = snap
            level = _point_sample(snap, points['level'])
            demand_sample = _point_sample(snap, points['demand'])
            if level is None or demand_sample is None:
                return None
            lv = _point_value(snap, points['level'])
            dv = _point_value(snap, points['demand'])
            if _quality_key(level.get('quality')) != 'good':
                demand['violations']['level-quality'] = \
                    'the failover-fed level served ' \
                    + _quality_key(level.get('quality'))
            demand['recent'].append(
                _point_value(snap, points['out']))
            del demand['recent'][:-3]
            if len(demand['recent']) > 1 and lv not in demand['recent']:
                demand['violations']['level-source'] = \
                    'the chain\'s level input is not the failover ' \
                    'output\'s image: ' + json.dumps(lv)
            if isinstance(dv, int) and not isinstance(dv, bool) \
                    and isinstance(lv, (int, float)) \
                    and not isinstance(lv, bool):
                if demand['prev'] is not None:
                    want = _advance_demand(demand['prev'], lv, table)
                    if dv != want:
                        demand['violations']['diverged'] = \
                            'demand ' + json.dumps(dv) + ' is not ' \
                            'the failover-fed evaluation ' \
                            + json.dumps(want) + ' of level ' \
                            + json.dumps(lv)
                demand['prev'] = dv
                expected = {
                    'duty_call': dv >= 1,
                    'lag_call': dv >= 2,
                    'below_cutoff': lv <= table['cutoff'],
                    'high_level': lv >= table['high']}
                for name, want in expected.items():
                    if name in flags and _point_value(
                            snap, flags[name]) is not want:
                        demand['violations'][name] = \
                            name + ' served ' + json.dumps(
                                _point_value(snap, flags[name])) \
                            + ' for demand ' + json.dumps(dv) \
                            + ' on level ' + json.dumps(lv)
            tick = demand_sample.get('tick')
            if demand['first_tick'] is None:
                demand['first_tick'] = tick
            demand['latest_tick'] = tick
            return isinstance(tick, int) \
                and isinstance(demand['first_tick'], int) \
                and tick > demand['first_tick'] and snap

        wait_for(demand_continues,
                 time.monotonic() + SOURCE_FAILOVER_DEADLINE,
                 interval=POLL_INTERVAL)
        snap = last.get('demand_snap') or {}
        ref = save_evidence(ctx['evidence_dir'],
                            'source-failover-demand.json',
                            {'tick': snap.get('tick'),
                             'level': _point_sample(
                                 snap, points['level']),
                             'demand': _point_sample(
                                 snap, points['demand']),
                             'first_tick': demand['first_tick'],
                             'latest_tick': demand['latest_tick'],
                             'violations': sorted(
                                 demand['violations'])})
        case.evidence('file', ref, 'the failover-fed demand '
                      'evaluation while the backup served')
        if demand['first_tick'] is None:
            return case.finish('inconclusive', 'the failover-fed '
                               'demand path never reported on the '
                               'served snapshot')
        if demand['violations']:
            return case.finish('failed', 'demand stalled while the '
                               'backup served healthy: '
                               + '; '.join(demand['violations'][key]
                                           for key in sorted(
                                               demand['violations'])))
        if not isinstance(demand['latest_tick'], int) \
                or demand['latest_tick'] <= demand['first_tick']:
            return case.finish('failed', 'the demand evaluation froze '
                               'while the backup served healthy — '
                               'tick held ' + json.dumps(
                                   demand['latest_tick']))
        case.observe('demand kept evaluating the failover-fed level '
                     'through tick ' + str(demand['latest_tick']))

        found = {}
        violations = {}

        def journaled():
            try:
                _, journal = http_json(
                    'GET', base + '/journal?since=' + str(floor))
            except Exception:
                return None
            last['journal'] = journal
            changes = _journal_point_changes(journal)
            for key in ('backup_active', 'alarm', 'unacknowledged'):
                if {'bool': True} in changes.get(points[key], []):
                    found[key] = True
            if changes.get(points['carrier']):
                violations['unjournaled-carrier'] = \
                    'the alarm\'s unjournaled carrier point ' \
                    + str(points['carrier']) + ' journaled ' \
                    + json.dumps(changes[points['carrier']])
            if any('role_changed' in (entry.get('event') or {})
                   for entry in _journal_list(journal)):
                violations['role-change'] = \
                    'a primary-source field fault journaled a ' \
                    'role_changed'
            return (len(found) == 3 or violations) and journal

        wait_for(journaled, time.monotonic() + SOURCE_FAILOVER_DEADLINE,
                 interval=POLL_INTERVAL)
        journal = last.get('journal') or {}
        ref = save_evidence(
            ctx['evidence_dir'], 'source-failover-journal.json',
            {'floor': floor, 'found': sorted(found),
             'violations': sorted(violations),
             'entries': _journal_list(journal)})
        case.evidence('file', ref, 'the journaled failover and '
                      'alarmed transitions above the floor')
        if violations:
            return case.finish('failed', '; '.join(
                violations[key] for key in sorted(violations)))
        if len(found) != 3:
            return case.finish('failed', 'the failover\'s '
                               'transitions never journaled (found '
                               + json.dumps(sorted(found)) + ')')
        case.observe('the failover, standing alarm, and '
                     'unacknowledged latch each journaled on their '
                     'declared-journaled points; the carrier left '
                     'unjournaled')

        verdict = _plant_ctl(ctx, 'clear-fault', str(points['primary']))
        ref = save_evidence(ctx['evidence_dir'],
                            'source-failover-clear.json',
                            {'point': points['primary'],
                             'verdict': verdict})
        case.evidence('file', ref, 'the clear_fault verdict')
        if verdict.get('result') != 'done':
            return case.finish('failed', 'clear_fault on the primary '
                               'source refused: '
                               + json.dumps(verdict)[:300])
        injected = None
        case.observe('fault cleared on primary field point '
                     + str(points['primary']))

        def recovered():
            snap = _try_snapshot(ctx, base)
            if snap is None:
                return None
            last['snap'] = snap
            primary = _point_sample(snap, points['primary'])
            if _quality_key((primary or {}).get('quality')) != 'good':
                return None
            selected = _point_sample(snap, points['out'])
            if _quality_key((selected or {}).get('quality')) != 'good' \
                    or (selected or {}).get('value') \
                    != (primary or {}).get('value'):
                return None
            if _point_value(snap, points['backup_active']) is not False:
                return None
            if _point_value(snap, points['alarm']) is not False:
                return None
            if _point_value(snap, points['unacknowledged']) is not True:
                return None
            return snap

        hit = wait_for(recovered, time.monotonic()
                       + SOURCE_FAILOVER_DEADLINE,
                       interval=POLL_INTERVAL)
        snap = last.get('snap') or {}
        ref = save_evidence(ctx['evidence_dir'],
                            'source-failover-recovered.json',
                            {'tick': snap.get('tick'),
                             'samples': {key: _point_sample(
                                             snap, points[key])
                                         for key in sorted(points)}})
        case.evidence('file', ref, 'the post-clear recovery — primary '
                      're-selected, latch still standing')
        if not hit:
            unmet = []
            if _quality_key((_point_sample(snap, points['primary'])
                             or {}).get('quality')) != 'good':
                unmet.append('the primary never recovered Good')
            selected = _point_sample(snap, points['out'])
            if _quality_key((selected or {}).get('quality')) != 'good' \
                    or (selected or {}).get('value') \
                    != (_point_sample(snap, points['primary']) or {}) \
                    .get('value'):
                unmet.append('the selection never re-selected the '
                             'primary')
            if _point_value(snap, points['backup_active']) is not False:
                unmet.append('backup_active never released')
            if _point_value(snap, points['alarm']) is not False:
                unmet.append('the standing alarm never returned')
            if _point_value(snap, points['unacknowledged']) is not True:
                unmet.append('the unacknowledged latch released '
                             'before the declared ack — the two-flag '
                             'lifecycle changed by the excursion')
            return case.finish('failed', 'the failover never '
                               'recovered: ' + '; '.join(unmet))
        case.observe('primary re-selected at '
                     + json.dumps(_point_value(hit, points['out']))
                     + ' Good; backup_active released, alarm returned, '
                     'unacknowledged still latched')

        found = {}
        violations = {}

        def journaled_return():
            try:
                _, journal = http_json(
                    'GET', base + '/journal?since=' + str(floor))
            except Exception:
                return None
            last['journal'] = journal
            changes = _journal_point_changes(journal)
            for key in ('backup_active', 'alarm'):
                if {'bool': False} in changes.get(points[key], []):
                    found[key] = True
            if {'bool': False} in changes.get(points['unacknowledged'],
                                              []):
                violations['unacknowledged-release'] = \
                    'the unacknowledged latch journaled a release ' \
                    'the declared ack never wrote'
            if any('role_changed' in (entry.get('event') or {})
                   for entry in _journal_list(journal)):
                violations['role-change'] = \
                    'a primary-source field fault journaled a ' \
                    'role_changed'
            return (len(found) == 2 or violations) and journal

        wait_for(journaled_return,
                 time.monotonic() + SOURCE_FAILOVER_DEADLINE,
                 interval=POLL_INTERVAL)
        journal = last.get('journal') or {}
        ref = save_evidence(
            ctx['evidence_dir'],
            'source-failover-return-journal.json',
            {'floor': floor, 'found': sorted(found),
             'violations': sorted(violations),
             'entries': _journal_list(journal)})
        case.evidence('file', ref, 'the journaled return transitions '
                      'above the floor')
        if violations:
            return case.finish('failed', '; '.join(
                violations[key] for key in sorted(violations)))
        if len(found) != 2:
            return case.finish('failed', 'the recovery\'s return '
                               'transitions never journaled (found '
                               + json.dumps(sorted(found)) + ')')

        # The acknowledgment leg: a receipted write on the alarm's
        # declared ack input releases the standing latch — the
        # two-flag lifecycle's declared path — and the settlement
        # journals applied and attributed.
        write = {'point': points['ack'], 'kind': 'bool',
                 'value': {'bool': True}}
        status, receipt = http_json(
            'POST', base + '/command',
            {'command': {'write_value': write},
             'actor': SOURCE_FAILOVER_ACTOR})
        ref = save_evidence(ctx['evidence_dir'],
                            'source-failover-ack-receipt.json',
                            {'status': status, 'body': receipt})
        case.evidence('file', ref, 'the acknowledged ack '
                      'submission receipt')
        outcome = (receipt or {}).get('outcome') or {}
        if status != 200 or 'rejected' in outcome:
            return case.finish('failed', 'the alarm\'s declared ack '
                               'write was refused: ' + str(status)
                               + ' ' + json.dumps(receipt)[:400])
        restore_ack = (base, points['ack'])

        def acknowledged():
            snap = _try_snapshot(ctx, base)
            if snap is None:
                return None
            last['snap'] = snap
            if _point_value(snap, points['unacknowledged']) is not False:
                return None
            return snap

        hit = wait_for(acknowledged, time.monotonic()
                       + SOURCE_FAILOVER_DEADLINE,
                       interval=POLL_INTERVAL)
        snap = last.get('snap') or {}
        ref = save_evidence(ctx['evidence_dir'],
                            'source-failover-acknowledged.json',
                            {'tick': snap.get('tick'),
                             'alarm': _point_sample(snap,
                                                    points['alarm']),
                             'unacknowledged': _point_sample(
                                 snap, points['unacknowledged'])})
        case.evidence('file', ref, 'the post-ack latch state')
        if not hit:
            return case.finish('failed', 'the declared ack write '
                               'never cleared the standing '
                               'unacknowledged latch')
        case.observe('the receipted ack released the standing latch')

        settled = {}

        def journaled_ack():
            try:
                _, journal = http_json(
                    'GET', base + '/journal?since=' + str(floor))
            except Exception:
                return None
            last['journal'] = journal
            for receipt_ in _settled_receipts(journal):
                if (receipt_.get('command') or {}).get('write_value') \
                        == write:
                    settled['receipt'] = receipt_
            if {'bool': False} in _journal_point_changes(journal).get(
                    points['unacknowledged'], []):
                settled['unack_cleared'] = True
            if 'receipt' in settled and 'unack_cleared' in settled:
                return journal
            return None

        wait_for(journaled_ack,
                 time.monotonic() + SOURCE_FAILOVER_DEADLINE,
                 interval=POLL_INTERVAL)
        journal = last.get('journal') or {}
        ref = save_evidence(
            ctx['evidence_dir'], 'source-failover-ack-journal.json',
            {'floor': floor,
             'receipt': settled.get('receipt'),
             'unack_cleared': settled.get('unack_cleared'),
             'entries': _journal_list(journal)})
        case.evidence('file', ref, 'the journaled ack settlement and '
                      'latch release above the floor')
        settled_receipt = settled.get('receipt')
        if settled_receipt is None:
            return case.finish('failed', 'the ack\'s CommandSettled '
                               'never journaled')
        if 'applied' not in (settled_receipt.get('outcome') or {}):
            return case.finish('failed', 'the ack receipt did not '
                               'settle applied: '
                               + json.dumps(settled_receipt
                                            .get('outcome'))[:200])
        if settled_receipt.get('actor') != SOURCE_FAILOVER_ACTOR:
            return case.finish('failed', 'the journaled ack receipt '
                               'is unattributed: actor='
                               + json.dumps(settled_receipt
                                            .get('actor')))
        if not settled.get('unack_cleared'):
            return case.finish('failed', 'the unacknowledged flag\'s '
                               'release never journaled')
        case.observe('ack settled applied, journaled attributed to '
                     + SOURCE_FAILOVER_ACTOR + ', unacknowledged '
                     'released through the declared path')

        # Restore the rig for later cases: the operator ack point back
        # to its declared initial through the same receipted path —
        # a standing true would hold the latch clear for every later
        # leg — and the field fault is already cleared.
        restore = {'point': points['ack'], 'kind': 'bool',
                   'value': {'bool': False}}
        status, receipt = http_json(
            'POST', base + '/command',
            {'command': {'write_value': restore},
             'actor': SOURCE_FAILOVER_ACTOR})
        ref = save_evidence(ctx['evidence_dir'],
                            'source-failover-restored.json',
                            {'status': status, 'body': receipt})
        case.evidence('file', ref, 'the ack restore receipt')
        outcome = (receipt or {}).get('outcome') or {}
        if status != 200 or 'rejected' in outcome:
            return case.finish('failed', 'the ack restore write was '
                               'refused: ' + str(status) + ' '
                               + json.dumps(receipt)[:400])

        def restored():
            snap = _try_snapshot(ctx, base)
            if snap is None:
                return None
            last['snap'] = snap
            return _point_value(snap, points['ack']) is False and snap

        if not wait_for(restored,
                        time.monotonic() + SOURCE_FAILOVER_DEADLINE,
                        interval=POLL_INTERVAL):
            return case.finish('failed', 'the ack restore write '
                               'never landed on the served snapshot')
        restore_ack = None
        clear_latch = None
        if _settled_active(ctx) != active:
            return case.finish('failed', 'the excursion moved the '
                               'active role to '
                               + str(_settled_active(ctx)))
        return case.finish('passed', 'the injected primary fault '
                           'selected the healthy backup — tracked at '
                           'Good on the selected output, annunciated '
                           'through the wired alarm\'s two-flag '
                           'lifecycle, demand live — and the clear '
                           're-selected the primary while the latch '
                           'stood for the receipted ack')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
    finally:
        # The injected point is the run's shared field, the ack point
        # the alarm's operator input, and the standing latch the
        # lifecycle's record: a case that leaves any of them standing
        # poisons every later scenario, so the latch is cleared through
        # the same receipted ack path when the leg could not finish it.
        if injected is not None:
            try:
                _plant_ctl(ctx, 'clear-fault', str(injected))
            except Exception:
                pass
        if restore_ack is not None:
            rbase, rpoint = restore_ack
            try:
                http_json('POST', rbase + '/command',
                          {'command': {'write_value': {
                              'point': rpoint, 'kind': 'bool',
                              'value': {'bool': False}}},
                           'actor': SOURCE_FAILOVER_ACTOR})
            except Exception:
                pass
        elif clear_latch is not None:
            rbase, rpoint = clear_latch
            for value in (True, False):
                try:
                    http_json('POST', rbase + '/command',
                              {'command': {'write_value': {
                                  'point': rpoint, 'kind': 'bool',
                                  'value': {'bool': value}}},
                               'actor': SOURCE_FAILOVER_ACTOR})
                except Exception:
                    pass


# --------------------------------------------------------------------
# The all-level-sources-bad declared fallback (WW-OPS-003's
# bad-measurement leg and WW-CTL-002's failover-first contract —
# decision 42, the pumping-station note's recorded answer to the
# all-measurement-bad assumption): with every level source untrusted
# the failover-select has no good source, `out` serves the failed
# backup verbatim — Bad — and the threshold chain takes its declared
# `on_bad_demand` rather than controlling on the failed measurement.
# The legs drive the same shipped `dcs-plant-ctl` fault/clear-fault
# seam the field-fault and backup-health cases use, against the
# station's served wiring: a backup-only fault asserts the
# `backup_unhealthy` annunciation while the healthy primary keeps the
# field and no failover transition journals; faulting the primary too
# drives the declared fallback with the alarmed engagement journaled;
# and clearing in the reverse order resumes primary control and drops
# the annunciation through the named journaled transitions. The
# recovery half is where the requirement's "no unintended output step"
# clause lands: every served snapshot is checked against the declared
# selection and fallback rules, so a step to stale, substituted, or
# untrusted-data-driven output fails the leg it was observed in.

FALLBACK_DEADLINE = 30  # bound on one injection or clear surfacing


def _descriptor_ports(snapshot, kind, required):
    """(instance name, {port: bound point}) for the first served
    snapshot descriptor of `kind` whose ports all carry a bound point —
    the bound-point-annotated wiring the serving layer joins each port
    to — or (None, {}) when no instance serves the leg's contract."""
    for entry in snapshot.get('descriptors') or []:
        if entry.get('kind') != kind:
            continue
        ports = {port.get('name'): port.get('point')
                 for port in entry.get('ports') or []}
        if all(ports.get(name) is not None for name in required):
            return entry.get('name'), ports
    return None, {}


def _literal_bool(value):
    """A served `{'bool': v}` value literal — or a bare bool — unwrapped."""
    if isinstance(value, dict):
        return value.get('bool')
    return value


def _float_of(sample):
    """A served sample's unwrapped numeric value, or None."""
    value = (sample or {}).get('value')
    if isinstance(value, dict):
        value = next(iter(value.values()), None)
    return value


def _good_finite(sample):
    """Whether a served sample reads Good with a finite numeric value —
    the selector's own trustworthy-measurement predicate."""
    value = _float_of(sample)
    return _quality_key((sample or {}).get('quality')) == 'good' \
        and isinstance(value, (int, float)) and math.isfinite(value)


def _journal_transitions(body, point):
    """The (from, to) value transitions a /journal payload records on
    `point` — the point_changed entries of a declared-journaled
    carrier."""
    seen = []
    for entry in _journal_list(body):
        change = (entry.get('event') or {}).get('point_changed')
        if isinstance(change, dict) and change.get('point') == point:
            seen.append((change.get('from'), change.get('to')))
    return seen


def _fallback_violation(snap, points, on_bad):
    """The declared-rule violation one served snapshot shows, or None:
    the failover-select's `out` carries the selected source's sample —
    the primary while it reads Good and finite, else the backup
    verbatim, quality included — and the threshold chain emits its
    declared `on_bad_demand` whenever its delivered level input is
    untrusted. A snapshot missing a needed sample is one lost
    observation, never a verdict."""
    primary = _point_sample(snap, points['primary'])
    backup = _point_sample(snap, points['backup'])
    out = _point_sample(snap, points['out'])
    level_in = _point_sample(snap, points['level'])
    if not all(isinstance(sample, dict) and sample
               for sample in (primary, backup, out, level_in)):
        return None
    if _good_finite(primary):
        expected, source = primary, 'primary'
    else:
        expected, source = backup, 'backup'
    # The selector's non-finite merge: a selected value that is not
    # finite serves Bad(DeviceFault) rather than the source's stamp.
    expected_quality = _quality_key(expected.get('quality'))
    expected_value = _float_of(expected)
    if not (isinstance(expected_value, (int, float))
            and math.isfinite(expected_value)):
        expected_quality = 'bad:device_fault'
    if _float_of(out) != expected_value \
            or _quality_key(out.get('quality')) != expected_quality:
        return 'out serves ' + json.dumps(out, sort_keys=True) \
            + ' while the ' + source + ' is the selected source (' \
            + json.dumps(expected, sort_keys=True)[:200] + ')'
    demand = _point_value(snap, points['demand'])
    if not _good_finite(level_in) and demand is not None \
            and demand != on_bad:
        return 'demand ' + json.dumps(demand) \
            + ' controls on an untrusted level instead of the ' \
            'declared on_bad_demand ' + json.dumps(on_bad)
    return None


def _await_journal(ctx, base, floor, point, want_to):
    """The first point_changed entry on `point` landing `want_to` above
    the run's journal floor, polled until it lands or the deadline
    passes — the durable record of one named transition."""
    def found():
        try:
            _, body = http_json('GET',
                                base + '/journal?since=' + str(floor))
        except Exception:
            return None
        for entry in _journal_list(body):
            change = (entry.get('event') or {}).get('point_changed')
            if isinstance(change, dict) and change.get('point') == point \
                    and _literal_bool(change.get('to')) is want_to:
                return entry
        return None
    return wait_for(found, time.monotonic() + FALLBACK_DEADLINE,
                    interval=POLL_INTERVAL)


def scenario_unavailable_fallback(ctx):
    """Inject a backup-only then an all-source level fault: the
    backup_unhealthy annunciation asserts with no failover transition,
    the all-bad state serves Bad on the failover output and the
    declared on_bad_demand on the chain — transitions journaled — and
    clearing in reverse order resumes primary control."""
    case = Case('unavailable-fallback',
                'All level sources bad engages the declared fallback',
                'a non-Good fault on the backup level input alone '
                'asserts the failover-select\'s backup_unhealthy while '
                'the healthy primary keeps serving and no failover '
                'transition journals; faulting the primary too leaves '
                'no trustworthy source — the select\'s output serves '
                'Bad and the threshold chain emits its declared '
                'on_bad_demand with the alarmed engagement journaled; '
                'clearing the faults in reverse order resumes primary '
                'control and drops the annunciation through the named '
                'transitions with no unintended output step')
    injected = []
    violations = []
    last = {}
    try:
        # Self-contained on either role layout, like field-fault:
        # whichever peer reports settled active is the observed
        # surface, and every leg is reversible field-side state.
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        base = ctx[active]
        if ctx.get('plant') is None:
            return case.finish('inconclusive',
                               'the run publishes no plant endpoint')
        case.observe('observing ' + active + ' (' + base + ')')

        # The leg's contract out of the served wiring: the
        # failover-select's bound ports and the threshold-chain's
        # delivered level and demand plus its declared on_bad_demand —
        # the model is the contract, so no point is addressed by
        # convention. A model without the optional standby-health port
        # declares no annunciation for this leg to check.
        snap = _snapshot(ctx, base)
        sel_name, sel = _descriptor_ports(
            snap, 'failover-select',
            ('primary', 'backup', 'out', 'backup_active',
             'backup_unhealthy'))
        chain_name, chain = _descriptor_ports(
            snap, 'threshold-chain', ('level', 'demand'))
        on_bad = _parameter_value(snap, chain_name, 'on_bad_demand') \
            if chain_name else None
        # The field census and the fault commands ride the shipped
        # dcs-plant-ctl: neither takes the writer claim, so they run
        # beside the field owner's standing claim exactly as the raw
        # requests did.
        census = _field_inputs(ctx)
        ref = save_evidence(
            ctx['evidence_dir'], 'unavailable-fallback-layout.json',
            {'select': {'name': sel_name, 'ports': sel},
             'chain': {'name': chain_name, 'ports': chain,
                       'on_bad_demand': on_bad},
             'field_inputs': sorted(census)})
        case.evidence('file', ref, 'the served level-path wiring and '
                      'the declared on_bad_demand')
        if sel_name is None or chain_name is None or on_bad is None:
            return case.finish(
                'inconclusive', 'the rig model does not serve the '
                'fallback leg: select=' + str(sel_name) + ' chain='
                + str(chain_name) + ' on_bad_demand=' + str(on_bad))
        points = {'primary': sel['primary'], 'backup': sel['backup'],
                  'out': sel['out'], 'active': sel['backup_active'],
                  'unhealthy': sel['backup_unhealthy'],
                  'level': chain['level'], 'demand': chain['demand']}
        absent = [name for name in ('primary', 'backup')
                  if points[name] not in census]
        if absent:
            return case.finish(
                'inconclusive', 'the bound level sources are not '
                'plant-served field inputs: ' + ', '.join(absent))
        case.observe('level path: primary ' + str(points['primary'])
                     + ', backup ' + str(points['backup']) + ' -> '
                     + str(sel_name) + '.out ' + str(points['out'])
                     + ' -> ' + str(chain_name) + '.demand '
                     + str(points['demand']) + ', declared '
                     'on_bad_demand ' + json.dumps(on_bad))

        def poll():
            """One observation: the served snapshot in, the declared-
            rule check out — every leg polls through here so a step
            outside the selection/fallback rules is recorded against
            the leg that served it."""
            snap = _try_snapshot(ctx, base)
            if snap is None:
                return None
            last['snap'] = snap
            violation = _fallback_violation(snap, points, on_bad)
            if violation:
                violations.append({'tick': snap.get('tick'),
                                   'violation': violation})
            return snap

        # The settled healthy baseline: both sources Good and finite,
        # the primary serving, no annunciation standing — the state
        # the legs leave the rig in, and the only state a backup-only
        # leg can be attributed from.
        def healthy():
            snap = poll()
            if snap is None:
                return None
            if _good_finite(_point_sample(snap, points['primary'])) \
                    and _good_finite(_point_sample(snap,
                                                 points['backup'])) \
                    and _point_value(snap, points['active']) is False \
                    and _point_value(snap, points['unhealthy']) \
                    is False:
                return snap
            return None

        def leg_evidence(name, extra):
            """One leg's evidence record: the last observed samples on
            the leg's points plus the leg's own payload."""
            payload = {'leg': name,
                       'samples': {key: _point_sample(
                           last.get('snap') or {}, points[key])
                           for key in points}}
            payload.update(extra)
            ref = save_evidence(ctx['evidence_dir'],
                                'unavailable-fallback-' + name + '.json',
                                payload)
            case.evidence('file', ref, 'the ' + name + ' leg')

        def unintended():
            """The first recorded rule violation's failure record, or
            None — an output step the declared rules never called
            for."""
            if violations:
                return case.finish(
                    'failed', 'an unintended output step at tick '
                    + str(violations[0]['tick']) + ': '
                    + violations[0]['violation'])
            return None

        if wait_for(healthy, time.monotonic() + FALLBACK_DEADLINE,
                    interval=POLL_INTERVAL) is None:
            return case.finish('inconclusive', 'no healthy settled '
                               'baseline: both level sources Good with '
                               'the primary serving never presented')
        _, body = http_json('GET', base + '/journal?since=0')
        floor = max((entry.get('seq') or 0
                     for entry in _journal_list(body)), default=0)

        # Leg 1 — the backup alone goes untrusted: the standby-health
        # report asserts while the healthy primary keeps the field —
        # the loss of the unused leg annunciated before it is needed —
        # and no failover transition may journal.
        verdict = _plant_ctl(ctx, 'fault', str(points['backup']),
                             'bad:device_fault')
        if verdict.get('result') != 'done':
            return case.finish('failed', 'inject_fault on the backup '
                               'point refused: '
                               + json.dumps(verdict)[:300])
        injected.append(points['backup'])
        moved = []

        def annunciated():
            snap = poll()
            if snap is None:
                return None
            if _point_value(snap, points['active']) is True:
                moved.append(snap.get('tick'))
            if _point_value(snap, points['unhealthy']) is True:
                return snap
            return None

        hit = wait_for(annunciated,
                       time.monotonic() + FALLBACK_DEADLINE,
                       interval=POLL_INTERVAL)
        _, body = http_json('GET',
                            base + '/journal?since=' + str(floor))
        leg_transitions = _journal_transitions(body, points['active'])
        leg_evidence('backup-degraded',
                     {'selection_moves': moved,
                      'failover_transitions': leg_transitions})
        if hit is None:
            detail = 'a backup-only fault never asserted the ' \
                     'backup_unhealthy annunciation: last served ' \
                     + json.dumps(_point_sample(last.get('snap') or {},
                                                points['unhealthy']))[:300]
            if violations:
                detail = 'an unintended output step: ' \
                    + violations[0]['violation']
            return case.finish('failed', detail)
        if moved:
            return case.finish('failed', 'a backup-only fault moved '
                               'the selection — backup_active asserted '
                               'at tick ' + str(moved[0]))
        if leg_transitions:
            return case.finish('failed', 'a backup-only fault '
                               'journaled a failover transition: '
                               + json.dumps(leg_transitions)[:300])
        if _await_journal(ctx, base, floor, points['unhealthy'],
                          True) is None:
            return case.finish('failed', 'the backup_unhealthy '
                               'annunciation never journaled')
        case.observe('backup-only fault: backup_unhealthy asserted at '
                     'tick ' + str(hit.get('tick'))
                     + ', the primary kept serving, no failover '
                     'transition journaled')
        failed = unintended()
        if failed:
            return failed

        # Leg 2 — the primary joins it: no trustworthy source remains,
        # so `out` serves the failed backup verbatim — Bad — and the
        # chain emits the declared on_bad_demand instead of controlling
        # on the untrusted measurement; the engagement is the alarmed
        # transition the durable record carries.
        verdict = _plant_ctl(ctx, 'fault', str(points['primary']),
                             'bad:device_fault')
        if verdict.get('result') != 'done':
            return case.finish('failed', 'inject_fault on the primary '
                               'point refused: '
                               + json.dumps(verdict)[:300])
        injected.append(points['primary'])

        def all_bad():
            snap = poll()
            if snap is None:
                return None
            if _point_value(snap, points['active']) is True \
                    and not _good_finite(
                        _point_sample(snap, points['out'])) \
                    and not _good_finite(
                        _point_sample(snap, points['level'])) \
                    and _point_value(snap, points['demand']) == on_bad:
                return snap
            return None

        hit = wait_for(all_bad, time.monotonic() + FALLBACK_DEADLINE,
                       interval=POLL_INTERVAL)
        leg_evidence('all-bad', {'on_bad_demand': on_bad})
        if hit is None:
            if violations:
                return case.finish('failed', 'an unintended output '
                                   'step: ' + violations[0]['violation'])
            return case.finish('failed', 'the all-bad state never '
                               'engaged the declared fallback: last '
                               'served out '
                               + json.dumps(_point_sample(
                                   last.get('snap') or {},
                                   points['out']))[:200] + ', demand '
                               + json.dumps(_point_value(
                                   last.get('snap') or {},
                                   points['demand'])))
        out_quality = _quality_key(
            _point_sample(hit, points['out']).get('quality'))
        if not out_quality.startswith('bad:'):
            return case.finish('failed', 'the failover output did not '
                               'serve Bad while every source was '
                               'untrusted: ' + out_quality)
        if _await_journal(ctx, base, floor, points['active'],
                          True) is None:
            return case.finish('failed', 'the alarmed backup_active '
                               'engagement never journaled')
        case.observe('all sources bad: out serves ' + out_quality
                     + ', demand ' + json.dumps(on_bad)
                     + ' (the declared on_bad_demand), the engagement '
                     'journaled')
        failed = unintended()
        if failed:
            return failed

        # Leg 3 — reverse-order clearing: the primary recovers first.
        # The stateless selector re-selects it the scan its sample
        # turns Good — backup_active drops through the named
        # transition — while the still-bad backup keeps the
        # annunciation standing.
        verdict = _plant_ctl(ctx, 'clear-fault', str(points['primary']))
        if verdict.get('result') != 'done':
            return case.finish('failed', 'clear_fault on the primary '
                               'point refused: '
                               + json.dumps(verdict)[:300])
        injected.remove(points['primary'])

        def primary_back():
            snap = poll()
            if snap is None:
                return None
            if _point_value(snap, points['active']) is False \
                    and _good_finite(
                        _point_sample(snap, points['out'])):
                return snap
            return None

        hit = wait_for(primary_back,
                       time.monotonic() + FALLBACK_DEADLINE,
                       interval=POLL_INTERVAL)
        leg_evidence('primary-recovered', {})
        if hit is None:
            if violations:
                return case.finish('failed', 'an unintended output '
                                   'step: ' + violations[0]['violation'])
            return case.finish('failed', 'the recovered primary never '
                               'resumed control: last served out '
                               + json.dumps(_point_sample(
                                   last.get('snap') or {},
                                   points['out']))[:300])
        if _point_value(hit, points['unhealthy']) is not True:
            return case.finish('failed', 'the backup_unhealthy '
                               'annunciation dropped while the backup '
                               'stayed bad')
        if _await_journal(ctx, base, floor, points['active'],
                          False) is None:
            return case.finish('failed', 'the return to primary '
                               'control never journaled')
        case.observe('primary recovered: backup_active cleared at '
                     'tick ' + str(hit.get('tick'))
                     + ', the primary serves again with the '
                     'annunciation still standing')
        failed = unintended()
        if failed:
            return failed

        # Leg 4 — the backup's clear drops the annunciation through
        # the named transition, leaving the rig on the healthy
        # baseline the legs started from.
        verdict = _plant_ctl(ctx, 'clear-fault', str(points['backup']))
        if verdict.get('result') != 'done':
            return case.finish('failed', 'clear_fault on the backup '
                               'point refused: '
                               + json.dumps(verdict)[:300])
        injected.remove(points['backup'])

        def annunciation_clear():
            snap = poll()
            if snap is None:
                return None
            if _point_value(snap, points['unhealthy']) is False:
                return snap
            return None

        hit = wait_for(annunciation_clear,
                       time.monotonic() + FALLBACK_DEADLINE,
                       interval=POLL_INTERVAL)
        leg_evidence('cleared', {'violations': violations})
        if hit is None:
            return case.finish('failed', 'clearing the backup never '
                               'cleared the annunciation: last served '
                               + json.dumps(_point_sample(
                                   last.get('snap') or {},
                                   points['unhealthy']))[:300])
        if _await_journal(ctx, base, floor, points['unhealthy'],
                          False) is None:
            return case.finish('failed', 'the annunciation\'s clear '
                               'never journaled')
        failed = unintended()
        if failed:
            return failed
        case.observe('backup recovered: the annunciation cleared and '
                     'journaled; the pair is back on the healthy '
                     'baseline')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
    finally:
        # The injected level points are the run's shared field: a case
        # that leaves them faulted poisons every later scenario.
        for point in injected:
            try:
                _plant_ctl(ctx, 'clear-fault', str(point))
            except Exception:
                pass


# --------------------------------------------------------------------
# The threshold-chain lag-staging and high-level annunciation leg
# (WW-CTL-002/WW-OPS-002 — decision 42's ordered setpoint chain on the
# deployed station). The dynamics' declared forcing input is `inflow`:
# no element drives it, so the harness writes it through the plant
# protocol's shared-claim path — ctx['plant_owner'] carries the
# --owner-token each controller pinned, and the scenario attachment
# ensures the writer claim under the settled active's token, the
# designed test-harness claim the sim's fencing grants `claimed_shared`.
# With the pair settled the leg raises the well through the served
# chain — demand 0 -> 1 -> 2 with duty_call/lag_call answering and the
# group's staged inside start_delay_ticks of the demand it consumed —
# crosses high into the managed high-level alarm's two-flag lifecycle
# with journaled point_changed evidence, then drives the falling edge:
# the lag de-stages before the duty releases, the alarm returns on its
# declared hysteresis, and a receipted ack clears the latch before the
# restore write lands. Functional misses name staging-failed; ordering,
# delay-bound, and journal-contract violations name
# staging-nondeterministic.

LAG_STAGING_DEADLINE = 60  # bound on each leg's served transition —
                         # the level traverse spans a few dozen scans
                         # under the deployed dynamics' rates
LAG_STAGING_POLL = 0.05    # transition-watch cadence — under the scan
LAG_STAGING_ACTOR = 'qa-lane'


def _component_instance(schema, kind, alarm_point=None):
    """The served name of the `kind` component instance — when several
    instances share the kind, the one whose `alarm` resource binds
    `alarm_point` — or None. The `alarm` port is Status-role, so it
    lands in the interface's `state` collection, not `measurements`."""
    for entry in schema.get('interfaces') or []:
        interface = entry.get('interface') or {}
        if interface.get('kind') != kind:
            continue
        if alarm_point is None:
            return entry.get('name')
        resources = (interface.get('measurements') or []) \
            + (interface.get('state') or [])
        for resource in resources:
            if resource.get('name') == 'alarm' \
                    and resource.get('point') == alarm_point:
                return entry.get('name')
    return None


def _history_transitions(payload, point, since_tick):
    """[(tick, value)] — the value changes one point's /history samples
    carry at tick >= since_tick, in seq order; the first served sample
    anchors the pre-window value and never counts."""
    if not isinstance(payload, list):
        return []
    for entry in payload:
        if entry.get('point') != point:
            continue
        transitions = []
        previous = None
        anchored = False
        for sample in entry.get('samples') or []:
            body = sample.get('sample') or {}
            value = body.get('value')
            if isinstance(value, dict):
                value = next(iter(value.values()), None)
            if not anchored:
                previous, anchored = value, True
                continue
            if value != previous:
                tick = body.get('tick')
                if isinstance(tick, int) and tick >= since_tick:
                    transitions.append((tick, value))
                previous = value
        return transitions
    return []


def _nth_transition(transitions, value, n=1):
    """The tick of the n-th transition landing on `value`, or None."""
    seen = 0
    for tick, landed in transitions:
        if landed == value:
            seen += 1
            if seen == n:
                return tick
    return None


def scenario_lag_staging(ctx):
    """Drive the wet well through the deployed threshold chain — demand
    0 -> 1 -> 2 with the pump group staging behind start_delay_ticks,
    the high-level alarm's two-flag lifecycle journaled — then down the
    falling edge de-staging the lag before the duty releases."""
    case = Case('lag-staging',
                'Threshold-chain lag staging and high-level '
                'annunciation',
                'with the deployed pair settled and tracking, a plant-'
                'protocol inflow write under the active\'s shared '
                'writer claim drives the level through the served '
                'setpoint chain: demand moves 0 -> 1 -> 2 with '
                'duty_call and lag_call asserting at the start and '
                'lag_start crossings, the pump group\'s staged answers '
                'inside start_delay_ticks of the demand it consumed, '
                'the high crossing asserts high_level and stands the '
                'managed alarm unacknowledged with journaled '
                'point_changed evidence, the falling edge de-stages '
                'the lag before the duty releases, the receipted ack '
                'clears the latch, and the driven inputs restore with '
                'the pair\'s roles unchanged')
    stream = None
    restore_inflow = None  # (point, baseline) while the write stands
    restore_ack = None     # (base, point) while the ack write stands
    try:
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        base = ctx[active]
        tracking = wait_for(lambda: _tracking_peer(ctx, active),
                            time.monotonic() + LAG_STAGING_DEADLINE,
                            interval=POLL_INTERVAL)
        if tracking is None:
            return case.finish('inconclusive',
                               'no tracking peer — the deployed pair '
                               'never settled')
        case.observe('settled pair: ' + active + ' active, '
                     + tracking + ' tracking')

        _, signals = http_json('GET', base + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'lag-staging-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming the '
                      'threshold-chain staging path')
        names = {'inflow': 'inflow', 'level-selected': 'level',
                 'demand': 'demand', 'demand-in': 'demand_in',
                 'duty': 'duty', 'staged': 'staged',
                 'duty-call': 'duty_call', 'lag-call': 'lag_call',
                 'below-cutoff': 'below_cutoff',
                 'high-level': 'high_level', 'lah-ack': 'ack',
                 'lah-alarm': 'alarm',
                 'lah-unacknowledged': 'unack',
                 'lah-shelved': 'shelved',
                 'lah-suppressed': 'suppressed',
                 'lah-out-of-service': 'out_of_service',
                 'p101-mode': 'p1_mode', 'p101-oos': 'p1_oos',
                 'p102-mode': 'p2_mode', 'p102-oos': 'p2_oos'}
        entries = {entry.get('name'): entry
                   for entry in signals.get('points', [])
                   if entry.get('name') in names}
        missing = sorted(set(names) - set(entries))
        if missing:
            return case.finish('inconclusive', 'the deployed model '
                               'lacks the threshold-chain staging '
                               'wiring — no signals '
                               + ', '.join(missing))
        ack_entry = entries['lah-ack']
        if not ack_entry.get('writable') \
                or ack_entry.get('direction') != 'in' \
                or ack_entry.get('value_type') != 'bool':
            return case.finish('inconclusive', 'the lah-ack point is '
                               'not the alarm\'s writable bool ack '
                               'input: ' + json.dumps(ack_entry)[:300])
        points = {names[name]: entry.get('point')
                  for name, entry in entries.items()}
        case.observe('staging path: '
                     + json.dumps({name: entry.get('point')
                                   for name, entry in
                                   sorted(entries.items())},
                                  sort_keys=True))

        _, schema = http_json('GET', base + '/schema')
        snap0 = _snapshot(ctx, base)
        chain_name = _component_instance(schema, 'threshold-chain')
        group_name = _component_instance(schema, 'pump-group')
        alarm_name = _component_instance(
            schema, 'managed-latching-alarm', points['alarm'])
        if chain_name is None or group_name is None \
                or alarm_name is None:
            return case.finish('inconclusive', 'the served schema '
                               'lacks the threshold-chain, pump-group, '
                               'or the managed-latching-alarm instance '
                               'wired to the lah-alarm point')
        table = {key: _parameter_value(snap0, chain_name, key)
                 for key in ('cutoff', 'stop', 'start', 'lag_start',
                             'high')}
        start_delay = _parameter_value(snap0, group_name,
                                       'start_delay_ticks')
        high_limit = _parameter_value(snap0, alarm_name, 'high_limit')
        hysteresis = _parameter_value(snap0, alarm_name, 'hysteresis')
        ref = save_evidence(
            ctx['evidence_dir'], 'lag-staging-chain.json',
            {'points': {key: points[key] for key in sorted(points)},
             'components': {'chain': chain_name, 'group': group_name,
                            'alarm': alarm_name},
             'setpoints': table,
             'start_delay_ticks': start_delay,
             'high_limit': high_limit, 'hysteresis': hysteresis})
        case.evidence('file', ref, 'the served setpoint chain, the '
                      'staging delay, and the alarm limits')
        ordered = [table[key]
                   for key in ('cutoff', 'stop', 'start', 'lag_start',
                               'high')]
        if any(not isinstance(value, (int, float))
               or isinstance(value, bool) or not math.isfinite(value)
               for value in ordered):
            return case.finish('inconclusive', 'the served setpoint '
                               'chain is incomplete: '
                               + json.dumps(table))
        if any(ordered[i] >= ordered[i + 1]
               for i in range(len(ordered) - 1)):
            return case.finish('inconclusive', 'the served setpoint '
                               'chain is not strictly increasing: '
                               + json.dumps(table))
        if not isinstance(start_delay, int) \
                or isinstance(start_delay, bool) or start_delay < 0:
            return case.finish('inconclusive', 'the pump group serves '
                               'no usable start_delay_ticks: '
                               + json.dumps(start_delay))
        if not isinstance(high_limit, (int, float)) \
                or isinstance(high_limit, bool) \
                or not math.isfinite(high_limit) \
                or high_limit != table['high']:
            return case.finish('inconclusive', 'the wired alarm\'s '
                               'high_limit is not the chain\'s served '
                               'high: ' + json.dumps(high_limit))
        if not isinstance(hysteresis, (int, float)) \
                or isinstance(hysteresis, bool) \
                or not math.isfinite(hysteresis):
            hysteresis = 0.0

        if ctx.get('plant') is None:
            return case.finish('inconclusive',
                               'the run publishes no plant endpoint')
        owner = (ctx.get('plant_owner') or {}).get(active)
        if owner is None:
            return case.finish('inconclusive', 'the run pins no '
                               'plant-writer owner token for the '
                               'settled active ' + str(active))
        # The claim seam splits here: the field census and the inflow
        # reads ride the shipped dcs-plant-ctl (list/read need no
        # writer claim), while the standing shared claim and the
        # writes under it stay on the raw client — the tool exposes no
        # ensure_writer under a chosen owner token, and its `write`
        # would claim under the tool token and fence against the
        # active's standing claim rather than join it.
        stream = _plant_connect(ctx)
        field = _field_inputs(ctx)
        if points['inflow'] not in field:
            return case.finish('inconclusive', 'the inflow signal\'s '
                               'point ' + str(points['inflow'])
                               + ' is not a field in-point the plant '
                               'serves')
        verdict = _plant_request(stream, {'op': 'ensure_writer',
                                          'owner': owner})
        if verdict.get('result') not in ('done', 'claimed_shared'):
            return case.finish('inconclusive', 'the writer claim '
                               'refused the shared attachment under '
                               'the active\'s pinned token: '
                               + json.dumps(verdict)[:300])
        case.observe('plant protocol attached under ' + active
                     + '\'s writer claim ('
                     + str(verdict.get('result')) + ')')
        baseline_inflow = (_plant_read(ctx, points['inflow'])
                           .get('value') or {}).get('float')
        if not isinstance(baseline_inflow, (int, float)) \
                or isinstance(baseline_inflow, bool) \
                or not math.isfinite(baseline_inflow):
            return case.finish('inconclusive', 'the inflow point '
                               'serves no float baseline: '
                               + json.dumps(baseline_inflow))

        last = {}
        seen = set()

        def value(key, snap):
            """The served value of one named point — and the point's
            presence, for the never-reported inconclusive split."""
            sample = _point_sample(snap, points[key])
            if sample is None:
                return None
            seen.add(key)
            raw = sample.get('value')
            if isinstance(raw, dict):
                return next(iter(raw.values()), None)
            return raw

        def poll(cond, keys=()):
            snap = _try_snapshot(ctx, base)
            if snap is None:
                return None
            last['snap'] = snap
            for key in keys:
                if _point_sample(snap, points[key]) is not None:
                    seen.add(key)
            return snap if cond(snap) else None

        def leg(name, cond, keys):
            """One served-transition wait plus its evidence file. A
            miss classifies inconclusive when an awaited output never
            reported a sample, staging-failed when the served values
            never landed the leg."""
            hit = wait_for(lambda: poll(cond, keys),
                           time.monotonic() + LAG_STAGING_DEADLINE,
                           interval=LAG_STAGING_POLL)
            snap = last.get('snap') or {}
            ref = save_evidence(
                ctx['evidence_dir'], 'lag-staging-' + name + '.json',
                {'tick': snap.get('tick'),
                 'samples': {key: _point_sample(snap, points[key])
                             for key in sorted(keys)}})
            case.evidence('file', ref, 'the served ' + name + ' leg')
            if hit:
                return hit, None
            unreported = sorted(key for key in keys if key not in seen)
            if unreported:
                return None, case.finish(
                    'inconclusive', 'the ' + name + ' leg\'s outputs '
                    'never reported on the served snapshot: '
                    + ', '.join(unreported))
            return None, case.finish(
                'failed', 'staging-failed: the ' + name + ' leg never '
                'landed; last served ' + json.dumps(
                    {key: value(key, snap) for key in sorted(keys)},
                    sort_keys=True)[:500])

        def settled_low(snap):
            if value('demand', snap) != 0 \
                    or value('staged', snap) != 0:
                return None
            for key in ('duty_call', 'lag_call', 'below_cutoff',
                        'high_level', 'alarm', 'unack', 'shelved',
                        'suppressed', 'out_of_service'):
                if value(key, snap) is not False:
                    return None
            level = value('level', snap)
            if not isinstance(level, (int, float)) \
                    or isinstance(level, bool) \
                    or level >= table['start']:
                return None
            return snap

        baseline = wait_for(lambda: poll(settled_low),
                            time.monotonic() + LAG_STAGING_DEADLINE,
                            interval=LAG_STAGING_POLL)
        snap = last.get('snap') or {}
        pump0 = {key: value(key, snap)
                 for key in ('p1_mode', 'p1_oos', 'p2_mode', 'p2_oos',
                             'duty')}
        ref = save_evidence(
            ctx['evidence_dir'], 'lag-staging-baseline.json',
            {'tick': snap.get('tick'), 'inflow': baseline_inflow,
             'level': value('level', snap), 'pump': pump0})
        case.evidence('file', ref, 'the settled low-level baseline')
        if baseline is None:
            return case.finish('inconclusive', 'the station never '
                               'read the settled low-level baseline '
                               'ahead of the drive')

        # The journal floor ahead of the drive: this leg's records are
        # the ones above it. The snapshot tick anchors the /history
        # transition scan.
        _, journal0 = http_json('GET', base + '/journal?since=0')
        floor = max((entry.get('seq') or 0
                     for entry in _journal_list(journal0)
                     if isinstance(entry, dict)), default=0)
        drive_tick = baseline.get('tick') or 0

        # The rising edge: inflow above the high setpoint and both
        # pumps' declared draw, so the level climbs unopposed through
        # every threshold — the honest lever the dynamics document.
        rise_inflow = table['high'] + 1.0
        verdict = _plant_request(
            stream, {'op': 'write', 'point': points['inflow'],
                     'value': {'float': rise_inflow}})
        if verdict.get('result') != 'done':
            return case.finish('failed', 'staging-failed: the inflow '
                               'write on point ' + str(points['inflow'])
                               + ' was refused under the shared '
                               'claim: ' + json.dumps(verdict)[:300])
        restore_inflow = (points['inflow'], baseline_inflow)
        case.observe('inflow driven to ' + str(rise_inflow)
                     + ' — past the ' + str(table['high'])
                     + ' high setpoint and both pumps\' draw')

        hit, error = leg(
            'duty-call',
            lambda s: isinstance(value('demand', s), int)
            and not isinstance(value('demand', s), bool)
            and value('demand', s) >= 1
            and value('duty_call', s) is True,
            ('demand', 'demand_in', 'duty_call', 'staged'))
        if error:
            return error
        case.observe('demand staged to '
                     + str(value('demand', hit)) + ' with duty_call '
                     'at tick ' + str(hit.get('tick')))

        hit, error = leg(
            'lag-call',
            lambda s: value('demand', s) == 2
            and value('lag_call', s) is True,
            ('demand', 'demand_in', 'duty_call', 'lag_call', 'staged'))
        if error:
            return error
        case.observe('demand staged to 2 with lag_call at tick '
                     + str(hit.get('tick')))

        hit, error = leg(
            'staged',
            lambda s: value('staged', s) == 2,
            ('demand', 'demand_in', 'staged'))
        if error:
            return error
        case.observe('the pump group staged both pumps at tick '
                     + str(hit.get('tick')))

        hit, error = leg(
            'annunciated',
            lambda s: value('high_level', s) is True
            and value('alarm', s) is True
            and value('unack', s) is True
            and value('shelved', s) is False
            and value('suppressed', s) is False
            and value('out_of_service', s) is False,
            ('level', 'high_level', 'alarm', 'unack', 'shelved',
             'suppressed', 'out_of_service'))
        if error:
            return error
        case.observe('the high crossing annunciated at tick '
                     + str(hit.get('tick')) + ': high_level, the '
                     'managed alarm standing unacknowledged, no '
                     'shelved/suppressed/out-of-service flag')
        if _settled_active(ctx) != active:
            return case.finish('failed', 'staging-failed: the active '
                               'role moved while the level rose — a '
                               'process drive is not peer loss')

        # The falling edge: the inflow back to its baseline and the
        # running pumps pull the level down through the chain.
        verdict = _plant_request(
            stream, {'op': 'write', 'point': points['inflow'],
                     'value': {'float': baseline_inflow}})
        if verdict.get('result') != 'done':
            return case.finish('failed', 'staging-failed: the inflow '
                               'restore write was refused: '
                               + json.dumps(verdict)[:300])

        hit, error = leg(
            'destaged',
            lambda s: isinstance(value('staged', s), int)
            and not isinstance(value('staged', s), bool)
            and value('staged', s) <= 1
            and isinstance(value('demand', s), int)
            and not isinstance(value('demand', s), bool)
            and value('demand', s) <= 1
            and value('lag_call', s) is False
            and value('duty_call', s) is True,
            ('demand', 'demand_in', 'staged', 'duty_call', 'lag_call',
             'high_level', 'alarm'))
        if error:
            return error
        case.observe('the lag de-staged (staged '
                     + str(value('staged', hit)) + ') at tick '
                     + str(hit.get('tick')) + ' while duty_call still '
                     'stood — the declared lag-first release order')

        hit, error = leg(
            'released',
            lambda s: value('demand', s) == 0
            and value('staged', s) == 0
            and value('duty_call', s) is False
            and value('high_level', s) is False
            and value('alarm', s) is False
            and value('unack', s) is True,
            ('demand', 'demand_in', 'staged', 'duty_call', 'lag_call',
             'high_level', 'alarm', 'unack'))
        if error:
            return error
        case.observe('pump-down complete at tick '
                     + str(hit.get('tick')) + ' — the duty released, '
                     'the alarm returned on its hysteresis, the latch '
                     'still standing for the ack')

        # The lifecycle's second flag: the receipted ack clears the
        # held unacknowledged latch; the restore write then re-arms
        # the input for the next trip.
        write = {'point': points['ack'], 'kind': 'bool',
                 'value': {'bool': True}}
        status, receipt = http_json(
            'POST', base + '/command',
            {'command': {'write_value': write},
             'actor': LAG_STAGING_ACTOR})
        ref = save_evidence(ctx['evidence_dir'],
                            'lag-staging-ack-receipt.json',
                            {'status': status, 'body': receipt})
        case.evidence('file', ref, 'the ack submission receipt')
        outcome = (receipt or {}).get('outcome') or {}
        if status != 200 or 'rejected' in outcome:
            return case.finish('failed', 'staging-failed: the ack '
                               'write was refused: ' + str(status)
                               + ' ' + json.dumps(receipt)[:400])
        restore_ack = (base, points['ack'])

        hit, error = leg('acknowledged',
                         lambda s: value('unack', s) is False,
                         ('alarm', 'unack'))
        if error:
            return error
        case.observe('the settled ack cleared the unacknowledged '
                     'latch at tick ' + str(hit.get('tick')))

        restore = {'point': points['ack'], 'kind': 'bool',
                   'value': {'bool': False}}
        status, receipt = http_json(
            'POST', base + '/command',
            {'command': {'write_value': restore},
             'actor': LAG_STAGING_ACTOR})
        ref = save_evidence(ctx['evidence_dir'],
                            'lag-staging-ack-restored.json',
                            {'status': status, 'body': receipt})
        case.evidence('file', ref, 'the ack-restore receipt')
        outcome = (receipt or {}).get('outcome') or {}
        if status != 200 or 'rejected' in outcome:
            return case.finish('failed', 'staging-failed: the ack '
                               'restore write was refused: '
                               + str(status) + ' '
                               + json.dumps(receipt)[:400])

        hit, error = leg('ack-released',
                         lambda s: value('ack', s) is False
                         and value('unack', s) is False,
                         ('ack', 'unack'))
        if error:
            return error
        restore_ack = None

        restored = (_plant_read(ctx, points['inflow'])
                    .get('value') or {}).get('float')
        if restored != baseline_inflow:
            return case.finish('failed', 'staging-failed: the inflow '
                               'point did not restore — it reads '
                               + json.dumps(restored)
                               + ' against baseline '
                               + json.dumps(baseline_inflow))
        restore_inflow = None

        # The durable record: each alarm-side transition lands its
        # point_changed on a declared-journaled point — assert then
        # clear — while the pair journaled no role change and no
        # non-journaled staging point records a transition.
        expected_journal = {
            'high_level': [{'bool': True}, {'bool': False}],
            'alarm': [{'bool': True}, {'bool': False}],
            'unack': [{'bool': True}, {'bool': False}]}
        nonjournaled = {points[key] for key in
                        ('demand', 'demand_in', 'duty', 'staged',
                         'duty_call', 'lag_call')}
        complete = {}
        violations = {}

        def journaled():
            try:
                _, journal = http_json('GET', base + '/journal?since='
                                       + str(floor))
            except Exception:
                return None
            last['journal'] = journal
            changes = _journal_point_changes(journal)
            for key, wanted in expected_journal.items():
                got = changes.get(points[key], [])
                if got == wanted:
                    complete[key] = got
                elif len(got) > len(wanted) or got != wanted[:len(got)]:
                    violations['transitions-' + key] = \
                        'point ' + str(points[key]) + ' journaled ' \
                        + json.dumps(got) \
                        + ' — not the declared assert/clear pair'
            for entry in _journal_list(journal):
                event = entry.get('event') or {}
                if 'role_changed' in event:
                    violations['role-change'] = \
                        'a role_changed event journaled while the ' \
                        'level drove'
                change = event.get('point_changed')
                if isinstance(change, dict) \
                        and change.get('point') in nonjournaled:
                    violations['unjournaled-' + str(change['point'])] = \
                        'the non-journaled point ' \
                        + str(change['point']) \
                        + ' journaled a transition'
            if len(complete) == len(expected_journal) or violations:
                return journal
            return None

        wait_for(journaled, time.monotonic() + LAG_STAGING_DEADLINE,
                 interval=POLL_INTERVAL)
        ref = save_evidence(
            ctx['evidence_dir'], 'lag-staging-journal.json',
            {'floor': floor, 'complete': sorted(complete),
             'violations': sorted(violations)})
        case.evidence('file', ref, 'the journaled transitions above '
                      'the pre-drive floor')
        if violations:
            return case.finish(
                'failed', 'staging-nondeterministic: ' + '; '.join(
                    violations[key] for key in sorted(violations)))
        missing = [key for key in expected_journal
                   if key not in complete]
        if missing:
            return case.finish('failed', 'staging-failed: the served '
                               'journal never recorded the declared '
                               'point_changed assert/clear pair on: '
                               + ', '.join(missing))

        # The tick-domain proof: /history carries every crossing in
        # scan order. Each edge anchors at its own demand-1 transition
        # — the harness write lands between scans, so absolute ticks
        # shift with the attachment while the inter-transition deltas
        # the chain, carrier, and delay produce stay fixed.
        watch = {key: points[key] for key in
                 ('demand', 'demand_in', 'staged', 'duty_call',
                  'lag_call', 'high_level', 'alarm', 'unack', 'duty')}
        query = ''.join('&point=' + str(point)
                        for point in sorted(set(watch.values())))
        _, history = http_json('GET', base + '/history?since=0'
                               + query)
        transitions = {key: _history_transitions(history, point,
                                                 drive_tick)
                       for key, point in watch.items()}

        def nth(key, landed, n=1):
            return _nth_transition(transitions.get(key) or [],
                                   landed, n)

        rise_anchor = nth('demand', 1)
        rise = {'demand-1': rise_anchor,
                'duty_call': nth('duty_call', True),
                'staged-1': nth('staged', 1),
                'demand-2': nth('demand', 2),
                'lag_call': nth('lag_call', True),
                'staged-2': nth('staged', 2),
                'high_level': nth('high_level', True),
                'alarm': nth('alarm', True),
                'unack': nth('unack', True)}
        missing = sorted(key for key, tick in rise.items()
                         if tick is None)
        if missing:
            return case.finish('failed', 'staging-failed: the served '
                               'history never showed '
                               + ', '.join(missing)
                               + ' on the rising edge: '
                               + json.dumps({key: transitions[key]
                                             for key in
                                             ('demand', 'staged',
                                              'alarm')})[:400])
        deltas_rise = {key: rise[key] - rise_anchor
                       for key in rise}

        fall_anchor = nth('demand', 1, 2)
        fall = {'high_level': nth('high_level', False),
                'alarm': nth('alarm', False),
                'demand-1': fall_anchor,
                'lag_call': nth('lag_call', False),
                'staged-1': nth('staged', 1, 2),
                'demand-0': nth('demand', 0),
                'duty_call': nth('duty_call', False),
                'staged-0': nth('staged', 0)}
        missing = sorted(key for key, tick in fall.items()
                         if tick is None)
        if missing:
            return case.finish('failed', 'staging-failed: the served '
                               'history never showed '
                               + ', '.join(missing)
                               + ' on the falling edge: '
                               + json.dumps({key: transitions[key]
                                             for key in
                                             ('demand', 'staged',
                                              'alarm')})[:400])
        deltas_fall = {key: fall[key] - fall_anchor
                       for key in fall}

        problems = []
        if rise['duty_call'] != rise_anchor:
            problems.append('duty_call did not assert with demand 1 '
                            '(+' + str(rise['duty_call'] - rise_anchor)
                            + ')')
        if not rise['demand-1'] <= rise['staged-1'] \
                <= rise['demand-2']:
            problems.append('the duty stage did not answer between '
                            'demand 1 and demand 2')
        if rise['lag_call'] != rise['demand-2']:
            problems.append('lag_call did not assert with demand 2 '
                            '(+' + str(rise['lag_call']
                                       - rise['demand-2']) + ')')
        if not rise['demand-2'] <= rise['staged-2'] \
                <= rise['high_level']:
            problems.append('the lag stage did not answer between '
                            'demand 2 and the high crossing')
        if not rise['high_level'] <= rise['alarm'] <= rise['unack']:
            problems.append('the annunciation did not follow the '
                            'high crossing in order')
        # The declared delay: the group's staged answers within
        # start_delay_ticks of the demand it consumed — the 205
        # carrier delivers the served demand one scan later, so the
        # served-demand bound is the delay plus that scan.
        for k in (1, 2):
            staged_t = rise['staged-' + str(k)]
            served_t = rise['demand-' + str(k)]
            consumed_t = nth('demand_in', k)
            if staged_t - served_t > start_delay + 1:
                problems.append(
                    'staged ' + str(k) + ' answered '
                    + str(staged_t - served_t) + ' ticks after demand '
                    + str(k) + ' — beyond start_delay_ticks '
                    + str(start_delay) + ' plus the carrier scan')
            if consumed_t is not None \
                    and staged_t - consumed_t > start_delay:
                problems.append(
                    'staged ' + str(k) + ' answered '
                    + str(staged_t - consumed_t) + ' ticks after the '
                    'group\'s consumed demand ' + str(k)
                    + ' — beyond start_delay_ticks '
                    + str(start_delay))
        if not fall['high_level'] <= fall['alarm'] \
                <= fall['demand-1'] <= fall['lag_call'] \
                <= fall['staged-1'] <= fall['demand-0'] \
                <= fall['duty_call'] <= fall['staged-0']:
            problems.append('the falling edge did not release in '
                            'order — high_level, alarm, lag demand, '
                            'lag stage, duty demand, duty stage: '
                            + json.dumps(deltas_fall, sort_keys=True))
        if fall['staged-1'] >= fall['staged-0']:
            problems.append('the lag never de-staged ahead of the '
                            'duty release')
        if problems:
            return case.finish('failed',
                               'staging-nondeterministic: '
                               + '; '.join(problems))

        # The restore audit: pump operator state the leg never drove
        # reads back unchanged, and the pair's roles never moved.
        final = _try_snapshot(ctx, base) or {}
        pump1 = {key: value(key, final)
                 for key in ('p1_mode', 'p1_oos', 'p2_mode', 'p2_oos')}
        moved = sorted(key for key in pump1
                       if pump1[key] != pump0.get(key))
        if moved:
            return case.finish('failed', 'staging-failed: the leg '
                               'moved pump operator state it never '
                               'drove: ' + ', '.join(moved))
        if _settled_active(ctx) != active \
                or _tracking_peer(ctx, active) != tracking:
            return case.finish('failed', 'staging-failed: the pair\'s '
                               'roles moved during the leg')
        ref = save_evidence(
            ctx['evidence_dir'], 'lag-staging-legs.json',
            {'drive': {'inflow_rise': rise_inflow,
                       'inflow_restored': baseline_inflow},
             'setpoints': table,
             'start_delay_ticks': start_delay,
             'rise': deltas_rise,
             'fall': deltas_fall,
             'duty': [[tick - fall_anchor, landed]
                      for tick, landed in transitions['duty']],
             'journaled': {key: complete[key]
                           for key in sorted(complete)}})
        case.evidence('file', ref, 'the tick-domain transition '
                      'evidence — deltas anchored per edge')
        case.observe('chain walked: rise deltas '
                     + json.dumps(deltas_rise, sort_keys=True)
                     + ', fall deltas '
                     + json.dumps(deltas_fall, sort_keys=True))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
    finally:
        # The inflow write and the ack input are the run's shared
        # state: a case that leaves either standing poisons every
        # later scenario.
        if stream is not None:
            if restore_inflow is not None:
                try:
                    _plant_request(
                        stream, {'op': 'write',
                                 'point': restore_inflow[0],
                                 'value': {'float': restore_inflow[1]}})
                except Exception:
                    pass
            try:
                stream.close()
            except Exception:
                pass
        if restore_ack is not None:
            rbase, rpoint = restore_ack
            try:
                http_json('POST', rbase + '/command',
                          {'command': {'write_value': {
                              'point': rpoint, 'kind': 'bool',
                              'value': {'bool': False}}},
                           'actor': LAG_STAGING_ACTOR})
            except Exception:
                pass


# --------------------------------------------------------------------
# The declared duty-rotation contract and its carryover across
# promotion (WW-CTL-001's rotation clause — decision 41 — and
# WW-LCM-001's named continuity state): the rig's pump group declares
# rotation = alternate-each-cycle with min_off_ticks and
# start_delay_ticks, so consecutive demand cycles must hand `duty`
# to the next available pump in rotation order, and the rotation
# position plus the banked run-hours must ride the checkpoint into
# the promoted peer rather than reset. The honest lever the deployed
# rig admits is the group's own writable maintenance points: holding
# both pumps out of service lets the lane dynamics' declared inflow
# lift the level past `start` unopposed, and releasing them returns
# the station to the group, whose staged duty holder drains the well
# back through `stop`. The demote/promote lands mid-cycle — while the
# rotated duty's in-flight command stands — and every writable point,
# the none-available annunciation the hold latches, and the pair's
# roles are restored for the later legs.
#
# Rotation position and run-hours read back through the checkpoint the
# monitor serves — the same StateMap the tracking peer adopts — so the
# carryover assertions observe exactly what WW-LCM-001 names. Named
# diagnostics: rotation-failed for a broken rotation or carryover
# clause, rotation-nondeterministic when the deployed dynamics cannot
# drive a deterministic cycle.

ROTATION_DEADLINE = 30         # bound on settle, track, and audit waits
ROTATION_RISE_DEADLINE = 15    # bound on the unopposed rise past `start`
ROTATION_CYCLE_DEADLINE = 20   # bound on each start/stop observation
ROTATION_SWITCH_DEADLINE = 30  # bound on each demote/promote leg
ROTATION_POLL = 0.5            # level-edge cadence
ROTATION_ACTOR = 'qa-lane'
ROTATION_MIN_OFF_TICKS = 2     # the rig model's declared holdout


def _rotation_component(checkpoint):
    """The pump-group's checkpointed StateMap — the component whose
    carried vocabulary names the rotation position — as (name, state)."""
    for name, state in (checkpoint or {}).get('components', {}).items():
        if isinstance(state, dict) and 'rotation_cursor' in state:
            return name, state
    return None, {}


def _state_value(state, key):
    """One scalar out of a checkpoint StateMap — the `{"int": v}` /
    `{"bool": v}` wire shape unwrapped."""
    value = (state or {}).get(key)
    if isinstance(value, dict):
        return next(iter(value.values()), None)
    return value


def _point_changes(journal, point):
    """The `to` values the journal recorded for `point`, in seq order —
    point_changed entries only."""
    changes = []
    for entry in _journal_list(journal):
        change = (entry.get('event') or {}).get('point_changed') or {}
        if change.get('point') == point:
            changes.append((entry.get('tick'), change.get('to')))
    return changes


def _checkpoint_at(ctx, base):
    """`GET /checkpoint` or None — one dropped read loses a sample, not
    the leg's verdict."""
    try:
        _, checkpoint = http_json('GET', base + '/checkpoint')
        return checkpoint
    except Exception:
        return None


def scenario_duty_rotation(ctx):
    """Two demand cycles alternate the declared duty holder under the
    alternate-each-cycle policy with the declared off-time and start
    delay honored, and the rotation position plus banked run-hours
    ride the checkpoint across a mid-cycle demote/promote."""
    case = Case('duty-rotation',
                'Duty rotation alternates and carries across promotion',
                'with the pair settled and the station answering the '
                'declared dynamics, two consecutive demand cycles '
                'alternate duty between the pumps under the declared '
                'alternate-each-cycle policy with min_off_ticks and '
                'start_delay_ticks honored and staged reporting the '
                'commanded count; a demote/promote mid-cycle leaves '
                'the promoted peer naming the rotation position and '
                'run-hours the checkpoint carried rather than reset; '
                'every commanded transition settles through the '
                'receipted path with journaled evidence and the rig '
                'is restored')
    held = []          # oos points while they stand held true
    ack_held = None    # the ack point while it stands true
    restored = True    # roles back as the case found them
    try:
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + ROTATION_DEADLINE)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        peer = 'standby' if active == 'active' else 'active'
        base, peer_base = ctx[active], ctx[peer]
        live = base    # writes and observations land on the current active

        def converged():
            try:
                report = _role(ctx, peer_base)
            except Exception:
                return None
            return report if 'tracking' in (report.get('sync') or {}) \
                else None

        tracking = wait_for(converged,
                            time.monotonic() + ROTATION_DEADLINE)
        ref = save_evidence(ctx['evidence_dir'],
                            'duty-rotation-peer-role.json',
                            {'peer': peer, 'report': tracking})
        case.evidence('file', ref, 'the tracking peer\'s role report')
        if not tracking:
            return case.finish('inconclusive', 'the peer never '
                               'reported tracking convergence — the '
                               'rotation pair never settled')
        case.observe('settled pair: ' + active + ' active, ' + peer
                     + ' tracking')

        _, signals = http_json('GET', base + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'duty-rotation-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming the rotation '
                      'leg\'s points')
        names = {'p101-oos': 'oos1', 'p102-oos': 'oos2',
                 'level-selected': 'level', 'demand-in': 'demand',
                 'duty': 'duty', 'staged': 'staged',
                 'p101-cmd': 'cmd1', 'p102-cmd': 'cmd2',
                 'p101-run': 'run1', 'p102-run': 'run2',
                 'none-available': 'none_available',
                 'none-available-ack': 'ack',
                 'none-available-unacknowledged': 'unack',
                 'inflow': 'inflow'}
        entries = {entry.get('name'): entry
                   for entry in signals.get('points', [])
                   if entry.get('name') in names}
        missing = sorted(set(names) - set(entries))
        if missing:
            return case.finish('inconclusive', 'the deployed model '
                               'lacks the rotation leg\'s wiring — no '
                               'signals ' + ', '.join(missing))
        points = {names[name]: entry.get('point')
                  for name, entry in entries.items()}
        for name in ('p101-oos', 'p102-oos', 'none-available-ack'):
            entry = entries[name]
            if not (entry.get('writable')
                    and entry.get('direction') == 'in'
                    and entry.get('value_type') == 'bool'):
                return case.finish('inconclusive', 'signal ' + name
                                   + ' is not the writable bool input '
                                   'the leg needs: '
                                   + json.dumps(entry)[:300])

        def cmd_point(index):
            return points['cmd1' if index == 1 else 'cmd2']

        def run_point(index):
            return points['run1' if index == 1 else 'run2']

        baseline = _snapshot(ctx, base)
        inflow = _point_value(baseline, points['inflow'])
        grown = wait_for(
            lambda: (s.get('tick', 0) > baseline.get('tick', 0)
                     and s or None)
            if (s := _try_snapshot(ctx, base)) else None,
            time.monotonic() + ROTATION_DEADLINE,
            interval=ROTATION_POLL)
        ref = save_evidence(ctx['evidence_dir'],
                            'duty-rotation-baseline.json',
                            {'baseline': baseline, 'advanced': grown,
                             'inflow': inflow})
        case.evidence('file', ref, 'the settled baseline and the '
                      'declared inflow')
        if not grown:
            return case.finish('failed', 'the active peer\'s scans '
                               'never advanced')
        if not isinstance(inflow, (int, float)):
            return case.finish('inconclusive',
                               'rotation-nondeterministic: the inflow '
                               'point serves no numeric sample — the '
                               'level cannot be trusted to rise '
                               + json.dumps(inflow))

        floors = {}
        for key, address in ((active, base), (peer, peer_base)):
            try:
                _, journal = http_json('GET', address + '/journal?since=0')
            except Exception:
                journal = []
            floors[key] = max(
                (entry.get('seq') or 0 for entry in _journal_list(journal)
                 if isinstance(entry, dict)), default=0)

        submitted = []

        def write(target_base, point, value):
            command = {'write_value': {'point': point, 'kind': 'bool',
                                       'value': {'bool': value}}}
            status, receipt = http_json(
                'POST', target_base + '/command',
                {'command': command, 'actor': ROTATION_ACTOR})
            submitted.append({'base': target_base, 'command': command,
                              'status': status, 'receipt': receipt})
            outcome = (receipt or {}).get('outcome') or {}
            return status == 200 and 'rejected' not in outcome

        observed = {}

        def watch(predicate):
            last = {}

            def probe():
                snap = _try_snapshot(ctx, live)
                if snap is None:
                    return None
                last['snap'] = snap
                return snap if predicate(snap) else None
            return probe, last

        # Leg 1 — the unopposed rise: both pumps held out of service
        # while the declared inflow lifts the level past `start`; the
        # group stages nothing and none_available annunciates.
        held_ok = True
        for point in (points['oos1'], points['oos2']):
            if not write(live, point, True):
                held_ok = False
            else:
                held.append(point)
        ref = save_evidence(ctx['evidence_dir'],
                            'duty-rotation-hold-receipts.json',
                            submitted[-2:])
        case.evidence('file', ref, 'the out-of-service hold receipts')
        if not held_ok:
            return case.finish('failed', 'an out-of-service write was '
                               'refused: '
                               + json.dumps(submitted[-1])[:300])

        def risen(snap):
            level = _point_value(snap, points['level'])
            return isinstance(level, (int, float)) and level > 2.0 \
                and _point_value(snap, points['staged']) == 0 \
                and _point_value(snap, points['none_available']) is True

        probe, last = watch(risen)
        hit = wait_for(probe, time.monotonic() + ROTATION_RISE_DEADLINE,
                       interval=ROTATION_POLL)
        ref = save_evidence(ctx['evidence_dir'],
                            'duty-rotation-held.json',
                            hit or last.get('snap') or {})
        case.evidence('file', ref, 'the held snapshot at level > start')
        if not hit:
            return case.finish('inconclusive',
                               'rotation-nondeterministic: the declared '
                               'inflow never raised the level past '
                               'start while both pumps were held out — '
                               'last level '
                               + json.dumps(_point_value(
                                   last.get('snap') or {},
                                   points['level'])))
        case.observe('held both pumps out: level rose past start, '
                     'staged 0, none_available standing')

        # Leg 2 — release and the first cycle: the group stages one
        # pump as duty, drains to `stop`, and banks the holdout.
        released_ok = True
        for point in (points['oos1'], points['oos2']):
            if write(live, point, False):
                held.remove(point)
            else:
                released_ok = False
        ref = save_evidence(ctx['evidence_dir'],
                            'duty-rotation-release-receipts.json',
                            submitted[-2:])
        case.evidence('file', ref, 'the out-of-service release '
                      'receipts')
        if not released_ok:
            return case.finish('failed', 'an out-of-service release '
                               'was refused: '
                               + json.dumps(submitted[-1])[:300])

        duty_stuck = {}

        def staged_one(snap):
            staged = _point_value(snap, points['staged'])
            duty = _point_value(snap, points['duty'])
            duty_stuck['duty'] = duty
            duty_stuck['staged'] = staged
            return staged == 1 and duty in (1, 2) \
                and _point_value(snap, cmd_point(duty)) is True

        probe, last = watch(staged_one)
        first = wait_for(probe,
                         time.monotonic() + ROTATION_CYCLE_DEADLINE,
                         interval=ROTATION_POLL)
        ref = save_evidence(ctx['evidence_dir'],
                            'duty-rotation-cycle1-start.json',
                            first or last.get('snap') or {})
        case.evidence('file', ref, 'the first cycle\'s staged duty '
                      'holder')
        if first is None:
            if duty_stuck.get('duty') in (None, 0):
                return case.finish(
                    'inconclusive',
                    'rotation-nondeterministic: duty never moved off '
                    + json.dumps(duty_stuck.get('duty'))
                    + ' through the first demand cycle')
            return case.finish('failed',
                               'rotation-failed: the released pump '
                               'group never staged a duty pump')
        duty1 = _point_value(first, points['duty'])
        duty2 = 3 - duty1
        case.observe('first cycle: duty=' + str(duty1)
                     + ' staged 1, command standing on point '
                     + str(cmd_point(duty1)))

        def stopped(snap):
            level = _point_value(snap, points['level'])
            return _point_value(snap, points['staged']) == 0 \
                and _point_value(snap, cmd_point(duty1)) is not True \
                and isinstance(level, (int, float)) and level < 1.0

        probe, last = watch(stopped)
        ended = wait_for(probe,
                         time.monotonic() + ROTATION_CYCLE_DEADLINE,
                         interval=ROTATION_POLL)
        ref = save_evidence(ctx['evidence_dir'],
                            'duty-rotation-cycle1-stop.json',
                            ended or last.get('snap') or {})
        case.evidence('file', ref, 'the first cycle\'s stop at `stop`')
        if not ended:
            return case.finish('failed',
                               'rotation-failed: the first duty '
                               'holder never stopped at stop')
        case.observe('first cycle ended: pump ' + str(duty1)
                     + ' stopped at stop')

        # The post-stop checkpoint carries the rotation position the
        # cycle-end assign produced plus the banked holdout — the
        # min_off_ticks evidence — and the accrued run-hours.
        def served_state():
            checkpoint = _checkpoint_at(ctx, live)
            if checkpoint is None:
                return None
            name, found = _rotation_component(checkpoint)
            if not found:
                return None
            observed['checkpoint'] = checkpoint
            observed['component'] = name
            observed['state'] = found
            return found

        state = wait_for(served_state,
                         time.monotonic() + ROTATION_DEADLINE,
                         interval=ROTATION_POLL)
        checkpoint = observed.get('checkpoint') or {}
        _, journal = http_json('GET', live + '/journal?since='
                               + str(floors[active]))
        run_ticks = [tick for tick, to in
                     _point_changes(journal, run_point(duty1))
                     if to == {'bool': False}]
        component = observed.get('component')
        if state is None:
            return case.finish('inconclusive', 'the served checkpoint '
                               'never carried the pump-group state')
        ref = save_evidence(
            ctx['evidence_dir'], 'duty-rotation-cycle1-state.json',
            {'component': component, 'tick': checkpoint.get('tick'),
             'state': state, 'run_stop_ticks': run_ticks})
        case.evidence('file', ref, 'the checkpointed pump-group state '
                      'after the first cycle')
        unmet = []
        if _state_value(state, 'rotation') != 0:
            unmet.append('rotation != alternate-each-cycle')
        if _state_value(state, 'min_off_ticks') \
                != ROTATION_MIN_OFF_TICKS:
            unmet.append('min_off_ticks != 2')
        if _state_value(state, 'duty') != duty2:
            unmet.append('duty did not rotate to pump ' + str(duty2)
                         + ' (reads ' + json.dumps(
                             _state_value(state, 'duty')) + ')')
        if _state_value(state, 'commanded_' + str(duty1)) is not False:
            unmet.append('the stopped holder is still commanded')
        held_until = _state_value(state, 'held_until_' + str(duty1))
        if not run_ticks:
            unmet.append('no journaled run-contact stop to bound the '
                         'holdout against')
        elif not isinstance(held_until, int) \
                or held_until < run_ticks[-1] + 1:
            unmet.append('the banked holdout ' + json.dumps(held_until)
                         + ' does not extend past the journaled stop '
                         + json.dumps(run_ticks[-1]))
        if not isinstance(_state_value(state, 'run_hours_'
                                       + str(duty1)), int) \
                or _state_value(state, 'run_hours_' + str(duty1)) <= 0:
            unmet.append('no run-hours banked for pump ' + str(duty1))
        if unmet:
            return case.finish('failed', 'rotation-failed: '
                               + '; '.join(unmet))
        carried_duty = _state_value(state, 'duty')
        carried_cursor = _state_value(state, 'rotation_cursor')
        case.observe('post-stop state: duty=' + str(carried_duty)
                     + ' rotation_cursor=' + str(carried_cursor)
                     + ' held_until_' + str(duty1) + '='
                     + str(held_until) + ' run_hours_' + str(duty1)
                     + '=' + str(_state_value(state, 'run_hours_'
                                              + str(duty1))))

        # Leg 3 — the second cycle starts on the rotated duty: the
        # other pump stages and commands.
        def staged_two(snap):
            return _point_value(snap, points['staged']) == 1 \
                and _point_value(snap, points['duty']) == duty2 \
                and _point_value(snap, cmd_point(duty2)) is True

        probe, last = watch(staged_two)
        second = wait_for(probe,
                          time.monotonic() + ROTATION_CYCLE_DEADLINE,
                          interval=ROTATION_POLL)
        ref = save_evidence(ctx['evidence_dir'],
                            'duty-rotation-cycle2-start.json',
                            second or last.get('snap') or {})
        case.evidence('file', ref, 'the second cycle\'s rotated duty '
                      'holder')
        if not second:
            return case.finish('failed',
                               'rotation-failed: the second cycle '
                               'never staged the rotated duty pump '
                               + str(duty2))
        case.observe('second cycle: duty=' + str(duty2)
                     + ' staged 1 — the rotation alternated')

        # Leg 4 — the mid-cycle demote/promote while the rotated
        # duty's in-flight command stands: the promoted peer must name
        # the same rotation position the checkpoint carried, with the
        # in-flight command and banked run-hours intact.
        status, body = http_json('POST', live + '/demote')
        case.observe('demote ' + active + ': ' + str(status) + ' '
                     + json.dumps(body))
        if status != 200:
            return case.finish('failed', 'the mid-cycle demote was '
                               'refused: ' + str(body))
        restored = False
        promoted = None
        deadline = time.monotonic() + ROTATION_SWITCH_DEADLINE
        while time.monotonic() < deadline and promoted is None:
            try:
                status, body = http_json('POST', peer_base + '/promote')
                if status == 200:
                    promoted = body
                else:
                    time.sleep(ROTATION_POLL)
            except urllib.error.HTTPError as exc:
                if exc.code == 409:
                    time.sleep(ROTATION_POLL)
                else:
                    raise
        settled_role = wait_for(
            lambda: (r.get('role') == 'active' and r or None)
            if (r := _role(ctx, peer_base)) else None,
            time.monotonic() + ROTATION_SWITCH_DEADLINE,
            interval=ROTATION_POLL)
        ref = save_evidence(ctx['evidence_dir'],
                            'duty-rotation-promotion.json',
                            {'demoted': active, 'promote': promoted,
                             'role': settled_role})
        case.evidence('file', ref, 'the mid-cycle demote/promote '
                      'responses')
        if promoted is None or not settled_role:
            return case.finish('failed', 'the mid-cycle promotion '
                               'never settled')
        live = peer_base
        case.observe(peer + ' promoted mid-cycle; checking the '
                     'carried rotation position')

        carried_state = wait_for(served_state,
                                 time.monotonic() + ROTATION_DEADLINE,
                                 interval=ROTATION_POLL)
        carried = observed.get('checkpoint') or {}
        component = observed.get('component')
        if carried_state is None:
            return case.finish('inconclusive', 'the promoted peer '
                               'serves no checkpointed pump-group '
                               'state')
        ref = save_evidence(ctx['evidence_dir'],
                            'duty-rotation-carried.json',
                            {'component': component,
                             'tick': carried.get('tick'),
                             'state': carried_state})
        case.evidence('file', ref, 'the promoted peer\'s checkpointed '
                      'pump-group state')
        unmet = []
        if _state_value(carried_state, 'duty') != duty2:
            unmet.append('duty reset to ' + json.dumps(
                _state_value(carried_state, 'duty')) + ' — the '
                'checkpoint carried ' + str(duty2))
        if _state_value(carried_state, 'rotation_cursor') \
                != carried_cursor:
            unmet.append('rotation_cursor '
                         + json.dumps(_state_value(
                             carried_state, 'rotation_cursor'))
                         + ' != the carried '
                         + json.dumps(carried_cursor))
        if _state_value(carried_state, 'commanded_' + str(duty2)) \
                is not True:
            unmet.append('the in-flight command on pump '
                         + str(duty2) + ' did not carry')
        if not isinstance(_state_value(carried_state,
                                       'run_hours_' + str(duty1)),
                          int) \
                or _state_value(carried_state,
                                'run_hours_' + str(duty1)) <= 0:
            unmet.append('the banked run-hours for pump '
                         + str(duty1) + ' did not carry')
        if unmet:
            return case.finish('failed', 'rotation-failed: the '
                               'promoted peer lost the rotation '
                               'position: ' + '; '.join(unmet))
        case.observe('the promoted peer names duty='
                     + str(_state_value(carried_state, 'duty'))
                     + ' rotation_cursor='
                     + str(_state_value(carried_state,
                                        'rotation_cursor'))
                     + ' — the rotation position carried')

        # Run-hours keep accruing on the promoted peer: two captures
        # bracket the resumed drain.
        hours0 = _state_value(carried_state, 'run_hours_' + str(duty2))

        def accruing():
            checkpoint = _checkpoint_at(ctx, live)
            if checkpoint is None:
                return None
            _, later = _rotation_component(checkpoint)
            observed['later'] = later
            value = _state_value(later, 'run_hours_' + str(duty2))
            return later if isinstance(value, int) \
                and value > (hours0 or 0) else None

        grown_hours = wait_for(accruing,
                               time.monotonic()
                               + ROTATION_CYCLE_DEADLINE,
                               interval=ROTATION_POLL)
        ref = save_evidence(ctx['evidence_dir'],
                            'duty-rotation-runhours.json',
                            {'pump': duty2, 'at_promotion': hours0,
                             'later': grown_hours or {}})
        case.evidence('file', ref, 'run-hours accruing on the '
                      'promoted peer')
        if not grown_hours:
            return case.finish('failed', 'rotation-failed: run-hours '
                               'for pump ' + str(duty2)
                               + ' never accrued past '
                               + json.dumps(hours0)
                               + ' on the promoted peer')
        case.observe('run_hours_' + str(duty2) + ': '
                     + str(hours0) + ' -> '
                     + str(_state_value(grown_hours,
                                        'run_hours_' + str(duty2))))

        # The second cycle completes on the promoted peer: the holder
        # stops at `stop` and duty rotates back.
        def ended_two(snap):
            return _point_value(snap, points['staged']) == 0 \
                and _point_value(snap, cmd_point(duty2)) is not True \
                and _point_value(snap, points['duty']) == duty1

        probe, last = watch(ended_two)
        second_end = wait_for(probe,
                              time.monotonic()
                              + ROTATION_CYCLE_DEADLINE,
                              interval=ROTATION_POLL)
        ref = save_evidence(ctx['evidence_dir'],
                            'duty-rotation-cycle2-end.json',
                            second_end or last.get('snap') or {})
        case.evidence('file', ref, 'the second cycle\'s stop and '
                      'return rotation')
        if not second_end:
            return case.finish('failed', 'rotation-failed: the second '
                               'cycle never ended or duty never '
                               'rotated back to pump ' + str(duty1))
        case.observe('second cycle ended: duty rotated back to '
                     + str(duty1))

        # Restore: the none-available annunciation the hold latched is
        # acknowledged through the receipted path, then the pair fails
        # back to its entry role assignment.
        snap = _try_snapshot(ctx, live) or {}
        if _point_value(snap, points['unack']) is True:
            if not write(live, points['ack'], True):
                return case.finish('failed', 'the none-available ack '
                                   'write was refused: '
                                   + json.dumps(submitted[-1])[:300])
            ack_held = points['ack']

            def cleared(snap_):
                return _point_value(snap_, points['unack']) is not True

            probe, last = watch(cleared)
            if not wait_for(probe,
                            time.monotonic() + ROTATION_DEADLINE,
                            interval=ROTATION_POLL):
                return case.finish('failed', 'the none-available '
                                   'unacknowledged latch never '
                                   'cleared under the ack')
            if not write(live, points['ack'], False):
                return case.finish('failed', 'the ack restore write '
                                   'was refused: '
                                   + json.dumps(submitted[-1])[:300])
            ack_held = None
            case.observe('the hold\'s none-available annunciation '
                         'acknowledged and restored')

        status, body = http_json('POST', live + '/demote')
        case.observe('restore demote ' + peer + ': ' + str(status)
                     + ' ' + json.dumps(body))
        if status != 200:
            return case.finish('failed', 'the restore demote was '
                               'refused: ' + str(body))
        restored_promote = None
        deadline = time.monotonic() + ROTATION_SWITCH_DEADLINE
        while time.monotonic() < deadline and restored_promote is None:
            try:
                status, body = http_json('POST', base + '/promote')
                if status == 200:
                    restored_promote = body
                else:
                    time.sleep(ROTATION_POLL)
            except urllib.error.HTTPError as exc:
                if exc.code == 409:
                    time.sleep(ROTATION_POLL)
                else:
                    raise
        settled_back = wait_for(
            lambda: (r.get('role') == 'active' and r or None)
            if (r := _role(ctx, base)) else None,
            time.monotonic() + ROTATION_SWITCH_DEADLINE,
            interval=ROTATION_POLL)
        peer_back = wait_for(
            lambda: (r.get('role') == 'standby'
                     and 'tracking' in (r.get('sync') or {})
                     and r or None)
            if (r := _role(ctx, peer_base)) else None,
            time.monotonic() + ROTATION_SWITCH_DEADLINE,
            interval=ROTATION_POLL)
        ref = save_evidence(ctx['evidence_dir'],
                            'duty-rotation-restored.json',
                            {'demoted': peer,
                             'promote': restored_promote,
                             'role': settled_back, 'peer': peer_back})
        case.evidence('file', ref, 'the fail-back responses and the '
                      'restored roles')
        if restored_promote is None or not settled_back \
                or not peer_back:
            return case.finish('failed', 'the pair is not restored '
                               'to its pre-scenario role assignment')
        restored = True
        live = base
        case.observe('restored: ' + active + ' active again, ' + peer
                     + ' tracking')

        # The journaled audit: every commanded transition settled
        # through the receipted path, and the declared-journaled
        # points recorded the hold, the stops, and the switches.
        audited = {}
        for key, address in ((active, base), (peer, peer_base)):
            try:
                _, journal = http_json('GET', address
                                       + '/journal?since='
                                       + str(floors[key]))
            except Exception:
                journal = []
            audited[key] = journal
            ref = save_evidence(ctx['evidence_dir'],
                                'duty-rotation-journal-' + key
                                + '.json', journal)
            case.evidence('file', ref, 'the ' + key + ' peer\'s '
                          'journal since the leg opened')
        missing = []
        settled = {key: _settled_receipts(journal)
                   for key, journal in audited.items()}
        for item in submitted:
            key = active if item['base'] == base else peer
            receipt = next(
                (entry for entry in settled[key]
                 if entry.get('command') == item['command']), None)
            if receipt is None:
                missing.append('no settled receipt journaled for '
                               + json.dumps(item['command'])[:200])
            elif receipt.get('actor') != ROTATION_ACTOR:
                missing.append('a settled receipt lost its actor: '
                               + json.dumps(receipt)[:200])
            elif 'applied' not in (receipt.get('outcome') or {}):
                missing.append('a settled receipt did not apply: '
                               + json.dumps(receipt)[:200])
        active_changes = audited[active]
        for point in (points['oos1'], points['oos2']):
            changes = _point_changes(active_changes, point)
            if {'bool': True} not in [to for _, to in changes] \
                    or {'bool': False} not in [to for _, to in changes]:
                missing.append('the oos hold/release on point '
                               + str(point) + ' never journaled')
        if {'bool': True} not in [to for _, to in _point_changes(
                active_changes, points['none_available'])]:
            missing.append('the none-available assertion never '
                           'journaled')
        if {'bool': False} not in [to for _, to in _point_changes(
                active_changes, run_point(duty1))]:
            missing.append('the first holder\'s run stop never '
                           'journaled')
        role_marks = [(entry.get('event') or {}).get('role_changed')
                      for entry in _journal_list(active_changes)]
        if not any(mark for mark in role_marks):
            missing.append('the demotion never journaled on the '
                           'active peer')
        peer_changes = audited[peer]
        peer_marks = [(entry.get('event') or {}).get('role_changed')
                      for entry in _journal_list(peer_changes)]
        if not any(mark for mark in peer_marks):
            missing.append('the promotion never journaled on the '
                           'promoted peer')
        if {'bool': False} not in [to for _, to in _point_changes(
                peer_changes, run_point(duty2))]:
            missing.append('the second holder\'s run stop never '
                           'journaled on the promoted peer')
        if missing:
            return case.finish('failed', 'journaled evidence missing: '
                               + '; '.join(missing))
        case.observe('journaled: oos holds, run stops, role changes, '
                     'and every settled receipt across both peers')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
    finally:
        # The held points are the run's shared field and the roles the
        # pair's standing assignment: a case that leaves either
        # standing poisons every later leg.
        live_base = None
        try:
            settled = _settled_active(ctx)
            if settled is not None:
                live_base = ctx[settled]
        except Exception:
            pass
        for point in held:
            try:
                if live_base is not None:
                    http_json('POST', live_base + '/command',
                              {'command': {'write_value': {
                                  'point': point, 'kind': 'bool',
                                  'value': {'bool': False}}},
                               'actor': ROTATION_ACTOR})
            except Exception:
                pass
        if ack_held is not None and live_base is not None:
            try:
                http_json('POST', live_base + '/command',
                          {'command': {'write_value': {
                              'point': ack_held, 'kind': 'bool',
                              'value': {'bool': False}}},
                           'actor': ROTATION_ACTOR})
            except Exception:
                pass
        if not restored:
            try:
                if live_base is not None and live_base != base:
                    http_json('POST', live_base + '/demote')
                http_json('POST', base + '/promote')
            except Exception:
                pass


# --------------------------------------------------------------------
# The plant's single-writer field claim (the failover decision's
# fencing half, exercised standing): with the settled pair's active
# holding the write-ownership claim it took at launch, a third sim-net
# attachment must find the field closed — writes and steps refused
# with the named fencing failure — while the owner's own writes keep
# landing. The claim lifecycle then answers per contract:
# `ensure_writer` under a foreign token fences (the conditional grant
# never preempts a live owner), under the standing owner's token
# answers `claimed_shared` and writes under the shared claim, and
# `release_writer` drops only the caller's hold — harmless `done` for
# a holder of nothing. A rogue `claim_writer` preempts — the grant is
# unconditional — but never silently and never fatally: the superseded
# owner's journal records `field_claim_lost` and the peer demotes
# rather than dying, after which the scenario restores the rig by
# re-promoting it. The post-restart `unclaimed` window is the
# link-loss case's leg; the fenced writer's degrade details are the
# fenced-writer-degrade case's — this case asserts the standing-claim
# side only.
#
# The shared-claim leg needs the standing owner's token: the rig pins
# each peer's --owner-token and ctx['plant_owner'] carries them keyed
# by endpoint name — the harness-sharing case the flag exists for.

CLAIM_DEADLINE = 30      # bound on the demote watch and the restore
CLAIM_FOREIGN = 0xF00D   # a token no rig peer or tooling client claims
CLAIM_ROGUE = 0xF00E     # the rogue claim's token — likewise never an owner


def _mutation_fenced(response):
    """Whether a field-mutation answer is the named fencing refusal —
    `write` carries the point's IoError::Fenced under the `io` kind,
    `step` and the claim requests the plant-level `fenced` kind."""
    error = (response or {}).get('error') or {}
    if error.get('kind') == 'fenced':
        return True
    inner = error.get('error')
    return error.get('kind') == 'io' and isinstance(inner, dict) \
        and 'fenced' in inner


def _journal_has_claim_loss(entries):
    """Whether a `GET /journal` answer recorded the preemption the
    superseded owner must journal — one `field_claim_lost` entry per
    held claim."""
    for entry in entries:
        if 'field_claim_lost' in (entry.get('event') or {}):
            return True
    return False


def scenario_field_claim(ctx):
    """A third sim-net attachment meets the standing field claim: its
    writes and steps fence, the claim lifecycle answers per contract,
    and a rogue claim preempts through the journaled fencing-loss path
    — degrade, never death — after which the pair is restored."""
    case = Case('field-claim',
                'The single-writer claim fences third attachments',
                'with the settled active holding the field claim it '
                'took at launch, a third sim-net attachment\'s write '
                'and step are refused with the named fencing failure '
                'while the active keeps writing; ensure_writer under '
                'a foreign token is refused fenced, under the owner\'s '
                'token answers claimed_shared and writes under the '
                'shared claim; release_writer drops only the caller\'s '
                'hold and answers done for a holder of nothing; and a '
                'rogue claim_writer preempts with field_claim_lost '
                'journaled — the superseded owner demotes instead of '
                'dying — before the scenario restores the pair')
    stream = None
    try:
        if ctx.get('plant') is None:
            return case.finish('inconclusive',
                               'the run publishes no plant endpoint')
        tokens = ctx.get('plant_owner') or {}
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + CLAIM_DEADLINE)
        if active is None:
            reachable = any(
                _try_role(ctx, ctx[name]) is not None
                for name in ('active', 'standby') if ctx.get(name))
            return case.finish(
                'failed' if reachable else 'inconclusive',
                'no peer reports role=active' if reachable
                else 'the pair is unreachable')
        owner = tokens.get(active)
        if owner is None:
            return case.finish('inconclusive', 'the run pins no '
                               'plant-writer owner token for the '
                               'settled active ' + str(active))
        peer = 'standby' if active == 'active' else 'active'
        base, peer_base = ctx[active], ctx[peer]
        stream = _plant_connect(ctx)
        case.observe('field owner: ' + active + ' (' + base + '); '
                     'third sim-net attachment on the run\'s plant '
                     'endpoint')

        # The baseline: a field input whose own stored value the
        # probes write back — an idempotent mutation that only the
        # fencing verdict distinguishes — and the two named refusals.
        # The census rides the shipped tool (list needs no claim); the
        # probes and the whole claim lifecycle stay on the raw
        # attachment — the ops the tool does not expose.
        inputs = _field_inputs(ctx)
        target = None
        for point, info in sorted(inputs.items()):
            if (info.get('sample') or {}).get('value') is not None:
                target = (point, info['sample']['value'])
                break
        before = _snapshot(ctx, base)
        step_probe = _plant_request(stream, {'op': 'step', 'dt': 0})
        write_probe = (None if target is None else _plant_request(
            stream, {'op': 'write', 'point': target[0],
                     'value': target[1]}))
        ref = save_evidence(ctx['evidence_dir'],
                            'field-claim-probes.json',
                            {'target': target, 'step': step_probe,
                             'write': write_probe})
        case.evidence('file', ref, 'the third attachment\'s refused '
                      'mutations')
        if target is None:
            return case.finish('inconclusive', 'the plant census '
                               'lists no field input with a stored '
                               'value to write')
        point, value = target
        if not _mutation_fenced(step_probe):
            return case.finish('failed', 'a third attachment\'s step '
                               'was not refused fenced — the field '
                               'held no enforceable writer claim: '
                               + json.dumps(step_probe)[:300])
        if not _mutation_fenced(write_probe):
            return case.finish('failed', 'a third attachment\'s '
                               'write was not refused fenced: '
                               + json.dumps(write_probe)[:300])
        case.observe('point ' + str(point) + ': step and write '
                     'refused ' + json.dumps(step_probe.get('error'))
                     + ' / ' + json.dumps(write_probe.get('error')))

        # The owner undisturbed: scans keep landing while the fenced
        # attachment's mutations bounce — the tick advances, the role
        # holds, and io_health records no new field failure.
        grown = wait_for(
            lambda: (s.get('tick', 0) > (before or {}).get('tick', 0)
                     and s or None)
            if (s := _try_snapshot(ctx, base)) else None,
            time.monotonic() + CLAIM_DEADLINE)
        health0 = (before or {}).get('io_health') or {}
        health1 = (grown or {}).get('io_health') or {}
        ref = save_evidence(ctx['evidence_dir'],
                            'field-claim-owner.json',
                            {'before': {'tick': (before or {})
                                        .get('tick'),
                                        'io_health': health0},
                             'after': {'tick': (grown or {})
                                       .get('tick'),
                                       'io_health': health1}})
        case.evidence('file', ref, 'the owner\'s scans across the '
                      'fenced probes')
        if grown is None:
            return case.finish('failed', 'the active\'s scans '
                               'stalled under a third attachment\'s '
                               'fenced probes')
        if _settled_active(ctx) != active:
            return case.finish('failed', 'a fenced attachment\'s '
                               'probes moved the active role')
        for key in ('failed_reads', 'failed_writes'):
            if (health1.get(key) or 0) > (health0.get(key) or 0):
                return case.finish('failed', 'the fenced probes '
                                   'counted against the owner\'s '
                                   'io_health: '
                                   + json.dumps(health1)[:300])
        case.observe('the active kept scanning — tick '
                     + str((before or {}).get('tick')) + ' -> '
                     + str(grown.get('tick')))

        # The claim lifecycle: a foreign token's conditional grant
        # fences — ensure_writer never preempts a standing owner —
        # the owner's own token answers claimed_shared and writes
        # under the shared claim, and release_writer drops only the
        # caller's hold.
        foreign = _plant_request(
            stream, {'op': 'ensure_writer', 'owner': CLAIM_FOREIGN})
        shared = _plant_request(
            stream, {'op': 'ensure_writer', 'owner': owner})
        grant_write = _plant_request(
            stream, {'op': 'write', 'point': point, 'value': value})
        release = _plant_request(stream, {'op': 'release_writer'})
        own_write = _plant_request(
            stream, {'op': 'write', 'point': point, 'value': value})
        standing = _plant_probe(ctx, {'op': 'step', 'dt': 0})
        idle = _plant_request(stream, {'op': 'release_writer'})
        still = _plant_probe(ctx, {'op': 'step', 'dt': 0})
        ref = save_evidence(ctx['evidence_dir'],
                            'field-claim-lifecycle.json',
                            {'foreign': foreign, 'shared': shared,
                             'grant_write': grant_write,
                             'release': release,
                             'own_write': own_write,
                             'standing': standing, 'idle': idle,
                             'still': still})
        case.evidence('file', ref, 'the conditional-grant, '
                      'shared-claim, and release answers')
        if not _fenced(foreign):
            return case.finish('failed', 'ensure_writer under a '
                               'foreign token was not refused fenced '
                               '— the conditional grant preempted: '
                               + json.dumps(foreign)[:300])
        if shared.get('result') != 'claimed_shared' \
                or shared.get('owner') != owner:
            return case.finish('failed', 'ensure_writer under the '
                               'owner\'s token did not answer '
                               'claimed_shared: '
                               + json.dumps(shared)[:300])
        if grant_write.get('result') != 'done':
            return case.finish('failed', 'a write under the shared '
                               'claim was refused: '
                               + json.dumps(grant_write)[:300])
        if release.get('result') != 'done':
            return case.finish('failed', 'release_writer refused: '
                               + json.dumps(release)[:300])
        if not _mutation_fenced(own_write):
            return case.finish('failed', 'a released attachment\'s '
                               'write was not fenced back out — the '
                               'release dropped more than the '
                               'caller\'s hold: '
                               + json.dumps(own_write)[:300])
        if not _fenced(standing):
            return case.finish('failed', 'the caller\'s release '
                               'dropped the owner\'s claim — probes '
                               'answered '
                               + json.dumps(standing)[:300])
        if idle.get('result') != 'done':
            return case.finish('failed', 'release_writer from a '
                               'holder of nothing did not answer '
                               'done: ' + json.dumps(idle)[:300])
        if not _fenced(still):
            return case.finish('failed', 'a harmless release changed '
                               'the field: probes answered '
                               + json.dumps(still)[:300])
        case.observe('foreign ensure fenced; owner ensure answered '
                     'claimed_shared and wrote; release dropped only '
                     'the caller\'s hold')

        # The rogue claim: the grant is unconditional, so the attempt
        # preempts — but never silently and never fatally. The
        # declared evidence is the superseded owner's journaled
        # field_claim_lost and its demotion to standby while the
        # monitor keeps serving — the peer degrades, it does not die.
        # A refused attempt is the alternative the contract admits:
        # the standing claim and the owner untouched.
        cursor = _journal_cursor(ctx, base)
        rogue = _plant_request(
            stream, {'op': 'claim_writer', 'owner': CLAIM_ROGUE})
        ref = save_evidence(ctx['evidence_dir'],
                            'field-claim-rogue.json',
                            {'claim': rogue, 'cursor': cursor})
        case.evidence('file', ref, 'the rogue claim answer')
        if rogue.get('result') == 'error':
            probe = _plant_probe(ctx, {'op': 'step', 'dt': 0})
            settled = _settled_active(ctx)
            ref = save_evidence(ctx['evidence_dir'],
                                'field-claim-refused.json',
                                {'probe': probe, 'active': settled})
            case.evidence('file', ref, 'the field and the owner '
                          'after the refused claim')
            if not _fenced(probe):
                return case.finish('failed', 'the refused rogue '
                                   'claim still dropped the standing '
                                   'claim: ' + json.dumps(probe)[:300])
            if settled != active:
                return case.finish('failed', 'a refused rogue claim '
                                   'moved the active role')
            case.observe('the rogue claim was refused '
                         + json.dumps(rogue.get('error'))
                         + ' — the owner and the field undisturbed')
            return case.finish('passed')
        if rogue.get('result') == 'claimed_shared':
            return case.finish('inconclusive', 'the rogue claim '
                               'joined a standing claim under its own '
                               'token — the pinned tokens are not '
                               'unique: ' + json.dumps(rogue)[:300])
        if rogue.get('result') != 'done':
            return case.finish('inconclusive', 'the rogue claim '
                               'answered off-contract: '
                               + json.dumps(rogue)[:300])

        # Preempted: the claim now belongs to the rogue token. The
        # superseded owner's first fenced write must journal
        # field_claim_lost and demote the peer in place — the monitor
        # answering throughout, because a degrade is not a death.
        watch = {'answered': 0, 'roles': []}
        deadline = time.monotonic() + CLAIM_DEADLINE
        settled = None
        while time.monotonic() < deadline:
            report = _try_role(ctx, base)
            if report is not None:
                watch['answered'] += 1
                watch['roles'].append(report.get('role'))
                if report.get('role') == 'standby':
                    settled = report
                    break
            time.sleep(0.1)
        # Whether the monitor still answers at all — the superseded
        # owner degrading is the contract; it going silent is a kill.
        alive = settled is not None
        if not alive:
            report = wait_for(lambda: _try_role(ctx, base),
                              time.monotonic() + 4)
            alive = report is not None
            if (report or {}).get('role') == 'standby':
                settled = report
        journal = None
        if alive:
            try:
                _, body = http_json('GET', base + '/journal?since='
                                    + str(cursor))
                journal = _journal_list(body)
            except Exception:
                pass
        ref = save_evidence(ctx['evidence_dir'],
                            'field-claim-superseded.json',
                            {'watch': watch, 'settled': settled,
                             'alive': alive, 'journal': journal})
        case.evidence('file', ref, 'the superseded owner\'s demotion '
                      'and journal')
        if not alive:
            return case.finish('failed', 'the rogue claim killed the '
                               'active — its monitor stopped '
                               'answering')
        if journal is None:
            return case.finish('inconclusive', 'the superseded '
                               'owner\'s journal never answered')
        if settled is None:
            return case.finish('failed', 'the superseded owner never '
                               'demoted — the claim moved but the '
                               'role stayed '
                               + json.dumps(watch['roles'][-3:]))
        if not _journal_has_claim_loss(journal):
            return case.finish('failed', 'the preemption was silent '
                               '— the superseded owner\'s journal '
                               'recorded no field_claim_lost')
        case.observe('the superseded owner demoted to standby with '
                     'field_claim_lost journaled — degrade, not death')

        # Restore: re-promote the demoted peer — its promotion claim
        # preempts the rogue token, so the field stays claimed
        # throughout — then prove the pair settled back onto the same
        # field owner with probes fenced again.
        promoted = None
        deadline = time.monotonic() + CLAIM_DEADLINE
        while time.monotonic() < deadline and promoted is None:
            try:
                status, body = http_json('POST', base + '/promote')
                if status == 200:
                    promoted = body
                else:
                    time.sleep(POLL_INTERVAL)
            except urllib.error.HTTPError as exc:
                if exc.code == 409:
                    time.sleep(POLL_INTERVAL)
                else:
                    raise
        ref = save_evidence(ctx['evidence_dir'],
                            'field-claim-promote.json',
                            promoted or {'refused': True})
        case.evidence('file', ref, 'the restore promotion')
        if promoted is None:
            return case.finish('failed', 'the demoted owner never '
                               're-promoted — the rig was left '
                               'without a field writer')
        restored = wait_for(
            lambda: _settled_active(ctx) == active
            and _try_role(ctx, base),
            time.monotonic() + CLAIM_DEADLINE)
        snap0 = _try_snapshot(ctx, base)
        probe = _plant_probe(ctx, {'op': 'step', 'dt': 0})
        grown = wait_for(
            lambda: (s.get('tick', 0)
                     > (snap0 or {}).get('tick', 0) and s or None)
            if (s := _try_snapshot(ctx, base)) else None,
            time.monotonic() + CLAIM_DEADLINE)
        peer_role = _try_role(ctx, peer_base)
        ref = save_evidence(ctx['evidence_dir'],
                            'field-claim-restored.json',
                            {'role': restored, 'probe': probe,
                             'peer': peer_role,
                             'snapshot': grown or snap0})
        case.evidence('file', ref, 'the restored pair')
        if not restored:
            return case.finish('failed', 'the re-promoted peer '
                               'never settled active')
        if not _fenced(probe):
            return case.finish('failed', 'the restored claim does '
                               'not fence probes: '
                               + json.dumps(probe)[:300])
        if grown is None:
            return case.finish('failed', 'the restored active is '
                               'not scanning')
        if (peer_role or {}).get('role') != 'standby':
            return case.finish('failed', 'the peer did not return '
                               'to standby: '
                               + json.dumps(peer_role)[:300])
        case.observe('restored: ' + active + ' active and scanning, '
                     'probes fenced, ' + peer + ' standby')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
    finally:
        if stream is not None:
            # Detach cleanly: release whatever hold this attachment
            # still carries — a hold left standing would keep the
            # field claimed for a dead token — then close. The
            # release drops only this connection's hold, so it can
            # never take the owner's claim down with it.
            try:
                _plant_request(stream, {'op': 'release_writer'})
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass


# --------------------------------------------------------------------
# The settled fenced-writer degrade contract — QA finding #508's
# supersession pinned as per-revision lane evidence for WW-LCM-001's
# continuity clause and decision 28's single-writer fencing: the
# documented switch order is demote-then-promote, and a promote posted
# to the tracking standby while the active still owns the field is the
# misorder the finding reproduces. The promotion's claim preempts the
# standing owner unconditionally, so the old active's next write meets
# the fence — and where the pre-fix process exited on that verdict
# (docker restart crash-looping into the same wall, the supersession
# made permanent), the settled contract demotes it in place at that
# first fenced write: the gate re-closes, the reported role walks
# demoting to standby, field_claim_lost and the role changes journal,
# its monitor keeps serving, and receipted commands posted to it
# answer the named not_active refusal — #522's admission-time refusal,
# never a write that fails on the field. The promoted peer's writes
# land undisturbed throughout. The restart-of-the-superseded-peer
# takeover leg is the superseded-restart case's; this case covers the
# demote-in-place leg only and restores the pair's roles behind it.

FENCED_DEGRADE_WATCH = 0.05    # cadence polling the superseded peer mid-demotion
FENCED_DEGRADE_DEADLINE = 30   # bound on the demote-in-place settle
FENCED_DEGRADE_JOURNAL = 15    # bound on the durable journal landing the records
FENCED_DEGRADE_SETTLE = 60     # bound on reconvergence and role settles
FENCED_DEGRADE_POLL = 0.5      # cadence on the tracking and restore waits


def _journal_file_events(path):
    """Every event body a `--journal-file` records, in append order."""
    return [(item.get('entry') or {}).get('event')
            for item in _journal_entries(path)
            if isinstance(item.get('entry'), dict)]


def _journal_file_runs(path):
    """The run-boundary markers a `--journal-file` carries — one per
    process lifetime the file records: a count that grows is a restart
    on the durable record."""
    return [item['run_boundary'] for item in _journal_entries(path)
            if 'run_boundary' in item]


def _role_walk(events):
    """The (from, to) transitions a journal event list's role_changed
    entries carry, in record order."""
    walk = []
    for event in events:
        change = (event or {}).get('role_changed')
        if isinstance(change, dict):
            walk.append((change.get('from'), change.get('to')))
    return walk


def _walked_down(walk):
    """Whether a role walk demotes in place: active->demoting followed
    by demoting->standby — the transitions the fenced owner's first
    refused write must record."""
    if ('active', 'demoting') not in walk:
        return False
    return ('demoting', 'standby') \
        in walk[walk.index(('active', 'demoting')) + 1:]


def scenario_fenced_writer_degrade(ctx):
    """Drive the misordered promote — POST /promote on the tracking
    standby while the active still owns the field — and assert the
    demote-in-place contract on the superseded peer: the reported role
    walks demoting to standby with field_claim_lost and the role
    changes on the durable journal, its monitor keeps serving and
    answers commands with the named not_active refusal, and the
    promoted peer's writes land undisturbed — then restore the pair."""
    case = Case('fenced-writer-degrade',
                'A misordered promote demotes the fenced writer in '
                'place',
                'with the pair settled and tracking, POST /promote on '
                'the standby while the active still runs preempts the '
                'field claim; the superseded peer\'s first fenced '
                'write demotes it in place — the reported role walking '
                'demoting then standby, field_claim_lost and the role '
                'changes on the durable journal, the monitor serving '
                'throughout, commands answered not_active — while the '
                'promoted peer\'s writes land undisturbed and no '
                'process exits; the documented demote/promote order '
                'then restores the pair')
    stream = None
    state = {'switched': False}
    restore = None
    try:
        if ctx.get('plant') is None:
            return case.finish('inconclusive',
                               'the run publishes no plant endpoint')
        tokens = ctx.get('plant_owner') or {}
        journals = ctx.get('journal_files') or {}
        active = wait_for(lambda: _pair_active(ctx),
                          time.monotonic() + FENCED_DEGRADE_DEADLINE)
        if active is None:
            reachable = any(
                _try_role(ctx, ctx[name]) is not None
                for name in ('active', 'standby') if ctx.get(name))
            return case.finish(
                'failed' if reachable else 'inconclusive',
                'no pair peer reports role=active' if reachable
                else 'the pair is unreachable')
        peer = 'standby' if active == 'active' else 'active'
        base, peer_base = ctx[active], ctx[peer]
        owner, rival = tokens.get(active), tokens.get(peer)
        if owner is None or rival is None:
            return case.finish('inconclusive', 'the run pins no '
                               'plant-writer owner tokens for the pair')
        journal = journals.get(active)
        peer_journal = journals.get(peer)
        if journal is None or peer_journal is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no journal-file paths for '
                               'the pair')
        case.observe('field owner: ' + active + ' (' + base + '); '
                     'misordered promote on ' + peer + ' ('
                     + peer_base + ')')

        # The promote target must be the tracking standby the finding
        # reproduces against — an unconverged peer would only meet the
        # promote gate's not_converged, never the claim preemption.
        if wait_for(lambda: _tracking_standby(ctx, peer),
                    time.monotonic() + FENCED_DEGRADE_SETTLE,
                    interval=FENCED_DEGRADE_POLL) is None:
            return case.finish('inconclusive', 'the ' + peer
                               + ' peer is not a tracking standby — '
                               'the misordered promote has no target')

        # The baselines every leg diffs against: both journals'
        # lifetimes and the superseded peer's entry count, the command
        # target and its standing value, both peers' io_health, and
        # the field's fencing probe — the standing claim the misorder
        # preempts must be visible before it runs.
        try:
            bounds0 = _journal_file_runs(journal)
            peer_bounds0 = _journal_file_runs(peer_journal)
            entries0 = _journal_file_events(journal)
        except (OSError, ValueError) as exc:
            return case.finish('inconclusive', 'a pair journal file '
                               'is unreadable: ' + str(exc)[:300])
        _, signals = http_json('GET', base + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'fenced-degrade-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming writable points')
        target = _writable_bool_point(signals)
        if target is None:
            return case.finish('inconclusive',
                               'no writable bool point in the model')
        point = target['point']
        baseline = _point_value(_snapshot(ctx, base), point)
        if not isinstance(baseline, bool):
            return case.finish('inconclusive', 'point ' + str(point)
                               + ' serves no bool baseline to write '
                               'against')
        peer_snap0 = _snapshot(ctx, peer_base)
        peer_health0 = peer_snap0.get('io_health') or {}
        health0 = (_snapshot(ctx, base)).get('io_health') or {}
        probe0 = _plant_probe(ctx, {'op': 'step', 'dt': 0})
        if not _fenced(probe0):
            return case.finish('inconclusive', 'the field held no '
                               'writer claim to preempt — the '
                               'induction\'s standing claim was never '
                               'there: ' + json.dumps(probe0)[:300])

        # The misorder: promote with no demote first. The promotion's
        # claim preempts unconditionally — the standing owner's next
        # write meets the fence.
        status, promoted = _settle_call(peer_base + '/promote')
        ref = save_evidence(ctx['evidence_dir'],
                            'fenced-degrade-promote.json',
                            {'status': status, 'body': promoted})
        case.evidence('file', ref, 'the misordered promote\'s answer')
        if status != 200:
            return case.finish('failed', 'the misordered promote on a '
                               'tracking standby answered '
                               + str(status) + ': '
                               + json.dumps(promoted)[:300])
        state['switched'] = True
        case.observe('promoted ' + peer + ' while ' + active
                     + ' still ran: ' + json.dumps(promoted)[:200])

        def restore_roles():
            """The documented demote/promote order putting the entry
            roles back: the demoted writer reconverges tracking behind
            its successor, the successor demotes, and the tracking
            writer re-promotes. Returns the failure detail or None."""
            if wait_for(lambda: _tracking_standby(ctx, active),
                        time.monotonic() + FENCED_DEGRADE_SETTLE,
                        interval=FENCED_DEGRADE_POLL) is None:
                return 'the demoted field writer never reconverged ' \
                       'tracking — the restore has no promotable peer'
            status, body = _settle_call(peer_base + '/demote')
            if status != 200:
                return 'the restore demote answered ' + str(status) \
                       + ': ' + json.dumps(body)[:300]
            status, body = _settle_call(base + '/promote')
            if status != 200:
                return 'the restore promote answered ' + str(status) \
                       + ': ' + json.dumps(body)[:300]
            if wait_for(
                    lambda: (_pair_active(ctx) == active or None)
                    and _tracking_standby(ctx, peer),
                    time.monotonic() + FENCED_DEGRADE_SETTLE,
                    interval=FENCED_DEGRADE_POLL) is None:
                return 'the pair did not settle back to its ' \
                       'pre-scenario role assignment'
            return None

        restore = restore_roles

        # The demote-in-place watch: poll both monitors until the
        # superseded peer settles standby. Every answered poll is the
        # monitor-serving half of "degrade, never death"; the promoted
        # peer must hold promoting->active at every poll.
        watch = {'polls': 0, 'answered': 0, 'superseded': [],
                 'promoted': []}
        deadline = time.monotonic() + FENCED_DEGRADE_DEADLINE
        settled = None
        while time.monotonic() < deadline and settled is None:
            watch['polls'] += 1
            report = _try_role(ctx, base)
            if report is not None:
                watch['answered'] += 1
                watch['superseded'].append(report.get('role'))
                if report.get('role') == 'standby':
                    settled = report
            watch['promoted'].append(
                (_try_role(ctx, peer_base) or {}).get('role'))
            if settled is None:
                time.sleep(FENCED_DEGRADE_WATCH)
        ref = save_evidence(ctx['evidence_dir'],
                            'fenced-degrade-watch.json', watch)
        case.evidence('file', ref, 'the reported role walk across the '
                      'preemption')
        if watch['answered'] != watch['polls']:
            return case.finish('failed', 'the superseded peer\'s '
                               'monitor stopped answering mid-demotion '
                               '— the fenced writer exited instead of '
                               'degrading')
        if settled is None:
            return case.finish('failed', 'the superseded peer never '
                               'demoted — its reported role stayed '
                               + json.dumps(watch['superseded'][-3:]))
        if any(role not in ('active', 'demoting', 'standby')
               for role in watch['superseded']):
            return case.finish('failed', 'the superseded peer '
                               'reported an off-contract role '
                               'mid-demotion: '
                               + json.dumps(watch['superseded']))
        if 'demoting' not in watch['superseded']:
            return case.finish('failed', 'the superseded peer\'s '
                               'reported role never walked demoting: '
                               + json.dumps(watch['superseded']))
        if any(role not in ('promoting', 'active')
               for role in watch['promoted']) \
                or watch['promoted'][-1] != 'active':
            return case.finish('failed', 'the promoted peer\'s role '
                               'moved off the field across the '
                               'supersession: '
                               + json.dumps(watch['promoted']))
        case.observe('the superseded peer walked '
                     + ' -> '.join(watch['superseded'])
                     + ' with its monitor answering every poll; the '
                     'promoted peer settled active')

        # The durable record: the superseded peer's journal file must
        # carry the preemption — exactly one field_claim_lost — beside
        # the role walk, while its boundary count proves one process
        # lifetime: a restart would append a second marker.
        added = {}

        def journaled():
            try:
                events = _journal_file_events(journal)
            except (OSError, ValueError) as exc:
                added['error'] = str(exc)
                return None
            added['events'] = events[len(entries0):]
            added['walk'] = _role_walk(added['events'])
            added['losses'] = [e['field_claim_lost']
                               for e in added['events']
                               if isinstance(e, dict)
                               and 'field_claim_lost' in e]
            if _walked_down(added['walk']) and added['losses']:
                return added
            return None

        record = wait_for(journaled,
                          time.monotonic() + FENCED_DEGRADE_JOURNAL,
                          interval=FENCED_DEGRADE_POLL)
        bounds1 = _journal_file_runs(journal)
        ref = save_evidence(ctx['evidence_dir'],
                            'fenced-degrade-journal.json',
                            {'added': added, 'boundaries': bounds1})
        case.evidence('file', ref, 'the superseded peer\'s durable '
                      'journal through the demotion')
        if record is None:
            return case.finish('failed', 'the superseded peer\'s '
                               'durable journal never carried the '
                               'demotion contract — expected '
                               'field_claim_lost and role_changed '
                               'active->demoting->standby, found '
                               + json.dumps(added.get('events'))[:400])
        if len(added['losses']) != 1:
            return case.finish('failed', 'expected exactly one '
                               'field_claim_lost on the superseded '
                               'peer\'s journal, found '
                               + str(len(added['losses'])))
        if bounds1 != bounds0:
            return case.finish('failed', 'the superseded peer\'s '
                               'journal gained a run boundary — the '
                               'process restarted instead of '
                               'degrading in place')
        case.observe('field_claim_lost and role_changed '
                     'active->demoting->standby on the durable '
                     'journal; one process lifetime throughout')

        # The field's own account: the claim now belongs to the
        # promoted peer's token — an ensure under the superseded
        # owner's token fences (its writes no longer reach the field),
        # under the promoted owner's token answers claimed_shared and
        # writes land, and bare mutations stay fenced throughout. Both
        # peers' io_health records the boundary the same way: the
        # superseded peer's fenced write counted once, the promoted
        # peer's clean.
        stream = _plant_connect(ctx)
        superseded = _plant_request(stream, {'op': 'ensure_writer',
                                             'owner': owner})
        shared = _plant_request(stream, {'op': 'ensure_writer',
                                         'owner': rival})
        grant_write = _plant_request(
            stream, {'op': 'write', 'point': point,
                     'value': {'bool': baseline}})
        probe = _plant_probe(ctx, {'op': 'step', 'dt': 0})
        snap_old = _snapshot(ctx, base)
        snap_new = _snapshot(ctx, peer_base)
        ref = save_evidence(
            ctx['evidence_dir'], 'fenced-degrade-field.json',
            {'superseded_ensure': superseded, 'shared': shared,
             'grant_write': grant_write, 'probe': probe,
             'superseded_io_health': snap_old.get('io_health'),
             'promoted_io_health': snap_new.get('io_health'),
             'promoted_ticks': [peer_snap0.get('tick'),
                                snap_new.get('tick')]})
        case.evidence('file', ref, 'the claim arbitration and both '
                      'peers\' io_health after the supersession')
        if not _fenced(superseded):
            return case.finish('failed', 'an ensure under the '
                               'superseded owner\'s token was not '
                               'fenced — its writes still reach the '
                               'field: ' + json.dumps(superseded)[:300])
        if shared.get('result') != 'claimed_shared' \
                or shared.get('owner') != rival:
            return case.finish('failed', 'the promoted peer does not '
                               'hold the field claim: '
                               + json.dumps(shared)[:300])
        if grant_write.get('result') != 'done':
            return case.finish('failed', 'a write under the promoted '
                               'peer\'s shared claim was refused: '
                               + json.dumps(grant_write)[:300])
        if not _fenced(probe):
            return case.finish('failed', 'the field went unclaimed '
                               'across the supersession: '
                               + json.dumps(probe)[:300])
        old_health = snap_old.get('io_health') or {}
        new_health = snap_new.get('io_health') or {}
        if (old_health.get('failed_writes') or 0) \
                <= (health0.get('failed_writes') or 0):
            return case.finish('failed', 'the superseded peer\'s '
                               'fenced write was never counted on its '
                               'io_health: '
                               + json.dumps(old_health)[:300])
        if (new_health.get('failed_writes') or 0) \
                != (peer_health0.get('failed_writes') or 0):
            return case.finish('failed', 'the promoted peer\'s writes '
                               'met the fence — io_health degraded: '
                               + json.dumps(new_health)[:300])
        if not isinstance(snap_new.get('tick'), int) \
                or not isinstance(peer_snap0.get('tick'), int) \
                or snap_new['tick'] <= peer_snap0['tick']:
            return case.finish('failed', 'the promoted peer\'s scans '
                               'stalled across the supersession')
        case.observe('the claim moved to ' + peer + '\'s token: the '
                     'superseded owner fenced out, shared-claim '
                     'writes land, probes stay fenced, the promoted '
                     'peer\'s io_health clean')

        # The demoted peer's command path: the receipted answer is the
        # named not_active refusal at admission — never a write that
        # fails on the field — and the point stands unchanged in the
        # promoted peer's served snapshot.
        command = {'command': {'write_value': {
            'point': point, 'kind': 'bool',
            'value': {'bool': not baseline}}},
            'actor': 'qa-lane'}
        status, receipt = http_json('POST', base + '/command', command)
        served = _point_value(_snapshot(ctx, peer_base), point)
        ref = save_evidence(ctx['evidence_dir'],
                            'fenced-degrade-refusal.json',
                            {'command': command, 'status': status,
                             'receipt': receipt, 'served': served})
        case.evidence('file', ref, 'the demoted peer\'s command '
                      'answer and the point\'s served value')
        if status != 200 or _outcome_key(receipt) \
                != 'rejected:not_active':
            return case.finish('failed', 'the demoted peer\'s command '
                               'did not answer the named not_active '
                               'rejection at admission: ' + str(status)
                               + ' ' + json.dumps(receipt)[:300])
        if served != baseline:
            return case.finish('failed', 'the refused write reached '
                               'the field: point ' + str(point)
                               + ' serves ' + str(served))
        case.observe('the demoted peer answered the write '
                     'rejected:not_active; the point stands at '
                     + str(baseline))

        # The promoted writer undisturbed end to end: a receipted
        # command settles applied and its write lands on the field —
        # then a second restores the point's standing value.
        _, receipts0 = http_json('GET', peer_base + '/receipts')
        index = len(_receipt_list(receipts0))
        writes = []
        for value in (not baseline, baseline):
            write_command = {'command': {'write_value': {
                'point': point, 'kind': 'bool',
                'value': {'bool': value}}},
                'actor': 'qa-lane'}
            status, receipt = http_json('POST', peer_base + '/command',
                                        write_command)
            outcome = wait_for(
                lambda: _settled_outcome(ctx, peer_base, index),
                time.monotonic() + FENCED_DEGRADE_DEADLINE,
                interval=FENCED_DEGRADE_POLL)
            index += 1
            field = _field_sample(ctx, point)
            writes.append({'command': write_command, 'status': status,
                           'receipt': receipt, 'outcome': outcome,
                           'field': field})
            if status != 200 or outcome != 'applied':
                ref = save_evidence(ctx['evidence_dir'],
                                    'fenced-degrade-writes.json',
                                    writes)
                case.evidence('file', ref, 'the promoted peer\'s '
                              'command settlements')
                return case.finish('failed', 'the promoted peer\'s '
                                   'command did not settle applied: '
                                   + str(status) + ' '
                                   + json.dumps(receipt)[:300])
            if field is None:
                ref = save_evidence(ctx['evidence_dir'],
                                    'fenced-degrade-writes.json',
                                    writes)
                case.evidence('file', ref, 'the promoted peer\'s '
                              'command settlements')
                return case.finish('inconclusive', 'the field read '
                                   'behind the applied write never '
                                   'answered')
            if field.get('value') != {'bool': value}:
                ref = save_evidence(ctx['evidence_dir'],
                                    'fenced-degrade-writes.json',
                                    writes)
                case.evidence('file', ref, 'the promoted peer\'s '
                              'command settlements')
                return case.finish('failed', 'the promoted peer\'s '
                                   'applied write never reached the '
                                   'field: point ' + str(point)
                                   + ' reads '
                                   + json.dumps(field)[:300])
        ref = save_evidence(ctx['evidence_dir'],
                            'fenced-degrade-writes.json', writes)
        case.evidence('file', ref, 'the promoted peer\'s command '
                      'settlements and the field reads behind them')
        case.observe('the promoted peer\'s writes settle applied and '
                     'land on the field; the point is back at '
                     + str(baseline))

        # Restore: the documented demote/promote order returns the pair
        # to its entry roles for the cases behind this one — the final
        # evidence both journals still record a single lifetime.
        detail = restore_roles()
        bounds_a = _journal_file_runs(journal)
        bounds_b = _journal_file_runs(peer_journal)
        final = {'restore': detail,
                 'active': _try_role(ctx, base),
                 'peer': _try_role(ctx, peer_base),
                 'boundaries': [bounds_a, bounds_b],
                 'probe': _try_plant(ctx, {'op': 'step', 'dt': 0})}
        ref = save_evidence(ctx['evidence_dir'],
                            'fenced-degrade-restored.json', final)
        case.evidence('file', ref, 'the restored pair and the '
                      'lifetime counts')
        if detail is not None:
            return case.finish('failed', 'the pair was not restored '
                               'to its pre-scenario role assignment: '
                               + detail)
        if bounds_a != bounds0 or bounds_b != peer_bounds0:
            return case.finish('failed', 'a journal gained a run '
                               'boundary across the case — a peer '
                               'process restarted')
        if not _fenced(final['probe']):
            return case.finish('failed', 'the restored claim does '
                               'not fence probes: '
                               + json.dumps(final['probe'])[:300])
        state['switched'] = False
        case.observe('restored: ' + active + ' active and ' + peer
                     + ' tracking; one lifetime per journal, probes '
                     'fenced')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
    finally:
        # Whatever the legs left behind — a switched pair on an early
        # exit — the documented order puts the entry roles back,
        # best-effort; and the scenario's attachment releases whatever
        # hold it took so no claim outlives the connection.
        if state['switched'] and restore is not None:
            try:
                detail = restore()
            except Exception as exc:
                detail = str(exc)[:200]
            case.observe('cleanup: role restore '
                         + (detail or 'completed'))
        if stream is not None:
            try:
                _plant_request(stream, {'op': 'release_writer'})
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass


# --------------------------------------------------------------------
# The bounded command-admission contract (decision 83's ingress half):
# commands submitted faster than the scan boundary drains them must each
# take a structured receipt — a settlement or the named queue_full
# rejection — never a silent drop, a hang, or a server fault. The flood
# volume derives from the snapshot's served command_queue.capacity, and
# the flood leg's scan outputs and probe receipts must equal the
# bracketing no-flood legs'.

def scenario_command_admission(ctx):
    """A bounded command flood meets the receipted admission contract —
    decision 83's bounded-ingress half on the simulated rig."""
    case = Case('command-admission',
                'Bounded command admission under flood',
                'a command flood past the served command_queue capacity '
                'answers every submission with a receipted settlement '
                'or the named queue_full rejection — never a silent '
                'drop, a hang, or a server fault — admitted commands '
                'settle applied at their scan boundary, and the leg\'s '
                'scan outputs and probe receipts match the no-flood '
                'legs')
    try:
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30, interval=LEG_POLL)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        base = ctx[active]
        case.observe('command flood against ' + active
                     + ' (' + base + ')')
        _, signals = http_json('GET', base + '/signals')
        targets = _signal_targets(signals)
        if targets is None:
            return case.finish('inconclusive',
                               'no writable bool command point or '
                               'non-writable point in the model')
        snapshot = _snapshot(ctx, base)
        queue = snapshot.get('command_queue') or {}
        capacity = queue.get('capacity')
        ref = save_evidence(ctx['evidence_dir'],
                            'command-admission-signals.json',
                            {'signals': signals,
                             'command_queue': queue})
        case.evidence('file', ref, 'signal index and the served '
                      'admission bound')
        if not isinstance(capacity, int) or isinstance(capacity, bool) \
                or capacity < 1:
            return case.finish('inconclusive',
                               'the served snapshot carries no '
                               'command_queue capacity')
        if capacity > ADMISSION_MAX_CAPACITY:
            return case.finish(
                'inconclusive', 'the served command_queue capacity '
                + str(capacity) + ' is beyond the lane\'s flood reach '
                '(bound ' + str(ADMISSION_MAX_CAPACITY) + ')')
        case.observe('served command_queue capacity ' + str(capacity))

        legs = []
        signatures = {}
        for index, kind in enumerate(
                ('reference', 'command-flood', 'reference')):
            name = 'reference-' + ('a' if not signatures else 'b') \
                if kind == 'reference' else kind
            flood = {'point': targets['write'], 'capacity': capacity} \
                if kind == 'command-flood' else None
            overlay = _Overlay(kind, base, targets['watch'], flood)
            try:
                signature, failures = _consumer_leg(
                    ctx, base, targets, overlay, index % 2 == 0)
            except Exception as exc:
                # A flood leg that lost the monitor mid-run is the
                # submission path reaching the plant — a named failure;
                # a reference leg that cannot read the rig at all is
                # inconclusive like the other scenarios.
                if kind == 'reference':
                    raise
                signature, failures = None, ['leg errored: '
                                             + str(exc)[:200]]
            leg = {'leg': name, 'signature': signature,
                   'statuses': overlay.statuses[:40],
                   'errors': overlay.errors[:5]}
            if overlay.submissions:
                leg['submissions'] = len(overlay.submissions)
                outcomes = {}
                for submission in overlay.submissions:
                    key = str(submission['status']) + ':' \
                        + submission['outcome']
                    outcomes[key] = outcomes.get(key, 0) + 1
                leg['outcomes'] = outcomes
            legs.append(leg)
            ref = save_evidence(ctx['evidence_dir'],
                                'command-admission-legs.json', legs)
            if len(legs) == 1:
                case.evidence('file', ref)
            if kind == 'command-flood':
                snap = _try_snapshot(ctx, base) or {}
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'command-admission-flood.json',
                    {'capacity': capacity,
                     'submissions': len(overlay.submissions),
                     'queue_full': overlay.queue_fulls,
                     'command_queue': snap.get('command_queue')})
                case.evidence('file', ref, 'flood outcome counts and '
                              'the served queue metrics after the leg')
            if failures:
                return case.finish('failed',
                                   name + ': ' + '; '.join(failures))
            case.observe('leg ' + name + ': '
                         + json.dumps(signature, sort_keys=True))
            signatures[name] = signature
            if kind == 'reference':
                continue
            if signature != signatures['reference-a']:
                return case.finish(
                    'failed',
                    name + ' diverged from the no-flood legs: '
                    + json.dumps(signature, sort_keys=True) + ' vs '
                    + json.dumps(signatures['reference-a'],
                                 sort_keys=True))
        reference = signatures['reference-a']
        for key, healthy in (('scan', True), ('applied', True),
                             ('write', 'applied'),
                             ('reject', 'rejected:not_writable')):
            if reference[key] != healthy:
                return case.finish(
                    'failed', 'the no-flood reference leg is '
                    'unhealthy at ' + key + ': '
                    + json.dumps(reference, sort_keys=True))
        if reference['history'] not in ('contiguous', 'gapped'):
            return case.finish(
                'failed', 'the no-flood reference leg is unhealthy '
                'at history: ' + json.dumps(reference, sort_keys=True))
        if signatures['reference-b'] != reference:
            return case.finish('failed',
                               'the post-flood reference leg '
                               'diverged from the first')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))


# --------------------------------------------------------------------
# The served per-command availability verdicts (WW-FND-003's
# served-contract honesty): GET /resources reports each command's
# `available`/`refusal` verdict — advisory, the receipted path stays
# the sole authority — so a read model reporting a command invocable
# while dispatch refuses it, or vice versa, is exactly the dishonesty
# the tranche exists to prevent. The case reads the settled active's
# rows, asserts each is self-consistent (an `available: false` row
# carries a named refusal, an `available` row carries none), then
# proves probe-then-submit agreement: a served-unavailable command —
# the rig's port-adapted surfaces include managed-alarm `shelve`/`oos`
# commands whose bound-point writability is model-declared — settles
# the same named refusal the row served, never `applied`, and a
# served-available command settles `applied`. A tracking standby's
# /resources must report identical verdicts: decision 84's
# same-adopted-state rule makes availability a property of the
# adopted run, not of which peer is asked.

AVAILABILITY_DEADLINE = 30  # bound on each settled-receipt wait


def _command_rows(view):
    """The (component-name, row) command verdicts a /resources view
    serves, in served order."""
    rows = []
    for component in (view or {}).get('components') or []:
        for row in component.get('commands') or []:
            rows.append((component.get('name'), row))
    return rows


def _verdict_map(view):
    """A /resources view's verdicts keyed for the cross-peer
    comparison: {(component, command): (available, refusal)}."""
    verdicts = {}
    for component, row in _command_rows(view):
        verdicts[(str(component), str(row.get('name')))] = \
            (row.get('available'), row.get('refusal'))
    return verdicts


def _specs_by_name(schema):
    """The /schema registry's command specs keyed by (component,
    command name) — the provenance join a /resources row needs to
    reach the receipted path."""
    specs = {}
    for entry in (schema or {}).get('interfaces') or []:
        interface = (entry or {}).get('interface') or {}
        for spec in interface.get('commands') or []:
            specs[(entry.get('name'), spec.get('name'))] = spec
    return specs


def _served_refusal_text(reason):
    """The /resources `refusal` string a settled CommandError reason
    corresponds to: `not_writable`/`unknown_point` rows serve the
    named CommandError's text exactly as the receipted path answers
    it, while a kind-declared `command_refused` serves the declared
    refusal reason the CommandError's `reason` field echoes. Returns
    None for a reason the verdict surface cannot have served."""
    if not isinstance(reason, dict):
        return None
    if 'not_writable' in reason:
        point = (reason.get('not_writable') or {}).get('point')
        return 'I/O point PointId(' + str(point) \
            + ') is not declared writable'
    if 'unknown_point' in reason:
        point = (reason.get('unknown_point') or {}).get('point')
        return 'unknown I/O point PointId(' + str(point) + ')'
    if 'command_refused' in reason:
        return (reason.get('command_refused') or {}).get('reason')
    return None


def _rejected_reason(receipt):
    """The settled CommandError a terminal receipt carries, or None."""
    rejected = ((receipt or {}).get('outcome') or {}).get('rejected')
    return (rejected or {}).get('reason') \
        if isinstance(rejected, dict) else None


def _submitted_receipt(ctx, base, index, command):
    """The receipt log's entry for the submission appended at `index`
    once its outcome is terminal — None while it still reads
    `accepted` or the log cannot be read. The bounded ring answers at
    `index` until it evicts; past that the submission is the log's
    newest entry."""
    try:
        _, body = http_json('GET', base + '/receipts')
    except Exception:
        return None
    receipts = _receipt_list(body)
    if not receipts:
        return None
    receipt = receipts[index] if index < len(receipts) else receipts[-1]
    if receipt.get('command') != command:
        return None
    if _outcome_key(receipt) == 'accepted':
        return None
    return receipt


def _tracking_peer(ctx, active):
    """The ctx endpoint key reporting role=standby with tracking
    convergence — the parity check's peer — or None when the pair
    has no tracking standby."""
    for name in ('active', 'standby', 'revised'):
        if name == active or ctx.get(name) is None:
            continue
        try:
            report = _role(ctx, ctx[name])
        except Exception:
            continue
        if report.get('role') == 'standby' \
                and 'tracking' in (report.get('sync') or {}):
            return name
    return None


def scenario_command_availability(ctx):
    """The served per-command availability verdicts against the
    receipted path — WW-FND-003's served-contract honesty on the
    simulated rig."""
    case = Case('command-availability',
                'Served command verdicts agree with dispatch',
                'every GET /resources command row is self-consistent '
                '(an available: false row carries a named refusal, an '
                'available row carries none), a served-unavailable '
                'command submitted through POST /command settles the '
                'same named refusal the row served rather than '
                'applied, a served-available command settles applied, '
                'and a tracking standby serves identical verdicts')
    try:
        # Self-contained on either role layout, like served-interface:
        # replayed alone the rig is fresh (ctrl-a active), while the
        # full suite reaches this case after the failover.
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        base = ctx[active]
        case.observe('verdict surface against ' + active
                     + ' (' + base + ')')

        bodies = {}
        for path in ('/resources', '/schema', '/signals'):
            try:
                _, bodies[path] = http_json('GET', base + path)
            except urllib.error.HTTPError as exc:
                return case.finish('failed', 'GET ' + path
                                   + ' answered ' + str(exc.code))
        resources, schema, signals = (bodies['/resources'],
                                      bodies['/schema'],
                                      bodies['/signals'])
        ref = save_evidence(
            ctx['evidence_dir'], 'command-availability-rows.json',
            {'publication': resources.get('publication'),
             'tick': resources.get('tick'),
             'components': [{'name': entry.get('name'),
                             'commands': entry.get('commands')}
                            for entry in resources.get('components')
                            or []]})
        case.evidence('file', ref, 'the active\'s served verdict rows')

        rows = _command_rows(resources)
        if not rows:
            return case.finish('inconclusive',
                               'the resource view serves no command '
                               'rows to check')
        case.observe(str(len(rows)) + ' served command rows')

        # Every served verdict is self-consistent: `available: false`
        # carries the named refusal a submission would meet, an
        # `available` row carries none.
        malformed = []
        for component, row in rows:
            name = str(component) + ' ' + str(row.get('name'))
            if not isinstance(row.get('available'), bool):
                malformed.append(name + ' serves no boolean verdict')
            elif row['available'] and row.get('refusal') is not None:
                malformed.append(name + ' serves available with a '
                                 'refusal')
            elif not row['available'] \
                    and not (isinstance(row.get('refusal'), str)
                             and row['refusal'].strip()):
                malformed.append(name + ' serves available: false '
                                 'without a named refusal')
        if malformed:
            return case.finish('failed', 'served verdicts are not '
                               'self-consistent: '
                               + '; '.join(malformed[:8]))

        # The probe legs need each row's provenance: the /schema spec
        # sharing the row's (component, command) key is what translates
        # it back into a receipted-path submission.
        specs = _specs_by_name(schema)
        if not any(spec.get('availability') == 'kind_declared'
                   for spec in specs.values()):
            case.observe('the rig model declares no kind-declared-'
                         'availability command — that probe leg is '
                         'uncovered; bound-point-writable verdicts are '
                         'asserted below')
        translatable = []
        for component, row in rows:
            spec = specs.get((component, row.get('name')))
            if spec is None:
                continue
            submission = _command_for_spec(component, spec)
            if submission is not None:
                translatable.append((component, spec, row, submission))

        # Deterministic probe selection: the refused probe prefers a
        # port-adapted write — the managed-alarm shelve/oos surfaces
        # whose BoundPointWritable verdicts the model always declares —
        # then a kind-declared invoke; the available probe prefers the
        # writable-bool write the other command legs use, then a
        # declared invoke, then any remaining translatable row.
        writable = {entry.get('point')
                    for entry in signals.get('points', [])
                    if entry.get('writable')
                    and entry.get('direction') == 'in'
                    and entry.get('value_type') == 'bool'}
        preferred = {entry.get('point')
                     for entry in signals.get('points', [])
                     if entry.get('name') == 'p101-oos'} & writable

        def refused_rank(entry):
            adapted = entry[1].get('adapted')
            return 0 if adapted == 'write_value' \
                else 1 if adapted == 'declared' else 2

        def available_rank(entry):
            spec = entry[1]
            if spec.get('adapted') == 'write_value':
                return 0 if spec.get('point') in preferred \
                    else 1 if spec.get('point') in writable else 3
            return 2 if spec.get('adapted') == 'declared' else 3

        refused = sorted(
            (entry for entry in translatable
             if entry[2].get('available') is False), key=refused_rank)
        available = sorted(
            (entry for entry in translatable
             if entry[2].get('available') is True), key=available_rank)

        def submit(command):
            """POST the command and wait out its terminal receipt."""
            _, before = http_json('GET', base + '/receipts')
            index = len(_receipt_list(before))
            status, receipt = http_json(
                'POST', base + '/command',
                {'command': command, 'actor': 'qa-lane'})
            settled = wait_for(
                lambda: _submitted_receipt(ctx, base, index, command),
                time.monotonic() + AVAILABILITY_DEADLINE)
            return status, receipt, settled

        if refused:
            component, spec, row, command = refused[0]
            case.observe('served-unavailable probe: ' + str(component)
                         + ' ' + str(spec.get('name')) + ' — served '
                         + json.dumps(row.get('refusal')))
            status, receipt, settled = submit(command)
            ref = save_evidence(
                ctx['evidence_dir'],
                'command-availability-refused.json',
                {'row': row, 'command': command,
                 'submission': {'status': status, 'receipt': receipt},
                 'settled': settled})
            case.evidence('file', ref, 'the refused probe\'s settled '
                          'receipt against its served row')
            if status != 200:
                return case.finish('failed', 'the served-unavailable '
                                   'command returned no receipt: HTTP '
                                   + str(status))
            if settled is None:
                return case.finish('failed', 'the served-unavailable '
                                   'command\'s receipt never settled')
            outcome = _outcome_key(settled)
            if outcome == 'applied':
                return case.finish('failed', 'the served-unavailable '
                                   'command ' + str(spec.get('name'))
                                   + ' settled applied anyway')
            reason = _rejected_reason(settled)
            if _served_refusal_text(reason) != row.get('refusal'):
                return case.finish(
                    'failed', 'the settled refusal names a different '
                    'reason than the served row: served '
                    + json.dumps(row.get('refusal')) + ', settled '
                    + json.dumps(settled.get('outcome')))
            case.observe('settled ' + outcome
                         + ' — the same refusal the row served')
        else:
            case.observe('no served-unavailable translatable command '
                         '— the refused probe leg is uncovered')

        if available:
            component, spec, row, command = available[0]
            case.observe('served-available probe: ' + str(component)
                         + ' ' + str(spec.get('name')))
            status, receipt, settled = submit(command)
            ref = save_evidence(
                ctx['evidence_dir'],
                'command-availability-available.json',
                {'row': row, 'command': command,
                 'submission': {'status': status, 'receipt': receipt},
                 'settled': settled})
            case.evidence('file', ref, 'the available probe\'s '
                          'settled receipt against its served row')
            if status != 200:
                return case.finish('failed', 'the served-available '
                                   'command returned no receipt: HTTP '
                                   + str(status))
            if settled is None:
                return case.finish('failed', 'the served-available '
                                   'command\'s receipt never settled')
            if _outcome_key(settled) != 'applied':
                return case.finish('failed', 'the served-available '
                                   'command settled a named refusal: '
                                   + json.dumps(settled.get('outcome')))
            case.observe('served-available command settled applied')
        else:
            case.observe('no served-available translatable command '
                         '— the applied probe leg is uncovered')

        # Decision 84's same-adopted-state rule: the tracking peer's
        # read model carries the run's one verdict set.
        peer = _tracking_peer(ctx, active)
        if peer is not None:
            try:
                _, peer_view = http_json('GET', ctx[peer] + '/resources')
            except urllib.error.HTTPError as exc:
                return case.finish('failed', 'the tracking standby\'s '
                                   'GET /resources answered '
                                   + str(exc.code))
            ref = save_evidence(
                ctx['evidence_dir'],
                'command-availability-standby.json',
                {'peer': peer,
                 'components': [{'name': entry.get('name'),
                                 'commands': entry.get('commands')}
                                for entry in
                                peer_view.get('components') or []]})
            case.evidence('file', ref, 'the tracking standby\'s '
                          'served verdict rows')
            local, remote = _verdict_map(resources), \
                _verdict_map(peer_view)
            diverged = sorted(key for key in set(local) | set(remote)
                              if local.get(key) != remote.get(key))
            if diverged:
                return case.finish(
                    'failed', 'the tracking standby reports different '
                    'verdicts: ' + '; '.join(
                        key[0] + ' ' + key[1] + ': active '
                        + json.dumps(local.get(key)) + ' vs standby '
                        + json.dumps(remote.get(key))
                        for key in diverged[:8]))
            case.observe('tracking peer ' + peer + ' serves identical '
                         'verdicts (' + str(len(local)) + ' rows)')
        else:
            case.observe('no tracking standby — the cross-peer parity '
                         'leg is uncovered')
        return case.finish('passed')
    except urllib.error.HTTPError as exc:
        return case.finish('failed', 'a served endpoint answered '
                           + str(exc.code))
    except Exception as exc:
        return case.finish('inconclusive', str(exc))


# --------------------------------------------------------------------
# The shipped operator CLI as an external consumer (WW-FND-004's
# replaceable-consumer contract, WW-OPS-001/002's operator-facing
# surface): the lane's one proof that the real dcs-ctl binary — not a
# test harness — reads and commands a deployed pair. The binary comes
# from the run's image build (the bounded builder's
# `cargo build -p dcs-monitor --bin dcs-ctl` beside the image
# binaries), handed to the scenario as ctx['dcs_ctl']; every asserted
# read and mutation travels through CLI invocations against the
# published monitor addresses — the whole served-resource surface,
# role/signals/schema beside resources, the keyed and per-component
# events reads, receipts, and history. The read and refusal legs
# leave the plant untouched; the mutation legs — the named
# rejections, the receipted write, descriptor-bounded tune,
# force/unforce, the CLI switch legs, and the driven scan probe —
# run in _ctl_write_legs with per-leg actors and a full restore
# before the case returns.

DCS_CTL_TIMEOUT = 20   # bound on one dcs-ctl invocation
CTL_DEADLINE = 30      # bound on the journaled-settlement wait
CTL_ACTOR = 'qa-lane-dcs-ctl'  # the --actor the invoke declares


def _ctl_addr(base):
    """A monitor base URL as dcs-ctl's `<addr>` argument — host:port."""
    return base.split('://', 1)[-1]


def _run_ctl(binary, addr, args):
    """One dcs-ctl invocation, captured — the subprocess seam the pool
    tests fake."""
    return subprocess.run([binary, addr, *args], capture_output=True,
                          text=True, timeout=DCS_CTL_TIMEOUT)


def _value_literal(value):
    """A wire `{"bool": true}`-shaped Value as dcs-ctl's `<value>` text."""
    if 'bool' in value:
        return 'true' if value['bool'] else 'false'
    if 'int' in value:
        return str(value['int'])
    return repr(value['float'])


def _ctl_command_args(command):
    """The dcs-ctl argv submitting the picked receipted-path command:
    `invoke`, `set_parameter`, and `write_value` map to the same-named
    subcommands; any other variant has no CLI spelling and returns
    None."""
    if 'invoke' in command:
        body = command['invoke']
        return ['invoke', str(body['component']), str(body['command'])] \
            + [str(name) + '=' + _value_literal(value)
               for name, value in
               (body.get('arguments') or {}).items()]
    if 'set_parameter' in command:
        body = command['set_parameter']
        return ['set-parameter', str(body['component']),
                str(body['name']), _value_literal(body['value'])]
    if 'write_value' in command:
        body = command['write_value']
        return ['write', str(body['point']), _value_literal(body['value'])]
    return None


def _emitted_match(events, command):
    """The emitted-events entry covering `command`'s settlement — or
    any kind-emitted event — out of one component's attributed list:
    the produced-event proof both `events` read shapes owe."""
    for entry in events or []:
        event = (entry or {}).get('event') or {}
        settled = (event.get('command_settled') or {}) \
            .get('receipt') or {}
        if settled.get('command') == command \
                or event.get('event_emitted'):
            return entry
    return None


def scenario_dcs_ctl(ctx):
    """The shipped dcs-ctl binary against the deployed pair — the
    replaceable-consumer contract exercised through the operator CLI
    rather than raw HTTP."""
    case = Case('dcs-ctl',
                'dcs-ctl consumes the served contract externally',
                'the lane-built dcs-ctl binary reports exactly one '
                'active and one standby across the pair, its schema '
                'read covers every component kind the rig model '
                'declares, the resources read serves one live record '
                'per declared component beside the picked component\'s '
                'interface-parallel entry, the command the '
                'served-interface selection logic picks settles a '
                'receipt journaled with the --actor the leg passed '
                'and listed by the receipts read, the emitted-events '
                'read — keyed across the model and per component — '
                'attributes a produced event to its component, the '
                'history read returns a declared measurement point\'s '
                'retained samples, an undeclared or unavailable '
                'invocation is refused by name — never silently '
                'accepted — and the mutation legs refuse a write to '
                'a non-writable point and a command aimed at the '
                'standby peer by name, drive a receipted write of '
                'the writable p101-oos point from the served '
                'snapshot, a range-bounded set-parameter tune, a '
                'force and unforce asserting the '
                'Uncertain(Substituted) badge and its release, the '
                'demote/promote switch legs settling through the CLI '
                'with journaled role_changed events, and a scan '
                'probe the driven mode permits — every receipted '
                'leg under its own CTL_ACTOR-prefixed actor with the '
                'changed values, parameters, forces, and role layout '
                'restored before the case returns')
    transcript = []

    def done(outcome, detail=None):
        ref = save_evidence(ctx['evidence_dir'],
                            'dcs-ctl-transcript.json', transcript)
        if not any(entry['ref'] == ref
                   for entry in case.record['evidence']):
            case.evidence('file', ref,
                          'the dcs-ctl invocation transcript')
        return case.finish(outcome, detail)

    def ctl(base, *args):
        """Run the binary; append the invocation to the transcript;
        return (exit, parsed-stdout-or-None, stderr)."""
        addr = _ctl_addr(base)
        entry = {'argv': [addr] + [str(arg) for arg in args]}
        transcript.append(entry)
        try:
            result = _run_ctl(binary, addr, entry['argv'][1:])
        except Exception as exc:
            entry['error'] = str(exc)[:300]
            return None, None, str(exc)[:300]
        entry['exit'] = result.returncode
        try:
            body = json.loads(result.stdout)
        except (TypeError, ValueError):
            body = None
            entry['stdout'] = str(result.stdout)[:300]
        stderr = str(result.stderr or '').strip()
        if result.returncode or stderr:
            entry['stderr'] = stderr[:300]
        return result.returncode, body, stderr

    try:
        binary = ctx.get('dcs_ctl')
        if binary is None:
            return done('inconclusive', 'the run context carries no '
                        'dcs-ctl binary path')
        if not Path(binary).is_file() \
                or not os.access(binary, os.X_OK):
            return done('inconclusive', 'no executable dcs-ctl at '
                        + str(binary) + ' — the documented seam '
                        '(cargo build -p dcs-monitor --bin dcs-ctl '
                        'inside the lane\'s bounded image build) '
                        'produced nothing')
        case.observe('dcs-ctl binary: ' + str(binary) + ' — built by '
                     'the run\'s image build (cargo build --release '
                     '--locked -p dcs-monitor --bin dcs-ctl)')

        # The pair must be serving before the tool's answers mean
        # anything — the same liveness gate the other post-failover
        # cases apply, so a down monitor stays a rig failure rather
        # than masquerading as a CLI defect.
        if wait_for(lambda: _settled_active(ctx),
                    time.monotonic() + 30) is None:
            return done('failed', 'no peer reports role=active')

        roles = {}
        for name in ('active', 'standby'):
            rc, body, err = ctl(ctx[name], 'role')
            if rc != 0 or not isinstance(body, dict):
                return done('failed', 'dcs-ctl role failed on ' + name
                            + ' against a serving monitor: exit '
                            + str(rc) + ' ' + str(err)[:200])
            roles[name] = body.get('role')
        ref = save_evidence(ctx['evidence_dir'], 'dcs-ctl-roles.json',
                            roles)
        case.evidence('file', ref, 'dcs-ctl role on both endpoints')
        if sorted(str(role) for role in roles.values()) \
                != ['active', 'standby']:
            return done('failed', 'the post-failover pair is not one '
                        'active plus one standby: '
                        + json.dumps(roles, sort_keys=True))
        active = next(name for name in roles if roles[name] == 'active')
        base = ctx[active]
        case.observe('post-failover layout per dcs-ctl: '
                     + json.dumps(roles, sort_keys=True))

        rc, signals, err = ctl(base, 'signals')
        if rc != 0 or not isinstance(signals, dict):
            return done('failed', 'dcs-ctl signals failed: exit '
                        + str(rc) + ' ' + str(err)[:200])
        rc, schema, err = ctl(base, 'schema')
        if rc != 0 or not isinstance(schema, dict):
            return done('failed', 'dcs-ctl schema failed: exit '
                        + str(rc) + ' ' + str(err)[:200])
        ref = save_evidence(ctx['evidence_dir'], 'dcs-ctl-schema.json',
                            {'signals': signals, 'schema': schema})
        case.evidence('file', ref, 'the CLI-printed signal index and '
                      'interface registry')
        declared = signals.get('components') or []
        if not declared:
            return done('inconclusive', 'the signal index serves no '
                        'component records to check coverage against')
        served = {}
        for entry in schema.get('interfaces') or []:
            if isinstance(entry, dict):
                served[entry.get('name')] = entry.get('interface') or {}
        missing = [record for record in declared
                   if (served.get(record.get('name')) or {}).get('kind')
                   != record.get('kind')]
        if missing:
            return done(
                'failed', 'the schema read misses declared kinds '
                + ', '.join(sorted({str(r.get('kind'))
                                    for r in missing}))
                + ' (instances: '
                + ', '.join(str(r.get('name')) for r in missing[:8])
                + ')')
        kinds = sorted({str(record.get('kind')) for record in declared})
        case.observe('schema read covers ' + str(len(declared))
                     + ' declared instances across '
                     + str(len(kinds)) + ' kinds ('
                     + ', '.join(kinds) + ')')
        declared_kinds = {str(record.get('name')): record.get('kind')
                          for record in declared}

        # The whole-model resources read: the served ResourceView —
        # one live record per declared component, each kind-matched to
        # the index's declaration.
        rc, resources, err = ctl(base, 'resources')
        if rc != 0 or not isinstance(resources, dict):
            return done('failed', 'dcs-ctl resources failed: exit '
                        + str(rc) + ' ' + str(err)[:200])
        live = {}
        for record in resources.get('components') or []:
            if isinstance(record, dict) and record.get('name'):
                live[str(record['name'])] = record
        mismatch = [name for name in sorted(declared_kinds)
                    if (live.get(name) or {}).get('kind')
                    != declared_kinds[name]]
        if sorted(live) != sorted(declared_kinds) or mismatch:
            return done(
                'failed', 'the resources read does not answer one '
                'kind-matched record per declared component: served '
                + ', '.join(sorted(live)[:8]) + ' against '
                + str(len(declared)) + ' declared'
                + (('; mismatched kinds: ' + ', '.join(mismatch[:8]))
                   if mismatch else ''))
        case.observe('resources read serves one live record per '
                     'declared component (' + str(len(live)) + ')')

        picked = _pick_declared_command(schema.get('interfaces') or [],
                                        signals)
        if picked is None:
            return done('inconclusive', 'no served command translates '
                        'to the receipted path')
        component, spec, command = picked
        argv = _ctl_command_args(command)
        if argv is None:
            return done('inconclusive', 'the picked command has no '
                        'dcs-ctl spelling: ' + json.dumps(command))
        case.observe('picked command: ' + str(component) + ' '
                     + str(spec.get('name')) + ' -> dcs-ctl '
                     + ' '.join(argv) + ' --actor ' + CTL_ACTOR)

        # The named-component resources read: the picked instance's
        # ComponentResources entry — name- and kind-matched, its live
        # collections parallel to the interface's declared ones.
        rc, res_entry, err = ctl(base, 'resources', component)
        ref = save_evidence(ctx['evidence_dir'],
                            'dcs-ctl-resources.json',
                            {'view': resources, 'component': component,
                             'entry': res_entry})
        case.evidence('file', ref, 'the ResourceView and '
                      + str(component) + '\'s entry')
        if rc != 0 or not isinstance(res_entry, dict):
            return done('failed', 'dcs-ctl resources ' + str(component)
                        + ' failed: exit ' + str(rc) + ' '
                        + str(err)[:200])
        interface = served.get(component) or {}
        collections = ('measurements', 'configuration', 'state',
                       'commands', 'events')
        absent = [name for name in collections
                  if not isinstance(res_entry.get(name), list)]
        short = [name for name in collections[:-1]
                 if isinstance(res_entry.get(name), list)
                 and len(res_entry[name])
                 != len(interface.get(name) or [])]
        if res_entry.get('name') != component \
                or res_entry.get('kind') != interface.get('kind') \
                or absent or short:
            return done('failed', 'the resources entry for '
                        + str(component) + ' does not mirror the '
                        'served interface: name='
                        + str(res_entry.get('name')) + ' kind='
                        + str(res_entry.get('kind')) + ' absent='
                        + json.dumps(absent) + ' non-parallel='
                        + json.dumps(short))
        case.observe('resources entry for ' + str(component)
                     + ' mirrors the interface\'s collections')

        # The journal cursor ahead of the submission: earlier legs
        # already settled identical commands into the ring — the
        # served-interface case picks from the same selection logic
        # and submits under its own actor — so the settlement read
        # below starts above the high-water seq and never matches a
        # prior leg's entry.
        rc, prior, err = ctl(base, 'journal', '--since', '0')
        if rc != 0 or not isinstance(prior, list):
            return done('failed', 'dcs-ctl journal failed ahead of the '
                        'submission: exit ' + str(rc) + ' '
                        + str(err)[:200])
        floor = max((entry.get('seq') or 0
                     for entry in prior if isinstance(entry, dict)),
                    default=0)

        rc, receipt, err = ctl(base, *argv, '--actor', CTL_ACTOR)
        ref = save_evidence(
            ctx['evidence_dir'], 'dcs-ctl-invoke.json',
            {'argv': argv + ['--actor', CTL_ACTOR], 'exit': rc,
             'receipt': receipt, 'stderr': err})
        case.evidence('file', ref, 'the command\'s printed receipt')
        outcome = receipt.get('outcome') if isinstance(receipt, dict) \
            else None
        if not isinstance(receipt, dict) \
                or receipt.get('command') != command \
                or not isinstance(outcome, dict) or not outcome:
            return done('failed', 'the command returned no settled '
                        'receipt: exit ' + str(rc) + ' '
                        + json.dumps(receipt)[:300] + ' '
                        + str(err)[:200])
        if receipt.get('actor') != CTL_ACTOR:
            return done('failed', 'the printed receipt dropped the '
                        'declared --actor: actor='
                        + json.dumps(receipt.get('actor')))
        case.observe('receipt outcome: '
                     + json.dumps(outcome, sort_keys=True))

        # The attributed CommandSettled in the served journal, read
        # through `dcs-ctl journal` — GET /journal through the shipped
        # consumer — above the pre-submission cursor. The cursor alone
        # cannot name this leg's settlement: a checkpoint-adopted
        # receipt re-journals on the observing peer — the pair's one
        # command audit trail — so an earlier leg's identical command
        # under its own actor can land above the floor, as the lenovo
        # run's actor="qa-lane" carryover did. The receipt identity is
        # the match: only this leg declares CTL_ACTOR, so a settled
        # entry carrying it for this command is this submission's
        # echo — while a same-command entry under a foreign actor is
        # recorded for the failure detail, not matched.
        observed = {}

        def journaled():
            rc, journal, _err = ctl(base, 'journal', '--since',
                                    str(floor))
            if rc != 0 or not isinstance(journal, list):
                return None
            observed['journal_len'] = len(journal)
            for entry in journal:
                settled = (((entry or {}).get('event') or {})
                           .get('command_settled') or {}) \
                           .get('receipt') or {}
                if settled.get('command') != command:
                    continue
                settled_outcome = settled.get('outcome') or {}
                if 'applied' not in settled_outcome \
                        and 'rejected' not in settled_outcome:
                    continue
                if settled.get('actor') == CTL_ACTOR:
                    observed['entry'] = entry
                    return True
                observed.setdefault('foreign', entry)
            return None

        covered = wait_for(journaled,
                           time.monotonic() + CTL_DEADLINE)
        ref = save_evidence(
            ctx['evidence_dir'], 'dcs-ctl-journal.json',
            {'entry': observed.get('entry'),
             'foreign': observed.get('foreign'),
             'journal_len': observed.get('journal_len')})
        case.evidence('file', ref, 'the CLI-read journal covering the '
                      'command\'s settlement')
        if not covered:
            foreign = (((observed.get('foreign') or {})
                        .get('event') or {})
                       .get('command_settled') or {}) \
                       .get('receipt') or {}
            if foreign:
                return done('failed', 'the journaled receipt is '
                            'unattributed: actor='
                            + json.dumps(foreign.get('actor')))
            return done('failed', 'the served journal never recorded '
                        'the command\'s CommandSettled')
        settled = (observed['entry'].get('event') or {}) \
            .get('command_settled', {}).get('receipt') or {}
        if settled.get('actor') != CTL_ACTOR:
            return done('failed', 'the journaled receipt is '
                        'unattributed: actor='
                        + json.dumps(settled.get('actor')))
        case.observe('journal carries the settled receipt attributed '
                     'to ' + CTL_ACTOR)

        # The emitted-events reads: the produced event — the command's
        # settled receipt — attributed to its component, both in the
        # keyed whole-model view and the per-component list.
        rc_all, keyed, err_all = ctl(base, 'events')
        rc, events, err = ctl(base, 'events', component)
        ref = save_evidence(ctx['evidence_dir'], 'dcs-ctl-events.json',
                            {'component': component,
                             'keyed': {'exit': rc_all, 'events': keyed},
                             'exit': rc, 'events': events})
        case.evidence('file', ref, 'the emitted-events reads — keyed '
                      'across the model and for ' + str(component))
        if rc_all != 0 or not isinstance(keyed, dict):
            return done('failed', 'dcs-ctl events failed: exit '
                        + str(rc_all) + ' ' + str(err_all)[:200])
        if rc != 0 or not isinstance(events, list):
            return done('failed', 'dcs-ctl events failed for '
                        + str(component) + ': exit ' + str(rc) + ' '
                        + str(err)[:200])
        unkeyed = [name for name in sorted(declared_kinds)
                   if not isinstance(keyed.get(name), list)]
        if unkeyed:
            return done('failed', 'the keyed events read serves no '
                        'attributed list for '
                        + ', '.join(unkeyed[:8]))
        match = _emitted_match(events, command)
        if match is None:
            return done('failed', 'the emitted-events read attributes '
                        'no produced event to ' + str(component))
        if _emitted_match(keyed.get(component), command) is None:
            return done('failed', 'the keyed events read attributes '
                        'no produced event to ' + str(component))
        case.observe('events read attributes '
                     + next(iter(match.get('event') or {}), '?')
                     + ' to ' + str(component))

        # The settled-command read: the receipt log carries this leg's
        # attributed invoke — the command audit's listing half beside
        # the journal's durable record.
        rc, receipts, err = ctl(base, 'receipts')
        ref = save_evidence(ctx['evidence_dir'], 'dcs-ctl-receipts.json',
                            {'exit': rc, 'receipts': receipts})
        case.evidence('file', ref, 'the settled-command receipt list')
        if rc != 0 or not isinstance(receipts, list):
            return done('failed', 'dcs-ctl receipts failed: exit '
                        + str(rc) + ' ' + str(err)[:200])
        own = next((entry for entry in receipts
                    if isinstance(entry, dict)
                    and entry.get('command') == command
                    and entry.get('actor') == CTL_ACTOR), None)
        if own is None:
            return done('failed', 'the receipt log never recorded the '
                        'leg\'s attributed invoke')
        outcome = own.get('outcome') or {}
        if 'applied' not in outcome and 'rejected' not in outcome:
            return done('failed', 'the attributed invoke\'s receipt '
                        'never settled: ' + json.dumps(outcome)[:200])
        case.observe('receipts carries the attributed invoke settled '
                     + next(iter(outcome)))

        # The retained-samples read on a declared measurement point —
        # the picked component's bound measurement first, else any
        # served instance's.
        history_point = None
        ordered = sorted(
            schema.get('interfaces') or [],
            key=lambda entry: entry.get('name') != component)
        for entry in ordered:
            for measurement in ((entry.get('interface') or {})
                                .get('measurements') or []):
                if isinstance(measurement, dict) \
                        and measurement.get('point') is not None:
                    history_point = measurement['point']
                    break
            if history_point is not None:
                break
        if history_point is None:
            return done('inconclusive', 'no served measurement binds a '
                        'point for the history read')
        rc, history, err = ctl(base, 'history', '--point',
                               str(history_point))
        ref = save_evidence(ctx['evidence_dir'], 'dcs-ctl-history.json',
                            {'point': history_point, 'exit': rc,
                             'history': history})
        case.evidence('file', ref, 'retained samples for declared '
                      'point ' + str(history_point))
        if rc != 0 or not isinstance(history, list):
            return done('failed', 'dcs-ctl history failed for point '
                        + str(history_point) + ': exit ' + str(rc)
                        + ' ' + str(err)[:200])
        record = next((item for item in history
                       if isinstance(item, dict)
                       and item.get('point') == history_point), None)
        if record is None:
            return done('failed', 'the history read serves no record '
                        'for declared point ' + str(history_point))
        samples = record.get('samples')
        if not isinstance(samples, list) or not samples:
            return done('failed', 'declared point ' + str(history_point)
                        + ' retains no served samples')
        bad = [item for item in samples
               if not isinstance(item, dict)
               or not isinstance(item.get('seq'), int)
               or not isinstance(item.get('sample'), dict)]
        if bad:
            return done('failed', 'the history read serves malformed '
                        'samples: ' + json.dumps(bad[:2])[:300])
        case.observe('history retains ' + str(len(samples))
                     + ' samples for declared point '
                     + str(history_point))

        # The refusal legs: an invoke the served contract does not
        # declare, and — when the resource view advertises one — a
        # command whose availability rule currently refuses. Both must
        # answer the named refusal, never a silent accept.
        refusals = {}
        declared_names = {str(item.get('name'))
                          for item in (served.get(component) or {})
                          .get('commands') or []}
        probe = 'dcs-ctl-undeclared'
        while probe in declared_names:
            probe += '-x'
        rc, refused, err = ctl(base, 'invoke', component, probe,
                               '--actor', CTL_ACTOR)
        refusals['undeclared'] = {
            'argv': ['invoke', component, probe, '--actor', CTL_ACTOR],
            'exit': rc, 'receipt': refused, 'stderr': err}

        unavailable = None
        rc, fresh, _err = ctl(base, 'resources')
        if rc != 0 or not isinstance(fresh, dict):
            fresh = {}
        for record in fresh.get('components') or []:
            interface = served.get(record.get('name')) or {}
            states = {state.get('name'): state
                      for state in record.get('commands') or []}
            for cspec in interface.get('commands') or []:
                state = states.get(cspec.get('name'))
                if not state or state.get('available') is not False:
                    continue
                submission = _command_for_spec(record.get('name'),
                                               cspec)
                un_argv = (_ctl_command_args(submission)
                           if submission else None)
                if un_argv:
                    unavailable = (record.get('name'), cspec.get('name'),
                                   un_argv, state.get('refusal'))
                    break
            if unavailable:
                break
        if unavailable:
            un_component, un_name, un_argv, advertised = unavailable
            rc, refused, err = ctl(base, *un_argv,
                                   '--actor', CTL_ACTOR)
            refusals['unavailable'] = {
                'argv': un_argv + ['--actor', CTL_ACTOR], 'exit': rc,
                'receipt': refused, 'stderr': err,
                'component': un_component, 'command': un_name,
                'advertised_refusal': advertised}
        else:
            case.observe('no unavailable command advertised; the '
                         'undeclared probe covers the refusal leg')
        ref = save_evidence(ctx['evidence_dir'],
                            'dcs-ctl-refusals.json', refusals)
        case.evidence('file', ref, 'the named refusals')

        def rejection(leg):
            """The named rejection a refusal leg answered, or None."""
            receipt = leg['receipt']
            reason = ((receipt or {}).get('outcome') or {}) \
                .get('rejected') if isinstance(receipt, dict) else None
            reason = (reason or {}).get('reason') \
                if isinstance(reason, dict) else None
            return next(iter(reason), None) \
                if isinstance(reason, dict) and reason else None

        undeclared = refusals['undeclared']
        if undeclared['exit'] == 0 \
                or rejection(undeclared) != 'unknown_command':
            return done('failed', 'the undeclared invoke was not '
                        'refused by name: exit '
                        + str(undeclared['exit']) + ' '
                        + json.dumps(undeclared['receipt'])[:300])
        case.observe('undeclared invoke refused by name: '
                     + rejection(undeclared))
        if 'unavailable' in refusals:
            if refusals['unavailable']['exit'] == 0 \
                    or rejection(refusals['unavailable']) is None:
                return done('failed', 'the contract-named unavailable '
                            'command was silently accepted: '
                            + json.dumps(refusals['unavailable'])[:300])
            case.observe('unavailable command refused by name: '
                         + rejection(refusals['unavailable']))

        outcome, detail, legs = _ctl_write_legs(ctx, case, ctl,
                                                signals, schema,
                                                roles)
        for leg_name, payload in legs.items():
            ref = save_evidence(ctx['evidence_dir'],
                                'dcs-ctl-leg-' + leg_name + '.json',
                                payload)
            case.evidence('file', ref, 'the ' + leg_name
                          + ' mutation leg through the binary')
        ref = save_evidence(ctx['evidence_dir'],
                            'dcs-ctl-write-legs.json',
                            {'outcome': outcome, 'detail': detail})
        case.evidence('file', ref, 'the mutation legs outcome')
        if outcome != 'passed':
            return done(outcome, detail)
        case.observe('mutation legs: ' + str(detail))
        return done('passed')
    except Exception as exc:
        return done('inconclusive', str(exc))


# --------------------------------------------------------------------
# The peer-wait seams the monitoring-isolation contract owes the
# surviving monitor (WW-FND-004, decision 83's schedule extended to
# the two dead-peer findings): while the peer a standby pulls
# checkpoints from is unreachable, every request on the survivors —
# the sampled reads, the receipted command path, and a driven
# POST /scan batch whose per-scan pulls each wait the documented pull
# bound — must answer inside a declared bound rather than serialize
# behind the fetch. The run's third monitor is launched --driven
# through the runner-owned start_driven/stop_driven actions: an
# externally paced standby whose pulls happen only inside POST /scan,
# so a batch on it occupies exactly the worker the finding pinned.
# The pair-health surface is the page's own pairHealth rule applied
# to the /role reports the scenario polls — the unreachable peer, the
# degraded convergence, and the still-unsynchronized-past-grace
# standby are all named faults, never "redundant pair healthy".

LATENCY_BOUND = 2.0      # the declared per-request bound — under the
                         # ~3s dead-peer serialization the fixes removed
LATENCY_POLL = 0.4       # the sampling cadence inside the window
LATENCY_WINDOW = 8.0     # the dead-peer sampling window — inside the
                         # armed failover bound (~12s at
                         # failover_misses=120 on a 100ms scan)
LATENCY_BATCH_SCANS = 3  # driven scans per mid-window batch; each pull
                         # waits the documented CHECKPOINT_PULL_TIMEOUT
LATENCY_HEALTHY_SCANS = 64  # the large batch posted against the
                            # reconverged pair — its pulls answer, so
                            # it occupies its worker for a stretch
LATENCY_GRACE = 5.0      # the page's CONVERGENCE_GRACE — an
                         # unsynchronized peer past it is a named fault
LATENCY_SERVE_DEADLINE = 30   # bound on the driven monitor answering
LATENCY_SETTLE_DEADLINE = 60  # bound on the pair reconverging


def _timed(call):
    """Run call(); return (result, elapsed-seconds)."""
    started = time.monotonic()
    return call(), time.monotonic() - started


def _try_role(ctx, base):
    """GET /role on a monitor, or None when the peer does not answer."""
    try:
        return _role(ctx, base)
    except Exception:
        return None


def _pair_view(*keys):
    """The health view the case grades: one entry per peer the page
    would be configured with — its ctx endpoint key plus the
    unsynchronized-age clock pairHealth's noteSyncAge keeps."""
    return [{'key': key, 'report': None, 'error': None,
             'unsynced_since': None} for key in keys]


def _pair_poll(ctx, view):
    """One /role poll per peer in the view — the pair view's role
    cadence. A failed poll is recorded as pair health (the peer's
    name is its ctx key); a report of 'unsynchronized' starts or
    continues the grace clock, any converged state or the
    unreachable fault ends it."""
    for entry in view:
        try:
            entry['report'] = _role(ctx, ctx[entry['key']])
            entry['error'] = None
        except Exception as exc:
            entry['error'] = str(exc)[:200]
        if entry['error'] is None and entry['report'] is not None \
                and entry['report'].get('sync') == 'unsynchronized':
            entry['unsynced_since'] = (entry['unsynced_since']
                                       or time.monotonic())
        else:
            entry['unsynced_since'] = None


def _pair_health(view):
    """The page's pairHealth verdict over the view's last poll — the
    surface the deploy shows. Each failed poll names its peer
    unreachable; each peer reporting 'unsynchronized' for longer than
    the declared grace is named still not converged; any degraded or
    diverged sync is named by the peer reporting it. The pair is
    healthy only when exactly one peer reports active and no other
    faults exist — unreachable, unsynchronized, or diverged all fail
    healthy."""
    faults, actives = [], []
    for entry in view:
        name = entry['key']
        if entry['error'] is not None:
            was = (entry['report'] or {}).get('role')
            faults.append((str(was) + ' ' if was else '')
                          + name + ' unreachable')
            continue
        report = entry['report']
        if report is None:
            continue
        if report.get('role') == 'active':
            actives.append(name)
        sync = report.get('sync')
        if sync == 'unsynchronized' \
                and entry['unsynced_since'] is not None \
                and time.monotonic() - entry['unsynced_since'] \
                >= LATENCY_GRACE:
            faults.append(name + ' has not converged: unsynchronized '
                          'past the convergence grace')
        if isinstance(sync, dict):
            if 'degraded' in sync:
                faults.append(name + ' sync degraded: '
                              + str((sync['degraded'] or {})
                                    .get('detail')))
            if 'diverged' in sync:
                mismatches = (sync['diverged'] or {}) \
                    .get('mismatches') or []
                faults.append(name + ' standby diverged: staged '
                              'outputs mismatch the field at '
                              + ', '.join(str(m.get('point'))
                                          for m in mismatches))
    if not actives:
        faults.append('no peer reports role active')
    elif len(actives) > 1:
        faults.append('dual-active: ' + ', '.join(actives)
                      + ' report role active')
    return {'active': actives[0] if len(actives) == 1 else None,
            'faults': faults}


def scenario_dead_peer_latency(ctx):
    """Isolate the checkpoint source the tracking peer pulls from:
    the surviving standby's monitor and a driven third peer's batched
    /scan must keep answering inside the declared bound, the
    pair-health surface must name the redundancy faults the window
    leaves, and the pair must reconverge once the source returns."""
    case = Case(
        'dead-peer-latency',
        'Monitor stays bounded under dead-peer pulls and batched '
        'scans',
        'stopping the peer the tracking standby pulls checkpoints '
        'from leaves every sampled read on the surviving monitor and '
        'one receipted command answering inside the declared '
        + str(LATENCY_BOUND) + 's bound across the dead-peer window, '
        'a driven POST /scan batch on the third peer occupying only '
        'its own worker while its per-scan pulls wait the documented '
        'bound, the pair-health verdict naming the unreachable, '
        'degraded, and unsynchronized-past-grace faults rather than '
        'serving healthy, and the pair reconverging once the source '
        'returns — a large batch against the healthy pair leaving '
        'the sampled endpoints just as responsive')
    try:
        stop = ctx.get('stop_controller')
        start = ctx.get('start_controller')
        start_driven = ctx.get('start_driven')
        stop_driven = ctx.get('stop_driven')
        driven_base = ctx.get('driven')
        if stop is None or start is None or start_driven is None \
                or stop_driven is None or driven_base is None:
            return case.finish(
                'inconclusive', 'the run context carries no '
                'controller stop/start or driven-peer action — the '
                'dead-peer induction has no documented seam')
        owner = wait_for(lambda: _settled_active(ctx),
                         time.monotonic() + 30)
        if owner is None:
            return case.finish('failed', 'no peer reports '
                               'role=active')
        if owner not in ('active', 'standby'):
            return case.finish(
                'inconclusive', 'the field owner is ' + owner
                + ' — outside the launched pair the stop action '
                'names')
        peer = 'standby' if owner == 'active' else 'active'
        base = ctx[peer]
        case.observe('seam=source-stop: stop ' + owner + ' ('
                     + ctx[owner] + '), sample ' + peer + ' (' + base
                     + '), driven batch on ' + driven_base)

        # The tracking baseline: the survivor is the peer pulling
        # the owner's checkpoints — the fetch this induction kills.
        def tracking():
            report = _try_role(ctx, base)
            if (report or {}).get('role') == 'standby':
                return _tracking_aligned(report)
            return None

        if wait_for(tracking, time.monotonic() + 45,
                    interval=LATENCY_POLL) is None:
            return case.finish(
                'inconclusive', 'the surviving peer is not a '
                'tracking standby — the dead-peer induction has no '
                'observation point')
        _, signals = http_json('GET', base + '/signals')
        target = _writable_bool_point(signals)
        if target is None:
            return case.finish('inconclusive', 'no writable bool '
                               'point in the model')
        point = target['point']

        # The driven survivor: the run's third monitor, --standby on
        # the same source but externally paced — a batch on it is the
        # per-request pull chain. Fresh and never driven, it reports
        # unsynchronized: the convergence-grace clock the pair health
        # surface reads.
        try:
            launched = start_driven(owner)
        except Exception as exc:
            return case.finish(
                'inconclusive', 'the driven-peer launch never '
                'completed: ' + str(exc)[:300])
        case.observe('driven peer up: '
                     + str((launched or {}).get('container')))
        driven = wait_for(lambda: _try_role(ctx, driven_base),
                          time.monotonic() + LATENCY_SERVE_DEADLINE,
                          interval=LATENCY_POLL)
        if driven is None:
            return case.finish('inconclusive', 'the driven monitor '
                               'never answered /role')
        view = _pair_view(owner, peer, 'driven')
        _pair_poll(ctx, view)   # the grace clock starts here

        # Isolate: the peer both survivors pull from. The container
        # stop is the lane's partition convention — the fetch path
        # goes dead while every survivor stays serving.
        try:
            stop(owner)
        except Exception as exc:
            return case.finish(
                'inconclusive', 'the source-stop induction never '
                'completed: ' + str(exc)[:300])
        case.observe('checkpoint source ' + owner + ' stopped')

        stats = {}        # endpoint -> {'samples': n, 'within': n}
        breaches = []     # named bound violations, for the detail
        command_receipt = None
        grace_fault = None
        batch = {'thread': None, 'result': None, 'error': None,
                 'scans': LATENCY_BATCH_SCANS}
        promoted = False

        def run_batch(scans):
            try:
                batch['result'] = http_json(
                    'POST', driven_base + '/scan',
                    {'scans': scans},
                    timeout=scans * 2 + 15)
            except Exception as exc:
                batch['error'] = exc

        def sample(key, path):
            label = key + ' ' + path
            try:
                _body, elapsed = _timed(
                    lambda: http_json('GET', ctx[key] + path,
                                      timeout=LATENCY_BOUND))
            except Exception as exc:
                breaches.append(label + ' never answered inside the '
                                'bound: ' + str(exc)[:150])
                return
            entry = stats.setdefault(label,
                                     {'samples': 0, 'within': 0})
            entry['samples'] += 1
            if elapsed <= LATENCY_BOUND:
                entry['within'] += 1
            else:
                breaches.append(label + ' answered in '
                                + format(elapsed, '.2f') + 's past '
                                'the ' + str(LATENCY_BOUND)
                                + 's bound')

        # The dead-peer window: poll the health view (the grace
        # clock keeps running while the fetch path is dead), post
        # the driven batch once the unsynchronized fault is named,
        # and sample every endpoint on every pass — each read and
        # the one receipted command must answer inside the bound.
        deadline = time.monotonic() + LATENCY_WINDOW
        while time.monotonic() < deadline:
            _pair_poll(ctx, view)
            health = _pair_health(view)
            if grace_fault is None:
                grace_fault = next(
                    (f for f in health['faults']
                     if 'unsynchronized' in f and 'grace' in f), None)
            if grace_fault is not None and batch['thread'] is None:
                batch['thread'] = threading.Thread(
                    target=run_batch, args=(LATENCY_BATCH_SCANS,),
                    daemon=True)
                batch['thread'].start()
            for path in ('/role', '/snapshot', '/journal?since=0'):
                sample(peer, path)
                if batch['thread'] is not None \
                        and batch['thread'].is_alive():
                    sample('driven', path)
            if command_receipt is None:
                try:
                    (status, receipt), elapsed = _timed(
                        lambda: http_json(
                            'POST', base + '/command',
                            {'command': {'write_value': {
                                'point': point, 'kind': 'bool',
                                'value': {'bool': True}}},
                             'actor': 'qa-lane'},
                            timeout=LATENCY_BOUND))
                    command_receipt = {
                        'status': status, 'elapsed': elapsed,
                        'outcome': _outcome_key(receipt)}
                    if status != 200:
                        breaches.append('the window command answered '
                                        + str(status))
                except Exception as exc:
                    command_receipt = {'status': None, 'outcome': 'lost'}
                    breaches.append('the window command raised '
                                    + str(exc)[:150])
            survivor_role = next(
                (e['report'] for e in view if e['key'] == peer), None)
            if (survivor_role or {}).get('role') \
                    in ('promoting', 'active'):
                promoted = True
            if grace_fault and command_receipt \
                    and batch['thread'] is not None \
                    and not batch['thread'].is_alive() \
                    and all(v['within'] for v in stats.values()):
                break
            time.sleep(LATENCY_POLL)
        if promoted:
            case.observe('the surviving standby promoted to active '
                         'mid-window — reads and the command stayed '
                         'bounded through the failover')
        if batch['thread'] is not None:
            batch['thread'].join(timeout=LATENCY_BATCH_SCANS * 2 + 15)
        batch_result = batch['result']
        batch_status = batch_result[0] \
            if isinstance(batch_result, tuple) else None
        ref = save_evidence(
            ctx['evidence_dir'], 'dead-peer-latency-window.json',
            {'bound_seconds': LATENCY_BOUND,
             'endpoints': {label: {'answered': entry['samples'] > 0,
                                   'within_bound':
                                   entry['within'] == entry['samples']}
                           for label, entry in stats.items()},
             'command': {'status': command_receipt['status'],
                         'outcome': command_receipt['outcome']}
             if command_receipt else None,
             'grace_fault': grace_fault,
             'batch': {'scans': batch['scans'], 'status': batch_status,
                       'completed': batch['thread'] is not None
                       and not batch['thread'].is_alive()},
             'survivor_promoted': promoted})
        case.evidence('file', ref, 'the dead-peer window: bounded '
                      'samples, the receipted command, the named '
                      'grace fault, the driven batch')

        # Restore the path: the survivor's fetch thread is still
        # running — the next pull re-links the chain.
        try:
            start(owner)
        except Exception as exc:
            return case.finish(
                'inconclusive', 'the source restart never completed: '
                + str(exc)[:300])
        case.observe('checkpoint source ' + owner + ' restarted')

        # The driven peer's own pulls bring it into the pair: one
        # batch past the reconverged source lands it tracking.
        def owner_serving():
            return _try_role(ctx, ctx[owner]) is not None

        if wait_for(owner_serving,
                    time.monotonic() + LATENCY_SETTLE_DEADLINE,
                    interval=LATENCY_POLL) is None:
            return case.finish('failed', 'the checkpoint source '
                               'never served again after restart')
        try:
            http_json('POST', driven_base + '/scan',
                      {'scans': LATENCY_BATCH_SCANS},
                      timeout=LATENCY_BATCH_SCANS * 2 + 15)
        except Exception as exc:
            return case.finish('failed', 'the reconvergence batch '
                               'on the driven peer failed: '
                               + str(exc)[:200])

        def reconverged():
            _pair_poll(ctx, view)
            health = _pair_health(view)
            return health if not health['faults'] else None

        healthy = wait_for(reconverged,
                           time.monotonic() + LATENCY_SETTLE_DEADLINE,
                           interval=LATENCY_POLL)

        def sync_state(entry):
            """The named convergence state a /role report carries —
            the evidence's deterministic shape, never the tick-bearing
            payload."""
            sync = ((entry.get('report') or {}).get('sync'))
            if isinstance(sync, dict):
                return next(iter(sync), 'unknown')
            return sync

        ref = save_evidence(
            ctx['evidence_dir'], 'dead-peer-latency-health.json',
            {'faults': (healthy or {}).get('faults'),
             'roles': {e['key']: (e['report'] or {}).get('role')
                       for e in view},
             'syncs': {e['key']: sync_state(e) for e in view}})
        case.evidence('file', ref, 'the pair-health verdict after '
                      'reconvergence — every window fault cleared')
        if healthy is None:
            return case.finish('failed', 'the pair never '
                               'reconverged after the path returned')

        # The healthy-pair batch: the large driven batch while the
        # pull path answers — the sampled endpoints must stay inside
        # the bound while the batch occupies its worker.
        healthy_batch = {'thread': None, 'result': None,
                         'error': None}

        def run_healthy():
            try:
                healthy_batch['result'] = http_json(
                    'POST', driven_base + '/scan',
                    {'scans': LATENCY_HEALTHY_SCANS},
                    timeout=LATENCY_HEALTHY_SCANS * 2 + 15)
            except Exception as exc:
                healthy_batch['error'] = exc

        healthy_batch['thread'] = threading.Thread(
            target=run_healthy, daemon=True)
        healthy_batch['thread'].start()
        healthy_stats = {}
        healthy_deadline = time.monotonic() + LATENCY_WINDOW \
            + LATENCY_HEALTHY_SCANS * 2 + 15
        while healthy_batch['thread'].is_alive() \
                and time.monotonic() < healthy_deadline:
            for key in (peer, 'driven'):
                for path in ('/role', '/snapshot', '/journal?since=0'):
                    label = key + ' ' + path
                    try:
                        _body, elapsed = _timed(
                            lambda: http_json('GET', ctx[key] + path,
                                              timeout=LATENCY_BOUND))
                    except Exception as exc:
                        breaches.append('healthy-batch ' + label
                                        + ' raised '
                                        + str(exc)[:150])
                        continue
                    entry = healthy_stats.setdefault(
                        label, {'samples': 0, 'within': 0})
                    entry['samples'] += 1
                    if elapsed <= LATENCY_BOUND:
                        entry['within'] += 1
                    else:
                        breaches.append('healthy-batch ' + label
                                        + ' answered in '
                                        + format(elapsed, '.2f')
                                        + 's past the bound')
            time.sleep(LATENCY_POLL)
        healthy_batch['thread'].join(
            timeout=LATENCY_HEALTHY_SCANS * 2 + 15)
        healthy_result = healthy_batch['result']
        healthy_status = healthy_result[0] \
            if isinstance(healthy_result, tuple) else None
        ref = save_evidence(
            ctx['evidence_dir'],
            'dead-peer-latency-healthy-batch.json',
            {'scans': LATENCY_HEALTHY_SCANS,
             'status': healthy_status,
             'completed': not healthy_batch['thread'].is_alive(),
             'endpoints': {label: {'answered': e['samples'] > 0,
                                   'within_bound':
                                   e['within'] == e['samples']}
                           for label, e in healthy_stats.items()}})
        case.evidence('file', ref, 'the healthy-pair batch — the '
                      'sampled endpoints stayed inside the bound '
                      'while it ran')

        # Teardown the third peer; the rig's pair is what later
        # cases see.
        try:
            stop_driven()
        except Exception as exc:
            return case.finish(
                'inconclusive', 'the driven-peer teardown never '
                'completed: ' + str(exc)[:300])

        if grace_fault is None:
            return case.finish(
                'failed', 'the isolation never produced the named '
                'unsynchronized-past-grace fault — the health '
                'surface never named the still-unsynchronized '
                'standby')
        if command_receipt is None \
                or command_receipt['status'] != 200:
            return case.finish(
                'failed', 'the dead-peer window never produced the '
                'receipted command answer')
        if command_receipt['outcome'] == 'unknown':
            return case.finish(
                'failed', 'the window command settled with no '
                'named verdict: ' + json.dumps(command_receipt))
        if command_receipt['outcome'] == 'rejected:queue_full':
            return case.finish(
                'failed', 'the window command hit an admission '
                'rejection: rejected:queue_full — the queueing the '
                'dead-peer fix removes')
        if batch['error'] is not None or batch_status != 200:
            return case.finish(
                'failed', 'the mid-window driven batch starved: '
                + str(batch['error'] or batch_status)[:300])
        if healthy_batch['error'] is not None \
                or healthy_status != 200:
            return case.finish(
                'failed', 'the healthy-pair batch starved: '
                + str(healthy_batch['error']
                      or healthy_status)[:300])
        missing = [label for label, entry in stats.items()
                   if not entry['samples']] \
            + [label for label, entry in healthy_stats.items()
               if not entry['samples']]
        if missing:
            return case.finish(
                'failed', 'no sample ever landed on '
                + ', '.join(sorted(missing)))
        if breaches:
            return case.finish(
                'failed', 'a sampled request starved behind the '
                'peer waits: ' + '; '.join(breaches[:3])
                + (' ...' if len(breaches) > 3 else ''))
        case.observe('pair-health verdict: ' + grace_fault)
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))


# --------------------------------------------------------------------
# The serving-lane resilience contract (WW-FND-004, WW-LCM-001 — the
# settled #624 lane split proven end to end on the armed deployed
# pair): the monitor quarantines the body-reading handlers — POST
# /command, POST /scan, and any request still owed a body — onto a
# bounded submission pool behind MAX_REQUEST_BODY, so a stalled-body
# flood pins only that lane while the SERVE_WORKERS serving lane keeps
# answering — GET /checkpoint among it, the heartbeat the armed
# tracking standby measures the active's liveness by. Before the fix
# the same flood pinned every worker, timed out /role for the whole
# hold, and on an armed rig read as a dead active: the standby's
# heartbeat pulls missed, it self-promoted inside the armed budget,
# claimed the field, and fenced a live active — transport-level
# starvation indistinguishable from active death. The leg opens the
# saturating set of incomplete-body connections against the settled
# active's monitor; through the hold the liveness reads keep answering
# inside the declared per-request bound while the standby's checkpoint
# pulls keep landing — its reported tracking alignment advancing, no
# role transition on either peer, no role_changed or field_claim_lost
# journaled, and the plant's writer claim still fencing third-party
# probes; closing the set frees the submission lane — a receipted
# command settles applied and the pace-driven scans keep landing —
# with the pair's roles unchanged throughout. Two consecutive passes
# must produce identical digests. The diagnostics name the miss by
# class: monitor-starvation-failed is the serving contract not
# performing — a liveness read never answering, the standby's pulls
# stalling, a fencing probe unanswered, the recovery never landing;
# monitor-starvation-nondeterministic is the run producing a result
# the contract declares impossible — a role transition or
# field_claim_lost, a probe writing through the standing claim, a
# regressed alignment, an answer past the declared bound, or two
# passes disagreeing.

STARVE_CONNECTIONS = 8   # the saturating set — past both lanes' worker
                         # pools summed, the shape the reproduction
                         # pinned every worker with
STARVE_PULL_ADVANCE = 15  # checkpoint-pull advances the hold must
                          # witness on the armed standby — ~1.5s of
                          # landed heartbeats at the rig's scan cadence
STARVE_SETTLE = 30       # bound on the pair reporting its settled
                         # armed tracking layout before the leg runs
STARVE_DEADLINE = 20     # bound on one pass's hold — past the armed
                         # 120-miss budget at the rig's scan cadence,
                         # so a starved heartbeat produces the spurious
                         # promotion inside the window; a heartbeat
                         # that never lands the witnessed pulls starved
STARVE_POLL = 0.4        # sampling cadence inside the hold
STARVE_RECOVER = 20      # bound on the post-close settlement and the
                         # resumed scan cadence
STARVE_ENDPOINTS = ('/role', '/snapshot', '/checkpoint')

# The reproduction's pinning shape: headers a body-reading handler must
# block on, the declared body never following — an Expect request's
# held reader and a chunked scan's unterminated first chunk. Each holds
# its submission-lane worker until the connection closes.
STARVE_REQUESTS = (
    b'POST /command HTTP/1.1\r\nHost: q\r\nContent-Length: 16\r\n'
    b'Expect: 100-continue\r\n\r\n',
    b'POST /scan HTTP/1.1\r\nHost: q\r\nTransfer-Encoding: chunked\r\n'
    b'\r\n10\r\n')


def _starvation_flood(base):
    """The saturating set of incomplete-body connections against a
    monitor base — each carrying a pinning request whose declared body
    never follows. Returns the open sockets; raises OSError when the
    set never opens."""
    streams = []
    try:
        for index in range(STARVE_CONNECTIONS):
            stream = _connect(base)
            stream.sendall(
                STARVE_REQUESTS[index % len(STARVE_REQUESTS)])
            streams.append(stream)
    except OSError:
        for stream in streams:
            stream.close()
        raise
    return streams


def _starvation_pass(ctx, active, peer, point, value, floors):
    """One starvation pass: flood the active's submission lane, hold
    the window while the serving reads, the armed standby's checkpoint
    pulls, and the field's fencing probes are sampled — the hold ends
    once the witnessed pulls prove the heartbeat kept landing — then
    close and prove the submission lane recovered. Returns (digest,
    violations, evidence): digest is the pass's normalized verdict
    record, identical across clean passes; violations is
    {key: (diagnostic, detail)} in first-seen order."""
    base, peer_base = ctx[active], ctx[peer]
    violations = {}
    evidence = {}

    def note(key, diagnostic, detail):
        violations.setdefault(key, (diagnostic, detail))

    stats = {path: {'answered': 0, 'within': 0}
             for path in STARVE_ENDPOINTS}
    tracking = {'first': None, 'aligned': None, 'lost': False}
    fencing = {'answered': 0, 'unfenced': 0}
    snaps = []
    journal_clean = True
    try:
        streams = _starvation_flood(base)
    except OSError as exc:
        note('flood', 'monitor-starvation-failed',
             'the saturating set never opened: ' + str(exc)[:200])
        return None, violations, evidence
    try:
        # The hold: sample until the standby's witnessed pulls prove
        # the heartbeat kept landing — or the deadline names the
        # starvation.
        deadline = time.monotonic() + STARVE_DEADLINE
        while time.monotonic() < deadline:
            for path in STARVE_ENDPOINTS:
                try:
                    body, elapsed = _timed(
                        lambda: http_json('GET', base + path,
                                          timeout=LATENCY_BOUND)[1])
                except Exception as exc:
                    note('read-' + path, 'monitor-starvation-failed',
                         'GET ' + path + ' never answered inside the '
                         + str(LATENCY_BOUND) + 's bound under the '
                         'hold: ' + str(exc)[:150])
                    continue
                stats[path]['answered'] += 1
                if elapsed <= LATENCY_BOUND:
                    stats[path]['within'] += 1
                else:
                    note('late-' + path,
                         'monitor-starvation-nondeterministic',
                         'GET ' + path + ' answered in '
                         + format(elapsed, '.2f') + 's past the '
                         + str(LATENCY_BOUND) + 's bound under the '
                         'hold')
                if path == '/snapshot':
                    snaps.append(body.get('tick')
                                 if isinstance(body, dict) else None)
            report = _try_role(ctx, peer_base)
            if report is None:
                note('peer-role', 'monitor-starvation-failed',
                     'the armed standby\'s own monitor never answered '
                     '/role through the hold')
            elif report.get('role') != 'standby':
                tracking['lost'] = True
                note('peer-moved',
                     'monitor-starvation-nondeterministic',
                     'the armed standby moved to role '
                     + str(report.get('role')) + ' under the hold — '
                     'the failover transition the lane split exists '
                     'to prevent')
            else:
                aligned = _tracking_aligned(report)
                if aligned is None:
                    note('sync-lost', 'monitor-starvation-failed',
                         'the standby\'s sync left tracking under the '
                         'hold — its checkpoint pulls stopped landing')
                elif tracking['first'] is None:
                    tracking['first'] = aligned
                    tracking['aligned'] = aligned
                elif tracking['aligned'] is not None \
                        and aligned < tracking['aligned']:
                    note('regressed',
                         'monitor-starvation-nondeterministic',
                         'the standby\'s reported alignment regressed '
                         'mid-hold: ' + str(aligned) + ' below '
                         + str(tracking['aligned']))
                else:
                    tracking['aligned'] = aligned
            probe = _try_plant(ctx, {'op': 'step', 'dt': 0})
            if probe is not None:
                fencing['answered'] += 1
                if not _fenced(probe):
                    fencing['unfenced'] += 1
                    note('unfenced',
                         'monitor-starvation-nondeterministic',
                         'a third-party probe wrote through the '
                         'standing writer claim under the hold')
            if tracking['aligned'] is not None \
                    and tracking['first'] is not None \
                    and tracking['aligned'] - tracking['first'] \
                    >= STARVE_PULL_ADVANCE:
                break
            time.sleep(STARVE_POLL)
        # The journal audit — read through the still-open flood; it is
        # itself a serving-lane read.
        for name, floor in floors.items():
            try:
                _, journal = http_json(
                    'GET', ctx[name] + '/journal?since=' + str(floor),
                    timeout=LATENCY_BOUND)
            except Exception as exc:
                journal_clean = False
                note('journal-' + name, 'monitor-starvation-failed',
                     name + '\'s journal never served through the '
                     'hold: ' + str(exc)[:150])
                continue
            for entry in _journal_list(journal):
                event = entry.get('event') or {}
                if 'role_changed' in event:
                    journal_clean = False
                    note('journaled-role-' + name,
                         'monitor-starvation-nondeterministic',
                         name + ' journaled a role_changed event '
                         'through the hold')
                if 'field_claim_lost' in event:
                    journal_clean = False
                    note('journaled-claim-' + name,
                         'monitor-starvation-nondeterministic',
                         name + ' journaled a field_claim_lost event '
                         'through the hold')
    finally:
        for stream in streams:
            stream.close()

    # The hold's verdicts.
    for path, entry in stats.items():
        if entry['answered'] == 0:
            note('served-' + path, 'monitor-starvation-failed',
                 'GET ' + path + ' never answered under the hold')
    moved = 'peer-moved' in violations or 'sync-lost' in violations
    if not moved:
        if tracking['first'] is None:
            note('pulls-never', 'monitor-starvation-failed',
                 'the standby\'s checkpoint pulls never landed '
                 'through the hold — the heartbeat the armed budget '
                 'counts')
        elif tracking['aligned'] - tracking['first'] \
                < STARVE_PULL_ADVANCE:
            note('pulls-stalled', 'monitor-starvation-failed',
                 'the standby\'s checkpoint pulls stalled through '
                 'the hold — ' + str(tracking['aligned']
                                     - tracking['first'])
                 + ' witnessed advances, the heartbeat the armed '
                 + str(ctx.get('failover_misses'))
                 + '-miss budget counts')
    ticks = [tick for tick in snaps if isinstance(tick, int)]
    if snaps and not ticks:
        note('scans-tickless', 'monitor-starvation-failed',
             'the served snapshots carried no run tick under the '
             'hold')
    elif len(ticks) > 1 and ticks[-1] <= ticks[0]:
        note('scans-stalled', 'monitor-starvation-failed',
             'the active\'s pace-driven scans stalled under the '
             'hold — /snapshot\'s tick never advanced')
    if fencing['answered'] == 0:
        note('fencing', 'monitor-starvation-failed',
             'the field never answered a fencing probe through the '
             'hold')

    # Recovery: the flood closed, the submission lane frees — the
    # receipted path, the driven-scan endpoint's documented refusal,
    # and the paced cadence all return.
    recovery = {}
    floor = floors.get(active, 0)
    try:
        _, before = http_json('GET', base + '/receipts')
        index = len(_receipt_list(before))
        status, receipt = http_json(
            'POST', base + '/command',
            {'command': {'write_value': {'point': point, 'kind': 'bool',
                                         'value': {'bool': value}}},
             'actor': 'qa-lane'})
        recovery['command'] = {'status': status,
                               'outcome': _outcome_key(receipt)}
        if status != 200 \
                or recovery['command']['outcome'] != 'accepted':
            note('recovery-command', 'monitor-starvation-failed',
                 'the recovery command answered '
                 + recovery['command']['outcome'] + ' — the '
                 'submission lane never freed')
        else:
            settled = wait_for(
                lambda: _settled_outcome(ctx, base, index),
                time.monotonic() + STARVE_RECOVER)
            recovery['settled'] = settled
            if settled != 'applied':
                note('recovery-settled', 'monitor-starvation-failed',
                     'the recovery command never settled applied: '
                     + str(settled))

            def covered():
                try:
                    _, journal = http_json(
                        'GET', base + '/journal?since=' + str(floor))
                except Exception:
                    return None
                return _journal_covers(journal, point) or None
            if wait_for(covered,
                        time.monotonic() + STARVE_RECOVER) is None:
                note('recovery-journal', 'monitor-starvation-failed',
                     'the settled recovery command never journaled')
    except Exception as exc:
        note('recovery-command', 'monitor-starvation-failed',
             'the submission lane never answered a command after the '
             'hold: ' + str(exc)[:150])
    try:
        scan_status, _ = _request_status('POST', base + '/scan',
                                         {'scans': 1})
    except Exception:
        scan_status = None
    recovery['scan_status'] = scan_status
    if scan_status != 409:
        note('recovery-scan', 'monitor-starvation-failed',
             'the paced monitor\'s documented POST /scan refusal '
             'never answered after the hold — the submission lane '
             'never freed: ' + str(scan_status))
    last_tick = ticks[-1] if ticks else None

    def resumed():
        snap = _try_snapshot(ctx, base)
        if not isinstance(snap, dict) or last_tick is None:
            return None
        return snap.get('tick') if isinstance(snap.get('tick'), int) \
            and snap['tick'] > last_tick else None
    if last_tick is not None \
            and wait_for(resumed,
                         time.monotonic() + STARVE_RECOVER) is None:
        note('recovery-scans', 'monitor-starvation-failed',
             'the pace-driven scans never resumed after the hold')
    last_aligned = tracking['aligned']

    def converged():
        report = _try_role(ctx, peer_base)
        if (report or {}).get('role') != 'standby':
            return None
        aligned = _tracking_aligned(report)
        if aligned is None or last_aligned is not None \
                and aligned <= last_aligned:
            return None
        return aligned
    if not tracking['lost'] \
            and wait_for(converged,
                         time.monotonic() + STARVE_RECOVER) is None:
        note('recovery-tracking', 'monitor-starvation-failed',
             'the standby\'s checkpoint pulls never resumed landing '
             'after the hold')
    roles = {}
    for name in (active, peer):
        report = _try_role(ctx, ctx[name])
        roles[name] = (report or {}).get('role')
    if roles[active] is None:
        note('roles-active', 'monitor-starvation-failed',
             'the field owner\'s /role never answered after the '
             'hold')
    elif roles[active] != 'active':
        note('roles-active', 'monitor-starvation-nondeterministic',
             'the field owner left active role: '
             + str(roles[active]))
    if roles[peer] is None:
        note('roles-peer', 'monitor-starvation-failed',
             'the armed standby\'s /role never answered after the '
             'hold')
    elif roles[peer] != 'standby' and 'peer-moved' not in violations:
        note('roles-peer', 'monitor-starvation-nondeterministic',
             'the armed standby left standby role: '
             + str(roles[peer]))
    recovery['roles'] = roles
    evidence.update({
        'connections': STARVE_CONNECTIONS,
        'armed_miss_budget': ctx.get('failover_misses'),
        'stats': stats, 'snaps': snaps, 'tracking': tracking,
        'fencing': fencing, 'recovery': recovery,
        'violations': {key: name for key, (name, _)
                       in violations.items()}})
    evidence['digest'] = {
        'reads': {path: ('starved' if not e['answered']
                         else 'bounded'
                         if e['within'] == e['answered'] else 'late')
                  for path, e in stats.items()},
        'scans': ('advanced'
                  if ticks and ticks[-1] > ticks[0] else 'stalled'),
        'tracking': ('lost' if tracking['lost']
                     else 'regressed' if 'regressed' in violations
                     else 'advanced'
                     if tracking['first'] is not None
                     and tracking['aligned'] - tracking['first']
                     >= STARVE_PULL_ADVANCE
                     else 'stalled'),
        'fencing': ('unanswered' if not fencing['answered']
                    else 'breached' if fencing['unfenced']
                    else 'fenced'),
        'journal': 'clean' if journal_clean else 'transitioned',
        'recovery': ('settled'
                     if recovery.get('settled') == 'applied'
                     and 'recovery-scan' not in violations
                     else 'unsettled'),
        'roles': ('moved'
                  if {roles[active], roles[peer]}
                  - {'active', 'standby', None}
                  else 'unanswered' if None in (roles[active],
                                                roles[peer])
                  else 'unchanged')}
    return evidence['digest'], violations, evidence


def scenario_monitor_starvation(ctx):
    """Saturate the settled active's monitor with incomplete request
    bodies — the #624 reproduction's pinning shape — and prove the
    serving lane keeps answering through the hold on the armed rig:
    the liveness reads inside the declared bound, the tracking
    standby's checkpoint pulls landing, neither peer transitioning,
    the field's writer claim fencing probes; then closing the set
    restores the submission lane's command and scan serving."""
    case = Case('monitor-starvation',
                'Serving lane survives incomplete-body saturation',
                'with the armed tracking standby settled, the '
                'saturating set of incomplete-body connections '
                'against the active\'s monitor leaves GET /role, '
                '/snapshot, and /checkpoint answering inside the '
                'declared ' + str(LATENCY_BOUND) + 's bound while '
                'the standby\'s checkpoint pulls keep landing — no '
                'role transition or field_claim_lost on either peer '
                'and the plant\'s writer claim fencing third-party '
                'probes — and closing the set restores full command '
                'and scan serving: a receipted command settling '
                'applied and the pace-driven scans resuming, the '
                'pair\'s roles unchanged; two consecutive passes '
                'produce identical digests')
    try:
        if ctx.get('plant') is None:
            return case.finish('inconclusive',
                               'no simulated plant endpoint — the '
                               'fencing probes cannot run')
        deadline = time.monotonic() + STARVE_SETTLE
        active = wait_for(lambda: _settled_active(ctx), deadline)
        if active is None:
            return case.finish('failed',
                               'no peer reports role=active')
        if active != 'active':
            return case.finish(
                'inconclusive', 'the pair\'s roles are already '
                'switched — the armed standby no longer tracks the '
                'peer this leg starves')
        peer = wait_for(lambda: _tracking_peer(ctx, active), deadline)
        if peer != 'standby':
            return case.finish(
                'inconclusive', 'no armed tracking standby — the '
                '--auto-promote heartbeat half of the contract '
                'cannot run')
        base = ctx[active]
        _, signals = http_json('GET', base + '/signals')
        target = _writable_bool_point(signals)
        if target is None or target.get('point') is None:
            return case.finish('failed', 'no writable boolean point '
                               'for the recovery command')
        point = target['point']
        snap = _try_snapshot(ctx, base) or {}
        baseline = _point_value(snap, point)
        value = baseline if isinstance(baseline, bool) else True
        floors = {}
        for name in (active, peer):
            try:
                _, journal = http_json('GET', ctx[name] + '/journal')
            except Exception as exc:
                return case.finish('inconclusive',
                                   name + '\'s journal floor never '
                                   'served: ' + str(exc)[:200])
            entries = _journal_list(journal)
            floors[name] = (entries[-1].get('seq') or 0) \
                if entries else 0
        digests = []
        for number in (1, 2):
            digest, violations, evidence = _starvation_pass(
                ctx, active, peer, point, value, floors)
            ref = save_evidence(
                ctx['evidence_dir'],
                'monitor-starvation-pass-' + str(number) + '.json',
                evidence)
            case.evidence('file', ref, 'starvation pass '
                          + str(number) + ' — the held window\'s '
                          'sampled verdicts, the fencing probes, and '
                          'the recovery')
            if violations:
                failed = any(name == 'monitor-starvation-failed'
                             for name, _ in violations.values())
                diagnostic = 'monitor-starvation-failed' if failed \
                    else 'monitor-starvation-nondeterministic'
                return case.finish(
                    'failed', diagnostic + ': ' + '; '.join(
                        detail for _, detail in
                        list(violations.values())[:4]))
            digests.append(digest)
        if digests[0] != digests[1]:
            return case.finish(
                'failed', 'monitor-starvation-nondeterministic: the '
                'two passes\' digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two starvation passes, identical digests')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))


# --------------------------------------------------------------------
# The unclaimed-field inline re-arm contract (WW-LCM-001's continuity
# clause, WW-OPS-003's field-confidence clause, the settled #621
# behavior the qax-20260918-008 run verified on this rig): a field
# write refused `unclaimed` while no claim stands is a recoverable
# ownerless window, not supersession — the recorded owner re-arms
# inline through one conditional, non-preempting `ensure_writer` and
# the write lands without field_claim_lost or demotion, while a write
# refused under another standing claim still fences and demotes. The
# leg opens that window deliberately: a dedicated attachment's
# `claim_writer` preempts the standing claim and its `release_writer`
# hands it back in the same breath, so the field sits unclaimed
# behind the owner's live connection — then the watch proves the
# owner's next scan re-armed in place (its writes keep landing, its
# role and journals unmoved, the fencing-loss ledger empty) and that
# the re-armed claim is real: a foreign attachment's conditional
# ensure and write probes stay fenced. The claim ops stay on the raw
# client — `claim_writer` is an op `dcs-plant-ctl` does not expose,
# and the tool's conditional claim could never preempt the owner into
# the window — while the census and field reads ride the shipped
# tool. The named diagnostics are unclaimed-rearm-failed and
# unclaimed-rearm-nondeterministic; two consecutive passes must
# produce identical digests.

UNCLAIMED_REARM_SETTLE = 30    # bound on the pair reporting settled
UNCLAIMED_REARM_POLL = 0.4     # cadence watching the owner mid-window
UNCLAIMED_REARM_ROUNDS = 6     # polls through the post-window watch
UNCLAIMED_REARM_DEADLINE = 15  # bound on the anchor and landing waits
# The dedicated attachment's foreign owner token — never a
# controller's pinned token nor the tool's "dcs-pltc": the preempting
# claim that opens the ownerless window behind the standing owner's
# live connection.
UNCLAIMED_REARM_FOREIGN = 0x7161_2d72_6561_726d  # "qa-rearm"


def _write_fenced(response):
    """Whether a write probe's answer is the point-level fencing
    verdict — `{"kind":"io","error":{"fenced":N}}` — the refusal the
    standing claim gives a non-holder's write (the `step` probe's
    named `fenced` kind has no point-level counterpart)."""
    error = (response or {}).get('error') or {}
    inner = error.get('error')
    return error.get('kind') == 'io' and isinstance(inner, dict) \
        and 'fenced' in inner


def _unclaimed_rearm_pass(ctx, active, peer, watch, probe_point):
    """One induction pass: the dedicated attachment's preempt-and-
    release opens the ownerless window behind the field owner's live
    connection, then the watch proves the recorded owner re-armed
    inline — its write landing, its role and journals unmoved — and
    the foreign probes prove the re-armed claim is real. Returns
    (digest, violations, evidence): digest is the pass's normalized
    verdict record, identical across clean passes; violations is
    {key: (diagnostic, detail)} in first-seen order."""
    base, peer_base = ctx[active], ctx[peer]
    violations = {}
    evidence = {'watch_point': watch, 'probe_point': probe_point}

    def note(key, diagnostic, detail):
        violations.setdefault(key, (diagnostic, detail))

    def failed(key, detail):
        note(key, 'unclaimed-rearm-failed', detail)

    # The audit positions the window must leave untouched: each peer's
    # journal floor, the fencing-loss ledger the owner's io_health
    # keeps, the watched field output's stamp, and the standing
    # claim's fencing answer.
    floors = {}
    for name in (active, peer):
        _, journal = http_json('GET', ctx[name] + '/journal')
        entries = _journal_list(journal)
        floors[name] = (entries[-1].get('seq') or 0) if entries else 0
    snap0 = _try_snapshot(ctx, base) or {}
    ledger0 = (snap0.get('io_health') or {}).get('failed_writes') or 0
    sample0 = _probe_sample(ctx, watch)
    probe0 = _try_plant(ctx, {'op': 'step', 'dt': 0})
    evidence['baseline'] = {'floors': floors, 'field': sample0,
                            'failed_writes': ledger0, 'probe': probe0,
                            'tick': snap0.get('tick')}
    if probe0 is None:
        raise ConnectionError('the simulated plant never answered '
                              'the baseline fencing probe')
    if not _fenced(probe0):
        failed('baseline-claim', 'the field held no standing writer '
               'claim at pass start — a third attachment\'s mutation '
               'probe answered ' + json.dumps(probe0)[:300])

    # The induction: claim_writer preempts unconditionally, the
    # same-breath release_writer empties the holder set — the field
    # sits unclaimed while the recorded owner's connection stays
    # live, the #621 reproduction's window.
    stream = _plant_connect(ctx)
    released = False
    try:
        claim = _plant_request(stream, {'op': 'claim_writer',
                                        'owner': UNCLAIMED_REARM_FOREIGN})
        evidence['claim'] = claim
        verdict = claim.get('result')
        if verdict == 'claimed_shared':
            note('claim-shared', 'unclaimed-rearm-nondeterministic',
                 'the preempting claim joined a live foreign holder '
                 '— a leaked attachment shares the induction token: '
                 + json.dumps(claim)[:200])
        elif verdict != 'done':
            raise ConnectionError('the preempting claim was refused: '
                                  + json.dumps(claim)[:300])
        release = _plant_request(stream, {'op': 'release_writer'})
        evidence['release'] = release
        released = release.get('result') == 'done'
        if not released:
            raise ConnectionError('the claim hand-back was refused: '
                                  + json.dumps(release)[:300])

        # The window observed from the released attachment itself:
        # `unclaimed` while the ownerless window still stands,
        # `fenced` once the owner's re-arm already landed — both the
        # fail-closed field; only a mutation answer is the defect.
        window = _plant_request(stream, {'op': 'step', 'dt': 0})
        evidence['window_probe'] = window
        kind = _probe_error(window)
        if kind not in ('unclaimed', 'fenced'):
            failed('window-open', 'the field accepted a third-party '
                   'mutation with no claim standing: '
                   + json.dumps(window)[:300])

        # The landing anchor: the watched output's stamp at release
        # time — the first post-window write must stamp past it, and
        # only the recorded owner's re-arm can land one.
        anchor = wait_for(lambda: _probe_sample(ctx, watch),
                          time.monotonic() + UNCLAIMED_REARM_DEADLINE,
                          interval=UNCLAIMED_REARM_POLL)
        if not isinstance(anchor, dict) \
                or not isinstance(anchor.get('tick'), int):
            raise ConnectionError('the field read anchoring the '
                                  'landing check never answered a '
                                  'stamped sample')
        anchor = anchor['tick']

        # The watch: the owner re-arms and its write lands, the pair's
        # roles hold, the ledger stays empty, and third-party probes
        # meet the fail-closed field throughout.
        rounds = []
        landed = role_moved = ledger_grew = False
        answered = 0
        last_field = anchor
        for index in range(UNCLAIMED_REARM_ROUNDS):
            report = _try_role(ctx, base)
            partner = _try_role(ctx, peer_base)
            snap = _try_snapshot(ctx, base)
            sample = _probe_sample(ctx, watch)
            probe = _try_plant(ctx, {'op': 'step', 'dt': 0})
            health = (snap or {}).get('io_health') or {}
            ftick = (sample or {}).get('tick')
            rounds.append({'owner': (report or {}).get('role'),
                           'peer': (partner or {}).get('role'),
                           'tick': (snap or {}).get('tick'),
                           'field_tick': ftick,
                           'failed_writes':
                               health.get('failed_writes'),
                           'probe': _probe_error(probe)})
            if report is None and partner is None and snap is None:
                if index + 1 < UNCLAIMED_REARM_ROUNDS:
                    time.sleep(UNCLAIMED_REARM_POLL)
                continue  # a dropped poll — one lost observation
            answered += 1
            if report is not None and report.get('role') != 'active':
                failed('owner-moved', 'the field owner left '
                       'role=active through the unclaimed window — '
                       'the refused write demoted it instead of '
                       're-arming inline: '
                       + json.dumps(report)[:300])
                role_moved = True
                break
            if partner is not None \
                    and partner.get('role') != 'standby':
                failed('peer-moved', 'the tracking peer reported a '
                       'spurious role change through the window: '
                       + json.dumps(partner)[:300])
                role_moved = True
                break
            if (health.get('failed_writes') or 0) > ledger0:
                ledger_grew = True
                failed('ledger-grew', 'the fencing-loss ledger grew '
                       'through the window — io_health.failed_writes '
                       + str(health.get('failed_writes'))
                       + ' over the baseline ' + str(ledger0))
            if probe is not None and not _fenced(probe) \
                    and _probe_error(probe) != 'unclaimed':
                failed('probe-wrote', 'a third-party mutation probe '
                       'wrote through during the watch: '
                       + json.dumps(probe)[:300])
            if isinstance(ftick, int) and isinstance(anchor, int) \
                    and ftick > anchor:
                landed = True
                last_field = ftick
            if index + 1 < UNCLAIMED_REARM_ROUNDS:
                time.sleep(UNCLAIMED_REARM_POLL)
        evidence['watch'] = rounds
        if answered == 0:
            raise ConnectionError('the pair\'s monitors never '
                                  'answered through the watch')
        if not landed:
            failed('write-stalled', 'the owner\'s write never landed '
                   'through the unclaimed window — the inline re-arm '
                   'dropped it or never ran (field tick held at '
                   + str(anchor) + ')')

        # The journal audit: no field_claim_lost and no role
        # transition above either peer's floor — the fencing-loss
        # ledger stays empty per the settled contract.
        journal_clean = True
        for name in (active, peer):
            try:
                _, journal = http_json(
                    'GET', ctx[name] + '/journal?since='
                    + str(floors[name]))
            except Exception as exc:
                journal_clean = False
                failed('journal-' + name, name + '\'s journal never '
                       'served the post-window audit: '
                       + str(exc)[:200])
                continue
            for entry in _journal_list(journal):
                event = entry.get('event') or {}
                if 'field_claim_lost' in event:
                    journal_clean = False
                    failed('claim-journal-' + name, name + ' journaled '
                           'field_claim_lost through the window — the '
                           'owner fenced instead of re-arming')
                if 'role_changed' in event:
                    journal_clean = False
                    failed('role-journal-' + name, name + ' journaled '
                           'a role transition through the window')

        # The re-arm produced a real claim: a fresh probe fences, the
        # foreign attachment's conditional ensure is refused, and its
        # write probe stays fenced — while the owner's writes keep
        # landing after them.
        fence = _try_plant(ctx, {'op': 'step', 'dt': 0})
        evidence['rearm_probe'] = fence
        if fence is None:
            raise ConnectionError('the plant never answered the '
                                  'post-window fencing probe')
        if _probe_error(fence) == 'unclaimed':
            failed('never-rearmed', 'the field stayed unclaimed — '
                   'the recorded owner never re-armed its claim')
        elif not _fenced(fence):
            failed('rearm-open', 'the field answered a third-party '
                   'probe without the re-armed claim\'s fencing: '
                   + json.dumps(fence)[:300])
        ensure = _plant_request(stream, {'op': 'ensure_writer',
                                         'owner': UNCLAIMED_REARM_FOREIGN})
        evidence['foreign_ensure'] = ensure
        if not _fenced(ensure):
            failed('foreign-ensure', 'a foreign attachment\'s '
                   'conditional claim was admitted past the re-armed '
                   'claim — the re-arm produced no real claim: '
                   + json.dumps(ensure)[:300])
        probe_sample = _probe_sample(ctx, probe_point)
        if not isinstance(probe_sample, dict):
            raise ConnectionError('the field read sizing the foreign '
                                  'write probe never answered')
        value = probe_sample.get('value') or {'bool': True}
        write = _plant_request(stream, {'op': 'write',
                                        'point': probe_point,
                                        'value': value})
        evidence['foreign_write'] = write
        if not _write_fenced(write):
            failed('foreign-write', 'a foreign attachment\'s write '
                   'probe was not fenced under the re-armed claim: '
                   + json.dumps(write)[:300])

        def advancing():
            _try_snapshot(ctx, base)  # the owner's scans keep running
            sample_ = _probe_sample(ctx, watch)
            if not isinstance(sample_, dict):
                return None
            return sample_ if isinstance(sample_.get('tick'), int) \
                and sample_['tick'] > last_field else None
        sustained = wait_for(advancing,
                             time.monotonic() + UNCLAIMED_REARM_DEADLINE,
                             interval=UNCLAIMED_REARM_POLL)
        evidence['sustained'] = sustained
        if sustained is None:
            failed('writes-stalled', 'the owner\'s writes stopped '
                   'landing after the foreign probes — the re-armed '
                   'claim fences its own owner')
    finally:
        stream.close()
        if not released:
            # Best effort: a stranded foreign claim would fence the
            # owner's re-arm — re-open the ownerless window through a
            # fresh attachment so the recorded owner can reclaim.
            try:
                restore = _plant_connect(ctx)
                try:
                    _plant_request(restore, {'op': 'claim_writer',
                                             'owner':
                                             UNCLAIMED_REARM_FOREIGN})
                    _plant_request(restore, {'op': 'release_writer'})
                finally:
                    restore.close()
            except Exception:
                pass  # an unreachable plant is the pass's own verdict
        # Best effort: the launch role layout for the legs behind this
        # one — a rig that demoted the owner or moved the peer gets
        # the settled active/standby pair back; a clean pass moves
        # nothing, so neither restore fires.
        if (_try_role(ctx, base) or {}).get('role') != 'active':
            try:
                http_json('POST', base + '/promote')
            except Exception:
                pass
        if (_try_role(ctx, peer_base) or {}).get('role') != 'standby':
            try:
                http_json('POST', peer_base + '/demote')
            except Exception:
                pass

    evidence['digest'] = {
        'baseline': 'fenced' if _fenced(probe0) else 'unfenced',
        'claim': 'shared' if verdict == 'claimed_shared'
                 else 'granted' if verdict == 'done' else 'refused',
        'window': 'closed' if kind in ('unclaimed', 'fenced')
                  else 'open',
        'writes': 'landed' if landed else 'stalled',
        'roles': 'unchanged' if not role_moved else 'moved',
        'ledger': 'empty' if not ledger_grew else 'grew',
        'journal': 'clean' if journal_clean else 'transitioned',
        'fence': ('fenced' if _fenced(fence)
                  else 'unclaimed'
                  if _probe_error(fence) == 'unclaimed' else 'open'),
        'foreign': 'fenced' if _fenced(ensure) else 'admitted',
        'foreign-write': 'fenced' if _write_fenced(write)
                         else 'landed',
        'sustained': 'landing' if sustained is not None else 'stalled'}
    return evidence['digest'], violations, evidence


def scenario_unclaimed_rearm(ctx):
    """Open the unclaimed window behind the field owner's live
    connection — a dedicated attachment's preempt-and-release — and
    prove the recorded owner's next scan re-arms inline: the write
    lands, the owner stays active, nothing journals, the ledger
    stays empty, and the re-armed claim fences foreign probes."""
    case = Case('unclaimed-rearm',
                'The unclaimed field re-arms the owner inline',
                'with the deployed pair settled and the field-owning '
                'peer holding the plant\'s writer claim, a dedicated '
                'attachment\'s claim_writer/release_writer opens the '
                'ownerless window behind the owner\'s live '
                'connection — the #621 reproduction — and the '
                'owner\'s next scan re-arms inline through one '
                'conditional ensure_writer: its field write keeps '
                'landing, it reports active throughout, no '
                'field_claim_lost or role transition journals, and '
                'the fencing-loss ledger stays empty; the re-armed '
                'claim then fences a foreign attachment\'s '
                'ensure_writer and write probes while the owner\'s '
                'writes keep landing, and the rig returns to its '
                'launch claim state and roles; two consecutive '
                'passes produce identical digests')
    try:
        if not ctx.get('plant') or ctx.get('plant_ctl') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no plant endpoint or '
                               'plant_ctl seam — the claim ops and '
                               'field reads cannot run')
        deadline = time.monotonic() + UNCLAIMED_REARM_SETTLE
        active = wait_for(lambda: _settled_active(ctx), deadline)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        peer = 'standby' if active == 'active' else 'active'
        report = wait_for(lambda: _try_role(ctx, ctx[peer]), deadline)
        if report is None:
            return case.finish('inconclusive', 'the pair\'s other '
                               'endpoint never answered /role — the '
                               'peer-stability half cannot run')
        if report.get('role') != 'standby':
            return case.finish('inconclusive', 'the pair never '
                               'settled — ' + peer + ' reports '
                               + str(report.get('role')))
        case.observe('field owner: ' + active + ' (' + ctx[active]
                     + '); watching peer ' + peer)

        field_out = _field_out_points(ctx)
        if not field_out:
            return case.finish('inconclusive', 'the simulated plant '
                               'serves no field output to watch the '
                               'owner\'s writes on')
        watch = min(field_out)
        field_in = sorted(_field_inputs(ctx))
        probe_point = field_in[0] if field_in else watch
        case.observe('watching field out point ' + str(watch)
                     + '; foreign write probe on point '
                     + str(probe_point))

        digests = []
        for number in (1, 2):
            digest, violations, evidence = _unclaimed_rearm_pass(
                ctx, active, peer, watch, probe_point)
            ref = save_evidence(ctx['evidence_dir'],
                                'unclaimed-rearm-pass-'
                                + str(number) + '.json', evidence)
            case.evidence('file', ref, 're-arm pass ' + str(number)
                          + ' — the induction, the watch rounds, the '
                          'fencing legs, and the normalized digest')
            if violations:
                failed = any(name == 'unclaimed-rearm-failed'
                             for name, _ in violations.values())
                diagnostic = 'unclaimed-rearm-failed' if failed \
                    else 'unclaimed-rearm-nondeterministic'
                return case.finish(
                    'failed', diagnostic + ': ' + '; '.join(
                        detail for _, detail in
                        list(violations.values())[:4]))
            digests.append(digest)
        if digests[0] != digests[1]:
            return case.finish(
                'failed', 'unclaimed-rearm-nondeterministic: the two '
                'passes\' digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two unclaimed-window passes, identical digests')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))


# --------------------------------------------------------------------
# WW-LCM-001's peer-lifecycle clauses and WW-OPS-003's tolerated
# interruption on the standby side — the pair coverage's asymmetric
# half: the restart and failover cases already exercise active loss
# (ordered demote/promote, restart recovery), this case exercises
# standby loss. Four legs on the settled pair: a receipted write to
# the tracking standby's monitor answers the named not_active
# rejection with no field effect; the runner-owned
# stop_controller/start_controller pair holds the standby's container
# down — the rig launches controllers with --restart no — while the
# active's scan, role, command path, and journal run undisturbed
# (peer loss is not an event the controller of record reacts to); the
# started standby returns to tracking inside the settle bound; and
# POST /promote fired at its monitor the moment it first answers —
# before its first transfer completes — answers the named
# not_converged refusal, the same promote succeeding once it tracks,
# after which the documented demote/promote order restores the
# pre-scenario role assignment for the cases behind this one.

STANDBY_LOSS_POLL = 0.5            # cadence watching the pair mid-window
STANDBY_LOSS_PROBE = 0.05          # cadence firing /promote at the
                                   # returning monitor — the probe must
                                   # land inside the unsynchronized
                                   # window before the paced cadence
                                   # applies the first checkpoint
STANDBY_LOSS_ROUNDS = 6            # down-window observation rounds
STANDBY_LOSS_REFUSAL_ROUNDS = 3    # post-refusal snapshot rounds
STANDBY_LOSS_RETURN_DEADLINE = 60  # bound on the returning monitor answering
STANDBY_LOSS_SETTLE_DEADLINE = 60  # bound on reconvergence and role settles


def scenario_standby_loss(ctx):
    """Standby loss and the role-gated refusals on the simulated rig:
    a write aimed at the tracking standby is refused not_active with
    no field effect; the standby's container held down never moves the
    active's scan, role, command path, or journal; the returned peer
    reconverges tracking, its first answered promote refuses
    not_converged, and the pair ends on its pre-scenario role
    assignment."""
    case = Case('standby-loss',
                'Standby loss never disturbs the controller of record',
                'a receipted write to the tracking standby\'s monitor '
                'answers the named not_active rejection with the point '
                'unchanged in the active\'s served snapshot, the write '
                'absent from both peers\' adopted receipt logs, and no '
                'journal entry recording it as anything but the '
                'refusal; the stopped standby\'s down-window leaves '
                'the active\'s snapshot tick advancing, its role '
                'active, a receipted command settling, and its journal '
                'free of promotion/demotion entries; the started '
                'standby returns to tracking inside the settle bound, '
                'POST /promote fired the moment its monitor first '
                'answers refuses the named not_converged, the same '
                'promote succeeds once it tracks, and demote/promote '
                'restores the pre-scenario role assignment')
    try:
        stop = ctx.get('stop_controller')
        start = ctx.get('start_controller')
        journals = ctx.get('journal_files') or {}
        if stop is None or start is None:
            return case.finish('inconclusive', 'the run context carries '
                               'no controller stop/start action — the '
                               'standby-loss induction has no '
                               'documented seam')
        # The field writer and its tracking standby — ctx keys, not
        # roles: 'active'/'standby' name the launched containers
        # whichever role each currently reports, so the legs hold on
        # either role layout.
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        if active not in ('active', 'standby'):
            return case.finish('inconclusive', 'the field writer is '
                               + active + ' — outside the launched pair '
                               'the stop action names')
        peer = 'standby' if active == 'active' else 'active'
        base, peer_base = ctx[active], ctx[peer]
        journal = journals.get(active)
        peer_journal = journals.get(peer)
        if journal is None or peer_journal is None:
            return case.finish('inconclusive', 'the run context carries '
                               'no journal-file paths for the pair')
        case.observe('field writer: ' + active + ' (' + base
                     + '); standby-loss target: ' + peer + ' ('
                     + peer_base + ')')

        def tracking(base_url):
            report = _try_role(ctx, base_url)
            if (report or {}).get('role') == 'standby' \
                    and 'tracking' in ((report or {}).get('sync') or {}):
                return report
            return None

        def settled_roles():
            """The pre-scenario role assignment: the original writer
            active, the loss target a tracking standby."""
            owner = _try_role(ctx, base)
            other = _try_role(ctx, peer_base)
            if (owner or {}).get('role') != 'active':
                return None
            if (other or {}).get('role') != 'standby' \
                    or 'tracking' not in ((other or {}).get('sync')
                                          or {}):
                return None
            return {'writer': owner, 'standby': other}

        def control(url, payload=None):
            """POST a control-plane request — /promote, /demote, and
            the refusal leg's /command — returning (status, body) with
            a refused call's named outcome decoded instead of raised."""
            try:
                return http_json('POST', url, payload)
            except urllib.error.HTTPError as exc:
                try:
                    body = json.loads(exc.read() or b'null')
                except ValueError:
                    body = None
                finally:
                    exc.close()
                return exc.code, body

        state = {'stopped': False, 'switched': False}

        def restore_roles():
            """The documented demote/promote order putting the
            pre-scenario role assignment back: the demoted writer
            reconverges tracking behind the promoted peer, the peer
            releases the field, and the tracking writer promotes.
            Returns the failure detail or None."""
            if wait_for(lambda: tracking(base),
                        time.monotonic()
                        + STANDBY_LOSS_SETTLE_DEADLINE,
                        interval=STANDBY_LOSS_POLL) is None:
                return 'the demoted field writer never reconverged ' \
                       'tracking — the restore has no promotable peer'
            status, body = control(peer_base + '/demote')
            if status != 200:
                return 'the restore demote answered ' + str(status) \
                       + ': ' + json.dumps(body)[:300]
            status, body = control(base + '/promote')
            if status != 200:
                return 'the restore promote answered ' + str(status) \
                       + ': ' + json.dumps(body)[:300]
            if wait_for(settled_roles,
                        time.monotonic()
                        + STANDBY_LOSS_SETTLE_DEADLINE,
                        interval=STANDBY_LOSS_POLL) is None:
                return 'the pair did not settle back to its ' \
                       'pre-scenario role assignment'
            return None

        # The tracked baseline: the peer must be a tracking standby —
        # the role-gated legs' observation point.
        if wait_for(lambda: tracking(peer_base),
                    time.monotonic() + 45,
                    interval=STANDBY_LOSS_POLL) is None:
            return case.finish('inconclusive', 'the ' + peer
                               + ' peer is not a tracking standby — '
                               'the role-gated legs have no '
                               'observation point')

        # The command target: the scenarios' writable bool point, read
        # on the field writer for the value the refused write flips.
        _, signals = http_json('GET', base + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'standby-loss-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming writable points')
        target = _writable_bool_point(signals)
        if target is None:
            return case.finish('inconclusive', 'no writable bool '
                               'point in the model')
        point = target['point']
        baseline = _point_value(_snapshot(ctx, base), point)
        if not isinstance(baseline, bool):
            return case.finish('inconclusive', 'point ' + str(point)
                               + ' serves no bool baseline to write '
                               'against')

        def attempt():
            # ---- leg (a): the role-gated command refusal ----
            command = {'command': {'write_value': {
                'point': point, 'kind': 'bool',
                'value': {'bool': not baseline}}},
                'actor': 'qa-lane'}
            status, receipt = control(peer_base + '/command', command)
            ref = save_evidence(ctx['evidence_dir'],
                                'standby-loss-refusal.json',
                                {'command': command, 'status': status,
                                 'receipt': receipt})
            case.evidence('file', ref, 'the standby-directed command\'s '
                          'answer')
            outcome = _outcome_key(receipt)
            case.observe('standby-directed write answered '
                         + str(status) + ' outcome ' + outcome)
            if status != 200 or outcome != 'rejected:not_active':
                return case.finish('failed', 'the standby-directed '
                                   'command did not answer the named '
                                   'not_active rejection: '
                                   + str(status) + ' '
                                   + json.dumps(receipt)[:300])

            # No field effect: the point stands at baseline in the
            # active's served snapshot across the refusal window.
            served = []
            violation = None
            for _ in range(STANDBY_LOSS_REFUSAL_ROUNDS):
                snap = _try_snapshot(ctx, base) or {}
                served.append({'tick': snap.get('tick'),
                               'value': _point_value(snap, point)})
                if served[-1]['value'] != baseline:
                    violation = ('the refused write reached the field: '
                                 'point ' + str(point) + ' serves '
                                 + str(served[-1]['value']))
                    break
                time.sleep(STANDBY_LOSS_POLL)
            # The audit: the write enters neither peer's adopted
            # receipt log; the active's journal carries no record of
            # it at all; the standby's echoes the named rejection
            # alone.
            _, active_served = http_json('GET', base + '/receipts')
            _, peer_served = http_json('GET', peer_base + '/receipts')
            leaked_a = [r for r in _receipt_list(active_served)
                        if r.get('command') == command['command']]
            leaked_p = [r for r in _receipt_list(peer_served)
                        if r.get('command') == command['command']]
            journaled_a = [_journal_settled(item) for item in
                           _journal_entries(journal)
                           if (_journal_settled(item) or {}).get(
                               'command') == command['command']]
            journaled_p = [r for r in (_journal_settled(item)
                                       for item in
                                       _journal_entries(peer_journal))
                           if (r or {}).get('command')
                           == command['command']]
            ref = save_evidence(
                ctx['evidence_dir'], 'standby-loss-refusal-audit.json',
                {'point': point, 'baseline': baseline,
                 'served': served,
                 'receipts_active': leaked_a,
                 'receipts_peer': leaked_p,
                 'journal_active': journaled_a,
                 'journal_peer': journaled_p})
            case.evidence('file', ref, 'the refused write\'s absence '
                          'from the served snapshot, receipt logs, '
                          'and journals')
            if violation is not None:
                return case.finish('failed', violation)
            if leaked_a or leaked_p:
                return case.finish('failed', 'the refused write '
                                   'entered a peer\'s adopted receipt '
                                   'log')
            if journaled_a:
                return case.finish('failed', 'the field writer\'s '
                                   'journal carries a command record '
                                   'for the refused write')
            misnamed = [r for r in journaled_p
                        if _outcome_key(r) != 'rejected:not_active']
            if misnamed:
                return case.finish('failed', 'the standby\'s journal '
                                   'records the refused write as '
                                   'something but the named '
                                   'rejection: '
                                   + json.dumps(misnamed[0])[:300])

            # ---- leg (b): standby-loss non-interference ----
            # The durable journal's baseline before the induction —
            # the diff over the down-window is the contract's proof
            # the controller of record never reacted to the peer loss.
            before_stop = _journal_entries(journal)
            # A receipted command on the active across the window —
            # the write that must still settle.
            window_command = {'command': {'write_value': {
                'point': point, 'kind': 'bool',
                'value': {'bool': baseline}}},
                'actor': 'qa-lane'}
            try:
                stop(peer)
            except Exception as exc:
                return case.finish('inconclusive', 'the standby-stop '
                                   'induction never completed: '
                                   + str(exc)[:300])
            state['stopped'] = True
            case.observe('standby container ' + peer + ' stopped')
            try:
                status, window_receipt = http_json(
                    'POST', base + '/command', window_command)
            except Exception as exc:
                window_receipt = None
                status = str(exc)[:200]
            window = []
            settled = None
            peer_up = False
            last_tick = None
            answered = 0
            for _ in range(STANDBY_LOSS_ROUNDS):
                role = _try_role(ctx, base)
                snap = _try_snapshot(ctx, base)
                tick = (snap or {}).get('tick')
                if role is not None:
                    answered += 1
                try:
                    _role(ctx, peer_base)
                    peer_up = True
                except Exception:
                    pass
                outcome = settled
                if outcome is None:
                    try:
                        _, receipts = http_json('GET',
                                                base + '/receipts')
                        matches = [r for r in _receipt_list(receipts)
                                   if r.get('command')
                                   == window_command['command']]
                        if matches and _outcome_key(matches[-1]) \
                                == 'applied':
                            outcome = 'applied'
                            settled = outcome
                    except Exception:
                        pass
                window.append({'role': (role or {}).get('role'),
                               'tick': tick, 'peer_up': peer_up,
                               'command': outcome})
                if role is not None and role.get('role') != 'active':
                    violation = ('the field writer reported '
                                 + str(role.get('role'))
                                 + ' while the standby was down')
                    break
                if tick is not None and last_tick is not None \
                        and tick <= last_tick:
                    violation = ('the active\'s scan stalled at tick '
                                 + str(tick)
                                 + ' while the standby was down')
                    break
                if tick is not None:
                    last_tick = tick
                time.sleep(STANDBY_LOSS_POLL)
            if isinstance(status, int) and status != 200:
                violation = violation or (
                    'the window command answered ' + str(status))
            elif not isinstance(status, int):
                violation = violation or (
                    'the window command never reached the field '
                    'writer: ' + status)
            elif isinstance(window_receipt, dict) and 'rejected' in \
                    (window_receipt.get('outcome') or {}):
                violation = violation or (
                    'the window command was refused while the '
                    'standby was down: '
                    + json.dumps(window_receipt)[:300])
            # The journal's diff over the window: no role or claim
            # transition, and the window command's settle recorded.
            after = _journal_entries(journal)
            gained = [item['entry'] for item in
                      after[len(before_stop):]
                      if isinstance(item.get('entry'), dict)]
            moved = [entry for entry in gained
                     if 'role_changed' in (entry.get('event') or {})
                     or 'field_claim_lost'
                     in (entry.get('event') or {})]
            journaled_settle = [entry for entry in gained
                                if (_journal_settled(
                                    {'entry': entry}) or {}).get(
                                        'command')
                                == window_command['command']]
            ref = save_evidence(
                ctx['evidence_dir'], 'standby-loss-window.json',
                {'target': peer, 'window': window,
                 'command': window_command,
                 'receipt': window_receipt,
                 'journal_gained': gained[:40],
                 'journal_moved': moved})
            case.evidence('file', ref, 'the down-window observations: '
                          'role, tick, command settle, and the active '
                          'journal\'s diff')
            if peer_up:
                return case.finish('inconclusive', 'the standby '
                                   'answered during the down-window — '
                                   'the stop did not hold a real loss')
            if answered == 0:
                return case.finish('inconclusive', 'the field writer '
                                   'never answered during the window — '
                                   'the induction\'s effect cannot be '
                                   'attributed')
            if violation is not None:
                return case.finish('failed', violation)
            if settled is None:
                return case.finish('failed', 'the window command '
                                   'never settled applied while the '
                                   'standby was down')
            if moved:
                return case.finish('failed', 'the controller of '
                                   'record journaled a role or claim '
                                   'transition for a peer loss it '
                                   'must not react to: '
                                   + json.dumps(moved[0])[:300])
            if not journaled_settle:
                return case.finish('failed', 'the window command '
                                   'settled but its command record '
                                   'never reached the journal')
            case.observe('the standby held down ' + str(len(window))
                         + ' rounds: the writer stayed active, its '
                         'tick advanced, the window command settled, '
                         'no role entries journaled')

            # ---- legs (c)+(d): return, reconvergence, the gate ----
            try:
                start(peer)
            except Exception as exc:
                return case.finish('inconclusive', 'the standby-start '
                                   'action never completed: '
                                   + str(exc)[:300])
            state['stopped'] = False
            case.observe('standby container ' + peer + ' started')
            # The promotion gate: POST /promote is itself the probe —
            # the first answered request lands the moment the
            # returning monitor serves, before the paced cadence has
            # applied a checkpoint, and the peer's standing is still
            # unsynchronized.
            first = None
            deadline = time.monotonic() + STANDBY_LOSS_RETURN_DEADLINE
            while time.monotonic() < deadline and first is None:
                try:
                    status, body = http_json('POST',
                                             peer_base + '/promote')
                    first = {'status': status, 'body': body}
                except urllib.error.HTTPError as exc:
                    try:
                        body = json.loads(exc.read() or b'null')
                    except ValueError:
                        body = None
                    finally:
                        exc.close()
                    first = {'status': exc.code, 'body': body}
                except Exception:
                    time.sleep(STANDBY_LOSS_PROBE)
            ref = save_evidence(ctx['evidence_dir'],
                                'standby-loss-promote-gate.json',
                                {'first_promote': first})
            case.evidence('file', ref, 'the first promote the '
                          'returning standby answered')
            if first is None:
                return case.finish('inconclusive', 'the returned '
                                   'standby\'s monitor never answered '
                                   'inside '
                                   + str(STANDBY_LOSS_RETURN_DEADLINE)
                                   + 's')
            if first['status'] == 200:
                # Promoted without the named refusal ever preceding —
                # the gate the issue asserts was never exercised. The
                # pair is already switched; restore it, then fail.
                state['switched'] = True
                restore_detail = restore_roles()
                if restore_detail is None:
                    state['switched'] = False
                detail = ('the returning standby promoted on its '
                          'first answered request — no not_converged '
                          'refusal preceded the switch')
                if restore_detail is not None:
                    detail += '; the restore also failed: ' \
                              + restore_detail
                return case.finish('failed', detail)
            named = (first['body'] or {}).get('not_converged') \
                if isinstance(first['body'], dict) else None
            if first['status'] != 409 or not isinstance(named, dict):
                return case.finish('failed', 'the first promote on '
                                   'the returning standby answered '
                                   + str(first['status']) + ' '
                                   + json.dumps(first['body'])[:300]
                                   + ' — not the named not_converged '
                                   'refusal')
            case.observe('promotion gate held: the returning '
                         'standby\'s first promote answered '
                         'not_converged')

            # Reconvergence: the returned standby's paced pulls carry
            # it to tracking inside the settle bound.
            reconverged = wait_for(
                lambda: tracking(peer_base),
                time.monotonic() + STANDBY_LOSS_SETTLE_DEADLINE,
                interval=STANDBY_LOSS_POLL)
            ref = save_evidence(ctx['evidence_dir'],
                                'standby-loss-reconverged.json',
                                reconverged)
            case.evidence('file', ref, 'the returned standby\'s '
                          'tracking report')
            if reconverged is None:
                return case.finish('failed', 'the returned standby '
                                   'never reached tracking '
                                   'convergence inside '
                                   + str(STANDBY_LOSS_SETTLE_DEADLINE)
                                   + 's')

            # Once tracking, promotion succeeds — the documented
            # switch: demote the standing writer, then promote the
            # converged peer.
            status, body = control(base + '/demote')
            if status != 200:
                return case.finish('failed', 'the switch demote '
                                   'answered ' + str(status) + ': '
                                   + json.dumps(body)[:300])
            status, body = control(peer_base + '/promote')
            if status != 200:
                return case.finish('failed', 'the converged peer\'s '
                                   'promote answered ' + str(status)
                                   + ': ' + json.dumps(body)[:300])
            state['switched'] = True

            def promoted_role():
                report = _try_role(ctx, peer_base)
                if (report or {}).get('role') == 'active':
                    return report
                return None

            promoted = wait_for(
                promoted_role,
                time.monotonic() + STANDBY_LOSS_SETTLE_DEADLINE,
                interval=STANDBY_LOSS_POLL)
            ref = save_evidence(ctx['evidence_dir'],
                                'standby-loss-switched.json',
                                {'promoted': promoted})
            case.evidence('file', ref, 'the promoted peer settling '
                          'active')
            if promoted is None:
                return case.finish('failed', 'the promoted peer '
                                   'never settled active')

            # Restore: demote/promote returns the pair to its
            # pre-scenario role assignment for the cases behind this
            # one.
            detail = restore_roles()
            if detail is None:
                state['switched'] = False
            final = settled_roles()
            ref = save_evidence(ctx['evidence_dir'],
                                'standby-loss-restored.json', final)
            case.evidence('file', ref, 'the restored role assignment')
            if detail is not None:
                return case.finish('failed', 'the pair was not '
                                   'restored to its pre-scenario role '
                                   'assignment: ' + detail)
            if final is None:
                return case.finish('failed', 'the pair did not '
                                   'return to its pre-scenario role '
                                   'assignment')
            return case.finish('passed')

        try:
            return attempt()
        finally:
            # Whatever the legs left behind on an early exit — a
            # stopped peer or a switched pair — put it back for the
            # cases behind this one, best-effort.
            if state['stopped']:
                try:
                    start(peer)
                    case.observe('cleanup: standby container '
                                 + peer + ' restarted')
                except Exception as exc:
                    case.observe('cleanup: the standby restart '
                                 'failed: ' + str(exc)[:200])
            if state['switched']:
                try:
                    detail = restore_roles()
                except Exception as exc:
                    detail = str(exc)[:200]
                case.observe('cleanup: role restore '
                             + (detail or 'completed'))
    except Exception as exc:
        return case.finish('inconclusive', str(exc))


# --------------------------------------------------------------------
# The single-terminal-outcome command settlement contract (WW-FND-004's
# receipted-command clause, WW-LCM-001's audit clause) pinned on the rig
# the #685 defect was demonstrated on: a command raced against a demote
# used to settle both rejected{superseded} and applied — one admission,
# two contradictory terminal outcomes. The verified #687 contract
# settles exactly one: the demoted run's pending queue suspends at the
# write gate's close, so a still-`Accepted` admission either rides the
# promote boundary's carry and settles `applied` on the promoted run's
# scan boundary, or the adopted window's submission high-water passes
# it and it settles the named `superseded` rejection — never both,
# never vanished. The leg races receipted writable-point submissions
# against the documented demote on the field-owning peer across
# repeated switch cycles, then audits every admission against both
# peers' journals, adopted receipt logs, and served images: exactly one
# `command_settled` outcome per admission — applied or superseded — the
# same single outcome in each peer's adopted log, and the image
# reflecting at most one application. The named diagnostics are
# demote-settle-uniqueness-failed — the contract never performed: a
# refused switch step, an admission that never reaches a terminal
# journaled outcome — and demote-settle-uniqueness-nondeterministic —
# the run produced an outcome the contract declares impossible: two
# terminal outcomes on one admission, a peer journaling the settle
# twice, diverged adopted logs, a phantom application, or two passes
# disagreeing. The even cycle count lands the pair back on its entry
# roles; two consecutive passes produce identical digests.

DEMOTE_SETTLE_CYCLES = 2       # switch cycles per pass — even, so the
                               # entry role layout restores by
                               # construction
DEMOTE_SETTLE_ADMISSIONS = 3   # receipted submissions raced per cycle
DEMOTE_SETTLE_SETTLE = 45      # bound on each demote/promote and the
                               # pair's reconvergence
DEMOTE_SETTLE_AUDIT = 30       # bound on an admission's terminal
                               # journaled outcome
DEMOTE_SETTLE_POLL = 0.4       # wait cadence inside the leg


def _pair_active(ctx):
    """The launched pair's field-owning endpoint key, or None —
    _settled_active scoped to the pair: a third endpoint's active is
    not this leg's race target."""
    for name in ('active', 'standby'):
        if ctx.get(name) is None:
            continue
        try:
            if _role(ctx, ctx[name]).get('role') == 'active':
                return name
        except Exception:
            pass
    return None


def _tracking_standby(ctx, name):
    """The endpoint's report while it is a tracking standby — the
    promotable posture a raced switch needs — else None."""
    try:
        report = _role(ctx, ctx[name])
    except Exception:
        return None
    if report.get('role') == 'standby' \
            and 'tracking' in (report.get('sync') or {}):
        return report
    return None


def _settle_call(url):
    """POST a control-plane request — /demote, /promote — answering
    (status, body) with a refused call's named outcome decoded rather
    than raised."""
    try:
        return http_json('POST', url)
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read() or b'null')
        except ValueError:
            body = None
        finally:
            exc.close()
        return exc.code, body


def _writable_bool_points(signals, count):
    """Up to `count` distinct writable bool `in` points a SignalIndex
    serves — the raced submissions' targets, the scenarios' p101-oos
    point first when the model declares it."""
    ranked = []
    for entry in signals.get('points', []):
        if entry.get('writable') and entry.get('direction') == 'in' \
                and entry.get('value_type') == 'bool' \
                and isinstance(entry.get('point'), int):
            ranked.append((0 if entry.get('name') == 'p101-oos' else 1,
                           entry['point']))
    return [point for _, point in sorted(ranked)][:count]


def _admission_hit(receipt, admission):
    """Whether a served or journaled receipt is the raced admission —
    the (command, actor) pair is unique per raced submission."""
    return isinstance(receipt, dict) \
        and receipt.get('command') == admission['command'] \
        and receipt.get('actor') == admission['actor']


def _settle_window(ctx, admission, floors):
    """One polled audit snapshot for a raced admission: each launched
    peer's journaled command_settled receipts, adopted-log receipts,
    and served image value for the admission's point since the pass's
    journal floors. None while either peer's monitor drops a read — a
    lost observation, never the audit's verdict."""
    window = {'journaled': {}, 'logged': {}, 'image': {}}
    for name in ('active', 'standby'):
        try:
            _, journal = http_json('GET', ctx[name] + '/journal?since='
                                   + str(floors[name]))
            _, receipts = http_json('GET', ctx[name] + '/receipts')
            snapshot = _snapshot(ctx, ctx[name])
        except Exception:
            return None
        window['journaled'][name] = [
            receipt for receipt in
            (_journal_settled(entry) for entry in _journal_list(journal))
            if _admission_hit(receipt, admission)]
        window['logged'][name] = [
            receipt for receipt in _receipt_list(receipts)
            if _admission_hit(receipt, admission)]
        window['image'][name] = _point_value(snapshot, admission['point'])
    return window


def _settle_resolved(window, admission):
    """Whether the audit snapshot is terminal for the admission: the
    demoted peer's journal has reached an outcome and the promoted
    peer's evidence agrees — or a contradiction is already showing,
    which no further wait can heal."""
    demoted, promoted = admission['demoted'], admission['promoted']
    counts = {name: len(entries)
              for name, entries in window['journaled'].items()}
    outcomes = {_outcome_key(receipt)
                for entries in window['journaled'].values()
                for receipt in entries}
    if not outcomes:
        return False
    if len(outcomes) != 1:
        return True           # two terminal outcomes — the defect
    outcome = next(iter(outcomes))
    if outcome not in ('applied', 'rejected:superseded'):
        return True           # an out-of-contract outcome is terminal
    if any(_outcome_key(receipt) not in (outcome, 'accepted')
           for entries in window['logged'].values()
           for receipt in entries):
        return True           # the adopted logs already contradict
                              # the journaled outcome
    if outcome == 'applied':
        # Applied journals once on each peer — the settler's own
        # boundary plus the adopted record — and each adopted log
        # carries the receipt settled. Over-journaling is terminal
        # too: a settle a peer journaled twice no wait heals.
        if counts[demoted] > 1 or counts[promoted] > 1:
            return True
        return counts[demoted] == 1 and counts[promoted] == 1 \
            and len(window['logged'][demoted]) == 1 \
            and len(window['logged'][promoted]) == 1 \
            and all(_outcome_key(receipt) == 'applied'
                    for name in (demoted, promoted)
                    for receipt in window['logged'][name])
    # Superseded journals once on the demoted peer alone — the
    # promoted run never held the admission — while no adopted log may
    # still hold the admission pending.
    if counts[promoted] > 0 or counts[demoted] > 1:
        return True
    return counts[demoted] == 1 \
        and not any(_outcome_key(receipt) == 'accepted'
                    for entries in window['logged'].values()
                    for receipt in entries)


def _settle_judge(window, admission, note, check_image=True):
    """The admission's audit verdict once its window resolved —
    records a violation for every clause the evidence breaks and
    answers 'single' or 'diverged' for the digest."""
    demoted = admission['demoted']
    label = 'admission ' + str(admission['actor']) + ' (point ' \
        + str(admission['point']) + ')'
    outcomes = {_outcome_key(receipt)
                for entries in window['journaled'].values()
                for receipt in entries}
    if len(outcomes) != 1:
        note('contradictory-' + admission['actor'],
             'demote-settle-uniqueness-nondeterministic',
             label + ' journaled ' + json.dumps(sorted(outcomes))
             + ' — one admission, never more than one terminal '
             'outcome')
        return 'diverged'
    outcome = next(iter(outcomes))
    if outcome not in ('applied', 'rejected:superseded'):
        note('outcome-' + admission['actor'],
             'demote-settle-uniqueness-nondeterministic',
             label + ' settled ' + outcome + ' — neither applied nor '
             'the named superseded rejection')
        return 'diverged'
    verdict = 'single'
    # The journal coverage: the demoted peer journals the settle once
    # either way; the promoted peer journals it once only when the
    # line applied it — it never held a superseded admission.
    for name in ('active', 'standby'):
        count = len(window['journaled'][name])
        want = 1 if name == demoted or outcome == 'applied' else 0
        if count != want:
            verdict = 'diverged'
            note('journal-' + admission['actor'] + '-' + name,
                 'demote-settle-uniqueness-nondeterministic',
                 label + ' journaled ' + str(count)
                 + ' command_settled records on ' + name
                 + ' — the contract settles exactly ' + str(want)
                 + ' there')
    # The adopted logs: an applied admission's receipt settles applied
    # in both peers' logs; a superseded admission carries no pending
    # copy anywhere — the same single outcome in each peer's log.
    for name in ('active', 'standby'):
        logged = window['logged'][name]
        if len(logged) > 1:
            verdict = 'diverged'
            note('log-count-' + admission['actor'] + '-' + name,
                 'demote-settle-uniqueness-nondeterministic',
                 name + "'s adopted log carries " + str(len(logged))
                 + ' receipts for ' + label)
        for receipt in logged:
            if _outcome_key(receipt) != outcome:
                verdict = 'diverged'
                note('log-outcome-' + admission['actor'] + '-' + name,
                     'demote-settle-uniqueness-nondeterministic',
                     name + "'s adopted log carries "
                     + _outcome_key(receipt) + ' for ' + label
                     + ' where the journal settled ' + outcome)
        if outcome == 'applied' and not logged:
            verdict = 'diverged'
            note('log-missing-' + admission['actor'] + '-' + name,
                 'demote-settle-uniqueness-nondeterministic',
                 name + "'s adopted log lost " + label
                 + ' the journal settled applied')
    # The image: the applied admission's value present on both peers,
    # the superseded admission's value on neither — at most one
    # application per raced admission.
    if check_image:
        for name in ('active', 'standby'):
            value = window['image'][name]
            if outcome == 'applied' and value != admission['value']:
                verdict = 'diverged'
                note('image-missing-' + admission['actor'] + '-'
                     + name,
                     'demote-settle-uniqueness-nondeterministic',
                     name + "'s image never took " + label
                     + "'s applied write — serves "
                     + json.dumps(value))
            if outcome == 'rejected:superseded' \
                    and value == admission['value']:
                verdict = 'diverged'
                note('image-phantom-' + admission['actor'] + '-'
                     + name,
                     'demote-settle-uniqueness-nondeterministic',
                     name + "'s image carries " + label
                     + "'s superseded value — an application the "
                     'journal never settled')
    return verdict


def _demote_settle_pass(ctx, number, entry_owner, points):
    """One raced-switch pass: DEMOTE_SETTLE_CYCLES cycles each racing
    DEMOTE_SETTLE_ADMISSIONS receipted writable-point submissions
    against the documented demote on the field-owning peer, then the
    per-admission audit — exactly one journaled terminal outcome, the
    same single outcome in both peers' adopted logs, at most one image
    application. Returns (digest, violations, evidence): the digest is
    the pass's normalized verdict record, identical across clean
    passes."""
    violations = {}
    evidence = {'entry_owner': entry_owner}
    admissions = []
    cycles = []

    def note(key, diagnostic, detail):
        violations.setdefault(key, (diagnostic, detail))

    def failed(key, detail):
        note(key, 'demote-settle-uniqueness-failed', detail)

    # The journal floors the audit reads from — a peer that cannot
    # serve its journal cannot evidence the contract.
    floors = {}
    try:
        for name in ('active', 'standby'):
            _, journal = http_json('GET', ctx[name] + '/journal')
            entries = _journal_list(journal)
            floors[name] = (entries[-1].get('seq') or 0) \
                if entries else 0
    except Exception as exc:
        failed('floors', 'a peer\'s journal floor never served: '
               + str(exc)[:200])
        floors = None

    for cycle in range(DEMOTE_SETTLE_CYCLES):
        if violations:
            break
        record = {'cycle': cycle}
        cycles.append(record)
        deadline = time.monotonic() + DEMOTE_SETTLE_SETTLE
        owner = wait_for(lambda: _pair_active(ctx), deadline,
                         interval=DEMOTE_SETTLE_POLL)
        if owner not in ('active', 'standby'):
            failed('owner-' + str(cycle), 'no launched peer reports '
                   'role=active for cycle ' + str(cycle)
                   + ' — the race has no field owner')
            break
        peer = 'standby' if owner == 'active' else 'active'
        record['owner'], record['peer'] = owner, peer
        base, peer_base = ctx[owner], ctx[peer]
        if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                    interval=DEMOTE_SETTLE_POLL) is None:
            failed('tracking-' + str(cycle), peer + ' is not a '
                   'tracking standby — the raced promote has no '
                   'converged target')
            break
        # The cycle's targets and the value each flips to — a distinct
        # slice per cycle keeps every raced admission its point's only
        # writer.
        slice_ = points[cycle * DEMOTE_SETTLE_ADMISSIONS:
                        (cycle + 1) * DEMOTE_SETTLE_ADMISSIONS]
        snapshot = _try_snapshot(ctx, base) or {}
        targets = []
        for index, point in enumerate(slice_):
            baseline = _point_value(snapshot, point)
            if not isinstance(baseline, bool):
                baseline = False
            value = not baseline
            targets.append({
                'point': point, 'value': value, 'baseline': baseline,
                'command': {'write_value': {'point': point,
                                            'kind': 'bool',
                                            'value': {'bool': value}}},
                'actor': 'qa-lane-settle-' + str(number) + '-'
                         + str(cycle) + '-' + str(index),
                'demoted': owner, 'promoted': peer})
        # The race: the receipted submissions land back-to-back on the
        # still-active owner, the documented demote on their heels.
        submissions = []
        for target in targets:
            try:
                status, receipt = http_json(
                    'POST', base + '/command',
                    {'command': target['command'],
                     'actor': target['actor']})
            except Exception as exc:
                status, receipt = None, None
                failed('submit-' + target['actor'],
                       'the raced submission never answered: '
                       + str(exc)[:200])
            submissions.append({'actor': target['actor'],
                                'status': status, 'receipt': receipt})
            if status == 200 \
                    and _outcome_key(receipt) == 'accepted':
                admissions.append(target)
        record['submissions'] = submissions
        record['raced'] = sum(
            1 for submission in submissions
            if _outcome_key(submission.get('receipt')) == 'accepted')
        if record['raced'] == 0 and not violations:
            failed('admissions-' + str(cycle), 'cycle ' + str(cycle)
                   + ' raced no admitted command — every submission '
                   'missed the role gate')
            break
        if violations:
            break
        status, demote = _settle_call(base + '/demote')
        record['demote'] = {'status': status, 'body': demote}
        if status != 200:
            failed('demote-' + str(cycle), 'the field owner\'s demote '
                   'answered ' + str(status) + ': '
                   + json.dumps(demote)[:300])
            break
        # The converged peer promotes — the documented order, mid-
        # transition refusals retried inside the settle bound.
        promoted, last = None, None
        while time.monotonic() < deadline and promoted is None:
            status, body = _settle_call(peer_base + '/promote')
            if status == 200:
                promoted = body
            else:
                last = (status, body)
                time.sleep(DEMOTE_SETTLE_POLL)
        record['promote'] = {'body': promoted, 'last_refusal': last}
        if promoted is None:
            failed('promote-' + str(cycle), 'the converged peer\'s '
                   'promote never succeeded: '
                   + json.dumps(last)[:300])
            break
        # The switch settles: the promoted peer reports active, the
        # demoted one reconverges tracking behind it — the adopted
        # line the audit reads.
        settled = wait_for(
            lambda: (_pair_active(ctx) == peer or None)
            and _tracking_standby(ctx, owner),
            deadline, interval=DEMOTE_SETTLE_POLL)
        record['settled'] = settled
        if settled is None:
            failed('settle-' + str(cycle), 'the switch never settled: '
                   'the promoted peer\'s role or the demoted peer\'s '
                   'reconvergence missed the bound')
            break

    evidence['cycles'] = cycles
    evidence['admissions'] = [
        {'actor': admission['actor'], 'point': admission['point'],
         'value': admission['value'], 'baseline': admission['baseline'],
         'demoted': admission['demoted'],
         'promoted': admission['promoted']}
        for admission in admissions]

    # The audit: every raced admission resolves to exactly one
    # terminal outcome — applied on a scan boundary or the named
    # superseded rejection — journaled once, carried identically in
    # both peers' adopted logs, and applied to the image at most once.
    audits = []
    if floors is not None:
        for index, admission in enumerate(admissions):
            deadline = time.monotonic() + DEMOTE_SETTLE_AUDIT
            window = wait_for(
                lambda: (lambda w: w if w is not None
                         and _settle_resolved(w, admission)
                         else None)(
                    _settle_window(ctx, admission, floors)),
                deadline, interval=DEMOTE_SETTLE_POLL)
            audits.append({'actor': admission['actor'],
                           'window': window})
            if window is None:
                failed('settled-' + admission['actor'],
                       'admission ' + str(admission['actor'])
                       + ' (point ' + str(admission['point'])
                       + ') never reached a terminal journaled '
                       'outcome inside ' + str(DEMOTE_SETTLE_AUDIT)
                       + 's')
                continue
            # The image check holds for the pass's last writer of each
            # point — an earlier superseded value a later admission
            # rewrote is the later one's evidence.
            last_writer = all(
                other['point'] != admission['point']
                for other in admissions[index + 1:])
            admission['verdict'] = _settle_judge(
                window, admission, note, check_image=last_writer)
        for record in evidence['admissions']:
            record['verdict'] = next(
                (admission.get('verdict') for admission in admissions
                 if admission['actor'] == record['actor']), None)
    evidence['audit'] = audits
    evidence['violations'] = {
        key: diagnostic for key, (diagnostic, _)
        in violations.items()}
    restored = _pair_active(ctx) == entry_owner
    evidence['digest'] = {
        'cycles': [{'owner': record.get('owner'),
                    'raced': record.get('raced', 0)}
                   for record in cycles],
        'outcomes': 'single'
                    if floors is not None and not violations
                    and len(admissions)
                    == sum(record.get('raced', 0) for record in cycles)
                    and all(admission.get('verdict') == 'single'
                            for admission in admissions)
                    else 'diverged',
        'roles': 'restored' if restored else 'switched'}
    return evidence['digest'], violations, evidence


def scenario_demote_settle_uniqueness(ctx):
    """Race receipted writable-point submissions against the documented
    demote on the field-owning peer across repeated switch cycles —
    the #685 defect's rig replay: every admission must journal exactly
    one command_settled outcome — applied at a scan boundary or the
    named superseded rejection, never both — with both peers' adopted
    receipt logs carrying the same single outcome and the image
    reflecting at most one application per raced admission."""
    case = Case(
        'demote-settle-uniqueness',
        'Raced commands settle one terminal outcome',
        'against the deployed pair, receipted writable-point '
        'submissions raced against the documented demote on the '
        'field-owning peer across ' + str(DEMOTE_SETTLE_CYCLES)
        + ' switch cycles per pass journal exactly one '
        'command_settled outcome per admission — applied at a scan '
        'boundary or the named superseded rejection, never both — '
        'both peers\' adopted receipt logs carry the same single '
        'outcome per admission, the served image reflects at most one '
        'application per raced admission, the pair returns to its '
        'entry role layout, and two passes produce identical digests')
    try:
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries only one endpoint — the pair '
                               'the race needs is absent')
        for name in ('active', 'standby'):
            try:
                _role(ctx, ctx[name])
            except Exception as exc:
                return case.finish('inconclusive', name + '\'s '
                                   'monitor is unreachable: '
                                   + str(exc)[:200])
        deadline = time.monotonic() + DEMOTE_SETTLE_SETTLE
        owner = wait_for(lambda: _pair_active(ctx), deadline,
                         interval=DEMOTE_SETTLE_POLL)
        if owner is None:
            return case.finish('failed', 'no peer reports role=active')
        peer = 'standby' if owner == 'active' else 'active'
        if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                    interval=DEMOTE_SETTLE_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — the raced promote '
                               'has no converged target')
        case.observe('field owner: ' + owner + ' (' + ctx[owner]
                     + '); racing the demote against ' + peer)
        _, signals = http_json('GET', ctx[owner] + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'demote-settle-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming the raced '
                      'writable points')
        points = _writable_bool_points(
            signals, DEMOTE_SETTLE_CYCLES * DEMOTE_SETTLE_ADMISSIONS)
        if len(points) < DEMOTE_SETTLE_ADMISSIONS:
            return case.finish('inconclusive', 'the model declares '
                               'fewer writable bool in-points than a '
                               'cycle races')
        digests = []
        try:
            for number in (1, 2):
                digest, violations, evidence = _demote_settle_pass(
                    ctx, number, owner, points)
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'demote-settle-pass-' + str(number) + '.json',
                    evidence)
                case.evidence('file', ref, 'raced-switch pass '
                              + str(number) + ' — the submissions, '
                              'switch answers, per-admission audit, '
                              'and normalized digest')
                if violations:
                    diagnostic = 'demote-settle-uniqueness-failed' \
                        if any(name
                               == 'demote-settle-uniqueness-failed'
                               for name, _ in violations.values()) \
                        else \
                        'demote-settle-uniqueness-nondeterministic'
                    return case.finish(
                        'failed', diagnostic + ': ' + '; '.join(
                            detail for _, detail in
                            list(violations.values())[:4]))
                digests.append(digest)
        finally:
            # The pair's entry layout for the cases behind this one —
            # even cycles restore it by construction; a mid-pass exit
            # gets the documented order run again, best-effort.
            current = _pair_active(ctx)
            other = 'standby' if owner == 'active' else 'active'
            if current != owner \
                    and _tracking_standby(ctx, owner) is not None:
                try:
                    if current is not None:
                        _settle_call(ctx[current] + '/demote')
                    _settle_call(ctx[owner] + '/promote')
                    wait_for(
                        lambda: (_pair_active(ctx) == owner or None)
                        and _tracking_standby(ctx, other),
                        time.monotonic() + DEMOTE_SETTLE_SETTLE,
                        interval=DEMOTE_SETTLE_POLL)
                    case.observe('cleanup: restored the entry role '
                                 'layout')
                except Exception as exc:
                    case.observe('cleanup: role restore failed: '
                                 + str(exc)[:200])
        if digests[0] != digests[1]:
            return case.finish(
                'failed', 'demote-settle-uniqueness-nondeterministic: '
                'the two passes\' digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two raced-switch passes, identical digests')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))


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
# case's a->b switch. The
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
# the field-owning peer. The unclaimed-rearm case is the same shape:
# its preempt-and-release induction opens the ownerless window behind
# whichever peer owns the field, watches the recorded owner's inline
# re-arm and the fencing it restores, and leaves the claim state and
# launch roles as found. The unavailable-fallback case is
# self-contained on either role layout as well: it drives per-point
# faults on the shared field through the shipped plant tool and
# clears them all in teardown. The dcs-ctl case closes the schedule:
# it observes the post-failover role layout and perturbs nothing
# earlier cases established.
SCENARIOS = (scenario_controller_active, scenario_standby_tracking,
             scenario_operator_command, scenario_controller_restart,
             scenario_source_restart, scenario_stale_freshness,
             scenario_field_claim, scenario_fenced_writer_degrade,
             scenario_dead_peer_latency,
             scenario_monitor_starvation, scenario_duty_rotation,
             scenario_force_carryover,
             scenario_backup_health, scenario_source_failover,
             scenario_lag_staging,
             scenario_standby_loss,
             scenario_demote_settle_uniqueness,
             scenario_parameter_tune_carryover, scenario_failover,
             scenario_checkpoint_negotiation,
             scenario_doomed_startup_claim,
             scenario_incompatible_revision, scenario_model_revision,
             scenario_evidence_capture, scenario_served_interface,
             scenario_event_retention,
             scenario_force_release, scenario_consumer_schedule,
             scenario_command_admission, scenario_command_availability,
             scenario_plant_link_loss,
             scenario_field_fault, scenario_unclaimed_rearm,
             scenario_unavailable_fallback, scenario_dcs_ctl)


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
