"""The bounded_liveness acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: The bounded-liveness case runs late in the schedule —
# after the failover switch and the earlier field legs, inside the
# unpinned window before the unclaimed-rearm leg's pinned tail: its
# plant container pausing cannot contaminate an earlier case, and
# whichever endpoint owns the field by then keeps it through the
# wedge and the recovery the scenario drives.
RUNS_AFTER = frozenset({'scenario_failover'})


# --------------------------------------------------------------------
# The bounded liveness-report contract (WW-OPS-003's field-confidence
# clause — #991's bounded liveness answer feeding the container
# HEALTHCHECK, exercised per-revision for the defect #1147 fixed):
# GET /health and GET /role serve the store's published liveness
# mirror, refreshed at every report or scan-stamp change, so a scan
# loop parked inside remote field I/O — holding the executor lock
# for the driver's whole request timeout — cannot queue them. The
# leg stages the wedge the liveness regression names: `docker pause`
# on the run's plant container leaves the remote driver's socket
# open but unanswered — never the fencing reconnect the link-loss
# leg's stop/start severs — so each peer's in-flight scan holds for
# the pause's length: far past a healthy answer's milliseconds, yet
# inside the driver's field timeout, so no wedged request completes
# mid-window. Through the hold both peers' /health and /role must
# keep answering inside the declared small bound — the liveness
# mirror's whole point — while /health's last_scan_age_ms grows to
# report the stall it exists to name, and the published-copy reads
# /snapshot and /journal stay at baseline latency. Unpausing lets
# the held exchanges complete: the next finished scan re-stamps the
# mirror, the reported age drops back to scan-cadence freshness, and
# the pair stands at its launch roles. Named diagnostics are
# bounded-liveness-failed for a contract miss — a liveness read
# unanswered or past the bound, the scan age flat or absent, a
# published-copy read degraded, the recovery never landing —
# bounded-liveness-nondeterministic for a role move or two passes
# disagreeing, and bounded-liveness-unchecked when the self-check's
# planted negatives slip the leg's own audits. A run whose monitors
# predate the /health contract, that never settles into a tracking
# pair, or whose pause lever cannot land reports inconclusive.

LIVE_BOUND = 1.0       # the declared small bound a liveness read owes —
                       # the regression's answer bound: ~2ms healthy
                       # against the ~4s stall the shared-lock fetch took
WEDGE_HOLD = 4.0       # the plant freeze the issue stages (~4s) — short
                       # of the remote driver's field timeout so every
                       # wedged request holds, never severs, the scan
WEDGE_OBSERVE = 3.0    # the poll window inside the hold — bounded off
                       # the driver's timeout so no in-flight request
                       # completes while the window samples
LIVE_POLL = 0.5        # the sampling cadence inside the wedge
LIVE_SETTLE = 30       # bound on the pair settling before the leg runs
                       # and on each best-effort restore after it
LIVE_RECOVER = 30      # bound on the post-unpause recovery landing
AGE_WEDGE_FLOOR = 1500  # the scan age in ms the wedge must grow the
                        # report to — fifteen scan cadences, far past
                        # anything a healthy loop reports
AGE_GROW_FLOOR = 500   # the growth in ms the window's first-to-last
                       # reads must show — the wedge reporting itself
AGE_FRESH = 1000       # the re-stamped age in ms a recovered scan
                       # reports — cadence freshness, never the wedge's
                       # thousands
LIVE_PATHS = ('/health', '/role')
COPY_PATHS = ('/snapshot', '/journal?since=0')
DIAG_FAILED = 'bounded-liveness-failed'
DIAG_NONDET = 'bounded-liveness-nondeterministic'
DIAG_UNCHECKED = 'bounded-liveness-unchecked'


def _health_report(body):
    """The decoded /health answer when it is the bounded HealthReport
    the contract serves — live, role, tick, and a completed scan's
    stamp — else None: a run predating the contract answers 404 or an
    unshaped body."""
    if not isinstance(body, dict):
        return None
    if body.get('live') is not True \
            or not isinstance(body.get('role'), str) \
            or not isinstance(body.get('tick'), int) \
            or isinstance(body.get('tick'), bool):
        return None
    age = body.get('last_scan_age_ms')
    if not isinstance(age, int) or isinstance(age, bool):
        return None
    return body


def _bounded_read(name, base, path, bound):
    """One timed GET under the leg's declared client bound — the read
    either answers inside it or the client's own timeout raises, so
    the stall the pre-fix fetch took behind the wedged scan's lock
    hold surfaces as an unanswered read, never a late verdict."""
    read = {'peer': name, 'path': path, 'bound': bound,
            'answered': False}
    started = time.monotonic()
    try:
        status, body = http_json('GET', base + path, timeout=bound)
    except Exception as exc:
        read['error'] = str(exc)[:150]
        return read
    read.update({'answered': True, 'status': status,
                 'elapsed': round(time.monotonic() - started, 4)})
    if isinstance(body, dict):
        for key in ('live', 'role', 'tick', 'last_scan_age_ms'):
            if key in body:
                read[key] = body[key]
    return read


def _judge_wedge(record, note):
    """Audit the wedge window: every /health and /role read on both
    peers answered inside the declared small bound, the reported scan
    age grew on both, the published-copy reads stayed at baseline
    latency, and neither peer's role moved."""
    launch = record.get('launch_roles') or {}
    for read in record.get('wedge') or []:
        label = read.get('peer', '?') + ' GET ' + str(read.get('path'))
        bound = read.get('bound', LIVE_BOUND)
        kind = 'live' if read.get('path') in LIVE_PATHS else 'copy'
        if not read.get('answered'):
            note(kind + '-' + label, DIAG_FAILED,
                 label + ' never answered inside its '
                 + format(bound, '.1f') + 's bound under the wedge'
                 + (' — ' + str(read.get('error'))
                    if read.get('error') else ''))
            continue
        if read.get('status') != 200:
            note(kind + '-' + label, DIAG_FAILED,
                 label + ' answered status ' + str(read.get('status'))
                 + ' under the wedge')
            continue
        if read.get('elapsed') is not None \
                and read['elapsed'] > bound:
            note(kind + '-' + label, DIAG_FAILED,
                 label + ' answered in '
                 + format(read['elapsed'], '.2f') + 's past the '
                 + format(bound, '.1f') + 's bound — the read queued '
                 'behind the wedged scan the liveness mirror exists '
                 'to unmask')
        if read.get('path') == '/health' \
                and read.get('live') is not True:
            note(kind + '-' + label, DIAG_FAILED,
                 label + ' answered without live=true — not the '
                 'liveness declaration the contract serves')
    for name, ages in (record.get('ages') or {}).items():
        present = [age for age in ages
                   if isinstance(age, int)
                   and not isinstance(age, bool)]
        if len(present) != len(ages) or len(present) < 2:
            note('age-' + name, DIAG_FAILED,
                 name + '\'s wedged /health answers never carried '
                 'the completed-scan age the wedge must report: '
                 + json.dumps(ages))
        elif present[-1] - present[0] < AGE_GROW_FLOOR \
                or max(present) < AGE_WEDGE_FLOOR:
            note('age-' + name, DIAG_FAILED,
                 name + '\'s last_scan_age_ms never grew under the '
                 'wedge — the liveness report cannot show the stall '
                 'it exists to name: ' + json.dumps(ages))
    for name, roles in (record.get('roles') or {}).items():
        moved = [role for role in roles
                 if role is not None and role != launch.get(name)]
        if moved:
            note('role-' + name, DIAG_NONDET,
                 name + ' left its launch role '
                 + str(launch.get(name)) + ' under the wedge: '
                 + json.dumps(roles))


