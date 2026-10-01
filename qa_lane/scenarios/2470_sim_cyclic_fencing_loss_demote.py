"""The sim_cyclic_fencing_loss_demote acceptance leg — one module per leg
of the scenario schedule; see qa_lane/scenarios/__init__.py for the
ordering rule and the shared seam."""
from .common import *

# Ordering: the leg stages its own pair on the lane's register-protocol
# device, so it needs the born seats the earlier legs hold — the
# foreign seat the doomed-startup-claim leg launches onto and the
# driven seat the ownerless-backoff leg's scratch launch occupies — and
# it must be done before the scan-batch leg claims the driven seat
# again.
RUNS_AFTER = frozenset({'scenario_doomed_startup_claim',
                        'scenario_ownerless_remote_backoff'})
RUNS_BEFORE = frozenset({'scenario_scan_batch_bound'})


# --------------------------------------------------------------------
# The sim-cyclic fencing-loss demotion contract — the per-revision lane
# evidence for the contract #1352's fix establishes, the single-writer
# and one-active role invariant WW-FND-002 requires: losing the
# write-ownership token must demote the ex-owner exactly as a fenced
# point write does, so at most one controller ever reports role:active.
#
# The defect: a `sim-cyclic` device's write phase only *stages* — the
# whole process image is published by the scan's `exchange` — so the
# field's fencing verdict arrives at the exchange, and CyclicBusDriver's
# lazy re-attach loses the connection-bound claim with the dropped
# connection while every later exchange comes back Fenced. That verdict
# reached io_health as a point-level Disconnected, so the claim-loss
# mark never fired: a fenced-out zombie reported `active` beside a
# legitimately promoted peer, with no claim event journaled at all.
#
# The staging is the rig's own cyclic device: the lane's shipped
# dcs-sim-bus-device server serving the run config's staged model, and a
# two-controller pair launched onto that staged document on two born
# seats (through the `--peer`/`--standby` wiring the born launcher
# carries) with no --remote attachment — a register-protocol model
# carries its device address in its own parameters. The pair settles
# exactly as any redundant pair does: the launched active holds the
# device server's single-writer claim, the other member tracks it.
#
# The sever is `restart_sim_bus_device`: every attachment's control
# connection drops at once and the same server comes back on the same
# bridge address, so the connection-bound claim releases with its dead
# holders and the field reopens claimed by nobody — the recorded link
# flap, staged durably by the rig. Nothing re-arms the dropped claim,
# so the ex-owner keeps reporting `active` on writes the unclaimed
# field accepts; POST /promote on the standby then takes the claim, and
# the ex-owner's next staged exchange meets the fence.
#
# The assertions, through the pair's own serving monitors and the
# durable --journal-file each seat bind-mounts:
#   * the ex-owner demotes through the named fencing-loss path inside
#     the documented bound — role_changed active->demoting->standby with
#     origin `fenced` (never an unattributed operator request) and
#     exactly one field_claim_lost, attributed to the promoted peer's
#     own owner token;
#   * at every poll after the promotion exactly one peer reports
#     role:active — the dual-active state the finding saw is the
#     contract's core failure — while the ex-owner's monitor keeps
#     answering throughout (degrade, never death);
#   * the field resumes stepping under the promoted owner: its exchange
#     counters keep advancing with no failure streak standing, the claim
#     reads held, and its run tick keeps moving;
#   * the demoted peer's recorded recovery path: the staged output image
#     the demotion released is gone, so its exchanges run census-only
#     and complete rather than fencing forever, and it settles standby
#     on an honest sync verdict;
#   * the pair reconverges to the roles it was launched with — the
#     documented demote-then-promote order returns the launch-active
#     seat to the field — and the deployed pair is undisturbed
#     throughout.
#
# Named diagnostics: cyclic-fencing-loss-failed tags the contract
# clauses (the ex-owner still role:active past the bound, two peers
# active at one poll, a demotion that never journaled the fenced origin
# or its single claim loss, a claim loss naming no claimant or the
# wrong one, a field that stopped stepping under the promoted owner, a
# demoted ex-owner still fencing or reporting a verdict its recovery
# path cannot hold, a pair that never reconverged to its launch roles,
# a seat process that restarted instead of degrading in place) and
# cyclic-fencing-loss-nondeterministic tags the instability the
# contract does not answer for: a refused staging call, a starved
# watch, an unread container or journal verdict, a refused promote, a
# moved or wedged deployed pair, or two passes whose digests diverge. A
# staged revision predating the contract is inconclusive: the absent
# device-server capability, a staged model declaring no `sim-cyclic`
# device or no output channel to fence, a pair that never settled, or
# the recorded defect signature itself — the ex-owner whose io_health
# never once named a fenced exchange and who stayed active beside its
# promoted peer, the point-level Disconnected translation a pre-#1352
# build reports. The unchecked-diagnostic self-check replays the judge
# over planted negatives and reports cyclic-fencing-loss-unchecked for
# any that slip through.

OWNER_SEAT = 'driven'      # the born seat launched field-active
PEER_SEAT = 'foreign'      # the born seat launched as the tracking member
CYCLIC_KIND = 'sim-cyclic'  # the device kind this contract is about

LAUNCH_BOUND = 90.0        # bound on the launched pair's settle
FLAP_BOUND = 30.0          # bound on the claim's release after the sever
DEMOTE_BOUND = 30.0        # the documented demotion bound — the demotion
                           # must land inside the ex-owner's next scan
                           # after its fenced exchange, so a bound
                           # spanning hundreds of the seats' 100ms scan
                           # periods reads a demotion that never came
STEP_WINDOW = 3.0          # the field-stepping observation window
RESTORE_BOUND = 90.0       # bound on the reconcile and role restores
WATCH_POLL = 0.1           # the demotion watch's sampling cadence
SETTLE_POLL = 0.5          # the settle/restore waits' cadence
IO_EVERY = 3               # rounds between the io_health reads
DIAG_FAILED = 'cyclic-fencing-loss-failed'
DIAG_NONDET = 'cyclic-fencing-loss-nondeterministic'
DIAG_UNCHECKED = 'cyclic-fencing-loss-unchecked'

