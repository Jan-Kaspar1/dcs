"""The cyclic-exchange failure legs acceptance leg — one module per leg
of the scenario schedule; see qa_lane/scenarios/__init__.py for the
ordering rule and the shared seam."""
from .common import *

from qa_lane import rig_model

# Ordering: the leg stages the lane's own register-protocol device server
# and one born seat, the same staging the scripted-miss claim-hold leg
# takes, so it runs after it and before the reclaim legs take the born
# seats over again.
RUNS_AFTER = frozenset({'scenario_sim_bus_scripted_miss_claim_hold'})
RUNS_BEFORE = frozenset({'scenario_reclaim_convergence_gate'})


# --------------------------------------------------------------------
# The cyclic-exchange failure legs — the per-revision lane evidence for
# decision 78's exchange semantics above the register protocol: what a
# failed, a short, and a recovered exchange do to the rig's scan
# outputs, its input image, and its exchange counters.
#
# The four legs, each queued through the shipped `dcs-sim-bus-ctl` the
# revision under test carries, and each read off the served snapshot the
# rig's own monitoring surface serves:
#
#   * **Aged, not aborted.** One scripted `miss` must not abort the
#     scan: the exchange counters advance on both halves, the held
#     input image still answers with its own value at its own
#     acquisition stamp, and the scan's output image — what the field's
#     register bank actually holds — does not move. A driver that
#     dropped the scan, or that published a zeroed image, fails here.
#   * **Threshold escalation.** Further misses up to the declared
#     `exchange_miss_threshold` must escalate the reads rather than
#     serve a stale value forever: past the declared threshold every
#     field input reports Bad with the communication-fault reason, and
#     the transport's link verdict reads disconnected. Reading one
#     before the threshold must still serve the held image — the
#     escalation is at the declared count, not at the first miss.
#   * **Per-station attribution.** A scripted `short-station` must
#     degrade only the named station's points while the rest of the
#     image latched: the coupler station's input stays Good and fresh
#     beside the degraded derived station's. One station cannot express
#     the distinction — every point would belong to it — so the leg
#     stages a lane-derived two-station model (`qa_lane/rig_model.py`)
#     and declines as inconclusive on a device that declares fewer.
#   * **Recovery re-entry.** A following scripted `complete` must
#     re-enter on the exchange boundary alone: the link returns to
#     connected, the failure streak returns to zero, the points serve
#     Good again, and the counters account for exactly what happened —
#     `attempted - succeeded` equal to the number of queued misses,
#     `working_counter_mismatches` advanced once by the shortfall leg.
#
# The counters are read as *differences* across each window rather than
# as absolutes, so a busy rig cannot make a leg pass by accident and a
# driver that double-counts a boundary shows up as a drift.
#
# Named diagnostics: cyclic-exchange-legs-failed tags the contract
# clauses — a missed exchange that moved the scan's outputs, a
# threshold that escalated early or not at all, a shortfall that
# degraded the wrong points, a recovery that did not re-enter, and
# counters that do not reflect the boundary events — while
# cyclic-exchange-legs-nondeterministic tags the instability the
# contract does not answer for: a refused staging, launch, or
# control-tool call, a seat that never settled, a starved or unread
# monitor, a device server that stopped answering, a moved or wedged
# deployed pair, a rig the sweep did not restore, and two passes whose
# digests diverge. The unchecked-diagnostic self-check replays the
# judge over planted negatives and reports
# cyclic-exchange-legs-unchecked for any that slip through.

LEGS_SEAT = 'driven'                 # the born seat the leg launches
LEGS_CYCLIC_KIND = 'sim-cyclic'      # the device kind the exchange rides
STATION = '750-354'             # the coupler station the fixture declares
LEGS_DERIVED_STATION = rig_model.DERIVED_STATION
#: The rig's coupler-station input point — the point a station-attributed
#: shortfall must leave fresh.
LEGS_COUPLER_INPUT = 1
#: The declared `exchange_miss_threshold` in the rig's cyclic fixture.
LEGS_MISS_THRESHOLD = 3

LEGS_SETTLE_BOUND = 90.0
LEGS_BOUND = 45.0
LEGS_SETTLE_POLL = 0.5
LEGS_POLL = 0.2

LEGS_CLAUSE = 'cyclic-exchange-legs-failed'
LEGS_NONDET = 'cyclic-exchange-legs-nondeterministic'
LEGS_UNCHECKED = 'cyclic-exchange-legs-unchecked'


def _legs_count(value):
    """A served counter as an int, or None — a payload carrying no
    integer where the contract reads one is a shape the judge reports,
    never a comparison that silently passes."""
    return value if isinstance(value, int) and not isinstance(value, bool) \
        else None


def _legs_io(snapshot):
    """The io_health half this contract reads, normalized: the boundary
    counters, the transport's link verdict, the cyclic exchange
    counters including the working-counter mismatches and the missed
    deadlines the shortfall and late legs move, and the per-bus rows
    that keep each counter attributable. None while unread."""
    if not isinstance(snapshot, dict):
        return None
    health = snapshot.get('io_health')
    if not isinstance(health, dict):
        return None
    driver = health.get('driver') or {}
    exchange = driver.get('exchange') or {}
    buses = exchange.get('buses')
    return {
        'failed_exchanges': health.get('failed_exchanges'),
        'failed_reads': health.get('failed_reads'),
        'consecutive_failures': health.get('consecutive_failures'),
        'link': driver.get('link'),
        'attempted': exchange.get('attempted'),
        'succeeded': exchange.get('succeeded'),
        'working_counter_mismatches': exchange.get('working_counter_mismatches'),
        'missed_deadlines': exchange.get('missed_deadlines'),
        'last_exchange_tick': exchange.get('last_exchange_tick'),
        'buses': buses if isinstance(buses, list) else None}