def _judge_recovery(record, note):
    """Audit the post-unpause recovery: both peers' liveness reads
    back inside the bound, the reported scan age re-stamped to
    cadence freshness, the published-copy reads back at baseline,
    and the pair standing at its launch roles."""
    launch = record.get('launch_roles') or {}
    for name in launch:
        entry = (record.get('recovery') or {}).get(name)
        if not isinstance(entry, dict):
            note('reage-' + name, DIAG_FAILED,
                 name + '\'s recovery was never probed')
            continue
        if entry.get('re_stamped_age') is None:
            note('reage-' + name, DIAG_FAILED,
                 name + '\'s last_scan_age_ms never re-stamped under '
                 + str(AGE_FRESH) + 'ms after the unpause — the '
                 'wedged scan loop never recovered')
        role = entry.get('role_read') or {}
        if not role.get('answered') or role.get('status') != 200 \
                or (role.get('elapsed') or 0) \
                > role.get('bound', LIVE_BOUND):
            note('relive-' + name, DIAG_FAILED,
                 name + '\'s GET /role never answered inside the '
                 'bound after the unpause')
        elif role.get('role') != launch.get(name):
            note('role-' + name, DIAG_NONDET,
                 name + ' did not return to its launch role '
                 + str(launch.get(name)) + ' after the wedge: '
                 + json.dumps(role.get('role')))
        for path, read in (entry.get('published') or {}).items():
            if not read.get('answered') or read.get('status') != 200 \
                    or (read.get('elapsed') or 0) \
                    > read.get('bound', LATENCY_BOUND):
                note('recopy-' + name + '-' + path, DIAG_FAILED,
                     name + ' GET ' + path + ' never recovered to '
                     'baseline latency after the unpause')