# The sync verdicts a demoted standby may honestly report on its way
# back to the line: 'tracking' once it has adopted the promoted peer's
# checkpoints, and the recoverable verdicts its pulls may read until
# then.
_HONEST_SYNC = ('tracking', 'unsynchronized', 'degraded', 'orphaned',
                'diverged', 'reinitialized')


def _cyclic_sync(value):
    """The served StandbySync's variant name — a bare string or a
    single-key object, depending on the payload shape."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and value:
        return next(iter(value))
    return None


def _cyclic_io(snapshot):
    """The io_health half the fencing contract reads, normalized: the
    boundary counters, the transport's link verdict, the cyclic exchange
    counters, and the named driver error the most recent boundary
    failure reported — `Fenced` is the contract's evidence that the
    field refused this run's image exchange, `Disconnected`/`Timeout`
    the transport verdict a fenced exchange used to be translated into.
    None while the read has not answered."""
    if not isinstance(snapshot, dict):
        return None
    health = snapshot.get('io_health')
    if not isinstance(health, dict):
        return None
    driver = health.get('driver') or {}
    exchange = driver.get('exchange') or {}
    error = (health.get('last_error') or {}).get('error')
    if isinstance(error, dict) and error:
        error = next(iter(error))
    return {
        'failed_exchanges': health.get('failed_exchanges'),
        'failed_writes': health.get('failed_writes'),
        'consecutive_failures': health.get('consecutive_failures'),
        'link': driver.get('link'),
        'attempted': exchange.get('attempted'),
        'succeeded': exchange.get('succeeded'),
        'last_exchange_tick': exchange.get('last_exchange_tick'),
        'error': error if isinstance(error, str) else None}


def _cyclic_fenced(io_views):
    """Whether any sampled io_health named a fenced exchange — the
    contract's own evidence that the field refused this run's staged
    image rather than a transport it could not reach."""
    return any((view or {}).get('error') == 'Fenced'
               for view in io_views or [])


def _cyclic_count(value):
    """A served counter as an int, or None — a payload that carries no
    integer where the contract reads one is a shape the judge reports,
    never a comparison that silently passes."""
    return value if isinstance(value, int) and not isinstance(value, bool) \
        else None


def _cyclic_advanced(before, after):
    """Whether the exchange counters advanced from one sampled
    io_health to a later one — the device still answering this run's
    own exchanges."""
    first = _cyclic_count((before or {}).get('attempted'))
    second = _cyclic_count((after or {}).get('attempted'))
    return first is not None and second is not None and second > first


def _cyclic_role_view(ctx, seat):
    """The seat's served RoleReport, or None while unreachable."""
    return _try_role(ctx, ctx[seat])


def _cyclic_state(ctx, seat):
    """The seat container's process verdict through the runner's
    read-only state lever, or the read's failure as a string."""
    state = ctx.get('born_controller_state')
    if state is None:
        return None
    try:
        return state(seat)
    except Exception as exc:
        return {'error': str(exc)[:200]}


def _cyclic_events(path):
    """Every event body a seat's `--journal-file` records, in append
    order."""
    return [(item.get('entry') or {}).get('event')
            for item in _journal_entries(path)
            if isinstance(item.get('entry'), dict)]


def _cyclic_boundaries(path):
    """The run-boundary markers a seat's journal carries — one per
    process lifetime the file records."""
    return [item['run_boundary'] for item in _journal_entries(path)
            if 'run_boundary' in item]


def _cyclic_walk(events):
    """The (from, to, origin) role transitions a journal event list
    carries, in record order."""
    walk = []
    for event in events:
        change = (event or {}).get('role_changed')
        if isinstance(change, dict):
            walk.append((change.get('from'), change.get('to'),
                         change.get('origin')))
    return walk


def _cyclic_losses(events):
    """The field_claim_lost records a journal event list carries, in
    record order."""
    return [event['field_claim_lost'] for event in events
            if isinstance(event, dict) and 'field_claim_lost' in event]


def _cyclic_demoted(walk):
    """Whether the walk demotes in place through the fencing path:
    active->demoting followed by demoting->standby, both attributed to
    the peer's own protective demotion rather than to a request."""
    fenced = ('active', 'demoting', 'fenced')
    if fenced not in walk:
        return False
    return ('demoting', 'standby', 'fenced') in walk[walk.index(fenced) + 1:]


def _cyclic_pair_view(ctx, name):
    """The deployed pair's normalized role evidence for one member —
    role, run tick, tracking posture; None when the read dropped."""
    report = _try_role(ctx, ctx[name])
    if not isinstance(report, dict):
        return None
    return {'role': report.get('role'), 'tick': report.get('tick'),
            'tracking': 'tracking' in (report.get('sync') or {})}


def _cyclic_pair_held(record):
    """The deployed pair's undisturbed verdict: the owner still active
    and advancing its scan across the leg's own field staging, the peer
    still a tracking standby — before and after alike. `launch_roles`
    names the deployed pair's endpoint keys per role."""
    launch = record.get('launch_roles') or {}
    roles = record.get('roles') or {}
    before = roles.get('before') or {}
    after = roles.get('after') or {}
    owner, peer = launch.get('owner'), launch.get('peer')
    for view in (before, after):
        if (view.get(owner) or {}).get('role') != 'active':
            return False
        seen = view.get(peer) or {}
        if seen.get('role') != 'standby' or seen.get('tracking') is not True:
            return False
    first = _cyclic_count((before.get(owner) or {}).get('tick'))
    last = _cyclic_count((after.get(owner) or {}).get('tick'))
    return first is not None and last is not None and last > first


