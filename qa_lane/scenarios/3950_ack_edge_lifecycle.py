"""The ack_edge_lifecycle acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the ack-edge-lifecycle case is the same shape as the
# power-fail-trip leg: it drives the journaled moisture field contact
# through the plant protocol under whichever peer owns the field,
# cycles the managed alarm's ack input through its consumed edge, and
# restores both — it perturbs no role and leaves no latch standing, so
# it needs no declared window.


# --------------------------------------------------------------------
# The consumed-edge acknowledgment lifecycle on the deployed pair
# (WW-ALM-002's lifecycle clause — the per-revision lane evidence for
# the contract the #781/#947/#961 cluster settled). `p101-moisture` is
# the journaled field contact wired through the pump's moisture-guard
# interlock into the managed `p101-moisture-*` alarm's `in` — the rig's
# declared lever for that alarm — and into nothing else on the control
# path, so the leg trips no duty consequence. With the pair settled and
# the alarm idle, a plant-protocol write under the active's shared
# writer claim stands the condition: `alarm` asserts and
# `unacknowledged` latches. A receipted `write_value` on the declared
# writable ack input is the operator's press — the latch clears on the
# consumed false->true edge while `alarm` keeps reporting process
# truth. The release write then never goes out — the dropped-release
# state #947/#961 settled: the input serves held `true`. With the level
# held, clearing and re-driving the condition must re-latch
# `unacknowledged` — a held level is not an acknowledgment (#781) — and
# a press landing on the held level settles applied while clearing
# nothing, the wedge the release owed. The late release write re-arms
# the edge and a subsequent press acknowledges — a dropped release
# cannot wedge later acknowledgments. The contact and the ack input
# restore to their baselines, every journaled transition lands in the
# declared order beside the attributed settlements, and the pair's
# roles never move. Functional misses name ack-edge-failed; ordering
# and journal-contract violations name ack-edge-nondeterministic; a rig
# that predates the wiring this lifecycle rides reports inconclusive.

ACK_EDGE_DEADLINE = 30   # bound on each served transition/settle wait
ACK_EDGE_POLL = 0.05     # transition-watch cadence — under the scan
ACK_EDGE_ACTOR = 'qa-lane'
ACK_EDGE_HELD_SCANS = 2  # the wedge window: served scans the latch must
                         # stand through once the held press settles


def _managed_alarm_bindings(snapshot):
    """{alarm point: (instance, {port: bound point})} — every served
    managed-alarm descriptor keyed by the point its `alarm` output
    binds, so the leg reads the instance's declared port wiring rather
    than assuming it."""
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


def _journaled_settles(journal, write):
    """[(entry seq, entry tick, receipt)] — the journaled settled
    receipts for `write`, in journal order; identical submissions
    match positionally by sequence."""
    return [(entry.get('seq'), entry.get('tick'),
             (entry.get('event', {}).get('command_settled', {})
              .get('receipt') or {}))
            for entry in _journal_list(journal)
            if ((entry.get('event', {}).get('command_settled', {})
                 .get('receipt') or {})
                .get('command', {}).get('write_value') == write)]


def scenario_ack_edge_lifecycle(ctx):
    """Exercise the consumed-edge ack lifecycle on the deployed pair:
    drive the moisture alarm's condition, prove the edge clears the
    latch while the alarm stands, prove a held ack level neither
    suppresses a fresh trip's latch nor acknowledges on a second press,
    land the late release, and prove the next press acknowledges —
    every transition journaled in order."""
    case = Case('ack-edge-lifecycle',
                'The consumed-edge ack lifecycle on the moisture alarm',
                'with the deployed pair settled and the managed '
                'p101-moisture alarm idle, a plant-protocol write '
                'driving the journaled p101-moisture contact under the '
                'active\'s shared writer claim asserts the alarm and '
                'latches unacknowledged; a receipted write_value true '
                'on the declared ack input clears the latch on the '
                'consumed edge while alarm keeps reporting process '
                'truth; the release write never landing leaves the '
                'input held, a fresh trip re-latches under the held '
                'level, and a press on the held level settles applied '
                'while clearing nothing; the late release re-arms the '
                'edge and a subsequent press acknowledges; the contact '
                'and the ack input restore to baseline, every journaled '
                'transition lands in the declared order beside the '
                'attributed settlements, and the pair\'s roles never '
                'move')
    stream = None
    restore_contact = None  # the field point while the drive stands
    held_acks = []          # ack input points left standing true
    submitted = []          # (tag, write_value) in submission order
    cleanup = {}            # {ack_point: unack_point} once resolved
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
                            'ack-edge-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming the moisture '
                      'contact and its managed alarm set')
        names = {'p101-moisture': 'contact',
                 'p101-moisture-ok': 'moisture_ok',
                 'p101-moisture-ack': 'ack',
                 'p101-moisture-alarm': 'alarm',
                 'p101-moisture-unacknowledged': 'unack',
                 'p101-moisture-shelved': 'shelved',
                 'p101-moisture-suppressed': 'suppressed',
                 'p101-moisture-out-of-service': 'alarm_oos'}
        entries = {entry.get('name'): entry
                   for entry in signals.get('points', [])
                   if entry.get('name') in names}
        missing = sorted(set(names) - set(entries))
        if missing:
            return case.finish('inconclusive', 'the deployed model '
                               'lacks the moisture-contact alarm '
                               'wiring — no signals '
                               + ', '.join(missing))
        entry = entries['p101-moisture-ack']
        if not entry.get('writable') \
                or entry.get('direction') != 'in' \
                or entry.get('value_type') != 'bool':
            return case.finish('inconclusive', 'the p101-moisture-ack '
                               'point is not the alarm\'s writable '
                               'bool ack input: '
                               + json.dumps(entry)[:300])
        points = {names[name]: entry.get('point')
                  for name, entry in entries.items()}
        cleanup = {points['ack']: points['unack']}

        # The declared wiring: a served managed-alarm descriptor must
        # bind this instance's ports to the resolved points — the rig's
        # own account of the lifecycle's wiring rather than the name
        # convention's. The `in` port's bound point is the synthesized
        # carrier the guard interlock's tripped delivers into.
        snap0 = _snapshot(ctx, base)
        bindings = _managed_alarm_bindings(snap0)
        alarm_name, ports = bindings.get(points['alarm'], (None, {}))
        if alarm_name is None:
            return case.finish('inconclusive', 'no served managed-'
                               'alarm descriptor binds the '
                               'p101-moisture-alarm point '
                               + str(points['alarm']))
        bound = {'ack': 'ack', 'unacknowledged': 'unack',
                 'shelved': 'shelved', 'suppressed': 'suppressed',
                 'out_of_service': 'alarm_oos'}
        drift = sorted(port for port, key in bound.items()
                       if ports.get(port) is not None
                       and ports[port] != points[bound[port]])
        if drift:
            return case.finish('inconclusive', 'the ' + alarm_name
                               + ' descriptor binds ' + ', '.join(drift)
                               + ' off the declared points — the '
                               'served wiring drifts from the model')
        for port in ('in', 'ack', 'unacknowledged'):
            if ports.get(port) is None:
                return case.finish('inconclusive', 'the ' + alarm_name
                                   + ' descriptor binds no ' + port
                                   + ' port — the ack lifecycle has '
                                   'no wiring to exercise')
        points['alarm_in'] = ports['in']
        ref = save_evidence(
            ctx['evidence_dir'], 'ack-edge-wiring.json',
            {'instance': alarm_name,
             'points': {key: points[key] for key in sorted(points)}})
        case.evidence('file', ref, 'the managed alarm\'s declared '
                      'port bindings')
        case.observe('alarm wiring: ' + alarm_name + ' — contact '
                     + str(points['contact']) + ' -> in '
                     + str(points['alarm_in']) + ', ack '
                     + str(points['ack']) + ', alarm '
                     + str(points['alarm']) + ', unacknowledged '
                     + str(points['unack']))

        if ctx.get('plant') is None:
            return case.finish('inconclusive',
                               'the run publishes no plant endpoint')
        owner = (ctx.get('plant_owner') or {}).get(active)
        if owner is None:
            return case.finish('inconclusive', 'the run pins no '
                               'plant-writer owner token for the '
                               'settled active ' + str(active))
        # The same claim seam as the power-trip drive: the field
        # census and the point reads ride the shipped dcs-plant-ctl,
        # while the standing shared claim and the writes under it stay
        # on the raw attachment — the tool exposes no ensure_writer
        # under a chosen owner token.
        stream = _plant_connect(ctx)
        field = _field_inputs(ctx)
        if points['contact'] not in field:
            return case.finish('inconclusive', 'the p101-moisture '
                               'signal\'s point '
                               + str(points['contact'])
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
        baseline_contact = (_plant_read(ctx, points['contact'])
                            .get('value') or {}).get('bool')
        if baseline_contact is not False:
            return case.finish('inconclusive', 'the p101-moisture '
                               'contact does not read false ahead of '
                               'the drive: '
                               + json.dumps(baseline_contact))

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
            reported a sample, ack-edge-failed when the served values
            never landed the leg."""
            hit = wait_for(lambda: poll(cond, keys),
                           time.monotonic() + ACK_EDGE_DEADLINE,
                           interval=ACK_EDGE_POLL)
            snap = last.get('snap') or {}
            ref = save_evidence(
                ctx['evidence_dir'], 'ack-edge-' + name + '.json',
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
                'failed', 'ack-edge-failed: the ' + name + ' leg '
                'never landed; last served ' + json.dumps(
                    {key: value(key, snap) for key in sorted(keys)},
                    sort_keys=True)[:500])

        def command(tag, want):
            """One receipted write_value on the ack input: submit,
            record, and save the wire receipt. A refused submission is
            a functional miss — the receipted path owes the writable
            point its admission."""
            write = {'point': points['ack'], 'kind': 'bool',
                     'value': {'bool': want}}
            status, receipt = http_json(
                'POST', base + '/command',
                {'command': {'write_value': write},
                 'actor': ACK_EDGE_ACTOR})
            ref = save_evidence(
                ctx['evidence_dir'],
                'ack-edge-' + tag + '-receipt.json',
                {'status': status, 'body': receipt})
            case.evidence('file', ref, 'the ' + tag
                          + ' submission receipt')
            outcome = (receipt or {}).get('outcome') or {}
            if status != 200 or 'rejected' in outcome:
                return None, case.finish(
                    'failed', 'ack-edge-failed: the ' + tag
                    + ' write was refused: ' + str(status) + ' '
                    + json.dumps(receipt)[:400])
            submitted.append((tag, write))
            return write, None

        # The idle baseline: the contact clear, the alarm's flags down,
        # the ack input re-armed, and the contact's inverted serving
        # standing — the posture the lifecycle runs from.
        baseline_keys = ('contact', 'moisture_ok', 'alarm_in', 'ack',
                         'alarm', 'unack', 'shelved', 'suppressed',
                         'alarm_oos')

        def idle(snap):
            if value('contact', snap) is not False \
                    or value('moisture_ok', snap) is not True:
                return None
            if value('alarm_in', snap) is not False \
                    or value('alarm', snap) is not False \
                    or value('unack', snap) is not False \
                    or value('ack', snap) is not False:
                return None
            for key in ('shelved', 'suppressed', 'alarm_oos'):
                if value(key, snap) is not False:
                    return None
            return snap

        hit = wait_for(lambda: poll(idle, baseline_keys),
                       time.monotonic() + ACK_EDGE_DEADLINE,
                       interval=ACK_EDGE_POLL)
        snap = last.get('snap') or {}
        ref = save_evidence(
            ctx['evidence_dir'], 'ack-edge-baseline.json',
            {'tick': snap.get('tick'),
             'contact': baseline_contact,
             'samples': {key: _point_sample(snap, points[key])
                         for key in sorted(baseline_keys)}})
        case.evidence('file', ref, 'the settled idle baseline ahead '
                      'of the drive')
        if hit is None:
            unreported = sorted(key for key in baseline_keys
                                if key not in seen)
            return case.finish(
                'inconclusive', 'the settled idle baseline never '
                'landed'
                + (': awaited points never reported on the served '
                   'snapshot: ' + ', '.join(unreported)
                   if unreported else ' — the deployed rig is not in '
                   'the posture the lifecycle runs from'))

        # The journal floor ahead of the drive: this leg's records are
        # the ones above it.
        _, journal0 = http_json('GET', base + '/journal?since=0')
        floor = max((entry.get('seq') or 0
                     for entry in _journal_list(journal0)
                     if isinstance(entry, dict)), default=0)

        def drive(want):
            """One plant-protocol write on the contact under the
            shared claim — the declared field lever."""
            return _plant_request(
                stream, {'op': 'write', 'point': points['contact'],
                         'value': {'bool': want}})

        alarm_keys = ('contact', 'moisture_ok', 'alarm_in', 'ack',
                      'alarm', 'unack', 'shelved', 'suppressed',
                      'alarm_oos')

        def standing(snap, alarmed, unacked):
            """The managed flags never move; the carrier chain and the
            two alarm flags land the driven posture."""
            if value('contact', snap) is not alarmed:
                return None
            if value('moisture_ok', snap) is alarmed \
                    or value('alarm_in', snap) is not alarmed \
                    or value('alarm', snap) is not alarmed:
                return None
            if value('unack', snap) is not unacked:
                return None
            for key in ('shelved', 'suppressed', 'alarm_oos'):
                if value(key, snap) is not False:
                    return None
            return snap

        # The trip: the journaled field contact driven true under the
        # shared claim. The guard interlock trips the same scan; the
        # alarm's `in` lands one carrier hop later.
        verdict = drive(True)
        if verdict.get('result') != 'done':
            return case.finish('failed', 'ack-edge-failed: the '
                               'p101-moisture write on point '
                               + str(points['contact'])
                               + ' was refused under the shared '
                               'claim: ' + json.dumps(verdict)[:300])
        restore_contact = points['contact']
        case.observe('p101-moisture driven true on field point '
                     + str(points['contact']))

        hit, error = leg(
            'tripped', lambda s: standing(s, True, True)
            and value('ack', s) is False, alarm_keys)
        if error:
            return error
        case.observe('the alarm asserted at tick '
                     + str(hit.get('tick')) + ': the guard tripped, '
                     'alarm stands, unacknowledged latched, the '
                     'managed flags down, the ack input still re-armed')
        if _settled_active(ctx) != active:
            return case.finish('failed', 'ack-edge-failed: the '
                               'active role moved under the moisture '
                               'drive — a field trip is not peer loss')

        # The edge acknowledgment: a receipted press clears the latch
        # while the standing alarm keeps reporting process truth.
        write, error = command('press-1', True)
        if error:
            return error
        held_acks.append(points['ack'])

        hit, error = leg(
            'acknowledged',
            lambda s: standing(s, True, False)
            and value('ack', s) is True, alarm_keys)
        if error:
            return error
        case.observe('the settled press cleared the unacknowledged '
                     'latch at tick ' + str(hit.get('tick'))
                     + ' while the condition stood — the alarm, the '
                     'contact, and the held ack level all unchanged')

        # The dropped release: the release write never goes out. The
        # condition clears and re-asserts while the input stays held —
        # the state a lost release leaves.
        verdict = drive(False)
        if verdict.get('result') != 'done':
            return case.finish('failed', 'ack-edge-failed: the '
                               'condition-clear write was refused: '
                               + json.dumps(verdict)[:300])
        case.observe('condition cleared — the release write never '
                     'went out; the ack input stays held')
        hit, error = leg(
            'cleared', lambda s: standing(s, False, False)
            and value('ack', s) is True, alarm_keys)
        if error:
            return error
        case.observe('the condition released at tick '
                     + str(hit.get('tick')) + ' with the ack input '
                     'still held true — the dropped-release state')

        # The held-level fresh trip: the latch must re-fire — a held
        # ack level is not an acknowledgment.
        verdict = drive(True)
        if verdict.get('result') != 'done':
            return case.finish('failed', 'ack-edge-failed: the '
                               'fresh-trip write was refused: '
                               + json.dumps(verdict)[:300])
        hit, error = leg(
            'retripped', lambda s: standing(s, True, True)
            and value('ack', s) is True, alarm_keys)
        if error:
            return error
        case.observe('the fresh trip re-latched unacknowledged at '
                     'tick ' + str(hit.get('tick'))
                     + ' under the held ack level — the consumed edge '
                     'suppressed nothing')

        # The wedge press: a second ack=true write on the held level
        # lands no edge — the receipt must settle applied while the
        # standing latch clears nothing, for the settle's scan plus the
        # declared held window.
        write2, error = command('press-2', True)
        if error:
            return error
        settle = {}

        def press2_settled():
            try:
                _, journal = http_json('GET', base + '/journal?since='
                                       + str(floor))
            except Exception:
                return None
            settles = _journaled_settles(journal, write2)
            if len(settles) >= 2:
                settle['tick'] = settles[1][1]
                settle['receipt'] = settles[1][2]
                return journal
            return None

        if not wait_for(press2_settled,
                        time.monotonic() + ACK_EDGE_DEADLINE,
                        interval=ACK_EDGE_POLL):
            return case.finish('failed', 'ack-edge-failed: the held '
                               'press\'s settlement never journaled')
        receipt2 = settle.get('receipt') or {}
        if 'applied' not in (receipt2.get('outcome') or {}):
            return case.finish('failed', 'ack-edge-failed: the held '
                               'press did not settle applied: '
                               + json.dumps(receipt2)[:300])
        settle_tick = settle.get('tick')
        if not isinstance(settle_tick, int) \
                or isinstance(settle_tick, bool):
            return case.finish('inconclusive', 'the held press\'s '
                               'journaled settlement carries no '
                               'scan tick: '
                               + json.dumps(receipt2)[:300])

        broken = {}

        def wedge_window():
            """The latch must stand through the held press: every
            served scan until the settle's scan clears by the declared
            window; any clear is the acknowledgment the consumed edge
            owes never to land."""
            snap = _try_snapshot(ctx, base)
            if snap is None:
                return None
            last['snap'] = snap
            if value('unack', snap) is not True \
                    or value('alarm', snap) is not True \
                    or value('ack', snap) is not True:
                broken['at'] = snap.get('tick')
                return snap
            if (snap.get('tick') or 0) \
                    >= settle_tick + ACK_EDGE_HELD_SCANS:
                return snap
            return None

        wait_for(wedge_window,
                 time.monotonic() + ACK_EDGE_DEADLINE,
                 interval=ACK_EDGE_POLL)
        snap = last.get('snap') or {}
        ref = save_evidence(
            ctx['evidence_dir'], 'ack-edge-held-press.json',
            {'settle_tick': settle_tick,
             'receipt': receipt2,
             'last_tick': snap.get('tick'),
             'held': 'at' not in broken,
             'samples': {key: _point_sample(snap, points[key])
                         for key in sorted(alarm_keys)}})
        case.evidence('file', ref, 'the held-press wedge window — '
                      'the press settled applied while the latch '
                      'stood')
        if 'at' in broken:
            return case.finish('failed', 'ack-edge-failed: the '
                               'held-level press acknowledged the '
                               'standing latch at tick '
                               + str(broken['at']) + ' — the consumed '
                               'edge owes no clear without a fresh '
                               'false->true write')
        if (snap.get('tick') or 0) < settle_tick + ACK_EDGE_HELD_SCANS:
            return case.finish('failed', 'ack-edge-failed: the '
                               'served snapshot stopped advancing '
                               'through the held-press window at tick '
                               + str(snap.get('tick')))
        case.observe('the held press settled applied at tick '
                     + str(settle_tick) + ' and cleared nothing — the '
                     'standing latch held through '
                     + str(snap.get('tick') - settle_tick)
                     + ' more served scans')

        # The release-write leg: the late release re-arms the edge —
        # a falling write clears no latch.
        release, error = command('release', False)
        if error:
            return error
        hit, error = leg(
            'released', lambda s: standing(s, True, True)
            and value('ack', s) is False, alarm_keys)
        if error:
            return error
        held_acks.remove(points['ack'])
        case.observe('the release write landed at tick '
                     + str(hit.get('tick')) + ': the ack input '
                     're-armed while the latch still stood — a '
                     'falling write acknowledges nothing')

        # The recovered press: a fresh edge on the re-armed input —
        # the dropped release cannot wedge later acknowledgments.
        write3, error = command('press-3', True)
        if error:
            return error
        held_acks.append(points['ack'])
        hit, error = leg(
            're-acknowledged',
            lambda s: standing(s, True, False)
            and value('ack', s) is True, alarm_keys)
        if error:
            return error
        case.observe('the recovered press cleared the latch at tick '
                     + str(hit.get('tick'))
                     + ' while the condition stood — the dropped '
                     'release wedged nothing')

        # Restore: the contact back to its baseline while the press
        # holds the ack input — then the re-arm write sequenced after
        # the clear lands, so its settlement journals in its own
        # phase — the leg leaves the field and the latch as found.
        verdict = drive(False)
        if verdict.get('result') != 'done':
            return case.finish('failed', 'ack-edge-failed: the '
                               'contact restore write was refused: '
                               + json.dumps(verdict)[:300])
        restore_contact = None
        hit, error = leg(
            'restored-alarm', lambda s: standing(s, False, False)
            and value('ack', s) is True, alarm_keys)
        if error:
            return error
        rearm, error = command('rearm', False)
        if error:
            return error
        hit, error = leg(
            'restored', lambda s: standing(s, False, False)
            and value('ack', s) is False, alarm_keys)
        if error:
            return error
        held_acks.remove(points['ack'])
        case.observe('restored at tick ' + str(hit.get('tick'))
                     + ': the contact, the standing alarm, the latch, '
                     'and the ack input all back to baseline')

        # The journaled audit: every declared-journaled point lands
        # its transition pair exactly, the journaled event stream
        # decomposes into the leg's declared phases in order — the
        # fresh trip under the held level included — every receipted
        # write settles applied and attributed in submission order,
        # and no role change and no non-journaled point records a
        # transition.
        expected_pairs = {
            'contact': [True, False, True, False],
            'alarm': [True, False, True, False],
            'unack': [True, False, True, False],
            'shelved': [], 'suppressed': [], 'alarm_oos': []}
        nonjournaled = {points[key] for key in
                        ('ack', 'alarm_in', 'moisture_ok')}
        complete = {}
        violations = {}
        got_pairs = {}

        def journaled():
            try:
                _, journal = http_json('GET', base + '/journal?since='
                                       + str(floor))
            except Exception:
                return None
            last['journal'] = journal
            changes = _journal_point_changes(journal)
            for key, wanted in expected_pairs.items():
                got = list(changes.get(points[key], []))
                want = [{'bool': value} for value in wanted]
                got_pairs[key] = got
                if got == want:
                    complete[key] = got
                elif len(got) > len(want) or got != want[:len(got)]:
                    violations['transitions-' + key] = \
                        'point ' + str(points[key]) + ' journaled ' \
                        + json.dumps(got) \
                        + ' — not the declared lifecycle sequence'
            for entry in _journal_list(journal):
                event = entry.get('event') or {}
                if 'role_changed' in event:
                    violations['role-change'] = \
                        'a role_changed event journaled under the ' \
                        'ack lifecycle'
                change = event.get('point_changed')
                if isinstance(change, dict) \
                        and change.get('point') in nonjournaled:
                    violations['unjournaled-' + str(change['point'])] = \
                        'the non-journaled point ' \
                        + str(change['point']) \
                        + ' journaled a transition'
            if len(complete) == len(expected_pairs) or violations:
                return journal
            return None

        wait_for(journaled, time.monotonic() + ACK_EDGE_DEADLINE,
                 interval=POLL_INTERVAL)
        ref = save_evidence(
            ctx['evidence_dir'], 'ack-edge-journal.json',
            {'floor': floor, 'complete': sorted(complete),
             'transitions': {key: got_pairs.get(key)
                             for key in sorted(got_pairs)},
             'violations': sorted(violations)})
        case.evidence('file', ref, 'the journaled lifecycle above '
                      'the pre-drive floor')
        if violations:
            return case.finish(
                'failed', 'ack-edge-nondeterministic: ' + '; '.join(
                    violations[key] for key in sorted(violations)))
        missing = [key for key in expected_pairs if key not in complete]
        if missing:
            return case.finish('failed', 'ack-edge-failed: the '
                               'served journal never recorded the '
                               'declared point_changed evidence on: '
                               + ', '.join(missing))

        # The order half: the journal's own sequence decomposes into
        # the leg's declared phases — the contact's edge leads the
        # carrier hop that lands the alarm flags, each settle brackets
        # its scan's clear, and the held press's settlement sits
        # between the fresh trip's latch and the release with no clear
        # beside it.
        events = []
        press_n = 0
        release_n = 0
        ordinals = {}
        for entry in _journal_list(last.get('journal') or []):
            event = entry.get('event') or {}
            change = event.get('point_changed')
            if isinstance(change, dict) \
                    and change.get('point') in {
                        points[key] for key in expected_pairs}:
                key = next(key for key in expected_pairs
                           if points[key] == change['point'])
                to = (change.get('to') or {}).get('bool')
                tag = key + ('+' if to is True else '-')
                ordinals[tag] = ordinals.get(tag, 0) + 1
                events.append((entry.get('seq'),
                               tag + str(ordinals[tag])))
                continue
            receipt = (event.get('command_settled') or {}) \
                .get('receipt')
            if not isinstance(receipt, dict):
                continue
            write_ = (receipt.get('command') or {}) \
                .get('write_value') or {}
            if write_.get('point') != points['ack']:
                continue
            if ((write_.get('value') or {}).get('bool')) is True:
                press_n += 1
                events.append((entry.get('seq'),
                               'press+' + str(press_n)))
            elif ((write_.get('value') or {}).get('bool')) is False:
                release_n += 1
                events.append((entry.get('seq'),
                               'release+' + str(release_n)))
        phases = [
            frozenset({'contact+1'}),
            frozenset({'alarm+1', 'unack+1'}),
            frozenset({'press+1', 'unack-1'}),
            frozenset({'contact-1'}),
            frozenset({'alarm-1'}),
            frozenset({'contact+2'}),
            frozenset({'alarm+2', 'unack+2'}),
            frozenset({'press+2'}),
            frozenset({'release+1'}),
            frozenset({'press+3', 'unack-2'}),
            frozenset({'contact-2'}),
            frozenset({'alarm-2'}),
            frozenset({'release+2'})]
        events.sort(key=lambda item: item[0])
        problems = []
        position = 0
        for index, phase in enumerate(phases):
            got = {tag for _, tag
                   in events[position:position + len(phase)]}
            if got != phase:
                problems.append(
                    'journal phase ' + str(index + 1) + ' landed '
                    + json.dumps(sorted(got))
                    + ' — not the declared '
                    + json.dumps(sorted(phase)))
                break
            position += len(phase)
        else:
            if position != len(events):
                problems.append('the journal carried ' + str(
                    len(events) - position)
                    + ' lifecycle events past the declared sequence: '
                    + json.dumps(
                        [tag for _, tag in events[position:]])[:300])
        ref = save_evidence(
            ctx['evidence_dir'], 'ack-edge-journal-order.json',
            {'floor': floor,
             'events': [[seq, tag] for seq, tag in events],
             'phases': [sorted(phase) for phase in phases],
             'problems': problems})
        case.evidence('file', ref, 'the journaled lifecycle order — '
                      'each leg\'s transitions inside its declared '
                      'phase')
        if problems:
            return case.finish('failed',
                               'ack-edge-nondeterministic: '
                               + '; '.join(problems))

        # The receipts: every submitted write settled applied and
        # attributed, in submission order.
        journal = last.get('journal') or []
        for tag, write_ in submitted:
            matches = [receipt for receipt in _settled_receipts(journal)
                       if (receipt.get('command') or {})
                       .get('write_value') == write_]
            ordinal = sum(1 for _, other in submitted[:submitted
                          .index((tag, write_)) + 1]
                          if other == write_)
            if len(matches) < ordinal:
                return case.finish('failed', 'ack-edge-failed: no '
                                   'settled receipt journaled for '
                                   'the ' + tag + ' write '
                                   + json.dumps(write_)[:200])
            receipt_ = matches[ordinal - 1]
            if receipt_.get('actor') != ACK_EDGE_ACTOR:
                return case.finish('failed', 'ack-edge-failed: the '
                                   + tag + ' settle lost its actor: '
                                   + json.dumps(receipt_)[:200])
            if 'applied' not in (receipt_.get('outcome') or {}):
                return case.finish('failed', 'ack-edge-failed: the '
                                   + tag + ' write did not settle '
                                   'applied: '
                                   + json.dumps(receipt_)[:200])
        case.observe('journaled: the contact, the alarm\'s flags, '
                     'and every receipted write — the whole lifecycle '
                     'in the declared order, applied and attributed')

        # The restore audit: the driven contact reads back its
        # baseline and the pair's roles never moved.
        restored = (_plant_read(ctx, points['contact'])
                    .get('value') or {}).get('bool')
        if restored is not False:
            return case.finish('failed', 'ack-edge-failed: the '
                               'p101-moisture contact did not '
                               'restore — it reads '
                               + json.dumps(restored)
                               + ' against baseline false')
        if _settled_active(ctx) != active:
            return case.finish('failed', 'ack-edge-failed: the '
                               'active role moved during the leg')
        if peer_base is not None:
            report = _try_role(ctx, peer_base)
            if report is None \
                    or report.get('role') \
                    != (peer_role0 or {}).get('role'):
                return case.finish('failed', 'ack-edge-failed: the '
                                   'pair\'s roles moved during the '
                                   'leg — ' + peer + ' reports '
                                   + json.dumps(report))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
    finally:
        # The driven contact is the run's shared field and the ack
        # input the alarm's operator point: a case that leaves either
        # standing poisons every later leg — the drive back to its
        # baseline, every held ack re-armed, and any unacknowledged
        # latch the legs left standing acknowledged best-effort.
        if stream is not None:
            if restore_contact is not None:
                try:
                    _plant_request(
                        stream, {'op': 'write',
                                 'point': restore_contact,
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
                               'actor': ACK_EDGE_ACTOR})
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
                                   'actor': ACK_EDGE_ACTOR})
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
                                   'actor': ACK_EDGE_ACTOR})
                except Exception:
                    pass