def _self_check():
    """The leg's unchecked-diagnostic self-test: replay each judge
    over the planted negatives it must name — a liveness read
    answered past the bound (the doctored wedge the regression
    names), an unanswered one, a flat scan age, a stalled
    published-copy read, a role moved under the wedge, and a scan
    age that never re-stamps — and require each to trip. A silent
    judge returns the negative names it let through."""
    slipped = []

    def clean():
        def live(name, role, age):
            return {'peer': name, 'path': '/health', 'bound': 1.0,
                    'answered': True, 'status': 200, 'elapsed': 0.002,
                    'live': True, 'role': role, 'tick': 40,
                    'last_scan_age_ms': age}

        def role_read(name, role):
            return {'peer': name, 'path': '/role', 'bound': 1.0,
                    'answered': True, 'status': 200,
                    'elapsed': 0.002, 'role': role}

        def copy_read(name, path):
            return {'peer': name, 'path': path, 'bound': 2.0,
                    'answered': True, 'status': 200, 'elapsed': 0.003}

        record = {'launch_roles': {'active': 'active',
                                   'standby': 'standby'},
                  'wedge': [], 'ages': {}, 'roles': {},
                  'recovery': {}}
        for name, role in (('active', 'active'),
                           ('standby', 'standby')):
            record['wedge'] += [live(name, role, 300),
                                live(name, role, 2100),
                                role_read(name, role)]
            record['wedge'] += [copy_read(name, path)
                                for path in COPY_PATHS]
            record['ages'][name] = [300, 2100]
            record['roles'][name] = [role]
            record['recovery'][name] = {
                're_stamped_age': 120,
                'role_read': role_read(name, role),
                'published': {path: copy_read(name, path)
                              for path in COPY_PATHS}}
        return record

    def expect(name, judge):
        found = []
        judge(lambda key, diagnostic, detail: found.append(key))
        if not found:
            slipped.append(name)

    # The doctored negative: a liveness read asserted as answering
    # inside the bound while it stalls behind the wedge.
    record = clean()
    record['wedge'][0] = dict(record['wedge'][0], elapsed=2.5)
    expect('stalled-live-read', lambda note:
           _judge_wedge(record, note))
    # The same stall with the client bound lapsing first.
    record = clean()
    record['wedge'][1] = {'peer': 'active', 'path': '/role',
                          'bound': 1.0, 'answered': False,
                          'error': 'timed out'}
    expect('unanswered-live-read', lambda note:
           _judge_wedge(record, note))
    # The wedge the report exists to name, unreported.
    record = clean()
    record['ages']['standby'] = [300, 300]
    expect('flat-scan-age', lambda note:
           _judge_wedge(record, note))
    # The published-copy read queuing with the scan.
    record = clean()
    record['wedge'][-1] = {'peer': 'standby',
                           'path': '/journal?since=0', 'bound': 2.0,
                           'answered': False, 'error': 'timed out'}
    expect('stalled-copy-read', lambda note:
           _judge_wedge(record, note))
    # A role moved under the wedge.
    record = clean()
    record['roles']['standby'] = ['standby', 'active']
    expect('moved-role', lambda note:
           _judge_wedge(record, note))
    # The scan loop never recovered after the release.
    record = clean()
    record['recovery']['active']['re_stamped_age'] = None
    expect('wedged-recovery', lambda note:
           _judge_recovery(record, note))
    return slipped