def _cyclic_field(ctx):
    """Stage the lane's register-protocol device server and prove the
    model it serves is the field this contract is about: the run
    config's staged device declared `sim-cyclic`, with an output channel
    the scan's staged image publishes — a device with no outputs stages
    nothing, and an empty image is never fenced. Returns the launch
    dict, or the inconclusive reason as a string. A launch that raises
    is no inconclusive reason but a refused staging call: it propagates
    so the pass records it as the instability it is."""
    spec = ctx.get('sim_bus_device')
    if not spec:
        return ('the run config stages no sim-bus device server — this '
                'leg needs the lane\'s register-protocol field')
    field = ctx['start_sim_bus_device']()
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
                    + repr(declared.get('kind')) + ' — point '
                    'sim_bus_device.model_fixture at a model declaring a '
                    + CYCLIC_KIND + ' device for this leg')
        outputs = [name for name, channel
                   in (declared.get('channels') or {}).items()
                   if (channel or {}).get('direction') == 'out']
        if not outputs:
            return ('the staged ' + CYCLIC_KIND + ' device declares no '
                    'output channel — its scan stages no image, and an '
                    'empty exchange is never fenced')
        field['channels'] = outputs
        return field
    return 'the staged device model declares no device ' \
        + str(spec.get('device'))


def _cyclic_settle(ctx):
    """Wait for the staged pair's documented settle: the launch-active
    seat reporting active with the device claim held, the other member a
    tracking standby. Returns {'owner', 'peer'} or the inconclusive
    reason under 'inconclusive'."""
    deadline = time.monotonic() + LAUNCH_BOUND
    owner = wait_for(
        lambda: (lambda report: report
                 if isinstance(report, dict)
                 and report.get('role') == 'active'
                 and report.get('field_claim') == 'held'
                 else None)(_cyclic_role_view(ctx, OWNER_SEAT)),
        deadline, interval=SETTLE_POLL)
    if owner is None:
        return {'inconclusive': 'the launched active never settled with '
                     'the device claim held — the staged pair did not '
                     'come up'}
    peer = wait_for(lambda: _tracking_standby(ctx, PEER_SEAT), deadline,
                    interval=SETTLE_POLL)
    if peer is None:
        return {'owner': owner,
                'inconclusive': 'the pair\'s tracking member never '
                     'converged — the promote this leg drives has no '
                     'converged target'}
    return {'owner': owner, 'peer': peer}


def _cyclic_flap(ctx):
    """Sever the field's control connections and wait for the claim's
    release to nobody: the ex-owner's claim probe answers `unclaimed`
    once its attachment re-attaches to the restarted server. Returns the
    flap's evidence, or the inconclusive reason as a string."""
    ctx['restart_sim_bus_device']()
    released = wait_for(
        lambda: (lambda report: report
                 if isinstance(report, dict)
                 and report.get('field_claim') == 'unclaimed'
                 else None)(_cyclic_role_view(ctx, OWNER_SEAT)),
        time.monotonic() + FLAP_BOUND, interval=SETTLE_POLL)
    if released is None:
        return ('the sever never released the connection-bound claim to '
                'nobody — the ex-owner still reads the device claim '
                'held, so the induction did not sever the control '
                'connection it claims to')
    return {'owner': released, 'peer': _cyclic_role_view(ctx, PEER_SEAT)}


def _cyclic_watch(ctx):
    """The post-promotion watch: both monitors at the sampling cadence,
    the io_health pair every IO_EVERY rounds, and the count of peers
    reporting role:active at each poll — the one-active invariant the
    promotion must hold at every sample. Ends at the settle (the
    ex-owner standby, the promoted peer active) or at the bound."""
    watch = {'polls': [], 'settled': None, 'read_errors': 0,
             'state_error': None}
    deadline = time.monotonic() + DEMOTE_BOUND
    while time.monotonic() < deadline:
        owner = _try_role(ctx, ctx[OWNER_SEAT])
        peer = _try_role(ctx, ctx[PEER_SEAT])
        sample = {'owner': (owner or {}).get('role'),
                  'peer': (peer or {}).get('role')}
        if owner is None or peer is None:
            watch['read_errors'] += 1
        sample['active'] = sum(
            1 for role in (sample['owner'], sample['peer'])
            if role == 'active')
        if not watch['polls'] or len(watch['polls']) % IO_EVERY == 0:
            sample['owner_io'] = _cyclic_io(
                _try_snapshot(ctx, ctx[OWNER_SEAT]))
            sample['peer_io'] = _cyclic_io(
                _try_snapshot(ctx, ctx[PEER_SEAT]))
        watch['polls'].append(sample)
        if sample['owner'] == 'standby' and sample['peer'] == 'active':
            watch['settled'] = sample
            break
        time.sleep(WATCH_POLL)
    return watch


def _cyclic_stepping(ctx, record):
    """The field-stepping evidence over the observation window: the
    promoted peer's role, claim, run tick and exchange counters beside
    the demoted ex-owner's — census-only exchanges that keep completing
    are the release half of the contract, ones that keep fencing are
    not."""
    time.sleep(STEP_WINDOW)
    promoted = _cyclic_role_view(ctx, PEER_SEAT) or {}
    demoted = _cyclic_role_view(ctx, OWNER_SEAT) or {}
    return {
        'promoted': {'role': promoted.get('role'),
                     'claim': promoted.get('field_claim'),
                     'sync': _cyclic_sync(promoted.get('sync')),
                     'tick': [record.get('promoted_tick'),
                              promoted.get('tick')],
                     'io': _cyclic_io(_try_snapshot(ctx, ctx[PEER_SEAT]))},
        'demoted': {'role': demoted.get('role'),
                    'sync': _cyclic_sync(demoted.get('sync')),
                    'io': _cyclic_io(_try_snapshot(ctx, ctx[OWNER_SEAT]))}}


