"""The ownerless_remote_backoff acceptance leg — one module per leg of the
scenario schedule; see qa_lane/scenarios/__init__.py for the ordering
rule and the shared seam."""
from .common import *

# Ordering: the leg stages on the scenario seat 'driven' and the born
# legs' scratch field — it needs the foreign-model leg's seats and
# field released and must be done before the revision legs take the
# born seats over.
RUNS_AFTER = frozenset({'scenario_remote_foreign_model'})
RUNS_BEFORE = frozenset({'scenario_incompatible_revision',
                         'scenario_model_revision'})


# --------------------------------------------------------------------
# The ownerless remote-attachment reattach-backoff contract — the
# per-revision lane evidence for #1303's fix (#1324's runtime half):
# RemoteDriver::REATTACH_INTERVAL's own documented rule that a dead
# endpoint must not stall every point's access on its own connect
# timeout. The defect the contract answers: an ownerless attachment —
# a pending born-active whose conditional startup claim never produced
# a verdict — on a field that completes the TCP handshake but never
# answers (the reproduction's `docker pause`) paid every request its
# own full exchange timeout, serializing ~14 stalls into one ~72s scan
# and freezing the pending run's served tick at 0 for >60s while
# /health still reported live. On the fixed revision a failed exchange
# arms the re-attach window, so a frozen endpoint costs one timeout
# per REATTACH_INTERVAL — the paced scan keeps a bounded degraded
# cadence instead of collapsing ~(points+overhead)*timeout per scan.
#
# The leg stages the finding's recorded shape on the born legs'
# scratch field: start_born_field('serving') launches a same-model
# sim-serve plant, pause_born_field()'s `docker pause` freezes it in
# place — the listener stays connectable, no request is ever answered
# — and the labeled born-active launch (--remote, no declared pair)
# lands pending and ownerless on it. Through the pending seat's
# serving monitor the leg asserts the bounded contract across the
# declared frozen window: the tick leaves 0 and advances at least the
# floor the fix names, the liveness reads /role and /health keep
# answering inside the mirror bound, one executor-lock read
# (/checkpoint) answers inside the bound near one field timeout the
# fix names, and the served scan age stays under it — while the
# pending surface stays honest: standby, an honest sync verdict, no
# held claim on a silent field. The thaw (unpause_born_field) lets the
# first answered contact re-issue the deferred conditional grant: the
# seat walks standby → promoting → active and reports field_claim
# held. The deployed pair never enters the staging — the scratch
# field is a different container on a different claim token — and the
# leg asserts the pair's roles and scan are undisturbed before and
# after every pass. Two consecutive passes must produce identical
# outcome digests.
#
# Named diagnostics: ownerless-backoff-failed tags the contract
# clauses — a pending surface that reports a role or claim it cannot
# hold, the tick frozen at 0 while bounded degradation was asserted,
# a cadence below the declared floor, a monitor read starved or late
# past its bound, the served scan age growing past one field timeout,
# or the thawed field never landing the deferred grant — and
# ownerless-backoff-nondeterministic tags the instability the
# contract does not answer for: refused staging calls, a starved
# watch, an unread state verdict, a moved or wedged deployed pair, or
# two passes whose digests diverge. A staged revision predating the
# contract — the pending launch exiting on the frozen field, the
# pending surface never serving, the bounded-liveness report absent —
# or showing the recorded defect signature (tick frozen at 0 with the
# lock-taking read starved or the scan age unbounded) reports
# inconclusive. The unchecked-diagnostic self-check replays the judge
# over planted negatives — bounded degradation asserted while the
# tick stays frozen at 0 — and reports ownerless-backoff-unchecked
# for any that slip through.

SEAT = 'driven'          # the labeled born seat the leg launches on
FIELD_TIMEOUT = 5.0      # RemoteDriver::DEFAULT_TIMEOUT — one exchange
REATTACH_INTERVAL = 1.0  # RemoteDriver::REATTACH_INTERVAL
MIRROR_BOUND = 1.0       # the liveness-mirror bound /role and /health
                         # owe — lock-free reads, never queued behind
                         # the executor's wedged scan
LOCK_BOUND = FIELD_TIMEOUT + 4.0   # the executor-lock read's bound —
                         # near one field timeout, never the defect's
                         # ~72s serialized stall
