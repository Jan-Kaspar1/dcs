"""The sim-bus driver-reattach acceptance leg — one module per leg of
the scenario schedule; see qa_lane/scenarios/__init__.py for the
ordering rule and the shared seam."""
from .common import *

# Ordering: the reattach leg runs on its own staged rig — a
# `dcs-sim-bus-device` server plus a controller pair bound to it
# (ctx['bus']) — and never touches the deployed pair, the probe
# pair's plant, or any shared field, claim token, or runner-owned
# file. Nothing it stages can contaminate an earlier case, and the
# cases behind it find the rig exactly as they left it, so the leg
# needs no window of its own.
RUNS_AFTER = frozenset({'scenario_remote_driver_recovery'})

# --------------------------------------------------------------------
# The point-wise BusDriver's lazy-reattach contract — the per-revision
# lane evidence for #1351's fix, serving WW-LCM-001's continuity clause
# and the health model's rule that a field communication fault is a
# transient, retriable event. `dcs-sim-net`'s `RemoteDriver` and
# `dcs-sim-bus`'s `BusDriver` implement their recovery independently:
# each keeps a link, drops it on a failed exchange, and re-attaches on
# the next access with a backoff. Before the fix the point-wise driver
# dropped its stream on the first failed exchange and answered
# `Disconnected` for the life of the process — the active's scanning
# `communication_fault` values never cleared and the standby stayed
# unpromotable, because a promotion must claim the device and the
# claim request rode a dead link. The leg drives both outage classes
# the finding records, against a rig-mounted register device:
#
#   (a) device-restart — the device server is restarted under the
#       pair, returning on its bound address with its claim table
#       empty, so the field owner must re-attach *and* re-arm its
#       writer claim;
#   (b) field-stall — the device is frozen for ~2 s and thawed: a
#       transient unanswerable window with nothing dying, which the
#       rig's declared per-request timeout turns into a failed
#       exchange the driver has to recover from.
#
# Through each member's serving monitor the leg asserts the whole
# contract, twice per class so the run's own determinism is measured:
# the healthy baseline carries a connected link and no standing
# `last_error`; the outage surfaces as `disconnected` with the
# severing failure named in `last_error` and io_health counting the
# degraded boundary; the link returns to `connected` inside the
# documented bound — the fix's one-second `REATTACH_INTERVAL` plus the
# driver's request timeout and the restarted device's own bind, never
# a permanent `link:disconnected` — with the first successful
# exchange clearing the standing record, the failure streak reset, and
# the cumulative counters and recorded fault keeping the outage's
# history; the served tick and the scan cadence resume, never rewound
# past the running peak, which would be a controller restart rather
# than a driver recovery; the pair's launch roles hold throughout; and
# once the backend is back, `POST /promote` on the converged standby
# answers inside its own bound — the promotion path's device claim
# riding the recovered link — after which the launch roles are
# restored by promoting the original owner back.
#
# Functional misses on the recovery contract name
# sim-bus-reattach-failed; verdicts on the run's own determinism — a
# rewound tick, two passes whose digests disagree — name
# sim-bus-reattach-nondeterministic. A run context carrying no
# register-mapped field rig, a rig whose device or pair never answers,
# a pair that never settles tracking, a staging lever that never
# completes, and a staged revision whose served surface cannot express
# the contract at all report inconclusive.

# The documented recovery bound: the fix's REATTACH_INTERVAL (one
# second) plus one request timeout and the restarted device server's
# own bind, with scan-cadence slack — generous where the contract is
# fast, and well inside the suite's per-scenario budget either way.
BUS_REATTACH_BOUND = 30
# The bound on the outage surfacing as a counted degradation, and on
# the promotion probe answering once the backend is back.
BUS_REATTACH_DEGRADE = 20
BUS_REATTACH_PROMOTE = 20
# The freeze the stall class holds: the ~2 s the finding records, above
# the rig's declared per-request timeout, so the window produces a
# failed exchange to recover from rather than a late answer.
BUS_REATTACH_STALL = 2.0
BUS_REATTACH_SETTLE = 30    # bound on the pair settling before/after
BUS_REATTACH_POLL = 0.25     # cadence watching the link mid-outage
# The two outage classes, each staged by its own levers.
BUS_OUTAGE_LEGS = ('device-restart', 'field-stall')


