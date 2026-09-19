"""Deterministic acceptance scenarios for the simulated QA rig.

Each scenario drives the redundant controller pair through the monitor
endpoints documented in docs/packaging.md (GET /role, /signals,
/snapshot, /receipts, /journal, /schema, /resources; POST /command,
/demote, /promote) and returns one report-schema scenario case. Stdlib
only — the Lenovo host needs nothing but Python and Docker. The
restart scenario also triggers the runner-owned container lifecycle
action ctx['restart_controller'] carries and reads the per-controller
--journal-file the rig bind-mounts into the run directory; the
model-revision scenario likewise triggers ctx['start_revised'] — the
runner action that derives the recipe's revised model and launches the
run's third controller on it — and reads field-side truth off the
simulated plant's sim-net service at ctx['plant']. The
checkpoint-negotiation scenario triggers ctx['start_foreign'] — the
same derivation launched --standby <active> WITHOUT --revised so the
fingerprint gate must refuse it — and removes the peer through
ctx['stop_foreign']. The link-loss
scenario drives the runner-owned plant stop/start actions
ctx['stop_plant']/ctx['start_plant'] carry and probes the run's plant
server directly on ctx['plant'] — the field's own fencing evidence.

The field-fault case additionally opens one plant-protocol connection
to the run's published plant port — the newline-JSON request/response
surface crates/dcs-sim-net/src/protocol.rs documents — to inject and
clear per-point faults on the shared simulated field.

Evidence is written into the run's evidence/ directory as each response
arrives, so a killed run still leaves inspectable artifacts behind.
"""
import json
import math
import os
import socket
import subprocess
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


def _field_request(ctx, request):
    """One newline-delimited request against the simulated plant's
    sim-net service — the field-side truth below the monitor layer.
    `ctx['plant']` carries the plant's loopback host:port."""
    host, _, port = str(ctx['plant']).rpartition(':')
    stream = socket.create_connection((host or '127.0.0.1',
                                       int(port or 0)), timeout=5)
    try:
        stream.sendall(json.dumps(request).encode() + b'\n')
        data = b''
        while not data.endswith(b'\n'):
            chunk = stream.recv(65536)
            if not chunk:
                break
            data += chunk
    finally:
        stream.close()
    return json.loads(data) if data else None


def _field_sample(ctx, point):
    """The plant's stored sample for `point` — `{'value', 'quality',
    'tick'}` — or None when the read dropped; a lost observation, never
    the leg's verdict."""
    try:
        body = _field_request(ctx, {'op': 'read', 'point': point})
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    return body.get('sample')


def _field_out_points(ctx):
    """Every field `out` point the simulated plant serves — the points
    a field-owning scan writes — from the plant's own census."""
    body = _field_request(ctx, {'op': 'list_points'})
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
                     + ' components reinitialized')

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
# process lifetimes, and the pair settles back to active/standby.


def scenario_controller_restart(ctx):
    """Stop the active peer's container and restart it: the run resumes
    from --state-file rather than cold-starting."""
    case = Case('controller-restart',
                'Restarted controller resumes its persisted run',
                'stopping and starting the active controller container '
                'leaves the resumed run continuing the persisted tick '
                'domain rather than restarting at zero, the '
                'pre-restart point write still applied, the journal '
                'file carrying a run_boundary marker with continuing '
                'seqs across both process lifetimes, and the pair '
                'settled back to active/standby')
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
        ref = save_evidence(ctx['evidence_dir'],
                            'controller-restart-before.json',
                            {'tick': tick0, 'point': point,
                             'receipt': receipt})
        case.evidence('file', ref, 'pre-restart tick and applied write')
        case.observe('point ' + str(point) + ' applied true at tick '
                     + str(tick0))

        # The runner-owned lifecycle action: docker stop + start on the
        # already-running container, recorded on the run's timeline.
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
        if _point_value(grown, point) is not True:
            return case.finish('failed', 'point ' + str(point)
                               + ' lost its written value across the '
                               'restart')
        case.observe('resumed at tick ' + str(tick1) + ' (pre-restart '
                     + str(tick0) + '), point ' + str(point)
                     + ' still applied')

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
        journal = (ctx.get('journal_files') or {}).get(active)
        if journal is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no journal-file path for '
                               + active)
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
# checks): the runner-owned plant stop/start action severs both
# controllers' remote-driver connections mid-run — the non-cyclic
# remote form of decision 78's exchange-loss shape, and unlike a
# per-point fault a dead plant fails every field point at once at the
# link boundary. The active's telemetry must degrade honestly — held
# values re-marked past the read boundary, the driver link reporting
# disconnected with counted per-direction failures and a last_error —
# while scans keep running and the pair's roles hold: the standby's
# checkpoint-pull heartbeat is peer-to-peer, not field traffic, so it
# never promotes on field loss. A restarted plant is a new server
# lifetime — its single-writer claim died with the old process — so
# recovery means the field owner re-attaches and re-claims: reads Good
# again, a third attachment's mutation fenced, and the outage's
# failures still counted in io_health rather than silently reset.

