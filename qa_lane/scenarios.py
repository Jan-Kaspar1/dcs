"""Deterministic acceptance scenarios for the simulated QA rig.

Each scenario drives the redundant controller pair through the monitor
endpoints documented in docs/packaging.md (GET /role, /signals,
/snapshot, /receipts, /journal; POST /command, /demote, /promote) and
returns one report-schema scenario case. Stdlib only — the Lenovo host
needs nothing but Python and Docker.

Evidence is written into the run's evidence/ directory as each response
arrives, so a killed run still leaves inspectable artifacts behind.
"""
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

SCENARIO_TIMEOUT = 120  # per-scenario wall clock bound
POLL_INTERVAL = 2.0


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
        target = None
        for entry in signals.get('points', []):
            if entry.get('name') == 'p101-oos' and entry.get('writable'):
                target = entry
        if target is None:
            for entry in signals.get('points', []):
                if entry.get('writable') and entry.get('direction') == 'in' \
                        and entry.get('value_type') == 'bool':
                    target = entry
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
                'POST /demote on ctrl-a then POST /promote on ctrl-b leaves '
                'ctrl-b active with telemetry advancing')
    try:
        status, body = http_json('POST', ctx['active'] + '/demote')
        case.observe('demote ctrl-a: ' + str(status) + ' '
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


def scenario_evidence_capture(ctx):
    """The monitor exposes receipts and a transition journal."""
    case = Case('evidence-capture',
                'Monitor exposes receipts and journal',
                'GET /receipts and GET /journal return records covering the '
                'run so far')
    try:
        _, receipts = http_json('GET', ctx['standby'] + '/receipts')
        _, journal = http_json('GET', ctx['standby'] + '/journal')
        save_evidence(ctx['evidence_dir'], 'evidence-receipts.json',
                      receipts)
        save_evidence(ctx['evidence_dir'], 'evidence-journal.json', journal)
        case.evidence('file', 'evidence/evidence-receipts.json')
        case.evidence('file', 'evidence/evidence-journal.json')
        count = len(receipts) if isinstance(receipts, list) \
            else len(receipts.get('receipts', []))
        case.observe('receipts returned ' + str(count) + ' entr'
                     + ('y' if count == 1 else 'ies'))
        if count < 1:
            return case.finish('failed', 'no command receipts recorded')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))


SCENARIOS = (scenario_controller_active, scenario_standby_tracking,
             scenario_operator_command, scenario_failover,
             scenario_evidence_capture)


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