def _leave_clean(ctx, launch, unpause):
    """The exit the rig is owed: release any wedge a pass's own
    release missed — an unpause on a running plant errs harmlessly —
    then walk the pair back to its launch roles."""
    if callable(unpause):
        try:
            unpause()
        except Exception:
            pass
    owner = next((name for name, role in launch.items()
                  if role == 'active'), None)
    peer = next((name for name, role in launch.items()
                 if role == 'standby'), None)
    if owner is None or peer is None:
        return
    try:
        report = _try_role(ctx, ctx[peer])
        if (report or {}).get('role') in ('active', 'promoting'):
            _settle_call(ctx[peer] + '/demote')
            wait_for(lambda: (_try_role(ctx, ctx[peer]) or {})
                     .get('role') == 'standby' or None,
                     time.monotonic() + LIVE_SETTLE,
                     interval=LIVE_POLL)
        if (_try_role(ctx, ctx[owner]) or {}).get('role') != 'active':
            _settle_call(ctx[owner] + '/promote')
            wait_for(lambda: (_try_role(ctx, ctx[owner]) or {})
                     .get('role') == 'active' or None,
                     time.monotonic() + LIVE_SETTLE,
                     interval=LIVE_POLL)
        wait_for(lambda: _tracking_standby(ctx, peer) or None,
                 time.monotonic() + LIVE_SETTLE, interval=LIVE_POLL)
    except Exception:
        pass


