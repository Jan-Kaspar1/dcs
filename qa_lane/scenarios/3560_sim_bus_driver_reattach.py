"""The sim-bus driver-reattach acceptance leg — one module per leg of
the scenario schedule; see qa_lane/scenarios/__init__.py for the
ordering rule and the shared seam."""
from .common import *

# Ordering: the reattach leg stages its own pair on the lane's
# register-protocol device, so it needs the born seats the earlier
# legs hold — the fencing-loss demotion leg's driven/foreign pair —
# and it must be done before the scan-batch leg claims the driven
# seat again.
RUNS_AFTER = frozenset({'scenario_remote_driver_recovery',
                        'scenario_sim_cyclic_fencing_loss_demote'})
RUNS_BEFORE = frozenset({'scenario_scan_batch_bound'})

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
# The re-attach is fast by design — the fix's one-second
# `REATTACH_INTERVAL` — so the window in which the link is observably
# down is roughly one re-attach interval wide, and no poll-based
# watch can be asked to prove it caught that window. The leg
# therefore splits what it asserts by what it can hold still. Both
# classes assert the durable contract, which survives the recovery
# and is what the issue names: on every member the boundary counters
# moved over the baseline and the recorded fault is stamped (the
# outage was counted), and the first serve reporting the link
# `connected` after the outage already carries the cleared standing
# record, the reset failure streak, the kept cumulative history, and
# a served tick advanced past the baseline — never rewound past the
# running peak, which would be a controller restart rather than a
# driver recovery. The stall class, whose freeze the leg owns,
# additionally *holds* the device down until every member has
# surfaced the outage as `disconnected` with the severing failure
# named in `last_error` and io_health counting the degraded
# boundary — so a driver that swallows its own transport diagnostics
# is caught on a window the leg controls rather than on one it
# races. The restart class lets the device return on its own
# schedule and records the transient where the return allows it; a
# driver fast enough to drop and restore between two of the leg's
# reads is still a recovery the contract permits, so the moved
# counters witness that one.
#
# The pair's launch roles are watched through both classes, and once
# the backend is back `POST /promote` on the converged standby must
# answer inside its own bound — the promotion path's device claim
# riding the recovered link, so a peer still sealed to a dead driver
# never becomes promotable — after which the launch roles are
# restored by promoting the original owner back.
#
# Functional misses on the recovery contract name
# sim-bus-reattach-failed; verdicts on the run's own determinism — a
# rewound tick, two passes whose digests disagree — name
# sim-bus-reattach-nondeterministic. The unchecked-diagnostic
# self-check replays the recovered-link audit over planted negatives —
# the standing record lingering, the streak standing, the counted
# history reset, the outage uncounted, the tick held at the baseline —
# and any it lets through reports sim-bus-reattach-unchecked. A run
# context carrying no sim-bus device-server seam or no born-controller
# levers, a rig whose device or pair never answers, a pair that never
# settles tracking, a staging lever that never completes, a restart
# whose device never serves again, a restore the leg cannot classify,
# and a staged revision whose served surface cannot express the
# contract at all report inconclusive.

OWNER_SEAT = 'driven'      # the born seat launched field-active
PEER_SEAT = 'foreign'      # the born seat launched as the tracking member
BUS_KIND = 'sim-bus'       # the device kind this contract is about
# The per-request timeout the leg stamps onto the staged document:
# under the ~2 s stall, so a frozen device times out mid-exchange and
# the driver has a failed exchange to recover from — the fixtures'
# device-agnostic five-second default would sit longer than the
# outage.
BUS_FIELD_TIMEOUT = 1500

