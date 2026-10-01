"""The scan_batch_bound acceptance leg — one module per leg of the
scenario schedule; see qa_lane/scenarios/__init__.py for the ordering
rule and the shared seam."""
from .common import *

# Ordering: The scan-batch-bound case runs late — after the failover
# switch and the earlier field legs, inside the unpinned window before
# the unclaimed-rearm leg's pinned tail: it promotes the run's driven
# third peer onto the field for the disconnect leg and restores the
# pair's launch roles, so it needs a settled pair, the driven
# launch/teardown actions, and the published plant endpoint.
RUNS_AFTER = frozenset({'scenario_failover'})


# --------------------------------------------------------------------
# The bounded driven-scan batch contract (WW-FND-004's bounded-consumer
# rule — #1203's fix pinned as per-run lane evidence): a driven peer's
# run is exactly as deterministic as the requests driving it, which
# requires every request to terminate. POST /scan must refuse a scans
# count above the declared per-request bound by name — 'refused: scans
# <n> exceeds the per-request bound of 256', answered before the first
# scan — a client disconnect mid-batch must terminate the batch, and
# no single batch may pin a submission-lane worker for an unbounded
# duration. The defect this evidence convicts had a u64-unbounded
# in-request loop surviving client disconnect, a plant-server restart,
# and every operator cancel path — on a field-owning driven peer each
# scan also steps the shared plant, so an uncancellable batch diverges
# the shared plant's timeline far past every peer's checkpoint line.
#
# The leg launches the run's driven third controller through
# ctx['start_driven'] — `--standby <owner> --driven`, scans only inside
# POST /scan — converges it, and per pass probes the named refusal on
# the tracking standby, then moves field ownership onto it through the
# demote-then-promote hand-off so its scans step the shared plant: the
# bound batch is posted on a throwaway client that severs mid-flight,
# and the served tick plus the plant's own step counter must show the
# batch's bounded remainder completing — the tick reaching the bound
# and stopping, the plant's counter climbing by the bound and freezing
# — never racing on for a dead client. A bounded follow-up request
# must still answer, proving the submission worker freed with the
# batch's end. The pair's launch roles and the field's owner restore
# before the pass ends, and the driven container is removed at the
# leg's close. Named diagnostics are scan-batch-bound-failed for a
# contract miss — an accepted or unnamed refusal, a refused batch that
# ran anyway, a batch that never ended, a served tick or plant counter
# that kept climbing for the dead client, a pinned worker, a restore
# that never landed — scan-batch-bound-nondeterministic when two
# passes disagree, and scan-batch-bound-unchecked when the
# self-check's planted negatives slip the leg's own audits. A run
# whose monitor predates the driven-scan contract, whose pair cannot
# settle, or whose rig outruns the mid-flight staging reports
# inconclusive.

SCAN_BATCH_BOUND = 256  # crates/dcs-monitor's MAX_SCANS_PER_REQUEST —
                        # the declared bound the refusal names
BOUND_SETTLE = 45       # bound on each converge/switch/restore wait
BOUND_POLL = 0.25       # the settle-window poll cadence
CONVERGE_SCANS = 4      # driven scans per convergence prod — every
                        # pull the peer runs lands inside a request
REFUSAL_TIMEOUT = 10    # bound on the over-bound probe answering — an
                        # accepted batch is exactly the request that
                        # never ends
SEVER_WATCH = 8.0       # bound on one attempt's mid-flight watch on
                        # the plant's step counter
SEVER_POLL = 0.01       # the mid-flight watch cadence — tight against
                        # the batch's per-scan plant steps
SEVER_ATTEMPTS = 3      # sever-staging retries; each attempt is a
                        # legal bounded batch
TERMINATE_DEADLINE = 60  # bound on the severed batch's remainder
                         # completing — the cancellation the declared
                         # bound is
PLATEAU_HOLD = 2.0      # the post-termination watch window — a batch
                        # racing for a dead client keeps the counters
                        # climbing inside it
PROBE_SCANS = 1         # the follow-up request proving the worker freed
DIAG_FAILED = 'scan-batch-bound-failed'
DIAG_NONDET = 'scan-batch-bound-nondeterministic'
DIAG_UNCHECKED = 'scan-batch-bound-unchecked'


def _scan_call(ctx, scans, timeout=20):
    """POST /scan on the driven peer answering (status, body) —
    _settle_call's refused-verdict decoding for a request with a
    body."""
    try:
        return http_json('POST', ctx['driven'] + '/scan',
                         {'scans': scans}, timeout=timeout)
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read() or b'null')
        except ValueError:
            body = None
        finally:
            exc.close()
        return exc.code, body


