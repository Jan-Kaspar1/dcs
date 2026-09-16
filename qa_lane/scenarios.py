"""Deterministic acceptance scenarios for the simulated QA rig.

Each scenario drives the redundant controller pair through the monitor
endpoints documented in docs/packaging.md (GET /role, /signals,
/snapshot, /receipts, /journal, /schema, /resources; POST /command,
/demote, /promote) and returns one report-schema scenario case. Stdlib
only — the Lenovo host needs nothing but Python and Docker. The
restart scenario also triggers the runner-owned container lifecycle
action ctx['restart_controller'] carries and reads the per-controller
--journal-file the rig bind-mounts into the run directory. The
link-loss scenario drives the runner-owned plant stop/start actions
ctx['stop_plant']/ctx['start_plant'] carry and probes the run's plant
server directly on ctx['plant'] — the field's own fencing evidence.

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


def _plant_request(ctx, request, timeout=5):
    """One request/response line against the run's plant server — the
    `dcs-sim-net` wire protocol on the runner-published plant port
    ctx['plant'] carries. The link-loss scenario uses it for the
    field's own evidence: `list_points` is the census of points the
    boundary fails at once, and a `step` mutation is the fencing probe
    — answered `fenced` while any attachment holds the plant's
    single-writer claim, `stepped` while nobody does. The probe never
    sends `claim_writer`: claiming from here would preempt the field
    owner it is checking for."""
    stream = _connect(ctx['plant'], timeout=timeout)
    try:
        stream.settimeout(timeout)
        stream.sendall(json.dumps(request).encode() + b'\n')
        line = b''
        while b'\n' not in line and len(line) <= 65536:
            chunk = stream.recv(65536)
            if not chunk:
                break
            line += chunk
        return json.loads(line) if line.strip() else None
    finally:
        stream.close()


def _try_plant(ctx, request):
    """`_plant_request` or None — a refused probe is one lost poll, not
    the leg's verdict."""
    try:
        return _plant_request(ctx, request)
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


# The restart case runs ahead of the failover case: the peer it stops
# is ctrl-a — launched without --standby, so its resumed process comes
# back active — while ctrl-b is the tracking standby the settle check
# watches reconverge. The plant-link-loss case runs last: whichever
# endpoint owns the field by then keeps it through the outage and
# recovery the scenario drives, and its container cycling cannot
# contaminate an earlier case.
SCENARIOS = (scenario_controller_active, scenario_standby_tracking,
             scenario_operator_command, scenario_controller_restart,
             scenario_failover, scenario_evidence_capture,
             scenario_served_interface, scenario_consumer_schedule,
             scenario_command_admission, scenario_plant_link_loss)


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