def _driver_health(snapshot):
    """The served io_health's backend driver diagnostics — the
    DriverDiagnostics the point-wise driver volunteers — or None when
    the snapshot carries no reporting backend."""
    health = (snapshot or {}).get('io_health') or {}
    driver = health.get('driver')
    return driver if isinstance(driver, dict) else None


def _device_serving(subject):
    """Whether the rig's register device answers its own census right
    now — the device-side half of the outage classification, asked
    through the shipped field tool exec'd inside the device's own
    container. True, False, or None when the subject carries no
    device-tool seam to ask with."""
    try:
        answer = subject['device_ctl']('list')
    except Exception:
        return None
    return getattr(answer, 'returncode', 1) == 0


def _bus_owner(ctx, subject):
    """The bus rig's field-owning endpoint key, or None while the pair
    is mid-transition — `_settled_active` scoped to the bus subject,
    which never touches the deployed or probe pair's endpoints."""
    for name in ('active', 'standby'):
        report = _try_role(ctx, subject[name])
        if report is not None and report.get('role') == 'active':
            return name
    return None


def _bus_tracking(ctx, subject, name):
    """The endpoint's report while it is a tracking standby — the
    converged, promotable posture the promotion probe needs — else
    None."""
    report = _try_role(ctx, subject[name])
    if report is None or report.get('role') != 'standby' \
            or 'tracking' not in (report.get('sync') or {}):
        return None
    return report