def _driven_tick(ctx):
    """The driven monitor's served tick, or None — the batch's own
    counter: on the driven peer only a requested scan advances it."""
    snapshot = _try_snapshot(ctx, ctx['driven'])
    tick = (snapshot or {}).get('tick')
    return tick if isinstance(tick, int) \
        and not isinstance(tick, bool) else None


def _plant_tick(ctx):
    """The shared plant's step counter through the published protocol
    endpoint's `ping`, or None — every field-owning driven scan
    advances it exactly once."""
    answer = _try_plant(ctx, {'op': 'ping'})
    tick = (answer or {}).get('tick')
    return tick if isinstance(tick, int) \
        and not isinstance(tick, bool) else None


def _sever_client(ctx, scans):
    """The dead client the leg severs mid-batch: a raw HTTP/1.1 POST
    of the batch on the driven monitor — a request whose answer the
    client never reads back."""
    base = ctx['driven'].split('://', 1)[-1]
    host, _, port = base.partition('/')[0].rpartition(':')
    stream = socket.create_connection(
        (host or '127.0.0.1', int(port)), timeout=5)
    body = json.dumps({'scans': scans}).encode()
    stream.sendall(
        ('POST /scan HTTP/1.1\r\nHost: ' + base
         + '\r\nContent-Type: application/json\r\nContent-Length: '
         + str(len(body))
         + '\r\nConnection: close\r\n\r\n').encode() + body)
    return stream


def _severed_batch(ctx, plant_base, scans):
    """Post the batch on a throwaway client and sever it mid-flight:
    the plant's step counter is the watch — once it has advanced past
    the baseline but short of the bound, the batch is provably in
    flight and the client closes. Returns ('severed', tick) for a
    mid-flight sever, ('finished', tick) when the batch completed
    inside the watch before a mid-flight window appeared, or
    ('lost', None) when the step counter dropped the watch."""
    stream = _sever_client(ctx, scans)
    try:
        deadline = time.monotonic() + SEVER_WATCH
        while time.monotonic() < deadline:
            tick = _plant_tick(ctx)
            if tick is None:
                time.sleep(SEVER_POLL)
                continue
            if tick >= plant_base + scans:
                return 'finished', tick
            if tick > plant_base:
                return 'severed', tick
            time.sleep(SEVER_POLL)
        return 'lost', None
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _sync_state(report):
    """The named convergence state a /role report carries — the
    restore record's deterministic shape, never the tick-bearing
    payload."""
    sync = (report or {}).get('sync')
    if isinstance(sync, dict):
        return next(iter(sync), 'none')
    return sync or 'none'


def _converge_driven(ctx, deadline):
    """Drive the driven peer in bounded batches until it reports a
    tracking standby — every pull it performs happens inside a
    POST /scan, so convergence is request-paced. Returns the tracking
    report or None."""
    while time.monotonic() < deadline:
        report = _tracking_standby(ctx, 'driven')
        if report is not None:
            return report
        try:
            _scan_call(ctx, CONVERGE_SCANS)
        except Exception:
            pass
        time.sleep(BOUND_POLL)
    return None


def _judge_batch_refusal(record, note):
    """Audit one over-bound /scan probe: the contract's named refusal
    is a 400 naming the per-request bound answered before the first
    scan, the refused batch running nothing. An accepted batch — the
    u64-unbounded defect's own shape — an unnamed refusal, a wrong
    verdict, or a refused batch that ran scans anyway each names its
    failure."""
    scans, status = record.get('scans'), record.get('status')
    text = json.dumps(record.get('body'))
    if status == 'unanswered':
        if isinstance(record.get('tick0'), int) \
                and isinstance(record.get('tick1'), int) \
                and record['tick1'] > record['tick0']:
            note('refusal-accepted', DIAG_FAILED,
                 'POST /scan with scans=' + str(scans)
                 + ' never answered — the over-bound batch is '
                 'running, the u64-unbounded defect\'s own shape')
        else:
            note('refusal-unanswered', DIAG_FAILED,
                 'POST /scan with scans=' + str(scans)
                 + ' never answered inside the probe bound — the '
                 'request owes a bounded answer')
        return
    if status == 200:
        note('refusal-accepted', DIAG_FAILED,
             'POST /scan accepted scans=' + str(scans) + ' — past '
             'the declared bound of ' + str(SCAN_BATCH_BOUND)
             + ', the u64-unbounded defect reproduced')
        return
    if status != 400:
        note('refusal-verdict', DIAG_FAILED,
             'POST /scan with scans=' + str(scans) + ' answered '
             + str(status) + ' ' + text[:160]
             + ' — not the named bound refusal')
        return
    if 'per-request bound' not in text \
            or str(SCAN_BATCH_BOUND) not in text:
        note('refusal-unnamed', DIAG_FAILED,
             'the refusal ' + text[:160] + ' never named the '
             'per-request bound of ' + str(SCAN_BATCH_BOUND))
        return
    if isinstance(record.get('tick0'), int) \
            and isinstance(record.get('tick1'), int) \
            and record['tick1'] != record['tick0']:
        note('refusal-ran', DIAG_FAILED,
             'the refused batch still ran — the served tick moved '
             + str(record['tick0']) + ' -> ' + str(record['tick1'])
             + ' across the refusal')