def _cyclic_restore(ctx):
    """The documented demote-then-promote order returning the pair to
    the roles it was launched with: the demoted seat reconverges
    tracking behind its successor, the successor stands down, and the
    launch-active seat takes the field back. Returns the failure detail
    or None."""
    if wait_for(lambda: _tracking_standby(ctx, OWNER_SEAT),
                time.monotonic() + RESTORE_BOUND,
                interval=SETTLE_POLL) is None:
        return ('the demoted seat never rejoined the line as a tracking '
                'standby — its recovery path never converged, so the '
                'restore has no promotable peer')
    status, body = _settle_call(ctx[PEER_SEAT] + '/demote')
    if status != 200:
        return 'the restore demote answered ' + str(status) + ': ' \
            + json.dumps(body)[:300]
    status, body = _settle_call(ctx[OWNER_SEAT] + '/promote')
    if status != 200:
        return 'the restore promote answered ' + str(status) + ': ' \
            + json.dumps(body)[:300]
    if wait_for(
            lambda: (_cyclic_role_view(ctx, OWNER_SEAT) or {}).get('role')
            == 'active' and _tracking_standby(ctx, PEER_SEAT),
            time.monotonic() + RESTORE_BOUND, interval=SETTLE_POLL) is None:
        return ('the pair did not settle back to the roles it was '
                'launched with — one active plus one tracking standby')
    return None


def _cyclic_teardown(ctx):
    """Best-effort teardown: both launched seats and the device server —
    a clean pass leaves nothing standing, and an aborted pass gets the
    same sweep so the legs behind this one find their seats free."""
    for seat in (OWNER_SEAT, PEER_SEAT):
        try:
            ctx['stop_born_controller'](seat)
        except Exception:
            pass
    try:
        ctx['stop_sim_bus_device']()
    except Exception:
        pass


def _judge_cyclic(record, note):
    """Replay one pass's record — runnable against planted negatives in
    the self-check. `note(key, diagnostic, detail)` records each clause
    the record violates: DIAG_FAILED tags the contract clauses and
    DIAG_NONDET the instability the contract does not answer for."""
    def failed(key, detail):
        note(key, DIAG_FAILED, detail)

    def nondet(key, detail):
        note(key, DIAG_NONDET, detail)

    if record.get('stage_error') is not None:
        nondet('stage', 'the field staging, the sever, or the pair '
               'launch never completed: ' + str(record['stage_error']))
        return
    if record.get('promote_refused') is not None:
        nondet('promote-refused', 'the promote on the tracking standby '
               'was refused: ' + json.dumps(record['promote_refused'])[:300])
        return
    watch = record.get('watch') or {}
    polls = watch.get('polls') or []
    if not polls:
        nondet('watch-starved', 'the pair\'s monitors answered no poll '
               'after the promotion')
        return
    if watch.get('state_error') is not None:
        nondet('state-read', 'the ex-owner\'s container verdict never '
               'read: ' + str(watch['state_error']))
    if record.get('journal_error') is not None:
        nondet('journal-unreadable', 'the ex-owner\'s durable journal '
               'never read: ' + str(record['journal_error']))
    if watch.get('read_errors'):
        state = record.get('owner_state') or {}
        if isinstance(state, dict) and (
                state.get('absent')
                or (state.get('running') is False
                    and state.get('exit') is not None)):
            failed('owner-exited', 'the fenced ex-owner\'s process '
                   'stopped answering (exit ' + str(state.get('exit'))
                   + ') instead of degrading in place')
        else:
            nondet('watch-starved', str(watch['read_errors'])
                   + ' monitor poll(s) after the promotion did not '
                   'answer while the ex-owner\'s process stands')
    if not any(poll.get('owner') is not None
               and poll.get('peer') is not None for poll in polls):
        # The promotion left the pair's own surfaces unreadable: the
        # demotion is not observable, so the leg reports the starved
        # watch rather than a contract verdict it never read. The
        # deployed pair's stillness is still the leg's to report.
        nondet('watch-starved', 'the pair reported no readable role at '
               'any poll after the promotion — the demotion cannot be '
               'observed through an unreadable surface')
        if not _cyclic_pair_held(record):
            nondet('pair-disturbed', 'the deployed pair moved or wedged '
                   'across the leg\'s own field staging: '
                   + json.dumps(record.get('roles'), sort_keys=True)[:300])
        return

    # The demotion itself: the ex-owner must leave the field inside the
    # documented bound, and at no poll may a second peer report active
    # beside it — the dual-active state the finding recorded.
    if polls[-1].get('owner') == 'active':
        failed('demotion-missed', 'the fenced ex-owner still reported '
               'role=active ' + str(DEMOTE_BOUND) + 's after the '
               'promotion — its dead field writes never demoted it: '
               + json.dumps(polls[-3:])[:300])
    if not any(poll.get('owner') == 'demoting' for poll in polls):
        failed('no-demoting', 'the ex-owner\'s reported role never '
               'walked demoting: '
               + json.dumps([poll.get('owner') for poll in polls])[:300])
    dual = [poll for poll in polls if (poll.get('active') or 0) > 1]
    if dual:
        failed('dual-active', 'two peers reported role=active at '
               + str(len(dual)) + ' poll(s) after the promotion — the '
               'one-active invariant: ' + json.dumps(dual[:3])[:300])
    if polls[-1].get('peer') != 'active':
        failed('promoted-lost', 'the promoted peer did not settle '
               'active: '
               + json.dumps([poll.get('peer') for poll in polls])[:300])

    # The durable half: the named path, its attribution, and the single
    # claim loss — the record the finding saw missing entirely. An
    # unreadable journal is an evidence gap, not a violated clause.
    journal = record.get('journal') or {}
    if not record.get('journal_error'):
        walk = journal.get('walk') or []
        losses = journal.get('losses') or []
        if not _cyclic_demoted(walk):
            failed('unjournaled-path', 'the ex-owner\'s durable journal '
                   'never recorded role_changed active->demoting->standby '
                   'with origin fenced — the demotion ran on an '
                   'unattributed path: ' + json.dumps(walk)[:400])
        if len(losses) != 1:
            failed('claim-loss-record', 'expected exactly one '
                   'field_claim_lost on the ex-owner\'s journal, found '
                   + str(len(losses)) + ': ' + json.dumps(losses)[:300])
        elif losses[0].get('claimant') != record.get('promoted_token'):
            failed('unattributed', 'the journaled field_claim_lost named '
                   + repr(losses[0].get('claimant')) + ' as the claimant, '
                   'not the promoted peer\'s own owner token '
                   + repr(record.get('promoted_token')))
        if len(journal.get('boundaries') or []) != 1:
            failed('owner-restarted', 'the ex-owner\'s journal carries '
                   + str(len(journal.get('boundaries') or []))
                   + ' run boundaries — a process restarted instead of '
                   'demoting in place')

    # The field under the promoted owner: the exchanges keep completing,
    # the claim is held, and the run tick keeps moving.
    stepping = record.get('stepping') or {}
    promoted = stepping.get('promoted') or {}
    promoted_io = promoted.get('io') or {}
    if promoted.get('role') != 'active' or promoted.get('claim') != 'held':
        failed('claim-not-held', 'the promoted owner does not hold the '
               'device claim after the takeover: '
               + json.dumps({'role': promoted.get('role'),
                             'claim': promoted.get('claim')})[:300])
    if not _cyclic_advanced(record.get('promoted_io'), promoted_io) \
            or (promoted_io.get('consecutive_failures') or 0) != 0 \
            or promoted_io.get('link') != 'connected':
        failed('field-stalled', 'the promoted owner\'s exchanges did '
               'not keep completing over the device — the field never '
               'resumed stepping under it: '
               + json.dumps({'before': record.get('promoted_io'),
                             'after': promoted_io})[:400])
    ticks = [tick for tick in promoted.get('tick') or []
             if _cyclic_count(tick) is not None]
    if len(ticks) != 2 or ticks[1] <= ticks[0]:
        failed('owner-scan-stalled', 'the promoted owner\'s run tick '
               'did not advance across the observation window: '
               + json.dumps(promoted.get('tick'))[:200])

    # The demoted ex-owner: census-only exchanges that complete (the
    # release half of the contract) on an honest recovery verdict, and
    # still no second active peer once the settle window has passed.
    demoted = stepping.get('demoted') or {}
    demoted_io = demoted.get('io') or {}
    if demoted.get('role') == 'active':
        failed('dual-active', 'the ex-owner reported role=active again '
               'across the observation window after its demotion '
               'settled — the one-active invariant: '
               + json.dumps(demoted)[:300])
    if (demoted_io.get('consecutive_failures') or 0) != 0 \
            or demoted_io.get('link') != 'connected':
        failed('ex-owner-fenced', 'the demoted ex-owner\'s exchanges '
               'still meet the fence — its staged output image was not '
               'released, so it never returns to census-only '
               'exchanges: ' + json.dumps(demoted_io)[:300])
    if not _cyclic_advanced(journal.get('fenced_io'), demoted_io):
        failed('ex-owner-stalled', 'the demoted ex-owner stopped '
               'exchanging with the device altogether: '
               + json.dumps(demoted_io)[:300])
    if demoted.get('sync') not in _HONEST_SYNC:
        failed('recovery-path', 'the demoted ex-owner reports a sync '
               'verdict its recovery path cannot hold: '
               + repr(demoted.get('sync')))

    # The pair's own reconcile, and the deployed pair's stillness.
    if record.get('restored') is not None:
        failed('not-restored', 'the pair never reconverged to its launch '
               'roles: ' + str(record['restored']))
    if not _cyclic_pair_held(record):
        nondet('pair-disturbed', 'the deployed pair moved or wedged '
               'across the leg\'s own field staging: '
               + json.dumps(record.get('roles'), sort_keys=True)[:300])


