"""The backup_health acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: The backup-health case sits in the same restored window: only with
# the pair settled and tracking does a backup-only field fault have a
# standby whose takeover the annunciation must precede — the leg
# injects, annunciates, acks, clears, and restores without moving the
# selection or the roles.
RUNS_BEFORE = frozenset({'scenario_parameter_tune_carryover'})


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