FROZEN_WINDOW = 14.0     # the frozen observation window — spans well
                         # past two re-attach windows on the paced
                         # cadence, and the defect's ~72s scan several
                         # times under
TICK_FLOOR = 4           # the tick advance the window must show — the
                         # fix's declared floor; the defect holds 0
SCAN_AGE_BOUND = 8000    # ms — the served completed-scan-age ceiling
                         # mid-freeze: one field timeout plus slack
BACKOFF_POLL = 0.4       # the sampling cadence inside the window
BACKOFF_SETTLE = 30      # bound on the pair settling and each wait the
                         # leg drives — the pending surface's serving,
                         # the deferred grant's landing
STATE_EVERY = 8          # rounds between container-verdict probes
LOCK_EVERY = 5           # rounds between executor-lock reads
DIAG_FAILED = 'ownerless-backoff-failed'
DIAG_NONDET = 'ownerless-backoff-nondeterministic'
DIAG_UNCHECKED = 'ownerless-backoff-unchecked'

# The sync verdicts an honest pending standby may report — 'tracking'
# is only honest behind a field-owning source.
_HONEST_SYNC = ('unsynchronized', 'degraded', 'orphaned', 'diverged',
                'reinitialized')


def _backoff_sync(value):
    """The served StandbySync's variant name — 'unsynchronized' is a
    bare string, the rest are single-key objects."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and value:
        return next(iter(value))
    return None


def _backoff_probe(base, path, bound):
    """One timed GET under the leg's declared bound — the read either
    answers inside it or the client's own timeout raises, so a request
    stalled behind the wedged scan's lock hold records unanswered,
    never a late verdict. Refusals before the first served read are
    the container's boot, not the window's evidence — the judge marks
    them through 'serving'."""
    read = {'path': path, 'bound': bound, 'answered': False}
    started = time.monotonic()
    try:
        status, body = http_json('GET', base + path, timeout=bound)
    except Exception as exc:
        read['error'] = str(exc)[:150]
        read['elapsed'] = round(time.monotonic() - started, 4)
        return read
    read.update({'answered': True, 'status': status,
                 'elapsed': round(time.monotonic() - started, 4)})
    if isinstance(body, dict):
        for key in ('live', 'role', 'tick', 'sync', 'field_claim',
                    'last_scan_age_ms'):
            if key in body:
                read[key] = body[key]
    return read


def _health_shaped(read):
    """The /health answer is the bounded HealthReport the contract
    serves — live, role, tick, and a completed scan's stamp — else the
    staged run predates the liveness contract and cannot be read."""
    return read.get('live') is True \
        and isinstance(read.get('role'), str) \
        and isinstance(read.get('tick'), int) \
        and not isinstance(read.get('tick'), bool) \
        and isinstance(read.get('last_scan_age_ms'), int) \
        and not isinstance(read.get('last_scan_age_ms'), bool)


def _seat_state(ctx, seat):
    """The seat container's process verdict through the runner's
    read-only state lever."""
    state = ctx.get('born_controller_state')
    if state is None:
        return None
    return state(seat)


def _seat_report(ctx):
    """The seat's served RoleReport, or None while unreachable."""
    return _try_role(ctx, ctx[SEAT])


def _seat_journal(ctx):
    """The seat's durable journal records — its runner-owned
    --journal-file the born launch reset at launch."""
    path = (ctx.get('journal_files') or {}).get(SEAT)
    if not path or not Path(path).is_file():
        return []
    return _journal_entries(path)


def _transitioned(ctx, source, target):
    """Whether the seat's journal carries a `role_changed` from → to —
    the deferred grant's landing walks standby → promoting → active."""
    for item in _seat_journal(ctx):
        event = (item.get('entry') or {}).get('event') or {}
        change = event.get('role_changed')
        if isinstance(change, dict) \
                and change.get('from') == source \
                and change.get('to') == target:
            return True
    return False


def _backoff_pair_view(ctx, name):
    """The pass's normalized role evidence for one deployed member —
    role, scan tick, tracking posture; None when the read dropped."""
    report = _try_role(ctx, ctx[name])
    if not isinstance(report, dict):
        return None
    return {'role': report.get('role'), 'tick': report.get('tick'),
            'tracking': 'tracking' in (report.get('sync') or {})}


