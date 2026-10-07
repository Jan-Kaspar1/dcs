"""The cyclic-failover settle leg — one module per leg of the scenario
schedule; see qa_lane/scenarios/__init__.py for the ordering rule and
the shared seam."""
from .common import *

# Ordering: the leg stages the lane's register-protocol device server
# and a born pair on its cyclic document — the same staging the
# exchange-failure legs take — so it runs after them and before the
# reclaim legs take the born seats over again.
RUNS_AFTER = frozenset({'scenario_cyclic_exchange_failure_legs'})
RUNS_BEFORE = frozenset({'scenario_reclaim_convergence_gate'})


# --------------------------------------------------------------------
# The demote/promote failover over the rig's cyclic device — the
# transition the hardware leg's evidence rests on: a promoted peer
# resuming staged-output publication at the exchange boundary while its
# demoted predecessor keeps latching inputs.
#
# The exchange-failure legs (#408) prove what a failed, a short, and a
# recovered exchange do to one peer's outputs, input image, and
# counters. This leg proves the *transition*: the same contract read
# across a role change rather than across a fault, on the rig's own
# cyclic field, through the documented monitor endpoints.
#
# What the rig can observe, and what it therefore asserts:
#
#   * **Publication resumed on the new active.** The promoted peer's
#     exchange counters advance once per boundary from its promotion
#     on, its field claim reads held, and the field's own register
#     census advances over the same window — so what the field holds is
#     being republished by the new active rather than ageing at the
#     frame the ex-owner left behind. A promotion that lifted the write
#     gate without the peer exchanging, or a peer that promoted and
#     then went quiet, fails here.
#   * **No skipped or doubled boundary.** The promoted peer's
#     `attempted - succeeded` and its `failed_exchanges` are unchanged
#     across the window: the switchover costs no exchange and invents
#     none, which is the once-per-boundary accounting the hardware
#     evidence reads.
#   * **Input latching uninterrupted.** Every field input the promoted
#     peer serves keeps a Good quality and an acquisition stamp that
#     advances across the transition — the input phase kept running
#     through the role change.
#   * **The demoted peer keeps latching, stops publishing.** Its role
#     walks to standby, its claim releases, and its exchanges keep
#     advancing on census-only terms: an unfenced failed exchange on
#     that seat after the demotion fails the leg, because a demoted
#     peer that is still writing is a two-writer field.
#
# What the rig cannot observe, and where that half is pinned: whether
# the *first* post-promotion exchange carries the first active scan's
# staged image rather than a stale pre-promotion seed is a question
# about bytes on the wire, and the rig's field census is downstream of
# it. That half is pinned deterministically at the driver seam in
# `crates/dcs-controller/tests/cyclic_lifecycle.rs`, where a scripted
# cyclic backend records every published image against the peer that
# published it. This leg is the field-observable continuation of the
# same contract, which is what the hardware leg's evidence will rely
# on.
#
# Named diagnostics: cyclic-failover-settle-failed tags the contract
# clauses — a promoted peer that never resumed publishing, an exchange
# skipped or double-counted across the transition, an input whose
# latching stopped, a demoted peer still writing, and a field whose
# registers stopped moving — while cyclic-failover-settle-nondeterministic
# tags the instability the contract does not answer for: a refused
# staging, launch, or control-plane call, a pair that never settled or
# never reconverged, a starved or unread monitor, a device server that
# stopped answering, an unreadable journal, a rig the sweep did not
# restore, and two passes whose digests diverge. The
# unchecked-diagnostic self-check replays the judge over planted
# negatives and reports cyclic-failover-settle-unchecked for any that
# slip through.

CFS_OWNER_SEAT = 'driven'      # the born seat launched field-active
CFS_PEER_SEAT = 'foreign'      # the born seat launched as the tracking member
CFS_CYCLIC_KIND = 'sim-cyclic'  # the device kind this contract is about

CFS_LAUNCH_BOUND = 90.0        # bound on the staged pair's settle
CFS_SWITCH_BOUND = 30.0        # bound on each documented switch step
CFS_WINDOW_BOUND = 20.0        # bound on the post-promotion window
CFS_WINDOW_POLL = 0.2
CFS_SETTLE_POLL = 0.5
CFS_RESTORE_BOUND = 90.0

CFS_CLAUSE = 'cyclic-failover-settle-failed'
CFS_NONDET = 'cyclic-failover-settle-nondeterministic'
CFS_UNCHECKED = 'cyclic-failover-settle-unchecked'


def _cfs_count(value):
    """A served counter as an int, or None — a payload carrying no
    integer where the contract reads one is a shape the judge reports,
    never a comparison that silently passes."""
    return value if isinstance(value, int) and not isinstance(value, bool) \
        else None