def _legs_point(snapshot, point):
    """One served point's sample — value, quality, reason, and the tick
    it was acquired at. None when the snapshot carries no sample for it,
    which the judge reports as an unread surface rather than as a good
    reading."""
    if not isinstance(snapshot, dict):
        return None
    for entry in snapshot.get('points') or []:
        if not isinstance(entry, dict) or entry.get('point') != point:
            continue
        sample = entry.get('sample')
        if not isinstance(sample, dict):
            return None
        value = sample.get('value')
        if isinstance(value, dict):
            value = next(iter(value.values()), None)
        quality = sample.get('quality')
        return {'value': value, 'quality': quality,
                'tick': sample.get('tick')}
    return None


def _legs_degraded(sample):
    """How one point's served quality reads: `good`, `bad`, or
    `unknown` for a shape this contract cannot read."""
    if not isinstance(sample, dict):
        return 'unknown'
    quality = sample.get('quality')
    if quality in ('good', 'Good'):
        return 'good'
    if isinstance(quality, str) and quality.lower().startswith('bad'):
        return 'bad'
    if isinstance(quality, str):
        return 'bad'
    if isinstance(quality, dict):
        return 'bad'
    return 'unknown'


def _legs_view(ctx, seat):
    """One normalized read of the seat's role and run tick, or None when
    the read dropped."""
    base = ctx.get(seat)
    if not base:
        return None
    report = _try_role(ctx, base)
    if not isinstance(report, dict):
        return None
    return {'role': report.get('role'), 'tick': report.get('tick'),
            'claim': report.get('field_claim')}


def _legs_pair(ctx, name):
    """The deployed pair's normalized role evidence for one member."""
    report = _try_role(ctx, ctx.get(name))
    if not isinstance(report, dict):
        return None
    return {'role': report.get('role'), 'tick': report.get('tick'),
            'tracking': 'tracking' in (report.get('sync') or {})}


def _legs_pair_held(record):
    """The deployed pair's undisturbed verdict across the leg's own
    staging — the owner still active and advancing, the peer still a
    tracking standby, in every framing the record carries."""
    launch = record.get('launch_roles') or {}
    roles = record.get('roles') or {}
    owner, peer = launch.get('owner'), launch.get('peer')
    tick = None
    for phase in ('before', 'after', 'final'):
        view = roles.get(phase)
        if not isinstance(view, dict):
            continue
        if (view.get(owner) or {}).get('role') != 'active':
            return False
        seen = view.get(peer) or {}
        if seen.get('role') != 'standby' or seen.get('tracking') is not True:
            return False
        seen_tick = _legs_count((view.get(owner) or {}).get('tick'))
        if seen_tick is None or (tick is not None and seen_tick <= tick):
            return False
        tick = seen_tick
    return tick is not None


def _legs_derive(ctx, document_path):
    """The lane-derived two-station cyclic document, written beside the
    run's evidence so the device server can mount it.

    Returns the written path and the derivation's description, or the
    reason it cannot be made as a string."""
    try:
        document = json.loads(Path(document_path).read_text())
    except (OSError, ValueError, TypeError) as exc:
        return ('the mounted cyclic document is unreadable: '
                + str(exc)[:200])
    try:
        derivation = rig_model.two_station_cyclic(document)
    except rig_model.RigModelError as exc:
        return str(exc)
    path = Path(ctx['evidence_dir']) / 'cyclic-two-station-model.json'
    try:
        path.write_text(json.dumps(derivation['document'], indent=1,
                                   sort_keys=True) + '\n')
    except OSError as exc:
        return ('the derived two-station document is unwritable: '
                + str(exc)[:200])
    return {'path': str(path),
            'description': rig_model.describe(derivation)}


def _legs_stage(ctx):
    """Stage the lane's device server on the derived two-station cyclic
    document and prove the field is the one this contract is about: a
    `sim-cyclic` device whose station map carries at least two stations,
    since a station-attributed shortfall on one station is
    indistinguishable from a whole-bus one.

    Returns the launch dict, or the inconclusive reason as a string."""
    spec = ctx.get('sim_bus_device')
    if not spec:
        return ('the run config stages no sim-bus device server — this '
                "leg needs the lane's register-protocol field")
    cyclic = spec.get('cyclic_model')
    if not cyclic:
        return ("the run config's sim_bus_device block stages no "
                'cyclic_model — the fixture declaring the ' + LEGS_CYCLIC_KIND
                + ' device whose exchange consumes the scripted queue')
    src = Path(ctx.get('src_dir', ''))
    source = None
    for root in (src, Path.cwd()):
        candidate = root / cyclic
        if candidate.is_file():
            source = candidate
            break
    if source is None:
        return ('the revision tree carries no cyclic fixture at ' + cyclic)
    derived = _legs_derive(ctx, source)
    if isinstance(derived, str):
        return derived
    field = ctx['start_sim_bus_device'](fixture=derived['path'])
    model = field.get('model')
    try:
        document = json.loads(Path(model).read_text())
    except (OSError, ValueError, TypeError) as exc:
        return 'the staged device model is unreadable: ' + str(exc)[:200]
    for declared in document.get('devices') or []:
        if declared.get('id') != spec.get('device'):
            continue
        if declared.get('kind') != LEGS_CYCLIC_KIND:
            return ('the staged field serves device '
                    + str(spec.get('device')) + ' as '
                    + repr(declared.get('kind')) + ' — a point-wise '
                    'device issues no exchange, so the scripted queue '
                    'nothing would consume')
        stations = ((declared.get('parameters') or {}).get('stations')
                    or {})
        if len(stations) < 2:
            return ('the staged ' + LEGS_CYCLIC_KIND + ' device declares '
                    + str(len(stations)) + ' station(s) — a '
                    'station-attributed shortfall on a single station is '
                    'indistinguishable from a whole-bus one')
        if STATION not in stations or LEGS_DERIVED_STATION not in stations:
            return ('the staged device declares stations '
                    + json.dumps(sorted(stations))
                    + ' where this leg reads ' + STATION + ' and '
                    + LEGS_DERIVED_STATION)
        field['stations'] = sorted(stations)
        field['derived'] = derived['description']
        return field
    return ('the staged device model declares no device '
            + str(spec.get('device')))


