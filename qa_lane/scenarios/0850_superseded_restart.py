"""The superseded_restart acceptance leg — one module per leg
of the scenario schedule; see qa_lane/scenarios/__init__.py for the
ordering rule and the shared seam."""
from .common import *

# Ordering: The superseded-restart case shares the fenced-writer
# degrade case's restored window — both drive the demote-in-place
# contract and hand the pair back on its launch roles — and it runs
# before the legs that need every member as it launched, so the
# restart it performs lands on the pair the following cases read.
RUNS_AFTER = frozenset({'scenario_fenced_writer_degrade'})
RUNS_BEFORE = frozenset({'scenario_failover',
                         'scenario_parameter_tune_carryover'})


# --------------------------------------------------------------------
# The takeover half of the #512/#515 cluster the fenced-writer case
# leaves open — WW-LCM-001's takeover-continuity clause as
# per-revision lane evidence on the simulated rig. The demote-in-place
# leg belongs to `0800_fenced_writer_degrade`; this case stages the
# *restart* of the superseded peer and everything the restart owes.
# The two ctx keys are the launched containers, not the roles they
# currently report: ctrl-a ('active') launched without `--standby`, so
# a restart returns it as a launched active whose cold-start claim
# preempts whoever holds the field; ctrl-b ('standby') launched with
# `--standby`, so a restart returns it rejoining as a tracker. The
# staging keeps that distinction explicit — the container restarted is
# always the launched active.
#
# - (a) The demote-reconvergence leg. With the pair settled and
#   tracking, the documented demote-then-promote switchover runs and
#   the demoted peer must resolve its tracking source per scan cycle
#   and walk back to a converged verdict — the configured `--peer` or
#   the address the peer announced through `GET /checkpoint?peer=` —
#   rather than stranding `unsynchronized`. A later demote/promote on
#   that reconverged peer hands the field back without a restart
#   anywhere: the documented switch order is all a hand-back costs.
#   Where the demoted peer transited `diverged` on its way back — a
#   staged mismatch the switch window opened — the return to
#   `tracking` must journal exactly one `divergence_resolved` carrying
#   every compared point with both sides' values, so the cleared
#   verdict is auditable rather than silent; a peer that cleared on an
#   incomplete comparison, or journaled no resolution at all, is the
#   #541/#542 contract violation this closes.
#
# - (b) The superseded restart. With the promoted peer back on the
#   field and the demoted one converged, the demoted *launched
#   active's* container is restarted through the lane's own restart
#   machinery, so it returns as a launched active whose cold-start
#   claim preempts the promoted peer. Everything that follows is read
#   through the two serving monitors and the field's own claim
#   answers:
#     * exactly one peer reports `active` at every poll after the
#       restart — the field ends owned by one peer, never two;
#     * the preempted peer's first fenced scan demotes it *in place*:
#       `GET /role` walks `demoting` to `standby`, the claim loss
#       journals, and no second process lifetime opens on it;
#     * its writes stop reaching the field — an `ensure_writer` under
#       its own pinned `--owner-token` answers `fenced` while the
#       restartee's writes still land;
#     * a receipted command posted to it answers the named
#       `not_active` rejection rather than applying — the
#       admission-time refusal, never a write that fails on the field;
#     * the plant's field keeps stepping under the surviving owner
#       across the whole window, and that owner's `io_health`
#       never records a fenced write of its own.
#   Where the observation window does catch two peers reporting
#   `active`, the page's own `pairHealth` rule must name the
#   dual-active fault rather than silently serving the first peer it
#   sees reporting active — a monitoring surface that hides the split
#   is the defect the clause exists to prevent.
#
# - (c) The shared-owner-token settlement (#519). One owner's several
#   attachments share its claim: a second plant attachment's
#   `ensure_writer` under the standing owner's own pinned token
#   answers `claimed_shared` naming that one owner, while the
#   superseded owner's token is fenced out — the takeover claimed a
#   fresh token, so two processes sharing a token after the promotion
#   can never both write and step the field.
#
# Rig state is restored for the cases behind this one: both members
# running, ctrl-a owning the field and ctrl-b converged on it. The leg
# is inconclusive when the rig or a settled pair is unreachable, when
# the run publishes no plant endpoint to fence through, when a peer
# pins no owner token, when the model declares no writable bool point
# to command, or when a restart lever never completed — an induction
# that never stood, never a verdict.
#
# Named diagnostics: superseded-restart-failed tags the contract
# clauses — a demoted peer stranded unconverged, a hand-back that
# needed a restart, a cleared-on-incomplete-comparison divergence or
# an unjournaled resolution, two peers reporting active, a preempted
# peer that never demoted in place or whose claim loss went
# unjournaled, a superseded owner whose writes still reach the field,
# a command that applied on the fenced peer, a stalled field, a shared
# token answered fenced or a superseded token answered granted — and
# superseded-restart-nondeterministic the instability the contract does
# not answer for: a member's monitor that stopped answering, a staging
# lever that never completed, or two passes whose evidence digests
# diverge.

