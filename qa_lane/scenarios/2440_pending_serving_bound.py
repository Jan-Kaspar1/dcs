"""The pending_serving_bound acceptance leg — one module per leg of the
scenario schedule; see qa_lane/scenarios/__init__.py for the ordering
rule and the shared seam."""
from .common import *

# Ordering: the leg stages on the scenario seat 'driven' and the born
# legs' scratch field — it needs the ownerless-backoff leg's seat and
# field released and must be done before the revision legs take the
# born seats over.
RUNS_AFTER = frozenset({'scenario_ownerless_remote_backoff'})
RUNS_BEFORE = frozenset({'scenario_incompatible_revision',
                         'scenario_model_revision'})


# --------------------------------------------------------------------
# The pending born-active's bounded *serving* contract — the
# per-revision lane evidence for #1324's fix (WW-LCM-001 continuity;
# decision 103's pending state must be *served*, and the monitor's lane
# design intends a wedged scan to leave serving responsive for
# incident-time control). The defect the contract answers:
# Monitor::paced_scan held the shared executor mutex for the whole scan
# while each RemoteDriver channel independently burned the full 5s
# exchange timeout per ~1s re-attach window — ~14 serialized timeouts,
# ~72s per scan against a configured --scan-ms 100 — so every
# lock-taking endpoint queued tens of seconds behind the wedged scan:
# /checkpoint pulls (failover misses accumulating on a checkpoint
# consumer), /promote, /demote, and /command, while the lock-free /role
# and /health mirrors stayed instant and the served tick advanced ~1
# per ~70s — a ~700x cadence collapse. On the fixed revision a failed
# exchange arms the re-attach window, so the pending run's scan stalls
# once near one field timeout per window and every lock-taking caller
# bounds near it.
#
# The leg stages the finding's recorded shape on the born legs' scratch
# field: start_born_field('serving') launches a same-model sim-serve
# plant, pause_born_field()'s `docker pause` freezes it in place — the
# listener accepts connections but never answers, the accept-but-never-
# answer shape the finding records (a connection-refused field fails
# instantly and is not the defect) — and the labeled born-active launch
# (--remote, no declared pair) stands pending on it. Through the
# pending seat's serving monitor the leg asserts the bounded contract
# across the declared frozen window: every lock-taking endpoint — GET
# /checkpoint, POST /promote answering the named not_converged refusal,
# POST /demote answering not_active, and POST /command answering the
# receipted not_active rejection — answers inside the declared bound
# near one field timeout, never queuing tens of seconds behind the
# wedged scan; the lock-free published mirror /role and /health keeps
# answering instantly; the pending seat's tick advances at the bounded
# degraded rate the fix names and the served last_scan_age_ms stays
# bounded; and the pending surface stays honest — standby under an
# honest sync verdict with no held claim, the checkpoint's
# source_owns_field stamp false. The contrast control then re-stages
# the seat on the 'silent' field — the address resolvable but nothing
# listening — where the same serving set must answer inside the instant
# bound: a connection-refused field fails fast and never queues the
# scan, the signature that distinguishes the bounded stall the fix
# names from an unreachable transport. The thaw (unpause_born_field)
# lets the deferred claim resolve per the standing born-active contract
# — the seat walks standby → promoting → active and reports the claim
# held. The deployed pair never enters the staging and the leg asserts
# its roles and scan undisturbed before and after every pass; each pass
# tears the seat and scratch field down, restoring the launch
# configuration as found. Two consecutive passes must produce identical
# outcome digests.
#
# Named diagnostics: pending-serving-bound-failed tags the contract
# clauses — a pending surface reporting a role, claim, or sync verdict
# it cannot hold, a served checkpoint stamping source_owns_field, a
# lock-taking endpoint never answering or answering past the declared
# bound, a refusal answering other than its named verdict, the tick
# frozen at 0 or under the declared floor, an unbounded scan age, the
# thawed field never landing the deferred grant, or the refused-field
# contrast stalling — and pending-serving-bound-nondeterministic tags
# the instability the contract does not answer for: refused staging
# calls, a starved watch, an endpoint the window never reached, an
# unread state verdict, a moved or wedged deployed pair, or two passes
# whose digests diverge. A staged revision predating the contract — the
# pending launch exiting (or its container standing absent) on the frozen
# or refused field, a /health answer set that never carries the bounded
# liveness report at all (a run that has not completed a scan yet serves
# the stamp null, which is the documented shape before the first
# completion, not a pre-contract revision) — or showing the recorded
# defect signature (the tick frozen at 0 while the lock-taking reads
# starve or the served scan age runs unbounded) reports inconclusive. A
# container that stands while its monitor never serves is not a
# pre-contract signature — the pending state has always published its
# mirror — so that instability stays nondeterministic, named as the
# starved watch it is. The unchecked-diagnostic self-check replays the
# judge over planted negatives — bounded serving asserted while a
# lock-taking endpoint queues ~N_channels timeouts behind the wedged
# pending scan among them — and reports pending-serving-bound-unchecked
# for any that slip through.

