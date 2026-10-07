"""The managed_state_carryover acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: The managed-state-carryover case drives journaled field
# contacts and the receipted command path under the settled active's
# shared writer claim, switches the pair twice inside the shelve
# bound, and restores every driven input plus the pair's launch roles
# — the run's redundancy state is the assertion, so it needs no
# declared window beyond the settled pair.


# --------------------------------------------------------------------
# Managed-alarm run-state carryover across promotion on the simulated
# rig (WW-ALM-002, WW-LCM-001 — the managed kinds checkpoint their run
# state so a tracking standby continues a mid-shelve countdown
# identically, and WW-LCM-001 names unacknowledged latches among the
# state a takeover must carry). With the pair settled and tracking,
# the leg shelves `lal` mid-run, drives the moisture alarm's condition
# so `unacknowledged` latches, and puts the p101 fault alarm out of
# service through `p101-oos`; demote/promote runs inside the shelve
# bound. On the promoted peer `lal-shelved` still stands and releases
# on the same tick the countdown would have expired — the
# checkpointed timer continued rather than restarted — the
# `unacknowledged` latch and `out_of_service` carry rather than reset,
# and the durable journal's ordered record stays continuous across
# the switch. Every written point and the pair's roles restore.
# Functional misses name carryover-failed; journal-continuity and
# ordering violations name carryover-nondeterministic; a rig whose
# expected surface never reports finishes inconclusive.

CARRY_DEADLINE = 60   # bound on each served transition/settle wait
CARRY_POLL = 0.05     # transition-watch cadence — under the scan
CARRY_ACTOR = 'qa-lane'
CARRY_SLACK = 2       # poll/carrier slack on the expiry-tick proof


def _carry_points(signals):
    """The carryover seam's signal-name to point map, or None when
    the deployed model declares no such surface."""
    names = {'lal-shelve': 'lal_shelve',
             'lal-shelved': 'lal_shelved',
             'p101-moisture': 'contact',
             'p101-moisture-alarm': 'alarm',
             'p101-moisture-unacknowledged': 'unack',
             'p101-moisture-ack': 'ack',
             'p101-oos': 'oos',
             'p101-fault-alarm': 'fault_alarm',
             'p101-fault-unacknowledged': 'fault_unack',
             'p101-fault-out-of-service': 'fault_oos'}
    entries = {entry.get('name'): entry
               for entry in signals.get('points', [])
               if entry.get('name') in names}
    missing = sorted(set(names) - set(entries))
    if missing:
        return None, missing
    return ({key: entries[name].get('point')
             for name, key in names.items()}, [])


def _switch_pair(ctx, case, prefix, deadline=30):
    """Demote the settled active and promote its peer; wait for the
    new active to settle. Returns (new_active_key, None) or
    (None, error_record)."""
    before = wait_for(lambda: _settled_active(ctx),
                      time.monotonic() + deadline)
    if before is None:
        return None, case.finish('failed', 'carryover-failed: no '
                                 'peer reports role=active ahead of '
                                 'the ' + prefix + ' switch')
    peer = 'standby' if before == 'active' else 'active'
    try:
        status, body = http_json('POST', ctx[before] + '/demote')
    except Exception as exc:
        return None, case.finish('failed', 'carryover-failed: the '
                                 + prefix + ' demote errored: '
                                 + str(exc))
    if status != 200:
        return None, case.finish('failed', 'carryover-failed: the '
                                 + prefix + ' demote refused: '
                                 + json.dumps(body)[:300])
    promoted, end = None, time.monotonic() + deadline
    while time.monotonic() < end and promoted is None:
        try:
            status, body = http_json('POST', ctx[peer] + '/promote')
            if status == 200:
                promoted = body
            else:
                time.sleep(POLL_INTERVAL)
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                time.sleep(POLL_INTERVAL)
            else:
                raise
    ref = save_evidence(ctx['evidence_dir'],
                        'carry-' + prefix + '-promote.json',
                        promoted or {'refused': True})
    case.evidence('file', ref, 'the ' + prefix + ' promote response')
    if promoted is None:
        return None, case.finish('failed', 'carryover-failed: the '
                                 + prefix + ' promote never '
                                 'succeeded within 30s')
    settled = wait_for(lambda: _settled_active(ctx),
                       time.monotonic() + deadline)
    if settled != peer:
        return None, case.finish('failed', 'carryover-failed: the '
                                 + prefix + ' switch never settled '
                                 'on ' + peer + ': ' + str(settled))
    snap = _try_snapshot(ctx, ctx[peer]) or {}
    return peer, None


def scenario_managed_state_carryover(ctx):
    """Shelve lal mid-run, latch a second alarm, take the fault alarm
    out of service, switch inside the bound, and prove the promoted
    peer continues the countdown identically with latch and OOS
    carried and the journal continuous — then restore everything."""
    case = Case('managed-state-carryover',
                'Managed run state carries across promotion',
                'with the deployed pair settled and tracking, a '
                'mid-run lal shelve, a latched moisture alarm, and '
                'the p101 fault alarm out of service ride a '
                'demote/promote inside the shelve bound — the '
                'promoted peer holding shelved until the countdown '
                'would have expired, the unacknowledged latch and '
                'out_of_service carried, the journal continuous — '
                'then every point and role restores')
    stream = None
    base = None
    active = None
    points = None
    restore_contact = None
    try:
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        home = active
        base = ctx[active]
        case.observe('settled pair: ' + active + ' active')

        _, signals = http_json('GET', base + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'carry-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming the seam')
        points, missing = _carry_points(signals)
        if points is None:
            return case.finish('inconclusive', 'the deployed model '
                               'lacks the managed-state seam — no '
                               'signals ' + ', '.join(missing))
        for name in ('lal_shelve', 'ack', 'oos'):
            entry = next(entry for entry in signals.get('points', [])
                         if entry.get('point') == points[name])
            if not entry.get('writable') \
                    or entry.get('direction') != 'in':
                return case.finish('inconclusive', 'the point '
                                   + str(points[name]) + ' is not the '
                                   'writable input the leg needs')
        case.observe('carryover points: ' + json.dumps(points,
                                                        sort_keys=True))

        _, schema = http_json('GET', base + '/schema')
        snap0 = _snapshot(ctx, base)
        lal_name = _component_instance(schema, 'managed-latching-alarm')
        if lal_name is None:
            return case.finish('inconclusive', 'the served schema '
                               'carries no managed-latching-alarm '
                               'instance')
        bound = _parameter_value(snap0, lal_name, 'max_shelve_ticks')
        if not isinstance(bound, int) or isinstance(bound, bool) \
                or bound < 3:
            return case.finish('inconclusive', 'the lal instance '
                               'serves no usable max_shelve_ticks: '
                               + json.dumps(bound))
        case.observe('lal shelve bound: ' + str(bound) + ' scans')

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
        if points['contact'] not in field:
            return case.finish('inconclusive', 'the moisture '
                               'contact\'s point '
                               + str(points['contact'])
                               + ' is not a field in-point the plant '
                               'serves')
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

        def poll_on(url, cond):
            snap = _try_snapshot(ctx, url)
            if snap is None:
                return None
            last['snap'] = snap
            return snap if cond(snap) else None

        def leg(name, cond, keys, url=None):
            hit = wait_for(
                lambda: poll_on(url, cond) if url else poll(cond),
                time.monotonic() + CARRY_DEADLINE,
                interval=CARRY_POLL)
            snap = last.get('snap') or {}
            ref = save_evidence(
                ctx['evidence_dir'], 'carry-' + name + '.json',
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
                'carryover-failed: the ' + name + ' leg never '
                'landed; last served '
                + json.dumps({key: value(key, snap) for key in keys},
                             sort_keys=True)[:500])

        def submit(url, point, val):
            write = {'point': point, 'kind': 'bool',
                     'value': {'bool': val}}
            try:
                status, receipt = http_json(
                    'POST', url + '/command',
                    {'command': {'write_value': write},
                     'actor': CARRY_ACTOR})
            except Exception as exc:
                return None, case.finish(
                    'failed', 'carryover-failed: the write on point '
                    + str(point) + ' errored: ' + str(exc))
            if status != 200 or 'rejected' in (
                    (receipt or {}).get('outcome') or {}):
                return None, case.finish(
                    'failed', 'carryover-failed: the write on point '
                    + str(point) + ' was refused: ' + str(status)
                    + ' ' + json.dumps(receipt)[:300])
            return write, None

        # Phase 1 — the mid-run shelve: the receipted request asserts
        # lal-shelved; its tick anchors the expiry proof.
        _, error = submit(base, points['lal_shelve'], True)
        if error:
            return error
        hit, error = leg('shelved',
                         lambda s: value('lal_shelved', s) is True,
                         ('lal_shelved',))
        if error:
            return error
        shelve_tick = hit.get('tick')
        case.observe('lal shelved at tick ' + str(shelve_tick)
                     + ' — bound ' + str(bound))

        # Phase 2 — the latched second alarm: the field contact
        # driven true under the shared claim.
        verdict = _plant_request(
            stream, {'op': 'write', 'point': points['contact'],
                     'value': {'bool': True}})
        if verdict.get('result') != 'done':
            return case.finish('failed', 'carryover-failed: the '
                               'moisture write was refused: '
                               + json.dumps(verdict)[:300])
        restore_contact = points['contact']
        hit, error = leg(
            'latched',
            lambda s: value('alarm', s) is True
            and value('unack', s) is True,
            ('alarm', 'unack'))
        if error:
            return error
        case.observe('the moisture alarm latched at tick '
                     + str(hit.get('tick')))

        # Phase 3 — out of service: the receipted inhibit asserts the
        # fault alarm's flag.
        _, error = submit(base, points['oos'], True)
        if error:
            return error
        hit, error = leg(
            'oos',
            lambda s: value('fault_oos', s) is True,
            ('fault_oos',))
        if error:
            return error
        case.observe('the fault alarm out of service at tick '
                     + str(hit.get('tick')))

        # Phase 4 — the journal floor ahead of the switch.
        _, journal0 = http_json('GET', base + '/journal?since=0')
        floor = max((entry.get('seq') or 0
                     for entry in _journal_list(journal0)
                     if isinstance(entry, dict)), default=0)

        # Phase 5 — the switch inside the bound.
        active, error = _switch_pair(ctx, case, 'carry')
        if error:
            return error
        base = ctx[active]
        switch_snap = _try_snapshot(ctx, base) or {}
        switch_tick = switch_snap.get('tick')
        if switch_tick is None or switch_tick - shelve_tick >= bound:
            return case.finish(
                'failed', 'carryover-failed: the switch landed at '
                'tick ' + str(switch_tick) + ' — outside the '
                + str(bound) + '-scan shelve bound from '
                + str(shelve_tick))
        case.observe('switched at tick ' + str(switch_tick)
                     + ' — inside the bound')

        # Phase 6 — the carried state: shelved still stands, the latch
        # and the OOS flag rode the checkpoint rather than reset.
        hit, error = leg(
            'carried',
            lambda s: value('lal_shelved', s) is True
            and value('unack', s) is True
            and value('fault_oos', s) is True,
            ('lal_shelved', 'unack', 'fault_oos'), url=base)
        if error:
            return error
        case.observe('the promoted peer holds shelved, the latch, '
                     'and out_of_service at tick '
                     + str(hit.get('tick')))

        # Phase 7 — the journal stays continuous across the switch:
        # gap-free seqs carrying every pre-switch record.
        try:
            _, journal = http_json('GET', base + '/journal?since='
                                   + str(floor))
        except Exception as exc:
            return case.finish(
                'failed', 'carryover-nondeterministic: the promoted '
                'peer\'s journal refused: ' + str(exc))
        seqs = sorted(entry.get('seq') for entry in
                      _journal_list(journal)
                      if isinstance(entry, dict))
        ref = save_evidence(ctx['evidence_dir'],
                            'carry-journal.json',
                            {'floor': floor, 'seqs': seqs})
        case.evidence('file', ref, 'the post-switch journal')
        if not seqs or seqs != list(range(seqs[0], seqs[-1] + 1)):
            return case.finish(
                'failed', 'carryover-nondeterministic: the promoted '
                'peer\'s journal is not gap-free across the switch: '
                + json.dumps(seqs[:12])[:300])
        if seqs[0] > floor + 1:
            return case.finish(
                'failed', 'carryover-nondeterministic: the promoted '
                'peer\'s journal starts at seq ' + str(seqs[0])
                + ' — the pre-switch record from floor '
                + str(floor) + ' did not continue')
        case.observe('the journal runs continuous across the switch')

        # Phase 8 — the identical expiry: shelved releases on the
        # tick the countdown would have expired, not a restarted
        # bound.
        remaining = bound - (switch_tick - shelve_tick)
        hit, error = leg(
            'released',
            lambda s: value('lal_shelved', s) is False,
            ('lal_shelved',), url=base)
        if error:
            return error
        release_tick = hit.get('tick')
        if release_tick - switch_tick > remaining + CARRY_SLACK:
            return case.finish(
                'failed', 'carryover-failed: shelved released at '
                'tick ' + str(release_tick) + ' — '
                + str(release_tick - switch_tick) + ' scans after '
                'the switch with ' + str(remaining) + ' bound scans '
                'left; the checkpointed timer restarted')
        case.observe('shelved released at tick ' + str(release_tick)
                     + ' — the countdown continued')

        # Phase 9 — restore: unshelve, clear the contact, ack the
        # latch, return to service, and switch the roles home.
        _, error = submit(base, points['lal_shelve'], False)
        if error:
            return error
        verdict = _plant_request(
            stream, {'op': 'write', 'point': points['contact'],
                     'value': {'bool': False}})
        restore_contact = None
        _, error = submit(base, points['ack'], True)
        if error:
            return error
        hit, error = leg(
            'acked',
            lambda s: value('unack', s) is False,
            ('unack',), url=base)
        if error:
            return error
        _, error = submit(base, points['ack'], False)
        if error:
            return error
        _, error = submit(base, points['oos'], False)
        if error:
            return error
        hit, error = leg(
            'restored',
            lambda s: value('alarm', s) is False
            and value('fault_oos', s) is False,
            ('alarm', 'fault_oos'), url=base)
        if error:
            return error
        if active != home:
            active, error = _switch_pair(ctx, case, 'restore')
            if error:
                return error
            base = ctx[active]
        if _settled_active(ctx) != home:
            return case.finish('failed', 'carryover-failed: the '
                               'roles did not restore to ' + home)
        case.observe('every point and role restored')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
    finally:
        try:
            if base is not None and points is not None:
                for point in (points['lal_shelve'], points['ack'],
                              points['oos']):
                    try:
                        http_json(
                            'POST', base + '/command',
                            {'command': {'write_value': {
                                'point': point, 'kind': 'bool',
                                'value': {'bool': False}}},
                             'actor': CARRY_ACTOR})
                    except Exception:
                        pass
            if restore_contact is not None and stream is not None:
                _plant_request(stream, {'op': 'write',
                                        'point': restore_contact,
                                        'value': {'bool': False}})
        except Exception:
            pass
        finally:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