def _bounded_pass(ctx, number, owner, peer, launch):
    """One wedge pass: a healthy baseline on every probed surface,
    then the ~4s plant freeze with both peers' /health, /role,
    /snapshot, and /journal polled inside it, the release, and the
    recovery probes — the record `_judge_wedge`/`_judge_recovery`
    audit. Returns (digest, violations, record): digest is the
    pass's normalized verdict, identical across clean passes;
    violations maps each clause key to (diagnostic, detail)."""
    record = {'pass': number, 'owner': owner,
              'launch_roles': dict(launch), 'baseline': {},
              'wedge': [], 'ages': {owner: [], peer: []},
              'roles': {owner: [], peer: []}, 'recovery': {}}
    digest = {'liveness': 'unprobed', 'scan_age': 'unprobed',
              'published': 'unprobed', 'recovery': 'unprobed',
              'roles': 'moved'}
    violations = {}

    def note(key, diagnostic, detail):
        violations.setdefault(key, (diagnostic, detail))

    peers = ((owner, ctx[owner]), (peer, ctx[peer]))

    # The healthy baseline each wedge-window read is timed against —
    # one bounded read of every surface per peer. A rig that cannot
    # show it is not a rig the wedge can convict.
    for name, base in peers:
        for path in LIVE_PATHS + COPY_PATHS:
            bound = LIVE_BOUND if path in LIVE_PATHS else LATENCY_BOUND
            record['baseline'][name + ' ' + path] = _bounded_read(
                name, base, path, bound)
    late = [label for label, read in record['baseline'].items()
            if not read.get('answered') or read.get('status') != 200
            or (read.get('elapsed') or 0) > read.get('bound', 0)]
    if late:
        record['inconclusive'] = 'the pair never showed a healthy ' \
            'baseline to wedge against: ' + ', '.join(sorted(late))
        return None, violations, record
    for name, _ in peers:
        read = record['baseline'][name + ' /health']
        age = read.get('last_scan_age_ms')
        if not isinstance(age, int) or isinstance(age, bool):
            record['inconclusive'] = name + '\'s liveness report ' \
                'carries no completed-scan stamp — nothing to ' \
                'watch grow under the wedge'
            return None, violations, record

    try:
        ctx['pause_plant']()
    except Exception as exc:
        record['inconclusive'] = 'the plant pause action never ' \
            'landed — the wedged field connection cannot be ' \
            'staged: ' + str(exc)[:200]
        return None, violations, record
    paused_at = time.monotonic()
    record['hold'] = {'hold_s': WEDGE_HOLD, 'observed_s': WEDGE_OBSERVE}
    try:
        deadline = paused_at + WEDGE_OBSERVE
        while time.monotonic() < deadline:
            for name, base in peers:
                for path in LIVE_PATHS:
                    read = _bounded_read(name, base, path, LIVE_BOUND)
                    record['wedge'].append(read)
                    if path == '/health':
                        record['ages'][name].append(
                            read.get('last_scan_age_ms'))
                    else:
                        record['roles'][name].append(
                            read.get('role'))
                for path in COPY_PATHS:
                    record['wedge'].append(_bounded_read(
                        name, base, path, LATENCY_BOUND))
            time.sleep(LIVE_POLL)
    finally:
        # The full ~4s hold stands even on an aborted pass — then
        # the field is owed its release either way.
        while time.monotonic() < paused_at + WEDGE_HOLD:
            time.sleep(0.05)
        try:
            ctx['unpause_plant']()
            record['unpaused'] = True
        except Exception as exc:
            record['unpaused'] = False
            record['inconclusive'] = 'the plant unpause action ' \
                'never landed: ' + str(exc)[:200]

    # The recovery: the resumed exchanges complete the wedged scans,
    # the next finished scan re-stamps the mirror to cadence
    # freshness, and the liveness and published-copy reads sit back
    # at their baseline.
    for name, base in peers:
        entry = {'watch': []}

        def fresh(name=name, base=base, entry=entry):
            read = _bounded_read(name, base, '/health', LIVE_BOUND)
            entry['watch'].append(read)
            age = read.get('last_scan_age_ms')
            if read.get('answered') and read.get('status') == 200 \
                    and isinstance(age, int) \
                    and not isinstance(age, bool) \
                    and age < AGE_FRESH:
                return age
            return None
        entry['re_stamped_age'] = wait_for(
            fresh, time.monotonic() + LIVE_RECOVER,
            interval=LIVE_POLL)
        entry['role_read'] = _bounded_read(name, base, '/role',
                                           LIVE_BOUND)
        entry['published'] = {
            path: _bounded_read(name, base, path, LATENCY_BOUND)
            for path in COPY_PATHS}
        record['recovery'][name] = entry

    problems = []

    def collect(key, diagnostic, detail):
        problems.append((key, diagnostic, detail))
    _judge_wedge(record, collect)
    _judge_recovery(record, collect)
    keys = {key for key, _, _ in problems}
    for key, diagnostic, detail in problems:
        note(key, diagnostic, detail)
    if not any(key.startswith('live') for key in keys):
        digest['liveness'] = 'bounded'
    if not any(key.startswith('age') for key in keys):
        digest['scan_age'] = 'grew'
    if not any(key.startswith('copy') for key in keys):
        digest['published'] = 'baseline'
    if not any(key.startswith('re') for key in keys):
        digest['recovery'] = 'resumed'
    if not any(key.startswith('role') for key in keys):
        digest['roles'] = 'held'
    record['digest'] = dict(digest)
    record['violations'] = {key: diagnostic
                            for key, (diagnostic, _)
                            in violations.items()}
    return digest, violations, record


