"""The remote_driver_recovery acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the remote-driver-recovery case reuses the link-loss
# leg's staging — the runner-owned plant stop/start actions cycle the
# field container mid-run — and follows that leg inside its declared
# window: its container cycling cannot contaminate an earlier case,
# and whichever endpoint owns the field by then keeps it through the
# staged interruptions and recoveries the leg drives.
RUNS_AFTER = frozenset({'scenario_plant_link_loss'})

# --------------------------------------------------------------------
# The remote driver's standing-failure recovery contract (WW-OPS-003's
# served-I/O-health clause — the per-revision lane evidence for #990's
# settled behavior, ahead of HQ-5's hardware link-loss checks): the
# driver diagnostics the snapshot's io_health section serves describe
# the link as it is, not the worst thing that ever happened to it.
# While the field owner's plant link is interrupted the served backend
# diagnostics report `disconnected` with the severing failure named in
# `last_error` and io_health reports the backend degraded — the
# consecutive-failure streak advancing and the boundary fault
# recorded; on the link's return the *first* successful exchange
# clears the standing record, so a serve reporting `connected` already
# carries no last_error — the record must never linger behind a
# now-healthy backend — while the cumulative counters and the recorded
# fault keep the outage's history. The leg drives the runner's
# stop/start plant actions the link-loss case owns, twice: the second
# staged interruption must reproduce the first's verdicts exactly,
# and the pair's roles and the served tick's advance hold throughout.
# Functional misses on the served-health contract name
# remote-driver-recovery-failed; verdicts on the run's own determinism
# — a served tick that rewinds, two cycles whose digests disagree —
# name remote-driver-recovery-nondeterministic. A rig that is
# unreachable, that predates the served driver-diagnostics surface, or
# whose run context lacks the link-staging lever reports inconclusive.

REMOTE_RECOVERY_SETTLE = 30    # bound on the pair reporting settled
REMOTE_RECOVERY_POLL = 0.25    # cadence watching the link mid-cycle
REMOTE_RECOVERY_DEGRADE = 45   # bound on the outage surfacing degraded
REMOTE_RECOVERY_RESTORE = 60   # bound on the plant's return + re-attach


def _driver_health(snapshot):
    """The served io_health's backend driver diagnostics — the
    DriverDiagnostics object the remote backend volunteers — or None
    when the snapshot carries no reporting backend."""
    health = (snapshot or {}).get('io_health') or {}
    driver = health.get('driver')
    return driver if isinstance(driver, dict) else None


def _plant_serving(ctx):
    """Whether the run's plant answers a probe right now — True or
    False, or None when the run carries no probe seam to ask with.
    The staged-link checks classify 'the lever never bit' apart from
    'the served surface never reported' off this."""
    if ctx.get('plant_ctl') is not None:
        return _try_plant_ctl(ctx, 'list') is not None
    if ctx.get('plant'):
        return _try_plant(ctx, {'op': 'list_points'}) is not None
    return None


def _remote_driver_cycle(ctx, number, active, peer, stop, start):
    """One staged interruption-and-restore of the field owner's plant
    link: `stop` severs it until the served backend diagnostics name
    the standing failure and io_health counts the degraded boundary,
    `start` returns the plant, and the first serve reporting the link
    connected must already carry the cleared record. Returns
    (digest, violations, evidence): the digest is the cycle's
    normalized verdict — identical across clean cycles; violations is
    {key: (diagnostic, detail)} in first-seen order. Rig states the
    contract cannot answer for — the lever never completing, the plant
    never returning, the served surface predating the diagnostics —
    raise for the caller's inconclusive verdict."""
    base, peer_base = ctx[active], ctx[peer]
    expected = {active: 'active', peer: 'standby'}
    violations = {}
    evidence = {'cycle': number}
    state = {'peak_tick': None}
    marks = {'interrupted': None, 'degraded': None, 'cleared': None,
             'streak': None, 'history': None, 'roles': 'held',
             'cadence': 'held'}
    stopped = False

    def note(key, diagnostic, detail):
        violations.setdefault(key, (diagnostic, detail))

    def failed(key, detail):
        note(key, 'remote-driver-recovery-failed', detail)

    def digest():
        return {key: marks[key] if marks[key] is not None
                else 'unreached' for key in sorted(marks)}

    def watch_roles():
        """One poll of both peers' /role: a moved role is the pair's
        stability contract breaking — the cycle's watches end on it."""
        for name, url in ((active, base), (peer, peer_base)):
            report = _try_role(ctx, url)
            if report is not None and report.get('role') \
                    != expected[name]:
                marks['roles'] = 'moved'
                failed('role-' + name, name + ' reported role '
                       + str(report.get('role')) + ' through the '
                       'staged interruption — the pair\'s launch '
                       'roles did not hold: '
                       + json.dumps(report)[:300])
                return False
        return True

    def watch_tick(snap):
        """Served-tick monotonicity across the cycle: a rewind is the
        tick-domain signature of a controller restart, which the
        nondeterminism diagnostic names."""
        tick = snap.get('tick')
        if not isinstance(tick, int):
            return True
        peak = state['peak_tick']
        if peak is not None and tick < peak:
            marks['cadence'] = 'rewound'
            note('tick-rewound',
                 'remote-driver-recovery-nondeterministic',
                 'the served tick rewound through the staged cycle — '
                 + str(tick) + ' under the running peak ' + str(peak)
                 + ': a controller restarted instead of riding the '
                 'link loss out')
            return False
        state['peak_tick'] = tick if peak is None else max(peak, tick)
        return True

    def watch(match, bound):
        """One watch loop shared by the cycle's two halves: poll the
        field owner's snapshot until `match(health, driver)` holds,
        the roles move, the tick rewinds, or `bound` seconds pass.
        Returns (trace, hit, tail_silent) — hit is the matched
        snapshot, tail_silent marks the monitor having died
        mid-watch."""
        trace = []
        hit = None
        silent = 0
        deadline = time.monotonic() + bound
        while time.monotonic() < deadline:
            if not watch_roles():
                break
            snap = _try_snapshot(ctx, base)
            if snap is None:
                silent += 1
                if silent >= 3:
                    break  # the monitor is gone — the run aborted
                time.sleep(REMOTE_RECOVERY_POLL)
                continue
            silent = 0
            if not watch_tick(snap):
                break
            health = snap.get('io_health') or {}
            driver = _driver_health(snap) or {}
            trace.append({'tick': snap.get('tick'),
                          'link': driver.get('link'),
                          'last_error': driver.get('last_error'),
                          'consecutive':
                              health.get('consecutive_failures'),
                          'failed_reads': health.get('failed_reads'),
                          'failed_writes': health.get('failed_writes'),
                          'fault': health.get('last_error')})
            if match(health, driver):
                hit = snap
                break
            time.sleep(REMOTE_RECOVERY_POLL)
        return trace, hit, silent >= 3

    try:
        # The cycle's baseline: the settled healthy serve the
        # interruption runs from — the backend's connected link
        # carrying no standing record. A last_error already standing
        # behind a healthy link is the contract's miss caught before
        # the staging even runs.
        snap = _try_snapshot(ctx, base)
        health = (snap or {}).get('io_health')
        driver = _driver_health(snap)
        evidence['baseline_health'] = health
        if snap is None:
            raise ConnectionError('the field owner\'s monitor never '
                                  'answered the cycle\'s baseline '
                                  'snapshot')
        if not isinstance(health, dict) or driver is None \
                or 'link' not in driver or 'last_error' not in driver:
            raise ConnectionError('the served io_health carries no '
                                  'backend driver diagnostics — the '
                                  'rig predates the contract the leg '
                                  'measures')
        if driver.get('link') != 'connected':
            raise ConnectionError('the backend link reports '
                                  + str(driver.get('link'))
                                  + ' ahead of cycle ' + str(number)
                                  + ' — the rig never presented the '
                                  'healthy baseline')
        if driver.get('last_error'):
            failed('baseline-lingered', 'the served backend '
                   'diagnostics carry a standing last_error behind a '
                   'healthy link ahead of the staged interruption — '
                   'the record never cleared on the exchanges since '
                   'it stood: ' + str(driver.get('last_error'))[:300])
            evidence['digest'] = digest()
            return evidence['digest'], violations, evidence
        tick0 = snap.get('tick')
        evidence['baseline_tick'] = tick0

        # The staged interruption: the runner's plant stop severs the
        # field owner's remote-driver link mid-run. The watch ends on
        # the served io_health reporting the backend degraded — the
        # link disconnected, the severing failure named, the boundary
        # streak and the recorded fault counting.
        try:
            stop()
        except Exception as exc:
            raise ConnectionError('the link-staging stop lever '
                                  'never completed: '
                                  + str(exc)[:300])
        stopped = True
        marks['interrupted'] = 'severed'

        def degraded(health, driver):
            return driver.get('link') == 'disconnected' \
                and bool(driver.get('last_error')) \
                and (health.get('consecutive_failures') or 0) > 0 \
                and health.get('last_error')

        outage, snap, tail_silent = watch(degraded,
                                        REMOTE_RECOVERY_DEGRADE)
        evidence['outage'] = outage
        evidence['degraded'] = (snap or {}).get('io_health')
        if violations:
            evidence['digest'] = digest()
            return evidence['digest'], violations, evidence
        if not outage:
            failed('monitor-silent', 'the field owner\'s monitor '
                   'never answered after the link interruption — the '
                   'run aborted rather than degrading its served '
                   'health')
            evidence['digest'] = digest()
            return evidence['digest'], violations, evidence
        if snap is None:
            if tail_silent:
                failed('monitor-silent', 'the field owner\'s monitor '
                       'stopped answering during the outage — the '
                       'run aborted on field loss')
                evidence['digest'] = digest()
                return evidence['digest'], violations, evidence
            # The deadline passed without the degraded serve: a
            # plant that still answers means the lever never bit; a
            # dead plant means the served surface never reported the
            # loss — the misses named off the last trace entry.
            serving = _plant_serving(ctx)
            if serving is None:
                raise ConnectionError('the staged interruption never '
                                      'surfaced degraded and the '
                                      'run carries no plant probe '
                                      'seam to classify it')
            if serving:
                raise ConnectionError('the link-staging stop lever '
                                      'left the field serving — the '
                                      'staged interruption never '
                                      'reached the plant link')
            last = outage[-1]
            if last['link'] != 'disconnected':
                failed('never-degraded', 'the interrupted plant '
                       'link never surfaced on the served backend '
                       'diagnostics — the link still reports '
                       + str(last['link']))
            elif not last['last_error']:
                failed('never-named', 'the interrupted link reports '
                       'disconnected but the served backend '
                       'diagnostics never named the standing '
                       'failure in last_error: '
                       + json.dumps(last)[:300])
            else:
                failed('never-counted', 'the named link failure '
                       'never moved io_health\'s boundary streak — '
                       'the backend reported degraded without the '
                       'counted outage: ' + json.dumps(last)[:300])
            evidence['digest'] = digest()
            return evidence['digest'], violations, evidence
        marks['degraded'] = 'link-down-named-counted'
        if not (snap.get('tick') or 0) > (tick0 or 0):
            marks['cadence'] = 'stalled'
            failed('cadence', 'the served tick did not advance '
                   'through the outage — the scan cadence held at '
                   + str(snap.get('tick')))
            evidence['digest'] = digest()
            return evidence['digest'], violations, evidence
        marks['cadence'] = 'advancing'
        outage_health = snap.get('io_health') or {}

        # The restore: the plant returns as a new server lifetime and
        # the driver's lazy re-attach lands the first successful
        # exchange — which is also the standing record's clearing
        # boundary, so the first serve reporting the link connected
        # must already carry no last_error.
        try:
            start()
        except Exception as exc:
            raise ConnectionError('the link-staging start lever '
                                  'never completed: '
                                  + str(exc)[:300])
        stopped = False

        recovery, snap, tail_silent = watch(
            lambda _health, driver:
                driver.get('link') == 'connected',
            REMOTE_RECOVERY_RESTORE)
        evidence['recovery'] = recovery
        if violations:
            evidence['digest'] = digest()
            return evidence['digest'], violations, evidence
        if snap is None:
            if not recovery:
                failed('monitor-silent', 'the field owner\'s '
                       'monitor never answered across the plant\'s '
                       'return — the run aborted instead of riding '
                       'the link back')
                evidence['digest'] = digest()
                return evidence['digest'], violations, evidence
            if tail_silent:
                failed('monitor-silent', 'the field owner\'s '
                       'monitor stopped answering across the '
                       'plant\'s return — the run aborted on field '
                       'loss')
                evidence['digest'] = digest()
                return evidence['digest'], violations, evidence
            serving = _plant_serving(ctx)
            if serving is None:
                raise ConnectionError('the backend link never '
                                      'reported connected again and '
                                      'no plant probe seam can '
                                      'classify it')
            if not serving:
                raise ConnectionError('the restarted plant never '
                                      'served again — the '
                                      'link-staging restore never '
                                      'landed')
            failed('never-reattached', 'the backend never '
                   're-attached — the served link still reports '
                   'disconnected while the field answers: '
                   + json.dumps(recovery[-1])[:300])
            evidence['digest'] = digest()
            return evidence['digest'], violations, evidence
        marks['cleared'] = 're-attached'

        # The clearing verdict on the first connected serve — the
        # recorded failure must be gone already, never lingering
        # behind the healthy backend; the boundary streak stands
        # reset while the cumulative counters and the recorded fault
        # keep the outage's history.
        health = snap.get('io_health') or {}
        driver = _driver_health(snap) or {}
        evidence['first_connected'] = {'tick': snap.get('tick'),
                                       'driver': driver,
                                       'io_health': health}
        if driver.get('last_error'):
            failed('lingered', 'the backend\'s last_error lingered '
                   'behind the restored link — the first successful '
                   'exchange after the plant\'s return did not '
                   'clear the standing failure: '
                   + str(driver.get('last_error'))[:300])
        else:
            marks['cleared'] = 'first-exchange'
        if (health.get('consecutive_failures') or 0) > 0:
            failed('streak-stood', 'the boundary failure streak '
                   'still stood behind the healthy link: '
                   + json.dumps(health)[:300])
        else:
            marks['streak'] = 'reset'
        if (health.get('failed_reads') or 0) \
                < (outage_health.get('failed_reads') or 0) \
                or (health.get('failed_writes') or 0) \
                < (outage_health.get('failed_writes') or 0) \
                or not health.get('last_error'):
            failed('history-lost', 'the outage\'s counted history '
                   'reset across the recovery — the cumulative '
                   'counters or the recorded fault dropped what '
                   'they counted: ' + json.dumps(health)[:400])
        else:
            marks['history'] = 'retained'
        if not (snap.get('tick') or 0) > (tick0 or 0):
            marks['cadence'] = 'stalled'
            failed('cadence', 'the served tick did not advance '
                   'across the staged cycle — the scan cadence '
                   'held at ' + str(snap.get('tick')))
        evidence['digest'] = digest()
        return evidence['digest'], violations, evidence
    finally:
        if stopped:
            # The lane leaves the rig as it found it even on an
            # aborted cycle — best effort; a refused restore is the
            # next leg's observation, not this one's.
            try:
                start()
            except Exception:
                pass


