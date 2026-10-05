"""The cause_alarm_quality acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the quality-aware cause-alarm case sits with the alarm
# contract's cluster and is self-contained on either role layout: it
# drives the declared pump into manual, degrades and clears the
# declared protection contacts through the shipped plant tool, and
# restores the contacts, the manual selection, the hand request, every
# ack input, and every latch it drove — it perturbs no role, and
# leaves the field and the pump's service posture as it found them, so
# it needs no declared window.


# --------------------------------------------------------------------
# The quality-aware cause-alarm contract on the deployed pair
# (WW-ALM-001's annunciation clause, WW-OPS-003's signal-confidence
# clause, WW-ENG-003's declared-wiring clause — the per-revision lane
# evidence for the contract the #827 fix establishes: a protection
# contact degraded enough to trip the pump's fail-safe protection must
# annunciate its own cause alarm, because a protective stop that
# annunciates nothing withholds the operator's evidence of the cause).
# The rig's pump-station model declares `p101-thermal` and
# `p101-moisture` as journaled field contacts wired two ways: straight
# into the pump's protection aggregator's trip ports — so an asserted
# *or* untrusted contact drops `p101-protect-tripped`,
# `p101-protections-ok`, and the motor command — and into one
# single-trip `interlock` guard each whose `tripped` drives the
# matching managed `p101-thermal-*` / `p101-moisture-*` alarm's
# condition. A guard whose `in` binds a held anchor and whose
# `permissive` binds a held `true` reports the contact alone, so a
# degraded contact — value healthy, quality `bad:device_fault` —
# raises the declared alarm with its reason instead of leaving it
# clean-false over a stopped motor.
#
# The leg, with the pair settled and the pump hand-held and running:
#
# - the degraded thermal contact: the shipped `dcs-plant-ctl` fault
#   surface substitutes `bad:device_fault` over the stored field value
#   (the degraded reading is presented, never a silently healthy
#   last-known), the protection trips fail-safe, the field command
#   de-energizes, and the thermal cause alarm's condition, `alarm`, and
#   `unacknowledged` annunciate with `shelved`/`suppressed`/
#   `out-of-service` down — while the sibling contact's alarm and the
#   pump's motor-fault alarm stay clean: no double-firing, no
#   cross-annunciation;
# - the declared recovery: clearing the fault returns the contact to
#   Good, the trip clears, the alarm's condition reports clear while
#   its latch stands, and the held hand request re-energizes the field
#   command; the receipted ack press then clears the latch through the
#   consumed edge (WW-ALM-002's lifecycle) and the released request
#   re-arms the input;
# - the degraded moisture contact: the same contract on the sibling
#   contact, from the same running hand-held pump;
# - the honest-absence halves: a Good-quality contact tripping by value
#   annunciates identically — the quality-aware path must not rename a
#   value trip, and the durable record carries no quality transition
#   beside it — and a degraded contact on a *stopped* pump plants no
#   alarm beyond the declared contract: the named cause alarm still
#   annunciates (a cause alarm stands in every service state) while the
#   sibling cause alarm, the pump's motor-fault alarm, and the field
#   command stay exactly where they were.
#
# Every driven transition lands in the durable journal: each declared
# journaled point's transition sequence exactly, each contact's quality
# sequence exactly, the annunciation ordered beside the trip it explains
# within the guard's declared one-hop carrier crossing, the receipted
# writes settled applied and attributed, and no role change. Functional
# misses name cause-alarm-quality-failed; ordering, bounds, and
# journal-contract violations name cause-alarm-quality-nondeterministic.
# A rig whose model does not declare the declared surface reports
# inconclusive; a model that declares the surface but wires a cause
# alarm's condition to the raw contact — the pre-#827 shape, where no
# degraded reading can ever annunciate — fails, because that is the
# defect this leg exists to catch, not an absent surface.
#
# Evidence accumulates one file per phase — the report schema bounds a
# scenario's evidence list, so each drive's legs, the submissions, and
# the journaled record land in their own file rewritten as the phase
# progresses, and each file is recorded in the case once.

CAUSE_ALARM_DEADLINE = 30  # bound on each served transition/settle wait
CAUSE_ALARM_POLL = 0.05    # transition-watch cadence — under the scan
CAUSE_ALARM_ACTOR = 'qa-lane'
CAUSE_ALARM_FAULT = 'bad:device_fault'   # the degraded quality the
                                         # shipped tool injects
CAUSE_ALARM_STEP = 2       # the scans a cause alarm's annunciation
                           # may trail its trip by — the guard's
                           # port-to-port carrier crosses one hop

# The durable record's own wire shape for a Bool sample.
TRUE = {'bool': True}
FALSE = {'bool': False}

# The declared quality-gated protection contacts the leg drives.
CONTACTS = ('thermal', 'moisture')

# The declared drives, in the order the durable record pairs their
# trips with their annunciations — matched by position, never by wall
# clock. Each entry is (drive name, evidence file stem, annunciating
# contact, the trip's 1-based occurrence on the protection carrier,
# the alarm's 1-based occurrence on that contact's own alarm point).
DRIVE_PAIRS = (('thermal', 'thermal', 'thermal', 1, 1),
               ('moisture', 'moisture', 'moisture', 2, 1),
               ('value-trip', 'value-trip', 'thermal', 3, 2),
               ('stopped-moisture', 'stopped-moisture', 'moisture', 4, 2))
DRIVES = tuple(name for name, _, _, _, _ in DRIVE_PAIRS)

# The managed surfaces every cause alarm and the pump's motor-fault
# alarm carry: the leg-private key and the signal-name suffix the
# model declares.
MANAGED_FLAGS = (('shelved', 'shelved'), ('suppressed', 'suppressed'),
                 ('oos', 'out-of-service'))


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


def _journaled_records(journal, point):
    """[(kind, to)] — the served journal's own records for one point
    above the leg's floor: the `point_changed` value transitions and
    the `quality_changed` quality transitions, in seq order."""
    records = []
    for entry in _journal_list(journal):
        event = entry.get('event') or {}
        change = event.get('point_changed')
        if isinstance(change, dict) and change.get('point') == point:
            records.append(('value', change.get('to')))
            continue
        change = event.get('quality_changed')
        if isinstance(change, dict) and change.get('point') == point:
            records.append(('quality', change.get('to')))
    return records


def _journaled_values(journal, point):
    """[to] — the `point_changed` targets one point recorded."""
    return [to for kind, to in _journaled_records(journal, point)
            if kind == 'value']


def _journaled_qualities(journal, point):
    """[to] — the `quality_changed` targets one point recorded."""
    return [to for kind, to in _journaled_records(journal, point)
            if kind == 'quality']


def _journal_tick(journal, point, kind, value, ordinal=1):
    """The scan tick of the `ordinal`-th record of `kind` landing
    `value` on `point`, or None."""
    seen = 0
    for entry in _journal_list(journal):
        event = entry.get('event') or {}
        change = event.get(
            'point_changed' if kind == 'value' else 'quality_changed')
        if not isinstance(change, dict) \
                or change.get('point') != point \
                or change.get('to') != value:
            continue
        seen += 1
        if seen == ordinal:
            return entry.get('tick')
    return None


def scenario_cause_alarm_quality(ctx):
    """Exercise the quality-aware cause-alarm contract on the deployed
    pair: degrade and clear each declared protection contact's quality
    on a hand-held running pump, prove the fail-safe trip cuts the
    field command and annunciates the named cause alarm with its
    reason, prove a Good-quality value trip annunciates identically,
    prove a degraded contact on a stopped pump plants nothing beyond
    the declared contract, and restore every driven input — roles
    unmoved throughout."""
    case = Case('cause-alarm-quality',
                'A degraded protection contact annunciates its cause',
                'with the deployed pair settled and the declared pump '
                'hand-held and running, a bad:device_fault injected on '
                'each declared quality-gated protection contact leaves '
                'the stored field value standing under the substituted '
                'quality, trips the protection fail-safe, cuts the '
                'field command, and annunciates the matching cause '
                'alarm\'s condition, alarm, and unacknowledged with '
                'shelved/suppressed/out-of-service down — the sibling '
                'cause alarm and the pump\'s fault alarm clean — '
                'ordered beside the trip within the guard\'s declared '
                'one-hop carrier crossing; clearing the fault restores '
                'the contact to Good, returns the trip and the '
                'hand-held field command, and the condition reports '
                'clear while the latch stands, which the receipted ack '
                'press clears on the consumed edge before the released '
                'request re-arms the input; a Good-quality value trip '
                'annunciates identically with no quality transition '
                'beside it; a degraded contact on the stopped pump '
                'annunciates its own cause alarm and nothing else; '
                'every driven transition lands in the served journal on '
                'its declared-journaled point with the receipted '
                'writes settled applied and attributed, and the '
                'pair\'s roles never move')
    stream = None
    points = {}              # the resolved leg-private point map
    restore_contact = None  # the field contact while a value drive stands
    faulted = []            # the contacts carrying an injected quality
    held_acks = []          # ack input points left standing true
    submitted = []          # (tag, write body) in submission order
    wire = []               # every submission's wire answer
    cleanup = {}            # {ack point: unacknowledged point}
    live = {'base': None}
    files = {}              # evidence stem -> its accumulating record
    filed = set()           # the stems already recorded in the case
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

        def note(stem, detail, **payload):
            """Accumulate one evidence file and record it in the case
            the first time — the report schema bounds a scenario's
            evidence list, so a phase's whole record is one entry,
            rewritten as the phase progresses."""
            files.setdefault(stem, {}).update(payload)
            ref = save_evidence(ctx['evidence_dir'],
                                'cause-alarm-' + stem + '.json',
                                files[stem])
            if stem not in filed:
                case.evidence('file', ref, detail)
                filed.add(stem)
            return files[stem]

        _, signals = http_json('GET', base + '/signals')
        note('signals', 'SignalIndex naming the declared protection '
             'contacts and their cause alarms', signals=signals)
        names = {'p101-mode': 'mode', 'p101-hand': 'hand',
                 'p101-oos': 'oos', 'p101-cmd': 'cmd',
                 'p101-avail': 'avail',
                 'p101-protect-tripped': 'protect',
                 'p101-protections-ok': 'protect_ok',
                 'p101-fault': 'fault',
                 'p101-fault-alarm': 'fault_alarm',
                 'p101-fault-unacknowledged': 'fault_unack'}
        for label, suffix in MANAGED_FLAGS:
            names['p101-fault-' + suffix] = 'fault_' + label
        for tag in CONTACTS:
            names['p101-' + tag] = tag
            names['p101-' + tag + '-ok'] = tag + '_ok'
            names['p101-' + tag + '-ack'] = tag + '_ack'
            names['p101-' + tag + '-alarm'] = tag + '_alarm'
            names['p101-' + tag + '-unacknowledged'] = tag + '_unack'
            for label, suffix in MANAGED_FLAGS:
                names['p101-' + tag + '-' + suffix] = tag + '_' + label
        entries = {entry.get('name'): entry
                   for entry in signals.get('points', [])
                   if entry.get('name') in names}
        missing = sorted(set(names) - set(entries))
        if missing:
            return case.finish('inconclusive', 'the deployed model '
                               'lacks the declared quality-gated '
                               'protection surface — no signals '
                               + ', '.join(missing))
        by_key = {names[name]: entry for name, entry in entries.items()}
        for key in ('mode', 'hand') + tuple(tag + '_ack'
                                             for tag in CONTACTS):
            entry = by_key[key]
            if not entry.get('writable') \
                    or entry.get('direction') != 'in' \
                    or entry.get('value_type') != 'bool':
                return case.finish('inconclusive', 'the '
                                   + str(entry.get('name'))
                                   + ' point is not a writable bool '
                                   'input: ' + json.dumps(entry)[:300])
        points = {names[name]: entry.get('point')
                  for name, entry in entries.items()}
        for tag in CONTACTS:
            cleanup[points[tag + '_ack']] = points[tag + '_unack']
        case.observe('declared surface: '
                     + json.dumps({name: entry.get('point')
                                   for name, entry in
                                   sorted(entries.items())},
                                  sort_keys=True))

        # The declared wiring: each cause alarm's descriptor must bind
        # this instance's ports to the resolved points, and its
        # condition port must bind something other than the raw
        # contact — the guard's synthesized carrier, not the contact
        # itself. A model binding the raw contact is the pre-#827
        # shape, where a degraded reading can never annunciate: the
        # surface is declared, so the leg fails rather than reporting
        # the absent wiring inconclusive.
        snap0 = _snapshot(ctx, base)
        bindings = _managed_alarm_bindings(snap0)
        wiring = {}
        for tag in CONTACTS:
            name, ports = bindings.get(points[tag + '_alarm'],
                                       (None, {}))
            if name is None:
                return case.finish('inconclusive', 'no served managed-'
                                   'alarm descriptor binds the '
                                   'p101-' + tag + '-alarm point '
                                   + str(points[tag + '_alarm']))
            bound = {'ack': tag + '_ack',
                     'unacknowledged': tag + '_unack'}
            bound.update({label: tag + '_' + label
                          for label, _ in MANAGED_FLAGS})
            drift = sorted(port for port, key in bound.items()
                           if ports.get(port) is not None
                           and ports[port] != points[bound[port]])
            if drift:
                return case.finish('inconclusive', 'the ' + name
                                   + ' descriptor binds ' + ', '.join(drift)
                                   + ' off the declared points — the '
                                   'served wiring drifts from the '
                                   'model')
            for port in ('in', 'ack', 'unacknowledged', 'alarm'):
                if ports.get(port) is None:
                    return case.finish('inconclusive', 'the ' + name
                                       + ' descriptor binds no ' + port
                                       + ' port — the cause alarm has '
                                       'no wiring to exercise')
            if ports['in'] == points[tag]:
                return case.finish(
                    'failed', 'cause-alarm-quality-failed: the ' + name
                    + ' alarm\'s condition binds the raw p101-' + tag
                    + ' contact (point ' + str(points[tag])
                    + ') — a degraded reading can never annunciate '
                    'through it, the pre-#827 quality-blind wiring')
            points[tag + '_alarm_in'] = ports['in']
            wiring[tag] = {'instance': name, 'in': ports['in'],
                           'ack': ports['ack'], 'alarm': ports['alarm'],
                           'unacknowledged': ports['unacknowledged']}
            case.observe('p101-' + tag + ' cause alarm: contact '
                         + str(points[tag]) + ' -> in '
                         + str(ports['in']) + ' (the guard\'s carrier), '
                         'alarm ' + str(points[tag + '_alarm'])
                         + ', ack ' + str(points[tag + '_ack']))
        note('wiring', 'the declared cause-guard wiring behind each '
             'alarm\'s condition',
             points={key: points[key] for key in sorted(points)},
             wiring=wiring)

        if ctx.get('plant') is None:
            return case.finish('inconclusive',
                               'the run publishes no plant endpoint')
        owner = (ctx.get('plant_owner') or {}).get(active)
        if owner is None:
            return case.finish('inconclusive', 'the run pins no '
                               'plant-writer owner token for the '
                               'settled active ' + str(active))
        # The same claim seam as the power-trip and ack-edge drives:
        # the field census and the point reads ride the shipped
        # dcs-plant-ctl, while the standing shared claim and the value
        # writes under it stay on the raw attachment — the tool
        # exposes no ensure_writer under a chosen owner token.
        stream = _plant_connect(ctx)
        field = _field_inputs(ctx)
        for tag in CONTACTS:
            if points[tag] not in field:
                return case.finish('inconclusive', 'the p101-' + tag
                                   + ' signal\'s point '
                                   + str(points[tag])
                                   + ' is not a field in-point the '
                                   'plant serves')
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

        last = {}
        seen = set()

        def value(key, snap):
            """The served value of one named point — and the point's
            presence, for the never-reported inconclusive split."""
            body = _point_sample(snap, points[key])
            if body is None:
                return None
            seen.add(key)
            raw = body.get('value')
            if isinstance(raw, dict):
                return next(iter(raw.values()), None)
            return raw

        def quality(key, snap):
            """The served quality key of one named point, and its
            presence."""
            body = _point_sample(snap, points[key])
            if body is None:
                return None
            seen.add(key)
            return _quality_key(body.get('quality'))

        def proven(key, snap):
            """Whether one named point reads `true` at Good — the
            fail-safe reading: a degraded contact's inverted serving
            cannot prove its condition, so availability never reports
            proven over one."""
            return value(key, snap) is True \
                and quality(key, snap) == 'good'

        def poll(cond, keys=()):
            snap = _try_snapshot(ctx, base)
            if snap is None:
                return None
            last['snap'] = snap
            for key in keys:
                if _point_sample(snap, points[key]) is not None:
                    seen.add(key)
            return snap if cond(snap) else None

        def leg(stem, name, cond, keys=()):
            """One served-transition wait, recorded into its phase's
            evidence file. A miss classifies inconclusive when an
            awaited output never reported a sample and
            cause-alarm-quality-failed when the served values never
            landed the leg."""
            hit = wait_for(lambda: poll(cond, keys),
                           time.monotonic() + CAUSE_ALARM_DEADLINE,
                           interval=CAUSE_ALARM_POLL)
            snap = last.get('snap') or {}
            note(stem, 'the served ' + name + ' leg',
                 **{name: {
                     'tick': snap.get('tick'),
                     'samples': {key: _point_sample(snap, points[key])
                                 for key in sorted(keys)}}})
            if hit:
                return hit, None
            unreported = sorted(key for key in keys if key not in seen)
            if unreported:
                return None, case.finish(
                    'inconclusive', 'the ' + name + ' leg\'s outputs '
                    'never reported on the served snapshot: '
                    + ', '.join(unreported))
            return None, case.finish(
                'failed', 'cause-alarm-quality-failed: the ' + name
                + ' leg never landed; last served ' + json.dumps(
                    {key: [_point_value(snap, points[key]),
                           _quality_key(_point_quality(
                               snap, points[key]))]
                     for key in sorted(keys)},
                    sort_keys=True)[:600])

        def field_value(point):
            """The stored field sample for one point — the field's own
            account of the driven output and contact."""
            return _plant_read(ctx, point)

        def field_cmd(want, stem, name):
            """Wait for the field's own stored motor command to read
            `want` — the leg's proof that the protective stop (or the
            declared recovery) actually reached the field, observed
            beside the served snapshot that advances the scan."""
            def reached():
                poll(lambda snap: True)
                body = field_value(points['cmd'])
                return body if (body.get('value') or {}
                                ).get('bool') is want else None
            hit = wait_for(reached,
                           time.monotonic() + CAUSE_ALARM_DEADLINE,
                           interval=CAUSE_ALARM_POLL)
            note(stem, 'the field command the ' + name + ' leg observed',
                 **{'field-' + name: {'point': points['cmd'],
                                       'want': want,
                                       'sample': hit
                                       or field_value(points['cmd'])}})
            if hit is None:
                return case.finish(
                    'failed', 'cause-alarm-quality-failed: the field '
                    'command on point ' + str(points['cmd'])
                    + ' never read ' + json.dumps(want) + ' during the '
                    + name + ' leg; it reads '
                    + json.dumps(field_value(points['cmd']))[:300])
            return None

        def write_point(point, want, tag):
            """One receipted write_value on a writable bool input:
            submit, record, and file the wire receipt. A refused
            submission is a functional miss — the receipted path owes
            the writable point its admission."""
            body = {'point': point, 'kind': 'bool',
                    'value': {'bool': want}}
            status, receipt = http_json(
                'POST', base + '/command',
                {'command': {'write_value': body},
                 'actor': CAUSE_ALARM_ACTOR})
            wire.append({'tag': tag, 'status': status, 'body': receipt})
            note('receipts', 'every receipted submission the leg drove',
                 submissions=wire)
            outcome = (receipt or {}).get('outcome') or {}
            if status != 200 or 'rejected' in outcome:
                return case.finish(
                    'failed', 'cause-alarm-quality-failed: the ' + tag
                    + ' write on point ' + str(point)
                    + ' was refused: ' + str(status) + ' '
                    + json.dumps(receipt)[:400])
            submitted.append((tag, body))
            return None

        def contact_clear(tag):
            """The contact's healthy baseline: the stored field value
            reads false at Good quality — the field's own account."""
            body = field_value(points[tag])
            return ((body.get('value') or {}).get('bool') is False
                    and _quality_key(body.get('quality')) == 'good')

        def alarm_clear(tag, snap):
            """The cause alarm's whole managed surface standing clear —
            the condition, both flags, and the three managed outputs."""
            if value(tag + '_alarm_in', snap) is not False \
                    or value(tag + '_alarm', snap) is not False \
                    or value(tag + '_unack', snap) is not False:
                return None
            for label, _ in MANAGED_FLAGS:
                if value(tag + '_' + label, snap) is not False:
                    return None
            return True

        def alarm_reported_clear(tag, snap):
            """The condition cleared while the latch still stands — the
            latching half's declared report: `alarm` follows the
            process truth, `unacknowledged` waits for its
            acknowledgment."""
            if value(tag + '_alarm_in', snap) is not False \
                    or value(tag + '_alarm', snap) is not False \
                    or value(tag + '_unack', snap) is not True:
                return None
            for label, _ in MANAGED_FLAGS:
                if value(tag + '_' + label, snap) is not False:
                    return None
            return True

        def alarm_standing(tag, snap, latched):
            """The declared annunciation: the condition carries, `alarm`
            reports process truth, the latch stands or clears as asked,
            and the managed outputs stay down."""
            if value(tag + '_alarm_in', snap) is not True \
                    or value(tag + '_alarm', snap) is not True \
                    or value(tag + '_unack', snap) is not latched:
                return None
            for label, _ in MANAGED_FLAGS:
                if value(tag + '_' + label, snap) is not False:
                    return None
            return True

        def nothing_else(tag, snap):
            """The honest-absence half: the sibling contact's whole
            alarm set and the pump's motor-fault alarm stay clean — no
            alarm beyond the declared contract may annunciate."""
            sibling = [other for other in CONTACTS
                       if other != tag][0]
            if not alarm_clear(sibling, snap):
                return None
            if value('fault', snap) is not False \
                    or value('fault_alarm', snap) is not False \
                    or value('fault_unack', snap) is not False:
                return None
            for label, _ in MANAGED_FLAGS:
                if value('fault_' + label, snap) is not False:
                    return None
            return True

        # -- the auto baseline: the contacts healthy and clear, the
        # declared surface idle, the pump in its auto posture.
        baseline_keys = ['cmd', 'avail', 'protect', 'protect_ok',
                         'fault', 'fault_alarm', 'fault_unack',
                         'mode', 'hand', 'oos']
        for tag in CONTACTS:
            baseline_keys += [tag, tag + '_ok', tag + '_alarm_in',
                              tag + '_alarm', tag + '_unack']
            baseline_keys += [tag + '_' + label
                              for label, _ in MANAGED_FLAGS]

        def baseline(snap):
            for tag in CONTACTS:
                if value(tag, snap) is not False \
                        or quality(tag, snap) != 'good' \
                        or value(tag + '_ok', snap) is not True:
                    return None
                if not alarm_clear(tag, snap):
                    return None
            if value('protect', snap) is not False \
                    or value('protect_ok', snap) is not True:
                return None
            if not proven('avail', snap):
                return None
            if value('fault', snap) is not False \
                    or value('fault_alarm', snap) is not False:
                return None
            for key in ('mode', 'hand', 'oos'):
                if value(key, snap) is not False:
                    return None
            return snap

        hit = wait_for(lambda: poll(baseline, baseline_keys),
                       time.monotonic() + CAUSE_ALARM_DEADLINE,
                       interval=CAUSE_ALARM_POLL)
        snap = last.get('snap') or {}
        note('baseline', 'the settled auto baseline ahead of the drive',
             baseline={'tick': snap.get('tick'),
                       'contacts': {tag: [field_value(points[tag]),
                                          _quality_key(_point_quality(
                                              snap, points[tag]))]
                                    for tag in CONTACTS},
                       'samples': {key: _point_sample(snap, points[key])
                                   for key in sorted(baseline_keys)}})
        if hit is None:
            unreported = sorted(key for key in baseline_keys
                                if key not in seen)
            return case.finish(
                'inconclusive', 'the settled auto baseline never landed'
                + (': awaited points never reported on the served '
                   'snapshot: ' + ', '.join(unreported)
                   if unreported else ' — the deployed rig is not in '
                   'the posture the leg runs from'))
        for tag in CONTACTS:
            if not contact_clear(tag):
                return case.finish(
                    'inconclusive', 'the p101-' + tag + ' contact does '
                    'not read false at Good ahead of the drive: '
                    + json.dumps(field_value(points[tag]))[:300])

        # The journal floor ahead of the drives: this leg's records are
        # the ones above it, the manual selection's own settlement
        # included.
        _, journal0 = http_json('GET', base + '/journal?since=0')
        floor = max((entry.get('seq') or 0
                     for entry in _journal_list(journal0)
                     if isinstance(entry, dict)), default=0)

        # -- the hand-held running pump: manual mode and the operator's
        # hand request, both receipted, until the field's own stored
        # motor command reads energized.
        for key, want in (('hand', False), ('mode', True),
                          ('hand', True)):
            error = write_point(points[key], want, key + '-start')
            if error:
                return error
        hit, error = leg('baseline', 'manual',
                         lambda s: value('mode', s) is True
                         and value('hand', s) is True
                         and value('protect_ok', s) is True,
                         ('mode', 'hand'))
        if error:
            return error
        error = field_cmd(True, 'baseline', 'energized')
        if error:
            return error
        case.observe('p101 held in manual under the standing hand '
                     'request at tick ' + str(hit.get('tick'))
                     + ': the field command reads energized')

        def acknowledge(tag, stem, ordinal):
            """The managed lifecycle's acknowledgment: the receipted
            press clears the standing latch on the consumed edge while
            the input itself holds, then the released request re-arms
            the input — a held level is not an acknowledgment."""
            error = write_point(points[tag + '_ack'], True,
                                ordinal + '-ack-press')
            if error:
                return error
            held_acks.append(points[tag + '_ack'])
            hit, error = leg(stem, ordinal + '-acknowledged',
                             lambda s: value(tag + '_unack', s) is False
                             and value(tag + '_ack', s) is True,
                             (tag + '_unack', tag + '_ack'))
            if error:
                return error
            error = write_point(points[tag + '_ack'], False,
                                ordinal + '-ack-release')
            if error:
                return error
            hit, error = leg(stem, ordinal + '-ack-released',
                             lambda s: value(tag + '_ack', s) is False
                             and value(tag + '_unack', s) is False,
                             (tag + '_ack', tag + '_unack'))
            if error:
                return error
            held_acks.remove(points[tag + '_ack'])
            case.observe('the receipted ack press cleared the p101-'
                         + tag + ' latch at tick '
                         + str(hit.get('tick')) + ' with the input held '
                         'true, and the released request re-armed it at '
                         'tick ' + str(hit.get('tick')) + ' — the whole '
                         'surface back to baseline')
            return None

        def recover(tag, stem, ordinal, running):
            """Clear the injected fault and prove the declared
            recovery: the contact back at Good over its standing
            value, the trip clearing, the condition reporting clear
            while the latch stands, and — while the hand request still
            stands — the field command returning."""
            verdict = _plant_ctl(ctx, 'clear-fault', str(points[tag]))
            if verdict.get('result') != 'done':
                return case.finish(
                    'failed', 'cause-alarm-quality-failed: clear_fault '
                    'on p101-' + tag + ' answered '
                    + json.dumps(verdict)[:300])
            faulted.remove(tag)
            keys = ['protect', 'protect_ok', tag, tag + '_alarm',
                    tag + '_unack']
            keys += [tag + '_' + label for label, _ in MANAGED_FLAGS]

            def cleared(snap):
                if quality(tag, snap) != 'good' \
                        or value(tag, snap) is not False:
                    return None
                if value('protect', snap) is not False \
                        or value('protect_ok', snap) is not True:
                    return None
                if value(tag + '_alarm', snap) is not False \
                        or value(tag + '_unack', snap) is not True:
                    return None
                for label, _ in MANAGED_FLAGS:
                    if value(tag + '_' + label, snap) is not False:
                        return None
                return snap

            hit, error = leg(stem, ordinal + '-cleared', cleared, keys)
            if error:
                return error
            case.observe('p101-' + tag + ' recovered at tick '
                         + str(hit.get('tick')) + ': quality Good, the '
                         'trip cleared, the condition reporting clear '
                         'while the latch still stands')
            if running:
                error = field_cmd(True, stem, ordinal + '-restored')
                if error:
                    return error
                case.observe('the standing hand request re-energized '
                             'the field command under the declared '
                             'holdout')
            else:
                error = field_cmd(False, stem,
                                  ordinal + '-stayed-stopped')
                if error:
                    return error
            return acknowledge(tag, stem, ordinal)

        def degrade(tag, stem, ordinal):
            """Inject the declared bad-quality fault on one contact and
            prove the whole contract on it: the substituted quality
            over the standing stored value, the fail-safe trip, the
            field command cut, the named cause alarm's annunciation,
            and nothing else announcing."""
            verdict = _plant_ctl(ctx, 'fault', str(points[tag]),
                                 CAUSE_ALARM_FAULT)
            if verdict.get('result') != 'done':
                return case.finish(
                    'failed', 'cause-alarm-quality-failed: inject_fault '
                    'on p101-' + tag + ' answered '
                    + json.dumps(verdict)[:300])
            faulted.append(tag)

            keys = ['protect', 'protect_ok', 'avail', tag, tag + '_ok',
                    tag + '_alarm_in', tag + '_alarm', tag + '_unack']
            keys += [tag + '_' + label for label, _ in MANAGED_FLAGS]

            def degraded(snap):
                if quality(tag, snap) != CAUSE_ALARM_FAULT \
                        or value(tag, snap) is not False:
                    return None
                if value('protect', snap) is not True \
                        or value('protect_ok', snap) is not False:
                    return None
                if proven('avail', snap):
                    return None
                if not alarm_standing(tag, snap, True):
                    return None
                return snap if nothing_else(tag, snap) else None

            hit, error = leg(stem, ordinal + '-degraded', degraded, keys)
            if error:
                return error
            body = field_value(points[tag])
            if (body.get('value') or {}).get('bool') is not False:
                return case.finish(
                    'failed', 'cause-alarm-quality-failed: the degraded '
                    'p101-' + tag + ' sample replaced the stored field '
                    'value with ' + json.dumps(body.get('value')))
            note(stem, 'the degraded contact\'s own stored sample beside '
                 'its served sample',
                 contact={'point': points[tag], 'field': body,
                          'served': _point_sample(hit, points[tag])})
            case.observe('p101-' + tag + ' degraded at the plant and '
                         'tripped its protection with its cause alarm '
                         'annunciated at tick ' + str(hit.get('tick'))
                         + ': ' + tag + '-alarm and ' + tag + '-unack '
                         'stand with the managed flags down, the '
                         'sibling alarm and the fault alarm clean, and '
                         'availability no longer proven')
            return field_cmd(False, stem, ordinal + '-cut')

        # -- drive 1: the degraded thermal contact on the running pump.
        error = degrade('thermal', 'thermal', 'thermal')
        if error:
            return error
        error = recover('thermal', 'thermal', 'thermal', True)
        if error:
            return error

        # -- drive 2: the degraded moisture contact, the sibling
        # contact's own contract from the same running hand-held pump.
        error = degrade('moisture', 'moisture', 'moisture')
        if error:
            return error
        error = recover('moisture', 'moisture', 'moisture', True)
        if error:
            return error

        # -- the honest-absence half: a Good-quality contact tripping
        # by value annunciates identically. The value drive rides the
        # raw attachment under the shared claim; the drive asserts the
        # contact's served quality never leaves Good.
        verdict = _plant_request(
            stream, {'op': 'write', 'point': points['thermal'],
                     'value': {'bool': True}})
        if verdict.get('result') != 'done':
            return case.finish(
                'failed', 'cause-alarm-quality-failed: the value write '
                'on p101-thermal answered ' + json.dumps(verdict)[:300])
        restore_contact = points['thermal']
        hit, error = leg(
            'value-trip', 'value-trip',
            lambda s: quality('thermal', s) == 'good'
            and alarm_standing('thermal', s, True)
            and value('protect', s) is True
            and value('protect_ok', s) is not True,
            ('thermal', 'protect', 'protect_ok', 'thermal_alarm_in',
             'thermal_alarm', 'thermal_unack', 'thermal_shelved',
             'thermal_suppressed', 'thermal_oos'))
        if error:
            return error
        error = field_cmd(False, 'value-trip', 'value-cut')
        if error:
            return error
        case.observe('a Good-quality value trip on p101-thermal '
                     'annunciated the same cause alarm at tick '
                     + str(hit.get('tick')) + ' — the quality-aware '
                     'path did not rename a value trip')

        verdict = _plant_request(
            stream, {'op': 'write', 'point': points['thermal'],
                     'value': {'bool': False}})
        if verdict.get('result') != 'done':
            return case.finish(
                'failed', 'cause-alarm-quality-failed: the release '
                'write on p101-thermal answered '
                + json.dumps(verdict)[:300])
        restore_contact = None
        hit, error = leg(
            'value-trip', 'value-released',
            lambda s: alarm_reported_clear('thermal', s)
            and value('thermal', s) is False
            and quality('thermal', s) == 'good'
            and value('protect', s) is False,
            ('thermal', 'protect', 'thermal_alarm', 'thermal_unack'))
        if error:
            return error
        case.observe('the released value cleared the condition at tick '
                     + str(hit.get('tick')) + ' while the latch stood')
        error = acknowledge('thermal', 'value-trip', 'value')
        if error:
            return error
        error = field_cmd(True, 'value-trip', 'value-restored')
        if error:
            return error

        # -- the second honest-absence half: the pump stopped. The hand
        # request releases while the manual selection stands, so the
        # auto leg stays gated and the pump cannot start under any
        # demand.
        error = write_point(points['hand'], False, 'hand-release')
        if error:
            return error
        hit, error = leg('stopped', 'stopped',
                         lambda s: value('hand', s) is False
                         and value('mode', s) is True
                         and value('protect', s) is False,
                         ('hand', 'mode', 'protect'))
        if error:
            return error
        error = field_cmd(False, 'stopped', 'stopped')
        if error:
            return error
        case.observe('the hand request released at tick '
                     + str(hit.get('tick')) + ' — p101 stopped under '
                     'the standing manual selection')

        # -- drive 3: the degraded contact on the stopped pump: the
        # named cause alarm still annunciates, the pump does not start,
        # and nothing beyond the declared contract plants an alarm.
        error = degrade('moisture', 'stopped-moisture',
                        'stopped-moisture')
        if error:
            return error
        case.observe('the stopped pump stayed stopped under the '
                     'degraded contact — no spurious start')
        error = recover('moisture', 'stopped-moisture',
                        'stopped-moisture', False)
        if error:
            return error

        # -- the restore: the manual selection released, every contact
        # healthy and clear, every declared surface at its baseline.
        error = write_point(points['mode'], False, 'mode-restore')
        if error:
            return error
        hit, error = leg('baseline', 'restored', baseline, baseline_keys)
        if error:
            return error
        case.observe('restored at tick ' + str(hit.get('tick')) + ': the '
                     'auto posture, both contacts healthy and clear, '
                     'every cause alarm\'s surface clean')
        for tag in CONTACTS:
            if not contact_clear(tag):
                return case.finish(
                    'failed', 'cause-alarm-quality-failed: the p101-'
                    + tag + ' contact did not restore — it reads '
                    + json.dumps(field_value(points[tag]))[:300])

        # -- the durable audit: each declared journaled point's
        # transition sequence exactly, each contact's quality sequence
        # exactly, the annunciation ordered beside the trip it explains
        # within the guard's one-hop carrier crossing, every receipted
        # write settled applied and attributed, no role change, and no
        # transition on a point the model leaves unjournaled.
        bad = {'bad': CAUSE_ALARM_FAULT.split(':')[1]}
        expected = {
            'protect': [TRUE, FALSE] * len(DRIVES),
            'protect_ok': [FALSE, TRUE] * len(DRIVES),
            'mode': [TRUE, FALSE],
            'thermal': [TRUE, FALSE],
            'moisture': [],
            'thermal_alarm': [TRUE, FALSE] * 2,
            'thermal_unack': [TRUE, FALSE] * 2,
            'moisture_alarm': [TRUE, FALSE] * 2,
            'moisture_unack': [TRUE, FALSE] * 2,
            'fault': [], 'fault_alarm': [], 'fault_unack': []}
        qualities = {'thermal': [bad, 'good'],
                     'moisture': [bad, 'good', bad, 'good']}
        for tag in CONTACTS:
            for label, _ in MANAGED_FLAGS:
                expected[tag + '_' + label] = []
                expected['fault_' + label] = []
        quiet = {points[key] for key in expected
                 if key.split('_')[-1] in ('shelved', 'suppressed', 'oos')}
        unjournaled = {points['cmd'], points['hand'],
                       points['thermal_ok'], points['moisture_ok'],
                       points['thermal_ack'], points['moisture_ack'],
                       points['thermal_alarm_in'],
                       points['moisture_alarm_in']}
        got = {}
        violations = {}
        complete = set()

        def journaled():
            try:
                _, journal = http_json('GET', base + '/journal?since='
                                       + str(floor))
            except Exception:
                return None
            last['journal'] = journal
            for key, wanted in expected.items():
                landed = _journaled_values(journal, points[key])
                got[key] = landed
                if landed == wanted:
                    complete.add(key)
                elif len(landed) > len(wanted) \
                        or landed != wanted[:len(landed)]:
                    violations['transitions-' + key] = (
                        'point ' + str(points[key]) + ' journaled '
                        + json.dumps(landed) + ' — not the declared '
                        'transition sequence')
            for tag in CONTACTS:
                landed = _journaled_qualities(journal, points[tag])
                got[tag + ':quality'] = landed
                if landed == qualities[tag]:
                    complete.add(tag + ':quality')
                elif len(landed) > len(qualities[tag]) \
                        or landed != qualities[tag][:len(landed)]:
                    violations['quality-' + tag] = (
                        'the p101-' + tag + ' contact journaled the '
                        'quality sequence ' + json.dumps(landed)
                        + ' — not the declared injection and recovery '
                        'pairs')
            for entry in _journal_list(journal):
                event = entry.get('event') or {}
                if 'role_changed' in event:
                    violations['role-change'] = (
                        'a role_changed event journaled under the '
                        'quality-aware cause-alarm drive')
                change = event.get('point_changed')
                if isinstance(change, dict) \
                        and change.get('point') in unjournaled:
                    violations['unjournaled-' + str(change['point'])] = (
                        'the unjournaled point ' + str(change['point'])
                        + ' journaled a transition')
            # The annunciation beside the trip it explains: each drive's
            # cause alarm asserts on its own trip's reading, at most the
            # guard's declared one-hop carrier crossing later.
            for name_, _stem, tag, trip_at, said_at in DRIVE_PAIRS:
                trip = _journal_tick(journal, points['protect'], 'value',
                                     TRUE, trip_at)
                said = _journal_tick(journal, points[tag + '_alarm'],
                                     'value', TRUE, said_at)
                if trip is None or said is None:
                    violations['annunciation-' + name_] = (
                        'the durable journal carries no trip and '
                        'annunciation pair for the ' + name_
                        + ' drive — trip ' + json.dumps(trip)
                        + ', alarm ' + json.dumps(said))
                elif not trip <= said <= trip + CAUSE_ALARM_STEP:
                    violations['annunciation-' + name_] = (
                        'the ' + name_ + ' cause alarm annunciated at '
                        'tick ' + str(said) + ' against its trip at tick '
                        + str(trip) + ' — outside the declared one-hop '
                        'carrier crossing')
            for point in sorted(quiet):
                landed = _journaled_values(journal, point)
                if landed:
                    violations['quiet-' + str(point)] = (
                        'the declared-quiet point ' + str(point)
                        + ' journaled ' + json.dumps(landed))
            if len(complete) == len(expected) + len(qualities) \
                    or violations:
                return journal
            return None

        wait_for(journaled, time.monotonic() + CAUSE_ALARM_DEADLINE,
                 interval=CAUSE_ALARM_POLL)
        journal = last.get('journal') or []
        annunciations = {}
        for name_, _stem, tag, trip_at, said_at in DRIVE_PAIRS:
            annunciations[name_] = [
                _journal_tick(journal, points['protect'], 'value', TRUE,
                              trip_at),
                _journal_tick(journal, points[tag + '_alarm'], 'value',
                              TRUE, said_at)]
        note('journal', 'the journaled drives above the pre-drive floor',
             journal={'floor': floor, 'complete': sorted(complete),
                      'transitions': {key: got.get(key)
                                      for key in sorted(got)},
                      'annunciations': annunciations,
                      'violations': sorted(violations)})
        if violations:
            return case.finish(
                'failed', 'cause-alarm-quality-nondeterministic: '
                + '; '.join(violations[key] for key in sorted(violations)))
        missing = sorted(key for key in expected
                         if key not in complete)
        missing += [key for key in qualities if key not in complete]
        if missing:
            return case.finish(
                'failed', 'cause-alarm-quality-failed: the served '
                'journal never recorded the declared evidence on: '
                + ', '.join(missing))

        # The receipts: every submitted write settled applied and
        # attributed under the leg's actor.
        for index, (tag, body) in enumerate(submitted):
            matches = [receipt for receipt in _settled_receipts(journal)
                       if (receipt.get('command') or {})
                       .get('write_value') == body]
            ordinal = sum(1 for _, other in submitted[:index + 1]
                          if other == body)
            if len(matches) < ordinal:
                return case.finish(
                    'failed', 'cause-alarm-quality-failed: no settled '
                    'receipt journaled for the ' + tag + ' write '
                    + json.dumps(body)[:200])
            receipt = matches[ordinal - 1]
            if receipt.get('actor') != CAUSE_ALARM_ACTOR:
                return case.finish(
                    'failed', 'cause-alarm-quality-failed: the ' + tag
                    + ' settle lost its actor: '
                    + json.dumps(receipt)[:200])
            if 'applied' not in (receipt.get('outcome') or {}):
                return case.finish(
                    'failed', 'cause-alarm-quality-failed: the ' + tag
                    + ' write did not settle applied: '
                    + json.dumps(receipt)[:200])
        case.observe('journaled: both contacts\' injected qualities and '
                     'recoveries, every trip and annunciation pair, and '
                     'every receipted write — applied and attributed')

        # -- the record audit: the pair's roles never moved — a field
        # fault is a plant event, not a failover.
        if _settled_active(ctx) != active:
            return case.finish('failed', 'cause-alarm-quality-failed: '
                               'the active role moved under the '
                               'quality-aware cause-alarm drive')
        if peer_base is not None:
            report = _try_role(ctx, peer_base)
            if report is None \
                    or report.get('role') \
                    != (peer_role0 or {}).get('role'):
                return case.finish(
                    'failed', 'cause-alarm-quality-failed: the pair\'s '
                    'roles moved during the leg — ' + peer + ' reports '
                    + json.dumps(report))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
    finally:
        # The injected qualities are the run's shared field and the
        # driven manual selection and hand request are the pump's
        # service posture: a leg leaving either standing poisons every
        # later leg. The faults clear, the hand request and manual
        # selection release, every held ack re-arms, and any
        # unacknowledged latch the drives left standing is
        # acknowledged best-effort.
        for tag in list(faulted):
            try:
                _plant_ctl(ctx, 'clear-fault', str(points[tag]))
            except Exception:
                pass
        base_ = live['base']
        if base_ is not None and points:
            for key in ('hand', 'mode'):
                try:
                    http_json('POST', base_ + '/command',
                              {'command': {'write_value': {
                                  'point': points[key], 'kind': 'bool',
                                  'value': {'bool': False}}},
                               'actor': CAUSE_ALARM_ACTOR})
                except Exception:
                    pass
            for point in held_acks:
                try:
                    http_json('POST', base_ + '/command',
                              {'command': {'write_value': {
                                  'point': point, 'kind': 'bool',
                                      'value': {'bool': False}}},
                               'actor': CAUSE_ALARM_ACTOR})
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
                                   'actor': CAUSE_ALARM_ACTOR})
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
                                   'actor': CAUSE_ALARM_ACTOR})
                except Exception:
                    pass
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