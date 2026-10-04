"""The sim_bus_scripted_miss_claim_hold acceptance leg — one module per
leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the
ordering rule and the shared seam."""
from .common import *

# Ordering: the leg stages the lane's own register-protocol device
# server and a claim-holding controller on one born seat, so it runs
# after the sim-bus born-active refusal leg that shares both and sweeps
# them, and before the reclaim legs take the born seats over again.
RUNS_AFTER = frozenset({'scenario_sim_bus_startup_claim_refusal'})
RUNS_BEFORE = frozenset({'scenario_reclaim_convergence_gate'})


# --------------------------------------------------------------------
# The scripted-miss claim-hold contract — the per-revision lane
# evidence for the contract #1413's fix establishes on the register
# protocol, the fieldbus exchange semantics WW-FND-002's claim
# lifecycle rides: a scripted `miss` faults the cycle *in band* — the
# device answers `missed` — while the consuming connection and the
# write-ownership claim it carries stay up, because a missed cycle is
# not a severed link.
#
# The defect: the scripted-outcome queue is exchange tooling, not part
# of the arbitration vocabulary, but a queued `miss` used to drop the
# consuming connection unanswered. Teardown released that attachment's
# claim hold with the dead connection, so on a bus claim — where a
# claim stands only while an attachment holds it — any unfenced
# attachment could free another's field ownership by scripting one
# queued outcome onto the shared device. The exchange then met the
# fail-closed unclaimed field, and once a peer took the claim the
# superseded owner's staged image fenced and demoted it: a scripted
# flaky device reached straight into the single-writer decision.
#
# The staging is the rig's own register-protocol field: the lane's
# shipped `dcs-sim-bus-device` server serving the run config's staged
# `sim-cyclic` document — the device kind whose per-scan process-image
# `exchange` is the only traffic that consumes the scripted queue, so a
# queued outcome lands on the claim holder's own scan and nowhere else —
# and one born-active controller launched onto that staged document on
# a born seat, with no `--remote` and no pair: it takes the device's
# write-ownership claim at startup and serves `field_claim: held`. The
# field is kept single-attached on purpose. The queue is device-global,
# so a second exchanging attachment could consume the queued miss and
# make the attribution — whose cycle missed, and whose claim it freed —
# nondeterministic; the device's continued service is witnessed
# instead through the holder's own following exchanges and through the
# shipped `dcs-sim-bus-ctl` the leg scripts and reads the device with,
# exec'd inside the device server's own container because the register
# protocol is rig-dialed and no host socket reaches it.
#
# The pass then:
#   * samples the settled holder's baseline — the claim standing, the
#     exchange counters moving, the durable journal's floor;
#   * queues `miss` through the shipped control tool and reads the
#     failed cycle the scan recorded: the cumulative `failed_exchanges`
#     advanced and the driver's own standing description of the failed
#     exchange names the *in-band* scripted miss, which a severed link
#     cannot produce;
#   * asserts the claim it carries survived: the run still reports the
#     device claim held, still scans as `active`, and its durable
#     `--journal-file` records no `field_claim_lost`, no fenced
#     `active → demoting` walk, no observed foreign claimant, and no
#     second run boundary — a process restart rather than an in-place
#     degrade;
#   * queues `complete` and reads the recovery on the *same* link: the
#     exchange counters' successes advanced past the missed cycle, the
#     transport's link verdict returned to connected and the boundary's
#     failure streak to zero, the run tick kept moving, and the device
#     answered the shipped tool's own served read.
#
# A staged revision predating the contract is inconclusive, and the
# signature is exact: the queued miss answered as a sever — the driver
# naming the dropped connection rather than the scripted miss — and the
# claim it bound went with it. That is the recorded defect, read from
# the rig rather than assumed.
#
# Named diagnostics: sim-bus-scripted-miss-failed tags the contract
# clauses — a queued miss whose failed cycle was never recorded, a
# failed cycle the driver did not report as the in-band scripted
# answer, a claim the in-band miss did not keep, a holder the miss
# demoted or restarted, a journaled claim loss or fenced walk, a
# following `complete` that never completed on the same link, a run
# tick that stopped across the window — while
# sim-bus-scripted-miss-nondeterministic tags the instability the
# contract does not answer for: a refused staging, launch, or control-
# tool call, a claim holder that never settled, a starved or unread
# monitor, an unreadable journal, a control tool answering something
# other than its documented `done`, a device server that stopped
# answering at all, a moved or wedged deployed pair, a rig the sweep
# did not restore, or two passes whose digests diverge. The
# unchecked-diagnostic self-check replays the judge over planted
# negatives and reports sim-bus-scripted-miss-unchecked for any that
# slip through.

HOLDER_SEAT = 'driven'    # the born seat whose controller holds the claim
CYCLIC_KIND = 'sim-cyclic'  # the device kind the scripted queue rides

SETTLE_BOUND = 90.0       # bound on the claim-taking launch settling
MISS_BOUND = 30.0         # bound on the queued miss consuming a cycle
RECOVER_BOUND = 30.0      # bound on the following scripted completion
SETTLE_POLL = 0.5         # the settle wait's cadence
MISS_POLL = 0.2           # the miss/recovery windows' sampling cadence

CLAUSE = 'sim-bus-scripted-miss-failed'
NONDET = 'sim-bus-scripted-miss-nondeterministic'
UNCHECKED = 'sim-bus-scripted-miss-unchecked'

# The driver records the failure of an exchange it could not complete as
# its own description, and the two answers this leg separates read
# differently: `LinkError::Missed`'s text is the in-band verdict a live
# connection carries, while a severed link reads as the connection
# being gone. A revision that severed instead is the pre-contract
# defect, not a slow answer to the same question.
INBAND = 'a scripted miss answered the exchange'
SEVERED = 'no live connection to the device server'