# The documented recovery bound: the fix's REATTACH_INTERVAL (one
# second) plus one request timeout and the restarted device server's
# own bind, with scan-cadence slack — generous where the contract is
# fast, and well inside the suite's per-scenario budget either way.
BUS_REATTACH_BOUND = 30
# The bound on the stall class holding the device down until the
# outage surfaces on every member, and on the promotion probe
# answering once the backend is back.
BUS_REATTACH_DEGRADE = 20
BUS_REATTACH_PROMOTE = 20
# The freeze the stall class holds: the ~2 s the finding records, above
# the rig's declared per-request timeout, so the window produces a
# failed exchange to recover from rather than a late answer. It is the
# class's floor, not its ceiling — the freeze is held past it until
# every member has surfaced the outage, so the documented window is
# the window the leg observes.
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
    """Whether the rig's register device reports itself serving right
    now — the device-side half of the outage classification, asked
    through the runner's serving lever: the server's own bound-address
    announcement scoped to its running lifetime. True, False, or None
    when the subject carries no serving seam to ask with.

    Never asked while the device is frozen: a paused container's log
    still carries its announcement, so the lever answers False on the
    frozen state itself — every call site runs after the restore."""
    probe = subject.get('device_serving')
    if probe is None:
        return None
    try:
        return bool(probe(subject['device']))
    except Exception:
        return None


