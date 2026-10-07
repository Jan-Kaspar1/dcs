"""The first_out_burst acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: The first-out-burst case is the same shape as the
# power-fail-trip leg: it drives journaled field contacts and quality
# faults through the plant-side seams under the settled active's
# shared writer claim, restores every driven input, re-arms every
# alarm latch it drove, and perturbs no role — a consequential alarm
# cascade is not peer loss, so it needs no declared window.


# --------------------------------------------------------------------
# First-out ordering across a consequential alarm burst on the
# simulated rig (WW-ALM-003, WW-ALM-004 — the per-revision lane
# evidence for the durable ordered transition record under a
# consequential burst). The fixture's alarm set supports a
# deterministic cascade: a quality fault on `level-primary` so
# `backup-active` annunciates first, then the `power-fail` drive so
# the station permissives drop and the power alarm fires while the
# undrawn level climbs, then both pumps' availability dropped so
# `none-available`/`all-faulted` land last. The leg asserts through
# the active's monitor that every driven alarm asserts
# `alarm`/`unacknowledged`, that the durable journal's
# `point_changed` sequence preserves the driven activation order with
# no dropped or reordered entries, and that the served alarm
# summary's first-out ordering — the /history transition ticks per
# alarm point — agrees with the record; restoring inputs journals the
# returns in order too. Functional misses name burst-order-failed;
# ordering and journal-contract violations name
# burst-order-nondeterministic; a rig whose expected alarm never
# reports finishes inconclusive.

BURST_DEADLINE = 60   # bound on each served transition/settle wait
BURST_POLL = 0.05     # transition-watch cadence — under the scan
BURST_ACTOR = 'qa-lane'


def _burst_points(signals):
    """The cascade's signal-name to point map, or None when the
    deployed model declares no such alarm set."""
    names = {'level-primary': 'level_primary',
             'backup-active': 'backup_active',
             'backup-active-alarm': 'backup_alarm',
             'backup-active-unacknowledged': 'backup_unack',
             'backup-active-ack': 'backup_ack',
             'power-fail': 'power_fail',
             'power-fail-alarm': 'power_alarm',
             'power-fail-unacknowledged': 'power_unack',
             'power-fail-ack': 'power_ack',
             'none-available-alarm': 'none_alarm',
             'none-available-unacknowledged': 'none_unack',
             'none-available-ack': 'none_ack',
             'all-faulted-alarm': 'faulted_alarm',
             'all-faulted-unacknowledged': 'faulted_unack',
             'all-faulted-ack': 'faulted_ack',
             'p101-run': 'run1', 'p102-run': 'run2',
             'p101-fault-alarm': 'fault_alarm1',
             'p101-fault-unacknowledged': 'fault_unack1',
             'p102-fault-alarm': 'fault_alarm2',
             'p102-fault-unacknowledged': 'fault_unack2'}
    entries = {entry.get('name'): entry
               for entry in signals.get('points', [])
               if entry.get('name') in names}
    missing = sorted(set(names) - set(entries))
    if missing:
        return None, missing
    return ({key: entries[name].get('point')
             for name, key in names.items()}, [])


def _activation_order(journal, alarm_points):
    """The journal's first `point_changed`-to-true tick per alarm
    point, in journal order — the durable first-out record."""
    first = {}
    for entry in _journal_list(journal):
        change = (entry.get('event') or {}).get('point_changed') or {}
        if change.get('point') in alarm_points \
                and (change.get('to') or {}).get('bool') is True \
                and change['point'] not in first:
            first[change['point']] = entry.get('tick')
    return [point for point, _ in sorted(first.items(),
                                         key=lambda item: item[1])]


def _ordered_misses(order, groups):
    """The first-out audit: each group's alarm points must appear in
    the record's activation order after the previous group's — none
    dropped and none reordered."""
    misses, cursor = [], 0
    for index, group in enumerate(groups):
        positions = []
        for want in group:
            try:
                position = order.index(want, cursor)
            except ValueError:
                position = None
            if position is None:
                misses.append(
                    'the durable journal carries no activation for '
                    'alarm point ' + str(want) + ' at or after group '
                    + str(index) + ' — the driven transition is '
                    'missing or out of order')
            else:
                positions.append(position)
        if positions:
            cursor = max(positions) + 1
    return misses


def scenario_first_out_burst(ctx):
    """Drive the consequential alarm cascade and prove the durable
    journal keeps activation order: backup-active first, the power
    drive second, the availability tail last — every driven alarm
    asserting alarm/unacknowledged, the served history agreeing, the
    returns journaling in order."""
    case = Case('first-out-burst',
                'A consequential burst journals in activation order',
                'with the deployed pair settled, a quality fault on '
                'level-primary annunciates backup-active first, the '
                'power-fail drive fires the power alarm while the '
                'undrawn level climbs, and dropped pump availability '
                'lands none-available/all-faulted last — every driven '
                'alarm asserting alarm/unacknowledged, the durable '
                'journal preserving the driven activation order with '
                'no dropped or reordered entries, the served history '
                'agreeing, and the restored inputs journaling the '
                'returns in order')
    stream = None
    base = None
    active = None
    points = None
    restore_fail = None
    injected = []
    try:
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        base = ctx[active]
        peer = 'standby' if active == 'active' else 'active'
        peer_base = ctx.get(peer)
        case.observe('settled pair: ' + active + ' active')

        _, signals = http_json('GET', base + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'burst-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming the cascade')
        points, missing = _burst_points(signals)
        if points is None:
            return case.finish('inconclusive', 'the deployed model '
                               'lacks the burst cascade wiring — no '
                               'signals ' + ', '.join(missing))
        for name in ('backup_ack', 'power_ack', 'none_ack',
                     'faulted_ack'):
            entry = next(entry for entry in signals.get('points', [])
                         if entry.get('point') == points[name])
            if not entry.get('writable') \
                    or entry.get('direction') != 'in':
                return case.finish('inconclusive', 'the ack point '
                                   + str(points[name]) + ' is not the '
                                   'writable ack input the leg needs')
        case.observe('cascade points: ' + json.dumps(points,
                                                     sort_keys=True))
        if ctx.get('plant') is None:
            return case.finish('inconclusive',
                               'the run publishes no plant endpoint')
        owner = (ctx.get('plant_owner') or {}).get(active)
        if owner is None:
            return case.finish('inconclusive', 'the run pins no '
                               'plant-writer owner token for the '
                               'settled active ' + str(active))
        stream = _plant_connect(ctx)
        verdict = _plant_request(stream, {'op': 'ensure_writer',
                                          'owner': owner})
        if verdict.get('result') not in ('done', 'claimed_shared'):
            return case.finish('inconclusive', 'the writer claim '
                               'refused the shared attachment: '
                               + json.dumps(verdict)[:300])

        last = {}

        def value(key, snap):
            sample = _point_sample(snap, points[key])
            if sample is None:
                return None
            raw = sample.get('value')
            if isinstance(raw, dict):
                return next(iter(raw.values()), None)
            return raw

        def poll(cond):
            snap = _try_snapshot(ctx, base)
            if snap is None:
                return None
            last['snap'] = snap
            return snap if cond(snap) else None

        def leg(name, cond, keys):
            hit = wait_for(lambda: poll(cond),
                           time.monotonic() + BURST_DEADLINE,
                           interval=BURST_POLL)
            snap = last.get('snap') or {}
            ref = save_evidence(
                ctx['evidence_dir'], 'burst-' + name + '.json',
                {'tick': snap.get('tick'),
                 'samples': {key: _point_sample(snap, points[key])
                             for key in keys}})
            case.evidence('file', ref, 'the served ' + name + ' leg')
            if hit:
                return hit, None
            return None, case.finish(
                'inconclusive' if any(
                    _point_sample(snap, points[key]) is None
                    for key in keys) else 'failed',
                'burst-order-failed: the ' + name + ' leg never '
                'landed; last served '
                + json.dumps({key: value(key, snap) for key in keys},
                             sort_keys=True)[:500])

        _, journal0 = http_json('GET', base + '/journal?since=0')
        floor = max((entry.get('seq') or 0
                     for entry in _journal_list(journal0)
                     if isinstance(entry, dict)), default=0)

        # Phase 1 — the initiating cause: a non-Good quality on
        # level-primary fails the measurement over to the backup.
        verdict = _plant_ctl(ctx, 'fault', str(points['level_primary']),
                             'bad:device_fault')
        if verdict.get('result') != 'done':
            return case.finish('failed', 'burst-order-failed: '
                               'inject_fault on level-primary refused: '
                               + json.dumps(verdict)[:300])
        injected.append(points['level_primary'])
        hit, error = leg(
            'backup',
            lambda s: value('backup_active', s) is True
            and value('backup_alarm', s) is True
            and value('backup_unack', s) is True,
            ('backup_active', 'backup_alarm', 'backup_unack'))
        if error:
            return error
        case.observe('backup-active annunciated first at tick '
                     + str(hit.get('tick')))

        # Phase 2 — the consequential drive: power-fail drops the
        # permissives while the undrawn level climbs.
        verdict = _plant_request(
            stream, {'op': 'write', 'point': points['power_fail'],
                     'value': {'bool': True}})
        if verdict.get('result') != 'done':
            return case.finish('failed', 'burst-order-failed: the '
                               'power-fail write was refused: '
                               + json.dumps(verdict)[:300])
        restore_fail = points['power_fail']
        hit, error = leg(
            'power',
            lambda s: value('power_alarm', s) is True
            and value('power_unack', s) is True
            and value('none_alarm', s) is True
            and value('none_unack', s) is True,
            ('power_alarm', 'power_unack', 'none_alarm',
             'none_unack'))
        if error:
            return error
        case.observe('the power drive landed at tick '
                     + str(hit.get('tick')))

        # Phase 3 — the consequential tail: both run contacts faulted
        # so the all-faulted roll-up lands last.
        for key in ('run1', 'run2'):
            verdict = _plant_ctl(ctx, 'fault', str(points[key]),
                                 'bad:device_fault')
            if verdict.get('result') != 'done':
                return case.finish('failed', 'burst-order-failed: '
                                   'inject_fault on ' + key
                                   + ' refused: '
                                   + json.dumps(verdict)[:300])
            injected.append(points[key])
        hit, error = leg(
            'faulted',
            lambda s: value('faulted_alarm', s) is True
            and value('faulted_unack', s) is True
            and value('fault_alarm1', s) is True
            and value('fault_alarm2', s) is True,
            ('faulted_alarm', 'faulted_unack', 'fault_alarm1',
             'fault_alarm2'))
        if error:
            return error
        case.observe('the all-faulted tail landed at tick '
                     + str(hit.get('tick')))

        # Phase 4 — the peak: every driven alarm stands asserted with
        # its latch.
        snap = last.get('snap') or {}
        for key in ('backup_alarm', 'backup_unack', 'power_alarm',
                    'power_unack', 'none_alarm', 'none_unack',
                    'faulted_alarm', 'faulted_unack'):
            if value(key, snap) is not True:
                return case.finish(
                    'failed', 'burst-order-failed: the ' + key
                    + ' flag does not stand at the burst peak: '
                    + json.dumps(value(key, snap)))
        case.observe('every driven alarm stands at the peak')

        # Phase 5 — the record: the durable journal preserves the
        # driven activation order with no dropped or reordered entry.
        try:
            _, journal = http_json('GET', base + '/journal?since='
                                   + str(floor))
        except Exception as exc:
            return case.finish('failed', 'burst-order-failed: the '
                               'served journal refused: ' + str(exc))
        order = _activation_order(
            journal, [points['backup_alarm'], points['power_alarm'],
                      points['none_alarm'], points['faulted_alarm']])
        ref = save_evidence(ctx['evidence_dir'], 'burst-order.json',
                            {'floor': floor, 'order': order})
        case.evidence('file', ref, 'the durable activation order')
        misses = _ordered_misses(
            order, [[points['backup_alarm']],
                    [points['power_alarm'], points['none_alarm']],
                    [points['faulted_alarm']]])
        if misses:
            return case.finish('failed', 'burst-order-failed: '
                               + '; '.join(misses))
        case.observe('the durable journal preserves the driven '
                     'activation order')

        # Phase 6 — the served surface agrees: the /history first-true
        # tick per alarm point orders the same way.
        query = ''.join('&point=' + str(points[key]) for key in
                        ('backup_alarm', 'power_alarm', 'none_alarm',
                         'faulted_alarm'))
        try:
            _, history = http_json('GET', base + '/history?since=0'
                                   + query)
        except Exception as exc:
            return case.finish('failed', 'burst-order-failed: the '
                               'served history refused: ' + str(exc))
        ticks = {}
        for key in ('backup_alarm', 'power_alarm', 'none_alarm',
                    'faulted_alarm'):
            seq = _history_transitions(history, points[key], 0)
            first = next((tick for tick, val in seq if val is True),
                         None)
            if first is None:
                return case.finish(
                    'failed', 'burst-order-failed: the served '
                    'history never showed ' + key)
            ticks[key] = first
        surface = sorted(ticks, key=lambda key: ticks[key])
        ref = save_evidence(ctx['evidence_dir'],
                            'burst-history.json', ticks)
        case.evidence('file', ref, 'the served first-out surface')
        if [points[key] for key in surface] != order:
            return case.finish(
                'failed', 'burst-order-nondeterministic: the served '
                'history order ' + json.dumps(surface) + ' disagrees '
                'with the durable record ' + json.dumps(order))
        case.observe('the served history agrees with the record')

        # Phase 7 — the returns, in driven order, journaled too.
        _plant_ctl(ctx, 'clear-fault', str(points['level_primary']))
        hit, error = leg(
            'backup-return',
            lambda s: value('backup_alarm', s) is False,
            ('backup_alarm',))
        if error:
            return error
        verdict = _plant_request(
            stream, {'op': 'write', 'point': points['power_fail'],
                     'value': {'bool': False}})
        restore_fail = None
        hit, error = leg(
            'power-return',
            lambda s: value('power_alarm', s) is False
            and value('none_alarm', s) is False,
            ('power_alarm', 'none_alarm'))
        if error:
            return error
        for key in ('run1', 'run2'):
            _plant_ctl(ctx, 'clear-fault', str(points[key]))
        hit, error = leg(
            'faulted-return',
            lambda s: value('faulted_alarm', s) is False,
            ('faulted_alarm',))
        if error:
            return error
        try:
            _, journal = http_json('GET', base + '/journal?since='
                                   + str(floor))
        except Exception as exc:
            return case.finish('failed', 'burst-order-failed: the '
                               'served journal refused: ' + str(exc))
        changes = _journal_point_changes(journal)
        for key in ('backup_alarm', 'power_alarm', 'none_alarm',
                    'faulted_alarm'):
            seq = changes.get(points[key], [])
            if {'bool': True} not in seq or {'bool': False} not in seq \
                    or seq.index({'bool': False}) < seq.index(
                        {'bool': True}):
                return case.finish(
                    'failed', 'burst-order-failed: the return for '
                    + key + ' never journaled in order')
        case.observe('the returns journaled in order')

        # Phase 8 — re-arm: acknowledge every driven latch through
        # the receipted path and release each ack input, so the leg
        # leaves the alarm set as found for later legs.
        for ack, unack in (('backup_ack', 'backup_unack'),
                           ('power_ack', 'power_unack'),
                           ('none_ack', 'none_unack'),
                           ('faulted_ack', 'faulted_unack')):
            write = {'point': points[ack], 'kind': 'bool',
                     'value': {'bool': True}}
            try:
                status, receipt = http_json(
                    'POST', base + '/command',
                    {'command': {'write_value': write},
                     'actor': BURST_ACTOR})
            except Exception as exc:
                return case.finish('failed', 'burst-order-failed: '
                                   'the ' + ack + ' write errored: '
                                   + str(exc))
            if status != 200 or 'rejected' in (
                    (receipt or {}).get('outcome') or {}):
                return case.finish('failed', 'burst-order-failed: '
                                   'the ' + ack + ' write was '
                                   'refused: ' + str(status) + ' '
                                   + json.dumps(receipt)[:300])
            hit, error = leg(
                ack + '-cleared',
                lambda s, u=unack: value(u, s) is False,
                (unack,))
            if error:
                return error
            restore = {'point': points[ack], 'kind': 'bool',
                       'value': {'bool': False}}
            try:
                http_json('POST', base + '/command',
                          {'command': {'write_value': restore},
                           'actor': BURST_ACTOR})
            except Exception:
                pass
        case.observe('every driven latch acknowledged and re-armed')
        if _settled_active(ctx) != active:
            return case.finish('failed', 'burst-order-failed: the '
                               'active role moved under the burst')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
    finally:
        try:
            for point in injected:
                _try_plant_ctl(ctx, 'clear-fault', str(point))
            if restore_fail is not None and stream is not None:
                _plant_request(stream, {'op': 'write',
                                        'point': restore_fail,
                                        'value': {'bool': False}})
            if base is not None and points is not None:
                for ack in ('backup_ack', 'power_ack', 'none_ack',
                            'faulted_ack'):
                    try:
                        http_json(
                            'POST', base + '/command',
                            {'command': {'write_value': {
                                'point': points[ack], 'kind': 'bool',
                                'value': {'bool': False}}},
                             'actor': BURST_ACTOR})
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