def _miss_count(value):
    """A served counter as an int, or None — a payload carrying no
    integer where the contract reads one is a shape the judge reports,
    never a comparison that silently passes."""
    return value if isinstance(value, int) and not isinstance(value, bool) \
        else None


def _miss_io(snapshot):
    """The io_health half this contract reads, normalized: the boundary
    counters, the transport's link verdict, the cyclic exchange
    counters, the boundary's own error variant, and the driver's own
    standing description of the last exchange that did not complete —
    the line that names the in-band scripted miss a severed link cannot
    produce. None while the read has not answered."""
    if not isinstance(snapshot, dict):
        return None
    health = snapshot.get('io_health')
    if not isinstance(health, dict):
        return None
    driver = health.get('driver') or {}
    exchange = driver.get('exchange') or {}
    error = (health.get('last_error') or {}).get('error')
    if isinstance(error, dict) and error:
        # The IoError variant's wire name — snake_case on the served
        # contract; normalized so a producer emitting the legacy
        # PascalCase spelling reads the same.
        error = str(next(iter(error))).lower()
    described = driver.get('last_error')
    return {
        'failed_exchanges': health.get('failed_exchanges'),
        'failed_reads': health.get('failed_reads'),
        'failed_writes': health.get('failed_writes'),
        'consecutive_failures': health.get('consecutive_failures'),
        'link': driver.get('link'),
        'error': error if isinstance(error, str) else None,
        'described': described if isinstance(described, str) else None,
        'attempted': exchange.get('attempted'),
        'succeeded': exchange.get('succeeded'),
        'last_exchange_tick': exchange.get('last_exchange_tick')}


def _miss_view(ctx, seat):
    """One normalized claim-holder read: role, run tick, and the field's
    claim posture the run's own probe last answered. None when the read
    dropped or carried no claim verdict at all — an observation the leg
    must report as unread rather than read as a loss."""
    base = ctx.get(seat)
    if not base:
        return None
    report = _try_role(ctx, base)
    if not isinstance(report, dict):
        return None
    return {'role': report.get('role'), 'tick': report.get('tick'),
            'claim': report.get('field_claim')}


def _miss_pair(ctx, name):
    """The deployed pair's normalized role evidence for one member —
    role, run tick, tracking posture; None when the read dropped."""
    report = _try_role(ctx, ctx[name])
    if not isinstance(report, dict):
        return None
    return {'role': report.get('role'), 'tick': report.get('tick'),
            'tracking': 'tracking' in (report.get('sync') or {})}


def _miss_pair_held(record):
    """The deployed pair's undisturbed verdict: the owner still active
    and advancing its scan across the leg's own field staging, the peer
    still a tracking standby — in every framing the record carries, the
    `before` and `after` of the staging and the `final` one the leg
    reads once its own seat is gone."""
    launch = record.get('launch_roles') or {}
    roles = record.get('roles') or {}
    owner, peer = launch.get('owner'), launch.get('peer')
    tick = None
    for phase in ('before', 'after', 'final'):
        view = roles.get(phase)
        if view is None:
            continue
        if (view.get(owner) or {}).get('role') != 'active':
            return False
        seen = view.get(peer) or {}
        if seen.get('role') != 'standby' \
                or seen.get('tracking') is not True:
            return False
        seen_tick = (view.get(owner) or {}).get('tick')
        if _miss_count(seen_tick) is None \
                or (tick is not None and seen_tick <= tick):
            return False
        tick = seen_tick
    return tick is not None


def _miss_events(path):
    """Every event body a seat's `--journal-file` records, in append
    order."""
    return [(item.get('entry') or {}).get('event')
            for item in _journal_entries(path)
            if isinstance(item.get('entry'), dict)]


def _miss_bodies(events, kind):
    """The `kind` event bodies one journal event slice carries."""
    out = []
    for event in events:
        body = (event or {}).get(kind)
        if isinstance(body, dict):
            out.append(body)
    return out


def _miss_boundaries(path):
    """The run-boundary markers a seat's journal carries — one per
    process lifetime the file records."""
    return [item['run_boundary'] for item in _journal_entries(path)
            if 'run_boundary' in item]


def _miss_walk(events):
    """The (from, to, origin) role transitions a journal event slice
    carries, in record order."""
    walk = []
    for event in events:
        change = (event or {}).get('role_changed')
        if isinstance(change, dict):
            walk.append([change.get('from'), change.get('to'),
                         change.get('origin')])
    return walk