def _counted(health):
    """The io_health boundary counters summed — the failed-exchange
    accounting's monotone total, the one part of it a successful
    exchange never rewinds."""
    body = health or {}
    return (body.get('failed_reads') or 0) + (body.get('failed_writes') or 0)


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
    answer for raise for the caller's inconclusive verdict.

    What the class can hold still is what it asserts: the stall
    class keeps the device frozen until every member has surfaced
    the outage, so its degraded observation is a fact rather than a
    race with the driver's one-second re-attach; the restart class
    lets the device return on its own schedule and gates only on the
    durable accounting."""
    expected = {active: 'active', peer: 'standby'}
    members = (active, peer)
    violations = {}
    evidence = {'leg': leg, 'pass': number}
    peak = {}
    marks = {'baseline': None, 'degraded': None, 'outage': None,
             'recovered': None, 'roles': 'held', 'cadence': 'held'}
    floors = {}
    ticks = {}
    severed = set()   # the members whose served link the leg has
                      # watched report disconnected
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

    def watch(match, bound, hold=None):
        """One watch loop across the outage's two halves: poll each
        member's snapshot until `match(health, driver)` holds on both,
        a role moves, a tick rewinds, or `bound` seconds pass. `hold`
        is a monotonic instant the watch waits out even after the
        match, which is how the stall class keeps the device frozen
        for the documented window the driver's own re-attach latency
        would otherwise close. Returns (trace, hits, silent) — hits
        maps each matched member to its snapshot, silent names the
        members whose monitor died mid-watch."""
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
                if name not in hits and match(name, health, driver):
                    hits[name] = snap
            if len(hits) == len(members) and (hold is None
                                              or time.monotonic() >= hold):
                break
            if len(silent) == len(members):
                return trace, hits, silent
            time.sleep(BUS_REATTACH_POLL)
        return trace, hits, silent

    def degraded(name, health, driver):
        """The outage surfacing on one member: the severed link, the
        severing failure named beside it, the boundary streak
        advancing, and the boundary fault recorded."""
        return driver.get('link') == 'disconnected' \
            and bool(driver.get('last_error')) \
            and (health.get('consecutive_failures') or 0) > 0 \
            and bool(health.get('last_error'))

    def reattached(name, health, driver):
        """The recovery watch's per-member match, as a state machine
        over the outage's own served diagnostics: a member counts as
        recovered on the first serve reporting the link `connected`
        after the leg watched it report `disconnected`. A watch that
        opens while the driver is still severing therefore never
        matches the healthy serve it started from, which is what
        makes the matched serve the *first* serve after the outage
        rather than an arbitrary one.

        A member the leg never watched go down is matched instead by
        its boundary counters having moved past the baseline — the
        same serve under the other witness. The fix re-attaches on a
        one-second bound, so a driver faster than the poll cadence
        can drop and restore between two of the leg's reads; that is
        a recovery the contract permits and the leg must not read as
        a defect."""
        link = driver.get('link')
        if link == 'disconnected':
            severed.add(name)
            return False
        if link != 'connected':
            return False
        return name in severed or _counted(health) > floors[name]

    def abandoned(*traces):
        """The no-recovery verdict, classified by the device itself
        and by what the leg actually watched. A member whose served
        diagnostics never reported the outage is a driver that does
        not surface its own transport degradation — named as such
        rather than as a re-attach that never landed, because the
        honest reading of a link that never went down is a driver
        that never said it did. A device answering again with the
        outage surfaced is the driver's own verdict: the re-attach
        the fix names did not land inside the documented bound. A
        device that never served again means the class's restore
        never landed, and a census the rig cannot answer for is
        unclassifiable; both raise for the caller's inconclusive
        verdict."""
        seen = set(severed)
        for trace in traces:
            seen.update(entry['member'] for entry in trace
                        if entry.get('link') == 'disconnected')
        serving = _device_serving(subject)
        evidence['device_serving'] = serving
        if serving:
            missing = [name for name in members if name not in seen]
            if missing:
                failed('never-degraded',
                       'the ' + leg + ' never surfaced on the served '
                       'register-driver diagnostics of '
                       + ', '.join(missing) + ' — neither a '
                       'disconnected link nor a moved boundary total '
                       'while the device answered the leg\'s own '
                       'census: ' + json.dumps(traces[-1][-2:])[:400])
            else:
                failed('never-reattached',
                       'the register driver never re-attached inside '
                       'the documented ' + str(BUS_REATTACH_BOUND)
                       + 's bound after the ' + leg + ' — the served '
                       'link still reports disconnected while the '
                       'device answers: '
                       + json.dumps(traces[-1][-2:])[:400])
            evidence['digest'] = digest()
            return evidence['digest'], violations, evidence
        if serving is None:
            raise ConnectionError('the register driver never '
                                  're-attached and the device itself '
                                  'cannot be asked whether it serves '
                                  'again — the ' + leg + '\'s restore '
                                  'is unclassifiable')
        raise ConnectionError('the register driver never re-attached '
                              'and the device itself never served '
                              'again — the ' + leg + '\'s restore '
                              'never landed')

    try:
        # The baseline the outage runs from: the settled healthy serve
        # — the register driver's connected link carrying no standing
        # record. A severed link, a surface without the contract's
        # accounting, or a standing record behind a healthy link is
        # caught here, before any staging runs.
        baseline = {}
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
            floors[name] = _counted(health)
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

        # The staging. The stall class holds the freeze until every
        # member has surfaced the outage, so the degraded observation
        # is one the leg witnessed rather than one it raced; the
        # restart class lets the device return on its own schedule and
        # records the transient where the return allows it.
        marks['degraded'] = 'not-gated'
        try:
            if leg == 'device-restart':
                subject['restart_device']()
            else:
                subject['freeze_device']()
                frozen = True
                degrade, hits, silent = watch(
                    degraded, BUS_REATTACH_DEGRADE,
                    hold=time.monotonic() + BUS_REATTACH_STALL)
                evidence['degrade_trace'] = degrade
                if violations:
                    evidence['digest'] = digest()
                    return evidence['digest'], violations, evidence
                if silent:
                    failed('monitor-silent-' + sorted(silent)[0],
                           'the bus pair\'s monitor stopped answering '
                           'while the device was frozen: '
                           + ', '.join(sorted(silent))
                           + ' — the run aborted on field loss')
                    evidence['digest'] = digest()
                    return evidence['digest'], violations, evidence
                if len(hits) < len(members):
                    failed('never-degraded',
                           'the ' + leg + ' never surfaced on the '
                           'served register-driver diagnostics of both '
                           'members while the leg held the device '
                           'down — the last trace entries read '
                           + json.dumps(degrade[-2:])[:400])
                    evidence['digest'] = digest()
                    return evidence['digest'], violations, evidence
                marks['degraded'] = 'link-down-named-counted'
                severed.update(hits)
                subject['thaw_device']()
                frozen = False
        except ConnectionError:
            raise
        except Exception as exc:
            raise ConnectionError('the ' + leg + ' staging lever never '
                                  'completed: ' + str(exc)[:300])

        # The recovery: the first serve on each member reporting the
        # link connected after the outage was counted must land inside
        # the documented bound — on both members, never one.
        recovery, hits, silent = watch(reattached, BUS_REATTACH_BOUND)
        evidence['recovery_trace'] = recovery
        evidence['degraded_members'] = sorted({
            entry['member'] for entry in
            evidence.get('degrade_trace', []) + recovery
            if entry.get('link') == 'disconnected'})
        evidence['device_serving'] = _device_serving(subject)
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
            return abandoned(evidence.get('degrade_trace', []), recovery)
        marks['outage'] = 'counted'
        marks['recovered'] = 're-attached'
        evidence['first_connected'] = {
            name: {'tick': hits[name].get('tick'),
                   'driver': _driver_health(hits[name]),
                   'io_health': hits[name].get('io_health')}
            for name in members}
        seen = {name: any(entry.get('fault') for entry in
                          evidence.get('degrade_trace', []) + recovery
                          if entry['member'] == name)
                for name in members}
        evidence['counted_mid'] = seen

        _bus_reattach_audit(members, hits, baseline, ticks, leg,
                            seen, marks, note)
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


def _bus_reattach_audit(members, hits, baseline, ticks, leg, seen,
                        marks, note):
    """The recovered-serve assertions — runnable against planted hits
    in the self-check. `hits` maps each member to the first serve
    reporting its link connected after the outage; the first such
    serve must already carry the cleared standing record, the reset
    boundary streak, the kept cumulative history and recorded fault,
    and a served tick advanced past the baseline — on every member,
    never one. `baseline` maps each member to the serve the outage
    ran from, whose counted total is what the recovery must have
    moved; `seen` names the members whose own watch saw a stamped
    boundary fault during the outage, which is what separates an
    accounting that reset what it had counted from one that never
    saw the outage at all. `note(key, diagnostic, detail)` records
    each clause the hits violate; a stalled tick also marks the
    digest's cadence."""
    def failed(key, detail):
        note(key, 'sim-bus-reattach-failed', detail)

    for name in members:
        health = hits[name].get('io_health') or {}
        driver = _driver_health(hits[name]) or {}
        before = (baseline.get(name) or {}).get('io_health') or {}
        counted, was = _counted(health), _counted(before)
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
        if counted < was or (counted == was and seen.get(name)):
            # The matched serve is the first one past the outage, so a
            # total back at (or below) the baseline is history the
            # accounting dropped rather than history it never took.
            failed('history-lost-' + name,
                   name + '\'s counted outage history reset across '
                   'the recovery — the cumulative counters or the '
                   'recorded fault dropped what they counted before '
                   'the ' + leg + ': ' + json.dumps(health)[:400])
        elif counted == was or not health.get('last_error'):
            failed('never-counted-' + name,
                   name + '\'s io_health never counted the ' + leg
                   + ' — the boundary counters stand at the baseline '
                   'total ' + str(was) + ' and the boundary fault was '
                   'never recorded: ' + json.dumps(health)[:400])
        if not (hits[name].get('tick') or 0) > (ticks[name] or 0):
            marks['cadence'] = 'stalled'
            failed('cadence-' + name,
                   name + '\'s served tick did not advance across '
                   'the ' + leg + ' — the scan cadence held at '
                   + str(hits[name].get('tick')))


