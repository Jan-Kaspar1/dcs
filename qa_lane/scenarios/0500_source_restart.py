"""The source_restart acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *


# --------------------------------------------------------------------
# WW-LCM-001's continuity clause on the contract QA finding #532 landed
# (merged as #535): a tracking standby whose checkpoint source
# cold-restarts or is replaced — the stream's served tick falling below
# the last alignment, or below the run's own tick before any alignment
# stood — adopts the regressed state at its own run tick under the
# live-derived apply offset (crates/dcs-runtime/src/peer.rs
# `SourceRestart` / `stream_offset`), never rewinding the tick domain
# /history and /journal attribute into, and journals exactly one
# `source_restarted` entry per regression carrying the resync tick,
# the prior alignment, and the resumed stream tick
# (JournalEvent::SourceRestarted).
#
# The induction needs a cold start the state-file-preserving
# `restart_controller` cannot produce: ctx['cold_restart_controller']
# stops the named container, drops its host-side state.json inside the
# bounded run dir — the journal file stays, its new run-boundary marker
# part of the evidence the resume was cold — and starts it again. Leg
# one cold-restarts the settled active: the tracking standby's served
# tick stays monotonic and never below its pre-restart mark, /history
# stays newest-last, and the journaled resync carries the prior
# alignment. Leg two promotes the tracked peer and cold-restarts the
# demoted peer's container: the fresh process's startup claim fences
# the promoted writer into demotion, and the demoted peer's first pull
# — its alignment cleared by the demotion — regresses against the
# run's own tick, journaling the same shape with was_aligned null.
# Both legs leave the launch layout standing (ctrl-a active, ctrl-b
# tracking) for the cases behind this one.

SOURCE_RESTART_POLL = 0.5            # cadence watching the pair mid-restart
SOURCE_RESTART_RETURN_DEADLINE = 60  # the cold peer's monitor returning
SOURCE_RESTART_SETTLE_DEADLINE = 60  # adoption and role settles
SOURCE_RESTART_JOURNAL_DEADLINE = 30  # the source_restarted entry landing
SOURCE_RESTART_BASELINE_DEADLINE = 45  # settled tracking + tick clearance
# The adopting peer's run tick must stand well past where a cold
# process resumes, so the regressed stream is unambiguous.
SOURCE_RESTART_MIN_TICK = 60
# The pull-per-scan cadence re-applies the stream's latest checkpoint
# every cycle, so a tracking peer's served tick jitters by a scan or
# two around the tracked line — the regression the induction produces
# rewinds the domain by the whole run tick, orders of magnitude
# wider.
SOURCE_RESTART_JITTER = 16


def _source_restarts(items):
    """The `source_restarted` events a journal carries, each as
    {'seq', 'tick', 'was_aligned', 'resumed_at'} — for a parsed
    --journal-file's records and a served `GET /journal` tail alike."""
    found = []
    for item in items:
        body = item.get('entry') if isinstance(item.get('entry'), dict) \
            else item
        event = (body or {}).get('event') or {}
        restart = event.get('source_restarted')
        if not isinstance(restart, dict):
            continue
        found.append({'seq': body.get('seq'), 'tick': body.get('tick'),
                      'was_aligned': restart.get('was_aligned'),
                      'resumed_at': restart.get('resumed_at')})
    return found