def scenario_bounded_liveness(ctx):
    """Wedge the settled pair's remote-driver field connection —
    `docker pause` on the run's plant container holds every socket
    open but unanswered — and prove both peers' liveness reads stay
    inside the declared bound while the reported scan age grows and
    the published-copy reads hold baseline latency; then release and
    prove recovery."""
    case = Case('bounded-liveness',
                'Bounded liveness reports under a wedged field '
                'connection',
                'with the deployed pair settled, pausing the run\'s '
                'plant container — the remote driver\'s socket held '
                'open but unanswered for ~' + format(WEDGE_HOLD,
                                                     '.0f')
                + 's, inside its field timeout — leaves GET /health '
                'and GET /role answering inside the declared '
                + format(LIVE_BOUND, '.1f') + 's bound on both '
                'peers, /health reporting a growing '
                'last_scan_age_ms, and the published-copy reads GET '
                '/snapshot and GET /journal at baseline latency; '
                'unpausing restores baseline latency and scan '
                'freshness with the pair at its launch roles; two '
                'consecutive passes produce identical digests')
    launch = {}
    unpause = None
    try:
        pause, unpause = ctx.get('pause_plant'), ctx.get('unpause_plant')
        if not callable(pause) or not callable(unpause):
            return case.finish('inconclusive', 'the run context '
                               'carries no plant pause/unpause '
                               'action — the wedged field connection '
                               'cannot be staged')
        deadline = time.monotonic() + LIVE_SETTLE
        owner = wait_for(lambda: _pair_active(ctx), deadline,
                         interval=LIVE_POLL)
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
                    interval=LIVE_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — the settled '
                               'layout the leg restores to was '
                               'never reached')
        launch = {owner: 'active', peer: 'standby'}
        case.observe('field owner: ' + owner + ' (' + ctx[owner]
                     + '); tracking peer: ' + peer + ' ('
                     + ctx[peer] + ')')
        # The contract gate: /health must serve the bounded
        # HealthReport — a staged run predating it answers 404 or an
        # unshaped body, and the leg reports inconclusive rather
        # than a verdict.
        for name in (owner, peer):
            try:
                _, body = http_json('GET', ctx[name] + '/health',
                                    timeout=LIVE_BOUND)
            except Exception as exc:
                return case.finish('inconclusive', name + '\'s '
                                   'monitor never answered /health: '
                                   + str(exc)[:200] + ' — the '
                                   'staged run predates the bounded '
                                   'liveness contract')
            if _health_report(body) is None:
                return case.finish('inconclusive', name + '\'s '
                                   '/health answer is not the '
                                   'bounded HealthReport — the '
                                   'staged run predates the '
                                   'liveness contract: '
                                   + json.dumps(body)[:200])
        digests = []
        for number in (1, 2):
            digest, violations, record = _bounded_pass(
                ctx, number, owner, peer, launch)
            ref = save_evidence(
                ctx['evidence_dir'],
                'bounded-liveness-pass-' + str(number) + '.json',
                record)
            case.evidence('file', ref, 'bounded-liveness pass '
                          + str(number) + ' — the baseline, the '
                          'wedged window\'s timed reads and '
                          'scan-age growth, the recovery probes, '
                          'and the normalized digest')
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
        if digests[0] != digests[1]:
            return case.finish(
                'failed', DIAG_NONDET + ': the two passes\' '
                'digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two wedge passes, identical digests: '
                     + json.dumps(digests[0], sort_keys=True))
        slipped = _self_check()
        if slipped:
            return case.finish('failed', DIAG_UNCHECKED
                               + ': planted negatives slipped the '
                               'leg\u2019s own audits: '
                               + ', '.join(slipped))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
    finally:
        _leave_clean(ctx, launch, unpause)