def _bus_reattach_self_check():
    """The leg's unchecked-diagnostic self-test: replay the recovered-
    link audit over each planted negative the contract names — a
    standing last_error behind the restored link, a streak still
    standing, the counted history rewound, the outage uncounted, the
    tick held at the baseline — and require each to trip
    sim-bus-reattach-failed while the clean record trips nothing.
    Returns the planted case names the audit let through or wrongly
    named."""
    def snap(tick, driver_error=None, streak=0, reads=9, writes=2,
             fault='faulted'):
        return {'tick': tick,
                'io_health': {'failed_reads': reads,
                              'failed_writes': writes,
                              'consecutive_failures': streak,
                              'last_error': fault,
                              'driver': {'link': 'connected',
                                         'last_error': driver_error}}}

    members = ('active', 'standby')
    baseline = {name: {'io_health': {'failed_reads': 7,
                                     'failed_writes': 2,
                                     'last_error': None}}
                for name in members}
    ticks = {name: 4 for name in members}
    clean = {name: snap(9) for name in members}
    counted = {name: True for name in members}
    plants = {'clean': (clean, counted, False),
              'lingered': ({**clean, 'active': snap(
                  9, driver_error='connection reset by peer')},
                  counted, True),
              'streak-stood': ({**clean, 'active': snap(9, streak=3)},
                               counted, True),
              'history-lost': ({**clean, 'active': snap(
                  9, reads=0, fault=None)}, counted, True),
              'never-counted': ({**clean, 'active': snap(
                  9, reads=7, writes=2, fault=None)},
                  {**counted, 'active': False}, True),
              'cadence': ({**clean, 'active': snap(4)}, counted, True)}
    slipped = []
    for name, (hits, seen, expect) in plants.items():
        violations = {}
        _bus_reattach_audit(
            members, hits, baseline, ticks, 'device-restart', seen,
            {'cadence': 'held'},
            lambda key, diagnostic, detail:
                violations.setdefault(key, (diagnostic, detail)))
        tripped = any(diagnostic == 'sim-bus-reattach-failed'
                      for diagnostic, _ in violations.values())
        if tripped != expect:
            slipped.append(name)
    return slipped


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
        return bool(wait_for(lambda: _bus_owner(ctx, subject) == key,
                             time.monotonic() + BUS_REATTACH_SETTLE,
                             interval=BUS_REATTACH_POLL))

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