def _cfs_sync(value):
    """The served StandbySync's variant name — a bare string or a
    single-key object, depending on the payload shape."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and value:
        return next(iter(value))
    return None


def _cfs_tracking(report):
    """Whether a served role report reads as a tracking standby."""
    return isinstance(report, dict) and 'tracking' in (report.get('sync') or {})


def _cfs_view(ctx, seat):
    """One normalized read of a seat's role, claim, run tick, and sync
    verdict — or None when the read dropped."""
    report = _try_role(ctx, ctx.get(seat))
    if not isinstance(report, dict):
        return None
    return {'role': report.get('role'), 'tick': report.get('tick'),
            'claim': report.get('field_claim'),
            'sync': _cfs_sync(report.get('sync'))}


def _cfs_io(snapshot):
    """The io_health half this contract reads, normalized: the
    boundary counters, the transport's link verdict, and the cyclic
    exchange counters. None while unread."""
    if not isinstance(snapshot, dict):
        return None
    health = snapshot.get('io_health')
    if not isinstance(health, dict):
        return None
    driver = health.get('driver') or {}
    exchange = driver.get('exchange') or {}
    return {
        'failed_exchanges': health.get('failed_exchanges'),
        'failed_reads': health.get('failed_reads'),
        'consecutive_failures': health.get('consecutive_failures'),
        'link': driver.get('link'),
        'attempted': exchange.get('attempted'),
        'succeeded': exchange.get('succeeded'),
        'working_counter_mismatches': exchange.get(
            'working_counter_mismatches'),
        'missed_deadlines': exchange.get('missed_deadlines'),
        'last_error': driver.get('last_error')}


def _cfs_degraded(sample):
    """How one served sample's quality reads: `good`, `bad`, or
    `unknown` for a shape this contract cannot read."""
    if not isinstance(sample, dict):
        return 'unknown'
    quality = sample.get('quality')
    if quality in ('good', 'Good'):
        return 'good'
    if isinstance(quality, str):
        return 'bad'
    if isinstance(quality, dict):
        return 'bad'
    return 'unknown'


def _cfs_inputs(snapshot):
    """Every field input's served sample out of one snapshot, keyed by
    point id — the latching half each window reads. None while
    unread."""
    if not isinstance(snapshot, dict):
        return None
    points = snapshot.get('points')
    if not isinstance(points, list):
        return None
    out = {}
    for entry in points:
        if not isinstance(entry, dict):
            continue
        sample = entry.get('sample')
        if isinstance(sample, dict):
            out[str(entry.get('point'))] = sample
    return out


def _cfs_field(ctx):
    """Stage the lane's register-protocol device server on the run
    config's cyclic model and prove the field it serves is the one this
    contract is about: a `sim-cyclic` device with an output channel —
    a device with no outputs stages no image, and there is nothing for
    a promotion to resume publishing.

    Returns the launch dict, or the inconclusive reason as a string. A
    launch that raises is no inconclusive reason but a refused staging
    call: it propagates so the pass records it as the instability it
    is."""
    spec = ctx.get('sim_bus_device')
    if not spec:
        return ('the run config stages no sim-bus device server — this '
                'leg needs the lane\'s register-protocol field')
    cyclic = spec.get('cyclic_model')
    if not cyclic:
        return ("the run config's sim_bus_device block stages no "
                'cyclic_model — the fixture declaring the ' + CFS_CYCLIC_KIND
                + ' device this leg\'s field must be')
    field = ctx['start_sim_bus_device'](cyclic)
    document = field.get('model')
    try:
        model = json.loads(Path(document).read_text())
    except (OSError, ValueError, TypeError) as exc:
        return 'the staged device model is unreadable: ' + str(exc)[:200]
    for declared in model.get('devices') or []:
        if declared.get('id') != spec.get('device'):
            continue
        if declared.get('kind') != CFS_CYCLIC_KIND:
            return ('the staged field serves device '
                    + str(spec.get('device')) + ' as '
                    + repr(declared.get('kind')) + ' — point '
                    'sim_bus_device.cyclic_model at a model declaring a '
                    + CFS_CYCLIC_KIND + ' device for this leg')
        outputs = sorted(name for name, channel
                         in (declared.get('channels') or {}).items()
                         if (channel or {}).get('direction') == 'out')
        if not outputs:
            return ('the staged ' + CFS_CYCLIC_KIND + ' device declares no '
                    'output channel — its scan stages no image, and a '
                    'promotion has nothing to resume publishing')
        field['outputs'] = outputs
        return field
    return ('the staged device model declares no device '
            + str(spec.get('device')))


def _cfs_settle(ctx):
    """Wait for the staged pair's documented settle: the launch-active
    seat reporting active with the device claim held, the other member
    a tracking standby. Returns {'owner', 'peer'} or the inconclusive
    reason under 'inconclusive'."""
    deadline = time.monotonic() + CFS_LAUNCH_BOUND
    owner = wait_for(
        lambda: (lambda seen: seen if seen is not None
                 and seen.get('role') == 'active'
                 and seen.get('claim') == 'held' else None)(
                     _cfs_view(ctx, CFS_OWNER_SEAT)),
        deadline, interval=CFS_SETTLE_POLL)
    if owner is None:
        return {'inconclusive': 'the launched active never settled with '
                     'the device claim held — the staged pair did not '
                     'come up'}
    peer = wait_for(
        lambda: (lambda seen: seen if seen is not None
                 and seen.get('role') == 'standby'
                 and seen.get('sync') == 'tracking' else None)(
                     _cfs_view(ctx, CFS_PEER_SEAT)),
        deadline, interval=CFS_SETTLE_POLL)
    if peer is None:
        return {'owner': owner,
                'inconclusive': "the pair's tracking member never "
                     'converged — the promote this leg drives has no '
                     'converged target'}
    return {'owner': owner, 'peer': peer}


def _cfs_registers(ctx):
    """The field's own register census through the shipped control tool
    — what the bus actually holds. A tool that answered nothing is a
    device no longer serving."""
    try:
        result = ctx['sim_bus_ctl']('list')
    except Exception as exc:
        return {'read': False,
                'detail': 'the shipped control tool never ran: '
                          + str(exc)[:200]}
    code = getattr(result, 'returncode', None)
    answer = str(getattr(result, 'stdout', '') or '').strip()
    samples = []
    if code == 0 and '"registers"' in answer:
        try:
            served = json.loads(answer)
        except ValueError:
            served = None
        for entry in (served or {}).get('registers') or []:
            if not isinstance(entry, dict):
                continue
            sample = entry.get('sample') or {}
            samples.append({'register': entry.get('register'),
                            'value': sample.get('value'),
                            'tick': sample.get('tick')})
    return {'read': bool(samples) or code == 0, 'exit': code,
            'samples': samples, 'answer': answer[-400:]}


def _cfs_switch(ctx, seat, verb):
    """One documented control-plane switch — `POST /demote` or
    `POST /promote` — answering (status, body) with a refused call's
    named outcome decoded rather than raised."""
    return _settle_call(ctx[seat] + '/' + verb)


def _cfs_window(ctx, seat, key, before):
    """The first snapshot of `seat` whose named counter has moved past
    `before`, with the io_health reading and the field-input samples
    taken out of **that one snapshot**, so a counter and the samples it
    explains are always from the same scan. `read` says whether the
    monitor answered at all inside the window, which separates a starved
    surface from a peer whose counter stood still."""
    deadline = time.monotonic() + CFS_WINDOW_BOUND
    seen, points, answered = None, None, False
    while time.monotonic() < deadline:
        snapshot = _try_snapshot(ctx, ctx[seat])
        io = _cfs_io(snapshot)
        if io is not None:
            answered = True
            count = _cfs_count(io.get(key))
            prior = _cfs_count((before or {}).get(key))
            if count is not None and (prior is None or count > prior):
                seen = io
                points = _cfs_inputs(snapshot)
                break
        time.sleep(CFS_WINDOW_POLL)
    return {'io': seen, 'read': answered, 'points': points}


def _cfs_journal(path):
    """The journal entries a peer's --journal-file carries, as the
    role-change walk this leg reads: `(run, tick, from, to, origin)`
    per `role_changed` record. None when the file cannot be read."""
    if not path or not Path(path).is_file():
        return None
    events = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        event = record.get('event') or {}
        change = event.get('role_changed')
        if not isinstance(change, dict):
            continue
        events.append({'run': record.get('run'), 'tick': record.get('tick'),
                       'from': change.get('from'), 'to': change.get('to'),
                       'origin': change.get('origin')})
    return events


def _cfs_role_walk(events):
    """The role walk the transition journalled on one peer's own
    record: the `from -> to` pairs in file order, and the entries whose
    origin names the request that drove them rather than the run's own
    startup or an armed failover."""
    walk = [(entry.get('from'), entry.get('to')) for entry in events
            or [] if isinstance(entry, dict)]
    driven = [entry for entry in events or []
              if isinstance(entry, dict)
              and entry.get('origin') not in (None, 'failover', 'startup')]
    return {'walk': walk, 'driven': driven}


def _cfs_teardown(ctx):
    """Best-effort teardown: the pair's born seats and the device
    server."""
    for seat in (CFS_OWNER_SEAT, CFS_PEER_SEAT):
        lever = ctx.get('stop_born_controller')
        if lever is not None:
            try:
                lever(seat)
            except Exception:
                pass
    try:
        if ctx.get('stop_sim_bus_device') is None:
            return None
        ctx['stop_sim_bus_device']()
    except Exception as exc:
        return str(exc)[:200]
    return None


def _cfs_rig_state(ctx, device_error):
    """The rig's claim state after the sweep."""
    state = ctx.get('born_controller_state')
    seats = {}
    for seat in (CFS_OWNER_SEAT, CFS_PEER_SEAT):
        try:
            seats[seat] = (state(seat) or {}).get('absent') \
                if state is not None else None
        except Exception:
            seats[seat] = None
    return {'seats': seats, 'device_error': device_error}


