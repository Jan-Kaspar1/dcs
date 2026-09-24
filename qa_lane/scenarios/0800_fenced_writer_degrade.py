"""The fenced_writer_degrade acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *


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
        index = _next_receipt_index(ctx, peer_base)
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