def _judge_batch_disconnect(record, note):
    """Audit the severed batch's ending: the served tick climbed to
    the bound and stayed — the bounded remainder the request always
    completes — the shared plant's counter climbed by exactly the
    bound and froze (a dead client owns no further steps), and the
    follow-up bounded request answered — the batch's worker freed
    with its end."""
    base = record.get('served_base')
    final = record.get('served_final')
    if not isinstance(base, int) or not isinstance(final, int) \
            or final != base + SCAN_BATCH_BOUND:
        key = 'raced-past-bound' \
            if isinstance(base, int) and isinstance(final, int) \
            and final > base + SCAN_BATCH_BOUND \
            else 'batch-unfinished'
        note(key, DIAG_FAILED,
             'the severed batch\'s served tick reached '
             + json.dumps(final) + ' against the bound '
             + json.dumps(None if base is None
                          else base + SCAN_BATCH_BOUND)
             + (' — still climbing for the dead client'
                if key == 'raced-past-bound'
                else ' — it never terminated'))
        return
    if isinstance(record.get('served_ceiling'), int) \
            and record['served_ceiling'] > base + SCAN_BATCH_BOUND:
        note('raced-past-bound', DIAG_FAILED,
             'the dead client\'s batch kept the served tick climbing '
             'past the bound: ' + str(record['served_ceiling'])
             + ' > ' + str(base + SCAN_BATCH_BOUND))
    hold = record.get('plateau') or {}
    if hold.get('served_held') is False:
        note('served-climbed', DIAG_FAILED,
             'the severed batch\'s served tick kept moving through '
             'the plateau window — still running for the dead '
             'client')
    plant_base, plant_final = (record.get('plant_base'),
                               record.get('plant_final'))
    if isinstance(plant_base, int) and isinstance(plant_final, int) \
            and plant_final - plant_base != SCAN_BATCH_BOUND:
        note('plant-divergence', DIAG_FAILED,
             'the shared plant\'s counter moved '
             + str(plant_final - plant_base)
             + ' steps across the bound batch — one field-owning '
             'scan is one step, so the run\'s timeline diverged '
             'from its tick')
    if hold.get('plant_held') is False:
        note('plant-kept-stepping', DIAG_FAILED,
             'the shared plant kept stepping through the plateau '
             'window on the dead client\'s behalf — the '
             'uncancellable batch\'s diverging timeline')
    if record.get('probe_status') != 200:
        note('worker-pinned', DIAG_FAILED,
             'a bounded request after the severed batch answered '
             + json.dumps(record.get('probe_status'))
             + ' — its submission worker stayed pinned past the '
             'batch\'s end')


def _judge_batch_restore(record, note):
    """Audit the pass's hand-back: the driven owner's demote and the
    entry owner's promote both answered, the settled layout is the
    launch's — the pair member active again, the sibling tracking,
    the driven peer tracking — and the shared plant steps under the
    launch pair once more."""
    restore = record.get('restore') or {}
    if restore.get('demote_status') != 200:
        note('restore-demote', DIAG_FAILED,
             'POST /demote on the driven owner answered '
             + json.dumps(restore.get('demote_status')) + ' '
             + json.dumps(restore.get('demote_body'))[:160]
             + ' — the hand-back refused')
    if restore.get('promote_status') != 200:
        note('restore-promote', DIAG_FAILED,
             'POST /promote on the entry owner answered '
             + json.dumps(restore.get('promote_status')) + ' '
             + json.dumps(restore.get('promote_body'))[:160]
             + ' — the launch role never re-took the field')
    roles = restore.get('roles') or {}
    if roles.get('owner') != 'active' \
            or roles.get('peer') != 'tracking' \
            or roles.get('driven') != 'tracking':
        note('restore-roles', DIAG_NONDET,
             'the settle left ' + json.dumps(roles, sort_keys=True)
             + ' — not the launch layout\'s active owner plus two '
             'tracking standbys')
    if restore.get('plant_resumed') is not True:
        note('restore-plant', DIAG_FAILED,
             'the shared plant never stepped again under the '
             'restored owner — the field\'s stepping did not come '
             'back with the launch roles')