def _bus_outage(ctx, subject, active, peer, leg, number):
    """One outage class: the class's levers stage the register device
    out of reach and the watches follow the pair's serving monitors
    across it. Returns (digest, violations, evidence) — the digest is
    the outage's normalized verdict, identical across two clean
    passes of the same class; violations is {key: (diagnostic,
    detail)} in first-seen order. Rig states the contract cannot
    answer for raise for the caller's inconclusive verdict."""
    expected = {active: 'active', peer: 'standby'}
    members = (active, peer)
    violations = {}
    evidence = {'leg': leg, 'pass': number}
    peak = {}
    marks = {'baseline': None, 'outage': None, 'recovered': None,
             'roles': 'held', 'cadence': 'held'}
    frozen = False

    def note(key, diagnostic, detail):
        violations.setdefault(key, (diagnostic, detail))

    def failed(key, detail):
        note(key, 'sim-bus-reattach-failed', detail)

    def digest():
        return {key: marks[key] if marks[key] is not None
                else 'unreached' for key in sorted(marks)}

    def watch_roles():
        """One poll of both members' /role: a moved role is the pair's
        stability contract breaking — the outage's watches end on it.
        The promotion probe, not the device, is what moves a role."""
        for name in (active, peer):
            report = _try_role(ctx, subject[name])
            if report is not None and report.get('role') \
                    != expected[name]:
                marks['roles'] = 'moved'
                failed('role-' + name, name + ' reported role '
                       + str(report.get('role')) + ' through the '
                       + leg + ' outage — the pair\'s launch roles '
                       'did not hold: ' + json.dumps(report)[:300])
                return False
        return True

    def watch_tick(snap, name):
        """Served-tick monotonicity across the outage: a rewind is the
        tick-domain signature of a controller restart, which the
        nondeterminism diagnostic names."""
        tick = snap.get('tick')
        if not isinstance(tick, int):
            return True
        seen = peak.get(name)
        if seen is not None and tick < seen:
            marks['cadence'] = 'rewound'
            note('tick-rewound-' + name,
                 'sim-bus-reattach-nondeterministic',
                 name + '\'s served tick rewound across the ' + leg
                 + ' outage — ' + str(tick) + ' under the running '
                 'peak ' + str(seen) + ': a controller restarted '
                 'instead of riding the device outage out')
            return False
        peak[name] = tick if seen is None else max(seen, tick)
        return True

    def watch(match, bound):
        """One watch loop across the outage's two halves: poll each
        member's snapshot until `match(health, driver)` holds on
        both, a role moves, a tick rewinds, or `bound` seconds pass.
        Returns (trace, hits, silent) — hits maps each matched
        member to its snapshot, silent names the members whose
        monitor died mid-watch."""
        trace = []
        hits = {}
        silent = set()
        deadline = time.monotonic() + bound
        while time.monotonic() < deadline:
            if not watch_roles():
                break
            for name in members:
                snap = _try_snapshot(ctx, subject[name])
                if snap is None:
                    if name not in hits:
                        silent.add(name)
                    continue
                if name in silent:
                    silent.discard(name)
                if not watch_tick(snap, name):
                    return trace, hits, silent
                health = snap.get('io_health') or {}
                driver = _driver_health(snap) or {}
                trace.append({'member': name, 'tick': snap.get('tick'),
                              'link': driver.get('link'),
                              'last_error': driver.get('last_error'),
                              'consecutive':
                                  health.get('consecutive_failures'),
                              'failed_reads': health.get('failed_reads'),
                              'failed_writes': health.get('failed_writes'),
                              'fault': health.get('last_error')})
                if name not in hits and match(health, driver):
                    hits[name] = snap
            if len(hits) == len(members):
                break
            if len(silent) == len(members):
                return trace, hits, silent
            time.sleep(BUS_REATTACH_POLL)
        return trace, hits, silent

    def degraded(health, driver):
        return driver.get('link') == 'disconnected' \
            and bool(driver.get('last_error')) \
            and (health.get('consecutive_failures') or 0) > 0 \
            and bool(health.get('last_error'))

    def recovered(_health, driver):
        return driver.get('link') == 'connected'

    try:
        # The baseline the outage runs from: the settled healthy serve
        # — the register driver's connected link carrying no standing
        # record. A severed link, a surface without the contract's
        # accounting, or a standing record behind a healthy link is
        # caught here, before any staging runs.
        baseline = {}
        ticks = {}
        for name in members:
            snap = _try_snapshot(ctx, subject[name])
            health = (snap or {}).get('io_health')
            driver = _driver_health(snap)
            baseline[name] = {'tick': (snap or {}).get('tick'),
                              'io_health': health, 'driver': driver}
            if snap is None:
                raise ConnectionError(name + '\'s monitor never '
                                      'answered the ' + leg + '\'s '
                                      'baseline snapshot')
            if not isinstance(health, dict) or driver is None \
                    or 'link' not in driver \
                    or 'last_error' not in driver \
                    or 'consecutive_failures' not in health \
                    or 'failed_reads' not in health \
                    or 'last_error' not in health:
                raise ConnectionError('the served io_health carries no '
                                      'register-driver diagnostics and '
                                      'failed-exchange accounting — '
                                      'the staged revision predates the '
                                      'sim-bus reattach contract the '
                                      'leg measures')
            if driver.get('link') != 'connected':
                raise ConnectionError(name + '\'s register driver '
                                      'reports '
                                      + str(driver.get('link'))
                                      + ' ahead of the ' + leg
                                      + ' — the rig never presented the '
                                      'healthy baseline')
            ticks[name] = snap.get('tick')
            if driver.get('last_error'):
                failed('baseline-lingered-' + name,
                       'a standing last_error stands behind ' + name
                       + '\'s healthy register link ahead of the '
                       + leg + ' — the record never cleared on the '
                       'exchanges since it stood: '
                       + str(driver.get('last_error'))[:300])
        evidence['baseline'] = baseline
        if violations:
            evidence['digest'] = digest()
            return evidence['digest'], violations, evidence
        marks['baseline'] = 'connected-and-clear'

        # The outage: stage the class, then watch the pair degrade.
        try:
            if leg == 'device-restart':
                subject['restart_device']()
            else:
                subject['freeze_device']()
                frozen = True
                time.sleep(BUS_REATTACH_STALL)
                subject['thaw_device']()
                frozen = False
        except Exception as exc:
            raise ConnectionError('the ' + leg + ' staging lever never '
                                  'completed: ' + str(exc)[:300])

        outage, hits, silent = watch(degraded, BUS_REATTACH_DEGRADE)
        evidence['outage'] = outage
        evidence['device_serving_mid'] = _device_serving(subject)
        if violations:
            evidence['digest'] = digest()
            return evidence['digest'], violations, evidence
        if silent:
            failed('monitor-silent-' + sorted(silent)[0],
                   'the bus pair\'s monitor stopped answering during '
                   'the ' + leg + ' outage: ' + ', '.join(sorted(silent))
                   + ' — the run aborted on field loss')
            evidence['digest'] = digest()
            return evidence['digest'], violations, evidence
        if len(hits) < len(members):
            failed('never-degraded',
                   'the ' + leg + ' never surfaced on the served '
                   'register-driver diagnostics of both members — the '
                   'last trace entries read '
                   + json.dumps(outage[-2:])[:400])
            evidence['digest'] = digest()
            return evidence['digest'], violations, evidence
        marks['outage'] = 'link-down-named-counted'
        outage_health = {name: (hits[name].get('io_health') or {})
                         for name in hits}

        # The recovery: the first serve reporting the link connected
        # inside the documented bound must already carry the cleared
        # record, the reset streak, the kept history, and an advanced
        # tick — on both members, never one.
        recovery, hits, silent = watch(recovered, BUS_REATTACH_BOUND)
        evidence['recovery'] = recovery
        evidence['device_serving_after'] = _device_serving(subject)
        if violations:
            evidence['digest'] = digest()
            return evidence['digest'], violations, evidence
        if silent:
            failed('monitor-silent-' + sorted(silent)[0],
                   'the bus pair\'s monitor stopped answering across '
                   'the ' + leg + '\'s recovery: '
                   + ', '.join(sorted(silent))
                   + ' — the run aborted on field loss')
            evidence['digest'] = digest()
            return evidence['digest'], violations, evidence
        if len(hits) < len(members):
            # The device answering again makes a still-disconnected
            # link the driver's own verdict: the re-attach the fix
            # names did not land inside the documented bound.
            serving = _device_serving(subject)
            if serving:
                failed('never-reattached',
                       'the register driver never re-attached inside '
                       'the documented ' + str(BUS_REATTACH_BOUND)
                       + 's bound after the ' + leg + ' — the served '
                       'link still reports disconnected while the '
                       'device answers: '
                       + json.dumps(recovery[-2:])[:400])
            elif serving is None:
                raise ConnectionError('the register driver never '
                                      're-attached and the device '
                                      'itself cannot be asked whether '
                                      'it serves again — the ' + leg
                                      + '\'s restore is unclassifiable')
            else:
                raise ConnectionError('the register driver never '
                                      're-attached and the device '
                                      'itself never served again — the '
                                      + leg + '\'s restore never '
                                      'landed')
            evidence['digest'] = digest()
            return evidence['digest'], violations, evidence
        marks['recovered'] = 're-attached'
        evidence['first_connected'] = {
            name: {'tick': hits[name].get('tick'),
                   'driver': _driver_health(hits[name]),
                   'io_health': hits[name].get('io_health')}
            for name in members}

        for name in members:
            health = hits[name].get('io_health') or {}
            driver = _driver_health(hits[name]) or {}
            before = outage_health.get(name) or {}
            if driver.get('last_error'):
                failed('lingered-' + name,
                       name + '\'s register-driver last_error lingered '
                       'behind its restored link — the first successful '
                       'exchange after the ' + leg + ' did not clear '
                       'the standing failure: '
                       + str(driver.get('last_error'))[:300])
            if (health.get('consecutive_failures') or 0) > 0:
                failed('streak-stood-' + name,
                       name + '\'s boundary failure streak still stood '
                       'behind the healthy link: '
                       + json.dumps(health)[:300])
            if (health.get('failed_reads') or 0) \
                    < (before.get('failed_reads') or 0) \
                    or (health.get('failed_writes') or 0) \
                    < (before.get('failed_writes') or 0) \
                    or not health.get('last_error'):
                failed('history-lost-' + name,
                       name + '\'s counted outage history reset across '
                       'the recovery — the cumulative counters or the '
                       'recorded fault dropped what they counted: '
                       + json.dumps(health)[:400])
            if not (hits[name].get('tick') or 0) > (ticks[name] or 0):
                marks['cadence'] = 'stalled'
                failed('cadence-' + name,
                       name + '\'s served tick did not advance across '
                       'the ' + leg + ' — the scan cadence held at '
                       + str(hits[name].get('tick')))
        if not violations and marks['cadence'] != 'stalled':
            marks['cadence'] = 'advancing'
        evidence['digest'] = digest()
        return evidence['digest'], violations, evidence
    finally:
        if frozen:
            # The lane leaves the device running even on an aborted
            # outage — best effort; a refused thaw is the next pass's
            # observation, not this one's.
            try:
                subject['thaw_device']()
            except Exception:
                pass