def _cfs_delta(after, before, key):
    """`after[key] - before[key]` when both are readable counters, else
    None — a window the judge reports as an unread difference rather
    than as a zero drift."""
    left = _cfs_count((after or {}).get(key))
    right = _cfs_count((before or {}).get(key))
    if left is None or right is None:
        return None
    return left - right


def _judge_cfs(record, note):
    """Replay one pass's record — runnable against planted negatives in
    the self-check."""
    def failed(key, detail):
        note(key, CFS_CLAUSE, detail)

    def nondet(key, detail):
        note(key, CFS_NONDET, detail)

    if record.get('stage_error') is not None:
        nondet('stage', 'the device server, the pair launch, or a '
               'control-plane call never completed: '
               + str(record['stage_error']))
        return
    if record.get('switch_error') is not None:
        nondet('switch', 'the documented switch never completed: '
               + str(record['switch_error']))
        return
    demote = record.get('demote') or {}
    promote = record.get('promote') or {}
    window = record.get('window') or {}
    owner_window = record.get('owner_window') or {}
    before = record.get('promoted_io') or {}
    fields = record.get('registers') or {}

    # The switch itself: both documented steps answered, and the roles
    # moved where the leg asked them to.
    if demote.get('status') != 200:
        nondet('demote-refused', 'POST /demote on the active answered '
               + json.dumps(demote)[:300])
    if promote.get('status') != 200:
        nondet('promote-refused', 'POST /promote on the standby answered '
               + json.dumps(promote)[:300])
    if demote.get('status') != 200 or promote.get('status') != 200:
        # A refused switch step never moved the roles, so what the
        # transition contract reads below is not evidence about it.
        return
    if (record.get('owner_after') or {}).get('role') != 'standby':
        failed('demote-not-landed', 'the demoted peer must settle as a '
               'standby: ' + json.dumps(record.get('owner_after'))[:300])
    if (record.get('promoted_after') or {}).get('role') != 'active':
        failed('promote-not-landed', 'the promoted peer must settle as '
               'the active: '
               + json.dumps(record.get('promoted_after'))[:300])
    if (record.get('promoted_after') or {}).get('claim') != 'held':
        failed('claim-not-taken', 'the promoted peer must hold the '
               'device claim it published against: '
               + json.dumps(record.get('promoted_after'))[:300])

    # Publication resumed on the new active.
    if window.get('read') is not True:
        nondet('window-unreadable', "the promoted peer's monitor "
               'answered no io_health read inside the post-promotion '
               'window: ' + json.dumps(window)[:200])
        return
    if window.get('io') is None:
        # The monitor answered and the counter never moved: the
        # promoted peer exchanged nothing at all, which is the clause
        # this leg exists to catch rather than an unread window.
        failed('no-resume', 'the promoted peer must resume its '
               'exchanges at the boundary after its promotion — its '
               'exchange counter never moved inside the window although '
               'its monitor answered: ' + json.dumps(window)[:300])
        return
    if (window.get('io') or {}).get('link') != 'connected':
        failed('promoted-link-down', 'the promoted peer\'s link must read '
               'connected while it is publishing: '
               + json.dumps(window.get('io'))[:300])
    if _cfs_delta(window.get('io'), before, 'attempted') is None:
        failed('promoted-unreadable', 'the promoted peer\'s exchange '
               'counters must be readable across the transition: '
               + json.dumps(window.get('io'))[:300])
    elif _cfs_delta(window.get('io'), before, 'attempted') < 1:
        failed('no-resume', 'the promoted peer must resume its '
               'exchanges at the boundary after its promotion — the '
               'counters advanced by '
               + str(_cfs_delta(window.get('io'), before, 'attempted'))
               + ': ' + json.dumps(window.get('io'))[:300])

    # Once per boundary: no exchange skipped, none invented, and no new
    # failure counted at the switchover.
    gaps = _cfs_delta(window.get('io'), before, 'attempted')
    shortfalls = _cfs_delta(window.get('io'), before, 'succeeded')
    if gaps is not None and shortfalls is not None \
            and gaps != shortfalls:
        failed('boundary-skipped', 'every boundary the promoted peer '
               'attempted must complete — ' + str(gaps - shortfalls)
               + ' of ' + str(gaps) + ' exchanges went uncounted: '
               + json.dumps({'attempted': gaps,
                             'succeeded': shortfalls})[:300])
    if _cfs_delta(window.get('io'), before, 'failed_exchanges') != 0:
        failed('switchover-counted-a-failure', 'the switchover must cost '
               'no failed exchange — the counters read '
               + str(_cfs_delta(window.get('io'), before, 'failed_exchanges'))
               + ': ' + json.dumps(window.get('io'))[:300])

    # Input latching uninterrupted across the transition.
    points = window.get('points') or {}
    if not points:
        nondet('points-unreadable', 'the post-promotion window carried '
               'no field input sample: ' + json.dumps(window)[:200])
    else:
        degraded = sorted(name for name, sample in points.items()
                          if _cfs_degraded(sample) != 'good')
        if degraded:
            failed('latching-degraded', 'every field input must keep '
                   'serving Good across the switchover, but '
                   + json.dumps(degraded) + ' did not: '
                   + json.dumps(points, sort_keys=True)[:400])
        stale = sorted(name for name, sample in points.items()
                       if _cfs_count(sample.get('tick')) is None)
        if stale:
            failed('latching-unstamped', 'a latched sample must carry '
                   'the acquisition stamp it was read at: '
                   + json.dumps(stale))

    # The field's own registers moved: publication is live, not held at
    # the frame the ex-owner left behind.
    if fields.get('before', {}).get('read') is not True \
            or fields.get('after', {}).get('read') is not True:
        nondet('field-unreadable', 'the field\'s register census never '
               'read across the transition: ' + json.dumps(fields)[:300])
    elif fields.get('before', {}).get('samples') == \
            fields.get('after', {}).get('samples'):
        failed('field-frozen', 'the promoted peer must republish the '
               'field at every boundary — the register census is '
               'identical before the switch and after it: '
               + json.dumps(fields.get('after'))[:300])

    # The demoted peer keeps latching and stops publishing: census-only
    # exchanges, no fencing, no claim.
    if owner_window.get('read') is not True:
        nondet('owner-window-unreadable', "the demoted peer's monitor "
               'answered no io_health read inside the window: '
               + json.dumps(owner_window)[:200])
    else:
        owner_io = owner_window.get('io') or {}
        if _cfs_delta(owner_io, record.get('owner_io'), 'attempted') in (None, 0):
            failed('demoted-stopped-latching', 'the demoted peer must '
                   'keep latching inputs — its exchanges advanced by '
                   + str(_cfs_delta(owner_io, record.get('owner_io'),
                                'attempted')) + ': '
                   + json.dumps(owner_io)[:300])
        if owner_io.get('link') != 'connected':
            failed('demoted-fenced', 'a demoted peer exchanges '
                   'census-only: it must not meet a fence, so its link '
                   'must read connected — it reads '
                   + str(owner_io.get('link')) + ': '
                   + json.dumps(owner_io)[:300])
        if _cfs_delta(owner_io, record.get('owner_io'), 'failed_exchanges') != 0:
            failed('demoted-counted-a-failure', 'the demoted peer\'s '
                   'census-only exchanges must not fail: '
                   + json.dumps(owner_io)[:300])
    if (record.get('owner_after') or {}).get('claim') == 'held':
        failed('claim-not-released', 'the demoted peer must release the '
               'device claim it gave up: '
               + json.dumps(record.get('owner_after'))[:300])

    # The transition is journaled on the promoted peer's own record, and
    # the driver asked for it.
    journal = record.get('journal') or {}
    if journal.get('error') is not None:
        nondet('journal-unreadable', 'a peer\'s --journal-file could not '
               'be read: ' + str(journal.get('error')))
    else:
        walk = (journal.get('promoted') or {}).get('walk') or []
        if not any(to == 'active' for _frm, to in walk):
            failed('promote-unjournaled', 'the promoted peer must '
                   'journal its own promotion — its role walk reads '
                   + json.dumps(walk)[:300])
        driven = (journal.get('promoted') or {}).get('driven') or []
        if not driven:
            failed('promote-unattributed', 'the promotion must be '
                   'attributed to the request that drove it: '
                   + json.dumps((journal.get('promoted') or {})
                                .get('driven'))[:300])
        owner_walk = (journal.get('demoted') or {}).get('walk') or []
        if not any(to == 'standby' for _frm, to in owner_walk):
            failed('demote-unjournaled', 'the demoted peer must journal '
                   'its own demotion — its role walk reads '
                   + json.dumps(owner_walk)[:300])

    restore = record.get('restore')
    if isinstance(restore, str):
        nondet('restore', 'the pair never reconverged to its launch '
               'roles: ' + restore)
    rig = record.get('rig') or {}
    if set((rig.get('seats') or {}).values()) != {True} \
            or rig.get('device_error') is not None:
        nondet('rig-not-restored', 'the leg left the rig\'s claim state '
               'standing — a seat or the device server outlived the '
               'sweep the legs behind this one inherit: '
               + json.dumps(rig, sort_keys=True)[:300])