def _self_check():
    """The unchecked-diagnostic guard: replay the leg's auditors over
    planted negatives — an accepted or unnamed refusal, a wrong
    verdict, a refused batch that ran, a batch that never ended or
    raced past its bound, a served tick or plant counter that kept
    climbing for the dead client, a pinned worker, a refused or
    half-landed restore — and report every one let slip."""
    slipped = []

    def audit(judge, record):
        found = []
        judge(record, lambda key, diagnostic, detail: found.append(key))
        return found

    def expect_flagged(name, found):
        if not found:
            slipped.append(name)

    def expect_clean(name, found):
        if found:
            slipped.append(name + '-overstrict')

    refusal = {'scans': SCAN_BATCH_BOUND + 1, 'status': 400,
               'body': 'refused: scans ' + str(SCAN_BATCH_BOUND + 1)
                       + ' exceeds the per-request bound of '
                       + str(SCAN_BATCH_BOUND),
               'tick0': 40, 'tick1': 40}
    expect_clean('refusal-clean', audit(_judge_batch_refusal, refusal))
    for name, change in (
            ('refusal-accepted', {'status': 200}),
            ('refusal-accepted', {'status': 'unanswered',
                                  'tick1': 41}),
            ('refusal-unanswered', {'status': 'unanswered'}),
            ('refusal-verdict', {'status': 503}),
            ('refusal-unnamed', {'body': 'refused'}),
            ('refusal-ran', {'tick1': 41})):
        record = dict(refusal)
        record.update(change)
        expect_flagged(name, audit(_judge_batch_refusal, record))

    disconnect = {'served_base': 100, 'plant_base': 500,
                  'served_final': 100 + SCAN_BATCH_BOUND,
                  'served_ceiling': 100 + SCAN_BATCH_BOUND,
                  'plateau': {'served_held': True, 'plant_held': True},
                  'plant_final': 500 + SCAN_BATCH_BOUND,
                  'probe_status': 200}
    expect_clean('disconnect-clean',
                 audit(_judge_batch_disconnect, disconnect))
    for name, change in (
            ('batch-unfinished',
             {'served_final': 100 + SCAN_BATCH_BOUND - 1}),
            ('batch-unfinished', {'served_final': None}),
            ('raced-past-bound',
             {'served_ceiling': 101 + SCAN_BATCH_BOUND}),
            ('served-climbed', {'plateau': {'served_held': False,
                                            'plant_held': True}}),
            ('plant-divergence',
             {'plant_final': 500 + SCAN_BATCH_BOUND - 1}),
            ('plant-kept-stepping', {'plateau': {'served_held': True,
                                                 'plant_held': False}}),
            ('worker-pinned', {'probe_status': None}),
            ('worker-pinned', {'probe_status': 503})):
        record = dict(disconnect)
        record.update(change)
        expect_flagged(name, audit(_judge_batch_disconnect, record))

    restore = {'demote_status': 200, 'promote_status': 200,
               'roles': {'owner': 'active', 'peer': 'tracking',
                         'driven': 'tracking'},
               'plant_resumed': True}
    expect_clean('restore-clean',
                 audit(_judge_batch_restore, {'restore': restore}))
    for name, change in (
            ('restore-demote', {'demote_status': 409}),
            ('restore-promote', {'promote_status': 409}),
            ('restore-roles', {'roles': {'owner': 'standby',
                                         'peer': 'tracking',
                                         'driven': 'tracking'}}),
            ('restore-plant', {'plant_resumed': False})):
        record = dict(restore)
        record.update(change)
        expect_flagged(name, audit(_judge_batch_restore,
                                   {'restore': record}))
    return slipped