def _cyclic_digest(record, violations):
    """The pass's normalized verdict record — identical digests across
    two consecutive passes is the determinism contract."""
    def clean(*keys):
        return not any(key in violations for key in keys)
    polls = (record.get('watch') or {}).get('polls') or []
    losses = (record.get('journal') or {}).get('losses') or []
    return {
        'demotion': 'fenced'
                    if clean('demotion-missed', 'no-demoting',
                             'ex-owner-fenced', 'ex-owner-stalled',
                             'owner-exited', 'recovery-path',
                             'owner-restarted')
                    else 'unfenced',
        'active': 'single' if clean('dual-active', 'promoted-lost')
                  else 'dual',
        'journal': 'attributed'
                   if clean('claim-loss-record', 'unattributed',
                            'unjournaled-path')
                   else ('unattributed'
                         if losses and losses[0].get('claimant') is None
                         else 'silent'),
        'field': 'stepping'
                 if clean('field-stalled', 'owner-scan-stalled',
                          'claim-not-held')
                 else 'stalled',
        'pair': 'restored' if clean('not-restored') else 'unrestored',
        'verdict': 'fenced'
                   if _cyclic_fenced([poll.get('owner_io')
                                     for poll in polls])
                   else 'unfenced'}


def _cyclic_pass(ctx, number, launch):
    """One pass over the fencing-loss demotion: stage the rig's cyclic
    field, launch the pair onto it, settle, sever the control
    connections, promote the standby, watch the ex-owner demote through
    the named path, read the field's stepping and the durable journal,
    and restore the pair's launch roles — the deployed pair framed
    before and after."""
    record = {'pass': number, 'launch_roles': dict(launch), 'roles': {},
              'watch': {}, 'journal': {}, 'stepping': {}, 'recovery': {}}
    record['roles']['before'] = {name: _cyclic_pair_view(ctx, name)
                                 for name in launch.values()}
    try:
        try:
            field = _cyclic_field(ctx)
        except Exception as exc:
            record['stage_error'] = ('the device server never staged: '
                                     + str(exc)[:250])
            return record
        if isinstance(field, str):
            record['inconclusive'] = field
            return record
        record['field'] = field
        try:
            for seat, wiring in ((OWNER_SEAT, {'peer': PEER_SEAT}),
                                 (PEER_SEAT, {'standby': OWNER_SEAT})):
                ctx['start_born_controller'](seat, None,
                                             document=field['model'],
                                             **wiring)
        except Exception as exc:
            record['stage_error'] = 'the pair launch never ran: ' \
                + str(exc)[:250]
            return record
        settled = _cyclic_settle(ctx)
        if settled.get('inconclusive'):
            record['inconclusive'] = settled['inconclusive']
            return record
        record['settled'] = settled
        journals = ctx.get('journal_files') or {}
        owner_journal = journals.get(OWNER_SEAT)
        if not owner_journal or not Path(owner_journal).is_file():
            record['inconclusive'] = (
                'the ex-owner\'s --journal-file never appeared at '
                + str(owner_journal) + ' — the demotion\'s durable half '
                'cannot run')
            return record
        try:
            record['journal']['boundaries0'] = _cyclic_boundaries(
                owner_journal)
            record['journal']['entries0'] = len(
                _cyclic_events(owner_journal))
        except (OSError, ValueError) as exc:
            record['inconclusive'] = ('the ex-owner\'s journal file is '
                                      'unreadable: ' + str(exc)[:200])
            return record
        record['promoted_token'] = (ctx.get('plant_owner')
                                    or {}).get(PEER_SEAT)
        try:
            flap = _cyclic_flap(ctx)
        except Exception as exc:
            record['stage_error'] = ('the control-connection sever never '
                                     'ran: ' + str(exc)[:250])
            return record
        if isinstance(flap, str):
            record['inconclusive'] = flap
            return record
        record['flap'] = flap
        # The promoted peer's own baseline, read after the sever's
        # release settled: the flap's dead socket may cost either peer
        # one transport failure, and the contract's field evidence is
        # what happens after it.
        record['promoted_tick'] = (settled.get('peer') or {}).get('tick')
        record['promoted_io'] = _cyclic_io(
            _try_snapshot(ctx, ctx[PEER_SEAT]))
        status, body = _settle_call(ctx[PEER_SEAT] + '/promote')
        if status != 200:
            record['promote_refused'] = {'status': status, 'body': body}
            return record
        record['promote'] = {'status': status, 'body': body}
        record['watch'] = _cyclic_watch(ctx)
        state = _cyclic_state(ctx, OWNER_SEAT)
        if isinstance(state, dict) and state.get('error'):
            record['watch']['state_error'] = state['error']
        else:
            record['owner_state'] = state
        polls = record['watch']['polls']
        fenced = [poll for poll in polls
                  if (poll.get('owner_io') or {}).get('error') == 'Fenced']
        record['journal']['fenced_io'] = (fenced[0].get('owner_io')
                                          if fenced else None)
        try:
            events = _cyclic_events(owner_journal)
            record['journal']['events'] = events[
                record['journal']['entries0']:]
            record['journal']['walk'] = _cyclic_walk(
                record['journal']['events'])
            record['journal']['losses'] = _cyclic_losses(
                record['journal']['events'])
            record['journal']['boundaries'] = _cyclic_boundaries(
                owner_journal)
        except (OSError, ValueError) as exc:
            record['journal_error'] = str(exc)[:200]
        # The pre-contract signature: the fenced exchange never surfaced
        # as fencing at all — it reached io_health as a point-level
        # transport verdict and the ex-owner never left the field. That
        # is a revision predating the contract, not a failure of it.
        if polls and polls[-1].get('owner') == 'active' \
                and not _cyclic_fenced([poll.get('owner_io')
                                        for poll in polls]):
            record['inconclusive'] = (
                'the ex-owner\'s io_health never named a fenced exchange '
                'and it stayed role=active beside its promoted peer — '
                'the refused image exchange surfaced as a point-level '
                'transport verdict instead: the staged revision '
                'predates the fencing-loss demotion contract')
            return record
        record['stepping'] = _cyclic_stepping(ctx, record)
        record['recovery']['restore'] = _cyclic_restore(ctx)
        record['recovery']['owner'] = _cyclic_role_view(ctx, OWNER_SEAT)
        record['recovery']['peer'] = _cyclic_role_view(ctx, PEER_SEAT)
        record['restored'] = record['recovery']['restore']
        return record
    finally:
        record['roles']['after'] = {name: _cyclic_pair_view(ctx, name)
                                    for name in launch.values()}


