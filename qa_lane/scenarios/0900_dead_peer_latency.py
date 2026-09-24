"""The dead_peer_latency acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: The dead-peer-latency case sits in the same restored window: it
# isolates ctrl-a — the source ctrl-b and its driven third peer pull
# from — restores it before the armed failover bound, and removes the
# driven peer, so the launch roles still hold for the cases that
# follow.
RUNS_BEFORE = frozenset({'scenario_parameter_tune_carryover'})

LATENCY_POLL = 0.4       # the sampling cadence inside the window
LATENCY_WINDOW = 8.0     # the dead-peer sampling window — inside the
                         # armed failover bound (~12s at
                         # failover_misses=120 on a 100ms scan)
LATENCY_BATCH_SCANS = 3  # driven scans per mid-window batch; each pull
                         # waits the documented CHECKPOINT_PULL_TIMEOUT
LATENCY_HEALTHY_SCANS = 64  # the large batch posted against the
                            # reconverged pair — its pulls answer, so
                            # it occupies its worker for a stretch
LATENCY_GRACE = 5.0      # the page's CONVERGENCE_GRACE — an
                         # unsynchronized peer past it is a named fault
LATENCY_SERVE_DEADLINE = 30   # bound on the driven monitor answering
LATENCY_SETTLE_DEADLINE = 60  # bound on the pair reconverging


def _pair_view(*keys):
    """The health view the case grades: one entry per peer the page
    would be configured with — its ctx endpoint key plus the
    unsynchronized-age clock pairHealth's noteSyncAge keeps."""
    return [{'key': key, 'report': None, 'error': None,
             'unsynced_since': None} for key in keys]


def _pair_poll(ctx, view):
    """One /role poll per peer in the view — the pair view's role
    cadence. A failed poll is recorded as pair health (the peer's
    name is its ctx key); a report of 'unsynchronized' starts or
    continues the grace clock, any converged state or the
    unreachable fault ends it."""
    for entry in view:
        try:
            entry['report'] = _role(ctx, ctx[entry['key']])
            entry['error'] = None
        except Exception as exc:
            entry['error'] = str(exc)[:200]
        if entry['error'] is None and entry['report'] is not None \
                and entry['report'].get('sync') == 'unsynchronized':
            entry['unsynced_since'] = (entry['unsynced_since']
                                       or time.monotonic())
        else:
            entry['unsynced_since'] = None


def _pair_health(view):
    """The page's pairHealth verdict over the view's last poll — the
    surface the deploy shows. Each failed poll names its peer
    unreachable; each peer reporting 'unsynchronized' for longer than
    the declared grace is named still not converged; any degraded or
    diverged sync is named by the peer reporting it. The pair is
    healthy only when exactly one peer reports active and no other
    faults exist — unreachable, unsynchronized, or diverged all fail
    healthy."""
    faults, actives = [], []
    for entry in view:
        name = entry['key']
        if entry['error'] is not None:
            was = (entry['report'] or {}).get('role')
            faults.append((str(was) + ' ' if was else '')
                          + name + ' unreachable')
            continue
        report = entry['report']
        if report is None:
            continue
        if report.get('role') == 'active':
            actives.append(name)
        sync = report.get('sync')
        if sync == 'unsynchronized' \
                and entry['unsynced_since'] is not None \
                and time.monotonic() - entry['unsynced_since'] \
                >= LATENCY_GRACE:
            faults.append(name + ' has not converged: unsynchronized '
                          'past the convergence grace')
        if isinstance(sync, dict):
            if 'degraded' in sync:
                faults.append(name + ' sync degraded: '
                              + str((sync['degraded'] or {})
                                    .get('detail')))
            if 'diverged' in sync:
                mismatches = (sync['diverged'] or {}) \
                    .get('mismatches') or []
                faults.append(name + ' standby diverged: staged '
                              'outputs mismatch the field at '
                              + ', '.join(str(m.get('point'))
                                          for m in mismatches))
    if not actives:
        faults.append('no peer reports role active')
    elif len(actives) > 1:
        faults.append('dual-active: ' + ', '.join(actives)
                      + ' report role active')
    return {'active': actives[0] if len(actives) == 1 else None,
            'faults': faults}