def _backoff_pair_held(record):
    """The deployed pair's undisturbed verdict: the owner still active
    and advancing its scan across the leg's scratch staging, the peer
    still a tracking standby — before and after alike."""
    launch = record.get('launch_roles') or {}
    roles = record.get('roles') or {}
    owner, peer = launch.get('owner'), launch.get('peer')
    before = roles.get('before') or {}
    after = roles.get('after') or {}
    for view in (before, after):
        if (view.get(owner) or {}).get('role') != 'active':
            return False
        seen = view.get(peer) or {}
        if seen.get('role') != 'standby' \
                or seen.get('tracking') is not True:
            return False
    tick0 = (before.get(owner) or {}).get('tick')
    tick1 = (after.get(owner) or {}).get('tick')
    return isinstance(tick0, int) and not isinstance(tick0, bool) \
        and isinstance(tick1, int) and not isinstance(tick1, bool) \
        and tick1 > tick0


def _frozen_window(ctx, base):
    """Collect the frozen window's evidence: the mirror reads /role and
    /health at the poll cadence, one executor-lock read /checkpoint
    every LOCK_EVERY rounds, and the container verdict every
    STATE_EVERY rounds. The window ends at FROZEN_WINDOW or at the
    seat's departure — a pending run that exits is the pre-contract
    shape, not a window to finish."""
    frozen = {'window_s': FROZEN_WINDOW, 'reads': [], 'ticks': [],
              'ages': [], 'views': [], 'states': [],
              'departed': False, 'state_error': None}
    deadline = time.monotonic() + FROZEN_WINDOW
    rounds = 0
    while time.monotonic() < deadline:
        role_read = _backoff_probe(base, '/role', MIRROR_BOUND)
        role_read['kind'] = 'mirror'
        frozen['reads'].append(role_read)
        if role_read.get('answered'):
            frozen['ticks'].append(role_read.get('tick'))
            frozen['views'].append({
                'role': role_read.get('role'),
                'sync': _backoff_sync(role_read.get('sync')),
                'field_claim': role_read.get('field_claim')})
        else:
            frozen['ticks'].append(None)
        health = _backoff_probe(base, '/health', MIRROR_BOUND)
        health['kind'] = 'mirror'
        frozen['reads'].append(health)
        frozen['ages'].append(health.get('last_scan_age_ms')
                              if health.get('answered') else None)
        if rounds % LOCK_EVERY == 0:
            locked = _backoff_probe(base, '/checkpoint', LOCK_BOUND)
            locked['kind'] = 'lock'
            frozen['reads'].append(locked)
        if rounds % STATE_EVERY == 0:
            try:
                state = _seat_state(ctx, SEAT)
            except Exception as exc:
                frozen['state_error'] = str(exc)[:150]
                state = None
            if state is not None:
                frozen['states'].append(
                    {'running': state.get('running'),
                     'exit': state.get('exit'),
                     'absent': state.get('absent')})
                if state.get('absent') \
                        or (state.get('running') is False
                            and state.get('exit') is not None):
                    frozen['departed'] = True
                    break
        rounds += 1
        time.sleep(BACKOFF_POLL)
    if not frozen['departed']:
        try:
            state = _seat_state(ctx, SEAT)
        except Exception as exc:
            frozen['state_error'] = str(exc)[:150]
            state = None
        if state is not None:
            frozen['states'].append(
                {'running': state.get('running'),
                 'exit': state.get('exit'),
                 'absent': state.get('absent')})
            if state.get('absent') \
                    or (state.get('running') is False
                        and state.get('exit') is not None):
                frozen['departed'] = True
    return frozen