def _bus_promote_probe(ctx, subject, owner, tracker):
    """The promotable-standby half: with the backend recovered,
    `POST /promote` on the converged standby must answer inside the
    declared bound — the switch path's device claim rides the
    recovered link, so a peer still sealed to a dead driver never
    becomes promotable — and the launch roles restore behind it by
    promoting the original owner back. Returns the evidence record;
    `inconclusive` names a rig state the contract cannot answer for
    and `violations` a missed clause ('failed' or
    'nondeterministic') with its detail."""
    record = {'promoted': tracker, 'restored': owner}

    def settled_at(key):
        return wait_for(lambda: _bus_owner(ctx, subject) == key,
                        time.monotonic() + BUS_REATTACH_SETTLE,
                        interval=BUS_REATTACH_POLL) is not None

    def refuse(clause, detail):
        record['violations'] = [clause]
        record['detail'] = detail
        return record

    if _bus_tracking(ctx, subject, tracker) is None:
        record['inconclusive'] = (
            'the bus pair\'s tracking peer never settled tracking '
            'after the outages — the promotion probe has no converged '
            'standby to promote')
        return record

    started = time.monotonic()
    status, body = _settle_call(subject[tracker] + '/promote')
    elapsed = time.monotonic() - started
    record['status'] = status
    record['body'] = body
    record['elapsed_ms'] = int(elapsed * 1000)
    if status != 200:
        return refuse('failed',
                      'POST /promote on the converged standby answered '
                      + str(status) + ' after the backend recovered: '
                      + json.dumps(body)[:300])
    if elapsed > BUS_REATTACH_PROMOTE:
        return refuse('failed',
                      'POST /promote answered in '
                      + str(int(elapsed * 1000)) + 'ms, past the '
                      'declared ' + str(BUS_REATTACH_PROMOTE) + 's bound')
    if not settled_at(tracker):
        return refuse('failed',
                      'the promoted peer never settled active — the '
                      'switch did not complete against the recovered '
                      'device')
    if _bus_tracking(ctx, subject, owner) is None:
        return refuse('failed',
                      'the demoted peer never settled tracking behind '
                      'the promoted owner — the pair did not '
                      'reconverge')

    status, body = _settle_call(subject[owner] + '/promote')
    record['restore_status'] = status
    record['restore_body'] = body
    if status != 200:
        return refuse('failed',
                      'restoring the launch roles failed: POST /promote '
                      'on the original owner answered ' + str(status)
                      + ' — ' + json.dumps(body)[:300])
    if not settled_at(owner):
        return refuse('nondeterministic',
                      'the original owner never regained active after '
                      'the promotion probe — the pair never reconverged '
                      'on its launch roles')
    if _bus_tracking(ctx, subject, tracker) is None:
        return refuse('nondeterministic',
                      'the demoted peer never returned to tracking '
                      'after the launch roles were restored')
    return record


