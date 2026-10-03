"""The lag_staging acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: The lag-staging case sits in the same restored window: the settled
# pair's pinned owner token is the shared claim its inflow drive
# needs, and the leg writes, stages, annunciates, acks, drains, and
# restores — inflow back to baseline, the ack input re-armed, no pump
# operator state touched, no role moved.
RUNS_BEFORE = frozenset({'scenario_parameter_tune_carryover'})


# --------------------------------------------------------------------
# The threshold-chain lag-staging and high-level annunciation leg
# (WW-CTL-002/WW-OPS-002 — decision 42's ordered setpoint chain on the
# deployed station). The dynamics' declared forcing input is `inflow`:
# no element drives it, so the harness writes it through the plant
# protocol's shared-claim path — ctx['plant_owner'] carries the
# --owner-token each controller pinned, and the scenario attachment
# ensures the writer claim under the settled active's token, the
# designed test-harness claim the sim's fencing grants `claimed_shared`.
# With the pair settled the leg raises the well through the served
# chain — demand 0 -> 1 -> 2 with duty_call/lag_call answering and the
# group's staged inside start_delay_ticks of the demand it consumed —
# crosses high into the managed high-level alarm's two-flag lifecycle
# with journaled point_changed evidence, then drives the falling edge:
# the lag de-stages before the duty releases, the alarm returns on its
# declared hysteresis, and a receipted ack clears the latch before the
# restore write lands. Functional misses name staging-failed; ordering,
# delay-bound, and journal-contract violations name
# staging-nondeterministic.

LAG_STAGING_DEADLINE = 60  # bound on each leg's served transition —
                         # the level traverse spans a few dozen scans
                         # under the deployed dynamics' rates
LAG_STAGING_POLL = 0.05    # transition-watch cadence — under the scan
LAG_STAGING_ACTOR = 'qa-lane'


def scenario_lag_staging(ctx):
    """Drive the wet well through the deployed threshold chain — demand
    0 -> 1 -> 2 with the pump group staging behind start_delay_ticks,
    the high-level alarm's two-flag lifecycle journaled — then down the
    falling edge de-staging the lag before the duty releases."""
    case = Case('lag-staging',
                'Threshold-chain lag staging and high-level '
                'annunciation',
                'with the deployed pair settled and tracking, a plant-'
                'protocol inflow write under the active\'s shared '
                'writer claim drives the level through the served '
                'setpoint chain: demand moves 0 -> 1 -> 2 with '
                'duty_call and lag_call asserting at the start and '
                'lag_start crossings, the pump group\'s staged answers '
                'inside start_delay_ticks of the demand it consumed, '
                'the high crossing asserts high_level and stands the '
                'managed alarm unacknowledged with journaled '
                'point_changed evidence, the falling edge de-stages '
                'the lag before the duty releases, the receipted ack '
                'clears the latch, and the driven inputs restore with '
                'the pair\'s roles unchanged')
    stream = None
    restore_inflow = None  # (point, baseline) while the write stands
    restore_ack = None     # (base, point) while the ack write stands
    try:
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        base = ctx[active]
        tracking = wait_for(lambda: _tracking_peer(ctx, active),
                            time.monotonic() + LAG_STAGING_DEADLINE,
                            interval=POLL_INTERVAL)
        if tracking is None:
            return case.finish('inconclusive',
                               'no tracking peer — the deployed pair '
                               'never settled')
        case.observe('settled pair: ' + active + ' active, '
                     + tracking + ' tracking')

        _, signals = http_json('GET', base + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'lag-staging-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming the '
                      'threshold-chain staging path')
        names = {'inflow': 'inflow', 'level-selected': 'level',
                 'demand': 'demand', 'demand-in': 'demand_in',
                 'duty': 'duty', 'staged': 'staged',
                 'duty-call': 'duty_call', 'lag-call': 'lag_call',
                 'below-cutoff': 'below_cutoff',
                 'high-level': 'high_level', 'lah-ack': 'ack',
                 'lah-alarm': 'alarm',
                 'lah-unacknowledged': 'unack',
                 'lah-shelved': 'shelved',
                 'lah-suppressed': 'suppressed',
                 'lah-out-of-service': 'out_of_service',
                 'p101-mode': 'p1_mode', 'p101-oos': 'p1_oos',
                 'p102-mode': 'p2_mode', 'p102-oos': 'p2_oos'}
        entries = {entry.get('name'): entry
                   for entry in signals.get('points', [])
                   if entry.get('name') in names}
        missing = sorted(set(names) - set(entries))
        if missing:
            return case.finish('inconclusive', 'the deployed model '
                               'lacks the threshold-chain staging '
                               'wiring — no signals '
                               + ', '.join(missing))
        ack_entry = entries['lah-ack']
        if not ack_entry.get('writable') \
                or ack_entry.get('direction') != 'in' \
                or ack_entry.get('value_type') != 'bool':
            return case.finish('inconclusive', 'the lah-ack point is '
                               'not the alarm\'s writable bool ack '
                               'input: ' + json.dumps(ack_entry)[:300])
        points = {names[name]: entry.get('point')
                  for name, entry in entries.items()}
        case.observe('staging path: '
                     + json.dumps({name: entry.get('point')
                                   for name, entry in
                                   sorted(entries.items())},
                                  sort_keys=True))

        _, schema = http_json('GET', base + '/schema')
        snap0 = _snapshot(ctx, base)
        chain_name = _component_instance(schema, 'threshold-chain')
        group_name = _component_instance(schema, 'pump-group')
        alarm_name = _component_instance(
            schema, 'managed-latching-alarm', points['alarm'])
        if chain_name is None or group_name is None \
                or alarm_name is None:
            return case.finish('inconclusive', 'the served schema '
                               'lacks the threshold-chain, pump-group, '
                               'or the managed-latching-alarm instance '
                               'wired to the lah-alarm point')
        table = {key: _parameter_value(snap0, chain_name, key)
                 for key in ('cutoff', 'stop', 'start', 'lag_start',
                             'high')}
        start_delay = _parameter_value(snap0, group_name,
                                       'start_delay_ticks')
        high_limit = _parameter_value(snap0, alarm_name, 'high_limit')
        hysteresis = _parameter_value(snap0, alarm_name, 'hysteresis')
        ref = save_evidence(
            ctx['evidence_dir'], 'lag-staging-chain.json',
            {'points': {key: points[key] for key in sorted(points)},
             'components': {'chain': chain_name, 'group': group_name,
                            'alarm': alarm_name},
             'setpoints': table,
             'start_delay_ticks': start_delay,
             'high_limit': high_limit, 'hysteresis': hysteresis})
        case.evidence('file', ref, 'the served setpoint chain, the '
                      'staging delay, and the alarm limits')
        ordered = [table[key]
                   for key in ('cutoff', 'stop', 'start', 'lag_start',
                               'high')]
        if any(not isinstance(value, (int, float))
               or isinstance(value, bool) or not math.isfinite(value)
               for value in ordered):
            return case.finish('inconclusive', 'the served setpoint '
                               'chain is incomplete: '
                               + json.dumps(table))
        if any(ordered[i] >= ordered[i + 1]
               for i in range(len(ordered) - 1)):
            return case.finish('inconclusive', 'the served setpoint '
                               'chain is not strictly increasing: '
                               + json.dumps(table))
        if not isinstance(start_delay, int) \
                or isinstance(start_delay, bool) or start_delay < 0:
            return case.finish('inconclusive', 'the pump group serves '
                               'no usable start_delay_ticks: '
                               + json.dumps(start_delay))
        if not isinstance(high_limit, (int, float)) \
                or isinstance(high_limit, bool) \
                or not math.isfinite(high_limit) \
                or high_limit != table['high']:
            return case.finish('inconclusive', 'the wired alarm\'s '
                               'high_limit is not the chain\'s served '
                               'high: ' + json.dumps(high_limit))
        if not isinstance(hysteresis, (int, float)) \
                or isinstance(hysteresis, bool) \
                or not math.isfinite(hysteresis):
            hysteresis = 0.0

        if ctx.get('plant') is None:
            return case.finish('inconclusive',
                               'the run publishes no plant endpoint')
        owner = (ctx.get('plant_owner') or {}).get(active)
        if owner is None:
            return case.finish('inconclusive', 'the run pins no '
                               'plant-writer owner token for the '
                               'settled active ' + str(active))
        # The claim seam splits here: the field census and the inflow
        # reads ride the shipped dcs-plant-ctl (list/read need no
        # writer claim), while the standing shared claim and the
        # writes under it stay on the raw client — the tool exposes no
        # ensure_writer under a chosen owner token, and its `write`
        # would claim under the tool token and fence against the
        # active's standing claim rather than join it.
        stream = _plant_connect(ctx)
        field = _field_inputs(ctx)
        if points['inflow'] not in field:
            return case.finish('inconclusive', 'the inflow signal\'s '
                               'point ' + str(points['inflow'])
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
        baseline_inflow = (_plant_read(ctx, points['inflow'])
                           .get('value') or {}).get('float')
        if not isinstance(baseline_inflow, (int, float)) \
                or isinstance(baseline_inflow, bool) \
                or not math.isfinite(baseline_inflow):
            return case.finish('inconclusive', 'the inflow point '
                               'serves no float baseline: '
                               + json.dumps(baseline_inflow))

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
            reported a sample, staging-failed when the served values
            never landed the leg."""
            hit = wait_for(lambda: poll(cond, keys),
                           time.monotonic() + LAG_STAGING_DEADLINE,
                           interval=LAG_STAGING_POLL)
            snap = last.get('snap') or {}
            ref = save_evidence(
                ctx['evidence_dir'], 'lag-staging-' + name + '.json',
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
                'failed', 'staging-failed: the ' + name + ' leg never '
                'landed; last served ' + json.dumps(
                    {key: value(key, snap) for key in sorted(keys)},
                    sort_keys=True)[:500])

        def settled_low(snap):
            if value('demand', snap) != 0 \
                    or value('staged', snap) != 0:
                return None
            for key in ('duty_call', 'lag_call', 'below_cutoff',
                        'high_level', 'alarm', 'unack', 'shelved',
                        'suppressed', 'out_of_service'):
                if value(key, snap) is not False:
                    return None
            level = value('level', snap)
            if not isinstance(level, (int, float)) \
                    or isinstance(level, bool) \
                    or level >= table['start']:
                return None
            return snap

        baseline = wait_for(lambda: poll(settled_low),
                            time.monotonic() + LAG_STAGING_DEADLINE,
                            interval=LAG_STAGING_POLL)
        snap = last.get('snap') or {}
        pump0 = {key: value(key, snap)
                 for key in ('p1_mode', 'p1_oos', 'p2_mode', 'p2_oos',
                             'duty')}
        ref = save_evidence(
            ctx['evidence_dir'], 'lag-staging-baseline.json',
            {'tick': snap.get('tick'), 'inflow': baseline_inflow,
             'level': value('level', snap), 'pump': pump0})
        case.evidence('file', ref, 'the settled low-level baseline')
        if baseline is None:
            return case.finish('inconclusive', 'the station never '
                               'read the settled low-level baseline '
                               'ahead of the drive')

        # The journal floor ahead of the drive: this leg's records are
        # the ones above it. The snapshot tick anchors the /history
        # transition scan.
        _, journal0 = http_json('GET', base + '/journal?since=0')
        floor = max((entry.get('seq') or 0
                     for entry in _journal_list(journal0)
                     if isinstance(entry, dict)), default=0)
        drive_tick = baseline.get('tick') or 0

        # The rising edge: inflow above the high setpoint and both
        # pumps' declared draw, so the level climbs unopposed through
        # every threshold — the honest lever the dynamics document.
        rise_inflow = table['high'] + 1.0
        verdict = _plant_request(
            stream, {'op': 'write', 'point': points['inflow'],
                     'value': {'float': rise_inflow}})
        if verdict.get('result') != 'done':
            return case.finish('failed', 'staging-failed: the inflow '
                               'write on point ' + str(points['inflow'])
                               + ' was refused under the shared '
                               'claim: ' + json.dumps(verdict)[:300])
        restore_inflow = (points['inflow'], baseline_inflow)
        case.observe('inflow driven to ' + str(rise_inflow)
                     + ' — past the ' + str(table['high'])
                     + ' high setpoint and both pumps\' draw')

        hit, error = leg(
            'duty-call',
            lambda s: isinstance(value('demand', s), int)
            and not isinstance(value('demand', s), bool)
            and value('demand', s) >= 1
            and value('duty_call', s) is True,
            ('demand', 'demand_in', 'duty_call', 'staged'))
        if error:
            return error
        case.observe('demand staged to '
                     + str(value('demand', hit)) + ' with duty_call '
                     'at tick ' + str(hit.get('tick')))

        hit, error = leg(
            'lag-call',
            lambda s: value('demand', s) == 2
            and value('lag_call', s) is True,
            ('demand', 'demand_in', 'duty_call', 'lag_call', 'staged'))
        if error:
            return error
        case.observe('demand staged to 2 with lag_call at tick '
                     + str(hit.get('tick')))

        hit, error = leg(
            'staged',
            lambda s: value('staged', s) == 2,
            ('demand', 'demand_in', 'staged'))
        if error:
            return error
        case.observe('the pump group staged both pumps at tick '
                     + str(hit.get('tick')))

        hit, error = leg(
            'annunciated',
            lambda s: value('high_level', s) is True
            and value('alarm', s) is True
            and value('unack', s) is True
            and value('shelved', s) is False
            and value('suppressed', s) is False
            and value('out_of_service', s) is False,
            ('level', 'high_level', 'alarm', 'unack', 'shelved',
             'suppressed', 'out_of_service'))
        if error:
            return error
        case.observe('the high crossing annunciated at tick '
                     + str(hit.get('tick')) + ': high_level, the '
                     'managed alarm standing unacknowledged, no '
                     'shelved/suppressed/out-of-service flag')
        if _settled_active(ctx) != active:
            return case.finish('failed', 'staging-failed: the active '
                               'role moved while the level rose — a '
                               'process drive is not peer loss')

        # The falling edge: the inflow back to its baseline and the
        # running pumps pull the level down through the chain.
        verdict = _plant_request(
            stream, {'op': 'write', 'point': points['inflow'],
                     'value': {'float': baseline_inflow}})
        if verdict.get('result') != 'done':
            return case.finish('failed', 'staging-failed: the inflow '
                               'restore write was refused: '
                               + json.dumps(verdict)[:300])

        hit, error = leg(
            'destaged',
            lambda s: isinstance(value('staged', s), int)
            and not isinstance(value('staged', s), bool)
            and value('staged', s) <= 1
            and isinstance(value('demand', s), int)
            and not isinstance(value('demand', s), bool)
            and value('demand', s) <= 1
            and value('lag_call', s) is False
            and value('duty_call', s) is True,
            ('demand', 'demand_in', 'staged', 'duty_call', 'lag_call',
             'high_level', 'alarm'))
        if error:
            return error
        case.observe('the lag de-staged (staged '
                     + str(value('staged', hit)) + ') at tick '
                     + str(hit.get('tick')) + ' while duty_call still '
                     'stood — the declared lag-first release order')

        hit, error = leg(
            'released',
            lambda s: value('demand', s) == 0
            and value('staged', s) == 0
            and value('duty_call', s) is False
            and value('high_level', s) is False
            and value('alarm', s) is False
            and value('unack', s) is True,
            ('demand', 'demand_in', 'staged', 'duty_call', 'lag_call',
             'high_level', 'alarm', 'unack'))
        if error:
            return error
        case.observe('pump-down complete at tick '
                     + str(hit.get('tick')) + ' — the duty released, '
                     'the alarm returned on its hysteresis, the latch '
                     'still standing for the ack')

        # The lifecycle's second flag: the receipted ack clears the
        # held unacknowledged latch; the restore write then re-arms
        # the input for the next trip.
        write = {'point': points['ack'], 'kind': 'bool',
                 'value': {'bool': True}}
        status, receipt = http_json(
            'POST', base + '/command',
            {'command': {'write_value': write},
             'actor': LAG_STAGING_ACTOR})
        ref = save_evidence(ctx['evidence_dir'],
                            'lag-staging-ack-receipt.json',
                            {'status': status, 'body': receipt})
        case.evidence('file', ref, 'the ack submission receipt')
        outcome = (receipt or {}).get('outcome') or {}
        if status != 200 or 'rejected' in outcome:
            return case.finish('failed', 'staging-failed: the ack '
                               'write was refused: ' + str(status)
                               + ' ' + json.dumps(receipt)[:400])
        restore_ack = (base, points['ack'])

        hit, error = leg('acknowledged',
                         lambda s: value('unack', s) is False,
                         ('alarm', 'unack'))
        if error:
            return error
        case.observe('the settled ack cleared the unacknowledged '
                     'latch at tick ' + str(hit.get('tick')))

        restore = {'point': points['ack'], 'kind': 'bool',
                   'value': {'bool': False}}
        status, receipt = http_json(
            'POST', base + '/command',
            {'command': {'write_value': restore},
             'actor': LAG_STAGING_ACTOR})
        ref = save_evidence(ctx['evidence_dir'],
                            'lag-staging-ack-restored.json',
                            {'status': status, 'body': receipt})
        case.evidence('file', ref, 'the ack-restore receipt')
        outcome = (receipt or {}).get('outcome') or {}
        if status != 200 or 'rejected' in outcome:
            return case.finish('failed', 'staging-failed: the ack '
                               'restore write was refused: '
                               + str(status) + ' '
                               + json.dumps(receipt)[:400])

        hit, error = leg('ack-released',
                         lambda s: value('ack', s) is False
                         and value('unack', s) is False,
                         ('ack', 'unack'))
        if error:
            return error
        restore_ack = None

        restored = (_plant_read(ctx, points['inflow'])
                    .get('value') or {}).get('float')
        if restored != baseline_inflow:
            return case.finish('failed', 'staging-failed: the inflow '
                               'point did not restore — it reads '
                               + json.dumps(restored)
                               + ' against baseline '
                               + json.dumps(baseline_inflow))
        restore_inflow = None

        # The durable record: each alarm-side transition lands its
        # point_changed on a declared-journaled point — assert then
        # clear — while the pair journaled no role change and no
        # non-journaled staging point records a transition.
        expected_journal = {
            'high_level': [{'bool': True}, {'bool': False}],
            'alarm': [{'bool': True}, {'bool': False}],
            'unack': [{'bool': True}, {'bool': False}]}
        nonjournaled = {points[key] for key in
                        ('demand', 'demand_in', 'duty', 'staged',
                         'duty_call', 'lag_call')}
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
            for key, wanted in expected_journal.items():
                got = changes.get(points[key], [])
                if got == wanted:
                    complete[key] = got
                elif len(got) > len(wanted) or got != wanted[:len(got)]:
                    violations['transitions-' + key] = \
                        'point ' + str(points[key]) + ' journaled ' \
                        + json.dumps(got) \
                        + ' — not the declared assert/clear pair'
            for entry in _journal_list(journal):
                event = entry.get('event') or {}
                if 'role_changed' in event:
                    violations['role-change'] = \
                        'a role_changed event journaled while the ' \
                        'level drove'
                change = event.get('point_changed')
                if isinstance(change, dict) \
                        and change.get('point') in nonjournaled:
                    violations['unjournaled-' + str(change['point'])] = \
                        'the non-journaled point ' \
                        + str(change['point']) \
                        + ' journaled a transition'
            if len(complete) == len(expected_journal) or violations:
                return journal
            return None

        wait_for(journaled, time.monotonic() + LAG_STAGING_DEADLINE,
                 interval=POLL_INTERVAL)
        ref = save_evidence(
            ctx['evidence_dir'], 'lag-staging-journal.json',
            {'floor': floor, 'complete': sorted(complete),
             'violations': sorted(violations)})
        case.evidence('file', ref, 'the journaled transitions above '
                      'the pre-drive floor')
        if violations:
            return case.finish(
                'failed', 'staging-nondeterministic: ' + '; '.join(
                    violations[key] for key in sorted(violations)))
        missing = [key for key in expected_journal
                   if key not in complete]
        if missing:
            return case.finish('failed', 'staging-failed: the served '
                               'journal never recorded the declared '
                               'point_changed assert/clear pair on: '
                               + ', '.join(missing))

        # The tick-domain proof: /history carries every crossing in
        # scan order. Each edge anchors at its own demand-1 transition
        # — the harness write lands between scans, so absolute ticks
        # shift with the attachment while the inter-transition deltas
        # the chain, carrier, and delay produce stay fixed.
        watch = {key: points[key] for key in
                 ('demand', 'demand_in', 'staged', 'duty_call',
                  'lag_call', 'high_level', 'alarm', 'unack', 'duty')}
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

        rise_anchor = nth('demand', 1)
        rise = {'demand-1': rise_anchor,
                'duty_call': nth('duty_call', True),
                'staged-1': nth('staged', 1),
                'demand-2': nth('demand', 2),
                'lag_call': nth('lag_call', True),
                'staged-2': nth('staged', 2),
                'high_level': nth('high_level', True),
                'alarm': nth('alarm', True),
                'unack': nth('unack', True)}
        missing = sorted(key for key, tick in rise.items()
                         if tick is None)
        if missing:
            return case.finish('failed', 'staging-failed: the served '
                               'history never showed '
                               + ', '.join(missing)
                               + ' on the rising edge: '
                               + json.dumps({key: transitions[key]
                                             for key in
                                             ('demand', 'staged',
                                              'alarm')})[:400])
        deltas_rise = {key: rise[key] - rise_anchor
                       for key in rise}

        fall_anchor = nth('demand', 1, 2)
        fall = {'high_level': nth('high_level', False),
                'alarm': nth('alarm', False),
                'demand-1': fall_anchor,
                'lag_call': nth('lag_call', False),
                'staged-1': nth('staged', 1, 2),
                'demand-0': nth('demand', 0),
                'duty_call': nth('duty_call', False),
                'staged-0': nth('staged', 0)}
        missing = sorted(key for key, tick in fall.items()
                         if tick is None)
        if missing:
            return case.finish('failed', 'staging-failed: the served '
                               'history never showed '
                               + ', '.join(missing)
                               + ' on the falling edge: '
                               + json.dumps({key: transitions[key]
                                             for key in
                                             ('demand', 'staged',
                                              'alarm')})[:400])
        deltas_fall = {key: fall[key] - fall_anchor
                       for key in fall}

        problems = []
        if rise['duty_call'] != rise_anchor:
            problems.append('duty_call did not assert with demand 1 '
                            '(+' + str(rise['duty_call'] - rise_anchor)
                            + ')')
        if not rise['demand-1'] <= rise['staged-1'] \
                <= rise['demand-2']:
            problems.append('the duty stage did not answer between '
                            'demand 1 and demand 2')
        if rise['lag_call'] != rise['demand-2']:
            problems.append('lag_call did not assert with demand 2 '
                            '(+' + str(rise['lag_call']
                                       - rise['demand-2']) + ')')
        if not rise['demand-2'] <= rise['staged-2'] \
                <= rise['high_level']:
            problems.append('the lag stage did not answer between '
                            'demand 2 and the high crossing')
        if not rise['high_level'] <= rise['alarm'] <= rise['unack']:
            problems.append('the annunciation did not follow the '
                            'high crossing in order')
        # The declared delay: the group's staged answers within
        # start_delay_ticks of the demand it consumed — the 205
        # carrier delivers the served demand one scan later, so the
        # served-demand bound is the delay plus that scan.
        for k in (1, 2):
            staged_t = rise['staged-' + str(k)]
            served_t = rise['demand-' + str(k)]
            consumed_t = nth('demand_in', k)
            if staged_t - served_t > start_delay + 1:
                problems.append(
                    'staged ' + str(k) + ' answered '
                    + str(staged_t - served_t) + ' ticks after demand '
                    + str(k) + ' — beyond start_delay_ticks '
                    + str(start_delay) + ' plus the carrier scan')
            if consumed_t is not None \
                    and staged_t - consumed_t > start_delay:
                problems.append(
                    'staged ' + str(k) + ' answered '
                    + str(staged_t - consumed_t) + ' ticks after the '
                    'group\'s consumed demand ' + str(k)
                    + ' — beyond start_delay_ticks '
                    + str(start_delay))
        if not fall['high_level'] <= fall['alarm'] \
                <= fall['demand-1'] <= fall['lag_call'] \
                <= fall['staged-1'] <= fall['demand-0'] \
                <= fall['duty_call'] <= fall['staged-0']:
            problems.append('the falling edge did not release in '
                            'order — high_level, alarm, lag demand, '
                            'lag stage, duty demand, duty stage: '
                            + json.dumps(deltas_fall, sort_keys=True))
        if fall['staged-1'] >= fall['staged-0']:
            problems.append('the lag never de-staged ahead of the '
                            'duty release')
        if problems:
            return case.finish('failed',
                               'staging-nondeterministic: '
                               + '; '.join(problems))

        # The restore audit: pump operator state the leg never drove
        # reads back unchanged, and the pair's roles never moved.
        final = _try_snapshot(ctx, base) or {}
        pump1 = {key: value(key, final)
                 for key in ('p1_mode', 'p1_oos', 'p2_mode', 'p2_oos')}
        moved = sorted(key for key in pump1
                       if pump1[key] != pump0.get(key))
        if moved:
            return case.finish('failed', 'staging-failed: the leg '
                               'moved pump operator state it never '
                               'drove: ' + ', '.join(moved))
        if _settled_active(ctx) != active \
                or _tracking_peer(ctx, active) != tracking:
            return case.finish('failed', 'staging-failed: the pair\'s '
                               'roles moved during the leg')
        ref = save_evidence(
            ctx['evidence_dir'], 'lag-staging-legs.json',
            {'drive': {'inflow_rise': rise_inflow,
                       'inflow_restored': baseline_inflow},
             'setpoints': table,
             'start_delay_ticks': start_delay,
             'rise': deltas_rise,
             'fall': deltas_fall,
             'duty': [[tick - fall_anchor, landed]
                      for tick, landed in transitions['duty']],
             'journaled': {key: complete[key]
                           for key in sorted(complete)}})
        case.evidence('file', ref, 'the tick-domain transition '
                      'evidence — deltas anchored per edge')
        case.observe('chain walked: rise deltas '
                     + json.dumps(deltas_rise, sort_keys=True)
                     + ', fall deltas '
                     + json.dumps(deltas_fall, sort_keys=True))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
    finally:
        # The inflow write and the ack input are the run's shared
        # state: a case that leaves either standing poisons every
        # later scenario.
        if stream is not None:
            if restore_inflow is not None:
                try:
                    _plant_request(
                        stream, {'op': 'write',
                                 'point': restore_inflow[0],
                                 'value': {'float': restore_inflow[1]}})
                except Exception:
                    pass
            try:
                stream.close()
            except Exception:
                pass
        if restore_ack is not None:
            rbase, rpoint = restore_ack
            try:
                http_json('POST', rbase + '/command',
                          {'command': {'write_value': {
                              'point': rpoint, 'kind': 'bool',
                              'value': {'bool': False}}},
                           'actor': LAG_STAGING_ACTOR})
            except Exception:
                pass
