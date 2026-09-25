"""The duty_rotation acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: The duty-rotation case shares that restored window: it cycles
# demand through the writable maintenance points, runs its own mid-
# cycle a->b switch for the carried rotation position, and fails back
# to the launch roles before the force case.
RUNS_BEFORE = frozenset({'scenario_force_carryover'})


# --------------------------------------------------------------------
# The declared duty-rotation contract and its carryover across
# promotion (WW-CTL-001's rotation clause — decision 41 — and
# WW-LCM-001's named continuity state): the rig's pump group declares
# rotation = alternate-each-cycle with min_off_ticks and
# start_delay_ticks, so consecutive demand cycles must hand `duty`
# to the next available pump in rotation order, and the rotation
# position plus the banked run-hours must ride the checkpoint into
# the promoted peer rather than reset. The honest lever the deployed
# rig admits is the group's own writable maintenance points: holding
# both pumps out of service lets the lane dynamics' declared inflow
# lift the level past `start` unopposed, and releasing them returns
# the station to the group, whose staged duty holder drains the well
# back through `stop`. The demote/promote lands mid-cycle — while the
# rotated duty's in-flight command stands — and every writable point,
# the none-available annunciation the hold latches, and the pair's
# roles are restored for the later legs.
#
# Rotation position and run-hours read back through the checkpoint the
# monitor serves — the same StateMap the tracking peer adopts — so the
# carryover assertions observe exactly what WW-LCM-001 names. Named
# diagnostics: rotation-failed for a broken rotation or carryover
# clause, rotation-nondeterministic when the deployed dynamics cannot
# drive a deterministic cycle.

ROTATION_DEADLINE = 30         # bound on settle, track, and audit waits
ROTATION_RISE_DEADLINE = 15    # bound on the unopposed rise past `start`
ROTATION_CYCLE_DEADLINE = 20   # bound on each start/stop observation
ROTATION_SWITCH_DEADLINE = 30  # bound on each demote/promote leg
ROTATION_POLL = 0.5            # level-edge cadence
ROTATION_ACTOR = 'qa-lane'
ROTATION_MIN_OFF_TICKS = 2     # the rig model's declared holdout


def _rotation_component(checkpoint):
    """The pump-group's checkpointed StateMap — the component whose
    carried vocabulary names the rotation position — as (name, state)."""
    for name, state in (checkpoint or {}).get('components', {}).items():
        if isinstance(state, dict) and 'rotation_cursor' in state:
            return name, state
    return None, {}


def _state_value(state, key):
    """One scalar out of a checkpoint StateMap — the `{"int": v}` /
    `{"bool": v}` wire shape unwrapped."""
    value = (state or {}).get(key)
    if isinstance(value, dict):
        return next(iter(value.values()), None)
    return value


def _point_changes(journal, point):
    """The `to` values the journal recorded for `point`, in seq order —
    point_changed entries only."""
    changes = []
    for entry in _journal_list(journal):
        change = (entry.get('event') or {}).get('point_changed') or {}
        if change.get('point') == point:
            changes.append((entry.get('tick'), change.get('to')))
    return changes


def _checkpoint_at(ctx, base):
    """`GET /checkpoint` or None — one dropped read loses a sample, not
    the leg's verdict."""
    try:
        _, checkpoint = http_json('GET', base + '/checkpoint')
        return checkpoint
    except Exception:
        return None