RESTART_POLL = 0.4            # cadence watching the pair mid-stage
RESTART_RECONVERGE = 60       # bound on a demoted peer reconverging
RESTART_WINDOW = 45           # bound on the one-active observation window
RESTART_FIELD_TICKS = 3       # served field ticks the survivor must show
RESTART_SETTLE_DEADLINE = 60  # bound on the closing role restore

# The sync-state verdicts a demoted peer returns through: the
# converged ones it must reach, and the unconverged ones it must never
# strand on.
CONVERGED_SYNC = ('tracking', 'orphaned')


def _sync_kind(report):
    """The served StandbySync's variant name — 'unsynchronized' and
    'degraded' are bare strings, the rest single-key objects. None for
    a settled `active`."""
    sync = (report or {}).get('sync')
    if isinstance(sync, str):
        return sync
    if isinstance(sync, dict) and sync:
        return next(iter(sync))
    return None


def _converged(report):
    """Whether a served report reads a standby under a converged sync
    verdict — the posture a demoted peer must walk back to."""
    return (report or {}).get('role') == 'standby' \
        and _sync_kind(report) in CONVERGED_SYNC


def _tracking_standby_report(ctx, key):
    """A served report while the endpoint is a tracking standby, else
    None — the peer's tracking posture, scoped to one ctx key."""
    report = _try_role(ctx, ctx[key])
    if report is not None and _converged(report):
        return report
    return None


def _announced_tracking_source(base):
    """The tracking-source address a peer announces through
    `GET /checkpoint?peer=` — the address its own served checkpoint
    stamps, the source its demotion resolves per scan cycle. None
    where the read drops or the checkpoint names none."""
    try:
        _, served = http_json('GET', base + '/checkpoint?peer=1')
    except Exception:
        return None
    source = (served or {}).get('tracking_source')
    return source if isinstance(source, str) and source else None


def _journal_file_events(path):
    """Every event body a `--journal-file` records, in append order."""
    return [(item.get('entry') or {}).get('event')
            for item in _journal_entries(path)
            if isinstance(item.get('entry'), dict)]


def _journal_file_runs(path):
    """The run-boundary markers a `--journal-file` carries — one per
    process lifetime the file records."""
    return [item['run_boundary'] for item in _journal_entries(path)
            if 'run_boundary' in item]


def _journal_file_events_of(path, kind):
    """The payloads one event kind carries in a `--journal-file`, in
    record order, or None where the file cannot be read."""
    try:
        return [event[kind] for event in _journal_file_events(path)
                if isinstance(event, dict) and kind in event]
    except (OSError, ValueError):
        return None


def _role_walk(events):
    """The (from, to) transitions a journal event list's role_changed
    entries carry, in record order."""
    walk = []
    for event in events:
        change = (event or {}).get('role_changed')
        if isinstance(change, dict):
            walk.append((change.get('from'), change.get('to')))
    return walk


def _walked_down(walk):
    """Whether a role walk demotes in place: active->demoting followed
    by demoting->standby — the transitions the fenced owner's first
    refused write must record."""
    if ('active', 'demoting') not in walk:
        return False
    return ('demoting', 'standby') \
        in walk[walk.index(('active', 'demoting')) + 1:]


def _served_events(base, kind):
    """The `(tick, payload)` stream one journal event kind carries on a
    served `GET /journal`, or None when the read dropped."""
    try:
        _, payload = http_json('GET', base + '/journal?since=0')
    except Exception:
        return None
    return [(entry.get('tick'), (entry.get('event') or {}).get(kind))
            for entry in _journal_list(payload)
            if isinstance((entry.get('event') or {}).get(kind), dict)]


def _resolutions_prove(compared):
    """Whether a `divergence_resolved` record's compared evidence is a
    positive clearing: a nonempty row list where every row carries
    both sides' values and the two agree. `None` where the record names
    no compared rows at all — a resolution that proved nothing."""
    if not compared:
        return None
    return all(isinstance(row, dict)
               and 'staged' in row and 'field' in row
               and row.get('staged') == row.get('field')
               for row in compared)