SEAT = 'driven'          # the labeled born seat the leg launches on
FIELD_TIMEOUT = 5.0      # RemoteDriver::DEFAULT_TIMEOUT — one exchange
REATTACH_INTERVAL = 1.0  # RemoteDriver::REATTACH_INTERVAL
MIRROR_BOUND = 1.0       # the liveness-mirror bound /role and /health
                         # owe — lock-free reads, never queued behind
                         # the executor's wedged scan
LOCK_BOUND = FIELD_TIMEOUT + 4.0   # each lock-taking endpoint's bound —
                         # near one field timeout, never the defect's
                         # ~72s serialized stall
REFUSED_BOUND = 2.0      # the refused-field contrast's bound — the
                         # declared per-request bound LATENCY_BOUND
                         # names; a refused connect fails instantly, so
                         # nothing queues
FROZEN_WINDOW = 20.0     # the frozen observation window — spans several
                         # bounded scans on the fixed revision while
                         # staying well under the defect's ~72s
                         # serialized scan
TICK_FLOOR = 4           # the tick advance the window must show — the
                         # fix's declared floor; the defect holds 0
SCAN_AGE_BOUND = 8000    # ms — the served completed-scan-age ceiling
                         # mid-freeze: one field timeout plus slack
SERVING_POLL = 0.4       # the sampling cadence inside the window
SERVING_SETTLE = 30      # bound on the pair settling and each wait the
                         # leg drives — the pending surface's serving,
                         # the deferred grant's landing
STATE_EVERY = 8          # rounds between container-verdict probes
COMMAND_POINT = 1000     # pump_station's declared writable bool
                         # in-point — the receipted refusal's target
DIAG_FAILED = 'pending-serving-bound-failed'
DIAG_NONDET = 'pending-serving-bound-nondeterministic'
DIAG_UNCHECKED = 'pending-serving-bound-unchecked'

# The sync verdicts an honest pending standby may report — 'tracking'
# is only honest behind a field-owning source.
_HONEST_SYNC = ('unsynchronized', 'degraded', 'orphaned', 'diverged',
                'reinitialized')

# The lock-free published-mirror reads — they must answer instantly
# even while the executor lock is held by a stalled scan.
MIRROR_PATHS = ('/role', '/health')

# The lock-taking serving set the pending contract owes bounded answers
# on — (method, path, expected status, named verdict): the checkpoint
# pull a tracking consumer's failover heartbeat counts on, the two
# switchover actuations an incident-time operator would issue, and the
# receipted command path. Verdict names: the checkpoint's document
# itself, promote's named not_converged refusal, demote's not_active,
# and the command's receipted not_active rejection.
LOCK_CALLS = (
    ('GET', '/checkpoint', 200, 'document'),
    ('POST', '/promote', 409, 'not_converged'),
    ('POST', '/demote', 409, 'not_active'),
    ('POST', '/command', 200, 'rejected:not_active'),
)

# The receipted refusal's submission — a declared writable point under
# the leg's actor.
COMMAND_BODY = {'command': {'write_value': {'point': COMMAND_POINT,
                                            'kind': 'bool',
                                            'value': {'bool': True}}},
                'actor': 'qa-pending-serving'}


def _pend_sync(value):
    """The served StandbySync's variant name — 'unsynchronized' is a
    bare string, the rest are single-key objects."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and value:
        return next(iter(value))
    return None


def _pend_call(method, url, body=None, timeout=10):
    """(status, decoded body) — http_json with the refusal statuses
    decoded rather than raised: the pending surface's named refusals —
    409 not_converged, 409 not_active — are the leg's data."""
    try:
        return http_json(method, url, body, timeout=timeout)
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b'null')
        except ValueError:
            payload = None
        finally:
            exc.close()
        return exc.code, payload


def _pend_verdict(path, body):
    """The named verdict an answered call carries — the checkpoint's
    document, a switchover's named SwitchError, or the receipted
    command's settled outcome."""
    if path == '/checkpoint':
        return 'document' if isinstance(body, dict) else 'malformed'
    if path == '/command':
        return _outcome_key(body)
    if isinstance(body, str):
        return body
    if isinstance(body, dict) and len(body) == 1:
        return next(iter(body))
    return 'malformed'