def _legs_settle(ctx):
    """Wait for the documented settle: the seat reporting `active` with
    its device claim held and exchange counters already moving. A seat
    that never exchanged has no scripted outcome to consume."""
    deadline = time.monotonic() + LEGS_SETTLE_BOUND
    view = wait_for(
        lambda: (lambda seen: seen if seen is not None
                 and seen.get('role') == 'active'
                 and seen.get('claim') == 'held' else None)(
                     _legs_view(ctx, LEGS_SEAT)),
        deadline, interval=LEGS_SETTLE_POLL)
    if view is None:
        return ('the born seat never settled as the device\'s claim '
                'holder: ' + json.dumps(_legs_view(ctx, LEGS_SEAT))[:200])
    settled = wait_for(
        lambda: (lambda seen: seen if seen is not None
                 and _legs_count(_legs_io(seen).get('attempted')) is not None
                 and _legs_io(seen)['attempted'] >= LEGS_MISS_THRESHOLD else None)(
                     _try_snapshot(ctx, ctx[LEGS_SEAT])),
        deadline, interval=LEGS_SETTLE_POLL)
    if settled is None:
        return ('the seat\'s io_health never reported '
                + str(LEGS_MISS_THRESHOLD) + ' exchanges — the scripted queue '
                'has no scans to consume it on this rig')
    return {'view': view, 'io': _legs_io(settled),
            'points': _legs_points(settled),
            'held': _legs_point(settled, LEGS_COUPLER_INPUT)}


def _legs_script(ctx, *outcomes):
    """One shipped-control-tool invocation: the queued outcomes and the
    tool's own verdict — the documented `done` answer, or the nonzero
    exit and stderr the caller classifies as a refused tool call."""
    try:
        result = ctx['sim_bus_ctl']('script-exchange', *outcomes)
    except Exception as exc:
        return {'outcomes': list(outcomes), 'ok': False,
                'detail': 'the shipped control tool never ran: '
                          + str(exc)[:200]}
    code = getattr(result, 'returncode', None)
    answer = str(getattr(result, 'stdout', '') or '').strip()
    return {'outcomes': list(outcomes),
            'ok': code == 0 and '"done"' in answer,
            'exit': code, 'answer': answer[-200:],
            'detail': str(getattr(result, 'stderr', '') or '').strip()[-200:]}


def _legs_registers(ctx):
    """The field's own served register bank through the shipped control
    tool — the scan's output image as the device actually holds it. A
    tool that answered nothing is a device no longer serving."""
    try:
        result = ctx['sim_bus_ctl']('list')
    except Exception as exc:
        return {'read': False,
                'detail': 'the shipped control tool never ran: '
                          + str(exc)[:200]}
    code = getattr(result, 'returncode', None)
    answer = str(getattr(result, 'stdout', '') or '').strip()
    return {'read': code == 0 and '"registers"' in answer,
            'exit': code, 'answer': answer[-400:]}


def _legs_window(ctx, key, floors, accept=None):
    """Read the next window the leg waits for.

    `key` names the counter that must move; `floors` is the previous
    window's counters; `accept` narrows further predicates the window
    must satisfy once the named counter has moved. The io_health reading
    and the per-point samples come out of **one** snapshot, so a
    counter and the samples it explains are always from the same scan —
    reading them separately would let a poll consume another scripted
    outcome between the two and drift the difference the judge computes.
    `read` says whether the monitor answered at all inside the window,
    which separates a starved surface from a device that answered
    nothing."""
    deadline = time.monotonic() + LEGS_BOUND
    seen, points, answered = None, None, False
    while time.monotonic() < deadline:
        snapshot = _try_snapshot(ctx, ctx[LEGS_SEAT])
        io = _legs_io(snapshot)
        if io is not None:
            answered = True
            count = _legs_count(io.get(key))
            before = _legs_count((floors or {}).get(key))
            if count is not None and (before is None or count > before):
                if accept is None or accept(io):
                    seen = io
                    points = _legs_points(snapshot)
                    break
        time.sleep(LEGS_POLL)
    return {'io': seen, 'read': answered, 'points': points}


def _legs_delta(after, before, key):
    """`after[key] - before[key]` when both are readable counters, else
    None — a window the judge reports as an unread difference rather
    than as a zero drift."""
    left, right = _legs_count((after or {}).get(key)), _legs_count((before or {}).get(key))
    if left is None or right is None:
        return None
    return left - right


