"""The power_fail_trip acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *


# --------------------------------------------------------------------
# The station power-fail interlock trip and its declared recovery
# (WW-OPS-001's interlock-trips clause, WW-CTL-002's recovery clause —
# the demand-side leg the burst-order case's alarm-ordering drive left
# open). `power-fail` is the journaled field contact wired into every
# pump's availability aggregation — the inverted `power-ok` feeds each
# pump's `power-ok-in` leg of `avail_i` — and straight into the managed
# `power-fail-*` alarm's `in`. With the pair settled and the group
# holding a duty demand — `duty` naming a pump, `demand` above zero, a
# motor command standing — a plant-protocol write on the contact under
# the active's shared writer claim must drop `power-ok`, strip both
# pumps' availability, and release both motor commands while the
# chain's demand still stands (the station keeps calling — no pump can
# serve): `none-available` annunciates the all-out state, the managed
# alarm stands unacknowledged, and every transition journals on its
# declared-journaled point beside the `power-fail` transition itself.
# The receipted `power-fail-ack` clears the latch while the condition
# stands; the restore write then returns `power-ok` and the
# availability legs, and the group re-stages the standing demand inside
# the declared `start_delay_ticks`/`min_off_ticks` bounds with no
# output step outside the deterministic scan sequence, and the pair's
# roles never move. Functional misses name power-trip-failed;
# ordering, bounds, and journal-contract violations name
# power-trip-nondeterministic.

POWER_TRIP_DEADLINE = 60  # bound on each leg's served transition — the
                          # natural demand window spans a dozen scans
                          # under the deployed dynamics' rates
POWER_TRIP_POLL = 0.05    # transition-watch cadence — under the scan
POWER_TRIP_ACTOR = 'qa-lane'


def scenario_power_fail_trip(ctx):
    """Trip the station power-fail interlock and prove the declared
    recovery: the driven contact strips availability, releases both
    motor commands while the demand stands, annunciates none-available
    and the managed alarm's two flags, and the restore re-stages the
    group inside the declared bounds — roles unmoved throughout."""
    case = Case('power-fail-trip',
                'Power-fail trips the group; the restore re-stages it',
                'with the deployed pair settled and the pump group '
                'holding a duty demand — duty naming a pump, demand '
                'above zero, a motor command standing — a plant-'
                'protocol write driving the journaled power-fail '
                'contact true drops power-ok and both pumps\' '
                'availability, releases p101-cmd/p102-cmd while the '
                'chain\'s demand still stands, and asserts '
                'none-available with the power-fail alarm standing '
                'unacknowledged — every transition journaled on its '
                'declared-journaled point beside the power-fail '
                'transition itself; the receipted power-fail-ack '
                'clears the latch while the condition stands; the '
                'restore write returns power-ok and the availability '
                'legs and the group re-stages the standing demand '
                'inside the declared start_delay_ticks/min_off_ticks '
                'bounds with no output step outside the deterministic '
                'scan sequence; the pair\'s roles never move')
    stream = None
    restore_fail = None  # the field point while the drive stands
    held_acks = []       # ack input points left standing true
    submitted = []       # commands this case receipted
    cleanup = {}         # {ack_point: unack_point} once resolved
    live = {'base': None}
    try:
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        base = ctx[active]
        live['base'] = base
        peer = 'standby' if active == 'active' else 'active'
        peer_base = ctx.get(peer)
        peer_role0 = _try_role(ctx, peer_base) if peer_base else None
        case.observe('settled pair: ' + active + ' active'
                     + (', ' + peer + ' reporting '
                        + json.dumps((peer_role0 or {}).get('role'))
                        if peer_base else ', no second endpoint'))

        _, signals = http_json('GET', base + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'power-trip-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming the power-fail '
                      'interlock path')
        names = {'power-fail': 'power_fail', 'power-ok': 'power_ok',
                 'level-selected': 'level',
                 'demand': 'demand', 'demand-in': 'demand_in',
                 'duty': 'duty', 'staged': 'staged',
                 'none-available': 'none_available',
                 'none-available-in': 'none_in',
                 'none-available-ack': 'none_ack',
                 'none-available-alarm': 'none_alarm',
                 'none-available-unacknowledged': 'none_unack',
                 'power-fail-ack': 'ack',
                 'power-fail-alarm': 'alarm',
                 'power-fail-unacknowledged': 'unack',
                 'power-fail-shelved': 'shelved',
                 'power-fail-suppressed': 'suppressed',
                 'power-fail-out-of-service': 'alarm_oos',
                 'p101-cmd': 'cmd1', 'p102-cmd': 'cmd2',
                 'p101-run': 'run1', 'p102-run': 'run2',
                 'p101-avail': 'avail1', 'p102-avail': 'avail2',
                 'p101-avail-in': 'avail_in1',
                 'p102-avail-in': 'avail_in2',
                 'p101-power-ok-in': 'pok_in1',
                 'p102-power-ok-in': 'pok_in2',
                 'p101-mode': 'p1_mode', 'p101-oos': 'p1_oos',
                 'p102-mode': 'p2_mode', 'p102-oos': 'p2_oos'}
        optional = {'lah-ack': 'lah_ack',
                    'lah-unacknowledged': 'lah_unack'}
        entries = {entry.get('name'): entry
                   for entry in signals.get('points', [])
                   if entry.get('name') in set(names) | set(optional)}
        missing = sorted(set(names) - set(entries))
        if missing:
            return case.finish('inconclusive', 'the deployed model '
                               'lacks the power-fail interlock '
                               'wiring — no signals '
                               + ', '.join(missing))
        for key in ('power-fail-ack', 'none-available-ack'):
            entry = entries[key]
            if not entry.get('writable') \
                    or entry.get('direction') != 'in' \
                    or entry.get('value_type') != 'bool':
                return case.finish('inconclusive', 'the ' + key
                                   + ' point is not the alarm\'s '
                                   'writable bool ack input: '
                                   + json.dumps(entry)[:300])
        resolved = dict(names)
        resolved.update(optional)
        points = {resolved[name]: entry.get('point')
                  for name, entry in entries.items()}
        case.observe('interlock path: '
                     + json.dumps({name: entry.get('point')
                                   for name, entry in
                                   sorted(entries.items())},
                                  sort_keys=True))
        cleanup = {points['ack']: points['unack'],
                   points['none_ack']: points['none_unack']}
        if all(name in entries for name in optional):
            cleanup[entries['lah-ack'].get('point')] = \
                entries['lah-unacknowledged'].get('point')

        _, schema = http_json('GET', base + '/schema')
        snap0 = _snapshot(ctx, base)
        chain_name = _component_instance(schema, 'threshold-chain')
        group_name = _component_instance(schema, 'pump-group')
        alarm_name = _component_instance(
            schema, 'managed-bool-latching-alarm', points['alarm'])
        if chain_name is None or group_name is None \
                or alarm_name is None:
            return case.finish('inconclusive', 'the served schema '
                               'lacks the threshold-chain, pump-group, '
                               'or the managed-bool-latching-alarm '
                               'instance wired to the power-fail-alarm '
                               'point')
        start_delay = _parameter_value(snap0, group_name,
                                       'start_delay_ticks')
        min_off = _parameter_value(snap0, group_name, 'min_off_ticks')
        start_set = _parameter_value(snap0, chain_name, 'start')
        stop_set = _parameter_value(snap0, chain_name, 'stop')
        ref = save_evidence(
            ctx['evidence_dir'], 'power-trip-wiring.json',
            {'points': {key: points[key] for key in sorted(points)},
             'components': {'chain': chain_name, 'group': group_name,
                            'alarm': alarm_name},
             'start_delay_ticks': start_delay,
             'min_off_ticks': min_off,
             'start': start_set, 'stop': stop_set})
        case.evidence('file', ref, 'the served interlock wiring and '
                      'the declared staging bounds')
        for name_, param in (('start_delay_ticks', start_delay),
                             ('min_off_ticks', min_off)):
            if not isinstance(param, int) or isinstance(param, bool) \
                    or param < 0:
                return case.finish('inconclusive', 'the pump group '
                                   'serves no usable ' + name_ + ': '
                                   + json.dumps(param))
        for name_, param in (('start', start_set), ('stop', stop_set)):
            if not isinstance(param, (int, float)) \
                    or isinstance(param, bool) \
                    or not math.isfinite(param):
                return case.finish('inconclusive', 'the threshold '
                                   'chain serves no usable ' + name_
                                   + ' setpoint: ' + json.dumps(param))
        if stop_set >= start_set:
            return case.finish('inconclusive', 'the served setpoint '
                               'chain is not ordered: stop '
                               + json.dumps(stop_set) + ' >= start '
                               + json.dumps(start_set))

        if ctx.get('plant') is None:
            return case.finish('inconclusive',
                               'the run publishes no plant endpoint')
        owner = (ctx.get('plant_owner') or {}).get(active)
        if owner is None:
            return case.finish('inconclusive', 'the run pins no '
                               'plant-writer owner token for the '
                               'settled active ' + str(active))
        # The same claim seam as the lag-staging drive: the field
        # census and the point reads ride the shipped dcs-plant-ctl,
        # while the standing shared claim and the writes under it stay
        # on the raw attachment — the tool exposes no ensure_writer
        # under a chosen owner token.
        stream = _plant_connect(ctx)
        field = _field_inputs(ctx)
        if points['power_fail'] not in field:
            return case.finish('inconclusive', 'the power-fail '
                               'signal\'s point '
                               + str(points['power_fail'])
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
        baseline_fail = (_plant_read(ctx, points['power_fail'])
                         .get('value') or {}).get('bool')
        if baseline_fail is not False:
            return case.finish('inconclusive', 'the power-fail '
                               'contact does not read false ahead of '
                               'the drive: '
                               + json.dumps(baseline_fail))

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
            reported a sample, power-trip-failed when the served
            values never landed the leg."""
            hit = wait_for(lambda: poll(cond, keys),
                           time.monotonic() + POWER_TRIP_DEADLINE,
                           interval=POWER_TRIP_POLL)
            snap = last.get('snap') or {}
            ref = save_evidence(
                ctx['evidence_dir'], 'power-trip-' + name + '.json',
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
                'failed', 'power-trip-failed: the ' + name + ' leg '
                'never landed; last served ' + json.dumps(
                    {key: value(key, snap) for key in sorted(keys)},
                    sort_keys=True)[:500])

        # The drive precondition: the group holding a duty demand —
        # duty naming a pump, demand above zero, that pump's command
        # standing — caught early enough in the drain that the write's
        # transit cannot outrun the cycle (the level must still clear
        # the stop/start midpoint when the poll lands).
        holding_keys = ('power_fail', 'power_ok', 'level', 'demand',
                        'duty', 'staged', 'cmd1', 'cmd2', 'avail1',
                        'avail2', 'none_available', 'alarm', 'unack',
                        'shelved', 'suppressed', 'alarm_oos')

        def holding(snap):
            demand = value('demand', snap)
            if not isinstance(demand, int) or isinstance(demand, bool) \
                    or demand < 1:
                return None
            duty = value('duty', snap)
            if duty not in (1, 2):
                return None
            staged = value('staged', snap)
            if not isinstance(staged, int) or isinstance(staged, bool) \
                    or staged < 1:
                return None
            if value('cmd1' if duty == 1 else 'cmd2', snap) is not True:
                return None
            level = value('level', snap)
            if not isinstance(level, (int, float)) \
                    or isinstance(level, bool) \
                    or level < (start_set + stop_set) / 2.0:
                return None
            if value('power_fail', snap) is not False:
                return None
            for key in ('power_ok', 'avail1', 'avail2'):
                if value(key, snap) is not True:
                    return None
            for key in ('none_available', 'alarm', 'unack', 'shelved',
                        'suppressed', 'alarm_oos'):
                if value(key, snap) is not False:
                    return None
            return snap

        baseline = wait_for(lambda: poll(holding, holding_keys),
                            time.monotonic() + POWER_TRIP_DEADLINE,
                            interval=POWER_TRIP_POLL)
        snap = last.get('snap') or {}
        ref = save_evidence(
            ctx['evidence_dir'], 'power-trip-baseline.json',
            {'tick': snap.get('tick'), 'power_fail': baseline_fail,
             'level': value('level', snap),
             'demand': value('demand', snap),
             'duty': value('duty', snap),
             'staged': value('staged', snap)})
        case.evidence('file', ref, 'the settled duty-demand baseline '
                      'ahead of the drive')
        if baseline is None:
            unreported = sorted(key for key in holding_keys
                                if key not in seen)
            return case.finish(
                'inconclusive', 'the settled duty-demand baseline '
                'never landed'
                + (': awaited points never reported on the served '
                   'snapshot: ' + ', '.join(unreported)
                   if unreported else ' — the deployed dynamics never '
                   'presented the window'))
        duty0 = value('duty', baseline)
        pump0 = {key: value(key, baseline)
                 for key in ('p1_mode', 'p1_oos', 'p2_mode', 'p2_oos')}
        case.observe('the group holding a duty demand at tick '
                     + str(baseline.get('tick')) + ': duty='
                     + str(duty0) + ' demand='
                     + str(value('demand', baseline)) + ' staged='
                     + str(value('staged', baseline)))

        # The journal floor ahead of the drive: this leg's records are
        # the ones above it. The snapshot tick anchors the /history
        # transition scan.
        _, journal0 = http_json('GET', base + '/journal?since=0')
        floor = max((entry.get('seq') or 0
                     for entry in _journal_list(journal0)
                     if isinstance(entry, dict)), default=0)
        drive_tick = baseline.get('tick') or 0

        # The trip: the journaled field contact driven true under the
        # shared claim. The interlock's consequences ride the carriers
        # — power-ok, the per-pump availability legs, then the group.
        verdict = _plant_request(
            stream, {'op': 'write', 'point': points['power_fail'],
                     'value': {'bool': True}})
        if verdict.get('result') != 'done':
            return case.finish('failed', 'power-trip-failed: the '
                               'power-fail write on point '
                               + str(points['power_fail'])
                               + ' was refused under the shared '
                               'claim: ' + json.dumps(verdict)[:300])
        restore_fail = points['power_fail']
        case.observe('power-fail driven true on field point '
                     + str(points['power_fail']))

        trip_keys = ('power_fail', 'power_ok', 'avail1', 'avail2',
                     'avail_in1', 'avail_in2', 'cmd1', 'cmd2', 'duty',
                     'staged', 'demand', 'none_available', 'alarm',
                     'unack', 'shelved', 'suppressed', 'alarm_oos')

        def tripped(snap):
            if value('power_fail', snap) is not True \
                    or value('power_ok', snap) is not False:
                return None
            if value('avail1', snap) is not False \
                    or value('avail2', snap) is not False:
                return None
            if value('cmd1', snap) is not False \
                    or value('cmd2', snap) is not False:
                return None
            if value('staged', snap) != 0 or value('duty', snap) != 0:
                return None
            if value('none_available', snap) is not True:
                return None
            if value('alarm', snap) is not True \
                    or value('unack', snap) is not True:
                return None
            for key in ('shelved', 'suppressed', 'alarm_oos'):
                if value(key, snap) is not False:
                    return None
            demand = value('demand', snap)
            if not isinstance(demand, int) or isinstance(demand, bool) \
                    or demand < 1:
                return None
            return snap

        hit, error = leg('tripped', tripped, trip_keys)
        if error:
            return error
        case.observe('the interlock tripped at tick '
                     + str(hit.get('tick')) + ': power-ok dropped, '
                     'both availability legs down, both commands '
                     'released with demand '
                     + str(value('demand', hit)) + ' still standing — '
                     'none-available and the alarm standing '
                     'unacknowledged')
        if _settled_active(ctx) != active:
            return case.finish('failed', 'power-trip-failed: the '
                               'active role moved under the power '
                               'drive — a field trip is not peer loss')

        # The durable half of the trip: each assertion lands its
        # point_changed on a declared-journaled point beside the
        # power-fail transition itself — while the pair journaled no
        # role change and no non-journaled point records a transition.
        asserted_want = {'power_fail': True, 'avail1': False,
                         'avail2': False, 'none_available': True,
                         'none_alarm': True, 'none_unack': True,
                         'alarm': True, 'unack': True}
        nonjournaled = {points[key] for key in
                        ('power_ok', 'pok_in1', 'pok_in2', 'avail_in1',
                         'avail_in2', 'cmd1', 'cmd2', 'demand',
                         'demand_in', 'duty', 'staged', 'none_in',
                         'ack', 'none_ack')}
        found = {}
        violations = {}

        def journaled_asserts():
            try:
                _, journal = http_json('GET', base + '/journal?since='
                                       + str(floor))
            except Exception:
                return None
            last['journal'] = journal
            changes = _journal_point_changes(journal)
            for key, wanted in asserted_want.items():
                if {'bool': wanted} in changes.get(points[key], []):
                    found[key] = True
            for entry in _journal_list(journal):
                event = entry.get('event') or {}
                if 'role_changed' in event:
                    violations['role-change'] = \
                        'a role_changed event journaled under the ' \
                        'power-fail drive'
                change = event.get('point_changed')
                if isinstance(change, dict) \
                        and change.get('point') in nonjournaled:
                    violations['unjournaled-' + str(change['point'])] = \
                        'the non-journaled point ' \
                        + str(change['point']) \
                        + ' journaled a transition'
            if len(found) == len(asserted_want) or violations:
                return journal
            return None

        wait_for(journaled_asserts,
                 time.monotonic() + POWER_TRIP_DEADLINE,
                 interval=POLL_INTERVAL)
        ref = save_evidence(
            ctx['evidence_dir'], 'power-trip-journal.json',
            {'floor': floor, 'asserted': sorted(found),
             'violations': sorted(violations)})
        case.evidence('file', ref, 'the journaled trip transitions '
                      'above the pre-drive floor')
        if violations:
            return case.finish(
                'failed', 'power-trip-nondeterministic: ' + '; '.join(
                    violations[key] for key in sorted(violations)))
        missing = [key for key in asserted_want if key not in found]
        if missing:
            return case.finish('failed', 'power-trip-failed: the '
                               'served journal never recorded '
                               'point_changed on: '
                               + ', '.join(missing))
        case.observe('journaled: the power-fail transition beside the '
                     'availability drops, the all-out annunciation, '
                     'and the alarm\'s two flags')

        # The acknowledgment leg: a receipted write on the alarm's
        # declared ack input clears the latch while the condition still
        # stands — the managed alarm's ack-dominates rule — and the
        # settlement journals attributed.
        write = {'point': points['ack'], 'kind': 'bool',
                 'value': {'bool': True}}
        status, receipt = http_json(
            'POST', base + '/command',
            {'command': {'write_value': write},
             'actor': POWER_TRIP_ACTOR})
        ref = save_evidence(ctx['evidence_dir'],
                            'power-trip-ack-receipt.json',
                            {'status': status, 'body': receipt})
        case.evidence('file', ref, 'the ack submission receipt')
        outcome = (receipt or {}).get('outcome') or {}
        if status != 200 or 'rejected' in outcome:
            return case.finish('failed', 'power-trip-failed: the ack '
                               'write was refused: ' + str(status)
                               + ' ' + json.dumps(receipt)[:400])
        held_acks.append(points['ack'])
        submitted.append(write)

        hit, error = leg(
            'acknowledged',
            lambda s: value('unack', s) is False
            and value('alarm', s) is True
            and value('power_fail', s) is True
            and value('power_ok', s) is False
            and value('avail1', s) is False
            and value('avail2', s) is False,
            ('power_fail', 'power_ok', 'avail1', 'avail2', 'alarm',
             'unack'))
        if error:
            return error
        case.observe('the settled ack cleared the unacknowledged '
                     'latch at tick ' + str(hit.get('tick'))
                     + ' while the condition stood — the alarm, the '
                     'contact, and the stripped availability all '
                     'unchanged')

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
                 time.monotonic() + POWER_TRIP_DEADLINE,
                 interval=POLL_INTERVAL)
        ref = save_evidence(
            ctx['evidence_dir'], 'power-trip-ack-journal.json',
            {'receipt': settled.get('receipt'),
             'unack_cleared': settled.get('unack_cleared')})
        case.evidence('file', ref, 'the journaled ack settlement')
        settled_receipt = settled.get('receipt')
        if settled_receipt is None:
            return case.finish('failed', 'power-trip-failed: the '
                               'ack\'s CommandSettled never journaled')
        if 'applied' not in (settled_receipt.get('outcome') or {}):
            return case.finish('failed', 'power-trip-failed: the ack '
                               'receipt did not settle applied: '
                               + json.dumps(settled_receipt
                                            .get('outcome'))[:200])
        if settled_receipt.get('actor') != POWER_TRIP_ACTOR:
            return case.finish('failed', 'power-trip-failed: the '
                               'journaled ack receipt is '
                               'unattributed: actor='
                               + json.dumps(settled_receipt
                                            .get('actor')))
        if not settled.get('unack_cleared'):
            return case.finish('failed', 'power-trip-failed: the '
                               'unacknowledged flag\'s clearing never '
                               'journaled')
        case.observe('ack settled applied, journaled attributed to '
                     + POWER_TRIP_ACTOR + ', unacknowledged cleared '
                     'while the condition stood')

        # Re-arm the ack input for the next trip before the restore
        # write lands — a standing true would hold the latch clear.
        restore = {'point': points['ack'], 'kind': 'bool',
                   'value': {'bool': False}}
        status, receipt = http_json(
            'POST', base + '/command',
            {'command': {'write_value': restore},
             'actor': POWER_TRIP_ACTOR})
        ref = save_evidence(ctx['evidence_dir'],
                            'power-trip-ack-restored.json',
                            {'status': status, 'body': receipt})
        case.evidence('file', ref, 'the ack-restore receipt')
        outcome = (receipt or {}).get('outcome') or {}
        if status != 200 or 'rejected' in outcome:
            return case.finish('failed', 'power-trip-failed: the ack '
                               'restore write was refused: '
                               + str(status) + ' '
                               + json.dumps(receipt)[:400])
        submitted.append(restore)
        hit, error = leg('ack-released',
                         lambda s: value('ack', s) is False
                         and value('unack', s) is False,
                         ('ack', 'unack'))
        if error:
            return error
        held_acks.remove(points['ack'])

        # The declared recovery: the contact released, power-ok and
        # the availability legs return, and the group re-stages the
        # standing demand inside the declared bounds.
        verdict = _plant_request(
            stream, {'op': 'write', 'point': points['power_fail'],
                     'value': {'bool': False}})
        if verdict.get('result') != 'done':
            return case.finish('failed', 'power-trip-failed: the '
                               'power-fail restore write was refused: '
                               + json.dumps(verdict)[:300])
        restore_fail = None
        case.observe('power-fail released — the station power '
                     'contact restored')

        recovery_keys = ('power_fail', 'power_ok', 'avail1', 'avail2',
                         'avail_in1', 'avail_in2', 'cmd1', 'cmd2',
                         'duty', 'staged', 'demand', 'none_available',
                         'alarm', 'unack')

        def recovered(snap):
            if value('power_fail', snap) is not False \
                    or value('power_ok', snap) is not True:
                return None
            if value('avail1', snap) is not True \
                    or value('avail2', snap) is not True:
                return None
            if value('none_available', snap) is not False:
                return None
            if value('alarm', snap) is not False \
                    or value('unack', snap) is not False:
                return None
            duty = value('duty', snap)
            if duty not in (1, 2):
                return None
            demand = value('demand', snap)
            if not isinstance(demand, int) or isinstance(demand, bool) \
                    or demand < 1:
                return None
            staged = value('staged', snap)
            if not isinstance(staged, int) or isinstance(staged, bool) \
                    or staged < min(demand, 2):
                return None
            if value('cmd1' if duty == 1 else 'cmd2', snap) is not True:
                return None
            return snap

        hit, error = leg('recovered', recovered, recovery_keys)
        if error:
            return error
        duty1 = value('duty', hit)
        case.observe('the declared recovery at tick '
                     + str(hit.get('tick')) + ': power-ok restored, '
                     'both availability legs back, duty='
                     + str(duty1) + ' re-staged the standing demand='
                     + str(value('demand', hit)))

        # The trip's consequential none-available alarm latched its own
        # unacknowledged flag — clear it through the receipted path and
        # re-arm the input, so the leg leaves the alarm set as found.
        snap = _try_snapshot(ctx, base) or {}
        if _point_value(snap, points['none_unack']) is True:
            none_write = {'point': points['none_ack'], 'kind': 'bool',
                          'value': {'bool': True}}
            status, receipt = http_json(
                'POST', base + '/command',
                {'command': {'write_value': none_write},
                 'actor': POWER_TRIP_ACTOR})
            ref = save_evidence(ctx['evidence_dir'],
                                'power-trip-none-ack.json',
                                {'status': status, 'body': receipt})
            case.evidence('file', ref, 'the none-available ack '
                          'submission receipt')
            outcome = (receipt or {}).get('outcome') or {}
            if status != 200 or 'rejected' in outcome:
                return case.finish('failed', 'power-trip-failed: the '
                                   'none-available ack write was '
                                   'refused: ' + str(status) + ' '
                                   + json.dumps(receipt)[:400])
            held_acks.append(points['none_ack'])
            submitted.append(none_write)
            hit, error = leg(
                'none-acked',
                lambda s: value('none_unack', s) is False,
                ('none_alarm', 'none_unack'))
            if error:
                return error
            none_restore = {'point': points['none_ack'], 'kind': 'bool',
                            'value': {'bool': False}}
            status, receipt = http_json(
                'POST', base + '/command',
                {'command': {'write_value': none_restore},
                 'actor': POWER_TRIP_ACTOR})
            outcome = (receipt or {}).get('outcome') or {}
            if status != 200 or 'rejected' in outcome:
                return case.finish('failed', 'power-trip-failed: the '
                                   'none-available ack restore was '
                                   'refused: ' + str(status) + ' '
                                   + json.dumps(receipt)[:400])
            submitted.append(none_restore)
            hit, error = leg(
                'none-ack-released',
                lambda s: value('none_ack', s) is False
                and value('none_unack', s) is False,
                ('none_ack', 'none_unack'))
            if error:
                return error
            held_acks.remove(points['none_ack'])
            case.observe('the consequential none-available '
                         'annunciation acknowledged and re-armed')

        # The restore audit: the driven contact reads back its
        # baseline, pump operator state the leg never drove is
        # unchanged, and the pair's roles never moved.
        restored = (_plant_read(ctx, points['power_fail'])
                    .get('value') or {}).get('bool')
        if restored is not False:
            return case.finish('failed', 'power-trip-failed: the '
                               'power-fail contact did not restore — '
                               'it reads ' + json.dumps(restored)
                               + ' against baseline false')
        final = _try_snapshot(ctx, base) or {}
        pump1 = {key: value(key, final)
                 for key in ('p1_mode', 'p1_oos', 'p2_mode', 'p2_oos')}
        moved = sorted(key for key in pump1
                       if pump1[key] != pump0.get(key))
        if moved:
            return case.finish('failed', 'power-trip-failed: the leg '
                               'moved pump operator state it never '
                               'drove: ' + ', '.join(moved))
        if _settled_active(ctx) != active:
            return case.finish('failed', 'power-trip-failed: the '
                               'active role moved during the leg')
        if peer_base is not None:
            report = _try_role(ctx, peer_base)
            if report is None \
                    or report.get('role') \
                    != (peer_role0 or {}).get('role'):
                return case.finish('failed', 'power-trip-failed: the '
                                   'pair\'s roles moved during the '
                                   'leg — ' + peer + ' reports '
                                   + json.dumps(report))

        # The journaled audit: every receipted command settled applied
        # and attributed, and the declared-journaled points recorded
        # the whole excursion — the contact, the availability legs, the
        # all-out annunciation's own alarm lifecycle, the run contacts,
        # and the power-fail alarm's two flags — with no role change
        # and no non-journaled point recording a transition.
        expected_pairs = {
            'power_fail': [{'bool': True}, {'bool': False}],
            'avail1': [{'bool': False}, {'bool': True}],
            'avail2': [{'bool': False}, {'bool': True}],
            'none_available': [{'bool': True}, {'bool': False}],
            'none_alarm': [{'bool': True}, {'bool': False}],
            'none_unack': [{'bool': True}, {'bool': False}],
            'alarm': [{'bool': True}, {'bool': False}],
            'unack': [{'bool': True}, {'bool': False}]}
        run_want = {'run' + str(duty0): False,
                    'run' + str(duty1): True}
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
            for key, wanted in expected_pairs.items():
                got = changes.get(points[key], [])
                if got == wanted:
                    complete[key] = got
                elif len(got) > len(wanted) \
                        or got != wanted[:len(got)]:
                    violations['transitions-' + key] = \
                        'point ' + str(points[key]) + ' journaled ' \
                        + json.dumps(got) \
                        + ' — not the declared assert/clear pair'
            for key, wanted in run_want.items():
                if {'bool': wanted} in changes.get(points[key], []):
                    complete[key] = True
            for entry in _journal_list(journal):
                event = entry.get('event') or {}
                if 'role_changed' in event:
                    violations['role-change'] = \
                        'a role_changed event journaled while the ' \
                        'interlock drove'
                change = event.get('point_changed')
                if isinstance(change, dict) \
                        and change.get('point') in nonjournaled:
                    violations['unjournaled-' + str(change['point'])] = \
                        'the non-journaled point ' \
                        + str(change['point']) \
                        + ' journaled a transition'
            if len(complete) == len(expected_pairs) + len(run_want) \
                    or violations:
                return journal
            return None

        wait_for(journaled, time.monotonic() + POWER_TRIP_DEADLINE,
                 interval=POLL_INTERVAL)
        ref = save_evidence(
            ctx['evidence_dir'], 'power-trip-journal-final.json',
            {'floor': floor, 'complete': sorted(complete),
             'violations': sorted(violations)})
        case.evidence('file', ref, 'the journaled excursion above the '
                      'pre-drive floor')
        if violations:
            return case.finish(
                'failed', 'power-trip-nondeterministic: ' + '; '.join(
                    violations[key] for key in sorted(violations)))
        missing = [key for key in list(expected_pairs) + list(run_want)
                   if key not in complete]
        if missing:
            return case.finish('failed', 'power-trip-failed: the '
                               'served journal never recorded the '
                               'declared point_changed evidence on: '
                               + ', '.join(missing))
        settled_receipts = _settled_receipts(last.get('journal') or [])
        for command in submitted:
            receipt_ = next(
                (entry for entry in settled_receipts
                 if (entry.get('command') or {}).get('write_value')
                 == command), None)
            if receipt_ is None:
                return case.finish('failed', 'power-trip-failed: no '
                                   'settled receipt journaled for '
                                   + json.dumps(command)[:200])
            if receipt_.get('actor') != POWER_TRIP_ACTOR:
                return case.finish('failed', 'power-trip-failed: a '
                                   'settled receipt lost its actor: '
                                   + json.dumps(receipt_)[:200])
            if 'applied' not in (receipt_.get('outcome') or {}):
                return case.finish('failed', 'power-trip-failed: a '
                                   'settled receipt did not apply: '
                                   + json.dumps(receipt_)[:200])
        case.observe('journaled: the contact, both availability legs, '
                     'the all-out annunciation and its own alarm '
                     'lifecycle, the run contacts, and the alarm\'s '
                     'two flags — every receipted command settled '
                     'applied and attributed')

        # The tick-domain proof: /history carries every transition in
        # scan order. The write lands between scans so absolute ticks
        # shift with the attachment, but the carrier and staging deltas
        # the declared chain produces stay fixed — the interlock's
        # falling order, the recovery's rising order, and the group's
        # re-stage inside the declared bounds.
        watch = {key: points[key] for key in
                 ('power_fail', 'power_ok', 'pok_in1', 'pok_in2',
                  'avail1', 'avail2', 'avail_in1', 'avail_in2',
                  'cmd1', 'cmd2', 'duty', 'staged', 'demand',
                  'none_available', 'none_in', 'alarm', 'unack',
                  'run1', 'run2')}
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

        def first_ge(key, bound):
            """The tick of `key`'s first transition landing at or
            above `bound` — the re-stage the standing demand earned."""
            for tick, landed in transitions.get(key) or []:
                if isinstance(landed, int) \
                        and not isinstance(landed, bool) \
                        and landed >= bound:
                    return tick
            return None

        t_fail = nth('power_fail', True)
        if t_fail is not None:
            # Re-anchor the windows at the drive's landing scan: the
            # baseline's own start may legitimately land between the
            # catch and the write — only the drive's effects count.
            transitions = {key: [(t, v) for t, v in seq if t >= t_fail]
                           for key, seq in transitions.items()}
        t_back = nth('power_fail', False)
        t_ok0 = nth('power_ok', False)
        t_ok1 = nth('power_ok', True)
        t_av0 = {k: nth('avail' + k, False) for k in ('1', '2')}
        t_av1 = {k: nth('avail' + k, True) for k in ('1', '2')}
        t_avin0 = {k: nth('avail_in' + k, False) for k in ('1', '2')}
        t_avin1 = {k: nth('avail_in' + k, True) for k in ('1', '2')}
        t_st0 = nth('staged', 0)
        t_nav1 = nth('none_available', True)
        t_nav0 = nth('none_available', False)
        t_al1 = nth('alarm', True)
        t_al0 = nth('alarm', False)
        t_un1 = nth('unack', True)
        t_un0 = nth('unack', False)
        t_d0 = nth('duty', 0)
        t_d1 = first_ge('duty', 1)
        t_rs1 = first_ge('staged', 1)
        cmd_up = {}
        cmd_down = {}
        for k in ('1', '2'):
            seq = transitions.get('cmd' + k) or []
            cmd_up[k] = next((t for t, v in seq if v is True), None)
            cmd_down[k] = next((t for t, v in seq if v is False), None)
        restage = min((t for t in cmd_up.values() if t is not None),
                      default=None)
        required = {'power-fail assert': t_fail,
                    'power-fail release': t_back,
                    'power-ok drop': t_ok0,
                    'power-ok return': t_ok1,
                    'avail1 drop': t_av0['1'], 'avail2 drop': t_av0['2'],
                    'avail1 return': t_av1['1'],
                    'avail2 return': t_av1['2'],
                    'avail_in1 drop': t_avin0['1'],
                    'avail_in2 drop': t_avin0['2'],
                    'avail_in1 return': t_avin1['1'],
                    'avail_in2 return': t_avin1['2'],
                    'staged release': t_st0,
                    'none-available assert': t_nav1,
                    'none-available clear': t_nav0,
                    'alarm assert': t_al1, 'alarm return': t_al0,
                    'unack latch': t_un1, 'unack clear': t_un0,
                    'duty release': t_d0, 'duty restage': t_d1,
                    'first re-stage': restage}
        missing = sorted(key for key, tick in required.items()
                         if tick is None)
        ref = save_evidence(
            ctx['evidence_dir'], 'power-trip-timing.json',
            {'drive_tick': drive_tick,
             'bounds': {'start_delay_ticks': start_delay,
                        'min_off_ticks': min_off},
             'transitions': {key: [[t, v] for t, v in seq]
                             for key, seq in
                             sorted(transitions.items())},
             'anchors': {key: required[key]
                         for key in sorted(required)}})
        case.evidence('file', ref, 'the tick-domain transition '
                      'evidence — carrier order and the declared '
                      'restage bound')
        if missing:
            return case.finish('failed', 'power-trip-failed: the '
                               'served history never showed '
                               + ', '.join(missing)
                               + ': ' + json.dumps(
                                   {key: transitions[key]
                                    for key in ('staged', 'avail1',
                                                'alarm')})[:400])

        # Every watched carrier and flag follows the declared
        # assert/clear pair exactly — an extra toggle is a step
        # outside the deterministic scan sequence.
        pair_shapes = {
            'power_fail': [True, False], 'power_ok': [False, True],
            'pok_in1': [False, True], 'pok_in2': [False, True],
            'avail1': [False, True], 'avail2': [False, True],
            'avail_in1': [False, True], 'avail_in2': [False, True],
            'none_available': [True, False],
            'none_in': [True, False],
            'alarm': [True, False], 'unack': [True, False]}
        problems = []
        for key, wanted in pair_shapes.items():
            got = [v for _, v in transitions.get(key) or []]
            if got != wanted:
                problems.append(key + ' transitioned '
                                + json.dumps(got)
                                + ' — not the declared '
                                + json.dumps(wanted) + ' pair')
        # The command points: the pre-trip holder releases first; any
        # later assertion is the declared re-stage. A command landing
        # true before its own availability returned is a step outside
        # the sequence; so is a re-start inside the banked holdout.
        for k in ('1', '2'):
            seq = [v for _, v in transitions.get('cmd' + k) or []]
            first = seq[0] if seq else None
            holder = cmd_down[k] is not None
            if holder and first is not False:
                problems.append('p10' + k + '-cmd\'s first transition '
                                'was not the release: '
                                + json.dumps(seq))
            if not holder and first is not None and first is not True:
                problems.append('p10' + k + '-cmd transitioned '
                                + json.dumps(seq)
                                + ' — no declared step produces that')
            if len(seq) > 2:
                problems.append('p10' + k + '-cmd stepped '
                                + json.dumps(seq)
                                + ' — beyond the release/re-stage pair')
            if cmd_up[k] is not None and cmd_up[k] < t_avin1[k]:
                problems.append('p10' + k + '-cmd re-staged '
                                + str(t_avin1[k] - cmd_up[k])
                                + ' ticks before its availability '
                                'returned')
            if cmd_up[k] is not None and cmd_down[k] is not None \
                    and cmd_up[k] < cmd_down[k] + min_off:
                problems.append('p10' + k + '-cmd re-staged '
                                + str(cmd_up[k] - cmd_down[k])
                                + ' ticks after its stop — inside the '
                                'banked min_off_ticks '
                                + str(min_off) + ' holdout')
        duty_seq = [v for _, v in transitions.get('duty') or []]
        if duty_seq != [0, duty1]:
            problems.append('duty transitioned '
                            + json.dumps(duty_seq)
                            + ' — not the declared release/re-name '
                            'pair')
        staged_seq = [v for _, v in transitions.get('staged') or []]
        if not staged_seq or staged_seq[0] != 0:
            problems.append('staged\'s first transition was not the '
                            'release: ' + json.dumps(staged_seq))
        else:
            for tick, landed in transitions['staged']:
                if t_fail <= tick <= t_back and landed != 0:
                    problems.append('staged served ' + str(landed)
                                    + ' at tick ' + str(tick)
                                    + ' — an output step inside the '
                                    'outage')
        # The chain keeps calling: no demand transition may land below
        # the standing request while the outage stands.
        for tick, landed in transitions.get('demand') or []:
            if t_fail <= tick <= restage \
                    and isinstance(landed, int) \
                    and not isinstance(landed, bool) and landed < 1:
                problems.append('demand fell to ' + str(landed)
                                + ' at tick ' + str(tick)
                                + ' — the station stopped calling '
                                'under the outage')
        # The declared carrier order, falling and rising.
        if t_ok0 < t_fail:
            problems.append('power-ok dropped before the driven '
                            'contact landed')
        if t_al1 < t_fail or t_un1 < t_al1:
            problems.append('the alarm lifecycle did not follow the '
                            'contact in order')
        for k in ('1', '2'):
            if t_av0[k] < t_ok0 or t_avin0[k] < t_av0[k]:
                problems.append('the p10' + k + ' availability chain '
                                'did not drop in carrier order')
            if t_av1[k] < t_ok1 or t_avin1[k] < t_av1[k]:
                problems.append('the p10' + k + ' availability chain '
                                'did not return in carrier order')
        if t_st0 < max(t_avin0['1'], t_avin0['2']):
            problems.append('the group released before availability '
                            'was lost')
        if t_nav1 < t_st0 or t_d0 < t_st0:
            problems.append('the all-out state did not follow the '
                            'release')
        if t_ok1 < t_back:
            problems.append('power-ok returned before the contact '
                            'released')
        if t_un0 >= t_back:
            problems.append('the latch cleared only after the '
                            'condition released — the ack did not '
                            'dominate while it stood')
        if t_al0 < t_back:
            problems.append('the standing alarm cleared before the '
                            'contact released')
        if t_nav0 < min(t_avin1['1'], t_avin1['2']):
            problems.append('none-available cleared before '
                            'availability returned')
        if t_d1 != restage:
            problems.append('duty re-named at tick ' + str(t_d1)
                            + ' — off the re-stage scan '
                            + str(restage))
        # The declared re-stage bound: the first new start lands no
        # earlier than any pump's delivered availability — and no
        # later than the earliest eligible pump plus the inter-pump
        # delay — while each re-started pump honors its banked
        # min_off_ticks holdout.
        earliest = min(
            max(t_avin1[k],
                (cmd_down[k] + min_off) if cmd_down[k] is not None
                else 0)
            for k in ('1', '2'))
        if restage < earliest:
            problems.append('the group re-staged ' + str(
                earliest - restage) + ' ticks before the earliest '
                'eligible pump — a step outside the deterministic '
                'scan sequence')
        if restage > earliest + start_delay + 1:
            problems.append('the group re-staged '
                            + str(restage - earliest) + ' ticks after '
                            'the earliest eligible pump — beyond '
                            'start_delay_ticks ' + str(start_delay))
        if t_rs1 is not None and restage != t_rs1:
            problems.append('staged\'s re-stage tick ' + str(t_rs1)
                            + ' != the first command assertion '
                            + str(restage))
        ups = sorted(t for t in cmd_up.values() if t is not None)
        if len(ups) == 2:
            gap = ups[1] - ups[0]
            if gap < start_delay:
                problems.append('the second re-stage landed '
                                + str(gap) + ' ticks after the first '
                                '— inside start_delay_ticks '
                                + str(start_delay))
            if gap > start_delay + 1:
                problems.append('the second re-stage landed '
                                + str(gap) + ' ticks after the first '
                                '— beyond the declared staging '
                                'bound')
        if problems:
            return case.finish('failed',
                               'power-trip-nondeterministic: '
                               + '; '.join(problems))
        case.observe('the excursion followed the declared carrier '
                     'order and the re-stage landed inside '
                     'start_delay_ticks=' + str(start_delay)
                     + ' / min_off_ticks=' + str(min_off)
                     + ' with no step outside the scan sequence')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
    finally:
        # The driven contact is the run's shared field and the ack
        # inputs the alarms' operator points: a case that leaves any
        # standing poisons every later leg — the drive back to its
        # baseline, every held ack re-armed, and any unacknowledged
        # latch the legs left standing acknowledged best-effort.
        if stream is not None:
            if restore_fail is not None:
                try:
                    _plant_request(
                        stream, {'op': 'write',
                                 'point': restore_fail,
                                 'value': {'bool': False}})
                except Exception:
                    pass
            try:
                stream.close()
            except Exception:
                pass
        base_ = live['base']
        if base_ is not None:
            for point in held_acks:
                try:
                    http_json('POST', base_ + '/command',
                              {'command': {'write_value': {
                                  'point': point, 'kind': 'bool',
                                  'value': {'bool': False}}},
                               'actor': POWER_TRIP_ACTOR})
                except Exception:
                    pass
            for ack_point, unack_point in cleanup.items():
                try:
                    snap_ = _try_snapshot(ctx, base_) or {}
                    if _point_value(snap_, unack_point) is True:
                        http_json('POST', base_ + '/command',
                                  {'command': {'write_value': {
                                      'point': ack_point,
                                      'kind': 'bool',
                                      'value': {'bool': True}}},
                                   'actor': POWER_TRIP_ACTOR})
                        wait_for(
                            lambda: (_point_value(
                                _try_snapshot(ctx, base_) or {},
                                unack_point) is False),
                            time.monotonic() + 10,
                            interval=POLL_INTERVAL)
                        http_json('POST', base_ + '/command',
                                  {'command': {'write_value': {
                                      'point': ack_point,
                                      'kind': 'bool',
                                      'value': {'bool': False}}},
                                   'actor': POWER_TRIP_ACTOR})
                except Exception:
                    pass