def _pend_probe(base, method, path, bound, body=None):
    """One timed request under the leg's declared bound — the call
    either answers inside it or the client's own timeout raises, so a
    request stalled behind the wedged scan's lock hold records
    unanswered, never a late verdict. Refusals before the first served
    read are the container's boot, not the window's evidence — the
    judge marks them through 'serving'."""
    read = {'method': method, 'path': path, 'bound': bound,
            'answered': False}
    started = time.monotonic()
    try:
        status, answer = _pend_call(method, base + path, body,
                                    timeout=bound)
    except Exception as exc:
        read['error'] = str(exc)[:150]
        read['elapsed'] = round(time.monotonic() - started, 4)
        return read
    read.update({'answered': True, 'status': status,
                 'elapsed': round(time.monotonic() - started, 4),
                 'verdict': _pend_verdict(path, answer)})
    if isinstance(answer, dict):
        for key in ('live', 'role', 'tick', 'sync', 'field_claim',
                    'last_scan_age_ms', 'source_owns_field'):
            if key in answer:
                read[key] = answer[key]
    return read


def _pend_health(read):
    """The /health answer is the bounded HealthReport the contract
    serves — live, role, tick, and the completed-scan stamp field. A
    run that has not completed a scan yet serves that stamp null, the
    documented shape before the first completion, so only a run that
    never carries the field at all is read as a pre-contract
    revision."""
    if 'last_scan_age_ms' not in read:
        return False
    stamp = read['last_scan_age_ms']
    return read.get('live') is True \
        and isinstance(read.get('role'), str) \
        and isinstance(read.get('tick'), int) \
        and not isinstance(read.get('tick'), bool) \
        and (stamp is None
             or (isinstance(stamp, int)
                 and not isinstance(stamp, bool)))


def _pend_state(ctx, seat):
    """The seat container's process verdict through the runner's
    read-only state lever."""
    state = ctx.get('born_controller_state')
    if state is None:
        return None
    return state(seat)


def _pend_report(ctx):
    """The seat's served RoleReport, or None while unreachable."""
    return _try_role(ctx, ctx[SEAT])


def _pend_journal(ctx):
    """The seat's durable journal records — its runner-owned
    --journal-file the born launch reset at launch."""
    path = (ctx.get('journal_files') or {}).get(SEAT)
    if not path or not Path(path).is_file():
        return []
    return _journal_entries(path)


def _pend_transition(ctx, source, target):
    """Whether the seat's journal carries a `role_changed` from → to —
    the deferred grant's landing walks standby → promoting → active."""
    for item in _pend_journal(ctx):
        event = (item.get('entry') or {}).get('event') or {}
        change = event.get('role_changed')
        if isinstance(change, dict) \
                and change.get('from') == source \
                and change.get('to') == target:
            return True
    return False


def _pend_pair_view(ctx, name):
    """The pass's normalized role evidence for one deployed member —
    role, scan tick, tracking posture; None when the read dropped."""
    report = _try_role(ctx, ctx[name])
    if not isinstance(report, dict):
        return None
    return {'role': report.get('role'), 'tick': report.get('tick'),
            'tracking': 'tracking' in (report.get('sync') or {})}


def _pend_pair_held(record):
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


def _pend_window(ctx, base):
    """Collect the frozen window's evidence: the mirror reads /role and
    /health at the poll cadence, one rotating lock-taking call per
    round — /checkpoint, /promote, /demote, /command cycling — and the
    container verdict every STATE_EVERY rounds. The window runs at
    least FROZEN_WINDOW; past it the loop continues only until every
    lock call has been reached once — each probe is self-bounding, so
    the tail stays bounded — giving every endpoint its say on the pass.
    The window ends early at the seat's departure — a pending run that
    exits is the pre-contract shape, not a window to finish."""
    every = {call[1] for call in LOCK_CALLS}
    frozen = {'window_s': FROZEN_WINDOW, 'reads': [], 'ticks': [],
              'ages': [], 'views': [], 'states': [],
              'departed': False, 'state_error': None}
    deadline = time.monotonic() + FROZEN_WINDOW
    hard = deadline + SERVING_SETTLE
    rounds = 0
    probed = set()
    while time.monotonic() < deadline \
            or (time.monotonic() < hard and probed != every):
        for path in MIRROR_PATHS:
            read = _pend_probe(base, 'GET', path, MIRROR_BOUND)
            read['kind'] = 'mirror'
            frozen['reads'].append(read)
            if path == '/role':
                frozen['ticks'].append(read.get('tick')
                                       if read.get('answered')
                                       else None)
                if read.get('answered'):
                    frozen['views'].append({
                        'role': read.get('role'),
                        'sync': _pend_sync(read.get('sync')),
                        'field_claim': read.get('field_claim')})
            else:
                frozen['ages'].append(
                    read.get('last_scan_age_ms')
                    if read.get('answered') else None)
        method, path, status, verdict = \
            LOCK_CALLS[rounds % len(LOCK_CALLS)]
        read = _pend_probe(
            base, method, path, LOCK_BOUND,
            body=COMMAND_BODY if path == '/command' else None)
        read.update({'kind': 'lock', 'want_status': status,
                     'want_verdict': verdict})
        frozen['reads'].append(read)
        probed.add(path)
        if rounds % STATE_EVERY == 0:
            try:
                state = _pend_state(ctx, SEAT)
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
        time.sleep(SERVING_POLL)
    if not frozen['departed']:
        try:
            state = _pend_state(ctx, SEAT)
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