def _legs_teardown(ctx):
    """Best-effort teardown: the leg's seat and the device server."""
    lever = ctx.get('stop_born_controller')
    if lever is not None:
        try:
            lever(LEGS_SEAT)
        except Exception:
            pass
    try:
        if ctx.get('stop_sim_bus_device') is None:
            return None
        ctx['stop_sim_bus_device']()
    except Exception as exc:
        return str(exc)[:200]
    return None


def _legs_rig_state(ctx, device_error):
    """The rig's claim state after the sweep."""
    state = ctx.get('born_controller_state')
    present = None
    if state is not None:
        try:
            present = (state(LEGS_SEAT) or {}).get('absent')
        except Exception:
            present = None
    return {'seat': present, 'device_error': device_error}


def _judge_legs(record, note):
    """Replay one pass's record — runnable against planted negatives in
    the self-check."""
    def failed(key, detail):
        note(key, LEGS_CLAUSE, detail)

    def nondet(key, detail):
        note(key, LEGS_NONDET, detail)

    if record.get('stage_error') is not None:
        nondet('stage', 'the device server, the launch, or a control-tool '
               'call never completed: ' + str(record['stage_error']))
        return
    settled = record.get('settled') or {}
    ages = record.get('aged') or {}
    escalated = record.get('escalated') or {}
    attributed = record.get('attributed') or {}
    recovered = record.get('recovered') or {}

    # Leg 1 — a missed exchange ages the held image, it does not abort
    # the scan: the outputs must not move and both exchange halves must
    # advance.
    if ages.get('read') is not True:
        nondet('aged-unreadable', 'the seat\'s monitor answered no '
               'io_health read inside the aged window: '
               + json.dumps(ages)[:200])
    else:
        if _legs_delta(ages.get('io'), settled.get('io'), 'failed_exchanges') != 1:
            failed('aged-count', 'the queued miss must count exactly one '
                   'failed exchange at the boundary, and the counters '
                   'read: ' + json.dumps({
                       'settled': (settled.get('io') or {}).get(
                           'failed_exchanges'),
                       'aged': (ages.get('io') or {}).get(
                           'failed_exchanges')})[:300])
        if _legs_delta(ages.get('io'), settled.get('io'), 'attempted') != 1:
            failed('aged-not-attempted', 'the scan must still attempt its '
                   'exchange across a missed cycle — a driver that '
                   'skipped the boundary is not ageing anything: '
                   + json.dumps({
                       'settled': (settled.get('io') or {}).get('attempted'),
                       'aged': (ages.get('io') or {}).get('attempted')})[:300])
        outputs = record.get('aged_outputs') or {}
        if outputs.get('read') is not True:
            nondet('outputs-unreadable', 'the field\'s register bank never '
                   'read across the aged window: ' + json.dumps(outputs)[:200])
        elif outputs.get('before') is not None \
                and outputs.get('before') != outputs.get('after'):
            failed('aged-moved-outputs', 'a missed exchange must publish '
                   'nothing — the field\'s register bank moved from '
                   + json.dumps(outputs.get('before'))[:200] + ' to '
                   + json.dumps(outputs.get('after'))[:200])
        held = (ages.get('points') or {}).get(str(LEGS_COUPLER_INPUT)) or {}
        if _legs_degraded(held) != 'good':
            failed('aged-hold', 'below the declared threshold the held '
                   'input image must still serve Good — the point reads '
                   + _legs_degraded(held) + ': ' + json.dumps(held)[:200])
        elif held.get('tick') != (ages.get('before_point') or {}).get('tick'):
            failed('aged-restamped', 'the held sample must keep its own '
                   'acquisition stamp rather than being restamped by the '
                   'observation: ' + json.dumps({
                       'before': ages.get('before_point'),
                       'after': held})[:300])

    # Leg 2 — the declared threshold escalates the reads.
    if escalated.get('read') is not True:
        nondet('escalated-unreadable', 'the seat\'s monitor answered no '
               'io_health read inside the escalation window: '
               + json.dumps(escalated)[:200])
    else:
        missed_total = record.get('missed_total')
        if missed_total is not None \
                and _legs_delta(escalated.get('io'), settled.get('io'),
                           'failed_exchanges') != missed_total:
            failed('escalation-count', 'every queued miss must count once '
                   'at the boundary — the leg queued '
                   + str(missed_total) + ': ' + json.dumps({
                       'settled': (settled.get('io') or {}).get(
                           'failed_exchanges'),
                       'escalated': (escalated.get('io') or {}).get(
                           'failed_exchanges')})[:300])
        points = escalated.get('points') or {}
        if not points:
            nondet('escalation-points-unreadable',
                   'no field input sample read inside the escalation '
                   'window: ' + json.dumps(escalated)[:200])
        else:
            degraded = sorted(name for name, sample in points.items()
                              if _legs_degraded(sample) != 'good')
            if not degraded:
                failed('no-escalation', 'past the declared '
                       + str(LEGS_MISS_THRESHOLD) + '-miss threshold every '
                       'field input must escalate — none did: '
                       + json.dumps(points, sort_keys=True)[:400])
            elif len(degraded) < len(points):
                failed('partial-escalation', 'the declared threshold '
                       'escalates every field input on the bus, but only '
                       + json.dumps(degraded) + ' of '
                       + json.dumps(sorted(points)) + ' escalated: '
                       + json.dumps(points, sort_keys=True)[:400])
        io = escalated.get('io') or {}
        if io.get('link') != 'disconnected':
            failed('link-not-down', 'the transport must report its link '
                   'down while the bus is missing exchanges, not healthy: '
                   + json.dumps(io)[:300])

    # Leg 3 — a station-attributed shortfall degrades only its station.
    if attributed.get('read') is not True:
        nondet('attribution-unreadable', 'the seat\'s monitor answered no '
               'io_health read inside the shortfall window: '
               + json.dumps(attributed)[:200])
    else:
        if _legs_delta(attributed.get('io'), escalated.get('io'),
                  'working_counter_mismatches') != 1:
            failed('attribution-count', 'a completed-but-short exchange '
                   'must count exactly one working-counter mismatch — the '
                   'counters read: ' + json.dumps({
                       'escalated': (escalated.get('io') or {}).get(
                           'working_counter_mismatches'),
                       'attributed': (attributed.get('io') or {}).get(
                           'working_counter_mismatches')})[:300])
        points = attributed.get('points') or {}
        coupler = points.get(str(LEGS_COUPLER_INPUT))
        derived = points.get(str(record.get('derived_point')))
        if coupler is None or derived is None:
            nondet('attribution-points-unreadable',
                   'the shortfall window carried no sample for both the '
                   'coupler station\'s point and the derived station\'s: '
                   + json.dumps(sorted(points)))
        else:
            if _legs_degraded(derived) == 'good':
                failed('station-not-degraded', 'the shortfall named the '
                       'derived station, so its point must degrade — it '
                       'reads Good: ' + json.dumps(points)[:300])
            if _legs_degraded(coupler) != 'good':
                failed('station-misattributed', 'the shortfall must '
                       'degrade only the station it names — the coupler '
                       'station\'s point reads '
                       + _legs_degraded(coupler) + ': '
                       + json.dumps(points)[:300])

    # Leg 4 — recovery re-enters on the exchange boundary alone, and the
    # counters account for exactly what happened.
    if recovered.get('read') is not True:
        nondet('recovery-unreadable', 'the seat\'s monitor answered no '
               'io_health read inside the recovery window: '
               + json.dumps(recovered)[:200])
    else:
        io = recovered.get('io') or {}
        if io.get('link') != 'connected':
            failed('no-recovery', 'the following scripted complete must '
                   'bring the link verdict back: ' + json.dumps(io)[:300])
        if io.get('consecutive_failures') != 0:
            failed('streak-not-cleared', 'a clean exchange resets the '
                   'boundary\'s failure streak: ' + json.dumps(io)[:300])
        gaps = _legs_delta(io, settled.get('io'), 'attempted')
        shortfalls = _legs_delta(io, settled.get('io'), 'succeeded')
        if gaps is None or shortfalls is None:
            nondet('recovery-counts-unreadable',
                   'the exchange counters the recovery window compares '
                   'were not readable: ' + json.dumps(io)[:300])
        elif gaps - shortfalls != (record.get('missed_total') or 0):
            failed('counter-drift', 'the exchange counters must account '
                   'for exactly the queued misses and nothing else — '
                   + str(gaps - shortfalls) + ' exchanges went uncounted '
                   'against ' + str(record.get('missed_total'))
                   + ' misses: ' + json.dumps({'attempted': gaps,
                                               'succeeded': shortfalls})[:300])
        if _legs_delta(io, attributed.get('io'),
                  'working_counter_mismatches') != 0:
            failed('mismatch-accumulated', 'the clean exchange must clear '
                   'the station attribution, not accumulate another '
                   'mismatch: ' + json.dumps({
                       'attributed': (attributed.get('io') or {}).get(
                           'working_counter_mismatches'),
                       'recovered': io.get(
                           'working_counter_mismatches')})[:300])
        points = recovered.get('points') or {}
        stale = sorted(name for name, sample in points.items()
                       if _legs_degraded(sample) != 'good')
        if stale:
            failed('recovery-degraded', 'every field input must serve '
                   'Good again after the recovery, but '
                   + json.dumps(stale) + ' did not: '
                   + json.dumps(points, sort_keys=True)[:400])

    if (record.get('device') or {}).get('read') is not True:
        nondet('device-unreadable', 'the register device answered no '
               'served read after the recovery — the field the contract '
               'asks to still be serving cannot be reached: '
               + json.dumps(record.get('device'), sort_keys=True)[:300])
    if not _legs_pair_held(record):
        nondet('pair-disturbed', 'the deployed pair moved or wedged across '
               "the leg's own field staging: "
               + json.dumps(record.get('roles'), sort_keys=True)[:300])
    rig = record.get('rig') or {}
    if rig.get('seat') is not True or rig.get('device_error') is not None:
        nondet('rig-not-restored', 'the leg left the rig\'s claim state '
               'standing — a seat or the device server outlived the '
               'sweep the legs behind this one inherit: '
               + json.dumps(rig, sort_keys=True)[:300])


