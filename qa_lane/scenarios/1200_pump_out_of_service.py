"""The pump_out_of_service acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *


# --------------------------------------------------------------------
# Per-pump out-of-service duty exclusion and the managed-alarm surface
# (WW-OPS-001's maintenance-inhibit clause, WW-ALM-002's declared
# managed precedence): each pump's writable `oos` point is journaled
# and receipted, and the model wires its inversion into the pump's
# in-service availability leg (`oos-ok` -> `oos-ok-avail-in`), the
# demand guard (`oos-ok-guard-in`), and the managed per-pump alarms'
# declared `oos`/`suppress` inputs — so one receipted write both
# excludes the machine from duty and manages its alarm surface. The
# case holds the duty holder out while the pair sits settled-idle, so
# the handover's only cause is the exclusion, proves the sibling
# serves the next demand while the held pump's command stays released,
# drives a run-contact fault in mid-hold to prove `alarm` still
# reports process truth while suppression withholds the annunciation,
# then releases the hold under the standing fault — the declared
# re-annunciation on suppression's release — before the fault clears,
# the receipted ack settles the latch, and the pump rejoins
# availability and the rotation. Every managed transition lands in the
# durable journal as ordered `point_changed` entries beside the
# attributed receipts, and /history carries the tick-domain ordering
# the declared wiring depth bounds. Named diagnostics: oos-failed for
# a broken exclusion, managed-state, or return clause;
# oos-nondeterministic when the served surfaces cannot drive or record
# a deterministic leg.

OOS_DEADLINE = 30         # bound on settle, exclusion, and audit waits
OOS_CYCLE_DEADLINE = 20   # bound on each served demand-cycle window
OOS_POLL = 0.5            # observation cadence
OOS_ACTOR = 'qa-lane'
OOS_BOUND_TICKS = 8       # the oos -> carrier -> avail -> group depth


def _managed_alarm_bindings(snapshot):
    """{alarm point: (instance, {port: bound point})} — every served
    managed-alarm descriptor keyed by the point its `alarm` output
    binds, so the leg reads each instance's declared `oos`/`suppress`/
    `shelve` inputs rather than assuming the wiring."""
    found = {}
    for entry in (snapshot or {}).get('descriptors') or []:
        if entry.get('kind') not in ('managed-bool-latching-alarm',
                                     'managed-latching-alarm'):
            continue
        ports = {port.get('name'): port.get('point')
                 for port in entry.get('ports') or []}
        if ports.get('alarm') is not None:
            found[ports['alarm']] = (entry.get('name'), ports)
    return found


def _subsequence(wanted, got):
    """Whether `wanted`'s values land in `got` in order — the journaled
    per-point transition ordering check."""
    it = iter(got)
    return all(any(item == want for item in it) for want in wanted)


def scenario_pump_out_of_service(ctx):
    """A receipted write on the duty pump's `oos` point excludes it
    from availability and hands duty to the sibling inside the
    declared bounds, manages the pump's alarms per their declared
    `oos`/`suppress` bindings — `alarm` still reporting process truth
    mid-OOS — and the false write returns it to availability and the
    rotation."""
    case = Case('pump-out-of-service',
                'Per-pump out-of-service duty exclusion and managed '
                'alarms',
                'with the deployed pair settled and tracking at an '
                'idle assigned-duty baseline, an attributed receipted '
                'write on the duty pump\'s oos point drops its '
                'in-service leg (the oos-ok cone and avail), hands '
                'duty to the sibling inside the declared wiring bound '
                'with staged reporting the available count, releases '
                'the held pump\'s command for the whole of the '
                'sibling\'s service, and drives the pump\'s managed '
                'alarms into the states their declared oos/suppress '
                'bindings select — the fault alarm out_of_service and '
                'suppressed, the unbound alarms untouched — while a '
                'mid-OOS run fault still asserts alarm as process '
                'truth without the suppressed annunciation; every '
                'managed transition journals as ordered point_changed '
                'entries beside the attributed receipts, the false '
                'write returns the pump to availability and '
                're-annunciates the standing fault on suppression\'s '
                'release, the receipted ack settles the latch, and '
                'the pump rejoins the duty rotation with the pair\'s '
                'roles unchanged')
    held_oos = None      # the held pump's oos point while it stands
    injected = None      # the held pump's run point while it faults
    restore_ack = None   # (base, point) while the ack write stands
    try:
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + OOS_DEADLINE)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        base = ctx[active]
        tracking = wait_for(lambda: _tracking_peer(ctx, active),
                            time.monotonic() + OOS_DEADLINE,
                            interval=POLL_INTERVAL)
        if tracking is None:
            return case.finish('inconclusive',
                               'no tracking peer — the deployed pair '
                               'never settled')
        case.observe('settled pair: ' + active + ' active, ' + tracking
                     + ' tracking')

        _, signals = http_json('GET', base + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'pump-oos-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming the '
                      'out-of-service leg\'s wiring')
        flags = ('ack', 'alarm', 'unacknowledged', 'shelved',
                 'suppressed', 'out-of-service')
        names = {'inflow': 'inflow', 'level-selected': 'level',
                 'demand-in': 'demand_in', 'duty': 'duty',
                 'staged': 'staged', 'none-available': 'none_available'}
        for index, tag in ((1, 'p101'), (2, 'p102')):
            for suffix, key in (
                    ('oos', 'oos%d' % index),
                    ('oos-ok', 'oos_ok%d' % index),
                    ('oos-ok-avail-in', 'oos_ok_avail%d' % index),
                    ('oos-ok-guard-in', 'oos_ok_guard%d' % index),
                    ('avail', 'avail%d' % index),
                    ('avail-in', 'avail_in%d' % index),
                    ('cmd', 'cmd%d' % index),
                    ('run', 'run%d' % index),
                    ('fault', 'fault%d' % index),
                    ('fault-alarm-in', 'fault_in%d' % index),
                    ('fault-sup', 'fault_sup%d' % index),
                    ('fault-sup-in', 'fault_sup_in%d' % index)):
                names[tag + '-' + suffix] = key
            for kind in ('fault', 'thermal', 'moisture'):
                for flag in flags:
                    names['%s-%s-%s' % (tag, kind, flag)] = \
                        '%s_%d_%s' % (kind, index,
                                      flag.replace('-', '_'))
        entries = {entry.get('name'): entry
                   for entry in signals.get('points', [])
                   if entry.get('name') in names}
        missing = sorted(set(names) - set(entries))
        if missing:
            return case.finish('inconclusive', 'the deployed model '
                               'lacks the out-of-service leg\'s '
                               'wiring — no signals '
                               + ', '.join(missing))
        for name in ('p101-oos', 'p102-oos'):
            entry = entries[name]
            if not (entry.get('writable')
                    and entry.get('direction') == 'in'
                    and entry.get('value_type') == 'bool'):
                return case.finish('inconclusive', 'signal ' + name
                                   + ' is not the writable bool '
                                   'maintenance-inhibit input the leg '
                                   'needs: ' + json.dumps(entry)[:300])
        points = {names[name]: entry.get('point')
                  for name, entry in entries.items()}
        case.observe('out-of-service path: '
                     + json.dumps({name: entry.get('point')
                                   for name, entry in
                                   sorted(entries.items())},
                                  sort_keys=True))

        snap0 = _snapshot(ctx, base)
        group_name, _group_ports = _descriptor_ports(
            snap0, 'pump-group', [])
        if group_name is None:
            return case.finish('inconclusive', 'the served snapshot '
                               'carries no bound pump-group '
                               'descriptor')
        rotation = _parameter_value(snap0, group_name, 'rotation')
        if rotation != 0:
            return case.finish('inconclusive', 'the deployed group '
                               'does not declare the '
                               'alternate-each-cycle rotation the '
                               'return leg\'s eligibility proof '
                               'needs: ' + json.dumps(rotation))
        bindings = _managed_alarm_bindings(snap0)
        managed = {}
        for index in (1, 2):
            for kind in ('fault', 'thermal', 'moisture'):
                key = '%s_%d_alarm' % (kind, index)
                found = bindings.get(points[key])
                if found is None:
                    return case.finish(
                        'inconclusive', 'the served descriptors bind '
                        'no managed alarm to the '
                        + key.replace('_', '-') + ' point '
                        + str(points[key]))
                managed[(index, kind)] = found[1]
        ref = save_evidence(
            ctx['evidence_dir'], 'pump-oos-bindings.json',
            {'group': group_name, 'rotation': rotation,
             'managed': {'p%d-%s' % (index + 100, kind): {
                 port: managed[(index, kind)].get(port)
                 for port in ('in', 'ack', 'shelve', 'oos', 'suppress')}
                 for index in (1, 2)
                 for kind in ('fault', 'thermal', 'moisture')}})
        case.evidence('file', ref, 'each managed alarm\'s declared '
                      'lifecycle bindings')

        last = {}
        seen = set()
        breaches = []       # opportunistic OOS-window violations
        held = [None]       # the held pump index, once duty names it
        oos_window = [False]

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
            if oos_window[0] and held[0] is not None \
                    and value('avail%d' % held[0], snap) is False \
                    and value('cmd%d' % held[0], snap) is True:
                breaches.append('the held pump\'s command re-asserted '
                                'at tick ' + str(snap.get('tick')))
            return snap if cond(snap) else None

        def leg(name, cond, keys, failed, deadline=None):
            """One served-state wait plus its evidence file. A miss
            classifies inconclusive when an awaited output never
            reported a sample, oos-failed when the served values never
            landed the leg."""
            hit = wait_for(lambda: poll(cond, keys),
                           time.monotonic()
                           + (deadline or OOS_DEADLINE),
                           interval=OOS_POLL)
            snap = last.get('snap') or {}
            ref_ = save_evidence(
                ctx['evidence_dir'], 'pump-oos-' + name + '.json',
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
                    'inconclusive', 'oos-nondeterministic: the ' + name
                    + ' leg\'s outputs never reported on the served '
                    'snapshot: ' + ', '.join(unreported))
            return None, case.finish(
                'failed', failed + '; last served ' + json.dumps(
                    {key: value(key, snap) for key in sorted(keys)
                     if key in points}, sort_keys=True)[:500])

        def managed_want(index, kind, snap):
            """The managed flags a snapshot should serve for one alarm:
            each bound lifecycle input's delivered level, `False` for
            an input the kind never declared — the declared precedence
            read off the descriptor rather than assumed."""
            ports = managed[(index, kind)]

            def bound(port):
                point = ports.get(port)
                return point is not None \
                    and _point_value(snap, point) is True
            return {'shelved': bound('shelve'),
                    'suppressed': bound('suppress'),
                    'out_of_service': bound('oos')}

        def managed_match(snap):
            for index in (1, 2):
                for kind in ('fault', 'thermal', 'moisture'):
                    for flag, want in managed_want(index, kind,
                                                   snap).items():
                        if value('%s_%d_%s' % (kind, index, flag),
                                 snap) != want:
                            return None
            return snap

        flag_keys = ['%s_%d_%s' % (kind, index, flag)
                     for index in (1, 2)
                     for kind in ('fault', 'thermal', 'moisture')
                     for flag in ('alarm', 'unacknowledged', 'shelved',
                                  'suppressed', 'out_of_service')]
        pump_keys = [prefix + str(index)
                     for index in (1, 2)
                     for prefix in ('oos', 'oos_ok', 'oos_ok_avail',
                                    'oos_ok_guard', 'avail', 'avail_in',
                                    'cmd', 'run', 'fault', 'fault_in',
                                    'fault_sup', 'fault_sup_in')]

        def settled_idle(snap):
            if value('demand_in', snap) != 0 \
                    or value('staged', snap) != 0 \
                    or value('none_available', snap) is not False:
                return None
            if value('duty', snap) not in (1, 2):
                return None
            for index in (1, 2):
                if value('avail%d' % index, snap) is not True \
                        or value('oos%d' % index, snap) is not False \
                        or value('cmd%d' % index, snap) is not False \
                        or value('fault%d' % index, snap) is not False:
                    return None
            for key in flag_keys:
                if value(key, snap) is not False:
                    return None
            return snap

        baseline, error = leg(
            'baseline', settled_idle,
            ['demand_in', 'staged', 'none_available', 'duty', 'level',
             'inflow'] + pump_keys + flag_keys,
            'oos-failed: the pair never settled to the idle '
            'assigned-duty baseline')
        if error:
            return error
        held[0] = value('duty', baseline)
        sibling = 3 - held[0]
        case.observe('idle baseline: duty=p' + str(100 + held[0])
                     + ' sibling p' + str(100 + sibling)
                     + ' — the oos write targets the duty holder')

        # The journal floor ahead of the leg: this case's records are
        # the ones above it. The baseline tick anchors the /history
        # transition scan.
        _, journal0 = http_json('GET', base + '/journal?since=0')
        floor = max((entry.get('seq') or 0
                     for entry in _journal_list(journal0)
                     if isinstance(entry, dict)), default=0)
        anchor_tick = baseline.get('tick') or 0

        submitted = []

        def write(point, flag):
            command = {'write_value': {'point': point, 'kind': 'bool',
                                       'value': {'bool': flag}}}
            status, receipt = http_json(
                'POST', base + '/command',
                {'command': command, 'actor': OOS_ACTOR})
            submitted.append({'command': command, 'status': status,
                              'receipt': receipt})
            outcome = (receipt or {}).get('outcome') or {}
            return status == 200 and 'rejected' not in outcome

        # Leg 1 — the receipted hold: the oos write lands, the
        # in-service cone falls through the availability leg and the
        # demand guard, the avail carrier drops, and duty hands to the
        # sibling — the only cause on an idle pair is the exclusion.
        if not write(points['oos%d' % held[0]], True):
            return case.finish('failed', 'oos-failed: the oos write '
                               'on point ' + str(points['oos%d'
                                                     % held[0]])
                               + ' was refused: '
                               + json.dumps(submitted[-1])[:300])
        held_oos = points['oos%d' % held[0]]
        oos_window[0] = True
        ref = save_evidence(ctx['evidence_dir'],
                            'pump-oos-hold-receipt.json',
                            submitted[-1])
        case.evidence('file', ref, 'the attributed oos write receipt')
        case.observe('oos held on p' + str(100 + held[0]) + ' point '
                     + str(held_oos))

        def excluded(snap):
            if value('oos%d' % held[0], snap) is not True:
                return None
            for key in ('oos_ok%d' % held[0], 'oos_ok_avail%d'
                        % held[0], 'oos_ok_guard%d' % held[0],
                        'avail%d' % held[0], 'avail_in%d' % held[0]):
                if value(key, snap) is not False:
                    return None
            if value('avail%d' % sibling, snap) is not True \
                    or value('duty', snap) != sibling \
                    or value('cmd%d' % held[0], snap) is not False \
                    or value('none_available', snap) is not False:
                return None
            return snap

        hit, error = leg(
            'excluded', excluded,
            ['oos%d' % held[0], 'oos_ok%d' % held[0],
             'oos_ok_avail%d' % held[0], 'oos_ok_guard%d' % held[0],
             'avail%d' % held[0], 'avail_in%d' % held[0],
             'avail%d' % sibling, 'duty', 'staged',
             'cmd%d' % held[0], 'none_available'],
            'oos-failed: the held pump\'s exclusion never landed — '
            'the in-service cone, the avail drop, or the duty '
            'handover missing')
        if error:
            return error
        case.observe('excluded: avail p' + str(100 + held[0])
                     + ' dropped, duty=p' + str(100 + sibling)
                     + ' at tick ' + str(hit.get('tick')))

        # Leg 2 — the managed surface: each per-pump alarm reports the
        # managed states its declared bindings select — the held
        # pump's fault alarm out_of_service and suppressed through the
        # delivered copy, the unbound alarms and the sibling's whole
        # set untouched.
        hit, error = leg(
            'managed', managed_match, flag_keys + [
                'oos%d' % index for index in (1, 2)] + [
                'fault_sup_in%d' % index for index in (1, 2)],
            'oos-failed: the managed alarms never reported the states '
            'their declared bindings select')
        if error:
            return error
        case.observe('managed states: ' + json.dumps(
            {'p%d-%s' % (index + 100, kind): managed_want(
                index, kind, hit)
             for index in (1, 2)
             for kind in ('fault', 'thermal', 'moisture')},
            sort_keys=True))

        # Leg 3 — the sibling's service: the next demand stages only
        # the sibling while the held pump's command stays released.
        def served(snap):
            demand = value('demand_in', snap)
            if not isinstance(demand, int) or isinstance(demand, bool) \
                    or demand < 1:
                return None
            if value('duty', snap) != sibling \
                    or value('staged', snap) != min(demand, 1) \
                    or value('cmd%d' % sibling, snap) is not True \
                    or value('cmd%d' % held[0], snap) is not False \
                    or value('avail%d' % held[0], snap) is not False:
                return None
            return snap

        hit, error = leg(
            'served', served,
            ['demand_in', 'staged', 'duty', 'cmd%d' % held[0],
             'cmd%d' % sibling, 'avail%d' % held[0]],
            'oos-failed: the sibling never served the demand with '
            'the held pump excluded', deadline=OOS_CYCLE_DEADLINE)
        if error:
            return error
        case.observe('the sibling serves: duty=p' + str(100 + sibling)
                     + ' staged ' + str(value('staged', hit))
                     + ' while the held pump\'s command stays '
                     'released')

        def completed(snap):
            return value('demand_in', snap) == 0 \
                and value('staged', snap) == 0 \
                and value('duty', snap) == sibling \
                and value('cmd%d' % held[0], snap) is False

        hit, error = leg(
            'completed', completed,
            ['demand_in', 'staged', 'duty', 'cmd%d' % held[0],
             'cmd%d' % sibling],
            'oos-failed: the sibling\'s demand cycle never completed '
            'with the held pump still excluded',
            deadline=OOS_CYCLE_DEADLINE)
        if error:
            return error
        case.observe('the sibling\'s cycle completed at tick '
                     + str(hit.get('tick'))
                     + ' — duty never moved back to the held pump')

        # Leg 4 — mid-OOS process truth: an injected run-contact fault
        # proves while the suppression stands — `alarm` reports the
        # truth, `unacknowledged` stays withheld, the named alarm
        # counts without annunciating.
        if ctx.get('plant_ctl') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no plant_ctl seam for the '
                               'fault leg')
        verdict = _plant_ctl(ctx, 'fault', str(points['run%d'
                                                     % held[0]]),
                             'bad:device_fault')
        if verdict.get('result') != 'done':
            return case.finish('failed', 'oos-failed: inject_fault '
                               'on the held pump\'s run point '
                               + str(points['run%d' % held[0]])
                               + ' refused: '
                               + json.dumps(verdict)[:300])
        injected = points['run%d' % held[0]]
        case.observe('bad:device_fault injected on p'
                     + str(100 + held[0]) + ' run point '
                     + str(injected) + ' mid-OOS')

        def truth(snap):
            if value('fault%d' % held[0], snap) is not True:
                return None
            for flag, want in (('alarm', True), ('unacknowledged',
                                                 False),
                               ('suppressed', True),
                               ('out_of_service', True)):
                if value('fault_%d_%s' % (held[0], flag), snap) \
                        is not want:
                    return None
            if value('cmd%d' % held[0], snap) is not False \
                    or value('duty', snap) != sibling:
                return None
            return snap

        hit, error = leg(
            'truth', truth,
            ['fault%d' % held[0], 'fault_in%d' % held[0],
             'fault_%d_alarm' % held[0],
             'fault_%d_unacknowledged' % held[0],
             'fault_%d_suppressed' % held[0],
             'fault_%d_out_of_service' % held[0],
             'cmd%d' % held[0], 'duty'],
            'oos-failed: the mid-OOS fault never landed the named-'
            'without-annunciating contract — alarm must report the '
            'process truth while suppression withholds the latch')
        if error:
            return error
        case.observe('mid-OOS truth: the fault alarm reports '
                     'alarm=true under suppressed/out_of_service '
                     'with the unacknowledged latch withheld')

        # Leg 5 — the manual return under the standing fault: the
        # false write reopens the in-service leg, avail rejoins, and
        # suppression's release re-annunciates the trip that outlasted
        # it — the declared contract's fresh unacknowledged.
        if not write(held_oos, False):
            return case.finish('failed', 'oos-failed: the oos '
                               'release write was refused: '
                               + json.dumps(submitted[-1])[:300])
        oos_window[0] = False
        ref = save_evidence(ctx['evidence_dir'],
                            'pump-oos-release-receipt.json',
                            submitted[-1])
        case.evidence('file', ref, 'the attributed oos release '
                      'receipt')

        def returned(snap):
            if value('oos%d' % held[0], snap) is not False:
                return None
            for key in ('oos_ok%d' % held[0], 'oos_ok_avail%d'
                        % held[0], 'oos_ok_guard%d' % held[0],
                        'avail%d' % held[0], 'avail_in%d' % held[0]):
                if value(key, snap) is not True:
                    return None
            for flag, want in (('alarm', True),
                               ('unacknowledged', True),
                               ('suppressed', False),
                               ('out_of_service', False)):
                if value('fault_%d_%s' % (held[0], flag), snap) \
                        is not want:
                    return None
            return snap

        hit, error = leg(
            'returned', returned,
            ['oos%d' % held[0], 'oos_ok%d' % held[0],
             'avail%d' % held[0], 'avail_in%d' % held[0],
             'fault_%d_alarm' % held[0],
             'fault_%d_unacknowledged' % held[0],
             'fault_%d_suppressed' % held[0],
             'fault_%d_out_of_service' % held[0]],
            'oos-failed: the manual return never landed — the '
            'in-service leg, the avail rejoin, or the suppression-'
            'release re-annunciation missing')
        if error:
            return error
        case.observe('manual return: avail rejoined and the '
                     'outlasted fault re-annunciated at tick '
                     + str(hit.get('tick')))

        # The fault clears, the latch holds for the receipted ack,
        # and the ack input restores.
        verdict = _plant_ctl(ctx, 'clear-fault', str(injected))
        if verdict.get('result') != 'done':
            return case.finish('failed', 'oos-failed: clear_fault on '
                               'the held pump\'s run point refused: '
                               + json.dumps(verdict)[:300])
        injected = None

        def cleared(snap):
            return value('fault%d' % held[0], snap) is False \
                and value('fault_%d_alarm' % held[0], snap) is False \
                and value('fault_%d_unacknowledged' % held[0], snap) \
                is True

        hit, error = leg(
            'cleared', cleared,
            ['fault%d' % held[0], 'fault_%d_alarm' % held[0],
             'fault_%d_unacknowledged' % held[0]],
            'oos-failed: the cleared fault never landed — the latch '
            'must hold unacknowledged until the receipted ack')
        if error:
            return error
        case.observe('the fault cleared with the latch still '
                     'standing at tick ' + str(hit.get('tick')))

        ack_point = points['fault_%d_ack' % held[0]]
        if not write(ack_point, True):
            return case.finish('failed', 'oos-failed: the fault-ack '
                               'write was refused: '
                               + json.dumps(submitted[-1])[:300])
        restore_ack = (base, ack_point)

        hit, error = leg(
            'acknowledged',
            lambda s: value('fault_%d_unacknowledged' % held[0], s)
            is False,
            ['fault_%d_ack' % held[0],
             'fault_%d_unacknowledged' % held[0]],
            'oos-failed: the receipted ack never cleared the '
            'unacknowledged latch')
        if error:
            return error
        if not write(ack_point, False):
            return case.finish('failed', 'oos-failed: the ack '
                               'restore write was refused: '
                               + json.dumps(submitted[-1])[:300])
        restore_ack = None
        case.observe('the receipted ack settled the latch and '
                     'restored at tick ' + str(hit.get('tick')))

        # Leg 6 — rotation eligibility: the returned pump takes duty
        # at the next cycle end and stages the next demand.
        def rejoined(snap):
            return value('duty', snap) == held[0] \
                and value('cmd%d' % held[0], snap) is True \
                and isinstance(value('staged', snap), int) \
                and value('staged', snap) >= 1

        hit, error = leg(
            'rejoined', rejoined,
            ['duty', 'staged', 'cmd%d' % held[0],
             'avail%d' % held[0]],
            'oos-failed: the returned pump never rejoined the duty '
            'rotation — duty never named it again under the declared '
            'alternate policy', deadline=OOS_CYCLE_DEADLINE)
        if error:
            return error
        case.observe('rotation eligibility restored: duty=p'
                     + str(100 + held[0]) + ' commanding again at '
                     'tick ' + str(hit.get('tick')))

        if breaches:
            return case.finish('failed', 'oos-failed: '
                               + '; '.join(breaches))
        if _settled_active(ctx) != active \
                or _tracking_peer(ctx, active) != tracking:
            return case.finish('failed', 'oos-failed: the pair\'s '
                               'roles moved during the leg')

        # The durable record: every submission settles through the
        # receipted path attributed to the lane actor, and each
        # declared-journaled point records the leg's transitions in
        # order — while no sibling or unbound managed flag, and no
        # none-available, ever asserts.
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
            elif receipt.get('actor') != OOS_ACTOR:
                missing.append('a settled receipt lost its actor: '
                               + json.dumps(receipt)[:200])
            elif 'applied' not in (receipt.get('outcome') or {}):
                missing.append('a settled receipt did not apply: '
                               + json.dumps(receipt)[:200])
        changes = _journal_point_changes(journal)
        bool_t, bool_f = {'bool': True}, {'bool': False}
        # The ordered record the leg drove: the hold and release, the
        # availability drop and rejoin, the fault prove and clear, and
        # each managed flag whose declared binding the oos point drives
        # — the alarm's own `oos` binding answers on the write's scan,
        # a `suppress` bound to the delivered copy one carrier later.
        # An unbound lifecycle input can never assert its flag, and a
        # binding to a driver outside this leg's model is left
        # unchecked — the declared precedence read off the descriptors,
        # never assumed.
        ordered = {'oos%d' % held[0]: [bool_t, bool_f],
                   'avail%d' % held[0]: [bool_f, bool_t],
                   'fault%d' % held[0]: [bool_t, bool_f],
                   'fault_%d_alarm' % held[0]: [bool_t, bool_f],
                   'fault_%d_unacknowledged' % held[0]: [bool_t, bool_f]}
        never_true = ['none_available']
        for index in (1, 2):
            for kind in ('fault', 'thermal', 'moisture'):
                ports = managed[(index, kind)]
                for flag, port in (('shelved', 'shelve'),
                                   ('suppressed', 'suppress'),
                                   ('out_of_service', 'oos')):
                    key = '%s_%d_%s' % (kind, index, flag)
                    bound = ports.get(port)
                    if bound is None \
                            or (bound in (points['oos%d' % index],
                                          points['fault_sup_in%d'
                                                 % index])
                                and index != held[0]):
                        never_true.append(key)
                    elif bound in (points['oos%d' % index],
                                   points['fault_sup_in%d' % index]):
                        ordered[key] = [bool_t, bool_f]
                if not (index == held[0] and kind == 'fault'):
                    never_true.extend(
                        '%s_%d_%s' % (kind, index, flag)
                        for flag in ('alarm', 'unacknowledged'))
        for key, wanted in ordered.items():
            if not _subsequence(wanted, changes.get(points[key], [])):
                missing.append('point ' + str(points[key]) + ' never '
                               'journaled the ordered ' + key
                               + ' transitions: '
                               + json.dumps(changes.get(points[key],
                                                        []))[:200])
        for key in never_true:
            if bool_t in changes.get(points[key], []):
                missing.append('point ' + str(points[key]) + ' (' + key
                               + ') journaled a true transition the '
                               'leg never drove')
        ref = save_evidence(
            ctx['evidence_dir'], 'pump-oos-journal.json',
            {'floor': floor, 'receipts': len(settled),
             'missing': missing,
             'transitions': {key: changes.get(points[key], [])
                             for key in sorted(ordered)}})
        case.evidence('file', ref, 'the journaled transitions and '
                      'settled receipts above the floor')
        if missing:
            return case.finish('failed', 'oos-failed: journaled '
                               'evidence missing: '
                               + '; '.join(missing))
        case.observe('journaled: the oos hold/release, the avail '
                     'drop and rejoin, the fault prove/clear, the '
                     'managed flags, and every settled receipt')

        # The tick-domain audit: /history carries every transition in
        # scan order — the handover lands inside the declared wiring
        # depth, the held pump's command never re-asserts while the
        # hold stands, staged never exceeds the available count, and
        # the withheld annunciation lands only on suppression's
        # release under the standing fault.
        watch = {'oos': points['oos%d' % held[0]],
                 'avail': points['avail%d' % held[0]],
                 'duty': points['duty'], 'staged': points['staged'],
                 'cmd_held': points['cmd%d' % held[0]],
                 'cmd_sibling': points['cmd%d' % sibling],
                 'fault': points['fault%d' % held[0]],
                 'alarm': points['fault_%d_alarm' % held[0]],
                 'unack': points['fault_%d_unacknowledged' % held[0]],
                 'suppressed': points['fault_%d_suppressed' % held[0]],
                 'oos_flag': points['fault_%d_out_of_service'
                                    % held[0]]}
        query = ''.join('&point=' + str(point)
                        for point in sorted(set(watch.values())))
        _, history = http_json('GET', base + '/history?since=0'
                               + query)
        transitions = {key: _history_transitions(history, point,
                                                 anchor_tick)
                       for key, point in watch.items()}

        def nth(key, landed, n=1):
            return _nth_transition(transitions.get(key) or [],
                                   landed, n)

        marks = {'oos_true': nth('oos', True),
                 'oos_false': nth('oos', False),
                 'avail_false': nth('avail', False),
                 'avail_true': nth('avail', True),
                 'duty_sibling': nth('duty', sibling),
                 'duty_held': nth('duty', held[0]),
                 'cmd_held_true': nth('cmd_held', True),
                 'oos_flag_on': nth('oos_flag', True),
                 'oos_flag_off': nth('oos_flag', False),
                 'supp_on': nth('suppressed', True),
                 'supp_off': nth('suppressed', False),
                 'fault_on': nth('fault', True),
                 'fault_off': nth('fault', False),
                 'alarm_on': nth('alarm', True),
                 'alarm_off': nth('alarm', False),
                 'unack_on': nth('unack', True),
                 'unack_off': nth('unack', False)}
        deltas = {key: (marks[key] - marks['oos_true']
                        if marks[key] is not None
                        and marks['oos_true'] is not None else None)
                  for key in marks}
        ref = save_evidence(ctx['evidence_dir'],
                            'pump-oos-history.json',
                            {'anchor_tick': anchor_tick,
                             'marks': marks, 'deltas': deltas})
        case.evidence('file', ref, 'the tick-domain transition '
                      'evidence — deltas anchored on the oos write')
        missing = sorted(key for key, tick in marks.items()
                         if tick is None)
        if missing:
            return case.finish(
                'failed', 'oos-failed: the served history never '
                'showed ' + ', '.join(missing) + ': '
                + json.dumps({key: transitions[key]
                              for key in ('oos', 'avail', 'duty',
                                          'unack')})[:400])
        problems = []
        if marks['avail_false'] < marks['oos_true'] \
                or marks['avail_false'] - marks['oos_true'] \
                > OOS_BOUND_TICKS:
            problems.append('the avail drop landed '
                            + str(marks['avail_false']
                                  - marks['oos_true'])
                            + ' ticks from the oos write — outside '
                            'the declared wiring bound')
        if marks['duty_sibling'] < marks['avail_false'] \
                or marks['duty_sibling'] - marks['avail_false'] \
                > OOS_BOUND_TICKS:
            problems.append('duty handed to the sibling '
                            + str(marks['duty_sibling']
                                  - marks['avail_false'])
                            + ' ticks from the avail drop — outside '
                            'the declared wiring bound')
        if marks['oos_flag_on'] - marks['oos_true'] \
                > OOS_BOUND_TICKS \
                or marks['supp_on'] - marks['oos_true'] \
                > OOS_BOUND_TICKS:
            problems.append('the managed flags asserted beyond the '
                            'declared wiring bound '
                            + json.dumps(deltas, sort_keys=True)[:300])
        if marks['avail_true'] < marks['oos_false'] \
                or marks['avail_true'] - marks['oos_false'] \
                > OOS_BOUND_TICKS:
            problems.append('the avail rejoin landed '
                            + str(marks['avail_true']
                                  - marks['oos_false'])
                            + ' ticks from the release — outside '
                            'the declared wiring bound')
        if marks['cmd_held_true'] <= marks['oos_false']:
            problems.append('the held pump\'s command asserted while '
                            'the hold stood (tick '
                            + str(marks['cmd_held_true']) + ')')
        if any(landed == held[0] and tick < marks['oos_false']
               for tick, landed in transitions['duty']):
            problems.append('duty named the held pump while the '
                            'hold stood')
        if any(isinstance(landed, int) and landed > 1
               and tick < marks['oos_false']
               for tick, landed in transitions['staged']):
            problems.append('staged exceeded the available count '
                            'while the hold stood')
        if not marks['supp_off'] <= marks['unack_on'] \
                <= marks['unack_off']:
            problems.append('the withheld annunciation did not land '
                            'on suppression\'s release (supp_off '
                            + str(marks['supp_off']) + ', unack '
                            + str(marks['unack_on']) + ' -> '
                            + str(marks['unack_off']) + ')')
        if marks['unack_on'] <= marks['fault_on']:
            problems.append('the mid-OOS fault annunciated before '
                            'suppression released — the named-'
                            'without-annunciating contract broken')
        if marks['alarm_on'] < marks['fault_on'] \
                or marks['alarm_on'] - marks['fault_on'] \
                > OOS_BOUND_TICKS:
            problems.append('the fault alarm did not follow the '
                            'proven fault inside the carrier hop')
        if marks['duty_held'] <= marks['oos_false']:
            problems.append('duty returned to the held pump before '
                            'the release landed')
        if problems:
            return case.finish('failed', 'oos-nondeterministic: '
                               + '; '.join(problems))
        case.observe('history: handover +'
                     + str(marks['duty_sibling'] - marks['oos_true'])
                     + ' ticks from the write, managed flags +'
                     + str(marks['supp_on'] - marks['oos_true'])
                     + ', the withheld annunciation landed at +'
                     + str(marks['unack_on'] - marks['oos_true'])
                     + ' on suppression\'s release')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
    finally:
        # The held oos point, the injected fault, and the standing ack
        # are the run's shared state: a case that leaves any of them
        # standing poisons every later leg. The pair's roles never
        # moved — there is nothing to fail back.
        live_base = None
        try:
            settled = _settled_active(ctx)
            if settled is not None:
                live_base = ctx[settled]
        except Exception:
            pass
        if held_oos is not None and live_base is not None:
            try:
                http_json('POST', live_base + '/command',
                          {'command': {'write_value': {
                              'point': held_oos, 'kind': 'bool',
                              'value': {'bool': False}}},
                           'actor': OOS_ACTOR})
            except Exception:
                pass
        if restore_ack is not None:
            rbase, rpoint = restore_ack
            try:
                http_json('POST', rbase + '/command',
                          {'command': {'write_value': {
                              'point': rpoint, 'kind': 'bool',
                              'value': {'bool': False}}},
                           'actor': OOS_ACTOR})
            except Exception:
                pass
        if injected is not None:
            try:
                _try_plant_ctl(ctx, 'clear-fault', str(injected))
            except Exception:
                pass