LINK_POLL = 1.0                # cadence watching the pair mid-outage
LINK_DEGRADE_DEADLINE = 45     # bound on the telemetry degrading
LINK_SETTLE = 6.0              # extra role watch once degradation shows
LINK_RECOVERY_DEADLINE = 90    # bound on the plant's return + re-claim


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
    here would preempt the field owner it is checking for."""
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
    """Stop the run's plant container mid-run, prove the link-loss
    degradation through the monitor surface, then restart it and prove
    the field owner's re-claim and recovery."""
    case = Case('plant-link-loss',
                'Plant-link loss degrades honestly and recovers',
                'stopping the run\'s plant container leaves the active '
                'scanning with its field reads marked down at the link '
                'boundary — io_health counting the per-direction '
                'failures, the driver link reporting disconnected, a '
                'last_error recorded — the standby never promoting, '
                'and restarting the plant recovering Good reads under '
                'a re-claimed writer claim with the outage\'s failures '
                'still counted')

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
        census = _try_plant(ctx, {'op': 'list_points'})
        points = (census or {}).get('points', [])
        field_in = sorted(entry.get('point') for entry in points
                          if entry.get('direction') == 'in')
        field_out = any(entry.get('direction') == 'out'
                        for entry in points)
        probe = _try_plant(ctx, {'op': 'step', 'dt': 0})
        before = _try_snapshot(ctx, base)
        ref = save_evidence(ctx['evidence_dir'],
                            'plant-link-loss-baseline.json',
                            {'census': census, 'probe': probe,
                             'qualities': {
                                 point: _sample_quality(before or {},
                                                        point)
                                 for point in field_in}})
        case.evidence('file', ref, 'the plant census, the pre-outage '
                      'fencing probe, and the baseline qualities')
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
        case.observe('field inputs ' + json.dumps(field_in)
                     + ' reading good under the active\'s claim')

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
                  'qualities': {}, 'health': None}
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
        outage_reads = health.get('failed_reads', 0)
        outage_writes = health.get('failed_writes', 0)
        case.observe('degradation confirmed by tick '
                     + str(outage['tick']) + ': '
                     + json.dumps(outage['qualities'], sort_keys=True)
                     + ', io_health ' + json.dumps(health)[:300])

        # The recovery half: the plant container comes back as a new
        # server lifetime, so the single-writer claim is gone until the
        # field owner re-attaches and re-claims it.
        try:
            start()
        except Exception as exc:
            return case.finish('inconclusive', 'the plant start '
                               'action never completed: '
                               + str(exc)[:300])
        case.observe('plant container started; waiting for the '
                     're-claim and Good reads')

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
            if _try_plant(ctx, {'op': 'list_points'}) is not None:
                recovery['plant'] = True
            probe = _try_plant(ctx, {'op': 'step', 'dt': 0})
            if probe is not None:
                recovery['probe'] = probe
            snap = _try_snapshot(ctx, base)
            if snap is not None:
                recovery['snapshot'] = snap
                tick = snap.get('tick') or 0
                if recovery['first_tick'] is None:
                    recovery['first_tick'] = tick
                elif tick > recovery['first_tick']:
                    recovery['tick_grew'] = True
            snap_health = (snap or {}).get('io_health') or {}
            if recovery['plant'] and _fenced(probe) \
                    and snap is not None and recovery['tick_grew'] \
                    and all(_sample_quality(snap, point) == 'good'
                            for point in field_in) \
                    and (snap_health.get('driver') or {}).get('link') \
                    == 'connected':
                break
            time.sleep(LINK_POLL)
        ref = save_evidence(ctx['evidence_dir'],
                            'plant-link-loss-recovery.json', recovery)
        case.evidence('file', ref, 'the plant\'s return, the fencing '
                      'probe, and the recovered snapshot')
        if not recovery['plant']:
            return case.finish('inconclusive', 'the restarted plant '
                               'container never served again')
        if not _fenced(recovery['probe']):
            return case.finish('failed', 'the restarted plant never '
                               're-armed the single-writer claim — a '
                               'third attachment\'s mutation answered '
                               + json.dumps(recovery['probe'])[:300])
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
        case.observe('recovered: reads Good, the writer claim '
                     're-armed, io_health still records '
                     + str(health.get('failed_reads')) + ' failed '
                     'reads and ' + str(health.get('failed_writes'))
                     + ' failed writes')
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