def _legs_digest(record, violations):
    """The pass's normalized verdict record — identical digests across
    two consecutive passes is the determinism contract."""
    def clean(*keys):
        return not any(key in violations for key in keys)

    def verdict(name, keys, values, other='unread'):
        for value in values:
            if clean(*keys):
                return value
        return other
    return {
        'aged': verdict('aged', ('aged-count', 'aged-not-attempted',
                                 'aged-moved-outputs', 'aged-hold',
                                 'aged-restamped'), ['held', 'moved'],
                       'aborted'),
        'escalation': verdict('escalation',
                              ('no-escalation', 'partial-escalation',
                               'link-not-down', 'escalation-count'),
                              ['escalated'], 'held'),
        'attribution': verdict('attribution',
                               ('station-not-degraded',
                                'station-misattributed',
                                'attribution-count'), ['named'],
                               'blurred'),
        'recovery': verdict('recovery', ('no-recovery',
                                         'streak-not-cleared',
                                         'counter-drift',
                                         'mismatch-accumulated',
                                         'recovery-degraded'),
                            ['re-entered'], 'stalled'),
        'device': 'serving' if clean('outputs-unreadable',
                                      'device-unreadable') else 'unreadable',
        'pair': 'held' if clean('pair-disturbed') else 'disturbed',
        'rig': 'restored' if clean('rig-not-restored') else 'dirty'}