def _miss_field(ctx):
    """Stage the lane's register-protocol device server on its cyclic
    model and prove the field it serves is the one this contract is
    about: the staged device declared `sim-cyclic` with an output
    channel — the scripted-outcome queue rides the exchange only a
    process-image driver's scan issues, and a point-wise field would
    leave the queued outcome unconsumed, which is the absence this leg
    declines on rather than a defect it could report.
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
        return ('the run config\'s sim_bus_device block stages no '
                'cyclic_model — the fixture declaring the '
                + CYCLIC_KIND + ' device whose exchange consumes the '
                'scripted queue')
    field = ctx['start_sim_bus_device'](cyclic)
    document = field.get('model')
    try:
        model = json.loads(Path(document).read_text())
    except (OSError, ValueError, TypeError) as exc:
        return 'the staged device model is unreadable: ' + str(exc)[:200]
    for declared in model.get('devices') or []:
        if declared.get('id') != spec.get('device'):
            continue
        if declared.get('kind') != CYCLIC_KIND:
            return ('the staged field serves device '
                    + str(spec.get('device')) + ' as '
                    + repr(declared.get('kind')) + ' — a point-wise '
                    'device issues no exchange, so the scripted queue '
                    'nothing would consume; point sim_bus_device.'
                    'cyclic_model at a model declaring a ' + CYCLIC_KIND
                    + ' device for this leg')
        outputs = [name for name, channel
                   in (declared.get('channels') or {}).items()
                   if (channel or {}).get('direction') == 'out']
        if not outputs:
            return ('the staged ' + CYCLIC_KIND + ' device declares no '
                    'output channel — the scan stages no image to '
                    'publish past the missed cycle')
        field['channels'] = outputs
        return field
    return 'the staged device model declares no device ' \
        + str(spec.get('device'))


def _miss_settle(ctx):
    """Wait for the documented settle: the born seat reporting `active`
    with the device claim held, and an io_health whose exchange
    counters have already moved — a holder that never exchanged has no
    scripted miss to consume, and the leg would be staging its
    induction against a field the controller never reached.
    Returns the baseline record, or the inconclusive reason as a
    string."""
    deadline = time.monotonic() + SETTLE_BOUND
    view = wait_for(
        lambda: (lambda seen: seen if seen is not None
                 and seen.get('role') == 'active'
                 and seen.get('claim') == 'held'
                 else None)(_miss_view(ctx, HOLDER_SEAT)),
        deadline, interval=SETTLE_POLL)
    if view is None:
        return ('the born seat never settled as the device\'s claim '
                'holder — no write-ownership claim stands for the '
                'scripted miss to have kept: '
                + json.dumps(_miss_view(ctx, HOLDER_SEAT))[:200])
    io = wait_for(
        lambda: (lambda seen: seen if seen is not None
                 and _miss_count(seen.get('attempted')) is not None
                 and seen['attempted'] >= 1 else None)(
                     _miss_io(_try_snapshot(ctx, ctx[HOLDER_SEAT]))),
        deadline, interval=SETTLE_POLL)
    if io is None:
        return ('the claim holder\'s io_health never reported an '
                'exchange — the scripted queue has no scan to consume '
                'it on this rig')
    return {'view': view, 'io': io}


def _miss_script(ctx, *outcomes):
    """One shipped-control-tool invocation against the staged device:
    the queued outcomes, and the tool's own verdict — the documented
    `done` answer the device server gives a `script-exchange` it
    applied, or the nonzero exit and stderr the caller classifies as a
    refused tool call rather than a contract verdict."""
    try:
        result = ctx['sim_bus_ctl']('script-exchange', *outcomes)
    except Exception as exc:
        return {'outcomes': list(outcomes), 'ok': False, 'exit': None,
                'answer': '',
                'detail': 'the shipped control tool never ran: '
                          + str(exc)[:200]}
    code = getattr(result, 'returncode', None)
    answer = str(getattr(result, 'stdout', '') or '').strip()
    detail = str(getattr(result, 'stderr', '') or '').strip()
    return {'outcomes': list(outcomes),
            'ok': code == 0 and '"done"' in answer,
            'exit': code,
            'answer': answer[-200:],
            'detail': detail[-200:]}


def _miss_answer(io):
    """How the failed cycle was answered: `in-band` when the driver's
    own standing description names the scripted miss the protocol
    answers in-band, `severed` when it names the connection a sever
    dropped, `unattributed` otherwise — the payload whose verdict the
    leg cannot read, which the judge reports as an unread surface
    rather than as either answer."""
    described = (io or {}).get('described') or ''
    if INBAND in described:
        return 'in-band'
    if SEVERED in described:
        return 'severed'
    return 'unattributed'


def _miss_window(ctx, settled):
    """The recorded failed cycle: wait for the boundary's cumulative
    `failed_exchanges` to move past the settled baseline — a monotonic
    counter, so the window cannot be missed by polling — and read the
    driver's standing description of it beside the claim the holder
    still reports. `read` says whether any io_health read answered at
    all inside the window, which separates a monitor that never
    answered from a device that answered nothing: a window that stayed
    unread is an instability the judge never reads as a contract
    verdict."""
    floor = (settled.get('io') or {}).get('failed_exchanges')
    deadline = time.monotonic() + MISS_BOUND
    seen, answered = None, False
    while time.monotonic() < deadline:
        io = _miss_io(_try_snapshot(ctx, ctx[HOLDER_SEAT]))
        if io is not None:
            answered = True
            count = _miss_count(io.get('failed_exchanges'))
            if count is not None and (floor is None or count > floor):
                seen = io
                break
        time.sleep(MISS_POLL)
    if seen is None:
        return {'consumed': False, 'read': answered,
                'answer': 'unconsumed', 'io': None,
                'view': _miss_view(ctx, HOLDER_SEAT),
                'advanced': False}
    before = (settled.get('io') or {}).get('attempted')
    return {'consumed': True, 'read': True,
            'answer': _miss_answer(seen), 'io': seen,
            'view': _miss_view(ctx, HOLDER_SEAT),
            'advanced': _miss_count(seen.get('attempted')) is not None
                        and (before is None
                             or seen['attempted'] > before)}


def _miss_recovery(ctx, missed):
    """The following scripted outcome's completion on the same link:
    wait for the exchange successes to move past the missed cycle's
    own count, the transport's link verdict to return to connected and
    the boundary's failure streak to zero — the three states a clean
    exchange restores — then read the claim the holder reports beside
    them. `read` says whether the monitor answered inside the window,
    so a starved surface is reported apart from a device that never
    completed the exchange."""
    floor = (missed.get('io') or {}).get('succeeded')
    if floor is None:
        return {'io': None, 'read': False, 'advanced': False,
                'view': _miss_view(ctx, HOLDER_SEAT)}
    deadline = time.monotonic() + RECOVER_BOUND
    seen, answered = None, False
    while time.monotonic() < deadline:
        io = _miss_io(_try_snapshot(ctx, ctx[HOLDER_SEAT]))
        if io is not None:
            answered = True
            count = _miss_count(io.get('succeeded'))
            if io.get('link') == 'connected' \
                    and io.get('consecutive_failures') == 0 \
                    and count is not None and count > floor:
                seen = io
                break
        time.sleep(MISS_POLL)
    if seen is None:
        return {'io': None, 'read': answered, 'advanced': False,
                'view': _miss_view(ctx, HOLDER_SEAT)}
    return {'io': seen, 'read': True, 'advanced': True,
            'view': _miss_view(ctx, HOLDER_SEAT)}


def _miss_device(ctx):
    """The device server's own served read after the missed cycle,
    through the shipped control tool: the register bank it still serves
    on the connection the tool opens beside the holder's. A tool that
    answered nothing is the device no longer serving at all — the rig
    the leg reports as nondeterministic, never a claim the field's own
    arbitration still answers."""
    try:
        result = ctx['sim_bus_ctl']('list')
    except Exception as exc:
        return {'read': False, 'detail': 'the shipped control tool '
                                         'never ran: ' + str(exc)[:200]}
    code = getattr(result, 'returncode', None)
    answer = str(getattr(result, 'stdout', '') or '').strip()
    detail = str(getattr(result, 'stderr', '') or '').strip()
    return {'read': code == 0 and '"registers"' in answer,
            'exit': code, 'answer': answer[-300:],
            'detail': detail[-200:]}


def _miss_state(ctx, seat):
    """The seat container's process verdict through the runner's
    read-only state lever, or the read's failure as a string."""
    state = ctx.get('born_controller_state')
    if state is None:
        return None
    try:
        return state(seat)
    except Exception as exc:
        return {'error': str(exc)[:200]}