def _cyclic_self_check():
    """The leg's unchecked-diagnostic self-test: replay the judge over
    each planted negative — the ex-owner still role:active past the
    bound, two peers reporting active, a demotion that never journaled
    the fenced origin, a claim loss with no claimant or the wrong one, a
    field that stopped stepping, a demoted ex-owner still fencing or
    stopped exchanging, a dishonest recovery verdict, a pair that never
    reconverged, a restarted ex-owner — and require each to trip.
    Returns the negative names the judge let through."""
    def io(attempted, error=None, failures=0, link='connected'):
        return {'failed_exchanges': failures, 'failed_writes': 0,
                'consecutive_failures': failures, 'link': link,
                'attempted': attempted, 'succeeded': attempted - failures,
                'last_exchange_tick': attempted, 'error': error}

    def clean_record():
        polls = [{'owner': 'active', 'peer': 'promoting', 'active': 0},
                 {'owner': 'demoting', 'peer': 'active', 'active': 1,
                  'owner_io': io(12, 'Fenced', 1),
                  'peer_io': io(24, None, 0)},
                 {'owner': 'standby', 'peer': 'active', 'active': 1,
                  'owner_io': io(40, 'Fenced', 1),
                  'peer_io': io(60, None, 0)}]
        return {'pass': 1,
                'launch_roles': {'owner': 'active', 'peer': 'standby'},
                'field': {'device': 1, 'port': 9005,
                          'address': 'dcs-hw-qa-1-bus:9005',
                          'channels': ['do1', 'do2']},
                'settled': {'owner': {'role': 'active', 'tick': 40,
                                      'field_claim': 'held'},
                            'peer': {'role': 'standby', 'tick': 39}},
                'flap': {'owner': {'role': 'active', 'tick': 45,
                                   'field_claim': 'unclaimed'},
                         'peer': {'role': 'standby', 'tick': 44}},
                'promote': {'status': 200, 'body': {'role': 'promoting'}},
                'promoted_token': 424246,
                'promoted_tick': 44,
                'promoted_io': io(20),
                'watch': {'polls': polls, 'settled': polls[-1],
                          'read_errors': 0, 'state_error': None},
                'owner_state': {'running': True, 'exit': None,
                                'absent': False},
                'journal': {'boundaries0': [{'run': 1, 'tick': 0}],
                            'boundaries': [{'run': 1, 'tick': 0}],
                            'entries0': 0, 'events': [],
                            'fenced_io': polls[1]['owner_io'],
                            'walk': [('standby', 'promoting', 'request'),
                                     ('promoting', 'active', 'request'),
                                     ('active', 'demoting', 'fenced'),
                                     ('demoting', 'standby', 'fenced')],
                            'losses': [{'point': 3, 'claimant': 424246}]},
                'stepping': {
                    'promoted': {'role': 'active', 'claim': 'held',
                                 'sync': None, 'tick': [44, 90],
                                 'io': io(90)},
                    'demoted': {'role': 'standby',
                                'sync': 'tracking', 'io': io(70)}},
                'restored': None,
                'roles': {
                    'before': {
                        'active': {'role': 'active', 'tick': 900,
                                   'tracking': False},
                        'standby': {'role': 'standby', 'tick': 900,
                                    'tracking': True}},
                    'after': {
                        'active': {'role': 'active', 'tick': 1200,
                                   'tracking': False},
                        'standby': {'role': 'standby', 'tick': 1200,
                                    'tracking': True}}}}

    def audit(record):
        found = {}
        _judge_cyclic(
            record, lambda key, diagnostic, detail:
            found.setdefault(key, diagnostic))
        return found

    slipped = []
    if audit(clean_record()):
        slipped.append('clean-overstrict')

    def expect(name, mutate, diagnostic=DIAG_FAILED):
        record = clean_record()
        mutate(record)
        if diagnostic not in audit(record).values():
            slipped.append(name)

    # The doctored negatives the issue names — the demotion missing
    # while the ex-owner stays role:active past the bound, and two peers
    # reporting active.
    expect('ex-owner-stays-active', lambda r:
           r['watch']['polls'].__setitem__(
               -1, dict(r['watch']['polls'][-1], owner='active',
                        active=2)))
    expect('dual-active', lambda r:
           r['watch']['polls'].__setitem__(
               0, dict(r['watch']['polls'][0], owner='active',
                       peer='active', active=2)))
    expect('no-demoting-walk', lambda r:
           r['watch']['polls'][1].update(owner='standby'))
    expect('walk-not-fenced', lambda r:
           r['journal']['walk'].__setitem__(
               2, ('active', 'demoting', 'request')))
    expect('no-journaled-path', lambda r: r['journal'].update(walk=[]))
    expect('no-claim-loss', lambda r: r['journal'].update(losses=[]))
    expect('two-claim-losses', lambda r:
           r['journal']['losses'].append({'point': 3, 'claimant': 424246}))
    expect('unattributed-loss', lambda r:
           r['journal']['losses'].__setitem__(
               0, {'point': 3, 'claimant': None}))
    expect('wrong-claimant', lambda r:
           r['journal']['losses'].__setitem__(
               0, {'point': 3, 'claimant': 424247}))
    expect('field-stalled', lambda r:
           r['stepping']['promoted']['io'].update(attempted=20,
                                                  succeeded=16,
                                                  consecutive_failures=4))
    expect('owner-tick-frozen', lambda r:
           r['stepping']['promoted'].update(tick=[44, 44]))
    expect('claim-not-held', lambda r:
           r['stepping']['promoted'].update(claim='unclaimed'))
    expect('promoted-lost-field', lambda r:
           r['stepping']['promoted'].update(role='demoting'))
    expect('ex-owner-still-fenced', lambda r:
           r['stepping']['demoted']['io'].update(consecutive_failures=2,
                                                  link='disconnected'))
    expect('ex-owner-stopped', lambda r:
           r['stepping']['demoted']['io'].update(attempted=12,
                                                 succeeded=12))
    expect('dishonest-recovery', lambda r:
           r['stepping']['demoted'].update(sync='healthy'))
    expect('pair-not-restored', lambda r:
           r.update(restored='the pair never settled back'))
    expect('ex-owner-restarted', lambda r:
           r['journal'].update(boundaries=[{'run': 1, 'tick': 0},
                                           {'run': 2, 'tick': 0}]))
    expect('ex-owner-exited', lambda r: r.update(
        owner_state={'running': False, 'exit': 1, 'absent': False},
        watch=dict(r['watch'], read_errors=4)))
    expect('stage-failed', lambda r:
           r.update(stage_error='docker restart failed'), DIAG_NONDET)
    expect('promote-refused', lambda r:
           r.update(promote_refused={'status': 409,
                                     'body': {'error': 'not_converged'}}),
           DIAG_NONDET)
    expect('watch-starved', lambda r:
           r.update(watch={'polls': [], 'settled': None,
                           'read_errors': 1, 'state_error': None}),
           DIAG_NONDET)
    expect('state-unreadable', lambda r:
           r['watch'].update(state_error='docker inspect failed'),
           DIAG_NONDET)
    expect('journal-unreadable', lambda r:
           r.update(journal_error='journal line 4 does not parse'),
           DIAG_NONDET)
    expect('pair-moved', lambda r:
           r['roles']['after']['standby'].update(role='active'),
           DIAG_NONDET)
    expect('pair-wedged', lambda r:
           r['roles']['after']['active'].update(tick=900), DIAG_NONDET)
    return slipped


