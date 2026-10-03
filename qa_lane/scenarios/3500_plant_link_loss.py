"""The plant_link_loss acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: The plant-link-loss case follows later in the schedule: its plant
# container cycling cannot contaminate an earlier case, and whichever
# endpoint owns the field by then keeps it through the outage and
# recovery the scenario drives.
RUNS_AFTER = frozenset({'scenario_failover'})

# --------------------------------------------------------------------
# The plant-link boundary (WW-OPS-003's communication confidence and
# WW-FND-002's remote-I/O evidence, ahead of HQ-5's hardware link-loss
# checks, pinning the settled #507 field-loss contract and the #531
# restart-window half of the claim contract as per-revision lane
# evidence): the runner-owned plant stop/start action severs both
# controllers' remote-driver connections mid-run — the non-cyclic
# remote form of decision 78's exchange-loss shape, and unlike a
# per-point fault a dead plant fails every field point at once at the
# link boundary. The active's telemetry must degrade honestly — held
# values re-marked Bad past the read boundary, the driver link
# reporting disconnected with per-direction failures counted and still
# advancing and a last_error, the loss journaled as quality_changed —
# while scans keep running and the pair's roles hold: the standby's
# checkpoint-pull heartbeat is peer-to-peer, not field traffic, so it
# stays tracking and never promotes on field loss — its promotion path
# still following the convergence gate. A restarted plant is a new
# server lifetime — its single-writer claim died with the old process —
# so the return first serves a fail-closed unclaimed window every
# third attachment's mutation probe must answer `unclaimed` (never
# `stepped`, never a silent write) while reads stay open; recovery
# means the field owner re-attaches and re-arms the recorded claim
# through `ensure_writer`: probes fence again, reads return Good with
# the outage's failures still counted in io_health rather than
# silently reset, and no controller restarts.

LINK_POLL = 1.0                # cadence watching the pair mid-outage
LINK_DEGRADE_DEADLINE = 45     # bound on the telemetry degrading
LINK_SETTLE = 6.0              # extra role watch once degradation shows
LINK_RECOVERY_DEADLINE = 90    # bound on the plant's return + re-claim
LINK_WINDOW_POLL = 0.2         # cadence probing the fail-closed window


def scenario_plant_link_loss(ctx):
    """Stop the run's plant container mid-run, prove the settled
    field-loss contract through the monitor and the durable journal,
    then restart it, prove the fail-closed unclaimed window, and prove
    the recorded owner's re-arm and recovery without a restart."""
    case = Case('plant-link-loss',
                'Plant-link loss degrades honestly and recovers',
                'stopping the run\'s plant container leaves the active '
                'scanning with its field reads Bad at the link '
                'boundary — io_health counting and advancing the '
                'per-direction failures, the driver link reporting '
                'disconnected, a last_error recorded, the loss '
                'journaled as quality_changed — the standby staying '
                'tracking with its promotion path on the convergence '
                'gate; restarting the plant serves a fail-closed '
                'unclaimed window every mutation probe answers '
                'unclaimed — never stepped, never a silent write — '
                'while reads stay open; the recorded owner\'s re-attach '
                're-arms the claim through ensure_writer so probes '
                'fence again, reads return Good with the outage\'s '
                'failures still counted, and no controller restarts')

    def role_violation(name, report, expected_roles, seen):
        ref = save_evidence(ctx['evidence_dir'],
                            'plant-link-loss-roles.json',
                            {'expected': expected_roles,
                             'offender': report, 'seen': seen})
        case.evidence('file', ref)
        return case.finish('failed', 'field loss moved ' + name
                           + ' to role ' + str(report.get('role'))
                           + ': ' + json.dumps(report)[:300])

    try:
        stop = ctx.get('stop_plant')
        start = ctx.get('start_plant')
        if stop is None or start is None or not ctx.get('plant'):
            return case.finish('inconclusive', 'the run context '
                               'carries no plant stop/start action or '
                               'plant address')
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        peer = 'standby' if active == 'active' else 'active'
        base, peer_base = ctx[active], ctx[peer]
        expected = {active: 'active', peer: 'standby'}
        case.observe('field owner: ' + active + ' (' + base + ')')

        # The baseline: the field census names the points the link
        # boundary fails at once, and the fencing probe proves the
        # writer claim the outage must lose and recovery must re-take.
        # The served journal cursor opens the loss-journaled leg, the
        # standby's sync opens the convergence-gate leg, and the
        # baseline health and tick open the advancing-counters and
        # no-restart legs.
        census = _try_plant_ctl(ctx, 'list')
        points = (census or {}).get('points', [])
        field_in = sorted(entry.get('point') for entry in points
                          if entry.get('direction') == 'in')
        field_out = any(entry.get('direction') == 'out'
                        for entry in points)
        probe = _try_plant(ctx, {'op': 'step', 'dt': 0})
        before = _try_snapshot(ctx, base)
        try:
            _, journal_body = http_json('GET', base + '/journal?since=0')
            cursor = _journal_seqs(journal_body)
            cursor = cursor[-1] if cursor else 0
        except Exception as exc:
            return case.finish('inconclusive', 'the active\'s journal '
                               'never answered its baseline cursor: '
                               + str(exc)[:200])
        peer_report = _try_role(ctx, peer_base)
        write_target = None
        for entry in sorted(points, key=lambda item: item.get('point')):
            sample = entry.get('sample') or {}
            if entry.get('direction') == 'in' \
                    and sample.get('value') is not None:
                write_target = (entry.get('point'), sample['value'])
                break
        ref = save_evidence(ctx['evidence_dir'],
                            'plant-link-loss-baseline.json',
                            {'census': census, 'probe': probe,
                             'qualities': {
                                 point: _sample_quality(before or {},
                                                        point)
                                 for point in field_in},
                             'journal_cursor': cursor,
                             'standby_sync': (peer_report or {}).get(
                                 'sync'),
                             'io_health': (before or {}).get('io_health'),
                             'tick': (before or {}).get('tick'),
                             'write_target': write_target})
        case.evidence('file', ref, 'the plant census, the pre-outage '
                      'fencing probe, the baseline qualities, journal '
                      'cursor, standby sync, health, and tick')
        if census is None:
            return case.finish('inconclusive', 'the plant did not '
                               'answer its point census')
        if not field_in:
            return case.finish('inconclusive', 'the plant census '
                               'lists no field input points')
        if not _fenced(probe):
            return case.finish('failed', 'the field held no writer '
                               'claim before the outage — a third '
                               'attachment\'s mutation probe answered '
                               + json.dumps(probe)[:300])
        fresh = {point: _sample_quality(before or {}, point)
                 for point in field_in}
        if before is None or any(q != 'good' for q in fresh.values()):
            return case.finish('inconclusive', 'the rig never showed '
                               'a healthy field baseline: '
                               + json.dumps(fresh, sort_keys=True))
        if peer_report is None:
            return case.finish('inconclusive', 'the standby peer never '
                               'answered /role — the convergence-gate '
                               'leg has no observation point')
        if peer_report.get('role') != 'standby' \
                or 'tracking' not in (peer_report.get('sync') or {}):
            return case.finish('inconclusive', 'the pair never settled '
                               'tracking — ' + peer + ' reports '
                               + str(peer_report.get('role')) + ' '
                               + json.dumps(peer_report.get('sync'))[:200])
        health0 = (before or {}).get('io_health') or {}
        case.observe('field inputs ' + json.dumps(field_in)
                     + ' reading good under the active\'s claim; '
                     'standby tracking at journal cursor '
                     + str(cursor))

        # The runner-owned lifecycle action: docker stop on the run's
        # plant container, recorded on the run's action timeline.
        try:
            stop()
        except Exception as exc:
            return case.finish('inconclusive', 'the plant stop action '
                               'never completed: ' + str(exc)[:300])
        case.observe('plant container stopped; watching the pair '
                     'through the outage')

        outage = {'roles': {}, 'snapshots': 0, 'silent': 0,
                  'tail_silent': 0, 'first_tick': None, 'tick': None,
                  'qualities': {}, 'health': None, 'health_trace': []}
        degraded_at = None
        deadline = time.monotonic() + LINK_DEGRADE_DEADLINE
        while time.monotonic() < deadline:
            for name, url in ((active, base), (peer, peer_base)):
                report = _try_role(ctx, url)
                if report is None:
                    continue
                outage['roles'][name] = report
                if report.get('role') != expected[name]:
                    return role_violation(name, report, expected,
                                          outage['roles'])
            snap = _try_snapshot(ctx, base)
            if snap is None:
                outage['silent'] += 1
                outage['tail_silent'] += 1
            else:
                outage['snapshots'] += 1
                outage['tail_silent'] = 0
                tick = snap.get('tick') or 0
                if outage['first_tick'] is None:
                    outage['first_tick'] = tick
                outage['tick'] = tick
                outage['qualities'] = {
                    point: _sample_quality(snap, point)
                    for point in field_in}
                outage['health'] = snap.get('io_health')
                seen = snap.get('io_health') or {}
                outage['health_trace'].append(
                    {'tick': tick,
                     'failed_reads': seen.get('failed_reads', 0),
                     'failed_writes': seen.get('failed_writes', 0)})
            if outage['snapshots'] == 0 and outage['silent'] >= 3:
                break  # the monitor is gone — the run aborted
            health = outage['health'] or {}
            degraded = outage['snapshots'] > 0 \
                and outage['tick'] > outage['first_tick'] \
                and all(q not in (None, 'good')
                        for q in outage['qualities'].values()) \
                and health.get('failed_reads', 0) > 0 \
                and (health.get('driver') or {}).get('link') \
                == 'disconnected'
            if degraded:
                if degraded_at is None:
                    degraded_at = time.monotonic()
                elif time.monotonic() - degraded_at > LINK_SETTLE:
                    break
            time.sleep(LINK_POLL)
        ref = save_evidence(ctx['evidence_dir'],
                            'plant-link-loss-outage.json', outage)
        case.evidence('file', ref, 'the pair\'s served state through '
                      'the outage')
        health = outage['health'] or {}
        if outage['snapshots'] == 0:
            return case.finish('failed', 'the active\'s monitor never '
                               'answered after the plant stop — field '
                               'loss aborted the run rather than '
                               'degrading its telemetry')
        if outage['tail_silent']:
            return case.finish('failed', 'the active\'s monitor '
                               'stopped answering during the outage — '
                               'the run aborted on field loss')
        if not outage['tick'] > outage['first_tick']:
            return case.finish('failed', 'the active\'s scans did not '
                               'continue through the outage — the '
                               'served tick held at '
                               + str(outage['tick']))
        still_fresh = [str(point) for point, q
                       in outage['qualities'].items() if q == 'good']
        if still_fresh:
            return case.finish('failed', 'field inputs kept reading '
                               'good through the outage: '
                               + ','.join(still_fresh))
        not_bad = {str(point): q for point, q
                   in outage['qualities'].items()
                   if q != 'bad:communication_fault'}
        if not_bad:
            return case.finish('failed', 'field inputs never reached '
                               'Bad(communication_fault) through the '
                               'outage: '
                               + json.dumps(not_bad, sort_keys=True))
        if health.get('failed_reads', 0) == 0 \
                or (health.get('driver') or {}).get('link') \
                != 'disconnected' \
                or not health.get('last_error'):
            return case.finish('failed', 'io_health lacks the counted '
                               'link failure: '
                               + json.dumps(health)[:400])
        if field_out and health.get('failed_writes', 0) == 0:
            return case.finish('failed', 'io_health counted the read '
                               'failures but no write failures though '
                               'the field serves output points: '
                               + json.dumps(health)[:400])
        trace = outage['health_trace']
        reads_grew = len(trace) >= 2 \
            and trace[-1]['failed_reads'] > trace[0]['failed_reads'] \
            and trace[-1]['failed_reads'] \
            > (health0.get('failed_reads') or 0)
        writes_grew = (not field_out) or (
            len(trace) >= 2
            and trace[-1]['failed_writes']
            > trace[0]['failed_writes'])
        if not reads_grew or not writes_grew:
            return case.finish('failed', 'io_health counters never '
                               'advanced through the outage — the loss '
                               'counted once but never grew: '
                               + json.dumps(trace[-3:])[:300])
        writes_held = all(entry['failed_writes']
                          >= trace[0]['failed_writes']
                          for entry in trace)
        if not writes_held:
            return case.finish('failed', 'io_health lost failures '
                               'through the outage — the counters '
                               'regressed: '
                               + json.dumps(trace[-3:])[:300])
        outage_reads = health.get('failed_reads', 0)
        outage_writes = health.get('failed_writes', 0)
        case.observe('degradation confirmed by tick '
                     + str(outage['tick']) + ': '
                     + json.dumps(outage['qualities'], sort_keys=True)
                     + ', io_health ' + json.dumps(health)[:300])

        # The journal leg: the served journal past the baseline cursor
        # must carry the loss as quality_changed on a failed field
        # input — the durable annunciation, not just the live
        # telemetry.
        try:
            _, journal_body = http_json(
                'GET', base + '/journal?since=' + str(cursor))
        except Exception as exc:
            return case.finish('inconclusive', 'the active\'s journal '
                               'never served the outage audit: '
                               + str(exc)[:200])
        journaled = []
        for entry in _journal_list(journal_body):
            observed = _journal_observation(entry)
            if observed is not None and observed[0] == 'quality_changed' \
                    and observed[1] in field_in:
                journaled.append(observed)
        ref = save_evidence(ctx['evidence_dir'],
                            'plant-link-loss-journal.json',
                            {'cursor': cursor,
                             'quality_changed': journaled,
                             'seqs': _journal_seqs(journal_body)})
        case.evidence('file', ref, 'the loss in the served journal '
                      'past the baseline cursor')
        if not journaled:
            return case.finish('failed', 'the loss was never journaled '
                               '— the served journal past seq '
                               + str(cursor) + ' carries no '
                               'quality_changed for a failed field '
                               'input')
        case.observe(str(len(journaled)) + ' quality_changed records '
                     'journal the loss past seq ' + str(cursor))

        # The standby-gate leg: the checkpoint heartbeat is
        # peer-to-peer, so the outage must leave the standby tracking
        # — its promotion path still on the convergence gate, with no
        # spontaneous promotion behind the field loss.
        standby_report = (outage['roles'] or {}).get(peer) \
            or _try_role(ctx, peer_base)
        ref = save_evidence(ctx['evidence_dir'],
                            'plant-link-loss-standby.json',
                            {'report': standby_report})
        case.evidence('file', ref, 'the standby\'s convergence through '
                      'the outage')
        if standby_report is None:
            return case.finish('inconclusive', 'the standby peer never '
                               'answered /role through the outage — '
                               'the convergence-gate leg has no '
                               'observation point')
        if standby_report.get('role') != 'standby' \
                or 'tracking' not in (standby_report.get('sync') or {}):
            return case.finish('failed', 'the standby left the '
                               'tracking convergence through field '
                               'loss — its promotion path no longer '
                               'follows the gate: '
                               + json.dumps(standby_report)[:300])
        case.observe('standby still tracking through the outage — '
                     'the promotion path holds on the gate')

        # The recovery half: the plant container comes back as a new
        # server lifetime, so the single-writer claim is gone until the
        # field owner re-attaches and re-arms it through ensure_writer.
        # One tight watch covers both the fail-closed window and the
        # recovery: every iteration probes the field's own fencing
        # answers and the serving monitor, so no probe between the
        # plant's return and the owner's re-arm can slip past the
        # unclaimed verdict.
        try:
            start()
        except Exception as exc:
            return case.finish('inconclusive', 'the plant start '
                               'action never completed: '
                               + str(exc)[:300])
        case.observe('plant container started; watching the '
                     'fail-closed window and the re-arm')

        window = []
        recovery = {'plant': False, 'probe': None, 'snapshot': None,
                    'roles': {}, 'first_tick': None, 'tick_grew': False}
        deadline = time.monotonic() + LINK_RECOVERY_DEADLINE
        while time.monotonic() < deadline:
            for name, url in ((active, base), (peer, peer_base)):
                report = _try_role(ctx, url)
                if report is None:
                    continue
                recovery['roles'][name] = report
                if report.get('role') != expected[name]:
                    return role_violation(name, report, expected,
                                          recovery['roles'])
            listed = _try_plant_ctl(ctx, 'list')
            if listed is not None:
                recovery['plant'] = True
            step_probe = _try_plant(ctx, {'op': 'step', 'dt': 0})
            write_probe = None
            if write_target is not None:
                write_probe = _try_plant(
                    ctx, {'op': 'write', 'point': write_target[0],
                          'value': write_target[1]})
            if step_probe is not None:
                recovery['probe'] = step_probe
            snap = _try_snapshot(ctx, base)
            if snap is not None:
                recovery['snapshot'] = snap
                tick = snap.get('tick') or 0
                if recovery['first_tick'] is None:
                    recovery['first_tick'] = tick
                elif tick > recovery['first_tick']:
                    recovery['tick_grew'] = True
            snap_health = (snap or {}).get('io_health') or {}
            window.append(
                {'plant': listed is not None,
                 'step': _probe_error(step_probe)
                 if step_probe is not None else None,
                 'step_result': (step_probe or {}).get('result'),
                 'write': _probe_error(write_probe)
                 if write_probe is not None
                 else ('skipped' if write_target is None else None),
                 'write_result': (write_probe or {}).get('result'),
                 'tick': (snap or {}).get('tick'),
                 'snapshot': snap is not None})
            if recovery['plant'] and _fenced(step_probe) \
                    and snap is not None and recovery['tick_grew'] \
                    and all(_sample_quality(snap, point) == 'good'
                            for point in field_in) \
                    and (snap_health.get('driver') or {}).get('link') \
                    == 'connected':
                break
            time.sleep(LINK_WINDOW_POLL)
        ref = save_evidence(ctx['evidence_dir'],
                            'plant-link-loss-window.json', window)
        case.evidence('file', ref, 'every fencing probe and monitor '
                      'read between the plant\'s return and the '
                      'recovery')
        ref = save_evidence(ctx['evidence_dir'],
                            'plant-link-loss-recovery.json', recovery)
        case.evidence('file', ref, 'the plant\'s return, the fencing '
                      'probe, and the recovered snapshot')
        if not recovery['plant']:
            return case.finish('inconclusive', 'the restarted plant '
                               'container never served again')

        # The fail-closed verdicts come before the re-arm verdict: a
        # probe that mutated through the window is the sharper defect
        # than a claim that never re-armed.
        if any(round_['step_result'] == 'stepped' for round_ in window):
            return case.finish('failed', 'a mutation probe stepped '
                               'through the unclaimed window — the '
                               'restarted field stood open: '
                               + json.dumps(window)[:300])
        if any(round_['write_result'] == 'done' for round_ in window):
            return case.finish('failed', 'a mutation probe wrote '
                               'silently through the window — the '
                               'restarted field stood open: '
                               + json.dumps(window)[:300])
        # The fail-closed window: the probes before the first fenced
        # answer are the unclaimed window — each must carry the named
        # refusal, never a stepped mutation and never a silent write —
        # while the serving monitor kept answering with advancing
        # scans throughout.
        first_fenced = next(
            (index for index, round_ in enumerate(window)
             if round_['step'] == 'fenced'), None)
        if first_fenced is None:
            return case.finish('failed', 'the restarted plant never '
                               're-armed the single-writer claim — a '
                               'third attachment\'s mutation answered '
                               + json.dumps(recovery['probe'])[:300])
        window_rounds = window[:first_fenced]
        answered = [round_ for round_ in window_rounds
                    if round_['step'] is not None]
        if not answered or any(round_['step'] != 'unclaimed'
                               for round_ in answered):
            return case.finish('failed', 'the fail-closed window never '
                               'observed — no mutation probe answered '
                               'the named unclaimed refusal between '
                               'the plant\'s return and the re-arm: '
                               + json.dumps(answered[-3:])[:300])
        if any(not round_['snapshot'] for round_ in window):
            return case.finish('failed', 'reads went silent through '
                               'the window — the monitor stopped '
                               'answering while the plant returned')
        window_ticks = [round_['tick'] for round_ in window
                        if isinstance(round_['tick'], int)]
        if len(window_ticks) < 2 \
                or window_ticks[-1] <= window_ticks[0]:
            return case.finish('failed', 'the active\'s scans stalled '
                               'through the window — the served tick '
                               'held at ' + str(window_ticks[-1:]))
        case.observe(str(len(answered)) + ' window probes answered '
                     'unclaimed before the re-arm; reads stayed open')

        snap = recovery['snapshot'] or {}
        recovered = {point: _sample_quality(snap, point)
                     for point in field_in}
        if any(q != 'good' for q in recovered.values()):
            return case.finish('failed', 'reads never returned to '
                               'Good after the plant\'s return: '
                               + json.dumps(recovered,
                                            sort_keys=True))
        if not recovery['tick_grew']:
            return case.finish('failed', 'the active\'s scans stalled '
                               'across the plant\'s return — the '
                               'served tick held at '
                               + str(snap.get('tick')))
        health = snap.get('io_health') or {}
        if (health.get('driver') or {}).get('link') != 'connected':
            return case.finish('failed', 'the driver link never '
                               'reported connected after the plant\'s '
                               'return: ' + json.dumps(health)[:400])
        if health.get('failed_reads', 0) < outage_reads \
                or health.get('failed_writes', 0) < outage_writes \
                or not health.get('last_error'):
            return case.finish('failed', 'io_health lost the '
                               'outage\'s recorded failures — the '
                               'counters reset across the recovery: '
                               + json.dumps(health)[:400])
        if not (snap.get('tick') or 0) >= (outage['tick'] or 0):
            return case.finish('failed', 'the served tick regressed '
                               'across the plant\'s return — tick '
                               + str(snap.get('tick')) + ' after '
                               + str(outage['tick']) + ': a controller '
                               'restarted instead of riding out the '
                               'loss')
        case.observe('recovered: reads Good, the writer claim '
                     're-armed, io_health still records '
                     + str(health.get('failed_reads')) + ' failed '
                     'reads and ' + str(health.get('failed_writes'))
                     + ' failed writes')

        # The no-restart leg: neither controller's durable journal file
        # may show a second lifetime — the pair rode the loss out in
        # place. A missing path predates the lane's journal-file seam
        # and skips; an unreadable one cannot prove the leg.
        journals = ctx.get('journal_files') or {}
        lifetimes = {}
        for name in (active, peer):
            path = journals.get(name)
            if path is None:
                continue
            try:
                parsed = _journal_entries(path)
            except (OSError, ValueError) as exc:
                return case.finish('inconclusive', 'the ' + name
                                   + ' journal file is unreadable: '
                                   + str(exc)[:200])
            bounds = [record['run_boundary'] for record in parsed
                      if 'run_boundary' in record]
            seqs = [(record.get('entry') or {}).get('seq')
                    for record in parsed if 'entry' in record]
            lifetimes[name] = {'lifetimes': len(bounds), 'seqs': seqs}
            if len(bounds) != 1:
                return case.finish('failed', name + ' journal file '
                                   'holds ' + str(len(bounds))
                                   + ' lifetimes — a controller '
                                   'restarted across the field loss')
            if not seqs or any(not isinstance(seq, int)
                               for seq in seqs) \
                    or seqs != sorted(seqs) \
                    or len(set(seqs)) != len(seqs):
                return case.finish('failed', name + ' journal seqs do '
                                   'not continue across the loss: '
                                   + str(seqs[:10]))
        ref = save_evidence(ctx['evidence_dir'],
                            'plant-link-loss-lifetimes.json', lifetimes)
        case.evidence('file', ref, 'the journal-file lifetimes across '
                      'the loss')
        case.observe('journal files hold one lifetime each — no '
                     'controller restarted' if lifetimes
                     else 'no journal-file paths in context — '
                     'the restart leg rests on the served tick')

        # The normalized digest: the deterministic rerun contract —
        # categorical verdicts, identical across clean passes.
        digest = {'baseline': 'fenced-good',
                  'outage': 'bad-counted-disconnected',
                  'counters': 'advancing',
                  'journal': 'quality_changed',
                  'standby_gate': 'tracking',
                  'window': 'unclaimed-then-fenced',
                  'writes': 'refused',
                  'reads': 'open',
                  'recovery': 'good-connected-retained',
                  'restarts': 'none'}
        ref = save_evidence(ctx['evidence_dir'],
                            'plant-link-loss-digest.json', digest)
        case.evidence('file', ref, 'the normalized deterministic '
                      'digest')

        # Restore the rig for later scenarios: the plant serving, the
        # pair settled on its entry assignment.
        settled = wait_for(lambda: _settled_active(ctx),
                           time.monotonic() + 30)
        if settled != active:
            return case.finish('failed', 'the pair did not settle back '
                               'on its entry assignment — ' + active
                               + ' no longer reports active')
        case.observe('rig restored: plant serving, ' + active
                     + ' active, ' + peer + ' standby')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