def _miss_teardown(ctx):
    """Best-effort teardown: the leg's seat and the device server — a
    clean pass leaves nothing standing, and an aborted pass gets the
    same sweep so the legs behind this one find the seat and the
    register protocol free. Returns the device server's own removal
    error, or None when it came down: the one half of the claim state
    the read-only state lever cannot confirm, since the device
    container is not a born seat."""
    lever = ctx.get('stop_born_controller')
    if lever is not None:
        try:
            lever(HOLDER_SEAT)
        except Exception:
            pass
    try:
        if ctx.get('stop_sim_bus_device') is None:
            return None
        ctx['stop_sim_bus_device']()
    except Exception as exc:
        return str(exc)[:200]
    return None


def _miss_rig_state(ctx, device_error):
    """The rig's claim state after the sweep: the seat's own presence
    read back through the read-only state lever — a seat still standing
    is a claim the legs behind this one would inherit — beside the
    device server's removal error. `absent` None is a read the lever
    could not answer, never a seat proven gone."""
    return {'seat': (_miss_state(ctx, HOLDER_SEAT) or {}).get('absent'),
            'device_error': device_error}


def _judge_miss(record, note):
    """Replay one pass's record — runnable against planted negatives in
    the self-check. `note(key, diagnostic, detail)` records each clause
    the record violates: CLAUSE tags the contract clauses and NONDET
    the instability the contract does not answer for."""
    def failed(key, detail):
        note(key, CLAUSE, detail)

    def nondet(key, detail):
        note(key, NONDET, detail)

    if record.get('stage_error') is not None:
        nondet('stage', 'the device server, the launch, or a control-'
               'tool call never completed: ' + str(record['stage_error']))
        return
    if record.get('script') is not None \
            and record['script'].get('ok') is not True:
        nondet('script-refused', 'the shipped control tool answered the '
               'queued scripted miss with ' + json.dumps(
                   record['script'])[:300] + ' — no outcome reached the '
               'device, so no contract verdict can be read')
    settled = record.get('settled') or {}
    missed = record.get('missed') or {}

    if missed.get('consumed') is not True:
        if missed.get('read') is not True:
            nondet('miss-window', 'the claim holder\'s monitor answered '
                   'no io_health read inside the window — the failed '
                   'cycle cannot be observed through an unread surface')
        else:
            failed('miss-unconsumed', 'the queued scripted miss never '
                   'consumed an exchange on the claim holder — the scan '
                   'recorded no failed cycle inside the bound: '
                   + json.dumps({'settled': settled.get('io'),
                                 'missed': missed})[:400])
        return
    if missed.get('answer') != 'in-band':
        if missed.get('answer') == 'unattributed':
            nondet('miss-unnamed', 'the failed cycle carried no verdict '
                   'this leg can read — the driver described neither the '
                   'in-band scripted miss nor a severed link: '
                   + json.dumps((missed.get('io') or {}).get('described')))
        else:
            failed('miss-answer', 'the queued scripted miss was answered '
                   'as a severed link, not in band — the driver '
                   'described the dropped connection where the contract '
                   'requires the `missed` answer on the live link: '
                   + json.dumps((missed.get('io') or {}).get('described')))
    if missed.get('advanced') is not True:
        failed('miss-attempted', 'the failed cycle the scan recorded is '
               'no exchange attempt past the settled baseline — the '
               'driver did not reach the device for it: '
               + json.dumps({'settled': settled.get('io'),
                             'missed': missed.get('io')})[:300])

    # The claim the missed cycle carried: still standing, still the
    # holder's own, and never paid for with a demotion.
    view = missed.get('view')
    if view is None:
        nondet('claim-view', 'the holder\'s role read never answered '
               'after the scripted miss — the claim cannot be audited '
               'through an unread surface')
    elif view.get('claim') != 'held':
        failed('claim-lost', 'the in-band scripted miss cost the holder '
               'its write-ownership claim — the served claim posture '
               'reads ' + repr(view.get('claim')) + ' where the contract '
               'requires the claim the connection carries to stay up: '
               + json.dumps(view)[:200])
    elif view.get('role') != 'active':
        failed('holder-left-active', 'the holder stopped reporting '
               'role=active across the missed cycle: '
               + json.dumps(view)[:200])

    journal = record.get('journal') or {}
    if record.get('journal_error') is not None:
        nondet('journal-unreadable', 'the holder\'s durable journal '
               'never read: ' + str(record['journal_error']))
    else:
        losses = journal.get('losses') or []
        if losses:
            failed('claim-loss-record', 'the holder\'s journal recorded '
                   + str(len(losses)) + ' field_claim_lost entr'
                   + ('y' if len(losses) == 1 else 'ies')
                   + ' across a missed cycle that must not have taken '
                   'the claim: ' + json.dumps(losses)[:300])
        walk = journal.get('walk') or []
        fenced = [entry for entry in walk
                  if entry[0] == 'active' and entry[2] == 'fenced']
        if fenced:
            failed('fenced-demotion', 'the holder walked a fenced '
                   'demotion across the missed cycle — a missed cycle '
                   'is not a lost claim: ' + json.dumps(walk)[:300])
        elif [entry for entry in walk if entry[0] == 'active']:
            failed('demoted', 'the holder left the field across the '
                   'missed cycle — the scripted queue must cost the '
                   'holder its cycle alone: '
                   + json.dumps(walk)[:300])
        token = record.get('holder_token')
        foreign = [claimant for claimant in journal.get('claimants') or []
                   if claimant != token]
        if foreign:
            failed('foreign-claimant', 'the journal observed a foreign '
                   'write-ownership claim on the holder\'s own field — '
                   'the missed cycle must leave the claim with the '
                   'holder that held it: ' + json.dumps(foreign)[:300])
        if len(journal.get('boundaries') or []) > 1:
            failed('holder-restarted', 'the holder\'s journal carries '
                   + str(len(journal.get('boundaries'))) + ' run '
                   'boundaries — a process restarted instead of holding '
                   'its claim through the missed cycle')

    # The following scripted outcome on the same link, and the device
    # still serving.
    recovered = record.get('recovered') or {}
    if recovered.get('advanced') is not True:
        if recovered.get('read') is not True:
            nondet('recovery-unreadable', 'the claim holder\'s monitor '
                   'answered no io_health read inside the recovery '
                   'window — whether the scripted complete completed on '
                   'the live link cannot be read')
        else:
            failed('no-recovery', 'the following scripted complete never '
                   'completed on the holder\'s link — the exchange '
                   'successes never moved past the missed cycle with '
                   'the link verdict back to connected: '
                   + json.dumps({'missed': (missed.get('io') or {}).get(
                       'succeeded'),
                       'recovered': recovered.get('io')})[:400])
    first = _miss_count((settled.get('view') or {}).get('tick'))
    last = _miss_count((recovered.get('view') or {}).get('tick'))
    if first is None or last is None:
        # A run clock the leg never read across the window cannot be
        # compared: an unread surface, never a scan that stopped.
        nondet('scan-view-unreadable', 'the holder\'s run tick was never '
               'read on both sides of the window — the scan advancing '
               'past the missed cycle cannot be observed: '
               + json.dumps({'settled': first, 'recovered': last}))
    elif last <= first:
        failed('scan-stalled', 'the holder\'s run tick did not advance '
               'across the missed cycle and its recovery: '
               + json.dumps({'settled': first, 'recovered': last}))
    device = record.get('device') or {}
    if device.get('read') is not True:
        nondet('device-unreadable', 'the register device answered no '
               'served read after the missed cycle — the field the '
               'contract asks to still be serving cannot be reached: '
               + json.dumps(device)[:300])

    if not _miss_pair_held(record):
        nondet('pair-disturbed', 'the deployed pair moved or wedged '
               'across the leg\'s own field staging: '
               + json.dumps(record.get('roles'), sort_keys=True)[:300])

    # The sweep, read back over the rig: a seat still standing, or a
    # device server that outlived it, is a claim the legs behind this
    # one would inherit.
    rig = record.get('rig') or {}
    if rig.get('seat') is not True or rig.get('device_error') is not None:
        nondet('rig-not-restored', 'the leg left the rig\'s claim state '
               'standing — a seat or the device server outlived the '
               'sweep the legs behind this one inherit: '
               + json.dumps(rig, sort_keys=True)[:300])


