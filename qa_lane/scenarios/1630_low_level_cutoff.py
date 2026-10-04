"""The low_level_cutoff acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *


# --------------------------------------------------------------------
# The threshold-chain's cutoff clamp and the low-level `lal`
# lifecycle (WW-CTL-002, WW-OPS-002 — decision 42's cutoff bound on
# the deployed station). The chain leg walks `start`/`lag_start`/
# `high` upward and the declared de-stage at `stop`, but the `cutoff`
# bound — the low cut-off that releases every call while a Good
# level reads at or below it — and `lal`'s low-level two-flag
# lifecycle are unexercised.
#
# The honest lever is the declared forcing input `inflow`: no
# dynamics element drives it, so the harness writes it through the
# plant protocol's shared-claim path under the settled active's
# pinned owner token — the same seam the staging leg uses. With the
# pair settled and the station holding a duty pump commanded, a
# strongly negative inflow drains the wet well through `stop` to
# `cutoff`: `below_cutoff` asserts, `demand` releases to 0 with
# `duty_call`/`lag_call` off and the running pump stopped, and the
# managed `lal` alarm annunciates `alarm`/`unacknowledged` with
# journaled `point_changed` evidence. A held inflow pinned at net
# zero keeps the Good level at the cutoff while the receipted `ack`
# clears `unacknowledged` with `alarm` standing; restoring the
# baseline inflow lets the level rise past the declared hysteresis —
# the alarm returns — and past `start` where demand resumes and a
# pump stages. Every driven input restores, the pair's roles never
# move, and the durable journal carries the lifecycle entries in
# order beside the attributed receipts. Named diagnostics:
# cutoff-failed for a broken clamp, lifecycle, or restore clause;
# cutoff-nondeterministic when the served surfaces cannot drive or
# record a deterministic leg.

CUTOFF_DEADLINE = 30      # bound on each leg's served transition
CUTOFF_TRAVERSE = 60      # bound on the drain and the rising return
CUTOFF_POLL = 0.05        # transition-watch cadence — under the scan
CUTOFF_ACTOR = 'qa-lane'
CUTOFF_DRAIN_INFLOW = -3.0  # unopposed drain past stop to the cutoff
CUTOFF_HOLD_INFLOW = -0.2   # net zero against the declared 0.2 bias —
                          # pins the Good level at the cutoff while
                          # the two-flag lifecycle is exercised


def scenario_low_level_cutoff(ctx):
    """A driven level at or below `cutoff` raises `below_cutoff`,
    releases `demand` to 0 with every pump call off and the running
    pump stopped, and annunciates `lal` through the two-flag
    lifecycle with journaled evidence; the receipted `ack` clears
    `unacknowledged` while `alarm` stands; recovery past hysteresis
    returns the alarm and demand resumes at `start`."""
    case = Case('low-level-cutoff',
                'The cutoff clamp and the low-level alarm lifecycle',
                'with the deployed pair settled and tracking and the '
                'station holding a duty pump commanded, a shared-'
                'claim inflow write drains the wet well through stop '
                'to the declared cutoff: below_cutoff asserts, '
                'demand releases to 0 with duty_call/lag_call off and '
                'the running pump stopped, and the managed lal alarm '
                'annunciates alarm/unacknowledged with journaled '
                'point_changed evidence; a held net-zero inflow pins '
                'the level at the cutoff while the receipted ack '
                'clears the unacknowledged latch with the alarm '
                'standing; restoring the baseline inflow lets the '
                'level rise past the declared hysteresis — the alarm '
                'returning — and past start where demand resumes and '
                'a pump stages; every driven input restores, the '
                'pair\'s roles never move, and every commanded '
                'transition settles through the attributed '
                'receipted path')
    stream = None          # the shared-claim plant connection
    restore_inflow = None  # (point, baseline) while the drive stands
    restore_ack = None     # (base, point) while the ack write stands
    try:
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + CUTOFF_DEADLINE)
        if active is None:
            return case.finish('failed', 'no peer reports '
                               'role=active')
        base = ctx[active]
        tracking = wait_for(lambda: _tracking_peer(ctx, active),
                            time.monotonic() + CUTOFF_DEADLINE,
                            interval=POLL_INTERVAL)
        if tracking is None:
            return case.finish('inconclusive',
                               'no tracking peer — the deployed pair '
                               'never settled')
        case.observe('settled pair: ' + active + ' active, '
                     + tracking + ' tracking')

        _, signals = http_json('GET', base + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'low-level-cutoff-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming the '
                      'low-level-cutoff leg\'s wiring')
        names = {'inflow': 'inflow', 'level-selected': 'level',
                 'demand': 'demand', 'demand-in': 'demand_in',
                 'below-cutoff': 'below_cutoff',
                 'duty-call': 'duty_call', 'lag-call': 'lag_call',
                 'duty': 'duty', 'staged': 'staged',
                 'p101-cmd': 'cmd1', 'p102-cmd': 'cmd2',
                 'p101-run': 'run1', 'p102-run': 'run2',
                 'lal-ack': 'lal_ack', 'lal-alarm': 'lal_alarm',
                 'lal-unacknowledged': 'lal_unack',
                 'lal-shelved': 'lal_shelved',
                 'lal-suppressed': 'lal_suppressed',
                 'lal-out-of-service': 'lal_out_of_service'}
        entries = {entry.get('name'): entry
                   for entry in signals.get('points', [])
                   if entry.get('name') in names}
        missing = sorted(set(names) - set(entries))
        if missing:
            return case.finish('inconclusive', 'the deployed model '
                               'lacks the low-level-cutoff leg\'s '
                               'wiring — no signals '
                               + ', '.join(missing))
        points = {names[name]: entry.get('point')
                  for name, entry in entries.items()}
        ack_entry = entries['lal-ack']
        if not (ack_entry.get('writable')
                and ack_entry.get('direction') == 'in'
                and ack_entry.get('value_type') == 'bool'):
            return case.finish('inconclusive', 'the lal-ack point is '
                               'not the alarm\'s writable bool ack '
                               'input: ' + json.dumps(ack_entry)[:300])
        case.observe('cutoff path: '
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
        table = {name: _parameter_value(snap0, chain_name, name)
                 for name in ('cutoff', 'stop', 'start')}
        if not all(isinstance(item, (int, float))
                   and not isinstance(item, bool)
                   for item in table.values()) \
                or not (table['cutoff'] < table['stop']
                        < table['start']):
            return case.finish('inconclusive', 'the deployed chain '
                               'does not declare the ordered '
                               'cutoff<stop<start the leg reads: '
                               + json.dumps(table))
        lal = None
        for entry in (snap0 or {}).get('descriptors') or []:
            if entry.get('kind') != 'managed-latching-alarm':
                continue
            ports = {port.get('name'): port.get('point')
                     for port in entry.get('ports') or []}
            if ports.get('alarm') == points['lal_alarm']:
                lal = entry.get('name')
        if lal is None:
            return case.finish('inconclusive', 'the served '
                               'descriptors bind no managed-latching-'
                               'alarm to the lal-alarm point '
                               + str(points['lal_alarm']))
        low_limit = _parameter_value(snap0, lal, 'low_limit')
        hysteresis = _parameter_value(snap0, lal, 'hysteresis')
        if not isinstance(low_limit, (int, float)) \
                or isinstance(low_limit, bool) \
                or not isinstance(hysteresis, (int, float)) \
                or isinstance(hysteresis, bool):
            return case.finish('inconclusive', 'the lal instance '
                               'declares no low_limit/hysteresis the '
                               'lifecycle reads: '
                               + json.dumps({'low_limit': low_limit,
                                             'hysteresis':
                                             hysteresis}))
        case.observe('declared bounds: cutoff=' + str(table['cutoff'])
                     + ' stop=' + str(table['stop']) + ' start='
                     + str(table['start']) + ' — lal low_limit='
                     + str(low_limit) + ' hysteresis='
                     + str(hysteresis))

        if ctx.get('plant') is None:
            return case.finish('inconclusive',
                               'the run publishes no plant endpoint')
        owner = (ctx.get('plant_owner') or {}).get(active)
        if owner is None:
            return case.finish('inconclusive', 'the run pins no '
                               'plant-writer owner token for the '
                               'settled active ' + str(active))
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
            reported a sample, cutoff-failed when the served values
            never landed the leg."""
            hit = wait_for(lambda: poll(cond, keys),
                           time.monotonic()
                           + (deadline or CUTOFF_DEADLINE),
                           interval=CUTOFF_POLL)
            snap = last.get('snap') or {}
            ref_ = save_evidence(
                ctx['evidence_dir'], 'low-level-cutoff-' + name
                + '.json',
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
                    'inconclusive', 'cutoff-nondeterministic: the '
                    + name + ' leg\'s outputs never reported on the '
                    'served snapshot: ' + ', '.join(unreported))
            return None, case.finish(
                'failed', failed + '; last served ' + json.dumps(
                    {key: value(key, snap) for key in sorted(keys)
                     if key in points}, sort_keys=True)[:500])

        def inflow_write(value_):
            return _plant_request(
                stream, {'op': 'write', 'point': points['inflow'],
                         'value': {'float': value_}})

        # Leg 1 — the commanded baseline: the level loop holds a duty
        # pump commanded — the clamp must release a running pump, not
        # merely hold demand at zero.
        def commanded(snap):
            duty = value('duty', snap)
            if duty not in (1, 2):
                return None
            if value('cmd%d' % duty, snap) is not True:
                return None
            if value('below_cutoff', snap) is not False \
                    or value('lal_alarm', snap) is not False \
                    or value('lal_unack', snap) is not False:
                return None
            return snap

        baseline, error = leg(
            'baseline', commanded,
            ['demand_in', 'demand', 'duty', 'staged', 'below_cutoff',
             'duty_call', 'lag_call', 'cmd1', 'cmd2', 'level',
             'lal_alarm', 'lal_unack'],
            'cutoff-failed: the pair never held the commanded-duty '
            'baseline the leg needs', deadline=CUTOFF_TRAVERSE)
        if error:
            return error
        holder = value('duty', baseline)
        case.observe('commanded baseline: duty=p' + str(100 + holder)
                     + ' commanding, level '
                     + str(value('level', baseline)))

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
                {'command': command, 'actor': CUTOFF_ACTOR})
            submitted.append({'command': command, 'status': status,
                              'receipt': receipt})
            outcome = (receipt or {}).get('outcome') or {}
            return status == 200 and 'rejected' not in outcome

        # Leg 2 — the drain: the held negative inflow empties the
        # well through stop to the cutoff — below_cutoff asserts,
        # demand releases to 0 with every call off, the running
        # pump's command and looped-back run contact release, and
        # the managed lal alarm annunciates through the two-flag
        # lifecycle.
        verdict = inflow_write(CUTOFF_DRAIN_INFLOW)
        if verdict.get('result') != 'done':
            return case.finish('failed', 'cutoff-failed: the inflow '
                               'drain write on point '
                               + str(points['inflow'])
                               + ' was refused under the shared '
                               'claim: ' + json.dumps(verdict)[:300])
        restore_inflow = (points['inflow'], baseline_inflow)
        case.observe('inflow driven to '
                     + str(CUTOFF_DRAIN_INFLOW) + ' — the well '
                     'draining to the cutoff')

        def clamped(snap):
            level = value('level', snap)
            if not isinstance(level, (int, float)) \
                    or isinstance(level, bool) \
                    or level > table['cutoff']:
                return None
            if value('below_cutoff', snap) is not True \
                    or value('demand', snap) != 0 \
                    or value('duty_call', snap) is not False \
                    or value('lag_call', snap) is not False \
                    or value('staged', snap) != 0 \
                    or value('cmd%d' % holder, snap) is not False \
                    or value('run%d' % holder, snap) is not False \
                    or value('lal_alarm', snap) is not True \
                    or value('lal_unack', snap) is not True:
                return None
            return snap

        hit, error = leg(
            'clamped', clamped,
            ['level', 'below_cutoff', 'demand', 'demand_in',
             'duty_call', 'lag_call', 'staged', 'cmd1', 'cmd2',
             'run%d' % holder, 'lal_alarm', 'lal_unack'],
            'cutoff-failed: the cutoff clamp never landed — '
            'below_cutoff, the demand release, the pump stop, or '
            'the lal annunciation missing', deadline=CUTOFF_TRAVERSE)
        if error:
            return error
        case.observe('the clamp: below_cutoff standing at level '
                     + str(value('level', hit))
                     + ', demand released, the running pump stopped, '
                     'lal annunciating at tick '
                     + str(hit.get('tick')))
        if _settled_active(ctx) != active:
            return case.finish('failed', 'cutoff-failed: the '
                               'active role moved under a field '
                               'write — a plant-side input is not '
                               'peer loss')

        # Pin the level at the cutoff: net zero against the declared
        # bias holds the Good reading under the low limit while the
        # two-flag lifecycle is exercised — the integrator cannot
        # drift the condition away mid-ack.
        verdict = inflow_write(CUTOFF_HOLD_INFLOW)
        if verdict.get('result') != 'done':
            return case.finish('failed', 'cutoff-failed: the inflow '
                               'hold write on point '
                               + str(points['inflow'])
                               + ' was refused under the shared '
                               'claim: ' + json.dumps(verdict)[:300])
        case.observe('inflow pinned at net zero — the level held at '
                     'the cutoff')

        # Leg 3 — ack while standing: the receipted lal-ack clears
        # the unacknowledged latch while the alarm condition and its
        # standing flag hold.
        if not write(points['lal_ack'], True):
            return case.finish('failed', 'cutoff-failed: the lal-ack '
                               'write was refused: '
                               + json.dumps(submitted[-1])[:300])
        restore_ack = (base, points['lal_ack'])

        hit, error = leg(
            'acknowledged',
            lambda s: value('lal_unack', s) is False
            and value('lal_alarm', s) is True
            and value('below_cutoff', s) is True
            and value('level', s) is not None
            and value('level', s) <= table['cutoff'],
            ['lal_ack', 'lal_unack', 'lal_alarm', 'below_cutoff',
             'level'],
            'cutoff-failed: the receipted lal ack never cleared the '
            'unacknowledged latch with the alarm standing')
        if error:
            return error
        case.observe('acknowledged: the latch cleared at level '
                     + str(value('level', hit))
                     + ' with the alarm still standing')
        if not write(points['lal_ack'], False):
            return case.finish('failed', 'cutoff-failed: the lal-ack '
                               'restore write was refused: '
                               + json.dumps(submitted[-1])[:300])
        restore_ack = None

        # Leg 4 — the recovery: the baseline inflow restored, the
        # level rises past the declared hysteresis — the alarm
        # returns and below_cutoff releases — then past start where
        # demand resumes and a duty pump stages.
        verdict = inflow_write(baseline_inflow)
        if verdict.get('result') != 'done':
            return case.finish('failed', 'cutoff-failed: the inflow '
                               'restore write was refused under the '
                               'shared claim: '
                               + json.dumps(verdict)[:300])
        restore_inflow = None
        case.observe('inflow restored to '
                     + str(baseline_inflow) + ' — the level rising')

        def returned(snap):
            level = value('level', snap)
            if not isinstance(level, (int, float)) \
                    or isinstance(level, bool) \
                    or level <= low_limit + hysteresis:
                return None
            if value('lal_alarm', snap) is not False \
                    or value('lal_unack', snap) is not False \
                    or value('below_cutoff', snap) is not False:
                return None
            return snap

        hit, error = leg(
            'returned', returned,
            ['level', 'lal_alarm', 'lal_unack', 'below_cutoff'],
            'cutoff-failed: the recovery past hysteresis never '
            'returned the alarm — lal must stand down with the '
            'latch clear and below_cutoff released',
            deadline=CUTOFF_TRAVERSE)
        if error:
            return error
        case.observe('returned: the alarm stood down at level '
                     + str(value('level', hit))
                     + ' past hysteresis ' + str(low_limit
                                               + hysteresis))

        def resumed(snap):
            demand = value('demand', snap)
            if not isinstance(demand, int) or isinstance(demand, bool) \
                    or demand < 1:
                return None
            if value('duty_call', snap) is not True \
                    or not isinstance(value('staged', snap), int) \
                    or isinstance(value('staged', snap), bool) \
                    or value('staged', snap) < 1 \
                    or value('duty', snap) not in (1, 2):
                return None
            return snap

        hit, error = leg(
            'resumed', resumed,
            ['level', 'demand', 'demand_in', 'duty_call', 'lag_call',
             'staged', 'duty', 'cmd1', 'cmd2'],
            'cutoff-failed: demand never resumed at the start '
            'crossing — the duty call, the stage, or the duty '
            'designation missing', deadline=CUTOFF_TRAVERSE)
        if error:
            return error
        case.observe('resumed: demand ' + str(value('demand', hit))
                     + ' at level ' + str(value('level', hit))
                     + ' — duty=p' + str(100 + value('duty', hit))
                     + ' staging at tick ' + str(hit.get('tick')))

        if _settled_active(ctx) != active \
                or _tracking_peer(ctx, active) != tracking:
            return case.finish('failed', 'cutoff-failed: the '
                               'pair\'s roles moved during the leg')

        # The durable record: each declared-journaled point records
        # the leg's transitions in order — the clamp's assert and
        # release, the stopped pump's run contact, the managed
        # alarm's lifecycle — and every submission settles applied
        # through the receipted path attributed to the lane actor.
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
            elif receipt.get('actor') != CUTOFF_ACTOR:
                missing.append('a settled receipt lost its actor: '
                               + json.dumps(receipt)[:200])
            elif 'applied' not in (receipt.get('outcome') or {}):
                missing.append('a settled receipt did not apply: '
                               + json.dumps(receipt)[:200])
        changes = _journal_point_changes(journal)
        bool_t, bool_f = {'bool': True}, {'bool': False}
        ordered = {
            'below_cutoff': [bool_t, bool_f],
            'lal_alarm': [bool_t, bool_f],
            'lal_unack': [bool_t, bool_f],
            'run%d' % holder: [bool_f]}

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
                               'under a field write')
        ref = save_evidence(
            ctx['evidence_dir'], 'low-level-cutoff-journal.json',
            {'floor': floor, 'receipts': len(settled),
             'missing': missing,
             'transitions': {key: changes.get(points[key], [])
                             for key in sorted(ordered)}})
        case.evidence('file', ref, 'the journaled transitions and '
                      'settled receipts above the floor')
        if missing:
            return case.finish('failed', 'cutoff-failed: journaled '
                               'evidence missing: '
                               + '; '.join(missing))
        case.observe('journaled: the clamp, the run stop, the lal '
                     'lifecycle, and every settled receipt')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
    finally:
        # The driven inflow and the standing ack input are the run's
        # shared state: a case that leaves either standing poisons
        # every later leg. The pair's roles never moved — there is
        # nothing to fail back.
        if stream is not None:
            if restore_inflow is not None:
                try:
                    _plant_request(
                        stream, {'op': 'write',
                                 'point': restore_inflow[0],
                                 'value': {'float':
                                           restore_inflow[1]}})
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
                           'actor': CUTOFF_ACTOR})
            except Exception:
                pass
