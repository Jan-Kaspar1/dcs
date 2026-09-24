"""The controller_restart acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

RESTART_POLL = 1.0              # cadence watching the pair mid-restart
RESTART_RETURN_DEADLINE = 60  # bound on the restarted monitor's return
RESTART_SETTLE_DEADLINE = 60  # bound on active/standby roles settling
# Bound on the restart-window command's verdict: the point serving the
# second write's value and the durable journal carrying its
# CommandSettled — on whichever side of the run boundary it landed.
RESTART_COMMAND_DEADLINE = 30
# Scans the persisted checkpoint may lag the last served snapshot: the
# state file is written at the end of each completed scan cycle, so a
# /snapshot answer can interleave before that cycle's write lands.
RESTART_SLACK_TICKS = 4


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


def _receipt_key(receipt):
    """A receipt's canonical identity — the dedup legs compare the
    journaled record, not the receipt's position."""
    return json.dumps(receipt, sort_keys=True)


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