def _miss_digest(record, violations):
    """The pass's normalized verdict record — identical digests across
    two consecutive passes is the determinism contract."""
    def clean(*keys):
        return not any(key in violations for key in keys)
    return {
        'script': 'consumed'
                  if clean('script-refused', 'miss-unconsumed')
                  else 'unconsumed',
        'answer': 'in-band'
                  if clean('miss-answer', 'miss-unnamed')
                  else ('severed' if (record.get('missed') or {}).get(
                      'answer') == 'severed' else 'unread'),
        'claim': 'held'
                 if clean('claim-lost', 'claim-view')
                 else ('lost' if 'claim-lost' in violations
                       else 'unread'),
        'fencing': 'none'
                   if clean('fenced-demotion', 'demoted',
                            'claim-loss-record', 'holder-left-active',
                            'holder-restarted', 'foreign-claimant')
                   else 'walked',
        'recovery': 'completed'
                    if clean('no-recovery', 'scan-stalled',
                             'scan-view-unreadable')
                    else 'stalled',
        'device': 'serving'
                  if clean('device-unreadable') else 'unreadable',
        'pair': 'held' if clean('pair-disturbed') else 'disturbed',
        'rig': 'restored' if clean('rig-not-restored') else 'dirty'}


def _miss_pass(ctx, number, launch):
    """One pass over the scripted-miss contract: frame the deployed
    pair's roles, stage the device server, launch the claim holder onto
    the staged document and let it settle, queue the scripted `miss`
    and read the failed cycle it caused beside the claim the holder
    still reports, queue the `complete` that must answer on the same
    link, read the device's own served read, and audit the holder's
    durable journal across the whole window. The deployed pair is
    framed again afterwards; the `final` framing and the rig's
    restoration read belong to the caller, since they only mean
    anything once the sweep has run."""
    record = {'pass': number, 'launch_roles': dict(launch), 'roles': {},
              'field': {}, 'settled': {}, 'missed': {}, 'recovered': {},
              'device': {}, 'journal': {}}
    record['roles']['before'] = {
        name: _miss_pair(ctx, name) for name in (launch['owner'],
                                                 launch['peer'])}
    try:
        try:
            field = _miss_field(ctx)
        except Exception as exc:
            record['stage_error'] = ('the device server never staged: '
                                     + str(exc)[:250])
            return record
        if isinstance(field, str):
            record['inconclusive'] = field
            return record
        record['field'] = field
        try:
            record['launch'] = ctx['start_born_controller'](
                HOLDER_SEAT, None, document=field['model'])
        except Exception as exc:
            record['stage_error'] = ('the claim holder launch never ran: '
                                     + str(exc)[:250])
            return record
        # The staged document both ends read: the server's register map
        # and the attachment's dialed address come out of this one file.
        record['field']['mounted'] = (record['launch'] or {}).get('model')
        settled = _miss_settle(ctx)
        if isinstance(settled, str):
            record['inconclusive'] = settled
            return record
        record['settled'] = settled
        path = (ctx.get('journal_files') or {}).get(HOLDER_SEAT)
        if not path or not Path(path).is_file():
            record['inconclusive'] = (
                'the holder\'s --journal-file never appeared at '
                + str(path) + ' — the no-demotion audit\'s durable half '
                'cannot run')
            return record
        try:
            record['journal']['entries0'] = len(_miss_events(path))
        except (OSError, ValueError) as exc:
            record['inconclusive'] = ('the holder\'s journal file is '
                                      'unreadable: ' + str(exc)[:200])
            return record
        record['script'] = _miss_script(ctx, 'miss')
        if record['script'].get('ok') is not True:
            return record
        record['missed'] = _miss_window(ctx, settled)
        view = record['missed'].get('view')
        if record['missed'].get('answer') == 'severed' \
                and (view or {}).get('claim') != 'held':
            # The recorded defect signature, read off the rig rather
            # than assumed: the queued miss answered as a sever and the
            # claim bound to that connection went with it. That is a
            # revision predating the contract, not a failure of it —
            # and it is exactly why the evidence collection continues
            # before the pass is declined, so the run's artifacts carry
            # the signature.
            record['pre_contract'] = (
                'the queued scripted miss severed the consuming '
                'connection and released the claim bound to it — the '
                'staged revision predates the in-band missed-cycle '
                'answer')
        record['script_complete'] = _miss_script(ctx, 'complete')
        record['recovered'] = _miss_recovery(ctx, record['missed'])
        record['device'] = _miss_device(ctx)
        state = _miss_state(ctx, HOLDER_SEAT)
        if isinstance(state, dict):
            record['holder_state'] = state
        try:
            events = _miss_events(path)
            # Everything the window itself recorded: the role walk, the
            # claim losses, and the standing claimants its probes
            # refused against. The baseline floor is what keeps the
            # cold launch's own records out of the audit.
            window = events[record['journal']['entries0']:]
            record['journal']['walk'] = _miss_walk(window)
            record['journal']['losses'] = _miss_bodies(
                window, 'field_claim_lost')
            record['journal']['claimants'] = sorted({
                body.get('claimant') for body in
                _miss_bodies(window, 'field_claim_observed')
                if body.get('claimant') is not None})
            record['journal']['boundaries'] = _miss_boundaries(path)
        except (OSError, ValueError) as exc:
            record['journal_error'] = str(exc)[:200]
        return record
    finally:
        record['roles']['after'] = {
            name: _miss_pair(ctx, name) for name in (launch['owner'],
                                                     launch['peer'])}


