"""The duty_handover acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *


# --------------------------------------------------------------------
# The duty-pump failure handover leg (WW-CTL-001, WW-OPS-001 —
# decision 41's duty/standby contract on the deployed station). The
# rig's instrument and plant-failure legs exercise failover-select,
# the all-sources-bad fallback, field loss, and generic point-fault
# degradation — but no leg drives the pump group's central behavior:
# a proven failure of the duty pump must hand duty to the standby
# pump while the operator surface annunciates it, and the station's
# `none_available`/`all_faulted` roll-ups annunciate when every pump
# is out. The honest lever is the plant protocol's unfenced
# `inject_fault` on the commanded pump's run-feedback channel —
# bad:device_fault quality the motor's `fault_ticks`
# feedback-discrepancy proof treats as the proven failure the group's
# `fault_i` input excludes on — the same lever the reference plant's
# pair-side handover leg drives.
#
# With the pair settled and the station holding a duty pump
# commanded, the leg faults the duty holder's run channel: the
# proven fault asserts, the group reassigns duty inside the declared
# bound, the staged sibling's command asserts and its run feedback
# follows, the failed pump stays excluded with its `fault` reported
# while the five-leg availability aggregate honestly keeps the
# permissives it wires, the managed per-pump fault alarm annunciates
# `alarm`/`unacknowledged` with journaled `point_changed` evidence,
# and the level loop recovers on the new duty pump — the net flow
# going negative against the standing inflow, never a silent station
# stop. A second injection on the remaining pump's channel stands the
# station-level `none_available`/`all_faulted` annunciation while the
# level excursion stays bounded. Clearing each channel restores the
# declared post-repair state — fault flags clearing once command and
# feedback agree, the all-out annunciation returning, the duty
# designation reassigning, the unacknowledged latches holding for the
# receipted acks that close the lifecycle — and no peer reports a
# role change throughout. Named diagnostics: handover-failed for a
# broken reassignment, exclusion, annunciation, or restore clause;
# handover-nondeterministic when the served surfaces cannot drive or
# record the leg deterministically.

HANDOVER_DEADLINE = 40      # bound on each leg's served transition —
                            # the fault proof spans the declared
                            # fault_ticks plus the carrier hops
HANDOVER_CYCLE_DEADLINE = 60  # bound on the sibling's demand cycle
HANDOVER_POLL = 0.05        # transition-watch cadence — under the scan
HANDOVER_ACTOR = 'qa-lane'


def scenario_duty_handover(ctx):
    """Proven duty-pump failure hands duty to the standby pump inside
    the declared bound with the failed pump excluded and its managed
    alarm annunciated; the both-pumps-faulted leg stands
    none_available/all_faulted; the restores land the declared
    post-repair state with the latches held for the receipted acks."""
    case = Case('duty-handover',
                'Duty-pump failure handover and the all-out '
                'annunciation',
                'with the deployed pair settled and tracking and the '
                'station holding a duty pump commanded, an injected '
                'bad:device_fault quality fault on the duty pump\'s '
                'run-feedback channel proves the motor\'s '
                'feedback-discrepancy fault: the pump is excluded, '
                'duty hands to the sibling whose command and run '
                'feedback assert inside the declared bound, the '
                'managed p-fault alarm annunciates alarm/'
                'unacknowledged with journaled point_changed '
                'evidence, and the level loop recovers on the new '
                'duty pump with the excursion bounded; a second '
                'injection on the remaining pump stands '
                'none_available/all_faulted with their managed '
                'alarms; clearing each channel restores the declared '
                'post-repair state — fault flags clearing, the '
                'annunciation returning, the duty designation '
                'reassigning, the latches held for the receipted '
                'acks — and the pair\'s roles never move')
    injected = []      # run-channel points faulted through plant_ctl
    restore_acks = []  # (base, point) while each ack write stands
    try:
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        base = ctx[active]
        tracking = wait_for(lambda: _tracking_peer(ctx, active),
                            time.monotonic() + HANDOVER_DEADLINE,
                            interval=POLL_INTERVAL)
        if tracking is None:
            return case.finish('inconclusive',
                               'no tracking peer — the deployed pair '
                               'never settled')
        case.observe('settled pair: ' + active + ' active, '
                     + tracking + ' tracking')

        _, signals = http_json('GET', base + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'duty-handover-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming the '
                      'duty-handover leg\'s wiring')
        flags = ('ack', 'alarm', 'unacknowledged')
        names = {'level-selected': 'level', 'net-flow': 'net_flow',
                 'demand-in': 'demand_in', 'duty': 'duty',
                 'staged': 'staged',
                 'none-available': 'none_available',
                 'all-faulted': 'all_faulted',
                 'none-available-alarm': 'none_alarm',
                 'none-available-unacknowledged': 'none_unack',
                 'all-faulted-alarm': 'all_alarm',
                 'all-faulted-unacknowledged': 'all_unack',
                 'none-available-ack': 'none_ack',
                 'all-faulted-ack': 'all_ack'}
        for index, tag in ((1, 'p101'), (2, 'p102')):
            for suffix, key in (
                    ('run', 'run%d' % index),
                    ('cmd', 'cmd%d' % index),
                    ('fault', 'fault%d' % index),
                    ('avail', 'avail%d' % index)):
                names[tag + '-' + suffix] = key
            for flag in flags:
                names['%s-fault-%s' % (tag, flag)] = \
                    'fault_%d_%s' % (index, flag)
        entries = {entry.get('name'): entry
                   for entry in signals.get('points', [])
                   if entry.get('name') in names}
        missing = sorted(set(names) - set(entries))
        if missing:
            return case.finish('inconclusive', 'the deployed model '
                               'lacks the duty-handover leg\'s '
                               'wiring — no signals '
                               + ', '.join(missing))
        points = {names[name]: entry.get('point')
                  for name, entry in entries.items()}
        for key in ('fault_1_ack', 'fault_2_ack', 'none_ack',
                    'all_ack'):
            entry = next(entry for entry in entries.values()
                         if entry.get('point') == points[key])
            if not (entry.get('writable')
                    and entry.get('direction') == 'in'
                    and entry.get('value_type') == 'bool'):
                return case.finish(
                    'inconclusive', 'the ' + key + ' point is not '
                    'the alarm\'s writable bool ack input: '
                    + json.dumps(entry)[:300])
        case.observe('handover path: '
                     + json.dumps({name: entry.get('point')
                                   for name, entry in
                                   sorted(entries.items())},
                                  sort_keys=True))

        snap0 = _snapshot(ctx, base)
        chain_name, _chain_ports = _descriptor_ports(
            snap0, 'threshold-chain', [])
        if chain_name is None:
            return case.finish('inconclusive', 'the served snapshot '
                               'carries no bound threshold-chain '
                               'descriptor')
        high = _parameter_value(snap0, chain_name, 'high')
        if not isinstance(high, (int, float)) or isinstance(high, bool):
            return case.finish('inconclusive', 'the deployed chain '
                               'declares no high bound the excursion '
                               'clause reads: ' + json.dumps(high))
        case.observe('the chain\'s declared excursion bound: high='
                     + str(high))

        if ctx.get('plant_ctl') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no plant_ctl seam for the '
                               'fault injection')

        last = {}
        seen = set()

        def value(key, snap):
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

        def leg(name, cond, keys, failed, deadline=None):
            """One served-state wait plus its evidence file. A miss
            classifies inconclusive when an awaited output never
            reported a sample, handover-failed when the served values
            never landed the leg."""
            hit = wait_for(lambda: poll(cond, keys),
                           time.monotonic()
                           + (deadline or HANDOVER_DEADLINE),
                           interval=HANDOVER_POLL)
            snap = last.get('snap') or {}
            ref_ = save_evidence(
                ctx['evidence_dir'], 'duty-handover-' + name + '.json',
                {'tick': snap.get('tick'),
                 'samples': {key: _point_sample(snap, points[key])
                             for key in sorted(keys)
                             if key in points}})
            case.evidence('file', ref_, 'the served ' + name + ' leg')
            if hit:
                return hit, None
            unreported = sorted(key for key in keys
                                if key in points and key not in seen)
            if unreported:
                return None, case.finish(
                    'inconclusive', 'handover-nondeterministic: the '
                    + name + ' leg\'s outputs never reported on the '
                    'served snapshot: ' + ', '.join(unreported))
            return None, case.finish(
                'failed', failed + '; last served ' + json.dumps(
                    {key: value(key, snap) for key in sorted(keys)
                     if key in points}, sort_keys=True)[:500])

        # Leg 1 — the commanded-duty baseline: the level loop holds a
        # duty pump commanded and proven running — the run contact
        # loopback answering the delivered command.
        def commanded(snap):
            duty = value('duty', snap)
            if duty not in (1, 2):
                return None
            if value('cmd%d' % duty, snap) is not True \
                    or value('run%d' % duty, snap) is not True:
                return None
            for index in (1, 2):
                if value('fault%d' % index, snap) is not False \
                        or value('avail%d' % index, snap) is not True \
                        or value('fault_%d_alarm' % index, snap) \
                        is not False:
                    return None
            for key in ('none_available', 'all_faulted',
                        'none_alarm', 'all_alarm'):
                if value(key, snap) is not False:
                    return None
            return snap

        pump_keys = [prefix + str(index)
                     for index in (1, 2)
                     for prefix in ('cmd', 'run', 'fault', 'avail')] \
            + ['fault_%d_alarm' % index for index in (1, 2)]
        baseline, error = leg(
            'baseline', commanded,
            ['demand_in', 'duty', 'staged', 'level', 'net_flow',
             'none_available', 'all_faulted', 'none_alarm',
             'all_alarm'] + pump_keys,
            'handover-failed: the pair never held the commanded-duty '
            'baseline the leg needs', deadline=HANDOVER_CYCLE_DEADLINE)
        if error:
            return error
        held = value('duty', baseline)
        sibling = 3 - held
        case.observe('commanded baseline: duty=p'
                     + str(100 + held) + ' commanding with run '
                     'feedback, sibling p' + str(100 + sibling))

        # The journal floor ahead of the leg: this case's records are
        # the ones above it.
        _, journal0 = http_json('GET', base + '/journal?since=0')
        floor = max((entry.get('seq') or 0
                     for entry in _journal_list(journal0)
                     if isinstance(entry, dict)), default=0)

        submitted = []

        def write(point, flag):
            command = {'write_value': {'point': point, 'kind': 'bool',
                                       'value': {'bool': flag}}}
            status, receipt = http_json(
                'POST', base + '/command',
                {'command': command, 'actor': HANDOVER_ACTOR})
            submitted.append({'command': command, 'status': status,
                              'receipt': receipt})
            outcome = (receipt or {}).get('outcome') or {}
            return status == 200 and 'rejected' not in outcome

        def inject(point):
            verdict = _plant_ctl(ctx, 'fault', str(point),
                                 'bad:device_fault')
            if verdict.get('result') == 'done':
                injected.append(point)
                return True
            return verdict

        # Leg 2 — the proven duty-pump failure: the injected
        # run-channel fault proves the motor's feedback discrepancy,
        # the group excludes the pump and hands duty to the sibling —
        # whose command and run feedback land inside the declared
        # start bound — the availability aggregate honestly keeps the
        # permissives it wires (the exclusion is the proven fault,
        # not an availability collapse), and the managed fault alarm
        # annunciates.
        verdict = inject(points['run%d' % held])
        if verdict is not True:
            return case.finish('failed', 'handover-failed: '
                               'inject_fault on the duty pump\'s run '
                               'point ' + str(points['run%d' % held])
                               + ' refused: '
                               + json.dumps(verdict)[:300])
        case.observe('bad:device_fault injected on p'
                     + str(100 + held) + ' run point '
                     + str(points['run%d' % held]))

        def handed(snap):
            demand = value('demand_in', snap)
            if not isinstance(demand, int) or isinstance(demand, bool) \
                    or value('staged', snap) != min(demand, 1):
                return None
            if value('fault%d' % held, snap) is not True \
                    or value('avail%d' % held, snap) is not True \
                    or value('duty', snap) != sibling \
                    or value('cmd%d' % held, snap) is not False \
                    or value('cmd%d' % sibling, snap) is not True \
                    or value('run%d' % sibling, snap) is not True \
                    or value('none_available', snap) is not False \
                    or value('all_faulted', snap) is not False \
                    or value('fault_%d_alarm' % held, snap) is not True \
                    or value('fault_%d_unacknowledged' % held, snap) \
                    is not True:
                return None
            return snap

        hit, error = leg(
            'handed', handed,
            ['duty', 'staged', 'demand_in',
             'fault%d' % held, 'avail%d' % held,
             'cmd%d' % held, 'cmd%d' % sibling, 'run%d' % sibling,
             'none_available', 'all_faulted',
             'fault_%d_alarm' % held,
             'fault_%d_unacknowledged' % held],
            'handover-failed: the proven duty-pump failure never '
            'handed duty — the exclusion, the sibling\'s staged '
            'command, or the managed annunciation missing')
        if error:
            return error
        inject_tick = hit.get('tick')
        case.observe('handover: fault proven, duty=p'
                     + str(100 + sibling) + ' commanding at tick '
                     + str(inject_tick)
                     + ' — the failed pump excluded with the managed '
                     'alarm standing')
        if _settled_active(ctx) != active:
            return case.finish('failed', 'handover-failed: the '
                               'active role moved under a field '
                               'fault — a plant-side injection is '
                               'not peer loss')

        # Leg 3 — the level loop recovers on the new duty pump: the
        # staged sibling drains the well — the net flow reading
        # negative against the standing inflow — never a silent
        # station stop, and the excursion stays bounded.
        def draining(snap):
            flow = value('net_flow', snap)
            if not isinstance(flow, (int, float)) \
                    or isinstance(flow, bool) or flow >= 0:
                return None
            if value('cmd%d' % sibling, snap) is not True \
                    or value('run%d' % sibling, snap) is not True:
                return None
            level = value('level', snap)
            if not isinstance(level, (int, float)) \
                    or isinstance(level, bool):
                return None
            return snap

        hit, error = leg(
            'recovering', draining,
            ['net_flow', 'level', 'cmd%d' % sibling,
             'run%d' % sibling, 'demand_in', 'duty'],
            'handover-failed: the level loop never recovered on the '
            'new duty pump — the staged sibling never drained the '
            'well')
        if error:
            return error
        level_at_handover = value('level', hit)
        case.observe('recovery: the sibling drains — net-flow '
                     + str(value('net_flow', hit))
                     + ' against the standing inflow at tick '
                     + str(hit.get('tick')))

        # Leg 4 — the all-out failure: the remaining pump's channel
        # faults too — every pump out, the station roll-ups and their
        # managed alarms annunciate, and the level excursion stays
        # bounded inside the leg's window.
        verdict = inject(points['run%d' % sibling])
        if verdict is not True:
            return case.finish('failed', 'handover-failed: '
                               'inject_fault on the sibling\'s run '
                               'point ' + str(points['run%d' % sibling])
                               + ' refused: '
                               + json.dumps(verdict)[:300])
        case.observe('bad:device_fault injected on p'
                     + str(100 + sibling) + ' run point '
                     + str(points['run%d' % sibling])
                     + ' — every pump out')

        def allout(snap):
            for index in (1, 2):
                if value('fault%d' % index, snap) is not True \
                        or value('cmd%d' % index, snap) is not False:
                    return None
            if value('duty', snap) != 0 \
                    or value('staged', snap) != 0 \
                    or value('none_available', snap) is not True \
                    or value('all_faulted', snap) is not True \
                    or value('none_alarm', snap) is not True \
                    or value('none_unack', snap) is not True \
                    or value('all_alarm', snap) is not True \
                    or value('all_unack', snap) is not True:
                return None
            return snap

        hit, error = leg(
            'allout', allout,
            ['duty', 'staged', 'none_available', 'all_faulted',
             'none_alarm', 'none_unack', 'all_alarm', 'all_unack',
             'fault1', 'fault2', 'cmd1', 'cmd2', 'level'],
            'handover-failed: the all-out state never annunciated — '
            'none_available/all_faulted or their managed alarms '
            'missing')
        if error:
            return error
        case.observe('all-out: every pump faulted at tick '
                     + str(hit.get('tick'))
                     + ' — none_available/all_faulted standing with '
                     'their managed latches')

        # The bounded excursion: the sibling keeps draining through
        # its own two-scan fault proof, so the level can only have
        # climbed on the declared inflow in the annunciation hops
        # since — it must sit under the chain's declared high bound
        # at the all-out mark, never an unbounded excursion.
        level_at_allout = value('level', hit)
        if not isinstance(level_at_allout, (int, float)) \
                or isinstance(level_at_allout, bool) \
                or level_at_allout >= high:
            return case.finish(
                'failed', 'handover-failed: the level excursion '
                'through the all-out window was unbounded — level '
                + str(level_at_allout) + ' against the declared high '
                'bound ' + str(high))
        case.observe('bounded excursion: level '
                     + str(level_at_allout) + ' under high='
                     + str(high) + ' at the all-out mark')

        # Leg 5 — the declared post-repair recovery: clearing the
        # first faulted channel — the failed pump's command stays
        # released, so command and feedback agree and the proven
        # fault clears — one pump back drops the all-out conditions,
        # the duty designation reassigns to the recovered pump under
        # the declared rotation, the alarm returns, and the
        # unacknowledged latches hold for the acks.
        verdict = _plant_ctl(ctx, 'clear-fault',
                             str(points['run%d' % held]))
        if verdict.get('result') != 'done':
            return case.finish('failed', 'handover-failed: '
                               'clear_fault on the duty pump\'s run '
                               'channel refused: '
                               + json.dumps(verdict)[:300])
        injected.remove(points['run%d' % held])

        def repaired(snap):
            if value('fault%d' % held, snap) is not False \
                    or value('avail%d' % held, snap) is not True \
                    or value('duty', snap) != held \
                    or value('none_available', snap) is not False \
                    or value('all_faulted', snap) is not False \
                    or value('fault_%d_alarm' % held, snap) is not False \
                    or value('fault_%d_unacknowledged' % held, snap) \
                    is not True \
                    or value('none_alarm', snap) is not False \
                    or value('all_alarm', snap) is not False:
                return None
            return snap

        hit, error = leg(
            'repaired', repaired,
            ['duty', 'staged', 'fault%d' % held, 'avail%d' % held,
             'none_available', 'all_faulted',
             'fault_%d_alarm' % held,
             'fault_%d_unacknowledged' % held,
             'none_alarm', 'none_unack', 'all_alarm', 'all_unack'],
            'handover-failed: the declared post-repair state never '
            'landed — the fault flag, the all-out return, the duty '
            'reassignment, or the held latches missing')
        if error:
            return error
        case.observe('post-repair: p' + str(100 + held)
                     + ' recovered and re-designated duty at tick '
                     + str(hit.get('tick'))
                     + ' — the latches holding for the acks')

        # The remaining channel clears; its fault flag and alarm
        # return while the latch stands.
        verdict = _plant_ctl(ctx, 'clear-fault',
                             str(points['run%d' % sibling]))
        if verdict.get('result') != 'done':
            return case.finish('failed', 'handover-failed: '
                               'clear_fault on the sibling\'s run '
                               'channel refused: '
                               + json.dumps(verdict)[:300])
        injected.remove(points['run%d' % sibling])

        hit, error = leg(
            'cleared',
            lambda s: value('fault%d' % sibling, s) is False
            and value('avail%d' % sibling, s) is True
            and value('fault_%d_alarm' % sibling, s) is False
            and value('fault_%d_unacknowledged' % sibling, s)
            is True,
            ['fault%d' % sibling, 'avail%d' % sibling,
             'fault_%d_alarm' % sibling,
             'fault_%d_unacknowledged' % sibling],
            'handover-failed: the sibling\'s recovery never landed — '
            'its fault flag and alarm must return while the latch '
            'stands')
        if error:
            return error
        case.observe('sibling recovered at tick '
                     + str(hit.get('tick')))

        # The latches' lifecycle close: one receipted ack per managed
        # alarm that annunciated — the two pump fault alarms and the
        # two station roll-ups — each clearing its unacknowledged
        # latch, each ack input then restored for the next trip.
        ack_pairs = [
            ('fault_%d_ack' % held, 'fault_%d_unacknowledged' % held),
            ('fault_%d_ack' % sibling,
             'fault_%d_unacknowledged' % sibling),
            ('none_ack', 'none_unack'), ('all_ack', 'all_unack')]
        for ack_key, unack_key in ack_pairs:
            if not write(points[ack_key], True):
                return case.finish('failed', 'handover-failed: the '
                                   + ack_key + ' ack write was '
                                   'refused: '
                                   + json.dumps(submitted[-1])[:300])
            restore_acks.append((base, points[ack_key]))
            hit, error = leg(
                'ack-' + unack_key,
                lambda s, key=unack_key: value(key, s) is False,
                [ack_key, unack_key],
                'handover-failed: the receipted ' + ack_key
                + ' never cleared the ' + unack_key + ' latch')
            if error:
                return error
            if not write(points[ack_key], False):
                return case.finish('failed', 'handover-failed: the '
                                   + ack_key + ' restore write was '
                                   'refused: '
                                   + json.dumps(submitted[-1])[:300])
            restore_acks.pop()
        case.observe('all four latches acknowledged and re-armed')

        if _settled_active(ctx) != active \
                or _tracking_peer(ctx, active) != tracking:
            return case.finish('failed', 'handover-failed: the '
                               'pair\'s roles moved during the leg')

        # The durable record: each declared-journaled point records
        # the leg's transitions in order — the two fault flags' prove
        # and clear, the station roll-ups, the managed alarm and
        # latch flags — and every submission settles applied through
        # the receipted path attributed to the lane actor.
        _, journal = http_json('GET', base + '/journal?since='
                               + str(floor))
        settled = _settled_receipts(journal)
        missing = []
        for item in submitted:
            receipt = next(
                (entry for entry in settled
                 if entry.get('command') == item['command']), None)
            if receipt is None:
                missing.append('no settled receipt journaled for '
                               + json.dumps(item['command'])[:200])
            elif receipt.get('actor') != HANDOVER_ACTOR:
                missing.append('a settled receipt lost its actor: '
                               + json.dumps(receipt)[:200])
            elif 'applied' not in (receipt.get('outcome') or {}):
                missing.append('a settled receipt did not apply: '
                               + json.dumps(receipt)[:200])
        changes = _journal_point_changes(journal)
        bool_t, bool_f = {'bool': True}, {'bool': False}
        ordered = {
            'fault%d' % held: [bool_t, bool_f],
            'fault%d' % sibling: [bool_t, bool_f],
            'none_available': [bool_t, bool_f],
            'all_faulted': [bool_t, bool_f],
            'fault_%d_alarm' % held: [bool_t, bool_f],
            'fault_%d_unacknowledged' % held: [bool_t, bool_f],
            'fault_%d_alarm' % sibling: [bool_t, bool_f],
            'fault_%d_unacknowledged' % sibling: [bool_t, bool_f],
            'none_alarm': [bool_t, bool_f],
            'none_unack': [bool_t, bool_f],
            'all_alarm': [bool_t, bool_f],
            'all_unack': [bool_t, bool_f]}

        def _subsequence(wanted, got):
            it = iter(got)
            return all(any(item == want for item in it)
                       for want in wanted)

        for key, wanted in ordered.items():
            if not _subsequence(wanted, changes.get(points[key], [])):
                missing.append('point ' + str(points[key]) + ' never '
                               'journaled the ordered ' + key
                               + ' transitions: '
                               + json.dumps(changes.get(points[key],
                                                        []))[:200])
        for entry in _journal_list(journal):
            if 'role_changed' in (entry.get('event') or {}):
                missing.append('a role_changed event journaled '
                               'under a field fault')
        ref = save_evidence(
            ctx['evidence_dir'], 'duty-handover-journal.json',
            {'floor': floor, 'receipts': len(settled),
             'missing': missing,
             'transitions': {key: changes.get(points[key], [])
                             for key in sorted(ordered)}})
        case.evidence('file', ref, 'the journaled transitions and '
                      'settled receipts above the floor')
        if missing:
            return case.finish('failed', 'handover-failed: journaled '
                               'evidence missing: '
                               + '; '.join(missing))
        case.observe('journaled: both fault proofs and clears, the '
                     'all-out roll-ups, the managed flags, and every '
                     'settled receipt')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
    finally:
        # The injected channel faults and the standing ack inputs are
        # the run's shared state: a case that leaves either standing
        # poisons every later leg. The pair's roles never moved —
        # there is nothing to fail back.
        for point in list(injected):
            try:
                _try_plant_ctl(ctx, 'clear-fault', str(point))
            except Exception:
                pass
        live_base = None
        try:
            settled = _settled_active(ctx)
            if settled is not None:
                live_base = ctx[settled]
        except Exception:
            pass
        if live_base is not None:
            for _base, rpoint in restore_acks:
                try:
                    http_json('POST', live_base + '/command',
                              {'command': {'write_value': {
                                  'point': rpoint, 'kind': 'bool',
                                  'value': {'bool': False}}},
                               'actor': HANDOVER_ACTOR})
                except Exception:
                    pass