def _backoff_pre_contract(frozen):
    """The staged revision's pre-contract signature, or None when the
    record is the judge's to read. Narrow by contract: only the
    recorded pre-contract shapes inconclude — the pending launch
    exiting on the frozen field, the pending surface never serving
    while the container stands, a monitor predating the bounded
    liveness contract, or the recorded defect itself — the tick
    frozen at 0 while the executor-lock read starves or the served
    scan age runs unbounded. A frozen tick with a bounded monitor
    surface is not the defect: it is a failure the judge names."""
    if frozen.get('departed'):
        return ('the labeled born-active exited rather than standing '
                'pending on the frozen field — the staged revision '
                'predates the born-active pending contract')
    reads = frozen.get('reads') or []
    answered = [read for read in reads if read.get('answered')]
    if not answered:
        return None
    health = [read for read in answered if read.get('path')
              == '/health']
    if health and not all(_health_shaped(read) for read in health):
        return ('the pending seat\'s /health answer is not the '
                'bounded HealthReport — the staged revision predates '
                'the liveness contract the leg reads through')
    views = frozen.get('views') or []
    int_ticks = [tick for tick in frozen.get('ticks') or []
                 if isinstance(tick, int) and not isinstance(tick, bool)]
    int_ages = [age for age in frozen.get('ages') or []
                if isinstance(age, int) and not isinstance(age, bool)]
    coherent = bool(views) and all(
        view.get('role') == 'standby'
        and view.get('field_claim') != 'held'
        and view.get('sync') in _HONEST_SYNC
        for view in views)
    starved = any(read.get('kind') == 'lock'
                  and (not read.get('answered')
                       or (read.get('elapsed') or 0)
                       > read.get('bound', LOCK_BOUND))
                  for read in reads)
    unbounded = any(age > SCAN_AGE_BOUND for age in int_ages)
    if coherent and int_ticks and not any(int_ticks) \
            and (starved or unbounded):
        return ('the pending ownerless attachment froze at tick 0 '
                'across the whole frozen window while its executor '
                'stall starved the lock-taking read — the recorded '
                'defect signature: the staged revision predates the '
                'ownerless reattach-backoff contract')
    return None


def _judge_backoff(record, note):
    """Replay one pass's record — runnable against planted negatives in
    the self-check. `note(key, diagnostic, detail)` records each clause
    the record violates: DIAG_FAILED tags the contract clauses and
    DIAG_NONDET the instability the contract does not answer for."""
    def failed(key, detail):
        note(key, DIAG_FAILED, detail)

    def nondet(key, detail):
        note(key, DIAG_NONDET, detail)

    if record.get('stage_error') is not None:
        nondet('stage', 'the born-field or launch staging never '
               'completed: ' + str(record['stage_error']))
        return
    frozen = record.get('frozen') or {}
    reads = frozen.get('reads') or []
    views = frozen.get('views') or []
    int_ticks = [tick for tick in frozen.get('ticks') or []
                 if isinstance(tick, int) and not isinstance(tick, bool)]
    int_ages = [age for age in frozen.get('ages') or []
                if isinstance(age, int) and not isinstance(age, bool)]

    if frozen.get('state_error') is not None:
        nondet('state-read', 'the seat\'s container verdict never '
               'read: ' + str(frozen['state_error']))
    if not any(read.get('answered') for read in reads):
        nondet('watch-starved', 'the pending seat\'s monitor answered '
               'no read inside the frozen window')
        return

    # The pending ownerless surface: standby, an honest sync verdict,
    # and never a held claim reported against a silent field.
    if not views:
        nondet('surface-unread', 'the answered reads carried no role '
               'views — the pending surface is unreadable')
    else:
        if any(view.get('role') != 'standby' for view in views):
            failed('pending-role', 'the pending ownerless seat '
                   'reported a role it cannot hold: '
                   + json.dumps(views)[:300])
        if any(view.get('field_claim') == 'held' for view in views):
            failed('pending-claimed', 'the ownerless attachment '
                   'reported field_claim held on a field that never '
                   'answered a claim: ' + json.dumps(views)[:300])
        if any(view.get('sync') not in _HONEST_SYNC for view in views):
            failed('pending-sync', 'the pending seat reported a sync '
                   'verdict it cannot hold: '
                   + json.dumps(views)[:300])

    # The bounded degraded cadence: the tick leaves 0 and advances at
    # least the declared floor across the window — never the defect's
    # frozen 0, never a per-request-timeout collapse.
    if not int_ticks:
        nondet('ticks-unread', 'the answered /role reads carried no '
               'integer tick')
    elif not any(int_ticks):
        failed('tick-frozen', 'the pending seat\'s tick stayed at 0 '
               'across the frozen window while its monitor asserted '
               'bounded degradation — the scan is stalled past every '
               'declared bound')
    elif int_ticks[-1] - int_ticks[0] < TICK_FLOOR:
        failed('cadence', 'the pending seat\'s tick advanced '
               + str(int_ticks[-1] - int_ticks[0]) + ' across the '
               + format(frozen.get('window_s', FROZEN_WINDOW), '.0f')
               + 's frozen window — below the bounded degraded '
               'floor of ' + str(TICK_FLOOR))

    # The monitor bounds: every read that reached a serving monitor
    # answered inside its declared bound — the defect's serialized
    # stalls starve the lock-taking read past it.
    serving = False
    for read in reads:
        if read.get('answered'):
            serving = True
        if not serving:
            continue
        bound = read.get('bound', MIRROR_BOUND)
        label = str(read.get('kind', 'mirror')) + ' GET ' \
            + str(read.get('path'))
        if not read.get('answered'):
            failed('read-bound', label + ' never answered inside its '
                   + format(bound, '.1f') + 's bound on the serving '
                   'monitor' + (' — ' + str(read.get('error'))
                                if read.get('error') else ''))
        elif read.get('status') != 200:
            failed('read-bound', label + ' answered status '
                   + str(read.get('status')) + ' under the freeze')
        elif (read.get('elapsed') or 0) > bound:
            failed('read-bound', label + ' answered in '
                   + format(read['elapsed'], '.2f') + 's past the '
                   + format(bound, '.1f') + 's bound — the wedged '
                   'scan\'s lock hold leaked past the window\'s '
                   'single field timeout')
    if int_ages and max(int_ages) > SCAN_AGE_BOUND:
        failed('scan-age', 'the served last_scan_age_ms reached '
               + str(max(int_ages)) + 'ms past the '
               + str(SCAN_AGE_BOUND) + 'ms bound — the paced scan '
               'is not completing near one field timeout')

    # The thaw: the first answered contact re-issues the deferred
    # conditional grant — the pending seat walks standby → promoting →
    # active and reports the claim held.
    settled = (record.get('recovery') or {}).get('settled')
    if not isinstance(settled, dict) \
            or settled.get('role') != 'active' \
            or settled.get('field_claim') != 'held':
        failed('deferred-claim', 'the thawed field never landed the '
               'deferred conditional grant — the pending seat never '
               'reached active with field_claim held: '
               + json.dumps(settled)[:300])
    elif not (record.get('recovery') or {}).get('grant_journaled'):
        failed('grant-journal', 'the deferred grant\'s landing never '
               'journaled standby → promoting → active')

    # The deployed pair: undisturbed before and after the leg's
    # scratch staging.
    if not _backoff_pair_held(record):
        nondet('pair-disturbed', 'the deployed pair moved or wedged '
               'across the scratch staging: '
               + json.dumps(record.get('roles'), sort_keys=True)[:300])