def scenario_sim_bus_driver_reattach(ctx):
    """Sever the rig's register-mapped device twice over — a device
    restart and a ~2 s stall — with a controller pair attached, and
    assert the point-wise BusDriver recovers from both by lazy
    re-attach: the served link returns to connected inside the
    documented bound with the standing record cleared, the exchange
    accounting keeps the outage's history, the scan cadence resumes
    without a rewind, the launch roles hold, and POST /promote on the
    converged standby answers inside its bound once the backend is
    back — each class twice with identical digests, the launch roles
    restored afterward."""
    case = Case('sim-bus-reattach',
                'The point-wise register driver re-attaches after a '
                'device restart and a brief stall',
                'with a controller pair settled on the rig\'s '
                'register-mapped device, a device restart and a ~2 s '
                'stall each surface the served backend diagnostics as '
                'disconnected with the severing failure named in '
                'last_error and io_health counting the degraded '
                'boundary — the streak advancing, the recorded fault '
                'stamped; the driver then re-attaches inside the '
                'documented bound and the first serve reporting the '
                'link connected already carries the cleared record and '
                'the reset streak while the cumulative counters and '
                'the recorded fault keep the outage\'s history, the '
                'served tick advances without rewinding, the pair\'s '
                'launch roles hold throughout, and POST /promote on '
                'the converged standby answers inside its bound once '
                'the backend is back — a second pass over both classes '
                'reproducing the digests exactly')
    try:
        subject = ctx.get('bus')
        if subject is None:
            return case.finish('inconclusive', 'the run context stages '
                               'no register-mapped field rig — the '
                               'lane carries no sim-bus device for the '
                               'driver-reattach contract')
        for lever in ('restart_device', 'freeze_device', 'thaw_device',
                      'device_ctl'):
            if subject.get(lever) is None:
                return case.finish('inconclusive', 'the register-mapped '
                                   'field subject carries no '
                                   + lever + ' lever')
        owner = wait_for(lambda: _bus_owner(ctx, subject),
                         time.monotonic() + BUS_REATTACH_SETTLE,
                         interval=BUS_REATTACH_POLL)
        if owner is None:
            if all(_try_role(ctx, subject[name]) is None
                   for name in ('active', 'standby')):
                return case.finish('inconclusive', 'the register-field '
                                   'rig is unreachable — neither bus '
                                   'pair endpoint answered /role')
            return case.finish('failed', 'sim-bus-reattach-failed: no '
                               'bus pair member reports role=active')
        tracker = 'standby' if owner == 'active' else 'active'
        if _bus_tracking(ctx, subject, tracker) is None:
            return case.finish('inconclusive', 'the bus pair never '
                               'settled tracking — ' + tracker + ' '
                               'reports no converged standby')
        case.observe('register field rig: ' + owner + ' ('
                     + subject[owner] + '), peer ' + tracker + ' ('
                     + subject[tracker] + ')')

        passes = {}
        for number in (1, 2):
            for leg in BUS_OUTAGE_LEGS:
                digest, violations, evidence = _bus_outage(
                    ctx, subject, owner, tracker, leg, number)
                ref = save_evidence(
                    ctx['evidence_dir'], 'sim-bus-reattach-' + leg
                    + '-pass-' + str(number) + '.json', evidence)
                case.evidence('file', ref, leg + ', pass '
                              + str(number) + ' — the baseline, outage '
                              'and recovery watches and the normalized '
                              'digest')
                if violations:
                    broke = any(name == 'sim-bus-reattach-failed'
                                for name, _ in violations.values())
                    diagnostic = ('sim-bus-reattach-failed' if broke
                                  else
                                  'sim-bus-reattach-nondeterministic')
                    return case.finish(
                        'failed', diagnostic + ': ' + '; '.join(
                            detail for _, detail in
                            list(violations.values())[:4]))
                passes.setdefault(leg, []).append(digest)
                case.observe(leg + ', pass ' + str(number)
                             + ': degraded, then re-attached inside '
                             + str(BUS_REATTACH_BOUND) + 's')
        diverged = {leg: passes[leg] for leg in passes
                    if passes[leg][0] != passes[leg][1]}
        if diverged:
            return case.finish(
                'failed', 'sim-bus-reattach-nondeterministic: the two '
                'passes\' digests diverged: '
                + json.dumps(diverged, sort_keys=True))
        ref = save_evidence(ctx['evidence_dir'],
                            'sim-bus-reattach-digest.json',
                            {'passes': passes})
        case.evidence('file', ref, 'the normalized deterministic digest '
                      'both passes of each outage class produced')

        probe = _bus_promote_probe(ctx, subject, owner, tracker)
        ref = save_evidence(ctx['evidence_dir'],
                            'sim-bus-reattach-promote.json', probe)
        case.evidence('file', ref, 'the promotion probe and the restored '
                      'launch roles')
        if probe.get('inconclusive'):
            return case.finish('inconclusive', probe['inconclusive'])
        if probe.get('violations'):
            diagnostic = ('sim-bus-reattach-failed'
                          if probe['violations'][0] == 'failed'
                          else 'sim-bus-reattach-nondeterministic')
            return case.finish('failed', diagnostic + ': '
                               + probe['detail'])
        case.observe('POST /promote answered on the converged standby in '
                     + str(probe['elapsed_ms']) + 'ms; launch roles '
                     'restored')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))