def scenario_duty_rotation(ctx):
    """Two demand cycles alternate the declared duty holder under the
    alternate-each-cycle policy with the declared off-time and start
    delay honored, and the rotation position plus banked run-hours
    ride the checkpoint across a mid-cycle demote/promote."""
    case = Case('duty-rotation',
                'Duty rotation alternates and carries across promotion',
                'with the pair settled and the station answering the '
                'declared dynamics, two consecutive demand cycles '
                'alternate duty between the pumps under the declared '
                'alternate-each-cycle policy with min_off_ticks and '
                'start_delay_ticks honored and staged reporting the '
                'commanded count; a demote/promote mid-cycle leaves '
                'the promoted peer naming the rotation position and '
                'run-hours the checkpoint carried rather than reset; '
                'every commanded transition settles through the '
                'receipted path with journaled evidence and the rig '
                'is restored')
    held = []          # oos points while they stand held true
    ack_held = None    # the ack point while it stands true
    restored = True    # roles back as the case found them
    try:
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + ROTATION_DEADLINE)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        peer = 'standby' if active == 'active' else 'active'
        base, peer_base = ctx[active], ctx[peer]
        live = base    # writes and observations land on the current active

        def converged():
            try:
                report = _role(ctx, peer_base)
            except Exception:
                return None
            return report if 'tracking' in (report.get('sync') or {}) \
                else None

        tracking = wait_for(converged,
                            time.monotonic() + ROTATION_DEADLINE)
        ref = save_evidence(ctx['evidence_dir'],
                            'duty-rotation-peer-role.json',
                            {'peer': peer, 'report': tracking})
        case.evidence('file', ref, 'the tracking peer\'s role report')
        if not tracking:
            return case.finish('inconclusive', 'the peer never '
                               'reported tracking convergence — the '
                               'rotation pair never settled')
        case.observe('settled pair: ' + active + ' active, ' + peer
                     + ' tracking')

        _, signals = http_json('GET', base + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'duty-rotation-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming the rotation '
                      'leg\'s points')
        names = {'p101-oos': 'oos1', 'p102-oos': 'oos2',
                 'level-selected': 'level', 'demand-in': 'demand',
                 'duty': 'duty', 'staged': 'staged',
                 'p101-cmd': 'cmd1', 'p102-cmd': 'cmd2',
                 'p101-run': 'run1', 'p102-run': 'run2',
                 'none-available': 'none_available',
                 'none-available-ack': 'ack',
                 'none-available-unacknowledged': 'unack',
                 'inflow': 'inflow'}
        entries = {entry.get('name'): entry
                   for entry in signals.get('points', [])
                   if entry.get('name') in names}
        missing = sorted(set(names) - set(entries))
        if missing:
            return case.finish('inconclusive', 'the deployed model '
                               'lacks the rotation leg\'s wiring — no '
                               'signals ' + ', '.join(missing))
        points = {names[name]: entry.get('point')
                  for name, entry in entries.items()}
        for name in ('p101-oos', 'p102-oos', 'none-available-ack'):
            entry = entries[name]
            if not (entry.get('writable')
                    and entry.get('direction') == 'in'
                    and entry.get('value_type') == 'bool'):
                return case.finish('inconclusive', 'signal ' + name
                                   + ' is not the writable bool input '
                                   'the leg needs: '
                                   + json.dumps(entry)[:300])

        def cmd_point(index):
            return points['cmd1' if index == 1 else 'cmd2']

        def run_point(index):
            return points['run1' if index == 1 else 'run2']

        baseline = _snapshot(ctx, base)
        inflow = _point_value(baseline, points['inflow'])
        grown = wait_for(
            lambda: (s.get('tick', 0) > baseline.get('tick', 0)
                     and s or None)
            if (s := _try_snapshot(ctx, base)) else None,
            time.monotonic() + ROTATION_DEADLINE,
            interval=ROTATION_POLL)
        ref = save_evidence(ctx['evidence_dir'],
                            'duty-rotation-baseline.json',
                            {'baseline': baseline, 'advanced': grown,
                             'inflow': inflow})
        case.evidence('file', ref, 'the settled baseline and the '
                      'declared inflow')
        if not grown:
            return case.finish('failed', 'the active peer\'s scans '
                               'never advanced')
        if not isinstance(inflow, (int, float)):
            return case.finish('inconclusive',
                               'rotation-nondeterministic: the inflow '
                               'point serves no numeric sample — the '
                               'level cannot be trusted to rise '
                               + json.dumps(inflow))

        floors = {}
        for key, address in ((active, base), (peer, peer_base)):
            try:
                _, journal = http_json('GET', address + '/journal?since=0')
            except Exception:
                journal = []
            floors[key] = max(
                (entry.get('seq') or 0 for entry in _journal_list(journal)
                 if isinstance(entry, dict)), default=0)

        submitted = []

        def write(target_base, point, value):
            command = {'write_value': {'point': point, 'kind': 'bool',
                                       'value': {'bool': value}}}
            status, receipt = http_json(
                'POST', target_base + '/command',
                {'command': command, 'actor': ROTATION_ACTOR})
            submitted.append({'base': target_base, 'command': command,
                              'status': status, 'receipt': receipt})
            outcome = (receipt or {}).get('outcome') or {}
            return status == 200 and 'rejected' not in outcome

        observed = {}

        def watch(predicate):
            last = {}

            def probe():
                snap = _try_snapshot(ctx, live)
                if snap is None:
                    return None
                last['snap'] = snap
                return snap if predicate(snap) else None
            return probe, last

        # Leg 1 — the unopposed rise: both pumps held out of service
        # while the declared inflow lifts the level past `start`; the
        # group stages nothing and none_available annunciates.
        held_ok = True
        for point in (points['oos1'], points['oos2']):
            if not write(live, point, True):
                held_ok = False
            else:
                held.append(point)
        ref = save_evidence(ctx['evidence_dir'],
                            'duty-rotation-hold-receipts.json',
                            submitted[-2:])
        case.evidence('file', ref, 'the out-of-service hold receipts')
        if not held_ok:
            return case.finish('failed', 'an out-of-service write was '
                               'refused: '
                               + json.dumps(submitted[-1])[:300])

        def risen(snap):
            level = _point_value(snap, points['level'])
            return isinstance(level, (int, float)) and level > 2.0 \
                and _point_value(snap, points['staged']) == 0 \
                and _point_value(snap, points['none_available']) is True

        probe, last = watch(risen)
        hit = wait_for(probe, time.monotonic() + ROTATION_RISE_DEADLINE,
                       interval=ROTATION_POLL)
        ref = save_evidence(ctx['evidence_dir'],
                            'duty-rotation-held.json',
                            hit or last.get('snap') or {})
        case.evidence('file', ref, 'the held snapshot at level > start')
        if not hit:
            return case.finish('inconclusive',
                               'rotation-nondeterministic: the declared '
                               'inflow never raised the level past '
                               'start while both pumps were held out — '
                               'last level '
                               + json.dumps(_point_value(
                                   last.get('snap') or {},
                                   points['level'])))
        case.observe('held both pumps out: level rose past start, '
                     'staged 0, none_available standing')

        # Leg 2 — release and the first cycle: the group stages one
        # pump as duty, drains to `stop`, and banks the holdout.
        released_ok = True
        for point in (points['oos1'], points['oos2']):
            if write(live, point, False):
                held.remove(point)
            else:
                released_ok = False
        ref = save_evidence(ctx['evidence_dir'],
                            'duty-rotation-release-receipts.json',
                            submitted[-2:])
        case.evidence('file', ref, 'the out-of-service release '
                      'receipts')
        if not released_ok:
            return case.finish('failed', 'an out-of-service release '
                               'was refused: '
                               + json.dumps(submitted[-1])[:300])

        duty_stuck = {}

        def staged_one(snap):
            staged = _point_value(snap, points['staged'])
            duty = _point_value(snap, points['duty'])
            duty_stuck['duty'] = duty
            duty_stuck['staged'] = staged
            return staged == 1 and duty in (1, 2) \
                and _point_value(snap, cmd_point(duty)) is True

        probe, last = watch(staged_one)
        first = wait_for(probe,
                         time.monotonic() + ROTATION_CYCLE_DEADLINE,
                         interval=ROTATION_POLL)
        ref = save_evidence(ctx['evidence_dir'],
                            'duty-rotation-cycle1-start.json',
                            first or last.get('snap') or {})
        case.evidence('file', ref, 'the first cycle\'s staged duty '
                      'holder')
        if first is None:
            if duty_stuck.get('duty') in (None, 0):
                return case.finish(
                    'inconclusive',
                    'rotation-nondeterministic: duty never moved off '
                    + json.dumps(duty_stuck.get('duty'))
                    + ' through the first demand cycle')
            return case.finish('failed',
                               'rotation-failed: the released pump '
                               'group never staged a duty pump')
        duty1 = _point_value(first, points['duty'])
        duty2 = 3 - duty1
        case.observe('first cycle: duty=' + str(duty1)
                     + ' staged 1, command standing on point '
                     + str(cmd_point(duty1)))

        def stopped(snap):
            level = _point_value(snap, points['level'])
            return _point_value(snap, points['staged']) == 0 \
                and _point_value(snap, cmd_point(duty1)) is not True \
                and isinstance(level, (int, float)) and level < 1.0

        probe, last = watch(stopped)
        ended = wait_for(probe,
                         time.monotonic() + ROTATION_CYCLE_DEADLINE,
                         interval=ROTATION_POLL)
        ref = save_evidence(ctx['evidence_dir'],
                            'duty-rotation-cycle1-stop.json',
                            ended or last.get('snap') or {})
        case.evidence('file', ref, 'the first cycle\'s stop at `stop`')
        if not ended:
            return case.finish('failed',
                               'rotation-failed: the first duty '
                               'holder never stopped at stop')
        case.observe('first cycle ended: pump ' + str(duty1)
                     + ' stopped at stop')

        # The post-stop checkpoint carries the rotation position the
        # cycle-end assign produced plus the banked holdout — the
        # min_off_ticks evidence — and the accrued run-hours.
        def served_state():
            checkpoint = _checkpoint_at(ctx, live)
            if checkpoint is None:
                return None
            name, found = _rotation_component(checkpoint)
            if not found:
                return None
            observed['checkpoint'] = checkpoint
            observed['component'] = name
            observed['state'] = found
            return found

        state = wait_for(served_state,
                         time.monotonic() + ROTATION_DEADLINE,
                         interval=ROTATION_POLL)
        checkpoint = observed.get('checkpoint') or {}
        _, journal = http_json('GET', live + '/journal?since='
                               + str(floors[active]))
        run_ticks = [tick for tick, to in
                     _point_changes(journal, run_point(duty1))
                     if to == {'bool': False}]
        component = observed.get('component')
        if state is None:
            return case.finish('inconclusive', 'the served checkpoint '
                               'never carried the pump-group state')
        ref = save_evidence(
            ctx['evidence_dir'], 'duty-rotation-cycle1-state.json',
            {'component': component, 'tick': checkpoint.get('tick'),
             'state': state, 'run_stop_ticks': run_ticks})
        case.evidence('file', ref, 'the checkpointed pump-group state '
                      'after the first cycle')
        unmet = []
        if _state_value(state, 'rotation') != 0:
            unmet.append('rotation != alternate-each-cycle')
        if _state_value(state, 'min_off_ticks') \
                != ROTATION_MIN_OFF_TICKS:
            unmet.append('min_off_ticks != 2')
        if _state_value(state, 'duty') != duty2:
            unmet.append('duty did not rotate to pump ' + str(duty2)
                         + ' (reads ' + json.dumps(
                             _state_value(state, 'duty')) + ')')
        if _state_value(state, 'commanded_' + str(duty1)) is not False:
            unmet.append('the stopped holder is still commanded')
        held_until = _state_value(state, 'held_until_' + str(duty1))
        if not run_ticks:
            unmet.append('no journaled run-contact stop to bound the '
                         'holdout against')
        elif not isinstance(held_until, int) \
                or held_until < run_ticks[-1] + 1:
            unmet.append('the banked holdout ' + json.dumps(held_until)
                         + ' does not extend past the journaled stop '
                         + json.dumps(run_ticks[-1]))
        if not isinstance(_state_value(state, 'run_hours_'
                                       + str(duty1)), int) \
                or _state_value(state, 'run_hours_' + str(duty1)) <= 0:
            unmet.append('no run-hours banked for pump ' + str(duty1))
        if unmet:
            return case.finish('failed', 'rotation-failed: '
                               + '; '.join(unmet))
        carried_duty = _state_value(state, 'duty')
        carried_cursor = _state_value(state, 'rotation_cursor')
        case.observe('post-stop state: duty=' + str(carried_duty)
                     + ' rotation_cursor=' + str(carried_cursor)
                     + ' held_until_' + str(duty1) + '='
                     + str(held_until) + ' run_hours_' + str(duty1)
                     + '=' + str(_state_value(state, 'run_hours_'
                                              + str(duty1))))

        # Leg 3 — the second cycle starts on the rotated duty: the
        # other pump stages and commands.
        def staged_two(snap):
            return _point_value(snap, points['staged']) == 1 \
                and _point_value(snap, points['duty']) == duty2 \
                and _point_value(snap, cmd_point(duty2)) is True

        probe, last = watch(staged_two)
        second = wait_for(probe,
                          time.monotonic() + ROTATION_CYCLE_DEADLINE,
                          interval=ROTATION_POLL)
        ref = save_evidence(ctx['evidence_dir'],
                            'duty-rotation-cycle2-start.json',
                            second or last.get('snap') or {})
        case.evidence('file', ref, 'the second cycle\'s rotated duty '
                      'holder')
        if not second:
            return case.finish('failed',
                               'rotation-failed: the second cycle '
                               'never staged the rotated duty pump '
                               + str(duty2))
        case.observe('second cycle: duty=' + str(duty2)
                     + ' staged 1 — the rotation alternated')

        # Leg 4 — the mid-cycle demote/promote while the rotated
        # duty's in-flight command stands: the promoted peer must name
        # the same rotation position the checkpoint carried, with the
        # in-flight command and banked run-hours intact.
        status, body = http_json('POST', live + '/demote')
        case.observe('demote ' + active + ': ' + str(status) + ' '
                     + json.dumps(body))
        if status != 200:
            return case.finish('failed', 'the mid-cycle demote was '
                               'refused: ' + str(body))
        restored = False
        promoted = None
        deadline = time.monotonic() + ROTATION_SWITCH_DEADLINE
        while time.monotonic() < deadline and promoted is None:
            try:
                status, body = http_json('POST', peer_base + '/promote')
                if status == 200:
                    promoted = body
                else:
                    time.sleep(ROTATION_POLL)
            except urllib.error.HTTPError as exc:
                if exc.code == 409:
                    time.sleep(ROTATION_POLL)
                else:
                    raise
        settled_role = wait_for(
            lambda: (r.get('role') == 'active' and r or None)
            if (r := _role(ctx, peer_base)) else None,
            time.monotonic() + ROTATION_SWITCH_DEADLINE,
            interval=ROTATION_POLL)
        ref = save_evidence(ctx['evidence_dir'],
                            'duty-rotation-promotion.json',
                            {'demoted': active, 'promote': promoted,
                             'role': settled_role})
        case.evidence('file', ref, 'the mid-cycle demote/promote '
                      'responses')
        if promoted is None or not settled_role:
            return case.finish('failed', 'the mid-cycle promotion '
                               'never settled')
        live = peer_base
        case.observe(peer + ' promoted mid-cycle; checking the '
                     'carried rotation position')

        carried_state = wait_for(served_state,
                                 time.monotonic() + ROTATION_DEADLINE,
                                 interval=ROTATION_POLL)
        carried = observed.get('checkpoint') or {}
        component = observed.get('component')
        if carried_state is None:
            return case.finish('inconclusive', 'the promoted peer '
                               'serves no checkpointed pump-group '
                               'state')
        ref = save_evidence(ctx['evidence_dir'],
                            'duty-rotation-carried.json',
                            {'component': component,
                             'tick': carried.get('tick'),
                             'state': carried_state})
        case.evidence('file', ref, 'the promoted peer\'s checkpointed '
                      'pump-group state')
        unmet = []
        if _state_value(carried_state, 'duty') != duty2:
            unmet.append('duty reset to ' + json.dumps(
                _state_value(carried_state, 'duty')) + ' — the '
                'checkpoint carried ' + str(duty2))
        if _state_value(carried_state, 'rotation_cursor') \
                != carried_cursor:
            unmet.append('rotation_cursor '
                         + json.dumps(_state_value(
                             carried_state, 'rotation_cursor'))
                         + ' != the carried '
                         + json.dumps(carried_cursor))
        if _state_value(carried_state, 'commanded_' + str(duty2)) \
                is not True:
            unmet.append('the in-flight command on pump '
                         + str(duty2) + ' did not carry')
        if not isinstance(_state_value(carried_state,
                                       'run_hours_' + str(duty1)),
                          int) \
                or _state_value(carried_state,
                                'run_hours_' + str(duty1)) <= 0:
            unmet.append('the banked run-hours for pump '
                         + str(duty1) + ' did not carry')
        if unmet:
            return case.finish('failed', 'rotation-failed: the '
                               'promoted peer lost the rotation '
                               'position: ' + '; '.join(unmet))
        case.observe('the promoted peer names duty='
                     + str(_state_value(carried_state, 'duty'))
                     + ' rotation_cursor='
                     + str(_state_value(carried_state,
                                        'rotation_cursor'))
                     + ' — the rotation position carried')

        # Run-hours keep accruing on the promoted peer: two captures
        # bracket the resumed drain.
        hours0 = _state_value(carried_state, 'run_hours_' + str(duty2))

        def accruing():
            checkpoint = _checkpoint_at(ctx, live)
            if checkpoint is None:
                return None
            _, later = _rotation_component(checkpoint)
            observed['later'] = later
            value = _state_value(later, 'run_hours_' + str(duty2))
            return later if isinstance(value, int) \
                and value > (hours0 or 0) else None

        grown_hours = wait_for(accruing,
                               time.monotonic()
                               + ROTATION_CYCLE_DEADLINE,
                               interval=ROTATION_POLL)
        ref = save_evidence(ctx['evidence_dir'],
                            'duty-rotation-runhours.json',
                            {'pump': duty2, 'at_promotion': hours0,
                             'later': grown_hours or {}})
        case.evidence('file', ref, 'run-hours accruing on the '
                      'promoted peer')
        if not grown_hours:
            return case.finish('failed', 'rotation-failed: run-hours '
                               'for pump ' + str(duty2)
                               + ' never accrued past '
                               + json.dumps(hours0)
                               + ' on the promoted peer')
        case.observe('run_hours_' + str(duty2) + ': '
                     + str(hours0) + ' -> '
                     + str(_state_value(grown_hours,
                                        'run_hours_' + str(duty2))))

        # The second cycle completes on the promoted peer: the holder
        # stops at `stop` and duty rotates back.
        def ended_two(snap):
            return _point_value(snap, points['staged']) == 0 \
                and _point_value(snap, cmd_point(duty2)) is not True \
                and _point_value(snap, points['duty']) == duty1

        probe, last = watch(ended_two)
        second_end = wait_for(probe,
                              time.monotonic()
                              + ROTATION_CYCLE_DEADLINE,
                              interval=ROTATION_POLL)
        ref = save_evidence(ctx['evidence_dir'],
                            'duty-rotation-cycle2-end.json',
                            second_end or last.get('snap') or {})
        case.evidence('file', ref, 'the second cycle\'s stop and '
                      'return rotation')
        if not second_end:
            return case.finish('failed', 'rotation-failed: the second '
                               'cycle never ended or duty never '
                               'rotated back to pump ' + str(duty1))
        case.observe('second cycle ended: duty rotated back to '
                     + str(duty1))

        # Restore: the none-available annunciation the hold latched is
        # acknowledged through the receipted path, then the pair fails
        # back to its entry role assignment.
        snap = _try_snapshot(ctx, live) or {}
        if _point_value(snap, points['unack']) is True:
            if not write(live, points['ack'], True):
                return case.finish('failed', 'the none-available ack '
                                   'write was refused: '
                                   + json.dumps(submitted[-1])[:300])
            ack_held = points['ack']

            def cleared(snap_):
                return _point_value(snap_, points['unack']) is not True

            probe, last = watch(cleared)
            if not wait_for(probe,
                            time.monotonic() + ROTATION_DEADLINE,
                            interval=ROTATION_POLL):
                return case.finish('failed', 'the none-available '
                                   'unacknowledged latch never '
                                   'cleared under the ack')
            if not write(live, points['ack'], False):
                return case.finish('failed', 'the ack restore write '
                                   'was refused: '
                                   + json.dumps(submitted[-1])[:300])
            ack_held = None
            case.observe('the hold\'s none-available annunciation '
                         'acknowledged and restored')

        status, body = http_json('POST', live + '/demote')
        case.observe('restore demote ' + peer + ': ' + str(status)
                     + ' ' + json.dumps(body))
        if status != 200:
            return case.finish('failed', 'the restore demote was '
                               'refused: ' + str(body))
        restored_promote = None
        deadline = time.monotonic() + ROTATION_SWITCH_DEADLINE
        while time.monotonic() < deadline and restored_promote is None:
            try:
                status, body = http_json('POST', base + '/promote')
                if status == 200:
                    restored_promote = body
                else:
                    time.sleep(ROTATION_POLL)
            except urllib.error.HTTPError as exc:
                if exc.code == 409:
                    time.sleep(ROTATION_POLL)
                else:
                    raise
        settled_back = wait_for(
            lambda: (r.get('role') == 'active' and r or None)
            if (r := _role(ctx, base)) else None,
            time.monotonic() + ROTATION_SWITCH_DEADLINE,
            interval=ROTATION_POLL)
        peer_back = wait_for(
            lambda: (r.get('role') == 'standby'
                     and 'tracking' in (r.get('sync') or {})
                     and r or None)
            if (r := _role(ctx, peer_base)) else None,
            time.monotonic() + ROTATION_SWITCH_DEADLINE,
            interval=ROTATION_POLL)
        ref = save_evidence(ctx['evidence_dir'],
                            'duty-rotation-restored.json',
                            {'demoted': peer,
                             'promote': restored_promote,
                             'role': settled_back, 'peer': peer_back})
        case.evidence('file', ref, 'the fail-back responses and the '
                      'restored roles')
        if restored_promote is None or not settled_back \
                or not peer_back:
            return case.finish('failed', 'the pair is not restored '
                               'to its pre-scenario role assignment')
        restored = True
        live = base
        case.observe('restored: ' + active + ' active again, ' + peer
                     + ' tracking')

        # The journaled audit: every commanded transition settled
        # through the receipted path, and the declared-journaled
        # points recorded the hold, the stops, and the switches.
        audited = {}
        for key, address in ((active, base), (peer, peer_base)):
            try:
                _, journal = http_json('GET', address
                                       + '/journal?since='
                                       + str(floors[key]))
            except Exception:
                journal = []
            audited[key] = journal
            ref = save_evidence(ctx['evidence_dir'],
                                'duty-rotation-journal-' + key
                                + '.json', journal)
            case.evidence('file', ref, 'the ' + key + ' peer\'s '
                          'journal since the leg opened')
        missing = []
        settled = {key: _settled_receipts(journal)
                   for key, journal in audited.items()}
        for item in submitted:
            key = active if item['base'] == base else peer
            receipt = next(
                (entry for entry in settled[key]
                 if entry.get('command') == item['command']), None)
            if receipt is None:
                missing.append('no settled receipt journaled for '
                               + json.dumps(item['command'])[:200])
            elif receipt.get('actor') != ROTATION_ACTOR:
                missing.append('a settled receipt lost its actor: '
                               + json.dumps(receipt)[:200])
            elif 'applied' not in (receipt.get('outcome') or {}):
                missing.append('a settled receipt did not apply: '
                               + json.dumps(receipt)[:200])
        active_changes = audited[active]
        for point in (points['oos1'], points['oos2']):
            changes = _point_changes(active_changes, point)
            if {'bool': True} not in [to for _, to in changes] \
                    or {'bool': False} not in [to for _, to in changes]:
                missing.append('the oos hold/release on point '
                               + str(point) + ' never journaled')
        if {'bool': True} not in [to for _, to in _point_changes(
                active_changes, points['none_available'])]:
            missing.append('the none-available assertion never '
                           'journaled')
        if {'bool': False} not in [to for _, to in _point_changes(
                active_changes, run_point(duty1))]:
            missing.append('the first holder\'s run stop never '
                           'journaled')
        role_marks = [(entry.get('event') or {}).get('role_changed')
                      for entry in _journal_list(active_changes)]
        if not any(mark for mark in role_marks):
            missing.append('the demotion never journaled on the '
                           'active peer')
        peer_changes = audited[peer]
        peer_marks = [(entry.get('event') or {}).get('role_changed')
                      for entry in _journal_list(peer_changes)]
        if not any(mark for mark in peer_marks):
            missing.append('the promotion never journaled on the '
                           'promoted peer')
        if {'bool': False} not in [to for _, to in _point_changes(
                peer_changes, run_point(duty2))]:
            missing.append('the second holder\'s run stop never '
                           'journaled on the promoted peer')
        if missing:
            return case.finish('failed', 'journaled evidence missing: '
                               + '; '.join(missing))
        case.observe('journaled: oos holds, run stops, role changes, '
                     'and every settled receipt across both peers')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
    finally:
        # The held points are the run's shared field and the roles the
        # pair's standing assignment: a case that leaves either
        # standing poisons every later leg.
        live_base = None
        try:
            settled = _settled_active(ctx)
            if settled is not None:
                live_base = ctx[settled]
        except Exception:
            pass
        for point in held:
            try:
                if live_base is not None:
                    http_json('POST', live_base + '/command',
                              {'command': {'write_value': {
                                  'point': point, 'kind': 'bool',
                                  'value': {'bool': False}}},
                               'actor': ROTATION_ACTOR})
            except Exception:
                pass
        if ack_held is not None and live_base is not None:
            try:
                http_json('POST', live_base + '/command',
                          {'command': {'write_value': {
                              'point': ack_held, 'kind': 'bool',
                              'value': {'bool': False}}},
                           'actor': ROTATION_ACTOR})
            except Exception:
                pass
        if not restored:
            try:
                if live_base is not None and live_base != base:
                    http_json('POST', live_base + '/demote')
                http_json('POST', base + '/promote')
            except Exception:
                pass