def _cfs_digest(record, violations):
    """The pass's normalized verdict record — identical digests across
    two consecutive passes is the determinism contract."""
    def clean(*keys):
        return not any(key in violations for key in keys)

    def verdict(keys, values, other):
        for value in values:
            if clean(*keys):
                return value
        return other

    return {
        'promotion': verdict(('promote-not-landed', 'no-resume',
                              'promoted-link-down', 'promoted-unreadable'),
                             ['resumed'], 'stalled'),
        'boundaries': verdict(('boundary-skipped',
                               'switchover-counted-a-failure'), ['once'],
                              'drifted'),
        'latching': verdict(('latching-degraded', 'latching-unstamped'),
                            ['continuous'], 'broken'),
        'field': verdict(('field-frozen',), ['republished'], 'frozen'),
        'demoted': verdict(('demote-not-landed', 'demoted-stopped-latching',
                            'demoted-fenced', 'demoted-counted-a-failure',
                            'claim-not-released'), ['census-only'],
                          'writing'),
        'journal': verdict(('promote-unjournaled', 'promote-unattributed',
                            'demote-unjournaled'), ['recorded'],
                           'silent'),
        'rig': 'restored' if clean('rig-not-restored') else 'dirty'}


def _cfs_restore(ctx):
    """The documented demote-then-promote order returning the pair to
    the roles it was launched with, or the reason it never did."""
    deadline = time.monotonic() + CFS_RESTORE_BOUND
    if wait_for(lambda: _cfs_tracking(_try_role(ctx, ctx[CFS_OWNER_SEAT])
                                  or {}), deadline,
                interval=CFS_SETTLE_POLL) is None:
        return ('the demoted peer never rejoined the line as a tracking '
                'standby')
    status, body = _cfs_switch(ctx, CFS_PEER_SEAT, 'demote')
    if status != 200:
        return ('the restore demote answered ' + str(status) + ': '
                + json.dumps(body)[:200])
    status, body = _cfs_switch(ctx, CFS_OWNER_SEAT, 'promote')
    if status != 200:
        return ('the restore promote answered ' + str(status) + ': '
                + json.dumps(body)[:200])
    restored = wait_for(
        lambda: (lambda seen: seen if seen is not None
                 and seen.get('role') == 'active' else None)(
                     _cfs_view(ctx, CFS_OWNER_SEAT)),
        deadline, interval=CFS_SETTLE_POLL)
    if restored is None:
        return 'the restore promote never landed the active role'
    return None


