"""The manual_takeover acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *


# --------------------------------------------------------------------
# Per-pump manual takeover and the return to auto (WW-OPS-001,
# WW-CTL-002 — decision 42's manual mode on the deployed station).
# The composition wires the takeover seam outright: the writable
# `mode`/`hand`/`oos` points select between the pump group's `cmd_i`
# and the operator's hand demand through `motor.cmd = ((group-cmd and
# auto) or (hand and mode and the held protection set)) and
# protections-ok`, guarded by in-service and the declared
# thermal/moisture/power/dry-run permissives — the operator's `hand`
# request stays a demand the declared protections bound, not a
# bypass. The rig's operator-command and force legs cover generic
# writes and the handover leg covers failures, but no leg drives the
# deliberate per-pump manual control.
#
# With the pair settled and the group holding a duty demand, the leg
# writes `mode` through the receipted path: the pump leaves the
# group's `cmd_i` (`group-cmd`/`auto` report the manual leg), the
# standing group demand hands to the sibling, and the delivered
# command releases. `hand` then runs the pump on the operator demand
# while the declared guards still stand. An injected
# bad:device_fault on the thermal contact — the asserted *or*
# untrusted clause the interlock trips on — defeats the hand command
# while `mode`/`hand` stand, with the managed thermal alarm
# annunciating `alarm`/`unacknowledged`; clearing the contact re-arms
# the command through the declared holdout. `oos` then drops the
# interlock's permissive — the maintenance inhibit removes the pump
# from both manual and group paths and reports its managed state on
# the fault alarm's declared `oos`/`suppress` bindings. Restoring
# `hand`/`mode`/`oos` returns the pump to group control with `cmd`
# following `group-cmd` exactly — no unintended output step. Every
# write settles with attribution, the durable journal carries the
# lifecycle entries in order, and no peer reports a role change.
# Named diagnostics: takeover-failed for a broken selection, guard,
# inhibit, or restore clause; takeover-nondeterministic when the
# served surfaces cannot drive or record a deterministic leg.

TAKEOVER_DEADLINE = 30      # bound on each leg's served transition
TAKEOVER_CYCLE_DEADLINE = 60  # bound on the commanded-duty baseline
TAKEOVER_POLL = 0.5         # observation cadence
TAKEOVER_ACTOR = 'qa-lane'


def scenario_manual_takeover(ctx):
    """A receipted `mode` write removes the commanded pump from group
    demand and hands control to the writable `hand` point; the
    declared protections still gate the manual command and the
    managed thermal alarm annunciates; `oos` inhibits both paths and
    reports its managed state; the restore writes return the pump to
    group control with `cmd` following `group-cmd` exactly."""
    case = Case('manual-takeover',
                'Per-pump manual takeover and return to auto',
                'with the deployed pair settled and tracking and the '
                'station holding a duty pump commanded, an attributed '
                'receipted write on the pump\'s mode point drops the '
                'auto leg, releases the delivered command, and hands '
                'the standing group demand to the sibling — the '
                'group-cmd/auto carriers reporting the manual leg; '
                'the hand write runs the pump on the operator demand '
                'through the held protections; an injected '
                'bad:device_fault on the thermal contact trips the '
                'interlock and defeats the command while mode/hand '
                'stand, with the managed thermal alarm annunciating '
                'alarm/unacknowledged and journaled point_changed '
                'evidence; clearing the contact re-arms the command '
                'through the declared holdout; the oos write drops '
                'the permissive — inhibiting the manual path and '
                'reporting the fault alarm\'s declared oos/suppress '
                'states; restoring hand/mode/oos returns the pump to '
                'group control with cmd following group-cmd exactly '
                'and the latch held for the receipted ack; every '
                'submission settles through the attributed receipted '
                'path and the pair\'s roles never move')
    injected = None       # the thermal point while it faults
    restore_writes = []   # (key, point) while writes stand
    try:
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + TAKEOVER_DEADLINE)
        if active is None:
            return case.finish('failed', 'no peer reports '
                               'role=active')
        base = ctx[active]
        tracking = wait_for(lambda: _tracking_peer(ctx, active),
                            time.monotonic() + TAKEOVER_DEADLINE,
                            interval=POLL_INTERVAL)
        if tracking is None:
            return case.finish('inconclusive',
                               'no tracking peer — the deployed pair '
                               'never settled')
        case.observe('settled pair: ' + active + ' active, '
                     + tracking + ' tracking')

        _, signals = http_json('GET', base + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'manual-takeover-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming the '
                      'manual-takeover leg\'s wiring')
        names = {'demand-in': 'demand_in', 'duty': 'duty',
                 'staged': 'staged', 'below-cutoff': 'below_cutoff'}
        for index, tag in ((1, 'p101'), (2, 'p102')):
            for suffix, key in (
                    ('mode', 'mode%d' % index),
                    ('hand', 'hand%d' % index),
                    ('oos', 'oos%d' % index),
                    ('group-cmd', 'group_cmd%d' % index),
                    ('auto', 'auto%d' % index),
                    ('avail', 'avail%d' % index),
                    ('cmd', 'cmd%d' % index),
                    ('run', 'run%d' % index),
                    ('thermal', 'thermal%d' % index),
                    ('thermal-ok', 'thermal_ok%d' % index),
                    ('moisture-ok', 'moisture_ok%d' % index),
                    ('protect-tripped', 'protect_tripped%d' % index),
                    ('protections-ok', 'protections_ok%d' % index),
                    ('fault', 'fault%d' % index),
                    ('fault-out-of-service',
                     'fault_%d_out_of_service' % index),
                    ('fault-suppressed',
                     'fault_%d_suppressed' % index),
                    ('thermal-ack', 'thermal_%d_ack' % index),
                    ('thermal-alarm', 'thermal_%d_alarm' % index),
                    ('thermal-unacknowledged',
                     'thermal_%d_unacknowledged' % index)):
                names[tag + '-' + suffix] = key
        entries = {entry.get('name'): entry
                   for entry in signals.get('points', [])
                   if entry.get('name') in names}
        missing = sorted(set(names) - set(entries))
        if missing:
            return case.finish('inconclusive', 'the deployed model '
                               'lacks the manual-takeover leg\'s '
                               'wiring — no signals '
                               + ', '.join(missing))
        points = {names[name]: entry.get('point')
                  for name, entry in entries.items()}
        for index in (1, 2):
            for key in ('mode%d' % index, 'hand%d' % index,
                        'oos%d' % index, 'thermal_%d_ack' % index):
                point = points[key]
                entry = next(entry for entry in entries.values()
                             if entry.get('point') == point)
                if not (entry.get('writable')
                        and entry.get('direction') == 'in'
                        and entry.get('value_type') == 'bool'):
                    return case.finish(
                        'inconclusive', 'the ' + key + ' point is not '
                        'the writable bool input the leg needs: '
                        + json.dumps(entry)[:300])
        case.observe('takeover path: '
                     + json.dumps({name: entry.get('point')
                                   for name, entry in
                                   sorted(entries.items())},
                                  sort_keys=True))

        if ctx.get('plant_ctl') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no plant_ctl seam for the '
                               'protection leg')

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
            reported a sample, takeover-failed when the served values
            never landed the leg."""
            hit = wait_for(lambda: poll(cond, keys),
                           time.monotonic()
                           + (deadline or TAKEOVER_DEADLINE),
                           interval=TAKEOVER_POLL)
            snap = last.get('snap') or {}
            ref_ = save_evidence(
                ctx['evidence_dir'], 'manual-takeover-' + name
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
                    'inconclusive', 'takeover-nondeterministic: the '
                    + name + ' leg\'s outputs never reported on the '
                    'served snapshot: ' + ', '.join(unreported))
            return None, case.finish(
                'failed', failed + '; last served ' + json.dumps(
                    {key: value(key, snap) for key in sorted(keys)
                     if key in points}, sort_keys=True)[:500])

        # Leg 1 — the commanded-duty baseline: the level loop holds a
        # duty pump commanded and proven running, both pumps clean.
        def commanded(snap):
            duty = value('duty', snap)
            if duty not in (1, 2):
                return None
            if value('cmd%d' % duty, snap) is not True \
                    or value('run%d' % duty, snap) is not True:
                return None
            for index in (1, 2):
                for key in ('mode%d' % index, 'hand%d' % index,
                            'oos%d' % index, 'fault%d' % index,
                            'protect_tripped%d' % index,
                            'thermal_%d_alarm' % index,
                            'thermal_%d_unacknowledged' % index):
                    if value(key, snap) is not False:
                        return None
                if value('avail%d' % index, snap) is not True \
                        or value('protections_ok%d' % index, snap) \
                        is not True:
                    return None
            return snap

        baseline, error = leg(
            'baseline', commanded,
            ['demand_in', 'duty', 'staged'] + [
                prefix + str(index)
                for index in (1, 2)
                for prefix in ('cmd', 'run', 'fault', 'avail', 'mode',
                               'hand', 'oos', 'protect_tripped',
                               'protections_ok')] + [
                'thermal_%d_alarm' % index
                for index in (1, 2)] + [
                'thermal_%d_unacknowledged' % index
                for index in (1, 2)],
            'takeover-failed: the pair never held the commanded-duty '
            'baseline the leg needs',
            deadline=TAKEOVER_CYCLE_DEADLINE)
        if error:
            return error
        held = value('duty', baseline)
        sibling = 3 - held
        case.observe('commanded baseline: duty=p' + str(100 + held)
                     + ' — the mode write targets the duty holder')

        # The journal floor ahead of the leg: this case's records are
        # the ones above it.
        _, journal0 = http_json('GET', base + '/journal?since=0')
        floor = max((entry.get('seq') or 0
                     for entry in _journal_list(journal0)
                     if isinstance(entry, dict)), default=0)

        submitted = []

        def write(key, flag):
            command = {'write_value': {'point': points[key],
                                       'kind': 'bool',
                                       'value': {'bool': flag}}}
            status, receipt = http_json(
                'POST', base + '/command',
                {'command': command, 'actor': TAKEOVER_ACTOR})
            submitted.append({'command': command, 'status': status,
                              'receipt': receipt})
            outcome = (receipt or {}).get('outcome') or {}
            if status == 200 and 'rejected' not in outcome:
                restore_writes[:] = [
                    item for item in restore_writes
                    if item[0] != key]
                if flag:
                    restore_writes.append((key, points[key]))
                return True
            return False

        # Leg 2 — the receipted mode write: the pump leaves the
        # group's cmd_i — auto drops, the delivered command releases,
        # the availability aggregate reports the exclusion, and the
        # standing group demand hands to the sibling inside the
        # declared bound.
        if not write('mode%d' % held, True):
            return case.finish('failed', 'takeover-failed: the mode '
                               'write on point '
                               + str(points['mode%d' % held])
                               + ' was refused: '
                               + json.dumps(submitted[-1])[:300])
        case.observe('mode held on p' + str(100 + held) + ' point '
                     + str(points['mode%d' % held]))

        def manual(snap):
            demand = value('demand_in', snap)
            if not isinstance(demand, int) or isinstance(demand, bool):
                return None
            if value('mode%d' % held, snap) is not True \
                    or value('auto%d' % held, snap) is not False \
                    or value('group_cmd%d' % held, snap) is not False \
                    or value('cmd%d' % held, snap) is not False \
                    or value('avail%d' % held, snap) is not False \
                    or value('duty', snap) != sibling \
                    or value('cmd%d' % sibling, snap) is not True:
                return None
            return snap

        hit, error = leg(
            'manual', manual,
            ['mode%d' % held, 'auto%d' % held, 'group_cmd%d' % held,
             'cmd%d' % held, 'avail%d' % held, 'duty', 'staged',
             'cmd%d' % sibling, 'demand_in'],
            'takeover-failed: the mode write never landed the manual '
            'leg — the auto drop, the command release, or the '
            'sibling\'s demand takeover missing')
        if error:
            return error
        case.observe('manual leg: p' + str(100 + held)
                     + ' off the group\'s cmd_i, duty=p'
                     + str(100 + sibling) + ' serving at tick '
                     + str(hit.get('tick')))

        # Leg 3 — the operator demand: hand runs the pump through the
        # held protections while the exclusion stands.
        if not write('hand%d' % held, True):
            return case.finish('failed', 'takeover-failed: the hand '
                               'write on point '
                               + str(points['hand%d' % held])
                               + ' was refused: '
                               + json.dumps(submitted[-1])[:300])

        def running(snap):
            if value('cmd%d' % held, snap) is not True \
                    or value('run%d' % held, snap) is not True \
                    or value('thermal_ok%d' % held, snap) is not True \
                    or value('moisture_ok%d' % held, snap) is not True \
                    or value('protections_ok%d' % held, snap) \
                    is not True \
                    or value('avail%d' % held, snap) is not False:
                return None
            return snap

        hit, error = leg(
            'running', running,
            ['cmd%d' % held, 'run%d' % held, 'thermal_ok%d' % held,
             'moisture_ok%d' % held, 'protections_ok%d' % held,
             'avail%d' % held, 'duty'],
            'takeover-failed: the receipted hand write never ran the '
            'pump — the delivered command or the run feedback never '
            'asserted on the operator demand')
        if error:
            return error
        case.observe('hand demand: p' + str(100 + held)
                     + ' commanding with run feedback at tick '
                     + str(hit.get('tick'))
                     + ' — the declared guards standing')

        # Leg 4 — the protection layer: the thermal contact reads
        # untrusted, the interlock trips, the delivered command
        # releases while mode/hand stand, and the managed thermal
        # alarm annunciates through the two-flag lifecycle.
        verdict = _plant_ctl(ctx, 'fault',
                             str(points['thermal%d' % held]),
                             'bad:device_fault')
        if verdict.get('result') != 'done':
            return case.finish('failed', 'takeover-failed: '
                               'inject_fault on the thermal point '
                               + str(points['thermal%d' % held])
                               + ' refused: '
                               + json.dumps(verdict)[:300])
        injected = points['thermal%d' % held]
        case.observe('bad:device_fault injected on p'
                     + str(100 + held) + ' thermal point '
                     + str(injected))

        def tripped(snap):
            # `below_cutoff` must stand clear so the trip the leg
            # observes is the untrusted contact's, not the dry-run
            # clause's — the cause the injection names.
            if value('protect_tripped%d' % held, snap) is not True \
                    or value('protections_ok%d' % held, snap) \
                    is not False \
                    or value('below_cutoff', snap) is not False \
                    or value('cmd%d' % held, snap) is not False \
                    or value('mode%d' % held, snap) is not True \
                    or value('hand%d' % held, snap) is not True \
                    or value('thermal_%d_alarm' % held, snap) \
                    is not True \
                    or value('thermal_%d_unacknowledged' % held, snap) \
                    is not True:
                return None
            return snap

        hit, error = leg(
            'tripped', tripped,
            ['protect_tripped%d' % held, 'protections_ok%d' % held,
             'below_cutoff', 'cmd%d' % held, 'mode%d' % held,
             'hand%d' % held, 'thermal_%d_alarm' % held,
             'thermal_%d_unacknowledged' % held],
            'takeover-failed: the untrusted thermal contact never '
            'defeated the hand command — the trip, the release, or '
            'the managed annunciation missing')
        if error:
            return error
        case.observe('protection: the untrusted contact tripped the '
                     'interlock at tick ' + str(hit.get('tick'))
                     + ' — the hand demand defeated with the managed '
                     'alarm standing')
        if _settled_active(ctx) != active:
            return case.finish('failed', 'takeover-failed: the '
                               'active role moved under a field '
                               'fault — a plant-side injection is '
                               'not peer loss')

        # Clearing the contact re-arms the command through the
        # declared min_off_ticks holdout the hand leg carries.
        verdict = _plant_ctl(ctx, 'clear-fault', str(injected))
        if verdict.get('result') != 'done':
            return case.finish('failed', 'takeover-failed: '
                               'clear_fault on the thermal channel '
                               'refused: '
                               + json.dumps(verdict)[:300])
        injected = None

        hit, error = leg(
            'rearmed', running,
            ['cmd%d' % held, 'run%d' % held,
             'protect_tripped%d' % held, 'protections_ok%d' % held,
             'thermal_ok%d' % held, 'moisture_ok%d' % held,
             'avail%d' % held],
            'takeover-failed: the cleared contact never re-armed the '
            'hand command through the declared holdout')
        if error:
            return error
        case.observe('re-armed: protections stood and the hand '
                     'command re-asserted at tick '
                     + str(hit.get('tick')))

        # Leg 5 — the maintenance inhibit: the oos write drops the
        # interlock's permissive — the manual path releases and the
        # fault alarm reports its declared out-of-service/suppressed
        # states.
        if not write('oos%d' % held, True):
            return case.finish('failed', 'takeover-failed: the oos '
                               'write on point '
                               + str(points['oos%d' % held])
                               + ' was refused: '
                               + json.dumps(submitted[-1])[:300])

        def inhibited(snap):
            # As in `tripped`: `below_cutoff` must stand clear so the
            # trip is the in-service permissive's, not the dry-run
            # clause's.
            if value('oos%d' % held, snap) is not True \
                    or value('protect_tripped%d' % held, snap) \
                    is not True \
                    or value('below_cutoff', snap) is not False \
                    or value('cmd%d' % held, snap) is not False \
                    or value('avail%d' % held, snap) is not False \
                    or value('fault_%d_out_of_service' % held, snap) \
                    is not True \
                    or value('fault_%d_suppressed' % held, snap) \
                    is not True:
                return None
            return snap

        hit, error = leg(
            'inhibited', inhibited,
            ['oos%d' % held, 'protect_tripped%d' % held,
             'below_cutoff', 'cmd%d' % held, 'avail%d' % held,
             'fault_%d_out_of_service' % held,
             'fault_%d_suppressed' % held],
            'takeover-failed: the oos write never inhibited the pump '
            '— the permissive drop, the command release, or the '
            'managed out-of-service/suppressed states missing')
        if error:
            return error
        case.observe('inhibited: p' + str(100 + held)
                     + ' off both paths at tick '
                     + str(hit.get('tick'))
                     + ' — the managed surface reporting the hold')

        # Leg 6 — the return to auto: hand first, then mode, then oos
        # — the auto leg rejoins, avail re-proves, and the delivered
        # command follows the group's request exactly, never an
        # unintended output step.
        for key in ('hand%d' % held, 'mode%d' % held, 'oos%d' % held):
            if not write(key, False):
                return case.finish('failed', 'takeover-failed: the '
                                   + key + ' restore write was '
                                   'refused: '
                                   + json.dumps(submitted[-1])[:300])

        def rejoined(snap):
            command = value('cmd%d' % held, snap)
            group = value('group_cmd%d' % held, snap)
            if command is None or group is None or command != group:
                return None
            for key, want in (
                    ('mode%d' % held, False), ('hand%d' % held, False),
                    ('oos%d' % held, False), ('auto%d' % held, True),
                    ('avail%d' % held, True),
                    ('protections_ok%d' % held, True),
                    ('fault_%d_out_of_service' % held, False),
                    ('fault_%d_suppressed' % held, False),
                    ('thermal_%d_alarm' % held, False)):
                if value(key, snap) is not want:
                    return None
            if value('thermal_%d_unacknowledged' % held, snap) \
                    is not True:
                return None
            return snap

        hit, error = leg(
            'rejoined', rejoined,
            ['cmd%d' % held, 'group_cmd%d' % held, 'mode%d' % held,
             'hand%d' % held, 'oos%d' % held, 'auto%d' % held,
             'avail%d' % held, 'protections_ok%d' % held,
             'fault_%d_out_of_service' % held,
             'fault_%d_suppressed' % held,
             'thermal_%d_alarm' % held,
             'thermal_%d_unacknowledged' % held],
            'takeover-failed: the return to auto never landed — the '
            'rejoin, the protections restore, or the cmd==group-cmd '
            'no-step contract missing')
        if error:
            return error
        case.observe('returned to group control at tick '
                     + str(hit.get('tick'))
                     + ' — cmd follows group-cmd, no unintended '
                     'output step')

        # The thermal latch's lifecycle close: the receipted ack
        # clears the standing unacknowledged, then the ack input
        # re-arms for the next trip.
        if not write('thermal_%d_ack' % held, True):
            return case.finish('failed', 'takeover-failed: the '
                               'thermal-ack write was refused: '
                               + json.dumps(submitted[-1])[:300])

        hit, error = leg(
            'acknowledged',
            lambda s: value('thermal_%d_unacknowledged' % held, s)
            is False,
            ['thermal_%d_ack' % held,
             'thermal_%d_unacknowledged' % held],
            'takeover-failed: the receipted thermal ack never '
            'cleared the unacknowledged latch')
        if error:
            return error
        if not write('thermal_%d_ack' % held, False):
            return case.finish('failed', 'takeover-failed: the '
                               'thermal-ack restore write was '
                               'refused: '
                               + json.dumps(submitted[-1])[:300])
        case.observe('the receipted ack settled the thermal latch '
                     'and re-armed at tick ' + str(hit.get('tick')))

        if _settled_active(ctx) != active \
                or _tracking_peer(ctx, active) != tracking:
            return case.finish('failed', 'takeover-failed: the '
                               'pair\'s roles moved during the leg')

        # The durable record: each declared-journaled point records
        # the leg's transitions in order — the mode/oos holds and
        # releases, the two protection trips and clears, the
        # availability drop and rejoin, the managed flags — and every
        # submission settles applied through the receipted path
        # attributed to the lane actor.
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
            elif receipt.get('actor') != TAKEOVER_ACTOR:
                missing.append('a settled receipt lost its actor: '
                               + json.dumps(receipt)[:200])
            elif 'applied' not in (receipt.get('outcome') or {}):
                missing.append('a settled receipt did not apply: '
                               + json.dumps(receipt)[:200])
        changes = _journal_point_changes(journal)
        bool_t, bool_f = {'bool': True}, {'bool': False}
        ordered = {
            'mode%d' % held: [bool_t, bool_f],
            'oos%d' % held: [bool_t, bool_f],
            'avail%d' % held: [bool_f, bool_t],
            'protect_tripped%d' % held: [bool_t, bool_f,
                                         bool_t, bool_f],
            'protections_ok%d' % held: [bool_f, bool_t,
                                        bool_f, bool_t],
            'fault_%d_out_of_service' % held: [bool_t, bool_f],
            'fault_%d_suppressed' % held: [bool_t, bool_f],
            'thermal_%d_alarm' % held: [bool_t, bool_f],
            'thermal_%d_unacknowledged' % held: [bool_t, bool_f]}

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
                               'under a manual takeover')
        ref = save_evidence(
            ctx['evidence_dir'], 'manual-takeover-journal.json',
            {'floor': floor, 'receipts': len(settled),
             'missing': missing,
             'transitions': {key: changes.get(points[key], [])
                             for key in sorted(ordered)}})
        case.evidence('file', ref, 'the journaled transitions and '
                      'settled receipts above the floor')
        if missing:
            return case.finish('failed', 'takeover-failed: journaled '
                               'evidence missing: '
                               + '; '.join(missing))
        case.observe('journaled: the mode/oos holds and releases, '
                     'the protection trips and clears, the managed '
                     'flags, and every settled receipt')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
    finally:
        # The injected contact fault and the standing writes are the
        # run's shared state: a case that leaves mode/hand/oos or the
        # ack standing — or the contact untrusted — poisons every
        # later leg. The pair's roles never moved — there is nothing
        # to fail back.
        if injected is not None:
            try:
                _try_plant_ctl(ctx, 'clear-fault', str(injected))
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
            for _key, point in restore_writes:
                try:
                    http_json('POST', live_base + '/command',
                              {'command': {'write_value': {
                                  'point': point, 'kind': 'bool',
                                  'value': {'bool': False}}},
                               'actor': TAKEOVER_ACTOR})
                except Exception:
                    pass