def scenario_source_restart(ctx):
    """Cold-restart the active peer's container — its state file
    dropped — and prove the tracking standby adopts the regressed
    checkpoint stream at its own run tick; a second leg promotes the
    tracked peer and cold-restarts the demoted peer, journaling the
    no-prior-alignment form."""
    case = Case('source-restart',
                'Regressed checkpoint stream adopts at the run tick',
                'cold-restarting the field owner\'s container — its '
                'host-side state file dropped, the journal retained — '
                'leaves the tracking standby adopting the regressed '
                'checkpoint stream without rewinding its run tick: '
                'the served snapshot tick stays monotonic and never '
                'below the pre-restart mark, /history stays '
                'newest-last, and the durable journal carries exactly '
                'one source_restarted entry per regression naming the '
                'resync tick, the prior alignment, and the resumed '
                'stream tick; the demoted peer\'s first pull records '
                'the no-prior-alignment form; the standby never '
                'spuriously promotes, the field\'s writer claim stands '
                'throughout, and the pair re-settles to one active '
                'and a tracking standby')
    try:
        cold_restart = ctx.get('cold_restart_controller')
        if cold_restart is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no cold-restart action — the '
                               'regression induction has no '
                               'documented seam')
        journals = ctx.get('journal_files') or {}
        journal = journals.get('standby')
        owner_journal = journals.get('active')
        if journal is None or owner_journal is None \
                or ctx.get('plant') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no journal-file paths for the '
                               'pair or no plant endpoint for the '
                               'fencing probes')
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        if active != 'active':
            return case.finish('inconclusive', 'the field owner is '
                               + active + ' — the rig\'s only '
                               'checkpoint-tracking peer — so no '
                               'tracked endpoint can adopt a '
                               'regressed stream')
        base, peer_base = ctx['active'], ctx['standby']
        case.observe('cold-restart target: active (' + base
                     + '); adopting peer standby (' + peer_base + ')')

        # The settled baseline: a tracking peer whose run tick stands
        # well past where a cold process resumes — the regression the
        # induction produces must be unambiguous.
        def tracking():
            report = _try_role(ctx, peer_base)
            if (report or {}).get('role') == 'standby' \
                    and _tracking_aligned(report) is not None:
                return report
            return None

        report = wait_for(tracking,
                          time.monotonic()
                          + SOURCE_RESTART_BASELINE_DEADLINE,
                          interval=SOURCE_RESTART_POLL)
        if report is None:
            return case.finish('inconclusive', 'the standby is not a '
                               'tracking peer — the regression has no '
                               'adopter')
        aligned0 = _tracking_aligned(report)
        snap0 = wait_for(
            lambda: (s.get('tick', 0) >= SOURCE_RESTART_MIN_TICK
                     and s or None)
            if (s := _try_snapshot(ctx, peer_base)) else None,
            time.monotonic() + SOURCE_RESTART_BASELINE_DEADLINE,
            interval=SOURCE_RESTART_POLL)
        if snap0 is None:
            return case.finish('inconclusive', 'the tracking peer\'s '
                               'run tick never reached '
                               + str(SOURCE_RESTART_MIN_TICK)
                               + ' — no room for a regressed stream')
        tick0 = snap0['tick']
        restarts0 = _source_restarts(_journal_entries(journal))
        ref = save_evidence(ctx['evidence_dir'],
                            'source-restart-before.json',
                            {'peer_tick': tick0, 'aligned': aligned0,
                             'source_restarts': restarts0})
        case.evidence('file', ref, 'pre-restart baseline: run tick, '
                      'stream alignment, journaled resyncs')
        case.observe('baseline: peer run tick ' + str(tick0)
                     + ', aligned at stream tick ' + str(aligned0))

        # Leg one: cold-restart the field owner. While its monitor is
        # down the tracking peer's checkpoint pulls miss — the armed
        # failover budget stays unreached on a healthy rig — and the
        # fresh process's first served checkpoint is the regressed
        # stream. The watch records the peer's served tick and role
        # plus the plant's fencing answers on every poll.
        watch = {'peer_ticks': [], 'peer_roles': [], 'fencing': []}
        promoted = []
        # The standby's armed failover budget in seconds — a promotion
        # past it is the documented bound on the outage, not a defect;
        # one inside it is the spurious promotion the contract forbids.
        budget = (ctx.get('failover_misses') or 0) * 0.1

        def restart_returned():
            report = _try_role(ctx, peer_base)
            if report is not None:
                watch['peer_roles'].append(
                    {'role': report.get('role'),
                     'aligned': _tracking_aligned(report)})
                if report.get('role') in ('promoting', 'active'):
                    promoted.append(
                        (report, time.monotonic() - stopped_at))
            snap = _try_snapshot(ctx, peer_base)
            if snap is not None:
                watch['peer_ticks'].append(snap.get('tick'))
            probe = _try_plant(ctx, {'op': 'step', 'dt': 0})
            if probe is not None:
                watch['fencing'].append(bool(_fenced(probe)))
            report = _try_role(ctx, base)
            if report is None or report.get('role') != 'active':
                return None
            return report

        stopped_at = time.monotonic()
        try:
            cold_restart('active')
        except Exception as exc:
            return case.finish('inconclusive', 'the cold-restart '
                               'action never completed: '
                               + str(exc)[:300])
        case.observe('cold restart action returned — watching the '
                     'tracked peer across the outage')
        returned = wait_for(restart_returned,
                            time.monotonic()
                            + SOURCE_RESTART_RETURN_DEADLINE,
                            interval=SOURCE_RESTART_POLL)
        resumed = (returned or {}).get('tick')
        problem = None
        if promoted:
            report, elapsed = promoted[0]
            if budget and elapsed > budget:
                problem = ('inconclusive', 'the tracked peer promoted '
                           + str(elapsed)[:5] + 's into the outage — '
                           'past the armed failover budget of '
                           + str(budget) + 's, the documented bound '
                           'rather than a defect')
            else:
                problem = ('failed', 'the tracked peer reported '
                           + str(report.get('role'))
                           + ' while the cold-restarted controller '
                           'was down — a spurious promotion inside '
                           'the armed failover budget: '
                           + json.dumps(report)[:400])
        elif returned is None:
            problem = ('inconclusive', 'the cold-restarted '
                       'controller never returned')
        elif not isinstance(resumed, int) or resumed >= aligned0:
            problem = ('inconclusive', 'the restarted peer resumed '
                       'at tick ' + str(resumed) + ' — not below the '
                       'last alignment ' + str(aligned0) + ', so the '
                       'induction never produced a regressed stream')
        case.observe('restarted peer '
                     + ('active again at tick ' + str(resumed)
                        if isinstance(resumed, int) else 'not back')
                     + '; tracked peer at '
                     + str(watch['peer_ticks'][-1]
                           if watch['peer_ticks'] else None))
        converged = None
        if problem is None:
            # The adoption itself: the peer reconverges onto the new
            # generation's stream — an alignment below the
            # pre-restart mark names the regression it just absorbed.
            # The watch keeps recording its served tick: the apply
            # lands under the generation offset here, so this window
            # is part of the monotonicity evidence.
            def realigned():
                report = _try_role(ctx, peer_base)
                snap = _try_snapshot(ctx, peer_base)
                if snap is not None:
                    watch['peer_ticks'].append(snap.get('tick'))
                aligned = _tracking_aligned(report)
                if (report or {}).get('role') == 'standby' \
                        and isinstance(aligned, int) \
                        and aligned < aligned0:
                    return report
                return None

            converged = wait_for(realigned,
                                 time.monotonic()
                                 + SOURCE_RESTART_SETTLE_DEADLINE,
                                 interval=SOURCE_RESTART_POLL)
            if converged is None:
                problem = ('failed', 'the tracked peer never '
                           'reconverged onto the regressed stream')
        ref = save_evidence(ctx['evidence_dir'],
                            'source-restart-watch.json', watch)
        case.evidence('file', ref, 'the tracked peer\'s roles and '
                      'ticks plus the field\'s fencing answers across '
                      'the cold restart')
        if problem is not None:
            return case.finish(problem[0], problem[1])
        case.observe('peer tracking the new generation, aligned at '
                     'stream tick '
                     + str(_tracking_aligned(converged)))
        rewound = _tick_rewind(watch['peer_ticks'],
                               tick0 - SOURCE_RESTART_JITTER,
                               SOURCE_RESTART_JITTER)
        if rewound is not None:
            return case.finish('failed', 'the tracked peer\'s served '
                               'tick rewound across the adoption: '
                               'pre-restart ' + str(tick0)
                               + ', observed '
                               + json.dumps(watch['peer_ticks'][:30]))
        if watch['fencing'] and not all(watch['fencing']):
            return case.finish('failed', 'the field\'s writer claim '
                               'lapsed across the cold restart — a '
                               'fencing probe answered unclaimed')
        case.observe('tracked peer\'s served tick stayed monotonic, '
                     'never below ' + str(tick0) + '; the field\'s '
                     'claim fenced throughout')

        # /history on the adopting peer: the run-tick domain it
        # attributes into never rewound, so the served rows stay
        # newest-last with no out-of-order ticks.
        _, history = http_json('GET', peer_base + '/history?since=0')
        disorder = _history_disorder(history, SOURCE_RESTART_JITTER)
        ref = save_evidence(ctx['evidence_dir'],
                            'source-restart-history.json', history)
        case.evidence('file', ref, 'the tracked peer\'s /history '
                      'across the adoption')
        if disorder:
            return case.finish('failed', '/history is not '
                               'newest-last across the resync: '
                               + disorder)

        # The durable audit record: exactly one source_restarted entry
        # for the regression — its entry tick the run tick the resync
        # landed at, was_aligned the alignment the regression broke,
        # resumed_at the regressed checkpoint's own tick.
        resynced = {}

        def journaled():
            try:
                items = _journal_entries(journal)
            except (OSError, ValueError) as exc:
                resynced['error'] = str(exc)
                return None
            resynced['items'] = items
            restarts = _source_restarts(items)
            resynced['restarts'] = restarts
            return restarts if len(restarts) > len(restarts0) \
                else None

        wait_for(journaled,
                 time.monotonic() + SOURCE_RESTART_JOURNAL_DEADLINE,
                 interval=SOURCE_RESTART_POLL)
        restarts1 = resynced.get('restarts') or []
        added = restarts1[len(restarts0):] \
            if restarts1[:len(restarts0)] == restarts0 else None
        _, served_journal = http_json('GET', peer_base + '/journal')
        served_restarts = _source_restarts(_journal_list(
            served_journal))
        ref = save_evidence(ctx['evidence_dir'],
                            'source-restart-journal.json',
                            {'path': str(journal), 'added': added,
                             'restarts': restarts1,
                             'served_restarts': served_restarts,
                             'error': resynced.get('error')})
        case.evidence('file', ref, 'the tracked peer\'s durable '
                      'journal around the resync, with the served '
                      'journal\'s resync records')
        if added is None:
            return case.finish('failed', 'the tracked peer\'s journal '
                               'changed existing records — attribution '
                               'corrupted: ' + str(resynced.get('error')))
        if len(added) != 1:
            return case.finish('failed', 'expected exactly one '
                               'source_restarted entry for the '
                               'regression, found ' + str(len(added))
                               + ': ' + json.dumps(added)[:300])
        entry = added[0]
        if not isinstance(entry.get('was_aligned'), int) \
                or entry['was_aligned'] < aligned0:
            return case.finish('failed', 'the journaled resync does '
                               'not carry the prior alignment ('
                               + str(aligned0) + '): '
                               + json.dumps(entry))
        if not isinstance(entry.get('resumed_at'), int) \
                or entry['resumed_at'] >= entry['was_aligned']:
            return case.finish('failed', 'the journaled resync\'s '
                               'resumed tick is not below the prior '
                               'alignment — the stream never '
                               'regressed or the fields misreport: '
                               + json.dumps(entry))
        if not isinstance(entry.get('tick'), int) \
                or entry['tick'] < tick0:
            return case.finish('failed', 'the journaled resync is '
                               'attributed below the run\'s own '
                               'pre-restart tick — the adoption '
                               'rewound the tick domain: '
                               + json.dumps(entry))
        # The served journal answers from the same record: the
        # resync the durable file carries must appear identically on
        # the monitor's view.
        if not any(s.get('tick') == entry['tick']
                   and s.get('was_aligned') == entry['was_aligned']
                   and s.get('resumed_at') == entry['resumed_at']
                   for s in served_restarts):
            return case.finish('failed', 'the served journal does '
                               'not carry the journaled resync — the '
                               'served and durable records diverge: '
                               + json.dumps(served_restarts)[-300:])
        case.observe('journaled resync at run tick '
                     + str(entry['tick']) + ': was_aligned '
                     + str(entry['was_aligned']) + ', resumed_at '
                     + str(entry['resumed_at']))

        # The cold-resume evidence the retained journal file owes: the
        # restarted peer's file opens a new lifetime whose boundary
        # marker records tick 0 — a warm resume would carry the
        # restored tick instead.
        bounds = {}

        def cold_boundary():
            try:
                bounds['items'] = _journal_entries(owner_journal)
            except (OSError, ValueError) as exc:
                bounds['error'] = str(exc)
                return None
            marks = [item['run_boundary'] for item in bounds['items']
                     if 'run_boundary' in item]
            bounds['marks'] = marks
            return marks if len(marks) >= 2 else None

        marks = wait_for(cold_boundary,
                         time.monotonic()
                         + SOURCE_RESTART_JOURNAL_DEADLINE,
                         interval=SOURCE_RESTART_POLL)
        if marks is None or (marks[-1] or {}).get('tick') != 0:
            return case.finish('failed', 'the cold-restarted peer\'s '
                               'journal lacks a run-boundary marker at '
                               'tick 0 — the cold resume is not on '
                               'the record: '
                               + str(bounds.get('error')
                                     or (marks or [])[-1:]))
        case.observe('the restarted peer\'s journal opened run '
                     + str(marks[-1].get('run')) + ' at tick 0 — '
                     'the cold start on the durable record')

        # Leg two — the no-prior-alignment form. Demote the restarted
        # peer, promote the tracked one, then cold-restart the demoted
        # peer's container: the fresh process's startup claim preempts
        # the promoted writer, whose first fenced write demotes it in
        # place — its alignment cleared by the demotion, so the first
        # pull of the new stream regresses against the run's own tick.
        status, body = http_json('POST', base + '/demote')
        case.observe('demote the restarted peer for leg two: '
                     + str(status) + ' ' + json.dumps(body)[:200])
        if status != 200:
            return case.finish('failed', 'demote refused: '
                               + json.dumps(body)[:300])
        promoted_peer = None
        deadline = time.monotonic() + SOURCE_RESTART_SETTLE_DEADLINE
        while time.monotonic() < deadline and promoted_peer is None:
            try:
                status, body = http_json('POST', peer_base + '/promote')
                if status == 200:
                    promoted_peer = body
                else:
                    time.sleep(SOURCE_RESTART_POLL)
            except urllib.error.HTTPError as exc:
                if exc.code == 409:
                    time.sleep(SOURCE_RESTART_POLL)
                else:
                    raise
        if promoted_peer is None:
            return case.finish('failed', 'the tracked peer never '
                               'promoted — leg two has no writer to '
                               'fence')
        case.observe('tracked peer promoted: '
                     + json.dumps(promoted_peer)[:200])
        snap2 = _snapshot(ctx, peer_base)
        tick2 = snap2.get('tick') or 0

        leg2_stopped = time.monotonic()
        try:
            cold_restart('active')
        except Exception as exc:
            return case.finish('inconclusive', 'the second '
                               'cold-restart action never completed: '
                               + str(exc)[:300])
        leg2 = {'peer_ticks': [], 'peer_roles': [], 'fencing': [],
                'owner_roles': []}
        seen = {'demoted': False}
        repromoted = []

        def leg2_settled():
            report = _try_role(ctx, peer_base)
            if report is not None:
                leg2['peer_roles'].append(report.get('role'))
                if report.get('role') in ('demoting', 'standby'):
                    seen['demoted'] = True
                elif report.get('role') in ('promoting', 'active') \
                        and seen['demoted']:
                    repromoted.append(
                        (report, time.monotonic() - leg2_stopped))
            snap = _try_snapshot(ctx, peer_base)
            if snap is not None:
                leg2['peer_ticks'].append(snap.get('tick'))
            probe = _try_plant(ctx, {'op': 'step', 'dt': 0})
            if probe is not None:
                leg2['fencing'].append(bool(_fenced(probe)))
            owner = _try_role(ctx, base)
            if owner is not None:
                leg2['owner_roles'].append(owner.get('role'))
            if not seen['demoted'] or owner is None \
                    or owner.get('role') != 'active':
                return None
            if (report or {}).get('role') == 'standby' \
                    and _tracking_aligned(report) is not None:
                return {'owner': owner, 'peer': report}
            return None

        settled = wait_for(leg2_settled,
                           time.monotonic()
                           + SOURCE_RESTART_SETTLE_DEADLINE,
                           interval=SOURCE_RESTART_POLL)
        ref = save_evidence(ctx['evidence_dir'],
                            'source-restart-leg2-watch.json', leg2)
        case.evidence('file', ref, 'the fenced demotion and the '
                      'demoted-peer first pull across the second '
                      'cold restart')
        if repromoted:
            report, elapsed = repromoted[0]
            if budget and elapsed > budget:
                return case.finish('inconclusive', 'the demoted peer '
                                   're-promoted ' + str(elapsed)[:5]
                                   + 's into the second outage — past '
                                   'the armed failover budget of '
                                   + str(budget) + 's, the documented '
                                   'bound rather than a defect')
            return case.finish('failed', 'the fenced peer re-took '
                               'the field inside the armed failover '
                               'budget after its demotion: '
                               + json.dumps(report)[:400])
        if 'active' not in leg2['owner_roles']:
            return case.finish('inconclusive', 'the second cold '
                               'restart never returned the demoted '
                               'peer')
        if not seen['demoted']:
            return case.finish('failed', 'the promoted writer was '
                               'never fenced off the field by the '
                               'cold-restarted peer\'s claim')
        if settled is None:
            return case.finish('failed', 'the demoted peer never '
                               'reconverged onto the new stream')
        rewound = _tick_rewind(leg2['peer_ticks'],
                               tick2 - SOURCE_RESTART_JITTER,
                               SOURCE_RESTART_JITTER)
        if rewound is not None:
            return case.finish('failed', 'the demoted peer\'s served '
                               'tick rewound across the no-alignment '
                               'adoption: induction-time '
                               + str(tick2) + ', observed '
                               + json.dumps(leg2['peer_ticks'][:30]))
        if leg2['fencing'] and not all(leg2['fencing']):
            return case.finish('failed', 'the field\'s writer claim '
                               'lapsed across the second cold '
                               'restart — a fencing probe answered '
                               'unclaimed')
        owner_tick = (settled['owner'] or {}).get('tick')
        if not isinstance(owner_tick, int) or owner_tick >= tick2:
            return case.finish('inconclusive', 'the demoted peer '
                               'resumed at tick ' + str(owner_tick)
                               + ' — not below the adopting peer\'s '
                               + str(tick2) + ', so the second '
                               'induction never produced a regressed '
                               'stream')

        # The same journaled shape, no-prior-alignment form: exactly
        # one new source_restarted entry carrying was_aligned null —
        # the demotion cleared the alignment — the resumed stream
        # tick, and an entry tick at the run's own.
        resynced2 = {}

        def journaled2():
            try:
                items = _journal_entries(journal)
            except (OSError, ValueError) as exc:
                resynced2['error'] = str(exc)
                return None
            resynced2['items'] = items
            restarts = _source_restarts(items)
            resynced2['restarts'] = restarts
            return restarts if len(restarts) > len(restarts1) \
                else None

        wait_for(journaled2,
                 time.monotonic() + SOURCE_RESTART_JOURNAL_DEADLINE,
                 interval=SOURCE_RESTART_POLL)
        restarts2 = resynced2.get('restarts') or []
        added2 = restarts2[len(restarts1):] \
            if restarts2[:len(restarts1)] == restarts1 else None
        items2 = resynced2.get('items') or []
        ref = save_evidence(
            ctx['evidence_dir'], 'source-restart-leg2-journal.json',
            {'path': str(journal), 'added': added2,
             'restarts': restarts2,
             'entries_added':
                 items2[len(resynced.get('items') or []):],
             'error': resynced2.get('error')})
        case.evidence('file', ref, 'the demoted peer\'s journal '
                      'through the fenced demotion and regressed '
                      'first pull')
        if added2 is None:
            return case.finish('failed', 'the demoted peer\'s '
                               'journal changed existing records or '
                               'never carried the resync: '
                               + str(resynced2.get('error')))
        if len(added2) != 1:
            return case.finish('failed', 'expected exactly one '
                               'source_restarted entry for the '
                               'demoted-peer regression, found '
                               + str(len(added2)) + ': '
                               + json.dumps(added2)[:300])
        entry2 = added2[0]
        if entry2.get('was_aligned') is not None:
            return case.finish('failed', 'the demoted peer\'s first '
                               'pull journaled a prior alignment — '
                               'the no-prior-alignment form was not '
                               'exercised: ' + json.dumps(entry2))
        if not isinstance(entry2.get('resumed_at'), int) \
                or entry2['resumed_at'] >= tick2:
            return case.finish('failed', 'the demoted-peer resync\'s '
                               'resumed tick is not below the run\'s '
                               'own tick — the no-alignment '
                               'regression never happened or the '
                               'fields misreport: '
                               + json.dumps(entry2))
        if not isinstance(entry2.get('tick'), int) \
                or entry2['tick'] < tick2:
            return case.finish('failed', 'the demoted-peer resync is '
                               'attributed below the run\'s own '
                               'tick: ' + json.dumps(entry2))
        case.observe('demoted-peer resync journaled at run tick '
                     + str(entry2['tick']) + ': was_aligned null, '
                     'resumed_at ' + str(entry2['resumed_at']))

        # Attribution across both legs: the adopting peer's file is
        # one process lifetime, so every journaled entry's run-tick
        # attribution must read non-decreasing in seq order.
        seqs = [item['entry'].get('seq') for item in items2
                if isinstance(item.get('entry'), dict)]
        entry_ticks = [item['entry'].get('tick') for item in items2
                       if isinstance(item.get('entry'), dict)]
        if any(not isinstance(seq, int) for seq in seqs) \
                or seqs != sorted(seqs) or len(set(seqs)) != len(seqs) \
                or any(not isinstance(tick, int) for tick in entry_ticks) \
                or _tick_rewind(entry_ticks, 0,
                                SOURCE_RESTART_JITTER) is not None:
            return case.finish('failed', 'journal attribution is not '
                               'monotonic in seq order across the '
                               'adoptions: seqs ' + str(seqs[:20])
                               + ', ticks ' + str(entry_ticks[:20]))

        # The second cold restart owes the same cold-resume marker on
        # the restarted peer's retained journal: one more lifetime,
        # opened at tick 0.
        marks2 = [item['run_boundary'] for item in
                  _journal_entries(owner_journal)
                  if 'run_boundary' in item]
        ref = save_evidence(ctx['evidence_dir'],
                            'source-restart-boundaries.json',
                            {'owner_journal': str(owner_journal),
                             'owner_boundaries': marks2})
        case.evidence('file', ref, 'the cold-restarted peer\'s '
                      'run-boundary markers — each lifetime opened '
                      'cold')
        if len(marks2) != len(marks) + 1 \
                or (marks2[-1] or {}).get('tick') != 0:
            return case.finish('failed', 'the second cold restart '
                               'did not add a run-boundary marker at '
                               'tick 0 to the restarted peer\'s '
                               'journal: '
                               + json.dumps(marks2)[-300:])
        case.observe('cold-resume markers on the restarted peer\'s '
                     'journal: ' + json.dumps(marks2))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