def _backoff_digest(record, violations):
    """The pass's normalized verdict record — identical digests across
    two consecutive passes is the determinism contract."""
    def clean(*keys):
        return not any(key in violations for key in keys)
    return {
        'pending': 'held'
                   if clean('pending-role', 'pending-claimed',
                            'pending-sync', 'surface-unread',
                            'watch-starved')
                   else 'defect',
        'cadence': 'bounded'
                   if clean('tick-frozen', 'cadence', 'scan-age',
                            'ticks-unread')
                   else 'defect',
        'monitor': 'bounded' if clean('read-bound') else 'defect',
        'recovery': 'claimed'
                    if clean('deferred-claim', 'grant-journal')
                    else 'defect',
        'pair': 'undisturbed' if clean('pair-disturbed')
                else 'disturbed'}


def _backoff_teardown(ctx):
    """Best-effort teardown: the born seat and the scratch field — a
    clean pass leaves nothing standing, and an aborted pass gets the
    same sweep so the legs behind this one see a free seat and a free
    field name, and the launch configuration is restored as found."""
    lever = ctx.get('stop_born_controller')
    if lever is not None:
        try:
            lever(SEAT)
        except Exception:
            pass
    try:
        if ctx.get('unpause_born_field') is not None:
            ctx['unpause_born_field']()
    except Exception:
        pass
    try:
        if ctx.get('stop_born_field') is not None:
            ctx['stop_born_field']()
    except Exception:
        pass