def _bus_field(ctx):
    """Stage the lane's register-protocol device server on its point-
    wise model and prove the field it serves is the one this contract
    is about: the staged device declared `sim-bus` — the driver's own
    kind; a `sim-cyclic` fixture would exercise the cyclic driver's
    separate recovery and say nothing here. The staged document gets
    the leg's per-request timeout stamped onto it, so the stall
    class's freeze produces a failed exchange to recover from.
    Returns the launch dict, or the inconclusive reason as a string. A
    launch that raises is no inconclusive reason but a refused staging
    call: it propagates so the pass records it as the instability it
    is."""
    spec = ctx.get('sim_bus_device')
    if not spec:
        return ('the run config stages no sim-bus device server — this '
                'leg needs the lane\'s register-protocol field')
    field = ctx['start_sim_bus_device'](timeout_ms=BUS_FIELD_TIMEOUT)
    document = field.get('model')
    try:
        model = json.loads(Path(document).read_text())
    except (OSError, ValueError, TypeError) as exc:
        return 'the staged device model is unreadable: ' + str(exc)[:200]
    for declared in model.get('devices') or []:
        if declared.get('id') != spec.get('device'):
            continue
        if declared.get('kind') != BUS_KIND:
            return ('the staged field serves device '
                    + str(spec.get('device')) + ' as '
                    + repr(declared.get('kind')) + ' — point '
                    'sim_bus_device.model_fixture at a model declaring '
                    'a ' + BUS_KIND + ' device for this leg')
        return field
    return 'the staged device model declares no device ' \
        + str(spec.get('device'))


def _bus_teardown(ctx):
    """Best-effort teardown: both launched seats and the device
    server — a clean run leaves nothing standing, and an aborted one
    gets the same sweep so the legs behind this one find their seats
    free."""
    for seat in (OWNER_SEAT, PEER_SEAT):
        try:
            ctx['stop_born_controller'](seat)
        except Exception:
            pass
    try:
        ctx['stop_sim_bus_device']()
    except Exception:
        pass