def _legs_points(snapshot):
    """Every field input's served sample out of one snapshot, keyed by
    point id — the per-point half each leg reads."""
    if not isinstance(snapshot, dict):
        return None
    out = {}
    for entry in snapshot.get('points') or []:
        if not isinstance(entry, dict):
            continue
        sample = _legs_point(snapshot, entry.get('point'))
        if sample is not None:
            out[str(entry.get('point'))] = sample
    return out


def _legs_pass(ctx, number, launch):
    """One pass over the four legs: stage the two-station cyclic field,
    launch one born-active controller onto the staged document, let it
    settle, then queue one miss and read the aged-but-not-aborted
    window, two more misses and read the threshold escalation, one
    station-attributed shortfall and read the per-station attribution,
    and one complete and read the recovery."""
    record = {'pass': number, 'launch_roles': dict(launch), 'roles': {},
              'settled': {}, 'aged': {}, 'escalated': {},
              'attributed': {}, 'recovered': {}}
    record['roles']['before'] = {
        name: _legs_pair(ctx, name) for name in (launch['owner'], launch['peer'])}
    try:
        try:
            field = _legs_stage(ctx)
        except Exception as exc:
            record['stage_error'] = ('the field never staged: '
                                     + str(exc)[:250])
            return record
        if isinstance(field, str):
            record['inconclusive'] = field
            return record
        record['field'] = dict(field)
        try:
            record['launch'] = ctx['start_born_controller'](
                LEGS_SEAT, None, document=field['model'])
        except Exception as exc:
            record['stage_error'] = ('the controller launch never ran: '
                                     + str(exc)[:250])
            return record
        # The staged document both ends read: the server's station map
        # and the attachment's mounted model come out of this one file.
        record['field']['mounted'] = (record['launch'] or {}).get('model')
        record['derived_point'] = (field.get('derived') or {}).get(
            'station_point')
        settled = _legs_settle(ctx)
        if isinstance(settled, str):
            record['inconclusive'] = settled
            return record
        record['settled'] = settled

        # Leg 1 — one miss, aged not aborted.
        record['script_miss'] = _legs_script(ctx, 'miss')
        if record['script_miss'].get('ok') is not True:
            return record
        before_registers = _legs_registers(ctx)
        after_registers = _legs_registers(ctx)
        aged = _legs_window(ctx, 'failed_exchanges', settled.get('io'))
        # The pre-queue sample is the settle's own: the exchange the
        # miss replaced is the one that latched it.
        aged['before_point'] = settled.get('held')
        record['aged'] = aged
        record['aged_outputs'] = {
            'read': (before_registers.get('read') is True
                     and after_registers.get('read') is True),
            'before': before_registers.get('answer'),
            'after': after_registers.get('answer') or before_registers.get(
                'answer')}

        # Leg 2 — two more misses, reaching the declared threshold.
        record['script_escalate'] = _legs_script(ctx, 'miss', 'miss')
        if record['script_escalate'].get('ok') is not True:
            return record
        record['missed_total'] = LEGS_MISS_THRESHOLD
        escalated = _legs_window(
            ctx, 'failed_exchanges', aged.get('io'),
            accept=lambda io: io.get('link') == 'disconnected')
        record['escalated'] = escalated

        # Leg 3 — a station-attributed shortfall on the derived station.
        record['script_short'] = _legs_script(
            ctx, 'short-station:' + LEGS_DERIVED_STATION)
        if record['script_short'].get('ok') is not True:
            return record
        attributed = _legs_window(ctx, 'working_counter_mismatches',
                             escalated.get('io'),
                             accept=lambda io: io.get('link') == 'connected')
        record['attributed'] = attributed

        # Leg 4 — the clean exchange that re-enters.
        record['script_complete'] = _legs_script(ctx, 'complete')
        if record['script_complete'].get('ok') is not True:
            return record
        recovered = _legs_window(ctx, 'succeeded', attributed.get('io'),
                            accept=lambda io: io.get('link') == 'connected'
                            and io.get('consecutive_failures') == 0)
        record['recovered'] = recovered
        record['device'] = _legs_registers(ctx)
        return record
    finally:
        record['roles']['after'] = {
            name: _legs_pair(ctx, name) for name in (launch['owner'],
                                                launch['peer'])}