def scenario_sim_cyclic_fencing_loss_demote(ctx):
    """Exercise the sim-cyclic fencing-loss demotion on the rig's own
    cyclic device: with a controller pair settled on it, sever the
    control connections so the connection-bound claim is released to
    nobody, then POST /promote on the standby — the ex-owner's staged
    image must meet the fence and demote it through the named path
    (role_changed active->demoting->standby attributed to the fenced
    origin, beside exactly one field_claim_lost naming the promoted
    peer's token) inside the documented bound, never two peers
    reporting role:active, while the field resumes stepping under the
    promoted owner, the demoted peer returns to census-only exchanges
    on its recorded recovery path, and the pair reconverges to its
    launch roles. Two consecutive passes must produce identical
    digests."""
    case = Case(
        'sim-cyclic-fencing-loss-demote',
        'A sim-cyclic ex-owner fenced off the device demotes through '
        'the named fencing-loss path',
        'a controller pair on the rig\'s staged sim-cyclic device loses '
        'its connection-bound claim to a control-connection sever; the '
        'promoted standby takes the field and the ex-owner\'s refused '
        'image exchange demotes it in place — the reported role walking '
        'demoting then standby, role_changed entries attributed to the '
        'fenced origin and exactly one field_claim_lost naming the '
        'promoted peer\'s token, never two peers reporting role:active, '
        'the field resuming its exchanges under the promoted owner, the '
        'demoted peer settling on census-only exchanges, and the pair '
        'reconverging to its launch roles with identical digests across '
        'two passes')
    try:
        missing = [key for key in ('start_sim_bus_device',
                                   'restart_sim_bus_device',
                                   'stop_sim_bus_device',
                                   'start_born_controller',
                                   'stop_born_controller',
                                   'born_controller_state')
                   if ctx.get(key) is None]
        if missing:
            return case.finish('inconclusive',
                               'the run context carries no '
                               'register-protocol staging levers: '
                               + ', '.join(missing))
        absent = [seat for seat in (OWNER_SEAT, PEER_SEAT)
                  if not ctx.get(seat)]
        if absent:
            return case.finish('inconclusive', 'the run context carries '
                               'no published monitor for the leg\'s '
                               'seats: ' + ', '.join(absent))
        journals = ctx.get('journal_files') or {}
        absent = [seat for seat in (OWNER_SEAT, PEER_SEAT)
                  if not journals.get(seat)]
        if absent:
            return case.finish('inconclusive', 'the run context carries '
                               'no journal file for the leg\'s seats — '
                               'the demotion\'s durable half cannot run: '
                               + ', '.join(absent))
        tokens = ctx.get('plant_owner') or {}
        if not all(_cyclic_count(tokens.get(seat)) is not None
                   for seat in (OWNER_SEAT, PEER_SEAT)):
            return case.finish('inconclusive', 'the run pins no '
                               'owner-token for the leg\'s pair — the '
                               'claim attribution has no identity to '
                               'name')
        deadline = time.monotonic() + LAUNCH_BOUND
        owner = wait_for(lambda: _pair_active(ctx), deadline,
                         interval=SETTLE_POLL)
        if owner is None:
            reachable = any(
                _try_role(ctx, ctx[name]) is not None
                for name in ('active', 'standby') if ctx.get(name))
            return case.finish(
                'failed' if reachable else 'inconclusive',
                'no pair peer reports role=active' if reachable
                else 'the deployed pair is unreachable')
        peer = 'standby' if owner == 'active' else 'active'
        if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                    interval=SETTLE_POLL) is None:
            return case.finish('inconclusive', 'the deployed pair has no '
                               'tracking standby — the settled posture '
                               'this leg proves undisturbed was never '
                               'reached')
        case.observe('deployed field owner: ' + owner + ' (' + ctx[owner]
                     + '); tracking peer: ' + peer + ' (' + ctx[peer] + ')')
        digests = []
        for number in (1, 2):
            violations = {}

            def note(key, diagnostic, detail):
                violations.setdefault(key, (diagnostic, detail))

            record = _cyclic_pass(ctx, number, {'owner': owner,
                                                'peer': peer})
            # Each pass ends with the rig swept — both seats and the
            # device server removed, so the next pass and the legs
            # behind this one start from a free seat and a free field.
            _cyclic_teardown(ctx)
            if not record.get('inconclusive'):
                _judge_cyclic(record, note)
            digest = _cyclic_digest(record, violations)
            record['digest'] = dict(digest)
            record['violations'] = {
                key: diagnostic
                for key, (diagnostic, _) in violations.items()}
            ref = save_evidence(
                ctx['evidence_dir'],
                'sim-cyclic-fencing-loss-pass-' + str(number) + '.json',
                record)
            case.evidence('file', ref,
                          'sim-cyclic-fencing-loss pass ' + str(number)
                          + ' — the staged field and pair, the sever\'s '
                          'released claim, the promote, the demotion '
                          'watch, the durable journal, the field-stepping '
                          'evidence, the role restore, and the '
                          'normalized digest')
            if record.get('inconclusive'):
                return case.finish('inconclusive',
                                   record['inconclusive'])
            if violations:
                name = DIAG_FAILED if any(
                    diagnostic == DIAG_FAILED
                    for diagnostic, _ in violations.values()) \
                    else DIAG_NONDET
                return case.finish(
                    'failed', name + ': ' + '; '.join(
                        detail for _, detail in
                        list(violations.values())[:4]))
            case.observe('pass ' + str(number) + ': the ex-owner demoted '
                         'through the fenced path with its claim loss '
                         'attributed to the promoted token; one active '
                         'peer at every poll; the field stepped under the '
                         'promoted owner; the pair reconverged to its '
                         'launch roles')
            digests.append(digest)
        if digests[0] != digests[1]:
            return case.finish(
                'failed', DIAG_NONDET + ': the two passes\' digests '
                'diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two sim-cyclic-fencing-loss passes, identical '
                     'digests: ' + json.dumps(digests[0], sort_keys=True))
        slipped = _cyclic_self_check()
        if slipped:
            return case.finish('failed', DIAG_UNCHECKED
                               + ': planted negatives slipped the '
                               'leg’s own audits: '
                               + ', '.join(slipped))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