def _cfs_pass(ctx, number):
    """One pass: stage the cyclic field, launch a born pair on it, let
    it settle, demote the active, promote the converged standby, and
    read the post-promotion window from both peers plus the field."""
    record = {'pass': number}
    try:
        field = _cfs_field(ctx)
    except Exception as exc:
        record['stage_error'] = ('the device server never staged: '
                                 + str(exc)[:250])
        return record
    if isinstance(field, str):
        record['inconclusive'] = field
        return record
    record['field'] = {'device': field.get('device'),
                       'port': field.get('port'),
                       'outputs': field.get('outputs'),
                       'model': field.get('model')}
    try:
        for seat, wiring in ((CFS_OWNER_SEAT, {'peer': CFS_PEER_SEAT}),
                             (CFS_PEER_SEAT, {'standby': CFS_OWNER_SEAT})):
            ctx['start_born_controller'](seat, None,
                                         document=field['model'],
                                         **wiring)
    except Exception as exc:
        record['stage_error'] = 'the pair launch never ran: ' \
            + str(exc)[:250]
        return record
    settled = _cfs_settle(ctx)
    if settled.get('inconclusive'):
        record['inconclusive'] = settled['inconclusive']
        return record
    record['settled'] = settled

    journals = ctx.get('journal_files') or {}
    record['journal'] = {'paths': {CFS_OWNER_SEAT: journals.get(CFS_OWNER_SEAT),
                                   CFS_PEER_SEAT: journals.get(CFS_PEER_SEAT)}}
    for path_key, seat in ((CFS_OWNER_SEAT, 'demoted'), (CFS_PEER_SEAT, 'promoted')):
        try:
            record['journal'][seat] = _cfs_role_walk(
                _cfs_journal(journals.get(path_key)))
        except (OSError, ValueError) as exc:
            record['journal']['error'] = str(exc)[:200]

    record['registers'] = {'before': _cfs_registers(ctx)}
    record['demote'] = {'status': None, 'body': None}
    try:
        status, body = _cfs_switch(ctx, CFS_OWNER_SEAT, 'demote')
    except Exception as exc:
        record['switch_error'] = ('the demote never ran: ' + str(exc)[:250])
        return record
    record['demote'] = {'status': status, 'body': body}
    if status != 200:
        return record
    # The baselines for the post-promotion window, read on the promoted
    # peer after the demote settled and before its promote.
    record['promoted_io'] = _cfs_io(_try_snapshot(ctx, ctx[CFS_PEER_SEAT]))
    record['owner_io'] = _cfs_io(_try_snapshot(ctx, ctx[CFS_OWNER_SEAT]))
    try:
        status, body = _cfs_switch(ctx, CFS_PEER_SEAT, 'promote')
    except Exception as exc:
        record['switch_error'] = ('the promote never ran: '
                                  + str(exc)[:250])
        return record
    record['promote'] = {'status': status, 'body': body}
    if status != 200:
        return record

    record['window'] = _cfs_window(ctx, CFS_PEER_SEAT, 'attempted',
                                   record['promoted_io'])
    record['owner_window'] = _cfs_window(ctx, CFS_OWNER_SEAT, 'attempted',
                                         record['owner_io'])
    record['promoted_after'] = _cfs_view(ctx, CFS_PEER_SEAT)
    record['owner_after'] = _cfs_view(ctx, CFS_OWNER_SEAT)
    record['registers']['after'] = _cfs_registers(ctx)
    try:
        for path_key, seat in ((CFS_OWNER_SEAT, 'demoted'),
                               (CFS_PEER_SEAT, 'promoted')):
            record['journal'][seat] = _cfs_role_walk(
                _cfs_journal(journals.get(path_key)))
    except (OSError, ValueError) as exc:
        record['journal']['error'] = str(exc)[:200]
    record['restore'] = _cfs_restore(ctx)
    return record


