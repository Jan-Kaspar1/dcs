"""The monitor_starvation acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: The monitor-starvation case runs in the same armed window: it needs
# ctrl-b — the only peer launched --auto-promote — as the tracking
# standby whose checkpoint pulls measure the starved ctrl-a monitor,
# and it leaves the launch roles untouched, so it must run before the
# tune case's a->b switch.
RUNS_BEFORE = frozenset({'scenario_parameter_tune_carryover'})


# --------------------------------------------------------------------
# The serving-lane resilience contract (WW-FND-004, WW-LCM-001 — the
# settled #624 lane split proven end to end on the armed deployed
# pair): the monitor quarantines the body-reading handlers — POST
# /command, POST /scan, and any request still owed a body — onto a
# bounded submission pool behind MAX_REQUEST_BODY, so a stalled-body
# flood pins only that lane while the SERVE_WORKERS serving lane keeps
# answering — GET /checkpoint among it, the heartbeat the armed
# tracking standby measures the active's liveness by. Before the fix
# the same flood pinned every worker, timed out /role for the whole
# hold, and on an armed rig read as a dead active: the standby's
# heartbeat pulls missed, it self-promoted inside the armed budget,
# claimed the field, and fenced a live active — transport-level
# starvation indistinguishable from active death. The leg opens the
# saturating set of incomplete-body connections against the settled
# active's monitor; through the hold the liveness reads keep answering
# inside the declared per-request bound while the standby's checkpoint
# pulls keep landing — its reported tracking alignment advancing, no
# role transition on either peer, no role_changed or field_claim_lost
# journaled, and the plant's writer claim still fencing third-party
# probes; closing the set frees the submission lane — a receipted
# command settles applied and the pace-driven scans keep landing —
# with the pair's roles unchanged throughout. Two consecutive passes
# must produce identical digests. The diagnostics name the miss by
# class: monitor-starvation-failed is the serving contract not
# performing — a liveness read never answering, the standby's pulls
# stalling, a fencing probe unanswered, the recovery never landing;
# monitor-starvation-nondeterministic is the run producing a result
# the contract declares impossible — a role transition or
# field_claim_lost, a probe writing through the standing claim, a
# regressed alignment, an answer past the declared bound, or two
# passes disagreeing.

STARVE_CONNECTIONS = 8   # the saturating set — past both lanes' worker
                         # pools summed, the shape the reproduction
                         # pinned every worker with
STARVE_PULL_ADVANCE = 15  # checkpoint-pull advances the hold must
                          # witness on the armed standby — ~1.5s of
                          # landed heartbeats at the rig's scan cadence
STARVE_SETTLE = 30       # bound on the pair reporting its settled
                         # armed tracking layout before the leg runs
STARVE_DEADLINE = 20     # bound on one pass's hold — past the armed
                         # 120-miss budget at the rig's scan cadence,
                         # so a starved heartbeat produces the spurious
                         # promotion inside the window; a heartbeat
                         # that never lands the witnessed pulls starved
STARVE_POLL = 0.4        # sampling cadence inside the hold
STARVE_RECOVER = 20      # bound on the post-close settlement and the
                         # resumed scan cadence
STARVE_ENDPOINTS = ('/role', '/snapshot', '/checkpoint')

# The reproduction's pinning shape: headers a body-reading handler must
# block on, the declared body never following — an Expect request's
# held reader and a chunked scan's unterminated first chunk. Each holds
# its submission-lane worker until the connection closes.
STARVE_REQUESTS = (
    b'POST /command HTTP/1.1\r\nHost: q\r\nContent-Length: 16\r\n'
    b'Expect: 100-continue\r\n\r\n',
    b'POST /scan HTTP/1.1\r\nHost: q\r\nTransfer-Encoding: chunked\r\n'
    b'\r\n10\r\n')


def _starvation_flood(base):
    """The saturating set of incomplete-body connections against a
    monitor base — each carrying a pinning request whose declared body
    never follows. Returns the open sockets; raises OSError when the
    set never opens."""
    streams = []
    try:
        for index in range(STARVE_CONNECTIONS):
            stream = _connect(base)
            stream.sendall(
                STARVE_REQUESTS[index % len(STARVE_REQUESTS)])
            streams.append(stream)
    except OSError:
        for stream in streams:
            stream.close()
        raise
    return streams


def _starvation_pass(ctx, active, peer, point, value, floors):
    """One starvation pass: flood the active's submission lane, hold
    the window while the serving reads, the armed standby's checkpoint
    pulls, and the field's fencing probes are sampled — the hold ends
    once the witnessed pulls prove the heartbeat kept landing — then
    close and prove the submission lane recovered. Returns (digest,
    violations, evidence): digest is the pass's normalized verdict
    record, identical across clean passes; violations is
    {key: (diagnostic, detail)} in first-seen order."""
    base, peer_base = ctx[active], ctx[peer]
    violations = {}
    evidence = {}

    def note(key, diagnostic, detail):
        violations.setdefault(key, (diagnostic, detail))

    stats = {path: {'answered': 0, 'within': 0}
             for path in STARVE_ENDPOINTS}
    tracking = {'first': None, 'aligned': None, 'lost': False}
    fencing = {'answered': 0, 'unfenced': 0}
    snaps = []
    journal_clean = True
    try:
        streams = _starvation_flood(base)
    except OSError as exc:
        note('flood', 'monitor-starvation-failed',
             'the saturating set never opened: ' + str(exc)[:200])
        return None, violations, evidence
    try:
        # The hold: sample until the standby's witnessed pulls prove
        # the heartbeat kept landing — or the deadline names the
        # starvation.
        deadline = time.monotonic() + STARVE_DEADLINE
        while time.monotonic() < deadline:
            for path in STARVE_ENDPOINTS:
                try:
                    body, elapsed = _timed(
                        lambda: http_json('GET', base + path,
                                          timeout=LATENCY_BOUND)[1])
                except Exception as exc:
                    note('read-' + path, 'monitor-starvation-failed',
                         'GET ' + path + ' never answered inside the '
                         + str(LATENCY_BOUND) + 's bound under the '
                         'hold: ' + str(exc)[:150])
                    continue
                stats[path]['answered'] += 1
                if elapsed <= LATENCY_BOUND:
                    stats[path]['within'] += 1
                else:
                    note('late-' + path,
                         'monitor-starvation-nondeterministic',
                         'GET ' + path + ' answered in '
                         + format(elapsed, '.2f') + 's past the '
                         + str(LATENCY_BOUND) + 's bound under the '
                         'hold')
                if path == '/snapshot':
                    snaps.append(body.get('tick')
                                 if isinstance(body, dict) else None)
            report = _try_role(ctx, peer_base)
            if report is None:
                note('peer-role', 'monitor-starvation-failed',
                     'the armed standby\'s own monitor never answered '
                     '/role through the hold')
            elif report.get('role') != 'standby':
                tracking['lost'] = True
                note('peer-moved',
                     'monitor-starvation-nondeterministic',
                     'the armed standby moved to role '
                     + str(report.get('role')) + ' under the hold — '
                     'the failover transition the lane split exists '
                     'to prevent')
            else:
                aligned = _tracking_aligned(report)
                if aligned is None:
                    note('sync-lost', 'monitor-starvation-failed',
                         'the standby\'s sync left tracking under the '
                         'hold — its checkpoint pulls stopped landing')
                elif tracking['first'] is None:
                    tracking['first'] = aligned
                    tracking['aligned'] = aligned
                elif tracking['aligned'] is not None \
                        and aligned < tracking['aligned']:
                    note('regressed',
                         'monitor-starvation-nondeterministic',
                         'the standby\'s reported alignment regressed '
                         'mid-hold: ' + str(aligned) + ' below '
                         + str(tracking['aligned']))
                else:
                    tracking['aligned'] = aligned
            probe = _try_plant(ctx, {'op': 'step', 'dt': 0})
            if probe is not None:
                fencing['answered'] += 1
                if not _fenced(probe):
                    fencing['unfenced'] += 1
                    note('unfenced',
                         'monitor-starvation-nondeterministic',
                         'a third-party probe wrote through the '
                         'standing writer claim under the hold')
            if tracking['aligned'] is not None \
                    and tracking['first'] is not None \
                    and tracking['aligned'] - tracking['first'] \
                    >= STARVE_PULL_ADVANCE:
                break
            time.sleep(STARVE_POLL)
        # The journal audit — read through the still-open flood; it is
        # itself a serving-lane read.
        for name, floor in floors.items():
            try:
                _, journal = http_json(
                    'GET', ctx[name] + '/journal?since=' + str(floor),
                    timeout=LATENCY_BOUND)
            except Exception as exc:
                journal_clean = False
                note('journal-' + name, 'monitor-starvation-failed',
                     name + '\'s journal never served through the '
                     'hold: ' + str(exc)[:150])
                continue
            for entry in _journal_list(journal):
                event = entry.get('event') or {}
                if 'role_changed' in event:
                    journal_clean = False
                    note('journaled-role-' + name,
                         'monitor-starvation-nondeterministic',
                         name + ' journaled a role_changed event '
                         'through the hold')
                if 'field_claim_lost' in event:
                    journal_clean = False
                    note('journaled-claim-' + name,
                         'monitor-starvation-nondeterministic',
                         name + ' journaled a field_claim_lost event '
                         'through the hold')
    finally:
        for stream in streams:
            stream.close()

    # The hold's verdicts.
    for path, entry in stats.items():
        if entry['answered'] == 0:
            note('served-' + path, 'monitor-starvation-failed',
                 'GET ' + path + ' never answered under the hold')
    moved = 'peer-moved' in violations or 'sync-lost' in violations
    if not moved:
        if tracking['first'] is None:
            note('pulls-never', 'monitor-starvation-failed',
                 'the standby\'s checkpoint pulls never landed '
                 'through the hold — the heartbeat the armed budget '
                 'counts')
        elif tracking['aligned'] - tracking['first'] \
                < STARVE_PULL_ADVANCE:
            note('pulls-stalled', 'monitor-starvation-failed',
                 'the standby\'s checkpoint pulls stalled through '
                 'the hold — ' + str(tracking['aligned']
                                     - tracking['first'])
                 + ' witnessed advances, the heartbeat the armed '
                 + str(ctx.get('failover_misses'))
                 + '-miss budget counts')
    ticks = [tick for tick in snaps if isinstance(tick, int)]
    if snaps and not ticks:
        note('scans-tickless', 'monitor-starvation-failed',
             'the served snapshots carried no run tick under the '
             'hold')
    elif len(ticks) > 1 and ticks[-1] <= ticks[0]:
        note('scans-stalled', 'monitor-starvation-failed',
             'the active\'s pace-driven scans stalled under the '
             'hold — /snapshot\'s tick never advanced')
    if fencing['answered'] == 0:
        note('fencing', 'monitor-starvation-failed',
             'the field never answered a fencing probe through the '
             'hold')

    # Recovery: the flood closed, the submission lane frees — the
    # receipted path, the driven-scan endpoint's documented refusal,
    # and the paced cadence all return.
    recovery = {}
    floor = floors.get(active, 0)
    try:
        index = _next_receipt_index(ctx, base)
        status, receipt = http_json(
            'POST', base + '/command',
            {'command': {'write_value': {'point': point, 'kind': 'bool',
                                         'value': {'bool': value}}},
             'actor': 'qa-lane'})
        recovery['command'] = {'status': status,
                               'outcome': _outcome_key(receipt)}
        if status != 200 \
                or recovery['command']['outcome'] != 'accepted':
            note('recovery-command', 'monitor-starvation-failed',
                 'the recovery command answered '
                 + recovery['command']['outcome'] + ' — the '
                 'submission lane never freed')
        else:
            settled = wait_for(
                lambda: _settled_outcome(ctx, base, index),
                time.monotonic() + STARVE_RECOVER)
            recovery['settled'] = settled
            if settled != 'applied':
                note('recovery-settled', 'monitor-starvation-failed',
                     'the recovery command never settled applied: '
                     + str(settled))

            def covered():
                try:
                    _, journal = http_json(
                        'GET', base + '/journal?since=' + str(floor))
                except Exception:
                    return None
                return _journal_covers(journal, point) or None
            if wait_for(covered,
                        time.monotonic() + STARVE_RECOVER) is None:
                note('recovery-journal', 'monitor-starvation-failed',
                     'the settled recovery command never journaled')
    except Exception as exc:
        note('recovery-command', 'monitor-starvation-failed',
             'the submission lane never answered a command after the '
             'hold: ' + str(exc)[:150])
    try:
        scan_status, _ = _request_status('POST', base + '/scan',
                                         {'scans': 1})
    except Exception:
        scan_status = None
    recovery['scan_status'] = scan_status
    if scan_status != 409:
        note('recovery-scan', 'monitor-starvation-failed',
             'the paced monitor\'s documented POST /scan refusal '
             'never answered after the hold — the submission lane '
             'never freed: ' + str(scan_status))
    last_tick = ticks[-1] if ticks else None

    def resumed():
        snap = _try_snapshot(ctx, base)
        if not isinstance(snap, dict) or last_tick is None:
            return None
        return snap.get('tick') if isinstance(snap.get('tick'), int) \
            and snap['tick'] > last_tick else None
    if last_tick is not None \
            and wait_for(resumed,
                         time.monotonic() + STARVE_RECOVER) is None:
        note('recovery-scans', 'monitor-starvation-failed',
             'the pace-driven scans never resumed after the hold')
    last_aligned = tracking['aligned']

    def converged():
        report = _try_role(ctx, peer_base)
        if (report or {}).get('role') != 'standby':
            return None
        aligned = _tracking_aligned(report)
        if aligned is None or last_aligned is not None \
                and aligned <= last_aligned:
            return None
        return aligned
    if not tracking['lost'] \
            and wait_for(converged,
                         time.monotonic() + STARVE_RECOVER) is None:
        note('recovery-tracking', 'monitor-starvation-failed',
             'the standby\'s checkpoint pulls never resumed landing '
             'after the hold')
    roles = {}
    for name in (active, peer):
        report = _try_role(ctx, ctx[name])
        roles[name] = (report or {}).get('role')
    if roles[active] is None:
        note('roles-active', 'monitor-starvation-failed',
             'the field owner\'s /role never answered after the '
             'hold')
    elif roles[active] != 'active':
        note('roles-active', 'monitor-starvation-nondeterministic',
             'the field owner left active role: '
             + str(roles[active]))
    if roles[peer] is None:
        note('roles-peer', 'monitor-starvation-failed',
             'the armed standby\'s /role never answered after the '
             'hold')
    elif roles[peer] != 'standby' and 'peer-moved' not in violations:
        note('roles-peer', 'monitor-starvation-nondeterministic',
             'the armed standby left standby role: '
             + str(roles[peer]))
    recovery['roles'] = roles
    evidence.update({
        'connections': STARVE_CONNECTIONS,
        'armed_miss_budget': ctx.get('failover_misses'),
        'stats': stats, 'snaps': snaps, 'tracking': tracking,
        'fencing': fencing, 'recovery': recovery,
        'violations': {key: name for key, (name, _)
                       in violations.items()}})
    evidence['digest'] = {
        'reads': {path: ('starved' if not e['answered']
                         else 'bounded'
                         if e['within'] == e['answered'] else 'late')
                  for path, e in stats.items()},
        'scans': ('advanced'
                  if ticks and ticks[-1] > ticks[0] else 'stalled'),
        'tracking': ('lost' if tracking['lost']
                     else 'regressed' if 'regressed' in violations
                     else 'advanced'
                     if tracking['first'] is not None
                     and tracking['aligned'] - tracking['first']
                     >= STARVE_PULL_ADVANCE
                     else 'stalled'),
        'fencing': ('unanswered' if not fencing['answered']
                    else 'breached' if fencing['unfenced']
                    else 'fenced'),
        'journal': 'clean' if journal_clean else 'transitioned',
        'recovery': ('settled'
                     if recovery.get('settled') == 'applied'
                     and 'recovery-scan' not in violations
                     else 'unsettled'),
        'roles': ('moved'
                  if {roles[active], roles[peer]}
                  - {'active', 'standby', None}
                  else 'unanswered' if None in (roles[active],
                                                roles[peer])
                  else 'unchanged')}
    return evidence['digest'], violations, evidence


def scenario_monitor_starvation(ctx):
    """Saturate the settled active's monitor with incomplete request
    bodies — the #624 reproduction's pinning shape — and prove the
    serving lane keeps answering through the hold on the armed rig:
    the liveness reads inside the declared bound, the tracking
    standby's checkpoint pulls landing, neither peer transitioning,
    the field's writer claim fencing probes; then closing the set
    restores the submission lane's command and scan serving."""
    case = Case('monitor-starvation',
                'Serving lane survives incomplete-body saturation',
                'with the armed tracking standby settled, the '
                'saturating set of incomplete-body connections '
                'against the active\'s monitor leaves GET /role, '
                '/snapshot, and /checkpoint answering inside the '
                'declared ' + str(LATENCY_BOUND) + 's bound while '
                'the standby\'s checkpoint pulls keep landing — no '
                'role transition or field_claim_lost on either peer '
                'and the plant\'s writer claim fencing third-party '
                'probes — and closing the set restores full command '
                'and scan serving: a receipted command settling '
                'applied and the pace-driven scans resuming, the '
                'pair\'s roles unchanged; two consecutive passes '
                'produce identical digests')
    try:
        if ctx.get('plant') is None:
            return case.finish('inconclusive',
                               'no simulated plant endpoint — the '
                               'fencing probes cannot run')
        deadline = time.monotonic() + STARVE_SETTLE
        active = wait_for(lambda: _settled_active(ctx), deadline)
        if active is None:
            return case.finish('failed',
                               'no peer reports role=active')
        if active != 'active':
            return case.finish(
                'inconclusive', 'the pair\'s roles are already '
                'switched — the armed standby no longer tracks the '
                'peer this leg starves')
        peer = wait_for(lambda: _tracking_peer(ctx, active), deadline)
        if peer != 'standby':
            return case.finish(
                'inconclusive', 'no armed tracking standby — the '
                '--auto-promote heartbeat half of the contract '
                'cannot run')
        base = ctx[active]
        _, signals = http_json('GET', base + '/signals')
        target = _writable_bool_point(signals)
        if target is None or target.get('point') is None:
            return case.finish('failed', 'no writable boolean point '
                               'for the recovery command')
        point = target['point']
        snap = _try_snapshot(ctx, base) or {}
        baseline = _point_value(snap, point)
        value = baseline if isinstance(baseline, bool) else True
        floors = {}
        for name in (active, peer):
            try:
                _, journal = http_json('GET', ctx[name] + '/journal')
            except Exception as exc:
                return case.finish('inconclusive',
                                   name + '\'s journal floor never '
                                   'served: ' + str(exc)[:200])
            entries = _journal_list(journal)
            floors[name] = (entries[-1].get('seq') or 0) \
                if entries else 0
        digests = []
        for number in (1, 2):
            digest, violations, evidence = _starvation_pass(
                ctx, active, peer, point, value, floors)
            ref = save_evidence(
                ctx['evidence_dir'],
                'monitor-starvation-pass-' + str(number) + '.json',
                evidence)
            case.evidence('file', ref, 'starvation pass '
                          + str(number) + ' — the held window\'s '
                          'sampled verdicts, the fencing probes, and '
                          'the recovery')
            if violations:
                failed = any(name == 'monitor-starvation-failed'
                             for name, _ in violations.values())
                diagnostic = 'monitor-starvation-failed' if failed \
                    else 'monitor-starvation-nondeterministic'
                return case.finish(
                    'failed', diagnostic + ': ' + '; '.join(
                        detail for _, detail in
                        list(violations.values())[:4]))
            digests.append(digest)
        if digests[0] != digests[1]:
            return case.finish(
                'failed', 'monitor-starvation-nondeterministic: the '
                'two passes\' digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two starvation passes, identical digests')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