def _tracking_peer(ctx):
    """The ctx endpoint key whose peer reports standby with tracking
    convergence — the run's checkpoint-following controller, or None
    while no pair peer tracks."""
    for name in ('standby', 'active'):
        if ctx.get(name) is None:
            continue
        try:
            report = _role(ctx, ctx[name])
        except Exception:
            continue
        sync = report.get('sync') or {}
        if report.get('role') == 'standby' and 'tracking' in sync:
            return name
    return None


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
        peer = wait_for(lambda: _tracking_peer(ctx),
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
# resumes the held-value rule — the last-stamped (forced) sample
# persists — so the recovery leg restamps the held value through the
# receipted write path: a force still standing would re-substitute on
# the next scan, so the held value read at Good with an empty `forces`
# list proves the release took.

FORCE_DEADLINE = 30  # bound on each boundary/settlement wait


def _point_sample(snapshot, point):
    for entry in (snapshot or {}).get('points', []):
        if entry.get('point') == point:
            return entry.get('sample') or {}
    return {}


def _point_quality(snapshot, point):
    return _point_sample(snapshot, point).get('quality')


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


def scenario_force_release(ctx):
    """A receipted force pins p101-oos at Substituted quality with the
    control image following it; its release plus the restore write
    return the held value at Good — every command journaled as a
    settled, attributed receipt."""
    case = Case('force-release',
                'Receipted forcing and release on a writable point',
                'force_point on the writable p101-oos point serves the '
                'forced value at Uncertain(Substituted), lists the '
                'point under snapshot.forces, and the inverted '
                'p101-oos-ok carrier follows the forced value; '
                'unforce_point clears the badge and the restored held '
                'value reads at Good quality; both commands journal as '
                'settled receipts attributed to qa-lane')
    try:
        # Self-contained on either role layout, like evidence-capture:
        # replayed alone the rig is fresh (ctrl-a active), while the
        # full suite reaches this case after the failover.
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        base = ctx[active]
        case.observe('forcing against ' + active + ' (' + base + ')')

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

        # The held-value rule resumed on release; restamping the held
        # value through the receipted write path produces the Good read
        # the case requires — a force still standing would re-substitute
        # on the next scan, so this read persisting alongside an empty
        # forces list is what proves the release took.
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


def _point_sample(snapshot, point):
    """The served sample of one point in a /snapshot payload, or None."""
    for entry in snapshot.get('points', []):
        if entry.get('point') == point:
            return entry.get('sample')
    return None


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


def _plant_read(stream, point):
    """The stored field sample for `point` — `{"op":"read"}` answered
    as a `sample` result."""
    response = _plant_request(stream, {'op': 'read', 'point': point})
    if response.get('result') != 'sample':
        raise ConnectionError('plant read on point ' + str(point)
                              + ' answered '
                              + json.dumps(response)[:300])
    return response.get('sample') or {}


def _field_inputs(stream):
    """{point: PointInfo entry} for every 'in'-direction point the
    plant serves — the list_points census, which is the field side's
    own account of what the scenario may fault."""
    response = _plant_request(stream, {'op': 'list_points'})
    if response.get('result') != 'points':
        raise ConnectionError('plant list_points answered '
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
    stream = None
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
        stream = _plant_connect(ctx)
        case.observe('plant protocol at ' + str(ctx['plant'])
                     + '; observing ' + active + ' (' + base + ')')

        # Fault targets must be points the field itself holds still:
        # only a stable stored value can prove the clear restored the
        # field value rather than a moved one. Two list_points probes
        # straddling a few plant steps find them, and each must already
        # read Good on the monitor — a forced or degraded point cannot
        # evidence a fault it would mask.
        first = _field_inputs(stream)
        time.sleep(FAULT_PROBE)
        second = _field_inputs(stream)
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
        field_value = _plant_read(stream, quality_point).get('value')
        verdict = _plant_request(
            stream, {'op': 'inject_fault', 'point': quality_point,
                     'fault': {'quality': {'bad': 'device_fault'}}})
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

        verdict = _plant_request(stream, {'op': 'clear_fault',
                                          'point': quality_point})
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
                field = _plant_read(stream, quality_point)
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
        verdict = _plant_request(
            stream, {'op': 'inject_fault', 'point': error_point,
                     'fault': 'disconnected'})
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
        if stream is not None:
            # The injected points are the run's shared field: a case
            # that leaves them faulted poisons every later scenario.
            for point in injected:
                try:
                    _plant_request(stream, {'op': 'clear_fault',
                                            'point': point})
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
# The shipped operator CLI as an external consumer (WW-FND-004's
# replaceable-consumer contract, WW-OPS-001/002's operator-facing
# surface): the lane's one proof that the real dcs-ctl binary — not a
# test harness — reads and commands a deployed pair. The binary comes
# from the run's image build (the bounded builder's
# `cargo build -p dcs-monitor --bin dcs-ctl` beside the image
# binaries), handed to the scenario as ctx['dcs_ctl']; every asserted
# read and mutation travels through CLI invocations against the
# published monitor addresses. The leg is read-mostly by construction:
# its single mutation is the writable-safe declared command the
# served-interface case's selection logic picks, and the refusal
# probes are rejected before they can perturb the plant.

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


def scenario_dcs_ctl(ctx):
    """The shipped dcs-ctl binary against the deployed pair — the
    replaceable-consumer contract exercised through the operator CLI
    rather than raw HTTP."""
    case = Case('dcs-ctl',
                'dcs-ctl consumes the served contract externally',
                'the lane-built dcs-ctl binary reports exactly one '
                'active and one standby across the pair, its schema '
                'read covers every component kind the rig model '
                'declares, the command the served-interface selection '
                'logic picks settles a receipt journaled with the '
                '--actor the leg passed, the emitted-events read '
                'attributes a produced event to its component, and an '
                'undeclared or unavailable invocation is refused by '
                'name — never silently accepted')
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

        # The emitted-events read: the produced event — the command's
        # settled receipt — attributed to its component.
        rc, events, err = ctl(base, 'events', component)
        ref = save_evidence(ctx['evidence_dir'], 'dcs-ctl-events.json',
                            {'component': component, 'exit': rc,
                             'events': events})
        case.evidence('file', ref, 'the emitted-events read for '
                      + str(component))
        if rc != 0 or not isinstance(events, list):
            return done('failed', 'dcs-ctl events failed for '
                        + str(component) + ': exit ' + str(rc) + ' '
                        + str(err)[:200])
        match = None
        for entry in events:
            event = (entry or {}).get('event') or {}
            settled_receipt = (event.get('command_settled') or {}) \
                .get('receipt') or {}
            if settled_receipt.get('command') == command \
                    or event.get('event_emitted'):
                match = entry
                break
        if match is None:
            return done('failed', 'the emitted-events read attributes '
                        'no produced event to ' + str(component))
        case.observe('events read attributes '
                     + next(iter(match.get('event') or {}), '?')
                     + ' to ' + str(component))

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
        try:
            _, resources = http_json('GET', base + '/resources')
        except Exception:
            resources = {}
        for record in (resources or {}).get('components') or []:
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
        return done('passed')
    except Exception as exc:
        return done('inconclusive', str(exc))


# The restart case runs ahead of the failover case: the peer it stops
# is ctrl-a — launched without --standby, so its resumed process comes
# back active — while ctrl-b is the tracking standby the settle check
# watches reconverge. The parameter-tune case also runs ahead of the
# failover leg: only ctrl-b tracks (its --standby source is ctrl-a),
# so a tuned value can cross a checkpoint only from ctrl-a to ctrl-b,
# and the promotion it performs is the run's one a->b switch — the
# failover leg behind it demotes whichever peer reports settled active
# and promotes the converged one back. The checkpoint-negotiation case
# sits between them and the model-revision case: it needs the pair
# still on the mounted fingerprint so the recipe-derived document is
# foreign, and it removes its foreign peer before the revision launch
# takes the third-controller seat. The model-revision case runs
# behind the failover: whichever peer holds the field then is the one
# its third --revised controller stands by on and supersedes, so every
# case after it already exercises the revised model document. The
# incompatible-revision case sits immediately ahead of it: its
# carryover-breaking peer never promotes, so the field writer is
# unchanged, and the compatible case's launch replaces the degraded
# third container and performs the control's promote leg in the same
# run. The plant-link-loss case
# follows later in the schedule: its plant container cycling cannot
# contaminate an earlier case, and whichever endpoint owns the field
# by then keeps it through the outage and recovery the scenario
# drives. The field-fault case is self-contained on either role
# layout — including the post-recovery rig — and leaves the rig as it
# found it. The event-retention case rides beside the
# served-interface case — the same registry surface, the same
# either-layout self-containment, and nothing but receipted drives on
# the field-owning peer. The dcs-ctl case closes the schedule: it
# observes the post-failover role layout and perturbs nothing earlier
# cases established.
SCENARIOS = (scenario_controller_active, scenario_standby_tracking,
             scenario_operator_command, scenario_controller_restart,
             scenario_stale_freshness,
             scenario_parameter_tune_carryover, scenario_failover,
             scenario_checkpoint_negotiation,
             scenario_incompatible_revision, scenario_model_revision,
             scenario_evidence_capture, scenario_served_interface,
             scenario_event_retention,
             scenario_force_release, scenario_consumer_schedule,
             scenario_command_admission, scenario_plant_link_loss,
             scenario_field_fault, scenario_dcs_ctl)


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
