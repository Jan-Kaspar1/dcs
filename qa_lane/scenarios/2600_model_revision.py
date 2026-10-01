"""The model_revision acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: The model-revision case runs behind the failover: whichever peer
# holds the field then is the one its third --revised controller
# stands by on and supersedes, so every case after it already
# exercises the revised model document.
RUNS_AFTER = frozenset({'scenario_checkpoint_negotiation', 'scenario_doomed_startup_claim', 'scenario_failover', 'scenario_incompatible_revision'})

REVISION_SETTLE_DEADLINE = 60     # bound on demote/promote role settles


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