def _legs_self_check():
    """The unchecked-diagnostic guard: replay the judge over planted
    negatives and report each that slipped."""
    def io(attempted, succeeded, failed, mismatches=0, link='connected',
           streak=0):
        return {'failed_exchanges': failed, 'failed_reads': 0,
                'consecutive_failures': streak, 'link': link,
                'attempted': attempted, 'succeeded': succeeded,
                'working_counter_mismatches': mismatches,
                'missed_deadlines': 0, 'last_exchange_tick': attempted,
                'buses': []}

    def good(value=False, tick=100):
        return {'value': value, 'quality': 'good', 'tick': tick}

    def bad(tick=100):
        return {'value': value_or_none(), 'quality': 'bad', 'tick': tick}

    def value_or_none():
        return False

    def clean_record():
        derived = 13
        return {
            'pass': 1,
            'launch_roles': {'owner': 'active', 'peer': 'standby'},
            'field': {'device': 1, 'port': 9005, 'stations': [STATION,
                                                               LEGS_DERIVED_STATION],
                      'derived': {'station_point': derived}},
            'derived_point': derived,
            'missed_total': LEGS_MISS_THRESHOLD,
            'settled': {'view': {'role': 'active', 'tick': 40,
                                 'claim': 'held'},
                        'io': io(40, 40, 0),
                        'held': good(True, 40),
                        'points': {'1': good(True, 40)}},
            'script_miss': {'ok': True},
            'script_escalate': {'ok': True},
            'script_short': {'ok': True},
            'script_complete': {'ok': True},
            'aged': {'read': True, 'io': io(41, 40, 1, link='disconnected',
                                             streak=1),
                     'points': {'1': good(True, 40)},
                     'before_point': good(True, 40)},
            'aged_outputs': {'read': True, 'before': '{"registers": []}',
                             'after': '{"registers": []}'},
            'escalated': {'read': True,
                          'io': io(43, 40, 3, link='disconnected', streak=3),
                          'points': {'1': bad(43), '2': bad(43),
                                     str(derived): bad(43)}},
            # The shortfall and the recovery both *completed* — only
            # the three queued misses went uncounted, which is what the
            # counter-drift clause reads.
            'attributed': {'read': True,
                           'io': io(44, 42, 3, mismatches=1),
                           'points': {'1': good(False, 44),
                                      '2': good(False, 44),
                                      str(derived): bad(44)}},
            'recovered': {'read': True,
                          'io': io(45, 42, 3, mismatches=1),
                          'points': {'1': good(False, 45),
                                     '2': good(False, 45),
                                     str(derived): good(False, 45)}},
            'device': {'read': True},
            'rig': {'seat': True, 'device_error': None},
            'roles': {
                'before': {'active': {'role': 'active', 'tick': 900,
                                      'tracking': False},
                           'standby': {'role': 'standby', 'tick': 900,
                                       'tracking': True}},
                'after': {'active': {'role': 'active', 'tick': 1200,
                                     'tracking': False},
                          'standby': {'role': 'standby', 'tick': 1200,
                                      'tracking': True}},
                'final': {'active': {'role': 'active', 'tick': 1500,
                                     'tracking': False},
                          'standby': {'role': 'standby', 'tick': 1500,
                                      'tracking': True}}}}

    def audit(record):
        found = {}
        _judge_legs(record, lambda key, diagnostic, detail:
               found.setdefault(key, diagnostic))
        return found

    slipped = []
    if audit(clean_record()):
        slipped.append('clean-overstrict')

    def expect(name, mutate, diagnostic=LEGS_CLAUSE):
        record = clean_record()
        mutate(record)
        if diagnostic not in audit(record).values():
            slipped.append(name)

    expect('missed-exchange-moved-the-outputs',
           lambda r: r['aged_outputs'].update(after='{"registers": [1]}'))
    expect('missed-exchange-not-counted',
           lambda r: r['aged']['io'].update(failed_exchanges=0))
    expect('missed-exchange-skipped-its-exchange',
           lambda r: r['aged']['io'].update(attempted=42))
    expect('held-image-degraded-below-the-threshold',
           lambda r: r['aged']['points'].update({'1': bad(41)}))
    expect('held-image-restamped-by-the-observation',
           lambda r: r['aged']['points'].update({'1': good(True, 41)}))
    expect('threshold-did-not-escalate',
           lambda r: r['escalated'].update(points={'1': good(False, 43),
                                                  '2': good(False, 43),
                                                  '13': good(False, 43)}))
    expect('threshold-escalated-only-some-stations',
           lambda r: r['escalated']['points'].update(
               {'2': good(False, 43), '13': good(False, 43)}))
    expect('link-read-healthy-while-missing',
           lambda r: r['escalated']['io'].update(link='connected'))
    expect('misattributed-shortfall',
           lambda r: r['attributed']['points'].update(
               {'1': bad(44), '2': bad(44)}))
    expect('named-station-left-fresh',
           lambda r: r['attributed']['points'].update({'13': good(False, 44)}))
    expect('shortfall-not-counted',
           lambda r: r['attributed']['io'].update(working_counter_mismatches=0))
    expect('recovery-never-re-entered',
           lambda r: r['recovered']['io'].update(link='disconnected'))
    expect('recovery-left-the-streak',
           lambda r: r['recovered']['io'].update(consecutive_failures=1))
    expect('counters-drifted',
           lambda r: r['recovered']['io'].update(succeeded=43))
    expect('recovery-accumulated-a-mismatch',
           lambda r: r['recovered']['io'].update(working_counter_mismatches=2))
    expect('recovery-left-points-degraded',
           lambda r: r['recovered']['points'].update({'1': bad(45)}))
    expect('aged-window-unreadable',
           lambda r: r['aged'].update(read=False, io=None), LEGS_NONDET)
    expect('escalation-window-unreadable',
           lambda r: r['escalated'].update(read=False, io=None), LEGS_NONDET)
    expect('attribution-window-unreadable',
           lambda r: r['attributed'].update(read=False, io=None), LEGS_NONDET)
    expect('recovery-window-unreadable',
           lambda r: r['recovered'].update(read=False, io=None), LEGS_NONDET)
    expect('outputs-unreadable',
           lambda r: r['aged_outputs'].update(read=False), LEGS_NONDET)
    expect('device-stopped-serving',
           lambda r: r['device'].update(read=False), LEGS_NONDET)
    expect('attribution-points-unreadable',
           lambda r: r['attributed'].update(points={}), LEGS_NONDET)
    expect('pair-owner-moved',
           lambda r: r['roles']['after']['active'].update(role='standby'),
           LEGS_NONDET)
    expect('pair-peer-lost-tracking',
           lambda r: r['roles']['final']['standby'].update(tracking=False),
           LEGS_NONDET)
    expect('pair-scan-wedged',
           lambda r: r['roles']['after']['active'].update(tick=900), LEGS_NONDET)
    expect('rig-left-standing',
           lambda r: r['rig'].update(seat=False), LEGS_NONDET)
    expect('rig-presence-unreadable',
           lambda r: r['rig'].update(seat=None), LEGS_NONDET)
    expect('stage-failed',
           lambda r: r.update(stage_error='docker run failed'), LEGS_NONDET)
    return slipped