def _batch_pass(ctx, number, owner, peer, launch):
    """One probe pass: the named-refusal probes on the converged
    tracking standby, the ownership hand-off onto the driven peer,
    the severed bound batch with the served-tick and plant-counter
    witnesses, and the launch-layout restore. Returns (digest,
    violations, record): digest is the pass's normalized verdict set
    two passes compare; violations maps each clause key to
    (diagnostic, detail); record carries 'inconclusive' when the pass
    itself could not stage."""
    record = {'pass': number, 'owner': owner, 'peer': peer,
              'launch_roles': dict(launch), 'refusals': []}
    violations = {}
    problems = []

    def note(key, diagnostic, detail):
        violations.setdefault(key, (diagnostic, detail))
        problems.append(key)

    def failed(key, detail):
        note(key, DIAG_FAILED, detail)

    # The settled gate: the entry layout the pass's restore owes —
    # the launch owner holding the field, the sibling tracking, and
    # the driven peer converged behind the same line.
    if _pair_active(ctx) != owner \
            or _tracking_standby(ctx, peer) is None:
        failed('settle', 'the pair never settled — ' + owner
               + ' holds no active role with ' + peer
               + ' tracking behind it')
        return None, violations, record
    converged = _converge_driven(
        ctx, time.monotonic() + BOUND_SETTLE)
    record['driven_converged'] = converged
    if converged is None:
        record['inconclusive'] = 'the driven peer never reported a ' \
            'tracking standby — the leg\'s staging has no driven run'
        return None, violations, record

    # The refusal probes: a count one past the bound and the defect's
    # own u64 shape — each must answer the named refusal before the
    # first scan. A 404 marks a run predating the driven-scan
    # contract.
    for scans in (SCAN_BATCH_BOUND + 1, (1 << 64) - 1):
        tick0 = _driven_tick(ctx)
        verdict = {'scans': scans, 'tick0': tick0}
        try:
            status, body = _scan_call(ctx, scans,
                                      timeout=REFUSAL_TIMEOUT)
            verdict.update({'status': status, 'body': body})
        except Exception as exc:
            verdict.update({'status': 'unanswered',
                            'error': str(exc)[:200]})
        verdict['tick1'] = _driven_tick(ctx)
        record['refusals'].append(verdict)
        if verdict['status'] in (404, 405):
            record['inconclusive'] = 'POST /scan answered ' \
                + str(verdict['status']) + ' — the staged run ' \
                'predates the driven-scan contract'
            return None, violations, record
        _judge_batch_refusal(verdict, note)
    if problems:
        return None, violations, record

    # The ownership hand-off: the entry owner's demote leaves the
    # claim standing yielded — the deliberate hand-back a
    # conditional claim preempts — then the driven peer's promote
    # takes the field so each of its scans steps the shared plant.
    status, body = _settle_call(ctx[owner] + '/demote')
    record['demote'] = {'status': status, 'body': body}
    if status != 200:
        failed('demote', 'POST /demote on the field owner answered '
               + str(status) + ' ' + json.dumps(body)[:200])
        return None, violations, record
    try:
        _scan_call(ctx, 1)
    except Exception as exc:
        record['inconclusive'] = 'the driven peer\'s ownerless-' \
            'observing scan never answered: ' + str(exc)[:200]
        return None, violations, record
    status, body = _settle_call(ctx['driven'] + '/promote')
    record['promote'] = {'status': status, 'body': body}
    if status != 200:
        failed('driven-promote', 'POST /promote on the driven peer '
               'answered ' + str(status) + ' '
               + json.dumps(body)[:200])
        return None, violations, record
    active, deadline = None, time.monotonic() + BOUND_SETTLE
    while active is None and time.monotonic() < deadline:
        try:
            _scan_call(ctx, 1)
        except Exception:
            pass
        report = _try_role(ctx, ctx['driven'])
        if (report or {}).get('role') == 'active':
            active = report
        else:
            time.sleep(BOUND_POLL)
    record['driven_active'] = active
    if active is None:
        failed('driven-active', 'the promoted driven peer never '
               'reported role=active — its scans never took the '
               'field')
        return None, violations, record

    # The disconnect leg: the bound batch on a throwaway client,
    # severed mid-flight. The plant's step counter is the in-flight
    # watch — each field-owning scan advances it — and the batch's
    # own served tick the termination witness.
    sever = {'status': None, 'at': None, 'attempts': []}
    served_base = plant_base = None
    for attempt in range(1, SEVER_ATTEMPTS + 1):
        plant_base = _plant_tick(ctx)
        served_base = _driven_tick(ctx)
        if plant_base is None or served_base is None:
            record['inconclusive'] = 'the disconnect leg\'s ' \
                'witnesses dropped — the plant\'s step counter or ' \
                'the served tick never answered the baseline'
            return None, violations, record
        status, at = _severed_batch(ctx, plant_base,
                                    SCAN_BATCH_BOUND)
        sever['attempts'].append({'attempt': attempt,
                                  'status': status, 'at': at})
        if status == 'severed':
            sever.update({'status': 'mid-flight', 'at': at})
            break
        if status == 'lost':
            record['inconclusive'] = 'the plant\'s step counter ' \
                'dropped the mid-flight watch — the sever staging ' \
                'could not observe the batch'
            return None, violations, record
    record['sever'] = sever
    record['served_base'], record['plant_base'] = (served_base,
                                                 plant_base)
    if sever['status'] != 'mid-flight':
        record['inconclusive'] = 'every severed-client attempt ' \
            'landed after the bound batch finished — the rig ' \
            'outruns the mid-flight staging'
        return None, violations, record

    # The bounded remainder: the dead client's batch must still end —
    # the bound is the cancellation — so the served tick climbs to
    # the baseline plus the bound and stops.
    deadline = time.monotonic() + TERMINATE_DEADLINE
    ceiling, served_final = served_base, None
    while time.monotonic() < deadline:
        tick = _driven_tick(ctx)
        if tick is not None:
            ceiling = max(ceiling, tick)
            if tick >= served_base + SCAN_BATCH_BOUND:
                served_final = tick
                break
        time.sleep(SEVER_POLL)
    record['served_final'], record['served_ceiling'] = (served_final,
                                                      ceiling)

    # The plant's own witness: the field-owning batch's steps must
    # total exactly the bound — then the counter freezes; nothing
    # steps for a dead client once its request ends.
    plant_final, deadline = None, time.monotonic() + TERMINATE_DEADLINE
    while time.monotonic() < deadline:
        tick = _plant_tick(ctx)
        if tick is not None:
            plant_final = tick
            if tick >= plant_base + SCAN_BATCH_BOUND:
                break
        time.sleep(SEVER_POLL)
    record['plant_final'] = plant_final
    if plant_final is None:
        record['inconclusive'] = 'the plant\'s step counter dropped ' \
            'mid-leg — the disconnect witness never answered the ' \
            'remainder watch'
        return None, violations, record

    # The plateau window: both counters hold through the hold — a
    # batch racing on for its dead client keeps them climbing.
    hold = {'served_held': None, 'plant_held': None}
    hold0 = {'served': _driven_tick(ctx), 'plant': _plant_tick(ctx)}
    time.sleep(PLATEAU_HOLD)
    hold1 = {'served': _driven_tick(ctx), 'plant': _plant_tick(ctx)}
    if hold0['served'] is not None and hold1['served'] is not None:
        hold['served_held'] = hold0['served'] == hold1['served']
    if hold0['plant'] is not None and hold1['plant'] is not None:
        hold['plant_held'] = hold0['plant'] == hold1['plant']
    record['plateau'] = {'hold0': hold0, 'hold1': hold1,
                         'served_held': hold['served_held'],
                         'plant_held': hold['plant_held']}

    # The freed worker: a bounded follow-up request must still
    # answer — the batch's end released its submission worker.
    try:
        probe_status, _probe = _scan_call(ctx, PROBE_SCANS)
    except Exception:
        probe_status = None
    record['probe_status'] = probe_status
    _judge_batch_disconnect(record, note)

    # The hand-back: the driven owner demotes onto its configured
    # source, the entry owner's promote preempts the yielded claim,
    # and the three settle to the launch layout with the plant
    # stepping under it again.
    restore = {}
    plant_start = _plant_tick(ctx)
    status, body = _settle_call(ctx['driven'] + '/demote')
    restore['demote_status'], restore['demote_body'] = status, body
    promoted, last = None, None
    deadline = time.monotonic() + BOUND_SETTLE
    while promoted is None and time.monotonic() < deadline:
        status, body = _settle_call(ctx[owner] + '/promote')
        if status == 200:
            promoted = status
        else:
            last = (status, body)
            time.sleep(BOUND_POLL)
    restore['promote_status'], restore['promote_body'] = (promoted,
                                                        last)

    def settled():
        try:
            _scan_call(ctx, 2)
        except Exception:
            pass
        reports = {name: _try_role(ctx, ctx[name])
                   for name in (owner, peer, 'driven')}
        if (reports[owner] or {}).get('role') == 'active' \
                and _tracking_standby(ctx, peer) is not None \
                and _tracking_standby(ctx, 'driven') is not None:
            return reports
        return None

    reports = wait_for(settled, time.monotonic() + BOUND_SETTLE,
                       interval=BOUND_POLL)
    restore['roles'] = {
        'owner': ((reports or {}).get(owner) or {}).get('role'),
        'peer': _sync_state((reports or {}).get(peer)),
        'driven': _sync_state((reports or {}).get('driven'))}
    # The field's stepping resuming under the launch owner is a
    # watched event, not a single read — the role settle can converge
    # inside the owner's first scan period.
    resumed = None
    if isinstance(plant_start, int):
        resumed = wait_for(
            lambda: (lambda tick: tick
                     if isinstance(tick, int) and tick > plant_start
                     else None)(_plant_tick(ctx)),
            time.monotonic() + BOUND_SETTLE, interval=BOUND_POLL)
    restore['plant_resumed'] = resumed is not None
    record['restore'] = restore
    _judge_batch_restore(record, note)

    keys = set(problems)
    digest = {'refusal': 'unprobed', 'severed': 'unstaged',
              'terminated': 'unfinished', 'plant': 'unwatched',
              'worker': 'pinned', 'restored': 'unrestored'}
    if not any(key.startswith('refusal') for key in keys):
        digest['refusal'] = 'named'
    if sever['status'] == 'mid-flight':
        digest['severed'] = 'mid-flight'
    if not any(key in ('batch-unfinished', 'raced-past-bound',
                       'served-climbed') for key in keys):
        digest['terminated'] = 'at-bound'
    if not any(key in ('plant-divergence', 'plant-kept-stepping')
               for key in keys):
        digest['plant'] = 'froze'
    if 'worker-pinned' not in keys:
        digest['worker'] = 'freed'
    if not any(key.startswith('restore') for key in keys):
        digest['restored'] = 'restored'
    record['digest'] = dict(digest)
    record['violations'] = {key: diagnostic
                            for key, (diagnostic, _)
                            in violations.items()}
    return digest, violations, record