def _backoff_pass(ctx, number, launch):
    """One pass over the ownerless-attachment freeze: stage the scratch
    field, freeze it under `docker pause`, launch the labeled
    born-active --remote against it, watch the pending seat's monitor
    through the frozen window, thaw the field, and read the deferred
    grant's landing — the deployed pair framed before and after."""
    record = {'pass': number, 'launch_roles': dict(launch),
              'roles': {}, 'frozen': {}, 'recovery': {}}
    owner, peer = launch['owner'], launch['peer']
    record['roles']['before'] = {
        name: _backoff_pair_view(ctx, name) for name in (owner, peer)}
    try:
        try:
            field = ctx['start_born_field']('serving')
        except Exception as exc:
            record['stage_error'] = str(exc)[:300]
            return record
        record['remote'] = field.get('remote')
        try:
            ctx['pause_born_field']()
        except Exception as exc:
            record['stage_error'] = 'the field freeze never landed: ' \
                + str(exc)[:250]
            return record
        try:
            record['launch'] = ctx['start_born_controller'](
                SEAT, record['remote'])
        except Exception as exc:
            record['stage_error'] = 'the labeled born-active launch ' \
                'never ran: ' + str(exc)[:250]
            return record
        # The monitor's serving is the window's precondition — the
        # pending surface must exist before its cadence can be read.
        # A launch that never serves while its container stands is
        # the nondeterministic shape; one that exited is the
        # pre-contract shape.
        served = wait_for(lambda: _seat_report(ctx) or None,
                          time.monotonic() + BACKOFF_SETTLE,
                          interval=BACKOFF_POLL)
        if served is None:
            try:
                state = _seat_state(ctx, SEAT)
            except Exception:
                state = None
            if state is None or state.get('absent') \
                    or (state.get('running') is False
                        and state.get('exit') is not None):
                record['inconclusive'] = (
                    'the labeled born-active never served its '
                    'monitor on the frozen field — the staged '
                    'revision predates the born-active pending '
                    'contract')
            else:
                record['frozen'] = {'window_s': 0, 'reads': [],
                                    'ticks': [], 'ages': [],
                                    'views': [], 'states': [],
                                    'departed': False,
                                    'state_error': None}
            return record
        record['frozen'] = _frozen_window(ctx, ctx[SEAT])
        pre_contract = _backoff_pre_contract(record['frozen'])
        if pre_contract is not None:
            record['inconclusive'] = pre_contract
            return record
        try:
            ctx['unpause_born_field']()
            record['unpaused'] = True
        except Exception as exc:
            record['unpaused'] = False
            record['inconclusive'] = 'the field unpause action never ' \
                'landed: ' + str(exc)[:200]
            return record
        record['recovery']['settled'] = wait_for(
            lambda: (lambda report: report
                     if report is not None
                     and report.get('role') == 'active'
                     and report.get('field_claim') == 'held'
                     else None)(_seat_report(ctx)),
            time.monotonic() + BACKOFF_SETTLE, interval=BACKOFF_POLL)
        record['recovery']['grant_journaled'] = (
            _transitioned(ctx, 'standby', 'promoting')
            and _transitioned(ctx, 'promoting', 'active'))
        return record
    finally:
        record['roles']['after'] = {
            name: _backoff_pair_view(ctx, name) for name in (owner, peer)}