def _miss_self_check():
    """The unchecked-diagnostic guard: replay the judge over planted
    negatives — the issue's own doctored record, one asserting the
    claim survived a severed link, a queued miss no exchange consumed, a
    failed cycle the driver did not report as the in-band scripted
    answer, a claim the in-band miss did not keep, a holder demoted,
    restarted, or fenced through the window, a journaled claim loss or
    a foreign claimant, a following complete that never completed, a
    frozen run tick — and every named instability, then report each
    that slipped."""
    def io(attempted, failed, succeeded, described=None,
           link='connected', streak=0):
        return {'failed_exchanges': failed, 'failed_reads': 0,
                'failed_writes': 0, 'consecutive_failures': streak,
                'link': link, 'error': None, 'described': described,
                'attempted': attempted, 'succeeded': succeeded,
                'last_exchange_tick': attempted}

    def clean_record():
        return {
            'pass': 1,
            'launch_roles': {'owner': 'active', 'peer': 'standby'},
            'field': {'device': 1, 'port': 9005,
                      'address': 'dcs-hw-qa-1-bus:9005',
                      'channels': ['do1', 'do2'],
                      'mounted': '/run/sim-bus/model.json'},
            'settled': {'view': {'role': 'active', 'tick': 40,
                                 'claim': 'held'},
                        'io': io(40, 0, 40)},
            'script': {'outcomes': ['miss'], 'ok': True, 'exit': 0,
                       'answer': '{\n  "result": "done"\n}',
                       'detail': ''},
            'missed': {'consumed': True, 'read': True,
                       'answer': 'in-band',
                       'io': io(41, 1, 40,
                                'the exchange did not complete: '
                                + INBAND, 'disconnected', 1),
                       'view': {'role': 'active', 'tick': 41,
                                'claim': 'held'},
                       'advanced': True},
            'script_complete': {'outcomes': ['complete'], 'ok': True,
                                'exit': 0, 'answer': 'done', 'detail': ''},
            'recovered': {'io': io(42, 1, 41), 'read': True,
                          'view': {'role': 'active', 'tick': 42,
                                   'claim': 'held'},
                          'advanced': True},
            'device': {'read': True, 'exit': 0,
                       'answer': '{"registers": []}', 'detail': ''},
            'holder_token': 424247,
            'holder_state': {'running': True, 'exit': None,
                             'absent': False},
            'journal': {'entries0': 0, 'walk': [], 'losses': [],
                        'claimants': [],
                        'boundaries': [{'run': 1, 'tick': 0}]},
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
        _judge_miss(
            record, lambda key, diagnostic, detail:
            found.setdefault(key, diagnostic))
        return found

    slipped = []
    if audit(clean_record()):
        slipped.append('clean-overstrict')

    def expect(name, mutate, diagnostic=CLAUSE):
        record = clean_record()
        mutate(record)
        if diagnostic not in audit(record).values():
            slipped.append(name)

    # The doctored negative the issue names: the record asserts the
    # claim survived while the device answered the queued miss by
    # dropping the connection — a sever the contract forbids, reported
    # in band as though it had not happened.
    expect('claim-survived-a-severed-link', lambda r:
           r['missed'].update(answer='severed', io=dict(
               r['missed']['io'],
               described='the exchange did not complete: ' + SEVERED)))
    expect('miss-never-consumed', lambda r:
           r['missed'].update(consumed=False, read=True,
                              answer='unconsumed', io=None,
                              advanced=False))
    expect('miss-never-attempted', lambda r:
           r['missed'].update(advanced=False))
    expect('miss-not-in-band', lambda r:
           r['missed'].update(answer='severed'))
    expect('claim-freed-by-the-miss', lambda r:
           r['missed']['view'].update(claim='unclaimed'))
    expect('claim-evidence-gone', lambda r:
           r['missed'].update(view={'role': 'active', 'tick': 41,
                                    'claim': None}))
    expect('holder-left-the-field', lambda r:
           r['missed']['view'].update(role='demoting'))
    expect('claim-loss-journaled', lambda r:
           r['journal'].update(losses=[{'point': 3,
                                       'claimant': 424248}]))
    expect('fenced-demotion-walk', lambda r:
           r['journal'].update(walk=[['active', 'demoting', 'fenced'],
                                     ['demoting', 'standby', 'fenced']]))
    expect('operator-demotion-walk', lambda r:
           r['journal'].update(walk=[['active', 'demoting', 'request']]))
    expect('foreign-claimant-observed', lambda r:
           r['journal'].update(claimants=[424248]))
    expect('holder-restarted', lambda r:
           r['journal'].update(boundaries=[{'run': 1, 'tick': 0},
                                           {'run': 2, 'tick': 0}]))
    expect('complete-never-answered', lambda r:
           r['recovered'].update(io=None, read=True, advanced=False))
    expect('miss-window-unreadable', lambda r:
           r['missed'].update(consumed=False, read=False), NONDET)
    expect('recovery-window-unreadable', lambda r:
           r['recovered'].update(io=None, read=False, advanced=False),
           NONDET)
    expect('scan-frozen', lambda r:
           r['recovered']['view'].update(tick=40))
    expect('scan-view-unreadable', lambda r:
           r['recovered'].update(view=None), NONDET)
    expect('script-tool-refused', lambda r:
           r.update(script={'outcomes': ['miss'], 'ok': False, 'exit': 1,
                            'answer': '',
                            'detail': 'device is busy'}), NONDET)
    expect('miss-unattributable', lambda r:
           r['missed'].update(answer='unattributed', io=dict(
               r['missed']['io'], described=None)), NONDET)
    expect('claim-view-unreadable', lambda r:
           r['missed'].update(view=None), NONDET)
    expect('journal-unreadable', lambda r:
           r.update(journal_error='journal line 4 does not parse'),
           NONDET)
    expect('device-stopped-serving', lambda r:
           r['device'].update(read=False, answer='', exit=1), NONDET)
    expect('device-server-tool-failed', lambda r:
           r.update(device={'read': False,
                            'detail': 'the shipped control tool never '
                                      'ran'}), NONDET)
    expect('pair-owner-moved', lambda r:
           r['roles']['after']['active'].update(role='standby'), NONDET)
    expect('pair-peer-lost-tracking', lambda r:
           r['roles']['final']['standby'].update(tracking=False), NONDET)
    expect('pair-scan-wedged', lambda r:
           r['roles']['after']['active'].update(tick=900), NONDET)
    expect('rig-left-standing', lambda r:
           r['rig'].update(seat=False), NONDET)
    expect('rig-presence-unreadable', lambda r:
           r['rig'].update(seat=None), NONDET)
    expect('device-server-still-serving', lambda r:
           r['rig'].update(device_error='device is busy'), NONDET)
    expect('stage-failed', lambda r:
           r.update(stage_error='docker run failed'), NONDET)
    return slipped


def scenario_sim_bus_scripted_miss_claim_hold(ctx):
    """Exercise the scripted-miss claim-hold contract on the rig's own
    register-protocol field: stage the lane's shipped device server,
    launch one born-active controller onto its staged `sim-cyclic`
    document so it takes the device's write-ownership claim, then queue
    a scripted `miss` through the shipped `dcs-sim-bus-ctl` — the
    exchange must fault the cycle in band while the claim its
    connection carries stays up: the scan records the failed cycle, the
    run keeps reporting the claim held and stays active, and its durable
    journal carries neither a `field_claim_lost` nor a fenced demotion.
    A following scripted `complete` must complete on the same link with
    the device still serving. The rig's claim state and launch roles are
    restored afterward, and two consecutive passes produce identical
    outcome digests."""
    case = Case(
        'sim-bus-scripted-miss-claim-hold',
        'A scripted sim-bus exchange miss answers in band and holds the '
        'field claim',
        'over a rig-staged sim-cyclic device whose write-ownership claim '
        'a live controller holds, a scripted `miss` queued through the '
        'shipped control tool answers in band — the driver describing '
        'the missed cycle rather than a severed link — and the scan '
        'records the failed cycle while the claim stays held, the run '
        'stays active, and its durable journal carries no field_claim_lost '
        'and no fenced demotion; a following scripted `complete` '
        'completes on the same link with the device still serving and '
        'the run tick advancing, the rig\'s claim state and launch roles '
        'restored — audited back over the swept rig, a seat or device '
        'server that outlived the sweep fails the leg — and two passes '
        'produce identical digests')
    try:
        missing = [key for key in ('start_sim_bus_device',
                                   'stop_sim_bus_device',
                                   'start_born_controller',
                                   'stop_born_controller',
                                   'born_controller_state',
                                   'sim_bus_ctl')
                   if ctx.get(key) is None]
        if missing:
            return case.finish('inconclusive', 'the run context carries '
                               'no register-protocol staging levers: '
                               + ', '.join(missing))
        if not ctx.get(HOLDER_SEAT):
            return case.finish('inconclusive', 'the run context carries '
                               'no published monitor for the leg\'s '
                               'claim-holder seat')
        journals = ctx.get('journal_files') or {}
        if not journals.get(HOLDER_SEAT):
            return case.finish('inconclusive', 'the run context carries '
                               'no journal file for the leg\'s seat — '
                               'the no-demotion audit\'s durable half '
                               'cannot run')
        tokens = ctx.get('plant_owner') or {}
        if _miss_count(tokens.get(HOLDER_SEAT)) is None:
            return case.finish('inconclusive', 'the run pins no '
                               'owner-token for the leg\'s seat — the '
                               'claim standing on the field cannot be '
                               'attributed to the holder that carries it')
        deadline = time.monotonic() + SETTLE_BOUND
        owner = wait_for(lambda: _pair_active(ctx), deadline,
                         interval=SETTLE_POLL)
        if owner is None:
            reports = {name: _try_role(ctx, ctx[name])
                       for name in ('active', 'standby')
                       if ctx.get(name)}
            if not reports or all(report is None
                                  for report in reports.values()):
                return case.finish(
                    'inconclusive', 'the deployed pair is unreachable — '
                    'monitor endpoints ' + str(ctx.get('active'))
                    + ' and ' + str(ctx.get('standby')))
            return case.finish('failed', 'no peer reports role=active')
        peer = 'standby' if owner == 'active' else 'active'
        if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                    interval=SETTLE_POLL) is None:
            return case.finish('inconclusive', 'the pair has no tracking '
                               'standby — the settled posture the leg '
                               'proves undisturbed was never reached')
        launch = {'owner': owner, 'peer': peer}
        case.observe('field owner: ' + owner + ' (' + ctx[owner] + '); '
                     'tracking peer: ' + peer + ' (' + ctx[peer] + ')')
        digests = []
        for number in (1, 2):
            violations = {}

            def note(key, diagnostic, detail):
                violations.setdefault(key, (diagnostic, detail))

            record = _miss_pass(ctx, number, launch)
            # Each pass ends with the rig swept — the seat and the
            # device server removed so the next pass and the legs
            # behind this one start on a free seat and a free field.
            device_error = _miss_teardown(ctx)
            # The restoration audit, read back over the swept rig: the
            # seat's own presence and the pair's launch roles once the
            # leg's claim is gone.
            record['rig'] = _miss_rig_state(ctx, device_error)
            record['roles']['final'] = {
                name: _miss_pair(ctx, name) for name in (owner, peer)}
            record['holder_token'] = tokens.get(HOLDER_SEAT)
            if not (record.get('inconclusive')
                    or record.get('pre_contract')):
                _judge_miss(record, note)
                digest = _miss_digest(record, violations)
                record['digest'] = dict(digest)
            else:
                digest = None
            record['violations'] = {
                key: diagnostic
                for key, (diagnostic, _) in violations.items()}
            ref = save_evidence(
                ctx['evidence_dir'],
                'sim-bus-scripted-miss-claim-hold-pass-'
                + str(number) + '.json', record)
            case.evidence('file', ref,
                          'sim-bus-scripted-miss pass ' + str(number)
                          + ' — the staged device server and the '
                          'mounted document, the claim holder\'s settle, '
                          'the queued scripted miss and the failed cycle '
                          'it recorded beside the claim still held, the '
                          'following scripted complete and the recovery '
                          'on the same link, the device\'s own served '
                          'read, the holder\'s durable journal, the '
                          'deployed pair\'s before/after/final framing, '
                          'the swept rig\'s restoration read, and the '
                          'normalized digest')
            if record.get('inconclusive'):
                return case.finish('inconclusive', record['inconclusive'])
            if record.get('pre_contract'):
                return case.finish('inconclusive',
                                   record['pre_contract'])
            if violations:
                name = CLAUSE if any(
                    diagnostic == CLAUSE
                    for diagnostic, _ in violations.values()) \
                    else NONDET
                return case.finish(
                    'failed', name + ': ' + '; '.join(
                        detail for _, detail in
                        list(violations.values())[:4]))
            digests.append(digest)
        if digests[0] != digests[1]:
            return case.finish(
                'failed', NONDET + ': the two passes\' digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two sim-bus scripted-miss passes, identical '
                     'digests: ' + json.dumps(digests[0], sort_keys=True))
        # The unchecked-diagnostic self-check: the judge replays each
        # planted negative it must name; a silent judge means the leg
        # can no longer catch what it names.
        slipped = _miss_self_check()
        if slipped:
            return case.finish('failed', UNCHECKED
                               + ': planted negatives slipped the '
                               'leg’s own audits: '
                               + ', '.join(slipped))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))