def scenario_dead_peer_latency(ctx):
    """Isolate the checkpoint source the tracking peer pulls from:
    the surviving standby's monitor and a driven third peer's batched
    /scan must keep answering inside the declared bound, the
    pair-health surface must name the redundancy faults the window
    leaves, and the pair must reconverge once the source returns."""
    case = Case(
        'dead-peer-latency',
        'Monitor stays bounded under dead-peer pulls and batched '
        'scans',
        'stopping the peer the tracking standby pulls checkpoints '
        'from leaves every sampled read on the surviving monitor and '
        'one receipted command answering inside the declared '
        + str(LATENCY_BOUND) + 's bound across the dead-peer window, '
        'a driven POST /scan batch on the third peer occupying only '
        'its own worker while its per-scan pulls wait the documented '
        'bound, the pair-health verdict naming the unreachable, '
        'degraded, and unsynchronized-past-grace faults rather than '
        'serving healthy, and the pair reconverging once the source '
        'returns — a large batch against the healthy pair leaving '
        'the sampled endpoints just as responsive')
    try:
        stop = ctx.get('stop_controller')
        start = ctx.get('start_controller')
        start_driven = ctx.get('start_driven')
        stop_driven = ctx.get('stop_driven')
        driven_base = ctx.get('driven')
        if stop is None or start is None or start_driven is None \
                or stop_driven is None or driven_base is None:
            return case.finish(
                'inconclusive', 'the run context carries no '
                'controller stop/start or driven-peer action — the '
                'dead-peer induction has no documented seam')
        owner = wait_for(lambda: _settled_active(ctx),
                         time.monotonic() + 30)
        if owner is None:
            return case.finish('failed', 'no peer reports '
                               'role=active')
        if owner not in ('active', 'standby'):
            return case.finish(
                'inconclusive', 'the field owner is ' + owner
                + ' — outside the launched pair the stop action '
                'names')
        peer = 'standby' if owner == 'active' else 'active'
        base = ctx[peer]
        case.observe('seam=source-stop: stop ' + owner + ' ('
                     + ctx[owner] + '), sample ' + peer + ' (' + base
                     + '), driven batch on ' + driven_base)

        # The tracking baseline: the survivor is the peer pulling
        # the owner's checkpoints — the fetch this induction kills.
        def tracking():
            report = _try_role(ctx, base)
            if (report or {}).get('role') == 'standby':
                return _tracking_aligned(report)
            return None

        if wait_for(tracking, time.monotonic() + 45,
                    interval=LATENCY_POLL) is None:
            return case.finish(
                'inconclusive', 'the surviving peer is not a '
                'tracking standby — the dead-peer induction has no '
                'observation point')
        _, signals = http_json('GET', base + '/signals')
        target = _writable_bool_point(signals)
        if target is None:
            return case.finish('inconclusive', 'no writable bool '
                               'point in the model')
        point = target['point']

        # The driven survivor: the run's third monitor, --standby on
        # the same source but externally paced — a batch on it is the
        # per-request pull chain. Fresh and never driven, it reports
        # unsynchronized: the convergence-grace clock the pair health
        # surface reads.
        try:
            launched = start_driven(owner)
        except Exception as exc:
            return case.finish(
                'inconclusive', 'the driven-peer launch never '
                'completed: ' + str(exc)[:300])
        case.observe('driven peer up: '
                     + str((launched or {}).get('container')))
        driven = wait_for(lambda: _try_role(ctx, driven_base),
                          time.monotonic() + LATENCY_SERVE_DEADLINE,
                          interval=LATENCY_POLL)
        if driven is None:
            return case.finish('inconclusive', 'the driven monitor '
                               'never answered /role')
        view = _pair_view(owner, peer, 'driven')
        _pair_poll(ctx, view)   # the grace clock starts here

        # Isolate: the peer both survivors pull from. The container
        # stop is the lane's partition convention — the fetch path
        # goes dead while every survivor stays serving.
        try:
            stop(owner)
        except Exception as exc:
            return case.finish(
                'inconclusive', 'the source-stop induction never '
                'completed: ' + str(exc)[:300])
        case.observe('checkpoint source ' + owner + ' stopped')

        stats = {}        # endpoint -> {'samples': n, 'within': n}
        breaches = []     # named bound violations, for the detail
        command_receipt = None
        grace_fault = None
        batch = {'thread': None, 'result': None, 'error': None,
                 'scans': LATENCY_BATCH_SCANS}
        promoted = False

        def run_batch(scans):
            try:
                batch['result'] = http_json(
                    'POST', driven_base + '/scan',
                    {'scans': scans},
                    timeout=scans * 2 + 15)
            except Exception as exc:
                batch['error'] = exc

        def sample(key, path):
            label = key + ' ' + path
            try:
                _body, elapsed = _timed(
                    lambda: http_json('GET', ctx[key] + path,
                                      timeout=LATENCY_BOUND))
            except Exception as exc:
                breaches.append(label + ' never answered inside the '
                                'bound: ' + str(exc)[:150])
                return
            entry = stats.setdefault(label,
                                     {'samples': 0, 'within': 0})
            entry['samples'] += 1
            if elapsed <= LATENCY_BOUND:
                entry['within'] += 1
            else:
                breaches.append(label + ' answered in '
                                + format(elapsed, '.2f') + 's past '
                                'the ' + str(LATENCY_BOUND)
                                + 's bound')

        # The dead-peer window: poll the health view (the grace
        # clock keeps running while the fetch path is dead), post
        # the driven batch once the unsynchronized fault is named,
        # and sample every endpoint on every pass — each read and
        # the one receipted command must answer inside the bound.
        deadline = time.monotonic() + LATENCY_WINDOW
        while time.monotonic() < deadline:
            _pair_poll(ctx, view)
            health = _pair_health(view)
            if grace_fault is None:
                grace_fault = next(
                    (f for f in health['faults']
                     if 'unsynchronized' in f and 'grace' in f), None)
            if grace_fault is not None and batch['thread'] is None:
                batch['thread'] = threading.Thread(
                    target=run_batch, args=(LATENCY_BATCH_SCANS,),
                    daemon=True)
                batch['thread'].start()
            for path in ('/role', '/snapshot', '/journal?since=0'):
                sample(peer, path)
                if batch['thread'] is not None \
                        and batch['thread'].is_alive():
                    sample('driven', path)
            if command_receipt is None:
                try:
                    (status, receipt), elapsed = _timed(
                        lambda: http_json(
                            'POST', base + '/command',
                            {'command': {'write_value': {
                                'point': point, 'kind': 'bool',
                                'value': {'bool': True}}},
                             'actor': 'qa-lane'},
                            timeout=LATENCY_BOUND))
                    command_receipt = {
                        'status': status, 'elapsed': elapsed,
                        'outcome': _outcome_key(receipt)}
                    if status != 200:
                        breaches.append('the window command answered '
                                        + str(status))
                except Exception as exc:
                    command_receipt = {'status': None, 'outcome': 'lost'}
                    breaches.append('the window command raised '
                                    + str(exc)[:150])
            survivor_role = next(
                (e['report'] for e in view if e['key'] == peer), None)
            if (survivor_role or {}).get('role') \
                    in ('promoting', 'active'):
                promoted = True
            if grace_fault and command_receipt \
                    and batch['thread'] is not None \
                    and not batch['thread'].is_alive() \
                    and all(v['within'] for v in stats.values()):
                break
            time.sleep(LATENCY_POLL)
        if promoted:
            case.observe('the surviving standby promoted to active '
                         'mid-window — reads and the command stayed '
                         'bounded through the failover')
        if batch['thread'] is not None:
            batch['thread'].join(timeout=LATENCY_BATCH_SCANS * 2 + 15)
        batch_result = batch['result']
        batch_status = batch_result[0] \
            if isinstance(batch_result, tuple) else None
        ref = save_evidence(
            ctx['evidence_dir'], 'dead-peer-latency-window.json',
            {'bound_seconds': LATENCY_BOUND,
             'endpoints': {label: {'answered': entry['samples'] > 0,
                                   'within_bound':
                                   entry['within'] == entry['samples']}
                           for label, entry in stats.items()},
             'command': {'status': command_receipt['status'],
                         'outcome': command_receipt['outcome']}
             if command_receipt else None,
             'grace_fault': grace_fault,
             'batch': {'scans': batch['scans'], 'status': batch_status,
                       'completed': batch['thread'] is not None
                       and not batch['thread'].is_alive()},
             'survivor_promoted': promoted})
        case.evidence('file', ref, 'the dead-peer window: bounded '
                      'samples, the receipted command, the named '
                      'grace fault, the driven batch')

        # Restore the path: the survivor's fetch thread is still
        # running — the next pull re-links the chain.
        try:
            start(owner)
        except Exception as exc:
            return case.finish(
                'inconclusive', 'the source restart never completed: '
                + str(exc)[:300])
        case.observe('checkpoint source ' + owner + ' restarted')

        # The driven peer's own pulls bring it into the pair: one
        # batch past the reconverged source lands it tracking.
        def owner_serving():
            return _try_role(ctx, ctx[owner]) is not None

        if wait_for(owner_serving,
                    time.monotonic() + LATENCY_SETTLE_DEADLINE,
                    interval=LATENCY_POLL) is None:
            return case.finish('failed', 'the checkpoint source '
                               'never served again after restart')
        try:
            http_json('POST', driven_base + '/scan',
                      {'scans': LATENCY_BATCH_SCANS},
                      timeout=LATENCY_BATCH_SCANS * 2 + 15)
        except Exception as exc:
            return case.finish('failed', 'the reconvergence batch '
                               'on the driven peer failed: '
                               + str(exc)[:200])

        def reconverged():
            _pair_poll(ctx, view)
            health = _pair_health(view)
            return health if not health['faults'] else None

        healthy = wait_for(reconverged,
                           time.monotonic() + LATENCY_SETTLE_DEADLINE,
                           interval=LATENCY_POLL)

        def sync_state(entry):
            """The named convergence state a /role report carries —
            the evidence's deterministic shape, never the tick-bearing
            payload."""
            sync = ((entry.get('report') or {}).get('sync'))
            if isinstance(sync, dict):
                return next(iter(sync), 'unknown')
            return sync

        ref = save_evidence(
            ctx['evidence_dir'], 'dead-peer-latency-health.json',
            {'faults': (healthy or {}).get('faults'),
             'roles': {e['key']: (e['report'] or {}).get('role')
                       for e in view},
             'syncs': {e['key']: sync_state(e) for e in view}})
        case.evidence('file', ref, 'the pair-health verdict after '
                      'reconvergence — every window fault cleared')
        if healthy is None:
            return case.finish('failed', 'the pair never '
                               'reconverged after the path returned')

        # The healthy-pair batch: the large driven batch while the
        # pull path answers — the sampled endpoints must stay inside
        # the bound while the batch occupies its worker.
        healthy_batch = {'thread': None, 'result': None,
                         'error': None}

        def run_healthy():
            try:
                healthy_batch['result'] = http_json(
                    'POST', driven_base + '/scan',
                    {'scans': LATENCY_HEALTHY_SCANS},
                    timeout=LATENCY_HEALTHY_SCANS * 2 + 15)
            except Exception as exc:
                healthy_batch['error'] = exc

        healthy_batch['thread'] = threading.Thread(
            target=run_healthy, daemon=True)
        healthy_batch['thread'].start()
        healthy_stats = {}
        healthy_deadline = time.monotonic() + LATENCY_WINDOW \
            + LATENCY_HEALTHY_SCANS * 2 + 15
        while healthy_batch['thread'].is_alive() \
                and time.monotonic() < healthy_deadline:
            for key in (peer, 'driven'):
                for path in ('/role', '/snapshot', '/journal?since=0'):
                    label = key + ' ' + path
                    try:
                        _body, elapsed = _timed(
                            lambda: http_json('GET', ctx[key] + path,
                                              timeout=LATENCY_BOUND))
                    except Exception as exc:
                        breaches.append('healthy-batch ' + label
                                        + ' raised '
                                        + str(exc)[:150])
                        continue
                    entry = healthy_stats.setdefault(
                        label, {'samples': 0, 'within': 0})
                    entry['samples'] += 1
                    if elapsed <= LATENCY_BOUND:
                        entry['within'] += 1
                    else:
                        breaches.append('healthy-batch ' + label
                                        + ' answered in '
                                        + format(elapsed, '.2f')
                                        + 's past the bound')
            time.sleep(LATENCY_POLL)
        healthy_batch['thread'].join(
            timeout=LATENCY_HEALTHY_SCANS * 2 + 15)
        healthy_result = healthy_batch['result']
        healthy_status = healthy_result[0] \
            if isinstance(healthy_result, tuple) else None
        ref = save_evidence(
            ctx['evidence_dir'],
            'dead-peer-latency-healthy-batch.json',
            {'scans': LATENCY_HEALTHY_SCANS,
             'status': healthy_status,
             'completed': not healthy_batch['thread'].is_alive(),
             'endpoints': {label: {'answered': e['samples'] > 0,
                                   'within_bound':
                                   e['within'] == e['samples']}
                           for label, e in healthy_stats.items()}})
        case.evidence('file', ref, 'the healthy-pair batch — the '
                      'sampled endpoints stayed inside the bound '
                      'while it ran')

        # Teardown the third peer; the rig's pair is what later
        # cases see.
        try:
            stop_driven()
        except Exception as exc:
            return case.finish(
                'inconclusive', 'the driven-peer teardown never '
                'completed: ' + str(exc)[:300])

        if grace_fault is None:
            return case.finish(
                'failed', 'the isolation never produced the named '
                'unsynchronized-past-grace fault — the health '
                'surface never named the still-unsynchronized '
                'standby')
        if command_receipt is None \
                or command_receipt['status'] != 200:
            return case.finish(
                'failed', 'the dead-peer window never produced the '
                'receipted command answer')
        if command_receipt['outcome'] == 'unknown':
            return case.finish(
                'failed', 'the window command settled with no '
                'named verdict: ' + json.dumps(command_receipt))
        if command_receipt['outcome'] == 'rejected:queue_full':
            return case.finish(
                'failed', 'the window command hit an admission '
                'rejection: rejected:queue_full — the queueing the '
                'dead-peer fix removes')
        if batch['error'] is not None or batch_status != 200:
            return case.finish(
                'failed', 'the mid-window driven batch starved: '
                + str(batch['error'] or batch_status)[:300])
        if healthy_batch['error'] is not None \
                or healthy_status != 200:
            return case.finish(
                'failed', 'the healthy-pair batch starved: '
                + str(healthy_batch['error']
                      or healthy_status)[:300])
        missing = [label for label, entry in stats.items()
                   if not entry['samples']] \
            + [label for label, entry in healthy_stats.items()
               if not entry['samples']]
        if missing:
            return case.finish(
                'failed', 'no sample ever landed on '
                + ', '.join(sorted(missing)))
        if breaches:
            return case.finish(
                'failed', 'a sampled request starved behind the '
                'peer waits: ' + '; '.join(breaches[:3])
                + (' ...' if len(breaches) > 3 else ''))
        case.observe('pair-health verdict: ' + grace_fault)
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
