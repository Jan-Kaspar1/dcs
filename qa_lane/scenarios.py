"""Deterministic acceptance scenarios for the simulated QA rig.

Each scenario drives the redundant controller pair through the monitor
endpoints documented in docs/packaging.md (GET /role, /signals,
/snapshot, /receipts, /journal, /schema, /resources; POST /command,
/demote, /promote) and returns one report-schema scenario case. Stdlib
only — the Lenovo host needs nothing but Python and Docker.

The field-fault case additionally opens one plant-protocol connection
to the run's published plant port — the newline-JSON request/response
surface crates/dcs-sim-net/src/protocol.rs documents — to inject and
clear per-point faults on the shared simulated field.

Evidence is written into the run's evidence/ directory as each response
arrives, so a killed run still leaves inspectable artifacts behind.
"""
import json
import socket
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
    or None while the pair is mid-transition."""
    for name in ('active', 'standby'):
        try:
            if _role(ctx, ctx[name]).get('role') == 'active':
                return name
        except Exception:
            pass
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
        target = None
        for entry in signals.get('points', []):
            if entry.get('name') == 'p101-oos' and entry.get('writable'):
                target = entry
        if target is None:
            for entry in signals.get('points', []):
                if entry.get('writable') \
                        and entry.get('direction') == 'in' \
                        and entry.get('value_type') == 'bool':
                    target = entry
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

    def __init__(self, kind, base, watch):
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

    def poll(self):
        if self.kind == 'polling':
            self._send('GET', self._next_surface())
        elif self.kind == 'disconnect-reconnect':
            self._churn()
        elif self.kind == 'malformed-and-flood':
            self._send('GET', self._next_surface())
            if self._index % 4 == 1:
                self._raw_probes()

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


SCENARIOS = (scenario_controller_active, scenario_standby_tracking,
             scenario_operator_command, scenario_failover,
             scenario_evidence_capture, scenario_served_interface,
             scenario_consumer_schedule, scenario_field_fault)


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