def scenario_scan_batch_bound(ctx):
    """Exercise the bounded driven-scan batch contract on the
    deployed rig: POST /scan refuses a scans count past the declared
    bound by name, a client severed mid-batch cannot keep its batch
    running — the served tick and the shared plant's step counter
    both stop at the bound — and the pair's launch roles and the
    field's owner restore."""
    case = Case(
        'scan-batch-bound',
        'Driven /scan bound refuses by name and a severed batch '
        'terminates',
        'on the run\'s driven third peer — launched --standby '
        '--driven so every scan lands inside a POST /scan — a scans '
        'count past the declared per-request bound of '
        + str(SCAN_BATCH_BOUND)
        + ' answers the named refusal before the first scan; then '
        'with the driven peer promoted onto the field so each scan '
        'steps the shared plant, a bound batch posted on a client '
        'severed mid-flight still terminates — the served tick and '
        'the plant\'s step counter both stop at the bound instead of '
        'racing on for the dead client — a follow-up request answers '
        'the freed worker, and the pair\'s launch roles and the '
        'field\'s owner restore; two passes produce identical '
        'digests')
    launch = {}
    try:
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries only one endpoint — the '
                               'pair the batch leg needs is absent')
        # The ownership hand-off the disconnect leg stages rides the
        # demote-then-promote path — the keyed subject (#1058): the
        # deployed pair while the run config keys it, else the
        # lane-staged probe pair with its own plant.
        subject = _keyed_subject(ctx)
        if subject is None:
            return case.finish('inconclusive', 'the deployed pair '
                               'carries no --pair-token and no '
                               'keyed probe pair is staged — the '
                               'ownership hand-off the disconnect '
                               'leg needs cannot be staged')
        if subject is not ctx:
            case.observe('exercised on the lane-staged keyed '
                         'probe pair — the deployed pair runs '
                         'unkeyed')
            ctx = subject
        for action in ('start_driven', 'stop_driven'):
            if ctx.get(action) is None:
                return case.finish('inconclusive', 'the run '
                                   'context carries no ' + action
                                   + ' action — the driven third '
                                   'controller the leg needs is '
                                   'absent')
        if ctx.get('driven') is None or ctx.get('plant') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no driven monitor or plant '
                               'endpoint — the leg\'s witnesses are '
                               'absent')
        for name in ('active', 'standby'):
            try:
                _role(ctx, ctx[name])
            except Exception as exc:
                return case.finish('inconclusive', name + '\'s '
                                   'monitor is unreachable: '
                                   + str(exc)[:200])
        deadline = time.monotonic() + BOUND_SETTLE
        owner = wait_for(lambda: _pair_active(ctx), deadline,
                         interval=BOUND_POLL)
        if owner is None:
            return case.finish('failed', 'no peer reports '
                               'role=active')
        peer = 'standby' if owner == 'active' else 'active'
        if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                    interval=BOUND_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — the settled '
                               'layout the leg restores to was '
                               'never reached')
        launch = {owner: 'active', peer: 'standby'}
        case.observe('field owner: ' + owner + ' (' + ctx[owner]
                     + '); tracking peer: ' + peer + ' ('
                     + ctx[peer] + ')')
        try:
            launched = ctx['start_driven'](owner)
        except Exception as exc:
            return case.finish('inconclusive', 'the driven-peer '
                               'launch never completed: '
                               + str(exc)[:300])
        case.observe('driven peer up: '
                     + str((launched or {}).get('container')))
        if wait_for(lambda: _try_role(ctx, ctx['driven']),
                    time.monotonic() + BOUND_SETTLE,
                    interval=BOUND_POLL) is None:
            return case.finish('inconclusive', 'the driven '
                               'monitor never answered /role')
        try:
            status, _body = _scan_call(ctx, 0)
        except Exception as exc:
            return case.finish('inconclusive', 'the driven '
                               'monitor\'s /scan never answered: '
                               + str(exc)[:200])
        if status in (404, 405):
            return case.finish('inconclusive', 'POST /scan '
                               'answered ' + str(status) + ' — the '
                               'staged run predates the '
                               'driven-scan contract')
        digests = []
        try:
            for number in (1, 2):
                digest, violations, record = _batch_pass(
                    ctx, number, owner, peer, launch)
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'scan-batch-bound-pass-' + str(number) + '.json',
                    record)
                case.evidence('file', ref, 'scan-batch-bound pass '
                              + str(number) + ' — the refusal '
                              'probes, the ownership hand-off, the '
                              'severed batch\'s served-tick and '
                              'plant-counter witnesses, the '
                              'restore, and the normalized digest')
                if record.get('inconclusive'):
                    return case.finish('inconclusive',
                                       record['inconclusive'])
                if violations or digest is None:
                    diagnostic = DIAG_FAILED \
                        if digest is None or any(
                            name == DIAG_FAILED
                            for name, _ in violations.values()) \
                        else DIAG_NONDET
                    return case.finish(
                        'failed', diagnostic + ': ' + '; '.join(
                            detail for _, detail in
                            list(violations.values())[:4]))
                digests.append(digest)
        finally:
            # The launch layout for the cases behind this one: a
            # clean pass restores it by construction; an aborted
            # pass gets the documented hand-back run again,
            # best-effort — demote whichever peer still owns the
            # field (the driven peer included), promote the entry
            # owner — then the driven container is removed.
            try:
                reports = {name: _try_role(ctx, ctx[name])
                           for name in (owner, peer, 'driven')}
                for name, report in reports.items():
                    if name != owner \
                            and (report or {}).get('role') \
                            in ('active', 'promoting'):
                        _settle_call(ctx[name] + '/demote')
                deadline = time.monotonic() + BOUND_SETTLE
                while time.monotonic() < deadline:
                    try:
                        _scan_call(ctx, 2)
                    except Exception:
                        pass
                    if _pair_active(ctx) == owner \
                            and _tracking_standby(ctx, peer) \
                            is not None:
                        break
                    if (_try_role(ctx, ctx[owner]) or {}) \
                            .get('role') == 'standby':
                        _settle_call(ctx[owner] + '/promote')
                    time.sleep(BOUND_POLL)
            except Exception as exc:
                case.observe('cleanup: role restore failed: '
                             + str(exc)[:200])
            try:
                ctx['stop_driven']()
            except Exception:
                pass
        if digests[0] != digests[1]:
            return case.finish(
                'failed', DIAG_NONDET + ': the two passes\' '
                'digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two probe passes, identical digests: '
                     + json.dumps(digests[0], sort_keys=True))
        slipped = _self_check()
        if slipped:
            return case.finish('failed', DIAG_UNCHECKED
                               + ': planted negatives slipped the '
                               'leg\u2019s own audits: '
                               + ', '.join(slipped))
        return case.finish('passed', 'digest '
                           + json.dumps(digests[0], sort_keys=True))
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