def scenario_remote_driver_recovery(ctx):
    """Interrupt the field owner's plant link twice through the
    runner's stop/start staging: each cycle must surface the severed
    backend as `disconnected` with the named last_error and a counted
    io_health degradation, and the first serve reporting the link
    restored must already carry the cleared record — the pair's roles
    and the scan cadence holding throughout, both cycles producing the
    same verdicts."""
    case = Case('remote-driver-recovery',
                'The remote driver\'s standing failure clears on '
                'the first good exchange',
                'with the deployed pair settled and tracking, '
                'interrupting the field-owning controller\'s plant '
                'link surfaces the served backend diagnostics as '
                'disconnected with the severing failure named in '
                'last_error and io_health reporting the backend '
                'degraded — the failure streak advancing, the '
                'boundary fault recorded; restoring the link clears '
                'the backend\'s last_error on the first successful '
                'exchange — it never lingers behind the now-healthy '
                'backend — while the cumulative counters and the '
                'recorded fault keep the outage\'s history, the '
                'pair\'s roles and the scan cadence hold throughout, '
                'and a second staged interruption reproduces the '
                'cycle with identical digests')
    try:
        stop = ctx.get('stop_plant')
        start = ctx.get('start_plant')
        if stop is None or start is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no plant stop/start '
                               'link-staging lever')
        deadline = time.monotonic() + REMOTE_RECOVERY_SETTLE
        active = wait_for(lambda: _settled_active(ctx), deadline,
                          interval=REMOTE_RECOVERY_POLL)
        if active is None:
            if all(_try_role(ctx, ctx[name]) is None
                   for name in ('active', 'standby')):
                return case.finish('inconclusive', 'the rig is '
                                   'unreachable — neither pair '
                                   'endpoint answered /role')
            return case.finish('failed', 'remote-driver-recovery-'
                               'failed: no peer reports role=active')
        peer = 'standby' if active == 'active' else 'active'
        base, peer_base = ctx[active], ctx[peer]
        report = _try_role(ctx, peer_base)
        if report is None:
            return case.finish('inconclusive', 'the pair\'s '
                               'tracking peer never answered /role '
                               '— the roles-held leg has no '
                               'observation point')
        if report.get('role') != 'standby' \
                or 'tracking' not in (report.get('sync') or {}):
            return case.finish('inconclusive', 'the pair never '
                               'settled tracking — ' + peer
                               + ' reports '
                               + str(report.get('role')) + ' '
                               + json.dumps(report.get('sync'))[:200])
        case.observe('field owner: ' + active + ' (' + base
                     + '); watching peer ' + peer)

        digests = []
        for number in (1, 2):
            digest, violations, evidence = _remote_driver_cycle(
                ctx, number, active, peer, stop, start)
            ref = save_evidence(ctx['evidence_dir'],
                                'remote-driver-recovery-cycle-'
                                + str(number) + '.json', evidence)
            case.evidence('file', ref, 'cycle ' + str(number)
                          + ' — the outage and recovery watches and '
                          'the normalized digest')
            if violations:
                failed = any(name == 'remote-driver-recovery-failed'
                             for name, _ in violations.values())
                diagnostic = 'remote-driver-recovery-failed' \
                    if failed \
                    else 'remote-driver-recovery-nondeterministic'
                return case.finish(
                    'failed', diagnostic + ': ' + '; '.join(
                        detail for _, detail in
                        list(violations.values())[:4]))
            digests.append(digest)
            case.observe('cycle ' + str(number)
                         + ': interrupted to degraded-and-named, '
                         'restored to connected-and-cleared')
        if digests[0] != digests[1]:
            return case.finish(
                'failed', 'remote-driver-recovery-nondeterministic: '
                'the two cycles\' digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        ref = save_evidence(ctx['evidence_dir'],
                            'remote-driver-recovery-digest.json',
                            {'cycles': digests})
        case.evidence('file', ref, 'the normalized deterministic '
                      'digest both cycles produced')

        # The launch layout for the cases behind this one: the pair
        # settles back on the roles it entered with — a clean run
        # never moved them, so the wait returns immediately.
        def settled():
            if _settled_active(ctx) != active:
                return None
            report = _try_role(ctx, peer_base)
            if report is None or report.get('role') != 'standby':
                return None
            return report
        if wait_for(settled, time.monotonic()
                    + REMOTE_RECOVERY_SETTLE,
                    interval=REMOTE_RECOVERY_POLL) is None:
            return case.finish('failed', 'remote-driver-recovery-'
                               'failed: the pair did not settle '
                               'back on its launch roles — ' + active
                               + ' no longer reports active or '
                               + peer + ' left standby')
        case.observe('rig restored: plant serving, ' + active
                     + ' active, ' + peer + ' standby')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