def _backoff_self_check():
    """The leg's unchecked-diagnostic self-test: replay the judge over
    each planted negative — bounded degradation asserted while the
    pending seat's tick stays frozen at 0, a cadence below the floor, a
    pending surface reporting a role or claim it cannot hold, a starved
    or late monitor read, an unbounded scan age, the deferred grant
    never landing or never journaling — and require each to trip.
    Returns the negative names the judge let through."""
    def role_read(tick):
        return {'path': '/role', 'kind': 'mirror', 'bound': 1.0,
                'answered': True, 'status': 200, 'elapsed': 0.002,
                'role': 'standby', 'tick': tick,
                'sync': 'unsynchronized', 'field_claim': None}

    def health_read(age):
        return {'path': '/health', 'kind': 'mirror', 'bound': 1.0,
                'answered': True, 'status': 200, 'elapsed': 0.002,
                'live': True, 'role': 'standby', 'tick': 1,
                'last_scan_age_ms': age}

    def lock_read():
        return {'path': '/checkpoint', 'kind': 'lock', 'bound': 9.0,
                'answered': True, 'status': 200, 'elapsed': 0.05}

    def clean_record():
        ticks = [1, 2, 3, 5]
        return {'pass': 1,
                'launch_roles': {'owner': 'active', 'peer': 'standby'},
                'remote': 'dcs-hw-qa-1-born-plant:9003',
                'launch': {'seat': SEAT},
                'unpaused': True,
                'frozen': {'window_s': 14.0, 'departed': False,
                           'state_error': None,
                           'reads': [role_read(tick)
                                     for tick in ticks]
                           + [health_read(age)
                              for age in (110, 120, 130, 140)]
                           + [lock_read()],
                           'ticks': list(ticks),
                           'ages': [110, 120, 130, 140],
                           'views': [{'role': 'standby',
                                      'sync': 'unsynchronized',
                                      'field_claim': None}
                                     for _ in ticks],
                           'states': [{'running': True, 'exit': None,
                                       'absent': False}]},
                'recovery': {'settled': {'role': 'active',
                                         'field_claim': 'held',
                                         'tick': 9},
                             'grant_journaled': True},
                'roles': {
                    'before': {
                        'active': {'role': 'active', 'tick': 10,
                                   'tracking': False},
                        'standby': {'role': 'standby', 'tick': 10,
                                    'tracking': True}},
                    'after': {
                        'active': {'role': 'active', 'tick': 12,
                                   'tracking': False},
                        'standby': {'role': 'standby', 'tick': 12,
                                    'tracking': True}}}}

    def audit(record):
        found = {}
        _judge_backoff(
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

    # The doctored negative the issue names — bounded degradation
    # asserted while the pending seat's tick stays frozen at 0.
    expect('bounded-asserted-but-frozen', lambda r:
           r['frozen'].update(ticks=[0, 0, 0, 0]))
    expect('cadence-below-floor', lambda r:
           r['frozen'].update(ticks=[1, 1, 2, 2]))
    expect('pending-reports-active', lambda r:
           r['frozen']['views'][0].update(role='active'))
    expect('pending-claims-held', lambda r:
           r['frozen']['views'][1].update(field_claim='held'))
    expect('pending-dishonest-sync', lambda r:
           r['frozen']['views'][2].update(sync='tracking'))
    expect('mirror-read-starved', lambda r:
           r['frozen']['reads'].append(
               {'path': '/role', 'kind': 'mirror', 'bound': 1.0,
                'answered': False, 'elapsed': 1.0,
                'error': 'timed out'}))
    expect('lock-read-starved', lambda r:
           r['frozen']['reads'].append(
               {'path': '/checkpoint', 'kind': 'lock', 'bound': 9.0,
                'answered': False, 'elapsed': 9.0,
                'error': 'timed out'}))
    expect('scan-age-unbounded', lambda r:
           r['frozen'].update(ages=[110, 120, 130, 60000]))
    expect('grant-never-lands', lambda r:
           r['recovery'].update(settled=None))
    expect('grant-unjournaled', lambda r:
           r['recovery'].update(grant_journaled=False))
    expect('grant-settles-unclaimed', lambda r:
           r['recovery'].update(
               settled={'role': 'active', 'field_claim': None}))
    expect('stage-refused', lambda r:
           r.update(stage_error='docker run failed'), DIAG_NONDET)
    expect('watch-starved', lambda r:
           r['frozen'].update(reads=[], ticks=[], ages=[], views=[]),
           DIAG_NONDET)
    expect('ticks-unread', lambda r:
           r['frozen'].update(ticks=[None, None, None, None]),
           DIAG_NONDET)
    expect('state-read-failed', lambda r:
           r['frozen'].update(state_error='docker inspect failed'),
           DIAG_NONDET)
    expect('pair-owner-moved', lambda r:
           r['roles']['after']['active'].update(role='standby'),
           DIAG_NONDET)
    expect('pair-peer-lost-tracking', lambda r:
           r['roles']['after']['standby'].update(tracking=False),
           DIAG_NONDET)
    expect('pair-scan-wedged', lambda r:
           r['roles']['after']['active'].update(tick=10),
           DIAG_NONDET)
    return slipped


def scenario_ownerless_remote_backoff(ctx):
    """Exercise the ownerless remote-attachment reattach backoff: a
    labeled born-active launched --remote against a `docker pause`-
    frozen scratch field stands pending and ownerless, and through its
    serving monitor the frozen window must show a bounded degraded
    cadence — the tick leaving 0 and advancing past the declared
    floor, /role and /health answering inside the mirror bound, the
    executor-lock read and the served scan age inside one field
    timeout — while the pending surface stays honest; the thaw then
    lands the deferred conditional grant, and the deployed pair is
    undisturbed and restored. Two consecutive passes must produce
    identical digests."""
    case = Case(
        'ownerless-remote-backoff',
        'An ownerless remote attachment keeps a bounded degraded '
        'cadence under a frozen field',
        'a labeled born-active --remote launch against a plant '
        'frozen by the lane\'s container-pause lever — connectable '
        'but never answering — stands pending and ownerless while its '
        'served tick advances at a bounded degraded rate across the '
        'frozen window (never frozen at 0, never paying a full '
        'exchange timeout per point per cycle), /role and /health '
        'keep answering inside their bound, the executor-lock read '
        'answers inside one field timeout, the thawed field lands '
        'the deferred conditional claim, the deployed pair is '
        'undisturbed, and two passes produce identical digests')
    try:
        missing = [key for key in ('start_born_field',
                                   'pause_born_field',
                                   'unpause_born_field',
                                   'stop_born_field',
                                   'start_born_controller',
                                   'stop_born_controller',
                                   'born_controller_state')
                   if ctx.get(key) is None]
        if missing:
            return case.finish('inconclusive',
                               'the run context carries no '
                               'born-active staging levers: '
                               + ', '.join(missing))
        if not ctx.get(SEAT):
            return case.finish('inconclusive', 'the run context '
                               'carries no published monitor for '
                               'the leg\'s born seat')
        if not (ctx.get('journal_files') or {}).get(SEAT):
            return case.finish('inconclusive', 'the run context '
                               'carries no journal file for the '
                               'leg\'s born seat — the deferred '
                               'grant\'s durable half cannot run')
        deadline = time.monotonic() + BACKOFF_SETTLE
        owner = wait_for(lambda: _pair_active(ctx), deadline,
                         interval=BACKOFF_POLL)
        if owner is None:
            reports = {name: _try_role(ctx, ctx[name])
                       for name in ('active', 'standby')
                       if ctx.get(name)}
            if not reports or all(report is None
                                  for report in reports.values()):
                return case.finish(
                    'inconclusive', 'the deployed pair is '
                    'unreachable — monitor endpoints '
                    + str(ctx.get('active')) + ' and '
                    + str(ctx.get('standby')))
            return case.finish('failed', 'no peer reports '
                               'role=active')
        peer = 'standby' if owner == 'active' else 'active'
        if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                    interval=BACKOFF_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — the settled '
                               'posture the leg proves undisturbed '
                               'was never reached')
        launch = {'owner': owner, 'peer': peer}
        case.observe('field owner: ' + owner + ' (' + ctx[owner]
                     + '); tracking peer: ' + peer + ' ('
                     + ctx[peer] + ')')
        digests = []
        for number in (1, 2):
            violations = {}

            def note(key, diagnostic, detail):
                violations.setdefault(key, (diagnostic, detail))

            try:
                record = _backoff_pass(ctx, number, launch)
            finally:
                # Each pass ends with the rig swept — the seat and
                # the scratch field removed so the next pass and the
                # legs behind this one start cold, and the launch
                # configuration is restored as found.
                _backoff_teardown(ctx)
            if not record.get('inconclusive'):
                _judge_backoff(record, note)
            digest = _backoff_digest(record, violations)
            record['digest'] = dict(digest)
            record['violations'] = {
                key: diagnostic
                for key, (diagnostic, _) in violations.items()}
            ref = save_evidence(
                ctx['evidence_dir'],
                'ownerless-remote-backoff-pass-' + str(number)
                + '.json', record)
            case.evidence('file', ref,
                          'ownerless-remote-backoff pass '
                          + str(number) + ' — the frozen window\'s '
                          'timed reads, ticks, and scan ages, the '
                          'pending surface, the deferred grant\'s '
                          'recovery, the pair framing, and the '
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
            digests.append(digest)
        if digests[0] != digests[1]:
            return case.finish(
                'failed', DIAG_NONDET + ': the two passes\' digests '
                'diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two ownerless-backoff passes, identical '
                     'digests: '
                     + json.dumps(digests[0], sort_keys=True))
        slipped = _backoff_self_check()
        if slipped:
            return case.finish('failed', DIAG_UNCHECKED
                               + ': planted negatives slipped the '
                               'leg’s own audits: '
                               + ', '.join(slipped))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