def _pair_view(*keys):
    """The health view the leg grades: one entry per peer the page
    would be configured with."""
    return [{'key': key, 'report': None, 'error': None}
            for key in keys]


def _pair_poll(ctx, view):
    """One `/role` poll per peer in the view — the pair view's role
    cadence. A failed poll is recorded as pair health; the page reads a
    peer that does not answer as unreachable, never as healthy."""
    for entry in view:
        report = _try_role(ctx, ctx[entry['key']])
        entry['report'] = report
        entry['error'] = None if report is not None else 'unreachable'


def _pair_health(view):
    """The page's pairHealth verdict over the view's last poll: the
    peers reporting `active`, and the redundancy faults it names — an
    unreachable peer, a standby off a converged verdict, no active peer
    at all, and the dual-active split the surface must never silently
    resolve by picking the first peer that reports active."""
    faults, actives = [], []
    for entry in view:
        name = entry['key']
        if entry['error'] is not None:
            faults.append(name + ' unreachable')
            continue
        report = entry['report']
        if report is None:
            continue
        if report.get('role') == 'active':
            actives.append(name)
        if report.get('role') == 'standby' \
                and _sync_kind(report) not in CONVERGED_SYNC:
            faults.append(name + ' standby ' + str(_sync_kind(report)))
    if not actives:
        faults.append('no_active_peer')
    elif len(actives) > 1:
        faults.append('dual_active')
    return {'active': actives[0] if len(actives) == 1 else None,
            'actives': actives,
            'faults': faults}


def _control(url, payload=None):
    """POST a control-plane request — /demote, /promote, and the
    fenced peer's receipted /command — answering
    `(status, body)` with a refused call's named outcome decoded
    instead of raised."""
    try:
        return http_json('POST', url, payload)
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read() or b'null')
        except ValueError:
            body = None
        finally:
            exc.close()
        return exc.code, body


def _switch(ctx, demote_key, promote_key):
    """The documented switch order on one pair: demote the field owner,
    then promote the converged peer. Returns `(demote, promote)` — the
    two `(status, body)` answers, a refused call decoded rather than
    raised."""
    return (_control(ctx[demote_key] + '/demote'),
            _control(ctx[promote_key] + '/promote'))


def _owning(ctx, key):
    """A served report while the endpoint owns the field — the
    promotable posture the reseat wait needs."""
    report = _try_role(ctx, ctx[key])
    if report is not None and report.get('role') == 'active':
        return report
    return None