def scenario_cyclic_exchange_failure_legs(ctx):
    """Exercise the cyclic-exchange failure legs on the rig's cyclic
    device: stage a lane-derived two-station `sim-cyclic` field, launch
    one born-active controller onto it, and queue the four scripted
    exchange outcomes through the shipped control tool. A missed
    exchange must age the held input image without moving the scan's
    outputs; misses up to the declared threshold must escalate the
    reads rather than serve a stale value; a station-attributed
    shortfall must degrade only the station it names; and a following
    complete must re-enter on the exchange boundary with the counters
    accounting for exactly what happened. The rig is swept afterward
    and two consecutive passes produce identical outcome digests."""
    case = Case(
        'cyclic-exchange-failure-legs',
        'Cyclic-exchange misses age the held image, the declared '
        'threshold escalates, a shortfall is attributed, and a clean '
        'exchange re-enters',
        'over a rig-staged lane-derived two-station sim-cyclic device, '
        'one scripted miss ages the held input image and the held '
        'acquisition stamp while the scan outputs stay exactly where the '
        'field had them and the counters count the boundary once; further '
        'misses up to the declared threshold escalate every field input '
        'to a degraded quality with the link verdict disconnected rather '
        'than serving a stale value; a station-attributed shortfall '
        'degrades the derived station\'s point and leaves the coupler '
        'station\'s point Good and fresh; a following complete re-enters '
        'on the exchange boundary with the link connected, the streak '
        'zero, the points Good again, and the counters accounting for '
        'exactly the queued misses — the rig swept afterward, a seat or '
        'device server that outlived the sweep fails the leg — and two '
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
        owner = 'active' if ctx.get('active') else None
        peer = 'standby' if ctx.get('standby') else None
        if owner is None or peer is None:
            return case.finish(
                'inconclusive',
                'the run stages no two-member deployed pair — this leg '
                'needs one to prove the field staging left it alone')
        digests = []
        for number in (1, 2):
            violations = {}

            def note(key, diagnostic, detail):
                violations.setdefault(key, (diagnostic, detail))

            launch = {'owner': owner, 'peer': peer}
            record = _legs_pass(ctx, number, launch)
            device_error = _legs_teardown(ctx)
            record['rig'] = _legs_rig_state(ctx, device_error)
            record['roles']['final'] = {
                name: _legs_pair(ctx, name) for name in (owner, peer)}
            if not record.get('inconclusive'):
                # A staging failure is judged too: the judge reports it
                # under the instability diagnostic rather than leaving a
                # pass with no verdict behind it.
                _judge_legs(record, note)
                digest = _legs_digest(record, violations)
                record['digest'] = dict(digest)
            else:
                digest = None
            record['violations'] = {
                key: diagnostic
                for key, (diagnostic, _) in violations.items()}
            ref = save_evidence(
                ctx['evidence_dir'],
                'cyclic-exchange-failure-legs-pass-' + str(number)
                + '.json', record)
            case.evidence('file', ref,
                          'cyclic-exchange failure-legs pass '
                          + str(number) + ' — the lane-derived '
                          'two-station document and the staged device '
                          'server, the seat\'s settle, the four queued '
                          'scripted exchange outcomes with the window '
                          'each leg read, the field\'s own register '
                          'reads beside them, the deployed pair\'s '
                          'before/after/final framing, the swept rig\'s '
                          'restoration read, and the normalized digest')
            if record.get('inconclusive'):
                return case.finish('inconclusive', record['inconclusive'])
            if violations:
                name = LEGS_CLAUSE if any(
                    diagnostic == LEGS_CLAUSE
                    for diagnostic, _ in violations.values()) else LEGS_NONDET
                return case.finish(
                    'failed', name + ': ' + '; '.join(
                        detail for _, detail
                        in list(violations.values())[:4]))
            digests.append(digest)
        if digests[0] != digests[1]:
            return case.finish(
                'failed', LEGS_NONDET + ': the two passes\' digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two cyclic-exchange failure-legs passes, identical '
                     'digests: ' + json.dumps(digests[0], sort_keys=True))
        slipped = _legs_self_check()
        if slipped:
            return case.finish('failed', LEGS_UNCHECKED + ': planted negatives '
                               'slipped the leg’s own audits: '
                               + ', '.join(slipped))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))