def _pend_pre_contract(frozen):
    """The staged revision's pre-contract signature, or None when the
    record is the judge's to read. Narrow by contract: only the
    recorded pre-contract shapes inconclude — the pending launch
    exiting on the frozen field, a monitor whose /health answers never
    carried the bounded liveness report, or the recorded defect itself
    — the tick frozen at 0 while the lock-taking reads starve or the
    served scan age runs unbounded. A frozen tick with a bounded monitor
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
    if health and not any(_pend_health(read) for read in health):
        return ('the pending seat\'s /health answers never carried the '
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
        return ('the pending attachment froze at tick 0 across the '
                'frozen window while the executor\'s serialized field '
                'stalls starved the lock-taking serving set — the '
                'recorded defect signature: the staged revision '
                'predates the pending bounded-serving contract')
    return None


def _judge_serving(record, note):
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

    # The pending surface: standby, an honest sync verdict, and never a
    # held claim reported against a silent field — on /role or on the
    # checkpoint's source_owns_field stamp alike.
    if not views:
        nondet('surface-unread', 'the answered reads carried no role '
               'views — the pending surface is unreadable')
    else:
        if any(view.get('role') != 'standby' for view in views):
            failed('pending-role', 'the pending seat reported a role '
                   'it cannot hold: ' + json.dumps(views)[:300])
        if any(view.get('field_claim') == 'held' for view in views):
            failed('pending-claimed', 'the pending seat reported '
                   'field_claim held on a field that never answered a '
                   'claim: ' + json.dumps(views)[:300])
        if any(view.get('sync') not in _HONEST_SYNC for view in views):
            failed('pending-sync', 'the pending seat reported a sync '
                   'verdict it cannot hold: '
                   + json.dumps(views)[:300])
    if any(read.get('source_owns_field') is True for read in reads):
        failed('checkpoint-claimed', 'the pending seat\'s served '
               'checkpoint stamped source_owns_field true — a pending '
               'run serves state but claims nothing')

    # The bounded degraded cadence: the tick leaves 0 and advances at
    # least the declared floor across the window — never the defect's
    # frozen 0, never the ~700x collapse.
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

    # The serving bounds: every endpoint the window reached must have
    # answered inside its declared bound with its named verdict — the
    # defect's serialized stalls starve the lock-taking set past it,
    # and a mislabeled refusal is the pending surface lying about the
    # closed gate.
    serving = False
    for read in reads:
        if read.get('answered'):
            serving = True
        if not serving:
            continue
        bound = read.get('bound', MIRROR_BOUND)
        label = str(read.get('kind', 'mirror')) + ' ' \
            + str(read.get('method', 'GET')) + ' ' \
            + str(read.get('path'))
        if not read.get('answered'):
            failed('read-bound', label + ' never answered inside its '
                   + format(bound, '.1f') + 's bound on the serving '
                   'monitor' + (' — ' + str(read.get('error'))
                                if read.get('error') else ''))
            continue
        if (read.get('elapsed') or 0) > bound:
            failed('read-bound', label + ' answered in '
                   + format(read['elapsed'], '.2f') + 's past the '
                   + format(bound, '.1f') + 's bound — the wedged '
                   'scan\'s lock hold leaked tens of seconds of '
                   'serialized field timeouts into the serving lane')
            continue
        want_status = read.get('want_status', 200)
        if read.get('status') != want_status:
            failed('refusal-verdict', label + ' answered status '
                   + str(read.get('status')) + ' — the pending '
                   'contract names ' + str(want_status))
            continue
        want_verdict = read.get('want_verdict')
        if want_verdict is not None \
                and read.get('verdict') != want_verdict:
            failed('refusal-verdict', label + ' answered '
                   + str(read.get('verdict')) + ' — the pending '
                   'contract names ' + str(want_verdict))
    probed = {read.get('path') for read in reads
              if read.get('kind') == 'lock'}
    for _, path, _, _ in LOCK_CALLS:
        if path not in probed:
            nondet('unprobed', 'the frozen window never reached ' + path
                   + ' — the pass cannot speak for its bound')
    if int_ages and max(int_ages) > SCAN_AGE_BOUND:
        failed('scan-age', 'the served last_scan_age_ms reached '
               + str(max(int_ages)) + 'ms past the '
               + str(SCAN_AGE_BOUND) + 'ms bound — the paced scan '
               'is not completing near one field timeout')

    # The thaw: the first answered contact re-issues the deferred
    # conditional grant — the pending seat walks standby → promoting →
    # active and reports the claim held, per the standing born-active
    # contract.
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

    # The contrast control: the connection-refused field fails
    # instantly — the same serving set on the refused-field pending
    # seat answers inside the instant bound with the same named
    # verdicts, the signature distinguishing the bounded stall from an
    # unreachable transport.
    # A refused-field seat that departed is the pre-contract shape the
    # pass reports inconclusive on before the judge runs; reaching here
    # with one means the control left the judge nothing to speak for,
    # which is an instability, never a silent pass.
    refused = record.get('refused') or {}
    if refused.get('departed'):
        nondet('refused-watch', 'the refused-field pending seat '
               'departed rather than serving the contrast — the '
               'contrast cannot speak for the instant bound')
    elif refused.get('stage_error') is not None:
        nondet('refused-stage', 'the refused-field staging never '
               'completed: ' + str(refused['stage_error']))
    elif not refused.get('served'):
        nondet('refused-watch', 'the refused-field pending seat never '
               'served its monitor while standing')
    else:
        for read in refused.get('reads') or []:
            bound = read.get('bound', REFUSED_BOUND)
            label = str(read.get('kind', 'mirror')) + ' ' \
                + str(read.get('method', 'GET')) + ' ' \
                + str(read.get('path'))
            if not read.get('answered'):
                failed('refused-bound', 'the refused-field contrast: '
                       + label + ' never answered inside its '
                       + format(bound, '.1f') + 's bound — a '
                       'connection-refused field fails instantly')
                continue
            if (read.get('elapsed') or 0) > bound:
                failed('refused-bound', 'the refused-field contrast: '
                       + label + ' answered in '
                       + format(read['elapsed'], '.2f') + 's past the '
                       + format(bound, '.1f') + 's bound — a refused '
                       'connect must never queue behind a scan')
                continue
            want_status = read.get('want_status', 200)
            if read.get('status') != want_status:
                failed('refused-verdict', 'the refused-field contrast: '
                       + label + ' answered status '
                       + str(read.get('status')) + ' — the pending '
                       'contract names ' + str(want_status))
                continue
            want_verdict = read.get('want_verdict')
            if want_verdict is not None \
                    and read.get('verdict') != want_verdict:
                failed('refused-verdict', 'the refused-field '
                       'contrast: ' + label + ' answered '
                       + str(read.get('verdict')) + ' — the pending '
                       'contract names ' + str(want_verdict))
        refused_views = refused.get('views') or []
        if any(view.get('role') != 'standby'
               or view.get('field_claim') == 'held'
               or view.get('sync') not in _HONEST_SYNC
               for view in refused_views):
            failed('refused-pending', 'the refused-field pending seat '
                   'reported a verdict it cannot hold: '
                   + json.dumps(refused_views)[:300])

    # The deployed pair: undisturbed before and after the leg's
    # scratch staging.
    if not _pend_pair_held(record):
        nondet('pair-disturbed', 'the deployed pair moved or wedged '
               'across the scratch staging: '
               + json.dumps(record.get('roles'), sort_keys=True)[:300])


def _serving_digest(record, violations):
    """The pass's normalized verdict record — identical digests across
    two consecutive passes is the determinism contract."""
    def clean(*keys):
        return not any(key in violations for key in keys)
    return {
        'pending': 'held'
                   if clean('pending-role', 'pending-claimed',
                            'pending-sync', 'checkpoint-claimed',
                            'surface-unread', 'watch-starved',
                            'state-read')
                   else 'defect',
        'cadence': 'bounded'
                   if clean('tick-frozen', 'cadence', 'scan-age',
                            'ticks-unread')
                   else 'defect',
        'serving': 'bounded' if clean('read-bound', 'refusal-verdict',
                                      'unprobed') else 'defect',
        'refused': 'instant'
                   if clean('refused-bound', 'refused-verdict',
                            'refused-pending', 'refused-watch',
                            'refused-stage')
                   else 'defect',
        'recovery': 'claimed'
                    if clean('deferred-claim', 'grant-journal')
                    else 'defect',
        'pair': 'undisturbed' if clean('pair-disturbed')
                else 'disturbed'}


def _serving_teardown(ctx):
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


def _refused_control(ctx):
    """The contrast control — the connection-refused field case: the
    pending seat is torn down, the scratch field re-staged 'silent'
    (the address resolvable but nothing listening, so every attach
    refuses instantly), and the seat relaunched pending on it. The
    same serving set then answers inside the instant bound with the
    same named verdicts — a refused connect never queues the scan, so
    the control separates the bounded stall the fix names from a
    transport that was never slow."""
    control = {'served': False, 'departed': False, 'reads': [],
               'views': []}
    try:
        ctx['stop_born_controller'](SEAT)
        ctx['stop_born_field']()
        field = ctx['start_born_field']('silent')
        control['remote'] = field.get('remote')
        ctx['start_born_controller'](SEAT, control['remote'])
    except Exception as exc:
        control['stage_error'] = str(exc)[:300]
        return control
    served = wait_for(lambda: _pend_report(ctx) or None,
                      time.monotonic() + SERVING_SETTLE,
                      interval=SERVING_POLL)
    if served is None:
        try:
            state = _pend_state(ctx, SEAT)
        except Exception:
            state = None
        if state is not None \
                and (state.get('absent')
                     or (state.get('running') is False
                         and state.get('exit') is not None)):
            control['departed'] = True
        return control
    control['served'] = True
    for path in MIRROR_PATHS:
        read = _pend_probe(ctx[SEAT], 'GET', path, REFUSED_BOUND)
        read['kind'] = 'mirror'
        read['want_status'] = 200
        control['reads'].append(read)
        if path == '/role' and read.get('answered'):
            control['views'].append({
                'role': read.get('role'),
                'sync': _pend_sync(read.get('sync')),
                'field_claim': read.get('field_claim')})
    for method, path, status, verdict in LOCK_CALLS:
        read = _pend_probe(
            ctx[SEAT], method, path, REFUSED_BOUND,
            body=COMMAND_BODY if path == '/command' else None)
        read.update({'kind': 'lock', 'want_status': status,
                     'want_verdict': verdict})
        control['reads'].append(read)
    return control


def _serving_pass(ctx, number, launch):
    """One pass over the pending born-active's bounded-serving window:
    stage the scratch field, freeze it under `docker pause`, launch the
    labeled born-active --remote against it, probe the pending seat's
    serving monitor through the frozen window, thaw the field and read
    the deferred grant's landing, then run the refused-field contrast —
    the deployed pair framed before and after."""
    record = {'pass': number, 'launch_roles': dict(launch),
              'roles': {}, 'frozen': {}, 'recovery': {}, 'refused': {}}
    owner, peer = launch['owner'], launch['peer']
    record['roles']['before'] = {
        name: _pend_pair_view(ctx, name) for name in (owner, peer)}
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
        # pending surface must exist before its bounds can be read.
        # A launch that never serves while its container stands is
        # the nondeterministic shape; one that exited is the
        # pre-contract shape.
        served = wait_for(lambda: _pend_report(ctx) or None,
                          time.monotonic() + SERVING_SETTLE,
                          interval=SERVING_POLL)
        if served is None:
            try:
                state = _pend_state(ctx, SEAT)
            except Exception:
                state = None
            if state is not None and (state.get('absent')
                                      or (state.get('running') is False
                                          and state.get('exit')
                                          is not None)):
                record['inconclusive'] = (
                    'the labeled born-active exited rather than '
                    'standing pending on the frozen field — the '
                    'staged revision predates the born-active '
                    'pending contract')
            else:
                record['frozen'] = {'window_s': 0, 'reads': [],
                                    'ticks': [], 'ages': [],
                                    'views': [], 'states': [],
                                    'departed': False,
                                    'state_error': None}
            return record
        record['frozen'] = _pend_window(ctx, ctx[SEAT])
        pre_contract = _pend_pre_contract(record['frozen'])
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
                     else None)(_pend_report(ctx)),
            time.monotonic() + SERVING_SETTLE, interval=SERVING_POLL)
        record['recovery']['grant_journaled'] = (
            _pend_transition(ctx, 'standby', 'promoting')
            and _pend_transition(ctx, 'promoting', 'active'))
        # The contrast control — the connection-refused field case.
        record['refused'] = _refused_control(ctx)
        if record['refused'].get('departed'):
            record['inconclusive'] = (
                'the labeled born-active exited rather than standing '
                'pending on the refused field — the staged revision '
                'predates the born-active pending contract')
            return record
        return record
    finally:
        record['roles']['after'] = {
            name: _pend_pair_view(ctx, name) for name in (owner, peer)}


def _serving_self_check():
    """The leg's unchecked-diagnostic self-test: replay the judge over
    each planted negative — bounded serving asserted while a
    lock-taking endpoint queues ~N_channels timeouts behind the wedged
    pending scan, a starved or mislabeled refusal, a pending surface
    reporting a verdict it cannot hold, a frozen or under-floor
    cadence, an unbounded scan age, the deferred grant never landing
    or never journaling, the refused-field contrast stalling — and
    require each to trip. Returns the negative names the judge let
    through."""
    def role_read(tick):
        return {'method': 'GET', 'path': '/role', 'kind': 'mirror',
                'bound': 1.0, 'answered': True, 'status': 200,
                'elapsed': 0.002, 'role': 'standby', 'tick': tick,
                'sync': 'unsynchronized', 'field_claim': None}

    def health_read(age):
        return {'method': 'GET', 'path': '/health', 'kind': 'mirror',
                'bound': 1.0, 'answered': True, 'status': 200,
                'elapsed': 0.002, 'live': True, 'role': 'standby',
                'tick': 1, 'last_scan_age_ms': age}

    def lock_call(path, verdict, elapsed=0.05, answered=True,
                  status=None):
        table = {'/checkpoint': ('GET', 200, 'document'),
                 '/promote': ('POST', 409, 'not_converged'),
                 '/demote': ('POST', 409, 'not_active'),
                 '/command': ('POST', 200, 'rejected:not_active')}
        method, code, want = table[path]
        read = {'method': method, 'path': path, 'kind': 'lock',
                'bound': 9.0, 'answered': answered, 'elapsed': elapsed,
                'want_status': code, 'want_verdict': want}
        if answered:
            read.update({'status': status if status is not None
                         else code, 'verdict': verdict})
        else:
            read['error'] = 'timed out'
        return read

    def refused_read(path):
        table = {'/role': ('GET', 200, 'document'),
                 '/health': ('GET', 200, 'document'),
                 '/checkpoint': ('GET', 200, 'document'),
                 '/promote': ('POST', 409, 'not_converged'),
                 '/demote': ('POST', 409, 'not_active'),
                 '/command': ('POST', 200, 'rejected:not_active')}
        method, code, verdict = table[path]
        kind = 'mirror' if path in MIRROR_PATHS else 'lock'
        return {'method': method, 'path': path, 'kind': kind,
                'bound': 2.0, 'answered': True, 'status': code,
                'elapsed': 0.004, 'verdict': verdict,
                'want_status': code, 'want_verdict': verdict,
                'role': 'standby', 'tick': 30,
                'sync': 'unsynchronized', 'field_claim': None,
                'live': True, 'last_scan_age_ms': 30}

    def clean_record():
        ticks = [1, 2, 3, 5]
        return {'pass': 1,
                'launch_roles': {'owner': 'active', 'peer': 'standby'},
                'remote': 'dcs-hw-qa-1-born-plant:9003',
                'launch': {'seat': SEAT},
                'unpaused': True,
                'frozen': {'window_s': 20.0, 'departed': False,
                           'state_error': None,
                           'reads': [role_read(tick)
                                     for tick in ticks]
                           + [health_read(age)
                              for age in (110, 120, 130, 140)]
                           + [lock_call('/checkpoint', 'document'),
                              lock_call('/promote', 'not_converged'),
                              lock_call('/demote', 'not_active'),
                              lock_call('/command',
                                        'rejected:not_active')],
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
                'refused': {'served': True, 'departed': False,
                            'remote': 'dcs-hw-qa-1-born-plant:9003',
                            'reads': [refused_read(path)
                                      for path in
                                      ('/role', '/health',
                                       '/checkpoint', '/promote',
                                       '/demote', '/command')],
                            'views': [{'role': 'standby',
                                       'sync': 'unsynchronized',
                                       'field_claim': None}]},
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
        _judge_serving(
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

    # The doctored negative the issue names — bounded serving asserted
    # while a lock-taking endpoint queues ~N_channels timeouts behind
    # the wedged pending scan: the promote call answered in ~72s, past
    # its declared bound.
    expect('bounded-asserted-but-queued', lambda r:
           r['frozen']['reads'].append(
               {'method': 'POST', 'path': '/promote', 'kind': 'lock',
                'bound': 9.0, 'answered': True, 'status': 409,
                'elapsed': 72.4, 'verdict': 'not_converged',
                'want_status': 409, 'want_verdict': 'not_converged'}))
    expect('lock-read-starved', lambda r:
           r['frozen']['reads'].append(
               {'method': 'GET', 'path': '/checkpoint', 'kind': 'lock',
                'bound': 9.0, 'answered': False, 'elapsed': 9.0,
                'error': 'timed out', 'want_status': 200,
                'want_verdict': 'document'}))
    expect('mirror-read-starved', lambda r:
           r['frozen']['reads'].append(
               {'method': 'GET', 'path': '/role', 'kind': 'mirror',
                'bound': 1.0, 'answered': False, 'elapsed': 1.0,
                'error': 'timed out'}))
    expect('promote-mislabeled', lambda r:
           r['frozen']['reads'].append(lock_call(
               '/promote', 'field_claim_failed')))
    expect('promote-admitted', lambda r:
           r['frozen']['reads'].append(lock_call(
               '/promote', 'document', status=200)))
    expect('demote-mislabeled', lambda r:
           r['frozen']['reads'].append(lock_call(
               '/demote', 'no_tracking_source')))
    expect('command-admitted', lambda r:
           r['frozen']['reads'].append(lock_call(
               '/command', 'accepted')))
    expect('checkpoint-unreadable', lambda r:
           r['frozen']['reads'].append(lock_call(
               '/checkpoint', 'malformed')))
    expect('checkpoint-claims-field', lambda r:
           r['frozen']['reads'].append(
               dict(lock_call('/checkpoint', 'document'),
                    source_owns_field=True)))
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
    expect('scan-age-unbounded', lambda r:
           r['frozen'].update(ages=[110, 120, 130, 60000]))
    expect('grant-never-lands', lambda r:
           r['recovery'].update(settled=None))
    expect('grant-unjournaled', lambda r:
           r['recovery'].update(grant_journaled=False))
    expect('grant-settles-unclaimed', lambda r:
           r['recovery'].update(
               settled={'role': 'active', 'field_claim': None}))
    expect('refused-stalls', lambda r:
           r['refused']['reads'][3].update(
               answered=False, elapsed=2.0, error='timed out'))
    expect('refused-late', lambda r:
           r['refused']['reads'][2].update(elapsed=4.5))
    expect('refused-mislabeled', lambda r:
           r['refused']['reads'][3].update(verdict='field_claim_failed'))
    expect('refused-dishonest-surface', lambda r:
           r['refused']['views'][0].update(field_claim='held'))
    expect('unprobed-endpoint', lambda r:
           r['frozen'].update(reads=[
               read for read in r['frozen']['reads']
               if read.get('path') != '/command']), DIAG_NONDET)
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
    expect('refused-stage-refused', lambda r:
           r['refused'].update(stage_error='docker run failed'),
           DIAG_NONDET)
    expect('refused-watch-starved', lambda r:
           r['refused'].update(served=False), DIAG_NONDET)
    expect('refused-departed', lambda r:
           r['refused'].update(served=True, departed=True), DIAG_NONDET)
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


def scenario_pending_serving_bound(ctx):
    """Exercise the pending born-active's bounded serving contract: a
    labeled born-active launched --remote against a `docker pause`-
    frozen scratch field stands pending, and through its serving
    monitor every lock-taking endpoint — the checkpoint pull, the
    promote/demote switchovers, and the receipted command path — must
    answer inside the declared bound near one field timeout with its
    named verdict (not_converged, not_active, not_active) rather than
    queue tens of seconds behind the wedged scan, while /role and
    /health stay instant and the tick holds its bounded degraded
    cadence; the connection-refused contrast field answers the same
    set instantly, the thaw lands the deferred conditional grant, the
    deployed pair is undisturbed, and two passes produce identical
    digests."""
    case = Case(
        'pending-serving-bound',
        'A pending born-active on a frozen field keeps every '
        'lock-taking endpoint inside its declared bound',
        'a labeled born-active --remote launch against a plant '
        'frozen by the lane\'s container-pause lever — connectable '
        'but never answering — stands pending while its serving '
        'monitor answers GET /checkpoint, POST /promote '
        '(not_converged), POST /demote (not_active), and POST '
        '/command (receipted not_active) each inside the declared '
        'bound near one field timeout — never ~N_channels timeouts '
        'serialized behind the wedged scan — /role and /health '
        'answer instantly, the tick advances at the bounded degraded '
        'rate and the served scan age stays bounded, the '
        'connection-refused contrast field answers the same set '
        'instantly, the thawed field lands the deferred conditional '
        'claim, the deployed pair is undisturbed, and two passes '
        'produce identical digests')
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
        deadline = time.monotonic() + SERVING_SETTLE
        owner = wait_for(lambda: _pair_active(ctx), deadline,
                         interval=SERVING_POLL)
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
                    interval=SERVING_POLL) is None:
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
                record = _serving_pass(ctx, number, launch)
            finally:
                # Each pass ends with the rig swept — the seat and
                # the scratch field removed so the next pass and the
                # legs behind this one start cold, and the launch
                # configuration is restored as found.
                _serving_teardown(ctx)
            if not record.get('inconclusive'):
                _judge_serving(record, note)
            digest = _serving_digest(record, violations)
            record['digest'] = dict(digest)
            record['violations'] = {
                key: diagnostic
                for key, (diagnostic, _) in violations.items()}
            ref = save_evidence(
                ctx['evidence_dir'],
                'pending-serving-bound-pass-' + str(number)
                + '.json', record)
            case.evidence('file', ref,
                          'pending-serving-bound pass '
                          + str(number) + ' — the frozen window\'s '
                          'timed mirror and lock-taking reads, ticks, '
                          'and scan ages, the pending surface, the '
                          'deferred grant\'s recovery, the '
                          'refused-field contrast, the pair framing, '
                          'and the normalized digest')
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
        case.observe('two pending-serving-bound passes, identical '
                     'digests: '
                     + json.dumps(digests[0], sort_keys=True))
        slipped = _serving_self_check()
        if slipped:
            return case.finish('failed', DIAG_UNCHECKED
                               + ': planted negatives slipped the '
                               'leg’s own audits: '
                               + ', '.join(slipped))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