def _cfs_self_check():
    """The unchecked-diagnostic guard: replay the judge over planted
    negatives and report each that slipped."""
    def io(attempted, succeeded, failed=0, link='connected', streak=0):
        return {'failed_exchanges': failed, 'failed_reads': 0,
                'consecutive_failures': streak, 'link': link,
                'attempted': attempted, 'succeeded': succeeded,
                'working_counter_mismatches': 0, 'missed_deadlines': 0,
                'last_error': None}

    def good(tick):
        return {'value': False, 'quality': 'good', 'tick': tick}

    def bad(tick):
        return {'value': False, 'quality': 'bad', 'tick': tick}

    def registers(tick):
        return {'read': True,
                'samples': [{'register': 0, 'value': False, 'tick': tick}]}

    def walk(to):
        return {'walk': [('standby', to)],
                'driven': [{'run': 1, 'tick': 60, 'from': 'standby',
                            'to': to, 'origin': 'qa-lane'}]}

    def clean_record():
        return {
            'pass': 1,
            'field': {'device': 1, 'port': 9005,
                      'outputs': ['do1', 'do2'],
                      'model': '/run/sim-bus/model.json'},
            'settled': {'owner': {'role': 'active', 'tick': 40,
                                  'claim': 'held', 'sync': None},
                        'peer': {'role': 'standby', 'tick': 40,
                                 'claim': 'unclaimed',
                                 'sync': 'tracking'}},
            'journal': {'paths': {CFS_OWNER_SEAT: '/run/journal-d.jsonl',
                                  CFS_PEER_SEAT: '/run/journal-f.jsonl'},
                        'promoted': walk('active'),
                        'demoted': walk('standby')},
            'registers': {'before': registers(40), 'after': registers(90)},
            'demote': {'status': 200, 'body': {'role': 'demoting'}},
            'promote': {'status': 200, 'body': {'role': 'active'}},
            'promoted_io': io(40, 40),
            'owner_io': io(40, 40),
            'window': {'read': True, 'io': io(90, 90),
                       'points': {'1': good(90), '2': good(90)}},
            'owner_window': {'read': True, 'io': io(140, 140),
                             'points': {'1': good(140)}},
            'promoted_after': {'role': 'active', 'tick': 90,
                               'claim': 'held', 'sync': None},
            'owner_after': {'role': 'standby', 'tick': 140,
                            'claim': 'unclaimed', 'sync': 'tracking'},
            'restore': None,
            'rig': {'seats': {CFS_OWNER_SEAT: True, CFS_PEER_SEAT: True},
                    'device_error': None}}

    def audit(record):
        found = {}
        _judge_cfs(record, lambda key, diagnostic, detail:
               found.setdefault(key, diagnostic))
        return found

    slipped = []
    if audit(clean_record()):
        slipped.append('clean-overstrict')

    def expect(name, mutate, diagnostic=CFS_CLAUSE):
        record = clean_record()
        mutate(record)
        if diagnostic not in audit(record).values():
            slipped.append(name)

    expect('promotion-never-took-the-field',
           lambda r: r['promoted_after'].update(role='standby'))
    expect('promotion-did-not-take-the-claim',
           lambda r: r['promoted_after'].update(claim='unclaimed'))
    expect('promotion-published-nothing',
           lambda r: r['window']['io'].update(attempted=40, succeeded=40))
    expect('promotion-read-an-unlinked-bus',
           lambda r: r['window']['io'].update(link='disconnected'))
    expect('a-boundary-went-uncounted',
           lambda r: r['window']['io'].update(succeeded=89))
    expect('the-switchover-counted-a-failure',
           lambda r: r['window']['io'].update(failed_exchanges=1))
    expect('latching-degraded-across-the-transition',
           lambda r: r['window']['points'].update({'1': bad(90)}))
    expect('latched-sample-unstamped',
           lambda r: r['window']['points'].update({'1': {'value': False,
                                                         'quality': 'good'}}))
    expect('the-field-stopped-moving',
           lambda r: r['registers'].update(before=registers(90),
                                           after=registers(90)))
    expect('the-field-stopped-answering',
           lambda r: r['registers'].update(after={'read': False}),
           CFS_NONDET)
    expect('demotion-never-landed',
           lambda r: r['owner_after'].update(role='active'))
    expect('demotion-kept-the-claim',
           lambda r: r['owner_after'].update(claim='held'))
    expect('the-demoted-peer-stopped-latching',
           lambda r: r['owner_window']['io'].update(attempted=40,
                                                    succeeded=40))
    expect('the-demoted-peer-met-a-fence',
           lambda r: r['owner_window']['io'].update(link='disconnected'))
    expect('the-demoted-peers-census-failed',
           lambda r: r['owner_window']['io'].update(failed_exchanges=2))
    expect('the-promotion-was-unjournaled',
           lambda r: r['journal'].update(promoted={'walk': [('standby',
                                                             'tracking')],
                                                   'driven': [
                                                       {'origin':
                                                        'qa-lane'}]}))
    expect('the-promotion-was-unattributed',
           lambda r: r['journal']['promoted'].update(driven=[]))
    expect('the-demotion-was-unjournaled',
           lambda r: r['journal'].update(demoted={'walk': [], 'driven': []}))
    expect('an-unreadable-journal',
           lambda r: r['journal'].update(error='journal line 4 is not '
                                               'json'), CFS_NONDET)
    expect('a-refused-demote',
           lambda r: r.update(demote={'status': 409,
                                      'body': {'error': 'not_active'}}),
           CFS_NONDET)
    expect('a-refused-promote',
           lambda r: r.update(promote={'status': 409,
                                      'body': {'error': 'not_converged'}}),
           CFS_NONDET)
    expect('a-starved-window',
           lambda r: r['window'].update(read=False, io=None), CFS_NONDET)
    expect('a-starved-demoted-window',
           lambda r: r['owner_window'].update(read=False, io=None), CFS_NONDET)
    expect('unreadable-field-inputs',
           lambda r: r['window'].update(points={}), CFS_NONDET)
    expect('a-launch-failure',
           lambda r: r.update(stage_error='docker run failed'), CFS_NONDET)
    expect('a-switch-call-raised',
           lambda r: r.update(switch_error='the promote never ran'),
           CFS_NONDET)
    expect('the-pair-never-reconverged',
           lambda r: r.update(restore='the restore promote never landed'),
           CFS_NONDET)
    expect('the-rig-stayed-standing',
           lambda r: r['rig'].update(seats={CFS_OWNER_SEAT: False,
                                            CFS_PEER_SEAT: True}), CFS_NONDET)
    expect('the-rig-presence-was-unreadable',
           lambda r: r['rig'].update(seats={CFS_OWNER_SEAT: None,
                                            CFS_PEER_SEAT: None}), CFS_NONDET)
    return slipped