def scenario_sim_bus_driver_reattach(ctx):
    """Sever the rig's register-mapped device twice over — a device
    restart and a ~2 s stall — with a controller pair attached, and
    assert the point-wise BusDriver recovers from both by lazy
    re-attach: each outage is counted in the served io_health and the
    link returns to connected inside the documented bound with the
    standing record cleared, the history kept, and the scan cadence
    resumed without a rewind, the launch roles hold, and POST
    /promote on the converged standby answers inside its bound once
    the backend is back — each class twice with identical digests,
    the launch roles restored afterward."""
    case = Case('sim-bus-driver-reattach',
                'The point-wise register driver re-attaches after a '
                'device restart and a brief stall',
                'with a controller pair settled on the rig\'s '
                'register-mapped device, a device restart and a ~2 s '
                'stall each count in the served io_health of both '
                'members — the boundary counters advanced over the '
                'baseline and the fault recorded — and the driver '
                'then re-attaches inside the documented bound, the '
                'first serve reporting the link connected already '
                'carrying the cleared record and the reset streak '
                'while the cumulative counters and the recorded fault '
                'keep the outage\'s history and the served tick '
                'advances without rewinding; the stall additionally '
                'surfaces on both members as disconnected with the '
                'severing failure named, on a window the leg holds '
                'the device down for; the pair\'s launch roles hold '
                'throughout, and POST /promote on the converged '
                'standby answers inside its bound once the backend is '
                'back — a second pass over both classes reproducing '
                'the digests exactly')
    try:
        missing = [key for key in ('start_sim_bus_device',
                                   'restart_sim_bus_device',
                                   'stop_sim_bus_device',
                                   'freeze_sim_bus_device',
                                   'thaw_sim_bus_device',
                                   'sim_bus_device_serving',
                                   'start_born_controller',
                                   'stop_born_controller')
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
        try:
            field = _bus_field(ctx)
        except Exception as exc:
            return case.finish('inconclusive', 'the device server '
                               'never staged: ' + str(exc)[:250])
        if isinstance(field, str):
            return case.finish('inconclusive', field)
        try:
            # The staged pair: the owner seat born-active declaring
            # its pair member, the peer seat tracking it — a
            # register-protocol model carries its device address in
            # its parameters, so the pair needs no --remote.
            ctx['start_born_controller'](OWNER_SEAT, None,
                                         document=field['model'],
                                         peer=PEER_SEAT)
            ctx['start_born_controller'](PEER_SEAT, None,
                                         document=field['model'],
                                         standby=OWNER_SEAT)
        except Exception as exc:
            return case.finish('inconclusive', 'the pair launch never '
                               'ran: ' + str(exc)[:250])
        try:
            subject = {'active': ctx[OWNER_SEAT],
                       'standby': ctx[PEER_SEAT],
                       'device': field['device'],
                       'restart_device': ctx['restart_sim_bus_device'],
                       'freeze_device': ctx['freeze_sim_bus_device'],
                       'thaw_device': ctx['thaw_sim_bus_device'],
                       'device_serving': ctx['sim_bus_device_serving']}
            verdict = _bus_reattach_run(ctx, case, subject)
        finally:
            _bus_teardown(ctx)
        return verdict
    except Exception as exc:
        return case.finish('inconclusive', str(exc))


def _bus_reattach_run(ctx, case, subject):
    """The leg's body once the rig is staged: settle the launched pair,
    drive both outage classes twice through `_bus_outage`, compare the
    normalized digests, run the unchecked self-check, and probe the
    promotion path on the converged standby — returning the case's
    finished record."""
    try:
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
                    ctx['evidence_dir'], 'sim-bus-driver-reattach-'
                    + leg
                    + '-pass-' + str(number) + '.json', evidence)
                case.evidence('file', ref, leg + ', pass '
                              + str(number) + ' — the baseline, the '
                              'held-freeze and recovery watches and the '
                              'normalized digest')
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
                             + ': outage counted, then re-attached '
                             'inside ' + str(BUS_REATTACH_BOUND) + 's')
        diverged = {leg: passes[leg] for leg in passes
                    if passes[leg][0] != passes[leg][1]}
        if diverged:
            return case.finish(
                'failed', 'sim-bus-reattach-nondeterministic: the two '
                'passes\' digests diverged: '
                + json.dumps(diverged, sort_keys=True))
        ref = save_evidence(ctx['evidence_dir'],
                            'sim-bus-driver-reattach-digest.json',
                            {'passes': passes})
        case.evidence('file', ref, 'the normalized deterministic digest '
                      'both passes of each outage class produced')

        # The unchecked self-check: the recovered-link audit, replayed
        # over each planted negative the contract names, must trip —
        # a silent audit can no longer be trusted to catch what it
        # names.
        slipped = _bus_reattach_self_check()
        if slipped:
            return case.finish(
                'failed', 'sim-bus-reattach-unchecked: the '
                'recovered-link audit stayed silent on, or wrongly '
                'named, the planted negatives: ' + ', '.join(slipped))
        case.observe('the self-check leg\'s planted negatives each '
                     'named sim-bus-reattach-failed')

        probe = _bus_promote_probe(ctx, subject, owner, tracker)
        ref = save_evidence(ctx['evidence_dir'],
                            'sim-bus-driver-reattach-promote.json',
                            probe)
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