def scenario_superseded_restart(ctx):
    """Restart the superseded peer: after a demote/promote switchover
    the demoted peer reconverges through its announced or configured
    tracking source and a later promote fails back without a restart,
    the restarted superseded peer's claim preempts the promoted one
    and the preempted peer demotes in place with its fencing loss
    journaled and its commands refused not_active, exactly one peer
    reports active throughout, any dual-active window surfaces the
    pairHealth fault, and the shared --owner-token behaves per the
    landed claim contract."""
    case = Case('superseded-restart',
                'A restarted superseded peer preempts exactly one '
                'field owner',
                'with the pair settled and tracking, the documented '
                'demote/promote switchover leaves the demoted peer '
                'reconverged through its announced or configured '
                'tracking source rather than stranded '
                'unsynchronized, and a later demote/promote on it '
                'hands the field back without a restart; where the '
                'demoted peer transited diverged, its return to '
                'tracking journals exactly one divergence_resolved '
                "naming every compared point with both sides' "
                'values equal; restarting that demoted launched '
                'active leaves exactly one peer reporting active at '
                'every poll, the preempted peer demoting in place to '
                'standby with its claim loss journaled and no second '
                'process lifetime, its writes refused at the field, a '
                'receipted command on it answered not_active, and the '
                'field stepping under the surviving owner throughout; '
                'a window catching two peers active surfaces the '
                'pairHealth dual-active fault rather than serving; the '
                'standing owner\'s several attachments share its claim '
                'under its own pinned token while the superseded '
                'owner\'s token is fenced out; and the pair is restored '
                'to its launch roles afterward')
    stream = None
    active, standby = 'active', 'standby'
    try:
        restart = ctx.get('restart_controller')
        if restart is None:
            return case.finish(
                'inconclusive',
                'the run context carries no controller restart action '
                '— the superseded-restart induction has no documented '
                'seam')
        if not ctx.get('plant'):
            return case.finish('inconclusive',
                               'the run publishes no plant endpoint')
        if not all(ctx.get(name) for name in (active, standby)):
            return case.finish('inconclusive', 'the run context '
                               'publishes no monitor for one of the '
                               'launched pair members')
        tokens = ctx.get('plant_owner') or {}
        journals = ctx.get('journal_files') or {}
        restartee_token = tokens.get(active)
        preempted_token = tokens.get(standby)
        if restartee_token is None or preempted_token is None:
            return case.finish('inconclusive', 'the run pins no '
                               'plant-writer owner tokens for the pair')
        demoted_journal = journals.get(active)
        preempted_journal = journals.get(standby)
        if demoted_journal is None or preempted_journal is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no journal-file paths for '
                               'the pair')
        case.observe('launched active: ' + active + ' (' + ctx[active]
                     + '); launched standby: ' + standby + ' ('
                     + ctx[standby] + ')')


        def settled():
            """The settled launch shape: the launched active owning the
            field with the launched standby converged on it, or None."""
            owner = _try_role(ctx, ctx[active])
            tracker = _try_role(ctx, ctx[standby])
            if (owner or {}).get('role') == 'active' \
                    and _converged(tracker):
                return True
            return None


        if wait_for(settled, time.monotonic() + RESTART_RECONVERGE,
                    interval=RESTART_POLL) is None:
            reachable = any(_try_role(ctx, ctx[name]) is not None
                            for name in (active, standby))
            return case.finish(
                'failed' if reachable else 'inconclusive',
                'the pair never settled in its launch shape — '
                + active + ' must own the field with ' + standby
                + ' converged on it' if reachable
                else 'the pair is unreachable')
        _, signals = http_json('GET', ctx[active] + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'superseded-restart-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming writable '
                      'points')
        target = _writable_bool_point(signals)
        if target is None:
            return case.finish('inconclusive', 'no writable bool '
                               'point in the model')
        point = target['point']
        baseline = _point_value(_snapshot(ctx, ctx[active]), point)
        if not isinstance(baseline, bool):
            return case.finish('inconclusive', 'point ' + str(point)
                               + ' serves no bool baseline to write '
                               'against')
        try:
            bounds0 = _journal_file_runs(demoted_journal)
            preempted_bounds0 = _journal_file_runs(preempted_journal)
        except (OSError, ValueError) as exc:
            return case.finish('inconclusive', 'a pair journal file '
                               'is unreadable: ' + str(exc)[:300])
        # The restartee's own fencing-loss baseline: the takeover must
        # leave its writes clean, so the count is read before the
        # restart and diffed after it.
        restartee_health0 = (_snapshot(ctx, ctx[active])).get(
            'io_health') or {}

        # --- (a) the demote-reconvergence leg ----------------------
        # The documented switch, then the demoted peer's walk back to
        # a converged verdict off the tracking source it resolves per
        # scan cycle. An unconverged final report is the stranding the
        # clause names.
        demote, promote = _switch(ctx, active, standby)
        if demote[0] != 200:
            return case.finish('failed', 'the switchover demote on '
                               + active + ' answered '
                               + str(demote[0]) + ': '
                               + json.dumps(demote[1])[:300])
        if promote[0] != 200:
            return case.finish('failed', 'the switchover promote on '
                               + standby + ' answered '
                               + str(promote[0]) + ': '
                               + json.dumps(promote[1])[:300])
        case.observe('switched: ' + active + ' demoted, '
                     + standby + ' promoted')
        announced = _announced_tracking_source(ctx[active])
        reconverged = wait_for(
            lambda: _tracking_standby_report(ctx, active),
            time.monotonic() + RESTART_RECONVERGE,
            interval=RESTART_POLL)
        demoted_report = _try_role(ctx, ctx[active]) or {}
        ref = save_evidence(
            ctx['evidence_dir'], 'superseded-restart-reconverge.json',
            {'announced': announced, 'demote': demote[1],
             'promote': promote[1], 'role': demoted_report.get('role'),
             'sync': _sync_kind(demoted_report)})
        case.evidence('file', ref, 'the demoted peer\'s announced '
                      'tracking source and its served convergence '
                      'verdict')
        if reconverged is None:
            return case.finish(
                'failed',
                'superseded-restart-failed: the demoted peer never '
                'reconverged after the switchover — GET /role reads '
                + json.dumps(demoted_report)[:300] + ' over the '
                'announced source ' + str(announced) + ': a demoted '
                'peer that cannot resolve a tracking source strands '
                'unsynchronized forever')
        case.observe('the demoted peer reconverged off '
                     + str(announced) + ': '
                     + demoted_report.get('role') + '/ '
                     + str(_sync_kind(demoted_report)))

        # The divergence leg. Where the demoted peer opened or cleared a
        # divergence across the switch, the durable record must carry
        # exactly one `divergence_resolved` whose compared rows prove
        # the clearing on both sides; a resolution naming no rows, or
        # rows whose staged and field sides disagree, is the #541/#542
        # contract violation this closes.
        detected = _journal_file_events_of(demoted_journal,
                                           'divergence_detected')
        resolved = _journal_file_events_of(demoted_journal,
                                           'divergence_resolved')
        served_resolutions = _served_events(ctx[active],
                                            'divergence_resolved')
        ref = save_evidence(
            ctx['evidence_dir'], 'superseded-restart-divergence.json',
            {'detected': detected, 'resolved': resolved,
             'served': served_resolutions})
        case.evidence('file', ref, 'the demoted peer\'s divergence '
                      'detection and resolution records')
        if resolved is None:
            return case.finish('inconclusive', 'the demoted peer\'s '
                               'durable journal became unreadable '
                               'across the divergence audit')
        if detected or resolved:
            if len(resolved) != 1:
                return case.finish(
                    'failed',
                    'superseded-restart-failed: the demoted peer\'s '
                    'durable journal carries ' + str(len(resolved))
                    + ' divergence_resolved records across the '
                    'switchover — a cleared divergence journals exactly '
                    'one')
            proved = _resolutions_prove(
                (resolved[0] or {}).get('compared'))
            if proved is None:
                return case.finish(
                    'failed',
                    'superseded-restart-failed: the demoted peer\'s '
                    'divergence_resolved record names no compared '
                    'points — a cleared verdict proved nothing: '
                    + json.dumps(resolved[0])[:300])
            if not proved:
                return case.finish(
                    'failed',
                    'superseded-restart-failed: the demoted peer '
                    'cleared its divergence on an incomplete '
                    'comparison — divergence_resolved names points whose '
                    'staged and field sides disagree: '
                    + json.dumps(resolved[0])[:300])
            case.observe('one divergence_resolved journaled naming '
                         + str(len(resolved[0].get('compared') or []))
                         + ' compared points')

        # The hand-back: a later demote/promote on the reconverged peer
        # returns the field with no restart anywhere on the pair — the
        # documented switch order is the whole cost.
        handback_demote, handback_promote = _switch(ctx, standby, active)
        if handback_demote[0] != 200 or handback_promote[0] != 200:
            return case.finish(
                'failed',
                'superseded-restart-failed: the hand-back switch '
                'answered demote ' + str(handback_demote[0])
                + ' promote ' + str(handback_promote[0])
                + ' — a reconverged demoted peer '
                'must take the field back on the documented order, with '
                'no restart')
        if wait_for(settled, time.monotonic() + RESTART_RECONVERGE,
                    interval=RESTART_POLL) is None:
            return case.finish(
                'failed',
                'superseded-restart-failed: the pair did not settle '
                'back on its launch roles after the hand-back switch')
        case.observe('the reconverged peer took the field back on the '
                     'documented order with no restart')

        # --- (b) the superseded restart ----------------------------
        # The promoted peer back on the field, then the demoted
        # launched active's own container restarted through the lane's
        # restart machinery: it returns as a launched active whose
        # cold-start claim preempts the promoted incumbent.
        reseat, repromote = _switch(ctx, active, standby)
        if reseat[0] != 200 or repromote[0] != 200:
            return case.finish(
                'failed',
                'superseded-restart-failed: the reseating switch '
                'answered demote ' + str(reseat[0]) + ' promote '
                + str(repromote[0]) + ' — the promoted peer could not be '
                'put back on the field before the restart')
        if wait_for(lambda: _owning(ctx, standby),
                    time.monotonic() + RESTART_RECONVERGE,
                    interval=RESTART_POLL) is None:
            return case.finish(
                'failed',
                'superseded-restart-failed: the promoted peer never '
                'settled active before the restart — the restart '
                'induction has no incumbent to preempt')
        try:
            preempted_bounds1 = _journal_file_runs(preempted_journal)
            preempted_walk0 = _role_walk(
                _journal_file_events(preempted_journal) or [])
            preempted_losses0 = [
                event for event
                in (_journal_file_events(preempted_journal) or [])
                if isinstance(event, dict)
                and 'field_claim_lost' in event]
        except (OSError, ValueError) as exc:
            return case.finish('inconclusive', 'the promoted peer\'s '
                               'journal file is unreadable: '
                               + str(exc)[:300])
        case.observe('the promoted ' + standby + ' is back on the '
                     'field; restarting the demoted launched '
                     + active)
        try:
            restart(active)
        except Exception as exc:
            return case.finish('inconclusive', 'the superseded '
                               'restart never completed: '
                               + str(exc)[:300])

        # The one-active observation window: every poll must see at
        # most one peer reporting active, and where it catches two,
        # the page's own rule must name the dual-active fault rather
        # than silently serving the first peer reporting active.
        view = _pair_view(active, standby)
        polls = []
        dual_windows = []
        ticks = []
        prev_tick = None
        deadline = time.monotonic() + RESTART_WINDOW
        while time.monotonic() < deadline:
            _pair_poll(ctx, view)
            health = _pair_health(view)
            polls.append({'roles': {
                entry['key']: (entry['report'] or {}).get('role')
                for entry in view},
                'actives': health['actives'],
                'faults': health['faults']})
            if len(health['actives']) > 1:
                if 'dual_active' not in health['faults']:
                    return case.finish(
                        'failed',
                        'superseded-restart-failed: two peers reported '
                        'active (' + ', '.join(health['actives'])
                        + ') and the pair-health surface served '
                        + json.dumps(health['faults'])
                        + ' — the dual-active fault must be named, '
                          'never silently resolved to the first peer '
                          'reporting active')
                dual_windows.append({'actives': health['actives'],
                                     'faults': health['faults']})
            if health['active'] == active:
                tick = (_try_snapshot(ctx, ctx[active]) or {}).get('tick')
                if isinstance(tick, int) \
                        and (prev_tick is None or tick > prev_tick):
                    prev_tick = tick
                    ticks.append(tick)
            if len(ticks) >= RESTART_FIELD_TICKS and not dual_windows:
                break
            time.sleep(RESTART_POLL)
        ref = save_evidence(
            ctx['evidence_dir'], 'superseded-restart-window.json',
            {'polls': polls, 'dual_windows': dual_windows,
             'field_ticks': ticks})
        case.evidence('file', ref, 'the post-restart one-active '
                      'window, its dual-active observations, and the '
                      'surviving owner\'s advancing field ticks')
        if not polls:
            return case.finish('inconclusive', 'no poll after the '
                               'restart answered on either peer — '
                               'the observation window never stood')
        split = [poll for poll in polls if len(poll['actives']) > 1]
        if split:
            return case.finish(
                'failed',
                'superseded-restart-failed: ' + str(len(split)) + ' of '
                + str(len(polls)) + ' polls after the restart saw two '
                'peers reporting active — the takeover must leave the '
                'field owned by exactly one peer: '
                + json.dumps(split[0])[:300])
        if len(ticks) < RESTART_FIELD_TICKS:
            return case.finish(
                'failed',
                'superseded-restart-failed: the restartee '
                + active + ' advanced its served field '
                + str(len(ticks)) + ' times across the restart window '
                'where ' + str(RESTART_FIELD_TICKS) + ' were required '
                '— the plant stopped stepping under the field\'s '
                'owner')
        case.observe('exactly one peer reported active at every one of '
                     + str(len(polls)) + ' polls; the surviving owner '
                     + active + ' advanced the field ' + str(len(ticks))
                     + ' times'
                     + ('; the dual-active window surfaced the '
                        'pairHealth fault at ' + str(len(dual_windows))
                        + ' poll(s)' if dual_windows else ''))

        # The demote-in-place contract on the preempted peer: its role
        # surface walks demoting to standby, its claim loss journals,
        # and neither journal opens a second process lifetime the fence
        # did not earn. The audit reads only what the takeover added —
        # the switches above walked their own peers in place, so a walk
        # diffed from the whole file would be satisfied by them.
        walk = _role_walk(_journal_file_events(preempted_journal)
                          or [])[len(preempted_walk0):]
        losses = [event['field_claim_lost']
                  for event in _journal_file_events(preempted_journal) or []
                  if isinstance(event, dict)
                  and 'field_claim_lost' in event][len(preempted_losses0):]
        try:
            preempted_bounds2 = _journal_file_runs(preempted_journal)
        except (OSError, ValueError) as exc:
            return case.finish('inconclusive', 'the preempted peer\'s '
                               'journal file is unreadable: '
                               + str(exc)[:300])
        ref = save_evidence(
            ctx['evidence_dir'], 'superseded-restart-journal.json',
            {'walk': walk, 'losses': len(losses),
             'boundaries': preempted_bounds2})
        case.evidence('file', ref, 'the preempted peer\'s role walk, '
                      'its journaled claim loss, and its process '
                      'lifetimes')
        if not walk:
            return case.finish(
                'failed',
                'superseded-restart-failed: the preempted peer never '
                'journaled a role transition — its first fenced scan '
                'did not demote it in place: '
                + json.dumps(preempted_bounds2)[:300])
        if not _walked_down(walk):
            return case.finish(
                'failed',
                'superseded-restart-failed: the preempted peer\'s role '
                'walk is ' + json.dumps(walk) + ' — a fenced owner '
                'demotes in place, active -> demoting -> standby')
        if len(losses) != 1:
            return case.finish(
                'failed',
                'superseded-restart-failed: the preempted peer\'s '
                'journal carries ' + str(len(losses)) + ' '
                'field_claim_lost records — the supersession journals '
                'exactly one')
        if preempted_bounds1 != preempted_bounds0 \
                or preempted_bounds2 != preempted_bounds1:
            return case.finish(
                'failed',
                'superseded-restart-failed: the preempted peer\'s '
                'journal gained a process lifetime across the takeover '
                '— the fence demotes the process in place rather than '
                'restarting it')
        try:
            restartee_bounds = _journal_file_runs(demoted_journal)
        except (OSError, ValueError) as exc:
            return case.finish('inconclusive', 'the restartee\'s '
                               'journal file is unreadable: '
                               + str(exc)[:300])
        if len(restartee_bounds) != len(bounds0) + 1:
            return case.finish(
                'failed',
                'superseded-restart-failed: the restartee\'s journal '
                'carries ' + str(len(restartee_bounds)) + ' process '
                'lifetimes over the pre-restart ' + str(len(bounds0))
                + ' — the restart opened exactly one run boundary: '
                + json.dumps(restartee_bounds)[:300])
        case.observe('the preempted peer walked '
                     + ' -> '.join(str(step) for step in walk)
                     + ' with one field_claim_lost and one process '
                     'lifetime')


        # The field's own account: the claim moved to the restartee's
        # token, the preempted owner's own token is fenced out (its
        # writes no longer reach the field), a bare third-party
        # mutation stays fenced, and the restartee's own writes carry
        # no fenced-write count.
        stream = _plant_connect(ctx)
        preempted_ensure = _plant_request(stream, {
            'op': 'ensure_writer', 'owner': preempted_token})
        probe = _plant_probe(ctx, {'op': 'step', 'dt': 0})
        restartee_health = (_snapshot(ctx, ctx[active])).get(
            'io_health') or {}
        ref = save_evidence(
            ctx['evidence_dir'], 'superseded-restart-field.json',
            {'preempted_ensure': preempted_ensure, 'probe': probe,
             'restartee_io_health': restartee_health,
             'restartee_io_health_baseline': restartee_health0})
        case.evidence('file', ref, 'the claim arbitration after the '
                      'supersession and both peers\' io_health')
        if not _fenced(preempted_ensure):
            return case.finish(
                'failed',
                'superseded-restart-failed: an ensure under the '
                'preempted owner\'s own token was answered '
                + json.dumps(preempted_ensure)[:300] + ' — its writes '
                'still reach the field after the takeover')
        if not _fenced(probe):
            return case.finish(
                'failed',
                'superseded-restart-failed: the field went unclaimed '
                'across the takeover: ' + json.dumps(probe)[:300])
        if (restartee_health.get('failed_writes') or 0) \
                != (restartee_health0.get('failed_writes') or 0):
            return case.finish(
                'failed',
                'superseded-restart-failed: the restartee\'s own '
                'io_health moved across the takeover — it recorded '
                + str(restartee_health.get('failed_writes') or 0)
                + ' fenced writes where its own baseline counted '
                + str(restartee_health0.get('failed_writes') or 0))
        case.observe('the claim moved to the restartee\'s token: the '
                     'preempted owner fenced out, third-party probes '
                     'stay fenced, the restartee\'s io_health clean')

        # The command path on the fenced peer: a receipted command
        # posted to it answers the named not_active refusal at
        # admission, never a write that fails on the field.
        command = {'command': {'write_value': {
            'point': point, 'kind': 'bool',
            'value': {'bool': not baseline}}},
            'actor': 'qa-lane'}
        status, receipt = _control(ctx[standby] + '/command', command)
        outcome = _outcome_key(receipt)
        ref = save_evidence(
            ctx['evidence_dir'], 'superseded-restart-command.json',
            {'command': command, 'status': status,
             'receipt': receipt, 'baseline': baseline})
        case.evidence('file', ref, 'the receipted command submitted to '
                      'the fenced peer')
        if status != 200 or outcome != 'rejected:not_active':
            return case.finish(
                'failed',
                'superseded-restart-failed: a receipted command on the '
                'fenced peer answered ' + str(status) + ' outcome '
                + str(outcome) + ' — the fenced owner must refuse at '
                'admission with the named not_active rejection, never '
                'apply')
        served_value = _point_value(_snapshot(ctx, ctx[standby]), point)
        if served_value != baseline:
            return case.finish(
                'failed',
                'superseded-restart-failed: the fenced peer\'s refused '
                'write changed the served value to '
                + str(served_value) + ' over the baseline '
                + str(baseline) + ' — a refused admission must leave the '
                'field untouched')
        case.observe('the fenced peer refused a receipted command with '
                     'the named not_active rejection')

        # --- (c) the shared-owner-token settlement ------------------
        # One owner\'s several attachments share its claim: a second
        # plant attachment\'s conditional claim under the standing
        # owner\'s own pinned token is granted as `claimed_shared` and
        # the field keeps naming that one owner. The superseded
        # owner\'s token stays fenced — the takeover claimed a fresh
        # token, so two processes sharing one can never both write and
        # step the field.
        shared = _plant_request(stream, {
            'op': 'ensure_writer', 'owner': restartee_token})
        after_shared = _plant_probe(ctx, {'op': 'step', 'dt': 0})
        preempted_again = _plant_request(stream, {
            'op': 'ensure_writer', 'owner': preempted_token})
        ref = save_evidence(
            ctx['evidence_dir'], 'superseded-restart-token.json',
            {'shared': shared, 'probe': after_shared,
             'preempted_ensure': preempted_again,
             'restartee_token': restartee_token,
             'preempted_token': preempted_token})
        case.evidence('file', ref, 'the shared-owner-token '
                      'settlement: one owner\'s several attachments '
                      'under one token, the superseded token fenced '
                      'out')
        if shared.get('result') != 'claimed_shared' \
                or shared.get('owner') != restartee_token:
            return case.finish(
                'failed',
                'superseded-restart-failed: a second attachment\'s '
                'claim under the standing owner\'s own pinned token '
                'answered ' + json.dumps(shared)[:300] + ' — one '
                'controller\'s several attachments share its claim')
        if not _fenced(after_shared):
            return case.finish(
                'failed',
                'superseded-restart-failed: the field stopped fencing '
                'third-party mutations under the shared owner token: '
                + json.dumps(after_shared)[:300])
        if not _fenced(preempted_again):
            return case.finish(
                'failed',
                'superseded-restart-failed: the superseded owner\'s '
                'own token reclaimed the field after the takeover — '
                'two processes must never share the token and both '
                'write: ' + json.dumps(preempted_again)[:300])
        case.observe('the restartee\'s several attachments share its '
                     'claim under its own pinned token while the '
                     'superseded owner\'s token stays fenced out')


        # The restore: the pair back on its launch roles. The takeover
        # already left the restartee on the field, so the restore is
        # proved from the other direction too — the field handed to the
        # other member and back through the documented order — so a
        # takeover can never leave the pair wedged for the legs behind
        # this one whichever way it landed.
        swap_demote, swap_promote = _switch(ctx, active, standby)
        if swap_demote[0] != 200 or swap_promote[0] != 200:
            return case.finish(
                'failed',
                'superseded-restart-failed: the restore swap answered '
                'demote ' + str(swap_demote[0]) + ' promote '
                + str(swap_promote[0]) + ' — the pair could not be '
                'handed off the restartee after the supersession')
        if wait_for(lambda: _owning(ctx, standby),
                    time.monotonic() + RESTART_SETTLE_DEADLINE,
                    interval=RESTART_POLL) is None:
            return case.finish(
                'failed',
                'superseded-restart-failed: the restore swap left no '
                'field owner — the pair lost the field outright after '
                'the supersession')
        restore_demote, restore_promote = _switch(ctx, standby, active)
        if restore_demote[0] != 200 or restore_promote[0] != 200:
            return case.finish(
                'failed',
                'superseded-restart-failed: the restore switch '
                'answered demote ' + str(restore_demote[0])
                + ' promote ' + str(restore_promote[0])
                + ' — the pair could not be handed '
                'back to its launch roles')
        if wait_for(settled, time.monotonic() + RESTART_SETTLE_DEADLINE,
                    interval=RESTART_POLL) is None:
            return case.finish(
                'failed',
                'superseded-restart-failed: the pair did not settle '
                'back to its launch roles after the supersession — '
                + active + ' must own the field with ' + standby
                + ' converged on it')
        case.observe('the pair stands restored on its launch roles')
    except Exception as exc:
        return case.finish('failed', 'superseded-restart-failed: the '
                           'leg raised ' + repr(exc)[:300])
    finally:
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
    return case.finish('passed')