def scenario_cyclic_failover_settle(ctx):
    """Exercise demote-then-promote failover over the rig's cyclic
    device: with a controller pair settled on the staged `sim-cyclic`
    field, demote the active and promote the converged standby, and read
    the transition off both peers and the field — the promoted peer
    resuming publication at the exchange boundary with its claim held,
    one accounted exchange per boundary, input latching uninterrupted,
    the field's own registers moving again, the demoted peer back to
    census-only exchanges with its claim released, and the transition
    journaled on both peers' own records. The pair is restored to its
    launch roles and two consecutive passes produce identical digests.
    """
    case = Case(
        'cyclic-failover-settle',
        'A promoted peer resumes cyclic publication at the exchange '
        'boundary while the demoted peer keeps latching',
        'over the rig\'s staged sim-cyclic field with a controller pair '
        'settled on it: the documented demote-then-promote moves the '
        'roles, and the promoted peer resumes publishing at the '
        'exchange boundary with its device claim held and its link '
        'connected; every boundary it attempts completes and the '
        'switchover costs no failed exchange; every field input keeps '
        'serving Good with a stamped acquisition across the transition; '
        "the field's own register census moves again rather than ageing "
        "at the frame the ex-owner left; the demoted peer settles as a "
        'standby with its claim released, keeps latching on '
        'census-only exchanges, meets no fence and counts no failure; '
        'the promotion and the demotion are journaled on the driving '
        "peer's own record with the request's attribution; the pair is "
        'restored to its launch roles, the rig swept afterward, and two '
        'passes produce identical digests')
    try:
        missing = [key for key in ('start_sim_bus_device',
                                   'stop_sim_bus_device',
                                   'start_born_controller',
                                   'stop_born_controller',
                                   'born_controller_state',
                                   'sim_bus_ctl', 'sim_bus_device',
                                   'evidence_dir')
                   if ctx.get(key) is None]
        if missing:
            return case.finish('inconclusive',
                               'the run stages no capability this leg '
                               'needs: ' + ', '.join(missing))
        digests = []
        for number in (1, 2):
            violations = {}

            def note(key, diagnostic, detail):
                violations.setdefault(key, (diagnostic, detail))

            record = _cfs_pass(ctx, number)
            device_error = _cfs_teardown(ctx)
            record['rig'] = _cfs_rig_state(ctx, device_error)
            if not record.get('inconclusive'):
                _judge_cfs(record, note)
                digest = _cfs_digest(record, violations)
                record['digest'] = dict(digest)
            else:
                digest = None
            record['violations'] = {
                key: diagnostic
                for key, (diagnostic, _) in violations.items()}
            ref = save_evidence(
                ctx['evidence_dir'],
                'cyclic-failover-settle-pass-' + str(number) + '.json',
                record)
            case.evidence('file', ref,
                          'cyclic-failover settle pass ' + str(number)
                          + ' — the staged cyclic field and the born '
                          'pair, the pair\'s settle, the documented '
                          'demote and promote answers, the promoted and '
                          'demoted peers\' post-transition windows with '
                          "the field's own register census beside "
                          'them, both peers\' journaled role walks and '
                          'the file paths they came from, the restored '
                          'pair, the swept rig\'s restoration read, and '
                          'the normalized digest')
            if record.get('inconclusive'):
                return case.finish('inconclusive', record['inconclusive'])
            if violations:
                name = CFS_CLAUSE if any(
                    diagnostic == CFS_CLAUSE
                    for diagnostic, _ in violations.values()) else CFS_NONDET
                return case.finish(
                    'failed', name + ': ' + '; '.join(
                        detail for _, detail
                        in list(violations.values())[:4]))
            digests.append(digest)
        if digests[0] != digests[1]:
            return case.finish(
                'failed', CFS_NONDET + ": the two passes' digests diverged: "
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two cyclic-failover settle passes, identical '
                     'digests: ' + json.dumps(digests[0], sort_keys=True))
        slipped = _cfs_self_check()
        if slipped:
            return case.finish('failed', CFS_UNCHECKED + ': planted negatives '
                               'slipped the leg’s own audits: '
                               + ', '.join(slipped))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc)[:400])
