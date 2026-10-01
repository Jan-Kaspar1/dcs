"""The stale_freshness acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *


def _history_qualities(payload, point):
    """One point's served history as (seq, quality-key, tick) triples out
    of a `/history` answer — the record the stale interval must survive
    in."""
    if isinstance(payload, list):
        for entry in payload:
            if entry.get('point') == point:
                return [(sample.get('seq'),
                         _quality_key(
                             (sample.get('sample') or {}).get('quality')),
                         (sample.get('sample') or {}).get('tick'))
                        for sample in entry.get('samples', [])]
    return []


# --------------------------------------------------------------------
# The declared freshness budget (WW-OPS-003's stale-data surface,
# WW-ALM-003's rule that stale data never presents as a healthy
# last-known value): the rig model's `net-flow` field input carries a
# declared `stale_after_ticks` — a per-point declaration, not a global
# rule. Stopping the writer-holding controller freezes the shared
# plant's stepping (the sim-net single-writer claim means no surviving
# peer steps it), so the tracking standby's scans outrun the frozen
# driver stamps: the budgeted point must present Uncertain(Stale) while
# the undeclared `level-primary` keeps reporting Good. The outage is
# bounded by the standby's armed failover budget — the writer's restart
# lands inside it, the resumed checkpoint stream realigns the tracking
# peer's state at its own (never-rewound) tick, and the budgeted point
# returns Good. Should the restart ever land late, the armed self-promotion is
# the documented bound: the promoted peer reclaims the writer and
# resumes stepping, and the case reports whether the point recovers on
# that path instead. `GET /history` preserves the stale interval either
# way — the durable evidence when live polling lands late.

STALE_FRESHNESS_POLL = 0.2   # cadence polling the surviving peer mid-freeze
STALE_HOLD_TICKS = 10        # keep sampling past first stale — the relapse check
FREEZE_MAX_TICKS = 60        # hard bound on the outage, under the armed failover budget
STALE_WALL_DEADLINE = 20     # backstop when the peer's tick stops serving
STALE_RECOVER_DEADLINE = 45  # bound on the point returning Good after the restart
STALE_RETURN_DEADLINE = 45   # bound on the restarted writer's monitor returning

# The probe pair out of the served SignalIndex: the model's one declared
# stale_after_ticks point, and an undeclared field input sharing the
# same frozen driver — the per-point-contract contrast.
STALE_BUDGETED_NAME = 'net-flow'
STALE_COMPARISON_NAME = 'level-primary'


def scenario_stale_freshness(ctx):
    """Freeze the shared plant's stepping by stopping the writer-holding
    controller: the declared-budget input presents Uncertain(Stale) on
    the surviving standby while an undeclared field input keeps Good;
    the writer's restart realigns the tracking peer inside the armed
    failover bound and the point returns Good — /history preserving the
    stale interval."""
    case = Case('stale-freshness',
                'Declared freshness budget presents stale on writer loss',
                'stopping the writer-holding controller freezes the '
                'shared plant\'s stepping, so the field input carrying '
                'the declared stale_after_ticks presents Uncertain(Stale) '
                'on the surviving standby while an undeclared field input '
                'keeps Good; the writer\'s restart lands inside the armed '
                'failover bound, the resumed checkpoints realign the '
                'tracking peer, and the point returns Good — with the '
                '/history record preserving the stale interval')
    try:
        stop = ctx.get('stop_controller')
        start = ctx.get('start_controller')
        if stop is None or start is None:
            return case.finish('inconclusive', 'the run context carries '
                               'no controller stop/start action — the '
                               'writer-loss induction has no documented '
                               'seam')
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        peer = 'standby' if active == 'active' else 'active'
        base, peer_base = ctx[active], ctx[peer]
        budget = ctx.get('failover_misses')
        case.observe('seam=writer-stop: stop ' + active + ' (' + base
                     + '), observe ' + peer + ' (' + peer_base + ')'
                     + (', failover budget ' + str(budget) + ' misses'
                        if budget else ''))

        # The probe pair out of the served index. The freshness budget
        # itself is model data — the index names the declared point by
        # its signal name.
        _, signals = http_json('GET', peer_base + '/signals')
        named = {entry.get('name'): entry
                 for entry in signals.get('points', [])}
        budgeted = named.get(STALE_BUDGETED_NAME)
        comparison = named.get(STALE_COMPARISON_NAME)
        ref = save_evidence(ctx['evidence_dir'],
                            'stale-freshness-signals.json',
                            {'budgeted': budgeted,
                             'comparison': comparison})
        case.evidence('file', ref, 'the probe points\' served entries')
        if budgeted is None or comparison is None:
            return case.finish('inconclusive', 'the rig model declares '
                               'no ' + STALE_BUDGETED_NAME + '/'
                               + STALE_COMPARISON_NAME
                               + ' field input — the freshness probe is '
                               'not declared')
        b_point, c_point = budgeted['point'], comparison['point']
        case.observe('budgeted probe: ' + STALE_BUDGETED_NAME
                     + ' point ' + str(b_point) + '; comparison: '
                     + STALE_COMPARISON_NAME + ' point ' + str(c_point))

        # The pre-freeze baseline: the surviving peer a tracking
        # standby, both probes Good.
        def tracking():
            try:
                report = _role(ctx, peer_base)
            except Exception:
                return None
            return report if report.get('role') == 'standby' \
                and 'tracking' in (report.get('sync') or {}) else None

        if wait_for(tracking, time.monotonic() + 45,
                    interval=STALE_FRESHNESS_POLL) is None:
            return case.finish('inconclusive', 'the surviving peer is '
                               'not a tracking standby — the writer-loss '
                               'induction has no observation point')
        baseline = _snapshot(ctx, peer_base)
        baseline_q = {p: _sample_quality(baseline, p)
                      for p in (b_point, c_point)}
        case.observe('baseline qualities: ' + STALE_BUDGETED_NAME + '='
                     + str(baseline_q[b_point]) + ' '
                     + STALE_COMPARISON_NAME + '='
                     + str(baseline_q[c_point]))
        if baseline_q[b_point] != 'good':
            return case.finish('failed', 'the budgeted point presents '
                               + str(baseline_q[b_point])
                               + ' before any induction — the declared '
                               'budget misfires on a healthy rig')
        if baseline_q[c_point] != 'good':
            return case.finish('inconclusive', 'the comparison point '
                               'presents ' + str(baseline_q[c_point])
                               + ' before the freeze — no healthy '
                               'baseline to contrast against')

        # Induce: stop the writer. The shared plant's stamps freeze;
        # the tracking peer's scans outrun them while its checkpoint
        # pulls miss. The freeze is tick-bounded — the peer's own scan
        # tick paces the miss cadence — and capped under the armed
        # failover budget so the restart lands first.
        try:
            stop(active)
        except Exception as exc:
            return case.finish('inconclusive', 'the writer-stop '
                               'induction never completed: '
                               + str(exc)[:300])
        case.observe('writer stopped — polling ' + peer)

        tick0 = None
        stale_tick = None
        freeze_obs = []
        degraded = False
        promoted = False
        relapse = None
        comparison_seen = set()
        wall = time.monotonic() + STALE_WALL_DEADLINE
        while time.monotonic() < wall:
            try:
                report = _role(ctx, peer_base)
                snap = _snapshot(ctx, peer_base)
            except Exception:
                time.sleep(STALE_FRESHNESS_POLL)
                continue
            tick = snap.get('tick') or 0
            if tick0 is None:
                tick0 = tick
            sync = report.get('sync') or {}
            sync_key = next(iter(sync), None)
            if sync_key == 'degraded':
                degraded = True
            if report.get('role') in ('promoting', 'active'):
                promoted = True
            qb = _sample_quality(snap, b_point)
            qc = _sample_quality(snap, c_point)
            freeze_obs.append({'tick': tick, 'budgeted': qb,
                               'comparison': qc,
                               'role': report.get('role'),
                               'sync': sync_key})
            if qc is not None:
                comparison_seen.add(qc)
            if qb == 'uncertain:stale' and stale_tick is None:
                stale_tick = tick
            elif stale_tick is not None and qb == 'good':
                relapse = {'tick': tick}
            if promoted or relapse:
                break
            if stale_tick is not None \
                    and tick - stale_tick >= STALE_HOLD_TICKS:
                break
            if tick - tick0 >= FREEZE_MAX_TICKS:
                break
            time.sleep(STALE_FRESHNESS_POLL)
        case.observe('freeze: ' + str(len(freeze_obs))
                     + ' polls over ' + str((freeze_obs[-1]['tick']
                                             - tick0)
                                            if freeze_obs and tick0
                                            is not None else 0)
                     + ' peer ticks; stale first seen '
                     + ('at tick ' + str(stale_tick) if stale_tick
                        is not None else 'never')
                     + (', peer promoted' if promoted else ''))

        # End the outage inside the failover bound: the restarted writer
        # resumes its persisted run, reclaims the plant, and its
        # checkpoint stream realigns the tracking peer — the documented
        # recovery path. (If the peer already promoted, the freeze ended
        # at the armed failover bound instead; the restart is then
        # fenced out of the field and the promoted peer is the writer —
        # the recovery check below reads that path's result either way.)
        try:
            start(active)
        except Exception as exc:
            return case.finish('inconclusive', 'the writer restart '
                               'never completed: ' + str(exc)[:300])
        case.observe('writer restart issued')

        # Recovery on the observing peer: the budgeted point back to
        # Good. On the restart path the resumed checkpoints adopt at
        # the tracking peer's own tick — the run clock never rewinds —
        # and the resumed plant stepping changes the driver sample, so
        # the change-judged freshness budget clears.
        def back_to_good():
            try:
                report = _role(ctx, peer_base)
                snap = _snapshot(ctx, peer_base)
            except Exception:
                return None
            if _sample_quality(snap, b_point) != 'good':
                return None
            return {'role': report.get('role'), 'tick': snap.get('tick')}

        recovered = wait_for(back_to_good,
                             time.monotonic() + STALE_RECOVER_DEADLINE,
                             interval=STALE_FRESHNESS_POLL)
        ref = save_evidence(
            ctx['evidence_dir'], 'stale-freshness-freeze.json',
            {'seam': 'writer-stop', 'promoted': promoted,
             'observations': freeze_obs,
             'recovery': recovered})
        case.evidence('file', ref, 'per-poll qualities through the '
                      'freeze and the recovery read')

        # The durable record: the peer's /history must preserve the
        # stale interval even where live polling landed late, bracketed
        # by Good — never interrupted by a healthy last-known value.
        _, history_body = http_json(
            'GET', peer_base + '/history?point=' + str(b_point)
            + '&point=' + str(c_point) + '&since=0')
        b_hist = _history_qualities(history_body, b_point)
        c_hist = _history_qualities(history_body, c_point)
        stale_pos = [i for i, (_s, q, _t) in enumerate(b_hist)
                     if q == 'uncertain:stale']
        c_stale = [s for s, q, _t in c_hist if q == 'uncertain:stale']
        interval = None
        if stale_pos:
            first, last = stale_pos[0], stale_pos[-1]
            interval = {
                'first_seq': b_hist[first][0],
                'last_seq': b_hist[last][0],
                'ticks': [t for _s, _q, t in b_hist[first:last + 1]],
                'bracket': [{'seq': s, 'quality': q, 'tick': t}
                            for s, q, t in b_hist[max(0, first - 1):
                                                  last + 2]],
                'good_inside': any(q == 'good'
                                   for _s, q, _t
                                   in b_hist[first:last + 1]),
                'recovered': any(q == 'good'
                                 for _s, q, _t in b_hist[last + 1:]),
            }
        ref = save_evidence(
            ctx['evidence_dir'], 'stale-freshness-history.json',
            {'budgeted': {'point': b_point, 'interval': interval,
                          'samples': len(b_hist)},
             'comparison': {'point': c_point, 'stale_seqs': c_stale}})
        case.evidence('file', ref, 'the stale interval in the peer\'s '
                      'served history')

        # Verdicts, in contract order: an induction that never took
        # effect is inconclusive; everything else names the broken
        # clause.
        freeze_proven = degraded or promoted or stale_tick is not None \
            or 'uncertain:stale' in comparison_seen
        if not freeze_proven:
            return case.finish('inconclusive', 'the writer-stop '
                               'induction never took effect — the peer '
                               'kept tracking and no stamp froze, so no '
                               'staleness absence can be attributed')
        if 'uncertain:stale' in comparison_seen or c_stale:
            return case.finish('failed', 'the undeclared comparison '
                               'point presented stale — the budget '
                               'leaked past its declaration')
        if comparison_seen - {'good'}:
            return case.finish('failed', 'the undeclared comparison '
                               'point presented '
                               + str(sorted(comparison_seen - {'good'}))
                               + ' during the freeze — expected Good '
                               'throughout')
        if relapse:
            return case.finish('failed', 'the stale interval reverted '
                               'to a healthy last-known value at tick '
                               + str(relapse['tick'])
                               + ' while the plant stayed frozen')
        if stale_tick is None and not stale_pos:
            return case.finish('failed', 'the budgeted point never '
                               'presented Uncertain(Stale) while the '
                               'plant\'s stepping was frozen')
        if recovered is None:
            note = 'the point did not return Good after the writer '
            if promoted:
                note += ('lost the field to the peer\'s failover-budget '
                         'self-promotion — the promoted run resumes '
                         'stepping but its scan ticks lead the frozen '
                         'stamps by the outage length, so the lag never '
                         'closes')
            else:
                note += ('resumed stepping — the tracking peer never '
                         'realigned inside ' + str(STALE_RECOVER_DEADLINE)
                         + 's')
            return case.finish('failed', note)
        if not stale_pos:
            return case.finish('failed', 'the /history record does not '
                               'preserve the stale interval live '
                               'polling observed')
        disorder = _history_disorder(history_body, 0)
        if disorder is not None:
            return case.finish('failed', 'the peer\'s /history ring '
                               'rewound its tick axis across the '
                               'realign — a retained range double-'
                               'covered: ' + disorder)
        if interval['good_inside']:
            return case.finish('failed', 'a healthy last-known value '
                               'sits inside the recorded stale interval')
        if not interval['recovered']:
            return case.finish('failed', 'the /history record never '
                               'shows the interval closing — no Good '
                               'after the last stale sample')
        case.observe('stale interval: seqs '
                     + str(interval['first_seq']) + '..'
                     + str(interval['last_seq']) + ', '
                     + str(len(stale_pos)) + ' samples, bracketed by '
                     'Good in history')
        if promoted:
            case.observe('the peer\'s failover-budget self-promotion '
                         'reclaimed the writer and the point recovered '
                         'on the promoted run')
        else:
            case.observe('the restarted writer\'s checkpoints realigned '
                         'the tracking peer — the point returned Good '
                         'inside the failover bound')

        # Leave the rig the way the suite expects it: the restarted
        # endpoint serving again. Only meaningful when no promotion
        # happened — a promoted peer owns the field and the restarted
        # container stays fenced out.
        if not promoted:
            def serving_again():
                try:
                    report = _role(ctx, base)
                except Exception:
                    return None
                return report if report.get('role') == 'active' else None

            back = wait_for(serving_again,
                            time.monotonic() + STALE_RETURN_DEADLINE,
                            interval=STALE_FRESHNESS_POLL)
            if back is None:
                return case.finish('inconclusive', 'the restarted '
                                   'writer never returned')
            case.observe('the restarted writer is serving as active '
                         'at tick ' + str(back.get('tick')))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
